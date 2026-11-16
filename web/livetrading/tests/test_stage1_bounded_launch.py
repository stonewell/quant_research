"""Bounded Stage-1 launch: a slow child must be LIMITED, VISIBLE and DISTINCTLY reported.

FORENSIC CONTEXT (2026-11-15 incident)
--------------------------------------
A live Stage-1 health check ran for 30+ minutes with **no output at all**.  The operator
read that as "hung"; py-spy on the live PID showed the child was actually *progressing*,
slowly.  Three separate defects made "progressing" indistinguishable from "wedged" and
made the round impossible to abort:

1. ``subprocess.run(cmd, capture_output=True, text=True)`` had **no ``timeout=``** and
   ``proc.stdout`` was read only *after* the child exited -> an unbounded, invisible wait.
2. ``_get_subprocess_env`` did not set ``PYTHONUNBUFFERED``, so the child's ``print()`` into
   a pipe stayed in an ~8 KB block buffer: nothing reached the parent until the buffer
   filled or the process ended (and nothing at all survived a kill).
3. The ``async def`` ``/api/run-health`` route called the blocking service inline, so
   FastAPI's event loop -- the whole station -- was frozen for the entire run.

The tests below pin the three fixes, 100% offline: the ONLY child ever launched is a stub
script written into ``tmp_path`` (``print`` + ``sleep``), no real runner, no network, no
market data.  ``subprocess.run`` is monkeypatched wherever the launch itself is not the
subject.

DISCRIMINATION (measured, not assumed)
--------------------------------------
* Dropping ``timeout=`` from the ``subprocess.run`` call makes the real-timeout test hang
  (it would wait out the stub's sleep) and fails the ``timeout`` kwargs assertion.
* Dropping ``PYTHONUNBUFFERED=1`` makes the partial-output assertion fail: the stub uses
  PLAIN ``print()`` on purpose, so with a block-buffered pipe the killed child leaves
  ``stdout == ""``.
* Calling the service inline in the route again makes the concurrency test's ``/api/config``
  probe wait out the whole stubbed child (latency >= the sleep instead of ~0).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from execution_service import (  # noqa: E402
    STAGE1_DEFAULT_TIMEOUT_SECONDS,
    STAGE1_TIMEOUT_ENV,
    STAGE1_TIMEOUT_ERROR_KIND,
    ExecutionService,
    resolve_stage1_timeout_seconds,
)
from server import create_app  # noqa: E402
from storage import StorageManager  # noqa: E402


@pytest.fixture
def temp_workspace():
    """Hermetic workspace: every writable path under the OS temp root."""
    temp_dir = tempfile.mkdtemp()
    data_dir = os.path.join(temp_dir, "data")
    archive_dir = os.path.join(temp_dir, "runs")
    cache_dir = os.path.join(temp_dir, "duckdb_cache")
    settings_dir = os.path.join(temp_dir, "settings")
    for d in (data_dir, archive_dir, cache_dir, settings_dir):
        os.makedirs(d, exist_ok=True)

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=data_dir,
        archive_dir=archive_dir,
        cache_dir=cache_dir,
        settings_dir=settings_dir,
    )
    yield storage, temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def service(temp_workspace):
    storage, temp_dir = temp_workspace
    return ExecutionService(storage=storage), storage, temp_dir


class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _strategy_file(temp_dir: str) -> str:
    path = os.path.join(temp_dir, "strat.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"template_name": "dummy", "params": {}}, f)
    return path


def _patch_subprocess(monkeypatch):
    """Capture the launch kwargs and never spawn a real child."""
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _write_slow_stub(temp_dir: str, sleep_seconds: float = 30.0) -> str:
    """A child that reports progress through PLAIN ``print()`` and then sleeps.

    Plain ``print()`` is deliberate: it is block-buffered on a pipe unless the child is
    started with ``PYTHONUNBUFFERED=1``, which is exactly the behaviour under test.  With
    the fix, the two lines below are in the parent's hands at kill time; without it, the
    pipe is empty and the operator learns nothing.
    """
    path = os.path.join(temp_dir, "slow_stage1_stub.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "import time\n"
            "print('[STUB] stage1 started')\n"
            "print('[STUB] refreshing market data ...')\n"
            "time.sleep(%r)\n"
            "print('[STUB] never reached')\n" % (float(sleep_seconds),)
        )
    return path


# --------------------------------------------------------------------------- #
# 1. Defect 2: the child must not block-buffer its stdout
# --------------------------------------------------------------------------- #
def test_child_env_forces_unbuffered_stdout(service, monkeypatch):
    """GUARD: a buffered child is exactly the defect -- its progress never leaves the pipe."""
    svc, _storage, _temp = service
    # An inherited `PYTHONUNBUFFERED=0` must not survive: unbuffered is not opt-in here.
    monkeypatch.setenv("PYTHONUNBUFFERED", "0")
    env = svc._get_subprocess_env()
    assert env["PYTHONUNBUFFERED"] == "1"


def test_child_env_does_not_invent_pythonioencoding(service, monkeypatch):
    """NO SPECULATIVE CONFIG: `PYTHONIOENCODING` has no precedent in tracked code.

    Pinning `utf-8` here would silently change how a child encodes its Chinese gate text on
    a non-UTF-8 host -- a separate decision, deliberately not taken by this fix.
    """
    svc, _storage, _temp = service
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    assert "PYTHONIOENCODING" not in svc._get_subprocess_env()


# --------------------------------------------------------------------------- #
# 2. Defect 1a: the budget itself (default, override, opt-out, typo)
# --------------------------------------------------------------------------- #
def test_default_budget_is_generous_when_env_absent(monkeypatch):
    """The observed honest run needed 30+ min, so the default must not be near 30 min."""
    monkeypatch.delenv(STAGE1_TIMEOUT_ENV, raising=False)
    budget = resolve_stage1_timeout_seconds()
    assert budget == STAGE1_DEFAULT_TIMEOUT_SECONDS
    assert budget >= 45 * 60, (
        "a default below ~45 min would kill the legitimately slow runs the 2026-11-15 "
        "incident was actually made of (known data-path problem, fixed separately)"
    )


def test_env_override_controls_the_budget(monkeypatch):
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "1234.5")
    assert resolve_stage1_timeout_seconds() == 1234.5


def test_zero_or_negative_disables_the_bound(monkeypatch):
    """The documented escape hatch: an operator who KNOWS the round needs longer."""
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "0")
    assert resolve_stage1_timeout_seconds() is None
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "-5")
    assert resolve_stage1_timeout_seconds() is None


def test_unparsable_override_falls_back_to_the_default(monkeypatch):
    """A typo must NOT silently remove the safety net (that is how the defect returns)."""
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "45m")
    assert resolve_stage1_timeout_seconds() == STAGE1_DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# 3. Defect 1b: the launch site -- bounded, unbuffered, and the success path intact
# --------------------------------------------------------------------------- #
def test_launch_passes_the_budget_and_the_unbuffered_env(service, monkeypatch):
    """The call site must actually USE both knobs (a helper nobody calls fixes nothing)."""
    svc, _storage, temp_dir = service
    monkeypatch.delenv(STAGE1_TIMEOUT_ENV, raising=False)
    calls = _patch_subprocess(monkeypatch)

    result = svc.run_health_check(
        strategy_file=_strategy_file(temp_dir),
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
        data_provider="synthetic",
    )

    assert len(calls) == 1
    assert calls[0]["kwargs"]["timeout"] == STAGE1_DEFAULT_TIMEOUT_SECONDS
    assert calls[0]["kwargs"]["capture_output"] is True
    assert calls[0]["kwargs"]["text"] is True
    assert calls[0]["kwargs"]["env"]["PYTHONUNBUFFERED"] == "1"
    # The mocked child "succeeded", so the pre-existing return shape must be untouched.
    assert result["status"] == "success"


def test_env_override_reaches_the_launch_site(service, monkeypatch):
    svc, _storage, temp_dir = service
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "77")
    calls = _patch_subprocess(monkeypatch)

    svc.run_health_check(
        strategy_file=_strategy_file(temp_dir),
        custom_symbols=["600519.SH"],
        data_provider="synthetic",
    )
    assert calls[0]["kwargs"]["timeout"] == 77.0


def test_real_timeout_returns_a_distinct_state_with_the_partial_output(
    service, monkeypatch
):
    """THE INCIDENT ITSELF: a child that never finishes is bounded and *reported*.

    A REAL (but stubbed, offline) child is launched so the kill semantics, the unbuffered
    pipe and the partial-output capture are exercised end to end -- not mocked away.
    """
    svc, storage, temp_dir = service
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "1.0")
    storage.stage1_script = _write_slow_stub(temp_dir, sleep_seconds=30.0)
    storage.python_executable = sys.executable

    started = time.monotonic()
    result = svc.run_health_check(
        strategy_file=_strategy_file(temp_dir),
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
        data_provider="synthetic",
    )
    elapsed = time.monotonic() - started

    # BOUNDED: the 30s stub cannot outlive the 1s budget (plus interpreter start-up slack).
    assert elapsed < 20.0, f"the launch was not bounded (elapsed={elapsed:.1f}s)"

    # DISTINCT STATE, not a traceback and not a success.
    assert result["status"] == "error"
    assert result["error_kind"] == STAGE1_TIMEOUT_ERROR_KIND
    assert result["timed_out"] is True
    assert result["timeout_seconds"] == 1.0
    assert result["health_report"] is None
    assert result["account_state"] is None
    assert "超时" in result["message"]
    assert STAGE1_TIMEOUT_ENV in result["message"], (
        "the operator must be told which knob raises the budget"
    )

    # PARTIAL OUTPUT SURVIVED: this is what `PYTHONUNBUFFERED=1` buys. The stub prints with
    # plain `print()`, so a block-buffered pipe would leave this EMPTY.
    assert "[STUB] stage1 started" in result["stdout"]
    assert "[STUB] refreshing market data ..." in result["stdout"]
    assert "never reached" not in result["stdout"]

    # ARCHIVED HONESTLY: a later reader (or Stage 2) must never mistake it for a gate pass.
    summaries = glob.glob(os.path.join(storage.archive_dir, "**", "summary.json"), recursive=True)
    assert len(summaries) == 1
    with open(summaries[0], "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["health_status"] == "TIMEOUT"
    assert summary["health_output_truncated"] is True
    assert summary["health_timeout_seconds"] == 1.0
    assert summary["error_kind"] == STAGE1_TIMEOUT_ERROR_KIND
    assert summary["health_returncode"] is None
    assert "[STUB] stage1 started" in summary["health_stdout"]
    # No fabricated gate report was written for a round that produced no verdict.
    assert not os.path.exists(
        os.path.join(os.path.dirname(summaries[0]), "stage1_health_report.json")
    )


def test_completed_child_keeps_the_original_return_shape(service, monkeypatch):
    """BACKWARD COMPATIBILITY: a normal round must not grow a timeout marker."""
    svc, _storage, temp_dir = service
    _patch_subprocess(monkeypatch)

    result = svc.run_health_check(
        strategy_file=_strategy_file(temp_dir),
        custom_symbols=["600519.SH"],
        data_provider="synthetic",
    )
    assert result["status"] == "success"
    assert "timed_out" not in result
    assert "timeout_seconds" not in result
    assert result["message"] == "Stage 1 Health Check executed successfully"


# --------------------------------------------------------------------------- #
# 4. Defect 3: the route must not freeze the event loop
# --------------------------------------------------------------------------- #
def test_stage1_route_keeps_the_station_responsive_while_the_child_runs(service, monkeypatch):
    """GUARD: the blocking service call must run OFF the event loop.

    The service is replaced by a short blocking sleep standing in for the child.  While the
    POST is in flight, an unrelated GET must still be served promptly: inline (the defect)
    it could only be answered after the sleep finished, because the loop itself was asleep.
    """
    _svc, storage, _temp = service
    app = create_app(storage)
    sleep_seconds = 1.0
    calls = []

    def _slow_health_check(**kwargs):
        calls.append(kwargs)
        time.sleep(sleep_seconds)  # stands in for the blocking child
        return {
            "status": "success",
            "message": "ok",
            "date": "2026-11-13",
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(app.state.execution_service, "run_health_check", _slow_health_check)

    async def _probe():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            t0 = time.monotonic()
            health = asyncio.create_task(
                client.post(
                    "/api/run-health",
                    json={"strategy_file": "s.json", "portfolio_value": 100000.0},
                )
            )
            # Let the POST reach the route and enter the blocking call.
            await asyncio.sleep(0.12)
            other = await client.get("/api/config")
            # Measured from the START of the POST, so a frozen loop is visible: with the call
            # inline, this `await sleep(0.12)` is itself only resumed after the child returns.
            answered_at = time.monotonic() - t0
            return await health, other, answered_at

    health_res, other_res, answered_at = asyncio.run(_probe())

    assert calls, "the route never reached the service"
    assert health_res.status_code == 200
    assert health_res.json()["status"] == "success"
    assert other_res.status_code == 200
    assert other_res.json(), "unexpected empty /api/config payload"
    assert answered_at < 0.6, (
        f"/api/config was only answered after {answered_at:.2f}s, i.e. the Stage-1 call "
        f"held the event loop for the whole {sleep_seconds}s child (no threadpool offload)"
    )


def test_http_payload_tells_the_operator_the_round_timed_out(service, monkeypatch):
    """END TO END (still offline): the payload the dashboard actually receives.

    `RunResponse` FILTERS undeclared keys, so an `error_kind` living only inside the service
    dict would never reach the UI. What must survive the model is therefore `status` (the one
    value the dashboard treats as failure) and `message` (where the timeout is spelled out),
    plus the partial `stdout` the operator needs to see how far the child got.
    """
    from fastapi.testclient import TestClient

    _svc, storage, temp_dir = service
    monkeypatch.setenv(STAGE1_TIMEOUT_ENV, "0.5")
    storage.stage1_script = _write_slow_stub(temp_dir, sleep_seconds=30.0)
    storage.python_executable = sys.executable
    client = TestClient(create_app(storage))

    res = client.post(
        "/api/run-health",
        json={
            "strategy_file": _strategy_file(temp_dir),
            "custom_symbols": ["600519.SH"],
            "portfolio_value": 100000.0,
            "data_provider": "synthetic",
        },
    )

    assert res.status_code == 200
    body = res.json()
    # Loud, never a success toast for a round that produced no gate verdict.
    assert body["status"] == "error"
    assert "超时" in body["message"]
    assert STAGE1_TIMEOUT_ENV in body["message"]
    # The partial progress travels with it (the whole point of the unbuffered pipe).
    assert "[STUB] stage1 started" in body["stdout"]
    assert body["health_report"] is None

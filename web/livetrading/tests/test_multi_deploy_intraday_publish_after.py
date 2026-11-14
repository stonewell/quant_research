"""P1-1 parity on the THIRD request shape: `/api/deploy/multi` must forward
`--intraday-publish-after` exactly like both single-strategy stages do.

THE DEFECT THIS CLOSES (triple gap, all three layers)
-----------------------------------------------------
`RunHealthRequest` (Stage 1) and `RunDeployRequest` (Stage 2) already carry
`intraday_publish_after` and both routes already validate + forward it. The multi-strategy
netted path carried NONE of it:

* `web/livetrading/models.py::MultiStrategyDeployRequest` had no such field (pydantic would
  reject or ignore it -- and the model silently dropped the operator's intent);
* `web/livetrading/server.py::run_multi_strategy_deploy` never forwarded it (and this route
  calls the service with an EXPLICIT keyword list, so a model field not re-listed there is
  accepted by pydantic and then SILENTLY DROPPED -- the same trap that lost `cash` and
  `confirm_empty_book` before it);
* `web/livetrading/execution_service.py::run_multi_strategy_netted_deploy` had no such
  parameter, so `scripts/run_live_multi_strategy.py`'s own `--intraday-publish-after` (its
  "pending_publication vs missing" second availability axis) was unreachable over HTTP.

THE CONTRACT PINNED HERE
------------------------
* not supplied -> `intraday_publish_after is None` -> the flag is NOT appended: the child
  argv is byte-for-byte the pre-P1-1 argv (asserted as a FULL token list, not "no crash"),
  and `summary.json` records the effective value as `null` -- NOT a second default;
* supplied -> `--intraday-publish-after <value>` is appended last, verbatim, and the value
  the round actually ran with is mirrored into the matrix `summary.json` provenance block;
* malformed -> HTTP 400 at the route, through the SAME validator as the single-strategy
  endpoints (never a silent fallback, never the generic 500 that means "the child failed"),
  refused before any matrix run directory is allocated.

100% OFFLINE: throwaway `StorageManager` (the package conftest already re-points
`cache_dir`/`data_dir`/`archive_dir` at `tmp_path`), `subprocess.run` monkeypatched, no
network, nothing written under the real `data/`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution_service  # noqa: E402
import server  # noqa: E402
import storage  # noqa: E402

AS_OF = "2026-10-20"
START = "2025-01-02"
MATRIX = "tri_core_blend"
FLAG = "--intraday-publish-after"


# --------------------------------------------------------------------------- #
# fixtures / helpers (same shape as the neighbouring offline web tests)
# --------------------------------------------------------------------------- #
class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch):
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _MockProc(returncode=0, stdout="mocked")

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


@pytest.fixture
def workspace(tmp_path):
    dirs = {}
    for name in ("data", "runs", "cache", "settings"):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        dirs[name] = d
    mgr = storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(dirs["data"]),
        archive_dir=str(dirs["runs"]),
        cache_dir=str(dirs["cache"]),
        settings_dir=str(dirs["settings"]),
    )
    return {"storage": mgr, "dirs": dirs, "tmp_path": tmp_path}


def _run_multi(workspace, monkeypatch, **kwargs):
    """One REAL `run_multi_strategy_netted_deploy`; returns `(argv, summary)`."""
    captured = _patch_subprocess(monkeypatch)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    result = svc.run_multi_strategy_netted_deploy(
        matrix_id=MATRIX,
        strategies=[{"strategy_key": "chan_four_state_shield_blend", "capital_weight": 1.0}],
        custom_symbols=["600519.SH", "000001.SZ"],
        as_of_date=AS_OF,
        start_date=START,
        portfolio_value=100_000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        **kwargs,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1, captured
    summary_file = os.path.join(_flag(captured[0], "--output-dir"), "summary.json")
    with open(summary_file, encoding="utf-8") as f:
        summary = json.load(f)
    return captured[0], summary


# --------------------------------------------------------------------------- #
# 1. Not supplied -> byte-identical argv (the whole "nothing changed" claim)
# --------------------------------------------------------------------------- #
def test_multi_argv_is_unchanged_when_the_flag_is_not_supplied(workspace, monkeypatch):
    argv, _summary = _run_multi(workspace, monkeypatch)

    assert FLAG not in argv, "an unsupplied value must not add a token"
    st = workspace["storage"]
    run_dir = _flag(argv, "--output-dir")
    # The FULL token list, in order: this is the whole "byte-identical to the old argv"
    # claim. Anything appended, reordered or normalised shows up here.
    assert argv == [
        st.python_executable,
        st.multi_netting_script,
        "--strategies",
        "chan_four_state_shield_blend:1.0",
        "--universe-file",
        os.path.join(run_dir, "resolved_universe.txt"),
        "--portfolio-value",
        "100000.0",
        "--current-holdings-file",
        os.path.join(run_dir, "holdings.json"),
        "--output-dir",
        run_dir,
        "--data-provider",
        "synthetic",
        "--min-holding-bars",
        "2",
        "--inertia-threshold",
        "0.05",
        "--as-of-date",
        AS_OF,
        "--start-date",
        START,
        "--cache-dir",
        st.cache_dir,
        "--allow-intraday",
    ]


def test_multi_summary_records_null_when_the_flag_is_not_supplied(workspace, monkeypatch):
    """`null` = "the operator supplied none". It must never read as a pinned default."""
    _argv, summary = _run_multi(workspace, monkeypatch)

    assert "intraday_publish_after" in summary, (
        "the effective value must be observable, including the unset case"
    )
    assert summary["intraday_publish_after"] is None


# --------------------------------------------------------------------------- #
# 2. Supplied -> appended last, verbatim, and recorded
# --------------------------------------------------------------------------- #
def test_multi_forwards_the_flag_verbatim_and_records_it(workspace, monkeypatch):
    argv, summary = _run_multi(workspace, monkeypatch, intraday_publish_after="21:30")

    assert _flag(argv, FLAG) == "21:30"
    # Appended at the END, so every pre-existing token keeps its position (the reason the
    # "not supplied" case above can stay byte-identical).
    assert argv[-2:] == [FLAG, "21:30"]
    assert summary["intraday_publish_after"] == "21:30"


def test_multi_strips_surrounding_whitespace(workspace, monkeypatch):
    argv, summary = _run_multi(workspace, monkeypatch, intraday_publish_after=" 09:05 ")

    assert _flag(argv, FLAG) == "09:05"
    assert summary["intraday_publish_after"] == "09:05"


def test_blank_value_is_treated_as_not_supplied(workspace, monkeypatch):
    argv, summary = _run_multi(workspace, monkeypatch, intraday_publish_after="   ")

    assert FLAG not in argv
    assert summary["intraday_publish_after"] is None


def test_service_rejects_a_malformed_value_before_any_side_effect(workspace, monkeypatch):
    """Direct (non-HTTP) callers get the SAME refusal, before the run dir exists."""
    captured = _patch_subprocess(monkeypatch)
    svc = execution_service.ExecutionService(storage=workspace["storage"])

    with pytest.raises(ValueError) as excinfo:
        svc.run_multi_strategy_netted_deploy(
            matrix_id=MATRIX,
            custom_symbols=["600519.SH"],
            as_of_date=AS_OF,
            current_holdings={"600519.SH": 100},
            data_provider="synthetic",
            intraday_publish_after="25:00",
        )

    assert FLAG in str(excinfo.value)
    assert captured == [], "the child must never be launched on a malformed value"
    assert not os.path.exists(
        os.path.join(str(workspace["dirs"]["runs"]), AS_OF)
    ), "refused BEFORE the matrix run directory was allocated"


# --------------------------------------------------------------------------- #
# 3. Route: same 400 as the single-strategy endpoints, through the same validator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["25:00", "abc", "9:30", "20:60", "20:00:00"])
def test_route_rejects_malformed_values_with_400(workspace, bad):
    app = server.create_app(workspace["storage"])
    client = TestClient(app)

    response = client.post(
        "/api/deploy/multi",
        json={
            "matrix_id": MATRIX,
            "custom_symbols": ["600519.SH"],
            "as_of_date": AS_OF,
            "intraday_publish_after": bad,
        },
    )

    assert response.status_code == 400, response.text
    assert FLAG in response.json()["detail"]
    # Refused BEFORE the service ran: no matrix run directory was ever allocated.
    for dirpath, _dirnames, filenames in os.walk(str(workspace["dirs"]["runs"])):
        assert "summary.json" not in filenames, os.path.join(dirpath, "summary.json")


def _record_service_kwargs(monkeypatch, app, method_name):
    """Record what the route hands the service, then run the real method.

    `create_app` builds the `ExecutionService` INSTANCE and the route closes over it, so an
    instance-level attribute is what the route's `execution_service.<method>(...)` lookup
    resolves. A dropped field is simply ABSENT from the recorded kwargs (it does not arrive
    as `None`), which is exactly the silent-drop defect this test guards.
    """
    seen = []
    real = getattr(app.state.execution_service, method_name)

    def _spy(**kwargs):
        seen.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(app.state.execution_service, method_name, _spy)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _MockProc(returncode=0, stdout="mocked"))
    return seen


def test_route_forwards_the_value_to_the_service(workspace, monkeypatch):
    """The silent-drop guard: the route's explicit keyword list must re-list the field."""
    app = server.create_app(workspace["storage"])
    seen = _record_service_kwargs(monkeypatch, app, "run_multi_strategy_netted_deploy")

    response = TestClient(app).post(
        "/api/deploy/multi",
        json={
            "matrix_id": MATRIX,
            "custom_symbols": ["600519.SH"],
            "as_of_date": AS_OF,
            "current_holdings": {"600519.SH": 100},
            "data_provider": "synthetic",
            "intraday_publish_after": "21:30",
        },
    )

    assert response.status_code == 200, response.text
    assert len(seen) == 1, seen
    assert seen[0].get("intraday_publish_after") == "21:30"


def test_route_leaves_an_omitted_value_alone(workspace, monkeypatch):
    """Omitted -> `None` reaches the service, and the model default is None (not 20:00)."""
    from models import MultiStrategyDeployRequest

    assert MultiStrategyDeployRequest().intraday_publish_after is None

    app = server.create_app(workspace["storage"])
    seen = _record_service_kwargs(monkeypatch, app, "run_multi_strategy_netted_deploy")

    response = TestClient(app).post(
        "/api/deploy/multi",
        json={
            "matrix_id": MATRIX,
            "custom_symbols": ["600519.SH"],
            "as_of_date": AS_OF,
            "current_holdings": {"600519.SH": 100},
            "data_provider": "synthetic",
        },
    )

    assert response.status_code == 200, response.text
    assert len(seen) == 1, seen
    assert "intraday_publish_after" in seen[0], (
        "the field must be re-listed in the route's explicit keyword list"
    )
    assert seen[0]["intraday_publish_after"] is None

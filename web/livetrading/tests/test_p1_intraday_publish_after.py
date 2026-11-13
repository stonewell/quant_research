"""P1-1 (2026-11-12): `--intraday-publish-after` must reach Stage 1 and Stage 2 -- and
must change NOTHING when the operator did not ask for it.

THE DEFECT THIS CLOSES
----------------------
Both child runners already define `--intraday-publish-after HH:MM`
(`scripts/check_live_portfolio_health.py`, `scripts/run_live_strategy.py`) with a 20:00
default, and the value only decides whether a not-yet-published 5m layer is reported as
`pending_publication` (wait) or `missing` (sync defect). The web layer never forwarded it,
so an operator who knew the session's 5m bars land later had no way to say so -- and,
because both `server.py` routes call the service with an EXPLICIT keyword list, a new
pydantic field that is not re-listed there is silently dropped (the exact trap that lost
`cash` and `confirm_empty_book` before it).

THE CONTRACT PINNED HERE
------------------------
* not supplied -> `intraday_publish_after is None` -> the flag is NOT appended: the argv
  is byte-for-byte the pre-P1-1 argv (asserted as a FULL token list, not "no crash"), and
  the child keeps its own 20:00 default;
* supplied -> `--intraday-publish-after <value>` is appended, on BOTH stages, so the gate
  and the deploy judge the same 5m availability axis;
* malformed -> HTTP 400 at the route (never a silent fallback to 20:00, never the generic
  500 that means "the child failed").

100% OFFLINE: throwaway `StorageManager` + a file-backed DuckDB cache under `tmp_path`,
`subprocess.run` monkeypatched. No network, nothing written under the real `data/`.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pandas as pd
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
from common.duckdb_cache import DuckDBCache  # noqa: E402

PROVIDER = "marketdb"  # the engine reads the throwaway cache for its mark
PREV_SESSION = "2026-01-05"
AS_OF = "2026-01-06"
START = "2025-01-02"
SHARES = 100
PRICE = 10.0
CASH = 42.5
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


def _frame(rows, column="Close"):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    series = pd.Series([float(v) for _, v in items], index=idx)
    return pd.DataFrame({column: series}, index=idx)


def _seed_book(cache, symbol="AAA", price=PRICE):
    cache.store_bars(
        PROVIDER,
        symbol,
        _frame({PREV_SESSION: price, AS_OF: price}).assign(
            Open=price, High=price, Low=price, Volume=1_000_000.0
        )[["Open", "High", "Low", "Close", "Volume"]],
        adjustflag="raw",
    )
    cache.store_adjust_factors(symbol, _frame({PREV_SESSION: 1.0, AS_OF: 1.0}, "factor_hfq"))


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


def _strategy_file(workspace):
    path = os.path.join(str(workspace["tmp_path"]), "strat.json")
    workspace["storage"]._save_json(path, {"template_name": "dummy", "params": {}})
    return path


def _run_stage1(workspace, monkeypatch, **kwargs):
    """One REAL `run_health_check`; returns `(argv, run_dir)`."""
    _seed_book(DuckDBCache(db_path=str(workspace["dirs"]["cache"])))
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": SHARES}, "previous_post_holdings"),
    )
    captured = _patch_subprocess(monkeypatch)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    result = svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        start_date=START,
        data_provider=PROVIDER,
        refresh_data=False,
        cash=CASH,
        **kwargs,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1, captured
    return captured[0], svc


# --------------------------------------------------------------------------- #
# 1. Not supplied -> byte-identical argv (Stage 1)
# --------------------------------------------------------------------------- #
def test_stage1_argv_is_unchanged_when_the_flag_is_not_supplied(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch)

    assert FLAG not in argv, "an unsupplied value must not add a token"
    run_dir = _flag(argv, "--output-dir")
    st = workspace["storage"]
    # The FULL token list, in order: this is the whole "byte-identical to the old argv"
    # claim. Anything appended, reordered or normalised shows up here.
    assert argv == [
        st.python_executable,
        st.stage1_script,
        "--strategy-file",
        _flag(argv, "--strategy-file"),
        "--universe-file",
        os.path.join(run_dir, "resolved_universe.txt"),
        "--portfolio-value",
        "100000.0",
        "--output-dir",
        run_dir,
        "--account-state-file",
        os.path.join(run_dir, "account_state.json"),
        "--data-provider",
        PROVIDER,
        "--nav-breakdown-file",
        os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME),
        "--as-of-date",
        AS_OF,
        "--start-date",
        START,
        "--cache-dir",
        st.cache_dir,
        "--enable-intraday-5m",
        "--allow-intraday",
        "--no-refresh-data",
    ]
    # The `--strategy-file` value is the resolved strategy path, not the raw argument.
    assert os.path.basename(_flag(argv, "--strategy-file")) == "strat.json"


# --------------------------------------------------------------------------- #
# 2. Supplied -> the flag is appended verbatim, and only then
# --------------------------------------------------------------------------- #
def test_stage1_forwards_the_flag_verbatim(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch, intraday_publish_after="21:30")

    assert _flag(argv, FLAG) == "21:30"
    # Appended at the END, so every pre-existing token keeps its position (the reason the
    # "not supplied" case above can stay byte-identical).
    assert argv[-2:] == [FLAG, "21:30"]


def test_stage1_strips_surrounding_whitespace(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch, intraday_publish_after=" 09:05 ")

    assert _flag(argv, FLAG) == "09:05"


def test_blank_value_is_treated_as_not_supplied(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch, intraday_publish_after="   ")

    assert FLAG not in argv


# --------------------------------------------------------------------------- #
# 3. Stage 2: same contract on the deploy argv
# --------------------------------------------------------------------------- #
def _run_stage2(workspace, monkeypatch, svc, run_dir, **kwargs):
    captured = _patch_subprocess(monkeypatch)
    result = svc.run_live_deploy(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        start_date=START,
        portfolio_value=100_000.0,
        current_holdings={"AAA": SHARES},
        cash=CASH,
        data_provider=PROVIDER,
        **kwargs,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1, captured
    assert _flag(captured[0], "--output-dir") == run_dir, (
        "Stage 2 must reuse the Stage-1 round directory"
    )
    return captured[0]


def test_stage2_argv_is_unchanged_when_the_flag_is_not_supplied(workspace, monkeypatch):
    stage1_argv, svc = _run_stage1(workspace, monkeypatch)
    run_dir = _flag(stage1_argv, "--output-dir")
    argv = _run_stage2(workspace, monkeypatch, svc, run_dir)

    assert FLAG not in argv
    st = workspace["storage"]
    assert argv == [
        st.python_executable,
        st.stage2_script,
        "--strategy-file",
        _flag(argv, "--strategy-file"),
        "--universe-file",
        os.path.join(run_dir, "resolved_universe.txt"),
        "--portfolio-value",
        f"{SHARES * PRICE + CASH}",
        "--current-holdings-file",
        os.path.join(run_dir, "holdings.json"),
        "--output-dir",
        run_dir,
        "--account-state-file",
        os.path.join(run_dir, "account_state.json"),
        "--data-provider",
        PROVIDER,
        "--as-of-date",
        AS_OF,
        "--start-date",
        START,
        # NOTE: `--health-report-file` is deliberately absent here -- the mocked child never
        # writes `stage1_health_report.json`, so this round legitimately deploys without a
        # gate report (recorded as `health_gate_applied=False`). That is pre-existing
        # behaviour and must stay unaffected by P1-1.
        "--cache-dir",
        st.cache_dir,
        "--enable-intraday-5m",
        "--guard-latency-bars",
        "2",
        "--allow-intraday",
    ]


def test_stage2_forwards_the_flag_verbatim(workspace, monkeypatch):
    stage1_argv, svc = _run_stage1(workspace, monkeypatch)
    run_dir = _flag(stage1_argv, "--output-dir")
    argv = _run_stage2(
        workspace, monkeypatch, svc, run_dir, intraday_publish_after="20:00"
    )

    assert _flag(argv, FLAG) == "20:00"
    assert argv[-2:] == [FLAG, "20:00"]


# --------------------------------------------------------------------------- #
# 4. Route: malformed values are HTTP 400, on BOTH stage endpoints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["25:00", "abc", "9:30", "20:60", "20:00:00"])
@pytest.mark.parametrize("route", ["/api/run-health", "/api/run-deploy"])
def test_route_rejects_malformed_values_with_400(workspace, bad, route):
    app = server.create_app(workspace["storage"])
    client = TestClient(app)

    body = {
        "strategy_file": _strategy_file(workspace),
        "custom_symbols": ["AAA"],
        "as_of_date": AS_OF,
        "intraday_publish_after": bad,
    }
    response = client.post(route, json=body)

    assert response.status_code == 400, response.text
    assert FLAG in response.json()["detail"]
    # Refused BEFORE the service ran: no run directory was ever allocated.
    for dirpath, _dirnames, filenames in os.walk(str(workspace["dirs"]["runs"])):
        assert "summary.json" not in filenames, os.path.join(dirpath, "summary.json")


def test_route_leaves_an_omitted_value_alone(workspace, monkeypatch):
    """The default must stay "not supplied": the model field is `None`, not 20:00."""
    from models import RunDeployRequest, RunHealthRequest

    assert RunHealthRequest(strategy_file="s.json").intraday_publish_after is None
    assert RunDeployRequest(strategy_file="s.json").intraday_publish_after is None

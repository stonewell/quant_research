"""P1-3 (2026-11-12): `cash_as_of` needs a SOURCE and an ADOPTION TIME, and a cash leg
identical to the previous round's residual must WARN (never refuse).

THE DEFECT THIS CLOSES
----------------------
`cash_as_of` (LV1-TIMEBASE) says WHICH session the cash leg belongs to, but nothing said
WHERE that stamp came from -- an operator's same-session entry and a residual carried
forward from a previous archive looked identical on disk -- and nothing said WHEN it was
adopted, so "the operator typed this at 09:12" was unreconstructable after the round.

THE CONTRACT PINNED HERE
------------------------
* `cash_as_of_source` -- exactly one of `operator_form` / `operator_override` /
  `archive_residual` / `missing`, written to BOTH `nav_breakdown.json` (Stage-1 gate
  snapshot) and `nav_breakdown.stage2.json` (Stage 2's own mark), plus `summary.json`;
* `cash_as_of_input_at` -- ISO-8601 adoption time, same three places;
* `cash_as_of_warning = "identical_to_previous_round_residual"` -- appended ONLY when this
  round's cash is numerically equal to the previous archived round's residual. It is a
  WARNING: the deploy and the gate proceed exactly as before, and nothing about the `cash`
  semantics or the children's 20:00 default changes;
* both fields are ADDITIVE: an archive written before this change still reads fine, and its
  absent keys stay absent (never a fabricated timestamp).

100% OFFLINE: throwaway `StorageManager` + a file-backed DuckDB cache under `tmp_path`,
`subprocess.run` monkeypatched. No network, nothing written under the real `data/`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pandas as pd
import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution_service  # noqa: E402
import storage  # noqa: E402
from common.duckdb_cache import DuckDBCache  # noqa: E402

PROVIDER = "marketdb"
PREV_SESSION = "2026-01-05"
AS_OF = "2026-01-06"
SHARES = 100
PRICE = 10.0
PREV_CASH = 42.5
RECORDED_AT_KEY = "cash_as_of_input_at"
SOURCE_KEY = "cash_as_of_source"
WARNING_KEY = "cash_as_of_warning"
WARNING_VALUE = "identical_to_previous_round_residual"


# --------------------------------------------------------------------------- #
# fixtures / helpers
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


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _seed_previous_round(workspace, cash=PREV_CASH, date=PREV_SESSION):
    """One archived round whose MEASURED residual is the number a rerun can inherit.

    Written straight to `summary.json` (the only thing `_cash_residual_from_summary` reads)
    at `<archive>/<strategy>/<universe>/<date>/`, which is exactly the layout
    `_archived_cash_for` walks -- no run needs to be executed to stage a prior round.
    """
    st = workspace["storage"]
    strat_path = _strategy_file(workspace)
    strat_key, univ_key, _sn, _un = st.resolve_keys(
        strategy_file=strat_path, custom_symbols=["AAA"]
    )
    run_dir = st.get_run_dir(strat_key, univ_key, date, prefer_existing=False)
    os.makedirs(run_dir, exist_ok=True)
    st._save_json(
        os.path.join(run_dir, "summary.json"),
        {
            "date": date,
            "timestamp": f"{date}T15:00:00+00:00",
            "strategy_key": strat_key,
            "universe_key": univ_key,
            "cash": float(cash),
            "cash_source": "explicit",
            "cash_as_of": date,
        },
    )
    return run_dir


def _run_stage1(workspace, monkeypatch, **kwargs):
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
        data_provider=PROVIDER,
        refresh_data=False,
        **kwargs,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1, captured
    return captured[0], svc


def _gate_breakdown(run_dir):
    return _read_json(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME))


def _stage2_breakdown(run_dir):
    return _read_json(
        os.path.join(run_dir, execution_service.NAV_BREAKDOWN_STAGE2_FILENAME)
    )


# --------------------------------------------------------------------------- #
# 0. The enum is exactly what the archive claims it is
# --------------------------------------------------------------------------- #
def test_the_four_source_values_are_the_pinned_enum():
    assert (
        execution_service.CASH_AS_OF_SOURCE_OPERATOR_FORM,
        execution_service.CASH_AS_OF_SOURCE_OPERATOR_OVERRIDE,
        execution_service.CASH_AS_OF_SOURCE_ARCHIVE_RESIDUAL,
        execution_service.CASH_AS_OF_SOURCE_MISSING,
    ) == ("operator_form", "operator_override", "archive_residual", "missing")
    assert execution_service.CASH_AS_OF_WARNING_IDENTICAL == WARNING_VALUE


# --------------------------------------------------------------------------- #
# 1. operator_form -- the form's 现金余额 for THIS session
# --------------------------------------------------------------------------- #
def test_operator_form_source_is_recorded(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch, cash=77.0)
    run_dir = _flag(argv, "--output-dir")
    payload = _gate_breakdown(run_dir)

    assert payload["cash"] == pytest.approx(77.0)
    assert payload["cash_as_of"] == AS_OF
    assert payload[SOURCE_KEY] == "operator_form"
    # ISO-8601 adoption time, this run.
    recorded_at = payload[RECORDED_AT_KEY]
    assert isinstance(recorded_at, str) and "T" in recorded_at
    assert recorded_at >= f"{AS_OF}T"


# --------------------------------------------------------------------------- #
# 2. archive_residual -- previous round's measured residual, on ITS timebase
# --------------------------------------------------------------------------- #
def test_archive_residual_source_is_recorded(workspace, monkeypatch):
    _seed_previous_round(workspace)
    argv, _svc = _run_stage1(workspace, monkeypatch)  # no form cash -> archive residual
    run_dir = _flag(argv, "--output-dir")
    payload = _gate_breakdown(run_dir)

    assert payload["cash"] == pytest.approx(PREV_CASH)
    # The residual keeps the ARCHIVED round's session -- that is the whole LV1-TIMEBASE
    # contract, and the reason the snapshot withholds `nav_effective` below.
    assert payload["cash_as_of"] == PREV_SESSION
    assert payload[SOURCE_KEY] == "archive_residual"
    assert payload["nav_effective"] is None
    # Recycling the residual unchanged is exactly the identical-cash case.
    assert payload[WARNING_KEY] == WARNING_VALUE


# --------------------------------------------------------------------------- #
# 3. operator_override -- the operator pinned the NAV by hand
# --------------------------------------------------------------------------- #
def test_operator_override_source_is_recorded(workspace, monkeypatch):
    captured = _patch_subprocess(monkeypatch)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    result = svc.run_live_deploy(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        portfolio_value=100_000.0,
        current_holdings={"AAA": SHARES},
        # An empty cache cannot mark the book; the operator pinned the NAV instead.
        nav_override=True,
        data_provider=PROVIDER,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1
    run_dir = _flag(captured[0], "--output-dir")
    payload = _stage2_breakdown(run_dir)

    assert payload["nav_source"] == "override"
    assert payload["cash_as_of"] == AS_OF
    assert payload[SOURCE_KEY] == "operator_override"
    assert isinstance(payload[RECORDED_AT_KEY], str)


# --------------------------------------------------------------------------- #
# 4. missing -- no operator/archive cash input at all
# --------------------------------------------------------------------------- #
def test_missing_source_is_recorded(workspace):
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    payload = svc._nav_payload(
        nav=100_000.0,
        nav_source="fallback_input",
        nav_computed=None,
        equity=None,
        cash=None,
        cash_source=None,
        as_of=AS_OF,
        cash_as_of=None,
    )
    assert payload[SOURCE_KEY] == "missing"
    assert payload[RECORDED_AT_KEY] is None

    run_dir = os.path.join(str(workspace["tmp_path"]), "missing_run")
    os.makedirs(run_dir, exist_ok=True)
    written = _read_json(svc._write_nav_breakdown(run_dir, payload))
    assert written[SOURCE_KEY] == "missing"
    assert SOURCE_KEY in written and RECORDED_AT_KEY in written


# --------------------------------------------------------------------------- #
# 5. Identical-to-previous-round warning: fires, and does not fire
# --------------------------------------------------------------------------- #
def test_identical_cash_appends_the_warning(workspace, monkeypatch):
    _seed_previous_round(workspace, cash=PREV_CASH)
    argv, _svc = _run_stage1(workspace, monkeypatch, cash=PREV_CASH)
    payload = _gate_breakdown(_flag(argv, "--output-dir"))

    assert payload[WARNING_KEY] == WARNING_VALUE
    # WARNING ONLY: the round still produced a usable same-instant NAV.
    assert payload["nav_effective"] == pytest.approx(SHARES * PRICE + PREV_CASH)


def test_different_cash_does_not_append_the_warning(workspace, monkeypatch):
    _seed_previous_round(workspace, cash=PREV_CASH)
    argv, _svc = _run_stage1(workspace, monkeypatch, cash=PREV_CASH + 7.5)
    payload = _gate_breakdown(_flag(argv, "--output-dir"))

    assert WARNING_KEY not in payload
    assert payload["cash"] == pytest.approx(PREV_CASH + 7.5)


def test_no_previous_round_means_no_warning(workspace, monkeypatch):
    argv, _svc = _run_stage1(workspace, monkeypatch, cash=PREV_CASH)
    payload = _gate_breakdown(_flag(argv, "--output-dir"))

    assert WARNING_KEY not in payload


# --------------------------------------------------------------------------- #
# 6. Both breakdown files (and summary.json) carry the provenance pair
# --------------------------------------------------------------------------- #
def test_both_nav_breakdown_files_carry_the_provenance_pair(workspace, monkeypatch):
    argv, svc = _run_stage1(workspace, monkeypatch, cash=PREV_CASH)
    run_dir = _flag(argv, "--output-dir")

    _patch_subprocess(monkeypatch)
    result = svc.run_live_deploy(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        portfolio_value=100_000.0,
        current_holdings={"AAA": SHARES},
        cash=PREV_CASH,
        data_provider=PROVIDER,
    )
    assert result["status"] == "success", result

    gate = _gate_breakdown(run_dir)
    stage2 = _stage2_breakdown(run_dir)
    for payload in (gate, stage2):
        assert SOURCE_KEY in payload
        assert RECORDED_AT_KEY in payload
        assert payload[SOURCE_KEY] in (
            "operator_form",
            "operator_override",
            "archive_residual",
            "missing",
        )
    assert gate[SOURCE_KEY] == "operator_form"
    assert stage2[SOURCE_KEY] == "operator_form"

    summary = _read_json(os.path.join(run_dir, "summary.json"))
    assert summary[SOURCE_KEY] == "operator_form"
    assert summary[f"{SOURCE_KEY}_stage2"] == "operator_form"
    assert RECORDED_AT_KEY in summary and f"{RECORDED_AT_KEY}_stage2" in summary
    # Backward compatibility: the pre-existing field family is untouched.
    assert summary["cash_as_of"] == AS_OF
    assert summary["cash_as_of_stage2"] == AS_OF


# --------------------------------------------------------------------------- #
# 7. The HTTP response itself carries the pair (`response_model` filters undeclared keys,
#    which is how the UI would silently lose the "上次录入" line)
# --------------------------------------------------------------------------- #
def test_http_response_exposes_the_cash_provenance_pair(workspace, monkeypatch):
    import server  # local import: keeps the module-level import list minimal
    from fastapi.testclient import TestClient

    _seed_book(DuckDBCache(db_path=str(workspace["dirs"]["cache"])))
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": SHARES}, "previous_post_holdings"),
    )
    _patch_subprocess(monkeypatch)
    client = TestClient(server.create_app(workspace["storage"]))
    strat = _strategy_file(workspace)

    health = client.post(
        "/api/run-health",
        json={
            "strategy_file": strat,
            "custom_symbols": ["AAA"],
            "as_of_date": AS_OF,
            "data_provider": PROVIDER,
            "refresh_data": False,
            "cash": PREV_CASH,
        },
    )
    assert health.status_code == 200, health.text

    deploy = client.post(
        "/api/run-deploy",
        json={
            "strategy_file": strat,
            "custom_symbols": ["AAA"],
            "as_of_date": AS_OF,
            "data_provider": PROVIDER,
            "portfolio_value": 100_000.0,
            "current_holdings": {"AAA": SHARES},
            "cash": PREV_CASH,
        },
    )
    assert deploy.status_code == 200, deploy.text
    data = deploy.json()
    assert data[SOURCE_KEY] == "operator_form"
    assert isinstance(data[RECORDED_AT_KEY], str) and "T" in data[RECORDED_AT_KEY]


def test_old_archives_without_the_pair_still_read_fine(workspace):
    """Additive only: a round written before P1-3 keeps its exact old shape."""
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    legacy = {
        "as_of": AS_OF,
        "nav_effective": 1000.0,
        "nav_source": "computed",
        "equity": 1000.0,
        "cash": 0.0,
        "cash_source": "explicit",
        "cash_as_of": AS_OF,
        "per_symbol": {},
    }
    # The summary projection must not invent a provenance for a legacy payload.
    assert svc._nav_summary_fields(legacy)[SOURCE_KEY] is None
    assert svc._nav_summary_fields(legacy)[RECORDED_AT_KEY] is None

"""D8 (2026-11-11): Stage 2 must not overwrite the Stage-1 GATE valuation snapshot.

FORENSIC CONTEXT (2026-11-09/204716, reproduced offline here)
-------------------------------------------------------------
`nav_breakdown.json` used to be written TWICE per round to the SAME path:

1. `ExecutionService.run_health_check` pre-stages it BEFORE the gate child launches
   (LV11; cash = the previous round's archived `summary.json::cash`);
2. `ExecutionService.run_live_deploy` overwrites it with Stage 2's mark
   (cash = the operator's form value).

The gate reads (1) and the archive kept (2).  Whenever the two cash legs differ the
archived file is NOT the number the breaker decided on, so "the gate saw X" becomes
unprovable after the fact:

    gate     nav_effective = 197,899.90  (equity 172,303.00 + archive cash 25,596.90)
    archived nav_effective = 197,899.60  (equity 172,303.00 + form    cash 25,596.60)

Side effect: 11-06's 199,480.90 never entered `nav_history_10d` because the gate's
number was the one that got overwritten before the archive was read back.

WHAT THE CONTRACT NOW IS (pinned here)
--------------------------------------
* `nav_breakdown.json` -- byte-identical before/after Stage 2.  It IS the gate input.
* `nav_breakdown.stage2.json` -- Stage 2's own deploy-time mark.
* `summary.json` -- bare `nav_effective` / `cash` / `nav_source` / ... carry the GATE
  snapshot (plus `cash_as_of` and `nav_gate_snapshot_source`); Stage 2's numbers live
  in `nav_effective_stage2` / `cash_stage2` / `nav_source_stage2` / ... and
  `nav_breakdown_stage2_file`.  The two are never merged or averaged.
* A Stage-2-only round (fresh instance dir, no Stage 1 in this process) has no gate
  snapshot to preserve: the legacy filename is still populated and the summary says so
  (`nav_gate_snapshot_source == "stage2_nav_no_stage1_snapshot"`), never pretending a
  gate decided on those numbers.

The API guard (`POST /api/account-state`) is pinned here too: `docs/lv7_prior_state_contract.md`
§5.4 forbids re-stamping version metadata, and that endpoint is the only generic save a
caller could attempt it through.

100% OFFLINE: throwaway `StorageManager` + a file-backed DuckDB cache under `tmp_path`;
`subprocess.run` is always monkeypatched, so no child process and no network.
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
import server  # noqa: E402
import storage  # noqa: E402
from common.duckdb_cache import DuckDBCache  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROVIDER = "marketdb"  # the engine reads the cache for its mark; it is a THROWAWAY cache
PREV_SESSION = "2026-01-05"
AS_OF = "2026-01-06"

GATE_CASH = 42.5  # what Stage 1's pre-staged snapshot is marked with
DEPLOY_CASH = 7.0  # what the operator's form says at deploy time -- deliberately different
SHARES = 100
PRICE = 10.0
GATE_NAV = SHARES * PRICE + GATE_CASH  # 1,042.50
DEPLOY_NAV = SHARES * PRICE + DEPLOY_CASH  # 1,007.00


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
    """`AAA`: 10.00 on both sessions, factor 1.0 -> 100 shares mark to exactly 1,000."""
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


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _run_stage1(workspace, monkeypatch, cash=GATE_CASH):
    """Run one REAL `run_health_check`; return `(run_dir, captured argv)`."""
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
        cash=cash,
    )
    assert result["status"] == "success", result
    assert len(captured) == 1, captured
    run_dir = _flag(captured[0], "--output-dir")
    # The gate reads exactly the file that was just staged.
    assert _flag(captured[0], "--nav-breakdown-file") == os.path.join(
        run_dir, execution_service.NAV_BREAKDOWN_FILENAME
    )
    return run_dir, svc


# --------------------------------------------------------------------------- #
# 1. Stage 2 keeps Stage 1's gate snapshot and publishes both marks
# --------------------------------------------------------------------------- #
def test_stage2_never_overwrites_the_gate_snapshot_and_keeps_both_marks(
    workspace, monkeypatch
):
    run_dir, svc = _run_stage1(workspace, monkeypatch)

    gate_path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME)
    gate_bytes_before = _read_bytes(gate_path)
    gate_payload = _read_json(gate_path)
    assert gate_payload["nav_effective"] == pytest.approx(GATE_NAV)
    assert gate_payload["cash"] == pytest.approx(GATE_CASH)
    assert gate_payload["cash_as_of"] == AS_OF
    assert gate_payload["nav_source"] == "computed"

    deploy_captured = _patch_subprocess(monkeypatch)
    result = svc.run_live_deploy(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        portfolio_value=100_000.0,
        current_holdings={"AAA": SHARES},
        cash=DEPLOY_CASH,
        data_provider=PROVIDER,
    )
    assert result["status"] == "success", result
    assert len(deploy_captured) == 1
    assert _flag(deploy_captured[0], "--output-dir") == run_dir, (
        "Stage 2 must reuse the Stage-1 round directory (the snapshot it must not "
        "overwrite lives there)"
    )
    # The child got Stage 2's mark, so the two numbers genuinely differ in this round.
    assert float(_flag(deploy_captured[0], "--portfolio-value")) == pytest.approx(DEPLOY_NAV)

    # (1) THE GATE SNAPSHOT IS BYTE-IDENTICAL.  This is the whole point of D8.
    assert _read_bytes(gate_path) == gate_bytes_before
    assert _read_json(gate_path)["nav_effective"] == pytest.approx(GATE_NAV)

    # (2) Stage 2's own mark went to its OWN file.
    stage2_path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_STAGE2_FILENAME)
    assert os.path.exists(stage2_path)
    stage2_payload = _read_json(stage2_path)
    assert stage2_payload["nav_effective"] == pytest.approx(DEPLOY_NAV)
    assert stage2_payload["cash"] == pytest.approx(DEPLOY_CASH)
    assert stage2_payload["cash_as_of"] == AS_OF

    # (3) The summary carries BOTH, side by side, with the gate in the bare names.
    summary = _read_json(os.path.join(run_dir, "summary.json"))
    assert summary["nav_effective"] == pytest.approx(GATE_NAV), (
        "summary::nav_effective must be the number the breaker decided on"
    )
    assert summary["cash"] == pytest.approx(GATE_CASH)
    assert summary["nav_source"] == "computed"
    assert summary["cash_as_of"] == AS_OF
    assert summary["cash_source"] == "explicit"
    assert summary["nav_breakdown_file"] == execution_service.NAV_BREAKDOWN_FILENAME
    assert summary["nav_gate_snapshot_source"] == "stage1_gate_snapshot"

    assert summary["nav_effective_stage2"] == pytest.approx(DEPLOY_NAV)
    assert summary["cash_stage2"] == pytest.approx(DEPLOY_CASH)
    assert summary["nav_source_stage2"] == "computed"
    assert summary["cash_as_of_stage2"] == AS_OF
    assert (
        summary["nav_breakdown_stage2_file"]
        == execution_service.NAV_BREAKDOWN_STAGE2_FILENAME
    )
    # Never merged / averaged: the two families are independently reconstructable.
    assert summary["nav_effective"] != summary["nav_effective_stage2"]

    # (4) The API response keeps the DEPLOY numbers under the historical names (the UI
    # renders "what this deploy used") and exposes the gate block explicitly.
    assert result["nav_effective"] == pytest.approx(DEPLOY_NAV)
    assert result["cash"] == pytest.approx(DEPLOY_CASH)
    assert result["nav_gate_snapshot"]["source"] == "stage1_gate_snapshot"
    assert result["nav_gate_snapshot"]["nav_effective"] == pytest.approx(GATE_NAV)
    assert result["nav_gate_snapshot"]["cash"] == pytest.approx(GATE_CASH)


# --------------------------------------------------------------------------- #
# 2. Stage-2-only round: nothing to preserve, legacy path still populated + declared
# --------------------------------------------------------------------------- #
def test_stage2_without_a_gate_snapshot_still_populates_the_legacy_path(
    workspace, monkeypatch
):
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

    gate_path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME)
    stage2_path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_STAGE2_FILENAME)
    assert os.path.exists(gate_path), (
        "a Stage-2-only round must not leave the historical filename absent: readers "
        "that only know `nav_breakdown.json` keep working"
    )
    assert os.path.exists(stage2_path)
    gate_payload = _read_json(gate_path)
    stage2_payload = _read_json(stage2_path)
    assert gate_payload["nav_effective"] == pytest.approx(100_000.0)
    assert stage2_payload["nav_effective"] == pytest.approx(100_000.0)

    summary = _read_json(os.path.join(run_dir, "summary.json"))
    assert summary["nav_gate_snapshot_source"] == "stage2_nav_no_stage1_snapshot"
    assert summary["nav_effective"] == pytest.approx(100_000.0)
    assert summary["nav_effective_stage2"] == pytest.approx(100_000.0)
    assert summary["nav_breakdown_file"] == execution_service.NAV_BREAKDOWN_FILENAME
    assert (
        summary["nav_breakdown_stage2_file"]
        == execution_service.NAV_BREAKDOWN_STAGE2_FILENAME
    )
    assert result["nav_gate_snapshot"]["source"] == "stage2_nav_no_stage1_snapshot"


# --------------------------------------------------------------------------- #
# 3. POST /api/account-state refuses to re-stamp version metadata (§5.4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field",
    ["base_version", "strategy_version", "strategy_version_info"],
)
def test_api_account_state_refuses_version_restamping(workspace, field):
    app = server.create_app(workspace["storage"])
    client = TestClient(app)

    response = client.post(
        "/api/account-state",
        json={"as_of_date": AS_OF, field: "2.0.0" if field != "strategy_version_info" else {"base_version": "2.0.0"}},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "禁止重新盖章" in detail
    assert "lv7_prior_state_contract.md §5.4" in detail
    assert field in detail

    # Refused BEFORE any write: no archive copy was produced for that session.
    for dirpath, _dirnames, filenames in os.walk(str(workspace["dirs"]["runs"])):
        assert "account_state.json" not in filenames, os.path.join(dirpath, "account_state.json")


def test_api_account_state_still_saves_a_normal_body(workspace):
    app = server.create_app(workspace["storage"])
    client = TestClient(app)

    response = client.post(
        "/api/account-state",
        json={"as_of_date": AS_OF, "current_nav": 123456.78, "circuit_breaker_tier": "NORMAL"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    saved = workspace["storage"].get_account_state(date_str=AS_OF)
    assert saved is not None
    assert saved["current_nav"] == pytest.approx(123456.78)

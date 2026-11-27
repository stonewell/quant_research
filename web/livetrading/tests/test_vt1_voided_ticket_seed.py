"""VT1: Voided ticket resolution and seed isolation tests.

Validates that:
1. Rule 10 discriminating probe: Toggling `live_trading_ticket.voided.json` directly flips
   Day 3 seed resolution from Day 2's planned post-book (4800 shares) back to Day 1's
   planned post-book (400 shares), skipping the voided run completely.
2. Backfill priority: `void` strictly overrides `complete: true` sidecars, returning
   `refuse / ticket_voided` instead of `skip / post_holdings_meta_already_complete`.
3. Multi-netting parity: `netted_trading_ticket.voided.json` in matrix runs skips the voided
   matrix run in `get_previous_multi_netting_post_holdings`.
4. Fallback behaviour: `_display_book_for_run_dir` falls through to `pre_trade_fallback`
   for voided runs instead of surfacing `post_trade_book`.
5. Non-voided runs remain 100% byte-identical and unaffected.

100% offline: synthetic tmp_path directories only, zero network, zero real market data.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import storage  # noqa: E402

STRATEGY_KEY = "chan_four_state_blend"
UNIVERSE_KEY = "custom_astock_202609_20_new"
POST = storage.POST_HOLDINGS_FILENAME
META = storage.POST_HOLDINGS_META_FILENAME


@pytest.fixture
def temp_storage(tmp_path):
    """Hermetic StorageManager using isolated temporary directories."""
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _make_run(
    archive_dir: str,
    date_str: str,
    files: Optional[Dict[str, Any]] = None,
    time_str: str = "150000",
) -> str:
    run_dir = os.path.join(archive_dir, STRATEGY_KEY, UNIVERSE_KEY, date_str, time_str)
    os.makedirs(run_dir, exist_ok=True)
    _write_json(
        os.path.join(run_dir, "summary.json"),
        {"date": date_str, "strategy_key": STRATEGY_KEY, "universe_key": UNIVERSE_KEY},
    )
    for fname, content in (files or {}).items():
        _write_json(os.path.join(run_dir, fname), content)
    return run_dir


def _complete_meta(as_of: str, n_symbols: int = 1) -> Dict[str, Any]:
    return {
        "as_of": as_of,
        "complete": True,
        "n_symbols": n_symbols,
        "generated_at": f"{as_of}T15:00:00+08:00",
        "source": "ticket",
    }


# ==============================================================================
# Rule 10 Discriminating Probe: 2-Day Fixture (Day 1: 400, Day 2: BUY +4400 -> 4800)
# ==============================================================================

def test_vt1_rule10_discriminating_probe_seed_resolution(temp_storage, tmp_path):
    """Rule 10 probe: toggling live_trading_ticket.voided.json flips Day 3 seed.

    Arm A (unvoided):
      - Day 3 get_previous_post_holdings returns Day 2's planned post-book (4800 shares).
      - Day 2 plan_post_holdings_meta_backfill returns 'skip / post_holdings_meta_already_complete'.
    Arm B (voided):
      - Day 3 get_previous_post_holdings skips Day 2 and returns Day 1's book (400 shares).
      - Day 2 plan_post_holdings_meta_backfill returns 'refuse / ticket_voided'.
    """
    archive = str(tmp_path / "archive")

    # Day 1: 2026-11-25 holding 400 shares of 601728.SH
    day1_dir = _make_run(
        archive,
        "2026-11-25",
        {
            POST: {"601728.SH": 400},
            META: _complete_meta("2026-11-25", n_symbols=1),
            "live_trading_ticket.json": {
                "as_of": "2026-11-25",
                "orders": [{"symbol": "601728.SH", "action": "SELL", "target_shares": 400}],
            },
        },
    )

    # Day 2: 2026-11-26 BUY +4400 -> post_holdings = 4800, complete sidecar
    day2_files = {
        POST: {"601728.SH": 4800},
        META: _complete_meta("2026-11-26", n_symbols=1),
        "holdings.json": {"601728.SH": 400},  # pre-trade snapshot
        "live_trading_ticket.json": {
            "as_of": "2026-11-26",
            "orders": [{"symbol": "601728.SH", "action": "BUY", "target_shares": 4800}],
        },
    }
    day2_dir = _make_run(archive, "2026-11-26", day2_files)

    # --------------------------------------------------------------------------
    # Arm A: WITHOUT .voided.json (baseline unvoided run)
    # --------------------------------------------------------------------------
    prev_holdings_a, prev_dir_a = temp_storage.get_previous_post_holdings(
        date_str="2026-11-27", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert prev_holdings_a == {"601728.SH": 4800}, "Arm A must resolve to Day 2 post-holdings (4800)"
    assert prev_dir_a == day2_dir

    backfill_plan_a = storage.plan_post_holdings_meta_backfill(day2_dir)
    assert backfill_plan_a["action"] == "skip"
    assert backfill_plan_a["reason"] == storage.POST_HOLDINGS_REASON_ALREADY

    # --------------------------------------------------------------------------
    # Arm B: WITH live_trading_ticket.voided.json (voided run)
    # --------------------------------------------------------------------------
    void_marker_path = os.path.join(day2_dir, "live_trading_ticket.voided.json")
    _write_json(
        void_marker_path,
        {
            "voided_at": "2026-11-26T18:20:23-08:00",
            "reason": "thrust_slot_reallocation_revoked",
            "ticket": "live_trading_ticket.json",
        },
    )

    prev_holdings_b, prev_dir_b = temp_storage.get_previous_post_holdings(
        date_str="2026-11-27", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert prev_holdings_b == {"601728.SH": 400}, (
        f"Arm B Rule 10 probe FAIL: expected Day 1 (400), got {prev_holdings_b}"
    )
    assert prev_dir_b == day1_dir, (
        f"Arm B provenance FAIL: expected Day 1 dir {day1_dir}, got {prev_dir_b}"
    )

    backfill_plan_b = storage.plan_post_holdings_meta_backfill(day2_dir)
    assert backfill_plan_b["action"] == "refuse"
    assert backfill_plan_b["reason"] == storage.POST_HOLDINGS_REASON_TICKET_VOIDED, (
        f"Arm B backfill FAIL: expected {storage.POST_HOLDINGS_REASON_TICKET_VOIDED}, "
        f"got {backfill_plan_b['reason']}"
    )


# ==============================================================================
# Multi-Netted Voided Ticket Test
# ==============================================================================

def test_vt1_multi_netted_voided_ticket_skipped(temp_storage, tmp_path):
    """Multi-strategy netted run with netted_trading_ticket.voided.json is skipped."""
    archive = str(tmp_path / "archive")
    matrix_id = "matrix_global"

    # Day 1: 2026-11-25 matrix run
    day1_matrix_dir = os.path.join(archive, "2026-11-25", f"multi_netted_{matrix_id}")
    os.makedirs(day1_matrix_dir, exist_ok=True)
    _write_json(os.path.join(day1_matrix_dir, POST), {"601728.SH": 400})
    _write_json(os.path.join(day1_matrix_dir, META), _complete_meta("2026-11-25"))
    _write_json(
        os.path.join(day1_matrix_dir, "netted_trading_ticket.json"),
        {"orders": [{"symbol": "601728.SH", "target_shares": 400}]},
    )

    # Day 2: 2026-11-26 matrix run (voided)
    day2_matrix_dir = os.path.join(archive, "2026-11-26", f"multi_netted_{matrix_id}")
    os.makedirs(day2_matrix_dir, exist_ok=True)
    _write_json(os.path.join(day2_matrix_dir, POST), {"601728.SH": 4800})
    _write_json(os.path.join(day2_matrix_dir, META), _complete_meta("2026-11-26"))
    _write_json(
        os.path.join(day2_matrix_dir, "netted_trading_ticket.json"),
        {"orders": [{"symbol": "601728.SH", "target_shares": 4800}]},
    )
    _write_json(
        os.path.join(day2_matrix_dir, "netted_trading_ticket.voided.json"),
        {"voided_at": "2026-11-26T18:20:23-08:00", "reason": "operator_void"},
    )

    plan, run_dir = temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-11-27", matrix_id=matrix_id
    )
    assert plan == {"601728.SH": 400}, f"Expected Day 1 matrix post-holdings (400), got {plan}"
    assert run_dir == day1_matrix_dir, f"Expected Day 1 dir {day1_matrix_dir}, got {run_dir}"

    # Also verify plan_post_holdings_meta_backfill on netted ticket
    backfill = storage.plan_post_holdings_meta_backfill(
        day2_matrix_dir, ticket_names=("netted_trading_ticket",)
    )
    assert backfill["action"] == "refuse"
    assert backfill["reason"] == storage.POST_HOLDINGS_REASON_TICKET_VOIDED


# ==============================================================================
# extract_post_run_holdings and _display_book_for_run_dir on Voided Run
# ==============================================================================

def test_vt1_display_and_extract_fallback_on_voided_run(temp_storage, tmp_path):
    """A voided run falls through to pre_trade_fallback instead of surfacing voided post-trade book."""
    archive = str(tmp_path / "archive")
    run_dir = _make_run(
        archive,
        "2026-11-26",
        {
            POST: {"601728.SH": 4800},
            META: _complete_meta("2026-11-26"),
            "holdings.json": {"601728.SH": 400},
            "live_trading_ticket.json": {
                "orders": [{"symbol": "601728.SH", "action": "BUY", "target_shares": 4800}]
            },
            "live_trading_ticket.voided.json": {
                "voided_at": "2026-11-26T18:20:23-08:00",
                "reason": "voided",
            },
        },
    )

    # 1. _display_book_for_run_dir
    display_book, source = temp_storage._display_book_for_run_dir(run_dir)
    assert display_book == {"601728.SH": 400}, (
        f"Expected pre-trade fallback snapshot (400), got {display_book}"
    )
    assert source == "pre_trade_fallback", f"Expected 'pre_trade_fallback', got {source}"

    # 2. extract_post_run_holdings
    extracted = temp_storage.extract_post_run_holdings(run_dir)
    assert extracted == {"601728.SH": 400}, (
        f"extract_post_run_holdings must return pre-trade snapshot, got {extracted}"
    )

    # 3. _planned_book_from_run_dir directly
    assert temp_storage._planned_book_from_run_dir(run_dir) is None


# ==============================================================================
# Regression: Non-voided Runs Behave Identically
# ==============================================================================

def test_vt1_non_voided_runs_regression(temp_storage, tmp_path):
    """Non-voided runs remain 100% byte-identical to pre-VT1 behaviour."""
    archive = str(tmp_path / "archive")

    # Run with complete sidecar
    run1 = _make_run(
        archive,
        "2026-11-20",
        {
            POST: {"600584.SH": 1000},
            META: _complete_meta("2026-11-20"),
            "holdings.json": {"600584.SH": 500},
        },
    )
    assert temp_storage._planned_book_from_run_dir(run1) == {"600584.SH": 1000}
    book, src = temp_storage._display_book_for_run_dir(run1)
    assert book == {"600584.SH": 1000}
    assert src == "post_trade_book"
    assert temp_storage.extract_post_run_holdings(run1) == {"600584.SH": 1000}

    # Run without sidecar (legacy) re-derived from ticket
    run2 = _make_run(
        archive,
        "2026-11-21",
        {
            "live_trading_ticket.json": {
                "orders": [{"symbol": "600584.SH", "target_shares": 1200}]
            }
        },
    )
    assert temp_storage._planned_book_from_run_dir(run2) == {"600584.SH": 1200}

    # Confirmed empty book (all-cash)
    run3 = _make_run(
        archive,
        "2026-11-22",
        {
            POST: {},
            META: _complete_meta("2026-11-22", n_symbols=0),
        },
    )
    assert temp_storage._planned_book_from_run_dir(run3) == {}
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-11-23", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert holdings == {}
    assert where == run3

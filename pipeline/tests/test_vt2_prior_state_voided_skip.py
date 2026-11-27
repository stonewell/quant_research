"""Offline test suite for `VT2` -- voided ticket skips prior_state candidate.

THE DEFECT UNDER TEST (VT2):
When a trading ticket is voided on Day 2 (e.g., operator canceled execution, order
aborted, market halt, or mis-click void), the resulting Day 2 account_state /
post_holdings must NOT become Day 3's prior_state. Otherwise:
1. Phantom new buy `entry_price` leakage: An aborted buy of `300394.SZ` on Day 2
   would leak its `entry_price` into Day 3, causing false stop-loss / holding_bars
   calculations.
2. False `COOLDOWN`: An aborted sell/liquidation on Day 2 would force Day 3 into
   a 2-day `COOLDOWN` even though the position was never actually liquidated.

VT2 REQUIREMENTS PINNED HERE:
1. Rule 10 discriminating probe #1 (phantom buy `entry_price`):
   Arm A (unvoided): Day 3 selects Day 2 (carries 300394.SZ entry_price & HELD_GESTATION).
   Arm B (Day 2 has `live_trading_ticket.voided.json`): Day 3 skips Day 2 with
   `SKIP_TICKET_VOIDED` ("ticket_voided") and selects Day 1 (only 601728.SH).
2. Rule 10 discriminating probe #2 (false COOLDOWN):
   Day 2 entered COOLDOWN and voided -> Day 3 skips Day 2 and preserves Day 1 HELD_ACTIVE.
3. Same-day sibling instance fallback:
   Day 2 has instance 120000 (valid SUCCESS) and 145832 (voided SUCCESS) -> skips 145832,
   selects same-day 120000 (does not prematurely jump to older date).
4. Multi-strategy netted void marker:
   `netted_trading_ticket.voided.json` triggers `SKIP_TICKET_VOIDED` identically.
5. Unvoided byte-identical regression + flat date directory compatibility:
   Clean runs without voided marker remain 100% unaffected; flat `<session_root>/<date>/`
   directories carrying `.voided.json` are also skipped with `instance="flat"`.

100% offline: `tmp_path` synthetic directories only, zero network, zero real market data.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from scripts.live_preflight import (
    PRIOR_STATE_LOG_PREFIX,
    SKIP_STATUS_NOT_SUCCESS,
    SKIP_TICKET_VOIDED,
    VOIDED_TICKET_MARKERS,
    _run_dir_has_voided_ticket,
    apply_prior_state_selection,
    list_prior_state_candidates,
    select_prior_state,
)

_STRAT = "chan_four_state_blend"
_UNIV = "custom_astock_202609_20"
_ARCHIVE = "runs"


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _snapshot(
    root: Any,
    run_date: str,
    instance: Optional[str] = None,
    *,
    as_of: Optional[str] = None,
    nav: float = 100000.0,
    base_version: str = "2.0.0",
    strategy_version: str = "1.2.2",
    deploy_status: Optional[str] = "SUCCESS",
    health_status: Optional[str] = "SUCCESS",
    entry_prices: Optional[Dict[str, float]] = None,
    lifecycle_states: Optional[Dict[str, Any]] = None,
    extra_state: Optional[Dict[str, Any]] = None,
    voided_marker: Optional[str] = None,
) -> str:
    """Create `<root>/runs/<strat>/<univ>/<date>[/<instance>]/` with full state."""
    session_root = os.path.join(str(root), _ARCHIVE, _STRAT, _UNIV)
    run_dir = (
        os.path.join(session_root, run_date, instance)
        if instance
        else os.path.join(session_root, run_date)
    )
    os.makedirs(run_dir, exist_ok=True)

    account_state: Dict[str, Any] = {
        "as_of_date": as_of or run_date,
        "current_nav": nav,
        "base_version": base_version,
        "strategy_version": strategy_version,
        "nav_history_10d": [nav] * 8,
    }
    if entry_prices is not None:
        account_state["entry_prices"] = entry_prices
    if lifecycle_states is not None:
        account_state["lifecycle_states"] = lifecycle_states
    if extra_state:
        account_state.update(extra_state)

    _write_json(os.path.join(run_dir, "account_state.json"), account_state)

    summary: Dict[str, Any] = {
        "date": run_date,
        "timestamp": f"{run_date}T{instance or '000000'}",
    }
    if health_status is not None:
        summary["health_status"] = health_status
    if deploy_status is not None:
        summary["deploy_status"] = deploy_status
    _write_json(os.path.join(run_dir, "summary.json"), summary)

    if voided_marker:
        void_payload = {
            "voided_at": f"{run_date}T15:00:00+08:00",
            "reason": "operator_canceled_live_trade",
            "voided_by": "test_suite",
        }
        _write_json(os.path.join(run_dir, voided_marker), void_payload)

    return run_dir


def _select(root: Any, as_of: str = "2026-11-09", expected: str = "2.0.0"):
    session_root = os.path.join(str(root), _ARCHIVE, _STRAT, _UNIV)
    return select_prior_state(session_root, as_of, expected_base_version=expected)


# ==============================================================================
# Helper Unit Tests: _run_dir_has_voided_ticket & constants
# ==============================================================================


def test_voided_ticket_markers_contract():
    """Ensure standard marker filenames match web/livetrading storage conventions."""
    assert "live_trading_ticket.voided.json" in VOIDED_TICKET_MARKERS
    assert "netted_trading_ticket.voided.json" in VOIDED_TICKET_MARKERS
    assert SKIP_TICKET_VOIDED == "ticket_voided"


def test_run_dir_has_voided_ticket_detection(tmp_path):
    """Detection helper returns True if any marker exists, False otherwise."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()

    assert _run_dir_has_voided_ticket(None) is False
    assert _run_dir_has_voided_ticket(str(tmp_path / "non_existent")) is False
    assert _run_dir_has_voided_ticket(str(run_dir)) is False

    # Arbitrary file does not trigger
    (run_dir / "live_trading_ticket.json").write_text("{}", encoding="utf-8")
    assert _run_dir_has_voided_ticket(str(run_dir)) is False

    # Marker 1: live_trading_ticket.voided.json
    void_file = run_dir / "live_trading_ticket.voided.json"
    void_file.write_text("{}", encoding="utf-8")
    assert _run_dir_has_voided_ticket(str(run_dir)) is True

    # Marker 2: netted_trading_ticket.voided.json
    void_file.unlink()
    assert _run_dir_has_voided_ticket(str(run_dir)) is False
    netted_void = run_dir / "netted_trading_ticket.voided.json"
    netted_void.write_text("{}", encoding="utf-8")
    assert _run_dir_has_voided_ticket(str(run_dir)) is True


# ==============================================================================
# 1. Rule 10 Discriminating Probe #1: Phantom Buy entry_price
# ==============================================================================


def test_rule10_discriminating_probe_phantom_buy_entry_price(tmp_path):
    """Rule 10 Probe 1: Day 1 holds 601728.SH, Day 2 buys 300394.SZ.

    Arm A (unvoided):
      - Day 3 selects Day 2.
      - 300394.SZ is in entry_prices (160.52) and lifecycle_states (HELD_GESTATION).

    Arm B (Day 2 voided):
      - Day 3 skips Day 2 with reason == SKIP_TICKET_VOIDED ('ticket_voided').
      - Day 3 selects Day 1.
      - 300394.SZ phantom buy is completely absent; only 601728.SH entry_price (6.91) is kept.
    """
    root_a = tmp_path / "arm_a"
    root_b = tmp_path / "arm_b"

    day1_entry_prices = {"601728.SH": 6.91}
    day1_lifecycle = {
        "601728.SH": {
            "state": "HELD_ACTIVE",
            "entry_price": 6.91,
            "held_bars": 3,
        }
    }

    day2_entry_prices = {"601728.SH": 6.91, "300394.SZ": 160.52}
    day2_lifecycle = {
        "601728.SH": {
            "state": "HELD_ACTIVE",
            "entry_price": 6.91,
            "held_bars": 4,
        },
        "300394.SZ": {
            "state": "HELD_GESTATION",
            "entry_price": 160.52,
            "held_bars": 0,
        },
    }

    # Arm A: Unvoided
    _snapshot(
        root_a,
        "2026-11-05",
        "150000",
        nav=100000.0,
        entry_prices=day1_entry_prices,
        lifecycle_states=day1_lifecycle,
    )
    _snapshot(
        root_a,
        "2026-11-06",
        "150000",
        nav=105000.0,
        entry_prices=day2_entry_prices,
        lifecycle_states=day2_lifecycle,
    )

    sel_a = _select(root_a, as_of="2026-11-09")
    assert sel_a.source is not None
    assert sel_a.source["as_of"] == "2026-11-06"
    assert sel_a.source["instance"] == "150000"
    assert sel_a.state["entry_prices"] == day2_entry_prices
    assert "300394.SZ" in sel_a.state["entry_prices"]
    assert sel_a.state["entry_prices"]["300394.SZ"] == 160.52
    assert sel_a.state["lifecycle_states"]["300394.SZ"]["state"] == "HELD_GESTATION"
    assert sel_a.source["skipped"] == []

    # Arm B: Voided on Day 2
    day1_dir_b = _snapshot(
        root_b,
        "2026-11-05",
        "150000",
        nav=100000.0,
        entry_prices=day1_entry_prices,
        lifecycle_states=day1_lifecycle,
    )
    _snapshot(
        root_b,
        "2026-11-06",
        "150000",
        nav=105000.0,
        entry_prices=day2_entry_prices,
        lifecycle_states=day2_lifecycle,
        voided_marker="live_trading_ticket.voided.json",
    )

    sel_b = _select(root_b, as_of="2026-11-09")
    assert sel_b.source is not None
    assert sel_b.source["path"] == os.path.join(day1_dir_b, "account_state.json")
    assert sel_b.source["as_of"] == "2026-11-05"
    assert sel_b.source["instance"] == "150000"
    assert sel_b.state["entry_prices"] == day1_entry_prices
    # CRITICAL DISCRIMINATING PROBE: 300394.SZ must NOT exist in Arm B
    assert "300394.SZ" not in sel_b.state["entry_prices"]
    assert "300394.SZ" not in sel_b.state["lifecycle_states"]
    assert "601728.SH" in sel_b.state["entry_prices"]
    assert sel_b.state["entry_prices"]["601728.SH"] == 6.91

    # Verify audit trail in skipped list
    skipped_map = {s["instance"]: s for s in sel_b.source["skipped"]}
    assert "150000" in skipped_map
    assert skipped_map["150000"]["reason"] == SKIP_TICKET_VOIDED
    assert skipped_map["150000"]["reason"] == "ticket_voided"
    assert skipped_map["150000"]["run_date"] == "2026-11-06"

    # Verify state application to Day 3 account_state
    day3_state = {"as_of_date": "2026-11-09", "current_nav": 101000.0}
    log_line = apply_prior_state_selection(day3_state, sel_b)
    assert day3_state["prior_state"]["as_of_date"] == "2026-11-05"
    assert "300394.SZ" not in day3_state["prior_state"]["entry_prices"]
    assert day3_state["prior_state_source"]["as_of"] == "2026-11-05"
    assert "ticket_voided" in log_line


# ==============================================================================
# 2. Rule 10 Discriminating Probe #2: False COOLDOWN Avoidance
# ==============================================================================


def test_rule10_discriminating_probe_false_cooldown_avoidance(tmp_path):
    """Rule 10 Probe 2: Day 2 liquidated 601728.SH entering COOLDOWN, but ticket voided.

    Day 3 must NOT inherit COOLDOWN(remaining=2); it must inherit Day 1 HELD_ACTIVE.
    """
    day1_lifecycle = {
        "601728.SH": {
            "state": "HELD_ACTIVE",
            "entry_price": 6.91,
            "held_bars": 5,
        }
    }
    day2_lifecycle_cooldown = {
        "601728.SH": {
            "state": "COOLDOWN",
            "cooldown_remaining": 2,
            "cooldown_bars": 0,
            "exit_reason": "take_profit",
        }
    }

    day1_dir = _snapshot(
        tmp_path,
        "2026-11-05",
        "150000",
        nav=100000.0,
        entry_prices={"601728.SH": 6.91},
        lifecycle_states=day1_lifecycle,
    )
    _snapshot(
        tmp_path,
        "2026-11-06",
        "150000",
        nav=102000.0,
        entry_prices={},
        lifecycle_states=day2_lifecycle_cooldown,
        voided_marker="live_trading_ticket.voided.json",
    )

    selection = _select(tmp_path, as_of="2026-11-09")
    assert selection.source is not None
    assert selection.source["path"] == os.path.join(day1_dir, "account_state.json")
    assert selection.source["as_of"] == "2026-11-05"

    # Discriminating probe: State is active, not in cooldown
    st = selection.state["lifecycle_states"]["601728.SH"]
    assert st["state"] == "HELD_ACTIVE"
    assert "cooldown_remaining" not in st
    assert selection.state["entry_prices"]["601728.SH"] == 6.91

    # Verify skipped record
    assert len(selection.source["skipped"]) == 1
    assert selection.source["skipped"][0]["reason"] == SKIP_TICKET_VOIDED


# ==============================================================================
# 3. Same-Day Sibling Instance Fallback
# ==============================================================================


def test_falls_back_to_same_day_successful_instance_when_newer_is_voided(tmp_path):
    """Day 2 has 120000 (valid SUCCESS) and 145832 (voided SUCCESS).

    Day 3 must skip 145832 and select SAME-DAY 120000, NOT jump back to Day 1.
    """
    _snapshot(tmp_path, "2026-11-05", "150000", nav=100000.0)
    valid_sibling = _snapshot(tmp_path, "2026-11-06", "120000", nav=101000.0)
    _snapshot(
        tmp_path,
        "2026-11-06",
        "145832",
        nav=102000.0,
        voided_marker="live_trading_ticket.voided.json",
    )

    selection = _select(tmp_path, as_of="2026-11-09")

    assert selection.source is not None
    assert selection.source["path"] == os.path.join(valid_sibling, "account_state.json")
    assert selection.source["run_date"] == "2026-11-06"
    assert selection.source["instance"] == "120000"
    assert selection.state["current_nav"] == pytest.approx(101000.0)

    # 145832 is recorded in skipped with ticket_voided
    skipped = {s["instance"]: s for s in selection.source["skipped"]}
    assert "145832" in skipped
    assert skipped["145832"]["reason"] == SKIP_TICKET_VOIDED
    assert skipped["145832"]["run_status"] == "SUCCESS"


def test_all_same_day_instances_voided_falls_back_to_older_date(tmp_path):
    """When ALL instances of Day 2 are voided/failed, resolver falls back to Day 1."""
    older_dir = _snapshot(tmp_path, "2026-11-05", "150000", nav=100000.0)
    _snapshot(
        tmp_path,
        "2026-11-06",
        "120000",
        nav=101000.0,
        voided_marker="live_trading_ticket.voided.json",
    )
    _snapshot(
        tmp_path,
        "2026-11-06",
        "150000",
        nav=102000.0,
        voided_marker="netted_trading_ticket.voided.json",
    )

    selection = _select(tmp_path, as_of="2026-11-09")

    assert selection.source is not None
    assert selection.source["path"] == os.path.join(older_dir, "account_state.json")
    assert selection.source["run_date"] == "2026-11-05"
    assert len(selection.source["skipped"]) == 2
    for s in selection.source["skipped"]:
        assert s["reason"] == SKIP_TICKET_VOIDED


# ==============================================================================
# 4. Multi-Strategy Netted Void Marker
# ==============================================================================


def test_netted_trading_ticket_voided_triggers_skip(tmp_path):
    """netted_trading_ticket.voided.json triggers SKIP_TICKET_VOIDED."""
    older_dir = _snapshot(tmp_path, "2026-11-05", "150000", nav=100000.0)
    _snapshot(
        tmp_path,
        "2026-11-06",
        "150000",
        nav=102000.0,
        voided_marker="netted_trading_ticket.voided.json",
    )

    selection = _select(tmp_path, as_of="2026-11-09")

    assert selection.source is not None
    assert selection.source["path"] == os.path.join(older_dir, "account_state.json")
    assert selection.source["as_of"] == "2026-11-05"

    skipped = selection.source["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["instance"] == "150000"
    assert skipped[0]["reason"] == SKIP_TICKET_VOIDED
    assert skipped[0]["reason"] == "ticket_voided"


# ==============================================================================
# 5. Unvoided Byte-Identical Regression + Flat Date Directory Compatibility
# ==============================================================================


def test_unvoided_candidates_maintain_identical_behavior(tmp_path):
    """When no voided marker is present, list_prior_state_candidates maintains exact behavior."""
    s1 = _snapshot(tmp_path, "2026-11-05", "150000", nav=100000.0)
    s2 = _snapshot(tmp_path, "2026-11-06", "150000", nav=105000.0)

    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)
    candidates = list_prior_state_candidates(session_root, "2026-11-09", expected_base_version="2.0.0")

    assert len(candidates) == 2
    assert candidates[0].run_date == "2026-11-06"
    assert candidates[0].eligible is True
    assert candidates[0].reason == "eligible"
    assert candidates[1].run_date == "2026-11-05"
    assert candidates[1].eligible is True

    selection = select_prior_state(session_root, "2026-11-09", expected_base_version="2.0.0")
    assert selection.source["path"] == os.path.join(s2, "account_state.json")
    assert selection.source["skipped"] == []


def test_flat_date_directory_voided_marker_is_skipped(tmp_path):
    """Flat directory (<date>/account_state.json, no instance subdir) with voided marker is skipped."""
    older_dir = _snapshot(tmp_path, "2026-11-05", instance=None, nav=100000.0)
    _snapshot(
        tmp_path,
        "2026-11-06",
        instance=None,
        nav=105000.0,
        voided_marker="live_trading_ticket.voided.json",
    )

    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)
    candidates = list_prior_state_candidates(session_root, "2026-11-09", expected_base_version="2.0.0")

    assert len(candidates) == 2
    # Day 2 candidate is flat and skipped with ticket_voided
    c_day2 = next(c for c in candidates if c.run_date == "2026-11-06")
    assert c_day2.instance == "flat"
    assert c_day2.eligible is False
    assert c_day2.reason == SKIP_TICKET_VOIDED

    # Day 1 candidate is flat and eligible
    c_day1 = next(c for c in candidates if c.run_date == "2026-11-05")
    assert c_day1.instance == "flat"
    assert c_day1.eligible is True

    # select_prior_state picks Day 1
    selection = select_prior_state(session_root, "2026-11-09", expected_base_version="2.0.0")
    assert selection.source["path"] == os.path.join(older_dir, "account_state.json")
    assert selection.source["instance"] == "flat"
    assert selection.source["as_of"] == "2026-11-05"
    assert selection.source["skipped"][0]["instance"] == "flat"
    assert selection.source["skipped"][0]["reason"] == SKIP_TICKET_VOIDED


def test_flat_date_directory_netted_voided_marker_is_skipped(tmp_path):
    """Flat directory with netted_trading_ticket.voided.json is also skipped."""
    older_dir = _snapshot(tmp_path, "2026-11-05", instance=None, nav=100000.0)
    _snapshot(
        tmp_path,
        "2026-11-06",
        instance=None,
        nav=105000.0,
        voided_marker="netted_trading_ticket.voided.json",
    )

    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)
    selection = select_prior_state(session_root, "2026-11-09", expected_base_version="2.0.0")

    assert selection.source["path"] == os.path.join(older_dir, "account_state.json")
    assert selection.source["as_of"] == "2026-11-05"
    assert selection.source["skipped"][0]["instance"] == "flat"
    assert selection.source["skipped"][0]["reason"] == SKIP_TICKET_VOIDED

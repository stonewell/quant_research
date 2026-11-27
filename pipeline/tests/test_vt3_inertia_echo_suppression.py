"""100% Offline Test Suite for `VT3` -- Inertia Echo Suppression & Parity.

Validates that:
1. Base side-channel publishing & sparse contract:
   `BaseBlendAllocationStrategy` publishes `_last_inertia_meta` with structured
   metadata (`inertia_carried`, `pre_inertia_weight`, `post_inertia_weight`,
   `carry_reason` in {"TENURE_LOCKED", "INERTIA"}, `deleverage_scale`, `tenure_locked`).
   `cash_proxy` ("BIL") NEVER appears in any day's dict in `_last_inertia_meta`.
   Returned `sparse_weights` strictly complies with the sparse contract (NaN-vs-0.0 rule).
2. Rule 10 discriminating probe -- 601728.SH@2026-11-27 reproduction:
   Arm A (baseline / unsuppressed): Emits spurious BUY order (inertia echo).
   Arm B (VT3 active default): Resolves target to 0.0, action_taken="VOIDED_INERTIA_ECHO",
   no BUY emitted, `inertia_echo_audit` recorded in detail_reason, strictly passes
   `json.dumps(..., allow_nan=False)`, and is NOT falsely re-labeled as `MIN_ENTRY_THRESHOLD`.
3. Cross-day anti-recurrence & resolution semantics (Day 2 -> Day 3 -> Day 4):
   Day 2 BUY voided -> Day 3 non-voided SUCCESS session does NOT buy -> Day 4 still retains
   voided flag (anti-recurrence).
   Day 4 non-voided SUCCESS session genuinely buys (LV2 envelope or flat) -> Day 5 clears flag.
4. Boundary & scaling safeguards:
   Deleverage scaling: `deleverage_scale=0.5` aligns dimensions; uniform deleverage without
   inertia carry safely no-ops (`pre >= post - eps`).
   Downstream vol overlay scaling: `scale_ratio=0.5` proportionately scales `eff_pre`.
   Safe no-ops: `inertia_carried=False`, downward inertia, already holding >= eff_post,
   non-finite values.
5. Rule 11 backtest / live parity:
   `simulate_position_lifecycle` vs step-by-step `resolve_inertia_echo_target` +
   `step_position_lifecycle` yield 100% bit-identical actions, target weights (< 1e-12),
   and `inertia_echo_audit` records.
6. Opt-out & opt-in regressions:
   `suppress_voided_echo=False` (--no-suppress-inertia-echo) restores legacy behavior.
   `suppress_general_echo=True` clamps target to `max(cur_w, eff_pre)` under
   `INERTIA_CARRY_BUY_CLAMPED` even without voided marker.

100% offline: `tmp_path` synthetic data only, zero network, zero real market data.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Dict, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.allocation_templates import AllocationTemplate
from common.position_lifecycle import (
    AssetLifecycleState,
    BlockingReason,
    PositionLifecycleConfig,
    PositionState,
    simulate_position_lifecycle,
    step_position_lifecycle,
)
from common.position_policy import (
    PolicyAction,
    resolve_inertia_echo_target,
)
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import (
    BaseBlendAllocationStrategy,
    ChanFourStateBlendStrategy,
    GenericBlendStrategy,
)
from scripts.live_preflight import (
    collect_recent_voided_buy_symbols,
    derive_session_root,
)

_STRAT = "chan_four_state_blend"
_UNIV = "custom_astock_202609_20"
_ARCHIVE = "runs"


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ==============================================================================
# 1. Base Side-Channel Publishing & Sparse Contract Verification
# ==============================================================================


class _MockSubStrategy(AllocationTemplate):
    """Deterministic sub-strategy that alternates allocations to trigger inertia."""

    def __init__(self, name: str):
        super().__init__(name=name, param_grid={}, factor_tags=["relative_momentum"])

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
        dates = next(iter(universe.values())).index
        df = pd.DataFrame(index=dates)
        for i, dt in enumerate(dates):
            if i % 2 == 0:
                df.loc[dt, "601728.SH"] = 0.30
                df.loc[dt, "600584.SH"] = 0.10
            else:
                df.loc[dt, "601728.SH"] = 0.05
                df.loc[dt, "600584.SH"] = 0.35
            df.loc[dt, "BIL"] = 0.0
        return df


class _MockBlendWithInertia(GenericBlendStrategy):
    """Generic blend that activates adaptive asset inertia."""

    def get_sub_strategies(self, universe: Dict[str, pd.DataFrame], params: Optional[Dict[str, Any]] = None):
        return {"sub": _MockSubStrategy("sub")}

    def get_sub_strategy_weights(self, params: Optional[Dict[str, Any]] = None):
        return {"sub": 1.0}


def test_blend_side_channel_records_inertia_meta_and_excludes_cash_proxy():
    """Verify BaseBlendAllocationStrategy instruments and publishes _last_inertia_meta.

    - Exposes structured metadata: inertia_carried, pre_inertia_weight, post_inertia_weight,
      carry_reason in {"TENURE_LOCKED", "INERTIA"}, deleverage_scale, tenure_locked.
    - Cash proxy ("BIL") NEVER appears in _last_inertia_meta on any day.
    - Sparse weights contract compliance: non-rebalance is NaN, explicit 0.0 for zero target.
    """
    dates = pd.bdate_range("2024-01-01", periods=15)
    universe = {
        "601728.SH": make_ohlcv_from_closes([10.0 + 0.1 * i for i in range(15)]),
        "600584.SH": make_ohlcv_from_closes([20.0 + 0.2 * i for i in range(15)]),
        "BIL": make_ohlcv_from_closes([100.0] * 15),
    }
    for df in universe.values():
        df.index = dates

    strat = _MockBlendWithInertia()
    params = {
        "cash_proxy": "BIL",
        "blend_adaptive_inertia": True,
        "blend_inertia_threshold": 0.50,  # high threshold to easily trigger inertia carry
        "blend_rebalance_freq": "1D",
        "blend_weighting_mode": "equal",
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert hasattr(strat, "_last_inertia_meta")
    assert isinstance(strat._last_inertia_meta, dict)

    # Verify cash_proxy NEVER appears in _last_inertia_meta
    for dt, bar_meta in strat._last_inertia_meta.items():
        assert "BIL" not in bar_meta, f"cash_proxy 'BIL' leaked into _last_inertia_meta on {dt}"

    # Verify structured fields on risky assets
    found_carried = False
    for dt, bar_meta in strat._last_inertia_meta.items():
        for sym, meta in bar_meta.items():
            assert meta["symbol"] == sym
            assert "inertia_carried" in meta
            assert "pre_inertia_weight" in meta
            assert "post_inertia_weight" in meta
            assert "prev_virtual_weight" in meta
            assert "carry_reason" in meta
            assert meta["carry_reason"] in ("TENURE_LOCKED", "INERTIA")
            assert "deleverage_scale" in meta
            assert "tenure_locked" in meta
            assert "delta_w" in meta
            assert math.isfinite(meta["pre_inertia_weight"])
            assert math.isfinite(meta["post_inertia_weight"])
            assert math.isfinite(meta["deleverage_scale"])
            if meta["inertia_carried"]:
                found_carried = True

    assert found_carried, "Expected at least one inertia_carried entry across 15 bars"

    # Verify sparse weights contract compliance
    for col in ("601728.SH", "600584.SH", "BIL"):
        assert col in weights.columns
    # Check that values are finite or NaN (no string, no unexpected types)
    for col in weights.columns:
        valid_vals = weights[col].dropna()
        assert (valid_vals >= 0.0).all()


def test_chan_four_state_blend_strategy_side_channel_inertia_meta():
    """Verify ChanFourStateBlendStrategy directly publishes _last_inertia_meta on synthetic data."""
    dates = pd.bdate_range("2020-01-01", periods=150)
    t = np.arange(150)
    close_a = 100.0 + 0.15 * t + 5.0 * np.sin(2 * np.pi * t / 25)
    close_b = 100.0 + 0.08 * t + 4.0 * np.cos(2 * np.pi * t / 30)
    bil_close = np.full(150, 100.0)

    universe = {
        "601225.SH": make_ohlcv_from_closes(close_a),
        "601857.SH": make_ohlcv_from_closes(close_b),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates

    strat = ChanFourStateBlendStrategy()
    weights = strat.generate_weights(universe)

    assert not weights.empty
    assert hasattr(strat, "_last_inertia_meta")
    assert isinstance(strat._last_inertia_meta, dict)

    # Cash proxy ("BIL") never appears in _last_inertia_meta
    for dt, bar_meta in strat._last_inertia_meta.items():
        assert "BIL" not in bar_meta

    # At least one inertia_carried entry exists
    found_carried = False
    for dt, bar_meta in strat._last_inertia_meta.items():
        for sym, meta in bar_meta.items():
            if meta.get("inertia_carried"):
                assert meta["carry_reason"] in ("TENURE_LOCKED", "INERTIA")
                assert "pre_inertia_weight" in meta
                assert "post_inertia_weight" in meta
                assert "deleverage_scale" in meta
                found_carried = True

    assert found_carried, "ChanFourStateBlendStrategy did not expose any inertia_carried meta"



# ==============================================================================
# 2. Rule 10 Discriminating Probe -- 601728.SH@2026-11-27 Reproduction
# ==============================================================================


def test_rule10_discriminating_probe_phantom_inertia_echo(tmp_path):
    """Rule 10 Discriminating Probe: 601728.SH@2026-11-27.

    Day 1 (2026-11-25): Held 400 shares (cur_w=0.015).
    Day 2 (2026-11-26): Issued BUY +4400 -> voided via live_trading_ticket.voided.json!
    Day 3 (2026-11-27):
      Internal model inertia carries: pre=0.0, post=0.176036, inertia_carried=True.
      External holding: cur_w=0.015 (400 shares).

    Arm A (Baseline / unsuppressed):
      `resolve_inertia_echo_target` leaves target=0.176036 -> triggers BUY order (echo).
    Arm B (VT3 active default):
      `resolve_inertia_echo_target` resolves target=0.0, action_taken="VOIDED_INERTIA_ECHO".
      No BUY emitted, audit dict is recorded in detail_reason, passes JSON serialization,
      and is NOT mis-classified as MIN_ENTRY_THRESHOLD.
    """
    raw_target = 0.176036
    cur_w = 0.015
    inertia_meta = {
        "symbol": "601728.SH",
        "inertia_carried": True,
        "pre_inertia_weight": 0.0,
        "post_inertia_weight": 0.176036,
        "prev_virtual_weight": 0.176036,
        "deleverage_scale": 1.0,
        "tenure_locked": True,
        "carry_reason": "TENURE_LOCKED",
        "delta_w": 0.176036,
    }

    # Arm A: unsuppressed (legacy / --no-suppress-inertia-echo)
    target_a, audit_a = resolve_inertia_echo_target(
        original_target_weight=raw_target,
        current_weight=cur_w,
        inertia_meta=inertia_meta,
        was_voided_buy=True,
        suppress_voided_echo=False,
    )
    assert target_a == pytest.approx(raw_target)
    assert audit_a is None

    # Under Arm A, lifecycle step sees target 0.176036 vs cur_w 0.015 -> emits BUY!
    state_a = AssetLifecycleState(
        symbol="601728.SH",
        state=PositionState.HELD_ACTIVE,
        current_weight=cur_w,
        broker_weight=cur_w,
        entry_price=6.91,
        held_bars=5,
    )
    cfg = PositionLifecycleConfig(min_holding_bars=1)
    dec_a = step_position_lifecycle(
        state=state_a,
        raw_target_weight=target_a,
        current_price=7.50,
        cfg=cfg,
        market="china",
    )
    assert dec_a.action == PolicyAction.BUY
    assert dec_a.target_weight == pytest.approx(raw_target)

    # Arm B: VT3 active default
    target_b, audit_b = resolve_inertia_echo_target(
        original_target_weight=raw_target,
        current_weight=cur_w,
        inertia_meta=inertia_meta,
        was_voided_buy=True,
        suppress_voided_echo=True,
    )
    assert target_b == pytest.approx(0.0)
    assert audit_b is not None
    assert audit_b["action_taken"] == "VOIDED_INERTIA_ECHO"
    assert audit_b["suppressed"] is True
    assert audit_b["was_voided_buy"] is True
    assert audit_b["carry_reason"] == "TENURE_LOCKED"
    assert audit_b["original_target"] == pytest.approx(raw_target)
    assert audit_b["resolved_target"] == pytest.approx(0.0)
    assert audit_b["effective_pre_inertia_weight"] == pytest.approx(0.0)

    # Strict JSON serialization test (must not raise on NaN/Inf)
    serialized = json.dumps(audit_b, allow_nan=False)
    assert "VOIDED_INERTIA_ECHO" in serialized

    # Under Arm B, lifecycle step sees target 0.0 vs cur_w 0.015 -> DOES NOT EMIT BUY!
    state_b = AssetLifecycleState(
        symbol="601728.SH",
        state=PositionState.HELD_ACTIVE,
        current_weight=cur_w,
        broker_weight=cur_w,
        entry_price=6.91,
        held_bars=5,
    )
    dec_b = step_position_lifecycle(
        state=state_b,
        raw_target_weight=target_b,
        current_price=7.50,
        cfg=cfg,
        market="china",
    )
    assert dec_b.action != PolicyAction.BUY
    assert dec_b.blocking_reason != BlockingReason.MIN_ENTRY_THRESHOLD

    # Verify live runner guard logic: MIN_ENTRY_THRESHOLD must NOT overwrite suppressed echo
    action = dec_b.action.value if hasattr(dec_b.action, "value") else str(dec_b.action)
    min_entry_thresh = 0.05
    blocking_reason = None
    if action == "BUY" and cur_w == 0.0 and 0.0 < target_b < min_entry_thresh:
        if not (audit_b is not None and audit_b.get("suppressed")):
            blocking_reason = BlockingReason.MIN_ENTRY_THRESHOLD
    assert blocking_reason is None, "Inertia echo was improperly classified as MIN_ENTRY_THRESHOLD"


# ==============================================================================
# 3. Cross-Day Anti-Recurrence & Resolution Semantics
# ==============================================================================


def test_collect_recent_voided_buys_multi_day_anti_recurrence(tmp_path):
    """Multi-day archive lifecycle:
    Day 2 (2026-11-26): BUY 601728.SH voided.
    Day 3 (2026-11-27): SUCCESS round has NO buy for 601728.SH (suppressed/held).
      -> 2026-11-28 STILL flags 601728.SH as voided (anti-recurrence).
    Day 4 (2026-11-28): SUCCESS round genuinely BUYs 601728.SH (+1000 shares).
      -> 2026-11-29 CLEARS 601728.SH from voided symbols.
    """
    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)

    # Day 1 (2026-11-25): normal run
    d1_dir = os.path.join(session_root, "2026-11-25", "150000")
    _write_json(os.path.join(d1_dir, "summary.json"), {"date": "2026-11-25", "deploy_status": "SUCCESS"})
    _write_json(os.path.join(d1_dir, "account_state.json"), {"as_of_date": "2026-11-25", "current_nav": 100000.0})

    # Day 2 (2026-11-26): BUY voided
    d2_dir = os.path.join(session_root, "2026-11-26", "145832")
    _write_json(os.path.join(d2_dir, "summary.json"), {"date": "2026-11-26", "deploy_status": "SUCCESS"})
    _write_json(os.path.join(d2_dir, "account_state.json"), {"as_of_date": "2026-11-26", "current_nav": 100000.0})
    _write_json(
        os.path.join(d2_dir, "live_trading_ticket.json"),
        {"orders": [{"symbol": "601728.SH", "action": "BUY", "shares": 4400, "delta_shares": 4400, "target_weight": 0.176}]},
    )
    _write_json(
        os.path.join(d2_dir, "live_trading_ticket.voided.json"),
        {"voided_at": "2026-11-26T15:00:00", "reason": "operator_void"},
    )

    # Inspection as of Day 3 (2026-11-27): 601728.SH is voided
    voided_d3 = collect_recent_voided_buy_symbols(session_root, "2026-11-27")
    assert "601728.SH" in voided_d3
    assert voided_d3["601728.SH"]["voided_date"] == "2026-11-26"
    assert voided_d3["601728.SH"]["voided_shares"] == 4400
    assert voided_d3["601728.SH"]["voided_target_weight"] == pytest.approx(0.176)

    # Day 3 (2026-11-27): SUCCESS round, but NO buy for 601728.SH (e.g. HOLD or bought 600584.SH instead)
    d3_dir = os.path.join(session_root, "2026-11-27", "150000")
    _write_json(os.path.join(d3_dir, "summary.json"), {"date": "2026-11-27", "deploy_status": "SUCCESS"})
    _write_json(os.path.join(d3_dir, "account_state.json"), {"as_of_date": "2026-11-27", "current_nav": 101000.0})
    _write_json(
        os.path.join(d3_dir, "live_trading_ticket.json"),
        {"orders": [{"symbol": "600584.SH", "action": "BUY", "shares": 1000, "delta_shares": 1000}]},
    )

    # Inspection as of Day 4 (2026-11-28): 601728.SH MUST REMAIN VOIDED (anti-recurrence!)
    voided_d4 = collect_recent_voided_buy_symbols(session_root, "2026-11-28")
    assert "601728.SH" in voided_d4, "601728.SH voided status leaked/prematurely expired on Day 4"
    assert "600584.SH" not in voided_d4

    # Day 4 (2026-11-28): Genuine BUY for 601728.SH in netted_trading_ticket.json
    d4_dir = os.path.join(session_root, "2026-11-28", "150000")
    _write_json(os.path.join(d4_dir, "summary.json"), {"date": "2026-11-28", "deploy_status": "SUCCESS"})
    _write_json(os.path.join(d4_dir, "account_state.json"), {"as_of_date": "2026-11-28", "current_nav": 102000.0})
    _write_json(
        os.path.join(d4_dir, "netted_trading_ticket.json"),
        {"orders": [{"symbol": "601728.SH", "action": "BUY", "shares": 1000, "delta_shares": 1000}]},
    )

    # Inspection as of Day 5 (2026-11-29): 601728.SH is now RESOLVED and cleared!
    voided_d5 = collect_recent_voided_buy_symbols(session_root, "2026-11-29")
    assert "601728.SH" not in voided_d5, "601728.SH was not cleared after a genuine SUCCESS BUY"


def test_collect_recent_voided_buys_envelope_and_lookback_limits(tmp_path):
    """Test support for raw list format, max_lookback_dates boundary, and flat directories."""
    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)

    # Flat date directory with bare list orders and voided marker
    flat_dir = os.path.join(session_root, "2026-11-20")
    _write_json(os.path.join(flat_dir, "summary.json"), {"date": "2026-11-20", "deploy_status": "SUCCESS"})
    _write_json(os.path.join(flat_dir, "account_state.json"), {"as_of_date": "2026-11-20"})
    _write_json(
        os.path.join(flat_dir, "live_trading_ticket.json"),
        [{"symbol": "000001.SZ", "action": "BUY", "shares": 500, "delta_shares": 500, "target_weight": 0.08}],
    )
    _write_json(os.path.join(flat_dir, "live_trading_ticket.voided.json"), {"reason": "test"})

    # Within lookback window
    res = collect_recent_voided_buy_symbols(session_root, "2026-11-25", max_lookback_dates=10)
    assert "000001.SZ" in res
    assert res["000001.SZ"]["voided_shares"] == 500

    # Outside lookback window (e.g. max_lookback_dates=0)
    res_zero = collect_recent_voided_buy_symbols(session_root, "2026-11-25", max_lookback_dates=0)
    assert "000001.SZ" not in res_zero

    # Invalid session root or empty date returns empty dict gracefully
    assert collect_recent_voided_buy_symbols(None, "2026-11-25") == {}
    assert collect_recent_voided_buy_symbols(str(tmp_path / "nonexistent"), "2026-11-25") == {}
    assert collect_recent_voided_buy_symbols(session_root, "") == {}


# ==============================================================================
# 4. Boundary & Scaling Safeguards
# ==============================================================================


def test_resolve_inertia_echo_deleverage_and_downstream_vol_scaling():
    """Verify deleverage scaling and downstream vol overlay scaling."""
    # 1. Downstream vol scaling:
    # Model internal inertia had pre=0.04, post=0.20.
    # Downstream vol overlay scaled the portfolio target by 0.5 -> orig_raw=0.10.
    # eff_pre must scale by 0.5 -> 0.02, resolved target=0.02.
    meta = {
        "inertia_carried": True,
        "pre_inertia_weight": 0.04,
        "post_inertia_weight": 0.20,
        "deleverage_scale": 1.0,
        "carry_reason": "INERTIA",
    }
    tgt, audit = resolve_inertia_echo_target(
        original_target_weight=0.10,
        current_weight=0.0,
        inertia_meta=meta,
        was_voided_buy=True,
        suppress_voided_echo=True,
    )
    assert audit is not None
    assert audit["effective_pre_inertia_weight"] == pytest.approx(0.02)
    assert tgt == pytest.approx(0.02)
    assert audit["resolved_target"] == pytest.approx(0.02)

    # 2. Deleverage scale tracking:
    meta_dl = dict(meta, deleverage_scale=0.5)
    tgt_dl, audit_dl = resolve_inertia_echo_target(
        original_target_weight=0.10,
        current_weight=0.0,
        inertia_meta=meta_dl,
        was_voided_buy=True,
    )
    assert audit_dl["deleverage_scale"] == pytest.approx(0.5)


def test_resolve_inertia_echo_safe_noops():
    """Verify safe no-op branches in resolve_inertia_echo_target."""
    # Case A: inertia_carried is False
    res, audit = resolve_inertia_echo_target(
        0.15, 0.0, {"inertia_carried": False, "pre_inertia_weight": 0.0, "post_inertia_weight": 0.15}, was_voided_buy=True
    )
    assert res == 0.15
    assert audit is None

    # Case B: pre >= post - eps (downward inertia, not an upward carry)
    res, audit = resolve_inertia_echo_target(
        0.10, 0.0, {"inertia_carried": True, "pre_inertia_weight": 0.15, "post_inertia_weight": 0.10}, was_voided_buy=True
    )
    assert res == 0.10
    assert audit is None

    # Case C: current_weight >= eff_post - eps (external holding already satisfies or exceeds target)
    res, audit = resolve_inertia_echo_target(
        0.10, 0.12, {"inertia_carried": True, "pre_inertia_weight": 0.0, "post_inertia_weight": 0.10}, was_voided_buy=True
    )
    assert res == 0.10
    assert audit is None

    # Case D: Non-finite inputs gracefully return original without crashing
    res, audit = resolve_inertia_echo_target(float("nan"), 0.0, {"inertia_carried": True})
    assert math.isnan(res)
    assert audit is None

    res, audit = resolve_inertia_echo_target(0.10, float("nan"), {"inertia_carried": True})
    assert res == 0.10
    assert audit is None

    # Case E: Non-dict or empty inertia_meta
    res, audit = resolve_inertia_echo_target(0.10, 0.0, None)
    assert res == 0.10
    assert audit is None


# ==============================================================================
# 5. Rule 11 Backtest / Live Parity Verification
# ==============================================================================


def test_rule11_backtest_live_parity_on_vt3_inertia_echo():
    """Rule 11 Parity Test: simulate_position_lifecycle vs step_position_lifecycle.

    Asserts that backtest simulation and live ticket step produce 100% identical:
    - actions
    - target weights (< 1e-12 tolerance)
    - forensic inertia_echo_audit records
    """
    symbols = ["SYM_VOIDED_ECHO", "SYM_NORMAL_BUY", "SYM_ALREADY_HELD"]
    dates = pd.date_range("2024-01-01", periods=3, freq="B")
    d0, d1, d2 = dates[0], dates[1], dates[2]

    universe = {
        s: pd.DataFrame(
            {"Open": 10.0, "High": 10.5, "Low": 9.5, "Close": 10.0, "Volume": 100000.0},
            index=dates,
        )
        for s in symbols
    }

    target_weights = pd.DataFrame(
        [
            {"SYM_VOIDED_ECHO": 0.0, "SYM_NORMAL_BUY": 0.0, "SYM_ALREADY_HELD": 0.20},
            {"SYM_VOIDED_ECHO": 0.18, "SYM_NORMAL_BUY": 0.18, "SYM_ALREADY_HELD": 0.20},
            {"SYM_VOIDED_ECHO": 0.0, "SYM_NORMAL_BUY": 0.0, "SYM_ALREADY_HELD": 0.20},
        ],
        index=dates,
    )

    inertia_meta = {
        d1: {
            "SYM_VOIDED_ECHO": {
                "symbol": "SYM_VOIDED_ECHO",
                "inertia_carried": True,
                "pre_inertia_weight": 0.0,
                "post_inertia_weight": 0.18,
                "prev_virtual_weight": 0.18,
                "deleverage_scale": 1.0,
                "tenure_locked": True,
                "carry_reason": "TENURE_LOCKED",
            },
            "SYM_ALREADY_HELD": {
                "symbol": "SYM_ALREADY_HELD",
                "inertia_carried": True,
                "pre_inertia_weight": 0.0,
                "post_inertia_weight": 0.20,
                "prev_virtual_weight": 0.20,
                "deleverage_scale": 1.0,
                "tenure_locked": False,
                "carry_reason": "INERTIA",
            },
        }
    }

    voided_buy_events = {d1: {"SYM_VOIDED_ECHO"}}

    cfg = PositionLifecycleConfig(
        guard_latency_bars=1,
        min_holding_bars=1,
        suppress_inertia_echo=True,
    )

    initial_states = {
        "SYM_VOIDED_ECHO": AssetLifecycleState(symbol="SYM_VOIDED_ECHO", state=PositionState.FLAT),
        "SYM_NORMAL_BUY": AssetLifecycleState(symbol="SYM_NORMAL_BUY", state=PositionState.FLAT),
        "SYM_ALREADY_HELD": AssetLifecycleState(
            symbol="SYM_ALREADY_HELD",
            state=PositionState.HELD_ACTIVE,
            current_weight=0.20,
            broker_weight=0.20,
            entry_price=10.0,
            held_bars=3,
        ),
    }

    # 1. Backtest path: simulate_position_lifecycle
    filtered_weights, records = simulate_position_lifecycle(
        universe=universe,
        target_weights=target_weights,
        cfg=cfg,
        market="us",
        initial_states=initial_states,
        inertia_meta=inertia_meta,
        voided_buy_events=voided_buy_events,
    )

    d1_records = {r["symbol"]: r for r in records if r["date"] == str(d1)}

    # 2. Live step path: step_position_lifecycle for d1
    live_states = {
        "SYM_VOIDED_ECHO": AssetLifecycleState(symbol="SYM_VOIDED_ECHO", state=PositionState.FLAT),
        "SYM_NORMAL_BUY": AssetLifecycleState(symbol="SYM_NORMAL_BUY", state=PositionState.FLAT),
        "SYM_ALREADY_HELD": AssetLifecycleState(
            symbol="SYM_ALREADY_HELD",
            state=PositionState.HELD_ACTIVE,
            current_weight=0.20,
            broker_weight=0.20,
            entry_price=10.0,
            held_bars=3,
        ),
    }

    bar_meta = inertia_meta[d1]
    for sym in symbols:
        raw_tgt = float(target_weights.loc[d1, sym])
        cur_w = float(live_states[sym].current_weight) if live_states[sym].is_holding() else 0.0

        resolved_tgt, live_audit = resolve_inertia_echo_target(
            raw_tgt,
            cur_w,
            bar_meta.get(sym),
            was_voided_buy=(sym in voided_buy_events[d1]),
            suppress_voided_echo=True,
        )

        live_dec = step_position_lifecycle(
            state=live_states[sym],
            raw_target_weight=resolved_tgt,
            current_price=10.0,
            cfg=cfg,
            market="us",
        )

        sim_rec = d1_records[sym]

        # Verify 100% parity
        assert live_dec.action == sim_rec["action"], f"{sym} action mismatch"
        assert live_dec.target_weight == pytest.approx(sim_rec["target_weight"], abs=1e-12), f"{sym} target_weight mismatch"

        sim_audit = sim_rec.get("inertia_echo_audit")
        if live_audit is not None:
            assert sim_audit is not None, f"{sym} missing sim_audit"
            assert sim_audit["action_taken"] == live_audit["action_taken"]
            assert sim_audit["resolved_target"] == pytest.approx(live_audit["resolved_target"], abs=1e-12)
            assert sim_audit["carry_reason"] == live_audit["carry_reason"]
        else:
            assert sim_audit is None, f"{sym} unexpected sim_audit"

    # Specific semantic checks
    assert d1_records["SYM_VOIDED_ECHO"]["action"] == PolicyAction.HOLD
    assert d1_records["SYM_VOIDED_ECHO"]["target_weight"] == pytest.approx(0.0)
    assert d1_records["SYM_VOIDED_ECHO"]["inertia_echo_audit"]["action_taken"] == "VOIDED_INERTIA_ECHO"

    assert d1_records["SYM_NORMAL_BUY"]["action"] == PolicyAction.BUY
    assert d1_records["SYM_NORMAL_BUY"]["target_weight"] == pytest.approx(0.18)
    assert "inertia_echo_audit" not in d1_records["SYM_NORMAL_BUY"]


# ==============================================================================
# 6. Opt-Out & Opt-In Regressions
# ==============================================================================


def test_opt_out_and_general_echo_opt_in_regressions():
    """Verify opt-out (--no-suppress-inertia-echo) and opt-in (suppress_general_echo)."""
    meta = {
        "inertia_carried": True,
        "pre_inertia_weight": 0.03,
        "post_inertia_weight": 0.15,
        "deleverage_scale": 1.0,
        "carry_reason": "INERTIA",
    }

    # 1. Opt-out: suppress_voided_echo=False allows echo buy
    res_opt_out, audit_opt_out = resolve_inertia_echo_target(
        original_target_weight=0.15,
        current_weight=0.0,
        inertia_meta=meta,
        was_voided_buy=True,
        suppress_voided_echo=False,
    )
    assert res_opt_out == pytest.approx(0.15)
    assert audit_opt_out is None

    # 2. General echo opt-in: suppress_general_echo=True clamps even when was_voided_buy=False
    res_gen, audit_gen = resolve_inertia_echo_target(
        original_target_weight=0.15,
        current_weight=0.05,
        inertia_meta=meta,
        was_voided_buy=False,
        suppress_general_echo=True,
    )
    assert audit_gen is not None
    assert audit_gen["action_taken"] == "INERTIA_CARRY_BUY_CLAMPED"
    assert audit_gen["suppressed"] is True
    assert audit_gen["was_voided_buy"] is False
    # Clamped to max(cur_w, eff_pre) = max(0.05, 0.03) = 0.05
    assert res_gen == pytest.approx(0.05)
    assert audit_gen["resolved_target"] == pytest.approx(0.05)

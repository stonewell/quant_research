"""Unit tests for Unified Position Lifecycle State Machine (UPLSM) and inertia fixes."""

import json
import os
import sys

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.allocation_templates import apply_asset_inertia
from common.position_lifecycle import (
    AssetLifecycleState,
    ExitType,
    LifecycleDecision,
    PositionLifecycleConfig,
    PositionState,
    step_position_lifecycle,
)


def test_normal_lifecycle_transitions():
    """Verifies standard progression: FLAT -> PENDING_ENTRY -> HELD_GESTATION -> HELD_ACTIVE -> PENDING_EXIT -> COOLDOWN -> FLAT."""
    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        min_holding_bars=1,
        normal_cooldown_bars=1,
        inertia_threshold=0.05,
    )
    state = AssetLifecycleState(symbol="600519")
    assert state.state == PositionState.FLAT
    assert not state.is_holding()

    # Bar 1: raw target 0.20 -> pending entry (streak = 1 / 2)
    d1 = step_position_lifecycle(state, raw_target_weight=0.20, current_price=100.0, cfg=cfg)
    assert d1.action == "HOLD_FILTERED"
    assert d1.target_weight == 0.0
    assert state.state == PositionState.PENDING_ENTRY
    assert state.entry_streak == 1

    # Bar 2: raw target 0.20 confirmed (streak = 2 / 2) -> BUY into gestation
    d2 = step_position_lifecycle(state, raw_target_weight=0.20, current_price=102.0, cfg=cfg)
    assert d2.action == "BUY"
    assert d2.target_weight == 0.20
    assert state.state == PositionState.HELD_GESTATION
    assert state.held_bars == 0
    assert state.entry_price == 102.0
    assert state.peak_price == 102.0
    assert state.current_weight == 0.20
    assert state.is_holding()

    # Bar 3: holding continues, held_bars reaches 1 >= min_holding_bars -> HELD_ACTIVE
    d3 = step_position_lifecycle(state, raw_target_weight=0.20, current_price=105.0, cfg=cfg)
    assert d3.action == "HOLD"
    assert d3.target_weight == 0.20
    assert state.state == PositionState.HELD_ACTIVE
    assert state.held_bars == 1
    assert state.peak_price == 105.0

    # Bar 4: strategy signals 0.0 weight -> pending exit (exit_streak = 1 / 2)
    d4 = step_position_lifecycle(state, raw_target_weight=0.0, current_price=104.0, cfg=cfg)
    assert d4.action == "HOLD_FILTERED"
    assert d4.target_weight == 0.20
    assert state.state == PositionState.PENDING_EXIT
    assert state.exit_streak == 1

    # Bar 5: strategy signals 0.0 weight again (exit_streak = 2 / 2) -> SELL
    d5 = step_position_lifecycle(state, raw_target_weight=0.0, current_price=103.0, cfg=cfg)
    assert d5.action == "SELL"
    assert d5.target_weight == 0.0
    assert state.state == PositionState.COOLDOWN
    assert state.cooldown_remaining == 1
    assert state.last_exit_type == ExitType.NORMAL.value
    assert state.current_weight == 0.0

    # Bar 6: cooldown expires -> transitions to FLAT
    d6 = step_position_lifecycle(state, raw_target_weight=0.0, current_price=103.0, cfg=cfg)
    assert d6.action == "HOLD"
    assert d6.target_weight == 0.0
    assert state.state == PositionState.FLAT
    assert state.cooldown_remaining == 0


def test_volume_breakout_fast_path_entry():
    """Verifies that strict confluence (volume surge + macro thrust) or super volume surge triggers entry bypass."""
    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        volume_surge_multiplier=1.5,
        breadth_thrust_threshold=0.60,
        enable_entry_fast_path=True,
    )
    # Case A: Strict confluence (Volume surge 2.0x >= 1.5x AND macro breadth 0.65 >= 0.60) triggers fast path
    state1 = AssetLifecycleState(symbol="NVDA")
    d1 = step_position_lifecycle(
        state1,
        raw_target_weight=0.15,
        current_price=120.0,
        cfg=cfg,
        current_volume=2000000.0,
        ma20_volume=1000000.0,
        market_breadth=0.65,
    )
    assert d1.action == "BUY"
    assert d1.target_weight == 0.15
    assert d1.fast_path_triggered == "VOLUME_BREAKOUT"
    assert state1.state == PositionState.HELD_GESTATION
    assert state1.entry_price == 120.0

    # Case B: Extreme standalone super volume surge (3.0x >= 2.5x) triggers fast path even with weak breadth
    state2 = AssetLifecycleState(symbol="AAPL")
    d2 = step_position_lifecycle(
        state2,
        raw_target_weight=0.10,
        current_price=180.0,
        cfg=cfg,
        current_volume=3000000.0,
        ma20_volume=1000000.0,
        market_breadth=0.30,
    )
    assert d2.action == "BUY"
    assert d2.fast_path_triggered == "VOLUME_BREAKOUT"
    assert state2.state == PositionState.HELD_GESTATION

    # Case C: Volume surge alone (2.0x >= 1.5x) without macro thrust (breadth 0.40 < 0.60) does NOT bypass
    state3 = AssetLifecycleState(symbol="GOOGL")
    d3 = step_position_lifecycle(
        state3,
        raw_target_weight=0.12,
        current_price=150.0,
        cfg=cfg,
        current_volume=2000000.0,
        ma20_volume=1000000.0,
        market_breadth=0.40,
    )
    assert d3.action == "HOLD_FILTERED"
    assert state3.state == PositionState.PENDING_ENTRY

    # Case D: Macro breadth thrust alone (0.75 >= 0.60) without volume surge (1.0x < 1.5x) does NOT bypass
    state4 = AssetLifecycleState(symbol="AMZN")
    d4 = step_position_lifecycle(
        state4,
        raw_target_weight=0.12,
        current_price=175.0,
        cfg=cfg,
        current_volume=1000000.0,
        ma20_volume=1000000.0,
        market_breadth=0.75,
    )
    assert d4.action == "HOLD_FILTERED"
    assert state4.state == PositionState.PENDING_ENTRY

    # Case E: Fast path disabled enforces normal guard latency confirmation even with confluence
    cfg_disabled = PositionLifecycleConfig(
        guard_latency_bars=2,
        volume_surge_multiplier=1.5,
        breadth_thrust_threshold=0.60,
        enable_entry_fast_path=False,
    )
    state5 = AssetLifecycleState(symbol="MSFT")
    d5 = step_position_lifecycle(
        state5,
        raw_target_weight=0.15,
        current_price=400.0,
        cfg=cfg_disabled,
        current_volume=3000000.0,
        ma20_volume=1000000.0,
        market_breadth=0.70,
    )
    assert d5.action == "HOLD_FILTERED"
    assert state5.state == PositionState.PENDING_ENTRY


def test_hard_stop_loss_fast_path_exit():
    """Verifies hard stop-loss and ATR trailing stops trigger immediate emergency exit."""
    cfg = PositionLifecycleConfig(
        hard_stop_loss_pct=0.08,
        atr_multiplier=4.5,
        stop_cooldown_bars=5,
        enable_exit_fast_path=True,
    )
    # Case A: Hard stop triggered by loss_pct <= -8%
    state = AssetLifecycleState(
        symbol="TSLA",
        state=PositionState.HELD_ACTIVE,
        held_bars=5,
        entry_price=200.0,
        peak_price=210.0,
        current_weight=0.20,
    )
    # Price falls to 182.0 (loss = -9% <= -8%)
    d = step_position_lifecycle(
        state,
        raw_target_weight=0.20,
        current_price=182.0,
        cfg=cfg,
    )
    assert d.action == "SELL"
    assert d.target_weight == 0.0
    assert d.fast_path_triggered == "STOP_LOSS"
    assert state.state == PositionState.COOLDOWN
    assert state.cooldown_remaining == 5
    assert state.last_exit_type == ExitType.HARD_STOP.value
    assert state.current_weight == 0.0

    # Case B: Trailing stop triggered by ATR drawdown
    state_ts = AssetLifecycleState(
        symbol="GOOGL",
        state=PositionState.HELD_ACTIVE,
        held_bars=10,
        entry_price=100.0,
        peak_price=150.0,
        current_weight=0.15,
    )
    # peak = 150.0, atr = 5.0, trailing stop = 150 - 4.5 * 5 = 127.5
    # Current price drops to 125.0 (< 127.5)
    d_ts = step_position_lifecycle(
        state_ts,
        raw_target_weight=0.15,
        current_price=125.0,
        cfg=cfg,
        atr=5.0,
    )
    assert d_ts.action == "SELL"
    assert d_ts.target_weight == 0.0
    assert d_ts.fast_path_triggered == "STOP_LOSS"
    assert state_ts.state == PositionState.COOLDOWN
    assert state_ts.cooldown_remaining == 5
    assert state_ts.last_exit_type == ExitType.TRAILING_STOP.value


def test_china_t_plus_one_gestation():
    """Verifies that China market blocks selling before min_holding_bars tenure elapses."""
    cfg = PositionLifecycleConfig(
        guard_latency_bars=1,
        min_holding_bars=2,
        hard_stop_loss_pct=0.08,
    )
    # Day 0: Buy executes in China market
    state = AssetLifecycleState(symbol="600036")
    d_buy = step_position_lifecycle(state, raw_target_weight=0.10, current_price=30.0, cfg=cfg, market="china")
    assert d_buy.action == "BUY"
    assert state.state == PositionState.HELD_GESTATION
    assert state.held_bars == 0

    # Day 1: held_bars increments to 1 (< 2). Selling must be blocked!
    d_blocked = step_position_lifecycle(state, raw_target_weight=0.0, current_price=25.0, cfg=cfg, market="china")
    assert d_blocked.action == "HOLD_FILTERED"
    assert d_blocked.target_weight == 0.10
    assert "未满最低持仓周期" in d_blocked.reason
    assert state.held_bars == 1

    # Day 2: held_bars increments to 2 (>= 2). Selling is now allowed!
    d_allowed = step_position_lifecycle(state, raw_target_weight=0.0, current_price=25.0, cfg=cfg, market="china")
    assert d_allowed.action == "SELL"
    assert d_allowed.target_weight == 0.0

    # In contrast, US market allows intraday / Day-0 selling without tenure block
    state_us = AssetLifecycleState(symbol="SPY")
    d_us_buy = step_position_lifecycle(state_us, raw_target_weight=0.10, current_price=500.0, cfg=cfg, market="us")
    assert d_us_buy.action == "BUY"
    # Immediate sell signal on bar 1 (guard latency = 1) is allowed in US
    d_us_sell = step_position_lifecycle(state_us, raw_target_weight=0.0, current_price=490.0, cfg=cfg, market="us")
    assert d_us_sell.action == "SELL"


def test_asymmetric_cooldown():
    """Verifies stop loss enforces a multi-bar protective freeze whereas normal exits unfreeze rapidly."""
    cfg = PositionLifecycleConfig(
        normal_cooldown_bars=1,
        stop_cooldown_bars=5,
        guard_latency_bars=2,
    )
    # Stop loss exit creates 5-bar cooldown
    state_stop = AssetLifecycleState(
        symbol="SYM1",
        state=PositionState.COOLDOWN,
        cooldown_remaining=5,
        last_exit_type=ExitType.HARD_STOP.value,
    )
    # Bars 1 to 4: strictly blocked (remaining: 4, 3, 2, 1)
    for expected_rem in [4, 3, 2, 1]:
        dec = step_position_lifecycle(state_stop, raw_target_weight=0.20, current_price=50.0, cfg=cfg)
        assert dec.action == "COOLDOWN_BLOCKED"
        assert dec.target_weight == 0.0
        assert state_stop.cooldown_remaining == expected_rem

    # Bar 5: cooldown expires, transitions to FLAT, begins pending entry streak
    dec_flat = step_position_lifecycle(state_stop, raw_target_weight=0.20, current_price=50.0, cfg=cfg)
    assert dec_flat.action == "HOLD_FILTERED"
    assert state_stop.state == PositionState.PENDING_ENTRY

    # Normal exit creates 1-bar cooldown
    state_norm = AssetLifecycleState(
        symbol="SYM2",
        state=PositionState.COOLDOWN,
        cooldown_remaining=1,
        last_exit_type=ExitType.NORMAL.value,
    )
    dec_norm = step_position_lifecycle(state_norm, raw_target_weight=0.20, current_price=50.0, cfg=cfg)
    # 1 - 1 = 0 -> transitions immediately to FLAT on bar 1
    assert dec_norm.action == "HOLD_FILTERED"
    assert state_norm.state == PositionState.PENDING_ENTRY


def test_inertia_deadband():
    """Verifies that rebalancing weight changes below inertia_threshold are suppressed to prevent churn."""
    cfg = PositionLifecycleConfig(
        inertia_threshold=0.05,
        min_holding_bars=1,
    )
    state = AssetLifecycleState(
        symbol="NVDA",
        state=PositionState.HELD_ACTIVE,
        held_bars=5,
        entry_price=100.0,
        peak_price=110.0,
        current_weight=0.20,
    )

    # 1. Delta = 0.03 (< 0.05) -> HOLD, weight stays 0.20
    d1 = step_position_lifecycle(state, raw_target_weight=0.23, current_price=105.0, cfg=cfg)
    assert d1.action == "HOLD"
    assert d1.target_weight == 0.20
    assert state.current_weight == 0.20

    # 2. Delta = 0.08 (>= 0.05) -> BUY, weight updates to 0.28
    d2 = step_position_lifecycle(state, raw_target_weight=0.28, current_price=105.0, cfg=cfg)
    assert d2.action == "BUY"
    assert d2.target_weight == 0.28
    assert state.current_weight == 0.28

    # 3. Delta = 0.02 (< 0.05) -> HOLD, weight stays 0.28
    d3 = step_position_lifecycle(state, raw_target_weight=0.26, current_price=105.0, cfg=cfg)
    assert d3.action == "HOLD"
    assert d3.target_weight == 0.28
    assert state.current_weight == 0.28

    # 4. Delta = 0.18 (>= 0.05) -> SELL, weight updates to 0.10
    d4 = step_position_lifecycle(state, raw_target_weight=0.10, current_price=105.0, cfg=cfg)
    assert d4.action == "SELL"
    assert d4.target_weight == 0.10
    assert state.current_weight == 0.10


def test_serialization_roundtrip():
    """Verifies JSON serialization and deserialization of AssetLifecycleState."""
    state = AssetLifecycleState(
        symbol="601318",
        state=PositionState.HELD_ACTIVE,
        held_bars=7,
        entry_streak=2,
        exit_streak=0,
        cooldown_remaining=0,
        entry_price=45.5,
        peak_price=49.8,
        current_weight=0.12,
        last_exit_type="NORMAL",
    )
    d = state.to_dict()
    assert d["symbol"] == "601318"
    assert d["state"] == "HELD_ACTIVE"
    assert d["held_bars"] == 7
    assert d["entry_price"] == 45.5
    assert d["peak_price"] == 49.8
    assert d["current_weight"] == 0.12
    assert d["last_exit_type"] == "NORMAL"

    json_str = json.dumps(d)
    restored_d = json.loads(json_str)
    restored = AssetLifecycleState.from_dict(restored_d)

    assert restored.symbol == "601318"
    assert restored.state == PositionState.HELD_ACTIVE
    assert isinstance(restored.state, PositionState)
    assert restored.held_bars == 7
    assert restored.entry_price == 45.5
    assert restored.peak_price == 49.8
    assert restored.current_weight == 0.12
    assert restored.last_exit_type == "NORMAL"


def test_allocation_templates_incremental_add_does_not_lock_mother_position():
    """Verifies that an incremental add to an existing position does not reset tenure lock on mother position."""
    # 8-bar test
    # Bar 0: BUY A with 0.20
    # Bar 1-4: HOLD A at 0.20 (holding_age reaches 5)
    # Bar 5: Incremental BUY to 0.35 (last_buy_bar becomes 5)
    # Bar 6: Strategy exits to 0.0 (holding_age = 6 >= min_holding_bars = 3)
    # Prior bug: (6 - last_buy_bar = 1 < 3) suppressed selling.
    # Fix: holding_age = 6 >= 3 allows clean exit.
    dates = pd.date_range("2025-01-01", periods=8, freq="B")
    ideal_df = pd.DataFrame(
        {
            "A": [0.20, 0.20, 0.20, 0.20, 0.20, 0.35, 0.0, 0.0],
            "CASH": [0.80, 0.80, 0.80, 0.80, 0.80, 0.65, 1.0, 1.0],
        },
        index=dates,
    )
    result = apply_asset_inertia(
        ideal_df,
        min_weight_change=0.02,
        min_holding_bars=3,
        cash_proxy="CASH",
    )
    # At Bar 5 (2025-01-08): weight increased to 0.35
    assert result.loc[dates[5], "A"] == 0.35
    # At Bar 6 (2025-01-09): weight must drop cleanly to 0.0 without lock
    assert result.loc[dates[6], "A"] == 0.0
    assert result.loc[dates[6], "CASH"] == 1.0


def test_entry_below_inertia_threshold_filtered():
    """Verifies that an entry signal with raw target weight below inertia/entry threshold is filtered."""
    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        inertia_threshold=0.05,
    )
    state = AssetLifecycleState(symbol="601899")

    # Raw target 0.043 < 0.05 default threshold -> HOLD_FILTERED, entry_streak stays 0
    d1 = step_position_lifecycle(state, raw_target_weight=0.043, current_price=10.0, cfg=cfg)
    assert d1.action == "HOLD_FILTERED"
    assert d1.target_weight == 0.0
    assert "未达最低建仓/调仓门槛" in d1.reason
    assert state.state == PositionState.FLAT
    assert state.entry_streak == 0

    # With explicit min_entry_threshold=0.03, raw target 0.043 passes gate -> PENDING_ENTRY, streak = 1
    cfg_relaxed = PositionLifecycleConfig(
        guard_latency_bars=2,
        inertia_threshold=0.05,
        min_entry_threshold=0.03,
    )
    d2 = step_position_lifecycle(state, raw_target_weight=0.043, current_price=10.0, cfg=cfg_relaxed)
    assert d2.action == "HOLD_FILTERED"
    assert "进入确认期" in d2.reason
    assert state.state == PositionState.PENDING_ENTRY
    assert state.entry_streak == 1


def test_market_closed_freezes_lifecycle():
    """Verifies that non-trading days (is_market_open=False) completely freeze all lifecycle counters."""
    cfg = PositionLifecycleConfig(guard_latency_bars=2, min_holding_bars=2, normal_cooldown_bars=2)

    # 1. FLAT state with pending signal: entry_streak does not increment
    state_flat = AssetLifecycleState(symbol="A", state=PositionState.FLAT, entry_streak=1)
    d_flat = step_position_lifecycle(
        state_flat, raw_target_weight=0.10, current_price=50.0, cfg=cfg, is_market_open=False
    )
    assert d_flat.action == "HOLD_FILTERED"
    assert d_flat.target_weight == 0.0
    assert "休市日" in d_flat.reason
    assert state_flat.entry_streak == 1
    assert state_flat.state == PositionState.FLAT

    # 2. HELD_GESTATION state: held_bars does not increment
    state_held = AssetLifecycleState(
        symbol="B",
        state=PositionState.HELD_GESTATION,
        held_bars=1,
        current_weight=0.15,
        entry_price=100.0,
        peak_price=100.0,
    )
    d_held = step_position_lifecycle(
        state_held, raw_target_weight=0.15, current_price=102.0, cfg=cfg, is_market_open=False
    )
    assert d_held.action == "HOLD_FILTERED"
    assert d_held.target_weight == 0.15
    assert state_held.held_bars == 1
    assert state_held.state == PositionState.HELD_GESTATION

    # 3. PENDING_EXIT state: exit_streak does not increment
    state_exit = AssetLifecycleState(
        symbol="C",
        state=PositionState.PENDING_EXIT,
        held_bars=2,
        exit_streak=1,
        current_weight=0.15,
        entry_price=100.0,
        peak_price=105.0,
    )
    d_exit = step_position_lifecycle(
        state_exit, raw_target_weight=0.0, current_price=101.0, cfg=cfg, is_market_open=False
    )
    assert d_exit.action == "HOLD_FILTERED"
    assert d_exit.target_weight == 0.15
    assert state_exit.exit_streak == 1
    assert state_exit.held_bars == 2
    assert state_exit.state == PositionState.PENDING_EXIT

    # 4. COOLDOWN state: cooldown_remaining does not decrement
    state_cd = AssetLifecycleState(
        symbol="D",
        state=PositionState.COOLDOWN,
        cooldown_remaining=2,
    )
    d_cd = step_position_lifecycle(
        state_cd, raw_target_weight=0.20, current_price=80.0, cfg=cfg, is_market_open=False
    )
    assert d_cd.action == "HOLD_FILTERED"
    assert d_cd.target_weight == 0.0
    assert state_cd.cooldown_remaining == 2
    assert state_cd.state == PositionState.COOLDOWN


def test_entry_resets_exit_streak():
    """Fix 3: Asserts that when transitioning to HELD_GESTATION (both confirmation and fast-path),
    exit_streak is strictly reset to 0 even if it held a non-zero residual count before.
    """
    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        min_holding_bars=1,
        volume_surge_multiplier=1.5,
        breadth_thrust_threshold=0.60,
        enable_entry_fast_path=True,
    )

    # 1. Standard 2-day entry confirmation path with residual exit_streak
    state_conf = AssetLifecycleState(
        symbol="601857.SH",
        state=PositionState.FLAT,
        exit_streak=2,  # Residual exit streak from prior liquidation
        entry_streak=0,
    )
    # Day 1: First buy signal -> PENDING_ENTRY
    d1 = step_position_lifecycle(state_conf, raw_target_weight=0.10, current_price=10.0, cfg=cfg)
    assert d1.action == "HOLD_FILTERED"
    assert state_conf.state == PositionState.PENDING_ENTRY
    assert state_conf.entry_streak == 1

    # Day 2: Second buy signal -> BUY into HELD_GESTATION
    d2 = step_position_lifecycle(state_conf, raw_target_weight=0.10, current_price=10.2, cfg=cfg)
    assert d2.action == "BUY"
    assert state_conf.state == PositionState.HELD_GESTATION
    assert state_conf.entry_streak == 2
    assert state_conf.exit_streak == 0, "exit_streak must be reset to 0 on entry confirmation"

    # 2. Fast-path volume breakout entry with residual exit_streak
    state_fast = AssetLifecycleState(
        symbol="601857.SH",
        state=PositionState.FLAT,
        exit_streak=3,  # Residual exit streak
        entry_streak=0,
    )
    d_fast = step_position_lifecycle(
        state_fast,
        raw_target_weight=0.15,
        current_price=10.5,
        cfg=cfg,
        current_volume=3000.0,
        ma20_volume=1000.0,  # 3.0x > 2.5x super surge
        market_breadth=0.50,
    )
    assert d_fast.action == "BUY"
    assert d_fast.fast_path_triggered == "VOLUME_BREAKOUT"
    assert state_fast.state == PositionState.HELD_GESTATION
    assert state_fast.exit_streak == 0, "exit_streak must be reset to 0 on fast-path entry"



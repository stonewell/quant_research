"""Tests for ZR1 zero-request semantics and Rule 11 backtest/live parity.

Covers:
1. BaseBlendAllocationStrategy _last_request_meta side-channel instrumentation on CYQ veto.
2. Rule 11 parity: simulate_position_lifecycle (backtest) vs step_position_lifecycle (live ticket)
   agreement on ENTRY_PENDING, SUB_THRESHOLD, and VETO semantics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common.allocation_templates import AllocationTemplate
from common.position_lifecycle import (
    AssetLifecycleState,
    PositionLifecycleConfig,
    PositionState,
    RequestSemantics,
    simulate_position_lifecycle,
    step_position_lifecycle,
)
from common.position_policy import PolicyAction, BlockingReason
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import GenericBlendStrategy


def _build_synthetic_bar_df(dates, base_price, daily_ret=0.001):
    n = len(dates)
    closes = [base_price * ((1.0 + daily_ret) ** i) for i in range(n)]
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    return df


class _DummySubStrategy(AllocationTemplate):
    def generate_weights(self, universe, params=None):
        dates = next(iter(universe.values())).index
        df = pd.DataFrame(index=dates)
        for s in universe:
            df[s] = 0.50 if s != "BIL" else 0.0
        return df


class _TestBlendStrategy(GenericBlendStrategy):
    def get_sub_strategies(self, universe, params=None):
        return {"sub1": _DummySubStrategy("sub1", {}, ["relative_momentum"])}

    def get_sub_strategy_weights(self, params=None):
        return {"sub1": 1.0}


def test_blend_side_channel_records_zero_request_metadata():
    """Verify BaseBlendAllocationStrategy instruments and publishes _last_request_meta on CYQ veto."""
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    strat = _TestBlendStrategy()

    # Asset A: cyq_winner_ratio = 0.10 (< 0.20) -> VETO by CYQ
    df_a = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.002)
    df_a["cyq_winner_ratio"] = 0.10
    df_a["consolidation_density"] = 0.50

    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600584.SH": df_a,
        "BIL": cash_df,
    }

    params = {
        "cash_proxy": "BIL",
        "blend_enable_cyq_quality_gate": True,
        "blend_min_cyq_winner_ratio": 0.20,
        "blend_weighting_mode": "equal",
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert hasattr(strat, "_last_request_meta")
    assert isinstance(strat._last_request_meta, dict)

    # Check that CYQ veto was recorded for 600584.SH
    found_cyq_veto = False
    for d, meta in strat._last_request_meta.items():
        if "600584.SH" in meta:
            sym_meta = meta["600584.SH"]
            if sym_meta.get("request_semantics") == "VETO" and sym_meta.get("veto_reason") == "cyq":
                assert sym_meta.get("pre_gate_weight") == pytest.approx(0.50)
                found_cyq_veto = True
                break

    assert found_cyq_veto, "Expected CYQ veto metadata in _last_request_meta for 600584.SH"


def test_rule11_backtest_live_parity_on_zr1_semantics():
    """Rule 11 parity test: simulate_position_lifecycle and step_position_lifecycle

    Asserts that backtest simulation and live-trading step produce identical verdicts,
    target weights, exit streaks, and forensic detail metadata for ENTRY_PENDING,
    SUB_THRESHOLD, and VETO requests.
    """
    symbols = ["SYM_PENDING", "SYM_SUB", "SYM_VETO"]
    dates = pd.date_range("2024-01-01", periods=3, freq="B")
    d0, d1, d2 = dates[0], dates[1], dates[2]

    # Construct synthetic universe with constant prices
    universe = {
        s: pd.DataFrame(
            {
                "Open": 10.0,
                "High": 10.5,
                "Low": 9.5,
                "Close": 10.0,
                "Volume": 100000.0,
            },
            index=dates,
        )
        for s in symbols
    }

    # Raw target weights: on d0 all have position 0.20, on d1 all have raw target 0.0
    target_weights = pd.DataFrame(
        [
            {"SYM_PENDING": 0.20, "SYM_SUB": 0.20, "SYM_VETO": 0.20},
            {"SYM_PENDING": 0.0, "SYM_SUB": 0.0, "SYM_VETO": 0.0},
            {"SYM_PENDING": 0.0, "SYM_SUB": 0.0, "SYM_VETO": 0.0},
        ],
        index=dates,
    )

    request_meta = {
        d1: {
            "SYM_PENDING": {
                "request_semantics": RequestSemantics.ENTRY_PENDING.value,
                "pre_gate_weight": 0.15,
            },
            "SYM_SUB": {
                "request_semantics": RequestSemantics.SUB_THRESHOLD.value,
                "pre_gate_weight": 0.03,
            },
            "SYM_VETO": {
                "request_semantics": RequestSemantics.VETO.value,
                "pre_gate_weight": 0.18,
                "veto_reason": "cyq",
            },
        }
    }

    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        min_holding_bars=1,
        normal_cooldown_bars=1,
    )

    initial_states = {
        sym: AssetLifecycleState(
            symbol=sym,
            state=PositionState.HELD_ACTIVE,
            held_bars=1,
            entry_streak=0,
            exit_streak=0,
            cooldown_remaining=0,
            entry_price=10.0,
            peak_price=10.0,
            current_weight=0.20,
            broker_weight=0.20,
        )
        for sym in symbols
    }

    # 1. Backtest path: simulate_position_lifecycle
    filtered_weights, records = simulate_position_lifecycle(
        universe=universe,
        target_weights=target_weights,
        cfg=cfg,
        market="us",  # allow immediate execution without T+1 china locks
        initial_states=initial_states,
        request_meta=request_meta,
    )

    d1_records = {r["symbol"]: r for r in records if r["date"] == str(d1)}

    # 2. Live ticket path: step_position_lifecycle for d1
    for sym in symbols:
        live_state = AssetLifecycleState(
            symbol=sym,
            state=PositionState.HELD_ACTIVE,
            held_bars=1,
            entry_streak=0,
            exit_streak=0,
            cooldown_remaining=0,
            entry_price=10.0,
            peak_price=10.0,
            current_weight=0.20,
            broker_weight=0.20,
        )
        sym_meta = request_meta[d1][sym]
        live_dec = step_position_lifecycle(
            state=live_state,
            raw_target_weight=0.0,
            current_price=10.0,
            cfg=cfg,
            market="us",
            request_meta=sym_meta,
        )

        sim_rec = d1_records[sym]

        # Verify 100% parity between live decision and backtest simulation record
        assert live_dec.action == sim_rec["action"], f"{sym} action mismatch"
        assert live_dec.target_weight == pytest.approx(sim_rec["target_weight"]), f"{sym} target_weight mismatch"
        assert live_state.exit_streak == sim_rec["detail_reason"]["exit_streak"], f"{sym} exit_streak mismatch"

        # Verify semantics-specific assertions
        if sym in ("SYM_PENDING", "SYM_SUB"):
            assert live_dec.action == PolicyAction.HOLD_FILTERED
            assert live_dec.target_weight == pytest.approx(0.20)  # preserved holding!
            assert live_state.exit_streak == 0  # streak reset!
            assert sim_rec["detail_reason"]["request_semantics"] == sym_meta["request_semantics"]
            assert sim_rec["detail_reason"]["pre_gate_weight"] == pytest.approx(sym_meta["pre_gate_weight"])
        elif sym == "SYM_VETO":
            assert live_dec.action == PolicyAction.HOLD_FILTERED
            assert live_dec.blocking_reason == BlockingReason.EXIT_CONFIRMING
            assert live_state.exit_streak == 1  # advanced streak!
            assert sim_rec["detail_reason"]["request_semantics"] == "VETO"
            assert sim_rec["detail_reason"]["veto_reason"] == "cyq"
            assert sim_rec["detail_reason"]["pre_gate_weight"] == pytest.approx(0.18)

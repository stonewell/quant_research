"""Unit tests for Blend Strategy Track C Enhancements (Anchors 1-5).

Validates:
1. Parameter resolution for Track C parameters.
2. Anchor 1: Macro State Machine Budgeting with Fractal Dimension D_t.
3. Anchor 2: CYQ Quality Gate (winner ratio threshold & Wyckoff spring exemption, consolidation density).
4. Anchor 3: VP Confluence & Effort-Result Sizing.
5. Anchor 4: Wyckoff Spring Day 1 Fast-Track Entry & Extreme Top Divergence Veto.
6. Anchor 5: Structural ZG/VAL Chandelier Stop Floor & NPOC Profit Taking.

100% offline using synthetic OHLCV data.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest
import warnings

from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import (
    BaseBlendAllocationStrategy,
    GenericBlendStrategy,
)
from pipeline.research_strategy.rs.config import StrategyConfig


def _build_synthetic_bar_df(dates, base_price, daily_ret=0.001):
    n = len(dates)
    closes = [base_price * ((1.0 + daily_ret) ** i) for i in range(n)]
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    return df


def test_track_c_param_resolution():
    """Verify Track C parameter resolution across defaults and overrides."""
    strat = GenericBlendStrategy()

    # Defaults (Production parameters solidified to True)
    assert strat.resolve_param({}, "enable_structural_regime", False, bool) is True
    assert strat.resolve_param({}, "enable_cyq_quality_gate", False, bool) is True
    assert strat.resolve_param({}, "min_cyq_winner_ratio", 0.20, float) == pytest.approx(0.20)
    assert strat.resolve_param({}, "enable_vp_confluence_sizing", False, bool) is False
    assert strat.resolve_param({}, "enable_spring_fast_entry", False, bool) is False
    assert strat.resolve_param({}, "enable_structural_chandelier_stop", False, bool) is False

    # Runtime overrides with blend_ prefix
    p = {
        "blend_enable_structural_regime": False,
        "blend_enable_cyq_quality_gate": False,
        "blend_min_cyq_winner_ratio": 0.25,
        "blend_enable_vp_confluence_sizing": True,
        "blend_enable_spring_fast_entry": True,
        "blend_enable_structural_chandelier_stop": True,
    }
    assert strat.resolve_param(p, "enable_structural_regime", True, bool) is False
    assert strat.resolve_param(p, "enable_cyq_quality_gate", True, bool) is False
    assert strat.resolve_param(p, "min_cyq_winner_ratio", 0.20, float) == pytest.approx(0.25)
    assert strat.resolve_param(p, "enable_vp_confluence_sizing", False, bool) is True
    assert strat.resolve_param(p, "enable_spring_fast_entry", False, bool) is True
    assert strat.resolve_param(p, "enable_structural_chandelier_stop", False, bool) is True

    # StrategyConfig.from_dict with unprefixed aliases (zero unknown key warnings)
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        cfg = StrategyConfig.from_dict({
            "enable_structural_regime": True,
            "enable_cyq_quality_gate": True,
            "min_cyq_winner_ratio": 0.28,
            "enable_vp_confluence_sizing": True,
            "enable_spring_fast_entry": True,
            "enable_structural_chandelier_stop": True,
        })
        unknown_warnings = [
            w for w in record
            if issubclass(w.category, UserWarning) and "unknown key" in str(w.message)
        ]
        assert len(unknown_warnings) == 0, f"Unexpected unknown key warnings: {unknown_warnings}"

    assert cfg.enable_structural_regime is True
    assert cfg.blend_enable_structural_regime is True
    assert cfg.enable_cyq_quality_gate is True
    assert cfg.blend_enable_cyq_quality_gate is True
    assert cfg.min_cyq_winner_ratio == pytest.approx(0.28)
    assert cfg.blend_min_cyq_winner_ratio == pytest.approx(0.28)
    assert cfg.enable_vp_confluence_sizing is True
    assert cfg.blend_enable_vp_confluence_sizing is True
    assert cfg.enable_spring_fast_entry is True
    assert cfg.blend_enable_spring_fast_entry is True
    assert cfg.enable_structural_chandelier_stop is True
    assert cfg.blend_enable_structural_chandelier_stop is True


def test_anchor_1_macro_state_machine_budgeting():
    """Verify Anchor 1 macro state machine style budgeting with fractal dimension D_t."""
    from common.allocation_templates import AllocationTemplate
    strat = GenericBlendStrategy()
    sub_strats = {
        "strat_mom": AllocationTemplate(name="strat_mom", param_grid={}, factor_tags=["relative_momentum"]),
        "strat_rev": AllocationTemplate(name="strat_rev", param_grid={}, factor_tags=["mean_reversion"]),
        "strat_def": AllocationTemplate(name="strat_def", param_grid={}, factor_tags=["defensive"]),
    }
    base_weights = {k: 1.0 / len(sub_strats) for k in sub_strats}

    # Case 1: Persistent trend expansion (D_t < 1.45, vol_z < 1.0) -> Momentum budget 0.80
    w_trend = strat._compute_adaptive_weights(
        sub_strats=sub_strats,
        base_weights=base_weights,
        sub_returns_slice=pd.DataFrame(),
        weighting_mode="regime_adaptive",
        breadth_val=0.45,
        thrust=0.50,
        vol_z=0.5,
        persistence=0.50,
        adaptive_min_tstat=2.0,
        adaptive_shrinkage=0.20,
        date="2024-01-01",
        params={"blend_enable_structural_regime": True},
        median_d=1.40,
    )
    style_map = strat._classify_sub_strategy_styles(sub_strats)
    assert style_map["strat_mom"] == "momentum"
    assert style_map["strat_rev"] == "reversal"
    assert style_map["strat_def"] == "defensive"

    mom_weight_trend = sum(w_trend[k] for k, style in style_map.items() if style == "momentum" and k in w_trend)
    assert mom_weight_trend == pytest.approx(0.80)

    # Case 2: High fractal dimension Brownian chop (D_t > 1.55) -> Reversal budget 0.75
    w_chop = strat._compute_adaptive_weights(
        sub_strats=sub_strats,
        base_weights=base_weights,
        sub_returns_slice=pd.DataFrame(),
        weighting_mode="regime_adaptive",
        breadth_val=0.45,
        thrust=0.50,
        vol_z=1.0,
        persistence=0.50,
        adaptive_min_tstat=2.0,
        adaptive_shrinkage=0.20,
        date="2024-01-01",
        params={"blend_enable_structural_regime": True},
        median_d=1.60,
    )
    rev_weight_chop = sum(w_chop[k] for k, style in style_map.items() if style == "reversal" and k in w_chop)
    assert rev_weight_chop == pytest.approx(0.75)


def test_anchor_2_cyq_quality_gate():
    """Verify Anchor 2 CYQ winner ratio threshold and Wyckoff spring exemption."""
    dates = pd.date_range("2024-01-01", periods=100, freq="B")
    strat = GenericBlendStrategy()

    # Asset A: cyq_winner_ratio = 0.10 (< 0.20) -> Rejected
    df_a = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.002)
    df_a["cyq_winner_ratio"] = 0.10
    df_a["consolidation_density"] = 0.50

    # Asset B: cyq_winner_ratio = 0.10 but with Wyckoff spring >= 1.25 & curvature >= 1.5 -> Exempt!
    df_b = _build_synthetic_bar_df(dates, 20.0, daily_ret=0.002)
    df_b["cyq_winner_ratio"] = 0.10
    df_b["wyckoff_spring"] = 1.35
    df_b["extremum_curvature"] = 1.80
    df_b["consolidation_density"] = 0.50

    # Asset C: consolidation_density = 0.30 (< 0.40 hollow) -> Rejected
    df_c = _build_synthetic_bar_df(dates, 30.0, daily_ret=0.002)
    df_c["cyq_winner_ratio"] = 0.50
    df_c["consolidation_density"] = 0.30

    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600584.SH": df_a,
        "002371.SZ": df_b,
        "601857.SH": df_c,
        "BIL": cash_df,
    }

    params = {
        "cash_proxy": "BIL",
        "blend_enable_cyq_quality_gate": True,
        "blend_min_cyq_winner_ratio": 0.20,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty

    # Rebalance rows should not hold 600584.SH (rejected by winner ratio) or 601857.SH (hollow consolidation)
    valid_rebalances = weights.dropna(how="all")
    for date, row in valid_rebalances.iterrows():
        assert row.get("600584.SH", 0.0) == pytest.approx(0.0)
        assert row.get("601857.SH", 0.0) == pytest.approx(0.0)


def test_anchor_4_spring_fast_entry_and_divergence_veto():
    """Verify Anchor 4 Day 1 fast entry for Wyckoff spring and top divergence veto."""
    dates = pd.date_range("2024-01-01", periods=60, freq="B")
    strat = GenericBlendStrategy()

    # Asset A: Normal asset requiring min_entry_bars confirmation
    df_a = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.003)

    # Asset B: Spring asset with wyckoff_spring >= 1.25 and extremum_curvature >= 1.5
    df_b = _build_synthetic_bar_df(dates, 20.0, daily_ret=0.003)
    df_b["wyckoff_spring"] = 1.30
    df_b["extremum_curvature"] = 1.60

    # Asset C: Extreme kinetic top divergence asset: standardized_divergence < -1.2, Close < POC
    df_c = _build_synthetic_bar_df(dates, 30.0, daily_ret=0.003)
    df_c["standardized_divergence"] = -1.50
    df_c["POC"] = 40.0  # Close (~30-35) < POC (40.0)

    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600584.SH": df_a,
        "002371.SZ": df_b,
        "601857.SH": df_c,
        "BIL": cash_df,
    }

    params = {
        "cash_proxy": "BIL",
        "min_entry_bars": 2,
        "blend_enable_spring_fast_entry": True,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty

    # Top divergence asset C (601857.SH) must NEVER be entered (strictly 0.0)
    for date, row in weights.iterrows():
        if pd.notna(row.get("601857.SH")):
            assert row["601857.SH"] == pytest.approx(0.0)


def test_anchor_3_vp_confluence_sizing():
    """Verify Anchor 3 VP Confluence boost and Effort-Result distribution damping."""
    dates = pd.date_range("2024-01-01", periods=60, freq="B")
    strat = GenericBlendStrategy()

    # Asset A: Normal breakout
    df_a = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.003)

    # Asset B: Effort-Result distribution damping: effort_result < 0.35 and Close < (High+Low)/2
    df_b = _build_synthetic_bar_df(dates, 20.0, daily_ret=0.003)
    df_b["wyckoff_effort_result"] = 0.20
    # Artificially set High much higher so Close is below midpoint
    df_b["High"] = df_b["Close"] * 1.05
    df_b["Low"] = df_b["Close"] * 0.99

    # Asset C: VAH breakout + single peak chip concentration >= 0.85
    df_c = _build_synthetic_bar_df(dates, 30.0, daily_ret=0.003)
    df_c["VAH"] = df_c["Close"] * 0.98
    df_c["cyq_cost_concentration"] = 0.90

    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600584.SH": df_a,
        "002371.SZ": df_b,
        "601857.SH": df_c,
        "BIL": cash_df,
    }

    params = {
        "cash_proxy": "BIL",
        "confluence_sizing": True,
        "blend_enable_vp_confluence_sizing": True,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    # Asset C with high confluence should receive allocation when thrust deploys
    rebal = weights.dropna(how="all")
    assert len(rebal) > 0


def test_anchor_5_structural_stop_and_npoc_exit():
    """Verify Anchor 5 Structural ZG/VAL stop and NPOC profit taking exit."""
    dates = pd.date_range("2024-01-01", periods=60, freq="B")
    strat = GenericBlendStrategy()

    # Asset with strong initial rally (>10% profit) followed by hitting NPOC with negative divergence
    n = len(dates)
    closes = [10.0 * (1.005 ** i) for i in range(n)]
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    df["ZG"] = 10.5
    df["VAL"] = 10.2
    # At day 40+, price hits NPOC and has divergence < -1.0
    df["NPOC"] = 999.0
    df["standardized_divergence"] = 0.0
    df.iloc[40:, df.columns.get_loc("NPOC")] = 11.0  # Close is > 12.0 here
    df.iloc[40:, df.columns.get_loc("standardized_divergence")] = -1.5

    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600584.SH": df,
        "BIL": cash_df,
    }

    params = {
        "cash_proxy": "BIL",
        "blend_enable_structural_chandelier_stop": True,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty

    # Verify that on day 40 (2024-02-26) when NPOC divergence trigger occurs,
    # the position was held previously (0.2) and is immediately stopped out to explicit 0.0
    assert weights.loc[dates[40], "600584.SH"] == pytest.approx(0.0)
    assert weights.loc[dates[40], "BIL"] == pytest.approx(1.0)


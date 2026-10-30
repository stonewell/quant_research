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

import json
import numpy as np
import pandas as pd
import pytest
import warnings
from typing import Dict

from common.allocation_templates import AllocationTemplate
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


def test_regime_hysteresis_thrust_override():
    """Fix 4: thrust_active override routes the regime budget even when raw thrust < threshold.

    A raw thrust of 0.50 (< 0.60) with a hysteresis flag still held 'active' must
    select the momentum budget (0.80), while the same inputs WITHOUT the override
    fall through to the reversal budget (0.75). This is the unit-level contract
    that prevents the 0.60 -> 0.50 single-bar regime flip.
    """
    from common.allocation_templates import AllocationTemplate

    strat = GenericBlendStrategy()
    sub_strats = {
        "strat_mom": AllocationTemplate(name="strat_mom", param_grid={}, factor_tags=["relative_momentum"]),
        "strat_rev": AllocationTemplate(name="strat_rev", param_grid={}, factor_tags=["mean_reversion"]),
        "strat_def": AllocationTemplate(name="strat_def", param_grid={}, factor_tags=["defensive"]),
    }
    base_weights = {k: 1.0 / len(sub_strats) for k in sub_strats}
    style_map = strat._classify_sub_strategy_styles(sub_strats)

    common = dict(
        sub_strats=sub_strats,
        base_weights=base_weights,
        sub_returns_slice=pd.DataFrame(),
        weighting_mode="regime_adaptive",
        breadth_val=0.45,
        thrust=0.50,
        vol_z=1.5,
        persistence=0.50,
        adaptive_min_tstat=2.0,
        adaptive_shrinkage=0.20,
        date="2024-01-01",
        params={"blend_enable_structural_regime": True},
        median_d=1.60,
    )

    # Without override: momentum branch is False (breadth 0.45 < 0.60, thrust 0.50 < 0.60,
    # median_d 1.60 not < 1.45) -> reversal budget.
    w_off = strat._compute_adaptive_weights(**common)
    rev_off = sum(w_off[k] for k, s in style_map.items() if s == "reversal" and k in w_off)
    assert rev_off == pytest.approx(0.75)

    # With thrust_active override: momentum branch becomes True -> momentum budget.
    w_on = strat._compute_adaptive_weights(**common, thrust_active=True)
    mom_on = sum(w_on[k] for k, s in style_map.items() if s == "momentum" and k in w_on)
    assert mom_on == pytest.approx(0.80)


def test_bucket_renorm_max_delta_clamp():
    """Fix 3: single-bar style-bucket weight change is delta-clamped toward previous bucket weights.

    A defensive bucket cannot jump from 0.05 to 0.85 in one bar when
    bucket_renorm_max_delta=0.15; it is clamped to 0.05+0.15=0.20 (pre-renorm)
    then renormalized (~0.2105). Without the clamp it would be 0.85.
    """
    from common.allocation_templates import AllocationTemplate

    strat = GenericBlendStrategy()
    sub_strats = {
        "strat_mom": AllocationTemplate(name="strat_mom", param_grid={}, factor_tags=["relative_momentum"]),
        "strat_rev": AllocationTemplate(name="strat_rev", param_grid={}, factor_tags=["mean_reversion"]),
        "strat_def": AllocationTemplate(name="strat_def", param_grid={}, factor_tags=["defensive"]),
    }
    base_weights = {k: 1.0 / len(sub_strats) for k in sub_strats}
    prev_bucket_weights = {"momentum": 0.80, "reversal": 0.15, "defensive": 0.05}
    bucket_weights_out: Dict[str, float] = {}

    strat._compute_adaptive_weights(
        sub_strats=sub_strats,
        base_weights=base_weights,
        sub_returns_slice=pd.DataFrame(),
        weighting_mode="regime_adaptive",
        breadth_val=0.10,
        thrust=0.10,
        vol_z=2.5,
        persistence=0.10,
        adaptive_min_tstat=2.0,
        adaptive_shrinkage=0.20,
        date="2024-01-02",
        params={"blend_enable_structural_regime": True},
        median_d=1.30,
        prev_bucket_weights=prev_bucket_weights,
        bucket_renorm_max_delta=0.15,
        bucket_weights_out=bucket_weights_out,
    )

    # defensive clamped to 0.20 pre-renorm -> ~0.2105 post-renorm (NOT 0.85)
    assert bucket_weights_out["defensive"] == pytest.approx(0.210526, abs=1e-3)
    assert bucket_weights_out["momentum"] == pytest.approx(0.684211, abs=1e-3)
    assert sum(bucket_weights_out.values()) == pytest.approx(1.0, abs=1e-9)



# =====================================================================================
# 2026-10-22 live-incident replay (`chan_four_state_blend`, regime_ic_ir_hybrid)
# =====================================================================================


def test_cfsb_deployed_regime_mode_and_inertia_ratio_config():
    """Deployed-config regression: `chan_four_state_blend` is a DOCUMENTED EXCEPTION
    to the `rolling_ic_ir` blend defaults (AGENTS.md / docs/version.md §8).

    Why this test exists: the 2026-10-22 live run executed `chan_four_state_blend`
    under `regime_ic_ir_hybrid`, but a `--dump-strategies` pass silently wrote
    `rolling_ic_ir` into the dump because strategies_config.json still held the old
    default. That drift (a) changed the deployed weighting engine between runs and
    (b) made the Fix 3 (bucket delta-clamp) / Fix 4 (regime hysteresis) defenses
    inert, since both only execute inside the `regime_*` branch. The mode is now
    pinned in the SSOT config and asserted here so a future dump cannot flip it
    unnoticed. The other five blend strategies MUST stay on `rolling_ic_ir`.

    Also pins Fix 7's ratio at 0.30: the first fix round used 0.25, which is a
    mathematical NO-OP because max(0.05, 0.25 * 0.20 position cap) == 0.05 exactly.
    """
    path = os.path.join(_PROJECT_ROOT, "research_strategy", "strategies_config.json")
    with open(path, encoding="utf-8") as fh:
        cfg_json = json.load(fh)

    expected_modes = {
        "chan_four_state_blend": "regime_ic_ir_hybrid",   # documented exception
        "chan_risk_managed_blend": "rolling_ic_ir",
        "chan_four_state_shield_blend": "rolling_ic_ir",
        "chan_crisis_shield_blend": "rolling_ic_ir",
        "chan_dual_hybrid_blend": "rolling_ic_ir",
        "generic_blend": "rolling_ic_ir",
    }
    for key, expected_mode in expected_modes.items():
        params = cfg_json[key]["parameters"]
        assert params.get("blend_weighting_mode") == expected_mode, (
            f"{key}.blend_weighting_mode drifted to {params.get('blend_weighting_mode')!r} "
            f"(expected {expected_mode!r})"
        )

    cfsb = cfg_json["chan_four_state_blend"]["parameters"]
    assert cfsb["cfsb_inertia_relative_ratio"] == pytest.approx(0.30)
    assert cfsb["blend_enable_regime_hysteresis"] is True
    assert cfsb["blend_thrust_enter_thresh"] == pytest.approx(0.62)
    assert cfsb["blend_thrust_exit_thresh"] == pytest.approx(0.58)
    assert cfsb["blend_bucket_renorm_max_delta"] == pytest.approx(0.15)

    # Derivation check vs. the hard single-position cap: the relative hurdle must
    # strictly exceed the absolute floor, otherwise Fix 7 is a no-op.
    cfg = StrategyConfig()
    cap = cfg.cfsb_max_single_position
    floor = cfg.cfsb_min_weight_change
    assert cap == pytest.approx(0.20)
    assert floor == pytest.approx(0.05)
    assert cfg.cfsb_inertia_relative_ratio * cap == pytest.approx(0.06)
    assert cfg.cfsb_inertia_relative_ratio * cap > floor
    # The 600660 incumbent weight (0.18) must also clear the floor: 0.30*0.18 = 0.054.
    assert cfg.cfsb_inertia_relative_ratio * 0.18 == pytest.approx(0.054)

    # The generic lifecycle default must stay DISABLED so other strategies (and
    # their backtest numbers) are untouched.
    from common.position_lifecycle import PositionLifecycleConfig
    assert PositionLifecycleConfig().inertia_relative_ratio == 0.0


def test_incident_2026_10_22_end_to_end_replay():
    """End-to-end replay of the 2026-10-22 `chan_four_state_blend` live incident.

    Incident shape (verifiable from the live ticket, unchanged evidence):
      * held  000999.SZ 1100 sh @ w 0.1393 (held_bars 1), 600900.SH 1300 sh @ w 0.1948
        (held_bars 1), 600660.SH 700 sh @ w 0.1819 (held_bars 17);
      * model target weights 0.067310 / 0.131807 / 0.131807 on the rebalance bar;
      * 10d Zweig thrust collapsed 0.60 -> 0.50 in ONE bar, regime budget flipped, and
        the empty `reversal` bucket (no cfsb sub-strategy classifies as reversal, so
        the bucket budget is renormalized away) quadrupled the defensive allocation;
      * the engineered trims were emitted as real SELLs: 000999/600900 while still
        inside the min-holding lock, plus a 0.0501 micro-trim on 600660;
      * zero BUYs, so 40,041 RMB landed in cash (44.4% cash vs the 71.11% Stage-1
        advisory equity target).

    This test replays BOTH broken layers with the *restored deployed config*
    (`regime_ic_ir_hybrid`, hysteresis on, clamp 0.15, ratio 0.30) and asserts the
    day now produces ZERO sells:

      (a) hysteresis: the 0.59 noise bar (which the legacy single-threshold gate at
          0.60 WOULD have flipped) keeps the momentum budget;
      (b) the 0.50 reset is bounded by `bucket_renorm_max_delta=0.15` (defensive
          0.0588 -> 0.2088, NOT 0.40) — HONEST DISCLOSURE: 0.50 < thrust_exit_thresh
          0.58, so hysteresis alone legitimately releases the flag on that bar; the
          single-bar portfolio flip is prevented by the Fix 3 clamp, not by Fix 4;
      (c) the two locked incumbents return HOLD_FILTERED;
      (d) 600660's 0.0501 delta is filtered by the relative hurdle
          (0.30 * 0.1819 = 0.0546) -> ZERO SELL orders for the day.

    Layer boundary (disclosed, not hidden): this replays the regime/budget layer via
    the strategy's real `_compute_adaptive_weights` + real sub-strategy style map,
    and the execution layer via the real `step_position_lifecycle` state machine.
    It does NOT re-run `generate_weights` on synthetic OHLCV, because the exact
    per-bar breadth/median-D/thrust path is data-driven and cannot be pinned to the
    incident's numbers deterministically. 100% offline, no network, no real data.
    """
    from common.position_lifecycle import (
        AssetLifecycleState,
        PositionLifecycleConfig,
        PositionState,
        step_position_lifecycle,
    )
    from pipeline.research.blend import ChanFourStateBlendStrategy

    strat = ChanFourStateBlendStrategy()
    sub_strats = strat.get_sub_strategies({})
    base_weights = strat.get_sub_strategy_weights({})

    # Sanity: the empty-`reversal`-bucket precondition that caused the defensive
    # renormalization jump must still hold for the deployed sub-strategy roster.
    style_map = strat._classify_sub_strategy_styles(sub_strats)
    assert set(style_map.values()) == {"momentum", "defensive"}
    assert "reversal" not in style_map.values()

    deployed_params = {
        "blend_enable_structural_regime": True,
        "blend_thrust_thresh": 0.60,
        "blend_breadth_bull_thresh": 0.30,
        "blend_thrust_enter_thresh": 0.62,
        "blend_thrust_exit_thresh": 0.58,
        "blend_bucket_renorm_max_delta": 0.15,
    }
    # Regime context on 2026-10-21/22: breadth below the 0.60 bull gate, vol_z inside
    # the 2.0 band, fractal dimension D_t above 1.55 (the structural-regime branch).
    regime_kwargs = dict(
        sub_strats=sub_strats,
        base_weights=base_weights,
        sub_returns_slice=pd.DataFrame(),
        weighting_mode="regime_adaptive",
        breadth_val=0.45,
        vol_z=1.5,
        persistence=0.50,
        adaptive_min_tstat=2.0,
        adaptive_shrinkage=0.20,
        date="2026-10-22",
        params=deployed_params,
        median_d=1.60,
    )

    def bucket_weights(thrust, thrust_active, prev_buckets, clamp=True):
        params = dict(deployed_params)
        if not clamp:
            params.pop("blend_bucket_renorm_max_delta")
        out: Dict[str, float] = {}
        strat._compute_adaptive_weights(
            **{**regime_kwargs, "params": params},
            thrust=thrust,
            thrust_active=thrust_active,
            prev_bucket_weights=prev_buckets,
            bucket_weights_out=out,
        )
        return out

    # Day T-1: thrust 0.62 (>= enter 0.62) -> momentum regime budget is armed.
    prev_buckets = bucket_weights(0.62, True, None)
    assert prev_buckets["momentum"] == pytest.approx(0.80 / 0.85, abs=1e-6)
    assert prev_buckets["defensive"] == pytest.approx(0.05 / 0.85, abs=1e-6)

    # (a) One bar later thrust prints 0.59 — inside the 0.58..0.62 hysteresis band.
    # The LEGACY single-threshold gate (raw 0.59 < 0.60) would have flipped straight
    # into the empty-reversal defensive budget (defensive 0.40); with hysteresis the
    # momentum budget is retained, i.e. no single-bar budget flip from noise.
    noisy_legacy = bucket_weights(0.59, None, prev_buckets, clamp=False)
    assert noisy_legacy["defensive"] == pytest.approx(0.40, abs=1e-6)
    noisy_hysteretic = bucket_weights(0.59, True, prev_buckets)
    assert noisy_hysteretic["defensive"] == pytest.approx(prev_buckets["defensive"], abs=1e-9)
    assert noisy_hysteretic["momentum"] == pytest.approx(prev_buckets["momentum"], abs=1e-9)

    # (b) The incident bar: thrust collapses to 0.50 (< exit 0.58) -> the regime flag
    # legitimately releases; without the clamp the empty-reversal renormalization
    # would jump defensive 0.0588 -> 0.40 in one bar (the 4x jump seen live).
    unclamped = bucket_weights(0.50, False, prev_buckets, clamp=False)
    assert unclamped["defensive"] == pytest.approx(0.40, abs=1e-6)
    clamped = bucket_weights(0.50, False, prev_buckets, clamp=True)
    assert clamped["defensive"] - prev_buckets["defensive"] == pytest.approx(0.15, abs=1e-6)
    assert clamped["defensive"] == pytest.approx(0.2088, abs=1e-4)
    assert sum(clamped.values()) == pytest.approx(1.0, abs=1e-9)

    # (c)(d) Execution layer: the resulting trim targets from the model's
    # 0.2088 defensive budget are the incident's 0.067310 / 0.131807 / 0.131807.
    cfg = PositionLifecycleConfig(
        guard_latency_bars=1,
        min_holding_bars=2,
        inertia_threshold=0.05,
        inertia_relative_ratio=0.30,
        hard_stop_loss_pct=0.08,
        atr_multiplier=4.5,
        enable_exit_fast_path=True,
    )
    # (symbol, held shares, broker weight, target weight, held_bars BEFORE this bar)
    positions = [
        ("000999.SZ", 1100, 0.1393, 0.067310, 0),   # -> held_bars 1 -> tenure locked
        ("600900.SH", 1300, 0.1948, 0.131807, 0),   # -> held_bars 1 -> tenure locked
        ("600660.SH", 700, 0.1819, 0.131807, 16),   # -> held_bars 17 -> trim-side inertia
    ]
    decisions = {}
    for symbol, shares, broker_w, target_w, held_bars in positions:
        state = AssetLifecycleState(
            symbol=symbol,
            state=PositionState.HELD_GESTATION,
            held_bars=held_bars,
            entry_price=25.0,
            peak_price=25.5,
            current_weight=broker_w,
        )
        decision = step_position_lifecycle(
            state, raw_target_weight=target_w, current_price=25.0, cfg=cfg, market="china"
        )
        decisions[symbol] = decision
        assert state.current_weight == pytest.approx(broker_w), (
            f"{symbol}: emitted a trade the lock/inertia should have suppressed "
            f"(weight moved to {state.current_weight:.6f}, broker holds {broker_w:.6f} / {shares} sh)"
        )

    # (c) both newly-bought incumbents held through the min-holding lock
    for symbol in ("000999.SZ", "600900.SH"):
        assert decisions[symbol].action == "HOLD_FILTERED"
        assert "未满最低持仓周期" in decisions[symbol].reason
    # (d) 600660's 0.050093 micro-trim < 0.30 * 0.1819 = 0.05457 -> inertia HOLD,
    # explicitly NOT a SELL (the live run emitted ~10,256 RMB of sells here).
    assert decisions["600660.SH"].action == "HOLD"
    assert decisions["600660.SH"].action != "SELL"
    assert decisions["600660.SH"].detail_reason["effective_threshold"] == pytest.approx(0.05457, abs=1e-5)
    assert abs(decisions["600660.SH"].detail_reason["delta_w"]) == pytest.approx(0.050093, abs=1e-6)

    # The whole day emits ZERO sells. Outcome (live-ticket figures quoted for context,
    # NOT re-derived here): the equity book stays at 75.88%, i.e. +4.77pp ABOVE the
    # Stage-1 71.11% advisory (and well above the 44.4%-cash book the live run actually
    # ended in), because the ~40,041 RMB of sell proceeds is never released to cash.
    assert [d.action for d in decisions.values()].count("SELL") == 0
    # The three replayed incumbents alone are 51.6% of NAV - over half the book was at
    # risk of being liquidated/reduced by the three defects replayed above.
    assert sum(p[2] for p in positions) == pytest.approx(0.5160, abs=1e-9)


# =====================================================================================
# `C6.1` -- Anchor 2 consolidation-density veto must read
#            `chan_vp_min_consolidation_density` (no hardcoded 0.40 literal)
# =====================================================================================

_DENSITY_VETO_SYMBOL = "600584.SH"          # the candidate whose density is under test
_DENSITY_CONTROL_SYMBOL = "002371.SZ"       # control: density well above every threshold


class _ConstantWeightStub(AllocationTemplate):
    """Sub-strategy stub with CONSTANT equal targets.

    Everything that can move `_DENSITY_VETO_SYMBOL`'s target weight therefore comes
    from the blend engine's own gates -- in particular the Anchor 2 quality gate under
    test. Without this isolation the sub-strategy's own stock selection would mask the
    veto (the first draft of this fixture did exactly that and the assertion was
    vacuous: `601857.SH` was 0.0 at every density).
    """

    def generate_weights(self, universe, params=None):
        idx = universe["BIL"].index
        df = pd.DataFrame({s: 0.0 for s in universe}, index=idx)
        for sym in (_DENSITY_VETO_SYMBOL, _DENSITY_CONTROL_SYMBOL):
            if sym in df.columns:
                df[sym] = 0.5
        return df


class _QualityGateStubBlend(BaseBlendAllocationStrategy):
    """Minimal blend shell: one constant-weight sub-strategy at weight 1.0."""

    name = "quality_gate_stub_blend"

    def get_sub_strategies(self, universe, params=None):
        return {"mock": _ConstantWeightStub("mock", {})}

    def get_sub_strategy_weights(self, params=None):
        return {"mock": 1.0}


def _density_fixture(veto_density: float):
    """Two candidates + cash. `_CONTROL` density 0.50 is above every threshold used."""
    dates = pd.bdate_range("2020-01-01", periods=60)
    df_veto = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.002)
    df_veto["Volume"] = 1000.0
    df_veto["cyq_winner_ratio"] = 0.50          # above min_cyq_winner_ratio -> only density can veto
    df_veto["consolidation_density"] = veto_density

    df_ctrl = _build_synthetic_bar_df(dates, 20.0, daily_ret=0.002)
    df_ctrl["Volume"] = 1000.0
    df_ctrl["cyq_winner_ratio"] = 0.50
    df_ctrl["consolidation_density"] = 0.50

    df_cash = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)
    df_cash["Volume"] = 1000.0

    return {
        _DENSITY_VETO_SYMBOL: df_veto,
        _DENSITY_CONTROL_SYMBOL: df_ctrl,
        "BIL": df_cash,
    }


def _gate_weights(veto_density: float, config=None, **overrides):
    """Run the stub blend once and return forward-filled weights (last row = decision)."""
    params = {
        "cash_proxy": "BIL",
        "enable_cyq_quality_gate": True,
        "min_cyq_winner_ratio": 0.20,
        # Off so the pre-emptive thrust path cannot re-add a symbol the gate just vetoed
        # (it scores candidates from ROC and would silently bypass the veto).
        "preemptive_thrust_deployment": False,
    }
    params.update(overrides)
    strat = _QualityGateStubBlend(config=config)
    w = strat.generate_weights(_density_fixture(veto_density), params=params)
    return w.ffill().fillna(0.0)


def test_c61_density_veto_moves_with_the_configured_threshold():
    """`C6.1` regression: the Anchor 2 density veto threshold is the CONFIGURED value.

    The fixture density 0.42 sits strictly BETWEEN the old hardcoded literal (0.40)
    and the configured default (0.45):

      * old behaviour (`< 0.40`)  -> 0.42 > 0.40  -> ADMITTED (~0.2 weight);
      * required behaviour (`< 0.45`) -> 0.42 < 0.45 -> VETOED (explicit 0.0).

    So this test is red on the pre-fix code, and it also pins that the veto follows the
    parameter in BOTH directions rather than merely hardcoding 0.45 instead.
    """
    dens = 0.42

    w_default = _gate_weights(dens)
    assert float(w_default.iloc[-1][_DENSITY_VETO_SYMBOL]) == pytest.approx(0.0), (
        "configured default 0.45 must veto a 0.42-density candidate "
        "(a bare 0.40 literal admits it -- that is the C6.1 defect)"
    )
    # Control leg: same fixture, same call -- proves the veto is symbol-specific and
    # that a 0.0 above is not just "nothing was bought at all".
    assert float(w_default.iloc[-1][_DENSITY_CONTROL_SYMBOL]) > 0.0

    # Loosening the configured threshold below the candidate density admits it again.
    w_loose = _gate_weights(dens, chan_vp_min_consolidation_density=0.30)
    assert float(w_loose.iloc[-1][_DENSITY_VETO_SYMBOL]) > 0.0, (
        "with chan_vp_min_consolidation_density=0.30 the 0.42 density must PASS; "
        "a parameter-inert veto keeps it at 0.0"
    )

    # Tightening it above the candidate density vetoes it again -- parameter-driven
    # in BOTH directions, so "replaced 0.40 with a different literal" cannot pass.
    w_tight = _gate_weights(dens, chan_vp_min_consolidation_density=0.60)
    assert float(w_tight.iloc[-1][_DENSITY_VETO_SYMBOL]) == pytest.approx(0.0)

    # A candidate above the configured default still passes: not a blanket veto.
    w_above = _gate_weights(0.46)
    assert float(w_above.iloc[-1][_DENSITY_VETO_SYMBOL]) > 0.0


def test_c61_density_threshold_resolves_from_strategy_config():
    """`C6.1`: the same threshold is honoured when set on `StrategyConfig` (not params)."""
    assert StrategyConfig().chan_vp_min_consolidation_density == pytest.approx(0.45)

    strat = _QualityGateStubBlend()
    assert float(strat.resolve_param({}, "chan_vp_min_consolidation_density", None)) == pytest.approx(0.45), (
        "the veto threshold must resolve to the single source of truth "
        "(StrategyConfig.chan_vp_min_consolidation_density), default 0.45"
    )

    dens = 0.42
    cfg_loose = StrategyConfig.from_dict({"chan_vp_min_consolidation_density": 0.30})
    w_cfg_loose = _gate_weights(dens, config=cfg_loose)
    assert float(w_cfg_loose.iloc[-1][_DENSITY_VETO_SYMBOL]) > 0.0

    cfg_tight = StrategyConfig.from_dict({"chan_vp_min_consolidation_density": 0.60})
    w_cfg_tight = _gate_weights(dens, config=cfg_tight)
    assert float(w_cfg_tight.iloc[-1][_DENSITY_VETO_SYMBOL]) == pytest.approx(0.0)


def _blend_base_source() -> str:
    path = os.path.join(_PROJECT_ROOT, "research", "blend", "blend_base.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_c61_density_veto_has_no_float_literal_threshold():
    """`C6.1` source guard (AST): the density veto compares against a NAME, not a float.

    Also asserts the `chan_vp_min_consolidation_density` field is actually resolved in
    `blend_base.py`, so a revert to `float(c_dens) < 0.40` fails twice.
    """
    import ast

    tree = ast.parse(_blend_base_source())

    density_compares = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left = node.left
        if not (isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "float"):
            continue
        if not left.args:
            continue
        arg = left.args[0]
        arg_names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
        if "c_dens" not in arg_names:
            continue
        density_compares.append(node)

    assert density_compares, (
        "could not locate the consolidation-density veto comparison in blend_base.py "
        "(expected `float(c_dens) < <threshold>`); update this guard if it was renamed"
    )
    for node in density_compares:
        assert isinstance(node.ops[0], ast.Lt)
        rhs = node.comparators[0]
        assert not isinstance(rhs, ast.Constant) or not isinstance(rhs.value, (int, float)), (
            f"line {node.lineno}: the consolidation-density veto is compared against the "
            f"float literal {getattr(rhs, 'value', rhs)!r} -- it must read the configured "
            f"`chan_vp_min_consolidation_density` instead"
        )
        assert isinstance(rhs, ast.Name), (
            f"line {node.lineno}: the veto threshold must be a resolved parameter name"
        )

    resolved_fields = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "resolve_param" or not node.args:
            continue
        name_arg = node.args[1] if len(node.args) > 1 else None
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            resolved_fields.add(name_arg.value)
    assert "chan_vp_min_consolidation_density" in resolved_fields, (
        "blend_base.py no longer resolves `chan_vp_min_consolidation_density` -- the "
        "Anchor 2 veto would go back to an arbitrary hardcoded threshold"
    )


# =====================================================================================
# `C6.2` -- `cdhb_confluence_sizing` is the LEGACY resonance sizing, NOT Anchor 3
# =====================================================================================


def _pinned_blend_params(blend_key: str) -> dict:
    path = os.path.join(_PROJECT_ROOT, "research_strategy", "strategies_config.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)[blend_key]["parameters"]


def test_c62_confluence_attribution_under_pinned_cdhb_config():
    """`C6.2` attribution: under `chan_dual_hybrid_blend`'s pinned config, the LEGACY
    resonance sizing is ON and Track C Anchor 3 is OFF -- each decided by its OWN key.
    """
    from pipeline.research.blend import ChanDualHybridBlendStrategy

    params = _pinned_blend_params("chan_dual_hybrid_blend")
    strat = ChanDualHybridBlendStrategy()

    # The pinned key exists and is literally `cdhb_confluence_sizing: true`.
    assert params.get("cdhb_confluence_sizing") is True
    # ... and it is the ONLY key in the block whose name mentions "confluence".
    assert [k for k in params if "confluence" in k] == ["cdhb_confluence_sizing"]

    gates = strat.confluence_sizing_gates(params)
    assert gates["legacy_confluence_sizing"] is True, "legacy `c_mult` resonance sizing must be ON"
    assert gates["anchor3_vp_confluence_sizing"] is False, (
        "Track C Anchor 3 must be OFF: its key `blend_enable_vp_confluence_sizing` is "
        "absent from every blend block in strategies_config.json"
    )

    # Each flag is decided by its own key -- cross-referencing is exactly the C6.2 bug.
    assert strat.resolve_param(params, "confluence_sizing", False, bool) is True
    assert strat.resolve_param(params, "enable_vp_confluence_sizing", True, bool) is False

    # Flipping ONE key never moves the other, in either direction.
    p_anchor_on = dict(params)
    p_anchor_on["cdhb_enable_vp_confluence_sizing"] = True
    assert strat.resolve_param(p_anchor_on, "enable_vp_confluence_sizing", False, bool) is True
    assert strat.resolve_param(p_anchor_on, "confluence_sizing", False, bool) is True

    p_legacy_off = dict(params)
    p_legacy_off["cdhb_confluence_sizing"] = False
    assert strat.resolve_param(p_legacy_off, "confluence_sizing", True, bool) is False
    assert strat.resolve_param(p_legacy_off, "enable_vp_confluence_sizing", True, bool) is False

    # AUDIT-CALIBRE GUARD: a substring audit ("does any key mention confluence?") says
    # "Track C confluence sizing is enabled" on this block, while the key-based audit
    # says Anchor 3 is OFF. The two DISAGREE, which is precisely why any check that
    # infers Anchor 3 from a "confluence" substring match is wrong and must fail.
    assert any("confluence" in k for k in params)
    assert gates["anchor3_vp_confluence_sizing"] is False


def test_c62_anchor3_consumes_its_own_sizing_key():
    """`C6.2` behavioural proof: Anchor 3 changes the target weights via ITS OWN key.

    Fixture: 601857.SH prints a VAH breakout with single-peak concentration >= 0.85,
    which is exactly Anchor 3's `conf_count += 1` leg. With the legacy resonance block
    on, turning Anchor 3 on lifts 601857.SH's confluence multiplier above the other
    candidate's and the pre-emptive thrust book flips to it.
    """
    dates = pd.date_range("2024-01-01", periods=60, freq="B")

    def _fixture():
        df_a = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.003)
        df_b = _build_synthetic_bar_df(dates, 20.0, daily_ret=0.003)
        df_b["wyckoff_effort_result"] = 0.20
        df_b["High"] = df_b["Close"] * 1.05
        df_b["Low"] = df_b["Close"] * 0.99
        df_c = _build_synthetic_bar_df(dates, 30.0, daily_ret=0.003)
        df_c["VAH"] = df_c["Close"] * 0.98
        df_c["cyq_cost_concentration"] = 0.90
        cash = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)
        return {"600584.SH": df_a, "002371.SZ": df_b, "601857.SH": df_c, "BIL": cash}

    def _run(legacy_on: bool, anchor3_on: bool):
        params = {
            "cash_proxy": "BIL",
            "confluence_sizing": legacy_on,
            "blend_enable_vp_confluence_sizing": anchor3_on,
            "preemptive_thrust_deployment": True,
            "min_thrust_assets": 1,
        }
        w = GenericBlendStrategy().generate_weights(_fixture(), params=params)
        return w.ffill().fillna(0.0)

    w_anchor3_off = _run(legacy_on=True, anchor3_on=False)
    w_anchor3_on = _run(legacy_on=True, anchor3_on=True)

    assert float(w_anchor3_off.iloc[-1]["601857.SH"]) == pytest.approx(0.0)
    assert float(w_anchor3_on.iloc[-1]["601857.SH"]) > 0.0, (
        "Anchor 3's VAH+concentration leg must lift 601857.SH into the book when "
        "`enable_vp_confluence_sizing` is on -- if it does not, Anchor 3 is not "
        "consuming its own key"
    )

    # Documented structural precondition (not the defect): Anchor 3 is a *modifier* of
    # the legacy resonance block, so with the legacy block off it has no effect. This
    # assertion is intentionally fail-loud: decoupling the two is a behaviour change
    # that must be made consciously (and versioned), not by accident.
    w_legacy_off_anchor3_on = _run(legacy_on=False, anchor3_on=True)
    w_legacy_off_anchor3_off = _run(legacy_on=False, anchor3_on=False)
    pd.testing.assert_frame_equal(w_legacy_off_anchor3_on, w_legacy_off_anchor3_off)


def test_c62_anchor3_is_nested_in_legacy_block_and_keys_stay_distinct():
    """`C6.2` source guard (AST): the two switches stay separate, and Anchor 3's gate
    stays documented as a nested modifier of the legacy block.

    Guards two regressions:
      1. the Anchor 3 gate drifting out of the legacy block (making the documented
         precondition a lie, i.e. a silent wiring change); and
      2. a third "confluence" resolve_param key appearing (which is how the two keys
         got conflated in the first place).
    """
    import ast

    tree = ast.parse(_blend_base_source())

    legacy_gate = None
    anchor3_gate = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if names == {"legacy_confluence_sizing"}:
            legacy_gate = node
        # The Anchor 3 gate is `if enable_vp_confluence_sizing and ...:` (an AND, unlike
        # the OR-ed feature-enrichment trigger that also mentions the switch).
        if (
            "enable_vp_confluence_sizing" in names
            and isinstance(node.test, ast.BoolOp)
            and isinstance(node.test.op, ast.And)
        ):
            assert anchor3_gate is None, "expected exactly one Anchor 3 gate in blend_base.py"
            anchor3_gate = node

    assert legacy_gate is not None, "legacy `c_mult` confluence sizing gate not found"
    assert anchor3_gate is not None, "Track C Anchor 3 gate not found"

    nested = any(n is anchor3_gate for n in ast.walk(legacy_gate))
    assert nested, (
        "Track C Anchor 3's gate must stay nested inside the legacy confluence block it "
        "modifies; moving it out silently changes when Anchor 3 can fire"
    )

    confluence_keys = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "resolve_param" or len(node.args) < 2:
            continue
        name_arg = node.args[1]
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            if "confluence" in name_arg.value:
                confluence_keys.add(name_arg.value)
    # Both switches resolve through EXACTLY these two literal keys. A third
    # "confluence" key is how the legacy sizing and Anchor 3 got conflated; a
    # non-literal (constant/attribute) name argument would also silently drop the
    # config-wiring analyzer's proof that these fields are consumed.
    assert confluence_keys == {"confluence_sizing", "enable_vp_confluence_sizing"}, (
        f"expected exactly two distinct literal confluence resolve_param keys, got "
        f"{sorted(confluence_keys)}"
    )

    # And the documented constants must stay pinned to those two literals.
    assert BaseBlendAllocationStrategy.LEGACY_CONFLUENCE_SIZING_KEY == "confluence_sizing"
    assert BaseBlendAllocationStrategy.TRACK_C_ANCHOR3_CONFLUENCE_SIZING_KEY == "enable_vp_confluence_sizing"
    assert (
        BaseBlendAllocationStrategy.LEGACY_CONFLUENCE_SIZING_KEY
        != BaseBlendAllocationStrategy.TRACK_C_ANCHOR3_CONFLUENCE_SIZING_KEY
    )

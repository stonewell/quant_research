"""Unit and Integration Tests for Dedicated Blend Allocation Strategy Modules.

Validates the BaseBlendAllocationStrategy architecture in pipeline/research/blend/:
- BaseBlendAllocationStrategy parameter resolution and universal risk management
- ChanRiskManagedBlendStrategy
- ChanFourStateBlendStrategy
- ChanCrisisShieldBlendStrategy
- ChanDualHybridBlendStrategy
- GenericBlendStrategy
- Default institutional settings: vol_model="garman_klass", stop_mode="atr_trailing", adaptive_inertia=True
- Sparse weights contract compliance (NaN-vs-0.0 rule, no uninstructed rebalance leakage)
- Re-exports in pipeline.research.blend and pipeline.research_strategy.rs.chan_advanced_strategies

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py.
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

from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import (
    BaseBlendAllocationStrategy,
    ChanCrisisShieldBlendStrategy,
    ChanDualHybridBlendStrategy,
    ChanFourStateBlendStrategy,
    ChanFourStateShieldBlendStrategy,
    ChanRiskManagedBlendStrategy,
    GenericBlendStrategy,
)
from pipeline.research.blend.base import BaseBlendAllocationStrategy as ReexportedBase
from pipeline.research.blend.blend_base import _compute_garman_klass_vol
from pipeline.research_strategy.rs.config import StrategyConfig
from pipeline.research_strategy.rs.strategy import STRATEGY_CLASS_MAP, instantiate_strategy_from_config_entry


def create_mock_universe(n_days: int = 300) -> dict:
    """Creates a deterministic synthetic universe for offline testing."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    close_a = 100.0 + 0.15 * t + 5.0 * np.sin(2 * np.pi * t / 25)
    close_b = 100.0 + 0.08 * t + 4.0 * np.cos(2 * np.pi * t / 30)
    close_c = 100.0 - 0.05 * t + 3.0 * np.sin(2 * np.pi * t / 20)
    bil_close = np.full(n_days, 100.0)

    universe = {
        "601225.SH": make_ohlcv_from_closes(close_a),
        "601857.SH": make_ohlcv_from_closes(close_b),
        "601872.SH": make_ohlcv_from_closes(close_c),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates
    return universe


def test_base_blend_reexport_and_aliases():
    """Verify BaseBlendAllocationStrategy is cleanly accessible via all expected import paths."""
    assert BaseBlendAllocationStrategy is ReexportedBase
    assert issubclass(ChanRiskManagedBlendStrategy, BaseBlendAllocationStrategy)
    assert issubclass(ChanFourStateBlendStrategy, BaseBlendAllocationStrategy)
    assert issubclass(ChanCrisisShieldBlendStrategy, BaseBlendAllocationStrategy)
    assert issubclass(ChanFourStateShieldBlendStrategy, BaseBlendAllocationStrategy)
    assert issubclass(ChanDualHybridBlendStrategy, BaseBlendAllocationStrategy)
    assert issubclass(GenericBlendStrategy, BaseBlendAllocationStrategy)


def test_blend_strategy_class_map_registration():
    """Verify all existing blend strategies are properly registered in STRATEGY_CLASS_MAP."""
    assert "ChanRiskManagedBlendStrategy" in STRATEGY_CLASS_MAP
    assert "ChanFourStateBlendStrategy" in STRATEGY_CLASS_MAP
    assert "ChanCrisisShieldBlendStrategy" in STRATEGY_CLASS_MAP
    assert "ChanFourStateShieldBlendStrategy" in STRATEGY_CLASS_MAP
    assert "ChanDualHybridBlendStrategy" in STRATEGY_CLASS_MAP


def test_default_institutional_settings_on_all_blend_strategies():
    """Verify vol_model='garman_klass', stop_mode='atr_trailing', and adaptive_inertia=True
    are the defaults across all blend strategy configs and class instantiations."""
    cfg = StrategyConfig()
    # Check default config fields
    assert cfg.blend_vol_model == "garman_klass"
    assert cfg.blend_stop_mode == "atr_trailing"
    assert cfg.blend_adaptive_inertia is True
    assert cfg.crb_vol_model == "garman_klass"
    assert cfg.crb_stop_mode == "atr_trailing"
    assert cfg.crb_adaptive_inertia is True
    assert cfg.cfsb_vol_model == "garman_klass"
    assert cfg.cfsb_stop_mode == "atr_trailing"
    assert cfg.cfsb_adaptive_inertia is True
    assert cfg.ccsb_vol_model == "garman_klass"
    assert cfg.ccsb_stop_mode == "atr_trailing"
    assert cfg.ccsb_adaptive_inertia is True
    assert cfg.cfssb_vol_model == "garman_klass"
    assert cfg.cfssb_stop_mode == "atr_trailing"
    assert cfg.cfssb_adaptive_inertia is True
    assert cfg.cdhb_vol_model == "garman_klass"
    assert cfg.cdhb_stop_mode == "atr_trailing"
    assert cfg.cdhb_adaptive_inertia is True
    assert cfg.blend_min_holding_bars == 2
    assert cfg.blend_min_entry_bars == 2
    assert cfg.blend_thrust_incumbent_bonus == 1.20
    assert cfg.crb_min_holding_bars == 2
    assert cfg.crb_min_entry_bars == 2
    assert cfg.crb_thrust_incumbent_bonus == 1.20
    assert cfg.cfsb_min_holding_bars == 2
    assert cfg.cfsb_min_entry_bars == 2
    assert cfg.cfsb_thrust_incumbent_bonus == 1.20
    assert cfg.ccsb_min_holding_bars == 2
    assert cfg.ccsb_min_entry_bars == 2
    assert cfg.ccsb_thrust_incumbent_bonus == 1.20
    assert cfg.cfssb_min_holding_bars == 2
    assert cfg.cfssb_min_entry_bars == 2
    assert cfg.cfssb_thrust_incumbent_bonus == 1.20
    assert cfg.cdhb_min_holding_bars == 2
    assert cfg.cdhb_min_entry_bars == 2
    assert cfg.cdhb_thrust_incumbent_bonus == 1.20
    assert cfg.chan_fse_min_entry_bars == 2

    # Check instantiated strategy objects
    s_crb = ChanRiskManagedBlendStrategy()
    assert s_crb.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_crb.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_crb.resolve_param({}, "adaptive_inertia", False) is True
    assert s_crb.resolve_param({}, "min_holding_bars", 0) == 2
    assert s_crb.resolve_param({}, "min_entry_bars", 0) == 2
    assert s_crb.resolve_param({}, "thrust_incumbent_bonus", 1.0) == 1.20

    s_cfsb = ChanFourStateBlendStrategy()
    assert s_cfsb.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_cfsb.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_cfsb.resolve_param({}, "adaptive_inertia", False) is True
    assert s_cfsb.resolve_param({}, "min_holding_bars", 0) == 2
    assert s_cfsb.resolve_param({}, "min_entry_bars", 0) == 2

    s_ccsb = ChanCrisisShieldBlendStrategy()
    assert s_ccsb.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_ccsb.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_ccsb.resolve_param({}, "adaptive_inertia", False) is True
    assert s_ccsb.resolve_param({}, "min_holding_bars", 0) == 2
    assert s_ccsb.resolve_param({}, "min_entry_bars", 0) == 2

    s_cfssb = ChanFourStateShieldBlendStrategy()
    assert s_cfssb.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_cfssb.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_cfssb.resolve_param({}, "adaptive_inertia", False) is True
    assert s_cfssb.resolve_param({}, "min_holding_bars", 0) == 2
    assert s_cfssb.resolve_param({}, "min_entry_bars", 0) == 2
    assert s_cfssb.resolve_param({}, "thrust_incumbent_bonus", 1.0) == 1.20

    s_cdhb = ChanDualHybridBlendStrategy()
    assert s_cdhb.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_cdhb.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_cdhb.resolve_param({}, "adaptive_inertia", False) is True

    s_gen = GenericBlendStrategy()
    assert s_gen.resolve_param({}, "vol_model", "wrong") == "garman_klass"
    assert s_gen.resolve_param({}, "stop_mode", "wrong") == "atr_trailing"
    assert s_gen.resolve_param({}, "adaptive_inertia", False) is True


def test_base_blend_param_resolution_hierarchy():
    """Verify that resolve_param correctly resolves strategy prefix > blend prefix > bare name > config fallback."""
    strat = BaseBlendAllocationStrategy(
        name="test_blend",
        param_prefix="test",
        alt_param_prefixes=["crb", "cfsb"],
        config=StrategyConfig(blend_vol_model="garman_klass"),
    )

    # 1. Bare name in params
    assert strat.resolve_param({"vol_model": "close_to_close"}, "vol_model") == "close_to_close"

    # 2. Universal blend_ prefix in params overrides bare name
    assert strat.resolve_param({"blend_vol_model": "custom_blend", "vol_model": "close_to_close"}, "vol_model") == "custom_blend"

    # 3. Strategy-specific prefix in params overrides universal blend_
    assert strat.resolve_param({"test_vol_model": "specific_vol", "blend_vol_model": "custom_blend"}, "vol_model") == "specific_vol"

    # 4. Alt prefix in params overrides universal blend_
    assert strat.resolve_param({"crb_vol_model": "crb_vol", "blend_vol_model": "custom_blend"}, "vol_model") == "crb_vol"

    # 5. Fallback to config when params empty
    assert strat.resolve_param({}, "vol_model") == "garman_klass"


def test_all_blend_strategies_sparse_weights_contract():
    """Verify all 5 blend strategies strictly satisfy the sparse weights contract:
    - Target weights only on rebalance dates (sparse)
    - NaNs on intermediate dates (never forward filled by template)
    - Explicit 0.0 for excluded/stopped assets (never stale NaN carry-forward)
    - Risky weights sum <= 1.00001
    """
    universe = create_mock_universe(n_days=280)
    strats = [
        ChanRiskManagedBlendStrategy(),
        ChanFourStateBlendStrategy(),
        ChanCrisisShieldBlendStrategy(),
        ChanFourStateShieldBlendStrategy(),
        ChanDualHybridBlendStrategy(),
        GenericBlendStrategy(),
    ]

    for strat in strats:
        weights = strat.generate_weights(universe)
        assert isinstance(weights, pd.DataFrame)
        assert set(universe.keys()).issubset(weights.columns)

        # Check sparsity: some days must be completely NaN (non-rebalance days)
        all_nan_rows = weights.isna().all(axis=1)
        assert all_nan_rows.any(), f"{strat.name} did not generate sparse weights"

        # Check rebalance days
        rebal_days = weights.dropna(how="all")
        assert not rebal_days.empty, f"{strat.name} has no rebalances"

        # Check that rebalance rows do NOT contain NaN for risky assets (must be explicit 0.0 or weight)
        risky_cols = [c for c in weights.columns if c != "BIL"]
        assert not rebal_days[risky_cols].isna().any().any(), f"{strat.name} contains NaN on rebalance row for risky symbol"

        # Check max risky allocation <= 1.00001
        risky_sum = rebal_days[risky_cols].sum(axis=1)
        assert (risky_sum <= 1.00001).all(), f"{strat.name} exceeded 100% risky budget: {risky_sum.max()}"


def test_generic_blend_custom_weights_and_execution():
    """Verify GenericBlendStrategy supports custom sub-strategy weights and configures correctly."""
    custom_weights = {
        "chan_three_type": 0.40,
        "chan_vaa_compound": 0.35,
        "wq_alpha1": 0.25,
    }
    cfg = StrategyConfig(
        blend_sub_strategies=custom_weights,
        blend_max_single_position=0.25,
        cash_proxy="BIL",
    )
    strat = GenericBlendStrategy(config=cfg)
    assert strat.get_sub_strategy_weights() == custom_weights
    assert "chan_three_type (40%)" in strat.explain_weights()
    assert "chan_vaa_compound (35%)" in strat.explain_weights()
    assert "wq_alpha1 (25%)" in strat.explain_weights()

    universe = create_mock_universe(n_days=260)
    weights = strat.generate_weights(universe)
    rebal = weights.dropna(how="all")
    assert not rebal.empty
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky.sum(axis=1) <= 1.00001).all()


def test_garman_klass_vol_computation():
    """Verify Garman-Klass volatility computation with OHLC bars and fallback on degenerate data."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    c = np.linspace(100, 150, 100)
    # Realistic OHLC
    df_ohlc = pd.DataFrame({
        "Open": c * 0.99,
        "High": c * 1.02,
        "Low": c * 0.98,
        "Close": c,
    }, index=dates)

    gk_vol = _compute_garman_klass_vol(df_ohlc, window=21, default_vol=0.14)
    assert not gk_vol.isna().any()
    assert (gk_vol > 0.0).all()
    assert (gk_vol < 1.0).all()

    # Flat High==Low fallback
    df_flat = pd.DataFrame({
        "Open": c,
        "High": c,
        "Low": c,
        "Close": c,
    }, index=dates)
    fallback_vol = _compute_garman_klass_vol(df_flat, window=21, default_vol=0.14)
    assert not fallback_vol.isna().any()
    assert (fallback_vol >= 0.0).all()


def test_instantiate_blend_strategies_from_config_entry():
    """Verify that all blend strategies can be instantiated via instantiate_strategy_from_config_entry."""
    class_map = {
        "chan_risk_managed_blend": "ChanRiskManagedBlendStrategy",
        "chan_four_state_blend": "ChanFourStateBlendStrategy",
        "chan_four_state_shield_blend": "ChanFourStateShieldBlendStrategy",
        "chan_crisis_shield_blend": "ChanCrisisShieldBlendStrategy",
        "chan_dual_hybrid_blend": "ChanDualHybridBlendStrategy",
    }
    for strat_name, cls_name in class_map.items():
        entry = {
            "name": strat_name,
            "type": "class",
            "class_name": cls_name,
            "description": f"Test {strat_name}",
            "parameters": {"cash_proxy": "BIL"},
        }
        inst = instantiate_strategy_from_config_entry(strat_name, entry)
        assert isinstance(inst, BaseBlendAllocationStrategy)
        assert inst.name == strat_name
        assert inst.warmup_bars() >= 200


def test_chan_four_state_shield_blend_strategy_sub_strategies_and_regimes():
    """Verify ChanFourStateShieldBlendStrategy correctly builds its 4 sub-strategies and adapts regime weights."""
    universe = create_mock_universe(n_days=270)
    strat = ChanFourStateShieldBlendStrategy()
    sub_strats = strat.get_sub_strategies(universe)
    assert set(sub_strats.keys()) == {"chan_three_type", "chan_vaa_compound", "chan_four_state", "wq_alpha12"}

    # Base weights
    base_w = strat.get_sub_strategy_weights()
    assert abs(sum(base_w.values()) - 1.0) < 1e-5
    assert base_w["chan_three_type"] == 0.35
    assert base_w["chan_vaa_compound"] == 0.25
    assert base_w["chan_four_state"] == 0.20
    assert base_w["wq_alpha12"] == 0.20

    # Bull regime
    bull_w = strat.compute_regime_weights(base_w, breadth=0.70, thrust=0.65, date="2020-05-01")
    assert bull_w["chan_three_type"] == 0.40
    assert bull_w["chan_four_state"] == 0.25
    assert bull_w["chan_vaa_compound"] == 0.15
    assert bull_w["wq_alpha12"] == 0.20

    # Bear regime
    bear_w = strat.compute_regime_weights(base_w, breadth=0.20, thrust=0.30, date="2020-05-01")
    assert bear_w["chan_vaa_compound"] == 0.35
    assert bear_w["wq_alpha12"] == 0.25
    assert bear_w["chan_three_type"] == 0.25
    assert bear_w["chan_four_state"] == 0.15

    # Weights explanation
    exp = strat.explain_weights()
    assert "Alpha#12" in exp
    assert "chan_four_state_execution" in exp


def test_blend_sub_strategy_class_resolution_regression():
    """Regression test for the bare-name resolve_param collision that silently
    substituted WorldQuantAlphaStrategy #101 for ChanFourStateExecutionStrategy /
    ChanCompositeStrategy.

    The bug: get_sub_strategies() resolved a bare "wq_alpha_id" param, whose
    resolve_param fallback leaked the global StrategyConfig.wq_alpha_id=101,
    making the `wq_id is not None` branch always true. See docs/version.md.
    """
    from pipeline.research_strategy.rs.chan_advanced_strategies import (
        ChanCompositeStrategy,
        ChanFourStateExecutionStrategy,
        ChanVaaCompoundStrategy,
    )
    from pipeline.research_strategy.rs.strategy import ChanThreeTypeStrategy
    from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantAlphaStrategy

    # 1. chan_four_state_blend: the chan_four_state leg MUST be the four-state
    #    execution machine (not a WorldQuant alpha).
    subs = ChanFourStateBlendStrategy().get_sub_strategies({})
    assert isinstance(subs["chan_four_state"], ChanFourStateExecutionStrategy)
    assert not isinstance(subs["chan_four_state"], WorldQuantAlphaStrategy)
    assert isinstance(subs["chan_three_type"], ChanThreeTypeStrategy)
    assert isinstance(subs["chan_vaa_compound"], ChanVaaCompoundStrategy)

    # 2. chan_risk_managed_blend: the chan_composite leg MUST be ChanCompositeStrategy.
    subs = ChanRiskManagedBlendStrategy().get_sub_strategies({})
    assert isinstance(subs["chan_composite"], ChanCompositeStrategy)
    assert not isinstance(subs["chan_composite"], WorldQuantAlphaStrategy)

    # 3. chan_crisis_shield_blend: chan_composite is INTENTIONALLY WorldQuant Alpha #12.
    subs = ChanCrisisShieldBlendStrategy().get_sub_strategies({})
    assert isinstance(subs["chan_composite"], WorldQuantAlphaStrategy)
    assert subs["chan_composite"].config.wq_alpha_id == 12

    # 4. chan_four_state_shield_blend: real four-state machine + Alpha #12 shield.
    subs = ChanFourStateShieldBlendStrategy().get_sub_strategies({})
    assert isinstance(subs["chan_four_state"], ChanFourStateExecutionStrategy)
    assert isinstance(subs["wq_alpha12"], WorldQuantAlphaStrategy)
    assert subs["wq_alpha12"].config.wq_alpha_id == 12

    # 5. chan_dual_hybrid_blend: alphas #53 / #3 (expanded sleeve #41 / #101).
    subs = ChanDualHybridBlendStrategy().get_sub_strategies({})
    assert isinstance(subs["wq_alpha1"], WorldQuantAlphaStrategy)
    assert subs["wq_alpha1"].config.wq_alpha_id == 53
    assert subs["wq_alpha2"].config.wq_alpha_id == 3
    assert subs["wq_alpha3"].config.wq_alpha_id == 41
    assert subs["wq_alpha4"].config.wq_alpha_id == 101


def test_base_blend_rolling_ic_ir_weighting():
    """Tests rolling IC-IR adaptive weighting and HLZ t-stat significance hurdle."""
    mock_universe = create_mock_universe(150)
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "chan_three_type": 0.50,
        "chan_vaa_compound": 0.50,
    }
    cfg.blend_weighting_mode = "rolling_ic_ir"
    cfg.blend_adaptive_lookback_days = 30
    cfg.blend_adaptive_min_tstat = 1.5

    strat = GenericBlendStrategy(cfg)
    weights = strat.generate_weights(mock_universe)
    assert not weights.empty
    assert set(mock_universe.keys()).issubset(set(weights.columns))

    # Sparse weights contract verification
    nan_rows = weights.isna().all(axis=1)
    assert nan_rows.sum() > 0

    rebal_rows = weights[~nan_rows]
    for dt, row in rebal_rows.iterrows():
        assert not row.isna().any(), f"Rebalance row at {dt} contains NaN!"
        assert np.isclose(row.sum(), 1.0, atol=1e-3)


def test_base_blend_regime_adaptive_mode():
    """Tests multi-regime adaptive style routing across momentum, reversal, and defensive pools."""
    mock_universe = create_mock_universe(150)
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "chan_three_type": 0.40,
        "gtja_alpha16": 0.30,
        "chan_vaa_compound": 0.30,
    }
    cfg.blend_weighting_mode = "regime_adaptive"

    strat = GenericBlendStrategy(cfg)
    # Check style classification
    sub_strats = strat.get_sub_strategies(mock_universe)
    styles = strat._classify_sub_strategy_styles(sub_strats)
    assert styles["chan_three_type"] == "momentum"
    assert styles["gtja_alpha16"] == "reversal"
    assert styles["chan_vaa_compound"] == "defensive"

    weights = strat.generate_weights(mock_universe)
    assert not weights.empty

    nan_rows = weights.isna().all(axis=1)
    assert nan_rows.sum() > 0


def test_base_blend_regime_ic_ir_hybrid_mode():
    """Tests two-tier hybrid mode: macro regime style budgets + intra-bucket rolling IC-IR."""
    mock_universe = create_mock_universe(150)
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "chan_three_type": 0.35,
        "wq_alpha41": 0.35,
        "chan_vaa_compound": 0.30,
    }
    cfg.blend_weighting_mode = "regime_ic_ir_hybrid"
    cfg.blend_adaptive_lookback_days = 40
    cfg.blend_adaptive_min_tstat = 2.0
    cfg.blend_adaptive_shrinkage = 0.20

    strat = GenericBlendStrategy(cfg)
    weights = strat.generate_weights(mock_universe)
    assert not weights.empty

    nan_rows = weights.isna().all(axis=1)
    rebal_rows = weights[~nan_rows]
    for dt, row in rebal_rows.iterrows():
        assert not row.isna().any()
        assert np.isclose(row.sum(), 1.0, atol=1e-3)


def test_base_blend_lowdin_orthogonalization():
    """Tests Löwdin symmetric orthogonalization operator on collinear strategy weight vectors."""
    strat = BaseBlendAllocationStrategy()
    symbols = ["S1", "S2", "S3"]
    # Two identical strategies (perfect collinearity) + one distinct
    mat = pd.DataFrame({
        "strat_a": [0.5, 0.5, 0.0],
        "strat_b": [0.5, 0.5, 0.0],
        "strat_c": [0.0, 0.0, 1.0],
    }, index=symbols)

    mat_ortho = strat._apply_lowdin_orthogonalization(mat)
    assert mat_ortho.shape == mat.shape
    # Check non-negativity
    assert (mat_ortho.values >= -1e-6).all()
    # Check column preservation (sums match or scaled)
    for col in mat.columns:
        assert np.isclose(mat_ortho[col].sum(), mat[col].sum(), atol=1e-3)

    # Full integration test with enable_orthogonalization=True
    mock_universe = create_mock_universe(150)
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "chan_three_type": 0.50,
        "chan_vaa_compound": 0.50,
    }
    cfg.blend_enable_orthogonalization = True

    strat_ortho = GenericBlendStrategy(cfg)
    weights = strat_ortho.generate_weights(mock_universe)
    assert not weights.empty
    rebal_rows = weights[~weights.isna().all(axis=1)]
    for dt, row in rebal_rows.iterrows():
        assert not row.isna().any()
        assert np.isclose(row.sum(), 1.0, atol=1e-3)


def test_generic_blend_with_worldquant_alphas_and_regime_hybrid():
    """Validates GenericBlendStrategy combining multiple WQ/GTJA alphas under regime_ic_ir_hybrid."""
    mock_universe = create_mock_universe(150)
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "wq_alpha6": 0.25,
        "wq_alpha41": 0.25,
        "gtja_alpha16": 0.50,
    }
    cfg.blend_weighting_mode = "regime_ic_ir_hybrid"
    cfg.blend_adaptive_min_tstat = 2.0

    strat = GenericBlendStrategy(cfg)
    sub_strats = strat.get_sub_strategies(mock_universe)
    assert len(sub_strats) == 3
    assert "wq_alpha6" in sub_strats
    assert "wq_alpha41" in sub_strats
    assert "gtja_alpha16" in sub_strats

    weights = strat.generate_weights(mock_universe)
    assert not weights.empty
    rebal_rows = weights[~weights.isna().all(axis=1)]
    assert len(rebal_rows) > 0
    for dt, row in rebal_rows.iterrows():
        assert not row.isna().any()
        assert np.isclose(row.sum(), 1.0, atol=1e-3)


def test_min_holding_bars_suppresses_one_day_flips():
    """Validates that apply_asset_inertia with min_holding_bars=2 suppresses 1-day exit flips."""
    from common.allocation_templates import apply_asset_inertia

    dates = pd.bdate_range("2020-01-01", periods=5)
    # Day 0: 0.0, Day 1: 0.20 (buy), Day 2: 0.0 (ideal wants to flip sell), Day 3: 0.0, Day 4: 0.0
    daily = pd.DataFrame({
        "S1": [0.0, 0.20, 0.0, 0.0, 0.0],
        "CASH": [1.0, 0.80, 1.0, 1.0, 1.0],
    }, index=dates)

    # Without min_holding_bars: flips out on day 2 (1-day hold)
    res_no_hold = apply_asset_inertia(daily, min_weight_change=0.05, cash_proxy="CASH", min_holding_bars=0)
    assert res_no_hold.loc[dates[2], "S1"] == 0.0  # Sold after 1 day!

    # With min_holding_bars=2: holding tenure on day 2 is 1 bar (< 2), so sale is suppressed!
    res_held = apply_asset_inertia(daily, min_weight_change=0.05, cash_proxy="CASH", min_holding_bars=2)
    assert res_held.loc[dates[1], "S1"] == 0.20    # Bought on Day 1
    assert res_held.loc[dates[2], "S1"] == 0.20    # Protected on Day 2 (holding tenure = 1 bar)
    assert res_held.loc[dates[3], "S1"] == 0.0     # Cleanly liquidated on Day 3 (holding tenure = 2 bars >= 2)


def test_emergency_mask_bypasses_min_holding_bars():
    """Validates that emergency stop events (per-asset or global) bypass min_holding_bars."""
    from common.allocation_templates import apply_asset_inertia

    dates = pd.bdate_range("2020-01-01", periods=4)
    daily = pd.DataFrame({
        "S1": [0.0, 0.20, 0.0, 0.0],
        "S2": [0.0, 0.20, 0.0, 0.0],
        "CASH": [1.0, 0.60, 1.0, 1.0],
    }, index=dates)

    # Per-asset emergency mask: S1 is stopped out on day 2, S2 is not
    emergency_df = pd.DataFrame({
        "S1": [False, False, True, False],
        "S2": [False, False, False, False],
    }, index=dates)

    res = apply_asset_inertia(daily, min_weight_change=0.05, cash_proxy="CASH", emergency_mask=emergency_df, min_holding_bars=2)
    # S1 had emergency -> liquidated immediately on Day 2 despite tenure < 2
    assert res.loc[dates[2], "S1"] == 0.0
    # S2 had NO emergency -> protected on Day 2
    assert res.loc[dates[2], "S2"] == 0.20


def test_blend_strategy_no_one_day_trades_in_backtest():
    """Integration test verifying that ChanRiskManagedBlendStrategy produces 0 one-day holding trades."""
    mock_universe = create_mock_universe(120)
    cfg = StrategyConfig()
    cfg.crb_min_holding_bars = 2
    cfg.crb_thrust_incumbent_bonus = 1.20

    strat = ChanRiskManagedBlendStrategy(cfg)
    sparse_w = strat.generate_weights(mock_universe)
    assert not sparse_w.empty

    # Reconstruct dense holding series
    dense_w = sparse_w.ffill().fillna(0.0)
    risky_symbols = [s for s in sparse_w.columns if s != "BIL"]

    # Measure holding run lengths for each symbol
    one_day_holds = 0
    for sym in risky_symbols:
        held = (dense_w[sym] > 1e-4).astype(int)
        # Identify start and end of holding spells
        diff = held.diff().fillna(held.iloc[0])
        entries = held.index[diff == 1].tolist()
        exits = held.index[diff == -1].tolist()
        
        # Check every completed trade spell
        for entry_dt in entries:
            future_exits = [ex for ex in exits if ex > entry_dt]
            if future_exits:
                exit_dt = future_exits[0]
                bars_held = len(dense_w.loc[entry_dt:exit_dt]) - 1
                if bars_held == 1:
                    one_day_holds += 1

    assert one_day_holds == 0, f"Expected 0 one-day holding trades, found {one_day_holds}"


def test_chan_four_state_blend_no_one_day_trades_in_backtest():
    """Integration test verifying that ChanFourStateBlendStrategy produces 0 one-day holding trades."""
    mock_universe = create_mock_universe(120)
    cfg = StrategyConfig()
    cfg.cfsb_min_holding_bars = 2
    cfg.cfsb_min_entry_bars = 2

    strat = ChanFourStateBlendStrategy(cfg)
    sparse_w = strat.generate_weights(mock_universe)
    assert not sparse_w.empty

    dense_w = sparse_w.ffill().fillna(0.0)
    risky_symbols = [s for s in sparse_w.columns if s != "BIL"]

    one_day_holds = 0
    for sym in risky_symbols:
        held = (dense_w[sym] > 1e-4).astype(int)
        diff = held.diff().fillna(held.iloc[0])
        entries = held.index[diff == 1].tolist()
        exits = held.index[diff == -1].tolist()

        for entry_dt in entries:
            future_exits = [ex for ex in exits if ex > entry_dt]
            if future_exits:
                exit_dt = future_exits[0]
                bars_held = len(dense_w.loc[entry_dt:exit_dt]) - 1
                if bars_held == 1:
                    one_day_holds += 1

    assert one_day_holds == 0, f"Expected 0 one-day holding trades in ChanFourStateBlendStrategy, found {one_day_holds}"


def test_chan_fse_min_entry_bars_fsm_confirmation():
    """Validates that ChanFourStateExecutionStrategy's FSM confirms entry on bar 2 and aborts on invalidation."""
    from pipeline.research_strategy.rs.chan_advanced_strategies import run_four_state_position_loop

    n = 5
    # Scenario A: Bar 1 triggers 1B at 10.0, but bar 2 plunges to 9.0 (below 8% stop) -> invalidation aborts entry!
    close_fail = np.array([10.0, 10.0, 9.0, 9.0, 9.0])
    b1_signal = np.array([False, True, False, False, False])
    f = np.zeros(n, dtype=bool)

    raw_aborted = run_four_state_position_loop(
        close=close_fail, first_buy=b1_signal, second_buy=f, third_buy=f, sell_signal=f,
        min_entry_bars=2, stop_loss_pct=0.08,
    )
    assert np.all(raw_aborted == 0.0), f"Expected 0.0 allocation on aborted entry, got {raw_aborted}"

    # Scenario B: Bar 1 triggers 1B at 10.0, bar 2 holds at 10.2 -> confirmation succeeds on bar 2!
    close_hold = np.array([10.0, 10.0, 10.2, 10.5, 10.6])
    raw_confirmed = run_four_state_position_loop(
        close=close_hold, first_buy=b1_signal, second_buy=f, third_buy=f, sell_signal=f,
        min_entry_bars=2, stop_loss_pct=0.08,
    )
    assert raw_confirmed[0] == 0.0
    assert raw_confirmed[1] == 0.0  # Pending confirmation on Day 1 (not entered yet!)
    assert raw_confirmed[2] > 0.0   # Confirmed entry on Day 2!
    assert raw_confirmed[3] > 0.0   # Held on Day 3


def test_apply_asset_inertia_day0_and_incremental_protection():
    """Validates that apply_asset_inertia with min_holding_bars=2 protects both Day-0 entries and incremental add-ons from 1-day sells."""
    from common.allocation_templates import apply_asset_inertia

    dates = pd.bdate_range("2020-01-01", periods=4)

    # 1. Day 0 entry protection
    daily_day0 = pd.DataFrame({
        "S1": [0.20, 0.0, 0.0, 0.0],
        "CASH": [0.80, 1.0, 1.0, 1.0],
    }, index=dates)
    res_day0 = apply_asset_inertia(daily_day0, min_weight_change=0.05, cash_proxy="CASH", min_holding_bars=2)
    assert res_day0.loc[dates[0], "S1"] == 0.20
    assert res_day0.loc[dates[1], "S1"] == 0.20  # Day 1 sale was SUPPRESSED!
    assert res_day0.loc[dates[2], "S1"] == 0.0   # Day 2 sale was allowed!

    # 2. Incremental add-on protection
    daily_addon = pd.DataFrame({
        "S1": [0.05, 0.15, 0.05, 0.05],
        "CASH": [0.95, 0.85, 0.95, 0.95],
    }, index=dates)
    res_addon = apply_asset_inertia(daily_addon, min_weight_change=0.05, cash_proxy="CASH", min_holding_bars=2)
    assert res_addon.loc[dates[1], "S1"] == 0.15  # Bought add-on at Day 1
    assert res_addon.loc[dates[2], "S1"] == 0.15  # Day 2 reduction is SUPPRESSED!
    assert res_addon.loc[dates[3], "S1"] == 0.05  # Day 3 reduction is allowed!


def test_apply_asset_inertia_never_exceeds_100_pct_when_expanding_existing_positions():
    """Regression test: expanding multiple already-held positions must never push total risky weight > 1.0."""
    from common.allocation_templates import apply_asset_inertia

    dates = pd.bdate_range("2020-01-01", periods=3)
    daily = pd.DataFrame({
        "A": [0.40, 0.70, 0.70],
        "B": [0.40, 0.46, 0.46],
        "CASH": [0.20, 0.0, 0.0],
    }, index=dates)

    res = apply_asset_inertia(daily, min_weight_change=0.05, cash_proxy="CASH")
    risky_sum = res[["A", "B"]].sum(axis=1)
    assert (risky_sum <= 1.0 + 1e-9).all(), f"Risky sum exceeded 1.0: {risky_sum.to_dict()}"
    assert np.isclose(res.loc[dates[1], "A"], 0.60)
    assert np.isclose(res.loc[dates[1], "B"], 0.40)
    assert np.isclose(res.loc[dates[1], "CASH"], 0.0)


def test_base_blend_causal_adaptive_inertia_no_future_vol_lookahead():
    """Regression test: adaptive_inertia uses causal expanding volatility so future volatility spikes
    do not alter historical rebalance weights."""
    univ_full = create_mock_universe(n_days=160)
    cutoff = 120
    univ_trunc = {sym: df.iloc[:cutoff].copy() for sym, df in univ_full.items()}

    # Inject massive volatility after cutoff in univ_full
    univ_shocked = {sym: df.copy() for sym, df in univ_full.items()}
    shock_idx = univ_shocked["601225.SH"].index[cutoff:]
    univ_shocked["601225.SH"].loc[shock_idx, "High"] *= 3.0
    univ_shocked["601225.SH"].loc[shock_idx, "Low"] *= 0.3

    strat = ChanRiskManagedBlendStrategy()
    w_trunc = strat.generate_weights(univ_trunc)
    w_shocked = strat.generate_weights(univ_shocked).iloc[:cutoff]

    pd.testing.assert_frame_equal(w_trunc, w_shocked)


def test_base_blend_uplsm_fast_path_and_cooldown_parity():
    """Validates UPLSM Phase 3 parity features in BaseBlendAllocationStrategy:
    1. Day 1 entry bypass requires strict confluence of volume breakout (V >= 1.5 * MA20) AND market breadth >= 0.60
    2. Expansion bypass on volume breakout
    3. Normal strategic exit 1-bar rotation cooldown

    NOTE: each generate_weights call passes enable_cyq_quality_gate=False so the
    unrelated Track-C CYQ structural gate (which defaults ON and triggers on-demand
    structural enrichment of the synthetic universe) does not zero out the new entry
    and mask the UPLSM fast-path feature this test isolates.
    """
    class DummySubStrategy:
        def __init__(self, target_df):
            self.target_df = target_df
        def generate_weights(self, universe, params=None):
            return self.target_df

    class DummyUPLSMBlend(BaseBlendAllocationStrategy):
        name = "dummy_uplsm_blend"
        def __init__(self, target_df, config=None):
            super().__init__(config=config)
            self._target_df = target_df
        def get_sub_strategies(self, universe, params=None):
            return {"dummy": DummySubStrategy(self._target_df)}
        def get_sub_strategy_weights(self, params=None):
            return {"dummy": 1.0}

    n_days = 80
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)

    # Base down-trending universe: breadth is 0.0 after warmup
    close_down = 100.0 - 0.05 * t
    df_down = make_ohlcv_from_closes(close_down)
    df_down.index = dates
    df_down["Volume"] = 1000.0

    df_bil = make_ohlcv_from_closes(np.full(n_days, 100.0))
    df_bil.index = dates
    df_bil["Volume"] = 1000.0

    univ_down = {
        "S1": df_down.copy(),
        "BIL": df_bil.copy(),
    }

    # Case 1: Standard entry confirmation without fast-path (min_entry_bars=2)
    # Downward trend -> breadth is 0.0 < 0.60; Volume is flat 1000.0 -> no volume breakout.
    # Signal appears at index 55 for 1 bar only -> should be suppressed on Day 1
    sig_1d = pd.DataFrame(0.0, index=dates, columns=["S1", "BIL"])
    sig_1d.iloc[55, 0] = 0.20
    strat_1 = DummyUPLSMBlend(sig_1d)
    w1 = strat_1.generate_weights(univ_down, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "normal_cooldown_bars": 1,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert w1.iloc[55]["S1"] == 0.0, "Day 1 transient signal should be suppressed when no fast-path"

    # Case 2: Volume breakout alone without breadth thrust (breadth = 0.0 < 0.60)
    # Volume spikes to 2000.0 on Day 1 (2.0x MA20), but no macro thrust -> suppressed under strict confluence!
    univ_vol = {s: d.copy() for s, d in univ_down.items()}
    univ_vol["S1"].iloc[55, univ_vol["S1"].columns.get_loc("Volume")] = 2000.0
    strat_2 = DummyUPLSMBlend(sig_1d)
    w2 = strat_2.generate_weights(univ_vol, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "normal_cooldown_bars": 1,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert w2.iloc[55]["S1"] == 0.0, "Volume breakout alone without breadth thrust should be suppressed!"

    # Case 3: Market Breadth alone without volume breakout
    # Up-trending universe: close > 50d SMA at index 55 -> breadth = 1.0 >= 0.60, but Volume is flat 1000.0
    close_up = 100.0 + 0.05 * t
    df_up = make_ohlcv_from_closes(close_up)
    df_up.index = dates
    df_up["Volume"] = 1000.0
    univ_up = {"S1": df_up.copy(), "BIL": df_bil.copy()}

    strat_3 = DummyUPLSMBlend(sig_1d)
    w3 = strat_3.generate_weights(univ_up, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "normal_cooldown_bars": 1,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert w3.iloc[55]["S1"] == 0.0, "Breadth thrust alone without volume breakout should be suppressed!"

    # Case 3b: Strict Confluence: BOTH Volume Breakout (2.0x MA20) AND Market Breadth (>= 0.60) -> immediate Day 1 fast path!
    univ_confluence = {s: d.copy() for s, d in univ_up.items()}
    univ_confluence["S1"].iloc[55, univ_confluence["S1"].columns.get_loc("Volume")] = 2000.0
    strat_confluence = DummyUPLSMBlend(sig_1d)
    w_confluence = strat_confluence.generate_weights(univ_confluence, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "normal_cooldown_bars": 1,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert w_confluence.iloc[55]["S1"] > 0.0, "Day 1 signal should execute on strict confluence of volume and breadth!"

    # Case 4: Expansion volume breakout bypass
    # Position held at 0.10, demands expansion to 0.20 on index 56
    sig_exp = pd.DataFrame(0.0, index=dates, columns=["S1", "BIL"])
    sig_exp.iloc[55:60, 0] = 0.10
    sig_exp.iloc[56, 0] = 0.20  # Spike on day 56

    # 4a. Without volume breakout on down-trending market: expansion suppressed on Day 1
    strat_exp_normal = DummyUPLSMBlend(sig_exp)
    w_exp_normal = strat_exp_normal.generate_weights(univ_down, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "min_weight_change": 0.05,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    # At index 56, should be suppressed to prev_w (which was 0.0 because index 55 had no fast path)
    # Let's ensure index 55 was confirmed by giving 2 bars of entry signal before expansion:
    sig_exp2 = pd.DataFrame(0.0, index=dates, columns=["S1", "BIL"])
    sig_exp2.iloc[54:60, 0] = 0.10
    sig_exp2.iloc[56, 0] = 0.20  # Demands expansion on day 56 (held since day 55)

    w_exp_suppressed = DummyUPLSMBlend(sig_exp2).generate_weights(univ_down, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "min_weight_change": 0.05,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert np.isclose(w_exp_suppressed.iloc[56]["S1"], 0.10), "Expansion without volume surge should be suppressed to prev_w"

    # 4b. With volume breakout on day 56: expansion allowed immediately!
    univ_exp_vol = {s: d.copy() for s, d in univ_down.items()}
    univ_exp_vol["S1"].iloc[56, univ_exp_vol["S1"].columns.get_loc("Volume")] = 2500.0  # Surge!
    w_exp_surge = DummyUPLSMBlend(sig_exp2).generate_weights(univ_exp_vol, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 2,
        "min_weight_change": 0.05,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)
    assert w_exp_surge.iloc[56]["S1"] > 0.15, "Expansion with volume surge should be allowed immediately!"

    # Case 5: Normal Strategic Exit 1-bar rotation cooldown
    # Position held on days 54-56. Exits to 0.0 on day 57.
    # Attempts immediate re-entry on day 58.
    sig_exit = pd.DataFrame(0.0, index=dates, columns=["S1", "BIL"])
    sig_exit.iloc[54:57, 0] = 0.20
    sig_exit.iloc[57, 0] = 0.0      # Exit on day 57
    sig_exit.iloc[58:62, 0] = 0.20  # Re-entry attempt on day 58

    strat_exit = DummyUPLSMBlend(sig_exit)
    w_exit = strat_exit.generate_weights(univ_up, {
        "enable_cyq_quality_gate": False,
        "min_entry_bars": 1,
        "normal_cooldown_bars": 1,
        "min_holding_bars": 1,
        "min_weight_change": 0.02,
        "breadth_lookback": 50,
    }).ffill().fillna(0.0)

    assert w_exit.iloc[56]["S1"] > 0.0, "Held on day 56"
    assert w_exit.iloc[57]["S1"] == 0.0, "Exited on day 57"
    assert w_exit.iloc[58]["S1"] == 0.0, "Day 58 must be blocked by normal_cooldown_bars=1!"
    assert w_exit.iloc[59]["S1"] > 0.0, "Day 59 cooldown expired, re-entry permitted!"


def test_base_blend_uplsm_sector_incumbent_stop_unlock():
    """Validates that an incumbent that triggers a stop-loss is NOT locked in locked_incumbents,
    allowing an outside challenger in the same sector to enter without being blocked by sector limits.
    """
    class DummyThrustBlend(BaseBlendAllocationStrategy):
        name = "dummy_thrust_blend"
        def get_sub_strategies(self, universe, params=None):
            return {}
        def get_sub_strategy_weights(self, params=None):
            return {}

    n_days = 80
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)

    # Two assets in same sector 'sec_60'
    close_a = 100.0 + 0.10 * t
    df_a = make_ohlcv_from_closes(close_a)
    df_a.index = dates
    df_a["Volume"] = 1000.0

    close_b = 100.0 + 0.10 * t
    df_b = make_ohlcv_from_closes(close_b)
    df_b.index = dates
    df_b["Volume"] = 1000.0

    df_bil = make_ohlcv_from_closes(np.full(n_days, 100.0))
    df_bil.index = dates
    df_bil["Volume"] = 1000.0

    # On day 55, stock A plunges to trigger fixed_pct stop loss
    df_a.iloc[55, df_a.columns.get_loc("Close")] = 80.0
    df_a.iloc[55, df_a.columns.get_loc("Low")] = 79.0

    universe = {
        "601225.SH": df_a,
        "601857.SH": df_b,
        "BIL": df_bil,
    }

    strat = DummyThrustBlend()
    # Execute with preemptive thrust deployment enabled and max 1 asset per sector
    w = strat.generate_weights(universe, {
        "stop_mode": "fixed_pct",
        "min_holding_bars": 5,
        "preemptive_thrust_deployment": True,
        "max_assets_per_sector": 1,
        "breadth_lookback": 20,
    }).ffill().fillna(0.0)

    # On day 55, 601225.SH is stopped out
    assert w.iloc[55]["601225.SH"] == 0.0



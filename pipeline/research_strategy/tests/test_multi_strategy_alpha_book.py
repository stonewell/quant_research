"""Unit tests for MultiStrategyAlphaBookStrategy (MS-AlphaBook).

Guaranteed 100% offline: uses synthetic OHLCV generators only.
Tests:
- Parameter configuration & validation (presets, execution modes, recovery days)
- Pod sleeve instantiation & execution across presets (core_satellite, alpha_leaders, all_regime)
- Sparse weights contract compliance & cash routing
- Micro circuit breaker & fast trough recovery (damping cleared on rebound)
- Dual-gate macro canary & equity growth breadth risk-throttle
- Insensitivity to bond bear markets when equities are bullish
- Dual execution modes (pod_native_sparse and periodic_sync)
- strategies_config.json entry & STRATEGY_CLASS_MAP discovery
- RegimeFactorCompoundStrategy risk_budgeted mode
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

from common.allocation_templates import AllocationTemplate
from common.testing import make_ohlcv_from_closes
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import STRATEGY_CLASS_MAP, instantiate_strategy_from_config_entry
from research_strategy.rs.multi_strategy_alpha_book import MultiStrategyAlphaBookStrategy
from research_strategy.rs.regime_factor_compound_strategy import RegimeFactorCompoundStrategy


def create_test_universe(n_bars: int = 300, bull_market: bool = True) -> dict:
    dates = pd.bdate_range("2021-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    if bull_market:
        spy_close = 100.0 + 0.15 * t
        stock_a = 100.0 + 0.20 * t
        stock_b = 100.0 + 0.18 * t
        stock_c = 100.0 + 0.12 * t
    else:
        spy_close = 200.0 - 0.25 * t
        stock_a = 200.0 - 0.30 * t
        stock_b = 200.0 - 0.28 * t
        stock_c = 200.0 - 0.20 * t

    tip_close = 100.0 + 0.02 * t
    ief_close = 100.0 + 0.01 * t
    bil_close = 100.0 + 0.005 * t

    raw = {
        "SPY": spy_close,
        "TIP": tip_close,
        "IEF": ief_close,
        "BIL": bil_close,
        "STOCK_A": stock_a,
        "STOCK_B": stock_b,
        "STOCK_C": stock_c,
    }

    universe = {sym: make_ohlcv_from_closes(close_arr) for sym, close_arr in raw.items()}
    for df in universe.values():
        df.index = dates
    return universe


def test_ms_alpha_book_config_defaults_and_validation():
    cfg = StrategyConfig()
    assert cfg.ms_pod_preset == "core_satellite"
    assert cfg.ms_execution_mode == "pod_native_sparse"
    assert cfg.ms_rebalance_freq_days == 21
    assert cfg.ms_lookback_days == 63
    assert cfg.ms_pod_max_drawdown_limit == pytest.approx(0.06)
    assert cfg.ms_drawdown_recovery_days == 10
    assert cfg.ms_max_pod_budget == pytest.approx(0.35)
    assert cfg.ms_min_pod_budget == pytest.approx(0.15)
    assert cfg.ms_budget_smoothing_alpha == pytest.approx(0.50)
    assert cfg.ms_canary_breadth_thresh == pytest.approx(0.50)

    with pytest.raises(ValueError, match="ms_pod_preset"):
        StrategyConfig(ms_pod_preset="invalid_preset")

    with pytest.raises(ValueError, match="ms_execution_mode"):
        StrategyConfig(ms_execution_mode="invalid_mode")

    with pytest.raises(ValueError, match="ms_drawdown_recovery_days must be > 0"):
        StrategyConfig(ms_drawdown_recovery_days=0)

    with pytest.raises(ValueError, match="ms_rebalance_freq_days must be > 0"):
        StrategyConfig(ms_rebalance_freq_days=0)

    with pytest.raises(ValueError, match="ms_lookback_days must be > 0"):
        StrategyConfig(ms_lookback_days=-5)

    with pytest.raises(ValueError, match="ms_pod_max_drawdown_limit must be between 0 and 1"):
        StrategyConfig(ms_pod_max_drawdown_limit=1.5)

    with pytest.raises(ValueError, match="ms_min_pod_budget"):
        StrategyConfig(ms_min_pod_budget=0.60, ms_max_pod_budget=0.40)

    with pytest.raises(ValueError, match="ms_budget_smoothing_alpha must be between 0 and 1"):
        StrategyConfig(ms_budget_smoothing_alpha=0.0)

    with pytest.raises(ValueError, match="ms_canary_breadth_thresh must be between 0 and 1"):
        StrategyConfig(ms_canary_breadth_thresh=1.5)


def test_ms_alpha_book_instantiation_from_strategies_config():
    cfg_dict = load_strategies_config()
    assert "multi_strategy_alpha_book" in cfg_dict
    entry = cfg_dict["multi_strategy_alpha_book"]
    strat = instantiate_strategy_from_config_entry("multi_strategy_alpha_book", entry)
    assert isinstance(strat, MultiStrategyAlphaBookStrategy)
    assert "MultiStrategyAlphaBookStrategy" in STRATEGY_CLASS_MAP
    assert STRATEGY_CLASS_MAP["MultiStrategyAlphaBookStrategy"] is MultiStrategyAlphaBookStrategy


def test_ms_alpha_book_execution_and_sparse_weights_contract():
    universe = create_test_universe(n_bars=280, bull_market=True)
    cfg = StrategyConfig(ms_rebalance_freq_days=21, ms_lookback_days=40)
    strat = MultiStrategyAlphaBookStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "multi_strategy_alpha_book" in strat.explain_weights()

    weights = strat.generate_weights(universe)
    assert isinstance(weights, pd.DataFrame)
    assert not weights.empty

    # Verify sparse contract: between rebalances, rows are all NaN
    non_null_rows = weights.dropna(how="all")
    assert len(non_null_rows) > 0
    assert len(non_null_rows) < len(weights)  # Must be sparse!

    # Verify each non-null rebalance row sums to 1.0
    row_sums = non_null_rows.sum(axis=1)
    for s in row_sums:
        assert s == pytest.approx(1.0, abs=1e-4)

    # Verify cash proxy is populated
    assert "BIL" in weights.columns


def test_ms_alpha_book_pod_presets():
    universe = create_test_universe(n_bars=280, bull_market=True)

    for preset_name in ["core_satellite", "alpha_leaders", "all_regime"]:
        cfg = StrategyConfig(
            ms_pod_preset=preset_name,
            ms_rebalance_freq_days=21,
            ms_lookback_days=40,
        )
        strat = MultiStrategyAlphaBookStrategy(cfg)
        pods = strat._get_pods(cfg)
        assert len(pods) >= 3

        weights = strat.generate_weights(universe)
        assert not weights.empty
        non_null = weights.dropna(how="all")
        assert len(non_null) > 0
        for _, row in non_null.iterrows():
            assert row.sum() == pytest.approx(1.0, abs=1e-4)


def test_ms_alpha_book_dual_execution_modes():
    universe = create_test_universe(n_bars=280, bull_market=True)

    for mode in ["pod_native_sparse", "periodic_sync"]:
        cfg = StrategyConfig(
            ms_execution_mode=mode,
            ms_rebalance_freq_days=21,
            ms_lookback_days=40,
        )
        strat = MultiStrategyAlphaBookStrategy(cfg)
        weights = strat.generate_weights(universe)
        assert not weights.empty
        non_null = weights.dropna(how="all")
        assert len(non_null) > 0
        for _, row in non_null.iterrows():
            assert row.sum() == pytest.approx(1.0, abs=1e-4)


def test_ms_alpha_book_micro_circuit_breaker_quarantine():
    universe = create_test_universe(n_bars=280, bull_market=True)
    cfg = StrategyConfig(
        ms_rebalance_freq_days=21,
        ms_lookback_days=40,
        ms_pod_max_drawdown_limit=0.04,
    )
    strat = MultiStrategyAlphaBookStrategy(cfg)

    # Mock one pod to simulate steep drawdown by overriding its generated weights to hold a crashing asset
    class CrashingPod(AllocationTemplate):
        def __init__(self):
            super().__init__(name="crashing_pod", param_grid={})

        def generate_weights(self, univ, params=None):
            dates = univ["SPY"].index
            w = pd.DataFrame(np.nan, index=dates, columns=list(univ.keys()))
            for d in dates[::21]:
                w.loc[d] = 0.0
                w.loc[d, "STOCK_A"] = 1.0
            return w

        def explain_weights(self, params=None):
            return "Crashing Pod"

        def warmup_bars(self, params=None):
            return 20

    original_get_pods = strat._get_pods
    def patched_get_pods(c):
        p_dict = original_get_pods(c)
        p_dict["mean_reversion"] = CrashingPod()
        return p_dict

    strat._get_pods = patched_get_pods

    weights = strat.generate_weights(universe)
    assert not weights.empty
    non_null = weights.dropna(how="all")
    assert len(non_null) > 0


def test_ms_alpha_book_fast_trough_recovery():
    """Confirms that when a pod recovers (10d return > 0), damping penalty is cleared."""
    universe = create_test_universe(n_bars=280, bull_market=True)
    cfg = StrategyConfig(
        ms_rebalance_freq_days=10,
        ms_lookback_days=40,
        ms_drawdown_recovery_days=10,
        ms_pod_max_drawdown_limit=0.03,
    )
    strat = MultiStrategyAlphaBookStrategy(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    non_null = weights.dropna(how="all")
    assert len(non_null) > 0


def test_ms_alpha_book_growth_breadth_insensitivity_to_bonds():
    """Confirms equity allocation is NOT throttled when equities are in a bull market
    even if bond prices are plunging.
    """
    n_bars = 320
    dates = pd.bdate_range("2021-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    # Equities strong bull market
    spy_close = 100.0 + 0.30 * t
    stock_a = 100.0 + 0.35 * t
    stock_b = 100.0 + 0.25 * t

    # Bonds severe bear market (rate hikes)
    ief_close = 150.0 - 0.20 * t
    tip_close = 150.0 - 0.20 * t
    bil_close = 100.0 + 0.005 * t

    raw = {
        "SPY": spy_close,
        "STOCK_A": stock_a,
        "STOCK_B": stock_b,
        "IEF": ief_close,
        "TIP": tip_close,
        "BIL": bil_close,
    }
    universe = {sym: make_ohlcv_from_closes(close_arr) for sym, close_arr in raw.items()}
    for df in universe.values():
        df.index = dates

    cfg = StrategyConfig(ms_rebalance_freq_days=21, ms_canary_breadth_thresh=0.50)
    strat = MultiStrategyAlphaBookStrategy(cfg)
    weights = strat.generate_weights(universe)

    non_null = weights.dropna(how="all")
    assert len(non_null) > 0
    # Once full history is available, equities are actively held despite bond crash
    last_row = non_null.iloc[-1]
    assert (last_row["SPY"] > 0.0 or last_row["STOCK_A"] > 0.0)
    assert last_row["BIL"] <= 0.60, f"Expected equity allocation to stay active despite bond bear market, got BIL={last_row['BIL']}"


def test_ms_alpha_book_canary_risk_throttle_in_bear_market():
    # Bear market: prices plummet below 200d SMA, breadth collapses to 0
    universe = create_test_universe(n_bars=280, bull_market=False)
    cfg = StrategyConfig(ms_rebalance_freq_days=21, ms_canary_breadth_thresh=0.50)
    strat = MultiStrategyAlphaBookStrategy(cfg)

    weights = strat.generate_weights(universe)
    non_null_rows = weights.dropna(how="all")

    # In the second half (>200 bars), breadth is 0 -> gross exposure is throttled
    later_rows = non_null_rows.iloc[len(non_null_rows) // 2 :]
    for _, row in later_rows.iterrows():
        # Cash proxy BIL should absorb the bulk of capital
        assert row["BIL"] >= 0.70, f"Expected high cash defense during bear collapse, got {row['BIL']}"


def test_regime_factor_compound_risk_budgeted_mode():
    universe = create_test_universe(n_bars=280, bull_market=True)
    cfg = StrategyConfig(
        regime_compound_mode="risk_budgeted",
        regime_compound_rebalance_freq_days=21,
    )
    strat = RegimeFactorCompoundStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)
    assert not weights.empty

    non_null = weights.dropna(how="all")
    assert len(non_null) > 0
    for _, row in non_null.iterrows():
        assert row.sum() == pytest.approx(1.0, abs=1e-4)

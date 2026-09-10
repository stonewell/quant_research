"""Unit tests for AdaptiveFastExpansionStrategy (AFE).

Guaranteed 100% offline: uses synthetic OHLCV generators only.
Tests:
- Parameter configuration & validation
- Dual-horizon momentum scoring
- Breadth thrust fast-recovery activation
- Barroso/Santa-Clara volatility-targeted scaling
- Sparse weights contract & cash routing
- Registry & config loading
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
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import STRATEGY_CLASS_MAP, instantiate_strategy_from_config_entry
from research_strategy.rs.adaptive_fast_expansion_strategy import AdaptiveFastExpansionStrategy


def create_test_universe(n_bars: int = 300, breadth_thrust: bool = True) -> dict:
    dates = pd.bdate_range("2021-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    # SPY benchmark
    spy_trend = 0.10 * t
    spy_close = 100.0 + spy_trend

    # Canary assets
    tip_close = 100.0 + 0.05 * t
    ief_close = 100.0 + 0.02 * t
    bil_close = 100.0 + 0.005 * t

    # Risky equities
    if breadth_thrust:
        # All stocks sharply accelerating over the last 30 bars
        stock_a = 100.0 + 0.05 * t + (0.3 * (t - 270)).clip(min=0)
        stock_b = 100.0 + 0.08 * t + (0.4 * (t - 270)).clip(min=0)
        stock_c = 100.0 + 0.03 * t + (0.2 * (t - 270)).clip(min=0)
    else:
        # Sharp downtrend / deceleration
        stock_a = 100.0 - 0.1 * t
        stock_b = 100.0 - 0.08 * t
        stock_c = 100.0 - 0.05 * t

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


def test_afe_strategy_config_defaults_and_validation():
    cfg = StrategyConfig()
    assert cfg.afe_fast_roc_days == 15
    assert cfg.afe_slow_roc_days == 126
    assert cfg.afe_target_vol == 0.12
    assert cfg.afe_top_k == 3
    assert cfg.afe_breadth_thrust_thresh == 0.65
    assert cfg.afe_rebalance_freq_days == 10

    # Validation errors
    with pytest.raises(ValueError, match="afe_fast_roc_days"):
        StrategyConfig(afe_fast_roc_days=0)

    with pytest.raises(ValueError, match="must be < afe_slow_roc_days"):
        StrategyConfig(afe_fast_roc_days=150, afe_slow_roc_days=100)

    with pytest.raises(ValueError, match="afe_target_vol"):
        StrategyConfig(afe_target_vol=-0.05)

    with pytest.raises(ValueError, match="afe_breadth_thrust_thresh"):
        StrategyConfig(afe_breadth_thrust_thresh=1.5)


def test_afe_strategy_breadth_thrust_allocation():
    universe = create_test_universe(n_bars=300, breadth_thrust=True)
    strat = AdaptiveFastExpansionStrategy()

    assert strat.warmup_bars() == 200
    assert "Adaptive Fast Expansion" in strat.explain_weights()

    weights = strat.generate_weights(universe, params={
        "afe_fast_roc_days": 15,
        "afe_slow_roc_days": 126,
        "afe_target_vol": 0.12,
        "afe_top_k": 2,
        "afe_breadth_thrust_thresh": 0.60,
        "afe_rebalance_freq_days": 10,
        "cash_proxy": "BIL",
    })

    assert not weights.empty
    # Sparse weights contract: NaNs on non-rebalance days
    rebal_rows = weights.dropna(how="all")
    assert len(rebal_rows) > 0

    last_row = rebal_rows.iloc[-1]
    # In breadth thrust mode, top stocks are selected and allocated
    assert last_row["STOCK_A"] > 0 or last_row["STOCK_B"] > 0
    # Every rebalance row must sum to 1.0 (with BIL absorbing unallocated remainder)
    for _, row in rebal_rows.iterrows():
        assert row.sum() == pytest.approx(1.0, abs=1e-4)


def test_afe_strategy_downtrend_cash_protection():
    universe = create_test_universe(n_bars=300, breadth_thrust=False)
    strat = AdaptiveFastExpansionStrategy()

    weights = strat.generate_weights(universe, params={
        "afe_fast_roc_days": 15,
        "afe_slow_roc_days": 126,
        "afe_target_vol": 0.12,
        "afe_top_k": 2,
        "afe_breadth_thrust_thresh": 0.65,
        "afe_rebalance_freq_days": 10,
        "cash_proxy": "BIL",
    })

    assert not weights.empty
    rebal_rows = weights.dropna(how="all")
    last_row = rebal_rows.iloc[-1]

    # In downtrend without breadth thrust or trend alignment, risky assets should be 0.0
    assert last_row["STOCK_A"] == 0.0
    assert last_row["STOCK_B"] == 0.0
    assert last_row["STOCK_C"] == 0.0
    # 100% routed to BIL cash proxy
    assert last_row["BIL"] == pytest.approx(1.0, abs=1e-4)


def test_afe_instantiation_from_strategies_config():
    configs = load_strategies_config()
    assert "adaptive_fast_expansion" in configs

    inst = instantiate_strategy_from_config_entry(
        "adaptive_fast_expansion",
        configs["adaptive_fast_expansion"]
    )
    assert isinstance(inst, AdaptiveFastExpansionStrategy)
    assert "AdaptiveFastExpansionStrategy" in STRATEGY_CLASS_MAP

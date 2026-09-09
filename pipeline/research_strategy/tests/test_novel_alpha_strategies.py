"""Unit and Integration Tests for Novel High-Alpha Strategies:
- HybridAssetAllocationStrategy (HAA)
- DefensiveAssetAllocationStrategy (DAA)
- ResidualMomentumStrategy (ResMom)
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
from research_strategy.rs.strategy import instantiate_strategy_from_config_entry
from research_strategy.rs.taa_strategies import (
    DefensiveAssetAllocationStrategy,
    HybridAssetAllocationStrategy,
    score_13612w,
)
from research_strategy.rs.residual_momentum_strategy import ResidualMomentumStrategy


def create_taa_mock_universe(n_bars: int = 350, tip_bullish: bool = True) -> dict:
    dates = pd.bdate_range("2020-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    # TIP canary
    tip_trend = 0.05 if tip_bullish else -0.05
    tip_close = 100.0 + tip_trend * t

    # Offensive / Risky assets
    spy_close = 100.0 + 0.15 * t
    qqq_close = 100.0 + 0.20 * t
    iwm_close = 100.0 + 0.10 * t
    efa_close = 100.0 + 0.08 * t
    eem_close = 100.0 - 0.05 * t  # Negative momentum asset
    vwo_close = 100.0 + (0.05 if tip_bullish else -0.05) * t
    bnd_close = 100.0 + (0.03 if tip_bullish else -0.03) * t

    # Defensive assets
    ief_close = 100.0 + 0.02 * t
    bil_close = 100.0 + 0.005 * t
    lqd_close = 100.0 + 0.03 * t

    raw = {
        "TIP": tip_close,
        "SPY": spy_close,
        "QQQ": qqq_close,
        "IWM": iwm_close,
        "EFA": efa_close,
        "EEM": eem_close,
        "VWO": vwo_close,
        "BND": bnd_close,
        "IEF": ief_close,
        "BIL": bil_close,
        "LQD": lqd_close,
    }

    universe = {}
    for sym, c in raw.items():
        df = make_ohlcv_from_closes(c)
        df.index = dates
        universe[sym] = df

    return universe


# --- Test 13612W Momentum Metric ---

def test_score_13612w_calculation():
    # 300 bars of strictly increasing prices
    close = pd.Series(np.linspace(100.0, 200.0, 300))
    score = score_13612w(close)
    # The score at the end should be strictly positive
    assert score.iloc[-1] > 0
    # Falling price should yield strictly negative score
    close_down = pd.Series(np.linspace(200.0, 100.0, 300))
    score_down = score_13612w(close_down)
    assert score_down.iloc[-1] < 0


# --- Test Hybrid Asset Allocation (HAA) ---

def test_haa_bullish_and_defensive_modes():
    # Bullish scenario: TIP is rising
    univ_bull = create_taa_mock_universe(n_bars=350, tip_bullish=True)
    strat_haa = HybridAssetAllocationStrategy()
    weights_bull = strat_haa.generate_weights(univ_bull, params={
        "haa_canary_symbol": "TIP",
        "haa_top_k": 4,
        "haa_rebalance_freq_days": 21,
        "haa_offensive_universe": ["SPY", "QQQ", "IWM", "EFA", "EEM"],
        "haa_defensive_universe": ["IEF", "BIL"],
        "cash_proxy": "BIL",
    })

    assert not weights_bull.empty
    last_rebal = weights_bull.dropna(how="all").iloc[-1]
    # In bullish mode, top offensive assets like QQQ, SPY, IWM should hold weight
    assert last_rebal["QQQ"] > 0
    assert last_rebal["SPY"] > 0
    # Negative momentum asset EEM should have 0 weight
    assert last_rebal["EEM"] == 0.0

    # Bearish scenario: TIP is falling
    univ_bear = create_taa_mock_universe(n_bars=350, tip_bullish=False)
    weights_bear = strat_haa.generate_weights(univ_bear, params={
        "haa_canary_symbol": "TIP",
        "haa_top_k": 4,
        "haa_rebalance_freq_days": 21,
        "haa_offensive_universe": ["SPY", "QQQ", "IWM", "EFA", "EEM"],
        "haa_defensive_universe": ["IEF", "BIL"],
        "cash_proxy": "BIL",
    })

    last_bear_rebal = weights_bear.dropna(how="all").iloc[-1]
    # In defensive mode, offensive assets should have 0 weight, defensive assets hold 100%
    assert last_bear_rebal["SPY"] == 0.0
    assert last_bear_rebal["QQQ"] == 0.0
    assert last_bear_rebal["IEF"] + last_bear_rebal["BIL"] == pytest.approx(1.0, abs=1e-4)


# --- Test Defensive Asset Allocation (DAA) ---

def test_daa_multi_tier_crash_protection():
    strat_daa = DefensiveAssetAllocationStrategy()

    # Case 1: 0 bad canaries (VWO & BND both bullish)
    univ_safe = create_taa_mock_universe(n_bars=350, tip_bullish=True)
    weights_safe = strat_daa.generate_weights(univ_safe, params={
        "daa_canary_universe": ["VWO", "BND"],
        "daa_top_k": 4,
        "daa_rebalance_freq_days": 21,
        "daa_risky_universe": ["SPY", "QQQ", "IWM", "EFA"],
        "daa_defensive_universe": ["IEF", "BIL"],
        "cash_proxy": "BIL",
    })
    last_safe = weights_safe.dropna(how="all").iloc[-1]
    # 100% in risky assets
    assert (last_safe["SPY"] + last_safe["QQQ"] + last_safe["IWM"] + last_safe["EFA"]) == pytest.approx(1.0, abs=1e-4)

    # Case 2: 2 bad canaries (VWO & BND both bearish)
    univ_danger = create_taa_mock_universe(n_bars=350, tip_bullish=False)
    weights_danger = strat_daa.generate_weights(univ_danger, params={
        "daa_canary_universe": ["VWO", "BND"],
        "daa_top_k": 4,
        "daa_rebalance_freq_days": 21,
        "daa_risky_universe": ["SPY", "QQQ", "IWM", "EFA"],
        "daa_defensive_universe": ["IEF", "BIL"],
        "cash_proxy": "BIL",
    })
    last_danger = weights_danger.dropna(how="all").iloc[-1]
    # 100% in defensive assets
    assert (last_danger["IEF"] + last_danger["BIL"] + last_danger["LQD"]) == pytest.approx(1.0, abs=1e-4)


# --- Test Residual Momentum Strategy ---

def test_residual_momentum_generation_and_vol_scaling():
    n_bars = 350
    dates = pd.bdate_range("2020-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    # Benchmark: SPY moderate trend
    spy = 100.0 + 0.1 * t
    # Stock A: High positive idiosyncratic alpha over SPY
    stock_a = 100.0 + 0.25 * t + 2.0 * np.sin(t / 5.0)
    # Stock B: Just tracks SPY with zero idiosyncratic alpha
    stock_b = 100.0 + 0.1 * t
    # Stock C: Negative alpha, downtrend below 200d SMA
    stock_c = 100.0 - 0.1 * t
    # Cash
    bil = 100.0 + 0.001 * t

    universe = {
        "SPY": make_ohlcv_from_closes(spy),
        "STOCK_A": make_ohlcv_from_closes(stock_a),
        "STOCK_B": make_ohlcv_from_closes(stock_b),
        "STOCK_C": make_ohlcv_from_closes(stock_c),
        "BIL": make_ohlcv_from_closes(bil),
    }
    for df in universe.values():
        df.index = dates

    strat_resmom = ResidualMomentumStrategy()
    assert strat_resmom.warmup_bars() >= 252
    assert "Residual Momentum" in strat_resmom.explain_weights()

    weights = strat_resmom.generate_weights(universe, params={
        "resmom_lookback_days": 252,
        "resmom_skip_days": 21,
        "resmom_benchmark_symbol": "SPY",
        "resmom_top_k": 2,
        "resmom_target_vol": 0.12,
        "resmom_rebalance_freq_days": 21,
        "resmom_require_trend_filter": True,
        "cash_proxy": "BIL",
    })

    assert not weights.empty
    rebal_rows = weights.dropna(how="all")
    last_row = rebal_rows.iloc[-1]

    # STOCK_A has the highest positive idiosyncratic alpha and should be selected
    assert last_row["STOCK_A"] > 0
    # STOCK_C is in a downtrend and fails the 200d trend filter
    assert last_row["STOCK_C"] == 0.0
    # Total row sum must equal 1.0 (with cash proxy absorbing remainder)
    assert last_row.sum() == pytest.approx(1.0, abs=1e-4)


# --- Test Config File Instantiation of All 3 Novel Strategies ---

def test_instantiate_all_novel_strategies_from_config():
    configs = load_strategies_config()
    for strat_key, expected_cls in [
        ("hybrid_asset_allocation", HybridAssetAllocationStrategy),
        ("defensive_asset_allocation", DefensiveAssetAllocationStrategy),
        ("residual_momentum", ResidualMomentumStrategy),
    ]:
        assert strat_key in configs
        inst = instantiate_strategy_from_config_entry(strat_key, configs[strat_key])
        assert isinstance(inst, expected_cls)

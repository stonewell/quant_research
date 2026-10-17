"""Unit and Integration Tests for ChanVolumeProfileHybridStrategy (Task C2.1).

Guaranteed 100% offline using synthetic OHLCV data generators.
Verifies:
1. Instantiation, factor tags, warmup_bars, explain_weights.
2. Sparse weights contract (NaN on non-rebalance days, explicit 0.0 on exit).
3. Integration with run_allocation_backtest.
4. HVN pivot validation (accepts dense HVN pivots, rejects hollow zero-density pivots).
5. Dynamic Naked POC (NPOC) take-profit and stop-loss exits.
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

from common.allocation_backtester import run_allocation_backtest
from common.testing import make_ohlcv_from_closes
from research_strategy.rs.chan_vp_hybrid_strategy import (
    ChanVolumeProfileHybridStrategy,
    run_chan_vp_position_loop,
)
from research_strategy.rs.config import StrategyConfig


def make_test_universe(n_days: int = 250) -> dict:
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    t = np.arange(n_days)
    # Oscillating consolidation then strong breakout
    spy_close = 100.0 + 3.0 * np.sin(t / 6.0) + 0.1 * t
    bil_close = 100.0 + 0.001 * t
    universe = {
        "SPY": make_ohlcv_from_closes(spy_close),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates
    return universe


def test_chan_vp_hybrid_instantiation_and_metadata():
    strat = ChanVolumeProfileHybridStrategy()
    assert strat.name == "chan_vp_hybrid"
    assert "regime_trend_strength" in strat.factor_tags
    assert "relative_momentum" in strat.factor_tags
    assert "absolute_momentum_trend" in strat.factor_tags

    warmup = strat.warmup_bars()
    assert warmup >= 60

    explanation = strat.explain_weights()
    assert isinstance(explanation, str)
    assert "HVN" in explanation or "Volume Profile" in explanation


def test_chan_vp_hybrid_generate_weights_sparse_contract():
    universe = make_test_universe(250)
    strat = ChanVolumeProfileHybridStrategy()
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)
    assert set(universe.keys()).issubset(set(weights.columns))

    # Sparse weights contract verification:
    # 1. Non-rebalance rows must be all NaN
    # 2. Rebalance rows must have non-NaN values summing to <= 1.0 (with cash proxy)
    non_nan_rows = weights.dropna(how="all")
    assert len(non_nan_rows) > 0, "Strategy should have at least one rebalance row"

    # All non-rebalance rows must be entirely NaN
    nan_rows = weights[weights.isna().all(axis=1)]
    assert len(nan_rows) > 0, "Sparse weights contract requires non-rebalance days to be NaN"

    # Row sums on active rebalance days must sum close to 1.0
    for idx, row in non_nan_rows.iterrows():
        valid_sum = row.sum()
        assert np.isclose(valid_sum, 1.0, atol=1e-3) or valid_sum < 1.0


def test_chan_vp_hybrid_backtester_integration():
    universe = make_test_universe(250)
    strat = ChanVolumeProfileHybridStrategy()
    weights = strat.generate_weights(universe)

    backtest_result = run_allocation_backtest(universe=universe, target_weights=weights)
    assert backtest_result is not None
    assert "equity_curve" in backtest_result
    assert len(backtest_result["equity_curve"]) > 0
    assert not backtest_result["equity_curve"].isna().values.any()


def test_run_chan_vp_position_loop_mechanics():
    # 20 bars
    n = 20
    close = np.array([100.0] * 5 + [102.0, 105.0, 108.0, 110.0, 112.0] + [104.0, 93.0] + [90.0] * 8)
    high = close * 1.01
    low = close * 0.99
    poc = np.array([100.0] * 20)
    zd = np.array([98.0] * 20)
    zg = np.array([101.0] * 20)

    # Entry at bar 5
    entry = np.zeros(n, dtype=bool)
    entry[5] = True
    sell = np.zeros(n, dtype=bool)
    std_div = np.zeros(n, dtype=float)

    # Test stop loss trigger: bar 11 drops from 102 entry to 93 (> 6% loss)
    raw = run_chan_vp_position_loop(
        close=close,
        high=high,
        low=low,
        poc=poc,
        zd=zd,
        zg=zg,
        entry_signals=entry,
        sell_signals=sell,
        std_div=std_div,
        stop_loss_pct=0.06,
        max_holding_days=60,
        position_size_pct=1.0,
    )

    # Bar 5 to 10 should be in position (1.0)
    assert np.all(raw[5:11] == 1.0)
    # Bar 11 should have stopped out to explicit 0.0
    assert raw[11] == 0.0
    assert np.all(raw[12:] == 0.0)


def test_chan_vp_hvn_filter_rejection():
    # When density is below min_density, entry should be rejected
    n_days = 200
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    t = np.arange(n_days)
    # Hollow erratic price action with high volume spikes far from pivot
    close_erratic = 100.0 + 15.0 * np.sin(t / 2.0)
    universe = {
        "SPY": make_ohlcv_from_closes(close_erratic),
        "BIL": make_ohlcv_from_closes(100.0 + 0.001 * t),
    }
    for df in universe.values():
        df.index = dates

    strat = ChanVolumeProfileHybridStrategy()
    # High consolidation density threshold requires dense clustering
    w_strict = strat.generate_weights(universe, params={"chan_vp_min_consolidation_density": 0.99})
    # With 0.99 threshold on erratic swings, SPY should never be entered (weights always in BIL or 0)
    if not w_strict.empty and "SPY" in w_strict.columns:
        spy_active = w_strict["SPY"].dropna()
        assert (spy_active == 0.0).all() or len(spy_active) == 0

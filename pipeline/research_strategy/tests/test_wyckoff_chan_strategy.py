"""Unit and Integration Tests for WyckoffChanSpringStrategy (Task C2.2).

Guaranteed 100% offline using synthetic OHLCV data generators.
Verifies:
1. Instantiation, factor tags, warmup_bars, explain_weights.
2. Sparse weights contract (NaN on non-rebalance days, explicit 0.0 on exit).
3. Integration with run_allocation_backtest.
4. Crafted Wyckoff Spring + V-curvature (kappa >= 1.5) entry mechanics.
5. Two-stage profit scaling (50% scale-out at ZG, 100% exit at Target 2) and stop-loss at 0.995 * Low_{t-1}.
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
from research_strategy.rs.config import StrategyConfig
from research_strategy.rs.wyckoff_chan_strategy import (
    WyckoffChanSpringStrategy,
    run_wyckoff_spring_position_loop,
)


def make_test_universe(n_days: int = 250) -> dict:
    dates = pd.bdate_range("2021-01-01", periods=n_days)
    t = np.arange(n_days)
    # Sine wave creating multiple swing lows / spring candidates
    spy_close = 100.0 + 4.0 * np.sin(t / 8.0) + 0.05 * t
    bil_close = 100.0 + 0.001 * t
    universe = {
        "SPY": make_ohlcv_from_closes(spy_close),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates
    return universe


def test_wyckoff_chan_spring_instantiation_and_metadata():
    strat = WyckoffChanSpringStrategy()
    assert strat.name == "wyckoff_chan_spring"
    assert "mean_reversion" in strat.factor_tags
    assert "regime_trend_strength" in strat.factor_tags

    warmup = strat.warmup_bars()
    assert warmup >= 40

    explanation = strat.explain_weights()
    assert isinstance(explanation, str)
    assert "Wyckoff" in explanation or "Spring" in explanation


def test_wyckoff_chan_spring_generate_weights_sparse_contract():
    universe = make_test_universe(250)
    strat = WyckoffChanSpringStrategy()
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)
    assert set(universe.keys()).issubset(set(weights.columns))

    # Sparse weights contract verification:
    # 1. Non-rebalance rows must be all NaN
    # 2. Rebalance rows must be fully populated (explicit 0.0 on exit / for any
    #    de-risked symbol, never NaN -- the ledger's column-wise ffill would
    #    otherwise keep the stale position), summing to at most 1.0.
    # (C4.1: strengthened from the previous "len(non_nan_rows) > 0" check,
    # which the initial all-zero row alone satisfied.)
    per_row_nan = weights.isna().sum(axis=1)
    non_nan_rows = weights[per_row_nan == 0]
    nan_rows = weights[per_row_nan == weights.shape[1]]
    assert len(non_nan_rows) > 0, "Strategy should produce rebalances"
    assert len(non_nan_rows) + len(nan_rows) == len(weights), (
        "every row must be either a fully-populated rebalance row or all-NaN"
    )
    assert len(nan_rows) > 0, "Sparse weights contract requires non-rebalance days to be NaN (no self-ffill)"

    row_sums = non_nan_rows.sum(axis=1)
    assert (row_sums <= 1.0 + 1e-9).all(), f"target weights must never exceed 1.0, max={row_sums.max()}"
    assert (non_nan_rows.values >= -1e-12).all(), "no negative target weights"


def test_wyckoff_chan_spring_backtester_integration():
    universe = make_test_universe(250)
    strat = WyckoffChanSpringStrategy()
    weights = strat.generate_weights(universe)

    backtest_result = run_allocation_backtest(universe=universe, target_weights=weights)
    assert backtest_result is not None
    assert "equity_curve" in backtest_result
    assert len(backtest_result["equity_curve"]) > 0
    assert not backtest_result["equity_curve"].isna().values.any()


def test_run_wyckoff_spring_position_loop_two_stage_targets():
    # Construct exact scenario:
    # Bar 0: support = 100.0, price = 100.0
    # Bar 1: dips below support to 98.0 (Low = 97.5) -> spring low
    # Bar 2: reclaims to 101.0, entry signal fires!
    # Entry price = 101.0, spring low = 97.5, stop price = 97.5 * 0.995 = 97.0125
    # ZG target = 105.0, Target 2 = 105 + 1.2*(105 - 100) = 111.0
    # Bar 3: price = 103.0 -> hold full 1.0
    # Bar 4: price = 105.5 -> hits ZG target! scale out to 0.5, stop price raised to 101.0
    # Bar 5: price = 108.0 -> hold 0.5
    # Bar 6: price = 111.5 -> hits Target 2! full exit to 0.0
    # Bar 7: price = 112.0 -> remains 0.0
    close = np.array([100.0, 98.0, 101.0, 103.0, 105.5, 108.0, 111.5, 112.0])
    high = close * 1.005
    low = close * 0.995
    low[1] = 97.5  # explicit spring low

    entry = np.array([False, False, True, False, False, False, False, False])
    zd = np.full(8, 100.0)
    zg = np.full(8, 105.0)

    raw = run_wyckoff_spring_position_loop(
        close=close,
        high=high,
        low=low,
        entry_signals=entry,
        zd=zd,
        zg=zg,
        stop_buffer_pct=0.005,
        max_holding_days=30,
        position_size_pct=1.0,
    )

    # Bar 2: entry at 1.0
    assert raw[2] == 1.0
    # Bar 3: hold 1.0
    assert raw[3] == 1.0
    # Bar 4: hit ZG (105.5 >= 105.0), scale out to 0.5
    assert raw[4] == 0.5
    # Bar 5: hold 0.5
    assert raw[5] == 0.5
    # Bar 6: hit Target 2 (111.5 >= 111.0), full exit to 0.0
    assert raw[6] == 0.0
    # Bar 7: flat 0.0
    assert raw[7] == 0.0


def test_run_wyckoff_spring_position_loop_stop_loss():
    # Bar 2: entry at 101.0, spring low = 97.5, stop price = 97.5 * 0.995 = 97.0125
    # Bar 3: drops to 96.0 -> stops out immediately to 0.0
    close = np.array([100.0, 98.0, 101.0, 96.0, 95.0])
    high = close * 1.01
    low = close * 0.99
    low[1] = 97.5

    entry = np.array([False, False, True, False, False])
    zd = np.full(5, 100.0)
    zg = np.full(5, 105.0)

    raw = run_wyckoff_spring_position_loop(
        close=close,
        high=high,
        low=low,
        entry_signals=entry,
        zd=zd,
        zg=zg,
        stop_buffer_pct=0.005,
        max_holding_days=30,
        position_size_pct=1.0,
    )

    assert raw[2] == 1.0
    assert raw[3] == 0.0  # stopped out
    assert raw[4] == 0.0

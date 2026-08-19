"""Unit tests for common/allocation_backtester.py."""

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.testing import make_ohlcv_from_closes as make_df


def test_allocation_backtester_disjoint_date_ranges_returns_empty_result():
    # Regression test: target_weights and the universe's OHLCV used to be
    # aligned via closes.index.intersection(target_weights.index) with no
    # check that the intersection is non-empty. When the two cover disjoint
    # date ranges (e.g. a template's rebalance schedule was computed against
    # a different calendar), `common_idx` came out zero-length, and
    # `equity[0] = initial_capital` raised an unhandled IndexError on the
    # zero-length `equity` array. This must instead short-circuit exactly
    # like the existing empty-target_weights/empty-universe cases.
    universe = {"A": make_df([100.0] * 10, start="2020-01-01")}

    # A completely disjoint date range, far in the future.
    disjoint_idx = pd.bdate_range("2030-01-01", periods=5)
    target_weights = pd.DataFrame(np.nan, index=disjoint_idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights)

    assert set(res.keys()) == {"equity_curve", "turnover"}
    assert res["equity_curve"].empty
    assert res["turnover"] == 0.0


def test_allocation_backtester_overlapping_date_ranges_still_runs():
    # Sanity check alongside the disjoint-range regression test above: a
    # partial overlap should NOT be treated as "no data" -- only a truly
    # empty intersection short-circuits.
    idx = pd.bdate_range("2020-01-01", periods=10)
    universe = {"A": make_df([100.0] * 10, start="2020-01-01")}

    # Overlaps the last 3 bars of the universe's calendar, then extends past it.
    overlap_idx = idx[-3:].append(pd.bdate_range(idx[-1] + pd.Timedelta(days=10), periods=2))
    target_weights = pd.DataFrame(np.nan, index=overlap_idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights)

    assert not res["equity_curve"].empty
    assert len(res["equity_curve"]) == 3

import json
import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

# Add backtester to path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.testing import make_ohlcv_from_closes as make_df
from backtester.run_backtest import _align_universe, _get_template, run_standard, run_walkforward


def test_align_universe():
    idx1 = pd.bdate_range("2020-01-01", periods=10)
    idx2 = pd.bdate_range("2020-01-05", periods=10)

    universe = {
        "A": pd.DataFrame({"Close": np.ones(10)}, index=idx1),
        "B": pd.DataFrame({"Close": np.ones(10)}, index=idx2),
    }

    aligned = _align_universe(universe)

    # Should be the intersection
    expected_idx = idx1.intersection(idx2)
    assert len(aligned["A"]) == len(expected_idx)
    assert len(aligned["B"]) == len(expected_idx)
    assert (aligned["A"].index == expected_idx).all()


def test_get_template():
    template = _get_template("equal_weight")
    assert template.name == "equal_weight"

    try:
        _get_template("non_existent")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


class MockArgs:
    def __init__(self, **kwargs):
        self.initial_capital = 100_000.0
        self.commission_pct = 0.0
        self.slippage_pct = 0.0
        self.window_years = 0.1
        self.step_years = 0.05
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_run_standard():
    idx = pd.bdate_range("2020-01-01", periods=100)
    universe = {
        "A": make_df(np.linspace(100, 200, 100), start="2020-01-01"),
        "B": make_df(np.linspace(100, 50, 100), start="2020-01-01"),
    }

    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    args = MockArgs()

    result = run_standard(universe, template, params, args)

    assert "sharpe" in result
    assert "max_drawdown" in result
    assert "equity_curve" in result
    assert not result["equity_curve"].empty


def test_run_walkforward():
    # 1 year of data
    idx = pd.bdate_range("2020-01-01", periods=252)
    universe = {
        "A": make_df(np.linspace(100, 200, 252), start="2020-01-01"),
        "B": make_df(np.linspace(100, 50, 252), start="2020-01-01"),
    }

    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    # 0.5 year window, 0.25 year step
    args = MockArgs(window_years=0.5, step_years=0.25)

    folds = run_walkforward(universe, template, params, args)

    # Total 1 year. Window 0.5. Step 0.25.
    # Folds:
    # 0.0 to 0.5
    # 0.25 to 0.75
    # 0.5 to 1.0
    assert len(folds) == 3

    for fold in folds:
        assert "start_date" in fold
        assert "end_date" in fold
        assert "sharpe_ratio" in fold
        assert "max_drawdown" in fold
        assert "total_turnover" in fold
        assert "total_rebalances" in fold


def test_run_walkforward_warms_up_indicator_lookback_before_each_fold():
    # Regression test: InverseVolatility's realized_vol needs `vol_lookback`
    # bars of history before it stops returning NaN. A fold sliced to bare
    # [start_idx:end_idx) recomputes it from scratch, so every rebalance date
    # inside that cold period used to be silently dropped. A fold that has at
    # least `vol_lookback` bars of real history BEFORE its own start should
    # now get the full number of scheduled rebalances, not just the ones
    # after the window's own warmup period.
    idx = pd.bdate_range("2020-01-01", periods=252 * 2)
    rng = np.random.default_rng(0)
    closes_a = 100 + np.cumsum(rng.normal(0.05, 1.0, len(idx)))
    closes_b = 100 + np.cumsum(rng.normal(0.05, 1.0, len(idx)))
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = _get_template("inverse_volatility")
    params = {"vol_lookback": 120, "rebalance_freq_days": 21}
    args = MockArgs(window_years=1.0, step_years=0.5)

    folds = run_walkforward(universe, template, params, args)

    # Fold 0 starts at day 0 of the whole series -- there is no history
    # before it to draw a warmup buffer from, so it's expected to still lose
    # its first ~120 days' worth of rebalances (a genuine data-availability
    # limit, not a bug).
    assert folds[0]["total_rebalances"] < 12

    # Every later fold has >= 120 days of real history before its own start,
    # so it should now get the full ~252/21 = 12 scheduled rebalances instead
    # of only the ones after its own in-window warmup period.
    for fold in folds[1:]:
        assert fold["total_rebalances"] == 12

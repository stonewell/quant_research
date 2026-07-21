import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes as make_df

from stratgen.allocation_templates import (
    CrossSectionalMomentumAllocation,
    EqualWeightAllocation,
    InverseVolatilityAllocation,
)


def test_equal_weight_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    universe = {
        "A": pd.DataFrame({"Close": np.ones(100)}, index=idx),
        "B": pd.DataFrame({"Close": np.ones(100)}, index=idx),
        "C": pd.DataFrame({"Close": np.ones(100)}, index=idx),
    }
    
    template = EqualWeightAllocation()
    weights = template.generate_weights(universe, {"rebalance_freq_days": 10})

    assert list(weights.columns) == ["A", "B", "C"]
    assert len(weights) == 100

    # Sparse: only the 10 actual rebalance-date rows (every 10 of 100 days)
    # carry a value, everything else is NaN -- the backtester relies on this
    # to tell "rebalanced to the same 1/N weight again" apart from "no
    # rebalance happened" (see allocation_backtester.py).
    rebalance_rows = weights.dropna(how="all")
    assert len(rebalance_rows) == 10

    # Check that weights sum to 1.0 (or very close to it) on every rebalance date
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)

    # Check individual weights are 1/3
    np.testing.assert_allclose(rebalance_rows["A"], 1.0 / 3.0)


def test_inverse_volatility_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    
    # A is flat (low vol), B is volatile
    rng = np.random.default_rng(42)
    closes_a = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 5.0, 100))
    
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }
    
    template = InverseVolatilityAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 20, "rebalance_freq_days": 10})
    
    # After the 20-day warmup, A should have a much higher weight than B
    assert weights.iloc[30]["A"] > weights.iloc[30]["B"]
    np.testing.assert_allclose(weights.iloc[30].sum(), 1.0)


def test_cross_sectional_momentum_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    
    # A goes up, B goes down, C stays flat
    closes_a = np.linspace(100, 200, 100)
    closes_b = np.linspace(100, 50, 100)
    closes_c = np.full(100, 100)
    
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": make_df(closes_c, start="2020-01-01"),
    }
    
    template = CrossSectionalMomentumAllocation()
    # Top 33% of 3 assets = top 1 asset
    weights = template.generate_weights(universe, {"mom_lookback": 20, "top_n_fraction": 0.33, "rebalance_freq_days": 10})
    
    # After warmup, asset A (the only one going up) should get 100% of the weight
    np.testing.assert_allclose(weights.iloc[30]["A"], 1.0)
    np.testing.assert_allclose(weights.iloc[30]["B"], 0.0)
    np.testing.assert_allclose(weights.iloc[30]["C"], 0.0)

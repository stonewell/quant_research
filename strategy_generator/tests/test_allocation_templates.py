import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes as make_df

from common.allocation_templates import (
    CrossSectionalMomentumAllocation,
    DualMomentumAllocation,
    EqualWeightAllocation,
    HierarchicalRiskParityAllocation,
    InverseVolatilityAllocation,
    MaxDiversificationAllocation,
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


def test_hierarchical_risk_parity_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(10)
    closes_a = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 2.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = HierarchicalRiskParityAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    # Weights should sum to 1.0
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)
    # A (lower vol) gets higher HRP weight than B (higher vol)
    assert rebalance_rows.iloc[-1]["A"] > rebalance_rows.iloc[-1]["B"]


def test_hierarchical_risk_parity_excludes_a_symbol_with_no_data_in_window():
    # Regression test: a symbol that hasn't started trading yet (all-NaN
    # Close for the whole lookback window -- e.g. a newer ETF mixed into an
    # older basket) used to have its covariance zero-filled, which
    # inverse-variance weighting misreads as "risk-free" and hands almost
    # the entire portfolio to. It must instead be excluded from that
    # rebalance date's weights (left at 0 once the backtester fills it in),
    # not dominate them. All synthetic data, no network/market data involved.
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(7)
    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    # C has no price history at all for the first 90 bars (NaN Close) --
    # every 20-day lookback window before bar 90 is entirely NaN for C.
    closes_c = np.full(100, np.nan)
    closes_c[90:] = 100 + np.cumsum(rng.normal(0, 1.0, 10))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": pd.DataFrame({"Close": closes_c}, index=idx),
    }

    template = HierarchicalRiskParityAllocation()
    weights = template.generate_weights(universe, {"cov_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty

    # Every rebalance date before C has any data must NOT allocate to C --
    # it should be excluded (NaN in this sparse frame, later filled to 0.0
    # by the backtester), not dominate the portfolio.
    early_rows = rebalance_rows[rebalance_rows.index < idx[90]]
    assert not early_rows.empty
    assert early_rows["C"].isna().all()
    # A and B alone still sum to 1.0 -- properly renormalized among just the
    # symbols that actually had data, not diluted by a phantom C weight.
    np.testing.assert_allclose(early_rows["A"] + early_rows["B"], 1.0)


def test_dual_momentum_allocation_steps_to_cash_when_trend_negative():
    idx = pd.bdate_range("2020-01-01", periods=100)

    # A and B both go down
    closes_a = np.linspace(100, 50, 100)
    closes_b = np.linspace(100, 30, 100)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = DualMomentumAllocation()
    weights = template.generate_weights(universe, {"mom_lookback": 20, "top_n_fraction": 0.5, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    # After warmup, both assets have negative trailing returns -> absolute momentum filter sets weights to 0 (cash)
    last_row = rebalance_rows.iloc[-1]
    np.testing.assert_allclose(last_row["A"], 0.0)
    np.testing.assert_allclose(last_row["B"], 0.0)


def test_max_diversification_allocation():
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(20)

    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = MaxDiversificationAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 20, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    np.testing.assert_allclose(rebalance_rows.sum(axis=1), 1.0)


def test_warmup_bars_reports_each_templates_indicator_lookback():
    # A caller slicing a sub-window (e.g. backtester/run_backtest.py's
    # run_walkforward) needs to know how much history to pull in ahead of
    # that window so the template's own indicator isn't cold at the window's
    # start -- this is the contract each template must expose.
    assert EqualWeightAllocation().warmup_bars({"rebalance_freq_days": 21}) == 0
    assert InverseVolatilityAllocation().warmup_bars({"vol_lookback": 60, "rebalance_freq_days": 21}) == 60
    assert CrossSectionalMomentumAllocation().warmup_bars(
        {"mom_lookback": 126, "top_n_fraction": 0.5, "rebalance_freq_days": 21}
    ) == 126
    assert HierarchicalRiskParityAllocation().warmup_bars({"cov_lookback": 126, "rebalance_freq_days": 21}) == 126
    assert DualMomentumAllocation().warmup_bars({"mom_lookback": 126, "top_n_fraction": 0.5, "rebalance_freq_days": 21}) == 126
    assert MaxDiversificationAllocation().warmup_bars({"vol_lookback": 126, "rebalance_freq_days": 21}) == 126

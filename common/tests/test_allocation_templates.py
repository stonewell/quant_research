"""Unit tests for common/allocation_templates.py."""

import os
import sys

import numpy as np
import pandas as pd

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_templates import MaxDiversificationAllocation, _inverse_vol_weights
from common.testing import make_ohlcv_from_closes as make_df


def test_inverse_vol_weights_normal_case_sums_to_scale():
    vols = pd.Series({"A": 1.0, "B": 2.0, "C": 4.0})
    weights = _inverse_vol_weights(vols)
    np.testing.assert_allclose(weights.sum(), 1.0)
    # Lower vol -> higher weight.
    assert weights["A"] > weights["B"] > weights["C"]


def test_inverse_vol_weights_partial_fill_scale():
    vols = pd.Series({"A": 1.0, "B": 1.0})
    weights = _inverse_vol_weights(vols, scale=0.5)
    np.testing.assert_allclose(weights.sum(), 0.5)


def test_inverse_vol_weights_zero_vol_treated_as_missing():
    vols = pd.Series({"A": 0.0, "B": 2.0})
    weights = _inverse_vol_weights(vols)
    assert np.isnan(weights["A"])
    np.testing.assert_allclose(weights["B"], 1.0)


def test_inverse_vol_weights_all_invalid_on_invalid_nan():
    vols = pd.Series({"A": 0.0, "B": np.nan})
    weights = _inverse_vol_weights(vols, on_invalid="nan")
    assert weights.isna().all()


def test_inverse_vol_weights_all_invalid_on_invalid_zero():
    vols = pd.Series({"A": 0.0, "B": np.nan})
    weights = _inverse_vol_weights(vols, on_invalid="zero")
    assert (weights == 0.0).all()


def test_max_diversification_excludes_a_symbol_with_no_data_in_window():
    # Regression test: unlike its sibling covariance templates
    # (HierarchicalRiskParityAllocation, MinimumVarianceAllocation),
    # MaxDiversificationAllocation used to NOT filter to symbols with a full
    # lookback return history before computing correlations/volatilities. A
    # symbol that hasn't started trading yet (all-NaN Close for the whole
    # lookback window) would have its correlation `fillna(0)`'d to look
    # maximally diversifying, and its own weight would come out NaN --
    # breaking the row's weight-sum invariant. It must instead be excluded
    # entirely (left NaN in this sparse frame, later filled to 0.0 by the
    # backtester). All synthetic data, no network/market data involved.
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(7)
    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    # C has no price history at all for the first 90 bars (NaN Close) --
    # every 60-day lookback window before bar 90 is entirely NaN for C.
    closes_c = np.full(100, np.nan)
    closes_c[90:] = 100 + np.cumsum(rng.normal(0, 1.0, 10))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": pd.DataFrame({"Close": closes_c}, index=idx),
    }

    template = MaxDiversificationAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 60, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty

    # Every rebalance date before C has any data must NOT allocate to C --
    # it should be excluded (NaN in this sparse frame), not leak a NaN
    # weight into A/B's row via a maximally-diversifying fillna(0) correlation.
    early_rows = rebalance_rows[rebalance_rows.index < idx[90]]
    assert not early_rows.empty
    assert early_rows["C"].isna().all()

    # No NaN leaks into A or B's weights, and they sum to 1.0 -- properly
    # renormalized among just the symbols that actually had data, not
    # diluted (or NaN-poisoned) by a phantom C weight.
    assert not early_rows["A"].isna().any()
    assert not early_rows["B"].isna().any()
    np.testing.assert_allclose(early_rows["A"] + early_rows["B"], 1.0, atol=1e-9)

import numpy as np
import pandas as pd
import pytest
from common.testing import make_ar1_series as _ar1_series

from selectorbot.persistence import autocorrelation, hurst_exponent, hurst_significance, variance_ratio


def test_hurst_orders_trend_randomwalk_meanrev_correctly():
    trend = _ar1_series(0.75, 3000, seed=42)
    rw = pd.Series(np.random.default_rng(42).normal(0, 1, 3000))
    meanrev = _ar1_series(-0.9, 3000, seed=42)

    h_trend = hurst_exponent(trend)
    h_rw = hurst_exponent(rw)
    h_meanrev = hurst_exponent(meanrev)

    assert h_meanrev < h_rw < h_trend


def test_hurst_significance_detects_strong_trend():
    trend = _ar1_series(0.75, 3000, seed=42)
    result = hurst_significance(trend, n_surrogates=200, seed=7)
    assert result["hurst"] > 0.6
    assert result["significant"]
    assert result["p_value"] < 0.05


def test_hurst_significance_detects_strong_mean_reversion():
    meanrev = _ar1_series(-0.9, 3000, seed=42)
    result = hurst_significance(meanrev, n_surrogates=200, seed=7)
    assert result["hurst"] < 0.4
    assert result["significant"]


def test_hurst_significance_does_not_flag_pure_noise():
    rw = pd.Series(np.random.default_rng(3).normal(0, 1, 2000))
    result = hurst_significance(rw, n_surrogates=200, seed=7)
    assert not result["significant"]


def test_hurst_returns_nan_for_too_short_series():
    short = pd.Series(np.random.default_rng(1).normal(0, 1, 20))
    assert np.isnan(hurst_exponent(short))


def test_autocorrelation_matches_pandas_autocorr():
    s = pd.Series(np.random.default_rng(5).normal(0, 1, 500))
    assert autocorrelation(s, lag=1) == pytest.approx(s.autocorr(1))


def test_variance_ratio_near_one_for_random_walk():
    rw = pd.Series(np.random.default_rng(9).normal(0, 1, 5000))
    vr = variance_ratio(rw, q=5)
    assert 0.85 < vr < 1.15


def test_variance_ratio_below_one_for_mean_reverting_series():
    meanrev = _ar1_series(-0.9, 5000, seed=9)
    vr = variance_ratio(meanrev, q=5)
    assert vr < 0.85

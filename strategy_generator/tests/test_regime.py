import numpy as np
import pandas as pd
import pytest
from common.testing import make_ar1_series as _ar1_series

from stratgen.regime import (aggregate_regime, calibrate_null_distribution, classify_regime, classify_series,
                               hurst_exponent, hurst_zscore)


def test_hurst_orders_trend_randomwalk_meanrev_correctly():
    trend = _ar1_series(0.75, 3000, seed=42)
    rw = pd.Series(np.random.default_rng(42).normal(0, 1, 3000))
    meanrev = _ar1_series(-0.9, 3000, seed=42)
    assert hurst_exponent(meanrev) < hurst_exponent(rw) < hurst_exponent(trend)


def test_calibrate_null_distribution_centers_above_naive_half_for_short_windows():
    # The R/S estimator's well-documented finite-sample upward bias means the
    # calibrated null mean should sit above the naive textbook 0.5, not at it.
    calibration = calibrate_null_distribution(window_length=500, n_simulations=150, seed=1)
    assert calibration.mean > 0.5
    assert calibration.std > 0


def test_classify_regime_boundaries():
    class FakeCalibration:
        mean = 0.55
        std = 0.03

    cal = FakeCalibration()
    assert classify_regime(0.55 + 0.03 * 0.6, cal, k=0.5) == "trending"
    assert classify_regime(0.55 - 0.03 * 0.6, cal, k=0.5) == "mean_reverting"
    assert classify_regime(0.55, cal, k=0.5) == "random_walk_like"
    assert classify_regime(float("nan"), cal, k=0.5) == "random_walk_like"


def test_classify_series_end_to_end_detects_strong_trend():
    trend = _ar1_series(0.75, 2000, seed=9)
    result = classify_series(trend, n_simulations=200, seed=3)
    assert result["regime_label"] == "trending"
    assert result["hurst"] > result["null_mean"]


def test_classify_series_end_to_end_detects_strong_mean_reversion():
    meanrev = _ar1_series(-0.9, 2000, seed=9)
    result = classify_series(meanrev, n_simulations=200, seed=3)
    assert result["regime_label"] == "mean_reverting"


def test_classify_series_false_positive_rate_on_pure_noise_is_reasonable():
    # A k=1.5 threshold implies pure noise WILL occasionally cross it by
    # chance (~13% two-sided) -- asserting a single fixed seed never
    # misfires is statistically fragile (it's a coin-weighted-toward-pass,
    # not a guarantee). Instead, check the empirical false-positive rate
    # across many independent noise realizations stays in a sane range.
    calibration = calibrate_null_distribution(window_length=800, n_simulations=300, seed=0)
    flagged = 0
    n_series = 30
    for seed in range(n_series):
        rw = pd.Series(np.random.default_rng(seed + 100).normal(0, 1, 800))
        h = hurst_exponent(rw)
        if classify_regime(h, calibration, k=1.5) != "random_walk_like":
            flagged += 1
    false_positive_rate = flagged / n_series
    assert false_positive_rate < 0.35  # comfortably above the ~13% theoretical rate, well below "no better than chance"


def test_hurst_zscore_matches_manual_calculation():
    trend = _ar1_series(0.75, 1500, seed=4)
    result = hurst_zscore(trend, n_simulations=150, seed=1)
    expected_z = (result["hurst"] - result["null_mean"]) / result["null_std"]
    assert result["z"] == pytest.approx(expected_z)


def test_aggregate_regime_detects_consistent_trend_across_universe():
    series_by_symbol = {
        "A": _ar1_series(0.75, 1200, seed=10),
        "B": _ar1_series(0.75, 1200, seed=11),
        "C": _ar1_series(0.75, 1200, seed=12),
    }
    result = aggregate_regime(series_by_symbol, n_simulations=150, k=1.5, seed=1)
    assert result["regime_label"] == "trending"
    assert result["n_symbols"] == 3
    assert set(result["per_symbol"].keys()) == {"A", "B", "C"}


def test_aggregate_regime_median_resists_one_outlier():
    # Two strongly trending + one strongly mean-reverting outlier: the
    # median-pooled z-score should still reflect the trending majority,
    # unlike a mean which an extreme outlier could drag toward zero.
    series_by_symbol = {
        "A": _ar1_series(0.75, 1200, seed=10),
        "B": _ar1_series(0.75, 1200, seed=11),
        "C": _ar1_series(-0.9, 1200, seed=12),
    }
    result = aggregate_regime(series_by_symbol, n_simulations=150, k=1.5, seed=1)
    assert result["regime_label"] == "trending"


def test_aggregate_regime_on_pure_noise_universe_is_random_walk_like():
    series_by_symbol = {
        "A": pd.Series(np.random.default_rng(20).normal(0, 1, 1200)),
        "B": pd.Series(np.random.default_rng(21).normal(0, 1, 1200)),
        "C": pd.Series(np.random.default_rng(22).normal(0, 1, 1200)),
    }
    result = aggregate_regime(series_by_symbol, n_simulations=150, k=1.5, seed=1)
    assert result["regime_label"] == "random_walk_like"


def test_aggregate_regime_handles_empty_dict_gracefully():
    result = aggregate_regime({}, n_simulations=50, seed=1)
    assert result["regime_label"] == "random_walk_like"
    assert result["n_symbols"] == 0

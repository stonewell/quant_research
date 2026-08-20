"""Unit tests for common/metrics.py."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.metrics import alpha_beta, information_ratio, profit_factor_from_returns, tracking_error, win_rate_from_returns


def test_win_rate_from_returns_mixed():
    returns = pd.Series([0.01, -0.02, 0.03, -0.01, 0.0])
    # 2 of 5 periods are strictly positive
    assert win_rate_from_returns(returns) == pytest.approx(2 / 5)


def test_win_rate_from_returns_empty():
    assert win_rate_from_returns(pd.Series([], dtype=float)) == 0.0


def test_win_rate_from_returns_all_positive():
    returns = pd.Series([0.01, 0.02, 0.03])
    assert win_rate_from_returns(returns) == pytest.approx(1.0)


def test_profit_factor_from_returns_mixed():
    returns = pd.Series([0.02, -0.01, 0.03, -0.01])
    # gains = 0.05, losses = 0.02
    assert profit_factor_from_returns(returns) == pytest.approx(0.05 / 0.02)


def test_profit_factor_from_returns_no_losses_is_nan():
    returns = pd.Series([0.01, 0.02, 0.0])
    assert np.isnan(profit_factor_from_returns(returns))


def test_profit_factor_from_returns_empty_is_nan():
    assert np.isnan(profit_factor_from_returns(pd.Series([], dtype=float)))


def _series(values, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx)


def test_alpha_beta_identical_series_is_beta_one_alpha_zero():
    rng = np.random.default_rng(0)
    base = _series(rng.normal(0.0005, 0.01, 200))
    result = alpha_beta(base, base)
    assert result["beta"] == pytest.approx(1.0, abs=1e-9)
    assert result["alpha"] == pytest.approx(0.0, abs=1e-9)


def test_alpha_beta_double_leveraged_series():
    rng = np.random.default_rng(1)
    base = _series(rng.normal(0.0005, 0.01, 200))
    strategy = 2 * base
    result = alpha_beta(strategy, base)
    assert result["beta"] == pytest.approx(2.0, abs=1e-9)
    assert result["alpha"] == pytest.approx(0.0, abs=1e-6)


def test_alpha_beta_zero_variance_baseline_returns_zero():
    strategy = _series([0.01, -0.02, 0.03, 0.01, -0.01])
    baseline = _series([0.001] * 5)
    assert alpha_beta(strategy, baseline) == {"alpha": 0.0, "beta": 0.0}


def test_alpha_beta_insufficient_overlap_returns_zero():
    strategy = _series([0.01], start="2020-01-01")
    baseline = _series([0.01], start="2021-01-01")
    assert alpha_beta(strategy, baseline) == {"alpha": 0.0, "beta": 0.0}


def test_tracking_error_identical_series_is_zero():
    rng = np.random.default_rng(2)
    base = _series(rng.normal(0.0, 0.01, 100))
    assert tracking_error(base, base) == 0.0


def test_tracking_error_known_constant_offset():
    rng = np.random.default_rng(3)
    base = _series(rng.normal(0.0, 0.01, 100))
    strategy = base + 0.001  # constant offset doesn't change the diff's std
    assert tracking_error(strategy, base) == pytest.approx(0.0, abs=1e-9)


def test_tracking_error_computed_value():
    # Alternating +1%/-1% diff pattern -> hand-computable std.
    strategy = _series([0.01, -0.01] * 50)
    baseline = _series([0.0] * 100)
    diff = strategy - baseline
    expected = diff.std(ddof=1) * np.sqrt(252)
    assert tracking_error(strategy, baseline) == pytest.approx(expected)


def test_information_ratio_zero_tracking_error_is_zero():
    rng = np.random.default_rng(4)
    base = _series(rng.normal(0.0, 0.01, 100))
    assert information_ratio(base, base) == 0.0


def test_information_ratio_computed_value():
    rng = np.random.default_rng(5)
    diff_values = rng.normal(0.001, 0.005, 200)
    baseline = _series(rng.normal(0.0, 0.01, 200))
    strategy = baseline + diff_values
    diff = strategy - baseline
    expected = (diff.mean() / diff.std(ddof=1)) * np.sqrt(252)
    assert information_ratio(strategy, baseline) == pytest.approx(expected)


def test_information_ratio_misaligned_index_uses_intersection():
    idx_a = pd.bdate_range("2020-01-01", periods=100)
    idx_b = pd.bdate_range("2020-02-01", periods=100)
    rng = np.random.default_rng(6)
    strategy = pd.Series(rng.normal(0.001, 0.01, 100), index=idx_a)
    baseline = pd.Series(rng.normal(0.0, 0.01, 100), index=idx_b)

    common_idx = idx_a.intersection(idx_b)
    assert len(common_idx) > 2  # sanity: fixture actually overlaps
    diff = strategy.loc[common_idx] - baseline.loc[common_idx]
    expected = (diff.mean() / diff.std(ddof=1)) * np.sqrt(252)
    assert information_ratio(strategy, baseline) == pytest.approx(expected)

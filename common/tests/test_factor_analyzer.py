"""Unit tests for Factor Analyzer and IC/IR Diagnostic Evaluation Engine."""

import numpy as np
import pandas as pd
import pytest

from common.factor_analyzer import (
    compute_forward_returns,
    compute_rank_ic,
    compute_factor_autocorrelation_half_life,
    factor_ic_summary,
    compute_quantile_returns,
)


@pytest.fixture
def sample_market_data():
    """Generates synthetic price and forward return matrices for 10 assets across 100 days."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=100, freq="B")
    symbols = [f"SYM_{i}" for i in range(10)]
    
    # Random walk prices
    log_ret = np.random.normal(0.0005, 0.015, size=(100, 10))
    prices = 100.0 * np.exp(np.cumsum(log_ret, axis=0))
    close_df = pd.DataFrame(prices, index=dates, columns=symbols)
    return close_df


def test_compute_forward_returns(sample_market_data):
    close_df = sample_market_data
    fwd_ret_dict = compute_forward_returns(close_df, periods=[1, 5, 21])
    assert 1 in fwd_ret_dict
    assert 5 in fwd_ret_dict
    assert 21 in fwd_ret_dict
    
    # 1-day forward return: (P_{t+1} / P_t) - 1
    expected_1d = (close_df.shift(-1) / close_df - 1.0)
    pd.testing.assert_frame_equal(fwd_ret_dict[1], expected_1d)
    
    # Single period int mode
    single_ret = compute_forward_returns(close_df, periods=5)
    pd.testing.assert_frame_equal(single_ret, fwd_ret_dict[5])


def test_compute_rank_ic_perfect_and_inverse_signals(sample_market_data):
    close_df = sample_market_data
    fwd_1d = compute_forward_returns(close_df, periods=1)
    
    # Perfect predictor factor
    factor_perfect = fwd_1d.copy()
    ic_perfect = compute_rank_ic(factor_perfect, fwd_1d).dropna()
    assert len(ic_perfect) > 0
    assert np.allclose(ic_perfect.values, 1.0, atol=1e-4)

    # Inverse predictor factor
    factor_inverse = -fwd_1d.copy()
    ic_inverse = compute_rank_ic(factor_inverse, fwd_1d).dropna()
    assert np.allclose(ic_inverse.values, -1.0, atol=1e-4)


def test_factor_ic_summary_harvey_liu_zhu_hurdle(sample_market_data):
    close_df = sample_market_data
    fwd_1d = compute_forward_returns(close_df, periods=1)
    
    # Strong synthetic factor with t-stat > 3.0
    np.random.seed(123)
    noise = np.random.normal(0, 0.005, size=fwd_1d.shape)
    strong_factor = fwd_1d + noise
    
    summary_strong = factor_ic_summary(strong_factor, fwd_1d, min_tstat=2.0, hlz_tstat=3.0)
    assert summary_strong["mean_ic"] > 0.40
    assert summary_strong["ic_ir"] > 5.0
    assert summary_strong["t_stat"] > 3.0
    assert summary_strong["is_significant"] is True
    assert summary_strong["is_statistically_significant"] is True
    assert summary_strong["p_value"] < 0.01

    # Pure random noise factor: t-stat should not pass HLZ threshold
    pure_noise = pd.DataFrame(
        np.random.normal(0, 1.0, size=fwd_1d.shape),
        index=fwd_1d.index,
        columns=fwd_1d.columns,
    )
    summary_noise = factor_ic_summary(pure_noise, fwd_1d, min_tstat=2.0, hlz_tstat=3.0)
    assert abs(summary_noise["mean_ic"]) < 0.20
    assert summary_noise["is_statistically_significant"] is False


def test_factor_autocorrelation_half_life():
    dates = pd.date_range("2024-01-01", periods=100, freq="B")
    symbols = [f"SYM_{i}" for i in range(10)]
    
    # Highly persistent factor (e.g. 60-day moving average rank)
    np.random.seed(42)
    persistent = np.cumsum(np.random.normal(0, 0.01, size=(100, 10)), axis=0)
    df_persistent = pd.DataFrame(persistent, index=dates, columns=symbols)
    hl_persistent = compute_factor_autocorrelation_half_life(df_persistent)
    assert hl_persistent > 5.0

    # Fast turnover white noise factor
    df_noise = pd.DataFrame(
        np.random.normal(0, 1.0, size=(100, 10)),
        index=dates,
        columns=symbols,
    )
    hl_noise = compute_factor_autocorrelation_half_life(df_noise)
    assert hl_noise < 2.0


def test_compute_quantile_returns_monotonicity(sample_market_data):
    close_df = sample_market_data
    fwd_1d = compute_forward_returns(close_df, periods=1)
    
    # Synthetic factor positively correlated with forward returns
    np.random.seed(7)
    noise = np.random.normal(0, 0.002, size=fwd_1d.shape)
    pos_factor = fwd_1d + noise
    
    q_df = compute_quantile_returns(pos_factor, fwd_1d, n_quantiles=5)
    assert list(q_df.columns) == ["Q1", "Q2", "Q3", "Q4", "Q5", "spread"]
    
    # Average spread Q5 - Q1 should be positive for positively correlated factor
    mean_spread = q_df["spread"].mean()
    assert mean_spread > 0.0
    assert q_df["Q5"].mean() > q_df["Q1"].mean()

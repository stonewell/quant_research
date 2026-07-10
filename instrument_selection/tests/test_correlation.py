import numpy as np
import pandas as pd
import pytest

from selectorbot.correlation import (beta_to_benchmark, correlation_matrix, correlation_regime_shift,
                                       hierarchical_clusters, redundancy_flags, returns_matrix)


def make_price_df(close):
    close = pd.Series(close, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(close))
    return pd.DataFrame({
        "Open": close.values, "High": close.values + 0.3, "Low": close.values - 0.3, "Close": close.values,
    }, index=idx)


def test_returns_matrix_aligns_on_common_dates():
    rng = np.random.default_rng(1)
    n = 300
    close_a = 100 + np.cumsum(rng.normal(0, 1, n))
    close_b = 50 + np.cumsum(rng.normal(0, 1, n))
    df_a = make_price_df(close_a)
    df_b = make_price_df(close_b).iloc[10:]  # shorter history
    result = returns_matrix({"A": df_a, "B": df_b})
    assert list(result.columns) == ["A", "B"]
    assert result.index.min() >= df_b.index[1]  # can't have a return before B's first available bar+1


def test_correlation_matrix_diagonal_is_one():
    rng = np.random.default_rng(2)
    n = 300
    data = {sym: make_price_df(100 + np.cumsum(rng.normal(0, 1, n))) for sym in ["A", "B", "C"]}
    returns = returns_matrix(data)
    corr = correlation_matrix(returns)
    assert np.allclose(np.diag(corr.values), 1.0)


def test_beta_to_benchmark_recovers_known_scaling():
    rng = np.random.default_rng(3)
    n = 1000
    bench_returns = rng.normal(0, 0.01, n)
    noisy_returns = 2.0 * bench_returns + rng.normal(0, 0.001, n)
    bench_close = 100 * np.cumprod(1 + bench_returns)
    other_close = 100 * np.cumprod(1 + noisy_returns)
    data = {"BENCH": make_price_df(bench_close), "OTHER": make_price_df(other_close)}
    returns = returns_matrix(data)
    betas = beta_to_benchmark(returns, "BENCH")
    assert betas["OTHER"] == pytest.approx(2.0, rel=0.1)


def test_hierarchical_clusters_groups_near_identical_series():
    rng = np.random.default_rng(4)
    n = 500
    base_returns = rng.normal(0, 0.01, n)
    a_returns = base_returns + rng.normal(0, 0.0005, n)   # near-identical to base
    b_returns = base_returns + rng.normal(0, 0.0005, n)   # near-identical to base
    c_returns = rng.normal(0, 0.01, n)                     # independent

    data = {
        "A": make_price_df(100 * np.cumprod(1 + a_returns)),
        "B": make_price_df(100 * np.cumprod(1 + b_returns)),
        "C": make_price_df(100 * np.cumprod(1 + c_returns)),
    }
    returns = returns_matrix(data)
    corr = correlation_matrix(returns)
    clusters = hierarchical_clusters(corr, distance_threshold=0.5)
    assert clusters["A"] == clusters["B"]
    assert clusters["C"] != clusters["A"]


def test_redundancy_flags_detects_highly_correlated_pair():
    rng = np.random.default_rng(5)
    n = 500
    base_returns = rng.normal(0, 0.01, n)
    a_returns = base_returns + rng.normal(0, 0.0002, n)
    b_returns = rng.normal(0, 0.01, n)
    data = {
        "A": make_price_df(100 * np.cumprod(1 + a_returns)),
        "REDUNDANT_A": make_price_df(100 * np.cumprod(1 + a_returns + rng.normal(0, 0.0002, n))),
        "B": make_price_df(100 * np.cumprod(1 + b_returns)),
    }
    returns = returns_matrix(data)
    corr = correlation_matrix(returns)
    flags = redundancy_flags(corr, threshold=0.9)
    flagged_pairs = {frozenset([a, b]) for a, b, _ in flags}
    assert frozenset(["A", "REDUNDANT_A"]) in flagged_pairs


def test_correlation_regime_shift_detects_higher_stress_correlation():
    rng = np.random.default_rng(6)
    n = 600
    bench_returns = rng.normal(0, 0.01, n)
    bench_returns[400:450] = rng.normal(0, 0.05, 50)  # stress window: much higher vol

    # During the stress window, force the other two assets to move together with the benchmark (crash contagion).
    a_returns = rng.normal(0, 0.01, n)
    b_returns = rng.normal(0, 0.01, n)
    a_returns[400:450] = bench_returns[400:450] + rng.normal(0, 0.002, 50)
    b_returns[400:450] = bench_returns[400:450] + rng.normal(0, 0.002, 50)

    data = {
        "BENCH": make_price_df(100 * np.cumprod(1 + bench_returns)),
        "A": make_price_df(100 * np.cumprod(1 + a_returns)),
        "B": make_price_df(100 * np.cumprod(1 + b_returns)),
    }
    returns = returns_matrix(data)
    result = correlation_regime_shift(returns, "BENCH", high_vol_quantile=0.75)
    assert result["stress_avg_corr"] > result["calm_avg_corr"]

"""Unit tests for common/covariance.py's RMT correlation/covariance denoising."""

import numpy as np
import pandas as pd
import pytest

from common.covariance import denoise_correlation, denoise_covariance


def test_denoise_correlation_moves_closer_to_the_true_correlation():
    """The core rigor test: on a synthetic factor-model return panel with a
    KNOWN true correlation, a short/noisy sample window's denoised
    correlation must be closer (Frobenius norm) to the true correlation
    than the raw sample correlation is -- proof the technique works, not
    just that it runs."""
    rng = np.random.default_rng(0)
    n_assets, n_obs, n_factors = 20, 60, 2  # q = n_assets/n_obs ~ 0.33: short, noisy window

    loadings = rng.normal(0, 1, size=(n_assets, n_factors))
    factor_returns = rng.normal(0, 1, size=(n_obs, n_factors))
    idio_std = 2.0
    idio = rng.normal(0, idio_std, size=(n_obs, n_assets))
    returns = factor_returns @ loadings.T + idio

    cov_true = loadings @ loadings.T + np.diag(np.full(n_assets, idio_std**2))
    d_true = np.sqrt(np.diag(cov_true))
    corr_true = cov_true / np.outer(d_true, d_true)

    corr_sample = pd.DataFrame(returns).corr()
    corr_denoised = denoise_correlation(corr_sample, n_obs=n_obs)

    err_sample = np.linalg.norm(corr_sample.to_numpy() - corr_true, ord="fro")
    err_denoised = np.linalg.norm(corr_denoised.to_numpy() - corr_true, ord="fro")

    assert err_denoised < err_sample


def test_denoise_correlation_does_not_corrupt_a_near_duplicate_pair_in_a_tiny_universe():
    """Regression: with only 3 assets (a near-duplicate pair + one
    independent one), naive eigenvalue clipping can average the independent
    asset's own legitimate ~1.0 eigenvalue together with genuine noise,
    corrupting the near-duplicate pair's correlation even though that
    pair's own (large) eigenvalue was never touched -- see
    denoise_correlation's docstring. The `min_noise_eigenvalues` safety
    rail must keep this a no-op instead."""
    rng = np.random.default_rng(9)
    n = 500
    base = rng.normal(0, 0.01, n)
    a = base + rng.normal(0, 0.0002, n)
    b = base + rng.normal(0, 0.0002, n)
    c = rng.normal(0, 0.01, n)
    returns = pd.DataFrame({"A": a, "B": b, "C": c})
    corr = returns.corr()

    out = denoise_correlation(corr, n_obs=n)

    assert out.loc["A", "B"] >= 0.85


def test_denoise_correlation_is_a_noop_for_two_or_fewer_assets():
    corr = pd.DataFrame([[1.0, 0.4], [0.4, 1.0]], index=["A", "B"], columns=["A", "B"])
    out = denoise_correlation(corr, n_obs=100)
    pd.testing.assert_frame_equal(out, corr)


def test_denoise_correlation_preserves_dataframe_index_and_unit_diagonal():
    rng = np.random.default_rng(1)
    symbols = ["A", "B", "C", "D", "E"]
    returns = pd.DataFrame(rng.normal(0, 1, size=(50, len(symbols))), columns=symbols)
    corr = returns.corr()

    out = denoise_correlation(corr, n_obs=len(returns))

    assert isinstance(out, pd.DataFrame)
    assert list(out.index) == symbols
    assert list(out.columns) == symbols
    np.testing.assert_allclose(np.diag(out.to_numpy()), 1.0, atol=1e-8)


def test_denoise_correlation_accepts_and_returns_ndarray():
    rng = np.random.default_rng(2)
    returns = rng.normal(0, 1, size=(50, 5))
    corr = np.corrcoef(returns, rowvar=False)

    out = denoise_correlation(corr, n_obs=50)

    assert isinstance(out, np.ndarray)
    np.testing.assert_allclose(np.diag(out), 1.0, atol=1e-8)


def test_denoise_covariance_preserves_original_variances():
    rng = np.random.default_rng(3)
    n_assets, n_obs = 10, 40
    returns = rng.normal(0, 1, size=(n_obs, n_assets)) * rng.uniform(0.5, 3.0, size=n_assets)
    cov = np.cov(returns, rowvar=False)

    cleaned = denoise_covariance(cov, n_obs=n_obs)

    np.testing.assert_allclose(np.diag(cleaned), np.diag(cov), rtol=1e-8)


def test_denoise_covariance_is_a_noop_for_two_or_fewer_assets():
    cov = np.array([[1.0, 0.3], [0.3, 2.0]])
    out = denoise_covariance(cov, n_obs=100)
    np.testing.assert_array_equal(out, cov)

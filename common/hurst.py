"""Shared trend-persistence statistics: the classical rescaled-range (R/S)
Hurst exponent estimator, lag-1 autocorrelation, and a simplified
variance-ratio statistic.

H < 0.5: anti-persistent/mean-reverting. H = 0.5: random walk. H > 0.5:
persistent/trending. Two different projects in this workspace build
different significance-testing methodologies on top of this same base
estimator (instrument_selection's shuffle-based surrogate test;
strategy_generator's Monte-Carlo-calibrated-null approach) -- that
project-specific logic stays local to each project; only the underlying
math is shared here.
"""

import numpy as np
import pandas as pd


def _rs_stat(x: np.ndarray) -> float:
    mean = x.mean()
    cum_dev = np.cumsum(x - mean)
    r = cum_dev.max() - cum_dev.min()
    s = x.std(ddof=1)
    if s == 0:
        return np.nan
    return r / s


def hurst_exponent(series: pd.Series, min_chunk_size: int = 8, max_lag_fraction: float = 0.5) -> float:
    """Classical rescaled-range (R/S) Hurst estimator. `series` should be a
    stationary increment series (e.g., log returns), not raw price levels."""
    x = pd.Series(series).dropna().to_numpy()
    n = len(x)
    if n < min_chunk_size * 4:
        return np.nan

    max_chunk = int(n * max_lag_fraction)
    sizes = np.unique(np.floor(np.logspace(np.log10(min_chunk_size), np.log10(max_chunk), num=20)).astype(int))
    sizes = sizes[sizes >= min_chunk_size]

    log_sizes, log_rs = [], []
    for size in sizes:
        n_chunks = n // size
        if n_chunks < 1:
            continue
        chunk_rs = [_rs_stat(x[i * size:(i + 1) * size]) for i in range(n_chunks)]
        chunk_rs = [v for v in chunk_rs if not np.isnan(v) and v > 0]
        if chunk_rs:
            log_sizes.append(np.log(size))
            log_rs.append(np.log(np.mean(chunk_rs)))

    if len(log_sizes) < 4:
        return np.nan
    slope, _ = np.polyfit(log_sizes, log_rs, 1)
    return slope


def autocorrelation(returns: pd.Series, lag: int = 1) -> float:
    return returns.dropna().autocorr(lag)


def variance_ratio(returns: pd.Series, q: int = 2) -> float:
    """Simplified Lo-MacKinlay variance ratio: Var(q-period overlapping sum)
    / (q * Var(1-period)). VR=1 under a random walk; VR>1 suggests positive
    serial correlation (trending), VR<1 suggests mean reversion. This is the
    point estimate only, not the full heteroskedasticity-robust test
    statistic with confidence intervals from the original Lo-MacKinlay paper."""
    r = returns.dropna()
    mu = r.mean()
    var_1 = ((r - mu) ** 2).mean()
    q_sum = r.rolling(q).sum().dropna()
    var_q = ((q_sum - q * mu) ** 2).mean()
    if var_1 == 0:
        return np.nan
    return var_q / (q * var_1)

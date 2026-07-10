"""Trend-persistence vs. mean-reversion tendency: Hurst exponent (via
classical rescaled-range analysis), a surrogate-data significance test, lag-1
autocorrelation, and a simplified variance-ratio statistic.

H < 0.5: anti-persistent / mean-reverting. H = 0.5: random walk. H > 0.5:
persistent / trending. Research-documented practical band: |H - 0.5| <= 0.05
is generally not economically meaningful.

IMPORTANT, well-documented pitfall this module specifically addresses: naive
Hurst estimates computed directly on raw return series are frequently above
0.5 (often ~0.6) purely from short-term autocorrelation artifacts, NOT
genuine long-range memory (Cheung, 1995, Applied Economics Letters, found
this on a decade of daily FX returns). Reporting a raw H value without a
significance test is exactly the kind of unverified claim that failed
adversarial fact-checking repeatedly during this project's research pass.
`hurst_significance` builds an empirical null distribution by computing H on
many randomly-shuffled copies of the same series (which by construction have
no memory at all) and reports how extreme the observed H is relative to that
null -- a legitimate bootstrap significance test, simpler than (and a
coarser instrument than) the Fourier-phase-randomization surrogates used in
the academic literature, since a full shuffle destroys short-range as well
as long-range dependence. Treat a significant H as "this series shows some
temporal dependence beyond what chance produces," not proof of long memory
specifically.
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
    x = series.dropna().to_numpy()
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


def hurst_significance(series: pd.Series, n_surrogates: int = 200, min_chunk_size: int = 8,
                        max_lag_fraction: float = 0.5, seed: int = None) -> dict:
    observed = hurst_exponent(series, min_chunk_size, max_lag_fraction)
    if np.isnan(observed):
        return {"hurst": np.nan, "surrogate_mean": np.nan, "p_value": np.nan, "significant": False}

    x = series.dropna().to_numpy()
    rng = np.random.default_rng(seed)
    surrogate_h = []
    for _ in range(n_surrogates):
        shuffled = pd.Series(rng.permutation(x))
        h = hurst_exponent(shuffled, min_chunk_size, max_lag_fraction)
        if not np.isnan(h):
            surrogate_h.append(h)
    surrogate_h = np.array(surrogate_h)

    deviation = abs(observed - 0.5)
    p_value = (np.abs(surrogate_h - 0.5) >= deviation).mean() if len(surrogate_h) else np.nan

    return {
        "hurst": observed,
        "surrogate_mean": surrogate_h.mean() if len(surrogate_h) else np.nan,
        "surrogate_std": surrogate_h.std() if len(surrogate_h) else np.nan,
        "p_value": p_value,
        "significant": bool(p_value < 0.05) if not np.isnan(p_value) else False,
    }


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


def persistence_summary(close: pd.Series, config) -> dict:
    log_returns = np.log(close / close.shift(1)).dropna()
    if len(log_returns) < config.hurst_min_obs:
        return {"hurst": np.nan, "hurst_significant": False, "hurst_p_value": np.nan,
                "autocorr_lag1": np.nan, "variance_ratio_q5": np.nan, "regime_label": "insufficient_data"}

    sig = hurst_significance(log_returns, n_surrogates=config.hurst_n_surrogates,
                              max_lag_fraction=config.hurst_max_lag_fraction)
    acf1 = autocorrelation(log_returns, lag=1)
    vr5 = variance_ratio(log_returns, q=5)

    h = sig["hurst"]
    if np.isnan(h) or abs(h - 0.5) <= config.hurst_neutral_band or not sig["significant"]:
        label = "random_walk_like"
    elif h > 0.5:
        label = "trending"
    else:
        label = "mean_reverting"

    return {
        "hurst": h,
        "hurst_significant": sig["significant"],
        "hurst_p_value": sig["p_value"],
        "autocorr_lag1": acf1,
        "variance_ratio_q5": vr5,
        "regime_label": label,
    }

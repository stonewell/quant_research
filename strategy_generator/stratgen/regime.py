"""Hurst-exponent regime classification, calibrated via Monte Carlo
simulation against the estimator's own finite-sample bias/variance --
rather than a naive universal H=0.5 cutoff.

Research grounding (Noppakaew et al. 2025; Chang, Lizardi & Shah 2022):
route an instrument to a momentum template if its Hurst exponent is
significantly above what pure noise of the SAME window length and estimator
would produce, a mean-reversion template if significantly below, and no
strategy if indistinguishable from that noise floor. The R/S estimator used
here has a well-known finite-sample bias (it doesn't center exactly on 0.5
for a true random walk at practical sample sizes) -- simulating the null
directly, rather than assuming H=0.5, is what corrects for this rather than
requiring a separate bias-correction formula.
"""

from dataclasses import dataclass

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
    """Classical rescaled-range (R/S) Hurst estimator on a stationary
    increment series (e.g., log returns), not raw price levels."""
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


@dataclass
class NullCalibration:
    window_length: int
    mean: float
    std: float
    n_simulations: int


def calibrate_null_distribution(window_length: int, n_simulations: int = 300, seed: int = None) -> NullCalibration:
    """Simulate `n_simulations` pure random walks (iid increments) of the
    same length as the data you'll actually classify, and compute this
    module's own Hurst estimator on each -- giving the estimator's true
    mean/std under "no memory at all" for that specific window length,
    rather than assuming the textbook value of exactly 0.5."""
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(n_simulations):
        increments = rng.normal(0, 1, window_length)
        h = hurst_exponent(pd.Series(increments))
        if not np.isnan(h):
            values.append(h)
    values = np.array(values)
    return NullCalibration(window_length=window_length, mean=values.mean(), std=values.std(), n_simulations=len(values))


def classify_regime(hurst_value: float, calibration: NullCalibration, k: float = 1.5) -> str:
    """'trending' if hurst_value is >= k standard deviations above the
    calibrated null mean, 'mean_reverting' if that far below, else
    'random_walk_like' (statistically indistinguishable from noise of the
    same window length).

    Note: Noppakaew et al.'s own regime study used k=0.5 for a descriptive
    three-way tertile split of trend strength. That is far too permissive a
    bar for THIS use -- gating a binary decision to deploy an entirely
    different strategy template. At k=0.5, a true random walk gets
    misclassified as trending or mean-reverting roughly a third of the time
    by chance alone (two-sided). k=1.5 (~13% two-sided false-positive rate)
    is a stricter, deliberately-chosen default for that reason; tighten
    further (e.g. k=2.0) if you want fewer, higher-conviction generated
    strategies at the cost of routing more instruments to no-trade.
    """
    if np.isnan(hurst_value) or calibration.std == 0:
        return "random_walk_like"
    z = (hurst_value - calibration.mean) / calibration.std
    if z >= k:
        return "trending"
    if z <= -k:
        return "mean_reverting"
    return "random_walk_like"


def classify_series(series: pd.Series, n_simulations: int = 300, k: float = 1.5, seed: int = None) -> dict:
    """Convenience wrapper: compute H on `series`, calibrate the null at the
    same length, and classify -- the one-call entry point most callers want."""
    h = hurst_exponent(series)
    calibration = calibrate_null_distribution(len(series.dropna()), n_simulations=n_simulations, seed=seed)
    label = classify_regime(h, calibration, k=k)
    return {"hurst": h, "regime_label": label, "null_mean": calibration.mean, "null_std": calibration.std}

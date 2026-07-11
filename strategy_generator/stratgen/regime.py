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
from common.hurst import hurst_exponent


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


def hurst_zscore(series: pd.Series, n_simulations: int = 300, seed: int = None) -> dict:
    """Like `classify_series`, but returns the raw z-score (Hurst deviation
    from its own calibrated null, in std units) instead of a discretized
    label -- the building block for pooling regime evidence across multiple
    instruments without throwing away information by labeling too early."""
    h = hurst_exponent(series)
    calibration = calibrate_null_distribution(len(series.dropna()), n_simulations=n_simulations, seed=seed)
    z = (h - calibration.mean) / calibration.std if (not np.isnan(h) and calibration.std > 0) else np.nan
    return {"hurst": h, "z": z, "null_mean": calibration.mean, "null_std": calibration.std}


def aggregate_regime(series_by_symbol: dict, n_simulations: int = 300, k: float = 1.5, seed: int = None) -> dict:
    """Classify a WHOLE UNIVERSE at once, for generating a single strategy
    meant to apply across every instrument in it, rather than one strategy
    per symbol.

    Each symbol's Hurst is standardized against a null calibrated to that
    symbol's OWN window length (so instruments with different history
    lengths remain comparable), then the per-symbol z-scores are pooled via
    their MEDIAN and classified against the same threshold used for a single
    series. The median (not the mean) is used deliberately so one outlier
    instrument -- e.g. a commodity ETF with an unusually strong idiosyncratic
    trend -- can't single-handedly flip the whole universe's classification.

    This directly addresses a question research flagged as open and
    unresolved: generating an independent strategy per instrument across a
    universe is itself a multiple-comparisons problem (whichever instrument's
    history happened to back-test best gets reported, without correcting for
    having effectively run N trials). Pooling the regime decision -- and,
    downstream in `generator.py`, the parameter search itself -- across the
    whole universe treats "does this generalize across many instruments" as
    the actual object of search, rather than "which one instrument got the
    luckiest fit."
    """
    per_symbol = {symbol: hurst_zscore(series, n_simulations=n_simulations, seed=seed)
                  for symbol, series in series_by_symbol.items()}

    z_values = np.array([v["z"] for v in per_symbol.values() if not np.isnan(v["z"])])
    if len(z_values) == 0:
        pooled_z, label = float("nan"), "random_walk_like"
    else:
        pooled_z = float(np.median(z_values))
        if pooled_z >= k:
            label = "trending"
        elif pooled_z <= -k:
            label = "mean_reverting"
        else:
            label = "random_walk_like"

    return {
        "regime_label": label,
        "pooled_z": pooled_z,
        "n_symbols": len(series_by_symbol),
        "n_symbols_with_valid_hurst": len(z_values),
        "per_symbol": per_symbol,
    }

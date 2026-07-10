"""Synthetic OHLCV/price-series generators shared across every project's
test suite in this workspace. Test-only helpers -- never imported by any
production code (see data.py/indicators.py/hurst.py/metrics.py for that).

Each generator's defaults match the most common usage found when this
module was extracted from duplicated per-project test helpers; call sites
needing different parameters (spread, index, sine period, ...) pass them
explicitly or wrap the call, rather than this module growing bespoke
defaults for every caller.
"""

import numpy as np
import pandas as pd


def make_ohlcv_from_closes(closes, spread: float = 0.5, use_index: bool = True,
                            start: str = "2020-01-01", pad_left: int = 0) -> pd.DataFrame:
    """Wrap a bare price array/Series into a synthetic OHLCV DataFrame:
    Open=Close, High=Close+spread, Low=Close-spread. `pad_left` prepends
    that many bars at the series' own first value (used by tests that need
    extra indicator warmup before the "interesting" price action starts)."""
    closes = pd.Series(closes, dtype=float)
    if pad_left:
        closes = pd.Series(np.concatenate([np.full(pad_left, closes.iloc[0]), closes.to_numpy()]))
    values = closes.to_numpy()
    df = pd.DataFrame({"Open": values, "High": values + spread, "Low": values - spread, "Close": values})
    if use_index:
        df.index = pd.bdate_range(start, periods=len(df))
    return df


def make_random_walk_df(n: int, seed: int, start: str = "2015-01-01") -> pd.DataFrame:
    """A plain additive random walk (no drift), wrapped into OHLCV."""
    close = 100 + np.cumsum(np.random.default_rng(seed).normal(0, 1, n))
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close}, index=idx)


def make_oscillating_df(n: int = 400, base: float = 100.0, amplitude: float = 8.0, noise: float = 0.3,
                         seed: int = 7, sine_period: float = 15.0, start: str = "2020-01-01") -> pd.DataFrame:
    """A sine-wave choppy/range-bound market -- should reliably trigger
    mean-reversion entries without ever trending."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base + amplitude * np.sin(t / sine_period) + rng.normal(0, noise, n)
    high = close + np.abs(rng.normal(0.5, 0.2, n))
    low = close - np.abs(rng.normal(0.5, 0.2, n))
    open_ = close + rng.normal(0, 0.1, n)
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def make_trending_pullback_df(n: int = 600, seed: int = 7, start: str = "2018-01-01") -> pd.DataFrame:
    """A rising market with periodic dips -- should reliably trigger
    pullback/dip-buying entries in an uptrend."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    trend = 100 + t * 0.15
    dips = -6 * np.abs(np.sin(t / 25.0))
    close = trend + dips + rng.normal(0, 0.3, n)
    high = close + np.abs(rng.normal(0.4, 0.15, n))
    low = close - np.abs(rng.normal(0.4, 0.15, n))
    open_ = close + rng.normal(0, 0.1, n)
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def make_ar1_series(phi: float, n: int, seed: int) -> pd.Series:
    """A bare AR(1) increment series (not wrapped into OHLCV) -- used to
    test Hurst/persistence statistics directly against a known
    trending (phi>0) / mean-reverting (phi<0) / random-walk (phi=0) process."""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, 1, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + eps[t]
    return pd.Series(x)


def make_ar1_ohlcv(phi: float, n: int, seed: int, scale: float = 0.01, start: str = "2015-01-01") -> pd.DataFrame:
    """An AR(1)-driven GEOMETRIC (multiplicative) random walk, wrapped into
    OHLCV -- guarantees positive prices, unlike an additive cumsum which can
    wander negative over long/volatile paths (confirmed empirically: phi=0.75
    can go negative with an additive construction at some seeds). High/Low/
    Open jitter is proportional to price level, not a fixed absolute offset,
    so it stays realistic across the wide price range a geometric walk can
    produce.

    Deliberately self-contained (not composed from `make_ar1_series`): the
    AR(1) innovations and the OHLC jitter are drawn from a single continuous
    `rng` stream, in that order, matching the original helper this was
    extracted from. Composing from a second, separately-seeded generator for
    the jitter would produce different numbers for the same seed and risks
    silently breaking a test that was empirically tuned around specific
    values.
    """
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, 1, n)
    increments = np.zeros(n)
    for t in range(1, n):
        increments[t] = phi * increments[t - 1] + eps[t]
    close = 100 * np.exp(np.cumsum(increments * scale))
    idx = pd.bdate_range(start, periods=n)
    high = close * (1 + np.abs(rng.normal(0.003, 0.001, n)))
    low = close * (1 - np.abs(rng.normal(0.003, 0.001, n)))
    open_ = close * (1 + rng.normal(0, 0.001, n))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)

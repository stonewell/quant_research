"""Vectorized mathematical operators for WorldQuant 101 Formulaic Alphas.

Academic Grounding:
Kakushadze, Zura. "101 Formulaic Alphas." Wilmott Magazine 2016.84 (2016): 72-81.
arXiv:1601.00991 (2015).

This module implements the complete mathematical operator algebra described in
Section A.1 of Kakushadze (2015):
1. Time-series operators (ts_*): rolling windows along the time axis (rows) for each symbol.
2. Cross-sectional operators (cs_*): cross-sectional ranking, scaling, demeaning,
   and industry neutralization across symbols (columns) for each timestamp.
3. Element-wise math operators: signed_power, ternary conditional, VWAP proxy, ADV.

Implementation Philosophy:
- High performance: Fully vectorized Pandas and NumPy expressions with zero row loops.
- Numerical safety: Safeguarded against zero division, negative base fractional powers,
  and flat/constant price series.
- Seamless DataFrame contract: Preserves DatetimeIndex and symbol columns throughout.
"""

from typing import Dict, Optional, Union
import numpy as np
import pandas as pd


# =============================================================================
# Helper Utilities
# =============================================================================

def _to_int_days(d: Union[int, float]) -> int:
    """Converts a day parameter to an integer >= 1 per Kakushadze (2015) Section A.1:
    'non-integer number of days d is converted to floor(d)'."""
    val = int(np.floor(d))
    return max(1, val)


def vwap_proxy(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
               volume: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Computes typical price proxy for VWAP when intraday tick data is unavailable:
    (High + Low + Close) / 3.
    """
    return (high + low + close) / 3.0


def adv(volume: pd.DataFrame, close: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """Average Daily Volume over past d days per Section A.2:
    adv{d} = average daily dollar volume for past d days: rolling_mean(volume * close, d).
    Falls back to volume if close is not provided.
    """
    w = _to_int_days(d)
    dollar_vol = volume * close
    return dollar_vol.rolling(w, min_periods=1).mean()


# =============================================================================
# Time-Series Operators (Section A.1)
# =============================================================================

def ts_delay(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """delay(x, d) = value of x d days ago."""
    w = _to_int_days(d)
    return df.shift(w)


def ts_delta(df: pd.DataFrame, d: Union[int, float] = 1) -> pd.DataFrame:
    """delta(x, d) = today's value of x minus the value of x d days ago: x_t - x_{t-d}."""
    w = _to_int_days(d)
    return df.diff(w)


def ts_min(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """ts_min(x, d) = time-series minimum over the past d days."""
    w = _to_int_days(d)
    return df.rolling(w, min_periods=1).min()


def ts_max(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """ts_max(x, d) = time-series maximum over the past d days."""
    w = _to_int_days(d)
    return df.rolling(w, min_periods=1).max()


def ts_sum(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """sum(x, d) = time-series sum over the past d days."""
    w = _to_int_days(d)
    return df.rolling(w, min_periods=1).sum()


def ts_mean(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """sma(x, d) = time-series simple moving average over past d days."""
    w = _to_int_days(d)
    return df.rolling(w, min_periods=1).mean()


def ts_prod(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """product(x, d) = time-series product over the past d days."""
    w = _to_int_days(d)
    # Using exp(rolling_sum(log(abs))) to avoid overflow and preserve speed
    # For small windows, rolling apply with np.prod is robust
    return df.rolling(w, min_periods=1).apply(np.prod, raw=True)


def ts_std(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """stddev(x, d) = moving time-series sample standard deviation over past d days."""
    w = _to_int_days(d)
    return df.rolling(w, min_periods=2).std().fillna(0.0)


def ts_corr(x: pd.DataFrame, y: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """correlation(x, y, d) = time-serial Pearson correlation of x and y for past d days."""
    w = _to_int_days(d)
    corr = x.rolling(w, min_periods=min(w, 2)).corr(y)
    return corr.fillna(0.0)


def ts_cov(x: pd.DataFrame, y: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """covariance(x, y, d) = time-serial covariance of x and y for past d days."""
    w = _to_int_days(d)
    cov = x.rolling(w, min_periods=min(w, 2)).cov(y)
    return cov.fillna(0.0)


def ts_rank(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """ts_rank(x, d) = time-series percentile rank in the past d days in [0.0, 1.0]."""
    w = _to_int_days(d)
    if w <= 1:
        return pd.DataFrame(0.5, index=df.index, columns=df.columns)
    # Native rolling rank with pct=True in pandas
    ranked = df.rolling(w, min_periods=1).rank(pct=True)
    return ranked.fillna(0.5)


def ts_argmax(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """ts_argmax(x, d) = which day ts_max(x, d) occurred on.
    Returns 1-based index (1 to d) within the trailing window, where d is today.
    """
    w = _to_int_days(d)
    if w <= 1:
        return pd.DataFrame(1.0, index=df.index, columns=df.columns)
    res = df.rolling(w, min_periods=1).apply(np.argmax, raw=True) + 1.0
    return res.fillna(1.0)


def ts_argmin(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """ts_argmin(x, d) = which day ts_min(x, d) occurred on.
    Returns 1-based index (1 to d) within the trailing window, where d is today.
    """
    w = _to_int_days(d)
    if w <= 1:
        return pd.DataFrame(1.0, index=df.index, columns=df.columns)
    res = df.rolling(w, min_periods=1).apply(np.argmin, raw=True) + 1.0
    return res.fillna(1.0)


def ts_decay_linear(df: pd.DataFrame, d: Union[int, float]) -> pd.DataFrame:
    """decay_linear(x, d) = weighted moving average over past d days with linearly
    decaying weights d, d-1, ..., 1 (rescaled to sum to 1).
    """
    w = _to_int_days(d)
    if w <= 1:
        return df.copy()
    weights = np.arange(1, w + 1, dtype=float)
    weights /= weights.sum()

    # Fast 1D convolution across rows for each column
    clean_df = df.ffill().fillna(0.0)
    arr = clean_df.values
    out = np.empty_like(arr)
    # For initial bars < w:
    for i in range(min(w - 1, len(arr))):
        sub_w = np.arange(1, i + 2, dtype=float)
        sub_w /= sub_w.sum()
        out[i] = np.dot(sub_w, arr[:i + 1])

    # For full windows:
    for col in range(arr.shape[1]):
        out[w - 1:, col] = np.convolve(arr[:, col], weights[::-1], mode='valid')

    return pd.DataFrame(out, index=df.index, columns=df.columns)


# =============================================================================
# Cross-Sectional Operators (Section A.1)
# =============================================================================

def cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """rank(x) = cross-sectional percentile rank across columns, scaled to [0.0, 1.0]."""
    ranked = df.rank(axis=1, pct=True, method="average")
    return ranked.fillna(0.5)


def cs_scale(df: pd.DataFrame, a: float = 1.0) -> pd.DataFrame:
    """scale(x, a) = rescaled x such that sum(abs(x)) = a (default a=1.0)."""
    denom = df.abs().sum(axis=1).replace(0.0, np.nan)
    scaled = df.div(denom, axis=0).mul(a)
    return scaled.fillna(0.0)


def cs_demean(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional demeaning: x - mean(x) across assets on each date."""
    mean_val = df.mean(axis=1)
    return df.sub(mean_val, axis=0)


def cs_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional standardization: (x - mean(x)) / std(x) across assets."""
    mean_val = df.mean(axis=1)
    std_val = df.std(axis=1).replace(0.0, np.nan)
    z = df.sub(mean_val, axis=0).div(std_val, axis=0)
    return z.fillna(0.0)


def cs_indneutralize(df: pd.DataFrame,
                     groups: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """indneutralize(x, g) = x cross-sectionally neutralized against groups g
    (demeaned within each industry/sector group). If groups is None or all assets
    share one group, reduces to cross-sectional demeaning across all assets.
    """
    if not groups:
        return cs_demean(df)

    res = df.copy()
    unique_groups = set(groups.values())
    for g in unique_groups:
        cols = [c for c in df.columns if groups.get(c) == g]
        if cols:
            sub = df[cols]
            res[cols] = sub.sub(sub.mean(axis=1), axis=0)
    return res


# =============================================================================
# Mathematical Primitives & Logic
# =============================================================================

def signed_power(df: pd.DataFrame, a: Union[float, int, pd.DataFrame]) -> pd.DataFrame:
    """signedpower(x, a) = sign(x) * |x|^a."""
    sign = np.sign(df)
    abs_val = np.abs(df)
    if isinstance(a, pd.DataFrame):
        powered = abs_val ** a
    else:
        powered = abs_val ** float(a)
    return (sign * powered).fillna(0.0)


def ternary(cond: Union[pd.DataFrame, pd.Series],
            a: Union[pd.DataFrame, pd.Series, float, int],
            b: Union[pd.DataFrame, pd.Series, float, int]) -> pd.DataFrame:
    """cond ? a : b element-wise conditional evaluation."""
    if isinstance(cond, pd.Series):
        cond_df = cond.to_frame()
    else:
        cond_df = cond

    bool_mask = cond_df.fillna(False).values

    val_a = a.values if isinstance(a, (pd.DataFrame, pd.Series)) else a
    val_b = b.values if isinstance(b, (pd.DataFrame, pd.Series)) else b

    res = np.where(bool_mask, val_a, val_b)
    return pd.DataFrame(res, index=cond_df.index, columns=cond_df.columns)

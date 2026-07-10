"""Shared technical indicators used across every project in this workspace:
RSI (Wilder + Cutler variants), SMA, ATR/ATR%, ADX, realized volatility,
volatility-of-volatility, and the ATR volatility-regime-change ratio.

Each project's own `indicators.py`/`volatility.py` re-exports the subset it
needs (and keeps any project-specific logic, like a trend-regime classifier
or a config-driven summary dict, local to that project) so existing call
sites and signatures are unchanged.
"""

import numpy as np
import pandas as pd


def _gains_losses(close: pd.Series) -> tuple:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    return gain, loss


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI: recursively smooths average gain/loss with weight 1/n
    on the newest bar (equivalent to a (2n-1)-period EMA)."""
    gain, loss = _gains_losses(close)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return result.where(avg_loss != 0, 100.0)


def rsi_cutler(close: pd.Series, period: int) -> pd.Series:
    """Cutler's/"plain" RSI: simple moving average of gains/losses instead
    of Wilder's recency-weighted smoothing, trading recency weighting for a
    result that doesn't depend on how far back the calculation window starts."""
    gain, loss = _gains_losses(close)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return result.where(avg_loss != 0, 100.0)


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Plain alias for Wilder's RSI -- the default most callers want when
    there's no need to choose between smoothing methods."""
    return rsi_wilder(close, period)


def cumulative_rsi(rsi_series: pd.Series, lookback: int) -> pd.Series:
    """Sum of RSI over the trailing `lookback` bars (the Connors 'cumulative RSI(2)' variant)."""
    return rsi_series.rolling(window=lookback, min_periods=lookback).sum()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range. df must have columns High, Low, Close."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return atr(df, period) / df["Close"]


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average Directional Index -- conventionally, ADX >= ~25
    signals a trending market, ADX <= ~20 a ranging one."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def realized_vol(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    returns = close.pct_change()
    return returns.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)


def vol_of_vol(close: pd.Series, vol_window: int = 20, vov_window: int = 60,
               periods_per_year: int = 252) -> pd.Series:
    """Rolling std of the realized-vol series itself -- a volatility-
    clustering measure: high vol-of-vol means volatility regimes are
    themselves unstable, harder for any strategy to size risk against."""
    vol = realized_vol(close, vol_window, periods_per_year)
    return vol.rolling(vov_window, min_periods=vov_window).std()


def atr_regime_ratio(df: pd.DataFrame, period: int = 14, short_window: int = 20, long_window: int = 60) -> pd.Series:
    """Short-term ATR% vs its own longer-term average. A ratio >= 1.30 is a
    commonly-cited illustrative trigger for a volatility-regime change (cut
    size, widen stops); well below 1.0 signals volatility compression."""
    a_pct = atr_pct(df, period)
    short_avg = a_pct.rolling(short_window, min_periods=short_window).mean()
    long_avg = a_pct.rolling(long_window, min_periods=long_window).mean()
    return short_avg / long_avg

"""Volatility characteristics: realized volatility, ATR%, volatility-of-
volatility (clustering), ATR-regime-change ratio, and ADX (trend strength).

These jointly answer: is there enough volatility to generate tradable
moves and cover costs, is that volatility range-bound (grid-friendly) or
directional (trend-friendly), and is the current regime stable or shifting?
"""

import numpy as np
import pandas as pd


def realized_vol(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    returns = close.pct_change()
    return returns.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return atr(df, period) / df["Close"]


def vol_of_vol(close: pd.Series, vol_window: int = 20, vov_window: int = 60,
               periods_per_year: int = 252) -> pd.Series:
    """Rolling std of the realized-vol series itself -- a volatility-
    clustering measure: high vol-of-vol means volatility regimes are
    themselves unstable, harder for any strategy to size risk against."""
    vol = realized_vol(close, vol_window, periods_per_year)
    return vol.rolling(vov_window, min_periods=vov_window).std()


def atr_regime_ratio(df: pd.DataFrame, period: int = 14, short_window: int = 20, long_window: int = 60) -> pd.Series:
    """Short-term ATR% vs its own longer-term average. Research-documented
    illustrative trigger: a ratio 30% above 1.0 (i.e., >= 1.30) signals a
    volatility-regime change (cut size, widen stops); a value well below 1.0
    signals volatility compression (grid spacing may need tightening)."""
    a_pct = atr_pct(df, period)
    short_avg = a_pct.rolling(short_window, min_periods=short_window).mean()
    long_avg = a_pct.rolling(long_window, min_periods=long_window).mean()
    return short_avg / long_avg


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX -- conventionally, >~25 signals a trending regime,
    <~20 a ranging one (illustrative thresholds, not a universal law)."""
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


def volatility_summary(df: pd.DataFrame, config) -> dict:
    rv = realized_vol(df["Close"], config.realized_vol_window)
    ap = atr_pct(df, config.atr_period)
    vov = vol_of_vol(df["Close"], config.realized_vol_window)
    regime_ratio = atr_regime_ratio(df, config.atr_period, config.atr_short_window, config.atr_long_window)
    adx_series = adx(df, config.adx_period)

    return {
        "realized_vol_annualized_pct": rv.mean(skipna=True) * 100,
        "atr_pct_mean": ap.mean(skipna=True) * 100,
        "vol_of_vol": vov.mean(skipna=True),
        "pct_days_vol_regime_change": (regime_ratio.dropna().sub(1).abs() >= config.atr_regime_change_threshold).mean() * 100,
        "adx_mean": adx_series.mean(skipna=True),
        "pct_days_trending_adx": (adx_series.dropna() >= config.adx_trend_threshold).mean() * 100,
        "pct_days_ranging_adx": (adx_series.dropna() <= config.adx_range_threshold).mean() * 100,
    }

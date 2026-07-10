"""Technical indicators used to drive the adaptive grid: ATR for volatility-based
spacing, and a long-term SMA band used as a trend filter.
"""

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range.

    df must have columns High, Low, Close. Returns a Series aligned to df.index.
    Uses Wilder's smoothing (equivalent to an EWM with alpha = 1/period), the
    standard ATR definition used in the ATR-grid-spacing sources reviewed.
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def trend_regime(close: pd.Series, ma_period: int = 100, band_pct: float = 0.03) -> pd.Series:
    """Classify each bar as 'up', 'down', or 'range' relative to a long-term SMA.

    Rationale (research-backed): grid bots lose money buying every dip in a
    downtrend or selling every rally in an uptrend, so a trend filter is used
    to gate grid activity. Price within +-band_pct of the MA is treated as
    range-bound (grid trading allowed on both sides); outside the band the
    market is trending, so the strategy should stop opening new positions
    against the trend.
    """
    ma = sma(close, ma_period)
    upper = ma * (1 + band_pct)
    lower = ma * (1 - band_pct)
    regime = pd.Series(np.where(close > upper, "up", np.where(close < lower, "down", "range")), index=close.index)
    regime[ma.isna()] = np.nan
    return regime

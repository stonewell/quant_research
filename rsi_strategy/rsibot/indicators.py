"""RSI (two calculation methods) and supporting moving averages.

Wilder's RSI recursively smooths average gain/loss with weight 1/n on the
newest bar (equivalent to a (2n-1)-period EMA). Cutler's/"plain" RSI instead
uses a simple moving average of gains/losses, trading Wilder's recency
weighting for a result that doesn't depend on how far back the calculation
window starts. Both are documented, real variants -- which one a backtest
uses is a genuine, reproducibility-relevant choice, not just an implementation
detail, so it's exposed as a config option rather than hard-coded.
"""

import numpy as np
import pandas as pd


def _gains_losses(close: pd.Series) -> tuple:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    return gain, loss


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    gain, loss = _gains_losses(close)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(avg_loss != 0, 100.0)


def rsi_cutler(close: pd.Series, period: int) -> pd.Series:
    gain, loss = _gains_losses(close)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(avg_loss != 0, 100.0)


def rsi(close: pd.Series, period: int, method: str = "wilder") -> pd.Series:
    if method == "wilder":
        return rsi_wilder(close, period)
    if method == "cutler":
        return rsi_cutler(close, period)
    raise ValueError(f"Unknown RSI method: {method!r} (expected 'wilder' or 'cutler')")


def cumulative_rsi(rsi_series: pd.Series, lookback: int) -> pd.Series:
    """Sum of RSI over the trailing `lookback` bars (the Connors 'cumulative RSI(2)' variant)."""
    return rsi_series.rolling(window=lookback, min_periods=lookback).sum()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()

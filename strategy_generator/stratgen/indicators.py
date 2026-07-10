"""RSI (Wilder), SMA, and ATR -- the small, deliberately restricted set of
long-established indicator primitives used to build strategy templates.

Research grounding: unconstrained/highly flexible primitive sets in
automated rule search are documented as especially prone to data-snooping
bias (Allen & Karjalainen 1999). Their own mitigation -- restricting the
primitive set to a small number of simple, economically-motivated
constructs rather than an arbitrary function set -- is deliberately followed
here instead of building a full genetic-programming symbolic search.
"""

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return result.where(avg_loss != 0, 100.0)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return atr(df, period) / df["Close"]

"""Regime classification: for each bar, decide which sleeve should be in
control -- trend-following (buy-and-hold), tactical mean-reversion (RSI-2),
or cash.

Every column returned here is already shifted by one bar, so "the regime for
bar t" reflects only information known through bar t-1's close. The
backtester acts on that decision at bar t's OPEN. This is the no-lookahead
discipline research flagged as "the cardinal sin" of regime-switching
backtests -- fit/decide only on data available before the bar you're trading.
"""

import numpy as np
import pandas as pd

from .config import EnsembleConfig
from .indicators import adx, rsi, sma


def apply_hysteresis(adx_series: pd.Series, trend_threshold: float, range_threshold: float) -> pd.Series:
    """Classify each bar 'trend' (ADX >= trend_threshold) or 'range' (ADX <=
    range_threshold); in the dead zone between the two thresholds, carry the
    previous classification forward instead of flipping, to reduce whipsaw."""
    raw = pd.Series(np.where(
        adx_series >= trend_threshold, "trend",
        np.where(adx_series <= range_threshold, "range", None)
    ), index=adx_series.index, dtype=object)
    return raw.ffill().fillna("range")


def classify_regime(df: pd.DataFrame, config: EnsembleConfig) -> pd.DataFrame:
    close = df["Close"]
    out = pd.DataFrame(index=df.index)

    trend_ma = sma(close, config.trend_ma_period)
    adx_series = adx(df, config.adx_period)
    rsi_series = rsi(close, config.rsi_period)

    long_term_uptrend = close > trend_ma
    sub_regime = apply_hysteresis(adx_series, config.adx_trend_threshold, config.adx_range_threshold)
    regime = pd.Series(np.where(long_term_uptrend, sub_regime, "downtrend"), index=df.index)

    # Shift everything by 1: bar t's decision uses only data through bar t-1's close.
    out["regime"] = regime.shift(1)
    out["rsi"] = rsi_series.shift(1)
    out["adx"] = adx_series.shift(1)
    out["trend_ma"] = trend_ma.shift(1)
    out["long_term_uptrend"] = long_term_uptrend.shift(1)
    return out

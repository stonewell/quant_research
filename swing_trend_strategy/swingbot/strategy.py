"""Vectorized signal computation for the trend-pullback swing strategy.

Entry (verified rule set): price above a rising 200-day SMA (uptrend), price
below its 20-day SMA (temporary pullback), and 5-period RSI below 45.
Mean-reversion exit signal: RSI back above 65. The backtester layers a
stop-loss, profit target, trailing stop, and max-holding-period on top of
this signal -- see config.py for why.
"""

import pandas as pd

from .config import SwingConfig
from .indicators import rsi, sma


def generate_signals(df: pd.DataFrame, config: SwingConfig) -> pd.DataFrame:
    close = df["Close"]
    out = pd.DataFrame(index=df.index)

    out["trend_ma"] = sma(close, config.trend_ma_period)
    out["pullback_ma"] = sma(close, config.pullback_ma_period)
    out["rsi"] = rsi(close, config.rsi_period)

    trend_ok = close > out["trend_ma"]
    if config.require_rising_trend_ma:
        trend_ok &= out["trend_ma"] > out["trend_ma"].shift(config.trend_slope_lookback)
    out["trend_ok"] = trend_ok

    pullback_ok = close < out["pullback_ma"]
    rsi_ok = out["rsi"] < config.entry_rsi_threshold

    out["entry_signal"] = out["trend_ok"] & pullback_ok & rsi_ok
    out["exit_signal"] = out["rsi"] > config.exit_rsi_threshold

    return out

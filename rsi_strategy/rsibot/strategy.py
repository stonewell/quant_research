"""Vectorized signal computation for the RSI-2 mean-reversion strategy.

Pure indicator/condition math -- no state, no money. The backtester decides
when signals are actually acted on (next bar's open, to avoid lookahead) and
handles position/cash accounting.
"""

import pandas as pd

from .config import RSIConfig
from .indicators import cumulative_rsi, rsi, sma


def generate_signals(df: pd.DataFrame, config: RSIConfig) -> pd.DataFrame:
    close = df["Close"]
    out = pd.DataFrame(index=df.index)

    out["rsi"] = rsi(close, config.rsi_period, config.rsi_method)
    out["trend_ma"] = sma(close, config.trend_ma_period)
    out["exit_ma"] = sma(close, config.exit_ma_period)

    if config.entry_mode == "cumulative":
        out["entry_metric"] = cumulative_rsi(out["rsi"], config.cumulative_lookback)
        entry_trigger = out["entry_metric"] < config.cumulative_threshold
    elif config.entry_mode == "single":
        out["entry_metric"] = out["rsi"]
        entry_trigger = out["entry_metric"] < config.oversold_threshold
    else:
        raise ValueError(f"Unknown entry_mode: {config.entry_mode!r} (expected 'single' or 'cumulative')")

    out["trend_ok"] = (close > out["trend_ma"]) if config.require_trend_filter else True
    out["entry_signal"] = entry_trigger & out["trend_ok"]

    exit_rsi_ok = out["rsi"] > config.exit_rsi_threshold
    exit_ma_ok = close > out["exit_ma"]
    if config.exit_mode == "rsi_cross":
        out["exit_signal"] = exit_rsi_ok
    elif config.exit_mode == "ma_cross":
        out["exit_signal"] = exit_ma_ok
    elif config.exit_mode == "either":
        out["exit_signal"] = exit_rsi_ok | exit_ma_ok
    else:
        raise ValueError(f"Unknown exit_mode: {config.exit_mode!r} (expected 'rsi_cross', 'ma_cross', or 'either')")

    return out

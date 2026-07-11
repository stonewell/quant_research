"""Technical indicators used to drive the adaptive grid: ATR for volatility-based
spacing, and a long-term SMA band used as a trend filter.

`atr` and `sma` are re-exported from the shared `common/indicators.py`
module; `trend_regime` is specific to this project's grid strategy.
"""

import numpy as np
import pandas as pd
from common.indicators import atr, sma


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

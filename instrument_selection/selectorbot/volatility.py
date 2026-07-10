"""Volatility characteristics: realized volatility, ATR%, volatility-of-
volatility (clustering), ATR-regime-change ratio, and ADX (trend strength).

These jointly answer: is there enough volatility to generate tradable
moves and cover costs, is that volatility range-bound (grid-friendly) or
directional (trend-friendly), and is the current regime stable or shifting?

All indicator math is re-exported from the shared `common/indicators.py`
module; `volatility_summary` (this project's config-driven report) stays local.
"""

import pandas as pd
from common.indicators import adx, atr, atr_pct, atr_regime_ratio, realized_vol, vol_of_vol


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

"""Liquidity/tradability metrics.

Two complementary measures, since research found no single verified numeric
liquidity cutoff (requirements are strategy- and position-size-dependent):
- Average dollar volume: the standard, simple tradability proxy.
- The Corwin & Schultz (2012, Journal of Finance) high-low spread estimator:
  a well-established academic method for estimating the bid-ask spread from
  daily OHLC data alone, when no tick/quote data is available. It exploits
  the fact that a day's high-low range reflects both true volatility and the
  bid-ask bounce, while a 2-day range reflects volatility over twice the
  time but the same bounce component -- letting the two be disentangled.
"""

import numpy as np
import pandas as pd

_K = 3 - 2 * np.sqrt(2)  # Corwin-Schultz constant


def corwin_schultz_spread(df: pd.DataFrame) -> pd.Series:
    """Estimated proportional bid-ask spread for each day, using that day's
    and the prior day's High/Low (Corwin & Schultz, 2012). Negative raw
    estimates (a known artifact of the method) are floored at zero."""
    high, low = df["High"], df["Low"]
    single_day_sq = np.log(high / low) ** 2
    beta = single_day_sq + single_day_sq.shift(1)

    two_day_high = high.rolling(2).max()
    two_day_low = low.rolling(2).min()
    gamma = np.log(two_day_high / two_day_low) ** 2

    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / _K - np.sqrt(gamma / _K)
    spread = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return spread.clip(lower=0)


def avg_dollar_volume(df: pd.DataFrame, window: int = 60) -> pd.Series:
    return (df["Close"] * df["Volume"]).rolling(window, min_periods=window).mean()


def liquidity_summary(df: pd.DataFrame, window: int = 60) -> dict:
    dollar_vol = avg_dollar_volume(df, window)
    spread = corwin_schultz_spread(df)
    return {
        "avg_dollar_volume": dollar_vol.mean(skipna=True),
        "median_dollar_volume": dollar_vol.median(skipna=True),
        "median_spread_pct": spread.median(skipna=True) * 100,
        "spread_pct_p90": spread.quantile(0.90) * 100,
    }

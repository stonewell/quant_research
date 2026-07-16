"""Pairs trading signal calculation (Gatev, Goetzmann & Rouwenhorst 2006,
Review of Financial Studies -- the seminal, peer-reviewed "distance method"
pairs-trading study). Deliberately kept out of `templates.py`'s
`Template`/`TEMPLATES_BY_REGIME` machinery: every other template here signals
off ONE instrument's own OHLCV, routed by that instrument's own regime, and
is executed by a single-position 0%/100%-long backtester. Pairs trading is
inherently a TWO-instrument, market-neutral long-short strategy -- forcing it
into that interface would misrepresent the mechanism the evidence is about.
It's a different SHAPE of candidate, not a lesser one: `generator.py`'s
`StrategyGenerator.generate()` searches this family (via `pairs_search.py`)
as a first-class alternative to the single-symbol templates, and returns
whichever is better-supported by the evidence -- see that module's docstring
for how it removed the generator's original single-instrument-per-symbol
architecture limit. See `pairs_backtester.py` for the matching dollar-neutral
execution model, and ../README.md for the full research grounding and
confidence levels.

Disclosed simplifications versus the original paper, both deliberate:

1. GGR re-picks pairs and re-estimates the spread's mean/std at the boundary
   of discrete, non-overlapping 12-month formation / 6-month trading blocks.
   This module instead uses a single continuously-ROLLING lookback window for
   both, which is simpler, is directly walk-forward-safe (no block
   bookkeeping), and is how most practitioner implementations of the same
   idea are actually built today -- but it is a real simplification, not a
   reproduction of the original block design.
2. GGR's own reported average holding period for an open pair position was
   3.75-4 months (they explicitly call it a "medium-term" strategy) -- longer
   than this project's <3-month target. `PairsConfig.max_holding_days`
   defaults to 63 trading days (~3 months) as a hard cap the original design
   did not have, forced specifically to keep holds under that ceiling.
3. This is inherently long-short (short the "rich" leg, long the "cheap"
   leg) -- it cannot be made long-only without destroying the market-neutral
   mechanism the evidence is about, unlike every other template in this
   project.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PairsConfig:
    name: str = "distance_pairs"
    lookback: int = 60          # rolling window for the spread's mean/std (trading days)
    entry_zscore: float = 2.0   # GGR's own threshold: open when the spread diverges >2 historical std devs
    exit_zscore: float = 0.5    # fixed, not searched -- "close to convergence" band around zero
    stop_zscore: float = 4.0    # fixed, not searched -- cointegration-breakdown safety net (see pairs_backtester)
    max_holding_days: int = 63  # fixed, not searched -- forces holds under this project's 3-month ceiling


def spread_zscore(price_a: pd.Series, price_b: pd.Series, lookback: int) -> pd.Series:
    """Rolling z-score of the log price spread log(a) - log(b) -- the
    standard modernization of GGR's normalized-price divergence test."""
    log_spread = np.log(price_a) - np.log(price_b)
    mean = log_spread.rolling(lookback, min_periods=lookback).mean()
    std = log_spread.rolling(lookback, min_periods=lookback).std()
    return (log_spread - mean) / std.replace(0, np.nan)


def pairs_signals(df_a: pd.DataFrame, df_b: pd.DataFrame, config: PairsConfig) -> pd.DataFrame:
    """`df_a`/`df_b` must share the same DatetimeIndex (align/inner-join the
    two instruments' calendars before calling, same convention as this
    workspace's other multi-symbol tooling in stratgen/generator.py). Returns
    booleans for both entry directions plus the exit/stop conditions;
    `pairs_backtester.run_pairs_backtest` decides which one direction the
    z-score's sign implies to actually enter, at the next bar's open."""
    z = spread_zscore(df_a["Close"], df_b["Close"], config.lookback)
    out = pd.DataFrame(index=df_a.index)
    out["zscore"] = z
    out["enter_short_a_long_b"] = (z > config.entry_zscore).fillna(False)   # a rich vs b: short a, long b
    out["enter_long_a_short_b"] = (z < -config.entry_zscore).fillna(False)  # a cheap vs b: long a, short b
    out["exit_signal"] = (z.abs() < config.exit_zscore).fillna(False)
    out["stop_signal"] = (z.abs() > config.stop_zscore).fillna(False)
    return out

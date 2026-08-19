"""Candlestick-pattern predictability: does an instrument's OHLC structure
carry any statistically significant, exploitable information beyond the raw
open/close drift -- tested against a placebo/bootstrap null, exactly like
`persistence.hurst_significance` tests the Hurst exponent, and gated on that
significance so pure noise scores near zero.

This is deliberately a STRATEGY-AGNOSTIC, information-content question (the
same one `persistence.py` asks with the Hurst exponent), NOT a "trade this
candlestick pattern" signal. The direction of any detected edge is reported
in `candlestick_label` for whatever strategy decision you make separately;
the score itself only asks whether ANY exploitable candlestick structure is
present in EITHER direction.

WHY THIS COMPONENT IS SMALL, GATED, AND EXPECTED TO BE NEAR-ZERO FOR MOST
LIQUID INSTRUMENTS -- the honest, adversarially-checked research picture:

- The FIRST rigorous, large-scale test in favour: Caginalp & Laurent (1998,
  *Applied Mathematical Finance* 5:181-206, "The Predictive Power of Price
  Patterns") ran a non-parametric test on daily OHLC of all S&P 500 stocks
  1992-1996 and found three-day candlestick reversal patterns predicted a
  reversal out-of-sample at ~36 standard deviations from the null, ~1% over a
  two-day hold. Their core statistic -- does the CONDITIONAL probability of a
  reversal given a pattern exceed the UNCONDITIONAL probability p0 -- is the
  information-content test this module implements (direction-agnostic).
- The DEFINITIVE, more rigorous test AGAINST: Marshall, Young & Rose (2006,
  *Journal of Banking & Finance* 30(8):2303-2323) built an extension of the
  Efron (1979) bootstrap that resamples random OHLC series, and found
  candlestick strategies have NO value on DJIA stocks 1992-2002 -- "further
  evidence that this market is informationally efficient." The bootstrap-null
  comparison (real edge vs. the edge you'd get from randomly-relocated
  signals) is exactly what `candlestick_significance` below does; ours is a
  coarser random-date placebo, not their full OHLC resample -- documented as
  such, the same honesty caveat `hurst_significance` carries.
- Corroborated by later work across markets: a Swedish OMXS30 study
  (2007-2015) found poor predictive power / weak-form efficiency; an intraday
  DJIA 5-minute study (Etienne et al., 30 DJIA stocks) found a third of rules
  beat buy-and-hold at the Bonferroni level but NONE survived transaction
  costs and the SSPA data-snooping correction.

Net: for liquid US equities/ETFs the weight of rigorous evidence says
candlestick patterns carry little-to-no exploitable information once you
correct for data snooping and the base-rate drift. So -- exactly like this
project's Hurst result, where 15 of 16 broad ETFs came back
random-walk-like -- EXPECT `candlestick_significant` to be False for most
instruments; a non-zero, significant candlestick edge is a flag that an
instrument is unusual and worth investigating, not a validated trading edge.
That is why `scoring.py` gives this component a deliberately small weight and
gates it on significance, and why the composite treats an insignificant
result as ~zero rather than noise-ranking on it.
"""

import numpy as np
import pandas as pd

from common.indicators import bearish_reversal_signals, bullish_reversal_signals
from common.significance import StopSurrogates, shuffle_null_test


def _directional_edge(bull: pd.Series, bear: pd.Series, fwd: pd.Series) -> tuple:
    """Mean signal-conditional forward return in the pattern's OWN predicted
    direction (+ after bullish, - after bearish), net of the unconditional
    baseline drift each direction would earn on a random day. Returns
    (edge, n_signals)."""
    direction = pd.Series(0.0, index=bull.index)
    direction[bull.values] = 1.0
    direction[bear.values & ~bull.values] = -1.0  # a bar flagged both ways is treated as bullish only
    mask = (direction != 0) & fwd.notna()
    n = int(mask.sum())
    if n == 0:
        return np.nan, 0
    baseline = fwd.mean(skipna=True)
    directional_return = (direction[mask] * fwd[mask]).mean()
    directional_baseline = (direction[mask] * baseline).mean()
    return directional_return - directional_baseline, n


def candlestick_significance(df: pd.DataFrame, horizon: int = 5, n_surrogates: int = 200,
                             trend_window: int = 5, seed: int = None) -> dict:
    """Test whether pattern-conditional forward returns beat the base-rate
    drift by more than random signal placement would. The null keeps the
    SAME number of bullish/bearish signals but relocates them to random bars
    (a placebo test -- coarser than Marshall-Young-Rose's full OHLC bootstrap,
    finer than nothing), builds an empirical edge distribution, and reports a
    two-sided p-value. `horizon` is the forward-return holding window in bars
    (research uses 2-10 days; 5 is a mid-range default)."""
    close = df["Close"]
    fwd = close.shift(-horizon) / close - 1
    bull = bullish_reversal_signals(df, trend_window=trend_window)
    bear = bearish_reversal_signals(df, trend_window=trend_window)

    observed, n_signals = _directional_edge(bull, bear, fwd)
    n_bull, n_bear = int(bull.sum()), int((bear & ~bull).sum())
    if np.isnan(observed) or n_signals == 0:
        return {"candlestick_edge": np.nan, "candlestick_p_value": np.nan,
                "candlestick_significant": False, "candlestick_n_signals": n_signals,
                "candlestick_n_bullish": n_bull, "candlestick_n_bearish": n_bear}

    rng = np.random.default_rng(seed)
    valid_idx = np.flatnonzero(fwd.notna().to_numpy())

    def _surrogate_stat(rng):
        if len(valid_idx) < n_bull + n_bear:
            raise StopSurrogates
        picks = rng.choice(valid_idx, size=n_bull + n_bear, replace=False)
        fake_bull = pd.Series(False, index=df.index)
        fake_bear = pd.Series(False, index=df.index)
        fake_bull.iloc[picks[:n_bull]] = True
        fake_bear.iloc[picks[n_bull:]] = True
        edge, _ = _directional_edge(fake_bull, fake_bear, fwd)
        return edge

    result = shuffle_null_test(observed, _surrogate_stat, n_surrogates, rng)

    return {
        "candlestick_edge": observed,
        "candlestick_p_value": result["p_value"],
        "candlestick_significant": result["significant"],
        "candlestick_n_signals": n_signals,
        "candlestick_n_bullish": n_bull,
        "candlestick_n_bearish": n_bear,
    }


def candlestick_summary(df: pd.DataFrame, config) -> dict:
    """Config-driven per-instrument candlestick report. Returns NaN /
    'insufficient_data' when there isn't enough history or too few detected
    signals to test anything meaningfully -- the same hard-floor discipline
    `persistence_summary` applies to the Hurst estimate (research: an
    under-sampled edge estimate is worse than no estimate, so don't rank on
    it)."""
    close = df["Close"]
    if len(close.dropna()) < config.candlestick_min_obs:
        return {"candlestick_edge": np.nan, "candlestick_significant": False,
                "candlestick_p_value": np.nan, "candlestick_n_signals": 0,
                "candlestick_signal_rate": np.nan, "candlestick_label": "insufficient_data"}

    sig = candlestick_significance(df, horizon=config.candlestick_horizon,
                                   n_surrogates=config.candlestick_n_surrogates,
                                   trend_window=config.candlestick_trend_window)

    n_signals = sig["candlestick_n_signals"]
    if n_signals < config.candlestick_min_signals:
        label = "insufficient_signals"
    elif not sig["candlestick_significant"]:
        label = "no_edge"
    elif sig["candlestick_edge"] > 0:
        label = "bullish_edge"
    else:
        label = "bearish_edge"

    return {
        "candlestick_edge": sig["candlestick_edge"],
        "candlestick_significant": sig["candlestick_significant"] and n_signals >= config.candlestick_min_signals,
        "candlestick_p_value": sig["candlestick_p_value"],
        "candlestick_n_signals": n_signals,
        "candlestick_signal_rate": n_signals / len(close.dropna()),
        "candlestick_label": label,
    }

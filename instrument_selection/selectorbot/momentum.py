"""Time-series-momentum predictability: does an instrument's OWN past return
predict its future return -- the single most-replicated form of the "is there
exploitable structure?" question -- tested against a bootstrap null, exactly
like `persistence.hurst_significance` and `candlestick.candlestick_significance`,
and gated on that significance so a series with no real momentum scores near
zero.

This is deliberately a STRATEGY-AGNOSTIC, information-content question (the
same one §3 asks with the Hurst exponent and §3b with candlesticks), NOT a
"buy this because it's going up right now" timing signal. The trailing return
is reported as a point-in-time DESCRIPTOR only (`momentum_lookback_return`);
the scored quantity is `momentum_edge` -- the serial correlation between an
instrument's past `lookback`-day return and its subsequent `horizon`-day
return, which measures whether time-series momentum WORKS on this instrument
over the whole sample, in either direction (positive edge = trending/
persistent, negative edge = reversal/mean-reverting; reported in
`momentum_label`). Direction is descriptive; the score is the direction-
agnostic magnitude, gated on significance.

WHY MOMENTUM IS HERE, WHY IT'S SEPARATE FROM THE HURST CHANNEL, AND WHY IT'S
STILL TESTED PER-INSTRUMENT AGAINST A NULL RATHER THAN TRUSTED OUTRIGHT --
the honest, adversarially-checked research picture:

- **The evidence FOR (two of the most-cited anomalies in finance):**
  Jegadeesh & Titman (1993, *Journal of Finance* 48(1):65-91) documented
  CROSS-SECTIONAL momentum -- past 3-12-month winners keep beating past
  losers by ~1%/month over the next 3-12 months on NYSE/AMEX 1965-1989, not
  explained by systematic risk (though it partially reverses over the
  following two years). Moskowitz, Ooi & Pedersen (2012, *Journal of
  Financial Economics* 104(2):228-250) documented TIME-SERIES momentum -- a
  security's own past 12-month excess return positively predicts its next-
  month return for EVERY one of 58 liquid futures across equities, currencies,
  commodities and bonds, persisting ~a year then partially reversing. The
  time-series form is the one relevant to picking a SINGLE instrument, and it
  is what `momentum_edge` estimates.
- **Why it's a SEPARATE component from Hurst, not a duplicate:** the Hurst R/S
  statistic measures long-range memory over the whole sample via variance
  scaling; time-series momentum measures a specific, horizon-targeted serial
  correlation (past k-day vs. next h-day return) -- the exact, tradable,
  massively-replicated construct. They overlap conceptually (both detect
  trending persistence) and this project weights them so the family total is
  unchanged; but they are different estimators and can disagree, so both are
  reported. This overlap is disclosed, not hidden.
- **The critique AGAINST -- and the reason this is tested per-instrument with
  a bootstrap null:** Huang, Li, Wang & Zhou (2020, *Journal of Financial
  Economics* 135(3):774-794, "Time series momentum: Is it there?") showed that
  the headline TSM evidence comes from a POOLED regression whose large
  t-statistic (~4.3) is NOT statistically reliable -- it over-rejects
  no-predictability because of cross-asset mean differences, a persistent
  predictor, and volatility scaling. Their bootstrap-corrected, ASSET-BY-ASSET
  tests find little TSM in- or out-of-sample, and the strategy performs about
  the same as one based on the historical mean that needs no predictability at
  all. Their prescription -- test each instrument on its own, against a proper
  bootstrap null, not a pooled t-stat -- is EXACTLY what this module does
  (`momentum_edge` is demeaned via the Pearson correlation, stripping the
  drift that biased the pooled test, and gated on a shuffle null).
- **The technical-indicator angle (RSI/MACD/MA) specifically:** Park & Irwin
  (2007, *Journal of Economic Surveys*, "What Do We Know About the
  Profitability of Technical Analysis?") reviewed 95 modern studies (56
  positive / 20 negative / 19 mixed) and found most suffer data-snooping,
  ex-post rule selection and transaction-cost problems; their futures
  reality-check (Park & Irwin 2010) found that after White's Bootstrap Reality
  Check and Hansen's SPA data-snooping corrections, popular rules including
  RSI and MACD were significant in only 2 of 17 contracts and did not persist
  out-of-sample. So the classic momentum INDICATORS (RSI, MACD, ROC) are
  computed and reported here descriptively, but the SCORED quantity is the
  bootstrap-tested return-predictability edge, not a raw indicator signal.
- **The cross-cutting caveat even when momentum IS real:** Daniel & Moskowitz
  (2016, *JFE* 122(2):221-247, "Momentum crashes") show momentum returns are
  strongly negatively skewed (the WML portfolio's monthly skew is -4.70) with
  infrequent but severe, persistent crashes concentrated in panic states --
  after market declines, when volatility is high, contemporaneous with
  rebounds (e.g. Mar-May 2009: past losers +163% vs. past winners +8%). A
  high momentum edge is therefore NOT a free lunch; it carries left-tail
  crash risk that this single-number score cannot capture -- the same spirit
  as this project's unresolved correlation-spikes-in-a-crash caveat.

Net: like the Hurst and candlestick components, EXPECT `momentum_significant`
to be False for many broad, liquid instruments over long mixed-regime windows
(consistent with both this project's own random-walk-like Hurst finding and
Huang et al.'s weak per-asset TSM result). A significant momentum edge flags
an instrument whose own-return predictability is unusually strong on this
sample -- a lead to investigate, weighed small and crash-caveated, not a
validated trading edge. See `scoring.py` for the (deliberately modest) weight.
"""

import numpy as np
import pandas as pd

from common.indicators import macd, roc, rsi, sma  # noqa: F401  (re-exported technical momentum indicators)


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def _past_forward(log_price: pd.Series, lookback: int, horizon: int) -> tuple:
    """Overlapping (past `lookback`-day return, next `horizon`-day return)
    pairs. Using log prices makes both simple differences; the Pearson
    correlation of the two demeans each -- stripping the per-asset drift that
    Huang et al. (2020) show biases the raw pooled momentum test upward."""
    past = (log_price - log_price.shift(lookback))
    fwd = (log_price.shift(-horizon) - log_price)
    valid = past.notna() & fwd.notna()
    return past[valid].to_numpy(), fwd[valid].to_numpy()


def momentum_efficacy(close: pd.Series, lookback: int = 252, horizon: int = 21,
                      n_surrogates: int = 200, seed: int = None) -> dict:
    """Serial correlation between an instrument's past `lookback`-day return
    and its next `horizon`-day return (the statistical core of time-series
    momentum), tested against a shuffle null. The null shuffles daily log
    returns and rebuilds the price path -- destroying serial dependence while
    preserving the mechanical overlap of the sliding windows -- so a
    significant edge means "past returns predict future returns beyond what
    iid returns would produce," in the spirit of Huang et al. (2020)'s
    per-asset bootstrap. Two-sided p-value -> direction-agnostic significance."""
    lp = np.log(close.dropna())
    if len(lp) < lookback + horizon + 20:
        return {"momentum_edge": np.nan, "momentum_p_value": np.nan,
                "momentum_significant": False, "momentum_n_windows": 0}

    x, y = _past_forward(lp, lookback, horizon)
    observed = _corr(x, y)
    n_windows = len(x)
    if np.isnan(observed):
        return {"momentum_edge": np.nan, "momentum_p_value": np.nan,
                "momentum_significant": False, "momentum_n_windows": n_windows}

    daily = lp.diff().dropna().to_numpy()
    start = float(lp.iloc[0])
    rng = np.random.default_rng(seed)
    surrogate = []
    for _ in range(n_surrogates):
        shuffled = rng.permutation(daily)
        lp_s = pd.Series(np.concatenate([[start], start + np.cumsum(shuffled)]))
        xs, ys = _past_forward(lp_s, lookback, horizon)
        c = _corr(xs, ys)
        if not np.isnan(c):
            surrogate.append(c)
    surrogate = np.array(surrogate)
    p_value = (np.abs(surrogate) >= abs(observed)).mean() if len(surrogate) else np.nan

    return {
        "momentum_edge": observed,
        "momentum_p_value": p_value,
        "momentum_significant": bool(p_value < 0.05) if not np.isnan(p_value) else False,
        "momentum_n_windows": n_windows,
    }


def momentum_summary(df: pd.DataFrame, config) -> dict:
    """Config-driven per-instrument momentum report. Returns NaN /
    'insufficient_data' when there isn't enough history to estimate the
    lookback/horizon serial correlation at all -- the same hard-floor
    discipline `persistence_summary` and `candlestick_summary` apply. The
    trailing return and trend-persistence fraction are DESCRIPTIVE snapshots
    (like `regime_label`), not part of the score."""
    close = df["Close"].dropna()
    if len(close) < config.momentum_min_obs:
        return {"momentum_edge": np.nan, "momentum_significant": False,
                "momentum_p_value": np.nan, "momentum_n_windows": 0,
                "momentum_lookback_return": np.nan, "pct_days_above_trend_ma": np.nan,
                "momentum_label": "insufficient_data"}

    eff = momentum_efficacy(close, lookback=config.momentum_lookback, horizon=config.momentum_horizon,
                            n_surrogates=config.momentum_n_surrogates)

    lookback_return = close.iloc[-1] / close.iloc[-1 - config.momentum_lookback] - 1
    ma = sma(close, config.momentum_trend_ma)
    pct_above = (close > ma).mean(skipna=True)

    if not eff["momentum_significant"]:
        label = "no_momentum"
    elif eff["momentum_edge"] > 0:
        label = "momentum"       # persistent / trending: past up predicts future up
    else:
        label = "reversal"       # mean-reverting: past up predicts future down

    return {
        "momentum_edge": eff["momentum_edge"],
        "momentum_significant": eff["momentum_significant"],
        "momentum_p_value": eff["momentum_p_value"],
        "momentum_n_windows": eff["momentum_n_windows"],
        "momentum_lookback_return": lookback_return * 100,
        "pct_days_above_trend_ma": pct_above * 100,
        "momentum_label": label,
    }

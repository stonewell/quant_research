"""Combine the individual, documented metrics into a single, strategy-
agnostic instrument-selection score.

This intentionally does NOT score "fit" for grid trading / trend-following /
mean-reversion specifically -- that requires assuming which strategy you'll
run before you've even decided which instrument to trade, and no verified
formula for that combination survived this project's research pass anyway.
Instead, every component below answers a strategy-agnostic question that
matters for ANY systematic strategy: can you trade it cheaply (liquidity),
does it move enough to matter (volatility adequacy), does it show genuine
statistical structure worth exploiting at all regardless of direction
(predictability), does it actually diversify a basket (correlation to the
rest of the universe), and is there enough history to trust these numbers
and to have cleared an ETF's highest-closure-risk window (history length).
Two further components (expense ratio, AUM) are included when available,
since research found both are real determinants of ETF survival -- but they
degrade gracefully to "not counted" rather than penalizing instruments (like
individual stocks) for which they don't apply.

No single verified scoring formula survived research, so treat the ranking
this produces as a documented, transparent starting shortlist -- not a
validated predictive model.
"""

import numpy as np
import pandas as pd

# Three components now answer the SAME strategy-agnostic question -- "does this
# series show genuine, statistically significant exploitable structure at
# all?" -- via three independent, bootstrap-null-gated tests: long-memory
# Hurst (`predictability_score`), time-series-momentum serial correlation
# (`momentum_score`), and OHLC candlestick reversal edge (`candlestick_score`).
# The "structure family" keeps its original combined weight of 0.20, now split
# BY STRENGTH OF EVIDENCE: momentum gets the largest share (0.10 -- two of the
# most-replicated anomalies in finance, Jegadeesh-Titman 1993 / Moskowitz-Ooi-
# Pedersen 2012), Hurst long-memory 0.07, and candlestick the smallest (0.03 --
# Marshall-Young-Rose 2006 found no value in liquid US equities). Momentum is
# still only 0.10, not higher, because its per-asset significance is contested
# (Huang et al. 2020) and it carries left-tail crash risk (Daniel-Moskowitz
# 2016). Everything outside the family is unchanged, so the weights still sum
# to 1.0, and any missing/insignificant component degrades gracefully to ~0.
DEFAULT_WEIGHTS = {
    "liquidity_score": 0.30,
    "vol_adequacy_score": 0.20,
    "predictability_score": 0.07,
    "momentum_score": 0.10,
    "candlestick_score": 0.03,
    "diversification_score": 0.15,
    "history_adequacy_score": 0.10,
    "etf_expense_score": 0.025,
    "etf_aum_score": 0.025,
}


def _pct_rank(series: pd.Series) -> pd.Series:
    # na_option="keep" (pandas' default) excludes NaN entries from both the
    # ranking AND the denominator -- a NaN row gets a NaN rank, and every
    # OTHER row's percentile is computed only over the non-NaN population, so
    # one symbol's missing data can't silently compress everyone else's
    # percentile scores. (na_option="bottom" would count NaNs in the
    # denominator while still giving them the top rank -- wrong on both counts.)
    return series.rank(pct=True)


def _weighted_average(df: pd.DataFrame, weights: dict) -> pd.Series:
    """Weighted average of the given columns, renormalizing weights per row
    over whichever columns are non-NaN for that row -- so a stock missing
    etf_expense_score/etf_aum_score isn't penalized for simply not being an
    ETF, it just gets scored on the remaining, applicable components."""
    cols = [c for c in weights if c in df.columns]
    values = df[cols]
    w = pd.Series({c: weights[c] for c in cols})
    mask = values.notna()
    weighted_sum = (values.fillna(0) * w).sum(axis=1)
    weight_total = mask.mul(w, axis=1).sum(axis=1)
    return (weighted_sum / weight_total.replace(0, np.nan)).fillna(0)


def score_universe(metrics: pd.DataFrame, weights: dict = None,
                    min_history_years_for_full_credit: float = 4.0) -> pd.DataFrame:
    df = metrics.copy()
    weights = weights or DEFAULT_WEIGHTS

    # Liquidity: higher dollar volume is better, lower spread is better.
    df["liquidity_score"] = (
        _pct_rank(df["avg_dollar_volume"]) * 60 + (1 - _pct_rank(df["median_spread_pct"])) * 40
    )

    # Volatility adequacy: too little volatility (relative to peers) can't
    # cover costs; too much tends to come with instability. Score peaks in
    # the middle of the cross-sectional distribution, and is penalized for
    # high downside-volatility ratio (disproportionate tail risk, Ang et al. 2006).
    vol_rank = _pct_rank(df["realized_vol_annualized_pct"])
    base_vol_score = 100 * (1 - 2 * (vol_rank - 0.5).abs())
    if "downside_vol_ratio" in df.columns and df["downside_vol_ratio"].notna().any():
        downside_penalty = _pct_rank(df["downside_vol_ratio"]) * 20.0
        df["vol_adequacy_score"] = (base_vol_score - downside_penalty).clip(lower=0.0)
    else:
        df["vol_adequacy_score"] = base_vol_score

    # Predictability: does the series show genuine, statistically
    # significant structure at all (in EITHER direction), per the
    # shuffle-null significance test -- not whether it's trending vs.
    # mean-reverting, which is a strategy-choice question this score
    # deliberately stays agnostic about (see `regime_label` for that detail).
    hurst_deviation = (df["hurst"] - 0.5).abs().clip(upper=0.5)
    significance_weight = np.where(df["hurst_significant"].fillna(False), 1.0, 0.15)
    df["predictability_score"] = (hurst_deviation * 2 * 100) * significance_weight

    # Candlestick predictability: the SAME "is there exploitable structure"
    # question, tested on OHLC reversal patterns instead of long memory.
    # Rank the ABSOLUTE conditional edge (direction-agnostic, matching
    # predictability) across the universe, then gate it hard on the
    # placebo/bootstrap significance test -- an insignificant edge is
    # down-weighted to 0.15 exactly like an insignificant Hurst reading, so
    # noise-level candlestick "edges" (the expected case for most liquid
    # instruments, per Marshall-Young-Rose) score near zero rather than
    # ranking on chance. Absent (e.g. insufficient history) -> NaN, excluded.
    if "candlestick_edge" in df.columns:
        cs_significance_weight = np.where(df["candlestick_significant"].fillna(False), 1.0, 0.15)
        df["candlestick_score"] = _pct_rank(df["candlestick_edge"].abs()) * 100 * cs_significance_weight
        df.loc[df["candlestick_edge"].isna(), "candlestick_score"] = np.nan
    else:
        df["candlestick_score"] = np.nan

    # Time-series-momentum predictability: the SAME "is there exploitable
    # structure" question, tested on the serial correlation between past and
    # future returns (the statistical core of the momentum anomaly) instead of
    # long memory or OHLC geometry. Rank the ABSOLUTE edge (direction-agnostic:
    # a strong reversal tendency is as exploitable as a strong trend) and gate
    # it on the shuffle-null significance test, exactly like the other two
    # structure components -- so a series whose past returns don't reliably
    # predict its future ones (the expected case for many liquid instruments,
    # per Huang et al. 2020) scores near zero rather than ranking on chance.
    if "momentum_edge" in df.columns:
        mom_significance_weight = np.where(df["momentum_significant"].fillna(False), 1.0, 0.15)
        df["momentum_score"] = _pct_rank(df["momentum_edge"].abs()) * 100 * mom_significance_weight
        df.loc[df["momentum_edge"].isna(), "momentum_score"] = np.nan
    else:
        df["momentum_score"] = np.nan

    # Diversification: lower average correlation to the rest of the universe
    # is better for basket construction. Requires the caller to have merged
    # an `avg_correlation_to_universe` column (see run_screener.py).
    if "avg_correlation_to_universe" in df.columns:
        df["diversification_score"] = (1 - _pct_rank(df["avg_correlation_to_universe"])) * 100
    else:
        df["diversification_score"] = np.nan

    # History adequacy: more years of data both makes every statistic above
    # more reliable and, for ETFs, means the fund has cleared its
    # highest-closure-risk window. Requires a `history_years` column.
    if "history_years" in df.columns:
        df["history_adequacy_score"] = (
            (df["history_years"] / min_history_years_for_full_credit).clip(upper=1.0) * 100
        )
    else:
        df["history_adequacy_score"] = np.nan

    # ETF quality (optional, best-effort): lower expense ratio and higher
    # AUM are both documented determinants of ETF survival. NaN (e.g., for
    # plain stocks, or metadata that wasn't available) is left as NaN here
    # so `_weighted_average` excludes it rather than penalizing the symbol.
    if "expense_ratio" in df.columns:
        df["etf_expense_score"] = (1 - _pct_rank(df["expense_ratio"])) * 100
        df.loc[df["expense_ratio"].isna(), "etf_expense_score"] = np.nan
    if "total_assets" in df.columns:
        df["etf_aum_score"] = _pct_rank(df["total_assets"]) * 100
        df.loc[df["total_assets"].isna(), "etf_aum_score"] = np.nan

    df["overall_selection_score"] = _weighted_average(df, weights)
    return df

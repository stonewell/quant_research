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

from typing import Optional
import warnings

import numpy as np
import pandas as pd

from common.sector_metadata import resolve_sector, sector_neutral_demean

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

STRATEGY_FIT_DEFAULT_WEIGHTS = {
    "strategy_fit_score": 0.40,
    "liquidity_score": 0.20,
    "vol_adequacy_score": 0.125,
    "diversification_score": 0.125,
    "history_adequacy_score": 0.05,
    "predictability_score": 0.02,
    "momentum_score": 0.02,
    "candlestick_score": 0.01,
    "etf_expense_score": 0.025,
    "etf_aum_score": 0.025,
}


# Minimum number of valid members a sector group needs before cross-sectional
# demeaning is applied to it. Single source of truth: this value is used BOTH by
# the `sector_neutral_demean` call AND by the no-op warning block that recomputes
# how many groups qualified. Keeping it in one place stops the two from silently
# desynchronizing (e.g. the warning claiming "no group qualified" while the demean
# actually neutralized). Deliberately a module constant -- NOT a config field and
# NOT a CLI flag: M2.1 adds no new tunables.
SECTOR_NEUTRAL_MIN_GROUP_SIZE = 2


def _pct_rank(series: pd.Series) -> pd.Series:
    # na_option="keep" (pandas' default) excludes NaN entries from both the
    # ranking AND the denominator -- a NaN row gets a NaN rank, and every
    # OTHER row's percentile is computed only over the non-NaN population, so
    # one symbol's missing data can't silently compress everyone else's
    # percentile scores. (na_option="bottom" would count NaNs in the
    # denominator while still giving them the top rank -- wrong on both counts.)
    if series.empty:
        return series
    s = series.dropna()
    if len(s) == 0:
        return pd.Series(np.nan, index=series.index)
    if len(s) <= 1 or s.nunique() <= 1:
        res = pd.Series(0.5, index=series.index)
        res[series.isna()] = np.nan
        return res
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
                   min_history_years_for_full_credit: float = 4.0,
                   strategy_weight: float = None,
                   sector_map: Optional[dict] = None,
                   enable_sector_neutral: bool = False) -> pd.DataFrame:
    df = metrics.copy()
    if weights is None:
        if "strategy_fit_score" in df.columns:
            weights = dict(STRATEGY_FIT_DEFAULT_WEIGHTS)
            if strategy_weight is not None:
                sw = max(min(float(strategy_weight), 0.95), 0.05)
                rem = 1.0 - sw
                other_sum = sum(v for k, v in STRATEGY_FIT_DEFAULT_WEIGHTS.items() if k != "strategy_fit_score")
                weights = {"strategy_fit_score": sw}
                for k, v in STRATEGY_FIT_DEFAULT_WEIGHTS.items():
                    if k != "strategy_fit_score":
                        weights[k] = (v / other_sum) * rem
        else:
            weights = DEFAULT_WEIGHTS

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

    if enable_sector_neutral:
        # Sector resolution priority:
        # 1. Existing 'sector' column on df if present
        # 2. sector_map passed as dict, mapped over df's symbols/index
        # 3. Pure fallback resolution via resolve_sector
        sym_list = df["symbol"].astype(str).tolist() if "symbol" in df.columns else [str(x) for x in df.index]
        if "sector" in df.columns:
            sec_col = df["sector"]
            sec_list = [
                str(sec_col.iloc[i]) if pd.notna(sec_col.iloc[i]) and str(sec_col.iloc[i]).strip() != ""
                else resolve_sector(sym_list[i], sector_map, fallback="auto")
                for i in range(len(df))
            ]
        elif sector_map is not None:
            sec_list = [resolve_sector(s, sector_map, fallback="auto") for s in sym_list]
        else:
            sec_list = [resolve_sector(s, None, fallback="auto") for s in sym_list]

        sec_series = pd.Series(sec_list, index=df.index)

        # Cross-sectional sector demeaning (Task M2.1 integration layer rework):
        # Replaces sector_neutral_zscore to preserve 0-100 score magnitude.
        # Verified defects of z-score:
        # 1. overall_selection_score is a 0-100 score (weighted average of pct_rank * 100 components).
        #    Overwriting with z-score silently changes scale.
        # 2. selectorbot/selection.py:210: norm_score divides by 100 if 100.0 in scores or max > 1,
        #    causing negative z-scores to corrupt DR + score_weight * norm_score in select_max_diversification_ratio.
        # 3. selectorbot/selection.py:121-141: select_diversified_greedy combines score + diversity_weight * distance
        #    (distance is 0-2 scale); a 0-100 score dominates distance, but a z-score does not, altering basket selection.
        raw_scores = df["overall_selection_score"].copy()
        df["overall_selection_score"] = sector_neutral_demean(
            raw_scores,
            sector=sec_series,
            fallback="auto",
            min_group_size=SECTOR_NEUTRAL_MIN_GROUP_SIZE,
        )

        # np.clip to [0, 100]:
        # sector_neutral_demean deliberately avoids clipping in the core module.
        # While demeaning preserves native scale, group shifts (raw - group_mean + global_mean)
        # can push extreme assets slightly outside the documented 0-100 range.
        # All downstream selection algorithms assume [0, 100]. Clipping only bites the few
        # extreme boundaries and strictly preserves native magnitude and ranking without re-standardizing.
        df["overall_selection_score"] = np.clip(df["overall_selection_score"], 0.0, 100.0)

        # Emit warning if neutralization was a degenerate NO-OP:
        # 1. sec_series.nunique() == 1: all symbols resolved to a single sector group.
        #    Cross-sectional demeaning has no between-sector structure to remove.
        # 2. qualifying_groups == 0: multiple sector groups exist, but none has >=
        #    SECTOR_NEUTRAL_MIN_GROUP_SIZE members and non-zero spread (e.g. all singletons or zero variance).
        # In both degenerate cases, scores remain unchanged and sector exposure was NOT removed.
        # Deterministic order: single-group first, then qualifying_groups == 0, emitting exactly one warning.
        qualifying_groups = 0
        for _, group_syms in sec_series.groupby(sec_series).groups.items():
            sub_vals = raw_scores.loc[group_syms].dropna()
            if len(sub_vals) >= SECTOR_NEUTRAL_MIN_GROUP_SIZE:
                spread = float(sub_vals.max()) - float(sub_vals.min())
                if not np.isclose(spread, 0.0):
                    qualifying_groups += 1

        if sec_series.nunique() == 1:
            single_group = str(sec_series.iloc[0])
            n_symbols = len(sec_series)
            warnings.warn(
                f"Sector neutralization was requested, but all {n_symbols} symbols resolved to the "
                f"single sector group '{single_group}', so there is no between-sector structure to remove. "
                "Scores are unchanged and sector exposure was NOT adjusted. This usually means the sector "
                "map does not cover this universe (pass --sector-map-path, or note that symbols outside "
                "the map fall back to 'other').",
                UserWarning,
                stacklevel=2,
            )
        elif qualifying_groups == 0:
            warnings.warn(
                "Sector neutralization was requested, but no sector group had enough members "
                f"(>= {SECTOR_NEUTRAL_MIN_GROUP_SIZE}) or variance to neutralize. Scores are unchanged and sector "
                "exposure was NOT removed.",
                UserWarning,
                stacklevel=2,
            )

    return df

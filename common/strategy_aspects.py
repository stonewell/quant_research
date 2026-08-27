"""Selection x weighting aspect composition for basket allocation templates.

The 9 static templates in `common/allocation_templates.py` each fuse two
orthogonal decisions into one `generate_weights` method:

- SELECTION: which symbols are eligible at a rebalance date, and what
  fraction of the book should be invested at all (momentum top-N,
  dual-momentum's absolute-momentum gate, RSI-oversold top-N, breadth-gated
  de-risking; or "everyone, fully invested" for the 5 weighting-only
  templates).
- WEIGHTING: how the invested fraction splits across the eligible symbols
  (equal-split, inverse-vol, HRP, min-variance, vol/correlation "max
  diversification").

This module reimplements each side as a standalone, independently
composable `SelectionAspect`/`WeightingAspect`, built from the exact same
shared primitives the 9 template classes already import
(`common.indicators`, `common.allocation_templates`'s private weighting
helpers, `common.covariance`, `common.scheduling`) -- so a
`CompositeAllocationTemplate` can pair, say, momentum's stock-picking with
inverse-volatility's position sizing, a combination that doesn't exist as
any single template today.

Deliberately NOT a refactor of the 9 existing classes (zero regression risk
to already-tested code): this is new, parallel logic. `dual_momentum_topn`
below is a disclosed simplification of `DualMomentumAllocation`'s own
selection step -- it drops a failed absolute-momentum top-N slot instead of
reserving it empty -- since this is a new composable variant, not a
byte-for-byte reconstruction of that atomic template.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List

import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _hrp_portfolio,
    _inverse_vol_weights,
    _min_variance_weights,
)
from common.covariance import denoise_correlation, denoise_covariance
from common.indicators import realized_vol, roc, rsi
from common.scheduling import get_rebalance_dates as _get_rebalance_dates


@dataclass
class SelectionResult:
    """`eligible`: bool DataFrame, index=rebalance_dates, columns=symbols.
    `invested_fraction`: float Series indexed by rebalance date (0.0-1.0),
    the TOTAL fraction of the book the weighting aspect should distribute
    across that date's eligible symbols -- defaults to 1.0 (fully invested)
    for every selection aspect except `breadth_gated_topn`."""
    eligible: pd.DataFrame
    invested_fraction: pd.Series


@dataclass
class SelectionAspect:
    key: str
    param_grid: dict
    factor_tags: list
    select_fn: Callable
    warmup_fn: Callable
    describe_fn: Callable

    def select(self, universe, params, master_index, rebalance_dates) -> SelectionResult:
        return self.select_fn(universe, params, master_index, rebalance_dates)

    def warmup_bars(self, params: dict) -> int:
        return self.warmup_fn(params)

    def describe(self, params: dict) -> str:
        return self.describe_fn(params)


@dataclass
class WeightingAspect:
    key: str
    param_grid: dict
    factor_tags: list
    weight_fn: Callable
    warmup_fn: Callable
    describe_fn: Callable

    def weight(self, universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
        return self.weight_fn(universe, selection, params, master_index, rebalance_dates)

    def warmup_bars(self, params: dict) -> int:
        return self.warmup_fn(params)

    def describe(self, params: dict) -> str:
        return self.describe_fn(params)


def _chosen_symbols(selection: SelectionResult, date) -> list:
    row = selection.eligible.loc[date]
    return row[row].index.tolist()


# --------------------------------------------------------------------------
# Selection aspects
# --------------------------------------------------------------------------

def _select_all(universe, params, master_index, rebalance_dates) -> SelectionResult:
    symbols = list(universe.keys())
    eligible = pd.DataFrame(True, index=rebalance_dates, columns=symbols)
    invested_fraction = pd.Series(1.0, index=rebalance_dates)
    return SelectionResult(eligible=eligible, invested_fraction=invested_fraction)


def _select_momentum_topn(universe, params, master_index, rebalance_dates) -> SelectionResult:
    symbols = list(universe.keys())
    moms = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        moms[sym] = roc(df["Close"], period=params["mom_lookback"])
    moms_rebal = moms.loc[rebalance_dates]

    n_symbols = len(symbols)
    top_n = max(1, int(n_symbols * params["top_n_fraction"]))
    eligible = pd.DataFrame(False, index=rebalance_dates, columns=symbols)
    for date, row in moms_rebal.iterrows():
        if row.isna().all():
            continue
        eligible.loc[date, row.nlargest(top_n).index] = True

    return SelectionResult(eligible=eligible, invested_fraction=pd.Series(1.0, index=rebalance_dates))


def _select_dual_momentum_topn(universe, params, master_index, rebalance_dates) -> SelectionResult:
    symbols = list(universe.keys())
    moms = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        moms[sym] = roc(df["Close"], period=params["mom_lookback"])
    moms_rebal = moms.loc[rebalance_dates]

    n_symbols = len(symbols)
    top_n = max(1, int(n_symbols * params["top_n_fraction"]))
    eligible = pd.DataFrame(False, index=rebalance_dates, columns=symbols)
    for date, row in moms_rebal.iterrows():
        if row.isna().all():
            continue
        top_symbols = row.nlargest(top_n).index
        passing = [s for s in top_symbols if row[s] > 0.0]
        eligible.loc[date, passing] = True

    return SelectionResult(eligible=eligible, invested_fraction=pd.Series(1.0, index=rebalance_dates))


def _select_mean_reversion_topn(universe, params, master_index, rebalance_dates) -> SelectionResult:
    symbols = list(universe.keys())
    rsis = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        rsis[sym] = rsi(df["Close"], period=params["rsi_period"])
    rsis_rebal = rsis.loc[rebalance_dates]

    n_symbols = len(symbols)
    top_n = max(1, int(n_symbols * params["top_n_fraction"]))
    eligible = pd.DataFrame(False, index=rebalance_dates, columns=symbols)
    for date, row in rsis_rebal.iterrows():
        if row.isna().all():
            continue
        # LOWEST RSI = most oversold (Connors-style RSI(2) mean-reversion).
        eligible.loc[date, row.nsmallest(top_n).index] = True

    return SelectionResult(eligible=eligible, invested_fraction=pd.Series(1.0, index=rebalance_dates))


def _select_breadth_gated_topn(universe, params, master_index, rebalance_dates) -> SelectionResult:
    symbols = list(universe.keys())
    moms = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        moms[sym] = roc(df["Close"], period=params["mom_lookback"])
    moms_rebal = moms.loc[rebalance_dates]

    n_symbols = len(symbols)
    top_n = max(1, int(n_symbols * params["top_n_fraction"]))
    n1 = params["protection_factor"] * n_symbols / 4.0

    eligible = pd.DataFrame(False, index=rebalance_dates, columns=symbols)
    invested_fraction = pd.Series(0.0, index=rebalance_dates)

    for date, row in moms_rebal.iterrows():
        row = row.dropna()
        if len(row) < n_symbols:
            # Whole-basket warmup gate, matching BreadthGatedMomentumAllocation:
            # breadth's denominator N must be the full basket.
            continue

        n_positive = int((row > 0).sum())
        denom = n_symbols - n1
        if n_positive <= n1:
            derisked_fraction = 1.0
        elif denom > 0:
            derisked_fraction = max(0.0, (n_symbols - n_positive) / denom)
        else:
            derisked_fraction = 0.0
        invested_fraction.loc[date] = 1.0 - derisked_fraction
        eligible.loc[date, row.nlargest(top_n).index] = True

    return SelectionResult(eligible=eligible, invested_fraction=invested_fraction)


SELECTION_ASPECTS: Dict[str, SelectionAspect] = {
    "all_symbols": SelectionAspect(
        key="all_symbols", param_grid={}, factor_tags=[],
        select_fn=_select_all, warmup_fn=lambda params: 0,
        describe_fn=lambda params: "the full basket (no selection filter)",
    ),
    "momentum_topn": SelectionAspect(
        key="momentum_topn",
        param_grid={"mom_lookback": [63, 126, 252], "top_n_fraction": [0.25, 0.5]},
        factor_tags=["relative_momentum"],
        select_fn=_select_momentum_topn,
        warmup_fn=lambda params: params["mom_lookback"],
        describe_fn=lambda params: (
            f"the top {int(params['top_n_fraction'] * 100)}% of the basket by "
            f"{params['mom_lookback']}-day momentum"
        ),
    ),
    "dual_momentum_topn": SelectionAspect(
        key="dual_momentum_topn",
        param_grid={"mom_lookback": [63, 126, 252], "top_n_fraction": [0.25, 0.5]},
        factor_tags=["absolute_momentum_trend", "relative_momentum"],
        select_fn=_select_dual_momentum_topn,
        warmup_fn=lambda params: params["mom_lookback"],
        describe_fn=lambda params: (
            f"the top {int(params['top_n_fraction'] * 100)}% of the basket by "
            f"{params['mom_lookback']}-day momentum, excluding any with non-positive momentum"
        ),
    ),
    "mean_reversion_topn": SelectionAspect(
        key="mean_reversion_topn",
        param_grid={"rsi_period": [2, 5, 14], "top_n_fraction": [0.25, 0.5]},
        factor_tags=["mean_reversion"],
        select_fn=_select_mean_reversion_topn,
        warmup_fn=lambda params: params["rsi_period"],
        describe_fn=lambda params: (
            f"the most-oversold {int(params['top_n_fraction'] * 100)}% of the basket by "
            f"{params['rsi_period']}-period RSI"
        ),
    ),
    "breadth_gated_topn": SelectionAspect(
        key="breadth_gated_topn",
        param_grid={
            "mom_lookback": [63, 126, 252], "top_n_fraction": [0.25, 0.5], "protection_factor": [1, 2],
        },
        factor_tags=["breadth", "relative_momentum"],
        select_fn=_select_breadth_gated_topn,
        warmup_fn=lambda params: params["mom_lookback"],
        describe_fn=lambda params: (
            f"the top {int(params['top_n_fraction'] * 100)}% of the basket by "
            f"{params['mom_lookback']}-day momentum, with total exposure scaled by market breadth "
            f"(protection_factor={params['protection_factor']})"
        ),
    ),
}


# --------------------------------------------------------------------------
# Weighting aspects
# --------------------------------------------------------------------------

def _weight_equal(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    weights_rebal = pd.DataFrame(0.0, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        if chosen and inv_frac > 0:
            weights_rebal.loc[date, chosen] = inv_frac / len(chosen)

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df


def _weight_inverse_vol(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    vol_lookback = params["vol_lookback"]
    vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        vols[sym] = realized_vol(df["Close"], window=vol_lookback)
    vols_rebal = vols.loc[rebalance_dates]

    # NaN-initialized (not 0.0) to match InverseVolatilityAllocation's own
    # convention: a symbol EXCLUDED BY SELECTION (not chosen this date) gets
    # an explicit 0.0 below so the backtester's column-wise ffill actually
    # closes out any previous position in it, but a CHOSEN symbol with
    # invalid/zero volatility stays NaN (on_invalid="nan", matching the
    # atomic template's own default) rather than being coerced to 0.0.
    weights_rebal = pd.DataFrame(np.nan, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        not_chosen = [s for s in symbols if s not in chosen]
        if not_chosen:
            weights_rebal.loc[date, not_chosen] = 0.0
        if not chosen:
            continue
        if inv_frac <= 0:
            # A fully de-risked date (only breadth_gated_topn ever produces
            # this) is a definite, known decision, NOT a data-availability
            # gap -- these symbols must be explicitly zeroed too, or they'd
            # stay NaN and ffill a stale prior weight straight through the
            # de-risk event instead of actually closing the position.
            weights_rebal.loc[date, chosen] = 0.0
            continue
        w = _inverse_vol_weights(vols_rebal.loc[date, chosen], scale=inv_frac, on_invalid="nan")
        weights_rebal.loc[date, chosen] = w.values

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df


def _weight_hrp(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    lookback = params["cov_lookback"]
    returns_df = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        returns_df[sym] = df["Close"].pct_change()

    # NaN-initialized to match HierarchicalRiskParityAllocation's own
    # convention: a symbol lacking a full lookback return history is left
    # NaN (not coerced to 0.0), same as the atomic template. A symbol
    # EXCLUDED BY SELECTION gets an explicit 0.0 so the backtester's
    # column-wise ffill actually closes out any previous position in it.
    weights_rebal = pd.DataFrame(np.nan, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        not_chosen = [s for s in symbols if s not in chosen]
        if not_chosen:
            weights_rebal.loc[date, not_chosen] = 0.0
        if not chosen:
            continue
        if inv_frac <= 0:
            # See _weight_inverse_vol: a fully de-risked date is a definite
            # decision, not a data gap -- these symbols must be explicitly
            # zeroed too, not left NaN to ffill a stale prior weight.
            weights_rebal.loc[date, chosen] = 0.0
            continue
        loc = master_index.get_loc(date)
        if loc < lookback:
            continue
        sub_ret = returns_df.iloc[loc - lookback:loc]
        valid = [s for s in chosen if sub_ret[s].notna().all()]
        if not valid:
            continue
        cov = denoise_covariance(sub_ret[valid].cov().to_numpy(), n_obs=len(sub_ret))
        weights_rebal.loc[date, valid] = _hrp_portfolio(cov) * inv_frac

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df


def _weight_min_variance(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    lookback = params["cov_lookback"]
    returns_df = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        returns_df[sym] = df["Close"].pct_change()

    # See _weight_hrp for the NaN-vs-0.0 convention this mirrors.
    weights_rebal = pd.DataFrame(np.nan, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        not_chosen = [s for s in symbols if s not in chosen]
        if not_chosen:
            weights_rebal.loc[date, not_chosen] = 0.0
        if not chosen:
            continue
        if inv_frac <= 0:
            # See _weight_inverse_vol: a fully de-risked date is a definite
            # decision, not a data gap -- these symbols must be explicitly
            # zeroed too, not left NaN to ffill a stale prior weight.
            weights_rebal.loc[date, chosen] = 0.0
            continue
        loc = master_index.get_loc(date)
        if loc < lookback:
            continue
        sub_ret = returns_df.iloc[loc - lookback:loc]
        valid = [s for s in chosen if sub_ret[s].notna().all()]
        if not valid:
            continue
        cov = denoise_covariance(sub_ret[valid].cov().to_numpy(), n_obs=len(sub_ret))
        weights_rebal.loc[date, valid] = _min_variance_weights(cov) * inv_frac

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df


def _weight_max_diversification(universe, selection: SelectionResult, params, master_index, rebalance_dates) -> pd.DataFrame:
    symbols = list(universe.keys())
    lookback = params["vol_lookback"]
    returns_df = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
    for sym, df in universe.items():
        returns_df[sym] = df["Close"].pct_change()

    # See _weight_hrp for the NaN-vs-0.0 convention this mirrors.
    weights_rebal = pd.DataFrame(np.nan, index=rebalance_dates, columns=symbols)
    for date in rebalance_dates:
        inv_frac = selection.invested_fraction.loc[date]
        chosen = _chosen_symbols(selection, date)
        not_chosen = [s for s in symbols if s not in chosen]
        if not_chosen:
            weights_rebal.loc[date, not_chosen] = 0.0
        if not chosen:
            continue
        if inv_frac <= 0:
            # See _weight_inverse_vol: a fully de-risked date is a definite
            # decision, not a data gap -- these symbols must be explicitly
            # zeroed too, not left NaN to ffill a stale prior weight.
            weights_rebal.loc[date, chosen] = 0.0
            continue
        loc = master_index.get_loc(date)
        if loc < lookback:
            continue
        sub_ret = returns_df.iloc[loc - lookback:loc]
        valid = [s for s in chosen if sub_ret[s].notna().all()]
        if not valid:
            continue
        sub_ret = sub_ret[valid]
        vols = sub_ret.std() * np.sqrt(252)
        corr = denoise_correlation(sub_ret.corr().fillna(0), n_obs=len(sub_ret))
        n = len(valid)
        avg_corr = (corr.sum(axis=1) - 1.0) / max(n - 1, 1)
        denom = (1.0 + avg_corr.clip(lower=0.0)).replace(0, np.nan)
        raw_w = vols / denom
        sum_w = raw_w.sum()
        if sum_w > 0:
            weights_rebal.loc[date, valid] = (raw_w / sum_w * inv_frac).values

    weights_df = pd.DataFrame(np.nan, index=master_index, columns=symbols)
    weights_df.loc[rebalance_dates] = weights_rebal
    return weights_df


WEIGHTING_ASPECTS: Dict[str, WeightingAspect] = {
    "equal_weight": WeightingAspect(
        key="equal_weight", param_grid={}, factor_tags=["static_fixed_weight"],
        weight_fn=_weight_equal, warmup_fn=lambda params: 0,
        describe_fn=lambda params: "an equal split across selected symbols",
    ),
    "inverse_vol": WeightingAspect(
        key="inverse_vol",
        param_grid={"vol_lookback": [20, 60, 120]},
        factor_tags=["volatility_targeting"],
        weight_fn=_weight_inverse_vol,
        warmup_fn=lambda params: params["vol_lookback"],
        describe_fn=lambda params: f"inverse-volatility weighting ({params['vol_lookback']}-day realized vol)",
    ),
    "hrp": WeightingAspect(
        key="hrp",
        param_grid={"cov_lookback": [60, 126, 252]},
        factor_tags=["correlation_diversification"],
        weight_fn=_weight_hrp,
        warmup_fn=lambda params: params["cov_lookback"],
        describe_fn=lambda params: f"Hierarchical Risk Parity weighting ({params['cov_lookback']}-day covariance)",
    ),
    "min_variance": WeightingAspect(
        key="min_variance",
        param_grid={"cov_lookback": [60, 126, 252]},
        factor_tags=["correlation_diversification"],
        weight_fn=_weight_min_variance,
        warmup_fn=lambda params: params["cov_lookback"],
        describe_fn=lambda params: f"minimum-variance weighting ({params['cov_lookback']}-day covariance)",
    ),
    "max_diversification": WeightingAspect(
        key="max_diversification",
        param_grid={"vol_lookback": [60, 126]},
        factor_tags=["volatility_targeting", "correlation_diversification"],
        weight_fn=_weight_max_diversification,
        warmup_fn=lambda params: params["vol_lookback"],
        describe_fn=lambda params: f"maximum-diversification weighting ({params['vol_lookback']}-day window)",
    ),
}


# Maps each of the 9 static ALLOCATION_TEMPLATES' own `.name` to the
# (selection_key, weighting_key) pair that reproduces its logic -- used by
# `build_composite_candidates` to know which templates are decomposable and
# to avoid rebuilding a composite that's identical to one already searched.
ATOMIC_TEMPLATE_ASPECTS = {
    "equal_weight": ("all_symbols", "equal_weight"),
    "inverse_volatility": ("all_symbols", "inverse_vol"),
    "cross_sectional_momentum": ("momentum_topn", "equal_weight"),
    "hierarchical_risk_parity": ("all_symbols", "hrp"),
    "dual_momentum": ("dual_momentum_topn", "equal_weight"),
    "max_diversification": ("all_symbols", "max_diversification"),
    "mean_reversion": ("mean_reversion_topn", "equal_weight"),
    "minimum_variance": ("all_symbols", "min_variance"),
    "breadth_gated_momentum": ("breadth_gated_topn", "equal_weight"),
}


class CompositeAllocationTemplate(AllocationTemplate):
    """A basket allocation template built by pairing one `SelectionAspect`
    with one `WeightingAspect` from a DIFFERENT source template -- e.g.
    momentum's stock-picking (`momentum_topn`) with inverse-volatility's
    position sizing (`inverse_vol`), a combination that isn't any single
    template in `common/allocation_templates.py`. See
    `build_composite_candidates` for how/when these are constructed."""

    def __init__(self, selection: SelectionAspect, weighting: WeightingAspect, default_params: dict = None):
        """`default_params`, if given, backstops any key `generate_weights`
        needs but a caller's own `params` omits -- notably
        `rebalance_freq_days`, which belongs to NEITHER aspect's own
        `param_grid` (it's synthesized once at composition time from the
        selection side's best-found value, see `build_composite_candidates`)
        so a caller re-running `common.allocation_search.grid_search_template`
        on just `self.param_grid` (e.g. `backtester --optimize`) would
        otherwise KeyError. Not required for the normal path (generator.py's
        composition step and `StrategyGenerator.generate()` always pass a
        complete, already-merged `params` dict)."""
        self.selection = selection
        self.weighting = weighting
        self.default_params = default_params or {}
        param_grid = {**selection.param_grid, **weighting.param_grid}
        factor_tags = list(dict.fromkeys(selection.factor_tags + weighting.factor_tags))
        super().__init__(
            name=f"{selection.key}__{weighting.key}",
            param_grid=param_grid,
            factor_tags=factor_tags,
        )

    def generate_weights(self, universe, params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()
        params = {**self.default_params, **(params or {})}
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])
        selection_result = self.selection.select(universe, params, master_index, rebalance_dates)
        return self.weighting.weight(universe, selection_result, params, master_index, rebalance_dates)

    def explain_weights(self, params: dict) -> str:
        params = {**self.default_params, **(params or {})}
        return (
            f"Composite Strategy ({self.name}): rebalances every {params['rebalance_freq_days']} trading days. "
            f"Selects {self.selection.describe(params)}, then allocates via {self.weighting.describe(params)}. "
            f"This pairing is NOT one of this workspace's 9 static allocation templates -- it was assembled by "
            f"strategy_generator's aspect-composition search from two different templates' own logic."
        )

    def warmup_bars(self, params: dict) -> int:
        params = {**self.default_params, **(params or {})}
        return max(self.selection.warmup_bars(params), self.weighting.warmup_bars(params))


def build_composite_candidates(best_per_template: dict, top_k: int = 4) -> list:
    """Given `_search_allocation`'s `best_per_template` (template name ->
    its best {template, params, res, score} result from the atomic +
    extra_templates grid search), returns a list of
    `(CompositeAllocationTemplate, merged_params)` pairs worth evaluating:
    the cross product of the top-`top_k` decomposable templates' own
    selection/weighting keys, excluding any pairing already present among
    those top-k templates (which would just reconstruct one of them).

    Each returned template comes with a single ready-to-score `params`
    dict (that selection aspect's own best-found params, overlaid with that
    weighting aspect's own best-found params, so `rebalance_freq_days`
    -- shared by both -- resolves to the SELECTION side's best value) --
    callers should score it once via their existing `score_fn`, not run a
    fresh grid search over it.
    """
    decomposable = [
        (name, ATOMIC_TEMPLATE_ASPECTS[name], result)
        for name, result in best_per_template.items()
        if name in ATOMIC_TEMPLATE_ASPECTS
    ]
    if len(decomposable) < 2:
        return []

    decomposable.sort(key=lambda t: t[2]["score"], reverse=True)
    top = decomposable[:top_k]

    existing_pairs = {aspects for _, aspects, _ in top}
    sel_params, wt_params = {}, {}
    for _, (sel_key, wt_key), result in top:
        sel_params.setdefault(sel_key, result["params"])
        wt_params.setdefault(wt_key, result["params"])

    candidates = []
    for sel_key, sel_best_params in sel_params.items():
        for wt_key, wt_best_params in wt_params.items():
            if (sel_key, wt_key) in existing_pairs:
                continue
            merged_params = {**wt_best_params, **sel_best_params}
            template = CompositeAllocationTemplate(
                SELECTION_ASPECTS[sel_key], WEIGHTING_ASPECTS[wt_key], default_params=merged_params,
            )
            candidates.append((template, merged_params))

    return candidates

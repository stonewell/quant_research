"""Shared grid-search + Equivalent Random Search (ERS) validation primitives.

Originally lived only inside `strategy_generator/stratgen/generator.py`,
entangled with that project's multi-template comparison/factor-tiebreak
logic. This module extracts the genuinely template-agnostic pieces so
`backtester` can tune a SINGLE, already-chosen template's parameters (its
`--optimize` flag) using the exact same validated mechanism, instead of a
second, independently-maintained copy.

Key design point: the ERS check historically runs ONCE, on whatever
(template, params) a caller has already decided is the winner -- never once
per candidate template. That's why this module exposes two independently
callable pieces instead of one bundled function:

- `grid_search_template`: per-template grid search, zero cross-template
  knowledge, safe to call once per template in a multi-template search
  (`strategy_generator`'s use) without changing behavior.
- `run_ers_validation`: ERS-only, called exactly once on the already-decided
  winner.
- `optimize_template`: a thin convenience wrapper composing both, for the
  single-template case -- what `backtester --optimize` calls directly.

Every function here takes a `score_fn(template, params) -> dict` callback
(a result dict with at least a `"sharpe_ratio"` key) instead of hardcoding
how to backtest a candidate -- `strategy_generator` supplies a single-shot
`run_allocation_backtest`-based scorer, `backtester` supplies
`run_standard`/`run_walkforward`-based ones.
"""

import itertools
import warnings
from typing import Callable, Optional

import numpy as np
import pandas as pd

ScoreFn = Callable[[object, dict], dict]


def grid_combinations(param_grid: dict) -> list:
    """Cartesian product of a `{param_name: [values]}` dict into a list of
    param dicts. An empty/falsy `param_grid` returns `[{}]` (one degenerate
    trial), never an empty list."""
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]


def random_weights(universe: dict, rebalance_freq_days: int, rng: np.random.Generator) -> pd.DataFrame:
    """Generates a random valid weight matrix (sums to 1.0 on rebalance days).
    Sparse (NaN off rebalance dates), matching the contract every
    AllocationTemplate.generate_weights must follow -- see
    common/allocation_templates.py."""
    symbols = list(universe.keys())
    if not symbols:
        return pd.DataFrame()

    master_index = universe[symbols[0]].index
    rebalance_dates = master_index[::rebalance_freq_days]

    n_dates = len(rebalance_dates)
    n_symbols = len(symbols)

    # Generate random weights (Dirichlet distribution equivalent)
    raw_w = rng.exponential(scale=1.0, size=(n_dates, n_symbols))
    norm_w = raw_w / raw_w.sum(axis=1, keepdims=True)

    weights_rebal = pd.DataFrame(norm_w, index=rebalance_dates, columns=symbols)

    weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
    weights_df.loc[rebalance_dates] = weights_rebal

    return weights_df


class RandomAllocationTemplate:
    """A dummy template used purely for the ERS check."""

    def __init__(self, rng: np.random.Generator):
        self.rng = rng

    def generate_weights(self, universe: dict, params: dict) -> pd.DataFrame:
        return random_weights(universe, params["rebalance_freq_days"], self.rng)

    def warmup_bars(self, params: dict) -> int:
        """Random weights have no indicator to warm up -- 0, matching
        `common.allocation_templates.AllocationTemplate`'s own default.
        Required so `backtester.run_walkforward`'s fold-warmup buffering
        (`template.warmup_bars(params)`, called unconditionally) doesn't
        raise `AttributeError` on every ERS random-portfolio draw during
        `--optimize --mode walkforward` -- previously latent since
        `strategy_generator`'s single-shot scoring never called this
        method, only `run_walkforward` does."""
        return 0


def _safe_score(score_fn: ScoreFn, template, params: dict) -> dict:
    """Calls `score_fn(template, params)`; ANY exception is caught and
    converted to the `-inf` fallback dict + a `RuntimeWarning` naming the
    exception, so a caller's `score_fn` never needs to guard itself against
    a bad candidate crashing the whole search."""
    try:
        return score_fn(template, params)
    except Exception as exc:
        warnings.warn(
            f"allocation_search: scoring {template!r} with params={params!r} raised "
            f"{type(exc).__name__}: {exc} -- treating this candidate/trial as -inf Sharpe "
            f"and continuing.",
            category=RuntimeWarning,
        )
        return {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}


def grid_search_template(template, score_fn: ScoreFn) -> list:
    """Evaluates `score_fn` over every combination in `template.param_grid`.
    Returns `[{"params", "result", "score"}, ...]`, one entry per
    combination (`grid_combinations({})` -> `[{}]`, so a degenerate/empty
    `param_grid` still yields exactly one trial, never zero). Zero
    cross-template dependency -- safe to call once per template in a
    multi-template search without altering behavior."""
    trials = []
    for params in grid_combinations(template.param_grid):
        result = _safe_score(score_fn, template, params)
        trials.append({
            "params": params,
            "result": result,
            "score": result.get("sharpe_ratio", float("-inf")),
        })
    return trials


def run_ers_validation(params: dict, best_score: float, best_result: dict, score_fn: ScoreFn, *,
                        n_random_search: int = 200, ers_percentile_threshold: float = 0.90,
                        min_rebalances_for_trust: int = 4, seed: Optional[int] = None) -> dict:
    """Equivalent Random Search: draws `n_random_search` random-weight
    portfolios (`RandomAllocationTemplate`) at
    `params.get("rebalance_freq_days", 21)`, scores each through `score_fn`,
    and computes `best_score`'s percentile rank against the survivors. Call
    this ONCE per search (on whatever `(params, best_score, best_result)` a
    caller has already picked as the winner) -- never once per candidate
    template, or `n_random_search` draws are burned per template instead of
    once overall.

    Returns `{"ers_percentile", "ers_passed", "trusted"}`. `ers_percentile`
    defaults to 0.0 (fail-safe -- this candidate has NOT been shown to beat
    anything), never a trivial 1.0, when every random trial is
    non-finite/filtered out.
    """
    rng = np.random.default_rng(seed)
    random_template = RandomAllocationTemplate(rng)
    winning_freq = params.get("rebalance_freq_days", 21)

    random_scores = []
    for _ in range(n_random_search):
        res = _safe_score(score_fn, random_template, {"rebalance_freq_days": winning_freq})
        s = res.get("sharpe_ratio", float("-inf"))
        if np.isfinite(s):
            random_scores.append(s)

    ers_percentile = float((np.array(random_scores) < best_score).mean()) if random_scores else 0.0
    ers_passed = ers_percentile >= ers_percentile_threshold
    trusted = ers_passed and best_result.get("total_rebalances", 0) >= min_rebalances_for_trust
    return {"ers_percentile": ers_percentile, "ers_passed": ers_passed, "trusted": trusted}


def optimize_template(universe: dict, template, score_fn: ScoreFn, *,
                       n_random_search: int = 200, ers_percentile_threshold: float = 0.90,
                       min_rebalances_for_trust: int = 4, seed: Optional[int] = None) -> dict:
    """Single-template grid-search + ERS validation (`grid_search_template`
    then `run_ers_validation` on its winner). For a MULTI-template search, do
    NOT call this once per template -- call `grid_search_template` per
    template yourself, reduce to one winner, then call `run_ers_validation`
    ONCE on that winner (see `strategy_generator/stratgen/generator.py`'s
    `_search_allocation`).

    Returns `{"best_params", "best_result", "best_score", "ers_percentile",
    "ers_passed", "trusted", "n_trials", "all_trials"}`. `all_trials` is the
    full `grid_search_template()` trace, for diagnostics/reporting.
    """
    if not universe:
        raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

    trials = grid_search_template(template, score_fn)
    best = max(trials, key=lambda t: t["score"])
    ers = run_ers_validation(
        best["params"], best["score"], best["result"], score_fn,
        n_random_search=n_random_search, ers_percentile_threshold=ers_percentile_threshold,
        min_rebalances_for_trust=min_rebalances_for_trust, seed=seed,
    )
    return {
        "best_params": best["params"],
        "best_result": best["result"],
        "best_score": best["score"],
        **ers,
        "n_trials": len(trials) + n_random_search,
        "all_trials": trials,
    }

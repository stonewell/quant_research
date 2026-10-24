"""Shared grid-search + Equivalent Random Search (ERS) validation primitives.

Originally lived only inside `pipeline/strategy_generator/stratgen/generator.py`,
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

Task B2.4 adds an OPT-IN `n_jobs` knob to all three entry points: the
per-candidate (grid) and per-draw (ERS) evaluations can be fanned out over a
forked process pool. It changes only *where* an item is evaluated, never how
many items exist -- the one-ERS-per-search rule above still holds.
"""

import contextlib
import itertools
import multiprocessing as mp
import numbers
import os
import warnings
from typing import Callable, Optional

import numpy as np
import pandas as pd

from common.allocation_backtester import minimal_metrics_scope
from common.allocation_templates import AllocationTemplate
from common.scheduling import get_rebalance_dates

ScoreFn = Callable[[object, dict], dict]

# ---------------------------------------------------------------------------
# B2.4: process-pool fan-out for grid search + ERS sampling.
#
# Chunking / data-transfer strategy (deliberate, see also `_run_chunked`):
# the pool is created with the **fork** start method only, and the per-call
# payload (`template`, the caller's `score_fn` closure -- which transitively
# owns the whole OHLCV `universe` dict -- plus the pre-derived `SeedSequence`
# list) is published into `_PARALLEL_STATE` *before* the pool is forked.
# Children inherit it copy-on-write; what actually crosses the task pipe is
# only a tiny `(start, stop)` index pair, and what crosses back is a small
# list of per-item results. So the universe DataFrames are serialised ZERO
# times, not once per worker -- this is the whole reason fork (not spawn) is
# required for the parallel path, and why a non-picklable `score_fn` (a
# lambda, a closure over a lock, ...) still works here.
#
# `spawn`/`forkserver` platforms (Windows) and sandboxed containers that
# forbid fork therefore degrade to the serial path with a RuntimeWarning
# instead of crashing -- see `_run_chunked`.
_PARALLEL_STATE: dict = {}

# Below this many work items the pool's own fork+teardown overhead dominates
# the evaluation it would parallelise (a single small backtest is ~1-10 ms,
# a fork+pool cycle ~30-80 ms), so such calls stay in-process.
_MIN_ITEMS_FOR_POOL = 8

# 4 chunks per worker so that workers that draw cheap candidates can pick up
# the slack left by expensive ones (grid candidates differ hugely in cost --
# a 5-day rebalance backtest is not a 252-day one), while `pool.map`'s
# ordered gathering still returns chunks -- and therefore results -- in
# candidate order.
_CHUNKS_PER_WORKER = 4


def resolve_n_jobs(n_jobs: Optional[int], n_items: int) -> int:
    """Normalises the public `n_jobs` argument against the amount of work.

    Semantics (identical for `grid_search_template`, `run_ers_validation` and
    `optimize_template`):

    - `None` or `-1`: auto -- `min(os.cpu_count(), n_items)`, i.e. one worker
      per CPU but never more workers than there are work items. This must be
      requested EXPLICITLY: the API default is `1`.
    - `1` (the default): strictly serial, in-process, no pool is ever
      created -- byte-identical to the pre-B2.4 code path.
    - `N > 1`: at most `N` worker processes (further capped by `n_items`).
    - `0` or `N < -1`: `ValueError` (ambiguous / meaningless).
    - `True`/`False` or a non-integer: `TypeError` (`bool` is an `int`
      subclass, so it is rejected explicitly rather than silently treated as
      1/0).

    WHY serial-by-default (deliberate, disclosed): several existing suites
    assert parent-process side effects of the scoring callback (e.g.
    `mock.calls` counts in `backtester/tests/test_run_backtest.py` and
    `pipeline/strategy_generator/tests/test_generator.py`). Those callbacks
    run in worker processes once fan-out is on, so a CPU-adaptive DEFAULT
    would silently break them. Callers that want the speedup pass
    `n_jobs=-1`/`None` explicitly; `backtester --optimize` and
    `strategy_generator` can be wired to a CLI `--n-jobs` (files owned by the
    B2.2/B2.3 work) without any further change here.
    """
    if isinstance(n_jobs, bool) or (n_jobs is not None and not isinstance(n_jobs, numbers.Integral)):
        raise TypeError(f"n_jobs must be an int or None, got {type(n_jobs).__name__}: {n_jobs!r}")
    max_useful = max(1, int(n_items))
    if n_jobs is None or int(n_jobs) == -1:
        return max(1, min(os.cpu_count() or 1, max_useful))
    n_jobs = int(n_jobs)
    if n_jobs == 0 or n_jobs < -1:
        raise ValueError(f"n_jobs must be -1 (auto), None (auto) or a positive int, got {n_jobs}")
    return max(1, min(n_jobs, max_useful))


def _chunk_bounds(n_items: int, n_chunks: int) -> list:
    """Contiguous, balanced `(start, stop)` slices covering `range(n_items)`.

    Contiguous (not strided) so that a chunk is one `SeedSequence`-derived
    run of consecutive ERS draws / grid combinations, and so results come
    back in the caller's original order after ordered gathering.
    """
    if n_items <= 0:
        return []
    n_chunks = max(1, min(int(n_chunks), n_items))
    base, rem = divmod(n_items, n_chunks)
    bounds, start = [], 0
    for i in range(n_chunks):
        stop = start + base + (1 if i < rem else 0)
        bounds.append((start, stop))
        start = stop
    return bounds


def _fork_context():
    """The `fork` multiprocessing context, or `None` if this platform /
    environment cannot provide it (Windows, some sandboxes)."""
    try:
        return mp.get_context("fork")
    except (ValueError, AttributeError, OSError):
        return None


@contextlib.contextmanager
def _worker_payload(**payload):
    """Publishes the fork-inherited worker payload for the duration of a call.

    Save/restore (rather than a lock) so a nested search invoked from inside
    a score function cannot lose the outer state. Same-process *concurrent*
    calls from multiple threads are not supported -- they would race on this
    module-level dict; every existing caller (`strategy_generator`,
    `backtester --optimize`) is single-threaded.
    """
    saved = dict(_PARALLEL_STATE)
    _PARALLEL_STATE.clear()
    _PARALLEL_STATE.update(payload)
    try:
        yield
    finally:
        _PARALLEL_STATE.clear()
        _PARALLEL_STATE.update(saved)


def _reemit_warnings(captured) -> None:
    """Re-raises warnings that were recorded inside a worker (or inside the
    in-process chunk function) in the CALLING process, so a caller's
    `warnings.catch_warnings`/`pytest.warns` still sees one warning per
    failed candidate with the same category and message as a serial run."""
    for category, message in captured:
        warnings.warn(message, category)


def _run_chunked(chunk_fn, n_items: int, n_jobs: int, *, label: str) -> list:
    """Evaluates `chunk_fn((start, stop))` over all index bounds, returning
    one result per chunk in order.

    `n_jobs <= 1`, a small workload, or an unusable process pool all fall
    through to the *same* `chunk_fn` running in-process -- there is exactly
    one implementation of the per-item work, so serial and parallel results
    cannot drift apart for reasons other than the pool boundary itself.
    """
    if n_items <= 0:
        return []
    n_chunks = min(n_items, max(int(n_jobs), 1) * _CHUNKS_PER_WORKER)
    bounds = _chunk_bounds(n_items, n_chunks)
    if n_jobs <= 1 or n_items < _MIN_ITEMS_FOR_POOL:
        return [chunk_fn(b) for b in bounds]

    ctx = _fork_context()
    if ctx is None:
        warnings.warn(
            f"allocation_search: {label} asked for n_jobs={n_jobs} but this platform has no "
            f"'fork' multiprocessing start method (workers would have to re-pickle the universe "
            f"and score_fn per process); evaluating serially instead.",
            category=RuntimeWarning,
        )
        return [chunk_fn(b) for b in bounds]

    try:
        with ctx.Pool(processes=int(n_jobs)) as pool:
            return pool.map(chunk_fn, bounds)
    except Exception as exc:  # noqa: BLE001 - any pool-level failure => serial fallback
        warnings.warn(
            f"allocation_search: {label} process pool (n_jobs={n_jobs}) failed with "
            f"{type(exc).__name__}: {exc}; falling back to serial evaluation.",
            category=RuntimeWarning,
        )
        return [chunk_fn(b) for b in bounds]


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
    rebalance_dates = get_rebalance_dates(master_index, rebalance_freq_days)

    n_dates = len(rebalance_dates)
    n_symbols = len(symbols)

    # Generate random weights (Dirichlet distribution equivalent)
    raw_w = rng.exponential(scale=1.0, size=(n_dates, n_symbols))
    norm_w = raw_w / raw_w.sum(axis=1, keepdims=True)

    weights_rebal = pd.DataFrame(norm_w, index=rebalance_dates, columns=symbols)

    weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
    weights_df.loc[rebalance_dates] = weights_rebal

    return weights_df


class RandomAllocationTemplate(AllocationTemplate):
    """A dummy template used purely for the ERS check. Subclasses
    `AllocationTemplate` (rather than staying a bare standalone class) so it
    automatically inherits the full template protocol -- `name`,
    `param_grid`, `factor_tags`, `generate_weights`, `explain_weights`,
    `warmup_bars` -- and any future addition to that protocol, instead of
    silently missing a member a future caller happens to need. This is the
    exact same latent-bug shape as `warmup_bars` below: `backtester.
    run_walkforward` calling it unconditionally and finding it absent used
    to raise `AttributeError`, which `_safe_score` would catch and silently
    degrade to `-inf` instead of failing loudly.

    `__init__` keeps its original single-`rng`-argument signature (not the
    base dataclass's `(name, param_grid, factor_tags)`) since every existing
    call site constructs this with just `RandomAllocationTemplate(rng)` --
    the base class's fields are supplied internally instead, hardcoded to
    this template's own fixed identity.
    """

    def __init__(self, rng: np.random.Generator):
        super().__init__(name="random_allocation", param_grid={}, factor_tags=[])
        self.rng = rng

    def generate_weights(self, universe: dict, params: dict) -> pd.DataFrame:
        return random_weights(universe, params["rebalance_freq_days"], self.rng)

    def explain_weights(self, params: dict) -> str:
        return (
            f"Random-weight portfolio (ERS null comparison), rebalanced every "
            f"{params.get('rebalance_freq_days', '?')} trading days."
        )

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


def _grid_chunk_worker(bounds) -> list:
    """Scores one contiguous slice of `_PARALLEL_STATE["combos"]`.

    Module-level (not a closure) and taking only an `(start, stop)` pair, so
    the pool task payload is two ints -- `template`, `score_fn` and its
    captured universe are inherited through fork, never pickled (B2.4).
    Warnings are recorded rather than emitted, because a warning raised in a
    child process would otherwise be invisible to the caller; the parent
    re-emits them via `_reemit_warnings`.
    """
    start, stop = bounds
    state = _PARALLEL_STATE
    template, score_fn, combos = state["template"], state["score_fn"], state["combos"]
    out = []
    for params in combos[start:stop]:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _safe_score(score_fn, template, params)
        out.append((params, result, [(w.category, str(w.message)) for w in caught]))
    return out


def grid_search_template(template, score_fn: ScoreFn, *, n_jobs: Optional[int] = 1) -> list:
    """Evaluates `score_fn` over every combination in `template.param_grid`.
    Returns `[{"params", "result", "score"}, ...]`, one entry per
    combination (`grid_combinations({})` -> `[{}]`, so a degenerate/empty
    `param_grid` still yields exactly one trial, never zero). Zero
    cross-template dependency -- safe to call once per template in a
    multi-template search without altering behavior.

    `n_jobs` (B2.4) fans the candidate evaluations out over a forked process
    pool; see `resolve_n_jobs` for the exact semantics (default `1` =
    serial, `-1`/`None` = CPU-adaptive) and `_run_chunked` for the fallback.
    The returned trials are still in `grid_combinations` order, with
    byte-identical `params`/`score`/`result` values: chunking only decides
    *which process* runs a candidate, never *what* is run.
    """
    combos = grid_combinations(template.param_grid)
    n_jobs_eff = resolve_n_jobs(n_jobs, len(combos))
    with _worker_payload(template=template, score_fn=score_fn, combos=combos):
        chunks = _run_chunked(
            _grid_chunk_worker, len(combos), n_jobs_eff,
            label=f"grid_search_template[{getattr(template, 'name', template)}]",
        )

    trials = []
    for chunk in chunks:
        for params, result, captured in chunk:
            _reemit_warnings(captured)
            trials.append({
                "params": params,
                "result": result,
                "score": result.get("sharpe_ratio", float("-inf")),
            })

    if len(trials) > 0 and all(t["score"] == float("-inf") for t in trials):
        warnings.warn(
            f"grid_search_template: ALL {len(trials)} trials for template "
            f"'{getattr(template, 'name', template)}' returned -inf -- this usually means "
            f"score_fn itself is broken (a bug, not just bad candidates), not that every "
            f"parameter combination is genuinely bad. Check the individual per-trial warnings "
            f"above for the actual exception(s).",
            category=RuntimeWarning,
        )
    return trials


def _ers_draw_seeds(seed: Optional[int], n_draws: int) -> list:
    """Derives one independent `SeedSequence` per ERS draw from the caller's
    single master `seed` (B2.4).

    This is what makes the random-portfolio stream independent of execution
    order: draw `i` is always `default_rng(SeedSequence(seed).spawn(n)[i])`,
    whether it is evaluated serially, in a 4-worker chunk, or as draw #913
    of a 32-way split. Without it (the pre-B2.4 behaviour) every draw
    advanced ONE shared `Generator`, so draw `i` was only reachable after
    draws `0..i-1` -- inherently order-dependent and un-parallelisable.

    DISCLOSED BEHAVIOUR CHANGE: for the same `seed`, the concrete random
    draws differ from the pre-B2.4 single-stream generator (same
    distribution, same count, different fixed sample). That is unavoidable
    for an order-independent stream; the ERS statistic itself (percentile of
    `best_score` among the draws) is unchanged in meaning.
    """
    if n_draws <= 0:
        return []
    return list(np.random.SeedSequence(seed).spawn(int(n_draws)))


def _ers_chunk_worker(bounds) -> list:
    """Scores one contiguous slice of ERS draws (B2.4).

    Each draw gets its OWN `RandomAllocationTemplate` seeded from the
    pre-derived `SeedSequence` at that draw index, so the chunk boundaries a
    given run happens to use cannot affect the sampled weights. `minimal_
    metrics_scope` is entered *inside* the worker as well -- the ambient
    ContextVar is inherited across fork, but relying on that would be a
    latent trap for any future non-fork start method.
    """
    start, stop = bounds
    state = _PARALLEL_STATE
    score_fn, freq = state["score_fn"], state["freq"]
    seeds, minimal = state["seeds"], state["minimal_metrics"]
    out = []
    for i in range(start, stop):
        template = RandomAllocationTemplate(np.random.default_rng(seeds[i]))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with minimal_metrics_scope(enabled=minimal):
                res = _safe_score(score_fn, template, {"rebalance_freq_days": freq})
        out.append((res.get("sharpe_ratio", float("-inf")),
                    [(w.category, str(w.message)) for w in caught]))
    return out


def _ers_draw_scores(score_fn: ScoreFn, winning_freq, seed: Optional[int], n_random_search: int,
                     minimal_metrics: bool, n_jobs: int) -> tuple:
    """Evaluates all `n_random_search` random portfolios and returns
    `(raw_scores_in_draw_order, warnings)`.

    Kept separate from `run_ers_validation` so the sampling stream can be
    asserted directly in tests (`n_jobs=1` vs `n_jobs=N` must yield the same
    list, element for element and bit for bit).
    """
    seeds = _ers_draw_seeds(seed, n_random_search)
    with _worker_payload(score_fn=score_fn, freq=winning_freq, seeds=seeds,
                         minimal_metrics=minimal_metrics):
        chunks = _run_chunked(_ers_chunk_worker, n_random_search, n_jobs, label="run_ers_validation")

    raw_scores, captured = [], []
    for chunk in chunks:
        for score, chunk_warnings in chunk:
            raw_scores.append(score)
            captured.extend(chunk_warnings)
    return raw_scores, captured


def run_ers_validation(params: dict, best_score: float, best_result: dict, score_fn: ScoreFn, *,
                        n_random_search: int = 200, ers_percentile_threshold: float = 0.90,
                        min_rebalances_for_trust: int = 4, seed: Optional[int] = None,
                        minimal_metrics: bool = True,
                        n_jobs: Optional[int] = 1) -> dict:
    """Equivalent Random Search: draws `n_random_search` random-weight
    portfolios (`RandomAllocationTemplate`) at
    `params.get("rebalance_freq_days", 21)`, scores each through `score_fn`,
    and computes `best_score`'s percentile rank against the survivors. Call
    this ONCE per search (on whatever `(params, best_score, best_result)` a
    caller has already picked as the winner) -- never once per candidate
    template, or `n_random_search` draws are burned per template instead of
    once overall.

    `n_jobs` (B2.4) splits THAT ONE ERS's draws into contiguous chunks
    evaluated concurrently -- it never changes how many draws a search
    spends, who they are spent on, or how many ERS runs happen. See
    `resolve_n_jobs` for the argument semantics; the default `n_jobs=1` is
    the old in-process loop.

    Returns `{"ers_percentile", "ers_passed", "trusted"}`. `ers_percentile`
    defaults to 0.0 (fail-safe -- this candidate has NOT been shown to beat
    anything), never a trivial 1.0, when every random trial is
    non-finite/filtered out.

    `minimal_metrics` (default True, task B1.2) wraps the whole draw loop in
    `common.allocation_backtester.minimal_metrics_scope()`, so a `score_fn`
    that backtests each draw through `run_allocation_backtest` builds no
    per-draw trade/weight frames. The candidate's own `best_score` /
    `best_result` are passed IN (already computed outside this scope), so the
    ERS result is unchanged: same draws, same seed stream, same percentile.
    Pass False only to reproduce a pre-B1.2 full-report ERS run.
    """
    winning_freq = params.get("rebalance_freq_days", 21)

    # B1.2's minimal-metrics scope is still per-draw (inside the chunk
    # worker), so a `score_fn` that runs `run_allocation_backtest` (both
    # `strategy_generator` and `backtester` do) skips building the per-draw
    # `rebalance_report` / `actual_weights` frames -- only the scalar Sharpe
    # is compared. B2.4 only adds concurrency on top: same draws, same
    # count, still exactly ONE ERS per search.
    raw_scores, captured = _ers_draw_scores(
        score_fn, winning_freq, seed, n_random_search, minimal_metrics,
        resolve_n_jobs(n_jobs, n_random_search),
    )
    _reemit_warnings(captured)

    random_scores = [s for s in raw_scores if np.isfinite(s)]

    if n_random_search > 0 and not random_scores:
        warnings.warn(
            f"run_ers_validation: ALL {n_random_search} random-portfolio draws returned "
            f"-inf/non-finite -- the random comparison pool is EMPTY. This usually means "
            f"score_fn itself is broken (a bug, not that every random draw was genuinely bad), "
            f"not a normal 'candidate didn't beat the random pool' outcome. Check the "
            f"individual per-trial warnings above for the actual exception(s). Falling back to "
            f"the fail-safe ers_percentile=0.0 (untrusted).",
            category=RuntimeWarning,
        )

    ers_percentile = float((np.array(random_scores) < best_score).mean()) if random_scores else 0.0
    ers_passed = ers_percentile >= ers_percentile_threshold
    trusted = ers_passed and best_result.get("total_rebalances", 0) >= min_rebalances_for_trust
    return {"ers_percentile": ers_percentile, "ers_passed": ers_passed, "trusted": trusted}


def optimize_template(universe: dict, template, score_fn: ScoreFn, *,
                       n_random_search: int = 200, ers_percentile_threshold: float = 0.90,
                       min_rebalances_for_trust: int = 4, seed: Optional[int] = None,
                       minimal_metrics: bool = True, n_jobs: Optional[int] = 1) -> dict:
    """Single-template grid-search + ERS validation (`grid_search_template`
    then `run_ers_validation` on its winner). For a MULTI-template search, do
    NOT call this once per template -- call `grid_search_template` per
    template yourself, reduce to one winner, then call `run_ers_validation`
    ONCE on that winner (see `pipeline/strategy_generator/stratgen/generator.py`'s
    `_search_allocation`).

    `n_jobs` (B2.4) is forwarded unchanged to both phases; it parallelises
    the candidate/draw evaluations within each phase and never changes the
    number of grid candidates or random draws.

    Returns `{"best_params", "best_result", "best_score", "ers_percentile",
    "ers_passed", "trusted", "n_trials", "all_trials"}`. `all_trials` is the
    full `grid_search_template()` trace, for diagnostics/reporting.
    """
    if not universe:
        raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

    trials = grid_search_template(template, score_fn, n_jobs=n_jobs)
    best = max(trials, key=lambda t: t["score"])
    ers = run_ers_validation(
        best["params"], best["score"], best["result"], score_fn,
        n_random_search=n_random_search, ers_percentile_threshold=ers_percentile_threshold,
        min_rebalances_for_trust=min_rebalances_for_trust, seed=seed,
        minimal_metrics=minimal_metrics, n_jobs=n_jobs,
    )
    return {
        "best_params": best["params"],
        "best_result": best["result"],
        "best_score": best["score"],
        **ers,
        "n_trials": len(trials) + n_random_search,
        "all_trials": trials,
    }

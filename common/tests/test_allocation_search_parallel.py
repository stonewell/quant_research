"""B2.4: process-pool fan-out for `common/allocation_search.py`'s grid search
and ERS sampling.

100% offline (synthetic/hand-built data only). Two classes of property are
asserted here, and they are deliberately kept apart:

1. **Determinism / bit-identity** -- a parallel run must reproduce the serial
   run EXACTLY (same candidate order, same `params`, same float scores at
   `atol=0`, same ERS sampling stream in draw order). Not "statistically
   equivalent".
2. **Zero data pickling** -- worker payloads are inherited through `fork`
   (copy-on-write), so a `score_fn` closing over a non-picklable object (a
   `threading.Lock`, a bare lambda) still runs in parallel. If the
   implementation ever switched to shipping the universe/closure per worker,
   these tests would fail with a pickling error.

Plus the graceful-degradation contract: no `fork` start method, or a pool
that refuses to start, must emit a RuntimeWarning and fall back to serial
rather than crash.
"""

import hashlib
import math
import os
import sys
import threading
import warnings

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import allocation_search as search_mod
from common.allocation_search import (
    RandomAllocationTemplate,
    _ers_draw_scores,
    _ers_draw_seeds,
    grid_combinations,
    grid_search_template,
    optimize_template,
    resolve_n_jobs,
    run_ers_validation,
)

# Any pool-based test needs at least `_MIN_ITEMS_FOR_POOL` items to actually
# build a pool; these constants make that dependency explicit rather than
# implicit, so if the threshold is retuned the tests still exercise the pool.
_GRID = {"n": [3, 5, 8], "x": [1.5, 2.0, 2.5]}  # 9 combos >= _MIN_ITEMS_FOR_POOL
_N_ERS_DRAWS = 16  # >= _MIN_ITEMS_FOR_POOL


def _universe(periods=60):
    idx = pd.bdate_range("2020-01-01", periods=periods)
    return {
        "A": pd.DataFrame({"Close": np.linspace(100.0, 120.0, periods)}, index=idx),
        "B": pd.DataFrame({"Close": np.linspace(50.0, 43.0, periods)}, index=idx),
    }


class _FakeTemplate:
    """Minimal duck-typed template (only `name` + `param_grid` are read by
    `grid_search_template`)."""

    def __init__(self, name="fake", param_grid=None):
        self.name = name
        self.param_grid = param_grid or {}


def _grid_score(template, params):
    """Deterministic, order-preserving, process-independent scorer.

    Purely arithmetic (`math.sqrt` / multiplication / addition), so a value
    computed in a forked child is bit-identical to the same value computed in
    the parent -- any mismatch therefore indicates reordering or a dropped
    candidate, not libm drift.
    """
    return {
        "sharpe_ratio": math.sqrt(float(params["n"])) * float(params["x"]),
        "total_rebalances": int(params["n"]),
        "total_turnover": float(params["x"]) / 10.0,
    }


def _digest_of_weights(weights: pd.DataFrame) -> float:
    """Stable float fingerprint of a generated weight matrix (NaN included by
    its canonical float64 bit pattern), used to assert that the ERS sampling
    STREAM -- not just its summary statistic -- is identical."""
    arr = np.ascontiguousarray(weights.to_numpy(dtype=float))
    return int.from_bytes(hashlib.sha256(arr.tobytes()).digest()[:8], "big") / float(1 << 64)


def _ers_digest_score(template, params):
    weights = template.generate_weights(_universe(), params)
    return {"sharpe_ratio": _digest_of_weights(weights), "total_rebalances": 10}


def _mixed_score(template, params):
    """Routes ERS draws through the real `RandomAllocationTemplate` weight
    generator and grid candidates through `_grid_score`, so
    `optimize_template` exercises both phases with finite scores."""
    if isinstance(template, RandomAllocationTemplate):
        return _ers_digest_score(template, params)
    return _grid_score(template, params)


# ---------------------------------------------------------------------------
# n_jobs semantics
# ---------------------------------------------------------------------------

def test_resolve_n_jobs_auto_never_exceeds_work_items():
    cpu = os.cpu_count() or 1
    assert resolve_n_jobs(None, 4) == min(cpu, 4)
    assert resolve_n_jobs(-1, 4) == min(cpu, 4)
    assert resolve_n_jobs(None, 1000) == cpu
    # Never zero/negative, even for a degenerate call with no work items.
    assert resolve_n_jobs(None, 0) == 1


def test_resolve_n_jobs_explicit_values():
    assert resolve_n_jobs(1, 1000) == 1          # strictly serial
    assert resolve_n_jobs(3, 1000) == 3          # honoured
    assert resolve_n_jobs(3, 2) == 2             # capped by work items
    assert resolve_n_jobs(np.int64(2), 10) == 2  # numpy ints accepted


def test_resolve_n_jobs_rejects_ambiguous_values():
    with pytest.raises(ValueError, match="n_jobs must be"):
        resolve_n_jobs(0, 10)
    with pytest.raises(ValueError, match="n_jobs must be"):
        resolve_n_jobs(-2, 10)
    with pytest.raises(TypeError, match="n_jobs must be"):
        resolve_n_jobs(True, 10)  # bool is an int subclass -- rejected explicitly
    with pytest.raises(TypeError, match="n_jobs must be"):
        resolve_n_jobs(2.5, 10)


# ---------------------------------------------------------------------------
# Grid search: parallel == serial, exactly
# ---------------------------------------------------------------------------

def test_grid_search_parallel_matches_serial_bitwise():
    template = _FakeTemplate(param_grid=_GRID)

    serial = grid_search_template(template, _grid_score, n_jobs=1)
    parallel = grid_search_template(template, _grid_score, n_jobs=3)

    assert len(serial) == len(parallel) == 9
    for s, p in zip(serial, parallel):
        # atol=0 / rtol=0 by construction: plain `==`, not approx.
        assert s["params"] == p["params"]
        assert s["score"] == p["score"]
        assert s["result"] == p["result"]


def test_grid_search_parallel_preserves_candidate_order():
    template = _FakeTemplate(param_grid=_GRID)
    trials = grid_search_template(template, _grid_score, n_jobs=3)
    assert [t["params"] for t in trials] == grid_combinations(_GRID)


def test_grid_search_parallel_actually_uses_worker_processes():
    """Guards against a silently-serial "parallel" implementation: the score
    encodes whether it ran in the parent process."""
    template = _FakeTemplate(param_grid={"n": [1, 2, 3, 4, 5, 6, 7, 8, 9]})
    parent_pid = os.getpid()

    def score_fn(t, params):
        return {"sharpe_ratio": float(os.getpid() != parent_pid), "total_rebalances": 1}

    serial = grid_search_template(template, score_fn, n_jobs=1)
    assert [t["score"] for t in serial] == [0.0] * 9

    parallel = grid_search_template(template, score_fn, n_jobs=3)
    assert [t["score"] for t in parallel] == [1.0] * 9


def test_api_default_is_serial_for_backward_compatibility():
    """B2.4 default is `n_jobs=1`, not auto: existing callers (and suites that
    assert parent-process mock call counts) keep the in-process path unless
    they opt in. Auto must be requested explicitly."""
    template = _FakeTemplate(param_grid={"n": list(range(1, 10))})
    parent_pid = os.getpid()

    def score_fn(t, params):
        return {"sharpe_ratio": float(os.getpid() != parent_pid), "total_rebalances": 1}

    default = grid_search_template(template, score_fn)  # no n_jobs argument
    assert [t["score"] for t in default] == [0.0] * 9

    auto = grid_search_template(template, score_fn, n_jobs=None)  # explicit auto
    assert [t["score"] for t in auto] == [1.0] * 9


def test_grid_search_parallel_degenerate_grid_stays_single_trial():
    template = _FakeTemplate(param_grid={})

    def score_fn(t, params):
        return {"sharpe_ratio": 1.0, "total_rebalances": 5}

    for n_jobs in (1, 4):
        trials = grid_search_template(template, score_fn, n_jobs=n_jobs)
        assert len(trials) == 1
        assert trials[0]["params"] == {}
        assert trials[0]["score"] == 1.0


# ---------------------------------------------------------------------------
# Zero data pickling (fork copy-on-write chunk strategy)
# ---------------------------------------------------------------------------

def test_grid_search_parallel_needs_no_pickling_of_score_fn_or_universe():
    """A `threading.Lock` and a lambda inside the score_fn closure are NOT
    picklable. `grid_search_template(n_jobs=3)` still works, proving the
    universe/score_fn are inherited by fork, never shipped to the workers."""
    lock = threading.Lock()
    payload = _universe()  # a second, non-trivial captured object

    def score_fn(t, params):
        with lock:  # would still work in a fork child (its own copy)
            weight = float(params["x"]) + len(payload["A"])
        return {"sharpe_ratio": weight, "total_rebalances": 1}

    trials = grid_search_template(_FakeTemplate(param_grid=_GRID), score_fn, n_jobs=3)
    assert len(trials) == 9
    assert all(isinstance(t["score"], float) for t in trials)


# ---------------------------------------------------------------------------
# Warning fidelity across the process boundary
# ---------------------------------------------------------------------------

def test_grid_search_parallel_forwards_per_trial_warnings():
    template = _FakeTemplate(name="broken_on_5", param_grid=_GRID)

    def score_fn(t, params):
        if params["n"] == 5:
            raise ValueError("boom on n=5")
        return _grid_score(t, params)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        trials = grid_search_template(template, score_fn, n_jobs=3)

    assert len(trials) == 9
    messages = [str(w.message) for w in caught]
    # One forwarded warning per failing candidate (3 of the 9 combos have n=5).
    assert len([m for m in messages if "boom on n=5" in m]) == 3
    assert not [m for m in messages if "ALL" in m and "broken_on_5" in m]
    assert all(issubclass(w.category, RuntimeWarning) for w in caught)


def test_grid_search_parallel_all_fail_emits_aggregate_warning_once():
    template = _FakeTemplate(name="always_broken", param_grid=_GRID)

    def score_fn(t, params):
        raise ValueError("always broken")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        trials = grid_search_template(template, score_fn, n_jobs=3)

    assert len(trials) == 9
    assert all(t["score"] == float("-inf") for t in trials)
    messages = [str(w.message) for w in caught]
    assert len([m for m in messages if "always broken" in m]) == 9
    assert len([m for m in messages if "ALL" in m and "always_broken" in m]) == 1


# ---------------------------------------------------------------------------
# ERS: same seed => same draws, serially and in parallel
# ---------------------------------------------------------------------------

def test_ers_draw_seeds_are_independent_and_deterministic():
    seeds = _ers_draw_seeds(1234, _N_ERS_DRAWS)
    assert len(seeds) == _N_ERS_DRAWS
    # Deterministic for the same master seed, independent per draw.
    assert [s.entropy for s in seeds] == [s.entropy for s in _ers_draw_seeds(1234, _N_ERS_DRAWS)]
    assert len({tuple(s.generate_state(4)) for s in seeds}) == _N_ERS_DRAWS
    # A different master seed gives a different stream.
    other = _ers_draw_seeds(99, _N_ERS_DRAWS)
    assert tuple(other[0].generate_state(4)) != tuple(seeds[0].generate_state(4))
    assert _ers_draw_seeds(7, 0) == []


def test_ers_parallel_sampling_stream_matches_serial_and_reference_bitwise():
    """The whole point of `SeedSequence.spawn`: draw `i` is a pure function of
    `(seed, i)`, so chunking cannot change the sample. Asserted three ways --
    serial == parallel == an independently recomputed reference, at `==`."""
    serial, _ = _ers_draw_scores(_ers_digest_score, 21, 4242, _N_ERS_DRAWS, True, 1)
    parallel, _ = _ers_draw_scores(_ers_digest_score, 21, 4242, _N_ERS_DRAWS, True, 4)

    reference = []
    for child in np.random.SeedSequence(4242).spawn(_N_ERS_DRAWS):
        template = RandomAllocationTemplate(np.random.default_rng(child))
        reference.append(_digest_of_weights(template.generate_weights(_universe(), {"rebalance_freq_days": 21})))

    assert serial == reference  # exactly, element for element
    assert parallel == reference
    assert len(serial) == _N_ERS_DRAWS


def test_run_ers_validation_parallel_matches_serial():
    serial = run_ers_validation(
        {"rebalance_freq_days": 21}, 0.5, {"total_rebalances": 10}, _ers_digest_score,
        n_random_search=_N_ERS_DRAWS, seed=4242, n_jobs=1,
    )
    parallel = run_ers_validation(
        {"rebalance_freq_days": 21}, 0.5, {"total_rebalances": 10}, _ers_digest_score,
        n_random_search=_N_ERS_DRAWS, seed=4242, n_jobs=4,
    )
    assert parallel == serial


def test_ers_parallel_draws_still_run_in_minimal_metrics_mode():
    """B1.2's light-weight mode must survive the process boundary: the flag is
    checked inside each worker, and only a scalar crosses back."""
    from common.allocation_backtester import run_allocation_backtest
    from common.testing import make_ohlcv_from_closes

    universe = {"A": make_ohlcv_from_closes(np.linspace(100, 120, 40), start="2020-01-01")}

    def score_fn(template, params):
        weights = template.generate_weights(universe, params)
        res = run_allocation_backtest(universe, weights)
        return {"sharpe_ratio": 1.0 if res["rebalance_report"].empty else 0.0,
                "total_rebalances": res.get("total_rebalances", 0)}

    minimal, _ = _ers_draw_scores(score_fn, 5, 0, _N_ERS_DRAWS, True, 2)
    assert minimal == [1.0] * _N_ERS_DRAWS, "every parallelised ERS draw must be minimal-metrics"

    full, _ = _ers_draw_scores(score_fn, 5, 0, _N_ERS_DRAWS, False, 2)
    assert full == [0.0] * _N_ERS_DRAWS, "the opt-out must restore full reports in workers too"


def test_ers_parallel_forwards_draw_warnings():
    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            raise ValueError("random scoring broken")
        return {"sharpe_ratio": 5.0, "total_rebalances": 10}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_ers_validation(
            {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 10}, score_fn,
            n_random_search=_N_ERS_DRAWS, seed=0, n_jobs=3,
        )

    assert result["ers_percentile"] == 0.0
    messages = [str(w.message) for w in caught]
    assert len([m for m in messages if "random scoring broken" in m]) == _N_ERS_DRAWS
    assert len([m for m in messages if "ALL" in m and "EMPTY" in m]) == 1


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_parallel_falls_back_to_serial_without_fork(monkeypatch):
    monkeypatch.setattr(search_mod, "_fork_context", lambda: None)
    template = _FakeTemplate(param_grid=_GRID)

    with pytest.warns(RuntimeWarning, match="no 'fork' multiprocessing start method"):
        trials = grid_search_template(template, _grid_score, n_jobs=3)

    assert [t["score"] for t in trials] == [t["score"] for t in
                                             grid_search_template(template, _grid_score, n_jobs=1)]


def test_parallel_falls_back_to_serial_when_pool_creation_fails(monkeypatch):
    class _BadContext:
        def Pool(self, processes):  # noqa: N802 - mimic the mp context API
            raise OSError("process pools are forbidden in this sandbox")

    monkeypatch.setattr(search_mod, "_fork_context", lambda: _BadContext())

    with pytest.warns(RuntimeWarning, match="falling back to serial evaluation"):
        result = run_ers_validation(
            {"rebalance_freq_days": 21}, 0.5, {"total_rebalances": 10}, _ers_digest_score,
            n_random_search=_N_ERS_DRAWS, seed=4242, n_jobs=4,
        )

    assert result == run_ers_validation(
        {"rebalance_freq_days": 21}, 0.5, {"total_rebalances": 10}, _ers_digest_score,
        n_random_search=_N_ERS_DRAWS, seed=4242, n_jobs=1,
    )


def test_small_workload_skips_pool_entirely():
    """Below `_MIN_ITEMS_FOR_POOL` the pool overhead exceeds the work itself,
    so no processes are forked at all (same results either way)."""
    template = _FakeTemplate(param_grid={"n": [1, 2], "x": [1.0]})
    assert search_mod._MIN_ITEMS_FOR_POOL > 2
    trials = grid_search_template(template, _grid_score, n_jobs=4)
    assert len(trials) == 2


# ---------------------------------------------------------------------------
# optimize_template plumbing
# ---------------------------------------------------------------------------

def test_optimize_template_parallel_matches_serial():
    template = _FakeTemplate(param_grid=_GRID)
    serial = optimize_template(_universe(), template, _mixed_score,
                               n_random_search=_N_ERS_DRAWS, seed=11, n_jobs=1)
    parallel = optimize_template(_universe(), template, _mixed_score,
                                 n_random_search=_N_ERS_DRAWS, seed=11, n_jobs=3)

    assert parallel["best_params"] == serial["best_params"]
    assert parallel["best_score"] == serial["best_score"]
    assert parallel["ers_percentile"] == serial["ers_percentile"]
    assert parallel["n_trials"] == serial["n_trials"]
    assert [t["score"] for t in parallel["all_trials"]] == [t["score"] for t in serial["all_trials"]]


def test_ers_still_runs_exactly_once_per_search():
    """The B2.4 hard rule: parallelism must not multiply ERS runs. Count how
    many random-portfolio draws are scored -- it must equal
    `n_random_search`, however many workers are used."""
    counters = {"random": 0, "candidate": 0}
    lock = threading.Lock()

    def score_fn(t, params):
        with lock:
            if isinstance(t, RandomAllocationTemplate):
                counters["random"] += 1
            else:
                counters["candidate"] += 1
        return {"sharpe_ratio": 1.0, "total_rebalances": 10}

    # n_jobs=1 so the counters are visible in-process; the per-worker count is
    # separately covered by test_ers_parallel_forwards_draw_warnings, which
    # asserts exactly one forwarded warning per draw across 3 workers.
    optimize_template(_universe(), _FakeTemplate(param_grid=_GRID), score_fn,
                      n_random_search=_N_ERS_DRAWS, seed=3, n_jobs=1)
    assert counters["candidate"] == 9
    assert counters["random"] == _N_ERS_DRAWS

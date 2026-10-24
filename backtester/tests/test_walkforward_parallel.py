"""`B2.2` -- window-level multiprocessing scheduler for `run_walkforward`.

The whole point of this task is a *performance* change with ZERO behavioural
change, so these tests are written as proofs rather than as smoke tests:

1. **Bit-for-bit equality.** `--n-jobs 1` and `--n-jobs 4` must produce the same
   `folds_df` AND the same `walkforward_rebalances.csv` -- same columns, same
   ROW ORDER, same float values -- compared with `check_exact=True` plus a
   literal `to_csv()` string comparison (the strongest available statement:
   two independently produced CSV bodies, byte for byte).
2. **Real parallelism.** A fork-inherited monkeypatch records `os.getpid()` per
   fold, so "the pool was used" is measured, not assumed.
3. **Loud degradation.** A container/platform that cannot fork must fall back
   to the serial loop with a WARNING, produce identical numbers, and never
   crash.
4. **Per-fold deterministic seeding** via `SeedSequence`, so fold k draws the
   same numbers regardless of which worker (or process count) runs it.

Everything is offline: synthetic OHLCV only (`common.testing`), no provider that
touches the network, no real market data.
"""

import json
import multiprocessing
import os
import sys

import numpy as np
import pandas as pd
import pytest

# Add the repo root + pipeline group dir to sys.path (same convention as
# backtester/tests/test_run_backtest.py).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_PROJECT_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

import backtester.run_backtest as run_backtest
from backtester.run_backtest import (
    WalkforwardPoolUnavailable,
    _walkforward_pool_context,
    resolve_n_jobs,
    run_walkforward,
    walkforward_fold_seed,
)
from common.allocation_backtester import REBALANCE_REPORT_COLUMNS
from common.strategy_spec import get_template
from common.testing import make_ohlcv_from_closes as make_df


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
class _WalkforwardArgs:
    """Minimal stand-in for the CLI namespace `run_walkforward` reads."""

    def __init__(self, **kwargs):
        self.initial_capital = 100_000.0
        self.commission_pct = 0.0005
        self.slippage_pct = 0.0005
        self.min_shares = 1
        self.min_weight_change = 0.0
        self.window_years = 0.25
        self.step_years = 0.06
        self.start = None
        self.end = None
        self.china_trading = False
        self.us_trading = False
        self.hk_trading = False
        self.n_jobs = 1
        self.seed = None
        for key, value in kwargs.items():
            setattr(self, key, value)


def _synthetic_universe(n_symbols=4, n_bars=420):
    """Deterministic, offline universe (distinct drift/vol per symbol so fold
    metrics are genuinely non-trivial and not all identical)."""
    rng = np.random.default_rng(11)
    universe = {}
    for i in range(n_symbols):
        drift = 0.0002 + 0.0001 * i
        vol = 0.010 + 0.004 * i
        closes = 100.0 * np.exp(np.cumsum(rng.normal(drift, vol, n_bars)))
        universe[f"SYM{i}"] = make_df(closes, start="2020-01-01")
    return universe


def _template_and_params():
    # inverse_volatility exercises the warmup buffer + a real per-fold backtest
    # with periodic rebalances, i.e. the full fold body (not a trivial one).
    return get_template("inverse_volatility"), {"vol_lookback": 30, "rebalance_freq_days": 21}


def _folds_frame(folds):
    """Exactly the transformation `main()` applies before writing
    walkforward_report.csv (rebalance_report popped out of the fold records)."""
    return pd.DataFrame([{k: v for k, v in f.items() if k != "rebalance_report"} for f in folds])


def _rebalances_frame(folds):
    """Exactly the transformation `main()` applies before writing
    walkforward_rebalances.csv (fold number prepended, folds concatenated in
    fold order)."""
    frames = []
    for fold_idx, f in enumerate(folds, 1):
        report = f.get("rebalance_report")
        if report is not None and not report.empty:
            df = report.copy()
            df.insert(0, "fold", fold_idx)
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["fold"] + REBALANCE_REPORT_COLUMNS)
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# 1. `--n-jobs` semantics
# --------------------------------------------------------------------------- #
def test_resolve_n_jobs_auto_and_explicit_semantics():
    cpu = os.cpu_count() or 1

    # Unset / -1 / 0 all mean AUTO: min(cpu_count, n_folds).
    for requested in (None, -1, 0):
        assert resolve_n_jobs(requested, 40) == max(1, min(cpu, 40)), requested
        assert resolve_n_jobs(requested, 3) == max(1, min(cpu, 3)), requested

    # 1 is the serial loop, verbatim.
    assert resolve_n_jobs(1, 40) == 1
    # More workers than folds is pointless: cap at n_folds.
    assert resolve_n_jobs(8, 3) == 3
    assert resolve_n_jobs(8, 40) == min(8, 40)
    # Degenerate fold counts never yield 0 workers.
    assert resolve_n_jobs(None, 0) == 1
    assert resolve_n_jobs(4, 0) == 1

    with pytest.raises(ValueError, match="n-jobs"):
        resolve_n_jobs(-2, 10)
    with pytest.raises(ValueError, match="n-jobs"):
        resolve_n_jobs(2.5, 10)


def test_arg_parser_exposes_n_jobs_alias_and_defaults_to_auto():
    parser = run_backtest.build_arg_parser()
    args = parser.parse_args(["--strategy-file", "x.json"])
    assert args.n_jobs is None  # None == AUTO (min(cpu_count, n_folds))
    assert args.seed is None
    assert parser.parse_args(["--strategy-file", "x.json", "-j", "4"]).n_jobs == 4
    assert parser.parse_args(["--strategy-file", "x.json", "--n-jobs", "1"]).n_jobs == 1


# --------------------------------------------------------------------------- #
# 2. Deterministic per-fold seeding
# --------------------------------------------------------------------------- #
def test_fold_seeds_derive_independently_from_fold_index():
    seeds = [walkforward_fold_seed(42, i) for i in range(1, 6)]
    # Every fold gets its own stream (no shared/derived-by-consumption state)...
    assert len(set(seeds)) == len(seeds)
    # ...and the mapping is a pure function of (master seed, fold index), so a
    # fold draws the same numbers whichever worker picks it up / in what order.
    assert seeds == [walkforward_fold_seed(42, i) for i in range(1, 6)]
    # Different master seeds must diverge, and no seed means "don't touch RNG".
    assert walkforward_fold_seed(42, 1) != walkforward_fold_seed(43, 1)
    assert walkforward_fold_seed(None, 1) is None


def test_seeded_run_is_process_count_invariant_and_leaves_global_rng_untouched():
    universe = _synthetic_universe()
    template, params = _template_and_params()

    np.random.seed(2024)
    before = np.random.get_state()[1].copy()
    serial = run_walkforward(
        universe, template, params, _WalkforwardArgs(n_jobs=1, seed=7)
    )
    np.testing.assert_array_equal(np.random.get_state()[1], before)

    parallel = run_walkforward(
        universe, get_template("inverse_volatility"), params,
        _WalkforwardArgs(n_jobs=4, seed=7),
    )
    np.testing.assert_array_equal(np.random.get_state()[1], before)

    pd.testing.assert_frame_equal(_folds_frame(serial), _folds_frame(parallel), check_exact=True)


# --------------------------------------------------------------------------- #
# 3. THE proof: `--n-jobs 1` vs `--n-jobs 4` bit-for-bit
# --------------------------------------------------------------------------- #
def test_walkforward_n_jobs_1_and_4_are_bit_identical():
    universe = _synthetic_universe()
    template, params = _template_and_params()

    serial = run_walkforward(universe, template, params, _WalkforwardArgs(n_jobs=1))
    parallel = run_walkforward(
        universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=4)
    )

    assert len(serial) == len(parallel) >= 4, "fixture must produce enough folds to overlap workers"

    serial_df, parallel_df = _folds_frame(serial), _folds_frame(parallel)
    # Column ORDER, row ORDER and every float: exact, no tolerance.
    assert list(serial_df.columns) == list(parallel_df.columns)
    assert list(serial_df["start_date"]) == [f["start_date"] for f in serial]
    pd.testing.assert_frame_equal(serial_df, parallel_df, check_exact=True)
    assert serial_df.to_csv(index=False) == parallel_df.to_csv(index=False)

    # The rebalance trade log is where row ORDER is easiest to silently break
    # (completion-order concatenation), so it gets the same treatment.
    serial_reb, parallel_reb = _rebalances_frame(serial), _rebalances_frame(parallel)
    assert not serial_reb.empty, "fixture must actually generate rebalances for this proof to mean anything"
    assert list(serial_reb.columns) == ["fold"] + REBALANCE_REPORT_COLUMNS
    # fold column must be non-decreasing == folds concatenated in fold order.
    assert list(serial_reb["fold"]) == sorted(serial_reb["fold"])
    pd.testing.assert_frame_equal(serial_reb, parallel_reb, check_exact=True)
    assert serial_reb.to_csv(index=False) == parallel_reb.to_csv(index=False)


def test_parallel_results_are_reordered_by_fold_index_not_completion_order():
    """Deliberately desynchronise completion order from fold order: make the
    EARLY folds slow and the LATE folds fast (a 4-worker pool then finishes
    fold 4 long before fold 1). The assembled list must still be fold order."""
    universe = _synthetic_universe()
    template, params = _template_and_params()
    real = run_backtest._evaluate_walkforward_fold

    def slewed(ctx, spec):
        # fold_index 1 sleeps longest -> completion order is ~reversed (or at
        # least not fold order) with 4 workers.
        import time

        time.sleep(max(0.0, 0.30 - 0.05 * spec["fold_index"]))
        return real(ctx, spec)

    serial = run_walkforward(universe, template, params, _WalkforwardArgs(n_jobs=1))
    run_backtest._evaluate_walkforward_fold = slewed
    try:
        parallel = run_walkforward(
            universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=4)
        )
    finally:
        run_backtest._evaluate_walkforward_fold = real

    assert [f["start_date"] for f in parallel] == [f["start_date"] for f in serial]
    pd.testing.assert_frame_equal(_folds_frame(serial), _folds_frame(parallel), check_exact=True)
    assert (
        _rebalances_frame(serial).to_csv(index=False)
        == _rebalances_frame(parallel).to_csv(index=False)
    )


# --------------------------------------------------------------------------- #
# 4. The pool is really used (measured pids, not "should be")
# --------------------------------------------------------------------------- #
def test_only_fold_indices_cross_the_process_boundary(tmp_path, monkeypatch):
    """Requirement: the loaded universe must NOT be pickled per task (that would
    serialise tens of MB into every worker for every fold). The submitted task
    payload is provably a single `int` fold position; the data is inherited via
    fork/COW from `_WALKFORWARD_WORKER_CTX`."""
    from concurrent.futures import ProcessPoolExecutor

    universe = _synthetic_universe()
    template, params = _template_and_params()
    submitted = []
    real_submit = ProcessPoolExecutor.submit

    def recording_submit(self, fn, /, *args, **kwargs):
        submitted.append((getattr(fn, "__name__", repr(fn)), args, kwargs))
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(ProcessPoolExecutor, "submit", recording_submit)
    folds = run_walkforward(
        universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=3)
    )

    assert submitted, "parallel run submitted nothing?"
    for fn_name, args, kwargs in submitted:
        assert fn_name == "_walkforward_fold_worker"
        assert len(args) == 1 and isinstance(args[0], int), f"payload leaked: {args!r}"
        assert not kwargs
    # One task per fold, covering every fold position 0..n-1 exactly once.
    assert sorted(args[0] for _, args, _ in submitted) == list(range(len(folds)))
    # ...and the (large) universe reference is not left behind on the module.
    assert run_backtest._WALKFORWARD_WORKER_CTX is None


def test_parallel_path_runs_folds_in_multiple_processes(tmp_path, monkeypatch):
    universe = _synthetic_universe()
    template, params = _template_and_params()
    pid_log = tmp_path / "fold_pids.txt"
    real = run_backtest._evaluate_walkforward_fold

    def recording(ctx, spec):
        with open(pid_log, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        return real(ctx, spec)

    monkeypatch.setattr(run_backtest, "_evaluate_walkforward_fold", recording)

    serial = run_walkforward(universe, template, params, _WalkforwardArgs(n_jobs=1))
    serial_pids = {int(x) for x in pid_log.read_text().split()}
    assert serial_pids == {os.getpid()}, "serial path must stay in-process"

    pid_log.write_text("")
    parallel = run_walkforward(
        universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=4)
    )
    parallel_pids = {int(x) for x in pid_log.read_text().split()}
    assert len(parallel_pids) >= 2, f"expected multiple worker processes, saw {parallel_pids}"
    assert os.getpid() not in parallel_pids, "folds must be evaluated by workers, not the parent"

    pd.testing.assert_frame_equal(_folds_frame(serial), _folds_frame(parallel), check_exact=True)


# --------------------------------------------------------------------------- #
# 5. Graceful, LOUD degradation
# --------------------------------------------------------------------------- #
def test_spawn_only_platform_is_reported_unavailable(monkeypatch):
    """Windows-style platforms only offer 'spawn': the universe would have to be
    pickled into every worker, which is explicitly rejected -- so the pool must
    report itself unavailable rather than quietly duplicating the dataset."""
    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    with pytest.raises(WalkforwardPoolUnavailable, match="fork"):
        _walkforward_pool_context()


def test_pool_unavailable_falls_back_to_serial_with_warning(monkeypatch, capsys):
    universe = _synthetic_universe()
    template, params = _template_and_params()
    serial = run_walkforward(universe, template, params, _WalkforwardArgs(n_jobs=1))

    def no_fork():
        raise WalkforwardPoolUnavailable("simulated container without fork")

    monkeypatch.setattr(run_backtest, "_walkforward_pool_context", no_fork)

    with pytest.warns(UserWarning, match="process pool unavailable"):
        degraded = run_walkforward(
            universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=4)
        )

    out = capsys.readouterr().out
    assert "WARNING:" in out and "falling back to the serial" in out, "degradation must not be silent"
    pd.testing.assert_frame_equal(_folds_frame(serial), _folds_frame(degraded), check_exact=True)
    assert (
        _rebalances_frame(serial).to_csv(index=False)
        == _rebalances_frame(degraded).to_csv(index=False)
    )


def test_broken_pool_midway_falls_back_to_serial_with_warning(monkeypatch, capsys):
    """A worker dying mid-run (BrokenProcessPool) must degrade, not propagate."""
    universe = _synthetic_universe()
    template, params = _template_and_params()
    serial = run_walkforward(universe, template, params, _WalkforwardArgs(n_jobs=1))

    def broken(ctx, fold_specs, n_jobs):
        raise WalkforwardPoolUnavailable("BrokenProcessPool: simulated worker death")

    monkeypatch.setattr(run_backtest, "_run_walkforward_folds_parallel", broken)

    with pytest.warns(UserWarning, match="process pool unavailable"):
        degraded = run_walkforward(
            universe, get_template("inverse_volatility"), params, _WalkforwardArgs(n_jobs=4)
        )

    assert "WARNING:" in capsys.readouterr().out
    pd.testing.assert_frame_equal(_folds_frame(serial), _folds_frame(degraded), check_exact=True)


def test_fold_exception_still_yields_nan_metrics_in_parallel(monkeypatch, capsys):
    """The pre-B2.2 fail-soft contract survives the move to workers: a fold whose
    BODY raises is reported (in fold order) as NaN metrics, and the remaining
    folds still run -- the pool is not torn down and the numbers match serial."""
    universe = _synthetic_universe()
    params = {"vol_lookback": 30, "rebalance_freq_days": 21}

    serial_template = get_template("inverse_volatility")
    serial = run_walkforward(universe, serial_template, params, _WalkforwardArgs(n_jobs=1))
    # Fold 2's eval window ends here; its buffered slice ends there too, which is
    # how the sabotaged template recognises "this is fold 2" without knowing the
    # fold index (generate_weights only ever sees the data).
    doomed_last_bar = serial[1]["end_date"]

    template = get_template("inverse_volatility")
    real_generate_weights = template.generate_weights

    def generate_weights(universe_arg, params_arg):
        if any(len(df) and df.index[-1].strftime("%Y-%m-%d") == doomed_last_bar
               for df in universe_arg.values()):
            raise ValueError("simulated fold blow-up")
        return real_generate_weights(universe_arg, params_arg)

    monkeypatch.setattr(template, "generate_weights", generate_weights)

    parallel = run_walkforward(
        universe, template, params, _WalkforwardArgs(n_jobs=3)
    )

    assert np.isnan(parallel[1]["sharpe_ratio"])
    assert parallel[1]["total_rebalances"] == 0
    assert list(parallel[1]["rebalance_report"].columns) == REBALANCE_REPORT_COLUMNS
    assert any(not np.isnan(f["sharpe_ratio"]) for i, f in enumerate(parallel) if i != 1)
    assert "Error in window" in capsys.readouterr().out

    # ...and the whole run is still bit-identical to the serial one.
    monkeypatch.setattr(template, "generate_weights", generate_weights)
    serial_sabotaged = run_walkforward(
        universe, template, params, _WalkforwardArgs(n_jobs=1)
    )
    pd.testing.assert_frame_equal(
        _folds_frame(serial_sabotaged), _folds_frame(parallel), check_exact=True
    )


# --------------------------------------------------------------------------- #
# 6. End-to-end: the two CSVs main() writes are byte-identical
# --------------------------------------------------------------------------- #
def _write_strategy_file(path, template_name="inverse_volatility",
                         params=None):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "template_name": template_name,
            "params": params or {"vol_lookback": 30, "rebalance_freq_days": 21},
        }, fh)


def test_main_walkforward_csv_outputs_are_byte_identical_across_n_jobs(
    tmp_path, monkeypatch, capsys
):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)

    def _run(tag, n_jobs):
        results_dir = tmp_path / tag
        argv = [
            "run_backtest.py",
            "--strategy-file", str(strategy_path),
            "--universe", "AAA", "BBB", "CCC",
            "--data-provider", "synthetic",
            "--no-cache",
            "--mode", "walkforward",
            "--window-years", "0.5",
            "--step-years", "0.1",
            "--start", "2020-01-01",
            "--end", "2022-12-31",
            "--seed", "17",
            "--n-jobs", str(n_jobs),
            "--results-dir", str(results_dir),
            "--cache-dir", str(tmp_path / "cache"),
        ]
        monkeypatch.setattr(sys, "argv", argv)
        run_backtest.main()
        return results_dir

    serial_dir = _run("serial", 1)
    capsys.readouterr()
    parallel_dir = _run("parallel", 4)

    report_serial = (serial_dir / "walkforward_report.csv").read_text(encoding="utf-8")
    report_parallel = (parallel_dir / "walkforward_report.csv").read_text(encoding="utf-8")
    assert len(pd.read_csv(serial_dir / "walkforward_report.csv")) >= 3
    assert report_serial == report_parallel, "folds_df CSV differs between --n-jobs 1 and 4"

    reb_serial_path = serial_dir / "walkforward_rebalances.csv"
    reb_parallel_path = parallel_dir / "walkforward_rebalances.csv"
    reb_serial = reb_serial_path.read_text(encoding="utf-8")
    reb_parallel = reb_parallel_path.read_text(encoding="utf-8")
    # Row ORDER matters here, not just content: this is the file that used to be
    # assembled from a fold list.
    assert not pd.read_csv(reb_serial_path).empty
    assert reb_serial == reb_parallel, "walkforward_rebalances.csv differs between --n-jobs 1 and 4"

    out = capsys.readouterr().out
    assert "Parallel walkforward: dispatching" in out

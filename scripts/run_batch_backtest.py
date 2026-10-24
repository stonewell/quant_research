#!/usr/bin/env python
"""`B2.3` -- one-process batch walk-forward backtests for N strategies.

Replaces `pipeline/scripts/backtest_blend_strategies.sh`'s shell `while` loop,
which cold-started a fresh Python interpreter (and re-queried DuckDB for the
whole universe) once per strategy. The shape here is:

    parent process
      ├── resolve the strategy file list, probe each one's `warmup_bars`
      ├── load the universe ONCE at the WIDEST warmup window any strategy needs
      ├── fork a process pool (workers inherit that universe by COW -- nothing
      │   is pickled per task, exactly the payload contract B2.2 established)
      └── per strategy in a worker: run `backtester/run_backtest.py`'s own
          `main()` verbatim, with ONLY its universe load swapped for the
          already-loaded dict (see `_UniverseInjection`)
      └── aggregate: global Sharpe / Calmar / MaxDD ranking matrix + failed list

## Why "reuse `run_backtest.main()` verbatim" instead of re-assembling folds

`run_walkforward()`'s fold grid, its fold-order assembly, `folds_df`, and
`walkforward_rebalances.csv` are four things a second implementation would
have to keep agreeing with forever. Instead of copying them, each worker
calls `backtester.run_backtest.main()` with a per-strategy `sys.argv`, and
republishes the pre-loaded universe through `run_backtest.load_universe_with_banner`
(the single name `main()` uses to fetch data). Consequences:

* **Bit-for-bit parity with a direct CLI run is structural, not asserted**: the
  same code path writes `walkforward_report.csv` / `walkforward_rebalances.csv`
  / `walkforward_summary.json`, including `--baseline-symbol` comparison
  reports, Track-C/T enrichment, `--strategy-params` merge and
  `min_weight_change` inheritance. `pipeline/tests/test_run_batch_backtest.py`
  still byte-compares the two, so a future `run_backtest.py` refactor that
  breaks the injection fails loudly rather than silently re-reading the cache.
* The injection is **keyed on the requested symbol set**: any other load
  (`--baseline-symbol`, whose universe is `[baseline_symbol]`) falls through to
  the real loader. So the batch's "load once" claim never silently feeds the
  wrong data to the wrong call -- and if `main()` somehow never hits the
  injection, the strategy is marked FAILED with that reason.

## Parallelism level: batch-level (between strategies), not fold-level

`--n-jobs` sizes a pool of STRATEGY workers; inside a worker the folds stay
serial by default (`--inner-n-jobs 1`). Rationale:

1. **No nested pools.** `run_backtest.build_arg_parser`'s own `--n-jobs` help
   documents that nesting pools (a search/outer layer fanning out while the
   fold layer also fans out) oversubscribes the machine; 55 strategies x N
   workers is already more parallelism than a 8-16 core box can use.
2. **55 strategies >> cores**, so cross-strategy scheduling keeps every worker
   busy for the whole batch, whereas per-strategy fold fan-out pays the pool
   spin-up cost 55 times and leaves cores idle whenever one strategy has few
   folds (short histories) or several strategies finish at different times.
3. **Parity.** Fold-level scheduling would mean re-implementing
   `run_walkforward`'s fold grid + assembly (see above); strategy-level
   scheduling reuses it untouched.
4. The universe is still shared by fork/COW, so "one load" holds either way.

### Measured, not asserted (2026-10-23 re-benchmark, offline, 32 cores)

6 blend dumps x 4 symbols, `2021-01-01..2021-12-31`, 0.25y window / 0.25y step
(4 folds each), `--data-provider synthetic --no-cache`, one discarded warm-up,
then the median of 3 runs:

| invocation | wall |
| --- | --- |
| old shell loop (6 cold starts, fold fan-out ON, as it used to run) | 52.9s |
| `--n-jobs 1` | 102.7s |
| `--n-jobs 2` | 56.1s |
| `--n-jobs 6` | 26.0s |
| `--n-jobs auto` (= min(32, 6) = 6) | 26.8s |

Two things this pins down, because a previous write-up mixed up these numbers:

* **AUTO is the right default** and the strategy pool is a real win (~2x over
  the old loop, ~3.9x over this script serialised). It is near-ideal: the
  serial total is 101.4s of strategy work and the longest single strategy is
  ~25s, which is what `--n-jobs 6` costs.
* **`--n-jobs 1` is the slow corner, not a safe default**: it forfeits the fold
  fan-out too, so it lands ~2x SLOWER than the old shell loop (which ran each
  strategy with fold parallelism on). The escape hatch is `--inner-n-jobs auto`
  (102.7s -> 52.9s), but even that loses to plain `--n-jobs 6` (26.0s), so it is
  only for batches with fewer strategies than cores. The break-even for the
  strategy pool is ~0.5s of work per strategy (pool spin-up ~2s end-to-end);
  the cheapest workload measured here -- a 1-fold "tiny" batch -- still costs
  ~5.5s per strategy, so AUTO never loses on this codebase.

## Failure isolation

A strategy that raises is recorded in `batch_failed.csv` /
`batch_summary.json["failed"]` with its exception text (and the full traceback
in `<results-dir>/run.log`) and the batch continues. Fold-level failures are
NOT re-invented here: `run_allocation_backtest`/`run_walkforward`'s existing
fail-soft contract (fold -> NaN metrics + printed error) is inherited as-is,
which is why a strategy can legitimately finish `status=ok` with
`n_valid_folds < n_folds`.

## 100% offline

Tests drive this script with `--data-provider synthetic --no-cache` only.
`--data-provider` defaults to `synthetic` per `AGENTS.md`; the blend wrapper
passes `fuyao` explicitly to preserve the old shell script's behavior.

## Outputs (all under `--output-dir`, default `scripts/results/batch`)

* `<output-dir>/batch_ranking.csv` -- the global ranking matrix, one row per
  strategy, columns in `RANKING_COLUMNS` order:
  `rank, status, strategy, strategy_file, template_name, n_folds,
  n_valid_folds, mean_sharpe_ratio, mean_cagr, mean_max_drawdown,
  mean_calmar_ratio, deflated_sharpe_ratio, fold_sharpe_std, total_rebalances,
  elapsed_seconds, error`.
  **Sort rule:** rows with `status == "ok"` AND a finite `mean_sharpe_ratio`
  are ranked 1..K by `mean_sharpe_ratio` DESC, then `mean_calmar_ratio` DESC,
  then `strategy` ASC; every other row (failed, or all-NaN folds) gets an empty
  `rank` and is listed after them, `ok`-but-NaN before `failed`, each
  alphabetical. So "unranked" never masquerades as "ranked last".
  Means are over each strategy's own walk-forward folds, using
  `run_backtest.py`'s own convention (NaN folds excluded).
* `<output-dir>/batch_folds.csv` -- long form: every fold of every strategy
  (`strategy, template_name, fold, start_date, end_date, sharpe_ratio, cagr,
  max_drawdown, calmar_ratio, win_rate, profit_factor, total_turnover,
  total_rebalances`).
* `<output-dir>/batch_failed.csv` -- `strategy, strategy_file, error`; empty
  (header only) when nothing failed.
* `<output-dir>/batch_summary.json` -- config, `ranking_rule`, `n_ok`/
  `n_failed`, every strategy's own `walkforward_summary.json` payload, the full
  failed list with tracebacks, and the fold records.
* `<output-dir>/<strategy stem>/` -- that strategy's untouched
  `run_backtest.py` artifacts (`walkforward_report.csv`,
  `walkforward_rebalances.csv`, `walkforward_summary.json`,
  `comparison_report.json` when `--baseline-symbol` is set) plus `run.log`.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

# Add the repo root to sys.path to allow importing from common / backtester
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.cli_utils import (  # noqa: E402
    add_data_provider_cli_args,
    bootstrap_project_paths,
    build_data_kwargs,
    load_universe_with_banner,
    shared_data_dir,
)

# research_strategy (blend strategies) lives under pipeline/ -- same bootstrap as
# backtester/run_backtest.py, so blend strategy.json files construct identically here.
bootstrap_project_paths(_REPO_ROOT, __file__)
import backtester.run_backtest as run_backtest  # noqa: E402
from common.reporting import write_json_report  # noqa: E402
from common.strategy_spec import get_template, load_strategy_file  # noqa: E402
from common.universe import add_universe_cli_args, resolve_universe_from_args  # noqa: E402

DEFAULT_OUTPUT_DIR = os.path.join(_REPO_ROOT, "scripts", "results", "batch")

# Per-strategy result directory name: the strategy FILE's stem, matching the old
# shell loop's `--results-dir "${RESULTS}/${name_only}"` so existing dashboards /
# docs that point at pipeline/results/<dump_stem>/ keep working.
RESULTS_SUBDIR_MODE_STEM = "stem"
RESULTS_SUBDIR_MODE_NAME = "name"

#: Columns of `batch_ranking.csv`, in order. `rank` is 1..K over the strategies
#: that produced a FINITE mean Sharpe (best first); every other row (failed, or
#: all-NaN folds) carries an empty rank and is sorted to the bottom.
RANKING_COLUMNS = [
    "rank",
    "status",
    "strategy",
    "strategy_file",
    "template_name",
    "n_folds",
    "n_valid_folds",
    "mean_sharpe_ratio",
    "mean_cagr",
    "mean_max_drawdown",
    "mean_calmar_ratio",
    "deflated_sharpe_ratio",
    "fold_sharpe_std",
    "total_rebalances",
    "elapsed_seconds",
    "error",
]

FOLD_COLUMNS = [
    "strategy",
    "template_name",
    "fold",
    "start_date",
    "end_date",
    "sharpe_ratio",
    "cagr",
    "max_drawdown",
    "calmar_ratio",
    "win_rate",
    "profit_factor",
    "total_turnover",
    "total_rebalances",
]

FAILED_COLUMNS = ["strategy", "strategy_file", "error"]

# --------------------------------------------------------------------------- #
# `--n-jobs` evidence: measured on 2026-10-23, 32-core Linux box, OFFLINE
# --------------------------------------------------------------------------- #
#: The exact benchmark the `--n-jobs` help quotes, so the numbers stay traceable.
N_JOBS_BENCH_SPAN = "2021-01-01..2021-12-31, 0.25y window / 0.25y step, 4 folds"
#: Wall-clock below which a single strategy cannot outrun the pool's spin-up.
#: Derivation: the 6-worker pool costs ~2s end-to-end (measured: 6 x ~5.5s serial
#: strategies took 8.7s wall at -j 6 instead of ~5.5s), i.e. ~0.33s per worker;
#: dividing that by the (workers-1) parallelism gain gives a break-even near 0.5s
#: per strategy. Nothing in this repo is that cheap: even a 1-fold "tiny" run
#: costs ~5.5s per strategy, which is why AUTO (the default) always wins here.
N_JOBS_PARALLEL_BREAK_EVEN = 0.5


def parse_n_jobs_arg(raw):
    """`--n-jobs` value parser: `"auto"` -> `None` (let `resolve_n_jobs` decide).

    `run_backtest.resolve_n_jobs` already treats unset/-1/0 as AUTO, but `--help`
    advertises the word "auto", and typing it used to die in argparse with
    `invalid int value: 'auto'` before the batch ever started (found by the
    B2.3 re-benchmark). Keep `-1`/`0` working verbatim.
    """
    if isinstance(raw, str) and raw.strip().lower() in {"auto", "none", ""}:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--n-jobs must be an integer, 'auto' (== unset), -1 or 0; got {raw!r}"
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Batch walk-forward backtests: loads the universe ONCE, runs N strategy files "
            "(default: parallel across strategies), and writes a global Sharpe/Calmar/MaxDD "
            "ranking matrix plus a failed list."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Equivalent of the old serial shell loop (pipeline/scripts/backtest_blend_strategies.sh):\n"
            "  uv run python scripts/run_batch_backtest.py \\\n"
            "    --strategies-glob 'pipeline/research_strategy/results/strategy_dumps/*blend*.json' \\\n"
            "    --universe-file docs/universe/china/50_stock.txt \\\n"
            "    --start 2022-01-01 --end 2026-01-01 \\\n"
            "    --window-years 0.25 --step-years 0.25 --min-shares 100 --china-trading \\\n"
            "    --baseline-symbol 000300.SH --data-provider fuyao \\\n"
            "    --output-dir pipeline/results\n"
        ),
    )
    p.add_argument(
        "--strategies", nargs="+", default=None, metavar="FILE",
        help="Strategy files to backtest. Each token may be: a strategy.json path, a comma-separated "
             "list of paths, a .txt/.list/.csv file with one path per line, or a directory (all *.json "
             "directly inside it).",
    )
    p.add_argument(
        "--strategies-glob", action="append", default=[], metavar="PATTERN",
        help="Shell-style glob (repeatable) expanded by THIS script, e.g. "
             "'pipeline/research_strategy/results/strategy_dumps/*blend*.json'. Preferred over letting "
             "bash expand a bare glob: an empty expansion is reported as an error instead of being "
             "passed through as a literal filename.",
    )
    add_universe_cli_args(p)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--window-years", type=float, default=1.0, help="Rolling window size (years).")
    p.add_argument("--step-years", type=float, default=0.5, help="Step between rolling windows (years).")
    p.add_argument("--initial-capital", type=float, default=100_000.0)
    p.add_argument("--commission-pct", type=float, default=0.0005)
    p.add_argument("--slippage-pct", type=float, default=0.0005)
    p.add_argument("--min-shares", type=int, default=1)
    p.add_argument("--min-weight-change", type=float, default=0.0)
    p.add_argument("--china-trading", action="store_true",
                   help="Apply China A-share trading rules (price limits, T+1, 100-share lots, stamp duty).")
    p.add_argument("--us-trading", action="store_true", help="Apply US equity trading rules.")
    p.add_argument("--hk-trading", action="store_true", help="Apply Hong Kong equity trading rules.")
    p.add_argument("--baseline-symbol", type=str, default=None,
                   help="Optional benchmark symbol compared against every strategy (e.g. 000300.SH). "
                        "Its own data is still loaded per strategy by run_backtest.main() (one symbol, "
                        "not the universe) -- same as the old shell loop.")
    p.add_argument("--baseline-template", type=str, default="equal_weight")
    p.add_argument("--baseline-params", type=str, default=None)
    p.add_argument("--strategy-params", type=str, default=None,
                   help="JSON object merged into EVERY strategy's params (and into "
                        "research_strategy_spec.entry_data.parameters), exactly like run_backtest.py's "
                        "own --strategy-params. The old shell script's WEIGHTING_MODE / "
                        "ENABLE_ORTHOGONALIZATION / EXTRA_PARAMS env vars map onto this.")
    p.add_argument("--seed", type=int, default=None,
                   help="Master seed forwarded to run_backtest's per-fold SeedSequence derivation.")
    p.add_argument("--inner-n-jobs", type=parse_n_jobs_arg, default=1, metavar="N",
                   help="Fold-level workers INSIDE each strategy (forwarded to run_backtest.py's own "
                        "--n-jobs). Default 1 = no fold fan-out, so `--n-jobs N` workers never nest pools "
                        "(run_backtest's help warns that nesting oversubscribes the box). Set it to "
                        "'auto'/-1/0 only when the batch has FEWER strategies than cores (e.g. "
                        "`--n-jobs 1 --inner-n-jobs auto`): that is what the old shell loop effectively "
                        "did, and for 6 strategies it measured 52.9s vs 102.7s for --n-jobs 1 alone -- "
                        "but it loses to the strategy pool (26.0s at --n-jobs 6), which is why it is "
                        "off by default.")
    p.add_argument("--n-jobs", "-j", type=parse_n_jobs_arg, default=None, metavar="N",
                   help="Strategy-level worker processes. 'auto'/unset/-1/0 = auto: "
                        "min(os.cpu_count(), n_strategies). 1 = fully in-process serial batch "
                        "(still one universe load, still one interpreter). Fold-level parallelism "
                        "stays OFF inside a worker by default (nesting pools oversubscribes the box); "
                        "--inner-n-jobs opts in. "
                        f"MEASURED (6 blend strategies, 4 synthetic symbols, {N_JOBS_BENCH_SPAN}): "
                        "1 -> 102.7s, 2 -> 56.1s, 6 -> 26.0s, auto(=6) -> 26.8s (medians of 3, after "
                        "one discarded warm-up). Parallel therefore pays as soon as a single strategy "
                        f"costs more than ~{N_JOBS_PARALLEL_BREAK_EVEN}s: the 6-worker pool's total spin-up "
                        "is ~2s, and the CHEAPEST strategy measured here (1 fold) still costs ~5.5s. "
                        "`--n-jobs 1` is NOT a good way to save CPU on a small batch: it disables the "
                        "strategy pool AND the fold pool (see --inner-n-jobs), landing ~2x slower than the "
                        "old per-strategy shell loop (102.7s vs 52.9s measured for the same 6 strategies).")
    p.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR,
                   help=f"Aggregate reports go here; each strategy's own run_backtest artifacts go to "
                        f"<output-dir>/<strategy stem>/ (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--results-subdir", choices=[RESULTS_SUBDIR_MODE_STEM, RESULTS_SUBDIR_MODE_NAME],
                   default=RESULTS_SUBDIR_MODE_STEM,
                   help="Per-strategy subdirectory naming: 'stem' = strategy FILE stem (matches the old "
                        "shell loop, default), 'name' = the strategy's own name from its JSON.")
    p.add_argument("--cache-dir", type=str, default=None,
                   help=f"Shared OHLCV DuckDB cache directory (default: {shared_data_dir()})")
    p.add_argument("--no-plots", action="store_true", help="Forwarded to run_backtest (walkforward mode "
                                                           "draws no chart anyway).")
    p.add_argument("--capture-logs", action=argparse.BooleanOptionalAction, default=True,
                   help="Write each strategy's full console output to <results-dir>/run.log (default: on).")
    p.add_argument("--stream-strategy-logs", action="store_true",
                   help="Also echo strategy output to this console (interleaved across workers). Off by "
                        "default: the batch prints one START/OK/FAILED line per strategy instead.")
    p.add_argument("--top", type=int, default=10,
                   help="How many rows of the ranking matrix to print at the end (default 10; the CSV/JSON "
                        "always carry every strategy).")
    add_data_provider_cli_args(p, default_provider="synthetic")
    return p


# --------------------------------------------------------------------------- #
# Strategy list resolution
# --------------------------------------------------------------------------- #
_LIST_FILE_SUFFIXES = (".txt", ".list", ".lst", ".csv")


def _read_strategy_list_file(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _expand_strategy_token(token: str) -> list:
    """One `--strategies` token -> zero or more strategy file paths.

    A token may be a directory (all `*.json` directly inside, sorted), a
    `*.txt`/`*.list`/`*.csv` file (one path per line, `#` comments allowed), or a
    comma-separated list of paths. `.json` tokens are taken as-is.
    """
    token = token.strip()
    if not token:
        return []
    if "," in token and not os.path.exists(token):
        out = []
        for part in token.split(","):
            out.extend(_expand_strategy_token(part))
        return out
    if os.path.isdir(token):
        return sorted(glob.glob(os.path.join(token, "*.json")))
    if os.path.exists(token) and token.lower().endswith(_LIST_FILE_SUFFIXES):
        out = []
        for line in _read_strategy_list_file(token):
            out.extend(_expand_strategy_token(line))
        return out
    return [token]


def resolve_strategy_files(args) -> list:
    """Absolute, de-duplicated, ORDER-PRESERVING strategy file list.

    Globs are expanded here (not by the shell) so an empty expansion is a clear
    error instead of a literal path that fails 55 times.
    """
    found = []
    for token in args.strategies or []:
        found.extend(_expand_strategy_token(token))
    for pattern in args.strategies_glob or []:
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise ValueError(
                f"--strategies-glob {pattern!r} matched no files. Refusing to run an empty batch "
                f"(the old shell loop's `find ... | while read` would have silently run zero strategies)."
            )
        found.extend(matches)

    out, seen = [], set()
    for path in found:
        abspath = os.path.abspath(path)
        if abspath in seen:
            continue
        if not os.path.isfile(abspath):
            raise FileNotFoundError(f"Strategy file not found: {path}")
        seen.add(abspath)
        out.append(abspath)
    if not out:
        raise ValueError("No strategy files given. Pass --strategies and/or --strategies-glob.")
    return out


def _unique_stems(paths: list) -> dict:
    """abs path -> results subdirectory name. Two different files can share a
    stem (`a/blend_1.json` and `b/blend_1.json`): the second gets `__2`, `__3`,
    ... so nothing silently overwrites another strategy's results."""
    used, out = set(), {}
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        candidate, n = stem, 1
        while candidate in used:
            n += 1
            candidate = f"{stem}__{n}"
        used.add(candidate)
        out[path] = candidate
    return out


def _safe_dirname(name: str) -> str:
    """A strategy NAME becomes a directory name in `--results-subdir name` mode:
    keep it filesystem-safe (no separators, no traversal) rather than creating
    surprise nested paths from a name like 'Chan/Four State'."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._-")
    return cleaned or "strategy"


# --------------------------------------------------------------------------- #
# Per-strategy probing (parent side: warmup window, name, template)
# --------------------------------------------------------------------------- #
def _merged_strategy_params(strategy_def: dict, overrides: dict) -> tuple:
    """The `--strategy-params` merge, mirroring `run_backtest.main()` L971-981.

    Duplicated (not imported -- main() does it inline) because the parent needs
    each strategy's warmup window BEFORE loading the universe, and the warmup
    call takes the merged params. Kept in lockstep by
    `test_strategy_params_override_reaches_the_worker_path`, which byte-compares
    a `--strategy-params` batch run against a direct CLI run.
    """
    params = dict(strategy_def.get("params") or {})
    rs_spec = strategy_def.get("research_strategy_spec")
    if isinstance(rs_spec, dict):
        rs_spec = json.loads(json.dumps(rs_spec))  # deep copy: main() mutates entry_data.parameters
    if overrides:
        params.update(overrides)
        if isinstance(rs_spec, dict) and "entry_data" in rs_spec:
            rs_spec.setdefault("entry_data", {}).setdefault("parameters", {}).update(overrides)
    return params, rs_spec


def _fetch_start_for(args, warmup_bars: int) -> str:
    """Identical arithmetic to `run_backtest.main()`: the requested start padded
    by the template's warmup (bars -> calendar days, ~7/5 + 45 days of holiday
    buffer). Getting this wrong would silently change which bars exist, i.e.
    exactly the kind of drift this task must not introduce."""
    fetch_start = args.start
    if warmup_bars > 0 and args.start:
        warmup_calendar_days = int(round(warmup_bars * 7 / 5)) + 45
        fetch_start = (pd.Timestamp(args.start) - pd.Timedelta(days=warmup_calendar_days)).strftime("%Y-%m-%d")
    return fetch_start


def _probe_strategy(path: str, args, overrides: dict) -> dict:
    """Everything the parent must know about one strategy before the batch runs:
    its display name, template, and — critically — its own data window.

    A failure here is isolated: the returned dict carries `error` and the
    strategy is reported as failed without aborting the batch.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    probe = {"path": path, "stem": stem, "name": stem, "template_name": None,
             "fetch_start": None, "error": None}
    try:
        strategy_def = load_strategy_file(path)
        params, rs_spec = _merged_strategy_params(strategy_def, overrides)
        template_name = strategy_def["template_name"]
        name = (
            strategy_def.get("strategy_name")
            or strategy_def.get("name")
            or (rs_spec.get("entry_data", {}).get("name") if isinstance(rs_spec, dict) else None)
            or (rs_spec.get("strategy_key") if isinstance(rs_spec, dict) else None)
            or template_name
        )
        probe["template_name"] = template_name
        probe["name"] = str(name)
        template = get_template(
            template_name,
            strategy_def.get("pattern_spec"),
            rs_spec,
            strategy_def.get("composite_spec"),
            params,
            strategy_def.get("fundamental_spec"),
            strategy_def.get("bnn_spec"),
        )
        probe["fetch_start"] = _fetch_start_for(args, int(template.warmup_bars(params) or 0))
    except Exception as exc:  # noqa: BLE001 -- isolated, reported in batch_failed.csv
        probe["error"] = f"{type(exc).__name__}: {exc}"
        probe["traceback"] = traceback.format_exc()
        # A strategy whose JSON/spec cannot be resolved has no trustworthy name;
        # the FILE stem is the only identity an operator can act on.
        probe["name"] = stem
    return probe


# --------------------------------------------------------------------------- #
# Universe injection (the "load once" mechanism)
# --------------------------------------------------------------------------- #
def _trim_universe(universe: dict, start, end, require_nonempty: bool = True) -> dict:
    """Slice the shared universe to `[start, end]` — what a per-strategy
    `load_universe_with_banner(symbols, start, end, ...)` would have returned for
    a provider whose bars are date-addressed (the DuckDB cache / fuyao /
    yfinance). See the module docstring's caveat about purely generative
    providers such as `synthetic`, whose prices depend on the requested start."""
    start_ts = pd.Timestamp(start) if start else None
    end_ts = pd.Timestamp(end) if end else None
    out = {}
    for symbol, df in universe.items():
        if df is None or df.empty:
            continue
        index = df.index
        mask = np.ones(len(index), dtype=bool)
        if start_ts is not None:
            mask &= np.asarray(index >= start_ts)
        if end_ts is not None:
            mask &= np.asarray(index <= end_ts)
        sliced = df.loc[index[mask]]
        if not sliced.empty:
            out[symbol] = sliced
    if require_nonempty and not out:
        raise ValueError(
            f"No symbols could be loaded successfully in [{start}, {end}] from the batch's shared "
            f"universe; see warnings above."
        )
    return out


class _UniverseInjection:
    """Swap `run_backtest.load_universe_with_banner` for a closure returning the
    already-loaded universe, for the duration of one `main()` call.

    Only a request for EXACTLY the batch's symbol set (and interval) is served
    from memory; anything else (`--baseline-symbol`'s own single-symbol load)
    falls through to the real loader, so this can never feed the wrong data to
    the wrong call. `calls` is checked by the caller: a strategy that finished
    without ever hitting the injection means `run_backtest.main()` re-read the
    cache for its universe, i.e. the whole point of B2.3 was lost — that is
    reported as a failure, never silently accepted.
    """

    def __init__(self, universe: dict, symbols, interval: str):
        self.universe = universe
        self.symbol_set = frozenset(str(s) for s in symbols)
        self.interval = interval
        self.calls = 0
        self._original = None

    def __enter__(self):
        self._original = run_backtest.load_universe_with_banner
        run_backtest.load_universe_with_banner = self._injected
        return self

    def __exit__(self, exc_type, exc, tb):
        run_backtest.load_universe_with_banner = self._original
        return False

    def _injected(self, symbols, start, end, interval: str = "1d", **kwargs):
        if interval == self.interval and frozenset(str(s) for s in symbols) == self.symbol_set:
            self.calls += 1
            return _trim_universe(
                self.universe, start, end,
                require_nonempty=kwargs.get("require_nonempty", True),
            )
        return self._original(symbols, start, end, interval, **kwargs)


# --------------------------------------------------------------------------- #
# Worker side
# --------------------------------------------------------------------------- #
#: Published in the PARENT before the pool is forked; inherited (not pickled) by
#: every worker. Holds the shared universe, the strategy tasks, and the batch
#: args -- exactly the B2.2 payload contract, extended from folds to strategies.
_BATCH_STATE = None


class _Tee:
    """Write to several streams; a broken secondary stream never kills the run."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            try:
                stream.write(data)
            except Exception:  # pragma: no cover - closed/broken capture stream
                pass

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # pragma: no cover
                pass


def _inner_n_jobs_arg_value(value):
    """`--inner-n-jobs` -> the literal `run_backtest.py --n-jobs` token.

    `None` means AUTO, which `run_backtest.resolve_n_jobs` spells `-1`.
    """
    return -1 if value is None else int(value)


def _build_backtest_argv(args, task: dict) -> list:
    """The exact `run_backtest.py` command line for one strategy.

    Universe flags are forwarded verbatim so the worker resolves the SAME
    symbol list as the parent (which is what makes the injection's symbol-set
    match work), and fold fan-out is off by default (`--inner-n-jobs 1`) so a
    batch worker never nests pools. Raising `--inner-n-jobs` only overrides
    that default; it does not change what any strategy computes (run_backtest
    re-orders fold results by fold index before assembly).
    """
    argv = [
        "run_backtest.py",
        "--strategy-file", task["path"],
        "--mode", "walkforward",
        "--start", str(args.start),
        "--end", str(args.end),
        "--interval", str(args.interval),
        "--window-years", repr(float(args.window_years)),
        "--step-years", repr(float(args.step_years)),
        "--initial-capital", repr(float(args.initial_capital)),
        "--commission-pct", repr(float(args.commission_pct)),
        "--slippage-pct", repr(float(args.slippage_pct)),
        "--min-shares", str(int(args.min_shares)),
        "--min-weight-change", repr(float(args.min_weight_change)),
        "--data-provider", str(args.data_provider),
        "--results-dir", task["results_dir"],
        # Fold fan-out is OFF by default inside a worker (no nested pools); see
        # `--inner-n-jobs`. `None` == auto, which run_backtest spells `-1`.
        "--n-jobs", str(_inner_n_jobs_arg_value(args.inner_n_jobs)),
    ]
    if args.universe:
        argv += ["--universe", *[str(s) for s in args.universe]]
    if args.universe_file:
        argv += ["--universe-file", str(args.universe_file)]
    if args.universe_provider:
        argv += ["--universe-provider", str(args.universe_provider)]
    if args.universe_kwargs:
        argv += ["--universe-kwargs", str(args.universe_kwargs)]
    if args.strategy_params:
        argv += ["--strategy-params", str(args.strategy_params)]
    if args.seed is not None:
        argv += ["--seed", str(int(args.seed))]
    if args.china_trading:
        argv.append("--china-trading")
    if args.us_trading:
        argv.append("--us-trading")
    if args.hk_trading:
        argv.append("--hk-trading")
    if args.baseline_symbol:
        argv += ["--baseline-symbol", str(args.baseline_symbol)]
        argv += ["--baseline-template", str(args.baseline_template)]
        if args.baseline_params:
            argv += ["--baseline-params", str(args.baseline_params)]
    if args.no_plots:
        argv.append("--no-plots")
    if args.no_cache:
        argv.append("--no-cache")
    if args.cache_dir:
        argv += ["--cache-dir", str(args.cache_dir)]
    if args.data_dir:
        argv += ["--data-dir", str(args.data_dir)]
    return argv


def _read_json_or_none(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _run_one_strategy(task: dict, state: dict) -> dict:
    """Run one strategy's walk-forward through `run_backtest.main()` verbatim.

    Never raises: every failure (including `SystemExit` from a bad argv) is
    captured into the returned dict, which is what keeps one broken strategy
    from aborting a 55-strategy batch.
    """
    started = time.time()
    results_dir = task["results_dir"]
    outcome = {
        "strategy": task["name"],
        "stem": task["stem"],
        "path": task["path"],
        "template_name": task.get("template_name"),
        "results_dir": results_dir,
        "status": "failed",
        "error": None,
        "traceback": None,
        "summary": None,
        "elapsed_seconds": 0.0,
    }

    try:
        os.makedirs(results_dir, exist_ok=True)
    except OSError as exc:
        outcome["error"] = f"cannot create results dir {results_dir}: {exc}"
        outcome["elapsed_seconds"] = time.time() - started
        return outcome

    args = state["args"]
    argv = _build_backtest_argv(args, task)
    log_path = os.path.join(results_dir, "run.log")

    saved_argv = sys.argv
    saved_out, saved_err = sys.stdout, sys.stderr
    log_handle = None
    error = None
    tb = None
    injection = _UniverseInjection(state["universe"], state["symbols"], args.interval)
    try:
        if args.capture_logs:
            log_handle = open(log_path, "w", encoding="utf-8")
            target = _Tee(log_handle, saved_out) if args.stream_strategy_logs else log_handle
            err_target = _Tee(log_handle, saved_err) if args.stream_strategy_logs else log_handle
            sys.stdout, sys.stderr = target, err_target
        sys.argv = argv
        with injection:
            run_backtest.main()
        if injection.calls == 0:
            error = (
                "run_backtest.main() never used the batch's pre-loaded universe: it re-read the OHLCV "
                "cache for its own symbol set, so this run did NOT honour B2.3's single-load contract. "
                "Refusing to report numbers produced outside the sanctioned path."
            )
    except (KeyboardInterrupt,):
        raise
    except BaseException as exc:  # noqa: BLE001 -- includes SystemExit from argparse
        error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
    finally:
        sys.argv = saved_argv
        sys.stdout, sys.stderr = saved_out, saved_err
        if log_handle is not None:
            log_handle.close()

    if error is None:
        summary = _read_json_or_none(os.path.join(results_dir, "walkforward_summary.json"))
        if summary is None:
            error = ("run_backtest.main() returned without writing walkforward_summary.json "
                     f"({results_dir}) -- refusing to score a strategy with no result artifacts.")
        else:
            outcome["summary"] = summary
            outcome["status"] = "ok"
    if error is not None:
        outcome["error"] = error
        outcome["traceback"] = tb
        if tb:
            # Fold the traceback into the per-strategy log too: the log is the
            # only place an operator sees the full stack of a failed strategy.
            try:
                mode = "a" if log_handle is not None else "w"
                with open(log_path, mode, encoding="utf-8") as f:
                    f.write(f"\n=== BATCH: strategy failed ===\n{tb}\n")
            except OSError:  # pragma: no cover - log dir vanished
                pass
    outcome["elapsed_seconds"] = time.time() - started
    return outcome


def _batch_worker(position: int) -> dict:
    """Process-pool entry point: takes only the strategy's POSITION (the payload
    -- universe, task list, args -- lives in the fork-inherited module global)."""
    state = _BATCH_STATE
    if state is None:
        raise RuntimeError("batch worker started without a published batch state")
    return _run_one_strategy(state["tasks"][position], state)


def _report(outcome: dict, completed: int, total: int) -> None:
    """One progress line per FINISHED strategy (`completed` is a completion
    count, not the strategy's queue position -- parallel workers finish out of
    order, and pretending otherwise would make the log lie)."""
    summary = outcome.get("summary") or {}
    if outcome["status"] == "ok":
        print(
            f"[{completed}/{total} done] OK     {outcome['strategy']}  "
            f"Sharpe={_fmt(summary.get('mean_sharpe_ratio'), '.2f')}  "
            f"Calmar={_fmt(summary.get('mean_calmar_ratio'), '.2f')}  "
            f"MaxDD={_fmt_pct(summary.get('mean_max_drawdown'))}  "
            f"folds={summary.get('n_valid_folds')}/{summary.get('n_folds')}  "
            f"({outcome['elapsed_seconds']:.1f}s)",
            flush=True,
        )
    else:
        print(
            f"[{completed}/{total} done] FAILED {outcome['strategy']}: {outcome['error']} "
            f"({outcome['elapsed_seconds']:.1f}s)",
            flush=True,
        )


def _fmt(value, spec: str = ".3f") -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return format(f, spec) if np.isfinite(f) else "N/A"


def _fmt_pct(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return f"{f * 100:.1f}%" if np.isfinite(f) else "N/A"


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _run_serial(tasks, state) -> list:
    global _BATCH_STATE
    _BATCH_STATE = state
    results = []
    try:
        for position, task in enumerate(tasks):
            print(f"[{position + 1}/{len(tasks)}] START  {task['name']}", flush=True)
            outcome = _batch_worker(position)
            _report(outcome, position + 1, len(tasks))
            results.append(outcome)
    finally:
        _BATCH_STATE = None
    return results


def _run_parallel(tasks, n_jobs, state) -> list:
    """Fan the strategies out to a fork pool (the universe is inherited by COW,
    never pickled per task). Pool-level failures degrade to the serial loop with
    a loud WARNING instead of losing the batch."""
    global _BATCH_STATE

    try:
        mp_ctx = run_backtest._walkforward_pool_context()
    except run_backtest.WalkforwardPoolUnavailable as exc:
        message = (
            f"process pool unavailable ({exc}); falling back to the serial strategy loop. "
            f"Results are identical, only slower."
        )
        warnings.warn(message)
        print(f"WARNING: {message}", flush=True)
        return _run_serial(tasks, state)

    print(
        f"Parallel batch: dispatching {len(tasks)} strategies to {n_jobs} worker process(es); "
        f"the universe is forked (copy-on-write), not pickled, and the folds inside each worker "
        f"stay serial.",
        flush=True,
    )
    for position, task in enumerate(tasks):
        print(f"[{position + 1}/{len(tasks)}] QUEUED {task['name']}", flush=True)

    _BATCH_STATE = state
    results = {}
    broken = None
    executor = None
    futures = {}
    try:
        try:
            executor = ProcessPoolExecutor(max_workers=n_jobs, mp_context=mp_ctx)
        except (OSError, ImportError, PermissionError, ValueError, NotImplementedError) as exc:
            message = (
                f"cannot start a process pool ({type(exc).__name__}: {exc}); falling back to the "
                f"serial strategy loop."
            )
            warnings.warn(message)
            print(f"WARNING: {message}", flush=True)
            return _run_serial(tasks, state)

        futures = {executor.submit(_batch_worker, position): position
                   for position in range(len(tasks))}
        for future in as_completed(futures):
            position = futures[future]
            try:
                outcome = future.result()
            except BrokenProcessPool as exc:
                broken = exc
                break
            results[position] = outcome
            _report(outcome, len(results), len(tasks))
    finally:
        if broken is not None:
            for future in futures:
                future.cancel()
        if executor is not None:
            executor.shutdown(wait=broken is None, cancel_futures=True)
        _BATCH_STATE = None

    if broken is not None:
        message = (
            f"batch worker pool broke ({broken}); re-running the {len(tasks) - len(results)} "
            f"unfinished strateg(ies) serially in-process. Results are identical, only slower."
        )
        warnings.warn(message)
        print(f"WARNING: {message}", flush=True)
        remaining = [i for i in range(len(tasks)) if i not in results]
        _BATCH_STATE = state
        try:
            for position in remaining:
                print(f"[retry {position + 1}/{len(tasks)}] START  {tasks[position]['name']}", flush=True)
                outcome = _batch_worker(position)
                results[position] = outcome
                _report(outcome, position + 1, len(tasks))
        finally:
            _BATCH_STATE = None

    return [results[i] for i in range(len(tasks)) if i in results]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _ranking_frame(tasks, results) -> pd.DataFrame:
    rows = []
    for task, outcome in zip(tasks, results):
        summary = outcome.get("summary") or {}
        rows.append({
            "status": outcome["status"],
            "strategy": outcome["strategy"],
            "strategy_file": outcome["path"],
            "template_name": outcome.get("template_name"),
            "n_folds": summary.get("n_folds"),
            "n_valid_folds": summary.get("n_valid_folds"),
            "mean_sharpe_ratio": summary.get("mean_sharpe_ratio"),
            "mean_cagr": summary.get("mean_cagr"),
            "mean_max_drawdown": summary.get("mean_max_drawdown"),
            "mean_calmar_ratio": summary.get("mean_calmar_ratio"),
            "deflated_sharpe_ratio": summary.get("deflated_sharpe_ratio"),
            "fold_sharpe_std": summary.get("fold_sharpe_std"),
            "total_rebalances": None if summary.get("rolling_window_performance") is None else
                                int(sum(f.get("total_rebalances", 0) or 0
                                        for f in summary.get("rolling_window_performance") or [])),
            "elapsed_seconds": round(float(outcome["elapsed_seconds"]), 3),
            "error": outcome.get("error"),
        })
    frame = pd.DataFrame(rows, columns=[c for c in RANKING_COLUMNS if c != "rank"])
    for column in ("n_folds", "n_valid_folds", "mean_sharpe_ratio", "mean_cagr",
                   "mean_max_drawdown", "mean_calmar_ratio", "deflated_sharpe_ratio",
                   "fold_sharpe_std", "elapsed_seconds"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # Ranking rule (documented in the CSV's own `rank` column): a strategy is
    # ranked only if it finished AND produced a finite mean Sharpe. Sort = mean
    # Sharpe desc, then mean Calmar desc, then name asc. Everything else
    # (failed, all-NaN folds) is unranked and listed after, `ok` rows before
    # `failed` rows, each alphabetically — so a reader can always tell "no data"
    # from "ranked last".
    frame["_ranked"] = (frame["status"] == "ok") & np.isfinite(frame["mean_sharpe_ratio"])
    ranked = frame[frame["_ranked"]].sort_values(
        ["mean_sharpe_ratio", "mean_calmar_ratio", "strategy"], ascending=[False, False, True]
    )
    # Unranked: `ok`-but-all-NaN before `failed` (a strategy that ran and produced
    # nothing is more informative than one that never ran), each alphabetical.
    # (The key is built on the FILTERED frame: assigning a Series computed from
    # `frame` onto `frame[~mask]` would re-align on the full index and inject
    # phantom all-NaN rows.)
    unranked = frame[~frame["_ranked"]].copy()
    unranked["_status_order"] = (unranked["status"] != "ok").astype(int)
    unranked = unranked.sort_values(["_status_order", "strategy"], ascending=[True, True])
    ordered = pd.concat([ranked, unranked.drop(columns=["_status_order"])], ignore_index=True)
    ordered["rank"] = pd.array(
        [i + 1 for i in range(len(ranked))] + [None] * len(unranked), dtype="Int64"
    )
    return ordered[RANKING_COLUMNS]


def _folds_frame(results) -> pd.DataFrame:
    frames = []
    for outcome in results:
        summary = outcome.get("summary") or {}
        for fold_index, fold in enumerate(summary.get("rolling_window_performance") or [], 1):
            frames.append({
                "strategy": outcome["strategy"],
                "template_name": outcome.get("template_name"),
                "fold": fold_index,
                "start_date": fold.get("start_date"),
                "end_date": fold.get("end_date"),
                "sharpe_ratio": fold.get("sharpe_ratio"),
                "cagr": fold.get("cagr"),
                "max_drawdown": fold.get("max_drawdown"),
                "calmar_ratio": fold.get("calmar_ratio"),
                "win_rate": fold.get("win_rate"),
                "profit_factor": fold.get("profit_factor"),
                "total_turnover": fold.get("total_turnover"),
                "total_rebalances": fold.get("total_rebalances"),
            })
    return pd.DataFrame(frames, columns=FOLD_COLUMNS)


def _write_aggregates(output_dir, tasks, results, ranking, folds, args, meta) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    ranking_path = os.path.join(output_dir, "batch_ranking.csv")
    ranking.to_csv(ranking_path, index=False)
    folds_path = os.path.join(output_dir, "batch_folds.csv")
    folds.to_csv(folds_path, index=False)

    failed = [
        {"strategy": outcome["strategy"], "strategy_file": outcome["path"], "error": outcome.get("error")}
        for outcome in results if outcome["status"] != "ok"
    ]
    failed_path = os.path.join(output_dir, "batch_failed.csv")
    pd.DataFrame(failed, columns=FAILED_COLUMNS).to_csv(failed_path, index=False)

    payload = {
        "task": "B2.3",
        "description": "Global walk-forward ranking matrix over a batch of strategies, "
                       "one interpreter and one universe load.",
        "generated_at": pd.Timestamp.now().isoformat(),
        "config": {
            "strategies": [task["path"] for task in tasks],
            "universe": list(meta["symbols"]),
            "universe_size": len(meta["symbols"]),
            "universe_load_start": meta["load_start"],
            "start": args.start,
            "end": args.end,
            "interval": args.interval,
            "window_years": args.window_years,
            "step_years": args.step_years,
            "data_provider": args.data_provider,
            "china_trading": bool(args.china_trading),
            "us_trading": bool(args.us_trading),
            "hk_trading": bool(args.hk_trading),
            "min_shares": args.min_shares,
            "baseline_symbol": args.baseline_symbol,
            "strategy_params": args.strategy_params,
            "seed": args.seed,
            "n_jobs": meta["n_jobs"],
            "output_dir": os.path.abspath(output_dir),
        },
        "ranking_rule": (
            "ranked rows = status=='ok' AND finite mean_sharpe_ratio, sorted by "
            "mean_sharpe_ratio DESC, then mean_calmar_ratio DESC, then strategy ASC; "
            "all other rows are unranked, 'ok' before 'failed', each alphabetical. "
            "Means are taken over each strategy's own walk-forward folds (run_backtest.py's "
            "convention: NaN folds excluded)."
        ),
        "n_strategies": len(tasks),
        "n_ok": sum(1 for outcome in results if outcome["status"] == "ok"),
        "n_failed": len(failed),
        "elapsed_seconds": round(meta["elapsed_seconds"], 3),
        "ranking": json.loads(ranking.to_json(orient="records")),
        "folds": json.loads(folds.to_json(orient="records")),
        "failed": failed,
        "strategy_summaries": {
            outcome["strategy"]: outcome["summary"]
            for outcome in results if outcome["status"] == "ok" and outcome.get("summary") is not None
        },
        "strategy_errors": {
            outcome["strategy"]: {"error": outcome.get("error"), "traceback": outcome.get("traceback")}
            for outcome in results if outcome["status"] != "ok"
        },
    }
    summary_path = os.path.join(output_dir, "batch_summary.json")
    write_json_report(payload, summary_path)
    return {
        "ranking": ranking_path,
        "folds": folds_path,
        "failed": failed_path,
        "summary": summary_path,
    }


def _print_ranking(ranking: pd.DataFrame, top: int) -> None:
    if ranking.empty:
        print("\n(no strategies were run)")
        return
    shown = ranking.head(max(1, int(top))) if top else ranking
    print("\n=== Global Strategy Ranking (mean walk-forward folds) ===")
    header = f"{'#':>3}  {'strategy':<38} {'Sharpe':>7} {'Calmar':>7} {'MaxDD':>8} {'CAGR':>8} {'folds':>7}"
    print(header)
    print("-" * len(header))
    for _, row in shown.iterrows():
        rank = "" if pd.isna(row["rank"]) else str(int(row["rank"]))
        folds = "N/A"
        if pd.notna(row["n_valid_folds"]) and pd.notna(row["n_folds"]):
            folds = f"{int(row['n_valid_folds'])}/{int(row['n_folds'])}"
        status = "" if row["status"] == "ok" else " [FAILED]"
        print(
            f"{rank:>3}  {str(row['strategy'])[:38]:<38} {_fmt(row['mean_sharpe_ratio'], '.2f'):>7} "
            f"{_fmt(row['mean_calmar_ratio'], '.2f'):>7} {_fmt_pct(row['mean_max_drawdown']):>8} "
            f"{_fmt_pct(row['mean_cagr']):>8} {folds:>7}{status}"
        )
    if top and len(ranking) > len(shown):
        print(f"... {len(ranking) - len(shown)} more row(s) in batch_ranking.csv")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_batch(args) -> dict:
    """Run the whole batch. Returns the aggregate payload written to
    `batch_summary.json` (plus the written paths under `"paths"`)."""
    started = time.time()
    if sum([bool(args.china_trading), bool(args.us_trading), bool(args.hk_trading)]) > 1:
        raise ValueError("Only one of --china-trading, --us-trading, --hk-trading may be enabled.")

    overrides = {}
    if args.strategy_params:
        try:
            overrides = json.loads(args.strategy_params)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse --strategy-params JSON string: {exc}") from exc
        if not isinstance(overrides, dict):
            raise ValueError(
                f"--strategy-params must be a JSON object, got {type(overrides).__name__}."
            )

    strategy_files = resolve_strategy_files(args)
    subdirs = _unique_stems(strategy_files)
    output_dir = os.path.abspath(args.output_dir)

    print(f"=== Batch Walk-Forward Backtest: {len(strategy_files)} strategy file(s) ===")
    print(f"Output dir:      {output_dir}")
    print(f"Data provider:   {args.data_provider}")
    print(f"Window / step:   {args.window_years}y / {args.step_years}y")
    print(f"Range:           {args.start} -> {args.end}")

    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError("No universe symbols provided or resolved. Pass --universe, --universe-file, "
                         "or --universe-provider.")
    print(f"Universe:        {len(universe_symbols)} symbol(s)")

    probes = [_probe_strategy(path, args, overrides) for path in strategy_files]

    tasks = []
    for probe in probes:
        subdir = subdirs[probe["path"]] if args.results_subdir == RESULTS_SUBDIR_MODE_STEM else _safe_dirname(probe["name"])
        tasks.append(dict(probe, results_dir=os.path.join(output_dir, subdir)))

    runnable = [task for task in tasks if task["error"] is None]
    if runnable:
        load_start = min(task["fetch_start"] for task in runnable)
    else:
        load_start = args.start
        warnings.warn("Every strategy failed the pre-flight probe; no backtest will run.")
        print("WARNING: every strategy failed the pre-flight probe; nothing to run.", flush=True)

    cache_dir = args.cache_dir or shared_data_dir()
    if runnable:
        if any(task["fetch_start"] != load_start for task in runnable):
            print(
                f"NOTE: strategies need different warmup windows; loading the union from {load_start} "
                f"ONCE and trimming per strategy to its own window (this is the whole point of B2.3)."
            )
        universe = load_universe_with_banner(
            universe_symbols, load_start, args.end, args.interval,
            use_cache=not args.no_cache, cache_dir=cache_dir,
            data_kwargs=build_data_kwargs(args), require_nonempty=True,
            expected_last_session=run_backtest.backtest_expected_last_session(),
        )
        print(f"Universe loaded ONCE and shared with every strategy (no per-strategy cache query).")
    else:
        universe = {}

    n_jobs = run_backtest.resolve_n_jobs(args.n_jobs, len(runnable))
    print(f"Workers:         {n_jobs} (strategy-level; folds stay serial inside a worker "
          f"unless --inner-n-jobs says otherwise)\n")

    state = {
        "args": args,
        "universe": universe,
        "symbols": list(universe_symbols),
        "tasks": tasks,
    }

    # Probe failures never enter the pool: they already carry their reason.
    positions = [i for i, task in enumerate(tasks) if task["error"] is None]
    if positions:
        subset = [tasks[i] for i in positions]
        if n_jobs > 1 and len(subset) > 1:
            outcomes = _run_parallel(subset, n_jobs, dict(state, tasks=subset))
        else:
            outcomes = _run_serial(subset, dict(state, tasks=subset))
    else:
        outcomes = []

    results = []
    outcome_iter = iter(outcomes)
    for task in tasks:
        if task["error"] is None:
            results.append(next(outcome_iter))
        else:
            results.append({
                "strategy": task["name"],
                "stem": task["stem"],
                "path": task["path"],
                "template_name": task.get("template_name"),
                "results_dir": task["results_dir"],
                "status": "failed",
                "error": f"pre-flight probe failed: {task['error']}",
                "traceback": task.get("traceback"),
                "summary": None,
                "elapsed_seconds": 0.0,
            })
            print(f"FAILED {task['name']}: pre-flight probe failed: {task['error']}", flush=True)

    ranking = _ranking_frame(tasks, results)
    folds = _folds_frame(results)
    elapsed = time.time() - started
    meta = {
        "symbols": universe_symbols,
        "load_start": load_start,
        "n_jobs": n_jobs,
        "elapsed_seconds": elapsed,
    }
    paths = _write_aggregates(output_dir, tasks, results, ranking, folds, args, meta)

    _print_ranking(ranking, args.top)
    n_ok = sum(1 for outcome in results if outcome["status"] == "ok")
    print(
        f"\n{n_ok}/{len(results)} strateg(ies) OK, {len(results) - n_ok} failed "
        f"in {elapsed:.1f}s with {n_jobs} worker(s)."
    )
    print(f"Ranking matrix: {paths['ranking']}")
    print(f"Fold detail:    {paths['folds']}")
    print(f"Failed list:    {paths['failed']}")
    print(f"JSON summary:   {paths['summary']}")
    return {
        "n_strategies": len(tasks), "n_ok": n_ok, "n_failed": len(results) - n_ok,
        "elapsed_seconds": elapsed, "paths": paths, "ranking": ranking,
    }


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    return run_batch(args)


if __name__ == "__main__":
    main()

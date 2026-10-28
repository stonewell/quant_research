#!/usr/bin/env python
"""CLI entry point: evaluate a generated allocation strategy on a basket of assets.

Loads a `strategy.json` file exported by the strategy_generator and evaluates
those fixed rules on a new basket of assets.

Modes:
- standard: Evaluates the strategy over the full date range.
- walkforward: Evaluates the fixed strategy parameters over rolling time windows
               to measure consistency (no re-optimization).

Example:
    python run_backtest.py --strategy-file ../pipeline/strategy_generator/results/strategy.json --universe SPY QQQ AAPL --mode standard
"""

import argparse
import copy
import datetime
import enum
import hashlib
import io
import json
import multiprocessing
import os
import pathlib
import random
import re
import subprocess
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

# Add the repo root to sys.path to allow importing from common
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
REPO_ROOT = _REPO_ROOT

from common.allocation_backtester import REBALANCE_REPORT_COLUMNS, run_allocation_backtest
from common.allocation_search import optimize_template
from common.allocation_templates import ALLOCATION_TEMPLATES
from common.duckdb_cache import is_synthetic_provider
from common.cli_utils import (
    add_data_provider_cli_args,
    add_output_dir_override_args,
    bootstrap_project_paths,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)

# research_strategy and fundamental_screener live under pipeline/,
# bnn_forecaster under ml/ -- add both group directories so those projects' bare
# `import research_strategy...`-style modules keep resolving unchanged.
bootstrap_project_paths(_REPO_ROOT, __file__)
from common.metrics import alpha_beta, deflated_sharpe_ratio, information_ratio, tracking_error
from common import plotting
from common.reporting import (
    format_backtest_metrics_summary,
    format_rebalance_trades_preview,
    format_walkforward_performance_table,
    write_json_report,
)
from common.strategy_spec import get_template, load_strategy_file
from common.universe import add_universe_cli_args, resolve_universe_from_args

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


# --------------------------------------------------------------------------- #
# (L2.3, design §11) BACKTEST FRESHNESS GUARD -- strengthened by `L2.5`
# --------------------------------------------------------------------------- #
#: The one legal `expected_last_session` value on the backtest side of the boundary.
_LEGAL_BACKTEST_SESSION = None


class BacktestSessionLeakError(RuntimeError):
    """A non-None `expected_last_session` reached a backtest data load.

    Subclasses `RuntimeError` so existing callers/tests that catch the historical
    type keep working, while giving the failure a name an operator can grep for.
    """


def backtest_expected_last_session(value=_LEGAL_BACKTEST_SESSION):
    """Pass `expected_last_session=None` into the cache from a BACKTEST, or fail.

    The whole market-clock feature exists because live runs must demand a specific
    session; a backtest must never do so, otherwise its results become a function of
    the wall clock (today's date decides whether a bar is "covered" -> a re-fetch ->
    potentially different data and different numbers for the same command). There is
    therefore exactly one legal value on this side of the boundary: `None`.

    (`L2.5`) The rejection is on the VALUE, so **any** non-None argument fails --
    `0`, `""`, `False`, a `datetime.date`, a `pd.Timestamp` or a wall-clock-derived
    string are all a real session request in disguise. There is deliberately no
    truthiness/type check that could let a falsey non-None value through.

    Call sites use `expected_last_session=backtest_expected_last_session()`, so any
    future edit that tries to thread a real session in here raises immediately instead
    of silently making backtests non-reproducible.
    `backtester/tests/test_backtest_freshness_guard.py` asserts every load call in
    this file carries that marker (with NO argument), and
    `pipeline/tests/test_live_market_clock_wiring.py` pins the static
    "no live-clock call on the backtest side" guardrail.
    """
    if value is not _LEGAL_BACKTEST_SESSION:  # identity, not equality: `0 == False`
        raise BacktestSessionLeakError(
            "Backtests must pass expected_last_session=None (design §11): a real session makes "
            f"cache coverage, and therefore the backtest result, depend on the wall clock (got {value!r})."
        )
    return _LEGAL_BACKTEST_SESSION


def normalize_position_lifecycle_flag(args):
    """Resolve the `--enable-position-lifecycle` / `--no-position-lifecycle` pair into one bool.

    `--enable-position-lifecycle` parses to `True` or `None` (None = not typed),
    `--no-position-lifecycle` parses to its own `no_position_lifecycle` flag.
    The effective value is OFF only when the negative flag was supplied, so the
    off switch is the single, order-independent way to disable the layer:
    `--no-` beats `--enable-` no matter which came first on the command line.

    Idempotent and tolerant of namespaces that carry neither attribute (the
    tests' `MockArgs`, hand-built namespaces): those keep the documented ON
    default, which is what the pre-F1 `default=True` switch silently enforced.
    """
    disabled = bool(getattr(args, "no_position_lifecycle", False))
    requested = getattr(args, "enable_position_lifecycle", None)
    args.enable_position_lifecycle = False if disabled else (True if requested is None else bool(requested))
    return args


class _PositionLifecycleArgumentParser(argparse.ArgumentParser):
    """`argparse.ArgumentParser` that normalizes the lifecycle pair after parsing.

    The normalization lives in the parser (rather than in `main()`) so EVERY
    caller that goes through `build_arg_parser().parse_args(...)` -- `main`,
    the tests, `scripts/run_batch_backtest.py` -- observes the same resolved
    `args.enable_position_lifecycle` bool, and no call site can inherit a
    `None` that reads as OFF by accident.
    """

    def parse_args(self, args=None, namespace=None):
        return normalize_position_lifecycle_flag(super().parse_args(args, namespace))


def _normalise_day_keys(values) -> set:
    """Reduce a date-like container to a set of `YYYY-MM-DD` day keys.

    WHY THIS EXISTS (regression, fixed after F4): the two sides of the
    "which lifecycle records fall inside the eval window?" comparison were
    formatted DIFFERENTLY. `common/position_lifecycle.py` stamps every decision
    record with `rec["date"] = str(t)` where `t` is a `Timestamp`, i.e.
    `'2021-12-06 00:00:00'`, while the eval index was stringified verbatim via
    `set(eval_index.astype(str))` -> `{'2021-12-06'}`. `str(r["date"]) in
    eval_dates_set` therefore matched NOTHING (except on an index that happened
    to carry a time component), so `eval_records` was ALWAYS `[]`.

    That emptiness was invisible while `run_allocation_backtest` treated "empty
    list" as "caller supplied records" (it passed the raw, unfiltered weights
    through). Once the F4 fix made empty-list mean "no records supplied", the
    same folds started RE-SIMULATING already-filtered, pre-buffer-warmed weights
    -- a second `guard_latency_bars` confirmation pass that silently cleared the
    fold's seeded day-0 row to 0.0 and made `walkforward_lifecycle_records.csv`
    permanently empty. Normalising BOTH sides to the day granularity (one
    function, used by both call sites -- never two copies) removes the format
    coupling entirely.

    Robust to the three input shapes the callers actually pass: a
    `pd.DatetimeIndex` (the normal case), an index of strings / `date` /
    `datetime` objects (a template-built or CSV-round-tripped calendar), and a
    plain list/ndarray of any of those. Every supported spelling starts with
    `YYYY-MM-DD`, so the textual fallback below is exact to the day.
    """
    if values is None:
        return set()
    try:
        return set(pd.DatetimeIndex(list(values)).strftime("%Y-%m-%d"))
    except (TypeError, ValueError, OverflowError):
        return {str(v)[:10] for v in values}


def _lifecycle_records_in_window(records, eval_index) -> list:
    """Filter a lifecycle decision log to the bars covered by `eval_index`.

    Both sides are day-normalised through :func:`_normalise_day_keys`, so the
    `'2021-12-06 00:00:00'` record stamp and the `'2021-12-06'` eval bar agree.
    A record with no `date` key is kept (fail-open: dropping it would make the
    caller hand an EMPTY list to `run_allocation_backtest`, which now means
    "re-simulate" -- the exact double-filtering bug this fixes).
    """
    eval_dates_set = _normalise_day_keys(eval_index)
    out = []
    for rec in records or []:
        stamp = rec.get("date")
        if stamp is None or str(stamp)[:10] in eval_dates_set:
            out.append(rec)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    p = _PositionLifecycleArgumentParser(description="Standalone Basket Allocation Backtester")
    p.add_argument("--strategy-file", required=True, help="Path to strategy.json file exported by strategy_generator")
    add_universe_cli_args(p)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--mode", choices=["standard", "walkforward"], default="standard")
    p.add_argument("--window-years", type=float, default=1.0, help="Size of the rolling window in walkforward mode")
    p.add_argument("--step-years", type=float, default=0.5, help="Step size between rolling windows in walkforward mode")
    p.add_argument("--initial-capital", type=float, default=100_000.0)
    p.add_argument("--commission-pct", type=float, default=0.0005)
    p.add_argument("--slippage-pct", type=float, default=0.0005)
    p.add_argument("--baseline-symbol", type=str, default=None,
                   help="Optional single reference symbol (e.g. SPY) to compare the strategy against. Off by default.")
    p.add_argument("--baseline-template", type=str, default="equal_weight",
                   choices=[cls.name for cls in ALLOCATION_TEMPLATES],
                   help="Static allocation template used to turn --baseline-symbol into a baseline equity curve (default: equal_weight)")
    p.add_argument("--baseline-params", type=str, default=None,
                   help="JSON object string of params for --baseline-template (default: the template's first param_grid combination)")
    p.add_argument("--strategy-params", type=str, default=None,
                   help="JSON object string of params to merge/override in the loaded strategy.json")
    p.add_argument("--optimize", action="store_true",
        help="Grid-search the loaded strategy's template.param_grid on THIS universe (scored via "
             "the same --mode you selected) and Equivalent-Random-Search-validate the winner before "
             "running the final backtest. If the winner fails ERS validation, falls back to the "
             "strategy.json's ORIGINAL params (never silently produces no output) -- see "
             "results/optimize_report.json either way.")
    p.add_argument("--n-random-search", type=int, default=200)
    p.add_argument("--ers-percentile-threshold", type=float, default=0.90)
    p.add_argument("--min-rebalances-for-trust", type=int, default=4)
    p.add_argument("--min-shares", type=int, default=1,
                   help="Minimum amount of shares to trade each time (default: 1). Fractional share trading is not allowed.")
    p.add_argument("--min-weight-change", type=float, default=0.0,
                   help="Minimum weight change threshold (e.g. 0.02) to trigger a position rebalance, suppressing passive drift churn.")
    p.add_argument("--execution-price", type=str, default="close",
                   choices=["close", "next_open", "next_vwap", "intraday_twap"],
                   help="WHEN a rebalance signal is filled (task B3.1). 'close' (default) fills the "
                        "signal dated T at T's close -- the pre-B3.1 engine, bit-for-bit. 'next_open' "
                        "queues it at T's close and fills at T+1's open, so an overnight gap hits the "
                        "strategy and the trade is booked on T+1 (one-word limit boards and suspended "
                        "bars DEFER the order to the next tradable session; every deferral is written "
                        "to the deferral_report). 'next_vwap' does the same at the execution day's "
                        "amount/volume VWAP, and 'intraday_twap' at the mean of its 5m typical prices. "
                        "The last two need data this CLI does not load (a VWAP/Amount column, and 5m "
                        "bars respectively) and fail with an explicit ValueError rather than silently "
                        "degrading to another price.")
    p.add_argument("--market-impact", action="store_true",
                   help="(B3.2) Charge an Almgren-Chriss SQUARE-ROOT temporary market impact on every "
                        "fill: cost = notional * sigma * gamma * sqrt(shares / ADV20), where sigma is the "
                        "symbol's daily return vol over the past 20 sessions and ADV20 the mean volume "
                        "over the past 20 sessions (both strictly prior to the fill, so no look-ahead). "
                        "Charged to CASH on top of --commission/--slippage (a size-dependent execution "
                        "concession, not a duplicate of the flat bps terms) and reported in the "
                        "`slippage` column plus a `total_market_impact` total. OFF by default, which is the "
                        "pre-B3.2 engine bit-for-bit.")
    p.add_argument("--market-impact-gamma", type=float, default=1.0,
                   help="(B3.2) Dimensionless square-root impact prefactor (default: 1.0, the conservative "
                        "end of the Toth et al. 2011 / Almgren et al. 2005 range). Only used with "
                        "--market-impact. 0.0 disables the charge while keeping the model wired.")
    p.add_argument("--max-volume-participation", type=float, default=None, metavar="PCT",
                   help="(B3.2) Hard liquidity cap: no symbol may trade more than volume_today * PCT shares "
                        "in one session (e.g. 0.10). The unfilled excess is DEFERRED to the next tradable "
                        "session through the same order queue --execution-price uses, and every capped "
                        "attempt is written to deferral_report with reason 'volume_participation_cap'. "
                        "Default: unset = no cap (bit-identical to the pre-B3.2 engine).")
    p.add_argument("--cash-sweep-rate", type=float, default=None, metavar="ANNUAL",
                   help="(B3.3) Annualized repo/逆回购 rate credited on the UNINVESTED cash balance, "
                        "as a FRACTION of 1 (0.02 = 2%%/yr -- not percent). Accrues on ACTUAL CALENDAR "
                        "DAYS / 365, so a Friday balance earns three days of carry by Monday and a "
                        "holiday week earns its full week. Settles daily inside the normal loop as a "
                        "credit to cash (reported in cash_carry_report / total_cash_interest); it is "
                        "never charged on a cash SUBSTITUTE's notional (BIL/511880/GC001 earn their "
                        "carry through their own price series). Default: unset = off, which is the "
                        "pre-B3.3 engine bit-for-bit.")
    p.add_argument("--borrow-fee-annual", type=float, default=None, metavar="ANNUAL",
                   help="(B3.4) Annualized securities-borrow fee charged on the SHORT book's market "
                        "value (marked at the prior close), as a FRACTION of 1 (0.045 = 4.5%%/yr). "
                        "Deducted from cash daily on the opening (overnight) position, so a short "
                        "opened at T's close first pays at T+1 and the cover day still pays for the "
                        "night held; reported in cash_carry_report / total_borrow_fees. This is a "
                        "FINANCING cost, not a fill: rebalance_report (commission/slippage/stamp/"
                        "impact) is unchanged by it. Default: unset = off (the pre-B3.4 engine "
                        "bit-for-bit), NOT the task table's 4.5%% -- pass 0.045 to enable it.")
    # Tri-state pair, NOT a `store_true(default=True)` dead switch (F1): with
    # `store_true` + `default=True` the flag was a constant -- `args.` was True
    # whether or not the operator typed it, and only `--no-position-lifecycle`
    # could ever move it. The evidence that it was dead: no code could tell
    # "explicitly asked for ON" from "said nothing", so `run_manifest.json`'s
    # provenance recorded the same value for both.
    # Now: default is None ("the operator said nothing"), normalized to True
    # right after parsing by `normalize_position_lifecycle_flag` (see
    # `_PositionLifecycleArgumentParser`). The negative flag uses its OWN dest
    # so `--no-` wins even when `--enable-` is passed after it -- an argparse
    # shared-dest pair would let argument ORDER decide, which is not a
    # documented contract anywhere.
    p.add_argument("--enable-position-lifecycle",
                   dest="enable_position_lifecycle",
                   action="store_true",
                   default=None,
                   help="Explicitly request the Unified Position Lifecycle State Machine (UPLSM) in "
                        "the backtest: entry confirmation latency, T+1 tenure locks, cooldowns and "
                        "stop-loss exits. Already the DEFAULT -- the flag exists only to make the "
                        "choice explicit in run_manifest.json's cli_args; the only way to turn the "
                        "layer off is --no-position-lifecycle / --disable-position-lifecycle.")
    p.add_argument("--no-position-lifecycle", "--disable-position-lifecycle",
                   dest="no_position_lifecycle",
                   action="store_true",
                   default=False,
                   help="Disable the Unified Position Lifecycle State Machine in the backtest "
                        "(reverts to unconstrained raw model targets). The ONLY off switch, and it "
                        "wins even if --enable-position-lifecycle is also supplied.")
    p.add_argument("--lifecycle-guard-latency-bars",
                   type=int, default=None, dest="lifecycle_guard_latency_bars",
                   help="UPLSM entry confirmation latency bars (default: 2).")
    p.add_argument("--lifecycle-min-holding-bars",
                   type=int, default=None, dest="lifecycle_min_holding_bars",
                   help="UPLSM minimum holding tenure bars before exit/trim (default: 2).")
    p.add_argument("--lifecycle-normal-cooldown-bars",
                   type=int, default=None, dest="lifecycle_normal_cooldown_bars",
                   help="UPLSM cooldown bars after voluntary exit (default: 2, the value the live runner deploys).")
    p.add_argument("--lifecycle-stop-cooldown-bars",
                   type=int, default=None, dest="lifecycle_stop_cooldown_bars",
                   help="UPLSM cooldown bars after stop-loss liquidation (default: 5).")
    p.add_argument("--lifecycle-inertia-threshold",
                   type=float, default=None, dest="lifecycle_inertia_threshold",
                   help="UPLSM delta-weight inertia threshold to filter micro-rebalances (default: 0.05).")
    p.add_argument("--lifecycle-inertia-relative-ratio",
                   type=float, default=None, dest="lifecycle_inertia_relative_ratio",
                   help="UPLSM proportional hurdle ratio on trim-side inertia (default: 0.0 = disabled, "
                        "the value the live runner deploys).")
    p.add_argument("--lifecycle-min-entry-threshold",
                   type=float, default=None, dest="lifecycle_min_entry_threshold",
                   help="UPLSM minimum initial entry allocation weight threshold (default: None).")
    p.add_argument("--lifecycle-hard-stop-loss-pct",
                   type=float, default=None, dest="lifecycle_hard_stop_loss_pct",
                   help="UPLSM hard stop-loss drawdown threshold from entry price (default: 0.08).")
    p.add_argument("--lifecycle-atr-multiplier",
                   type=float, default=None, dest="lifecycle_atr_multiplier",
                   help="UPLSM ATR trailing stop multiplier (default: 4.5).")
    p.add_argument("--lifecycle-volume-surge-multiplier",
                   type=float, default=None, dest="lifecycle_volume_surge_multiplier",
                   help="UPLSM volume breakout multiplier for entry fast path (default: 1.5).")
    p.add_argument("--lifecycle-breadth-thrust-threshold",
                   type=float, default=None, dest="lifecycle_breadth_thrust_threshold",
                   help="UPLSM market breadth thrust threshold for entry fast path (default: 0.60).")
    p.add_argument("--lifecycle-enable-entry-fast-path",
                   dest="lifecycle_enable_entry_fast_path", action="store_true", default=None,
                   help="Enable UPLSM volume/breadth entry fast path.")
    p.add_argument("--no-lifecycle-entry-fast-path",
                   dest="lifecycle_enable_entry_fast_path", action="store_false",
                   help="Disable UPLSM entry fast path.")
    p.add_argument("--lifecycle-enable-exit-fast-path",
                   dest="lifecycle_enable_exit_fast_path", action="store_true", default=None,
                   help="Enable UPLSM emergency deleverage exit fast path.")
    p.add_argument("--no-lifecycle-exit-fast-path",
                   dest="lifecycle_enable_exit_fast_path", action="store_false",
                   help="Disable UPLSM exit fast path.")
    p.add_argument("--no-lifecycle-compare",
                   dest="lifecycle_compare", action="store_false", default=True,
                   help="Skip lifecycle A/B comparison arm (lifecycle_ab_comparison.csv).")
    p.add_argument("--china-trading", action="store_true",
                   help="Apply China A-share trading rules (price limit up/down blocks, T+1 settlement, 100-share minimum round lots, 5 bps sell stamp duty).")
    p.add_argument("--us-trading", action="store_true",
                   help="Apply US equity market trading rules (1-share lots, SEC Section 31 sell fee, T+0 margin trading).")
    p.add_argument("--hk-trading", action="store_true",
                   help="Apply Hong Kong equity market trading rules (board lot sizing, 0.1085%% dual-sided stamp duty/levies, T+0 trading).")
    add_data_provider_cli_args(p)
    add_output_dir_override_args(p, RESULTS_DIR, DATA_DIR, "equity/weights/report CSVs")
    p.add_argument("--no-plots", action="store_true",
                   help="Skip the equity-curve chart normally produced in --mode standard (charts are ON by default).")
    # (B2.2) Window-level parallelism for --mode walkforward only (standard mode is
    # one monolithic backtest call, nothing to fan out).
    p.add_argument("--n-jobs", "-j", type=int, default=None, metavar="N",
                   help="Worker processes for the rolling folds of --mode walkforward. "
                        "Default (unset/None) = auto: min(os.cpu_count(), n_folds). "
                        "-1 (as well as 0) = same auto rule, spelled explicitly. "
                        "1 = the historical fully serial in-process loop. "
                        "If a process pool cannot be created here (e.g. a platform whose only "
                        "start method is 'spawn', or a container that forbids fork), the run "
                        "falls back to that serial loop with a WARNING instead of failing. "
                        "Note --optimize: the fold fan-out then runs once per candidate; pass "
                        "-j 1 there if the search layer (common/allocation_search.py) already "
                        "fans out on its own, to avoid nesting pools.")
    p.add_argument("--seed", type=int, default=None,
                   help="Optional master seed for walkforward folds. Each fold derives its own "
                        "independent seed via numpy SeedSequence(entropy=--seed, "
                        "spawn_key=(fold_index,)), so the same --seed gives identical results "
                        "whatever --n-jobs is, while different folds never share a random stream. "
                        "Unset (default) leaves the global RNG untouched -- the pre-B2.2 behavior.")
    return p


def _align_universe(universe: dict) -> dict:
    """Walk-forward's fold boundaries are bar-position-based, so every
    symbol must share the same trading calendar -- trim to the intersection
    of all symbols' dates (an inner join). Warns (naming the culprit) when
    that intersection ends materially earlier than the latest date any
    universe symbol actually reaches -- otherwise a single short-history
    symbol silently shrinks the whole walk-forward range with no visible
    signal that anything was cut short."""
    common_index = None
    for df in universe.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    if universe and common_index is not None and len(common_index) > 0:
        aligned_end = common_index[-1]
        overall_latest = max(df.index[-1] for df in universe.values() if len(df))
        if overall_latest - aligned_end > pd.Timedelta(days=7):
            limiting_symbols = sorted(
                sym for sym, df in universe.items() if len(df) and df.index[-1] <= aligned_end
            )
            warnings.warn(
                f"_align_universe: aligned date range ends {aligned_end.date()}, "
                f"{(overall_latest - aligned_end).days} day(s) short of the latest date available "
                f"anywhere in the universe ({overall_latest.date()}). Symbol(s) with the shortest "
                f"trading history are limiting the whole walk-forward range: {limiting_symbols}."
            )
        aligned_start = common_index[0]
        overall_earliest = min(df.index[0] for df in universe.values() if len(df))
        if aligned_start - overall_earliest > pd.Timedelta(days=7):
            late_symbols = sorted(
                sym for sym, df in universe.items() if len(df) and df.index[0] >= aligned_start
            )
            warnings.warn(
                f"_align_universe: aligned date range starts {aligned_start.date()}, "
                f"{(aligned_start - overall_earliest).days} day(s) later than the earliest date available "
                f"anywhere in the universe ({overall_earliest.date()}). Symbol(s) listed latest "
                f"are trimming the start date for the whole universe: {late_symbols}."
            )
    return {symbol: df.loc[common_index] for symbol, df in universe.items()}


def _resolve_window_bars(window_years: float) -> int:
    return int(round(window_years * 252))


def _slice_universe_for_range(universe: dict, date_range: pd.DatetimeIndex) -> dict:
    """Returns a subset of universe where each symbol has at least one bar within date_range,
    with the dataframe sliced to the intersection with date_range.
    
    Temporarily removes assets that have no bars for the given date range.
    """
    sliced = {}
    for sym, df in universe.items():
        if df.empty:
            continue
        common = df.index.intersection(date_range)
        if len(common) > 0:
            sliced[sym] = df.loc[common]
    return sliced


def _resolve_master_calendar(
    universe: dict,
    start=None,
    end=None,
    min_coverage: float = 0.20,
) -> tuple:
    """Resolve master trading calendar across non-empty dataframes in universe.

    Finds all distinct trading dates. Computes the earliest date where at least
    min_coverage (default 20%) of universe assets have trading data.
    If `start` is specified and precedes this 20% coverage date, the effective
    start date is moved forward to the 20% coverage date and a warning is logged.

    Returns (master_calendar: pd.DatetimeIndex, effective_start: pd.Timestamp).
    """
    non_empty = {sym: df for sym, df in universe.items() if not df.empty}
    if not non_empty:
        return pd.DatetimeIndex([]), pd.Timestamp.now()

    all_dates = sorted(set().union(*(df.index for df in non_empty.values())))
    if not all_dates:
        return pd.DatetimeIndex([]), pd.Timestamp.now()

    total_assets = len(universe)
    min_assets = max(1, int(np.ceil(min_coverage * total_assets)))

    date_counts = pd.Series(0, index=pd.DatetimeIndex(all_dates))
    for df in non_empty.values():
        date_counts.loc[df.index.unique()] += 1

    valid_mask = date_counts >= min_assets
    if not valid_mask.any():
        earliest_20pct_date = all_dates[0]
    else:
        earliest_20pct_date = date_counts[valid_mask].index[0]

    effective_start = earliest_20pct_date
    if start:
        start_ts = pd.Timestamp(start)
        if start_ts < earliest_20pct_date:
            warnings.warn(
                f"Requested start date {start} precedes earliest date where at least "
                f"{min_coverage*100:.0f}% of universe assets ({min_assets}/{total_assets}) "
                f"have trading data ({earliest_20pct_date.strftime('%Y-%m-%d')}). "
                f"Moving start date to {earliest_20pct_date.strftime('%Y-%m-%d')}."
            )
            effective_start = earliest_20pct_date
        else:
            effective_start = start_ts

    cal_dates = [d for d in all_dates if d >= earliest_20pct_date]
    if end:
        end_ts = pd.Timestamp(end)
        cal_dates = [d for d in cal_dates if d <= end_ts]

    master_calendar = pd.DatetimeIndex(cal_dates)
    return master_calendar, effective_start


# --------------------------------------------------------------------------- #
# (B2.2) WALKFORWARD WINDOW-LEVEL MULTIPROCESS SCHEDULER
# --------------------------------------------------------------------------- #
# Payload contract -- a hard requirement, do NOT "simplify" it into
# `executor.submit(fold_worker, universe, template, params, ...)`:
#
#   * The ONLY thing that crosses the process boundary per task is an integer
#     FOLD INDEX. The fold's payload -- the already-loaded `universe` (tens of
#     MB of OHLCV), the template instance, params, args and the fold grid -- is
#     published in `_WALKFORWARD_WORKER_CTX` and INHERITED by each worker at
#     fork time (copy-on-write: ONE physical copy of the frames in RAM, mapped
#     read-only into every child). Nothing is pickled per task, and no worker
#     ever touches the OHLCV cache, so a parallel walk-forward puts exactly ONE
#     reader on `common/duckdb_cache.py` no matter how many workers or folds
#     there are. (The rejected alternative -- every worker re-reads the cache --
#     would put N concurrent DuckDB readers on it; DuckDB does allow concurrent
#     read-only connections opened AFTER the database exists, but it would also
#     multiply decompression/parse cost by N and expose the run to write races
#     from any other process refreshing the shared cache. Nothing here needs
#     that risk.)
#   * On platforms whose only start method is "spawn" (Windows) that inheritance
#     is unavailable, and the two remaining designs are (a) pickle the whole
#     universe into every worker or (b) have every worker re-read the cache.
#     Both are explicitly rejected, so `_walkforward_pool_context()` raises
#     `WalkforwardPoolUnavailable` and `run_walkforward` degrades to the serial
#     loop with a loud WARNING -- never silently, never by crashing.
#
# Determinism / ordering: a fold's metrics are computed by exactly the same
# function in both modes, results are collected KEYED BY FOLD INDEX, and the
# returned fold list is assembled in fold order -- never in completion order.
# `folds_df` rows, column order and `walkforward_rebalances.csv` rows are
# therefore bit-identical between `--n-jobs 1` and any `--n-jobs N`.
_WALKFORWARD_WORKER_CTX = None


class WalkforwardPoolUnavailable(RuntimeError):
    """A process pool cannot be used for this run: degrade to serial, loudly."""


def _nan_fold_metrics() -> dict:
    """All-NaN metrics for a fold that produced nothing. Module-level (and used
    by both the serial and the parallel path) so the two can never drift."""
    return {
        "sharpe_ratio": float("nan"), "cagr": float("nan"), "max_drawdown": float("nan"),
        "calmar_ratio": float("nan"), "win_rate": float("nan"), "profit_factor": float("nan"),
        "total_turnover": 0.0, "total_rebalances": 0,
        "rebalance_report": pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS),
        "actual_weights": pd.DataFrame(),
    }


def _evaluate_walkforward_fold(ctx: dict, spec: dict) -> tuple:
    """Evaluate ONE rolling fold, self-contained.

    Returns `(fold_metrics, error_message_or_None)`. This is the single
    implementation both the serial loop and the process pool call, so
    "parallel == serial" is structural rather than something two copies of the
    code have to keep agreeing on. A fold's failure is captured here (the
    pre-B2.2 contract: warn and emit NaN metrics, never abort the whole
    walk-forward) and reported by the caller in fold order.

    Deliberately performs NO data loading: the universe is handed in already
    loaded. That is both the fork/COW data-sharing requirement above and what
    keeps the `L2.3` freshness guard intact -- no fold ever reaches
    `expected_last_session` at all, so there is nothing that could thread a
    real session in.
    """
    universe = ctx["universe"]
    template = ctx["template"]
    params = ctx["params"]
    args = ctx["args"]
    master_calendar = ctx["master_calendar"]
    warmup_bars = ctx["warmup_bars"]

    start_idx = spec["start_idx"]
    end_idx = spec["end_idx"]
    buffer_start_idx = max(0, start_idx - warmup_bars)

    fold_dates = master_calendar[buffer_start_idx:end_idx]
    buffered_universe = _slice_universe_for_range(universe, fold_dates)
    eval_index = master_calendar[start_idx:end_idx]

    try:
        if not buffered_universe:
            fold_metrics = _nan_fold_metrics()
        else:
            full_weights = template.generate_weights(buffered_universe, params)
            if full_weights.empty:
                fold_metrics = _nan_fold_metrics()
            else:
                # Restrict to the eval window, but seed its first row with the
                # carried-over (forward-filled) target as of the window's
                # start -- otherwise a fold that starts between two
                # buffer-period rebalances would open in all-cash instead of
                # whatever the (now-warm) strategy actually held at that point.
                eval_universe = _slice_universe_for_range(universe, eval_index)
                eval_symbols = list(eval_universe.keys())

                from common.position_lifecycle import resolve_position_lifecycle_config, simulate_position_lifecycle
                from common.allocation_backtester import CASH_PROXY_SYMBOLS

                enable_pl = getattr(args, "enable_position_lifecycle", True)
                pl_cfg = resolve_position_lifecycle_config(params, args) if enable_pl else None

                _proxy_syms = CASH_PROXY_SYMBOLS if getattr(args, "cash_proxy_symbols", None) is None else args.cash_proxy_symbols
                resolved_cash_proxy = next((s for s in eval_symbols if str(s).upper() in _proxy_syms), None)
                market_type = "china" if getattr(args, "china_trading", False) else ("us" if getattr(args, "us_trading", False) else ("hk" if getattr(args, "hk_trading", False) else "other"))

                if enable_pl:
                    # Warm the FSM causally over the fold's pre-eval warmup buffer
                    warmed_weights, all_records = simulate_position_lifecycle(
                        universe=buffered_universe,
                        target_weights=full_weights,
                        cfg=pl_cfg,
                        market=market_type,
                        cash_proxy=resolved_cash_proxy,
                    )
                    eval_weights = warmed_weights.reindex(index=eval_index, columns=eval_symbols)
                    seed_row = (
                        warmed_weights.ffill()
                        .reindex(index=eval_index, columns=eval_symbols)
                        .iloc[0]
                        .fillna(0.0)
                    )
                    if resolved_cash_proxy is not None and resolved_cash_proxy in seed_row.index:
                        risky_sum = seed_row.drop(index=resolved_cash_proxy).sum()
                        seed_row[resolved_cash_proxy] = max(0.0, 1.0 - risky_sum)
                    eval_weights.iloc[0] = seed_row

                    # Both sides day-normalised, or the timestamped record stamp
                    # (`'2021-12-06 00:00:00'`) never matches the eval bar key
                    # (`'2021-12-06'`) -- see `_normalise_day_keys`. An empty result
                    # here is NOT harmless: `run_allocation_backtest` reads it as
                    # "no records supplied" and re-simulates the already-warmed
                    # weights (double entry-confirmation, day-0 wiped to 0.0).
                    eval_records = _lifecycle_records_in_window(all_records, eval_index)
                else:
                    eval_weights = full_weights.reindex(index=eval_index, columns=eval_symbols)
                    eval_weights.iloc[0] = full_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]
                    eval_records = None

                result = run_allocation_backtest(
                    eval_universe, eval_weights,
                    initial_capital=args.initial_capital,
                    commission_pct=args.commission_pct,
                    slippage_pct=args.slippage_pct,
                    min_shares=getattr(args, "min_shares", 1),
                    min_weight_change=getattr(args, "min_weight_change", 0.0),
                    china_trading=getattr(args, "china_trading", False),
                    us_trading=getattr(args, "us_trading", False),
                    hk_trading=getattr(args, "hk_trading", False),
                    # (B3.1) `close` by default == the pre-B3.1 engine; a fold that
                    # used a different execution mode would not be comparable to
                    # the standard-mode run, so the CLI value is threaded through
                    # unchanged (see `--execution-price`).
                    execution_price=getattr(args, "execution_price", "close"),
                    # (B3.2) same reasoning: both default to the pre-B3.2 engine,
                    # and a fold must be measured exactly like the standard run.
                    market_impact=getattr(args, "market_impact", False),
                    impact_gamma=getattr(args, "market_impact_gamma", 1.0),
                    max_volume_participation=getattr(args, "max_volume_participation", None),
                    # (B3.3 / B3.4) both default to None = off, so a fold is measured
                    # exactly like the standard run (and like every pre-B3.3 fold).
                    cash_yield_annual=getattr(args, "cash_sweep_rate", None),
                    borrow_fee_annual=getattr(args, "borrow_fee_annual", None),
                    enable_position_lifecycle=enable_pl,
                    lifecycle_config=pl_cfg,
                    lifecycle_records=eval_records,
                )
                if result["equity_curve"].empty:
                    fold_metrics = _nan_fold_metrics()
                else:
                    # Same fields (and the same sign convention) run_standard
                    # reports -- no separate recomputation, so the two modes
                    # can't drift apart.
                    fold_metrics = {
                        "sharpe_ratio": result["sharpe_ratio"],
                        "cagr": result["cagr"],
                        "max_drawdown": result["max_drawdown"],
                        "calmar_ratio": result["calmar_ratio"],
                        "win_rate": result["win_rate"],
                        "profit_factor": result["profit_factor"],
                        "total_turnover": result["total_turnover"],
                        "total_rebalances": result["total_rebalances"],
                        "rebalance_report": result.get("rebalance_report", pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS)),
                        "actual_weights": result.get("actual_weights", pd.DataFrame()),
                        # (F3) The fold's raw lifecycle decision log, carried in the fold dict
                        # exactly like `rebalance_report`/`actual_weights` so `main()` can
                        # concatenate it into `walkforward_lifecycle_records.csv`. `folds_clean`
                        # drops it before ANY pre-existing artifact is built, so
                        # `walkforward_report.csv` / `walkforward_summary.json` keep their
                        # exact columns and values (L2.37).
                        "lifecycle_records": result.get("lifecycle_records") or [],
                    }

                    if enable_pl and getattr(args, "lifecycle_compare", False):
                        eval_weights_off = full_weights.reindex(index=eval_index, columns=eval_symbols)
                        eval_weights_off.iloc[0] = full_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]
                        result_off = run_allocation_backtest(
                            eval_universe, eval_weights_off,
                            initial_capital=args.initial_capital,
                            commission_pct=args.commission_pct,
                            slippage_pct=args.slippage_pct,
                            min_shares=getattr(args, "min_shares", 1),
                            min_weight_change=getattr(args, "min_weight_change", 0.0),
                            china_trading=getattr(args, "china_trading", False),
                            us_trading=getattr(args, "us_trading", False),
                            hk_trading=getattr(args, "hk_trading", False),
                            execution_price=getattr(args, "execution_price", "close"),
                            market_impact=getattr(args, "market_impact", False),
                            impact_gamma=getattr(args, "market_impact_gamma", 1.0),
                            max_volume_participation=getattr(args, "max_volume_participation", None),
                            cash_yield_annual=getattr(args, "cash_sweep_rate", None),
                            borrow_fee_annual=getattr(args, "borrow_fee_annual", None),
                            enable_position_lifecycle=False,
                        )
                        if not result_off["equity_curve"].empty:
                            fold_metrics["off_metrics"] = {
                                "sharpe_ratio": result_off["sharpe_ratio"],
                                "cagr": result_off["cagr"],
                                "max_drawdown": result_off["max_drawdown"],
                                "total_turnover": result_off["total_turnover"],
                                "total_rebalances": result_off["total_rebalances"],
                            }
    except Exception as e:  # noqa: BLE001 -- pre-B2.2 fail-soft contract, per fold
        return _nan_fold_metrics(), str(e)

    return fold_metrics, None


def walkforward_fold_seed(base_seed, fold_index: int):
    """Per-fold seed derived from the master seed via `SeedSequence`.

    `spawn_key=(fold_index,)` gives every fold an INDEPENDENT stream (folds
    never share or shift each other's draws), and the mapping is a pure
    function of `(--seed, fold_index)` -- so fold k draws the same numbers
    whether it runs first on 1 process or last on 8 workers. `None` means "no
    seeding at all", which is the pre-B2.2 behavior (today's templates are
    deterministic and never touch the global RNG, so this only matters for
    future stochastic ones).
    """
    if base_seed is None:
        return None
    ss = np.random.SeedSequence(entropy=int(base_seed), spawn_key=(int(fold_index),))
    return int(ss.generate_state(1)[0])


def _run_fold_with_seed(ctx: dict, spec: dict) -> tuple:
    """`_evaluate_walkforward_fold` wrapped in per-fold RNG isolation.

    Seeds numpy's AND `random`'s global RNGs from the fold's own SeedSequence
    before the fold, then restores the caller's RNG state afterwards, so a fold
    cannot leak randomness into its neighbours (which would make fold order --
    and therefore worker count -- observable in the results).
    """
    seed = walkforward_fold_seed(ctx.get("base_seed"), spec["fold_index"])
    if seed is None:
        return _evaluate_walkforward_fold(ctx, spec)

    np_state = np.random.get_state()
    py_state = random.getstate()
    try:
        np.random.seed(seed)
        random.seed(seed)
        return _evaluate_walkforward_fold(ctx, spec)
    finally:
        np.random.set_state(np_state)
        random.setstate(py_state)


def _walkforward_fold_worker(position: int) -> tuple:
    """Process-pool entry point. Takes only the fold's POSITION in the fold
    grid; reads the payload from the fork-inherited module global (see the
    payload contract above). Returns the fold's 1-based index so the caller can
    reassemble in fold order regardless of completion order."""
    ctx = _WALKFORWARD_WORKER_CTX
    if ctx is None:
        raise RuntimeError("walkforward worker started without a published fold context")
    spec = ctx["fold_specs"][position]
    metrics, error = _run_fold_with_seed(ctx, spec)
    return spec["fold_index"], metrics, error


def _walkforward_pool_context():
    """Return a multiprocessing context that can SHARE the parent's universe by
    fork/COW, or raise `WalkforwardPoolUnavailable` explaining why it cannot.

    Only "fork" qualifies: the payload must be inherited, not serialized. (A
    forked child inherits the whole address space, so `_WALKFORWARD_WORKER_CTX`
    and the DataFrames inside it cost zero bytes of copying until written --
    and folds never write them.)"""
    try:
        available = multiprocessing.get_all_start_methods()
    except (ImportError, OSError) as exc:  # pragma: no cover - exotic platforms
        raise WalkforwardPoolUnavailable(f"multiprocessing unavailable ({exc})") from exc
    if "fork" not in available:
        raise WalkforwardPoolUnavailable(
            f"start method 'fork' is unavailable (available: {available}), so the loaded universe "
            f"cannot be shared with workers without pickling it N times or re-reading the cache N times"
        )
    try:
        return multiprocessing.get_context("fork")
    except (ValueError, OSError) as exc:  # pragma: no cover - exotic platforms
        raise WalkforwardPoolUnavailable(f"cannot create a 'fork' context ({exc})") from exc


def resolve_n_jobs(requested, n_folds: int) -> int:
    """Resolve `--n-jobs` into an actual worker count.

    * `None` / `-1` / `0` -> AUTO: `min(os.cpu_count(), n_folds)` (never more
      workers than there are folds, never fewer than 1).
    * anything `>= 1` -> that many workers, capped at `n_folds`.
    * `< -1` -> ValueError (only `-1` is a legal sentinel).
    """
    n_folds = max(1, int(n_folds))
    if requested is None or requested == -1 or requested == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(int(cpu), n_folds))
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise ValueError(f"--n-jobs must be an integer, got {type(requested).__name__}.")
    if requested < -1:
        raise ValueError(
            f"--n-jobs must be -1 (auto), 0 (auto), 1 (serial) or a positive worker count; got {requested}."
        )
    return max(1, min(int(requested), n_folds))


def _run_walkforward_folds_parallel(ctx: dict, fold_specs: list, n_jobs: int) -> tuple:
    """Fan the folds out to a `ProcessPoolExecutor`, returning
    `({fold_index: metrics}, {fold_index: error})`.

    The result mapping is keyed by fold index, so completion order is
    irrelevant to the caller. Any pool-level failure (fork refused by the
    container, broken pool, ...) is re-raised as `WalkforwardPoolUnavailable`
    for the caller to degrade on -- the fold-level failures themselves are
    already captured inside `_evaluate_walkforward_fold`."""
    global _WALKFORWARD_WORKER_CTX

    mp_ctx = _walkforward_pool_context()
    _WALKFORWARD_WORKER_CTX = dict(ctx, fold_specs=fold_specs)
    results, errors = {}, {}
    try:
        try:
            executor = ProcessPoolExecutor(max_workers=n_jobs, mp_context=mp_ctx)
        except (OSError, ImportError, PermissionError, ValueError, NotImplementedError) as exc:
            # e.g. "[Errno 38] Function not implemented" from a container that
            # forbids fork, or no /dev/shm for the pool's semaphores.
            raise WalkforwardPoolUnavailable(
                f"cannot start a process pool ({type(exc).__name__}: {exc})"
            ) from exc

        with executor:
            futures = {
                executor.submit(_walkforward_fold_worker, position): position
                for position in range(len(fold_specs))
            }
            for future in as_completed(futures):
                position = futures[future]
                try:
                    fold_index, metrics, error = future.result()
                except BrokenProcessPool as exc:
                    # A worker died (OOM-kill, segfault, fork restriction hit
                    # late): the pool is unusable, degrade -- don't crash.
                    raise WalkforwardPoolUnavailable(
                        f"walkforward worker pool broke on fold {position + 1}: {exc}"
                    ) from exc
                except (KeyboardInterrupt, SystemExit):
                    raise
                results[fold_index] = metrics
                if error is not None:
                    errors[fold_index] = error
    finally:
        # Do not leak the (large) universe reference into any later pool.
        _WALKFORWARD_WORKER_CTX = None
    return results, errors


def _collect_walkforward_folds(ctx: dict, fold_specs: list, n_jobs: int) -> tuple:
    """Evaluate every fold, serially or on a process pool, and return
    `({fold_index: metrics}, {fold_index: error})`.

    The serial branch is the pre-B2.2 loop verbatim (same print sequence, same
    fail-soft per fold); it is also the documented fallback when the pool
    cannot be created, in which case a WARNING is emitted first -- degradation
    is never silent. (On that degraded path the `Evaluating ...` lines are
    legitimately printed twice: once for the dispatch attempt that failed, once
    for the serial pass that produced the numbers.)"""
    total_folds = len(fold_specs)
    if total_folds == 0:
        return {}, {}

    if n_jobs > 1 and total_folds > 1:
        print(
            f"Parallel walkforward: dispatching {total_folds} folds to {n_jobs} worker process(es); "
            f"results are re-ordered by fold index before assembly.",
            flush=True,
        )
        for spec in fold_specs:
            print(
                f"[Fold {spec['fold_index']}/{total_folds}] Evaluating {spec['start_date']} to {spec['end_date']}...",
                flush=True,
            )
        try:
            return _run_walkforward_folds_parallel(ctx, fold_specs, n_jobs)
        except WalkforwardPoolUnavailable as exc:
            message = (
                f"walkforward process pool unavailable ({exc}); falling back to the serial fold loop. "
                f"Results are identical, only slower."
            )
            warnings.warn(message)
            print(f"WARNING: {message}", flush=True)

    results, errors = {}, {}
    for spec in fold_specs:
        print(
            f"[Fold {spec['fold_index']}/{total_folds}] Evaluating {spec['start_date']} to {spec['end_date']}...",
            flush=True,
        )
        metrics, error = _run_fold_with_seed(ctx, spec)
        results[spec["fold_index"]] = metrics
        if error is not None:
            errors[spec["fold_index"]] = error
    return results, errors


#: Flattened column layout for the `lifecycle_records.csv` /
#: `walkforward_lifecycle_records.csv` artifacts (F3). Derived from
#: `common.position_lifecycle.LifecycleDecision.to_dict()` -- the engine's own
#: record schema -- plus the `date`/`symbol` keys the simulator stamps onto
#: every record. Nobody downstream may re-derive these names: if the decision
#: dataclass grows a field, add it here in the same commit.
LIFECYCLE_RECORD_COLUMNS = [
    "date",
    "symbol",
    "action",
    "reason",
    "target_weight",
    "state_before",
    "state_after",
    "fast_path_triggered",
    "cancelled_entry_streak",
    "blocking_reason",
    "detail_reason",
]


def lifecycle_records_to_frame(records, *, fold_index: int | None = None) -> pd.DataFrame:
    """Serialize the position-lifecycle decision log into a flat, CSV-safe frame.

    `detail_reason` is the one nested field (a dict of diagnosis keys such as
    `current_price` / `blocking_reason` / `note`); it is JSON-encoded rather
    than dropped, so the file keeps the "why was nothing traded" evidence the
    lifecycle layer exists to produce. A stable `columns=` list is always
    passed, so an all-`HOLD` fold and an empty fold produce the same header.

    `fold_index` prepends a `fold` column for the walkforward roll-up.
    """
    columns = (["fold"] if fold_index is not None else []) + list(LIFECYCLE_RECORD_COLUMNS)
    rows = []
    for rec in records or []:
        row = {"fold": fold_index} if fold_index is not None else {}
        for col in LIFECYCLE_RECORD_COLUMNS:
            if col == "detail_reason":
                detail = rec.get(col)
                row[col] = json.dumps(detail, ensure_ascii=False, default=str) if detail else ""
            elif col == "date":
                # The simulator stamps `str(Timestamp)` ("2024-08-27 00:00:00"); flatten
                # to the same `YYYY-MM-DD` convention `rebalance_report.csv` uses so the
                # two logs can be joined on their date column without parsing.
                try:
                    row[col] = str(pd.Timestamp(rec.get(col)).date())
                except (TypeError, ValueError):
                    row[col] = rec.get(col)
            else:
                row[col] = rec.get(col)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def run_standard(universe: dict, template, params: dict, args) -> dict:
    master_calendar, effective_start = _resolve_master_calendar(
        universe, getattr(args, "start", None), getattr(args, "end", None), min_coverage=0.20
    )
    if master_calendar.empty:
        raise ValueError("Universe contains no valid trading dates.")

    raw_target_weights = template.generate_weights(universe, params)
    if raw_target_weights.empty:
        raise ValueError("Template generated empty weights.")

    eval_index = master_calendar[master_calendar >= effective_start]
    eval_universe = _slice_universe_for_range(universe, eval_index)
    eval_symbols = list(eval_universe.keys())

    from common.position_lifecycle import resolve_position_lifecycle_config, simulate_position_lifecycle
    from common.allocation_backtester import CASH_PROXY_SYMBOLS

    enable_pl = getattr(args, "enable_position_lifecycle", True)
    pl_cfg = resolve_position_lifecycle_config(params, args) if enable_pl else None

    _proxy_syms = CASH_PROXY_SYMBOLS if getattr(args, "cash_proxy_symbols", None) is None else args.cash_proxy_symbols
    resolved_cash_proxy = next((s for s in eval_symbols if str(s).upper() in _proxy_syms), None)
    market_type = "china" if getattr(args, "china_trading", False) else ("us" if getattr(args, "us_trading", False) else ("hk" if getattr(args, "hk_trading", False) else "other"))

    has_warmup = len(eval_index) > 0 and len(eval_index) < len(raw_target_weights)
    if enable_pl:
        warmed_weights, all_records = simulate_position_lifecycle(
            universe=universe,
            target_weights=raw_target_weights,
            cfg=pl_cfg,
            market=market_type,
            cash_proxy=resolved_cash_proxy,
        )
        if has_warmup:
            eval_weights = warmed_weights.reindex(index=eval_index, columns=eval_symbols)
            seed_row = (
                warmed_weights.ffill()
                .reindex(index=eval_index, columns=eval_symbols)
                .iloc[0]
                .fillna(0.0)
            )
            if resolved_cash_proxy is not None and resolved_cash_proxy in seed_row.index:
                risky_sum = seed_row.drop(index=resolved_cash_proxy).sum()
                seed_row[resolved_cash_proxy] = max(0.0, 1.0 - risky_sum)
            eval_weights.iloc[0] = seed_row
            # Same day-normalised filter as the walkforward path (root cause of the
            # "records never match the eval bar" bug) -- `astype(str)` on a
            # Timestamp index yields `'2021-12-06'`, the record stamp is
            # `'2021-12-06 00:00:00'`.
            eval_records = _lifecycle_records_in_window(all_records, eval_index)
            target_weights = eval_weights
        else:
            target_weights = warmed_weights.reindex(index=eval_index, columns=eval_symbols)
            eval_records = all_records
    else:
        if has_warmup:
            eval_weights = raw_target_weights.reindex(index=eval_index, columns=eval_symbols)
            eval_weights.iloc[0] = raw_target_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]
            target_weights = eval_weights
        else:
            target_weights = raw_target_weights.reindex(index=eval_index, columns=eval_symbols)
        eval_records = None

    result = run_allocation_backtest(
        eval_universe, target_weights,
        initial_capital=args.initial_capital,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct,
        min_shares=getattr(args, "min_shares", 1),
        min_weight_change=getattr(args, "min_weight_change", 0.0),
        china_trading=getattr(args, "china_trading", False),
        us_trading=getattr(args, "us_trading", False),
        hk_trading=getattr(args, "hk_trading", False),
        # (B3.1) default "close" leaves every pre-B3.1 result bit-identical.
        execution_price=getattr(args, "execution_price", "close"),
        # (B3.2) both default OFF (False / None) == the pre-B3.2 engine.
        market_impact=getattr(args, "market_impact", False),
        impact_gamma=getattr(args, "market_impact_gamma", 1.0),
        max_volume_participation=getattr(args, "max_volume_participation", None),
        # (B3.3 / B3.4) both default to None = off == the pre-B3.3 engine.
        cash_yield_annual=getattr(args, "cash_sweep_rate", None),
        borrow_fee_annual=getattr(args, "borrow_fee_annual", None),
        enable_position_lifecycle=enable_pl,
        lifecycle_config=pl_cfg,
        lifecycle_records=eval_records,
    )

    if result["equity_curve"].empty:
        raise ValueError("Backtest produced empty equity curve.")

    if enable_pl and getattr(args, "lifecycle_compare", False):
        if has_warmup:
            target_weights_off = raw_target_weights.reindex(index=eval_index, columns=eval_symbols)
            target_weights_off.iloc[0] = raw_target_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]
        else:
            target_weights_off = raw_target_weights.reindex(index=eval_index, columns=eval_symbols)

        result_off = run_allocation_backtest(
            eval_universe, target_weights_off,
            initial_capital=args.initial_capital,
            commission_pct=args.commission_pct,
            slippage_pct=args.slippage_pct,
            min_shares=getattr(args, "min_shares", 1),
            min_weight_change=getattr(args, "min_weight_change", 0.0),
            china_trading=getattr(args, "china_trading", False),
            us_trading=getattr(args, "us_trading", False),
            hk_trading=getattr(args, "hk_trading", False),
            execution_price=getattr(args, "execution_price", "close"),
            market_impact=getattr(args, "market_impact", False),
            impact_gamma=getattr(args, "market_impact_gamma", 1.0),
            max_volume_participation=getattr(args, "max_volume_participation", None),
            cash_yield_annual=getattr(args, "cash_sweep_rate", None),
            borrow_fee_annual=getattr(args, "borrow_fee_annual", None),
            enable_position_lifecycle=False,
        )
        if not result_off["equity_curve"].empty:
            result["off_metrics"] = {
                "sharpe_ratio": result_off["sharpe_ratio"],
                "cagr": result_off["cagr"],
                "max_drawdown": result_off["max_drawdown"],
                "total_turnover": result_off["total_turnover"],
                "total_rebalances": result_off["total_rebalances"],
            }

    # result already carries sharpe_ratio/cagr/max_drawdown/calmar_ratio/
    # win_rate/profit_factor from run_allocation_backtest -- report those
    # directly rather than recomputing (that recomputation used to disagree
    # in sign with the backtester's own max_drawdown).
    return result


def run_walkforward(universe: dict, template, params: dict, args) -> list:
    master_calendar, effective_start = _resolve_master_calendar(
        universe, getattr(args, "start", None), getattr(args, "end", None), min_coverage=0.20
    )
    if master_calendar.empty:
        raise ValueError("Master calendar has no valid trading dates.")

    n_bars = len(master_calendar)
    window_bars = _resolve_window_bars(args.window_years)
    step_bars = int(round(args.step_years * 252))

    if window_bars <= 0:
        raise ValueError(
            f"--window-years must be positive (resolved to {window_bars} bars from "
            f"window_years={args.window_years}); a non-positive window would silently "
            f"corrupt fold end dates via negative indexing instead of ever evaluating anything."
        )
    if step_bars <= 0:
        raise ValueError(
            f"--step-years must be positive (resolved to {step_bars} bars from "
            f"step_years={args.step_years}); a non-positive step would never advance "
            f"past the first fold, hanging the walk-forward loop forever."
        )

    base_start_idx = 0
    if effective_start is not None and master_calendar[0] < effective_start:
        locs = np.flatnonzero(master_calendar >= effective_start)
        if len(locs) > 0:
            base_start_idx = int(locs[0])

    if window_bars >= n_bars or window_bars > (n_bars - base_start_idx):
        raise ValueError("Window size is larger than the available data.")

    # Lookback indicators (e.g. InverseVolatility's realized_vol,
    # CrossSectionalMomentum's roc) are cold for their first `warmup_bars`
    # bars. Slicing a fold to bare [start_idx:end_idx) recomputes them from
    # scratch, so every rebalance date inside that cold period is dropped --
    # silently under-investing roughly the first `warmup_bars` bars of EVERY
    # fold. Pull in that many extra bars before the window purely for
    # indicator warmup; the eval window itself (start_idx:end_idx) is
    # unchanged.
    warmup_bars = template.warmup_bars(params)
    enable_pl = getattr(args, "enable_position_lifecycle", True)
    if enable_pl:
        from common.position_lifecycle import resolve_position_lifecycle_config
        pl_cfg = resolve_position_lifecycle_config(params, args)
        freq = int(params.get("rebalance_freq_days", 21)) if params else 21
        warmup_bars = max(warmup_bars, 0) + max(freq * 2, 42) + pl_cfg.guard_latency_bars + 5

    # (B2.2) Enumerate the fold grid up front. Fold boundaries are pure
    # bar-position arithmetic over `master_calendar` -- identical whether the
    # folds are then evaluated serially or on a process pool, which is what
    # makes the two modes directly comparable.
    fold_specs = []
    start_idx = base_start_idx
    while start_idx + window_bars <= n_bars:
        end_idx = start_idx + window_bars
        fold_specs.append({
            "fold_index": len(fold_specs) + 1,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "start_date": master_calendar[start_idx].strftime("%Y-%m-%d"),
            "end_date": master_calendar[end_idx - 1].strftime("%Y-%m-%d"),
        })
        start_idx += step_bars

    ctx = {
        "universe": universe,
        "template": template,
        "params": params,
        "args": args,
        "warmup_bars": warmup_bars,
        "master_calendar": master_calendar,
        # Programmatic callers that predate B2.2 (and this module's own unit
        # tests) construct a bare namespace without `n_jobs`: they keep the
        # historical entirely-serial behavior. Only the CLI defaults to auto.
        "base_seed": getattr(args, "seed", None),
    }
    n_jobs = resolve_n_jobs(getattr(args, "n_jobs", 1), len(fold_specs))

    fold_metrics_by_index, fold_errors_by_index = _collect_walkforward_folds(ctx, fold_specs, n_jobs)

    folds = []
    for spec in fold_specs:
        i = spec["fold_index"]
        total_folds = len(fold_specs)
        # A fold that blew up reports its exception text here, in FOLD order
        # (the worker only hands the message back -- it never prints), so the
        # console transcript stays byte-identical to the serial loop's.
        if i in fold_errors_by_index:
            print(f"Error in window {spec['start_date']} to {spec['end_date']}: {fold_errors_by_index[i]}")
        fold_metrics = fold_metrics_by_index[i]

        sr = fold_metrics.get("sharpe_ratio", float("nan"))
        if np.isnan(sr):
            print(f"  [Fold {i}/{total_folds}] Done: NaN metrics", flush=True)
        else:
            print(
                f"  [Fold {i}/{total_folds}] Done: Sharpe={sr:.2f}, "
                f"CAGR={fold_metrics['cagr']:.1%}, MaxDD={fold_metrics['max_drawdown']:.1%}, "
                f"Rebalances={fold_metrics['total_rebalances']}",
                flush=True,
            )

        folds.append({"start_date": spec["start_date"], "end_date": spec["end_date"], **fold_metrics})

    return folds


def _standard_score_fn(universe, args):
    def score_fn(template, params):
        return run_standard(universe, template, params, args)
    return score_fn


def _walkforward_score_fn(universe, args):
    def score_fn(template, params):
        folds = run_walkforward(universe, template, params, args)
        sharpes = [f["sharpe_ratio"] for f in folds if np.isfinite(f["sharpe_ratio"])]
        mean_sharpe = float(np.mean(sharpes)) if sharpes else float("-inf")
        # Mean-fold reductions for cagr/max_drawdown/calmar_ratio, same
        # convention as sharpe_ratio above -- cagr in particular is needed so
        # main()'s optimize_report.json "improvement.cagr" (best_result.cagr
        # - original_result.cagr) isn't always NaN-minus-NaN under
        # --mode walkforward (neither dict used to carry a "cagr" key at all).
        cagrs = [f["cagr"] for f in folds if np.isfinite(f["cagr"])]
        mean_cagr = float(np.mean(cagrs)) if cagrs else float("nan")
        max_drawdowns = [f["max_drawdown"] for f in folds if np.isfinite(f["max_drawdown"])]
        mean_max_drawdown = float(np.mean(max_drawdowns)) if max_drawdowns else float("nan")
        calmar_ratios = [f["calmar_ratio"] for f in folds if np.isfinite(f["calmar_ratio"])]
        mean_calmar_ratio = float(np.mean(calmar_ratios)) if calmar_ratios else float("nan")
        total_rebalances = sum(f.get("total_rebalances", 0) for f in folds)
        total_turnover = sum(f.get("total_turnover", 0.0) for f in folds)

        n_valid_folds = len(sharpes)
        dsr = float("nan")
        sharpe_std = float("nan")
        if n_valid_folds >= 2:
            sharpe_std = float(pd.Series(sharpes).std(ddof=1))
            dsr = deflated_sharpe_ratio(
                observed_sharpe=mean_sharpe,
                n_trials=n_valid_folds,
                n_obs=_resolve_window_bars(args.window_years),
                sharpe_std=sharpe_std,
            )

        return {
            "sharpe_ratio": mean_sharpe, "cagr": mean_cagr,
            "max_drawdown": mean_max_drawdown, "calmar_ratio": mean_calmar_ratio,
            "deflated_sharpe_ratio": dsr,
            "total_rebalances": total_rebalances,
            "total_turnover": total_turnover, "folds": folds,
        }
    return score_fn


def _resolve_baseline_params(template, baseline_params_json: str = None) -> dict:
    """Same JSON-object-string convention `common/universe.py`'s
    `resolve_universe_from_args` uses for `--universe-kwargs`: parse if given,
    raise ValueError on malformed JSON or a non-dict result. Falls back to the
    template's first param_grid combination when no override is given."""
    if baseline_params_json is not None:
        try:
            parsed = json.loads(baseline_params_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse --baseline-params JSON string: {exc}")
        if not isinstance(parsed, dict):
            raise ValueError(f"--baseline-params must be a JSON object, got {type(parsed).__name__}.")
        return parsed
    return {k: v[0] for k, v in template.param_grid.items()}


def _run_baseline(args, cache_dir, data_kwargs, aligned_index=None):
    """Loads --baseline-symbol and runs it through the same run_standard/
    run_walkforward path as the main strategy, using --baseline-template (a
    static template only -- no pattern_spec) and --baseline-params (or that
    template's first param_grid combination). Returns (baseline_out,
    baseline_params).

    `aligned_index` (walkforward mode only): the main run's own aligned
    calendar (see `_align_universe`). Trimming the baseline universe onto
    this exact calendar before running `run_walkforward` forces its fold
    boundaries (bar-position arithmetic over `n_bars`) to land on the same
    dates as the main run's folds -- otherwise the baseline's independently-
    loaded calendar essentially never lines up bar-for-bar with the main
    universe's, and `_merge_baseline_folds`'s date-based join silently
    matches zero rows (see that function's `baseline_calendar_mismatch`
    docstring)."""
    print(f"\n=== Loading Baseline: {args.baseline_symbol} ({args.baseline_template}) ===")
    baseline_template = get_template(args.baseline_template)
    baseline_params = _resolve_baseline_params(baseline_template, args.baseline_params)

    baseline_fetch_start = args.start
    if aligned_index is not None and len(aligned_index) > 0:
        earliest_aligned = aligned_index[0].strftime("%Y-%m-%d")
        if args.start and earliest_aligned < args.start:
            baseline_fetch_start = earliest_aligned

    baseline_universe = load_universe_with_banner(
        [args.baseline_symbol], baseline_fetch_start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=cache_dir,
        data_kwargs=data_kwargs, require_nonempty=True,
        expected_last_session=backtest_expected_last_session(),
    )

    if aligned_index is not None:
        spy_df = next(iter(baseline_universe.values()))
        common = aligned_index.intersection(spy_df.index)
        if len(common) < len(aligned_index):
            warnings.warn(
                f"--baseline-symbol {args.baseline_symbol}'s own calendar only covers "
                f"{len(common)}/{len(aligned_index)} of the main universe's aligned dates -- "
                f"baseline fold boundaries may not exactly match the main run's."
            )
        baseline_universe = {sym: df.loc[common] for sym, df in baseline_universe.items()}

    baseline_args = copy.copy(args)
    # The baseline represents an unconstrained market benchmark portfolio (e.g. 000300.SH, SPY, ^GSPC).
    # It must not be constrained by discrete equity board lots (e.g. min_shares=100 under china_trading)
    # or market circuit breakers / T+1 / stamp duty, which would prevent an index priced at thousands
    # of points from executing trades or cause artificial cash drag / 0-share execution.
    baseline_args.min_shares = 0
    baseline_args.min_weight_change = 0.0
    baseline_args.china_trading = False
    baseline_args.us_trading = False
    baseline_args.hk_trading = False
    baseline_args.enable_position_lifecycle = False

    if args.mode == "standard":
        baseline_out = run_standard(baseline_universe, baseline_template, baseline_params, baseline_args)
    else:
        baseline_out = run_walkforward(baseline_universe, baseline_template, baseline_params, baseline_args)

    return baseline_out, baseline_params


def _compute_standard_comparison(result: dict, baseline_result: dict) -> dict:
    """Strategy-vs-baseline comparison metrics for --mode standard, aligned on
    the two equity curves' overlapping dates. Degenerate (<2 overlapping
    bars) input returns NaN for every relative field rather than raising."""
    strat_eq = result["equity_curve"]["equity"]
    base_eq = baseline_result["equity_curve"]["equity"]
    common_idx = strat_eq.index.intersection(base_eq.index)

    if len(common_idx) < 2:
        return {
            "overlap_bars": int(len(common_idx)),
            "alpha": float("nan"),
            "beta": float("nan"),
            "tracking_error": float("nan"),
            "information_ratio": float("nan"),
            "outperformance_cagr": float("nan"),
        }

    strat_ret = strat_eq.loc[common_idx].pct_change().dropna()
    base_ret = base_eq.loc[common_idx].pct_change().dropna()
    ab = alpha_beta(strat_ret, base_ret)
    return {
        "overlap_bars": int(len(common_idx)),
        "alpha": ab["alpha"],
        "beta": ab["beta"],
        "tracking_error": tracking_error(strat_ret, base_ret),
        "information_ratio": information_ratio(strat_ret, base_ret),
        "outperformance_cagr": result["cagr"] - baseline_result["cagr"],
    }


def _merge_baseline_folds(folds_df: pd.DataFrame, baseline_folds_df: pd.DataFrame) -> tuple:
    """LEFT-JOIN by (start_date, end_date) columns, NOT row position -- the
    two fold sets are computed from independent bar-position arithmetic over
    independently-loaded calendars and are not guaranteed to align row-for-
    row.

    Returns `(merged_df, baseline_calendar_mismatch)`. `baseline_calendar_mismatch`
    is True iff BOTH fold lists are non-empty but the join matched zero rows
    (every baseline_* column came back all-NaN) -- almost always because the
    main universe's aligned calendar and --baseline-symbol's calendar cover
    different date ranges (e.g. one of the main --universe symbols has a
    shorter history than --baseline-symbol, shifting every fold's start/end
    date), silently producing an all-NaN baseline comparison with no other
    signal that anything went wrong. Does NOT change the join logic itself --
    date-based joining is still correct in the normal (calendar-aligned) case."""
    renamed = baseline_folds_df[["start_date", "end_date", "sharpe_ratio", "cagr", "max_drawdown", "calmar_ratio"]].rename(
        columns={
            "sharpe_ratio": "baseline_sharpe_ratio",
            "cagr": "baseline_cagr",
            "max_drawdown": "baseline_max_drawdown",
            "calmar_ratio": "baseline_calmar_ratio",
        }
    )
    merged = folds_df.merge(renamed, on=["start_date", "end_date"], how="left")
    merged["outperformance"] = merged["cagr"] - merged["baseline_cagr"]

    baseline_calendar_mismatch = bool(
        not folds_df.empty and not baseline_folds_df.empty
        and merged["baseline_sharpe_ratio"].notna().sum() == 0
    )
    if baseline_calendar_mismatch:
        print(
            "WARNING: baseline comparison matched ZERO overlapping (start_date, end_date) "
            "fold windows between the main universe and --baseline-symbol, even though both "
            "produced folds -- every baseline_* column and 'outperformance' below will be NaN. "
            "This almost always means the main universe and --baseline-symbol have different "
            "effective trading calendars (e.g. one of the main --universe symbols has a shorter "
            "history than --baseline-symbol). Check each symbol's actual date range/history."
        )

    return merged, baseline_calendar_mismatch


# --------------------------------------------------------------------------- #
# Run Manifest & Effective Strategy Persistence (§1 backtest_research_impl.md)
# --------------------------------------------------------------------------- #

_NULL_METRICS: dict = {
    "sharpe": None,
    "cagr": None,
    "max_drawdown": None,
    "calmar": None,
    "n_folds": None,
    "deflated_sharpe": None,
}


def _git(args_list: list[str]) -> str | None:
    try:
        res = subprocess.run(
            ["git", *args_list],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
        return None
    except Exception:
        return None


def _sha256_file(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _mtime_iso(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path), tz=datetime.timezone.utc
        ).isoformat()
    except Exception:
        return None


def _run_ts_from_results_dir(results_dir: str) -> str | None:
    if not results_dir:
        return None
    parts = [p for p in os.path.abspath(results_dir).split(os.sep) if p]
    for p in reversed(parts):
        if re.match(r"^\d{8}_\d{6}$", p):
            return p
    return None


def _group_from_results_dir(results_dir: str) -> str:
    if not results_dir:
        print(f"WARNING: could not infer group from results_dir '{results_dir}'; setting group=\"\"")
        return ""
    norm_path = os.path.abspath(results_dir)
    parts = [p for p in norm_path.split(os.sep) if p]
    ts_idx = None
    for i, p in enumerate(parts):
        if re.match(r"^\d{8}_\d{6}$", p):
            ts_idx = i
            break
    if ts_idx is not None and (len(parts) - 1 - ts_idx) >= 2:
        return "/".join(parts[ts_idx + 1 : -1])
    print(f"WARNING: could not infer group from results_dir '{results_dir}'; setting group=\"\"")
    return ""


def _artifact_sizes(results_dir: str) -> list[dict]:
    if not os.path.exists(results_dir):
        return []
    artifacts = []
    with os.scandir(results_dir) as it:
        for entry in it:
            # `run.log` is written by the shell `tee` after this process exits, so it is
            # invisible here; `run_manifest.json` is this very manifest, and a file cannot
            # truthfully report its own post-write size from a pre-write scan (Phase 1 vs
            # Phase 2 sizes differ) -- listing it can only record a stale number. Both are
            # therefore excluded by design, not by oversight
            # (docs/backtest_research_impl.md §1 Phase-2 rules).
            if entry.is_file() and entry.name not in ("run.log", "run_manifest.json"):
                artifacts.append({"name": entry.name, "bytes": entry.stat().st_size})
    artifacts.sort(key=lambda a: a["name"])
    return artifacts


def _extract_walkforward_metrics(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return dict(_NULL_METRICS)
    return {
        "sharpe": summary.get("mean_sharpe_ratio"),
        "cagr": summary.get("mean_cagr"),
        "max_drawdown": summary.get("mean_max_drawdown"),
        "calmar": summary.get("mean_calmar_ratio"),
        "n_folds": summary.get("n_folds"),
        "deflated_sharpe": summary.get("deflated_sharpe_ratio"),
    }


def _dump_effective_strategy_def(
    strategy_def: dict,
    params: dict,
    research_strategy_spec: dict | None,
    results_dir: str,
    cli_overrides: dict | None = None,
) -> str:
    """Persist the effective strategy configuration (with CLI overrides / optimize
    updates merged) into the current run's results_dir so every backtest output
    folder is self-contained and auditable."""
    dump_payload: dict = copy.deepcopy(strategy_def) if isinstance(strategy_def, dict) else {}
    dump_payload["params"] = copy.deepcopy(params)
    # Always emitted -- `{}` when no --strategy-params was passed -- so a reader can tell
    # "no CLI override" apart from "field missing because an older build wrote this dump"
    # without guessing (docs/backtest_research_impl.md §1).
    dump_payload["cli_strategy_params_override"] = copy.deepcopy(cli_overrides) if cli_overrides is not None else {}
    if research_strategy_spec is not None:
        spec_copy = copy.deepcopy(research_strategy_spec)
        if isinstance(spec_copy, dict) and isinstance(spec_copy.get("entry_data"), dict):
            spec_copy["entry_data"]["parameters"] = copy.deepcopy(params)
        # NOTE: only `research_strategy_spec` is written. An earlier version also emitted a
        # duplicate `research_strategy` key that nothing ever read (`load_strategy_file`
        # validates `research_strategy_spec` only -- common/strategy_spec.py) -- removed so the
        # dump matches the documented schema in docs/backtest_research_impl.md §1 exactly.
        dump_payload["research_strategy_spec"] = spec_copy
    elif isinstance(dump_payload.get("research_strategy_spec"), dict):
        if isinstance(dump_payload["research_strategy_spec"].get("entry_data"), dict):
            dump_payload["research_strategy_spec"]["entry_data"]["parameters"] = copy.deepcopy(params)

    dump_path = os.path.join(results_dir, "strategy_dump.json")
    write_json_report(dump_payload, dump_path)
    return dump_path


_CLI_ARGS_MAX_SERIALIZED_BYTES = 4096


def _json_safe_cli_value(value):
    """Convert one parsed-argparse value into something `json` can serialise.

    **Whitelist-free by design**: every current and future CLI flag must land in the
    manifest automatically, so this converts by *type* (Path -> str, Enum -> `.value`,
    nested list/dict -> recursive conversion, anything else -> `str()`) and never by a
    hand-maintained list of flag names. A hand-written allowlist is exactly the drift this
    field exists to prevent: add `--cash-sweep-rate` and forget the list, and the manifest
    silently stops recording the one assumption that decides whether two runs are comparable.

    Values that cannot be recorded *faithfully* are replaced by a `"<omitted:type>"` marker
    instead of being guessed at: callables, open file handles (their `repr` contains a
    memory address, i.e. it is volatile and not reproducible from the manifest), and any
    value whose serialised form exceeds `_CLI_ARGS_MAX_SERIALIZED_BYTES` (a 4 KB cap keeps
    a huge `--universe` from bloating every leaf manifest). Dropping such a value entirely
    would be worse than marking it: a missing key is indistinguishable from an old manifest
    (docs/backtest_research_impl.md §1 `cli_args`).
    """
    if callable(value):
        return f"<omitted:{type(value).__name__}>"
    if isinstance(value, enum.Enum):
        return _json_safe_cli_value(value.value)
    if isinstance(value, pathlib.PurePath):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        candidate = value
    elif isinstance(value, (list, tuple)):
        candidate = [_json_safe_cli_value(item) for item in value]
    elif isinstance(value, dict):
        candidate = {
            str(k): _json_safe_cli_value(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    elif isinstance(value, io.IOBase) or hasattr(value, "read") or hasattr(value, "write"):
        return f"<omitted:{type(value).__name__}>"
    else:
        # Unknown (usually third-party) object: `str()` is the only honest generic
        # rendering, and it is still better than a missing key. If it does not
        # serialise we mark it rather than raising -- provenance must never crash a run.
        candidate = str(value)
    try:
        serialized = json.dumps(candidate)
    except (TypeError, ValueError):
        return f"<omitted:{type(value).__name__}>"
    if len(serialized) > _CLI_ARGS_MAX_SERIALIZED_BYTES:
        return f"<omitted:{type(value).__name__}>"
    return candidate


def _coerce_strategy_params_for_manifest(raw):
    """`--strategy-params` arrives as a JSON *string*; record the parsed object when it
    parses, the raw string when it does not.

    Never drop it and never raise: the whole point of `cli_args` is that a reader can
    tell what assumptions a run was launched with (e.g. a 1.5% vs 0% cash yield), so a
    malformed override must still be visible -- as the literal text the operator typed --
    rather than vanishing from the manifest.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _cli_args_payload(args=None) -> dict:
    """Structured CLI provenance for `run_manifest.json`: `{"argv": [...], "options": {...}}`.

    `argv` is `sys.argv[1:]` verbatim (the fallback of record -- argument order, repeated
    flags and quoting all survive it). `options` is derived mechanically from the parsed
    namespace via `vars(args)`, sorted by key, so it cannot drift when a new flag is added
    (see `_json_safe_cli_value`). Both are fully known before the backtest starts, which is
    why this block is written in Phase 1 and merely preserved by Phase 2
    (docs/backtest_research_impl.md §1).
    """
    payload: dict = {"argv": list(sys.argv[1:]), "options": {}}
    if args is None:
        return payload
    namespace = vars(args)
    for key in sorted(namespace):
        value = namespace[key]
        if key == "strategy_params":
            payload["options"][key] = _coerce_strategy_params_for_manifest(value)
            continue
        payload["options"][key] = _json_safe_cli_value(value)
    return payload


def _dump_run_manifest(
    results_dir: str,
    *,
    mode: str,
    group: str,
    strategy_name: str,
    strategy_file: str,
    strategy_dump_path: str,
    db_path: str,
    db_used: bool,
    cli_overrides: dict | None = None,
    cli_args: dict | None = None,
    metrics: dict | None = None,
    artifacts: list[dict] | None = None,
    enable_position_lifecycle: bool = True,
    position_lifecycle_config: dict | None = None,
) -> str:
    """Phase 1: persist the reproducibility fingerprint skeleton *before* the
    backtest runs, so a mid-run crash still leaves an auditable run_manifest.json behind."""
    if not results_dir:
        raise ValueError("results_dir is required for _dump_run_manifest")
    os.makedirs(results_dir, exist_ok=True)
    db_size = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else None
    db_mtime = _mtime_iso(db_path)
    overrides = copy.deepcopy(cli_overrides) if cli_overrides is not None else {}

    manifest = {
        "schema_version": 1,
        "run_ts": _run_ts_from_results_dir(results_dir),
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_dirty": bool(_git(["status", "--porcelain"])),
        # Phase 1 always says "running"; Phase 2 flips it to "completed" and stamps
        # `completed_at_utc`, so a skeleton that was never finalized is visibly unfinished
        # rather than looking like a complete run (docs/backtest_research_impl.md §1).
        "status": "running",
        "completed_at_utc": None,
        "mode": mode,
        "group": group,
        "strategy": strategy_name,
        "strategy_file": strategy_file,
        "strategy_dump_sha256": _sha256_file(strategy_dump_path),
        "db_path": db_path,
        "db_size_bytes": db_size,
        "db_mtime_iso": db_mtime,
        # `db_path` / `db_size_bytes` / `db_mtime_iso` are the stat of the *configured*
        # cache file; `db_used` is the separate, honest answer to "did this run actually
        # touch it?". A synthetic provider never constructs `DuckDBCache`
        # (`CachedDataProvider.__init__` skips it), and `--no-cache` disables it too, so
        # without this flag a synthetic run would record the size of a production cache it
        # never opened (docs/backtest_research_impl.md §1).
        "db_used": db_used,
        "cli_overrides": overrides,
        "enable_position_lifecycle": bool(enable_position_lifecycle),
        "position_lifecycle_config": position_lifecycle_config,
        # Structured CLI provenance (`{"argv": [...], "options": {...}}`): without it a
        # manifest can name the git SHA, the cache and every artifact but still cannot say
        # with *which* flags the run was launched -- e.g. `--cash-sweep-rate 0.015` was
        # echoed by no artifact at all, so a 1.5%-vs-0% cash-yield difference could only be
        # asserted in prose. Fully known before the run, so Phase 1 writes it and Phase 2
        # only preserves it (docs/backtest_research_impl.md §1 `cli_args`).
        "cli_args": cli_args if cli_args is not None else _cli_args_payload(),
        "metrics": metrics if metrics is not None else dict(_NULL_METRICS),
        # Phase 1 scans the directory: at this point it normally holds only
        # strategy_dump.json, and listing it keeps "the skeleton already accounts for
        # every file it can see" true right up to a mid-run crash (§1 Phase-1 NOTE).
        "artifacts": artifacts if artifacts is not None else _artifact_sizes(results_dir),
    }
    manifest_path = os.path.join(results_dir, "run_manifest.json")
    write_json_report(manifest, manifest_path)
    return manifest_path


def _finalize_run_manifest(
    manifest_path: str,
    *,
    metrics: dict | None = None,
    artifacts: list[dict] | None = None,
    enable_position_lifecycle: bool | None = None,
    position_lifecycle_config: dict | None = None,
) -> str:
    """Phase 2: re-open the Phase-1 skeleton and fill in the post-run facts.

    Deliberately NOT accepting `mode`: which metrics are legitimately fillable is decided
    at the call site (standard must stay all-`null`, walkforward has
    `walkforward_summary.json`), so a `mode` argument here would only ever be discarded --
    a documented-but-ignored parameter is a future-bug magnet
    (docs/backtest_research_impl.md §1). Every other Phase-1 field (`db_path`,
    `db_size_bytes`, `db_mtime_iso`, `db_used`, fingerprints …) survives this call because
    it is a read-modify-write of the same JSON object.
    """
    path = os.path.join(manifest_path, "run_manifest.json") if os.path.isdir(manifest_path) else manifest_path
    if not os.path.exists(path):
        raise FileNotFoundError(f"run manifest skeleton missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["status"] = "completed"
    manifest["completed_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # `cli_args` is a Phase-1 fact (the flags are fully known before the run), so Phase 2
    # keeps the Phase-1 value verbatim via the read-modify-write. The fallback branch below
    # is only a safety net for a skeleton written before this field existed: it fills the
    # `argv` fallback rather than leaving an older manifest with a missing key, which would
    # be indistinguishable from "this run recorded no CLI provenance"
    # (docs/backtest_research_impl.md §1).
    if "cli_args" not in manifest:
        manifest["cli_args"] = _cli_args_payload()
    if enable_position_lifecycle is not None and "enable_position_lifecycle" not in manifest:
        manifest["enable_position_lifecycle"] = bool(enable_position_lifecycle)
    if position_lifecycle_config is not None and "position_lifecycle_config" not in manifest:
        manifest["position_lifecycle_config"] = position_lifecycle_config
    manifest["metrics"] = metrics if metrics is not None else dict(_NULL_METRICS)
    manifest["artifacts"] = artifacts if artifacts is not None else []
    write_json_report(manifest, path)

    res_dir = os.path.dirname(path)
    prov_path = os.path.join(res_dir, "PROVENANCE.json")
    if os.path.exists(prov_path):
        try:
            with open(prov_path, "r", encoding="utf-8") as pf:
                prov = json.load(pf)
            prov["enable_position_lifecycle"] = manifest.get("enable_position_lifecycle")
            prov["position_lifecycle_config"] = manifest.get("position_lifecycle_config")
            write_json_report(prov, prov_path)
        except Exception:
            pass

    return path


def main():
    args = build_arg_parser().parse_args()
    if sum([bool(args.china_trading), bool(args.us_trading), bool(args.hk_trading)]) > 1:
        raise ValueError("Only one of --china-trading, --us-trading, --hk-trading may be enabled.")
    results_dir = args.results_dir or RESULTS_DIR
    cache_dir = args.cache_dir or DATA_DIR

    strategy_def = load_strategy_file(args.strategy_file)

    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    explanation = strategy_def.get("explanation", "")
    pattern_spec = strategy_def.get("pattern_spec")
    research_strategy_spec = strategy_def.get("research_strategy_spec")
    composite_spec = strategy_def.get("composite_spec")
    fundamental_spec = strategy_def.get("fundamental_spec")
    bnn_spec = strategy_def.get("bnn_spec")

    strat_overrides: dict | None = None
    if args.strategy_params:
        try:
            strat_overrides = json.loads(args.strategy_params)
            if not isinstance(strat_overrides, dict):
                raise ValueError(f"--strategy-params must be a JSON object, got {type(strat_overrides).__name__}.")
            params.update(strat_overrides)
            if isinstance(research_strategy_spec, dict) and "entry_data" in research_strategy_spec:
                entry_params = research_strategy_spec["entry_data"].setdefault("parameters", {})
                entry_params.update(strat_overrides)
        except Exception as exc:
            raise ValueError(f"Failed to parse --strategy-params JSON string: {exc}") from exc

    strategy_name = (
        strategy_def.get("strategy_name")
        or strategy_def.get("name")
        or (research_strategy_spec.get("entry_data", {}).get("name") if isinstance(research_strategy_spec, dict) else None)
        or (research_strategy_spec.get("strategy_key") if isinstance(research_strategy_spec, dict) else None)
        or template_name
    )

    strat_label = f"{strategy_name} ({template_name})" if strategy_name != template_name else strategy_name
    print(f"Loaded Strategy: {strat_label}")
    print(f"Parameters: {params}")
    print(f"Logic: {explanation}")
    if "trusted" in strategy_def and not strategy_def["trusted"]:
        print(f"WARNING: this strategy did NOT pass the generator's trust gate "
              f"(ers_passed={strategy_def.get('ers_passed')}, "
              f"ers_percentile={strategy_def.get('ers_percentile')}) -- "
              f"treat these results as exploratory, not validated.")
    print()

    # If min_weight_change was not explicitly supplied via CLI, inherit from strategy params if present
    if getattr(args, "min_weight_change", 0.0) <= 0.0 and isinstance(params, dict):
        strat_min_change = float(
            params.get("min_weight_change")
            or params.get("crb_min_weight_change")
            or params.get("cfsb_min_weight_change")
            or params.get("cdhb_min_weight_change")
            or next((v for k, v in params.items() if k.endswith("_min_weight_change") and isinstance(v, (int, float))), 0.0)
            or 0.0
        )
        if strat_min_change > 0.0:
            args.min_weight_change = strat_min_change

    data_kwargs = build_data_kwargs(args)

    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError("No universe symbols provided or resolved. Pass --universe, --universe-file, or --universe-provider.")

    warmup_template = get_template(
        template_name, pattern_spec, research_strategy_spec, composite_spec, params, fundamental_spec, bnn_spec
    )
    warmup_bars = warmup_template.warmup_bars(params)
    fetch_start = args.start
    if warmup_bars > 0 and args.start:
        # Convert trading bars into calendar days (~7/5 ratio) + 45 days buffer for holidays/weekends
        warmup_calendar_days = int(round(warmup_bars * 7 / 5)) + 45
        fetch_start = (pd.Timestamp(args.start) - pd.Timedelta(days=warmup_calendar_days)).strftime("%Y-%m-%d")

    universe = load_universe_with_banner(universe_symbols, fetch_start, args.end, args.interval,
                                          use_cache=not args.no_cache, cache_dir=cache_dir,
                                          data_kwargs=data_kwargs, require_nonempty=True,
                                          expected_last_session=backtest_expected_last_session())

    # Task B2.1: Memoized Pre-Enrichment across full historical universe
    track_c_flags = {
        "enable_structural_regime",
        "enable_cyq_quality_gate",
        "enable_vp_confluence_sizing",
        "enable_spring_fast_entry",
        "enable_structural_chandelier_stop",
    }
    track_t_flags = {
        "enable_intraday_5m",
        "enable_5m_interval_nesting",
        "enable_5m_stop_filter",
        "use_intraday_downside_vol",
    }
    has_track_c = False
    has_track_t = False
    if isinstance(params, dict):
        has_track_c = any(
            any(k.endswith(f) and bool(v) for f in track_c_flags)
            for k, v in params.items()
        )
        has_track_t = any(
            any(k.endswith(f) and bool(v) for f in track_t_flags)
            for k, v in params.items()
        )
    if template_name in ("wyckoff_chan_spring", "chan_vp_hybrid"):
        has_track_c = True

    if has_track_c:
        from common.structural_factors import enrich_universe_with_structural_features
        universe = enrich_universe_with_structural_features(universe)

    if has_track_t:
        from common.intraday_features import enrich_universe_with_intraday_features, load_intraday_5m_universe
        intraday_data = load_intraday_5m_universe(
            universe_symbols,
            fetch_start,
            args.end,
            cache_dir=cache_dir,
        )
        if intraday_data:
            cutoff_str = (
                params.get("blend_intraday_cutoff_time")
                or params.get("intraday_cutoff_time")
                or "14:15"
            )
            universe = enrich_universe_with_intraday_features(universe, intraday_data, cutoff_time=cutoff_str)

    os.makedirs(results_dir, exist_ok=True)

    # Populated inside the --optimize block below with whichever of
    # original_result/opt["best_result"] ends up matching the final `params`
    # -- reused as-is by the mode branches below instead of re-running
    # run_standard/run_walkforward a second time for a result already in
    # hand. Stays None (no reuse, zero behavior change) when --optimize
    # isn't set.
    reused_result = None

    if args.optimize:
        optimize_template_instance = get_template(
            template_name, pattern_spec, research_strategy_spec, composite_spec, params, fundamental_spec,
            bnn_spec,
        )
        score_fn = _standard_score_fn(universe, args) if args.mode == "standard" else _walkforward_score_fn(universe, args)

        original_result = score_fn(optimize_template_instance, params)

        opt = optimize_template(
            universe, optimize_template_instance, score_fn,
            n_random_search=args.n_random_search,
            ers_percentile_threshold=args.ers_percentile_threshold,
            min_rebalances_for_trust=args.min_rebalances_for_trust,
        )

        original_sharpe = original_result.get("sharpe_ratio", float("-inf"))
        best_sharpe = opt["best_result"].get("sharpe_ratio", float("-inf"))
        status = "success" if opt["trusted"] else "failed"
        reason = None
        if not opt["trusted"]:
            if not opt["ers_passed"]:
                reason = (f"ERS percentile {opt['ers_percentile']:.2f} < required "
                           f"{args.ers_percentile_threshold:.2f}")
            else:
                reason = (f"winning combo's total_rebalances "
                           f"({opt['best_result'].get('total_rebalances', 0)}) < "
                           f"--min-rebalances-for-trust ({args.min_rebalances_for_trust})")

        optimize_report = {
            "status": status, "reason": reason,
            "original_params": params, "original_result": original_result,
            "best_params": opt["best_params"], "best_result": opt["best_result"],
            "ers_percentile": opt["ers_percentile"], "ers_passed": opt["ers_passed"], "trusted": opt["trusted"],
            "n_trials": opt["n_trials"],
            "improvement": {
                "sharpe_ratio": best_sharpe - original_sharpe,
                "cagr": opt["best_result"].get("cagr", float("nan")) - original_result.get("cagr", float("nan")),
            },
        }
        optimize_report_path = os.path.join(results_dir, "optimize_report.json")
        write_json_report(optimize_report, optimize_report_path)

        print(f"\n=== Optimize: {status} ===")
        if opt["trusted"]:
            if opt["best_params"]:
                params = opt["best_params"]
                reused_result = opt["best_result"]
                print(f"\nOptimization TRUSTED — switching to best_params: {params}")
            else:
                reused_result = original_result
                print(
                    "\nWARNING: --optimize produced an empty best_params "
                    "(template param_grid is empty); keeping original params"
                )
        else:
            print(f"  Tuning did NOT pass validation ({reason}) -- falling back to original params {params}.")
            # Same reuse argument as above, but for original_result (already
            # score_fn(optimize_template_instance, params)'s result for
            # these exact original params).
            reused_result = original_result
        print(f"Saved optimize report to {optimize_report_path}")

    candidate_db = os.path.join(cache_dir, "cache.duckdb")
    db_path = candidate_db if os.path.exists(candidate_db) else os.path.join(DATA_DIR, "cache.duckdb")
    # `db_path` is only the *configured* cache location (a `stat`, never an open). Whether
    # this run actually read/wrote it is a separate fact, and the two must not be conflated:
    # a synthetic provider never constructs `DuckDBCache` at all
    # (`CachedDataProvider.__init__` -> `is_synthetic_provider`), and `--no-cache`
    # (`use_cache=not args.no_cache` at every load call) disables it too. Without this flag
    # a synthetic run would record a 300MB+ production `cache.duckdb` it never opened --
    # a pure provenance lie (docs/backtest_research_impl.md §1).
    db_used = (not is_synthetic_provider(args.data_provider)) and not args.no_cache

    dump_path = _dump_effective_strategy_def(
        strategy_def,
        params,
        research_strategy_spec,
        results_dir,
        cli_overrides=strat_overrides,
    )
    print(f"Effective strategy dump saved to {dump_path}")

    from common.position_lifecycle import resolve_position_lifecycle_config
    enable_pl = getattr(args, "enable_position_lifecycle", True)
    pl_cfg = resolve_position_lifecycle_config(params, args) if enable_pl else None
    pl_cfg_dict = pl_cfg.to_dict() if pl_cfg is not None else None

    manifest_path = _dump_run_manifest(
        results_dir,
        mode=args.mode,
        group=_group_from_results_dir(results_dir),
        strategy_name=strategy_name,
        strategy_file=args.strategy_file,
        strategy_dump_path=dump_path,
        db_path=db_path,
        db_used=db_used,
        cli_overrides=strat_overrides,
        # Derived from the *parsed* namespace, not re-parsed from sys.argv inside the
        # helper: this is the single moment where what the CLI actually became (defaults
        # applied, store_true resolved, type coercion done) is known.
        cli_args=_cli_args_payload(args),
        metrics=None,          # standard/walkforward always start as six nulls (§1)
        artifacts=None,        # Phase 1 -> scan the dir: only strategy_dump.json exists yet
        enable_position_lifecycle=enable_pl,
        position_lifecycle_config=pl_cfg_dict,
    )

    wf_summary: dict | None = None

    if args.mode == "standard":
        print(f"\n=== Running Standard Backtest: {strategy_name} ===")
        if reused_result is not None:
            result = reused_result
        else:
            result = run_standard(
                universe,
                get_template(template_name, pattern_spec, research_strategy_spec, composite_spec, params, fundamental_spec, bnn_spec),
                params, args,
            )

        print(f"\n=== Strategy: {strategy_name} ===")
        print(format_backtest_metrics_summary(result))
        print(f"Total Rebalances: {result['total_rebalances']}")
        print(f"Total Turnover: {result['total_turnover']:.2f}")

        out_path = os.path.join(results_dir, "backtest_equity.csv")
        result["equity_curve"].to_csv(out_path)
        print(f"\nSaved equity curve to {out_path}")

        weights_path = os.path.join(results_dir, "backtest_weights.csv")
        result["actual_weights"].to_csv(weights_path)
        print(f"Saved actual daily weights to {weights_path}")

        rebal_df = result.get("rebalance_report", pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS))
        rebalance_report_path = os.path.join(results_dir, "rebalance_report.csv")
        rebal_df.to_csv(rebalance_report_path, index=False)
        print(f"Saved rebalance report to {rebalance_report_path}")
        print(f"Total Rebalance Trades: {len(rebal_df)}")
        if not rebal_df.empty:
            print("\nRecent Rebalance Trades:")
            print(format_rebalance_trades_preview(rebal_df))

        # (F3) Position-lifecycle decision log. Until now `result["lifecycle_records"]`
        # -- the UPLSM's per-(date, symbol) BUY/SELL/HOLD/gate verdict plus its
        # structured `blocking_reason` -- was computed, carried out of the engine and
        # then thrown away by every artifact writer, so an operator could see `SELL`
        # in rebalance_report.csv but never WHY the gate had blocked the trade on the
        # preceding days.
        # Deliberately a NEW file: `backtest_equity.csv`, `backtest_weights.csv` and
        # `rebalance_report.csv` above are untouched (L2.37 -- their content, columns
        # and ordering stay bit-identical). OFF runs write nothing at all: with the
        # layer disabled there are no decisions to log, and an empty file would be
        # indistinguishable from "the layer ran and never traded".
        if enable_pl:
            lifecycle_records_path = os.path.join(results_dir, "lifecycle_records.csv")
            lifecycle_records_to_frame(result.get("lifecycle_records")).to_csv(
                lifecycle_records_path, index=False
            )
            print(f"Saved {len(result.get('lifecycle_records') or [])} lifecycle decision record(s) "
                  f"to {lifecycle_records_path}")

        # Lifecycle A/B comparison arm
        if enable_pl and getattr(args, "lifecycle_compare", False):
            off_m = result.get("off_metrics", {})
            sr_on = float(result.get("sharpe_ratio", 0.0) or 0.0)
            sr_off = float(off_m.get("sharpe_ratio", 0.0) or 0.0)
            sign_reversal = bool((sr_on > 0 and sr_off < 0) or (sr_on < 0 and sr_off > 0))
            ab_df = pd.DataFrame([{
                "mode": "standard",
                "sharpe_on": sr_on,
                "cagr_on": float(result.get("cagr", 0.0) or 0.0),
                "max_drawdown_on": float(result.get("max_drawdown", 0.0) or 0.0),
                "turnover_on": float(result.get("total_turnover", 0.0) or 0.0),
                "rebalances_on": int(result.get("total_rebalances", 0) or 0),
                "sharpe_off": sr_off,
                "cagr_off": float(off_m.get("cagr", 0.0) or 0.0),
                "max_drawdown_off": float(off_m.get("max_drawdown", 0.0) or 0.0),
                "turnover_off": float(off_m.get("total_turnover", 0.0) or 0.0),
                "rebalances_off": int(off_m.get("total_rebalances", 0) or 0),
                "sharpe_sign_reversal": sign_reversal,
            }])
            ab_path = os.path.join(results_dir, "lifecycle_ab_comparison.csv")
            ab_df.to_csv(ab_path, index=False)
            print(f"Saved lifecycle A/B comparison to {ab_path}")

        baseline_result = None
        if args.baseline_symbol:
            main_calendar, _ = _resolve_master_calendar(universe, args.start, args.end, min_coverage=0.20)
            baseline_result, baseline_params = _run_baseline(
                args, cache_dir, data_kwargs, aligned_index=main_calendar
            )
            comparison = _compute_standard_comparison(result, baseline_result)

            print(f"\n=== Baseline Comparison: {args.baseline_symbol} ({args.baseline_template}) ===")
            print(f"Baseline Sharpe Ratio: {baseline_result['sharpe_ratio']:.2f} | "
                  f"Baseline CAGR: {baseline_result['cagr']*100:.2f}% | "
                  f"Baseline Max Drawdown: {baseline_result['max_drawdown']*100:.1f}%")
            print(f"Alpha (annualized): {comparison['alpha']*100:.2f}% | Beta: {comparison['beta']:.2f}")
            print(f"Tracking Error: {comparison['tracking_error']*100:.2f}% | "
                  f"Information Ratio: {comparison['information_ratio']:.2f}")
            print(f"Outperformance CAGR: {comparison['outperformance_cagr']*100:.2f}%")

            baseline_equity_path = os.path.join(results_dir, "baseline_equity.csv")
            baseline_result["equity_curve"].to_csv(baseline_equity_path)
            print(f"Saved baseline equity curve to {baseline_equity_path}")

            comparison_report = {
                "strategy": strategy_name,
                "strategy_name": strategy_name,
                "baseline_symbol": args.baseline_symbol,
                "baseline_template": args.baseline_template,
                "baseline_params": baseline_params,
                "baseline_sharpe_ratio": baseline_result["sharpe_ratio"],
                "baseline_cagr": baseline_result["cagr"],
                "baseline_max_drawdown": baseline_result["max_drawdown"],
                "baseline_calmar_ratio": baseline_result["calmar_ratio"],
                "strategy_sharpe_ratio": result["sharpe_ratio"],
                "strategy_cagr": result["cagr"],
                **comparison,
            }
            comparison_report_path = os.path.join(results_dir, "comparison_report.json")
            write_json_report(comparison_report, comparison_report_path)
            print(f"Saved comparison report to {comparison_report_path}")

        if not args.no_plots:
            equity_series = result["equity_curve"]["equity"]
            baseline_series = None
            baseline_chart_label = "Baseline"
            if args.baseline_symbol and baseline_result is not None:
                baseline_series = baseline_result["equity_curve"]["equity"]
                baseline_chart_label = args.baseline_symbol
            chart_path = plotting.plot_equity_curve(
                equity_series, results_dir, baseline=baseline_series, baseline_label=baseline_chart_label,
                strategy_label=template_name, title=f"{template_name} Equity Curve",
            )
            print(f"Saved equity curve chart to {chart_path}")

    elif args.mode == "walkforward":
        print(f"\n=== Running Walkforward Rolling Evaluation: {strategy_name} ===")
        print(f"Window: {args.window_years} years, Step: {args.step_years} years")

        if reused_result is not None:
            folds = reused_result["folds"]
        else:
            folds = run_walkforward(
                universe,
                get_template(template_name, pattern_spec, research_strategy_spec, composite_spec, params, fundamental_spec, bnn_spec),
                params, args,
            )

        all_wf_trades = []
        for fold_idx, f in enumerate(folds, 1):
            f_trades = f.get("rebalance_report")
            if f_trades is not None and not f_trades.empty:
                df = f_trades.copy()
                df.insert(0, "fold", fold_idx)
                all_wf_trades.append(df)
        if all_wf_trades:
            wf_trades_df = pd.concat(all_wf_trades, ignore_index=True)
        else:
            wf_trades_df = pd.DataFrame(columns=["fold"] + REBALANCE_REPORT_COLUMNS)

        all_wf_weights = []
        for fold_idx, f in enumerate(folds, 1):
            f_weights = f.get("actual_weights")
            if f_weights is not None and not f_weights.empty:
                df_w = f_weights.copy()
                df_w.insert(0, "fold", fold_idx)
                all_wf_weights.append(df_w)
        if all_wf_weights:
            wf_weights_df = pd.concat(all_wf_weights)
        else:
            wf_weights_df = pd.DataFrame()

        folds_clean = [
            {k: v for k, v in f.items()
             if k not in ("rebalance_report", "actual_weights", "off_metrics", "lifecycle_records")}
            for f in folds
        ]
        folds_df = pd.DataFrame(folds_clean)

        baseline_params = None
        baseline_calendar_mismatch = False
        if args.baseline_symbol:
            main_calendar, _ = _resolve_master_calendar(universe, args.start, args.end, min_coverage=0.20)
            baseline_folds, baseline_params = _run_baseline(
                args, cache_dir, data_kwargs, aligned_index=main_calendar
            )
            baseline_folds_df = pd.DataFrame(baseline_folds)
            folds_df, baseline_calendar_mismatch = _merge_baseline_folds(folds_df, baseline_folds_df)

        print("\nRolling Windows Performance:")
        print(format_walkforward_performance_table(folds_df))

        valid_sharpes = folds_df["sharpe_ratio"].dropna()
        n_valid_folds = len(valid_sharpes)
        dsr = float("nan")
        sharpe_std = float("nan")
        if n_valid_folds >= 2:
            sharpe_std = float(valid_sharpes.std(ddof=1))
            dsr = deflated_sharpe_ratio(
                observed_sharpe=float(valid_sharpes.mean()),
                n_trials=n_valid_folds,
                n_obs=_resolve_window_bars(args.window_years),
                sharpe_std=sharpe_std,
            )

        print(f"\n=== Walkforward Summary: {strategy_name} ===")
        print(f"Mean Sharpe Ratio: {folds_df['sharpe_ratio'].mean():.2f} | "
              f"Mean CAGR: {folds_df['cagr'].mean()*100:.2f}%")
        print(f"Mean Max Drawdown: {folds_df['max_drawdown'].mean()*100:.1f}% | "
              f"Mean Calmar Ratio: {folds_df['calmar_ratio'].mean():.2f}")
        dsr_str = f"{dsr:.3f}" if np.isfinite(dsr) else "N/A"
        std_str = f"{sharpe_std:.3f}" if np.isfinite(sharpe_std) else "N/A"
        print(f"Deflated Sharpe Ratio: {dsr_str} (n_trials={n_valid_folds}, fold Sharpe std={std_str})")

        if args.baseline_symbol:
            mean_baseline_sharpe = folds_df["baseline_sharpe_ratio"].mean()
            mean_baseline_cagr = folds_df["baseline_cagr"].mean()
            mean_outperformance = folds_df["outperformance"].mean()
            print(f"Mean Baseline Sharpe Ratio: {mean_baseline_sharpe:.2f} | "
                  f"Mean Baseline CAGR: {mean_baseline_cagr*100:.2f}%")
            print(f"Mean Outperformance CAGR: {mean_outperformance*100:.2f}%")

        summary = {
            "strategy": strategy_name,
            "strategy_name": strategy_name,
            "mean_sharpe_ratio": float(folds_df["sharpe_ratio"].mean()),
            "mean_cagr": float(folds_df["cagr"].mean()),
            "mean_max_drawdown": float(folds_df["max_drawdown"].mean()),
            "mean_calmar_ratio": float(folds_df["calmar_ratio"].mean()),
            "n_folds": int(len(folds_df)),
            "n_valid_folds": n_valid_folds,
            "fold_sharpe_std": sharpe_std,
            "deflated_sharpe_ratio": dsr,
            "requested_start": args.start,
            "requested_end": args.end,
            "actual_first_fold_start": folds_df["start_date"].iloc[0],
            "actual_last_fold_end": folds_df["end_date"].iloc[-1],
            "rolling_window_performance": folds_df.to_dict(orient="records"),
        }
        wf_summary = summary
        shortfall_days = (pd.Timestamp(args.end) - pd.Timestamp(folds_df["end_date"].iloc[-1])).days
        # A fold needs a FULL window to fit (run_walkforward's `while start_idx + window_bars <=
        # n_bars`), so up to (step_bars - 1) trading days of already-loaded data are always left
        # unevaluated at the tail -- normal, not a bug. Scale the "is this shortfall suspicious"
        # threshold to that expected slack (~7/5 calendar-to-trading-day ratio, plus a small
        # holiday buffer) instead of a flat cutoff, so ordinary --step-years rounding doesn't
        # trigger a false-positive "data didn't reach --end" note.
        expected_max_shortfall_days = int(round(args.step_years * 252 * 7 / 5)) + 14
        if shortfall_days > expected_max_shortfall_days:
            print(f"\nNOTE: the last walk-forward fold ends {folds_df['end_date'].iloc[-1]}, "
                  f"{shortfall_days} day(s) short of the requested --end {args.end}. Up to "
                  f"(--step-years worth of bars) short is expected (a fold needs a full window to "
                  f"fit); a shortfall this large usually means the loaded universe's actual data "
                  f"didn't reach --end -- check for warnings above and the OHLCV cache under {cache_dir}.")
        summary_path = os.path.join(results_dir, "walkforward_summary.json")
        write_json_report(summary, summary_path)
        print(f"Saved walkforward summary to {summary_path}")

        if args.baseline_symbol:
            comparison_report = {
                "strategy": strategy_name,
                "strategy_name": strategy_name,
                "baseline_symbol": args.baseline_symbol,
                "baseline_template": args.baseline_template,
                "baseline_params": baseline_params,
                "mean_baseline_sharpe_ratio": float(folds_df["baseline_sharpe_ratio"].mean()),
                "mean_baseline_cagr": float(folds_df["baseline_cagr"].mean()),
                "mean_outperformance_cagr": float(folds_df["outperformance"].mean()),
                "baseline_calendar_mismatch": baseline_calendar_mismatch,
            }
            comparison_report_path = os.path.join(results_dir, "comparison_report.json")
            write_json_report(comparison_report, comparison_report_path)
            print(f"Saved comparison report to {comparison_report_path}")

        out_path = os.path.join(results_dir, "walkforward_report.csv")
        folds_df.to_csv(out_path, index=False)
        print(f"\nSaved walkforward report to {out_path}")

        wf_trades_path = os.path.join(results_dir, "walkforward_rebalances.csv")
        wf_trades_df.to_csv(wf_trades_path, index=False)
        print(f"Saved walkforward rebalance report to {wf_trades_path}")
        print(f"Total Walkforward Rebalance Trades: {len(wf_trades_df)}")
        if not wf_trades_df.empty:
            print("\nRecent Walkforward Rebalance Trades:")
            print(format_rebalance_trades_preview(wf_trades_df))

        wf_weights_path = os.path.join(results_dir, "walkforward_weights.csv")
        wf_weights_df.to_csv(wf_weights_path, index=True, index_label="date")
        print(f"Saved walkforward actual weights to {wf_weights_path}")

        # (F3) Per-fold lifecycle decision log, rolled up exactly like
        # `walkforward_rebalances.csv` (one `fold` column prepended, 1-based fold
        # numbering matching the summary table). Same contract as the standard
        # mode: a NEW artifact only -- `walkforward_report.csv` /
        # `walkforward_summary.json` / `walkforward_rebalances.csv` /
        # `walkforward_weights.csv` keep their exact columns and values (L2.37),
        # which is why `lifecycle_records` is stripped in `folds_clean` above.
        if enable_pl:
            wf_lifecycle_frames = []
            for fold_idx, f in enumerate(folds, 1):
                fold_records = f.get("lifecycle_records") or []
                if fold_records:
                    wf_lifecycle_frames.append(lifecycle_records_to_frame(fold_records, fold_index=fold_idx))
            if wf_lifecycle_frames:
                wf_lifecycle_df = pd.concat(wf_lifecycle_frames, ignore_index=True)
            else:
                # Header-only file: the layer ran but no fold produced a decision.
                wf_lifecycle_df = lifecycle_records_to_frame([], fold_index=1)
            wf_lifecycle_path = os.path.join(results_dir, "walkforward_lifecycle_records.csv")
            wf_lifecycle_df.to_csv(wf_lifecycle_path, index=False)
            print(f"Saved {len(wf_lifecycle_df)} walkforward lifecycle decision record(s) "
                  f"to {wf_lifecycle_path}")

        if enable_pl and getattr(args, "lifecycle_compare", False):
            ab_rows = []
            for fold_idx, f in enumerate(folds, 1):
                f_off = f.get("off_metrics", {})
                sr_on = float(f.get("sharpe_ratio", 0.0) or 0.0)
                sr_off = float(f_off.get("sharpe_ratio", 0.0) or 0.0)
                sign_reversal = bool((sr_on > 0 and sr_off < 0) or (sr_on < 0 and sr_off > 0))
                ab_rows.append({
                    "fold": fold_idx,
                    "start_date": f.get("start_date"),
                    "end_date": f.get("end_date"),
                    "sharpe_on": sr_on,
                    "cagr_on": float(f.get("cagr", 0.0) or 0.0),
                    "max_drawdown_on": float(f.get("max_drawdown", 0.0) or 0.0),
                    "turnover_on": float(f.get("total_turnover", 0.0) or 0.0),
                    "rebalances_on": int(f.get("total_rebalances", 0) or 0),
                    "sharpe_off": sr_off,
                    "cagr_off": float(f_off.get("cagr", 0.0) or 0.0),
                    "max_drawdown_off": float(f_off.get("max_drawdown", 0.0) or 0.0),
                    "turnover_off": float(f_off.get("total_turnover", 0.0) or 0.0),
                    "rebalances_off": int(f_off.get("total_rebalances", 0) or 0),
                    "sharpe_sign_reversal": sign_reversal,
                })
            if ab_rows:
                ab_df = pd.DataFrame(ab_rows)
                ab_path = os.path.join(results_dir, "lifecycle_ab_comparison.csv")
                ab_df.to_csv(ab_path, index=False)
                print(f"Saved lifecycle A/B comparison to {ab_path}")

    _finalize_run_manifest(
        manifest_path,
        metrics=_extract_walkforward_metrics(wf_summary) if args.mode == "walkforward" else None,
        artifacts=_artifact_sizes(results_dir),
        enable_position_lifecycle=enable_pl,
        position_lifecycle_config=pl_cfg_dict,
    )


if __name__ == "__main__":
    main()

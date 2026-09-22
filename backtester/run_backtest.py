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
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

# Add the repo root to sys.path to allow importing from common
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.allocation_backtester import REBALANCE_REPORT_COLUMNS, run_allocation_backtest
from common.allocation_search import optimize_template
from common.allocation_templates import ALLOCATION_TEMPLATES
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone Basket Allocation Backtester")
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


def run_standard(universe: dict, template, params: dict, args) -> dict:
    master_calendar, effective_start = _resolve_master_calendar(
        universe, getattr(args, "start", None), getattr(args, "end", None), min_coverage=0.20
    )
    if master_calendar.empty:
        raise ValueError("Universe contains no valid trading dates.")

    target_weights = template.generate_weights(universe, params)
    if target_weights.empty:
        raise ValueError("Template generated empty weights.")

    eval_index = master_calendar[master_calendar >= effective_start]
    eval_universe = _slice_universe_for_range(universe, eval_index)
    eval_symbols = list(eval_universe.keys())
    if len(eval_index) > 0 and len(eval_index) < len(target_weights):
        eval_weights = target_weights.reindex(index=eval_index, columns=eval_symbols)
        eval_weights.iloc[0] = target_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]
        target_weights = eval_weights
    else:
        target_weights = target_weights.reindex(index=eval_index, columns=eval_symbols)

    result = run_allocation_backtest(
        eval_universe, target_weights,
        initial_capital=args.initial_capital,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct,
        min_shares=getattr(args, "min_shares", 1),
        china_trading=getattr(args, "china_trading", False),
        us_trading=getattr(args, "us_trading", False),
        hk_trading=getattr(args, "hk_trading", False),
    )

    if result["equity_curve"].empty:
        raise ValueError("Backtest produced empty equity curve.")

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

    def _nan_fold_metrics() -> dict:
        return {
            "sharpe_ratio": float("nan"), "cagr": float("nan"), "max_drawdown": float("nan"),
            "calmar_ratio": float("nan"), "win_rate": float("nan"), "profit_factor": float("nan"),
            "total_turnover": 0.0, "total_rebalances": 0,
            "rebalance_report": pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS),
        }

    total_folds = 0
    s_idx = base_start_idx
    while s_idx + window_bars <= n_bars:
        total_folds += 1
        s_idx += step_bars

    folds = []
    start_idx = base_start_idx
    fold_idx = 0
    while start_idx + window_bars <= n_bars:
        fold_idx += 1
        end_idx = start_idx + window_bars
        buffer_start_idx = max(0, start_idx - warmup_bars)

        fold_dates = master_calendar[buffer_start_idx:end_idx]
        buffered_universe = _slice_universe_for_range(universe, fold_dates)
        eval_index = master_calendar[start_idx:end_idx]

        start_date = master_calendar[start_idx].strftime("%Y-%m-%d")
        end_date = master_calendar[end_idx - 1].strftime("%Y-%m-%d")
        print(f"[Fold {fold_idx}/{total_folds}] Evaluating {start_date} to {end_date}...", flush=True)

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
                    eval_weights = full_weights.reindex(index=eval_index, columns=eval_symbols)
                    eval_weights.iloc[0] = full_weights.ffill().reindex(index=eval_index, columns=eval_symbols).iloc[0]

                    result = run_allocation_backtest(
                        eval_universe, eval_weights,
                        initial_capital=args.initial_capital,
                        commission_pct=args.commission_pct,
                        slippage_pct=args.slippage_pct,
                        min_shares=getattr(args, "min_shares", 1),
                        china_trading=getattr(args, "china_trading", False),
                        us_trading=getattr(args, "us_trading", False),
                        hk_trading=getattr(args, "hk_trading", False),
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
                        }
        except Exception as e:
            print(f"Error in window {start_date} to {end_date}: {e}")
            fold_metrics = _nan_fold_metrics()

        sr = fold_metrics.get("sharpe_ratio", float("nan"))
        if np.isnan(sr):
            print(f"  [Fold {fold_idx}/{total_folds}] Done: NaN metrics", flush=True)
        else:
            print(
                f"  [Fold {fold_idx}/{total_folds}] Done: Sharpe={sr:.2f}, "
                f"CAGR={fold_metrics['cagr']:.1%}, MaxDD={fold_metrics['max_drawdown']:.1%}, "
                f"Rebalances={fold_metrics['total_rebalances']}",
                flush=True,
            )

        folds.append({"start_date": start_date, "end_date": end_date, **fold_metrics})

        start_idx += step_bars

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
    baseline_args.china_trading = False
    baseline_args.us_trading = False
    baseline_args.hk_trading = False

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
                                          data_kwargs=data_kwargs, require_nonempty=True)

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
            print(f"  Tuned params {params} -> {opt['best_params']} "
                  f"(Sharpe {original_sharpe:.2f} -> {best_sharpe:.2f}, ERS percentile {opt['ers_percentile']:.2f})")
            params = opt["best_params"]
            # opt["best_result"] is score_fn(optimize_template_instance, best_params)'s
            # result -- for standard mode that IS run_standard's own return
            # value verbatim (_standard_score_fn's score_fn just returns it),
            # and for walkforward mode its "folds" key IS run_walkforward's
            # own return value verbatim (_walkforward_score_fn's score_fn
            # passes it through unmodified) -- so re-running run_standard/
            # run_walkforward below for these exact same (template, params)
            # would just recompute an answer already in hand.
            reused_result = opt["best_result"]
        else:
            print(f"  Tuning did NOT pass validation ({reason}) -- falling back to original params {params}.")
            # Same reuse argument as above, but for original_result (already
            # score_fn(optimize_template_instance, params)'s result for
            # these exact original params).
            reused_result = original_result
        print(f"Saved optimize report to {optimize_report_path}")

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

        folds_clean = [{k: v for k, v in f.items() if k != "rebalance_report"} for f in folds]
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


if __name__ == "__main__":
    main()

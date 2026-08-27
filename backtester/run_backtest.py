#!/usr/bin/env python
"""CLI entry point: evaluate a generated allocation strategy on a basket of assets.

Loads a `strategy.json` file exported by the strategy_generator and evaluates
those fixed rules on a new basket of assets.

Modes:
- standard: Evaluates the strategy over the full date range.
- walkforward: Evaluates the fixed strategy parameters over rolling time windows
               to measure consistency (no re-optimization).

Example:
    python run_backtest.py --strategy-file ../strategy_generator/results/strategy.json --universe SPY QQQ AAPL --mode standard
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

# Add the parent directory to sys.path to allow importing from common
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import run_allocation_backtest
from common.allocation_search import optimize_template
from common.allocation_templates import ALLOCATION_TEMPLATES, PatternBasedAllocationTemplate
from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.metrics import alpha_beta, deflated_sharpe_ratio, information_ratio, tracking_error
from common import plotting
from common.reporting import format_backtest_metrics_summary, write_json_report
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
    add_data_provider_cli_args(p)
    p.add_argument("--results-dir", type=str, default=None,
                   help=f"Override the output directory for equity/weights/report CSVs (default: {RESULTS_DIR})")
    p.add_argument("--cache-dir", type=str, default=None,
                   help=f"Override the shared, workspace-wide OHLCV CSV cache directory (default: {DATA_DIR})")
    p.add_argument("--no-plots", action="store_true",
                   help="Skip the equity-curve chart normally produced in --mode standard (charts are ON by default).")
    return p


def _align_universe(universe: dict) -> dict:
    """Walk-forward's fold boundaries are bar-position-based, so every
    symbol must share the same trading calendar -- trim to the intersection
    of all symbols' dates (an inner join)."""
    common_index = None
    for df in universe.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    return {symbol: df.loc[common_index] for symbol, df in universe.items()}


def _get_template(template_name: str, pattern_spec: dict = None, research_strategy_spec: dict = None,
                   composite_spec: dict = None, params: dict = None):
    """Looks up a static template by name, UNLESS `pattern_spec`,
    `research_strategy_spec`, or `composite_spec` is given (all three are
    mutually exclusive -- a winning strategy.json only ever came from one
    source).

    `pattern_spec`: a PatternBasedAllocationTemplate (pattern_mining/
    pmine/pattern_mining.py) is universe-specific and not zero-arg
    constructible, so it's never in the static ALLOCATION_TEMPLATES registry;
    a strategy.json produced from a winning mined pattern carries its own
    `pattern_spec` (see run_strategygen.py) so it can be reconstructed here
    instead.

    `research_strategy_spec`: a strategy.json produced from a winning
    research_strategy candidate (strategy_generator search) carries its own
    `research_strategy_spec` (`strategy_key` + `entry_data`) so the exact
    research_strategy strategy instance -- class-based or natural-language-
    parsed alike -- can be reconstructed here via research_strategy's own
    `instantiate_strategy_from_config_entry`, instead of being in the static
    ALLOCATION_TEMPLATES registry.

    `composite_spec`: a strategy.json produced from a winning aspect-composed
    hybrid (see strategy_generator/stratgen/generator.py's
    GeneratorConfig.enable_aspect_composition) carries its own
    `composite_spec` (`track` plus either `selection_key`/`weighting_key` or
    `entry_key`/`exit_key`) so the exact CompositeAllocationTemplate/
    CompositeTimingTemplate instance can be rebuilt here from
    common.strategy_aspects/research_strategy.rs.timing_aspects's registries.
    `params` (the strategy.json's own top-level saved params) is passed
    through as the reconstructed composite's `default_params` -- required so
    `--optimize`'s fresh grid search (which degenerates to a single `{}`
    trial for a Track B composite, or omits the shared `rebalance_freq_days`
    for a Track A one) still falls back to the actually-tuned values instead
    of each aspect's own generic hardcoded defaults."""
    if research_strategy_spec is not None:
        from research_strategy.rs.strategy import instantiate_strategy_from_config_entry
        return instantiate_strategy_from_config_entry(
            research_strategy_spec["strategy_key"], research_strategy_spec["entry_data"]
        )
    if composite_spec is not None:
        if composite_spec["track"] == "allocation":
            from common.strategy_aspects import SELECTION_ASPECTS, WEIGHTING_ASPECTS, CompositeAllocationTemplate
            return CompositeAllocationTemplate(
                SELECTION_ASPECTS[composite_spec["selection_key"]],
                WEIGHTING_ASPECTS[composite_spec["weighting_key"]],
                default_params=params,
            )
        elif composite_spec["track"] == "timing":
            from research_strategy.rs.timing_aspects import (
                ENTRY_SIGNAL_ASPECTS, EXIT_RISK_ASPECTS, CompositeTimingTemplate,
            )
            return CompositeTimingTemplate(
                ENTRY_SIGNAL_ASPECTS[composite_spec["entry_key"]],
                EXIT_RISK_ASPECTS[composite_spec["exit_key"]],
                default_params=params,
            )
        raise ValueError(f"Unknown composite_spec track: {composite_spec['track']!r} (expected 'allocation' or 'timing')")
    if pattern_spec is not None:
        if not template_name.startswith("pattern_"):
            raise ValueError(
                f"pattern_spec provided but template_name '{template_name}' does not "
                f"start with 'pattern_' -- a pattern_spec block only makes sense for a "
                f"mined pattern-based template; refusing to silently reinterpret "
                f"'{template_name}' as one."
            )
        return PatternBasedAllocationTemplate(
            feature_name=pattern_spec["feature_name"],
            feature_lookback=pattern_spec["feature_lookback"],
            threshold=pattern_spec["threshold"],
            comparison=pattern_spec["comparison"],
            event_type=pattern_spec["event_type"],
            mined_p_value=pattern_spec.get("mined_p_value"),
            mined_n_events=pattern_spec.get("mined_n_events"),
        )
    for cls in ALLOCATION_TEMPLATES:
        if cls.name == template_name:
            return cls()
    raise ValueError(f"Unknown template name: {template_name}")


def _load_strategy_file(path: str) -> dict:
    """Loads and validates a strategy.json file exported by strategy_generator.

    Only `template_name` and `params` are truly required (everything else is
    read via `.get()` downstream); validating both up front turns a bare
    `KeyError` deep in `main()` into one clear error naming every missing/
    malformed key at once.
    """
    with open(path, "r") as f:
        strategy_def = json.load(f)

    missing = [key for key in ("template_name", "params") if key not in strategy_def]
    if missing:
        raise ValueError(
            f"Malformed strategy file '{path}': missing required key(s) {missing}. "
            f"Expected keys: template_name (str), params (dict), and optionally "
            f"explanation/trusted/ers_passed/ers_percentile -- see strategy_generator's "
            f"run_strategygen.py output for the expected shape."
        )
    if not isinstance(strategy_def["params"], dict):
        raise ValueError(
            f"Malformed strategy file '{path}': 'params' must be a JSON object, "
            f"got {type(strategy_def['params']).__name__}."
        )

    pattern_spec = strategy_def.get("pattern_spec")
    if pattern_spec is not None:
        required_pattern_keys = ("feature_name", "feature_lookback", "threshold", "comparison", "event_type")
        missing_pattern_keys = [key for key in required_pattern_keys if key not in pattern_spec]
        if missing_pattern_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'pattern_spec' is missing required "
                f"key(s) {missing_pattern_keys}. Expected keys: {list(required_pattern_keys)} "
                f"(plus optional mined_p_value/mined_n_events) -- see strategy_generator's "
                f"run_strategygen.py pattern-mining output for the expected shape."
            )

    research_strategy_spec = strategy_def.get("research_strategy_spec")
    if research_strategy_spec is not None:
        missing_research_strategy_keys = [
            key for key in ("strategy_key", "entry_data") if key not in research_strategy_spec
        ]
        if missing_research_strategy_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec' is missing "
                f"required key(s) {missing_research_strategy_keys}. Expected keys: "
                f"strategy_key (str), entry_data (dict) -- see strategy_generator's "
                f"research_strategy-candidate output for the expected shape."
            )
        if not isinstance(research_strategy_spec["strategy_key"], str):
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec.strategy_key' "
                f"must be a string, got {type(research_strategy_spec['strategy_key']).__name__}."
            )
        if not isinstance(research_strategy_spec["entry_data"], dict):
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec.entry_data' "
                f"must be a JSON object, got {type(research_strategy_spec['entry_data']).__name__}."
            )

    composite_spec = strategy_def.get("composite_spec")
    if composite_spec is not None:
        if composite_spec.get("track") == "allocation":
            required_composite_keys = ("selection_key", "weighting_key")
        elif composite_spec.get("track") == "timing":
            required_composite_keys = ("entry_key", "exit_key")
        else:
            raise ValueError(
                f"Malformed strategy file '{path}': 'composite_spec.track' must be 'allocation' or "
                f"'timing', got {composite_spec.get('track')!r}."
            )
        missing_composite_keys = [key for key in required_composite_keys if key not in composite_spec]
        if missing_composite_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'composite_spec' (track={composite_spec.get('track')!r}) "
                f"is missing required key(s) {missing_composite_keys}. Expected keys: track, "
                f"{list(required_composite_keys)} -- see strategy_generator's aspect-composition output "
                f"for the expected shape."
            )

    specs_given = [
        s for s in (pattern_spec, research_strategy_spec, composite_spec) if s is not None
    ]
    if len(specs_given) > 1:
        raise ValueError(
            f"Malformed strategy file '{path}': 'pattern_spec', 'research_strategy_spec', and "
            f"'composite_spec' are mutually exclusive -- a strategy.json can only have come from "
            f"one source, never more than one. Refusing to guess which one this file actually means."
        )

    return strategy_def


def _resolve_window_bars(window_years: float) -> int:
    return int(round(window_years * 252))


def run_standard(universe: dict, template, params: dict, args) -> dict:
    target_weights = template.generate_weights(universe, params)
    if target_weights.empty:
        raise ValueError("Template generated empty weights.")

    result = run_allocation_backtest(
        universe, target_weights,
        initial_capital=args.initial_capital,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct
    )

    if result["equity_curve"].empty:
        raise ValueError("Backtest produced empty equity curve.")

    # result already carries sharpe_ratio/cagr/max_drawdown/calmar_ratio/
    # win_rate/profit_factor from run_allocation_backtest -- report those
    # directly rather than recomputing (that recomputation used to disagree
    # in sign with the backtester's own max_drawdown).
    return result


def run_walkforward(universe: dict, template, params: dict, args) -> list:
    aligned = _align_universe(universe)
    if not aligned:
        raise ValueError("Universe alignment resulted in empty data.")

    any_df = next(iter(aligned.values()))
    n_bars = len(any_df)

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
    if window_bars >= n_bars:
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
        }

    folds = []
    start_idx = 0
    while start_idx + window_bars <= n_bars:
        end_idx = start_idx + window_bars
        buffer_start_idx = max(0, start_idx - warmup_bars)

        buffered_universe = {sym: df.iloc[buffer_start_idx:end_idx] for sym, df in aligned.items()}
        eval_index = any_df.index[start_idx:end_idx]

        start_date = any_df.index[start_idx].strftime("%Y-%m-%d")
        end_date = any_df.index[end_idx - 1].strftime("%Y-%m-%d")

        try:
            full_weights = template.generate_weights(buffered_universe, params)
            if full_weights.empty:
                fold_metrics = _nan_fold_metrics()
            else:
                # Restrict to the eval window, but seed its first row with the
                # carried-over (forward-filled) target as of the window's
                # start -- otherwise a fold that starts between two
                # buffer-period rebalances would open in all-cash instead of
                # whatever the (now-warm) strategy actually held at that point.
                eval_weights = full_weights.reindex(eval_index)
                eval_weights.loc[eval_index[0]] = full_weights.ffill().reindex(eval_index).iloc[0]
                eval_universe = {sym: df.loc[eval_index] for sym, df in aligned.items()}

                result = run_allocation_backtest(
                    eval_universe, eval_weights,
                    initial_capital=args.initial_capital,
                    commission_pct=args.commission_pct,
                    slippage_pct=args.slippage_pct
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
                    }
        except Exception as e:
            print(f"Error in window {start_date} to {end_date}: {e}")
            fold_metrics = _nan_fold_metrics()

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
        return {
            "sharpe_ratio": mean_sharpe, "cagr": mean_cagr,
            "max_drawdown": mean_max_drawdown, "calmar_ratio": mean_calmar_ratio,
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


def _run_baseline(args, cache_dir, data_kwargs):
    """Loads --baseline-symbol and runs it through the same run_standard/
    run_walkforward path as the main strategy, using --baseline-template (a
    static template only -- no pattern_spec) and --baseline-params (or that
    template's first param_grid combination). Returns (baseline_out,
    baseline_params)."""
    print(f"\n=== Loading Baseline: {args.baseline_symbol} ({args.baseline_template}) ===")
    baseline_template = _get_template(args.baseline_template)
    baseline_params = _resolve_baseline_params(baseline_template, args.baseline_params)

    baseline_universe = load_universe_with_banner(
        [args.baseline_symbol], args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=cache_dir,
        data_kwargs=data_kwargs, require_nonempty=True,
        cache_max_age_days=args.cache_ttl_days,
    )

    if args.mode == "standard":
        baseline_out = run_standard(baseline_universe, baseline_template, baseline_params, args)
    else:
        baseline_out = run_walkforward(baseline_universe, baseline_template, baseline_params, args)

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
    results_dir = args.results_dir or RESULTS_DIR
    cache_dir = args.cache_dir or DATA_DIR

    strategy_def = _load_strategy_file(args.strategy_file)

    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    explanation = strategy_def.get("explanation", "")
    pattern_spec = strategy_def.get("pattern_spec")
    research_strategy_spec = strategy_def.get("research_strategy_spec")
    composite_spec = strategy_def.get("composite_spec")

    print(f"Loaded Strategy: {template_name}")
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

    universe = load_universe_with_banner(universe_symbols, args.start, args.end, args.interval,
                                          use_cache=not args.no_cache, cache_dir=cache_dir,
                                          data_kwargs=data_kwargs, require_nonempty=True,
                                          cache_max_age_days=args.cache_ttl_days)

    os.makedirs(results_dir, exist_ok=True)

    # Populated inside the --optimize block below with whichever of
    # original_result/opt["best_result"] ends up matching the final `params`
    # -- reused as-is by the mode branches below instead of re-running
    # run_standard/run_walkforward a second time for a result already in
    # hand. Stays None (no reuse, zero behavior change) when --optimize
    # isn't set.
    reused_result = None

    if args.optimize:
        optimize_template_instance = _get_template(
            template_name, pattern_spec, research_strategy_spec, composite_spec, params,
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
        print("\n=== Running Standard Backtest ===")
        if reused_result is not None:
            result = reused_result
        else:
            result = run_standard(
                universe,
                _get_template(template_name, pattern_spec, research_strategy_spec, composite_spec, params),
                params, args,
            )

        print(format_backtest_metrics_summary(result))
        print(f"Total Rebalances: {result['total_rebalances']}")
        print(f"Total Turnover: {result['total_turnover']:.2f}")

        out_path = os.path.join(results_dir, "backtest_equity.csv")
        result["equity_curve"].to_csv(out_path)
        print(f"\nSaved equity curve to {out_path}")

        weights_path = os.path.join(results_dir, "backtest_weights.csv")
        result["actual_weights"].to_csv(weights_path)
        print(f"Saved actual daily weights to {weights_path}")

        baseline_result = None
        if args.baseline_symbol:
            baseline_result, baseline_params = _run_baseline(args, cache_dir, data_kwargs)
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
        print(f"\n=== Running Walkforward Rolling Evaluation ===")
        print(f"Window: {args.window_years} years, Step: {args.step_years} years")

        if reused_result is not None:
            folds = reused_result["folds"]
        else:
            folds = run_walkforward(
                universe,
                _get_template(template_name, pattern_spec, research_strategy_spec, composite_spec, params),
                params, args,
            )

        folds_df = pd.DataFrame(folds)

        baseline_params = None
        baseline_calendar_mismatch = False
        if args.baseline_symbol:
            baseline_folds, baseline_params = _run_baseline(args, cache_dir, data_kwargs)
            baseline_folds_df = pd.DataFrame(baseline_folds)
            folds_df, baseline_calendar_mismatch = _merge_baseline_folds(folds_df, baseline_folds_df)

        print("\nRolling Windows Performance:")
        print(folds_df.to_string(index=False))

        print(f"\nMean Sharpe Ratio: {folds_df['sharpe_ratio'].mean():.2f} | "
              f"Mean CAGR: {folds_df['cagr'].mean()*100:.2f}%")
        print(f"Mean Max Drawdown: {folds_df['max_drawdown'].mean()*100:.1f}% | "
              f"Mean Calmar Ratio: {folds_df['calmar_ratio'].mean():.2f}")

        if args.baseline_symbol:
            mean_baseline_sharpe = folds_df["baseline_sharpe_ratio"].mean()
            mean_baseline_cagr = folds_df["baseline_cagr"].mean()
            mean_outperformance = folds_df["outperformance"].mean()
            print(f"Mean Baseline Sharpe Ratio: {mean_baseline_sharpe:.2f} | "
                  f"Mean Baseline CAGR: {mean_baseline_cagr*100:.2f}%")
            print(f"Mean Outperformance CAGR: {mean_outperformance*100:.2f}%")

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
        summary = {
            "mean_sharpe_ratio": float(folds_df["sharpe_ratio"].mean()),
            "mean_cagr": float(folds_df["cagr"].mean()),
            "mean_max_drawdown": float(folds_df["max_drawdown"].mean()),
            "mean_calmar_ratio": float(folds_df["calmar_ratio"].mean()),
            "n_folds": int(len(folds_df)),
            "n_valid_folds": n_valid_folds,
            "fold_sharpe_std": sharpe_std,
            "deflated_sharpe_ratio": dsr,
        }
        print(f"Deflated Sharpe Ratio: {dsr:.3f} (n_trials={n_valid_folds}, fold Sharpe std={sharpe_std:.3f})")
        summary_path = os.path.join(results_dir, "walkforward_summary.json")
        write_json_report(summary, summary_path)
        print(f"Saved walkforward summary to {summary_path}")

        if args.baseline_symbol:
            comparison_report = {
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


if __name__ == "__main__":
    main()

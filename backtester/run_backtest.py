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
from common.allocation_templates import ALLOCATION_TEMPLATES
from common.data import load_universe
from common.universe import add_universe_cli_args, resolve_universe_from_args

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


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
    p.add_argument("--data-provider", default="yfinance",
                   help="Market data source provider ('yfinance', 'csv', 'synthetic', or custom module specifier string e.g. 'script.py:CustomProvider')")
    p.add_argument("--data-dir", type=str, default=None,
                   help="Folder path for CSV data provider")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--results-dir", type=str, default=None,
                   help=f"Override the output directory for equity/weights/report CSVs (default: {RESULTS_DIR})")
    p.add_argument("--cache-dir", type=str, default=None,
                   help=f"Override the local OHLCV CSV cache directory (default: {DATA_DIR})")
    return p


def _align_universe(universe: dict) -> dict:
    """Walk-forward's fold boundaries are bar-position-based, so every
    symbol must share the same trading calendar -- trim to the intersection
    of all symbols' dates (an inner join)."""
    common_index = None
    for df in universe.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    return {symbol: df.loc[common_index] for symbol, df in universe.items()}


def _get_template(template_name: str):
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

    return strategy_def


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

    window_bars = int(round(args.window_years * 252))
    step_bars = int(round(args.step_years * 252))

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


def main():
    args = build_arg_parser().parse_args()
    results_dir = args.results_dir or RESULTS_DIR
    cache_dir = args.cache_dir or DATA_DIR

    strategy_def = _load_strategy_file(args.strategy_file)

    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    explanation = strategy_def.get("explanation", "")

    print(f"Loaded Strategy: {template_name}")
    print(f"Parameters: {params}")
    print(f"Logic: {explanation}")
    if "trusted" in strategy_def and not strategy_def["trusted"]:
        print(f"WARNING: this strategy did NOT pass the generator's trust gate "
              f"(ers_passed={strategy_def.get('ers_passed')}, "
              f"ers_percentile={strategy_def.get('ers_percentile')}) -- "
              f"treat these results as exploratory, not validated.")
    print()

    data_kwargs = {"provider": args.data_provider}
    if args.data_dir:
        data_kwargs["folder_path"] = args.data_dir

    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError("No universe symbols provided or resolved. Pass --universe, --universe-file, or --universe-provider.")

    print(f"Loading {len(universe_symbols)} symbols ...")
    universe = load_universe(universe_symbols, args.start, args.end, args.interval,
                              use_cache=not args.no_cache, cache_dir=cache_dir, **data_kwargs)
    print(f"Loaded {len(universe)}/{len(universe_symbols)} symbols (see warnings above for any skipped).")
    if not universe:
        raise ValueError("No symbols could be loaded successfully; see warnings above.")

    os.makedirs(results_dir, exist_ok=True)

    if args.mode == "standard":
        print("\n=== Running Standard Backtest ===")
        result = run_standard(universe, _get_template(template_name), params, args)

        print(f"Sharpe Ratio: {result['sharpe_ratio']:.2f} | CAGR: {result['cagr']*100:.2f}% | "
              f"Max Drawdown: {result['max_drawdown']*100:.1f}%")
        print(f"Calmar Ratio: {result['calmar_ratio']:.2f} | Win Rate: {result['win_rate']*100:.1f}% | "
              f"Profit Factor: {result['profit_factor']:.2f}")
        print(f"Total Rebalances: {result['total_rebalances']}")
        print(f"Total Turnover: {result['total_turnover']:.2f}")

        out_path = os.path.join(results_dir, "backtest_equity.csv")
        result["equity_curve"].to_csv(out_path)
        print(f"\nSaved equity curve to {out_path}")

        weights_path = os.path.join(results_dir, "backtest_weights.csv")
        result["actual_weights"].to_csv(weights_path)
        print(f"Saved actual daily weights to {weights_path}")

    elif args.mode == "walkforward":
        print(f"\n=== Running Walkforward Rolling Evaluation ===")
        print(f"Window: {args.window_years} years, Step: {args.step_years} years")

        folds = run_walkforward(universe, _get_template(template_name), params, args)

        folds_df = pd.DataFrame(folds)
        print("\nRolling Windows Performance:")
        print(folds_df.to_string(index=False))

        print(f"\nMean Sharpe Ratio: {folds_df['sharpe_ratio'].mean():.2f} | "
              f"Mean CAGR: {folds_df['cagr'].mean()*100:.2f}%")
        print(f"Mean Max Drawdown: {folds_df['max_drawdown'].mean()*100:.1f}% | "
              f"Mean Calmar Ratio: {folds_df['calmar_ratio'].mean():.2f}")

        out_path = os.path.join(results_dir, "walkforward_report.csv")
        folds_df.to_csv(out_path, index=False)
        print(f"\nSaved walkforward report to {out_path}")


if __name__ == "__main__":
    main()

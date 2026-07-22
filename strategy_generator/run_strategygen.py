#!/usr/bin/env python
"""CLI entry point: generate a portfolio allocation strategy for a basket of assets.

Unlike the previous version which focused on single-asset timing signals,
this tool searches for the optimal portfolio allocation method (e.g., Inverse Volatility,
Cross-Sectional Momentum, Equal Weight) across the entire basket simultaneously.

Example:
    python run_strategygen.py --universe SPY QQQ AAPL --mode generate
"""

import argparse
import os

import pandas as pd

from stratgen.data import load_ohlcv
from stratgen.generator import GeneratorConfig, StrategyGenerator

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Basket Asset Allocation Strategy Generator")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--universe", nargs="+", default=["SPY", "QQQ"], help="List of symbols to trade")
    group.add_argument("--universe-file", help="Path to a JSON file containing the universe basket, exported by the instrument selection tool")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--mode", choices=["generate"], default="generate", help="Currently only 'generate' is supported for allocation.")
    p.add_argument("--n-random-search", type=int, default=200)
    p.add_argument("--ers-percentile-threshold", type=float, default=0.90)
    p.add_argument("--min-rebalances-for-trust", type=int, default=4)
    p.add_argument("--no-cache", action="store_true")
    return p


def main():
    args = build_arg_parser().parse_args()
    gen_config = GeneratorConfig(
        n_random_search=args.n_random_search,
        ers_percentile_threshold=args.ers_percentile_threshold,
        min_rebalances_for_trust=args.min_rebalances_for_trust,
    )

    if args.universe_file:
        import json
        with open(args.universe_file, "r") as f:
            basket_data = json.load(f)
        universe_symbols = basket_data.get("basket", [])
        if not universe_symbols:
            raise ValueError(f"No 'basket' array found in {args.universe_file}")
        print(f"Loaded {len(universe_symbols)} symbols from {args.universe_file} (method: {basket_data.get('method', 'unknown')})")
    else:
        universe_symbols = args.universe

    universe = {}
    for symbol in universe_symbols:
        print(f"Loading {symbol} ...")
        universe[symbol] = load_ohlcv(symbol, args.start, args.end, args.interval, use_cache=not args.no_cache)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    if args.mode == "generate":
        spec = StrategyGenerator(gen_config).generate(universe)
        print(f"\n=== Generated Allocation Strategy for basket of {spec.n_symbols} assets ===")
        print(f"  Template: {spec.template_name}")
        print(f"  Optimal Parameters: {spec.params}")
        print(f"  Portfolio Sharpe Ratio: {spec.universe_sharpe:.2f}")
        print(f"  Total Rebalances: {spec.total_rebalances} | Total Turnover: {spec.total_turnover:.2f}")
        print(f"  ERS Percentile: {spec.ers_percentile:.2f} | Trusted: {spec.trusted}")

        print("\n=== Strategy Logic & Execution Schedule ===")
        print(f"  {spec.explanation}")

        print("\n=== Recent Target Weights (Last 5 Rebalance Dates) ===")
        # spec.target_weights is sparse -- NaN except on an actual rebalance
        # date, so those dates are just the non-NaN rows (no diffing needed,
        # and no risk of missing a rebalance that recomputed the same weight).
        weights = spec.target_weights
        recent_rebalances = weights.dropna(how="all").tail(5)

        # Convert to percentages for readability
        recent_pct = (recent_rebalances * 100).round(1).astype(str) + "%"
        print(recent_pct)

        out_path = os.path.join(RESULTS_DIR, "strategygen_allocation_weights.csv")
        weights.ffill().fillna(0.0).to_csv(out_path)
        print(f"\nSaved full daily target weights to {out_path}")

        import json
        strategy_json_path = os.path.join(RESULTS_DIR, "strategy.json")
        with open(strategy_json_path, "w") as f:
            json.dump({
                "template_name": spec.template_name,
                "params": spec.params,
                "explanation": spec.explanation,
                # Persisted so a downstream consumer (e.g. backtester/run_backtest.py)
                # can tell a candidate that failed the ERS/rebalance-count gate apart
                # from a genuinely trusted one without re-running the search.
                "trusted": spec.trusted,
                "ers_passed": spec.ers_passed,
                "ers_percentile": spec.ers_percentile,
            }, f, indent=2)
        print(f"Saved strategy definition to {strategy_json_path}")
    else:
        print("Walkforward mode is currently disabled for the new allocation architecture.")


if __name__ == "__main__":
    main()

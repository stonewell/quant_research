#!/usr/bin/env python
"""CLI entry point: generate ONE strategy for a whole universe of
instruments, pooling their historical data -- not a separate strategy per
symbol (see stratgen/generator.py's module docstring for why). Either a
single-window "generate" (fast, for exploration) or a full walk-forward
validation (slower, gives an honest out-of-sample read via the
generalization ratio and Deflated Sharpe Ratio).

NOTE: not exercised against real market data as part of this project's own
test suite (by request) -- only synthetic-data unit/integration tests are
included. This CLI is provided for you to run against real data yourself.

Example:
    python run_strategygen.py --universe SPY QQQ AAPL --mode generate
    python run_strategygen.py --universe SPY QQQ AAPL --mode walkforward --start 2010-01-01
"""

import argparse
import os

import pandas as pd

from stratgen.data import load_ohlcv
from stratgen.generator import GeneratorConfig, StrategyGenerator
from stratgen.walkforward import WalkForwardConfig, run_walkforward

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Universe-wide strategy generator")
    p.add_argument("--universe", nargs="+", default=["SPY", "QQQ"])
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--mode", choices=["generate", "walkforward"], default="generate")
    p.add_argument("--n-random-search", type=int, default=200)
    p.add_argument("--ers-percentile-threshold", type=float, default=0.90)
    p.add_argument("--min-trades-for-trust", type=int, default=10)
    p.add_argument("--max-concurrent-positions", type=int, default=10,
                   help="equal-weight slot cap for the single-symbol multi-asset portfolio backtest")
    p.add_argument("--single-symbol-max-holding-days", type=int, default=63,
                   help="hard cap forcing single-symbol-template positions to close under this many trading days")
    p.add_argument("--no-search-pairs", action="store_true",
                   help="disable the pairs-trading candidate search (single-symbol templates only)")
    p.add_argument("--max-pairs-to-search", type=int, default=50,
                   help="cap on distinct pairs backtested for large universes (C(N,2) grows quadratically)")
    p.add_argument("--pairs-max-holding-days", type=int, default=63)
    p.add_argument("--train-years", type=float, default=4.0)
    p.add_argument("--validation-years", type=float, default=2.0)
    p.add_argument("--test-years", type=float, default=1.0)
    p.add_argument("--embargo-days", type=int, default=30)
    p.add_argument("--no-cache", action="store_true")
    return p


def _align_universe(universe: dict) -> dict:
    """Walk-forward's fold boundaries are bar-position-based, so every
    symbol must share the same trading calendar -- trim to the intersection
    of all symbols' dates (an inner join) rather than assuming yfinance
    happens to return identical calendars for every ticker requested."""
    common_index = None
    for df in universe.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    return {symbol: df.loc[common_index] for symbol, df in universe.items()}


def main():
    args = build_arg_parser().parse_args()
    gen_config = GeneratorConfig(
        n_random_search=args.n_random_search, ers_percentile_threshold=args.ers_percentile_threshold,
        min_trades_for_trust=args.min_trades_for_trust,
        max_concurrent_positions=args.max_concurrent_positions,
        single_symbol_max_holding_days=args.single_symbol_max_holding_days,
        search_pairs=not args.no_search_pairs, max_pairs_to_search=args.max_pairs_to_search,
        pairs_max_holding_days=args.pairs_max_holding_days,
    )

    universe = {}
    for symbol in args.universe:
        print(f"Loading {symbol} ...")
        universe[symbol] = load_ohlcv(symbol, args.start, args.end, args.interval, use_cache=not args.no_cache)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    if args.mode == "generate":
        spec = StrategyGenerator(gen_config).generate(universe)
        print(f"\n=== Generated strategy for universe of {spec.n_symbols} symbols ===")
        print(f"  regime={spec.regime_label} pooled_hurst_z={spec.pooled_hurst_z:.2f}")
        print(f"  strategy_family={spec.strategy_family}"
              + (f" pair_symbols={spec.pair_symbols}" if spec.pair_symbols else ""))
        print(f"  template={spec.template_name} params={spec.params}")
        print(f"  universe_sharpe={spec.universe_sharpe:.2f} "
              f"total_trades={spec.total_num_trades} ers_percentile={spec.ers_percentile:.2f} trusted={spec.trusted}")

        if spec.single_symbol_result is not None:
            s = spec.single_symbol_result
            print(f"\n  [single-symbol candidate] template={s['template_name']} params={s['params']} "
                  f"score={s['score']:.2f} trades={s['total_trades']} "
                  f"ers_percentile={s['ers_percentile']:.2f} trusted={s['trusted']}")
        if spec.pairs_result is not None:
            p = spec.pairs_result
            print(f"  [pairs candidate] pair=({p.symbol_a}, {p.symbol_b}) params={p.params} "
                  f"sharpe={p.sharpe:.2f} trades={p.num_trades} ers_percentile={p.ers_percentile:.2f} "
                  f"trusted={p.trusted} (searched {p.n_pairs_searched}/{p.n_pairs_total} possible pairs)")

        print("\n  Per-symbol breakdown for the WINNING candidate (how consistent is it across instruments?):")
        per_symbol_df = pd.DataFrame({
            "realized_pnl": spec.per_symbol_pnl, "num_trades": spec.per_symbol_num_trades,
        })
        print(per_symbol_df.round(2))

        out_path = os.path.join(RESULTS_DIR, "strategygen_generate_report.csv")
        per_symbol_df.to_csv(out_path)
    else:
        aligned = _align_universe(universe)
        wf_config = WalkForwardConfig(
            train_years=args.train_years, validation_years=args.validation_years,
            test_years=args.test_years, embargo_days=args.embargo_days, generator_config=gen_config,
        )
        result = run_walkforward(aligned, wf_config)
        print(f"\n=== Walk-forward validation for universe of {result['n_symbols']} symbols ===")
        print(f"  n_folds={result['n_folds']} mean_validation_sharpe={result['mean_validation_sharpe']:.2f} "
              f"mean_test_sharpe={result['mean_test_sharpe']:.2f} "
              f"generalization_ratio={result['generalization_ratio']:.2f} "
              f"DSR={result['deflated_sharpe_ratio']}")
        for i, fold in enumerate(result["folds"]):
            print(f"  fold {i}: regime={fold['regime_label']} template={fold['template_name']} "
                  f"params={fold['params']} validation_sharpe={fold['validation_sharpe']:.2f} "
                  f"test_sharpe={fold['test_sharpe']:.2f} test_trades={fold['test_num_trades']} "
                  f"trusted={fold['trusted']}")

        out_path = os.path.join(RESULTS_DIR, "strategygen_walkforward_report.csv")
        pd.DataFrame(result["folds"]).to_csv(out_path, index=False)

    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()

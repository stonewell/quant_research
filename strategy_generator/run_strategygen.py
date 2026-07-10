#!/usr/bin/env python
"""CLI entry point: generate a strategy per instrument in a universe, from
that instrument's own historical data -- either a single-window "generate"
(fast, for exploration) or a full walk-forward validation (slower, gives an
honest out-of-sample read via the generalization ratio and Deflated Sharpe
Ratio).

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
    p = argparse.ArgumentParser(description="Per-instrument strategy generator")
    p.add_argument("--universe", nargs="+", default=["SPY", "QQQ"])
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--mode", choices=["generate", "walkforward"], default="generate")
    p.add_argument("--n-random-search", type=int, default=200)
    p.add_argument("--ers-percentile-threshold", type=float, default=0.90)
    p.add_argument("--min-trades-for-trust", type=int, default=10)
    p.add_argument("--train-years", type=float, default=4.0)
    p.add_argument("--validation-years", type=float, default=2.0)
    p.add_argument("--test-years", type=float, default=1.0)
    p.add_argument("--embargo-days", type=int, default=30)
    p.add_argument("--no-cache", action="store_true")
    return p


def main():
    args = build_arg_parser().parse_args()
    gen_config = GeneratorConfig(
        n_random_search=args.n_random_search, ers_percentile_threshold=args.ers_percentile_threshold,
        min_trades_for_trust=args.min_trades_for_trust,
    )

    rows = []
    for symbol in args.universe:
        print(f"Loading {symbol} ...")
        df = load_ohlcv(symbol, args.start, args.end, args.interval, use_cache=not args.no_cache)

        if args.mode == "generate":
            spec = StrategyGenerator(gen_config).generate(df)
            print(f"  {symbol}: regime={spec.regime_label} hurst={spec.hurst:.3f} "
                  f"template={spec.template_name} params={spec.params} "
                  f"train_sharpe={spec.train_sharpe:.2f} ers_percentile={spec.ers_percentile:.2f} "
                  f"trusted={spec.trusted}")
            rows.append({"symbol": symbol, "regime_label": spec.regime_label, "hurst": spec.hurst,
                         "template": spec.template_name, "params": spec.params, "train_sharpe": spec.train_sharpe,
                         "train_num_trades": spec.train_num_trades, "ers_percentile": spec.ers_percentile,
                         "trusted": spec.trusted})
        else:
            wf_config = WalkForwardConfig(
                train_years=args.train_years, validation_years=args.validation_years,
                test_years=args.test_years, embargo_days=args.embargo_days, generator_config=gen_config,
            )
            result = run_walkforward(df, wf_config)
            print(f"  {symbol}: n_folds={result['n_folds']} mean_validation_sharpe={result['mean_validation_sharpe']:.2f} "
                  f"mean_test_sharpe={result['mean_test_sharpe']:.2f} "
                  f"generalization_ratio={result['generalization_ratio']:.2f} "
                  f"DSR={result['deflated_sharpe_ratio']}")
            rows.append({"symbol": symbol, "n_folds": result["n_folds"],
                         "mean_validation_sharpe": result["mean_validation_sharpe"],
                         "mean_test_sharpe": result["mean_test_sharpe"],
                         "generalization_ratio": result["generalization_ratio"],
                         "deflated_sharpe_ratio": result["deflated_sharpe_ratio"]})

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, f"strategygen_{args.mode}_report.csv")
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()

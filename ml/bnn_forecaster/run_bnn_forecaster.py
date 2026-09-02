#!/usr/bin/env python
"""CLI entry point: rank a universe's top-N buy/sell candidates using
AutoBNN (compositional Bayesian Neural Network) probabilistic price
forecasts.

Given a universe, fits a BNN per candidate symbol (and the benchmark) on
trailing price history, and ranks the top-N symbols whose forecast clears a
required-return hurdle with a sufficiently narrow confidence interval
("buy") against the top-N whose forecast has decayed below the benchmark's
own forecast or lost confidence ("sell") -- see `bnnf/rules.py`'s
`evaluate_buy_sell` for the sell-always-wins overlap resolution.

This is the third of this workspace's "beat the benchmark" strategy family:
`research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy` (price-only
technical proxy) and `fundamental_screener` (real ROE/dividend/earnings
growth) are the other two. Unlike `fundamental_screener`, this project needs
NO network access for its actual signal (AutoBNN fits on OHLCV price history,
which `--data-provider synthetic` already supplies offline) -- the
computational cost here is CPU time (a BNN fit per symbol), not a live API
call.

**Not wired into `run_pipeline.py`.** Requires this project's OWN isolated
`uv` environment (`bnn_forecaster/pyproject.toml`/`.venv`) -- AutoBNN's JAX +
TensorFlow Probability dependency chain needs jax/jaxlib/numpy pinned well
below their latest releases, which would otherwise conflict with the rest of
this workspace's much lighter, unpinned dependency stack. See this project's
README for the exact versions and why.

Example:
    python run_bnn_forecaster.py --data-provider synthetic
    python run_bnn_forecaster.py --universe KO PG JNJ MSFT COST WMT MCD PEP --data-provider yfinance
"""

import argparse
import os
import sys
from dataclasses import asdict

import pandas as pd

# Ensure the repo root is in sys.path to allow importing from common
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.cli_utils import (
    add_data_provider_cli_args,
    bootstrap_project_paths,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.reporting import utc_timestamp, write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args

# Also add this project's own directory (for bare `from bnnf...` imports) and
# ml/ (for bnn_forecaster as a sibling group member).
bootstrap_project_paths(_REPO_ROOT, __file__)

from bnnf.config import DEFAULT_CANDIDATE_UNIVERSE, ForecasterConfig
from bnnf.forecasting import fit_forecast
from bnnf.rules import evaluate_buy_sell, rank_buy_sell
from bnnf.strategy import BnnForecastStrategy

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AutoBNN probabilistic-forecast buy/sell screener")
    add_universe_cli_args(p, default_universe=DEFAULT_CANDIDATE_UNIVERSE)
    p.add_argument("--benchmark", default="SPY", help="Broad-index benchmark symbol for the sell-trigger comparator (default: SPY)")
    p.add_argument("--top-n", type=int, default=5, help="How many top buy/sell candidates to report (default: 5)")
    p.add_argument("--horizon-days", type=int, default=ForecasterConfig().horizon_days,
                   help="Forecast horizon in trading days (default: 21, ~1 month)")
    p.add_argument("--lookback-days", type=int, default=ForecasterConfig().lookback_days,
                   help="Trading days of trailing history to fit each symbol's BNN on (default: 756, ~3 years)")
    p.add_argument("--estimator", choices=["map", "mcmc", "vi"], default=ForecasterConfig().estimator,
                   help="AutoBNN estimator type -- 'map' (default) is by far the cheapest")
    p.add_argument("--width", type=int, default=ForecasterConfig().width)
    p.add_argument("--num-iters", type=int, default=ForecasterConfig().num_iters,
                   help="MAP optimization iterations per symbol (default: 1000, reduced from AutoBNN's own "
                        "5000 default for practical runtime -- see README for the calibration trade-off this implies)")
    p.add_argument("--num-particles", type=int, default=ForecasterConfig().num_particles)
    p.add_argument("--required-return", type=float, default=ForecasterConfig().required_return)
    p.add_argument("--max-ci-width", type=float, default=ForecasterConfig().max_ci_width)
    p.add_argument("--seed", type=int, default=ForecasterConfig().seed)
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    add_data_provider_cli_args(p, default_provider="synthetic",
                                no_cache_help="Disable local CSV caching of OHLCV history")
    return p


def build_config_from_args(args) -> ForecasterConfig:
    universe_symbols = resolve_universe_from_args(args, default_symbols=DEFAULT_CANDIDATE_UNIVERSE)
    return ForecasterConfig(
        universe=universe_symbols,
        benchmark_symbol=args.benchmark,
        top_n=args.top_n,
        horizon_days=args.horizon_days,
        lookback_days=args.lookback_days,
        estimator=args.estimator,
        width=args.width,
        num_iters=args.num_iters,
        num_particles=args.num_particles,
        required_return=args.required_return,
        max_ci_width=args.max_ci_width,
        seed=args.seed,
    )


def main():
    args = build_arg_parser().parse_args()
    cfg = build_config_from_args(args)

    data_kwargs = build_data_kwargs(args)
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed

    ohlcv_universe = list(dict.fromkeys(cfg.universe + [cfg.benchmark_symbol]))
    price_universe = load_universe_with_banner(
        ohlcv_universe, args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=DATA_DIR, data_kwargs=data_kwargs,
        require_nonempty=False, cache_max_age_days=args.cache_ttl_days,
        loading_msg=f"Loading {len(ohlcv_universe)} symbols' OHLCV via provider '{args.data_provider}' "
                    f"({args.start} to {args.end}) ...",
    )
    if cfg.benchmark_symbol not in price_universe:
        raise ValueError(f"Benchmark symbol '{cfg.benchmark_symbol}' could not be loaded.")

    print(f"Fitting AutoBNN ({cfg.estimator}, width={cfg.width}, num_iters={cfg.num_iters}) on the benchmark "
          f"({cfg.benchmark_symbol}) ...")
    benchmark_forecast = fit_forecast(price_universe[cfg.benchmark_symbol]["Close"], cfg)
    benchmark_return = float(benchmark_forecast["forecast_return"].iloc[-1])
    print(f"Benchmark ({cfg.benchmark_symbol}) current {cfg.horizon_days}-day-ahead forecast return: "
          f"{benchmark_return * 100:.2f}% (ci_width={benchmark_forecast['ci_width'].iloc[-1] * 100:.2f}%)")

    rows = {}
    for symbol in cfg.universe:
        if symbol not in price_universe or symbol == cfg.benchmark_symbol:
            continue
        print(f"Fitting AutoBNN on {symbol} ...")
        forecast_df = fit_forecast(price_universe[symbol]["Close"], cfg)
        rows[symbol] = {
            "forecast_return": float(forecast_df["forecast_return"].iloc[-1]),
            "ci_width": float(forecast_df["ci_width"].iloc[-1]),
        }

    current_snapshot = pd.DataFrame(rows).T
    evaluated = evaluate_buy_sell(current_snapshot, benchmark_return, cfg)
    top_buy, top_sell = rank_buy_sell(evaluated, cfg.top_n)

    def _to_records(df):
        return df.reset_index(names="symbol").to_dict("records")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    report = {
        "run_context": {
            "data_provider": args.data_provider,
            "start": args.start, "end": args.end,
            "benchmark_symbol": cfg.benchmark_symbol,
            "benchmark_forecast_return": benchmark_return,
            "horizon_days": cfg.horizon_days,
            "generated_at": utc_timestamp(),
        },
        "n_universe_evaluated": len(rows),
        "top_buy": _to_records(top_buy),
        "top_sell": _to_records(top_sell),
        "caveat": (
            "AutoBNN's own compute profile (thousands of MAP iterations per symbol for a well-converged "
            "fit) means this run used reduced width/num_iters for practical runtime -- calibration is NOT "
            "verified for financial return series; this module's own experimentation found ci_width values "
            "routinely far larger than typical return-magnitude thresholds. Inspect actual ci_width output "
            "before trusting --max-ci-width. Overlap resolution: sell always takes precedence over buy (see "
            "bnnf/rules.py), so a symbol never appears on both lists."
        ),
    }
    report_path = os.path.join(RESULTS_DIR, "bnn_forecast_report.json")
    write_json_report(report, report_path)
    print(f"\nSaved forecast report to {report_path}")

    print(f"\n=== Top {len(report['top_buy'])} BUY candidates ===")
    for row in report["top_buy"]:
        print(f"  {row['symbol']}: forecast_return={row['expected_return'] * 100:.1f}% "
              f"ci_width={row['ci_width'] * 100:.1f}% (confident={row['confident']})")
    print(f"\n=== Top {len(report['top_sell'])} SELL candidates ===")
    for row in report["top_sell"]:
        print(f"  {row['symbol']}: forecast_return={row['expected_return'] * 100:.1f}% "
              f"ci_width={row['ci_width'] * 100:.1f}% (confident={row['confident']})")

    strategy = BnnForecastStrategy(cfg)
    strategy_def = {
        "template_name": strategy.name,
        "params": {**asdict(cfg), "cash_proxy": "BIL"},
        "explanation": strategy.explain_weights(),
        "bnn_spec": {"source": "bnn_forecaster"},
    }
    strategy_path = os.path.join(RESULTS_DIR, "bnn_strategy.json")
    write_json_report(strategy_def, strategy_path)
    print(f"\nSaved backtester-compatible strategy definition to {strategy_path}")
    print("(NOT wired into run_pipeline.py -- run backtester/run_backtest.py --strategy-file "
          f"{strategy_path} manually, using bnn_forecaster's OWN venv since backtester needs to import "
          "this project's autobnn-dependent code for a bnn_spec strategy file.)")


if __name__ == "__main__":
    main()

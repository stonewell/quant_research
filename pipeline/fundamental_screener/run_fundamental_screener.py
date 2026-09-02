#!/usr/bin/env python
"""CLI entry point: screen a universe against real-fundamentals buy/sell
rules adapted from a conservative value-investing community's valuation
framework (see `docs/snowball_strategy.txt` at the repo root).

Given a universe, ranks the top-N symbols passing the BUY rule and the
top-N symbols passing the SELL rule (a symbol that would otherwise qualify
for both is resolved by giving sell precedence -- see `fscreen/rules.py`'s
`evaluate_buy_sell` docstring). Also writes a `strategy.json`-compatible
`fundamental_strategy.json` so `backtester/run_backtest.py` can run a real
backtest against this project's strategy via `--strategy-file`, exactly
like every other strategy source in this workspace.

NOT wired into `run_pipeline.py` -- this project's real-fundamentals
lookups always hit the network (see `fscreen/fundamentals.py`), which is a
deliberately different operating mode than every other pipeline stage's
offline-by-default convention.

Example:
    python run_fundamental_screener.py --data-provider synthetic
    python run_fundamental_screener.py --universe KO PG JNJ MSFT COST WMT MCD PEP --data-provider yfinance
"""

import argparse
import json
import os
import sys
from dataclasses import asdict

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

# Also add this project's own directory (for bare `from fscreen...` imports)
# and pipeline/ (for fundamental_screener as a sibling group member).
bootstrap_project_paths(_REPO_ROOT, __file__)

from fscreen.config import DEFAULT_CANDIDATE_UNIVERSE, ScreenerConfig
from fscreen.fundamentals import fetch_fundamentals_frame
from fscreen.rules import evaluate_buy_sell, rank_buy_sell
from fscreen.strategy import FundamentalMarginOfSafetyStrategy

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fundamental (real ROE/dividend/earnings-growth/leverage) buy/sell screener")
    add_universe_cli_args(p, default_universe=DEFAULT_CANDIDATE_UNIVERSE)
    p.add_argument("--benchmark", default="SPY", help="Broad-index benchmark symbol for the sell-trigger comparator (default: SPY)")
    p.add_argument("--top-n", type=int, default=5, help="How many top buy/sell candidates to report (default: 5)")
    p.add_argument("--required-return", type=float, default=ScreenerConfig().required_return,
                   help="Buy hurdle: expected_return (earnings_growth + dividend_yield) must clear this (default: 0.12)")
    p.add_argument("--min-roe", type=float, default=ScreenerConfig().min_roe)
    p.add_argument("--min-dividend-yield", type=float, default=ScreenerConfig().min_dividend_yield)
    p.add_argument("--max-debt-to-equity", type=float, default=ScreenerConfig().max_debt_to_equity)
    p.add_argument("--min-earnings-growth", type=float, default=ScreenerConfig().min_earnings_growth)
    p.add_argument("--lookback-days", type=int, default=ScreenerConfig().lookback_days,
                   help="Trading days of benchmark price history used for its own trailing-return comparator (default: 1260, ~5 years)")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--seed", type=int, default=42, help="Random seed (only used with --data-provider synthetic)")
    add_data_provider_cli_args(p, default_provider="synthetic",
                                no_cache_help="Disable local CSV caching of the benchmark's OHLCV history")
    return p


def build_config_from_args(args) -> ScreenerConfig:
    universe_symbols = resolve_universe_from_args(args, default_symbols=DEFAULT_CANDIDATE_UNIVERSE)
    return ScreenerConfig(
        universe=universe_symbols,
        benchmark_symbol=args.benchmark,
        top_n=args.top_n,
        required_return=args.required_return,
        min_roe=args.min_roe,
        min_dividend_yield=args.min_dividend_yield,
        max_debt_to_equity=args.max_debt_to_equity,
        min_earnings_growth=args.min_earnings_growth,
        lookback_days=args.lookback_days,
    )


def main():
    args = build_arg_parser().parse_args()
    cfg = build_config_from_args(args)

    if args.data_provider == "synthetic":
        print("WARNING: --data-provider synthetic has no real price structure, so the benchmark's "
              "own trailing return (the sell-trigger comparator) is meaningless noise on this run -- "
              "fundamentals below are always real (yfinance), regardless of this flag. Use "
              "--data-provider yfinance for a meaningful benchmark comparator.")

    data_kwargs = build_data_kwargs(args)
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed

    ohlcv_universe = list(dict.fromkeys(cfg.universe + [cfg.benchmark_symbol]))
    price_universe = load_universe_with_banner(
        ohlcv_universe, args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=DATA_DIR, data_kwargs=data_kwargs,
        require_nonempty=False, cache_max_age_days=args.cache_ttl_days,
        loading_msg=f"Loading {len(ohlcv_universe)} symbols' OHLCV (benchmark comparator only) via "
                    f"provider '{args.data_provider}' ({args.start} to {args.end}) ...",
    )
    if cfg.benchmark_symbol not in price_universe:
        raise ValueError(f"Benchmark symbol '{cfg.benchmark_symbol}' could not be loaded -- cannot compute the sell-trigger comparator.")
    benchmark_close = price_universe[cfg.benchmark_symbol]["Close"]
    trailing = (benchmark_close / benchmark_close.shift(cfg.lookback_days)) ** (252.0 / cfg.lookback_days) - 1.0
    benchmark_return = float(trailing.dropna().iloc[-1]) if trailing.notna().any() else 0.0
    print(f"Benchmark ({cfg.benchmark_symbol}) trailing {cfg.lookback_days}-day annualized return: {benchmark_return * 100:.2f}%")

    print(f"Fetching real fundamentals for {len(cfg.universe)} candidate symbol(s) via yfinance ...")
    fundamentals_df = fetch_fundamentals_frame(cfg.universe)
    evaluated = evaluate_buy_sell(fundamentals_df, benchmark_return, cfg)
    top_buy, top_sell = rank_buy_sell(evaluated, cfg.top_n)

    def _to_records(df):
        return df.reset_index(names="symbol").to_dict("records")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    report = {
        "run_context": {
            "data_provider": args.data_provider,
            "start": args.start, "end": args.end,
            "benchmark_symbol": cfg.benchmark_symbol,
            "benchmark_trailing_return": benchmark_return,
            "generated_at": utc_timestamp(),
        },
        "n_universe_evaluated": len(cfg.universe),
        "top_buy": _to_records(top_buy),
        "top_sell": _to_records(top_sell),
        "caveat": (
            "Fundamentals (roe/dividend_yield/earnings_growth/debt_to_equity) are CURRENT, real "
            "yfinance data, not historical point-in-time figures -- yfinance's free API doesn't "
            "expose those. Overlap resolution: sell always takes precedence over buy (see "
            "fscreen/rules.py), so a symbol never appears on both lists. "
            + ("Benchmark comparator is from SYNTHETIC price data on this run and is NOT meaningful "
               "-- re-run with --data-provider yfinance." if args.data_provider == "synthetic" else
               "Benchmark comparator is a single trailing-window snapshot, not a validated forecast.")
        ),
    }
    report_path = os.path.join(RESULTS_DIR, "fundamental_screen_report.json")
    write_json_report(report, report_path)
    print(f"Saved screen report to {report_path}")

    print(f"\n=== Top {len(report['top_buy'])} BUY candidates ===")
    for row in report["top_buy"]:
        print(f"  {row['symbol']}: expected_return={row['expected_return'] * 100:.1f}% "
              f"(ROE={row['roe']:.2f}, div_yield={row['dividend_yield']:.3f}, "
              f"earnings_growth={row['earnings_growth']:.2f}, D/E={row['debt_to_equity']:.1f})")
    print(f"\n=== Top {len(report['top_sell'])} SELL candidates ===")
    for row in report["top_sell"]:
        print(f"  {row['symbol']}: expected_return={row['expected_return'] * 100:.1f}% "
              f"(quality_ok={row['quality_ok']})")

    strategy = FundamentalMarginOfSafetyStrategy(cfg)
    strategy_def = {
        "template_name": strategy.name,
        "params": {**asdict(cfg), "cash_proxy": "BIL"},
        "explanation": strategy.explain_weights(),
        "fundamental_spec": {"source": "fundamental_screener"},
    }
    strategy_path = os.path.join(RESULTS_DIR, "fundamental_strategy.json")
    write_json_report(strategy_def, strategy_path)
    print(f"\nSaved backtester-compatible strategy definition to {strategy_path}")
    print("(NOT wired into run_pipeline.py -- run backtester/run_backtest.py --strategy-file "
          f"{strategy_path} manually.)")


if __name__ == "__main__":
    main()

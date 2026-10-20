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
lookups hit the network (see `fscreen/fundamentals.py`) when using yfinance,
which is an operating mode distinct from the pipeline's offline-by-default convention.

Example:
    python run_fundamental_screener.py --data-provider synthetic
    python run_fundamental_screener.py --universe KO PG JNJ MSFT COST WMT MCD PEP --data-provider yfinance
"""

import argparse
import json
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

    # Snowball Valuation Engine CLI options (F3.1)
    p.add_argument("--enable-snowball-valuation", action=argparse.BooleanOptionalAction, default=True,
                   help="Enable conservative Snowball dual-model valuation and Margin of Safety screening")
    p.add_argument("--discount-rate", type=float, default=ScreenerConfig().discount_rate,
                   help="Discount rate for Snowball valuation models (default: 0.12)")
    p.add_argument("--margin-of-safety", type=float, default=ScreenerConfig().margin_of_safety,
                   help="Margin of safety discount required below fair market value (default: 0.20)")
    p.add_argument("--max-earnings-growth", type=float, default=ScreenerConfig().max_earnings_growth,
                   help="Maximum sustainable 5-year earnings growth CAGR cap (default: 0.12)")
    p.add_argument("--min-cfo-to-profit", type=float, default=ScreenerConfig().min_cfo_to_profit,
                   help="Minimum operating cash flow to net profit ratio (default: 0.80)")
    p.add_argument("--max-debt-to-assets", type=float, default=ScreenerConfig().max_debt_to_assets,
                   help="Maximum debt-to-assets ratio (default: 0.50)")
    p.add_argument("--min-dividend-payout", type=float, default=ScreenerConfig().min_dividend_payout,
                   help="Minimum dividend payout ratio (default: 0.20)")
    p.add_argument("--max-dividend-payout", type=float, default=ScreenerConfig().max_dividend_payout,
                   help="Maximum dividend payout ratio (default: 0.70)")
    p.add_argument("--enable-industry-blacklist", action=argparse.BooleanOptionalAction, default=True,
                   help="Reject blacklisted industries (financials, tech iteration, cyclicals, deteriorating, ST)")
    p.add_argument("--weighting-mode", choices=["equal", "mos"], default=ScreenerConfig().weighting_mode,
                   help="Weighting mode for portfolio: equal or mos (proportional to margin of safety)")
    p.add_argument("--rebalance-freq-days", type=int, default=ScreenerConfig().rebalance_freq_days,
                   help="Rebalance frequency in trading days (default: 63)")
    p.add_argument("--cash-proxy", default=ScreenerConfig().cash_proxy,
                   help="Cash proxy symbol to park unallocated weight (default: BIL)")

    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--seed", type=int, default=42, help="Random seed (only used with --data-provider synthetic)")
    add_data_provider_cli_args(p, default_provider="synthetic",
                                no_cache_help="Disable local DuckDB caching of the benchmark's OHLCV history")
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
        discount_rate=getattr(args, "discount_rate", ScreenerConfig().discount_rate),
        benchmark_expected_return=getattr(args, "benchmark_expected_return", ScreenerConfig().benchmark_expected_return),
        margin_of_safety=getattr(args, "margin_of_safety", ScreenerConfig().margin_of_safety),
        max_earnings_growth=getattr(args, "max_earnings_growth", ScreenerConfig().max_earnings_growth),
        min_cfo_to_profit=getattr(args, "min_cfo_to_profit", ScreenerConfig().min_cfo_to_profit),
        max_debt_to_assets=getattr(args, "max_debt_to_assets", ScreenerConfig().max_debt_to_assets),
        min_dividend_payout=getattr(args, "min_dividend_payout", ScreenerConfig().min_dividend_payout),
        max_dividend_payout=getattr(args, "max_dividend_payout", ScreenerConfig().max_dividend_payout),
        enable_industry_blacklist=getattr(args, "enable_industry_blacklist", ScreenerConfig().enable_industry_blacklist),
        enable_snowball_valuation=getattr(args, "enable_snowball_valuation", ScreenerConfig().enable_snowball_valuation),
        weighting_mode=getattr(args, "weighting_mode", ScreenerConfig().weighting_mode),
        rebalance_freq_days=getattr(args, "rebalance_freq_days", ScreenerConfig().rebalance_freq_days),
        cash_proxy=getattr(args, "cash_proxy", ScreenerConfig().cash_proxy),
    )


def format_ascii_report(
    evaluated: pd.DataFrame,
    top_buy: pd.DataFrame,
    top_sell: pd.DataFrame,
    cfg: ScreenerConfig,
    benchmark_return: float,
) -> str:
    lines = []
    lines.append("=" * 115)
    lines.append("                     SNOWBALL VALUE INVESTING & FUNDAMENTAL SCREENER REPORT")
    lines.append("=" * 115)
    lines.append(
        f"Benchmark: {cfg.benchmark_symbol} | Trailing {cfg.lookback_days}d Return: {benchmark_return * 100:.2f}% | "
        f"Required Hurdle: {cfg.required_return * 100:.1f}% | MoS Target: {cfg.margin_of_safety * 100:.1f}%"
    )
    lines.append(
        f"Quality Gates: ROE >= {cfg.min_roe:.1%}, Growth in [{cfg.min_earnings_growth:.1%}, {cfg.max_earnings_growth:.1%}], "
        f"CFO/NP >= {cfg.min_cfo_to_profit:.2f}, Debt/Assets <= {cfg.max_debt_to_assets:.1%}"
    )
    lines.append("-" * 115)

    # Table 1: Universe Evaluated & Valuation Overview
    lines.append("\n[UNIVERSE EVALUATION & VALUATION OVERVIEW]")
    hdr = (
        f"{'Symbol':<8} {'Industry':<16} {'Tier':<14} {'TermPE':>6} "
        f"{'Model1 MV':>11} {'Model2 MV':>11} {'Fair MV':>10} {'Buy Price':>10} "
        f"{'MoS %':>7} {'ExpRet':>7} {'Status':>8}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for sym, row in evaluated.iterrows():
        ind = str(row.get("industry_name", "Unknown"))[:15]
        tier = str(row.get("industry_tier", "Unknown"))[:13]
        term_pe = f"{row.get('terminal_pe', 10.0):.1f}x"
        m1 = f"{row.get('model1_mv', 0.0):.1f}" if pd.notna(row.get("model1_mv")) else "N/A"
        m2 = f"{row.get('model2_mv', 0.0):.1f}" if pd.notna(row.get("model2_mv")) else "N/A"
        fair = f"{row.get('fair_mv', 0.0):.1f}" if pd.notna(row.get("fair_mv")) else "N/A"
        bp = (
            f"{row.get('buy_price', 0.0):.2f}"
            if (pd.notna(row.get("buy_price")) and row.get("buy_price", 0.0) > 0)
            else "N/A"
        )
        mos = f"{row.get('mos_ratio', 0.0) * 100:.1f}%" if pd.notna(row.get("mos_ratio")) else "0.0%"
        exp_r = f"{row.get('expected_return', 0.0) * 100:.1f}%" if pd.notna(row.get("expected_return")) else "0.0%"
        status = "BUY" if row.get("buy_flag", False) else ("SELL" if row.get("sell_flag", False) else "HOLD")
        lines.append(
            f"{sym:<8} {ind:<16} {tier:<14} {term_pe:>6} "
            f"{m1:>11} {m2:>11} {fair:>10} {bp:>10} "
            f"{mos:>7} {exp_r:>7} {status:>8}"
        )

    # Table 2: Top Buy Candidates
    lines.append("\n[TOP BUY RECOMMENDATIONS (Ranked by Expected Return / MoS)]")
    if top_buy.empty:
        lines.append("  (No symbols cleared both fundamental quality gates and Margin of Safety hurdles)")
    else:
        hdr_buy = (
            f"{'Symbol':<8} {'Fair MV':>10} {'Buy MV':>10} {'Buy Price':>10} "
            f"{'MoS %':>8} {'ExpRet':>8} {'ROE':>7} {'Growth':>7} {'D/E':>7}"
        )
        lines.append(hdr_buy)
        lines.append("-" * len(hdr_buy))
        for sym, row in top_buy.iterrows():
            fair = f"{row.get('fair_mv', 0.0):.1f}"
            bmv = f"{row.get('buy_mv', 0.0):.1f}"
            bp = f"{row.get('buy_price', 0.0):.2f}" if pd.notna(row.get("buy_price")) else "N/A"
            mos = f"{row.get('mos_ratio', 0.0) * 100:.1f}%"
            exp_r = f"{row.get('expected_return', 0.0) * 100:.1f}%"
            roe = f"{row.get('roe', 0.0) * 100:.1f}%" if pd.notna(row.get("roe")) else "N/A"
            eg = f"{row.get('earnings_growth', 0.0) * 100:.1f}%" if pd.notna(row.get("earnings_growth")) else "N/A"
            de = f"{row.get('debt_to_equity', 0.0):.1f}" if pd.notna(row.get("debt_to_equity")) else "N/A"
            lines.append(f"{sym:<8} {fair:>10} {bmv:>10} {bp:>10} {mos:>8} {exp_r:>8} {roe:>7} {eg:>7} {de:>7}")

    # Table 3: Sell / Exclusion Audit List
    lines.append("\n[SELL / REJECTION AUDIT TRAIL]")
    if top_sell.empty:
        lines.append("  (No symbols flagged for sell)")
    else:
        hdr_sell = f"{'Symbol':<8} {'ExpRet':>8} {'QualOk':>8} {'Audit Reason'}"
        lines.append(hdr_sell)
        lines.append("-" * 115)
        for sym, row in top_sell.iterrows():
            exp_r = f"{row.get('expected_return', 0.0) * 100:.1f}%"
            q_ok = "PASS" if row.get("quality_ok", False) else "FAIL"
            reason = str(row.get("sell_reason", "") or "")
            if not reason:
                if not row.get("quality_ok", False):
                    reason = "Failed one or more fundamental quality gates (ROE, Leverage, Growth, Cash flow)"
                elif row.get("expected_return", 0.0) < benchmark_return:
                    reason = f"Return edge decay ({row.get('expected_return', 0.0):.1%} < benchmark {benchmark_return:.1%})"
                else:
                    reason = "Sell override triggered"
            lines.append(f"{sym:<8} {exp_r:>8} {q_ok:>8} {reason}")

    lines.append("=" * 115)
    return "\n".join(lines)


def main():
    args = build_arg_parser().parse_args()
    cfg = build_config_from_args(args)

    if args.data_provider == "synthetic":
        print("WARNING: --data-provider synthetic has no real price structure, so the benchmark's "
              "own trailing return (the sell-trigger comparator) is meaningless noise on this run -- "
              "fundamentals below are real (yfinance), regardless of this flag. Use "
              "--data-provider yfinance for a meaningful benchmark comparator.")

    data_kwargs = build_data_kwargs(args)
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed

    ohlcv_universe = list(dict.fromkeys(cfg.universe + [cfg.benchmark_symbol]))
    price_universe = load_universe_with_banner(
        ohlcv_universe, args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=DATA_DIR, data_kwargs=data_kwargs,
        require_nonempty=False,
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
            "config": asdict(cfg),
        },
        "universe_evaluated": _to_records(evaluated),
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

    # Save reports
    report_json_path = os.path.join(RESULTS_DIR, "report.json")
    write_json_report(report, report_json_path)
    legacy_report_path = os.path.join(RESULTS_DIR, "fundamental_screen_report.json")
    write_json_report(report, legacy_report_path)

    ascii_report = format_ascii_report(evaluated, top_buy, top_sell, cfg, benchmark_return)
    report_txt_path = os.path.join(RESULTS_DIR, "report.txt")
    with open(report_txt_path, "w", encoding="utf-8") as f:
        f.write(ascii_report)

    print(f"\n{ascii_report}")
    print(f"\nSaved structured JSON report to {report_json_path}")
    print(f"Saved backward-compatible report to {legacy_report_path}")
    print(f"Saved formatted ASCII text report to {report_txt_path}")

    strategy = FundamentalMarginOfSafetyStrategy(cfg)
    strategy_def = {
        "template_name": strategy.name,
        "params": asdict(cfg),
        "explanation": strategy.explain_weights(),
        "fundamental_spec": {"source": "fundamental_screener"},
    }
    strategy_path = os.path.join(RESULTS_DIR, "fundamental_strategy.json")
    write_json_report(strategy_def, strategy_path)
    print(f"Saved backtester-compatible strategy definition to {strategy_path}")
    print("(NOT wired into run_pipeline.py -- run backtester/run_backtest.py --strategy-file "
          f"{strategy_path} manually.)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""CLI entry point: run the regime-switching ensemble AND decompose it into
its standalone components (trend-following only, mean-reversion only) plus
plain buy-and-hold, so the combination can be judged against each of its
parts rather than just asserted to help.

Example:
    python run_backtest.py --symbol SPY --start 2000-01-01 --end 2024-12-31
"""

import argparse
import dataclasses
import os

from ensemblebot import metrics, plotting
from ensemblebot.backtester import run_backtest
from ensemblebot.config import EnsembleConfig
from ensemblebot.data import load_ohlcv

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    d = EnsembleConfig()
    p = argparse.ArgumentParser(description="Regime-switching ensemble backtester")
    p.add_argument("--symbol", default=d.symbol)
    p.add_argument("--start", default=d.start)
    p.add_argument("--end", default=d.end)
    p.add_argument("--interval", default=d.interval)
    p.add_argument("--initial-capital", type=float, default=d.initial_capital)
    p.add_argument("--trend-ma-period", type=int, default=d.trend_ma_period)
    p.add_argument("--adx-period", type=int, default=d.adx_period)
    p.add_argument("--adx-trend-threshold", type=float, default=d.adx_trend_threshold)
    p.add_argument("--adx-range-threshold", type=float, default=d.adx_range_threshold)
    p.add_argument("--rsi-period", type=int, default=d.rsi_period)
    p.add_argument("--entry-rsi-threshold", type=float, default=d.entry_rsi_threshold)
    p.add_argument("--exit-rsi-threshold", type=float, default=d.exit_rsi_threshold)
    p.add_argument("--commission-per-trade", type=float, default=d.commission_per_trade)
    p.add_argument("--commission-pct", type=float, default=d.commission_pct)
    p.add_argument("--slippage-pct", type=float, default=d.slippage_pct)
    p.add_argument("--warmup-bars", type=int, default=d.warmup_bars)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-plots", action="store_true")
    return p


def config_from_args(args, mode: str) -> EnsembleConfig:
    field_names = {f.name for f in dataclasses.fields(EnsembleConfig)}
    kwargs = {k: v for k, v in vars(args).items() if k in field_names}
    kwargs["mode"] = mode
    return EnsembleConfig(**kwargs)


def print_stats(title, stats):
    print(f"\n=== {title} ===")
    for k, v in stats.items():
        print(f"  {k:20s}: {v:,.4f}" if isinstance(v, float) else f"  {k:20s}: {v}")


def main():
    args = build_arg_parser().parse_args()
    base_config = config_from_args(args, "ensemble")

    print(f"Loading {base_config.symbol} {base_config.interval} data from {base_config.start} to {base_config.end} ...")
    df = load_ohlcv(base_config.symbol, base_config.start, base_config.end, base_config.interval, use_cache=not args.no_cache)
    print(f"Loaded {len(df)} bars.")

    results = {}
    for mode in ["ensemble", "trend_only", "meanrev_only"]:
        config = config_from_args(args, mode)
        results[mode] = run_backtest(df, config)

    equity_curve = results["ensemble"]["equity_curve"]
    if equity_curve.empty:
        print("No trading bars were produced (increase the date range or reduce warmup-bars).")
        return

    periods_per_year = 252
    bench_close = df.loc[equity_curve.index, "Close"]
    benchmark = bench_close / bench_close.iloc[0] * base_config.initial_capital
    bench_returns = benchmark.pct_change().dropna()
    bench_stats = {
        "total_return_pct": metrics.total_return(benchmark) * 100,
        "cagr_pct": metrics.cagr(benchmark, periods_per_year) * 100,
        "sharpe_ratio": metrics.sharpe_ratio(bench_returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": metrics.max_drawdown(benchmark) * 100,
    }

    print_stats(f"Buy & Hold {base_config.symbol}", bench_stats)
    for mode, label in [("ensemble", "Ensemble (trend + tactical RSI(2) + cash)"),
                         ("trend_only", "Standalone: trend-following only"),
                         ("meanrev_only", "Standalone: RSI(2) mean-reversion only")]:
        stats = metrics.summarize(results[mode]["equity_curve"], results[mode]["trades"], periods_per_year)
        print_stats(label, stats)

    ensemble_ret = metrics.summarize(equity_curve, results["ensemble"]["trades"])["total_return_pct"]
    print(f"\nEnsemble beats buy & hold on total return: {ensemble_ret > bench_stats['total_return_pct']}")
    for mode in ["trend_only", "meanrev_only"]:
        cmp_ret = metrics.summarize(results[mode]["equity_curve"], results[mode]["trades"])["total_return_pct"]
        print(f"Ensemble beats standalone {mode} on total return: {ensemble_ret > cmp_ret}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    for mode in results:
        results[mode]["trades"].to_csv(os.path.join(RESULTS_DIR, f"{base_config.symbol}_{mode}_trades.csv"), index=False)
        results[mode]["equity_curve"].to_csv(os.path.join(RESULTS_DIR, f"{base_config.symbol}_{mode}_equity_curve.csv"))
    print(f"\nSaved trade logs and equity curves to {RESULTS_DIR}")

    if not args.no_plots:
        p1 = plotting.plot_price_and_regime(df, equity_curve, base_config.symbol)
        curves = {
            "Ensemble": results["ensemble"]["equity_curve"]["equity"],
            "Trend-following only": results["trend_only"]["equity_curve"]["equity"],
            "RSI(2) mean-reversion only": results["meanrev_only"]["equity_curve"]["equity"],
            f"Buy & hold {base_config.symbol}": benchmark,
        }
        p2 = plotting.plot_equity_comparison(curves)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")


if __name__ == "__main__":
    main()

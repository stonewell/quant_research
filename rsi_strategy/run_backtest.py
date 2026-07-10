#!/usr/bin/env python
"""CLI entry point: run the RSI-2 long-only mean-reversion backtest.

Example:
    python run_backtest.py --symbol SPY --start 2018-01-01 --end 2024-12-31
"""

import argparse
import dataclasses
import os

from rsibot import metrics, plotting
from rsibot.backtester import run_backtest
from rsibot.config import RSIConfig
from rsibot.data import load_ohlcv

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    d = RSIConfig()
    p = argparse.ArgumentParser(description="RSI-2 long-only mean-reversion backtester")
    p.add_argument("--symbol", default=d.symbol)
    p.add_argument("--start", default=d.start)
    p.add_argument("--end", default=d.end)
    p.add_argument("--interval", default=d.interval)
    p.add_argument("--initial-capital", type=float, default=d.initial_capital)
    p.add_argument("--position-size-pct", type=float, default=d.position_size_pct)
    p.add_argument("--rsi-period", type=int, default=d.rsi_period)
    p.add_argument("--rsi-method", choices=["wilder", "cutler"], default=d.rsi_method)
    p.add_argument("--entry-mode", choices=["single", "cumulative"], default=d.entry_mode)
    p.add_argument("--oversold-threshold", type=float, default=d.oversold_threshold)
    p.add_argument("--cumulative-lookback", type=int, default=d.cumulative_lookback)
    p.add_argument("--cumulative-threshold", type=float, default=d.cumulative_threshold)
    p.add_argument("--no-trend-filter", action="store_false", dest="require_trend_filter", default=d.require_trend_filter)
    p.add_argument("--trend-ma-period", type=int, default=d.trend_ma_period)
    p.add_argument("--exit-mode", choices=["rsi_cross", "ma_cross", "either"], default=d.exit_mode)
    p.add_argument("--exit-rsi-threshold", type=float, default=d.exit_rsi_threshold)
    p.add_argument("--exit-ma-period", type=int, default=d.exit_ma_period)
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct)
    p.add_argument("--max-holding-days", type=int, default=d.max_holding_days)
    p.add_argument("--commission-per-trade", type=float, default=d.commission_per_trade)
    p.add_argument("--commission-pct", type=float, default=d.commission_pct)
    p.add_argument("--slippage-pct", type=float, default=d.slippage_pct)
    p.add_argument("--warmup-bars", type=int, default=d.warmup_bars)
    p.add_argument("--no-cache", action="store_true", help="force re-download instead of using cached CSV")
    p.add_argument("--no-plots", action="store_true", help="skip chart generation")
    return p


def config_from_args(args: argparse.Namespace) -> RSIConfig:
    field_names = {f.name for f in dataclasses.fields(RSIConfig)}
    kwargs = {k: v for k, v in vars(args).items() if k in field_names}
    return RSIConfig(**kwargs)


def main():
    args = build_arg_parser().parse_args()
    config = config_from_args(args)

    print(f"Loading {config.symbol} {config.interval} data from {config.start} to {config.end} ...")
    df = load_ohlcv(config.symbol, config.start, config.end, config.interval, use_cache=not args.no_cache)
    print(f"Loaded {len(df)} bars.")

    result = run_backtest(df, config)
    equity_curve, trades = result["equity_curve"], result["trades"]
    if equity_curve.empty:
        print("No trading bars were produced (increase the date range or reduce warmup-bars).")
        return

    periods_per_year = 252
    strategy_stats = metrics.summarize(equity_curve, trades, periods_per_year)

    bench_close = df.loc[equity_curve.index, "Close"]
    benchmark = bench_close / bench_close.iloc[0] * config.initial_capital
    bench_returns = benchmark.pct_change().dropna()
    benchmark_stats = {
        "total_return_pct": metrics.total_return(benchmark) * 100,
        "cagr_pct": metrics.cagr(benchmark, periods_per_year) * 100,
        "sharpe_ratio": metrics.sharpe_ratio(bench_returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": metrics.max_drawdown(benchmark) * 100,
    }

    print("\n=== RSI Strategy ===")
    for k, v in strategy_stats.items():
        print(f"  {k:22s}: {v:,.4f}" if isinstance(v, float) else f"  {k:22s}: {v}")

    print(f"\n=== Buy & Hold {config.symbol} (benchmark) ===")
    for k, v in benchmark_stats.items():
        print(f"  {k:22s}: {v:,.4f}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    trades_path = os.path.join(RESULTS_DIR, f"{config.symbol}_trades.csv")
    equity_path = os.path.join(RESULTS_DIR, f"{config.symbol}_equity_curve.csv")
    trades.to_csv(trades_path, index=False)
    equity_curve.to_csv(equity_path)
    print(f"\nSaved trade log to {trades_path}")
    print(f"Saved equity curve to {equity_path}")

    if not args.no_plots:
        p1 = plotting.plot_price_and_rsi(df, equity_curve, trades, config, config.symbol)
        p2 = plotting.plot_equity_curve(equity_curve, benchmark, config.symbol)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")


if __name__ == "__main__":
    main()

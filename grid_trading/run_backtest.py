#!/usr/bin/env python
"""CLI entry point: run the ATR-adaptive grid strategy backtest.

Example:
    python run_backtest.py --symbol SPY --start 2018-01-01 --end 2024-12-31
"""

import argparse
import dataclasses
import os

import pandas as pd

from gridbot import metrics, plotting
from gridbot.backtester import run_backtest
from gridbot.config import GridConfig
from gridbot.data import load_ohlcv

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    defaults = GridConfig()
    p = argparse.ArgumentParser(description="ATR-adaptive grid trading backtester")
    p.add_argument("--symbol", default=defaults.symbol)
    p.add_argument("--start", default=defaults.start)
    p.add_argument("--end", default=defaults.end)
    p.add_argument("--interval", default=defaults.interval)
    p.add_argument("--initial-capital", type=float, default=defaults.initial_capital)
    p.add_argument("--capital-reserve-pct", type=float, default=defaults.capital_reserve_pct)
    p.add_argument("--atr-period", type=int, default=defaults.atr_period)
    p.add_argument("--atr-multiplier", type=float, default=defaults.atr_multiplier)
    p.add_argument("--min-spacing-pct", type=float, default=defaults.min_spacing_pct)
    p.add_argument("--max-spacing-pct", type=float, default=defaults.max_spacing_pct)
    p.add_argument("--grid-levels-per-side", type=int, default=defaults.grid_levels_per_side)
    p.add_argument("--regrid-breakout-mult", type=float, default=defaults.regrid_breakout_mult)
    p.add_argument("--no-regrid-on-profit-cycle", action="store_false", dest="regrid_on_profit_cycle",
                    default=defaults.regrid_on_profit_cycle,
                    help="keep the grid fixed at its initial center instead of recentering while flat")
    p.add_argument("--position-size-pct", type=float, default=defaults.position_size_pct)
    p.add_argument("--max-open-slots", type=int, default=defaults.max_open_slots)
    p.add_argument("--trend-ma-period", type=int, default=defaults.trend_ma_period)
    p.add_argument("--trend-band-pct", type=float, default=defaults.trend_band_pct)
    p.add_argument("--drawdown-stop-pct", type=float, default=defaults.drawdown_stop_pct)
    p.add_argument("--cooldown-bars-after-stop", type=int, default=defaults.cooldown_bars_after_stop)
    p.add_argument("--commission-per-trade", type=float, default=defaults.commission_per_trade)
    p.add_argument("--commission-pct", type=float, default=defaults.commission_pct)
    p.add_argument("--slippage-pct", type=float, default=defaults.slippage_pct)
    p.add_argument("--warmup-bars", type=int, default=defaults.warmup_bars)
    p.add_argument("--no-cache", action="store_true", help="force re-download instead of using cached CSV")
    p.add_argument("--no-plots", action="store_true", help="skip chart generation")
    return p


def config_from_args(args: argparse.Namespace) -> GridConfig:
    field_names = {f.name for f in dataclasses.fields(GridConfig)}
    kwargs = {k.replace("-", "_"): v for k, v in vars(args).items() if k.replace("-", "_") in field_names}
    return GridConfig(**kwargs)


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

    periods_per_year = 252 if config.interval == "1d" else 252 * 7  # rough default; refine per interval if needed
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

    print("\n=== Grid Strategy ===")
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
        p1 = plotting.plot_price_and_grid(df, equity_curve, trades, config.symbol)
        p2 = plotting.plot_equity_curve(equity_curve, benchmark, config.symbol)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")


if __name__ == "__main__":
    main()

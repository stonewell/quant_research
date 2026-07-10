#!/usr/bin/env python
"""CLI entry point: run the trend-pullback swing strategy backtest.

Example:
    python run_backtest.py --symbol AAPL --start 2015-01-01 --end 2024-12-31

Always reports two comparison baselines: buy-and-hold of the traded symbol,
and buy-and-hold of --benchmark-symbol (default SPY), per the user's request
to compare against both.
"""

import argparse
import dataclasses
import os

from swingbot import metrics, plotting
from swingbot.backtester import run_backtest
from swingbot.config import SwingConfig
from swingbot.data import load_ohlcv

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def build_arg_parser() -> argparse.ArgumentParser:
    d = SwingConfig()
    p = argparse.ArgumentParser(description="Trend-pullback swing strategy backtester")
    p.add_argument("--symbol", default=d.symbol)
    p.add_argument("--benchmark-symbol", default=d.benchmark_symbol)
    p.add_argument("--start", default=d.start)
    p.add_argument("--end", default=d.end)
    p.add_argument("--interval", default=d.interval)
    p.add_argument("--initial-capital", type=float, default=d.initial_capital)
    p.add_argument("--sizing-mode", choices=["risk_based", "equity_pct"], default=d.sizing_mode)
    p.add_argument("--risk-per-trade-pct", type=float, default=d.risk_per_trade_pct)
    p.add_argument("--position-size-pct", type=float, default=d.position_size_pct)
    p.add_argument("--max-position-pct-of-equity", type=float, default=d.max_position_pct_of_equity)
    p.add_argument("--trend-ma-period", type=int, default=d.trend_ma_period)
    p.add_argument("--no-rising-trend-filter", action="store_false", dest="require_rising_trend_ma",
                    default=d.require_rising_trend_ma)
    p.add_argument("--trend-slope-lookback", type=int, default=d.trend_slope_lookback)
    p.add_argument("--pullback-ma-period", type=int, default=d.pullback_ma_period)
    p.add_argument("--rsi-period", type=int, default=d.rsi_period)
    p.add_argument("--entry-rsi-threshold", type=float, default=d.entry_rsi_threshold)
    p.add_argument("--exit-rsi-threshold", type=float, default=d.exit_rsi_threshold)
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct)
    p.add_argument("--reward-risk-ratio", type=float, default=d.reward_risk_ratio)
    p.add_argument("--no-trailing-stop", action="store_false", dest="use_trailing_stop", default=d.use_trailing_stop)
    p.add_argument("--trailing-activate-pct", type=float, default=d.trailing_activate_pct)
    p.add_argument("--trailing-stop-pct", type=float, default=d.trailing_stop_pct)
    p.add_argument("--max-holding-days", type=int, default=d.max_holding_days)
    p.add_argument("--commission-per-trade", type=float, default=d.commission_per_trade)
    p.add_argument("--commission-pct", type=float, default=d.commission_pct)
    p.add_argument("--slippage-pct", type=float, default=d.slippage_pct)
    p.add_argument("--warmup-bars", type=int, default=d.warmup_bars)
    p.add_argument("--no-cache", action="store_true", help="force re-download instead of using cached CSV")
    p.add_argument("--no-plots", action="store_true", help="skip chart generation")
    return p


def config_from_args(args: argparse.Namespace) -> SwingConfig:
    field_names = {f.name for f in dataclasses.fields(SwingConfig)}
    kwargs = {k: v for k, v in vars(args).items() if k in field_names}
    return SwingConfig(**kwargs)


def print_stats(title, stats):
    print(f"\n=== {title} ===")
    for k, v in stats.items():
        print(f"  {k:26s}: {v:,.4f}" if isinstance(v, float) else f"  {k:26s}: {v}")


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

    own_close = df.loc[equity_curve.index, "Close"]
    own_benchmark = own_close / own_close.iloc[0] * config.initial_capital
    own_returns = own_benchmark.pct_change().dropna()
    own_stats = {
        "total_return_pct": metrics.total_return(own_benchmark) * 100,
        "cagr_pct": metrics.cagr(own_benchmark, periods_per_year) * 100,
        "sharpe_ratio": metrics.sharpe_ratio(own_returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": metrics.max_drawdown(own_benchmark) * 100,
    }

    if config.benchmark_symbol != config.symbol:
        print(f"Loading benchmark {config.benchmark_symbol} data ...")
        spy_df = load_ohlcv(config.benchmark_symbol, config.start, config.end, config.interval, use_cache=not args.no_cache)
        spy_close = spy_df["Close"].reindex(equity_curve.index, method="ffill")
        spy_benchmark = spy_close / spy_close.iloc[0] * config.initial_capital
    else:
        spy_benchmark = own_benchmark
    spy_returns = spy_benchmark.pct_change().dropna()
    spy_stats = {
        "total_return_pct": metrics.total_return(spy_benchmark) * 100,
        "cagr_pct": metrics.cagr(spy_benchmark, periods_per_year) * 100,
        "sharpe_ratio": metrics.sharpe_ratio(spy_returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": metrics.max_drawdown(spy_benchmark) * 100,
    }

    print_stats("Swing Strategy", strategy_stats)
    print_stats(f"Buy & Hold {config.symbol} (own benchmark)", own_stats)
    print_stats(f"Buy & Hold {config.benchmark_symbol} (market benchmark)", spy_stats)

    beats_own = strategy_stats["total_return_pct"] > own_stats["total_return_pct"]
    beats_spy = strategy_stats["total_return_pct"] > spy_stats["total_return_pct"]
    print(f"\nBeats {config.symbol} buy & hold on total return: {beats_own}")
    print(f"Beats {config.benchmark_symbol} buy & hold on total return: {beats_spy}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    trades_path = os.path.join(RESULTS_DIR, f"{config.symbol}_trades.csv")
    equity_path = os.path.join(RESULTS_DIR, f"{config.symbol}_equity_curve.csv")
    trades.to_csv(trades_path, index=False)
    equity_curve.to_csv(equity_path)
    print(f"\nSaved trade log to {trades_path}")
    print(f"Saved equity curve to {equity_path}")

    if not args.no_plots:
        p1 = plotting.plot_price_and_trades(df, equity_curve, trades, config, config.symbol)
        p2 = plotting.plot_equity_curve(equity_curve, own_benchmark, spy_benchmark, config.symbol, config.benchmark_symbol)
        print(f"Saved chart to {p1}")
        print(f"Saved chart to {p2}")


if __name__ == "__main__":
    main()

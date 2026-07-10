"""Chart generation: price/trend/trades overview and equity-curve comparison."""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def plot_price_and_trades(df: pd.DataFrame, equity_curve: pd.DataFrame, trades: pd.DataFrame,
                           config, symbol: str, filename: str = "price_and_trades.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 7))

    close = df.loc[equity_curve.index, "Close"]
    trend_ma = close.rolling(config.trend_ma_period, min_periods=config.trend_ma_period).mean()
    pullback_ma = close.rolling(config.pullback_ma_period, min_periods=config.pullback_ma_period).mean()

    ax.plot(close.index, close.values, color="#1f77b4", linewidth=1, label=f"{symbol} close")
    ax.plot(trend_ma.index, trend_ma.values, color="#ff7f0e", linewidth=1, linestyle="--",
             label=f"{config.trend_ma_period}-day SMA (trend filter)")
    ax.plot(pullback_ma.index, pullback_ma.values, color="#9467bd", linewidth=0.8, linestyle=":",
             label=f"{config.pullback_ma_period}-day SMA (pullback)")

    if not trades.empty:
        buys = trades[trades["side"] == "buy"]
        sells = trades[trades["side"] == "sell"]
        ax.scatter(buys["date"], buys["price"], marker="^", color="green", s=40, label="buy", zorder=5)
        ax.scatter(sells["date"], sells["price"], marker="v", color="red", s=40, label="sell", zorder=5)

    ax.set_title(f"{symbol} - trend-pullback swing strategy: price, MAs, and fills")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_equity_curve(equity_curve: pd.DataFrame, own_benchmark: pd.Series, spy_benchmark: pd.Series,
                       symbol: str, benchmark_symbol: str, filename: str = "equity_curve.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(equity_curve.index, equity_curve["equity"], label="Swing strategy", color="#2ca02c", linewidth=1.5)
    ax1.plot(own_benchmark.index, own_benchmark.values, label=f"Buy & hold {symbol}", color="#7f7f7f", linestyle="--")
    if benchmark_symbol != symbol:
        ax1.plot(spy_benchmark.index, spy_benchmark.values, label=f"Buy & hold {benchmark_symbol}",
                  color="#d62728", linestyle=":")
    ax1.set_ylabel("Equity ($)")
    ax1.set_title("Strategy equity vs. buy-and-hold benchmarks")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.2)

    ax2.fill_between(equity_curve.index, -equity_curve["drawdown"] * 100, 0, color="#d62728", alpha=0.5)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path

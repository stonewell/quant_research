"""Chart generation: price/RSI/trades overview and equity-curve comparison."""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def plot_price_and_rsi(df: pd.DataFrame, equity_curve: pd.DataFrame, trades: pd.DataFrame,
                        config, symbol: str, filename: str = "price_and_rsi.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [2, 1]})

    close = df.loc[equity_curve.index, "Close"]
    ax1.plot(close.index, close.values, color="#1f77b4", linewidth=1, label=f"{symbol} close")
    if config.require_trend_filter:
        trend_ma = close.rolling(config.trend_ma_period, min_periods=config.trend_ma_period).mean()
        ax1.plot(trend_ma.index, trend_ma.values, color="#ff7f0e", linewidth=1, linestyle="--",
                  label=f"{config.trend_ma_period}-day SMA (trend filter)")

    if not trades.empty:
        buys = trades[trades["side"] == "buy"]
        sells = trades[trades["side"] == "sell"]
        ax1.scatter(buys["date"], buys["price"], marker="^", color="green", s=35, label="buy", zorder=5)
        ax1.scatter(sells["date"], sells["price"], marker="v", color="red", s=35, label="sell", zorder=5)

    ax1.set_title(f"{symbol} - RSI({config.rsi_period}) mean-reversion: price, trend filter, and fills")
    ax1.set_ylabel("Price")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.2)

    ax2.plot(equity_curve.index, equity_curve["rsi"], color="#9467bd", linewidth=1, label=f"RSI({config.rsi_period})")
    ax2.axhline(config.oversold_threshold, color="green", linestyle="--", linewidth=0.8, label="oversold")
    ax2.axhline(config.exit_rsi_threshold, color="red", linestyle="--", linewidth=0.8, label="exit")
    ax2.set_ylabel("RSI")
    ax2.set_ylim(0, 100)
    ax2.legend(loc="upper left")
    ax2.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_equity_curve(equity_curve: pd.DataFrame, benchmark: pd.Series, symbol: str,
                       filename: str = "equity_curve.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(equity_curve.index, equity_curve["equity"], label="RSI strategy", color="#2ca02c")
    ax1.plot(benchmark.index, benchmark.values, label=f"Buy & hold {symbol}", color="#7f7f7f", linestyle="--")
    ax1.set_ylabel("Equity ($)")
    ax1.set_title("Strategy equity vs. buy-and-hold benchmark")
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

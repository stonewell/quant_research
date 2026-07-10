"""Chart generation: price/grid/trades overview and equity-curve comparison."""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def plot_price_and_grid(df: pd.DataFrame, equity_curve: pd.DataFrame, trades: pd.DataFrame,
                         symbol: str, filename: str = "price_and_grid.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 7))

    close = df.loc[equity_curve.index, "Close"]
    ax.plot(close.index, close.values, color="#1f77b4", linewidth=1, label=f"{symbol} close")
    ax.plot(equity_curve.index, equity_curve["grid_lower"], color="#999999", linewidth=0.6, linestyle="--", label="grid bounds")
    ax.plot(equity_curve.index, equity_curve["grid_upper"], color="#999999", linewidth=0.6, linestyle="--")

    if not trades.empty:
        buys = trades[trades["side"] == "buy"]
        sells = trades[trades["side"] == "sell"]
        ax.scatter(buys["date"], buys["price"], marker="^", color="green", s=25, label="buy", zorder=5)
        ax.scatter(sells["date"], sells["price"], marker="v", color="red", s=25, label="sell", zorder=5)

    ax.set_title(f"{symbol} - ATR-Adaptive Grid: price, grid bounds, and fills")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_equity_curve(equity_curve: pd.DataFrame, benchmark: pd.Series, symbol: str,
                       filename: str = "equity_curve.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(equity_curve.index, equity_curve["equity"], label="Grid strategy", color="#2ca02c")
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

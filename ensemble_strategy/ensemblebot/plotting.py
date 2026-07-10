"""Chart generation: price with regime shading, and equity-curve comparison
across the ensemble and its standalone components."""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

REGIME_COLORS = {"trend": "#2ca02c", "range": "#ff7f0e", "downtrend": "#d62728"}


def plot_price_and_regime(df: pd.DataFrame, equity_curve: pd.DataFrame, symbol: str,
                           filename: str = "price_and_regime.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 7))

    close = df.loc[equity_curve.index, "Close"]
    ax.plot(close.index, close.values, color="black", linewidth=1, label=f"{symbol} close", zorder=5)

    regime = equity_curve["regime"]
    change_points = regime.ne(regime.shift()).cumsum()
    for _, seg in regime.groupby(change_points):
        label = seg.iloc[0]
        color = REGIME_COLORS.get(label, "#999999")
        ax.axvspan(seg.index[0], seg.index[-1], color=color, alpha=0.15)

    for label, color in REGIME_COLORS.items():
        ax.plot([], [], color=color, alpha=0.4, linewidth=8, label=f"regime: {label}")

    ax.set_title(f"{symbol} - regime classification over time")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_equity_comparison(curves: dict, filename: str = "equity_curve.png") -> str:
    """curves: {label: equity Series}, all aligned to the same index."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 7))

    palette = ["#2ca02c", "#1f77b4", "#ff7f0e", "#7f7f7f"]
    for (label, series), color in zip(curves.items(), palette):
        ax.plot(series.index, series.values, label=label, color=color, linewidth=1.3)

    ax.set_ylabel("Equity ($)")
    ax.set_title("Ensemble vs. standalone components vs. buy-and-hold")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path

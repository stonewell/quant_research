"""Chart generation: correlation heatmap, hierarchical-clustering dendrogram,
and a Hurst-exponent-vs-volatility scatter (descriptive, not strategy-specific)."""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.cluster.hierarchy import dendrogram, linkage
from scipy.spatial.distance import squareform

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")


def plot_correlation_heatmap(corr, filename: str = "correlation_heatmap.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 8))
    im = ax.imshow(corr.values, vmin=-1, vmax=1, cmap="RdBu_r")
    ax.set_xticks(range(len(corr.columns)))
    ax.set_yticks(range(len(corr.columns)))
    ax.set_xticklabels(corr.columns, rotation=90)
    ax.set_yticklabels(corr.columns)
    for i in range(len(corr.columns)):
        for j in range(len(corr.columns)):
            ax.text(j, i, f"{corr.iloc[i, j]:.2f}", ha="center", va="center", fontsize=6)
    fig.colorbar(im, ax=ax, label="correlation")
    ax.set_title("Pairwise return correlation")
    fig.tight_layout()
    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_dendrogram(corr, filename: str = "dendrogram.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    dist = np.sqrt(2 * (1 - corr.clip(-1, 1)))
    condensed = squareform(dist.to_numpy(), checks=False)
    z = linkage(condensed, method="average")

    fig, ax = plt.subplots(figsize=(10, 6))
    dendrogram(z, labels=list(corr.columns), ax=ax)
    ax.set_title("Correlation-distance clustering (redundant candidates group together)")
    ax.set_ylabel("distance = sqrt(2*(1-correlation))")
    fig.tight_layout()
    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_hurst_vs_volatility(metrics, filename: str = "hurst_vs_volatility.png") -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 7))

    colors = np.where(metrics["hurst_significant"], "#2ca02c", "#999999")
    ax.scatter(metrics["hurst"], metrics["realized_vol_annualized_pct"], c=colors, s=60, zorder=5)
    for symbol, row in metrics.iterrows():
        ax.annotate(symbol, (row["hurst"], row["realized_vol_annualized_pct"]),
                     textcoords="offset points", xytext=(5, 3), fontsize=8)

    ax.axvline(0.5, color="black", linestyle="--", linewidth=0.8)
    ax.axvspan(0.45, 0.55, color="grey", alpha=0.15, label="random-walk-like band")
    ax.set_xlabel("Hurst exponent (< 0.5 mean-reverting | > 0.5 trending)")
    ax.set_ylabel("Realized volatility (annualized %)")
    ax.set_title("Persistence vs. volatility landscape: green = statistically significant Hurst deviation from random walk")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path

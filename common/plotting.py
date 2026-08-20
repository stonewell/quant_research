"""Shared chart generation: equity-curve line chart, optionally with a
baseline/comparison series overlaid -- for cross-project reuse by
`backtester` and `strategy_generator`. Mirrors
`instrument_selection/selectorbot/plotting.py`'s conventions (Agg backend,
dpi=130, os.makedirs + savefig + close, returns the saved absolute path),
but takes an explicit `results_dir` argument since (unlike that module) this
one is called from multiple projects with different results/ directories
(including backtester's --results-dir override).
"""

import os
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def _build_equity_curve_figure(equity: pd.Series, baseline: Optional[pd.Series] = None, *,
                                strategy_label: str = "Strategy", baseline_label: str = "Baseline",
                                title: str = "Equity Curve"):
    """No I/O -- split out from plot_equity_curve so tests can assert on
    ax.get_lines() without touching disk."""
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(equity.index, equity.values, label=strategy_label, linewidth=1.5, color="#1f77b4")
    if baseline is not None and not baseline.empty:
        ax.plot(baseline.index, baseline.values, label=baseline_label, linewidth=1.5,
                color="#999999", linestyle="--")
    ax.set_title(title)
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio value")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.2)
    fig.tight_layout()
    return fig, ax


def plot_equity_curve(equity: pd.Series, results_dir: str, *, baseline: Optional[pd.Series] = None,
                       strategy_label: str = "Strategy", baseline_label: str = "Baseline",
                       title: str = "Equity Curve", filename: str = "equity_curve.png") -> str:
    """Single-line equity chart, or two lines if `baseline` is given (e.g.
    backtester's --mode standard with --baseline-symbol set). `equity`/
    `baseline` are plain pd.Series (date index -> portfolio value) -- pass
    result["equity_curve"]["equity"], not the raw run_allocation_backtest()
    dict. Saves under `results_dir` (created if missing); returns the saved
    absolute path."""
    os.makedirs(results_dir, exist_ok=True)
    fig, _ax = _build_equity_curve_figure(equity, baseline, strategy_label=strategy_label,
                                           baseline_label=baseline_label, title=title)
    path = os.path.join(results_dir, filename)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path

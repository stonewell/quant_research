"""Structural, Wyckoff, Fractal, and Volume Profile Quantitative Factor Library.

Shared bottom-layer quantitative factor and micro-structural operator library
providing strictly causal, mathematically rigorous primitives for:
1. Volume Profile (Point of Control, Value Area High/Low) calculation
2. Intraday and multi-frequency microstructure profile aggregation
3. Wyckoff structure and Chan pivot/fractal consolidation metrics

Architecture & Decoupling (Defect M5 resolution):
This module resides in `common/` and is strictly independent of `pipeline/`.
It MUST NEVER import from `pipeline.*` to ensure full environment portability
across both pipeline and ML execution environments and to prevent architectural
inversion and circular dependency bugs.
"""

from typing import Tuple
import numpy as np
import pandas as pd

__all__ = ["compute_rolling_poc_series"]


def compute_rolling_poc_series(
    bars: pd.DataFrame,
    lookback: int = 60,
    n_bins: int = 30,
    value_area_pct: float = 0.70,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Computes rolling Point of Control (POC), Value Area Low (VAL), and Value Area High (VAH).

    Parameters
    ----------
    bars : pd.DataFrame
        DataFrame with columns 'High', 'Low', 'Close', and optional 'Volume'.
    lookback : int, default 60
        Rolling window lookback length in bars.
    n_bins : int, default 30
        Number of price bins for the Volume Profile histogram.
    value_area_pct : float, default 0.70
        Fraction of volume included in the Value Area around POC.

    Returns
    -------
    Tuple[np.ndarray, np.ndarray, np.ndarray]
        Arrays of shape (n_bars,) for (poc, val, vah). The first lookback - 1 entries are NaN.
    """
    n_bars = len(bars)
    poc = np.full(n_bars, np.nan)
    val = np.full(n_bars, np.nan)
    vah = np.full(n_bars, np.nan)

    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    has_vol = "Volume" in bars.columns and not bars["Volume"].isna().all()
    vol = bars["Volume"].fillna(1.0).to_numpy(dtype=float) if has_vol else np.ones(n_bars)

    for t in range(lookback - 1, n_bars):
        w_low = low[t - lookback + 1 : t + 1]
        w_high = high[t - lookback + 1 : t + 1]
        w_vol = vol[t - lookback + 1 : t + 1]

        p_min = float(np.min(w_low))
        p_max = float(np.max(w_high))

        if p_max <= p_min or np.isnan(p_min) or np.isnan(p_max):
            poc[t] = close[t]
            val[t] = close[t] * 0.95
            vah[t] = close[t] * 1.05
            continue

        bin_width = (p_max - p_min) / n_bins
        bin_edges = np.linspace(p_min, p_max, n_bins + 1)
        bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0
        bin_vols = np.zeros(n_bins, dtype=float)

        for i in range(lookback):
            b_l = w_low[i]
            b_h = w_high[i]
            b_v = w_vol[i]
            # Distribute volume across overlapped price bins
            idx_start = max(0, min(n_bins - 1, int((b_l - p_min) / bin_width)))
            idx_end = max(0, min(n_bins - 1, int((b_h - p_min) / bin_width)))
            count = max(1, idx_end - idx_start + 1)
            v_per_bin = b_v / count
            bin_vols[idx_start : idx_end + 1] += v_per_bin

        max_bin_idx = int(np.argmax(bin_vols))
        poc[t] = bin_mids[max_bin_idx]

        # Calculate Value Area around POC
        total_vol = np.sum(bin_vols)
        target_va_vol = total_vol * value_area_pct
        accum_vol = bin_vols[max_bin_idx]
        va_low_idx = max_bin_idx
        va_high_idx = max_bin_idx

        while accum_vol < target_va_vol and (va_low_idx > 0 or va_high_idx < n_bins - 1):
            next_low_vol = bin_vols[va_low_idx - 1] if va_low_idx > 0 else 0.0
            next_high_vol = bin_vols[va_high_idx + 1] if va_high_idx < n_bins - 1 else 0.0
            if next_low_vol >= next_high_vol and va_low_idx > 0:
                va_low_idx -= 1
                accum_vol += next_low_vol
            elif va_high_idx < n_bins - 1:
                va_high_idx += 1
                accum_vol += next_high_vol
            else:
                break

        val[t] = bin_edges[va_low_idx]
        vah[t] = bin_edges[va_high_idx + 1]

    return poc, val, vah

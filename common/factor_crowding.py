"""Advisory-only cross-sectional factor crowding & quant-quake monitor (Track MS, MS5).

Computes three crowding dimensions on the top-quantile factor pool:
1. ``top_pairwise_corr``: mean off-diagonal pairwise return correlation within
   the top factor pool over ``corr_window`` vs universe baseline.
2. ``top_turnover_concentration``: share of total cross-sectional turnover
   absorbed by the top factor pool.
3. ``dispersion_compression_ratio``: current top-pool cross-sectional return
   dispersion divided by its trailing ``baseline_window`` dispersion.

Advisory-Only Contract (AT7 P1 / 2026-11-14 Guardrail (4))
----------------------------------------------------------
Every report returned by ``compute_factor_crowding_metrics`` ALWAYS sets
``advisory=True`` and ``gates_orders=False``, emits strictly JSON-native values
(no ``NaN`` / ``Infinity`` / ``np.floating`` / ``pd.Timestamp``), and returns
``status="INSUFFICIENT_HISTORY"`` (never ``"NORMAL"``) whenever cross-sectional
size ``N < min_assets`` (default 15) or history is shorter than ``corr_window``.
It is strictly decoupled from ``LV18`` volatility de-risking to avoid double
de-risking.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from common.signal_snapshot import MIN_CROSS_SECTION_ASSETS

CROWDING_STATUS_NORMAL: str = "NORMAL"
CROWDING_STATUS_WATCH: str = "CROWDED_WATCH"
CROWDING_STATUS_QUAKE: str = "QUANT_QUAKE_RISK"
CROWDING_STATUS_INSUFFICIENT: str = "INSUFFICIENT_HISTORY"

CROWDING_STATUSES = (
    CROWDING_STATUS_NORMAL,
    CROWDING_STATUS_WATCH,
    CROWDING_STATUS_QUAKE,
    CROWDING_STATUS_INSUFFICIENT,
)

# Crowding classification thresholds (single source of truth for MS5 / MS14)
CORR_WATCH_THRESHOLD: float = 0.55
CORR_QUAKE_THRESHOLD: float = 0.75
TURNOVER_CONC_WATCH_MULT: float = 1.5
TURNOVER_CONC_QUAKE_MULT: float = 2.0
DISPERSION_WATCH_THRESHOLD: float = 0.75
DISPERSION_QUAKE_THRESHOLD: float = 0.55

__all__ = [
    "CROWDING_STATUS_NORMAL",
    "CROWDING_STATUS_WATCH",
    "CROWDING_STATUS_QUAKE",
    "CROWDING_STATUS_INSUFFICIENT",
    "CROWDING_STATUSES",
    "compute_factor_crowding_metrics",
]


def _json_number(val: Any) -> Optional[float]:
    """Convert numeric value to native Python float or None (JSON-safe, allow_nan=False)."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _mean_off_diag_corr(df: pd.DataFrame) -> Optional[float]:
    """Mean off-diagonal pairwise Pearson correlation across columns of ``df``."""
    if df.shape[1] < 2 or df.shape[0] < 2:
        return None
    corr = df.corr().to_numpy(dtype=np.float64)
    n = corr.shape[0]
    tri = corr[np.triu_indices(n, k=1)]
    finite = tri[np.isfinite(tri)]
    if finite.size == 0:
        return None
    return float(np.mean(finite))


def _insufficient_report(
    reason: str,
    *,
    as_of_iso: Optional[str] = None,
    n_assets: int = 0,
    n_history_rows: int = 0,
) -> Dict[str, Any]:
    return {
        "status": CROWDING_STATUS_INSUFFICIENT,
        "available": False,
        "advisory": True,
        "gates_orders": False,
        "reason": reason,
        "as_of": as_of_iso,
        "n_assets": int(n_assets),
        "top_pool_size": 0,
        "n_history_rows": int(n_history_rows),
        "top_pairwise_corr": None,
        "baseline_pairwise_corr": None,
        "corr_surge_delta": None,
        "top_turnover_concentration": None,
        "top_turnover_relative_ratio": None,
        "dispersion_compression_ratio": None,
        "flags": [],
    }


def compute_factor_crowding_metrics(
    factor_df: Optional[pd.DataFrame],
    returns_df: Optional[pd.DataFrame],
    turnover_df: Optional[pd.DataFrame] = None,
    *,
    top_quantile: float = 0.20,
    corr_window: int = 20,
    baseline_window: int = 60,
    min_assets: int = MIN_CROSS_SECTION_ASSETS,
    as_of: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compute advisory-only factor crowding and quant-quake risk metrics (MS5).

    Returns a strictly JSON-native dict (safe under ``json.dumps(..., allow_nan=False)``)
    with ``advisory=True`` and ``gates_orders=False``.
    """
    if not (0.0 < top_quantile < 1.0):
        raise ValueError(f"top_quantile must lie in (0, 1), got {top_quantile!r}")
    if corr_window < 2:
        raise ValueError(f"corr_window must be >= 2, got {corr_window!r}")

    if (
        factor_df is None
        or returns_df is None
        or not isinstance(factor_df, pd.DataFrame)
        or not isinstance(returns_df, pd.DataFrame)
        or factor_df.empty
        or returns_df.empty
    ):
        return _insufficient_report("empty_input")

    common_cols = factor_df.columns.intersection(returns_df.columns)
    common_idx = factor_df.index.intersection(returns_df.index).sort_values()
    if as_of is not None:
        as_of_ts = pd.Timestamp(as_of)
        common_idx = common_idx[common_idx <= as_of_ts]

    if len(common_idx) == 0 or len(common_cols) < min_assets:
        return _insufficient_report(
            "insufficient_cross_section_or_dates",
            n_assets=len(common_cols),
            n_history_rows=len(common_idx),
        )

    eval_date = common_idx[-1]
    as_of_iso = pd.Timestamp(eval_date).date().isoformat()

    ret_upto = returns_df.loc[returns_df.index <= eval_date, common_cols].sort_index()
    if len(ret_upto) < corr_window:
        return _insufficient_report(
            "insufficient_return_window",
            as_of_iso=as_of_iso,
            n_assets=len(common_cols),
            n_history_rows=len(ret_upto),
        )

    f_row = pd.to_numeric(factor_df.loc[eval_date, common_cols], errors="coerce")
    valid_syms = list(f_row[np.isfinite(f_row)].index)
    n_valid = len(valid_syms)
    if n_valid < min_assets:
        return _insufficient_report(
            "below_min_cross_section_assets",
            as_of_iso=as_of_iso,
            n_assets=n_valid,
            n_history_rows=len(ret_upto),
        )

    top_n = max(2, int(math.ceil(top_quantile * n_valid)))
    top_n = min(top_n, n_valid)
    top_syms = list(f_row.loc[valid_syms].nlargest(top_n).index)

    # 1. Top-pool internal pairwise return correlation vs universe baseline
    ret_curr = ret_upto.iloc[-corr_window:]
    top_corr = _mean_off_diag_corr(ret_curr[top_syms])
    base_win = min(len(ret_upto), max(baseline_window, corr_window))
    ret_base = ret_upto.iloc[-base_win:]
    base_corr = _mean_off_diag_corr(ret_base[valid_syms])
    corr_surge = (
        (top_corr - base_corr)
        if (top_corr is not None and base_corr is not None)
        else None
    )

    if top_corr is None:
        return _insufficient_report(
            "non_finite_top_correlation",
            as_of_iso=as_of_iso,
            n_assets=n_valid,
            n_history_rows=len(ret_upto),
        )

    # 2. Top-pool turnover concentration share (optional)
    turn_conc: Optional[float] = None
    turn_rel: Optional[float] = None
    if turnover_df is not None and isinstance(turnover_df, pd.DataFrame) and not turnover_df.empty:
        if eval_date in turnover_df.index:
            t_cols = [s for s in valid_syms if s in turnover_df.columns]
            if len(t_cols) >= min_assets:
                t_row = pd.to_numeric(turnover_df.loc[eval_date, t_cols], errors="coerce")
                t_pos = t_row[np.isfinite(t_row) & (t_row >= 0.0)]
                tot_turn = float(t_pos.sum())
                if tot_turn > 1e-12:
                    top_in_t = [s for s in top_syms if s in t_pos.index]
                    if top_in_t:
                        turn_conc = float(t_pos.loc[top_in_t].sum() / tot_turn)
                        expected_share = float(len(top_in_t)) / float(len(t_pos))
                        if expected_share > 1e-12:
                            turn_rel = float(turn_conc / expected_share)

    # 3. Cross-sectional dispersion compression ratio within top pool
    disp_curr_series = ret_curr[top_syms].std(axis=1, ddof=1)
    disp_base_series = ret_base[top_syms].std(axis=1, ddof=1)
    curr_disp = float(disp_curr_series.mean()) if disp_curr_series.notna().any() else float("nan")
    base_disp = float(disp_base_series.median()) if disp_base_series.notna().any() else float("nan")
    disp_ratio: Optional[float] = None
    if math.isfinite(curr_disp) and math.isfinite(base_disp) and base_disp > 1e-12:
        disp_ratio = float(curr_disp / base_disp)

    # 4. Grading rules
    high_corr = top_corr >= CORR_WATCH_THRESHOLD
    extreme_corr = top_corr >= CORR_QUAKE_THRESHOLD
    high_turn = (
        turn_conc is not None and turn_conc >= TURNOVER_CONC_WATCH_MULT * top_quantile
    )
    extreme_turn = (
        turn_conc is not None and turn_conc >= TURNOVER_CONC_QUAKE_MULT * top_quantile
    )
    compressed_disp = (
        disp_ratio is not None and disp_ratio <= DISPERSION_WATCH_THRESHOLD
    )
    extreme_disp = (
        disp_ratio is not None and disp_ratio <= DISPERSION_QUAKE_THRESHOLD
    )

    flags = []
    if extreme_corr:
        flags.append("EXTREME_TOP_CORRELATION")
    elif high_corr:
        flags.append("HIGH_TOP_CORRELATION")
    if extreme_turn:
        flags.append("EXTREME_TURNOVER_CONCENTRATION")
    elif high_turn:
        flags.append("HIGH_TURNOVER_CONCENTRATION")
    if extreme_disp:
        flags.append("EXTREME_DISPERSION_COMPRESSION")
    elif compressed_disp:
        flags.append("DISPERSION_COMPRESSION")

    if (
        extreme_corr
        or (high_corr and (high_turn or compressed_disp))
        or (extreme_turn and extreme_disp)
    ):
        status = CROWDING_STATUS_QUAKE
    elif high_corr or high_turn or compressed_disp:
        status = CROWDING_STATUS_WATCH
    else:
        status = CROWDING_STATUS_NORMAL

    return {
        "status": status,
        "available": True,
        "advisory": True,
        "gates_orders": False,
        "reason": "ok",
        "as_of": as_of_iso,
        "n_assets": int(n_valid),
        "top_pool_size": int(len(top_syms)),
        "n_history_rows": int(len(ret_upto)),
        "top_pairwise_corr": _json_number(top_corr),
        "baseline_pairwise_corr": _json_number(base_corr),
        "corr_surge_delta": _json_number(corr_surge),
        "top_turnover_concentration": _json_number(turn_conc),
        "top_turnover_relative_ratio": _json_number(turn_rel),
        "dispersion_compression_ratio": _json_number(disp_ratio),
        "flags": flags,
    }

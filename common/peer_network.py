"""Dynamic peer network, momentum spillover, and chip-interaction alpha primitives (Track MS, MS3).

Provides strictly causal, pure-Python/NumPy/Pandas graph-alpha primitives:
1. ``build_partial_corr_adjacency``: sparse Top-K row-normalized partial-correlation
   adjacency built from Ledoit-Wolf shrunk covariance + eigendecomposition-floored
   precision matrix (never bare ``np.linalg.inv``).
2. ``build_lead_lag_adjacency``: sparse Top-K row-normalized lag-1 cross-correlation
   adjacency where column ``j`` at ``t-1`` leads row ``i`` at ``t``.
3. ``compute_peer_momentum_spillover``: rolling causal peer momentum spillover
   ``PeerMom_{i,t} = sum_j A_tilde_{ij,t} * R_{j, t-lag} - R_{i, t-lag}``.
4. ``compute_peer_chip_interaction_alpha``: cross-sectional rank of ``PeerMom``
   interacted with ``-ts_delta(ChipSpread_90_10, delta_window)``.

Architecture & NaN Contract
---------------------------
- Zero ``pipeline.*`` imports.
- Strictly preserves factor-layer ``NaN`` on warmup rows, missing impulse returns,
  and isolated nodes (never fabricates ``0.0`` or ``0.5``).
- Uses ``ledoit_wolf_shrinkage`` exclusively (never stacked with ``denoise_covariance``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common.alpha101_operators import ts_delta
from common.covariance import ledoit_wolf_shrinkage
from common.cross_sectional import cs_rank_gaussian_or_uniform

__all__ = [
    "build_partial_corr_adjacency",
    "build_lead_lag_adjacency",
    "compute_peer_momentum_spillover",
    "compute_peer_chip_interaction_alpha",
]


def _invert_shrunk_covariance(
    cov: np.ndarray,
    *,
    min_eigenvalue: float = 1e-6,
) -> np.ndarray:
    """Invert a symmetric shrunk covariance matrix via eigendecomposition floor.

    2026-11-14 Addendum Guardrail (2): never calls bare ``np.linalg.inv`` on a
    near-singular covariance matrix.
    """
    sym = 0.5 * (cov + cov.T)
    eigvals, eigvecs = np.linalg.eigh(sym)
    max_eig = float(np.max(eigvals)) if eigvals.size > 0 else 0.0
    floor = max(float(min_eigenvalue), 1e-10 * max(max_eig, 0.0))
    safe_eigvals = np.maximum(eigvals, floor)
    prec = (eigvecs * (1.0 / safe_eigvals)[None, :]) @ eigvecs.T
    return 0.5 * (prec + prec.T)


def _sparsify_and_normalize(
    scores: np.ndarray,
    *,
    top_k: int,
    positive_only: bool,
) -> np.ndarray:
    """Zero diagonal, filter sign, retain Top-K edges per row, and row-normalize."""
    n = scores.shape[0]
    mat = np.array(scores, dtype=np.float64, copy=True)
    mat[~np.isfinite(mat)] = 0.0
    np.fill_diagonal(mat, 0.0)

    if positive_only:
        mat = np.where(mat > 0.0, mat, 0.0)
    else:
        mat = np.abs(mat)

    out = np.zeros((n, n), dtype=np.float64)
    k = min(int(top_k), max(n - 1, 0))
    if k <= 0:
        return out

    for i in range(n):
        row = mat[i]
        pos_idx = np.flatnonzero(row > 1e-12)
        if pos_idx.size == 0:
            continue
        # Deterministic sort: descending value, ascending column index on ties
        order = sorted(pos_idx.tolist(), key=lambda j: (-row[j], j))
        chosen = order[:k]
        weights = row[chosen]
        total = float(np.sum(weights))
        if total > 1e-12:
            out[i, chosen] = weights / total

    return out


def build_partial_corr_adjacency(
    returns_window: pd.DataFrame,
    *,
    top_k: int = 5,
    positive_only: bool = True,
    min_obs: int = 10,
    shrinkage_target: str = "scaled_identity",
    min_eigenvalue: float = 1e-6,
) -> pd.DataFrame:
    """Build a Top-K row-normalized partial-correlation adjacency matrix.

    Estimates a Ledoit-Wolf shrunk covariance matrix over valid columns in
    ``returns_window``, inverts it via ``_invert_shrunk_covariance``, computes
    partial correlations ``rho_{ij|rest} = -Omega_ij / sqrt(Omega_ii * Omega_jj)``,
    retains the Top-K positive (or absolute) neighbors per row, and row-normalizes.
    """
    if not isinstance(returns_window, pd.DataFrame):
        raise TypeError(
            f"returns_window must be a pd.DataFrame, got {type(returns_window).__name__}"
        )
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k!r}")

    cols = list(returns_window.columns)
    adj = pd.DataFrame(0.0, index=cols, columns=cols, dtype=np.float64)
    if len(cols) < 2 or len(returns_window) < min_obs:
        return adj

    valid_cols: list[str] = []
    for c in cols:
        s = pd.to_numeric(returns_window[c], errors="coerce")
        finite = s[np.isfinite(s)]
        if len(finite) >= min_obs and float(finite.std(ddof=1)) > 1e-12:
            valid_cols.append(c)

    if len(valid_cols) < 2:
        return adj

    sub = returns_window[valid_cols].astype(np.float64).copy()
    # Fill internal column gaps with column mean only inside the trailing window
    # without mutating caller input, so Ledoit-Wolf operates on a complete block.
    if sub.isna().any().any():
        col_means = sub.mean(axis=0)
        sub = sub.fillna(col_means)

    cov_lw, _ = ledoit_wolf_shrinkage(
        sub, target=shrinkage_target, min_eigenvalue=min_eigenvalue
    )
    cov_arr = cov_lw.to_numpy(dtype=np.float64, copy=False)
    prec = _invert_shrunk_covariance(cov_arr, min_eigenvalue=min_eigenvalue)

    diag_p = np.maximum(np.diag(prec), 1e-12)
    denom = np.sqrt(np.outer(diag_p, diag_p))
    partial_corr = -prec / denom
    np.fill_diagonal(partial_corr, 0.0)

    sub_adj = _sparsify_and_normalize(partial_corr, top_k=top_k, positive_only=positive_only)
    adj.loc[valid_cols, valid_cols] = sub_adj
    return adj


def build_lead_lag_adjacency(
    returns_window: pd.DataFrame,
    *,
    top_k: int = 5,
    positive_only: bool = True,
    min_obs: int = 10,
) -> pd.DataFrame:
    """Build a Top-K row-normalized lag-1 lead-lag cross-correlation adjacency matrix.

    Entry ``(i, j)`` measures ``Corr(R_{i, t}, R_{j, t-1})`` so column ``j``
    leading row ``i`` receives positive weight in row ``i``.
    """
    if not isinstance(returns_window, pd.DataFrame):
        raise TypeError(
            f"returns_window must be a pd.DataFrame, got {type(returns_window).__name__}"
        )
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k!r}")

    cols = list(returns_window.columns)
    n = len(cols)
    adj = pd.DataFrame(0.0, index=cols, columns=cols, dtype=np.float64)
    if n < 2 or len(returns_window) < min_obs + 1:
        return adj

    arr = returns_window.to_numpy(dtype=np.float64, copy=False)
    r_curr = arr[1:, :]   # t
    r_prev = arr[:-1, :]  # t - 1

    corr_mat = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        yi = r_curr[:, i]
        for j in range(n):
            if i == j:
                continue
            xj = r_prev[:, j]
            valid = np.isfinite(yi) & np.isfinite(xj)
            if int(valid.sum()) < min_obs:
                continue
            yv = yi[valid]
            xv = xj[valid]
            sy = float(np.std(yv, ddof=1))
            sx = float(np.std(xv, ddof=1))
            if sy <= 1e-12 or sx <= 1e-12:
                continue
            corr_mat[i, j] = float(np.corrcoef(yv, xv)[0, 1])

    norm_mat = _sparsify_and_normalize(corr_mat, top_k=top_k, positive_only=positive_only)
    return pd.DataFrame(norm_mat, index=cols, columns=cols, dtype=np.float64)


def compute_peer_momentum_spillover(
    returns_panel: pd.DataFrame,
    *,
    window: int = 20,
    top_k: int = 5,
    adjacency_method: str = "partial_corr",
    positive_only: bool = True,
    min_obs: int = 10,
    return_lag: int = 0,
) -> pd.DataFrame:
    """Compute strictly causal rolling peer momentum spillover across a return panel.

    At decision date ``t`` (end-of-day ``t`` information, ``<= t`` only):
    ``PeerMom_{i, t} = sum_{j in N_t(i)} A_tilde_{ij, t} * R_{j, t - lag} - R_{i, t - lag}``.

    Default ``return_lag=0`` uses day ``t``'s realized return as the impulse (predicting
    day ``t+1``) with adjacency estimated over ``[t - window + 1 : t + 1]``. Passing
    ``return_lag=1`` shifts both the impulse and trailing adjacency window back by 1 bar.

    Factor-Layer NaN Contract
    -------------------------
    Warmup rows (``t < window - 1 + return_lag``), missing impulse returns, and isolated
    nodes (``sum_j A_tilde_{ij, t} == 0``) remain strictly ``NaN``.
    """
    if not isinstance(returns_panel, pd.DataFrame):
        raise TypeError(
            f"returns_panel must be a pd.DataFrame, got {type(returns_panel).__name__}"
        )
    if adjacency_method not in ("partial_corr", "lead_lag"):
        raise ValueError(
            f"Unknown adjacency_method {adjacency_method!r}; expected 'partial_corr' or 'lead_lag'"
        )
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window!r}")
    if return_lag < 0:
        raise ValueError(f"return_lag must be >= 0, got {return_lag!r}")

    t_len, n_cols = returns_panel.shape
    out = np.full((t_len, n_cols), np.nan, dtype=np.float64)
    start_t = (window - 1) + return_lag

    for t in range(start_t, t_len):
        imp_t = t - return_lag
        win_df = returns_panel.iloc[imp_t - window + 1 : imp_t + 1]
        imp_row = returns_panel.iloc[imp_t].to_numpy(dtype=np.float64, copy=False)
        active_mask = np.isfinite(imp_row)
        if int(active_mask.sum()) < 2:
            continue

        active_cols = [c for c, ok in zip(returns_panel.columns, active_mask) if ok]
        sub_win = win_df[active_cols]
        if adjacency_method == "partial_corr":
            adj_df = build_partial_corr_adjacency(
                sub_win, top_k=top_k, positive_only=positive_only, min_obs=min_obs
            )
        else:
            adj_df = build_lead_lag_adjacency(
                sub_win, top_k=top_k, positive_only=positive_only, min_obs=min_obs
            )

        adj_arr = adj_df.to_numpy(dtype=np.float64, copy=False)
        sub_imp = imp_row[active_mask]
        row_sums = adj_arr.sum(axis=1)
        connected = row_sums > 1e-12
        peer_ret = adj_arr @ sub_imp
        spill = np.where(connected, peer_ret - sub_imp, np.nan)
        out[t, active_mask] = spill

    return pd.DataFrame(
        out, index=returns_panel.index, columns=returns_panel.columns, dtype=np.float64
    )


def compute_peer_chip_interaction_alpha(
    peer_mom_panel: pd.DataFrame,
    chip_spread_panel: pd.DataFrame,
    *,
    delta_window: int = 5,
    rank_method: str = "unit_uniform",
) -> pd.DataFrame:
    """Compute PeerMom x (-ts_delta(ChipSpread_90_10)) interaction alpha (MS3 item 3).

    Uses ``cs_rank_gaussian_or_uniform`` from ``common.cross_sectional`` so missing
    ``PeerMom`` observations remain ``NaN`` (never ``0.5``), multiplied by
    ``-ts_delta(chip_spread_panel, delta_window)`` (positive when chip spread narrows).
    """
    if not isinstance(peer_mom_panel, pd.DataFrame) or not isinstance(
        chip_spread_panel, pd.DataFrame
    ):
        raise TypeError("peer_mom_panel and chip_spread_panel must be pd.DataFrames")
    if delta_window < 1:
        raise ValueError(f"delta_window must be >= 1, got {delta_window!r}")

    ranked_peer = cs_rank_gaussian_or_uniform(peer_mom_panel, method=rank_method)
    aligned_chip = chip_spread_panel.reindex_like(peer_mom_panel).astype(np.float64)
    neg_chip_delta = -ts_delta(aligned_chip, delta_window)
    out = ranked_peer * neg_chip_delta
    return out.astype(np.float64)

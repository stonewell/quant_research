"""Signed cross-sectional Löwdin symmetric orthogonalization for factor panels.

Academic Grounding:
- Löwdin, Per-Olov (1950). "On the Non-Orthogonality Problem Connected with
  the Use of Atomic Wave Functions in the Theory of Molecules and Crystals."
  *Journal of Chemical Physics* 18(3), 365-375.
- Klein, Robert F. & Chow, Victor (2013). "Orthogonalized Equity Risk Premia
  and Systematic Risk Decomposition." *Quantitative Finance* 13(11), 1755-1769.

Why Signed Symmetric Orthogonalization (instead of Gram-Schmidt or PCA):
1. **Order-invariant (permutation-equivariant)**: unlike Gram-Schmidt, the
   result does not depend on factor ordering.
2. **Minimum distortion & sign-preserving**: ``F* = F M^{-1/2}`` is the unique
   orthogonal basis closest to ``F`` in Frobenius norm, so near-orthogonal
   factors preserve their original sign and economic identity.
3. **Collinearity-safe**: eigenvalues of ``M = F^T F / (n - 1)`` are floored
   at ``max(lambda) * 1e-6`` so collinear inputs never produce ``inf`` or ``NaN``.

Contract:
- Per date, operate only on rows finite across all ``m`` factors.
- Z-score each column on valid rows.
- If ``n_valid < m + 2``, return the z-scores unchanged with ``degenerate=True``.
- Restore ``NaN`` on rows that had any non-finite input.
- No clipping and no post-hoc renormalizing.
"""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

import numpy as np
import pandas as pd


def lowdin_orthogonalize_matrix(
    matrix: np.ndarray,
    *,
    eig_floor_ratio: float = 1e-6,
) -> Tuple[np.ndarray, bool]:
    """Apply signed Löwdin symmetric orthogonalization to a single date's ``n x m`` block.

    Parameters
    ----------
    matrix : np.ndarray
        2D array of shape ``(n_assets, n_factors)`` for a single cross-section.
    eig_floor_ratio : float, default 1e-6
        Floor ratio applied to eigenvalues of ``M = F^T F / (n - 1)``:
        ``lambda_floored = max(lambda, max(lambda) * eig_floor_ratio)``.

    Returns
    -------
    Tuple[np.ndarray, bool]
        ``(ortho_matrix, degenerate)`` where ``ortho_matrix`` has the same shape
        as ``matrix`` (with ``NaN`` restored on invalid rows) and ``degenerate``
        is ``True`` when ``n_valid < m + 2`` or the covariance matrix is zero.
    """
    arr = np.asarray(matrix, dtype=float)
    if arr.ndim != 2:
        raise ValueError(f"Expected 2D array (n_assets, n_factors), got shape {arr.shape}")

    n_rows, m_cols = arr.shape
    out = np.full((n_rows, m_cols), np.nan, dtype=float)
    if n_rows == 0 or m_cols == 0:
        return out, True

    valid_mask = np.isfinite(arr).all(axis=1)
    n_valid = int(valid_mask.sum())
    if n_valid == 0:
        return out, True

    sub = arr[valid_mask]
    means = np.mean(sub, axis=0)
    stds = np.std(sub, axis=0, ddof=1) if n_valid > 1 else np.zeros(m_cols, dtype=float)

    z = np.zeros_like(sub, dtype=float)
    nonzero_std = np.isfinite(stds) & (stds > 0.0)
    if nonzero_std.any():
        z[:, nonzero_std] = (sub[:, nonzero_std] - means[nonzero_std]) / stds[nonzero_std]

    if n_valid < m_cols + 2:
        out[valid_mask] = z
        return out, True

    # M = F^T F / (n - 1), symmetric PSD correlation matrix of z-scored factors.
    m_mat = (z.T @ z) / float(n_valid - 1)
    m_mat = 0.5 * (m_mat + m_mat.T)

    try:
        eigvals, eigvecs = np.linalg.eigh(m_mat)
    except np.linalg.LinAlgError:
        out[valid_mask] = z
        return out, True

    max_eig = float(np.max(eigvals))
    if not np.isfinite(max_eig) or max_eig <= 0.0:
        out[valid_mask] = z
        return out, True

    floor = max_eig * float(eig_floor_ratio)
    floored = np.maximum(eigvals, floor)
    inv_sqrt = (eigvecs * (floored ** -0.5)) @ eigvecs.T
    f_star = z @ inv_sqrt

    if not np.isfinite(f_star).all():
        out[valid_mask] = z
        return out, True

    out[valid_mask] = f_star
    degenerate = bool((~nonzero_std).any())
    return out, degenerate


def lowdin_orthogonalize_panel(
    factors: Mapping[str, pd.DataFrame],
    *,
    eig_floor_ratio: float = 1e-6,
) -> Tuple[Dict[str, pd.DataFrame], pd.Series]:
    """Apply per-date signed Löwdin orthogonalization across a dict of factor panels.

    Parameters
    ----------
    factors : Mapping[str, pd.DataFrame]
        Mapping of factor name -> ``date x symbol`` DataFrame. All panels are
        aligned onto the intersection of their date indices and symbol columns.
    eig_floor_ratio : float, default 1e-6
        Eigenvalue floor ratio passed to ``lowdin_orthogonalize_matrix``.

    Returns
    -------
    Tuple[Dict[str, pd.DataFrame], pd.Series]
        ``(ortho_factors, degenerate_flags)`` where ``ortho_factors`` has the
        same keys as ``factors`` and ``degenerate_flags`` is a boolean Series
        indexed by date indicating whether that date fell back or was degenerate.
    """
    if not factors:
        return {}, pd.Series(dtype=bool, name="degenerate")

    names = list(factors.keys())
    first = factors[names[0]]
    common_idx = first.index
    common_cols = first.columns
    for name in names[1:]:
        common_idx = common_idx.intersection(factors[name].index)
        common_cols = common_cols.intersection(factors[name].columns)

    n_dates = len(common_idx)
    n_syms = len(common_cols)
    m_factors = len(names)

    # Stack into shape (n_dates, n_syms, m_factors)
    cube = np.empty((n_dates, n_syms, m_factors), dtype=float)
    for k, name in enumerate(names):
        cube[:, :, k] = factors[name].loc[common_idx, common_cols].to_numpy(dtype=float)

    out_cube = np.full_like(cube, np.nan)
    degen = np.zeros(n_dates, dtype=bool)
    for t in range(n_dates):
        f_star, is_deg = lowdin_orthogonalize_matrix(
            cube[t], eig_floor_ratio=eig_floor_ratio
        )
        out_cube[t] = f_star
        degen[t] = is_deg

    ortho_factors = {
        name: pd.DataFrame(out_cube[:, :, k], index=common_idx, columns=common_cols)
        for k, name in enumerate(names)
    }
    degen_series = pd.Series(degen, index=common_idx, name="degenerate")
    return ortho_factors, degen_series

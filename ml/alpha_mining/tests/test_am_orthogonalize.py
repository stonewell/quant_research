"""Unit tests for amine/orthogonalize.py signed Löwdin orthogonalization.

100% offline: uses synthetic factor matrices/panels only.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_AM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AM_ROOT not in sys.path:
    sys.path.insert(0, _AM_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from amine.orthogonalize import (
    lowdin_orthogonalize_matrix,
    lowdin_orthogonalize_panel,
)


def test_lowdin_corr_is_identity():
    rng = np.random.default_rng(123)
    base = rng.normal(size=(80, 4))
    # Inject strong cross-factor correlation
    mat = np.column_stack([
        base[:, 0],
        0.7 * base[:, 0] + 0.7 * base[:, 1],
        -0.5 * base[:, 0] + 0.6 * base[:, 1] + 0.6 * base[:, 2],
        0.4 * base[:, 2] + 0.9 * base[:, 3],
    ])
    f_star, degen = lowdin_orthogonalize_matrix(mat)
    assert not degen
    corr = np.corrcoef(f_star, rowvar=False)
    np.testing.assert_allclose(corr, np.eye(4), atol=1e-10)


def test_lowdin_collinear_input_stays_finite():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(50, 2))
    # Column 2 is an exact linear combination of columns 0 and 1
    collinear = np.column_stack([x[:, 0], x[:, 1], 2.0 * x[:, 0] - 0.5 * x[:, 1]])
    f_star, _ = lowdin_orthogonalize_matrix(collinear)
    assert np.isfinite(f_star).all()


def test_lowdin_column_permutation_equivariance():
    rng = np.random.default_rng(9)
    mat = rng.normal(size=(60, 4))
    mat[:, 1] += 0.6 * mat[:, 0]
    mat[:, 3] -= 0.4 * mat[:, 2]

    perm = [2, 0, 3, 1]
    f_orig, _ = lowdin_orthogonalize_matrix(mat)
    f_perm, _ = lowdin_orthogonalize_matrix(mat[:, perm])
    np.testing.assert_allclose(f_perm, f_orig[:, perm], atol=1e-12)


def test_lowdin_near_orthogonal_sign_preservation():
    rng = np.random.default_rng(77)
    n = 100
    # Construct near-orthogonal columns
    q, _ = np.linalg.qr(rng.normal(size=(n, 3)))
    mat = q + 0.02 * rng.normal(size=(n, 3))
    f_star, degen = lowdin_orthogonalize_matrix(mat)
    assert not degen
    # Diagonal correlations between original and orthogonalized columns stay strongly positive
    for j in range(3):
        c = np.corrcoef(mat[:, j], f_star[:, j])[0, 1]
        assert c > 0.95


def test_lowdin_nan_rows_restored_and_small_n_valid_degenerate():
    rng = np.random.default_rng(31)
    mat = rng.normal(size=(20, 3))
    mat[2, 1] = np.nan
    mat[7, 0] = np.inf
    f_star, degen = lowdin_orthogonalize_matrix(mat)
    assert not degen
    assert np.isnan(f_star[2]).all()
    assert np.isnan(f_star[7]).all()
    valid_rows = [i for i in range(20) if i not in (2, 7)]
    assert np.isfinite(f_star[valid_rows]).all()

    # n_valid < m + 2 triggers degenerate=True and returns z-scores
    small = rng.normal(size=(5, 4))  # n_valid = 5 < 4 + 2 = 6
    f_small, is_deg = lowdin_orthogonalize_matrix(small)
    assert is_deg is True
    assert np.isfinite(f_small).all()

    # Panel wrapper propagates degenerate flags per date
    dates = pd.bdate_range("2023-01-01", periods=3)
    cols = [f"A{i}" for i in range(6)]
    f1 = pd.DataFrame(rng.normal(size=(3, 6)), index=dates, columns=cols)
    f2 = pd.DataFrame(rng.normal(size=(3, 6)), index=dates, columns=cols)
    f3 = pd.DataFrame(rng.normal(size=(3, 6)), index=dates, columns=cols)
    # Date 1 has only 3 valid rows (< 3 + 2 = 5) -> degenerate
    f1.iloc[1, :3] = np.nan
    ortho, flags = lowdin_orthogonalize_panel({"f1": f1, "f2": f2, "f3": f3})
    assert bool(flags.iloc[0]) is False
    assert bool(flags.iloc[1]) is True
    assert bool(flags.iloc[2]) is False
    assert ortho["f1"].iloc[1, :3].isna().all()

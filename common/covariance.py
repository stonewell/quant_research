"""Random Matrix Theory (RMT) denoising for correlation/covariance matrices
estimated from a short return history.

WHY THIS EXISTS: a sample correlation matrix estimated from N assets over T
return observations is dominated by estimation noise once N is a meaningful
fraction of T -- Marchenko & Pastur (1967) showed that even for a TRUE
underlying correlation of pure identity (i.e. genuinely uncorrelated assets),
the SAMPLE correlation matrix's eigenvalues spread out over a well-defined
band purely from finite-sample noise, with an upper edge at
`lambda_plus = (1 + sqrt(N/T))**2`. Any sample eigenvalue at or below that
edge is statistically indistinguishable from noise, yet a raw sample
correlation/covariance matrix treats it as real signal -- which is the
documented mechanism behind unstable, whipsawing minimum-variance weights
and spuriously "diversified" baskets (Laloux, Cizeau, Bouchaud & Potters
1999, *Phys. Rev. Lett.* 83:1467, "Noise Dressing of Financial Correlation
Matrices"). The practical cleaning recipe used here -- clip noise-band
eigenvalues to their mean (preserving the trace), reconstruct, then rescale
the diagonal back to exactly 1 -- follows Bun, Bouchaud & Potters (2017,
*Physics Reports* 666, "Cleaning large correlation matrices: tools from
random matrix theory"), the standard reference for this exact procedure.

Deliberately does NOT touch the diagonal (per-asset variance): with N assets
there are only N variance estimates but N(N-1)/2 pairwise correlations, so
the curse of dimensionality -- and the noise this module removes -- lives
almost entirely in the OFF-diagonal correlation structure, not the
individual variances.

`n_assets <= 2` is a deliberate no-op: RMT is an asymptotic-N result, and a
2x2 correlation matrix (eigenvalues `1+rho`, `1-rho`) has no meaningful
"noise eigenvalue" concept to clip.

Mutual Exclusivity Ruling (2026-11-14 Addendum, MS3 / MS6)
----------------------------------------------------------
``ledoit_wolf_shrinkage`` (analytical linear shrinkage toward a structured
positive-definite target) and ``denoise_covariance`` / ``denoise_correlation``
(Marchenko-Pastur RMT eigenvalue clipping) are **MUTUALLY EXCLUSIVE**
covariance-regularization alternatives (**二选一互斥**). Callers MUST select
one path or the other and must never stack both on the same covariance matrix,
as stacking double-compresses the off-diagonal spectrum.
"""

from typing import Tuple, Union

import numpy as np
import pandas as pd


def denoise_correlation(corr, n_obs: int, min_noise_eigenvalues: int = 3):
    """RMT (Marchenko-Pastur) eigenvalue-clipping denoise of a correlation
    matrix estimated from `n_obs` return observations. Accepts and returns
    either a `pd.DataFrame` (index/columns preserved) or a `np.ndarray`.
    No-op when `n_assets <= 2`, `n_obs <= 0`, or fewer than
    `min_noise_eigenvalues` eigenvalues actually fall in the noise band
    (see below for why).

    SAFETY RAIL BEYOND THE TEXTBOOK RECIPE (disclosed, not part of the
    Laloux/Bun-Bouchaud-Potters procedure itself): replacing every
    noise-band eigenvalue with their shared mean is only a sound
    approximation when there are ENOUGH of them for that mean to be a
    stable estimate. With very few assets, one genuinely informative small
    eigenvalue (e.g. a single independent asset's own, entirely real,
    near-1 eigenvalue) can fall at/below `lambda_plus` alongside true noise
    -- averaging the two together corrupts it. Concretely: for 3 assets (a
    near-duplicate pair + one independent one), the near-duplicate pair's
    OWN large eigenvalue is correctly kept, but the independent asset's
    legitimate ~1.0 eigenvalue can land in the same noise band as a truly
    tiny (~0) noise eigenvalue and get averaged with it -- silently
    corrupting the near-duplicate pair's reconstructed correlation even
    though that pair's real signal (the large eigenvalue) was never
    touched. Requiring at least `min_noise_eigenvalues` in the band before
    clipping at all avoids this failure mode; when there aren't enough,
    this returns the input unchanged rather than denoise badly."""
    is_df = isinstance(corr, pd.DataFrame)
    arr = corr.to_numpy(dtype=float, copy=True) if is_df else np.array(corr, dtype=float, copy=True)
    n = arr.shape[0]

    if n <= 2 or n_obs <= 0:
        return corr.copy() if is_df else arr

    q = n / n_obs
    lambda_plus = (1.0 + np.sqrt(q)) ** 2

    eigvals, eigvecs = np.linalg.eigh(arr)  # ascending order; arr is symmetric
    noise_mask = eigvals <= lambda_plus
    if noise_mask.sum() < min_noise_eigenvalues:
        return corr.copy() if is_df else arr
    noise_mean = eigvals[noise_mask].mean()
    cleaned_eigvals = np.where(noise_mask, noise_mean, eigvals)

    cleaned = (eigvecs * cleaned_eigvals) @ eigvecs.T

    # Clipping preserves the TRACE (sum of eigenvalues) exactly, but
    # individual diagonal entries can still drift slightly during
    # reconstruction -- rescale back to a proper correlation matrix
    # (unit diagonal) per the standard recipe.
    d = np.sqrt(np.diag(cleaned))
    d[d == 0] = 1e-12
    cleaned = cleaned / np.outer(d, d)
    np.fill_diagonal(cleaned, 1.0)
    cleaned = np.clip(cleaned, -1.0, 1.0)

    if is_df:
        return pd.DataFrame(cleaned, index=corr.index, columns=corr.columns)
    return cleaned


def denoise_covariance(cov: np.ndarray, n_obs: int, min_noise_eigenvalues: int = 3) -> np.ndarray:
    """Splits `cov` into (per-asset volatilities, correlation), denoises
    ONLY the correlation structure via `denoise_correlation` (see its
    docstring for `min_noise_eigenvalues`), and rescales back by the
    ORIGINAL volatilities -- see module docstring for why the diagonal is
    left untouched. `cov` must be a plain `np.ndarray` (matches
    `_hrp_portfolio`/`_min_variance_weights`'s expected input)."""
    cov = np.array(cov, dtype=float, copy=True)
    n = cov.shape[0]
    if n <= 2 or n_obs <= 0:
        return cov

    vols = np.sqrt(np.diag(cov))
    safe_vols = np.where(vols == 0, 1e-12, vols)
    corr = np.clip(cov / np.outer(safe_vols, safe_vols), -1.0, 1.0)

    cleaned_corr = denoise_correlation(corr, n_obs, min_noise_eigenvalues=min_noise_eigenvalues)
    return cleaned_corr * np.outer(vols, vols)


def ledoit_wolf_shrinkage(
    returns: Union[pd.DataFrame, np.ndarray],
    *,
    target: str = "scaled_identity",
    min_eigenvalue: float = 1e-8,
    ddof: int = 1,
) -> Tuple[Union[pd.DataFrame, np.ndarray], float]:
    """Analytical Ledoit-Wolf linear covariance shrinkage (Task MS3 / MS6).

    Academic Grounding
    ------------------
    - ``target="scaled_identity"``: Ledoit & Wolf (2004), "A well-conditioned
      estimator for large-dimensional covariance matrices", *Journal of
      Multivariate Analysis* 88(2), 365-411. Target ``F = mu * I`` with
      ``mu = tr(S) / N``.
    - ``target="constant_correlation"``: Ledoit & Wolf (2004), "Honey, I Shrunk
      the Sample Covariance Matrix", *Journal of Portfolio Management* 30(4),
      110-119. Target ``F_ii = S_ii``, ``F_ij = r_bar * sqrt(S_ii * S_jj)``.

    Mutual Exclusivity (2026-11-14 Addendum)
    ----------------------------------------
    ``ledoit_wolf_shrinkage`` and ``denoise_covariance`` / ``denoise_correlation``
    are **MUTUALLY EXCLUSIVE** (**二选一互斥**). Do not stack both on the same
    covariance matrix.

    Parameters
    ----------
    returns : pd.DataFrame or np.ndarray
        ``T x N`` return matrix (rows = observations, columns = assets).
    target : {'scaled_identity', 'constant_correlation'}, default 'scaled_identity'
        Structured shrinkage target ``F``.
    min_eigenvalue : float, default 1e-8
        Strict positive-definite eigenvalue floor applied after shrinkage.
    ddof : int, default 1
        Degrees of freedom (``0`` or ``1``) for final covariance scaling.

    Returns
    -------
    Tuple[pd.DataFrame or np.ndarray, float]
        ``(shrunk_cov, delta)`` where ``delta in [0.0, 1.0]`` is the analytical
        shrinkage intensity and ``shrunk_cov`` preserves DataFrame labels when a
        DataFrame was passed.
    """
    if target not in ("scaled_identity", "constant_correlation"):
        raise ValueError(
            f"Unknown target {target!r}; expected 'scaled_identity' or 'constant_correlation'"
        )
    if min_eigenvalue <= 0.0 or not np.isfinite(min_eigenvalue):
        raise ValueError(f"min_eigenvalue must be positive and finite, got {min_eigenvalue!r}")
    if ddof not in (0, 1):
        raise ValueError(f"ddof must be 0 or 1, got {ddof!r}")

    is_df = isinstance(returns, pd.DataFrame)
    cols = returns.columns if is_df else None
    arr = (
        returns.to_numpy(dtype=np.float64, copy=False)
        if is_df
        else np.asarray(returns, dtype=np.float64)
    )
    if arr.ndim != 2:
        raise ValueError(f"returns must be 2-D (T x N), got shape {arr.shape}")

    _, n = arr.shape
    if n == 0:
        empty = np.empty((0, 0), dtype=np.float64)
        return (pd.DataFrame(empty, index=cols, columns=cols) if is_df else empty), 1.0

    finite_rows = np.all(np.isfinite(arr), axis=1)
    x_raw = arr[finite_rows]
    t = int(x_raw.shape[0])

    if t < 2:
        fallback = np.eye(n, dtype=np.float64) * max(float(min_eigenvalue), 1e-4)
        return (pd.DataFrame(fallback, index=cols, columns=cols) if is_df else fallback), 1.0

    x = x_raw - np.mean(x_raw, axis=0, keepdims=True)
    s_ml = (x.T @ x) / float(t)
    s_ml = 0.5 * (s_ml + s_ml.T)

    if n == 1:
        var_val = float(s_ml[0, 0]) * (float(t) / float(t - ddof) if ddof == 1 else 1.0)
        out_1 = np.array([[max(var_val, float(min_eigenvalue))]], dtype=np.float64)
        return (pd.DataFrame(out_1, index=cols, columns=cols) if is_df else out_1), 0.0

    row_sq_norms = np.sum(x * x, axis=1)
    sum_sq_outer = float(np.sum(row_sq_norms * row_sq_norms))
    s_frob_sq = float(np.sum(s_ml * s_ml))

    if target == "scaled_identity":
        mu_raw = float(np.trace(s_ml)) / float(n)
        if not np.isfinite(mu_raw) or mu_raw <= 1e-16:
            f_mat = np.eye(n, dtype=np.float64) * float(min_eigenvalue)
            delta = 1.0
        else:
            f_mat = np.eye(n, dtype=np.float64) * mu_raw
            gamma_hat = float(np.sum((s_ml - f_mat) ** 2)) / float(n)
            pi_hat = max(
                0.0,
                (sum_sq_outer - float(t) * s_frob_sq) / (float(n) * float(t) * float(t)),
            )
            delta = float(np.clip(pi_hat / gamma_hat, 0.0, 1.0)) if gamma_hat > 1e-16 else 1.0
    else:
        diag_s = np.maximum(np.diag(s_ml), 0.0)
        vols = np.sqrt(diag_s)
        nz = vols > 1e-12
        k_nz = int(nz.sum())
        if k_nz >= 2:
            sub_s = s_ml[np.ix_(nz, nz)]
            sub_v = vols[nz]
            corr_sub = np.clip(sub_s / np.outer(sub_v, sub_v), -1.0, 1.0)
            r_bar = float((np.sum(corr_sub) - np.trace(corr_sub)) / (k_nz * (k_nz - 1)))
            r_bar = float(np.clip(r_bar, -1.0 + 1e-6, 1.0 - 1e-6))
        else:
            r_bar = 0.0
        f_mat = r_bar * np.outer(vols, vols)
        np.fill_diagonal(f_mat, diag_s)
        gamma_hat = float(np.sum((s_ml - f_mat) ** 2))
        pi_hat = max(0.0, (sum_sq_outer - float(t) * s_frob_sq) / (float(t) * float(t)))
        delta = float(np.clip(pi_hat / gamma_hat, 0.0, 1.0)) if gamma_hat > 1e-16 else 1.0

    shrunk = (1.0 - delta) * s_ml + delta * f_mat
    if ddof == 1:
        shrunk = shrunk * (float(t) / float(t - 1))
    shrunk = 0.5 * (shrunk + shrunk.T)

    # Strict PSD eigenvalue floor guardrail
    eigvals, eigvecs = np.linalg.eigh(shrunk)
    if float(eigvals[0]) < float(min_eigenvalue):
        floored = np.maximum(eigvals, float(min_eigenvalue))
        shrunk = (eigvecs * floored[None, :]) @ eigvecs.T
        shrunk = 0.5 * (shrunk + shrunk.T)

    if is_df:
        return pd.DataFrame(shrunk, index=cols, columns=cols, dtype=np.float64), delta
    return shrunk, delta

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
"""

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

"""Canonical cross-sectional factor preprocessing primitives (Track MS, MS1 / MS11).

This module provides the single canonical factor-layer cross-sectional toolkit for
outlier clipping, rank/Gaussian transforms, continuous + sector OLS/WLS
neutralization, and factor-level Löwdin symmetric orthogonalization.

Factor-Layer NaN Preservation Contract
--------------------------------------
Every function in this module strictly preserves missing (``NaN``) observations:
input ``NaN`` cells remain ``NaN`` in the output, and no routine ever fabricates
zero or neutral values for missing entries. This contract is intentionally
separate from the weight-layer explicit-``0.0`` rule in the allocation backtester
(where an explicit ``0.0`` on a rebalance date means "close / zero target weight").

Reuse & Deprecation Map (MS11)
------------------------------
Legacy cross-sectional operators in ``common/alpha101_operators.py`` and
``common/sector_metadata.py`` are **not deprecated**; their existing fill behavior
is frozen for WorldQuant/GTJA/M2.1 production strategies. New factor-layer code
must go through the wrappers below, which re-apply the input ``NaN`` mask.

| New / Legacy Primitive | Kind | Reuses | Delegates / Forbids | NaN Mask Re-applied |
| :--- | :--- | :--- | :--- | :--- |
| ``cs_mad_winsorize`` | New primitive | None (vectorized NumPy) | No zero-fill | Preserved natively |
| ``cs_rank_gaussian_or_uniform`` | New primitive | ``alpha101_operators.cs_rank`` | No neutral 0.5 fill | Yes (``.where(valid)``) |
| ``cs_neutralize_ols`` | New primitive | ``cs_zscore``, ``_resolve_group_labels`` | Sector-only -> legacy | Yes (``.where(valid)``) |
| ``cs_lowdin_orthogonalize`` | New primitive | None (factor-level ``K x K``) | Distinct from weight Löwdin | Listwise ``NaN`` |
| ``cs_demean`` | Legacy (frozen) | ``alpha101_operators`` | Retained for alpha101 | Preserves ``NaN`` |
| ``cs_scale`` | Legacy (frozen) | ``alpha101_operators`` | Retained for alpha101 | Legacy fills ``0.0`` |
| ``cs_indneutralize`` | Legacy (frozen) | ``alpha101_operators`` | Sector-only demean | Preserves ``NaN`` |
| ``sector_neutral_zscore`` | Legacy (frozen) | ``sector_metadata`` | Sector-only z-score | Legacy fills ``0.0`` |
| ``sector_neutral_demean`` | Legacy (frozen) | ``sector_metadata`` | Sector-only demean | Preserves ``NaN`` |
| ``resolve_sector`` | Legacy (frozen) | ``sector_metadata`` | Symbol -> sector map | N/A |
| ``_resolve_group_labels`` | Legacy (shared) | ``sector_metadata`` | Shared group resolver | N/A |
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any, Literal, Union, overload

import numpy as np
import pandas as pd

from common.alpha101_operators import cs_rank, cs_zscore
from common.sector_metadata import _resolve_group_labels

#: Consistency factor mapping Median Absolute Deviation (MAD) to Gaussian sigma:
#: 1 / Phi^{-1}(0.75) ~= 1.4826.
MAD_TO_SIGMA: float = 1.4826

#: Consistency factor mapping Mean Absolute Deviation (MeanAD) to Gaussian sigma:
#: sqrt(pi / 2) ~= 1.2533. Used only when MAD == 0 (e.g., >50% ties).
MEANAD_TO_SIGMA: float = 1.2533

#: Neutralization mode: full requested specification (continuous + sector dummies,
#: or continuous-only when no sector dummies were requested).
MODE_FULL: str = "full"

#: Neutralization mode: sector dummies stripped due to cross-sectional DoF guard.
MODE_CONTINUOUS_ONLY: str = "continuous_only"

#: Neutralization mode: fallback to cross-sectional z-score due to small N or low DoF.
MODE_ZSCORE: str = "zscore"

#: Mode for dates with too few valid observations to produce any estimate.
MODE_INSUFFICIENT: str = "insufficient"

#: Machine-readable reuse and boundary map for contract tests (MS11).
PRIMITIVE_REUSE_MAP: dict[str, dict[str, Any]] = {
    "cs_mad_winsorize": {
        "kind": "new_primitive",
        "reuses": [],
        "delegates_sector_only_to": [],
        "nan_mask_reapplied": True,
        "distinct_from": [],
    },
    "cs_rank_gaussian_or_uniform": {
        "kind": "new_primitive",
        "reuses": ["common.alpha101_operators.cs_rank"],
        "delegates_sector_only_to": [],
        "nan_mask_reapplied": True,
        "distinct_from": [],
    },
    "cs_neutralize_ols": {
        "kind": "new_primitive",
        "reuses": [
            "common.alpha101_operators.cs_zscore",
            "common.sector_metadata._resolve_group_labels",
        ],
        "delegates_sector_only_to": [
            "common.alpha101_operators.cs_indneutralize",
            "common.sector_metadata.sector_neutral_demean",
            "common.sector_metadata.sector_neutral_zscore",
        ],
        "nan_mask_reapplied": True,
        "distinct_from": [],
    },
    "cs_lowdin_orthogonalize": {
        "kind": "new_primitive",
        "reuses": [],
        "delegates_sector_only_to": [],
        "nan_mask_reapplied": True,
        "distinct_from": [
            "BaseBlendAllocationStrategy._apply_lowdin_orthogonalization (weight-level, L478)",
        ],
    },
}

__all__ = [
    "MAD_TO_SIGMA",
    "MEANAD_TO_SIGMA",
    "MODE_FULL",
    "MODE_CONTINUOUS_ONLY",
    "MODE_ZSCORE",
    "MODE_INSUFFICIENT",
    "PRIMITIVE_REUSE_MAP",
    "cs_mad_winsorize",
    "cs_rank_gaussian_or_uniform",
    "cs_neutralize_ols",
    "cs_lowdin_orthogonalize",
]


def _to_frame(panel: Union[pd.DataFrame, pd.Series]) -> tuple[pd.DataFrame, bool, Any]:
    """Normalize a Series or DataFrame into a float64 DataFrame without mutating input."""
    if isinstance(panel, pd.Series):
        df = pd.DataFrame(
            [panel.to_numpy(dtype=np.float64, copy=True)],
            index=pd.Index([0]),
            columns=panel.index,
            dtype=np.float64,
        )
        return df, True, panel.name
    if isinstance(panel, pd.DataFrame):
        return panel.astype(np.float64).copy(), False, None
    raise TypeError(f"Expected pd.DataFrame or pd.Series, got {type(panel).__name__}")


def _restore_type(
    frame: pd.DataFrame,
    is_series: bool,
    series_name: Any,
) -> Union[pd.DataFrame, pd.Series]:
    """Restore a 1-row DataFrame back to a float64 Series when the input was a Series."""
    if is_series:
        return pd.Series(
            frame.iloc[0].to_numpy(dtype=np.float64, copy=True),
            index=frame.columns,
            name=series_name,
            dtype=np.float64,
        )
    return frame


@overload
def cs_mad_winsorize(panel: pd.DataFrame, n_mad: float = 3.0) -> pd.DataFrame: ...


@overload
def cs_mad_winsorize(panel: pd.Series, n_mad: float = 3.0) -> pd.Series: ...


def cs_mad_winsorize(
    panel: Union[pd.DataFrame, pd.Series],
    n_mad: float = 3.0,
) -> Union[pd.DataFrame, pd.Series]:
    """Cross-sectionally winsorize each row using median +/- n_mad * 1.4826 * MAD.

    Academic Grounding
    ------------------
    Hampel (1974), "The Influence Curve and Its Role in Robust Estimation", JASA;
    Rousseeuw & Croux (1993), "Alternatives to the Median Absolute Deviation", JASA;
    Leys et al. (2013), "Detecting Outliers: Do Not Use Standard Deviation Around
    the Mean, Use Absolute Deviation Around the Median", JESP.

    Scale Fallback & NaN Contract
    -----------------------------
    - Median and MAD are computed per row over finite entries only.
    - ``scale = 1.4826 * MAD``. When ``MAD == 0`` (e.g., more than half of the
      cross-section is tied), falls back to ``1.2533 * mean(|x - median|)``.
    - If ``scale`` is still zero or non-finite (e.g., an all-equal or all-``NaN``
      row), the row is returned unchanged.
    - Every non-``NaN`` entry, including ``+inf`` and ``-inf``, is clipped to
      ``[median - n_mad * scale, median + n_mad * scale]``.
    - ``NaN`` entries remain ``NaN``; an all-``NaN`` row remains all-``NaN``.

    Parameters
    ----------
    panel : pd.DataFrame or pd.Series
        Date x symbol factor panel, or single-date cross-sectional Series.
    n_mad : float, default 3.0
        Number of robust sigma units beyond the cross-sectional median.

    Returns
    -------
    pd.DataFrame or pd.Series
        Float64 winsorized output with identical type, index, and columns/name.
    """
    if not np.isfinite(n_mad) or n_mad <= 0.0:
        raise ValueError(f"n_mad must be positive and finite, got {n_mad!r}")

    frame, is_series, series_name = _to_frame(panel)
    if frame.empty:
        return _restore_type(frame, is_series, series_name)

    arr = frame.to_numpy(dtype=np.float64, copy=True)
    finite_mask = np.isfinite(arr)
    finite_arr = np.where(finite_mask, arr, np.nan)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med = np.nanmedian(finite_arr, axis=1, keepdims=True)
        abs_dev = np.abs(finite_arr - med)
        mad = np.nanmedian(abs_dev, axis=1, keepdims=True)
        scale = MAD_TO_SIGMA * mad

        zero_mad = (mad == 0.0) & np.isfinite(med)
        if np.any(zero_mad):
            mean_ad = np.nanmean(abs_dev, axis=1, keepdims=True)
            scale = np.where(zero_mad, MEANAD_TO_SIGMA * mean_ad, scale)

    active_row = np.isfinite(med) & np.isfinite(scale) & (scale > 0.0)
    lo = med - n_mad * scale
    hi = med + n_mad * scale

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        clipped = np.clip(arr, lo, hi)

    out = np.where(active_row & ~np.isnan(arr), clipped, arr)
    res = pd.DataFrame(out, index=frame.index, columns=frame.columns, dtype=np.float64)
    return _restore_type(res, is_series, series_name)


@overload
def cs_rank_gaussian_or_uniform(
    panel: pd.DataFrame,
    method: Literal["centered_uniform", "unit_uniform", "gaussian", "normal"] = "centered_uniform",
    *,
    eps: float = 1e-9,
    min_valid: int = 2,
) -> pd.DataFrame: ...


@overload
def cs_rank_gaussian_or_uniform(
    panel: pd.Series,
    method: Literal["centered_uniform", "unit_uniform", "gaussian", "normal"] = "centered_uniform",
    *,
    eps: float = 1e-9,
    min_valid: int = 2,
) -> pd.Series: ...


def cs_rank_gaussian_or_uniform(
    panel: Union[pd.DataFrame, pd.Series],
    method: str = "centered_uniform",
    *,
    eps: float = 1e-9,
    min_valid: int = 2,
) -> Union[pd.DataFrame, pd.Series]:
    """Transform each cross-section via average-tie ranks into uniform or Gaussian scores.

    Academic Grounding
    ------------------
    Van der Waerden (1952), "Order Tests for the Two-Sample Problem and Their
    Power", Indagationes Mathematicae (normal scores ``Phi^{-1}(r / (n + 1))``).

    Implementation Notes
    --------------------
    Reuses ``common.alpha101_operators.cs_rank`` and immediately re-applies the
    input ``NaN`` mask via ``.where(valid)`` because legacy ``cs_rank`` fills
    missing entries with ``0.5``. Rows with fewer than ``min_valid`` non-``NaN``
    observations are set to all-``NaN``. ``+/-inf`` entries count as valid and
    rank as extremes.

    Parameters
    ----------
    panel : pd.DataFrame or pd.Series
        Date x symbol factor panel, or single-date cross-sectional Series.
    method : {'centered_uniform', 'unit_uniform', 'gaussian', 'normal'}
        Target rank mapping. ``'normal'`` is an alias for ``'gaussian'``.
    eps : float, default 1e-9
        Tail clipping margin for the Gaussian inverse-CDF mapping ``[eps, 1-eps]``.
    min_valid : int, default 2
        Minimum non-``NaN`` observations required per cross-section (must be >= 2).

    Returns
    -------
    pd.DataFrame or pd.Series
        Float64 transformed output with identical type, index, and columns/name.
    """
    valid_methods = ("centered_uniform", "unit_uniform", "gaussian", "normal")
    if method not in valid_methods:
        raise ValueError(f"Unknown method {method!r}; expected one of {valid_methods}")
    if min_valid < 2:
        raise ValueError(f"min_valid must be >= 2, got {min_valid!r}")
    if not np.isfinite(eps) or not (0.0 < eps < 0.5):
        raise ValueError(f"eps must lie strictly in (0, 0.5), got {eps!r}")

    frame, is_series, series_name = _to_frame(panel)
    if frame.empty:
        return _restore_type(frame, is_series, series_name)

    valid = frame.notna()
    n = valid.sum(axis=1).astype(np.float64)
    row_ok = n >= float(min_valid)

    # Reuse legacy cs_rank and immediately re-apply the input NaN mask.
    pct = cs_rank(frame).where(valid)
    r = np.round(pct.mul(n, axis=0) * 2.0) / 2.0

    if method in ("unit_uniform", "centered_uniform"):
        denom = (n - 1.0).where(row_ok)
        unit = (r - 1.0).div(denom, axis=0)
        res = unit if method == "unit_uniform" else (unit - 0.5)
    else:
        from scipy.special import ndtri

        u = r.div(n + 1.0, axis=0).clip(lower=eps, upper=1.0 - eps)
        arr_g = ndtri(u.to_numpy(dtype=np.float64, copy=False))
        res = pd.DataFrame(arr_g, index=frame.index, columns=frame.columns, dtype=np.float64)

    res = res.where(valid)
    if not bool(row_ok.all()):
        res.loc[~row_ok, :] = np.nan
    res = res.astype(np.float64)
    return _restore_type(res, is_series, series_name)


def cs_neutralize_ols(
    factor: Union[pd.DataFrame, pd.Series],
    exposures: Union[Mapping[str, pd.DataFrame], pd.DataFrame, Mapping[str, pd.Series], None],
    sectors: Union[Mapping[str, str], pd.Series, None] = None,
    *,
    weights: Union[pd.DataFrame, pd.Series, None] = None,
    min_obs_full: int = 30,
    dof_multiplier: int = 3,
    min_obs_ols: int = 10,
    min_resid_dof: int = 5,
    sector_fallback: str = "auto",
    standardize_residuals: bool = True,
    return_diagnostics: bool = False,
) -> Union[
    pd.DataFrame,
    pd.Series,
    tuple[pd.DataFrame, pd.DataFrame],
    tuple[pd.Series, pd.DataFrame],
]:
    """Cross-sectionally neutralize a factor against continuous exposures and optional sectors.

    Academic Grounding
    ------------------
    Barra / Grinold & Kahn (2000), *Active Portfolio Management*, Chapter 12
    (cross-sectional factor neutralization via OLS/WLS regression with intercept
    and industry indicator variables).

    DoF Degradation Ladder
    ----------------------
    For each cross-section with ``N`` valid observations (finite factor, finite
    exposures, and positive finite weight when WLS is active), ``Kc`` continuous
    exposures, and ``G`` distinct sectors among valid assets (``K_full = Kc + G - 1``):

    1. ``N < 2``: all-``NaN``, mode ``'insufficient'``.
    2. ``N < min_obs_ols``: fallback to ``cs_zscore(row.where(valid)).where(valid)``,
       mode ``'zscore'``.
    3. Sector dummies requested (``G > 1``) and
       ``N >= max(min_obs_full, dof_multiplier * K_full)``:
       design ``[1, X, D(G-1)]`` dropping the first sorted sector label, mode ``'full'``.
    4. Sector dummies requested but threshold not met:
       design ``[1, X]`` (DoF guard strips sector dummies), mode ``'continuous_only'``.
    5. No sector dummies requested: design ``[1, X]``, mode ``'full'``.
    6. Any design without sector dummies where ``N - Kc - 1 < min_resid_dof``:
       fallback to the ``'zscore'`` path.

    Residual Standardization & Exact-Fit Note
    -----------------------------------------
    When ``standardize_residuals=True``, residuals are divided by their sample
    standard deviation (``ddof=1``) **without re-centering**, preserving exact
    orthogonality ``X^T W e = 0``. If ``std(e) <= 1e-12 * max(1, std(y))`` (an
    exact linear fit), the computed residual is ``0.0`` on valid entries; this is
    a genuine mathematical residual, not a missing-value fill. Entries outside the
    valid mask always remain ``NaN``.

    Parameters
    ----------
    factor : pd.DataFrame or pd.Series
        Raw factor panel (date x symbol) or single-date Series.
    exposures : Mapping[str, pd.DataFrame] or pd.DataFrame
        Continuous risk exposures. Must be non-empty; for sector-only
        neutralization use ``common.alpha101_operators.cs_indneutralize`` or
        ``common.sector_metadata.sector_neutral_demean`` / ``sector_neutral_zscore``.
    sectors : dict[str, str], pd.Series, or None, default None
        Optional sector specification resolved once via ``_resolve_group_labels``.
    weights : pd.DataFrame, pd.Series, or None, default None
        Optional WLS observation weights. Non-positive or non-finite weights are
        excluded from the valid mask.
    min_obs_full : int, default 30
        Minimum valid observations required to include sector dummies.
    dof_multiplier : int, default 3
        Minimum observations per regressor (``dof_multiplier * K_full``) for dummies.
    min_obs_ols : int, default 10
        Minimum valid observations required to run OLS/WLS instead of z-score fallback.
    min_resid_dof : int, default 5
        Minimum residual degrees of freedom ``N - Kc - 1`` for continuous-only OLS.
    sector_fallback : str, default 'auto'
        Fallback rule passed to ``_resolve_group_labels``.
    standardize_residuals : bool, default True
        Whether to scale residuals to unit sample std without re-centering.
    return_diagnostics : bool, default False
        If True, return ``(residuals, diagnostics_df)`` with columns
        ``['n_valid', 'n_sectors', 'k_regressors', 'mode']``.

    Returns
    -------
    pd.DataFrame or pd.Series (or tuple with diagnostics DataFrame)
        Neutralized factor residuals with identical index and columns/name.
    """
    frame, is_series, series_name = _to_frame(factor)

    if exposures is None:
        raise ValueError(
            "exposures must be non-empty; for sector-only neutralization use "
            "common.alpha101_operators.cs_indneutralize or "
            "common.sector_metadata.sector_neutral_demean / sector_neutral_zscore."
        )

    exp_arrays: list[np.ndarray] = []
    if is_series:
        if isinstance(exposures, pd.DataFrame):
            if exposures.shape[1] == 0:
                raise ValueError(
                    "exposures must be non-empty; for sector-only neutralization use "
                    "common.alpha101_operators.cs_indneutralize or "
                    "common.sector_metadata.sector_neutral_demean / sector_neutral_zscore."
                )
            aligned_exp = exposures.reindex(index=frame.columns)
            for col in aligned_exp.columns:
                arr_1d = aligned_exp[col].to_numpy(dtype=np.float64, copy=False)
                exp_arrays.append(arr_1d.reshape(1, -1))
        elif isinstance(exposures, Mapping):
            if len(exposures) == 0:
                raise ValueError(
                    "exposures must be non-empty; for sector-only neutralization use "
                    "common.alpha101_operators.cs_indneutralize or "
                    "common.sector_metadata.sector_neutral_demean / sector_neutral_zscore."
                )
            for val in exposures.values():
                if not isinstance(val, pd.Series):
                    raise TypeError(
                        "For a Series factor, exposures must be a symbols x names "
                        "DataFrame or Mapping[str, pd.Series]."
                    )
                arr_1d = val.reindex(frame.columns).to_numpy(dtype=np.float64, copy=False)
                exp_arrays.append(arr_1d.reshape(1, -1))
        else:
            raise TypeError(
                "For a Series factor, exposures must be a symbols x names DataFrame "
                f"or Mapping[str, pd.Series], got {type(exposures).__name__}."
            )
    else:
        if not isinstance(exposures, Mapping) or len(exposures) == 0:
            raise ValueError(
                "exposures must be a non-empty Mapping[str, pd.DataFrame]; for "
                "sector-only neutralization use common.alpha101_operators.cs_indneutralize "
                "or common.sector_metadata.sector_neutral_demean / sector_neutral_zscore."
            )
        for name, panel in exposures.items():
            if not isinstance(panel, pd.DataFrame):
                raise TypeError(
                    f"Exposure {name!r} must be a pd.DataFrame, got {type(panel).__name__}"
                )
            aligned = panel.reindex_like(frame)
            exp_arrays.append(aligned.to_numpy(dtype=np.float64, copy=False))

    n_rows, n_cols = frame.shape
    k_cont = len(exp_arrays)

    # Resolve sector labels once for the entire column universe.
    sec_labels: np.ndarray | None = None
    if sectors is not None:
        sec_input: Union[dict[str, str], pd.Series]
        if isinstance(sectors, pd.Series):
            sec_input = sectors
        else:
            sec_input = dict(sectors)
        sec_series = _resolve_group_labels(
            pd.Series(index=frame.columns, dtype=float),
            sec_input,
            fallback=sector_fallback,
        )
        sec_labels = sec_series.to_numpy(dtype=object)

    # Align optional WLS weights.
    w_arr: np.ndarray | None = None
    if weights is not None:
        if isinstance(weights, pd.Series):
            w_1d = weights.reindex(frame.columns).to_numpy(dtype=np.float64, copy=False)
            w_arr = np.broadcast_to(w_1d.reshape(1, -1), (n_rows, n_cols))
        elif isinstance(weights, pd.DataFrame):
            if is_series:
                raise TypeError("For a Series factor, weights must be a Series or None.")
            w_arr = weights.reindex_like(frame).to_numpy(dtype=np.float64, copy=False)
        else:
            raise TypeError(
                f"weights must be pd.DataFrame, pd.Series, or None, got {type(weights).__name__}"
            )

    y_arr = frame.to_numpy(dtype=np.float64, copy=False)
    out = np.full((n_rows, n_cols), np.nan, dtype=np.float64)
    diag_records: list[dict[str, Any]] = []

    for t in range(n_rows):
        y_row = y_arr[t]
        valid = np.isfinite(y_row)
        for e_arr in exp_arrays:
            valid &= np.isfinite(e_arr[t])
        if w_arr is not None:
            w_row = w_arr[t]
            valid &= np.isfinite(w_row) & (w_row > 0.0)

        n_valid = int(valid.sum())
        if sec_labels is not None and n_valid > 0:
            uniq_secs = sorted(set(sec_labels[valid]))
            n_sec = len(uniq_secs)
        else:
            uniq_secs = []
            n_sec = 0

        if n_valid < 2:
            diag_records.append(
                {
                    "n_valid": n_valid,
                    "n_sectors": n_sec,
                    "k_regressors": 0,
                    "mode": MODE_INSUFFICIENT,
                }
            )
            continue

        use_zscore = False
        use_dummies = False
        mode = MODE_FULL

        if n_valid < min_obs_ols:
            use_zscore = True
            mode = MODE_ZSCORE
        else:
            dummies_requested = (sec_labels is not None) and (n_sec > 1)
            if dummies_requested:
                k_full = k_cont + (n_sec - 1)
                if n_valid >= max(min_obs_full, dof_multiplier * k_full):
                    use_dummies = True
                    mode = MODE_FULL
                else:
                    use_dummies = False
                    mode = MODE_CONTINUOUS_ONLY
            else:
                use_dummies = False
                mode = MODE_FULL

            if not use_dummies and (n_valid - k_cont - 1) < min_resid_dof:
                use_zscore = True
                mode = MODE_ZSCORE

        if use_zscore:
            row_df = pd.DataFrame([y_row], columns=frame.columns, dtype=np.float64)
            valid_df = pd.DataFrame([valid], columns=frame.columns, dtype=bool)
            z_df = cs_zscore(row_df.where(valid_df)).where(valid_df)
            out[t, :] = z_df.iloc[0].to_numpy(dtype=np.float64, copy=False)
            diag_records.append(
                {
                    "n_valid": n_valid,
                    "n_sectors": n_sec,
                    "k_regressors": 0,
                    "mode": MODE_ZSCORE,
                }
            )
            continue

        # Construct design matrix: [1, X, (optional D(G-1))].
        cols_list: list[np.ndarray] = [np.ones((n_valid, 1), dtype=np.float64)]
        for e_arr in exp_arrays:
            cols_list.append(e_arr[t, valid].reshape(-1, 1))
        if use_dummies and sec_labels is not None:
            sec_v = sec_labels[valid]
            for sec_name in uniq_secs[1:]:
                cols_list.append((sec_v == sec_name).astype(np.float64).reshape(-1, 1))

        design = np.hstack(cols_list)
        k_reg = design.shape[1] - 1
        y_v = y_row[valid]

        if w_arr is not None:
            sw = np.sqrt(w_arr[t, valid])
            beta, _, _, _ = np.linalg.lstsq(design * sw[:, None], y_v * sw, rcond=None)
        else:
            beta, _, _, _ = np.linalg.lstsq(design, y_v, rcond=None)

        resid = y_v - design @ beta

        if standardize_residuals:
            y_std = float(np.std(y_v, ddof=1))
            r_std = float(np.std(resid, ddof=1))
            if not np.isfinite(r_std) or r_std <= 1e-12 * max(1.0, y_std):
                # Exact fit: mathematical residual is 0.0 (not a missing-value fill).
                resid = np.zeros_like(resid)
            else:
                # Divide by std without re-centering to preserve X^T W e = 0.
                resid = resid / r_std

        out[t, valid] = resid
        diag_records.append(
            {
                "n_valid": n_valid,
                "n_sectors": n_sec,
                "k_regressors": k_reg,
                "mode": mode,
            }
        )

    res_df = pd.DataFrame(out, index=frame.index, columns=frame.columns, dtype=np.float64)
    res_out = _restore_type(res_df, is_series, series_name)
    if return_diagnostics:
        diag_df = pd.DataFrame(
            diag_records,
            index=frame.index,
            columns=["n_valid", "n_sectors", "k_regressors", "mode"],
        )
        return res_out, diag_df
    return res_out


def cs_lowdin_orthogonalize(
    factors: Mapping[str, pd.DataFrame],
    *,
    demean: bool = True,
    scale: Literal["gram_identity", "unit_variance"] = "gram_identity",
    rcond: float = 1e-10,
    min_obs: int | None = None,
    return_diagnostics: bool = False,
) -> Union[dict[str, pd.DataFrame], tuple[dict[str, pd.DataFrame], pd.DataFrame]]:
    """Factor-level Löwdin symmetric orthogonalization across K factor panels per date.

    Academic Grounding
    ------------------
    Löwdin (1950), "On the Non-Orthogonality Problem Connected with the Use of
    Atomic Wave Functions in the Theory of Molecules and Crystals", J. Chem. Phys.;
    Klein & Chow (2013), "Orthogonalized Factors and Systematic Risk Decompositions",
    Quarterly Review of Economics and Finance.

    Architectural Distinction (MS11)
    --------------------------------
    This is the **factor-level** cross-sectional operation: on each date, it
    stacks ``K`` factor panels into an ``N_v x K`` matrix ``F``, forms the
    ``K x K`` symmetrized Gram matrix ``G = 0.5 * (F^T F + (F^T F)^T)``, and
    computes ``F* = F V_k diag(lambda_k^{-1/2}) V_k^T``. It is strictly distinct
    from the weight-level Löwdin orthogonalization on sub-strategy allocation
    matrices in ``BaseBlendAllocationStrategy._apply_lowdin_orthogonalization``
    (line 478) and never imports or reuses that code.

    Rank Deficiency & NaN Contract
    ------------------------------
    - Uses listwise deletion across the ``K`` factors per date.
    - Columns whose norm is ``<= 1e-12 * max_norm`` on a given date become ``NaN``
      on that date while remaining active columns stay orthonormal.
    - Dates with ``N_v < (min_obs or K + int(demean))`` are set to all-``NaN``
      (mode ``'insufficient'``).
    - When ``G`` is rank-deficient (e.g., duplicate factors), eigenvalues below
      ``rcond * lambda_max`` are truncated, ``F*^T F*`` is an orthogonal projector
      (``P^2 = P``), duplicate factors share the signal symmetrically, and the
      diagnostic mode is ``'rank_deficient'``.

    Parameters
    ----------
    factors : Mapping[str, pd.DataFrame]
        Non-empty mapping from factor name to aligned date x symbol DataFrame.
    demean : bool, default True
        Whether to demean each factor column across valid assets on each date.
    scale : {'gram_identity', 'unit_variance'}, default 'gram_identity'
        ``'gram_identity'`` yields ``F*^T F* = I``; ``'unit_variance'`` rescales
        by ``sqrt(N_v - 1)`` so sample variance (``ddof=1``) is 1 and correlation is ``I``.
    rcond : float, default 1e-10
        Relative eigenvalue cutoff ``lambda > rcond * lambda_max``.
    min_obs : int or None, default None
        Minimum listwise-valid observations per date. Defaults to ``K + int(demean)``.
    return_diagnostics : bool, default False
        If True, return ``(ortho_dict, diagnostics_df)`` with columns
        ``['n_valid', 'effective_rank', 'mode']``.

    Returns
    -------
    dict[str, pd.DataFrame] (or tuple with diagnostics DataFrame)
        Orthogonalized float64 panels with identical keys, index, and columns.
    """
    if not isinstance(factors, Mapping) or len(factors) == 0:
        raise ValueError("factors must be a non-empty Mapping[str, pd.DataFrame]")
    if scale not in ("gram_identity", "unit_variance"):
        raise ValueError(f"Unknown scale {scale!r}; expected 'gram_identity' or 'unit_variance'")
    if not np.isfinite(rcond) or rcond < 0.0:
        raise ValueError(f"rcond must be finite and >= 0, got {rcond!r}")

    names = list(factors.keys())
    k_total = len(names)
    first_df = factors[names[0]]
    if not isinstance(first_df, pd.DataFrame):
        raise TypeError(f"Factor {names[0]!r} must be a pd.DataFrame")
    ref_index = first_df.index
    ref_cols = first_df.columns

    arrays: list[np.ndarray] = []
    for name in names:
        df = factors[name]
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"Factor {name!r} must be a pd.DataFrame, got {type(df).__name__}")
        if not df.index.equals(ref_index) or not df.columns.equals(ref_cols):
            raise ValueError(
                f"Factor {name!r} index/columns do not match {names[0]!r}; "
                "all panels must be aligned before cs_lowdin_orthogonalize."
            )
        arrays.append(df.to_numpy(dtype=np.float64, copy=False))

    n_rows, n_cols = first_df.shape
    min_req = min_obs if min_obs is not None else (k_total + int(demean))
    if scale == "unit_variance":
        min_req = max(min_req, 2)

    out_arrays = [np.full((n_rows, n_cols), np.nan, dtype=np.float64) for _ in range(k_total)]
    diag_records: list[dict[str, Any]] = []

    for t in range(n_rows):
        mat = np.column_stack([arr[t] for arr in arrays])  # (N, K)
        valid = np.all(np.isfinite(mat), axis=1)
        n_v = int(valid.sum())

        if n_v < min_req or n_v < 1:
            diag_records.append(
                {"n_valid": n_v, "effective_rank": 0, "mode": MODE_INSUFFICIENT}
            )
            continue

        f_mat = mat[valid].astype(np.float64, copy=True)
        if demean:
            f_mat = f_mat - f_mat.mean(axis=0, keepdims=True)

        col_norms = np.linalg.norm(f_mat, axis=0)
        max_norm = float(np.max(col_norms)) if k_total > 0 else 0.0
        if not np.isfinite(max_norm) or max_norm <= 0.0:
            diag_records.append(
                {"n_valid": n_v, "effective_rank": 0, "mode": MODE_INSUFFICIENT}
            )
            continue

        active_mask = col_norms > (1e-12 * max_norm)
        k_act = int(active_mask.sum())
        if k_act == 0:
            diag_records.append(
                {"n_valid": n_v, "effective_rank": 0, "mode": MODE_INSUFFICIENT}
            )
            continue

        f_act = f_mat[:, active_mask]
        ftf = f_act.T @ f_act
        gram = 0.5 * (ftf + ftf.T)

        eigvals, eigvecs = np.linalg.eigh(gram)
        lam_max = float(eigvals[-1])
        if not np.isfinite(lam_max) or lam_max <= 0.0:
            diag_records.append(
                {"n_valid": n_v, "effective_rank": 0, "mode": MODE_INSUFFICIENT}
            )
            continue

        keep = eigvals > (rcond * lam_max)
        eff_rank = int(keep.sum())
        if eff_rank == 0:
            diag_records.append(
                {"n_valid": n_v, "effective_rank": 0, "mode": MODE_INSUFFICIENT}
            )
            continue

        v_k = eigvecs[:, keep]
        inv_sqrt = eigvals[keep] ** (-0.5)
        f_star = f_act @ (v_k * inv_sqrt[None, :]) @ v_k.T

        if scale == "unit_variance":
            f_star = f_star * np.sqrt(float(n_v - 1))

        active_indices = np.flatnonzero(active_mask)
        for j, orig_k in enumerate(active_indices):
            out_arrays[orig_k][t, valid] = f_star[:, j]

        mode = "full_rank" if (k_act == k_total and eff_rank == k_total) else "rank_deficient"
        diag_records.append(
            {"n_valid": n_v, "effective_rank": eff_rank, "mode": mode}
        )

    out_dict = {
        name: pd.DataFrame(out_arrays[idx], index=ref_index, columns=ref_cols, dtype=np.float64)
        for idx, name in enumerate(names)
    }
    if return_diagnostics:
        diag_df = pd.DataFrame(
            diag_records,
            index=ref_index,
            columns=["n_valid", "effective_rank", "mode"],
        )
        return out_dict, diag_df
    return out_dict

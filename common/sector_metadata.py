"""
common/sector_metadata.py

Offline sector metadata resolution and cross-sectional sector-neutralization.
Task M2.1 Core Layer.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Union

import numpy as np
import pandas as pd

__version__ = "1.0.0"

_LOGGER = logging.getLogger(__name__)

# Validated sector taxonomy matching pipeline/research/blend/blend_base.py
VALID_SECTORS = (
    "coal",
    "oil_petrochem",
    "shipping",
    "aviation",
    "telecom",
    "banking",
    "insurance",
    "metals",
    "machinery",
    "semiconductor",
    "computer_ict",
    "telecom_cpo",
    "auto_parts",
    "construction",
)

VALID_FALLBACKS = ("auto", "prefix", "other")


def _prefix_sector(symbol: str) -> str:
    """Classifies an asset by its ticker prefix pseudo-class.

    NOTE: This is a faithful copy of the prefix logic, with added `.strip()`/`str()`
    coercion for robustness, from pipeline/research/blend/blend_base.py:66-71
    (`_get_symbol_sector`). Directly importing BaseBlendAllocationStrategy or blend_base
    from the root common/ layer would violate architectural layering and create a circular
    dependency (common -> pipeline.research.blend -> common).
    """
    raw = str(symbol).split(".")[0].strip()
    return f"sec_{raw[:2]}" if len(raw) >= 2 else "sec_other"


def load_sector_map(path: Optional[str] = None) -> dict[str, str]:
    """Loads a JSON sector mapping file into an uppercase-keyed dictionary.

    Parameters
    ----------
    path : Optional[str]
        Path to JSON sector map file. If None or empty, returns an empty dict {}.
        If the file does not exist or fails to parse, logs a warning and returns {}.
        Never raises FileNotFoundError or json.JSONDecodeError.

    Returns
    -------
    dict[str, str]
        Dictionary mapping uppercase symbol to sector label. Keys beginning with
        an underscore (such as metadata header '_meta') are strictly omitted.
    """
    if not path:
        return {}
    if not os.path.exists(path):
        _LOGGER.warning("Sector map file not found at %s; returning empty map.", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        _LOGGER.warning("Failed to load or parse sector map at %s: %s; returning empty map.", path, exc)
        return {}

    if not isinstance(data, dict):
        _LOGGER.warning("Sector map at %s is not a JSON object; returning empty map.", path)
        return {}

    result: dict[str, str] = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        result[str(k).strip().upper()] = str(v).strip()
    return result


def resolve_sector(
    symbol: str,
    sector_map: Optional[dict[str, str]] = None,
    *,
    fallback: str = "auto",
) -> str:
    """Resolves an asset symbol into its corresponding sector category.

    Assumes `symbol` is a single unique ticker identifier string.

    Parameters
    ----------
    symbol : str
        Asset ticker/symbol (e.g. '601288.SH', 'AAPL', '0700.HK').
    sector_map : Optional[dict[str, str]]
        Mapping of uppercase symbols to sector labels.
    fallback : str, default 'auto'
        Fallback strategy when `symbol` is not found in `sector_map`:
        - 'auto': For Chinese A-shares (identified by 6-digit numeric ticker
          with .SH/.SZ suffix), falls back to the prefix pseudo-class via `_prefix_sector`.
          For all other assets (US tickers, HK stocks, synthetic IDs), falls back to 'other'.
          (Rationale: prefixing US tickers like 'AAPL' yields meaningless pseudo-sectors
          like 'sec_AA', which would distort cross-sectional groupings. Restricting prefix
          fallback to genuine A-share 6-digit codes resolves this ambiguity safely).
        - 'prefix': Unconditionally classifies via `_prefix_sector(symbol)`.
        - 'other': Unconditionally falls back to 'other'.

    Returns
    -------
    str
        Resolved sector name.

    Raises
    ------
    ValueError
        If `fallback` is not one of VALID_FALLBACKS ('auto', 'prefix', 'other').
    """
    if fallback not in VALID_FALLBACKS:
        raise ValueError(f"unknown fallback: {fallback!r}")

    sym_clean = str(symbol).strip()
    sym_upper = sym_clean.upper()

    if sector_map:
        val = None
        if sym_upper in sector_map:
            val = sector_map[sym_upper]
        elif sym_clean in sector_map:
            val = sector_map[sym_clean]

        if val is not None and not (isinstance(val, float) and np.isnan(val)):
            val_str = str(val).strip()
            if val_str:
                return val_str

    if fallback == "prefix":
        return _prefix_sector(sym_upper)
    elif fallback == "other":
        return "other"
    else:  # fallback == "auto"
        parts = sym_upper.split(".")
        raw = parts[0]
        suffix = parts[1] if len(parts) > 1 else ""
        is_ashare = len(raw) == 6 and raw.isdigit() and suffix in ("SH", "SZ")
        if is_ashare:
            return _prefix_sector(sym_upper)
        return "other"


def _resolve_group_labels(
    values: pd.Series,
    sector: Union[dict[str, str], pd.Series, None] = None,
    *,
    fallback: str = "auto",
) -> pd.Series:
    """Resolves sector group labels for each asset in values.index while strictly preserving index.

    Shared grouping helper used by both `sector_neutral_zscore` and `sector_neutral_demean`
    to guarantee identical sector resolution and prevent logic drift.
    """
    if isinstance(sector, pd.Series):
        aligned_sec = sector.reindex(values.index)
        sec_list = [
            resolve_sector(s, None, fallback=fallback)
            if pd.isna(aligned_sec.loc[s]) or str(aligned_sec.loc[s]).strip() == ""
            else str(aligned_sec.loc[s]).strip()
            for s in values.index
        ]
        return pd.Series(sec_list, index=values.index)
    elif isinstance(sector, dict):
        sec_list = [resolve_sector(s, sector, fallback=fallback) for s in values.index]
        return pd.Series(sec_list, index=values.index)
    else:
        sec_list = [resolve_sector(s, None, fallback=fallback) for s in values.index]
        return pd.Series(sec_list, index=values.index)


def sector_neutral_zscore(
    values: pd.Series,
    sector: Union[dict[str, str], pd.Series, None] = None,
    *,
    fallback: str = "auto",
    ddof: int = 1,
) -> pd.Series:
    """Computes cross-sectional sector-neutralized z-scores for a factor series.

    Assumes a UNIQUE index for `values` (and `sector` when passed as pd.Series).
    Duplicate index labels are unsupported and may raise an error rather than
    silently misaligning cross-sectional data.

    Guarantees:
    1. Input-output index contract: The resulting Series strictly preserves the
       original index length, order, and name. No symbols are dropped or filtered.
    2. Degenerate sector safety: Any sector group with <= 1 member, or with zero
       variance (all members having equal factor values), is assigned exactly 0.0
       for all its members. No division by zero, no NaN, no exception is raised.
    3. Multi-sector isolation: A degenerate sector will not contaminate or NaN
       other valid non-degenerate sectors in the cross-section.
    4. Sector-demeaning invariant: For any non-degenerate sector group, the mean
       of output z-scores across its members is 0.0 (within numerical tolerance 1e-9).
    5. Unique index assumption: Assumes a unique index for values and sector.

    Parameters
    ----------
    values : pd.Series
        Raw factor values indexed by symbol strings. Assumes a UNIQUE index.
    sector : Union[dict[str, str], pd.Series, None]
        Sector specification. Can be:
        - A sector mapping dictionary (symbol -> sector).
        - A pd.Series of sector labels indexed by symbols.
        - None (will resolve purely using the `fallback` rule).
    fallback : str, default 'auto'
        Fallback mode passed to `resolve_sector` ('auto', 'prefix', 'other').
        Raises ValueError if unrecognised.
    ddof : int, default 1
        Degrees of freedom for sample standard deviation.

    Returns
    -------
    pd.Series
        Sector-neutralized z-score series with identical index to `values`.

    Raises
    ------
    ValueError
        If `fallback` is not one of VALID_FALLBACKS ('auto', 'prefix', 'other').
    """
    if fallback not in VALID_FALLBACKS:
        raise ValueError(f"unknown fallback: {fallback!r}")

    if values.empty:
        return pd.Series(dtype=float, index=values.index, name=values.name)

    # 1. Resolve sector label for each asset while strictly preserving index
    sec_series = _resolve_group_labels(values, sector, fallback=fallback)

    output = pd.Series(0.0, index=values.index, dtype=float, name=values.name)

    # 2. Group-wise neutralization
    for sec_name, group_syms in sec_series.groupby(sec_series).groups.items():
        sub_vals = values.loc[group_syms]
        valid_mask = sub_vals.notna()
        n_valid = int(valid_mask.sum())

        # Degenerate guard: <= 1 valid member gets exactly 0.0
        if n_valid <= 1:
            output.loc[group_syms] = 0.0
            continue

        valid_vals = sub_vals.loc[valid_mask].astype(float)
        mean_val = float(valid_vals.mean())
        std_val = float(valid_vals.std(ddof=ddof))

        # Degenerate guard: zero variance or NaN std gets exactly 0.0
        if pd.isna(std_val) or std_val == 0.0 or np.isclose(std_val, 0.0):
            output.loc[group_syms] = 0.0
        else:
            z_vals = (valid_vals - mean_val) / std_val
            output.loc[valid_vals.index] = z_vals
            if n_valid < len(sub_vals):
                nan_idx = sub_vals.index[~valid_mask]
                output.loc[nan_idx] = 0.0

    return output


def sector_neutral_demean(
    values: pd.Series,
    sector: Union[dict[str, str], pd.Series, None] = None,
    *,
    fallback: str = "auto",
    min_group_size: int = 2,
) -> pd.Series:
    """Demeans a series within each sector while preserving overall score magnitude and scale.

    Unlike `sector_neutral_zscore` which standardizes values to unit variance and zero mean
    (z-scale), `sector_neutral_demean` subtracts the sector-level mean and adds back the
    global mean across all valid assets: `out = raw - group_mean + global_mean`. This removes
    sector-level shifts while preserving the score's native magnitude and scale (e.g. 0-100).

    Assumes a UNIQUE index for `values` (and `sector` when passed as pd.Series).
    Duplicate index labels are unsupported and may misalign cross-sectional data.

    Degradation and Invariant Properties:
    1. Single-group identity: When every symbol resolves to a single group (or on universes
       where no sector information exists), group_mean == global_mean, so the transform
       is an exact mathematical identity (graceful no-op degradation).
    2. Small and zero-spread groups: Groups with size < min_group_size OR zero spread
       (all values equal within float tolerance) have their members' raw values left UNTOUCHED.
       Rationale: A one- or two-member group carries no usable within-group information,
       and a zero-spread group has nothing to shift; forcing them to a constant would destroy
       real cross-sectional information for no benefit.
    3. Missing values: Input NaN stays NaN in the output; no other NaN is introduced.
    4. No clipping or rescaling: This function never clips or rescales values. Any bounding
       (such as clipping to [0, 100]) is a scoring-layer policy belonging to the caller.
    5. Input-output index contract: The resulting Series strictly preserves original index
       length, order, and name. Map-less or unknown symbols are never dropped.

    Parameters
    ----------
    values : pd.Series
        Raw factor/score values indexed by symbol strings. Assumes a UNIQUE index.
    sector : Union[dict[str, str], pd.Series, None], default None
        Sector specification: dict (symbol -> sector), pd.Series, or None.
    fallback : str, default 'auto'
        Fallback mode passed to `resolve_sector` ('auto', 'prefix', 'other').
    min_group_size : int, default 2
        Minimum number of valid members required in a sector group to apply demeaning.
        Must be >= 1.

    Returns
    -------
    pd.Series
        Sector-neutral demeaned series with identical index to `values`.

    Raises
    ------
    ValueError
        If `fallback` is not in VALID_FALLBACKS or `min_group_size < 1`.
    """
    if fallback not in VALID_FALLBACKS:
        raise ValueError(f"unknown fallback: {fallback!r}")
    if min_group_size < 1:
        raise ValueError(f"min_group_size must be >= 1, got {min_group_size}")

    if values.empty:
        return pd.Series(dtype=float, index=values.index, name=values.name)

    # 1. Resolve sector label for each asset while strictly preserving index
    sec_series = _resolve_group_labels(values, sector, fallback=fallback)

    # 2. Global mean across all valid values (NaN-skipping)
    global_mean = float(values.mean())
    output = values.astype(float).copy()

    # Short-circuit: when all symbols resolve to a single group,
    # group_mean == global_mean identically, making this transform a no-op identity.
    if sec_series.nunique() <= 1:
        return output

    # 3. Group-wise demeaning
    for sec_name, group_syms in sec_series.groupby(sec_series).groups.items():
        sub_vals = values.loc[group_syms]
        valid_mask = sub_vals.notna()
        n_valid = int(valid_mask.sum())

        # Guard: size < min_group_size leaves raw values untouched
        if n_valid < min_group_size:
            continue

        valid_vals = sub_vals.loc[valid_mask].astype(float)
        val_min = float(valid_vals.min())
        val_max = float(valid_vals.max())
        spread = val_max - val_min

        # Guard: zero spread (all values equal within float tolerance) leaves untouched
        if np.isclose(spread, 0.0):
            continue

        group_mean = float(valid_vals.mean())
        shifted = valid_vals - group_mean + global_mean
        output.loc[valid_vals.index] = shifted

    return output

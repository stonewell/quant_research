"""Vectorized RankIC, multi-objective GP fitness, and greedy factor selection.

Academic & Quantitative Grounding:
- Cross-sectional Spearman RankIC and Harvey-Liu-Zhu (2016) significance
  testing: ``common/factor_analyzer.py`` (``compute_rank_ic``, ``factor_ic_summary``).
- Multi-objective GP fitness combining predictive power with turnover,
  collinearity, and parsimony penalties:
  ``F = |IC| * sqrt(|IR_daily|) - lambda_1 * TO - lambda_2 * max(0, C - C_max) / (1 - C_max) - lambda_3 * (nodes / max_nodes)``.

Design Notes:
- **Daily IR scale**: ``factor_ic_summary["ic_ir"]`` is annualized (x sqrt(252)),
  which would swamp the penalty terms inside ``sqrt(ICIR)``. Fitness uses
  daily ``IR = mean(IC) / std(IC)`` instead.
- **Vectorized speed**: ``fast_rank_ic`` vectorizes per-date average-ranking
  and row-wise Pearson correlation to match ``compute_rank_ic`` within ``1e-10``
  while running ~100x faster inside the GP loop. Every reported/gate summary
  still delegates to ``common.factor_analyzer.factor_ic_summary``.
- **Sign orientation**: ``s = sign(IC)`` is stored and applied as ``s * expr``
  so negative-IC trees are not discarded and ``sqrt(|IR|)`` is always real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from common.alpha101_operators import (
    cs_demean,
    cs_rank,
    cs_scale,
    ts_corr,
    ts_delay,
    ts_delta,
)
from common.daily_factors import momentum_1m_1w
from common.factor_analyzer import factor_ic_summary


@dataclass
class FitnessResult:
    """Multi-objective fitness evaluation for a single candidate factor."""

    fitness: float
    mean_ic: float
    std_ic: float
    daily_ir: float
    sign: float
    turnover: float
    max_corr: float
    node_count: int
    n_obs: int
    rejected: bool = False
    reject_reason: str = ""


@dataclass
class SelectedFactor:
    """An accepted GP factor with its orientation sign, panel, and IC summary."""

    name: str
    expression: str
    sign: float
    fitness: float
    node_count: int
    max_window: int
    panel: pd.DataFrame
    fitness_result: FitnessResult
    node: Optional[Any] = None
    ic_summary: Dict[str, Any] = field(default_factory=dict)


def fast_rank_ic(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    min_assets: int = 3,
) -> pd.Series:
    """Vectorized daily cross-sectional Spearman Rank IC.

    Matches ``common.factor_analyzer.compute_rank_ic`` to within ``1e-10``:
    - Aligns on intersection of index and columns.
    - Masks out any cell where either input is ``NaN`` or non-finite.
    - Dates with fewer than ``min_assets`` valid pairs return ``NaN``.
    - Dates where either valid cross-section is constant return ``0.0``.
    - Uses average ranks for ties, identical to ``scipy.stats.spearmanr``.
    """
    common_idx = factor_df.index.intersection(forward_returns_df.index)
    common_cols = factor_df.columns.intersection(forward_returns_df.columns)

    if len(common_idx) == 0 or len(common_cols) < min_assets:
        return pd.Series(dtype=float, index=common_idx, name="rank_ic")

    f_arr = factor_df.loc[common_idx, common_cols].to_numpy(dtype=float)
    r_arr = forward_returns_df.loc[common_idx, common_cols].to_numpy(dtype=float)

    valid = np.isfinite(f_arr) & np.isfinite(r_arr)
    n_valid = valid.sum(axis=1)

    # Mask invalid cells to NaN before row-wise average ranking so ties and
    # ranks are computed strictly over the jointly-valid cross-section.
    f_masked = np.where(valid, f_arr, np.nan)
    r_masked = np.where(valid, r_arr, np.nan)

    rf = pd.DataFrame(f_masked).rank(axis=1, method="average", na_option="keep").to_numpy(dtype=float)
    rr = pd.DataFrame(r_masked).rank(axis=1, method="average", na_option="keep").to_numpy(dtype=float)

    rf_0 = np.where(valid, rf, 0.0)
    rr_0 = np.where(valid, rr, 0.0)
    denom_n = np.maximum(n_valid, 1).astype(float)[:, None]

    mean_f = rf_0.sum(axis=1, keepdims=True) / denom_n
    mean_r = rr_0.sum(axis=1, keepdims=True) / denom_n

    df_c = np.where(valid, rf - mean_f, 0.0)
    dr_c = np.where(valid, rr - mean_r, 0.0)

    ss_f = (df_c * df_c).sum(axis=1)
    ss_r = (dr_c * dr_c).sum(axis=1)
    cov_fr = (df_c * dr_c).sum(axis=1)

    denom = np.sqrt(ss_f * ss_r)
    corr = np.zeros(len(common_idx), dtype=float)
    non_const = denom > 1e-15
    corr[non_const] = cov_fr[non_const] / denom[non_const]
    corr = np.clip(corr, -1.0, 1.0)
    corr[n_valid < min_assets] = np.nan

    return pd.Series(corr, index=common_idx, name="rank_ic")


def compute_factor_turnover(factor_df: pd.DataFrame) -> float:
    """Compute mean daily one-way portfolio turnover of a long-short rank book.

    Constructs dollar-neutral unit-gross weights ``w = cs_scale(cs_demean(cs_rank(f)))``
    on dates with at least one finite observation, then computes the mean of
    ``0.5 * sum(|w_t - w_{t-1}|)``, bounded in ``[0.0, 1.0]``.
    """
    valid_rows = np.isfinite(factor_df.to_numpy(dtype=float)).any(axis=1)
    if int(valid_rows.sum()) < 2:
        return 0.0

    f_sub = factor_df.loc[valid_rows]
    weights = cs_scale(cs_demean(cs_rank(f_sub)), a=1.0)
    daily_to = 0.5 * weights.diff().abs().sum(axis=1).iloc[1:]
    if daily_to.empty:
        return 0.0
    val = float(daily_to.mean())
    if not np.isfinite(val):
        return 1.0
    return float(np.clip(val, 0.0, 1.0))


def compute_pairwise_abs_rank_corr(
    factor_a: pd.DataFrame,
    factor_b: pd.DataFrame,
    min_assets: int = 3,
) -> float:
    """Compute time-mean cross-sectional ``|Spearman rho(a_t, b_t)|``."""
    rho = fast_rank_ic(factor_a, factor_b, min_assets=min_assets).dropna()
    if rho.empty:
        return 0.0
    val = float(rho.abs().mean())
    return val if np.isfinite(val) else 0.0


def compute_max_library_corr(
    factor_df: pd.DataFrame,
    library: Mapping[str, pd.DataFrame],
    min_assets: int = 3,
) -> float:
    """Maximum time-mean cross-sectional Spearman ``|rho|`` against a factor library."""
    if not library:
        return 0.0
    max_c = 0.0
    for lib_df in library.values():
        c = compute_pairwise_abs_rank_corr(factor_df, lib_df, min_assets=min_assets)
        if c > max_c:
            max_c = c
    return max_c


def check_factor_degenerate(
    factor_df: pd.DataFrame,
    availability_mask: Optional[pd.DataFrame] = None,
    *,
    max_nan_fraction: float = 0.5,
    max_zero_std_fraction: float = 0.5,
) -> Optional[str]:
    """Return a non-empty rejection reason if ``factor_df`` is degenerate, else None."""
    arr = factor_df.to_numpy(dtype=float)
    if arr.size == 0:
        return "empty_panel"

    finite = np.isfinite(arr)
    if availability_mask is not None:
        avail = availability_mask.reindex(
            index=factor_df.index, columns=factor_df.columns
        ).fillna(False).to_numpy(dtype=bool)
        denom = int(avail.sum())
        if denom == 0:
            return "no_available_cells"
        nan_frac = 1.0 - float((finite & avail).sum()) / float(denom)
    else:
        nan_frac = 1.0 - float(finite.sum()) / float(arr.size)

    if nan_frac > max_nan_fraction:
        return f"nan_fraction={nan_frac:.3f}>{max_nan_fraction}"

    finite_vals = arr[finite]
    if finite_vals.size < 2 or float(np.std(finite_vals)) <= 1e-12:
        return "constant_panel"

    # Cross-sectional std == 0 fraction across dates
    n_dates = arr.shape[0]
    row_finite_counts = finite.sum(axis=1)
    zero_std_dates = 0
    for t in range(n_dates):
        if row_finite_counts[t] < 2:
            zero_std_dates += 1
            continue
        row_vals = arr[t, finite[t]]
        if float(np.std(row_vals)) <= 1e-12:
            zero_std_dates += 1

    zero_std_frac = float(zero_std_dates) / float(n_dates)
    if zero_std_frac > max_zero_std_fraction:
        return f"zero_std_fraction={zero_std_frac:.3f}>{max_zero_std_fraction}"

    return None


def evaluate_factor_fitness(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    *,
    node_count: int,
    library: Optional[Mapping[str, pd.DataFrame]] = None,
    availability_mask: Optional[pd.DataFrame] = None,
    lambda_turnover: float = 0.5,
    lambda_corr: float = 1.0,
    lambda_length: float = 0.05,
    max_abs_corr: float = 0.6,
    max_nodes: int = 30,
    min_assets: int = 3,
    max_nan_fraction: float = 0.5,
    max_zero_std_fraction: float = 0.5,
) -> FitnessResult:
    """Evaluate multi-objective GP fitness on discovery dates.

    Formula::

        F = |IC| * sqrt(|IR_daily|)
            - lambda_turnover * TO
            - lambda_corr * max(0, C - max_abs_corr) / (1 - max_abs_corr)
            - lambda_length * (node_count / max_nodes)
    """
    reason = check_factor_degenerate(
        factor_df,
        availability_mask=availability_mask,
        max_nan_fraction=max_nan_fraction,
        max_zero_std_fraction=max_zero_std_fraction,
    )
    if reason is not None:
        return FitnessResult(
            fitness=-np.inf,
            mean_ic=0.0,
            std_ic=0.0,
            daily_ir=0.0,
            sign=1.0,
            turnover=1.0,
            max_corr=0.0,
            node_count=int(node_count),
            n_obs=0,
            rejected=True,
            reject_reason=reason,
        )

    ic_series = fast_rank_ic(factor_df, forward_returns_df, min_assets=min_assets).dropna()
    n_obs = len(ic_series)
    if n_obs < 2:
        return FitnessResult(
            fitness=-np.inf,
            mean_ic=0.0,
            std_ic=0.0,
            daily_ir=0.0,
            sign=1.0,
            turnover=1.0,
            max_corr=0.0,
            node_count=int(node_count),
            n_obs=n_obs,
            rejected=True,
            reject_reason="insufficient_ic_obs",
        )

    mean_ic = float(ic_series.mean())
    std_ic = float(ic_series.std(ddof=1))
    daily_ir = float(mean_ic / std_ic) if std_ic > 1e-12 else 0.0
    sign = 1.0 if mean_ic >= 0.0 else -1.0

    turnover = compute_factor_turnover(factor_df)
    max_corr = compute_max_library_corr(factor_df, library or {}, min_assets=min_assets)

    corr_denom = max(1.0 - float(max_abs_corr), 1e-9)
    corr_pen = max(0.0, max_corr - float(max_abs_corr)) / corr_denom
    len_pen = float(node_count) / float(max(max_nodes, 1))

    fitness = (
        abs(mean_ic) * float(np.sqrt(abs(daily_ir)))
        - float(lambda_turnover) * turnover
        - float(lambda_corr) * corr_pen
        - float(lambda_length) * len_pen
    )
    if not np.isfinite(fitness):
        fitness = -np.inf

    return FitnessResult(
        fitness=float(fitness),
        mean_ic=mean_ic,
        std_ic=std_ic,
        daily_ir=daily_ir,
        sign=sign,
        turnover=turnover,
        max_corr=max_corr,
        node_count=int(node_count),
        n_obs=n_obs,
        rejected=not np.isfinite(fitness),
        reject_reason="" if np.isfinite(fitness) else "non_finite_fitness",
    )


def build_seed_library(ohlcv: Mapping[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Build baseline seed factors for GP novelty/correlation regularization.

    Includes ``momentum_1m_1w`` (``common.daily_factors``) plus two classic
    WorldQuant-style price/volume signals from ``common.alpha101_operators``.
    """
    close = ohlcv["close"].astype(float)
    volume = ohlcv["volume"].astype(float)
    avail = close.notna()

    def _mask(df: pd.DataFrame) -> pd.DataFrame:
        out = df.replace([np.inf, -np.inf], np.nan)
        return out.where(avail)

    prev_c5 = ts_delay(close, 5)
    rev_5d = -ts_delta(close, 5) / prev_c5.where(prev_c5.abs() > 1e-12)
    vp_corr10 = -ts_corr(cs_rank(close), cs_rank(volume), 10)

    return {
        "seed_momentum_1m_1w": _mask(momentum_1m_1w(close)),
        "seed_reversal_5d": _mask(rev_5d),
        "seed_vp_corr_10d": _mask(vp_corr10),
    }


def select_top_k_factors(
    candidates: Sequence[SelectedFactor],
    *,
    top_k: int = 10,
    max_abs_corr: float = 0.6,
    seed_library: Optional[Mapping[str, pd.DataFrame]] = None,
    min_assets: int = 3,
    forward_returns_df: Optional[pd.DataFrame] = None,
) -> List[SelectedFactor]:
    """Greedily select up to ``top_k`` non-redundant factors with ``C <= max_abs_corr``.

    Candidates are ranked by ``fitness`` descending. Each accepted factor must
    have max cross-sectional ``|Spearman rho| <= max_abs_corr`` against all
    already-accepted factors (and ``seed_library`` if supplied). When
    ``forward_returns_df`` is provided, each accepted factor's ``ic_summary``
    is populated via ``common.factor_analyzer.factor_ic_summary``.
    """
    valid = [c for c in candidates if np.isfinite(c.fitness)]
    ranked = sorted(valid, key=lambda c: (-c.fitness, c.expression))

    accepted: List[SelectedFactor] = []
    active_lib: Dict[str, pd.DataFrame] = dict(seed_library) if seed_library else {}

    for cand in ranked:
        if len(accepted) >= top_k:
            break
        c_max = compute_max_library_corr(cand.panel, active_lib, min_assets=min_assets)
        if c_max > max_abs_corr:
            continue
        if forward_returns_df is not None:
            cand.ic_summary = factor_ic_summary(
                cand.panel, forward_returns_df, min_assets=min_assets
            )
        accepted.append(cand)
        active_lib[cand.name] = cand.panel

    return accepted

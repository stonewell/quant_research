"""Vectorized Factor Analysis & Diagnostic Evaluation Engine.

Academic & Quantitative Grounding:
1. Harvey, Campbell R., Yan Liu, and Heqing Zhu. "... and the Cross-Section of
   Expected Returns." The Journal of Finance 71.1 (2016): 5-68.
   Establishes the t-statistic hurdle t >= 3.0 for newly discovered alphas to
   correct for multiple hypothesis testing and data snooping.
2. Kakushadze, Zura. "101 Formulaic Alphas." Wilmott Magazine 2016.84 (2016): 72-81.
   Cross-sectional rank information coefficient (Rank IC) and IC-IR metrics.
3. Gu, Shihao, Bryan Kelly, and Dacheng Xiu. "Empirical Asset Pricing via
   Machine Learning." The Review of Financial Studies 33.5 (2020): 2223-2273.
   Quantile monotonicity and factor predictive decay.

Features:
- Forward returns alignment across arbitrary holding horizons [1, 5, 21].
- Daily cross-sectional Spearman Rank IC tracking.
- Harvey-Liu-Zhu (2016) statistical significance hurdle verification (t >= 3.0 / t >= 2.0).
- Annualized IC-IR (Information Ratio) and studentized p-values.
- Autocorrelation half-life decay estimation (AR(1) half-life).
- Quantile return attribution and long-short monotonicity validation.
"""

from typing import Any, Dict, List, Optional, Union
import numpy as np
import pandas as pd
from scipy import stats


def compute_forward_returns(
    close_df: pd.DataFrame,
    periods: Union[int, List[int]] = (1, 5, 21),
) -> Union[pd.DataFrame, Dict[int, pd.DataFrame]]:
    """Computes forward returns aligned to date t for single or multiple holding periods.

    Forward return for period h is: (Close_{t+h} / Close_t) - 1.0.

    Parameters:
    -----------
    close_df : pd.DataFrame
        Asset close prices with DatetimeIndex and asset symbol columns.
    periods : int or sequence of ints
        Holding horizon in trading bars (e.g., 1, 5, 21).

    Returns:
    --------
    Union[pd.DataFrame, Dict[int, pd.DataFrame]]:
        DataFrame if single period passed; Dict mapping horizon -> DataFrame if sequence.
    """
    if isinstance(periods, (int, np.integer)):
        h = int(periods)
        return (close_df.shift(-h) / close_df - 1.0)

    result = {}
    for h in periods:
        h_int = int(h)
        result[h_int] = (close_df.shift(-h_int) / close_df - 1.0)
    return result


def compute_rank_ic(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    min_assets: int = 3,
) -> pd.Series:
    """Computes daily cross-sectional Spearman Rank Information Coefficient (Rank IC).

    Parameters:
    -----------
    factor_df : pd.DataFrame
        Factor values indexed by DatetimeIndex with asset symbols as columns.
    forward_returns_df : pd.DataFrame
        Forward returns indexed by DatetimeIndex with asset symbols as columns.
    min_assets : int
        Minimum number of valid assets required on date t to compute Rank IC (default: 3).

    Returns:
    --------
    pd.Series:
        Daily Rank IC series indexed by DatetimeIndex.
    """
    common_idx = factor_df.index.intersection(forward_returns_df.index)
    common_cols = factor_df.columns.intersection(forward_returns_df.columns)

    if len(common_idx) == 0 or len(common_cols) < min_assets:
        return pd.Series(dtype=float, index=common_idx)

    f_sub = factor_df.loc[common_idx, common_cols]
    r_sub = forward_returns_df.loc[common_idx, common_cols]

    ic_values = []
    for dt in common_idx:
        f_row = f_sub.loc[dt]
        r_row = r_sub.loc[dt]
        valid_mask = f_row.notna() & r_row.notna() & np.isfinite(f_row) & np.isfinite(r_row)
        n_valid = int(valid_mask.sum())

        if n_valid < min_assets:
            ic_values.append(np.nan)
            continue

        f_valid = f_row[valid_mask]
        r_valid = r_row[valid_mask]

        # Check for non-constant series
        if f_valid.nunique() <= 1 or r_valid.nunique() <= 1:
            ic_values.append(0.0)
            continue

        corr, _ = stats.spearmanr(f_valid, r_valid)
        ic_values.append(float(corr) if np.isfinite(corr) else 0.0)

    return pd.Series(ic_values, index=common_idx, name="rank_ic")


def compute_factor_autocorrelation_half_life(
    factor_df: pd.DataFrame,
    max_lag: int = 20,
    min_assets: int = 3,
) -> float:
    """Estimates factor decay half-life using AR(1) cross-sectional rank autocorrelation.

    Academic grounding:
    Kakushadze (2015), Cartea et al. (2015).
    rho(1) is the 1-day lag cross-sectional rank autocorrelation.
    Half-life tau_{1/2} = -ln(2) / ln(rho(1)).

    Returns:
    --------
    float:
        Estimated half-life in days (clipped to [0.0, 252.0]).
    """
    if len(factor_df) < 2:
        return 0.0

    rank_df = factor_df.rank(axis=1, pct=True)
    rank_shifted = rank_df.shift(1)

    daily_rhos = []
    for i in range(1, len(rank_df)):
        r_t = rank_df.iloc[i]
        r_prev = rank_shifted.iloc[i]
        valid = r_t.notna() & r_prev.notna() & np.isfinite(r_t) & np.isfinite(r_prev)
        if valid.sum() >= min_assets:
            corr, _ = stats.spearmanr(r_t[valid], r_prev[valid])
            if np.isfinite(corr):
                daily_rhos.append(corr)

    if not daily_rhos:
        return 0.0

    mean_rho1 = float(np.mean(daily_rhos))
    if mean_rho1 <= 0.0:
        return 0.0
    if mean_rho1 >= 0.999:
        return 252.0

    # tau_{1/2} = ln(0.5) / ln(rho1)
    half_life = float(np.log(0.5) / np.log(mean_rho1))
    return float(np.clip(half_life, 0.0, 252.0))


def factor_ic_summary(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    min_assets: int = 3,
    min_tstat: float = 2.0,
    hlz_tstat: float = 3.0,
) -> Dict[str, Any]:
    """Generates comprehensive factor performance diagnostics.

    Includes Rank IC, annualized IC-IR, Harvey-Liu-Zhu (2016) t-statistic
    hurdle test (t >= 3.0), Student-t two-tailed p-value, and autocorrelation half-life.

    Parameters:
    -----------
    factor_df : pd.DataFrame
        Factor panel matrix.
    forward_returns_df : pd.DataFrame
        Forward return matrix.
    min_assets : int
        Minimum cross-sectional assets required.
    min_tstat : float
        Robust significance threshold (default: 2.0).
    hlz_tstat : float
        Harvey, Liu & Zhu (2016) discovery threshold (default: 3.0).

    Returns:
    --------
    Dict[str, Any]:
        Dictionary with statistical metrics and decision flags.
    """
    ic_series = compute_rank_ic(factor_df, forward_returns_df, min_assets=min_assets).dropna()
    n_obs = len(ic_series)

    if n_obs < 2:
        return {
            "mean_ic": 0.0,
            "std_ic": 0.0,
            "ic_ir": 0.0,
            "t_stat": 0.0,
            "p_value": 1.0,
            "n_obs": n_obs,
            "is_significant": False,
            "is_statistically_significant": False,
            "half_life": 0.0,
            "ic_series": ic_series,
        }

    mean_ic = float(ic_series.mean())
    std_ic = float(ic_series.std(ddof=1))

    if std_ic > 1e-12:
        ic_ir = float((mean_ic / std_ic) * np.sqrt(252.0))
        t_stat = float(mean_ic / (std_ic / np.sqrt(n_obs)))
        p_val = float(stats.t.sf(abs(t_stat), df=n_obs - 1) * 2.0)
    else:
        ic_ir = 0.0
        t_stat = 0.0
        p_val = 1.0

    is_significant = bool(abs(t_stat) >= min_tstat)
    is_hlz_significant = bool(abs(t_stat) >= hlz_tstat)
    half_life = compute_factor_autocorrelation_half_life(factor_df, min_assets=min_assets)

    return {
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "ic_ir": ic_ir,
        "t_stat": t_stat,
        "p_value": p_val,
        "n_obs": n_obs,
        "is_significant": is_significant,
        "is_statistically_significant": is_hlz_significant,
        "half_life": half_life,
        "ic_series": ic_series,
    }


def compute_quantile_returns(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    n_quantiles: int = 5,
    min_assets: int = 5,
) -> pd.DataFrame:
    """Computes daily equal-weighted returns across N factor quantiles (Q1 to QN).

    Q1 holds assets in the lowest factor quantile; QN holds assets in the highest.
    Also produces 'spread' column (QN - Q1).

    Parameters:
    -----------
    factor_df : pd.DataFrame
        Factor panel matrix.
    forward_returns_df : pd.DataFrame
        Forward return matrix.
    n_quantiles : int
        Number of quantile groups (default: 5).
    min_assets : int
        Minimum number of valid assets needed on a date.

    Returns:
    --------
    pd.DataFrame:
        DataFrame with columns ['Q1', 'Q2', ..., 'QN', 'spread'].
    """
    common_idx = factor_df.index.intersection(forward_returns_df.index)
    common_cols = factor_df.columns.intersection(forward_returns_df.columns)

    cols = [f"Q{i+1}" for i in range(n_quantiles)] + ["spread"]
    if len(common_idx) == 0 or len(common_cols) < max(min_assets, n_quantiles):
        return pd.DataFrame(index=common_idx, columns=cols, dtype=float)

    f_sub = factor_df.loc[common_idx, common_cols]
    r_sub = forward_returns_df.loc[common_idx, common_cols]

    quantile_records = []
    for dt in common_idx:
        f_row = f_sub.loc[dt]
        r_row = r_sub.loc[dt]
        valid_mask = f_row.notna() & r_row.notna() & np.isfinite(f_row) & np.isfinite(r_row)
        n_valid = int(valid_mask.sum())

        if n_valid < max(min_assets, n_quantiles):
            quantile_records.append([np.nan] * len(cols))
            continue

        f_valid = f_row[valid_mask]
        r_valid = r_row[valid_mask]

        # Discrete quantile assignment using integer rank
        ranks = f_valid.rank(method="first")
        # Bin 0 to n_quantiles - 1
        q_bins = pd.Series(
            np.floor((ranks - 1.0) * n_quantiles / n_valid).astype(int),
            index=f_valid.index,
        ).clip(0, n_quantiles - 1)

        row_vals = []
        for q in range(n_quantiles):
            in_q = q_bins == q
            if in_q.sum() > 0:
                row_vals.append(float(r_valid[in_q].mean()))
            else:
                row_vals.append(0.0)

        spread = row_vals[-1] - row_vals[0]
        row_vals.append(spread)
        quantile_records.append(row_vals)

    return pd.DataFrame(quantile_records, index=common_idx, columns=cols, dtype=float)


# =============================================================================
# MS5: Asymmetric Long-Only Factor Evaluation (Short-Sale Constrained)
# =============================================================================

from common.ic_decay_monitor import STATUS_INSUFFICIENT_HISTORY, STATUS_OK
from common.live_capacity import (
    TURNOVER_CEILING_ANN_ONE_WAY,
    TURNOVER_QUALIFIED_ANN_ONE_WAY,
    turnover_label,
)
from common.signal_alerts import (
    ALERT_LEVEL_GREEN,
    ALERT_LEVEL_INSUFFICIENT,
    ALERT_LEVEL_ORANGE,
    ALERT_LEVEL_RED,
    ALERT_LEVEL_YELLOW,
    IC_IR_ORANGE,
    IC_IR_RED,
    IC_IR_YELLOW_FLOOR,
)
from common.signal_snapshot import MIN_CROSS_SECTION_ASSETS

SHORT_LEG_ILLUSION_THRESHOLD: float = 0.30
MONOTONICITY_MIN_CROSS_SECTION: int = 30
ALERT_SHORT_LEG_ILLUSION: str = "SHORT_LEG_ILLUSION"


def _classify_ic_ir_tier(ic_ir: float) -> str:
    """Classify IC-IR using frozen AT5 thresholds from ``common/signal_alerts.py``."""
    if not np.isfinite(ic_ir):
        return ALERT_LEVEL_INSUFFICIENT
    if ic_ir <= IC_IR_RED:
        return ALERT_LEVEL_RED
    if ic_ir <= IC_IR_ORANGE:
        return ALERT_LEVEL_ORANGE
    if ic_ir < IC_IR_YELLOW_FLOOR:
        return ALERT_LEVEL_YELLOW
    return ALERT_LEVEL_GREEN


def asymmetric_long_only_factor_summary(
    factor_df: pd.DataFrame,
    forward_returns_df: pd.DataFrame,
    *,
    n_quantiles: int = 5,
    top_k: Optional[int] = None,
    min_assets: int = MIN_CROSS_SECTION_ASSETS,
    one_way_cost_bps: float = 15.0,
    annualization_days: int = 252,
) -> Dict[str, Any]:
    """Evaluate a cross-sectional factor under A-share / long-only asymmetry (MS5).

    Computes:
    1. ``top_group_excess_return_ann``: annualized equal-weighted excess return of
       the top group (``top_k`` or top quantile) over the cross-sectional mean.
    2. ``long_ic_contribution_ratio`` & ``short_leg_illusion``: additive rank-IC
       decomposition into long half-plane (above-median factor rank) vs short
       half-plane; emits ``SHORT_LEG_ILLUSION`` when overall mean IC > 0 but
       long-side contribution < 30%.
    3. ``quantile_monotonicity_spearman``: report-only Spearman correlation across
       quantile mean returns, with ``monotonicity_gating_allowed = False`` whenever
       median cross-section size < 30.
    4. ``top_k_turnover_adj_sharpe`` & ``turnover_tier``: net-of-cost top-group
       Sharpe using ``one_way_cost_bps`` and frozen ``L2.36`` turnover bands.

    Dates with fewer than ``min_assets`` (default 15, ``MIN_CROSS_SECTION_ASSETS``)
    valid finite assets are excluded (never zero-filled). Fewer than 2 valid dates
    returns ``status = 'INSUFFICIENT_HISTORY'`` with ``NaN`` metrics.
    """
    if n_quantiles < 2:
        raise ValueError(f"n_quantiles must be >= 2, got {n_quantiles!r}")

    common_idx = factor_df.index.intersection(forward_returns_df.index)
    common_cols = factor_df.columns.intersection(forward_returns_df.columns)

    n_below_floor = 0
    valid_n_list: List[int] = []
    ic_list: List[float] = []
    ic_long_list: List[float] = []
    excess_list: List[float] = []
    turnover_list: List[float] = []
    q_returns: List[List[float]] = []

    prev_weights: Optional[Dict[Any, float]] = None

    if len(common_idx) > 0 and len(common_cols) >= min_assets:
        f_sub = factor_df.loc[common_idx, common_cols]
        r_sub = forward_returns_df.loc[common_idx, common_cols]

        for dt in common_idx:
            f_row = f_sub.loc[dt]
            r_row = r_sub.loc[dt]
            valid_mask = (
                f_row.notna()
                & r_row.notna()
                & np.isfinite(f_row)
                & np.isfinite(r_row)
            )
            n_valid = int(valid_mask.sum())
            if n_valid < min_assets or n_valid < n_quantiles:
                n_below_floor += 1
                continue

            f_valid = f_row[valid_mask].astype(float)
            r_valid = r_row[valid_mask].astype(float)
            if f_valid.nunique() <= 1 or r_valid.nunique() <= 1:
                n_below_floor += 1
                continue

            valid_n_list.append(n_valid)

            # Exact additive Rank IC decomposition
            u = f_valid.rank(method="average").to_numpy(dtype=np.float64) - 0.5 * (n_valid + 1.0)
            v = r_valid.rank(method="average").to_numpy(dtype=np.float64) - 0.5 * (n_valid + 1.0)
            denom = float(np.sqrt(np.sum(u * u) * np.sum(v * v)))
            if denom > 1e-12:
                ic_t = float(np.sum(u * v) / denom)
                ic_long_t = float(np.sum(u[u > 0.0] * v[u > 0.0]) / denom)
            else:
                ic_t = 0.0
                ic_long_t = 0.0
            ic_list.append(ic_t)
            ic_long_list.append(ic_long_t)

            # Quantile returns
            ranks_first = f_valid.rank(method="first")
            q_bins = pd.Series(
                np.floor((ranks_first - 1.0) * n_quantiles / n_valid).astype(int),
                index=f_valid.index,
            ).clip(0, n_quantiles - 1)
            q_row = [float(r_valid[q_bins == q].mean()) for q in range(n_quantiles)]
            q_returns.append(q_row)

            # Top group selection
            if top_k is not None and 1 <= int(top_k) < n_valid:
                k_sel = int(top_k)
                top_syms = list(f_valid.nlargest(k_sel).index)
            else:
                top_syms = list(f_valid.index[q_bins == (n_quantiles - 1)])

            top_ret = float(r_valid.loc[top_syms].mean())
            mkt_ret = float(r_valid.mean())
            excess_list.append(top_ret - mkt_ret)

            # One-way turnover of equal-weighted top group
            w_val = 1.0 / float(len(top_syms))
            curr_weights = {s: w_val for s in top_syms}
            if prev_weights is None:
                turnover_list.append(0.0)
            else:
                all_syms = set(prev_weights.keys()) | set(curr_weights.keys())
                l1 = sum(
                    abs(curr_weights.get(s, 0.0) - prev_weights.get(s, 0.0))
                    for s in all_syms
                )
                turnover_list.append(0.5 * l1)
            prev_weights = curr_weights
    else:
        n_below_floor = len(common_idx)

    n_obs = len(ic_list)
    if n_obs < 2:
        return {
            "status": STATUS_INSUFFICIENT_HISTORY,
            "n_obs": n_obs,
            "n_dates_below_cross_section_floor": int(n_below_floor),
            "median_cross_section_size": float("nan"),
            "mean_ic": float("nan"),
            "mean_ic_long": float("nan"),
            "ic_ir": float("nan"),
            "ic_ir_tier": ALERT_LEVEL_INSUFFICIENT,
            "top_group_excess_return_ann": float("nan"),
            "long_ic_contribution_ratio": float("nan"),
            "short_leg_illusion": False,
            "alerts": [],
            "quantile_monotonicity_spearman": float("nan"),
            "monotonicity_is_report_only": True,
            "monotonicity_gating_allowed": False,
            "annualized_one_way_turnover": float("nan"),
            "turnover_tier": "INSUFFICIENT_HISTORY",
            "turnover_qualified_threshold": TURNOVER_QUALIFIED_ANN_ONE_WAY,
            "turnover_ceiling_threshold": TURNOVER_CEILING_ANN_ONE_WAY,
            "top_k_turnover_adj_sharpe": float("nan"),
        }

    median_n = float(np.median(valid_n_list))
    ic_arr = np.asarray(ic_list, dtype=np.float64)
    ic_long_arr = np.asarray(ic_long_list, dtype=np.float64)
    excess_arr = np.asarray(excess_list, dtype=np.float64)
    turn_arr = np.asarray(turnover_list, dtype=np.float64)

    mean_ic = float(np.mean(ic_arr))
    std_ic = float(np.std(ic_arr, ddof=1))
    ic_ir = float((mean_ic / std_ic) * np.sqrt(annualization_days)) if std_ic > 1e-12 else 0.0
    mean_ic_long = float(np.mean(ic_long_arr))

    if mean_ic > 1e-12:
        long_ic_ratio = float(np.clip(mean_ic_long / mean_ic, -1.0, 2.0))
    else:
        long_ic_ratio = 0.0 if mean_ic_long <= 0.0 else float("nan")

    alerts: List[str] = []
    short_leg_illusion = bool(
        mean_ic > 0.0
        and np.isfinite(long_ic_ratio)
        and long_ic_ratio < SHORT_LEG_ILLUSION_THRESHOLD
    )
    if short_leg_illusion:
        alerts.append(ALERT_SHORT_LEG_ILLUSION)

    # Quantile monotonicity (report-only; gating disallowed when median N < 30)
    q_mean = np.nanmean(np.asarray(q_returns, dtype=np.float64), axis=0)
    mono_corr, _ = stats.spearmanr(np.arange(1, n_quantiles + 1), q_mean)
    quantile_mono = float(mono_corr) if np.isfinite(mono_corr) else float("nan")

    # Top-group annualized excess return & net-of-cost Sharpe
    top_excess_ann = float(np.mean(excess_arr) * annualization_days)
    ann_one_way_turn = float(np.mean(turn_arr) * annualization_days)
    cost_rate = float(one_way_cost_bps) * 1e-4
    net_ret = excess_arr - 2.0 * turn_arr * cost_rate
    net_std = float(np.std(net_ret, ddof=1))
    net_sharpe = (
        float((np.mean(net_ret) / net_std) * np.sqrt(annualization_days))
        if net_std > 1e-12
        else 0.0
    )

    return {
        "status": STATUS_OK,
        "n_obs": n_obs,
        "n_dates_below_cross_section_floor": int(n_below_floor),
        "median_cross_section_size": median_n,
        "mean_ic": mean_ic,
        "mean_ic_long": mean_ic_long,
        "ic_ir": ic_ir,
        "ic_ir_tier": _classify_ic_ir_tier(ic_ir),
        "top_group_excess_return_ann": top_excess_ann,
        "long_ic_contribution_ratio": long_ic_ratio,
        "short_leg_illusion": short_leg_illusion,
        "alerts": alerts,
        "quantile_monotonicity_spearman": quantile_mono,
        "monotonicity_is_report_only": True,
        "monotonicity_gating_allowed": bool(median_n >= MONOTONICITY_MIN_CROSS_SECTION),
        "annualized_one_way_turnover": ann_one_way_turn,
        "turnover_tier": turnover_label(ann_one_way_turn),
        "turnover_qualified_threshold": TURNOVER_QUALIFIED_ANN_ONE_WAY,
        "turnover_ceiling_threshold": TURNOVER_CEILING_ANN_ONE_WAY,
        "top_k_turnover_adj_sharpe": net_sharpe,
    }

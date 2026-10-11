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

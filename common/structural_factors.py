"""Structural, Wyckoff, Fractal, and Volume Profile Quantitative Factor Library.

Shared bottom-layer quantitative factor and micro-structural operator library
providing strictly causal, mathematically rigorous primitives for:
1. Volume Profile (Point of Control, Value Area High/Low) calculation
2. Intraday and multi-frequency microstructure profile aggregation
3. Wyckoff structure and Chan pivot/fractal consolidation metrics
4. Scale-free turning point curvature and standardized dynamical divergence
5. Mandelbrot fractal dimension and consolidation volume density
6. CYQ turnover-decay chip distribution engine (winner ratio & cost concentration)

Architecture & Decoupling (Defect M5 resolution):
This module resides in `common/` and is strictly independent of `pipeline/`.
It MUST NEVER import from `pipeline.*` to ensure full environment portability
across both pipeline and ML execution environments and to prevent architectural
inversion and circular dependency bugs.
"""

from typing import Optional, Tuple
import numpy as np
import pandas as pd

from common.hurst import hurst_exponent
from common.indicators import atr, sma

__all__ = [
    "compute_rolling_poc_series",
    "wyckoff_spring",
    "extremum_curvature",
    "standardized_divergence",
    "wyckoff_effort_result",
    "fractal_dimension",
    "consolidation_density",
    "cyq_chip_distribution",
    "cyq_winner_ratio",
    "cyq_cost_concentration",
]


def compute_rolling_poc_series(
    bars: pd.DataFrame,
    lookback: int = 60,
    n_bins: int = 30,
    value_area_pct: float = 0.70,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Computes rolling Point of Control (POC), Value Area Low (VAL), and Value Area High (VAH).

    Parameters
    ----------
    bars : pd.DataFrame
        DataFrame with columns 'High', 'Low', 'Close', and optional 'Volume'.
    lookback : int, default 60
        Rolling window lookback length in bars.
    n_bins : int, default 30
        Number of price bins for the Volume Profile histogram.
    value_area_pct : float, default 0.70
        Fraction of volume included in the Value Area around POC.

    Returns
    -------
    Tuple[np.ndarray, np.ndarray, np.ndarray]
        Arrays of shape (n_bars,) for (poc, val, vah). The first lookback - 1 entries are NaN.
    """
    n_bars = len(bars)
    poc = np.full(n_bars, np.nan)
    val = np.full(n_bars, np.nan)
    vah = np.full(n_bars, np.nan)

    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    has_vol = "Volume" in bars.columns and not bars["Volume"].isna().all()
    vol = bars["Volume"].fillna(1.0).to_numpy(dtype=float) if has_vol else np.ones(n_bars)

    for t in range(lookback - 1, n_bars):
        w_low = low[t - lookback + 1 : t + 1]
        w_high = high[t - lookback + 1 : t + 1]
        w_vol = vol[t - lookback + 1 : t + 1]

        p_min = float(np.min(w_low))
        p_max = float(np.max(w_high))

        if p_max <= p_min or np.isnan(p_min) or np.isnan(p_max):
            poc[t] = close[t]
            val[t] = close[t] * 0.95
            vah[t] = close[t] * 1.05
            continue

        bin_width = (p_max - p_min) / n_bins
        bin_edges = np.linspace(p_min, p_max, n_bins + 1)
        bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0
        bin_vols = np.zeros(n_bins, dtype=float)

        for i in range(lookback):
            b_l = w_low[i]
            b_h = w_high[i]
            b_v = w_vol[i]
            # Distribute volume across overlapped price bins
            idx_start = max(0, min(n_bins - 1, int((b_l - p_min) / bin_width)))
            idx_end = max(0, min(n_bins - 1, int((b_h - p_min) / bin_width)))
            count = max(1, idx_end - idx_start + 1)
            v_per_bin = b_v / count
            bin_vols[idx_start : idx_end + 1] += v_per_bin

        max_bin_idx = int(np.argmax(bin_vols))
        poc[t] = bin_mids[max_bin_idx]

        # Calculate Value Area around POC
        total_vol = np.sum(bin_vols)
        target_va_vol = total_vol * value_area_pct
        accum_vol = bin_vols[max_bin_idx]
        va_low_idx = max_bin_idx
        va_high_idx = max_bin_idx

        while accum_vol < target_va_vol and (va_low_idx > 0 or va_high_idx < n_bins - 1):
            next_low_vol = bin_vols[va_low_idx - 1] if va_low_idx > 0 else 0.0
            next_high_vol = bin_vols[va_high_idx + 1] if va_high_idx < n_bins - 1 else 0.0
            if next_low_vol >= next_high_vol and va_low_idx > 0:
                va_low_idx -= 1
                accum_vol += next_low_vol
            elif va_high_idx < n_bins - 1:
                va_high_idx += 1
                accum_vol += next_high_vol
            else:
                break

        val[t] = bin_edges[va_low_idx]
        vah[t] = bin_edges[va_high_idx + 1]

    return poc, val, vah


def wyckoff_spring(
    bars: pd.DataFrame,
    support_series: pd.Series,
    max_penetration: float = 0.04,
    volume_lookback: int = 20,
    min_penetration: float = 0.0,
) -> pd.Series:
    """Detects causal Wyckoff Spring (support undercut followed by reclaim).

    Academic Grounding: Wyckoff (1931), Defect M4 resolution.
    Enforces causal support S_{t-1} and maximum penetration threshold delta_max (default 4%)
    to prevent lookahead bias and catastrophic breakdown trap false triggers.

    Parameters
    ----------
    bars : pd.DataFrame
        DataFrame with columns 'Low', 'Close', and optional 'Volume'.
    support_series : pd.Series
        Causal support price series (e.g. Zhongshu lower bound ZD_{t-1} or prior swing low).
    max_penetration : float, default 0.04
        Maximum allowed penetration depth below support (default 4%).
    volume_lookback : int, default 20
        Lookback window for relative volume moving average.
    min_penetration : float, default 0.0
        Minimum required penetration depth below support (default 0.0).

    Returns
    -------
    pd.Series
        Series named 'wyckoff_spring' holding relative volume at confirmed spring bars,
        and 0.0 elsewhere.
    """
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    volume = (
        bars["Volume"].astype(float).fillna(1.0)
        if ("Volume" in bars.columns and not bars["Volume"].isna().all())
        else pd.Series(1.0, index=bars.index)
    )

    vol_ma = volume.rolling(volume_lookback, min_periods=1).mean()
    rel_vol = volume / (vol_ma + 1e-8)

    s_prev = support_series.shift(1).fillna(support_series).astype(float)
    condition = (
        (low.shift(1) >= s_prev * (1.0 - max_penetration))
        & (low.shift(1) < s_prev * (1.0 - min_penetration))
        & (close > s_prev)
        & (~s_prev.isna())
        & (s_prev > 0.0)
    )

    out = pd.Series(0.0, index=bars.index, dtype=float, name="wyckoff_spring")
    out = out.where(~condition, rel_vol)
    return out


def extremum_curvature(
    price: pd.Series,
    window: int = 5,
    min_periods: int = 3,
) -> pd.Series:
    """Computes scale-free turning point curvature via quadratic fitting on z-scored prices.

    Academic Grounding: Tianfeng Securities Financial Engineering (2019), Defect M1 resolution.
    Normalizes local window prices via dimensionless z-scores before quadratic fitting:
        y(tau) = a * tau^2 + b * tau + c over symmetric centered tau in [-(k-1)/2, (k-1)/2]
        kappa = 2 * a / (1 + b^2)^(3/2)
    Scale invariant: multiplying price by any positive constant produces identical curvature.

    Parameters
    ----------
    price : pd.Series
        Price series (e.g. Close).
    window : int, default 5
        Rolling window length for local curvature estimation.
    min_periods : int, default 3
        Minimum number of valid price points required to fit quadratic curve.

    Returns
    -------
    pd.Series
        Series named 'extremum_curvature' with dimensionless curvature values.
    """
    n = len(price)
    out = np.full(n, np.nan, dtype=float)
    p_vals = price.to_numpy(dtype=float)

    for i in range(min_periods - 1, n):
        start_idx = max(0, i - window + 1)
        slice_p = p_vals[start_idx : i + 1]
        valid_p = slice_p[~np.isnan(slice_p)]
        k = len(valid_p)
        if k < min_periods:
            continue
        std_p = float(np.std(valid_p))
        if std_p < 1e-8:
            out[i] = 0.0
            continue
        y = (valid_p - np.mean(valid_p)) / (std_p + 1e-8)
        tau = np.linspace(-(k - 1) / 2.0, (k - 1) / 2.0, k)
        p = np.polyfit(tau, y, 2)
        a, b = float(p[0]), float(p[1])
        kappa = (2.0 * a) / ((1.0 + b ** 2) ** 1.5)
        out[i] = kappa

    return pd.Series(out, index=price.index, name="extremum_curvature")


def standardized_divergence(
    price: pd.Series,
    indicator: pd.Series,
    lag: int = 5,
    vol_lookback: int = 20,
) -> pd.Series:
    """Computes dimensionless z-score normalized divergence between price return and indicator momentum.

    Academic Grounding: Standardized Dynamical Divergence Modeling, Defect M2 resolution.
    Normalizes both price return and indicator momentum by rolling historical volatility:
        ret_k = price.pct_change(lag)
        delta_i = indicator.diff(lag)
        ret_z = ret_k / (std(ret_k, vol_lookback) + eps)
        ind_z = delta_i / (std(delta_i, vol_lookback) + eps)
        div = (ind_z - ret_z) / sqrt(2)

    HONEST CAVEAT (M2):
    (ind_z - ret_z) / sqrt(2) is adopted so that bullish bottom divergence (price makes lower
    lows ret_z < 0 while indicator momentum rises ind_z > ret_z) yields positive divergence (> 1.2),
    and bearish top divergence (price makes higher highs ret_z > 0 while momentum lags ind_z < ret_z)
    yields negative divergence (< -1.0), matching the factor taxonomy table and downstream strategy thresholds.

    Parameters
    ----------
    price : pd.Series
        Price series.
    indicator : pd.Series
        Momentum indicator series (e.g. MACD, RSI, or volume momentum).
    lag : int, default 5
        Return and difference lag.
    vol_lookback : int, default 20
        Rolling window length for standard deviation normalization.

    Returns
    -------
    pd.Series
        Series named 'standardized_divergence'.
    """
    ret_k = price.pct_change(lag)
    delta_i = indicator.diff(lag)
    min_p = max(3, vol_lookback // 2)
    ret_std = ret_k.rolling(vol_lookback, min_periods=min_p).std()
    ind_std = delta_i.rolling(vol_lookback, min_periods=min_p).std()
    ret_z = ret_k / (ret_std + 1e-8)
    ind_z = delta_i / (ind_std + 1e-8)
    div = (ind_z - ret_z) / np.sqrt(2.0)
    return div.rename("standardized_divergence")


def wyckoff_effort_result(
    bars: pd.DataFrame,
    lag: int = 5,
    volume_lookback: int = 20,
    signed: bool = False,
) -> pd.Series:
    """Computes price advancement achieved per unit of trading effort.

    Academic Grounding: Wyckoff Law of Effort vs Result (1931), Defect M3 resolution.
    Defines Effort as strictly positive relative volume ratio:
        Effort_t = V_t / (SMA(V, volume_lookback) + eps)
    and Result as ATR-normalized displacement:
        Result_t = |C_t - C_{t-lag}| / (ATR_lag(t) + eps)
        EffortResult_t = Result_t / (Effort_t + eps)
    Eliminates zero division explosions and sign confusion from negative volume changes.

    Parameters
    ----------
    bars : pd.DataFrame
        DataFrame with columns 'High', 'Low', 'Close', and optional 'Volume'.
    lag : int, default 5
        Advancement lookback window.
    volume_lookback : int, default 20
        Lookback for moving average volume.
    signed : bool, default False
        If True, multiplies result by sign(C_t - C_{t-lag}).

    Returns
    -------
    pd.Series
        Series named 'wyckoff_effort_result'.
    """
    volume = (
        bars["Volume"].astype(float).fillna(1.0)
        if ("Volume" in bars.columns and not bars["Volume"].isna().all())
        else pd.Series(1.0, index=bars.index)
    )
    close = bars["Close"].astype(float)
    vol_ma = volume.rolling(volume_lookback, min_periods=1).mean()
    effort = volume / (vol_ma + 1e-8)
    atr_series = atr(bars, period=lag)
    disp = (close - close.shift(lag)).abs()
    result = disp / (atr_series + 1e-8)
    er = result / (effort + 1e-8)

    if signed:
        er = er * np.sign(close - close.shift(lag))

    return er.rename("wyckoff_effort_result")


def fractal_dimension(
    price: pd.Series,
    window: int = 60,
    min_chunk_size: int = 8,
) -> pd.Series:
    """Computes rolling Mandelbrot fractal dimension D = 2 - H(P) from log returns via R/S Hurst estimator.

    Academic Grounding: Mandelbrot (1982), Hurst (1951).
    D < 1.45: Persistent directional expansion (trend regime).
    D > 1.55: Anti-persistent noisy chop (mean-reverting / oscillation regime).

    Parameters
    ----------
    price : pd.Series
        Price series.
    window : int, default 60
        Rolling window length in bars.
    min_chunk_size : int, default 8
        Minimum sub-period chunk size passed to classical R/S Hurst estimator.

    Returns
    -------
    pd.Series
        Series named 'fractal_dimension' clipped to [1.0, 2.0].
    """
    n = len(price)
    out = np.full(n, np.nan, dtype=float)
    log_ret = np.log(price.astype(float) / price.astype(float).shift(1))

    min_req_samples = min_chunk_size * 4
    for t in range(n):
        if t < min_req_samples:
            continue
        start_idx = max(0, t - window + 1)
        w_ret = log_ret.iloc[start_idx : t + 1].dropna()
        if len(w_ret) < min_req_samples:
            continue
        h = hurst_exponent(w_ret, min_chunk_size=min_chunk_size)
        if not np.isnan(h):
            out[t] = float(np.clip(2.0 - h, 1.0, 2.0))

    return pd.Series(out, index=price.index, name="fractal_dimension")


def consolidation_density(
    bars: pd.DataFrame,
    zd: pd.Series,
    zg: pd.Series,
    window: int = 20,
) -> pd.Series:
    """Measures volume density overlapping confirmed Chan pivot range [ZD, ZG].

    Academic Grounding: Chan (2006) pivot volume consolidation, Defect M5 resolution.
    Computes linear price overlap ratio between bar range [L_t, H_t] and pivot [ZD_t, ZG_t]:
        OverlapRatio_t = max(0, min(H_t, ZG_t) - max(L_t, ZD_t)) / (H_t - L_t + eps)
    and sums volume-weighted overlap over rolling window.

    Parameters
    ----------
    bars : pd.DataFrame
        DataFrame with columns 'High', 'Low', 'Close', and optional 'Volume'.
    zd : pd.Series
        Zhongshu lower bound series.
    zg : pd.Series
        Zhongshu upper bound series.
    window : int, default 20
        Rolling window length for volume density aggregation.

    Returns
    -------
    pd.Series
        Series named 'consolidation_density' in [0.0, 1.0].
    """
    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    volume = (
        bars["Volume"].astype(float).fillna(1.0)
        if ("Volume" in bars.columns and not bars["Volume"].isna().all())
        else pd.Series(1.0, index=bars.index)
    )

    zd_val = zd.to_numpy(dtype=float)
    zg_val = zg.to_numpy(dtype=float)
    high_val = high.to_numpy(dtype=float)
    low_val = low.to_numpy(dtype=float)
    close_val = close.to_numpy(dtype=float)
    vol_val = volume.to_numpy(dtype=float)

    n = len(bars)
    overlap_ratio = np.zeros(n, dtype=float)

    valid_z = (~np.isnan(zd_val)) & (~np.isnan(zg_val)) & (zg_val > zd_val)

    flat_bar = valid_z & (high_val <= low_val)
    overlap_ratio[flat_bar] = np.where(
        (close_val[flat_bar] >= zd_val[flat_bar]) & (close_val[flat_bar] <= zg_val[flat_bar]),
        1.0,
        0.0,
    )

    normal_bar = valid_z & (high_val > low_val)
    inter_len = np.maximum(
        0.0,
        np.minimum(high_val[normal_bar], zg_val[normal_bar])
        - np.maximum(low_val[normal_bar], zd_val[normal_bar]),
    )
    bar_len = high_val[normal_bar] - low_val[normal_bar] + 1e-8
    overlap_ratio[normal_bar] = np.clip(inter_len / bar_len, 0.0, 1.0)

    overlapped_vol = pd.Series(overlap_ratio * vol_val, index=bars.index)
    total_vol = pd.Series(vol_val, index=bars.index)
    density = overlapped_vol.rolling(window, min_periods=1).sum() / (
        total_vol.rolling(window, min_periods=1).sum() + 1e-8
    )
    return density.clip(0.0, 1.0).rename("consolidation_density")


def cyq_chip_distribution(
    bars: pd.DataFrame,
    n_bins: int = 100,
    decay_mode: str = "turnover",
    kernel: str = "triangular",
    intraday_5m_bars: Optional[pd.DataFrame] = None,
) -> Tuple[pd.Series, pd.Series]:
    """Computes recursive turnover-decay chip distribution and derived CYQ factors.

    Academic Grounding: Chen Hao (CYQ Chip Distribution Theory 1999), Task C1.3.
    Operates on a strictly causal dynamic price grid [p_min, p_max] expanding monotonically
    with historical price observations and projecting historical chips via mass-conserving
    CDF interpolation:
        chips_t = chips_{t-1} * (1 - tau_t) + tau_t * K_t(p)
    where sum(chips_t) == 1.0 at all bars.

    DISCLOSED APPROXIMATION:
    When exchange turnover column ('Turn', 'Turnover') is unavailable, effective turnover tau_t
    is approximated using relative volume proxy tau_hat_t = clip(0.02 * V_t / SMA_60(V)_t, 0.002, 0.50).

    Parameters
    ----------
    bars : pd.DataFrame
        Daily DataFrame with columns 'High', 'Low', 'Close', and optional 'Volume', 'Turn'/'Turnover', 'VWAP'.
    n_bins : int, default 100
        Number of price bins for the chip histogram.
    decay_mode : str, default 'turnover'
        Decay mode ('turnover' for turnover-based decay).
    kernel : str, default 'triangular'
        New chip injection kernel: 'triangular' (peaked at VWAP / typical price),
        'uniform', or 'intraday_5m' (using 5m bars if provided).
    intraday_5m_bars : Optional[pd.DataFrame], default None
        Optional intraday 5-minute bars DataFrame with columns 'High', 'Low', 'Close', 'Volume'.

    Returns
    -------
    Tuple[pd.Series, pd.Series]
        (cyq_winner_ratio, cyq_cost_concentration)
    """
    n_bars = len(bars)
    if n_bars == 0:
        return (
            pd.Series(dtype=float, name="cyq_winner_ratio"),
            pd.Series(dtype=float, name="cyq_cost_concentration"),
        )

    # 1. Effective turnover series tau_t
    has_turn = False
    if decay_mode == "turnover":
        for col in ["Turn", "turn", "Turnover", "turnover"]:
            if col in bars.columns and not bars[col].isna().all():
                col_s = bars[col].astype(float)
                med = col_s.dropna().median() if not col_s.dropna().empty else 0.0
                tau_series = col_s / 100.0 if med > 1.0 else col_s
                tau_series = tau_series.fillna(0.02).clip(lower=0.001, upper=0.99)
                has_turn = True
                break

    if not has_turn:
        vol = (
            bars["Volume"].fillna(1.0).astype(float)
            if ("Volume" in bars.columns and not bars["Volume"].isna().all())
            else pd.Series(1.0, index=bars.index)
        )
        sma_60 = vol.rolling(60, min_periods=1).mean()
        rel_vol = vol / (sma_60 + 1e-8)
        tau_series = (0.02 * rel_vol).clip(lower=0.002, upper=0.50)

    lows = bars["Low"].to_numpy(dtype=float)
    highs = bars["High"].to_numpy(dtype=float)
    closes = bars["Close"].to_numpy(dtype=float)

    # 2. Initialize strictly causal dynamic price grid [p_min, p_max]
    l_0 = lows[0]
    h_0 = highs[0]
    if h_0 <= l_0:
        h_0 = l_0 * 1.01 + 0.01
        l_0 = l_0 * 0.99 - 0.01

    p_min = l_0 * 0.95
    p_max = h_0 * 1.05
    if p_max <= p_min:
        p_max = p_min + 1.0

    bin_edges = np.linspace(p_min, p_max, n_bins + 1)
    chips = np.zeros(n_bins, dtype=float)

    winner_ratio = np.zeros(n_bars, dtype=float)
    cost_concentration = np.zeros(n_bars, dtype=float)

    for t in range(n_bars):
        l_t = lows[t]
        h_t = highs[t]
        c_t = closes[t]

        if h_t <= l_t:
            h_t = l_t * 1.001 + 0.001
            l_t = l_t * 0.999 - 0.001

        cand_min = l_t * 0.95
        cand_max = h_t * 1.05

        # Causal grid expansion
        if cand_min < p_min or cand_max > p_max:
            new_p_min = min(p_min, cand_min)
            new_p_max = max(p_max, cand_max)
            new_bin_edges = np.linspace(new_p_min, new_p_max, n_bins + 1)
            old_cdf = np.concatenate([[0.0], np.cumsum(chips)])
            new_cdf = np.interp(new_bin_edges, bin_edges, old_cdf, left=0.0, right=1.0)
            chips = np.diff(new_cdf)
            chips = np.maximum(0.0, chips)
            sum_c = np.sum(chips)
            if sum_c > 1e-12:
                chips /= sum_c
            else:
                chips = np.full(n_bins, 1.0 / n_bins)
            p_min, p_max = new_p_min, new_p_max
            bin_edges = new_bin_edges

        bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0
        k_t = np.zeros(n_bins, dtype=float)

        used_5m = False
        if (intraday_5m_bars is not None or kernel == "intraday_5m") and intraday_5m_bars is not None and len(intraday_5m_bars) > 0:
            day_5m = None
            if isinstance(intraday_5m_bars.index, pd.DatetimeIndex) and hasattr(bars.index[t], "date"):
                day_5m = intraday_5m_bars[intraday_5m_bars.index.date == bars.index[t].date()]
            elif hasattr(bars.index[t], "strftime"):
                cur_str = bars.index[t].strftime("%Y-%m-%d")
                for c in ["date", "Date", "datetime", "Datetime"]:
                    if c in intraday_5m_bars.columns:
                        day_5m = intraday_5m_bars[intraday_5m_bars[c].astype(str).str.startswith(cur_str)]
                        break
            if day_5m is not None and not day_5m.empty:
                for _, r in day_5m.iterrows():
                    ml, mh = float(r["Low"]), float(r["High"])
                    mv = float(r["Volume"]) if ("Volume" in r and not np.isnan(r["Volume"])) else 1.0
                    bl = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, ml, side="right") - 1)))
                    bh = max(0, min(n_bins - 1, int(np.searchsorted(bin_edges, mh, side="left"))))
                    if bh < bl:
                        bh = bl
                    k_t[bl : bh + 1] += mv / (bh - bl + 1)
                sum_k = np.sum(k_t)
                if sum_k > 1e-12:
                    k_t /= sum_k
                    used_5m = True

        if not used_5m:
            i_start = int(np.searchsorted(bin_edges, l_t, side="right") - 1)
            i_end = int(np.searchsorted(bin_edges, h_t, side="left"))
            i_start = max(0, min(n_bins - 1, i_start))
            i_end = max(0, min(n_bins - 1, i_end))
            if i_end < i_start:
                i_end = i_start

            if kernel == "uniform" or i_start == i_end:
                k_t[i_start : i_end + 1] = 1.0 / (i_end - i_start + 1)
            else:  # triangular
                vwap_val = (
                    bars["VWAP"].iloc[t]
                    if ("VWAP" in bars.columns and not np.isnan(bars["VWAP"].iloc[t]))
                    else (h_t + l_t + c_t) / 3.0
                )
                p_peak = float(np.clip(vwap_val, l_t, h_t))
                w = np.zeros(i_end - i_start + 1, dtype=float)
                for idx, j in enumerate(range(i_start, i_end + 1)):
                    m = bin_mids[j]
                    if m <= p_peak:
                        w[idx] = max(0.0, 1.0 - (p_peak - m) / (p_peak - l_t + 1e-8)) if p_peak > l_t else 1.0
                    else:
                        w[idx] = max(0.0, 1.0 - (m - p_peak) / (h_t - p_peak + 1e-8)) if h_t > p_peak else 1.0
                sum_w = np.sum(w)
                if sum_w > 1e-12:
                    k_t[i_start : i_end + 1] = w / sum_w
                else:
                    k_t[i_start : i_end + 1] = 1.0 / (i_end - i_start + 1)

        # 3. Recursive update
        if t == 0:
            chips = k_t.copy()
        else:
            tau_t = float(tau_series.iloc[t])
            chips = chips * (1.0 - tau_t) + tau_t * k_t
            chips /= np.sum(chips)

        # 4. Export winner ratio & cost concentration
        winner_ratio[t] = float(np.clip(np.sum(chips[bin_mids <= c_t]), 0.0, 1.0))

        cdf = np.concatenate([[0.0], np.cumsum(chips)])
        cdf[-1] = 1.0
        p_5 = float(np.interp(0.05, cdf, bin_edges))
        p_95 = float(np.interp(0.95, cdf, bin_edges))
        cost_concentration[t] = float(np.clip(1.0 - (p_95 - p_5) / (p_95 + p_5 + 1e-8), 0.0, 1.0))

    return (
        pd.Series(winner_ratio, index=bars.index, name="cyq_winner_ratio"),
        pd.Series(cost_concentration, index=bars.index, name="cyq_cost_concentration"),
    )


def cyq_winner_ratio(bars: pd.DataFrame, **kwargs) -> pd.Series:
    """Convenience wrapper returning cyq_winner_ratio Series."""
    wr, _ = cyq_chip_distribution(bars, **kwargs)
    return wr


def cyq_cost_concentration(bars: pd.DataFrame, **kwargs) -> pd.Series:
    """Convenience wrapper returning cyq_cost_concentration Series."""
    _, cc = cyq_chip_distribution(bars, **kwargs)
    return cc

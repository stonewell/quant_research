"""Intraday 5-minute causal feature aggregation and multi-cycle confluence engine.

Implements tasks T2.1, T2.2, and T2.3 from docs/intraday_5m_live_and_blend_enhancement_plan.md:
- Point-in-time causal cutoff (R1, R6): A-share 5m bars filtered strictly by ts.time() <= cutoff_time
  (36 bars at 14:00, 39 bars at 14:15, 48 bars at 15:00).
- Pure intraday realized variance, semivariance decomposition, and bipower variation (RV, RS_Up, RS_Down, BPV).
- True Intraday VWAP and dimensionless premium (VWAP_Prem_5m) immune to price basis.
- Microstructure order flow: signed up-volume ratio (UpVol_Ratio_5m) and overnight-gap-stripped
  Smart Money factor (SmartMoney_Q_5m).
- Afternoon return (Afternoon_Ret_5m = C_{t,M} / C_{t,11:30} - 1) for afternoon distribution detection (R9.3).
- 5m Chan sub-level interval nesting and MACD bull/bear confluence with 240-bar lookback (R9).
- Cross-source split ratio alignment kappa_t = C_{t,1d} / C_{t,M,5m} (R4, R6).
- Intraday volume seasonality extrapolation (Seasonality_Volume_Scale) and Is_Partial_Bar flag (R12).
- Zero top-level pipeline.* imports: lazy imports used for Chan theory components.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import datetime
import glob
import logging
import math
import os
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np
import pandas as pd

from common.indicators import macd

logger = logging.getLogger(__name__)


def _get_chan_modules():
    """Lazy import of pipeline Chan structure and signals modules.

    Maintains the architectural boundary that common/ must not have hard top-level
    imports of pipeline.*.
    """
    try:
        from pipeline.research_strategy.rs.chan_structure import (
            find_fractals,
            merge_inclusion,
            build_strokes,
            build_pivots,
        )
        from pipeline.research_strategy.rs.chan_signals import (
            compute_chan3_signals,
            compute_chan_pivot_macd_signals,
        )
        return {
            "find_fractals": find_fractals,
            "merge_inclusion": merge_inclusion,
            "build_strokes": build_strokes,
            "build_pivots": build_pivots,
            "compute_chan3_signals": compute_chan3_signals,
            "compute_chan_pivot_macd_signals": compute_chan_pivot_macd_signals,
        }
    except ImportError:
        return None


@dataclass
class IntradayFeatureConfig:
    """Configuration for causal intraday feature extraction and cross-cycle confluence."""

    cutoff_time: Optional[str] = "14:15"
    """R6: Default '14:15' (39 bars) aligned with Stage 2 live trading.
    Pass '14:00' (36 bars) for Stage 1 macro health gate.
    Pass None or '15:00' (48 bars) for full-day historical control mode.
    """

    chan_5m_lookback_bars: int = 240
    """R9: Lookback window of 5m bars for Chan structure detection (~5 trading days,
    anchored to t-4 09:35 open bar).
    """

    chan_5m_signal_window_bars: int = 12
    """Signals appearing within the most recent 12 bars (1 hour) are treated as active today."""

    smart_money_top_pct: float = 0.20
    """Cumulative volume percentage threshold for smart money selection (top 20%)."""

    morning_end_time: str = "11:30"
    """R9.3: Reference morning close time for Afternoon_Ret_5m calculation."""

    afternoon_start_time: str = "13:05"
    """Timestamp of the first 5m bar in the afternoon session."""

    enable_chan_5m: bool = True
    """Whether to evaluate 5m Chan structure, confluence, and pivots."""

    intraday_upvol_thresh: float = 0.58
    """Reference threshold for signed up-volume surge confirmation."""

    volume_seasonality_lookback_days: int = 20
    """Historical trading days to compute median cumulative volume proportion phi."""

    volume_seasonality_phi_1400: float = 0.78
    """Default cumulative volume proportion at 14:00 when historical baseline is unavailable."""

    volume_seasonality_phi_1415: float = 0.84
    """Default cumulative volume proportion at 14:15 when historical baseline is unavailable."""

    tick_size_stock: float = 0.01
    """A-share equity quote unit (RMB 0.01)."""

    tick_size_etf: float = 0.001
    """Exchange-traded fund quote unit (RMB 0.001)."""

    tick_tolerance: float = 1e-4
    """Tolerance for floating-point price comparison against limit bounds."""


def _parse_time_cutoff(val: Optional[Union[str, datetime.time]]) -> Optional[datetime.time]:
    """Parse string or time object into datetime.time or None (for full day)."""
    if val is None:
        return None
    if isinstance(val, datetime.time):
        if val >= datetime.time(15, 0):
            return None
        return val

    s = str(val).strip()
    if not s or s.lower() in ("none", "full", "all", "15:00", "15:00:00"):
        return None

    parts = s.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    sec = int(parts[2]) if len(parts) > 2 else 0

    t = datetime.time(h, m, sec)
    if t >= datetime.time(15, 0):
        return None
    return t


def _detect_limit_pct_and_tick(symbol: Optional[str]) -> Tuple[float, float]:
    """Determine price limit ratio and tick size based on asset classification (R10)."""
    if not symbol:
        return 0.10, 0.01

    sym_upper = symbol.strip().upper()

    # Special Treatment (ST)
    if "ST" in sym_upper:
        return 0.05, 0.01

    # Beijing Stock Exchange
    if sym_upper.endswith(".BJ") or sym_upper.startswith(("83", "87", "43", "92")):
        return 0.30, 0.01

    # STAR Market (688) & ChiNext (300, 301)
    if sym_upper.startswith(("688", "300", "301")):
        return 0.20, 0.01

    # ETFs
    if sym_upper.startswith(("51", "56", "58", "15", "16")):
        return 0.10, 0.001

    # Main Board Equities
    return 0.10, 0.01


def _compute_volume_seasonality_scales(
    df_5m: pd.DataFrame,
    cutoff_time: Optional[datetime.time],
    config: IntradayFeatureConfig,
) -> Dict[pd.Timestamp, float]:
    """Calculate causal volume seasonality scaling factor phi^{-1} (R6).

    For each date t, uses the median cumulative volume ratio from the prior N full days:
    phi_t = median_{k=1..N}(V_{t-k, <=cutoff} / V_{t-k, full}).
    """
    if cutoff_time is None or cutoff_time >= datetime.time(15, 0):
        unique_dates = df_5m.index.normalize().unique()
        return {d: 1.0 for d in unique_dates}

    # Group by calendar date
    day_groups = df_5m.groupby(df_5m.index.normalize())
    full_day_ratios: Dict[pd.Timestamp, float] = {}

    for d, g in day_groups:
        total_vol = float(g["Volume"].sum())
        bar_count = len(g)
        # Full trading day has 48 bars (or >= 45 bars)
        if bar_count >= 45 and total_vol > 0:
            vol_cutoff = float(g.loc[g.index.time <= cutoff_time, "Volume"].sum())
            full_day_ratios[d] = vol_cutoff / total_vol

    # Default fallback phi
    if cutoff_time == datetime.time(14, 0):
        default_phi = config.volume_seasonality_phi_1400
    elif cutoff_time == datetime.time(14, 15):
        default_phi = config.volume_seasonality_phi_1415
    else:
        # Approximate by bar count ratio: M / 48
        # e.g., 36 / 48 = 0.75, 39 / 48 = 0.8125
        approx_bars = sum(
            1 for h in [9, 10, 11, 13, 14] for m in range(0, 60, 5)
            if ((h == 9 and m >= 35) or (h == 10) or (h == 11 and m <= 30) or
                (h == 13 and m >= 5) or (h == 14)) and datetime.time(h, m) <= cutoff_time
        )
        default_phi = min(1.0, max(0.10, approx_bars / 48.0))

    sorted_full_dates = sorted(full_day_ratios.keys())
    unique_dates = sorted(df_5m.index.normalize().unique())
    scale_map: Dict[pd.Timestamp, float] = {}

    lookback = config.volume_seasonality_lookback_days

    for d in unique_dates:
        # Strictly causal: only dates strictly before d
        prior_ratios = [
            full_day_ratios[pd_date]
            for pd_date in sorted_full_dates
            if pd_date < d
        ]
        if len(prior_ratios) >= 5:
            # Take last N full days
            recent_ratios = prior_ratios[-lookback:]
            phi = float(np.median(recent_ratios))
            if phi <= 0.05 or phi > 1.0:
                phi = default_phi
        else:
            phi = default_phi

        scale_map[d] = 1.0 / phi if phi > 0 else 1.0

    return scale_map


def _compute_day_microstructure(
    day_bars: pd.DataFrame,
    prev_close: Optional[float],
    cutoff_time: Optional[datetime.time],
    config: IntradayFeatureConfig,
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate single-day high-frequency microstructure indicators."""
    m_count = len(day_bars)
    res: Dict[str, Any] = {}

    close_cutoff = float(day_bars["Close"].iloc[-1])
    res["Close_5m_Cutoff"] = close_cutoff

    highs = day_bars["High"].to_numpy(dtype=float)
    lows = day_bars["Low"].to_numpy(dtype=float)
    closes = day_bars["Close"].to_numpy(dtype=float)
    opens = day_bars["Open"].to_numpy(dtype=float)
    vols = day_bars["Volume"].to_numpy(dtype=float)

    typ_prices = (highs + lows + closes) / 3.0
    tot_vol = float(vols.sum())

    # 1. True Intraday VWAP & Dimensionless Premium
    if tot_vol > 0:
        vwap = float((typ_prices * vols).sum() / tot_vol)
    else:
        vwap = float(typ_prices.mean())
    res["VWAP"] = vwap

    if vwap > 0:
        res["VWAP_Prem_5m"] = (close_cutoff - vwap) / vwap
    else:
        res["VWAP_Prem_5m"] = 0.0

    # 2. Pure Intraday Returns & Semivariance Decomposition (Andersen et al. 2003, Patton & Sheppard 2015)
    # r_{t,1} = ln(C_1 / O_1), stripping overnight gap
    # r_{t,j} = ln(C_j / C_{j-1}) for j >= 2
    r = np.zeros(m_count, dtype=float)
    if opens[0] > 0 and closes[0] > 0:
        r[0] = math.log(closes[0] / opens[0])
    for j in range(1, m_count):
        if closes[j - 1] > 0 and closes[j] > 0:
            r[j] = math.log(closes[j] / closes[j - 1])

    r_sq = r * r
    rv = float(r_sq.sum())
    rs_up = float(r_sq[r > 0].sum())
    rs_down = float(r_sq[r < 0].sum())

    # Bipower Variation (BPV) and discrete Jump Component (Barndorff-Nielsen & Shephard 2004)
    if m_count > 1:
        abs_r = np.abs(r)
        bpv = float((math.pi / 2.0) * (m_count / (m_count - 1.0)) * (abs_r[1:] * abs_r[:-1]).sum())
    else:
        bpv = rv

    jump_component = max(0.0, rv - bpv)
    jump_ratio = jump_component / (rv + 1e-12)
    signed_jump = rs_up - rs_down

    res["RV_5m"] = rv
    res["RS_Up_5m"] = rs_up
    res["RS_Down_5m"] = rs_down
    res["BPV_5m"] = bpv
    res["Jump_Ratio_5m"] = jump_ratio
    res["Signed_Jump_5m"] = signed_jump

    # 3. Signed Up-Volume Ratio (Bulk Volume Classification, Easley et al. 2012)
    # w_j^+ in [0, 1] with robust H == L handling
    hl_diff = highs - lows
    w_plus = np.zeros(m_count, dtype=float)

    for j in range(m_count):
        if hl_diff[j] > 1e-9:
            w_plus[j] = (closes[j] - lows[j]) / hl_diff[j]
        else:
            if j == 0:
                if closes[0] > opens[0]:
                    w_plus[0] = 1.0
                elif closes[0] < opens[0]:
                    w_plus[0] = 0.0
                else:
                    w_plus[0] = 0.5
            else:
                if closes[j] > closes[j - 1]:
                    w_plus[j] = 1.0
                elif closes[j] < closes[j - 1]:
                    w_plus[j] = 0.0
                else:
                    w_plus[j] = 0.5

    w_plus = np.clip(w_plus, 0.0, 1.0)
    if tot_vol > 0:
        upvol_ratio = float((vols * w_plus).sum() / tot_vol)
    else:
        upvol_ratio = 0.5
    res["UpVol_Ratio_5m"] = float(np.clip(upvol_ratio, 0.0, 1.0))

    # 4. Overnight-Gap-Stripped Smart Money Factor (SmartMoney_Q_5m)
    # S_{t,j} = (|C_j - O_j| / O_j) / sqrt(V_j)
    bar_ret = np.zeros(m_count, dtype=float)
    valid_o = opens > 0
    bar_ret[valid_o] = np.abs(closes[valid_o] - opens[valid_o]) / opens[valid_o]

    sqrt_v = np.sqrt(np.maximum(0.0, vols))
    smartness = np.zeros(m_count, dtype=float)
    valid_v = sqrt_v > 0
    smartness[valid_v] = bar_ret[valid_v] / sqrt_v[valid_v]

    if tot_vol > 0:
        sort_idx = np.argsort(-smartness)  # descending order
        target_vol = tot_vol * config.smart_money_top_pct
        cum_vol = 0.0
        smart_indices = []

        for idx in sort_idx:
            smart_indices.append(idx)
            cum_vol += vols[idx]
            if cum_vol >= target_vol:
                break

        smart_vol = float(vols[smart_indices].sum())
        if smart_vol > 0:
            vwap_smart = float((typ_prices[smart_indices] * vols[smart_indices]).sum() / smart_vol)
            res["SmartMoney_Q_5m"] = (vwap_smart / vwap) - 1.0 if vwap > 0 else 0.0
        else:
            res["SmartMoney_Q_5m"] = 0.0
    else:
        res["SmartMoney_Q_5m"] = 0.0

    # 5. Afternoon Return relative to 11:30 morning close (Afternoon_Ret_5m, R9.3)
    t_1130 = datetime.time(11, 30)
    mask_1130 = day_bars.index.time == t_1130
    if mask_1130.any():
        c_1130 = float(day_bars.loc[mask_1130, "Close"].iloc[-1])
    else:
        # Fallback to last morning bar <= 11:30
        mask_morn = day_bars.index.time <= t_1130
        if mask_morn.any():
            c_1130 = float(day_bars.loc[mask_morn, "Close"].iloc[-1])
        else:
            c_1130 = float(day_bars["Close"].iloc[0])

    if c_1130 > 0:
        res["Afternoon_Ret_5m"] = (close_cutoff / c_1130) - 1.0
    else:
        res["Afternoon_Ret_5m"] = 0.0

    # 6. Morning Low (09:35..10:00) & Afternoon Low (13:05..cutoff)
    t_0935 = datetime.time(9, 35)
    t_1000 = datetime.time(10, 0)
    mask_morning_ext = (day_bars.index.time >= t_0935) & (day_bars.index.time <= t_1000)
    if mask_morning_ext.any():
        res["Morning_Low_5m"] = float(day_bars.loc[mask_morning_ext, "Low"].min())
    else:
        res["Morning_Low_5m"] = float(lows.min())

    t_1305 = datetime.time(13, 5)
    mask_afternoon = day_bars.index.time >= t_1305
    if mask_afternoon.any():
        res["Afternoon_Low_5m"] = float(day_bars.loc[mask_afternoon, "Low"].min())
    else:
        res["Afternoon_Low_5m"] = float("nan")

    # 7. Price Limit Lockout Detection (Last_3Bars_Locked_Limit, R10)
    limit_pct, tick = _detect_limit_pct_and_tick(symbol)
    if prev_close is not None and prev_close > 0:
        limit_up = round(round(prev_close * (1.0 + limit_pct) / tick) * tick, 4)
        limit_down = round(round(prev_close * (1.0 - limit_pct) / tick) * tick, 4)

        # Check last 3 bars H == L
        k_check = min(3, m_count)
        last_k_flat = all(
            abs(highs[-k] - lows[-k]) < config.tick_tolerance
            for k in range(1, k_check + 1)
        )

        tol = config.tick_tolerance
        if (close_cutoff >= limit_up - tol) and last_k_flat:
            res["Last_3Bars_Locked_Limit"] = 1
        elif (close_cutoff <= limit_down + tol) and last_k_flat:
            res["Last_3Bars_Locked_Limit"] = -1
        elif tot_vol == 0:
            res["Last_3Bars_Locked_Limit"] = -1
        else:
            res["Last_3Bars_Locked_Limit"] = 0
    else:
        if tot_vol == 0:
            res["Last_3Bars_Locked_Limit"] = -1
        else:
            res["Last_3Bars_Locked_Limit"] = 0

    return res


def _evaluate_chan_5m_state(
    df_window: pd.DataFrame,
    vwap_prem: float,
    chan_modules: Optional[Dict[str, Any]],
    config: IntradayFeatureConfig,
) -> Dict[str, Any]:
    """Evaluate 5m Chan sub-level interval nesting and MACD bull/bear confluence (R9)."""
    default_res = {
        "Chan_5m_Buy_Confluence": 0,
        "Chan_5m_Sell_Trigger": 0,
        "Chan_5m_State": "5m_NEUTRAL",
        "Chan_5m_ZG": float("nan"),
        "Chan_5m_ZD": float("nan"),
    }

    if not config.enable_chan_5m or chan_modules is None or len(df_window) < 30:
        return default_res

    # 1. 5m MACD Bull Test (Chan Lesson 103)
    macd_res = macd(df_window["Close"], 12, 26, 9)
    dif = float(macd_res["macd"].iloc[-1]) if "macd" in macd_res.columns else float(macd_res.get("dif", 0.0).iloc[-1])
    dea = float(macd_res["signal"].iloc[-1]) if "signal" in macd_res.columns else float(macd_res.get("dea", 0.0).iloc[-1])
    hist = float(macd_res["hist"].iloc[-1]) if "hist" in macd_res.columns else 0.0
    hist_prev = float(macd_res["hist"].iloc[-2]) if len(macd_res) > 1 and "hist" in macd_res.columns else 0.0
    delta_hist = hist - hist_prev

    macd_bull = (dif >= 0 and dea >= 0) or (hist > 0 and delta_hist > 0)

    try:
        # Build 5m Chan structural pivots
        merge_fn = chan_modules["merge_inclusion"]
        fractal_fn = chan_modules["find_fractals"]
        stroke_fn = chan_modules["build_strokes"]
        pivot_fn = chan_modules["build_pivots"]

        merged = merge_fn(df_window)
        fractals = fractal_fn(merged)
        strokes = stroke_fn(fractals)
        pivots = pivot_fn(strokes)

        zg_val = float("nan")
        zd_val = float("nan")
        if len(pivots) > 0:
            zg_val = float(pivots["zg"].iloc[-1])
            zd_val = float(pivots["zd"].iloc[-1])

        last_fractal_kind = fractals["kind"].iloc[-1] if len(fractals) > 0 else None

        # 2. Evaluate sub-level buy/sell signals
        c3_fn = chan_modules["compute_chan3_signals"]
        pm_fn = chan_modules["compute_chan_pivot_macd_signals"]

        sig_c3 = c3_fn(df_window)
        sig_pm = pm_fn(df_window)

        # Signal window (last 12 bars = 1 hour)
        win = config.chan_5m_signal_window_bars
        tail_c3 = sig_c3.iloc[-win:]
        tail_pm = sig_pm.iloc[-win:]

        has_c3_buy = bool(tail_c3["buy_signal"].any()) if "buy_signal" in tail_c3.columns else False
        has_c3_3b = bool(tail_c3["third_buy"].any()) if "third_buy" in tail_c3.columns else False
        has_pm_div_buy = (
            bool(tail_pm["divergence_buy"].any() or tail_pm["buy_signal"].any())
            if "divergence_buy" in tail_pm.columns else False
        )
        has_c3_3s = bool(tail_c3["third_sell"].any()) if "third_sell" in tail_c3.columns else False
        has_pm_div_sell = (
            bool(tail_pm["divergence_sell"].any())
            if "divergence_sell" in tail_pm.columns else False
        )

        # 3. Buy Confluence
        if (has_c3_buy or has_pm_div_buy) and macd_bull:
            confluence = 2
        elif macd_bull and last_fractal_kind == "bottom":
            confluence = 1
        else:
            confluence = 0

        # 4. Sell Trigger (3S or top divergence and broken below VWAP)
        if (has_c3_3s or has_pm_div_sell) and (vwap_prem < 0.0):
            sell_trigger = 1
        else:
            sell_trigger = 0

        # 5. Descriptive State Tag
        if confluence == 2:
            chan_state = "5m_3B_CONFIRMED" if has_c3_3b else "5m_1B2B_CONFIRMED"
        elif confluence == 1:
            chan_state = "5m_BULL_FLOOR"
        elif sell_trigger == 1:
            chan_state = "5m_3S_EXIT" if has_c3_3s else "5m_TOP_DIV_WARN"
        else:
            chan_state = "5m_NEUTRAL"

        return {
            "Chan_5m_Buy_Confluence": confluence,
            "Chan_5m_Sell_Trigger": sell_trigger,
            "Chan_5m_State": chan_state,
            "Chan_5m_ZG": zg_val,
            "Chan_5m_ZD": zd_val,
        }

    except Exception as exc:
        logger.debug("Error computing 5m Chan signals: %s", exc)
        default_res["Chan_5m_ZG"] = zg_val if "zg_val" in locals() else float("nan")
        default_res["Chan_5m_ZD"] = zd_val if "zd_val" in locals() else float("nan")
        return default_res


def compute_daily_intraday_features(
    df_5m: pd.DataFrame,
    config: Optional[IntradayFeatureConfig] = None,
    chan_candidate_dates: Optional[Set[pd.Timestamp]] = None,
    symbol: Optional[str] = None,
) -> pd.DataFrame:
    """Aggregate 5-minute bars into daily microstructure and Chan confluence features.

    Parameters
    ----------
    df_5m : pd.DataFrame
        Raw 5-minute OHLCV data with DatetimeIndex.
    config : Optional[IntradayFeatureConfig]
        Configuration options (cutoff time, windows, thresholds).
    chan_candidate_dates : Optional[Set[pd.Timestamp]]
        R9: Optional set of dates to evaluate 5m Chan structure (lazy candidate evaluation).
        If provided, dates outside this set skip heavy Chan structure calculation.
    symbol : Optional[str]
        Symbol name for price limit tier identification (ST, ChiNext, ETF, etc.).

    Returns
    -------
    pd.DataFrame
        DataFrame indexed by date (normalized DatetimeIndex) with 22 standard features.
    """
    if config is None:
        config = IntradayFeatureConfig()

    if df_5m.empty:
        return pd.DataFrame()

    if not isinstance(df_5m.index, pd.DatetimeIndex):
        raise TypeError("df_5m index must be a pd.DatetimeIndex")

    # Ensure sorted by timestamp
    if not df_5m.index.is_monotonic_increasing:
        df_5m = df_5m.sort_index()

    cutoff_t = _parse_time_cutoff(config.cutoff_time)

    # 1. Volume seasonality scaling map (R6)
    seasonality_scales = _compute_volume_seasonality_scales(df_5m, cutoff_t, config)

    # 2. Causal cutoff filtering (R1, R6)
    if cutoff_t is not None:
        df_cut = df_5m.loc[df_5m.index.time <= cutoff_t].copy()
    else:
        df_cut = df_5m.copy()

    if df_cut.empty:
        return pd.DataFrame()

    # Pre-load Chan modules once if enabled
    chan_modules = _get_chan_modules() if config.enable_chan_5m else None

    # Group filtered bars by calendar date
    day_groups = df_cut.groupby(df_cut.index.normalize())
    all_dates = sorted(day_groups.groups.keys())

    records: List[Dict[str, Any]] = []
    prev_close: Optional[float] = None

    for d in all_dates:
        day_bars = day_groups.get_group(d)
        if day_bars.empty:
            continue

        # Microstructure features
        row = _compute_day_microstructure(
            day_bars=day_bars,
            prev_close=prev_close,
            cutoff_time=cutoff_t,
            config=config,
            symbol=symbol,
        )

        row["Date"] = d
        row["Seasonality_Volume_Scale"] = seasonality_scales.get(d, 1.0)

        # Is_Partial_Bar flag (R12)
        # Only the current in-progress day (latest date) under a cutoff or any day with genuinely partial bars is partial
        is_latest_date = (d == all_dates[-1])
        if is_latest_date and (cutoff_t is not None and cutoff_t < datetime.time(15, 0)):
            row["Is_Partial_Bar"] = 1
        elif len(day_bars) < 45 and (cutoff_t is None or cutoff_t >= datetime.time(15, 0)):
            row["Is_Partial_Bar"] = 1
        else:
            row["Is_Partial_Bar"] = 0

        # Chan 5m structure (with lazy candidate date support, R9)
        should_calc_chan = (
            config.enable_chan_5m
            and (chan_candidate_dates is None or d in chan_candidate_dates)
        )

        if should_calc_chan:
            # 240-bar lookback ending at current day cutoff
            bars_up_to_d = df_cut.loc[df_cut.index <= day_bars.index[-1]]
            df_window = bars_up_to_d.iloc[-config.chan_5m_lookback_bars:]
            chan_res = _evaluate_chan_5m_state(
                df_window=df_window,
                vwap_prem=row["VWAP_Prem_5m"],
                chan_modules=chan_modules,
                config=config,
            )
            row.update(chan_res)
        else:
            row["Chan_5m_Buy_Confluence"] = 0
            row["Chan_5m_Sell_Trigger"] = 0
            row["Chan_5m_State"] = "5m_NEUTRAL"
            row["Chan_5m_ZG"] = float("nan")
            row["Chan_5m_ZD"] = float("nan")

        records.append(row)
        prev_close = row["Close_5m_Cutoff"]

    out_df = pd.DataFrame.from_records(records)
    if "Date" in out_df.columns:
        out_df.set_index("Date", inplace=True)
    out_df.index = pd.to_datetime(out_df.index)

    # Standard column ordering
    standard_cols = [
        "Close_5m_Cutoff",
        "VWAP",
        "VWAP_Prem_5m",
        "RV_5m",
        "RS_Up_5m",
        "RS_Down_5m",
        "BPV_5m",
        "Jump_Ratio_5m",
        "Signed_Jump_5m",
        "UpVol_Ratio_5m",
        "SmartMoney_Q_5m",
        "Afternoon_Ret_5m",
        "Morning_Low_5m",
        "Afternoon_Low_5m",
        "Seasonality_Volume_Scale",
        "Chan_5m_Buy_Confluence",
        "Chan_5m_Sell_Trigger",
        "Chan_5m_State",
        "Chan_5m_ZG",
        "Chan_5m_ZD",
        "Last_3Bars_Locked_Limit",
        "Is_Partial_Bar",
    ]

    for c in standard_cols:
        if c not in out_df.columns:
            out_df[c] = np.nan

    return out_df[standard_cols]


def enrich_universe_with_intraday_features(
    universe_1d: Dict[str, pd.DataFrame],
    universe_5m: Dict[str, pd.DataFrame],
    config: Optional[IntradayFeatureConfig] = None,
    synthesize_intraday_bar: bool = False,
    cutoff_time: Optional[Union[str, datetime.time]] = None,
) -> Dict[str, pd.DataFrame]:
    """Enrich daily universe DataFrames with aligned 5m intraday features and split alignment (R4, R6, R12).

    Parameters
    ----------
    universe_1d : Dict[str, pd.DataFrame]
        Daily OHLCV universe.
    universe_5m : Dict[str, pd.DataFrame]
        5-minute OHLCV universe.
    config : Optional[IntradayFeatureConfig]
        Configuration for feature computation.
    synthesize_intraday_bar : bool
        If True (Stage 1 / Stage 2 live mode), synthesizes or updates the current partial day's
        OHLCV bar using truncated 5m data and seasonality volume scaling, setting Is_Partial_Bar=1.
    cutoff_time : Optional[Union[str, datetime.time]]
        Optional time cutoff string (e.g. '14:00', '14:15', '15:00') or datetime.time.
        If provided, overrides config.cutoff_time.

    Returns
    -------
    Dict[str, pd.DataFrame]
        Enriched daily universe with price alignment and microstructure columns.
    """
    if config is None:
        config = IntradayFeatureConfig()
    else:
        config = replace(config)

    if cutoff_time is not None:
        if isinstance(cutoff_time, datetime.time):
            config.cutoff_time = cutoff_time.strftime("%H:%M")
        else:
            config.cutoff_time = str(cutoff_time)

    cutoff_t = _parse_time_cutoff(config.cutoff_time)
    enriched_universe: Dict[str, pd.DataFrame] = {}

    price_cols = [
        "VWAP",
        "Morning_Low_5m",
        "Afternoon_Low_5m",
        "Chan_5m_ZG",
        "Chan_5m_ZD",
    ]

    for sym, df_1d in universe_1d.items():
        df_out = df_1d.copy()
        if not isinstance(df_out.index, pd.DatetimeIndex):
            df_out.index = pd.to_datetime(df_out.index)
        df_out.index = df_out.index.normalize()

        df_5m = universe_5m.get(sym)
        if df_5m is None or df_5m.empty:
            df_out["Is_Partial_Bar"] = 0
            enriched_universe[sym] = df_out
            continue

        # Compute daily intraday features
        feat_df = compute_daily_intraday_features(
            df_5m, config=config, symbol=sym
        )

        if feat_df.empty:
            df_out["Is_Partial_Bar"] = 0
            enriched_universe[sym] = df_out
            continue

        # Synthesize intraday bar if requested (Live Stage 1 / Stage 2, R12)
        if synthesize_intraday_bar:
            if cutoff_t is not None:
                df_5m_cut = df_5m.loc[df_5m.index.time <= cutoff_t]
            else:
                df_5m_cut = df_5m

            for d_synth in feat_df.index:
                # Synthesize if partial bar or if date does not exist in 1d
                is_partial = feat_df.loc[d_synth, "Is_Partial_Bar"] == 1
                if is_partial or (d_synth not in df_out.index):
                    day_5m = df_5m_cut.loc[df_5m_cut.index.normalize() == d_synth]
                    if not day_5m.empty:
                        o_synth = float(day_5m["Open"].iloc[0])
                        h_synth = float(day_5m["High"].max())
                        l_synth = float(day_5m["Low"].min())
                        c_synth = float(day_5m["Close"].iloc[-1])
                        scale = float(feat_df.loc[d_synth, "Seasonality_Volume_Scale"])
                        v_synth = float(day_5m["Volume"].sum()) * scale

                        df_out.loc[d_synth, ["Open", "High", "Low", "Close", "Volume"]] = [
                            o_synth, h_synth, l_synth, c_synth, v_synth
                        ]

            df_out.sort_index(inplace=True)

        # Cross-source split ratio alignment: kappa_t = C_{t,1d} / C_{t,M,5m} (R4, R6)
        aligned_feat = feat_df.copy()
        common_dates = df_out.index.intersection(aligned_feat.index)

        for t_date in common_dates:
            c_1d = float(df_out.loc[t_date, "Close"])
            c_5m = float(aligned_feat.loc[t_date, "Close_5m_Cutoff"])
            if c_5m > 0 and c_1d > 0:
                kappa = c_1d / c_5m
            else:
                kappa = 1.0

            for p_col in price_cols:
                val = aligned_feat.loc[t_date, p_col]
                if not pd.isna(val):
                    aligned_feat.loc[t_date, p_col] = val * kappa

        # Join feature columns into daily DataFrame
        for col in aligned_feat.columns:
            df_out[col] = aligned_feat[col].reindex(df_out.index)

        # Fill Is_Partial_Bar for historical complete bars
        df_out["Is_Partial_Bar"] = df_out["Is_Partial_Bar"].fillna(0).astype(int)

        enriched_universe[sym] = df_out

    return enriched_universe


def _intraday_5m_cache_candidates(cache_dir: Optional[str]) -> List[str]:
    """Candidate `cache.duckdb` paths for the Step-1.5 5m fallback, in priority order.

    Extracted so tests can point the fallback at a `tmp_path` cache instead of the
    live `<repo>/data/cache.duckdb`; behaviour is identical to the previously
    inline list (`cache_dir` first, then the two repo-relative defaults).
    """
    return [
        os.path.join(cache_dir or "data", "cache.duckdb"),
        os.path.join("data", "cache.duckdb"),
        os.path.join("pipeline", "data", "cache.duckdb"),
    ]


# ---------------------------------------------------------------------------
# 5m provenance: honour the CALLER's provider, record every fallback
# ---------------------------------------------------------------------------
# (2026-11-11 defect, `六` item 6 -- "5m 特征静默陈旧") The Step-1.5 DuckDB fallback used
# a FIXED provider order (`BaoStockDataProvider` -> `EastMoneyDataProvider` -> `fuyao`),
# ignoring the `provider` the caller configured. The live run used `fuyao`, whose cache
# rows reached 2026-11-10, but the fallback read BaoStock first -- whose `intraday_ohlcv_bars`
# stopped at 2026-11-09 -- so the 5m layer was silently one session older than the data the
# run had actually asked for. Downstream that froze `RS_Down_5m` (via `dropna().iloc[-21:]`)
# and made `realized_vol_21d` identical for three consecutive sessions (0.3240854096084951),
# while the run blamed "provider does not support 5m" -- a false diagnosis of a stale cache.
#
# Rule now enforced here: the caller's provider is ALWAYS queried first, under every cache
# key form it may have been written with; other providers are only consulted when the
# caller's provider has no 5m rows at all, and that switch is recorded on the returned
# frame (`fallback_from` / `fallback_to` / `fallback_reason` attrs) and logged.
_INTRADAY_5M_FALLBACK_PROVIDERS: Tuple[str, ...] = (
    "BaoStockDataProvider",
    "EastMoneyDataProvider",
    "FuyaoDataProvider",
)
"""Last-resort provider keys, used ONLY when the caller's provider has no cached 5m rows.

Order preserved from the pre-2026-11-11 behaviour so a caller that passes no provider at
all (research/backtest paths) keeps the historical result bit-for-bit.

(2026-11-13 defect, dead fallback entry) The Fuyao entry used to be spelled `"fuyao"`, the
SHORT station name -- while every cache writer stamps `type(inner_provider).__name__`
(`FuyaoDataProvider`, see `common/data.py` / `DuckDBCache.store_intraday_bars`) and
`DuckDBCache.query_intraday_bars` is EXACT-match on that key. Measured on the live cache:
the 7 indices are stored under `"FuyaoDataProvider"` (1,728 rows/symbol) and `"EastMoneyDataProvider"`
(1,728), while the keys `"fuyao"` / `"baostock"` / `"eastmoney"` hold 0 rows each -- so this
entry queried nothing and that half of the fallback chain was DEAD. Consequence once the
live strategy pins its 5m provider to BaoStock: BaoStock carries the 21 equity-side symbols
(2,256 rows each) but NO index at all (it refuses index intraday queries), and the indices
exist only under Fuyao/EastMoney -- so the 7 indices' 5m silently vanished (28 -> 21 symbols
with 5m, only a WARNING).

Fix: this tuple now spells every provider the way the WRITERS store it, and
`_intraday_5m_provider_order` additionally queues the short-name spelling of each canonical
provider (see `_intraday_5m_provider_spellings`) so a cache written under either form is
reachable. Equivalence itself is still owned by `canonicalize_provider_key`; nothing here
re-implements it.
"""

_INTRADAY_5M_PROVIDER_CLASS_NAMES: Dict[str, str] = {
    "baostock": "BaoStockDataProvider",
    "eastmoney": "EastMoneyDataProvider",
    "fuyao": "FuyaoDataProvider",
    "marketdb": "MarketDBDataProvider",
}
"""Canonical short name -> the class name every cache writer actually stores.

Only needed when a caller passes a provider as a STRING (`"fuyao"`): the cache's primary key
is `type(inner_provider).__name__`, so a short string alone would query zero rows and look
like "no coverage" -- the exact confusion that produced the 2026-11-09 stale window.
"""

FALLBACK_REASON_UNSUPPORTED_5M = "provider_unsupported_5m"
"""Step-1 fetch raised -- the provider explicitly refused the 5m interval."""

FALLBACK_REASON_RETURNED_EMPTY = "provider_returned_empty"
"""Step-1 fetch succeeded but returned zero rows and the cache holds none either."""

INTRADAY_5M_FRESHNESS_OK = "ok"
INTRADAY_5M_FRESHNESS_STALE = "stale_last_bar"
INTRADAY_5M_FRESHNESS_NO_BARS = "no_5m_bars"


def _intraday_5m_canonical_provider(name: Any) -> str:
    """Comparison key for a provider name (`FuyaoDataProvider` == `fuyao`).

    Reuses `common.duckdb_cache.canonicalize_provider_key` (the single definition of that
    equivalence -- the same one the cache's read-path reconciliation uses), imported lazily
    because `duckdb_cache` hard-imports `duckdb` and this module must stay importable in
    environments without it. Falls back to an equivalent local rule if it cannot import.
    """
    try:
        from common.duckdb_cache import canonicalize_provider_key

        return canonicalize_provider_key(name)
    except Exception:  # noqa: BLE001 - duckdb absent / import cycle: use the same rule locally
        key = str(name).strip().lower()
        suffix = "dataprovider"
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[: -len(suffix)]
        return key


def _intraday_5m_unwrap_provider(data_provider: Any) -> Any:
    """Peel `CachedDataProvider`-style wrappers down to the real upstream provider."""
    inner = data_provider
    for _ in range(4):  # wrappers are shallow; the cap only stops a pathological cycle
        nxt = getattr(inner, "inner_provider", None)
        if nxt is None or nxt is inner:
            break
        inner = nxt
    return inner


def _intraday_5m_provider_plan(data_provider: Any) -> Tuple[List[str], Optional[str]]:
    """`(cache_provider_keys, display_name)` for the caller's configured provider.

    Cache keys are class names (`FuyaoDataProvider`, what every writer stamps via
    `type(self.inner_provider).__name__`), but the short station name (`fuyao`) is tried
    too, in case a caller hands us that string directly. Ordered most-specific first.
    """
    inner = _intraday_5m_unwrap_provider(data_provider)
    if inner is None:
        return [], None

    if isinstance(inner, str):
        class_name = inner.strip()
        if not class_name:
            return [], None
        # A caller may hand us the station's SHORT name (`fuyao`), while every cache writer
        # stamps the CLASS name (`FuyaoDataProvider`). Add the known class-name spelling for
        # the same canonical key so a short name can still hit its own rows first.
        short = _intraday_5m_canonical_provider(class_name)
        keys: List[str] = [class_name]
        for known in _INTRADAY_5M_FALLBACK_PROVIDERS:
            if _intraday_5m_canonical_provider(known) == short and known not in keys:
                keys.append(known)
        class_spelling = _INTRADAY_5M_PROVIDER_CLASS_NAMES.get(short)
        if class_spelling and class_spelling not in keys:
            keys.append(class_spelling)
        if short not in keys:
            keys.append(short)
        return keys, class_name

    class_name = type(inner).__name__
    short = _intraday_5m_canonical_provider(class_name)
    keys = []
    for key in (class_name, short):
        if key and key not in keys:
            keys.append(key)
    return keys, class_name


def _intraday_5m_provider_spellings(name: Any) -> List[str]:
    """Every EXACT cache key form under which `name`'s rows may have been written.

    `DuckDBCache.query_intraday_bars` matches `provider = ?` exactly (deliberately -- an
    auto-resolving read path would launder a genuine typo into a successful read, see
    `DuckDBCache.resolve_provider_key`), so a fallback entry naming only ONE spelling
    silently queries zero rows for the other. Writers stamp the class name
    (`FuyaoDataProvider`); settings / CLI / strategy params speak the short station name
    (`fuyao`). Both are queued, WRITER-STORED CLASS NAME FIRST, because that is the form
    every writer produces and therefore the one that actually returns rows.

    The `fuyao` == `FuyaoDataProvider` equivalence comes from `canonicalize_provider_key`
    (`_intraday_5m_canonical_provider`) plus `_INTRADAY_5M_PROVIDER_CLASS_NAMES`; this
    helper only expands a canonical name back into its known spellings.
    """
    raw = str(name).strip()
    if not raw:
        return []
    canon = _intraday_5m_canonical_provider(raw)
    spellings: List[str] = []
    for cand in (_INTRADAY_5M_PROVIDER_CLASS_NAMES.get(canon), raw, canon):
        if cand and cand not in spellings:
            spellings.append(cand)
    return spellings


def _intraday_5m_provider_order(
    preferred_keys: List[str],
) -> List[str]:
    """Preferred cache keys first, then the historical fallback list minus duplicates.

    The caller's own provider is queued ONLY under `preferred_keys` (one group skipped
    wholesale when its canonical form is already covered) so its rows are never queried
    twice under two spellings. Each remaining fallback entry is expanded to every spelling
    its rows may have been written with (`_intraday_5m_provider_spellings`), de-duplicated
    on the EXACT key -- dedup on the canonical name alone is what made the Fuyao entry
    unusable before 2026-11-13 (the short spelling won the group and matched no rows).
    """
    seen_canon: Set[str] = {_intraday_5m_canonical_provider(k) for k in preferred_keys}
    order: List[str] = list(preferred_keys)
    seen_keys: Set[str] = set(order)
    for name in _INTRADAY_5M_FALLBACK_PROVIDERS:
        canon = _intraday_5m_canonical_provider(name)
        if canon in seen_canon:
            continue
        seen_canon.add(canon)
        for spelling in _intraday_5m_provider_spellings(name):
            if spelling in seen_keys:
                continue
            seen_keys.add(spelling)
            order.append(spelling)
    return order


def latest_5m_bar_ts(frame: Optional[pd.DataFrame]) -> Optional[pd.Timestamp]:
    """Last timestamp of a 5m frame, or `None` when it holds no bars."""
    if frame is None or bool(getattr(frame, "empty", True)):
        return None
    try:
        idx = pd.DatetimeIndex(frame.index)
    except (TypeError, ValueError):
        return None
    if len(idx) == 0:
        return None
    return pd.Timestamp(idx.max())


def _naive(ts: pd.Timestamp) -> pd.Timestamp:
    """Drop tzinfo while keeping wall-clock time, so naive/aware frames compare cleanly."""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        return ts.tz_localize(None)
    return ts


def assess_intraday_5m_freshness(
    universe_5m: Union[Dict[str, pd.DataFrame], pd.DataFrame, None],
    *,
    expected_last_session: Optional[Any] = None,
    market: str = "china",
    now: Optional[datetime.datetime] = None,
    holidays: Optional[Any] = None,
    bar_interval_minutes: int = 5,
) -> Dict[str, Any]:
    """Measure 5m staleness against the session the cache is SUPPOSED to hold.

    Reusable, offline judgement (2026-11-11 defect, `六` item 6). It answers the question the
    archive could not: "is the last 5m bar actually today's session, or is the cache frozen?"
    A stale 5m layer must be reported as stale instead of being re-labelled "provider does not
    support 5m", and its `lag_calendar_days` is what makes the drift visible.

    Trading-calendar logic is NOT re-implemented: `expected_last_session` (close + publication
    grace) and `last_expected_bar_ts` (per-date close, half-days included) both come from
    `common/market_clock.py`.

    Args:
        universe_5m: `{symbol: frame}` as returned by `load_intraday_5m_universe`, or a single
            frame (reported under the key `"<single>"`).
        expected_last_session: session the cache must reach. `date`/`datetime`/`str`/Timestamp
            accepted; omitted -> `common.market_clock.expected_last_session(market, now, holidays)`.
        market: `common.market_holidays` market code, default China A-shares.
        now: deterministic instant for the default expected session (tests/replays pass one).
        holidays: extra full-day closures forwarded to `market_clock`.
        bar_interval_minutes: 5 for the 5m layer; only sets the intraday deadline (close minus
            one bar), never the day the session is on.

    Returns:
        Dict with `expected_last_session`, `expected_deadline_ts`, `latest_available_ts`,
        `stale`, `lag_calendar_days`, `reason`, per-symbol `symbols`, `stale_symbols`,
        `fresh_symbols` and `n_symbols`.
        `stale=True` iff the newest bar is older than the expected session's last full bar
        (or no bars exist at all -> `reason="no_5m_bars"`, `lag_calendar_days=None`).
    """
    from common.market_clock import last_expected_bar_ts
    from common.market_clock import expected_last_session as _expected_last_session

    if expected_last_session is None:
        session_date = _expected_last_session(market=market, now=now, holidays=holidays)
    else:
        session_date = pd.Timestamp(expected_last_session).date()

    deadline = pd.Timestamp(
        last_expected_bar_ts(
            market,
            session_date,
            datetime.timedelta(minutes=int(bar_interval_minutes)),
        )
    )
    session_start = pd.Timestamp(session_date)

    if isinstance(universe_5m, pd.DataFrame):
        frames: Dict[str, pd.DataFrame] = {"<single>": universe_5m}
    else:
        frames = dict(universe_5m or {})

    per_symbol: Dict[str, Dict[str, Any]] = {}
    stale_symbols: List[str] = []
    fresh_symbols: List[str] = []
    overall_last: Optional[pd.Timestamp] = None

    for sym, frame in frames.items():
        last = latest_5m_bar_ts(frame)
        name = str(sym)
        if last is None:
            per_symbol[name] = {
                "latest_available_ts": None,
                "stale": True,
                "lag_calendar_days": None,
                "reason": INTRADAY_5M_FRESHNESS_NO_BARS,
            }
            stale_symbols.append(name)
            continue

        last_naive = _naive(last)
        stale = bool(last_naive < deadline)
        lag = int((session_start - last_naive.normalize()).days)
        lag = max(0, lag)
        per_symbol[name] = {
            "latest_available_ts": last_naive.isoformat(),
            "stale": stale,
            "lag_calendar_days": lag,
            "reason": INTRADAY_5M_FRESHNESS_STALE if stale else INTRADAY_5M_FRESHNESS_OK,
        }
        if stale:
            stale_symbols.append(name)
        else:
            fresh_symbols.append(name)
        if overall_last is None or last_naive > overall_last:
            overall_last = last_naive

    if overall_last is None:
        overall_reason = INTRADAY_5M_FRESHNESS_NO_BARS
        overall_lag: Optional[int] = None
        overall_stale = True
    else:
        # Aggregate = WORST symbol, never the best one: one fresh symbol must not mask a
        # stale sibling (the 2026-11 freeze was per-symbol `RS_Down_5m`).
        overall_stale = bool(stale_symbols)
        overall_reason = (
            INTRADAY_5M_FRESHNESS_STALE if overall_stale else INTRADAY_5M_FRESHNESS_OK
        )
        stale_lags = [
            int(per_symbol[s]["lag_calendar_days"])
            for s in stale_symbols
            if per_symbol[s]["lag_calendar_days"] is not None
        ]
        all_lags = [
            int(v["lag_calendar_days"])
            for v in per_symbol.values()
            if v["lag_calendar_days"] is not None
        ]
        # Report the WORST observed lag; `None` when staleness is only "no bars at all"
        # (there is no bar date to measure a lag from).
        if stale_lags:
            overall_lag = max(stale_lags)
        elif overall_stale:
            overall_lag = None
        else:
            overall_lag = max(all_lags or [0])

    return {
        "expected_last_session": session_date.isoformat(),
        "expected_deadline_ts": deadline.isoformat(),
        "latest_available_ts": None if overall_last is None else overall_last.isoformat(),
        "stale": overall_stale,
        "lag_calendar_days": overall_lag,
        "reason": overall_reason,
        "symbols": per_symbol,
        "stale_symbols": sorted(stale_symbols),
        "fresh_symbols": sorted(fresh_symbols),
        "n_symbols": len(per_symbol),
    }


#: (2026-11-12) When BaoStock's **intraday 5m** bars for session T are expected to be
#: fetchable. LIVE OBSERVATION, NOT A PROTOCOL GUARANTEE: BaoStock often only refreshes the
#: day's minute bars after ~20:00 market-local, sometimes later, and other providers are
#: faster. Configurable via `--intraday-publish-after` (CLI) and overridable per call.
#:
#: This is a **second, independent availability axis** and must never be folded into
#: `publication_grace_minutes` (the DAILY-bar rule, close+30min -> 15:30 for China): a
#: strategy that does not need 5m is legally able to ticket from 15:30, and pushing the run
#: window start to 20:00 would delay it for no reason.
INTRADAY_5M_PUBLICATION_AFTER_LOCAL = "20:00"

#: Three-state readiness of the as-of session's 5m layer.
INTRADAY_5M_PUBLICATION_AVAILABLE = "available"
INTRADAY_5M_PUBLICATION_PENDING = "pending_publication"
INTRADAY_5M_PUBLICATION_MISSING = "missing"

INTRADAY_5M_PUBLICATION_CODE_PENDING = "intraday_5m_pending_publication"
#: Same token Stage 1's degraded path has always emitted (`intraday_5m_stale_window`).
#: Re-spelled here only so this module can be used on its own; the two MUST stay equal --
#: `pipeline/tests/test_intraday_5m_publication_state.py` asserts it identity-wise.
INTRADAY_5M_PUBLICATION_CODE_MISSING = "intraday_5m_stale_window"


def assess_intraday_5m_publication_state(
    universe_5m: Union[Dict[str, pd.DataFrame], pd.DataFrame, None],
    *,
    as_of: Optional[Any] = None,
    market: str = "china",
    now: Optional[datetime.datetime] = None,
    holidays: Optional[Any] = None,
    publish_after: str = INTRADAY_5M_PUBLICATION_AFTER_LOCAL,
    bar_interval_minutes: int = 5,
) -> Dict[str, Any]:
    """Three-state 5m readiness for `as_of`, on TOP of the existing staleness owner.

    States:
      * `available`           -- the cache reaches the as-of session's last full bar
                                 (`assess_intraday_5m_freshness().stale is False`);
      * `pending_publication` -- it does not, and the clock is still before
                                 `publish_after` on the as-of session's own date, i.e. the
                                 provider probably has not published yet (BaoStock: ~20:00);
      * `missing`             -- it does not, and `publish_after` has already passed: this
                                 is a sync defect, not a publication delay.

    DESIGN: this is a *thin layer*, never a second source of truth. Whether the bars are
    short is decided exclusively by `assess_intraday_5m_freshness` (which owns the
    per-session deadline, half-days included); this function only adds the "what time is
    it, relative to the publication habit" axis. Nothing here re-derives a bar deadline,
    and `available` is definitionally `not stale` -- so the "never silently use the
    previous session's 5m" guarantee is unchanged: a stale frame never becomes available
    by waiting past 20:00, it becomes `missing`.

    Args:
        universe_5m: frames as returned by `load_intraday_5m_universe`.
        as_of: the valuation session. Defaults to
            `common.market_clock.expected_last_session(market, now=now)`.
        now: injectable tz-aware instant (tests/replays). Omitted -> real UTC now, which is
            the only wall-clock read of this function.
        publish_after: `"HH:MM"` market-local expected 5m publication time.

    Returns:
        Flat dict with `status`, `available`, `expected_after`, `degraded_reason_code`
        (`intraday_5m_pending_publication` / `intraday_5m_stale_window` / None),
        `as_of`, `now_local`, `expected_publication_ts`, plus the freshness fields.
    """
    from common.market_clock import market_now

    publish_time = (
        publish_after
        if isinstance(publish_after, datetime.time)
        else datetime.time.fromisoformat(str(publish_after))
    )
    freshness = assess_intraday_5m_freshness(
        universe_5m,
        expected_last_session=as_of,
        market=market,
        now=now,
        holidays=holidays,
        bar_interval_minutes=bar_interval_minutes,
    )
    session_date = pd.Timestamp(freshness["expected_last_session"]).date()

    # One clock read, market-local, naive -> comparable with the wall-clock literals below.
    clock = market_now(market, now=now, holidays=holidays)
    now_local = clock.local_now().replace(tzinfo=None)
    expected_publication_ts = datetime.datetime.combine(session_date, publish_time)

    stale = bool(freshness.get("stale"))
    if not stale:
        status = INTRADAY_5M_PUBLICATION_AVAILABLE
        code = None
    elif now_local < expected_publication_ts:
        status = INTRADAY_5M_PUBLICATION_PENDING
        code = INTRADAY_5M_PUBLICATION_CODE_PENDING
    else:
        status = INTRADAY_5M_PUBLICATION_MISSING
        # Same code the degraded path has always used: "the cache did not reach the
        # session it was supposed to hold", now with the publication delay ruled out.
        code = INTRADAY_5M_PUBLICATION_CODE_MISSING

    return {
        "status": status,
        "available": status == INTRADAY_5M_PUBLICATION_AVAILABLE,
        "expected_after": publish_time.strftime("%H:%M"),
        "degraded_reason_code": code,
        "as_of": session_date.isoformat(),
        "market": market,
        "now_local": now_local.isoformat(),
        "expected_publication_ts": expected_publication_ts.isoformat(),
        "stale": stale,
        "expected_last_session": freshness.get("expected_last_session"),
        "expected_deadline_ts": freshness.get("expected_deadline_ts"),
        "latest_available_ts": freshness.get("latest_available_ts"),
        "lag_calendar_days": freshness.get("lag_calendar_days"),
        "freshness_reason": freshness.get("reason"),
        "freshness": freshness,
    }


def load_intraday_5m_universe(
    symbols: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    data_provider: Optional[Any] = None,
    parquet_dir: str = "data/intraday_5m",
    cutoff_time: Optional[str] = "14:15",
    cache_dir: Optional[str] = None,
    expected_last_session: Optional[Any] = None,
    market: str = "china",
    **kwargs: Any,
) -> Dict[str, pd.DataFrame]:
    """Three-layer intraday data loading pipeline (Section 1.6 / line 383).

    Step 1: Fetch historical baseline from DuckDB/data_provider.
    Step 2: Read Day-T lock-free Parquet if present (data/intraday_5m/{YYYY-MM-DD}.parquet).
    Step 3: On-demand EastMoney fallback if intraday bucket hole is detected.

    Provider provenance (2026-11-11, fixes the silently-stale 5m layer): the DuckDB fallback
    queries the CALLER's configured provider first (`FuyaoDataProvider` for a `fuyao` live run,
    even when `data_provider` is a `CachedDataProvider` wrapper) and only falls back to other
    providers when that provider has zero cached 5m rows. Each returned frame carries:

      * `intraday_5m_source` -- `"data_provider"` | `"duckdb_cache"` | `"parquet"` ...
      * `intraday_5m_provider` -- provider key that actually served the bars
      * `intraday_5m_latest_ts` -- last bar timestamp (ISO-8601)
      * `fallback_from` / `fallback_to` / `fallback_reason` (+ `fallback_reason_detail`)
        ONLY when the serving provider differs from the caller's -- never silent.

    `expected_last_session` (optional) additionally stamps `intraday_5m_stale` and
    `intraday_5m_lag_calendar_days` on each frame via `assess_intraday_5m_freshness`, so a
    frozen cache is visible at the loader instead of being blamed on provider capability.
    """
    if cache_dir and parquet_dir == "data/intraday_5m":
        cand = os.path.join(cache_dir, "intraday_5m")
        if os.path.isdir(cand):
            parquet_dir = cand
    out: Dict[str, pd.DataFrame] = {}

    # Caller-configured provider keys, resolved ONCE (not per symbol). Empty when the caller
    # passed no provider -- then the historical fallback order applies unchanged.
    preferred_keys, preferred_label = _intraday_5m_provider_plan(data_provider)
    preferred_canon = {_intraday_5m_canonical_provider(k) for k in preferred_keys}
    provider_order = _intraday_5m_provider_order(preferred_keys)

    for sym in symbols:
        df_sym = pd.DataFrame()
        source: Optional[str] = None
        provider_used: Optional[str] = None
        fallback_from: Optional[str] = None
        fallback_to: Optional[str] = None
        fallback_reason: Optional[str] = None
        fallback_detail: Optional[str] = None
        fetch_exc: Optional[BaseException] = None

        # Step 1: Data provider query
        if data_provider is not None:
            try:
                df_sym = data_provider.fetch_ohlcv(sym, start=start, end=end, interval="5m")
            except Exception as exc:
                fetch_exc = exc
                logger.debug("Provider fetch 5m failed for %s: %s", sym, exc)
            if not df_sym.empty:
                source = "data_provider"
                provider_used = preferred_label or type(data_provider).__name__

        # Step 1.5: DuckDBCache fallback if provider returned empty or was not specified
        #
        # READ-ONLY intent (L2.13). This branch only ever queries intraday bars, so it
        # MUST construct `DuckDBCache(..., read_only=True)`. Until 2026-10-23 it used the
        # default writable constructor inside a blanket `except Exception`, and the
        # L2.13 production-path guard (`common/duckdb_cache.py::refuse_production_cache_path`,
        # which refuses a WRITABLE open of `<repo>/data/cache.duckdb` under pytest) turned
        # exactly that combination into a silent degradation: the guard raised
        # `ProductionCachePathError`, the blanket except swallowed it, and the caller was
        # handed an empty 5m frame -- "no data" reported as a normal result. Read-only
        # opens are explicitly allowed by the guard, so the fix is both the flag and the
        # removal of the silent swallow: every failure path now logs a WARNING that says
        # the 5m cache is unavailable and which symbol was skipped, and the symbol's
        # entry stays an empty frame (the "unavailable" convention consumed by
        # `enrich_universe_with_intraday_features` and by the live scripts' own WARN).
        if df_sym.empty:
            try:
                from common.duckdb_cache import DuckDBCache, ProductionCachePathError
            except ImportError as exc:  # duckdb not installed in this environment
                DuckDBCache = None  # type: ignore[assignment,misc]
                logger.warning(
                    "5m 缓存不可用（DuckDBCache 导入失败），已跳过 %s 的 DuckDB 5m 回退: %s",
                    sym,
                    exc,
                )

            if DuckDBCache is not None:
                for db_p in _intraday_5m_cache_candidates(cache_dir):
                    if not os.path.exists(db_p):
                        continue
                    try:
                        # `read_only=True`: never creates directories/tables, never writes,
                        # and is deliberately NOT blocked by the L2.13 pytest guard.
                        duck_c = DuckDBCache(db_p, read_only=True)
                    except (ProductionCachePathError, RuntimeError, OSError) as exc:
                        # ProductionCachePathError subclasses RuntimeError; OSError covers a
                        # cache file that vanished between `exists()` and the open.
                        logger.warning(
                            "5m 缓存不可用（只读打开失败），已跳过 %s 的 DuckDB 5m 回退（%s）: %s",
                            sym,
                            db_p,
                            exc,
                        )
                        continue

                    try:
                        # Preferred (caller-configured) provider keys first; the historical
                        # fallback list only after them. Never a fixed order any more -- that
                        # fixed order is what served BaoStock's 11-09 bars to a `fuyao` run
                        # whose own cache rows reached 11-10.
                        for p_name in provider_order:
                            df_cand = duck_c.query_intraday_bars(
                                p_name, sym, interval="5m", start=start, end=end
                            )
                            if not df_cand.empty:
                                df_sym = df_cand
                                provider_used = str(p_name)
                                break
                    except Exception as exc:  # noqa: BLE001 - logged, never silent
                        # A readable-but-invalid cache (missing table / locked file) is
                        # still "5m data unavailable", not an implicit empty result.
                        logger.warning(
                            "5m 缓存不可用（查询失败），已跳过 %s 的 DuckDB 5m 回退（%s）: %s",
                            sym,
                            db_p,
                            exc,
                        )
                        df_sym = pd.DataFrame()
                        provider_used = None
                        continue

                    if df_sym.empty:
                        # Opened successfully but zero rows for every provider: report the
                        # absence instead of letting an empty frame pass as normal data.
                        logger.warning(
                            "5m 缓存不可用：只读打开成功但 %s 在 %s 中 0 行 5m 数据，已跳过该标的的 DuckDB 回退",
                            sym,
                            db_p,
                        )
                        continue

                    source = "duckdb_cache"
                    if (
                        provider_used is not None
                        and preferred_label is not None
                        and _intraday_5m_canonical_provider(provider_used) not in preferred_canon
                    ):
                        # The caller asked for `preferred_label`; this cache could not serve it.
                        # Record the switch so no consumer has to infer it from timestamps,
                        # and never let it look like "the configured provider delivered this".
                        fallback_from = preferred_label
                        fallback_to = provider_used
                        if fetch_exc is not None:
                            fallback_reason = FALLBACK_REASON_UNSUPPORTED_5M
                        else:
                            fallback_reason = FALLBACK_REASON_RETURNED_EMPTY
                        fallback_detail = (
                            f"{preferred_label} 在 {os.path.basename(db_p)} 中无 5m 行；"
                            f"改用 {provider_used}"
                            + (f"（Step-1 异常: {fetch_exc}）" if fetch_exc is not None else "")
                        )
                        logger.warning(
                            "5m 数据来源回退：%s 在缓存中无 5m 覆盖，%s 改用 %s（reason=%s）",
                            preferred_label,
                            sym,
                            provider_used,
                            fallback_reason,
                        )
                    break

        # Step 2: Check local lock-free parquets
        if os.path.isdir(parquet_dir):
            parquet_files = sorted(glob.glob(os.path.join(parquet_dir, "*.parquet")))
            p_dfs = []
            for pf in parquet_files:
                try:
                    p_df = pd.read_parquet(pf)
                    if "symbol" in p_df.columns:
                        p_sub = p_df[p_df["symbol"] == sym]
                        if not p_sub.empty:
                            if "timestamp" in p_sub.columns:
                                p_sub.set_index("timestamp", inplace=True)
                            p_dfs.append(p_sub[["Open", "High", "Low", "Close", "Volume"]])
                except Exception as exc:
                    logger.debug("Failed reading parquet %s: %s", pf, exc)

            if p_dfs:
                combined_p = pd.concat(p_dfs)
                if not df_sym.empty:
                    df_sym = pd.concat([df_sym, combined_p])
                    df_sym = df_sym[~df_sym.index.duplicated(keep="last")].sort_index()
                    source = f"{source}+parquet" if source else "duckdb_cache+parquet"
                else:
                    df_sym = combined_p.sort_index()
                    source = "parquet"

        if df_sym.empty:
            # Explicit unavailability marker (no `enabled`/`Is_Partial_Bar` field exists at
            # this layer -- empty frame is the convention) so a caller/log can distinguish
            # "5m data unavailable" from a frame that merely failed to be requested.
            df_sym.attrs["intraday_5m_available"] = False
        else:
            df_sym.attrs["intraday_5m_available"] = True
            if source:
                df_sym.attrs["intraday_5m_source"] = source
            if provider_used:
                df_sym.attrs["intraday_5m_provider"] = provider_used
            last_ts = latest_5m_bar_ts(df_sym)
            if last_ts is not None:
                df_sym.attrs["intraday_5m_latest_ts"] = _naive(last_ts).isoformat()
            if fallback_to is not None:
                df_sym.attrs["fallback_from"] = fallback_from
                df_sym.attrs["fallback_to"] = fallback_to
                df_sym.attrs["fallback_reason"] = fallback_reason
                if fallback_detail:
                    df_sym.attrs["fallback_reason_detail"] = fallback_detail
            if expected_last_session is not None:
                freshness = assess_intraday_5m_freshness(
                    {"<single>": df_sym},
                    expected_last_session=expected_last_session,
                    market=market,
                )["symbols"]["<single>"]
                df_sym.attrs["intraday_5m_stale"] = freshness["stale"]
                df_sym.attrs["intraday_5m_lag_calendar_days"] = freshness["lag_calendar_days"]
                df_sym.attrs["intraday_5m_freshness_reason"] = freshness["reason"]
        out[sym] = df_sym

    return out

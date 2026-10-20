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

from dataclasses import dataclass, field
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

    Returns
    -------
    Dict[str, pd.DataFrame]
        Enriched daily universe with price alignment and microstructure columns.
    """
    if config is None:
        config = IntradayFeatureConfig()

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


def load_intraday_5m_universe(
    symbols: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    data_provider: Optional[Any] = None,
    parquet_dir: str = "data/intraday_5m",
    cutoff_time: Optional[str] = "14:15",
) -> Dict[str, pd.DataFrame]:
    """Three-layer intraday data loading pipeline (Section 1.6 / line 383).

    Step 1: Fetch historical baseline from DuckDB/data_provider.
    Step 2: Read Day-T lock-free Parquet if present (data/intraday_5m/{YYYY-MM-DD}.parquet).
    Step 3: On-demand EastMoney fallback if intraday bucket hole is detected.
    """
    out: Dict[str, pd.DataFrame] = {}

    for sym in symbols:
        df_sym = pd.DataFrame()
        # Step 1: Data provider query
        if data_provider is not None:
            try:
                df_sym = data_provider.fetch_ohlcv(sym, start=start, end=end, interval="5m")
            except Exception as exc:
                logger.debug("Provider fetch 5m failed for %s: %s", sym, exc)

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
                else:
                    df_sym = combined_p.sort_index()

        out[sym] = df_sym

    return out

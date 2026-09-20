"""Chan-Similar Trading Systems Strategies.

Translates the three medium-frequency swing trading theories from
`docs/chan_similar_trading.md` into production-ready `AllocationTemplate`
implementations:

1. `PriceActionBreakoutRetestStrategy` (pa_breakout_retest):
   Price Action Trading Range Breakout & Pullback/Retest Strategy (价格行为学：
   交易区间突破与二次回踩策略) -- identifies multi-week consolidation boxes, awaits
   decisive breakout with volume expansion, enters on low-volume retest holding
   above the box boundary, with stop-loss at box midpoint and 1.5x height profit target.
   Corresponds to Chan's 3rd Buy Point (第三类买点 B3).

2. `VolumeProfilePocMigrationStrategy` (vp_poc_migration):
   Volume Profile Point of Control Migration Strategy (筹码轮廓与控制点POC迁移策略)
   -- constructs rolling Volume Profile, identifies staircase upward migration of
   institutional cost consensus (POC), enters when price washes out/dips into POC or
   Value Area Low (VAL), and exits on excessive divergence or POC downward collapse.
   Corresponds to Chan's moving Zhongshu center (动态中枢引力).

3. `Wave3FibonacciStrategy` (wave3_fibonacci):
   Modified Elliott Wave 3 Fibonacci Golden Ratio Strategy (改良版波浪理论与黄金分割三浪主升策略)
   -- identifies impulsive Wave 1 advance, confirms low-volume Wave 2 correction into
   the 0.50 - 0.618 golden ratio pocket, triggers on corrective breakout, and targets
   the 1.618x Fibonacci expansion of Wave 1.
   Corresponds to Chan's main trend segment without divergence (同级别主升浪).

All strategies strictly follow repository conventions:
- AllocationTemplate base class
- Sparse target weights contract (NaN on non-rebalance days, explicit 0.0 on de-risk)
- Safe handling of absent Volume series (graceful fallback)
- Cell-level NaN vs 0.0 safety
"""

from typing import Dict, Optional, Tuple
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import sma
from .chan_advanced_strategies import (
    _aligned_master_index_helper,
    _get_risky_symbols_helper,
)
from .config import StrategyConfig


# ==============================================================================
# 1. Price Action Breakout Retest Helper & Strategy
# ==============================================================================

def run_pa_breakout_retest_single_symbol(
    bars: pd.DataFrame,
    box_window: int = 20,
    max_box_range_pct: float = 0.15,
    vol_contraction_ratio: float = 0.90,
    breakout_vol_mult: float = 1.25,
    retest_max_bars: int = 10,
    retest_tolerance: float = 0.03,
    stop_loss_pct: float = 0.06,
    take_profit_mult: float = 1.5,
    max_holding_days: int = 60,
    position_size_pct: float = 1.0,
) -> np.ndarray:
    """Stateful simulation of Price Action Breakout & Retest setup on single instrument."""
    n_bars = len(bars)
    weights = np.zeros(n_bars, dtype=float)
    if n_bars < box_window + 5:
        return weights

    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    open_ = bars["Open"].to_numpy(dtype=float) if "Open" in bars.columns else close

    has_volume = "Volume" in bars.columns and not bars["Volume"].isna().all()
    if has_volume:
        vol = bars["Volume"].fillna(0.0).to_numpy(dtype=float)
        vol_s = pd.Series(vol)
        vol_sma20 = vol_s.rolling(20, min_periods=1).mean().to_numpy()
        vol_sma_box = vol_s.rolling(box_window, min_periods=1).mean().to_numpy()
        vol_sma_long = vol_s.rolling(box_window * 2, min_periods=1).mean().to_numpy()
    else:
        vol = np.ones(n_bars)
        vol_sma20 = np.ones(n_bars)
        vol_sma_box = np.ones(n_bars)
        vol_sma_long = np.ones(n_bars)

    in_position = False
    entry_idx = 0
    entry_price = 0.0
    active_target = 0.0
    active_stop = 0.0

    breakout_pending = False
    breakout_idx = 0
    b_high = 0.0
    b_low = 0.0
    b_mid = 0.0

    for t in range(box_window * 2, n_bars):
        if in_position:
            held = t - entry_idx
            is_take_profit = close[t] >= active_target
            is_stop_loss = (close[t] <= active_stop) or (close[t] / entry_price - 1.0 <= -stop_loss_pct)
            is_timeout = held >= max_holding_days

            if is_take_profit or is_stop_loss or is_timeout:
                in_position = False
                weights[t] = 0.0
            else:
                weights[t] = position_size_pct
            continue

        # Check existing pending breakout for retest
        if breakout_pending:
            bars_since = t - breakout_idx
            if bars_since > retest_max_bars:
                breakout_pending = False
            else:
                # Retest condition: low dips near box high, holds above box mid
                touched_retest = low[t] <= b_high * (1.0 + retest_tolerance)
                holds_above_mid = (low[t] >= b_mid) and (close[t] >= b_high * (1.0 - retest_tolerance))
                vol_ok = (not has_volume) or (vol[t] <= vol_sma20[t] * 1.25)
                rejection = (close[t] > open_[t]) or (close[t] >= close[t - 1])

                if touched_retest and holds_above_mid and vol_ok and rejection:
                    in_position = True
                    entry_idx = t
                    entry_price = close[t]
                    active_stop = max(b_mid, entry_price * (1.0 - stop_loss_pct))
                    box_height = b_high - b_low
                    active_target = b_high + take_profit_mult * box_height
                    weights[t] = position_size_pct
                    breakout_pending = False
                    continue

        # Check for new consolidation box and breakout
        box_slice_high = high[t - box_window : t]
        box_slice_low = low[t - box_window : t]
        curr_b_high = float(np.max(box_slice_high))
        curr_b_low = float(np.min(box_slice_low))

        if curr_b_low > 0:
            box_range = (curr_b_high - curr_b_low) / curr_b_low
        else:
            box_range = 1.0

        is_consolidated = box_range <= max_box_range_pct
        vol_contracted = (not has_volume) or (vol_sma_box[t - 1] <= vol_sma_long[t - 1] * vol_contraction_ratio + 1e-6)

        if is_consolidated and vol_contracted:
            # Check breakout on current bar t
            vol_expanded = (not has_volume) or (vol[t] >= vol_sma20[t] * breakout_vol_mult)
            if close[t] > curr_b_high and vol_expanded:
                breakout_pending = True
                breakout_idx = t
                b_high = curr_b_high
                b_low = curr_b_low
                b_mid = (b_high + b_low) / 2.0

        weights[t] = 0.0

    return weights


class PriceActionBreakoutRetestStrategy(AllocationTemplate):
    """Price Action Trading Range Breakout & Pullback/Retest Strategy (价格行为学：交易区间突破与二次回踩策略).

    Based on `docs/chan_similar_trading.md` Strategy 1:
    - Identifies multi-week consolidation boxes (>4 weeks) with volume contraction.
    - Waits for decisive breakout above the box upper boundary with volume expansion.
    - Waits for low-volume pullback/retest rejecting decline at the prior box top.
    - Enters with stop-loss at box midpoint/lower boundary and profit target at 1.5x box height.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="pa_breakout_retest",
            param_grid={},
            factor_tags=["regime_trend_strength", "absolute_momentum_trend"],
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        box_window = p.get("pabr_box_window", getattr(cfg, "pabr_box_window", 20))
        max_box_range_pct = p.get("pabr_max_box_range_pct", getattr(cfg, "pabr_max_box_range_pct", 0.15))
        vol_contraction_ratio = p.get("pabr_vol_contraction_ratio", getattr(cfg, "pabr_vol_contraction_ratio", 0.90))
        breakout_vol_mult = p.get("pabr_breakout_vol_mult", getattr(cfg, "pabr_breakout_vol_mult", 1.25))
        retest_max_bars = p.get("pabr_retest_max_bars", getattr(cfg, "pabr_retest_max_bars", 10))
        retest_tolerance = p.get("pabr_retest_tolerance", getattr(cfg, "pabr_retest_tolerance", 0.03))
        stop_loss_pct = p.get("pabr_stop_loss_pct", getattr(cfg, "pabr_stop_loss_pct", 0.06))
        take_profit_mult = p.get("pabr_take_profit_mult", getattr(cfg, "pabr_take_profit_mult", 1.5))
        max_holding_days = p.get("pabr_max_holding_days", getattr(cfg, "pabr_max_holding_days", 60))
        position_size_pct = p.get("pabr_position_size_pct", getattr(cfg, "pabr_position_size_pct", 1.0))

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym].reindex(master_index)
            raw_weights[sym] = run_pa_breakout_retest_single_symbol(
                bars,
                box_window=box_window,
                max_box_range_pct=max_box_range_pct,
                vol_contraction_ratio=vol_contraction_ratio,
                breakout_vol_mult=breakout_vol_mult,
                retest_max_bars=retest_max_bars,
                retest_tolerance=retest_tolerance,
                stop_loss_pct=stop_loss_pct,
                take_profit_mult=take_profit_mult,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        return (
            "Price Action Breakout & Retest Strategy (价格行为学：交易区间突破与二次回踩): "
            "identifies consolidation boxes (>4 weeks) with contracting volume, awaits volume-confirmed breakout, "
            "enters on low-volume retest holding above prior box top; exits on box midpoint stop or 1.5x target."
        )

    def warmup_bars(self, params: dict = None) -> int:
        p = params or {}
        box_window = p.get("pabr_box_window", getattr(self.config, "pabr_box_window", 20))
        return box_window * 2 + 10


# ==============================================================================
# 2. Volume Profile POC Migration Helper & Strategy
# ==============================================================================

def compute_rolling_poc_series(
    bars: pd.DataFrame,
    lookback: int = 60,
    n_bins: int = 30,
    value_area_pct: float = 0.70,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Computes rolling Point of Control (POC), Value Area Low (VAL), and Value Area High (VAH)."""
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


def run_vp_poc_migration_single_symbol(
    bars: pd.DataFrame,
    lookback: int = 60,
    n_bins: int = 30,
    value_area_pct: float = 0.70,
    poc_step_lookback: int = 20,
    min_step_pct: float = 0.015,
    dip_tolerance: float = 0.03,
    max_divergence_pct: float = 0.18,
    poc_collapse_pct: float = 0.04,
    stop_loss_pct: float = 0.06,
    max_holding_days: int = 60,
    position_size_pct: float = 1.0,
) -> np.ndarray:
    """Stateful simulation of Volume Profile POC Migration Strategy on single instrument."""
    n_bars = len(bars)
    weights = np.zeros(n_bars, dtype=float)
    warmup = lookback + poc_step_lookback * 2
    if n_bars < warmup:
        return weights

    poc, val, vah = compute_rolling_poc_series(bars, lookback=lookback, n_bins=n_bins, value_area_pct=value_area_pct)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)

    in_position = False
    entry_idx = 0
    entry_price = 0.0

    for t in range(warmup, n_bars):
        if np.isnan(poc[t]) or np.isnan(poc[t - poc_step_lookback]) or np.isnan(poc[t - poc_step_lookback * 2]):
            weights[t] = 0.0
            continue

        curr_poc = poc[t]
        prev_poc = poc[t - poc_step_lookback]
        prior_poc = poc[t - poc_step_lookback * 2]

        # Exit logic
        if in_position:
            held = t - entry_idx
            divergence = (close[t] - curr_poc) / curr_poc
            poc_collapse = curr_poc < prev_poc * (1.0 - poc_collapse_pct)
            is_stopped = (close[t] / entry_price - 1.0 <= -stop_loss_pct) or (close[t] < val[t] * (1.0 - stop_loss_pct))
            is_take_profit = divergence >= max_divergence_pct
            is_timeout = held >= max_holding_days

            if is_take_profit or poc_collapse or is_stopped or is_timeout:
                in_position = False
                weights[t] = 0.0
            else:
                weights[t] = position_size_pct
            continue

        # Check upward staircase migration: POC_t > POC_{t-20} > POC_{t-40}
        poc_stepping_up = (curr_poc > prev_poc * (1.0 + min_step_pct)) and (prev_poc >= prior_poc)

        if poc_stepping_up:
            # Washout dip to POC or VAL zone: low reaches POC +- tolerance, holds around VAL/POC
            dips_to_poc = low[t] <= curr_poc * (1.0 + dip_tolerance)
            holds_support = close[t] >= val[t] * 0.98 and close[t] >= curr_poc * 0.97
            stabilized = close[t] > low[t]

            if dips_to_poc and holds_support and stabilized:
                in_position = True
                entry_idx = t
                entry_price = close[t]
                weights[t] = position_size_pct
                continue

        weights[t] = 0.0

    return weights


class VolumeProfilePocMigrationStrategy(AllocationTemplate):
    """Volume Profile Point of Control Migration Strategy (筹码轮廓与控制点POC迁移策略).

    Based on `docs/chan_similar_trading.md` Strategy 2:
    - Computes rolling Volume Profile across a 3-month lookback (~60 bars).
    - Filters stocks where the Point of Control (POC - institutional cost consensus) exhibits
      staircase upward migration across monthly snapshots.
    - Accumulates when price washes out into the high-volume support zone (POC or Value Area Low).
    - Takes profit on excessive bias (+18% above POC) or exits if POC collapses downward.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="vp_poc_migration",
            param_grid={},
            factor_tags=["absolute_momentum_trend", "relative_momentum"],
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        lookback = p.get("vp_lookback", getattr(cfg, "vp_lookback", 60))
        n_bins = p.get("vp_n_bins", getattr(cfg, "vp_n_bins", 30))
        value_area_pct = p.get("vp_value_area_pct", getattr(cfg, "vp_value_area_pct", 0.70))
        poc_step_lookback = p.get("vp_poc_step_lookback", getattr(cfg, "vp_poc_step_lookback", 20))
        min_step_pct = p.get("vp_min_step_pct", getattr(cfg, "vp_min_step_pct", 0.015))
        dip_tolerance = p.get("vp_dip_tolerance", getattr(cfg, "vp_dip_tolerance", 0.03))
        max_divergence_pct = p.get("vp_max_divergence_pct", getattr(cfg, "vp_max_divergence_pct", 0.18))
        poc_collapse_pct = p.get("vp_poc_collapse_pct", getattr(cfg, "vp_poc_collapse_pct", 0.04))
        stop_loss_pct = p.get("vp_stop_loss_pct", getattr(cfg, "vp_stop_loss_pct", 0.06))
        max_holding_days = p.get("vp_max_holding_days", getattr(cfg, "vp_max_holding_days", 60))
        position_size_pct = p.get("vp_position_size_pct", getattr(cfg, "vp_position_size_pct", 1.0))

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym].reindex(master_index)
            raw_weights[sym] = run_vp_poc_migration_single_symbol(
                bars,
                lookback=lookback,
                n_bins=n_bins,
                value_area_pct=value_area_pct,
                poc_step_lookback=poc_step_lookback,
                min_step_pct=min_step_pct,
                dip_tolerance=dip_tolerance,
                max_divergence_pct=max_divergence_pct,
                poc_collapse_pct=poc_collapse_pct,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        return (
            "Volume Profile POC Migration Strategy (筹码轮廓与控制点POC迁移): "
            "tracks institutional cost floor via rolling 60-day Volume Profile POC staircase migration; "
            "accumulates on washouts to POC/VAL, takes profit on +18% divergence or exits on POC downward collapse."
        )

    def warmup_bars(self, params: dict = None) -> int:
        p = params or {}
        lookback = p.get("vp_lookback", getattr(self.config, "vp_lookback", 60))
        poc_step_lookback = p.get("vp_poc_step_lookback", getattr(self.config, "vp_poc_step_lookback", 20))
        return lookback + poc_step_lookback * 2 + 5


# ==============================================================================
# 3. Wave 3 Fibonacci Strategy Helper & Strategy
# ==============================================================================

def run_wave3_fibonacci_single_symbol(
    bars: pd.DataFrame,
    pivot_window: int = 5,
    min_wave1_pct: float = 0.08,
    fibo_min_retrace: float = 0.45,
    fibo_max_retrace: float = 0.65,
    vol_contraction_ratio: float = 0.70,
    fibo_extension: float = 1.618,
    stop_buffer_pct: float = 0.02,
    stop_loss_pct: float = 0.06,
    max_holding_days: int = 65,
    position_size_pct: float = 1.0,
) -> np.ndarray:
    """Stateful simulation of Wave 3 Fibonacci Golden Ratio Strategy on single instrument."""
    n_bars = len(bars)
    weights = np.zeros(n_bars, dtype=float)
    if n_bars < 60:
        return weights

    high = bars["High"].to_numpy(dtype=float)
    low = bars["Low"].to_numpy(dtype=float)
    close = bars["Close"].to_numpy(dtype=float)
    open_ = bars["Open"].to_numpy(dtype=float) if "Open" in bars.columns else close

    has_volume = "Volume" in bars.columns and not bars["Volume"].isna().all()
    vol = bars["Volume"].fillna(1.0).to_numpy(dtype=float) if has_volume else np.ones(n_bars)
    vol_sma20 = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()
    close_sma60 = pd.Series(close).rolling(60, min_periods=1).mean().to_numpy()

    in_position = False
    entry_idx = 0
    entry_price = 0.0
    active_target = 0.0
    active_stop = 0.0

    # Structural wave tracking
    p0 = 0.0  # Wave 1 start price (trough)
    p1 = 0.0  # Wave 1 end price (peak)
    p2 = 0.0  # Wave 2 bottom price
    t1 = -1   # Wave 1 peak index
    wave1_active = False

    for t in range(pivot_window * 2 + 1, n_bars):
        if in_position:
            held = t - entry_idx
            is_take_profit = close[t] >= active_target
            is_stopped = (close[t] <= active_stop) or (close[t] / entry_price - 1.0 <= -stop_loss_pct)
            is_timeout = held >= max_holding_days

            if is_take_profit or is_stopped or is_timeout:
                in_position = False
                weights[t] = 0.0
            else:
                weights[t] = position_size_pct
            continue

        # Look for completed Wave 1 peaks at bar t - pivot_window
        cand_peak_idx = t - pivot_window
        is_pivot_peak = high[cand_peak_idx] == np.max(high[cand_peak_idx - pivot_window : cand_peak_idx + pivot_window + 1])

        if is_pivot_peak and close[cand_peak_idx] > close_sma60[cand_peak_idx]:
            # Find prior trough in the 40 bars before peak
            start_search = max(0, cand_peak_idx - 40)
            cand_trough_idx = start_search + int(np.argmin(low[start_search:cand_peak_idx]))
            cand_p0 = low[cand_trough_idx]
            cand_p1 = high[cand_peak_idx]
            h1 = cand_p1 - cand_p0

            if cand_p0 > 0 and (h1 / cand_p0 >= min_wave1_pct):
                p0 = cand_p0
                p1 = cand_p1
                t1 = cand_peak_idx
                p2 = p1
                wave1_active = True

        # Check Wave 2 retracement & Wave 3 trigger
        if wave1_active and t > t1:
            h1 = p1 - p0
            retrace = (p1 - low[t]) / h1 if h1 > 0 else 0.0

            # Invalidation: Wave 2 cannot drop below Wave 1 origin
            if low[t] < p0:
                wave1_active = False
                weights[t] = 0.0
                continue

            # Update Wave 2 low
            if low[t] < p2:
                p2 = low[t]

            # In golden pocket (0.45 - 0.65)
            in_golden_pocket = fibo_min_retrace <= retrace <= fibo_max_retrace
            vol_dry = (not has_volume) or (vol[t] <= vol_sma20[t1] * vol_contraction_ratio + 1e-6)

            # Trigger on bullish breakout of short-term corrective high
            corrective_break = (
                close[t] > open_[t]
                and close[t] > close[t - 1]
                and close[t] > np.max(close[max(t1, t - 3) : t])
            )

            if in_golden_pocket and vol_dry and corrective_break:
                in_position = True
                entry_idx = t
                entry_price = close[t]
                active_target = p2 + fibo_extension * h1
                active_stop = max(p2 * (1.0 - stop_buffer_pct), entry_price * (1.0 - stop_loss_pct))
                weights[t] = position_size_pct
                wave1_active = False
                continue

        weights[t] = 0.0

    return weights


class Wave3FibonacciStrategy(AllocationTemplate):
    """Modified Elliott Wave 3 Fibonacci Golden Ratio Strategy (改良版波浪理论与黄金分割三浪主升策略).

    Based on `docs/chan_similar_trading.md` Strategy 3:
    - Identifies a moderate Wave 1 impulse advance from structural low to high.
    - Confirms Wave 2 corrective retracement holding into the 0.50 - 0.618 golden ratio pocket with drying volume.
    - Enters long upon bullish stabilization breaking above the short-term corrective line.
    - Sets primary take-profit target at 1.618x Wave 1 height with stop loss strictly below the Wave 2 trough.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="wave3_fibonacci",
            param_grid={},
            factor_tags=["absolute_momentum_trend", "relative_momentum", "volatility_targeting"],
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        pivot_window = p.get("w3_pivot_window", getattr(cfg, "w3_pivot_window", 5))
        min_wave1_pct = p.get("w3_min_wave1_pct", getattr(cfg, "w3_min_wave1_pct", 0.08))
        fibo_min_retrace = p.get("w3_fibo_min_retrace", getattr(cfg, "w3_fibo_min_retrace", 0.45))
        fibo_max_retrace = p.get("w3_fibo_max_retrace", getattr(cfg, "w3_fibo_max_retrace", 0.65))
        vol_contraction_ratio = p.get("w3_vol_contraction_ratio", getattr(cfg, "w3_vol_contraction_ratio", 0.70))
        fibo_extension = p.get("w3_fibo_extension", getattr(cfg, "w3_fibo_extension", 1.618))
        stop_buffer_pct = p.get("w3_stop_buffer_pct", getattr(cfg, "w3_stop_buffer_pct", 0.02))
        stop_loss_pct = p.get("w3_stop_loss_pct", getattr(cfg, "w3_stop_loss_pct", 0.06))
        max_holding_days = p.get("w3_max_holding_days", getattr(cfg, "w3_max_holding_days", 65))
        position_size_pct = p.get("w3_position_size_pct", getattr(cfg, "w3_position_size_pct", 1.0))

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym].reindex(master_index)
            raw_weights[sym] = run_wave3_fibonacci_single_symbol(
                bars,
                pivot_window=pivot_window,
                min_wave1_pct=min_wave1_pct,
                fibo_min_retrace=fibo_min_retrace,
                fibo_max_retrace=fibo_max_retrace,
                vol_contraction_ratio=vol_contraction_ratio,
                fibo_extension=fibo_extension,
                stop_buffer_pct=stop_buffer_pct,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        return (
            "Wave 3 Fibonacci Strategy (改良版波浪理论与黄金分割三浪主升): "
            "identifies Wave 1 impulse, enters on Wave 2 low-volume retracement into 0.50-0.618 golden pocket; "
            "targets 1.618x Fibonacci Wave 3 extension with stop loss strictly below Wave 2 trough."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 65

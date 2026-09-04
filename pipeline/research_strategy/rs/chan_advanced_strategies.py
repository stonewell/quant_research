"""Chan Theory Advanced & Compound Quantitative Trading Strategies.

This module implements 5 specialized Chan-theory (缠中说禅) trading strategies:

1. `ChanMultiTimeframeTrendStrategy`: Multi-timeframe trend gate (e.g. 200d SMA) +
   stroke/segment-level structural buy points, a genuine weekly-level 区间套
   (interval-nesting) re-confirmation, and Lesson 107's precise-trend gate
   (sizes down when the rally isn't a confirmed non-divergent B3).
2. `ChanTrendThirdBuyStrategy`: Focused on High-Momentum 3rd Buy Point (B3 - pivot
   breakout retest, 第三类买卖点突破回踩).
3. `ChanMeanReversionDivergenceStrategy`: Focused on 1st Buy Point (B1 - MACD divergence
   bottom-fishing, 一类买卖点背驰), gated by Lesson 103's actual "防狼术" MACD
   zero-axis-reclaim rule, plus tight risk controls.
4. `ChanCompositeStrategy`: Multi-stage position scaling across buy point types
   (30% on B1, +40% on B2, +30% on B3, 一二三类买点动态组合建仓), with a
   weighted-average cost basis on scale-ins and Lessons 92-99's dangerous
   pivot-relation state as an added risk-brake overlay.
5. `ChanBestSelectorStrategy`: Compound meta-strategy running all Chan strategies in
   parallel, tracking rolling performance (e.g. trailing Sharpe/return over 63d),
   and dynamically routing allocation to the best-performing strategy.

All strategies inherit from `AllocationTemplate` and maximize code reuse from
`rs/chan_structure.py`, `rs/chan_signals.py`, and `common.position_exits`.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import macd, roc, sma
from common.position_exits import run_stop_timeout_exit
from common.scheduling import get_rebalance_dates as _get_rebalance_dates

from .chan_structure import (
    build_pivots,
    build_strokes,
    classify_pivot_relations,
    compute_chan_signals,
    find_fractals,
    merge_inclusion,
)
from .chan_signals import compute_chan3_signals, compute_chan_pivot_macd_signals
from .config import StrategyConfig


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


def _macd_zero_axis_confirmed(close: pd.Series, macd_fast: int, macd_slow: int, macd_signal: int) -> pd.Series:
    """Lesson 103's actual '防狼术' (0952-486e105c01008pri-103.md): MACD's DIF/
    DEA lines (黄白线) below the zero axis mark a bear-dominated regime the
    lesson says to avoid entirely -- "回避所有MACD黄白线在0轴下面的市场或股票" --
    and only re-enter once it "重新站住0轴" (re-stands on the zero axis).
    Trusts a bottom-fishing entry only once both lines have reclaimed
    (`>= 0`) the zero axis."""
    macd_df = macd(close, macd_fast, macd_slow, macd_signal)
    return ((macd_df["macd"] >= 0) & (macd_df["signal"] >= 0)).fillna(False)


def _weekly_regime_state(bars: pd.DataFrame, min_gap_bars: int, min_strokes: int) -> pd.Series:
    """Genuine 区间套 (interval-nesting, Lessons 027/030): resamples to weekly
    bars, runs the same B1/B2/B3 structural detection at that slower period,
    and derives a persistent 'weekly bullish regime' -- True from the bar a
    weekly buy point fires until the next weekly sell point. Reindexed
    as-of (union + ffill) so a day within a still-forming week correctly
    inherits the PRIOR completed week's regime (that week's own Friday-dated
    bar doesn't exist in the index yet), never looking ahead into a
    not-yet-computed current week.

    Simplification (disclosed): this is a 2-level (daily/weekly) nesting,
    not the full recursive daily->30min/5min cascade the lessons illustrate
    with real intraday data -- this workspace only carries daily OHLCV.
    """
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in bars.columns:
        agg["Volume"] = "sum"
    weekly_bars = bars.resample("W-FRI").agg(agg).dropna(subset=["Close"])
    if len(weekly_bars) < min_strokes * 2:
        return pd.Series(False, index=bars.index)

    weekly_sig = compute_chan3_signals(weekly_bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
    weekly_buy = weekly_sig["buy_signal"].fillna(False).to_numpy()
    weekly_sell = weekly_sig["sell_signal"].fillna(False).to_numpy()

    state = False
    regime = np.zeros(len(weekly_bars), dtype=bool)
    for i in range(len(weekly_bars)):
        if weekly_buy[i]:
            state = True
        elif weekly_sell[i]:
            state = False
        regime[i] = state
    weekly_regime = pd.Series(regime, index=weekly_bars.index)

    combined_index = bars.index.union(weekly_regime.index)
    return weekly_regime.reindex(combined_index).ffill().fillna(False).reindex(bars.index).ffill().fillna(False)


def _precise_trend_confirmed(sig: pd.DataFrame) -> pd.Series:
    """Lesson 107's precise trend definition (1092-...-107.md): 'hold and
    sleep' only once a pivot has produced a genuine, non-divergent 3rd-buy
    (B3) rally -- otherwise always treat the move as pivot oscillation.
    Becomes True on a `third_buy`, False on any sell-point classification,
    else persists."""
    third_buy = sig["third_buy"].to_numpy()
    any_sell = sig["sell_signal"].to_numpy()
    state = np.zeros(len(third_buy), dtype=bool)
    cur = False
    for i in range(len(third_buy)):
        if third_buy[i]:
            cur = True
        elif any_sell[i]:
            cur = False
        state[i] = cur
    return pd.Series(state, index=sig.index)


def _run_variable_size_stop_timeout_exit(
    close: pd.Series | np.ndarray,
    entry_signal: pd.Series | np.ndarray,
    exit_signal: pd.Series | np.ndarray,
    stop_loss_pct: Optional[float],
    max_holding_days: Optional[int],
    size_at_entry: pd.Series | np.ndarray,
) -> np.ndarray:
    """Same stateful loop as `common.position_exits.run_stop_timeout_exit`,
    but the position size is chosen AT ENTRY from a per-bar `size_at_entry`
    array (e.g. full size only once Lesson 107's precise trend gate has
    confirmed, a reduced size otherwise) rather than one constant for every
    trade -- kept local rather than changing the shared helper, since every
    other caller wants one fixed size for the whole trade."""
    close_arr = np.asarray(close)
    entry_arr = np.asarray(entry_signal)
    exit_arr = np.asarray(exit_signal)
    size_arr = np.asarray(size_at_entry)
    n_bars = len(close_arr)
    raw = np.zeros(n_bars)
    in_position, entry_idx = False, 0
    current_size = 0.0

    for i in range(n_bars):
        if in_position:
            held = i - entry_idx
            stopped = stop_loss_pct is not None and (close_arr[i] / close_arr[entry_idx] - 1) <= -stop_loss_pct
            timed_out = max_holding_days is not None and held >= max_holding_days
            if exit_arr[i] or stopped or timed_out:
                in_position = False
                raw[i] = 0.0
            else:
                raw[i] = current_size
        elif entry_arr[i]:
            in_position = True
            entry_idx = i
            current_size = size_arr[i]
            raw[i] = current_size

    return raw


def _pivot_relation_danger_series(bars: pd.DataFrame, min_gap_bars: int, min_strokes: int) -> pd.Series:
    """Per-bar overlay of Lessons 92-99's dangerous `(dir, 1)` pivot-relation
    state (`chan_structure.classify_pivot_relations`, computed here on the
    stroke-level pivots -- the same pivot notion the lessons' relation
    notation itself operates on): True from the bar a pivot confirms as
    'broke through against the trend' until superseded by the next pivot's
    own relation. A shared risk overlay usable by any strategy holding
    `bars`, not a standalone signal of its own."""
    merged = merge_inclusion(bars)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    pivots = build_pivots(strokes, min_strokes)
    relations = classify_pivot_relations(pivots)

    danger = pd.Series(False, index=bars.index)
    if relations.empty:
        return danger

    state_series = pd.Series(False, index=merged.index)
    rel_by_pivot = {int(r["pivot_idx"]): not bool(r["contained"]) for _, r in relations.iterrows()}
    for k in sorted(rel_by_pivot):
        confirm_pos = int(pivots.iloc[k]["end_pos"]) + 1
        if confirm_pos < len(merged):
            state_series.iloc[confirm_pos:] = rel_by_pivot[k]

    combined_index = bars.index.union(state_series.index)
    return state_series.reindex(combined_index).ffill().fillna(False).reindex(bars.index).ffill().fillna(False)


def run_mrd_position_exit(
    close: pd.Series | np.ndarray,
    entry_signal: pd.Series | np.ndarray,
    exit_signal: pd.Series | np.ndarray,
    stop_loss_pct: Optional[float] = 0.05,
    profit_target_pct: Optional[float] = 0.15,
    trailing_stop_pct: Optional[float] = 0.04,
    trailing_activate_pct: Optional[float] = 0.08,
    max_holding_days: Optional[int] = 45,
    position_size_pct: float = 1.0,
) -> np.ndarray:
    """Stateful position exit loop for Chan Mean-Reversion Divergence (B1)
    incorporating "防狼术" (Lesson 103) tight risk controls:
    - Tight stop-loss
    - Quick profit target
    - Trailing stop after activation threshold
    - Max holding period timeout
    """
    close_arr = np.asarray(close)
    entry_arr = np.asarray(entry_signal)
    exit_arr = np.asarray(exit_signal)
    n_bars = len(close_arr)
    raw = np.zeros(n_bars)
    in_position, entry_idx = False, 0
    highest_price = 0.0

    for i in range(n_bars):
        if in_position:
            held = i - entry_idx
            p = close_arr[i]
            entry_p = close_arr[entry_idx]
            ret = p / entry_p - 1.0 if entry_p > 0 else 0.0

            if p > highest_price:
                highest_price = p

            stopped = stop_loss_pct is not None and ret <= -stop_loss_pct
            profit_hit = profit_target_pct is not None and ret >= profit_target_pct
            timed_out = max_holding_days is not None and held >= max_holding_days

            peak_ret = highest_price / entry_p - 1.0 if entry_p > 0 else 0.0
            trail_activated = trailing_activate_pct is None or peak_ret >= trailing_activate_pct
            trail_hit = (
                trailing_stop_pct is not None
                and trail_activated
                and highest_price > 0
                and (p / highest_price - 1.0) <= -trailing_stop_pct
            )

            if exit_arr[i] or stopped or profit_hit or timed_out or trail_hit:
                in_position = False
                raw[i] = 0.0
            else:
                raw[i] = position_size_pct
        elif entry_arr[i]:
            in_position = True
            entry_idx = i
            highest_price = close_arr[i]
            raw[i] = position_size_pct

    return raw


def run_composite_position_loop(
    close: pd.Series | np.ndarray,
    first_buy: pd.Series | np.ndarray,
    second_buy: pd.Series | np.ndarray,
    third_buy: pd.Series | np.ndarray,
    sell_signal: pd.Series | np.ndarray,
    b1_w: float = 0.30,
    b2_w: float = 0.40,
    b3_w: float = 0.30,
    stop_loss_pct: Optional[float] = 0.08,
    max_holding_days: Optional[int] = 90,
) -> np.ndarray:
    """Stateful position scaling loop for Chan Composite strategy:
    - B1 (first_buy): opens the position (b1_w). B2/B3 while flat are not
      actionable -- only B1 may open a new position.
    - B2 (second_buy): add allocation (+b2_w), once already open
    - B3 (third_buy): add allocation (+b3_w), once already open
    - Sell signal or stop-loss / timeout: clear back to 0.0

    `entry_price` is a weighted-average cost basis, updated on every B2/B3
    scale-in (`new_price = (old_price*old_weight + fill_price*added_weight)
    / new_weight`) so the stop-loss is measured against the position's real
    blended cost, not just the original B1 fill. `entry_idx` (holding-period
    timeout) deliberately stays at the ORIGINAL B1 bar -- position age is
    measured from when the thesis first opened, not reset on each add-on.
    """
    close_arr = np.asarray(close)
    b1_arr = np.asarray(first_buy)
    b2_arr = np.asarray(second_buy)
    b3_arr = np.asarray(third_buy)
    sell_arr = np.asarray(sell_signal)
    n = len(close_arr)
    raw = np.zeros(n)

    current_weight = 0.0
    entry_price = 0.0
    entry_idx = 0

    for i in range(n):
        if current_weight > 0.0:
            held = i - entry_idx
            p = close_arr[i]
            ret = p / entry_price - 1.0 if entry_price > 0 else 0.0

            stopped = stop_loss_pct is not None and ret <= -stop_loss_pct
            timed_out = max_holding_days is not None and held >= max_holding_days

            if sell_arr[i] or stopped or timed_out:
                current_weight = 0.0
                raw[i] = 0.0
                continue

            if b2_arr[i] and current_weight < b1_w + b2_w:
                new_weight = min(1.0, current_weight + b2_w)
                added = new_weight - current_weight
                entry_price = (entry_price * current_weight + p * added) / new_weight
                current_weight = new_weight
            elif b3_arr[i] and current_weight < 1.0:
                new_weight = min(1.0, current_weight + b3_w)
                added = new_weight - current_weight
                entry_price = (entry_price * current_weight + p * added) / new_weight
                current_weight = new_weight

            raw[i] = current_weight
        else:
            if b1_arr[i]:
                current_weight = b1_w
                entry_price = close_arr[i]
                entry_idx = i
                raw[i] = current_weight
            # B2/B3 do nothing while flat -- the strategy's own staged design
            # (30% B1 -> +40% B2 -> +30% B3) only ever OPENS a position on
            # B1; a lone B2/B3 signal with no prior B1 is not actionable.

    return raw


class ChanMultiTimeframeTrendStrategy(AllocationTemplate):
    """Chan Multi-Timeframe Trend Strategy (区间套与趋势共振策略):
    Combines a macro trend gate (e.g. 200-day SMA) with sub-period structural Chan
    buy signals (B1/B2/B3), a genuine weekly-level 区间套 (interval-nesting,
    Lessons 027/030) structural re-confirmation, and Lesson 107's precise trend
    definition (一旦形成有效的三买 non-divergent B3 rally -- else treat as pivot
    oscillation and size down). Entries trigger only when the daily Chan buy
    signal, the macro SMA trend gate, AND the weekly-level structure all agree;
    full position size is only used once the precise-trend gate has confirmed, a
    reduced size otherwise.

    Simplification (disclosed): the weekly re-confirmation is a 2-level
    (daily/weekly) nesting, not the full recursive daily->30min cascade the
    lessons illustrate with real intraday data -- this workspace only carries
    daily OHLCV.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_mtf_trend", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        trend_ma_period = p.get("chan_mtf_trend_ma_period", cfg.chan_mtf_trend_ma_period)
        min_gap_bars = p.get("chan_mtf_min_gap_bars", cfg.chan_mtf_min_gap_bars)
        min_strokes = p.get("chan_mtf_min_strokes", cfg.chan_mtf_min_strokes)
        stop_loss_pct = p.get("chan_mtf_stop_loss_pct", cfg.chan_mtf_stop_loss_pct)
        max_holding_days = p.get("chan_mtf_max_holding_days", cfg.chan_mtf_max_holding_days)
        position_size_pct = p.get("chan_mtf_position_size_pct", cfg.chan_mtf_position_size_pct)
        pivot_osc_size_pct = p.get("chan_mtf_pivot_osc_size_pct", cfg.chan_mtf_pivot_osc_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            close = bars["Close"].reindex(master_index)
            ma = sma(close, trend_ma_period)
            macro_trend_gate = (close > ma).fillna(False)

            sig = compute_chan3_signals(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
            chan_buy = sig["buy_signal"].reindex(master_index).fillna(False)
            chan_sell = sig["sell_signal"].reindex(master_index).fillna(False)

            weekly_regime = _weekly_regime_state(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)
            trend_confirmed = _precise_trend_confirmed(sig).reindex(master_index).ffill().fillna(False)

            entry_signal = chan_buy & macro_trend_gate & weekly_regime
            exit_signal = chan_sell | (~macro_trend_gate) | (~weekly_regime)
            size_at_entry = np.where(trend_confirmed.to_numpy(), position_size_pct, pivot_osc_size_pct)

            raw_weights[sym] = _run_variable_size_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, size_at_entry
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Multi-Timeframe Trend Strategy (区间套与趋势共振): "
            f"longs active risky symbols when daily Chan buy signals align with a macro "
            f"{p.get('chan_mtf_trend_ma_period', cfg.chan_mtf_trend_ma_period)}-day SMA uptrend filter "
            "AND a genuine weekly-level structural re-confirmation (区间套); uses full size once Lesson "
            "107's precise trend gate (non-divergent B3 rally) confirms, a reduced size otherwise; "
            "exits on Chan sell signals, macro trend loss, weekly regime loss, stop-loss, or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        trend_ma_period = p.get("chan_mtf_trend_ma_period", cfg.chan_mtf_trend_ma_period)
        min_gap_bars = p.get("chan_mtf_min_gap_bars", cfg.chan_mtf_min_gap_bars)
        min_strokes = p.get("chan_mtf_min_strokes", cfg.chan_mtf_min_strokes)
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        weekly_structural_bars = ((min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)) * 5
        return max(trend_ma_period, structural, weekly_structural_bars)


class ChanTrendThirdBuyStrategy(AllocationTemplate):
    """Chan Trend Third Buy Strategy (第三类买卖点突破回踩策略):
    Targeted trend-continuation strategy focusing specifically on 3rd-type buy points (B3)
    -- breakout above a pivot ($ZG$) where the subsequent pullback/retest low stays strictly
    above $ZG$ ($L_{pullback} > ZG$).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_trend_third_buy", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan_b3_min_gap_bars", cfg.chan_b3_min_gap_bars)
        min_strokes = p.get("chan_b3_min_strokes", cfg.chan_b3_min_strokes)
        macd_fast = p.get("chan_b3_macd_fast", cfg.chan_b3_macd_fast)
        macd_slow = p.get("chan_b3_macd_slow", cfg.chan_b3_macd_slow)
        macd_signal = p.get("chan_b3_macd_signal", cfg.chan_b3_macd_signal)
        stop_loss_pct = p.get("chan_b3_stop_loss_pct", cfg.chan_b3_stop_loss_pct)
        max_holding_days = p.get("chan_b3_max_holding_days", cfg.chan_b3_max_holding_days)
        position_size_pct = p.get("chan_b3_position_size_pct", cfg.chan_b3_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            )
            entry_signal = sig["third_buy"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Trend Third Buy Strategy (第三类买卖点突破回踩): "
            "longs active risky symbols on 3rd-type buy points (B3 - pivot breakout retest holding above pivot upper band ZG); "
            "exits on 3rd-type sell points (S3), general sell signals, stop-loss, or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_b3_min_gap_bars", cfg.chan_b3_min_gap_bars)
        min_strokes = p.get("chan_b3_min_strokes", cfg.chan_b3_min_strokes)
        macd_slow = p.get("chan_b3_macd_slow", cfg.chan_b3_macd_slow)
        macd_signal = p.get("chan_b3_macd_signal", cfg.chan_b3_macd_signal)
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, macd_slow + macd_signal + 10)


class ChanMeanReversionDivergenceStrategy(AllocationTemplate):
    """Chan Mean-Reversion Divergence Strategy (一类买卖点背驰与防狼术策略):
    Contrarian bottom-fishing strategy focusing on 1st-type buy points (B1) triggered by MACD
    histogram area/peak divergence after a downward trend. Only enters once MACD (DIF/DEA)
    has reclaimed the zero axis, per Lesson 103's actual '防狼术' rule (0952-...-103.md:
    avoid any market whose MACD lines sit below the zero axis; only re-enter once it
    re-stands on it) -- trades some bottom-timing edge for the lesson's own disclosed
    defensive discipline. Also incorporates strict tight-risk exit controls: tight
    stop-loss, quick profit target, trailing stop, and holding period cap.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_mean_reversion_divergence", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan_mrd_min_gap_bars", cfg.chan_mrd_min_gap_bars)
        min_strokes = p.get("chan_mrd_min_strokes", cfg.chan_mrd_min_strokes)
        macd_fast = p.get("chan_mrd_macd_fast", cfg.chan_mrd_macd_fast)
        macd_slow = p.get("chan_mrd_macd_slow", cfg.chan_mrd_macd_slow)
        macd_signal = p.get("chan_mrd_macd_signal", cfg.chan_mrd_macd_signal)
        stop_loss_pct = p.get("chan_mrd_stop_loss_pct", cfg.chan_mrd_stop_loss_pct)
        profit_target_pct = p.get("chan_mrd_profit_target_pct", cfg.chan_mrd_profit_target_pct)
        trailing_stop_pct = p.get("chan_mrd_trailing_stop_pct", cfg.chan_mrd_trailing_stop_pct)
        trailing_activate_pct = p.get("chan_mrd_trailing_activate_pct", cfg.chan_mrd_trailing_activate_pct)
        max_holding_days = p.get("chan_mrd_max_holding_days", cfg.chan_mrd_max_holding_days)
        position_size_pct = p.get("chan_mrd_position_size_pct", cfg.chan_mrd_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            )
            zero_axis_ok = _macd_zero_axis_confirmed(bars["Close"], macd_fast, macd_slow, macd_signal).reindex(master_index).fillna(False)
            entry_signal = sig["first_buy"].reindex(master_index).fillna(False) & zero_axis_ok
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_mrd_position_exit(
                close=close,
                entry_signal=entry_signal,
                exit_signal=exit_signal,
                stop_loss_pct=stop_loss_pct,
                profit_target_pct=profit_target_pct,
                trailing_stop_pct=trailing_stop_pct,
                trailing_activate_pct=trailing_activate_pct,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Mean-Reversion Divergence Strategy (一类买卖点背驰与防狼术): "
            "longs active risky symbols on 1st-type buy points (B1 - MACD histogram divergence after downward trend) "
            "only once MACD (DIF/DEA) has reclaimed the zero axis, per Lesson 103's actual 防狼术 rule; "
            "applies strict tight risk management (stop-loss, profit target, trailing stop, max holding cap)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_mrd_min_gap_bars", cfg.chan_mrd_min_gap_bars)
        min_strokes = p.get("chan_mrd_min_strokes", cfg.chan_mrd_min_strokes)
        macd_slow = p.get("chan_mrd_macd_slow", cfg.chan_mrd_macd_slow)
        macd_signal = p.get("chan_mrd_macd_signal", cfg.chan_mrd_macd_signal)
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, macd_slow + macd_signal + 10)


class ChanCompositeStrategy(AllocationTemplate):
    """Chan Composite Strategy (一二三类买点动态组合建仓策略):
    Dynamic multi-stage position scaling across all 3 Chan buy point types:
    - 30% initial position on B1 (first_buy, bottom divergence)
    - +40% position addition on B2 (second_buy, higher low pullback)
    - +30% position addition on B3 (third_buy, pivot breakout retest)
    Exits on any sell point (S1/S2/S3), stop-loss, max holding period, or
    Lessons 92-99's dangerous pivot-relation state (`classify_pivot_relations`
    -- the new pivot's own extreme broke through the prior pivot's against
    the trend) even absent a formal sell point, as an additional risk brake.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_composite", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan_comp_min_gap_bars", cfg.chan_comp_min_gap_bars)
        min_strokes = p.get("chan_comp_min_strokes", cfg.chan_comp_min_strokes)
        macd_fast = p.get("chan_comp_macd_fast", cfg.chan_comp_macd_fast)
        macd_slow = p.get("chan_comp_macd_slow", cfg.chan_comp_macd_slow)
        macd_signal = p.get("chan_comp_macd_signal", cfg.chan_comp_macd_signal)
        b1_w = p.get("chan_comp_b1_weight", cfg.chan_comp_b1_weight)
        b2_w = p.get("chan_comp_b2_weight", cfg.chan_comp_b2_weight)
        b3_w = p.get("chan_comp_b3_weight", cfg.chan_comp_b3_weight)
        stop_loss_pct = p.get("chan_comp_stop_loss_pct", cfg.chan_comp_stop_loss_pct)
        max_holding_days = p.get("chan_comp_max_holding_days", cfg.chan_comp_max_holding_days)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            )
            first_buy = sig["first_buy"].reindex(master_index).fillna(False)
            second_buy = sig["second_buy"].reindex(master_index).fillna(False)
            third_buy = sig["third_buy"].reindex(master_index).fillna(False)
            pivot_danger = _pivot_relation_danger_series(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)
            sell_signal = sig["sell_signal"].reindex(master_index).fillna(False) | pivot_danger
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_composite_position_loop(
                close=close,
                first_buy=first_buy,
                second_buy=second_buy,
                third_buy=third_buy,
                sell_signal=sell_signal,
                b1_w=b1_w,
                b2_w=b2_w,
                b3_w=b3_w,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Composite Strategy (一二三类买点动态组合建仓): "
            f"scales position dynamically across Chan buy points ({p.get('chan_comp_b1_weight', cfg.chan_comp_b1_weight)*100:.0f}% on B1, "
            f"+{p.get('chan_comp_b2_weight', cfg.chan_comp_b2_weight)*100:.0f}% on B2, +{p.get('chan_comp_b3_weight', cfg.chan_comp_b3_weight)*100:.0f}% on B3); "
            "exits on any sell point, stop-loss, or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_comp_min_gap_bars", cfg.chan_comp_min_gap_bars)
        min_strokes = p.get("chan_comp_min_strokes", cfg.chan_comp_min_strokes)
        macd_slow = p.get("chan_comp_macd_slow", cfg.chan_comp_macd_slow)
        macd_signal = p.get("chan_comp_macd_signal", cfg.chan_comp_macd_signal)
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, macd_slow + macd_signal + 10)


class ChanBestSelectorStrategy(AllocationTemplate):
    """Chan Best Selector Meta-Strategy (动态最佳缠论策略选择器):
    Runs all Chan strategies in parallel, evaluates their rolling trailing performance
    (e.g., rolling Sharpe ratio / return over lookback_days = 63), and dynamically routes
    100% of portfolio allocation to the best-performing strategy at each rebalance date.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_best_selector", param_grid={})

    def _get_sub_strategies(self, cfg: StrategyConfig) -> Dict[str, AllocationTemplate]:
        from .strategy import (
            ChanPivotShiftMACDStrategy,
            ChanPivotShiftStrategy,
            ChanThreeTypeStrategy,
        )
        return {
            "chan_pivot_shift": ChanPivotShiftStrategy(cfg),
            "chan_pivot_shift_macd": ChanPivotShiftMACDStrategy(cfg),
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_mtf_trend": ChanMultiTimeframeTrendStrategy(cfg),
            "chan_trend_third_buy": ChanTrendThirdBuyStrategy(cfg),
            "chan_mean_reversion_divergence": ChanMeanReversionDivergenceStrategy(cfg),
            "chan_composite": ChanCompositeStrategy(cfg),
        }

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        lookback_days = p.get("chan_best_lookback_days", cfg.chan_best_lookback_days)
        metric = p.get("chan_best_metric", cfg.chan_best_metric)
        rebalance_freq = p.get("chan_best_rebalance_freq_days", cfg.chan_best_rebalance_freq_days)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        n_dates = len(master_index)
        if n_dates == 0:
            return pd.DataFrame()

        # Build daily returns matrix for risky symbols
        asset_returns = pd.DataFrame(index=master_index)
        for sym in risky_symbols:
            c = universe[sym]["Close"].reindex(master_index).ffill()
            asset_returns[sym] = c.pct_change().fillna(0.0)

        sub_strats = self._get_sub_strategies(cfg)
        sub_daily_weights: Dict[str, pd.DataFrame] = {}
        sub_returns: Dict[str, pd.Series] = {}

        for key, strat in sub_strats.items():
            sparse_w = strat.generate_weights(universe, params)
            if sparse_w.empty:
                w_daily = pd.DataFrame(0.0, index=master_index, columns=symbols)
            else:
                w_daily = sparse_w.reindex(master_index).ffill().fillna(0.0)
                w_daily = _fill_out_columns(w_daily, symbols)

            sub_daily_weights[key] = w_daily

            # Daily portfolio return: sum_sym(w_{sym, t-1} * r_{sym, t})
            w_risky = w_daily[risky_symbols].shift(1).fillna(0.0)
            ret_series = (w_risky * asset_returns[risky_symbols]).sum(axis=1)
            sub_returns[key] = ret_series

        sub_ret_df = pd.DataFrame(sub_returns, index=master_index)

        # Select best strategy dynamically
        output_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        current_best_key = "chan_pivot_shift"  # Default fallback
        rebalance_dates = set(_get_rebalance_dates(master_index, rebalance_freq))

        for t in range(n_dates):
            date = master_index[t]

            # Reevaluate selection periodically
            if t >= lookback_days and (date in rebalance_dates or t == lookback_days):
                window_ret = sub_ret_df.iloc[t - lookback_days : t]
                best_key = current_best_key
                best_score = -999_999.0

                for key in sub_strats.keys():
                    r = window_ret[key]
                    if metric == "sharpe":
                        std_val = r.std()
                        score = (r.mean() / std_val * np.sqrt(252)) if std_val > 1e-8 else 0.0
                    else:
                        score = (1.0 + r).prod() - 1.0

                    if score > best_score:
                        best_score = score
                        best_key = key

                current_best_key = best_key

            # Copy current best strategy's weights
            output_weights.loc[date] = sub_daily_weights[current_best_key].loc[date]

        output_weights = _cap_and_deroute_to_cash(output_weights, symbols, cash_proxy)
        output_weights = _fill_out_columns(output_weights, symbols)
        return _sparse_from_daily(output_weights)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Best Selector Meta-Strategy (动态最佳缠论策略选择器): "
            f"evaluates 7 Chan strategies in parallel, evaluates rolling trailing {p.get('chan_best_metric', cfg.chan_best_metric)} "
            f"over a {p.get('chan_best_lookback_days', cfg.chan_best_lookback_days)}-day lookback window, and dynamically routes "
            "100% of allocation to the top-performing Chan strategy."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        lookback_days = p.get("chan_best_lookback_days", cfg.chan_best_lookback_days)
        sub_warmups = [s.warmup_bars(params) for s in self._get_sub_strategies(cfg).values()]
        return max(sub_warmups) + lookback_days


class ChanVaaCompoundStrategy(AllocationTemplate):
    """Chan Pivot Shift MACD + VAA Optimal Compound Strategy:

    A multi-regime compound strategy combining:
    1. Trend-following structural alpha from `ChanPivotShiftMACDStrategy` (中枢迁移买卖点 + MACD背驰/零轴确认)
    2. Regime crash-protection from `VigilantAssetAllocation` (VAA 13612W 动量防崩塌模型)

    Regime Logic:
    - In Safe/Bull regime (all tracked offensive assets have 13612W momentum > 0):
      Allocates base `chan_vaa_chan_weight` (default 60%) to Chan structural breakout
      positions, and remaining 40% (plus any unallocated Chan capacity) to the leading VAA offensive asset.
    - In Bear/Defensive regime (at least one offensive asset momentum <= 0):
      Rotates primary capital into VAA's leading defensive asset (IEF/BIL), scaling down
      or gating equity risk while strictly maintaining stop-losses.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_vaa_compound", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        chan_weight = float(p.get("chan_vaa_chan_weight", cfg.chan_vaa_chan_weight))
        mode = str(p.get("chan_vaa_mode", cfg.chan_vaa_mode))
        defensive_boost = bool(p.get("chan_vaa_defensive_boost", cfg.chan_vaa_defensive_boost))
        gate_in_defensive = bool(p.get("chan_vaa_gate_chan_in_defensive", cfg.chan_vaa_gate_chan_in_defensive))
        rebal_freq = int(p.get("chan_vaa_rebalance_freq_days", cfg.chan_vaa_rebalance_freq_days))

        # 1. Compute Chan Pivot Shift MACD raw weights for risky symbols
        risky_symbols = _get_risky_symbols_helper(
            universe, p,
            cfg_symbol=getattr(cfg, "symbol", None),
            cfg_risky_universe=getattr(cfg, "risky_universe", None),
            cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        min_gap_bars = int(p.get("chanm_min_gap_bars", cfg.chanm_min_gap_bars))
        min_strokes = int(p.get("chanm_min_strokes", cfg.chanm_min_strokes))
        macd_fast = int(p.get("chanm_macd_fast", cfg.chanm_macd_fast))
        macd_slow = int(p.get("chanm_macd_slow", cfg.chanm_macd_slow))
        macd_signal = int(p.get("chanm_macd_signal", cfg.chanm_macd_signal))
        stop_loss_pct = p.get("chanm_stop_loss_pct", cfg.chanm_stop_loss_pct)
        max_holding_days = p.get("chanm_max_holding_days", cfg.chanm_max_holding_days)
        position_size_pct = float(p.get("chanm_position_size_pct", cfg.chanm_position_size_pct))

        chan_raw_weights: Dict[str, pd.Series] = {}
        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan_pivot_macd_signals(
                bars,
                min_gap_bars=min_gap_bars,
                min_strokes=min_strokes,
                macd_fast=macd_fast,
                macd_slow=macd_slow,
                macd_signal=macd_signal,
            )
            entry_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            chan_raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        chan_daily = pd.DataFrame(chan_raw_weights, index=master_index)
        # Normalize across active risky positions so sum <= 1.0
        active_counts = (chan_daily > 0).sum(axis=1)
        chan_scaled = chan_daily.copy()
        for dt in master_index:
            c = active_counts.loc[dt]
            if c > 1:
                chan_scaled.loc[dt] = chan_daily.loc[dt] / c

        # 2. Compute VAA 13612W Momentum and Regime
        offensive_universe = p.get("chan_vaa_offensive_universe", cfg.chan_vaa_offensive_universe)
        defensive_universe = p.get("chan_vaa_defensive_universe", cfg.chan_vaa_defensive_universe)

        offensive_symbols = [s for s in offensive_universe if s in symbols]
        defensive_symbols = [s for s in defensive_universe if s in symbols]
        all_tracked = list(dict.fromkeys(offensive_symbols + defensive_symbols))

        def score_13612w(sym: str) -> pd.Series:
            close = universe[sym]["Close"]
            return 12 * roc(close, 21) + 4 * roc(close, 63) + 2 * roc(close, 126) + roc(close, 252)

        scores = pd.DataFrame({sym: score_13612w(sym) for sym in all_tracked}) if all_tracked else pd.DataFrame()

        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)
        vaa_rebal_weights = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)
        vaa_regime_series = pd.Series(index=rebalance_dates, dtype=object)

        for date in rebalance_dates:
            off_scores = scores.loc[date, offensive_symbols].dropna() if offensive_symbols else pd.Series(dtype=float)
            # If not enough history yet, default to defensive/cash
            if len(off_scores) < len(offensive_symbols) or off_scores.empty:
                vaa_regime_series.loc[date] = "defensive"
                def_scores = scores.loc[date, defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
                if not def_scores.empty:
                    vaa_rebal_weights.loc[date, def_scores.idxmax()] = 1.0
                elif cash_proxy in symbols:
                    vaa_rebal_weights.loc[date, cash_proxy] = 1.0
                continue

            # Check if all offensive assets have positive 13612W momentum
            if (off_scores > 0).all():
                vaa_regime_series.loc[date] = "bull"
                vaa_rebal_weights.loc[date, off_scores.idxmax()] = 1.0
            else:
                vaa_regime_series.loc[date] = "defensive"
                def_scores = scores.loc[date, defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
                if not def_scores.empty:
                    vaa_rebal_weights.loc[date, def_scores.idxmax()] = 1.0
                elif cash_proxy in symbols:
                    vaa_rebal_weights.loc[date, cash_proxy] = 1.0

        # Forward-fill VAA targets and regime across all dates
        vaa_daily = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        vaa_daily.loc[rebalance_dates] = vaa_rebal_weights
        vaa_daily = vaa_daily.ffill().fillna(0.0)

        vaa_regimes = pd.Series(index=master_index, dtype=object)
        vaa_regimes.loc[rebalance_dates] = vaa_regime_series
        vaa_regimes = vaa_regimes.ffill().fillna("defensive")

        # 3. Blend allocations according to mode and regime
        output_weights = pd.DataFrame(index=master_index, columns=symbols, data=0.0)

        for date in master_index:
            regime = vaa_regimes.loc[date]
            v_weights = vaa_daily.loc[date]

            if mode == "fixed_blend":
                # Fixed proportion blend
                w_chan = chan_scaled.loc[date] * chan_weight if date in chan_scaled.index else pd.Series(0.0, index=symbols)
                w_vaa = v_weights * (1.0 - chan_weight)
                for sym in symbols:
                    output_weights.loc[date, sym] = (w_chan.get(sym, 0.0) if sym in w_chan else 0.0) + w_vaa.get(sym, 0.0)
            else:
                # Regime-adaptive blend
                if regime == "bull":
                    # Bull mode: Allocate chan_weight to Chan, remaining to VAA top offensive
                    c_weights = chan_scaled.loc[date] if date in chan_scaled.index else pd.Series(0.0, index=symbols)
                    c_sum = c_weights.sum()

                    for sym in symbols:
                        c_alloc = c_weights.get(sym, 0.0) * chan_weight
                        v_alloc = v_weights.get(sym, 0.0) * (1.0 - chan_weight * c_sum)
                        output_weights.loc[date, sym] = c_alloc + v_alloc
                else:
                    # Defensive / Bear mode: Crash protection
                    if gate_in_defensive:
                        # Zero out Chan entirely; 100% in VAA defensive asset
                        output_weights.loc[date] = v_weights
                    elif defensive_boost:
                        # Scale down Chan exposure by 50% (or cap at 30%) and allocate balance to defensive asset
                        c_weights = (chan_scaled.loc[date] * 0.5) if date in chan_scaled.index else pd.Series(0.0, index=symbols)
                        c_sum = c_weights.sum()
                        def_alloc = max(1.0 - c_sum, 0.70)
                        for sym in symbols:
                            output_weights.loc[date, sym] = c_weights.get(sym, 0.0) + v_weights.get(sym, 0.0) * def_alloc
                    else:
                        c_weights = chan_scaled.loc[date] if date in chan_scaled.index else pd.Series(0.0, index=symbols)
                        c_sum = c_weights.sum()
                        for sym in symbols:
                            output_weights.loc[date, sym] = c_weights.get(sym, 0.0) * chan_weight + v_weights.get(sym, 0.0) * (1.0 - chan_weight)

        output_weights = _cap_and_deroute_to_cash(output_weights, symbols, cash_proxy)
        output_weights = _fill_out_columns(output_weights, symbols)
        return _sparse_from_daily(output_weights)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        chan_w = p.get("chan_vaa_chan_weight", cfg.chan_vaa_chan_weight)
        mode = p.get("chan_vaa_mode", cfg.chan_vaa_mode)
        def_boost = p.get("chan_vaa_defensive_boost", cfg.chan_vaa_defensive_boost)
        return (
            "Chan Pivot Shift MACD + VAA Optimal Compound Strategy (缠论中枢MACD与VAA动量混合最优策略): "
            f"blends ChanPivotShiftMACD (trend structural breakout, base weight={chan_w:.0%}) "
            f"with Vigilant Asset Allocation (VAA-G4 dual momentum regime crash protection). "
            f"Mode={mode}, Defensive Boost={def_boost}."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252


"""Chan Theory Advanced & Compound Quantitative Trading Strategies.

This module implements 9 specialized Chan-theory (缠中说禅) trading strategies:

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
6. `ChanVaaCompoundStrategy`: Macro regime crash protection compounder dynamically
   shifting allocation between Chan structural trend following (bullish alpha)
   and VAA-G4 dual momentum (crash defense).
7. `ChanRiskManagedBlendStrategy`: Institutional ensemble blending top 3 Chan strategies
   (chan_composite, chan_three_type, chan_vaa_compound) with position caps, multi-tier
   drawdown circuit breakers, and dynamic cash deployment in bull breadth regimes.
8. `ChanFourStateBlendStrategy`: Institutional ensemble blending chan_four_state (20%),
   chan_three_type (40%), and chan_vaa_compound (40%) with institutional risk controls.
9. `ChanFourStateExecutionStrategy`: Industrial-grade 4-state operational execution
   machine (BUY_CANDIDATE, HOLD, HOLD_ALERT, SELL_EXIT, WAIT_OBSERVE) with deterministic
   structural invalidation stops (1B bar low, 2B dd low, 3B zg breakout pivot),
   ratcheting trailing stop to ZG, MA entanglement filter, and Lesson 16 zero-consolidation drag.

All strategies inherit from `AllocationTemplate` and maximize code reuse from
`rs/chan_structure.py`, `rs/chan_signals.py`, and `common.position_exits`.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    apply_asset_inertia,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import adx, macd, roc, sma
from common.position_exits import run_stop_timeout_exit
from common.scheduling import get_rebalance_dates as _get_rebalance_dates

from .chan_structure import (
    build_pivots,
    build_strokes,
    classify_pivot_relations,
    compute_chan_signals,
    compute_stroke_trend,
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


def _failed_retest_confirmed(bars: pd.DataFrame, sig: pd.DataFrame, confirm_window_bars: int) -> pd.Series:
    """Lesson 108's precise bottom definition + "下探失败买" rule
    (1104-486e105c0100abkx-108.md): rather than entering on the raw
    `first_buy` (B1) MACD-divergence bottom itself, only confirms entry once
    a SECOND bottom fractal (顶分型/底分型) within `confirm_window_bars`
    merged bars fails to make a new low relative to the bottom fractal
    nearest the `first_buy` signal (a failed retest of the low). Returns a
    boolean series aligned to `bars.index`, True only on the later
    confirmation bar, never on the original `first_buy` bar itself.

    Simplification (disclosed): `classify_points` doesn't expose the exact
    fractal a first-type point's own bottom corresponds to, so the nearest
    PRECEDING bottom fractal is used as a proxy for "the bottom this B1
    divergence was fished from".
    """
    confirmed = pd.Series(False, index=bars.index)
    first_buy = sig["first_buy"].reindex(bars.index).fillna(False)
    if not first_buy.any():
        return confirmed

    merged = merge_inclusion(bars)
    fractals = find_fractals(merged)
    bottom_fractals = fractals[fractals["kind"] == "bottom"]
    if bottom_fractals.empty:
        return confirmed

    merged_pos_by_ts = {ts: i for i, ts in enumerate(merged.index)}

    for ts in first_buy.index[first_buy]:
        if ts not in merged_pos_by_ts:
            continue
        signal_pos = merged_pos_by_ts[ts]
        prior_bottoms = bottom_fractals[bottom_fractals["pos"] < signal_pos]
        if prior_bottoms.empty:
            continue
        first_bottom_price = float(prior_bottoms.iloc[-1]["price"])

        later_bottoms = bottom_fractals[
            (bottom_fractals["pos"] >= signal_pos)
            & (bottom_fractals["pos"] <= signal_pos + confirm_window_bars)
        ]
        for _, later in later_bottoms.iterrows():
            if float(later["price"]) > first_bottom_price:
                confirm_pos = int(later["pos"]) + 1
                if confirm_pos < len(merged):
                    confirmed.loc[merged.index[confirm_pos]] = True
                break

    return confirmed


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
    allow_flat_b2_b3: bool = True,
    low: Optional[pd.Series | np.ndarray] = None,
    high: Optional[pd.Series | np.ndarray] = None,
    structural_stop: Optional[pd.Series | np.ndarray] = None,
) -> np.ndarray:
    """Stateful position scaling loop for Chan Composite strategy:
    - B1 (first_buy): opens the position (b1_w). When allow_flat_b2_b3=True,
      B2/B3 can also open positions while flat at their respective weights.
    - B2 (second_buy): add allocation (+b2_w)
    - B3 (third_buy): add allocation (+b3_w)
    - Sell signal or stop-loss / timeout / structural invalidation: clear back to 0.0

    `entry_price` is a weighted-average cost basis, updated on every B2/B3
    scale-in (`new_price = (old_price*old_weight + fill_price*added_weight)
    / new_weight`) so the stop-loss is measured against the position's real
    blended cost, not just the original B1 fill. `entry_idx` (holding-period
    timeout) deliberately stays at the ORIGINAL entry bar -- position age is
    measured from when the thesis first opened, not reset on each add-on.
    When `structural_stop` is supplied, breach below it triggers deterministic
    invalidation exit.
    """
    close_arr = np.asarray(close)
    b1_arr = np.asarray(first_buy)
    b2_arr = np.asarray(second_buy)
    b3_arr = np.asarray(third_buy)
    sell_arr = np.asarray(sell_signal)
    low_arr = np.asarray(low) if low is not None else None
    struct_stop_arr = np.asarray(structural_stop) if structural_stop is not None else None
    n = len(close_arr)
    raw = np.zeros(n)

    current_weight = 0.0
    entry_price = 0.0
    entry_idx = 0

    for i in range(n):
        if current_weight > 0.0:
            held = i - entry_idx
            p = close_arr[i]
            eval_p = low_arr[i] if low_arr is not None else p
            ret = eval_p / entry_price - 1.0 if entry_price > 0 else 0.0

            stopped = stop_loss_pct is not None and ret <= -stop_loss_pct
            if struct_stop_arr is not None:
                s_stop = struct_stop_arr[i]
                if not np.isnan(s_stop) and eval_p < s_stop:
                    stopped = True
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
            elif allow_flat_b2_b3 and b2_arr[i]:
                current_weight = b2_w
                entry_price = close_arr[i]
                entry_idx = i
                raw[i] = current_weight
            elif allow_flat_b2_b3 and b3_arr[i]:
                current_weight = b3_w
                entry_price = close_arr[i]
                entry_idx = i
                raw[i] = current_weight

    return raw


def _extract_pivot_and_stroke_series(
    bars: pd.DataFrame, min_gap_bars: int = 4, min_strokes: int = 3
) -> pd.DataFrame:
    """Per-bar rolling extraction of confirmed pivot levels (zg, zd, gg, dd)
    and confirmed stroke direction (1 for UP, -1 for DOWN, 0 for unknown),
    aligned to `bars.index` without lookahead bias.

    A stroke ending at fractal position `end_pos` (in inclusion-merged bars)
    is confirmed at `end_pos + 1`. Similarly, a pivot spanning strokes up to
    `end_stroke_idx` is confirmed when that stroke's fractal is confirmed.
    """
    merged = merge_inclusion(bars)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    pivots = build_pivots(strokes, min_strokes)

    n_merged = len(merged)
    zg_arr = np.full(n_merged, np.nan)
    zd_arr = np.full(n_merged, np.nan)
    gg_arr = np.full(n_merged, np.nan)
    dd_arr = np.full(n_merged, np.nan)
    dir_arr = np.zeros(n_merged, dtype=int)

    if not strokes.empty:
        for _, s in strokes.iterrows():
            confirm_pos = int(s["end_pos"]) + 1
            if confirm_pos < n_merged:
                dir_arr[confirm_pos] = 1 if s["direction"] == "up" else -1

    if not pivots.empty:
        for _, p in pivots.iterrows():
            confirm_pos = int(p["end_pos"]) + 1
            if confirm_pos < n_merged:
                zg_arr[confirm_pos] = p["zg"]
                zd_arr[confirm_pos] = p["zd"]
                gg_arr[confirm_pos] = p["gg"]
                dd_arr[confirm_pos] = p["dd"]

    zg_s = pd.Series(zg_arr, index=merged.index).ffill()
    zd_s = pd.Series(zd_arr, index=merged.index).ffill()
    gg_s = pd.Series(gg_arr, index=merged.index).ffill()
    dd_s = pd.Series(dd_arr, index=merged.index).ffill()
    dir_s = pd.Series(dir_arr, index=merged.index).replace(0, np.nan).ffill().fillna(0).astype(int)

    comb = bars.index.union(merged.index)
    df_out = pd.DataFrame(
        {
            "zg": zg_s.reindex(comb).ffill().reindex(bars.index),
            "zd": zd_s.reindex(comb).ffill().reindex(bars.index),
            "gg": gg_s.reindex(comb).ffill().reindex(bars.index),
            "dd": dd_s.reindex(comb).ffill().reindex(bars.index),
            "stroke_dir": dir_s.reindex(comb).ffill().fillna(0).reindex(bars.index).astype(int),
        },
        index=bars.index,
    )
    return df_out


def run_four_state_position_loop(
    close: pd.Series | np.ndarray,
    first_buy: pd.Series | np.ndarray,
    second_buy: pd.Series | np.ndarray,
    third_buy: pd.Series | np.ndarray,
    sell_signal: pd.Series | np.ndarray,
    zg: Optional[pd.Series | np.ndarray] = None,
    zd: Optional[pd.Series | np.ndarray] = None,
    dd: Optional[pd.Series | np.ndarray] = None,
    stroke_dir: Optional[pd.Series | np.ndarray] = None,
    ma5: Optional[pd.Series | np.ndarray] = None,
    ma20: Optional[pd.Series | np.ndarray] = None,
    stop_loss_pct: Optional[float] = 0.08,
    max_holding_days: Optional[int] = 90,
    position_size_pct: float = 1.0,
    exit_on_consolidation: bool = True,
    trail_stop_to_zg: bool = True,
    use_ma_filter: bool = True,
    low: Optional[pd.Series | np.ndarray] = None,
    high: Optional[pd.Series | np.ndarray] = None,
    min_hold_bars: int = 5,
    zg_tolerance_pct: float = 0.025,
    consolidation_timeout_bars: int = 8,
    stop_evaluation_mode: str = "close",
    b1_buffer_pct: float = 0.03,
    cooldown_bars: int = 0,
    two_stage_entry: bool = False,
) -> np.ndarray:
    """Stateful 4-state execution position loop for Chan theory trading:
    Implements a deterministic finite state machine (FSM) following Chan Lessons 11-14, 16, 20, 53:
    1. BUY_CANDIDATE: On fresh 1B/2B/3B buy point, sets deterministic structural invalidation:
       - 3B (Third Buy): Invalidation is strictly ZG (pivot high) with zg_tolerance_pct buffer.
       - 2B (Second Buy): Invalidation is prior swing low DD.
       - 1B (First Buy): Invalidation is 1B fractal bar low with b1_buffer_pct (e.g. 3%) cushion
         to absorb secondary undercut / shakeout wicks.
       - Bounded by fallback stop_loss_pct (e.g. 8%).
    2. HOLD: When price is above_zs (Close > ZG) and stroke direction is UP. Position is maintained
       and trailing stop is ratcheted upward to ZG (with zg_tolerance_pct) to lock in breakout profits.
       If two_stage_entry=True, upgrades to 100% position size once upward expansion stroke confirms.
    3. HOLD_ALERT: When price is above_zs but stroke turns DOWN (minor pullback) or MA5/MA20 entangle
       (spread < 2%, Lessons 11-14 '吻') with non-positive stroke momentum. Tightens stop loss to ZG.
    4. SELL_EXIT: Formal sell points (1S/2S/3S), breach of structural invalidation stop
       (evaluated on Close or Low according to stop_evaluation_mode), fallback stop-loss, or
       max holding period timeout clears position to 0.0, and initiates cooldown_bars re-entry lock.
    5. WAIT_OBSERVE (Lesson 16 中小资金高效操作法):
       When price is in_zs (zd <= Close <= zg) or below_zs, allocation is 0.0.
       If exit_on_consolidation=True:
       - 3B entries: given min_hold_bars gestation buffer for breakout retest to develop.
         After min_hold_bars, exited if price falls back below ZG * (1 - zg_tolerance_pct).
       - 1B/2B entries: given min_hold_bars (e.g. 5 bars) to develop towards the pivot.
         After min_hold_bars, exits if price remains below_zs or is stagnant in_zs for
         consolidation_timeout_bars with non-positive stroke momentum, eliminating
         premature day-1 whipsaw churn while strictly avoiding multi-week sideways drag.
    """
    close_arr = np.asarray(close)
    b1_arr = np.asarray(first_buy)
    b2_arr = np.asarray(second_buy)
    b3_arr = np.asarray(third_buy)
    sell_arr = np.asarray(sell_signal)
    low_arr = np.asarray(low) if low is not None else close_arr
    high_arr = np.asarray(high) if high is not None else close_arr
    zg_arr = np.asarray(zg) if zg is not None else None
    zd_arr = np.asarray(zd) if zd is not None else None
    dd_arr = np.asarray(dd) if dd is not None else None
    dir_arr = np.asarray(stroke_dir) if stroke_dir is not None else None
    ma5_arr = np.asarray(ma5) if ma5 is not None else None
    ma20_arr = np.asarray(ma20) if ma20 is not None else None

    n = len(close_arr)
    raw = np.zeros(n)
    in_position = False
    entry_idx = 0
    entry_price = 0.0
    entry_buy_type = "1B"
    bars_in_zs = 0
    structural_stop = -np.inf
    cooldown_until = -1
    stage2_confirmed = False

    for i in range(n):
        p = close_arr[i]
        eval_low = low_arr[i]
        eval_high = high_arr[i]
        curr_zg = zg_arr[i] if (zg_arr is not None and not np.isnan(zg_arr[i])) else None
        curr_zd = zd_arr[i] if (zd_arr is not None and not np.isnan(zd_arr[i])) else None
        curr_dd = dd_arr[i] if (dd_arr is not None and not np.isnan(dd_arr[i])) else None
        curr_dir = dir_arr[i] if dir_arr is not None else 0

        # Position relative to pivot
        if curr_zg is not None and curr_zd is not None:
            if p > curr_zg:
                pos = "above_zs"
            elif p < curr_zd:
                pos = "below_zs"
            else:
                pos = "in_zs"
        else:
            pos = "unknown"

        if in_position:
            held = i - entry_idx

            # Ratchet trailing stop to ZG when trading above pivot band
            if trail_stop_to_zg and curr_zg is not None and pos == "above_zs":
                effective_zg = curr_zg * (1.0 - zg_tolerance_pct)
                structural_stop = max(structural_stop, effective_zg)

            eval_price = close_arr[i] if stop_evaluation_mode == "close" else eval_low
            stopped_struct = (structural_stop > -np.inf and eval_price < structural_stop)
            ret = (eval_price / entry_price - 1.0) if entry_price > 0 else 0.0
            stopped_pct = (stop_loss_pct is not None and ret <= -stop_loss_pct)
            timed_out = (max_holding_days is not None and held >= max_holding_days)

            # Lesson 16: Zero-consolidation drag exit
            consolidation_exit = False
            if exit_on_consolidation:
                if entry_buy_type == "3B":
                    # 3B breakout must hold ZG: allow min_hold_bars gestation for confirmation
                    if held >= min_hold_bars and curr_zg is not None:
                        ref_p = close_arr[i] if stop_evaluation_mode == "close" else eval_low
                        if ref_p < curr_zg * (1.0 - zg_tolerance_pct):
                            consolidation_exit = True
                else:
                    # 1B/2B entries develop from below/inside pivot: allow min_hold_bars gestation
                    if held >= min_hold_bars:
                        if pos == "below_zs":
                            consolidation_exit = True
                        elif pos == "in_zs":
                            bars_in_zs += 1
                            if bars_in_zs >= consolidation_timeout_bars and curr_dir <= 0:
                                consolidation_exit = True
                    elif pos == "in_zs":
                        bars_in_zs += 1

            if pos == "above_zs":
                bars_in_zs = 0

            if sell_arr[i] or stopped_struct or stopped_pct or timed_out or consolidation_exit:
                in_position = False
                raw[i] = 0.0
                structural_stop = -np.inf
                bars_in_zs = 0
                stage2_confirmed = False
                if cooldown_bars > 0:
                    cooldown_until = i + cooldown_bars
                continue

            # Alert state: price above ZG but stroke turned down or MAs entangled
            if curr_zg is not None:
                is_alert = False
                if curr_dir == -1:
                    is_alert = True
                if use_ma_filter and ma5_arr is not None and ma20_arr is not None:
                    spread = abs(ma5_arr[i] - ma20_arr[i]) / max(1e-6, p)
                    # Only alert on MA entanglement if stroke momentum is weakening or breaking MA20
                    if spread < 0.02 and (curr_dir <= 0 or p < ma20_arr[i]):
                        is_alert = True
                if is_alert:
                    structural_stop = max(structural_stop, curr_zg * (1.0 - zg_tolerance_pct))

            # Two-stage sizing: upgrade to 100% size once upward expansion stroke confirms
            if two_stage_entry and not stage2_confirmed:
                if curr_dir == 1 and p > entry_price:
                    stage2_confirmed = True

            current_size = position_size_pct if (not two_stage_entry or stage2_confirmed) else (position_size_pct * 0.50)
            raw[i] = current_size
        else:
            # Check re-entry cooldown
            if i < cooldown_until:
                raw[i] = 0.0
                continue

            b1 = b1_arr[i]
            b2 = b2_arr[i]
            b3 = b3_arr[i]

            if not (b1 or b2 or b3):
                raw[i] = 0.0
                continue

            # MA filter on entry: for 3B breakouts, require MA5 >= MA20 (bullish arrangement)
            if use_ma_filter and ma5_arr is not None and ma20_arr is not None:
                if b3 and not (b1 or b2) and ma5_arr[i] < ma20_arr[i]:
                    raw[i] = 0.0
                    continue

            # Deterministic structural invalidation stop price & buy type tracking
            if b3:
                inv_stop = (curr_zg * (1.0 - zg_tolerance_pct)) if curr_zg is not None else (eval_low * (1.0 - (stop_loss_pct or 0.08)))
                entry_buy_type = "3B"
            elif b2:
                inv_stop = curr_dd if curr_dd is not None else (eval_low * (1.0 - (stop_loss_pct or 0.08)))
                entry_buy_type = "2B"
            else:
                # 1B entry: apply b1_buffer_pct cushion below fractal bar low to absorb secondary undercut wicks
                inv_stop = eval_low * (1.0 - b1_buffer_pct)
                entry_buy_type = "1B"

            if stop_loss_pct is not None:
                structural_stop = max(inv_stop, p * (1.0 - stop_loss_pct))
            else:
                structural_stop = inv_stop

            in_position = True
            entry_idx = i
            entry_price = p
            bars_in_zs = 0
            stage2_confirmed = False
            raw[i] = (position_size_pct * 0.50) if two_stage_entry else position_size_pct

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

        require_weekly = p.get("chan_mtf_require_weekly_regime", getattr(cfg, "chan_mtf_require_weekly_regime", False))
        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))

        for sym in risky_symbols:
            bars = universe[sym]
            close = bars["Close"].reindex(master_index)
            ma = sma(close, trend_ma_period)
            macro_trend_gate = (close > ma).fillna(False)

            sig = compute_chan3_signals(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes, causal=chan_causal)
            stroke_sig = compute_chan_pivot_macd_signals(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes, causal=chan_causal)
            chan_buy = (sig["buy_signal"] | stroke_sig["buy_signal"]).reindex(master_index).fillna(False)
            chan_sell = (sig["sell_signal"] | stroke_sig["sell_signal"]).reindex(master_index).fillna(False)

            stroke_trend = compute_stroke_trend(bars, min_gap_bars).reindex(master_index).ffill().fillna(False)
            relaxed_trend = macro_trend_gate | stroke_trend

            if require_weekly:
                weekly_regime = _weekly_regime_state(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)
                entry_signal = chan_buy & macro_trend_gate & weekly_regime
                exit_signal = chan_sell | (~macro_trend_gate) | (~weekly_regime)
            else:
                entry_signal = chan_buy & relaxed_trend
                exit_signal = chan_sell | (~relaxed_trend & (close < ma * 0.95))

            trend_confirmed = _precise_trend_confirmed(sig).reindex(master_index).ffill().fillna(False) | stroke_trend
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
        req_weekly = p.get("chan_mtf_require_weekly_regime", getattr(cfg, "chan_mtf_require_weekly_regime", False))
        return (
            "Chan Multi-Timeframe Trend Strategy (区间套与趋势共振): "
            f"longs active risky symbols when Chan buy signals (stroke + segment) align with a macro "
            f"{p.get('chan_mtf_trend_ma_period', cfg.chan_mtf_trend_ma_period)}-day SMA uptrend filter or stroke trend "
            f"{'AND weekly structural re-confirmation' if req_weekly else '(relaxed Lesson 107 gating)'}; "
            "uses full size once Lesson 107's precise trend gate confirms, a reduced size otherwise; "
            "exits on Chan sell signals, macro/stroke trend loss, stop-loss, or max holding period."
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

        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))

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
                causal=chan_causal,
            )
            stroke_sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                causal=chan_causal,
            )
            stroke_pivot_shift = stroke_sig["buy_signal"] & ~stroke_sig["divergence_buy"]
            entry_signal = (sig["third_buy"] | stroke_pivot_shift).reindex(master_index).fillna(False)
            exit_signal = (sig["sell_signal"] | stroke_sig["divergence_sell"]).reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)
            low = bars["Low"].reindex(master_index) if "Low" in bars.columns else None
            high = bars["High"].reindex(master_index) if "High" in bars.columns else None

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct,
                low=low, high=high,
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
            "longs active risky symbols on 3rd-type buy points (segment B3 or stroke pivot breakout retest holding above pivot band); "
            "exits on sell points, stop-loss, or max holding period."
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
    """Chan Mean-Reversion Divergence Strategy (一类买卖点背驰与防狼术策略 / 下探失败买):
    Contrarian bottom-fishing strategy focusing on 1st-type buy points (B1) triggered by MACD
    histogram area/peak divergence after a downward trend. Supports modular entry confirmation modes:
    - 'raw_b1': Enters directly on MACD histogram divergence first-buy signal.
    - 'zero_axis': Only enters once MACD (DIF/DEA) reclaims the zero axis (Lesson 103 '防狼术').
    - 'failed_retest': Confirms entry once a second dip fails to make a new low (Lesson 108 '下探失败买').
    - 'combined': Requires both failed retest and MACD zero-axis reclaim.
    Optionally gates entries behind a long-term SMA trend filter (e.g. 200d SMA).
    Incorporates strict risk controls: stop-loss, profit target, trailing stop, and max holding timeout.
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
        entry_mode = p.get("chan_mrd_entry_mode", getattr(cfg, "chan_mrd_entry_mode", "zero_axis"))
        confirm_window_bars = p.get("chan_mrd_confirm_window_bars", getattr(cfg, "chan_mrd_confirm_window_bars", 20))
        require_trend_filter = p.get("chan_mrd_require_trend_filter", getattr(cfg, "chan_mrd_require_trend_filter", False))
        trend_ma_period = p.get("chan_mrd_trend_ma_period", getattr(cfg, "chan_mrd_trend_ma_period", 200))

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

        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                causal=chan_causal,
            )
            stroke_sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                causal=chan_causal,
            )
            raw_first_buy = (sig["first_buy"] | stroke_sig["divergence_buy"]).reindex(master_index).fillna(False)

            if entry_mode == "raw_b1":
                entry_signal = raw_first_buy
            elif entry_mode == "zero_axis":
                zero_axis_ok = _macd_zero_axis_confirmed(bars["Close"], macd_fast, macd_slow, macd_signal).reindex(master_index).fillna(False)
                entry_signal = raw_first_buy & zero_axis_ok
            elif entry_mode == "failed_retest":
                entry_signal = _failed_retest_confirmed(bars, sig, confirm_window_bars).reindex(master_index).fillna(False)
            elif entry_mode == "combined":
                retest_ok = _failed_retest_confirmed(bars, sig, confirm_window_bars).reindex(master_index).fillna(False)
                zero_axis_ok = _macd_zero_axis_confirmed(bars["Close"], macd_fast, macd_slow, macd_signal).reindex(master_index).fillna(False)
                entry_signal = retest_ok & zero_axis_ok
            else:
                entry_signal = raw_first_buy

            if require_trend_filter:
                stroke_trend = compute_stroke_trend(bars, min_gap_bars).reindex(master_index).ffill().fillna(False)
                trend_ma = sma(bars["Close"], trend_ma_period).reindex(master_index)
                trend_ok = (bars["Close"].reindex(master_index) > trend_ma).fillna(False) | stroke_trend
                entry_signal = entry_signal & trend_ok

            exit_signal = (sig["sell_signal"] | stroke_sig["divergence_sell"]).reindex(master_index).fillna(False)
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
        entry_mode = p.get("chan_mrd_entry_mode", getattr(cfg, "chan_mrd_entry_mode", "zero_axis"))
        trend_req = p.get("chan_mrd_require_trend_filter", getattr(cfg, "chan_mrd_require_trend_filter", False))
        return (
            f"Chan Mean-Reversion Divergence Strategy (一类买卖点背驰与防狼术 / 下探失败买): "
            f"entry_mode='{entry_mode}', trend_filter={trend_req} (200d SMA or stroke trend); longs active risky symbols on 1st-type buy points "
            f"with configured entry filters and tight risk management (stop-loss, profit target, trailing stop, holding timeout)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_mrd_min_gap_bars", cfg.chan_mrd_min_gap_bars)
        min_strokes = p.get("chan_mrd_min_strokes", cfg.chan_mrd_min_strokes)
        macd_slow = p.get("chan_mrd_macd_slow", cfg.chan_mrd_macd_slow)
        macd_signal = p.get("chan_mrd_macd_signal", cfg.chan_mrd_macd_signal)
        entry_mode = p.get("chan_mrd_entry_mode", getattr(cfg, "chan_mrd_entry_mode", "zero_axis"))
        confirm_window_bars = p.get("chan_mrd_confirm_window_bars", getattr(cfg, "chan_mrd_confirm_window_bars", 20))
        require_trend_filter = p.get("chan_mrd_require_trend_filter", getattr(cfg, "chan_mrd_require_trend_filter", False))
        trend_ma_period = p.get("chan_mrd_trend_ma_period", getattr(cfg, "chan_mrd_trend_ma_period", 200))

        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        extra_window = confirm_window_bars if entry_mode in ("failed_retest", "combined") else 0
        ma_period = trend_ma_period if require_trend_filter else 0
        return max(structural, macd_slow + macd_signal + 10, ma_period) + extra_window


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
        allow_flat_b2_b3 = p.get("chan_comp_allow_flat_b2_b3", getattr(cfg, "chan_comp_allow_flat_b2_b3", True))
        use_structural_stops = bool(p.get("chan_comp_use_structural_stops", getattr(cfg, "chan_comp_use_structural_stops", False)))
        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))

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
                causal=chan_causal,
            )
            stroke_sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                causal=chan_causal,
            )
            stroke_pivot_shift = stroke_sig["buy_signal"] & ~stroke_sig["divergence_buy"]
            first_buy = (sig["first_buy"] | stroke_sig["divergence_buy"]).reindex(master_index).fillna(False)
            second_buy = sig["second_buy"].reindex(master_index).fillna(False)
            third_buy = (sig["third_buy"] | stroke_pivot_shift).reindex(master_index).fillna(False)
            pivot_danger = _pivot_relation_danger_series(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)
            sell_signal = (sig["sell_signal"] | stroke_sig["sell_signal"]).reindex(master_index).fillna(False) | pivot_danger
            close = bars["Close"].reindex(master_index)
            low = bars["Low"].reindex(master_index) if "Low" in bars.columns else None
            high = bars["High"].reindex(master_index) if "High" in bars.columns else None

            structural_stop = None
            if use_structural_stops:
                p_df = _extract_pivot_and_stroke_series(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
                structural_stop = p_df["zg"].reindex(master_index).ffill()

            raw_weights[sym] = run_composite_position_loop(
                close=close,
                low=low,
                high=high,
                first_buy=first_buy,
                second_buy=second_buy,
                third_buy=third_buy,
                sell_signal=sell_signal,
                b1_w=b1_w,
                b2_w=b2_w,
                b3_w=b3_w,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
                allow_flat_b2_b3=allow_flat_b2_b3,
                structural_stop=structural_stop,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        max_single_pos = float(p.get("chan_comp_max_single_position", getattr(cfg, "chan_comp_max_single_position", 0.20)))
        min_weight_change = float(p.get("chan_comp_min_weight_change", getattr(cfg, "chan_comp_min_weight_change", 0.02)))

        if max_single_pos < 1.0 and risky_symbols:
            over_cap = (daily[risky_symbols] > max_single_pos)
            if over_cap.any().any():
                daily[risky_symbols] = np.minimum(daily[risky_symbols], max_single_pos)
                if cash_proxy in symbols:
                    daily[cash_proxy] = np.maximum(0.0, 1.0 - daily[risky_symbols].sum(axis=1))

        if min_weight_change > 0.0:
            daily = apply_asset_inertia(daily, min_weight_change=min_weight_change, cash_proxy=cash_proxy)

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
        from .chan_lesson_strategies import ChanPivotShiftMACDAdvStrategy
        return {
            "chan_pivot_shift": ChanPivotShiftStrategy(cfg),
            "chan_pivot_shift_macd": ChanPivotShiftMACDStrategy(cfg),
            "chan_pivot_shift_macd_adv": ChanPivotShiftMACDAdvStrategy(cfg),
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

        min_weight_change = float(p.get("chan_best_min_weight_change", getattr(cfg, "chan_best_min_weight_change", 0.02)))
        output_weights = _cap_and_deroute_to_cash(output_weights, symbols, cash_proxy)
        output_weights = _fill_out_columns(output_weights, symbols)
        return _sparse_from_daily(output_weights, min_weight_change=min_weight_change, cash_proxy=cash_proxy)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Best Selector Meta-Strategy (动态最佳缠论策略选择器): "
            f"evaluates 8 Chan strategies in parallel, evaluates rolling trailing {p.get('chan_best_metric', cfg.chan_best_metric)} "
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
        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))

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
                causal=chan_causal,
            )
            entry_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)
            low = bars["Low"].reindex(master_index) if "Low" in bars.columns else None
            high = bars["High"].reindex(master_index) if "High" in bars.columns else None

            chan_raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct,
                low=low, high=high,
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
                        if c_sum > 0.30:
                            c_weights = c_weights * (0.30 / c_sum)
                            c_sum = 0.30
                        def_alloc = max(0.0, 1.0 - c_sum)
                        for sym in symbols:
                            output_weights.loc[date, sym] = c_weights.get(sym, 0.0) + v_weights.get(sym, 0.0) * def_alloc
                    else:
                        c_weights = chan_scaled.loc[date] if date in chan_scaled.index else pd.Series(0.0, index=symbols)
                        c_sum = c_weights.sum()
                        for sym in symbols:
                            output_weights.loc[date, sym] = c_weights.get(sym, 0.0) * chan_weight + v_weights.get(sym, 0.0) * (1.0 - chan_weight * c_sum)

        # Scale down if total allocation exceeds 1.0, and route unallocated capital to cash_proxy
        risky_cols = [s for s in symbols if s != cash_proxy]
        current_cash = output_weights[cash_proxy].copy() if cash_proxy in symbols else 0.0
        risky_daily = output_weights[risky_cols].copy()

        total_alloc = risky_daily.sum(axis=1) + current_cash
        scale = np.where(total_alloc > 1.0, 1.0 / total_alloc, 1.0)
        risky_daily = risky_daily.mul(scale, axis=0)
        current_cash = current_cash * scale

        if cash_proxy in symbols:
            output_weights[risky_cols] = risky_daily
            output_weights[cash_proxy] = current_cash + np.maximum(0.0, 1.0 - (risky_daily.sum(axis=1) + current_cash))
        else:
            output_weights = risky_daily

        min_weight_change = float(p.get("chan_vaa_min_weight_change", getattr(cfg, "chan_vaa_min_weight_change", 0.02)))
        output_weights = _fill_out_columns(output_weights, symbols)
        return _sparse_from_daily(output_weights, min_weight_change=min_weight_change, cash_proxy=cash_proxy)

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


class ChanRiskManagedBlendStrategy(AllocationTemplate):
    """Chan Risk-Managed Blend Strategy (缠论风控混合配置策略):

    Institutional multi-strategy portfolio combining the top 3 walkforward-validated
    Chan strategies with strict institutional risk management and turnover controls:

    1. Core Sub-Strategy Allocation:
       - 40% `ChanVaaCompoundStrategy` (Rank 1: dual-momentum regime crash protection buffer & defensive anchor)
       - 40% `ChanThreeTypeStrategy` (Rank 2: segment-level pivot structural alpha)
       - 20% `ChanCompositeStrategy` (Rank 3: multi-stage B1/B2/B3 position scaling)

    2. Key Trading Rules & Risk Controls:
       - Concentration Cap: Hard cap of 20% NAV per individual stock (`crb_max_single_position = 0.20`),
         dynamically expanding up to 30% (`crb_bull_max_single_position = 0.30`) during bull breadth regimes.
       - Dual-Horizon Breadth Throttle & 10-Day Breadth Thrust:
         * Medium-term breadth: fraction of universe with Close > 50d SMA >= 30% (`crb_breadth_bull_thresh = 0.30`).
         * Short-term 10-day breadth thrust (`crb_thrust_lookback = 10`): fraction of universe with 10d ROC > 0 >= 60%
           (`crb_thrust_thresh = 0.60`). Fast rebound override immediately unlocks dynamic cash deployment.
       - Multi-Level Drawdown Circuit Breakers:
         * DD >= 10% from High-Water Mark: Halve equity allocation (50% risk damping, balance to cash).
         * DD >= 15% from High-Water Mark: Shift 100% of allocation to `ChanVaaCompoundStrategy` (which carries 70% cash buffer).
         * DD >= 20% from High-Water Mark: Hard stop — exit 100% to cash_proxy.
       - Turnover Filter:
         * Ignores rebalance shifts < 4% (`crb_min_weight_change = 0.04`), eliminating daily micro-rebalancing
           noise and preserving capital against A-share execution costs.
       - Sparse Weights Contract Compliance:
         * Guarantees explicit 0.0 for unheld assets, strictly adhering to workspace NaN-vs-0.0 rules.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="chan_risk_managed_blend",
            param_grid={},
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
            ],
        )

    def _get_sub_strategies(self, cfg: StrategyConfig) -> Dict[str, AllocationTemplate]:
        from .strategy import ChanThreeTypeStrategy
        return {
            "chan_composite": ChanCompositeStrategy(cfg),
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
        }

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        comp_w = float(p.get("crb_composite_weight", cfg.crb_composite_weight))
        three_w = float(p.get("crb_three_type_weight", cfg.crb_three_type_weight))
        vaa_w = float(p.get("crb_vaa_weight", cfg.crb_vaa_weight))
        max_single_pos = float(p.get("crb_max_single_position", cfg.crb_max_single_position))
        min_weight_change = float(p.get("crb_min_weight_change", cfg.crb_min_weight_change))
        dd_reduce_thresh = float(p.get("crb_dd_reduce_thresh", cfg.crb_dd_reduce_thresh))
        dd_defensive_thresh = float(p.get("crb_dd_defensive_thresh", cfg.crb_dd_defensive_thresh))
        dd_stop_thresh = float(p.get("crb_dd_stop_thresh", cfg.crb_dd_stop_thresh))
        dynamic_cash = bool(p.get("crb_dynamic_cash_deployment", getattr(cfg, "crb_dynamic_cash_deployment", True)))
        breadth_lookback = int(p.get("crb_breadth_lookback", getattr(cfg, "crb_breadth_lookback", 50)))
        breadth_bull_thresh = float(p.get("crb_breadth_bull_thresh", getattr(cfg, "crb_breadth_bull_thresh", 0.30)))
        thrust_lookback = int(p.get("crb_thrust_lookback", getattr(cfg, "crb_thrust_lookback", 10)))
        thrust_thresh = float(p.get("crb_thrust_thresh", getattr(cfg, "crb_thrust_thresh", 0.60)))
        target_bull_exposure = float(p.get("crb_target_bull_exposure", getattr(cfg, "crb_target_bull_exposure", 0.80)))
        bull_max_pos = float(p.get("crb_bull_max_single_position", getattr(cfg, "crb_bull_max_single_position", 0.30)))
        tier1_cooldown_bars = int(p.get("crb_tier1_cooldown_bars", getattr(cfg, "crb_tier1_cooldown_bars", 15)))

        tot_w = comp_w + three_w + vaa_w
        if tot_w > 0:
            comp_w /= tot_w
            three_w /= tot_w
            vaa_w /= tot_w

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        # Precompute daily universe market breadth (fraction > SMA50), 10-day breadth thrust, and 10-day ROC matrix
        breadth_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        thrust_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        roc_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        for sym in risky_symbols:
            if sym in universe and not universe[sym].empty:
                c = universe[sym]["Close"].reindex(master_index).ffill()
                ma = sma(c, breadth_lookback)
                breadth_matrix[sym] = (c > ma).astype(float)
                c_prev = c.shift(thrust_lookback)
                roc_thrust = (c / c_prev - 1.0)
                roc_matrix[sym] = roc_thrust
                thrust_matrix[sym] = (roc_thrust > 0.0).astype(float)
        daily_breadth = breadth_matrix.mean(axis=1).fillna(0.50)
        daily_thrust = thrust_matrix.mean(axis=1).fillna(0.50)

        # Run sub-strategies
        sub_strats = self._get_sub_strategies(cfg)

        w_comp_sparse = sub_strats["chan_composite"].generate_weights(universe, params)
        w_comp_daily = w_comp_sparse.reindex(master_index).ffill().fillna(0.0) if not w_comp_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_comp_daily = _fill_out_columns(w_comp_daily, symbols)

        w_three_sparse = sub_strats["chan_three_type"].generate_weights(universe, params)
        w_three_daily = w_three_sparse.reindex(master_index).ffill().fillna(0.0) if not w_three_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_three_daily = _fill_out_columns(w_three_daily, symbols)

        w_vaa_sparse = sub_strats["chan_vaa_compound"].generate_weights(universe, params)
        w_vaa_daily = w_vaa_sparse.reindex(master_index).ffill().fillna(0.0) if not w_vaa_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_vaa_daily = _fill_out_columns(w_vaa_daily, symbols)

        # Asset returns for tracking portfolio NAV and drawdown
        asset_returns = pd.DataFrame(0.0, index=master_index, columns=risky_symbols)
        for sym in risky_symbols:
            c = universe[sym]["Close"].reindex(master_index).ffill()
            asset_returns[sym] = c.pct_change().fillna(0.0)

        # Step through time to enforce drawdown circuit breakers, position caps, and turnover filters
        daily_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        cum_nav = 1.0
        peak_nav = 1.0
        nav_history = []
        tier1_counter = 0
        stop_counter = 0  # consecutive bars in Tier 3 full-stop
        stop_cooldown_bars = 21  # ~1 month before allowing re-entry
        current_held_w = pd.Series(0.0, index=symbols)
        if cash_proxy in symbols:
            current_held_w[cash_proxy] = 1.0

        for t in range(len(master_index)):
            date = master_index[t]

            # Update NAV based on positions held from previous day
            if t > 0:
                prev_date = master_index[t - 1]
                held_risky = daily_weights.loc[prev_date, risky_symbols]
                port_ret = float((held_risky * asset_returns.loc[date]).sum())
                cum_nav *= (1.0 + port_ret)
                if cum_nav > peak_nav:
                    peak_nav = cum_nav
            nav_history.append(cum_nav)

            # Fast-Recovery Override indicators (Recommendation 1)
            lookback_idx = max(0, len(nav_history) - 1 - 10)
            r_10d = (cum_nav / nav_history[lookback_idx] - 1.0) if len(nav_history) > 10 else 0.0
            thrust = float(daily_thrust.iloc[t])
            thrust_active = thrust >= thrust_thresh
            fast_recovery = thrust_active or (r_10d > 0.0)

            current_dd = (cum_nav - peak_nav) / peak_nav if peak_nav > 0 else 0.0
            dd_mag = abs(current_dd)

            # Auto-healing cooldown logic for Tier 3 and Tier 1/2 (Recommendation 2)
            if dd_mag >= dd_stop_thresh:
                stop_counter += 1
                tier1_counter = 0
                if stop_counter >= stop_cooldown_bars:
                    peak_nav = cum_nav
                    stop_counter = 0
                    current_dd = 0.0
                    dd_mag = 0.0
            elif dd_mag >= dd_reduce_thresh:
                tier1_counter += 1
                stop_counter = 0
                if tier1_counter >= tier1_cooldown_bars:
                    peak_nav = cum_nav
                    tier1_counter = 0
                    current_dd = 0.0
                    dd_mag = 0.0
            else:
                stop_counter = 0
                tier1_counter = 0

            # Circuit breaker logic with Fast-Recovery Override (Recommendation 1)
            if dd_mag >= dd_stop_thresh:
                if fast_recovery:
                    # Fast recovery: downgrade from 100% cash stop to defensive VAA
                    raw_w = w_vaa_daily.loc[date].copy()
                    is_emergency = not thrust_active
                else:
                    # Full stop: 100% cash
                    raw_w = pd.Series(0.0, index=symbols)
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = 1.0
                    is_emergency = True
            elif dd_mag >= dd_defensive_thresh:
                # Defensive mode: 100% into VAA compound strategy (carries cash buffer)
                raw_w = w_vaa_daily.loc[date].copy()
                is_emergency = not thrust_active if fast_recovery else True
            elif dd_mag >= dd_reduce_thresh:
                if fast_recovery:
                    # Fast recovery: restore full normal blend instead of 50% damping
                    raw_w = (
                        comp_w * w_comp_daily.loc[date] +
                        three_w * w_three_daily.loc[date] +
                        vaa_w * w_vaa_daily.loc[date]
                    ).copy()
                    is_emergency = False
                else:
                    # Risk reduction: 50% damping on equity exposure
                    raw_blend = (
                        comp_w * w_comp_daily.loc[date] +
                        three_w * w_three_daily.loc[date] +
                        vaa_w * w_vaa_daily.loc[date]
                    )
                    raw_w = pd.Series(0.0, index=symbols)
                    raw_w[risky_symbols] = raw_blend[risky_symbols] * 0.50
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = max(0.0, 1.0 - raw_w[risky_symbols].sum())
                    is_emergency = True
            else:
                # Normal blend
                raw_w = (
                    comp_w * w_comp_daily.loc[date] +
                    three_w * w_three_daily.loc[date] +
                    vaa_w * w_vaa_daily.loc[date]
                ).copy()
                is_emergency = False

            # Dynamic Cash Deployment: when breadth is bullish or 10-day breadth thrust triggers,
            # scale up high-conviction active risky holdings up to target_bull_exposure
            # and dynamically expand the single-position cap from max_single_pos to bull_max_pos.
            effective_cap = max_single_pos
            if not is_emergency and dynamic_cash:
                breadth = float(daily_breadth.iloc[t])
                bull_active = (breadth >= breadth_bull_thresh) or thrust_active

                if bull_active:
                    if thrust_active:
                        # 10-day Breadth Thrust Fast Override: rapid rebound detected, accelerate deployment
                        breadth_factor = 1.0
                    else:
                        breadth_factor = np.clip((breadth - breadth_bull_thresh) / max(0.01, 0.75 - breadth_bull_thresh), 0.0, 1.0)
                    target_exp = min(target_bull_exposure, 0.60 + breadth_factor * (target_bull_exposure - 0.60))
                    effective_cap = min(bull_max_pos, max_single_pos * (1.0 + 0.50 * breadth_factor)) if bull_max_pos > max_single_pos else max_single_pos

                    active_risky = [s for s in risky_symbols if raw_w[s] > 1e-6]
                    tot_active = float(raw_w[active_risky].sum())
                    if tot_active > 0 and target_exp > tot_active:
                        scale = target_exp / tot_active
                        raw_w[active_risky] = (raw_w[active_risky] * scale).clip(upper=effective_cap)

                    # Recommendation 3: Pre-Emptive Breadth Thrust Cash Deployment
                    # When a breadth thrust occurs, sub-strategies may have 0 or few active buy signals.
                    # If total active risky allocation is still below target_exp, deploy the unallocated
                    # exposure into the leading momentum assets that triggered the breadth thrust.
                    tot_active = float(raw_w[risky_symbols].sum())
                    if thrust_active and tot_active < target_exp:
                        unallocated = target_exp - tot_active
                        row_roc = roc_matrix.iloc[t]
                        cand_rocs = {s: float(row_roc[s]) for s in risky_symbols if pd.notna(row_roc[s]) and row_roc[s] > 0.0}
                        sorted_cands = sorted(cand_rocs.keys(), key=lambda s: cand_rocs[s], reverse=True)
                        for cand in sorted_cands:
                            if unallocated <= 1e-6:
                                break
                            current_w = float(raw_w[cand])
                            space = max(0.0, effective_cap - current_w)
                            if space > 0.01:
                                alloc = min(space, unallocated)
                                raw_w[cand] = current_w + alloc
                                unallocated -= alloc

            # Apply hard position cap per risky symbol (dynamically expanded in bull breadth)
            risky_w = raw_w[risky_symbols].copy().clip(lower=0.0, upper=effective_cap)

            # Ensure total risky allocation <= 1.0
            tot_risky = float(risky_w.sum())
            if tot_risky > 1.0:
                risky_w = risky_w / tot_risky
                tot_risky = 1.0

            ideal_target_w = pd.Series(0.0, index=symbols)
            ideal_target_w[risky_symbols] = risky_w
            if cash_proxy in symbols:
                ideal_target_w[cash_proxy] = max(0.0, 1.0 - tot_risky)

            if t == 0:
                current_held_w = ideal_target_w.copy()
                daily_weights.loc[date] = ideal_target_w
                continue

            # Option A: Asset-Level Inertia Filtering
            diff = ideal_target_w[risky_symbols] - current_held_w[risky_symbols]
            sells = diff[diff <= -min_weight_change].index.tolist()
            buys = diff[diff >= min_weight_change].index.tolist()

            if is_emergency:
                # Emergency circuit breaker override: liquidate/de-risk without threshold lag
                sells = [s for s in risky_symbols if ideal_target_w[s] < current_held_w[s] and abs(ideal_target_w[s] - current_held_w[s]) >= 0.001]
                buys = [s for s in risky_symbols if ideal_target_w[s] > current_held_w[s] and abs(ideal_target_w[s] - current_held_w[s]) >= min_weight_change]

            if not sells and not buys:
                # No asset changed >= min_weight_change: keep prior target weights (no rebalance)
                daily_weights.loc[date] = current_held_w.copy()
            else:
                new_target = current_held_w.copy()
                # 1. Execute sells first to release cash capacity
                for s in sells:
                    new_target[s] = ideal_target_w[s]

                # 2. Execute buys up to available capacity without diluting untouched assets
                non_buy_risky = [s for s in risky_symbols if s not in buys]
                avail_cap = max(0.0, 1.0 - float(new_target[non_buy_risky].sum()))

                buys_sorted = sorted(buys, key=lambda b: diff[b], reverse=True)
                for b in buys_sorted:
                    ideal_b = ideal_target_w[b]
                    buy_target = min(ideal_b, avail_cap)

                    if buy_target - current_held_w[b] >= min_weight_change:
                        new_target[b] = buy_target
                        avail_cap = max(0.0, avail_cap - buy_target)
                    else:
                        new_target[b] = current_held_w[b]

                if cash_proxy in symbols:
                    new_target[cash_proxy] = max(0.0, 1.0 - float(new_target[risky_symbols].sum()))

                daily_weights.loc[date] = new_target
                current_held_w = new_target.copy()

        daily_weights = _fill_out_columns(daily_weights, symbols)
        return _sparse_from_daily(daily_weights)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        comp_w = p.get("crb_composite_weight", cfg.crb_composite_weight)
        three_w = p.get("crb_three_type_weight", cfg.crb_three_type_weight)
        vaa_w = p.get("crb_vaa_weight", cfg.crb_vaa_weight)
        max_pos = p.get("crb_max_single_position", cfg.crb_max_single_position)
        dd_red = p.get("crb_dd_reduce_thresh", cfg.crb_dd_reduce_thresh)
        dd_def = p.get("crb_dd_defensive_thresh", cfg.crb_dd_defensive_thresh)
        dd_stop = p.get("crb_dd_stop_thresh", cfg.crb_dd_stop_thresh)
        tier1_cd = p.get("crb_tier1_cooldown_bars", getattr(cfg, "crb_tier1_cooldown_bars", 15))
        min_chg = p.get("crb_min_weight_change", cfg.crb_min_weight_change)
        dyn_cash = p.get("crb_dynamic_cash_deployment", getattr(cfg, "crb_dynamic_cash_deployment", True))
        b_thresh = p.get("crb_breadth_bull_thresh", getattr(cfg, "crb_breadth_bull_thresh", 0.30))
        t_lookback = p.get("crb_thrust_lookback", getattr(cfg, "crb_thrust_lookback", 10))
        return (
            f"Chan Risk-Managed Blend Strategy (chan_risk_managed_blend): "
            f"walkforward-optimized ensemble blending chan_vaa_compound ({vaa_w:.0%}), "
            f"chan_three_type ({three_w:.0%}), and chan_composite ({comp_w:.0%}) "
            f"with hard position cap ({max_pos:.0%} max per stock), "
            f"drawdown circuit breakers (halve equity at {dd_red:.0%}, defensive VAA at {dd_def:.0%}, stop at {dd_stop:.0%} with fast recovery & {tier1_cd}d auto-heal), "
            f"turnover filter (min trade change {min_chg:.0%}), "
            f"and {'dynamic cash deployment in bull breadth (>=' + f'{b_thresh:.0%}' + f' or {t_lookback}d thrust)' if dyn_cash else 'static cash buffer'}."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252


class ChanFourStateBlendStrategy(AllocationTemplate):
    """Chan Four-State Risk-Managed Blend Strategy (缠论四态风控混合配置策略):

    Institutional multi-strategy portfolio based on `ChanRiskManagedBlendStrategy`, replacing
    `ChanCompositeStrategy` with the 4-state operational execution machine (`ChanFourStateExecutionStrategy`):

    1. Core Sub-Strategy Allocation:
       - 40% `ChanVaaCompoundStrategy` (Rank 1: dual-momentum regime crash protection buffer & defensive anchor)
       - 40% `ChanThreeTypeStrategy` (Rank 2: segment-level pivot structural alpha)
       - 20% `ChanFourStateExecutionStrategy` (Rank 3: 4-state operational machine with 5-bar gestation buffer,
         1% ZG tolerance, moving average entanglement filter, and Lesson 16 stagnation avoidance)

    2. Key Trading Rules & Risk Controls:
       - Concentration Cap: Hard cap of 20% NAV per individual stock (`cfsb_max_single_position = 0.20`),
         dynamically expanding up to 30% (`cfsb_bull_max_single_position = 0.30`) during bull breadth regimes.
       - Dual-Horizon Breadth Throttle & 10-Day Breadth Thrust:
         * Medium-term breadth: fraction of universe with Close > 50d SMA >= 30% (`cfsb_breadth_bull_thresh = 0.30`).
         * Short-term 10-day breadth thrust (`cfsb_thrust_lookback = 10`): fraction of universe with 10d ROC > 0 >= 60%
           (`cfsb_thrust_thresh = 0.60`). Fast rebound override immediately unlocks dynamic cash deployment.
       - Multi-Level Drawdown Circuit Breakers:
         * DD >= 10% from High-Water Mark: Halve equity allocation (50% risk damping, balance to cash).
         * DD >= 15% from High-Water Mark: Shift 100% of allocation to `ChanVaaCompoundStrategy` (which carries 70% cash buffer).
         * DD >= 20% from High-Water Mark: Hard stop — exit 100% to cash_proxy (21-bar cooldown).
       - Turnover Filter:
         * Ignores rebalance shifts < 4% (`cfsb_min_weight_change = 0.04`), eliminating daily micro-rebalancing
           noise and preserving capital against execution costs.
       - Sparse Weights Contract Compliance:
         * Guarantees explicit 0.0 for unheld assets, strictly adhering to workspace NaN-vs-0.0 rules.

    DISCLOSED ZERO EXTERNAL DEPENDENCY:
    100% self-contained within this workspace, built strictly on native primitives.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="chan_four_state_blend",
            param_grid={},
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
            ],
        )

    def _get_sub_strategies(self, cfg: StrategyConfig) -> Dict[str, AllocationTemplate]:
        from .strategy import ChanThreeTypeStrategy
        return {
            "chan_four_state": ChanFourStateExecutionStrategy(cfg),
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
        }

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        fse_w = float(p.get("cfsb_four_state_weight", getattr(cfg, "cfsb_four_state_weight", 0.20)))
        three_w = float(p.get("cfsb_three_type_weight", getattr(cfg, "cfsb_three_type_weight", 0.40)))
        vaa_w = float(p.get("cfsb_vaa_weight", getattr(cfg, "cfsb_vaa_weight", 0.40)))
        max_single_pos = float(p.get("cfsb_max_single_position", getattr(cfg, "cfsb_max_single_position", 0.20)))
        min_weight_change = float(p.get("cfsb_min_weight_change", getattr(cfg, "cfsb_min_weight_change", 0.04)))
        dd_reduce_thresh = float(p.get("cfsb_dd_reduce_thresh", getattr(cfg, "cfsb_dd_reduce_thresh", 0.10)))
        dd_defensive_thresh = float(p.get("cfsb_dd_defensive_thresh", getattr(cfg, "cfsb_dd_defensive_thresh", 0.15)))
        dd_stop_thresh = float(p.get("cfsb_dd_stop_thresh", getattr(cfg, "cfsb_dd_stop_thresh", 0.20)))
        dynamic_cash = bool(p.get("cfsb_dynamic_cash_deployment", getattr(cfg, "cfsb_dynamic_cash_deployment", True)))
        breadth_lookback = int(p.get("cfsb_breadth_lookback", getattr(cfg, "cfsb_breadth_lookback", 50)))
        breadth_bull_thresh = float(p.get("cfsb_breadth_bull_thresh", getattr(cfg, "cfsb_breadth_bull_thresh", 0.30)))
        thrust_lookback = int(p.get("cfsb_thrust_lookback", getattr(cfg, "cfsb_thrust_lookback", 10)))
        thrust_thresh = float(p.get("cfsb_thrust_thresh", getattr(cfg, "cfsb_thrust_thresh", 0.60)))
        target_bull_exposure = float(p.get("cfsb_target_bull_exposure", getattr(cfg, "cfsb_target_bull_exposure", 0.80)))
        bull_max_pos = float(p.get("cfsb_bull_max_single_position", getattr(cfg, "cfsb_bull_max_single_position", 0.30)))
        tier1_cooldown_bars = int(p.get("cfsb_tier1_cooldown_bars", getattr(cfg, "cfsb_tier1_cooldown_bars", 15)))

        tot_w = fse_w + three_w + vaa_w
        if tot_w > 0:
            fse_w /= tot_w
            three_w /= tot_w
            vaa_w /= tot_w

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        # Precompute daily universe market breadth (fraction > SMA50), 10-day breadth thrust, and 10-day ROC matrix
        breadth_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        thrust_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        roc_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        for sym in risky_symbols:
            if sym in universe and not universe[sym].empty:
                c = universe[sym]["Close"].reindex(master_index).ffill()
                ma = sma(c, breadth_lookback)
                breadth_matrix[sym] = (c > ma).astype(float)
                c_prev = c.shift(thrust_lookback)
                roc_thrust = (c / c_prev - 1.0)
                roc_matrix[sym] = roc_thrust
                thrust_matrix[sym] = (roc_thrust > 0.0).astype(float)
        daily_breadth = breadth_matrix.mean(axis=1).fillna(0.50)
        daily_thrust = thrust_matrix.mean(axis=1).fillna(0.50)

        # Run sub-strategies
        sub_strats = self._get_sub_strategies(cfg)

        w_fse_sparse = sub_strats["chan_four_state"].generate_weights(universe, params)
        w_fse_daily = w_fse_sparse.reindex(master_index).ffill().fillna(0.0) if not w_fse_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_fse_daily = _fill_out_columns(w_fse_daily, symbols)

        w_three_sparse = sub_strats["chan_three_type"].generate_weights(universe, params)
        w_three_daily = w_three_sparse.reindex(master_index).ffill().fillna(0.0) if not w_three_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_three_daily = _fill_out_columns(w_three_daily, symbols)

        w_vaa_sparse = sub_strats["chan_vaa_compound"].generate_weights(universe, params)
        w_vaa_daily = w_vaa_sparse.reindex(master_index).ffill().fillna(0.0) if not w_vaa_sparse.empty else pd.DataFrame(0.0, index=master_index, columns=symbols)
        w_vaa_daily = _fill_out_columns(w_vaa_daily, symbols)

        # Asset returns for tracking portfolio NAV and drawdown
        asset_returns = pd.DataFrame(0.0, index=master_index, columns=risky_symbols)
        for sym in risky_symbols:
            c = universe[sym]["Close"].reindex(master_index).ffill()
            asset_returns[sym] = c.pct_change().fillna(0.0)

        # Step through time to enforce drawdown circuit breakers, position caps, and turnover filters
        daily_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        cum_nav = 1.0
        peak_nav = 1.0
        nav_history = []
        tier1_counter = 0
        stop_counter = 0  # consecutive bars in Tier 3 full-stop
        stop_cooldown_bars = 21  # ~1 month before allowing re-entry
        current_held_w = pd.Series(0.0, index=symbols)
        if cash_proxy in symbols:
            current_held_w[cash_proxy] = 1.0

        for t in range(len(master_index)):
            date = master_index[t]

            # Update NAV based on positions held from previous day
            if t > 0:
                prev_date = master_index[t - 1]
                held_risky = daily_weights.loc[prev_date, risky_symbols]
                port_ret = float((held_risky * asset_returns.loc[date]).sum())
                cum_nav *= (1.0 + port_ret)
                if cum_nav > peak_nav:
                    peak_nav = cum_nav
            nav_history.append(cum_nav)

            # Fast-Recovery Override indicators (Recommendation 1)
            lookback_idx = max(0, len(nav_history) - 1 - 10)
            r_10d = (cum_nav / nav_history[lookback_idx] - 1.0) if len(nav_history) > 10 else 0.0
            thrust = float(daily_thrust.iloc[t])
            thrust_active = thrust >= thrust_thresh
            fast_recovery = thrust_active or (r_10d > 0.0)

            current_dd = (cum_nav - peak_nav) / peak_nav if peak_nav > 0 else 0.0
            dd_mag = abs(current_dd)

            # Auto-healing cooldown logic for Tier 3 and Tier 1/2 (Recommendation 2)
            if dd_mag >= dd_stop_thresh:
                stop_counter += 1
                tier1_counter = 0
                if stop_counter >= stop_cooldown_bars:
                    peak_nav = cum_nav
                    stop_counter = 0
                    current_dd = 0.0
                    dd_mag = 0.0
            elif dd_mag >= dd_reduce_thresh:
                tier1_counter += 1
                stop_counter = 0
                if tier1_counter >= tier1_cooldown_bars:
                    peak_nav = cum_nav
                    tier1_counter = 0
                    current_dd = 0.0
                    dd_mag = 0.0
            else:
                stop_counter = 0
                tier1_counter = 0

            # Circuit breaker logic with Fast-Recovery Override (Recommendation 1)
            if dd_mag >= dd_stop_thresh:
                if fast_recovery:
                    # Fast recovery: downgrade from 100% cash stop to defensive VAA
                    raw_w = w_vaa_daily.loc[date].copy()
                    is_emergency = not thrust_active
                else:
                    # Full stop: 100% cash
                    raw_w = pd.Series(0.0, index=symbols)
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = 1.0
                    is_emergency = True
            elif dd_mag >= dd_defensive_thresh:
                # Defensive mode: 100% into VAA compound strategy (carries cash buffer)
                raw_w = w_vaa_daily.loc[date].copy()
                is_emergency = not thrust_active if fast_recovery else True
            elif dd_mag >= dd_reduce_thresh:
                if fast_recovery:
                    # Fast recovery: restore full normal blend instead of 50% damping
                    raw_w = (
                        fse_w * w_fse_daily.loc[date] +
                        three_w * w_three_daily.loc[date] +
                        vaa_w * w_vaa_daily.loc[date]
                    ).copy()
                    is_emergency = False
                else:
                    # Risk reduction: 50% damping on equity exposure
                    raw_blend = (
                        fse_w * w_fse_daily.loc[date] +
                        three_w * w_three_daily.loc[date] +
                        vaa_w * w_vaa_daily.loc[date]
                    )
                    raw_w = pd.Series(0.0, index=symbols)
                    raw_w[risky_symbols] = raw_blend[risky_symbols] * 0.50
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = max(0.0, 1.0 - raw_w[risky_symbols].sum())
                    is_emergency = True
            else:
                # Normal blend
                raw_w = (
                    fse_w * w_fse_daily.loc[date] +
                    three_w * w_three_daily.loc[date] +
                    vaa_w * w_vaa_daily.loc[date]
                ).copy()
                is_emergency = False

            # Dynamic Cash Deployment: when breadth is bullish or 10-day breadth thrust triggers,
            # scale up high-conviction active risky holdings up to target_bull_exposure
            # and dynamically expand the single-position cap from max_single_pos to bull_max_pos.
            effective_cap = max_single_pos
            if not is_emergency and dynamic_cash:
                breadth = float(daily_breadth.iloc[t])
                bull_active = (breadth >= breadth_bull_thresh) or thrust_active

                if bull_active:
                    if thrust_active:
                        # 10-day Breadth Thrust Fast Override: rapid rebound detected, accelerate deployment
                        breadth_factor = 1.0
                    else:
                        breadth_factor = np.clip((breadth - breadth_bull_thresh) / max(0.01, 0.75 - breadth_bull_thresh), 0.0, 1.0)
                    target_exp = min(target_bull_exposure, 0.60 + breadth_factor * (target_bull_exposure - 0.60))
                    effective_cap = min(bull_max_pos, max_single_pos * (1.0 + 0.50 * breadth_factor)) if bull_max_pos > max_single_pos else max_single_pos

                    active_risky = [s for s in risky_symbols if raw_w[s] > 1e-6]
                    tot_active = float(raw_w[active_risky].sum())
                    if tot_active > 0 and target_exp > tot_active:
                        scale = target_exp / tot_active
                        raw_w[active_risky] = (raw_w[active_risky] * scale).clip(upper=effective_cap)

                    # Recommendation 3: Pre-Emptive Breadth Thrust Cash Deployment
                    # When a breadth thrust occurs, sub-strategies may have 0 or few active buy signals.
                    # If total active risky allocation is still below target_exp, deploy the unallocated
                    # exposure into the leading momentum assets that triggered the breadth thrust.
                    tot_active = float(raw_w[risky_symbols].sum())
                    if thrust_active and tot_active < target_exp:
                        unallocated = target_exp - tot_active
                        row_roc = roc_matrix.iloc[t]
                        cand_rocs = {s: float(row_roc[s]) for s in risky_symbols if pd.notna(row_roc[s]) and row_roc[s] > 0.0}
                        sorted_cands = sorted(cand_rocs.keys(), key=lambda s: cand_rocs[s], reverse=True)
                        for cand in sorted_cands:
                            if unallocated <= 1e-6:
                                break
                            current_w = float(raw_w[cand])
                            space = max(0.0, effective_cap - current_w)
                            if space > 0.01:
                                alloc = min(space, unallocated)
                                raw_w[cand] = current_w + alloc
                                unallocated -= alloc

            # Apply hard position cap per risky symbol (dynamically expanded in bull breadth)
            risky_w = raw_w[risky_symbols].copy().clip(lower=0.0, upper=effective_cap)

            # Ensure total risky allocation <= 1.0
            tot_risky = float(risky_w.sum())
            if tot_risky > 1.0:
                risky_w = risky_w / tot_risky
                tot_risky = 1.0

            ideal_target_w = pd.Series(0.0, index=symbols)
            ideal_target_w[risky_symbols] = risky_w
            if cash_proxy in symbols:
                ideal_target_w[cash_proxy] = max(0.0, 1.0 - tot_risky)

            if t == 0:
                current_held_w = ideal_target_w.copy()
                daily_weights.loc[date] = ideal_target_w
                continue

            # Option A: Asset-Level Inertia Filtering
            diff = ideal_target_w[risky_symbols] - current_held_w[risky_symbols]
            sells = diff[diff <= -min_weight_change].index.tolist()
            buys = diff[diff >= min_weight_change].index.tolist()

            if is_emergency:
                # Emergency circuit breaker override: liquidate/de-risk without threshold lag
                sells = [s for s in risky_symbols if ideal_target_w[s] < current_held_w[s] and abs(ideal_target_w[s] - current_held_w[s]) >= 0.001]
                buys = [s for s in risky_symbols if ideal_target_w[s] > current_held_w[s] and abs(ideal_target_w[s] - current_held_w[s]) >= min_weight_change]

            if not sells and not buys:
                # No asset changed >= min_weight_change: keep prior target weights (no rebalance)
                daily_weights.loc[date] = current_held_w.copy()
            else:
                new_target = current_held_w.copy()
                # 1. Execute sells first to release cash capacity
                for s in sells:
                    new_target[s] = ideal_target_w[s]

                # 2. Execute buys up to available capacity without diluting untouched assets
                non_buy_risky = [s for s in risky_symbols if s not in buys]
                avail_cap = max(0.0, 1.0 - float(new_target[non_buy_risky].sum()))

                buys_sorted = sorted(buys, key=lambda b: diff[b], reverse=True)
                for b in buys_sorted:
                    ideal_b = ideal_target_w[b]
                    buy_target = min(ideal_b, avail_cap)

                    if buy_target - current_held_w[b] >= min_weight_change:
                        new_target[b] = buy_target
                        avail_cap = max(0.0, avail_cap - buy_target)
                    else:
                        new_target[b] = current_held_w[b]

                if cash_proxy in symbols:
                    new_target[cash_proxy] = max(0.0, 1.0 - float(new_target[risky_symbols].sum()))

                daily_weights.loc[date] = new_target
                current_held_w = new_target.copy()

        daily_weights = _fill_out_columns(daily_weights, symbols)
        return _sparse_from_daily(daily_weights)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        fse_w = p.get("cfsb_four_state_weight", getattr(cfg, "cfsb_four_state_weight", 0.20))
        three_w = p.get("cfsb_three_type_weight", getattr(cfg, "cfsb_three_type_weight", 0.40))
        vaa_w = p.get("cfsb_vaa_weight", getattr(cfg, "cfsb_vaa_weight", 0.40))
        max_pos = p.get("cfsb_max_single_position", getattr(cfg, "cfsb_max_single_position", 0.20))
        dd_red = p.get("cfsb_dd_reduce_thresh", getattr(cfg, "cfsb_dd_reduce_thresh", 0.10))
        dd_def = p.get("cfsb_dd_defensive_thresh", getattr(cfg, "cfsb_dd_defensive_thresh", 0.15))
        dd_stop = p.get("cfsb_dd_stop_thresh", getattr(cfg, "cfsb_dd_stop_thresh", 0.20))
        tier1_cd = p.get("cfsb_tier1_cooldown_bars", getattr(cfg, "cfsb_tier1_cooldown_bars", 15))
        min_chg = p.get("cfsb_min_weight_change", getattr(cfg, "cfsb_min_weight_change", 0.04))
        dyn_cash = p.get("cfsb_dynamic_cash_deployment", getattr(cfg, "cfsb_dynamic_cash_deployment", True))
        b_thresh = p.get("cfsb_breadth_bull_thresh", getattr(cfg, "cfsb_breadth_bull_thresh", 0.30))
        t_lookback = p.get("cfsb_thrust_lookback", getattr(cfg, "cfsb_thrust_lookback", 10))
        return (
            f"Chan Four-State Risk-Managed Blend Strategy (chan_four_state_blend): "
            f"institutional ensemble blending chan_vaa_compound ({vaa_w:.0%}), "
            f"chan_three_type ({three_w:.0%}), and chan_four_state_execution ({fse_w:.0%}) "
            f"with hard position cap ({max_pos:.0%} max per stock), "
            f"drawdown circuit breakers (halve equity at {dd_red:.0%}, defensive VAA at {dd_def:.0%}, stop at {dd_stop:.0%} with fast recovery & {tier1_cd}d auto-heal), "
            f"turnover filter (min trade change {min_chg:.0%}), "
            f"and {'dynamic cash deployment in bull breadth (>=' + f'{b_thresh:.0%}' + f' or {t_lookback}d thrust)' if dyn_cash else 'static cash buffer'}."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252


class ChanFourStateExecutionStrategy(AllocationTemplate):
    """Chan Four-State Operational Machine Strategy (缠论四态操作判定机策略 / 中小资金拒绝盘整):
    Industrial-grade execution strategy inspired by 缠中说禅 (Chan Theory) Lessons 11-14, 16, 20, 53,
    and the production chanlun-engine-skill architecture.

    Implements a deterministic 4-state operational finite state machine (FSM):
    1. BUY_CANDIDATE (买入候选): On fresh 1B (底背驰), 2B (次低点回抽), or 3B (中枢突破回踩) signals.
       Enforces deterministic structural invalidation stops (Table 3 in 108 lessons):
       - 3B: Invalidation is strictly ZG (pivot high). Re-entering pivot disproves breakout.
       - 2B: Invalidation is prior swing low DD. Breaking below proves downtrend extension.
       - 1B: Invalidation is 1B fractal bar low. Breaking below proves divergence extension.
       - Bound by hard fallback stop-loss (default 8%).
    2. HOLD (持有): When price trades above the pivot (above_zs, P > ZG) AND stroke direction is UP.
       Trailing stop is ratcheted upward to ZG, mechanically locking in breakout profits.
    3. HOLD_ALERT (持有/警惕): When price is above_zs but current stroke turns DOWN (minor pullback)
       or moving averages (MA5/MA20) enter an entanglement (spread < 2%, 11-14课"吻").
       Immediately tightens stop loss strictly to ZG.
    4. SELL_EXIT (卖出/减仓): Triggered by formal Chan sell points (1S/2S/3S), breach of structural
       invalidation stop, Lesson 92-99 dangerous pivot penetration, or holding timeout.
    5. WAIT_OBSERVE (等待观察 - Lesson 16 中小资金高效操作法):
       When price is in_zs (zd <= P <= zg) or below_zs without a buy point, allocation is 0.0,
       sweeping 100% of idle capital to Cash Proxy (BIL). Small and medium funds strictly refuse
       to participate in consolidation drag or knife-catching.
       When exit_on_consolidation=True, existing positions that drop into consolidation are exited
       immediately, eliminating consolidation drawdown.

    DISCLOSED ZERO EXTERNAL DEPENDENCY:
    This implementation is 100% self-contained within this workspace, built strictly on
    native primitives (`common.allocation_templates`, `common.indicators`, `rs/chan_structure.py`,
    `rs/chan_signals.py`). It does NOT import, reference, link to, or depend on any third-party
    code, libraries, or external repositories (including `/home/stone/Work/third-party/chan`,
    `chanpy`, or `czsc`).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_four_state_execution", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan_fse_min_gap_bars", getattr(cfg, "chan_fse_min_gap_bars", 4))
        min_strokes = p.get("chan_fse_min_strokes", getattr(cfg, "chan_fse_min_strokes", 3))
        macd_fast = p.get("chan_fse_macd_fast", getattr(cfg, "chan_fse_macd_fast", 12))
        macd_slow = p.get("chan_fse_macd_slow", getattr(cfg, "chan_fse_macd_slow", 26))
        macd_signal = p.get("chan_fse_macd_signal", getattr(cfg, "chan_fse_macd_signal", 9))
        stop_loss_pct = p.get("chan_fse_stop_loss_pct", getattr(cfg, "chan_fse_stop_loss_pct", 0.08))
        max_holding_days = p.get("chan_fse_max_holding_days", getattr(cfg, "chan_fse_max_holding_days", 90))
        exit_on_consolidation = p.get("chan_fse_exit_on_consolidation", getattr(cfg, "chan_fse_exit_on_consolidation", True))
        trail_stop_to_zg = p.get("chan_fse_trail_stop_to_zg", getattr(cfg, "chan_fse_trail_stop_to_zg", True))
        use_ma_filter = p.get("chan_fse_use_ma_filter", getattr(cfg, "chan_fse_use_ma_filter", True))
        max_single_pos = float(p.get("chan_fse_max_single_position", getattr(cfg, "chan_fse_max_single_position", 0.20)))
        min_weight_change = float(p.get("chan_fse_min_weight_change", getattr(cfg, "chan_fse_min_weight_change", 0.04)))
        min_hold_bars = int(p.get("chan_fse_min_hold_bars", getattr(cfg, "chan_fse_min_hold_bars", 5)))
        zg_tolerance_pct = float(p.get("chan_fse_zg_tolerance_pct", getattr(cfg, "chan_fse_zg_tolerance_pct", 0.025)))
        cons_timeout_bars = int(p.get("chan_fse_cons_timeout_bars", getattr(cfg, "chan_fse_cons_timeout_bars", 8)))
        chan_causal = bool(p.get("chan_causal_signals", getattr(cfg, "chan_causal_signals", True)))
        stop_eval_mode = str(p.get("chan_fse_stop_evaluation_mode", getattr(cfg, "chan_fse_stop_evaluation_mode", "close")))
        b1_buffer_pct = float(p.get("chan_fse_b1_buffer_pct", getattr(cfg, "chan_fse_b1_buffer_pct", 0.03)))
        cooldown_bars = int(p.get("chan_fse_cooldown_bars", getattr(cfg, "chan_fse_cooldown_bars", 4)))
        two_stage_entry = bool(p.get("chan_fse_two_stage_entry", getattr(cfg, "chan_fse_two_stage_entry", True)))
        use_breadth_filter = bool(p.get("chan_fse_use_breadth_filter", getattr(cfg, "chan_fse_use_breadth_filter", True)))
        breadth_bull_thresh = float(p.get("chan_fse_breadth_bull_thresh", getattr(cfg, "chan_fse_breadth_bull_thresh", 0.30)))
        adx_filter = bool(p.get("chan_fse_adx_filter", getattr(cfg, "chan_fse_adx_filter", True)))
        adx_threshold = float(p.get("chan_fse_adx_threshold", getattr(cfg, "chan_fse_adx_threshold", 20.0)))
        adx_period = int(p.get("chan_fse_adx_period", getattr(cfg, "chan_fse_adx_period", 14)))

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
                causal=chan_causal,
            )
            stroke_sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                causal=chan_causal,
            )
            stroke_pivot_shift = stroke_sig["buy_signal"] & ~stroke_sig["divergence_buy"]
            first_buy = (sig["first_buy"] | stroke_sig["divergence_buy"]).reindex(master_index).fillna(False)
            second_buy = sig["second_buy"].reindex(master_index).fillna(False)
            third_buy = (sig["third_buy"] | stroke_pivot_shift).reindex(master_index).fillna(False)
            pivot_danger = _pivot_relation_danger_series(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)
            sell_signal = (sig["sell_signal"] | stroke_sig["sell_signal"]).reindex(master_index).fillna(False) | pivot_danger

            # ADX Trend Strength Gate (Recommendation 5):
            # Suppress new buy signals during choppy / ranging periods where ADX < adx_threshold.
            if adx_filter and "High" in bars.columns and "Low" in bars.columns and len(bars) >= adx_period:
                adx_series = adx(bars, period=adx_period).reindex(master_index).ffill().fillna(0.0)
                trend_ok = adx_series >= adx_threshold
                first_buy = first_buy & trend_ok
                second_buy = second_buy & trend_ok
                third_buy = third_buy & trend_ok

            p_df = _extract_pivot_and_stroke_series(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
            zg = p_df["zg"].reindex(master_index).ffill()
            zd = p_df["zd"].reindex(master_index).ffill()
            dd = p_df["dd"].reindex(master_index).ffill()
            stroke_dir = p_df["stroke_dir"].reindex(master_index).ffill().fillna(0)

            close = bars["Close"].reindex(master_index)
            low = bars["Low"].reindex(master_index) if "Low" in bars.columns else None
            high = bars["High"].reindex(master_index) if "High" in bars.columns else None
            ma5 = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()

            raw_weights[sym] = run_four_state_position_loop(
                close=close,
                first_buy=first_buy,
                second_buy=second_buy,
                third_buy=third_buy,
                sell_signal=sell_signal,
                zg=zg,
                zd=zd,
                dd=dd,
                stroke_dir=stroke_dir,
                ma5=ma5,
                ma20=ma20,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
                position_size_pct=1.0,
                exit_on_consolidation=exit_on_consolidation,
                trail_stop_to_zg=trail_stop_to_zg,
                use_ma_filter=use_ma_filter,
                low=low,
                high=high,
                min_hold_bars=min_hold_bars,
                zg_tolerance_pct=zg_tolerance_pct,
                consolidation_timeout_bars=cons_timeout_bars,
                stop_evaluation_mode=stop_eval_mode,
                b1_buffer_pct=b1_buffer_pct,
                cooldown_bars=cooldown_bars,
                two_stage_entry=two_stage_entry,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)

        # Cap individual stock positions at max_single_pos
        if max_single_pos < 1.0 and risky_symbols:
            daily[risky_symbols] = np.minimum(daily[risky_symbols], max_single_pos)

        # Market breadth regime filter (throttle entries during severe macro bear crash)
        if use_breadth_filter and risky_symbols:
            breadth_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
            for sym in risky_symbols:
                if sym in universe and not universe[sym].empty:
                    c = universe[sym]["Close"].reindex(master_index).ffill()
                    ma50 = sma(c, 50)
                    breadth_matrix[sym] = (c > ma50).astype(float)
            daily_breadth = breadth_matrix.mean(axis=1).fillna(0.50)
            bear_regime = daily_breadth < breadth_bull_thresh
            if bear_regime.any():
                daily.loc[bear_regime, risky_symbols] = 0.0

        # Ensure total risky allocation <= 1.0
        tot_risky = daily[risky_symbols].sum(axis=1)
        over_invested = tot_risky > 1.0
        if over_invested.any():
            daily.loc[over_invested, risky_symbols] = daily.loc[over_invested, risky_symbols].div(tot_risky[over_invested], axis=0)

        # Route unallocated funds to cash proxy (e.g. BIL)
        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily[risky_symbols].sum(axis=1))

        # Apply asset-level inertia filter to eliminate churn
        if min_weight_change > 0.0:
            daily = apply_asset_inertia(daily, min_weight_change=min_weight_change, cash_proxy=cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        max_pos = float(p.get("chan_fse_max_single_position", getattr(cfg, "chan_fse_max_single_position", 0.20)))
        stop_loss = float(p.get("chan_fse_stop_loss_pct", getattr(cfg, "chan_fse_stop_loss_pct", 0.08)))
        exit_cons = bool(p.get("chan_fse_exit_on_consolidation", getattr(cfg, "chan_fse_exit_on_consolidation", True)))
        min_hold = int(p.get("chan_fse_min_hold_bars", getattr(cfg, "chan_fse_min_hold_bars", 5)))
        zg_tol = float(p.get("chan_fse_zg_tolerance_pct", getattr(cfg, "chan_fse_zg_tolerance_pct", 0.025)))
        mode = str(p.get("chan_fse_stop_evaluation_mode", getattr(cfg, "chan_fse_stop_evaluation_mode", "close")))
        cd = int(p.get("chan_fse_cooldown_bars", getattr(cfg, "chan_fse_cooldown_bars", 4)))
        two_stage = bool(p.get("chan_fse_two_stage_entry", getattr(cfg, "chan_fse_two_stage_entry", True)))
        adx_filt = bool(p.get("chan_fse_adx_filter", getattr(cfg, "chan_fse_adx_filter", True)))
        adx_th = float(p.get("chan_fse_adx_threshold", getattr(cfg, "chan_fse_adx_threshold", 20.0)))
        return (
            f"Chan Four-State Operational Execution Strategy (chan_four_state_execution): "
            f"industrial-grade Chan execution FSM with deterministic structural invalidation stops "
            f"({mode}-confirmed, 1B fractal low with 3% buffer, 2B dd swing low, 3B zg breakout pivot), "
            f"ratcheting trailing stop to ZG ({zg_tol:.1%} buffer), MA entanglement warning filter, "
            f"{'ADX trend strength gate (threshold ' + f'{adx_th:.0f}' + '), ' if adx_filt else ''}"
            f"max single position {max_pos:.0%}, fallback stop-loss {stop_loss:.0%}, {cd}d re-entry cooldown, "
            f"{'two-stage scaling sizing, ' if two_stage else ''}"
            f"and {'zero consolidation drag with ' + str(min_hold) + 'd gestation buffer (Lesson 16)' if exit_cons else 'standard consolidation hold'}."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_fse_min_gap_bars", getattr(cfg, "chan_fse_min_gap_bars", 4))
        min_strokes = p.get("chan_fse_min_strokes", getattr(cfg, "chan_fse_min_strokes", 3))
        macd_slow = p.get("chan_fse_macd_slow", getattr(cfg, "chan_fse_macd_slow", 26))
        macd_signal = p.get("chan_fse_macd_signal", getattr(cfg, "chan_fse_macd_signal", 9))
        adx_period = int(p.get("chan_fse_adx_period", getattr(cfg, "chan_fse_adx_period", 14)))
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, macd_slow + macd_signal + 20, adx_period + 20, 20)




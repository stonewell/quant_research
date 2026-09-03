"""New Chan-theory (缠中说禅) strategies built directly from lesson techniques
beyond the buy/sell-point basics already implemented in `chan_structure.py`/
`chan_signals.py`/`chan_advanced_strategies.py`:

1. `ChanPivotOscillationStrategy`: Lesson 92's Pivot-Oscillation Monitor
   (中枢震荡监视器) -- tracks each pivot sub-swing's midpoint (Zn) drift
   relative to the pivot's own center (Z) to trade a strengthening bias,
   avoiding wedge bull-/bear-trap breakouts and Lessons 92-99's dangerous
   pivot-relation state (`chan_structure.classify_pivot_relations`).
2. `ChanFiboSectorStrengthStrategy`: Lesson 106's Fibonacci MA sector-
   strength system (斐波那契均线系统) -- ranks every symbol into a tier by
   how many Fibonacci-period SMAs it trades above, then rotates capital
   into the strongest tier(s). A cross-sectional selection/rotation
   strategy, not single-symbol timing.
3. `ChanFailedRetestBuyStrategy`: Lesson 108's precise bottom definition +
   "下探失败买" rule -- only confirms a B1 entry once a second dip fails to
   make a new low relative to the first bottom fractal (a failed retest of
   the low), rather than entering on the raw divergence bottom itself.
4. `ChanPivotShiftMACDAdvStrategy`: an enhanced SIBLING of
   `ChanPivotShiftMACDStrategy` (`rs/strategy.py`, left completely
   untouched) -- layers 3 already-built overlays (MACD zero-axis entry gate,
   weekly 区间套 re-confirmation, dangerous pivot-relation exit brake) plus a
   盘整背驰-vs-背驰 divergence-strength filter, an opt-in volume
   confirmation, and coincidence-based position sizing (Lessons
   024/027/033/056/061) onto the same pivot-shift + MACD-divergence rule.

Kept in a separate module from `chan_advanced_strategies.py` (which holds
fixes/enhancements to the 5 pre-existing strategies there) to keep the two
kinds of change -- "fix what's there" vs. "build new from the lessons" --
easy to tell apart. Reuses `chan_advanced_strategies.py`'s own helpers
(`_get_risky_symbols_helper`/`_aligned_master_index_helper`/
`_pivot_relation_danger_series`/`run_mrd_position_exit`/
`_macd_zero_axis_confirmed`/`_weekly_regime_state`/
`_run_variable_size_stop_timeout_exit`) rather than duplicating them.
"""

from typing import Dict, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import sma
from common.position_exits import run_stop_timeout_exit
from common.scheduling import get_rebalance_dates as _get_rebalance_dates

from .chan_advanced_strategies import (
    _aligned_master_index_helper,
    _get_risky_symbols_helper,
    _macd_zero_axis_confirmed,
    _pivot_relation_danger_series,
    _run_variable_size_stop_timeout_exit,
    run_mrd_position_exit,
)
from .chan_signals import compute_chan3_signals, compute_chan_pivot_macd_signals
from .chan_structure import build_pivots, build_strokes, find_fractals, merge_inclusion
from .config import StrategyConfig

_FIBO_PERIODS = (5, 13, 21, 34, 55, 89, 144, 233)


def compute_pivot_oscillation_signals(
    df: pd.DataFrame,
    min_gap_bars: int = 4,
    min_strokes: int = 3,
    trap_confirm_bars: int = 6,
) -> pd.DataFrame:
    """Lesson 92's Pivot-Oscillation Monitor (中枢震荡监视器, 0844-486e105c01007zm6-092.md):
    for a confirmed stroke-level pivot with center `Z = (zg+zd)/2`, tracks
    each sub-swing stroke's own midpoint `Zn = (start_price+end_price)/2`
    inside that pivot; a stroke whose Zn rose from the prior stroke's Zn AND
    sits above Z signals a strengthening bullish bias (mirror: falling AND
    below Z -> bearish bias).

    Trap detection (disclosed simplification of the lesson's fuller 3rd-buy/
    sell structural check): once the pivot closes, if price breaks beyond
    `zg`/`zd` but a later stroke (within `trap_confirm_bars` merged bars of
    the breakout) fully reverts back inside `[zd, zg]`, that breakout is
    flagged a wedge bull-trap (failed upside break) / bear-trap (failed
    downside break).

    Returns booleans aligned to `df.index`: `bullish_bias`, `bearish_bias`,
    `bull_trap`, `bear_trap`.
    """
    out_cols = ["bullish_bias", "bearish_bias", "bull_trap", "bear_trap"]
    result = {c: pd.Series(False, index=df.index) for c in out_cols}

    merged = merge_inclusion(df)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    pivots = build_pivots(strokes, min_strokes)
    if len(pivots) == 0 or len(strokes) == 0:
        return pd.DataFrame(result)

    def _mark(col: str, merged_pos: int) -> None:
        confirm_pos = merged_pos + 1
        if confirm_pos < len(merged):
            result[col].loc[merged.index[confirm_pos]] = True

    for p in range(len(pivots)):
        piv = pivots.iloc[p]
        zg, zd = float(piv["zg"]), float(piv["zd"])
        z = (zg + zd) / 2.0
        start_idx, end_idx = int(piv["start_stroke_idx"]), int(piv["end_stroke_idx"])

        for si in range(start_idx + 1, end_idx + 1):
            stroke = strokes.iloc[si]
            prev_stroke = strokes.iloc[si - 1]
            zn = (float(stroke["start_price"]) + float(stroke["end_price"])) / 2.0
            zn_prev = (float(prev_stroke["start_price"]) + float(prev_stroke["end_price"])) / 2.0
            if zn > zn_prev and zn > z:
                _mark("bullish_bias", int(stroke["end_pos"]))
            elif zn < zn_prev and zn < z:
                _mark("bearish_bias", int(stroke["end_pos"]))

        breakout_dir = None
        breakout_end_pos = None
        for si in range(end_idx + 1, len(strokes)):
            stroke = strokes.iloc[si]
            hi = max(float(stroke["start_price"]), float(stroke["end_price"]))
            lo = min(float(stroke["start_price"]), float(stroke["end_price"]))
            if breakout_dir is None:
                if hi > zg:
                    breakout_dir, breakout_end_pos = "up", int(stroke["end_pos"])
                elif lo < zd:
                    breakout_dir, breakout_end_pos = "down", int(stroke["end_pos"])
                continue

            if int(stroke["end_pos"]) - breakout_end_pos > trap_confirm_bars:
                break
            if zd <= float(stroke["end_price"]) <= zg:
                _mark("bull_trap" if breakout_dir == "up" else "bear_trap", int(stroke["end_pos"]))
                break

    return pd.DataFrame(result)


def compute_fibo_tier(close: pd.Series) -> pd.Series:
    """Lesson 106's Fibonacci MA tier (1053-486e105c01009tb9-106.md): counts
    how many of the Fibonacci-period SMAs (5/13/21/34/55/89/144/233) the
    close currently trades above (0..8; 8 = reclaimed every one, the
    strongest tier). Requires `warmup_bars` (233 + buffer) of history for a
    meaningful (non-warmup-suppressed) tier."""
    tier = pd.Series(0.0, index=close.index)
    for period in _FIBO_PERIODS:
        ma = sma(close, period)
        tier = tier + (close > ma).fillna(False).astype(float)
    return tier


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


class ChanPivotOscillationStrategy(AllocationTemplate):
    """Chan Pivot-Oscillation Monitor Strategy (中枢震荡监视器策略, Lesson 92):
    Tracks each pivot's sub-swing midpoint (Zn) drift relative to the
    pivot's own center (Z) to trade a strengthening bias, while avoiding
    wedge bull-/bear-trap breakouts (a break that immediately reverts) and
    Lessons 92-99's dangerous pivot-relation state.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_pivot_oscillation", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("pivot_osc_min_gap_bars", cfg.pivot_osc_min_gap_bars)
        min_strokes = p.get("pivot_osc_min_strokes", cfg.pivot_osc_min_strokes)
        trap_confirm_bars = p.get("pivot_osc_trap_confirm_bars", cfg.pivot_osc_trap_confirm_bars)
        stop_loss_pct = p.get("pivot_osc_stop_loss_pct", cfg.pivot_osc_stop_loss_pct)
        max_holding_days = p.get("pivot_osc_max_holding_days", cfg.pivot_osc_max_holding_days)
        position_size_pct = p.get("pivot_osc_position_size_pct", cfg.pivot_osc_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            close = bars["Close"].reindex(master_index)
            sig = compute_pivot_oscillation_signals(bars, min_gap_bars, min_strokes, trap_confirm_bars)
            bullish_bias = sig["bullish_bias"].reindex(master_index).fillna(False)
            bearish_bias = sig["bearish_bias"].reindex(master_index).fillna(False)
            bull_trap = sig["bull_trap"].reindex(master_index).fillna(False)
            danger = _pivot_relation_danger_series(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)

            entry_signal = bullish_bias & ~bull_trap & ~danger
            exit_signal = bearish_bias | bull_trap | danger

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        return (
            "Chan Pivot-Oscillation Monitor Strategy (中枢震荡监视器, Lesson 92): "
            "longs active risky symbols when a pivot's sub-swing midpoint (Zn) drifts above its own "
            "center (Z), avoiding wedge bull-trap breakouts and Lessons 92-99's dangerous pivot-relation "
            "state; exits on a weakening (bearish) Zn drift, a bull-trap, a dangerous pivot-relation flip, "
            "stop-loss, or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("pivot_osc_min_gap_bars", cfg.pivot_osc_min_gap_bars)
        min_strokes = p.get("pivot_osc_min_strokes", cfg.pivot_osc_min_strokes)
        return (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)


class ChanFiboSectorStrengthStrategy(AllocationTemplate):
    """Chan Fibonacci MA Sector-Strength Rotation Strategy (缠中说禅板块强弱指标
    与斐波那契均线系统, Lesson 106): ranks every risky symbol by how many
    Fibonacci-period SMAs (5/13/21/34/55/89/144/233) it trades above (tier
    0-8), then rotates capital equally across the top `fibo_top_k` symbols
    clearing `fibo_min_tier`, rebalancing every `fibo_rebalance_freq_days`.
    A cross-sectional selection/rotation strategy, not single-symbol timing.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_fibo_sector_strength", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        top_k = p.get("fibo_top_k", cfg.fibo_top_k)
        min_tier = p.get("fibo_min_tier", cfg.fibo_min_tier)
        rebalance_freq = p.get("fibo_rebalance_freq_days", cfg.fibo_rebalance_freq_days)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        tiers = pd.DataFrame(index=master_index)
        for sym in risky_symbols:
            close = universe[sym]["Close"].reindex(master_index)
            tiers[sym] = compute_fibo_tier(close)

        daily = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
        for date in _get_rebalance_dates(master_index, rebalance_freq):
            row_tiers = tiers.loc[date]
            qualifying = row_tiers[row_tiers >= min_tier].sort_values(ascending=False)
            selected = list(qualifying.index[:top_k])
            row = {s: 0.0 for s in symbols}
            if selected:
                w = 1.0 / len(selected)
                for s in selected:
                    row[s] = w
            daily.loc[date] = row

        daily = daily.ffill().fillna(0.0)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan Fibonacci MA Sector-Strength Rotation Strategy (缠中说禅板块强弱指标与斐波那契均线系统, Lesson 106): "
            f"ranks risky symbols by how many of the Fibonacci-period SMAs (5/13/21/34/55/89/144/233) each trades "
            f"above (tier 0-8), and rotates equal weight across the top {p.get('fibo_top_k', cfg.fibo_top_k)} "
            f"symbols clearing tier {p.get('fibo_min_tier', cfg.fibo_min_tier)}, deroutes the rest to cash."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return max(_FIBO_PERIODS) + 10


class ChanFailedRetestBuyStrategy(AllocationTemplate):
    """Chan Failed-Retest Buy Strategy (下探失败买策略, Lesson 108): a stricter
    B1 variant that only confirms entry once a second dip fails to make a
    new low relative to the first bottom fractal (a failed retest of the
    low, per Lesson 108's precise bottom definition), rather than entering
    on the raw first_buy MACD-divergence bottom itself. Exits via the same
    tight '防狼术'-style risk controls as `ChanMeanReversionDivergenceStrategy`
    (`run_mrd_position_exit`).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_failed_retest_buy", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("failed_retest_min_gap_bars", cfg.failed_retest_min_gap_bars)
        min_strokes = p.get("failed_retest_min_strokes", cfg.failed_retest_min_strokes)
        macd_fast = p.get("failed_retest_macd_fast", cfg.failed_retest_macd_fast)
        macd_slow = p.get("failed_retest_macd_slow", cfg.failed_retest_macd_slow)
        macd_signal_period = p.get("failed_retest_macd_signal", cfg.failed_retest_macd_signal)
        confirm_window_bars = p.get("failed_retest_confirm_window_bars", cfg.failed_retest_confirm_window_bars)
        stop_loss_pct = p.get("failed_retest_stop_loss_pct", cfg.failed_retest_stop_loss_pct)
        profit_target_pct = p.get("failed_retest_profit_target_pct", cfg.failed_retest_profit_target_pct)
        trailing_stop_pct = p.get("failed_retest_trailing_stop_pct", cfg.failed_retest_trailing_stop_pct)
        trailing_activate_pct = p.get("failed_retest_trailing_activate_pct", cfg.failed_retest_trailing_activate_pct)
        max_holding_days = p.get("failed_retest_max_holding_days", cfg.failed_retest_max_holding_days)
        position_size_pct = p.get("failed_retest_position_size_pct", cfg.failed_retest_position_size_pct)

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
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal_period,
            )
            entry_signal = _failed_retest_confirmed(bars, sig, confirm_window_bars).reindex(master_index).fillna(False)
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
        return (
            "Chan Failed-Retest Buy Strategy (下探失败买, Lesson 108): "
            "longs active risky symbols only once a second dip fails to make a new low relative to the "
            "bottom fractal nearest a B1 MACD-divergence signal (a failed retest of the low), rather "
            "than entering on the raw divergence bottom itself; applies the same tight risk management "
            "as ChanMeanReversionDivergenceStrategy (stop-loss, profit target, trailing stop, max holding cap)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("failed_retest_min_gap_bars", cfg.failed_retest_min_gap_bars)
        min_strokes = p.get("failed_retest_min_strokes", cfg.failed_retest_min_strokes)
        macd_slow = p.get("failed_retest_macd_slow", cfg.failed_retest_macd_slow)
        macd_signal_period = p.get("failed_retest_macd_signal", cfg.failed_retest_macd_signal)
        confirm_window_bars = p.get("failed_retest_confirm_window_bars", cfg.failed_retest_confirm_window_bars)
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, macd_slow + macd_signal_period + 10) + confirm_window_bars


def _weekly_pivot_macd_regime_state(
    bars: pd.DataFrame, min_gap_bars: int, min_strokes: int, macd_fast: int, macd_slow: int, macd_signal: int
) -> pd.Series:
    """Weekly-level 区间套 re-confirmation for `ChanPivotShiftMACDAdvStrategy`,
    using the SAME signal type at both timeframes -- `compute_chan_pivot_macd_signals`
    (stroke/pivot-shift + real MACD divergence) run on weekly-resampled bars,
    rather than `chan_advanced_strategies._weekly_regime_state`'s
    `compute_chan3_signals` (the formal B1/B2/B3 taxonomy).

    Deliberately NOT reusing `_weekly_regime_state`: on real multi-year
    yfinance data (~600 weekly bars), `compute_chan3_signals`'s formal
    buy/sell points essentially never fire at the weekly degree (confirmed
    directly -- 0 fires across several real ETFs over 2015-2026), leaving
    that regime permanently False and blocking nearly every entry.
    `compute_chan_pivot_macd_signals` fires a handful of times per symbol
    over the same span, giving an actually-usable state signal. Same
    as-of union+ffill reindexing (never looks into a still-forming week) as
    `_weekly_regime_state`. `ChanMultiTimeframeTrendStrategy` (which DOES use
    `_weekly_regime_state`) is untouched -- out of this strategy's scope.
    """
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in bars.columns:
        agg["Volume"] = "sum"
    weekly_bars = bars.resample("W-FRI").agg(agg).dropna(subset=["Close"])
    if len(weekly_bars) < min_strokes * 2:
        return pd.Series(False, index=bars.index)

    weekly_sig = compute_chan_pivot_macd_signals(
        weekly_bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
        macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
    )
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


def _run_adv_position_exit(
    close: pd.Series | np.ndarray,
    entry_signal: pd.Series | np.ndarray,
    exit_signal: pd.Series | np.ndarray,
    stop_loss_pct: Optional[float],
    max_holding_days: Optional[int],
    size_at_entry: pd.Series | np.ndarray,
    trailing_activate_pct: Optional[float] = 0.08,
    trailing_stop_pct: Optional[float] = 0.04,
) -> np.ndarray:
    close_arr = np.asarray(close)
    entry_arr = np.asarray(entry_signal)
    exit_arr = np.asarray(exit_signal)
    size_arr = np.asarray(size_at_entry)
    n_bars = len(close_arr)
    raw = np.zeros(n_bars)
    in_position, entry_idx = False, 0
    current_size = 0.0
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
            timed_out = max_holding_days is not None and held >= max_holding_days

            peak_ret = highest_price / entry_p - 1.0 if entry_p > 0 else 0.0
            trail_activated = trailing_activate_pct is None or peak_ret >= trailing_activate_pct
            trail_hit = (
                trailing_stop_pct is not None
                and trail_activated
                and highest_price > 0
                and (p / highest_price - 1.0) <= -trailing_stop_pct
            )

            if exit_arr[i] or stopped or timed_out or trail_hit:
                in_position = False
                raw[i] = 0.0
            else:
                raw[i] = current_size
        elif entry_arr[i]:
            in_position = True
            entry_idx = i
            current_size = size_arr[i]
            highest_price = close_arr[i]
            raw[i] = current_size

    return raw


class ChanPivotShiftMACDAdvStrategy(AllocationTemplate):
    """Chan Pivot Shift (MACD) Advanced: an enhanced SIBLING of
    `ChanPivotShiftMACDStrategy` (`rs/strategy.py`, left completely
    untouched -- this is an additive, separately-registered strategy, not a
    replacement). Same stroke-level pivot-band-shift + real MACD-histogram-
    area divergence rule (`compute_chan_pivot_macd_signals`), layered with:

    - Lesson 103's MACD zero-axis entry gate (`_macd_zero_axis_confirmed`),
      applied only to the DIVERGENCE-sourced portion of `buy_signal` -- the
      lesson is specifically about not bottom-fishing while bear-dominated,
      not about blocking a pivot-shift trend-continuation breakout, which
      isn't a bottom-fish at all.
    - A genuine weekly-level 区间套 re-confirmation
      (`_weekly_pivot_macd_regime_state`, see above) on both entry and exit.
    - Lessons 92-99's dangerous pivot-relation exit brake
      (`_pivot_relation_danger_series`).
    - A 盘整背驰-vs-背驰 divergence-strength filter (Lessons 024/027/033):
      by default only trades cross-pivot (not pivot-internal/weaker)
      divergence signals (`chanm_adv_require_cross_pivot_divergence`).
    - An opt-in volume-confirmation gate (Lesson 056,
      `chanm_adv_require_volume_confirmation`) -- mechanically correct but
      NOT validated for real efficacy in this repo, since this workspace's
      synthetic Volume is disclosed pure noise (see `common.indicators.obv`'s
      own docstring); default off.
    - Coincidence-based position sizing (Lesson 061): an entry that
      coincides with a formal 2nd/3rd-type point (`compute_chan3_signals`)
      gets full size; a non-coincident (weaker-context) entry gets a
      reduced size (`chanm_adv_weak_signal_position_size_pct`).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_pivot_shift_macd_adv", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chanm_adv_min_gap_bars", cfg.chanm_adv_min_gap_bars)
        min_strokes = p.get("chanm_adv_min_strokes", cfg.chanm_adv_min_strokes)
        macd_fast = p.get("chanm_adv_macd_fast", cfg.chanm_adv_macd_fast)
        macd_slow = p.get("chanm_adv_macd_slow", cfg.chanm_adv_macd_slow)
        macd_signal_period = p.get("chanm_adv_macd_signal", cfg.chanm_adv_macd_signal)
        stop_loss_pct = p.get("chanm_adv_stop_loss_pct", cfg.chanm_adv_stop_loss_pct)
        max_holding_days = p.get("chanm_adv_max_holding_days", cfg.chanm_adv_max_holding_days)
        position_size_pct = p.get("chanm_adv_position_size_pct", cfg.chanm_adv_position_size_pct)
        require_cross_pivot_divergence = p.get(
            "chanm_adv_require_cross_pivot_divergence", cfg.chanm_adv_require_cross_pivot_divergence
        )
        require_volume_confirmation = p.get(
            "chanm_adv_require_volume_confirmation", cfg.chanm_adv_require_volume_confirmation
        )
        weak_signal_position_size_pct = p.get(
            "chanm_adv_weak_signal_position_size_pct", cfg.chanm_adv_weak_signal_position_size_pct
        )
        use_trend_gate = p.get(
            "chanm_adv_use_trend_gate", getattr(cfg, "chanm_adv_use_trend_gate", True)
        )
        trend_ma_period = p.get(
            "chanm_adv_trend_ma_period", getattr(cfg, "chanm_adv_trend_ma_period", 200)
        )
        suppress_top_div_in_uptrend = p.get(
            "chanm_adv_suppress_top_div_in_uptrend", getattr(cfg, "chanm_adv_suppress_top_div_in_uptrend", True)
        )
        trailing_activate_pct = p.get(
            "chanm_adv_trailing_activate_pct", getattr(cfg, "chanm_adv_trailing_activate_pct", 0.08)
        )
        trailing_stop_pct = p.get(
            "chanm_adv_trailing_stop_pct", getattr(cfg, "chanm_adv_trailing_stop_pct", 0.04)
        )

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal_period,
                require_volume_confirmation=require_volume_confirmation,
            )
            buy_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            sell_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            divergence_buy = sig["divergence_buy"].reindex(master_index).fillna(False)
            if require_cross_pivot_divergence:
                weak_buy = sig["weak_divergence_buy"].reindex(master_index).fillna(False)
                weak_sell = sig["weak_divergence_sell"].reindex(master_index).fillna(False)
                buy_signal = buy_signal & ~weak_buy
                sell_signal = sell_signal & ~weak_sell
                divergence_buy = divergence_buy & ~weak_buy

            # Lesson 103's zero-axis gate is about not bottom-fishing while
            # bear-dominated -- apply it only to the divergence-sourced
            # (bottom-fishing) portion of buy_signal, not the pivot-shift
            # trend-continuation portion, which isn't a bottom-fish.
            zero_axis_ok = _macd_zero_axis_confirmed(bars["Close"], macd_fast, macd_slow, macd_signal_period).reindex(master_index).fillna(False)
            pivot_shift_buy = buy_signal & ~divergence_buy
            gated_divergence_buy = divergence_buy & zero_axis_ok

            close = bars["Close"].reindex(master_index)
            if use_trend_gate:
                ma = sma(close, trend_ma_period)
                ma_slope = ma.diff(50).fillna(0.0)
                uptrend = (close > ma) | (ma_slope > 0) | ma.isna()
                bull_run = (close > ma * 1.03) & (ma_slope > 0)

                gated_divergence_buy = gated_divergence_buy & uptrend
                if suppress_top_div_in_uptrend:
                    sell_signal = sell_signal & ~bull_run

            gated_buy_signal = pivot_shift_buy | gated_divergence_buy

            weekly_regime = _weekly_pivot_macd_regime_state(
                bars, min_gap_bars, min_strokes, macd_fast, macd_slow, macd_signal_period
            ).reindex(master_index).ffill().fillna(False)
            danger = _pivot_relation_danger_series(bars, min_gap_bars, min_strokes).reindex(master_index).ffill().fillna(False)

            entry_signal = gated_buy_signal & weekly_regime
            exit_signal = sell_signal | (~weekly_regime) | danger

            sig3 = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal_period,
            )
            coincidence = (sig3["second_buy"] | sig3["third_buy"]).reindex(master_index).fillna(False)
            size_at_entry = np.where(coincidence.to_numpy(), position_size_pct, weak_signal_position_size_pct)

            raw_weights[sym] = _run_adv_position_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, size_at_entry,
                trailing_activate_pct=trailing_activate_pct, trailing_stop_pct=trailing_stop_pct,
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        cross_pivot_on = p.get("chanm_adv_require_cross_pivot_divergence", cfg.chanm_adv_require_cross_pivot_divergence)
        volume_on = p.get("chanm_adv_require_volume_confirmation", cfg.chanm_adv_require_volume_confirmation)
        return (
            "Chan Pivot Shift (MACD) Advanced (chan_pivot_shift_macd_adv): an enhanced sibling of "
            "chan_pivot_shift_macd -- the same stroke-level pivot-band-shift + MACD-histogram-area "
            "divergence rule, gated by a MACD zero-axis reclaim (Lesson 103), a genuine weekly-level "
            "区间套 re-confirmation, and a dangerous pivot-relation exit brake (Lessons 92-99); "
            f"{'only trades cross-pivot (not 盘整背驰) divergence' if cross_pivot_on else 'trades all divergence signals, any strength'}; "
            f"{'requires weakening volume to confirm divergence; ' if volume_on else ''}"
            "sizes down entries that don't coincide with a formal 2nd/3rd-type point (Lesson 061)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chanm_adv_min_gap_bars", cfg.chanm_adv_min_gap_bars)
        min_strokes = p.get("chanm_adv_min_strokes", cfg.chanm_adv_min_strokes)
        macd_slow = p.get("chanm_adv_macd_slow", cfg.chanm_adv_macd_slow)
        macd_signal_period = p.get("chanm_adv_macd_signal", cfg.chanm_adv_macd_signal)
        structural = min_strokes * 2 * (min_gap_bars + 2)
        macd_floor = macd_slow + macd_signal_period + 10
        weekly_structural_bars = structural * 5
        return max(structural, macd_floor, weekly_structural_bars)

"""Entry x exit/risk aspect composition for single-asset timing templates.

`RSIMeanReversionStrategy`, `SwingTrendPullbackStrategy`,
`ChanPivotShiftStrategy`, and `TurtleBreakoutStrategy` (`rs/strategy.py`)
each independently compute, per risky symbol:

- an ENTRY SIGNAL (would-enter-today-if-flat), and
- a stateful position loop owning the EXIT condition, optional stop-loss/
  trailing-stop, max-holding-days, and position sizing,

then aggregate every symbol's raw weight the same way (cap the total at
1.0, remainder to `cash_proxy`). This module reimplements each side as a
standalone, independently composable `EntrySignalAspect`/`ExitRiskAspect`,
ported from the matching template's own logic, so a `CompositeTimingTemplate`
can pair e.g. Turtle's Donchian-breakout entry with RSI's simpler
percent-stop-loss/max-holding-days exit instead of Turtle's own ATR
trailing stop.

Lives in `research_strategy/rs/` (not `common/`) because it depends on
`rs.strategy`'s single-asset-timing helper `_get_risky_symbols` and
`rs.chan_structure` -- this module is itself an addition to
`research_strategy`, not a core primitive. Deliberately NOT a refactor of the
4 existing classes (zero regression risk to already-tested code): this is
new, parallel logic reusing `common.indicators`, plus the same shared
weight-shaping/exit-loop helpers (`common.allocation_templates`,
`common.position_exits`) those classes themselves use.
"""

from dataclasses import asdict, dataclass
from typing import Callable, Dict

import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate, _cap_and_deroute_to_cash, _fill_out_columns, _sparse_from_daily
from common.indicators import atr, cumulative_rsi, rsi, rsi_cutler, rsi_wilder, sma
from common.position_exits import run_stop_timeout_exit

from .chan_structure import compute_chan_signals
from .chan_signals import compute_chan3_signals, compute_chan_pivot_macd_signals
from .strategy import _get_risky_symbols


@dataclass
class EntrySignalAspect:
    key: str
    factor_tags: list
    compute_fn: Callable
    warmup_fn: Callable
    describe_fn: Callable
    # The StrategyConfig field name (e.g. "rsi_symbol") this aspect's source
    # template uses as its own single-symbol default -- see
    # CompositeTimingTemplate.generate_weights, which reads this to resolve
    # `cfg_symbol` for _get_risky_symbols instead of falling through to
    # `risky_universe` (an 8-symbol default present on EVERY StrategyConfig,
    # which would otherwise silently outrank the intended single symbol).
    symbol_param_key: str

    def compute(self, df: pd.DataFrame, params: dict) -> pd.Series:
        return self.compute_fn(df, params)

    def warmup_bars(self, params: dict) -> int:
        return self.warmup_fn(params)

    def describe(self, params: dict) -> str:
        return self.describe_fn(params)


@dataclass
class ExitRiskAspect:
    key: str
    factor_tags: list
    run_fn: Callable
    warmup_fn: Callable
    describe_fn: Callable

    def run(self, df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
        return self.run_fn(df, entry_signal, params)

    def warmup_bars(self, params: dict) -> int:
        return self.warmup_fn(params)

    def describe(self, params: dict) -> str:
        return self.describe_fn(params)


# --------------------------------------------------------------------------
# Entry aspects
# --------------------------------------------------------------------------

def _entry_rsi_oversold(df: pd.DataFrame, params: dict) -> pd.Series:
    close = df["Close"]
    rsi_period = params.get("rsi_period", 2)
    rsi_method = params.get("rsi_method", "wilder")
    entry_mode = params.get("rsi_entry_mode", "single")
    oversold_threshold = params.get("rsi_oversold_threshold", 10.0)
    cumulative_lookback = params.get("rsi_cumulative_lookback", 2)
    cumulative_threshold = params.get("rsi_cumulative_threshold", 10.0)
    require_trend_filter = params.get("rsi_require_trend_filter", True)
    trend_ma_period = params.get("rsi_trend_ma_period", 200)

    rsi_series = rsi_wilder(close, rsi_period) if rsi_method == "wilder" else rsi_cutler(close, rsi_period)
    if entry_mode == "cumulative":
        entry_trigger = cumulative_rsi(rsi_series, cumulative_lookback) < cumulative_threshold
    elif entry_mode == "single":
        entry_trigger = rsi_series < oversold_threshold
    else:
        raise ValueError(f"Unknown rsi_entry_mode: {entry_mode!r} (expected 'single' or 'cumulative')")

    trend_ok = (close > sma(close, trend_ma_period)) if require_trend_filter else pd.Series(True, index=close.index)
    return (entry_trigger & trend_ok).fillna(False)


def _entry_swing_pullback(df: pd.DataFrame, params: dict) -> pd.Series:
    close = df["Close"]
    trend_ma_period = params.get("swing_trend_ma_period", 200)
    require_rising_trend_ma = params.get("swing_require_rising_trend_ma", True)
    trend_slope_lookback = params.get("swing_trend_slope_lookback", 20)
    pullback_ma_period = params.get("swing_pullback_ma_period", 20)
    rsi_period = params.get("swing_rsi_period", 5)
    entry_rsi_threshold = params.get("swing_entry_rsi_threshold", 45.0)

    trend_ma = sma(close, trend_ma_period)
    pullback_ma = sma(close, pullback_ma_period)
    rsi_series = rsi(close, rsi_period)

    trend_ok = close > trend_ma
    if require_rising_trend_ma:
        trend_ok = trend_ok & (trend_ma > trend_ma.shift(trend_slope_lookback))
    pullback_ok = close < pullback_ma
    rsi_ok = rsi_series < entry_rsi_threshold
    return (trend_ok & pullback_ok & rsi_ok).fillna(False)


def _entry_chan_pivot(df: pd.DataFrame, params: dict) -> pd.Series:
    min_gap_bars = params.get("chan_min_gap_bars", 4)
    min_strokes = params.get("chan_min_strokes", 3)
    sig = compute_chan_signals(df, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
    return sig["buy_signal"].reindex(df.index).fillna(False)


def _entry_chan3_point(df: pd.DataFrame, params: dict) -> pd.Series:
    min_gap_bars = params.get("chan3_min_gap_bars", 4)
    min_strokes = params.get("chan3_min_strokes", 3)
    macd_fast = params.get("chan3_macd_fast", 12)
    macd_slow = params.get("chan3_macd_slow", 26)
    macd_signal = params.get("chan3_macd_signal", 9)
    sig = compute_chan3_signals(
        df, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
        macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
    )
    return sig["buy_signal"].reindex(df.index).fillna(False)


def _entry_chanm_pivot(df: pd.DataFrame, params: dict) -> pd.Series:
    min_gap_bars = params.get("chanm_min_gap_bars", 4)
    min_strokes = params.get("chanm_min_strokes", 3)
    macd_fast = params.get("chanm_macd_fast", 12)
    macd_slow = params.get("chanm_macd_slow", 26)
    macd_signal = params.get("chanm_macd_signal", 9)
    sig = compute_chan_pivot_macd_signals(
        df, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
        macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
    )
    return sig["buy_signal"].reindex(df.index).fillna(False)


def _entry_turtle_breakout(df: pd.DataFrame, params: dict) -> pd.Series:
    close, high = df["Close"], df["High"]
    entry_breakout_days = params.get("turtle_entry_breakout_days", 20)
    atr_period = params.get("turtle_atr_period", 20)
    require_trend_filter = params.get("turtle_require_trend_filter", True)
    trend_ma_period = params.get("turtle_trend_ma_period", 200)

    atr_series = atr(df, atr_period)
    donchian_high = high.shift(1).rolling(entry_breakout_days).max()

    valid = close.notna() & (close > 0) & atr_series.notna() & (atr_series > 0)
    if require_trend_filter:
        trend_ma = sma(close, trend_ma_period)
        trend_ok = close.notna() & trend_ma.notna() & (close > trend_ma)
    else:
        trend_ok = pd.Series(True, index=close.index)
    breakout = donchian_high.notna() & (close > donchian_high)
    return (valid & trend_ok & breakout).fillna(False)


ENTRY_SIGNAL_ASPECTS: Dict[str, EntrySignalAspect] = {
    "rsi_oversold_entry": EntrySignalAspect(
        key="rsi_oversold_entry", factor_tags=["mean_reversion"], symbol_param_key="rsi_symbol",
        compute_fn=_entry_rsi_oversold,
        warmup_fn=lambda p: max(
            p.get("rsi_period", 2), p.get("rsi_cumulative_lookback", 2),
            p.get("rsi_trend_ma_period", 200) if p.get("rsi_require_trend_filter", True) else 0,
        ),
        describe_fn=lambda p: f"RSI({p.get('rsi_period', 2)}) oversold below {p.get('rsi_oversold_threshold', 10.0)}",
    ),
    "swing_pullback_entry": EntrySignalAspect(
        key="swing_pullback_entry", factor_tags=["absolute_momentum_trend", "mean_reversion"],
        symbol_param_key="swing_symbol",
        compute_fn=_entry_swing_pullback,
        warmup_fn=lambda p: max(p.get("swing_trend_ma_period", 200), p.get("swing_trend_slope_lookback", 20)),
        describe_fn=lambda p: (
            f"a pullback (close < {p.get('swing_pullback_ma_period', 20)}-day SMA, "
            f"RSI < {p.get('swing_entry_rsi_threshold', 45.0)}) within a confirmed uptrend"
        ),
    ),
    "chan_pivot_entry": EntrySignalAspect(
        key="chan_pivot_entry", factor_tags=["regime_trend_strength"], symbol_param_key="chan_symbol",
        compute_fn=_entry_chan_pivot,
        warmup_fn=lambda p: p.get("chan_min_strokes", 3) * 2 * (p.get("chan_min_gap_bars", 4) + 2),
        describe_fn=lambda p: "a Chan-theory pivot shift up with a confirming pullback low",
    ),
    "chan3_point_entry": EntrySignalAspect(
        key="chan3_point_entry", factor_tags=["regime_trend_strength"], symbol_param_key="chan3_symbol",
        compute_fn=_entry_chan3_point,
        warmup_fn=lambda p: max(
            (p.get("chan3_min_strokes", 3) ** 2) * 2 * (p.get("chan3_min_gap_bars", 4) + 2)
            + 2 * (p.get("chan3_min_gap_bars", 4) + 2),
            p.get("chan3_macd_slow", 26) + p.get("chan3_macd_signal", 9) + 10,
        ),
        describe_fn=lambda p: "a Chan-theory first/second/third-type buy point (segments + real MACD divergence)",
    ),
    "chanm_pivot_entry": EntrySignalAspect(
        key="chanm_pivot_entry", factor_tags=["regime_trend_strength"], symbol_param_key="chanm_symbol",
        compute_fn=_entry_chanm_pivot,
        warmup_fn=lambda p: max(
            p.get("chanm_min_strokes", 3) * 2 * (p.get("chanm_min_gap_bars", 4) + 2),
            p.get("chanm_macd_slow", 26) + p.get("chanm_macd_signal", 9) + 10,
        ),
        describe_fn=lambda p: "a Chan-theory pivot shift up, or a bottom-divergence buy point (real MACD)",
    ),
    "turtle_breakout_entry": EntrySignalAspect(
        key="turtle_breakout_entry", factor_tags=["absolute_momentum_trend"], symbol_param_key="turtle_symbol",
        compute_fn=_entry_turtle_breakout,
        warmup_fn=lambda p: max(
            p.get("turtle_entry_breakout_days", 20), p.get("turtle_atr_period", 20),
            p.get("turtle_trend_ma_period", 200) if p.get("turtle_require_trend_filter", True) else 0,
        ) + 1,
        describe_fn=lambda p: f"a {p.get('turtle_entry_breakout_days', 20)}-day Donchian-high breakout",
    ),
}


# --------------------------------------------------------------------------
# Exit/risk aspects -- each owns the full stateful per-symbol position loop.
# --------------------------------------------------------------------------

def _exit_rsi_cross(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close = df["Close"]
    rsi_period = params.get("rsi_period", 2)
    rsi_method = params.get("rsi_method", "wilder")
    exit_mode = params.get("rsi_exit_mode", "rsi_cross")
    exit_rsi_threshold = params.get("rsi_exit_rsi_threshold", 70.0)
    exit_ma_period = params.get("rsi_exit_ma_period", 5)
    stop_loss_pct = params.get("rsi_stop_loss_pct", None)
    max_holding_days = params.get("rsi_max_holding_days", 10)
    position_size_pct = params.get("rsi_position_size_pct", 1.0)

    rsi_series = rsi_wilder(close, rsi_period) if rsi_method == "wilder" else rsi_cutler(close, rsi_period)
    exit_ma = sma(close, exit_ma_period)
    exit_rsi_ok = rsi_series > exit_rsi_threshold
    exit_ma_ok = close > exit_ma
    if exit_mode == "rsi_cross":
        exit_signal = exit_rsi_ok
    elif exit_mode == "ma_cross":
        exit_signal = exit_ma_ok
    elif exit_mode == "either":
        exit_signal = exit_rsi_ok | exit_ma_ok
    else:
        raise ValueError(f"Unknown rsi_exit_mode: {exit_mode!r} (expected 'rsi_cross', 'ma_cross', or 'either')")
    exit_signal = exit_signal.fillna(False).to_numpy()

    return run_stop_timeout_exit(close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct)


def _exit_swing_stop_target(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close = df["Close"]
    n_bars = len(close)
    rsi_period = params.get("swing_rsi_period", 5)
    exit_rsi_threshold = params.get("swing_exit_rsi_threshold", 90.0)
    stop_loss_pct = params.get("swing_stop_loss_pct", 0.05)
    reward_risk_ratio = params.get("swing_reward_risk_ratio", 3.0)
    use_trailing_stop = params.get("swing_use_trailing_stop", True)
    trailing_activate_pct = params.get("swing_trailing_activate_pct", 0.07)
    trailing_stop_pct = params.get("swing_trailing_stop_pct", 0.04)
    max_holding_days = params.get("swing_max_holding_days", 63)
    position_size_pct = params.get("swing_position_size_pct", 1.0)

    exit_signal = (rsi(close, rsi_period) > exit_rsi_threshold).fillna(False).to_numpy()
    profit_target_pct = stop_loss_pct * reward_risk_ratio

    close_arr = close.to_numpy()
    entry_arr = entry_signal.to_numpy()
    raw = np.zeros(n_bars)
    in_position, entry_idx, peak_price = False, 0, 0.0
    for i in range(n_bars):
        c = close_arr[i]
        if in_position:
            entry_price = close_arr[entry_idx]
            peak_price = max(peak_price, c)
            held = i - entry_idx
            stopped = c <= entry_price * (1 - stop_loss_pct)
            targeted = c >= entry_price * (1 + profit_target_pct)
            trailed = (
                use_trailing_stop and (peak_price / entry_price - 1) >= trailing_activate_pct
                and c <= peak_price * (1 - trailing_stop_pct)
            )
            timed_out = max_holding_days is not None and held >= max_holding_days
            if stopped or targeted or trailed or exit_signal[i] or timed_out:
                in_position = False
                raw[i] = 0.0
            else:
                raw[i] = position_size_pct
        elif entry_arr[i]:
            in_position = True
            entry_idx = i
            peak_price = c
            raw[i] = position_size_pct
    return raw


def _exit_chan_signal(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close = df["Close"]
    min_gap_bars = params.get("chan_min_gap_bars", 4)
    min_strokes = params.get("chan_min_strokes", 3)
    stop_loss_pct = params.get("chan_stop_loss_pct", 0.08)
    max_holding_days = params.get("chan_max_holding_days", 90)
    position_size_pct = params.get("chan_position_size_pct", 1.0)

    sig = compute_chan_signals(df, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
    exit_signal = sig["sell_signal"].reindex(df.index).fillna(False).to_numpy()

    return run_stop_timeout_exit(close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct)


def _exit_chan3_point(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close = df["Close"]
    min_gap_bars = params.get("chan3_min_gap_bars", 4)
    min_strokes = params.get("chan3_min_strokes", 3)
    macd_fast = params.get("chan3_macd_fast", 12)
    macd_slow = params.get("chan3_macd_slow", 26)
    macd_signal = params.get("chan3_macd_signal", 9)
    stop_loss_pct = params.get("chan3_stop_loss_pct", 0.08)
    max_holding_days = params.get("chan3_max_holding_days", 90)
    position_size_pct = params.get("chan3_position_size_pct", 1.0)

    sig = compute_chan3_signals(
        df, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
        macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
    )
    exit_signal = sig["sell_signal"].reindex(df.index).fillna(False).to_numpy()

    return run_stop_timeout_exit(close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct)


def _exit_chanm_signal(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close = df["Close"]
    min_gap_bars = params.get("chanm_min_gap_bars", 4)
    min_strokes = params.get("chanm_min_strokes", 3)
    macd_fast = params.get("chanm_macd_fast", 12)
    macd_slow = params.get("chanm_macd_slow", 26)
    macd_signal = params.get("chanm_macd_signal", 9)
    stop_loss_pct = params.get("chanm_stop_loss_pct", 0.08)
    max_holding_days = params.get("chanm_max_holding_days", 90)
    position_size_pct = params.get("chanm_position_size_pct", 1.0)

    sig = compute_chan_pivot_macd_signals(
        df, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
        macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
    )
    exit_signal = sig["sell_signal"].reindex(df.index).fillna(False).to_numpy()

    return run_stop_timeout_exit(close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct)


def _exit_turtle_atr_trailing(df: pd.DataFrame, entry_signal: pd.Series, params: dict) -> np.ndarray:
    close, high = df["Close"], df["High"]
    n_bars = len(close)
    exit_breakout_days = params.get("turtle_exit_breakout_days", 10)
    atr_period = params.get("turtle_atr_period", 20)
    atr_stop_mult = params.get("turtle_atr_stop_mult", 2.0)
    position_sizing_mode = params.get("turtle_position_sizing_mode", "inverse_atr")

    atr_series = atr(df, atr_period)
    donchian_low = df["Low"].shift(1).rolling(exit_breakout_days).min()

    close_arr, high_arr, atr_arr, dl_arr = (
        close.to_numpy(), high.to_numpy(), atr_series.to_numpy(), donchian_low.to_numpy(),
    )
    entry_arr = entry_signal.to_numpy()

    active = np.zeros(n_bars, dtype=bool)
    vol_w = np.zeros(n_bars, dtype=float)
    in_position, peak_price = False, 0.0
    for i in range(n_bars):
        c, a = close_arr[i], atr_arr[i]
        if pd.isna(c) or c <= 0 or pd.isna(a) or a <= 0:
            in_position = False
            continue

        dl = dl_arr[i]
        if in_position:
            peak_price = max(peak_price, high_arr[i])
            stop_price = peak_price - atr_stop_mult * a
            donchian_exit = pd.notna(dl) and c < dl
            atr_exit = c < stop_price
            if donchian_exit or atr_exit:
                in_position = False
        elif entry_arr[i]:
            in_position = True
            peak_price = high_arr[i]

        active[i] = in_position
        vol_w[i] = (c / a) if (in_position and a > 0) else 0.0

    return vol_w if position_sizing_mode == "inverse_atr" else active.astype(float)


EXIT_RISK_ASPECTS: Dict[str, ExitRiskAspect] = {
    "rsi_cross_exit": ExitRiskAspect(
        key="rsi_cross_exit", factor_tags=["mean_reversion"],
        run_fn=_exit_rsi_cross,
        warmup_fn=lambda p: max(p.get("rsi_exit_ma_period", 5), p.get("rsi_period", 2)),
        describe_fn=lambda p: (
            f"RSI cross ({p.get('rsi_exit_mode', 'rsi_cross')}), "
            f"stop-loss={p.get('rsi_stop_loss_pct')}, max-holding-days={p.get('rsi_max_holding_days')}"
        ),
    ),
    "swing_stop_target_exit": ExitRiskAspect(
        key="swing_stop_target_exit", factor_tags=["mean_reversion"],
        run_fn=_exit_swing_stop_target,
        warmup_fn=lambda p: p.get("swing_rsi_period", 5),
        describe_fn=lambda p: (
            f"stop-loss={p.get('swing_stop_loss_pct', 0.05)}, "
            f"reward:risk={p.get('swing_reward_risk_ratio', 3.0)}, trailing stop, "
            f"max-holding-days={p.get('swing_max_holding_days')}"
        ),
    ),
    "chan_signal_exit": ExitRiskAspect(
        key="chan_signal_exit", factor_tags=["regime_trend_strength"],
        run_fn=_exit_chan_signal,
        warmup_fn=lambda p: p.get("chan_min_strokes", 3) * 2 * (p.get("chan_min_gap_bars", 4) + 2),
        describe_fn=lambda p: (
            f"a symmetric downward Chan pivot shift, stop-loss={p.get('chan_stop_loss_pct')}, "
            f"max-holding-days={p.get('chan_max_holding_days')}"
        ),
    ),
    "chan3_point_exit": ExitRiskAspect(
        key="chan3_point_exit", factor_tags=["regime_trend_strength"],
        run_fn=_exit_chan3_point,
        warmup_fn=lambda p: max(
            (p.get("chan3_min_strokes", 3) ** 2) * 2 * (p.get("chan3_min_gap_bars", 4) + 2)
            + 2 * (p.get("chan3_min_gap_bars", 4) + 2),
            p.get("chan3_macd_slow", 26) + p.get("chan3_macd_signal", 9) + 10,
        ),
        describe_fn=lambda p: (
            f"a Chan-theory first/second/third-type sell point, stop-loss={p.get('chan3_stop_loss_pct')}, "
            f"max-holding-days={p.get('chan3_max_holding_days')}"
        ),
    ),
    "chanm_signal_exit": ExitRiskAspect(
        key="chanm_signal_exit", factor_tags=["regime_trend_strength"],
        run_fn=_exit_chanm_signal,
        warmup_fn=lambda p: max(
            p.get("chanm_min_strokes", 3) * 2 * (p.get("chanm_min_gap_bars", 4) + 2),
            p.get("chanm_macd_slow", 26) + p.get("chanm_macd_signal", 9) + 10,
        ),
        describe_fn=lambda p: (
            f"a symmetric downward Chan pivot shift or a top-divergence sell point (real MACD), "
            f"stop-loss={p.get('chanm_stop_loss_pct')}, max-holding-days={p.get('chanm_max_holding_days')}"
        ),
    ),
    "turtle_atr_trailing_exit": ExitRiskAspect(
        key="turtle_atr_trailing_exit", factor_tags=["volatility_targeting"],
        run_fn=_exit_turtle_atr_trailing,
        warmup_fn=lambda p: max(p.get("turtle_exit_breakout_days", 10), p.get("turtle_atr_period", 20)) + 1,
        describe_fn=lambda p: (
            f"a {p.get('turtle_exit_breakout_days', 10)}-day Donchian low or "
            f"{p.get('turtle_atr_stop_mult', 2.0)}N ATR trailing stop, sized by "
            f"{p.get('turtle_position_sizing_mode', 'inverse_atr')}"
        ),
    ),
}


# Maps each of the 4 decomposable `research_strategy` timing classes' own
# name to the (entry_key, exit_key) pair that reproduces its logic -- used
# by `build_composite_timing_candidates` to know which winning instances are
# decomposable and to avoid rebuilding a composite identical to one already
# searched.
TIMING_TEMPLATE_ASPECTS = {
    "RSIMeanReversionStrategy": ("rsi_oversold_entry", "rsi_cross_exit"),
    "SwingTrendPullbackStrategy": ("swing_pullback_entry", "swing_stop_target_exit"),
    "ChanPivotShiftStrategy": ("chan_pivot_entry", "chan_signal_exit"),
    "ChanThreeTypeStrategy": ("chan3_point_entry", "chan3_point_exit"),
    "ChanPivotShiftMACDStrategy": ("chanm_pivot_entry", "chanm_signal_exit"),
    "TurtleBreakoutStrategy": ("turtle_breakout_entry", "turtle_atr_trailing_exit"),
}


class CompositeTimingTemplate(AllocationTemplate):
    """A single-asset timing template built by pairing one
    `EntrySignalAspect` with one `ExitRiskAspect` from a DIFFERENT source
    template -- e.g. Turtle's Donchian-breakout entry with RSI's simpler
    percent-stop-loss/max-holding-days exit. See
    `build_composite_timing_candidates` for how/when these are constructed."""

    def __init__(self, entry: EntrySignalAspect, exit_: ExitRiskAspect, default_params: dict = None):
        """`default_params`, if given, backstops any key the entry/exit
        functions read but a caller's own `params` omits. Since
        `param_grid` is always `{}` here (these source templates only ever
        evaluate one fixed point, see module docstring), a fresh
        `common.allocation_search.grid_search_template` call (e.g.
        `backtester --optimize`) degenerates to a single trial with
        `params={}` -- without a `default_params` fallback that would
        silently fall through to every aspect function's own generic
        hardcoded defaults instead of the actually-tuned config this
        instance was built from."""
        self.entry = entry
        self.exit = exit_
        self.default_params = default_params or {}
        factor_tags = list(dict.fromkeys(entry.factor_tags + exit_.factor_tags))
        super().__init__(name=f"{entry.key}__{exit_.key}", param_grid={}, factor_tags=factor_tags)

    def generate_weights(self, universe, params: dict = None) -> pd.DataFrame:
        p = {**self.default_params, **(params or {})}
        cash_proxy = p.get("cash_proxy", "BIL")
        symbols = list(universe.keys())
        # `p` is always a fully-resolved StrategyConfig merge (never a
        # sparse call-time override), and StrategyConfig.risky_universe
        # defaults to an 8-symbol list on EVERY instance -- so passing `p`
        # itself as _get_risky_symbols' "explicit override" argument would
        # make that always-present, always-truthy risky_universe field
        # outrank cfg_symbol every time (see its own precedence docstring),
        # silently trading the whole default universe instead of the entry
        # aspect's own intended single symbol (e.g. "SPY"). Passing `{}`
        # there and resolving cfg_symbol from the entry aspect's own
        # `symbol_param_key` instead restores that precedence.
        cfg_symbol = p.get(self.entry.symbol_param_key)
        risky_symbols = _get_risky_symbols(universe, {}, cfg_symbol, p.get("risky_universe", []), cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = universe[risky_symbols[0]].index
        raw_weights = {}
        for sym in risky_symbols:
            df = universe[sym]
            entry_signal = self.entry.compute(df, p)
            raw_weights[sym] = self.exit.run(df, entry_signal, p)

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        p = {**self.default_params, **(params or {})}
        return (
            f"Composite Timing Strategy ({self.name}): enters on {self.entry.describe(p)}; "
            f"exits/manages risk via {self.exit.describe(p)}. This pairing is NOT one of "
            f"research_strategy's original templates -- assembled by strategy_generator's "
            f"aspect-composition search from two different templates' own entry/exit logic."
        )

    def warmup_bars(self, params: dict = None) -> int:
        p = {**self.default_params, **(params or {})}
        return max(self.entry.warmup_bars(p), self.exit.warmup_bars(p))


def build_composite_timing_candidates(best_per_template: dict, top_k: int = 4) -> list:
    """Given `_search_allocation`'s `best_per_template`, returns a list of
    `(CompositeTimingTemplate, merged_params)` pairs worth evaluating: the
    cross product of the top-`top_k` decomposable timing templates' own
    entry/exit keys, excluding any pairing already present among those
    top-k templates. Only ever produces candidates when >=2 of the 4
    decomposable classes (`TIMING_TEMPLATE_ASPECTS`) were actually supplied
    as candidates for this run (e.g. via `run_strategygen.py
    --research-strategy KEY...`) -- these templates always evaluate a
    single fixed parameter point (`param_grid == {}`), so "best params" is
    just each winning instance's own resolved `.config`, no grid search
    needed.
    """
    decomposable = []
    for name, result in best_per_template.items():
        template = result["template"]
        cls_name = type(template).__name__
        if cls_name in TIMING_TEMPLATE_ASPECTS and hasattr(template, "config"):
            decomposable.append((name, TIMING_TEMPLATE_ASPECTS[cls_name], result, template))
    if len(decomposable) < 2:
        return []

    decomposable.sort(key=lambda t: t[2]["score"], reverse=True)
    top = decomposable[:top_k]

    existing_pairs = {aspects for _, aspects, _, _ in top}
    entry_params, exit_params = {}, {}
    for _, (entry_key, exit_key), _, template in top:
        cfg_dict = asdict(template.config)
        entry_params.setdefault(entry_key, cfg_dict)
        exit_params.setdefault(exit_key, cfg_dict)

    candidates = []
    for entry_key, ep in entry_params.items():
        for exit_key, xp in exit_params.items():
            if (entry_key, exit_key) in existing_pairs:
                continue
            merged_params = {**xp, **ep}
            template = CompositeTimingTemplate(
                ENTRY_SIGNAL_ASPECTS[entry_key], EXIT_RISK_ASPECTS[exit_key], default_params=merged_params,
            )
            candidates.append((template, merged_params))

    return candidates

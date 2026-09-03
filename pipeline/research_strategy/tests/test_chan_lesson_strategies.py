"""Unit and integration tests for the new lesson-grounded Chan strategies
(research_strategy/rs/chan_lesson_strategies.py):

- ChanPivotOscillationStrategy (Lesson 92, 中枢震荡监视器)
- ChanFiboSectorStrengthStrategy (Lesson 106, 斐波那契均线系统)
- ChanFailedRetestBuyStrategy (Lesson 108, 下探失败买)
- ChanPivotShiftMACDAdvStrategy (Lessons 92-99/103/024/027/033/056/061,
  an enhanced sibling of ChanPivotShiftMACDStrategy)

Guaranteed 100% offline using synthetic OHLCV data / hand-built fixtures --
no network access.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from research_strategy.rs.chan_lesson_strategies import (
    ChanFailedRetestBuyStrategy,
    ChanFiboSectorStrengthStrategy,
    ChanPivotOscillationStrategy,
    ChanPivotShiftMACDAdvStrategy,
    _FIBO_PERIODS,
    _failed_retest_confirmed,
    compute_fibo_tier,
    compute_pivot_oscillation_signals,
)
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import instantiate_strategy_from_config_entry
from research_strategy.tests.test_chan_advanced_strategies import create_mock_universe


# --- compute_pivot_oscillation_signals (Lesson 92) ---------------------------

def test_compute_pivot_oscillation_signals_no_signals_on_a_flat_series():
    closes = np.full(100, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_pivot_oscillation_signals(df)
    assert set(sig.columns) == {"bullish_bias", "bearish_bias", "bull_trap", "bear_trap"}
    assert sig.index.equals(df.index)
    assert not sig["bullish_bias"].any()
    assert not sig["bull_trap"].any()


def test_compute_pivot_oscillation_signals_detects_bullish_bias_and_bull_trap(monkeypatch):
    """Hand-built strokes/pivots (bypassing the real merge/fractal/stroke
    detection via monkeypatch, for full determinism): a pivot [zg=108, zd=102]
    whose 2nd sub-swing stroke's midpoint (Zn=106) rises above both the prior
    stroke's Zn (105) and the pivot center Z (105) -> bullish_bias; then a
    breakout above zg that reverts back inside the band within
    `trap_confirm_bars` -> bull_trap."""
    import research_strategy.rs.chan_lesson_strategies as cls_mod

    n = 30
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.arange(100.0, 100.0 + n)
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    strokes = pd.DataFrame(
        [
            {"start_pos": 0, "end_pos": 5, "start_price": 100.0, "end_price": 110.0, "direction": "up", "bars": 5},
            {"start_pos": 5, "end_pos": 10, "start_price": 110.0, "end_price": 102.0, "direction": "down", "bars": 5},
            {"start_pos": 10, "end_pos": 15, "start_price": 102.0, "end_price": 108.0, "direction": "up", "bars": 5},
            {"start_pos": 15, "end_pos": 20, "start_price": 108.0, "end_price": 115.0, "direction": "up", "bars": 5},
            {"start_pos": 20, "end_pos": 24, "start_price": 115.0, "end_price": 104.0, "direction": "down", "bars": 4},
        ]
    )
    pivots = pd.DataFrame(
        [{"start_pos": 0, "end_pos": 15, "zg": 108.0, "zd": 102.0, "gg": 110.0, "dd": 100.0, "start_stroke_idx": 0, "end_stroke_idx": 2}]
    )

    monkeypatch.setattr(cls_mod, "build_strokes", lambda fractals, min_gap_bars: strokes)
    monkeypatch.setattr(cls_mod, "build_pivots", lambda strokes_arg, min_strokes: pivots)

    sig = compute_pivot_oscillation_signals(df, min_gap_bars=1, min_strokes=3, trap_confirm_bars=6)

    assert sig["bullish_bias"].sum() == 1
    assert sig["bullish_bias"].iloc[11]
    assert sig["bull_trap"].sum() == 1
    assert sig["bull_trap"].iloc[25]
    assert not sig["bear_trap"].any()


# --- compute_fibo_tier (Lesson 106) -------------------------------------------

def test_compute_fibo_tier_is_zero_on_a_flat_series():
    closes = np.full(300, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    close = pd.Series(closes, index=idx)
    tier = compute_fibo_tier(close)
    assert (tier == 0).all()


def test_compute_fibo_tier_matches_direct_sma_computation():
    from common.indicators import sma

    n = 260
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = 100.0 + np.cumsum(np.random.RandomState(7).normal(0, 1, n))
    close = pd.Series(closes, index=idx)

    tier = compute_fibo_tier(close)
    expected = pd.Series(0.0, index=idx)
    for period in _FIBO_PERIODS:
        ma = sma(close, period)
        expected = expected + (close > ma).fillna(False).astype(float)

    pd.testing.assert_series_equal(tier, expected)
    assert tier.min() >= 0 and tier.max() <= len(_FIBO_PERIODS)


# --- _failed_retest_confirmed (Lesson 108) ------------------------------------

def test_failed_retest_confirmed_only_fires_on_a_higher_second_low(monkeypatch):
    import research_strategy.rs.chan_lesson_strategies as cls_mod

    n = 20
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.linspace(100, 120, n)
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    merged = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "orig_pos": np.arange(n)}, index=idx)
    fractals = pd.DataFrame(
        [
            {"pos": 3, "kind": "bottom", "price": 90.0},   # the bottom the B1 divergence was fished from
            {"pos": 10, "kind": "bottom", "price": 85.0},  # a LOWER second dip -> not a failed retest
            {"pos": 14, "kind": "bottom", "price": 95.0},  # a HIGHER second dip -> failed retest confirms here
        ]
    )

    monkeypatch.setattr(cls_mod, "merge_inclusion", lambda df: merged)
    monkeypatch.setattr(cls_mod, "find_fractals", lambda m: fractals)

    first_buy = pd.Series(False, index=idx)
    first_buy.iloc[4] = True  # confirm bar right after the pos=3 bottom fractal
    sig = pd.DataFrame({"first_buy": first_buy})

    confirmed = _failed_retest_confirmed(bars, sig, confirm_window_bars=12)
    assert confirmed.sum() == 1
    assert confirmed.iloc[15]


def test_failed_retest_confirmed_no_first_buy_returns_all_false():
    n = 10
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.linspace(100, 110, n)
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = pd.DataFrame({"first_buy": pd.Series(False, index=idx)})
    confirmed = _failed_retest_confirmed(bars, sig, confirm_window_bars=5)
    assert not confirmed.any()


# --- Strategy execution tests --------------------------------------------------

def test_chan_pivot_oscillation_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanPivotOscillationStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "中枢震荡监视器" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)
    assert isinstance(weights, pd.DataFrame)


def test_chan_fibo_sector_strength_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanFiboSectorStrengthStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "斐波那契均线系统" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)
    assert isinstance(weights, pd.DataFrame)


def test_chan_fibo_sector_strength_rotates_into_top_tier_symbols():
    cfg = StrategyConfig(fibo_top_k=1, fibo_min_tier=0, fibo_rebalance_freq_days=21)
    strat = ChanFiboSectorStrengthStrategy(cfg)

    n = 300
    idx = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    strong = 100.0 + 0.5 * t
    weak = 100.0 - 0.05 * t
    from common.testing import make_ohlcv_from_closes

    universe = {}
    for sym, arr in [("STRONG", strong), ("WEAK", weak), ("BIL", np.full(n, 100.0))]:
        df = make_ohlcv_from_closes(arr)
        df.index = idx
        universe[sym] = df

    weights = strat.generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert (daily["STRONG"].iloc[250:] > 0).any()
    assert not (daily["WEAK"].iloc[250:] > 0).any()


def test_chan_failed_retest_buy_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanFailedRetestBuyStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "下探失败买" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)
    assert isinstance(weights, pd.DataFrame)


# --- ChanPivotShiftMACDAdvStrategy --------------------------------------------

def _adv_universe(n=60):
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = 100.0 + np.arange(n) * 0.1
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    bil = pd.DataFrame({"Open": np.full(n, 100.0), "High": np.full(n, 100.5), "Low": np.full(n, 99.5), "Close": np.full(n, 100.0)}, index=idx)
    return idx, {"SPY": bars, "BIL": bil}


def _patch_adv_fakes(
    monkeypatch,
    buy_at=None, sell_at=None, weak_buy_at=None, weak_sell_at=None, divergence_buy_at=None,
    zero_axis_true=True, weekly_regime_true=True, danger_from=None, coincidence_at=None,
):
    import research_strategy.rs.chan_lesson_strategies as cls_mod

    def fake_pivot_macd_sig(bars_arg, **kwargs):
        idx = bars_arg.index
        cols = {
            k: pd.Series(False, index=idx)
            for k in ["buy_signal", "sell_signal", "divergence_buy", "divergence_sell", "weak_divergence_buy", "weak_divergence_sell"]
        }
        if buy_at is not None:
            cols["buy_signal"].iloc[buy_at] = True
        if sell_at is not None:
            cols["sell_signal"].iloc[sell_at] = True
        if weak_buy_at is not None:
            cols["weak_divergence_buy"].iloc[weak_buy_at] = True
        if weak_sell_at is not None:
            cols["weak_divergence_sell"].iloc[weak_sell_at] = True
        if divergence_buy_at is not None:
            cols["divergence_buy"].iloc[divergence_buy_at] = True
        return pd.DataFrame(cols)

    def fake_chan3_sig(bars_arg, **kwargs):
        idx = bars_arg.index
        cols = {
            k: pd.Series(False, index=idx)
            for k in ["first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell", "buy_signal", "sell_signal"]
        }
        if coincidence_at is not None:
            cols["second_buy"].iloc[coincidence_at] = True
        return pd.DataFrame(cols)

    def fake_zero_axis(close, macd_fast, macd_slow, macd_signal):
        return pd.Series(zero_axis_true, index=close.index)

    def fake_weekly_regime(bars_arg, min_gap_bars, min_strokes, macd_fast, macd_slow, macd_signal):
        return pd.Series(weekly_regime_true, index=bars_arg.index)

    def fake_danger(bars_arg, min_gap_bars, min_strokes):
        s = pd.Series(False, index=bars_arg.index)
        if danger_from is not None:
            s.iloc[danger_from:] = True
        return s

    monkeypatch.setattr(cls_mod, "compute_chan_pivot_macd_signals", fake_pivot_macd_sig)
    monkeypatch.setattr(cls_mod, "compute_chan3_signals", fake_chan3_sig)
    monkeypatch.setattr(cls_mod, "_macd_zero_axis_confirmed", fake_zero_axis)
    monkeypatch.setattr(cls_mod, "_weekly_pivot_macd_regime_state", fake_weekly_regime)
    monkeypatch.setattr(cls_mod, "_pivot_relation_danger_series", fake_danger)


def test_chan_pivot_shift_macd_adv_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanPivotShiftMACDAdvStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "chan_pivot_shift_macd_adv" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)
    assert isinstance(weights, pd.DataFrame)


def test_chan_pivot_shift_macd_adv_zero_axis_gate(monkeypatch):
    """The zero-axis gate applies only to the DIVERGENCE-sourced portion of
    buy_signal (bottom-fishing) -- a pivot-shift-sourced buy (not marked
    divergence_buy) is a trend-continuation breakout, not a bottom-fish, and
    is unaffected by this gate (see test_..._pivot_shift_bypasses_zero_axis_gate)."""
    idx, universe = _adv_universe()
    entry_bar = 10

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, divergence_buy_at=entry_bar, zero_axis_true=False)
    weights = ChanPivotShiftMACDAdvStrategy(StrategyConfig()).generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert daily["SPY"].iloc[entry_bar] == 0.0, "divergence entry suppressed while MACD zero-axis gate fails"

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, divergence_buy_at=entry_bar, zero_axis_true=True)
    weights = ChanPivotShiftMACDAdvStrategy(StrategyConfig()).generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert daily["SPY"].iloc[entry_bar] > 0.0, "entry fires once the zero-axis gate is satisfied"


def test_chan_pivot_shift_macd_adv_pivot_shift_bypasses_zero_axis_gate(monkeypatch):
    """A pivot-shift-sourced buy (buy_signal True, divergence_buy False) is a
    trend-continuation breakout, not a bottom-fish -- Lesson 103's zero-axis
    gate must NOT block it even while MACD is below zero."""
    idx, universe = _adv_universe()
    entry_bar = 10

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, divergence_buy_at=None, zero_axis_true=False)
    weights = ChanPivotShiftMACDAdvStrategy(StrategyConfig()).generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert daily["SPY"].iloc[entry_bar] > 0.0, "pivot-shift entry fires regardless of the zero-axis gate"


def test_chan_pivot_shift_macd_adv_weekly_regime_gate(monkeypatch):
    idx, universe = _adv_universe()
    entry_bar = 10

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, weekly_regime_true=False)
    weights = ChanPivotShiftMACDAdvStrategy(StrategyConfig()).generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert daily["SPY"].iloc[entry_bar] == 0.0, "entry suppressed while the weekly regime disagrees"


def test_chan_pivot_shift_macd_adv_exits_on_dangerous_pivot_relation(monkeypatch):
    idx, universe = _adv_universe()
    entry_bar, danger_bar = 5, 20

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, danger_from=danger_bar)
    weights = ChanPivotShiftMACDAdvStrategy(StrategyConfig()).generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)
    assert daily["SPY"].iloc[entry_bar + 1] > 0.0, "position open before the dangerous pivot-relation flip"
    assert daily["SPY"].iloc[danger_bar] == 0.0, "dangerous pivot-relation overlay forces an exit"


def test_chan_pivot_shift_macd_adv_cross_pivot_divergence_filter(monkeypatch):
    idx, universe = _adv_universe()
    entry_bar = 10

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, weak_buy_at=entry_bar)
    cfg_on = StrategyConfig(chanm_adv_require_cross_pivot_divergence=True)
    weights_on = ChanPivotShiftMACDAdvStrategy(cfg_on).generate_weights(universe)
    daily_on = weights_on.reindex(idx).ffill().fillna(0.0)
    assert daily_on["SPY"].iloc[entry_bar] == 0.0, "a weak (盘整背驰) divergence buy is filtered out by default"

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, weak_buy_at=entry_bar)
    cfg_off = StrategyConfig(chanm_adv_require_cross_pivot_divergence=False)
    weights_off = ChanPivotShiftMACDAdvStrategy(cfg_off).generate_weights(universe)
    daily_off = weights_off.reindex(idx).ffill().fillna(0.0)
    assert daily_off["SPY"].iloc[entry_bar] > 0.0, "disabling the filter restores the full (including weak) divergence signal"


def test_chan_pivot_shift_macd_adv_coincidence_sizing(monkeypatch):
    idx, universe = _adv_universe()
    entry_bar = 10
    cfg = StrategyConfig(chanm_adv_position_size_pct=1.0, chanm_adv_weak_signal_position_size_pct=0.5)

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, coincidence_at=entry_bar)
    weights_coincident = ChanPivotShiftMACDAdvStrategy(cfg).generate_weights(universe)
    daily_coincident = weights_coincident.reindex(idx).ffill().fillna(0.0)
    assert daily_coincident["SPY"].iloc[entry_bar] == pytest.approx(1.0)

    _patch_adv_fakes(monkeypatch, buy_at=entry_bar, coincidence_at=None)
    weights_weak = ChanPivotShiftMACDAdvStrategy(cfg).generate_weights(universe)
    daily_weak = weights_weak.reindex(idx).ffill().fillna(0.0)
    assert daily_weak["SPY"].iloc[entry_bar] == pytest.approx(0.5)


# --- Integration: strategies_config.json discovery ----------------------------

@pytest.mark.parametrize("key", [
    "chan_pivot_oscillation",
    "chan_fibo_sector_strength",
    "chan_failed_retest_buy",
    "chan_pivot_shift_macd_adv",
])
def test_instantiate_strategy_from_config(key: str):
    config_dict = load_strategies_config()
    assert key in config_dict

    entry = config_dict[key]
    strat_inst = instantiate_strategy_from_config_entry(key, entry)

    assert strat_inst is not None
    assert hasattr(strat_inst, "generate_weights")
    assert hasattr(strat_inst, "explain_weights")
    assert hasattr(strat_inst, "warmup_bars")

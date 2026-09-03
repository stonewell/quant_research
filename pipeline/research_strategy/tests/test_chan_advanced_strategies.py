"""Unit and Integration Tests for Chan Theory Advanced & Compound Quantitative Strategies.

Tests all 5 new strategies:
- ChanMultiTimeframeTrendStrategy
- ChanTrendThirdBuyStrategy
- ChanMeanReversionDivergenceStrategy
- ChanCompositeStrategy
- ChanBestSelectorStrategy

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py.
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

from common.testing import make_ohlcv_from_closes, make_oscillating_df
from research_strategy.rs.chan_advanced_strategies import (
    ChanBestSelectorStrategy,
    ChanCompositeStrategy,
    ChanMeanReversionDivergenceStrategy,
    ChanMultiTimeframeTrendStrategy,
    ChanTrendThirdBuyStrategy,
    run_composite_position_loop,
    run_mrd_position_exit,
)
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import instantiate_strategy_from_config_entry


def create_mock_universe(n_days: int = 350) -> dict:
    """Creates a deterministic synthetic universe for testing strategy mechanics."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)

    spy_close = 100.0 + 0.1 * t + 3.0 * np.sin(t / 8.0)
    qqq_close = 100.0 + 0.15 * t + 5.0 * np.sin(t / 6.0)
    bil_close = 100.0 + 0.001 * t

    symbols = {
        "SPY": spy_close,
        "QQQ": qqq_close,
        "BIL": bil_close,
    }

    universe = {}
    for sym, close_arr in symbols.items():
        df = make_ohlcv_from_closes(close_arr)
        df.index = dates
        universe[sym] = df

    return universe


# --- Unit Tests: Helper Loops ------------------------------------------------

def test_run_mrd_position_exit_basic_and_stops():
    close = np.array([100.0, 100.0, 102.0, 105.0, 93.0, 92.0])
    entry = np.array([False, True, False, False, False, False])
    exit_sig = np.array([False, False, False, False, False, False])

    # Stop loss at 5% (drop from 100 to 93 triggers stop)
    raw = run_mrd_position_exit(
        close, entry, exit_sig,
        stop_loss_pct=0.05,
        profit_target_pct=0.20,
        trailing_stop_pct=None,
        trailing_activate_pct=None,
        max_holding_days=10,
        position_size_pct=1.0,
    )

    assert raw[0] == 0.0
    assert raw[1] == 1.0  # Entry
    assert raw[2] == 1.0
    assert raw[3] == 1.0
    assert raw[4] == 0.0  # Stopped out (100 -> 93 = -7%)
    assert raw[5] == 0.0


def test_run_composite_position_loop_scaling():
    close = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 100.0])
    first_buy = np.array([False, True, False, False, False, False])
    second_buy = np.array([False, False, True, False, False, False])
    third_buy = np.array([False, False, False, True, False, False])
    sell_sig = np.array([False, False, False, False, True, False])

    raw = run_composite_position_loop(
        close, first_buy, second_buy, third_buy, sell_sig,
        b1_w=0.30, b2_w=0.40, b3_w=0.30,
        stop_loss_pct=0.10, max_holding_days=10,
    )

    assert raw[0] == 0.0
    assert raw[1] == 0.30  # B1 initial
    assert raw[2] == 0.70  # B2 addition (+0.40)
    assert raw[3] == 1.00  # B3 addition (+0.30)
    assert raw[4] == 0.00  # Sell signal clears
    assert raw[5] == 0.00


def test_run_composite_position_loop_ignores_b2_b3_while_flat():
    """Review fix 3: only B1 (first_buy) may open a position from flat -- a
    lone B2/B3 signal with no prior B1 is not actionable."""
    close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    first_buy = np.array([False, False, False, False, False])
    second_buy = np.array([False, True, False, False, False])
    third_buy = np.array([False, False, True, False, False])
    sell_sig = np.array([False, False, False, False, False])

    raw = run_composite_position_loop(
        close, first_buy, second_buy, third_buy, sell_sig,
        b1_w=0.30, b2_w=0.40, b3_w=0.30, stop_loss_pct=0.10, max_holding_days=10,
    )
    assert (raw == 0.0).all()


def test_run_composite_position_loop_weighted_average_cost_basis_on_scale_in():
    """Review fix 4: entry_price is a weighted-average cost basis, updated
    on every B2/B3 scale-in, not left at the original B1 fill price."""
    close = np.array([100.0, 200.0, 145.0, 140.0, 140.0])
    first_buy = np.array([True, False, False, False, False])
    second_buy = np.array([False, True, False, False, False])
    third_buy = np.array([False, False, False, False, False])
    sell_sig = np.array([False, False, False, False, False])

    raw = run_composite_position_loop(
        close, first_buy, second_buy, third_buy, sell_sig,
        b1_w=0.30, b2_w=0.40, b3_w=0.30, stop_loss_pct=0.10, max_holding_days=100,
    )
    # Blended cost after the B2 add: (100*0.30 + 200*0.40) / 0.70 ~= 157.14.
    assert raw[0] == pytest.approx(0.30)
    assert raw[1] == pytest.approx(0.70)
    assert raw[2] == pytest.approx(0.70), "-7.7% from the blended ~157.14 cost -- not stopped yet"
    assert raw[3] == 0.0, "stopped once price falls >10% below the BLENDED cost (~157.14), not the raw B1 fill (100)"


# --- Strategy Tests ----------------------------------------------------------

def test_chan_mtf_trend_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanMultiTimeframeTrendStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "区间套" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)
    if not weights.empty:
        assert "SPY" in weights.columns or "QQQ" in weights.columns or "BIL" in weights.columns


def test_chan_trend_third_buy_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanTrendThirdBuyStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "第三类买卖点" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)


def test_chan_mean_reversion_divergence_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanMeanReversionDivergenceStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "一类买卖点" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)


def test_chan_composite_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanCompositeStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "一二三类买点" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)


def test_chan_best_selector_strategy_execution():
    cfg = StrategyConfig()
    strat = ChanBestSelectorStrategy(cfg)

    assert strat.warmup_bars() > 0
    assert "动态最佳缠论策略选择器" in strat.explain_weights()

    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)

    assert isinstance(weights, pd.DataFrame)


# --- Review fix 1: Lesson 103 MACD zero-axis entry gate ---------------------

def test_macd_zero_axis_confirmed_matches_direct_macd_computation():
    from research_strategy.rs.chan_advanced_strategies import _macd_zero_axis_confirmed
    from common.indicators import macd as macd_fn

    n = 150
    closes = 100.0 + np.cumsum(np.concatenate([np.full(70, -0.3), np.full(80, 0.35)]))
    idx = pd.bdate_range("2020-01-01", periods=n)
    close = pd.Series(closes, index=idx)

    result = _macd_zero_axis_confirmed(close, 12, 26, 9)
    macd_df = macd_fn(close, 12, 26, 9)
    expected = ((macd_df["macd"] >= 0) & (macd_df["signal"] >= 0)).fillna(False)
    pd.testing.assert_series_equal(result, expected, check_names=False)
    assert result.iloc[:70].sum() == 0, "MACD should still be below zero throughout the decline"
    assert result.iloc[-1], "MACD should have reclaimed the zero axis by the end of the rally"


def test_mean_reversion_divergence_suppresses_entry_below_macd_zero_axis(monkeypatch):
    from research_strategy.rs import chan_advanced_strategies as cas

    n = 220
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.concatenate([np.full(40, 100.0), np.linspace(100, 70, 40)[1:], np.linspace(70, 140, n - 79)])
    close = pd.Series(closes, index=idx)

    from common.indicators import macd as macd_fn
    zero_axis_ok = cas._macd_zero_axis_confirmed(close, 12, 26, 9)
    pre_gate_bar = 60
    assert not zero_axis_ok.iloc[pre_gate_bar], "fixture bar must be below the zero axis"
    post_gate_bar = int(np.flatnonzero(zero_axis_ok.to_numpy())[len(np.flatnonzero(zero_axis_ok.to_numpy())) // 2])
    assert zero_axis_ok.iloc[post_gate_bar]

    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    bil = pd.DataFrame({"Open": np.full(n, 100.0), "High": np.full(n, 100.5), "Low": np.full(n, 99.5), "Close": np.full(n, 100.0)}, index=idx)
    universe = {"SPY": bars, "BIL": bil}

    def fake_sig(bars_arg, **kwargs):
        first_buy = pd.Series(False, index=bars_arg.index)
        if len(bars_arg) == n:
            first_buy.iloc[pre_gate_bar] = True
            first_buy.iloc[post_gate_bar] = True
        cols = {k: pd.Series(False, index=bars_arg.index) for k in
                ["first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell"]}
        cols["first_buy"] = first_buy
        cols["buy_signal"] = first_buy
        cols["sell_signal"] = pd.Series(False, index=bars_arg.index)
        return pd.DataFrame(cols)

    monkeypatch.setattr(cas, "compute_chan3_signals", fake_sig)

    cfg = StrategyConfig()
    strat = cas.ChanMeanReversionDivergenceStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)

    assert daily["SPY"].iloc[pre_gate_bar] == 0.0, "entry must be suppressed while MACD is below the zero axis"
    assert daily["SPY"].iloc[post_gate_bar] > 0.0, "entry should fire once MACD has reclaimed the zero axis"


# --- Review fix 2 / Part 5: weekly 区间套 regime + precise-trend gate --------

def test_weekly_regime_state_persists_between_weekly_buy_and_sell(monkeypatch):
    from research_strategy.rs import chan_advanced_strategies as cas

    n = 300
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = 100.0 + 0.05 * np.arange(n)
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    def fake_weekly_sig(df, **kwargs):
        buy = pd.Series(False, index=df.index)
        sell = pd.Series(False, index=df.index)
        buy.iloc[5] = True
        sell.iloc[20] = True
        return pd.DataFrame({"buy_signal": buy, "sell_signal": sell})

    monkeypatch.setattr(cas, "compute_chan3_signals", fake_weekly_sig)

    regime = cas._weekly_regime_state(bars, min_gap_bars=4, min_strokes=3)

    weekly_bars = bars.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna(subset=["Close"])
    buy_pos = bars.index.get_indexer([weekly_bars.index[5]])[0]
    sell_pos = bars.index.get_indexer([weekly_bars.index[20]])[0]

    assert not regime.iloc[:buy_pos].any(), "regime should be False before the first weekly buy"
    assert regime.iloc[buy_pos:sell_pos].all(), "regime should persist True between the weekly buy and sell"
    assert not regime.iloc[sell_pos + 1 : sell_pos + 10].any(), "regime should turn False again after the weekly sell"


def test_precise_trend_confirmed_persists_between_third_buy_and_any_sell():
    from research_strategy.rs.chan_advanced_strategies import _precise_trend_confirmed

    idx = pd.bdate_range("2020-01-01", periods=10)
    third_buy = pd.Series([False, False, False, True, False, False, False, False, False, False], index=idx)
    sell_signal = pd.Series([False, False, False, False, False, False, False, True, False, False], index=idx)
    sig = pd.DataFrame({"third_buy": third_buy, "sell_signal": sell_signal})

    result = _precise_trend_confirmed(sig)
    expected = pd.Series([False, False, False, True, True, True, True, False, False, False], index=idx)
    pd.testing.assert_series_equal(result, expected, check_names=False)


# --- Part 2: pivot-relation "dangerous consolidation" overlay --------------

def test_chan_composite_exits_on_dangerous_pivot_relation_overlay(monkeypatch):
    from research_strategy.rs import chan_advanced_strategies as cas

    n = 60
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = 100.0 + np.arange(n) * 0.1
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    bil = pd.DataFrame({"Open": np.full(n, 100.0), "High": np.full(n, 100.5), "Low": np.full(n, 99.5), "Close": np.full(n, 100.0)}, index=idx)
    universe = {"SPY": bars, "BIL": bil}

    danger_bar = 30

    def fake_sig(bars_arg, **kwargs):
        first_buy = pd.Series(False, index=bars_arg.index)
        first_buy.iloc[5] = True
        cols = {k: pd.Series(False, index=bars_arg.index) for k in
                ["first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell"]}
        cols["first_buy"] = first_buy
        cols["second_buy"] = pd.Series(False, index=bars_arg.index)
        cols["third_buy"] = pd.Series(False, index=bars_arg.index)
        cols["buy_signal"] = first_buy
        cols["sell_signal"] = pd.Series(False, index=bars_arg.index)
        return pd.DataFrame(cols)

    def fake_danger(bars_arg, min_gap_bars, min_strokes):
        danger = pd.Series(False, index=bars_arg.index)
        danger.iloc[danger_bar:] = True
        return danger

    monkeypatch.setattr(cas, "compute_chan3_signals", fake_sig)
    monkeypatch.setattr(cas, "_pivot_relation_danger_series", fake_danger)

    cfg = StrategyConfig()
    strat = cas.ChanCompositeStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)

    assert daily["SPY"].iloc[6] > 0.0, "B1 should open the position"
    assert daily["SPY"].iloc[danger_bar] == 0.0, "dangerous pivot-relation overlay should force an exit"


# --- Integration Tests: strategies_config.json Discovery -------------------

@pytest.mark.parametrize("key", [
    "chan_mtf_trend",
    "chan_trend_third_buy",
    "chan_mean_reversion_divergence",
    "chan_composite",
    "chan_best_selector",
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

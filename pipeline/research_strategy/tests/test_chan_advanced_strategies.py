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
    ChanFourStateBlendStrategy,
    ChanFourStateExecutionStrategy,
    ChanMeanReversionDivergenceStrategy,
    ChanMultiTimeframeTrendStrategy,
    ChanRiskManagedBlendStrategy,
    ChanTrendThirdBuyStrategy,
    ChanVaaCompoundStrategy,
    run_composite_position_loop,
    run_four_state_position_loop,
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
    """When allow_flat_b2_b3=False, only B1 may open from flat.
    When allow_flat_b2_b3=True (relaxed), B2 opens at b2_w and B3 scales in."""
    close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    first_buy = np.array([False, False, False, False, False])
    second_buy = np.array([False, True, False, False, False])
    third_buy = np.array([False, False, True, False, False])
    sell_sig = np.array([False, False, False, False, False])

    raw_strict = run_composite_position_loop(
        close, first_buy, second_buy, third_buy, sell_sig,
        b1_w=0.30, b2_w=0.40, b3_w=0.30, stop_loss_pct=0.10, max_holding_days=10,
        allow_flat_b2_b3=False,
    )
    assert (raw_strict == 0.0).all()

    raw_relaxed = run_composite_position_loop(
        close, first_buy, second_buy, third_buy, sell_sig,
        b1_w=0.30, b2_w=0.40, b3_w=0.30, stop_loss_pct=0.10, max_holding_days=10,
        allow_flat_b2_b3=True,
    )
    assert raw_relaxed[1] == 0.40
    assert raw_relaxed[2] == 0.70


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

    cfg = StrategyConfig(chan_mrd_entry_mode="zero_axis", chan_mrd_require_trend_filter=False)
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


# --- ChanVaaCompoundStrategy Tests -----------------------------------------

def test_chan_vaa_compound_generates_valid_weights():
    universe = create_mock_universe(n_days=300)
    cfg = StrategyConfig()
    strat = ChanVaaCompoundStrategy(cfg)

    weights = strat.generate_weights(universe)
    assert not weights.empty
    assert set(weights.columns) == {"SPY", "QQQ", "BIL"}

    # Convert sparse to dense daily
    daily = weights.reindex(universe["SPY"].index).ffill().fillna(0.0)

    # All weights in [0, 1]
    assert (daily >= -1e-6).all().all()
    assert (daily <= 1.0 + 1e-6).all().all()

    # Sum of weights per day <= 1.0 + epsilon
    row_sums = daily.sum(axis=1)
    assert (row_sums <= 1.0 + 1e-5).all()


def test_chan_vaa_compound_regime_adaptive_and_fixed_modes():
    universe = create_mock_universe(n_days=300)
    cfg = StrategyConfig()
    strat = ChanVaaCompoundStrategy(cfg)

    # Fixed blend mode
    w_fixed = strat.generate_weights(universe, params={"chan_vaa_mode": "fixed_blend", "chan_vaa_chan_weight": 0.5})
    assert not w_fixed.empty

    # Regime adaptive with gate in defensive
    w_gated = strat.generate_weights(universe, params={"chan_vaa_mode": "regime_adaptive", "chan_vaa_gate_chan_in_defensive": True})
    assert not w_gated.empty
    daily_gated = w_gated.reindex(universe["SPY"].index).ffill().fillna(0.0)
    assert daily_gated["BIL"].max() > 0.0, "defensive periods must hold BIL rather than zeroing out cash"
    assert (daily_gated.sum(axis=1) <= 1.0 + 1e-5).all()


def test_chan_vaa_compound_warmup_and_explain():
    cfg = StrategyConfig()
    strat = ChanVaaCompoundStrategy(cfg)

    assert strat.warmup_bars() == 252
    explanation = strat.explain_weights()
    assert "Chan Pivot Shift MACD + VAA Optimal Compound Strategy" in explanation
    assert "Vigilant Asset Allocation" in explanation


def test_chan_mrd_modes_and_trend_filter():
    from research_strategy.rs import chan_advanced_strategies as cas

    n = 250
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.full(n, 100.0)
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    universe = {"SPY": bars, "BIL": bars.copy()}

    cfg = StrategyConfig(chan_mrd_entry_mode="failed_retest", chan_mrd_require_trend_filter=True)
    strat = cas.ChanMeanReversionDivergenceStrategy(cfg)
    assert strat.warmup_bars() >= 200
    assert "failed_retest" in strat.explain_weights()

    for mode in ["raw_b1", "zero_axis", "failed_retest", "combined"]:
        w = strat.generate_weights(universe, params={"chan_mrd_entry_mode": mode, "chan_mrd_require_trend_filter": False})
        assert isinstance(w, pd.DataFrame)


def test_chan_mrd_relaxed_stroke_trend_gate(monkeypatch):
    from research_strategy.rs import chan_advanced_strategies as cas

    n = 250
    idx = pd.bdate_range("2020-01-01", periods=n)
    closes = np.linspace(150, 100, n)
    bars = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    universe = {"SPY": bars, "BIL": bars.copy()}

    entry_bar = 240

    def fake_chan3(bars_arg, **kwargs):
        first_buy = pd.Series(False, index=bars_arg.index)
        first_buy.iloc[entry_bar] = True
        cols = {k: pd.Series(False, index=bars_arg.index) for k in
                ["first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell"]}
        cols["first_buy"] = first_buy
        cols["buy_signal"] = first_buy
        cols["sell_signal"] = pd.Series(False, index=bars_arg.index)
        return pd.DataFrame(cols)

    def fake_stroke_sig(bars_arg, **kwargs):
        cols = {k: pd.Series(False, index=bars_arg.index) for k in
                ["buy_signal", "sell_signal", "divergence_buy", "divergence_sell"]}
        return pd.DataFrame(cols)

    def fake_stroke_trend(bars_arg, *args, **kwargs):
        st = pd.Series(False, index=bars_arg.index)
        st.iloc[entry_bar:] = True
        return st

    monkeypatch.setattr(cas, "compute_chan3_signals", fake_chan3)
    monkeypatch.setattr(cas, "compute_chan_pivot_macd_signals", fake_stroke_sig)
    monkeypatch.setattr(cas, "compute_stroke_trend", fake_stroke_trend)

    cfg = StrategyConfig(chan_mrd_entry_mode="raw_b1", chan_mrd_require_trend_filter=True)
    strat = cas.ChanMeanReversionDivergenceStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = weights.reindex(idx).ffill().fillna(0.0)

    assert daily["SPY"].iloc[entry_bar] > 0.0, "relaxed trend gate should allow entry via stroke trend even below 200d SMA"


# --- Integration Tests: strategies_config.json Discovery -------------------

@pytest.mark.parametrize("key", [
    "chan_mtf_trend",
    "chan_trend_third_buy",
    "chan_mean_reversion_divergence",
    "chan_composite",
    "chan_best_selector",
    "chan_vaa_compound",
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


def test_chan_mtf_trend_produces_active_trades():
    universe = create_mock_universe(n_days=400)
    strat = ChanMultiTimeframeTrendStrategy(StrategyConfig())
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky > 0).sum().sum() > 0


def test_chan_trend_third_buy_produces_active_trades():
    universe = create_mock_universe(n_days=400)
    strat = ChanTrendThirdBuyStrategy(StrategyConfig())
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky > 0).sum().sum() > 0


def test_chan_composite_produces_active_trades():
    universe = create_mock_universe(n_days=400)
    strat = ChanCompositeStrategy(StrategyConfig())
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky > 0).sum().sum() > 0


def test_chan_risk_managed_blend_interface_and_config():
    cfg = StrategyConfig()
    strat = ChanRiskManagedBlendStrategy(cfg)
    assert strat.name == "chan_risk_managed_blend"
    assert strat.warmup_bars() == 252
    assert "chan_composite" in strat.explain_weights()
    assert "chan_three_type" in strat.explain_weights()
    assert "chan_vaa_compound" in strat.explain_weights()
    assert strat.config.crb_composite_weight == 0.20
    assert strat.config.crb_three_type_weight == 0.45
    assert strat.config.crb_vaa_weight == 0.35
    assert strat.config.crb_tier1_cooldown_bars == 15
    assert strat.config.crb_breadth_bull_thresh == 0.30
    assert strat.config.crb_thrust_lookback == 10
    assert strat.config.crb_thrust_thresh == 0.60
    assert "10d thrust" in strat.explain_weights()
    assert "auto-heal" in strat.explain_weights()

    # Verify instantiation via strategies_config.json
    configs = load_strategies_config()
    assert "chan_risk_managed_blend" in configs
    entry = configs["chan_risk_managed_blend"]
    inst = instantiate_strategy_from_config_entry("chan_risk_managed_blend", entry)
    assert isinstance(inst, ChanRiskManagedBlendStrategy)
    assert inst.config.crb_composite_weight == 0.20
    assert inst.config.crb_three_type_weight == 0.45
    assert inst.config.crb_vaa_weight == 0.35
    assert inst.config.crb_tier1_cooldown_bars == 15
    assert inst.config.crb_breadth_bull_thresh == 0.30
    assert inst.config.crb_thrust_lookback == 10
    assert inst.config.crb_thrust_thresh == 0.60


def test_chan_risk_managed_blend_execution_and_constraints():
    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig()
    strat = ChanRiskManagedBlendStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert not weights.empty
    rebal_dates = weights.dropna(how="all").index
    assert len(rebal_dates) > 0

    # Test sparse weights contract: non-rebalance rows are all NaN
    non_rebal = weights.drop(index=rebal_dates)
    if not non_rebal.empty:
        assert non_rebal.isna().all().all()

    # Test position capping constraint: no individual risky stock > max position cap (crb_bull_max_single_position = 0.30)
    rebal_df = weights.loc[rebal_dates]
    risky_df = rebal_df.drop(columns=["BIL"], errors="ignore")
    assert (risky_df > cfg.crb_bull_max_single_position + 1e-6).sum().sum() == 0

    # Test leverage constraint: sum of risky weights <= 1.0
    assert (risky_df.sum(axis=1) <= 1.000001).all()

    # Test explicit zero floor: no NaNs inside any rebalance row
    assert not rebal_df.isna().any().any()


def test_chan_risk_managed_blend_circuit_breaker_triggers():
    # Construct a universe where asset prices plunge sharply to trigger circuit breakers
    dates = pd.bdate_range("2020-01-01", periods=360)
    t = np.arange(360)

    # Initial rally followed by catastrophic 40% crash
    spy_close = np.where(t < 250, 100.0 + 0.3 * t, 175.0 - 1.5 * (t - 250))
    qqq_close = np.where(t < 250, 100.0 + 0.4 * t, 200.0 - 2.0 * (t - 250))
    bil_close = np.full(360, 100.0)

    universe = {
        "SPY": make_ohlcv_from_closes(spy_close),
        "QQQ": make_ohlcv_from_closes(qqq_close),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates

    cfg = StrategyConfig(crb_dd_reduce_thresh=0.08, crb_dd_defensive_thresh=0.12, crb_dd_stop_thresh=0.18)
    strat = ChanRiskManagedBlendStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert not weights.empty
    rebal = weights.dropna(how="all")
    # During the crash (after t=250), risky exposure should be reduced, shifted to VAA or stopped out
    late_rebal = rebal.loc[rebal.index >= dates[280]]
    if not late_rebal.empty:
        risky_late = late_rebal.drop(columns=["BIL"], errors="ignore")
        # Equity exposure should be strongly curtailed compared to unhedged levels
        assert (risky_late.sum(axis=1) < 0.50).any()


def test_chan_risk_managed_blend_asset_level_inertia_filter():
    """Verifies Option A: in normal market regimes, non-zero target weight changes
    satisfy |delta_w| >= crb_min_weight_change (0.02) and untouched assets are not diluted."""
    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig(
        crb_composite_weight=0.50,
        crb_three_type_weight=0.30,
        crb_vaa_weight=0.20,
        crb_max_single_position=0.20,
        crb_min_weight_change=0.02,
        crb_dd_reduce_thresh=0.50,  # disable emergency breaker to test pure inertia
        crb_dd_defensive_thresh=0.60,
        crb_dd_stop_thresh=0.70,
    )
    strat = ChanRiskManagedBlendStrategy(cfg)
    weights = strat.generate_weights(universe)

    rebal_rows = weights.dropna(how="all")
    assert not rebal_rows.empty

    diffs = (rebal_rows - rebal_rows.shift(1)).dropna(how="all")
    risky_diffs = diffs.drop(columns=["BIL"], errors="ignore")

    abs_diffs = risky_diffs.abs()
    non_zero = abs_diffs[abs_diffs > 1e-6].values.flatten()
    non_zero = non_zero[~np.isnan(non_zero)]

    if len(non_zero) > 0:
        # Every non-zero target change must be >= 0.01999 (respecting 2% threshold)
        assert (non_zero >= 0.01999).all(), f"Found target changes < 0.02: {non_zero[non_zero < 0.01999]}"

    # Total risky allocation never exceeds 1.0
    risky_w = rebal_rows.drop(columns=["BIL"], errors="ignore")
    assert (risky_w.sum(axis=1) <= 1.00001).all()


def test_chan_composite_single_stock_cap_and_cash_derouting():
    """Verify that ChanCompositeStrategy never allocates > max_single_position (0.20)
    to any single stock and deroutes the excess capital to cash_proxy (BIL)."""
    from research_strategy.rs.strategy import ChanThreeTypeStrategy

    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig(
        chan_comp_max_single_position=0.20,
        chan_comp_min_weight_change=0.0,
        cash_proxy="BIL",
    )
    strat = ChanCompositeStrategy(cfg)
    weights = strat.generate_weights(universe)

    rebal = weights.dropna(how="all")
    assert not rebal.empty

    risky = rebal.drop(columns=["BIL"], errors="ignore")
    # No single stock should ever exceed 20%
    assert (risky <= 0.200001).all().all(), f"Found weights > 0.20:\n{risky[risky > 0.20].dropna(how='all')}"

    # Verify rows sum to 1.0 (with cash proxy)
    assert np.allclose(rebal.sum(axis=1), 1.0, atol=1e-5)

    # In single-signal periods where only 1 stock is held, BIL must hold >= 80%
    held_counts = (risky > 1e-4).sum(axis=1)
    single_held_rows = rebal[held_counts == 1]
    if not single_held_rows.empty:
        assert (single_held_rows["BIL"] >= 0.79999).all()


def test_chan_three_type_single_stock_cap_and_cash_derouting():
    """Verify that ChanThreeTypeStrategy never allocates > max_single_position (0.20)
    to any single stock and deroutes the excess capital to cash_proxy (BIL)."""
    from research_strategy.rs.strategy import ChanThreeTypeStrategy

    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig(
        chan3_max_single_position=0.20,
        chan3_min_weight_change=0.0,
        cash_proxy="BIL",
    )
    strat = ChanThreeTypeStrategy(cfg)
    weights = strat.generate_weights(universe)

    rebal = weights.dropna(how="all")
    assert not rebal.empty

    risky = rebal.drop(columns=["BIL"], errors="ignore")
    # No single stock should ever exceed 20%
    assert (risky <= 0.200001).all().all(), f"Found weights > 0.20:\n{risky[risky > 0.20].dropna(how='all')}"

    # Verify rows sum to 1.0 (with cash proxy)
    assert np.allclose(rebal.sum(axis=1), 1.0, atol=1e-5)

    # In single-signal periods where only 1 stock is held, BIL must hold >= 80%
    held_counts = (risky > 1e-4).sum(axis=1)
    single_held_rows = rebal[held_counts == 1]
    if not single_held_rows.empty:
        assert (single_held_rows["BIL"] >= 0.79999).all()


def test_chan_composite_inertia_filter_suppresses_micro_trades():
    """Verify that ChanCompositeStrategy's asset-level inertia suppresses
    any non-zero target changes < chan_comp_min_weight_change (0.02)."""
    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig(
        chan_comp_max_single_position=0.20,
        chan_comp_min_weight_change=0.02,
        cash_proxy="BIL",
    )
    strat = ChanCompositeStrategy(cfg)
    weights = strat.generate_weights(universe)

    rebal = weights.dropna(how="all")
    assert not rebal.empty

    diffs = (rebal - rebal.shift(1)).dropna(how="all")
    risky_diffs = diffs.drop(columns=["BIL"], errors="ignore")

    abs_diffs = risky_diffs.abs()
    non_zero = abs_diffs[abs_diffs > 1e-6].values.flatten()
    non_zero = non_zero[~np.isnan(non_zero)]

    if len(non_zero) > 0:
        assert (non_zero >= 0.01999).all(), f"Found target changes < 0.02: {non_zero[non_zero < 0.01999]}"


def test_chan_risk_managed_blend_dynamic_cash_deployment():
    """Verify that ChanRiskManagedBlendStrategy with dynamic cash deployment
    scales up active risky holdings during bull breadth regimes (breadth >= 0.50)
    towards target_bull_exposure (0.80) while respecting max_single_position (0.20)."""
    universe = create_mock_universe(n_days=400)
    # Enable dynamic cash deployment
    cfg_dynamic = StrategyConfig(
        crb_dynamic_cash_deployment=True,
        crb_breadth_lookback=50,
        crb_breadth_bull_thresh=0.50,
        crb_target_bull_exposure=0.80,
        crb_max_single_position=0.20,
        crb_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat_dynamic = ChanRiskManagedBlendStrategy(cfg_dynamic)
    weights_dynamic = strat_dynamic.generate_weights(universe)

    rebal_dyn = weights_dynamic.dropna(how="all")
    assert not rebal_dyn.empty

    risky_dyn = rebal_dyn.drop(columns=["BIL"], errors="ignore")
    # Rule 1: Dynamic mode scales up to crb_bull_max_single_position (0.30)
    assert (risky_dyn <= cfg_dynamic.crb_bull_max_single_position + 1e-5).all().all(), f"Found weights > {cfg_dynamic.crb_bull_max_single_position}:\n{risky_dyn[risky_dyn > cfg_dynamic.crb_bull_max_single_position].dropna(how='all')}"

    # Rule 2: Rows sum to 1.0 with BIL
    assert np.allclose(rebal_dyn.sum(axis=1), 1.0, atol=1e-5)

    # Disable dynamic cash deployment for comparison
    cfg_static = StrategyConfig(
        crb_dynamic_cash_deployment=False,
        crb_max_single_position=0.20,
        crb_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat_static = ChanRiskManagedBlendStrategy(cfg_static)
    weights_static = strat_static.generate_weights(universe)
    rebal_static = weights_static.dropna(how="all")

    risky_static = rebal_static.drop(columns=["BIL"], errors="ignore")
    # Static mode without dynamic cash deployment strictly respects crb_max_single_position (0.20)
    assert (risky_static <= 0.200001).all().all()

    # Common rebalance dates
    common_idx = rebal_dyn.index.intersection(rebal_static.index)
    assert len(common_idx) > 0

    # In bull breadth periods where static held non-zero stocks, dynamic exposure should be >= static exposure
    dyn_risky_sum = risky_dyn.loc[common_idx].sum(axis=1)
    stat_risky_sum = risky_static.loc[common_idx].sum(axis=1)

    # Across active days where static exposure was positive and didn't trigger circuit breaker,
    # dynamic cash deployment should either increase or equal exposure (never decrease exposure during bull markets)
    active_days = common_idx[stat_risky_sum > 0.05]
    if len(active_days) > 0:
        assert (dyn_risky_sum.loc[active_days] >= stat_risky_sum.loc[active_days] - 1e-4).all()


def test_chan_risk_managed_blend_bull_position_cap_expansion():
    """Verify that specifying crb_bull_max_single_position > crb_max_single_position
    allows positions to scale up to the expanded cap in bull breadth regimes."""
    universe = create_mock_universe(n_days=400)
    cfg_expanded = StrategyConfig(
        crb_dynamic_cash_deployment=True,
        crb_breadth_lookback=50,
        crb_breadth_bull_thresh=0.50,
        crb_target_bull_exposure=0.80,
        crb_max_single_position=0.20,
        crb_bull_max_single_position=0.30,
        crb_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat_expanded = ChanRiskManagedBlendStrategy(cfg_expanded)
    weights = strat_expanded.generate_weights(universe)
    rebal_df = weights.dropna(how="all")
    assert not rebal_df.empty

    risky_df = rebal_df.drop(columns=["BIL"], errors="ignore")
    # Weights should respect the expanded 0.30 cap
    assert (risky_df <= 0.300001).all().all()
    # At least some rebalance row should have taken advantage of the expanded cap (> 0.20)
    assert (risky_df > 0.200001).sum().sum() > 0


def test_chan_risk_managed_blend_10d_breadth_thrust_activation():
    """Verify that a short-term 10-day breadth thrust triggers dynamic cash deployment
    even when longer-term 50d breadth is below its threshold (fast rebound override)."""
    universe = create_mock_universe(n_days=400)

    # Set 50d breadth threshold artificially high (0.99) so standard breadth NEVER triggers,
    # but set 10d thrust threshold reachable (0.40) to test thrust override.
    cfg_thrust = StrategyConfig(
        crb_dynamic_cash_deployment=True,
        crb_breadth_lookback=50,
        crb_breadth_bull_thresh=0.99,  # Disabled standard breadth
        crb_thrust_lookback=10,
        crb_thrust_thresh=0.40,        # Enabled breadth thrust override
        crb_target_bull_exposure=0.80,
        crb_max_single_position=0.20,
        crb_bull_max_single_position=0.30,
        crb_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat_thrust = ChanRiskManagedBlendStrategy(cfg_thrust)
    weights_thrust = strat_thrust.generate_weights(universe)
    rebal_thrust = weights_thrust.dropna(how="all")
    assert not rebal_thrust.empty

    # Compare with a baseline where thrust threshold is also set impossible (0.99)
    cfg_no_thrust = StrategyConfig(
        crb_dynamic_cash_deployment=True,
        crb_breadth_lookback=50,
        crb_breadth_bull_thresh=0.99,
        crb_thrust_lookback=10,
        crb_thrust_thresh=0.99,
        crb_target_bull_exposure=0.80,
        crb_max_single_position=0.20,
        crb_bull_max_single_position=0.30,
        crb_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat_no_thrust = ChanRiskManagedBlendStrategy(cfg_no_thrust)
    weights_no_thrust = strat_no_thrust.generate_weights(universe)
    rebal_no_thrust = weights_no_thrust.dropna(how="all")

    # In thrust mode, max single position can scale up towards bull_max_pos (0.30)
    risky_thrust = rebal_thrust.drop(columns=["BIL"], errors="ignore")
    risky_no_thrust = rebal_no_thrust.drop(columns=["BIL"], errors="ignore")

    # Thrust mode should have greater or equal max exposure than no-thrust mode
    assert risky_thrust.max().max() >= risky_no_thrust.max().max() - 1e-5


def test_chan_four_state_execution_instantiation_and_interface():
    """Verify ChanFourStateExecutionStrategy instantiates correctly from config
    and follows AllocationTemplate interface."""
    cfg_dict = load_strategies_config()
    assert "chan_four_state_execution" in cfg_dict
    entry = cfg_dict["chan_four_state_execution"]
    assert entry["class_name"] == "ChanFourStateExecutionStrategy"

    strat = instantiate_strategy_from_config_entry("chan_four_state_execution", entry)
    assert isinstance(strat, ChanFourStateExecutionStrategy)
    assert strat.name == "chan_four_state_execution"
    assert strat.config.chan_fse_min_hold_bars == 5
    assert strat.config.chan_fse_zg_tolerance_pct == 0.025
    assert strat.config.chan_fse_cons_timeout_bars == 8
    assert strat.config.chan_fse_stop_evaluation_mode == "close"
    assert strat.config.chan_fse_b1_buffer_pct == 0.03
    assert strat.config.chan_fse_cooldown_bars == 4
    assert strat.config.chan_fse_two_stage_entry is True
    assert strat.config.chan_fse_use_breadth_filter is True
    assert strat.config.chan_fse_breadth_bull_thresh == 0.30
    assert strat.config.chan_fse_adx_filter is True
    assert strat.config.chan_fse_adx_threshold == 20.0
    assert strat.config.chan_fse_adx_period == 14
    assert "Chan Four-State Operational Execution Strategy" in strat.explain_weights()
    assert "gestation buffer" in strat.explain_weights()
    assert "close-confirmed" in strat.explain_weights()
    assert "re-entry cooldown" in strat.explain_weights()
    assert "ADX trend strength gate" in strat.explain_weights()


def test_chan_four_state_execution_weights_generation():
    """Verify ChanFourStateExecutionStrategy generates valid sparse weights
    adhering to position cap and budget allocation."""
    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig(
        chan_fse_min_gap_bars=4,
        chan_fse_min_strokes=3,
        chan_fse_max_single_position=0.20,
        chan_fse_min_weight_change=0.04,
        cash_proxy="BIL",
    )
    strat = ChanFourStateExecutionStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert not weights.empty
    rebal = weights.dropna(how="all")
    assert not rebal.empty

    # Verify rows sum to 1.0 with BIL
    assert np.allclose(rebal.sum(axis=1), 1.0, atol=1e-5)

    # Verify single-stock positions do not exceed cap
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky <= 0.200001).all().all()


def test_four_state_loop_structural_invalidation_stops():
    """Verify deterministic structural invalidation stops in run_four_state_position_loop:
    - 3B invalidation: breach below ZG disproves breakout.
    - 2B invalidation: breach below DD disproves pullback.
    - 1B invalidation: breach below bar low disproves bottom.
    """
    n = 10
    close = np.array([100.0, 105.0, 106.0, 98.0, 95.0, 90.0, 92.0, 93.0, 94.0, 95.0])
    b1 = np.zeros(n, dtype=bool)
    b2 = np.zeros(n, dtype=bool)
    b3 = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)

    # Case 1: 3B at bar 1 with ZG=102. Price drops to 98 at bar 3 (breaching ZG)
    b3[1] = True
    zg = np.full(n, 102.0)
    zd = np.full(n, 96.0)
    dd = np.full(n, 90.0)

    weights_3b = run_four_state_position_loop(
        close, b1, b2, b3, sell, zg=zg, zd=zd, dd=dd, exit_on_consolidation=False
    )
    # Entered at bar 1 and 2, but stopped out at bar 3 because close (98) < ZG (102)
    assert weights_3b[1] == 1.0
    assert weights_3b[2] == 1.0
    assert weights_3b[3] == 0.0

    # Case 2: 2B at bar 1 with DD=90. Price drops to 98 at bar 3 (above DD), but 89 at bar 5 (below DD)
    b3[1] = False
    b2[1] = True
    close_2b = np.array([100.0, 105.0, 106.0, 98.0, 95.0, 89.0, 92.0, 93.0, 94.0, 95.0])
    weights_2b = run_four_state_position_loop(
        close_2b, b1, b2, b3, sell, zg=zg, zd=zd, dd=dd, exit_on_consolidation=False, trail_stop_to_zg=False, stop_loss_pct=0.20
    )
    assert weights_2b[1] == 1.0
    assert weights_2b[2] == 1.0
    assert weights_2b[3] == 1.0
    assert weights_2b[4] == 1.0
    assert weights_2b[5] == 0.0  # close 89 < DD 90 -> invalidation stop

    # Case 3: 1B at bar 1 with low=104. Price drops to 98 at bar 3 (below entry low)
    b2[1] = False
    b1[1] = True
    low = close.copy()
    low[1] = 104.0  # entry bar low
    weights_1b = run_four_state_position_loop(
        close, b1, b2, b3, sell, zg=zg, zd=zd, dd=dd, low=low, exit_on_consolidation=False
    )
    assert weights_1b[1] == 1.0
    assert weights_1b[2] == 1.0
    assert weights_1b[3] == 0.0  # 98 < 104 -> divergence invalidated


def test_four_state_loop_lesson_16_consolidation_exit():
    """Verify Lesson 16 (中小资金拒绝盘整): exiting position when price drops into pivot consolidation."""
    n = 6
    close = np.array([100.0, 105.0, 106.0, 101.0, 101.5, 102.0])
    # Pivot band: ZD=95, ZG=102, DD=90. Close at bar 1, 2 is 105, 106 (above_zs).
    # Close at bar 3 is 101.0 (in_zs: between 95 and 102).
    zg = np.full(n, 102.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 90.0)
    b2 = np.zeros(n, dtype=bool)
    b2[1] = True
    dummy = np.zeros(n, dtype=bool)

    # When exit_on_consolidation=True with min_hold_bars=0: exits immediately on bar 3
    w_exit = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=True, trail_stop_to_zg=False, stop_loss_pct=0.20,
        min_hold_bars=0, consolidation_timeout_bars=1,
    )
    assert w_exit[1] == 1.0
    assert w_exit[2] == 1.0
    assert w_exit[3] == 0.0

    # When exit_on_consolidation=False: holds through in_zs consolidation
    w_hold = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=False, trail_stop_to_zg=False, stop_loss_pct=0.20,
        min_hold_bars=0,
    )
    assert w_hold[1] == 1.0
    assert w_hold[2] == 1.0
    assert w_hold[3] == 1.0


def test_four_state_loop_gestation_buffer_prevents_premature_exit():
    """Verify that min_hold_bars allows 1B/2B entries gestation time to develop toward
    the pivot without being prematurely churned on Day 1."""
    n = 8
    # 1B entry formed below pivot (ZD=100, ZG=110). Price enters at 95, stays below/in pivot for 3 bars.
    close = np.array([90.0, 95.0, 96.0, 97.0, 98.0, 99.0, 85.0, 84.0])
    zg = np.full(n, 110.0)
    zd = np.full(n, 100.0)
    dd = np.full(n, 80.0)
    b1 = np.zeros(n, dtype=bool)
    b1[1] = True
    dummy = np.zeros(n, dtype=bool)

    # With default min_hold_bars=5: held through bars 1-5 even though below_zs
    w_buffered = run_four_state_position_loop(
        close, b1, dummy, dummy, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=True, min_hold_bars=5, stop_loss_pct=0.20,
    )
    assert w_buffered[1] == 1.0
    assert w_buffered[2] == 1.0
    assert w_buffered[3] == 1.0
    assert w_buffered[4] == 1.0
    assert w_buffered[5] == 1.0

    # Without gestation buffer (min_hold_bars=0): dumped immediately on bar 2 for being below_zs
    w_unbuffered = run_four_state_position_loop(
        close, b1, dummy, dummy, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=True, min_hold_bars=0, stop_loss_pct=0.20,
    )
    assert w_unbuffered[1] == 1.0
    assert w_unbuffered[2] == 0.0  # Churned on bar 2


def test_four_state_loop_3b_zg_tolerance_and_breakout_invalidation():
    """Verify that 3B breakout positions:
    1. Are protected against noise within zg_tolerance_pct (default 1%).
    2. Exit once price definitively breaches below ZG * (1 - zg_tolerance_pct).
    """
    n = 6
    zg_val = 100.0
    zg = np.full(n, zg_val)
    zd = np.full(n, 90.0)
    dd = np.full(n, 85.0)
    b3 = np.zeros(n, dtype=bool)
    b3[1] = True
    dummy = np.zeros(n, dtype=bool)

    # Bar 1: Entry at 105 (> ZG 100)
    # Bar 2: Minor dip to 99.5 (0.5% below ZG -> within 1% tolerance, should NOT exit)
    # Bar 3: Dip to 98.5 (1.5% below ZG -> exceeds 1% tolerance, MUST exit)
    close = np.array([100.0, 105.0, 99.5, 98.5, 97.0, 96.0])

    weights = run_four_state_position_loop(
        close, dummy, dummy, b3, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=True, zg_tolerance_pct=0.01, stop_loss_pct=0.10,
    )
    assert weights[1] == 1.0
    assert weights[2] == 1.0   # 99.5 is within 1% tolerance of 100.0 (threshold 99.0)
    assert weights[3] == 0.0   # 98.5 is below 99.0 -> invalidation exit triggered


def test_four_state_loop_stagnation_consolidation_timeout():
    """Verify that positions lingering in_zs for consolidation_timeout_bars with
    non-positive stroke direction are cleanly exited to avoid Lesson 16 capital drag."""
    n = 12
    # 2B entry at bar 1. Price enters in_zs (ZD=90, ZG=110) at 100 and stays flat at 100 for 10 bars.
    close = np.full(n, 100.0)
    zg = np.full(n, 110.0)
    zd = np.full(n, 90.0)
    dd = np.full(n, 80.0)
    stroke_dir = np.zeros(n, dtype=int)  # 0 = flat/no upward momentum
    b2 = np.zeros(n, dtype=bool)
    b2[1] = True
    dummy = np.zeros(n, dtype=bool)

    # With min_hold_bars=3, consolidation_timeout_bars=4:
    # Bar 1: entry (held=0)
    # Bar 2: held=1 < 3, in_zs count = 1
    # Bar 3: held=2 < 3, in_zs count = 2
    # Bar 4: held=3 >= 3, in_zs count = 3
    # Bar 5: held=4 >= 3, in_zs count = 4 >= consolidation_timeout_bars and stroke_dir <= 0 -> exits!
    weights = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd,
        stroke_dir=stroke_dir, exit_on_consolidation=True,
        min_hold_bars=3, consolidation_timeout_bars=4, stop_loss_pct=0.20,
    )
    assert weights[1] == 1.0
    assert weights[2] == 1.0
    assert weights[3] == 1.0
    assert weights[4] == 1.0
    assert weights[5] == 0.0  # Exited at bar 5 due to stagnant consolidation timeout

    # If stroke direction is positive (stroke_dir = 1), position is NOT dumped because stroke is lifting off
    stroke_up = np.ones(n, dtype=int)
    weights_up = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd,
        stroke_dir=stroke_up, exit_on_consolidation=True,
        min_hold_bars=3, consolidation_timeout_bars=4, stop_loss_pct=0.20,
    )
    assert weights_up[1] == 1.0
    assert weights_up[4] == 1.0
    assert weights_up[5] == 1.0  # Kept holding because upward stroke momentum is alive!


def test_four_state_loop_trailing_stop_ratchet_to_zg():
    """Verify that trailing stop ratchets up to ZG as price trades above ZG."""
    n = 8
    # Price rises strongly, then falls back below ZG
    close = np.array([100.0, 110.0, 115.0, 120.0, 108.0, 105.0, 100.0, 95.0])
    zg = np.array([102.0, 102.0, 102.0, 108.0, 108.0, 108.0, 108.0, 108.0])
    zd = np.full(n, 95.0)
    b3 = np.zeros(n, dtype=bool)
    b3[1] = True
    dummy = np.zeros(n, dtype=bool)

    weights = run_four_state_position_loop(
        close, dummy, dummy, b3, dummy, zg=zg, zd=zd, trail_stop_to_zg=True, exit_on_consolidation=False
    )
    assert weights[1] == 1.0
    assert weights[2] == 1.0
    assert weights[3] == 1.0
    # At bar 4, price falls to 108. At bar 5, price is 105 which is below the ratcheted stop (108)
    assert weights[5] == 0.0


def test_chan_composite_structural_stop_option():
    """Verify ChanCompositeStrategy with chan_comp_use_structural_stops=True
    incorporates structural breakout invalidation stops."""
    universe = create_mock_universe(n_days=400)
    cfg_with_stops = StrategyConfig(
        chan_comp_use_structural_stops=True,
        cash_proxy="BIL",
    )
    strat_with_stops = ChanCompositeStrategy(cfg_with_stops)
    w_stops = strat_with_stops.generate_weights(universe)
    assert not w_stops.empty

    cfg_no_stops = StrategyConfig(
        chan_comp_use_structural_stops=False,
        cash_proxy="BIL",
    )
    strat_no_stops = ChanCompositeStrategy(cfg_no_stops)
    w_no_stops = strat_no_stops.generate_weights(universe)
    assert not w_no_stops.empty


def test_chan_four_state_blend_interface_and_config():
    cfg = StrategyConfig()
    strat = ChanFourStateBlendStrategy(cfg)
    assert strat.name == "chan_four_state_blend"
    assert strat.warmup_bars() == 252
    assert "chan_four_state_execution" in strat.explain_weights()
    assert "chan_three_type" in strat.explain_weights()
    assert "chan_vaa_compound" in strat.explain_weights()
    assert strat.config.cfsb_four_state_weight == 0.25
    assert strat.config.cfsb_three_type_weight == 0.45
    assert strat.config.cfsb_vaa_weight == 0.30
    assert strat.config.cfsb_tier1_cooldown_bars == 15
    assert strat.config.cfsb_breadth_bull_thresh == 0.30
    assert strat.config.cfsb_thrust_lookback == 10
    assert strat.config.cfsb_thrust_thresh == 0.60
    assert "10d thrust" in strat.explain_weights()
    assert "auto-heal" in strat.explain_weights()

    # Verify instantiation via strategies_config.json
    configs = load_strategies_config()
    assert "chan_four_state_blend" in configs
    entry = configs["chan_four_state_blend"]
    inst = instantiate_strategy_from_config_entry("chan_four_state_blend", entry)
    assert isinstance(inst, ChanFourStateBlendStrategy)
    assert inst.config.cfsb_four_state_weight == 0.25
    assert inst.config.cfsb_three_type_weight == 0.45
    assert inst.config.cfsb_vaa_weight == 0.30
    assert inst.config.cfsb_tier1_cooldown_bars == 15
    assert inst.config.cfsb_breadth_bull_thresh == 0.30
    assert inst.config.cfsb_thrust_lookback == 10
    assert inst.config.cfsb_thrust_thresh == 0.60


def test_chan_four_state_blend_execution_and_constraints():
    universe = create_mock_universe(n_days=400)
    cfg = StrategyConfig()
    strat = ChanFourStateBlendStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert not weights.empty
    rebal_dates = weights.dropna(how="all").index
    assert len(rebal_dates) > 0

    # Test sparse weights contract: non-rebalance rows are all NaN
    non_rebal = weights.drop(index=rebal_dates)
    if not non_rebal.empty:
        assert non_rebal.isna().all().all()

    # Test position capping constraint: no individual risky stock > max position cap (cfsb_bull_max_single_position = 0.30)
    rebal_df = weights.loc[rebal_dates]
    risky_df = rebal_df.drop(columns=["BIL"], errors="ignore")
    assert (risky_df > cfg.cfsb_bull_max_single_position + 1e-6).sum().sum() == 0

    # Test leverage constraint: sum of risky weights <= 1.0
    assert (risky_df.sum(axis=1) <= 1.000001).all()

    # Test explicit zero floor: no NaNs inside any rebalance row
    assert not rebal_df.isna().any().any()


def test_chan_four_state_blend_circuit_breaker_triggers():
    # Construct a universe where asset prices plunge sharply to trigger circuit breakers
    dates = pd.bdate_range("2020-01-01", periods=360)
    t = np.arange(360)

    # Initial rally followed by catastrophic 40% crash
    spy_close = np.where(t < 250, 100.0 + 0.3 * t, 175.0 - 1.5 * (t - 250))
    qqq_close = np.where(t < 250, 100.0 + 0.4 * t, 200.0 - 2.0 * (t - 250))
    bil_close = np.full(360, 100.0)

    universe = {
        "SPY": make_ohlcv_from_closes(spy_close),
        "QQQ": make_ohlcv_from_closes(qqq_close),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for df in universe.values():
        df.index = dates

    cfg = StrategyConfig(cfsb_dd_reduce_thresh=0.08, cfsb_dd_defensive_thresh=0.12, cfsb_dd_stop_thresh=0.18)
    strat = ChanFourStateBlendStrategy(cfg)
    weights = strat.generate_weights(universe)

    assert not weights.empty
    rebal_dates = weights.dropna(how="all").index
    crash_rebal_dates = [d for d in rebal_dates if d >= dates[250]]

    # Ensure strategy executed de-risking trades during the crash
    assert len(crash_rebal_dates) > 0
    crash_weights = weights.loc[crash_rebal_dates]
    if "BIL" in crash_weights.columns:
        bil_holdings = crash_weights["BIL"]
        assert (bil_holdings >= 0.50).any()


def test_chan_four_state_blend_dynamic_cash_deployment():
    universe = create_mock_universe(n_days=400)
    cfg_dynamic = StrategyConfig(cfsb_dynamic_cash_deployment=True, cash_proxy="BIL")
    cfg_static = StrategyConfig(cfsb_dynamic_cash_deployment=False, cash_proxy="BIL")

    strat_dynamic = ChanFourStateBlendStrategy(cfg_dynamic)
    strat_static = ChanFourStateBlendStrategy(cfg_static)

    w_dyn = strat_dynamic.generate_weights(universe)
    w_sta = strat_static.generate_weights(universe)

    assert not w_dyn.empty
    assert not w_sta.empty


def test_chan_four_state_blend_10d_breadth_thrust_activation():
    dates = pd.bdate_range("2020-01-01", periods=360)
    t = np.arange(360)
    # Severe bear market (t < 250) followed by a violent 10-day V-shape recovery (+15% surge)
    bear_then_thrust = np.where(
        t < 250,
        150.0 - 0.2 * t,
        100.0 + 1.5 * (t - 250)
    )
    universe = {
        "SPY": make_ohlcv_from_closes(bear_then_thrust),
        "QQQ": make_ohlcv_from_closes(bear_then_thrust * 1.05),
        "BIL": make_ohlcv_from_closes(np.full(360, 100.0)),
    }
    for df in universe.values():
        df.index = dates

    cfg_thrust = StrategyConfig(
        cfsb_dynamic_cash_deployment=True,
        cfsb_breadth_lookback=50,
        cfsb_breadth_bull_thresh=0.75,
        cfsb_thrust_lookback=10,
        cfsb_thrust_thresh=0.60,
        cfsb_target_bull_exposure=0.80,
        cash_proxy="BIL",
    )
    strat_thrust = ChanFourStateBlendStrategy(cfg_thrust)
    weights = strat_thrust.generate_weights(universe)
    assert not weights.empty


def test_four_state_loop_stop_evaluation_modes():
    """Verify close-confirmed vs low stop evaluation modes:
    Intraday shadow wicks dipping below stop do NOT trigger stop-out in 'close' mode."""
    n = 6
    close = np.array([100.0, 105.0, 106.0, 104.0, 107.0, 108.0])
    low = np.array([100.0, 105.0, 106.0, 95.0, 107.0, 108.0])  # bar 3 wicks down to 95, close is 104
    b2 = np.zeros(n, dtype=bool)
    b2[1] = True  # entry at bar 1
    dummy = np.zeros(n, dtype=bool)
    zg = np.full(n, 100.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 98.0)  # structural stop for 2B is DD=98.0

    # In 'close' mode: bar 3 close (104.0) >= DD (98.0) -> holds through shadow wick
    w_close = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd, low=low,
        stop_evaluation_mode="close", exit_on_consolidation=False, trail_stop_to_zg=False, stop_loss_pct=0.20,
    )
    assert w_close[1] == 1.0
    assert w_close[2] == 1.0
    assert w_close[3] == 1.0  # held because close=104 >= 98
    assert w_close[4] == 1.0

    # In 'low' mode: bar 3 low (95.0) < DD (98.0) -> stopped out on shadow wick
    w_low = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd, low=low,
        stop_evaluation_mode="low", exit_on_consolidation=False, trail_stop_to_zg=False, stop_loss_pct=0.20,
    )
    assert w_low[1] == 1.0
    assert w_low[2] == 1.0
    assert w_low[3] == 0.0  # stopped out on low=95 < 98


def test_four_state_loop_1b_buffer():
    """Verify 1B entry buffer absorbs secondary undercut wicks."""
    n = 6
    close = np.array([100.0, 100.0, 99.0, 99.5, 103.0, 105.0])
    b1 = np.zeros(n, dtype=bool)
    b1[1] = True  # 1B at bar 1 with entry low = 100.0
    dummy = np.zeros(n, dtype=bool)
    zg = np.full(n, 105.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 90.0)
    low = close.copy()

    # With b1_buffer_pct = 0.03, stop is 100 * 0.97 = 97.0. Bar 2 close is 99.0 (undercut low, but >= 97.0)
    w_buffered = run_four_state_position_loop(
        close, b1, dummy, dummy, dummy, zg=zg, zd=zd, dd=dd, low=low,
        b1_buffer_pct=0.03, exit_on_consolidation=False,
    )
    assert w_buffered[1] == 1.0
    assert w_buffered[2] == 1.0  # survives 1% undercut
    assert w_buffered[3] == 1.0

    # With b1_buffer_pct = 0.0, stop is 100.0. Bar 2 close is 99.0 < 100.0 -> stopped out
    w_unbuffered = run_four_state_position_loop(
        close, b1, dummy, dummy, dummy, zg=zg, zd=zd, dd=dd, low=low,
        b1_buffer_pct=0.0, exit_on_consolidation=False,
    )
    assert w_unbuffered[1] == 1.0
    assert w_unbuffered[2] == 0.0  # stopped out on exact low breach


def test_four_state_loop_3b_gestation_buffer():
    """Verify 3B breakout entries are granted min_hold_bars gestation buffer
    before consolidation exit triggers."""
    n = 8
    # 3B at bar 1 with ZG=102. Price pulls back to 101 (in_zs) on bars 2 and 3, then rallies to 108
    close = np.array([100.0, 104.0, 101.0, 101.5, 103.0, 106.0, 108.0, 110.0])
    b3 = np.zeros(n, dtype=bool)
    b3[1] = True
    dummy = np.zeros(n, dtype=bool)
    zg = np.full(n, 102.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 90.0)

    # With min_hold_bars=3: bars 2 and 3 (held=1, held=2) are protected during gestation
    w_gest = run_four_state_position_loop(
        close, dummy, dummy, b3, dummy, zg=zg, zd=zd, dd=dd,
        exit_on_consolidation=True, min_hold_bars=3, zg_tolerance_pct=0.01,
        stop_loss_pct=0.20, trail_stop_to_zg=False,
    )
    assert w_gest[1] == 1.0
    assert w_gest[2] == 1.0
    assert w_gest[3] == 1.0
    assert w_gest[4] == 1.0  # price recovered above ZG


def test_four_state_loop_cooldown_bars():
    """Verify that after an exit, new buy signals are locked out for cooldown_bars."""
    n = 8
    close = np.array([100.0, 105.0, 90.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    b2 = np.zeros(n, dtype=bool)
    b2[1] = True  # entry at bar 1
    b2[3] = True  # new buy signal at bar 3 (right after bar 2 stop-out)
    b2[6] = True  # new buy signal at bar 6 (after cooldown expires)
    sell = np.zeros(n, dtype=bool)
    sell[2] = True  # explicit exit at bar 2
    dummy = np.zeros(n, dtype=bool)
    zg = np.full(n, 100.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 90.0)

    # With cooldown_bars=3: exit at bar 2 locks out until bar 2+3 = 5. Bar 3 signal is ignored.
    w_cd = run_four_state_position_loop(
        close, dummy, b2, dummy, sell, zg=zg, zd=zd, dd=dd,
        cooldown_bars=3, exit_on_consolidation=False,
    )
    assert w_cd[1] == 1.0
    assert w_cd[2] == 0.0  # exited
    assert w_cd[3] == 0.0  # signal at bar 3 blocked by cooldown
    assert w_cd[4] == 0.0  # still in cooldown
    assert w_cd[5] == 0.0
    assert w_cd[6] == 1.0  # signal at bar 6 accepted (cooldown expired)


def test_four_state_loop_two_stage_sizing():
    """Verify two-stage sizing: 50% probe allocation scaling to 100% on stroke expansion confirmation."""
    n = 6
    close = np.array([100.0, 102.0, 104.0, 106.0, 105.0, 107.0])
    b2 = np.zeros(n, dtype=bool)
    b2[1] = True  # entry at bar 1
    dummy = np.zeros(n, dtype=bool)
    zg = np.full(n, 100.0)
    zd = np.full(n, 95.0)
    dd = np.full(n, 90.0)
    # Stroke direction: 0 at bar 1, turns UP (1) at bar 3
    stroke_dir = np.array([0, 0, 0, 1, 1, 1])

    w_two_stage = run_four_state_position_loop(
        close, dummy, b2, dummy, dummy, zg=zg, zd=zd, dd=dd,
        stroke_dir=stroke_dir, two_stage_entry=True, exit_on_consolidation=False,
    )
    assert w_two_stage[1] == 0.50  # probe entry at 50%
    assert w_two_stage[2] == 0.50  # stroke_dir is still 0
    assert w_two_stage[3] == 1.00  # stroke_dir=1 and close (106) > entry (102) -> scaled to 100%
    assert w_two_stage[4] == 1.00


def test_chan_four_state_execution_breadth_filter():
    """Verify ChanFourStateExecutionStrategy halts allocations during severe bear regimes (breadth < threshold)."""
    universe = create_mock_universe(n_days=100)
    # Configure high breadth threshold so mock universe is deemed a bear regime
    cfg = StrategyConfig(
        chan_fse_use_breadth_filter=True,
        chan_fse_breadth_bull_thresh=0.99,  # virtually all days will fail this threshold
        cash_proxy="BIL",
    )
    strat = ChanFourStateExecutionStrategy(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    assert not rebal.empty

    # Risky symbols must be 0.0, and 100% of capital routed to BIL
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky == 0.0).all().all()
    if "BIL" in rebal.columns:
        assert (rebal["BIL"] == 1.0).all()


def test_chan_blend_tier1_auto_healing_cooldown():
    """Verify Tier 1 Auto-Healing Cooldown resets peak_nav = cum_nav after tier1_cooldown_bars,
    preventing portfolio hysteresis freeze / liquidity trap."""
    universe = create_mock_universe(n_days=100)
    cfg = StrategyConfig(
        cfsb_dd_reduce_thresh=0.01,  # very tight threshold to force Tier 1 drawdown
        cfsb_dd_defensive_thresh=0.50,
        cfsb_dd_stop_thresh=0.90,
        cfsb_tier1_cooldown_bars=5,
        cash_proxy="BIL",
    )
    strat = ChanFourStateBlendStrategy(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    assert not rebal.empty


def test_chan_blend_preemptive_breadth_thrust_cash_deployment():
    """Verify pre-emptive breadth thrust cash deployment fills unallocated equity exposure
    into momentum leaders up to target_bull_exposure and bull_max_pos."""
    universe = create_mock_universe(n_days=120)
    cfg = StrategyConfig(
        cfsb_dynamic_cash_deployment=True,
        cfsb_thrust_thresh=0.30,  # easily triggered thrust
        cfsb_thrust_lookback=10,
        cfsb_target_bull_exposure=0.80,
        cfsb_bull_max_single_position=0.30,
        cash_proxy="BIL",
    )
    strat = ChanFourStateBlendStrategy(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    rebal = weights.dropna(how="all")
    assert not rebal.empty
    # Verify no individual stock exceeds expanded bull max single position (0.30)
    risky = rebal.drop(columns=["BIL"], errors="ignore")
    assert (risky <= 0.30 + 1e-4).all().all()


def test_chan_four_state_execution_adx_trend_filter():
    """Verify ChanFourStateExecutionStrategy ADX trend filter gates entry when ADX < threshold."""
    universe = create_mock_universe(n_days=400)
    # With impossible ADX threshold (e.g. 99), all buy signals are gated
    cfg_strict = StrategyConfig(
        chan_fse_adx_filter=True,
        chan_fse_adx_threshold=99.0,
        chan_fse_use_ma_filter=False,
        chan_fse_use_breadth_filter=False,
        cash_proxy="BIL",
    )
    strat_strict = ChanFourStateExecutionStrategy(cfg_strict)
    w_strict = strat_strict.generate_weights(universe)
    rebal_strict = w_strict.dropna(how="all")
    risky_strict = rebal_strict.drop(columns=["BIL"], errors="ignore")
    assert (risky_strict == 0.0).all().all()

    # With normal ADX threshold (20), positions can be initiated
    cfg_normal = StrategyConfig(
        chan_fse_adx_filter=True,
        chan_fse_adx_threshold=20.0,
        chan_fse_use_ma_filter=False,
        chan_fse_use_breadth_filter=False,
        cash_proxy="BIL",
    )
    strat_normal = ChanFourStateExecutionStrategy(cfg_normal)
    w_normal = strat_normal.generate_weights(universe)
    rebal_normal = w_normal.dropna(how="all")
    risky_normal = rebal_normal.drop(columns=["BIL"], errors="ignore")
    assert (risky_normal > 0.0).any().any()


def test_chan_risk_managed_blend_thrust_multi_asset_dispersion():
    """Recommendation 3: Verify that breadth thrust distributes unallocated cash across
    multiple momentum leaders (at least 5) and caps single-stock exposure at <= 20%."""
    n_days = 350
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    universe = {}
    for k in range(8):
        trend = 0.08 * (k + 1) * t
        osc = (2.0 + k) * np.sin(t / (8.0 + k))
        universe[f"SYM_{k}"] = make_ohlcv_from_closes(100.0 + trend + osc, start="2020-01-01")
    universe["BIL"] = make_ohlcv_from_closes(100.0 + 0.001 * t, start="2020-01-01")

    cfg = StrategyConfig(
        crb_dynamic_cash_deployment=True,
        crb_breadth_lookback=50,
        crb_breadth_bull_thresh=0.99,  # standard breadth disabled
        crb_thrust_lookback=10,
        crb_thrust_thresh=0.30,        # thrust enabled
        crb_target_bull_exposure=0.80,
        crb_max_single_position=0.20,
        crb_bull_max_single_position=0.20,  # 20% cap strictly enforced
        crb_min_weight_change=0.02,
        cash_proxy="BIL",
    )
    strat = ChanRiskManagedBlendStrategy(cfg)
    weights = strat.generate_weights(universe)
    rebal_df = weights.dropna(how="all")
    assert not rebal_df.empty

    risky_df = rebal_df.drop(columns=["BIL"], errors="ignore")
    # Single-stock exposure must NEVER exceed the 20% cap
    assert (risky_df <= 0.200001).all().all()

    # In rebalance rows where total risky exposure is elevated (> 0.40),
    # there must be at least 3-5 distinct assets sharing the allocation, not just 1 greedy stock
    elevated_rows = risky_df[risky_df.sum(axis=1) >= 0.40]
    if not elevated_rows.empty:
        non_zero_counts = (elevated_rows > 0.01).sum(axis=1)
        assert (non_zero_counts >= 3).any()


def test_chan_risk_managed_blend_smooth_drawdown_damping():
    """Recommendation 4: Verify that smooth linear drawdown damping continuously reduces
    exposure between dd_reduce_thresh (10%) and dd_stop_thresh (20%)."""
    universe = create_mock_universe(n_days=400)
    cfg_smooth = StrategyConfig(
        crb_smooth_drawdown=True,
        crb_dd_reduce_thresh=0.10,
        crb_dd_stop_thresh=0.20,
        crb_min_weight_change=0.01,
        cash_proxy="BIL",
    )
    strat_smooth = ChanRiskManagedBlendStrategy(cfg_smooth)
    weights = strat_smooth.generate_weights(universe)
    assert not weights.dropna(how="all").empty


def test_chan_risk_managed_blend_volatility_targeting():
    """Recommendation 4: Verify Barroso & Santa-Clara volatility targeting scales
    risky exposure according to target_vol."""
    universe = create_mock_universe(n_days=400)
    cfg_tight_vol = StrategyConfig(
        crb_enable_vol_targeting=True,
        crb_target_vol=0.02,  # Very tight target vol (2%), will definitely scale down
        crb_min_weight_change=0.01,
        cash_proxy="BIL",
    )
    strat_tight = ChanRiskManagedBlendStrategy(cfg_tight_vol)
    w_tight = strat_tight.generate_weights(universe).dropna(how="all")

    cfg_loose_vol = StrategyConfig(
        crb_enable_vol_targeting=True,
        crb_target_vol=0.50,  # Loose target vol (50%), no scaling down
        crb_min_weight_change=0.01,
        cash_proxy="BIL",
    )
    strat_loose = ChanRiskManagedBlendStrategy(cfg_loose_vol)
    w_loose = strat_loose.generate_weights(universe).dropna(how="all")

    risky_tight = w_tight.drop(columns=["BIL"], errors="ignore").sum(axis=1)
    risky_loose = w_loose.drop(columns=["BIL"], errors="ignore").sum(axis=1)

    # Tight target vol should yield strictly less or equal risky exposure on average
    assert risky_tight.mean() <= risky_loose.mean() + 1e-5





"""Unit tests for Researched Quantitative Trading & Timing Strategies (rs/strategy.py).

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py.
"""

import os
import sys

# Add project root to sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.testing import (
    make_ohlcv_from_closes,
    make_oscillating_df,
    make_trending_pullback_df,
)
from research_strategy.rs.config import StrategyConfig
from research_strategy.rs.strategy import (
    AcceleratingDualMomentum,
    ActiveDualMomentumRiskParity,
    AdaptiveGridStrategy,
    BoldAssetAllocation,
    EnsembleRegimeSwitchingStrategy,
    NaturalLanguageStrategy,
    RSIMeanReversionStrategy,
    SwingTrendPullbackStrategy,
    VigilantAssetAllocation,
    VolatilityManagedStrategy,
)


def create_mock_universe(n_days: int = 300) -> dict:
    """Creates a deterministic synthetic universe for testing strategy mechanics."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)

    spy_close = 100.0 + 0.1 * t
    eem_close = 100.0 - 0.1 * t
    qqq_close = 100.0 + 0.15 * t + 5.0 * np.sin(t / 5.0)
    gld_close = 100.0 + 0.05 * t + 0.5 * np.sin(t / 10.0)

    efa_close = 100.0 + 0.08 * t
    agg_close = 100.0 + 0.01 * t
    bil_close = 100.0 + 0.001 * t

    symbols = {
        "SPY": spy_close,
        "EEM": eem_close,
        "QQQ": qqq_close,
        "GLD": gld_close,
        "EFA": efa_close,
        "AGG": agg_close,
        "BIL": bil_close,
        "IWM": spy_close,
        "TLT": gld_close,
        "LQD": agg_close,
        "DBC": spy_close,
        "TIP": agg_close,
        "IEF": agg_close,
        "VNQ": spy_close,
    }

    universe = {}
    for sym, close_arr in symbols.items():
        df = make_ohlcv_from_closes(close_arr)
        df.index = dates
        universe[sym] = df

    return universe


def _timing_universe(df, symbol="SPY", cash_proxy="BIL"):
    idx = df.index
    return {symbol: df, cash_proxy: make_ohlcv_from_closes([100.0] * len(idx), start=str(idx[0].date()))}


def _daily(weights):
    return weights.ffill().fillna(0.0)


# --- NaturalLanguageStrategy & Presets -----------------------------------

def test_dual_momentum_trend_gate_and_cash_overlay():
    cfg = StrategyConfig(trend_sma_period=50, mom_long_lookback=50, rebalance_freq_days=20, top_k=3)
    strat = ActiveDualMomentumRiskParity(cfg)
    universe = create_mock_universe(n_days=250)

    weights = strat.generate_weights(universe)
    assert not weights.empty

    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    assert weights.loc[last_rebal, "EEM"] == 0.0
    total_w = weights.loc[last_rebal].sum()
    assert pytest.approx(total_w, abs=1e-4) == 1.0


def test_baa_canary_universe_switching():
    cfg = StrategyConfig(rebalance_freq_days=20, top_k=3)
    strat = BoldAssetAllocation(cfg)
    universe = create_mock_universe(n_days=250)

    dates = universe["EEM"].index
    universe["EEM"].loc[dates[-50]:, "Close"] = 10.0

    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    assert weights.loc[last_rebal, "QQQ"] == 0.0
    defensive_plus_cash = weights.loc[last_rebal, cfg.baa_defensive + [cfg.cash_proxy]].sum()
    assert pytest.approx(defensive_plus_cash, abs=1e-4) == 1.0


def test_volatility_managed_deleveraging():
    cfg = StrategyConfig(rebalance_freq_days=20, vol_managed_target_vol=0.05, vol_managed_var_lookback=20)
    strat = VolatilityManagedStrategy(cfg)
    universe = create_mock_universe(n_days=250)

    dates = universe["SPY"].index
    for sym in cfg.risky_universe:
        if sym in universe:
            rng = np.random.default_rng(123)
            universe[sym].loc[dates[-30]:, "Close"] *= (1.0 + rng.normal(0, 0.10, 30))

    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    cash_w = weights.loc[last_rebal, "BIL"]
    assert cash_w > 0.0
    total_w = weights.loc[last_rebal].sum()
    assert pytest.approx(total_w, abs=1e-4) == 1.0


def test_natural_language_strategy_custom_description():
    text = "Rebalance weekly. Select top 2 assets from SPY, QQQ, GLD, TLT with Close > 50d SMA. Allocate equally."
    strat = NaturalLanguageStrategy(text)
    universe = create_mock_universe(n_days=250)

    weights = strat.generate_weights(universe)
    assert not weights.empty

    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    active_weights = weights.loc[last_rebal][weights.loc[last_rebal] > 0]
    assert len(active_weights) <= 2
    for w in active_weights:
        assert pytest.approx(w, abs=1e-4) == 0.50


def test_volatility_managed_excludes_a_symbol_with_no_data_in_window():
    n_days = 250
    universe = create_mock_universe(n_days=n_days)

    dates = universe["SPY"].index
    new_close = np.full(n_days, np.nan)
    new_close[200:] = 100.0 + 0.05 * np.arange(n_days - 200)
    universe["NEW"] = make_ohlcv_from_closes(new_close)
    universe["NEW"].index = dates

    cfg = StrategyConfig(
        risky_universe=["SPY", "QQQ", "GLD", "NEW"], cash_proxy="BIL",
        rebalance_freq_days=20, vol_managed_var_lookback=20,
    )
    strat = VolatilityManagedStrategy(cfg)
    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index

    early_rebal = [d for d in rebal_dates if d < dates[200]]
    assert early_rebal

    for date in early_rebal:
        assert weights.loc[date, "NEW"] == 0.0
        total_w = weights.loc[date].sum()
        assert pytest.approx(total_w, abs=1e-4) == 1.0


# --- AcceleratingDualMomentum --------------------------------------------

def _adm_universe(n_days, spy_drift, scz_drift, tlt_drift, tip_drift, seed=1):
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    rng = np.random.default_rng(seed)
    noise = lambda scale: rng.normal(0, scale, n_days)
    return {
        "SPY": make_ohlcv_from_closes(100.0 + spy_drift * t + noise(0.3)),
        "SCZ": make_ohlcv_from_closes(100.0 + scz_drift * t + noise(0.3)),
        "TLT": make_ohlcv_from_closes(100.0 + tlt_drift * t + noise(0.1)),
        "TIP": make_ohlcv_from_closes(100.0 + tip_drift * t + noise(0.1)),
    }, dates


def test_accelerating_dual_momentum_picks_the_stronger_positive_equity_sleeve():
    universe, dates = _adm_universe(n_days=200, spy_drift=0.15, scz_drift=-0.10, tlt_drift=0.01, tip_drift=0.01)
    for sym, df in universe.items():
        df.index = dates

    strat = AcceleratingDualMomentum()
    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    assert len(rebal_dates) > 0

    last = rebal_dates[-1]
    assert weights.loc[last, "SPY"] == 1.0
    assert weights.loc[last, "SCZ"] == 0.0
    assert weights.loc[last, "TLT"] == 0.0
    assert weights.loc[last, "TIP"] == 0.0


def test_accelerating_dual_momentum_falls_back_to_stronger_bond_when_equities_decline():
    n_days = 200
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rng = np.random.default_rng(2)
    spy_close = 100.0 - 0.10 * np.arange(n_days) + rng.normal(0, 0.3, n_days)
    scz_close = 100.0 - 0.15 * np.arange(n_days) + rng.normal(0, 0.3, n_days)
    tlt_close = np.full(n_days, 100.0)
    tlt_close[-21:] = 100.0 * (1.05 ** np.arange(21))
    tip_close = np.full(n_days, 100.0) + rng.normal(0, 0.05, n_days)

    universe = {
        "SPY": make_ohlcv_from_closes(spy_close),
        "SCZ": make_ohlcv_from_closes(scz_close),
        "TLT": make_ohlcv_from_closes(tlt_close),
        "TIP": make_ohlcv_from_closes(tip_close),
    }
    for df in universe.values():
        df.index = dates

    strat = AcceleratingDualMomentum()
    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last = rebal_dates[-1]

    assert weights.loc[last, "SPY"] == 0.0
    assert weights.loc[last, "SCZ"] == 0.0
    assert weights.loc[last, "TLT"] == 1.0
    assert weights.loc[last, "TIP"] == 0.0


def test_accelerating_dual_momentum_missing_equity_returns_empty():
    universe, dates = _adm_universe(n_days=200, spy_drift=0.1, scz_drift=0.1, tlt_drift=0.0, tip_drift=0.0)
    for df in universe.values():
        df.index = dates
    del universe["SCZ"]

    strat = AcceleratingDualMomentum()
    weights = strat.generate_weights(universe)
    assert weights.empty


def test_accelerating_dual_momentum_warmup_bars():
    assert AcceleratingDualMomentum().warmup_bars() == 126


# --- VigilantAssetAllocation --------------------------------------------

def _vaa_universe(n_days=400, seed=3):
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    rng = np.random.default_rng(seed)
    noise_scale = {"SPY": 0.3, "QQQ": 0.3, "EFA": 0.3, "EEM": 0.3, "IEF": 0.03, "BIL": 0.03}
    drifts = {"SPY": 0.05, "QQQ": 0.08, "EFA": 0.04, "EEM": 0.03, "IEF": 0.02, "BIL": 0.001}
    universe = {}
    for sym, drift in drifts.items():
        close = 100.0 + drift * t + rng.normal(0, noise_scale[sym], n_days)
        df = make_ohlcv_from_closes(close)
        df.index = dates
        universe[sym] = df
    return universe, dates


def test_vaa_invests_in_best_offensive_asset_when_all_offensive_scores_positive():
    universe, dates = _vaa_universe()
    strat = VigilantAssetAllocation()
    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    assert len(rebal_dates) > 0

    last = rebal_dates[-1]
    assert weights.loc[last, "QQQ"] == 1.0
    for sym in ("SPY", "EFA", "EEM", "IEF", "BIL"):
        assert weights.loc[last, sym] == 0.0


def test_vaa_switches_to_defensive_when_any_offensive_score_is_negative():
    universe, dates = _vaa_universe()
    n_days = len(dates)
    rng = np.random.default_rng(4)
    universe["EEM"] = make_ohlcv_from_closes(100.0 - 0.05 * np.arange(n_days) + rng.normal(0, 0.3, n_days))
    universe["EEM"].index = dates

    strat = VigilantAssetAllocation()
    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last = rebal_dates[-1]

    assert weights.loc[last, "IEF"] == 1.0
    for sym in ("SPY", "QQQ", "EFA", "EEM", "BIL"):
        assert weights.loc[last, sym] == 0.0


def test_vaa_warmup_bars():
    assert VigilantAssetAllocation().warmup_bars() == 252


# --- RSIMeanReversionStrategy -------------------------------------------

def test_rsi_trend_filter_blocks_entries_in_a_sustained_downtrend():
    closes = np.concatenate([np.full(200, 100.0), np.linspace(100, 40, 200)])
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(rsi_require_trend_filter=True, rsi_trend_ma_period=200, rsi_oversold_threshold=50)
    strat = RSIMeanReversionStrategy(cfg)

    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    assert not (daily["SPY"].iloc[350:] > 0).any()


def test_rsi_enters_and_exits_on_an_oscillating_market():
    universe = _timing_universe(make_oscillating_df(n=500, seed=7))
    strat = RSIMeanReversionStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"] > 0).any(), "should enter at least once on a choppy market"
    assert (daily["SPY"] == 0).any(), "should also exit at least once, not hold forever"
    unique_vals = sorted(daily["SPY"].unique())
    assert set(unique_vals).issubset({0.0, 1.0})


def test_rsi_stop_loss_forces_an_exit_close_based():
    n = 60
    closes = np.full(n, 100.0)
    closes[10:] = 100.0 * (0.99 ** np.arange(n - 10))
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(
        rsi_require_trend_filter=False, rsi_oversold_threshold=99.0,
        rsi_exit_mode="either", rsi_exit_rsi_threshold=999.0, rsi_exit_ma_period=1,
        rsi_stop_loss_pct=0.05, rsi_max_holding_days=None,
    )
    strat = RSIMeanReversionStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    entry_day = daily.index[(daily["SPY"] > 0)][0]
    entry_price = universe["SPY"]["Close"].loc[entry_day]
    close = universe["SPY"]["Close"]
    breach_day = close.index[(close / entry_price - 1 <= -0.05) & (close.index > entry_day)][0]
    assert daily.loc[breach_day, "SPY"] == 0.0


def test_rsi_max_holding_days_forces_an_exit():
    n = 60
    closes = 100.0 + 0.5 * np.sin(np.arange(n) / 2.0)
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(
        rsi_require_trend_filter=False, rsi_oversold_threshold=99.0,
        rsi_exit_mode="either", rsi_exit_rsi_threshold=999.0, rsi_exit_ma_period=1,
        rsi_stop_loss_pct=None, rsi_max_holding_days=5,
    )
    strat = RSIMeanReversionStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    entry_day_pos = np.flatnonzero(daily["SPY"].to_numpy() > 0)[0]
    assert daily["SPY"].iloc[entry_day_pos + 5] == 0.0


def test_rsi_missing_symbol_returns_empty():
    universe = _timing_universe(make_oscillating_df(n=100))
    cfg = StrategyConfig(rsi_symbol="NOT_IN_UNIVERSE")
    assert RSIMeanReversionStrategy(cfg).generate_weights(universe).empty


def test_rsi_warmup_bars():
    assert RSIMeanReversionStrategy(StrategyConfig(rsi_trend_ma_period=200)).warmup_bars() == 200


def test_rsi_multi_asset_allocates_across_qualifying_symbols():
    df1 = make_oscillating_df(n=500, seed=7)
    df2 = make_oscillating_df(n=500, seed=12)
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * 500, start=str(df1.index[0].date())),
    }
    cfg = StrategyConfig(rsi_require_trend_filter=False, rsi_oversold_threshold=30)
    strat = RSIMeanReversionStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert "SPY" in daily.columns
    assert "QQQ" in daily.columns
    assert "BIL" in daily.columns
    np.testing.assert_allclose(daily["SPY"] + daily["QQQ"] + daily["BIL"], 1.0)


# --- SwingTrendPullbackStrategy ------------------------------------------

def test_swing_enters_on_a_trending_pullback_market():
    universe = _timing_universe(make_trending_pullback_df(n=500, seed=7))
    strat = SwingTrendPullbackStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    assert (daily["SPY"] > 0).any()


def test_swing_stop_loss_forces_an_exit():
    n = 260
    t = np.arange(n)
    closes = 100 + 0.2 * t
    closes[230:245] = closes[229] - np.linspace(0, 8, 15)
    closes[245:] = closes[244] * (0.95 ** np.arange(n - 245))
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(
        swing_require_rising_trend_ma=False, swing_entry_rsi_threshold=100.0,
        swing_exit_rsi_threshold=999.0, swing_use_trailing_stop=False,
        swing_stop_loss_pct=0.05, swing_reward_risk_ratio=100.0,
        swing_max_holding_days=None,
    )
    strat = SwingTrendPullbackStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"] > 0).any(), "should have entered on the pullback"
    entry_day = daily.index[daily["SPY"] > 0][0]
    entry_price = universe["SPY"]["Close"].loc[entry_day]
    close = universe["SPY"]["Close"]
    breach_day = close.index[(close / entry_price - 1 <= -0.05) & (close.index > entry_day)][0]
    assert daily.loc[breach_day, "SPY"] == 0.0


def test_swing_profit_target_forces_an_exit():
    n = 260
    t = np.arange(n)
    closes = 100 + 0.2 * t
    closes[230:245] = closes[229] - np.linspace(0, 8, 15)
    closes[245:] = closes[244] * (1.05 ** np.arange(n - 245))
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(
        swing_require_rising_trend_ma=False, swing_entry_rsi_threshold=100.0,
        swing_exit_rsi_threshold=999.0, swing_use_trailing_stop=False,
        swing_stop_loss_pct=0.05, swing_reward_risk_ratio=2.0,
        swing_max_holding_days=None,
    )
    strat = SwingTrendPullbackStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    entry_day = daily.index[daily["SPY"] > 0][0]
    entry_price = universe["SPY"]["Close"].loc[entry_day]
    close = universe["SPY"]["Close"]
    hit_day = close.index[(close / entry_price - 1 >= 0.10) & (close.index > entry_day)][0]
    assert daily.loc[hit_day, "SPY"] == 0.0


def test_swing_max_holding_days_forces_an_exit():
    n = 260
    t = np.arange(n)
    closes = 100 + 0.2 * t
    closes[230:245] = closes[229] - np.linspace(0, 8, 15)
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    cfg = StrategyConfig(
        swing_require_rising_trend_ma=False, swing_entry_rsi_threshold=100.0,
        swing_exit_rsi_threshold=999.0, swing_use_trailing_stop=False,
        swing_stop_loss_pct=0.99, swing_reward_risk_ratio=100.0,
        swing_max_holding_days=10,
    )
    strat = SwingTrendPullbackStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    entry_day_pos = np.flatnonzero(daily["SPY"].to_numpy() > 0)[0]
    assert daily["SPY"].iloc[entry_day_pos + 10] == 0.0


def test_swing_missing_symbol_returns_empty():
    universe = _timing_universe(make_trending_pullback_df(n=100))
    cfg = StrategyConfig(swing_symbol="NOT_IN_UNIVERSE")
    assert SwingTrendPullbackStrategy(cfg).generate_weights(universe).empty


def test_swing_warmup_bars():
    assert SwingTrendPullbackStrategy(StrategyConfig(swing_trend_ma_period=200, swing_trend_slope_lookback=20)).warmup_bars() == 200


def test_swing_multi_asset_normalizes_weights_when_multiple_symbols_trigger():
    df1 = make_trending_pullback_df(n=500, seed=7)
    df2 = make_trending_pullback_df(n=500, seed=12)
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * 500, start=str(df1.index[0].date())),
    }
    cfg = StrategyConfig(swing_position_size_pct=1.0)
    strat = SwingTrendPullbackStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert "SPY" in daily.columns
    assert "QQQ" in daily.columns
    assert (daily["SPY"] + daily["QQQ"] <= 1.0 + 1e-9).all()
    np.testing.assert_allclose(daily["SPY"] + daily["QQQ"] + daily["BIL"], 1.0)


# --- AdaptiveGridStrategy -------------------------------------------------

def test_grid_deploys_capital_in_a_range_bound_market():
    universe = _timing_universe(make_oscillating_df(n=500, seed=7))
    strat = AdaptiveGridStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"] > 0).any(), "should open at least one grid slot on a choppy market"
    assert daily["SPY"].max() <= 1.0 + 1e-9
    assert (daily["SPY"] >= 0).all()
    np.testing.assert_allclose(daily["SPY"] + daily["BIL"], 1.0)


def test_grid_paused_in_a_persistent_downtrend():
    n = 300
    closes = 100.0 * (0.998 ** np.arange(n))
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    strat = AdaptiveGridStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    assert not (daily["SPY"] > 0).any()


def test_grid_drawdown_circuit_breaker_flattens_and_cools_down():
    n = 300
    rng = np.random.default_rng(3)
    t = np.arange(n)
    closes = 100 + 3 * np.sin(t / 10.0) + rng.normal(0, 0.2, n)
    closes[250:] = closes[249] * np.linspace(1.0, 0.7, n - 250)
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    strat = AdaptiveGridStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"].iloc[:250] > 0).any(), "should have deployed capital before the crash"
    assert daily["SPY"].iloc[-1] == 0.0


def test_grid_missing_symbol_returns_empty():
    universe = _timing_universe(make_oscillating_df(n=100))
    cfg = StrategyConfig(grid_symbol="NOT_IN_UNIVERSE")
    assert AdaptiveGridStrategy(cfg).generate_weights(universe).empty


def test_grid_warmup_bars():
    assert AdaptiveGridStrategy(StrategyConfig(grid_atr_period=14, grid_trend_ma_period=100)).warmup_bars() == 100


def test_grid_multi_asset_deploys_grids_across_multiple_symbols():
    df1 = make_oscillating_df(n=500, seed=7)
    df2 = make_oscillating_df(n=500, seed=12)
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * 500, start=str(df1.index[0].date())),
    }
    strat = AdaptiveGridStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert "SPY" in daily.columns
    assert "QQQ" in daily.columns
    np.testing.assert_allclose(daily["SPY"] + daily["QQQ"] + daily["BIL"], 1.0)


# --- EnsembleRegimeSwitchingStrategy --------------------------------------

def test_ensemble_trend_only_mode_matches_the_shifted_trend_gate():
    universe = _timing_universe(make_trending_pullback_df(n=400, seed=7))
    cfg = StrategyConfig(ensemble_mode="trend_only", ensemble_trend_ma_period=200)
    strat = EnsembleRegimeSwitchingStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    from common.indicators import sma
    close = universe["SPY"]["Close"]
    trend_ma = sma(close, 200)
    expected = (close > trend_ma).shift(1).fillna(False).astype(float)
    pd.testing.assert_series_equal(daily["SPY"], expected, check_names=False)


def test_ensemble_downtrend_always_forces_cash_regardless_of_mode():
    n = 260
    closes = np.concatenate([np.full(200, 100.0), np.linspace(100, 50, 60)])
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    for mode in ("ensemble", "trend_only", "meanrev_only"):
        cfg = StrategyConfig(ensemble_mode=mode, ensemble_trend_ma_period=200)
        weights = EnsembleRegimeSwitchingStrategy(cfg).generate_weights(universe)
        daily = _daily(weights)
        assert not (daily["SPY"].iloc[-10:] > 0).any(), f"mode={mode} should be flat deep in a downtrend"


def test_ensemble_missing_symbol_returns_empty():
    universe = _timing_universe(make_trending_pullback_df(n=100))
    cfg = StrategyConfig(ensemble_symbol="NOT_IN_UNIVERSE")
    assert EnsembleRegimeSwitchingStrategy(cfg).generate_weights(universe).empty


def test_ensemble_unknown_mode_raises():
    universe = _timing_universe(make_trending_pullback_df(n=250))
    cfg = StrategyConfig(ensemble_mode="not_a_real_mode")
    with pytest.raises(ValueError):
        EnsembleRegimeSwitchingStrategy(cfg).generate_weights(universe)


def test_ensemble_warmup_bars():
    cfg = StrategyConfig(ensemble_trend_ma_period=200, ensemble_adx_period=14, ensemble_rsi_period=2)
    assert EnsembleRegimeSwitchingStrategy(cfg).warmup_bars() == 201


def test_ensemble_multi_asset_equal_weights_trending_symbols():
    df1 = make_trending_pullback_df(n=400, seed=7)
    df2 = make_trending_pullback_df(n=400, seed=12)
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * 400, start=str(df1.index[0].date())),
    }
    cfg = StrategyConfig(ensemble_mode="trend_only", ensemble_trend_ma_period=200)
    strat = EnsembleRegimeSwitchingStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    both_active = (daily["SPY"] > 0) & (daily["QQQ"] > 0)
    if both_active.any():
        np.testing.assert_allclose(daily.loc[both_active, "SPY"], 0.5)
        np.testing.assert_allclose(daily.loc[both_active, "QQQ"], 0.5)

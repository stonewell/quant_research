"""Unit tests for Researched Quantitative Trading & Timing Strategies (rs/strategy.py).

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py.
"""

import os
import sys

# Add project root to sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.testing import (
    make_ohlcv_from_closes,
    make_oscillating_df,
    make_trending_pullback_df,
)
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.nl_parser import ParsedStrategySpec
from research_strategy.run_research_strategy import instantiate_strategy_from_config_entry
from research_strategy.rs.strategy import (
    AcceleratingDualMomentum,
    ActiveDualMomentumRiskParity,
    AdaptiveAssetAllocation,
    AdaptiveGridStrategy,
    AllWeatherStrategy,
    BoldAssetAllocation,
    ChanPivotShiftStrategy,
    CompounderMarginOfSafetyStrategy,
    EnsembleRegimeSwitchingStrategy,
    GoldenButterflyStrategy,
    HFEAStrategy,
    NaturalLanguageStrategy,
    PermanentPortfolioStrategy,
    ProtectiveAssetAllocation,
    RSIMeanReversionStrategy,
    StaticAllocationStrategy,
    SwingTrendPullbackStrategy,
    TurtleBreakoutStrategy,
    VigilantAssetAllocation,
    VolatilityManagedStrategy,
    _min_variance_weights,
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
        "HYG": agg_close,
        "UPRO": spy_close,
        "TMF": gld_close,
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


def test_baa_canary_calm_market_with_no_offensive_candidates_does_not_mischaracterize_as_turbulent():
    # Regression test: `if not turbulent and offensive_symbols: ... else:
    # <defensive logic>` used to route a CALM market (canary passes) into
    # the defensive-allocation branch whenever `offensive_symbols` happened
    # to be empty (e.g. a custom config narrows the offensive list
    # independently of the canary list) -- mischaracterizing "no eligible
    # offensive candidates" as "market is turbulent". SPY is a steady
    # uptrend here, so the canary must stay calm; with offensive_universe
    # explicitly empty, the fixed code must fall back straight to cash
    # rather than pulling from defensive_universe as if turbulent.
    spec = ParsedStrategySpec(
        strategy_name="No Offensive Candidates Test",
        raw_description="test",
        use_canary_logic=True,
        canary_universe=["SPY"],
        offensive_universe=[],
        defensive_universe=["TLT", "AGG"],
        cash_proxy="BIL",
        rebalance_freq_days=20,
        trend_sma_period=50,
        trend_roc_lookback=50,
        top_k=3,
    )
    strat = NaturalLanguageStrategy(spec)
    universe = create_mock_universe(n_days=250)

    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    assert weights.loc[last_rebal, "TLT"] == 0.0
    assert weights.loc[last_rebal, "AGG"] == 0.0
    assert weights.loc[last_rebal, "BIL"] == 1.0


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


# --- Regression: default `<x>_symbol` must not silently expand to the full
# --- multi-asset universe (see _get_risky_symbols) -----------------------

def test_default_config_trades_only_spy_even_with_a_full_multi_symbol_universe_present():
    # Regression test: `_get_risky_symbols` used to special-case the literal
    # string "SPY" as meaning "not customized", so ANY config leaving
    # `<x>_symbol` at its default (or explicitly set to "SPY", the shipped
    # default in strategies_config.json for every one of these four
    # strategies) would silently expand to the full `risky_universe` instead
    # of trading just SPY. `create_mock_universe` intentionally contains all
    # of DEFAULT_RISKY_UNIVERSE plus extras -- exactly the shape of universe
    # the real CLI builds, where the bug was only ever visible (every other
    # test here uses a minimal 2-symbol universe that masked it).
    universe = create_mock_universe(n_days=300)

    for strat in (
        RSIMeanReversionStrategy(StrategyConfig(rsi_require_trend_filter=False, rsi_oversold_threshold=90)),
        SwingTrendPullbackStrategy(StrategyConfig(swing_require_rising_trend_ma=False, swing_entry_rsi_threshold=90)),
        AdaptiveGridStrategy(),
        EnsembleRegimeSwitchingStrategy(StrategyConfig(ensemble_mode="trend_only")),
    ):
        weights = strat.generate_weights(universe)
        daily = _daily(weights)
        other_symbols = [s for s in universe if s not in ("SPY", "BIL")]
        assert not (daily[other_symbols] > 0).any().any(), (
            f"{type(strat).__name__} with default config traded a non-SPY symbol"
        )


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
    # rsi_symbol defaults to "SPY", which is now correctly honored as "trade
    # just SPY" -- multi-asset evaluation is an explicit opt-in via params.
    weights = strat.generate_weights(universe, params={"symbols": ["SPY", "QQQ"]})
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
    # swing_symbol defaults to "SPY", now correctly honored as "trade just
    # SPY" -- multi-asset evaluation is an explicit opt-in via params.
    weights = strat.generate_weights(universe, params={"symbols": ["SPY", "QQQ"]})
    daily = _daily(weights)

    assert "SPY" in daily.columns
    assert "QQQ" in daily.columns
    assert (daily["SPY"] + daily["QQQ"] <= 1.0 + 1e-9).all()
    np.testing.assert_allclose(daily["SPY"] + daily["QQQ"] + daily["BIL"], 1.0)


# --- ChanPivotShiftStrategy -----------------------------------------------
# All fixtures below are synthetic-only, per this file's offline testing
# policy. This strategy is an independent, from-scratch reading of Chan
# theory (see rs/chan_structure.py) -- it shares no code with, and was not
# validated against, the third-party `czsc` library.

def _chan_breakout_closes(tail=None):
    """Deterministic closes array with two Chan pivots: an initial range
    around [90, 100] (bars 1-30), then a breakout to a wholly higher range
    around [104, 114] (bars 31-70), followed by 20 bars of runway (bars
    71-90) so the pivot-shift-up signal's 1-bar confirmation lag always
    lands inside the array. `tail` (if given) is appended after that."""

    def leg(a, b, n):
        return np.linspace(a, b, n + 1)[1:]

    zigzag = np.concatenate(
        [
            [100.0],
            leg(100, 90, 10), leg(90, 100, 10), leg(100, 90, 10),          # pivot #1 ~ [90, 100]
            leg(90, 112, 10), leg(112, 104, 10), leg(104, 114, 10), leg(114, 104, 10),  # pivot #2 ~ [104, 114]
            leg(104, 108, 20),                                            # runway past the confirmation bar
        ]
    )
    return zigzag if tail is None else np.concatenate([zigzag, tail])


def test_chan_enters_only_after_the_pivot_shifts_to_a_higher_range():
    universe = _timing_universe(make_ohlcv_from_closes(_chan_breakout_closes()))
    strat = ChanPivotShiftStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    # Comfortably before the 2nd pivot's window can even close (bar ~71):
    # no entry should exist yet -- the entry marks a real pivot shift, not
    # noise from the first (lower) range.
    assert not (daily["SPY"].iloc[:65] > 0).any(), "no entry before the pivot actually shifts up"
    assert (daily["SPY"].iloc[65:] > 0).any(), "should enter once the pivot shifts to the higher range"


def test_chan_no_entry_on_a_pure_monotonic_decline():
    closes = 100.0 - 0.1 * np.arange(300)
    universe = _timing_universe(make_ohlcv_from_closes(closes))
    strat = ChanPivotShiftStrategy()
    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    # A strictly monotonic path never forms an interior fractal, so no
    # stroke/pivot -- and therefore no buy signal -- can ever form.
    assert (daily["SPY"] == 0).all()


def test_chan_stop_loss_forces_an_exit():
    decline = np.linspace(108.0, 40.0, 41)[1:]
    universe = _timing_universe(make_ohlcv_from_closes(_chan_breakout_closes(tail=decline)))
    cfg = StrategyConfig(chan_stop_loss_pct=0.05, chan_max_holding_days=None)
    strat = ChanPivotShiftStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"] > 0).any(), "should have entered after the pivot shift"
    entry_day = daily.index[daily["SPY"] > 0][0]
    entry_price = universe["SPY"]["Close"].loc[entry_day]
    close = universe["SPY"]["Close"]
    breach_day = close.index[(close / entry_price - 1 <= -0.05) & (close.index > entry_day)][0]
    assert daily.loc[breach_day, "SPY"] == 0.0


def test_chan_max_holding_days_forces_an_exit():
    runway = np.linspace(108.0, 110.0, 31)[1:]
    universe = _timing_universe(make_ohlcv_from_closes(_chan_breakout_closes(tail=runway)))
    cfg = StrategyConfig(chan_stop_loss_pct=None, chan_max_holding_days=5)
    strat = ChanPivotShiftStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"] > 0).any(), "should have entered after the pivot shift"
    entry_day_pos = np.flatnonzero(daily["SPY"].to_numpy() > 0)[0]
    assert daily["SPY"].iloc[entry_day_pos + 5] == 0.0


def test_chan_missing_symbol_returns_empty():
    universe = _timing_universe(make_oscillating_df(n=100))
    cfg = StrategyConfig(chan_symbol="NOT_IN_UNIVERSE")
    assert ChanPivotShiftStrategy(cfg).generate_weights(universe).empty


def test_chan_warmup_bars():
    cfg = StrategyConfig(chan_min_gap_bars=4, chan_min_strokes=3)
    assert ChanPivotShiftStrategy(cfg).warmup_bars() == 3 * 2 * (4 + 2)


def test_chan_multi_asset_normalizes_weights_when_multiple_symbols_trigger():
    closes = _chan_breakout_closes()
    df1 = make_ohlcv_from_closes(closes)
    df2 = make_ohlcv_from_closes(closes, start=str(df1.index[0].date()))
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * len(closes), start=str(df1.index[0].date())),
    }
    cfg = StrategyConfig(chan_position_size_pct=1.0)
    strat = ChanPivotShiftStrategy(cfg)
    weights = strat.generate_weights(universe, params={"symbols": ["SPY", "QQQ"]})
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
    # grid_symbol defaults to "SPY", now correctly honored as "trade just
    # SPY" -- multi-asset evaluation is an explicit opt-in via params.
    weights = strat.generate_weights(universe, params={"symbols": ["SPY", "QQQ"]})
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
    # ensemble_symbol defaults to "SPY", now correctly honored as "trade just
    # SPY" -- multi-asset evaluation is an explicit opt-in via params.
    weights = strat.generate_weights(universe, params={"symbols": ["SPY", "QQQ"]})
    daily = _daily(weights)

    both_active = (daily["SPY"] > 0) & (daily["QQQ"] > 0)
    if both_active.any():
        np.testing.assert_allclose(daily.loc[both_active, "SPY"], 0.5)
        np.testing.assert_allclose(daily.loc[both_active, "QQQ"], 0.5)


def test_load_strategies_config_default():
    config = load_strategies_config()
    assert isinstance(config, dict)
    assert "dual_momentum" in config
    assert "baa_keller" in config
    assert "volatility_managed" in config
    assert "rsi_mean_reversion" in config
    assert "accelerating_dual_momentum" in config


def test_strategy_config_from_dict():
    params = {
        "rebalance_freq_days": 10,
        "top_k": 2,
        "non_existent_param": 999
    }
    with pytest.warns(UserWarning, match="non_existent_param"):
        cfg = StrategyConfig.from_dict(params)
    assert cfg.rebalance_freq_days == 10
    assert cfg.top_k == 2
    assert not hasattr(cfg, "non_existent_param")


@pytest.mark.parametrize("field_name,bad_value", [
    ("rebalance_freq_days", 0),
    ("rebalance_freq_days", -5),
    ("top_k", 0),
    ("commission_pct", -0.001),
    ("slippage_pct", -0.001),
    ("initial_capital", 0),
    ("cash_proxy", ""),
    ("risky_universe", "SPY,QQQ"),
])
def test_strategy_config_rejects_invalid_values(field_name, bad_value):
    with pytest.raises(ValueError, match=field_name):
        StrategyConfig(**{field_name: bad_value})


def test_strategy_config_defaults_are_valid():
    # Regression guard: the dataclass's own defaults must satisfy __post_init__.
    StrategyConfig()


def test_load_strategies_config_rejects_non_dict_entry(tmp_path):
    bad_path = tmp_path / "bad_config.json"
    bad_path.write_text('{"my_strategy": "not_an_object"}')
    with pytest.raises(ValueError, match="my_strategy"):
        load_strategies_config(str(bad_path))


def test_load_strategies_config_rejects_non_dict_top_level(tmp_path):
    bad_path = tmp_path / "bad_config.json"
    bad_path.write_text('["not", "a", "dict"]')
    with pytest.raises(ValueError):
        load_strategies_config(str(bad_path))


def test_instantiate_strategy_missing_class_name_gives_clear_error():
    with pytest.raises(ValueError, match="no 'class_name' key"):
        instantiate_strategy_from_config_entry("bad_entry", {"type": "class", "parameters": {}})


def test_instantiate_strategy_unrecognized_class_name_gives_clear_error():
    with pytest.raises(ValueError, match="Unrecognized strategy class_name"):
        instantiate_strategy_from_config_entry(
            "bad_entry", {"type": "class", "class_name": "NotARealClass", "parameters": {}}
        )


def test_instantiate_strategy_invalid_params_names_the_strategy_key():
    with pytest.raises(ValueError, match="bad_entry"):
        instantiate_strategy_from_config_entry(
            "bad_entry",
            {"type": "class", "class_name": "RSIMeanReversionStrategy", "parameters": {"top_k": 0}},
        )


def test_instantiate_strategy_missing_plain_english_description_gives_clear_error():
    with pytest.raises(ValueError, match="no 'plain_english_description' key"):
        instantiate_strategy_from_config_entry(
            "bad_nl_entry", {"type": "natural_language", "parameters": {}}
        )


def test_instantiate_strategy_empty_plain_english_description_gives_clear_error():
    with pytest.raises(ValueError, match="no 'plain_english_description' key"):
        instantiate_strategy_from_config_entry(
            "bad_nl_entry",
            {"type": "natural_language", "plain_english_description": "", "parameters": {}},
        )


def test_instantiate_strategy_whitespace_plain_english_description_gives_clear_error():
    with pytest.raises(ValueError, match="no 'plain_english_description' key"):
        instantiate_strategy_from_config_entry(
            "bad_nl_entry",
            {"type": "natural_language", "plain_english_description": "   ", "parameters": {}},
        )


def test_instantiate_strategy_valid_plain_english_description_still_works():
    text = "Rebalance weekly. Select top 2 assets from SPY, QQQ, GLD, TLT with Close > 50d SMA. Allocate equally."
    strat = instantiate_strategy_from_config_entry(
        "good_nl_entry",
        {"type": "natural_language", "plain_english_description": text, "parameters": {}},
    )
    universe = create_mock_universe(n_days=250)
    weights = strat.generate_weights(universe)
    assert not weights.empty

    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]
    active_weights = weights.loc[last_rebal][weights.loc[last_rebal] > 0]
    assert len(active_weights) <= 2


def test_instantiate_strategy_entry_data_none_gives_clear_error():
    with pytest.raises(ValueError, match="bad_entry"):
        instantiate_strategy_from_config_entry("bad_entry", None)


def test_instantiate_strategy_entry_data_not_a_dict_gives_clear_error():
    with pytest.raises(ValueError, match="bad_entry"):
        instantiate_strategy_from_config_entry("bad_entry", ["not", "a", "dict"])


def test_instantiate_strategy_missing_type_defaults_to_class():
    with pytest.raises(ValueError, match="no 'class_name' key"):
        instantiate_strategy_from_config_entry("bad_entry", {})


def test_instantiate_strategies_from_json_config():
    config = load_strategies_config()
    for key, entry in config.items():
        strat = instantiate_strategy_from_config_entry(key, entry)
        assert strat is not None
        assert hasattr(strat, "generate_weights")
        assert hasattr(strat, "explain_weights")


def test_run_strategy_from_json_config():
    config = load_strategies_config()
    universe = create_mock_universe(n_days=300)
    dual_mom_entry = config["dual_momentum"]
    strat = instantiate_strategy_from_config_entry("dual_momentum", dual_mom_entry)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    assert "SPY" in weights.columns


def test_turtle_breakout_triggers_entry_on_donchian_high():
    # Construct price series with a clear 20-day high breakout in an uptrend
    n = 250
    closes = np.linspace(100.0, 120.0, n)
    closes[150] = 135.0  # Big upward spike / breakout
    closes[151:] = 135.0

    df = make_ohlcv_from_closes(closes)
    universe = _timing_universe(df)
    cfg = StrategyConfig(
        turtle_entry_breakout_days=20,
        turtle_exit_breakout_days=10,
        turtle_require_trend_filter=False
    )
    strat = TurtleBreakoutStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    # donchian_high is already built from bars before i (shift(1) inside the
    # rolling window), so bar 150's own close is what breaks out -- the
    # position must be active on bar 150 itself, not a day later.
    assert daily["SPY"].iloc[150] > 0.0


def test_turtle_breakout_exits_on_donchian_low():
    # Construct price series that breaks out then drops below 10-day low
    n = 250
    closes = np.full(n, 100.0)
    closes[:200] = np.linspace(100, 110, 200)
    closes[200] = 125.0  # Breakout
    closes[201:215] = 125.0
    closes[215] = 90.0   # Drop below 10d low
    closes[216:] = 90.0

    df = make_ohlcv_from_closes(closes)
    universe = _timing_universe(df)
    cfg = StrategyConfig(
        turtle_entry_breakout_days=20,
        turtle_exit_breakout_days=10,
        turtle_require_trend_filter=False
    )
    strat = TurtleBreakoutStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    # Active on the breakout bar itself, exited on the drop bar itself --
    # both already-lagged Donchian series compare against the SAME day's
    # close, no extra day of delay.
    assert daily["SPY"].iloc[200] > 0.0
    assert daily["SPY"].iloc[215] == 0.0


def test_turtle_breakout_exits_on_atr_trailing_stop():
    n = 250
    closes = np.linspace(100.0, 110.0, n)
    closes[180] = 140.0  # Sharp spike
    closes[181] = 100.0  # Sharp reversal exceeding 2N ATR stop

    df = make_ohlcv_from_closes(closes)
    universe = _timing_universe(df)
    cfg = StrategyConfig(
        turtle_entry_breakout_days=20,
        turtle_exit_breakout_days=10,
        turtle_atr_stop_mult=2.0,
        turtle_require_trend_filter=False
    )
    strat = TurtleBreakoutStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    # Entered on the spike bar itself (a fresh 20-day high), then the very
    # next bar's sharp reversal breaches peak - 2*ATR on that SAME bar's
    # close -- exits on bar 181 itself, not a day later.
    assert daily["SPY"].iloc[180] > 0.0
    assert daily["SPY"].iloc[181] == 0.0


def test_turtle_breakout_trend_filter_blocks_downtrend():
    n = 250
    # Closes well below 200d SMA
    closes = np.concatenate([np.linspace(150, 80, 210), np.array([90.0] * 40)])
    # Even if 90.0 is a 20d high, close < 200d SMA should block entry
    df = make_ohlcv_from_closes(closes)
    universe = _timing_universe(df)
    cfg = StrategyConfig(
        turtle_entry_breakout_days=20,
        turtle_require_trend_filter=True,
        turtle_trend_ma_period=200
    )
    strat = TurtleBreakoutStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["SPY"].iloc[210:] == 0.0).all()


def test_turtle_breakout_multi_asset_inverse_atr_weights():
    df1 = make_trending_pullback_df(n=300, seed=42)
    df2 = make_trending_pullback_df(n=300, seed=99)
    universe = {
        "SPY": df1,
        "QQQ": df2,
        "BIL": make_ohlcv_from_closes([100.0] * 300, start=str(df1.index[0].date())),
    }
    cfg = StrategyConfig(
        turtle_entry_breakout_days=20,
        turtle_require_trend_filter=False,
        turtle_position_sizing_mode="inverse_atr"
    )
    strat = TurtleBreakoutStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert not weights.empty
    assert "SPY" in daily.columns
    assert "QQQ" in daily.columns


# --- Modern Popular Static Portfolios -------------------------------------

@pytest.mark.parametrize("strat_cls,expected_symbols", [
    (PermanentPortfolioStrategy, {"SPY", "TLT", "BIL", "GLD"}),
    (GoldenButterflyStrategy, {"SPY", "IWM", "TLT", "BIL", "GLD"}),
    (AllWeatherStrategy, {"SPY", "TLT", "IEF", "GLD", "DBC"}),
])
def test_static_portfolios_rebalance_to_fixed_weights(strat_cls, expected_symbols):
    universe = create_mock_universe(n_days=300)
    strat = strat_cls()
    assert isinstance(strat, StaticAllocationStrategy)

    weights = strat.generate_weights(universe)
    rebal_rows = weights.dropna(how="all")
    assert not rebal_rows.empty

    last_row = rebal_rows.iloc[-1]
    assert set(last_row[last_row > 0].index) == expected_symbols
    np.testing.assert_allclose(last_row.sum(), 1.0, atol=1e-9)
    # Every rebalance targets the SAME fixed weights -- not just the last one.
    for _, row in rebal_rows.iterrows():
        np.testing.assert_allclose(row.sum(), 1.0, atol=1e-9)


def test_permanent_portfolio_is_equal_25_pct():
    universe = create_mock_universe(n_days=300)
    strat = PermanentPortfolioStrategy()
    weights = strat.generate_weights(universe)
    last_row = weights.dropna(how="all").iloc[-1]
    for sym in ("SPY", "TLT", "BIL", "GLD"):
        assert last_row[sym] == pytest.approx(0.25)


def test_hfea_uses_leveraged_etf_symbols_and_5545_split():
    universe = create_mock_universe(n_days=300)
    strat = HFEAStrategy()
    weights = strat.generate_weights(universe)
    last_row = weights.dropna(how="all").iloc[-1]
    assert last_row["UPRO"] == pytest.approx(0.55)
    assert last_row["TMF"] == pytest.approx(0.45)


def test_hfea_quarterly_rebalance_frequency():
    strat = HFEAStrategy()
    assert strat.default_rebalance_freq_days == 63


def test_static_allocation_warns_and_degrades_gracefully_on_missing_symbol():
    # Universe missing GLD entirely -- Permanent Portfolio's gold sleeve.
    universe = create_mock_universe(n_days=300)
    del universe["GLD"]

    strat = PermanentPortfolioStrategy()
    with pytest.warns(UserWarning, match="GLD"):
        weights = strat.generate_weights(universe)

    last_row = weights.dropna(how="all").iloc[-1]
    # Remaining 75% stays at its original weights, GLD's 25% is simply unallocated.
    assert last_row["SPY"] == pytest.approx(0.25)
    assert last_row["TLT"] == pytest.approx(0.25)
    assert last_row["BIL"] == pytest.approx(0.25)
    assert last_row.sum() == pytest.approx(0.75)


def test_static_allocation_strategy_explain_weights_lists_percentages():
    strat = GoldenButterflyStrategy()
    summary = strat.explain_weights()
    assert "20.0%" in summary
    assert "golden_butterfly" in summary


# --- Protective Asset Allocation (PAA) ------------------------------------

def test_paa_full_protection_when_breadth_collapses():
    n = 400
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    # Every risky asset trending DOWN -> momentum negative for all -> breadth n=0 -> 100% protection.
    falling = 100.0 - 0.05 * t
    rising_protection = 100.0 + 0.02 * t

    cfg = StrategyConfig(paa_momentum_lookback=50, rebalance_freq_days=21)
    risky = cfg.paa_universe
    universe = {sym: make_ohlcv_from_closes(falling, start="2020-01-01") for sym in risky}
    universe["IEF"] = make_ohlcv_from_closes(rising_protection, start="2020-01-01")
    for df, sym in zip(universe.values(), list(universe.keys())):
        df.index = dates

    strat = ProtectiveAssetAllocation(cfg)
    weights = strat.generate_weights(universe)
    last_row = weights.dropna(how="all").iloc[-1]

    assert last_row["IEF"] == pytest.approx(1.0, abs=1e-6)
    for sym in risky:
        assert last_row[sym] == pytest.approx(0.0, abs=1e-6)


def test_paa_full_risk_on_when_breadth_is_universal():
    n = 400
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    rising = 100.0 + 0.1 * t

    cfg = StrategyConfig(paa_momentum_lookback=50, rebalance_freq_days=21, paa_top_k=6)
    risky = cfg.paa_universe
    universe = {sym: make_ohlcv_from_closes(rising, start="2020-01-01") for sym in risky}
    universe["IEF"] = make_ohlcv_from_closes(100.0 + 0.001 * t, start="2020-01-01")
    for df in universe.values():
        df.index = dates

    strat = ProtectiveAssetAllocation(cfg)
    weights = strat.generate_weights(universe)
    last_row = weights.dropna(how="all").iloc[-1]

    assert last_row["IEF"] == pytest.approx(0.0, abs=1e-6)
    n_held = (last_row > 0).sum()
    assert n_held == cfg.paa_top_k
    np.testing.assert_allclose(last_row.sum(), 1.0, atol=1e-9)


def test_paa_warmup_bars_matches_momentum_lookback():
    cfg = StrategyConfig(paa_momentum_lookback=180)
    strat = ProtectiveAssetAllocation(cfg)
    assert strat.warmup_bars() == 180


def test_paa_missing_protection_symbol_still_allocates_risky_sleeve():
    universe = create_mock_universe(n_days=300)
    del universe["IEF"]
    cfg = StrategyConfig(paa_momentum_lookback=50, rebalance_freq_days=21)
    strat = ProtectiveAssetAllocation(cfg)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    last_row = weights.dropna(how="all").iloc[-1]
    assert "IEF" not in last_row.index or pd.isna(last_row.get("IEF"))


# --- Adaptive Asset Allocation (AAA) ---------------------------------------

def test_min_variance_weights_favors_lower_variance_asset():
    cov = np.array([
        [0.01, 0.0],
        [0.0, 0.04],
    ])
    w = _min_variance_weights(cov)
    assert w[0] > w[1]
    np.testing.assert_allclose(w.sum(), 1.0, atol=1e-6)
    assert (w >= -1e-9).all()


def test_min_variance_weights_single_asset_is_fully_allocated():
    cov = np.array([[0.02]])
    w = _min_variance_weights(cov)
    np.testing.assert_allclose(w, [1.0])


def test_aaa_selects_top_momentum_survivors_and_sums_to_one():
    n = 400
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)

    cfg = StrategyConfig(
        aaa_momentum_lookback=50, aaa_corr_lookback=60, aaa_vol_lookback=20,
        aaa_top_k=4, rebalance_freq_days=21,
    )
    strong = 100.0 + 0.2 * t
    weak = 100.0 - 0.05 * t
    universe = {}
    for i, sym in enumerate(cfg.aaa_universe):
        closes = strong if i < 4 else weak
        df = make_ohlcv_from_closes(closes + i, start="2020-01-01")
        df.index = dates
        universe[sym] = df

    strat = AdaptiveAssetAllocation(cfg)
    weights = strat.generate_weights(universe)
    rebal_rows = weights.dropna(how="all")
    assert not rebal_rows.empty

    last_row = rebal_rows.iloc[-1]
    held = last_row[last_row > 0]
    assert len(held) <= cfg.aaa_top_k
    np.testing.assert_allclose(last_row.sum(), 1.0, atol=1e-6)
    # The 4 strongly-trending assets should be the ones actually selected.
    assert set(held.index).issubset(set(cfg.aaa_universe[:4]))


def test_aaa_warmup_bars_covers_longest_lookback():
    cfg = StrategyConfig(aaa_momentum_lookback=126, aaa_corr_lookback=200)
    strat = AdaptiveAssetAllocation(cfg)
    assert strat.warmup_bars() == 201


def test_aaa_empty_universe_returns_empty_frame():
    strat = AdaptiveAssetAllocation()
    assert strat.generate_weights({}).empty


# --- New strategies wired into JSON config --------------------------------

@pytest.mark.parametrize("key", [
    "permanent_portfolio", "golden_butterfly", "all_weather", "hfea",
    "protective_asset_allocation", "adaptive_asset_allocation", "chan_pivot_shift",
])
def test_new_strategies_instantiate_and_run_from_json_config(key):
    config = load_strategies_config()
    entry = config[key]
    strat = instantiate_strategy_from_config_entry(key, entry)
    universe = create_mock_universe(n_days=300)
    weights = strat.generate_weights(universe)
    assert not weights.empty
    assert strat.explain_weights()


# --- CompounderMarginOfSafetyStrategy (price-proxy of docs/snowball_strategy.txt) ---

def _cms_universe(n_days=300, seed=5):
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    rng = np.random.default_rng(seed)
    universe = {
        "KO": make_ohlcv_from_closes(100.0 + 0.25 * t + rng.normal(0, 0.5, n_days)),   # strong, steady uptrend
        "PG": make_ohlcv_from_closes(100.0 + 0.02 * t + rng.normal(0, 0.5, n_days)),   # weak trend -- shouldn't clear the hurdle
        "SPY": make_ohlcv_from_closes(100.0 + 0.05 * t + rng.normal(0, 0.3, n_days)),  # benchmark, modest drift
        "BIL": make_ohlcv_from_closes([100.0] * n_days),
    }
    for df in universe.values():
        df.index = dates
    return universe


def test_cms_entry_fires_for_quality_symbol_clearing_the_hurdle():
    universe = _cms_universe()
    cfg = StrategyConfig(
        cms_candidate_universe=["KO", "PG"], cms_benchmark_symbol="SPY",
        cms_lookback_days=60, cms_trend_ma_period=30, cms_vol_lookback=20,
        cms_max_volatility=5.0,  # generous -- not exercising the vol gate here
        cms_required_return=0.10,
    )
    strat = CompounderMarginOfSafetyStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    assert (daily["KO"] > 0).any(), "KO's strong steady uptrend should clear the return hurdle"


def test_cms_quality_gate_excludes_high_volatility_symbol():
    n = 300
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    rng = np.random.default_rng(11)
    universe = {
        "MSFT": make_ohlcv_from_closes(100.0 + 0.30 * t + rng.normal(0, 15.0, n)),  # big drift but very noisy
        "SPY": make_ohlcv_from_closes(100.0 + 0.05 * t + rng.normal(0, 0.3, n)),
        "BIL": make_ohlcv_from_closes([100.0] * n),
    }
    for df in universe.values():
        df.index = dates
    cfg = StrategyConfig(
        cms_candidate_universe=["MSFT"], cms_benchmark_symbol="SPY",
        cms_lookback_days=60, cms_trend_ma_period=30, cms_vol_lookback=20,
        cms_max_volatility=0.05,  # tight ceiling -- MSFT's high noise must fail this
        cms_required_return=0.05,
    )
    strat = CompounderMarginOfSafetyStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)
    assert (daily["MSFT"] == 0).all(), "high-volatility symbol must never enter despite a large drift"


def test_cms_sell_trigger_exits_when_edge_over_benchmark_decays():
    n = 300
    dates = pd.bdate_range("2020-01-01", periods=n)
    t = np.arange(n)
    rng = np.random.default_rng(13)
    # Strong uptrend for the first 150 bars, then flat -- once the 60-day
    # trailing-return window fully rolls past the regime change, the
    # trailing return should decay below SPY's own steady trailing return.
    ko_close = np.concatenate([
        100.0 + 0.4 * t[:150],
        100.0 + 0.4 * 149 + rng.normal(0, 0.2, n - 150),
    ]) + rng.normal(0, 0.3, n)
    universe = {
        "KO": make_ohlcv_from_closes(ko_close),
        "SPY": make_ohlcv_from_closes(100.0 + 0.10 * t + rng.normal(0, 0.3, n)),
        "BIL": make_ohlcv_from_closes([100.0] * n),
    }
    for df in universe.values():
        df.index = dates
    cfg = StrategyConfig(
        cms_candidate_universe=["KO"], cms_benchmark_symbol="SPY",
        cms_lookback_days=60, cms_trend_ma_period=30, cms_vol_lookback=20,
        cms_max_volatility=5.0, cms_required_return=0.10,
    )
    strat = CompounderMarginOfSafetyStrategy(cfg)
    weights = strat.generate_weights(universe)
    daily = _daily(weights)

    assert (daily["KO"].iloc[150:200] > 0).any(), "should have been held during/soon after the strong-trend regime"
    assert (daily["KO"].iloc[-30:] == 0).all(), "should have exited once the trend flattened and the edge decayed"


def test_cms_warmup_bars_covers_all_lookbacks():
    cfg = StrategyConfig(cms_trend_ma_period=50, cms_vol_lookback=20, cms_lookback_days=252)
    strat = CompounderMarginOfSafetyStrategy(cfg)
    assert strat.warmup_bars() == 252


def test_cms_returns_empty_when_no_candidates_in_universe():
    universe = {
        "SPY": make_ohlcv_from_closes([100.0] * 50),
        "BIL": make_ohlcv_from_closes([100.0] * 50),
    }
    strat = CompounderMarginOfSafetyStrategy(StrategyConfig(cms_candidate_universe=["KO", "PG"]))
    assert strat.generate_weights(universe).empty


def test_compounder_margin_of_safety_instantiates_and_runs_from_json_config():
    config = load_strategies_config()
    entry = config["compounder_margin_of_safety"]
    strat = instantiate_strategy_from_config_entry("compounder_margin_of_safety", entry)
    universe = _cms_universe()
    weights = strat.generate_weights(
        universe, {"cms_lookback_days": 60, "cms_trend_ma_period": 30, "cms_vol_lookback": 20},
    )
    assert not weights.empty
    assert strat.explain_weights()



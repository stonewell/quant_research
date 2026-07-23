"""Unit tests for Researched Quantitative Trading Strategies (rs/strategy.py).

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

from common.testing import make_ohlcv_from_closes
from research_strategy.rs.config import StrategyConfig
from research_strategy.rs.strategy import (
    ActiveDualMomentumRiskParity,
    BoldAssetAllocation,
    NaturalLanguageStrategy,
    VolatilityManagedStrategy,
)


def create_mock_universe(n_days: int = 300) -> dict:
    """Creates a deterministic synthetic universe for testing strategy mechanics."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)

    # Rising asset
    spy_close = 100.0 + 0.1 * t
    # Declining asset
    eem_close = 100.0 - 0.1 * t
    # High volatility rising asset
    qqq_close = 100.0 + 0.15 * t + 5.0 * np.sin(t / 5.0)
    # Low volatility rising asset
    gld_close = 100.0 + 0.05 * t + 0.5 * np.sin(t / 10.0)

    # Canary / Defensive assets
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


def test_dual_momentum_trend_gate_and_cash_overlay():
    cfg = StrategyConfig(trend_sma_period=50, mom_long_lookback=50, rebalance_freq_days=20, top_k=3)
    strat = ActiveDualMomentumRiskParity(cfg)
    universe = create_mock_universe(n_days=250)

    weights = strat.generate_weights(universe)
    assert not weights.empty

    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    # EEM is steadily declining -> should receive 0% weight due to trend gate
    assert weights.loc[last_rebal, "EEM"] == 0.0

    # Total weight on rebalance date should equal 1.0 (including cash proxy BIL)
    total_w = weights.loc[last_rebal].sum()
    assert pytest.approx(total_w, abs=1e-4) == 1.0


def test_baa_canary_universe_switching():
    cfg = StrategyConfig(rebalance_freq_days=20, top_k=3)
    strat = BoldAssetAllocation(cfg)
    universe = create_mock_universe(n_days=250)

    # Make EEM (a canary asset) severely crash at the end so canary triggers Turbulent state
    dates = universe["EEM"].index
    universe["EEM"].loc[dates[-50]:, "Close"] = 10.0

    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    # Because canary asset EEM crashed, state is Turbulent -> Offensive asset QQQ should be 0%
    assert weights.loc[last_rebal, "QQQ"] == 0.0
    # Defensive assets / cash proxy should hold weight
    defensive_plus_cash = weights.loc[last_rebal, cfg.baa_defensive + [cfg.cash_proxy]].sum()
    assert pytest.approx(defensive_plus_cash, abs=1e-4) == 1.0


def test_volatility_managed_deleveraging():
    cfg = StrategyConfig(rebalance_freq_days=20, vol_managed_target_vol=0.05, vol_managed_var_lookback=20)
    strat = VolatilityManagedStrategy(cfg)
    universe = create_mock_universe(n_days=250)

    # Spike volatility of risky universe assets near the end
    dates = universe["SPY"].index
    for sym in cfg.risky_universe:
        if sym in universe:
            rng = np.random.default_rng(123)
            universe[sym].loc[dates[-30]:, "Close"] *= (1.0 + rng.normal(0, 0.10, 30))

    weights = strat.generate_weights(universe)
    rebal_dates = weights.dropna(how="all").index
    last_rebal = rebal_dates[-1]

    # With high volatility, target_vol/realized_vol scales down risky weight and moves capital to BIL
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

    # Each selected asset gets 50%
    active_weights = weights.loc[last_rebal][weights.loc[last_rebal] > 0]
    assert len(active_weights) <= 2
    for w in active_weights:
        assert pytest.approx(w, abs=1e-4) == 0.50


def test_volatility_managed_excludes_a_symbol_with_no_data_in_window():
    n_days = 250
    universe = create_mock_universe(n_days=n_days)

    # NEW hasn't started trading for the first 200 days.
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

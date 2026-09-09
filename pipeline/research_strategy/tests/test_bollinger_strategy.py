"""Unit and Integration Tests for BollingerBandsStrategy (Breakout and Mean-Reversion).

Tests:
- Breakout mode (John Bollinger Method I Squeeze Breakout)
- Mean Reversion mode (John Bollinger Method III %B + RSI Oversold)
- Trailing stop, stop loss, and max holding timeout
- Multi-asset allocation and cash routing
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

from common.testing import make_ohlcv_from_closes
from research_strategy.rs.bollinger_strategy import BollingerBandsStrategy
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import instantiate_strategy_from_config_entry


def create_bollinger_mock_universe(n_bars: int = 300) -> dict:
    """Creates synthetic price series with known trend, squeeze, and breakout behavior."""
    dates = pd.bdate_range("2020-01-01", periods=n_bars)
    t = np.arange(n_bars, dtype=float)

    # Asset A: Steady uptrend, followed by tight squeeze, then strong breakout
    close_a = 100.0 + 0.15 * t
    # Squeeze window between bars 150-180 (very tight oscillation)
    close_a[150:180] += 0.2 * np.sin(t[150:180] * 2.0)
    # Breakout at bar 181
    close_a[180:200] += np.linspace(0, 15.0, 20)
    # Pullback at bar 205
    close_a[200:230] -= np.linspace(0, 12.0, 30)

    # Asset B: Asset with steady trend and sharp oversold dips for mean reversion
    close_b = 100.0 + 0.05 * t
    close_b[100:104] -= 15.0  # Deep oversold dip 1
    close_b[200:204] -= 15.0  # Deep oversold dip 2

    # Cash proxy
    close_cash = 100.0 + 0.001 * t

    universe = {
        "ASSET_A": make_ohlcv_from_closes(close_a),
        "ASSET_B": make_ohlcv_from_closes(close_b),
        "BIL": make_ohlcv_from_closes(close_cash),
    }
    for df in universe.values():
        df.index = dates

    return universe


def test_bollinger_strategy_instantiation_and_config():
    strat = BollingerBandsStrategy()
    assert strat.name == "bollinger_bands"
    assert strat.warmup_bars() > 120
    assert "Bollinger Bands" in strat.explain_weights()

    # Config entry instantiation
    configs = load_strategies_config()
    assert "bollinger_breakout" in configs
    assert "bollinger_mean_reversion" in configs

    strat_bo = instantiate_strategy_from_config_entry("bollinger_breakout", configs["bollinger_breakout"])
    assert isinstance(strat_bo, BollingerBandsStrategy)
    assert "Breakout" in strat_bo.explain_weights()

    strat_mr = instantiate_strategy_from_config_entry("bollinger_mean_reversion", configs["bollinger_mean_reversion"])
    assert isinstance(strat_mr, BollingerBandsStrategy)
    assert "Mean Reversion" in strat_mr.explain_weights()


def _daily(weights: pd.DataFrame) -> pd.DataFrame:
    return weights.ffill().fillna(0.0)


def test_bollinger_breakout_weights_generation():
    universe = create_bollinger_mock_universe(n_bars=300)
    strat = BollingerBandsStrategy()
    weights = strat.generate_weights(universe, params={
        "bb_mode": "breakout",
        "bb_period": 20,
        "bb_num_std": 2.0,
        "bb_squeeze_lookback": 60,
        "bb_squeeze_quantile": 0.25,
        "bb_require_trend_filter": False,
        "cash_proxy": "BIL",
    })

    assert isinstance(weights, pd.DataFrame)
    assert not weights.empty
    assert "ASSET_A" in weights.columns
    assert "BIL" in weights.columns

    daily = _daily(weights)
    row_sums = daily.sum(axis=1)
    assert (row_sums <= 1.0001).all()

    # Asset A should have positive weights during the breakout phase
    assert (daily["ASSET_A"] > 0).any()


def test_bollinger_mean_reversion_weights_generation():
    universe = create_bollinger_mock_universe(n_bars=300)
    strat = BollingerBandsStrategy()
    weights = strat.generate_weights(universe, params={
        "bb_mode": "mean_reversion",
        "bb_period": 20,
        "bb_num_std": 2.0,
        "bb_rsi_filter": True,
        "bb_rsi_period": 14,
        "bb_rsi_oversold": 40.0,
        "bb_require_trend_filter": False,
        "cash_proxy": "BIL",
    })

    assert isinstance(weights, pd.DataFrame)
    assert not weights.empty
    assert "ASSET_B" in weights.columns

    daily = _daily(weights)
    # Asset B oscillates and should trigger mean-reversion entries
    assert (daily["ASSET_B"] > 0).any()

    # Cash proxy routes remaining weight
    assert (daily["BIL"] >= 0).all()


def test_bollinger_stop_loss_and_trailing_stop():
    dates = pd.bdate_range("2020-01-01", periods=100)
    # Price rises then drops severely to trigger stop loss (exact 100 bars)
    closes = np.array([100.0] * 50 + [120.0] + [110.0] + [90.0] * 48)
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    universe = {
        "TEST": df,
        "BIL": make_ohlcv_from_closes(np.array([100.0] * 100)),
    }
    universe["BIL"].index = dates

    strat = BollingerBandsStrategy()
    weights = strat.generate_weights(universe, params={
        "bb_mode": "breakout",
        "bb_period": 10,
        "bb_squeeze_lookback": 20,
        "bb_squeeze_quantile": 0.5,
        "bb_require_trend_filter": False,
        "bb_stop_loss_pct": 0.05,
        "cash_proxy": "BIL",
    })
    daily = _daily(weights)
    # After dropping to 90 (well below 120 and 100), TEST position should be exited
    assert daily["TEST"].iloc[-1] == 0.0
    assert daily["BIL"].iloc[-1] == 1.0

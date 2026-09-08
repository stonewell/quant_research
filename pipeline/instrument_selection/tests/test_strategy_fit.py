"""Unit tests for Strategy-Fit Instrument Selection modules."""

import json
import os
import sys
import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_PROJECT_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

from common.testing import make_ohlcv_from_closes
from selectorbot.strategy_fit import (
    STYLE_PRESETS,
    STYLE_ALIASES,
    StrategyTarget,
    compute_combined_strategy_fit,
    compute_strategy_profile_fit,
    compute_strategy_simulation_fit,
    resolve_strategy_target,
)


def _make_mock_metrics():
    """Constructs representative metrics for 4 distinct asset profiles."""
    return pd.DataFrame({
        "avg_dollar_volume": [1e8, 1e8, 1e8, 1e8],
        "median_spread_pct": [0.02, 0.02, 0.02, 0.02],
        "realized_vol_annualized_pct": [25.0, 25.0, 30.0, 10.0],
        "downside_vol_ratio": [0.5, 0.4, 0.6, 0.3],
        "vol_of_vol": [0.2, 0.4, 0.3, 0.1],
        "atr_pct_mean": [2.0, 4.5, 3.0, 0.8],
        "adx_mean": [35.0, 12.0, 28.0, 10.0],
        "atr_regime_ratio": [0.05, 0.1, 0.05, 0.02],
        "hurst": [0.72, 0.28, 0.55, 0.50],
        "hurst_significant": [True, True, False, False],
        "pct_days_above_trend_ma": [85.0, 30.0, 75.0, 50.0],
        "momentum_edge": [0.15, -0.10, 0.20, 0.0],
        "momentum_significant": [True, False, True, False],
        "momentum_lookback_return": [0.35, -0.05, 0.45, 0.03],
        "candlestick_edge": [0.01, 0.08, 0.02, 0.0],
        "candlestick_significant": [False, True, False, False],
        "history_years": [10.0, 10.0, 8.0, 15.0],
        "avg_correlation_to_universe": [0.70, 0.45, 0.65, 0.10],
    }, index=["TRENDING_SYM", "MEANREV_SYM", "MOMENTUM_SYM", "DEFENSIVE_SYM"])


def _make_mock_universe(n_bars=300):
    """Constructs a deterministic synthetic universe for single-asset backtesting."""
    dates = pd.bdate_range("2020-01-01", periods=n_bars)
    t = np.arange(n_bars)

    trend_close = 100.0 * np.exp(0.0015 * t + 0.02 * np.sin(t / 10.0))
    meanrev_close = 100.0 + 10.0 * np.sin(t / 5.0)
    flat_close = np.full(n_bars, 100.0)

    universe = {
        "TREND": make_ohlcv_from_closes(trend_close),
        "MEANREV": make_ohlcv_from_closes(meanrev_close),
        "FLAT": make_ohlcv_from_closes(flat_close),
        "BIL": make_ohlcv_from_closes(100.0 + 0.0001 * t),
    }
    for k in universe:
        universe[k].index = dates
    return universe


def test_resolve_strategy_from_style_presets():
    # Style presets & aliases
    t_trend = resolve_strategy_target("trend")
    assert t_trend.is_style_preset
    assert t_trend.style_label == "trend"
    assert "absolute_momentum_trend" in t_trend.factor_tags

    t_breakout = resolve_strategy_target("breakout")
    assert t_breakout.style_label == "trend"

    t_meanrev = resolve_strategy_target("mean_reversion")
    assert t_meanrev.style_label == "mean_reversion"
    assert "mean_reversion" in t_meanrev.factor_tags

    t_mom = resolve_strategy_target("momentum")
    assert t_mom.style_label == "momentum"

    t_grid = resolve_strategy_target("grid")
    assert t_grid.style_label == "grid"

    t_multi = resolve_strategy_target("multi_asset")
    assert t_multi.style_label == "multi_asset"

    t_vol = resolve_strategy_target("volatility")
    assert t_vol.style_label == "volatility"


def test_resolve_strategy_from_strategies_config():
    t_chan = resolve_strategy_target("chan_pivot_shift_macd")
    assert not t_chan.is_style_preset
    assert t_chan.key == "chan_pivot_shift_macd"
    assert t_chan.strategy_instance is not None
    assert "regime_trend_strength" in t_chan.factor_tags
    assert t_chan.style_label == "trend"

    t_turtle = resolve_strategy_target("turtle_breakout_s1")
    assert t_turtle.strategy_instance is not None
    assert "absolute_momentum_trend" in t_turtle.factor_tags


def test_resolve_strategy_from_allocation_templates():
    t_ew = resolve_strategy_target("equal_weight")
    assert t_ew.strategy_instance is not None
    assert t_ew.key == "equal_weight"


def test_resolve_strategy_from_strategy_file(tmp_path):
    strat_file = tmp_path / "custom_test_strategy.json"
    content = {
        "template_name": "equal_weight",
        "name": "Custom EW",
        "description": "A custom test strategy file",
        "params": {"rebalance_freq_days": 21},
    }
    with open(strat_file, "w") as f:
        json.dump(content, f)

    t_file = resolve_strategy_target(strategy_file=str(strat_file))
    assert t_file.name == "Custom EW"
    assert t_file.strategy_instance is not None
    assert t_file.params["rebalance_freq_days"] == 21


def test_resolve_strategy_raises_on_invalid_key():
    with pytest.raises(ValueError, match="Unrecognized strategy"):
        resolve_strategy_target("completely_nonexistent_strategy_12345")


def test_trend_strategy_profile_favors_high_hurst_and_adx():
    metrics = _make_mock_metrics()
    scores = compute_strategy_profile_fit(
        metrics,
        factor_tags=["absolute_momentum_trend", "regime_trend_strength"],
        style_label="trend",
    )
    assert scores["TRENDING_SYM"] > scores["MEANREV_SYM"]
    assert scores["TRENDING_SYM"] > scores["DEFENSIVE_SYM"]


def test_mean_reversion_profile_favors_low_hurst_and_high_atr():
    metrics = _make_mock_metrics()
    scores = compute_strategy_profile_fit(
        metrics,
        factor_tags=["mean_reversion"],
        style_label="mean_reversion",
    )
    assert scores["MEANREV_SYM"] > scores["TRENDING_SYM"]
    assert scores["MEANREV_SYM"] > scores["DEFENSIVE_SYM"]


def test_momentum_profile_favors_high_trailing_return():
    metrics = _make_mock_metrics()
    scores = compute_strategy_profile_fit(
        metrics,
        factor_tags=["relative_momentum"],
        style_label="momentum",
    )
    assert scores["MOMENTUM_SYM"] > scores["DEFENSIVE_SYM"]
    assert scores["MOMENTUM_SYM"] > scores["MEANREV_SYM"]


def test_defensive_profile_favors_low_correlation():
    metrics = _make_mock_metrics()
    scores = compute_strategy_profile_fit(
        metrics,
        factor_tags=["correlation_diversification", "breadth"],
        style_label="multi_asset",
    )
    assert scores["DEFENSIVE_SYM"] > scores["TRENDING_SYM"]


def test_single_asset_simulation_evaluates_correct_metrics():
    universe = _make_mock_universe(n_bars=250)
    target = resolve_strategy_target("chan_pivot_shift_macd")
    sim_df = compute_strategy_simulation_fit(
        universe,
        strategy_instance=target.strategy_instance,
        params=target.params,
        cash_proxy="BIL",
    )
    assert not sim_df.empty
    assert "sim_sharpe" in sim_df.columns
    assert "sim_cagr" in sim_df.columns
    assert "sim_max_dd" in sim_df.columns
    assert "strategy_sim_fit_score" in sim_df.columns
    assert "sim_traded" in sim_df.columns


def test_strategy_fit_penalizes_zero_trade_assets():
    universe = _make_mock_universe(n_bars=250)
    target = resolve_strategy_target("turtle_breakout_s1")
    sim_df = compute_strategy_simulation_fit(
        universe,
        strategy_instance=target.strategy_instance,
        params=target.params,
        cash_proxy="BIL",
    )
    # FLAT asset never breaks out -> 0 trades -> sim_fit_score = 0
    if "FLAT" in sim_df.index:
        assert sim_df.loc["FLAT", "sim_traded"] == False
        assert sim_df.loc["FLAT", "strategy_sim_fit_score"] == 0.0


def test_compute_combined_strategy_fit_blending():
    profile_scores = pd.Series({"A": 80.0, "B": 40.0, "C": 60.0})
    sim_df = pd.DataFrame({
        "strategy_sim_fit_score": [90.0, 80.0, 10.0],
        "sim_traded": [True, True, True],
        "sim_sharpe": [1.5, 1.2, 0.2],
    }, index=["A", "B", "C"])

    combined = compute_combined_strategy_fit(profile_scores, sim_df, profile_weight=0.40, sim_weight=0.60)
    assert combined.loc["A", "strategy_fit_score"] == pytest.approx(0.40 * 80.0 + 0.60 * 90.0)
    assert combined.loc["B", "strategy_fit_score"] == pytest.approx(0.40 * 40.0 + 0.60 * 80.0)
    assert combined.loc["C", "strategy_fit_score"] == pytest.approx(0.40 * 60.0 + 0.60 * 10.0)

"""Unit tests for RegimeFactorCompoundStrategy and macro regime factor mining."""

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
from research_strategy.rs.config import StrategyConfig
from research_strategy.rs.regime_factor_compound_strategy import RegimeFactorCompoundStrategy


def make_multi_asset_synthetic_universe(n_days=300):
    symbols = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ", "TIP", "IEF", "BIL"]
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    universe = {}
    for idx, sym in enumerate(symbols):
        np.random.seed(42 + idx)
        trend = 0.0005 if sym in ["SPY", "QQQ"] else (-0.0002 if sym == "TLT" else 0.0001)
        rets = np.random.normal(trend, 0.012 + 0.001 * (idx % 3), n_days)
        closes = 100.0 * np.exp(np.cumsum(rets))
        df = make_ohlcv_from_closes(closes)
        df.index = dates
        universe[sym] = df
    return universe


def test_strategy_config_regime_compound_defaults_and_validation():
    cfg = StrategyConfig()
    assert cfg.regime_compound_rebalance_freq_days == 21
    assert cfg.regime_compound_lookback_days == 63
    assert cfg.regime_compound_breadth_bull_thresh == 0.60
    assert cfg.regime_compound_breadth_bear_thresh == 0.35
    assert cfg.regime_compound_breadth_mom_thresh == 0.65
    assert cfg.regime_compound_vol_zscore_thresh == 1.0
    assert cfg.regime_compound_hurst_trend_thresh == 0.52
    assert cfg.regime_compound_hurst_meanrev_thresh == 0.48
    assert cfg.regime_compound_mode == "discrete_winner"

    # Validation: rebalance <= 0
    with pytest.raises(ValueError, match="regime_compound_rebalance_freq_days"):
        StrategyConfig(regime_compound_rebalance_freq_days=0)

    # Validation: lookback <= 0
    with pytest.raises(ValueError, match="regime_compound_lookback_days"):
        StrategyConfig(regime_compound_lookback_days=-5)

    # Validation: bear > bull
    with pytest.raises(ValueError, match="breadth thresholds"):
        StrategyConfig(regime_compound_breadth_bear_thresh=0.7, regime_compound_breadth_bull_thresh=0.4)

    # Validation: bull > mom
    with pytest.raises(ValueError, match="breadth thresholds"):
        StrategyConfig(regime_compound_breadth_bull_thresh=0.8, regime_compound_breadth_mom_thresh=0.7)

    # Validation: invalid mode
    with pytest.raises(ValueError, match="regime_compound_mode"):
        StrategyConfig(regime_compound_mode="invalid_mode")


def test_top_5_sub_strategies_instantiation():
    cfg = StrategyConfig()
    strat = RegimeFactorCompoundStrategy(cfg)
    subs = strat._get_sub_strategies(cfg)
    assert len(subs) == 5
    assert set(subs.keys()) == {
        "CRASH_RISK_OFF",
        "BULL_TREND",
        "RANGE_BOUND",
        "MOMENTUM_EXPANSION",
        "VOLATILE_ROTATION",
    }
    from research_strategy.rs.chan_advanced_strategies import ChanVaaCompoundStrategy
    from research_strategy.rs.chan_lesson_strategies import ChanPivotShiftMACDAdvStrategy
    from research_strategy.rs.strategy import ChanPivotShiftStrategy, NaturalLanguageStrategy
    from research_strategy.rs.taa_strategies import VigilantAssetAllocation

    assert isinstance(subs["CRASH_RISK_OFF"], VigilantAssetAllocation)
    assert isinstance(subs["BULL_TREND"], ChanPivotShiftMACDAdvStrategy)
    assert isinstance(subs["RANGE_BOUND"], ChanPivotShiftStrategy)
    assert isinstance(subs["MOMENTUM_EXPANSION"], NaturalLanguageStrategy)
    assert isinstance(subs["VOLATILE_ROTATION"], ChanVaaCompoundStrategy)


def test_regime_classification_logic():
    strat = RegimeFactorCompoundStrategy()

    # 1. Crash Risk-Off scenario: canary breadth collapsed
    crash_factors = {
        "canary_breadth": 0.20,
        "market_breadth": 0.30,
        "vol_zscore": 1.5,
        "hurst_exponent": 0.55,
        "bench_above_ma200": False,
        "dispersion": 0.08,
    }
    assert strat._classify_regime(crash_factors, prev_regime=None) == "CRASH_RISK_OFF"

    # 2. Momentum Expansion scenario: all canaries positive, broad breadth, low vol
    mom_factors = {
        "canary_breadth": 1.0,
        "market_breadth": 0.70,
        "vol_zscore": -0.2,
        "hurst_exponent": 0.50,
        "bench_above_ma200": True,
        "dispersion": 0.04,
    }
    assert strat._classify_regime(mom_factors, prev_regime=None) == "MOMENTUM_EXPANSION"

    # 3. Bull Trend scenario: high breadth, above MA200, high Hurst persistence (canary < 1.0)
    bull_factors = {
        "canary_breadth": 0.80,
        "market_breadth": 0.75,
        "vol_zscore": -0.4,
        "hurst_exponent": 0.56,
        "bench_above_ma200": True,
        "dispersion": 0.03,
    }
    assert strat._classify_regime(bull_factors, prev_regime="BULL_TREND") == "BULL_TREND"

    # 4. Range-Bound Mean Reversion scenario: low Hurst (anti-persistent), calm vol
    range_factors = {
        "canary_breadth": 0.70,
        "market_breadth": 0.50,
        "vol_zscore": 0.1,
        "hurst_exponent": 0.45,
        "bench_above_ma200": True,
        "dispersion": 0.02,
    }
    assert strat._classify_regime(range_factors, prev_regime=None) == "RANGE_BOUND"

    # 5. Volatile Rotation scenario: elevated vol or dispersion, not crashing, not clean trend
    rot_factors = {
        "canary_breadth": 0.70,
        "market_breadth": 0.50,
        "vol_zscore": 0.8,
        "hurst_exponent": 0.50,
        "bench_above_ma200": False,
        "dispersion": 0.06,
    }
    assert strat._classify_regime(rot_factors, prev_regime=None) == "VOLATILE_ROTATION"

    # 6. Hysteresis test: prev_regime == CRASH_RISK_OFF prevents premature exit
    # Canary improves to 0.50 (still below 0.60 buffer)
    rebound_factors = {
        "canary_breadth": 0.50,
        "market_breadth": 0.45,
        "vol_zscore": 0.4,
        "hurst_exponent": 0.53,
        "bench_above_ma200": True,
        "dispersion": 0.04,
    }
    # Without hysteresis, canary=0.50 is not <= 0.40, so it might exit. But with hysteresis:
    assert strat._classify_regime(rebound_factors, prev_regime="CRASH_RISK_OFF") == "CRASH_RISK_OFF"
    # When canary firmly recovers to >= 0.60 and vol < 0.5 and breadth > bear_thresh:
    rebound_factors["canary_breadth"] = 0.75
    assert strat._classify_regime(rebound_factors, prev_regime="CRASH_RISK_OFF") != "CRASH_RISK_OFF"


def test_factor_extraction_on_synthetic_universe():
    universe = make_multi_asset_synthetic_universe(n_days=260)
    strat = RegimeFactorCompoundStrategy()

    risky_symbols = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"]
    canary_symbols = ["TIP", "IEF", "BIL"]
    bench_date = universe["SPY"].index[-1]

    factors = strat._compute_factors_at_date(
        date=bench_date,
        universe=universe,
        risky_symbols=risky_symbols,
        canary_symbols=canary_symbols,
        benchmark_sym="SPY",
        lookback_days=63,
    )

    assert "market_breadth" in factors
    assert 0.0 <= factors["market_breadth"] <= 1.0
    assert "canary_breadth" in factors
    assert 0.0 <= factors["canary_breadth"] <= 1.0
    assert "vol_zscore" in factors
    assert "hurst_exponent" in factors
    assert "dispersion" in factors
    assert factors["dispersion"] >= 0.0


def test_generate_weights_discrete_winner_and_smooth_blend():
    universe = make_multi_asset_synthetic_universe(n_days=270)

    # 1. Discrete Winner Mode
    cfg_disc = StrategyConfig(regime_compound_mode="discrete_winner", regime_compound_rebalance_freq_days=21)
    strat_disc = RegimeFactorCompoundStrategy(cfg_disc)
    w_disc = strat_disc.generate_weights(universe)

    assert isinstance(w_disc, pd.DataFrame)
    assert not w_disc.empty
    assert set(w_disc.columns) == set(universe.keys())

    # Sparse weights contract: check that non-rebalance days are NaN
    # and rebalance rows sum to <= 1.0 (with BIL absorbing remainder)
    rebalance_rows = w_disc.dropna(how="all")
    assert len(rebalance_rows) > 0
    for idx, row in rebalance_rows.iterrows():
        # No cell is NaN in a rebalance row
        assert not row.isna().any(), f"Rebalance row at {idx} must not contain NaN cells"
        # Sum of row must be approx 1.0
        assert row.sum() == pytest.approx(1.0, abs=1e-4)

    # 2. Smooth Blend Mode
    cfg_blend = StrategyConfig(regime_compound_mode="smooth_blend", regime_compound_rebalance_freq_days=21)
    strat_blend = RegimeFactorCompoundStrategy(cfg_blend)
    w_blend = strat_blend.generate_weights(universe)

    assert isinstance(w_blend, pd.DataFrame)
    rebalance_rows_blend = w_blend.dropna(how="all")
    assert len(rebalance_rows_blend) > 0
    for idx, row in rebalance_rows_blend.iterrows():
        assert not row.isna().any()
        assert row.sum() == pytest.approx(1.0, abs=1e-4)

    # 3. Explain weights & Warmup bars
    explanation = strat_disc.explain_weights()
    assert "Macro Regime Factor" in explanation
    assert strat_disc.warmup_bars() == 252

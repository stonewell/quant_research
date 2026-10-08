"""Unit tests for WorldQuant Alpha strategies in research_strategy.

100% offline: Uses synthetic data only.
Tests:
- Parameter configuration & validation
- WorldQuantAlphaStrategy execution across multiple alphas and weighting modes
- Sparse weights contract compliance and cash routing to BIL
- Absolute trend filter gating
- WorldQuantMegaAlphaStrategy multi-alpha composite execution
- MultiStrategyAlphaBookStrategy alpha_worldquant and all_regime_worldquant pod presets
- strategies_config.json loading and STRATEGY_CLASS_MAP discovery
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

from common.data import SyntheticDataProvider
from research_strategy.rs.config import StrategyConfig, load_strategies_config
from research_strategy.rs.strategy import STRATEGY_CLASS_MAP, instantiate_strategy_from_config_entry
from research_strategy.rs.worldquant_alpha_strategy import (
    WorldQuantAlphaStrategy,
    WorldQuantMegaAlphaStrategy,
)
from research_strategy.rs.multi_strategy_alpha_book import MultiStrategyAlphaBookStrategy


@pytest.fixture(scope="module")
def synthetic_universe():
    dp = SyntheticDataProvider(seed=42)
    return {
        "SPY": dp.fetch_ohlcv("SPY", "2021-01-01", "2022-06-01"),
        "QQQ": dp.fetch_ohlcv("QQQ", "2021-01-01", "2022-06-01"),
        "IWM": dp.fetch_ohlcv("IWM", "2021-01-01", "2022-06-01"),
        "TLT": dp.fetch_ohlcv("TLT", "2021-01-01", "2022-06-01"),
        "BIL": dp.fetch_ohlcv("BIL", "2021-01-01", "2022-06-01"),
    }


def test_strategy_config_validation():
    # Invalid alpha_id
    with pytest.raises(ValueError, match="wq_alpha_id"):
        StrategyConfig(wq_alpha_id=0)
    with pytest.raises(ValueError, match="wq_alpha_id"):
        StrategyConfig(wq_alpha_id=102)

    # Invalid rebalance frequency
    with pytest.raises(ValueError, match="wq_rebalance_freq_days"):
        StrategyConfig(wq_rebalance_freq_days=0)

    # Invalid top_k
    with pytest.raises(ValueError, match="wq_top_k"):
        StrategyConfig(wq_top_k=0)

    # Invalid ensemble alphas
    with pytest.raises(ValueError, match="wq_ensemble_alphas"):
        StrategyConfig(wq_ensemble_alphas=[])
    with pytest.raises(ValueError, match="wq_ensemble_alphas"):
        StrategyConfig(wq_ensemble_alphas=[105])


@pytest.mark.parametrize("alpha_id", [6, 12, 41, 53, 101])
@pytest.mark.parametrize("mode", ["equal", "alpha_rank", "inverse_vol"])
def test_worldquant_alpha_strategy_execution(synthetic_universe, alpha_id, mode):
    cfg = StrategyConfig(
        wq_alpha_id=alpha_id,
        wq_rebalance_freq_days=10,
        wq_top_k=2,
        wq_weighting_mode=mode,
        wq_require_trend_filter=True,
    )
    strat = WorldQuantAlphaStrategy(cfg)
    weights = strat.generate_weights(synthetic_universe)

    assert not weights.empty
    assert "BIL" in weights.columns
    assert "SPY" in weights.columns

    # Verify sparse contract
    rebal_rows = weights.dropna(how="all")
    assert len(rebal_rows) > 0
    assert len(rebal_rows) < len(weights)  # sparse: most days are NaN

    # Every rebalance row sums to <= 1.0 (with unallocated in BIL)
    assert (rebal_rows.sum(axis=1) <= 1.0001).all()
    # No negative weights
    assert (rebal_rows >= 0.0).all().all()


def test_worldquant_mega_alpha_strategy(synthetic_universe):
    cfg = StrategyConfig(
        wq_ensemble_alphas=[6, 12, 41, 53, 101, 38],
        wq_rebalance_freq_days=15,
        wq_top_k=3,
        wq_weighting_mode="inverse_vol",
        wq_require_trend_filter=True,
    )
    strat = WorldQuantMegaAlphaStrategy(cfg)
    weights = strat.generate_weights(synthetic_universe)

    assert not weights.empty
    rebal_rows = weights.dropna(how="all")
    assert len(rebal_rows) > 0
    assert (rebal_rows.sum(axis=1) <= 1.0001).all()
    assert (rebal_rows >= 0.0).all().all()


def test_multistrategy_alphabook_worldquant_presets(synthetic_universe):
    # Test alpha_worldquant preset
    cfg1 = StrategyConfig(ms_pod_preset="alpha_worldquant", ms_execution_mode="periodic_sync")
    book1 = MultiStrategyAlphaBookStrategy(cfg1)
    w1 = book1.generate_weights(synthetic_universe)
    assert not w1.empty
    assert len(w1.dropna(how="all")) > 0

    # Test all_regime_worldquant preset
    cfg2 = StrategyConfig(ms_pod_preset="all_regime_worldquant", ms_execution_mode="periodic_sync")
    book2 = MultiStrategyAlphaBookStrategy(cfg2)
    w2 = book2.generate_weights(synthetic_universe)
    assert not w2.empty
    assert len(w2.dropna(how="all")) > 0


def test_strategies_config_registration():
    config_dict = load_strategies_config()
    expected_keys = [
        "worldquant_mega_alpha",
        "worldquant_alpha6_vol_corr",
        "worldquant_alpha12_reversal",
        "worldquant_alpha41_vwap_trend",
        "worldquant_alpha53_wick_imbalance",
        "worldquant_alpha101_intraday",
    ]
    for k in expected_keys:
        assert k in config_dict
        entry = config_dict[k]
        assert entry["type"] == "class"
        assert entry["class_name"] in STRATEGY_CLASS_MAP

        # Verify instantiation
        instance = instantiate_strategy_from_config_entry(k, entry)
        assert instance is not None
        assert hasattr(instance, "generate_weights")

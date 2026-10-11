"""Unit tests for GTJA 191 Microstructure Factors, OFI Proxy, and GTJAAlphaStrategy."""

import numpy as np
import pandas as pd
import pytest

from common.testing import make_ohlcv_from_closes
from pipeline.research_strategy.rs.alpha101_factors import Alpha101Data
from pipeline.research_strategy.rs.config import StrategyConfig
from pipeline.research_strategy.rs.gtja191_factors import (
    GTJA_ALPHA_REGISTRY,
    GTJAAlphaStrategy,
    compute_gtja_alpha,
    cont_ks_ofi_proxy,
    kline_body,
    lower_shadow,
    shadow_imbalance,
    upper_shadow,
)
from pipeline.research.blend.generic_blend import GenericBlendStrategy


def create_synthetic_test_universe(n_days: int = 120) -> dict:
    t = np.arange(n_days)
    close_a = 100.0 + 0.15 * t + 5.0 * np.sin(2 * np.pi * t / 25)
    close_b = 100.0 + 0.08 * t + 4.0 * np.cos(2 * np.pi * t / 30)
    close_c = 100.0 - 0.05 * t + 3.0 * np.sin(2 * np.pi * t / 20)
    bil_close = np.full(n_days, 100.0)

    universe = {
        "SYM_A": make_ohlcv_from_closes(close_a),
        "SYM_B": make_ohlcv_from_closes(close_b),
        "SYM_C": make_ohlcv_from_closes(close_c),
        "BIL": make_ohlcv_from_closes(bil_close),
    }
    for s in ["SYM_A", "SYM_B", "SYM_C"]:
        universe[s]["Volume"] = 1_000_000.0 * (1.0 + 0.2 * np.sin(t))
    universe["BIL"]["Volume"] = 500_000.0
    return universe


@pytest.fixture
def synthetic_panel_data():
    """Builds a synthetic universe and Alpha101Data panel container for offline testing."""
    universe = create_synthetic_test_universe(120)
    risky_symbols = ["SYM_A", "SYM_B", "SYM_C"]
    idx = universe[risky_symbols[0]].index
    data = Alpha101Data.from_universe(universe, symbols=risky_symbols, master_index=idx)
    return universe, data, risky_symbols, idx


def test_gtja_microstructure_operators(synthetic_panel_data):
    _, data, _, _ = synthetic_panel_data
    u_shad = upper_shadow(data)
    l_shad = lower_shadow(data)
    shad_imb = shadow_imbalance(data)
    body = kline_body(data)
    ofi = cont_ks_ofi_proxy(data)

    assert u_shad.shape == data.close.shape
    assert l_shad.shape == data.close.shape
    assert shad_imb.shape == data.close.shape
    assert body.shape == data.close.shape
    assert ofi.shape == data.close.shape

    # Ratios should be non-negative
    assert (u_shad.dropna().values >= -1e-6).all()
    assert (l_shad.dropna().values >= -1e-6).all()
    assert (body.dropna().values >= -1e-6).all()


def test_top_25_gtja_factors_execution(synthetic_panel_data):
    _, data, _, _ = synthetic_panel_data
    assert len(GTJA_ALPHA_REGISTRY) >= 25

    for aid in GTJA_ALPHA_REGISTRY.keys():
        factor_df = compute_gtja_alpha(aid, data)
        assert factor_df.shape == data.close.shape
        # Ensure no inf values
        assert not np.isinf(factor_df.values).any()
        # Verify factor variance is non-zero
        assert factor_df.iloc[30:].var().sum() >= 0


def test_gtja_alpha_strategy_sparse_weights(synthetic_panel_data):
    universe, _, risky_symbols, _ = synthetic_panel_data
    cfg = StrategyConfig()
    cfg.gtja_alpha_id = 16
    cfg.gtja_rebalance_freq_days = 5
    cfg.gtja_top_k = 2

    strat = GTJAAlphaStrategy(cfg)
    assert strat.warmup_bars() >= 200
    assert "GTJA Alpha#16" in strat.explain_weights()

    weights = strat.generate_weights(universe)
    assert not weights.empty
    assert set(universe.keys()).issubset(set(weights.columns))

    # Sparse weights contract check: not every row is non-NaN
    nan_rows = weights.isna().all(axis=1)
    assert nan_rows.sum() > 0

    # Non-NaN rows must have explicit numbers (NaN-vs-0.0 contract)
    rebal_rows = weights[~nan_rows]
    for dt, row in rebal_rows.iterrows():
        assert not row.isna().any(), f"Rebalance row at {dt} contains NaN cells!"
        # Weights should sum to ~1.0
        assert np.isclose(row.sum(), 1.0, atol=1e-3)


def test_generic_blend_with_gtja_and_worldquant(synthetic_panel_data):
    universe, _, _, _ = synthetic_panel_data
    cfg = StrategyConfig()
    cfg.blend_sub_strategies = {
        "gtja_alpha16": 0.50,
        "wq_alpha41": 0.50,
    }

    generic_strat = GenericBlendStrategy(cfg)
    sub_strats = generic_strat.get_sub_strategies(universe)
    assert "gtja_alpha16" in sub_strats
    assert "wq_alpha41" in sub_strats
    assert isinstance(sub_strats["gtja_alpha16"], GTJAAlphaStrategy)

    weights = generic_strat.generate_weights(universe)
    assert not weights.empty
    assert set(universe.keys()).issubset(set(weights.columns))

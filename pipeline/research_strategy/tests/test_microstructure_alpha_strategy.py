"""Offline unit & integration tests for MS7 (MicrostructureAlphaCompositeStrategy)
and MS13 (Track MS Exit Gate).

All tests run 100% offline on deterministic synthetic OHLCV universes.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.allocation_backtester import run_allocation_backtest
from common.lookahead_checks import assert_future_append_invariant
from common.selection_band import RankBandSelection
from pipeline.research_strategy.rs.config import load_strategies_config
from pipeline.research_strategy.rs.microstructure_alpha_strategy import (
    MicrostructureAlphaCompositeStrategy,
)
from pipeline.research_strategy.rs.strategy import (
    STRATEGY_CLASS_MAP,
    instantiate_strategy_from_config_entry,
)
from scripts.verify_ms7_exit_gate import (
    _generate_synthetic_ms7_universe,
    run_ms7_exit_gate_evaluation,
)


def test_ms7_version_and_registration():
    inst = MicrostructureAlphaCompositeStrategy()
    assert inst.version == "1.0.0"
    assert getattr(inst, "base_version", None) is None
    assert "MicrostructureAlphaCompositeStrategy" in STRATEGY_CLASS_MAP
    assert STRATEGY_CLASS_MAP["MicrostructureAlphaCompositeStrategy"] is MicrostructureAlphaCompositeStrategy

    cfg = load_strategies_config()
    assert "microstructure_alpha_composite" in cfg
    # Guardrail: chan_four_state_blend must stay pinned to regime_ic_ir_hybrid
    assert cfg["chan_four_state_blend"]["parameters"]["blend_weighting_mode"] == "regime_ic_ir_hybrid"

    built = instantiate_strategy_from_config_entry(
        "microstructure_alpha_composite", cfg["microstructure_alpha_composite"]
    )
    assert isinstance(built, MicrostructureAlphaCompositeStrategy)
    assert built.version == "1.0.0"


def test_ms7_sparse_weights_and_explicit_zero_exit_contract():
    universe = _generate_synthetic_ms7_universe(n_days=120, n_symbols=6, seed=7)
    strat = MicrostructureAlphaCompositeStrategy()
    weights = strat.generate_weights(universe, {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"})

    assert weights.shape == (120, 7)
    rebal_mask = weights.notna().any(axis=1)
    assert int(rebal_mask.sum()) >= 5

    # Non-rebalance rows must be 100% NaN across all columns (never forward-filled)
    non_rebal = weights.loc[~rebal_mask]
    assert non_rebal.isna().all().all()

    # Active rebalance rows: all columns finite (no NaN inside rebalance row!),
    # weights in [0, 1], sum to 1.0, hard single_stock_cap <= 0.35 respected,
    # and dropped symbols carry explicit 0.0
    active = weights.loc[rebal_mask]
    assert active.notna().all().all()
    assert (active.to_numpy() >= -1e-12).all()
    assert (active.to_numpy() <= 1.0 + 1e-12).all()
    risky_cols = [c for c in active.columns if c != "BIL"]
    assert float(active[risky_cols].max().max()) <= 0.35 + 1e-6
    np.testing.assert_allclose(active.sum(axis=1).to_numpy(), 1.0, atol=1e-6)


def test_ms7_empty_symbol_preserves_sparse_weights_contract():
    """Empty symbol in universe must get 0.0 ONLY on active rebalance rows, NaN otherwise."""
    universe = _generate_synthetic_ms7_universe(n_days=120, n_symbols=6, seed=7)
    universe["EMPTY_SYM"] = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    strat = MicrostructureAlphaCompositeStrategy()
    weights = strat.generate_weights(universe, {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"})

    assert "EMPTY_SYM" in weights.columns
    rebal_mask = weights.notna().any(axis=1)
    assert int(rebal_mask.sum()) >= 5
    assert int((~rebal_mask).sum()) > 50
    # Non-rebalance rows must stay 100% all-NaN (including EMPTY_SYM!)
    assert weights.loc[~rebal_mask].isna().all().all()
    # Active rebalance rows write explicit 0.0 for EMPTY_SYM
    assert (weights.loc[rebal_mask, "EMPTY_SYM"] == 0.0).all()


def test_ms7_reuses_rank_band_selection_and_reduces_turnover():
    universe = _generate_synthetic_ms7_universe(n_days=160, n_symbols=8, seed=11)
    strat = MicrostructureAlphaCompositeStrategy()

    with patch.object(RankBandSelection, "select", wraps=RankBandSelection.select, autospec=True) as spy:
        w_buffered = strat.generate_weights(
            universe,
            {
                "ms7_rebalance_freq_days": 10,
                "ms7_buy_band_pct": 0.25,
                "ms7_sell_band_pct": 0.60,
                "ms7_delta_score_sigma": 0.40,
                "ms7_turnover_penalty_l1": 0.08,
                "ms7_impact_penalty_l15": 0.15,
                "cash_proxy": "BIL",
            },
        )
        assert spy.call_count == 1

    w_tight = strat.generate_weights(
        universe,
        {
            "ms7_rebalance_freq_days": 10,
            "ms7_buy_band_pct": 0.20,
            "ms7_sell_band_pct": 0.21,
            "ms7_delta_score_sigma": 0.0,
            "ms7_turnover_penalty_l1": 0.0,
            "ms7_impact_penalty_l15": 0.0,
            "cash_proxy": "BIL",
        },
    )

    def _turn(w_df: pd.DataFrame) -> float:
        r = w_df.dropna(how="all")
        return float(0.5 * r.diff().abs().sum(axis=1).iloc[1:].sum())

    assert _turn(w_buffered) <= _turn(w_tight)


def test_ms7_tradability_no_zero_trade_starvation():
    universe = _generate_synthetic_ms7_universe(n_days=160, n_symbols=6, seed=19)
    strat = MicrostructureAlphaCompositeStrategy()
    w = strat.generate_weights(universe, {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"})
    bt = run_allocation_backtest(universe, w)
    rebal_rows = w.dropna(how="all")
    assert len(rebal_rows) >= 3
    risky_cols = [c for c in rebal_rows.columns if c != "BIL"]
    assert float(rebal_rows[risky_cols].sum(axis=1).max()) > 0.1
    m = bt.get("metrics", bt) if isinstance(bt.get("metrics"), dict) else bt
    assert np.isfinite(m["sharpe_ratio"])


def test_ms7_causal_invariance_ms12():
    universe = _generate_synthetic_ms7_universe(n_days=100, n_symbols=6, seed=23)
    strat = MicrostructureAlphaCompositeStrategy()
    dates_df = pd.DataFrame({"marker": np.arange(100)}, index=universe["BIL"].index)

    def _run_prefix(prefix_df: pd.DataFrame) -> pd.DataFrame:
        sub_u = {k: v.loc[prefix_df.index] for k, v in universe.items()}
        return strat.generate_weights(sub_u, {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"})

    assert_future_append_invariant(_run_prefix, dates_df, [55, 75], atol=1e-6, rtol=1e-6)


def test_ms13_exit_gate_single_ers_and_baseline_comparison():
    report = run_ms7_exit_gate_evaluation(
        n_days=260, n_symbols=8, n_folds=3, ers_n_random=15, seed=42
    )
    assert report["ers_validation"]["ers_called_count"] == 1
    assert report["tradability_proof"]["zero_trade_starvation"] is False
    assert "m1_2_csmom_rankband" in report["baseline_comparison"]
    assert "m1_3_minvar_l1" in report["baseline_comparison"]
    assert "ms7_risky_only_sharpe" in report["baseline_comparison"]
    assert report["ms7_winner_metrics"]["sharpe_ratio"] > 0.0
    assert report["ms7_winner_metrics"]["dsr_n_obs"] > 200
    assert report["ers_validation"]["percentile"] >= 0.50
    assert report["ablation_turnover_reduction"]["turnover_reduced"] is True
    assert report["ms5_prefeed_diagnostics"]["factor_source"] == "ms7_composite_scores"
    assert report["opt_in_exit_gate_passed"] is True

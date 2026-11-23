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
    assert inst.version == "1.1.0"
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
    assert built.version == "1.1.0"


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
        n_days=260, n_symbols=16, n_folds=3, ers_n_random=15, seed=42
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
    assert report["ms5_prefeed_diagnostics"]["asymmetric_status"] == "OK"
    assert report["ms5_prefeed_diagnostics"]["crowding_status"] in {
        "NORMAL",
        "CROWDED_WATCH",
        "QUANT_QUAKE_RISK",
    }
    probe = report["rule10_discriminating_probe"]
    assert probe["full_arm_diagnostics"]["real_turn_bars"] > 0
    assert probe["full_arm_diagnostics"]["has_any_5m"] is True
    assert probe["full_arm_diagnostics"]["smart_money_non_nan_cells"] > 0
    assert probe["proxy_arm_diagnostics"]["real_turn_bars"] == 0
    assert probe["proxy_arm_diagnostics"]["has_any_5m"] is False
    assert probe["n_changed_weight_cells"] > 0
    assert probe["discriminating_power_verified"] is True
    assert report["opt_in_exit_gate_passed"] is True


def _make_synthetic_5m_for_daily(daily_df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    for d, row in daily_df.iterrows():
        morn = pd.date_range(f"{d.date()} 09:35:00", f"{d.date()} 11:30:00", freq="5min")
        aft = pd.date_range(f"{d.date()} 13:05:00", f"{d.date()} 15:00:00", freq="5min")
        idx = morn.append(aft)
        n = len(idx)
        c_open = float(row["Open"])
        c_close = float(row["Close"])
        path = np.linspace(c_open, c_close, n) + rng.normal(0.0, 0.002 * c_open, size=n)
        path[-1] = c_close
        highs = np.maximum(path, np.roll(path, 1)) * 1.001
        lows = np.minimum(path, np.roll(path, 1)) * 0.999
        highs[0] = max(c_open, path[0]) * 1.001
        lows[0] = min(c_open, path[0]) * 0.999
        vols = rng.uniform(100.0, 500.0, size=n)
        frames.append(
            pd.DataFrame(
                {
                    "Open": np.r_[c_open, path[:-1]],
                    "High": highs,
                    "Low": lows,
                    "Close": path,
                    "Volume": vols,
                },
                index=idx,
            )
        )
    return pd.concat(frames)


def test_w13_ms7_consumes_real_turn_and_intraday_5m_with_discriminating_probes():
    """W1.3 / Rule 10: MS7 v1.1.0 consumes real Turn + 5m bars, populates _last_ms7_diagnostics,
    and demonstrates discriminating power between 5m-supplied vs daily-proxy-only runs."""
    universe_base = _generate_synthetic_ms7_universe(
        n_days=70, n_symbols=6, seed=31, include_turn=False
    )
    rng = np.random.default_rng(31)
    universe_turn = {
        sym: (
            df.assign(Turn=rng.uniform(0.5, 12.0, size=len(df)))
            if sym != "BIL"
            else df
        )
        for sym, df in universe_base.items()
    }
    universe_5m = {
        sym: _make_synthetic_5m_for_daily(df, seed=100 + idx)
        for idx, (sym, df) in enumerate(universe_turn.items())
        if sym != "BIL"
    }

    strat = MicrostructureAlphaCompositeStrategy()
    assert strat.version == "1.1.0"

    base_params = {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"}
    w_proxy = strat.generate_weights(universe_base, base_params)
    diag_proxy = dict(strat._last_ms7_diagnostics)
    assert diag_proxy["real_turn_bars"] == 0
    assert diag_proxy["proxy_turn_bars"] > 0
    assert diag_proxy["has_any_5m"] is False
    assert diag_proxy["iceberg_5m_bars"] == 0

    w_5m = strat.generate_weights(
        universe_turn, {**base_params, "intraday_5m_universe": universe_5m}
    )
    diag_5m = dict(strat._last_ms7_diagnostics)
    assert diag_5m["real_turn_bars"] > 0
    assert diag_5m["n_symbols_with_real_turn"] == 6
    assert diag_5m["has_any_5m"] is True
    assert diag_5m["n_symbols_with_5m"] == 6
    assert diag_5m["iceberg_5m_bars"] > 0
    assert diag_5m["smart_money_non_nan_cells"] > 0
    assert diag_5m["iceberg_non_nan_cells"] > 0

    # Rule 10 discriminating probe: weights differ when real Turn + 5m bars are supplied
    diff = (w_5m.fillna(0.0) - w_proxy.fillna(0.0)).abs().to_numpy().max()
    assert diff > 1e-6


def test_w13_ms7_backtest_and_live_5m_parity_and_no_params_mutation():
    """W1.3 / Rule 11: backtest run_standard and live transient pass-through produce identical
    MS7 weights on a synthetic 5m fixture and never mutate the caller's params dict."""
    import types
    from backtester.run_backtest import run_standard

    universe = _generate_synthetic_ms7_universe(n_days=70, n_symbols=6, seed=41)
    universe_5m = {
        sym: _make_synthetic_5m_for_daily(df, seed=200 + idx)
        for idx, (sym, df) in enumerate(universe.items())
        if sym != "BIL"
    }

    strat_live = MicrostructureAlphaCompositeStrategy()
    params = {"ms7_rebalance_freq_days": 10, "cash_proxy": "BIL"}
    live_weights = strat_live.generate_weights(
        universe, {**params, "intraday_5m_universe": universe_5m}
    )
    assert "intraday_5m_universe" not in params

    captured = {}

    class _CapturingMS7(MicrostructureAlphaCompositeStrategy):
        def generate_weights(self, u, p=None):
            captured["has_5m"] = bool((p or {}).get("intraday_5m_universe"))
            w = super().generate_weights(u, p)
            captured["weights"] = w
            return w

    bt_args = types.SimpleNamespace(
        start=str(universe["BIL"].index[0].date()),
        end=str(universe["BIL"].index[-1].date()),
        initial_capital=100_000.0,
        commission_pct=0.0005,
        slippage_pct=0.0005,
        min_shares=1,
        min_weight_change=0.0,
        china_trading=False,
        us_trading=False,
        hk_trading=False,
        execution_price="close",
        market_impact=False,
        market_impact_gamma=1.0,
        max_volume_participation=None,
        cash_sweep_rate=None,
        borrow_fee_annual=None,
        enable_position_lifecycle=False,
        lifecycle_compare=False,
    )
    strat_bt = _CapturingMS7()
    run_standard(universe, strat_bt, params, bt_args, intraday_5m_universe=universe_5m)
    assert "intraday_5m_universe" not in params
    assert captured["has_5m"] is True
    pd.testing.assert_frame_equal(captured["weights"], live_weights, rtol=0.0, atol=0.0)

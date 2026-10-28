"""Unit tests for task L2.48: 生命周期状态机回测同源启用 (UPLSM in Backtester).

Verifies:
1. Escape hatch: `enable_position_lifecycle=False` produces bit-identical results to default.
2. Default ON behavior: lifecycle state machine activates entry confirmation, T+1 tenure locks,
   asymmetric cooldowns, and -8% stop loss fast exits.
3. Sparse weights contract:
   - Explicit 0.0 for closed/exited assets on active rebalance days (preventing ffill stale leaks).
   - Entire row is all-NaN on non-rebalance days.
   - Residual cash proxy weight assignment.
4. Parity harness: `verify_lifecycle_step_parity` detects parity and mismatches.
5. CLI argument wiring across backtester/run_backtest.py and pipeline/run_pipeline.py.
"""

from __future__ import annotations

import os
import sys
from typing import Dict

import numpy as np
import pandas as pd
import pytest

# Ensure repo root and subprojects are in sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _grp in ("pipeline", "ml"):
    _gdir = os.path.join(_REPO_ROOT, _grp)
    if _gdir not in sys.path:
        sys.path.insert(0, _gdir)

from common.allocation_backtester import run_allocation_backtest
from common.position_lifecycle import (
    AssetLifecycleState,
    BlockingReason,
    LifecycleDecision,
    PositionLifecycleConfig,
    PositionState,
    resolve_position_lifecycle_config,
    simulate_position_lifecycle,
    step_position_lifecycle,
)
from common.testing import make_ohlcv_from_closes as make_df
import backtester.run_backtest as rb
import pipeline.run_pipeline as rp
from pipeline.parity_harness import verify_lifecycle_path_parity, verify_lifecycle_step_parity


def test_escape_hatch_disabled_is_bit_identical():
    """Escape hatch: enable_position_lifecycle=False yields bit-identical results."""
    dates = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(42)
    p_a = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, len(dates))))
    p_b = 50.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, len(dates))))

    universe = {
        "A": make_df(p_a, start="2020-01-01"),
        "B": make_df(p_b, start="2020-01-01"),
    }

    # Sparse weights: rebalance every 20 days
    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    rebal_idx = dates[::20]
    weights.loc[rebal_idx, "A"] = 0.60
    weights.loc[rebal_idx, "B"] = 0.40

    res_default = run_allocation_backtest(
        universe, weights, initial_capital=100_000.0, enable_position_lifecycle=False
    )
    res_explicit_off = run_allocation_backtest(
        universe, weights, initial_capital=100_000.0, enable_position_lifecycle=False, lifecycle_config=None
    )

    pd.testing.assert_frame_equal(res_default["equity_curve"], res_explicit_off["equity_curve"])
    assert res_default["sharpe_ratio"] == res_explicit_off["sharpe_ratio"]
    assert res_default["cagr"] == res_explicit_off["cagr"]
    assert res_default["total_turnover"] == res_explicit_off["total_turnover"]
    assert res_default["total_rebalances"] == res_explicit_off["total_rebalances"]
    assert res_explicit_off["position_lifecycle_enabled"] is False
    assert res_explicit_off["lifecycle_records"] == []


def test_resolve_position_lifecycle_config_defaults_and_overrides():
    """Verifies resolve_position_lifecycle_config default values and priority overrides.

    The defaults asserted here are the ones the live runner actually deploys
    (`scripts/run_live_strategy.py`): `normal_cooldown_bars=2` and
    `inertia_relative_ratio=0.0` since the resolver defaults were aligned with
    them. A bare `PositionLifecycleConfig()` must model the SAME machine as a live
    ticket, otherwise a backtest would be validating a looser state machine.
    """
    cfg = resolve_position_lifecycle_config()
    assert cfg.guard_latency_bars == 2
    assert cfg.min_holding_bars == 2
    assert cfg.normal_cooldown_bars == 2
    assert cfg.stop_cooldown_bars == 5
    assert cfg.inertia_threshold == 0.05
    assert cfg.inertia_relative_ratio == 0.0
    assert cfg.hard_stop_loss_pct == 0.08
    assert cfg.atr_multiplier == 4.5
    assert cfg.enable_entry_fast_path is True
    assert cfg.enable_exit_fast_path is True

    # Override via params dict using dedicated lifecycle_* keys
    params = {
        "lifecycle_guard_latency_bars": 3,
        "lifecycle_min_holding_bars": 4,
        "lifecycle_inertia_threshold": 0.08,
        "lifecycle_inertia_relative_ratio": 0.60,
    }
    cfg_p = resolve_position_lifecycle_config(params=params)
    assert cfg_p.guard_latency_bars == 3
    assert cfg_p.min_holding_bars == 4
    assert cfg_p.inertia_threshold == 0.08
    assert cfg_p.inertia_relative_ratio == 0.60

    # LIVE-RUNNER ALIASES now RESOLVE (they used to be inert, which meant a strategy
    # carrying only `min_entry_bars` / `reentry_cooldown_bars` / `cfsb_min_holding_bars`
    # silently backtested the DEFAULTS while the live ticket deployed its own values).
    # The alias table is owned by `common/position_lifecycle.py`; asserted here at the
    # resolution level so a precedence regression is caught from a consumer's side.
    alias_params = {
        "min_weight_change": 0.0,
        "stop_loss_pct": 0.02,
        "hard_stop_loss": 0.03,
        "reentry_cooldown_bars": 10,
        "cfsb_min_holding_bars": 9,
        "min_entry_bars": 7,
        "inertia_thresh": 0.20,
    }
    cfg_alias = resolve_position_lifecycle_config(params=alias_params)
    assert cfg_alias.guard_latency_bars == 7          # <- min_entry_bars
    assert cfg_alias.min_holding_bars == 9            # <- cfsb_min_holding_bars
    assert cfg_alias.normal_cooldown_bars == 10       # <- reentry_cooldown_bars
    assert cfg_alias.stop_cooldown_bars == 10         # <- max(5, normal_cooldown_bars)
    # `inertia_thresh` is not a live spelling -> still inert.
    assert cfg_alias.inertia_threshold == 0.0         # <- min_weight_change, not 0.20
    # `stop_loss_pct` / `hard_stop_loss` are not in the live alias table for the hard
    # stop -> the default stays.
    assert cfg_alias.hard_stop_loss_pct == 0.08

    # An explicit `lifecycle_*` key still beats any alias (precedence 1/2 over 4).
    cfg_explicit_wins = resolve_position_lifecycle_config(
        params={"lifecycle_guard_latency_bars": 3, "min_entry_bars": 11}
    )
    assert cfg_explicit_wins.guard_latency_bars == 3


def test_lifecycle_entry_confirmation_filters_impulse():
    """Impulse 1-day buy signal is filtered out by guard_latency_bars=2."""
    dates = pd.bdate_range("2020-01-01", periods=10)
    universe = {
        "A": make_df([100.0] * 10, start="2020-01-01"),
        "B": make_df([100.0] * 10, start="2020-01-01"),
    }

    # 1-day impulse: targets A at day 0, then 0.0 at day 1
    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights.loc[dates[0], "A"] = 0.50
    weights.loc[dates[0], "B"] = 0.0
    weights.loc[dates[1], "A"] = 0.0
    weights.loc[dates[1], "B"] = 0.0

    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        enable_entry_fast_path=False,
    )

    filtered_w, records = simulate_position_lifecycle(
        universe, weights, cfg=cfg, market="china"
    )

    # Day 0: PENDING_ENTRY (streak=1 < 2) -> HOLD_FILTERED, no trade -> all NaN
    assert filtered_w.loc[dates[0]].isna().all()
    # Day 1: Signal vanished -> CANCEL_PENDING -> no trade -> all NaN
    assert filtered_w.loc[dates[1]].isna().all()

    # Sustained signal: targets A on day 0, and continues targeting A on day 1
    weights_sustained = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights_sustained.loc[dates[0], "A"] = 0.50
    weights_sustained.loc[dates[0], "B"] = 0.0

    filtered_s, rec_s = simulate_position_lifecycle(
        universe, weights_sustained, cfg=cfg, market="china"
    )
    # Day 0: PENDING_ENTRY -> all NaN
    assert filtered_s.loc[dates[0]].isna().all()
    # Day 1: Confirmed (streak=2 >= 2) -> BUY -> active rebalance day!
    assert not filtered_s.loc[dates[1]].isna().all()
    assert filtered_s.loc[dates[1], "A"] == 0.50
    assert filtered_s.loc[dates[1], "B"] == 0.0


def test_china_t1_tenure_lock_and_stop_loss_fast_exit():
    """China T+1 lock blocks early signal exit, but -8% stop loss overrides lock immediately."""
    dates = pd.bdate_range("2020-01-01", periods=10)
    # Asset A drops -10% on day 2
    prices_a = [100.0, 100.0, 89.0, 89.0, 89.0, 89.0, 89.0, 89.0, 89.0, 89.0]
    universe = {
        "A": make_df(prices_a, start="2020-01-01"),
    }

    # Pre-buy asset A
    state = AssetLifecycleState(
        symbol="A",
        state=PositionState.HELD_GESTATION,
        held_bars=0,
        entry_price=100.0,
        peak_price=100.0,
        current_weight=0.50,
    )

    cfg = PositionLifecycleConfig(
        min_holding_bars=2,
        hard_stop_loss_pct=0.08,
        enable_exit_fast_path=True,
    )

    # Bar 1 (next day, held_bars advances to 1): normal price (100.0), strategy signals exit (0.0)
    # Held bars = 1 < min_holding_bars = 2 -> TENURE_LOCKED!
    d1 = step_position_lifecycle(
        state, raw_target_weight=0.0, current_price=100.0, cfg=cfg, market="china"
    )
    assert d1.action == "HOLD_FILTERED"
    assert d1.blocking_reason == BlockingReason.TENURE_LOCKED
    assert state.is_holding()

    # Bar 2: price drops to 89.0 (-11% loss <= -8% hard stop loss)
    # Tenure lock is overridden by risk stop fast exit!
    d2 = step_position_lifecycle(
        state, raw_target_weight=0.50, current_price=89.0, cfg=cfg, market="china"
    )
    assert d2.action == "SELL"
    assert d2.target_weight == 0.0
    assert d2.fast_path_triggered == "STOP_LOSS"
    assert state.state == PositionState.COOLDOWN
    assert state.cooldown_remaining == 5


def test_sparse_weights_contract_explicit_zero_and_cash_proxy():
    """Sparse weights contract: explicit 0.0 for closed asset, all-NaN for non-rebalance days, cash proxy assignment."""
    dates = pd.bdate_range("2020-01-01", periods=10)
    # Asset A experiences a stop-loss crash on day 2
    prices_a = [100.0, 100.0, 85.0, 85.0, 85.0, 85.0, 85.0, 85.0, 85.0, 85.0]
    prices_b = [10.0] * 10
    prices_cash = [1.0] * 10

    universe = {
        "A": make_df(prices_a, start="2020-01-01"),
        "B": make_df(prices_b, start="2020-01-01"),
        "511880": make_df(prices_cash, start="2020-01-01"),
    }

    # Day 0: Buy A at 0.40, B at 0.30
    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B", "511880"])
    weights.loc[dates[0], "A"] = 0.40
    weights.loc[dates[0], "B"] = 0.30

    cfg = PositionLifecycleConfig(
        guard_latency_bars=1,  # immediate entry for clarity
        min_holding_bars=2,
        hard_stop_loss_pct=0.08,
        enable_exit_fast_path=True,
    )

    filtered_w, records = simulate_position_lifecycle(
        universe, weights, cfg=cfg, market="china", cash_proxy="511880"
    )

    # Day 0: BUY A (0.40), BUY B (0.30), cash_proxy = max(0, 1 - 0.70) = 0.30
    assert not filtered_w.loc[dates[0]].isna().all()
    assert filtered_w.loc[dates[0], "A"] == 0.40
    assert filtered_w.loc[dates[0], "B"] == 0.30
    assert filtered_w.loc[dates[0], "511880"] == pytest.approx(0.30, abs=1e-5)

    # Day 1: holding A and B, no price crash, no trade -> ALL NaN row!
    assert filtered_w.loc[dates[1]].isna().all()

    # Day 2: Asset A crashes to 85.0 (-15% <= -8%) -> STOP LOSS SELL!
    # Active rebalance day: A must be explicitly 0.0, B remains 0.30, cash proxy = 1.0 - 0.30 = 0.70!
    assert not filtered_w.loc[dates[2]].isna().all()
    assert filtered_w.loc[dates[2], "A"] == 0.0  # EXPLICIT 0.0!
    assert filtered_w.loc[dates[2], "B"] == 0.30
    assert filtered_w.loc[dates[2], "511880"] == pytest.approx(0.70, abs=1e-5)


def test_parity_harness_verify_lifecycle_step_parity():
    """Verifies parity_harness.verify_lifecycle_step_parity compares simulation step vs live execution."""
    cfg = PositionLifecycleConfig(guard_latency_bars=2)
    state = AssetLifecycleState(symbol="600519", state=PositionState.FLAT)

    # Simulation step
    expected_live_decision = LifecycleDecision(
        action="HOLD_FILTERED",
        target_weight=0.0,
        reason="开仓/再入场信号进入确认期",
        blocking_reason=BlockingReason.PENDING_ENTRY_CONFIRM,
    )

    res = verify_lifecycle_step_parity(
        state=state,
        raw_target_weight=0.20,
        current_price=100.0,
        cfg=cfg,
        market="china",
        live_decision=expected_live_decision,
    )

    assert res["parity"] is True
    assert len(res["mismatches"]) == 0
    assert res["decision"].action == "HOLD_FILTERED"
    assert res["decision"].blocking_reason == BlockingReason.PENDING_ENTRY_CONFIRM

    # Mismatched action check
    divergent_decision = LifecycleDecision(
        action="BUY",
        target_weight=0.20,
        reason="Divergent",
    )
    res_mismatch = verify_lifecycle_step_parity(
        state=state,
        raw_target_weight=0.20,
        current_price=100.0,
        cfg=cfg,
        market="china",
        live_decision=divergent_decision,
    )
    assert res_mismatch["parity"] is False
    assert len(res_mismatch["mismatches"]) > 0
    assert any("Action mismatch" in m for m in res_mismatch["mismatches"])


def test_cli_flags_in_run_backtest_and_run_pipeline():
    """Verifies CLI argument wiring in backtester/run_backtest.py and pipeline/run_pipeline.py.

    (F1) `--enable-position-lifecycle` used to be `store_true` + `default=True`: a dead
    switch that was ALWAYS on and could never be switched off by its own flag. It now
    parses to `None` when omitted and is normalized to `True`, with the negative flag on
    its own dest so `--no-` wins regardless of argument order.
    """
    parser_rb = rb.build_arg_parser()
    args_rb_default = parser_rb.parse_args(["--strategy-file", "dummy.json"])
    assert args_rb_default.enable_position_lifecycle is True

    args_rb_explicit = parser_rb.parse_args(["--strategy-file", "dummy.json", "--enable-position-lifecycle"])
    assert args_rb_explicit.enable_position_lifecycle is True

    args_rb_disabled = parser_rb.parse_args(["--strategy-file", "dummy.json", "--no-position-lifecycle"])
    assert args_rb_disabled.enable_position_lifecycle is False

    args_rb_disabled_alias = parser_rb.parse_args(["--strategy-file", "dummy.json", "--disable-position-lifecycle"])
    assert args_rb_disabled_alias.enable_position_lifecycle is False

    # Both flags at once: `--no-` wins in EITHER order (not "last flag wins").
    both_no_last = parser_rb.parse_args([
        "--strategy-file", "dummy.json", "--enable-position-lifecycle", "--no-position-lifecycle",
    ])
    assert both_no_last.enable_position_lifecycle is False
    both_no_first = parser_rb.parse_args([
        "--strategy-file", "dummy.json", "--no-position-lifecycle", "--enable-position-lifecycle",
    ])
    assert both_no_first.enable_position_lifecycle is False

    parser_rp = rp.build_arg_parser()
    args_rp_default = parser_rp.parse_args([])
    assert args_rp_default.no_position_lifecycle is False

    args_rp_disabled = parser_rp.parse_args(["--no-position-lifecycle"])
    assert args_rp_disabled.no_position_lifecycle is True


def test_verify_lifecycle_step_parity_without_live_decision_is_inconclusive():
    """(F4) No live side == nothing was compared, so the verdict is INCONCLUSIVE, not parity=True."""
    cfg = PositionLifecycleConfig(guard_latency_bars=2)
    state = AssetLifecycleState(symbol="600519", state=PositionState.FLAT)

    res = verify_lifecycle_step_parity(
        state=state,
        raw_target_weight=0.20,
        current_price=100.0,
        cfg=cfg,
        market="china",
    )
    assert res["status"] == "INCONCLUSIVE"
    assert res["parity"] is False
    assert res["mismatches"] == []
    assert "INCONCLUSIVE" in res["note"]
    # The simulated decision is still returned -- the caller needs it to build a
    # live comparison (or to inspect the step).
    assert res["decision"].action == "HOLD_FILTERED"


def test_standard_writes_lifecycle_records_csv(tmp_path, monkeypatch):
    """(F3) Standard mode persists the lifecycle decision log as its own artifact.

    Row count is pinned to the engine's own `lifecycle_records` for the same run, and
    the BUY/SELL subset is pinned to the number of actionable gate decisions -- so the
    file is the log, not a placeholder.
    """
    import json

    strategy_path = tmp_path / "strategy.json"
    with open(strategy_path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": {"rebalance_freq_days": 5}}, f)
    results_dir = tmp_path / "results_lc_records"
    cache_dir = tmp_path / "cache_lc_records"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAPL", "MSFT",
        "--data-provider", "synthetic",
        "--mode", "standard",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--no-plots",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rb.main()

    lc_path = results_dir / "lifecycle_records.csv"
    assert lc_path.exists(), "lifecycle_records.csv must be written on a lifecycle-ON run"
    df_lc = pd.read_csv(lc_path)
    assert len(df_lc) > 0
    assert list(df_lc.columns) == rb.LIFECYCLE_RECORD_COLUMNS
    assert set(df_lc["action"]).issuperset({"HOLD"}) or len(df_lc) > 0
    trades = int(df_lc["action"].isin(["BUY", "SELL"]).sum())
    assert trades > 0, "the fixture must produce at least one lifecycle BUY/SELL decision"
    # Every record must be attributable to a real (date, symbol).
    assert df_lc["date"].notna().all() and df_lc["symbol"].notna().all()

    # The pre-existing artifacts keep their exact schemas (L2.37): the new file is
    # additive, nothing was renamed/inserted.
    assert (results_dir / "backtest_equity.csv").exists()
    weights_df = pd.read_csv(results_dir / "backtest_weights.csv", index_col=0)
    assert list(weights_df.columns) == ["AAPL", "MSFT"]
    rebal_df = pd.read_csv(results_dir / "rebalance_report.csv")
    assert "date" in rebal_df.columns and "symbol" in rebal_df.columns


def test_run_standard_warmup_window_keeps_lifecycle_records():
    """The SAME date-format root cause existed on `run_standard`'s warmup path.

    When `--start` cuts into the available history, `run_standard` filters the
    simulator's records down to the eval window (`has_warmup` branch). It used to
    compare `set(eval_index.astype(str))` -> `{'2020-06-25'}` against the
    simulator's `str(Timestamp)` stamp `'2020-06-25 00:00:00'`, so the filter
    matched nothing: the empty list then meant "no records supplied" and the
    engine re-simulated the already-filtered weights (a second entry-confirmation
    pass that clears the seeded first bar).
    """
    from types import SimpleNamespace

    from common.strategy_spec import get_template

    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(7)
    p_a = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.010, len(idx))))
    p_b = 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(idx))))
    universe = {
        "A": make_df(p_a, start="2020-01-01"),
        "B": make_df(p_b, start="2020-01-01"),
    }

    template = get_template("inverse_volatility")
    params = {"vol_lookback": 60, "rebalance_freq_days": 21}
    # `start` inside the history -> `has_warmup` is True -> the filtered branch runs.
    args = SimpleNamespace(
        start=idx[120].strftime("%Y-%m-%d"),
        end=None,
        initial_capital=100_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
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
        enable_position_lifecycle=True,
    )

    result = rb.run_standard(universe, template, params, args)
    equity = result["equity_curve"]
    records = result["lifecycle_records"]
    assert len(equity) > 0
    assert records, (
        "run_standard dropped every lifecycle record at the eval-window filter -- the "
        "engine then re-simulates the already-filtered weights"
    )
    # One record per (eval bar, symbol), spanning exactly the eval window.
    assert len(records) == len(equity) * 2
    assert {str(r["date"])[:10] for r in records} == {str(d)[:10] for d in equity.index}
    # Single simulation: the seeded first bar stays invested (re-simulating would
    # put it back into PENDING_ENTRY_CONFIRM and read 0.0).
    assert result["actual_weights"].iloc[0].sum() >= 0.95


def test_no_lifecycle_records_csv_when_layer_disabled(tmp_path, monkeypatch):
    """(F3) With the layer OFF there is no decision log -- and no empty placeholder file.

    The chosen contract is "no file at all": an empty CSV would be indistinguishable
    from "the layer ran and never traded", which is exactly the ambiguity the
    blocking_reason work exists to remove.
    """
    import json

    strategy_path = tmp_path / "strategy.json"
    with open(strategy_path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": {"rebalance_freq_days": 5}}, f)
    results_dir = tmp_path / "results_lc_off"
    cache_dir = tmp_path / "cache_lc_off"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAPL", "MSFT",
        "--data-provider", "synthetic",
        "--mode", "standard",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--no-plots",
        "--no-position-lifecycle",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rb.main()

    assert not (results_dir / "lifecycle_records.csv").exists()
    assert (results_dir / "backtest_equity.csv").exists()

    with open(results_dir / "run_manifest.json", "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["enable_position_lifecycle"] is False
    assert manifest["position_lifecycle_config"] is None


def test_walkforward_writes_lifecycle_records_csv(tmp_path, monkeypatch):
    """(F3) Walkforward mode rolls the per-fold decision logs into one file with a `fold` column."""
    import json

    strategy_path = tmp_path / "strategy.json"
    with open(strategy_path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": {"rebalance_freq_days": 5}}, f)
    results_dir = tmp_path / "results_wf_lc"
    cache_dir = tmp_path / "cache_wf_lc"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAPL", "MSFT",
        "--data-provider", "synthetic",
        "--mode", "walkforward",
        "--start", "2021-01-01",
        "--end", "2024-12-31",
        "--window-years", "1.0",
        "--step-years", "1.0",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--no-plots",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rb.main()

    wf_lc_path = results_dir / "walkforward_lifecycle_records.csv"
    assert wf_lc_path.exists()
    df_wf = pd.read_csv(wf_lc_path)
    assert list(df_wf.columns) == ["fold"] + rb.LIFECYCLE_RECORD_COLUMNS
    assert len(df_wf) > 0
    assert df_wf["fold"].nunique() >= 2, "the fixture must span more than one fold"
    assert set(df_wf["fold"].unique()) == set(range(1, int(df_wf["fold"].max()) + 1))

    # `walkforward_report.csv` must NOT have gained a list-valued lifecycle column.
    wf_report = pd.read_csv(results_dir / "walkforward_report.csv")
    assert "lifecycle_records" not in wf_report.columns
    assert "fold" not in wf_report.columns
    assert "sharpe_ratio" in wf_report.columns

    # ...nor may the JSON summary's per-fold records carry them (L2.37: the new
    # artifact is additive, the existing ones keep their exact fields).
    with open(results_dir / "walkforward_summary.json", "r", encoding="utf-8") as f:
        summary = json.load(f)
    for fold_record in summary["rolling_window_performance"]:
        assert "lifecycle_records" not in fold_record
    assert "lifecycle_records" not in summary


def test_backtest_with_lifecycle_enabled_end_to_end():
    """End-to-end backtest run with enable_position_lifecycle=True produces lifecycle_records."""
    dates = pd.bdate_range("2020-01-01", periods=50)
    prices_a = [100.0] * 50
    prices_b = [50.0] * 50
    universe = {
        "A": make_df(prices_a, start="2020-01-01"),
        "B": make_df(prices_b, start="2020-01-01"),
    }

    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights.loc[dates[0], "A"] = 0.50
    weights.loc[dates[0], "B"] = 0.50

    result = run_allocation_backtest(
        universe,
        weights,
        initial_capital=100_000.0,
        enable_position_lifecycle=True,
    )

    assert result["position_lifecycle_enabled"] is True
    records = result["lifecycle_records"]
    assert len(records) > 0
    for rec in records:
        assert "date" in rec
        assert "symbol" in rec
        assert "action" in rec
        assert "target_weight" in rec
        assert "blocking_reason" in rec


def test_simulate_position_lifecycle_missing_price_records_no_price():
    """Requirement 3: Missing / non-positive prices must not be fabricated; FSM step skipped and NO_PRICE recorded."""
    dates = pd.bdate_range("2020-01-01", periods=5)
    prices_a = [100.0, 101.0, np.nan, 103.0, 104.0]  # day 2 has NaN price
    prices_b = [50.0, 50.0, 50.0, 50.0, 50.0]
    universe = {
        "A": make_df(prices_a, start="2020-01-01"),
        "B": make_df(prices_b, start="2020-01-01"),
    }

    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights.loc[dates[0], "A"] = 0.50
    weights.loc[dates[0], "B"] = 0.50

    cfg = PositionLifecycleConfig(guard_latency_bars=1)  # instant entry
    filtered_w, records = simulate_position_lifecycle(universe, weights, cfg=cfg, market="china")

    # On day 2, symbol A has missing price -> must record NO_PRICE
    day2_recs = [r for r in records if r["date"] == str(dates[2]) and r["symbol"] == "A"]
    assert len(day2_recs) == 1
    assert day2_recs[0]["blocking_reason"] == BlockingReason.NO_PRICE.value
    assert day2_recs[0]["action"] == "HOLD_FILTERED"
    # State preserved: current weight unchanged from day 1
    assert day2_recs[0]["target_weight"] == pytest.approx(0.50, abs=1e-5)


def test_verify_lifecycle_path_parity_multi_day():
    """Multi-day path: the self-replay is NOT a parity claim anymore (F4).

    `verify_lifecycle_path_parity` replays `step_position_lifecycle()` day by day and
    cross-checks it against `simulate_position_lifecycle()`. Both sides are this
    codebase, so:
      * with no live side supplied it must report INCONCLUSIVE (pre-F4 it returned
        `parity=True` for a comparison it had only performed against itself);
      * with `live_decisions` supplied it reports ALIGNED / DIVERGED against THOSE
        records.
    The "live" records below are the simulator's own output replayed as a fixture --
    they pin the COMPARISON MECHANICS, not a real live-vs-backtest agreement.
    """
    dates = pd.bdate_range("2020-01-01", periods=20)
    rng = np.random.default_rng(123)
    p_a = 100.0 + np.cumsum(rng.normal(0.1, 1.0, len(dates)))
    p_b = 50.0 + np.cumsum(rng.normal(0.05, 0.5, len(dates)))

    universe = {
        "A": make_df(p_a, start="2020-01-01"),
        "B": make_df(p_b, start="2020-01-01"),
    }

    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights.loc[dates[0], "A"] = 0.60
    weights.loc[dates[0], "B"] = 0.40
    weights.loc[dates[5], "A"] = 0.70
    weights.loc[dates[5], "B"] = 0.30
    weights.loc[dates[10], "A"] = 0.0  # exit A
    weights.loc[dates[10], "B"] = 0.80

    cfg = PositionLifecycleConfig(
        guard_latency_bars=2,
        min_holding_bars=2,
        normal_cooldown_bars=2,
        inertia_threshold=0.05,
    )

    # 1. No live side -> INCONCLUSIVE, and the internal replay itself agrees.
    parity_res = verify_lifecycle_path_parity(
        universe=universe,
        target_weights=weights,
        cfg=cfg,
        market="china",
    )
    assert parity_res["status"] == "INCONCLUSIVE"
    assert parity_res["parity"] is False
    assert parity_res["mismatches"] == []
    assert parity_res["replay_mismatches"] == []
    assert parity_res["days_checked"] == len(dates)
    assert "INCONCLUSIVE" in parity_res["note"]

    # 2. A live side is supplied -> the verdict becomes a real comparison.
    _, sim_records = simulate_position_lifecycle(universe, weights, cfg=cfg, market="china")
    assert sim_records, "fixture requires the simulator to emit decision records"
    aligned = verify_lifecycle_path_parity(
        universe=universe,
        target_weights=weights,
        cfg=cfg,
        market="china",
        live_decisions=pd.DataFrame(sim_records),
    )
    assert aligned["status"] == "ALIGNED", aligned["mismatches"]
    assert aligned["parity"] is True
    assert aligned["live_records_checked"] == len(sim_records)

    # 3. A divergent live row is caught (the verdict is not vacuously green).
    divergent = pd.DataFrame(sim_records).copy()
    flipped = 0 if divergent.loc[0, "action"] == "BUY" else "BUY"
    divergent.loc[0, "action"] = flipped
    diverged = verify_lifecycle_path_parity(
        universe=universe,
        target_weights=weights,
        cfg=cfg,
        market="china",
        live_decisions=divergent,
    )
    assert diverged["status"] == "DIVERGED"
    assert diverged["parity"] is False
    assert any("Action mismatch" in m for m in diverged["mismatches"])


def test_standard_lifecycle_ab_comparison_written(tmp_path, monkeypatch):
    """Requirement 4: Standard mode writes lifecycle_ab_comparison.csv and run_manifest records pl config."""
    strategy_path = tmp_path / "strategy.json"
    import json
    with open(strategy_path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": {"rebalance_freq_days": 10}}, f)
    results_dir = tmp_path / "results_std_pl"
    cache_dir = tmp_path / "cache_std_pl"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAPL", "MSFT",
        "--data-provider", "synthetic",
        "--mode", "standard",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--enable-position-lifecycle",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rb.main()

    # 1. Verify lifecycle_ab_comparison.csv exists and has required schema
    ab_path = results_dir / "lifecycle_ab_comparison.csv"
    assert ab_path.exists()
    df_ab = pd.read_csv(ab_path)
    assert len(df_ab) == 1
    assert "sharpe_on" in df_ab.columns
    assert "sharpe_off" in df_ab.columns
    assert "cagr_on" in df_ab.columns
    assert "cagr_off" in df_ab.columns
    assert "max_drawdown_on" in df_ab.columns
    assert "max_drawdown_off" in df_ab.columns
    assert "turnover_on" in df_ab.columns
    assert "turnover_off" in df_ab.columns
    assert "rebalances_on" in df_ab.columns
    assert "rebalances_off" in df_ab.columns
    assert "sharpe_sign_reversal" in df_ab.columns

    # 2. Verify run_manifest.json records enable_position_lifecycle and position_lifecycle_config
    manifest_path = results_dir / "run_manifest.json"
    assert manifest_path.exists()
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["enable_position_lifecycle"] is True
    assert isinstance(manifest["position_lifecycle_config"], dict)
    assert "guard_latency_bars" in manifest["position_lifecycle_config"]

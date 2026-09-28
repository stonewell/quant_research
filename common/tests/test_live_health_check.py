"""Unit tests for Stage 1 live trading portfolio health & macro regime check script."""

import os
import sys
from typing import Dict

import numpy as np
import pandas as pd
import pytest

import importlib.util

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load the health check script via importlib (it's a standalone script, not a package module)
_health_script = os.path.join(_PROJECT_ROOT, "scripts", "check_live_portfolio_health.py")
_spec = importlib.util.spec_from_file_location("check_live_portfolio_health", _health_script)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

evaluate_market_breadth_and_thrust = _mod.evaluate_market_breadth_and_thrust
evaluate_realized_volatility = _mod.evaluate_realized_volatility
inspect_portfolio_drawdown = _mod.inspect_portfolio_drawdown
resolve_gate_directive = _mod.resolve_gate_directive
to_json_serializable = _mod.to_json_serializable


def test_inspect_portfolio_drawdown_normal():
    """Verify normal drawdown tier when DD < 10%."""
    state = {"nav_history_10d": [100000.0] * 10}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
    }
    res = inspect_portfolio_drawdown(current_nav=96000.0, peak_nav=100000.0, state=state, params=params)
    assert res["tier"] == "NORMAL"
    assert res["drawdown_pct"] == pytest.approx(-0.04)
    assert res["linear_equity_scale"] == 1.0
    assert res["freeze_remaining_bars"] == 0


def test_inspect_portfolio_drawdown_tier1_linear_damping():
    """Verify Tier 1 smooth linear damping when 10% <= DD < 15%."""
    state = {"tier1_consecutive_bars": 2}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
    }
    # DD = -12.5% -> between 10% and 20%: scale = 1.0 - (0.125 - 0.10) / 0.10 = 0.75
    res = inspect_portfolio_drawdown(current_nav=87500.0, peak_nav=100000.0, state=state, params=params)
    assert res["tier"] == "TIER_1_DAMPING"
    assert res["drawdown_pct"] == pytest.approx(-0.125)
    assert res["linear_equity_scale"] == pytest.approx(0.75)
    assert res["tier1_counter"] == 3


def test_inspect_portfolio_drawdown_tier2_defensive():
    """Verify Tier 2 tactical defense tier when 15% <= DD < 20%."""
    state = {"tier1_consecutive_bars": 5}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
    }
    # DD = -17.0% -> scale = 1.0 - (0.17 - 0.10) / 0.10 = 0.30
    res = inspect_portfolio_drawdown(current_nav=83000.0, peak_nav=100000.0, state=state, params=params)
    assert res["tier"] == "TIER_2_DEFENSIVE"
    assert res["drawdown_pct"] == pytest.approx(-0.17)
    assert res["linear_equity_scale"] == pytest.approx(0.30)


def test_inspect_portfolio_drawdown_tier3_emergency_halt():
    """Verify Tier 3 emergency halt when DD >= 20% with 21-day freeze."""
    state = {"tier3_consecutive_bars": 0, "freeze_remaining_bars": 0}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
    }
    res = inspect_portfolio_drawdown(current_nav=78000.0, peak_nav=100000.0, state=state, params=params)
    assert res["tier"] == "TIER_3_HALT"
    assert res["drawdown_pct"] == pytest.approx(-0.22)
    assert res["linear_equity_scale"] == 0.0
    assert res["freeze_remaining_bars"] == 21
    assert res["tier3_counter"] == 1


def test_inspect_portfolio_drawdown_tier1_auto_healing():
    """Verify auto-healing resets HWM after 15 consecutive bars in Tier 1."""
    state = {"tier1_consecutive_bars": 14}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
        "cfsb_tier1_cooldown_bars": 15,
    }
    res = inspect_portfolio_drawdown(current_nav=88000.0, peak_nav=100000.0, state=state, params=params)
    assert res["auto_healed"] is True
    assert res["peak_nav"] == 88000.0
    assert res["drawdown_pct"] == 0.0
    assert res["tier"] == "NORMAL"


def test_inspect_portfolio_drawdown_tier3_auto_healing():
    """Verify auto-healing resets HWM after 21 consecutive bars in Tier 3."""
    state = {"tier3_consecutive_bars": 20, "freeze_remaining_bars": 1}
    params = {
        "cfsb_dd_reduce_thresh": 0.10,
        "cfsb_dd_defensive_thresh": 0.15,
        "cfsb_dd_stop_thresh": 0.20,
    }
    res = inspect_portfolio_drawdown(current_nav=75000.0, peak_nav=100000.0, state=state, params=params)
    assert res["auto_healed"] is True
    assert res["peak_nav"] == 75000.0
    assert res["drawdown_pct"] == 0.0
    assert res["tier"] == "NORMAL"
    assert res["freeze_remaining_bars"] == 0


def test_evaluate_market_breadth_and_thrust():
    """Verify market breadth and 10-day momentum thrust calculation."""
    idx = pd.bdate_range("2023-01-01", periods=60)
    # Asset A: Strongly trending up (above SMA50 and positive ROC10)
    df_a = pd.DataFrame({"Close": np.linspace(100, 150, 60)}, index=idx)
    # Asset B: Strongly trending down (below SMA50 and negative ROC10)
    df_b = pd.DataFrame({"Close": np.linspace(150, 100, 60)}, index=idx)

    universe = {"STOCK_A": df_a, "STOCK_B": df_b}
    params = {
        "cfsb_breadth_lookback": 50,
        "cfsb_breadth_bull_thresh": 0.30,
        "cfsb_thrust_lookback": 10,
        "cfsb_thrust_thresh": 0.60,
        "cfsb_target_bull_exposure": 0.80,
    }

    res = evaluate_market_breadth_and_thrust(universe, params)
    assert res["breadth"] == pytest.approx(0.50)
    assert res["thrust"] == pytest.approx(0.50)
    assert res["breadth_active"] is True
    assert res["thrust_active"] is False
    assert len(res["leaders"]) == 2
    assert res["leaders"][0]["symbol"] == "STOCK_A"
    assert res["leaders"][0]["roc_10d"] > 0.0


def test_evaluate_realized_volatility():
    """Verify Barroso realized volatility targeting and scaling factor."""
    idx = pd.bdate_range("2023-01-01", periods=50)
    # Constant low volatility asset
    df = pd.DataFrame({"Close": [100.0 * (1.001 ** i) for i in range(50)]}, index=idx)
    universe = {"STOCK_A": df}
    params = {"cfsb_target_vol": 0.12}

    res = evaluate_realized_volatility(universe, params)
    assert res["target_vol"] == 0.12
    assert res["realized_vol_21d"] < 0.12
    assert res["vol_scaling_factor"] == 1.0
    assert res["vol_elevated"] is False


def test_resolve_gate_directive():
    """Verify gate directives for GO, CAUTION, and NO-GO."""
    # Scenario 1: Normal regime -> GO
    dd_normal = {
        "tier": "NORMAL",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 0,
        "linear_equity_scale": 1.0,
    }
    macro_bull = {
        "thrust_active": True,
        "thrust": 0.70,
        "thrust_thresh": 0.60,
        "target_exposure": 0.80,
        "breadth_active": True,
        "breadth": 0.50,
        "breadth_thresh": 0.30,
    }
    vol_normal = {
        "realized_vol_21d": 0.05,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }
    code, headline, actions = resolve_gate_directive(dd_normal, macro_bull, vol_normal)
    assert code == "GO"
    assert "GO:" in headline

    # Scenario 2: Tier 1 damping -> CAUTION
    dd_tier1 = {
        "tier": "TIER_1_DAMPING",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 0,
        "linear_equity_scale": 0.70,
    }
    code, headline, actions = resolve_gate_directive(dd_tier1, macro_bull, vol_normal)
    assert code == "CAUTION_DAMPING"
    assert "CAUTION:" in headline

    # Scenario 3: Tier 3 full stop -> NO-GO
    dd_tier3 = {
        "tier": "TIER_3_HALT",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 21,
        "linear_equity_scale": 0.0,
    }
    code, headline, actions = resolve_gate_directive(dd_tier3, macro_bull, vol_normal)
    assert code == "NO_GO_EMERGENCY"
    assert "NO-GO:" in headline


def test_to_json_serializable():
    """Verify recursive NumPy/pandas type conversion to standard JSON types."""
    data = {
        "bool_val": np.bool_(True),
        "int_val": np.int64(42),
        "float_val": np.float64(3.1415),
        "arr_val": np.array([1, 2, 3]),
        "series_val": pd.Series([4, 5, 6]),
    }
    res = to_json_serializable(data)
    assert type(res["bool_val"]) is bool
    assert type(res["int_val"]) is int
    assert type(res["float_val"]) is float
    assert res["arr_val"] == [1, 2, 3]
    assert res["series_val"] == [4, 5, 6]


def test_account_state_in_output_directory(tmp_path):
    """Verify account_state.json is loaded and saved in custom output directory."""
    state_file = str(tmp_path / "account_state.json")
    state = {
        "as_of_date": "2026-09-28",
        "current_nav": 120000.0,
        "peak_nav": 125000.0,
        "circuit_breaker_tier": "NORMAL",
    }
    _mod.save_account_state(state_file, state)
    assert os.path.exists(state_file)

    loaded = _mod.load_account_state(state_file)
    assert loaded["current_nav"] == 120000.0
    assert loaded["peak_nav"] == 125000.0


def test_parse_args_health_check_dynamic_output_dir(tmp_path):
    """Verify parse_args dynamically binds account_state_file to output-dir."""
    custom_dir = str(tmp_path / "my_run")
    args = _mod.parse_args(["--output-dir", custom_dir])
    assert args.output_dir == os.path.abspath(custom_dir)
    assert args.account_state_file == os.path.join(os.path.abspath(custom_dir), "account_state.json")


def test_parse_args_health_check_explicit_account_state(tmp_path):
    """Verify explicit account-state-file is preserved even if output-dir is set."""
    custom_dir = str(tmp_path / "my_run")
    custom_state = str(tmp_path / "custom_state.json")
    args = _mod.parse_args(["--output-dir", custom_dir, "--account-state-file", custom_state])
    assert args.account_state_file == os.path.abspath(custom_state)


def test_stage2_parse_args_dynamic_output_dir(tmp_path):
    """Verify Stage 2 run_live_four_state_blend dynamically binds account_state_file to output-dir."""
    _stage2_script = os.path.join(_PROJECT_ROOT, "scripts", "run_live_four_state_blend.py")
    _spec2 = importlib.util.spec_from_file_location("run_live_four_state_blend", _stage2_script)
    _mod2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)

    custom_dir = str(tmp_path / "orders")
    args = _mod2.parse_args(["--output-dir", custom_dir])
    assert args.output_dir == os.path.abspath(custom_dir)
    assert args.account_state_file == os.path.join(os.path.abspath(custom_dir), "account_state.json")
    assert args.health_report_file == os.path.join(os.path.abspath(custom_dir), "stage1_health_report.json")


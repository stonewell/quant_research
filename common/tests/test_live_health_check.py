"""Unit tests for Stage 1 live trading portfolio health & macro regime check script.

NOTE (2026-10-24): a companion file of the same name lives at
`pipeline/tests/test_live_health_check.py`. The two are NOT duplicates -- they
cover disjoint surfaces (this one: drawdown tiers, breadth/thrust, volatility,
gate directives, state I/O, holdings resolution; that one: the Stage-1 -> Stage-2
argv contract). Keep both, and keep the gate-directive semantics in sync.
"""

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
    """Verify gate directives for GO, CAUTION, NO-GO, and the fast-recovery override."""
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

    # Scenario 3: Tier 3 full stop with NO recovery signal -> NO-GO.
    # (2026-10-24) The counter-check must use a NO-THRUST macro snapshot.
    # `resolve_gate_directive` computes `fast_rec = fast_recovery_nav or
    # macro_info["thrust_active"]`, so pairing a TIER_3_HALT / active-freeze
    # case with `thrust_active=True` no longer reaches this branch at all --
    # it is the fast-recovery override (Scenario 4 below). Reusing
    # `macro_bull` here asserted the pre-3f64f83 precedence and is exactly what
    # broke; see pipeline/tests/test_live_health_check.py, which is the
    # companion suite covering a disjoint surface (the Stage-2 argv contract).
    dd_tier3 = {
        "tier": "TIER_3_HALT",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 21,
        "linear_equity_scale": 0.0,
    }
    macro_no_thrust = {
        "thrust_active": False,
        "thrust": 0.50,
        "thrust_thresh": 0.60,
        "target_exposure": 0.80,
        "breadth_active": False,
        "breadth": 0.20,
        "breadth_thresh": 0.30,
    }
    code, headline, actions = resolve_gate_directive(dd_tier3, macro_no_thrust, vol_normal)
    assert code == "NO_GO_EMERGENCY"
    assert "NO-GO:" in headline
    # No override fired, so the halt state must survive untouched.
    assert dd_tier3["tier"] == "TIER_3_HALT"
    assert dd_tier3["linear_equity_scale"] == pytest.approx(0.0)

    # Scenario 4: the fast-recovery override OUTRANKS an ACTIVE freeze window
    # (commit 3f64f83). Both recovery triggers downgrade TIER_3_HALT to Tier-2
    # defensive AND synchronize dd_info so the persisted account_state.json /
    # stage1_health_report.json carry the 0.30 scale rather than the stale 0.0.
    for recovery in (
        {"fast_recovery_nav": True, "freeze_remaining_bars": 3},
        {"fast_recovery_nav": False, "freeze_remaining_bars": 3},  # thrust-only
    ):
        dd_freeze = {
            "tier": "TIER_3_HALT",
            "fast_recovery_nav": recovery["fast_recovery_nav"],
            "freeze_remaining_bars": recovery["freeze_remaining_bars"],
            "linear_equity_scale": 0.0,
        }
        code, headline, actions = resolve_gate_directive(dd_freeze, macro_bull, vol_normal)
        assert code == "CAUTION_RECOVERING", recovery
        assert "TIER 3 DOWNGRADED VIA FAST RECOVERY OVERRIDE" in headline
        assert dd_freeze["tier"] == "TIER_2_DEFENSIVE"
        assert dd_freeze["linear_equity_scale"] == pytest.approx(0.30)
        assert "Fast Recovery Override" in dd_freeze["tier_label"]


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
    """Verify Stage 2 run_live_strategy dynamically binds account_state_file to output-dir."""
    _stage2_script = os.path.join(_PROJECT_ROOT, "scripts", "run_live_strategy.py")
    _spec2 = importlib.util.spec_from_file_location("run_live_strategy", _stage2_script)
    _mod2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)

    custom_dir = str(tmp_path / "orders")
    args = _mod2.parse_args(["--output-dir", custom_dir])
    assert args.output_dir == os.path.abspath(custom_dir)
    assert args.account_state_file == os.path.join(os.path.abspath(custom_dir), "account_state.json")
    assert args.health_report_file == os.path.join(os.path.abspath(custom_dir), "stage1_health_report.json")


def test_resolve_holdings_weights_and_shares_quantities():
    """Verify share counts are converted to exact weights and cash is derived from NAV."""
    _stage2_script = os.path.join(_PROJECT_ROOT, "scripts", "run_live_strategy.py")
    _spec2 = importlib.util.spec_from_file_location("run_live_strategy", _stage2_script)
    _mod2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)

    universe = {
        "STOCK_A": pd.DataFrame({"Close": [100.0]}),
        "STOCK_B": pd.DataFrame({"Close": [50.0]}),
    }
    raw_holdings = {"STOCK_A": 200, "STOCK_B": 1000}
    port_val = 100000.0

    weights, shares, eq_val, cash_val = _mod2.resolve_holdings_weights_and_shares(
        raw_holdings, universe, port_val, cash_proxy="BIL"
    )

    # STOCK_A: 200 * 100 = 20,000 (20%)
    # STOCK_B: 1000 * 50 = 50,000 (50%)
    # Total equity: 70,000 (70%)
    # Implied cash: 30,000 (30%)
    assert shares["STOCK_A"] == 200
    assert shares["STOCK_B"] == 1000
    assert eq_val == pytest.approx(70000.0)
    assert cash_val == pytest.approx(30000.0)
    assert weights["STOCK_A"] == pytest.approx(0.20)
    assert weights["STOCK_B"] == pytest.approx(0.50)
    assert weights["BIL"] == pytest.approx(0.30)


def test_resolve_holdings_weights_and_shares_exceeds_nav():
    """Verify cash is clamped at 0 when equity market value exceeds NAV."""
    _stage2_script = os.path.join(_PROJECT_ROOT, "scripts", "run_live_strategy.py")
    _spec2 = importlib.util.spec_from_file_location("run_live_strategy", _stage2_script)
    _mod2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)

    universe = {
        "STOCK_A": pd.DataFrame({"Close": [100.0]}),
    }
    raw_holdings = {"STOCK_A": 1200}  # 120,000 > 100,000
    port_val = 100000.0

    weights, shares, eq_val, cash_val = _mod2.resolve_holdings_weights_and_shares(
        raw_holdings, universe, port_val, cash_proxy="BIL"
    )

    assert eq_val == pytest.approx(120000.0)
    assert cash_val == pytest.approx(0.0)
    assert weights["BIL"] == pytest.approx(0.0)
    assert weights["STOCK_A"] == pytest.approx(1.20)


def test_resolve_holdings_weights_and_shares_legacy_weights():
    """Verify backward compatibility when fractional weights are passed."""
    _stage2_script = os.path.join(_PROJECT_ROOT, "scripts", "run_live_strategy.py")
    _spec2 = importlib.util.spec_from_file_location("run_live_strategy", _stage2_script)
    _mod2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)

    universe = {
        "STOCK_A": pd.DataFrame({"Close": [100.0]}),
        "STOCK_B": pd.DataFrame({"Close": [50.0]}),
    }
    raw_holdings = {"STOCK_A": 0.20, "STOCK_B": 0.50}
    port_val = 100000.0

    weights, shares, eq_val, cash_val = _mod2.resolve_holdings_weights_and_shares(
        raw_holdings, universe, port_val, cash_proxy="BIL"
    )

    assert weights["STOCK_A"] == pytest.approx(0.20)
    assert weights["STOCK_B"] == pytest.approx(0.50)
    assert weights["BIL"] == pytest.approx(0.30)
    assert shares["STOCK_A"] == 200
    assert shares["STOCK_B"] == 1000


# ---------------------------------------------------------------------------
# 10-Day NAV recovery honesty gate (2026-10-30 incident)
#
# A live run archive's `account_state.json` carried FIVE identical
# `nav_history_10d` values (197,351.17) because the operator form's
# `portfolio_value` was frozen; an archive repair then collapsed it to 4 REAL
# entries. The pre-fix gate did `r_10d = ... if len(nav_hist) >= 10 else 0.0`,
# so those 4 entries rendered as `10-Day NAV Recovery +0.00% / ⚪ None` -- an
# unmeasured metric printed as a measurement. The 5m intraday gate already
# established the house style: `SKIPPED` / `UNKNOWN` never means PASS.
#
# Contract now: `nav_10d_return is None` + `nav_10d_status == "INSUFFICIENT_HISTORY"`
# + `fast_recovery_nav is False` (never turn an override ON from missing data),
# with tier / circuit-breaker / linear-scale outputs untouched.
# ---------------------------------------------------------------------------

_NAV_DD_PARAMS = {
    "cfsb_dd_reduce_thresh": 0.10,
    "cfsb_dd_defensive_thresh": 0.15,
    "cfsb_dd_stop_thresh": 0.20,
}


def _nav_history(n: int, *, start_nav: float = 100000.0, step: float = 0.0) -> list:
    return [start_nav + step * i for i in range(n)]


def test_nav_10d_insufficient_history_is_not_a_measured_zero():
    """4 real entries must be reported as UNMEASURED, never as a flat 0.00%.

    DISCRIMINATING: the pre-fix implementation returned `nav_10d_return == 0.0`
    for this exact input, so both the `is None` and the status assertions fail
    against it.
    """
    state = {"nav_history_10d": _nav_history(4)}
    res = inspect_portfolio_drawdown(
        current_nav=197351.17, peak_nav=197351.17, state=state, params=_NAV_DD_PARAMS
    )

    assert res["nav_10d_status"] == "INSUFFICIENT_HISTORY"
    assert res["nav_10d_return"] is None, "unmeasured history must not be a 0.00% 'measurement'"
    assert res["nav_10d_history_len"] == 4
    assert res["nav_10d_required_len"] == 10
    assert "4/10" in (res["nav_10d_unavailable_reason"] or "")
    # Conservative: missing data can never switch the override ON.
    assert res["fast_recovery_nav"] is False


def test_nav_10d_empty_history_also_insufficient():
    """No history at all is the same unmeasured state (not a 0.00% flat series)."""
    res = inspect_portfolio_drawdown(
        current_nav=100000.0, peak_nav=100000.0, state={}, params=_NAV_DD_PARAMS
    )
    assert res["nav_10d_status"] == "INSUFFICIENT_HISTORY"
    assert res["nav_10d_return"] is None
    assert res["nav_10d_history_len"] == 0
    assert res["fast_recovery_nav"] is False


def test_nav_10d_full_history_behaviour_unchanged():
    """A full 10-entry window keeps the pre-existing measurement semantics."""
    # Positive recovery -> measured return AND override ON.
    res_up = inspect_portfolio_drawdown(
        current_nav=105000.0,
        peak_nav=105000.0,
        state={"nav_history_10d": _nav_history(10, start_nav=100000.0)},
        params=_NAV_DD_PARAMS,
    )
    assert res_up["nav_10d_status"] == "OK"
    assert res_up["nav_10d_return"] == pytest.approx(0.05)
    assert res_up["nav_10d_history_len"] == 10
    assert res_up["nav_10d_unavailable_reason"] is None
    assert res_up["fast_recovery_nav"] is True

    # Negative recovery -> measured (not None), override OFF.
    res_down = inspect_portfolio_drawdown(
        current_nav=95000.0,
        peak_nav=100000.0,
        state={"nav_history_10d": _nav_history(10, start_nav=100000.0)},
        params=_NAV_DD_PARAMS,
    )
    assert res_down["nav_10d_status"] == "OK"
    assert res_down["nav_10d_return"] == pytest.approx(-0.05)
    assert res_down["fast_recovery_nav"] is False


def test_nav_10d_status_never_changes_tier_or_linear_scale():
    """Tier / circuit-breaker / linear-scale outputs must be history-length invariant.

    Same NAV, same HWM, only the history length differs: the verdict the operator
    acts on must be identical. Only the *disclosure* of the recovery leg differs.
    """
    short = inspect_portfolio_drawdown(
        current_nav=88000.0,
        peak_nav=100000.0,
        state={"nav_history_10d": _nav_history(4, start_nav=90000.0)},
        params=_NAV_DD_PARAMS,
    )
    full = inspect_portfolio_drawdown(
        current_nav=88000.0,
        peak_nav=100000.0,
        state={"nav_history_10d": _nav_history(10, start_nav=90000.0)},
        params=_NAV_DD_PARAMS,
    )

    assert short["nav_10d_status"] == "INSUFFICIENT_HISTORY"
    assert full["nav_10d_status"] == "OK"
    for key in (
        "tier",
        "tier_label",
        "drawdown_pct",
        "drawdown_mag",
        "linear_equity_scale",
        "peak_nav",
        "tier1_counter",
        "tier3_counter",
        "freeze_remaining_bars",
        "auto_healed",
    ):
        assert short[key] == full[key], key
    assert short["tier"] == "TIER_1_DAMPING"
    assert short["linear_equity_scale"] == pytest.approx(0.80)


def test_gate_directive_warns_fast_recovery_override_unavailable():
    """The actions section must say UNAVAILABLE -- not "not triggered".

    Also pins the conservative direction: TIER_3_HALT with unmeasured history
    stays NO_GO_EMERGENCY (the override may not be granted from missing data).
    """
    dd_info = inspect_portfolio_drawdown(
        current_nav=78000.0,
        peak_nav=100000.0,
        state={"nav_history_10d": _nav_history(4)},
        params=_NAV_DD_PARAMS,
    )
    macro = {
        "thrust_active": False,
        "thrust": 0.50,
        "thrust_thresh": 0.60,
        "breadth_active": False,
        "breadth": 0.20,
        "breadth_thresh": 0.30,
        "target_exposure": 0.80,
    }
    vol = {
        "realized_vol_21d": 0.05,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }

    code, _headline, actions = resolve_gate_directive(dd_info, macro, vol)

    assert code == "NO_GO_EMERGENCY", "an unmeasured override must not release the halt"
    assert dd_info["tier"] == "TIER_3_HALT"
    assert dd_info["linear_equity_scale"] == pytest.approx(0.0)
    warning = [a for a in actions if "Fast-Recovery NAV override UNAVAILABLE" in a]
    assert warning, actions
    assert "4/10" in warning[0]
    assert "PASS" in warning[0]

    # Counter-check: a measured full window emits no such warning (behaviour of
    # the OK path is untouched).
    dd_full = inspect_portfolio_drawdown(
        current_nav=78000.0,
        peak_nav=100000.0,
        state={"nav_history_10d": _nav_history(10)},
        params=_NAV_DD_PARAMS,
    )
    _code, _headline, actions_full = resolve_gate_directive(dd_full, macro, vol)
    assert not [a for a in actions_full if "Fast-Recovery NAV override UNAVAILABLE" in a]


def test_report_renders_na_when_nav_history_insufficient(tmp_path):
    """Markdown metric row must print `N/A (4/10 个交易日, 数据不足)`, not `+0.00%`."""
    import json

    dd_info = inspect_portfolio_drawdown(
        current_nav=197351.17,
        peak_nav=197351.17,
        state={"nav_history_10d": _nav_history(4)},
        params=_NAV_DD_PARAMS,
    )
    macro = {
        "breadth": 0.55,
        "breadth_active": True,
        "breadth_thresh": 0.30,
        "thrust": 0.62,
        "thrust_active": True,
        "thrust_thresh": 0.60,
        "target_exposure": 0.80,
        "leaders": [],
    }
    vol = {
        "realized_vol_21d": 0.11,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }
    _code, _headline, actions = resolve_gate_directive(dd_info, macro, vol)

    json_path, md_path = _mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-10-30",
        strategy_info={"strategy_name": "unit_test", "template_name": "equal_weight"},
        dd_info=dd_info,
        macro_info=macro,
        vol_info=vol,
        gate_code="GO",
        gate_headline="GO",
        actions=actions,
    )

    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    assert "N/A (4/10 个交易日, 数据不足)" in md
    assert "UNAVAILABLE" in md
    nav_row = [ln for ln in md.splitlines() if ln.startswith("| **10-Day NAV Recovery**")]
    assert len(nav_row) == 1, nav_row
    assert "N/A" in nav_row[0]
    assert "+0.00%" not in nav_row[0]
    # The warning must ALSO be in the actions section, not only the metric table.
    assert "Fast-Recovery NAV override UNAVAILABLE" in md

    with open(json_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    assert report["portfolio_health"]["nav_10d_status"] == "INSUFFICIENT_HISTORY"
    assert report["portfolio_health"]["nav_10d_return"] is None
    assert report["portfolio_health"]["fast_recovery_nav"] is False
    assert any("Fast-Recovery NAV override UNAVAILABLE" in a for a in report["trader_actions"])


def test_report_renders_measured_value_when_nav_history_full(tmp_path):
    """A measured window keeps rendering the real number (no regression)."""
    dd_info = inspect_portfolio_drawdown(
        current_nav=105000.0,
        peak_nav=105000.0,
        state={"nav_history_10d": _nav_history(10, start_nav=100000.0)},
        params=_NAV_DD_PARAMS,
    )
    macro = {
        "breadth": 0.55,
        "breadth_active": True,
        "breadth_thresh": 0.30,
        "thrust": 0.62,
        "thrust_active": True,
        "thrust_thresh": 0.60,
        "target_exposure": 0.80,
        "leaders": [],
    }
    vol = {
        "realized_vol_21d": 0.11,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
        "vol_elevated": False,
    }
    _code, _headline, actions = resolve_gate_directive(dd_info, macro, vol)
    _json_path, md_path = _mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-10-30",
        strategy_info={"strategy_name": "unit_test", "template_name": "equal_weight"},
        dd_info=dd_info,
        macro_info=macro,
        vol_info=vol,
        gate_code="GO",
        gate_headline="GO",
        actions=actions,
    )
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    nav_row = [ln for ln in md.splitlines() if "10-Day NAV Recovery" in ln]
    assert len(nav_row) == 1
    assert "+5.00%" in nav_row[0]
    assert "🚀 Active" in nav_row[0]
    assert "UNAVAILABLE" not in md


def test_append_nav_history_point_flags_frozen_series_and_caps_at_ten():
    """A duplicate point is appended AND flagged; the list stays ordered and <= 10.

    DISCRIMINATING for the root cause: the archived frozen series
    (5 x 197,351.17) was written silently. It must now raise a flag on every
    consecutive duplicate, so no frozen form value can masquerade as a flat NAV
    history again.
    """
    append = _mod.append_nav_history_point

    # Consecutive duplicate -> appended, flagged True.
    hist, frozen = append([197351.17] * 4, 197351.17)
    assert frozen is True
    assert hist == [197351.17] * 5
    hist, frozen = append(hist, 197351.17)
    assert frozen is True
    assert hist == [197351.17] * 6

    # A genuinely new value -> appended, not flagged.
    hist, frozen = append(hist, 198000.0)
    assert frozen is False
    assert hist[-1] == 198000.0

    # Cap at 10, oldest first (chronological order preserved).
    long_hist, frozen = append(list(range(1, 11)), 11.0)
    assert frozen is False
    assert long_hist == list(range(2, 12))
    assert len(long_hist) == _mod.NAV_10D_RECOVERY_WINDOW

    # Empty history is not a "frozen series".
    hist, frozen = append([], 100000.0)
    assert frozen is False
    assert hist == [100000.0]

    # The window constant is the single owner of the cap.
    assert _mod.NAV_10D_RECOVERY_WINDOW == 10


def test_main_wires_effective_nav_into_nav_history_not_form_value():
    """The state-update call site must persist `dd_info["current_nav"]` (effective NAV).

    Exercises the wiring without a data provider: the append call in `main` is
    the ONLY writer of `nav_history_10d`, and it must be fed the NAV the tier
    math acted on -- not the raw operator form value, which is what froze and
    produced the repeated 197,351.17 archive.
    """
    import inspect as _inspect

    src = _inspect.getsource(_mod.main)
    start = src.index("append_nav_history_point(")
    depth = 0
    end = len(src)
    for i in range(start, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    call_site = src[start:end]

    assert 'dd_info["current_nav"]' in call_site, call_site
    assert "args.portfolio_value" not in call_site, call_site



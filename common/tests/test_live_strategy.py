"""Unit tests for universal live strategy runner (scripts/run_live_strategy.py)."""

import importlib.util
import json
import os
import sys
import pytest
import pandas as pd

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.run_live_strategy import (
    pad_east_asian,
    resolve_lot_size,
    resolve_trade_inertia_threshold,
    resolve_min_entry_bars,
    resolve_min_holding_bars,
    resolve_holding_bars,
    resolve_exit_cooldown,
    resolve_strategy_definition,
    resolve_holdings_weights_and_shares,
    load_symbol_names,
    parse_args,
    apply_intraday_distribution_gate,
)


def test_pad_east_asian():
    # ASCII text
    assert len(pad_east_asian("ABC", 6)) == 6
    # Chinese text (each character has display width 2)
    s = pad_east_asian("中国", 6)
    # "中国" takes 4 display width, so 2 spaces added -> string length is 4 (2 chars + 2 spaces)
    assert len(s) == 4
    assert s == "中国  "


def test_resolve_lot_size():
    assert resolve_lot_size("601872.SH") == 100
    assert resolve_lot_size("000938.SZ") == 100
    assert resolve_lot_size("AAPL") == 1
    assert resolve_lot_size("SPY") == 1
    assert resolve_lot_size("BIL") == 1
    # Override
    assert resolve_lot_size("601872.SH", override_lot=50) == 50


def test_resolve_trade_inertia_threshold():
    # Direct keys
    assert resolve_trade_inertia_threshold({"min_weight_change": 0.04}) == pytest.approx(0.04)
    assert resolve_trade_inertia_threshold({"inertia_threshold": 0.03}) == pytest.approx(0.03)

    # Strategy prefixes
    assert resolve_trade_inertia_threshold({"cfsb_min_weight_change": 0.05}) == pytest.approx(0.05)
    assert resolve_trade_inertia_threshold({"turtle_min_weight_change": 0.02}) == pytest.approx(0.02)
    assert resolve_trade_inertia_threshold({"aaa_min_weight_pct": 0.025}) == pytest.approx(0.025)
    assert resolve_trade_inertia_threshold({"wq_min_weight_change": 0.01}) == pytest.approx(0.01)

    # Fallback default
    assert resolve_trade_inertia_threshold({}, default=0.05) == pytest.approx(0.05)
    assert resolve_trade_inertia_threshold({"unrelated_param": 10}, default=0.08) == pytest.approx(0.08)


def test_resolve_strategy_definition_static_templates():
    # Equal weight
    s_def, s_key, s_desc, template = resolve_strategy_definition(strategy_arg="equal_weight")
    assert s_key == "equal_weight"
    assert template.name == "equal_weight"
    assert "rebalance_freq_days" in s_def["params"]

    # Alias: risk_parity -> hierarchical_risk_parity
    s_def, s_key, s_desc, template = resolve_strategy_definition(strategy_arg="risk_parity")
    assert s_key == "hierarchical_risk_parity"
    assert template.name == "hierarchical_risk_parity"
    assert "cov_lookback" in s_def["params"]


def test_resolve_strategy_definition_dumps_and_config():
    # chan_four_state_blend
    s_def, s_key, s_desc, template = resolve_strategy_definition(strategy_arg="chan_four_state_blend")
    assert s_key == "chan_four_state_blend"
    assert template.name == "chan_four_state_blend"
    assert "cfsb_min_weight_change" in s_def["params"]

    # turtle_breakout
    s_def, s_key, s_desc, template = resolve_strategy_definition(strategy_arg="turtle_breakout")
    assert s_key == "turtle_breakout"
    assert template.name == "turtle_breakout"
    assert "turtle_min_weight_change" in s_def["params"]


def test_resolve_strategy_definition_invalid():
    with pytest.raises(ValueError, match="Could not resolve strategy"):
        resolve_strategy_definition(strategy_arg="non_existent_crazy_strategy_xyz")


def test_parse_args_dynamic_output_dir(tmp_path):
    # Case 1: Custom output dir
    custom_dir = str(tmp_path / "custom_orders")
    args = parse_args(["--strategy", "turtle_breakout", "--output-dir", custom_dir])
    assert args.output_dir == os.path.abspath(custom_dir)
    assert args.account_state_file == os.path.join(os.path.abspath(custom_dir), "account_state.json")
    assert args.health_report_file == os.path.join(os.path.abspath(custom_dir), "stage1_health_report.json")
    assert args._strategy_key == "turtle_breakout"

    # Case 2: Default output dir binding based on strategy key
    args_default = parse_args(["--strategy", "equal_weight"])
    expected_default_dir = os.path.join(_PROJECT_ROOT, "docs", "ruleset", "equal_weight")
    assert args_default.output_dir == expected_default_dir
    assert args_default.account_state_file == os.path.join(expected_default_dir, "account_state.json")
    assert args_default.health_report_file == os.path.join(expected_default_dir, "stage1_health_report.json")


def test_resolve_min_entry_bars():
    # CLI override has highest priority
    assert resolve_min_entry_bars({"cfsb_min_entry_bars": 3}, cli_override=4) == 4
    # Prefixed blend params
    assert resolve_min_entry_bars({"cfsb_min_entry_bars": 2}) == 2
    assert resolve_min_entry_bars({"cfssb_min_entry_bars": 3}) == 3
    assert resolve_min_entry_bars({"crb_min_entry_bars": 4}) == 4
    assert resolve_min_entry_bars({"blend_min_entry_bars": 2}) == 2
    assert resolve_min_entry_bars({"min_entry_bars": 3}) == 3
    # Fallback default
    assert resolve_min_entry_bars({}) == 2
    assert resolve_min_entry_bars({}, default=1) == 1


def test_resolve_min_holding_bars():
    # CLI override has highest priority
    assert resolve_min_holding_bars({"cfsb_min_holding_bars": 3}, cli_override=5) == 5
    # Prefixed blend params
    assert resolve_min_holding_bars({"cfsb_min_holding_bars": 3}) == 3
    assert resolve_min_holding_bars({"cfssb_min_holding_bars": 4}) == 4
    assert resolve_min_holding_bars({"crb_min_holding_bars": 4}) == 4
    assert resolve_min_holding_bars({"blend_min_holding_bars": 2}) == 2
    assert resolve_min_holding_bars({"min_holding_bars": 3}) == 3
    # Fallback default
    assert resolve_min_holding_bars({}) == 2
    assert resolve_min_holding_bars({}, default=1) == 1


def test_resolve_holding_bars_account_state():
    account_state = {"holding_bars": {"601872.SH": 1, "600941.SH": 3}}
    holdings = {"601872.SH": 700, "600941.SH": 100, "000938.SZ": 300}
    res = resolve_holding_bars(holdings, account_state=account_state, default_bars=1)
    assert res["601872.SH"] == 1
    assert res["600941.SH"] == 3
    assert res["000938.SZ"] == 1


def test_resolve_holding_bars_history_directory(tmp_path):
    # Simulate hierarchical date folders: <run_dir>/2026-10-11/ and <run_dir>/2026-10-12/
    base_dir = tmp_path / "runs" / "test_strat" / "test_univ"
    d1 = base_dir / "2026-10-11"
    d2 = base_dir / "2026-10-12"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)

    # In 2026-10-11: 601872.SH was freshly BOUGHT (cur_shares=0, tgt_shares=700)
    ticket_csv_11 = (
        "symbol,name,action,price,current_weight,target_weight,delta_weight,current_shares,target_shares,delta_shares,trade_value,trade_value_rmb\n"
        "601872.SH,招商轮船,BUY,22.0,0.0,0.08,0.08,0,700,700,15400,15400\n"
        "601288.SH,农业银行,HOLD_FILTERED,7.0,0.1,0.1,0.0,3300,3300,0,0,0\n"
    )
    with open(d1 / "live_trading_ticket.csv", "w", encoding="utf-8") as f:
        f.write(ticket_csv_11)

    holdings_12 = {"601872.SH": 700, "601288.SH": 3300}
    res = resolve_holding_bars(holdings_12, output_dir=str(d2), as_of_date="2026-10-12")

    # 601872.SH was entered on 2026-10-11, so on 2026-10-12 it has held 1 bar
    assert res["601872.SH"] == 1
    # 601288.SH was held on 2026-10-11
    assert res["601288.SH"] >= 1


def test_apply_intraday_distribution_gate_scaling():
    """Verifies that CAUTION_INTRADAY_DISTRIBUTION scales buy increments down by intraday_buy_scale
    while leaving all sell orders and holds 100% untouched.
    """
    target = pd.Series({"SYM_SELL": 0.10, "SYM_BUY": 0.20, "SYM_ADD": 0.30, "SYM_HOLD": 0.15, "BIL": 0.25})
    current = pd.Series({"SYM_SELL": 0.20, "SYM_BUY": 0.00, "SYM_ADD": 0.10, "SYM_HOLD": 0.15, "BIL": 0.55})

    # Case 1: Normal gate_code -> no scaling applied
    normal_report = {"gate_code": "NORMAL"}
    res_normal = apply_intraday_distribution_gate(target, current, cash_proxy="BIL", health_report=normal_report)
    pd.testing.assert_series_equal(res_normal, target)

    # Case 2: CAUTION_INTRADAY_DISTRIBUTION with intraday_buy_scale = 0.50 from health_report
    dist_report = {"gate_code": "CAUTION_INTRADAY_DISTRIBUTION", "intraday_buy_scale": 0.50}
    res_dist = apply_intraday_distribution_gate(target, current, cash_proxy="BIL", health_report=dist_report)

    # SYM_SELL: current 0.20 -> target 0.10 (sell order) must be 100% UNTOUCHED
    assert res_dist["SYM_SELL"] == pytest.approx(0.10)
    # SYM_BUY: current 0.00 -> target 0.20 (buy increment +0.20) scaled by 50% -> 0.00 + 0.20 * 0.5 = 0.10
    assert res_dist["SYM_BUY"] == pytest.approx(0.10)
    # SYM_ADD: current 0.10 -> target 0.30 (buy increment +0.20) scaled by 50% -> 0.10 + 0.20 * 0.5 = 0.20
    assert res_dist["SYM_ADD"] == pytest.approx(0.20)
    # SYM_HOLD: current 0.15 -> target 0.15 (no change) remains 0.15
    assert res_dist["SYM_HOLD"] == pytest.approx(0.15)
    # BIL: cash proxy remains untouched by the asset loop
    assert res_dist["BIL"] == pytest.approx(0.25)


def test_apply_intraday_distribution_gate_cash_sweep():
    """Verifies that unallocated buying weight from intraday distribution scaling
    sweeps 100% into implied cash.
    """
    cash_proxy = "BIL"
    current = pd.Series({"SYM_SELL": 0.20, "SYM_BUY": 0.00, "SYM_ADD": 0.10, "SYM_HOLD": 0.15, cash_proxy: 0.55})
    target = pd.Series({"SYM_SELL": 0.10, "SYM_BUY": 0.20, "SYM_ADD": 0.30, "SYM_HOLD": 0.15})

    # Un-gated implied cash:
    # equity_weight_sum = 0.10 + 0.20 + 0.30 + 0.15 = 0.75 -> implied cash = 0.25
    dist_report = {"gate_code": "CAUTION_INTRADAY_DISTRIBUTION", "intraday_buy_scale": 0.50}
    gated_target = apply_intraday_distribution_gate(target, current, cash_proxy=cash_proxy, health_report=dist_report)

    # Compute implied cash weight as done in Stage 2 runner:
    equity_weight_sum = sum(float(gated_target[s]) for s in gated_target.index if s != cash_proxy)
    implied_cash_weight = max(0.0, 1.0 - equity_weight_sum)
    gated_target[cash_proxy] = implied_cash_weight

    # Gated equity sum: 0.10 + 0.10 + 0.20 + 0.15 = 0.55
    assert equity_weight_sum == pytest.approx(0.55)
    # Implied cash weight must absorb the freed 0.20 weight -> 1.0 - 0.55 = 0.45
    assert gated_target[cash_proxy] == pytest.approx(0.45)
    # Total portfolio weight must sum to 1.0
    assert sum(gated_target.values) == pytest.approx(1.0)


def test_apply_intraday_distribution_gate_account_state_fallback():
    """Verifies fallback to account_state when health_report has no gate_code."""
    current = pd.Series({"A": 0.0, "BIL": 1.0})
    target = pd.Series({"A": 0.40})
    account_state = {"gate_code": "CAUTION_INTRADAY_DISTRIBUTION", "intraday_buy_scale": 0.25}

    gated = apply_intraday_distribution_gate(target, current, cash_proxy="BIL", account_state=account_state)
    # Buy increment 0.40 scaled by 0.25 -> 0.10
    assert gated["A"] == pytest.approx(0.10)


def test_health_report_file_parsing(tmp_path):
    """Verifies that health_report_file JSON path is parsed and loaded correctly."""
    report_file = tmp_path / "stage1_health_report.json"
    data = {
        "gate_code": "CAUTION_INTRADAY_DISTRIBUTION",
        "intraday_buy_scale": 0.50,
        "circuit_breaker_tier": "TIER_1_WARNING",
    }
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    args = parse_args(["--strategy", "equal_weight", "--health-report-file", str(report_file)])
    assert os.path.exists(args.health_report_file)
    with open(args.health_report_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["gate_code"] == "CAUTION_INTRADAY_DISTRIBUTION"
    assert loaded["intraday_buy_scale"] == 0.50
    assert loaded["circuit_breaker_tier"] == "TIER_1_WARNING"


def test_resolve_exit_cooldown():
    """Verify resolve_exit_cooldown calculates elapsed trading days accurately."""
    account_state = {
        "last_exit_date": {
            "601872.SH": "2025-08-20",
            "000938.SZ": "2025-08-22",
        }
    }
    # As of 2025-08-22 (Friday)
    cooldowns = resolve_exit_cooldown(
        ["601872.SH", "000938.SZ", "NEW_SYM"],
        account_state=account_state,
        as_of_date="2025-08-22",
    )
    # 2025-08-20 -> 2025-08-22 is 2 trading days (21st, 22nd)
    assert cooldowns["601872.SH"] == 2
    # Exited on 2025-08-22 as of 2025-08-22 is 0 days elapsed
    assert cooldowns["000938.SZ"] == 0
    # Not in last_exit_date returns default 999
    assert cooldowns["NEW_SYM"] == 999


def test_parse_args_anti_whipsaw_options():
    """Verify new anti-whipsaw CLI arguments in parse_args."""
    args = parse_args([
        "--strategy", "equal_weight",
        "--start-date", "2024-01-01",
        "--entry-confirm-bars", "3",
        "--exit-confirm-bars", "3",
        "--reentry-cooldown-bars", "4",
        "--min-entry-threshold", "0.05",
        "--volume-breakout-mult", "2.0",
    ])
    assert args.start_date == "2024-01-01"
    assert args.entry_confirm_bars == 3
    assert args.exit_confirm_bars == 3
    assert args.reentry_cooldown_bars == 4
    assert args.min_entry_threshold == pytest.approx(0.05)
    assert args.volume_breakout_mult == pytest.approx(2.0)
    assert args.guard_latency_bars == 3

    # Direct --guard-latency-bars sets both entry and exit confirm bars
    args2 = parse_args([
        "--strategy", "equal_weight",
        "--guard-latency-bars", "4",
    ])
    assert args2.guard_latency_bars == 4
    assert args2.entry_confirm_bars == 4
    assert args2.exit_confirm_bars == 4


def test_exit_confirmation_streak_protection_and_hard_stop(tmp_path, monkeypatch):
    """Verifies that 2-day exit confirmation uniformly protects held positions (> 5% weight)
    on Day 1 (HOLD_FILTERED) and only triggers SELL on Day 2, while -8% hard stop loss
    immediately penetrates on Day 1.
    """
    from unittest.mock import patch
    from common.allocation_templates import EqualWeightAllocation
    from scripts.run_live_strategy import main

    univ_file = str(tmp_path / "univ.txt")
    with open(univ_file, "w", encoding="utf-8") as f:
        f.write("600000.SH\n601398.SH\n")

    holdings_file = str(tmp_path / "holdings.json")
    with open(holdings_file, "w", encoding="utf-8") as f:
        json.dump({"600000.SH": 1000, "601398.SH": 1700}, f)

    state_file = str(tmp_path / "account_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({
            "holding_bars": {"600000.SH": 5, "601398.SH": 5},
            "zero_target_streak": {},
        }, f)

    cmd_args = [
        "run_live_strategy.py",
        "--strategy", "equal_weight",
        "--universe-file", univ_file,
        "--current-holdings-file", holdings_file,
        "--account-state-file", state_file,
        "--output-dir", str(tmp_path),
        "--as-of-date", "2024-05-10",
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--exit-confirm-bars", "2",
    ]
    monkeypatch.setattr(sys, "argv", cmd_args)

    orig_gen = EqualWeightAllocation.generate_weights
    def mock_gen(self, universe, params):
        df = orig_gen(self, universe, params)
        df["601398.SH"] = 0.0
        return df

    # --- Day 1 Execution: zero_target_streak is 0 -> increments to 1 (< 2) ---
    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        main()

    with open(tmp_path / "live_trading_ticket.json", "r", encoding="utf-8") as f:
        tickets_d1 = json.load(f)

    icbc_d1 = next(t for t in tickets_d1 if t["symbol"] == "601398.SH")
    assert icbc_d1["action"] == "HOLD_FILTERED"
    assert icbc_d1["target_shares"] == 1700
    assert "平仓信号进入确认期 (第 1/2 日待确认，防单日脉冲假平仓)" in icbc_d1["reason"]

    with open(state_file, "r", encoding="utf-8") as f:
        st_d1 = json.load(f)
    assert st_d1["zero_target_streak"]["601398.SH"] == 1
    assert "lifecycle_states" in st_d1
    assert st_d1["lifecycle_states"]["601398.SH"]["state"] == "PENDING_EXIT"
    assert st_d1["lifecycle_states"]["601398.SH"]["exit_streak"] == 1

    # --- Day 2 Execution: zero_target_streak is 1 -> increments to 2 (>= 2) ---
    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        main()

    with open(tmp_path / "live_trading_ticket.json", "r", encoding="utf-8") as f:
        tickets_d2 = json.load(f)

    icbc_d2 = next(t for t in tickets_d2 if t["symbol"] == "601398.SH")
    assert icbc_d2["action"] == "SELL"
    assert icbc_d2["target_shares"] == 0
    assert icbc_d2["delta_shares"] == -1700
    assert "策略模型平仓信号持续 2 日确认生效" in icbc_d2["reason"]

    with open(state_file, "r", encoding="utf-8") as f:
        st_d2 = json.load(f)
    assert "601398.SH" not in st_d2["zero_target_streak"]
    assert "lifecycle_states" in st_d2
    assert st_d2["lifecycle_states"]["601398.SH"]["state"] == "COOLDOWN"
    assert st_d2["lifecycle_states"]["601398.SH"]["last_exit_type"] == "NORMAL"

    # --- Hard Stop Scenario: -8% loss penetrates confirmation immediately on Day 1 ---
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({
            "holding_bars": {"601398.SH": 5},
            "entry_prices": {"601398.SH": 120.0},  # Price is ~96.6 -> -19.5% loss
            "zero_target_streak": {},
        }, f)

    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        main()

    with open(tmp_path / "live_trading_ticket.json", "r", encoding="utf-8") as f:
        tickets_stop = json.load(f)

    icbc_stop = next(t for t in tickets_stop if t["symbol"] == "601398.SH")
    assert icbc_stop["action"] == "SELL"
    assert icbc_stop["target_shares"] == 0
    assert "触发个股 -8% 硬止损风控" in icbc_stop["reason"]





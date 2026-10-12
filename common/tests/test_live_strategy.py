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
    resolve_strategy_definition,
    resolve_holdings_weights_and_shares,
    load_symbol_names,
    parse_args,
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

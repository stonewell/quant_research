"""Regression tests for run_fundamental_screener.py's CLI entry point.

All fundamentals are mocked -- this project's live yfinance dependency is
inherent to its purpose, but automated tests must never rely on the
network, matching this workspace's testing conventions."""

import json
import os
import sys
from unittest.mock import patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fundamental_screener import run_fundamental_screener as rfs


def test_build_arg_parser_defaults():
    args = rfs.build_arg_parser().parse_args([])
    assert args.benchmark == "SPY"
    assert args.top_n == 5
    assert args.required_return == pytest.approx(0.12)
    assert args.data_provider == "synthetic"


def test_build_config_from_args_resolves_universe_and_thresholds():
    args = rfs.build_arg_parser().parse_args([
        "--universe", "KO", "PG", "--benchmark", "SPY", "--top-n", "3", "--min-roe", "0.20",
    ])
    cfg = rfs.build_config_from_args(args)
    assert cfg.universe == ["KO", "PG"]
    assert cfg.benchmark_symbol == "SPY"
    assert cfg.top_n == 3
    assert cfg.min_roe == pytest.approx(0.20)


GOOD_META = {"roe": 0.35, "dividend_yield": 0.03, "earnings_growth": 0.10, "debt_to_equity": 50.0}
BAD_META = {"roe": 0.05, "dividend_yield": 0.01, "earnings_growth": 0.01, "debt_to_equity": 200.0}


def _fake_metadata(symbol, provider=None, **kwargs):
    table = {"KO": GOOD_META, "PG": BAD_META}
    return table.get(symbol, {
        "roe": float("nan"), "dividend_yield": float("nan"),
        "earnings_growth": float("nan"), "debt_to_equity": float("nan"),
    })


@patch("fscreen.fundamentals.fetch_fund_metadata", side_effect=_fake_metadata)
def test_main_writes_report_and_backtester_compatible_strategy_file(mock_fetch, tmp_path, monkeypatch):
    monkeypatch.setattr(rfs, "RESULTS_DIR", str(tmp_path))
    argv = [
        "run_fundamental_screener.py",
        "--universe", "KO", "PG",
        "--benchmark", "SPY",
        "--data-provider", "synthetic",
        "--seed", "3",
        "--start", "2015-01-01", "--end", "2024-12-31",
        "--top-n", "5",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rfs.main()  # must not raise, must not touch the network (fetch_fund_metadata is mocked)

    report_path = tmp_path / "fundamental_screen_report.json"
    assert report_path.exists()
    with open(report_path) as f:
        report = json.load(f)

    assert report["n_universe_evaluated"] == 2
    buy_symbols = {row["symbol"] for row in report["top_buy"]}
    sell_symbols = {row["symbol"] for row in report["top_sell"]}
    assert "KO" in buy_symbols
    assert "PG" in sell_symbols
    assert not (buy_symbols & sell_symbols), "overlap must always be resolved (sell wins)"

    strategy_path = tmp_path / "fundamental_strategy.json"
    assert strategy_path.exists()
    with open(strategy_path) as f:
        strategy_def = json.load(f)
    assert strategy_def["template_name"] == "fundamental_margin_of_safety"
    assert strategy_def["fundamental_spec"] == {"source": "fundamental_screener"}
    assert strategy_def["params"]["universe"] == ["KO", "PG"]

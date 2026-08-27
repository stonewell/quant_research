"""Regression tests for run_bnn_forecaster.py's CLI entry point.

`fit_forecast` is mocked everywhere here -- a real fit costs several seconds
per symbol even at reduced settings (JAX JIT compilation dominates), and its
actual calibration quality is a separate, disclosed-as-unverified concern
(see bnnf/forecasting.py's own docstring) that CLI-wiring tests shouldn't
depend on.
"""

import json
import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_BNN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BNN_ROOT not in sys.path:
    sys.path.insert(0, _BNN_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import run_bnn_forecaster as rbf


def test_build_arg_parser_defaults():
    args = rbf.build_arg_parser().parse_args([])
    assert args.benchmark == "SPY"
    assert args.top_n == 5
    assert args.estimator == "map"
    assert args.data_provider == "synthetic"


def test_build_config_from_args_resolves_universe_and_thresholds():
    args = rbf.build_arg_parser().parse_args([
        "--universe", "KO", "PG", "--benchmark", "SPY", "--top-n", "3", "--required-return", "0.20",
    ])
    cfg = rbf.build_config_from_args(args)
    assert cfg.universe == ["KO", "PG"]
    assert cfg.top_n == 3
    assert cfg.required_return == pytest.approx(0.20)


GOOD = (0.30, 0.10)   # (forecast_return, ci_width) -- confident, clears the hurdle
BAD = (0.01, 0.10)    # confident but below both the hurdle and the benchmark


@patch("run_bnn_forecaster.fit_forecast")
def test_main_writes_report_and_backtester_compatible_strategy_file(mock_fit, tmp_path, monkeypatch):
    monkeypatch.setattr(rbf, "RESULTS_DIR", str(tmp_path))

    # Return GOOD for KO, BAD for PG, and a modest positive forecast for the
    # benchmark (SPY) that KO clears and PG doesn't -- matched by call order
    # (main() fits the benchmark first, then each candidate symbol in order).
    call_sequence = [(0.05, 0.05)] + [GOOD, BAD]  # SPY, KO, PG
    def _side_effect(close, cfg):
        forecast_return, ci_width = call_sequence.pop(0)
        return pd.DataFrame({"forecast_return": forecast_return, "ci_width": ci_width}, index=close.index)
    mock_fit.side_effect = _side_effect

    argv = [
        "run_bnn_forecaster.py",
        "--universe", "KO", "PG",
        "--benchmark", "SPY",
        "--data-provider", "synthetic",
        "--seed", "3",
        "--start", "2015-01-01", "--end", "2018-12-31",
        "--top-n", "5",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rbf.main()  # must not raise, must not run a real (slow) fit (fit_forecast is mocked)

    report_path = tmp_path / "bnn_forecast_report.json"
    assert report_path.exists()
    with open(report_path) as f:
        report = json.load(f)

    assert report["n_universe_evaluated"] == 2
    buy_symbols = {row["symbol"] for row in report["top_buy"]}
    sell_symbols = {row["symbol"] for row in report["top_sell"]}
    assert "KO" in buy_symbols
    assert "PG" in sell_symbols
    assert not (buy_symbols & sell_symbols), "overlap must always be resolved (sell wins)"

    strategy_path = tmp_path / "bnn_strategy.json"
    assert strategy_path.exists()
    with open(strategy_path) as f:
        strategy_def = json.load(f)
    assert strategy_def["template_name"] == "bnn_forecast"
    assert strategy_def["bnn_spec"] == {"source": "bnn_forecaster"}
    assert strategy_def["params"]["universe"] == ["KO", "PG"]

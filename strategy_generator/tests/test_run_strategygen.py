import json
import os
import sys
import tempfile
from unittest.mock import patch

import pandas as pd

# Add strategy_generator to path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from run_strategygen import _load_factor_report, build_arg_parser, main


def test_build_arg_parser_universe_options():
    parser = build_arg_parser()

    # Should parse fine with just universe
    args = parser.parse_args(["--universe", "A", "B"])
    assert args.universe == ["A", "B"]
    assert args.universe_file is None

    # Should parse fine with just universe-file
    args = parser.parse_args(["--universe-file", "basket.json"])
    assert args.universe_file == "basket.json"


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_main_loads_universe_from_file(mock_gen_cls, mock_load):
    # Mock the generator to avoid running real backtests
    mock_gen_instance = mock_gen_cls.return_value

    class MockSpec:
        n_symbols = 2
        template_name = "test"
        params = {}
        universe_sharpe = 1.0
        cagr = 0.10
        max_drawdown = -0.05
        calmar_ratio = 2.0
        win_rate = 0.55
        profit_factor = 1.5
        total_turnover = 1.0
        total_rebalances = 1
        ers_passed = True
        ers_percentile = 0.99
        trusted = True
        explanation = "test"
        target_weights = pd.DataFrame()
        factor_context = None
        factor_tiebreak_used = False
        equity_curve = pd.DataFrame(
            {"equity": [100.0, 101.0, 102.0]}, index=pd.bdate_range("2020-01-01", periods=3)
        )

    mock_gen_instance.generate.return_value = MockSpec()

    # Create a temporary JSON file
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        json.dump({
            "basket": ["TICKER1", "TICKER2"],
            "method": "test",
            "date_generated": "2026-07-21T10:00:00Z"
        }, f)
        temp_path = f.name

    try:
        # Patch sys.argv to simulate command line
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate"]
        with patch.object(sys, "argv", test_args):
            main()

        # Verify load_universe_with_banner was called once with the full
        # symbol list from the JSON file (batch load, not per-symbol).
        assert mock_load.call_count == 1
        call_args = mock_load.call_args
        assert call_args[0][0] == ["TICKER1", "TICKER2"]

    finally:
        os.remove(temp_path)


@patch("run_strategygen.plotting.plot_equity_curve")
@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_main_writes_equity_curve_chart_by_default(mock_gen_cls, mock_load, mock_plot):
    mock_gen_instance = mock_gen_cls.return_value

    class MockSpec:
        n_symbols = 2
        template_name = "test"
        params = {}
        universe_sharpe = 1.0
        cagr = 0.10
        max_drawdown = -0.05
        calmar_ratio = 2.0
        win_rate = 0.55
        profit_factor = 1.5
        total_turnover = 1.0
        total_rebalances = 1
        ers_passed = True
        ers_percentile = 0.99
        trusted = True
        explanation = "test"
        target_weights = pd.DataFrame()
        factor_context = None
        factor_tiebreak_used = False
        equity_curve = pd.DataFrame(
            {"equity": [100.0, 101.0, 102.0]}, index=pd.bdate_range("2020-01-01", periods=3)
        )

    mock_spec = MockSpec()
    mock_gen_instance.generate.return_value = mock_spec
    mock_plot.return_value = "/fake/results/equity_curve.png"

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        json.dump({
            "basket": ["TICKER1", "TICKER2"],
            "method": "test",
            "date_generated": "2026-07-21T10:00:00Z"
        }, f)
        temp_path = f.name

    try:
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate"]
        with patch.object(sys, "argv", test_args):
            main()

        assert mock_plot.call_count == 1
        call_args = mock_plot.call_args
        pd.testing.assert_series_equal(call_args[0][0], mock_spec.equity_curve["equity"])
    finally:
        os.remove(temp_path)


@patch("run_strategygen.plotting.plot_equity_curve")
@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_main_no_plots_skips_equity_curve_chart(mock_gen_cls, mock_load, mock_plot):
    mock_gen_instance = mock_gen_cls.return_value

    class MockSpec:
        n_symbols = 2
        template_name = "test"
        params = {}
        universe_sharpe = 1.0
        cagr = 0.10
        max_drawdown = -0.05
        calmar_ratio = 2.0
        win_rate = 0.55
        profit_factor = 1.5
        total_turnover = 1.0
        total_rebalances = 1
        ers_passed = True
        ers_percentile = 0.99
        trusted = True
        explanation = "test"
        target_weights = pd.DataFrame()
        factor_context = None
        factor_tiebreak_used = False
        equity_curve = pd.DataFrame(
            {"equity": [100.0, 101.0, 102.0]}, index=pd.bdate_range("2020-01-01", periods=3)
        )

    mock_gen_instance.generate.return_value = MockSpec()

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        json.dump({
            "basket": ["TICKER1", "TICKER2"],
            "method": "test",
            "date_generated": "2026-07-21T10:00:00Z"
        }, f)
        temp_path = f.name

    try:
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate", "--no-plots"]
        with patch.object(sys, "argv", test_args):
            main()

        assert mock_plot.call_count == 0
    finally:
        os.remove(temp_path)


def test_load_factor_report_missing_key_raises_clear_error(tmp_path):
    path = tmp_path / "bad_factor_report.json"
    path.write_text(json.dumps({"not_factor_performance": {}}))

    with pytest.raises(ValueError, match="factor_performance"):
        _load_factor_report(str(path))


def test_load_factor_report_wrong_type_raises_clear_error(tmp_path):
    path = tmp_path / "bad_factor_report.json"
    path.write_text(json.dumps({"factor_performance": "not_an_object"}))

    with pytest.raises(ValueError, match="must be a JSON object"):
        _load_factor_report(str(path))


def test_load_factor_report_valid(tmp_path):
    path = tmp_path / "factor_report.json"
    path.write_text(json.dumps({"factor_performance": {"breadth": {"mean_sharpe_ratio": 0.5}}}))

    report = _load_factor_report(str(path))
    assert report["factor_performance"]["breadth"]["mean_sharpe_ratio"] == 0.5

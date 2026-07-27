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

from run_strategygen import build_arg_parser, main


def test_build_arg_parser_universe_options():
    parser = build_arg_parser()

    # Should parse fine with just universe
    args = parser.parse_args(["--universe", "A", "B"])
    assert args.universe == ["A", "B"]
    assert args.universe_file is None

    # Should parse fine with just universe-file
    args = parser.parse_args(["--universe-file", "basket.json"])
    assert args.universe_file == "basket.json"


@patch("run_strategygen.load_ohlcv")
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

        # Verify load_ohlcv was called with the tickers from the JSON file
        assert mock_load.call_count == 2
        calls = mock_load.call_args_list
        assert calls[0][0][0] == "TICKER1"
        assert calls[1][0][0] == "TICKER2"

    finally:
        os.remove(temp_path)

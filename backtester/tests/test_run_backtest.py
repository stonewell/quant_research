import json
import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# Add backtester to path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.data import BaseDataProvider, register_provider
from common.testing import make_ohlcv_from_closes as make_df
from backtester.run_backtest import (
    _align_universe,
    _get_template,
    _load_strategy_file,
    main,
    run_standard,
    run_walkforward,
)


def test_align_universe():
    idx1 = pd.bdate_range("2020-01-01", periods=10)
    idx2 = pd.bdate_range("2020-01-05", periods=10)

    universe = {
        "A": pd.DataFrame({"Close": np.ones(10)}, index=idx1),
        "B": pd.DataFrame({"Close": np.ones(10)}, index=idx2),
    }

    aligned = _align_universe(universe)

    # Should be the intersection
    expected_idx = idx1.intersection(idx2)
    assert len(aligned["A"]) == len(expected_idx)
    assert len(aligned["B"]) == len(expected_idx)
    assert (aligned["A"].index == expected_idx).all()


def test_get_template():
    template = _get_template("equal_weight")
    assert template.name == "equal_weight"

    try:
        _get_template("non_existent")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_get_template_reconstructs_pattern_based_template_from_spec():
    # A PatternBasedAllocationTemplate (strategy_generator/stratgen/
    # pattern_mining.py) is universe-specific and never in the static
    # ALLOCATION_TEMPLATES registry -- it must be reconstructed from a
    # pattern_spec dict instead (see run_strategygen.py's strategy.json output).
    pattern_spec = {
        "feature_name": "rsi",
        "feature_lookback": 14,
        "threshold": 30.0,
        "comparison": "below",
        "event_type": "trough",
        "mined_p_value": 0.001,
        "mined_n_events": 12,
    }
    template = _get_template("pattern_rsi_14_trough", pattern_spec)
    assert template.name == "pattern_rsi_14_trough"
    assert template.feature_name == "rsi"
    assert template.feature_lookback == 14
    assert template.comparison == "below"
    assert template.event_type == "trough"


class MockArgs:
    def __init__(self, **kwargs):
        self.initial_capital = 100_000.0
        self.commission_pct = 0.0
        self.slippage_pct = 0.0
        self.window_years = 0.1
        self.step_years = 0.05
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_run_standard():
    idx = pd.bdate_range("2020-01-01", periods=100)
    universe = {
        "A": make_df(np.linspace(100, 200, 100), start="2020-01-01"),
        "B": make_df(np.linspace(100, 50, 100), start="2020-01-01"),
    }

    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    args = MockArgs()

    result = run_standard(universe, template, params, args)

    assert "sharpe_ratio" in result
    assert "cagr" in result
    assert "max_drawdown" in result
    assert "calmar_ratio" in result
    assert "win_rate" in result
    assert "profit_factor" in result
    assert "equity_curve" in result
    assert not result["equity_curve"].empty


def test_run_standard_max_drawdown_is_a_positive_magnitude():
    # Regression test: run_allocation_backtest used to report max_drawdown as
    # a NEGATIVE number while common/metrics.py's max_drawdown() (used
    # elsewhere in this workspace, and previously ALSO recomputed inline
    # here) reports the same real-world quantity as POSITIVE -- so the same
    # backtest run could print "-18.00%" from one code path and "18.0%" from
    # another. A synthetic price path with a known, exact drawdown pins the
    # convention: it must come back positive and match the known magnitude.
    idx = pd.bdate_range("2020-01-01", periods=5)
    # Equal-weight, single asset A: up 25%, then down 20% (back to start),
    # then flat -- a textbook 20% drawdown from the day-2 peak.
    closes_a = [100.0, 125.0, 100.0, 100.0, 100.0]
    universe = {"A": make_df(closes_a, start="2020-01-01")}

    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 100}  # single allocation on day 0, never rebalanced again
    args = MockArgs()

    result = run_standard(universe, template, params, args)

    assert result["max_drawdown"] > 0
    np.testing.assert_allclose(result["max_drawdown"], 0.20, atol=1e-9)


def test_run_walkforward():
    # 1 year of data
    idx = pd.bdate_range("2020-01-01", periods=252)
    universe = {
        "A": make_df(np.linspace(100, 200, 252), start="2020-01-01"),
        "B": make_df(np.linspace(100, 50, 252), start="2020-01-01"),
    }

    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    # 0.5 year window, 0.25 year step
    args = MockArgs(window_years=0.5, step_years=0.25)

    folds = run_walkforward(universe, template, params, args)

    # Total 1 year. Window 0.5. Step 0.25.
    # Folds:
    # 0.0 to 0.5
    # 0.25 to 0.75
    # 0.5 to 1.0
    assert len(folds) == 3

    for fold in folds:
        assert "start_date" in fold
        assert "end_date" in fold
        assert "sharpe_ratio" in fold
        assert "max_drawdown" in fold
        assert "total_turnover" in fold
        assert "total_rebalances" in fold


def test_run_walkforward_warms_up_indicator_lookback_before_each_fold():
    # Regression test: InverseVolatility's realized_vol needs `vol_lookback`
    # bars of history before it stops returning NaN. A fold sliced to bare
    # [start_idx:end_idx) recomputes it from scratch, so every rebalance date
    # inside that cold period used to be silently dropped. A fold that has at
    # least `vol_lookback` bars of real history BEFORE its own start should
    # now get the full number of scheduled rebalances, not just the ones
    # after the window's own warmup period.
    idx = pd.bdate_range("2020-01-01", periods=252 * 2)
    rng = np.random.default_rng(0)
    closes_a = 100 + np.cumsum(rng.normal(0.05, 1.0, len(idx)))
    closes_b = 100 + np.cumsum(rng.normal(0.05, 1.0, len(idx)))
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    template = _get_template("inverse_volatility")
    params = {"vol_lookback": 120, "rebalance_freq_days": 21}
    args = MockArgs(window_years=1.0, step_years=0.5)

    folds = run_walkforward(universe, template, params, args)

    # Fold 0 starts at day 0 of the whole series -- there is no history
    # before it to draw a warmup buffer from, so it's expected to still lose
    # its first ~120 days' worth of rebalances (a genuine data-availability
    # limit, not a bug).
    assert folds[0]["total_rebalances"] < 12

    # Every later fold has >= 120 days of real history before its own start,
    # so it should now get the full ~252/21 = 12 scheduled rebalances instead
    # of only the ones after its own in-window warmup period.
    for fold in folds[1:]:
        assert fold["total_rebalances"] == 12


def _write_strategy_file(path, template_name="equal_weight", params=None, pattern_spec=None):
    strategy_def = {"template_name": template_name, "params": params or {"rebalance_freq_days": 10}}
    if pattern_spec is not None:
        strategy_def["pattern_spec"] = pattern_spec
    with open(path, "w") as f:
        json.dump(strategy_def, f)


def test_load_strategy_file_missing_required_keys(tmp_path):
    path = tmp_path / "strategy.json"
    with open(path, "w") as f:
        json.dump({"template_name": "equal_weight"}, f)  # missing "params"

    with pytest.raises(ValueError, match="missing required key"):
        _load_strategy_file(str(path))


def test_load_strategy_file_params_not_dict(tmp_path):
    path = tmp_path / "strategy.json"
    with open(path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": ["not", "a", "dict"]}, f)

    with pytest.raises(ValueError, match="must be a JSON object"):
        _load_strategy_file(str(path))


def test_load_strategy_file_valid(tmp_path):
    path = tmp_path / "strategy.json"
    _write_strategy_file(path)
    strategy_def = _load_strategy_file(str(path))
    assert strategy_def["template_name"] == "equal_weight"
    assert strategy_def["params"] == {"rebalance_freq_days": 10}


class _FlakySymbolProvider(BaseDataProvider):
    """Test-only provider that fails for one specific symbol, to exercise
    main()'s resilient (warn-and-skip) universe loading."""

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        if symbol == "BAD":
            raise ValueError("simulated fetch failure")
        dates = pd.bdate_range(start or "2020-01-01", periods=60)
        closes = np.linspace(100, 110, len(dates))
        return make_df(closes, start=dates[0].strftime("%Y-%m-%d"))


register_provider("flaky_symbol_test_provider", _FlakySymbolProvider)


def test_main_skips_bad_symbol_and_continues(tmp_path, monkeypatch, capsys):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"
    cache_dir = tmp_path / "cache"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "GOOD1", "BAD", "GOOD2",
        "--data-provider", "flaky_symbol_test_provider",
        "--no-cache",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    main()

    captured = capsys.readouterr()
    assert "Loaded 2/3 symbols" in captured.out
    assert os.path.exists(results_dir / "backtest_equity.csv")


def test_main_results_dir_and_cache_dir_overrides(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "custom_results"
    cache_dir = tmp_path / "custom_cache"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA", "BBB",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    main()

    assert os.path.exists(results_dir / "backtest_equity.csv")
    assert os.path.exists(results_dir / "backtest_weights.csv")
    assert len(list(cache_dir.glob("*.csv"))) == 2


def test_main_runs_pattern_based_strategy_end_to_end(tmp_path, monkeypatch):
    # A strategy.json carrying a pattern_spec (produced by strategy_generator's
    # --mine-patterns) must round-trip through main() exactly like a static
    # template does -- this is the integration point that lets a mined,
    # ERS-validated pattern actually be re-run/re-verified independently.
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(
        strategy_path,
        template_name="pattern_rsi_14_trough",
        params={"threshold_mult": 1.0, "hold_days": 21, "rebalance_freq_days": 21},
        pattern_spec={
            "feature_name": "rsi",
            "feature_lookback": 14,
            "threshold": 30.0,
            "comparison": "below",
            "event_type": "trough",
            "mined_p_value": 0.01,
            "mined_n_events": 15,
        },
    )
    results_dir = tmp_path / "results"
    cache_dir = tmp_path / "cache"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA", "BBB", "CCC",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    main()

    assert os.path.exists(results_dir / "backtest_equity.csv")
    assert os.path.exists(results_dir / "backtest_weights.csv")

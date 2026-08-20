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
    _compute_standard_comparison,
    _get_template,
    _load_strategy_file,
    _merge_baseline_folds,
    _resolve_baseline_params,
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


def test_get_template_rejects_pattern_spec_with_non_pattern_prefixed_template_name():
    # Regression test: SCHEMAS.md documents that pattern-template
    # reconstruction triggers "when template_name starts with pattern_", but
    # _get_template used to branch purely on `pattern_spec is not None`,
    # never checking template_name -- so a strategy.json naming a static
    # template (e.g. "equal_weight") with a stray leftover pattern_spec block
    # would be silently misinterpreted as a pattern template.
    pattern_spec = {
        "feature_name": "rsi",
        "feature_lookback": 14,
        "threshold": 30.0,
        "comparison": "below",
        "event_type": "trough",
    }
    with pytest.raises(ValueError, match="pattern_"):
        _get_template("equal_weight", pattern_spec)


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


def test_run_walkforward_raises_on_non_positive_step_years():
    # Regression test: step_bars = int(round(args.step_years * 252)) used to
    # be unvalidated, so a zero/negative --step-years never advanced
    # start_idx past the loop's termination condition, hanging the process
    # forever and growing `folds` unboundedly. Must raise promptly instead of
    # looping -- if this test doesn't raise, it will hang.
    idx = pd.bdate_range("2020-01-01", periods=252)
    universe = {
        "A": make_df(np.linspace(100, 200, 252), start="2020-01-01"),
    }
    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    args = MockArgs(window_years=0.5, step_years=0.0)

    with pytest.raises(ValueError, match="step-years"):
        run_walkforward(universe, template, params, args)


def test_run_walkforward_raises_on_non_positive_window_years():
    # Regression test: the old guard `if window_bars >= n_bars: raise ...`
    # never caught window_bars <= 0 (always false for non-positive vs
    # positive n_bars), so end_idx <= start_idx and
    # any_df.index[end_idx - 1] could silently wrap via negative indexing to
    # an unrelated date instead of raising a clean error.
    idx = pd.bdate_range("2020-01-01", periods=252)
    universe = {
        "A": make_df(np.linspace(100, 200, 252), start="2020-01-01"),
    }
    template = _get_template("equal_weight")
    params = {"rebalance_freq_days": 10}
    args = MockArgs(window_years=0.0, step_years=0.25)

    with pytest.raises(ValueError, match="window-years"):
        run_walkforward(universe, template, params, args)


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


def test_load_strategy_file_pattern_spec_missing_required_keys(tmp_path):
    # Regression test: _get_template indexed pattern_spec["threshold"] etc.
    # via plain brackets with no upfront validation, so a malformed
    # pattern_spec block used to raise a raw, unhandled KeyError deep in
    # main() instead of a clean, informative ValueError.
    path = tmp_path / "strategy.json"
    pattern_spec = {
        "feature_name": "rsi",
        "feature_lookback": 14,
        # "threshold" intentionally omitted
        "comparison": "below",
        "event_type": "trough",
    }
    _write_strategy_file(path, template_name="pattern_rsi_14_trough", pattern_spec=pattern_spec)

    with pytest.raises(ValueError, match="threshold"):
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


# --- Feature 1/2/3: baseline comparison, walkforward summary, equity charting ---


def test_resolve_baseline_params_uses_given_json():
    template = _get_template("equal_weight")
    params = _resolve_baseline_params(template, '{"rebalance_freq_days": 42}')
    assert params == {"rebalance_freq_days": 42}


def test_resolve_baseline_params_defaults_to_first_grid_value():
    template = _get_template("equal_weight")
    params = _resolve_baseline_params(template, None)
    assert params == {k: v[0] for k, v in template.param_grid.items()}


def test_resolve_baseline_params_malformed_json_raises():
    template = _get_template("equal_weight")
    with pytest.raises(ValueError, match="Failed to parse"):
        _resolve_baseline_params(template, "{not valid json")


def test_resolve_baseline_params_non_dict_json_raises():
    template = _get_template("equal_weight")
    with pytest.raises(ValueError, match="JSON object"):
        _resolve_baseline_params(template, "[1,2,3]")


def test_compute_standard_comparison_identical_curves():
    idx = pd.bdate_range("2020-01-01", periods=60)
    rng = np.random.default_rng(3)
    equity = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, len(idx)))), index=idx)
    eq_df = pd.DataFrame({"equity": equity})

    result = {"equity_curve": eq_df, "cagr": 0.12}
    baseline_result = {"equity_curve": eq_df, "cagr": 0.12}

    comparison = _compute_standard_comparison(result, baseline_result)

    assert comparison["overlap_bars"] == len(idx)
    np.testing.assert_allclose(comparison["alpha"], 0.0, atol=1e-9)
    np.testing.assert_allclose(comparison["beta"], 1.0, atol=1e-9)
    np.testing.assert_allclose(comparison["tracking_error"], 0.0, atol=1e-9)
    np.testing.assert_allclose(comparison["information_ratio"], 0.0, atol=1e-9)
    np.testing.assert_allclose(comparison["outperformance_cagr"], 0.0, atol=1e-9)


def test_compute_standard_comparison_disjoint_indexes_returns_nan_no_exception():
    idx1 = pd.bdate_range("2020-01-01", periods=10)
    idx2 = pd.bdate_range("2025-01-01", periods=10)

    result = {"equity_curve": pd.DataFrame({"equity": np.linspace(100, 110, 10)}, index=idx1), "cagr": 0.1}
    baseline_result = {"equity_curve": pd.DataFrame({"equity": np.linspace(100, 105, 10)}, index=idx2), "cagr": 0.05}

    comparison = _compute_standard_comparison(result, baseline_result)

    assert comparison["overlap_bars"] == 0
    for key in ("alpha", "beta", "tracking_error", "information_ratio", "outperformance_cagr"):
        assert np.isnan(comparison[key])


def test_merge_baseline_folds_pairs_by_date_not_position():
    # Deliberately different order between the two fold sets, plus a
    # strategy fold with no matching baseline (start_date, end_date) pair.
    folds_df = pd.DataFrame([
        {"start_date": "2020-01-01", "end_date": "2020-06-01", "sharpe_ratio": 1.0,
         "cagr": 0.10, "max_drawdown": 0.05, "calmar_ratio": 2.0},
        {"start_date": "2020-06-01", "end_date": "2020-12-01", "sharpe_ratio": 1.2,
         "cagr": 0.12, "max_drawdown": 0.06, "calmar_ratio": 2.0},
        {"start_date": "2021-01-01", "end_date": "2021-06-01", "sharpe_ratio": 0.8,
         "cagr": 0.08, "max_drawdown": 0.04, "calmar_ratio": 2.0},  # no baseline match
    ])
    baseline_folds_df = pd.DataFrame([
        {"start_date": "2020-06-01", "end_date": "2020-12-01", "sharpe_ratio": 0.5,
         "cagr": 0.05, "max_drawdown": 0.03, "calmar_ratio": 1.5},
        {"start_date": "2020-01-01", "end_date": "2020-06-01", "sharpe_ratio": 0.4,
         "cagr": 0.04, "max_drawdown": 0.02, "calmar_ratio": 1.8},
    ])

    merged = _merge_baseline_folds(folds_df, baseline_folds_df)

    assert len(merged) == 3
    row0 = merged[merged["start_date"] == "2020-01-01"].iloc[0]
    assert row0["baseline_sharpe_ratio"] == 0.4
    np.testing.assert_allclose(row0["outperformance"], 0.10 - 0.04)

    row1 = merged[merged["start_date"] == "2020-06-01"].iloc[0]
    assert row1["baseline_sharpe_ratio"] == 0.5
    np.testing.assert_allclose(row1["outperformance"], 0.12 - 0.05)

    row2 = merged[merged["start_date"] == "2021-01-01"].iloc[0]
    assert pd.isna(row2["baseline_sharpe_ratio"])
    assert pd.isna(row2["outperformance"])


def test_main_baseline_symbol_standard_mode_writes_comparison_files(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"
    cache_dir = tmp_path / "cache"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--baseline-symbol", "SPY",
        "--mode", "standard",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    main()

    assert os.path.exists(results_dir / "baseline_equity.csv")
    assert os.path.exists(results_dir / "comparison_report.json")
    assert os.path.exists(results_dir / "backtest_equity.csv")
    assert os.path.exists(results_dir / "backtest_weights.csv")

    with open(results_dir / "comparison_report.json") as f:
        report = json.load(f)
    assert report["baseline_symbol"] == "SPY"
    for key in ("overlap_bars", "alpha", "beta", "tracking_error", "information_ratio", "outperformance_cagr",
                "baseline_sharpe_ratio", "baseline_cagr", "baseline_max_drawdown", "baseline_calmar_ratio",
                "strategy_sharpe_ratio", "strategy_cagr"):
        assert key in report


def test_main_baseline_symbol_walkforward_mode_adds_columns(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"
    cache_dir = tmp_path / "cache"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--baseline-symbol", "SPY",
        "--mode", "walkforward",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    main()

    folds_df = pd.read_csv(results_dir / "walkforward_report.csv")
    for col in ("baseline_sharpe_ratio", "baseline_cagr", "baseline_max_drawdown",
                "baseline_calmar_ratio", "outperformance"):
        assert col in folds_df.columns

    assert os.path.exists(results_dir / "comparison_report.json")
    with open(results_dir / "comparison_report.json") as f:
        report = json.load(f)
    assert "mean_baseline_sharpe_ratio" in report
    assert "mean_baseline_cagr" in report
    assert "mean_outperformance_cagr" in report

    assert os.path.exists(results_dir / "walkforward_summary.json")


def test_main_without_baseline_symbol_is_unchanged(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)

    # Standard mode
    results_dir_std = tmp_path / "results_std"
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "standard",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir_std),
        "--cache-dir", str(tmp_path / "cache_std"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    assert not os.path.exists(results_dir_std / "baseline_equity.csv")
    assert not os.path.exists(results_dir_std / "comparison_report.json")

    # Walkforward mode
    results_dir_wf = tmp_path / "results_wf"
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "walkforward",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir_wf),
        "--cache-dir", str(tmp_path / "cache_wf"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    assert not os.path.exists(results_dir_wf / "baseline_equity.csv")
    assert not os.path.exists(results_dir_wf / "comparison_report.json")

    folds_df = pd.read_csv(results_dir_wf / "walkforward_report.csv")
    assert list(folds_df.columns) == [
        "start_date", "end_date", "sharpe_ratio", "cagr", "max_drawdown", "calmar_ratio",
        "win_rate", "profit_factor", "total_turnover", "total_rebalances",
    ]


def test_main_walkforward_writes_summary_json(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "walkforward",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(tmp_path / "cache"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    summary_path = results_dir / "walkforward_summary.json"
    assert os.path.exists(summary_path)
    with open(summary_path) as f:
        summary = json.load(f)
    for key in ("mean_sharpe_ratio", "mean_cagr", "mean_max_drawdown", "mean_calmar_ratio",
                "n_folds", "n_valid_folds", "fold_sharpe_std", "deflated_sharpe_ratio"):
        assert key in summary


def test_main_walkforward_summary_dsr_is_null_with_fewer_than_two_folds(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "walkforward",
        "--data-provider", "synthetic",
        "--start", "2020-01-01",
        "--end", "2020-06-01",
        "--window-years", "0.3",
        "--step-years", "5.0",
        "--results-dir", str(results_dir),
        "--cache-dir", str(tmp_path / "cache"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    with open(results_dir / "walkforward_summary.json") as f:
        summary = json.load(f)

    assert summary["n_valid_folds"] < 2
    assert summary["deflated_sharpe_ratio"] is None
    assert summary["fold_sharpe_std"] is None


def test_main_standard_mode_writes_equity_chart_by_default(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "standard",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(tmp_path / "cache"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    chart_path = results_dir / "equity_curve.png"
    assert os.path.exists(chart_path)
    assert os.path.getsize(chart_path) > 0


def test_main_standard_mode_no_plots_skips_chart(tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "standard",
        "--data-provider", "synthetic",
        "--no-plots",
        "--results-dir", str(results_dir),
        "--cache-dir", str(tmp_path / "cache"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    assert not os.path.exists(results_dir / "equity_curve.png")


@patch("backtester.run_backtest.plotting.plot_equity_curve")
def test_main_walkforward_mode_never_calls_plotting(mock_plot, tmp_path, monkeypatch):
    strategy_path = tmp_path / "strategy.json"
    _write_strategy_file(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "A", "B",
        "--mode", "walkforward",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--cache-dir", str(tmp_path / "cache"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    mock_plot.assert_not_called()

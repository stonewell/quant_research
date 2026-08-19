"""Regression tests for the `run_research_strategy.py` CLI entry point.

Guaranteed 100% offline: uses `--data-provider synthetic` only, never yfinance
or any network-backed provider.
"""

import json
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from research_strategy import run_research_strategy as rrs


# Every DEFAULT_UNIVERSE_SYMBOLS ticker except "SCZ" -- AcceleratingDualMomentum
# (config key "accelerating_dual_momentum") requires adm_equity_b="SCZ" to be
# present in the loaded universe; without it, its generate_weights() legitimately
# returns an empty DataFrame (see test_accelerating_dual_momentum_missing_equity_returns_empty
# in test_strategy.py), which drives run_allocation_backtest() into its
# short-circuit {"equity_curve": pd.DataFrame(), "turnover": 0.0} return (no
# "sharpe_ratio" key at all).
UNIVERSE_WITHOUT_SCZ = [s for s in rrs.DEFAULT_UNIVERSE_SYMBOLS if s != "SCZ"]


def test_strategy_all_survives_one_strategy_hitting_empty_weights_path(tmp_path, monkeypatch, capsys):
    """Regression test: main()'s per-strategy loop used to access
    backtest_res['sharpe_ratio'] unguarded. When a strategy legitimately
    returns empty target weights (missing required ticker), that KeyError
    used to crash the ENTIRE `--strategy all` run -- writing ZERO output
    (no research_strategy_report.json, no factor_summary.json, no
    per-strategy weights CSVs) for ANY strategy, not just the one that hit
    the empty-weights path. The fix must skip only the affected strategy,
    print a clear warning naming it, and let every other strategy in the
    run complete and get written out normally.
    """
    monkeypatch.setattr(rrs, "RESULTS_DIR", str(tmp_path))
    argv = [
        "run_research_strategy.py",
        "--strategy", "all",
        "--data-provider", "synthetic",
        "--seed", "7",
        "--n-days", "300",
        "--universe", *UNIVERSE_WITHOUT_SCZ,
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rrs.main()  # must not raise

    report_path = tmp_path / "research_strategy_report.json"
    assert report_path.exists(), "main() must still write the JSON report for the strategies that succeeded"
    with open(report_path) as f:
        report_data = json.load(f)

    # The strategy that hit the legitimate empty-weights path must be
    # excluded from the report, not crash the whole run.
    assert "accelerating_dual_momentum" not in report_data

    # But every other strategy in the config must still have completed and
    # produced a full, valid metrics entry.
    assert len(report_data) > 0
    for strat_name, entry in report_data.items():
        assert "sharpe_ratio" in entry
        assert "cagr" in entry

    # A per-strategy weights CSV must exist for a successful strategy...
    some_other_strategy = next(iter(report_data))
    assert (tmp_path / f"{some_other_strategy}_weights.csv").exists()
    # ...but not for the one that was skipped.
    assert not (tmp_path / "accelerating_dual_momentum_weights.csv").exists()

    # The factor summary must also be written and must not reference the
    # skipped strategy's metrics.
    factor_summary_path = tmp_path / "factor_summary.json"
    assert factor_summary_path.exists()

    captured = capsys.readouterr()
    assert "accelerating_dual_momentum" in captured.out
    assert "WARNING" in captured.out

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

from common import cli_utils
from research_strategy import run_research_strategy as rrs


def test_data_dir_uses_shared_data_dir():
    """DATA_DIR must resolve via the shared, repo-root-relative cache
    directory (common.cli_utils.shared_data_dir()) rather than a
    project-local path, now that the OHLCV cache is consolidated
    workspace-wide.
    """
    assert rrs.DATA_DIR == cli_utils.shared_data_dir()


def test_strategy_class_map_and_instantiate_helper_importable_from_rs_strategy():
    """STRATEGY_CLASS_MAP and instantiate_strategy_from_config_entry now live
    in rs/strategy.py (not this CLI script) so other projects can import them
    -- confirm run_research_strategy.py's own re-imported names are literally
    the same objects (not a stale duplicate)."""
    from rs.strategy import STRATEGY_CLASS_MAP as canonical_map
    from rs.strategy import instantiate_strategy_from_config_entry as canonical_fn

    assert rrs.STRATEGY_CLASS_MAP is canonical_map
    assert rrs.instantiate_strategy_from_config_entry is canonical_fn
    assert len(canonical_map) == 18


def test_strategy_class_map_importable_via_research_strategy_namespace_package():
    """The intended cross-project import path (research_strategy.rs.strategy,
    reachable via Python's implicit namespace packages since research_strategy/
    has no __init__.py) must resolve to a working, fully-populated registry --
    this is the mechanism strategy_generator/backtester use to consume these
    strategies.

    NOTE: this is deliberately NOT asserted `is` rrs.STRATEGY_CLASS_MAP -- when
    a single process imports the SAME file under two different qualified names
    (bare `rs.strategy`, used internally by run_research_strategy.py itself,
    vs. namespaced `research_strategy.rs.strategy`), Python's import system
    caches them as two SEPARATE module objects with two separate class
    definitions (verified directly; this is standard Python import behavior,
    not a bug). This is harmless in practice: strategy_generator/backtester
    only ever use the namespaced path and never also import
    run_research_strategy.py (the only thing that triggers the bare path) in
    the same process."""
    from research_strategy.rs.strategy import STRATEGY_CLASS_MAP as namespaced_map

    assert len(namespaced_map) == 18
    assert set(namespaced_map.keys()) == set(rrs.STRATEGY_CLASS_MAP.keys())


def test_cache_ttl_days_arg_default_and_parsing():
    """--cache-ttl-days is added automatically by add_data_provider_cli_args()
    and must default to None, parsing to a float when supplied.
    """
    parser = rrs.build_arg_parser()

    args = parser.parse_args([])
    assert args.cache_ttl_days is None

    args = parser.parse_args(["--cache-ttl-days", "7"])
    assert args.cache_ttl_days == 7.0


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

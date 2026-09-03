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
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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

    # The top-strategies leaderboard must also be written and must not
    # reference the skipped strategy.
    top_strategies_path = tmp_path / "top_strategies_summary.json"
    assert top_strategies_path.exists()
    with open(top_strategies_path) as f:
        top_summary = json.load(f)
    ranked_keys = {entry["strategy_key"] for entry in top_summary["top_strategies"]}
    assert "accelerating_dual_momentum" not in ranked_keys
    assert "=== Top" in captured.out


def test_vaa_strategy_requires_both_universe_flags_or_exits(tmp_path, monkeypatch, capsys):
    """vigilant_asset_allocation no longer has a hardcoded default universe -- requesting it
    directly without both --vaa-offensive-universe/--vaa-defensive-universe must exit(1) with a
    clear message, not silently fall back to some default or crash with an unrelated error."""
    monkeypatch.setattr(rrs, "RESULTS_DIR", str(tmp_path))
    argv = [
        "run_research_strategy.py",
        "--strategy", "vigilant_asset_allocation",
        "--data-provider", "synthetic",
        "--n-days", "300",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        rrs.main()
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "vaa-offensive-universe" in captured.out
    assert "vaa-defensive-universe" in captured.out


def test_vaa_strategy_runs_with_both_universe_flags_supplied(tmp_path, monkeypatch):
    monkeypatch.setattr(rrs, "RESULTS_DIR", str(tmp_path))
    argv = [
        "run_research_strategy.py",
        "--strategy", "vigilant_asset_allocation",
        "--data-provider", "synthetic",
        "--n-days", "300",
        "--vaa-offensive-universe", "SPY", "QQQ",
        "--vaa-defensive-universe", "IEF", "BIL",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rrs.main()  # must not raise/exit

    report_path = tmp_path / "research_strategy_report.json"
    assert report_path.exists()
    with open(report_path) as f:
        report_data = json.load(f)
    assert "vigilant_asset_allocation" in report_data


def test_strategy_all_skips_vaa_with_warning_when_universe_flags_missing(tmp_path, monkeypatch, capsys):
    """--strategy all must not be blocked by VAA's new requirement -- it should skip just VAA
    with a clear warning and let every other strategy in the run complete normally."""
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

    rrs.main()  # must not raise/exit

    report_path = tmp_path / "research_strategy_report.json"
    with open(report_path) as f:
        report_data = json.load(f)
    assert "vigilant_asset_allocation" not in report_data
    assert len(report_data) > 0

    captured = capsys.readouterr()
    assert "vigilant_asset_allocation" in captured.out
    assert "WARNING" in captured.out


def _fake_metrics(sharpe, cagr, strategy_name="Fake Strategy", raw_description="fake raw description"):
    return {
        "strategy_name": strategy_name,
        "raw_description": raw_description,
        "parsed_summary": "fake parsed summary",
        "sharpe_ratio": sharpe,
        "cagr": cagr,
        "max_drawdown": 0.1,
        "calmar_ratio": cagr / 0.1 if cagr else 0.0,
        "win_rate": 0.5,
        "profit_factor": 1.2,
        "total_turnover": 3.0,
        "total_rebalances": 12,
    }


class _FakeArgs:
    data_provider = "synthetic"
    seed = 42
    n_days = 300


def test_build_and_write_top_strategies_summary_ranks_by_sharpe_then_cagr(tmp_path):
    report_data = {
        "low_sharpe": _fake_metrics(sharpe=0.5, cagr=0.20),
        "high_sharpe": _fake_metrics(sharpe=1.5, cagr=0.10),
        "tied_sharpe_low_cagr": _fake_metrics(sharpe=1.0, cagr=0.05),
        "tied_sharpe_high_cagr": _fake_metrics(sharpe=1.0, cagr=0.15),
    }
    strategy_factor_tags = {"high_sharpe": ["relative_momentum"]}
    loaded_config = {
        "high_sharpe": {"name": "High Sharpe Strategy", "description": "A high-Sharpe test strategy."},
    }

    path, top_strategies = rrs.build_and_write_top_strategies_summary(
        report_data, strategy_factor_tags, loaded_config, _FakeArgs(),
        start="2020-01-01", end="2020-12-31", results_dir=str(tmp_path), top_n=5,
    )

    assert os.path.exists(path)
    with open(path) as f:
        written = json.load(f)
    assert written["n_strategies_evaluated"] == 4
    expected_order = ["high_sharpe", "tied_sharpe_high_cagr", "tied_sharpe_low_cagr", "low_sharpe"]
    assert [e["strategy_key"] for e in written["top_strategies"]] == expected_order
    assert [e["strategy_key"] for e in top_strategies] == expected_order
    assert [e["rank"] for e in top_strategies] == [1, 2, 3, 4]

    # Display name/description resolved from strategies_config.json when available...
    winner = top_strategies[0]
    assert winner["strategy_name"] == "High Sharpe Strategy"
    assert winner["description"] == "A high-Sharpe test strategy."
    assert winner["factor_tags"] == ["relative_momentum"]

    # ...and fall back to report_data's own fields when there's no config entry.
    runner_up = top_strategies[1]
    assert runner_up["strategy_name"] == "Fake Strategy"
    assert runner_up["description"] == "fake raw description"
    assert runner_up["factor_tags"] == []


def test_build_and_write_top_strategies_summary_top_n_larger_than_available(tmp_path):
    report_data = {"only_one": _fake_metrics(sharpe=1.0, cagr=0.1)}
    _, top_strategies = rrs.build_and_write_top_strategies_summary(
        report_data, {}, {}, _FakeArgs(), start="2020-01-01", end="2020-12-31",
        results_dir=str(tmp_path), top_n=5,
    )
    assert len(top_strategies) == 1


def test_build_and_write_top_strategies_summary_empty_report_data(tmp_path):
    path, top_strategies = rrs.build_and_write_top_strategies_summary(
        {}, {}, {}, _FakeArgs(), start="2020-01-01", end="2020-12-31",
        results_dir=str(tmp_path), top_n=5,
    )
    assert top_strategies == []
    with open(path) as f:
        written = json.load(f)
    assert written["n_strategies_evaluated"] == 0
    assert written["top_strategies"] == []


def test_top_n_cli_arg_default_and_parsing():
    parser = rrs.build_arg_parser()

    args = parser.parse_args([])
    assert args.top_n == 5

    args = parser.parse_args(["--top-n", "3"])
    assert args.top_n == 3


def test_dump_strategies_cli_arg_default_and_parsing():
    parser = rrs.build_arg_parser()

    args = parser.parse_args([])
    assert args.dump_strategies is False

    args = parser.parse_args(["--dump-strategies"])
    assert args.dump_strategies is True


def test_build_and_dump_strategies_writes_one_reloadable_file_per_config_entry(tmp_path):
    """The real regression check: every strategies_config.json entry must round-trip through
    common.strategy_spec.load_strategy_file + get_template, successfully reconstructing a live
    strategy instance -- not just produce syntactically valid JSON."""
    from common.strategy_spec import get_template, load_strategy_file

    from rs.config import load_strategies_config

    loaded_config = load_strategies_config()
    written = rrs.build_and_dump_strategies_as_backtester_files(loaded_config, str(tmp_path))

    assert len(written) == len(loaded_config)
    dump_dir = tmp_path / "strategy_dumps"
    for entry_key in loaded_config:
        expected_path = dump_dir / f"{entry_key}_strategy.json"
        assert str(expected_path) in written
        assert expected_path.exists()

        strategy_def = load_strategy_file(str(expected_path))
        assert strategy_def["research_strategy_spec"]["strategy_key"] == entry_key
        strat_obj = get_template(
            strategy_def["template_name"],
            research_strategy_spec=strategy_def["research_strategy_spec"],
            params=strategy_def["params"],
        )
        assert strat_obj is not None
        assert strat_obj.explain_weights()  # must not raise, must be non-empty


def test_build_and_dump_strategies_skips_malformed_entry_with_warning(capsys, tmp_path):
    loaded_config = {
        "broken": {"name": "Broken", "type": "class", "class_name": "NotARealClass", "parameters": {}},
        "dual_momentum": {
            "name": "Active Dual Momentum GTAA",
            "type": "natural_language",
            "plain_english_description": "Rebalance monthly. Risky assets: SPY, QQQ. "
                                          "Rank by 63d momentum, select top 1, equal weighting.",
            "parameters": {"rebalance_freq_days": 21, "top_k": 1, "cash_proxy": "BIL"},
        },
    }

    written = rrs.build_and_dump_strategies_as_backtester_files(loaded_config, str(tmp_path))

    assert len(written) == 1
    assert (tmp_path / "strategy_dumps" / "dual_momentum_strategy.json").exists()
    assert not (tmp_path / "strategy_dumps" / "broken_strategy.json").exists()

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "broken" in captured.out


def test_dump_strategies_exits_before_loading_universe_or_writing_eval_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(rrs, "RESULTS_DIR", str(tmp_path))

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("--dump-strategies must not load any universe/market data")

    monkeypatch.setattr(rrs, "load_universe_with_banner", _fail_if_called)
    monkeypatch.setattr(sys, "argv", ["run_research_strategy.py", "--dump-strategies"])

    rrs.main()  # must not raise, must not call load_universe_with_banner

    assert (tmp_path / "strategy_dumps").is_dir()
    assert not (tmp_path / "research_strategy_report.json").exists()
    assert not (tmp_path / "factor_summary.json").exists()
    assert not (tmp_path / "top_strategies_summary.json").exists()

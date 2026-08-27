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
_REPO_ROOT = os.path.dirname(os.path.dirname(_PROJECT_ROOT))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common import cli_utils
from research_strategy.rs.config import load_strategies_config
from research_strategy.rs.strategy import AdaptiveGridStrategy, instantiate_strategy_from_config_entry
from run_strategygen import RESULTS_DIR, _load_factor_report, _load_pattern_report, build_arg_parser, main


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
        is_composite = False
        composite_track = None
        component_templates = None

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
        is_composite = False
        composite_track = None
        component_templates = None

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
        is_composite = False
        composite_track = None
        component_templates = None

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


def test_cache_ttl_days_arg_default_and_parsing():
    """--cache-ttl-days is added automatically by add_data_provider_cli_args()
    and must default to None, parsing to a float when supplied.
    """
    parser = build_arg_parser()

    args = parser.parse_args([])
    assert args.cache_ttl_days is None

    args = parser.parse_args(["--cache-ttl-days", "7"])
    assert args.cache_ttl_days == 7.0


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_main_wires_shared_data_dir_and_cache_ttl(mock_gen_cls, mock_load):
    """cache_dir passed to load_universe_with_banner must resolve via the
    shared, repo-root-relative cache directory (common.cli_utils.shared_data_dir())
    rather than a project-local path, and --cache-ttl-days must be threaded
    through as cache_max_age_days -- now that the OHLCV cache is consolidated
    workspace-wide.
    """
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
        is_composite = False
        composite_track = None
        component_templates = None

    mock_gen_instance.generate.return_value = MockSpec()

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        json.dump({
            "basket": ["TICKER1", "TICKER2"],
            "method": "test",
            "date_generated": "2026-07-21T10:00:00Z"
        }, f)
        temp_path = f.name

    try:
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--cache-ttl-days", "3.5",
        ]
        with patch.object(sys, "argv", test_args):
            main()

        assert mock_load.call_count == 1
        _, call_kwargs = mock_load.call_args
        assert call_kwargs["cache_dir"] == cli_utils.shared_data_dir()
        assert call_kwargs["cache_max_age_days"] == 3.5
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


def test_load_pattern_report_missing_key_raises_clear_error(tmp_path):
    path = tmp_path / "bad_pattern_report.json"
    path.write_text(json.dumps({"not_findings": []}))

    with pytest.raises(ValueError, match="findings"):
        _load_pattern_report(str(path))


def test_load_pattern_report_wrong_type_raises_clear_error(tmp_path):
    path = tmp_path / "bad_pattern_report.json"
    path.write_text(json.dumps({"findings": "not_a_list"}))

    with pytest.raises(ValueError, match="must be a JSON array"):
        _load_pattern_report(str(path))


def test_load_pattern_report_valid(tmp_path):
    path = tmp_path / "pattern_report.json"
    path.write_text(json.dumps({"status": "ok", "findings": [{"feature": "rsi", "significant": True}]}))

    report = _load_pattern_report(str(path))
    assert report["findings"] == [{"feature": "rsi", "significant": True}]


# --- --research-strategy ----------------------------------------------------

def _mock_spec(**overrides):
    """A minimal GeneratedStrategySpec-shaped test double, matching the fields
    every MockSpec class above already fakes -- factored out just for the
    --research-strategy tests below so each one only overrides what it cares
    about (mainly template_name, to control which candidate 'wins')."""
    defaults = dict(
        n_symbols=2,
        template_name="test",
        params={},
        universe_sharpe=1.0,
        cagr=0.10,
        max_drawdown=-0.05,
        calmar_ratio=2.0,
        win_rate=0.55,
        profit_factor=1.5,
        total_turnover=1.0,
        total_rebalances=1,
        ers_passed=True,
        ers_percentile=0.99,
        trusted=True,
        explanation="test",
        target_weights=pd.DataFrame(),
        factor_context=None,
        factor_tiebreak_used=False,
        equity_curve=pd.DataFrame(
            {"equity": [100.0, 101.0, 102.0]}, index=pd.bdate_range("2020-01-01", periods=3)
        ),
        is_composite=False,
        composite_track=None,
        component_templates=None,
    )
    defaults.update(overrides)
    return type("MockSpec", (), defaults)()


def _universe_file(tmp_path=None):
    f = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json")
    json.dump({
        "basket": ["TICKER1", "TICKER2"],
        "method": "test",
        "date_generated": "2026-07-21T10:00:00Z",
    }, f)
    f.close()
    return f.name


@patch("run_strategygen.load_universe_with_banner")
def test_research_strategy_unknown_key_raises_named_value_error(mock_load):
    temp_path = _universe_file()
    try:
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--research-strategy", "not_a_real_strategy_key",
        ]
        with patch.object(sys, "argv", test_args):
            with pytest.raises(ValueError, match="Unknown --research-strategy key"):
                main()
    finally:
        os.remove(temp_path)


def test_research_strategy_valid_key_builds_correct_template_type():
    """Direct, isolated check (no CLI/main() involved) that the exact
    mechanism run_strategygen.py wires in -- load_strategies_config() +
    instantiate_strategy_from_config_entry() -- builds the same instance
    research_strategy's own CLI would for the 'adaptive_grid' key."""
    loaded_config = load_strategies_config()
    assert "adaptive_grid" in loaded_config
    entry_data = loaded_config["adaptive_grid"]
    template = instantiate_strategy_from_config_entry("adaptive_grid", entry_data)
    assert isinstance(template, AdaptiveGridStrategy)
    assert template.name == "adaptive_grid"


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_research_strategy_folded_into_generate_extra_templates(mock_gen_cls, mock_load):
    mock_gen_instance = mock_gen_cls.return_value
    mock_gen_instance.generate.return_value = _mock_spec()

    temp_path = _universe_file()
    try:
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--research-strategy", "adaptive_grid",
        ]
        with patch.object(sys, "argv", test_args):
            main()

        _, call_kwargs = mock_gen_instance.generate.call_args
        extra_templates = call_kwargs["extra_templates"]
        assert len(extra_templates) == 1
        assert isinstance(extra_templates[0], AdaptiveGridStrategy)
        assert extra_templates[0].name == "adaptive_grid"
    finally:
        os.remove(temp_path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_research_strategy_spec_written_when_it_wins(mock_gen_cls, mock_load):
    mock_gen_instance = mock_gen_cls.return_value
    # AdaptiveGridStrategy's own name is "adaptive_grid" -- pretend it won.
    mock_gen_instance.generate.return_value = _mock_spec(template_name="adaptive_grid")

    temp_path = _universe_file()
    try:
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--research-strategy", "adaptive_grid",
        ]
        with patch.object(sys, "argv", test_args):
            main()

        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            written = json.load(f)

        assert written["research_strategy_spec"] == {
            "strategy_key": "adaptive_grid",
            "entry_data": load_strategies_config()["adaptive_grid"],
        }
        assert written["pattern_spec"] is None
    finally:
        os.remove(temp_path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_research_strategy_spec_is_none_when_a_different_template_wins(mock_gen_cls, mock_load):
    mock_gen_instance = mock_gen_cls.return_value
    mock_gen_instance.generate.return_value = _mock_spec(template_name="equal_weight")

    temp_path = _universe_file()
    try:
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--research-strategy", "adaptive_grid",
        ]
        with patch.object(sys, "argv", test_args):
            main()

        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            written = json.load(f)

        assert written["research_strategy_spec"] is None
        assert written["pattern_spec"] is None
    finally:
        os.remove(temp_path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_composite_spec_written_when_a_composite_wins(mock_gen_cls, mock_load):
    mock_gen_instance = mock_gen_cls.return_value
    mock_gen_instance.generate.return_value = _mock_spec(
        template_name="momentum_topn__min_variance",
        is_composite=True,
        composite_track="allocation",
        component_templates=["momentum_topn", "min_variance"],
    )

    temp_path = _universe_file()
    try:
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate"]
        with patch.object(sys, "argv", test_args):
            main()

        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            written = json.load(f)

        assert written["composite_spec"] == {
            "track": "allocation", "selection_key": "momentum_topn", "weighting_key": "min_variance",
        }
        assert written["pattern_spec"] is None
        assert written["research_strategy_spec"] is None
    finally:
        os.remove(temp_path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_composite_spec_written_for_timing_track(mock_gen_cls, mock_load):
    mock_gen_instance = mock_gen_cls.return_value
    mock_gen_instance.generate.return_value = _mock_spec(
        template_name="turtle_breakout_entry__rsi_cross_exit",
        is_composite=True,
        composite_track="timing",
        component_templates=["turtle_breakout_entry", "rsi_cross_exit"],
    )

    temp_path = _universe_file()
    try:
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate"]
        with patch.object(sys, "argv", test_args):
            main()

        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            written = json.load(f)

        assert written["composite_spec"] == {
            "track": "timing", "entry_key": "turtle_breakout_entry", "exit_key": "rsi_cross_exit",
        }
    finally:
        os.remove(temp_path)


def test_no_compose_aspects_flag_disables_composition():
    args = build_arg_parser().parse_args(["--no-compose-aspects"])
    assert args.no_compose_aspects is True


class _FakePatternTemplate:
    """Minimal PatternBasedAllocationTemplate-shaped double -- just enough to
    exercise the pattern_spec reconstruction block without real turning-point
    mining (mirrors _FixedNameTemplate in tests/test_generator.py)."""
    name = "pattern_rsi_14_trough"
    feature_name = "rsi"
    feature_lookback = 14
    threshold = 30.0
    comparison = "below"
    event_type = "trough"
    mined_p_value = 0.01
    mined_n_events = 5


def _pattern_report_file(tmp_path):
    path = tmp_path / "pattern_report.json"
    path.write_text(json.dumps({"status": "ok", "findings": [{"feature": "rsi", "significant": True}]}))
    return str(path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
@patch("pattern_mining.pmine.pattern_mining.build_pattern_templates")
def test_pattern_spec_and_research_strategy_spec_never_both_nonnull(
    mock_build, mock_gen_cls, mock_load, tmp_path
):
    """When both --pattern-report and --research-strategy are supplied, only
    the actual winner's spec block may be non-null -- the two independent
    for-loops in main() search disjoint template lists, so this should hold
    regardless of which of the two candidate sources wins."""
    mock_build.return_value = [_FakePatternTemplate()]
    pattern_report_path = _pattern_report_file(tmp_path)

    temp_path = _universe_file()

    def run_with_winner(template_name):
        mock_gen_instance = mock_gen_cls.return_value
        mock_gen_instance.generate.return_value = _mock_spec(template_name=template_name)
        test_args = [
            "run_strategygen.py", "--universe-file", temp_path, "--mode", "generate",
            "--pattern-report", pattern_report_path, "--research-strategy", "adaptive_grid",
        ]
        with patch.object(sys, "argv", test_args):
            main()
        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            return json.load(f)

    try:
        # Case 1: the mined pattern template wins.
        written = run_with_winner("pattern_rsi_14_trough")
        assert written["pattern_spec"] is not None
        assert written["research_strategy_spec"] is None

        # Case 2: the research_strategy template wins.
        written = run_with_winner("adaptive_grid")
        assert written["pattern_spec"] is None
        assert written["research_strategy_spec"] is not None

        # Case 3: neither wins (a static template did).
        written = run_with_winner("equal_weight")
        assert written["pattern_spec"] is None
        assert written["research_strategy_spec"] is None
    finally:
        os.remove(temp_path)


@patch("run_strategygen.load_universe_with_banner")
@patch("run_strategygen.StrategyGenerator")
def test_omitting_research_strategy_is_byte_for_byte_unchanged(mock_gen_cls, mock_load):
    """Regression: not passing --research-strategy must produce the exact
    same strategy.json as before this feature existed, aside from the new
    'research_strategy_spec'/'composite_spec' keys always being present and
    null -- mirroring test_extra_templates_none_is_byte_for_byte_unchanged
    in test_generator.py for the equivalent --mine-patterns-era guarantee."""
    mock_gen_instance = mock_gen_cls.return_value
    mock_gen_instance.generate.return_value = _mock_spec(template_name="equal_weight")

    temp_path = _universe_file()
    try:
        test_args = ["run_strategygen.py", "--universe-file", temp_path, "--mode", "generate"]
        with patch.object(sys, "argv", test_args):
            main()

        # extra_templates passed to generate() must be unaffected (None, same
        # as if --research-strategy/--mine-patterns were never wired in).
        _, call_kwargs = mock_gen_instance.generate.call_args
        assert call_kwargs["extra_templates"] is None

        with open(os.path.join(RESULTS_DIR, "strategy.json")) as f:
            written = json.load(f)

        assert written == {
            "template_name": "equal_weight",
            "params": {},
            "explanation": "test",
            "sharpe_ratio": 1.0,
            "cagr": 0.10,
            "max_drawdown": -0.05,
            "calmar_ratio": 2.0,
            "win_rate": 0.55,
            "profit_factor": 1.5,
            "trusted": True,
            "ers_passed": True,
            "ers_percentile": 0.99,
            "factor_context": None,
            "factor_tiebreak_used": False,
            "pattern_spec": None,
            "research_strategy_spec": None,
            "composite_spec": None,
        }
    finally:
        os.remove(temp_path)

"""Unit tests for strategy.json reconstruction/validation (common/strategy_spec.py).
Guaranteed 100% offline: only reads hand-built local JSON fixtures, no network access.

Moved here from backtester/tests/test_run_backtest.py's own former `_get_template`/
`_load_strategy_file` coverage now that both live in common/strategy_spec.py as a
shared, public module -- backtester/tests/test_run_backtest.py keeps its own
tests that exercise reconstruction TOGETHER with backtester's own run_standard/
run_walkforward/main() (integration coverage), not duplicated here.
"""

import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from common.strategy_spec import get_template, load_strategy_file


def _write_strategy_file(path, template_name="equal_weight", params=None, pattern_spec=None,
                          research_strategy_spec=None, composite_spec=None, fundamental_spec=None,
                          bnn_spec=None):
    strategy_def = {"template_name": template_name, "params": params or {"rebalance_freq_days": 10}}
    if pattern_spec is not None:
        strategy_def["pattern_spec"] = pattern_spec
    if research_strategy_spec is not None:
        strategy_def["research_strategy_spec"] = research_strategy_spec
    if composite_spec is not None:
        strategy_def["composite_spec"] = composite_spec
    if fundamental_spec is not None:
        strategy_def["fundamental_spec"] = fundamental_spec
    if bnn_spec is not None:
        strategy_def["bnn_spec"] = bnn_spec
    with open(path, "w") as f:
        json.dump(strategy_def, f)


def _permanent_portfolio_research_strategy_spec():
    # The exact raw pipeline/research_strategy/strategies_config.json["permanent_portfolio"]
    # entry -- a simple, deterministic, class-based, fixed-weight strategy
    # (25% SPY / 25% TLT / 25% BIL / 25% GLD, annual rebalance) that needs no
    # natural-language parsing and no lookback warmup, making it ideal for a
    # focused reconstruction test.
    return {
        "strategy_key": "permanent_portfolio",
        "entry_data": {
            "name": "Permanent Portfolio (Harry Browne)",
            "type": "class",
            "class_name": "PermanentPortfolioStrategy",
            "description": (
                "Static 25% SPY / 25% TLT / 25% BIL / 25% GLD allocation, annual "
                "rebalance. SPY substitutes for the canonical VTI."
            ),
            "parameters": {},
        },
    }


def test_get_template():
    template = get_template("equal_weight")
    assert template.name == "equal_weight"

    try:
        get_template("non_existent")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_get_template_reconstructs_pattern_based_template_from_spec():
    # A PatternBasedAllocationTemplate (common/allocation_templates.py) is
    # universe-specific and never in the static ALLOCATION_TEMPLATES
    # registry -- it must be reconstructed from a pattern_spec dict instead
    # (see run_strategygen.py's strategy.json output).
    pattern_spec = {
        "feature_name": "rsi",
        "feature_lookback": 14,
        "threshold": 30.0,
        "comparison": "below",
        "event_type": "trough",
        "mined_p_value": 0.001,
        "mined_n_events": 12,
    }
    template = get_template("pattern_rsi_14_trough", pattern_spec)
    assert template.name == "pattern_rsi_14_trough"
    assert template.feature_name == "rsi"
    assert template.feature_lookback == 14
    assert template.comparison == "below"
    assert template.event_type == "trough"


def test_get_template_rejects_pattern_spec_with_non_pattern_prefixed_template_name():
    # Regression test: SCHEMAS.md documents that pattern-template
    # reconstruction triggers "when template_name starts with pattern_", but
    # get_template used to branch purely on `pattern_spec is not None`,
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
        get_template("equal_weight", pattern_spec)


def test_get_template_reconstructs_research_strategy_spec_from_spec():
    # A research_strategy_spec (produced by strategy_generator when a
    # research_strategy candidate wins) is reconstructed via research_strategy's
    # own instantiate_strategy_from_config_entry -- this must work uniformly
    # for class-based strategies without any research_strategy-side changes.
    research_strategy_spec = _permanent_portfolio_research_strategy_spec()

    template = get_template("permanent_portfolio", research_strategy_spec=research_strategy_spec)

    from research_strategy.rs.strategy import PermanentPortfolioStrategy
    assert isinstance(template, PermanentPortfolioStrategy)
    assert template.name == "permanent_portfolio"
    assert hasattr(template, "generate_weights")
    assert hasattr(template, "warmup_bars")


def test_get_template_reconstructs_allocation_composite_from_spec():
    # A composite_spec with track="allocation" (strategy_generator's aspect
    # composition, see common/strategy_aspects.py) must rebuild the exact
    # CompositeAllocationTemplate, with the saved params carried through as
    # its default_params fallback (needed by --optimize's fresh grid search).
    composite_spec = {"track": "allocation", "selection_key": "momentum_topn", "weighting_key": "min_variance"}
    params = {"mom_lookback": 126, "top_n_fraction": 0.5, "cov_lookback": 60, "rebalance_freq_days": 21}

    template = get_template("momentum_topn__min_variance", composite_spec=composite_spec, params=params)

    from common.strategy_aspects import CompositeAllocationTemplate
    assert isinstance(template, CompositeAllocationTemplate)
    assert template.name == "momentum_topn__min_variance"
    assert template.default_params == params


def test_get_template_reconstructs_timing_composite_from_spec():
    # A composite_spec with track="timing" (pipeline/research_strategy/rs/timing_aspects.py)
    # must rebuild the exact CompositeTimingTemplate, with the saved params
    # carried through as its default_params fallback.
    composite_spec = {"track": "timing", "entry_key": "turtle_breakout_entry", "exit_key": "rsi_cross_exit"}
    params = {"turtle_entry_breakout_days": 20, "rsi_stop_loss_pct": 0.05}

    template = get_template("turtle_breakout_entry__rsi_cross_exit", composite_spec=composite_spec, params=params)

    from research_strategy.rs.timing_aspects import CompositeTimingTemplate
    assert isinstance(template, CompositeTimingTemplate)
    assert template.name == "turtle_breakout_entry__rsi_cross_exit"
    assert template.default_params == params


def test_load_strategy_file_composite_spec_missing_required_keys(tmp_path):
    path = tmp_path / "strategy.json"
    composite_spec = {"track": "allocation", "selection_key": "momentum_topn"}  # "weighting_key" omitted
    _write_strategy_file(path, template_name="momentum_topn__min_variance", composite_spec=composite_spec)

    with pytest.raises(ValueError, match="weighting_key"):
        load_strategy_file(str(path))


def test_load_strategy_file_composite_spec_unknown_track(tmp_path):
    path = tmp_path / "strategy.json"
    composite_spec = {"track": "bogus"}
    _write_strategy_file(path, template_name="x", composite_spec=composite_spec)

    with pytest.raises(ValueError, match="track"):
        load_strategy_file(str(path))


def test_load_strategy_file_rejects_composite_spec_alongside_pattern_spec(tmp_path):
    path = tmp_path / "strategy.json"
    pattern_spec = {
        "feature_name": "rsi", "feature_lookback": 14, "threshold": 30.0,
        "comparison": "below", "event_type": "trough",
    }
    composite_spec = {"track": "allocation", "selection_key": "momentum_topn", "weighting_key": "min_variance"}
    _write_strategy_file(
        path, template_name="pattern_rsi_14_trough", pattern_spec=pattern_spec, composite_spec=composite_spec,
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        load_strategy_file(str(path))


def test_get_template_reconstructs_fundamental_screener_strategy_from_spec():
    # A fundamental_spec (produced by the separate fundamental_screener
    # project) must rebuild the exact FundamentalMarginOfSafetyStrategy --
    # it's zero-arg constructible, so the marker only needs to identify the
    # source/trigger the right import, not carry reconstruction data itself.
    fundamental_spec = {"source": "fundamental_screener"}

    template = get_template("fundamental_margin_of_safety", fundamental_spec=fundamental_spec)

    from fundamental_screener.fscreen.strategy import FundamentalMarginOfSafetyStrategy
    assert isinstance(template, FundamentalMarginOfSafetyStrategy)
    assert template.name == "fundamental_margin_of_safety"


def test_load_strategy_file_fundamental_spec_must_be_object(tmp_path):
    path = tmp_path / "strategy.json"
    strategy_def = {
        "template_name": "fundamental_margin_of_safety",
        "params": {},
        "fundamental_spec": "not-an-object",
    }
    with open(path, "w") as f:
        json.dump(strategy_def, f)

    with pytest.raises(ValueError, match="fundamental_spec"):
        load_strategy_file(str(path))


def test_load_strategy_file_rejects_fundamental_spec_alongside_composite_spec(tmp_path):
    path = tmp_path / "strategy.json"
    composite_spec = {"track": "allocation", "selection_key": "momentum_topn", "weighting_key": "min_variance"}
    fundamental_spec = {"source": "fundamental_screener"}
    _write_strategy_file(
        path, template_name="momentum_topn__min_variance",
        composite_spec=composite_spec, fundamental_spec=fundamental_spec,
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        load_strategy_file(str(path))


def test_get_template_reconstructs_bnn_forecaster_strategy_from_spec():
    # A bnn_spec (produced by the separate bnn_forecaster project) must
    # rebuild the exact BnnForecastStrategy -- zero-arg constructible, same
    # shape as fundamental_spec above. bnn_forecaster's autobnn/jax
    # dependency chain lives ONLY in its own isolated uv environment, not
    # this workspace's root/pipeline venv -- skip (not fail) when it's
    # absent, e.g. when this suite runs under a venv other than
    # bnn_forecaster's own.
    pytest.importorskip("autobnn", reason="bnn_forecaster's isolated uv environment is not active")
    bnn_spec = {"source": "bnn_forecaster"}

    template = get_template("bnn_forecast", bnn_spec=bnn_spec)

    from bnn_forecaster.bnnf.strategy import BnnForecastStrategy
    assert isinstance(template, BnnForecastStrategy)
    assert template.name == "bnn_forecast"


def test_load_strategy_file_bnn_spec_must_be_object(tmp_path):
    path = tmp_path / "strategy.json"
    strategy_def = {"template_name": "bnn_forecast", "params": {}, "bnn_spec": "not-an-object"}
    with open(path, "w") as f:
        json.dump(strategy_def, f)

    with pytest.raises(ValueError, match="bnn_spec"):
        load_strategy_file(str(path))


def test_load_strategy_file_rejects_bnn_spec_alongside_fundamental_spec(tmp_path):
    path = tmp_path / "strategy.json"
    fundamental_spec = {"source": "fundamental_screener"}
    bnn_spec = {"source": "bnn_forecaster"}
    _write_strategy_file(
        path, template_name="x", fundamental_spec=fundamental_spec, bnn_spec=bnn_spec,
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        load_strategy_file(str(path))


def test_load_strategy_file_missing_required_keys(tmp_path):
    path = tmp_path / "strategy.json"
    with open(path, "w") as f:
        json.dump({"template_name": "equal_weight"}, f)  # missing "params"

    with pytest.raises(ValueError, match="missing required key"):
        load_strategy_file(str(path))


def test_load_strategy_file_params_not_dict(tmp_path):
    path = tmp_path / "strategy.json"
    with open(path, "w") as f:
        json.dump({"template_name": "equal_weight", "params": ["not", "a", "dict"]}, f)

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_strategy_file(str(path))


def test_load_strategy_file_pattern_spec_missing_required_keys(tmp_path):
    # Regression test: get_template indexed pattern_spec["threshold"] etc.
    # via plain brackets with no upfront validation, so a malformed
    # pattern_spec block used to raise a raw, unhandled KeyError deep in a
    # caller instead of a clean, informative ValueError.
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
        load_strategy_file(str(path))


def test_load_strategy_file_research_strategy_spec_missing_required_keys(tmp_path):
    # Regression test mirroring test_load_strategy_file_pattern_spec_missing_required_keys:
    # a research_strategy_spec block missing strategy_key/entry_data must raise
    # a clear, named ValueError up front instead of a raw KeyError deep in
    # get_template.
    path = tmp_path / "strategy.json"
    research_strategy_spec = {
        "strategy_key": "permanent_portfolio",
        # "entry_data" intentionally omitted
    }
    _write_strategy_file(path, template_name="permanent_portfolio", research_strategy_spec=research_strategy_spec)

    with pytest.raises(ValueError, match="entry_data"):
        load_strategy_file(str(path))


def test_load_strategy_file_research_strategy_spec_wrong_types(tmp_path):
    path = tmp_path / "strategy.json"
    research_strategy_spec = {"strategy_key": 123, "entry_data": {}}
    _write_strategy_file(path, template_name="permanent_portfolio", research_strategy_spec=research_strategy_spec)

    with pytest.raises(ValueError, match="strategy_key"):
        load_strategy_file(str(path))


def test_load_strategy_file_rejects_both_pattern_spec_and_research_strategy_spec(tmp_path):
    # Bug 2 regression test: pattern_spec and research_strategy_spec are
    # mutually exclusive (a strategy.json only ever came from one source),
    # but the two blocks used to be validated completely independently, with
    # no check that at most one is set. get_template checks
    # research_strategy_spec first and returns immediately if present, so a
    # file with both would have research_strategy_spec silently win with no
    # diagnostic that pattern_spec was ignored -- must raise instead.
    path = tmp_path / "strategy.json"
    pattern_spec = {
        "feature_name": "rsi",
        "feature_lookback": 14,
        "threshold": 30.0,
        "comparison": "below",
        "event_type": "trough",
    }
    research_strategy_spec = _permanent_portfolio_research_strategy_spec()
    _write_strategy_file(
        path, template_name="pattern_rsi_14_trough",
        pattern_spec=pattern_spec, research_strategy_spec=research_strategy_spec,
    )

    with pytest.raises(ValueError, match="pattern_spec") as exc_info:
        load_strategy_file(str(path))
    assert "research_strategy_spec" in str(exc_info.value)
    assert "mutually exclusive" in str(exc_info.value)


def test_load_strategy_file_valid(tmp_path):
    path = tmp_path / "strategy.json"
    _write_strategy_file(path)
    strategy_def = load_strategy_file(str(path))
    assert strategy_def["template_name"] == "equal_weight"
    assert strategy_def["params"] == {"rebalance_freq_days": 10}

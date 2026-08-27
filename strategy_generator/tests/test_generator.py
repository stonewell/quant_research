from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from common.testing import make_ohlcv_from_closes as make_df

from common.allocation_templates import ALLOCATION_TEMPLATES

from stratgen.generator import (
    GeneratorConfig,
    RandomAllocationTemplate,
    StrategyGenerator,
    _apply_factor_tiebreak,
    _portfolio_score,
    _search_allocation,
    grid_combinations,
)


class _FakeTemplate:
    def __init__(self, name, factor_tags):
        self.name = name
        self.factor_tags = factor_tags


def _fake_result(name, score, factor_tags=()):
    return {"template": _FakeTemplate(name, list(factor_tags)), "params": {}, "res": {}, "score": score}


def test_generator_finds_momentum_allocation():
    # Construct a universe where asset A goes up, B goes down, C goes down
    idx = pd.bdate_range("2020-01-01", periods=300)

    closes_a = np.linspace(100, 200, 300)
    closes_b = np.linspace(100, 50, 300)
    closes_c = np.linspace(100, 50, 300)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": make_df(closes_c, start="2020-01-01"),
    }

    # Very small random search to keep tests fast
    config = GeneratorConfig(n_random_search=10, seed=42)
    gen = StrategyGenerator(config)

    spec = gen.generate(universe)

    # Given this universe, CrossSectionalMomentum should win easily
    # because it will allocate 100% to A, which is a straight line up (infinite Sharpe)
    # Equal weight would dilute the return with B and C.
    assert spec.template_name == "cross_sectional_momentum"
    assert spec.universe_sharpe > 0
    assert spec.n_symbols == 3
    assert not spec.target_weights.empty


def test_generator_exposes_winning_candidate_equity_curve():
    idx = pd.bdate_range("2020-01-01", periods=300)

    closes_a = np.linspace(100, 200, 300)
    closes_b = np.linspace(100, 50, 300)
    closes_c = np.linspace(100, 50, 300)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": make_df(closes_c, start="2020-01-01"),
    }

    config = GeneratorConfig(n_random_search=10, seed=42)
    gen = StrategyGenerator(config)

    spec = gen.generate(universe)

    assert spec.equity_curve is not None
    assert isinstance(spec.equity_curve, pd.DataFrame)
    assert not spec.equity_curve.empty
    assert "equity" in spec.equity_curve.columns


def test_generator_finds_inverse_vol_allocation():
    idx = pd.bdate_range("2020-01-01", periods=300)

    # Asset A is a smooth uptrend (low vol, positive return)
    # Asset B is a choppy uptrend (high vol, positive return)
    rng = np.random.default_rng(42)
    closes_a = 100 + np.cumsum(rng.normal(0.1, 0.1, 300))
    closes_b = 100 + np.cumsum(rng.normal(0.1, 5.0, 300))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    config = GeneratorConfig(n_random_search=10, seed=42)
    gen = StrategyGenerator(config)

    spec = gen.generate(universe)

    # Inverse Volatility or Hierarchical Risk Parity (both risk-parity methods)
    # should win here because they allocate more capital to the smooth asset A, maximizing Sharpe ratio
    assert spec.template_name in ("inverse_volatility", "hierarchical_risk_parity")
    assert spec.universe_sharpe > 0
    assert not spec.target_weights.empty


def test_generator_ers_check_works():
    idx = pd.bdate_range("2020-01-01", periods=300)

    # A genuine zero-drift random walk (geometric, so it can't go negative --
    # see common/testing.py's own note on this). Crucially this is NOT "a
    # constant plus iid noise" (100 + rng.normal(...)) -- that construction
    # has a fixed, exploitable mean to revert to, which is a real, legitimate
    # edge for a mean-reversion template, not overfitting; a true random walk
    # has no such fixed mean, so no template (momentum, mean-reversion, or
    # allocation-based) should find a persistent edge on it.
    rng = np.random.default_rng(123)
    closes_a = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    closes_b = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    # Aspect composition disabled: this test is about the ERS check's own
    # false-positive rate over the 9 static templates, not about whether
    # composition adds candidates -- see test_generator_composite_*.
    config = GeneratorConfig(
        n_random_search=100, ers_percentile_threshold=0.99, seed=123, enable_aspect_composition=False,
    )
    gen = StrategyGenerator(config)

    spec = gen.generate(universe)

    # On a genuine zero-drift random walk, no trend or allocation strategy has a significant edge over random search
    assert spec.trusted is False
    assert spec.ers_passed is False


def test_factor_tiebreak_fires_when_scores_are_close_and_factor_score_is_higher():
    best_per_template = {
        "momentum_like": _fake_result("momentum_like", score=1.00, factor_tags=["relative_momentum"]),
        "breadth_like": _fake_result("breadth_like", score=0.98, factor_tags=["breadth"]),
    }
    factor_report = {"factor_performance": {
        "relative_momentum": {"mean_sharpe_ratio": 0.1},
        "breadth": {"mean_sharpe_ratio": 0.9},
    }}

    winner, factor_context, tiebreak_used = _apply_factor_tiebreak(best_per_template, factor_report, epsilon=0.05)

    assert tiebreak_used is True
    assert winner["template"].name == "breadth_like"
    assert factor_context == {"momentum_like": 0.1, "breadth_like": 0.9}


def test_factor_tiebreak_does_not_fire_when_scores_are_clearly_different():
    best_per_template = {
        "clear_winner": _fake_result("clear_winner", score=2.00, factor_tags=["relative_momentum"]),
        "clear_loser": _fake_result("clear_loser", score=0.10, factor_tags=["breadth"]),
    }
    # clear_loser's factor tag scores much higher, but the Sharpe gap is not
    # within epsilon -- the factor report must NOT override a clear winner.
    factor_report = {"factor_performance": {
        "relative_momentum": {"mean_sharpe_ratio": 0.01},
        "breadth": {"mean_sharpe_ratio": 5.0},
    }}

    winner, factor_context, tiebreak_used = _apply_factor_tiebreak(best_per_template, factor_report, epsilon=0.05)

    assert tiebreak_used is False
    assert winner["template"].name == "clear_winner"


def test_factor_tiebreak_is_a_no_op_when_no_report_supplied():
    best_per_template = {
        "a": _fake_result("a", score=1.00, factor_tags=["relative_momentum"]),
        "b": _fake_result("b", score=0.99, factor_tags=["breadth"]),
    }
    winner, factor_context, tiebreak_used = _apply_factor_tiebreak(best_per_template, factor_report=None, epsilon=0.05)

    assert tiebreak_used is False
    assert winner["template"].name == "a"
    # Documented contract (see README's "Data Shapes & Schemas" and
    # GeneratedStrategySpec.factor_context's own type hint): factor_context
    # must be None -- not {} -- when no factor_report was supplied, so
    # strategy.json serializes it as JSON `null` rather than an empty object.
    assert factor_context is None


def test_factor_tiebreak_ignores_templates_with_no_computable_factor_score():
    # Both templates are tied, but neither's factor_tags appear in the report
    # -- there's nothing to break the tie WITH, so the original leader stands.
    best_per_template = {
        "a": _fake_result("a", score=1.00, factor_tags=["mean_reversion"]),
        "b": _fake_result("b", score=0.99, factor_tags=["correlation_diversification"]),
    }
    factor_report = {"factor_performance": {"breadth": {"mean_sharpe_ratio": 5.0}}}

    winner, factor_context, tiebreak_used = _apply_factor_tiebreak(best_per_template, factor_report, epsilon=0.05)

    assert tiebreak_used is False
    assert winner["template"].name == "a"
    assert factor_context == {"a": None, "b": None}


def test_generator_factor_report_none_is_byte_for_byte_unchanged():
    # Regression guard: omitting factor_report must reproduce the exact same
    # winner as before this feature existed.
    idx = pd.bdate_range("2020-01-01", periods=300)
    closes_a = np.linspace(100, 200, 300)
    closes_b = np.linspace(100, 50, 300)
    closes_c = np.linspace(100, 50, 300)
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": make_df(closes_c, start="2020-01-01"),
    }

    config = GeneratorConfig(n_random_search=10, seed=42)
    gen = StrategyGenerator(config)

    spec_without_report = gen.generate(universe)
    spec_with_none_report = gen.generate(universe, factor_report=None)

    assert spec_without_report.template_name == spec_with_none_report.template_name == "cross_sectional_momentum"
    assert not spec_without_report.factor_context
    assert spec_without_report.factor_tiebreak_used is False


# --- extra_templates -------------------------------------------------------

class _FixedNameTemplate:
    """A minimal AllocationTemplate-shaped test double -- just enough to
    exercise _search_allocation's pool-building/dedup logic without a real
    backtest."""
    def __init__(self, name):
        self.name = name
        self.param_grid = {"rebalance_freq_days": [21]}
        self.factor_tags = []

    def generate_weights(self, universe, params):
        symbols = list(universe.keys())
        master_index = universe[symbols[0]].index
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[master_index[::params["rebalance_freq_days"]]] = 1.0 / len(symbols)
        return weights_df

    def explain_weights(self, params):
        return "test"

    def warmup_bars(self, params):
        return 0


def _small_universe():
    idx = pd.bdate_range("2020-01-01", periods=100)
    return {"A": make_df(np.linspace(100, 110, 100), start="2020-01-01"),
            "B": make_df(np.linspace(100, 90, 100), start="2020-01-01")}


def test_extra_templates_none_is_byte_for_byte_unchanged():
    universe = _small_universe()
    config = GeneratorConfig(n_random_search=5, seed=1)
    result_default = _search_allocation(universe, config)
    result_none = _search_allocation(universe, config, extra_templates=None)
    result_empty = _search_allocation(universe, config, extra_templates=[])
    assert result_default["template"].name == result_none["template"].name == result_empty["template"].name
    assert result_default["score"] == result_none["score"] == result_empty["score"]


def test_extra_templates_duplicate_name_raises():
    universe = _small_universe()
    config = GeneratorConfig(n_random_search=5, seed=1)
    colliding = _FixedNameTemplate("equal_weight")  # collides with a static template's name
    with pytest.raises(ValueError, match="Duplicate allocation template name"):
        _search_allocation(universe, config, extra_templates=[colliding])


def test_extra_template_can_win_when_it_clearly_scores_best():
    universe = _small_universe()
    config = GeneratorConfig(n_random_search=5, seed=1)
    extra = _FixedNameTemplate("my_extra_template")

    def fake_portfolio_score(universe, template, params, cfg):
        if getattr(template, "name", None) == "my_extra_template":
            return {"sharpe_ratio": 999.0, "total_rebalances": 10, "total_turnover": 1.0}
        return _portfolio_score(universe, template, params, cfg)

    with patch("stratgen.generator._portfolio_score", side_effect=fake_portfolio_score):
        result = _search_allocation(universe, config, extra_templates=[extra])

    assert result["template"].name == "my_extra_template"
    assert result["score"] == 999.0


# --- ERS fail-safe / logging / n_trials regressions ------------------------

def test_ers_percentile_defaults_to_zero_fail_safe_when_random_pool_is_empty():
    # If EVERY random trial returns a non-finite Sharpe (filtered out by the
    # `if np.isfinite(s)` guard), random_scores ends up empty. Regression:
    # this used to default ers_percentile to 1.0 (a trivial, unearned "pass"
    # against a nonexistent comparison pool) instead of failing safe.
    universe = _small_universe()
    config = GeneratorConfig(n_random_search=5, seed=1)

    def fake_portfolio_score(universe, template, params, cfg):
        if isinstance(template, RandomAllocationTemplate):
            return {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}
        return _portfolio_score(universe, template, params, cfg)

    with patch("stratgen.generator._portfolio_score", side_effect=fake_portfolio_score):
        result = _search_allocation(universe, config)

    assert result["ers_percentile"] == 0.0
    assert result["ers_passed"] is False


class _RaisingTemplate:
    """A template whose generate_weights deliberately raises, to exercise
    _portfolio_score's except-and-continue fallback + warning."""
    name = "raising_template"

    def generate_weights(self, universe, params):
        raise ValueError("deliberate failure for test")


def test_portfolio_score_warns_and_returns_inf_fallback_on_exception():
    # The bare except must keep swallowing the exception into a -inf score
    # (a single bad candidate/trial should not crash the whole search), but
    # it must now surface a RuntimeWarning so the failure is visible instead
    # of silently indistinguishable from "this candidate just performs poorly".
    universe = _small_universe()
    config = GeneratorConfig(seed=1)
    template = _RaisingTemplate()

    with pytest.warns(RuntimeWarning, match="ValueError"):
        result = _portfolio_score(universe, template, {}, config)

    assert result == {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}


def test_n_trials_counts_configured_random_search_attempts_not_survivors():
    # Regression: n_trials used to be total_grid_trials + len(random_scores),
    # which undercounts whenever some random trials are filtered out for a
    # non-finite Sharpe. It must reflect cfg.n_random_search (the number of
    # random trials actually run/attempted), not the number that survived.
    universe = _small_universe()
    # Aspect composition disabled: this test is about counting grid +
    # random trials over the 9 static templates, not composite candidates.
    config = GeneratorConfig(n_random_search=10, seed=1, enable_aspect_composition=False)

    call_count = {"random": 0}

    def fake_portfolio_score(universe, template, params, cfg):
        if isinstance(template, RandomAllocationTemplate):
            call_count["random"] += 1
            # Half of the random trials "fail" (non-finite) -- these must
            # still be counted in n_trials even though they're excluded from
            # random_scores.
            if call_count["random"] % 2 == 0:
                return {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}
            return {"sharpe_ratio": 0.01, "total_rebalances": 4, "total_turnover": 1.0}
        return _portfolio_score(universe, template, params, cfg)

    with patch("stratgen.generator._portfolio_score", side_effect=fake_portfolio_score):
        result = _search_allocation(universe, config)

    expected_grid_trials = sum(len(grid_combinations(t().param_grid)) for t in ALLOCATION_TEMPLATES)
    assert call_count["random"] == config.n_random_search  # sanity: all 10 were actually run
    assert result["n_trials"] == expected_grid_trials + config.n_random_search

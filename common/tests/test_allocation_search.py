"""Unit tests for common/allocation_search.py. Guaranteed 100% offline --
hand-built fake templates/score_fns, no real backtest/market data involved."""

import os
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_search import (
    RandomAllocationTemplate,
    grid_combinations,
    grid_search_template,
    optimize_template,
    random_weights,
    run_ers_validation,
)


def test_grid_combinations_empty_returns_single_degenerate_trial():
    assert grid_combinations({}) == [{}]
    assert grid_combinations(None) == [{}]


def test_grid_combinations_cartesian_product():
    combos = grid_combinations({"a": [1, 2], "b": [10, 20]})
    assert combos == [
        {"a": 1, "b": 10}, {"a": 1, "b": 20},
        {"a": 2, "b": 10}, {"a": 2, "b": 20},
    ]


class _FakeTemplate:
    def __init__(self, name="fake", param_grid=None):
        self.name = name
        self.param_grid = param_grid or {}


def _small_universe():
    idx = pd.bdate_range("2020-01-01", periods=60)
    return {"A": pd.DataFrame({"Close": np.linspace(100, 110, 60)}, index=idx)}


def test_grid_search_template_scores_every_combination():
    template = _FakeTemplate(param_grid={"x": [1, 2, 3]})

    def score_fn(t, params):
        return {"sharpe_ratio": float(params["x"]), "total_rebalances": 5}

    trials = grid_search_template(template, score_fn)
    assert len(trials) == 3
    assert sorted(t["params"]["x"] for t in trials) == [1, 2, 3]
    assert all(t["score"] == t["params"]["x"] for t in trials)


def test_grid_search_template_degenerate_param_grid_yields_one_trial():
    template = _FakeTemplate(param_grid={})

    def score_fn(t, params):
        return {"sharpe_ratio": 1.0, "total_rebalances": 5}

    trials = grid_search_template(template, score_fn)
    assert len(trials) == 1
    assert trials[0]["params"] == {}


def test_grid_search_template_exception_becomes_inf_with_warning():
    template = _FakeTemplate(param_grid={"x": [1]})

    def score_fn(t, params):
        raise ValueError("boom")

    with pytest.warns(RuntimeWarning, match="boom"):
        trials = grid_search_template(template, score_fn)
    assert trials[0]["score"] == float("-inf")
    assert trials[0]["result"] == {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}


def test_run_ers_validation_trusted_when_candidate_clearly_beats_random():
    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            return {"sharpe_ratio": 0.0, "total_rebalances": 10}
        return {"sharpe_ratio": 5.0, "total_rebalances": 10}

    result = run_ers_validation(
        {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 10}, score_fn,
        n_random_search=50, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
    )
    assert result["ers_percentile"] == 1.0
    assert result["ers_passed"] is True
    assert result["trusted"] is True


def test_run_ers_validation_untrusted_when_candidate_does_not_beat_random():
    def score_fn(t, params):
        # Everyone (candidate and random alike) scores the same -- candidate
        # should NOT clear a 0.90 percentile bar.
        return {"sharpe_ratio": 1.0, "total_rebalances": 10}

    result = run_ers_validation(
        {"rebalance_freq_days": 21}, 1.0, {"total_rebalances": 10}, score_fn,
        n_random_search=50, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
    )
    assert result["ers_passed"] is False
    assert result["trusted"] is False


def test_run_ers_validation_min_rebalances_gate():
    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            return {"sharpe_ratio": 0.0, "total_rebalances": 10}
        return {"sharpe_ratio": 5.0, "total_rebalances": 1}  # below min_rebalances_for_trust

    result = run_ers_validation(
        {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 1}, score_fn,
        n_random_search=50, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
    )
    assert result["ers_passed"] is True
    assert result["trusted"] is False


def test_run_ers_validation_empty_random_pool_is_fail_safe_zero_not_one():
    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            return {"sharpe_ratio": float("-inf"), "total_rebalances": 0}
        return {"sharpe_ratio": 5.0, "total_rebalances": 10}

    result = run_ers_validation(
        {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 10}, score_fn,
        n_random_search=20, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
    )
    assert result["ers_percentile"] == 0.0
    assert result["ers_passed"] is False
    assert result["trusted"] is False


def test_optimize_template_picks_best_and_validates(monkeypatch):
    template = _FakeTemplate(param_grid={"rebalance_freq_days": [5, 21]})

    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            return {"sharpe_ratio": -1.0, "total_rebalances": 10}
        # Candidate with rebalance_freq_days=21 is clearly best.
        return {"sharpe_ratio": 10.0 if params["rebalance_freq_days"] == 21 else 0.0, "total_rebalances": 10}

    result = optimize_template(
        _small_universe(), template, score_fn,
        n_random_search=30, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
    )
    assert result["best_params"] == {"rebalance_freq_days": 21}
    assert result["best_score"] == 10.0
    assert result["trusted"] is True
    assert result["n_trials"] == 2 + 30
    assert len(result["all_trials"]) == 2


def test_optimize_template_empty_param_grid_single_trial():
    template = _FakeTemplate(param_grid={})

    def score_fn(t, params):
        return {"sharpe_ratio": 1.0, "total_rebalances": 10}

    result = optimize_template(_small_universe(), template, score_fn, n_random_search=10, seed=0)
    assert result["best_params"] == {}
    assert len(result["all_trials"]) == 1


def test_optimize_template_raises_on_empty_universe():
    template = _FakeTemplate(param_grid={"x": [1]})
    with pytest.raises(ValueError, match="universe must contain"):
        optimize_template({}, template, lambda t, p: {"sharpe_ratio": 1.0}, n_random_search=1)


def test_grid_search_template_all_trials_fail_emits_aggregate_warning():
    # Issue 1: a score_fn that ALWAYS raises must trigger BOTH the per-trial
    # RuntimeWarning(s) (one per trial, naming the exception) AND a SEPARATE,
    # more prominent aggregate warning that every trial failed -- the latter
    # is the loud signal that score_fn itself is likely broken, not just
    # having a run of bad candidates.
    template = _FakeTemplate(name="always_broken", param_grid={"x": [1, 2, 3]})

    def score_fn(t, params):
        raise ValueError("always broken")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        trials = grid_search_template(template, score_fn)

    assert len(trials) == 3
    assert all(t["score"] == float("-inf") for t in trials)

    messages = [str(w.message) for w in caught]
    assert all(issubclass(w.category, RuntimeWarning) for w in caught)
    per_trial = [m for m in messages if "always broken" in m]
    aggregate = [m for m in messages if "ALL" in m and "always_broken" in m]
    assert len(per_trial) == 3
    assert len(aggregate) == 1


def test_grid_search_template_partial_failure_no_aggregate_warning():
    # Normal case: only SOME trials fail -- the aggregate "everything is
    # broken" warning must NOT fire, only the per-trial ones, so it isn't
    # spuriously raised on ordinary partial failures.
    template = _FakeTemplate(name="partially_broken", param_grid={"x": [1, 2, 3]})

    def score_fn(t, params):
        if params["x"] == 2:
            raise ValueError("boom on 2")
        return {"sharpe_ratio": float(params["x"]), "total_rebalances": 5}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        trials = grid_search_template(template, score_fn)

    assert len(trials) == 3
    assert not all(t["score"] == float("-inf") for t in trials)

    messages = [str(w.message) for w in caught]
    per_trial = [m for m in messages if "boom on 2" in m]
    aggregate = [m for m in messages if "ALL" in m and "partially_broken" in m]
    assert len(per_trial) == 1
    assert len(aggregate) == 0


def test_run_ers_validation_all_random_draws_fail_emits_aggregate_warning():
    # Issue 1: a score_fn that always raises for the random template must
    # trigger the aggregate "random pool is EMPTY" warning, distinguishing a
    # broken score_fn from the normal "didn't beat the random pool" outcome.
    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            raise ValueError("random scoring broken")
        return {"sharpe_ratio": 5.0, "total_rebalances": 10}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_ers_validation(
            {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 10}, score_fn,
            n_random_search=10, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
        )

    assert result["ers_percentile"] == 0.0
    assert result["ers_passed"] is False
    assert result["trusted"] is False

    messages = [str(w.message) for w in caught]
    per_trial = [m for m in messages if "random scoring broken" in m]
    aggregate = [m for m in messages if "ALL" in m and "EMPTY" in m]
    assert len(per_trial) == 10
    assert len(aggregate) == 1


def test_run_ers_validation_partial_random_failure_no_aggregate_warning():
    # Normal case: only SOME random draws fail/are non-finite -- the
    # aggregate "empty pool" warning must NOT fire.
    calls = {"n": 0}

    def score_fn(t, params):
        if isinstance(t, RandomAllocationTemplate):
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                raise ValueError("boom every other draw")
            return {"sharpe_ratio": 0.0, "total_rebalances": 10}
        return {"sharpe_ratio": 5.0, "total_rebalances": 10}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_ers_validation(
            {"rebalance_freq_days": 21}, 5.0, {"total_rebalances": 10}, score_fn,
            n_random_search=10, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=0,
        )

    assert result["ers_percentile"] == 1.0

    messages = [str(w.message) for w in caught]
    aggregate = [m for m in messages if "ALL" in m and "EMPTY" in m]
    assert len(aggregate) == 0


def test_random_allocation_template_protocol_surface():
    # Issue 2: RandomAllocationTemplate must expose the full AllocationTemplate
    # protocol (name, param_grid, factor_tags, explain_weights) so a future
    # caller relying on any of these doesn't raise inside _safe_score's try
    # block and silently degrade to -inf.
    from common.allocation_templates import AllocationTemplate

    template = RandomAllocationTemplate(np.random.default_rng(0))
    assert isinstance(template, AllocationTemplate)
    assert template.name == "random_allocation"
    assert template.param_grid == {}
    assert template.factor_tags == []
    explanation = template.explain_weights({"rebalance_freq_days": 21})
    assert isinstance(explanation, str)
    assert "21" in explanation


def test_random_allocation_template_has_warmup_bars():
    # Regression test: backtester.run_walkforward calls template.warmup_bars(params)
    # UNCONDITIONALLY on every template it evaluates, including RandomAllocationTemplate
    # during ERS validation. Without this method, every random draw raised AttributeError
    # (silently caught by _safe_score as -inf, filtered out by np.isfinite in
    # run_ers_validation), leaving the random pool empty and ERS always fail-safe-failing
    # in --mode walkforward -- previously latent since strategy_generator's single-shot
    # scoring never calls warmup_bars, only backtester.run_walkforward does.
    template = RandomAllocationTemplate(np.random.default_rng(0))
    assert template.warmup_bars({"rebalance_freq_days": 21}) == 0


def test_random_weights_sums_to_one_on_rebalance_dates():
    universe = {"A": pd.DataFrame({"Close": range(30)}, index=pd.bdate_range("2020-01-01", periods=30)),
                "B": pd.DataFrame({"Close": range(30)}, index=pd.bdate_range("2020-01-01", periods=30))}
    rng = np.random.default_rng(0)
    weights = random_weights(universe, rebalance_freq_days=5, rng=rng)
    rebal_rows = weights.dropna(how="all")
    np.testing.assert_allclose(rebal_rows.sum(axis=1), 1.0)

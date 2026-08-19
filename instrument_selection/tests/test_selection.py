import numpy as np
import pandas as pd
import pytest
from common.testing import make_ohlcv_from_closes

from selectorbot.correlation import correlation_matrix, returns_matrix
from selectorbot.selection import (
    select_cluster_representatives,
    select_diversified_greedy,
    select_diversified_threshold_greedy,
    select_max_diversification_ratio,
)


def make_price_df(close):
    return make_ohlcv_from_closes(close, spread=0.3)


def _redundant_pair_plus_independent(seed=1, n=500, extra_noise_a=0.0002, extra_noise_b=0.0002):
    """A, B near-identical (highly correlated); C independent -- the
    canonical "naive top-K would double up on A/B" scenario every test
    below is built around. `extra_noise_a`/`extra_noise_b` are each symbol's
    OWN idiosyncratic noise std added on top of the shared `base_returns` --
    keep these small relative to base_returns' own 0.01 std, or A/B stop
    being highly correlated at all (breaking every test's premise)."""
    rng = np.random.default_rng(seed)
    base_returns = rng.normal(0, 0.01, n)
    a_returns = base_returns + rng.normal(0, extra_noise_a, n)
    b_returns = base_returns + rng.normal(0, extra_noise_b, n)
    c_returns = rng.normal(0, 0.01, n)
    data = {
        "A": make_price_df(100 * np.cumprod(1 + a_returns)),
        "B": make_price_df(100 * np.cumprod(1 + b_returns)),
        "C": make_price_df(100 * np.cumprod(1 + c_returns)),
    }
    returns = returns_matrix(data)
    corr = correlation_matrix(returns)
    return data, returns, corr


def test_select_cluster_representatives_picks_one_per_cluster():
    _, _, corr = _redundant_pair_plus_independent(seed=1)
    scores = pd.Series({"A": 80.0, "B": 79.0, "C": 60.0})
    chosen = select_cluster_representatives(scores, corr, distance_threshold=0.5, representative_rule="highest_score")
    assert len(chosen) == 2  # {A, B} collapse to one cluster, C is its own
    assert "C" in chosen
    assert ("A" in chosen) != ("B" in chosen)  # exactly one of the redundant pair


def test_select_cluster_representatives_highest_score_rule_picks_the_higher_scorer():
    _, _, corr = _redundant_pair_plus_independent(seed=2)
    scores = pd.Series({"A": 90.0, "B": 40.0, "C": 60.0})
    chosen = select_cluster_representatives(scores, corr, distance_threshold=0.5, representative_rule="highest_score")
    assert "A" in chosen
    assert "B" not in chosen


def test_select_cluster_representatives_lowest_volatility_rule_picks_the_calmer_one():
    _, returns, corr = _redundant_pair_plus_independent(seed=3, extra_noise_a=0.0, extra_noise_b=0.005)
    scores = pd.Series({"A": 50.0, "B": 90.0, "C": 60.0})  # B scores higher but is noisier
    volatility = returns.std()
    chosen = select_cluster_representatives(scores, corr, distance_threshold=0.5, volatility=volatility)
    assert "A" in chosen  # lowest-volatility rule overrides B's higher score
    assert "B" not in chosen


def test_select_cluster_representatives_lowest_volatility_requires_volatility_series():
    _, _, corr = _redundant_pair_plus_independent(seed=4)
    scores = pd.Series({"A": 80.0, "B": 79.0, "C": 60.0})
    with pytest.raises(ValueError):
        select_cluster_representatives(scores, corr, representative_rule="lowest_volatility", volatility=None)


def test_select_cluster_representatives_defaults_to_lowest_volatility_when_volatility_given():
    _, returns, corr = _redundant_pair_plus_independent(seed=5, extra_noise_a=0.0, extra_noise_b=0.005)
    scores = pd.Series({"A": 50.0, "B": 90.0, "C": 60.0})
    volatility = returns.std()
    chosen = select_cluster_representatives(scores, corr, distance_threshold=0.5, volatility=volatility)
    assert "A" in chosen


def test_select_cluster_representatives_handles_single_symbol_universe_without_crashing():
    # Regression: a 1-symbol universe (e.g. only the benchmark survived hard
    # screening) used to crash inside `correlation.hierarchical_clusters`
    # (scipy ValueError on an empty condensed distance matrix). It must now
    # produce a clean, trivial 1-symbol basket instead.
    corr = pd.DataFrame([[1.0]], index=["ONLY"], columns=["ONLY"])
    scores = pd.Series({"ONLY": 42.0})
    chosen = select_cluster_representatives(scores, corr, distance_threshold=0.5, representative_rule="highest_score")
    assert chosen == ["ONLY"]


def test_select_diversified_greedy_prefers_the_independent_symbol_over_a_near_duplicate():
    _, _, corr = _redundant_pair_plus_independent(seed=6)
    # A and B both score very high but are near-duplicates; C scores lower but is independent.
    scores = pd.Series({"A": 95.0, "B": 94.0, "C": 70.0})
    chosen = select_diversified_greedy(scores, corr, k=2, diversity_weight=50.0)
    assert set(chosen) == {"A", "C"} or set(chosen) == {"B", "C"}
    assert "A" in chosen or "B" in chosen  # the best-scoring seed is always picked first
    assert "C" in chosen  # diversity term should pull in the independent symbol over the near-duplicate


def test_select_diversified_greedy_respects_k():
    _, _, corr = _redundant_pair_plus_independent(seed=7)
    scores = pd.Series({"A": 95.0, "B": 94.0, "C": 70.0})
    chosen = select_diversified_greedy(scores, corr, k=1)
    assert chosen == ["A"]
    chosen_all = select_diversified_greedy(scores, corr, k=10)  # k larger than universe
    assert set(chosen_all) == {"A", "B", "C"}


def test_select_diversified_greedy_with_zero_diversity_weight_matches_naive_top_k():
    _, _, corr = _redundant_pair_plus_independent(seed=8)
    scores = pd.Series({"A": 95.0, "B": 94.0, "C": 70.0})
    chosen = select_diversified_greedy(scores, corr, k=2, diversity_weight=0.0)
    assert chosen == ["A", "B"]  # no diversity pressure -> pure top-K by score


def test_select_diversified_threshold_greedy_skips_the_correlated_lower_priority_candidate():
    _, _, corr = _redundant_pair_plus_independent(seed=9)
    scores = pd.Series({"A": 95.0, "B": 90.0, "C": 60.0})
    chosen = select_diversified_threshold_greedy(scores, corr, max_correlation=0.85)
    assert chosen[0] == "A"        # highest scorer always considered first
    assert "B" not in chosen       # too correlated with the already-selected A
    assert "C" in chosen


def test_select_diversified_threshold_greedy_determines_subset_size_from_data():
    _, _, corr = _redundant_pair_plus_independent(seed=10)
    scores = pd.Series({"A": 95.0, "B": 90.0, "C": 60.0})
    chosen = select_diversified_threshold_greedy(scores, corr, max_correlation=0.85)
    assert len(chosen) == 2  # A + C; B dropped -- size wasn't fixed in advance


def test_select_diversified_threshold_greedy_respects_max_k():
    _, _, corr = _redundant_pair_plus_independent(seed=11)
    scores = pd.Series({"A": 95.0, "B": 60.0, "C": 55.0})
    chosen = select_diversified_threshold_greedy(scores, corr, max_correlation=0.99, max_k=1)
    assert chosen == ["A"]


def test_select_diversified_threshold_greedy_high_threshold_behaves_like_naive_top_k():
    _, _, corr = _redundant_pair_plus_independent(seed=12)
    scores = pd.Series({"A": 95.0, "B": 90.0, "C": 60.0})
    chosen = select_diversified_threshold_greedy(scores, corr, max_correlation=1.01)  # nothing gets filtered
    assert chosen == ["A", "B", "C"]


def test_select_max_diversification_ratio_prefers_uncorrelated_assets():
    _, returns, corr = _redundant_pair_plus_independent(seed=13)
    scores = pd.Series({"A": 95.0, "B": 94.0, "C": 70.0})
    volatility = returns.std()
    chosen = select_max_diversification_ratio(scores, corr, volatility=volatility, k=2, score_weight=0.1)
    # A and B are ~0.99 correlated, C is independent -> DR prefers {A, C} or {B, C}
    assert "C" in chosen
    assert ("A" in chosen) != ("B" in chosen)


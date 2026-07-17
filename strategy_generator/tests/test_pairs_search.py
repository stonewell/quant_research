import numpy as np
import pandas as pd

from stratgen.generator import GeneratorConfig
from stratgen.pairs_search import PairsSearchConfig, search_pairs_candidates


def _cointegrated_pair(n=800, seed=1, noise=0.3, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0, 1, n))
    close_a = 100 + common + rng.normal(0, noise, n)
    close_b = 100 + common + rng.normal(0, noise, n)
    idx = pd.bdate_range(start, periods=n)
    df_a = pd.DataFrame({"Open": close_a, "High": close_a + 0.3, "Low": close_a - 0.3, "Close": close_a}, index=idx)
    df_b = pd.DataFrame({"Open": close_b, "High": close_b + 0.3, "Low": close_b - 0.3, "Close": close_b}, index=idx)
    return df_a, df_b


def _independent_walk(n=800, seed=1, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": close, "High": close + 0.3, "Low": close - 0.3, "Close": close}, index=idx)


def _stationary_filler(n=800, seed=1, start="2015-01-01"):
    """A bounded, mean-reverting (not unit-root) price series -- used as
    "obviously not cointegrated with a trending random walk" filler
    symbols. Two independent random WALKS can spuriously look cointegrated
    over a finite sample purely by chance (the classic Granger-Newbold
    spurious-regression result -- ironically exactly the kind of false
    positive this module's own ERS check exists to catch), so filler
    symbols need to be genuinely non-unit-root, not just "a different
    random walk," for a test to reliably isolate the deliberately
    cointegrated pair."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = -0.3 * x[t - 1] + rng.normal(0, 1)
    close = 100 + x
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": close, "High": close + 0.3, "Low": close - 0.3, "Close": close}, index=idx)


def test_search_pairs_candidates_returns_none_for_fewer_than_two_symbols():
    universe = {"A": _independent_walk(seed=1)}
    assert search_pairs_candidates(universe, GeneratorConfig()) is None


def test_search_pairs_candidates_finds_the_cointegrated_pair_among_noise():
    df_a, df_b = _cointegrated_pair(seed=1)
    universe = {
        "A": df_a, "B": df_b,
        "C": _stationary_filler(seed=100), "D": _stationary_filler(seed=200),
    }
    result = search_pairs_candidates(universe, GeneratorConfig(n_random_search=20, hurst_seed=1))
    assert result is not None
    assert {result.symbol_a, result.symbol_b} == {"A", "B"}
    assert set(result.params.keys()) == {"lookback", "entry_zscore"}
    assert result.n_pairs_total == 6  # C(4,2)
    assert result.n_pairs_searched == 6  # below the default cap, so nothing was sampled/dropped


def test_search_pairs_candidates_respects_max_pairs_to_search_cap():
    universe = {f"S{i}": _independent_walk(seed=i) for i in range(6)}  # C(6,2) = 15 pairs
    pairs_config = PairsSearchConfig(max_pairs_to_search=5, seed=1)
    result = search_pairs_candidates(universe, GeneratorConfig(n_random_search=5), pairs_config)
    assert result.n_pairs_total == 15
    assert result.n_pairs_searched == 5


def test_search_pairs_candidates_reports_ers_and_trust_fields():
    df_a, df_b = _cointegrated_pair(seed=2)
    universe = {"A": df_a, "B": df_b}
    result = search_pairs_candidates(universe, GeneratorConfig(n_random_search=20, hurst_seed=1))
    assert isinstance(result.ers_passed, bool)
    assert 0.0 <= result.ers_percentile <= 1.0
    assert isinstance(result.trusted, bool)
    assert result.trusted == (result.ers_passed and result.num_trades >= 10)
    assert result.n_trials == result.n_pairs_searched * 9 + 20  # 9 = 3 lookbacks x 3 entry_zscores

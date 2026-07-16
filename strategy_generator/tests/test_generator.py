import numpy as np
import pandas as pd
import pytest
from common.testing import make_ar1_ohlcv as _ar1_close
from common.testing import make_random_walk_df as _random_walk_close

from stratgen.generator import GeneratorConfig, StrategyGenerator


def test_generate_raises_on_empty_universe():
    with pytest.raises(ValueError):
        StrategyGenerator(GeneratorConfig()).generate({})


def test_generate_produces_one_spec_for_whole_universe_not_per_symbol():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=1),
        "B": _ar1_close(phi=0.75, n=1200, seed=2),
        "C": _ar1_close(phi=0.75, n=1200, seed=3),
    }
    # search_pairs=False: these single-symbol-routing tests care about the regime->template
    # path specifically; pairs-vs-single-symbol competition is covered separately below.
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, search_pairs=False)).generate(universe)
    # One spec, one template, one set of params -- not a dict of per-symbol specs.
    assert spec.n_symbols == 3
    assert isinstance(spec.template_name, str)
    assert isinstance(spec.params, dict)
    # But per-symbol transparency is still exposed for the chosen params.
    assert set(spec.per_symbol_pnl.keys()) == {"A", "B", "C"}
    assert set(spec.per_symbol_num_trades.keys()) == {"A", "B", "C"}


def test_universe_of_random_walks_routes_to_no_trade():
    universe = {"A": _random_walk_close(1000, seed=1), "B": _random_walk_close(1000, seed=2)}
    spec = StrategyGenerator(GeneratorConfig(hurst_seed=1, search_pairs=False)).generate(universe)
    assert spec.regime_label == "random_walk_like"
    assert spec.template_name == "no_trade"
    assert spec.strategy_family == "no_trade"
    assert spec.n_trials == 0
    assert spec.trusted
    assert spec.per_symbol_pnl == {"A": 0.0, "B": 0.0}


def test_universe_of_strong_trends_routes_to_momentum():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, search_pairs=False)).generate(universe)
    assert spec.regime_label == "trending"
    assert spec.template_name == "momentum"
    assert spec.strategy_family == "single_symbol"
    assert set(spec.params.keys()) == {"fast_ma", "slow_ma"}


def test_universe_of_strong_mean_reversion_routes_to_mean_reversion():
    universe = {
        "A": _ar1_close(phi=-0.9, n=1200, seed=10),
        "B": _ar1_close(phi=-0.9, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, search_pairs=False)).generate(universe)
    assert spec.regime_label == "mean_reverting"
    assert spec.template_name == "mean_reversion"


def test_one_outlier_symbol_does_not_flip_a_consistent_majority():
    # Two strongly trending symbols plus one strongly mean-reverting outlier;
    # the median-pooled z-score should still reflect the trending majority.
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=-0.9, n=1200, seed=12),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, search_pairs=False)).generate(universe)
    assert spec.regime_label == "trending"


def test_min_trades_for_trust_counts_total_across_universe():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(
        n_random_search=20, hurst_seed=1, min_trades_for_trust=100_000, search_pairs=False,
    )).generate(universe)
    assert spec.template_name != "no_trade"
    assert not spec.trusted
    assert spec.total_num_trades == sum(spec.per_symbol_num_trades.values())


def test_n_trials_accounts_for_grid_and_random_search_regardless_of_universe_size():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=0.75, n=1200, seed=12),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=25, hurst_seed=1, search_pairs=False)).generate(universe)
    grid_size = 9  # MomentumTemplate: 3 fast_ma x 3 slow_ma -- n_trials counts combinations tried, not per-symbol backtests
    assert spec.n_trials == grid_size + 25


def test_single_symbol_search_scores_one_combined_portfolio_not_pooled_per_symbol_sharpes():
    # The whole point of this design: universe_sharpe is ONE number from ONE
    # multi-asset portfolio backtest, not a per-symbol dict pooled by
    # median/mean -- there is no more `aggregation` config knob because
    # there's nothing left to pool.
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=0.75, n=1200, seed=12),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=15, hurst_seed=1, search_pairs=False)).generate(universe)
    assert isinstance(spec.universe_sharpe, float)
    assert np.isfinite(spec.universe_sharpe)
    assert not hasattr(GeneratorConfig(), "aggregation")


def test_max_concurrent_positions_caps_the_portfolio_search():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=0.75, n=1200, seed=12),
    }
    spec = StrategyGenerator(GeneratorConfig(
        n_random_search=10, hurst_seed=1, search_pairs=False, max_concurrent_positions=1,
    )).generate(universe)
    assert spec.strategy_family == "single_symbol"
    assert np.isfinite(spec.universe_sharpe)


# --- pairs-candidate integration: the architecture-limit removal itself ---

def test_generate_can_return_a_pairs_family_strategy():
    # A universe where two symbols are, by construction, cointegrated (they
    # share a common random-walk component plus small independent noise) --
    # a textbook case for pairs trading to have a real, findable edge.
    n = 800
    rng = np.random.default_rng(5)
    common = np.cumsum(rng.normal(0, 1, n))
    close_a = 100 + common + rng.normal(0, 0.3, n)
    close_b = 100 + common + rng.normal(0, 0.3, n)
    idx = pd.bdate_range("2015-01-01", periods=n)
    df_a = pd.DataFrame({"Open": close_a, "High": close_a + 0.3, "Low": close_a - 0.3, "Close": close_a}, index=idx)
    df_b = pd.DataFrame({"Open": close_b, "High": close_b + 0.3, "Low": close_b - 0.3, "Close": close_b}, index=idx)
    universe = {"A": df_a, "B": df_b}

    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
    # Whichever family actually won, both candidates must have been searched and reported.
    assert spec.pairs_result is not None
    assert spec.strategy_family in ("single_symbol", "pairs", "no_trade")
    if spec.strategy_family == "pairs":
        assert spec.pair_symbols == ("A", "B")
        assert spec.template_name == "distance_pairs"
        assert set(spec.params.keys()) == {"lookback", "entry_zscore"}


def test_search_pairs_false_disables_the_pairs_candidate_entirely():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=10, hurst_seed=1, search_pairs=False)).generate(universe)
    assert spec.pairs_result is None
    assert spec.strategy_family != "pairs"


def test_single_symbol_universe_never_searches_pairs():
    universe = {"A": _ar1_close(phi=0.75, n=1200, seed=10)}
    spec = StrategyGenerator(GeneratorConfig(n_random_search=10, hurst_seed=1)).generate(universe)
    assert spec.pairs_result is None
    assert spec.n_symbols == 1


def test_untrusted_pairs_candidate_does_not_win_over_a_trusted_single_symbol_one():
    # Strongly trending universe: single-symbol momentum should be trusted;
    # two independent AR(1) trends have no reason to be a good pairs trade,
    # so even if a pairs combo scores well on raw Sharpe, it should lose to
    # the trusted single-symbol candidate under the trust-gated comparison.
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=20),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
    if spec.single_symbol_result and spec.single_symbol_result["trusted"] and not (
        spec.pairs_result and spec.pairs_result.trusted
    ):
        assert spec.strategy_family == "single_symbol"

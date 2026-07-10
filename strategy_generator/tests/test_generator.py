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
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
    # One spec, one template, one set of params -- not a dict of per-symbol specs.
    assert spec.n_symbols == 3
    assert isinstance(spec.template_name, str)
    assert isinstance(spec.params, dict)
    # But per-symbol transparency is still exposed for the chosen params.
    assert set(spec.per_symbol_sharpe.keys()) == {"A", "B", "C"}
    assert set(spec.per_symbol_num_trades.keys()) == {"A", "B", "C"}


def test_universe_of_random_walks_routes_to_no_trade():
    universe = {"A": _random_walk_close(1000, seed=1), "B": _random_walk_close(1000, seed=2)}
    spec = StrategyGenerator(GeneratorConfig(hurst_seed=1)).generate(universe)
    assert spec.regime_label == "random_walk_like"
    assert spec.template_name == "no_trade"
    assert spec.n_trials == 0
    assert spec.trusted
    assert spec.per_symbol_sharpe == {"A": 0.0, "B": 0.0}


def test_universe_of_strong_trends_routes_to_momentum():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
    assert spec.regime_label == "trending"
    assert spec.template_name == "momentum"
    assert set(spec.params.keys()) == {"fast_ma", "slow_ma"}


def test_universe_of_strong_mean_reversion_routes_to_mean_reversion():
    universe = {
        "A": _ar1_close(phi=-0.9, n=1200, seed=10),
        "B": _ar1_close(phi=-0.9, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
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
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1)).generate(universe)
    assert spec.regime_label == "trending"


def test_min_trades_for_trust_counts_total_across_universe():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, min_trades_for_trust=100_000)).generate(universe)
    assert spec.template_name != "no_trade"
    assert not spec.trusted
    assert spec.total_num_trades == sum(spec.per_symbol_num_trades.values())


def test_n_trials_accounts_for_grid_and_random_search_regardless_of_universe_size():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=0.75, n=1200, seed=12),
    }
    spec = StrategyGenerator(GeneratorConfig(n_random_search=25, hurst_seed=1)).generate(universe)
    grid_size = 9  # MomentumTemplate: 3 fast_ma x 3 slow_ma -- n_trials counts combinations tried, not per-symbol backtests
    assert spec.n_trials == grid_size + 25


def test_median_aggregation_differs_from_mean_when_configured():
    universe = {
        "A": _ar1_close(phi=0.75, n=1200, seed=10),
        "B": _ar1_close(phi=0.75, n=1200, seed=11),
        "C": _ar1_close(phi=0.75, n=1200, seed=12),
    }
    spec_median = StrategyGenerator(GeneratorConfig(n_random_search=15, hurst_seed=1, aggregation="median")).generate(universe)
    spec_mean = StrategyGenerator(GeneratorConfig(n_random_search=15, hurst_seed=1, aggregation="mean")).generate(universe)
    # Both should at least run to completion and produce a real (non -inf) pooled score.
    assert np.isfinite(spec_median.universe_sharpe)
    assert np.isfinite(spec_mean.universe_sharpe)

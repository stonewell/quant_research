import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes as make_df

from stratgen.generator import GeneratorConfig, StrategyGenerator


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

    # A completely random universe with zero mean return (pure noise around 100)
    rng = np.random.default_rng(123)
    closes_a = 100 + rng.normal(0, 0.5, 300)
    closes_b = 100 + rng.normal(0, 0.5, 300)

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    config = GeneratorConfig(n_random_search=100, ers_percentile_threshold=0.99, seed=123)
    gen = StrategyGenerator(config)

    spec = gen.generate(universe)

    # On pure mean-reverting noise around a constant mean, no trend or allocation strategy has a significant edge over random search
    assert spec.trusted is False
    assert spec.ers_passed is False

import numpy as np
import pandas as pd

from stratgen.generator import GeneratorConfig, StrategyGenerator


def _ar1_close(phi, n, seed, scale=0.3):
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, 1, n)
    increments = np.zeros(n)
    for t in range(1, n):
        increments[t] = phi * increments[t - 1] + eps[t]
    close = 100 + np.cumsum(increments * scale)
    idx = pd.bdate_range("2015-01-01", periods=n)
    high = close + np.abs(rng.normal(0.3, 0.1, n))
    low = close - np.abs(rng.normal(0.3, 0.1, n))
    open_ = close + rng.normal(0, 0.1, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_random_walk_routes_to_no_trade_with_zero_trials():
    df = pd.DataFrame({
        "Open": 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 1000)),
    })
    df["Close"] = df["Open"]
    df["High"] = df["Close"] + 0.5
    df["Low"] = df["Close"] - 0.5
    df.index = pd.bdate_range("2015-01-01", periods=len(df))

    spec = StrategyGenerator(GeneratorConfig(hurst_seed=1)).generate(df)
    assert spec.regime_label == "random_walk_like"
    assert spec.template_name == "no_trade"
    assert spec.n_trials == 0
    assert spec.trusted


def test_strong_trend_routes_to_momentum_template():
    df = _ar1_close(phi=0.75, n=1200, seed=42)
    spec = StrategyGenerator(GeneratorConfig(n_random_search=30, hurst_seed=1)).generate(df)
    assert spec.regime_label == "trending"
    assert spec.template_name == "momentum"
    assert set(spec.params.keys()) == {"fast_ma", "slow_ma"}


def test_strong_mean_reversion_routes_to_mean_reversion_template():
    df = _ar1_close(phi=-0.9, n=1200, seed=42)
    spec = StrategyGenerator(GeneratorConfig(n_random_search=30, hurst_seed=1)).generate(df)
    assert spec.regime_label == "mean_reverting"
    assert spec.template_name == "mean_reversion"
    assert set(spec.params.keys()) == {"entry_threshold", "exit_threshold"}


def test_ers_percentile_is_bounded_0_to_1():
    df = _ar1_close(phi=0.6, n=1200, seed=7)
    spec = StrategyGenerator(GeneratorConfig(n_random_search=30, hurst_seed=1)).generate(df)
    assert 0.0 <= spec.ers_percentile <= 1.0


def test_low_trade_count_is_flagged_untrusted_even_if_ers_passes():
    df = _ar1_close(phi=0.75, n=1200, seed=42)
    # A very high min_trades_for_trust threshold should be nearly impossible to clear.
    spec = StrategyGenerator(GeneratorConfig(n_random_search=20, hurst_seed=1, min_trades_for_trust=100_000)).generate(df)
    assert spec.template_name != "no_trade"  # this phi/seed is chosen specifically to land on the momentum template
    assert not spec.trusted


def test_n_trials_accounts_for_grid_and_random_search():
    df = _ar1_close(phi=0.75, n=1200, seed=42)
    spec = StrategyGenerator(GeneratorConfig(n_random_search=25, hurst_seed=1)).generate(df)
    grid_size = 9  # MomentumTemplate: 3 fast_ma x 3 slow_ma
    assert spec.n_trials == grid_size + 25

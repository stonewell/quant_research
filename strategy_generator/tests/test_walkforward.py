import numpy as np
import pandas as pd
import pytest

from stratgen.generator import GeneratorConfig
from stratgen.walkforward import WalkForwardConfig, generate_folds, run_walkforward


def test_generate_folds_produces_chronologically_ordered_non_overlapping_windows():
    config = WalkForwardConfig(train_years=1.0, validation_years=0.5, test_years=0.25,
                                embargo_days=10, warmup_buffer_days=50)
    folds = generate_folds(n_bars=2000, config=config)
    assert len(folds) > 0
    for fold in folds:
        assert fold["buffer_start"] < fold["train_start"] < fold["train_end"]
        assert fold["train_end"] == fold["validation_start"]
        assert fold["validation_start"] < fold["validation_end"]
        assert fold["validation_end"] + config.embargo_days == fold["test_start"]
        assert fold["test_start"] < fold["test_end"]


def test_generate_folds_step_size_matches_test_window():
    config = WalkForwardConfig(train_years=1.0, validation_years=0.5, test_years=0.25,
                                embargo_days=5, warmup_buffer_days=50)
    folds = generate_folds(n_bars=3000, config=config)
    assert len(folds) >= 2
    step = folds[1]["buffer_start"] - folds[0]["buffer_start"]
    expected_step = int(round(config.test_years * 252))
    assert step == expected_step


def test_generate_folds_empty_when_data_too_short():
    config = WalkForwardConfig(train_years=4.0, validation_years=2.0, test_years=1.0)
    folds = generate_folds(n_bars=100, config=config)
    assert folds == []


def _ar1_close(phi, n, seed, scale=0.3):
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, 1, n)
    increments = np.zeros(n)
    for t in range(1, n):
        increments[t] = phi * increments[t - 1] + eps[t]
    close = 100 + np.cumsum(increments * scale)
    idx = pd.bdate_range("2010-01-01", periods=n)
    high = close + np.abs(rng.normal(0.3, 0.1, n))
    low = close - np.abs(rng.normal(0.3, 0.1, n))
    open_ = close + rng.normal(0, 0.1, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_run_walkforward_end_to_end_on_synthetic_trend():
    df = _ar1_close(phi=0.5, n=2500, seed=5)
    config = WalkForwardConfig(train_years=1.5, validation_years=0.7, test_years=0.5, embargo_days=10,
                                warmup_buffer_days=200,
                                generator_config=GeneratorConfig(n_random_search=10, hurst_seed=2, min_trades_for_trust=3))
    result = run_walkforward(df, config)
    assert result["n_folds"] > 0
    assert len(result["folds"]) == result["n_folds"]
    assert np.isfinite(result["mean_validation_sharpe"])
    assert np.isfinite(result["mean_test_sharpe"])
    for fold in result["folds"]:
        assert fold["regime_label"] in ("trending", "mean_reverting", "random_walk_like")


def test_run_walkforward_raises_when_no_folds_fit():
    df = _ar1_close(phi=0.5, n=100, seed=5)
    with pytest.raises(ValueError):
        run_walkforward(df, WalkForwardConfig())


def test_run_walkforward_dsr_is_a_probability_when_present():
    df = _ar1_close(phi=0.5, n=2500, seed=6)
    config = WalkForwardConfig(train_years=1.5, validation_years=0.7, test_years=0.5, embargo_days=10,
                                warmup_buffer_days=200,
                                generator_config=GeneratorConfig(n_random_search=10, hurst_seed=2, min_trades_for_trust=3))
    result = run_walkforward(df, config)
    if result["deflated_sharpe_ratio"] is not None:
        assert 0.0 <= result["deflated_sharpe_ratio"] <= 1.0

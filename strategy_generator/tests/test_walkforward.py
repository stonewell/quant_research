import numpy as np
import pandas as pd
import pytest
from common.testing import make_ar1_ohlcv

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
    assert generate_folds(n_bars=100, config=config) == []


def _ar1_close(phi, n, seed, scale=0.01):
    return make_ar1_ohlcv(phi, n, seed, scale, start="2010-01-01")


def _small_wf_config(seed=2):
    return WalkForwardConfig(train_years=1.5, validation_years=0.7, test_years=0.5, embargo_days=10,
                              warmup_buffer_days=200,
                              generator_config=GeneratorConfig(n_random_search=10, hurst_seed=seed, min_trades_for_trust=3))


def test_run_walkforward_raises_on_empty_universe():
    with pytest.raises(ValueError):
        run_walkforward({}, WalkForwardConfig())


def test_run_walkforward_raises_on_misaligned_universe():
    universe = {"A": _ar1_close(0.5, 2500, seed=1), "B": _ar1_close(0.5, 2400, seed=2)}
    with pytest.raises(ValueError):
        run_walkforward(universe, WalkForwardConfig())


def test_run_walkforward_end_to_end_on_synthetic_universe():
    universe = {"A": _ar1_close(0.5, 2500, seed=5), "B": _ar1_close(0.5, 2500, seed=6)}
    result = run_walkforward(universe, _small_wf_config())
    assert result["n_symbols"] == 2
    assert result["n_folds"] > 0
    assert len(result["folds"]) == result["n_folds"]
    assert np.isfinite(result["mean_validation_sharpe"])
    assert np.isfinite(result["mean_test_sharpe"])
    for fold in result["folds"]:
        assert fold["regime_label"] in ("trending", "mean_reverting", "random_walk_like")
        assert "pooled_hurst_z" in fold


def test_run_walkforward_matches_single_symbol_universe_as_a_sanity_check():
    # A one-symbol "universe" should behave like validating that symbol alone.
    universe = {"A": _ar1_close(0.5, 2500, seed=7)}
    result = run_walkforward(universe, _small_wf_config())
    assert result["n_symbols"] == 1
    assert result["n_folds"] > 0


def test_run_walkforward_raises_when_no_folds_fit():
    universe = {"A": _ar1_close(0.5, 100, seed=5), "B": _ar1_close(0.5, 100, seed=6)}
    with pytest.raises(ValueError):
        run_walkforward(universe, WalkForwardConfig())


def test_run_walkforward_dsr_is_a_probability_when_present():
    universe = {"A": _ar1_close(0.5, 2500, seed=8), "B": _ar1_close(0.5, 2500, seed=9)}
    result = run_walkforward(universe, _small_wf_config())
    if result["deflated_sharpe_ratio"] is not None:
        assert 0.0 <= result["deflated_sharpe_ratio"] <= 1.0


# --- portfolio-scoring rewire: matches generator.py's generate() methodology ---

def test_fold_results_report_test_num_trades_and_test_num_bars_separately():
    # These used to be conflated into one field (bar count doing double duty
    # as a trade-count proxy) -- the fix that matched generate()'s actual
    # trade-count semantics needs test_num_bars to stay separately available
    # for DSR's n_obs, which wants the return-series sample size, not trades.
    universe = {"A": _ar1_close(0.5, 2500, seed=5), "B": _ar1_close(0.5, 2500, seed=6)}
    result = run_walkforward(universe, _small_wf_config())
    for fold in result["folds"]:
        assert "test_num_trades" in fold
        assert "test_num_bars" in fold
        if fold["template_name"] != "no_trade":
            # A fold's test window has far more trading days than round-trip trades.
            assert fold["test_num_trades"] <= fold["test_num_bars"]


def test_min_trades_for_trust_uses_actual_trade_count_not_bar_count():
    # A test window of ~0.5 years has roughly 125 trading days but a
    # trend-following template holding for weeks/months will realistically
    # produce far fewer than 125 round-trip trades in that window -- setting
    # min_trades_for_trust between those two numbers should read as
    # untrusted. Before the fix, `test_num_trades` was actually a bar count,
    # so this threshold would have been satisfied by bars alone regardless
    # of how few real trades occurred.
    universe = {"A": _ar1_close(0.75, 2500, seed=10), "B": _ar1_close(0.75, 2500, seed=11)}
    config = WalkForwardConfig(
        train_years=1.5, validation_years=0.7, test_years=0.5, embargo_days=10, warmup_buffer_days=200,
        generator_config=GeneratorConfig(n_random_search=5, hurst_seed=1, min_trades_for_trust=80),
    )
    result = run_walkforward(universe, config)
    for fold in result["folds"]:
        if fold["template_name"] != "no_trade":
            assert fold["test_num_bars"] > 80  # far more trading days than trades in a ~0.5yr window
            if fold["test_num_trades"] < 80:
                assert not fold["trusted"]


def test_max_concurrent_positions_is_forwarded_to_the_portfolio_search():
    universe = {
        "A": _ar1_close(0.5, 2500, seed=5), "B": _ar1_close(0.5, 2500, seed=6), "C": _ar1_close(0.5, 2500, seed=7),
    }
    config = WalkForwardConfig(
        train_years=1.5, validation_years=0.7, test_years=0.5, embargo_days=10, warmup_buffer_days=200,
        generator_config=GeneratorConfig(n_random_search=5, hurst_seed=1, max_concurrent_positions=1),
    )
    result = run_walkforward(universe, config)
    assert result["n_folds"] > 0  # runs to completion with a restrictive slot cap, doesn't crash

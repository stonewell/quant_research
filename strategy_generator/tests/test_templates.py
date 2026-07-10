import numpy as np
import pandas as pd

from stratgen.indicators import rsi, sma
from stratgen.templates import MeanReversionTemplate, MomentumTemplate, NoTradeTemplate


def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    }, index=idx)


def test_momentum_template_matches_manual_ma_state():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = make_df(closes)
    template = MomentumTemplate()
    params = {"fast_ma": 20, "slow_ma": 100}

    result = template.signals(df, params)
    fast, slow = sma(df["Close"], 20), sma(df["Close"], 100)
    expected_entry = (fast > slow).fillna(False)
    pd.testing.assert_series_equal(result["entry_signal"], expected_entry, check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], (~expected_entry).fillna(False), check_names=False)


def test_mean_reversion_template_matches_manual_rsi_thresholds():
    rng = np.random.default_rng(2)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = make_df(closes)
    template = MeanReversionTemplate(rsi_period=2)
    params = {"entry_threshold": 10, "exit_threshold": 70}

    result = template.signals(df, params)
    r = rsi(df["Close"], 2)
    pd.testing.assert_series_equal(result["entry_signal"], (r < 10).fillna(False), check_names=False)
    pd.testing.assert_series_equal(result["exit_signal"], (r > 70).fillna(False), check_names=False)


def test_no_trade_template_never_signals():
    df = make_df(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 200)))
    result = NoTradeTemplate().signals(df, {})
    assert not result["entry_signal"].any()
    assert not result["exit_signal"].any()


def test_momentum_and_meanrev_param_grids_are_small():
    # Deliberately constrained search space (2 free params each), per the
    # research-documented mitigation against unconstrained/GP-style search.
    assert len(MomentumTemplate().param_grid) == 2
    assert len(MeanReversionTemplate().param_grid) == 2

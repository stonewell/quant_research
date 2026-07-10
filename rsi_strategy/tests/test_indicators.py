import numpy as np
import pandas as pd
import pytest

from rsibot.indicators import cumulative_rsi, rsi, rsi_cutler, rsi_wilder, sma


def test_rsi_is_100_when_all_gains():
    close = pd.Series(np.arange(1, 30, dtype=float))  # strictly increasing
    result = rsi_wilder(close, period=14)
    assert np.isclose(result.iloc[-1], 100.0)


def test_rsi_is_0_when_all_losses():
    close = pd.Series(np.arange(30, 1, -1, dtype=float))  # strictly decreasing
    result = rsi_wilder(close, period=14)
    assert np.isclose(result.iloc[-1], 0.0)


def test_rsi_bounded_0_to_100_for_noisy_series():
    rng = np.random.default_rng(1)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    result = rsi_wilder(close, period=2)
    valid = result.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi_has_warmup_nans():
    close = pd.Series(np.linspace(100, 110, 20))
    result = rsi_wilder(close, period=5)
    assert result.iloc[:5].isna().all()
    assert not result.iloc[6:].isna().any()


def test_wilder_and_cutler_diverge_on_mixed_series():
    rng = np.random.default_rng(2)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 100)))
    wilder = rsi_wilder(close, period=5).dropna()
    cutler = rsi_cutler(close, period=5).dropna()
    common = wilder.index.intersection(cutler.index)
    # Different smoothing methods should not produce an identical series.
    assert not np.allclose(wilder.loc[common], cutler.loc[common])


def test_rsi_dispatch_raises_on_unknown_method():
    close = pd.Series(np.arange(1, 10, dtype=float))
    with pytest.raises(ValueError):
        rsi(close, period=2, method="bogus")


def test_cumulative_rsi_is_rolling_sum():
    r = pd.Series([50.0, 20.0, 5.0, 5.0, 90.0])
    result = cumulative_rsi(r, lookback=2)
    assert pd.isna(result.iloc[0])
    assert np.isclose(result.iloc[1], 70.0)
    assert np.isclose(result.iloc[2], 25.0)
    assert np.isclose(result.iloc[4], 95.0)


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, period=2)
    assert np.isclose(result.iloc[1], 1.5)
    assert np.isclose(result.iloc[-1], 4.5)

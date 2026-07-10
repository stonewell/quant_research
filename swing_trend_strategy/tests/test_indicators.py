import numpy as np
import pandas as pd

from swingbot.indicators import rsi, sma


def test_rsi_is_100_when_all_gains():
    close = pd.Series(np.arange(1, 30, dtype=float))
    result = rsi(close, period=14)
    assert np.isclose(result.iloc[-1], 100.0)


def test_rsi_is_0_when_all_losses():
    close = pd.Series(np.arange(30, 1, -1, dtype=float))
    result = rsi(close, period=14)
    assert np.isclose(result.iloc[-1], 0.0)


def test_rsi_bounded_0_to_100():
    rng = np.random.default_rng(4)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    result = rsi(close, period=5).dropna()
    assert (result >= 0).all() and (result <= 100).all()


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, period=2)
    assert np.isclose(result.iloc[1], 1.5)
    assert np.isclose(result.iloc[-1], 4.5)
    assert pd.isna(result.iloc[0])

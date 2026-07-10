import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes

from ensemblebot.indicators import adx, rsi, sma


def make_df(closes):
    return make_ohlcv_from_closes(closes, use_index=False)


def test_rsi_is_100_when_all_gains():
    close = pd.Series(np.arange(1, 30, dtype=float))
    result = rsi(close, period=14)
    assert np.isclose(result.iloc[-1], 100.0)


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, period=2)
    assert np.isclose(result.iloc[1], 1.5)
    assert pd.isna(result.iloc[0])


def test_adx_is_low_in_a_flat_choppy_market():
    rng = np.random.default_rng(1)
    closes = 100 + rng.normal(0, 1, 300)  # no drift -> should not sustain a trend
    df = make_df(closes)
    result = adx(df, period=14).dropna()
    assert result.mean() < 25


def test_adx_is_high_in_a_strong_sustained_trend():
    closes = 100 + np.arange(300) * 0.5  # smooth, strong, sustained uptrend
    df = make_df(closes)
    result = adx(df, period=14).dropna()
    assert result.iloc[-1] > 25


def test_adx_bounded_0_to_100():
    rng = np.random.default_rng(2)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = make_df(closes)
    result = adx(df, period=14).dropna()
    assert (result >= 0).all() and (result <= 100).all()

import numpy as np
import pandas as pd

from stratgen.indicators import atr, atr_pct, rsi, sma


def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    })


def test_rsi_is_100_when_all_gains():
    close = pd.Series(np.arange(1, 30, dtype=float))
    assert np.isclose(rsi(close, period=14).iloc[-1], 100.0)


def test_rsi_is_0_when_all_losses():
    close = pd.Series(np.arange(30, 1, -1, dtype=float))
    assert np.isclose(rsi(close, period=14).iloc[-1], 0.0)


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, period=2)
    assert np.isclose(result.iloc[1], 1.5)
    assert pd.isna(result.iloc[0])


def test_atr_pct_is_positive():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 1, 200))
    result = atr_pct(make_df(closes), period=14).dropna()
    assert (result > 0).all()


def test_atr_constant_range_converges_to_known_value():
    df = make_df(np.full(30, 100.0))  # High-Low always 1.0
    result = atr(df, period=14)
    assert np.isclose(result.iloc[-1], 1.0, atol=1e-6)

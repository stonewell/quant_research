import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes

from gridbot.indicators import atr, sma, trend_regime


def make_df(closes):
    return make_ohlcv_from_closes(closes, spread=1.0, use_index=False)


def test_atr_constant_range_bar():
    # With High-Low always 2 and no gaps, true range is always 2 -> ATR converges to 2.
    df = make_df(np.full(30, 100.0))
    result = atr(df, period=14)
    assert np.isclose(result.iloc[-1], 2.0, atol=1e-6)


def test_atr_has_warmup_nans():
    df = make_df(np.linspace(100, 110, 20))
    result = atr(df, period=14)
    assert result.iloc[:13].isna().all()
    assert not result.iloc[14:].isna().any()


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(s, period=2)
    assert np.isclose(result.iloc[1], 1.5)
    assert np.isclose(result.iloc[-1], 4.5)
    assert pd.isna(result.iloc[0])


def test_trend_regime_classification():
    # Flat prices around 100 should be 'range'; a sustained rally above the
    # band should classify as 'up'.
    flat = pd.Series(np.full(150, 100.0))
    regime = trend_regime(flat, ma_period=100, band_pct=0.03)
    assert regime.iloc[-1] == "range"

    rally = pd.Series(np.concatenate([np.full(100, 100.0), np.linspace(100, 140, 50)]))
    regime = trend_regime(rally, ma_period=100, band_pct=0.03)
    assert regime.iloc[-1] == "up"

import numpy as np
import pandas as pd
from common.testing import make_ohlcv_from_closes

from stratgen.indicators import (
    atr,
    atr_pct,
    bollinger_bands,
    cci,
    ema,
    obv,
    rsi,
    sma,
    stochastic_oscillator,
    williams_r,
)


def make_df(closes):
    return make_ohlcv_from_closes(closes, use_index=False)


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


def test_ema_converges_to_constant_on_flat_series():
    close = pd.Series(np.full(30, 100.0))
    assert np.isclose(ema(close, period=10).iloc[-1], 100.0)


def test_bollinger_bands_pctb_identity_holds():
    # pctb is DEFINED as (close - lower) / (upper - lower); verify the
    # returned columns satisfy that identity exactly (a pure algebraic
    # check, robust regardless of the specific random data).
    rng = np.random.default_rng(3)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 100)))
    bb = bollinger_bands(close, period=20, num_std=2.0).dropna()
    reconstructed = bb["lower"] + bb["pctb"] * (bb["upper"] - bb["lower"])
    np.testing.assert_allclose(reconstructed, close.loc[bb.index], atol=1e-9)


def test_bollinger_bandwidth_widens_with_volatility():
    flat = pd.Series(np.full(60, 100.0))
    rng = np.random.default_rng(4)
    volatile = pd.Series(100 + np.cumsum(rng.normal(0, 2.0, 60)))
    flat_bw = bollinger_bands(flat, period=20).iloc[-1]["bandwidth"]
    volatile_bw = bollinger_bands(volatile, period=20).iloc[-1]["bandwidth"]
    assert flat_bw == 0.0
    assert volatile_bw > flat_bw


def test_stochastic_k_is_100_at_period_high():
    # spread=0.0 -> High=Low=Close, so "last close is the period high/low" is
    # exactly true (a nonzero spread offsets High/Low from Close by a fixed
    # amount, which breaks that identity for a linearly-ramping series).
    closes = np.linspace(100, 200, 30)
    df = make_ohlcv_from_closes(closes, spread=0.0, use_index=False)
    result = stochastic_oscillator(df, k_period=14, d_period=3)
    assert np.isclose(result["k"].iloc[-1], 100.0)


def test_stochastic_k_is_0_at_period_low():
    closes = np.linspace(200, 100, 30)
    df = make_ohlcv_from_closes(closes, spread=0.0, use_index=False)
    result = stochastic_oscillator(df, k_period=14, d_period=3)
    assert np.isclose(result["k"].iloc[-1], 0.0)


def test_cci_positive_when_price_spikes_above_its_recent_average():
    closes = np.concatenate([np.full(25, 100.0), [130.0]])
    df = make_df(closes)
    assert cci(df, period=20).iloc[-1] > 0


def test_williams_r_is_0_at_period_high():
    closes = np.linspace(100, 200, 30)
    df = make_ohlcv_from_closes(closes, spread=0.0, use_index=False)
    assert np.isclose(williams_r(df, period=14).iloc[-1], 0.0)


def test_williams_r_is_minus100_at_period_low():
    closes = np.linspace(200, 100, 30)
    df = make_ohlcv_from_closes(closes, spread=0.0, use_index=False)
    assert np.isclose(williams_r(df, period=14).iloc[-1], -100.0)


def test_obv_matches_manual_calculation():
    df = make_df(np.array([100.0, 102.0, 101.0, 101.0, 105.0]))
    df["Volume"] = [1000.0, 500.0, 300.0, 200.0, 400.0]
    result = obv(df)
    # day0: 0 (no prior close) -> diff=NaN->sign=0
    # day1: up (+500) -> 500
    # day2: down (-300) -> 200
    # day3: flat (0) -> 200
    # day4: up (+400) -> 600
    expected = [0.0, 500.0, 200.0, 200.0, 600.0]
    np.testing.assert_allclose(result.to_numpy(), expected)

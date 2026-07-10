import numpy as np
import pandas as pd
import pytest

from selectorbot.volatility import adx, atr, atr_pct, atr_regime_ratio, realized_vol, vol_of_vol


def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    })


def test_realized_vol_matches_known_std():
    rng = np.random.default_rng(1)
    daily_std = 0.02
    returns = rng.normal(0, daily_std, 500)
    close = pd.Series(100 * np.cumprod(1 + returns))
    result = realized_vol(close, window=250).dropna()
    expected = daily_std * np.sqrt(252)
    assert result.iloc[-1] == pytest.approx(expected, rel=0.15)


def test_atr_pct_is_positive():
    rng = np.random.default_rng(2)
    closes = 100 + np.cumsum(rng.normal(0, 1, 200))
    df = make_df(closes)
    result = atr_pct(df, period=14).dropna()
    assert (result > 0).all()


def test_vol_of_vol_zero_for_constant_volatility_series():
    # A pure random walk with constant-variance innovations should show
    # roughly stable realized vol over time -> low (not necessarily exactly
    # zero) vol-of-vol relative to a regime-switching series.
    rng = np.random.default_rng(3)
    stable_returns = rng.normal(0, 0.01, 1000)
    stable_close = pd.Series(100 * np.cumprod(1 + stable_returns))

    switching_returns = np.concatenate([rng.normal(0, 0.005, 500), rng.normal(0, 0.03, 500)])
    switching_close = pd.Series(100 * np.cumprod(1 + switching_returns))

    stable_vov = vol_of_vol(stable_close).dropna().mean()
    switching_vov = vol_of_vol(switching_close).dropna().mean()
    assert switching_vov > stable_vov


def test_adx_high_in_strong_trend_low_in_chop():
    trend_closes = 100 + np.arange(300) * 0.5
    chop_closes = 100 + np.random.default_rng(4).normal(0, 1, 300)
    trend_adx = adx(make_df(trend_closes), period=14).dropna().iloc[-1]
    chop_adx = adx(make_df(chop_closes), period=14).dropna().mean()
    assert trend_adx > 25
    assert chop_adx < 25


def test_atr_regime_ratio_flags_volatility_spike():
    calm = 100 + np.cumsum(np.random.default_rng(5).normal(0, 0.3, 150))
    spike = calm[-1] + np.cumsum(np.random.default_rng(6).normal(0, 3.0, 40))
    closes = np.concatenate([calm, spike])
    df = make_df(closes)
    ratio = atr_regime_ratio(df, period=14, short_window=10, long_window=60)
    assert ratio.dropna().iloc[-10] > 1.3

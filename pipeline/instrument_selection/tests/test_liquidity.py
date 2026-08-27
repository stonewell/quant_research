import numpy as np
import pandas as pd
import pytest

from selectorbot.liquidity import avg_dollar_volume, corwin_schultz_spread, liquidity_summary


def make_df(n=300, seed=1, spread_scale=0.002):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    half_spread = close * spread_scale
    high = close + half_spread + np.abs(rng.normal(0, 0.1, n))
    low = close - half_spread - np.abs(rng.normal(0, 0.1, n))
    open_ = close
    volume = rng.uniform(1e6, 2e6, n)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)


def test_corwin_schultz_spread_is_non_negative():
    df = make_df()
    spread = corwin_schultz_spread(df)
    assert (spread.dropna() >= 0).all()


def test_corwin_schultz_spread_increases_with_wider_high_low_noise():
    tight = make_df(seed=2, spread_scale=0.0005)
    wide = make_df(seed=2, spread_scale=0.01)
    tight_spread = corwin_schultz_spread(tight).dropna().median()
    wide_spread = corwin_schultz_spread(wide).dropna().median()
    assert wide_spread > tight_spread


def test_avg_dollar_volume_matches_manual_calculation():
    df = make_df()
    result = avg_dollar_volume(df, window=10)
    expected = (df["Close"] * df["Volume"]).rolling(10, min_periods=10).mean()
    pd.testing.assert_series_equal(result, expected)


def test_liquidity_summary_keys_present():
    df = make_df()
    summary = liquidity_summary(df, window=20)
    for key in ["avg_dollar_volume", "median_dollar_volume", "median_spread_pct", "spread_pct_p90"]:
        assert key in summary
        assert not pd.isna(summary[key])

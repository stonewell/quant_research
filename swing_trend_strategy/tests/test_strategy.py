import numpy as np
import pandas as pd

from swingbot.config import SwingConfig
from swingbot.indicators import rsi, sma
from swingbot.strategy import generate_signals


def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    }, index=idx)


def test_entry_requires_all_three_conditions():
    rng = np.random.default_rng(6)
    closes = 100 + np.cumsum(rng.normal(0.05, 1, 400))  # slight upward drift
    df = make_df(closes)
    config = SwingConfig(require_rising_trend_ma=False, trend_ma_period=200, pullback_ma_period=20,
                          rsi_period=5, entry_rsi_threshold=45)

    signals = generate_signals(df, config)
    trend_ma = sma(df["Close"], 200)
    pullback_ma = sma(df["Close"], 20)
    r = rsi(df["Close"], 5)

    expected = (df["Close"] > trend_ma) & (df["Close"] < pullback_ma) & (r < 45)
    pd.testing.assert_series_equal(signals["entry_signal"].fillna(False), expected.fillna(False), check_names=False)


def test_rising_trend_filter_blocks_entries_in_flat_market():
    # A perfectly flat market never has a "rising" 200-day MA relative to 20 bars ago.
    closes = np.full(400, 100.0)
    df = make_df(closes)
    config = SwingConfig(require_rising_trend_ma=True, trend_ma_period=200, trend_slope_lookback=20,
                          pullback_ma_period=20, rsi_period=5, entry_rsi_threshold=99)

    signals = generate_signals(df, config)
    assert not signals["trend_ok"].fillna(False).any()
    assert not signals["entry_signal"].fillna(False).any()


def test_exit_signal_matches_rsi_threshold():
    rng = np.random.default_rng(8)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = make_df(closes)
    config = SwingConfig(exit_rsi_threshold=65, rsi_period=5)
    signals = generate_signals(df, config)
    r = rsi(df["Close"], 5)
    pd.testing.assert_series_equal(signals["exit_signal"], r > 65, check_names=False)

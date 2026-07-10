import numpy as np
import pandas as pd

from rsibot.config import RSIConfig
from rsibot.indicators import rsi
from rsibot.strategy import generate_signals


def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    return pd.DataFrame({
        "Open": closes.values, "High": closes.values + 0.5, "Low": closes.values - 0.5, "Close": closes.values,
    }, index=idx)


def test_entry_signal_matches_raw_rsi_threshold_when_trend_filter_disabled():
    rng = np.random.default_rng(5)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = make_df(closes)
    config = RSIConfig(require_trend_filter=False, rsi_period=2, oversold_threshold=10)

    signals = generate_signals(df, config)
    expected_rsi = rsi(df["Close"], 2, "wilder")
    expected_entry = expected_rsi < 10

    pd.testing.assert_series_equal(signals["entry_signal"].fillna(False), expected_entry.fillna(False),
                                    check_names=False)


def test_trend_filter_blocks_entries_below_long_ma():
    # A sustained downtrend: price stays below its own 200-day MA once the MA catches up.
    n = 400
    closes = np.concatenate([np.full(200, 100.0), np.linspace(100, 40, 200)])
    df = make_df(closes)
    config = RSIConfig(require_trend_filter=True, trend_ma_period=200, rsi_period=2, oversold_threshold=50)

    signals = generate_signals(df, config)
    tail = signals.iloc[350:]
    # Deep in the downtrend, price is below the 200-day MA -> trend_ok False -> no entries,
    # even though RSI(2) will frequently be oversold during the decline.
    assert not tail["trend_ok"].any()
    assert not tail["entry_signal"].any()


def test_cumulative_entry_mode_uses_rolling_sum_of_rsi():
    rng = np.random.default_rng(9)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = make_df(closes)
    config = RSIConfig(require_trend_filter=False, entry_mode="cumulative", rsi_period=2,
                        cumulative_lookback=2, cumulative_threshold=10)
    signals = generate_signals(df, config)
    expected_rsi = rsi(df["Close"], 2, "wilder")
    expected_cum = expected_rsi.rolling(2, min_periods=2).sum()
    pd.testing.assert_series_equal(signals["entry_metric"], expected_cum, check_names=False)
    pd.testing.assert_series_equal(signals["entry_signal"].fillna(False), (expected_cum < 10).fillna(False),
                                    check_names=False)


def test_exit_mode_either_is_or_of_rsi_and_ma_cross():
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = make_df(closes)
    config = RSIConfig(exit_mode="either", exit_rsi_threshold=70, exit_ma_period=5)
    signals = generate_signals(df, config)

    expected_rsi_exit = signals["rsi"] > 70
    expected_ma_exit = df["Close"] > signals["exit_ma"]
    pd.testing.assert_series_equal(signals["exit_signal"], expected_rsi_exit | expected_ma_exit, check_names=False)

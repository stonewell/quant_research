"""Unit tests for offline intraday features: Dual-track DuckDB cache and Synthetic 5m Generator.

Tests 1 and 2 from Wave 1 Track T (T5.1).
All tests run 100% offline using in-memory / temporary DuckDB instances and SyntheticDataProvider.
"""

import datetime
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.data import BaseDataProvider, CachedDataProvider, SyntheticDataProvider
from common.duckdb_cache import (
    DuckDBCache,
    _get_resample_rule,
    is_intraday_interval,
)


def _generate_test_3day_5m_bars(start_date: str = "2026-10-14") -> pd.DataFrame:
    """Helper to generate exactly 3 days x 48 bars/day = 144 valid 5-minute bars."""
    days = pd.bdate_range(start_date, periods=3)
    dfs = []
    base_price = 100.0

    for d in days:
        date_str = d.strftime("%Y-%m-%d")
        m_times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 11:30:00", freq="5min")
        a_times = pd.date_range(f"{date_str} 13:05:00", f"{date_str} 15:00:00", freq="5min")
        day_times = m_times.append(a_times)
        n = len(day_times)  # 48

        c = base_price + np.linspace(0.0, 2.0, n)
        o = np.concatenate([[base_price], c[:-1]])
        h = np.maximum(o, c) + 0.5
        l = np.minimum(o, c) - 0.5
        v = np.full(n, 1000.0)

        df_day = pd.DataFrame(
            {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v},
            index=day_times,
        )
        dfs.append(df_day)
        base_price += 5.0

    full_df = pd.concat(dfs, axis=0)
    full_df.index.name = "Date"
    return full_df


def test_duckdb_intraday_cache_dual_track_and_r4_r13(tmp_path):
    """Test 1: Verify dual-track intraday cache, R13 end-date expansion, R4 drift purge, and CachedDataProvider."""
    # 1. Verify interval classification and resampling rules
    assert is_intraday_interval("5m") is True
    assert is_intraday_interval("1m") is True
    assert is_intraday_interval("1h") is True
    assert is_intraday_interval("1mo") is False

    assert _get_resample_rule("5m") is None
    assert _get_resample_rule("1m") is None
    assert _get_resample_rule("1h") is None
    assert _get_resample_rule("1mo") == "ME"

    # 2. Store 3 days of 48-bar 5m data (144 rows total)
    cache = DuckDBCache(db_path=":memory:")
    df_3days = _generate_test_3day_5m_bars("2026-10-14")
    assert len(df_3days) == 144

    cache.store_bars("TestProvider", "AAPL", df_3days, interval="5m")

    # Verify coverage check
    assert cache.is_range_covered("TestProvider", "AAPL", start="2026-10-14", end="2026-10-16", interval="5m") is True
    assert cache.is_intraday_range_covered("TestProvider", "AAPL", interval="5m", start="2026-10-14", end="2026-10-16") is True

    # 3. Query with date-only end="2026-10-16" (R13: final day's 48 bars not truncated at 00:00:00)
    queried_intraday = cache.query_intraday_bars("TestProvider", "AAPL", interval="5m", start="2026-10-14", end="2026-10-16")
    assert len(queried_intraday) == 144, f"Expected 144 bars, got {len(queried_intraday)}"
    assert queried_intraday.index[-1] == pd.Timestamp("2026-10-16 15:00:00")

    queried_facade = cache.query_bars("TestProvider", "AAPL", start="2026-10-14", end="2026-10-16", interval="5m")
    assert len(queried_facade) == 144

    # Universe query with date-only end
    uni = cache.query_universe("TestProvider", ["AAPL"], start="2026-10-14", end="2026-10-16", interval="5m")
    assert "AAPL" in uni
    assert len(uni["AAPL"]) == 144

    # 4. Simulate QFQ split/dividend adjustment (R4 drift check)
    meta_before = cache.get_intraday_metadata("TestProvider", "AAPL", "5m")
    assert meta_before is not None
    assert meta_before["latest_close_ref"] == pytest.approx(float(df_3days["Close"].iloc[-1]))

    df_scaled = df_3days.copy()
    df_scaled[["Open", "High", "Low", "Close"]] *= 0.90
    cache.store_intraday_bars("TestProvider", "AAPL", df_scaled, interval="5m")

    meta_after = cache.get_intraday_metadata("TestProvider", "AAPL", "5m")
    assert meta_after is not None
    assert meta_after["latest_close_ref"] == pytest.approx(float(df_scaled["Close"].iloc[-1]))

    q_scaled = cache.query_intraday_bars("TestProvider", "AAPL", interval="5m", start="2026-10-14", end="2026-10-16")
    assert len(q_scaled) == 144
    assert np.allclose(q_scaled["Close"].values, df_scaled["Close"].values)
    assert not np.allclose(q_scaled["Close"].values, df_3days["Close"].values)

    # 5. Verify CachedDataProvider with mock provider: caches in intraday table without touching daily table
    class Mock5mProvider(BaseDataProvider):
        def __init__(self, data: pd.DataFrame):
            self.data = data
            self.calls = 0

        def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
            self.calls += 1
            return self.data.copy()

    mock_inner = Mock5mProvider(df_3days)
    cached_prov = CachedDataProvider(mock_inner, cache_dir=str(tmp_path))

    # First fetch: cache miss -> calls inner provider
    df1 = cached_prov.fetch_ohlcv("TEST_SYM", start="2026-10-14", end="2026-10-16", interval="5m")
    assert mock_inner.calls == 1
    assert len(df1) == 144

    # Second fetch: cache hit -> reads DuckDB, no inner call
    df2 = cached_prov.fetch_ohlcv("TEST_SYM", start="2026-10-14", end="2026-10-16", interval="5m")
    assert mock_inner.calls == 1
    assert len(df2) == 144
    pd.testing.assert_frame_equal(df1, df2)

    # Verify DuckDB table isolation
    con = cached_prov.duckdb_cache._get_connection(read_only=True)
    daily_count = con.execute("SELECT count(*) FROM ohlcv_bars WHERE symbol = 'TEST_SYM'").fetchone()[0]
    intraday_count = con.execute("SELECT count(*) FROM intraday_ohlcv_bars WHERE symbol = 'TEST_SYM'").fetchone()[0]
    if not cached_prov.duckdb_cache.is_memory:
        con.close()

    assert daily_count == 0, "Daily table ohlcv_bars must remain empty for intraday cache"
    assert intraday_count == 144, "Intraday table intraday_ohlcv_bars must hold all 144 bars"


def test_synthetic_pinned_brownian_bridge_5m_conservation():
    """Test 2: Verify SyntheticDataProvider piecewise pinned Brownian bridge 5m generation and exact daily conservation."""
    provider = SyntheticDataProvider(seed=42)
    symbols = ["SPY", "600519.SH"]
    start = "2020-01-06"
    end = "2020-01-17"  # 10 business days

    for sym in symbols:
        daily_df = provider.fetch_ohlcv(sym, start=start, end=end, interval="1d")
        df_5m = provider.fetch_ohlcv(sym, start=start, end=end, interval="5m")

        assert len(daily_df) > 0
        assert len(df_5m) == len(daily_df) * 48

        # Group 5m bars by calendar date
        grouped = df_5m.groupby(df_5m.index.date)
        assert len(grouped) == len(daily_df)

        for d_date, group in grouped:
            assert len(group) == 48, f"Day {d_date} must have exactly 48 5m bars"

            # Check timestamp boundaries: 09:35..11:30 and 13:05..15:00
            m_bars = group.between_time("09:35", "11:30")
            a_bars = group.between_time("13:05", "15:00")
            assert len(m_bars) == 24
            assert len(a_bars) == 24

            d_ts = pd.Timestamp(d_date)
            daily_row = daily_df.loc[d_ts]

            # Aggregation check
            agg_open = float(group["Open"].iloc[0])
            agg_high = float(group["High"].max())
            agg_low = float(group["Low"].min())
            agg_close = float(group["Close"].iloc[-1])
            agg_vol = float(group["Volume"].sum())

            assert np.allclose(agg_open, daily_row["Open"], rtol=1e-7, atol=1e-7), (
                f"{sym} {d_date}: 5m Open ({agg_open}) != daily Open ({daily_row['Open']})"
            )
            assert np.allclose(agg_high, daily_row["High"], rtol=1e-7, atol=1e-7), (
                f"{sym} {d_date}: 5m High ({agg_high}) != daily High ({daily_row['High']})"
            )
            assert np.allclose(agg_low, daily_row["Low"], rtol=1e-7, atol=1e-7), (
                f"{sym} {d_date}: 5m Low ({agg_low}) != daily Low ({daily_row['Low']})"
            )
            assert np.allclose(agg_close, daily_row["Close"], rtol=1e-7, atol=1e-7), (
                f"{sym} {d_date}: 5m Close ({agg_close}) != daily Close ({daily_row['Close']})"
            )
            assert np.allclose(agg_vol, daily_row["Volume"], rtol=1e-7, atol=1e-7), (
                f"{sym} {d_date}: 5m Volume ({agg_vol}) != daily Volume ({daily_row['Volume']})"
            )

            # Bar-level OHLC validity: Low <= min(Open, Close) <= max(Open, Close) <= High
            upper = np.maximum(group["Open"], group["Close"])
            lower = np.minimum(group["Open"], group["Close"])
            assert (group["High"] >= upper).all(), f"{sym} {d_date}: Bar High must be >= max(Open, Close)"
            assert (group["Low"] <= lower).all(), f"{sym} {d_date}: Bar Low must be <= min(Open, Close)"
            assert (group["High"] >= group["Low"]).all(), f"{sym} {d_date}: Bar High must be >= Low"

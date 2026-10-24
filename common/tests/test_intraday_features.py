"""Unit tests for offline intraday features: Dual-track DuckDB cache and Synthetic 5m Generator.

Tests 1 and 2 from Wave 1 Track T (T5.1).
All tests run 100% offline using in-memory / temporary DuckDB instances and SyntheticDataProvider.
"""

import datetime
import logging
import os
import sys
from unittest.mock import MagicMock, patch

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


def test_baostock_provider_offline_mock(tmp_path):
    """Test 3 (T1.3): Verify BaoStockDataProvider offline mock, symbol normalization, field restrictions, and chunking.

    The explicit `cache_dir` keeps this test off the production market cache
    (`<repo>/data/cache.duckdb`); `DuckDBCache`'s pytest guard would otherwise
    (correctly) refuse the writable open that the no-cache_dir fallback performs.
    """
    from common.financial_api import (
        BaoStockDataProvider,
        is_index_symbol,
        to_baostock_code,
    )

    # 1. Verify to_baostock_code for SH, SZ, BJ
    assert to_baostock_code("600519.SH") == "sh.600519"
    assert to_baostock_code("000001.SZ") == "sz.000001"
    assert to_baostock_code("830001.BJ") == "bj.830001"

    # 2. Verify is_index_symbol
    assert is_index_symbol("000300.SH") is True
    assert is_index_symbol("000001.SH") is True
    assert is_index_symbol("000905.SH") is True
    assert is_index_symbol("000852.SH") is True
    assert is_index_symbol("000016.SH") is True
    assert is_index_symbol("399001.SZ") is True
    assert is_index_symbol("399006.SZ") is True
    assert is_index_symbol("399300.SZ") is True
    assert is_index_symbol("000001.SZ") is False
    assert is_index_symbol("600519.SH") is False

    provider = BaoStockDataProvider(adjustflag="2", cache_dir=str(tmp_path))
    assert provider.adjustflag == "2"

    # 3. Verify BJ stock and index return empty DataFrame with warning for interval="5m"
    with pytest.warns(UserWarning, match="BaoStock does not support intraday"):
        df_bj = provider.fetch_ohlcv("830001.BJ", start="2026-10-16", end="2026-10-16", interval="5m")
        assert df_bj.empty
        assert list(df_bj.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert isinstance(df_bj.index, pd.DatetimeIndex)

    with pytest.warns(UserWarning, match="BaoStock does not support intraday"):
        df_idx = provider.fetch_ohlcv("000300.SH", start="2026-10-16", end="2026-10-16", interval="5m")
        assert df_idx.empty
        assert list(df_idx.columns) == ["Open", "High", "Low", "Close", "Volume"]

    # 4. Mock baostock module for 17-digit time parsing and strict field restrictions
    mock_bs = MagicMock()
    mock_login_res = MagicMock()
    mock_login_res.error_code = "0"
    mock_bs.login.return_value = mock_login_res

    query_calls = []

    def fake_query(code, fields, start_date, end_date, frequency, adjustflag):
        query_calls.append({
            "code": code,
            "fields": fields,
            "start_date": start_date,
            "end_date": end_date,
            "frequency": frequency,
            "adjustflag": adjustflag,
        })
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        # If querying 2026-10-16, return 20261016093500000.
        # For multi-chunk range [2026-01-01, 2026-07-31], return 20260101093500000 for both chunks to verify deduplication.
        ts_date = "2026-10-16" if start_date == "2026-10-16" else "2026-01-01"
        ts_time = "20261016093500000" if start_date == "2026-10-16" else "20260101093500000"
        df_chunk = pd.DataFrame({
            "date": [ts_date],
            "time": [ts_time],
            "code": [code],
            "open": ["100.0"],
            "high": ["105.0"],
            "low": ["98.0"],
            "close": ["103.0"],
            "volume": ["10000"],
            "amount": ["1020000"],
            "adjustflag": [adjustflag],
        })
        mock_rs.get_data.return_value = df_chunk
        return mock_rs

    mock_bs.query_history_k_data_plus.side_effect = fake_query

    with patch.object(provider, "_get_bs", return_value=mock_bs):
        # Single-day query
        df_5m = provider.fetch_ohlcv("600519.SH", start="2026-10-16", end="2026-10-16", interval="5m")

        assert len(query_calls) == 1
        call_info = query_calls[0]
        assert call_info["code"] == "sh.600519"
        assert call_info["frequency"] == "5"
        assert call_info["adjustflag"] == "2"

        # Verify fields for interval="5m" strictly excludes preclose, turn, peTTM, pctChg, isST
        fields = call_info["fields"]
        assert "preclose" not in fields
        assert "turn" not in fields
        assert "peTTM" not in fields
        assert "pctChg" not in fields
        assert "isST" not in fields
        assert "time" in fields

        # Verify 17-digit time parsing
        assert len(df_5m) == 1
        assert df_5m.index[0] == pd.Timestamp("2026-10-16 09:35:00")
        assert df_5m["Open"].iloc[0] == 100.0
        assert df_5m["High"].iloc[0] == 105.0
        assert df_5m["Low"].iloc[0] == 98.0
        assert df_5m["Close"].iloc[0] == 103.0
        assert df_5m["Volume"].iloc[0] == 10000.0

        # 5. Verify >180-day range triggers multi-chunk queries and deduplicates cleanly
        query_calls.clear()
        df_multi = provider.fetch_ohlcv("600519.SH", start="2026-01-01", end="2026-07-31", interval="5m")
        assert len(query_calls) >= 2, f"Expected >= 2 chunks for 210 days, got {len(query_calls)}"
        assert query_calls[0]["start_date"] == "2026-01-01"
        assert len(df_multi) == 1


def test_eastmoney_provider_offline_mock():
    """Test 4 (T1.4): Verify EastMoneyDataProvider offline mock, O-C-H-L correction, and lots conversion."""
    from common.financial_api import (
        EastMoneyDataProvider,
        to_eastmoney_secid,
    )

    # 1. Verify to_eastmoney_secid
    assert to_eastmoney_secid("600519.SH") == "1.600519"
    assert to_eastmoney_secid("510300.SH") == "1.510300"
    assert to_eastmoney_secid("000300.SH") == "1.000300"
    assert to_eastmoney_secid("000001.SZ") == "0.000001"
    assert to_eastmoney_secid("830001.BJ") == "0.830001"

    # 2. Mock requests.Session
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "rc": 0,
        "data": {
            "code": "600519",
            "market": 1,
            "klines": [
                "2026-10-16 09:35,100.00,105.00,108.00,98.00,500,5125000.00,10.00"
            ],
        },
    }
    mock_session.get.return_value = mock_resp

    provider = EastMoneyDataProvider(session=mock_session)

    # Fetch 5m
    df = provider.fetch_ohlcv("600519.SH", start="2026-10-16", end="2026-10-16", interval="5m")

    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2026-10-16 09:35:00")
    # O-C-H-L field order correction: parts[1]=Open, parts[2]=Close, parts[3]=High, parts[4]=Low
    assert df["Open"].iloc[0] == 100.0
    assert df["Close"].iloc[0] == 105.0
    assert df["High"].iloc[0] == 108.0
    assert df["Low"].iloc[0] == 98.0
    # Lots-to-shares volume scaling (500 lots * 100 = 50,000 shares)
    assert df["Volume"].iloc[0] == 50000.0
    # Amount and VWAP cross check: 5,125,000 / 50,000 = 102.50
    assert df["Amount"].iloc[0] == 5125000.0
    assert df["VWAP"].iloc[0] == pytest.approx(102.50)

    # 3. Verify interval="1h" and interval="60m" both map to klt="60"
    provider.fetch_ohlcv("600519.SH", start="2026-10-16", end="2026-10-16", interval="1h")
    _, kwargs1 = mock_session.get.call_args
    assert kwargs1["params"]["klt"] == "60"

    provider.fetch_ohlcv("600519.SH", start="2026-10-16", end="2026-10-16", interval="60m")
    _, kwargs2 = mock_session.get.call_args
    assert kwargs2["params"]["klt"] == "60"


def test_intraday_snapshot_aggregator_and_parquet(tmp_path):
    """Test 5 (T1.5): Verify IntradayBarAggregator tick accumulation, LockFreeParquetWriter concurrency, and EastMoney gap backfilling."""
    from scripts.record_intraday_5m import (
        IntradayBarAggregator,
        LockFreeParquetWriter,
        EastMoneyGapBackfiller,
        cleanup_old_parquet_files,
    )

    # 1. Aggregator synthetic snapshot sequence
    agg = IntradayBarAggregator()
    sym = "600519.SH"

    # Snapshot 1: 09:25 opening auction baseline
    agg.process_snapshot({
        "symbol": sym,
        "timestamp": "2026-10-16 09:25:00",
        "open_price": 100.0,
        "last_price": 100.0,
        "high_price": 100.0,
        "low_price": 100.0,
        "volume": 1000.0,
        "turnover": 100000.0,
    })

    # Snapshot 2: 09:32 intra-poll spike (high=105.0, low=98.0)
    agg.process_snapshot({
        "symbol": sym,
        "timestamp": "2026-10-16 09:32:00",
        "open_price": 100.0,
        "last_price": 102.0,
        "high_price": 105.0,
        "low_price": 98.0,
        "volume": 3000.0,
        "turnover": 305000.0,
    })

    # Snapshot 3: 09:35 bucket end
    agg.process_snapshot({
        "symbol": sym,
        "timestamp": "2026-10-16 09:35:00",
        "open_price": 100.0,
        "last_price": 101.0,
        "high_price": 105.0,
        "low_price": 98.0,
        "volume": 5000.0,
        "turnover": 510000.0,
    })

    # Snapshot 4: 09:40 next bucket (triggers finalize on 09:35 bucket!)
    agg.process_snapshot({
        "symbol": sym,
        "timestamp": "2026-10-16 09:40:00",
        "open_price": 100.0,
        "last_price": 103.0,
        "high_price": 106.0,
        "low_price": 98.0,
        "volume": 8000.0,
        "turnover": 820000.0,
    })

    # Finalize remaining in-progress 09:40 bucket
    df_bars = agg.get_bars_df(finalize=True)
    assert len(df_bars) == 2

    # Check bar 1 (09:35)
    b1 = df_bars.iloc[0]
    assert b1["timestamp"] == pd.Timestamp("2026-10-16 09:35:00")
    assert b1["Open"] == 100.0
    assert b1["Close"] == 101.0
    assert b1["High"] == 105.0  # Captured intra-poll high spike!
    assert b1["Low"] == 98.0    # Captured intra-poll low dip!
    assert b1["Volume"] == 5000.0  # Differential volume: 5000 - 0
    assert b1["Amount"] == 510000.0  # Differential amount: 510000 - 0
    assert b1["VWAP"] == pytest.approx(102.0)  # 510000 / 5000

    # Check bar 2 (09:40)
    b2 = df_bars.iloc[1]
    assert b2["timestamp"] == pd.Timestamp("2026-10-16 09:40:00")
    assert b2["Volume"] == 3000.0  # Differential volume: 8000 - 5000
    assert b2["Amount"] == 310000.0  # Differential amount: 820000 - 510000
    assert b2["VWAP"] == pytest.approx(102.50)  # Cumulative: 820000 / 8000

    # 2. LockFreeParquetWriter + DuckDBCache concurrency (zero lock contention)
    parquet_dir = tmp_path / "intraday_5m"
    duckdb_file = tmp_path / "cache.duckdb"

    # Open DuckDB connection and write to it
    cache = DuckDBCache(db_path=str(duckdb_file))
    cache_con = cache._get_connection(read_only=False)
    cache_con.execute("CREATE TABLE concurrent_test (id INT)")
    cache_con.execute("INSERT INTO concurrent_test VALUES (1), (2), (3)")

    # While DuckDB is actively open, LockFreeParquetWriter atomically writes parquet
    writer = LockFreeParquetWriter(output_dir=str(parquet_dir))
    target_file = writer.write_bars(df_bars, "2026-10-16")

    assert os.path.isfile(target_file)
    saved_df = pd.read_parquet(target_file)
    assert len(saved_df) == 2
    assert list(saved_df["symbol"]) == [sym, sym]

    # Verify DuckDB cache continues writing unblocked
    cache_con.execute("INSERT INTO concurrent_test VALUES (4)")
    res = cache_con.execute("SELECT count(*) FROM concurrent_test").fetchone()[0]
    assert res == 4
    if not cache.is_memory:
        cache_con.close()

    # Test retention cleanup helper
    old_file = parquet_dir / "2020-01-01.parquet"
    old_file.write_text("dummy")
    deleted = cleanup_old_parquet_files(str(parquet_dir), retention_days=90)
    assert str(old_file) in deleted
    assert not os.path.exists(old_file)
    assert os.path.exists(target_file)  # Current file preserved

    # 3. EastMoneyGapBackfiller
    class MockEastMoneyProvider(BaseDataProvider):
        def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
            times = pd.date_range("2026-10-16 09:35:00", "2026-10-16 09:45:00", freq="5min")
            return pd.DataFrame(
                {
                    "Open": [100.0, 101.0, 102.0],
                    "High": [105.0, 106.0, 107.0],
                    "Low": [98.0, 100.0, 101.0],
                    "Close": [101.0, 103.0, 105.0],
                    "Volume": [5000.0, 3000.0, 4000.0],
                    "Amount": [510000.0, 310000.0, 415000.0],
                    "VWAP": [102.0, 102.5, 103.75],
                },
                index=times,
            )

    mock_em_prov = MockEastMoneyProvider()
    backfiller = EastMoneyGapBackfiller(provider=mock_em_prov)

    # Start with only 1 bar in df_bars
    df_partial = df_bars.iloc[:1]
    assert len(df_partial) == 1

    filled_df = backfiller.backfill_symbol(sym, "2026-10-16", existing_bars=df_partial)
    assert len(filled_df) == 3
    assert list(filled_df["timestamp"]) == [
        pd.Timestamp("2026-10-16 09:35:00"),
        pd.Timestamp("2026-10-16 09:40:00"),
        pd.Timestamp("2026-10-16 09:45:00"),
    ]


def test_normalize_thscode_and_baostock_index_fields(tmp_path):
    """Verify bare index code normalization and BaoStock index daily fields & adjustflag='3'."""
    from common.financial_api import (
        normalize_thscode,
        BaoStockDataProvider,
    )

    # 1. Verification of bare index code mapping vs Ping An Bank
    assert normalize_thscode("000300") == "000300.SH"
    assert normalize_thscode("000905") == "000905.SH"
    assert normalize_thscode("000852") == "000852.SH"
    assert normalize_thscode("000016") == "000016.SH"
    assert normalize_thscode("000688") == "000688.SH"
    assert normalize_thscode("000001") == "000001.SZ"
    assert normalize_thscode("000001.SH") == "000001.SH"

    # 2. BaoStockDataProvider index daily query fields and adjustflag='3'
    mock_bs = MagicMock()
    mock_login_res = MagicMock()
    mock_login_res.error_code = "0"
    mock_bs.login.return_value = mock_login_res

    query_calls = []

    def fake_query(code, fields, start_date, end_date, frequency, adjustflag):
        query_calls.append({
            "code": code,
            "fields": fields,
            "start_date": start_date,
            "end_date": end_date,
            "frequency": frequency,
            "adjustflag": adjustflag,
        })
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.get_data.return_value = pd.DataFrame({
            "date": ["2020-01-02"],
            "code": [code],
            "open": ["4000.0"],
            "high": ["4050.0"],
            "low": ["3980.0"],
            "close": ["4020.0"],
            "preclose": ["3990.0"],
            "volume": ["100000000"],
            "amount": ["200000000000"],
            "pctChg": ["0.75"],
        })
        return mock_rs

    mock_bs.query_history_k_data_plus.side_effect = fake_query

    b_prov = BaoStockDataProvider(adjustflag="2", cache_dir=str(tmp_path))
    with patch.object(b_prov, "_get_bs", return_value=mock_bs):
        df_res = b_prov.fetch_ohlcv("000300.SH", "2020-01-01", "2020-06-01", interval="1d")
        assert not df_res.empty
        assert len(query_calls) == 1
        call = query_calls[0]
        assert call["code"] == "sh.000300"
        assert call["fields"] == "date,code,open,high,low,close,preclose,volume,amount,pctChg"
        assert call["adjustflag"] == "3"
        assert call["frequency"] == "d"


def test_marketdb_and_fuyao_index_gap_fallback_stitching(tmp_path, monkeypatch):
    """Verify MarketDBDataProvider and FuyaoDataProvider index fallbacks and 5y/trailing stitching.

    Runs from `tmp_path` so the providers' no-`cache_dir` fallback cannot reach
    the repo's production cache: `common/financial_api.py` constructs
    `BaoStockDataProvider()` internally, whose cwd-relative `data` fallback would
    otherwise be refused by `DuckDBCache`'s pytest guard (and must be).
    """
    monkeypatch.chdir(tmp_path)
    from common.financial_api import (
        MarketDBDataProvider,
        FuyaoDataProvider,
        BaoStockDataProvider,
        EastMoneyDataProvider,
    )

    # 1. MarketDBDataProvider fallback on index symbol
    mock_con = MagicMock()
    mock_res = MagicMock()
    mock_res.df.return_value = pd.DataFrame()  # empty query
    mock_con.execute.return_value = mock_res

    fake_idx_df = pd.DataFrame(
        {"Open": [4000.0], "High": [4050.0], "Low": [3980.0], "Close": [4020.0], "Volume": [1e8]},
        index=pd.date_range("2020-01-02", periods=1, freq="D"),
    )

    mdb_prov = MarketDBDataProvider(con=mock_con)
    with patch.object(BaoStockDataProvider, "fetch_ohlcv", return_value=fake_idx_df) as mock_bs_fetch:
        df_out = mdb_prov.fetch_ohlcv("000300.SH", "2020-01-01", "2020-01-10", interval="1d")
        assert not df_out.empty
        mock_bs_fetch.assert_called_once()
        assert df_out.iloc[0]["Close"] == 4020.0

    # 2. FuyaoDataProvider index historical pre-5y and trailing stitching
    pre_df = pd.DataFrame(
        {
            "Open": [3500.0, 3510.0],
            "High": [3550.0, 3560.0],
            "Low": [3480.0, 3490.0],
            "Close": [3520.0, 3530.0],
            "Volume": [1e7, 1e7],
        },
        index=pd.to_datetime(["2016-01-04", "2016-01-05"]),
    )
    fuyao_items = [
        {
            "date_ms": int(pd.Timestamp("2021-01-04").timestamp() * 1000),
            "open_price": 5000.0,
            "high_price": 5050.0,
            "low_price": 4980.0,
            "close_price": 5020.0,
            "volume": 2e7,
        },
        {
            "date_ms": int(pd.Timestamp("2021-01-05").timestamp() * 1000),
            "open_price": 5020.0,
            "high_price": 5080.0,
            "low_price": 5000.0,
            "close_price": 5060.0,
            "volume": 2e7,
        },
    ]
    post_df = pd.DataFrame(
        {
            "Open": [3900.0, 3920.0],
            "High": [3950.0, 3960.0],
            "Low": [3880.0, 3900.0],
            "Close": [3930.0, 3950.0],
            "Volume": [3e7, 3e7],
        },
        index=pd.to_datetime(["2026-10-15", "2026-10-16"]),
    )

    fuyao_prov = FuyaoDataProvider(prefer_local=False)
    with patch("fuyao_client.index_prices_historical", return_value=fuyao_items), \
         patch.object(BaoStockDataProvider, "fetch_ohlcv", return_value=pre_df), \
         patch.object(EastMoneyDataProvider, "fetch_ohlcv", return_value=post_df):
        df_stitched = fuyao_prov.fetch_ohlcv("000300.SH", "2016-01-01", "2026-10-16", interval="1d")
        assert not df_stitched.empty
        assert df_stitched.index.min() == pd.Timestamp("2016-01-04")
        assert df_stitched.index.max() == pd.Timestamp("2026-10-16")
        assert len(df_stitched) == 6


def test_intraday_features_causal_truncation_and_kappa_alignment():
    """Test 6 (T2.1, T5.1): Causal truncation (36/39/48 bars, R1), anti-lookahead, kappa_t alignment (R4/R6), and Is_Partial_Bar (R12)."""
    from common.intraday_features import (
        IntradayFeatureConfig,
        compute_daily_intraday_features,
        enrich_universe_with_intraday_features,
    )

    # 1. Construct 5 days of 5m data: 4 normal historical days + 1 crash day (2026-10-16)
    days = pd.bdate_range("2026-10-12", "2026-10-16")  # 5 business days
    dfs = []
    base_p = 100.0

    for d in days:
        date_str = d.strftime("%Y-%m-%d")
        m_times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 11:30:00", freq="5min")
        a_times = pd.date_range(f"{date_str} 13:05:00", f"{date_str} 15:00:00", freq="5min")
        times = m_times.append(a_times)
        n = len(times)  # 48

        if date_str == "2026-10-16":
            # On 2026-10-16:
            # 09:35..14:15 (first 39 bars): steady at 100.0
            # 14:20..15:00 (last 9 bars): sudden plunge to 92.0 (-8% crash)
            c = np.full(n, 100.0)
            c[39:] = np.linspace(98.0, 92.0, 9)
            o = np.concatenate([[100.0], c[:-1]])
            h = np.maximum(o, c) + 0.2
            l = np.minimum(o, c) - 0.2
            v = np.full(n, 1000.0)
            v[39:] = 5000.0  # panic volume in final 9 bars
        else:
            c = base_p + np.linspace(0.0, 1.0, n)
            o = np.concatenate([[base_p], c[:-1]])
            h = np.maximum(o, c) + 0.3
            l = np.minimum(o, c) - 0.3
            v = np.full(n, 1000.0)
            base_p += 1.0

        df_day = pd.DataFrame(
            {"Open": o, "High": h, "Low": l, "Close": c, "Volume": v},
            index=times,
        )
        dfs.append(df_day)

    df_5m = pd.concat(dfs, axis=0)

    # Assert bar counts per cutoff (R1)
    d_crash = pd.Timestamp("2026-10-16")
    crash_day_bars = df_5m.loc[df_5m.index.normalize() == d_crash]

    bars_1400 = crash_day_bars.loc[crash_day_bars.index.time <= datetime.time(14, 0)]
    bars_1415 = crash_day_bars.loc[crash_day_bars.index.time <= datetime.time(14, 15)]
    bars_full = crash_day_bars.loc[crash_day_bars.index.time <= datetime.time(15, 0)]

    assert len(bars_1400) == 36, f"Expected 36 bars at 14:00, got {len(bars_1400)}"
    assert len(bars_1415) == 39, f"Expected 39 bars at 14:15, got {len(bars_1415)}"
    assert len(bars_full) == 48, f"Expected 48 bars at full day, got {len(bars_full)}"

    # Compute features under 14:00, 14:15, and full day
    feat_1400 = compute_daily_intraday_features(df_5m, config=IntradayFeatureConfig(cutoff_time="14:00"))
    feat_1415 = compute_daily_intraday_features(df_5m, config=IntradayFeatureConfig(cutoff_time="14:15"))
    feat_full = compute_daily_intraday_features(df_5m, config=IntradayFeatureConfig(cutoff_time=None))

    # Causal assertions on crash day:
    # At 14:00 and 14:15, price is ~100.0, completely unaware of the 14:30 plunge
    assert feat_1400.loc[d_crash, "Close_5m_Cutoff"] == pytest.approx(100.0)
    assert feat_1415.loc[d_crash, "Close_5m_Cutoff"] == pytest.approx(100.0)
    assert feat_full.loc[d_crash, "Close_5m_Cutoff"] == pytest.approx(92.0)

    # Is_Partial_Bar flag assertions (R12)
    assert feat_1400.loc[d_crash, "Is_Partial_Bar"] == 1
    assert feat_1415.loc[d_crash, "Is_Partial_Bar"] == 1
    assert feat_full.loc[d_crash, "Is_Partial_Bar"] == 0

    # Chan 5m sub-level structure & lazy candidate date assertions (T2.3, R9)
    assert "Chan_5m_Buy_Confluence" in feat_1415.columns
    assert "Chan_5m_Sell_Trigger" in feat_1415.columns
    assert "Chan_5m_State" in feat_1415.columns
    assert feat_1415.loc[d_crash, "Chan_5m_Buy_Confluence"] in (0, 1, 2)
    assert feat_1415.loc[d_crash, "Chan_5m_Sell_Trigger"] in (0, 1)

    # Lazy candidate dates: only d_crash is evaluated, days[0] defaults to neutral
    feat_lazy = compute_daily_intraday_features(
        df_5m,
        config=IntradayFeatureConfig(cutoff_time="14:15"),
        chan_candidate_dates={d_crash},
    )
    assert feat_lazy.loc[days[0], "Chan_5m_State"] == "5m_NEUTRAL"
    assert feat_lazy.loc[d_crash, "Chan_5m_Buy_Confluence"] == feat_1415.loc[d_crash, "Chan_5m_Buy_Confluence"]

    # Test kappa_t cross-source split ratio alignment (R4, R6)
    # Suppose universe_1d has Close = 200.0 on crash day (due to 2:1 split basis difference)
    # while 5m close at 14:15 cutoff is 100.0 -> kappa_t = 2.0
    daily_rows = []
    for d in days:
        d_bars = df_5m.loc[df_5m.index.normalize() == d]
        daily_rows.append({
            "Open": float(d_bars["Open"].iloc[0]) * 2.0,
            "High": float(d_bars["High"].max()) * 2.0,
            "Low": float(d_bars["Low"].min()) * 2.0,
            "Close": float(d_bars["Close"].iloc[-1]) * 2.0,
            "Volume": float(d_bars["Volume"].sum()) / 2.0,
        })
    df_1d = pd.DataFrame(daily_rows, index=days)

    universe_1d = {"SYM_TEST": df_1d}
    universe_5m = {"SYM_TEST": df_5m}

    enriched = enrich_universe_with_intraday_features(
        universe_1d,
        universe_5m,
        config=IntradayFeatureConfig(cutoff_time="14:15"),
        synthesize_intraday_bar=False,
    )
    df_enriched = enriched["SYM_TEST"]

    # 1. VWAP must be multiplied by kappa_t, placing it on the 200.0 / 184.0 price basis
    kappa_t = df_1d.loc[d_crash, "Close"] / feat_1415.loc[d_crash, "Close_5m_Cutoff"]
    raw_vwap_1415 = feat_1415.loc[d_crash, "VWAP"]
    aligned_vwap = df_enriched.loc[d_crash, "VWAP"]
    assert aligned_vwap == pytest.approx(raw_vwap_1415 * kappa_t, rel=1e-4)

    # 2. VWAP_Prem_5m is dimensionless and immune to split basis
    assert df_enriched.loc[d_crash, "VWAP_Prem_5m"] == pytest.approx(feat_1415.loc[d_crash, "VWAP_Prem_5m"], rel=1e-5)

    # 3. Alpha#41 = sqrt(High * Low) - VWAP is on the 200.0 scale without dimensional mismatch
    alpha41 = np.sqrt(df_enriched.loc[d_crash, "High"] * df_enriched.loc[d_crash, "Low"]) - df_enriched.loc[d_crash, "VWAP"]
    assert abs(alpha41) < 10.0  # reasonable residual on ~200 scale, not blown up by a 100 vs 200 basis mismatch

    # 4. synthesize_intraday_bar=True sets Is_Partial_Bar=1 for the truncated day
    enriched_synth = enrich_universe_with_intraday_features(
        universe_1d,
        universe_5m,
        config=IntradayFeatureConfig(cutoff_time="14:15"),
        synthesize_intraday_bar=True,
    )
    df_synth = enriched_synth["SYM_TEST"]
    assert df_synth.loc[d_crash, "Is_Partial_Bar"] == 1
    # Prior days with complete history remain 0
    assert df_synth.loc[days[0], "Is_Partial_Bar"] == 0


def test_smart_money_upvol_ratio_and_semivariance():
    """Test 7 (T2.2, T5.1): Realized semivariance decomposition, SmartMoney_Q_5m, UpVol_Ratio_5m, and Afternoon_Ret_5m."""
    from common.intraday_features import (
        IntradayFeatureConfig,
        compute_daily_intraday_features,
    )

    date_str = "2026-10-16"
    m_times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 11:30:00", freq="5min")
    a_times = pd.date_range(f"{date_str} 13:05:00", f"{date_str} 15:00:00", freq="5min")
    times = m_times.append(a_times)
    n = len(times)  # 48

    # Sample A: Low-level morning accumulation + afternoon push up
    # Morning (bars 0..23): prices 98..99 with high efficiency moves (|C - O| = 0.30, V = 1000).
    # Smartness S is high in morning at price ~98.5.
    # Afternoon (bars 24..47): heavy volume churn (V = 4000, |C - O| = 0.05) pushing price up to 102.
    c_a = np.zeros(n)
    o_a = np.zeros(n)
    v_a = np.zeros(n)

    for i in range(24):  # morning accumulation
        o_a[i] = 98.0 + (i * 0.04)
        c_a[i] = o_a[i] + 0.30  # high efficiency price advance
        v_a[i] = 1000.0

    for i in range(24, n):  # afternoon steady push on heavy volume
        o_a[i] = 99.0 + ((i - 24) * 0.12)
        c_a[i] = o_a[i] + 0.05
        v_a[i] = 4000.0

    h_a = np.maximum(o_a, c_a) + 0.05
    l_a = np.minimum(o_a, c_a) - 0.05

    df_a = pd.DataFrame(
        {"Open": o_a, "High": h_a, "Low": l_a, "Close": c_a, "Volume": v_a},
        index=times,
    )

    # Sample B: Morning pump to 105 at 11:30 (high S at high prices) + afternoon heavy volume dump
    # Morning (bars 0..23): high smartness pump at prices 104..105 (|C - O| = 0.30, V = 1000).
    # Afternoon (bars 24..47): heavy volume dump (V = 4000, C < O) dumping price down to 98.
    c_b = np.zeros(n)
    o_b = np.zeros(n)
    v_b = np.zeros(n)

    for i in range(24):  # morning pump
        o_b[i] = 103.5 + (i * 0.06)
        c_b[i] = o_b[i] + 0.25  # high efficiency pump at high prices
        v_b[i] = 1000.0

    for i in range(24, n):  # afternoon heavy volume dump
        o_b[i] = 104.5 - ((i - 24) * 0.25)
        c_b[i] = o_b[i] - 0.15  # red bars
        v_b[i] = 4000.0  # heavy dump volume

    h_b = np.maximum(o_b, c_b) + 0.05
    l_b = np.minimum(o_b, c_b) - 0.05

    df_b = pd.DataFrame(
        {"Open": o_b, "High": h_b, "Low": l_b, "Close": c_b, "Volume": v_b},
        index=times,
    )

    # Compute features for both
    feat_a = compute_daily_intraday_features(df_a, config=IntradayFeatureConfig(cutoff_time="14:15"))
    feat_b = compute_daily_intraday_features(df_b, config=IntradayFeatureConfig(cutoff_time="14:15"))

    d_test = pd.Timestamp(date_str)
    row_a = feat_a.loc[d_test]
    row_b = feat_b.loc[d_test]

    # 1. UpVol_Ratio_5m in [0, 1]
    assert 0.0 <= row_a["UpVol_Ratio_5m"] <= 1.0
    assert 0.0 <= row_b["UpVol_Ratio_5m"] <= 1.0
    assert row_a["UpVol_Ratio_5m"] > 0.60, f"Sample A UpVol ratio {row_a['UpVol_Ratio_5m']} expected > 0.60"
    assert row_b["UpVol_Ratio_5m"] < 0.40, f"Sample B UpVol ratio {row_b['UpVol_Ratio_5m']} expected < 0.40"

    # 2. SmartMoney_Q_5m
    # Sample A accumulated low -> Q < 0, close above VWAP -> VWAP_Prem > 0
    assert row_a["SmartMoney_Q_5m"] < 0.0, f"Sample A Q {row_a['SmartMoney_Q_5m']} expected < 0"
    assert row_a["VWAP_Prem_5m"] > 0.0
    # Sample B dumped high volume in afternoon -> Q > 0, close below VWAP -> VWAP_Prem < 0
    assert row_b["SmartMoney_Q_5m"] > 0.0, f"Sample B Q {row_b['SmartMoney_Q_5m']} expected > 0"
    assert row_b["VWAP_Prem_5m"] < 0.0

    # 3. Semivariance decomposition: RS_Up + RS_Down == RV
    assert row_a["RS_Up_5m"] + row_a["RS_Down_5m"] == pytest.approx(row_a["RV_5m"], rel=1e-8, abs=1e-10)
    assert row_b["RS_Up_5m"] + row_b["RS_Down_5m"] == pytest.approx(row_b["RV_5m"], rel=1e-8, abs=1e-10)
    # Sample A has more upside variance than downside variance
    assert row_a["RS_Up_5m"] > row_a["RS_Down_5m"]
    # Sample B has more downside variance than upside variance
    assert row_b["RS_Down_5m"] > row_b["RS_Up_5m"]

    # 4. Afternoon_Ret_5m
    # Sample B dropped from ~104 at 11:30 to ~98 at 14:15 -> Afternoon_Ret_5m < -0.01
    assert row_b["Afternoon_Ret_5m"] < -0.01, f"Sample B afternoon return {row_b['Afternoon_Ret_5m']} expected < -0.01"
    assert row_a["Afternoon_Ret_5m"] > 0.0


def test_enrich_universe_with_intraday_features_cutoff_time_kwarg():
    """Verify enrich_universe_with_intraday_features supports cutoff_time keyword argument,
    overrides default 14:15 cutoff (36 bars vs 39 bars), supports datetime.time objects,
    and updates config via replace(config) while preserving other config attributes.
    """
    from common.intraday_features import (
        IntradayFeatureConfig,
        compute_daily_intraday_features,
        enrich_universe_with_intraday_features,
    )

    df_5m = _generate_test_3day_5m_bars("2026-10-14")
    days = pd.bdate_range("2026-10-14", periods=3)

    daily_rows = []
    for d in days:
        d_bars = df_5m.loc[df_5m.index.normalize() == d]
        daily_rows.append({
            "Open": float(d_bars["Open"].iloc[0]),
            "High": float(d_bars["High"].max()),
            "Low": float(d_bars["Low"].min()),
            "Close": float(d_bars["Close"].iloc[-1]),
            "Volume": float(d_bars["Volume"].sum()),
        })
    df_1d = pd.DataFrame(daily_rows, index=days)
    universe_1d = {"SYM_A": df_1d}
    universe_5m = {"SYM_A": df_5m}

    d_last = days[-1]
    d_last_bars = df_5m.loc[df_5m.index.normalize() == d_last]
    # 36th bar corresponds to 14:00 (index 35, 24 morning + 12 afternoon)
    assert d_last_bars.index[35].time() == datetime.time(14, 0)
    bar_36_close = float(d_last_bars.iloc[35]["Close"])
    # 39th bar corresponds to 14:15 (index 38, 24 morning + 15 afternoon)
    assert d_last_bars.index[38].time() == datetime.time(14, 15)
    bar_39_close = float(d_last_bars.iloc[38]["Close"])
    assert bar_36_close != bar_39_close

    # 1. Default behavior: cutoff_time defaults to 14:15 (uses 39 bars)
    enriched_default = enrich_universe_with_intraday_features(
        universe_1d,
        universe_5m,
    )
    assert "SYM_A" in enriched_default
    df_default = enriched_default["SYM_A"]
    assert "Close_5m_Cutoff" in df_default.columns
    assert df_default.loc[d_last, "Close_5m_Cutoff"] == pytest.approx(bar_39_close)

    # 2. String cutoff_time="14:00" overrides default 14:15 (uses 36 bars instead of 39 bars)
    enriched_1400 = enrich_universe_with_intraday_features(
        universe_1d,
        universe_5m,
        cutoff_time="14:00",
    )
    df_1400 = enriched_1400["SYM_A"]
    assert df_1400.loc[d_last, "Close_5m_Cutoff"] == pytest.approx(bar_36_close)
    assert df_1400.loc[d_last, "Close_5m_Cutoff"] != df_default.loc[d_last, "Close_5m_Cutoff"]

    # 3. datetime.time(14, 0) works without raising errors and gives identical result
    enriched_time_obj = enrich_universe_with_intraday_features(
        universe_1d,
        universe_5m,
        cutoff_time=datetime.time(14, 0),
    )
    df_time_obj = enriched_time_obj["SYM_A"]
    assert df_time_obj.loc[d_last, "Close_5m_Cutoff"] == pytest.approx(bar_36_close)

    # 4. When config is provided along with cutoff_time, replace(config) updates cutoff_time
    #    while preserving other config attributes without mutating caller config.
    orig_config = IntradayFeatureConfig(
        cutoff_time="15:00",
        smart_money_top_pct=0.35,
        enable_chan_5m=False,
        volume_seasonality_lookback_days=10,
    )
    with patch("common.intraday_features.compute_daily_intraday_features", wraps=compute_daily_intraday_features) as spy:
        enriched_custom = enrich_universe_with_intraday_features(
            universe_1d,
            universe_5m,
            config=orig_config,
            cutoff_time="14:00",
        )
        assert spy.called
        called_config = spy.call_args.kwargs["config"]
        # replace() created a new instance with updated cutoff_time
        assert called_config is not orig_config
        assert called_config.cutoff_time == "14:00"
        # Preserved other attributes
        assert called_config.smart_money_top_pct == 0.35
        assert called_config.enable_chan_5m is False
        assert called_config.volume_seasonality_lookback_days == 10
        # Original config is unmutated
        assert orig_config.cutoff_time == "15:00"

    assert enriched_custom["SYM_A"].loc[d_last, "Close_5m_Cutoff"] == pytest.approx(bar_36_close)


# Alias for backward compatibility
test_enrich_universe_with_cutoff_time_kwarg = test_enrich_universe_with_intraday_features_cutoff_time_kwarg





# ---------------------------------------------------------------------------
# L2.13 follow-up: the Step-1.5 DuckDBCache 5m fallback in
# `load_intraday_5m_universe` is READ-ONLY by intent. Before 2026-10-23 it used
# the default writable constructor inside a blanket `except Exception`, so once
# the production-path guard (`refuse_production_cache_path`) started refusing
# writable opens of `<repo>/data/cache.duckdb` under pytest, the fallback
# silently returned an empty frame -- "no data" reported as a normal result.
# These tests pin the two required behaviours: read-only opens keep working
# under the guard, and every unavailable path is an explicit WARNING instead of
# a silent empty return. All three tests use `tmp_path` only; the live
# `<repo>/data/cache.duckdb` is never opened, because the candidate-path helper
# is monkeypatched while the guard is simulated with `production_cache_roots`.
# ---------------------------------------------------------------------------


def _patch_5m_cache_candidates(monkeypatch, db_path):
    """Point the 5m fallback at a single fake cache path (never the live one)."""
    import common.intraday_features as intraday_mod

    monkeypatch.setattr(
        intraday_mod, "_intraday_5m_cache_candidates", lambda cache_dir: [str(db_path)]
    )
    return intraday_mod


def test_load_intraday_5m_universe_fallback_reads_cache_read_only_under_guard(tmp_path, monkeypatch, caplog):
    """Read-only 5m fallback still works while the L2.13 guard is active (regression)."""
    from common import duckdb_cache as cache_mod

    fake_data_dir = tmp_path / "data"  # simulates <repo>/data, but inside tmp_path
    fake_data_dir.mkdir()
    db_file = fake_data_dir / "cache.duckdb"

    # Seed the cache while the guard still considers tmp_path non-production.
    seed = DuckDBCache(str(db_file))
    bars = _generate_test_3day_5m_bars("2026-10-14")
    seed.store_intraday_bars("BaoStockDataProvider", "AAA", bars, interval="5m")

    intraday_mod = _patch_5m_cache_candidates(monkeypatch, db_file)

    # Simulate "this directory is the production cache root": a writable open is now
    # refused exactly as the guard refuses `<repo>/data/cache.duckdb` under pytest.
    monkeypatch.setattr(cache_mod, "production_cache_roots", lambda: [str(fake_data_dir)])
    with pytest.raises(cache_mod.ProductionCachePathError):
        DuckDBCache(str(db_file))

    with caplog.at_level(logging.WARNING, logger="common.intraday_features"):
        out = intraday_mod.load_intraday_5m_universe(
            ["AAA"],
            start="2026-10-14",
            end="2026-10-16",
            parquet_dir=str(tmp_path / "no_parquet"),
        )

    # Behaviour identical to the pre-guard code: real bars, no "unavailable" warning.
    assert len(out["AAA"]) == 144
    assert out["AAA"].attrs.get("intraday_5m_available") is not False
    assert "5m 缓存不可用" not in caplog.text


def test_load_intraday_5m_universe_fallback_refused_open_warns_and_marks_unavailable(tmp_path, monkeypatch, caplog):
    """A refused read-only open logs WARNING and returns the explicit unavailable frame."""
    from common import duckdb_cache as cache_mod

    db_file = tmp_path / "cache.duckdb"
    db_file.write_bytes(b"")  # exists so the fallback attempts to open it

    def _refuse(self, db_path=None, read_only=False):
        raise cache_mod.ProductionCachePathError(
            "Refusing to open the DuckDB cache read-write while running under pytest (simulated)"
        )

    monkeypatch.setattr(cache_mod.DuckDBCache, "__init__", _refuse)
    intraday_mod = _patch_5m_cache_candidates(monkeypatch, db_file)

    with caplog.at_level(logging.WARNING, logger="common.intraday_features"):
        out = intraday_mod.load_intraday_5m_universe(
            ["AAA"], parquet_dir=str(tmp_path / "no_parquet")
        )

    assert out["AAA"].empty
    assert out["AAA"].attrs["intraday_5m_available"] is False
    assert any(
        r.levelno == logging.WARNING and "5m 缓存不可用" in r.getMessage() and "AAA" in r.getMessage()
        for r in caplog.records
    ), f"expected an explicit 5m-cache-unavailable warning, got: {[r.getMessage() for r in caplog.records]}"


def test_load_intraday_5m_universe_fallback_zero_rows_warns_and_marks_unavailable(tmp_path, monkeypatch, caplog):
    """A cache that opens read-only but holds 0 rows is 'unavailable', not a normal result."""
    db_file = tmp_path / "cache.duckdb"
    DuckDBCache(str(db_file))  # schema only: no intraday bars stored

    intraday_mod = _patch_5m_cache_candidates(monkeypatch, db_file)

    with caplog.at_level(logging.WARNING, logger="common.intraday_features"):
        out = intraday_mod.load_intraday_5m_universe(
            ["AAA"], parquet_dir=str(tmp_path / "no_parquet")
        )

    assert out["AAA"].empty
    assert out["AAA"].attrs["intraday_5m_available"] is False
    assert any(
        r.levelno == logging.WARNING and "5m 缓存不可用" in r.getMessage() and "0 行" in r.getMessage()
        for r in caplog.records
    ), f"expected a 0-row unavailability warning, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------------------------
# L2.7: `is_intraday_range_covered` gains the daily gate's bar-layer `MAX(ts)` end check.
#
# DISCLOSURE (do not overstate this): the defect was LATENT, not an incident. Fuyao does not serve
# 5m at all (`common/financial_api.py` raises for any non-`1d` interval), so no real intraday cache
# is populated from upstream today and no stale 5m price has ever reached a ticket. Without this
# fix, wiring in a real 5m source would have copied the 2026-10-22 "metadata says today, bars stop
# yesterday" staleness from the daily axis to the intraday axis.
#
# The rule is NOT a third implementation: both axes go through
# `common/duckdb_cache.py::_expected_session_is_fresh` / `_bar_layer_reaches_end`
# (see `test_intraday_and_daily_gates_share_one_expected_session_rule`).
# ---------------------------------------------------------------------------------------------


def _intraday_5m_frame(start_day: str, days: int) -> pd.DataFrame:
    """Synthetic 5m bars, one A-share-shaped session per day, offline and deterministic.

    Shape: 09:35-11:30 and 13:05-15:00 (5-minute steps, lunch break skipped), so the last bar of
    each day is the 15:00 close. `L2.26` note: this fixture used to run 09:35..15:00 at
    **30-minute** steps, i.e. its last bar was 14:35 -- a session that never actually reached the
    close. Nothing noticed because freshness compared *dates*; now that the expected-session gate
    also checks the session time, the fixture has to describe a real (complete) session, otherwise
    `test_l2_7_intraday_expected_session_covered_when_ts_reaches_the_session` would be asserting
    coverage of a truncated day.
    """
    frames = []
    for d in pd.bdate_range(start_day, periods=days):
        date_str = d.strftime("%Y-%m-%d")
        times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 11:30:00", freq="5min").append(
            pd.date_range(f"{date_str} 13:05:00", f"{date_str} 15:00:00", freq="5min")
        )
        n = len(times)
        frames.append(pd.DataFrame(
            {"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
             "Close": [100.5] * n, "Volume": [10.0] * n}, index=times))
    return pd.concat(frames, axis=0)


def _drop_last_intraday_day(cache, provider, symbol, interval, day: str):
    """Remove one session's bars while leaving the metadata row untouched.

    This is the exact shape the daily incident had: `intraday_sync_metadata` keeps saying "through
    <day>", the `intraday_ohlcv_bars` table stops a session earlier.
    """
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ? AND ts >= ?",
            [provider, symbol, interval, pd.Timestamp(day)],
        )
    finally:
        if not cache.is_memory:
            con.close()


def test_l2_7_intraday_metadata_cannot_claim_a_session_the_bars_do_not_reach():
    """L2.7 core case: metadata says 2026-10-16, `MAX(ts)` stops on 2026-10-15.

    Under an expectation the gate must say "not covered" (the six-symbol shape, intraday axis).
    The same state under `expected_last_session=None` stays "covered" -- the legacy `count(*) > 0`
    heuristic is frozen by L2.2's hard constraint, and the frozen table in
    `test_duckdb_cache.py::test_case10_none_path_matches_recorded_pre_l2_2_intraday_behaviour`
    pins that.
    """
    cache = DuckDBCache(db_path=":memory:")
    provider, symbol = "FuyaoDataProvider", "600000.SH"
    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        sync_time=datetime.datetime(2026, 10, 16, 16, 0),
    )
    _drop_last_intraday_day(cache, provider, symbol, "5m", "2026-10-16")

    meta = cache.get_intraday_metadata(provider, symbol, "5m")
    assert meta["latest_available_ts"].date() == datetime.date(2026, 10, 16), "fixture drifted"

    now = pd.Timestamp("2026-10-16 16:30:00")
    # Legacy/research path: unchanged (metadata + count > 0).
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now
    ) is True
    # Expected-session path: the bar layer says the session never landed -> not covered.
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=datetime.date(2026, 10, 16),
    ) is False
    # ...and the daily-facing facade forwards the same expectation (single gate, not two callers
    # each remembering to check).
    assert cache.is_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=datetime.date(2026, 10, 16),
    ) is False


def test_l2_7_intraday_expected_session_covered_when_ts_reaches_the_session():
    """Positive half: once `MAX(ts)` really reaches the session, coverage is reported (no
    over-rejection loop that would re-fetch 5m data forever)."""
    cache = DuckDBCache(db_path=":memory:")
    provider, symbol = "FuyaoDataProvider", "600000.SH"
    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        sync_time=datetime.datetime(2026, 10, 16, 16, 0),
    )
    now = pd.Timestamp("2026-10-16 16:30:00")
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=datetime.date(2026, 10, 16),
    ) is True
    # One session later there is no such bar: uncovered, even though the metadata "synced" 10-16.
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=datetime.date(2026, 10, 19),
    ) is False


def test_intraday_and_daily_gates_share_one_expected_session_rule():
    """L2.2 x L2.7 anti-drift guard: both axes must import the SAME decision helpers.

    A re-implementation would be invisible in behaviour tests until the day one side changes
    (design §7: two drifting checks are the bug class this whole track exists to remove), so the
    wiring itself is asserted here: the two public gates call the shared helpers, and no third
    copy of "latest_available_date >= expected" exists in the module.
    """
    import inspect

    import common.duckdb_cache as cache_mod

    daily_src = inspect.getsource(cache_mod.DuckDBCache.is_range_covered)
    intraday_src = inspect.getsource(cache_mod.DuckDBCache.is_intraday_range_covered)

    assert "_expected_session_is_fresh" in daily_src
    assert "_bar_layer_reaches_end" in daily_src
    assert "_expected_session_is_fresh" in intraday_src
    assert "_bar_layer_reaches_end" in intraday_src
    # The shared helpers live exactly once, at module level, and only they compare the session.
    module_src = inspect.getsource(cache_mod)
    assert module_src.count("def _expected_session_is_fresh(") == 1
    assert module_src.count("def _bar_layer_reaches_end(") == 1


# ---------------------------------------------------------------------------------------------
# L2.14: `store_intraday_bars` / `intraday_sync_metadata` -- the intraday copy of `L2.6`.
#
# DISCLOSURE (do not overstate this): the defect was LATENT, exactly like `L2.7`'s. Fuyao serves no
# intraday interval at all (`common/financial_api.py` raises for any non-`1d`), so the real 5m cache
# has never been written by a real upstream and no intraday metadata has been corrupted yet. The
# dangerous shapes were:
#   * a blind `INSERT OR REPLACE` (a DELETE + INSERT in DuckDB) that ignored the bar table and also
#     wiped the `L2.2` probe bookkeeping columns on every sync; and
#   * a `requested_start` branch that ran `earliest_avail = <this frame's first bar>`
#     UNCONDITIONALLY, so a sync that asked for an early window but was served a later one ROLLED
#     THE RECORDED EARLIEST BOUNDARY FORWARD -- the 2026-10-22 downgrade incident, 5m axis.
#
# Each test below is paired with the daily-side test it mirrors, as L2.14's acceptance requires:
#   later window / earlier backfill / sticky flag / first insert / idempotent
#   == test_duckdb_cache.py::test_store_bars_{later_window,earliest_can_still_improve,
#      is_earliest_known_is_sticky_true,first_insert_records_incoming_values_verbatim,
#      metadata_upsert_is_idempotent}
# ---------------------------------------------------------------------------------------------


def _intraday_new_cache() -> DuckDBCache:
    return DuckDBCache(db_path=":memory:")


def _force_intraday_metadata(cache, provider, symbol, interval="5m", **fields):
    """Injects a metadata state the bars do not support (legacy/hand-edited row)."""
    assignments = ", ".join(f"{key} = ?" for key in fields)
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            f"UPDATE intraday_sync_metadata SET {assignments} "
            f"WHERE provider = ? AND symbol = ? AND interval = ?",
            [*fields.values(), provider, symbol, interval],
        )
    finally:
        if not cache.is_memory:
            con.close()


def _delete_intraday_metadata_row(cache, provider, symbol, interval="5m"):
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, symbol, interval],
        )
    finally:
        if not cache.is_memory:
            con.close()


def test_l2_14_intraday_later_window_does_not_downgrade_earliest_available_ts():
    """Mirrors `test_store_bars_later_window_does_not_downgrade_earliest_available_date`.

    HONEST NOTE: this particular direction was already preserved by the old Python-side
    `min(prior, frame_min)` (so this test alone does NOT discriminate against the pre-L2.14 code).
    It is kept because it is the contract the rewritten `LEAST` upsert must not lose, and because a
    naive "bar fact wins unconditionally, computed from this frame" rewrite would break it.
    """
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "600362.SH"
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2022-01-04", 3), interval="5m")
    before = cache.get_intraday_metadata(provider, symbol, "5m")
    assert before["earliest_available_ts"].date() == datetime.date(2022, 1, 4)
    assert before["is_earliest_known"] is False

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        requested_start="2026-10-14", requested_end="2026-10-16",
    )
    after = cache.get_intraday_metadata(provider, symbol, "5m")
    assert after["earliest_available_ts"].date() == datetime.date(2022, 1, 4), (
        "earliest_available_ts was downgraded to the later window's first bar while "
        "intraday_ohlcv_bars still holds 2022 rows"
    )
    assert after["is_earliest_known"] is False


def test_l2_14_requested_start_must_not_roll_the_earliest_boundary_forward():
    """DISCRIMINATING (fails on the pre-L2.14 implementation).

    Old code: `if requested_start < frame_min - 7d: earliest_avail = frame_min` -- unconditional,
    so asking for 2020 and being served 2026 rewrote the recorded earliest boundary from
    2022-01-04 to 2026-10-14. The boundary must stay a bar fact; only `is_earliest_known` may
    legitimately become True, because "asked from 2020 and got nothing older back" is a probe.
    """
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "600036.SH"
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2022-01-04", 3), interval="5m")

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        requested_start="2020-01-01", requested_end="2026-10-16",
    )
    meta = cache.get_intraday_metadata(provider, symbol, "5m")
    assert meta["earliest_available_ts"].date() == datetime.date(2022, 1, 4), (
        "the requested_start branch rolled the earliest boundary forward to the window's first bar"
    )
    assert meta["latest_available_ts"].date() == datetime.date(2026, 10, 16)
    # The probe claim itself is legitimate and stays: we asked before everything we know.
    assert meta["is_earliest_known"] is True
    # ...and the 2022 bars are still there, so the metadata now describes the data.
    bars = cache.query_intraday_bars(provider, symbol, interval="5m", start="2022-01-01")
    assert bars.index.min().date() == datetime.date(2022, 1, 4)


def test_l2_14_metadata_row_missing_is_rebuilt_from_bars_not_from_the_window():
    """Mirrors `test_missing_metadata_row_is_rebuilt_from_bar_facts_not_request_window` (L2.18).

    DISCRIMINATING: the old code rebuilt a missing row from `min(frame_min, requested_start)` --
    i.e. a row rebuilt from a one-month window claimed that one month was the whole history.
    """
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "601318.SH"
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2022-01-04", 3), interval="5m")
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2026-10-13", 4), interval="5m")
    _delete_intraday_metadata_row(cache, provider, symbol)
    assert cache.get_intraday_metadata(provider, symbol, "5m") is None

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 1), interval="5m",
        requested_start="2026-10-14", requested_end="2026-10-14",
    )
    rebuilt = cache.get_intraday_metadata(provider, symbol, "5m")
    assert rebuilt["earliest_available_ts"].date() == datetime.date(2022, 1, 4), (
        "the row was rebuilt from the request window instead of the bar table"
    )
    assert rebuilt["latest_available_ts"].date() == datetime.date(2026, 10, 16)


def test_l2_14_earliest_later_than_the_oldest_stored_bar_is_restored_from_the_bar_fact():
    """DISCRIMINATING: metadata claiming a later start than the rows on disk was preserved by the
    old `min(prior, frame)` merge (the lie survived forever); it is now clamped to `MIN(ts)`."""
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "601988.SH"
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2022-01-04", 3), interval="5m")
    _force_intraday_metadata(
        cache, provider, symbol, earliest_available_ts=pd.Timestamp("2026-10-01 09:35:00")
    )

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 2), interval="5m",
        requested_start="2026-10-14",
    )
    meta = cache.get_intraday_metadata(provider, symbol, "5m")
    assert meta["earliest_available_ts"].date() == datetime.date(2022, 1, 4)


def test_l2_14_is_earliest_known_is_sticky_true():
    """Mirrors `test_store_bars_is_earliest_known_is_sticky_true`.

    HONEST NOTE: like the daily original, this direction is NOT discriminating against the
    pre-L2.14 code (it carried the flag through its Python-side computation). It guards the
    rewritten upsert against the classic regression `is_earliest_known = excluded.is_earliest_known`
    which would clear a previously-confirmed True.
    """
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "600030.SH"
    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2024-01-02", 3), interval="5m",
        requested_start="2020-01-01",
    )
    assert cache.get_intraday_metadata(provider, symbol, "5m")["is_earliest_known"] is True

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        requested_start="2026-10-14",
    )
    assert cache.get_intraday_metadata(provider, symbol, "5m")["is_earliest_known"] is True


def test_l2_14_metadata_write_is_one_upsert_not_a_blind_replace():
    """DISCRIMINATING, but at the STATEMENT level -- and the honesty matters here.

    DISCLOSED: a pure behaviour test cannot see this difference. DuckDB implements
    `INSERT OR REPLACE` as a per-column `ON CONFLICT DO UPDATE` over the columns named in the
    insert list, so the `L2.2` probe columns (`probe_attempts` / `last_probe_result`) survive
    either way -- verified by running the probe-preservation assertions below against the
    pre-L2.14 implementation, where they PASSED. The pre-L2.14 defect was therefore never
    "it wiped the probe columns"; it was "it wrote window-derived values with no reconciliation
    against the bar table" (which the other `l2_14` tests pin behaviourally).

    So the blind write itself is pinned where it is unambiguous: the intraday metadata write must
    be an explicit directional upsert (`ON CONFLICT ... DO UPDATE` + `LEAST` / `GREATEST` + sticky
    `OR`), and no `INSERT OR REPLACE INTO intraday_sync_metadata` may exist in the module.
    """
    import inspect

    import common.duckdb_cache as cache_mod

    module_src = inspect.getsource(cache_mod)
    assert "INSERT OR REPLACE INTO intraday_sync_metadata" not in module_src, (
        "the intraday metadata row is still written with a blind INSERT OR REPLACE"
    )
    store_src = inspect.getsource(cache_mod.DuckDBCache.store_intraday_bars)
    assert "ON CONFLICT (provider, symbol, interval) DO UPDATE" in store_src
    assert "LEAST(" in store_src, "earliest boundary must merge with MIN semantics"
    assert "GREATEST(" in store_src, "latest boundary must merge with MAX semantics"
    assert (
        "is_earliest_known = existing.is_earliest_known OR excluded.is_earliest_known" in store_src
    ), "is_earliest_known must stay sticky True"

    # Behavioural half (non-discriminating on its own -- see the disclosure above): a later sync
    # must not disturb the stored probe bookkeeping.
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "600519.SH"
    cache.store_intraday_bars(provider, symbol, _intraday_5m_frame("2026-10-14", 2), interval="5m")
    _force_intraday_metadata(
        cache, provider, symbol, probe_attempts=3, last_probe_result="exhausted"
    )

    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-16", 1), interval="5m",
        requested_start="2026-10-16",
    )
    con = cache._get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT probe_attempts, last_probe_result FROM intraday_sync_metadata "
            "WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, symbol, "5m"],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()
    assert row == (3, "exhausted"), (
        "probe bookkeeping was wiped; the upsert must not re-enumerate every column"
    )


def test_l2_14_intraday_metadata_upsert_is_idempotent():
    """Mirrors `test_store_bars_metadata_upsert_is_idempotent`: the same frame twice changes none
    of the three monotone fields (only `last_sync_time` may move) and does not duplicate bars."""
    cache = _intraday_new_cache()
    provider, symbol = "BaoStockDataProvider", "601166.SH"
    frame = _intraday_5m_frame("2026-10-14", 3)
    cache.store_intraday_bars(
        provider, symbol, frame, interval="5m", requested_start="2026-10-14",
        sync_time=datetime.datetime(2026, 10, 16, 16, 0),
    )
    first = cache.get_intraday_metadata(provider, symbol, "5m")
    cache.store_intraday_bars(
        provider, symbol, frame, interval="5m", requested_start="2026-10-14",
        sync_time=datetime.datetime(2026, 10, 16, 17, 0),
    )
    second = cache.get_intraday_metadata(provider, symbol, "5m")

    assert second["earliest_available_ts"] == first["earliest_available_ts"]
    assert second["latest_available_ts"] == first["latest_available_ts"]
    assert second["is_earliest_known"] == first["is_earliest_known"]
    assert first["last_sync_time"] != second["last_sync_time"]
    assert len(cache.query_intraday_bars(provider, symbol, interval="5m")) == len(frame)


def test_l2_14_intraday_metadata_uses_the_shared_daily_reconciliation_core():
    """Anti-drift guard (same idea as L2.7's shared-helper assertion): the intraday reconciliation
    must delegate to the ONE shared core instead of re-deriving the rule. A second copy would go
    stale silently -- the exact bug class L2.14 removes."""
    import inspect

    import common.duckdb_cache as cache_mod

    module_src = inspect.getsource(cache_mod)
    assert module_src.count("def _reconcile_sync_metadata_with_bars(") == 1
    daily_src = inspect.getsource(cache_mod._reconcile_asset_metadata_with_bars)
    intraday_src = inspect.getsource(cache_mod._reconcile_intraday_metadata_with_bars)
    for src in (daily_src, intraday_src):
        assert "_reconcile_sync_metadata_with_bars(" in src
        # No re-derivation of the priority rule inside either wrapper.
        assert "bars_min is not None" not in src


# ---------------------------------------------------------------------------------------------
# L2.15: `scripts/sync_china_5m_and_indices.py` must not bypass the cache's inference.
#
# The script used to run, right after syncing one symbol,
#     UPDATE asset_sync_metadata SET is_earliest_known = TRUE
#            WHERE symbol = ? AND provider IN (<BaoStock, EastMoney, Fuyao, MarketDB>)
# (and the intraday twin `UPDATE intraday_sync_metadata SET is_earliest_known = TRUE`), turning
# "this symbol was synced" into "every provider's earliest boundary is a known fact". It also
# handed `store_bars` a FRONT-TRUNCATED patch frame with `requested_start=patch_start`, i.e. the
# exact argument shape the pre-L2.6 cache misread as "the earliest reachable date is patch_start".
#
# These tests drive the real script functions offline against fake providers -- the script is
# NEVER run against the production cache (L2.15 item 4), and every frame here is synthetic.
#
# Placement note: kept in `common/tests/` (not `pipeline/tests/`) because every assertion is a
# *cache metadata* invariant (`asset_sync_metadata` / `intraday_sync_metadata` vs `*_ohlcv_bars`),
# the synthetic-provider harness already lives in this file, and this file is inside the assigned
# ownership. The script module is loaded with `importlib` exactly like
# `pipeline/tests/test_live_path_hardening.py` loads `scripts/check_live_portfolio_health.py`.
# ---------------------------------------------------------------------------------------------


_NC_DAILY_PROVIDERS = [
    "BaoStockDataProvider",
    "EastMoneyDataProvider",
    "FuyaoDataProvider",
    "MarketDBDataProvider",
]


def _load_sync_script_module():
    """Import `scripts/sync_china_5m_and_indices.py` as a module (standalone script)."""
    import importlib.util

    path = os.path.join(_REPO_ROOT, "scripts", "sync_china_5m_and_indices.py")
    spec = importlib.util.spec_from_file_location("sync_china_5m_and_indices_l215", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSyncProvider:
    """Offline provider double: returns the slice of a synthetic frame inside [start, end]."""

    def __init__(self, frame: pd.DataFrame, calls=None):
        self.frame = frame
        self.calls = calls if calls is not None else []

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        self.calls.append((symbol, start, end, interval))
        if self.frame is None or self.frame.empty:
            return self.frame.copy() if self.frame is not None else pd.DataFrame()
        mask = (self.frame.index >= pd.Timestamp(start)) & (
            self.frame.index <= pd.Timestamp(end) + pd.Timedelta(days=1)
        )
        return self.frame[mask].copy()

    def logout(self):
        pass


def _synthetic_daily_frame(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low": [99.0] * n,
            "Close": [100.5] * n,
            "Volume": [1000.0] * n,
        },
        index=idx,
    )


def _seed_daily_history(cache, provider: str, symbol: str, start: str, end: str) -> None:
    cache.store_bars(provider, symbol, _synthetic_daily_frame(start, end), requested_start=start)


def test_l2_15_single_symbol_index_sync_does_not_mark_other_namespaces_earliest_known(monkeypatch):
    """DISCRIMINATING: the old unconditional `UPDATE ... is_earliest_known = TRUE` (four provider
    namespaces, after a sync that never probed before the first bar) marked every namespace True.
    Scenario 1 of the three required: 单标的同步."""
    mod = _load_sync_script_module()
    cache = DuckDBCache(db_path=":memory:")

    # An unrelated symbol already in all four namespaces: a sync of 000300.SH must not touch it.
    for prov in _NC_DAILY_PROVIDERS:
        _seed_daily_history(cache, prov, "600000.SH", "2016-01-04", "2016-01-29")

    frame = _synthetic_daily_frame("2016-01-04", "2016-12-30")
    monkeypatch.setattr(mod, "BaoStockDataProvider", lambda **kw: _FakeSyncProvider(frame))
    monkeypatch.setattr(mod, "EastMoneyDataProvider", lambda **kw: _FakeSyncProvider(frame))

    mod.sync_indices_daily(cache, ["000300.SH"], start="2016-01-01", end="2016-12-31")

    for prov in _NC_DAILY_PROVIDERS:
        meta = cache.get_asset_metadata(prov, "000300.SH")
        assert meta is not None, f"{prov} was never stored"
        assert meta["earliest_available_date"] == datetime.date(2016, 1, 4)
        assert meta["latest_available_date"] == datetime.date(2016, 12, 30)
        assert meta["is_earliest_known"] is False, (
            f"{prov}: a sync that never asked before the first bar must not claim the earliest "
            f"boundary is known"
        )
        # ...and the unrelated symbol is untouched by the sync of 000300.SH.
        other = cache.get_asset_metadata(prov, "600000.SH")
        assert other["earliest_available_date"] == datetime.date(2016, 1, 4)
        assert other["latest_available_date"] == datetime.date(2016, 1, 29)
        assert other["is_earliest_known"] is False


def test_l2_15_patch_branch_never_lets_a_front_truncated_window_move_the_boundaries(monkeypatch):
    """DISCRIMINATING on the recorded `requested_start` (the L2.15 argument-shape fix).

    Scenario 2 of the three required: PATCH 补丁. The patch frame is front-truncated to `> max_date`
    by the qfq overlap alignment, so the stored frame starts at 2026-10-22 while `patch_start`
    (the look-back anchor) is 2026-10-16. Passing `patch_start` claims a request the stored frame
    cannot back; a tail patch must not speak about the past. The metadata boundaries themselves
    remain the bar facts (`store_bars` cross-checks MIN/MAX and never the window).
    """
    mod = _load_sync_script_module()
    cache = DuckDBCache(db_path=":memory:")
    symbol = "002371.SZ"

    for prov in ("FuyaoDataProvider", "MarketDBDataProvider"):
        _seed_daily_history(cache, prov, symbol, "2016-01-04", "2026-10-21")

    fetched = _synthetic_daily_frame("2026-10-14", "2026-10-26")  # overlaps the cached tail
    monkeypatch.setattr(mod, "BaoStockDataProvider", lambda **kw: _FakeSyncProvider(fetched))
    monkeypatch.setattr(mod, "EastMoneyDataProvider", lambda **kw: _FakeSyncProvider(fetched))

    recorded = []
    real_store = cache.store_bars

    def _spy(*args, **kwargs):
        recorded.append(kwargs)
        return real_store(*args, **kwargs)

    cache.store_bars = _spy  # instance-level spy; the script only calls cache.store_bars

    mod.patch_equity_daily_bars(cache, [symbol], end="2026-10-26")

    assert recorded, "the patch branch never wrote anything -- fixture drifted"
    for kwargs in recorded:
        assert kwargs["symbol"] == symbol
        assert kwargs["requested_start"] == "2026-10-22", (
            f"{kwargs['provider']}: the patch passed "
            f"requested_start={kwargs['requested_start']!r}; a front-truncated frame may only "
            f"describe its own first bar"
        )

    for prov in ("FuyaoDataProvider", "MarketDBDataProvider"):
        meta = cache.get_asset_metadata(prov, symbol)
        assert meta["earliest_available_date"] == datetime.date(2016, 1, 4), (
            "the patch rolled the earliest boundary forward"
        )
        assert meta["latest_available_date"] == datetime.date(2026, 10, 26)
        assert meta["is_earliest_known"] is False
        # The qfq overlap alignment kept exactly one copy of the overlapping bars.
        bar_count = len(cache.query_bars(prov, symbol))
        assert bar_count == len(_synthetic_daily_frame("2016-01-04", "2026-10-26"))


def test_l2_15_early_backfill_improves_earliest_without_fabricating_is_earliest_known(monkeypatch):
    """DISCRIMINATING: the old code marked all four namespaces True on the *first* (2024-only)
    sync, before any early window was ever requested. Scenario 3: 早期回补."""
    mod = _load_sync_script_module()
    cache = DuckDBCache(db_path=":memory:")

    frame = _synthetic_daily_frame("2016-01-04", "2024-01-31")
    monkeypatch.setattr(mod, "BaoStockDataProvider", lambda **kw: _FakeSyncProvider(frame))
    monkeypatch.setattr(mod, "EastMoneyDataProvider", lambda **kw: _FakeSyncProvider(frame))

    # (a) A late-window-only sync: 2024 rows, earliest = 2024-01-02, no boundary claim.
    mod.sync_indices_daily(cache, ["000300.SH"], start="2024-01-02", end="2024-01-31")
    for prov in _NC_DAILY_PROVIDERS:
        meta = cache.get_asset_metadata(prov, "000300.SH")
        assert meta["earliest_available_date"] == datetime.date(2024, 1, 2)
        assert meta["latest_available_date"] == datetime.date(2024, 1, 31)
        assert meta["is_earliest_known"] is False

    # (b) The genuine backfill: earliest moves earlier, latest is NOT lost (MAX semantics).
    mod.sync_indices_daily(cache, ["000300.SH"], start="2016-01-01", end="2016-12-31")
    for prov in _NC_DAILY_PROVIDERS:
        meta = cache.get_asset_metadata(prov, "000300.SH")
        assert meta["earliest_available_date"] == datetime.date(2016, 1, 4)
        assert meta["latest_available_date"] == datetime.date(2024, 1, 31)
        assert meta["is_earliest_known"] is False, (
            "requested_start=2016-01-01 is not >7 days before the first bar 2016-01-04, so the "
            "probe rule is not satisfied -- the flag must stay False rather than be asserted"
        )


def test_l2_15_5m_symbol_sync_does_not_assert_intraday_earliest_known(monkeypatch):
    """DISCRIMINATING: the old 5m path ran an unconditional intraday
    `UPDATE ... is_earliest_known = TRUE` whenever the frame began at/before 2022-01-10."""
    mod = _load_sync_script_module()
    cache = DuckDBCache(db_path=":memory:")
    symbol = "600000.SH"
    frame = _intraday_5m_frame("2022-01-04", 3)  # reaches the "early 2022" trigger of the old code

    monkeypatch.setattr(mod, "BaoStockDataProvider", lambda **kw: _FakeSyncProvider(frame))
    monkeypatch.setattr(mod, "EastMoneyDataProvider", lambda **kw: _FakeSyncProvider(pd.DataFrame()))

    mod.sync_5m_intraday(cache, [symbol], [], start="2022-01-01", end="2022-01-31")

    for prov in ("BaoStockDataProvider", "EastMoneyDataProvider"):
        meta = cache.get_intraday_metadata(prov, symbol, "5m")
        assert meta is not None, f"{prov} 5m row missing"
        assert meta["earliest_available_ts"].date() == datetime.date(2022, 1, 4)
        assert meta["is_earliest_known"] is False, (
            f"{prov}: a hard-coded 'early 2022 means earliest' guess must not become a stored fact"
        )


def test_l2_15_script_performs_no_direct_cache_writes():
    """Static anti-regression guard (L2.15 items 1/3): the script must never write the cache
    directly -- no `UPDATE`/`INSERT` statement may exist in it, so `is_earliest_known` can only be
    set by `store_bars` / `store_intraday_bars` (the one place that cross-checks the bar table).

    Parsed with `ast` rather than a text search: the file's docstrings legitimately *quote* the
    removed `UPDATE ... SET is_earliest_known = TRUE` to document why it is gone, and a substring
    guard would fail on its own documentation.
    """
    import ast

    path = os.path.join(_REPO_ROOT, "scripts", "sync_china_5m_and_indices.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    statements = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "execute":
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            sql = arg.value
        elif isinstance(arg, ast.JoinedStr):
            sql = "".join(
                part.value for part in arg.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        else:  # pragma: no cover - defensive
            sql = ""
        statements.append(sql.strip())

    assert statements, "fixture drifted: no con.execute(...) found at all"
    for sql in statements:
        assert sql.upper().startswith("SELECT"), (
            f"the sync script issues a direct write against the cache: {sql!r} -- all writes must "
            f"go through store_bars/store_intraday_bars, which own the metadata rule"
        )


# ---------------------------------------------------------------------------------------------
# L2.26: intraday freshness is DATE **and** SESSION TIME, not date alone.
#
# The defect (independently re-verified, `L2.26`): the expected-session branch compared
# `MAX(ts).date()` with `expected_last_session`. So a cache whose bars stopped at 11:30 (lunch) --
# or even at the very first 09:35 bar -- was reported covered for the whole day, because the date
# matched. That is the 2026-10-22 "metadata says today, bar stops mid-session" defect on the
# intraday axis. The fix adds an OPTIONAL session-time input to the SAME shared core
# (`_bar_layer_reaches_end`) and takes the close from `common/market_clock.py`
# (`last_expected_bar_ts` = market close minus one bar length), never from a literal here.
#
# The gate does NOT need a LUNCH_BREAK state: the deadline is anchored to the afternoon close, so
# "last bar 11:30" and "session closed at 15:00" are distinguished by the clock itself.
# ---------------------------------------------------------------------------------------------


def _truncate_intraday_session(cache, provider, symbol, interval, day: str, last_time: str):
    """Delete the tail of `day` after `last_time`, leaving the metadata row untouched.

    This is the exact shape of the defect: `intraday_sync_metadata.latest_available_ts` still
    says "15:00, full session", while the bar table stops mid-session.
    """
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ? "
            "AND ts > ? AND ts < ?",
            [provider, symbol, interval, pd.Timestamp(f"{day} {last_time}"),
             pd.Timestamp(day) + pd.Timedelta(days=1)],
        )
    finally:
        if not cache.is_memory:
            con.close()


def _stored_three_day_5m(cache, symbol, provider="FuyaoDataProvider"):
    cache.store_intraday_bars(
        provider, symbol, _intraday_5m_frame("2026-10-14", 3), interval="5m",
        sync_time=datetime.datetime(2026, 10, 16, 16, 0),
    )
    return provider


def test_l2_26_first_bar_of_the_expected_session_is_not_covered():
    """DISCRIMINATING: only the 09:35 bar of the expected session is on disk.

    The old implementation compared dates, so `MAX(ts).date() == 2026-10-16` read as "covered"
    and a live run would have marked positions with a single opening bar.
    """
    cache = DuckDBCache(db_path=":memory:")
    symbol = "600000.SH"
    provider = _stored_three_day_5m(cache, symbol)
    _truncate_intraday_session(cache, provider, symbol, "5m", "2026-10-16", "09:35")

    now = pd.Timestamp("2026-10-16 16:30:00")
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=datetime.date(2026, 10, 16),
    ) is False, "a single 09:35 bar must not count as a completed session"

    # The `expected_last_session=None` contract is untouched by L2.26 (legacy path).
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
    ) is True


def test_l2_26_lunch_break_1130_is_not_covered(caplog):
    """DISCRIMINATING: bars stop at the morning close (11:30), afternoon never synced.

    This is the "元数据说今天、bar 停半场" shape. It must be uncovered even though
    `intraday_sync_metadata` still claims the 15:00 session, and the failure must leave a
    diagnosable trace (expected deadline vs actual last ts).
    """
    cache = DuckDBCache(db_path=":memory:")
    symbol = "600000.SH"
    provider = _stored_three_day_5m(cache, symbol)
    _truncate_intraday_session(cache, provider, symbol, "5m", "2026-10-16", "11:30")
    assert cache.get_intraday_metadata(provider, symbol, "5m")["latest_available_ts"] == pd.Timestamp(
        "2026-10-16 15:00:00"
    ), "fixture drifted: metadata must still claim the full session"

    now = pd.Timestamp("2026-10-16 16:30:00")
    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        covered = cache.is_intraday_range_covered(
            provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
            expected_last_session=datetime.date(2026, 10, 16),
        )

    assert covered is False, "a lunch-break stop (11:30) must not count as a completed session"
    messages = [r.getMessage() for r in caplog.records if r.name == "common.duckdb_cache"]
    assert any(
        "2026-10-16T11:30:00" in m and "2026-10-16T14:55:00" in m for m in messages
    ), f"failure left no diagnosable trace (expected deadline vs actual last ts): {messages}"


def test_l2_26_close_reached_is_covered_and_one_bar_short_is_not():
    """Positive half plus the exact tolerance boundary (no over-rejection, no under-rejection).

    `last_expected_bar_ts` = close - one bar, so a bar stamped at 14:55 (bar-open convention) is
    accepted while 14:50 (5 minutes short) is not. The 14:50 assertion is the discriminating one:
    the old date-only rule accepted it.
    """
    provider, symbol = "FuyaoDataProvider", "600000.SH"
    now = pd.Timestamp("2026-10-16 16:30:00")
    expected = datetime.date(2026, 10, 16)

    full = DuckDBCache(db_path=":memory:")
    _stored_three_day_5m(full, symbol, provider)
    assert full.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=expected,
    ) is True, "a session that reaches the 15:00 close must stay covered"

    at_deadline = DuckDBCache(db_path=":memory:")
    _stored_three_day_5m(at_deadline, symbol, provider)
    _truncate_intraday_session(at_deadline, provider, symbol, "5m", "2026-10-16", "14:55")
    assert at_deadline.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=expected,
    ) is True, "close - one bar (14:55) must satisfy the deadline"

    short = DuckDBCache(db_path=":memory:")
    _stored_three_day_5m(short, symbol, provider)
    _truncate_intraday_session(short, provider, symbol, "5m", "2026-10-16", "14:50")
    assert short.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
        expected_last_session=expected,
    ) is False, "one bar short of the close must be uncovered"


def test_l2_26_empty_bar_table_is_not_covered():
    """A metadata row with zero bars must never read as covered (spec: `空 0 行 => 不 covered`)."""
    cache = DuckDBCache(db_path=":memory:")
    symbol = "600000.SH"
    provider = _stored_three_day_5m(cache, symbol)
    _drop_last_intraday_day(cache, provider, symbol, "5m", "2026-10-14")  # removes every bar
    assert cache.query_intraday_bars(provider, symbol, interval="5m").empty, "fixture drifted"

    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16",
        now=pd.Timestamp("2026-10-16 16:30:00"),
        expected_last_session=datetime.date(2026, 10, 16),
    ) is False


def test_l2_26_close_time_comes_from_the_market_spec_not_a_literal():
    """DISCRIMINATING across markets: the SAME bars are covered for China and not for HK/US.

    A 15:00 last bar is the A-share close (deadline 14:55) but four bars short of Hong Kong's
    (15:55) and US's (15:55) 16:00 close. A hard-coded "15:00" in `duckdb_cache.py` would have to
    answer all three alike; the market spec from `common/market_clock.py` is what splits them.
    """
    provider = "FuyaoDataProvider"
    now = pd.Timestamp("2026-10-16 16:30:00")
    expected = datetime.date(2026, 10, 16)
    frame = _intraday_5m_frame("2026-10-14", 3)

    cache = DuckDBCache(db_path=":memory:")
    for symbol in ("600000.SH", "00700.HK", "AAPL"):
        cache.store_intraday_bars(
            provider, symbol, frame, interval="5m",
            sync_time=datetime.datetime(2026, 10, 16, 16, 0),
        )

    results = {
        symbol: cache.is_intraday_range_covered(
            provider, symbol, interval="5m", start="2026-10-14", end="2026-10-16", now=now,
            expected_last_session=expected,
        )
        for symbol in ("600000.SH", "00700.HK", "AAPL")
    }
    assert results == {"600000.SH": True, "00700.HK": False, "AAPL": False}, results

    # A US session that really reaches 16:00 is covered (the rejection above is about the clock,
    # not a blanket tightening for non-China symbols).
    us_full = pd.date_range("2026-10-16 09:35:00", "2026-10-16 16:00:00", freq="5min")
    cache.store_intraday_bars(
        provider, "SPY",
        pd.DataFrame({"Open": [100.0] * len(us_full), "High": [101.0] * len(us_full),
                      "Low": [99.0] * len(us_full), "Close": [100.5] * len(us_full),
                      "Volume": [10.0] * len(us_full)}, index=us_full),
        interval="5m", sync_time=datetime.datetime(2026, 10, 16, 16, 30),
    )
    assert cache.is_intraday_range_covered(
        provider, "SPY", interval="5m", start="2026-10-16", end="2026-10-16", now=now,
        expected_last_session=expected,
    ) is True


def test_l2_26_shared_core_stays_date_only_when_no_deadline_is_supplied():
    """The daily axis (and any caller without a deadline) keeps the pre-L2.26 answer.

    `_bar_layer_reaches_end` gained an OPTIONAL session-time input, not a second branch: with the
    defaults (`expected_session_deadline=None`) it compares dates only, exactly as the daily gate
    always did. This is the anti-drift guard for `L2.2`/`L2.7`'s "one decision function" rule.
    """
    import inspect

    import common.duckdb_cache as cache_mod

    day = datetime.date(2026, 10, 16)
    first_bar = pd.Timestamp("2026-10-16 09:35:00")
    assert cache_mod._bar_layer_reaches_end(first_bar, day, day, day) is True
    assert cache_mod._bar_layer_reaches_end(None, day, day, day) is False
    assert cache_mod._bar_layer_reaches_end(
        pd.Timestamp("2026-10-15 15:00:00"), day, day, day
    ) is False

    # One definition, and the session-time input is keyword-only-by-default (None == date only).
    module_src = inspect.getsource(cache_mod)
    assert module_src.count("def _bar_layer_reaches_end(") == 1
    signature = inspect.signature(cache_mod._bar_layer_reaches_end)
    assert signature.parameters["expected_session_deadline"].default is None
    assert signature.parameters["expected_session_max_ts"].default is None
    # ...and the deadline itself is owned by `market_clock`, never re-derived from a literal here.
    deadline_src = inspect.getsource(cache_mod._intraday_expected_session_deadline)
    assert "last_expected_bar_ts" in deadline_src
    for literal in ("time(15", "time(16", "timedelta(hours=15", "timedelta(hours=16"):
        assert literal not in module_src, f"session close hard-coded in duckdb_cache.py: {literal}"


# ---------------------------------------------------------------------------------------------
# L2.27 -- half-day / early close (早收) on the intraday freshness gate.
#
# L2.26 anchored the deadline to `market_clock.last_expected_bar_ts` = close - one bar. That
# was correct only if every session ends at the market's normal close, which is false three
# times a year in the US (13:00) and on HK eves (12:00). The consequence was NOT a missed
# re-fetch but the opposite: an early-close session that is fully and correctly synced
# (09:35..13:00) still fails a 15:55 deadline, so every run declares the cache stale and
# re-fetches upstream -- forever, since no amount of downloading can add 15:00 bars to a day
# that ended at 13:00.
#
# `L2.27` fixes it in the clock, not here: `session_close_time(market, date)` consults the
# static early-close table in `common/market_holidays.py`. `duckdb_cache.py` is unchanged, so
# these tests are also the proof that the fix flows through the existing public API only.
# ---------------------------------------------------------------------------------------------
US_EARLY_CLOSE_DAY = "2026-11-27"    # day after Thanksgiving 2026-11-26 -> 13:00 close
US_NORMAL_DAY = "2026-11-25"         # Wednesday, full 16:00 session
HK_HALF_DAY = "2026-12-24"           # Christmas Eve -> 12:00, no afternoon session


def _us_session_5m_frame(day: str, last_time: str) -> pd.DataFrame:
    """Synthetic US continuous-session 5m bars for one day, ending at `last_time`.

    US has no lunch break, so the shape is simply 09:35..`last_time` in 5-minute steps; the
    last bar is the session close (`13:00` on an early close, `16:00` normally). All synthetic
    and deterministic -- no provider, no network.
    """
    times = pd.date_range(f"{day} 09:35:00", f"{day} {last_time}:00", freq="5min")
    n = len(times)
    return pd.DataFrame(
        {"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
         "Close": [100.5] * n, "Volume": [10.0] * n},
        index=times,
    )


def _store_us_session(cache, symbol: str, day: str, last_time: str, provider="FuyaoDataProvider"):
    cache.store_intraday_bars(
        provider, symbol, _us_session_5m_frame(day, last_time), interval="5m",
        sync_time=datetime.datetime.fromisoformat(f"{day} {last_time}:30"),
    )
    return provider


def test_l2_27_us_early_close_session_reaching_1300_is_covered(caplog):
    """ACCEPTANCE: an early-close day whose bars reach 13:00 is COVERED.

    Under L2.26 this exact cache was reported stale (deadline 15:55) and re-fetched on every
    run, which is the defect L2.27 closes.
    """
    provider, symbol = "FuyaoDataProvider", "AAPL"
    now = pd.Timestamp(f"{US_EARLY_CLOSE_DAY} 16:30:00")

    cache = DuckDBCache(db_path=":memory:")
    _store_us_session(cache, symbol, US_EARLY_CLOSE_DAY, "13:00")

    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        covered = cache.is_intraday_range_covered(
            provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
            now=now, expected_last_session=datetime.date(2026, 11, 27),
        )

    assert covered is True, (
        "a 13:00 early-close session that reached its own close must be covered; a 15:55 "
        "deadline here means the early close is not modelled and the cache re-fetches forever"
    )
    assert not [r for r in caplog.records if r.name == "common.duckdb_cache"], (
        "a covered session must not emit a staleness warning"
    )

    # The pre-L2.2 `None` fast path is untouched on an early-close day as well.
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
        now=now,
    ) is True


def test_l2_27_us_early_close_session_stopping_short_is_not_covered(caplog):
    """ACCEPTANCE (no false positive): bars short of 13:00 are still NOT covered.

    The discriminating boundary is `close - one bar` = 12:55 for 5m bars, exactly as it is
    14:55 on a 15:00 day -- the early close shortens the session, it does not weaken the rule.
    """
    provider, symbol = "FuyaoDataProvider", "AAPL"
    now = pd.Timestamp(f"{US_EARLY_CLOSE_DAY} 16:30:00")
    expected = datetime.date(2026, 11, 27)

    short = DuckDBCache(db_path=":memory:")
    _store_us_session(short, symbol, US_EARLY_CLOSE_DAY, "13:00")
    _truncate_intraday_session(short, provider, symbol, "5m", US_EARLY_CLOSE_DAY, "12:50")
    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        assert short.is_intraday_range_covered(
            provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
            now=now, expected_last_session=expected,
        ) is False, "one bar short of a 13:00 early close must be uncovered"

    messages = [r.getMessage() for r in caplog.records if r.name == "common.duckdb_cache"]
    assert any(
        "2026-11-27T12:50:00" in m and "2026-11-27T12:55:00" in m for m in messages
    ), f"the warning must name the SHORTENED deadline (12:55), got: {messages}"

    at_deadline = DuckDBCache(db_path=":memory:")
    _store_us_session(at_deadline, symbol, US_EARLY_CLOSE_DAY, "13:00")
    _truncate_intraday_session(at_deadline, provider, symbol, "5m", US_EARLY_CLOSE_DAY, "12:55")
    assert at_deadline.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
        now=now, expected_last_session=expected,
    ) is True, "12:55 (close - one 5m bar) must satisfy the early-close deadline"


def test_l2_27_normal_day_is_unaffected_by_the_early_close_table():
    """ACCEPTANCE: a normal day keeps the 16:00 / 15:55 US deadline, both directions.

    Same bars, one date apart: the 13:00 stop is stale on Wednesday and sufficient on the
    early-close Friday. That pair is what distinguishes "date-aware close" from "the gate
    got globally looser".
    """
    provider, symbol = "FuyaoDataProvider", "AAPL"
    now = pd.Timestamp(f"{US_NORMAL_DAY} 16:30:00")
    expected = datetime.date(2026, 11, 25)

    truncated = DuckDBCache(db_path=":memory:")
    _store_us_session(truncated, symbol, US_NORMAL_DAY, "16:00")
    _truncate_intraday_session(truncated, provider, symbol, "5m", US_NORMAL_DAY, "13:00")
    assert truncated.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_NORMAL_DAY, end=US_NORMAL_DAY,
        now=now, expected_last_session=expected,
    ) is False, "a 13:00 stop on a NORMAL 16:00 day must stay uncovered"

    full = DuckDBCache(db_path=":memory:")
    _store_us_session(full, symbol, US_NORMAL_DAY, "16:00")
    assert full.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_NORMAL_DAY, end=US_NORMAL_DAY,
        now=now, expected_last_session=expected,
    ) is True, "a full 16:00 session must remain covered"


def test_l2_27_early_close_and_the_following_weekend():
    """Weekend interaction: a weekend `end` is satisfied by Friday's early close.

    `2026-11-28/29` are Sat/Sun (no session, and no early close of their own), so the request
    end must be tolerated by the weekend branch -- and Friday's shortened deadline still has
    to be met. The truncated twin proves the tolerance is not blanket.
    """
    provider, symbol = "FuyaoDataProvider", "AAPL"
    now = pd.Timestamp("2026-11-30 16:30:00")     # Monday, after the weekend
    expected = datetime.date(2026, 11, 27)

    cache = DuckDBCache(db_path=":memory:")
    _store_us_session(cache, symbol, US_EARLY_CLOSE_DAY, "13:00")
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end="2026-11-29",
        now=now, expected_last_session=expected,
    ) is True

    short = DuckDBCache(db_path=":memory:")
    _store_us_session(short, symbol, US_EARLY_CLOSE_DAY, "13:00")
    _truncate_intraday_session(short, provider, symbol, "5m", US_EARLY_CLOSE_DAY, "12:50")
    assert short.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end="2026-11-29",
        now=now, expected_last_session=expected,
    ) is False, "the weekend end must not excuse a Friday that stopped before its early close"


def test_l2_27_none_path_is_bit_identical_on_an_early_close_day():
    """The `expected_last_session=None` fast path must not even resolve a session time.

    It is the frozen pre-L2.2 contract (`count(*) > 0`), so an early-close day answers exactly
    as it did before L2.26/L2.27 -- even for a cache truncated to its first bar.
    """
    provider, symbol = "FuyaoDataProvider", "AAPL"
    now = pd.Timestamp(f"{US_EARLY_CLOSE_DAY} 16:30:00")

    cache = DuckDBCache(db_path=":memory:")
    _store_us_session(cache, symbol, US_EARLY_CLOSE_DAY, "13:00")
    _truncate_intraday_session(cache, provider, symbol, "5m", US_EARLY_CLOSE_DAY, "09:35")

    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
        now=now,
    ) is True, "legacy None path: one bar is still `count(*) > 0`"
    assert cache.is_intraday_range_covered(
        provider, symbol, interval="5m", start=US_EARLY_CLOSE_DAY, end=US_EARLY_CLOSE_DAY,
        now=now, expected_last_session=datetime.date(2026, 11, 27),
    ) is False


def test_l2_27_hk_half_day_deadline_is_1155():
    """HK half-days close at 12:00 (morning only), so the same 5m rule gives 11:55.

    A China symbol is checked alongside to pin that the shrink is per-market AND per-date: a
    15:00 A-share stop is fine for `600000.SH` and 4 hours short for an HK half-day.
    """
    provider = "FuyaoDataProvider"
    now = pd.Timestamp(f"{HK_HALF_DAY} 16:30:00")
    expected = datetime.date(2026, 12, 24)

    cache = DuckDBCache(db_path=":memory:")
    hk_times = pd.date_range(f"{HK_HALF_DAY} 09:35:00", f"{HK_HALF_DAY} 12:00:00", freq="5min")
    n = len(hk_times)
    cache.store_intraday_bars(
        provider, "00700.HK",
        pd.DataFrame({"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
                      "Close": [100.5] * n, "Volume": [10.0] * n}, index=hk_times),
        interval="5m", sync_time=datetime.datetime(2026, 12, 24, 12, 5),
    )
    assert cache.is_intraday_range_covered(
        provider, "00700.HK", interval="5m", start=HK_HALF_DAY, end=HK_HALF_DAY,
        now=now, expected_last_session=expected,
    ) is True, "an HK half-day session that ended at 12:00 must be covered"

    short = DuckDBCache(db_path=":memory:")
    short.store_intraday_bars(
        provider, "00700.HK",
        pd.DataFrame({"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
                      "Close": [100.5] * n, "Volume": [10.0] * n}, index=hk_times),
        interval="5m", sync_time=datetime.datetime(2026, 12, 24, 12, 5),
    )
    _truncate_intraday_session(short, provider, "00700.HK", "5m", HK_HALF_DAY, "11:50")
    assert short.is_intraday_range_covered(
        provider, "00700.HK", interval="5m", start=HK_HALF_DAY, end=HK_HALF_DAY,
        now=now, expected_last_session=expected,
    ) is False, "11:50 is one bar short of an HK half-day's 11:55 deadline"


def test_l2_27_deadline_geometry_still_comes_from_the_clock_only():
    """Anti-drift: the early close is a CLOCK fact, so the cache layer learns nothing new."""
    import inspect

    import common.duckdb_cache as cache_mod
    import common.market_clock as clock_mod

    deadline_src = inspect.getsource(cache_mod._intraday_expected_session_deadline)
    assert "last_expected_bar_ts" in deadline_src
    for literal in ("time(", "timedelta(minutes", "timedelta(hours"):
        assert literal not in deadline_src, (
            f"session geometry re-derived in duckdb_cache.py: {literal!r}"
        )

    # One date->close lookup only: the clock delegates to `market_holidays` instead of
    # keeping its own `{date: time}` map (two calendars would drift).
    clock_src = inspect.getsource(clock_mod)
    assert clock_src.count("def close_time_on(") == 1
    assert clock_src.count("def session_close_time(") == 1
    assert "return early_close_time_or_none(self.market, day)" in clock_src

    import common.market_holidays as holidays_mod

    holidays_src = inspect.getsource(holidays_mod)
    assert "(11, 27, 13, 0)" in holidays_src, "US early-close data must live in market_holidays"
    assert "(12, 24, 12, 0)" in holidays_src, "HK half-day data must live in market_holidays"

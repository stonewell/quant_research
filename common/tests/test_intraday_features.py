"""Unit tests for offline intraday features: Dual-track DuckDB cache and Synthetic 5m Generator.

Tests 1 and 2 from Wave 1 Track T (T5.1).
All tests run 100% offline using in-memory / temporary DuckDB instances and SyntheticDataProvider.
"""

import datetime
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


def test_baostock_provider_offline_mock():
    """Test 3 (T1.3): Verify BaoStockDataProvider offline mock, symbol normalization, field restrictions, and chunking."""
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

    provider = BaoStockDataProvider(adjustflag="2")
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

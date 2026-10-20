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


def test_normalize_thscode_and_baostock_index_fields():
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

    b_prov = BaoStockDataProvider(adjustflag="2")
    with patch.object(b_prov, "_get_bs", return_value=mock_bs):
        df_res = b_prov.fetch_ohlcv("000300.SH", "2020-01-01", "2020-06-01", interval="1d")
        assert not df_res.empty
        assert len(query_calls) == 1
        call = query_calls[0]
        assert call["code"] == "sh.000300"
        assert call["fields"] == "date,code,open,high,low,close,preclose,volume,amount,pctChg"
        assert call["adjustflag"] == "3"
        assert call["frequency"] == "d"


def test_marketdb_and_fuyao_index_gap_fallback_stitching():
    """Verify MarketDBDataProvider and FuyaoDataProvider index fallbacks and 5y/trailing stitching."""
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


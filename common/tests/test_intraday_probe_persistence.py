"""Tests for M3.10 requirement #4: intraday probe verdict persistence in DuckDBCache.

Covers:
- Schema idempotence and column verification on intraday_sync_metadata
- Pre-existing rows preserve default/null values
- record_intraday_probe writes and overwrites probe fields, leaves intraday_ohlcv_bars untouched
- Missing metadata row logs a warning without writing/fabricating timestamps or raising
- get_intraday_metadata returns unchanged 6-key dict unless include_probe=True is opted in
- Sync script probe observer error handling swallows persistence failure safely
"""

import logging
from unittest.mock import MagicMock
import pandas as pd
import pytest

from common.duckdb_cache import DuckDBCache
from scripts.sync_china_5m_and_indices import _persist_intraday_probe


def _make_sample_5m_df(start_date: str = "2026-10-15") -> pd.DataFrame:
    ts = pd.date_range(f"{start_date} 09:35:00", f"{start_date} 10:00:00", freq="5min")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 101.0,
            "Low": 99.0,
            "Close": 100.5,
            "Volume": 1000.0,
            "Amount": 100500.0,
        },
        index=ts,
    )


def test_schema_columns_and_idempotence(tmp_path):
    db_file = str(tmp_path / "test_cache.duckdb")
    cache = DuckDBCache(db_path=db_file)

    con = cache._get_connection(read_only=True)
    try:
        cols = {row[1]: row[2] for row in con.execute("PRAGMA table_info('intraday_sync_metadata')").fetchall()}
    finally:
        con.close()

    assert "probe_attempts" in cols
    assert "last_probe_result" in cols

    # Test idempotence of _ensure_column
    con_w = cache._get_connection(read_only=False)
    try:
        DuckDBCache._ensure_column(con_w, "intraday_sync_metadata", "probe_attempts", "INTEGER DEFAULT 0")
        DuckDBCache._ensure_column(con_w, "intraday_sync_metadata", "last_probe_result", "VARCHAR")
    finally:
        con_w.close()


def test_pre_existing_row_survives(tmp_path):
    db_file = str(tmp_path / "test_cache.duckdb")
    cache = DuckDBCache(db_path=db_file)
    provider, symbol = "BaoStockDataProvider", "600519.SH"
    df = _make_sample_5m_df("2026-10-15")
    cache.store_intraday_bars(provider, symbol, df, interval="5m", adjustflag="raw")

    con = cache._get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT probe_attempts, last_probe_result FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, symbol, "5m"],
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    assert row[0] == 0  # DEFAULT 0 from migration
    assert row[1] is None  # NULL


def test_record_intraday_probe_writes_and_overwrites(tmp_path):
    db_file = str(tmp_path / "test_cache.duckdb")
    cache = DuckDBCache(db_path=db_file)
    provider, symbol = "BaoStockDataProvider", "600519.SH"
    df = _make_sample_5m_df("2026-10-15")
    cache.store_intraday_bars(provider, symbol, df, interval="5m", adjustflag="raw")

    # Count initial bars
    con = cache._get_connection(read_only=True)
    try:
        bar_count_before = con.execute(
            "SELECT count(*) FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, symbol, "5m"],
        ).fetchone()[0]
    finally:
        con.close()
    assert bar_count_before == len(df)

    # First probe write
    cache.record_intraday_probe(provider, symbol, attempts=3, last_result="exhausted", interval="5m")

    meta = cache.get_intraday_metadata(provider, symbol, interval="5m", include_probe=True)
    assert meta is not None
    assert meta["probe_attempts"] == 3
    assert meta["last_probe_result"] == "exhausted"

    # Second probe write (overwrite)
    cache.record_intraday_probe(provider, symbol, attempts=4, last_result="ok", interval="5m")

    meta2 = cache.get_intraday_metadata(provider, symbol, interval="5m", include_probe=True)
    assert meta2 is not None
    assert meta2["probe_attempts"] == 4
    assert meta2["last_probe_result"] == "ok"

    # Ensure intraday_ohlcv_bars is untouched
    con = cache._get_connection(read_only=True)
    try:
        bar_count_after = con.execute(
            "SELECT count(*) FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, symbol, "5m"],
        ).fetchone()[0]
    finally:
        con.close()
    assert bar_count_after == bar_count_before


def test_record_intraday_probe_missing_row_warns_no_write(tmp_path, caplog):
    db_file = str(tmp_path / "test_cache.duckdb")
    cache = DuckDBCache(db_path=db_file)
    provider, symbol = "BaoStockDataProvider", "000001.SZ"

    with caplog.at_level(logging.WARNING):
        cache.record_intraday_probe(provider, symbol, attempts=2, last_result="empty", interval="5m")

    assert any("no existing intraday_sync_metadata row" in record.message for record in caplog.records)

    # Confirm no row was created
    con = cache._get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT count(*) FROM intraday_sync_metadata WHERE provider = ? AND symbol = ?",
            [provider, symbol],
        ).fetchone()[0]
    finally:
        con.close()
    assert row == 0


def test_get_intraday_metadata_backward_compat(tmp_path):
    db_file = str(tmp_path / "test_cache.duckdb")
    cache = DuckDBCache(db_path=db_file)
    provider, symbol = "BaoStockDataProvider", "600519.SH"
    df = _make_sample_5m_df("2026-10-15")
    cache.store_intraday_bars(provider, symbol, df, interval="5m", adjustflag="raw")
    cache.record_intraday_probe(provider, symbol, attempts=2, last_result="ok", interval="5m")

    # Default call (include_probe=False)
    default_meta = cache.get_intraday_metadata(provider, symbol, interval="5m")
    assert default_meta is not None
    assert set(default_meta.keys()) == {
        "earliest_available_ts",
        "is_earliest_known",
        "latest_available_ts",
        "latest_close_ref",
        "last_sync_time",
        "adjustflag",
    }
    assert "probe_attempts" not in default_meta
    assert "last_probe_result" not in default_meta

    # Explicit call (include_probe=True)
    extended_meta = cache.get_intraday_metadata(provider, symbol, interval="5m", include_probe=True)
    assert extended_meta is not None
    assert "probe_attempts" in extended_meta
    assert "last_probe_result" in extended_meta
    assert extended_meta["probe_attempts"] == 2
    assert extended_meta["last_probe_result"] == "ok"


def test_sync_script_callback_swallows_exception(caplog):
    cache_mock = MagicMock()
    cache_mock.record_intraday_probe.side_effect = RuntimeError("disk full")

    with caplog.at_level(logging.WARNING):
        # Must not raise
        _persist_intraday_probe(cache_mock, "600519.SH", {"attempts": 3, "last_result": "ok"})

    assert any("Failed to persist intraday probe verdict for 600519.SH" in record.message for record in caplog.records)

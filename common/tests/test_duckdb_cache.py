"""Unit tests for common/duckdb_cache.py (DuckDB OHLCV Cache System).

All tests run 100% offline using in-memory or temporary DuckDB instances.
"""

import datetime
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import (
    DuckDBCache,
    is_synthetic_provider,
    resample_ohlcv,
)


@pytest.fixture
def mem_cache():
    """In-memory DuckDBCache instance for fast offline testing."""
    return DuckDBCache(db_path=":memory:")


def _make_sample_ohlcv(start: str, periods: int = 10, base_price: float = 100.0) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=periods)
    data = {
        "Open": [base_price + i for i in range(periods)],
        "High": [base_price + i + 2.0 for i in range(periods)],
        "Low": [base_price + i - 1.0 for i in range(periods)],
        "Close": [base_price + i + 1.0 for i in range(periods)],
        "Volume": [1000.0 * (i + 1) for i in range(periods)],
    }
    return pd.DataFrame(data, index=dates)


def test_schema_and_primary_keys(mem_cache):
    """Verifies tables exist and primary keys are strictly (provider, symbol, date) and (provider, symbol)."""
    con = mem_cache._get_connection(read_only=True)
    # Check table existence
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    assert "ohlcv_bars" in tables
    assert "asset_sync_metadata" in tables

    # Query table info
    bars_info = con.execute("PRAGMA table_info('ohlcv_bars')").fetchall()
    bars_cols = {row[1]: {"type": row[2], "pk": row[5]} for row in bars_info}
    assert "interval" not in bars_cols
    assert bars_cols["provider"]["pk"] > 0
    assert bars_cols["symbol"]["pk"] > 0
    assert bars_cols["date"]["pk"] > 0

    meta_info = con.execute("PRAGMA table_info('asset_sync_metadata')").fetchall()
    meta_cols = {row[1]: {"type": row[2], "pk": row[5]} for row in meta_info}
    assert "interval" not in meta_cols
    assert meta_cols["provider"]["pk"] > 0
    assert meta_cols["symbol"]["pk"] > 0


def test_store_and_query_exact_and_sub_range(mem_cache):
    """Verifies storing bars and querying exact range and arbitrary sub-ranges."""
    df = _make_sample_ohlcv("2020-01-01", periods=10)
    mem_cache.store_bars("TestProvider", "AAPL", df)

    # Exact range query
    retrieved = mem_cache.query_bars("TestProvider", "AAPL", start="2020-01-01", end="2020-01-14")
    assert len(retrieved) == 10
    assert list(retrieved.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert retrieved.index[0] == pd.Timestamp("2020-01-01")

    # Sub-range query (middle 5 days)
    sub = mem_cache.query_bars("TestProvider", "AAPL", start="2020-01-03", end="2020-01-09")
    assert len(sub) == 5
    assert sub.index[0] == pd.Timestamp("2020-01-03")
    assert sub.index[-1] == pd.Timestamp("2020-01-09")


def test_multi_provider_isolation(mem_cache):
    """Verifies that different providers storing the same symbol never overwrite each other."""
    df_yf = _make_sample_ohlcv("2020-01-01", periods=5, base_price=100.0)
    df_fuyao = _make_sample_ohlcv("2020-01-01", periods=5, base_price=200.0)

    mem_cache.store_bars("YFinanceProvider", "SPY", df_yf)
    mem_cache.store_bars("FuyaoProvider", "SPY", df_fuyao)

    q_yf = mem_cache.query_bars("YFinanceProvider", "SPY")
    q_fuyao = mem_cache.query_bars("FuyaoProvider", "SPY")

    assert len(q_yf) == 5
    assert len(q_fuyao) == 5
    assert q_yf["Close"].iloc[0] == 101.0
    assert q_fuyao["Close"].iloc[0] == 201.0


def test_earliest_available_date_avoids_requery(mem_cache):
    """Verifies tracking of provider's earliest date: when an asset starts at T_min and
    the user queried earlier than T_min - 7 days, is_earliest_known is set to True,
    and subsequent queries with start <= T_min are treated as fully covered without re-query.
    """
    # Asset only has data starting from 2015-06-01
    df = _make_sample_ohlcv("2015-06-01", periods=10)
    # Requested start was 2000-01-01 (far earlier than 2015-06-01)
    mem_cache.store_bars("YFinanceProvider", "NEW_IPO", df, requested_start="2000-01-01")

    meta = mem_cache.get_asset_metadata("YFinanceProvider", "NEW_IPO")
    assert meta is not None
    assert meta["is_earliest_known"] is True
    assert meta["earliest_available_date"] == datetime.date(2015, 6, 1)

    # Now check if a query asking for 1990-01-01 through 2015-06-12 is covered
    covered = mem_cache.is_range_covered(
        "YFinanceProvider", "NEW_IPO", start="1990-01-01", end="2015-06-12"
    )
    assert covered is True  # Fully covered! Avoids re-querying data provider!


def test_synthetic_data_skips_cache(mem_cache):
    """Verifies synthetic Brownian motion data always bypasses the DuckDB cache."""
    assert is_synthetic_provider("SyntheticDataProvider") is True
    assert is_synthetic_provider("synthetic") is True
    assert is_synthetic_provider("YFinanceDataProvider") is False

    df = _make_sample_ohlcv("2020-01-01", periods=5)
    # Storing synthetic data is a no-op
    mem_cache.store_bars("SyntheticDataProvider", "SPY", df)

    meta = mem_cache.get_asset_metadata("SyntheticDataProvider", "SPY")
    assert meta is None
    assert mem_cache.is_range_covered("SyntheticDataProvider", "SPY", "2020-01-01", "2020-01-05") is False

    bars = mem_cache.query_bars("SyntheticDataProvider", "SPY")
    assert bars.empty


def test_resample_ohlcv_any_interval(mem_cache):
    """Verifies that daily bars stored in DuckDB can serve weekly (1wk) and monthly (1mo) intervals."""
    # 20 business days (~4 full weeks)
    df = _make_sample_ohlcv("2020-01-01", periods=20, base_price=100.0)
    mem_cache.store_bars("TestProvider", "QQQ", df)

    # 1. Daily query
    daily = mem_cache.query_bars("TestProvider", "QQQ", interval="1d")
    assert len(daily) == 20

    # 2. Weekly query
    weekly = mem_cache.query_bars("TestProvider", "QQQ", interval="1wk")
    assert 4 <= len(weekly) <= 5
    # Weekly high should be the maximum of that week's daily highs
    assert weekly["High"].max() == daily["High"].max()
    # Weekly volume should be the sum
    assert pytest.approx(weekly["Volume"].sum()) == daily["Volume"].sum()

    # 3. Monthly query
    monthly = mem_cache.query_bars("TestProvider", "QQQ", interval="1mo")
    assert len(monthly) >= 1
    assert pytest.approx(monthly["Volume"].sum()) == daily["Volume"].sum()


def test_batch_query_universe(mem_cache):
    """Verifies high-performance batch query for multiple symbols."""
    df_a = _make_sample_ohlcv("2020-01-01", periods=5, base_price=50.0)
    df_b = _make_sample_ohlcv("2020-01-01", periods=5, base_price=150.0)

    mem_cache.store_bars("BatchProvider", "SYM_A", df_a)
    mem_cache.store_bars("BatchProvider", "SYM_B", df_b)

    uni = mem_cache.query_universe("BatchProvider", ["SYM_A", "SYM_B", "NON_EXISTENT"])
    assert "SYM_A" in uni
    assert "SYM_B" in uni
    assert "NON_EXISTENT" not in uni
    assert len(uni["SYM_A"]) == 5
    assert len(uni["SYM_B"]) == 5
    assert uni["SYM_A"]["Close"].iloc[0] == 51.0
    assert uni["SYM_B"]["Close"].iloc[0] == 151.0


def test_file_based_cache_persistence(tmp_path):
    """Verifies that DuckDBCache works properly on disk and persists data across instances."""
    db_file = tmp_path / "test_cache.duckdb"
    cache1 = DuckDBCache(db_path=str(db_file))
    df = _make_sample_ohlcv("2020-01-01", periods=5)
    cache1.store_bars("DiskProvider", "MSFT", df)

    # Re-open with a new instance
    cache2 = DuckDBCache(db_path=str(db_file))
    retrieved = cache2.query_bars("DiskProvider", "MSFT")
    assert len(retrieved) == 5
    assert retrieved["Close"].iloc[0] == 101.0

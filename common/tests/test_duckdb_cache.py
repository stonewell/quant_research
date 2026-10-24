"""Unit tests for common/duckdb_cache.py (DuckDB OHLCV Cache System).

All tests run 100% offline using in-memory or temporary DuckDB instances.
"""

import datetime
import logging
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
    ProductionCachePathError,
    is_synthetic_provider,
    production_cache_guard_active,
    production_cache_roots,
    refuse_production_cache_path,
    resample_ohlcv,
    is_intraday_interval,
    _get_resample_rule,
)
from common.freshness import StalePriceError, latest_close
import common.duckdb_cache as duckdb_cache_module


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


def test_is_range_covered_checks_last_sync_time_for_latest_data(mem_cache):
    """Verifies that DuckDBCache.is_range_covered checks last_sync_time when
    end is omitted (end=None) or today, so next-day queries refresh data.

    The "already synced today" short-circuit is only a *metadata* fast path: since the
    2026-10-22 incident it can no longer claim coverage on its own -- a bar at the requested
    end date must also exist (see the dedicated regression tests below). Both halves of that
    contract are asserted here.
    """
    df = _make_sample_ohlcv("2024-01-01", periods=2)  # 2024-01-01 and 2024-01-02
    yesterday_sync = datetime.datetime(2024, 1, 2, 16, 0)
    today_time = pd.Timestamp("2024-01-03 10:00:00")

    mem_cache.store_bars("TestProvider", "AAPL", df, sync_time=yesterday_sync)

    # 1. On today (2024-01-03), last sync was yesterday (2024-01-02), so cache is NOT covered
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end=None, now=today_time) is False
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end="2024-01-03", now=today_time) is False

    # 2. Synced today (2024-01-03) is still NOT coverage by itself: the bars only reach
    #    2024-01-02, i.e. the sync ran before upstream published today's bar -> must re-fetch.
    #    (This is exactly the 2026-10-22 incident: fresh metadata, bars one day short.)
    today_sync = datetime.datetime(2024, 1, 3, 15, 30)
    mem_cache.store_bars("TestProvider", "AAPL", df, sync_time=today_sync)
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end=None, now=today_time) is False
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end="2024-01-03", now=today_time) is False

    # 3. Once a bar for 2024-01-03 actually lands, the same-day fast path applies again
    df_with_today = _make_sample_ohlcv("2024-01-01", periods=3)  # 2024-01-01 .. 2024-01-03
    mem_cache.store_bars("TestProvider", "AAPL", df_with_today, sync_time=today_sync)
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end=None, now=today_time) is True
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end="2024-01-03", now=today_time) is True


def _force_sync_metadata(cache, provider, symbol, latest_available_date, sync_time):
    """Forces `asset_sync_metadata` to claim a latest date that the stored bars do NOT reach.

    Reproduces the 2026-10-22 incident state: the sync bookkeeping says "up to today, synced
    today" while `ohlcv_bars` stops on the previous session (the fetch happened before the
    upstream provider published the new bar). `store_bars` itself derives latest_available_date
    from the bar max, so this inconsistent state has to be injected directly.
    """
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "UPDATE asset_sync_metadata SET latest_available_date = ?, last_sync_time = ? "
            "WHERE provider = ? AND symbol = ?",
            [latest_available_date, sync_time, provider, symbol],
        )
    finally:
        if not cache.is_memory:
            con.close()


def test_is_range_covered_false_when_metadata_claims_end_but_bars_stop_one_day_short(mem_cache):
    """2026-10-22 REGRESSION: `latest_available_date == req_end` and `last_sync == today` used
    to be accepted as coverage even though the bars stopped a day short, so the live path was
    never re-fetched and marked five positions to the 2026-10-21 close (NAV understated
    ~0.70%, limit prices below the true market). The bar layer must reject it.
    """
    df = _make_sample_ohlcv("2026-10-19", periods=3)  # 10-19, 10-20, 10-21 (no 10-22 bar)
    mem_cache.store_bars("IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _force_sync_metadata(
        mem_cache, "IncidentProvider", "SPY",
        latest_available_date=datetime.date(2026, 10, 22),
        sync_time=datetime.datetime(2026, 10, 22, 10, 0),
    )

    now = pd.Timestamp("2026-10-22 10:35:00")
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-22", now=now) is False
    # end omitted (= today) must be just as strict -- no stale-mark hole for that call shape
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=None, now=now) is False

    # Querying up to the last stored bar is genuinely covered (nothing more was asked for)
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-21", now=now) is True


def test_is_range_covered_true_when_bars_reach_requested_end(mem_cache):
    """The same metadata state is covered once the bar for `req_end` actually exists."""
    df = _make_sample_ohlcv("2026-10-19", periods=4)  # includes the 2026-10-22 bar
    mem_cache.store_bars("IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    now = pd.Timestamp("2026-10-22 16:05:00")
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-22", now=now) is True
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=None, now=now) is True


def test_is_range_covered_false_when_no_bars_stored(mem_cache):
    """Fresh metadata with an empty `ohlcv_bars` table must never report coverage."""
    df = _make_sample_ohlcv("2026-10-19", periods=4)
    mem_cache.store_bars("IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    con = mem_cache._get_connection(read_only=False)
    try:
        con.execute("DELETE FROM ohlcv_bars WHERE provider = ? AND symbol = ?", ["IncidentProvider", "SPY"])
    finally:
        if not mem_cache.is_memory:
            con.close()

    now = pd.Timestamp("2026-10-22 16:05:00")
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-22", now=now) is False


def test_is_range_covered_true_on_weekend_when_bars_reach_friday(mem_cache):
    """Weekend guard against an infinite re-fetch loop: `end` = Saturday 2026-10-24 with bars
    through Friday 2026-10-23 must count as covered (no exchange session on the Saturday).
    """
    df = _make_sample_ohlcv("2026-10-19", periods=5)  # Mon 10-19 .. Fri 10-23
    mem_cache.store_bars("IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 24, 9, 0))

    saturday = pd.Timestamp("2026-10-24 09:00:00")
    assert saturday.weekday() == 5
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-24", now=saturday) is True
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=None, now=saturday) is True

    # ...but a weekday request that is short is still uncovered, even inside the 7-day tolerance
    short_df = _make_sample_ohlcv("2026-10-19", periods=4)  # Mon 10-19 .. Thu 10-22
    mem_cache.store_bars("IncidentProvider", "SHORT", short_df, sync_time=datetime.datetime(2026, 10, 24, 9, 0))
    assert mem_cache.is_range_covered("IncidentProvider", "SHORT", start="2026-10-19", end="2026-10-23", now=saturday) is False


def test_is_range_covered_is_per_symbol_not_per_provider(mem_cache):
    """One short symbol must not contaminate, nor be masked by, a complete sibling symbol
    under the same provider (the incident hit 6 of 20 symbols in one universe)."""
    short_df = _make_sample_ohlcv("2026-10-19", periods=3)   # stops 2026-10-21
    full_df = _make_sample_ohlcv("2026-10-19", periods=4)    # reaches 2026-10-22
    now = pd.Timestamp("2026-10-22 16:00:00")

    mem_cache.store_bars("IncidentProvider", "SHORT", short_df, sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _force_sync_metadata(
        mem_cache, "IncidentProvider", "SHORT",
        latest_available_date=datetime.date(2026, 10, 22),
        sync_time=datetime.datetime(2026, 10, 22, 10, 0),
    )
    mem_cache.store_bars("IncidentProvider", "FULL", full_df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    assert mem_cache.is_range_covered("IncidentProvider", "SHORT", start="2026-10-19", end="2026-10-22", now=now) is False
    assert mem_cache.is_range_covered("IncidentProvider", "FULL", start="2026-10-19", end="2026-10-22", now=now) is True


def test_is_range_covered_future_end_not_judged_by_bar_layer(mem_cache):
    """A requested `end` beyond today can never have a stored bar, so the bar layer must not
    reject it (otherwise every "give me everything up to a far-future date" call re-fetches
    forever). Only the metadata freshness rules apply there.
    """
    now = pd.Timestamp("2026-10-22 16:05:00")
    df = _make_sample_ohlcv("2026-10-19", periods=4)
    mem_cache.store_bars("IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    far_future_end = (now + pd.Timedelta(days=3650)).strftime("%Y-%m-%d")
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=far_future_end, now=now) is True


# ---------------------------------------------------------------------------------------------
# 2026-10-22 REGRESSION: `asset_sync_metadata` upsert monotonicity.
#
# A real Fuyao sync that started fetching at 2024-01-02 rewrote `earliest_available_date` to
# 2024-01-02 for six symbols whose `ohlcv_bars` still begin in 2017-01-18 / 2020-11-30 /
# 2021-08-20, and cleared `is_earliest_known` on 601728.SH (True -> False). The recorded metadata
# then contradicted the bars it describes, so any logic deriving a backtestable range from
# `earliest_available_date` silently truncated ~7 years of history. Each sync from a later start
# date damaged the field further. The upsert must therefore be directional:
#   earliest -> MIN, latest -> MAX, is_earliest_known -> sticky True.
# ---------------------------------------------------------------------------------------------


def _make_daily_ohlcv(start: str, end: str, base_price: float = 100.0) -> pd.DataFrame:
    """Business-day OHLCV frame covering [start, end] inclusive (synthetic, offline)."""
    dates = pd.bdate_range(start, end)
    n = len(dates)
    return pd.DataFrame(
        {
            "Open": [base_price] * n,
            "High": [base_price + 2.0] * n,
            "Low": [base_price - 1.0] * n,
            "Close": [base_price + 1.0] * n,
            "Volume": [1000.0] * n,
        },
        index=dates,
    )


def test_store_bars_later_window_does_not_downgrade_earliest_available_date(mem_cache):
    """2026-10-22 REGRESSION (six symbols): bars from 2017, then a sync window starting
    2024-01-02 must leave `earliest_available_date` reporting the 2017 date and must leave
    `is_earliest_known` unchanged. A sync that never looked before 2024 has learned nothing
    about 2017 and must not overwrite that knowledge.
    """
    provider, symbol = "FuyaoDataProvider", "600362.SH"
    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))
    before = mem_cache.get_asset_metadata(provider, symbol)
    assert before["earliest_available_date"] == datetime.date(2017, 1, 18)
    assert before["is_earliest_known"] is False

    # The incident: a sync that starts at 2024-01-02 (requested_start == first returned bar)
    mem_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-02-29"), requested_start="2024-01-02"
    )

    after = mem_cache.get_asset_metadata(provider, symbol)
    assert after["earliest_available_date"] == datetime.date(2017, 1, 18), (
        "earliest_available_date was downgraded to 2024-01-02 while ohlcv_bars still holds 2017 rows"
    )
    assert after["is_earliest_known"] is False
    # The 2017 bars themselves were never touched (metadata must describe the data, not replace it)
    assert mem_cache.query_bars(provider, symbol)["Close"].index.min() == pd.Timestamp("2017-01-18")


def test_store_bars_is_earliest_known_is_sticky_true(mem_cache):
    """2026-10-22 REGRESSION (601728.SH): once `is_earliest_known` is True it must stay True even
    when a later sync writes a row that says False (only a human may reset it explicitly)."""
    provider, symbol = "FuyaoDataProvider", "601728.SH"
    mem_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2000-01-01"
    )
    assert mem_cache.get_asset_metadata(provider, symbol)["is_earliest_known"] is True

    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-29"), requested_start="2024-02-01")
    assert mem_cache.get_asset_metadata(provider, symbol)["is_earliest_known"] is True


def test_store_bars_stale_latest_window_does_not_roll_back_latest_available_date(mem_cache):
    """A stale/regressed fetch (ending 2026-10-21 after 2026-10-22 was already recorded) must not
    move `latest_available_date` backwards -- MAX(existing, incoming), never greatest-latest-wins."""
    provider, symbol = "FuyaoDataProvider", "600900.SH"
    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2026-10-01", "2026-10-22"))
    assert mem_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 22)

    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2026-10-01", "2026-10-21"))
    assert mem_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 22)


def test_store_bars_earliest_can_still_improve_when_sync_reaches_further_back(mem_cache):
    """MIN semantics must not freeze the field: a sync that genuinely reaches back to 2017 still
    moves an existing 2024 earliest date earlier."""
    provider, symbol = "FuyaoDataProvider", "600584.SH"
    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"))
    assert mem_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2024, 1, 2)

    mem_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-01-31"))
    assert mem_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2017, 1, 18)


def test_store_bars_first_insert_records_incoming_values_verbatim(mem_cache):
    """With no existing row, the incoming window is recorded as-is (no MIN/MAX partner to compare
    against), including the `requested_start` earliest-known inference."""
    provider, symbol = "FuyaoDataProvider", "600660.SH"
    mem_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2021-08-20", "2021-09-30"), requested_start="2021-08-20"
    )

    meta = mem_cache.get_asset_metadata(provider, symbol)
    assert meta["earliest_available_date"] == datetime.date(2021, 8, 20)
    assert meta["latest_available_date"] == datetime.date(2021, 9, 30)
    assert meta["is_earliest_known"] is False


def test_store_bars_metadata_upsert_is_idempotent(mem_cache):
    """Storing the same frame twice must not change any of the three monotone fields (only the
    freely-overwritable `last_sync_time` may move)."""
    provider, symbol = "FuyaoDataProvider", "601872.SH"
    df = _make_daily_ohlcv("2020-11-30", "2021-02-26")
    mem_cache.store_bars(provider, symbol, df, requested_start="2020-11-30", sync_time=datetime.datetime(2026, 10, 22, 19, 0))
    first = mem_cache.get_asset_metadata(provider, symbol)

    mem_cache.store_bars(provider, symbol, df, requested_start="2020-11-30", sync_time=datetime.datetime(2026, 10, 22, 21, 34, 31))
    second = mem_cache.get_asset_metadata(provider, symbol)

    assert second["earliest_available_date"] == first["earliest_available_date"]
    assert second["latest_available_date"] == first["latest_available_date"]
    assert second["is_earliest_known"] == first["is_earliest_known"]
    assert first["last_sync_time"] != second["last_sync_time"]  # last attempt, not a data fact
    # Bars were replaced, not duplicated (primary key keeps them unique)
    assert len(mem_cache.query_bars(provider, symbol)) == len(df)


def test_intraday_interval_resampling_rules():
    """Verifies is_intraday_interval and _get_resample_rule mapping for intraday,
    daily, weekly, and monthly frequencies.
    """
    intraday_intervals = ["1m", "5m", "15m", "30m", "60m", "1h"]
    for interval in intraday_intervals:
        assert is_intraday_interval(interval) is True, f"{interval} should be recognized as intraday"
        # Never upsample daily bars to intraday intervals
        assert _get_resample_rule(interval) is None, f"{interval} should not have a resample rule"

    # Daily frequencies
    for daily_inv in ["1d", "d", "daily"]:
        assert is_intraday_interval(daily_inv) is False
        assert _get_resample_rule(daily_inv) is None

    # Weekly frequencies
    for weekly_inv in ["1wk", "w", "1w", "weekly"]:
        assert is_intraday_interval(weekly_inv) is False
        assert _get_resample_rule(weekly_inv) == "W-FRI"

    # Monthly frequencies
    monthly_intervals = ["1mo", "mo", "m", "monthly"]
    for monthly_inv in monthly_intervals:
        assert is_intraday_interval(monthly_inv) is False
        assert _get_resample_rule(monthly_inv) == "ME"

    # Edge cases
    assert is_intraday_interval("") is False
    assert is_intraday_interval(None) is False
    assert _get_resample_rule("") is None
    assert _get_resample_rule(None) is None

    # Verify resample_ohlcv with intraday does not resample to month-end or raise
    df = _make_sample_ohlcv("2024-01-01", periods=5)
    for interval in ["1m", "5m", "15m"]:
        res = resample_ohlcv(df, interval)
        assert len(res) == len(df), f"resample_ohlcv({interval}) should leave daily DataFrame untouched"


def test_fundamentals_table_schema_and_crud(mem_cache):
    """Verifies stock_fundamentals_annual and stock_industry_metadata tables, storage and query."""
    from common.data import FundamentalMetrics

    con = mem_cache._get_connection(read_only=True)
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    assert "stock_fundamentals_annual" in tables
    assert "stock_industry_metadata" in tables

    metrics = FundamentalMetrics(
        symbol="600519.SH",
        report_year=2023,
        stat_date="2023-12-31",
        roe=0.35,
        net_profit=74734000000.0,
        net_profit_5yr_cagr=0.18,
        cfo=66592000000.0,
        cfo_to_net_profit=0.89,
        total_assets=272710000000.0,
        total_liabilities=50000000000.0,
        debt_to_assets=0.183,
        debt_to_equity=0.224,
        interest_bearing_debt=0.0,
        dividend_yield=0.025,
        dividend_payout_ratio=0.519,
        pe_ttm=28.5,
        pb=8.2,
        total_shares=1256000000.0,
        market_cap=2100000000000.0,
        industry_name="食品饮料",
        industry_tier="tier2_consumer",
        terminal_pe=20.0,
        is_blacklisted=False,
        blacklist_reason=None,
    )

    mem_cache.store_fundamentals("BaoStockDataProvider", metrics)

    # Query latest (report_year=None)
    queried = mem_cache.query_fundamentals("BaoStockDataProvider", "600519.SH")
    assert queried is not None
    assert queried.symbol == "600519.SH"
    assert queried.report_year == 2023
    assert queried.stat_date == "2023-12-31"
    assert abs(queried.roe - 0.35) < 1e-4
    assert abs(queried.cfo_to_net_profit - 0.89) < 1e-4
    assert queried.industry_name == "食品饮料"
    assert queried.industry_tier == "tier2_consumer"
    assert abs(queried.terminal_pe - 20.0) < 1e-4
    assert queried.is_blacklisted is False

    # Query specific report_year
    queried_2023 = mem_cache.query_fundamentals("BaoStockDataProvider", "600519.SH", report_year=2023)
    assert queried_2023 is not None
    assert queried_2023.report_year == 2023

    # Query non-existent year
    queried_2020 = mem_cache.query_fundamentals("BaoStockDataProvider", "600519.SH", report_year=2020)
    assert queried_2020 is None

    # Query industry metadata
    meta = mem_cache.query_industry_metadata("BaoStockDataProvider", "600519.SH")
    assert meta is not None
    assert meta["industry_name"] == "食品饮料"
    assert meta["industry_tier"] == "tier2_consumer"
    assert meta["terminal_pe"] == 20.0
    assert meta["is_blacklisted"] is False



# ---------------------------------------------------------------------------
# pytest-only production-cache guard (L2.13)
# ---------------------------------------------------------------------------
# These tests NEVER touch the real `<repo>/data/cache.duckdb`: `_find_repo_root`
# is monkeypatched onto `tmp_path`, so "inside <repo>/data" is simulated by a
# throwaway file. The guard is asserted in both directions (refuse writable,
# allow read-only) plus the operator escape hatch and the non-pytest baseline.

_ALLOW_ENV = "LIVETRADING_ALLOW_PRODUCTION_PATHS"
_MARKER_ENV = "PYTEST_CURRENT_TEST"


def _fake_repo_cache_file(tmp_path, monkeypatch):
    """Create a fake repo root under `tmp_path` and return `<root>/data/cache.duckdb`."""
    repo_root = tmp_path / "fake_repo"
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(duckdb_cache_module, "_find_repo_root", lambda: str(repo_root))
    monkeypatch.delenv(_ALLOW_ENV, raising=False)
    monkeypatch.setenv(_MARKER_ENV, "test_duckdb_cache.py::guard (call)")
    return repo_root / "data" / "cache.duckdb"


def test_production_cache_roots_cover_this_repo_data_dir():
    """The net's default root is this checkout's `<repo>/data` (never monkeypatched here)."""
    assert os.path.join(_REPO_ROOT, "data") in production_cache_roots()
    assert production_cache_guard_active() is True  # pytest sets PYTEST_CURRENT_TEST
    # Pure decision, no filesystem I/O: the REAL production file would be refused.
    with pytest.raises(ProductionCachePathError):
        refuse_production_cache_path(os.path.join(_REPO_ROOT, "data", "cache.duckdb"))


def test_pytest_refuses_writable_open_of_production_cache(tmp_path, monkeypatch):
    """Writable opens under `<repo>/data` must raise, for file AND directory spellings."""
    db_file = _fake_repo_cache_file(tmp_path, monkeypatch)

    with pytest.raises(ProductionCachePathError, match="Refusing to open the DuckDB cache read-write"):
        DuckDBCache(db_path=str(db_file))
    # Refused BEFORE touching disk: no database file was created.
    assert not db_file.exists()

    # A directory argument resolves to `<dir>/cache.duckdb` -- still inside `<repo>/data`.
    with pytest.raises(ProductionCachePathError, match="production market cache directory"):
        DuckDBCache(db_path=str(db_file.parent))
    assert not db_file.exists()

    # `data/../data` and a symlink alias cannot bypass the containment check.
    aliased = db_file.parent / ".." / "data" / "cache.duckdb"
    with pytest.raises(ProductionCachePathError):
        DuckDBCache(db_path=str(aliased))
    link = tmp_path / "data_link"
    if not link.exists():
        try:
            os.symlink(db_file.parent, link)
        except (OSError, NotImplementedError):
            link = None
    if link is not None:
        with pytest.raises(ProductionCachePathError):
            DuckDBCache(db_path=str(link / "cache.duckdb"))
    assert not db_file.exists()


def test_read_only_open_of_production_cache_is_allowed(tmp_path, monkeypatch):
    """Read-only inspection of a production path keeps working (and cannot write)."""
    db_file = _fake_repo_cache_file(tmp_path, monkeypatch)

    # Seed the temp file through the escape hatch -- this is a throwaway path under
    # tmp_path, never the real cache. Writability is then revoked for the assertion.
    monkeypatch.setenv(_ALLOW_ENV, "1")
    seed = DuckDBCache(db_path=str(db_file))
    seed.store_bars("CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=5))
    monkeypatch.delenv(_ALLOW_ENV, raising=False)

    ro = DuckDBCache(db_path=str(db_file), read_only=True)
    assert ro.read_only is True
    assert ro.db_path == str(db_file)
    assert len(ro.query_bars("CacheGuardProvider", "AAPL")) == 5
    assert ro.is_range_covered("CacheGuardProvider", "AAPL", start="2020-01-01", end="2020-01-07") is True

    # A read-only handle refuses to hand out a writable connection.
    with pytest.raises(RuntimeError, match="opened read-only"):
        ro._get_connection(read_only=False)

    # Read-only on a missing file fails fast at construction (not on first query).
    with pytest.raises(FileNotFoundError, match="requires an existing cache file"):
        DuckDBCache(db_path=str(db_file.parent / "missing.duckdb"), read_only=True)


def test_escape_hatch_allows_writable_production_path(tmp_path, monkeypatch):
    """`LIVETRADING_ALLOW_PRODUCTION_PATHS=1` is an explicit operator override."""
    db_file = _fake_repo_cache_file(tmp_path, monkeypatch)
    monkeypatch.setenv(_ALLOW_ENV, "1")

    cache = DuckDBCache(db_path=str(db_file))
    cache.store_bars("CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=3))
    assert db_file.exists()
    assert len(cache.query_bars("CacheGuardProvider", "AAPL")) == 3


def test_non_pytest_process_writable_open_behaviour_unchanged(tmp_path, monkeypatch):
    """Without `PYTEST_CURRENT_TEST` the constructor behaves exactly like HEAD."""
    db_file = _fake_repo_cache_file(tmp_path, monkeypatch)
    monkeypatch.delenv(_MARKER_ENV, raising=False)
    monkeypatch.delenv(_ALLOW_ENV, raising=False)

    assert production_cache_guard_active() is False

    # Directory -> `<dir>/cache.duckdb` resolution and table creation are unchanged.
    cache = DuckDBCache(db_path=str(db_file.parent))
    assert cache.db_path == str(db_file)
    assert cache.is_memory is False
    assert cache.read_only is False
    cache.store_bars("CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=4))
    assert len(cache.query_bars("CacheGuardProvider", "AAPL")) == 4

    # Reopening the same file (the pre-existing persistence path) still works.
    reopened = DuckDBCache(db_path=str(db_file))
    assert len(reopened.query_bars("CacheGuardProvider", "AAPL")) == 4


# ---------------------------------------------------------------------------------------------
# L2.18: deleting an `asset_sync_metadata` row is NOT a durable exclusion -- the cache heals.
#
# 2026-10-22: two independent attempts to exclude an asset by deleting its metadata row failed the
# same way. The next sync enumerates the universe and re-inserts the row (measured: rebuilt within
# 156 seconds by a real Stage-1 health check). The SOP (`docs/version.md` L2.18) therefore says
# exclusion belongs in the universe / config layer / `docs/black_list.md`, and the cache layer must
# be able to rebuild a missing -- or contradicting -- row from `ohlcv_bars` instead of trusting the
# request window that happened to trigger the sync.
#
# Every test below writes to a throwaway `tmp_path` database (never `<repo>/data/cache.duckdb`),
# uses synthetic frames only, and asserts the ONE priority order that matters:
#     bar facts  >  prior metadata  >  this request window
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def disk_cache(tmp_path):
    """File-backed cache under `tmp_path` (L2.18 tests, offline, never the production file)."""
    return DuckDBCache(db_path=str(tmp_path / "l218_cache.duckdb"))


def _delete_metadata_row(cache, provider, symbol):
    """Reproduces the "manual cache surgery" attempt: drop the metadata row only."""
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM asset_sync_metadata WHERE provider = ? AND symbol = ?", [provider, symbol]
        )
    finally:
        if not cache.is_memory:
            con.close()


def _force_metadata(cache, provider, symbol, **fields):
    """Injects a metadata state the bars do not support (simulates a legacy/hand-edited row)."""
    assignments = ", ".join(f"{key} = ?" for key in fields)
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            f"UPDATE asset_sync_metadata SET {assignments} WHERE provider = ? AND symbol = ?",
            [*fields.values(), provider, symbol],
        )
    finally:
        if not cache.is_memory:
            con.close()


def test_missing_metadata_row_is_rebuilt_from_bar_facts_not_request_window(disk_cache):
    """L2.18: after the row is deleted, the next sync must rebuild it from `ohlcv_bars`
    MIN/MAX -- NOT from the (much narrower) window that sync happened to request."""
    provider, symbol = "FuyaoDataProvider", "600362.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2024-02-29"))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2024, 2, 29)

    _delete_metadata_row(disk_cache, provider, symbol)
    assert disk_cache.get_asset_metadata(provider, symbol) is None  # the "exclusion" attempt

    # A single narrow-window sync (one month in the middle of the history) re-creates the row.
    disk_cache.store_bars(
        provider,
        symbol,
        _make_daily_ohlcv("2021-03-01", "2021-03-31"),
        requested_start="2021-03-01",
        requested_end="2021-03-31",
    )

    meta = disk_cache.get_asset_metadata(provider, symbol)
    assert meta is not None, "deleting the row is not durable: the next sync re-inserts it"
    assert meta["earliest_available_date"] == datetime.date(2017, 1, 18), (
        "rebuilt from the request window (2021-03-01) instead of the bar table's MIN(date)"
    )
    assert meta["latest_available_date"] == datetime.date(2024, 2, 29), (
        "rebuilt from the request window (2021-03-31) instead of the bar table's MAX(date)"
    )
    # The rebuilt row is immediately usable by the freshness gate again.
    assert disk_cache.is_range_covered(
        provider, symbol, start="2018-01-02", end="2024-02-29"
    ) is True


def test_metadata_latest_newer_than_bars_is_clamped_to_bar_fact(disk_cache):
    """L2.18: a row claiming a `latest_available_date` past the newest stored bar (the 2026-10-22
    stale-mark shape) is clamped DOWN to the bar fact -- not overwritten by the request window,
    and not preserved by `L2.6`'s monotone GREATEST (which only refuses to move backwards)."""
    provider, symbol = "IncidentProvider", "SPY"
    disk_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-22"),
        sync_time=datetime.datetime(2026, 10, 22, 16, 0),
    )
    _force_metadata(disk_cache, provider, symbol, latest_available_date=datetime.date(2026, 10, 30))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 30)

    # A later sync whose own window ends *before* the newest stored bar.
    disk_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-21"),
        requested_start="2026-10-19", requested_end="2026-10-21",
        sync_time=datetime.datetime(2026, 10, 23, 9, 0),
    )

    meta = disk_cache.get_asset_metadata(provider, symbol)
    assert meta["latest_available_date"] == datetime.date(2026, 10, 22), (
        "expected the bar fact (2026-10-22), got neither the lie (2026-10-30) nor the request "
        "window (2026-10-21)"
    )


def test_metadata_earliest_older_than_bars_is_clamped_to_bar_fact(disk_cache):
    """L2.18: a row claiming history older than any stored bar is clamped to `MIN(date)`. It must
    not keep claiming a range the cache cannot serve, and must not adopt the request window."""
    provider, symbol = "FuyaoDataProvider", "600900.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))
    _force_metadata(disk_cache, provider, symbol, earliest_available_date=datetime.date(2010, 1, 1))

    disk_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2024-01-02"
    )

    meta = disk_cache.get_asset_metadata(provider, symbol)
    assert meta["earliest_available_date"] == datetime.date(2017, 1, 18), (
        "expected the bar fact (2017-01-18); 2010-01-01 is an unverifiable claim and 2024-01-02 "
        "is the request window"
    )
    assert meta["is_earliest_known"] is False, "a narrower window must not fabricate the probe"
    # Consequence: the freshness gate no longer reports the dropped 2010-2016 range as covered.
    assert disk_cache.is_range_covered(
        provider, symbol, start="2012-01-01", end="2017-02-28"
    ) is False


def test_metadata_earliest_later_than_bars_is_restored_from_bar_fact(disk_cache):
    """L2.18 (2026-10-22 six-symbol shape): a row that records a LATER start than the oldest bar
    on disk -- and whose triggering window did not supply that older bar -- is repaired from the
    bar table, and the repair is not a request-window write."""
    provider, symbol = "FuyaoDataProvider", "600584.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2020-11-30", "2021-02-26"))
    _force_metadata(disk_cache, provider, symbol, earliest_available_date=datetime.date(2024, 1, 2))

    disk_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2024-01-02"
    )

    assert disk_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2020, 11, 30)


def test_bar_fact_reconciliation_keeps_both_monotone_directions(disk_cache):
    """L2.18 x L2.6 regression: the bar-fact reconciliation must not resurrect the downgrade.

    (a) a later-window sync cannot shrink the recorded span (even right after a row deletion),
    (b) a sync that genuinely reaches further back still widens it,
    (c) a stale/regressed fetch cannot roll `latest_available_date` backwards.
    """
    provider, symbol = "FuyaoDataProvider", "601988.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))

    # (a) row deleted, then a much later window syncs: the span must be the bar facts again
    _delete_metadata_row(disk_cache, provider, symbol)
    disk_cache.store_bars(
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-02-29"), requested_start="2024-01-02"
    )
    meta = disk_cache.get_asset_metadata(provider, symbol)
    assert meta["earliest_available_date"] == datetime.date(2017, 1, 18)
    assert meta["latest_available_date"] == datetime.date(2024, 2, 29)

    # (b) an earlier backfill still moves the earliest boundary earlier
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2015-01-05", "2015-01-30"))
    assert disk_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2015, 1, 5)

    # (c) a stale fetch ending before the newest stored bar cannot roll latest backwards
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-15"))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2024, 2, 29)


def test_metadata_self_heal_logs_a_warning(disk_cache, caplog):
    """L2.18: a repair must never be silent -- one WARNING per repaired contradiction."""
    provider, symbol = "FuyaoDataProvider", "600362.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2017-01-18", "2024-02-29"))
    _delete_metadata_row(disk_cache, provider, symbol)

    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        disk_cache.store_bars(
            provider, symbol, _make_daily_ohlcv("2021-03-01", "2021-03-31"), requested_start="2021-03-01"
        )

    heal_records = [r for r in caplog.records if "self-heal" in r.getMessage()]
    assert heal_records, f"expected a self-heal WARNING, got: {[r.getMessage() for r in caplog.records]}"
    assert any(provider in r.getMessage() and symbol in r.getMessage() for r in heal_records)
    assert all(r.levelno == logging.WARNING for r in heal_records)
    assert "missing" in " ".join(r.getMessage() for r in heal_records)


def test_normal_sync_emits_no_self_heal_warning(disk_cache, caplog):
    """The healing path must be free of log noise: a plain two-window sync repairs nothing."""
    provider, symbol = "FuyaoDataProvider", "600030.SH"
    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"))
        disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-29"))
        disk_cache.store_bars(
            provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2024-01-02"
        )

    assert [r.getMessage() for r in caplog.records if "self-heal" in r.getMessage()] == []


def test_is_range_covered_rejects_front_hole_before_the_next_sync(disk_cache):
    """L2.18 read-path guard: until `store_bars` repairs a lying start boundary, the freshness
    gate must not report a front hole as covered (same rule as the 2026-10-22 end-side fix)."""
    provider, symbol = "FuyaoDataProvider", "601398.SH"
    disk_cache.store_bars(provider, symbol, _make_daily_ohlcv("2020-01-02", "2020-01-31"))
    _force_metadata(disk_cache, provider, symbol, earliest_available_date=datetime.date(2015, 1, 1))

    # Metadata "covers" 2016 -> but the oldest bar is 2020-01-02, so the range is a front hole.
    assert disk_cache.is_range_covered(provider, symbol, start="2016-01-01", end="2020-01-31") is False
    # The genuinely held range is still covered (no over-rejection).
    assert disk_cache.is_range_covered(provider, symbol, start="2020-01-02", end="2020-01-31") is True


# ---------------------------------------------------------------------------------------------
# L2.2 / L2.7: expected-session freshness (`expected_last_session`) + the 4-column migration.
#
# Design authority: docs/market_clock_and_asof_resolution.md §6, §11 Phase 1, §12 cases 9/10.
#
# The whole point is ONE rule -- `covered <=> latest_available_date >= expected_last_session`,
# with the 7-day tolerance and the "we already synced today" short-circuit abolished -- applied to
# the daily gate (`is_range_covered`) and the intraday gate (`is_intraday_range_covered`) through
# the SAME helpers (`_expected_session_is_fresh` / `_bar_layer_reaches_end`), so the two axes can
# never drift into a third opinion.
# ---------------------------------------------------------------------------------------------

#: The six symbols whose daily bars stopped on 2026-10-21 in the 2026-10-22 live run
#: (docs/version.md §9). Used verbatim so the regression reads like the incident.
_STALE_SIX = ["600362.SH", "600584.SH", "600660.SH", "600900.SH", "601728.SH", "601872.SH"]
#: The fourteen siblings that had published their 2026-10-22 bar in the same run.
_FRESH_FOURTEEN = [
    "000001.SZ", "000858.SZ", "002415.SZ", "300750.SZ", "600030.SH", "600036.SH", "600519.SH",
    "601318.SH", "601398.SH", "601988.SH", "603288.SH", "688981.SH", "000333.SZ", "002594.SZ",
]


def test_l2_2_schema_migration_is_idempotent_and_upgrades_an_old_db_in_place(tmp_path):
    """L2.2: the four probe columns land on BOTH sync-metadata tables, old caches upgrade in
    place, and re-initialising an already-migrated cache is a no-op (never `column exists`).

    `_init_tables` runs on every writable open, so the migration has to be a pure function of the
    existing schema -- otherwise the second run of a live process would crash.
    """
    db_file = tmp_path / "legacy_schema.duckdb"
    con = duckdb.connect(str(db_file))
    try:
        # Exactly the pre-L2.2 shape (6 and 8 columns, no probe bookkeeping).
        con.execute("""
            CREATE TABLE asset_sync_metadata (
                provider VARCHAR NOT NULL, symbol VARCHAR NOT NULL,
                earliest_available_date DATE NOT NULL,
                is_earliest_known BOOLEAN NOT NULL DEFAULT FALSE,
                latest_available_date DATE NOT NULL,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol)
            );
            CREATE TABLE intraday_sync_metadata (
                provider VARCHAR NOT NULL, symbol VARCHAR NOT NULL, interval VARCHAR NOT NULL,
                earliest_available_ts TIMESTAMP NOT NULL,
                is_earliest_known BOOLEAN NOT NULL DEFAULT FALSE,
                latest_available_ts TIMESTAMP NOT NULL, latest_close_ref DOUBLE,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol, interval)
            );
            INSERT INTO asset_sync_metadata VALUES
                ('FuyaoDataProvider', '600362.SH', DATE '2017-01-18', TRUE,
                 DATE '2026-10-21', TIMESTAMP '2026-10-22 10:00:00');
            INSERT INTO intraday_sync_metadata VALUES
                ('FuyaoDataProvider', '600362.SH', '5m', TIMESTAMP '2026-10-01 09:35:00', TRUE,
                 TIMESTAMP '2026-10-21 15:00:00', 12.5, TIMESTAMP '2026-10-22 10:00:00');
        """)
    finally:
        con.close()

    new_columns = {
        "expected_last_session": "DATE",
        "probe_attempts": "INTEGER",
        "last_probe_result": "VARCHAR",
        "last_probe_time": "TIMESTAMP",
    }

    cache = DuckDBCache(db_path=str(db_file))  # writable open => migration
    con = cache._get_connection(read_only=True)
    try:
        for table in ("asset_sync_metadata", "intraday_sync_metadata"):
            info = {row[1]: row[2].upper() for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            for column, column_type in new_columns.items():
                assert column in info, f"{table} was not migrated: missing {column}"
                assert info[column].startswith(column_type), f"{table}.{column} type drift: {info[column]}"
        # Existing rows survive verbatim; the new counters take their default.
        row = con.execute(
            "SELECT earliest_available_date, latest_available_date, probe_attempts, last_probe_result "
            "FROM asset_sync_metadata WHERE symbol = '600362.SH'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == datetime.date(2017, 1, 18)
    assert row[1] == datetime.date(2026, 10, 21)
    assert row[2] == 0, "existing rows must default probe_attempts to 0, not NULL"
    assert row[3] is None

    # Idempotency: a second writable open (same process or the next live run) must not raise.
    DuckDBCache(db_path=str(db_file))
    DuckDBCache(db_path=str(db_file))
    reopened = DuckDBCache(db_path=str(db_file))
    con = reopened._get_connection(read_only=True)
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info('asset_sync_metadata')").fetchall()]
    finally:
        con.close()
    assert columns.count("expected_last_session") == 1, "the migration must not add a column twice"

    # The migrated cache is immediately usable by the new contract (and the old one).
    reopened.store_bars("FuyaoDataProvider", "600362.SH", _make_daily_ohlcv("2018-01-02", "2026-10-21"))
    assert reopened.is_range_covered(
        "FuyaoDataProvider", "600362.SH",
        start="2018-01-02", end="2026-10-21", now=pd.Timestamp("2026-10-22 16:30:00"),
        expected_last_session=datetime.date(2026, 10, 21),
    ) is True
    assert reopened.is_range_covered(
        "FuyaoDataProvider", "600362.SH",
        start="2018-01-02", end="2026-10-21", now=pd.Timestamp("2026-10-22 16:30:00"),
        expected_last_session=datetime.date(2026, 10, 22),
    ) is False


def test_case9_expected_last_session_catches_the_six_stale_symbols(disk_cache):
    """§12 case 9 -- THE incident, replayed: 6 symbols stop on 2026-10-21, 14 reach 2026-10-22.

    With `expected_last_session=2026-10-22` the six must be `False` and the fourteen `True`, and
    `latest_close(strict=True)` must RAISE for the six instead of quietly returning the 2026-10-21
    close. The loud failure is the deliverable: the ticket must not be buildable at all while a
    position would be marked at yesterday's price.
    """
    provider = "FuyaoDataProvider"
    for symbol in _STALE_SIX:
        disk_cache.store_bars(
            provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-21"),
            sync_time=datetime.datetime(2026, 10, 22, 10, 0),
        )
    for symbol in _FRESH_FOURTEEN:
        disk_cache.store_bars(
            provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-22"),
            sync_time=datetime.datetime(2026, 10, 22, 16, 0),
        )

    expected = datetime.date(2026, 10, 22)
    now = pd.Timestamp("2026-10-22 16:30:00")

    stale_flags = {
        symbol: disk_cache.is_range_covered(
            provider, symbol, start="2026-10-19", end="2026-10-22", now=now,
            expected_last_session=expected,
        )
        for symbol in _STALE_SIX
    }
    assert stale_flags == {symbol: False for symbol in _STALE_SIX}

    fresh_flags = {
        symbol: disk_cache.is_range_covered(
            provider, symbol, start="2026-10-19", end="2026-10-22", now=now,
            expected_last_session=expected,
        )
        for symbol in _FRESH_FOURTEEN
    }
    assert fresh_flags == {symbol: True for symbol in _FRESH_FOURTEEN}

    # "Loud failure": the six cannot be priced at all under strict freshness...
    universe = {
        symbol: disk_cache.query_bars(provider, symbol, start="2026-10-19", end="2026-10-22")
        for symbol in _STALE_SIX + _FRESH_FOURTEEN
    }
    for symbol in _STALE_SIX:
        assert universe[symbol].index[-1] == pd.Timestamp("2026-10-21"), "fixture drifted"
        with pytest.raises(StalePriceError):
            latest_close(universe, symbol, expected_session="2026-10-22", strict=True)
        # ...while the operator's explicit `--allow-stale-prices` opt-out still degrades to the
        # stale close with a warning (documented degradation, never the default).
        assert latest_close(universe, symbol, expected_session="2026-10-22", strict=False) == pytest.approx(
            float(universe[symbol]["Close"].iloc[-1])
        )
    for symbol in _FRESH_FOURTEEN:
        assert latest_close(universe, symbol, expected_session="2026-10-22", strict=True) > 0


def test_expected_last_session_abolishes_the_legacy_tolerance_and_sync_short_circuit(mem_cache):
    """The 7-day tolerance and "already synced today" are GONE on the expected-session path.

    The clean discriminator: an historical window that the cache genuinely holds (bars reach the
    requested end) but that is older than the expected session. Legacy says covered; the
    expected-session contract says no, because the *cache* is stale as of the session being
    priced -- which is the only question the live path is asking.
    """
    provider = "IncidentProvider"
    mem_cache.store_bars(
        provider, "SPY", _make_daily_ohlcv("2026-10-19", "2026-10-21"),
        sync_time=datetime.datetime(2026, 10, 22, 10, 0),
    )
    now = pd.Timestamp("2026-10-22 10:35:00")

    # Legacy (backtest/research): the requested end IS reached -> covered.
    assert mem_cache.is_range_covered(
        provider, "SPY", start="2026-10-19", end="2026-10-21", now=now
    ) is True
    # Expected-session (live): the cache is one session short -> not covered, no tolerance.
    assert mem_cache.is_range_covered(
        provider, "SPY", start="2026-10-19", end="2026-10-21", now=now,
        expected_last_session=datetime.date(2026, 10, 22),
    ) is False

    # Metadata claiming the expected session cannot rescue it either -- the BAR layer is the
    # second opinion and it must agree (this is L2.9's rule, kept intact in both modes).
    _force_metadata(
        mem_cache, provider, "SPY",
        latest_available_date=datetime.date(2026, 10, 22),
        last_sync_time=datetime.datetime(2026, 10, 22, 10, 0),
    )
    assert mem_cache.is_range_covered(
        provider, "SPY", start="2026-10-19", end="2026-10-22", now=now,
        expected_last_session=datetime.date(2026, 10, 22),
    ) is False


def test_expected_last_session_accepts_a_weekend_padded_request_end(mem_cache):
    """A Saturday `end` with a Friday expectation stays covered (no pointless weekend refetch):
    the expectation is judged as a session, the request end still uses the Friday tolerance."""
    provider = "IncidentProvider"
    mem_cache.store_bars(
        provider, "SPY", _make_daily_ohlcv("2026-10-19", "2026-10-23"),
        sync_time=datetime.datetime(2026, 10, 24, 9, 0),
    )
    saturday = pd.Timestamp("2026-10-24 09:00:00")
    assert mem_cache.is_range_covered(
        provider, "SPY", start="2026-10-19", end="2026-10-24", now=saturday,
        expected_last_session=datetime.date(2026, 10, 23),
    ) is True
    # One session later (Monday 10-26) it is uncovered again -- the tolerance is not a blank cheque.
    assert mem_cache.is_range_covered(
        provider, "SPY", start="2026-10-19", end="2026-10-24", now=saturday,
        expected_last_session=datetime.date(2026, 10, 26),
    ) is False


def test_expected_last_session_rejects_an_unparseable_expectation(mem_cache):
    """An unparseable expectation is a caller bug and must fail loudly -- degrading to the
    never-raise legacy path would reintroduce exactly the silent staleness this gate removes."""
    mem_cache.store_bars("IncidentProvider", "SPY", _make_daily_ohlcv("2026-10-19", "2026-10-22"))
    with pytest.raises(ValueError, match="expected_last_session"):
        mem_cache.is_range_covered(
            "IncidentProvider", "SPY", start="2026-10-19", end="2026-10-22",
            expected_last_session="not-a-date",
        )
    with pytest.raises(ValueError, match="expected_last_session"):
        mem_cache.is_intraday_range_covered(
            "IncidentProvider", "SPY", interval="5m", start="2026-10-19", end="2026-10-22",
            expected_last_session="not-a-date",
        )


# --- §12 case 10: the `expected_last_session=None` path is bit-for-bit the pre-L2.2 behaviour ---
#
# `_CASE10_*` below are not written by hand: they were RECORDED by running
# `is_range_covered` / `is_intraday_range_covered` on the working tree BEFORE L2.2 touched
# `common/duckdb_cache.py` (a throwaway recorder script over the very same synthetic states this
# test rebuilds). They are the frozen answers of the old implementation, so any drift introduced
# by the refactor fails here even if it "looks reasonable".

_CASE10_DAILY = [
    ('FRESH', '2026-10-19', '2026-10-22', '2026-10-22 16:30:00', True),
    ('FRESH', '2026-10-19', None, '2026-10-22 16:30:00', True),
    ('FRESH', '2026-10-19', '2026-10-21', '2026-10-22 16:30:00', True),
    ('STALE1', '2026-10-19', '2026-10-22', '2026-10-22 10:35:00', False),
    ('STALE1', '2026-10-19', None, '2026-10-22 10:35:00', False),
    ('STALE1', '2026-10-19', '2026-10-21', '2026-10-22 10:35:00', True),
    ('PAST3', '2026-10-12', '2026-10-19', '2026-10-22 16:30:00', False),
    ('PAST3', '2026-10-12', '2026-10-16', '2026-10-22 16:30:00', True),
    ('PAST9', '2026-09-28', '2026-10-14', '2026-10-22 16:30:00', False),
    ('PAST9', '2026-09-28', '2026-10-05', '2026-10-22 16:30:00', True),
    ('WEEKEND', '2026-10-19', '2026-10-24', '2026-10-24 09:00:00', True),
    ('WEEKEND', '2026-10-19', None, '2026-10-24 09:00:00', True),
    ('WEEKEND', '2026-10-19', '2026-10-25', '2026-10-25 09:00:00', False),
    ('WEEKSHORT', '2026-10-19', '2026-10-23', '2026-10-24 09:00:00', False),
    ('FUTURE', '2026-10-19', '2030-01-01', '2026-10-22 16:30:00', True),
    ('FUTURE', '2026-10-19', '2026-10-23', '2026-10-22 16:30:00', True),
    ('HOLE', '2016-01-01', '2020-01-31', '2026-10-22 16:30:00', False),
    ('HOLE', '2019-12-01', '2020-01-31', '2026-10-22 16:30:00', False),
    ('HOLE', '2020-01-02', '2020-01-31', '2026-10-22 16:30:00', True),
    ('NOBARS', '2026-10-19', '2026-10-22', '2026-10-22 16:30:00', False),
    ('IPO', '1990-01-01', '2015-06-12', '2026-10-22 16:30:00', True),
    ('IPO', '2015-05-01', '2015-06-12', '2026-10-22 16:30:00', True),
    ('UNKNOWN_SYMBOL', '2026-10-19', '2026-10-22', '2026-10-22 16:30:00', False),
]

_CASE10_INTRADAY = [
    ('5M_FULL', '2026-10-14', '2026-10-16', '2026-10-19 16:00:00', True),
    ('5M_FULL', '2026-10-14', None, '2026-10-19 16:00:00', False),
    ('5M_FULL', '2026-10-14', '2026-10-15', '2026-10-19 16:00:00', True),
    ('5M_FULL', '2026-10-14', '2026-10-19', '2026-10-19 16:00:00', False),
    ('5M_FULL', '2026-10-14', '2026-10-16 11:30:00', '2026-10-19 16:00:00', True),
    ('5M_LIE', '2026-10-14', '2026-10-16', '2026-10-19 16:00:00', True),
    ('5M_LIE', '2026-10-14', '2026-10-15', '2026-10-19 16:00:00', True),
    ('5M_NOMETA', '2026-10-14', '2026-10-16', '2026-10-19 16:00:00', False),
]


def _build_case10_daily_states(cache):
    provider = "FuyaoDataProvider"
    cache.store_bars(provider, "FRESH", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
                     sync_time=datetime.datetime(2026, 10, 22, 16, 0))
    cache.store_bars(provider, "STALE1", _make_daily_ohlcv("2026-10-19", "2026-10-21"),
                     sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _force_metadata(cache, provider, "STALE1", latest_available_date=datetime.date(2026, 10, 22),
                    last_sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    cache.store_bars(provider, "PAST3", _make_daily_ohlcv("2026-10-12", "2026-10-16"),
                     sync_time=datetime.datetime(2026, 10, 16, 16, 0))
    cache.store_bars(provider, "PAST9", _make_daily_ohlcv("2026-10-01", "2026-10-05"),
                     sync_time=datetime.datetime(2026, 10, 5, 16, 0))
    cache.store_bars(provider, "WEEKEND", _make_daily_ohlcv("2026-10-19", "2026-10-23"),
                     sync_time=datetime.datetime(2026, 10, 24, 9, 0))
    cache.store_bars(provider, "FUTURE", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
                     sync_time=datetime.datetime(2026, 10, 22, 16, 0))
    cache.store_bars(provider, "HOLE", _make_daily_ohlcv("2020-01-02", "2020-01-31"))
    _force_metadata(cache, provider, "HOLE", earliest_available_date=datetime.date(2015, 1, 1))
    cache.store_bars(provider, "NOBARS", _make_daily_ohlcv("2026-10-19", "2026-10-22"))
    con = cache._get_connection(read_only=False)
    try:
        con.execute("DELETE FROM ohlcv_bars WHERE provider = ? AND symbol = ?", [provider, "NOBARS"])
    finally:
        if not cache.is_memory:
            con.close()
    cache.store_bars(provider, "IPO", _make_daily_ohlcv("2015-06-01", "2015-06-12"),
                     requested_start="2000-01-01")
    cache.store_bars(provider, "WEEKSHORT", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
                     sync_time=datetime.datetime(2026, 10, 24, 9, 0))


@pytest.mark.parametrize("symbol,start,end,now,expected", _CASE10_DAILY)
def test_case10_none_path_matches_recorded_pre_l2_2_behaviour(symbol, start, end, now, expected):
    """§12 case 10 -- backtest/research non-regression, per recorded old answer.

    One fresh in-memory cache per case keeps the states independent (the recorder did the same):
    rebuilding costs microseconds and avoids cross-case metadata contamination via `sync_time`.
    """
    cache = DuckDBCache(db_path=":memory:")
    _build_case10_daily_states(cache)
    assert cache.is_range_covered(
        "FuyaoDataProvider", symbol, start=start, end=end, now=pd.Timestamp(now),
        expected_last_session=None,
    ) is expected


def _case10_intraday_bars(start_day: str, days: int) -> pd.DataFrame:
    frames = []
    for d in pd.bdate_range(start_day, periods=days):
        date_str = d.strftime("%Y-%m-%d")
        times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 15:00:00", freq="30min")
        n = len(times)
        frames.append(pd.DataFrame(
            {"Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
             "Close": [100.5] * n, "Volume": [10.0] * n}, index=times))
    return pd.concat(frames, axis=0)


def _build_case10_intraday_states(cache):
    provider = "FuyaoDataProvider"
    cache.store_intraday_bars(provider, "5M_FULL", _case10_intraday_bars("2026-10-14", 3),
                              interval="5m", sync_time=datetime.datetime(2026, 10, 16, 16, 0))
    cache.store_intraday_bars(provider, "5M_LIE", _case10_intraday_bars("2026-10-14", 2),
                              interval="5m", sync_time=datetime.datetime(2026, 10, 16, 16, 0))
    con = cache._get_connection(read_only=False)
    try:
        # Metadata claims the 2026-10-16 session while the bars stop on 2026-10-15.
        con.execute(
            "UPDATE intraday_sync_metadata SET latest_available_ts = ?, last_sync_time = ? "
            "WHERE provider = ? AND symbol = ? AND interval = ?",
            [pd.Timestamp("2026-10-16 15:00"), datetime.datetime(2026, 10, 16, 16, 0),
             provider, "5M_LIE", "5m"],
        )
    finally:
        if not cache.is_memory:
            con.close()
    cache.store_intraday_bars(provider, "5M_NOMETA", _case10_intraday_bars("2026-10-14", 3),
                              interval="5m")
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
            [provider, "5M_NOMETA", "5m"],
        )
    finally:
        if not cache.is_memory:
            con.close()


@pytest.mark.parametrize("symbol,start,end,now,expected", _CASE10_INTRADAY)
def test_case10_none_path_matches_recorded_pre_l2_2_intraday_behaviour(symbol, start, end, now, expected):
    """§12 case 10 for the intraday axis (L2.7 must not tighten the `None` path either)."""
    cache = DuckDBCache(db_path=":memory:")
    _build_case10_intraday_states(cache)
    assert cache.is_intraday_range_covered(
        "FuyaoDataProvider", symbol, interval="5m", start=start, end=end, now=pd.Timestamp(now),
        expected_last_session=None,
    ) is expected

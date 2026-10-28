"""Unit tests for common/duckdb_cache.py (DuckDB OHLCV Cache System).

All tests run 100% offline using in-memory or temporary DuckDB instances.
"""

import datetime
import logging
import os
import sys
import warnings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import (
    AdjustBasisRequiredError,
    AdjustBasisTransitionRefused,
    DuckDBCache,
    HistoryRewriteRefused,
    ProductionCachePathError,
    UnknownAdjustBasisError,
    is_synthetic_provider,
    production_cache_guard_active,
    production_cache_roots,
    refuse_production_cache_path,
    resample_ohlcv,
    is_intraday_interval,
    _get_resample_rule,
)
from common.data import BaseDataProvider
from common.freshness import StalePriceError, latest_close
import common.duckdb_cache as duckdb_cache_module


@pytest.fixture
def mem_cache():
    """In-memory DuckDBCache instance for fast offline testing."""
    return DuckDBCache(db_path=":memory:")


#: M3.6 review P1: `store_bars` / `store_intraday_bars` now REFUSE an implicit adjustment basis
#: (`AdjustBasisRequiredError`), because an unlabelled row cannot be read back honestly -- see
#: `common/duckdb_cache.py::AdjustBasisRequiredError`. The fixtures below seed synthetic OHLCV
#: whose basis is irrelevant to what they assert (dates, metadata reconciliation, rewrite gates,
#: range coverage), and every read in this module uses the historical default `adjust="qfq"`.
#: These two helpers therefore declare that default explicitly instead of leaving the label out.
#: Tests that assert on BASIS SEMANTICS still call `store_bars(..., adjustflag=...)` directly.
def _store_bars(cache, provider, symbol, df, **kwargs):
    kwargs.setdefault("adjustflag", "qfq")
    return cache.store_bars(provider, symbol, df, **kwargs)


def _store_intraday_bars(cache, provider, symbol, df, **kwargs):
    kwargs.setdefault("adjustflag", "qfq")
    return cache.store_intraday_bars(provider, symbol, df, **kwargs)


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
    _store_bars(mem_cache, "TestProvider", "AAPL", df)

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

    _store_bars(mem_cache, "YFinanceProvider", "SPY", df_yf)
    _store_bars(mem_cache, "FuyaoProvider", "SPY", df_fuyao)

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
    _store_bars(mem_cache, "YFinanceProvider", "NEW_IPO", df, requested_start="2000-01-01")

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
    _store_bars(mem_cache, "SyntheticDataProvider", "SPY", df)

    meta = mem_cache.get_asset_metadata("SyntheticDataProvider", "SPY")
    assert meta is None
    assert mem_cache.is_range_covered("SyntheticDataProvider", "SPY", "2020-01-01", "2020-01-05") is False

    bars = mem_cache.query_bars("SyntheticDataProvider", "SPY")
    assert bars.empty


def test_resample_ohlcv_any_interval(mem_cache):
    """Verifies that daily bars stored in DuckDB can serve weekly (1wk) and monthly (1mo) intervals."""
    # 20 business days (~4 full weeks)
    df = _make_sample_ohlcv("2020-01-01", periods=20, base_price=100.0)
    _store_bars(mem_cache, "TestProvider", "QQQ", df)

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

    _store_bars(mem_cache, "BatchProvider", "SYM_A", df_a)
    _store_bars(mem_cache, "BatchProvider", "SYM_B", df_b)

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
    _store_bars(cache1, "DiskProvider", "MSFT", df)

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

    _store_bars(mem_cache, "TestProvider", "AAPL", df, sync_time=yesterday_sync)

    # 1. On today (2024-01-03), last sync was yesterday (2024-01-02), so cache is NOT covered
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end=None, now=today_time) is False
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end="2024-01-03", now=today_time) is False

    # 2. Synced today (2024-01-03) is still NOT coverage by itself: the bars only reach
    #    2024-01-02, i.e. the sync ran before upstream published today's bar -> must re-fetch.
    #    (This is exactly the 2026-10-22 incident: fresh metadata, bars one day short.)
    today_sync = datetime.datetime(2024, 1, 3, 15, 30)
    _store_bars(mem_cache, "TestProvider", "AAPL", df, sync_time=today_sync)
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end=None, now=today_time) is False
    assert mem_cache.is_range_covered("TestProvider", "AAPL", start="2024-01-01", end="2024-01-03", now=today_time) is False

    # 3. Once a bar for 2024-01-03 actually lands, the same-day fast path applies again
    df_with_today = _make_sample_ohlcv("2024-01-01", periods=3)  # 2024-01-01 .. 2024-01-03
    _store_bars(mem_cache, "TestProvider", "AAPL", df_with_today, sync_time=today_sync)
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
    _store_bars(mem_cache, "IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 10, 0))
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
    _store_bars(mem_cache, "IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    now = pd.Timestamp("2026-10-22 16:05:00")
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-22", now=now) is True
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=None, now=now) is True


def test_is_range_covered_false_when_no_bars_stored(mem_cache):
    """Fresh metadata with an empty `ohlcv_bars` table must never report coverage."""
    df = _make_sample_ohlcv("2026-10-19", periods=4)
    _store_bars(mem_cache, "IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

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
    _store_bars(mem_cache, "IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 24, 9, 0))

    saturday = pd.Timestamp("2026-10-24 09:00:00")
    assert saturday.weekday() == 5
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end="2026-10-24", now=saturday) is True
    assert mem_cache.is_range_covered("IncidentProvider", "SPY", start="2026-10-19", end=None, now=saturday) is True

    # ...but a weekday request that is short is still uncovered, even inside the 7-day tolerance
    short_df = _make_sample_ohlcv("2026-10-19", periods=4)  # Mon 10-19 .. Thu 10-22
    _store_bars(mem_cache, "IncidentProvider", "SHORT", short_df, sync_time=datetime.datetime(2026, 10, 24, 9, 0))
    assert mem_cache.is_range_covered("IncidentProvider", "SHORT", start="2026-10-19", end="2026-10-23", now=saturday) is False


def test_is_range_covered_is_per_symbol_not_per_provider(mem_cache):
    """One short symbol must not contaminate, nor be masked by, a complete sibling symbol
    under the same provider (the incident hit 6 of 20 symbols in one universe)."""
    short_df = _make_sample_ohlcv("2026-10-19", periods=3)   # stops 2026-10-21
    full_df = _make_sample_ohlcv("2026-10-19", periods=4)    # reaches 2026-10-22
    now = pd.Timestamp("2026-10-22 16:00:00")

    _store_bars(mem_cache, "IncidentProvider", "SHORT", short_df, sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _force_sync_metadata(
        mem_cache, "IncidentProvider", "SHORT",
        latest_available_date=datetime.date(2026, 10, 22),
        sync_time=datetime.datetime(2026, 10, 22, 10, 0),
    )
    _store_bars(mem_cache, "IncidentProvider", "FULL", full_df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

    assert mem_cache.is_range_covered("IncidentProvider", "SHORT", start="2026-10-19", end="2026-10-22", now=now) is False
    assert mem_cache.is_range_covered("IncidentProvider", "FULL", start="2026-10-19", end="2026-10-22", now=now) is True


def test_is_range_covered_future_end_not_judged_by_bar_layer(mem_cache):
    """A requested `end` beyond today can never have a stored bar, so the bar layer must not
    reject it (otherwise every "give me everything up to a far-future date" call re-fetches
    forever). Only the metadata freshness rules apply there.
    """
    now = pd.Timestamp("2026-10-22 16:05:00")
    df = _make_sample_ohlcv("2026-10-19", periods=4)
    _store_bars(mem_cache, "IncidentProvider", "SPY", df, sync_time=datetime.datetime(2026, 10, 22, 16, 0))

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
    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))
    before = mem_cache.get_asset_metadata(provider, symbol)
    assert before["earliest_available_date"] == datetime.date(2017, 1, 18)
    assert before["is_earliest_known"] is False

    # The incident: a sync that starts at 2024-01-02 (requested_start == first returned bar)
    _store_bars(mem_cache, 
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
    _store_bars(mem_cache, 
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2000-01-01"
    )
    assert mem_cache.get_asset_metadata(provider, symbol)["is_earliest_known"] is True

    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-29"), requested_start="2024-02-01")
    assert mem_cache.get_asset_metadata(provider, symbol)["is_earliest_known"] is True


def test_store_bars_stale_latest_window_does_not_roll_back_latest_available_date(mem_cache):
    """A stale/regressed fetch (ending 2026-10-21 after 2026-10-22 was already recorded) must not
    move `latest_available_date` backwards -- MAX(existing, incoming), never greatest-latest-wins."""
    provider, symbol = "FuyaoDataProvider", "600900.SH"
    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2026-10-01", "2026-10-22"))
    assert mem_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 22)

    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2026-10-01", "2026-10-21"))
    assert mem_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 22)


def test_store_bars_earliest_can_still_improve_when_sync_reaches_further_back(mem_cache):
    """MIN semantics must not freeze the field: a sync that genuinely reaches back to 2017 still
    moves an existing 2024 earliest date earlier."""
    provider, symbol = "FuyaoDataProvider", "600584.SH"
    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"))
    assert mem_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2024, 1, 2)

    _store_bars(mem_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-01-31"))
    assert mem_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2017, 1, 18)


def test_store_bars_first_insert_records_incoming_values_verbatim(mem_cache):
    """With no existing row, the incoming window is recorded as-is (no MIN/MAX partner to compare
    against), including the `requested_start` earliest-known inference."""
    provider, symbol = "FuyaoDataProvider", "600660.SH"
    _store_bars(mem_cache, 
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
    _store_bars(mem_cache, provider, symbol, df, requested_start="2020-11-30", sync_time=datetime.datetime(2026, 10, 22, 19, 0))
    first = mem_cache.get_asset_metadata(provider, symbol)

    _store_bars(mem_cache, provider, symbol, df, requested_start="2020-11-30", sync_time=datetime.datetime(2026, 10, 22, 21, 34, 31))
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
    _store_bars(seed, "CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=5))
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
    _store_bars(cache, "CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=3))
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
    _store_bars(cache, "CacheGuardProvider", "AAPL", _make_sample_ohlcv("2020-01-01", periods=4))
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
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2024-02-29"))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2024, 2, 29)

    _delete_metadata_row(disk_cache, provider, symbol)
    assert disk_cache.get_asset_metadata(provider, symbol) is None  # the "exclusion" attempt

    # A single narrow-window sync (one month in the middle of the history) re-creates the row.
    _store_bars(disk_cache, 
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
    _store_bars(disk_cache, 
        provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-22"),
        sync_time=datetime.datetime(2026, 10, 22, 16, 0),
    )
    _force_metadata(disk_cache, provider, symbol, latest_available_date=datetime.date(2026, 10, 30))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2026, 10, 30)

    # A later sync whose own window ends *before* the newest stored bar.
    _store_bars(disk_cache, 
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
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))
    _force_metadata(disk_cache, provider, symbol, earliest_available_date=datetime.date(2010, 1, 1))

    _store_bars(disk_cache, 
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
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2020-11-30", "2021-02-26"))
    _force_metadata(disk_cache, provider, symbol, earliest_available_date=datetime.date(2024, 1, 2))

    _store_bars(disk_cache, 
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
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2017-02-28"))

    # (a) row deleted, then a much later window syncs: the span must be the bar facts again
    _delete_metadata_row(disk_cache, provider, symbol)
    _store_bars(disk_cache, 
        provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-02-29"), requested_start="2024-01-02"
    )
    meta = disk_cache.get_asset_metadata(provider, symbol)
    assert meta["earliest_available_date"] == datetime.date(2017, 1, 18)
    assert meta["latest_available_date"] == datetime.date(2024, 2, 29)

    # (b) an earlier backfill still moves the earliest boundary earlier
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2015-01-05", "2015-01-30"))
    assert disk_cache.get_asset_metadata(provider, symbol)["earliest_available_date"] == datetime.date(2015, 1, 5)

    # (c) a stale fetch ending before the newest stored bar cannot roll latest backwards
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-15"))
    assert disk_cache.get_asset_metadata(provider, symbol)["latest_available_date"] == datetime.date(2024, 2, 29)


def test_metadata_self_heal_logs_a_warning(disk_cache, caplog):
    """L2.18: a repair must never be silent -- one WARNING per repaired contradiction."""
    provider, symbol = "FuyaoDataProvider", "600362.SH"
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2017-01-18", "2024-02-29"))
    _delete_metadata_row(disk_cache, provider, symbol)

    with caplog.at_level(logging.WARNING, logger="common.duckdb_cache"):
        _store_bars(disk_cache, 
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
        _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"))
        _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2024-02-01", "2024-02-29"))
        _store_bars(disk_cache, 
            provider, symbol, _make_daily_ohlcv("2024-01-02", "2024-01-31"), requested_start="2024-01-02"
        )

    assert [r.getMessage() for r in caplog.records if "self-heal" in r.getMessage()] == []


def test_is_range_covered_rejects_front_hole_before_the_next_sync(disk_cache):
    """L2.18 read-path guard: until `store_bars` repairs a lying start boundary, the freshness
    gate must not report a front hole as covered (same rule as the 2026-10-22 end-side fix)."""
    provider, symbol = "FuyaoDataProvider", "601398.SH"
    _store_bars(disk_cache, provider, symbol, _make_daily_ohlcv("2020-01-02", "2020-01-31"))
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
    _store_bars(reopened, "FuyaoDataProvider", "600362.SH", _make_daily_ohlcv("2018-01-02", "2026-10-21"))
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
        _store_bars(disk_cache, 
            provider, symbol, _make_daily_ohlcv("2026-10-19", "2026-10-21"),
            sync_time=datetime.datetime(2026, 10, 22, 10, 0),
        )
    for symbol in _FRESH_FOURTEEN:
        _store_bars(disk_cache, 
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
    _store_bars(mem_cache, 
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
    _store_bars(mem_cache, 
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
    _store_bars(mem_cache, "IncidentProvider", "SPY", _make_daily_ohlcv("2026-10-19", "2026-10-22"))
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
    _store_bars(cache, provider, "FRESH", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
                     sync_time=datetime.datetime(2026, 10, 22, 16, 0))
    _store_bars(cache, provider, "STALE1", _make_daily_ohlcv("2026-10-19", "2026-10-21"),
                     sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _force_metadata(cache, provider, "STALE1", latest_available_date=datetime.date(2026, 10, 22),
                    last_sync_time=datetime.datetime(2026, 10, 22, 10, 0))
    _store_bars(cache, provider, "PAST3", _make_daily_ohlcv("2026-10-12", "2026-10-16"),
                     sync_time=datetime.datetime(2026, 10, 16, 16, 0))
    _store_bars(cache, provider, "PAST9", _make_daily_ohlcv("2026-10-01", "2026-10-05"),
                     sync_time=datetime.datetime(2026, 10, 5, 16, 0))
    _store_bars(cache, provider, "WEEKEND", _make_daily_ohlcv("2026-10-19", "2026-10-23"),
                     sync_time=datetime.datetime(2026, 10, 24, 9, 0))
    _store_bars(cache, provider, "FUTURE", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
                     sync_time=datetime.datetime(2026, 10, 22, 16, 0))
    _store_bars(cache, provider, "HOLE", _make_daily_ohlcv("2020-01-02", "2020-01-31"))
    _force_metadata(cache, provider, "HOLE", earliest_available_date=datetime.date(2015, 1, 1))
    _store_bars(cache, provider, "NOBARS", _make_daily_ohlcv("2026-10-19", "2026-10-22"))
    con = cache._get_connection(read_only=False)
    try:
        con.execute("DELETE FROM ohlcv_bars WHERE provider = ? AND symbol = ?", [provider, "NOBARS"])
    finally:
        if not cache.is_memory:
            con.close()
    _store_bars(cache, provider, "IPO", _make_daily_ohlcv("2015-06-01", "2015-06-12"),
                     requested_start="2000-01-01")
    _store_bars(cache, provider, "WEEKSHORT", _make_daily_ohlcv("2026-10-19", "2026-10-22"),
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
    _store_intraday_bars(cache, provider, "5M_FULL", _case10_intraday_bars("2026-10-14", 3),
                              interval="5m", sync_time=datetime.datetime(2026, 10, 16, 16, 0))
    _store_intraday_bars(cache, provider, "5M_LIE", _case10_intraday_bars("2026-10-14", 2),
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
    _store_intraday_bars(cache, provider, "5M_NOMETA", _case10_intraday_bars("2026-10-14", 3),
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


# ---------------------------------------------------------------------------
# L2.32: history-rewrite gate at the cache's single write chokepoint.
#
# `ohlcv_bars`' PK is (provider, symbol, date) and `store_bars` writes with a plain
# `INSERT OR REPLACE`, i.e. an unconditional in-place overwrite. On 2026-10-23 a one-scalar-
# per-symbol rescale from `common/financial_api.py` reached that statement through
# `CachedDataProvider.fetch_ohlcv` and rewrote 13,303 stored rows over 2020-11-30..2023-12-29.
# These tests pin the gate's exact contract: value-based (not date-based), rtol 1e-9, and a
# declared opt-in for a genuine re-derivation.
# ---------------------------------------------------------------------------


def _gate_frame(periods: int = 5, start: str = "2020-01-01") -> pd.DataFrame:
    """Distinct, non-degenerate synthetic OHLCV (a constant frame could hide a column mix-up)."""
    dates = pd.bdate_range(start, periods=periods)
    return pd.DataFrame(
        {
            "Open": [10.0 + i for i in range(periods)],
            "High": [12.0 + i for i in range(periods)],
            "Low": [9.0 + i for i in range(periods)],
            "Close": [11.0 + i for i in range(periods)],
            "Volume": [1000.0 + 10.0 * i for i in range(periods)],
        },
        index=dates,
    )


def test_store_bars_refuses_material_history_rewrite(disk_cache):
    """A 2% close move on already-stored dates is a basis shift, not an append (2026-10-23).

    Also proves the refusal writes NOTHING: the stored frame must be byte-identical after the
    failed call, so a caught-and-continued caller cannot end up with a half-rewritten span.
    """
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)
    before = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")

    rescaled = df.copy()
    rescaled["Close"] = rescaled["Close"] * 1.02

    with pytest.raises(HistoryRewriteRefused) as excinfo:
        _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", rescaled)

    message = str(excinfo.value)
    assert "5 stored row(s)" in message  # every row of the frame collides and differs
    assert "601225.SH" in message
    assert "close" in message  # names the offending column
    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), before, check_exact=True
    )


def test_store_bars_allows_idempotent_overlap(disk_cache):
    """A refetch of the identical frame is a re-derivation of the same basis: allowed silently."""
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)
    before = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")

    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)  # must not raise

    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), before, check_exact=True
    )


def test_store_bars_allows_ulp_level_rewobble(disk_cache):
    """A full re-derivation of the SAME basis wobbles by <=4e-16, ~7 orders below rtol=1e-9.

    This is why the gate is tolerance-based: refusing any value change at all would make every
    honest re-fetch of an overlapping window fail on double-rounding noise alone.
    """
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)

    wobbled = df.copy()
    wobbled["Close"] = wobbled["Close"] * (1 + 2e-16)
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", wobbled)  # must not raise

    stored = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")
    assert len(stored) == len(df)
    assert np.allclose(stored["Close"], df["Close"] * (1 + 2e-16), rtol=0, atol=1e-12)


def test_store_bars_allows_pure_append(disk_cache):
    """New later dates only: no collision, no gate, row count grows -- the normal daily path."""
    df = _gate_frame(periods=5)
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)

    tail = _gate_frame(periods=3, start="2020-01-08")
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", tail)  # must not raise

    stored = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")
    assert len(stored) == 8
    assert stored.index.max() == tail.index.max()


def test_store_bars_escape_hatch_allows_declared_rewrite(disk_cache, caplog):
    """A genuine, declared re-derivation may rewrite -- and must leave a WARNING trace with the
    caller's reason (the cache keeps no audit table, so the log is the only durable record)."""
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)

    rescaled = df.copy()
    rescaled["Close"] = rescaled["Close"] * 1.02
    with caplog.at_level(logging.WARNING, logger=duckdb_cache_module.__name__):
        _store_bars(disk_cache, 
            "FuyaoDataProvider",
            "601225.SH",
            rescaled,
            allow_history_rewrite=True,
            rewrite_reason="test: declared re-derivation",
        )

    stored = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")
    assert np.allclose(stored["Close"], rescaled["Close"], rtol=0, atol=1e-12)
    assert any(
        "HISTORY REWRITE" in rec.getMessage() and "test: declared re-derivation" in rec.getMessage()
        for rec in caplog.records
    ), "an allowed history rewrite must never be silent"


def test_store_bars_escape_hatch_requires_a_reason(disk_cache):
    """`allow_history_rewrite=True` without a non-blank reason is rejected before any SQL runs.

    Why fail closed (L2.32 review item 4): the cache keeps no audit table, so the reason carried
    into the WARNING is the ONLY durable record of a rewrite. A bare boolean reproduces exactly
    the 2026-10-23 shape -- 13,303 rows overwritten with nothing recording who/why -- so the flag
    without a reason must not reach the writer at all. `ValueError` (not
    `HistoryRewriteRefused`) because this is a caller error, not a cache-history verdict.
    """
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)
    before = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")

    rescaled = df.copy()
    rescaled["Close"] = rescaled["Close"] * 1.02

    for missing_reason in (None, "", "   \t\n "):
        with pytest.raises(ValueError, match="requires a non-blank rewrite_reason"):
            _store_bars(disk_cache, 
                "FuyaoDataProvider",
                "601225.SH",
                rescaled,
                allow_history_rewrite=True,
                rewrite_reason=missing_reason,
            )

    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), before, check_exact=True
    ), "the up-front guard must fire before anything is written"

    # The hatch is still open WITH a reason (the guard must not close it accidentally).
    _store_bars(disk_cache, 
        "FuyaoDataProvider",
        "601225.SH",
        rescaled,
        allow_history_rewrite=True,
        rewrite_reason="test: declared re-derivation after the guard",
    )
    assert np.allclose(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH")["Close"],
        rescaled["Close"],
        rtol=0,
        atol=1e-12,
    )


def test_store_bars_refuses_volume_only_history_rewrite(disk_cache):
    """A 1e-3 VOLUME-only change on stored dates is a rewrite too: the gate compares every column.

    Why this case: the 2026-10-23 incident happened to leave `volume` untouched, so a gate that
    only compared `close` would have looked correct while a volume-only (split/adjustment) rewrite
    slipped through. Prices identical, one column moved 1e-3 -- refused, and nothing written.
    """
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)
    before = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")

    volume_bumped = df.copy()
    volume_bumped["Volume"] = volume_bumped["Volume"] * 1.001

    with pytest.raises(HistoryRewriteRefused) as excinfo:
        _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", volume_bumped)

    message = str(excinfo.value)
    assert "601225.SH" in message
    assert "volume" in message, "the message must name the column that actually moved"
    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), before, check_exact=True
    )


def test_store_bars_refuses_incoming_nan_and_schema_forbids_one_sided_null(disk_cache):
    """An incoming NaN at a colliding date is a material difference; a stored NULL is impossible.

    The NULL branch of the gate (`one side NULL` -> difference) is reachable only from the
    INCOMING side: pandas NaN is handed to DuckDB through `register`, which converts it to SQL
    NULL (verified here -- the refusal names the NaN column). A *stored* NULL cannot exist at all
    because `ohlcv_bars` declares all five value columns NOT NULL (`_init_tables`), and
    `store_bars` is the only writer, so the genuine one-sided "stored NULL vs value" case is
    unreachable through the public API and is covered by the SQL construction only. The schema
    assertion below keeps that claim honest: if a column ever becomes nullable, this test fails
    and the real one-sided-NULL case must be written instead.
    """
    con = disk_cache._get_connection(read_only=True)
    try:
        info = con.execute("PRAGMA table_info('ohlcv_bars')").fetchall()
    finally:
        if not disk_cache.is_memory:
            con.close()
    not_null = {row[1]: bool(row[3]) for row in info}
    for col in ("open", "high", "low", "close", "volume"):
        assert not_null[col], (
            f"ohlcv_bars.{col} must stay NOT NULL -- otherwise the gate needs a real "
            f"stored-NULL-vs-value test, see this test's docstring"
        )

    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)
    before = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")

    with_nan = df.copy()
    with_nan.iloc[2, with_nan.columns.get_loc("Close")] = float("nan")

    with pytest.raises(HistoryRewriteRefused) as excinfo:
        _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", with_nan)

    message = str(excinfo.value)
    assert "1 stored row(s)" in message  # only the NaN date differs
    assert "close" in message
    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), before, check_exact=True
    )


def test_store_bars_tolerance_boundaries(disk_cache):
    """5e-10 relative passes, 2e-9 refuses. The EXACT 1e-9 boundary is deliberately NOT tested.

    Why not 1e-9: `price * (1 + 1e-9)` is not representable exactly, so the relative deviation
    the gate measures lands a few ULP either side of the threshold depending on the price and the
    arithmetic -- a test pinned to it would be flaky by construction. These two probes sit
    comfortably inside each regime (0.5x and 2x the tolerance) and are bit-stable.
    """
    df = _gate_frame()
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", df)

    below = df.copy()
    below["Close"] = below["Close"] * (1 + 5e-10)
    _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", below)  # must not raise
    stored_below = disk_cache.query_bars("FuyaoDataProvider", "601225.SH")
    assert np.allclose(stored_below["Close"], below["Close"], rtol=0, atol=1e-12)

    above = df.copy()
    above["Close"] = above["Close"] * (1 + 2e-9)
    with pytest.raises(HistoryRewriteRefused) as excinfo:
        _store_bars(disk_cache, "FuyaoDataProvider", "601225.SH", above)

    assert "close" in str(excinfo.value)
    # ...and the refused write left the *below*-tolerance state untouched.
    pd.testing.assert_frame_equal(
        disk_cache.query_bars("FuyaoDataProvider", "601225.SH"), stored_below, check_exact=True
    )


def test_m36_schema_and_migration(mem_cache):
    """M3.6: Verifies schema migration and table structure for adjust_factors and amount/adjustflag."""
    con = mem_cache._get_connection(read_only=True)
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    assert "adjust_factors" in tables
    assert "ohlcv_bars" in tables
    assert "intraday_ohlcv_bars" in tables

    # Verify adjust_factors columns and PK
    factors_info = con.execute("PRAGMA table_info('adjust_factors')").fetchall()
    f_cols = {row[1]: {"type": row[2], "pk": row[5]} for row in factors_info}
    assert f_cols["symbol"]["pk"] > 0
    assert f_cols["date"]["pk"] > 0
    assert "factor_hfq" in f_cols
    assert "source" in f_cols
    assert "last_sync_time" in f_cols

    # Verify amount and adjustflag presence
    bars_info = con.execute("PRAGMA table_info('ohlcv_bars')").fetchall()
    bars_cols = {row[1] for row in bars_info}
    assert "amount" in bars_cols

    intra_info = con.execute("PRAGMA table_info('intraday_ohlcv_bars')").fetchall()
    intra_cols = {row[1] for row in intra_info}
    assert "amount" in intra_cols
    assert "adjustflag" in intra_cols

    meta_info = con.execute("PRAGMA table_info('asset_sync_metadata')").fetchall()
    assert "adjustflag" in {row[1] for row in meta_info}

    intraday_meta = con.execute("PRAGMA table_info('intraday_sync_metadata')").fetchall()
    assert "adjustflag" in {row[1] for row in intraday_meta}

    # Verify idempotency of migration
    mem_cache._init_tables(con)


def test_m36_amount_storage_and_unscaled(mem_cache):
    """M3.6: Verifies amount column is stored when present, leaves NULL when absent, and is unscaled."""
    dates = pd.bdate_range("2024-01-01", periods=3)
    df_with_amt = pd.DataFrame({
        "Open": [10.0, 11.0, 12.0],
        "High": [10.5, 11.5, 12.5],
        "Low": [9.5, 10.5, 11.5],
        "Close": [10.0, 11.0, 12.0],
        "Volume": [100.0, 200.0, 300.0],
        "Amount": [1000.0, 2200.0, 3600.0],
    }, index=dates)

    mem_cache.store_bars("TestProv", "600000.SH", df_with_amt, adjustflag="raw")
    # M3.6 `columns=` contract: `Amount` is opt-in now, so ask for it explicitly.
    res = mem_cache.query_bars("TestProv", "600000.SH", adjust="none", columns="all")
    assert "Amount" in res.columns
    assert list(res["Amount"]) == [1000.0, 2200.0, 3600.0]

    # ...and it is absent by default.
    assert "Amount" not in mem_cache.query_bars("TestProv", "600000.SH", adjust="none").columns

    # Verify factor scaling scales OHLC but NEVER scales Volume or Amount
    factor_dates = list(dates) + [dates[-1] + pd.Timedelta(days=1)]
    factors_df = pd.DataFrame({
        "factor_hfq": [1.0, 1.0, 1.0, 2.0],
    }, index=factor_dates)
    mem_cache.store_adjust_factors("600000.SH", factors_df)

    res_qfq = mem_cache.query_bars("TestProv", "600000.SH", adjust="qfq", columns="all")
    assert list(res_qfq["Close"]) == [5.0, 5.5, 6.0]
    assert list(res_qfq["Volume"]) == [100.0, 200.0, 300.0]  # unscaled!
    assert list(res_qfq["Amount"]) == [1000.0, 2200.0, 3600.0]  # unscaled!


def test_m38_adjust_factors_storage_and_derivation_daily(mem_cache):
    """M3.8: Tests storing factors and dynamic in-memory derivation for daily bars."""
    dates = pd.bdate_range("2024-01-01", periods=4)
    raw_df = pd.DataFrame({
        "Open": [100.0, 102.0, 52.0, 54.0],
        "High": [105.0, 106.0, 55.0, 56.0],
        "Low": [98.0, 100.0, 50.0, 52.0],
        "Close": [101.0, 104.0, 53.0, 55.0],
        "Volume": [10000.0, 12000.0, 25000.0, 24000.0],
    }, index=dates)
    mem_cache.store_bars("TestProv", "000001.SZ", raw_df, adjustflag="raw")

    # Ex-dividend on 2024-01-03: 10-for-10 split -> historical prices halved under qfq
    factors_df = pd.DataFrame({
        "factor_qfq": [0.5, 0.5, 1.0, 1.0],
        "factor_hfq": [1.0, 1.0, 2.0, 2.0],
    }, index=dates)
    mem_cache.store_adjust_factors("000001.SZ", factors_df)

    # 1. adjust="none" returns raw bars
    q_none = mem_cache.query_bars("TestProv", "000001.SZ", adjust="none")
    assert q_none["Close"].iloc[0] == 101.0
    assert q_none["Close"].iloc[2] == 53.0

    # 2. adjust="qfq" scales by factor_qfq
    q_qfq = mem_cache.query_bars("TestProv", "000001.SZ", adjust="qfq")
    assert q_qfq["Close"].iloc[0] == 50.5
    assert q_qfq["Close"].iloc[1] == 52.0
    assert q_qfq["Close"].iloc[2] == 53.0
    assert q_qfq["Close"].iloc[3] == 55.0

    # 3. adjust="hfq" scales by factor_hfq
    q_hfq = mem_cache.query_bars("TestProv", "000001.SZ", adjust="hfq")
    assert q_hfq["Close"].iloc[0] == 101.0
    assert q_hfq["Close"].iloc[1] == 104.0
    assert q_hfq["Close"].iloc[2] == 106.0
    assert q_hfq["Close"].iloc[3] == 110.0


def test_m38_adjust_factors_storage_and_derivation_intraday_5m(mem_cache):
    """M3.8: Tests 5m intraday bars dynamic scaling matched to daily adjustment factor."""
    # Day 1: 2024-01-02 2 bars
    # Day 2: 2024-01-03 2 bars (ex-dividend day)
    ts_list = [
        pd.Timestamp("2024-01-02 09:35:00"),
        pd.Timestamp("2024-01-02 09:40:00"),
        pd.Timestamp("2024-01-03 09:35:00"),
        pd.Timestamp("2024-01-03 09:40:00"),
    ]
    df_5m = pd.DataFrame({
        "Open": [100.0, 101.0, 50.0, 51.0],
        "High": [102.0, 103.0, 52.0, 53.0],
        "Low": [99.0, 100.0, 49.0, 50.0],
        "Close": [101.0, 102.0, 51.0, 52.0],
        "Volume": [1000.0, 1200.0, 2000.0, 2200.0],
        "Amount": [101000.0, 122400.0, 102000.0, 114400.0],
    }, index=ts_list)

    mem_cache.store_intraday_bars("TestProv", "000002.SZ", df_5m, interval="5m", adjustflag="3")

    factors = pd.DataFrame({
        "factor_qfq": [0.5, 1.0],
        "factor_hfq": [1.0, 2.0],
    }, index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    mem_cache.store_adjust_factors("000002.SZ", factors)

    # Query with adjustflag="qfq" (M3.6 `columns=` contract: `Amount` is opt-in)
    derived_5m = mem_cache.query_intraday_bars(
        "TestProv", "000002.SZ", interval="5m", adjustflag="qfq", columns="all"
    )
    # Day 1 scaled by 0.5
    assert derived_5m.loc[pd.Timestamp("2024-01-02 09:35:00"), "Close"] == 50.5
    assert derived_5m.loc[pd.Timestamp("2024-01-02 09:40:00"), "Close"] == 51.0
    # Day 2 scaled by 1.0
    assert derived_5m.loc[pd.Timestamp("2024-01-03 09:35:00"), "Close"] == 51.0
    assert derived_5m.loc[pd.Timestamp("2024-01-03 09:40:00"), "Close"] == 52.0

    # Volume and Amount are completely untouched
    assert list(derived_5m["Volume"]) == [1000.0, 1200.0, 2000.0, 2200.0]
    assert list(derived_5m["Amount"]) == [101000.0, 122400.0, 102000.0, 114400.0]


def test_m38_edge_assets_neutral_factor(mem_cache):
    """M3.8: Tests that edge assets 511880.SH and 000300.SH have neutral factor 1.0."""
    from common.financial_api import MarketDBDataProvider
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
        );
        CREATE VIEW v_daily_qfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;
    """)
    for i in range(3):
        d_str = f"2024-01-0{i+1}"
        con.execute("INSERT INTO raw_kline_daily VALUES ('511880.SH', ?, 100.0, 100.1, 99.9, 100.0, 1000)", [d_str])
        con.execute("INSERT INTO raw_kline_daily VALUES ('000300.SH', ?, 3500.0, 3550.0, 3480.0, 3520.0, 50000)", [d_str])

    provider = MarketDBDataProvider(con=con)
    f_cash = provider.fetch_adjust_factors("511880.SH", start="2024-01-01", end="2024-01-03")
    assert not f_cash.empty
    assert (f_cash["factor_qfq"] == 1.0).all()
    assert (f_cash["factor_hfq"] == 1.0).all()

    f_idx = provider.fetch_adjust_factors("000300.SH", start="2024-01-01", end="2024-01-03")
    assert not f_idx.empty
    assert (f_idx["factor_qfq"] == 1.0).all()
    assert (f_idx["factor_hfq"] == 1.0).all()
    con.close()


# =============================================================================
# M3.6 review F1 / F2 / F6: undeclared-basis refusal, transition gate,
# and the `columns=` contract.
# =============================================================================


def _force_legacy_null_basis(cache, provider: str, symbol: str, interval=None) -> None:
    """Simulates a pre-M3.6 row: the metadata column exists but was never backfilled.

    SQL NULL (not the `"unknown"` sentinel) is the state `UnknownAdjustBasisError` refuses --
    `store_bars(..., adjustflag=None)` deliberately writes the sentinel instead, so the only way
    to reach the legacy state in a test is to stamp NULL directly.
    """
    if interval:
        table = "intraday_sync_metadata"
        where = "provider = ? AND symbol = ? AND interval = ?"
        params = [provider, symbol, interval]
    else:
        table = "asset_sync_metadata"
        where = "provider = ? AND symbol = ?"
        params = [provider, symbol]
    con = cache._get_connection(read_only=False)
    con.execute(f"UPDATE {table} SET adjustflag = NULL WHERE {where}", params)
    row = con.execute(f"SELECT adjustflag FROM {table} WHERE {where}", params).fetchone()
    assert row is not None, "test setup: metadata row missing"
    assert row[0] is None, "test setup: basis was not actually nulled"


def test_f1_legacy_null_basis_refused_and_opt_in_warns(mem_cache):
    """DISCRIMINATING (F1): a pre-M3.6 row (basis NULL) must FAIL CLOSED, not pass through.

    Before F1 the read answered `stored_basis is None -> return as-is`, so a QFQ cache served
    QFQ bytes to a `raw` request with no warning (the 2026-10-23 incident shape). The only
    sanctioned escapes are `allow_legacy_basis=True` (warned, explicit) or a human declaration
    via `scripts/migrate_adjust_basis.py`.
    """
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=dates,
    )
    mem_cache.store_bars("F1LegacyProv", "600519.F1", df, adjustflag="raw")
    _force_legacy_null_basis(mem_cache, "F1LegacyProv", "600519.F1")

    # Both a deriving request and a "just give me raw" request must refuse.
    with pytest.raises(UnknownAdjustBasisError) as excinfo:
        mem_cache.query_bars("F1LegacyProv", "600519.F1", adjust="raw")
    assert "600519.F1" in str(excinfo.value)
    assert "migrate_adjust_basis.py" in str(excinfo.value)
    assert "asset_sync_metadata" in str(excinfo.value)

    with pytest.raises(UnknownAdjustBasisError):
        mem_cache.query_bars("F1LegacyProv", "600519.F1", adjust="qfq")

    # The 5m read path goes through the same three-state resolution.
    mem_cache.store_intraday_bars(
        "F1LegacyProv",
        "600519.F1",
        pd.DataFrame(
            {
                "Open": [10.0, 10.1],
                "High": [10.2, 10.3],
                "Low": [9.9, 10.0],
                "Close": [10.1, 10.2],
                "Volume": [100.0, 110.0],
            },
            index=pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"]),
        ),
        interval="5m",
        adjustflag="raw",
    )
    _force_legacy_null_basis(mem_cache, "F1LegacyProv", "600519.F1", interval="5m")
    with pytest.raises(UnknownAdjustBasisError):
        mem_cache.query_intraday_bars(
            "F1LegacyProv", "600519.F1", interval="5m", adjustflag="qfq"
        )

    # The explicit opt-in returns the stored series UNCHANGED and says so out loud (once).
    with pytest.warns(UserWarning, match="allow_legacy_basis pass-through"):
        legacy = mem_cache.query_bars(
            "F1LegacyProv", "600519.F1", adjust="qfq", allow_legacy_basis=True
        )
    np.testing.assert_array_equal(legacy["Close"].values, df["Close"].values)

    # Warn-once: the second explicit opt-in is silent (a universe read must not spam).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mem_cache.query_bars(
            "F1LegacyProv", "600519.F1", adjust="qfq", allow_legacy_basis=True
        )
    assert [w for w in caught if "allow_legacy_basis" in str(w.message)] == []


def test_f1_factor_neutral_symbol_exempt_from_null_refusal(mem_cache):
    """F1 exemption: indices / 511880 are factor-neutral, so an unknown basis changes no price."""
    dates = pd.bdate_range("2024-01-01", periods=2)
    df = pd.DataFrame(
        {
            "Open": [3500.0, 3510.0],
            "High": [3550.0, 3560.0],
            "Low": [3480.0, 3490.0],
            "Close": [3520.0, 3530.0],
            "Volume": [50000.0, 51000.0],
        },
        index=dates,
    )
    mem_cache.store_bars("F1NeutralProv", "000300.SH", df, adjustflag="raw")
    _force_legacy_null_basis(mem_cache, "F1NeutralProv", "000300.SH")

    out = mem_cache.query_bars("F1NeutralProv", "000300.SH", adjust="qfq")
    np.testing.assert_array_equal(out["Close"].values, df["Close"].values)


def test_f2_basis_transition_refused_on_legacy_rows(mem_cache):
    """DISCRIMINATING (F2): a write must not stamp a basis onto pre-M3.6 (NULL) rows.

    `COALESCE(excluded.adjustflag, existing.adjustflag)` used to let the next raw sync relabel
    legacy QFQ bytes as `raw`, after which every read multiplied them by the factor again
    (double adjustment). The label is a claim about bytes already on disk.
    """
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=dates,
    )
    mem_cache.store_bars("F2Prov", "600519.F2", df, adjustflag="raw")
    _force_legacy_null_basis(mem_cache, "F2Prov", "600519.F2")

    # An UNDECLARED write is not a claim, so it is allowed -- WITH the explicit opt-in required
    # since P1 (`adjustflag=None` alone now raises `AdjustBasisRequiredError`) -- and it must NOT
    # launder the NULL into the `"unknown"` sentinel (that would erase the legacy-migration
    # signal; the upsert's COALESCE keeps the pre-existing NULL).
    mem_cache.store_bars(
        "F2Prov", "600519.F2", df, adjustflag=None, allow_undeclared_basis=True
    )
    meta = mem_cache.get_asset_metadata("F2Prov", "600519.F2")
    assert meta["adjustflag"] is None

    # A DECLARED write is refused, and the refusal is atomic (metadata untouched).
    with pytest.raises(AdjustBasisTransitionRefused) as excinfo:
        mem_cache.store_bars("F2Prov", "600519.F2", df, adjustflag="raw")
    assert "adjustflag is NULL" in str(excinfo.value)
    assert "allow_basis_transition=True" in str(excinfo.value)
    assert mem_cache.get_asset_metadata("F2Prov", "600519.F2")["adjustflag"] is None

    # Audited escape hatch: an explicit, deliberate declaration goes through.
    mem_cache.store_bars("F2Prov", "600519.F2", df, adjustflag="raw", allow_basis_transition=True)
    assert mem_cache.get_asset_metadata("F2Prov", "600519.F2")["adjustflag"] == "raw"

    # Once declared, ordinary appends are no longer gated.
    mem_cache.store_bars(
        "F2Prov",
        "600519.F2",
        df.iloc[[2]].rename(index=lambda t: t + pd.Timedelta(days=1)),
        adjustflag="raw",
    )


def test_f2_intraday_basis_transition_refused(mem_cache):
    """F2 covers `intraday_sync_metadata` too (same COALESCE bug, same consequence)."""
    ts = pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"])
    df = pd.DataFrame(
        {
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.1, 10.2],
            "Volume": [100.0, 110.0],
        },
        index=ts,
    )
    mem_cache.store_intraday_bars("F2IntradayProv", "600519.F2I", df, interval="5m", adjustflag="raw")
    _force_legacy_null_basis(mem_cache, "F2IntradayProv", "600519.F2I", interval="5m")

    with pytest.raises(AdjustBasisTransitionRefused):
        mem_cache.store_intraday_bars(
            "F2IntradayProv", "600519.F2I", df, interval="5m", adjustflag="qfq"
        )
    assert mem_cache.get_intraday_metadata("F2IntradayProv", "600519.F2I", "5m")["adjustflag"] is None

    mem_cache.store_intraday_bars(
        "F2IntradayProv",
        "600519.F2I",
        df,
        interval="5m",
        adjustflag="qfq",
        allow_basis_transition=True,
    )
    assert (
        mem_cache.get_intraday_metadata("F2IntradayProv", "600519.F2I", "5m")["adjustflag"] == "qfq"
    )


def test_f6_columns_contract_daily_and_intraday(mem_cache):
    """DISCRIMINATING (F6): the output shape must not depend on whether `amount` is on disk."""
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
            "Amount": [100000.0, 110000.0, 120000.0],
        },
        index=dates,
    )
    mem_cache.store_bars("F6Prov", "600519.F6", df, adjustflag="raw")

    default_cols = ["Open", "High", "Low", "Close", "Volume"]
    # Default: exactly the required OHLCV columns, no `Amount`, even though it IS on disk.
    assert list(mem_cache.query_bars("F6Prov", "600519.F6", adjust="raw").columns) == default_cols
    # "all" adds it back.
    assert list(mem_cache.query_bars("F6Prov", "600519.F6", adjust="raw", columns="all").columns) == (
        default_cols + ["Amount"]
    )
    # An explicit sequence returns EXACTLY that, in that order.
    explicit = mem_cache.query_bars(
        "F6Prov", "600519.F6", adjust="raw", columns=["Close", "Amount"]
    )
    assert list(explicit.columns) == ["Close", "Amount"]
    np.testing.assert_array_equal(explicit["Amount"].values, df["Amount"].values)
    # A single-name string is accepted too.
    assert list(mem_cache.query_bars("F6Prov", "600519.F6", adjust="raw", columns="Volume").columns) == [
        "Volume"
    ]
    # An unavailable column fails loudly instead of silently returning something else.
    with pytest.raises(ValueError, match="not available"):
        mem_cache.query_bars("F6Prov", "600519.F6", adjust="raw", columns=["NoSuchColumn"])

    # Empty result keeps the requested shape (no schema flip on an empty range).
    empty = mem_cache.query_bars(
        "F6Prov", "600519.F6", start="2030-01-01", end="2030-01-02", adjust="raw"
    )
    assert list(empty.columns) == default_cols and empty.empty
    empty_all = mem_cache.query_bars(
        "F6Prov", "600519.F6", start="2030-01-01", end="2030-01-02", adjust="raw", columns="all"
    )
    assert list(empty_all.columns) == default_cols + ["Amount"] and empty_all.empty

    # Intraday twin: same contract, and `Amount` really is opt-in there as well.
    ts = pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"])
    df_5m = pd.DataFrame(
        {
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.1, 10.2],
            "Volume": [100.0, 110.0],
            "Amount": [1010.0, 1111.0],
        },
        index=ts,
    )
    mem_cache.store_intraday_bars("F6Prov", "600519.F6", df_5m, interval="5m", adjustflag="raw")
    assert (
        list(
            mem_cache.query_intraday_bars(
                "F6Prov", "600519.F6", interval="5m", adjustflag="raw"
            ).columns
        )
        == default_cols
    )
    intraday_all = mem_cache.query_intraday_bars(
        "F6Prov", "600519.F6", interval="5m", adjustflag="raw", columns="all"
    )
    assert list(intraday_all.columns) == default_cols + ["Amount"]
    np.testing.assert_array_equal(intraday_all["Amount"].values, df_5m["Amount"].values)


def test_f1_migration_script_dry_run_then_declare(tmp_path, capsys):
    """DISCRIMINATING (F1): the migration tool is read-only by default and rewrites no price.

    `--dry-run` must list the offending (provider, symbol) and change nothing; `--set-basis`
    must make the symbol readable again WITHOUT touching a single OHLCV value.
    """
    from scripts import migrate_adjust_basis

    db_path = tmp_path / "migrate.duckdb"
    cache = DuckDBCache(db_path=str(db_path))
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=dates,
    )
    cache.store_bars("MigrateProv", "600519.MIG", df, adjustflag="raw")
    _force_legacy_null_basis(cache, "MigrateProv", "600519.MIG")
    # Adjustment factors must already be on disk for a `raw` declaration to be legal (item-5
    # guard: `raw` + no factors makes every qfq/hfq read fail closed, so it needs --force).
    cache.store_adjust_factors(
        "600519.MIG",
        pd.DataFrame({"factor_hfq": [1.0, 1.0, 1.0]}, index=dates),
        source="MigrateProv",
    )

    # 1. Default invocation == dry run: lists the entry, writes nothing.
    assert migrate_adjust_basis.main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "600519.MIG" in out
    assert "legacy_null" in out
    assert "READ-ONLY" in out
    assert cache.get_asset_metadata("MigrateProv", "600519.MIG")["adjustflag"] is None
    with pytest.raises(UnknownAdjustBasisError):
        cache.query_bars("MigrateProv", "600519.MIG", adjust="raw")

    # 2. Declare: the only write this tool performs is the metadata label.
    assert (
        migrate_adjust_basis.main(
            [
                "--db",
                str(db_path),
                "--set-basis",
                "raw",
                "--provider",
                "MigrateProv",
                "--symbol",
                "600519.MIG",
            ]
        )
        == 0
    )
    declared_out = capsys.readouterr().out
    assert "No prices changed" in declared_out
    assert cache.get_asset_metadata("MigrateProv", "600519.MIG")["adjustflag"] == "raw"

    # 3. Prices are byte-identical, and the read no longer refuses.
    out_df = cache.query_bars("MigrateProv", "600519.MIG", adjust="raw")
    np.testing.assert_array_equal(out_df["Close"].values, df["Close"].values)
    np.testing.assert_array_equal(out_df["Volume"].values, df["Volume"].values)

    # 4. A re-scan finds nothing left to migrate.
    assert migrate_adjust_basis.main(["--db", str(db_path), "--dry-run"]) == 0
    assert "Nothing to migrate" in capsys.readouterr().out


def test_f1_migration_script_never_invents_a_metadata_row(tmp_path):
    """The tool only LABELS an existing row; a missing row is the sync's job, not the audit's."""
    from scripts import migrate_adjust_basis

    db_path = tmp_path / "missing.duckdb"
    cache = DuckDBCache(db_path=str(db_path))
    cache.store_bars(
        "MigrateProv",
        "600519.NOROW",
        _make_sample_ohlcv("2024-01-01", periods=2),
        adjustflag="raw",
    )
    con = cache._get_connection(read_only=False)
    try:
        con.execute(
            "DELETE FROM asset_sync_metadata WHERE provider = 'MigrateProv' "
            "AND symbol = '600519.NOROW'"
        )
    finally:
        con.close()

    with pytest.raises(KeyError, match="only LABELS an existing row"):
        migrate_adjust_basis.main(
            [
                "--db",
                str(db_path),
                "--set-basis",
                "raw",
                "--provider",
                "MigrateProv",
                "--symbol",
                "600519.NOROW",
            ]
        )


# ---------------------------------------------------------------------------
# M3.6 second review pass (P1 sentinel write gate, P2 migration guards,
# universe-read branch, `next_vwap` under the `columns` default)
# ---------------------------------------------------------------------------


def test_p1_undeclared_write_requires_opt_in_and_reads_fail_closed(mem_cache):
    """DISCRIMINATING (P1): `adjustflag=None` is not a silent default any more.

    The first revision recorded the `"unknown"` sentinel and then READ IT BACK SILENTLY -- no
    `allow_legacy_basis`, no transition gate -- so `rebase_corporate_action` and the CSV migration
    produced rows where `adjust="raw"` returned possibly-QFQ bytes and `adjust="qfq"` returned
    possibly-raw bytes. That is the F1/F2 hazard moved onto fresh rows. The write is now
    fail-closed unless the caller says out loud that it cannot state the basis, and an undeclared
    row is then read exactly like a pre-M3.6 NULL row.
    """
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=dates,
    )

    # 1. No declaration -> the write refuses, and nothing reaches the cache.
    with pytest.raises(AdjustBasisRequiredError) as excinfo:
        mem_cache.store_bars("P1SentinelProv", "600519.P1S", df)
    assert "adjustflag" in str(excinfo.value)
    assert "allow_undeclared_basis=True" in str(excinfo.value)
    assert mem_cache.get_asset_metadata("P1SentinelProv", "600519.P1S") is None

    # The intraday writer shares the gate.
    with pytest.raises(AdjustBasisRequiredError):
        mem_cache.store_intraday_bars(
            "P1SentinelProv",
            "600519.P1S",
            pd.DataFrame(
                {
                    "Open": [10.0, 10.1],
                    "High": [10.2, 10.3],
                    "Low": [9.9, 10.0],
                    "Close": [10.1, 10.2],
                    "Volume": [100.0, 110.0],
                },
                index=pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"]),
            ),
            interval="5m",
        )

    # 2. Factor-neutral symbols stay exempt: the label cannot change a single returned price.
    mem_cache.store_bars("P1SentinelProv", "000300.SH", df)
    assert mem_cache.get_asset_metadata("P1SentinelProv", "000300.SH")["adjustflag"] == "unknown"
    assert len(mem_cache.query_bars("P1SentinelProv", "000300.SH", adjust="qfq")) == 3

    # 3. Explicit opt-in records the sentinel, NOT SQL NULL (NULL stays reserved for pre-M3.6).
    mem_cache.store_bars(
        "P1SentinelProv", "600519.P1S", df, adjustflag=None, allow_undeclared_basis=True
    )
    assert mem_cache.get_asset_metadata("P1SentinelProv", "600519.P1S")["adjustflag"] == "unknown"

    # 4. Both un-declared states are one state for the reader: refused without the opt-in...
    for mode in ("qfq", "hfq", "raw", "none"):
        with pytest.raises(UnknownAdjustBasisError):
            mem_cache.query_bars("P1SentinelProv", "600519.P1S", adjust=mode)
    # ...and the SAME opt-in as a legacy NULL row when it is given, unchanged + warned (once).
    with pytest.warns(UserWarning, match="allow_legacy_basis pass-through") as caught:
        out = mem_cache.query_bars(
            "P1SentinelProv", "600519.P1S", adjust="qfq", allow_legacy_basis=True
        )
    assert "undeclared-basis sentinel" in str(caught[0].message)
    np.testing.assert_array_equal(out["Close"].values, df["Close"].values)
    with warnings.catch_warnings(record=True) as later:
        warnings.simplefilter("always")
        mem_cache.query_bars(
            "P1SentinelProv", "600519.P1S", adjust="qfq", allow_legacy_basis=True
        )
    assert [w for w in later if "allow_legacy_basis" in str(w.message)] == []


def test_p1_sentinel_to_declared_basis_is_gated(mem_cache):
    """DISCRIMINATING (P1/F2): the sentinel is now a REFUSED state, so it must also be gated.

    The first revision exempted the sentinel from `_guard_basis_transition` on the argument that
    such a row was readable pass-through anyway. Once the read refuses it, a later label becomes
    exactly as unrecoverable as on a NULL row, so both are gated and both take the audited
    `allow_basis_transition=True` escape hatch.
    """
    dates = pd.bdate_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=dates,
    )
    mem_cache.store_bars(
        "P1SentinelProv", "600519.P1T", df, adjustflag=None, allow_undeclared_basis=True
    )
    assert mem_cache.get_asset_metadata("P1SentinelProv", "600519.P1T")["adjustflag"] == "unknown"

    with pytest.raises(AdjustBasisTransitionRefused) as excinfo:
        mem_cache.store_bars("P1SentinelProv", "600519.P1T", df, adjustflag="qfq")
    assert "allow_basis_transition=True" in str(excinfo.value)
    # Atomic: the label is still undeclared.
    assert mem_cache.get_asset_metadata("P1SentinelProv", "600519.P1T")["adjustflag"] == "unknown"

    mem_cache.store_bars(
        "P1SentinelProv", "600519.P1T", df, adjustflag="qfq", allow_basis_transition=True
    )
    assert mem_cache.get_asset_metadata("P1SentinelProv", "600519.P1T")["adjustflag"] == "qfq"
    # Declared -> readable with no opt-in at all.
    assert len(mem_cache.query_bars("P1SentinelProv", "600519.P1T", adjust="qfq")) == 3

    # Intraday twin: same sentinel, same gate, same escape hatch.
    frame_5m = pd.DataFrame(
        {
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.1, 10.2],
            "Volume": [100.0, 110.0],
        },
        index=pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"]),
    )
    mem_cache.store_intraday_bars(
        "P1SentinelProv",
        "600519.P1TI",
        frame_5m,
        interval="5m",
        adjustflag=None,
        allow_undeclared_basis=True,
    )
    with pytest.raises(AdjustBasisTransitionRefused):
        mem_cache.store_intraday_bars(
            "P1SentinelProv", "600519.P1TI", frame_5m, interval="5m", adjustflag="qfq"
        )
    mem_cache.store_intraday_bars(
        "P1SentinelProv",
        "600519.P1TI",
        frame_5m,
        interval="5m",
        adjustflag="qfq",
        allow_basis_transition=True,
    )
    assert (
        mem_cache.get_intraday_metadata("P1SentinelProv", "600519.P1TI", "5m")["adjustflag"]
        == "qfq"
    )


class _BasisSilentProvider(BaseDataProvider):
    """Provider that declares no basis at all (the legacy `fetch_ohlcv`-only shape)."""

    __name__ = "BasisSilentProvider"

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        dates = pd.bdate_range(start, end)
        n = len(dates)
        return pd.DataFrame(
            {
                "Open": [400.0] * n,
                "High": [405.0] * n,
                "Low": [395.0] * n,
                "Close": [400.0] * n,
                "Volume": [1000.0] * n,
            },
            index=dates,
        )


class _RawNativeProvider(_BasisSilentProvider):
    """Provider whose own series IS unadjusted (`native_adjust_basis="raw"`)."""

    __name__ = "RawNativeProvider"
    native_adjust_basis = "raw"


def test_p1_rebase_corporate_action_declares_a_real_basis(tmp_path):
    """P1 production path 1: `rebase_corporate_action` must LABEL what it writes.

    It fetches through the inner provider's own `fetch_ohlcv`, so the truthful label is that
    provider's native basis; a provider that declares nothing gets the documented `qfq` default
    (the pre-M3.6 read default), logged. Nothing is left undeclared -- an undeclared rewrite would
    make the method's own return value unreadable.
    """
    import logging

    from common.data import CachedDataProvider

    # (a) Silent provider -> documented qfq assumption, and the label is a DECLARATION.
    silent = CachedDataProvider(_BasisSilentProvider(), cache_dir=str(tmp_path / "silent.duckdb"))
    silent.rebase_corporate_action(
        "600519.REB", start="2024-01-01", end="2024-01-05", reason="4-for-1 split rebase"
    )
    meta = silent.duckdb_cache.get_asset_metadata("_BasisSilentProvider", "600519.REB")
    assert meta["adjustflag"] == "qfq", "P1: the rebase left the basis undeclared"
    # A declared basis is fail-closed for another one instead of silently serving these bytes.
    with pytest.raises(ValueError, match="stored data has basis 'qfq'"):
        silent.duckdb_cache.query_bars("_BasisSilentProvider", "600519.REB", adjust="raw")

    # (b) A provider that says "raw" is stored as raw (never laundered into qfq).
    raw = CachedDataProvider(_RawNativeProvider(), cache_dir=str(tmp_path / "raw.duckdb"))
    out = raw.rebase_corporate_action(
        "600519.REB", start="2024-01-01", end="2024-01-05", reason="4-for-1 split rebase"
    )
    assert (
        raw.duckdb_cache.get_asset_metadata("_RawNativeProvider", "600519.REB")["adjustflag"]
        == "raw"
    )
    # ...and the method returns the declared series (a qfq read would need factors it never
    # stores, so returning qfq here would fail on a write that had already succeeded).
    np.testing.assert_array_equal(out["Close"].values, [400.0] * len(out))
    assert raw.duckdb_cache.query_adjust_factors("600519.REB").empty


def test_p1_csv_migration_declares_qfq_not_sentinel(tmp_path):
    """P1 production path 2: `migrate_cache_csv_to_duckdb` declares the legacy cache's basis."""
    from scripts.migrate_cache_csv_to_duckdb import migrate_csv_cache

    data_dir = tmp_path / "csv_data"
    data_dir.mkdir()
    dates = pd.bdate_range("2020-01-01", periods=5)
    pd.DataFrame(
        {
            "Open": [100.0] * 5,
            "High": [105.0] * 5,
            "Low": [95.0] * 5,
            "Close": [102.0] * 5,
            "Volume": [1000.0] * 5,
        },
        index=dates,
    ).to_csv(data_dir / "YFinanceDataProvider_AAPL_1d_2020-01-01_2020-01-07.csv")

    db_file = tmp_path / "migrated.duckdb"
    migrate_csv_cache(data_dir=str(data_dir), db_path=str(db_file))

    cache = DuckDBCache(db_path=str(db_file))
    meta = cache.get_asset_metadata("YFinanceDataProvider", "AAPL")
    assert meta["adjustflag"] == "qfq", "P1: the CSV migration left the basis undeclared"
    # Readable without any legacy opt-in, and fail-closed for a basis it is not.
    assert len(cache.query_bars("YFinanceDataProvider", "AAPL", adjust="qfq")) == 5
    with pytest.raises(ValueError, match="stored data has basis 'qfq'"):
        cache.query_bars("YFinanceDataProvider", "AAPL", adjust="hfq")


def test_p2_migration_declare_guards(tmp_path, capsys):
    """P2 (item 5): the declaration tool refuses both claims that reinterpret stored prices.

    a) `--set-basis raw` with no `adjust_factors` rows for the symbol -> every qfq/hfq read of it
       would fail closed, which is a legitimate final state but almost always a migration mistake;
    b) overwriting an ALREADY-DECLARED basis -> silently changes the meaning of the history.
    Both need `--force`, and `--force` prints what it just bypassed. A dry run stays read-only.
    """
    from scripts import migrate_adjust_basis

    db_path = tmp_path / "guards.duckdb"
    cache = DuckDBCache(db_path=str(db_path))
    symbol = "600519.GRD"
    cache.store_bars(
        "GuardProv", symbol, _make_sample_ohlcv("2024-01-01", periods=3), adjustflag="qfq"
    )

    def _declare(basis, *extra):
        return migrate_adjust_basis.main(
            ["--db", str(db_path), "--set-basis", basis, "--provider", "GuardProv",
             "--symbol", symbol, *extra]
        )

    # (a) raw without factors -> refused, metadata untouched.
    with pytest.raises(RuntimeError, match="--force"):
        _declare("raw")
    assert cache.get_asset_metadata("GuardProv", symbol)["adjustflag"] == "qfq"

    # (b) overwriting the declared basis -> refused, metadata untouched.
    with pytest.raises(RuntimeError, match="ALREADY-DECLARED basis"):
        _declare("hfq")
    assert cache.get_asset_metadata("GuardProv", symbol)["adjustflag"] == "qfq"

    # Re-declaring the SAME value is an idempotent no-op, not a "flip" that demands --force.
    assert _declare("qfq") == 0
    assert cache.get_asset_metadata("GuardProv", symbol)["adjustflag"] == "qfq"

    # (c) --force bypasses, says so loudly, and actually writes.
    assert _declare("raw", "--force") == 0
    out = capsys.readouterr().out
    assert "WARNING: --force" in out
    assert "FAIL CLOSED" in out
    assert cache.get_asset_metadata("GuardProv", symbol)["adjustflag"] == "raw"

    # (d) dry run is still read-only: no metadata write, no price change.
    before = cache.get_asset_metadata("GuardProv", symbol)["adjustflag"]
    assert migrate_adjust_basis.main(["--db", str(db_path), "--dry-run"]) == 0
    assert cache.get_asset_metadata("GuardProv", symbol)["adjustflag"] == before


def test_f1_query_universe_legacy_null_branch(mem_cache):
    """F1 coverage extension: the UNIVERSE readers hit the same three-state resolution."""
    dates = pd.bdate_range("2024-01-01", periods=2)
    df = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [101.0, 102.0],
            "Low": [99.0, 100.0],
            "Close": [100.5, 101.5],
            "Volume": [1000.0, 1100.0],
        },
        index=dates,
    )
    mem_cache.store_bars("F1UniProv", "600519.U1", df, adjustflag="qfq")
    mem_cache.store_bars("F1UniProv", "600519.U2", df, adjustflag="qfq")
    _force_legacy_null_basis(mem_cache, "F1UniProv", "600519.U2")

    # Daily: one legacy-null symbol in the batch fails the whole read (no silent smaller frame).
    with pytest.raises(UnknownAdjustBasisError) as excinfo:
        mem_cache.query_universe("F1UniProv", ["600519.U1", "600519.U2"], adjust="qfq")
    assert "600519.U2" in str(excinfo.value)
    uni = mem_cache.query_universe(
        "F1UniProv", ["600519.U1", "600519.U2"], adjust="qfq", allow_legacy_basis=True
    )
    assert set(uni) == {"600519.U1", "600519.U2"}
    np.testing.assert_array_equal(uni["600519.U2"]["Close"].values, df["Close"].values)

    # Intraday twin.
    frame_5m = pd.DataFrame(
        {
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.1, 10.2],
            "Volume": [100.0, 110.0],
        },
        index=pd.to_datetime(["2024-01-02 09:35:00", "2024-01-02 09:40:00"]),
    )
    for sym in ("600519.U1", "600519.U2"):
        mem_cache.store_intraday_bars(
            "F1UniProv", sym, frame_5m, interval="5m", adjustflag="qfq"
        )
    _force_legacy_null_basis(mem_cache, "F1UniProv", "600519.U2", interval="5m")
    with pytest.raises(UnknownAdjustBasisError):
        mem_cache.query_intraday_universe(
            "F1UniProv", ["600519.U1", "600519.U2"], interval="5m", adjustflag="qfq"
        )
    uni_5m = mem_cache.query_intraday_universe(
        "F1UniProv",
        ["600519.U1", "600519.U2"],
        interval="5m",
        adjustflag="qfq",
        allow_legacy_basis=True,
    )
    assert set(uni_5m) == {"600519.U1", "600519.U2"}


class _AmountEmittingProvider(BaseDataProvider):
    """Legacy-shaped provider (`fetch_ohlcv` only) that also reports turnover."""

    __name__ = "AmountEmittingProvider"
    native_adjust_basis = "qfq"

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        dates = pd.bdate_range(start, end)
        n = len(dates)
        close = [10.0 + 0.1 * i for i in range(n)]
        volume = [1000.0] * n
        return pd.DataFrame(
            {
                "Open": close,
                "High": [c + 0.2 for c in close],
                "Low": [c - 0.2 for c in close],
                "Close": close,
                "Volume": volume,
                "Amount": [c * v for c, v in zip(close, volume)],
            },
            index=dates,
        )


def test_p2_next_vwap_works_through_load_universe_default_columns(tmp_path):
    """P2 (item 3) POSITIVE CONTROL: `next_vwap` must survive the narrower cache default.

    `query_bars` returns required OHLCV only by default (no `Amount`), so `execution_price=
    "next_vwap"` -- which needs `VWAP` or `Amount`+`Volume` on every symbol -- became an
    unconditional `ValueError` for every cache-backed backtest. The dependency is satisfied by
    `load_universe` requesting `columns="all"`; this test drives the real loader, then the real
    engine, and checks the fill price is actually the turnover / volume VWAP.
    """
    from common.allocation_backtester import run_allocation_backtest
    from common.data import load_universe

    universe = load_universe(
        ["600000.SH"],
        "2024-01-01",
        "2024-01-10",
        use_cache=True,
        cache_dir=str(tmp_path / "vwap.duckdb"),
        provider=_AmountEmittingProvider(),
    )
    loaded = universe["600000.SH"]
    assert "Amount" in loaded.columns, "load_universe dropped the VWAP ingredient"

    idx = loaded.index
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights, execution_price="next_vwap")
    reb = res["rebalance_report"]
    assert len(reb) == 1
    expected = loaded.loc[idx[1], "Amount"] / loaded.loc[idx[1], "Volume"]
    np.testing.assert_allclose(float(reb.iloc[0]["price"]), expected, rtol=1e-9)

    # Control: the strict OHLCV-only shape is still available on demand and still fails loud
    # (so the fix is the loader asking for `Amount`, not the engine inventing a price).
    strict = load_universe(
        ["600000.SH"],
        "2024-01-01",
        "2024-01-10",
        use_cache=True,
        cache_dir=str(tmp_path / "vwap.duckdb"),
        provider=_AmountEmittingProvider(),
        columns=None,
    )
    assert "Amount" not in strict["600000.SH"].columns
    with pytest.raises(ValueError, match="next_vwap"):
        run_allocation_backtest(strict, target_weights, execution_price="next_vwap")

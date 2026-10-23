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
    is_intraday_interval,
    _get_resample_rule,
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


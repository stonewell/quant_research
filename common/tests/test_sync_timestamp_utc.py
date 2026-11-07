"""`last_sync_time` is stored as **naive UTC** (development_plan `LV19` item 2).

THE DEFECT
----------
Both writers stamped the column with `datetime.datetime.now()` -- the HOST's wall clock with the
offset thrown away. On the live station (UTC+08:00) a row written at `04:23:28` UTC was stored as
`20:23:28`, so the 2026-11-05 audit read a same-run write as "the previous evening": a 16-hour
skew between the stored number and the market clock, in the one field an operator uses to decide
whether a bar was written by THIS run.

WHAT IS PINNED HERE
-------------------
1. `store_bars` (daily) writes a tz-NAIVE value that is within seconds of **UTC** now -- so a
   host in any timezone gets the same number, and a tz-aware value is never handed to a
   `TIMESTAMP` column (which would let the driver's cast pick the offset);
2. `store_intraday_bars` does the same on `intraday_sync_metadata`;
3. `sync_timestamp_utc_naive` normalises an explicitly passed aware timestamp to naive UTC, so the
   column invariant holds for every caller rather than only the default path.

Rendering market-local time is a READ/report-side conversion (`docs/lv19_freshness_contract.md`
§3): the stored value is never local time.

100% offline: `tmp_path` DuckDB files, synthetic OHLCV frames, no network, no real prices.
"""

import datetime
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache, sync_timestamp_utc_naive

SYMBOL = "600584.SH"


def _daily_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-10-26", periods=3)
    return pd.DataFrame(
        {
            "Open": [10.01, 10.11, 10.21],
            "High": [10.31, 10.41, 10.51],
            "Low": [9.91, 10.01, 10.11],
            "Close": [10.21, 10.31, 10.41],
            "Volume": [1_000.0, 1_100.0, 1_200.0],
        },
        index=dates,
    )


def _intraday_frame() -> pd.DataFrame:
    stamps = pd.date_range("2026-10-26 09:35", periods=6, freq="5min")
    return pd.DataFrame(
        {
            "Open": [10.0 + 0.01 * i for i in range(6)],
            "High": [10.1 + 0.01 * i for i in range(6)],
            "Low": [9.9 + 0.01 * i for i in range(6)],
            "Close": [10.05 + 0.01 * i for i in range(6)],
            "Volume": [500.0 + 10.0 * i for i in range(6)],
        },
        index=stamps,
    )


def _read_sync_time(cache: DuckDBCache, table: str) -> datetime.datetime:
    con = cache._get_connection(read_only=True)
    try:
        row = con.execute(
            f"SELECT last_sync_time FROM {table} WHERE provider = ? AND symbol = ?",
            ["FuyaoDataProvider", SYMBOL],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()
    assert row is not None, f"{table}: store wrote no metadata row"
    return row[0]


def _assert_naive_utc(value) -> None:
    """tz-naive AND within a few seconds of the UTC clock (the discriminating assertion)."""
    assert isinstance(value, (datetime.datetime, pd.Timestamp)), type(value)
    assert value.tzinfo is None, (
        f"last_sync_time must be tz-NAIVE (a TIMESTAMP column has no offset); got {value!r}"
    )
    utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    delta = abs(utc_now - pd.Timestamp(value).to_pydatetime())
    # A bare LOCAL stamp fails by the host's UTC offset (8h here); a few seconds pins UTC.
    assert delta < datetime.timedelta(seconds=30), (
        f"last_sync_time {value!r} is {delta} from UTC now ({utc_now!r}) -- "
        "this is a local wall clock, not UTC"
    )


@pytest.fixture
def cache(tmp_path) -> DuckDBCache:
    """File-backed cache under `tmp_path` (the column is read back from DuckDB itself)."""
    return DuckDBCache(db_path=str(tmp_path / "cache.duckdb"))


def test_store_bars_naive_utc(cache):
    df = _daily_frame()
    cache.store_bars(
        provider="FuyaoDataProvider",
        symbol=SYMBOL,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="raw",
    )

    _assert_naive_utc(_read_sync_time(cache, "asset_sync_metadata"))


def test_store_intraday_naive_utc(cache):
    df = _intraday_frame()
    cache.store_intraday_bars(
        provider="FuyaoDataProvider",
        symbol=SYMBOL,
        df=df,
        interval="5m",
        requested_start=df.index.min().strftime("%Y-%m-%d %H:%M:%S"),
        requested_end=df.index.max().strftime("%Y-%m-%d %H:%M:%S"),
        adjustflag="raw",
    )

    _assert_naive_utc(_read_sync_time(cache, "intraday_sync_metadata"))


def test_explicit_aware_sync_time_is_normalised_to_naive_utc(cache):
    """A caller cannot smuggle a local offset into the column."""
    aware_local = datetime.datetime(
        2026, 11, 5, 20, 23, 28, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
    )
    df = _daily_frame()
    cache.store_bars(
        provider="FuyaoDataProvider",
        symbol=SYMBOL,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        sync_time=aware_local,
        interval="1d",
        adjustflag="raw",
    )

    stored = _read_sync_time(cache, "asset_sync_metadata")
    assert stored.tzinfo is None
    # 20:23:28 +08:00 IS 12:23:28 UTC -- the stored value is the same instant, not the wall clock.
    assert pd.Timestamp(stored) == pd.Timestamp("2026-11-05 12:23:28")


def test_sync_timestamp_utc_naive_contract():
    """The helper itself: naive in, naive out; aware in, naive UTC out; default = UTC now."""
    local_wall = datetime.datetime(2026, 11, 5, 20, 23, 28)
    assert sync_timestamp_utc_naive(local_wall) == local_wall  # naive = already UTC by contract

    aware = datetime.datetime(
        2026, 11, 5, 20, 23, 28, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
    )
    normalised = sync_timestamp_utc_naive(aware)
    assert normalised.tzinfo is None
    assert normalised == datetime.datetime(2026, 11, 5, 12, 23, 28)

    default = sync_timestamp_utc_naive()
    assert default.tzinfo is None
    utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs(utc_now - default) < datetime.timedelta(seconds=30)

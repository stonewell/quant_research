"""Offline regression tests for the 5m cache-freshness probe in
`scripts/sync_china_5m_and_indices.py::sync_5m_intraday`.

The defect this file pins (measured on real metadata, `600900.SH` cached
`earliest=2026-10-19 09:35` / `latest=2026-11-09 15:00`): the old cache-hit predicate was

    min_ts <= start_ts + 7d and max_ts.date() >= end_date - 2 days

The second half is a *daily* backfill tolerance ("a 2-day lag still counts as up to date")
applied to 5m data that is consumed the same session. Result: for `as_of` in
{2026-11-09, 2026-11-10, 2026-11-11} every run printed CACHE HIT and **today's 5m bars never
entered the cache** -- only `as_of = 2026-11-12` (a 3-day gap) triggered a fetch.

The contract now:

1. the cached newest bar must reach `common/market_clock.py::last_expected_bar_ts(expected
   session)` -- 14:55 for a China 5m day (close minus one bar), i.e. **the session actually
   landed**; no `end - 2 days` slack on the latest side;
2. the earliest side keeps its 7-day tolerance (that describes backfill coverage, not freshness);
3. the decision is returned as structured evidence (`cache_probe` / `intraday_incomplete`) and
   printed under `--verbose`, instead of being an opaque `continue`;
4. a fetch that comes back short of 14:55 is still written (published bars are not dropped) but
   is flagged `intraday_incomplete: true` with `latest_ts` / `missing_bars_estimate` and a fixed,
   grep-able `warnings.warn` (`[WARN] 5m 当日数据不完整`). No bar is ever fabricated;
5. the **daily** sync paths are untouched by the 5m rule (regression case 4).

100% offline: in-memory DuckDB (`:memory:`), synthetic sessions, provider doubles that never
touch the network. The module-level fixture asserts `data/cache.duckdb` is byte-identical
before/after, same discipline as `test_sync_qfq_write_path.py`.
"""

import datetime
import os
import sys
import warnings

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache
from scripts import sync_china_5m_and_indices as sync


SYMBOL = "600900.SH"
PROVIDER = "BaoStockDataProvider"

#: The measured cache state of the defect report.
CACHED_EARLIEST = "2026-10-19 09:35"
CACHED_LATEST = "2026-11-09 15:00"


@pytest.fixture
def cache() -> DuckDBCache:
    """In-memory cache: no file, no production path, no lock contention."""
    return DuckDBCache(db_path=":memory:")


@pytest.fixture(scope="module", autouse=True)
def production_cache_untouched():
    """`data/cache.duckdb` must be byte-identical before/after this module."""
    path = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
    before = os.stat(path) if os.path.exists(path) else None
    yield
    after = os.stat(path) if os.path.exists(path) else None
    if before is None:
        assert after is None, "the production cache appeared while running tests"
        return
    assert after is not None, "the production cache disappeared while running tests"
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size), (
        "data/cache.duckdb changed while running the 5m freshness tests: "
        f"mtime {before.st_mtime_ns} -> {after.st_mtime_ns}, size {before.st_size} -> {after.st_size}"
    )


# ---------------------------------------------------------------------------
# Synthetic sessions / provider double
# ---------------------------------------------------------------------------


def _session_5m(day: str, last: str = "15:00") -> pd.DataFrame:
    """One A-share-shaped 5m session: 09:35-11:30 and 13:05-`last` (bar-open stamps).

    `last="15:00"` is a complete session (its final bar is at/past the 14:55 deadline);
    `last="13:05"` truncates right after the lunch break, i.e. the "published but half a
    session" shape the incomplete-flag exists for.
    """
    times = pd.date_range(f"{day} 09:35:00", f"{day} 11:30:00", freq="5min").append(
        pd.date_range(f"{day} 13:05:00", f"{day} {last}:00", freq="5min")
    )
    n = len(times)
    return pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low": [99.0] * n,
            "Close": [100.5] * n,
            "Volume": [10.0] * n,
        },
        index=times,
    )


def _frames(days, last: str = "15:00") -> pd.DataFrame:
    return pd.concat([_session_5m(d, last=last) for d in days], axis=0)


def _seed_5m(cache: DuckDBCache, symbol: str, frame: pd.DataFrame) -> None:
    """Seeds bars + intraday metadata (the metadata row is what the probe reads)."""
    cache.store_intraday_bars(
        PROVIDER,
        symbol,
        frame,
        interval="5m",
        requested_start=frame.index.min().strftime("%Y-%m-%d"),
        requested_end=frame.index.max().strftime("%Y-%m-%d"),
        adjustflag="raw",
    )


class _FakeProvider:
    """Offline double: returns a prepared frame, records every fetch, never networks."""

    def __init__(self, frame: pd.DataFrame, calls: list):
        self.frame = frame
        self.calls = calls

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        self.calls.append((symbol, start, end, interval))
        return self.frame.copy() if self.frame is not None else pd.DataFrame()

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        return self.fetch_ohlcv(symbol, start, end, interval=interval)

    def logout(self):
        return None


def _patch_providers(monkeypatch, frame: pd.DataFrame) -> list:
    calls: list = []
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: _FakeProvider(frame, calls))
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: _FakeProvider(pd.DataFrame(), calls))
    return calls


def _fetched_symbols(calls: list) -> set:
    return {c[0] for c in calls}


def _stored_5m(cache: DuckDBCache, symbol: str = SYMBOL) -> pd.DataFrame:
    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT ts, open, high, low, close, volume FROM intraday_ohlcv_bars "
            "WHERE provider = ? AND symbol = ? AND interval = '5m' ORDER BY ts",
            [PROVIDER, symbol],
        ).fetchdf()
    finally:
        if not cache.is_memory:
            con.close()
    return rows


# ---------------------------------------------------------------------------
# 1. The deadline itself: market close minus one bar, from market_clock
# ---------------------------------------------------------------------------


def test_deadline_is_the_last_expected_bar_and_weekends_snap_to_the_previous_session():
    """14:55 for a China 5m day, and a Saturday `end` must not demand a Saturday bar.

    A literal-driven rule would either use 15:00/14:55 by hand or treat Saturday as a session
    and re-fetch every symbol forever (nothing can ever satisfy a Saturday deadline).
    """
    assert sync.intraday_deadline(datetime.date(2026, 11, 10)) == pd.Timestamp("2026-11-10 14:55")
    # 2026-11-14 is a Saturday -> the week's last real session is Friday 11-13.
    assert sync.expected_intraday_session("2026-11-14") == datetime.date(2026, 11, 13)
    assert sync.expected_intraday_session("2026-11-10") == datetime.date(2026, 11, 10)


def test_real_metadata_table_now_fetches_on_11_10_and_11_11():
    """The exact measured state: only 11-09 is fresh; 11-10/-11-12 must fetch.

    Counter-example kept executable: the old predicate's `end - 2 days` half said CACHE HIT for
    11-09, 11-10 and 11-11 (only 11-12 fetched).
    """
    cached_latest = pd.Timestamp(CACHED_LATEST)
    start_ts = pd.Timestamp("2026-11-01")
    expected = {
        "2026-11-09": True,   # the session the cache really reaches
        "2026-11-10": False,  # yesterday's bar is NOT today's session
        "2026-11-11": False,
        "2026-11-12": False,
    }
    for day, should_hit in expected.items():
        session = sync.expected_intraday_session(day)
        probe = sync.probe_intraday_cache(
            pd.Timestamp(CACHED_EARLIEST),
            cached_latest,
            start_ts,
            sync.intraday_deadline(session),
        )
        assert probe["hit"] is should_hit, (day, probe)
        assert probe["reason"] == ("fresh" if should_hit else "stale_session")
        assert probe["latest_ts"] == str(cached_latest)
        assert probe["expected_deadline"] == f"{day} 14:55:00"
        # The old rule, for the record: this half accepted a 2-day lag.
        old_hit = cached_latest.date() >= (
            pd.Timestamp(day).date() - datetime.timedelta(days=2)
        )
        assert old_hit is (day != "2026-11-12")  # 11-09/11-10/11-11 = the silent-fresh bug


def test_earliest_side_still_tolerates_seven_days():
    """Only the *latest* half became strict: a slightly-late backfill anchor is still a hit."""
    deadline = sync.intraday_deadline(datetime.date(2026, 11, 10))
    start_ts = pd.Timestamp("2026-11-02")  # earliest tolerance reaches 2026-11-09 00:00

    inside = sync.probe_intraday_cache(
        pd.Timestamp("2026-11-08 09:35"), pd.Timestamp("2026-11-10 15:00"), start_ts, deadline
    )
    assert inside["hit"] is True and inside["reason"] == "fresh"

    outside = sync.probe_intraday_cache(
        pd.Timestamp("2026-11-10 09:35"), pd.Timestamp("2026-11-10 15:00"), start_ts, deadline
    )
    assert outside["hit"] is False and outside["reason"] == "earliest_short"


# ---------------------------------------------------------------------------
# 2. End-to-end: HIT sends no request, a stale session DOES
# ---------------------------------------------------------------------------


def test_cache_reaching_the_session_close_is_a_hit_and_makes_no_request(cache, monkeypatch):
    """Newest bar = the expected session's 15:00 -> CACHE HIT, provider never called."""
    _seed_5m(cache, SYMBOL, _frames(pd.bdate_range("2026-11-02", "2026-11-10")))
    calls = _patch_providers(monkeypatch, _frames(pd.bdate_range("2026-11-02", "2026-11-10")))

    report = sync.sync_5m_intraday(
        cache, [SYMBOL], [], start="2026-11-01", end="2026-11-10"
    )

    assert SYMBOL not in _fetched_symbols(calls), "a fresh 5m symbol was fetched again"
    probe = report.cache_probes[SYMBOL]
    assert probe["hit"] is True and probe["reason"] == "fresh"
    assert probe["latest_ts"] == "2026-11-10 15:00:00"
    assert probe["expected_deadline"] == "2026-11-10 14:55:00"
    assert report.intraday_incomplete is False


@pytest.mark.parametrize("as_of", ["2026-11-10", "2026-11-11"])
def test_yesterdays_session_is_a_miss_and_really_fetches(cache, monkeypatch, as_of):
    """Cache stops on 11-09 while `end` is 11-10 / 11-11: FETCH (the defect's exact shape)."""
    cached = _frames(pd.bdate_range("2026-11-02", "2026-11-09"))
    assert cached.index.max() == pd.Timestamp("2026-11-09 15:00")
    _seed_5m(cache, SYMBOL, cached)

    fetched = _frames(pd.bdate_range("2026-11-02", as_of))
    calls = _patch_providers(monkeypatch, fetched)

    report = sync.sync_5m_intraday(cache, [SYMBOL], [], start="2026-11-01", end=as_of)

    assert SYMBOL in _fetched_symbols(calls), "a stale 5m session was skipped as CACHE HIT"
    probe = report.cache_probes[SYMBOL]
    assert probe["hit"] is False and probe["reason"] == "stale_session"
    assert probe["latest_ts"] == "2026-11-09 15:00:00"
    assert probe["expected_deadline"] == f"{as_of} 14:55:00"
    assert report.intraday_incomplete is False

    # The fetched session really landed: metadata and bars both reach the deadline.
    stored = _stored_5m(cache)
    assert stored["ts"].max() == pd.Timestamp(f"{as_of} 15:00")
    meta = cache.get_intraday_metadata(PROVIDER, SYMBOL, "5m")
    assert pd.Timestamp(meta["latest_available_ts"]) >= sync.intraday_deadline(
        datetime.date.fromisoformat(as_of)
    )


# ---------------------------------------------------------------------------
# 3. Fetched-but-short: write what exists, flag it, never fabricate
# ---------------------------------------------------------------------------


def test_short_fetch_is_written_flagged_and_warned(cache, monkeypatch, capsys):
    """Newest fetched bar 13:05 < 14:55 -> written, `intraday_incomplete`, greppable warning."""
    day = "2026-11-12"
    truncated = _frames(["2026-11-12"], last="13:05")
    assert truncated.index.max() == pd.Timestamp(f"{day} 13:05")
    _patch_providers(monkeypatch, truncated)

    with pytest.warns(sync.IntradayIncompleteWarning, match=r"\[WARN\] 5m 当日数据不完整"):
        report = sync.sync_5m_intraday(cache, [SYMBOL], [], start="2026-11-01", end=day)

    # 1) it is flagged, with the numbers a Stage-1 reason code needs
    assert report.intraday_incomplete is True
    evidence = report.incomplete_symbols[SYMBOL]
    assert evidence["intraday_incomplete"] is True
    assert evidence["latest_ts"] == f"{day} 13:05:00"
    assert evidence["expected_deadline"] == f"{day} 14:55:00"
    assert evidence["missing_bars_estimate"] == 22  # 14:55 - 13:05 = 110 min / 5 min
    assert report.to_diagnostics()["intraday_incomplete"] is True

    # 2) the published bars are NOT dropped and NOT padded
    stored = _stored_5m(cache)
    assert len(stored) == len(truncated)
    assert stored["ts"].max() == pd.Timestamp(f"{day} 13:05")
    assert stored["ts"].max() < pd.Timestamp(f"{day} 14:55")
    # no invented 14:55/15:00 bar for the session
    assert not (stored["ts"] >= pd.Timestamp(f"{day} 14:55")).any()

    out = capsys.readouterr().out
    assert "[WARN] 5m 当日数据不完整" in out
    assert "missing_bars_estimate=22" in out
    assert "intraday_incomplete=true" in out


def test_short_fetch_warning_is_the_fixed_stage1_key():
    """The message Stage 1 greps for must be a module constant, not an inline f-string."""
    assert sync.INTRADAY_INCOMPLETE_WARNING == "[WARN] 5m 当日数据不完整"
    src = open(os.path.join(_REPO_ROOT, "scripts", "sync_china_5m_and_indices.py"), encoding="utf-8").read()
    assert src.count("INTRADAY_INCOMPLETE_WARNING") >= 3  # defined + both emitters
    assert '"5m 当日数据不完整' not in src, "an inline copy of the key exists (drift risk)"


# ---------------------------------------------------------------------------
# 4. Regression: the daily paths are untouched by the 5m rule
# ---------------------------------------------------------------------------


def test_daily_sync_paths_never_consult_the_intraday_probe(cache, monkeypatch):
    """The daily paths must not inherit the strict 5m deadline.

    Discriminating: if the 5m freshness machinery leaked into `sync_indices_daily` /
    `patch_equity_daily_bars`, a weekend `end` (2026-11-07, a Saturday) with Friday's bars
    stored would look "one day stale" and re-fetch -- and these two functions would be calling
    the probe helpers at all. They must not.
    """

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError("the daily sync path consulted the 5m freshness machinery")

    monkeypatch.setattr(sync, "probe_intraday_cache", _boom)
    monkeypatch.setattr(sync, "expected_intraday_session", _boom)
    monkeypatch.setattr(sync, "intraday_deadline", _boom)

    daily = pd.DataFrame(
        {
            "Open": [100.0] * 4,
            "High": [101.0] * 4,
            "Low": [99.0] * 4,
            "Close": [100.5] * 4,
            "Volume": [1000.0] * 4,
        },
        index=pd.bdate_range("2026-11-02", "2026-11-05"),
    )
    _patch_providers(monkeypatch, daily)

    # `sync_indices_daily` -> four namespaces, `patch_equity_daily_bars` -> two; both must work
    # with a Saturday `end` and a Friday-stopping frame (the pre-fix daily tolerance is kept).
    assert sync.sync_indices_daily(cache, ["000300.SH"], start="2026-11-01", end="2026-11-07") == {}
    assert sync.patch_equity_daily_bars(cache, [SYMBOL], end="2026-11-07") == {}

    con = cache._get_connection(read_only=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM ohlcv_bars WHERE symbol = '000300.SH' "
            "AND provider = 'BaoStockDataProvider' AND date <= '2026-11-05'"
        ).fetchone()[0]
        n_5m = con.execute(
            "SELECT count(*) FROM intraday_ohlcv_bars WHERE symbol = ?", [SYMBOL]
        ).fetchone()[0]
    finally:
        if not cache.is_memory:
            con.close()
    assert n == len(daily), "the daily bar write shape changed"
    assert n_5m == 0, "the daily regression case must not touch the 5m tables"

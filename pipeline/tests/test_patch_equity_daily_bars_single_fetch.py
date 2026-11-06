"""Offline regression tests for the 2026-11-05 "one fetch per symbol" refactor of
`scripts/sync_china_5m_and_indices.py::patch_equity_daily_bars`.

THE SHAPE BEFORE (and why it was wrong): the fetch sat *inside* the write-namespace loop

    for sym in stocks:
        for prov in ["FuyaoDataProvider", "MarketDBDataProvider"]:
            ...fetch new_df... -> plan_history_write(cache, prov, ...) -> store_bars(...)

so every symbol that lagged in both namespaces was requested from BaoStock/EastMoney
**twice**, and each response was used for exactly one namespace. The code's own comment
admitted it ("the provider loop below runs the same fetch twice (once per write
namespace)"). The bytes that landed in the cache were identical either way; only the
number of network requests was doubled.

THE SHAPE NOW: one fetch per symbol, fanned out to the namespaces that are actually
lagging, with the request spanning the widest window any of them needs (the earliest
per-namespace look-back start). Pinned here:

1. both namespaces lag -> exactly ONE fetch, and both namespaces end up populated;
2. only one namespace lags -> exactly one fetch, and the FRESH namespace is untouched
   (no write, no metadata bump) -- this is the equivalence condition of the refactor;
3. differing per-namespace `max_date`s -> the fetch's `start` is the EARLIER of the two
   per-namespace starts (the widest window), not the later one;
4. both providers raising -> `fetch_errors[symbol]` is populated, nothing crashes, and
   neither namespace is written;
5. differing per-namespace `max_date`s where the LATER namespace's stored basis disagrees
   inside the widened prefix -> the per-namespace window slice keeps that namespace's
   input identical to what its own (pre-merge) fetch would have returned, so its tail
   appends instead of the merged window tripping `QfqWriteRefused`.

100% offline: `BaoStockDataProvider` / `EastMoneyDataProvider` are replaced by provider
doubles that return prepared frames (or raise), the cache is an in-memory DuckDB, and no
price series is real.
"""

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

SYMBOL = "600660.SH"
FUYAO = "FuyaoDataProvider"
MDB = "MarketDBDataProvider"
END = "2026-10-28"

#: `target_end_date - timedelta(days=2)` for `END`: anything at or after this is "fresh".
FRESH_FLOOR = pd.Timestamp("2026-10-26")


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _frame(dates, base: float = 40.0, step: float = 0.37) -> pd.DataFrame:
    """Deterministic OHLCV frame (non-round floats), index = the given dates."""
    rows = []
    for i, _ in enumerate(dates):
        price = base + i * step
        rows.append(
            {
                "Open": price * 0.998712345678,
                "High": price * 1.004321098765,
                "Low": price * 0.995432109876,
                "Close": price,
                "Volume": 1_000_000.0 + 137.0 * i,
            }
        )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates))


#: The full synthetic series the doubles serve and the seeds are sliced from.
CONTINUOUS = _frame(pd.bdate_range("2026-09-01", END))


def _up_to(day: str) -> pd.DataFrame:
    return CONTINUOUS.loc[CONTINUOUS.index <= day].copy()


def _refetch() -> pd.DataFrame:
    """What a real re-fetch returns: the look-back window plus the missing tail."""
    return CONTINUOUS.loc[CONTINUOUS.index >= "2026-10-15"].copy()


class _CountingProvider:
    """Provider double: returns one prepared frame and records every request."""

    def __init__(self, frame: pd.DataFrame, **kwargs):
        self._frame = frame
        self.kwargs = kwargs
        self.starts = []

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        assert interval == "1d"
        self.starts.append(start)
        return self._frame.copy()

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):  # pragma: no cover
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

    def logout(self):
        return None


class _RaisingProvider:
    """Provider double that always raises: the 2026-10-27 GBK-error-page shape."""

    def __init__(self, message: str = "接收数据异常，请稍后再试。", **kwargs):
        self.message = message
        self.kwargs = kwargs
        self.calls = 0

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        self.calls += 1
        raise RuntimeError(self.message)

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):  # pragma: no cover
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

    def logout(self):
        return None


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
        "data/cache.duckdb changed while running the single-fetch tests"
    )


def _store(cache: DuckDBCache, df: pd.DataFrame, provider: str, symbol: str = SYMBOL) -> None:
    # This path is the RAW patch path (F7), so the fixture declares the same basis it does.
    cache.store_bars(
        provider=provider,
        symbol=symbol,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="raw",
    )


def _stored(cache: DuckDBCache, provider: str, symbol: str = SYMBOL) -> pd.DataFrame:
    """Independent read-back (raw SQL, not the helper under test)."""
    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT date, open, high, low, close, volume FROM ohlcv_bars "
            "WHERE provider = ? AND symbol = ? ORDER BY date",
            [provider, symbol],
        ).fetchdf()
    finally:
        if not cache.is_memory:
            con.close()
    if rows.empty:
        return rows
    return rows.set_index(pd.to_datetime(rows.pop("date")))


def _install(monkeypatch, bs, em) -> None:
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: bs)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: em)


def _assert_lags(day: str) -> None:
    """Guard: the seed really is stale under the untouched freshness rule."""
    assert pd.Timestamp(day) < FRESH_FLOOR


# ---------------------------------------------------------------------------
# 1. Both namespaces lag => exactly ONE fetch, both namespaces written
# ---------------------------------------------------------------------------


def test_both_namespaces_lag_fetch_once_and_write_both(cache, monkeypatch, capsys):
    seeded = _up_to("2026-10-23")
    _assert_lags("2026-10-23")
    _store(cache, seeded, FUYAO)
    _store(cache, seeded, MDB)

    # Deliberately the SAME double behind both constructors: before the refactor the fetch
    # ran once per namespace, so this recorded two identical requests for one symbol.
    fake = _CountingProvider(_refetch())
    _install(monkeypatch, fake, fake)

    fetch_errors = sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)

    assert fetch_errors == {}
    assert len(fake.starts) == 1, f"expected one fetch for one symbol, got {fake.starts!r}"

    out = capsys.readouterr().out
    # One header line per symbol now: both namespaces it feeds, one common start.
    assert f"-> Patching {SYMBOL} [{FUYAO} + {MDB}] from 2026-10-18..." in out

    # Both write namespaces hold the patched tail: the single response fed both.
    for prov in (FUYAO, MDB):
        stored = _stored(cache, prov)
        assert pd.Timestamp(END) in stored.index, f"{prov} did not receive the tail"
        assert len(stored) > len(seeded)
        assert float(stored.loc[pd.Timestamp(END), "close"]) == pytest.approx(
            float(CONTINUOUS.loc[pd.Timestamp(END), "Close"])
        )


# ---------------------------------------------------------------------------
# 2. Only one namespace lags => one fetch, the fresh namespace is untouched
# ---------------------------------------------------------------------------


def test_fresh_namespace_is_never_written(cache, monkeypatch):
    fresh = _up_to(END)
    assert fresh.index.max() >= FRESH_FLOOR
    _store(cache, fresh, FUYAO)  # fresh: max_date >= end - 2 days
    # MarketDB has no bars at all -> max_date is None -> it lags.

    fuyao_rows_before = _stored(cache, FUYAO)
    fuyao_meta_before = cache.get_asset_metadata(FUYAO, SYMBOL)
    assert fuyao_meta_before is not None

    fake = _CountingProvider(_refetch())
    _install(monkeypatch, fake, fake)

    sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)

    # One fetch, and it is the widest window the *lagging* namespace needs: no history at
    # all in MarketDB, so the look-back start is the historical anchor, not `END - 5d`.
    assert fake.starts == ["2016-01-01"]

    # The fresh namespace received NO write: identical rows and identical bookkeeping.
    pd.testing.assert_frame_equal(_stored(cache, FUYAO), fuyao_rows_before, check_exact=True)
    assert cache.get_asset_metadata(FUYAO, SYMBOL) == fuyao_meta_before

    # ...and the lagging one was filled from the same single response.
    mdb_rows = _stored(cache, MDB)
    assert len(mdb_rows) == len(_refetch())
    assert mdb_rows.index.max() == pd.Timestamp(END)


# ---------------------------------------------------------------------------
# 3. Differing max_dates => the fetch covers the WIDEST (earliest) window
# ---------------------------------------------------------------------------


def test_fetch_start_is_the_earliest_per_namespace_start(cache, monkeypatch):
    fuyao_seed = _up_to("2026-10-21")  # own start: 2026-10-16
    mdb_seed = _up_to("2026-10-23")  # own start: 2026-10-18
    _assert_lags("2026-10-21")
    _assert_lags("2026-10-23")
    _store(cache, fuyao_seed, FUYAO)
    _store(cache, mdb_seed, MDB)

    fake = _CountingProvider(_refetch())
    _install(monkeypatch, fake, fake)

    sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)

    # Earlier of the two per-namespace starts -- one response must cover both namespaces.
    assert fake.starts == ["2026-10-16"]

    assert _stored(cache, FUYAO).index.max() == pd.Timestamp(END)
    assert _stored(cache, MDB).index.max() == pd.Timestamp(END)


# ---------------------------------------------------------------------------
# 4. Fetch failure stays isolated: reported once, nothing written
# ---------------------------------------------------------------------------


def test_total_fetch_failure_is_reported_and_writes_nothing(cache, monkeypatch):
    seeded = _up_to("2026-10-23")
    _store(cache, seeded, FUYAO)
    _store(cache, seeded, MDB)
    meta_before = {
        prov: cache.get_asset_metadata(prov, SYMBOL) for prov in (FUYAO, MDB)
    }

    bs = _RaisingProvider()
    em = _RaisingProvider("EastMoney error page")
    _install(monkeypatch, bs, em)

    fetch_errors = sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)

    # Reported once for the symbol, naming BOTH providers -- not once per namespace.
    assert set(fetch_errors) == {SYMBOL}
    assert fetch_errors[SYMBOL].startswith("BaoStockDataProvider: RuntimeError: ")
    assert "EastMoneyDataProvider: RuntimeError: EastMoney error page" in fetch_errors[SYMBOL]
    assert bs.calls == 1 and em.calls == 1  # one BaoStock attempt, one EastMoney fallback

    # Neither namespace was touched.
    for prov in (FUYAO, MDB):
        # `check_names=False`: the SQL read-back names its index "date" and lowercases columns.
        pd.testing.assert_frame_equal(
            _stored(cache, prov).rename(columns=str.capitalize),
            seeded,
            check_exact=True,
            check_names=False,
            check_freq=False,
        )
        assert cache.get_asset_metadata(prov, SYMBOL) == meta_before[prov]


# ---------------------------------------------------------------------------
# 5. Differing max_dates + a disagreeing stored close in the widened prefix
# ---------------------------------------------------------------------------


def test_per_namespace_window_is_sliced_back_to_its_own_fetch(cache, monkeypatch):
    """The merged window must not leak one namespace's days into the other's verification.

    Fuyao lags from 2026-10-21 (own start 2026-10-16), MarketDB from 2026-10-23 (own start
    2026-10-18), so the single merged fetch starts at 2026-10-16. MarketDB's stored close on
    2026-10-16 disagrees with the incoming value: that session is INSIDE the widened prefix
    but OUTSIDE the window MarketDB's own pre-merge fetch would have asked for.

    Without the per-namespace slice, `plan_history_write` sees the disagreement and refuses
    the whole MarketDB write (`QfqWriteRefused`, this test's pre-fix failure). With it,
    MarketDB's input starts at 2026-10-18 exactly as before, so its tail appends and the
    disagreeing stored row is left alone -- the same bytes the pre-merge code produced.
    """
    fuyao_seed = _up_to("2026-10-21")  # own start: 2026-10-16 (the merged start)
    mdb_seed = _up_to("2026-10-23")  # own start: 2026-10-18
    _assert_lags("2026-10-21")
    _assert_lags("2026-10-23")

    # The disagreement sits at 2026-10-16, i.e. only inside the prefix the merge introduced.
    prefix_day = pd.Timestamp("2026-10-16")
    disagreeing_close = float(CONTINUOUS.loc[prefix_day, "Close"]) * 0.98
    mdb_seed.loc[prefix_day, ["Open", "High", "Low", "Close"]] *= 0.98
    assert float(mdb_seed.loc[prefix_day, "Close"]) == pytest.approx(disagreeing_close)

    _store(cache, fuyao_seed, FUYAO)
    _store(cache, mdb_seed, MDB)

    fake = _CountingProvider(_refetch())
    _install(monkeypatch, fake, fake)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fetch_errors = sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)

    assert fetch_errors == {}
    # One fetch, still spanning the widest window (the earlier namespace's start).
    assert fake.starts == ["2026-10-16"]
    # Nothing was refused: no QfqRebaseWarning escaped the write gate.
    assert [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)] == []

    mdb_after = _stored(cache, MDB)
    # MarketDB's tail appended normally...
    assert mdb_after.index.max() == pd.Timestamp(END)
    assert pd.Timestamp(END) in mdb_after.index
    # ...and its disagreeing stored prefix row was NOT rewritten by the slice path.
    assert float(mdb_after.loc[prefix_day, "close"]) == pytest.approx(disagreeing_close)

    fuyao_after = _stored(cache, FUYAO)
    assert fuyao_after.index.max() == pd.Timestamp(END)
    assert float(fuyao_after.loc[prefix_day, "close"]) == pytest.approx(
        float(CONTINUOUS.loc[prefix_day, "Close"])
    )

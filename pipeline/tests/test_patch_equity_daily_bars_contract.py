"""LV19 freshness contract for `scripts/sync_china_5m_and_indices.py::patch_equity_daily_bars`.

THE DEFECT (development_plan `LV19`, freeze doc `docs/lv19_freshness_contract.md`)
-------------------------------------------------------------------------------
PART A.2 judged EVERY write namespace against one 2-day tolerance:

    if max_date is not None and max_date >= target_end_date - timedelta(days=2):
        continue                      # "already up to date"

while the valuation read (`common/freshness.py`) requires a bar **exactly at** `as_of`. On
2026-11-05 the gap produced `fetch_errors = {}` / `steps.patch_equity_daily_bars = ok` in the
archived `stage1_health_report.json` while the namespace `compute_nav` actually reads had been
SKIPPED as "fresh" one day behind -- the Stage-1 NAV then degraded and the first round could not
issue tickets.

WHAT THIS FILE PINS
-------------------
1. a STRICT namespace one day behind is patched (zero-day tolerance) and the stored max date
   reaches `end`;
2. a NON-STRICT namespace one day behind is still skipped (the weekend/holiday tolerance must
   NOT be removed wholesale -- a weekend full-universe run must not re-fetch everything);
3. an already-fresh strict namespace is skipped (no fetch is even issued);
4. a fetch error is reported as `failed`, and the key is PRESENT in the trace (not absent) --
   "we tried and could not" must never read the same as "we did not need to";
5. the strict namespace may be named either as the stored cache key (`MarketDBDataProvider`) or
   as the settings name (`marketdb`) -- `canonicalize_provider_key` makes them the same namespace.

100% offline: provider doubles return prepared synthetic frames (or raise), the cache is an
in-memory DuckDB, no network, and `data/runs/**` / the production cache are never read.
"""

import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache
from scripts import sync_china_5m_and_indices as sync
from scripts.sync_china_5m_and_indices import FUYAO_NAMESPACE, MARKETDB_NAMESPACE

SYMBOL = "600660.SH"
END = "2026-10-28"


# ---------------------------------------------------------------------------
# Synthetic fixtures (no real prices, no provider objects)
# ---------------------------------------------------------------------------


def _frame(dates, base: float = 40.0, step: float = 0.37) -> pd.DataFrame:
    """Deterministic OHLCV frame; non-round floats so a rescale could never be mistaken for it."""
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


CONTINUOUS = _frame(pd.bdate_range("2026-10-01", END))


def _up_to(day: str) -> pd.DataFrame:
    return CONTINUOUS.loc[CONTINUOUS.index <= day].copy()


def _refetch() -> pd.DataFrame:
    """The look-back window plus the missing tail, i.e. what a real re-fetch returns."""
    return CONTINUOUS.loc[CONTINUOUS.index >= "2026-10-15"].copy()


class _Provider:
    """Provider double: one prepared frame per call (or a raise), recording every request."""

    def __init__(self, frame=None, error: BaseException = None, **kwargs):
        self._frame = frame
        self._error = error
        self.kwargs = kwargs
        self.starts = []

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        assert interval == "1d"
        self.starts.append(start)
        if self._error is not None:
            raise self._error
        return self._frame.copy()

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):  # pragma: no cover
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

    def logout(self):
        return None


@pytest.fixture
def cache() -> DuckDBCache:
    """In-memory cache: no file, no production path, no lock contention."""
    return DuckDBCache(db_path=":memory:")


def _store(cache: DuckDBCache, df: pd.DataFrame, provider: str) -> None:
    cache.store_bars(
        provider=provider,
        symbol=SYMBOL,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="raw",
    )


def _stored_max(cache: DuckDBCache, provider: str) -> pd.Timestamp:
    """Independent read-back of the bar table (raw SQL, not the helper under test)."""
    con = cache._get_connection(read_only=True)
    try:
        row = con.execute(
            "SELECT MAX(date) FROM ohlcv_bars WHERE provider = ? AND symbol = ?",
            [provider, SYMBOL],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()
    return pd.Timestamp(row[0])


def _install(monkeypatch, bs, em) -> None:
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: bs)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: em)


# ---------------------------------------------------------------------------
# 1. Strict namespace: one day behind is NOT fresh => patched, and it reaches `end`
# ---------------------------------------------------------------------------


def test_strict_zero_tolerance_patches(cache, monkeypatch):
    """DISCRIMINATING: with the strict branch deleted this symbol stays one day behind.

    MarketDB (the namespace the station's valuation read uses) stops at `end - 1`; Fuyao is
    already at `end`. Under the old single 2-day rule MarketDB was declared fresh and never
    fetched -- exactly the 2026-11-05 shape.
    """
    lagging = _up_to("2026-10-27")  # ONE day behind END
    assert lagging.index.max() < pd.Timestamp(END)
    _store(cache, lagging, MARKETDB_NAMESPACE)
    _store(cache, _up_to(END), FUYAO_NAMESPACE)

    fake = _Provider(_refetch())
    _install(monkeypatch, fake, fake)

    fetch_errors = sync.patch_equity_daily_bars(
        cache, [SYMBOL], end=END, strict_namespaces=(MARKETDB_NAMESPACE,)
    )

    assert fetch_errors == {}
    assert len(fake.starts) == 1, "a one-day lag must still be fetched"
    trace = cache.namespace_trace()
    assert trace[MARKETDB_NAMESPACE] == "patched"
    assert trace[FUYAO_NAMESPACE] == "skipped"  # fresh, untouched
    assert _stored_max(cache, MARKETDB_NAMESPACE) == pd.Timestamp(END)
    assert _stored_max(cache, FUYAO_NAMESPACE) == pd.Timestamp(END)


def test_default_strict_namespace_is_the_stored_marketdb_key(cache, monkeypatch):
    """The primitive's own default must be MarketDB (the documented default provider), not both."""
    lagging = _up_to("2026-10-27")
    _store(cache, lagging, MARKETDB_NAMESPACE)
    _store(cache, _up_to(END), FUYAO_NAMESPACE)

    fake = _Provider(_refetch())
    _install(monkeypatch, fake, fake)

    sync.patch_equity_daily_bars(cache, [SYMBOL], end=END)  # no explicit strict_namespaces

    assert cache.namespace_trace()[MARKETDB_NAMESPACE] == "patched"
    assert _stored_max(cache, MARKETDB_NAMESPACE) == pd.Timestamp(END)


def test_strict_namespace_accepts_the_settings_name(cache, monkeypatch):
    """`marketdb` (settings.local.json) and `MarketDBDataProvider` (cache key) are ONE namespace."""
    _store(cache, _up_to("2026-10-27"), MARKETDB_NAMESPACE)
    _store(cache, _up_to(END), FUYAO_NAMESPACE)

    fake = _Provider(_refetch())
    _install(monkeypatch, fake, fake)

    sync.patch_equity_daily_bars(cache, [SYMBOL], end=END, strict_namespaces=("marketdb",))

    assert cache.namespace_trace()[MARKETDB_NAMESPACE] == "patched"
    assert _stored_max(cache, MARKETDB_NAMESPACE) == pd.Timestamp(END)


# ---------------------------------------------------------------------------
# 2. Non-strict namespace keeps the 2-day weekend/holiday tolerance
# ---------------------------------------------------------------------------


def test_non_strict_keeps_two_day_tolerance(cache, monkeypatch):
    """Fuyao one day behind, strict namespace = MarketDB: Fuyao must stay untouched.

    This is the half of the contract the LV19 row forbids deleting: a full-universe refresh on a
    weekend/holiday still needs its tolerance. If the tolerance were dropped wholesale, Fuyao
    would be fetched here and the trace would say `patched`.
    """
    _store(cache, _up_to("2026-10-27"), FUYAO_NAMESPACE)  # within 2 days of END
    _store(cache, _up_to(END), MARKETDB_NAMESPACE)  # strict, and genuinely fresh

    fake = _Provider(_refetch())
    _install(monkeypatch, fake, fake)

    fetch_errors = sync.patch_equity_daily_bars(
        cache, [SYMBOL], end=END, strict_namespaces=(MARKETDB_NAMESPACE,)
    )

    assert fetch_errors == {}
    assert fake.starts == [], "nothing lagged: no fetch may be issued at all"
    assert cache.namespace_trace() == {
        FUYAO_NAMESPACE: "skipped",
        MARKETDB_NAMESPACE: "skipped",
    }
    # Untouched: the tolerance means "we deliberately did not ask", not "we rewrote it".
    assert _stored_max(cache, FUYAO_NAMESPACE) == pd.Timestamp("2026-10-27")


# ---------------------------------------------------------------------------
# 3. Already-fresh strict namespace is skipped
# ---------------------------------------------------------------------------


def test_fresh_strict_is_skipped(cache, monkeypatch):
    _store(cache, _up_to(END), MARKETDB_NAMESPACE)
    _store(cache, _up_to(END), FUYAO_NAMESPACE)

    fake = _Provider(_refetch())
    _install(monkeypatch, fake, fake)

    sync.patch_equity_daily_bars(
        cache, [SYMBOL], end=END, strict_namespaces=(MARKETDB_NAMESPACE,)
    )

    assert fake.starts == []
    assert cache.namespace_trace() == {
        FUYAO_NAMESPACE: "skipped",
        MARKETDB_NAMESPACE: "skipped",
    }


# ---------------------------------------------------------------------------
# 4. Fetch error => `failed`, and the key is PRESENT
# ---------------------------------------------------------------------------


def test_failed_reported(cache, monkeypatch):
    """"We tried and could not" must be distinguishable from "we did not need to"."""
    _store(cache, _up_to("2026-10-27"), MARKETDB_NAMESPACE)  # strict, lagging
    _store(cache, _up_to(END), FUYAO_NAMESPACE)  # fresh, non-strict

    bs = _Provider(error=RuntimeError("接收数据异常，请稍后再试。"))
    em = _Provider(error=RuntimeError("EastMoney error page"))
    _install(monkeypatch, bs, em)

    fetch_errors = sync.patch_equity_daily_bars(
        cache, [SYMBOL], end=END, strict_namespaces=(MARKETDB_NAMESPACE,)
    )

    # `fetch_errors` keeps its frozen shape/meaning: one entry per failing symbol.
    assert set(fetch_errors) == {SYMBOL}
    assert fetch_errors[SYMBOL].startswith("BaoStockDataProvider: RuntimeError: ")

    trace = cache.namespace_trace()
    assert MARKETDB_NAMESPACE in trace, "a failed namespace must never be absent from the trace"
    assert trace[MARKETDB_NAMESPACE] == "failed"
    # The namespace that needed nothing is still reported as skipped, not as failed.
    assert trace[FUYAO_NAMESPACE] == "skipped"
    assert _stored_max(cache, MARKETDB_NAMESPACE) == pd.Timestamp("2026-10-27")


# ---------------------------------------------------------------------------
# 5. Refused write => `failed` (a refusal is not a green namespace)
# ---------------------------------------------------------------------------


def test_refused_write_is_reported_as_failed(cache, monkeypatch):
    """A `QfqWriteRefused` leaves the namespace behind -- the trace must not call it skipped."""
    _store(cache, _up_to("2026-10-27"), MARKETDB_NAMESPACE)
    _store(cache, _up_to(END), FUYAO_NAMESPACE)

    # Re-based window: the stored close and the incoming close disagree inside the look-back.
    refetched = _refetch()
    for col in ("Open", "High", "Low", "Close"):
        refetched[col] = refetched[col] * 0.75

    fake = _Provider(refetched)
    _install(monkeypatch, fake, fake)

    with pytest.raises(sync.QfqWriteRefused):
        sync.patch_equity_daily_bars(
            cache, [SYMBOL], end=END, strict_namespaces=(MARKETDB_NAMESPACE,)
        )

    # Published BEFORE `raise_if_any()`, i.e. the exception cannot hide the trace.
    assert cache.namespace_trace()[MARKETDB_NAMESPACE] == "failed"

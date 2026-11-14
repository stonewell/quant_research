"""PART B's adjust-factor write must COUNT a cross-source refusal and keep going.

`DuckDBCache.store_adjust_factors` refuses a batch whose `source` would mix two normalisation
bases with a `CrossSourceAdjustFactorRefused` UserWarning (prefix
`[REFUSED] cross-source adjust-factor write`, `rule` either `inside-foreign-interval` or
`frontier-append`). It deliberately never raises -- a scheduled sync has to be able to finish.

`scripts/sync_china_5m_and_indices.py::sync_5m_intraday` used to call it inside a bare
`try/except Exception`, which produced two distinct defects:

1. **Silent skip.** The refusal reached the operator only as a default-format stderr warning and
   was never counted anywhere in the run's own refusal reporting, so a nightly sync could leave a
   symbol without its factors and still look green.
2. **Abort under `-W error`.** With `-W error` / `PYTHONWARNINGS=error` the warning becomes an
   exception, lands in that `except Exception`, is re-reported as a generic `QfqWriteRefused`, and
   `refusals.raise_if_any()` then aborts the WHOLE of PART B -- every remaining symbol of the run
   is lost because one symbol's factor batch was refused.

The fix detects the refusal regardless of the warning filters in force
(`warnings.catch_warnings(record=True)` plus a CATEGORY-SCOPED `simplefilter("always", ...)`, so
the identical code path runs under the default filters and under `-W error`), records one
`[CROSS-SOURCE]` entry per refused symbol after the loop, and never routes it through
`raise_if_any()`. `allow_cross_source=True` is never used: the refusal is the point.

100% offline: in-memory DuckDB (`:memory:`), synthetic sessions and factors, provider doubles that
never touch the network.
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

from common.duckdb_cache import (  # noqa: E402
    CrossSourceAdjustFactorRefused,
    DuckDBCache,
)
from scripts import sync_china_5m_and_indices as sync  # noqa: E402

SYMBOL = "600900.SH"
#: The symbol AFTER the refused one: it is what proves the loop was not aborted.
TAIL_SYMBOL = "600036.SH"
PROVIDER = "BaoStockDataProvider"
#: The dense, 2024-anchored source whose basis a BaoStock batch must never overwrite (D9).
FOREIGN = "FuyaoDataProvider"

END = "2026-11-10"
START = "2026-11-01"


@pytest.fixture
def cache() -> DuckDBCache:
    """In-memory cache: no file, no production path, no lock contention."""
    return DuckDBCache(db_path=":memory:")


# ---------------------------------------------------------------------------
# Synthetic data / provider double
# ---------------------------------------------------------------------------


def _session_5m(day: str, last: str = "15:00") -> pd.DataFrame:
    """One complete A-share 5m session (09:35-11:30, 13:05-`last`)."""
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


def _frames(days) -> pd.DataFrame:
    return pd.concat([_session_5m(d) for d in days], axis=0)


def _factor_days() -> list:
    """The BaoStock batch's dates -- the same business-day range the fake provider returns."""
    return list(pd.bdate_range("2026-11-02", END))


def _factors(days, value: float = 1.30) -> pd.DataFrame:
    return pd.DataFrame(
        {"factor_hfq": [value] * len(days)}, index=pd.to_datetime(list(days))
    )


class _FakeProvider:
    """Offline double: one 5m frame, one factor frame per symbol, records every call."""

    def __init__(self, frame: pd.DataFrame, factors: dict, factor_calls: list, **kwargs):
        self.frame = frame
        self.factors = factors
        self.factor_calls = factor_calls
        self.kwargs = kwargs

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        return self.frame.copy()

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        return self.frame.copy()

    def fetch_adjust_factors(self, symbol, start, end):
        self.factor_calls.append(symbol)
        return self.factors.get(symbol, pd.DataFrame())

    def logout(self):
        return None


def _patch_providers(monkeypatch, factors: dict) -> list:
    calls: list = []
    frame = _frames(pd.bdate_range("2026-11-02", END))
    monkeypatch.setattr(
        sync, "BaoStockDataProvider", lambda **kw: _FakeProvider(frame, factors, calls, **kw)
    )
    monkeypatch.setattr(
        sync, "EastMoneyDataProvider", lambda **kw: _FakeProvider(frame, {}, calls, **kw)
    )
    return calls


def _seed_foreign_factors(cache: DuckDBCache, days) -> None:
    """A foreign-basis series that already owns those dates (empty symbol accepts any source)."""
    cache.store_adjust_factors(SYMBOL, _factors(days, value=1.2452), source=FOREIGN)


def _stored_factors(cache: DuckDBCache, symbol: str = SYMBOL) -> dict:
    df = cache.query_adjust_factors(symbol)
    return {
        pd.Timestamp(d).date(): (float(v), s)
        for d, v, s in zip(df.index, df["factor_hfq"], df["source"])
    }


def _stored_5m_symbols(cache: DuckDBCache) -> set:
    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM intraday_ohlcv_bars "
            "WHERE provider = ? AND interval = '5m'",
            [PROVIDER],
        ).fetchall()
    finally:
        if not cache.is_memory:
            con.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# 1. The refusal is counted, carries its rule, and does NOT abort the run
# ---------------------------------------------------------------------------


def test_refused_factor_batch_is_counted_and_does_not_abort_under_werror(
    cache, monkeypatch, capsys
):
    """Prevents BOTH original defects: the silent skip AND the `-W error` abort of PART B.

    The refused batch is the D9 shape -- a BaoStock batch whose dates fall inside the interval
    already covered by Fuyao rows -- so the rule must be `inside-foreign-interval`. The run is
    executed with `warnings.simplefilter("error")` (what `-W error` does at process level) to pin
    that the detection is filter-independent: before the fix this raised out of
    `refusals.raise_if_any()`, unwound the whole of PART B, and the tail symbol was never stored.
    """
    days = _factor_days()
    _seed_foreign_factors(cache, ["2026-11-05", "2026-11-06"])
    _patch_providers(
        monkeypatch,
        {sym: _factors(days) for sym in (SYMBOL, TAIL_SYMBOL, "511880.SH")},
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # == `-W error` for every warning inside the call
        report = sync.sync_5m_intraday(
            cache, [SYMBOL, TAIL_SYMBOL], [], start=START, end=END
        )

    out = capsys.readouterr().out

    # 1) Exactly ONE counted entry, naming the symbol, the incoming source, the rule and the reason.
    assert "[CROSS-SOURCE] 1 adjust-factor write(s) refused" in out, out
    entry = [ln for ln in out.splitlines() if f"rule=" in ln and SYMBOL in ln]
    assert len(entry) == 1, out
    assert "source=BaoStockDataProvider" in entry[0]
    assert f"rule={CrossSourceAdjustFactorRefused.RULE_INSIDE_INTERVAL}" in entry[0]
    assert "inside-foreign-interval" in entry[0]
    assert f"refused_dates=2/{len(days)}" in entry[0]
    assert f"stored={FOREIGN}" in entry[0]

    # 2) It is NOT re-reported as a generic QfqWriteRefused: no "Factor fetch failed", no abort.
    assert "Factor fetch failed" not in out, out
    assert dict(report) == {}, dict(report)

    # 3) The run really finished: the tail symbol (after the refused one) was written, and so was
    #    the refused symbol's own 5m bars -- only its FACTORS were skipped.
    assert _stored_5m_symbols(cache) >= {SYMBOL, TAIL_SYMBOL}, _stored_5m_symbols(cache)

    # 4) Nothing was overwritten on the foreign basis: every stored row is still Fuyao's.
    stored = _stored_factors(cache)
    assert {src for _, src in stored.values()} == {FOREIGN}
    assert sorted(stored) == [datetime.date(2026, 11, 5), datetime.date(2026, 11, 6)]


def test_frontier_append_refusal_is_counted_with_its_own_rule(cache, monkeypatch, capsys):
    """Prevents the `frontier-append` rule from being reported as an inside-interval clash.

    A batch whose dates are all PAST the stored rows overwrites nothing, yet it would take the
    QFQ anchor away from the source that owns it; the operator line must say so, and the run must
    still complete.
    """
    days = _factor_days()
    _seed_foreign_factors(cache, ["2026-10-01", "2026-10-02"])  # entirely BEHIND the batch
    _patch_providers(
        monkeypatch,
        {sym: _factors(days) for sym in (SYMBOL, TAIL_SYMBOL, "511880.SH")},
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        report = sync.sync_5m_intraday(
            cache, [SYMBOL, TAIL_SYMBOL], [], start=START, end=END
        )

    out = capsys.readouterr().out
    assert "[CROSS-SOURCE] 1 adjust-factor write(s) refused" in out, out
    entry = [ln for ln in out.splitlines() if "rule=" in ln and SYMBOL in ln]
    assert len(entry) == 1, out
    assert f"rule={CrossSourceAdjustFactorRefused.RULE_FRONTIER_APPEND}" in entry[0]
    assert "frontier-append" in entry[0]
    assert "inside-foreign-interval" not in entry[0]
    assert dict(report) == {}, dict(report)
    assert _stored_5m_symbols(cache) >= {SYMBOL, TAIL_SYMBOL}


# ---------------------------------------------------------------------------
# 2. A successful factor write is unchanged (no phantom refusal)
# ---------------------------------------------------------------------------


def test_successful_factor_write_prints_no_refusal_entry(cache, monkeypatch, capsys):
    """Prevents the reporter from inventing an entry for a batch that was actually written."""
    days = _factor_days()
    calls = _patch_providers(
        monkeypatch,
        {sym: _factors(days) for sym in (SYMBOL, TAIL_SYMBOL, "511880.SH")},
    )

    report = sync.sync_5m_intraday(cache, [SYMBOL, TAIL_SYMBOL], [], start=START, end=END)

    out = capsys.readouterr().out
    assert "[CROSS-SOURCE]" not in out, out
    assert dict(report) == {}, dict(report)
    assert SYMBOL in calls  # the factor fetch really ran (so the silence is not a skip)

    stored = _stored_factors(cache)
    assert sorted(stored) == sorted(d.date() for d in days)
    assert {src for _, src in stored.values()} == {PROVIDER}


# ---------------------------------------------------------------------------
# 3. FOUND WHILE FIXING (1): the generic factor-failure report itself was broken
# ---------------------------------------------------------------------------


def test_generic_factor_failure_reports_qfq_write_refused_not_attribute_error(
    cache, monkeypatch
):
    """Prevents `AttributeError: 'MarketClock' object has no attribute 'date'` in the handler.

    The `except Exception` arm (and the empty-factor arm) built its `QfqWriteRefused` with
    `market_now(MARKET_CHINA).date()`, but `market_now` returns a `MarketClock`. Any factor-write
    failure therefore aborted PART B with an opaque `AttributeError` -- which is also exactly what
    a cross-source refusal became under `-W error` before the catch above. It must stay the
    intended, structured refusal.
    """
    days = _factor_days()
    # Empty factor frame for a NON-neutral symbol -> the "Empty adjust factors" branch.
    _patch_providers(monkeypatch, {TAIL_SYMBOL: _factors(days)})

    with pytest.raises(sync.QfqWriteRefused) as excinfo:
        sync.sync_5m_intraday(cache, [SYMBOL], [], start=START, end=END)

    assert "Empty adjust factors for non-neutral symbol" in str(excinfo.value)
    assert excinfo.value.symbol == SYMBOL
    assert excinfo.value.date is not None  # the wall-clock date the report carries


# ---------------------------------------------------------------------------
# 4. Regression: the pre-existing null-volume refusal channel still works
# ---------------------------------------------------------------------------


def test_null_volume_refusal_channel_is_unchanged(cache, monkeypatch, capsys):
    """Prevents the new `[CROSS-SOURCE]` channel from disturbing `[NULL-VOLUME]`.

    `null_volume_refusals` is the existing "counted but never aborting" precedent this fix copies:
    a null-volume row in PART A.2 is isolated per symbol, printed after the loop, and does NOT
    reach `raise_if_any()`. Both channels must stay independent.
    """
    dates = [
        datetime.date.fromisoformat(d) for d in ("2026-10-26", "2026-10-27", "2026-10-28")
    ]

    def _daily_frame(volumes):
        n = len(dates)
        return pd.DataFrame(
            {
                "Open": [10.0 + i for i in range(n)],
                "High": [11.0 + i for i in range(n)],
                "Low": [9.5 + i for i in range(n)],
                "Close": [10.5 + i for i in range(n)],
                "Volume": list(volumes),
            },
            index=pd.to_datetime(list(dates)),
        )

    frames = {
        SYMBOL: _daily_frame([1000.0, float("nan"), 3000.0]),
        TAIL_SYMBOL: _daily_frame([2000.0, 2500.0, 3500.0]),
    }

    class _DailyProvider:
        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
            return frames[symbol].copy()

        def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):  # pragma: no cover
            return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

        def logout(self):
            return None

    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: _DailyProvider())
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: _DailyProvider())

    fetch_errors = sync.patch_equity_daily_bars(cache, [SYMBOL, TAIL_SYMBOL], end="2026-10-28")

    out = capsys.readouterr().out
    assert "[NULL-VOLUME]" in out, out
    assert "REFUSED (null volume)" in out, out
    assert "[CROSS-SOURCE]" not in out, out
    assert fetch_errors == {}, fetch_errors

    # The run completed: the symbol AFTER the refused one got its whole window.
    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT date FROM ohlcv_bars WHERE provider = ? AND symbol = ? ORDER BY date",
            ["FuyaoDataProvider", TAIL_SYMBOL],
        ).fetchall()
    finally:
        if not cache.is_memory:
            con.close()
    assert [r[0] for r in rows] == dates

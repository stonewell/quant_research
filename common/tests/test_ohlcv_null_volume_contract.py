"""LV5 write-side contract: a null `Volume` cell is REFUSED by name, never stored as `0.0`.

Frozen contract under test: `docs/data_gap_visibility_schema.md` §2.4 (§4.1 `NullVolumeRefused`
payload, §4.3 per-row scope, §6.1 per-symbol attribution). The whole point is the distinction the
schema freezes:

    MISSING_SESSION  <=> no row at all
    NULL_VOLUME      <=> row exists, volume IS NULL / NaN   -> refused, NEVER 0.0
    ZERO_VOLUME      <=> row exists, volume == 0.0          -> legitimate, must be stored

`0.0` in the store is a factual claim ("no trades that session"). Substituting it for “the source
did not send the value” destroys that claim irreversibly (the two are byte-identical), which is
what the pre-LV5 `df_to_store["Volume"] is the column, not the cell` behaviour did.

Everything here is offline: in-memory DuckDB, synthetic frames, no network.
"""

import datetime
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import DuckDBCache, NullVolumeRefused

PROVIDER = "NullVolumeContractProvider"
SYMBOL = "600000.SH"


def _frame(dates, volumes, base=10.0):
    """Synthetic OHLC frame. `volumes` is passed verbatim so NaN/None/0.0 survive as given."""
    n = len(dates)
    return pd.DataFrame(
        {
            "Open": [base + i for i in range(n)],
            "High": [base + i + 1.0 for i in range(n)],
            "Low": [base + i - 0.5 for i in range(n)],
            "Close": [base + i + 0.5 for i in range(n)],
            "Volume": list(volumes),
        },
        index=pd.to_datetime(list(dates)),
    )


def _store(cache, df, symbol=SYMBOL, provider=PROVIDER):
    return cache.store_bars(provider, symbol, df, interval="1d", adjustflag="qfq")


def _stored_rows(cache, symbol=SYMBOL, provider=PROVIDER):
    con = cache._get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT date, volume FROM ohlcv_bars WHERE provider = ? AND symbol = ? ORDER BY date",
            [provider, symbol],
        ).fetchall()
    finally:
        if not cache.is_memory:
            con.close()


def _metadata_row(cache, symbol=SYMBOL, provider=PROVIDER):
    con = cache._get_connection(read_only=True)
    try:
        return con.execute(
            "SELECT earliest_available_date, latest_available_date FROM asset_sync_metadata "
            "WHERE provider = ? AND symbol = ?",
            [provider, symbol],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()


@pytest.fixture
def cache():
    return DuckDBCache(db_path=":memory:")


# ---------------------------------------------------------------------------
# (a) NaN volume => NullVolumeRefused, payload carries symbol + date, NOTHING written
# ---------------------------------------------------------------------------


def test_nan_volume_is_refused_by_name_and_never_stored_as_zero(cache):
    dates = [datetime.date(2026, 10, 28)]
    df = _frame(dates, [float("nan")])

    with pytest.raises(NullVolumeRefused) as excinfo:
        _store(cache, df)

    exc = excinfo.value
    # Payload is the frozen §4.1 field list.
    assert exc.symbol == SYMBOL
    assert exc.provider == PROVIDER
    assert exc.interval == "1d"
    assert exc.dates == dates
    assert exc.n_rows == 1 == len(exc.dates)
    assert exc.first_date == exc.last_date == dates[0]
    assert exc.value_repr == "nan"
    # The symbol and the date must be readable OFF the exception -- a bare Constraint Error
    # carried neither (the defect this named refusal replaces).
    assert SYMBOL in str(exc) and "2026-10-28" in str(exc)
    assert "0.0 was NOT substituted" in str(exc)

    # No row at all: neither the refused value NOR a coerced 0.0 is on disk.
    assert _stored_rows(cache) == []
    # Detection runs BEFORE the metadata bookkeeping, so the refused day was not recorded
    # as covered either.
    assert _metadata_row(cache) is None


def test_none_volume_is_refused_with_null_repr(cache):
    """A Python `None` (object column) is the same refused state, reported as "null"."""
    dates = [datetime.date(2026, 10, 28)]
    df = _frame(dates, [np.nan])
    df["Volume"] = df["Volume"].astype(object)
    df.loc[df.index[0], "Volume"] = None

    with pytest.raises(NullVolumeRefused) as excinfo:
        _store(cache, df)

    assert excinfo.value.value_repr == "null"
    assert excinfo.value.dates == dates
    assert _stored_rows(cache) == []


# ---------------------------------------------------------------------------
# (b) Volume column ABSENT => the documented 0.0 substitution still happens
# ---------------------------------------------------------------------------


def test_absent_volume_column_defaults_to_zero(cache):
    dates = [datetime.date(2026, 10, 28)]
    df = _frame(dates, [123.0]).drop(columns=["Volume"])

    _store(cache, df)  # must NOT raise: "column absent" is a declared convention

    assert _stored_rows(cache) == [(dates[0], 0.0)]


# ---------------------------------------------------------------------------
# (c) An explicit 0.0 is a legitimate no-trade session => stored, not refused
# ---------------------------------------------------------------------------


def test_explicit_zero_volume_is_stored_and_not_refused(cache):
    dates = [datetime.date(2026, 10, 28), datetime.date(2026, 10, 29)]
    df = _frame(dates, [0.0, 1500.0])

    _store(cache, df)

    rows = _stored_rows(cache)
    assert rows == [(dates[0], 0.0), (dates[1], 1500.0)]


# ---------------------------------------------------------------------------
# (d) Mixed frame => PER-ROW refusal (frozen §4.3), valid rows still written
# ---------------------------------------------------------------------------
#
# DECISION (and why): the frozen schema's §4.3 freezes "per-row refuse, never per-batch die" --
# the null rows are removed and each forms a refusal record, while the rest of the batch is
# written normally; an empty post-removal batch must not INSERT. A whole-batch refusal would
# make "one bad row" and "the whole batch is bad" look identical to the caller, which is exactly
# the collapse §5.3 ("one bad row" vs "batch died halfway") exists to prevent. This test pins
# that choice: exact offending dates in the payload, valid rows present on disk afterwards.


def test_mixed_frame_refuses_only_the_null_rows(cache):
    dates = [datetime.date.fromisoformat(d) for d in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")]
    df = _frame(dates, [100.0, float("nan"), 300.0, 400.0, float("nan")])

    with pytest.raises(NullVolumeRefused) as excinfo:
        _store(cache, df)

    exc = excinfo.value
    assert exc.dates == [dates[1], dates[4]]  # every offending date, ascending, deduped
    assert exc.n_rows == 2
    assert exc.first_date == dates[1] and exc.last_date == dates[4]
    assert exc.value_repr == "nan"

    # Valid rows committed; refused rows absent (and NOT present as 0.0).
    assert _stored_rows(cache) == [
        (dates[0], 100.0),
        (dates[2], 300.0),
        (dates[3], 400.0),
    ]

    # The refused tail day must not be recorded as the covered boundary either.
    earliest, latest = _metadata_row(cache)
    assert earliest == dates[0]
    assert latest == dates[3]


# ---------------------------------------------------------------------------
# (e) Per-symbol isolation in `patch_equity_daily_bars`
# ---------------------------------------------------------------------------
#
# Before LV5 the `cache.store_bars(...)` call sat OUTSIDE every per-symbol `try`, so one
# null-volume row for symbol #1 raised a DuckDB `Constraint Error` out of the whole loop: the
# remaining symbols were never attempted, and `refusals.raise_if_any()` / `return fetch_errors`
# were both skipped. This test drives the REAL function (real in-memory cache, real store_bars)
# with a provider double whose #1 symbol has a NaN volume, and asserts the tail still lands.

import scripts.sync_china_5m_and_indices as sync  # noqa: E402  (after sys.path setup)

SYMBOL_1 = "600000.SH"
SYMBOL_2 = "600036.SH"
_E2E_DATES = [datetime.date.fromisoformat(d) for d in ("2026-10-26", "2026-10-27", "2026-10-28")]


class _FakeProvider:
    """Provider double: one prepared frame per symbol, no network."""

    def __init__(self, frames, **kwargs):
        self.frames = frames
        self.kwargs = kwargs
        self.seen = []

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        self.seen.append(symbol)
        return self.frames[symbol].copy()

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):  # pragma: no cover
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

    def logout(self):
        return None


def test_patch_equity_daily_bars_isolates_null_volume_per_symbol(cache, monkeypatch, capsys):
    frames = {
        SYMBOL_1: _frame(_E2E_DATES, [1000.0, float("nan"), 3000.0]),
        SYMBOL_2: _frame(_E2E_DATES, [2000.0, 2500.0, 3500.0]),
    }
    fake = _FakeProvider(frames)
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: fake)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: fake)

    fetch_errors = sync.patch_equity_daily_bars(cache, [SYMBOL_1, SYMBOL_2], end="2026-10-28")

    # No exception escaped: the refusal was isolated, so the report machinery still ran.
    assert fetch_errors == {}

    out = capsys.readouterr().out
    refused_lines = [ln for ln in out.splitlines() if "REFUSED (null volume)" in ln]
    assert refused_lines, out
    # Attributed to symbol #1 ONLY -- #2 has no null volume and must never be blamed.
    assert all(SYMBOL_1 in ln for ln in refused_lines)
    assert not any(SYMBOL_2 in ln for ln in refused_lines)
    assert "[NULL-VOLUME]" in out

    # The refused session is absent for symbol #1; its valid sessions were still stored...
    rows_1 = _stored_rows(cache, symbol=SYMBOL_1, provider="FuyaoDataProvider")
    assert [d for d, _ in rows_1] == [_E2E_DATES[0], _E2E_DATES[2]]
    assert rows_1[0][1] == 1000.0 and rows_1[1][1] == 3000.0
    rows_1_mdb = _stored_rows(cache, symbol=SYMBOL_1, provider="MarketDBDataProvider")
    assert [d for d, _ in rows_1_mdb] == [_E2E_DATES[0], _E2E_DATES[2]]

    # ...and the NEXT symbol in the loop got its full window in both write namespaces, which is
    # exactly what used to be lost when the exception aborted the `for sym` loop.
    for prov in ("FuyaoDataProvider", "MarketDBDataProvider"):
        rows_2 = _stored_rows(cache, symbol=SYMBOL_2, provider=prov)
        assert [d for d, _ in rows_2] == _E2E_DATES
        assert [v for _, v in rows_2] == [2000.0, 2500.0, 3500.0]

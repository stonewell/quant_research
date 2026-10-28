"""Offline regression tests for the QFQ write gate in `scripts/sync_china_5m_and_indices.py`
(development_plan `L2.28`).

Why a separate file (and not more cases in `test_audit_qfq_alignment.py`): that file pins the
*read-only* artefacts (`scripts/audit_qfq_alignment.py` / `repair_qfq_alignment.py` -- reproduce,
plan, dry-run, apply-on-a-copy). This one pins the **writer** contract of the sync script, which is
a different module with a different failure mode: it imports provider classes, it only ever calls
`plan_history_write`/`assert_append_only`, and its guarantee is "a sync inserts rows the cache
lacks and refuses everything else". Mixing them would put a network-shaped module import into the
pure-data audit suite.

What is pinned here (docs/version.md §13/§15 root cause, §14.5 contract):

1. the windowed "overlap-day alignment" that multiplied a locally derived scalar onto prices is
   GONE -- an incoming window that disagrees with a stored close is refused by symbol + date,
   with a `QfqRebaseWarning`, and **nothing is written** (no partial frame, no silent skip);
2. a first-ever insert (no stored bars) needs no alignment and lands verbatim;
3. an already-aligned overlap (ratio 1.0, or float64 ULP) is a *silent* no-op: no warning, no
   rewrite of the stored rows;
4. `assert_append_only` refuses `write_start <= MAX(date)` (the §14.5 sync contract);
5. the only admissible write shapes are insert-only (`append` / hole `backfill`), proven on the
   frame actually handed to the cache.

100% offline: every cache is an in-memory DuckDB (`:memory:`) or a `tmp_path` file, every price
series is synthetic, no provider is contacted (the one end-to-end test monkeypatches the provider
classes), and `data/cache.duckdb` is neither opened nor written -- the session fixture below
asserts its mtime/size are unchanged.
"""

import datetime
import os
import sys
import types
import warnings

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache
from scripts import sync_china_5m_and_indices as sync


SYMBOL = "600660.SH"
PROVIDER = "FuyaoDataProvider"

#: The exact measured factor of the §15.3 incident block (`600660.SH`, 2024-01-02 -> 2024-07-19).
INCIDENT_FACTOR = 0.9814126394


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


def _sessions(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def _frame(dates, base: float = 40.0, step: float = 0.37, factor: float = 1.0) -> pd.DataFrame:
    """Deterministic pseudo-qfq OHLCV frame (non-round floats, like real adjusted prices)."""
    rows = []
    for i, _ in enumerate(dates):
        price = (base + i * step) * factor
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


def _intraday_frame(day: str = "2022-01-04", bars: int = 8, factor: float = 1.0) -> pd.DataFrame:
    dates = pd.date_range(f"{day} 09:35", periods=bars, freq="5min")
    return _frame(dates, factor=factor)


@pytest.fixture
def cache() -> DuckDBCache:
    """In-memory cache: no file, no production path, no lock contention."""
    return DuckDBCache(db_path=":memory:")


@pytest.fixture(scope="module", autouse=True)
def production_cache_untouched():
    """The production cache must be byte-identical before/after this module (L2.8 discipline)."""
    path = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
    before = os.stat(path) if os.path.exists(path) else None
    yield
    after = os.stat(path) if os.path.exists(path) else None
    if before is None:
        assert after is None, "the production cache appeared while running tests"
        return
    assert after is not None, "the production cache disappeared while running tests"
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size), (
        "data/cache.duckdb changed while running the write-gate tests: "
        f"mtime {before.st_mtime_ns} -> {after.st_mtime_ns}, size {before.st_size} -> {after.st_size}"
    )


def _store(cache: DuckDBCache, df: pd.DataFrame, provider: str = PROVIDER, symbol: str = SYMBOL) -> None:
    # M3.6 review P1: `store_bars` refuses an implicit basis now. These fixtures ARE qfq series
    # (this whole module tests the qfq alignment/rewrite gate), so the label is declared rather
    # than omitted; no assertion below depends on it being undeclared.
    cache.store_bars(
        provider=provider,
        symbol=symbol,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="qfq",
    )


def _stored(cache: DuckDBCache, provider: str = PROVIDER, symbol: str = SYMBOL) -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# 1. First insert: no history -> verbatim, no alignment, no warning
# ---------------------------------------------------------------------------


def test_first_insert_is_written_verbatim_without_alignment(cache):
    """No stored bars -> there is no basis to align to, so the frame lands bit-for-bit."""
    dates = _sessions("2017-01-18", 12)
    incoming = _frame(dates)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = sync.plan_history_write(cache, PROVIDER, SYMBOL, incoming)

    assert plan.shape == sync.SHAPE_FIRST_INSERT
    assert plan.has_history is False
    assert plan.audit.n_overlap == 0 and plan.audit.is_aligned
    assert plan.n_dropped_overlap == 0
    # "alignment is allowed on a first insert" == "nothing is rescaled": identical bytes.
    pd.testing.assert_frame_equal(plan.df, incoming, check_exact=True)
    assert [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)] == []

    _store(cache, plan.df)
    stored = _stored(cache)
    assert len(stored) == len(incoming)
    np.testing.assert_array_equal(stored["close"].values, incoming["Close"].values)
    # requested_start falls back to the caller's request when there was no history
    assert plan.requested_start("2016-01-01") == "2016-01-01"


# ---------------------------------------------------------------------------
# 2. The discriminator: a window that disagrees with stored history is REFUSED
# ---------------------------------------------------------------------------


def test_windowed_rebase_of_stored_history_is_refused_and_names_symbol_and_date(cache):
    """The exact incident shape: stored history + a window on a 0.9814126394 basis.

    The old code multiplied the window by `loc_c / new_c` (one overlap session) and, in the
    write shapes that reached stored rows, rescaled 132 bars of this symbol (§15.3). It is now
    refused before any write, naming the symbol and the first offending date.
    """
    history = _frame(_sessions("2017-01-18", 20))
    _store(cache, history)
    before = _stored(cache)

    # Window reaching into stored history on a different (older/other) qfq basis.
    window_dates = _sessions("2017-01-18", 30)
    incoming = _frame(window_dates, factor=INCIDENT_FACTOR)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(sync.QfqWriteRefused) as excinfo:
            sync.plan_history_write(cache, PROVIDER, SYMBOL, incoming)

    exc = excinfo.value
    message = str(exc)
    assert exc.symbol == SYMBOL
    assert exc.provider == PROVIDER
    assert exc.date == datetime.date(2017, 1, 18)
    assert SYMBOL in message and "2017-01-18" in message
    assert "REFUSED windowed re-basing" in message
    assert "single-constant window" in message  # the §15.3 fingerprint, machine-detected
    assert len(exc.violations) == 20
    # every violating row is one stored session, and the ratio is the injected factor
    assert {v.ts for v in exc.violations} == set(history.index)
    np.testing.assert_allclose([v.ratio for v in exc.violations], INCIDENT_FACTOR, rtol=0, atol=1e-15)

    # A warning is emitted *before* refusing (never a silent skip) ...
    rebase_warnings = [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)]
    assert len(rebase_warnings) == 1
    assert SYMBOL in str(rebase_warnings[0].message)

    # ... and nothing at all was written for the symbol (no partial frame).
    pd.testing.assert_frame_equal(_stored(cache), before, check_exact=True)


def test_audit_reports_the_single_constant_fingerprint(cache):
    """Read-only API: `assert_no_windowed_rebase` returns the evidence, it does not raise."""
    stored = _frame(_sessions("2024-01-02", 10))
    rescaled = _frame(_sessions("2024-01-02", 10), factor=INCIDENT_FACTOR)

    audit = sync.assert_no_windowed_rebase(stored, rescaled, symbol=SYMBOL)
    assert audit.n_overlap == 10
    assert len(audit.violations) == 10
    assert audit.is_aligned is False
    assert audit.is_single_constant_rescale is True
    assert audit.ratio_spread <= 1e-15  # 1 ULP, not a re-fetch jitter
    assert audit.median_ratio == pytest.approx(INCIDENT_FACTOR, rel=0, abs=1e-12)

    aligned = sync.assert_no_windowed_rebase(stored, stored.copy(), symbol=SYMBOL)
    assert aligned.violations == ()
    assert aligned.is_aligned is True
    assert aligned.is_single_constant_rescale is False


# ---------------------------------------------------------------------------
# 3. Already aligned (1.0 / ULP): silent no-op, no rewrite, no warning noise
# ---------------------------------------------------------------------------


def test_already_aligned_overlap_is_a_silent_noop(cache):
    """Re-syncing an identical window must not write (and must not warn) -- no ULP churn."""
    history = _frame(_sessions("2024-01-02", 20))
    _store(cache, history)
    before = _stored(cache)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = sync.plan_history_write(cache, PROVIDER, SYMBOL, history.copy())

    assert plan.shape == sync.SHAPE_NOOP
    assert plan.is_empty
    assert plan.audit.is_aligned
    assert plan.n_dropped_overlap == len(history)
    assert [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)] == []
    assert [w for w in caught if issubclass(w.category, Warning)] == []
    # nothing was handed to the cache; the stored rows are untouched
    pd.testing.assert_frame_equal(_stored(cache), before, check_exact=True)


def test_ulp_level_overlap_counts_as_aligned_and_only_new_rows_are_written(cache):
    """`np.nextafter`-level differences are float64 noise: aligned, and the overlap is dropped."""
    history = _frame(_sessions("2024-01-02", 20))
    _store(cache, history)

    extended_dates = _sessions("2024-01-02", 24)
    extended = _frame(extended_dates)
    head = extended.index.isin(history.index)
    for col in ("Open", "High", "Low", "Close"):
        extended.loc[head, col] = np.nextafter(extended.loc[head, col].values, 0.0)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = sync.plan_history_write(cache, PROVIDER, SYMBOL, extended)

    assert plan.audit.is_aligned
    assert plan.audit.n_overlap == len(history)
    assert [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)] == []
    assert plan.shape == sync.SHAPE_APPEND
    assert len(plan.df) == 4  # only the 4 sessions the store lacked
    assert plan.n_dropped_overlap == len(history)
    assert plan.df.index.min() > history.index.max()
    assert plan.requested_start("2016-01-01") == plan.df.index.min().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 4. Hard write-entry contract: append-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write_start",
    ["2024-01-02", "2023-12-29"],  # equal to MAX, and strictly before MAX
)
def test_append_only_gate_rejects_first_row_at_or_before_max(write_start):
    """`write_start <= MAX(date)` is refused, naming the symbol and the offending date."""
    with pytest.raises(sync.QfqWriteRefused) as excinfo:
        sync.assert_append_only(
            write_start,
            "2024-01-02",
            symbol=SYMBOL,
            provider=PROVIDER,
            interval="1d",
        )
    exc = excinfo.value
    assert exc.symbol == SYMBOL
    assert exc.date == datetime.date.fromisoformat(write_start)
    assert SYMBOL in str(exc) and write_start in str(exc) and "2024-01-02" in str(exc)
    assert "append-only contract violated" in str(exc)


def test_append_only_gate_accepts_the_first_day_after_max():
    """The day after MAX is the ordinary tail append and must pass."""
    sync.assert_append_only("2024-01-03", "2024-01-02", symbol=SYMBOL, provider=PROVIDER)


def test_plan_writes_only_rows_the_store_lacks(cache):
    """Aligned overlap + a hole + a genuine backfill + new tail: never a stored row."""
    all_dates = _sessions("2023-12-20", 33)
    continuous = _frame(all_dates)
    history = continuous.loc["2024-01-02":"2024-01-29"]
    hole_ts = history.index[7]  # a session the cache genuinely lacks
    _store(cache, history.drop(index=hole_ts))
    stored_before = _stored(cache)

    plan = sync.plan_history_write(cache, PROVIDER, SYMBOL, continuous)

    assert plan.audit.is_aligned
    assert plan.n_dropped_overlap == len(stored_before)
    written = set(plan.df.index)
    assert written.isdisjoint(set(stored_before.index)), "a stored row would be overwritten"
    assert hole_ts in written, "the missing session must be filled"
    assert plan.shape == sync.SHAPE_BACKFILL

    n_earlier = int((continuous.index < history.index.min()).sum())
    n_tail = int((continuous.index > history.index.max()).sum())
    assert len(written) == n_earlier + 1 + n_tail
    assert plan.df.index.min() < history.index.min()
    assert plan.df.index.max() > history.index.max()

    # Writing the plan leaves the stored rows bit-identical and adds only the new sessions.
    cache.store_bars(
        provider=PROVIDER,
        symbol=SYMBOL,
        df=plan.df,
        requested_start=plan.requested_start("2023-12-20"),
        requested_end=continuous.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="qfq",
    )
    after = _stored(cache)
    assert len(after) == len(stored_before) + len(written)
    kept = after.loc[stored_before.index]
    pd.testing.assert_frame_equal(kept, stored_before, check_exact=True)


def test_pre_history_block_is_inserted_without_touching_the_stored_span(cache):
    """A backfill reaching further back (`L2.6`) is an INSERT: allowed, proven harmless.

    There is no overlapping session, so its basis cannot be *verified* here -- what the gate
    can prove is that it rewrites nothing (every written row is absent from the store), and
    the level at the join stays reviewable via `audit_qfq_alignment.py`'s seam detector.
    """
    history = _frame(_sessions("2024-01-02", 10))
    _store(cache, history)
    before = _stored(cache)

    older = _frame(_sessions("2023-11-01", 10))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = sync.plan_history_write(cache, PROVIDER, SYMBOL, older)

    assert plan.shape == sync.SHAPE_BACKFILL
    assert plan.audit.n_overlap == 0  # nothing to compare -- insert-only, no rescale path
    assert set(plan.df.index).isdisjoint(set(history.index))
    assert len(plan.df) == len(older)
    assert [w for w in caught if issubclass(w.category, sync.QfqRebaseWarning)] == []

    cache.store_bars(
        provider=PROVIDER,
        symbol=SYMBOL,
        df=plan.df,
        requested_start=plan.requested_start("2023-11-01"),
        requested_end="2023-11-14",
        interval="1d",
        adjustflag="qfq",
    )
    after = _stored(cache)
    assert len(after) == len(before) + len(older)
    pd.testing.assert_frame_equal(after.loc[before.index], before, check_exact=True)


# ---------------------------------------------------------------------------
# 5. Intraday: the gate runs before the cache's destructive R4 purge
# ---------------------------------------------------------------------------


def test_intraday_basis_change_is_refused_before_the_destructive_purge(cache):
    """`store_intraday_bars` DELETEs all bars on a >1e-4 overlap drift (R4).

    The gate refuses the frame first, so a basis change can no longer silently wipe and
    repopulate the minute history -- the stored rows survive untouched.
    """
    first = _intraday_frame()
    cache.store_intraday_bars(
        "BaoStockDataProvider",
        SYMBOL,
        first,
        interval="5m",
        requested_start="2022-01-04",
        requested_end="2022-01-04",
        adjustflag="qfq",
    )
    con = cache._get_connection(read_only=True)
    try:
        before = con.execute(
            "SELECT count(*), sum(close) FROM intraday_ohlcv_bars WHERE symbol = ? AND interval = '5m'",
            [SYMBOL],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()
    assert before[0] == len(first)

    with pytest.warns(sync.QfqRebaseWarning, match="REFUSED windowed re-basing"):
        with pytest.raises(sync.QfqWriteRefused) as excinfo:
            sync.plan_history_write(
                cache, "BaoStockDataProvider", SYMBOL, _intraday_frame(factor=0.98), interval="5m"
            )
    assert excinfo.value.interval == "5m"
    assert "09:35" in str(excinfo.value)  # intraday timestamps, not just the calendar date

    con = cache._get_connection(read_only=True)
    try:
        after = con.execute(
            "SELECT count(*), sum(close) FROM intraday_ohlcv_bars WHERE symbol = ? AND interval = '5m'",
            [SYMBOL],
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()
    assert after == before, "the intraday history was purged/rewritten despite the refusal"


# ---------------------------------------------------------------------------
# 6. End to end: `patch_equity_daily_bars` refuses instead of rescaling
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Minimal provider double: returns one prepared frame, never touches the network."""

    def __init__(self, frame: pd.DataFrame, **kwargs):
        self._frame = frame
        self.kwargs = kwargs
        self.calls = 0

    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        self.calls += 1
        return self._frame.copy()

    def logout(self):
        return None


def test_patch_equity_daily_bars_refuses_instead_of_rescaling(cache, monkeypatch, capsys):
    """The incident's end-to-end path: history + a re-based re-fetch.

    Before L2.28 this wrote the tail times `loc_c / new_c` (a locally derived factor).
    Now it refuses, prints `[REFUSED]`, and leaves the stored history bit-identical.
    """
    continuous = _frame(_sessions("2017-01-18", 38))  # 2017-01-18 -> ~2017-03-09
    history = continuous.loc[continuous.index <= "2017-02-14"]
    _store(cache, history)
    before = _stored(cache)

    # The re-fetch starts 5 days before MAX (the real look-back shape) and comes back on a
    # different qfq basis -- the 600660.SH incident factor. Volume untouched, as in the incident.
    refetch_dates = pd.bdate_range("2017-02-07", "2017-03-10")
    refetched = continuous.loc[continuous.index.isin(refetch_dates)].copy()
    for col in ("Open", "High", "Low", "Close"):
        refetched[col] = refetched[col] * INCIDENT_FACTOR

    fake = _FakeProvider(refetched)
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: fake)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: fake)

    with pytest.warns(sync.QfqRebaseWarning, match="REFUSED windowed re-basing"):
        with pytest.raises(sync.QfqWriteRefused) as excinfo:
            sync.patch_equity_daily_bars(cache, [SYMBOL], end="2017-03-10")

    exc = excinfo.value
    assert exc.symbol == SYMBOL and exc.provider == PROVIDER
    assert exc.date == datetime.date(2017, 2, 7)  # first overlapping session that disagrees
    out = capsys.readouterr().out
    assert "[REFUSED]" in out
    assert SYMBOL in out and "2017-02-07" in out

    # The stored series is byte-identical: no rescale, no tail seam, no partial write.
    pd.testing.assert_frame_equal(_stored(cache), before, check_exact=True)


def test_patch_equity_daily_bars_appends_an_aligned_tail(cache, monkeypatch):
    """The ordinary case still works: same basis -> the new sessions are appended."""
    continuous = _frame(_sessions("2017-01-18", 38))
    history = continuous.loc[continuous.index <= "2017-02-14"]
    _store(cache, history)

    refetch_dates = pd.bdate_range("2017-02-07", "2017-03-10")
    refetched = continuous.loc[continuous.index.isin(refetch_dates)].copy()

    fake = _FakeProvider(refetched)
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: fake)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: fake)

    sync.patch_equity_daily_bars(cache, [SYMBOL], end="2017-03-10")

    after = _stored(cache)
    before = _stored(cache).loc[after.index[after.index <= history.index.max()]]
    assert len(after) > len(history)
    expected = continuous.loc[continuous.index <= "2017-03-10"]
    np.testing.assert_array_equal(after["close"].values, expected.loc[after.index, "Close"].values)
    # the pre-existing sessions were not rewritten (same bytes as the synthetic continuous series)
    np.testing.assert_array_equal(
        before.loc[history.index, "close"].values, history["Close"].values
    )


# ---------------------------------------------------------------------------
# 7. The removed code, kept as the counter-example it is
# ---------------------------------------------------------------------------


def _head_windowed_alignment(frame: pd.DataFrame, stored_close_at_max: float, max_date) -> pd.DataFrame:
    """Verbatim HEAD body of the alignment this task removed (`git show HEAD:...:215-233`).

    Kept as an executable counter-example so the regression suite documents *what* was
    deleted and why the gate exists: one scalar from one overlap session, multiplied over a
    window. Note it has no notion of "this row is already stored" -- only the succeeding
    `> max_date` trim kept it off stored rows, which is one line of ordering, not a contract.
    """
    new_df = frame.copy()
    max_ts = pd.Timestamp(max_date)
    if max_ts in new_df.index:
        loc_c = stored_close_at_max
        new_c = float(new_df.loc[max_ts, "Close"])
        if loc_c > 0 and new_c > 0 and abs(new_c - loc_c) / loc_c > 1e-4:
            ratio = loc_c / new_c
            new_df[["Open", "High", "Low", "Close"]] *= ratio
    return new_df[new_df.index.date > max_date]


def test_head_alignment_rescales_a_whole_window_by_one_local_scalar():
    """Characterisation of the deleted code: a single constant over the fetched window."""
    dates = _sessions("2024-01-02", 30)
    stored = _frame(dates)
    incoming = _frame(dates, factor=INCIDENT_FACTOR)

    produced = _head_windowed_alignment(incoming, float(stored["Close"].iloc[-1]), dates[-1].date())

    # The legacy write shape (HEAD's own trim) keeps only the tail, but every row it does keep
    # has been multiplied by `stored_max_close / incoming_max_close` -- the local ratio.
    ratio = float(stored["Close"].iloc[-1]) / float(incoming["Close"].iloc[-1])
    np.testing.assert_allclose(
        produced["Close"].values / incoming.loc[produced.index, "Close"].values, ratio, rtol=0, atol=1e-15
    )
    assert abs(ratio - 1.0) > 1e-2, "the legacy factor is a material rescale, not float noise"

    # The new gate refuses the very same pair instead of manufacturing that factor.
    audit = sync.assert_no_windowed_rebase(stored, incoming, symbol=SYMBOL)
    assert audit.violations and audit.is_single_constant_rescale

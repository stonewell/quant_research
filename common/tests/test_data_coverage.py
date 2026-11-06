"""LV5 phase 2 read-side contract: the daily coverage / gap report.

Frozen contract under test: `docs/data_gap_visibility_schema.md` (§2.1 三态 predicates, §3 marker
vocabulary, §5.3 report fields + counting rules C1-C4, §5.4 window/availability, §6.2 abort
propagation). Nothing here redefines a term; the assertions are the doc's own G1-G8 shaped into
pytest.

The defect class this file guards is one: **"not observed" being written as an observed value.**

    missing_session  -> reported as 0.0 volume / as "not a gap"      (sections a, b)
    null_volume      -> reported as 0.0 / as a real zero             (sections a, g)
    unverified       -> reported as complete / missing_sessions = 0  (sections c, e)

and the reverse error, "a real zero reported as a gap" (section d), because a fix that only
pushes one way slides into the opposite defect.

Everything is offline: explicit calendars, in-memory DuckDB, synthetic frames, no network
(`check_missing_trading_days` is called only with an empty interval, where it returns before
touching `get_trading_days_calendar`).
"""

import datetime
import ast
import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common import data_coverage
from common.data_coverage import (
    COMPLETE,
    GAPPED,
    MISSING_SESSION,
    NULL_VOLUME,
    UNVERIFIED,
    ZERO_VOLUME,
    assess_coverage,
    build_report,
    complete_sessions,
    find_volume_column,
    resolve_window_calendar,
    verify_coverage_entry,
)
from common.duckdb_cache import DuckDBCache, NullVolumeRefused
from common.financial_api import check_missing_trading_days

# A Mon..Fri window. The explicit calendar below is the ONLY calendar used: the frozen doc §2.2
# forbids judging synthetic/offline data against the real holiday table, so no `MarketClock`
# without an explicit `holidays=` set is constructed for these five days.
SESSIONS = [datetime.date(2026, 10, 26) + datetime.timedelta(days=i) for i in range(5)]
WINDOW_START, WINDOW_END = SESSIONS[0], SESSIONS[-1]
SYMBOL = "600000.SH"
FIXED_STAMP = datetime.datetime(
    2026, 11, 5, 18, 4, 12, 480000, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
)


def _assess(bars, *, symbol=SYMBOL, sessions=SESSIONS, **kwargs):
    kwargs.setdefault("detected_at", FIXED_STAMP)
    return assess_coverage(
        symbol,
        window_start=sessions[0] if sessions else WINDOW_START,
        window_end=sessions[-1] if sessions else WINDOW_END,
        calendar=list(sessions),
        bars=bars,
        **kwargs,
    )


def _by_symbol(report):
    return {entry["symbol"]: entry for entry in report["symbols"]}


# ---------------------------------------------------------------------------
# (a) Three states on one symbol: interior missing session + null volume + real zero
# ---------------------------------------------------------------------------


def test_three_states_on_one_symbol_exact_markers_and_identity():
    """§2.1 + §8's worked example: one interior hole, one null cell, one real zero.

    The markers are exact strings (not `"MISSING"`, `"null"`, `"nan"`, `"zero"`), the real zero
    is NOT in the gap list, and C4's identity closes: 3 + 1 + 1 == 5 with the zero inside the 3.
    """
    bars = {
        SESSIONS[0]: 1_240_000.0,
        SESSIONS[1]: 987_000.0,
        # SESSIONS[2] absent -> interior MISSING_SESSION
        SESSIONS[3]: float("nan"),  # -> NULL_VOLUME
        SESSIONS[4]: 0.0,  # -> ZERO_VOLUME (legitimate)
    }

    entry = _assess(bars, bars_refused=1)

    assert entry["coverage"] == GAPPED
    records = {(rec["date"], rec["state"]) for rec in entry["records"]}
    assert records == {
        (SESSIONS[2].isoformat(), MISSING_SESSION),
        (SESSIONS[3].isoformat(), NULL_VOLUME),
    }
    # §9.2 G2/G3: a real zero is never a "gap record"; it is a verified observation.
    assert all(rec["state"] != ZERO_VOLUME for rec in entry["records"])
    assert entry["verified_zero_dates"] == [SESSIONS[4].isoformat()]

    counts = entry["counts"]
    assert counts["sessions_in_window"] == 5
    assert counts["sessions_checked"] == 5
    assert counts["bars_present"] == 4
    assert counts["missing_sessions"] == 1
    assert counts["null_volume_bars"] == 1
    assert counts["zero_volume_bars"] == 1
    assert counts["missing_sessions_lower_bound"] is False

    # C4, computed exactly as §5.3 defines it: 5 - 1 - 1 = 3 complete, and 3 + 1 + 1 == 5.
    assert complete_sessions(counts) == 3
    assert complete_sessions(counts) + counts["missing_sessions"] + counts["null_volume_bars"] == 5
    verify_coverage_entry(entry)  # raises CoverageContractError if the identity drifts

    # §5.1 record shape: evidence is FACTS only, and `detected_at` carries a numeric offset.
    by_date = {rec["date"]: rec for rec in entry["records"]}
    missing_rec = by_date[SESSIONS[2].isoformat()]
    assert missing_rec["evidence"]["row_present"] is False
    assert missing_rec["evidence"]["calendar_authority"] == "explicit"
    assert missing_rec["source_step"] == "stage1.coverage_check"
    assert missing_rec["detected_at"].endswith("+08:00")

    null_rec = by_date[SESSIONS[3].isoformat()]
    assert null_rec["evidence"] == {
        "row_present": True,
        "volume_is_null": True,
        "amount_is_null": None,  # not supplied => unknown, never guessed
        "refusal": "NullVolumeRefused",
    }

    # Same input twice => byte-identical entry (§8.4 reproducibility).
    assert _assess(bars, bars_refused=1) == entry
    # ...and a DataFrame with a lowercase `volume` column is the same input.
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "volume": list(bars.values())},
        index=pd.to_datetime(list(bars.keys())),
    )
    assert _assess(frame, bars_refused=1)["coverage"] == GAPPED


# ---------------------------------------------------------------------------
# (b) INTERIOR hole detection -- the assertion that fails if only the tail is checked
# ---------------------------------------------------------------------------


def test_interior_missing_session_is_detected_not_only_trailing():
    """The 2026-10-28 shape: a hole in the MIDDLE, with the last session present.

    `common/financial_api.py::check_missing_trading_days(latest, target)` walks the open tail
    interval `(latest, target]` only. With the final session present, `latest == target` and the
    tail interval is empty -- so the old check returns `[]` on exactly the input this test
    asserts a gap for. That is the structural blindness LV5 exists to remove, and this is the
    assertion that turns red if the checker is reduced to a tail comparison.
    """
    bars = {day: 100.0 for day in SESSIONS if day != SESSIONS[2]}  # 10-28 hole; 10-30 present
    latest_present = max(bars)  # == WINDOW_END: the window's LAST session does have a bar
    assert latest_present == WINDOW_END  # the hole is interior, NOT at the tail

    entry = _assess(bars)

    assert entry["coverage"] == GAPPED
    assert entry["counts"]["missing_sessions"] == 1
    assert [rec["date"] for rec in entry["records"]] == [SESSIONS[2].isoformat()]

    # The old tail-only check is blind to it -- this equality is the evidence, not decoration.
    # (Empty interval on purpose: it returns before touching `get_trading_days_calendar`.)
    assert check_missing_trading_days(latest_present, WINDOW_END) == []

    # Control: a TRAILING hole is still caught, so the fix did not trade one blindness for
    # another. Without the last session the tail interval is no longer empty.
    trailing = {day: 100.0 for day in SESSIONS[:4]}
    trailing_entry = _assess(trailing)
    assert trailing_entry["counts"]["missing_sessions"] == 1
    assert [rec["date"] for rec in trailing_entry["records"]] == [WINDOW_END.isoformat()]


# ---------------------------------------------------------------------------
# (c) `unverified` propagation + C1/C3 counter separation
# ---------------------------------------------------------------------------


def test_abort_after_two_of_five_leaves_the_rest_unverified():
    """§6.2 item 2 / §9.2 G6: never-attempted symbols must not read as "no gaps".

    Two symbols were assessed (A complete with 2 write-side refusals, B gapped by one hole);
    processing died at C, so C/D/E were never reached. Their read-side counts must be `None`,
    NOT `0` and not the window's session count -- and A's 2 refused bars must stay in
    `bars_refused`, never leak into `missing_sessions` (C3).
    """
    symbols = ["A", "B", "C", "D", "E"]
    entry_a = _assess({day: 100.0 for day in SESSIONS}, symbol="A", bars_refused=2)
    entry_b = _assess({day: 100.0 for day in SESSIONS[1:]}, symbol="B")

    report = build_report(
        symbols,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=SESSIONS,
        assessed=[entry_a, entry_b],
        aborted_at="C",
        abort_reason="NullVolumeRefused at C",
        detected_at=FIXED_STAMP,
    )

    assert report["abort"] == {"aborted": True, "at_symbol": "C", "reason": "NullVolumeRefused at C"}
    assert report["schema"] == "data_gap_report/v1"
    assert report["calendar"] == {"authority": "explicit", "market": None, "status": "explicit"}

    entries = _by_symbol(report)
    for symbol in ("C", "D", "E"):
        entry = entries[symbol]
        assert entry["coverage"] == UNVERIFIED
        assert entry["counts"]["missing_sessions"] is None  # C1: null, never 0
        assert entry["counts"]["sessions_checked"] is None
        assert entry["counts"]["bars_present"] is None
        assert entry["counts"]["bars_refused"] is None
        assert entry["counts"]["sessions_in_window"] == 5  # the calendar fact is still known
        assert entry["records"] == []
        assert entry["verified_zero_dates"] == []
        verify_coverage_entry(entry)

    # A: complete, and its 2 refusals are on the WRITE side only.
    assert entries["A"]["coverage"] == COMPLETE
    assert entries["A"]["counts"]["bars_refused"] == 2
    assert entries["A"]["counts"]["missing_sessions"] == 0
    # B: gapped by a genuine hole, with no write-side refusal reported.
    assert entries["B"]["coverage"] == GAPPED
    assert entries["B"]["counts"]["missing_sessions"] == 1

    totals = report["totals"]
    assert totals["symbols_requested"] == 5
    assert totals["symbols_attempted"] == 2
    assert totals["symbols_completed"] == 2
    assert totals["symbols_unverified"] == 3
    # C3: two separate counters, no combined "gaps" bucket anywhere.
    assert totals["bars_refused"] == 2
    assert totals["missing_sessions"] == 1
    assert not {"gaps", "total_gaps", "gap_count"} & set(totals)
    # C2: with unverified symbols in the batch the read-side total is a lower bound.
    assert totals["missing_sessions_lower_bound"] is True


def test_abort_point_symbol_keeps_a_fully_checked_entry():
    """§6.2 item 1: a refusal AFTER the write still leaves that symbol fully checked.

    C is at the abort point but its window was inspected, so it keeps its `gapped` verdict;
    only the never-touched tail (D, E) degrades to `unverified`.
    """
    symbols = ["A", "C", "D", "E"]
    entry_a = _assess({day: 100.0 for day in SESSIONS}, symbol="A")
    entry_c = _assess({SESSIONS[0]: 100.0}, symbol="C")  # 1 of 5 sessions present

    report = build_report(
        symbols,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=SESSIONS,
        assessed=[entry_a, entry_c],
        aborted_at="C",
        abort_reason="store_bars failed after the C write committed",
        detected_at=FIXED_STAMP,
    )

    entries = _by_symbol(report)
    assert entries["C"]["coverage"] == GAPPED
    assert entries["C"]["counts"]["missing_sessions"] == 4
    assert entries["D"]["coverage"] == UNVERIFIED
    assert entries["E"]["coverage"] == UNVERIFIED


def test_unassessed_symbol_is_never_treated_as_complete():
    """The vacuous-truth trap (§6.2): no entry supplied => `unverified`, with no abort needed."""
    report = build_report(
        ["A", "B"],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=SESSIONS,
        assessed={"A": _assess({day: 100.0 for day in SESSIONS}, symbol="A")},
        detected_at=FIXED_STAMP,
    )
    entries = _by_symbol(report)
    assert entries["A"]["coverage"] == COMPLETE
    assert entries["B"]["coverage"] == UNVERIFIED
    assert entries["B"]["counts"]["missing_sessions"] is None
    assert report["totals"]["symbols_unverified"] == 1
    assert report["abort"] == {"aborted": False, "at_symbol": None, "reason": None}


# ---------------------------------------------------------------------------
# (d) zero_volume ⊆ complete -- a real zero is data, not a gap
# ---------------------------------------------------------------------------


def test_zero_volume_session_is_not_gapped():
    """§3.2 item 1 + §9.2 G3/G8: a genuine 0.0 belongs to `complete`, and C4's second clause.

    If this reports `gapped`, every suspension becomes a re-fetch and a real collection defect
    drowns in legitimate zeros -- the reverse error the same contract forbids.
    """
    bars = {SESSIONS[0]: 1_000.0, SESSIONS[1]: 0.0, SESSIONS[2]: 2_000.0,
            SESSIONS[3]: 0.0, SESSIONS[4]: 3_000.0}
    entry = _assess(bars)

    assert entry["coverage"] == COMPLETE
    assert entry["records"] == []  # no gap list at all
    assert entry["counts"]["zero_volume_bars"] == 2
    assert entry["counts"]["missing_sessions"] == 0
    assert entry["counts"]["null_volume_bars"] == 0
    assert entry["verified_zero_dates"] == [SESSIONS[1].isoformat(), SESSIONS[3].isoformat()]
    # zero_volume ⊆ complete: all 5 sessions are complete and 2 of them are the verified zeros.
    assert complete_sessions(entry["counts"]) == 5
    assert entry["counts"]["zero_volume_bars"] <= complete_sessions(entry["counts"])
    verify_coverage_entry(entry)


# ---------------------------------------------------------------------------
# (e) Degenerate inputs: empty window, single session, all-complete, unknown calendar
# ---------------------------------------------------------------------------


def test_empty_window_is_complete_with_zero_sessions():
    """A window with no sessions has nothing to check: 0 + 0 + 0 == 0, not `unverified`.

    (`unverified` is reserved for "there was something to look at and I did not look"; an empty
    window would otherwise turn into a false alarm on every weekend run.)
    """
    entry = assess_coverage(
        SYMBOL,
        window_start=datetime.date(2026, 10, 31),  # Sat
        window_end=datetime.date(2026, 11, 1),  # Sun
        calendar=[],  # explicit calendar, zero sessions in the window
        bars={},
        detected_at=FIXED_STAMP,
    )
    assert entry["coverage"] == COMPLETE
    assert entry["counts"]["sessions_in_window"] == 0
    assert entry["counts"]["sessions_checked"] == 0
    assert entry["counts"]["missing_sessions"] == 0
    assert entry["records"] == []
    verify_coverage_entry(entry)

    report = build_report(
        [SYMBOL],
        window_start=datetime.date(2026, 10, 31),
        window_end=datetime.date(2026, 11, 1),
        calendar=[],
        assessed=[entry],
        detected_at=FIXED_STAMP,
    )
    assert report["totals"]["sessions_in_window"] == 0
    assert report["totals"]["missing_sessions"] == 0


def test_single_session_window_present_and_absent():
    one = [SESSIONS[2]]
    present = _assess({one[0]: 500.0}, sessions=one)
    assert present["coverage"] == COMPLETE
    assert present["counts"]["sessions_in_window"] == 1

    absent = _assess({}, sessions=one)
    assert absent["coverage"] == GAPPED
    assert absent["counts"]["missing_sessions"] == 1
    assert absent["records"][0]["state"] == MISSING_SESSION


def test_all_complete_window():
    entry = _assess({day: 1_000.0 + i for i, day in enumerate(SESSIONS)})
    assert entry["coverage"] == COMPLETE
    assert entry["counts"] == {
        "sessions_in_window": 5,
        "sessions_checked": 5,
        "bars_present": 5,
        "bars_refused": None,  # no write-side telemetry supplied => unknown, NOT 0
        "missing_sessions": 0,
        "null_volume_bars": 0,
        "zero_volume_bars": 0,
        "missing_sessions_lower_bound": False,
    }
    assert entry["records"] == []
    assert entry["verified_zero_dates"] == []
    verify_coverage_entry(entry)


def test_unknown_calendar_yields_unverified_not_complete():
    """§2.2 item 3 / §9.2 G5: no calendar => `unverified`, `missing_sessions is None`.

    A missing calendar must never degrade to "weekday => trading day" (which would fake a gap on
    every statutory holiday) nor to the real holiday table (which would fake one on every
    synthetic `pd.bdate_range` session).
    """
    entry = assess_coverage(
        SYMBOL,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=None,
        bars={day: 100.0 for day in SESSIONS},
        detected_at=FIXED_STAMP,
    )
    assert entry["coverage"] == UNVERIFIED
    assert entry["counts"]["sessions_in_window"] is None  # calendar UNKNOWN => null (C1)
    assert entry["counts"]["missing_sessions"] is None
    assert entry["counts"]["sessions_checked"] is None
    assert entry["records"] == []
    verify_coverage_entry(entry)

    report = build_report(
        [SYMBOL],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=None,
        assessed=[entry],
        detected_at=FIXED_STAMP,
    )
    assert report["calendar"]["status"] == "uncovered"
    assert report["totals"]["symbols_unverified"] == 1
    assert report["totals"]["missing_sessions"] is None  # nothing checkable, so not 0


def test_uncovered_holiday_year_is_unverified_with_market_clock():
    """The calendar authority is `MarketClock`/`market_holidays`, whose UNKNOWN is explicit.

    `get_trading_days_calendar()` would return an empty `set()` here and every day would look
    non-trading -- a gap list that shrinks to zero because the calendar failed (§2.2).
    """
    from common.market_clock import MarketClock

    clock = MarketClock(
        market="china", now=datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
    )
    window = resolve_window_calendar(datetime.date(2015, 1, 5), datetime.date(2015, 1, 9), clock)
    assert window.known is False
    assert window.status == "uncovered"
    assert window.authority == "market_clock"

    covered = resolve_window_calendar(WINDOW_START, WINDOW_END, clock)
    assert covered.known is True
    assert covered.status == "provisional"  # 2026 is PROVISIONAL, and that is NOT a downgrade
    assert len(covered.sessions) == 5

    explicit_clock = MarketClock(
        market="china",
        now=datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc),
        holidays=frozenset({SESSIONS[2]}),  # declare 10-28 a closure
    )
    declared = resolve_window_calendar(WINDOW_START, WINDOW_END, explicit_clock)
    assert declared.status == "explicit"
    assert SESSIONS[2] not in declared.sessions


def test_availability_before_first_known_date_is_unchecked_not_missing():
    """§5.4: sessions before a symbol's known availability are UNCHECKED, never gaps.

    Reporting them as `missing_session` would assert a negative that was never tested -- "I did
    not go there" masquerading as "there is nothing there".
    """
    entry = _assess(
        {day: 100.0 for day in SESSIONS[2:]},
        earliest_available_date=SESSIONS[2],
        is_earliest_known=False,
    )
    assert entry["coverage"] == UNVERIFIED
    assert entry["counts"]["sessions_in_window"] == 5
    assert entry["counts"]["sessions_checked"] == 3
    assert entry["counts"]["missing_sessions"] == 0  # nothing in the checked span is missing
    assert entry["counts"]["missing_sessions_lower_bound"] is True
    assert entry["records"] == []
    verify_coverage_entry(entry)


# ---------------------------------------------------------------------------
# (f) Phase 1 refusals: write-side count present, read-side counts unpolluted (C3)
# ---------------------------------------------------------------------------


PROVIDER = "CoveragePhase1Provider"
OUT_OF_WINDOW = datetime.date(2026, 11, 2)  # a session AFTER the report window


def _frame(dates, volumes, volume_name="Volume", base=10.0):
    n = len(dates)
    return pd.DataFrame(
        {
            "Open": [base + i for i in range(n)],
            "High": [base + i + 1.0 for i in range(n)],
            "Low": [base + i - 0.5 for i in range(n)],
            "Close": [base + i + 0.5 for i in range(n)],
            volume_name: list(volumes),
        },
        index=pd.to_datetime(list(dates)),
    )


def _stored_volumes(cache, symbol=SYMBOL, provider=PROVIDER):
    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT date, volume FROM ohlcv_bars WHERE provider = ? AND symbol = ? ORDER BY date",
            [provider, symbol],
        ).fetchall()
    finally:
        if not cache.is_memory:
            con.close()
    return {row[0]: row[1] for row in rows}


@pytest.fixture
def cache():
    return DuckDBCache(db_path=":memory:")


def test_phase1_refusal_is_write_side_only(cache):
    """Two counters, two questions: "we dropped a row" vs "the store is short a row".

    The refused row lies OUTSIDE the report window, so the window itself is complete: the
    read-side answer is 0 missing while the write-side answer is 1 refusal. Summing them (C3)
    would report a phantom gap and, on the next run, a phantom re-fetch.
    """
    dates = list(SESSIONS) + [OUT_OF_WINDOW]
    frame = _frame(dates, [100.0, 200.0, 300.0, 400.0, 500.0, float("nan")])

    with pytest.raises(NullVolumeRefused) as excinfo:
        cache.store_bars(PROVIDER, SYMBOL, frame, interval="1d", adjustflag="qfq")
    refusal = excinfo.value
    assert refusal.n_rows == 1
    assert refusal.dates == [OUT_OF_WINDOW]

    stored = _stored_volumes(cache)
    assert set(stored) == set(SESSIONS)

    entry = _assess(stored, bars_refused=refusal.n_rows)
    assert entry["coverage"] == COMPLETE
    assert entry["counts"]["bars_refused"] == 1  # write side: one row was dropped
    assert entry["counts"]["missing_sessions"] == 0  # read side: the window is fully populated
    verify_coverage_entry(entry)

    report = build_report(
        [SYMBOL],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        calendar=SESSIONS,
        assessed=[entry],
        detected_at=FIXED_STAMP,
    )
    totals = report["totals"]
    assert totals["bars_refused"] == 1
    assert totals["missing_sessions"] == 0
    assert not {"gaps", "total_gaps", "gap_count"} & set(totals)


def test_refusal_inside_the_window_still_leaves_the_read_side_missing(cache):
    """The complement of the test above, on purpose.

    A refused row inside the window is genuinely absent from the store, so the read side reports
    it as `missing_session` -- and it is reported AS a missing session, not as a null-volume one
    (the two are different facts; §2.3).
    """
    frame = _frame(SESSIONS, [100.0, 200.0, float("nan"), 400.0, 500.0])

    with pytest.raises(NullVolumeRefused) as excinfo:
        cache.store_bars(PROVIDER, SYMBOL, frame, interval="1d", adjustflag="qfq")

    stored = _stored_volumes(cache)
    entry = _assess(stored, bars_refused=excinfo.value.n_rows)

    assert entry["counts"]["bars_refused"] == 1
    assert entry["counts"]["missing_sessions"] == 1
    assert entry["counts"]["null_volume_bars"] == 0
    assert [(rec["date"], rec["state"]) for rec in entry["records"]] == [
        (SESSIONS[2].isoformat(), MISSING_SESSION)
    ]
    assert entry["coverage"] == GAPPED


# ---------------------------------------------------------------------------
# (g) Case-insensitive volume column: a real volume must never be stored as 0.0
# ---------------------------------------------------------------------------


def test_find_volume_column_is_case_insensitive():
    assert find_volume_column(["Open", "High", "Low", "Close", "Volume"]) == "Volume"
    assert find_volume_column(["open", "volume"]) == "volume"
    assert find_volume_column(["close", "VOLUME"]) == "VOLUME"
    assert find_volume_column([" Volume "]) == " Volume "
    assert find_volume_column(["Open", "Close"]) is None


def test_lowercase_volume_with_nan_is_refused_not_zeroed(cache):
    """The pre-fix hole: lowercase `volume` looked ABSENT, so the NaN became `0.0`.

    `0.0` is a factual claim ("no trades"); the source made no such claim. Refusal by name is
    the only outcome that keeps the two distinguishable (§2.4, §9.2 G1).
    """
    frame = _frame([SESSIONS[0]], [float("nan")], volume_name="volume")

    with pytest.raises(NullVolumeRefused) as excinfo:
        cache.store_bars(PROVIDER, SYMBOL, frame, interval="1d", adjustflag="qfq")

    assert excinfo.value.value_repr == "nan"
    assert excinfo.value.dates == [SESSIONS[0]]
    assert _stored_volumes(cache) == {}  # neither the NaN nor a fabricated 0.0


@pytest.mark.parametrize("volume_name", ["volume", "VOLUME", "Volume"])
def test_case_variant_volume_values_are_stored_verbatim(cache, volume_name):
    """Real volumes survive regardless of the column's case -- including a genuine 0.0."""
    dates = [SESSIONS[0], SESSIONS[1]]
    frame = _frame(dates, [1_234.0, 0.0], volume_name=volume_name)

    cache.store_bars(PROVIDER, SYMBOL, frame, interval="1d", adjustflag="qfq")

    assert _stored_volumes(cache) == {dates[0]: 1_234.0, dates[1]: 0.0}


def test_lowercase_volume_on_empty_frame_does_not_crash(cache):
    empty = pd.DataFrame(
        {"Open": [], "High": [], "Low": [], "Close": [], "volume": []},
        index=pd.to_datetime([]),
    )
    cache.store_bars(PROVIDER, SYMBOL, empty, interval="1d", adjustflag="qfq")
    assert _stored_volumes(cache) == {}


def test_frame_without_any_volume_column_still_defaults_to_zero(cache):
    """The documented convention survives the fix: a frame with NO volume axis stores 0.0."""
    frame = _frame([SESSIONS[0]], [123.0]).drop(columns=["Volume"])

    cache.store_bars(PROVIDER, SYMBOL, frame, interval="1d", adjustflag="qfq")

    assert _stored_volumes(cache) == {SESSIONS[0]: 0.0}


# ---------------------------------------------------------------------------
# Vocabulary stability (§3.3): the six markers are identifiers, not display text
# ---------------------------------------------------------------------------


def test_marker_vocabulary_is_exact_and_closed():
    assert (MISSING_SESSION, NULL_VOLUME, ZERO_VOLUME) == (
        "missing_session",
        "null_volume",
        "zero_volume",
    )
    assert (COMPLETE, GAPPED, UNVERIFIED) == ("complete", "gapped", "unverified")
    for marker in (MISSING_SESSION, NULL_VOLUME, ZERO_VOLUME, COMPLETE, GAPPED, UNVERIFIED):
        assert marker == marker.strip().lower()
        assert marker.isascii()
        assert " " not in marker
    # The module must not consume the network calendar the frozen doc excludes (§2.2): no import
    # of `common.financial_api`, and no call to `get_trading_days_calendar` (the docstring may
    # NAME it as the excluded authority -- prose is not a call site).
    source = inspect.getsource(data_coverage)
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("financial_api" in name for name in imported)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "get_trading_days_calendar" not in called

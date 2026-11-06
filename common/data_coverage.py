"""Daily coverage / gap-report layer (`LV5` phase 2), pure and fully offline.

Implements the frozen contract ``docs/data_gap_visibility_schema.md`` (``data_gap/v1`` +
``data_gap_report/v1``, frozen 2026-11-05). The doc is the authority: this module *consumes* its
三态 predicates (§2.1), marker vocabulary (§3), report fields and counting rules (§5.3) and
per-symbol attribution / abort propagation (§6). Nothing here redefines a term.

**What this module is for.** ``0.0`` in ``ohlcv_bars.volume`` is a factual claim ("no trades that
session"). The frozen doc exists because three different situations used to reach the same
"invisible" exit and became indistinguishable downstream:

    missing_session  <=> no row at all for a calendar session   (nothing was collected)
    null_volume      <=> row present, volume IS NULL / NaN      (the source sent a broken row)
    zero_volume      <=> row present, volume == 0.0             (a real no-trade session)

Only the first two are gaps. ``zero_volume`` is *verified data*: it counts into ``complete`` and
appears in ``verified_zero_dates`` so that "the gap list is empty" cannot also mean "nothing was
inspected" (§5.3).

**Why interior holes needed a new checker (and why `check_missing_trading_days` was not extended).**
``common/financial_api.py::check_missing_trading_days`` (and its four call sites in
``common/financial_api.py`` / ``scripts/run_live_strategy.py``) take
``(latest_retrieved_date, target_end_date)`` and walk only the **open tail interval**
``(latest, target]`` -- see its own docstring. By construction it cannot see a hole *inside* the
already-fetched range, which is exactly the shape of the 2026-10-28 incident (§1 item 8: the day
is silently absent, no exception, no log, no counter). Extending it would mean changing a
function whose semantics are "how many sessions do I still need to fetch" -- the fetch loop's
decision input -- into a window auditor, i.e. rewriting it under its existing callers, and its
calendar is ``get_trading_days_calendar()``, which the frozen doc §2.2 **explicitly excludes**
(it returns an empty ``set()`` for both "no holidays" and "fetch failed"; the calendar dying would
therefore make the gap list shrink to zero). This module instead takes the calendar as an
explicit argument (§2.2's offline/synthetic rule) and compares the *whole* window against the
bar dates, so a hole anywhere between the first and last session is seen. The old tail check is
left untouched: it still answers its own question, and rewriting it would break the sync loop
while gaining nothing.

**Calendar authority** (§2.2): a ``MarketClock`` (``common/market_clock.py``) -- whose holiday
tables come from ``common/market_holidays.py`` and which raises
``HolidayCalendarCoverageError`` instead of answering UNKNOWN as "closed" -- or an explicit
iterable of session dates for synthetic/offline callers. There is **no default**: a missing
calendar yields ``unverified`` (never "weekday means trading day", never the real holiday table,
and never ``get_trading_days_calendar()``).

**C1 (``null`` != ``0``)**, **C2 (lower bound)**, **C3 (``bars_refused`` and ``missing_sessions``
are never summed)** and **C4 (the closing identity)** are implemented here, not merely asserted:
see ``verify_coverage_entry`` and ``build_report``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import pandas as pd

from common.market_clock import HolidayCalendarCoverageError, MarketClock
from common.market_holidays import calendar_status as _holiday_calendar_status

__all__ = [
    # frozen marker vocabulary (§3) -- stable identifiers, never localised
    "MISSING_SESSION",
    "NULL_VOLUME",
    "ZERO_VOLUME",
    "COMPLETE",
    "GAPPED",
    "UNVERIFIED",
    "REPORT_SCHEMA",
    "CoverageContractError",
    "WindowCalendar",
    "assess_coverage",
    "assess_symbol_coverage",
    "build_report",
    "complete_sessions",
    "find_volume_column",
    "resolve_window_calendar",
    "unverified_symbol",
    "verify_coverage_entry",
]

# ---------------------------------------------------------------------------------------------
# Frozen vocabulary (`docs/data_gap_visibility_schema.md` §3). ASCII, lower snake_case, never
# translated, never carrying a number or a symbol code (§3.3). Do not invent synonyms:
# "MISSING", "null", "nan", "zero", "volume_null" are all contract violations.
# ---------------------------------------------------------------------------------------------
MISSING_SESSION = "missing_session"
NULL_VOLUME = "null_volume"
ZERO_VOLUME = "zero_volume"

COMPLETE = "complete"
GAPPED = "gapped"
UNVERIFIED = "unverified"

REPORT_SCHEMA = "data_gap_report/v1"

#: §5.3 count field names, in report order. `bars_refused` is the WRITE side (phase 1's
#: `NullVolumeRefused`), `missing_sessions` is the READ side -- C3 forbids ever adding them.
COUNT_FIELDS = (
    "sessions_in_window",
    "sessions_checked",
    "bars_present",
    "bars_refused",
    "missing_sessions",
    "null_volume_bars",
    "zero_volume_bars",
)

SOURCE_STEP = "stage1.coverage_check"


class CoverageContractError(RuntimeError):
    """A computed entry violates the frozen report contract (§5.3 C2 / C4).

    Deliberately a hard error: the identity is not decoration, it is the machine-checkable
    proof that a real-zero session was not counted as a gap (and vice versa). Raising here means
    a bug in this module, not bad input -- input problems surface as `ValueError`.
    """


def find_volume_column(columns: Iterable[Any]) -> Optional[str]:
    """The volume column's actual name in `columns`, matched **case-insensitively**.

    Why this is shared (and why it exists at all): `common/duckdb_cache.py::store_bars` used to
    look for the literal ``"Volume"`` only, so a frame carrying lowercase ``volume`` fell into
    the "column absent" branch and every real volume in it was silently stored as ``0.0``. That
    is the exact defect class the frozen doc forbids (§2.4: 缺数据被写成零成交量 -- "not
    collected" turned into "no trades"), and it is worse than a missing row because it leaves no
    trace at all: a case mismatch falsifies every downstream dollar-volume filter, turnover
    calculation and breadth count while the store looks perfectly populated. One definition,
    used by both the writer and this reader, is the only way the two agree on what "the volume
    column" is.

    Returns the first column whose stripped name case-folds to ``"volume"`` (so ``Volume``,
    ``volume``, ``VOLUME``, `` Volume `` all match), or `None` when the frame has no volume
    axis -- the documented "caller's frame has no volume" convention.
    """
    for name in columns:
        if str(name).strip().casefold() == "volume":
            return name
    return None


# ---------------------------------------------------------------------------------------------
# Calendar resolution (§2.2)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowCalendar:
    """The resolved session calendar for one `[start, end]` window.

    `sessions` is ordered ascending and contains **only trading days inside the window**, so
    `len(sessions)` *is* §5.3's `sessions_in_window` -- there is no second definition of "how
    many sessions should there have been".

    `known=False` is the calendar UNKNOWN state (§2.2 item 3): `uncovered` year or no calendar
    supplied at all. A symbol assessed against an unknown calendar is `unverified`, never
    `complete` (§9.2 G5).
    """

    start: _dt.date
    end: _dt.date
    sessions: Tuple[_dt.date, ...]
    authority: str  # "market_clock" | "explicit"
    status: str  # "covered" | "provisional" | "uncovered" | "explicit"
    market: Optional[str]
    known: bool
    reason: Optional[str] = None


def _as_date(value: Any) -> _dt.date:
    """`date` / `datetime` / ISO string / pandas Timestamp -> plain `date`."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return pd.Timestamp(value).date()


def _iter_days(start: _dt.date, end: _dt.date) -> Iterable[_dt.date]:
    day = start
    while day <= end:
        yield day
        day += _dt.timedelta(days=1)


def resolve_window_calendar(
    window_start: Any,
    window_end: Any,
    calendar: Union[MarketClock, Iterable[Any], None],
) -> WindowCalendar:
    """Resolve `calendar` into the session list of `[window_start, window_end]` (§2.2, §5.4).

    Accepted forms:
      * `MarketClock` -- `clock.is_trading_day(d)` per day (§2.2's authority). Its status is
        `explicit` when the clock was built with an explicit `holidays=` set, otherwise the
        static table's status per touched year, aggregated conservatively:
        any `uncovered` year -> `uncovered` (UNKNOWN), else any `provisional` -> `provisional`,
        else `covered`. Aggregating up is the honest direction: a window that touches a
        provisional year is not *more* certain than its weakest year.
      * an iterable of dates/session stamps -- an explicit session set (synthetic/offline).
        Only its in-window members are used; `authority="explicit"`, `status="explicit"`.
      * `None` -- UNKNOWN. **Never** falls back to "weekday => trading day" and never to the
        real holiday table (§2.2 item 2: a real table applied to `pd.bdate_range` synthetic data
        turns every statutory holiday into a fake `missing_session`).
    """
    start, end = _as_date(window_start), _as_date(window_end)
    if end < start:
        raise ValueError(f"window_end {end} is before window_start {start}")

    if calendar is None:
        return WindowCalendar(
            start=start,
            end=end,
            sessions=(),
            authority="explicit",
            status="uncovered",
            market=None,
            known=False,
            reason="no_calendar_supplied",
        )

    if isinstance(calendar, MarketClock):
        if calendar.holidays is None:
            years = {day.year for day in _iter_days(start, end)}
            statuses = {year: _holiday_calendar_status(calendar.market, year) for year in years}
            if "uncovered" in statuses.values():
                uncovered = sorted(y for y, s in statuses.items() if s == "uncovered")
                return WindowCalendar(
                    start=start,
                    end=end,
                    sessions=(),
                    authority="market_clock",
                    status="uncovered",
                    market=calendar.market,
                    known=False,
                    reason=f"holiday_calendar_uncovered_years={uncovered}",
                )
            status = "provisional" if "provisional" in statuses.values() else "covered"
        else:
            status = "explicit"
        try:
            sessions = tuple(day for day in _iter_days(start, end) if calendar.is_trading_day(day))
        except HolidayCalendarCoverageError as exc:  # defensive: pre-checked above
            return WindowCalendar(
                start=start,
                end=end,
                sessions=(),
                authority="market_clock",
                status="uncovered",
                market=calendar.market,
                known=False,
                reason=str(exc),
            )
        return WindowCalendar(
            start=start,
            end=end,
            sessions=sessions,
            authority="market_clock",
            status=status,
            market=calendar.market,
            known=True,
        )

    sessions = sorted({_as_date(day) for day in calendar})
    sessions = tuple(day for day in sessions if start <= day <= end)
    return WindowCalendar(
        start=start,
        end=end,
        sessions=sessions,
        authority="explicit",
        status="explicit",
        market=None,
        known=True,
    )


# ---------------------------------------------------------------------------------------------
# Bar / volume coercion
# ---------------------------------------------------------------------------------------------


def _coerce_bar_volumes(bars: Any) -> Dict[_dt.date, Any]:
    """`bars` -> ``{session_date: volume_value}``; values kept verbatim so NaN/None survive.

    The *presence* of a key is the row's existence (§2.3 first question), the value is the
    second question. Collapsing the two (e.g. building a Series and comparing to NaN) is the
    documented way to turn a `missing_session` into a `null_volume`.

    Accepted: a `DataFrame` (volume column found case-insensitively, index = dates), a `Series`
    (values = volumes), or a plain mapping ``{date: volume}``.
    """
    if isinstance(bars, pd.DataFrame):
        column = find_volume_column(bars.columns)
        if column is None:
            raise ValueError(
                "coverage assessment needs a volume column in the bar frame; none of "
                f"{list(bars.columns)!r} matches 'volume' (case-insensitive)"
            )
        index = pd.to_datetime(bars.index)
        return {
            ts.date(): value
            for ts, value in zip(index, bars[column].to_numpy(dtype=object))
        }
    if isinstance(bars, pd.Series):
        index = pd.to_datetime(bars.index)
        return {ts.date(): value for ts, value in zip(index, bars.to_numpy(dtype=object))}
    if isinstance(bars, Mapping):
        return {_as_date(key): value for key, value in bars.items()}
    raise TypeError(
        "bars must be a DataFrame, a Series or a {date: volume} mapping, "
        f"got {type(bars).__name__}"
    )


# ---------------------------------------------------------------------------------------------
# Per-symbol assessment (§2.3, §5.1-§5.4)
# ---------------------------------------------------------------------------------------------


def _iso_with_offset(value: Union[_dt.datetime, str, None]) -> str:
    """§5.1: `detected_at` is ISO-8601 **with a numeric offset**; a bare timestamp is malformed.

    Default is "now" in the host's local zone (aware), converted once per call so every record
    of one assessment carries the same instant (deterministic reports, §8.4).
    """
    if value is None:
        return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat()
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise ValueError(
            "detected_at must be timezone-aware (ISO-8601 with a numeric offset, §5.1); "
            f"got naive {value!r}"
        )
    return value.isoformat()


def _record(
    symbol: str,
    day: _dt.date,
    state: str,
    evidence: Mapping[str, Any],
    source_step: str,
    detected_at: str,
) -> Dict[str, Any]:
    """One §5.1 gap record. `evidence` carries facts only -- never "maybe suspended" (§5.2)."""
    return {
        "symbol": symbol,
        "date": day.isoformat(),
        "state": state,
        "evidence": dict(evidence),
        "detected_at": detected_at,
        "source_step": source_step,
    }


def _empty_counts() -> Dict[str, Any]:
    """All §5.3 counts UNKNOWN. `null`, never `0` -- C1."""
    return {
        "sessions_in_window": None,
        "sessions_checked": None,
        "bars_present": None,
        "bars_refused": None,
        "missing_sessions": None,
        "null_volume_bars": None,
        "zero_volume_bars": None,
        "missing_sessions_lower_bound": True,
    }


def unverified_symbol(
    symbol: str,
    window: WindowCalendar,
    *,
    bars_refused: Optional[int] = None,
    detected_at: Union[_dt.datetime, str, None] = None,
) -> Dict[str, Any]:
    """A symbol whose window was **not** inspected: `unverified`, `missing_sessions = None`.

    Frozen §6.2 / §9.2 G5-G6. `missing_sessions` is `None` -- not `0` and not the window's
    session count -- because "I did not look" must never be spelled the same way as "I looked
    and it was fine" (C1). `records` is empty because there is no observation to record.

    `sessions_in_window` **is** filled when the calendar is known: the expected denominator is
    a calendar fact, independent of whether anyone looked. `sessions_checked` stays `None`.

    `bars_refused` is passed through untouched (it is write-side telemetry, C3); `None` means
    "no write-side report for this symbol", which is not the same as `0`.
    """
    counts = _empty_counts()
    counts["bars_refused"] = bars_refused
    if window.known:
        counts["sessions_in_window"] = len(window.sessions)
    return {
        "symbol": symbol,
        "coverage": UNVERIFIED,
        "counts": counts,
        "records": [],
        "verified_zero_dates": [],
    }


def assess_symbol_coverage(
    symbol: str,
    window: WindowCalendar,
    *,
    bars: Any = None,
    provider: Optional[str] = None,
    source_step: str = SOURCE_STEP,
    detected_at: Union[_dt.datetime, str, None] = None,
    bars_refused: Optional[int] = None,
    amounts: Any = None,
    earliest_available_date: Any = None,
    is_earliest_known: bool = True,
) -> Dict[str, Any]:
    """Assess one symbol over `window` and return its §5.3 report entry.

    `bars` is the *stored* series (index = session date, value = volume) -- see
    `_coerce_bar_volumes`. `bars=None` means "this symbol was never attempted" and yields
    `unverified` (§6.2), which is how a batch that died before reaching it stays honest.

    `bars_refused` is the phase-1 write-side count (`NullVolumeRefused` rows) for this symbol.
    It is kept in its own field (C3) -- summing it into `missing_sessions` would erase the
    difference between "we dropped a row" and "the store is short a row".

    `amounts` (optional) supplies the `amount_is_null` fact frozen into §5.2's `null_volume`
    evidence. When not supplied the key is `None` = unknown, never `True`/`False` guessed.

    `earliest_available_date` / `is_earliest_known` implement §5.4: sessions before a symbol's
    known availability are **not** `missing_session` (nobody ever fetched that span); they are
    unchecked, so the entry is `unverified` with `missing_sessions_lower_bound=True`.
    """
    stamp = _iso_with_offset(detected_at)
    calendar_evidence = {
        "calendar_status": window.status,
        "calendar_authority": window.authority,
    }

    if not window.known or bars is None:
        return unverified_symbol(
            symbol, window, bars_refused=bars_refused, detected_at=detected_at
        )

    volumes = _coerce_bar_volumes(bars)
    amount_map: Optional[Dict[_dt.date, Any]] = None
    if amounts is not None:
        if isinstance(amounts, Mapping):
            amount_map = {_as_date(k): v for k, v in amounts.items()}
        elif isinstance(amounts, pd.Series):
            amount_map = _coerce_bar_volumes(amounts)
        else:  # pragma: no cover - defensive
            raise TypeError("amounts must be a mapping or Series")

    sessions: Tuple[_dt.date, ...] = window.sessions
    checked_sessions: Tuple[_dt.date, ...] = sessions
    truncated = False
    if not is_earliest_known and earliest_available_date is not None:
        earliest = _as_date(earliest_available_date)
        checked_sessions = tuple(day for day in sessions if day >= earliest)
        truncated = len(checked_sessions) != len(sessions)

    # A window that EXISTED but was entirely cut off by the symbol's known availability has no
    # observation at all -> unverified (§5.4: those sessions are "not looked at", never
    # `missing_session`). A window with ZERO sessions in the first place is a different thing:
    # there is nothing to check, so the loop below runs over it and the entry is `complete` with
    # `sessions_in_window == 0` (C4 holds: 0 + 0 + 0 == 0). Collapsing that empty window into
    # `unverified` would make a caller-supplied empty window look like a failed run.
    if truncated and not checked_sessions:
        return unverified_symbol(
            symbol, window, bars_refused=bars_refused, detected_at=detected_at
        )

    records: List[Dict[str, Any]] = []
    verified_zero_dates: List[str] = []
    present = missing = null_volume = zero_volume = 0

    # §2.3 order is normative: existence FIRST, value SECOND. Testing `isna` first would give a
    # NaN for an absent row (the read of a missing key) and silently collapse
    # `missing_session` into `null_volume` -- the data-layer twin of the cell-level NaN-vs-0.0
    # trap in `AGENTS.md`.
    for day in checked_sessions:
        if day not in volumes:
            missing += 1
            records.append(
                _record(
                    symbol,
                    day,
                    MISSING_SESSION,
                    {"row_present": False, **calendar_evidence},
                    source_step,
                    stamp,
                )
            )
            continue

        present += 1
        value = volumes[day]
        if pd.isna(value):
            null_volume += 1
            evidence = {
                "row_present": True,
                "volume_is_null": True,
                "amount_is_null": None,
                "refusal": "NullVolumeRefused",
            }
            if amount_map is not None:
                evidence["amount_is_null"] = (
                    bool(pd.isna(amount_map[day])) if day in amount_map else None
                )
            records.append(
                _record(symbol, day, NULL_VOLUME, evidence, source_step, stamp)
            )
        elif float(value) == 0.0:
            # A real no-trade session: it is DATA, not a gap (§3.2 item 1). It stays out of
            # `records` and is instead listed in `verified_zero_dates`.
            zero_volume += 1
            verified_zero_dates.append(day.isoformat())
        # else: a normal session; it belongs to no marker (§2.1 "每一对都穷尽不了全集").

    counts: Dict[str, Any] = {
        "sessions_in_window": len(sessions),
        "sessions_checked": len(checked_sessions),
        "bars_present": present,
        "bars_refused": bars_refused,
        "missing_sessions": missing,
        "null_volume_bars": null_volume,
        "zero_volume_bars": zero_volume,
        "missing_sessions_lower_bound": truncated,
    }

    entry = {
        "symbol": symbol,
        "coverage": _coverage_state(counts),
        "counts": counts,
        "records": records,
        "verified_zero_dates": verified_zero_dates,
    }
    verify_coverage_entry(entry)
    return entry


def _coverage_state(counts: Mapping[str, Any]) -> str:
    """§3.2 aggregation, derived ONLY from the row-level counts (never judged separately).

    `unverified` outranks `gapped` outranks `complete`: a partial check that already found a
    hole is still "not finished", and calling it `gapped` would let an interrupted run look
    like a measured result (§3.2 item 2).
    """
    if (
        counts["sessions_checked"] is None
        or counts["missing_sessions"] is None
        or counts["null_volume_bars"] is None
        or counts["sessions_checked"] != counts["sessions_in_window"]
    ):
        return UNVERIFIED
    if counts["missing_sessions"] + counts["null_volume_bars"] >= 1:
        return GAPPED
    return COMPLETE


def complete_sessions(counts: Mapping[str, Any]) -> Optional[int]:
    """§5.3 C4's derived term: `sessions_in_window - missing_sessions - null_volume_bars`.

    `None` when the inputs are UNKNOWN. `zero_volume_bars` is intentionally part of this term:
    a verified zero is a completed session.
    """
    if (
        counts.get("sessions_in_window") is None
        or counts.get("missing_sessions") is None
        or counts.get("null_volume_bars") is None
    ):
        return None
    return (
        int(counts["sessions_in_window"])
        - int(counts["missing_sessions"])
        - int(counts["null_volume_bars"])
    )


def verify_coverage_entry(entry: Mapping[str, Any]) -> None:
    """Enforce §5.3 C1/C2/C4 on one computed entry; raise `CoverageContractError` otherwise.

    This is executed by `assess_symbol_coverage` itself, so the identity cannot drift between
    the report and its documentation. It only applies where C4 applies: `sessions_checked ==
    sessions_in_window` (C2 explicitly forbids treating a lower bound as exact).

    Rule 1: `complete_sessions + missing_sessions + null_volume_bars == sessions_in_window`.
    Rule 2: `zero_volume_bars <= complete_sessions` -- since `verified_zero_dates` has exactly
    `zero_volume_bars` entries, this is the machine-checkable proof that a real zero was not
    counted as a gap.
    """
    counts = entry.get("counts") or {}
    coverage = entry.get("coverage")
    checked = counts.get("sessions_checked")
    in_window = counts.get("sessions_in_window")

    if coverage == UNVERIFIED:
        if checked is None:
            # Never inspected: no numeric read-side count and no records may pretend otherwise
            # (C1/G6). A PARTIALLY checked window (e.g. cut off by the symbol's known
            # availability) legitimately carries numbers for the span it did inspect, together
            # with `missing_sessions_lower_bound=True`.
            if counts.get("missing_sessions") is not None:
                raise CoverageContractError(
                    f"{entry.get('symbol')}: never-checked entry must report "
                    f"missing_sessions=None, got {counts.get('missing_sessions')!r} (C1)"
                )
            if entry.get("records"):
                raise CoverageContractError(
                    f"{entry.get('symbol')}: never-checked entry carries "
                    f"{len(entry['records'])} record(s); an observation nobody made cannot exist"
                )
        if checked is not None and in_window is not None and checked > in_window:
            raise CoverageContractError(
                f"{entry.get('symbol')}: sessions_checked ({checked}) > "
                f"sessions_in_window ({in_window})"
            )
        return

    if checked is None or in_window is None:
        raise CoverageContractError(
            f"{entry.get('symbol')}: coverage={coverage!r} requires sessions_checked and "
            f"sessions_in_window (C1)"
        )
    if checked != in_window:
        return  # C2: a lower bound is not an exact value, so C4 does not apply.

    derived = complete_sessions(counts)
    if derived is None:
        raise CoverageContractError(
            f"{entry.get('symbol')}: C4 needs numeric sessions_in_window / missing_sessions / "
            "null_volume_bars; got None"
        )
    total = derived + int(counts["missing_sessions"]) + int(counts["null_volume_bars"])
    if total != in_window:
        raise CoverageContractError(
            f"{entry.get('symbol')}: C4 identity broken -- complete {derived} + missing "
            f"{counts['missing_sessions']} + null_volume {counts['null_volume_bars']} = "
            f"{total} != sessions_in_window {in_window}"
        )
    if int(counts["zero_volume_bars"] or 0) > derived:
        raise CoverageContractError(
            f"{entry.get('symbol')}: zero_volume_bars {counts['zero_volume_bars']} exceeds "
            f"complete_sessions {derived} -- a verified zero was counted as a gap"
        )
    zero_dates = entry.get("verified_zero_dates") or []
    if len(zero_dates) != int(counts["zero_volume_bars"] or 0):
        raise CoverageContractError(
            f"{entry.get('symbol')}: verified_zero_dates has {len(zero_dates)} entries but "
            f"zero_volume_bars is {counts['zero_volume_bars']}"
        )
    if any(rec.get("state") == ZERO_VOLUME for rec in entry.get("records") or []):
        raise CoverageContractError(
            f"{entry.get('symbol')}: zero_volume must never appear in records (§9.2 G2/G3)"
        )


# ---------------------------------------------------------------------------------------------
# Single-symbol convenience entry point
# ---------------------------------------------------------------------------------------------


def assess_coverage(
    symbol: str,
    *,
    window_start: Any,
    window_end: Any,
    calendar: Union[MarketClock, Iterable[Any], None],
    bars: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """`resolve_window_calendar` + `assess_symbol_coverage` for one symbol.

    `calendar` has no default on purpose (§2.2): the caller states its calendar authority.
    """
    window = resolve_window_calendar(window_start, window_end, calendar)
    return assess_symbol_coverage(symbol, window, bars=bars, **kwargs)


# ---------------------------------------------------------------------------------------------
# Batch report (§5.3, §6.2)
# ---------------------------------------------------------------------------------------------


def _sum_known(values: Iterable[Optional[int]]) -> Optional[int]:
    """Sum the KNOWN values; `None` only when nothing is known (never a fake `0`, C1).

    When part of the batch is unverified the sum is a *lower bound*; `build_report` marks that
    with `missing_sessions_lower_bound=True` instead of pretending the total is exact (C2).
    """
    known = [int(v) for v in values if v is not None]
    if not known:
        return None
    return int(sum(known))


def build_report(
    symbols: Sequence[str],
    *,
    window_start: Any,
    window_end: Any,
    calendar: Union[MarketClock, Iterable[Any], None],
    assessed: Union[Mapping[str, Mapping[str, Any]], Iterable[Mapping[str, Any]], None] = None,
    aborted_at: Optional[str] = None,
    abort_reason: Optional[str] = None,
    detected_at: Union[_dt.datetime, str, None] = None,
) -> Dict[str, Any]:
    """Assemble the frozen `data_gap_report/v1` structure (§5.3, §6.2).

    `symbols` is the **denominator** (`symbols_requested`); `assessed` holds the entries that
    were actually produced, keyed by symbol (or an iterable of entries). A requested symbol with
    no entry is NOT treated as "no gap" -- it becomes `unverified` with `missing_sessions=None`
    (§6.2 item 4, §9.2 G6: 真空成立 is the failure mode where the worst day looks the cleanest).

    `aborted_at` marks where processing stopped. Symbols at/after that index keep a *fully
    checked* entry if one was supplied (§6.2 item 1 -- a refusal after the write still counts),
    otherwise they are reported `unverified`. Symbols before it are never overridden.

    Totals: the four symbol counts are always present; the per-session counters are summed over
    the known values and `missing_sessions_lower_bound` is set whenever any requested symbol
    did not complete, so a partial total can never read as exact. `bars_refused` and
    `missing_sessions` remain separate fields -- C3 forbids a combined "gaps" total, and none
    is emitted.
    """
    window = resolve_window_calendar(window_start, window_end, calendar)
    requested = list(symbols)
    if len(set(requested)) != len(requested):
        raise ValueError("symbols must not contain duplicates")

    supplied: Dict[str, Mapping[str, Any]] = {}
    if assessed is not None:
        items = assessed.items() if isinstance(assessed, Mapping) else ((e["symbol"], e) for e in assessed)
        for key, entry in items:
            supplied[str(key)] = entry
    unknown = [sym for sym in supplied if sym not in requested]
    if unknown:
        raise ValueError(f"assessed contains symbols that were not requested: {sorted(unknown)}")

    if aborted_at is not None and aborted_at not in requested:
        raise ValueError(f"aborted_at {aborted_at!r} is not one of the requested symbols")
    abort_index = requested.index(aborted_at) if aborted_at is not None else len(requested)

    entries: List[Dict[str, Any]] = []
    attempted = 0
    for position, symbol in enumerate(requested):
        entry = supplied.get(symbol)
        at_or_after_abort = position >= abort_index
        if entry is not None:
            attempted += 1
            fully_checked = entry.get("coverage") in (COMPLETE, GAPPED)
            if at_or_after_abort and not fully_checked:
                entry = unverified_symbol(symbol, window, detected_at=detected_at)
        else:
            # Never supplied => never inspected. This is the only branch allowed to invent an
            # entry, and it can ONLY invent `unverified`.
            entry = unverified_symbol(symbol, window, detected_at=detected_at)
        entries.append(dict(entry))

    completed = sum(1 for e in entries if e["coverage"] in (COMPLETE, GAPPED))
    unverified = sum(1 for e in entries if e["coverage"] == UNVERIFIED)

    def column(name: str) -> List[Optional[int]]:
        return [e["counts"].get(name) for e in entries]

    # Precision flag: exact only if every requested symbol was fully checked AND every count is
    # known. Any `unverified` entry makes `missing_sessions` a lower bound (C2).
    exact = unverified == 0 and all(v is not None for v in column("missing_sessions"))

    totals = {
        "symbols_requested": len(requested),
        "symbols_attempted": attempted,
        "symbols_completed": completed,
        "symbols_unverified": unverified,
        "sessions_in_window": _sum_known(column("sessions_in_window")),
        "sessions_checked": _sum_known(column("sessions_checked")),
        "bars_present": _sum_known(column("bars_present")),
        "bars_refused": _sum_known(column("bars_refused")),
        "missing_sessions": _sum_known(column("missing_sessions")),
        "null_volume_bars": _sum_known(column("null_volume_bars")),
        "zero_volume_bars": _sum_known(column("zero_volume_bars")),
        "missing_sessions_lower_bound": not exact,
    }

    return {
        "schema": REPORT_SCHEMA,
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "calendar": {
            "authority": window.authority,
            "market": window.market,
            "status": window.status,
        },
        "abort": {
            "aborted": aborted_at is not None,
            "at_symbol": aborted_at,
            "reason": abort_reason,
        },
        "totals": totals,
        "symbols": entries,
    }

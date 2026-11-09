"""Intraday 5m coverage / completeness / cross-comparability probes (`docs/development_plan.md` `M3.11`).

WHY THIS MODULE EXISTS
----------------------
`M3.11` (2026-11-01) asked for a *machine-checkable* registration of the `511880.SH` 5m
back-fill interval plus three acceptance questions. "登记" alone would be unfalsifiable:
a stored `bar_count` is a number someone typed, and the failure it is meant to prevent --
silently running a calibration / backtest on a short cash-leg history and calling the
result "the" result -- happens precisely when nobody recomputes it. So the registration is
paired with three pure predicates here and one read-only CLI
(`scripts/report_intraday_coverage.py`) that recomputes all three from the cache.

The three questions (verbatim intent from `M3.11` §2):

  (i)   coverage        -- does the observed session set cover the requested target window?
  (ii)  daily completeness -- is every session a *full* 48-bar day, with no bar inside the
                           11:30-13:00 A-share lunch break and no duplicated timestamp?
  (iii) cross-comparability -- do two 5m series share the same bar grid over their overlap
                           (so the cash leg is not measured on a different sampling grid
                           than the equity legs)?

TRI-STATE, NEVER A SILENT PASS (the contract)
---------------------------------------------
Every report is `pass` / `fail` / `insufficient`, and every report carries
`available: bool` plus a machine-readable `reason` string. Empty input, a missing
timestamp column, an empty calendar, a non-overlapping window pair -- all of these return
`insufficient` with `available=False` and a reason, *never* `pass`, and *never* a `0.0`
that a caller could mistake for a measured value. This mirrors the fail-closed rule in
`M3.11` §3: a short/unknown interval must be refused by the consumer, not silently
tolerated.

HONEST LIMITS OF THE PREDICATES
-------------------------------
* Exchange holidays are **not** known here. Without an explicit `trading_calendar` the
  expected session set is "every weekday in the window" (`calendar_basis="weekday_approx"`),
  which over-counts around holidays and therefore can only produce a *false FAIL*, never a
  false PASS. That asymmetry is deliberate: the error direction is fail-closed.
* "停牌 vs 缺失" cannot be decided from a bar table alone -- a suspended session and a
  dropped session look identical. The caller supplies `suspended_sessions` (from the daily
  bar table / a suspension calendar, per `M3.3` §3 and `M3.10` §3); sessions that are
  missing while *not* declared suspended are the ones that count against the verdict.
* Nothing here reads, writes or opens a database. The CLI is the only IO and it opens
  read-only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

import pandas as pd

#: Bars BaoStock serves per *closed* A-share 5m session.
#:
#: Verified against `common/financial_api.py:1879`:
#: `BaoStockDataProvider._BAOSTOCK_BARS_PER_DAY = {"5": 48, "15": 16, "30": 8, "60": 4}`.
#: The number is not a BaoStock quirk -- it is the exchange grid: 09:35..11:30 (24 bars) +
#: 13:05..15:00 (24 bars) = 48 five-minute bars for a 4-hour A-share session. `M3.10`'s
#: per-day completeness assertion uses the same constant, and `M3.3` §3 pinned the
#: "no 11:30-13:00 bar" half of the contract.
BARS_PER_SESSION_5M = 48

#: A-share lunch break, market-local, half-open on both ends in the sense that a bar
#: stamped exactly 11:30 is the *last morning* bar and 13:05 is the *first afternoon* bar;
#: anything strictly between the two boundary stamps is a violation.
LUNCH_BREAK_START = dt.time(11, 30)
LUNCH_BREAK_END = dt.time(13, 0)

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INSUFFICIENT = "insufficient"

#: Cap on how many offending timestamps a report carries inline (reports are printed and
#: compared; an unbounded tuple would make a broken 100k-bar frame unreadable).
_MAX_LISTED = 10


class IntradayCoverageError(RuntimeError):
    """Raised by `require_pass` -- the fail-closed door for consumers.

    `M3.11` §3 forbids both "先跑通再补记" and silently substituting a short window for the
    full one. A consumer that wants the cash leg must call `require_pass(...)` and let this
    propagate; the alternative (check `report.status` by hand and forget once) is exactly how
    the short-window result would leak into a calibration number.
    """


class _Insufficient(Exception):
    """Internal: raised by a predicate body and turned into an `insufficient` report."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------------------
# frame normalisation
# --------------------------------------------------------------------------------------
def _timestamp_series(frame: Any) -> pd.Series:
    """Market-local, tz-naive `datetime64[ns]` timestamps from a frame.

    Accepts a `DataFrame` with a `ts` (or `date`) column, or a `DatetimeIndex`. Raises
    `_Insufficient` for anything else so callers get a reason instead of a traceback.
    """
    if frame is None:
        raise _Insufficient("empty_input")
    if isinstance(frame, pd.DataFrame):
        if frame.empty:
            raise _Insufficient("empty_input")
        if "ts" in frame.columns:
            raw = frame["ts"]
        elif "date" in frame.columns:
            raw = frame["date"]
        elif isinstance(frame.index, pd.DatetimeIndex):
            raw = pd.Series(frame.index, index=frame.index)
        else:
            raise _Insufficient("no_timestamp_column")
    elif isinstance(frame, pd.Series):
        if frame.empty:
            raise _Insufficient("empty_input")
        raw = frame
    else:
        raise _Insufficient("unsupported_input_type")

    stamps = pd.to_datetime(raw, errors="coerce")
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    stamps = stamps.dropna()
    if stamps.empty:
        raise _Insufficient("no_parseable_timestamps")
    return stamps.reset_index(drop=True)


def _as_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _date_set(values: Optional[Iterable[Any]]) -> Tuple[dt.date, ...]:
    if values is None:
        return ()
    out = []
    for item in values:
        d = _as_date(item)
        if d is not None:
            out.append(d)
    return tuple(sorted(set(out)))


def _weekdays(start: dt.date, end: dt.date) -> Tuple[dt.date, ...]:
    """Weekday calendar -- disclosed approximation, blind to exchange holidays."""
    return tuple(
        d.date()
        for d in pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
        if d.weekday() < 5
    )


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


# --------------------------------------------------------------------------------------
# (i) coverage
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageReport:
    """Does the observed session set cover the requested window?

    `status` is `pass`/`fail`/`insufficient`; `available=False` iff `insufficient`.
    `missing_sessions` is every expected-but-absent session; `unexplained_missing` is that
    minus the caller-declared `suspended_sessions` -- only the latter counts against the
    verdict, which is how "停牌" is separated from "缺失" without guessing.
    `expected_sessions` (and therefore the `coverage_ratio` denominator and `expected_bars`)
    already excludes declared suspensions: a suspended session has no bars by definition, so
    leaving it in the denominator would make `suspended_sessions` decorative.
    """

    symbol: str
    interval: str
    requested_start: Optional[dt.date]
    requested_end: Optional[dt.date]
    calendar_basis: str
    actual_start: Optional[pd.Timestamp]
    actual_end: Optional[pd.Timestamp]
    bar_count: int
    bars_outside_window: int
    expected_sessions: Tuple[dt.date, ...]
    observed_sessions: Tuple[dt.date, ...]
    missing_sessions: Tuple[dt.date, ...]
    unexplained_missing: Tuple[dt.date, ...]
    suspended_sessions: Tuple[dt.date, ...]
    coverage_ratio: float
    min_coverage_ratio: float
    expected_bars: int
    status: str
    available: bool
    reason: str

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        for key in ("expected_sessions", "observed_sessions", "missing_sessions",
                    "unexplained_missing", "suspended_sessions"):
            out[key] = [d.isoformat() for d in out[key]]
        for key in ("actual_start", "actual_end", "requested_start", "requested_end"):
            out[key] = None if out[key] is None else str(out[key])
        return out


def _insufficient_coverage(
    reason: str,
    *,
    symbol: str,
    interval: str,
    start: Optional[dt.date],
    end: Optional[dt.date],
    min_coverage_ratio: float,
    calendar_basis: str = "unknown",
    bar_count: int = 0,
) -> CoverageReport:
    return CoverageReport(
        symbol=symbol,
        interval=interval,
        requested_start=start,
        requested_end=end,
        calendar_basis=calendar_basis,
        actual_start=None,
        actual_end=None,
        bar_count=bar_count,
        bars_outside_window=0,
        expected_sessions=(),
        observed_sessions=(),
        missing_sessions=(),
        unexplained_missing=(),
        suspended_sessions=(),
        coverage_ratio=0.0,
        min_coverage_ratio=float(min_coverage_ratio),
        expected_bars=0,
        status=STATUS_INSUFFICIENT,
        available=False,
        reason=reason,
    )


def check_coverage(
    frame: Any,
    symbol: str,
    start: Any,
    end: Any,
    interval: str = "5m",
    *,
    trading_calendar: Optional[Iterable[Any]] = None,
    suspended_sessions: Optional[Iterable[Any]] = None,
    min_coverage_ratio: float = 1.0,
) -> CoverageReport:
    """Question (i): coverage ratio of the observed sessions against the target window.

    `trading_calendar` -- the authoritative session list (e.g. from `ohlcv_bars` or an
    exchange calendar). When omitted, every weekday in `[start, end]` is expected and
    `calendar_basis="weekday_approx"` says so in the report. The approximation can only
    depress the ratio, never inflate it (holidays are not skipped), so a `pass` under it
    is a *stronger* claim than a pass under an explicit calendar.

    `min_coverage_ratio` -- numeric pass threshold on `coverage_ratio`. Default `1.0`
    because substituting a short window for a full one is exactly the silent failure
    `M3.11` §3 forbids; a caller who deliberately accepts a shorter window must say so by
    passing a lower threshold, which then shows up in the report.
    """
    req_start, req_end = _as_date(start), _as_date(end)
    suspended = _date_set(suspended_sessions)
    if req_start is None or req_end is None:
        return _insufficient_coverage(
            "unparseable_window", symbol=symbol, interval=interval,
            start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
        )
    if req_start > req_end:
        return _insufficient_coverage(
            "window_reversed", symbol=symbol, interval=interval,
            start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
        )

    if trading_calendar is None:
        expected = _weekdays(req_start, req_end)
        basis = "weekday_approx"
        if not expected:
            return _insufficient_coverage(
                "empty_window", symbol=symbol, interval=interval,
                start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
                calendar_basis=basis,
            )
    else:
        basis = "explicit_calendar"
        expected = tuple(d for d in _date_set(trading_calendar) if req_start <= d <= req_end)
        if not expected:
            return _insufficient_coverage(
                "calendar_empty_in_window", symbol=symbol, interval=interval,
                start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
                calendar_basis=basis,
            )

    try:
        stamps = _timestamp_series(frame)
    except _Insufficient as exc:
        return _insufficient_coverage(
            exc.reason, symbol=symbol, interval=interval,
            start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
            calendar_basis=basis,
        )

    sessions = stamps.dt.date
    start_ts, end_ts = pd.Timestamp(req_start), pd.Timestamp(req_end) + pd.Timedelta(days=1)
    in_window = (stamps >= start_ts) & (stamps < end_ts)
    observed = tuple(sorted(set(sessions[in_window])))
    missing = tuple(d for d in expected if d not in set(observed))
    unexplained = tuple(d for d in missing if d not in set(suspended))

    # A declared suspension is a session that legitimately has no bars, so it leaves the
    # denominator as well as the numerator -- otherwise a correct suspension list could
    # never lift the ratio to 1.0 and `suspended_sessions` would be decorative.
    suspended_in_window = tuple(d for d in suspended if req_start <= d <= req_end)
    effective = tuple(d for d in expected if d not in set(suspended))
    if not effective:
        return _insufficient_coverage(
            "all_sessions_suspended", symbol=symbol, interval=interval,
            start=req_start, end=req_end, min_coverage_ratio=min_coverage_ratio,
            calendar_basis=basis, bar_count=int(len(stamps)),
        )
    effective_set = set(effective)
    hit = len([d for d in observed if d in effective_set])
    ratio = _ratio(hit, len(effective))

    status = STATUS_PASS if ratio >= float(min_coverage_ratio) and not unexplained else STATUS_FAIL
    reason = "ok" if status == STATUS_PASS else (
        "unexplained_missing_sessions" if unexplained else "coverage_ratio_below_threshold"
    )
    return CoverageReport(
        symbol=symbol,
        interval=interval,
        requested_start=req_start,
        requested_end=req_end,
        calendar_basis=basis,
        actual_start=stamps.min(),
        actual_end=stamps.max(),
        bar_count=int(len(stamps)),
        bars_outside_window=int((~in_window).sum()),
        expected_sessions=effective,
        observed_sessions=observed,
        missing_sessions=missing,
        unexplained_missing=unexplained,
        suspended_sessions=suspended_in_window,
        coverage_ratio=ratio,
        min_coverage_ratio=float(min_coverage_ratio),
        expected_bars=len(effective) * BARS_PER_SESSION_5M,
        status=status,
        available=True,
        reason=reason,
    )


# --------------------------------------------------------------------------------------
# (ii) per-session completeness
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletenessReport:
    """Question (ii): is every session a full 48-bar day, on-grid and duplicate-free?"""

    symbol: str
    bars_per_session: int
    sessions: Tuple[dt.date, ...]
    session_bar_counts: Mapping[dt.date, int]
    short_sessions: Mapping[dt.date, int]
    overfull_sessions: Mapping[dt.date, int]
    duplicate_ts_count: int
    duplicate_ts: Tuple[pd.Timestamp, ...]
    lunch_break_violations: int
    lunch_break_ts: Tuple[pd.Timestamp, ...]
    status: str
    available: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bars_per_session": self.bars_per_session,
            "sessions": [d.isoformat() for d in self.sessions],
            "session_bar_counts": {d.isoformat(): n for d, n in self.session_bar_counts.items()},
            "short_sessions": {d.isoformat(): n for d, n in self.short_sessions.items()},
            "overfull_sessions": {
                d.isoformat(): n for d, n in self.overfull_sessions.items()
            },
            "duplicate_ts_count": self.duplicate_ts_count,
            "duplicate_ts": [str(t) for t in self.duplicate_ts],
            "lunch_break_violations": self.lunch_break_violations,
            "lunch_break_ts": [str(t) for t in self.lunch_break_ts],
            "status": self.status,
            "available": self.available,
            "reason": self.reason,
        }


def check_daily_completeness(
    frame: Any,
    symbol: str = "",
    bars_per_session: int = BARS_PER_SESSION_5M,
) -> CompletenessReport:
    """"A whole missing day and a short day look identical in an aggregate row count" --
    so this counts bars *per session*, then flags short days, over-full days, duplicate
    timestamps, and any bar stamped strictly inside 11:30-13:00.

    A short session is reported but not interpreted: whether it is a half-day, a suspension
    or a dropped chunk needs the daily table (`M3.10` §3). Over-full sessions are flagged
    too, because 49 bars on a 48-bar grid means a duplicate or an off-grid stamp -- the same
    class of defect as a short day, just in the other direction.
    """
    try:
        stamps = _timestamp_series(frame)
    except _Insufficient as exc:
        return CompletenessReport(
            symbol=symbol, bars_per_session=bars_per_session, sessions=(),
            session_bar_counts={}, short_sessions={}, overfull_sessions={},
            duplicate_ts_count=0, duplicate_ts=(), lunch_break_violations=0,
            lunch_break_ts=(), status=STATUS_INSUFFICIENT, available=False,
            reason=exc.reason,
        )
    if bars_per_session <= 0:
        return CompletenessReport(
            symbol=symbol, bars_per_session=bars_per_session, sessions=(),
            session_bar_counts={}, short_sessions={}, overfull_sessions={},
            duplicate_ts_count=0, duplicate_ts=(), lunch_break_violations=0,
            lunch_break_ts=(), status=STATUS_INSUFFICIENT, available=False,
            reason="non_positive_bars_per_session",
        )

    counts = stamps.dt.date.value_counts().sort_index()
    session_counts = {d: int(n) for d, n in counts.items()}
    short = {d: n for d, n in session_counts.items() if n < bars_per_session}
    overfull = {d: n for d, n in session_counts.items() if n > bars_per_session}

    dup_mask = stamps.duplicated(keep=False)
    dup_values = stamps[dup_mask]
    dup_count = int(dup_mask.sum())

    times = stamps.dt.time
    lunch_mask = (times > LUNCH_BREAK_START) & (times < LUNCH_BREAK_END)
    lunch_values = stamps[lunch_mask]

    ok = not short and not overfull and dup_count == 0 and int(lunch_mask.sum()) == 0
    if ok:
        reason = "ok"
    elif short:
        reason = "short_sessions"
    elif overfull:
        reason = "overfull_sessions"
    elif dup_count:
        reason = "duplicate_timestamps"
    else:
        reason = "lunch_break_bars"

    return CompletenessReport(
        symbol=symbol,
        bars_per_session=bars_per_session,
        sessions=tuple(sorted(session_counts)),
        session_bar_counts=session_counts,
        short_sessions=short,
        overfull_sessions=overfull,
        duplicate_ts_count=dup_count,
        duplicate_ts=tuple(dup_values.head(_MAX_LISTED)),
        lunch_break_violations=int(lunch_mask.sum()),
        lunch_break_ts=tuple(lunch_values.head(_MAX_LISTED)),
        status=STATUS_PASS if ok else STATUS_FAIL,
        available=True,
        reason=reason,
    )


# --------------------------------------------------------------------------------------
# (iii) cross comparability
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ComparabilityReport:
    """Question (iii): do two 5m series share one bar grid over their overlap window?"""

    label_a: str
    label_b: str
    window_a: Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]
    window_b: Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]
    overlap_start: Optional[pd.Timestamp]
    overlap_end: Optional[pd.Timestamp]
    overlap_sessions_a: int
    overlap_sessions_b: int
    shared_count: int
    bar_ratio: float
    tolerance: float
    min_shared_bars: int
    status: str
    available: bool
    reason: str

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        for key in ("window_a", "window_b"):
            out[key] = [None if v is None else str(v) for v in out[key]]
        for key in ("overlap_start", "overlap_end"):
            out[key] = None if out[key] is None else str(out[key])
        return out


def check_cross_comparability(
    frame_a: Any,
    frame_b: Any,
    tolerance: float = 0.99,
    *,
    label_a: str = "a",
    label_b: str = "b",
    min_shared_bars: int = BARS_PER_SESSION_5M,
) -> ComparabilityReport:
    """Question (iii): shared-timestamp ratio over the overlap, against `tolerance`.

    The cash leg (`511880.SH`) is only comparable to the equity legs if, over the shared
    window, both sit on the *same* 5m grid. `bar_ratio = shared / min(bars_a, bars_b)` in the
    overlap -- `min` rather than `union` so that a genuinely longer history on one side is
    not punished, while a grid mismatch (one series missing every other bar, or stamped at
    a different minute offset) still drags the ratio down. `min_shared_bars` (default one
    full session) refuses to declare comparability from a handful of coincident stamps.

    Non-overlapping windows are `insufficient`, not `fail`: "cannot compare" and "compared
    and mismatched" are different verdicts and must not share a code path.
    """
    try:
        stamps_a = _timestamp_series(frame_a)
        stamps_b = _timestamp_series(frame_b)
    except _Insufficient as exc:
        return ComparabilityReport(
            label_a=label_a, label_b=label_b, window_a=(None, None), window_b=(None, None),
            overlap_start=None, overlap_end=None, overlap_sessions_a=0, overlap_sessions_b=0,
            shared_count=0, bar_ratio=0.0, tolerance=float(tolerance),
            min_shared_bars=int(min_shared_bars), status=STATUS_INSUFFICIENT,
            available=False, reason=exc.reason,
        )

    start = max(stamps_a.min(), stamps_b.min())
    stop = min(stamps_a.max(), stamps_b.max())
    if start > stop:
        return ComparabilityReport(
            label_a=label_a, label_b=label_b,
            window_a=(stamps_a.min(), stamps_a.max()),
            window_b=(stamps_b.min(), stamps_b.max()),
            overlap_start=None, overlap_end=None, overlap_sessions_a=0,
            overlap_sessions_b=0, shared_count=0, bar_ratio=0.0,
            tolerance=float(tolerance), min_shared_bars=int(min_shared_bars),
            status=STATUS_INSUFFICIENT, available=False, reason="no_overlapping_window",
        )

    in_a = set(stamps_a[(stamps_a >= start) & (stamps_a <= stop)])
    in_b = set(stamps_b[(stamps_b >= start) & (stamps_b <= stop)])
    shared = len(in_a & in_b)
    ratio = _ratio(shared, min(len(in_a), len(in_b)))
    sessions_a = len({t.date() for t in in_a})
    sessions_b = len({t.date() for t in in_b})

    if shared == 0:
        return ComparabilityReport(
            label_a=label_a, label_b=label_b,
            window_a=(stamps_a.min(), stamps_a.max()),
            window_b=(stamps_b.min(), stamps_b.max()),
            overlap_start=start, overlap_end=stop,
            overlap_sessions_a=sessions_a, overlap_sessions_b=sessions_b,
            shared_count=0, bar_ratio=0.0, tolerance=float(tolerance),
            min_shared_bars=int(min_shared_bars), status=STATUS_INSUFFICIENT,
            available=False, reason="no_shared_timestamps",
        )

    if shared < int(min_shared_bars):
        status, reason = STATUS_INSUFFICIENT, "below_min_shared_bars"
    elif ratio >= float(tolerance):
        status, reason = STATUS_PASS, "ok"
    else:
        status, reason = STATUS_FAIL, "bar_grid_mismatch"

    return ComparabilityReport(
        label_a=label_a, label_b=label_b,
        window_a=(stamps_a.min(), stamps_a.max()),
        window_b=(stamps_b.min(), stamps_b.max()),
        overlap_start=start, overlap_end=stop,
        overlap_sessions_a=sessions_a, overlap_sessions_b=sessions_b,
        shared_count=shared, bar_ratio=ratio, tolerance=float(tolerance),
        min_shared_bars=int(min_shared_bars), status=status,
        available=status != STATUS_INSUFFICIENT, reason=reason,
    )


# --------------------------------------------------------------------------------------
# start-date spread (the "is the cash leg systematically shorter?" half of question iii)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class StartSpread:
    """Where one symbol's first session sits against a peer cohort."""

    symbol: str
    starts: Mapping[str, dt.date]
    earliest_symbol: Optional[str]
    earliest_start: Optional[dt.date]
    median_start: Optional[dt.date]
    symbol_sessions_behind_earliest: Optional[int]
    peers_shorter_than_symbol: Tuple[str, ...]
    status: str
    available: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "starts": {k: v.isoformat() for k, v in self.starts.items()},
            "earliest_symbol": self.earliest_symbol,
            "earliest_start": None if self.earliest_start is None else self.earliest_start.isoformat(),
            "median_start": None if self.median_start is None else self.median_start.isoformat(),
            "symbol_sessions_behind_earliest": self.symbol_sessions_behind_earliest,
            "peers_shorter_than_symbol": list(self.peers_shorter_than_symbol),
            "status": self.status,
            "available": self.available,
            "reason": self.reason,
        }


def start_date_spread(
    symbol: str,
    starts: Mapping[str, Any],
    *,
    calendar: Optional[Iterable[Any]] = None,
) -> StartSpread:
    """Compare `symbol`'s first session against the peer cohort's earliest and median.

    `calendar` (optional) is the session list used to convert the date gap into "how many
    sessions behind"; without it the field is `None` rather than a weekday approximation,
    because an inflated gap would read as a real finding.
    """
    parsed = {k: _as_date(v) for k, v in (starts or {}).items()}
    parsed = {k: v for k, v in parsed.items() if v is not None}
    if not parsed:
        return StartSpread(symbol=symbol, starts={}, earliest_symbol=None,
                           earliest_start=None, median_start=None,
                           symbol_sessions_behind_earliest=None,
                           peers_shorter_than_symbol=(), status=STATUS_INSUFFICIENT,
                           available=False, reason="no_start_dates")
    if symbol not in parsed:
        return StartSpread(symbol=symbol, starts=parsed, earliest_symbol=None,
                           earliest_start=None, median_start=None,
                           symbol_sessions_behind_earliest=None,
                           peers_shorter_than_symbol=(), status=STATUS_INSUFFICIENT,
                           available=False, reason="symbol_missing")

    # Tie-break on symbol: two series can share a first session, and "earliest"
    # must not depend on dict/SQL row order or the registration would flicker.
    ordered = sorted(parsed.items(), key=lambda kv: (kv[1], kv[0]))
    earliest_symbol, earliest_start = ordered[0]
    series = pd.Series([pd.Timestamp(v) for v in parsed.values()])
    median_start = series.median().date()
    own_start = parsed[symbol]
    behind: Optional[int] = None
    if calendar is not None:
        sessions = [d for d in _date_set(calendar) if own_start <= d <= earliest_start]
        behind = max(len(sessions) - 1, 0) if sessions else 0
    peers_shorter = tuple(sorted(k for k, v in parsed.items() if k != symbol and v > own_start))
    return StartSpread(
        symbol=symbol, starts=dict(parsed), earliest_symbol=earliest_symbol,
        earliest_start=earliest_start, median_start=median_start,
        symbol_sessions_behind_earliest=behind,
        peers_shorter_than_symbol=peers_shorter,
        status=STATUS_PASS if own_start <= median_start else STATUS_FAIL,
        available=True,
        reason="at_or_before_median_start" if own_start <= median_start
        else "starts_after_median",
    )


# --------------------------------------------------------------------------------------
# fail-closed door
# --------------------------------------------------------------------------------------
def require_pass(report: Any, *, context: str = "") -> Any:
    """Return `report` unchanged if it passed, else raise `IntradayCoverageError`.

    Shared by all three report types (they all carry `status`, `reason`, `available`).
    `insufficient` raises too: "cannot tell" must never be usable as "fine" -- that is the
    `M3.11` §3 fail-closed requirement, and it is why this lives here instead of being left
    to each consumer to re-implement.
    """
    status = getattr(report, "status", None)
    if status == STATUS_PASS:
        return report
    reason = getattr(report, "reason", "unknown")
    where = f"{context}: " if context else ""
    raise IntradayCoverageError(
        f"{where}intraday coverage check did not pass (status={status}, reason={reason})"
    )

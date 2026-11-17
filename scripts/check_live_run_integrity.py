#!/usr/bin/env python3
"""Read-only integrity auditor for a live-trading run archive.

WHY THIS EXISTS (2026-11-13 -> 2026-11-16 incident)
===================================================
The Friday 2026-11-13 signal never traded.  The archive tells the story in two rounds:

    <runs>/2026-11-13/144835/   stage2, gate_code=GO, as_of=2026-11-13, but NO
                                `deploy_status` field and NO `live_trading_ticket.json`
                                (run at 2026-11-16 07:12 local, i.e. *inside* the
                                pre-open window -- the deploy step simply never ran).
    <runs>/2026-11-13/192803/   deploy_status=FAILED, `MissedExecutionOpenError`
                                timing_violation=after_open lateness_minutes=119.2
                                (generated 2026-11-16 11:29, the 09:30 fill long gone).

Consequence: no ticket for `as_of=2026-11-13` exists at all, therefore nothing carries
`execution_date=2026-11-16`, so Monday's open had ZERO fill instructions -- and the
pending -8% stop on 600362 was delayed a full session.  Nothing in the pipeline
surfaced that; each individual run looked like "just another refusal".  This auditor is
the cross-run view: it judges the ARCHIVE, not one round.

WHAT IT SCANS (structure, never content mutation)
-------------------------------------------------
`<runs-root>/<YYYY-MM-DD>/<HHMMSS>/{summary.json, live_trading_ticket.json,
post_holdings.json, post_holdings.meta.json, ...}`.  Only directories matching
`YYYY-MM-DD` / `HHMMSS` are instances; repair backups (`202644.pre-repair-...`) and
day-level files are not.  Strictly read-only: no `--apply`, no repair, nothing is opened
for writing (the only write is `--json-out`, an operator-chosen output path).

FINDINGS (every one carries date, instance, code, severity, one-line Chinese detail)
------------------------------------------------------------------------------------
| code | severity | meaning |
|---|---|---|
| `MISSED_SESSION` | HIGH | a session day in range where NO instance produced a **usable ticket** |
| `TIMING_REFUSED` | HIGH | `FAILED` + `MissedExecutionOpenError` / `timing_violation=after_open` |
| `DEPLOY_FAILED` | MEDIUM | any other `FAILED` deploy (last non-empty stderr line as detail) |
| `TICKET_UNFILLED` | LOW | ticket whose `execution_date` is before `as_of` + next session, or before the scan end with no `post_holdings.json` |
| `POST_HOLDINGS_MISSING` | MEDIUM | ticket present but `post_holdings.json` absent, or `post_holdings.meta.json::complete` is not `true`. (NV10) Each finding is tagged `legacy: true/false` from `summary.json::post_holdings_written`: ABSENT = a pre-2026-11-11 round that could not write a sidecar; `false` = a current round that lost it. |
| `DUPLICATE_INSTANCES` | LOW | more than one SUCCESS-with-ticket instance for the same `as_of` (supersession order matters) |

**`MISSED_SESSION` is evaluated on the SIGNAL day** (`ticket.as_of == that day`), exactly
as specified, so it reports the day whose decision was lost (2026-11-13), not the day
whose *open* went unfilled (2026-11-16).  Those are the same broken signal, so the fill
side is reported as an annotation on the missed-session finding instead of a second
finding: when no ticket anywhere in the archive carries `execution_date == next session`,
the detail says so explicitly.  One broken signal therefore yields exactly one HIGH
finding, not two.

DEDUPLICATION RULE (explicit, because it changes the counts)
------------------------------------------------------------
`MISSED_SESSION` **supersedes the `TIMING_REFUSED` findings on the same date**: a refusal
is the *mechanism*, the missed session is the *outcome*, and counting both would make one
lost day look like a multi-incident crisis.  Superseded findings are still emitted (and
still printed, marked `[sup]`) with `counted=false` + `superseded_by`, but they are
excluded from `summary.counts_by_code` / `counts_by_severity`.  A `TIMING_REFUSED` on a
day that DID ship a usable ticket elsewhere in the day is NOT superseded -- a late retry
that refused on an already-covered session is its own (real) near-miss.

USABLE TICKET (definition, with one documented legacy fallback)
---------------------------------------------------------------
`summary.json::deploy_status == "SUCCESS"` **and** `live_trading_ticket.json` exists with
`as_of == that day`.  Pre-2026-11-04 rounds wrote the ticket as a bare JSON **list** of
orders with no `as_of`/`execution_date` envelope at all; for those the ticket's as-of is
taken from the same round's `summary.json::as_of` (reported as
`ticket_as_of_source="summary"`), because "the round says it shipped a ticket for D"
is the same evidence in a different file.  Never invent an as-of when neither file has
one.

CALENDAR (never guessed)
------------------------
Sessions come from `common/market_holidays.py`: weekdays in `[from, to]` minus full-day
closures, plus a 14-day look-ahead so `as_of + next session` can resolve past `--to`.
`--no-calendar` skips the holiday table and treats every weekday as a session (for
archives outside the table's covered years and for offline tests); an out-of-coverage
year is exit 2, **never** a silent weekday-only degradation.  Tests inject
`trading_days=[...]` directly, so no test depends on the real table.

EXIT CODES (`--strict` semantics belong to the archive, not to this session)
---------------------------------------------------------------------------
* `0` -- clean, or findings without `--strict` (the findings are still printed: a silent
  exit 0 on a dirty archive is the failure mode this tool exists to prevent).
* `1` -- at least one finding AND `--strict`.  Superseded findings cannot turn exit 1
  into 0: the check is on raw findings, so a missed session always fails a strict run.
* `2` -- usage / wrong tree: missing or non-directory `--runs-root`, no `YYYY-MM-DD`
  instance directories under it, `--from > --to`, unknown `--market`, or a calendar year
  outside `common/market_holidays.py`'s coverage (pass `--no-calendar` to override).

Usage:
    python scripts/check_live_run_integrity.py --runs-root <runs-root>
    python scripts/check_live_run_integrity.py --runs-root <runs-root> \\
        --from 2026-11-10 --to 2026-11-16 --json-out /tmp/integrity.json --strict
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.market_holidays import (  # noqa: E402  (after the sys.path injection)
    HolidayCalendarCoverageError,
    UnknownMarketError,
    holidays_in_range,
    normalize_market,
)

SUMMARY_FILENAME = "summary.json"
TICKET_FILENAME = "live_trading_ticket.json"
POST_HOLDINGS_FILENAME = "post_holdings.json"
POST_HOLDINGS_META_FILENAME = "post_holdings.meta.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANCE_RE = re.compile(r"^\d{6}$")

#: `lateness_minutes=119.2` out of the refusal text (present in both stderr and
#: `error_detail`; parsed once, reported once).
LATENESS_RE = re.compile(r"lateness_minutes\s*=\s*(-?[0-9]+(?:\.[0-9]+)?)")
#: `execution_date=2026-11-16` out of the refusal text -- a refused round writes no
#: ticket, so the declared fill date exists ONLY in this line (never invent it).
EXECUTION_DATE_RE = re.compile(r"execution_date\s*=\s*(\d{4}-\d{2}-\d{2})")
#: A Python exception header at the start of a line: `module.SomeError: message`.
EXCEPTION_HEADER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\s*:\s*(.*)$")
#: The two spellings the engine uses for "the declared next-open fill already opened".
AFTER_OPEN_MARKERS = ("MissedExecutionOpenError", "timing_violation=after_open")

#: Stable code order -- also the order used in the summary/legend.
FINDING_CODES: Tuple[str, ...] = (
    "MISSED_SESSION",
    "TIMING_REFUSED",
    "DEPLOY_FAILED",
    "POST_HOLDINGS_MISSING",
    "TICKET_UNFILLED",
    "DUPLICATE_INSTANCES",
)

SEVERITY_BY_CODE: Dict[str, str] = {
    "MISSED_SESSION": "HIGH",
    "TIMING_REFUSED": "HIGH",
    "DEPLOY_FAILED": "MEDIUM",
    "POST_HOLDINGS_MISSING": "MEDIUM",
    "TICKET_UNFILLED": "LOW",
    "DUPLICATE_INSTANCES": "LOW",
}

SEVERITY_ORDER: Dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

#: Calendar look-ahead (calendar days) so `as_of + next session` resolves beyond `--to`.
CALENDAR_LOOKAHEAD_DAYS = 14

#: Detail text is one line by contract; this is the truncation budget for a raw
#: stderr/traceback line, wide enough for a full engine refusal headline.
DETAIL_LIMIT = 220


# --------------------------------------------------------------------------- #
# small IO / formatting helpers
# --------------------------------------------------------------------------- #
def _one_line(text: Any, limit: int = DETAIL_LIMIT) -> str:
    """Collapse any whitespace run to one space and truncate (details are one-liners)."""
    if text is None:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _read_json(path: str) -> Tuple[Optional[Any], str]:
    """``(payload, error)`` -- never raises; a broken file is data, not a crash."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), ""
    except FileNotFoundError:
        return None, ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _one_line(f"{type(exc).__name__}: {exc}", 120)


def _as_date(value: Any) -> Optional[dt.date]:
    """ISO date/datetime string (or `date`) -> `date`; anything else -> `None`."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _get(payload: Any, key: str) -> Any:
    """Key lookup that tolerates a legacy LIST ticket (no envelope fields)."""
    return payload.get(key) if isinstance(payload, dict) else None


def _deploy_failure_line(text: Any) -> str:
    """The single most diagnostic line of a failed child traceback.

    Naively taking the *last* non-empty stderr line is what this tool's own incident
    report did, and on the real archive it returns a `!!!!!!!!` banner (11-04) or a
    trailing help paragraph.  So: prefer the last `...Error:`/`...Exception:` header,
    and when that header has no message of its own (the engine prints the refusal
    block *below* it), append the first meaningful line that follows.  Falls back to
    the last meaningful line for a non-Python failure.
    """
    if not isinstance(text, str):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    meaningful = [line for line in lines if any(ch.isalnum() for ch in line)]
    for index in range(len(lines) - 1, -1, -1):
        match = EXCEPTION_HEADER_RE.match(lines[index])
        if not match:
            continue
        header, message = match.group(1), match.group(2).strip()
        if message:
            return _one_line(lines[index])
        for following in lines[index + 1:]:
            if any(ch.isalnum() for ch in following):
                return _one_line(f"{header}: {following}")
        return _one_line(header)
    return _one_line(meaningful[-1]) if meaningful else ""


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def resolve_trading_days(
    market: str,
    start: dt.date,
    end: dt.date,
    trading_days: Optional[Sequence[dt.date]] = None,
    use_calendar: bool = True,
) -> List[dt.date]:
    """Session days in `[start, end]`, ascending.

    Precedence: an explicit `trading_days` list (tests, other calendars) wins over
    everything; then `common/market_holidays.py` weekdays-minus-closures; and with
    `use_calendar=False` every weekday is a session.  An out-of-coverage year raises
    `HolidayCalendarCoverageError` -- UNKNOWN must never be read as "market open"
    (see that module's docstring), so the caller turns it into exit 2 instead.
    """
    if end < start:
        return []
    if trading_days is not None:
        return sorted(d for d in trading_days if start <= d <= end)
    holidays = holidays_in_range(market, start, end) if use_calendar else frozenset()
    days: List[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _next_session(day: dt.date, sessions: Sequence[dt.date]) -> Optional[dt.date]:
    for session in sessions:
        if session > day:
            return session
    return None


# --------------------------------------------------------------------------- #
# one archive instance
# --------------------------------------------------------------------------- #
@dataclass
class Instance:
    """One `<date>/<HHMMSS>` round, parsed read-only (raw payloads kept for evidence)."""

    day: dt.date
    name: str
    path: str
    summary: Optional[Dict[str, Any]] = None
    summary_error: str = ""
    ticket_present: bool = False
    ticket: Optional[Any] = None
    ticket_error: str = ""
    post_holdings_present: bool = False
    post_meta: Optional[Dict[str, Any]] = None
    post_meta_error: str = ""

    # -- summary ------------------------------------------------------------
    @property
    def deploy_status(self) -> Optional[str]:
        value = _get(self.summary, "deploy_status")
        return value if isinstance(value, str) else None

    @property
    def stage(self) -> Optional[str]:
        value = _get(self.summary, "stage")
        return value if isinstance(value, str) else None

    @property
    def summary_as_of(self) -> Optional[dt.date]:
        return _as_date(_get(self.summary, "as_of"))

    # -- ticket -------------------------------------------------------------
    @property
    def ticket_as_of(self) -> Optional[dt.date]:
        return _as_date(_get(self.ticket, "as_of"))

    @property
    def effective_as_of(self) -> Optional[dt.date]:
        """Ticket as-of, falling back to the round's own summary as-of (legacy list)."""
        return self.ticket_as_of or self.summary_as_of

    @property
    def ticket_as_of_source(self) -> str:
        if self.ticket_as_of is not None:
            return "ticket"
        if self.summary_as_of is not None:
            return "summary"
        return ""

    @property
    def execution_date(self) -> Optional[dt.date]:
        return _as_date(_get(self.ticket, "execution_date"))

    @property
    def deploy_text(self) -> str:
        """Everything a refusal can hide in: raw stderr/stdout + classified detail."""
        parts = [
            _get(self.summary, "deploy_stderr"),
            _get(self.summary, "deploy_stdout"),
            _get(self.summary, "error_detail"),
        ]
        return "\n".join(p for p in parts if isinstance(p, str))

    # -- post-trade book ----------------------------------------------------
    @property
    def post_complete(self) -> Optional[bool]:
        """`post_holdings.meta.json::complete`; `None` when the attestation is absent."""
        value = _get(self.post_meta, "complete")
        return value if isinstance(value, bool) else None

    @property
    def post_holdings_written_flag(self) -> Optional[bool]:
        """`summary.json::post_holdings_written` -- ADDED BY THE WRITER (NV10).

        `None` means the round predates the key, which is the ONLY honest way to tell a
        legacy round (written before the D5 sidecar existed) from a current round that lost
        its book: on disk both are "book present, no sidecar".
        """
        value = _get(self.summary, "post_holdings_written")
        return value if isinstance(value, bool) else None

    @property
    def post_holdings_reason(self) -> str:
        value = _get(self.summary, "post_holdings_reason")
        return value if isinstance(value, str) else ""

    @property
    def post_holdings_legacy_round(self) -> bool:
        """A round whose summary predates `post_holdings_written` (legacy, not a live loss)."""
        return (
            self.post_holdings_written_flag is None
            and self.post_holdings_present
            and self.post_complete is None
        )

    # -- verdicts -----------------------------------------------------------
    @property
    def usable(self) -> bool:
        """`SUCCESS` + a ticket whose as-of is this round's own day (see docstring)."""
        return (
            self.deploy_status == "SUCCESS"
            and self.ticket_present
            and self.effective_as_of == self.day
        )

    @property
    def after_open_refusal(self) -> bool:
        text = self.deploy_text
        return any(marker in text for marker in AFTER_OPEN_MARKERS)

    def status_label(self) -> str:
        """Short per-instance reason used in a MISSED_SESSION detail line."""
        if self.deploy_status == "SUCCESS":
            return "SUCCESS但无当日票据" if not self.ticket_present else "票据as_of非当日"
        if self.deploy_status == "FAILED":
            return "执行时点拒绝(after_open)" if self.after_open_refusal else "部署失败"
        if self.deploy_status is None:
            if self.stage:
                return f"deploy_status缺失({self.stage})"
            return "deploy_status缺失"
        return f"deploy_status={self.deploy_status}"


def load_instance(day: dt.date, name: str, path: str) -> Instance:
    summary, summary_error = _read_json(os.path.join(path, SUMMARY_FILENAME))
    ticket_path = os.path.join(path, TICKET_FILENAME)
    ticket_present = os.path.exists(ticket_path)
    ticket, ticket_error = _read_json(ticket_path) if ticket_present else (None, "")
    post_path = os.path.join(path, POST_HOLDINGS_FILENAME)
    meta, meta_error = _read_json(os.path.join(path, POST_HOLDINGS_META_FILENAME))
    return Instance(
        day=day,
        name=name,
        path=path,
        summary=summary if isinstance(summary, dict) else None,
        summary_error=summary_error,
        ticket_present=ticket_present,
        ticket=ticket,
        ticket_error=ticket_error,
        post_holdings_present=os.path.exists(post_path),
        post_meta=meta if isinstance(meta, dict) else None,
        post_meta_error=meta_error,
    )


def scan_instances(runs_root: str) -> List[Instance]:
    """Every `<YYYY-MM-DD>/<HHMMSS>` instance under `runs_root`, ascending by name."""
    instances: List[Instance] = []
    for day_name in sorted(os.listdir(runs_root)):
        if not DATE_RE.match(day_name):
            continue
        day_dir = os.path.join(runs_root, day_name)
        if not os.path.isdir(day_dir):
            continue
        day = _as_date(day_name)
        if day is None:
            continue
        for inst_name in sorted(os.listdir(day_dir)):
            inst_path = os.path.join(day_dir, inst_name)
            if not INSTANCE_RE.match(inst_name) or not os.path.isdir(inst_path):
                continue
            instances.append(load_instance(day, inst_name, inst_path))
    return instances


def instance_date_dirs(runs_root: str) -> List[dt.date]:
    """Dates that actually carry at least one instance (the default scan range).

    Empty `YYYY-MM-DD` directories are NOT runs -- the real archive has seven of them
    before its first round (2026-10-20..2026-10-27), and counting a placeholder
    directory as "a session we ran and failed" would bury the real finding.
    """
    dates: List[dt.date] = []
    for day_name in sorted(os.listdir(runs_root)):
        day = _as_date(day_name) if DATE_RE.match(day_name) else None
        if day is None:
            continue
        day_dir = os.path.join(runs_root, day_name)
        if not os.path.isdir(day_dir):
            continue
        if any(
            INSTANCE_RE.match(name) and os.path.isdir(os.path.join(day_dir, name))
            for name in os.listdir(day_dir)
        ):
            dates.append(day)
    return dates


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    """One anomaly. `counted` is what the summary counts (see the dedup rule)."""

    date: dt.date
    instance: str
    code: str
    detail: str
    counted: bool = True
    superseded_by: str = ""
    #: (NV10) `POST_HOLDINGS_MISSING` only: `True` = a PRE-D5 round (no sidecar could exist),
    #: `False` = the writer says it attested and the sidecar is gone / never landed, `None` =
    #: not applicable (every other code) or the round's summary cannot say.
    legacy: Optional[bool] = None

    @property
    def severity(self) -> str:
        return SEVERITY_BY_CODE[self.code]

    def sort_key(self) -> Tuple[str, int, int, str]:
        return (
            self.date.isoformat(),
            SEVERITY_ORDER[self.severity],
            FINDING_CODES.index(self.code),
            self.instance,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "date": self.date.isoformat(),
            "instance": self.instance,
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
        }
        if not self.counted:
            payload["counted"] = False
            payload["superseded_by"] = self.superseded_by
        if self.legacy is not None:
            payload["legacy"] = self.legacy
        return payload


def _missed_session_detail(
    instances: Sequence[Instance],
    next_session: Optional[dt.date],
    fill_uncovered: bool,
) -> str:
    """One line: why the day has no ticket, and which open therefore went unfilled."""
    if not instances:
        detail = "当日无任何运行实例（未出票）"
    else:
        reasons = ", ".join(f"{inst.name}({inst.status_label()})" for inst in instances)
        detail = f"当日 {len(instances)} 个实例均未产生可用票据: {reasons}"
    if next_session is not None and fill_uncovered:
        detail += (
            f"；下一交易日 {next_session.isoformat()} 开盘无任何可执行票据"
            f"（档案中无 execution_date={next_session.isoformat()} 的票据）"
        )
    return _one_line(detail)


def _timing_refusal_detail(inst: Instance) -> str:
    text = inst.deploy_text
    match = LATENESS_RE.search(text)
    lateness = f"lateness_minutes={match.group(1)}" if match else "lateness_minutes=未解析"
    execution_date = inst.execution_date
    if execution_date is None:
        # A refused round writes no ticket, so `execution_date` survives only in the
        # refusal text. Prefer the structured ticket field when a ticket did get written.
        found = EXECUTION_DATE_RE.search(text)
        execution_date = _as_date(found.group(1)) if found else None
    exec_text = execution_date.isoformat() if execution_date else "未知"
    return _one_line(
        f"执行时点已过开盘，拒绝出票（MissedExecutionOpenError timing_violation=after_open "
        f"{lateness}）as_of={inst.day.isoformat()} execution_date={exec_text}"
    )


def _deploy_failed_detail(inst: Instance) -> str:
    line = _deploy_failure_line(_get(inst.summary, "deploy_stderr"))
    if not line:
        line = _deploy_failure_line(_get(inst.summary, "deploy_stdout"))
    if not line:
        line = _one_line(_get(inst.summary, "error_detail") or "")
    if not line:
        line = _one_line(_get(inst.summary, "error_headline") or _get(inst.summary, "error_kind") or "")
    if not line:
        line = f"summary.json 未给出 deploy 输出（summary_error={inst.summary_error or '无'}）"
    return _one_line(f"部署失败（deploy_status=FAILED）: {line}")


def _post_holdings_detail(inst: Instance) -> str:
    if not inst.post_holdings_present:
        detail = (
            "票据已出但缺少交易后持仓账簿 post_holdings.json（无法核对成交后的实际仓位）"
        )
        if inst.post_holdings_written_flag is False:
            detail += (
                f"；summary 记 post_holdings_written=false reason="
                f"{inst.post_holdings_reason or '<未记录>'} ⇒ 本轮（当前）丢失账簿"
            )
        return _one_line(detail)
    if inst.post_meta is None:
        why = f"（解析失败: {inst.post_meta_error}）" if inst.post_meta_error else "（旧格式，无完整性证明）"
        detail = f"post_holdings.meta.json 缺失{why}，无法确认账簿完整"
        flag = inst.post_holdings_written_flag
        if flag is False:
            detail += (
                f"；summary 记 post_holdings_written=false reason="
                f"{inst.post_holdings_reason or '<未记录>'} ⇒ 当前轮丢失侧车（非历史遗留）"
            )
        elif flag is True:
            detail += (
                "；summary 记 post_holdings_written=true 却无侧车 ⇒ 侧车在写出后被删除或丢失"
            )
        else:
            detail += (
                "；summary 无 post_holdings_written 键 ⇒ 判为历史遗留轮次"
                "（该侧车特性于 2026-11-11 落地），不得读作当前轮丢失"
            )
        return _one_line(detail)
    return _one_line(
        f"post_holdings.meta.json complete={inst.post_complete!r}（须为 true 才能确认账簿完整）"
    )


def _unfilled_execution_sessions(
    sessions: Sequence[dt.date],
    sessions_with_lookahead: Sequence[dt.date],
    by_day: Dict[dt.date, List["Instance"]],
    fills: set,
) -> List[str]:
    """Sessions whose open received no fill instruction at all.

    Two evidence states are kept apart, exactly like the calendar module's
    UNKNOWN-vs-empty discipline:

    * **missing** -- the preceding session shipped a usable ticket whose
      `execution_date` is KNOWN and is not this session, or shipped no usable ticket
      at all, and no ticket anywhere carries `execution_date == this session`;
    * **unknown** -- the preceding session's only usable ticket is a legacy list with
      no `execution_date` envelope, so it cannot be ruled out as the fill.
    """
    unfilled: List[str] = []
    for day in sessions:
        following = _next_session(day, sessions_with_lookahead)
        if following is None or following in fills:
            continue
        usable = [inst for inst in by_day.get(day, []) if inst.usable]
        if any(inst.execution_date is None for inst in usable):
            continue  # UNKNOWN, not missing
        unfilled.append(following.isoformat())
    return unfilled


def audit_run_archive(
    runs_root: str,
    market: str = "china",
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
    trading_days: Optional[Sequence[dt.date]] = None,
    use_calendar: bool = True,
) -> Dict[str, Any]:
    """Audit `runs_root` and return the full payload (`findings`/`summary`/`scanned`).

    `trading_days` injects the session list verbatim (tests); `use_calendar=False`
    disables the holiday table but keeps the weekday rule.  Raises `ValueError` for an
    empty/inverted range and `HolidayCalendarCoverageError` for an unknown calendar year
    -- the caller turns both into exit 2 rather than reporting a clean archive.
    """
    canonical_market = normalize_market(market)
    all_instances = scan_instances(runs_root)

    dated = instance_date_dirs(runs_root)
    start = from_date or (dated[0] if dated else None)
    end = to_date or (dated[-1] if dated else None)
    if start is None or end is None:
        raise ValueError(
            f"no YYYY-MM-DD/<HHMMSS> run directories under {runs_root!r}: "
            "this does not look like a live run archive"
        )
    if end < start:
        raise ValueError(f"--from {start.isoformat()} is after --to {end.isoformat()}")

    lookahead = end + dt.timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
    sessions = resolve_trading_days(canonical_market, start, end, trading_days, use_calendar)
    sessions_with_lookahead = resolve_trading_days(
        canonical_market, start, lookahead, trading_days, use_calendar
    )

    instances = [inst for inst in all_instances if start <= inst.day <= end]
    by_day: Dict[dt.date, List[Instance]] = {}
    for inst in instances:
        by_day.setdefault(inst.day, []).append(inst)

    # Which execution dates actually got a fill instruction anywhere in the archive?
    fills: set = {inst.execution_date for inst in all_instances if inst.execution_date is not None}

    findings: List[Finding] = []

    # 1. MISSED_SESSION (+ 6. DUPLICATE_INSTANCES) -------------------------- #
    for day in sessions:
        day_instances = by_day.get(day, [])
        usable = [inst for inst in day_instances if inst.usable]
        if not usable:
            following = _next_session(day, sessions_with_lookahead)
            findings.append(
                Finding(
                    date=day,
                    instance=", ".join(i.name for i in day_instances) or "-",
                    code="MISSED_SESSION",
                    detail=_missed_session_detail(
                        day_instances,
                        following,
                        fill_uncovered=following is not None and following not in fills,
                    ),
                )
            )
        elif len(usable) > 1:
            findings.append(
                Finding(
                    date=day,
                    instance=", ".join(i.name for i in usable),
                    code="DUPLICATE_INSTANCES",
                    detail=_one_line(
                        f"同日 {len(usable)} 个实例均出票（as_of={day.isoformat()}），"
                        "存在后写覆盖前写的关系，须按实例时间序确认生效票据"
                    ),
                )
            )

    # 2./3. per-instance deploy outcomes ------------------------------------ #
    for inst in instances:
        if inst.deploy_status != "FAILED":
            continue
        if inst.after_open_refusal:
            findings.append(
                Finding(
                    date=inst.day,
                    instance=inst.name,
                    code="TIMING_REFUSED",
                    detail=_timing_refusal_detail(inst),
                )
            )
        else:
            findings.append(
                Finding(
                    date=inst.day,
                    instance=inst.name,
                    code="DEPLOY_FAILED",
                    detail=_deploy_failed_detail(inst),
                )
            )

    # 4. TICKET_UNFILLED ---------------------------------------------------- #
    for inst in instances:
        if not inst.ticket_present or inst.ticket is None:
            continue
        problems: List[str] = []
        as_of = inst.effective_as_of
        expected = _next_session(as_of, sessions_with_lookahead) if as_of else None
        execution = inst.execution_date
        if execution is not None and expected is not None and execution < expected:
            problems.append(
                f"execution_date={execution.isoformat()} 早于 as_of({as_of.isoformat()}) "
                f"的下一交易日 {expected.isoformat()}"
            )
        if execution is not None and execution < end and not inst.post_holdings_present:
            problems.append(
                f"execution_date={execution.isoformat()} < 扫描终点 {end.isoformat()} "
                "且无 post_holdings.json（票据发出后未跟进交易后账簿）"
            )
        if problems:
            findings.append(
                Finding(
                    date=inst.day,
                    instance=inst.name,
                    code="TICKET_UNFILLED",
                    detail=_one_line("；".join(problems)),
                )
            )

    # 5. POST_HOLDINGS_MISSING ---------------------------------------------- #
    for inst in instances:
        if not inst.ticket_present or inst.ticket is None:
            continue
        if inst.post_holdings_present and inst.post_complete is True:
            continue
        findings.append(
            Finding(
                date=inst.day,
                instance=inst.name,
                code="POST_HOLDINGS_MISSING",
                detail=_post_holdings_detail(inst),
                # (NV10) Name the two states instead of leaving the operator to guess: a round
                # that predates the sidecar could not have written one, a current round that
                # lost it is a live defect. `None` = the summary itself cannot say.
                legacy=inst.post_holdings_legacy_round,
            )
        )

    # DEDUP: a MISSED_SESSION supersedes the TIMING_REFUSED findings that explain it.
    missed_days = {f.date for f in findings if f.code == "MISSED_SESSION"}
    deduped: List[Finding] = []
    for finding in findings:
        if finding.code == "TIMING_REFUSED" and finding.date in missed_days:
            deduped.append(
                Finding(
                    date=finding.date,
                    instance=finding.instance,
                    code=finding.code,
                    detail=finding.detail,
                    counted=False,
                    superseded_by=f"MISSED_SESSION@{finding.date.isoformat()}",
                )
            )
        else:
            deduped.append(finding)

    ordered = sorted(deduped, key=Finding.sort_key)
    covered = sum(1 for day in sessions if any(i.usable for i in by_day.get(day, [])))
    finding_dicts = [f.to_dict() for f in ordered]

    return {        "runs_root": runs_root,
        "findings": finding_dicts,
        "summary": _build_summary(ordered),
        "scanned": {
            "market": canonical_market,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "calendar_source": (
                "injected" if trading_days is not None else ("weekdays" if not use_calendar
                                                             else "common.market_holidays")
            ),
            "instances": len(instances),
            "sessions_expected": len(sessions),
            "sessions_covered": covered,
            "trading_days": [day.isoformat() for day in sessions],
            "unfilled_execution_sessions": _unfilled_execution_sessions(
                sessions, sessions_with_lookahead, by_day, fills
            ),
        },
        "verdict": _verdict(finding_dicts, sessions),
    }


def _build_summary(findings: Sequence[Finding]) -> Dict[str, Any]:
    """Counts by code/severity, superseded findings excluded (the dedup rule)."""
    counts_by_code = {code: 0 for code in FINDING_CODES}
    counts_by_severity = {severity: 0 for severity in SEVERITY_ORDER}
    superseded: List[Dict[str, str]] = []
    legacy_post_holdings = 0
    current_post_holdings_losses = 0
    for finding in findings:
        if not finding.counted:
            superseded.append(
                {
                    "code": finding.code,
                    "date": finding.date.isoformat(),
                    "instance": finding.instance,
                    "superseded_by": finding.superseded_by,
                }
            )
            continue
        counts_by_code[finding.code] += 1
        counts_by_severity[finding.severity] += 1
        if finding.code == "POST_HOLDINGS_MISSING":
            if finding.legacy is True:
                legacy_post_holdings += 1
            else:
                current_post_holdings_losses += 1
    return {
        "counts_by_code": counts_by_code,
        "counts_by_severity": counts_by_severity,
        "counted_findings": sum(counts_by_code.values()),
        "superseded_findings": len(superseded),
        "superseded": superseded,
        #: (NV10) The same `POST_HOLDINGS_MISSING` count, split by whether the round COULD have
        #: written a sidecar at all. A pre-D5 round is evidence of nothing new; a current
        #: round reported without one is a live write-path failure.
        "post_holdings_legacy_rounds": legacy_post_holdings,
        "post_holdings_current_losses": current_post_holdings_losses,
    }


def _verdict(findings: Sequence[Dict[str, Any]], sessions: Sequence[dt.date]) -> Dict[str, Any]:
    counted = [f for f in findings if f.get("counted", True)]
    by_severity = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in counted:
        by_severity[finding["severity"]] += 1
    missed = [f["date"] for f in counted if f["code"] == "MISSED_SESSION"]
    return {
        "clean": not findings,
        "counted_findings": len(counted),
        "raw_findings": len(findings),
        "by_severity": by_severity,
        "missed_sessions": missed,
        "sessions_expected": len(sessions),
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_report(payload: Dict[str, Any]) -> str:
    """Aligned table grouped by date, then a one-line verdict."""
    scanned = payload["scanned"]
    summary = payload["summary"]
    verdict = payload["verdict"]
    findings: List[Dict[str, Any]] = payload["findings"]

    code_width = max(len(code) for code in FINDING_CODES) + 2
    sev_width = 7
    lines: List[str] = []
    lines.append(f"runs root : {payload['runs_root']}")
    lines.append(
        f"scanned   : {scanned['from']}..{scanned['to']} ({scanned['market']}, "
        f"{scanned['calendar_source']})  sessions expected {scanned['sessions_expected']} / "
        f"covered {scanned['sessions_covered']}  instances {scanned['instances']}"
    )
    lines.append("")
    if not findings:
        lines.append("CLEAN: 每个交易日都有 SUCCESS 实例出票且交易后账簿完整。")
    else:
        current = None
        for finding in findings:
            if finding["date"] != current:
                if current is not None:
                    lines.append("")
                current = finding["date"]
                lines.append(current)
            marker = "" if finding.get("counted", True) else "  [sup]"
            if finding["code"] == "POST_HOLDINGS_MISSING":
                marker += "  [legacy]" if finding.get("legacy") is True else "  [当前轮]"
            lines.append(
                f"  {finding['severity']:<{sev_width}} {finding['code']:<{code_width}} "
                f"{finding['instance']:<10} {finding['detail']}{marker}"
            )
        lines.append("")
        lines.append("codes:")
        for code in FINDING_CODES:
            lines.append(f"  {code:<{code_width}} {SEVERITY_BY_CODE[code]}")
        lines.append("[sup] = superseded by a MISSED_SESSION on the same date (机制, 不计入统计)")
        lines.append(
            "[legacy] = 该轮早于 post_holdings 侧车特性（2026-11-11 落地），"
            "其 POST_HOLDINGS_MISSING 不是当前缺陷；[当前轮] = 本轮已具备写出侧车的能力却没有侧车"
        )
    lines.append("")
    counts = summary["counts_by_code"]
    nonzero = ", ".join(f"{code}={n}" for code, n in counts.items() if n) or "无"
    # (NV10) At-a-glance split of the one code whose two causes had been indistinguishable.
    post_split = ""
    if counts.get("POST_HOLDINGS_MISSING"):
        post_split = (
            f"；post_holdings 缺失: 历史遗留 {summary.get('post_holdings_legacy_rounds', 0)}"
            f" / 当前轮 {summary.get('post_holdings_current_losses', 0)}"
        )
    sup = (
        f"，另有 {summary['superseded_findings']} 条被取代的 TIMING_REFUSED"
        if summary["superseded_findings"]
        else ""
    )
    sev = verdict["by_severity"]
    if verdict["clean"]:
        lines.append(
            f"VERDICT: CLEAN — {scanned['sessions_expected']} 个交易日全部出票，0 条发现。"
        )
    else:
        missed = (
            f"；缺票交易日 {', '.join(verdict['missed_sessions'])}"
            if verdict["missed_sessions"]
            else ""
        )
        unfilled = scanned.get("unfilled_execution_sessions") or []
        open_note = f"；无票可填的开盘日 {', '.join(unfilled)}" if unfilled else ""
        lines.append(
            f"VERDICT: DIRTY — {verdict['counted_findings']} 条发现"
            f"（HIGH {sev['HIGH']} / MEDIUM {sev['MEDIUM']} / LOW {sev['LOW']}）"
            f" [{nonzero}]{post_split}{sup}{missed}{open_note}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only integrity audit of a live run archive "
            "(<runs-root>/<YYYY-MM-DD>/<HHMMSS>/). Reports MISSED_SESSION, TIMING_REFUSED, "
            "DEPLOY_FAILED, TICKET_UNFILLED, POST_HOLDINGS_MISSING and DUPLICATE_INSTANCES. "
            "Never writes to the archive. Exit 0 = clean OR findings without --strict "
            "(findings are still printed); 1 = findings with --strict; 2 = usage/wrong tree."
        ),
        epilog=(
            "A MISSED_SESSION supersedes the TIMING_REFUSED findings on the same date: both "
            "are reported, only the missed session is counted."
        ),
    )
    parser.add_argument(
        "--runs-root",
        required=True,
        help="live run archive root containing <YYYY-MM-DD>/<HHMMSS>/ instance directories",
    )
    parser.add_argument(
        "--market",
        default="china",
        help="market id used for the session calendar (default: china)",
    )
    parser.add_argument("--from", dest="from_date", default=None,
                        help="first session date (default: first date carrying an instance)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="last session date (default: last date carrying an instance)")
    parser.add_argument("--json-out", default=None,
                        help="also write the JSON payload (findings/summary/scanned) to this path")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any finding is reported (default: exit 0 even with findings)",
    )
    parser.add_argument(
        "--no-calendar",
        action="store_true",
        help="treat every weekday as a session instead of consulting common/market_holidays.py",
    )
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.isdir(args.runs_root):
        return _fail(f"--runs-root is not a directory: {args.runs_root}")

    def _parse_bound(value: Optional[str], flag: str) -> Tuple[Optional[dt.date], Optional[str]]:
        if value is None:
            return None, None
        day = _as_date(value)
        if day is None or len(value.strip()) != 10:
            return None, f"{flag} must be YYYY-MM-DD, got {value!r}"
        return day, None

    from_date, error = _parse_bound(args.from_date, "--from")
    if error:
        return _fail(error)
    to_date, error = _parse_bound(args.to_date, "--to")
    if error:
        return _fail(error)

    try:
        payload = audit_run_archive(
            args.runs_root,
            market=args.market,
            from_date=from_date,
            to_date=to_date,
            use_calendar=not args.no_calendar,
        )
    except (ValueError, UnknownMarketError) as exc:
        return _fail(str(exc))
    except HolidayCalendarCoverageError as exc:
        return _fail(f"{exc} (or pass --no-calendar)")

    print(render_report(payload))

    if args.json_out:
        try:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except OSError as exc:
            return _fail(f"cannot write --json-out {args.json_out}: {exc}")

    if args.strict and payload["findings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

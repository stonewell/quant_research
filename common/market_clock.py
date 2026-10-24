"""Market clock: the single owner of "what time is it *in that market*, and which
session has actually closed?" (design: `docs/market_clock_and_asof_resolution.md`).

**Why this module exists (2026-10-22 live incident, disclosed in full).**
The live chain (Stage 1 `scripts/check_live_portfolio_health.py`, Stage 2
`scripts/run_live_strategy.py`, `web/livetrading/*`) took its valuation date from the
*host's* `date.today()` and its "is the market open?" answer from nowhere at all --
there was no comparison anywhere between the current instant and a market session.
On 2026-10-22 a run designed for the A-share closing window executed at Beijing
10:05 (host was US Pacific), six symbols' cached series stopped one day short, and
nothing objected: the ticket merely looked well-formed. §2 of the design doc also
records an important correction -- the as-of date produced that day happened to be
*correct by construction* for a fixed UTC-7 host reading UTC+8 markets, so this
module is **not** fixing a wrong date; it is making the date correct *by
construction on any host*, and giving the window gate something to compare against.

**Design decisions taken here (and why), for reviewers of `L2.2`/`L2.3`:**

1. **Value object, not free functions.** `MarketClock` is a frozen dataclass holding
   exactly one `(tz, holiday calendar, session table)` triple. `now` is an injectable
   seam (tests pass a frozen instant; `freezegun` is neither a dependency nor needed).
   An immutable instance can never let two markets' judgements leak into each other.
2. **`now` is tz-aware UTC, always.** `market_now()` is the *only* place that reads
   the wall clock (`datetime.now(timezone.utc)`); every other function takes an
   injected instant. A **naive datetime raises `NaiveDatetimeError`** -- comparing a
   naive value against a tz-aware one is a `TypeError` in Python 3 anyway, but worse,
   "naive but intended as UTC" silently shifts the session by hours. Validation lives
   in `MarketClock.__post_init__`, so it cannot be bypassed by direct construction.
3. **Two date functions, never one** (§4 of the design doc -- the easiest thing to get
   wrong here):
   * `last_completed_session()` -- pure calendar: the latest trading day whose close
     time has passed. Used by the run-window gate, which does not care about
     upstream publication delay.
   * `as_of_trading_date()` -- close **plus** `publication_grace_minutes` (30 for
     China/HK, 15 for US; an estimate, disclosed as such in §14). Only after the grace
     does it admit *today*. This is what valuation / freshness expectations use.
4. **`SessionState` enum boundary.** `NON_TRADING_DAY` / `PRE_OPEN` /
   `OPENING_AUCTION` / `OPEN` / `LUNCH_BREAK` / `CLOSED`, with `HOLIDAY` kept as an
   **alias of `NON_TRADING_DAY`** (same value `"non_trading_day"`). Rationale: the §5
   decision table folds weekend and statutory holiday into one row -- both mean
   "market closed today, as-of is the previous session" -- while a *distinct*
   `HOLIDAY` member would create the "consumer forgot to also check HOLIDAY" bug class
   this repo exists to avoid. The weekend/holiday distinction is still available for
   observability via `non_trading_day_reason()`.
   Boundaries are half-open: the close instant itself is already `CLOSED`, the open
   instant is already `OPEN`, and China's 09:15-09:25 call auction is
   `OPENING_AUCTION` (also `as_of = previous session`, per §5 row 1).
5. **Intraday is `OPEN` or `LUNCH_BREAK`** (`is_intraday`), matching §5's
   "盘中（含午休）→ 默认拒绝" row. `OPENING_AUCTION`/`PRE_OPEN` are *not* intraday: §5
   explicitly allows pre-open runs (they value at the previous session).
6. **Holiday knowledge is never guessed.** With `holidays=None` the clock reads the
   static tables in `common/market_holidays.py`; a year outside that coverage raises
   `HolidayCalendarCoverageError` instead of falling back to "weekday => trading day".
   An explicit `holidays=frozenset(...)` overrides the table entirely (tests, and any
   caller that has a better calendar). Weekends short-circuit before the calendar is
   consulted -- a Saturday is structurally non-trading in all three markets, so that
   answer needs no table.
7. **No new dependency, no network.** `zoneinfo` (stdlib) + `datetime` + `re`; DST is
   `zoneinfo`'s job, not ours -- the session table is written in *market-local wall
   clock* times, so 09:30 New York is 13:30Z in March and 14:30Z in November without
   any DST arithmetic in this file.

**Not in scope here** (§11 Phase 0): nothing in the repo calls this module yet. Cache
freshness (`expected_last_session`), the CLI window gate and the Web archive keys are
`L2.2`-`L2.5`. `common/market_holidays.py` models full-day closures only; half-days
are a disclosed gap (see that module's docstring).
"""

from __future__ import annotations

import datetime as _dt
import enum
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from common.market_holidays import (
    MARKET_CHINA,
    MARKET_HK,
    MARKET_US,
    SUPPORTED_MARKETS,
    HolidayCalendarCoverageError,
    UnknownMarketError,
    calendar_status as _calendar_status,
    coverage_years,
    holidays_for,
    is_covered,
    is_provisional,
    normalize_market,
)


#: Hard stop for "walk back to the previous trading day".  The longest real closure
#: in the tables is China's Spring Festival (~9 calendar days incl. weekends); 45 is
#: loose enough to never fire legitimately and tight enough that a corrupted calendar
#: fails loudly instead of looping forever.
_MAX_LOOKBACK_DAYS = 45


class NaiveDatetimeError(ValueError):
    """A datetime reached the market clock without a tzinfo.

    Deliberately fatal: an aware/naive comparison raises `TypeError` in Python 3, and
    "naive, but I meant UTC" silently shifts every session judgement by the host's
    offset -- exactly the class of silent wrongness this module exists to remove.
    Pass `datetime(..., tzinfo=timezone.utc)` or a `ZoneInfo`-aware value.
    """


class UnknownSymbolMarketError(ValueError):
    """A symbol's market could not be identified (see `infer_market_from_symbol`).

    Fail-loud rather than defaulting to a market: a wrong calendar silently produces a
    wrong as-of date, which is the incident this whole track is about.
    """


class MixedMarketUniverseError(ValueError):
    """A symbol list spans more than one market and no explicit market was given.

    Per design §14 row 3 the caller must either pass an explicit market (and then
    value each symbol against **its own** market's expected session) or split the
    universe.  Guessing one calendar for a cross-market book is not allowed.
    """


class SessionState(str, enum.Enum):
    """Session phase of one market at one instant.

    Values are the lowercase strings that end up in logs / `summary.json`, so they are
    part of the on-disk contract: do not rename them.
    """

    NON_TRADING_DAY = "non_trading_day"
    #: Alias, on purpose: see module docstring decision 4.  `SessionState.HOLIDAY is
    #: SessionState.NON_TRADING_DAY` -> True, and `.value == "non_trading_day"`.
    HOLIDAY = "non_trading_day"
    PRE_OPEN = "pre_open"
    OPENING_AUCTION = "opening_auction"
    OPEN = "open"
    LUNCH_BREAK = "lunch_break"
    CLOSED = "closed"


# --------------------------------------------------------------------------- #
# Per-market session tables (market-LOCAL wall clock, half-open intervals)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketSpec:
    """Static session geometry for one market. Times are market-local wall clock."""

    market: str
    tz_name: str
    morning_open: _dt.time
    morning_close: _dt.time
    afternoon_open: Optional[_dt.time]
    afternoon_close: _dt.time
    opening_auction: Optional[Tuple[_dt.time, _dt.time]]
    default_publication_grace_minutes: int

    @property
    def close_time(self) -> _dt.time:
        """Final continuous-trading close of the day (drives as-of / completed session)."""
        return self.afternoon_close

    @property
    def has_lunch_break(self) -> bool:
        """True when the day is split into a morning and an afternoon continuous session.

        Observability only -- nothing in the session/reachability rules branches on it. In
        particular a *lunch-stopped* intraday cache is not detected by asking "is it lunch
        now?"; it is detected by the clock (11:30 is hours short of the 15:00/16:00 close),
        which is why `has_lunch_break` is never needed by the freshness gate (see
        `last_expected_bar_ts`).
        """
        return self.afternoon_open is not None


_MARKET_SPECS: Dict[str, MarketSpec] = {
    # A-shares: call auction 09:15-09:25, continuous 09:30-11:30 and 13:00-15:00.
    # (The 14:57-15:00 closing auction is deliberately NOT split out: it is still
    # "the market is trading", and splitting it would add a state no consumer needs.)
    MARKET_CHINA: MarketSpec(
        market=MARKET_CHINA,
        tz_name="Asia/Shanghai",
        morning_open=_dt.time(9, 30),
        morning_close=_dt.time(11, 30),
        afternoon_open=_dt.time(13, 0),
        afternoon_close=_dt.time(15, 0),
        opening_auction=(_dt.time(9, 15), _dt.time(9, 25)),
        default_publication_grace_minutes=30,
    ),
    # Hong Kong: continuous 09:30-12:00 and 13:00-16:00; the 09:00-09:20 pre-opening
    # session is not a tradable match, so it stays PRE_OPEN (design §4 table).
    MARKET_HK: MarketSpec(
        market=MARKET_HK,
        tz_name="Asia/Hong_Kong",
        morning_open=_dt.time(9, 30),
        morning_close=_dt.time(12, 0),
        afternoon_open=_dt.time(13, 0),
        afternoon_close=_dt.time(16, 0),
        opening_auction=None,
        default_publication_grace_minutes=30,
    ),
    # US: one continuous session, no lunch break -> every pre-09:30 instant is
    # PRE_OPEN and every post-16:00 instant is CLOSED (design §4 "连续交易" note).
    MARKET_US: MarketSpec(
        market=MARKET_US,
        tz_name="America/New_York",
        morning_open=_dt.time(9, 30),
        morning_close=_dt.time(16, 0),
        afternoon_open=None,
        afternoon_close=_dt.time(16, 0),
        opening_auction=None,
        default_publication_grace_minutes=15,
    ),
}


@lru_cache(maxsize=None)
def _zone(tz_name: str) -> ZoneInfo:
    """Cached `ZoneInfo` (constructing one per call would re-read tzdata each time)."""
    return ZoneInfo(tz_name)


def market_spec(market: str) -> MarketSpec:
    """Session geometry for a normalized market id (raises `UnknownMarketError`)."""
    return _MARKET_SPECS[normalize_market(market)]


def session_close_time(market: str) -> _dt.time:
    """Market-local final close of continuous trading (e.g. 15:00 China, 16:00 HK/US).

    Added for `L2.26` so the intraday cache gate never hard-codes "15:00"/"16:00" in
    `common/duckdb_cache.py`: this module stays the single owner of session geometry.
    Purely read-only -- it is a thin alias of `MarketSpec.close_time`, so the `L2.1`
    semantics (as-of / completed-session / run-window) are untouched.
    """
    return market_spec(market).close_time


def last_expected_bar_ts(
    market: str,
    session_date: _dt.date,
    bar_interval: _dt.timedelta,
) -> _dt.datetime:
    """Market-local (naive) timestamp a **complete** session's final bar must reach.

    Returned value is `close - bar_interval` on `session_date`, i.e. "the last bar of the
    day could have opened no earlier than this". It tolerates one bar of convention
    difference for free: with bar-**open** timestamps the final 5m bar of an A-share day
    starts at 14:55 (`>= 14:55` holds), and with bar-**close** timestamps it ends at 15:00
    (`>= 14:55` holds too). No assumption about which convention a provider uses is baked
    in, which is what makes this safe to apply to Fuyao/BaoStock/EastMoney alike.

    **Lunch breaks** (China/HK) need no special case: the anchor is the *afternoon* close
    (`MarketSpec.close_time`), so a cache whose last bar is 11:30 or 12:00 is simply
    3.5-4 hours short of the deadline. "Still inside the lunch break" and "session ended"
    are therefore distinguished by the clock alone -- an 11:30 bar can never satisfy a
    14:55 deadline, but a 14:55/15:00 bar does. No `LUNCH_BREAK` state has to be consulted
    (and it could not be: the cache has no "now" for the session it is judging).

    Cross-midnight sessions are NOT modelled (no supported market has one); the deadline
    is always on `session_date` itself.

    Half-days / early closes (US 13:00 after Thanksgiving, China/HK half sessions) are
    also NOT modelled here -- `common/market_holidays.py` tracks full-day closures only.
    An early-close session will therefore look "short" against this deadline; see
    `docs/development_plan.md` (`L2.26` follow-up) rather than papering over it.
    """
    if not isinstance(session_date, _dt.date) or isinstance(session_date, _dt.datetime):
        raise TypeError(
            f"session_date must be a plain datetime.date (not datetime), got "
            f"{type(session_date).__name__}"
        )
    if not isinstance(bar_interval, _dt.timedelta):
        raise TypeError(
            f"bar_interval must be a datetime.timedelta, got {type(bar_interval).__name__}"
        )
    if bar_interval <= _dt.timedelta(0):
        raise ValueError(f"bar_interval must be positive, got {bar_interval!r}")
    close_at = _dt.datetime.combine(session_date, session_close_time(market))
    return close_at - bar_interval


def _require_aware(now: _dt.datetime, what: str = "now") -> _dt.datetime:
    """Return `now` converted to UTC, refusing naive input loudly."""
    if not isinstance(now, _dt.datetime):
        raise NaiveDatetimeError(
            f"{what} must be a datetime, got {type(now).__name__}"
        )
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise NaiveDatetimeError(
            f"{what} must be timezone-aware, got naive {now.isoformat()!r}. "
            "A naive value cannot be interpreted unambiguously; pass e.g. "
            "'datetime(2026, 10, 23, 1, 59, tzinfo=timezone.utc)'."
        )
    return now.astimezone(_dt.timezone.utc)


def _naive_time(value: Any, what: str) -> _dt.time:
    """Validate a market-local wall-clock literal (a window bound)."""
    if not isinstance(value, _dt.time):
        raise ValueError(f"{what} must be a datetime.time, got {type(value).__name__}")
    if value.tzinfo is not None:
        raise ValueError(
            f"{what} must be a naive market-LOCAL wall-clock time "
            f"(no tzinfo), got {value.isoformat()!r}. Window literals are interpreted "
            "in the market's own zone; attaching a zone here would double-apply DST."
        )
    return value


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketClock:
    """One market, one instant, one holiday calendar. Immutable by construction.

    `now` is stored normalized to **tz-aware UTC**; every market-local question is
    answered by converting through `tz`. `holidays=None` means "use the static table
    for this market" (and therefore UNKNOWN-outside-coverage raises); an explicit
    `frozenset` -- including the empty one -- overrides the table completely.
    """

    market: str
    now: _dt.datetime
    holidays: Optional[FrozenSet[_dt.date]] = None
    publication_grace_minutes: Optional[int] = None

    # -- construction ------------------------------------------------------ #
    def __post_init__(self) -> None:
        canonical = normalize_market(self.market)
        if canonical not in _MARKET_SPECS:  # defensive: normalize_market already gates
            raise UnknownMarketError(f"unsupported market {self.market!r}")
        object.__setattr__(self, "market", canonical)
        object.__setattr__(self, "now", _require_aware(self.now, "MarketClock.now"))

        if self.holidays is not None:
            # A `datetime` is a `date` subclass but never equals a `date` in a set
            # comparison-in-spirit -- it would silently be treated as "not a holiday".
            bad = [d for d in self.holidays if not isinstance(d, _dt.date) or isinstance(d, _dt.datetime)]
            if bad:
                raise TypeError(
                    "holidays must contain plain datetime.date values (not datetime), "
                    f"got {bad[:3]!r}"
                )
            object.__setattr__(self, "holidays", frozenset(self.holidays))

        spec = _MARKET_SPECS[canonical]
        grace = self.publication_grace_minutes
        if grace is None:
            grace = spec.default_publication_grace_minutes
        if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
            raise ValueError(
                f"publication_grace_minutes must be a non-negative int, got {grace!r}"
            )
        object.__setattr__(self, "publication_grace_minutes", int(grace))

    # -- identity / time --------------------------------------------------- #
    @property
    def spec(self) -> MarketSpec:
        return _MARKET_SPECS[self.market]

    @property
    def tz(self) -> ZoneInfo:
        """`ZoneInfo` of the market (DST handled by tzdata, never by this module)."""
        return _zone(self.spec.tz_name)

    @property
    def grace_minutes(self) -> int:
        return int(self.publication_grace_minutes or 0)

    def local_now(self) -> _dt.datetime:
        """The same instant, expressed in the market's own zone (tz-aware)."""
        return self.now.astimezone(self.tz)

    def local_date(self) -> _dt.date:
        return self.local_now().date()

    def local_time(self) -> _dt.time:
        """Market-local wall clock, naive (safe for `time` comparisons; no date math)."""
        return self.local_now().time()

    def at(self, now: _dt.datetime) -> "MarketClock":
        """Same market/calendar at another instant (test + replay convenience)."""
        return replace(self, now=now)

    # -- calendar ---------------------------------------------------------- #
    def calendar_status(self) -> str:
        """`covered` / `provisional` / `uncovered` for the *local* year.

        `uncovered` is this module's UNKNOWN marker: callers that must not abort can
        log it instead of catching `HolidayCalendarCoverageError`.
        """
        if self.holidays is not None:
            return "explicit"
        return _calendar_status(self.market, self.local_date().year)

    def holidays_for_year(self, year: int) -> FrozenSet[_dt.date]:
        """Closure set for `year`: explicit override if supplied, else the static table."""
        if self.holidays is not None:
            return frozenset(d for d in self.holidays if d.year == int(year))
        return holidays_for(self.market, year)

    def is_trading_day(self, d: Optional[_dt.date] = None) -> bool:
        """True when `d` (default: the market-local date) is a tradable session day.

        Weekends are answered without the calendar (structurally non-trading in all
        three markets). Any weekday in an **uncovered** year raises
        `HolidayCalendarCoverageError` -- "calendar unknown" is never "open".
        """
        day = self.local_date() if d is None else d
        if not isinstance(day, _dt.date):
            raise TypeError(f"day must be a datetime.date, got {type(day).__name__}")
        if day.weekday() >= 5:
            return False
        return day not in self.holidays_for_year(day.year)

    def is_holiday(self, d: Optional[_dt.date] = None) -> bool:
        """Statutory weekday closure (False on weekends and on unknown calendars)."""
        day = self.local_date() if d is None else d
        if day.weekday() >= 5:
            return False
        return day in self.holidays_for_year(day.year)

    def non_trading_day_reason(self, d: Optional[_dt.date] = None) -> Optional[str]:
        """`"weekend"` / `"holiday"` / `"unknown_calendar"`, or None if trading.

        Observability only -- `session_state()` collapses these into one state on
        purpose (see module docstring decision 4).
        """
        day = self.local_date() if d is None else d
        if day.weekday() >= 5:
            return "weekend"
        try:
            return "holiday" if day in self.holidays_for_year(day.year) else None
        except HolidayCalendarCoverageError:
            return "unknown_calendar"

    # -- session ----------------------------------------------------------- #
    def session_state(self) -> SessionState:
        """Which phase of the trading day this instant falls in.

        Half-open boundaries: `[open, close)` is OPEN, so 09:30:00 is OPEN and
        15:00:00 / 16:00:00 are already CLOSED.
        """
        day = self.local_date()
        if not self.is_trading_day(day):
            return SessionState.NON_TRADING_DAY
        spec = self.spec
        t = self.local_time()

        if spec.opening_auction is not None:
            auction_open, auction_close = spec.opening_auction
            if auction_open <= t < auction_close:
                return SessionState.OPENING_AUCTION

        if t < spec.morning_open:
            return SessionState.PRE_OPEN
        if t < spec.morning_close:
            return SessionState.OPEN
        if spec.afternoon_open is None:
            # Continuous-session market (US): nothing but CLOSED remains.
            return SessionState.CLOSED
        if t < spec.afternoon_open:
            return SessionState.LUNCH_BREAK
        if t < spec.afternoon_close:
            return SessionState.OPEN
        return SessionState.CLOSED

    @property
    def is_intraday(self) -> bool:
        """`OPEN` or `LUNCH_BREAK` -- the §5 "默认拒绝" row (auction/pre-open are not)."""
        return self.session_state() in (SessionState.OPEN, SessionState.LUNCH_BREAK)

    def in_run_window(self, window: Tuple[_dt.time, _dt.time]) -> bool:
        """Is *now* inside `[start, end)` of a market-local trading-day window?

        `window` is a pair of **naive market-local** times (the whole point of §8:
        `"14:15"` means 14:15 in the market's zone, never the host's).  Non-trading
        days are outside every window.  A window whose start is later than its end is
        read as crossing midnight.
        """
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            raise ValueError(f"window must be a (start, end) pair, got {window!r}")
        start = _naive_time(window[0], "window start")
        end = _naive_time(window[1], "window end")
        if start == end:
            raise ValueError(f"degenerate window {start}..{end}: zero length")
        if not self.is_trading_day(self.local_date()):
            return False
        t = self.local_time()
        if start < end:
            return start <= t < end
        return t >= start or t < end

    # -- date resolution --------------------------------------------------- #
    def _publication_time(self, day: _dt.date) -> _dt.time:
        """Market-local time at which `day`'s close is assumed published (close+grace)."""
        close_dt = _dt.datetime.combine(day, self.spec.close_time)
        return (close_dt + _dt.timedelta(minutes=self.grace_minutes)).time()

    def publication_ready(self) -> bool:
        """True once *today's* close is at least `grace` minutes old.

        Before the close this is False by construction; on a non-trading day it is
        False (there is no today-close to publish). While it is False, `as_of` stays on
        the previous session even though today's session has already ended -- that
        gap is exactly design §5's "告警；不得信任当日 bar" row.
        """
        local = self.local_now()
        day = local.date()
        if not self.is_trading_day(day):
            return False
        return local.time() >= self._publication_time(day)

    def _previous_trading_day(self, start: _dt.date) -> _dt.date:
        """Strictly-before-`start` most recent trading day (raises if unknown)."""
        for offset in range(1, _MAX_LOOKBACK_DAYS + 1):
            candidate = start - _dt.timedelta(days=offset)
            if self.is_trading_day(candidate):
                return candidate
        raise RuntimeError(
            f"no trading day found for market={self.market!r} in the "
            f"{_MAX_LOOKBACK_DAYS} days before {start} -- the holiday calendar looks "
            "corrupt (or covers every weekday); refusing to fall back to a weekday guess"
        )

    def last_completed_session(self) -> _dt.date:
        """Latest trading day whose **close instant has passed**. Pure calendar.

        Used by the run-window gate, which asks "should we be running now?" and does
        not care whether upstream has published yet.  At exactly the close instant the
        day counts as completed (the session is over).
        """
        local = self.local_now()
        day = local.date()
        if self.is_trading_day(day) and local.time() >= self.spec.close_time:
            return day
        return self._previous_trading_day(day)

    def as_of_trading_date(self) -> _dt.date:
        """The session whose close **should already be published** -- the as-of date.

        Defined as: the latest trading day S whose `close + publication_grace` has
        elapsed at `now`. §5 decision table, row by row:

        | now (market-local) | session | as_of |
        |---|---|---|
        | pre-open / call auction | PRE_OPEN / OPENING_AUCTION | previous trading day |
        | intraday (incl. lunch) | OPEN / LUNCH_BREAK | previous trading day |
        | close .. close+grace | CLOSED (not ready) | previous trading day |
        | >= close+grace | CLOSED (ready) | today |
        | weekend / holiday | NON_TRADING_DAY | previous trading day |

        Note the close..grace row: the session *has* ended, yet as-of is still the
        previous day, because upstream is not expected to have published it. This is
        deliberately **not** `last_completed_session()` -- that function answers the
        window-gate question ("may we run?") and returns today from the close instant.
        """
        local = self.local_now()
        day = local.date()
        if self.is_trading_day(day) and local.time() >= self._publication_time(day):
            return day
        return self._previous_trading_day(day)

    # -- serialization ----------------------------------------------------- #
    def stamp(self) -> "ClockStamp":
        """Frozen snapshot for logs / `summary.json` (design §9)."""
        return ClockStamp(
            market=self.market,
            utc_now=self.now,
            local_now=self.local_now(),
            session=self.session_state().value,
            as_of=self.as_of_trading_date(),
            calendar=self.calendar_status(),
        )


# --------------------------------------------------------------------------- #
# Market / symbol inference
# --------------------------------------------------------------------------- #
#: Canonical symbol suffixes.  Ordered longest-first for clarity (all are 3 chars).
_SYMBOL_SUFFIX_MARKETS: Tuple[Tuple[str, str], ...] = (
    (".SH", MARKET_CHINA),
    (".SZ", MARKET_CHINA),
    (".BJ", MARKET_CHINA),
    (".HK", MARKET_HK),
    (".US", MARKET_US),
)

#: US-style tickers: optional leading `^` (index), letters/digits/dots/dashes.
#: `BRK.B` is a ticker here, not a `.B` suffix -- only `.SH/.SZ/.BJ/.HK/.US` are
#: treated as market suffixes.
_US_TICKER_RE = re.compile(r"^\^?[A-Z][A-Z0-9]{0,6}([.\-][A-Z0-9]{1,4})?$")


def _iter_symbols(symbols: Union[str, Iterable[str], Mapping[str, Any]]) -> Iterable[str]:
    """Normalize the accepted container shapes without consuming generators twice."""
    if isinstance(symbols, str):
        return [symbols]
    if isinstance(symbols, Mapping):
        return list(symbols.keys())
    if symbols is None:
        raise UnknownSymbolMarketError("symbols must not be None")
    return list(symbols)


def infer_market_from_symbol(symbol: str) -> str:
    """Infer one symbol's market: `china` / `hk` / `us`.

    Rules (mirroring the repo's existing conventions in
    `scripts/run_live_strategy.py::resolve_lot_size` / lifecycle market tagging, but
    fail-loud instead of defaulting):

    * `.SH` / `.SZ` / `.BJ` -> china; `.HK` -> hk; `.US` -> us
    * bare 6-digit code (`600000`, `000001`) -> china (A-share convention)
    * bare 4/5-digit code (`00700`, `0700`) -> hk
    * US-style ticker (`AAPL`, `SPY`, `BIL`, `BRK.B`, `^GSPC`) -> us
    * anything else raises `UnknownSymbolMarketError` -- never a silent default
    """
    if not isinstance(symbol, str):
        raise UnknownSymbolMarketError(
            f"symbol must be a string, got {type(symbol).__name__}: {symbol!r}"
        )
    text = symbol.strip().upper()
    if not text:
        raise UnknownSymbolMarketError("empty symbol: cannot infer a market")

    for suffix, market in _SYMBOL_SUFFIX_MARKETS:
        if text.endswith(suffix):
            return market

    if text.isdigit():
        if len(text) == 6:
            return MARKET_CHINA
        if len(text) in (4, 5):
            return MARKET_HK
        raise UnknownSymbolMarketError(
            f"cannot infer market for bare numeric code {symbol!r}: A-share codes are "
            "6 digits, HK codes 4-5 digits; add a .SH/.SZ/.BJ/.HK suffix"
        )

    if _US_TICKER_RE.match(text):
        return MARKET_US

    raise UnknownSymbolMarketError(
        f"cannot infer market for symbol {symbol!r}: unrecognised suffix/shape "
        f"(known suffixes: {[s for s, _ in _SYMBOL_SUFFIX_MARKETS]})"
    )


def infer_market_from_symbols(
    symbols: Union[str, Iterable[str], Mapping[str, Any], None],
) -> Optional[str]:
    """Single market implied by a symbol list, `None` for an empty list.

    A cross-market list raises `MixedMarketUniverseError` carrying the per-market
    groups (design §14 row 3: no explicit `--market` => refuse).
    """
    groups: Dict[str, list] = {}
    for sym in _iter_symbols(symbols):
        market = infer_market_from_symbol(sym)
        groups.setdefault(market, []).append(sym)
    if not groups:
        return None
    if len(groups) > 1:
        detail = ", ".join(
            f"{market}={sorted(str(s) for s in members)}"
            for market, members in sorted(groups.items())
        )
        raise MixedMarketUniverseError(
            f"symbols span multiple markets ({detail}); pass an explicit market and "
            "value each symbol against its own market's session, or split the universe"
        )
    return next(iter(groups))


def resolve_market(
    explicit: Optional[str],
    symbols: Union[str, Iterable[str], Mapping[str, Any], None],
) -> str:
    """Authoritative market id: `explicit` wins, else inferred from `symbols`.

    An explicit market is returned even when `symbols` is mixed (per design §14 row 3
    a mixed book is legal *once* the operator has declared the book's market); per
    symbol calendars then come from `infer_market_from_symbol`.
    """
    if explicit is not None and str(explicit).strip() != "":
        return normalize_market(str(explicit))
    inferred = infer_market_from_symbols(symbols)
    if inferred is None:
        raise UnknownMarketError(
            "cannot infer a market from an empty symbol list; pass market explicitly"
        )
    return inferred


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClockStamp:
    """Frozen, serializable clock snapshot (design §9 required fields)."""

    market: str
    utc_now: _dt.datetime
    local_now: _dt.datetime
    session: str
    as_of: _dt.date
    calendar: str

    def to_dict(self) -> Dict[str, str]:
        """ISO-8601 strings: UTC anchor + market-local anchor + as-of + coverage."""
        return {
            "market": self.market,
            "utc_now": self.utc_now.isoformat(),
            "local_now": self.local_now.isoformat(),
            "session": self.session,
            "as_of_trading_date": self.as_of.isoformat(),
            "calendar_status": self.calendar,
        }


def render_clock_stamp(clock: MarketClock) -> str:
    """One-line `market=... session=... utc=... local=... as_of=...` for logs."""
    stamp = clock.stamp()
    return (
        f"market={stamp.market} session={stamp.session} "
        f"utc={stamp.utc_now.isoformat()} local={stamp.local_now.isoformat()} "
        f"as_of={stamp.as_of.isoformat()} calendar={stamp.calendar}"
    )


# --------------------------------------------------------------------------- #
# Convenience constructors / free functions
# --------------------------------------------------------------------------- #
def market_now(
    market: str,
    now: Optional[_dt.datetime] = None,
    holidays: Optional[Iterable[_dt.date]] = None,
    publication_grace_minutes: Optional[int] = None,
) -> MarketClock:
    """Build a `MarketClock`; **the only wall-clock read in the whole module**.

    `now=None` -> `datetime.now(timezone.utc)`. Callers that need determinism (tests,
    replays, backtests) must pass an aware instant. Backtests must never call this
    with `now=None` at all (design §11 backtest guard).
    """
    instant = _dt.datetime.now(_dt.timezone.utc) if now is None else now
    return MarketClock(
        market=normalize_market(market),
        now=instant,
        holidays=None if holidays is None else frozenset(holidays),
        publication_grace_minutes=publication_grace_minutes,
    )


def session_state(
    now: Optional[_dt.datetime] = None,
    market: str = MARKET_CHINA,
    holidays: Optional[Iterable[_dt.date]] = None,
    publication_grace_minutes: Optional[int] = None,
) -> SessionState:
    """Free-function form of `MarketClock.session_state()` (same defaults, same rules)."""
    return market_now(
        market,
        now=now,
        holidays=holidays,
        publication_grace_minutes=publication_grace_minutes,
    ).session_state()


def as_of_trading_date(
    now: Optional[_dt.datetime] = None,
    market: str = MARKET_CHINA,
    holidays: Optional[Iterable[_dt.date]] = None,
    publication_grace_minutes: Optional[int] = None,
) -> _dt.date:
    """Free-function form of `MarketClock.as_of_trading_date()` (close + grace)."""
    return market_now(
        market,
        now=now,
        holidays=holidays,
        publication_grace_minutes=publication_grace_minutes,
    ).as_of_trading_date()


def expected_last_session(
    market: str = MARKET_CHINA,
    now: Optional[_dt.datetime] = None,
    holidays: Optional[Iterable[_dt.date]] = None,
    publication_grace_minutes: Optional[int] = None,
) -> _dt.date:
    """Alias of `as_of_trading_date` under the name `L2.2`'s cache layer will use.

    `common/duckdb_cache.py::is_range_covered(expected_last_session=...)` needs "the
    session that should already be on disk"; that IS the as-of date (close + grace),
    not `last_completed_session` (which would demand an in-progress day's bar before
    upstream could publish it).  Kept as a named alias so the cache layer reads
    intent-revealingly while there is exactly one implementation.
    """
    return as_of_trading_date(
        now=now,
        market=market,
        holidays=holidays,
        publication_grace_minutes=publication_grace_minutes,
    )


__all__ = [
    "ClockStamp",
    "MARKET_CHINA",
    "MARKET_HK",
    "MARKET_US",
    "MarketClock",
    "MarketSpec",
    "MixedMarketUniverseError",
    "NaiveDatetimeError",
    "SUPPORTED_MARKETS",
    "SessionState",
    "UnknownSymbolMarketError",
    "as_of_trading_date",
    "coverage_years",
    "expected_last_session",
    "infer_market_from_symbol",
    "infer_market_from_symbols",
    "is_covered",
    "is_provisional",
    "last_expected_bar_ts",
    "market_now",
    "market_spec",
    "normalize_market",
    "render_clock_stamp",
    "resolve_market",
    "session_close_time",
    "session_state",
]

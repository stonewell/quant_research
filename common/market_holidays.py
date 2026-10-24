"""Static, offline exchange-holiday tables for the three live market time zones.

**Why a static table (disclosed, not a preference).**
`docs/market_clock_and_asof_resolution.md` §10 requires the holiday source to be
able to say **UNKNOWN**. The only calendar already in the repo,
`common/financial_api.py::get_trading_days_calendar()`, covers China A-shares only,
needs network (`fuyao_client.calendar_trading_days()`) on a cache miss, and returns
an **empty `set()`** for both "no holiday" and "the calendar could not be fetched".
For a gate that authorises orders that is the same class of silent degradation as
the 2026-10-22 stale-price incident: calendar down -> gate passes -> nothing logged.
It is therefore deliberately **not** used here.

`exchange_calendars` / `pandas_market_calendars` are declared in no
`pyproject.toml`/`uv.lock`, and the L2.1 pre-check was actually executed:

    cd pipeline && uv run --offline python -c "import exchange_calendars"
    -> ModuleNotFoundError: No module named 'exchange_calendars'

so §10's "not importable" branch applies: hand-maintained tables,
**no new dependency, no network, no tzdata download** (`zoneinfo` reads the system
tz database; holiday data is pure literal dates in this file).

**UNKNOWN is never an empty set.**  The contract is:

| call | uncovered year |
|---|---|
| `holidays_for(market, year)` | raises `HolidayCalendarCoverageError` |
| `holidays_for_or_none(market, year)` | returns `None` (**UNKNOWN**) |
| `early_closes_for(market, year)` | raises `HolidayCalendarCoverageError` |
| `early_closes_for_or_none(market, year)` | returns `None` (**UNKNOWN**) |
| `is_covered(market, year)` / `calendar_status(...)` | `False` / `"uncovered"` |

`None` is UNKNOWN and is **distinguishable** from `frozenset()` / `{}`, which would mean
"known, and genuinely no holidays".  No caller may ever coerce the two together.
This is "fail loud" (raise) by default, with an explicit tri-state accessor for
callers that want to record the reason instead of crashing.  The single documented
exception is `early_close_time_or_none`, which collapses UNKNOWN onto "full session"
for the session-*geometry* accessors -- see its docstring for why raising there would
be worse than the stale deadline it reports.

**Scope / honest limitations (read before widening the window):**

1. **Coverage is 2020-2027 inclusive** (`COVERAGE_START_YEAR`/`COVERAGE_END_YEAR`).
   2026 and 2027 are flagged `PROVISIONAL_YEARS`: their statutory arrangements are
   still subject to exchange notices, so they must be re-verified before this
   table is trusted for an order-authorising gate.  2020-2025 are stable history.
2. **Full-day closures AND scheduled early closes are modelled** (`L2.27`).  The
   closure tables (`*_HOLIDAYS`) and the early-close tables (`*_EARLY_CLOSES`) are
   separate on purpose: a holiday is "no session today", an early close is "a shorter
   session today" -- collapsing them would make `is_trading_day` wrong.  Early closes
   are stored as a **per-date close time** in market-local wall clock (US 13:00 on the
   day after Thanksgiving / July 3 / Christmas Eve when those are sessions; HK 12:00
   with no afternoon session on the eves of Christmas, New Year and Lunar New Year;
   China: none -- A-share shortened days are always full closures, already in the
   holiday table).  An early-close date is therefore *still a trading day* and still
   reported `OPEN` after its real close by `common/market_clock.py::session_state()`,
   which keeps `L2.1`'s as-of/completed-session semantics untouched -- only the
   session-*geometry* accessors (`session_close_time` / `last_expected_bar_ts`) take
   the shortened close.  That residual gap is disclosed, not hidden: see
   `MarketSpec.close_time_on`'s docstring in `common/market_clock.py`.
   Early closes shift the *last bar* obligation, which is exactly what the `L2.26`
   intraday freshness gate compares against; without this table the 13:00 US days
   were permanently "not covered" and re-fetched upstream forever.
3. **Weekend dates inside a holiday block are omitted.**  These markets never trade
   on Sat/Sun (`weekday() >= 5` in `common/market_clock.py`), so listing e.g.
   `2026-02-21` would add no information.  A China "调休" make-up *working* Saturday
   is likewise **not** a trading day: SSE/SZSE stay closed on weekend make-up days.
4. **Ad-hoc closures are included only where known** (e.g. NYSE's 2025-01-09 national
   day of mourning).  A future ad-hoc closure (typhoon, mourning, emergency) cannot
   be predicted from a static table; the fail-loud freshness layer in `L2.2`/`L2.3`
   is what surfaces it.
5. This module imports **nothing** from the rest of the repo and does no I/O: it is
   a pure lookup table plus coverage bookkeeping.

**Provenance obligation.**  Entries were transcribed by hand from each exchange's
published closure calendar.  Any edit here is a data change, not a code change: cite
the source notice in the per-block comment and bump `TABLE_REVISION`.  The same applies
to the `*_EARLY_CLOSES` tables (`L2.27`): 2020-2025 are pinned from published
NYSE/Nasdaq and HKEX half-day notices, **2026-2027 are `PROVISIONAL`** (rule-derived,
not yet re-verified against the official notices -- see each block's comment).
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, FrozenSet, Optional, Tuple

#: Bump on every edit to the literal tables below (audit trail for archives).
#: `2026-10-23.2` = `L2.27` early-close (half-day) tables added.
TABLE_REVISION = "2026-10-23.2"

#: First and last calendar year for which the tables below are populated.
COVERAGE_START_YEAR = 2020
COVERAGE_END_YEAR = 2027

#: Years whose statutory arrangement was not yet pinned by an official exchange
#: notice when this file was written.  Treated as "known but not final": callers
#: that gate order release MUST surface this (see `calendar_status`).
PROVISIONAL_YEARS: FrozenSet[int] = frozenset({2026, 2027})

MARKET_CHINA = "china"
MARKET_HK = "hk"
MARKET_US = "us"
SUPPORTED_MARKETS: Tuple[str, ...] = (MARKET_CHINA, MARKET_HK, MARKET_US)

#: Human-facing aliases.  Canonical ids stay lowercase ASCII (`china`/`hk`/`us`)
#: because they are written into `summary.json`/tickets and must be stable.
_MARKET_ALIASES: Dict[str, str] = {
    "china": MARKET_CHINA,
    "cn": MARKET_CHINA,
    "a": MARKET_CHINA,
    "a_share": MARKET_CHINA,
    "a-share": MARKET_CHINA,
    "ashare": MARKET_CHINA,
    "cn_a": MARKET_CHINA,
    "mainland": MARKET_CHINA,
    "sse": MARKET_CHINA,
    "szse": MARKET_CHINA,
    "hk": MARKET_HK,
    "hkg": MARKET_HK,
    "hongkong": MARKET_HK,
    "hong_kong": MARKET_HK,
    "hong-kong": MARKET_HK,
    "hkex": MARKET_HK,
    "us": MARKET_US,
    "usa": MARKET_US,
    "us_equity": MARKET_US,
    "us-equity": MARKET_US,
    "nyse": MARKET_US,
    "nasdaq": MARKET_US,
}


class UnknownMarketError(ValueError):
    """A market identifier is not one of `china` / `hk` / `us` (or an alias)."""


class HolidayCalendarCoverageError(ValueError):
    """The requested calendar year is outside the static table's coverage.

    Raised **instead of** silently returning "no holidays": a gate must never read
    "calendar unavailable" as "market open".  Catch it only to record an explicit
    UNKNOWN and skip the gate -- never to degrade to a weekday-only guess.
    """


# --------------------------------------------------------------------------- #
# Literal tables
# --------------------------------------------------------------------------- #
def _d(year: int, *month_days: Tuple[int, int]) -> FrozenSet[_dt.date]:
    """Build a holiday set from `(month, day)` pairs (readability over brevity)."""
    return frozenset(_dt.date(year, month, day) for month, day in month_days)


def _et(year: int, *entries: Tuple[int, int, int, int]) -> Dict[_dt.date, _dt.time]:
    """Build an early-close table from `(month, day, hour, minute)` tuples (`L2.27`).

    A dict rather than a set because a half-day needs *when* it stops, not just that it
    is short: two markets/day-types can close at different hours, and a future 11:30
    close would need no new structure.
    """
    table: Dict[_dt.date, _dt.time] = {}
    for month, day, hour, minute in entries:
        date = _dt.date(year, month, day)
        if date in table:
            raise ValueError(f"duplicate early-close entry for {date.isoformat()}")
        table[date] = _dt.time(hour, minute)
    return table


# --- China A-share (SSE / SZSE / BSE) -------------------------------------- #
# Full-day closures, weekdays only (weekends are structurally non-trading).
# 2020 includes the COVID Spring-Festival extension (SSE reopened 2020-02-03).
CHINA_HOLIDAYS: Dict[int, FrozenSet[_dt.date]] = {
    2020: _d(
        2020,
        (1, 1),                                             # New Year
        (1, 24), (1, 27), (1, 28), (1, 29), (1, 30), (1, 31),  # Spring Festival + COVID extension
        (4, 6),                                             # Qingming
        (5, 1), (5, 4), (5, 5),                             # Labour Day
        (6, 25), (6, 26),                                   # Dragon Boat
        (10, 1), (10, 2), (10, 5), (10, 6), (10, 7), (10, 8),  # National Day + Mid-Autumn
    ),
    2021: _d(
        2021,
        (1, 1),
        (2, 11), (2, 12), (2, 15), (2, 16), (2, 17),
        (4, 5),
        (5, 3), (5, 4), (5, 5),
        (6, 14),
        (9, 20), (9, 21),
        (10, 1), (10, 4), (10, 5), (10, 6), (10, 7),
    ),
    2022: _d(
        2022,
        (1, 3),
        (1, 31), (2, 1), (2, 2), (2, 3), (2, 4),
        (4, 4), (4, 5),
        (5, 2), (5, 3), (5, 4),
        (6, 3),
        (9, 12),
        (10, 3), (10, 4), (10, 5), (10, 6), (10, 7),
    ),
    2023: _d(
        2023,
        (1, 2),
        (1, 23), (1, 24), (1, 25), (1, 26), (1, 27),
        (4, 5),
        (5, 1), (5, 2), (5, 3),
        (6, 22), (6, 23),
        (9, 29),
        (10, 2), (10, 3), (10, 4), (10, 5), (10, 6),
    ),
    2024: _d(
        2024,
        (1, 1),
        (2, 12), (2, 13), (2, 14), (2, 15), (2, 16),
        (4, 4), (4, 5),
        (5, 1), (5, 2), (5, 3),
        (6, 10),
        (9, 16), (9, 17),
        (10, 1), (10, 2), (10, 3), (10, 4), (10, 7),
    ),
    2025: _d(
        2025,
        (1, 1),
        (1, 28), (1, 29), (1, 30), (1, 31), (2, 3), (2, 4),
        (4, 4),
        (5, 1), (5, 2), (5, 5),
        (6, 2),
        (10, 1), (10, 2), (10, 3), (10, 6), (10, 7), (10, 8),
    ),
    # PROVISIONAL: 2026 statutory arrangement (Spring Festival CNY = 2026-02-17,
    # National Day block 2026-10-01..08).  Re-verify against SSE/SZSE notices.
    2026: _d(
        2026,
        (1, 1), (1, 2),
        (2, 16), (2, 17), (2, 18), (2, 19), (2, 20),
        (4, 6),
        (5, 1), (5, 4), (5, 5),
        (6, 19),
        (9, 25),
        (10, 1), (10, 2), (10, 5), (10, 6), (10, 7), (10, 8),
    ),
    # PROVISIONAL: 2027 (CNY = 2027-02-06, so New-Year's-Eve 02-05 is treated as
    # closed; Qingming 04-05, Dragon Boat 06-09, Mid-Autumn 09-15, National Day 10-01).
    2027: _d(
        2027,
        (1, 1),
        (2, 5), (2, 8), (2, 9), (2, 10), (2, 11), (2, 12),
        (4, 5),
        (5, 3), (5, 4), (5, 5),
        (6, 9),
        (9, 15),
        (10, 1), (10, 4), (10, 5), (10, 6), (10, 7),
    ),
}

# --- Hong Kong (HKEX) ------------------------------------------------------ #
# Full-day closures, weekdays only.  When a general holiday falls on a Sunday the
# following weekday is substituted; a Saturday general holiday is not substituted
# (Saturday is itself a general holiday and already non-trading here).
HK_HOLIDAYS: Dict[int, FrozenSet[_dt.date]] = {
    2020: _d(
        2020,
        (1, 1),
        (1, 27), (1, 28),                                  # Lunar New Year (day 3/4)
        (4, 10), (4, 13),                                  # Good Friday / Easter Monday
        (4, 30),                                           # Buddha's Birthday
        (5, 1),
        (6, 25),                                           # Tuen Ng
        (7, 1),
        (10, 1), (10, 2),                                  # National Day / day after Mid-Autumn
        (10, 26),                                          # Chung Yeung (observed)
        (12, 25),
    ),
    2021: _d(
        2021,
        (1, 1),
        (2, 12), (2, 15),
        (4, 2), (4, 5), (4, 6),                            # Good Friday / Easter / Ching Ming (obs.)
        (5, 19),
        (6, 14),
        (7, 1),
        (9, 22),                                           # day after Mid-Autumn
        (10, 1),
        (10, 14),
        (12, 27),                                          # Christmas (observed)
    ),
    2022: _d(
        2022,
        (2, 1), (2, 2), (2, 3),
        (4, 5),
        (4, 15), (4, 18),
        (5, 2), (5, 9),                                    # Labour (obs.) / Buddha's Birthday (obs.)
        (6, 3),
        (7, 1),
        (9, 12),
        (10, 4),
        (12, 26), (12, 27),                                # Christmas / Boxing Day (observed)
    ),
    2023: _d(
        2023,
        (1, 2),                                            # New Year (observed)
        (1, 23), (1, 24), (1, 25),
        (4, 5), (4, 7), (4, 10),
        (5, 1), (5, 26),
        (6, 22),
        (10, 23),
        (12, 25), (12, 26),
    ),
    2024: _d(
        2024,
        (1, 1),
        (2, 12), (2, 13),
        (3, 29), (4, 1), (4, 4),
        (5, 1), (5, 15),
        (6, 10),
        (7, 1),
        (9, 18),
        (10, 1), (10, 11),
        (12, 25), (12, 26),
    ),
    2025: _d(
        2025,
        (1, 1),
        (1, 29), (1, 30), (1, 31),
        (4, 4), (4, 18), (4, 21),
        (5, 1), (5, 5),
        (7, 1),
        (10, 1), (10, 7), (10, 29),
        (12, 25), (12, 26),
    ),
    # PROVISIONAL: 2026 (CNY = 2026-02-17; Mid-Autumn 09-25 falls Fri so the
    # "day after" lands on a Saturday and needs no weekday entry).
    2026: _d(
        2026,
        (1, 1),
        (2, 17), (2, 18), (2, 19),
        (4, 3), (4, 6), (4, 7),
        (5, 1), (5, 25),
        (6, 19),
        (7, 1),
        (10, 1), (10, 19),
        (12, 25),
    ),
    # PROVISIONAL: 2027.
    2027: _d(
        2027,
        (1, 1),
        (2, 8), (2, 9),
        (3, 26), (3, 29), (4, 5),
        (5, 13),
        (6, 9),
        (7, 1),
        (9, 16),
        (10, 1), (10, 8),
        (12, 27), (12, 28),
    ),
}

# --- United States (NYSE / Nasdaq full-day closures) ----------------------- #
# Rule-derived, then pinned as literals so the file is auditable: New Year (with
# Friday/Monday substitution), MLK (3rd Mon Jan), Washington's Birthday (3rd Mon
# Feb), Good Friday, Memorial Day (last Mon May), Juneteenth (since 2022; the
# 2021-06-19 statute took effect after that session), Independence Day, Labor Day
# (1st Mon Sep), Thanksgiving (4th Thu Nov), Christmas.
# 2025-01-09 is an ad-hoc closure (national day of mourning for President Carter).
# 13:00 early closes live in `US_EARLY_CLOSES` below (L2.27), not here -- a half-day is
# a shorter *session*, not a closure.
US_HOLIDAYS: Dict[int, FrozenSet[_dt.date]] = {
    2020: _d(
        2020,
        (1, 1), (1, 20), (2, 17), (4, 10), (5, 25),
        (7, 3), (9, 7), (11, 26), (12, 25),
    ),
    2021: _d(
        2021,
        (1, 1), (1, 18), (2, 15), (4, 2), (5, 31),
        (7, 5), (9, 6), (11, 25), (12, 24),
    ),
    2022: _d(
        2022,
        (1, 17), (2, 21), (4, 15), (5, 30), (6, 20),
        (7, 4), (9, 5), (11, 24), (12, 26),
    ),
    2023: _d(
        2023,
        (1, 2), (1, 16), (2, 20), (4, 7), (5, 29),
        (6, 19), (7, 4), (9, 4), (11, 23), (12, 25),
    ),
    2024: _d(
        2024,
        (1, 1), (1, 15), (2, 19), (3, 29), (5, 27),
        (6, 19), (7, 4), (9, 2), (11, 28), (12, 25),
    ),
    2025: _d(
        2025,
        (1, 1), (1, 9), (1, 20), (2, 17), (4, 18), (5, 26),
        (6, 19), (7, 4), (9, 1), (11, 27), (12, 25),
    ),
    2026: _d(
        2026,
        (1, 1), (1, 19), (2, 16), (4, 3), (5, 25),
        (6, 19), (7, 3), (9, 7), (11, 26), (12, 25),
    ),
    2027: _d(
        2027,
        (1, 1), (1, 18), (2, 15), (3, 26), (5, 31),
        (6, 18), (7, 5), (9, 6), (11, 25), (12, 24),
    ),
}

_TABLES: Dict[str, Dict[int, FrozenSet[_dt.date]]] = {
    MARKET_CHINA: CHINA_HOLIDAYS,
    MARKET_HK: HK_HOLIDAYS,
    MARKET_US: US_HOLIDAYS,
}


# --------------------------------------------------------------------------- #
# Early-close (half-day) tables -- `L2.27`
#
# A half-day is a SHORTER SESSION, not a closure, so it lives beside the closure tables
# instead of inside them: `is_trading_day()` must keep answering True for these dates.
# Values are the market-local wall-clock **final close of continuous trading**, the same
# quantity `common/market_clock.py::MarketSpec.close_time` carries for a normal day, so a
# consumer never has to know which kind of day it is looking at.
#
# Every date below is a weekday that is NOT already a full closure in the table above.
# That invariant is asserted in `common/tests/test_market_clock.py`
# (`test_early_close_tables_are_trading_weekdays_inside_the_covered_years`), because a
# half-day on a holiday would be silently inert *and* would hide a data-entry mistake.
# --------------------------------------------------------------------------- #
EarlyCloseTable = Dict[_dt.date, _dt.time]


# --- China A-shares: none --------------------------------------------------- #
# SSE/SZSE/BSE publish no scheduled half-day sessions in the covered window: a shortened
# trading day (Spring Festival run-up, 调休 adjustments) is always a FULL closure, which
# `CHINA_HOLIDAYS` already records. The years are listed as explicit EMPTY dicts so
# "known, and there are none" stays distinguishable from UNKNOWN (`None`), exactly as the
# `frozenset()` vs `None` discipline requires for closures.
CHINA_EARLY_CLOSES: Dict[int, EarlyCloseTable] = {
    year: {} for year in range(COVERAGE_START_YEAR, COVERAGE_END_YEAR + 1)
}

# --- Hong Kong (HKEX): 12:00, no afternoon session -------------------------- #
# Rule (HKEX trading calendar): there is NO afternoon trading session on the eves of
# Christmas, New Year and Lunar New Year, i.e. the day's last continuous bar ends at
# 12:00 instead of 16:00. An eve that falls on a weekend (Sat/Sun) is not a session at
# all, so it is simply absent. Source: HKEX "Trading hours" / half-day trading notice.
HK_EARLY_CLOSES: Dict[int, EarlyCloseTable] = {
    2020: _et(2020, (1, 24, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
    2021: _et(2021, (2, 11, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
    2022: _et(2022, (1, 31, 12, 0)),
    # 2023: LNY Eve 01-21 and New-Year's Eve 12-31 are Saturdays, Christmas Eve 12-24 is
    # a Sunday -> no half-day session in the year.
    2023: _et(2023),
    2024: _et(2024, (2, 9, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
    2025: _et(2025, (1, 28, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
    # PROVISIONAL: 2026 eves (LNY 2026-02-17 -> Eve 02-16 Mon). Re-verify with HKEX.
    2026: _et(2026, (2, 16, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
    # PROVISIONAL: 2027 eves (LNY 2027-02-06 -> Eve 02-05 Fri).
    2027: _et(2027, (2, 5, 12, 0), (12, 24, 12, 0), (12, 31, 12, 0)),
}

# --- United States (NYSE / Nasdaq): 13:00 ---------------------------------- #
# Rule-derived, then pinned as literals (auditable, and the rule has exceptions):
#   * day after Thanksgiving (4th Thu Nov + 1 day)          -> 13:00
#   * July 3, when it is itself a business day              -> 13:00
#     (if July 4 falls on a Sat the observed holiday is Fri July 3, a FULL closure; if
#      July 4 is a Sunday the observed holiday is Mon July 5 and Fri July 2 is a normal
#      full session -- so those years have no July entry, see 2020/2021/2022/2026/2027)
#   * Christmas Eve (Dec 24), when it is itself a session   -> 13:00
#     (Dec 24 Sat/Sun -> nothing; when Dec 25 falls on Sat/Sun the observed closure is
#      Dec 24 or Dec 26 -- 2021's Dec 24 and 2027's Dec 24 are FULL closures above)
# NOT modelled / deliberately absent: New Year's Eve (Dec 31) is a full session for US
# equities (the 14:00 SIFMA recommendation is a bond-market convention, not NYSE/Nasdaq).
US_EARLY_CLOSES: Dict[int, EarlyCloseTable] = {
    2020: _et(2020, (11, 27, 13, 0), (12, 24, 13, 0)),
    # 2021-12-24 is the observed Christmas closure (see US_HOLIDAYS), not a half-day.
    2021: _et(2021, (11, 26, 13, 0)),
    2022: _et(2022, (11, 25, 13, 0)),
    2023: _et(2023, (7, 3, 13, 0), (11, 24, 13, 0)),
    2024: _et(2024, (7, 3, 13, 0), (11, 29, 13, 0), (12, 24, 13, 0)),
    2025: _et(2025, (7, 3, 13, 0), (11, 28, 13, 0), (12, 24, 13, 0)),
    # PROVISIONAL: 2026 (July 3 is the observed Independence Day closure; Thanksgiving
    # 2026-11-26 -> 11-27; Christmas 12-25 Fri -> Eve 12-24 Thu a half-day).
    # Re-verify against the NYSE 2026 holiday calendar notice.
    2026: _et(2026, (11, 27, 13, 0), (12, 24, 13, 0)),
    # PROVISIONAL: 2027 (Christmas 12-25 Sat -> observed closure 12-24 Fri, already in
    # US_HOLIDAYS, so 2027 has a single early close). Re-verify with NYSE.
    2027: _et(2027, (11, 26, 13, 0)),
}

_EARLY_CLOSE_TABLES: Dict[str, Dict[int, EarlyCloseTable]] = {
    MARKET_CHINA: CHINA_EARLY_CLOSES,
    MARKET_HK: HK_EARLY_CLOSES,
    MARKET_US: US_EARLY_CLOSES,
}


# --------------------------------------------------------------------------- #
# Market id normalization
# --------------------------------------------------------------------------- #
def normalize_market(market: str) -> str:
    """Canonicalize a market id (`china` / `hk` / `us`), raising on nonsense.

    Accepts the aliases in `_MARKET_ALIASES` case-insensitively and strips
    whitespace.  Unknown ids raise `UnknownMarketError` rather than defaulting to
    A-shares: a silent default here would put a US universe on the China calendar.
    """
    if not isinstance(market, str):
        raise UnknownMarketError(f"market must be a string, got {type(market).__name__}")
    key = market.strip().lower()
    try:
        return _MARKET_ALIASES[key]
    except KeyError:
        raise UnknownMarketError(
            f"unknown market {market!r}: expected one of {list(SUPPORTED_MARKETS)} "
            f"(aliases: {sorted(_MARKET_ALIASES)})"
        ) from None


# --------------------------------------------------------------------------- #
# Coverage bookkeeping -- UNKNOWN is explicit, never an empty set
# --------------------------------------------------------------------------- #
def coverage_years(market: str) -> Tuple[int, int]:
    """`(first_year, last_year)` populated for `market`."""
    table = _TABLES[normalize_market(market)]
    return min(table), max(table)


def is_covered(market: str, year: int) -> bool:
    """True when `year` is inside the populated range for `market`."""
    return int(year) in _TABLES[normalize_market(market)]


def is_provisional(market: str, year: int) -> bool:
    """True when `year` is covered but its arrangement is not yet final.

    Independent of coverage: an uncovered year is not "provisional", it is unknown.
    """
    return is_covered(market, year) and int(year) in PROVISIONAL_YEARS


def calendar_status(market: str, year: int) -> str:
    """One of `"covered"`, `"provisional"`, `"uncovered"` (UNKNOWN)."""
    if not is_covered(market, year):
        return "uncovered"
    return "provisional" if is_provisional(market, year) else "covered"


def holidays_for(market: str, year: int) -> FrozenSet[_dt.date]:
    """Full-day closure dates of `year` for `market`.

    Raises `HolidayCalendarCoverageError` when `year` is outside the static table.
    Fail-loud by design -- see the module docstring.  Use `holidays_for_or_none`
    if the caller wants to record an UNKNOWN instead of aborting.
    """
    canonical = normalize_market(market)
    table = _TABLES[canonical]
    try:
        return table[int(year)]
    except (KeyError, ValueError):
        raise HolidayCalendarCoverageError(
            f"holiday calendar for market={canonical!r} has no data for year={year!r}: "
            f"covered range is {min(table)}-{max(table)} (table revision {TABLE_REVISION}). "
            "This is UNKNOWN, not 'no holidays' -- do not degrade to a weekday-only guess; "
            f"extend common/market_holidays.py after checking the official exchange notice."
        ) from None


def holidays_for_or_none(market: str, year: int) -> Optional[FrozenSet[_dt.date]]:
    """Tri-state accessor: the holiday set, or `None` meaning **UNKNOWN**.

    `None` (uncovered year) is deliberately distinct from `frozenset()` (covered
    year with no closures) so a caller can log "calendar unknown" explicitly.
    """
    canonical = normalize_market(market)
    return _TABLES[canonical].get(int(year))


def all_holidays(market: str) -> FrozenSet[_dt.date]:
    """Every closure date in the covered range (raises nothing; coverage is fixed)."""
    table = _TABLES[normalize_market(market)]
    return frozenset().union(*table.values())


def holidays_in_range(
    market: str,
    start: _dt.date,
    end: _dt.date,
) -> FrozenSet[_dt.date]:
    """Closure dates in `[start, end]`, inclusive on both ends.

    Unlike `holidays_for`, this accepts a multi-year span and raises
    `HolidayCalendarCoverageError` if **any** touched year is uncovered -- a
    partially-known answer must not be presented as a full one.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    canonical = normalize_market(market)
    years = range(start.year, end.year + 1)
    covered: FrozenSet[_dt.date] = frozenset()
    for year in years:
        covered = covered | holidays_for(canonical, year)
    return frozenset(d for d in covered if start <= d <= end)


def early_closes_for(market: str, year: int) -> EarlyCloseTable:
    """`{date: market-local close time}` for the early closes of `year` (`L2.27`).

    An **empty dict** means "covered year, no early closes" (China always).  Raises
    `HolidayCalendarCoverageError` when `year` is outside the static table -- UNKNOWN is
    never reported as "no half-days", same discipline as `holidays_for`.

    The mapping is returned as a *copy*: the literal tables are process-wide state, and a
    caller that mutated them would corrupt every later freshness decision in the process.
    """
    canonical = normalize_market(market)
    table = _TABLES[canonical]
    try:
        return dict(_EARLY_CLOSE_TABLES[canonical][int(year)])
    except (KeyError, ValueError):
        raise HolidayCalendarCoverageError(
            f"early-close calendar for market={canonical!r} has no data for year={year!r}: "
            f"covered range is {min(table)}-{max(table)} (table revision {TABLE_REVISION}). "
            "This is UNKNOWN, not 'no early closes' -- extend the `*_EARLY_CLOSES` tables "
            "in common/market_holidays.py after checking the official exchange notice."
        ) from None


def early_closes_for_or_none(market: str, year: int) -> Optional[EarlyCloseTable]:
    """Tri-state accessor: the early-close map, or `None` meaning **UNKNOWN**.

    `None` (uncovered year) stays distinct from `{}` (covered, no early closes), exactly
    as in `holidays_for_or_none`.
    """
    canonical = normalize_market(market)
    table = _EARLY_CLOSE_TABLES[canonical].get(int(year))
    return None if table is None else dict(table)


def early_close_time(market: str, day: _dt.date) -> Optional[_dt.time]:
    """Early-close time of `day`, or `None` when it is a normal full session.

    Raises `HolidayCalendarCoverageError` for an uncovered year (UNKNOWN is not "normal").
    Use `early_close_time_or_none` for the session-geometry call sites that must not
    raise into a coverage predicate.
    """
    return early_closes_for(market, day.year).get(day)


def early_close_time_or_none(market: str, day: _dt.date) -> Optional[_dt.time]:
    """Early-close time of `day`, or `None` -- **the one deliberate tri-state collapse**.

    `None` here covers both "normal full session" and "year not in the static table
    (UNKNOWN)".  This is the only place in this module where those two are merged, and it
    is merged on purpose: the consumer is session *geometry* (`MarketSpec.close_time_on`
    -> `common/market_clock.py::last_expected_bar_ts`), whose answer feeds a boolean
    coverage predicate.  Raising there would abort the predicate and make the caller
    (`common/data.py::fetch_universe`) silently drop the asset -- a worse failure than the
    one it reports.  The safe direction is kept: falling back to the FULL session close
    means a genuinely short session is judged "not covered" (a redundant re-fetch), never
    "covered" with a truncated day.  Callers that must distinguish the two states use
    `is_covered()` / `early_closes_for()`.
    """
    table = early_closes_for_or_none(market, day.year)
    return None if table is None else table.get(day)


def table_revision() -> str:
    """Revision string of the literal tables (for archive/audit logs)."""
    return TABLE_REVISION


__all__ = [
    "COVERAGE_START_YEAR",
    "COVERAGE_END_YEAR",
    "CHINA_HOLIDAYS",
    "CHINA_EARLY_CLOSES",
    "EarlyCloseTable",
    "HK_EARLY_CLOSES",
    "HK_HOLIDAYS",
    "US_EARLY_CLOSES",
    "US_HOLIDAYS",
    "HolidayCalendarCoverageError",
    "MARKET_CHINA",
    "MARKET_HK",
    "MARKET_US",
    "PROVISIONAL_YEARS",
    "SUPPORTED_MARKETS",
    "TABLE_REVISION",
    "UnknownMarketError",
    "all_holidays",
    "calendar_status",
    "coverage_years",
    "early_close_time",
    "early_close_time_or_none",
    "early_closes_for",
    "early_closes_for_or_none",
    "holidays_for",
    "holidays_for_or_none",
    "holidays_in_range",
    "is_covered",
    "is_provisional",
    "normalize_market",
    "table_revision",
]

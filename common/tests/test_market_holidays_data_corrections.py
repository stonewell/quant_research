"""Calibration corrections to the China holiday table (2026-11-13, `docs/version.md` §39).

Three dates contradicted the data (or, for the third, were ruled on by the owner) and are
fixed here:

* **2024-02-09 added** as an SSE/SZSE closure. It is the only 2024 weekday the table called
  a session while both daily providers (`FuyaoDataProvider`, `MarketDBDataProvider`) held
  zero bars for every symbol, and the 5m feed had no bars either. The surrounding block was
  `02-12..02-16` weekdays only, i.e. the real `02-09..02-17` closure minus its first day.
* **2026-10-08 removed** from the (provisional) 2026 National Day block. Both daily
  providers carry a full 27-symbol cross-section on 2026-10-08 and the independent BaoStock
  5m feed carries exactly 48 bars per symbol (no lunch bars); the 5m table has 47 sessions
  over 2026-09-01..2026-11-12 where the table implied 46, the symmetric difference being
  exactly this date.
* **2026-02-23 added** on the **owner's ruling of 2026-11-13**, corroborated by the data:
  it was "trading day per table" while both daily providers (all 27 symbols) and the
  BaoStock 5m feed carried zero bars. A universal hole alone cannot separate "closure" from
  "sync defect", which is why it waited for a ruling rather than being inferred.

The stale-alarm tests below are why these are not cosmetic. Two of the three went the
"spurious FAIL" way: the calendar expected a bar for a session that does not exist, the
newest real bar was OLDER, and `is_stale_bar` (`bar < expected`) failed the whole book. The
2026-10-08 one went the other way (a newer bar is never "stale" here): the harm was a
silently wrong as-of. Both directions are pinned so a future edit has to re-read them.

Everything here is offline and frozen: no provider, no network, no wall clock. The cache
figures quoted above are evidence recorded in the code comments, not something these tests
re-measure (a unit test must not require `data/cache.duckdb`).
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import market_holidays as mh
from common.freshness import is_stale_bar
from common.market_clock import (
    MARKET_CHINA,
    expected_last_session,
    market_now,
    next_trading_day,
)

_CST = _dt.timezone(_dt.timedelta(hours=8))
UTC = _dt.timezone.utc


def at_cst(y: int, mo: int, d: int, h: int, mi: int = 0) -> _dt.datetime:
    """Market-local China instant -> UTC (no DST in mainland China by construction)."""
    return _dt.datetime(y, mo, d, h, mi, tzinfo=_CST).astimezone(UTC)


def is_session(day: _dt.date) -> bool:
    """Calendar answer only (weekday + holiday table), no instant involved."""
    return market_now(MARKET_CHINA, now=at_cst(day.year, day.month, day.day, 10)).is_trading_day(day)


# --------------------------------------------------------------------------- #
# 2024-02-09: added to the pinned (non-provisional) block
# --------------------------------------------------------------------------- #
def test_2024_02_09_is_a_china_closure_not_a_session():
    assert _dt.date(2024, 2, 9) in mh.holidays_for(MARKET_CHINA, 2024)
    assert is_session(_dt.date(2024, 2, 9)) is False
    # 2024 is stable history, not a provisional year: this is a transcription fix.
    assert mh.is_provisional(MARKET_CHINA, 2024) is False


def test_2024_spring_festival_block_now_reaches_02_09():
    """The closure block must start on Friday 02-09, not on Monday 02-12.

    Weekend rows are omitted by design (`market_holidays` docstring §3), so the stored
    weekdays {02-09, 02-12..02-16} *are* the real 02-09..02-17 closure: 02-10/11 and 02-17
    are Sat/Sun/Sat. This asserts both halves of that statement so a future edit cannot
    quietly shrink the block back.
    """
    closures = mh.holidays_for(MARKET_CHINA, 2024)
    stored = {d for d in (_dt.date(2024, 2, 9), *(_dt.date(2024, 2, n) for n in range(10, 18)))
              if d in closures}
    assert stored == {
        _dt.date(2024, 2, 9),
        _dt.date(2024, 2, 12),
        _dt.date(2024, 2, 13),
        _dt.date(2024, 2, 14),
        _dt.date(2024, 2, 15),
        _dt.date(2024, 2, 16),
    }
    # The omitted days are exactly the weekend ones -- proof the block is contiguous.
    for day in (_dt.date(2024, 2, 10), _dt.date(2024, 2, 11), _dt.date(2024, 2, 17)):
        assert day.weekday() >= 5
    # And the block's neighbours are real sessions (boundary, not a widened closure).
    assert is_session(_dt.date(2024, 2, 8)) is True
    assert is_session(_dt.date(2024, 2, 19)) is True


def test_expected_last_session_after_2024_spring_festival_lands_on_a_real_session():
    """Pre-fix this resolved to 2024-02-09, a date with zero bars in every provider.

    The freshness gate is one-sided (`bar < expected` is stale), and a frame synced through
    the last real session before the holiday ends on 2024-02-08 -- which is OLDER than the
    phantom 2024-02-09 session. So every run from 02-12 up to 02-19's close+grace really did
    FAIL for every symbol under `strict_freshness`, blaming the data for a calendar error.
    The corrected answer is the pre-holiday Friday 02-08.
    """
    pre_holiday = pd.DataFrame({"Close": [7.5]}, index=pd.DatetimeIndex(["2024-02-08"]))
    for hour in (0, 10, 15):
        resolved = expected_last_session(MARKET_CHINA, now=at_cst(2024, 2, 12, hour))
        assert resolved == _dt.date(2024, 2, 8)
        assert is_session(resolved) is True
        assert resolved not in mh.holidays_for(MARKET_CHINA, 2024)
        assert is_stale_bar(pre_holiday, resolved.isoformat()) is False
    # The spurious alarm in one line: the old phantom session made the same bar "stale".
    assert is_stale_bar(pre_holiday, "2024-02-09") is True
    # 02-19 15:29 CST is still before that day's close+grace -> the answer must not advance.
    assert expected_last_session(MARKET_CHINA, now=at_cst(2024, 2, 19, 15, 29)) == _dt.date(2024, 2, 8)
    # ... and at/after the close+grace it is 02-19 itself, a session that really exists.
    assert expected_last_session(MARKET_CHINA, now=at_cst(2024, 2, 19, 15, 30)) == _dt.date(2024, 2, 19)


# --------------------------------------------------------------------------- #
# 2026-10-08: removed from the provisional National Day block
# --------------------------------------------------------------------------- #
def test_2026_10_08_is_a_trading_day_and_the_block_stops_at_10_07():
    assert _dt.date(2026, 10, 8) not in mh.holidays_for(MARKET_CHINA, 2026)
    assert is_session(_dt.date(2026, 10, 8)) is True
    assert next_trading_day(MARKET_CHINA, _dt.date(2026, 10, 7)) == _dt.date(2026, 10, 8)
    # The rest of the block is untouched (10-03/04 are the omitted weekend), and the year
    # is still provisional -- only this one date was corrected from cache evidence.
    for day in (1, 2, 5, 6, 7):
        assert _dt.date(2026, 10, day) in mh.holidays_for(MARKET_CHINA, 2026), day
    for day in (3, 4):
        assert _dt.date(2026, 10, day).weekday() >= 5
    assert mh.is_provisional(MARKET_CHINA, 2026) is True


def test_expected_last_session_on_2026_10_09_is_2026_10_08():
    """THE regression: pre-fix this returned 2026-09-30, i.e. the 10-08 session was erased.

    2026-10-09 10:00 CST is a genuine pre-close instant, so as-of must be the previous
    session -- which is 10-08, not the pre-holiday 09-30.
    """
    resolved = expected_last_session(MARKET_CHINA, now=at_cst(2026, 10, 9, 10))
    assert resolved == _dt.date(2026, 10, 8)
    # Documented pre-fix value, kept as an explicit contrast so the intent cannot be lost:
    assert resolved != _dt.date(2026, 9, 30)
    # And the window that opens on 10-08's close now starts there instead of on 09-30.
    assert expected_last_session(MARKET_CHINA, now=at_cst(2026, 10, 8, 15, 30)) == _dt.date(2026, 10, 8)


def test_2026_10_08_bar_is_not_stale_and_the_pre_fix_defect_was_silent():
    """A 2026-10-08 bar passes the freshness gate for a 2026-10-09 run.

    HONEST NOTE ON THE FAILURE MODE (measured, not assumed): `is_stale_bar` is deliberately
    ONE-SIDED -- only `bar < expected` is stale, a *newer* bar is accepted (the live callers
    truncate to as-of upstream). So the pre-fix expectation 2026-09-30 did NOT raise on a
    2026-10-08 bar; the damage was quieter. as-of resolved to 2026-09-30, so
    `as_of_universe(..., 2026-09-30)` dropped the 10-08 session and the book -- plus the run
    directory / `summary.json` as-of -- was dated 5 sessions back while the data was present
    (and a genuinely absent 10-08 bar was *hidden* by the earlier expectation). This test
    pins the corrected pair and the one-sided comparison so the mechanism stays documented.
    """
    frame = pd.DataFrame(
        {"Close": [10.0, 10.5]},
        index=pd.DatetimeIndex(["2026-09-30", "2026-10-08"]),
    )
    expected = expected_last_session(MARKET_CHINA, now=at_cst(2026, 10, 9, 10)).isoformat()
    assert expected == "2026-10-08"
    assert is_stale_bar(frame, expected) is False
    # Why the old defect never threw: a newer bar is not "stale" in this codebase.
    assert is_stale_bar(frame, "2026-09-30") is False


# --------------------------------------------------------------------------- #
# 2026-02-23: closure by owner ruling 2026-11-13 (was "trading day per this table")
# --------------------------------------------------------------------------- #
def test_2026_02_23_is_a_closure_by_owner_ruling_with_data_corroboration():
    """The third correction of `docs/version.md` §39 -- a decision plus data, not a guess.

    The table used to call 2026-02-23 a session while the cache carried zero bars for all
    27 symbols under both daily providers (and nothing in the BaoStock 5m feed); it is the
    only such day in 2026 (02-16..02-20 are zero *because they are holidays here*, 02-24
    onward is complete again). A universal hole alone cannot distinguish "closure" from
    "sync defect", which is why this flipped only once the owner ruled.
    """
    assert _dt.date(2026, 2, 23) in mh.holidays_for(MARKET_CHINA, 2026)
    assert is_session(_dt.date(2026, 2, 23)) is False
    # Not extrapolated: 02-24 is still a session, the rest of the block is untouched.
    assert is_session(_dt.date(2026, 2, 24)) is True
    assert is_session(_dt.date(2026, 2, 13)) is True


def test_expected_last_session_after_2026_02_23_lands_on_a_real_session():
    """Pre-fix this resolved to the phantom 2026-02-23 session, so every symbol failed.

    02-23 has no bars in any provider, so a 02-24 pre-close run compared the newest bar
    (02-13) against a phantom expectation (02-23) -> `bar < expected` -> spurious
    `strict_freshness` FAIL for the whole book. Corrected answer: 02-13.
    """
    pre_holiday = pd.DataFrame({"Close": [8.25]}, index=pd.DatetimeIndex(["2026-02-13"]))
    for hour in (0, 10, 15):
        resolved = expected_last_session(MARKET_CHINA, now=at_cst(2026, 2, 24, hour))
        assert resolved == _dt.date(2026, 2, 13)
        assert is_session(resolved) is True
        assert is_stale_bar(pre_holiday, resolved.isoformat()) is False
    # The spurious alarm in one line: the old phantom session made the same bar "stale".
    assert is_stale_bar(pre_holiday, "2026-02-23") is True
    # 02-24's own close+grace flips as-of to 02-24, which really is a session.
    assert expected_last_session(MARKET_CHINA, now=at_cst(2026, 2, 24, 15, 30)) == _dt.date(2026, 2, 24)
    # No flag change rides along with any of the three corrections.
    assert mh.PROVISIONAL_YEARS == frozenset({2026, 2027})


# --------------------------------------------------------------------------- #
# The structural invariant that must keep holding after both edits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("year", [2024, 2026])
def test_edited_blocks_still_contain_only_weekdays(year):
    closures = mh.holidays_for(MARKET_CHINA, year)
    assert all(d.weekday() < 5 for d in closures), year
    assert mh.early_closes_for(MARKET_CHINA, year) == {}  # no A-share half-days, unchanged
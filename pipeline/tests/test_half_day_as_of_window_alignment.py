"""Half-day alignment: `as_of` admits day D exactly when D's run window opens.

Contract under test (2026-11-12) -- ONE instant, not two:

    as_of admits day D  <=>  now >= close_time_on(D) + publication_grace
                        <=>  now is inside D's post-close -> next-open window

Before this fix `as_of_trading_date()` compared against the FULL-session close (16:00 +
grace) while `post_close_to_open_window()` started at the day-specific close (13:00 +
grace). On a US 13:00 half-day that left a real dead zone: the previous window ended at
09:30 and the half-day's window only became visible at 16:15, so between those two
instants NO window was open and the day could not legally be run at all.

The remaining 09:30..13:15 on such a day is *by design*: the session is still trading
(09:30..13:00) and the daily bar is not published yet (13:00..13:15). That is the
"already-open fill / unpublished bar" refusal, not the early-close bug.

Every instant below is injected into the clock. 100% offline, no network, no wall clock.
"""

import datetime as dt
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common.cli_utils import RunWindowError, assert_run_window, market_run_window
from common.market_clock import (
    MarketClock,
    SessionState,
    as_of_trading_date,
    in_post_close_to_open_window,
    next_trading_day,
    post_close_to_open_window,
)

_BEIJING = dt.timezone(dt.timedelta(hours=8))
# November 2026 is EST (UTC-5); Hong Kong is UTC+8 year-round.
_NY = dt.timezone(dt.timedelta(hours=-5))
_HK = dt.timezone(dt.timedelta(hours=8))

#: US day after Thanksgiving: recorded 13:00 early close (L2.27 table).
_US_HALF_DAY = dt.date(2026, 11, 27)       # Friday
_US_PREV_SESSION = dt.date(2026, 11, 25)   # Wednesday (11-26 is a full closure)
_US_NEXT_OPEN_DAY = dt.date(2026, 11, 30)  # Monday

#: HK Christmas Eve half-day: morning-only session, 12:00 close.
_HK_HALF_DAY = dt.date(2026, 12, 24)       # Thursday
_HK_PREV_SESSION = dt.date(2026, 12, 23)   # Wednesday
_HK_NEXT_OPEN_DAY = dt.date(2026, 12, 28)  # Monday (12-25 holiday, then weekend)


# --------------------------------------------------------------------------- #
# 1. US 13:00 half-day, 30-minute grace (the ruled numbers: 13:30 = usable)
# --------------------------------------------------------------------------- #
def test_half_day_1329_is_still_the_previous_session_and_the_window_is_shut():
    """09:30..13:30 is a LEGITIMATE refusal (grace not elapsed), not the early-close bug."""
    now = dt.datetime(2026, 11, 27, 13, 29, tzinfo=_NY)
    clock = MarketClock(market="us", now=now, publication_grace_minutes=30)

    assert clock.as_of_trading_date() == _US_PREV_SESSION
    assert clock.publication_ready() is False
    # `session_state` says CLOSED after the 13:00 half-day close (2026-11-13): the session
    # really has ended, so the honest reason for this refusal is the 13:00..13:30
    # publication grace -- not a still-trading session.
    assert clock.session_state() is SessionState.CLOSED

    window = market_run_window("us", "stage2", now=now, publication_grace_minutes=30)
    # The PREVIOUS session's window: opened 11-25 16:30, closed at 11-27 09:30.
    assert window == (
        dt.datetime(2026, 11, 25, 16, 30),
        dt.datetime(2026, 11, 27, 9, 30),
    )
    assert in_post_close_to_open_window(now, "us", publication_grace_minutes=30) is False
    with pytest.raises(RunWindowError):
        assert_run_window(clock, "stage2", window)

    # The dead zone that USED to exist (09:30..16:15) is gone: the very next minute past
    # close+grace is inside a window, and so is the whole old "16:15-only" afternoon.
    after = dt.datetime(2026, 11, 27, 13, 31, tzinfo=_NY)
    assert in_post_close_to_open_window(
        after, "us", publication_grace_minutes=30
    ) is True
    assert in_post_close_to_open_window(
        dt.datetime(2026, 11, 27, 15, 0, tzinfo=_NY),
        "us",
        publication_grace_minutes=30,
    ) is True


def test_half_day_1331_flips_as_of_and_opens_the_full_window():
    """13:31 == 13:00 close + 30min grace: as-of and the window start agree exactly."""
    now = dt.datetime(2026, 11, 27, 13, 31, tzinfo=_NY)
    clock = MarketClock(market="us", now=now, publication_grace_minutes=30)

    assert clock.publication_ready() is True
    assert clock.as_of_trading_date() == _US_HALF_DAY

    window = market_run_window("us", "stage2", now=now, publication_grace_minutes=30)
    assert window == (
        dt.datetime(2026, 11, 27, 13, 30),   # 13:00 + 30min, NOT 16:00 + 30min
        dt.datetime(2026, 11, 30, 9, 30),    # next trading day's normal open
    )
    # The anchor instant IS the window start -- one source, no gap after it.
    assert post_close_to_open_window(
        "us", now=now, publication_grace_minutes=30
    ) == window

    verdict = assert_run_window(clock, "stage2", window)
    assert verdict.outcome == "in_window"
    assert verdict.as_of == _US_HALF_DAY
    assert verdict.execution_date == _US_NEXT_OPEN_DAY


def test_half_day_default_us_grace_is_15_minutes_not_a_hard_coded_30():
    """The day-specific close is paired with the MARKET's own grace, not one constant."""
    before = dt.datetime(2026, 11, 27, 13, 14, tzinfo=_NY)
    at = dt.datetime(2026, 11, 27, 13, 15, tzinfo=_NY)

    assert as_of_trading_date(market="us", now=before) == _US_PREV_SESSION
    assert as_of_trading_date(market="us", now=at) == _US_HALF_DAY
    assert post_close_to_open_window("us", now=at)[0] == dt.datetime(
        2026, 11, 27, 13, 15
    )
    assert in_post_close_to_open_window(at, "us") is True


# --------------------------------------------------------------------------- #
# 2. Full sessions: byte-for-byte the pre-change answers
# --------------------------------------------------------------------------- #
def test_full_day_1529_and_1531_are_unchanged():
    before = dt.datetime(2026, 11, 12, 15, 29, tzinfo=_BEIJING)  # Thursday
    at = dt.datetime(2026, 11, 12, 15, 31, tzinfo=_BEIJING)

    assert as_of_trading_date(market="china", now=before) == dt.date(2026, 11, 11)
    assert post_close_to_open_window("china", now=before) == (
        dt.datetime(2026, 11, 11, 15, 30),
        dt.datetime(2026, 11, 12, 9, 30),
    )
    assert in_post_close_to_open_window(before, "china") is False

    assert as_of_trading_date(market="china", now=at) == dt.date(2026, 11, 12)
    assert post_close_to_open_window("china", now=at) == (
        dt.datetime(2026, 11, 12, 15, 30),
        dt.datetime(2026, 11, 13, 9, 30),
    )
    assert in_post_close_to_open_window(at, "china") is True

    # A normal US day is likewise untouched (16:00 + 15min, then the next 09:30).
    normal = dt.datetime(2026, 11, 25, 16, 20, tzinfo=_NY)
    assert as_of_trading_date(market="us", now=normal) == dt.date(2026, 11, 25)
    assert post_close_to_open_window("us", now=normal) == (
        dt.datetime(2026, 11, 25, 16, 15),
        dt.datetime(2026, 11, 27, 9, 30),  # 11-26 Thanksgiving -> skip the closure
    )


# --------------------------------------------------------------------------- #
# 3. Weekend / holiday still follow the trading calendar, not the half-day table
# --------------------------------------------------------------------------- #
def test_weekend_after_the_half_day_belongs_to_the_half_day_window():
    saturday = dt.datetime(2026, 11, 28, 10, 0, tzinfo=_NY)

    assert as_of_trading_date(market="us", now=saturday) == _US_HALF_DAY
    assert in_post_close_to_open_window(saturday, "us") is True
    assert post_close_to_open_window("us", now=saturday) == (
        dt.datetime(2026, 11, 27, 13, 15),  # the HALF-day's close, not 16:15
        dt.datetime(2026, 11, 30, 9, 30),
    )
    # Monday's pre-open is still Friday's window; one minute after the open it is not.
    assert in_post_close_to_open_window(
        dt.datetime(2026, 11, 30, 9, 0, tzinfo=_NY), "us"
    ) is True
    assert in_post_close_to_open_window(
        dt.datetime(2026, 11, 30, 9, 31, tzinfo=_NY), "us"
    ) is False


def test_thanksgiving_holiday_is_unaffected_and_uses_the_prior_session_window():
    holiday = dt.datetime(2026, 11, 26, 12, 0, tzinfo=_NY)  # full closure, not a half-day

    assert as_of_trading_date(market="us", now=holiday) == _US_PREV_SESSION
    assert post_close_to_open_window("us", now=holiday) == (
        dt.datetime(2026, 11, 25, 16, 15),
        dt.datetime(2026, 11, 27, 9, 30),
    )
    assert in_post_close_to_open_window(holiday, "us") is True
    assert next_trading_day("us", _US_PREV_SESSION) == _US_HALF_DAY


# --------------------------------------------------------------------------- #
# 4. HK half-day: morning-only, 12:00 close -> usable from 12:30
# --------------------------------------------------------------------------- #
def test_hk_half_day_flips_at_its_own_early_close_plus_grace():
    before = dt.datetime(2026, 12, 24, 12, 29, tzinfo=_HK)
    at = dt.datetime(2026, 12, 24, 12, 31, tzinfo=_HK)

    assert as_of_trading_date(market="hk", now=before) == _HK_PREV_SESSION
    assert in_post_close_to_open_window(before, "hk") is False
    assert as_of_trading_date(market="hk", now=at) == _HK_HALF_DAY
    assert post_close_to_open_window("hk", now=at) == (
        dt.datetime(2026, 12, 24, 12, 30),   # 12:00 + 30min
        dt.datetime(2026, 12, 28, 9, 30),    # 12-25 holiday + weekend
    )
    assert in_post_close_to_open_window(at, "hk") is True
    assert next_trading_day("hk", _HK_HALF_DAY) == _HK_NEXT_OPEN_DAY

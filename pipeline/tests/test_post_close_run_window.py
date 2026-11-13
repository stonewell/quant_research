"""The live run window is ONE range: `as_of` close + daily grace -> NEXT open (2026-11-12).

Pins the single-source contract:

* `common.market_clock.post_close_to_open_window()` owns the bounds (and only it);
* `common.cli_utils.market_run_window()` is a pure alias, so Stage 1 and Stage 2 cannot
  drift apart;
* the window CROSSES MIDNIGHT, so a weekend/holiday evening belongs to the previous
  session's window instead of being "not applicable";
* the end is the next **trading** day's open (Monday after a Friday, the day after a
  closure after a holiday), never "tomorrow 09:30";
* a recorded early close moves the START (close is 13:00 US), never the end.

Every instant below is injected; nothing here reads the wall clock. 100% offline.
"""

import datetime as dt
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

import common.cli_utils as cli_utils
from common.cli_utils import RunWindowError, assert_run_window, market_run_window
from common.market_clock import (
    MarketClock,
    format_run_window,
    in_post_close_to_open_window,
    next_trading_day,
    post_close_to_open_window,
)

_BEIJING = dt.timezone(dt.timedelta(hours=8))
_NY = dt.timezone(dt.timedelta(hours=-5))  # November: EST (no DST)


def _cn_clock(*args, **kwargs):
    """China clock at a naive market-LOCAL literal (tz attached, never host-converted)."""
    return MarketClock(market="china", now=dt.datetime(*args, tzinfo=_BEIJING, **kwargs))


# ---------------------------------------------------------------------------
# 1. The bounds, and the one-owner rule
# ---------------------------------------------------------------------------
def test_china_window_is_close_plus_grace_to_next_open():
    clock = _cn_clock(2026, 11, 12, 16, 0)  # Thursday, after publication
    window = market_run_window("china", "stage2", now=clock.now)

    assert window == (
        dt.datetime(2026, 11, 12, 15, 30),
        dt.datetime(2026, 11, 13, 9, 30),
    )
    assert post_close_to_open_window("china", now=clock.now) == window

    verdict = assert_run_window(clock, "stage2", window)
    assert verdict.outcome == "in_window"
    assert verdict.in_window is True
    assert verdict.as_of == dt.date(2026, 11, 12)
    assert verdict.execution_date == dt.date(2026, 11, 13)
    assert "2026-11-12 15:30 → 2026-11-13 09:30" in verdict.note

    # The persisted stamp uses the same pair (no re-derived `%H:%M` label).
    ctx_fields = {
        "run_window": format_run_window(window),
        "run_window_start": window[0].isoformat(),
        "run_window_end": window[1].isoformat(),
    }
    assert ctx_fields["run_window"] == "2026-11-12 15:30 → 2026-11-13 09:30"
    assert ctx_fields["run_window_start"] == "2026-11-12T15:30:00"


def test_both_stages_share_the_same_window():
    """Stage 1 (inspection) and Stage 2 (ticketing) read the SAME published close."""
    now = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)  # 16:00 Beijing
    assert market_run_window("china", "stage1", now=now) == market_run_window(
        "china", "stage2", now=now
    )
    with pytest.raises(ValueError):
        market_run_window("china", "stage9", now=now)


# ---------------------------------------------------------------------------
# 2. Weekend / holiday: the end is the next SESSION's open
# ---------------------------------------------------------------------------
def test_friday_window_ends_at_monday_open_not_tomorrow():
    friday_evening = _cn_clock(2026, 11, 13, 20, 0)
    window = market_run_window("china", "stage2", now=friday_evening.now)
    assert window == (
        dt.datetime(2026, 11, 13, 15, 30),
        dt.datetime(2026, 11, 16, 9, 30),  # Monday, not Saturday 09:30
    )

    # Saturday is INSIDE Friday's window -- it is not "not applicable".
    saturday = _cn_clock(2026, 11, 14, 10, 0)
    assert in_post_close_to_open_window(saturday.now, "china") is True
    verdict = assert_run_window(
        saturday, "stage2", market_run_window("china", "stage2", now=saturday.now)
    )
    assert verdict.outcome == "in_window"
    assert verdict.as_of == dt.date(2026, 11, 13)
    assert verdict.execution_date == dt.date(2026, 11, 16)


def test_holiday_end_is_the_next_session_after_the_closure():
    """China National Day 2026-10-01..08: the last session is 09-30, reopening 10-09."""
    before_holiday = _cn_clock(2026, 9, 30, 20, 0)
    window = market_run_window("china", "stage2", now=before_holiday.now)
    assert window == (
        dt.datetime(2026, 9, 30, 15, 30),
        dt.datetime(2026, 10, 9, 9, 30),
    )
    mid_holiday = _cn_clock(2026, 10, 4, 12, 0)
    assert in_post_close_to_open_window(mid_holiday.now, "china") is True
    assert (
        next_trading_day("china", dt.date(2026, 9, 30)) == dt.date(2026, 10, 9)
    )


# ---------------------------------------------------------------------------
# 3. Refusals: pre-publication and post-open
# ---------------------------------------------------------------------------
def test_pre_publication_instant_is_refused_with_actionable_facts():
    """15:05 is after the close but before close+grace: the as-of bar is not published."""
    clock = _cn_clock(2026, 11, 12, 15, 5)
    window = market_run_window("china", "stage2", now=clock.now)

    with pytest.raises(RunWindowError) as excinfo:
        assert_run_window(clock, "stage2", window)

    msg = str(excinfo.value)
    assert "RUN WINDOW GATE REFUSED" in msg
    assert "local_now=2026-11-12T15:05:00+08:00" in msg
    assert "expected_window=2026-11-11 15:30 → 2026-11-12 09:30" in msg
    assert "as_of=2026-11-11" in msg
    assert "execution_date=2026-11-12" in msg


def test_preopen_is_inside_the_window_but_after_the_open_is_not():
    """The window ENDS at the open: 09:00 is legal (fills at that open), 09:31 is not."""
    preopen = _cn_clock(2026, 11, 16, 9, 0)  # Monday
    window = market_run_window("china", "stage2", now=preopen.now)
    assert assert_run_window(preopen, "stage2", window).outcome == "in_window"

    after_open = _cn_clock(2026, 11, 16, 9, 31)
    window = market_run_window("china", "stage2", now=after_open.now)
    with pytest.raises(RunWindowError):
        assert_run_window(after_open, "stage2", window)

    # ... and `--allow-intraday` still only DOWNGRADES the gate (unchanged meaning).
    downgraded = assert_run_window(
        after_open, "stage2", window, allow_intraday=True
    )
    assert downgraded.outcome == "intraday_allowed"
    assert downgraded.in_window is False


def test_allow_intraday_note_names_the_window_not_a_time_literal():
    clock = _cn_clock(2026, 11, 16, 10, 0)
    window = market_run_window("china", "stage2", now=clock.now)
    verdict = assert_run_window(clock, "stage2", window, allow_intraday=True)
    assert "expected_window=2026-11-13 15:30 → 2026-11-16 09:30" in verdict.note
    assert "09:30" in verdict.note and "15:30" in verdict.note


# ---------------------------------------------------------------------------
# 4. Half-days move the START only
# ---------------------------------------------------------------------------
def test_half_day_start_uses_the_recorded_early_close():
    """US 2026-11-27 closes at 13:00 (recorded early close) -> the window opens 13:15.

    DISCLOSED RESIDUAL (`L2.27` scope, deliberately untouched here): the window's ANCHOR
    still flips on the full-session clock (16:00 + 15min), so the half-day window becomes
    visible at 16:15 rather than 13:15. That direction is LATE, never optimistic -- and once
    it is visible, its START is the recorded 13:00 close + grace, not 16:15.
    """
    clock = MarketClock(
        market="us", now=dt.datetime(2026, 11, 27, 16, 30, tzinfo=_NY)
    )
    window = market_run_window("us", "stage2", now=clock.now)
    assert window == (
        dt.datetime(2026, 11, 27, 13, 15),  # early close + 15min US grace
        dt.datetime(2026, 11, 30, 9, 30),  # next trading day's normal open
    )
    verdict = assert_run_window(clock, "stage2", window)
    assert verdict.as_of == dt.date(2026, 11, 27)
    assert verdict.execution_date == dt.date(2026, 11, 30)


def test_publication_grace_is_the_daily_axis_only():
    """A longer daily grace pushes the start; it is NOT the 5m publication rule."""
    now = dt.datetime(2026, 11, 12, 18, 0, tzinfo=_BEIJING)  # after both graces
    default = market_run_window("china", "stage2", now=now)
    slower = market_run_window("china", "stage2", now=now, publication_grace_minutes=120)
    assert default[0] == dt.datetime(2026, 11, 12, 15, 30)
    assert slower[0] == dt.datetime(2026, 11, 12, 17, 0)
    # The END is the next session's open either way.
    assert default[1] == slower[1] == dt.datetime(2026, 11, 13, 9, 30)

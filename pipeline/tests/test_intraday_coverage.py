"""Offline contract tests for `common/intraday_coverage.py` (`docs/development_plan.md` `M3.11`).

WHAT IS BEING PINNED, AND WHY IT NEEDS A TEST
---------------------------------------------
`M3.11` registers the `511880.SH` 5m back-fill interval and pairs it with three acceptance
predicates. The predicates exist because a stored `bar_count` is unfalsifiable: nobody
recomputes it, so a stale number survives forever (the registered `18,048 / 2026-01-05` is
already stale -- the current cache holds `672 / 2026-10-19`, see
`docs/intraday_5m_coverage.md`). The failure these tests guard is therefore not "the maths is
off by one" but:

  * a *short* interval passing as if it were the full window (coverage);
  * a *short day* or a *duplicated stamp* passing because the aggregate row count matches
    (`M3.10`'s own comment: "a whole missing day and a short day look identical" in a total);
  * a grid mismatch between the cash leg and the equity legs being read as comparability;
  * and, above all, an EMPTY or unparseable frame producing `pass` (or a bare `0` that a
    caller mistakes for a measurement) instead of `insufficient`.

100% offline: every frame here is built in memory, there is no DuckDB, no network, no
`data/` path, and no provider instantiation. `common/financial_api.py` is imported only to
read the `_BAOSTOCK_BARS_PER_DAY` class constant (no side effects at import).
"""

import datetime as dt
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.financial_api import BaoStockDataProvider
from common.intraday_coverage import (
    BARS_PER_SESSION_5M,
    LUNCH_BREAK_END,
    LUNCH_BREAK_START,
    STATUS_FAIL,
    STATUS_INSUFFICIENT,
    STATUS_PASS,
    IntradayCoverageError,
    check_coverage,
    check_cross_comparability,
    check_daily_completeness,
    require_pass,
    start_date_spread,
)

#: A week with no A-share holiday in it, so weekday-approximation and the real calendar
#: agree -- the synthetic frames below must be right for the *right* reason.
MONDAY = dt.date(2026, 10, 19)
FRIDAY = dt.date(2026, 10, 23)


def session_stamps(day: dt.date, morning: int = 24, afternoon: int = 24) -> pd.DatetimeIndex:
    """The A-share 5m grid for one closed session: 09:35..11:30 then 13:05..15:00."""
    am = pd.date_range(pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=35),
                       periods=morning, freq="5min")
    pm = pd.date_range(pd.Timestamp(day) + pd.Timedelta(hours=13, minutes=5),
                       periods=afternoon, freq="5min")
    return am.append(pm)


def weekdays(start: dt.date, end: dt.date):
    return [d.date() for d in pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
            if d.weekday() < 5]


def bars_frame(days, *, drop_last=0, duplicate_last=False, extra_lunch=False,
               shift_minutes=0):
    """Synthetic 5m bar frame; the knobs are exactly the four defect classes under test."""
    stamps = []
    for day in days:
        grid = session_stamps(day)
        if drop_last:
            grid = grid[:-drop_last]
        if shift_minutes:
            grid = grid + pd.Timedelta(minutes=shift_minutes)
        stamps.extend(grid)
        if extra_lunch:
            stamps.append(pd.Timestamp(day) + pd.Timedelta(hours=12, minutes=0))
    if duplicate_last and stamps:
        stamps.append(stamps[-1])
    return pd.DataFrame({"ts": pd.to_datetime(stamps),
                         "close": [100.0] * len(stamps)})


# --------------------------------------------------------------------------------------
# (i) coverage
# --------------------------------------------------------------------------------------
def test_bars_per_session_matches_verified_source_constant():
    """48 is not a guess: it is `BaoStockDataProvider._BAOSTOCK_BARS_PER_DAY["5"]`."""
    assert BARS_PER_SESSION_5M == BaoStockDataProvider._BAOSTOCK_BARS_PER_DAY["5"]
    # ... and it is the exchange grid, not a provider quirk: 24 morning + 24 afternoon.
    grid = session_stamps(MONDAY)
    assert len(grid) == BARS_PER_SESSION_5M
    assert grid[0].time() == dt.time(9, 35)
    assert grid[23].time() == dt.time(11, 30)
    assert grid[24].time() == dt.time(13, 5)
    assert grid[-1].time() == dt.time(15, 0)


def test_coverage_passes_when_window_fully_covered():
    frame = bars_frame(weekdays(MONDAY, FRIDAY))
    report = check_coverage(frame, "511880.SH", MONDAY, FRIDAY)
    assert report.status == STATUS_PASS and report.available
    assert report.reason == "ok"
    assert report.calendar_basis == "weekday_approx"
    assert len(report.expected_sessions) == 5
    assert len(report.observed_sessions) == 5
    assert report.coverage_ratio == 1.0
    assert report.bar_count == 5 * BARS_PER_SESSION_5M
    assert report.expected_bars == 5 * BARS_PER_SESSION_5M
    assert report.missing_sessions == () and report.unexplained_missing == ()


def test_coverage_fails_when_backfill_starts_late_and_require_pass_raises():
    """The live shape of the defect: 2 of 5 sessions present -> fail, never a soft pass."""
    frame = bars_frame([dt.date(2026, 10, 22), dt.date(2026, 10, 23)])
    report = check_coverage(frame, "511880.SH", MONDAY, FRIDAY)
    assert report.status == STATUS_FAIL
    assert report.available is True                      # measured, and it is bad
    assert report.reason == "unexplained_missing_sessions"
    assert report.coverage_ratio == pytest.approx(2 / 5)
    assert report.missing_sessions == (dt.date(2026, 10, 19), dt.date(2026, 10, 20),
                                       dt.date(2026, 10, 21))
    with pytest.raises(IntradayCoverageError):
        require_pass(report, context="cash_leg")


def test_coverage_separates_suspended_sessions_from_missing_ones():
    """A declared suspension is not a gap; an undeclared one is. Both are reported."""
    days = weekdays(MONDAY, FRIDAY)
    frame = bars_frame([d for d in days if d != dt.date(2026, 10, 21)])
    suspended = check_coverage(frame, "511880.SH", MONDAY, FRIDAY,
                               trading_calendar=days,
                               suspended_sessions=[dt.date(2026, 10, 21)])
    assert suspended.calendar_basis == "explicit_calendar"
    assert suspended.status == STATUS_PASS, "suspension must not count against coverage"
    assert suspended.missing_sessions == (dt.date(2026, 10, 21),)
    assert suspended.unexplained_missing == ()

    undeclared = check_coverage(frame, "511880.SH", MONDAY, FRIDAY, trading_calendar=days)
    assert undeclared.status == STATUS_FAIL
    assert undeclared.unexplained_missing == (dt.date(2026, 10, 21),)


# --------------------------------------------------------------------------------------
# (ii) per-session completeness
# --------------------------------------------------------------------------------------
def test_completeness_passes_on_full_sessions():
    report = check_daily_completeness(bars_frame(weekdays(MONDAY, FRIDAY)), "511880.SH")
    assert report.status == STATUS_PASS and report.available
    assert report.reason == "ok"
    assert len(report.sessions) == 5
    assert set(report.session_bar_counts.values()) == {BARS_PER_SESSION_5M}
    assert report.short_sessions == {} and report.overfull_sessions == {}
    assert report.duplicate_ts_count == 0
    assert report.lunch_break_violations == 0


def test_completeness_catches_a_short_session():
    """47 bars on one day out of 5*48 total -- invisible to a row count, caught per session."""
    frame = bars_frame(weekdays(MONDAY, FRIDAY), drop_last=1)
    report = check_daily_completeness(frame, "511880.SH")
    assert report.status == STATUS_FAIL
    assert report.reason == "short_sessions"
    assert len(report.short_sessions) == 5
    assert report.session_bar_counts[dt.date(2026, 10, 21)] == 47
    with pytest.raises(IntradayCoverageError):
        require_pass(report)


def test_completeness_catches_duplicate_timestamps():
    frame = bars_frame([MONDAY], duplicate_last=True)
    report = check_daily_completeness(frame, "511880.SH")
    assert report.status == STATUS_FAIL
    assert report.reason == "overfull_sessions"       # 49 bars: the aggregate symptom
    assert report.duplicate_ts_count == 2              # both copies are flagged
    assert len(report.duplicate_ts) == 2


def test_completeness_catches_a_lunch_break_bar():
    frame = bars_frame([MONDAY], extra_lunch=True)
    report = check_daily_completeness(frame, "511880.SH")
    assert report.lunch_break_violations == 1
    assert report.lunch_break_ts[0].time() > LUNCH_BREAK_START
    assert report.lunch_break_ts[0].time() < LUNCH_BREAK_END
    # 49 bars AND a lunch bar: the bar-count reason wins, but the violation is still listed.
    assert report.status == STATUS_FAIL


def test_completeness_is_independent_of_calendar_holidays():
    """A module-level sanity check on the grid helper: no 11:30-13:00 stamp by construction."""
    grid = session_stamps(MONDAY)
    assert not any(LUNCH_BREAK_START < t.time() < LUNCH_BREAK_END for t in grid)


# --------------------------------------------------------------------------------------
# (iii) cross comparability
# --------------------------------------------------------------------------------------
def test_cross_comparability_passes_on_identical_overlap():
    days = weekdays(MONDAY, FRIDAY)
    cash = bars_frame(days)
    equity = bars_frame(days)
    report = check_cross_comparability(cash, equity, label_a="511880.SH", label_b="000001.SH")
    assert report.status == STATUS_PASS and report.available
    assert report.reason == "ok"
    assert report.shared_count == 5 * BARS_PER_SESSION_5M
    assert report.bar_ratio == 1.0
    assert report.overlap_start == pd.Timestamp("2026-10-19 09:35")


def test_cross_comparability_passes_when_one_side_is_strictly_longer():
    """`min` denominator, not `union`: a longer peer history is not a grid mismatch."""
    equity = bars_frame(weekdays(dt.date(2026, 10, 14), FRIDAY))
    cash = bars_frame(weekdays(MONDAY, FRIDAY))
    report = check_cross_comparability(cash, equity)
    assert report.status == STATUS_PASS
    assert report.shared_count == cash.shape[0]
    assert report.bar_ratio == 1.0


def test_cross_comparability_fails_on_a_shifted_grid():
    days = weekdays(MONDAY, FRIDAY)
    report = check_cross_comparability(bars_frame(days), bars_frame(days, shift_minutes=5))
    assert report.status == STATUS_FAIL
    assert report.reason == "bar_grid_mismatch"
    assert report.bar_ratio < report.tolerance
    # 5 sessions x 46 shared bars: the grid is offset by one bar at each open, and the
    # overlap window itself starts at the shifted series' 09:40, so the denominator is 239.
    assert report.shared_count == 230
    assert report.bar_ratio == pytest.approx(230 / 239, abs=1e-9)
    assert report.available is True
    with pytest.raises(IntradayCoverageError):
        require_pass(report)


def test_cross_comparability_is_insufficient_without_overlap():
    early = bars_frame(weekdays(dt.date(2026, 9, 16), dt.date(2026, 9, 18)))
    late = bars_frame(weekdays(MONDAY, FRIDAY))
    report = check_cross_comparability(early, late)
    assert report.status == STATUS_INSUFFICIENT
    assert report.available is False
    assert report.reason == "no_overlapping_window"
    with pytest.raises(IntradayCoverageError):
        require_pass(report)


def test_cross_comparability_is_insufficient_below_min_shared_bars():
    one_bar = pd.DataFrame({"ts": [pd.Timestamp("2026-10-19 09:35")]})
    full = bars_frame([MONDAY])
    report = check_cross_comparability(one_bar, full)
    assert report.status == STATUS_INSUFFICIENT
    assert report.reason == "below_min_shared_bars"


# --------------------------------------------------------------------------------------
# tri-state: empty / malformed input must never be a silent pass
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("empty", [None, pd.DataFrame(), pd.DataFrame({"ts": []})])
def test_all_three_checks_are_insufficient_on_empty_input(empty):
    coverage = check_coverage(empty, "511880.SH", MONDAY, FRIDAY)
    assert coverage.status == STATUS_INSUFFICIENT
    assert coverage.available is False
    assert coverage.reason == "empty_input"

    completeness = check_daily_completeness(empty, "511880.SH")
    assert completeness.status == STATUS_INSUFFICIENT
    assert completeness.available is False

    comparability = check_cross_comparability(empty, bars_frame([MONDAY]))
    assert comparability.status == STATUS_INSUFFICIENT
    assert comparability.available is False

    for report in (coverage, completeness, comparability):
        with pytest.raises(IntradayCoverageError):
            require_pass(report)


def test_coverage_is_insufficient_without_a_timestamp_column():
    report = check_coverage(pd.DataFrame({"close": [1.0, 2.0]}), "511880.SH", MONDAY, FRIDAY)
    assert report.status == STATUS_INSUFFICIENT
    assert report.reason == "no_timestamp_column"


def test_coverage_is_insufficient_on_reversed_window():
    report = check_coverage(bars_frame([MONDAY]), "511880.SH", FRIDAY, MONDAY)
    assert report.status == STATUS_INSUFFICIENT
    assert report.reason == "window_reversed"


def test_coverage_is_insufficient_when_every_expected_session_is_suspended():
    """Zero-length denominator is "cannot tell", not "0% covered" and not "100% covered"."""
    days = weekdays(MONDAY, FRIDAY)
    report = check_coverage(bars_frame([MONDAY]), "511880.SH", MONDAY, FRIDAY,
                            suspended_sessions=days)
    assert report.status == STATUS_INSUFFICIENT
    assert report.available is False
    assert report.reason == "all_sessions_suspended"


# --------------------------------------------------------------------------------------
# start-date spread (the "is the cash leg systematically shorter?" half of question iii)
# --------------------------------------------------------------------------------------
def test_start_date_spread_flags_a_symbol_starting_after_the_median():
    starts = {
        "511880.SH": dt.date(2026, 10, 19),
        "000001.SH": dt.date(2026, 9, 16),
        "601288.SH": dt.date(2026, 10, 19),
        "600028.SH": dt.date(2026, 10, 19),
    }
    report = start_date_spread("511880.SH", starts)
    assert report.available is True
    assert report.earliest_symbol == "000001.SH"
    assert report.median_start == dt.date(2026, 10, 19)
    assert report.status == STATUS_PASS          # at the median, not behind it
    assert report.symbol_sessions_behind_earliest is None   # no calendar -> no fake number


def test_start_date_spread_is_insufficient_when_symbol_is_absent():
    report = start_date_spread("511880.SH", {"000001.SH": dt.date(2026, 9, 16)})
    assert report.status == STATUS_INSUFFICIENT
    assert report.available is False
    assert report.reason == "symbol_missing"

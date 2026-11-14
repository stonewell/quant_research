"""Offline tests for the `PROVISIONAL` disclosure of `common/market_holidays.py`.

WHY THESE EXIST
---------------
`PROVISIONAL_YEARS = {2026, 2027}` marks years whose statutory arrangement is **rule-derived**
and has not been re-verified against the exchange notices. Nothing in this repo can perform
that verification offline, so the only defensible engineering move is to make the
provisionality impossible to miss on a path that authorises orders -- and to pin it
mechanically, because a flag nobody tests is a flag that quietly disappears.

Three things are therefore pinned here:

  * the flag itself: 2020-2025 confirmed, 2026/2027 provisional, out-of-range years keep the
    existing UNKNOWN behaviour (`is_provisional` is about evidence quality, never a second
    coverage axis);
  * `provisional_years_covering(dates)`, the "which of these dates rests on a rule?" helper;
  * the live clock banner (`common/cli_utils.resolve_live_clock_context`) actually prints the
    warning on a provisional year and stays silent on a confirmed one -- a disclosure that no
    call site reads is not a disclosure.

100% offline: injected instants, static tables, no network, no real market data.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import market_holidays as mh
from common import cli_utils

_ALL_MARKETS = (mh.MARKET_CHINA, mh.MARKET_HK, mh.MARKET_US)

#: `2026-10-23 15:30` China local (== `07:30Z`, the close+grace instant, i.e. inside the
#: post-close window opened by that same session -- the shape the live runner runs in).
_BEIJING_2026_10_23_1530 = _dt.datetime(2026, 10, 23, 7, 30, tzinfo=_dt.timezone.utc)
#: Same wall-clock position two years earlier: a *confirmed* calendar year.
_BEIJING_2024_10_23_1530 = _dt.datetime(2024, 10, 23, 7, 30, tzinfo=_dt.timezone.utc)


class _LiveArgs:
    """The `argparse.Namespace` shape `resolve_live_clock_context` actually reads."""

    market = None
    as_of_date = None
    allow_intraday = False
    strict_market_clock = True
    publication_grace_minutes = None
    max_probe_attempts = 1
    strict_freshness = True
    allow_stale_prices = False


# ---------------------------------------------------------------------------
# 1. The flag: confirmed years vs provisional years vs UNKNOWN
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("market", _ALL_MARKETS)
@pytest.mark.parametrize("year", [2020, 2021, 2022, 2023, 2024, 2025])
def test_confirmed_years_are_not_provisional(market, year):
    assert mh.is_covered(market, year) is True
    assert mh.is_provisional(market, year) is False
    assert mh.calendar_status(market, year) == "covered"


@pytest.mark.parametrize("market", _ALL_MARKETS)
@pytest.mark.parametrize("year", [2026, 2027])
def test_provisional_years_are_flagged(market, year):
    assert mh.is_covered(market, year) is True
    assert mh.is_provisional(market, year) is True
    assert mh.calendar_status(market, year) == "provisional"


def test_provisional_years_constant_is_exactly_the_two_unverified_years():
    assert mh.PROVISIONAL_YEARS == frozenset({2026, 2027})
    # The flag must not leak outside the covered range: a year with no table is UNKNOWN,
    # which is a different (louder) answer than "rule-derived".
    assert not (mh.PROVISIONAL_YEARS - set(range(mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR + 1)))


@pytest.mark.parametrize("market", _ALL_MARKETS)
@pytest.mark.parametrize("year", [mh.COVERAGE_START_YEAR - 1, mh.COVERAGE_END_YEAR + 1])
def test_out_of_range_years_keep_the_existing_unknown_behaviour(market, year):
    """`is_provisional` must not become a second coverage axis."""
    assert mh.is_covered(market, year) is False
    assert mh.is_provisional(market, year) is False
    assert mh.calendar_status(market, year) == "uncovered"
    with pytest.raises(mh.HolidayCalendarCoverageError):
        mh.holidays_for(market, year)


# ---------------------------------------------------------------------------
# 2. `provisional_years_covering(dates)`
# ---------------------------------------------------------------------------
def test_provisional_years_covering_is_empty_for_confirmed_dates():
    assert mh.provisional_years_covering(()) == frozenset()
    assert mh.provisional_years_covering([_dt.date(2024, 3, 1), _dt.date(2025, 12, 31)]) == (
        frozenset()
    )
    assert mh.provisional_years_covering([None]) == frozenset()


def test_provisional_years_covering_finds_the_touched_provisional_years():
    assert mh.provisional_years_covering([_dt.date(2026, 1, 5)]) == frozenset({2026})
    # A window that straddles the new year touches both years' tables.
    assert mh.provisional_years_covering(
        [_dt.date(2025, 12, 31), _dt.date(2026, 1, 2)]
    ) == frozenset({2026})
    assert mh.provisional_years_covering(
        [_dt.date(2026, 12, 31), _dt.date(2027, 1, 4)]
    ) == frozenset({2026, 2027})


def test_provisional_years_covering_accepts_datetimes_and_iso_strings():
    stamp = _dt.datetime(2027, 3, 1, 15, 30)
    assert mh.provisional_years_covering([stamp]) == frozenset({2027})
    assert mh.provisional_years_covering(["2026-11-13", "2024-11-13"]) == frozenset({2026})


# ---------------------------------------------------------------------------
# 3. The live clock banner surfaces it (existing report surface, no new consumer)
# ---------------------------------------------------------------------------
def test_live_clock_banner_warns_on_a_provisional_year(capsys):
    cli_utils.resolve_live_clock_context(
        _LiveArgs(), ["600000.SH"], "stage2", now=_BEIJING_2026_10_23_1530
    )
    out = capsys.readouterr().out
    assert "[PROVISIONAL CALENDAR]" in out
    assert "2026" in out
    # The warning must name the mechanism a reader needs to act on, not just say "warn".
    assert "PROVISIONAL_YEARS" in out
    assert "market_holidays.py" in out


def test_live_clock_banner_stays_quiet_on_a_confirmed_year(capsys):
    cli_utils.resolve_live_clock_context(
        _LiveArgs(), ["600000.SH"], "stage2", now=_BEIJING_2024_10_23_1530
    )
    out = capsys.readouterr().out
    assert out.strip(), "the clock banner itself must still print"
    assert "[PROVISIONAL CALENDAR]" not in out


def test_live_clock_banner_stays_quiet_when_the_calendar_is_explicit(capsys):
    """An injected `holidays=` override is evidence by construction (`calendar_status=explicit`)."""
    cli_utils.resolve_live_clock_context(
        _LiveArgs(),
        ["600000.SH"],
        "stage2",
        now=_BEIJING_2026_10_23_1530,
        holidays=[_dt.date(2026, 10, 23)],
    )
    assert "[PROVISIONAL CALENDAR]" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. The written re-verification obligation (a docstring is the only place this can live)
# ---------------------------------------------------------------------------
def test_module_docstring_carries_the_reverification_checklist():
    doc = mh.__doc__ or ""
    assert "Re-verification checklist" in doc
    # Each market's notice must be named: a checklist that says "check the notices" without
    # saying WHICH notices is not actionable.
    for fragment in ("SSE", "SZSE", "HKEX", "NYSE", "Nasdaq"):
        assert fragment in doc, f"checklist must name {fragment}"
    # And what to change when a year is confirmed.
    assert "TABLE_REVISION" in doc
    assert "PROVISIONAL_YEARS" in doc

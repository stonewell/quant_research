"""Offline unit tests for `common/market_clock.py` + `common/market_holidays.py` (L2.1).

Design source: `docs/market_clock_and_asof_resolution.md` §4-§5, §10, §12 (cases 1-8).
Every case is a frozen, injected instant -- no wall clock, no network, no real market
data. Host time zone is irrelevant to these assertions by construction, which is the
whole point of the module under test.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import sys

import pytest

# Ensure project root is in sys.path (repo convention: common/tests has no package).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import market_holidays as mh
from common.market_clock import (
    ClockStamp,
    MARKET_CHINA,
    MARKET_HK,
    MARKET_US,
    MarketClock,
    MixedMarketUniverseError,
    NaiveDatetimeError,
    SessionState,
    UnknownSymbolMarketError,
    as_of_trading_date,
    coverage_years,
    expected_last_session,
    infer_market_from_symbol,
    infer_market_from_symbols,
    is_covered,
    is_provisional,
    last_expected_bar_ts,
    market_now,
    market_spec,
    normalize_market,
    render_clock_stamp,
    resolve_market,
    session_close_time,
    session_state,
)

UTC = _dt.timezone.utc
HOUR = _dt.timedelta(hours=1)


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> _dt.datetime:
    return _dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


def clock(market: str, y: int, mo: int, d: int, h: int = 0, mi: int = 0, **kw) -> MarketClock:
    return market_now(market, now=utc(y, mo, d, h, mi), **kw)


# --------------------------------------------------------------------------- #
# §5 decision table, row by row (design doc §12 cases 1-7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "case, market, instant, state, as_of",
    [
        # 1. Beijing 09:59 intraday -> previous session, OPEN.
        (1, MARKET_CHINA, utc(2026, 10, 23, 1, 59), SessionState.OPEN, _dt.date(2026, 10, 22)),
        # 2. Beijing 15:05: closed, but close+grace (15:30) not reached -> previous session.
        (2, MARKET_CHINA, utc(2026, 10, 23, 7, 5), SessionState.CLOSED, _dt.date(2026, 10, 22)),
        # 3. Beijing 15:30: closed and published -> today.
        (3, MARKET_CHINA, utc(2026, 10, 23, 7, 30), SessionState.CLOSED, _dt.date(2026, 10, 23)),
        # 4. Saturday -> previous Friday.
        (4, MARKET_CHINA, utc(2026, 10, 24, 4, 0), SessionState.NON_TRADING_DAY, _dt.date(2026, 10, 23)),
        # 5. Statutory holiday (2026 National Day) -> previous trading day.
        (5, MARKET_CHINA, utc(2026, 10, 1, 4, 0), SessionState.NON_TRADING_DAY, _dt.date(2026, 9, 30)),
        # 6. US 09:00 ET pre-open -> previous session (old date.today() said 10-23).
        (6, MARKET_US, utc(2026, 10, 23, 13, 0), SessionState.PRE_OPEN, _dt.date(2026, 10, 22)),
        # 7. US 17:00 ET -> closed and published (close 16:00 + 15 min grace).
        (7, MARKET_US, utc(2026, 10, 23, 21, 0), SessionState.CLOSED, _dt.date(2026, 10, 23)),
    ],
    ids=["1-intraday", "2-close-grace-pending", "3-close-published", "4-saturday",
         "5-holiday", "6-us-preopen", "7-us-postclose"],
)
def test_decision_table_rows(case, market, instant, state, as_of):
    clk = market_now(market, now=instant)
    assert clk.session_state() is state
    assert clk.as_of_trading_date() == as_of
    # as-of is always a real trading session, never a weekend/holiday.
    assert clk.is_trading_day(as_of)


def test_publication_ready_flips_only_at_close_plus_grace():
    assert clock(MARKET_CHINA, 2026, 10, 23, 7, 5).publication_ready() is False
    assert clock(MARKET_CHINA, 2026, 10, 23, 7, 29).publication_ready() is False
    assert clock(MARKET_CHINA, 2026, 10, 23, 7, 30).publication_ready() is True
    # Before the close it can never be ready, even with a zero grace.
    assert clock(MARKET_CHINA, 2026, 10, 23, 1, 59, publication_grace_minutes=0).publication_ready() is False


def test_publication_grace_is_per_market_and_overridable():
    assert clock(MARKET_CHINA, 2026, 10, 23).grace_minutes == 30
    assert clock(MARKET_HK, 2026, 10, 23).grace_minutes == 30
    assert clock(MARKET_US, 2026, 10, 23).grace_minutes == 15

    # Beijing 15:00:30 with grace=0 -> already published; default 30 -> still pending.
    instant = utc(2026, 10, 23, 7, 0) + _dt.timedelta(seconds=30)
    assert market_now(MARKET_CHINA, now=instant, publication_grace_minutes=0).as_of_trading_date() == _dt.date(2026, 10, 23)
    assert market_now(MARKET_CHINA, now=instant).as_of_trading_date() == _dt.date(2026, 10, 22)


def test_last_completed_session_is_pure_calendar_and_differs_from_as_of():
    """The window-gate question and the valuation question must not share one answer."""
    clk = clock(MARKET_CHINA, 2026, 10, 23, 7, 5)  # 15:05 CST
    assert clk.last_completed_session() == _dt.date(2026, 10, 23)   # closed
    assert clk.as_of_trading_date() == _dt.date(2026, 10, 22)       # not yet published
    assert clk.is_intraday is False


# --------------------------------------------------------------------------- #
# Session geometry per market (intraday / lunch / after close / pre-open)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market, instant, state",
    [
        # China A-share: auction 09:15-09:25, 09:30-11:30, 13:00-15:00.
        (MARKET_CHINA, utc(2026, 10, 23, 1, 14), SessionState.PRE_OPEN),
        (MARKET_CHINA, utc(2026, 10, 23, 1, 15), SessionState.OPENING_AUCTION),
        (MARKET_CHINA, utc(2026, 10, 23, 1, 25), SessionState.PRE_OPEN),
        (MARKET_CHINA, utc(2026, 10, 23, 1, 29), SessionState.PRE_OPEN),
        (MARKET_CHINA, utc(2026, 10, 23, 1, 30), SessionState.OPEN),           # 09:30:00
        (MARKET_CHINA, utc(2026, 10, 23, 3, 29), SessionState.OPEN),           # 11:29
        (MARKET_CHINA, utc(2026, 10, 23, 3, 30), SessionState.LUNCH_BREAK),    # 11:30:00
        (MARKET_CHINA, utc(2026, 10, 23, 4, 59), SessionState.LUNCH_BREAK),    # 12:59
        (MARKET_CHINA, utc(2026, 10, 23, 5, 0), SessionState.OPEN),            # 13:00:00
        (MARKET_CHINA, utc(2026, 10, 23, 6, 59), SessionState.OPEN),           # 14:59
        (MARKET_CHINA, utc(2026, 10, 23, 7, 0), SessionState.CLOSED),          # 15:00:00
        # Hong Kong: 09:30-12:00, 13:00-16:00, no call-auction state.
        (MARKET_HK, utc(2026, 10, 23, 1, 0), SessionState.PRE_OPEN),           # 09:00 pre-open
        (MARKET_HK, utc(2026, 10, 23, 1, 15), SessionState.PRE_OPEN),          # no auction in HK model
        (MARKET_HK, utc(2026, 10, 23, 1, 30), SessionState.OPEN),              # 09:30:00
        (MARKET_HK, utc(2026, 10, 23, 3, 59), SessionState.OPEN),              # 11:59
        (MARKET_HK, utc(2026, 10, 23, 4, 0), SessionState.LUNCH_BREAK),        # 12:00:00
        (MARKET_HK, utc(2026, 10, 23, 5, 0), SessionState.OPEN),               # 13:00:00
        (MARKET_HK, utc(2026, 10, 23, 7, 59), SessionState.OPEN),              # 15:59
        (MARKET_HK, utc(2026, 10, 23, 8, 0), SessionState.CLOSED),             # 16:00:00
        # US: continuous 09:30-16:00, so noon is OPEN (no lunch break).
        (MARKET_US, utc(2026, 10, 23, 12, 59), SessionState.PRE_OPEN),         # 08:59 ET
        (MARKET_US, utc(2026, 10, 23, 13, 30), SessionState.OPEN),             # 09:30:00 EDT
        (MARKET_US, utc(2026, 10, 23, 16, 0), SessionState.OPEN),              # 12:00 ET
        (MARKET_US, utc(2026, 10, 23, 19, 59), SessionState.OPEN),             # 15:59 ET
        (MARKET_US, utc(2026, 10, 23, 20, 0), SessionState.CLOSED),            # 16:00:00 EDT
    ],
)
def test_session_state_geometry_and_half_open_boundaries(market, instant, state):
    assert market_now(market, now=instant).session_state() is state


def test_is_intraday_covers_open_and_lunch_only():
    """§5 "盘中（含午休）-> 默认拒绝": auction / pre-open are NOT intraday."""
    assert clock(MARKET_CHINA, 2026, 10, 23, 1, 59).is_intraday is True     # 09:59
    assert clock(MARKET_CHINA, 2026, 10, 23, 4, 0).is_intraday is True      # lunch 12:00
    assert clock(MARKET_CHINA, 2026, 10, 23, 1, 20).is_intraday is False    # call auction
    assert clock(MARKET_CHINA, 2026, 10, 23, 0, 30).is_intraday is False    # pre-open
    assert clock(MARKET_CHINA, 2026, 10, 23, 7, 30).is_intraday is False    # closed


def test_session_state_enum_holiday_is_an_alias_of_non_trading_day():
    """A distinct HOLIDAY member would let consumers forget to check it (§5 folds them)."""
    assert SessionState.HOLIDAY is SessionState.NON_TRADING_DAY
    assert SessionState.HOLIDAY.value == "non_trading_day"
    clk = clock(MARKET_CHINA, 2026, 10, 1, 4, 0)
    assert clk.session_state() is SessionState.NON_TRADING_DAY
    # ...while observability still distinguishes why trading stopped.
    assert clk.non_trading_day_reason() == "holiday"
    assert clock(MARKET_CHINA, 2026, 10, 24, 4, 0).non_trading_day_reason() == "weekend"
    assert clock(MARKET_CHINA, 2026, 10, 23, 4, 0).non_trading_day_reason() is None


# --------------------------------------------------------------------------- #
# Weekends, holidays, cross-year and Spring-Festival boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market, instant, as_of",
    [
        # Weekend: both weekend days resolve to the same Friday.
        (MARKET_CHINA, utc(2026, 10, 24, 4, 0), _dt.date(2026, 10, 23)),
        (MARKET_CHINA, utc(2026, 10, 25, 4, 0), _dt.date(2026, 10, 23)),
        # Chinese New Year block 2026-02-16..20 -> last session 2026-02-13.
        (MARKET_CHINA, utc(2026, 2, 16, 4, 0), _dt.date(2026, 2, 13)),
        (MARKET_CHINA, utc(2026, 2, 20, 2, 0), _dt.date(2026, 2, 13)),
        # Cross-year: 2027-01-01 holiday resolves back into 2026.
        (MARKET_CHINA, utc(2027, 1, 1, 2, 0), _dt.date(2026, 12, 31)),
        # HK holiday (HKSAR day) and US holiday (Juneteenth), both Friday 2026.
        (MARKET_HK, utc(2026, 7, 1, 4, 0), _dt.date(2026, 6, 30)),
        (MARKET_US, utc(2026, 6, 19, 16, 0), _dt.date(2026, 6, 18)),
    ],
)
def test_weekend_and_holiday_roll_back_to_previous_session(market, instant, as_of):
    clk = market_now(market, now=instant)
    assert clk.session_state() is SessionState.NON_TRADING_DAY
    assert clk.as_of_trading_date() == as_of
    assert clk.last_completed_session() == as_of


def test_first_session_after_spring_festival_valued_against_pre_holiday_close():
    clk = clock(MARKET_CHINA, 2026, 2, 23, 1, 59)  # Monday 09:59 CST, market reopened
    assert clk.session_state() is SessionState.OPEN
    assert clk.as_of_trading_date() == _dt.date(2026, 2, 13)


def test_as_of_never_lands_on_a_weekend_or_holiday_across_two_months():
    """Property guard: whatever the instant, as-of must be a real session."""
    for day in range(1, 32):
        for hour in (0, 3, 7, 12, 16, 23):
            try:
                instant = utc(2026, 2, day, hour)
            except ValueError:  # pragma: no cover - February never has 30/31 here
                continue
            clk = market_now(MARKET_CHINA, now=instant)
            assert clk.is_trading_day(clk.as_of_trading_date())
    for day in range(1, 32):
        clk = market_now(MARKET_CHINA, now=utc(2026, 10, day, 3, 0))
        assert clk.is_trading_day(clk.as_of_trading_date())


def test_injected_holiday_overrides_the_static_table():
    """Design §12 case 5: an injected calendar wins over the shipped table."""
    injected = frozenset({_dt.date(2026, 10, 23)})
    clk = market_now(MARKET_CHINA, now=utc(2026, 10, 23, 2, 0), holidays=injected)
    assert clk.session_state() is SessionState.NON_TRADING_DAY
    assert clk.as_of_trading_date() == _dt.date(2026, 10, 22)
    assert clk.is_holiday() is True

    # An explicitly empty override means "no closures" -- including National Day.
    # This is the ONE way to bypass the table, and it is caller-supplied, not silent.
    empty = market_now(MARKET_CHINA, now=utc(2026, 10, 1, 2, 0), holidays=frozenset())
    assert empty.session_state() is SessionState.OPEN


# --------------------------------------------------------------------------- #
# US DST handling (design §12 case 8) -- zoneinfo, never hard-coded offsets
# --------------------------------------------------------------------------- #
def test_us_dst_offsets_and_open_determination_across_spring_forward():
    # Friday 2026-03-06 standard time (UTC-5): 09:30 ET == 14:30Z.
    std = clock(MARKET_US, 2026, 3, 6, 14, 30)
    assert std.local_now().utcoffset() == _dt.timedelta(hours=-5)
    assert std.session_state() is SessionState.OPEN
    # Monday 2026-03-09 daylight time (UTC-4): 09:30 ET == 13:30Z.
    dst = clock(MARKET_US, 2026, 3, 9, 13, 30)
    assert dst.local_now().utcoffset() == _dt.timedelta(hours=-4)
    assert dst.session_state() is SessionState.OPEN
    assert clock(MARKET_US, 2026, 3, 9, 13, 29).session_state() is SessionState.PRE_OPEN
    # The same UTC instant is 08:30 ET before the switch -> still pre-open.
    assert clock(MARKET_US, 2026, 3, 6, 13, 30).session_state() is SessionState.PRE_OPEN


def test_us_dst_offsets_and_open_determination_across_fall_back():
    # Friday 2026-10-30 daylight time (UTC-4): 10:00 ET == 14:00Z.
    dst = clock(MARKET_US, 2026, 10, 30, 14, 0)
    assert dst.local_now().utcoffset() == _dt.timedelta(hours=-4)
    assert dst.session_state() is SessionState.OPEN
    # Monday 2026-11-02 standard time (UTC-5): 10:00 ET == 15:00Z.
    std = clock(MARKET_US, 2026, 11, 2, 15, 0)
    assert std.local_now().utcoffset() == _dt.timedelta(hours=-5)
    assert std.session_state() is SessionState.OPEN
    # 09:30 ET == 14:30Z in standard time, 13:30Z in daylight time.
    assert clock(MARKET_US, 2026, 11, 2, 14, 29).session_state() is SessionState.PRE_OPEN
    assert clock(MARKET_US, 2026, 11, 2, 14, 30).session_state() is SessionState.OPEN


@pytest.mark.parametrize("instant", [utc(2026, 3, 8, 16, 0), utc(2026, 11, 1, 16, 0)])
def test_us_dst_switch_sundays_are_non_trading_days(instant):
    """Both 2026 US switch days are Sundays: the session table must not open them."""
    clk = market_now(MARKET_US, now=instant)
    assert clk.session_state() is SessionState.NON_TRADING_DAY


def test_us_as_of_is_stable_across_dst_transition():
    assert clock(MARKET_US, 2026, 3, 9, 13, 30).as_of_trading_date() == _dt.date(2026, 3, 6)
    assert clock(MARKET_US, 2026, 3, 9, 21, 0).as_of_trading_date() == _dt.date(2026, 3, 9)


# --------------------------------------------------------------------------- #
# Run-window gate (design §8): market-local literals, never host-local
# --------------------------------------------------------------------------- #
def test_in_run_window_uses_market_local_time():
    window = (_dt.time(13, 55), _dt.time(14, 20))
    assert clock(MARKET_CHINA, 2026, 10, 23, 6, 0).in_run_window(window) is True    # 14:00 CST
    assert clock(MARKET_CHINA, 2026, 10, 23, 5, 54).in_run_window(window) is False  # 13:54
    assert clock(MARKET_CHINA, 2026, 10, 23, 6, 20).in_run_window(window) is False  # 14:20 (half-open)
    # Same UTC instant is 23:00 the previous day in New York -> outside a US window.
    assert clock(MARKET_US, 2026, 10, 23, 6, 0).in_run_window(window) is False


def test_in_run_window_is_closed_on_non_trading_days():
    window = (_dt.time(9, 30), _dt.time(16, 0))
    assert clock(MARKET_CHINA, 2026, 10, 24, 4, 0).in_run_window(window) is False   # Saturday
    assert clock(MARKET_CHINA, 2026, 10, 1, 4, 0).in_run_window(window) is False    # holiday


def test_in_run_window_supports_windows_crossing_midnight():
    window = (_dt.time(22, 0), _dt.time(2, 0))
    assert clock(MARKET_US, 2026, 10, 23, 3, 0).in_run_window(window) is True   # 23:00 ET
    assert clock(MARKET_US, 2026, 10, 23, 5, 0).in_run_window(window) is True   # 01:00 ET
    assert clock(MARKET_US, 2026, 10, 23, 16, 0).in_run_window(window) is False  # 12:00 ET


def test_in_run_window_rejects_tz_aware_or_degenerate_literals():
    clk = clock(MARKET_CHINA, 2026, 10, 23, 6, 0)
    with pytest.raises(ValueError, match="market-LOCAL"):
        clk.in_run_window((_dt.time(13, 55, tzinfo=UTC), _dt.time(14, 20)))
    with pytest.raises(ValueError, match="degenerate window"):
        clk.in_run_window((_dt.time(14, 0), _dt.time(14, 0)))


# --------------------------------------------------------------------------- #
# Symbol -> market inference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "symbol, market",
    [
        ("600000.SH", MARKET_CHINA),
        ("000001.SZ", MARKET_CHINA),
        ("430047.BJ", MARKET_CHINA),
        ("511880.SH", MARKET_CHINA),
        ("600000", MARKET_CHINA),      # bare 6-digit A-share code
        ("00700.HK", MARKET_HK),
        ("0700.HK", MARKET_HK),
        ("00700", MARKET_HK),          # bare 5-digit HK code
        ("AAPL", MARKET_US),
        ("SPY", MARKET_US),
        ("BIL", MARKET_US),
        ("BRK.B", MARKET_US),          # ticker dot, not a market suffix
        ("^GSPC", MARKET_US),
        ("aapl", MARKET_US),           # case-insensitive
        (" 600000.sh ", MARKET_CHINA), # whitespace tolerated
    ],
)
def test_infer_market_from_symbol(symbol, market):
    assert infer_market_from_symbol(symbol) == market


def test_infer_market_from_symbols_agrees_with_single_symbol_inference():
    assert infer_market_from_symbols(["600000.SH", "000001.SZ", "430047.BJ"]) == MARKET_CHINA
    assert infer_market_from_symbols(["00700.HK", "00005.HK"]) == MARKET_HK
    assert infer_market_from_symbols(["AAPL", "SPY", "BIL"]) == MARKET_US
    assert infer_market_from_symbols([]) is None
    assert infer_market_from_symbols({}) is None
    assert infer_market_from_symbols("AAPL") == MARKET_US
    # Mapping input uses its keys (universe dicts are the live-call shape).
    assert infer_market_from_symbols({"600000.SH": object(), "000001.SZ": object()}) == MARKET_CHINA


def test_infer_market_from_symbols_rejects_mixed_universe():
    with pytest.raises(MixedMarketUniverseError) as excinfo:
        infer_market_from_symbols(["600000.SH", "00700.HK", "AAPL"])
    message = str(excinfo.value)
    assert "600000.SH" in message and "00700.HK" in message and "AAPL" in message
    assert "china=[" in message and "hk=[" in message and "us=[" in message


@pytest.mark.parametrize("symbol", ["", "   ", "!!", "12345.XX", "123", "600000AB"])
def test_infer_market_from_symbol_fails_loud_on_unrecognised_input(symbol):
    with pytest.raises(UnknownSymbolMarketError):
        infer_market_from_symbol(symbol)


def test_infer_market_from_symbols_propagates_unknown_symbol_error():
    with pytest.raises(UnknownSymbolMarketError):
        infer_market_from_symbols(["AAPL", "99999.ZZ"])


def test_resolve_market_prefers_explicit_and_handles_mixed_books():
    assert resolve_market("hk", ["600000.SH", "AAPL"]) == MARKET_HK   # explicit wins
    assert resolve_market("China", ["AAPL"]) == MARKET_CHINA         # alias + explicit
    assert resolve_market(None, ["AAPL", "SPY"]) == MARKET_US        # inferred
    with pytest.raises(MixedMarketUniverseError):
        resolve_market(None, ["600000.SH", "AAPL"])
    with pytest.raises(mh.UnknownMarketError):
        resolve_market(None, [])
    with pytest.raises(mh.UnknownMarketError):
        resolve_market("moon", ["AAPL"])


def test_normalize_market_accepts_aliases_and_rejects_nonsense():
    assert normalize_market(" A-Share ") == MARKET_CHINA
    assert normalize_market("Hong_Kong") == MARKET_HK
    assert normalize_market("USA") == MARKET_US
    with pytest.raises(mh.UnknownMarketError):
        normalize_market("mars")


# --------------------------------------------------------------------------- #
# Naive datetime: explicit failure, never a silent offset guess
# --------------------------------------------------------------------------- #
def test_naive_datetime_is_rejected_at_the_constructor():
    naive = _dt.datetime(2026, 10, 23, 9, 59)
    with pytest.raises(NaiveDatetimeError) as excinfo:
        market_now(MARKET_CHINA, now=naive)
    assert "timezone-aware" in str(excinfo.value)
    with pytest.raises(NaiveDatetimeError):
        MarketClock(market=MARKET_CHINA, now=naive)
    with pytest.raises(NaiveDatetimeError):
        clock(MARKET_CHINA, 2026, 10, 23, 1, 59).at(naive)


def test_naive_datetime_is_rejected_by_the_free_functions():
    naive = _dt.datetime(2026, 10, 23, 9, 59)
    with pytest.raises(NaiveDatetimeError):
        session_state(naive, MARKET_CHINA)
    with pytest.raises(NaiveDatetimeError):
        as_of_trading_date(naive, MARKET_CHINA)
    # NaiveDatetimeError stays catchable as the "bad input" base class.
    assert issubclass(NaiveDatetimeError, ValueError)


def test_aware_datetime_in_any_zone_is_normalized_to_utc():
    from zoneinfo import ZoneInfo

    shanghai = _dt.datetime(2026, 10, 23, 9, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    clk = market_now(MARKET_CHINA, now=shanghai)
    assert clk.now == utc(2026, 10, 23, 1, 59)
    assert clk.now.tzinfo is UTC
    assert clk.session_state() is SessionState.OPEN


def test_unknown_market_id_is_rejected_at_construction():
    with pytest.raises(mh.UnknownMarketError):
        market_now("mars", now=utc(2026, 10, 23, 1, 59))
    with pytest.raises(mh.UnknownMarketError):
        MarketClock(market="mars", now=utc(2026, 10, 23, 1, 59))


def test_market_clock_is_immutable():
    clk = clock(MARKET_CHINA, 2026, 10, 23, 1, 59)
    with pytest.raises(dataclasses.FrozenInstanceError):
        clk.market = MARKET_US  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Holiday table: coverage, structural invariants, UNKNOWN != empty
# --------------------------------------------------------------------------- #
def test_every_market_covers_the_documented_years():
    for market in mh.SUPPORTED_MARKETS:
        assert coverage_years(market) == (mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR)
        for year in range(mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR + 1):
            assert is_covered(market, year)
            assert is_provisional(market, year) is (year in mh.PROVISIONAL_YEARS)


def test_holiday_tables_contain_only_weekdays_and_no_duplicates():
    """Weekend dates inside a closure block are omitted on purpose (docstring §3)."""
    for market in mh.SUPPORTED_MARKETS:
        dates = [d for year in range(mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR + 1)
                 for d in mh.holidays_for(market, year)]
        assert len(dates) == len(set(dates))
        assert all(d.weekday() < 5 for d in dates), market
        assert dates, f"{market} has an empty calendar"


@pytest.mark.parametrize(
    "market, dates",
    [
        (MARKET_CHINA, [_dt.date(2026, 10, 1), _dt.date(2026, 10, 8), _dt.date(2026, 2, 17)]),
        (MARKET_HK, [_dt.date(2026, 7, 1), _dt.date(2026, 2, 17)]),
        (MARKET_US, [_dt.date(2026, 7, 3), _dt.date(2026, 1, 19), _dt.date(2026, 6, 19)]),
    ],
)
def test_known_landmark_closures_are_present(market, dates):
    holidays = mh.all_holidays(market)
    for d in dates:
        assert d in holidays


def test_unknown_year_is_explicit_never_an_empty_set():
    with pytest.raises(mh.HolidayCalendarCoverageError) as excinfo:
        mh.holidays_for(MARKET_CHINA, 2019)
    assert "UNKNOWN" in str(excinfo.value)
    assert mh.holidays_for_or_none(MARKET_CHINA, 2019) is None      # UNKNOWN
    assert mh.holidays_for_or_none(MARKET_CHINA, 2026) is not None  # known
    assert mh.holidays_for_or_none(MARKET_CHINA, 2026) != frozenset()
    assert is_covered(MARKET_CHINA, 2019) is False
    assert mh.calendar_status(MARKET_CHINA, 2019) == "uncovered"
    assert mh.calendar_status(MARKET_CHINA, 2026) == "provisional"
    assert mh.calendar_status(MARKET_CHINA, 2024) == "covered"


def test_clock_on_an_uncovered_weekday_raises_instead_of_assuming_open():
    clk = clock(MARKET_CHINA, 2019, 6, 3, 2, 0)  # Monday 10:00 CST, year not in tables
    assert clk.calendar_status() == "uncovered"  # queryable without raising
    with pytest.raises(mh.HolidayCalendarCoverageError):
        clk.session_state()
    with pytest.raises(mh.HolidayCalendarCoverageError):
        clk.as_of_trading_date()


def test_uncovered_year_weekend_still_answers_without_the_calendar():
    """Documented exception: Sat/Sun are structurally non-trading, so no table needed."""
    clk = clock(MARKET_CHINA, 2019, 6, 1, 4, 0)  # Saturday
    assert clk.session_state() is SessionState.NON_TRADING_DAY
    with pytest.raises(mh.HolidayCalendarCoverageError):
        clk.as_of_trading_date()  # walking back to Friday needs the unknown calendar


def test_explicit_calendar_makes_an_uncovered_year_usable():
    clk = market_now(
        MARKET_CHINA,
        now=utc(2019, 6, 3, 2, 0),
        holidays=frozenset({_dt.date(2019, 6, 3)}),
    )
    assert clk.calendar_status() == "explicit"
    assert clk.session_state() is SessionState.NON_TRADING_DAY
    assert clk.as_of_trading_date() == _dt.date(2019, 5, 31)  # Friday


def test_holidays_in_range_rejects_partially_known_spans():
    got = mh.holidays_in_range(MARKET_CHINA, _dt.date(2026, 10, 1), _dt.date(2026, 10, 8))
    assert _dt.date(2026, 10, 5) in got
    with pytest.raises(mh.HolidayCalendarCoverageError):
        mh.holidays_in_range(MARKET_CHINA, _dt.date(2019, 12, 30), _dt.date(2020, 1, 2))
    with pytest.raises(ValueError):
        mh.holidays_in_range(MARKET_CHINA, _dt.date(2026, 2, 2), _dt.date(2026, 2, 1))


# --------------------------------------------------------------------------- #
# Convenience API + observability stamp
# --------------------------------------------------------------------------- #
def test_expected_last_session_is_the_as_of_date():
    """`L2.2` will pass this value as `expected_last_session` to the cache layer."""
    instant = utc(2026, 10, 23, 1, 59)
    assert expected_last_session(MARKET_CHINA, now=instant) == _dt.date(2026, 10, 22)
    assert expected_last_session(MARKET_CHINA, now=instant) == as_of_trading_date(instant, MARKET_CHINA)
    # Free-function default market is A-shares (the primary live book), documented.
    assert as_of_trading_date(utc(2026, 10, 23, 7, 30)) == _dt.date(2026, 10, 23)


def test_clock_stamp_and_render_contain_the_observability_fields():
    clk = clock(MARKET_CHINA, 2026, 10, 23, 1, 59)
    stamp = clk.stamp()
    assert isinstance(stamp, ClockStamp)
    payload = stamp.to_dict()
    assert payload["market"] == "china"
    assert payload["session"] == "open"
    assert payload["as_of_trading_date"] == "2026-10-22"
    assert payload["utc_now"] == "2026-10-23T01:59:00+00:00"
    assert payload["local_now"].startswith("2026-10-23T09:59:00+08:00")
    assert payload["calendar_status"] == "provisional"

    line = render_clock_stamp(clk)
    assert "market=china" in line and "session=open" in line and "as_of=2026-10-22" in line


def test_clock_tz_is_zoneinfo_for_each_market():
    assert clock(MARKET_CHINA, 2026, 10, 23).tz.key == "Asia/Shanghai"
    assert clock(MARKET_HK, 2026, 10, 23).tz.key == "Asia/Hong_Kong"
    assert clock(MARKET_US, 2026, 10, 23).tz.key == "America/New_York"


# --------------------------------------------------------------------------- #
# L2.26: the intraday cache gate needs "when does a session actually end?".
#
# Added as READ-ONLY accessors only -- `session_close_time` / `last_expected_bar_ts` and the
# `MarketSpec.has_lunch_break` flag. None of the L2.1 semantics (`as_of_trading_date`,
# `last_completed_session`, `session_state`, `in_run_window`) is touched by them.
# --------------------------------------------------------------------------- #
SESSION_DAY = _dt.date(2026, 10, 23)
FIVE_MIN = _dt.timedelta(minutes=5)


def test_session_close_time_is_the_afternoon_close_for_every_market():
    assert session_close_time(MARKET_CHINA) == _dt.time(15, 0)
    assert session_close_time(MARKET_HK) == _dt.time(16, 0)
    assert session_close_time(MARKET_US) == _dt.time(16, 0)
    # ...and it agrees with the already-public market spec / clock (no second opinion).
    for market in (MARKET_CHINA, MARKET_HK, MARKET_US):
        assert session_close_time(market) == market_spec(market).close_time


def test_last_expected_bar_ts_leaves_one_bar_of_tolerance():
    assert last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, FIVE_MIN) == _dt.datetime(
        2026, 10, 23, 14, 55
    )
    assert last_expected_bar_ts(MARKET_HK, SESSION_DAY, FIVE_MIN) == _dt.datetime(
        2026, 10, 23, 15, 55
    )
    assert last_expected_bar_ts(MARKET_US, SESSION_DAY, FIVE_MIN) == _dt.datetime(
        2026, 10, 23, 15, 55
    )
    # A 1h bar shifts the deadline by an hour (the tolerance is the bar length, not a constant).
    assert last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, _dt.timedelta(hours=1)) == _dt.datetime(
        2026, 10, 23, 14, 0
    )
    # Always naive market-local, on the session day itself.
    deadline = last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, FIVE_MIN)
    assert deadline.tzinfo is None and deadline.date() == SESSION_DAY


def test_last_expected_bar_ts_rejects_a_lunch_stop_but_accepts_the_close():
    """The lunch-vs-close distinction the L2.26 cache gate relies on.

    There is no "is it lunch now?" state to consult (the cache is judging a *stored* session),
    so the clock answers the question instead: the morning close is strictly earlier than the
    deadline, the afternoon close is not.
    """
    china = last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, FIVE_MIN)
    hk = last_expected_bar_ts(MARKET_HK, SESSION_DAY, FIVE_MIN)
    assert _dt.datetime(2026, 10, 23, 11, 30) < china, "A-share 11:30 must fail the deadline"
    assert _dt.datetime(2026, 10, 23, 12, 0) < hk, "HK 12:00 must fail the deadline"
    assert _dt.datetime(2026, 10, 23, 15, 0) >= china
    assert _dt.datetime(2026, 10, 23, 16, 0) >= hk


def test_market_spec_has_lunch_break_flag_is_observability_only():
    assert market_spec(MARKET_CHINA).has_lunch_break is True
    assert market_spec(MARKET_HK).has_lunch_break is True
    assert market_spec(MARKET_US).has_lunch_break is False


def test_last_expected_bar_ts_validates_its_inputs():
    with pytest.raises(TypeError, match="plain datetime.date"):
        last_expected_bar_ts(MARKET_CHINA, _dt.datetime(2026, 10, 23, 12, 0), FIVE_MIN)
    with pytest.raises(TypeError, match="datetime.timedelta"):
        last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, 5)
    with pytest.raises(ValueError, match="positive"):
        last_expected_bar_ts(MARKET_CHINA, SESSION_DAY, _dt.timedelta(0))
    with pytest.raises(Exception):
        last_expected_bar_ts("atlantis", SESSION_DAY, FIVE_MIN)


# --------------------------------------------------------------------------- #
# L2.27: half-day / early-close (早收) modelling.
#
# `L2.26` made the intraday freshness deadline "session close minus one bar" and took the
# close from this module. That turned the `L2.1` disclosure "half-days are not modelled"
# into a live defect: a US 13:00 day (day after Thanksgiving, July 3, Christmas Eve) can
# never satisfy a 15:55 deadline, so a *correctly* synced cache was judged uncovered and
# re-fetched upstream forever. The clock now asks `common/market_holidays.py` for the
# date's real close instead of assuming one close per market.
#
# Everything below is frozen literals -- no wall clock, no network, no real market data.
# --------------------------------------------------------------------------- #
US_EARLY = _dt.date(2026, 11, 27)    # day after Thanksgiving 2026-11-26 -> 13:00
US_NORMAL = _dt.date(2026, 11, 25)   # Wednesday, full 16:00 session
HK_EARLY = _dt.date(2026, 12, 24)    # Christmas Eve -> 12:00, no afternoon session


def test_session_close_time_without_a_date_is_the_full_session():
    """The no-date call keeps the `L2.26` signature AND answer (no caller has to change)."""
    assert session_close_time(MARKET_CHINA) == _dt.time(15, 0)
    assert session_close_time(MARKET_HK) == _dt.time(16, 0)
    assert session_close_time(MARKET_US) == _dt.time(16, 0)
    # A normal day spells the same thing with an explicit date -- that is the "unaffected"
    # half of the L2.27 acceptance criteria.
    assert session_close_time(MARKET_US, US_NORMAL) == _dt.time(16, 0)
    assert session_close_time(MARKET_CHINA, SESSION_DAY) == _dt.time(15, 0)
    assert session_close_time(MARKET_HK, _dt.date(2026, 10, 23)) == _dt.time(16, 0)


def test_session_close_time_shrinks_on_an_early_close_day():
    assert session_close_time(MARKET_US, US_EARLY) == _dt.time(13, 0)
    assert session_close_time(MARKET_HK, HK_EARLY) == _dt.time(12, 0)
    # China A-shares have no scheduled half-day at all: a shortened trading day there is a
    # FULL closure (Spring Festival / 调休), which the holiday table already holds.
    for year in range(mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR + 1):
        assert mh.early_closes_for(MARKET_CHINA, year) == {}, (
            f"china {year}: A-shares publish no half-day sessions; a short day is a closure"
        )
    assert session_close_time(MARKET_CHINA, _dt.date(2026, 2, 13)) == _dt.time(15, 0)
    # ...and the market spec's day-level accessor agrees with the free function
    # (one calendar, one answer -- no second opinion to drift).
    assert market_spec(MARKET_US).close_time_on(US_EARLY) == _dt.time(13, 0)
    assert market_spec(MARKET_US).early_close_on(US_EARLY) == _dt.time(13, 0)
    assert market_spec(MARKET_HK).close_time_on(HK_EARLY) == _dt.time(12, 0)
    for market in (MARKET_CHINA, MARKET_HK, MARKET_US):
        spec = market_spec(market)
        assert spec.early_close_on(US_NORMAL) is None, "a normal day has no early close"
        assert spec.close_time_on(US_NORMAL) == spec.close_time


def test_last_expected_bar_ts_uses_the_early_close():
    """The deadline the `L2.26` gate actually compares against, on a 13:00 / 12:00 day."""
    # US 13:00 early close: 12:55 for 5m bars, i.e. the final bar covers 12:55-13:00.
    assert last_expected_bar_ts(MARKET_US, US_EARLY, FIVE_MIN) == _dt.datetime(
        2026, 11, 27, 12, 55
    )
    # The tolerance is still the BAR LENGTH, not a constant (1h bar -> 12:00).
    assert last_expected_bar_ts(MARKET_US, US_EARLY, HOUR) == _dt.datetime(2026, 11, 27, 12, 0)
    # HK half-day: morning-only, so the last 5m bar opens 11:55.
    assert last_expected_bar_ts(MARKET_HK, HK_EARLY, FIVE_MIN) == _dt.datetime(2026, 12, 24, 11, 55)
    # Untouched either side of the early close (the classic off-by-one-day regression).
    assert last_expected_bar_ts(MARKET_US, US_NORMAL, FIVE_MIN) == _dt.datetime(2026, 11, 25, 15, 55)
    assert last_expected_bar_ts(MARKET_US, _dt.date(2026, 11, 30), FIVE_MIN) == _dt.datetime(
        2026, 11, 30, 15, 55
    )
    # Still naive market-local, on the session day itself.
    deadline = last_expected_bar_ts(MARKET_US, US_EARLY, FIVE_MIN)
    assert deadline.tzinfo is None and deadline.date() == US_EARLY


def test_early_close_does_not_leak_to_adjacent_days_or_the_weekend():
    """Weekend / adjacent-session interaction: only the listed date is shortened."""
    spec = market_spec(MARKET_US)
    for day in (
        _dt.date(2026, 11, 26),   # Thanksgiving itself: a full CLOSURE, not a half-day
        _dt.date(2026, 11, 28),   # Saturday
        _dt.date(2026, 11, 29),   # Sunday
        _dt.date(2026, 11, 30),   # Monday, normal
    ):
        assert spec.close_time_on(day) == _dt.time(16, 0), f"{day} must stay a full session"
    # An early-close date is still a TRADING day (which is exactly why it lives in its own
    # table rather than inside `holidays_for`).
    assert clock(MARKET_US, 2026, 11, 27, 19, 0).is_trading_day(US_EARLY) is True
    assert clock(MARKET_US, 2026, 11, 26, 19, 0).is_trading_day(_dt.date(2026, 11, 26)) is False


def test_early_close_lookup_is_tri_state_and_returns_a_copy():
    """UNKNOWN stays distinguishable from "no early closes", per the module contract."""
    assert mh.early_closes_for_or_none(MARKET_US, 2030) is None
    with pytest.raises(mh.HolidayCalendarCoverageError):
        mh.early_closes_for(MARKET_US, 2030)
    with pytest.raises(mh.HolidayCalendarCoverageError):
        mh.early_close_time(MARKET_US, _dt.date(2030, 1, 2))
    # Covered, and genuinely no half-days -> `{}`, never `None`.
    assert mh.early_closes_for_or_none(MARKET_CHINA, 2026) == {}
    # The ONE documented collapse: the geometry accessor reads UNKNOWN as "full session",
    # because it feeds a boolean coverage predicate that must not start raising (a raise
    # would make `fetch_universe` drop the asset). Safe direction: a short session in an
    # unknown year is judged SHORT (re-fetch), never fresh.
    assert mh.early_close_time_or_none(MARKET_US, _dt.date(2030, 1, 2)) is None
    assert session_close_time(MARKET_US, _dt.date(2030, 1, 2)) == _dt.time(16, 0)
    # Returned maps are copies -- mutating one must not corrupt the process-wide table.
    leaked = mh.early_closes_for(MARKET_US, 2026)
    leaked[US_EARLY] = _dt.time(11, 0)
    assert mh.early_close_time(MARKET_US, US_EARLY) == _dt.time(13, 0)
    # A `datetime` is a `date` subclass that never equals a `date`: reject it instead of
    # silently answering "full session" (same trap as `MarketClock.holidays` validation).
    with pytest.raises(TypeError, match="plain datetime.date"):
        session_close_time(MARKET_US, _dt.datetime(2026, 11, 27, 12, 0))


def test_early_close_tables_are_trading_weekdays_inside_the_covered_years():
    """Data invariant: a half-day on a closure/weekend would be silently inert."""
    for market in (MARKET_CHINA, MARKET_HK, MARKET_US):
        first, last = coverage_years(market)
        assert (first, last) == (mh.COVERAGE_START_YEAR, mh.COVERAGE_END_YEAR)
        spec = market_spec(market)
        for year in range(first, last + 1):
            table = mh.early_closes_for(market, year)   # raises if a year were missing
            closures = mh.holidays_for(market, year)
            for day, close in sorted(table.items()):
                assert day.year == year, f"{market}: {day} filed under year {year}"
                assert day.weekday() < 5, f"{market} {day}: early close on a weekend"
                assert day not in closures, f"{market} {day}: is a full closure, not a half-day"
                assert spec.morning_open < close < spec.close_time, (
                    f"{market} {day}: early close {close} must be strictly inside "
                    f"({spec.morning_open}, {spec.close_time})"
                )


def test_pinned_early_close_dates():
    """Pin the transcribed data (a silent edit here changes live re-fetch behaviour)."""
    assert mh.early_closes_for(MARKET_US, 2026) == {
        _dt.date(2026, 11, 27): _dt.time(13, 0),
        _dt.date(2026, 12, 24): _dt.time(13, 0),
    }
    # 2021-12-24 is the *observed Christmas closure*, not a half-day (a real trap: the
    # 13:00 rule applies to Christmas Eve as a session, and that year it was not one).
    assert mh.early_closes_for(MARKET_US, 2021) == {_dt.date(2021, 11, 26): _dt.time(13, 0)}
    assert _dt.date(2021, 12, 24) in mh.holidays_for(MARKET_US, 2021)
    # Same trap for July 3: 2020-07-03 is the observed Independence Day closure.
    assert _dt.date(2020, 7, 3) in mh.holidays_for(MARKET_US, 2020)
    assert mh.early_closes_for(MARKET_US, 2020) == {
        _dt.date(2020, 11, 27): _dt.time(13, 0),
        _dt.date(2020, 12, 24): _dt.time(13, 0),
    }
    assert mh.early_closes_for(MARKET_HK, 2026) == {
        _dt.date(2026, 2, 16): _dt.time(12, 0),    # Lunar New Year's Eve
        _dt.date(2026, 12, 24): _dt.time(12, 0),
        _dt.date(2026, 12, 31): _dt.time(12, 0),
    }
    # 2023 has no half-day session at all (all three eves fall on a weekend).
    assert mh.early_closes_for(MARKET_HK, 2023) == {}
    assert mh.table_revision().endswith(".2"), "data edit without a TABLE_REVISION bump?"


def test_l2_1_session_state_and_as_of_are_deliberately_not_shortened():
    """DISCLOSED scope boundary (L2.27): only the session GEOMETRY shrinks, not L2.1.

    `session_state` / `as_of_trading_date` keep the full-session rules, i.e. a 13:00 day is
    still reported OPEN at 14:30 and as-of holds the previous session until close+grace.
    That is *late*, never optimistic, and it is what keeps the fix safe: the shortened
    deadline is only consulted from 16:15 onward, when the real close has long passed.
    """
    midday = clock(MARKET_US, 2026, 11, 27, 19, 30)   # 14:30 ET, after the 13:00 close
    assert midday.session_state() is SessionState.OPEN
    assert midday.as_of_trading_date() == US_NORMAL    # 11-26 is Thanksgiving

    after_grace = clock(MARKET_US, 2026, 11, 27, 21, 20)   # 16:20 ET
    assert after_grace.session_state() is SessionState.CLOSED
    assert after_grace.as_of_trading_date() == US_EARLY
    # ...and from that moment the freshness gate measures the SHORTENED deadline.
    assert last_expected_bar_ts(
        MARKET_US, after_grace.as_of_trading_date(), FIVE_MIN
    ) == _dt.datetime(2026, 11, 27, 12, 55)

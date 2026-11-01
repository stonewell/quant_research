"""Offline guards for the two *silent* BaoStock under-return defects (M3.10).

WHY (the two incidents this file pins, not re-derived from the plan):

1. **Dead session.** After a long `keep_logged_in=True` run, *every* query answered
   `error_code="0"` with **zero rows** -- no exception, no warning, no transport failure.
   During the 2026-10 5m calibration that silently cost three symbols until a second probe
   pass noticed. An `error_code="0"` + 0-row answer is therefore no longer "this symbol has
   no bars": the provider asks a reference series (`000001.SH` daily, which exists for every
   historical session) and, if that is empty too, relogins **exactly once** and re-runs the
   request; a second empty answer raises instead of returning/caching an empty frame.

2. **Page-boundary truncation.** `002371.SZ` over `2026-04-08..2026-10-03` came back cut at
   *exactly 4000 rows*. Re-querying the same range returned the complete series, so the
   answer was a plausible-looking short page -- every `rows > 0` style check stayed green.
   The provider now asserts `returned_rows == trading_days * 48` per shard (from the
   exchange calendar, never from the returned frame) and refetches each short trading day
   on its own.

Both guards are CALENDAR-GATED (`query_trade_dates`): without a verifiable calendar the
provider keeps its historical behaviour byte-identically, which is also why every existing
BaoStock double in the suite (which has no calendar) is unaffected.

Everything here is 100% offline: `baostock` is replaced by a fake package in `sys.modules`.
No login, no socket, no network, no real market data.
"""

from __future__ import annotations

import datetime
import sys
import types
import warnings
from typing import Dict, List, Optional

import pandas as pd
import pytest

import common.financial_api as fin_api
from common.financial_api import BaoStockDataProvider, DataProviderSessionError

#: Intraday field list `_fetch_ohlcv_internal` requests for a stock (`fields` at :2622).
INTRADAY_FIELDS = [
    "date", "time", "code", "open", "high", "low", "close",
    "volume", "amount", "adjustflag",
]
#: The reference-liveness probe's minimal daily field list.
PROBE_FIELDS = ["date", "close"]
#: The suspension classifier's daily field list.
DAILY_PROFILE_FIELDS = ["date", "open", "high", "low", "close", "volume"]


@pytest.fixture(autouse=True)
def _isolate_session_state(monkeypatch):
    """The relogin budget is process-wide by design; isolate it per test."""
    monkeypatch.setitem(fin_api._BAOSTOCK_RELOGIN_STATE, "count", 0)
    monkeypatch.delenv("BAOSTOCK_MAX_RELOGINS", raising=False)


# ---------------------------------------------------------------------------
# Fake `baostock` package (ResultData page semantics included)
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for `baostock.data.resultset.ResultData`.

    Mirrors the real paging contract: `data` holds the *current* page, `cur_row_num` is a
    cursor the provider must advance itself, and `next()` swaps in the following page. An
    empty `rows` list is the dead-session / no-bars shape (`error_code="0"`, zero rows).
    """

    def __init__(
        self,
        rows: Optional[List[list]] = None,
        fields: Optional[List[str]] = None,
        *,
        error_code: str = "0",
        error_msg: str = "",
        page_size: Optional[int] = None,
    ):
        self.fields = list(fields or [])
        self._rows = [list(r) for r in (rows or [])]
        self._page_size = page_size
        self._pos = 0
        self.error_code = error_code
        self.error_msg = error_msg
        self.data = self._take()
        self.cur_row_num = 0

    def _take(self) -> List[list]:
        if self._page_size is None:
            page = self._rows[self._pos:]
            self._pos = len(self._rows)
            return [list(r) for r in page]
        page = self._rows[self._pos:self._pos + self._page_size]
        self._pos += len(page)
        return [list(r) for r in page]

    def next(self) -> bool:
        if self._page_size is None or self._pos >= len(self._rows):
            return False
        self.data = self._take()
        return True


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def settimeout(self, _value):
        pass

    def close(self):
        self.closed = True


def _install_baostock(monkeypatch, *, kline, calendar=None):
    """Install a fake `baostock` package. Returns the ledger of calls made."""
    ledger: Dict[str, object] = {
        "login": 0, "logout": 0, "kline": [], "calendar": [], "sockets": [],
    }
    context = types.SimpleNamespace(default_socket=None)

    def _login():
        ledger["login"] += 1
        sock = _FakeSocket()
        ledger["sockets"].append(sock)
        context.default_socket = sock
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _logout():
        ledger["logout"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _kline_query(*args, **kwargs):
        ledger["kline"].append({"args": args, "kwargs": kwargs})
        return kline(args, kwargs)

    namespace = {
        "login": _login,
        "logout": _logout,
        "query_history_k_data_plus": _kline_query,
        "common": types.SimpleNamespace(context=context),
    }
    if calendar is not None:
        def _trade_dates(**kwargs):
            ledger["calendar"].append(dict(kwargs))
            return calendar(kwargs)

        namespace["query_trade_dates"] = _trade_dates

    fake_bs = types.SimpleNamespace(**namespace)
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    monkeypatch.setitem(sys.modules, "baostock.common", fake_bs.common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", context)
    return ledger


def _calendar_rows(kwargs) -> _FakeResult:
    """Mon-Fri are trading days; the fake has no holiday calendar (T+0 offline universe)."""
    start = pd.Timestamp(kwargs["start_date"])
    end = pd.Timestamp(kwargs["end_date"])
    rows = [
        [day.strftime("%Y-%m-%d"), "1" if day.weekday() < 5 else "0"]
        for day in pd.date_range(start, end, freq="D")
    ]
    return _FakeResult(rows, ["calendar_date", "is_trading_day"])


# ---------------------------------------------------------------------------
# Synthetic 5m bar factory (48 bars: 09:35..11:30 + 13:05..15:00)
# ---------------------------------------------------------------------------


def _weekdays(start: str, end: str) -> List[datetime.date]:
    return [ts.date() for ts in pd.date_range(start, end, freq="B")]


def _session_times(day: datetime.date) -> List[str]:
    """48 5m bar labels for one A-share session, in BaoStock's 17-digit time format."""
    morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min")
    return [ts.strftime("%Y%m%d%H%M%S000") for ts in list(morning) + list(afternoon)]


def _day_rows(day: datetime.date, code: str, adjustflag: str = "3") -> List[list]:
    rows = []
    for i, stamp in enumerate(_session_times(day)):
        open_ = 100.0 + i * 0.01
        close = open_ + 0.005
        high = close + 0.02
        low = open_ - 0.02
        rows.append([
            day.isoformat(), stamp, code,
            f"{open_:.4f}", f"{high:.4f}", f"{low:.4f}", f"{close:.4f}",
            "1000", f"{1000 * close:.2f}", adjustflag,
        ])
    return rows


def _intraday_rows(days: List[datetime.date], code: str) -> List[list]:
    rows: List[list] = []
    for day in days:
        rows.extend(_day_rows(day, code))
    return rows


def _daily_row(day: datetime.date, *, volume: str = "1000") -> list:
    return [
        day.isoformat(), "100.0", "101.0", "99.0", "100.5", volume,
    ]


def _kline_day(args, kwargs, days, code):
    """Answer a *single-session* query (`start_date == end_date`) with that day's bars."""
    start = kwargs.get("start_date")
    if start is None and len(args) > 2:
        start = args[2]
    day = datetime.date.fromisoformat(str(start))
    if kwargs.get("frequency") != "5":
        return _FakeResult([], INTRADAY_FIELDS)
    if day not in days:
        return _FakeResult([], INTRADAY_FIELDS)
    return _FakeResult(_day_rows(day, code), INTRADAY_FIELDS)


# ---------------------------------------------------------------------------
# 1. Dead session: one relogin, then the symbol is rescued
# ---------------------------------------------------------------------------


def test_dead_session_relogins_exactly_once_and_rescues_the_symbol(monkeypatch):
    """The 2026-10 defect: `error_code="0"` + 0 rows for *every* query, silently."""
    days = _weekdays("2020-01-06", "2020-01-10")
    assert len(days) == 5

    def kline(args, kwargs):
        # BEFORE the relogin every query -- target and reference alike -- is empty.
        if ledger["login"] < 2:
            return _FakeResult([], INTRADAY_FIELDS)
        if kwargs.get("code") == "sh.000001" or args[0] == "sh.000001":
            return _FakeResult([["2020-01-10", "3000.0"]], PROBE_FIELDS)
        if kwargs.get("start_date") != kwargs.get("end_date"):
            return _FakeResult(_intraday_rows(days, "sz.002371"), INTRADAY_FIELDS)
        return _kline_day(args, kwargs, days, "sz.002371")

    ledger = _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    assert ledger["login"] == 2, "a dead session must relogin exactly once"
    assert ledger["logout"] == 1, "the relogin path tears the old session down first"
    assert any("dead session" in str(w.message) for w in caught), (
        "the degradation must be visible as a warning, never swallowed"
    )

    assert len(df) == len(days) * 48
    counts = df.groupby(df.index.date).size()
    assert set(counts.tolist()) == {48}

    probe = provider.last_probe_result
    assert probe is not None
    assert probe["relogins"] == 1
    assert probe["last_result"] == "ok"
    assert probe["attempts"] >= 2, "the guard evaluated the empty answer and the retry"
    assert provider.probe_attempts["002371.SZ"] == probe["attempts"]


def test_dead_session_that_stays_empty_raises_instead_of_returning_empty(monkeypatch):
    """Second empty answer after the relogin => raise. Never hand back a legal-looking empty."""

    def kline(_args, _kwargs):
        return _FakeResult([], INTRADAY_FIELDS)

    ledger = _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with pytest.raises(DataProviderSessionError) as excinfo:
        provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    message = str(excinfo.value)
    assert "unusable BaoStock answer for 002371.SZ" in message
    assert "dead session" in message
    assert ledger["login"] == 2, "exactly one relogin, never a loop"
    assert provider.last_probe_result["last_result"] == "dead_session"


# ---------------------------------------------------------------------------
# 2. Page-boundary truncation: 4000 rows -> per-day refetch
# ---------------------------------------------------------------------------


def test_truncated_4000_row_shard_is_refetched_day_by_day(monkeypatch):
    """The 002371.SZ shape: a shard stops at exactly 4000 rows and looks successful."""
    days = _weekdays("2020-01-06", "2020-05-15")
    assert len(days) * 48 > 4000, "the fixture must be large enough to truncate at 4000"

    full_rows = _intraday_rows(days, "sz.002371")
    truncated_rows = full_rows[:4000]
    calls: List[Dict[str, object]] = []

    def kline(args, kwargs):
        calls.append({"code": args[0], "start": kwargs.get("start_date"),
                      "end": kwargs.get("end_date"), "frequency": kwargs.get("frequency")})
        if args[0] == "sh.000001" or kwargs.get("code") == "sh.000001":
            return _FakeResult([["2020-04-24", "3000.0"]], PROBE_FIELDS)
        if kwargs.get("start_date") != kwargs.get("end_date"):
            return _FakeResult(truncated_rows, INTRADAY_FIELDS)
        return _kline_day(args, kwargs, days, "sz.002371")

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv(
            "002371.SZ", days[0].isoformat(), days[-1].isoformat(), interval="5m"
        )

    assert any("under-return" in str(w.message) for w in caught)
    # Aggregate count is the assertion that a `rows > 0` check could never make.
    assert len(df) == len(days) * 48
    assert df.index.is_unique
    assert set(df.groupby(df.index.date).size().tolist()) == {48}

    single_day_calls = [c for c in calls if c["start"] == c["end"] and c["code"] == "sz.002371"]
    assert single_day_calls, "the short days must be refetched one trading day at a time"

    probe = provider.last_probe_result
    assert probe["truncated"] is True
    assert probe["truncated_chunks"] == 1
    assert probe["expected_bars"] == len(days) * 48
    assert probe["returned_bars"] == len(days) * 48
    assert probe["refetched_days"] == len(single_day_calls)
    assert probe["repaired_days"] == probe["refetched_days"]
    assert probe["missing_days"] == []
    assert probe["last_result"] == "repaired"


# ---------------------------------------------------------------------------
# 3. False-positive guards (the layer must not relogin on legitimate empties)
# ---------------------------------------------------------------------------


def test_alive_session_with_a_genuinely_empty_symbol_does_not_relogin(monkeypatch):
    """A pre-listing / delisted range is not a dead session: the reference series answers."""

    def kline(args, kwargs):
        if args[0] == "sh.000001" or kwargs.get("code") == "sh.000001":
            return _FakeResult([["2020-01-10", "3000.0"]], PROBE_FIELDS)
        return _FakeResult([], INTRADAY_FIELDS)

    ledger = _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert ledger["login"] == 1, "a live session must not burn the relogin budget"
    assert provider.last_probe_result["last_result"] == "empty_verified_alive"
    assert any("genuine empty range" in str(w.message) for w in caught)


def test_without_a_calendar_the_guard_stays_inert(monkeypatch):
    """No `query_trade_dates` => we cannot claim a day "should" exist => legacy behaviour."""

    def kline(_args, _kwargs):
        return _FakeResult([], INTRADAY_FIELDS)

    ledger = _install_baostock(monkeypatch, kline=kline, calendar=None)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    df = provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    assert df.empty
    assert ledger["login"] == 1
    assert ledger["calendar"] == []
    assert provider.last_probe_result["last_result"] == "unknown_no_calendar"


def test_probe_window_without_a_closed_session_is_unknown_not_dead(monkeypatch):
    """Review 2026-11-01 (#2): a 0-row probe over a closure window proves nothing.

    Closed sessions exist in the requested range, but ALL of them sit more than 10 calendar
    days before `end_dt`, so the liveness probe's own window (`[end_dt-10d, end_dt]`)
    contains no session at all and a *healthy* session is guaranteed to answer 0 rows there.
    Treating that as "dead" would relogin and -- one relogin later -- raise on a perfectly
    good session, burning the process-wide `BAOSTOCK_MAX_RELOGINS` budget for the run.
    """
    closure_start = datetime.date(2020, 1, 21)
    closure_end = datetime.date(2020, 1, 31)  # `end_dt` sits inside the closure

    def calendar(kwargs):
        start = pd.Timestamp(kwargs["start_date"])
        end = pd.Timestamp(kwargs["end_date"])
        rows = []
        for stamp in pd.date_range(start, end, freq="D"):
            day = stamp.date()
            trading = day.weekday() < 5 and not (closure_start <= day <= closure_end)
            rows.append([day.isoformat(), "1" if trading else "0"])
        return _FakeResult(rows, ["calendar_date", "is_trading_day"])

    probed: List[str] = []

    def kline(args, _kwargs):
        probed.append(args[0])
        return _FakeResult([], INTRADAY_FIELDS)  # healthy session, no bars in this window

    ledger = _install_baostock(monkeypatch, kline=kline, calendar=calendar)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv(
            "002371.SZ", "2020-01-06", closure_end.isoformat(), interval="5m"
        )

    assert df.empty
    assert ledger["login"] == 1, "an inconclusive probe must not relogin"
    assert ledger["logout"] == 0
    assert "sh.000001" not in probed, "the reference query is skipped, not merely ignored"
    assert not any("dead session" in str(w.message) for w in caught)

    probe = provider.last_probe_result
    assert probe["last_result"] == "unknown_no_probe_window"
    assert probe["relogins"] == 0


# ---------------------------------------------------------------------------
# 4. Per-trading-day completeness: 停牌 vs 缺失 vs unverifiable
# ---------------------------------------------------------------------------


def test_suspended_session_is_not_reported_as_missing(monkeypatch):
    """A 停牌 day has no 5m bars by definition: daily `volume == 0` classifies it."""
    days = _weekdays("2020-01-06", "2020-01-10")
    suspended = days[2]

    def kline(args, kwargs):
        code = args[0]
        if code == "sh.000001":
            return _FakeResult([["2020-01-10", "3000.0"]], PROBE_FIELDS)
        if kwargs.get("frequency") == "d":
            return _FakeResult(
                [_daily_row(day, volume="0") for day in days], DAILY_PROFILE_FIELDS
            )
        if kwargs.get("start_date") != kwargs.get("end_date"):
            rows = []
            for day in days:
                if day != suspended:
                    rows.extend(_day_rows(day, code))
            return _FakeResult(rows, INTRADAY_FIELDS)
        return _kline_day(args, kwargs, [d for d in days if d != suspended], code)

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv(
            "002371.SZ", days[0].isoformat(), days[-1].isoformat(), interval="5m"
        )

    assert len(df) == (len(days) - 1) * 48, "the suspended day contributes no 5m bars"
    assert not any("no suspension evidence" in str(w.message) for w in caught)

    probe = provider.last_probe_result
    assert probe["suspended_days"] == 1
    assert probe["missing_days"] == []
    assert probe["last_result"] == "suspended"


def test_day_missing_with_a_normal_daily_bar_is_reported_as_missing(monkeypatch):
    """A short day whose daily bar is normal (`volume > 0`, price moves) is a real gap."""
    days = _weekdays("2020-01-06", "2020-01-10")
    gap = days[1]

    def kline(args, kwargs):
        code = args[0]
        if code == "sh.000001":
            return _FakeResult([["2020-01-10", "3000.0"]], PROBE_FIELDS)
        if kwargs.get("frequency") == "d":
            return _FakeResult([_daily_row(day) for day in days], DAILY_PROFILE_FIELDS)
        if kwargs.get("start_date") != kwargs.get("end_date"):
            rows = []
            for day in days:
                if day != gap:
                    rows.extend(_day_rows(day, code))
            return _FakeResult(rows, INTRADAY_FIELDS)
        return _kline_day(args, kwargs, [], code)

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv(
            "002371.SZ", days[0].isoformat(), days[-1].isoformat(), interval="5m"
        )

    assert len(df) == (len(days) - 1) * 48
    assert any("no suspension evidence" in str(w.message) for w in caught)

    probe = provider.last_probe_result
    assert probe["missing_days"] == [gap.isoformat()]
    assert probe["suspended_days"] == 0
    assert probe["last_result"] == "missing"


def test_suspended_day_without_any_daily_row_is_unverified_not_missing(monkeypatch):
    """Review 2026-11-01 (#3): BaoStock omits the whole daily row for some suspensions.

    A missing daily row is *not* evidence of a download gap: the day must land in its own
    `unverified_days` bucket (disclosed by a distinct warning) instead of inflating
    `missing_days`, which would misdirect diagnosis towards the wrong root cause.
    """
    days = _weekdays("2020-01-06", "2020-01-10")
    suspended = days[2]

    def kline(args, kwargs):
        code = args[0]
        if code == "sh.000001":
            return _FakeResult([["2020-01-10", "3000.0"]], PROBE_FIELDS)
        if kwargs.get("frequency") == "d":
            # The daily series has NO row at all for the suspended session.
            return _FakeResult(
                [_daily_row(day) for day in days if day != suspended],
                DAILY_PROFILE_FIELDS,
            )
        if kwargs.get("start_date") != kwargs.get("end_date"):
            rows = []
            for day in days:
                if day != suspended:
                    rows.extend(_day_rows(day, code))
            return _FakeResult(rows, INTRADAY_FIELDS)
        return _kline_day(args, kwargs, [], code)

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv(
            "002371.SZ", days[0].isoformat(), days[-1].isoformat(), interval="5m"
        )

    assert len(df) == (len(days) - 1) * 48, "no raise, no data loss"
    assert any("unverifiable from the daily series" in str(w.message) for w in caught)
    assert not any("no suspension evidence" in str(w.message) for w in caught)

    probe = provider.last_probe_result
    assert probe["unverified_days"] == [suspended.isoformat()]
    assert probe["missing_days"] == []
    assert probe["suspended_days"] == 0
    assert probe["last_result"] == "unverified_no_daily_row"


# ---------------------------------------------------------------------------
# 5. Healthy path is untouched (byte-identical contract)
# ---------------------------------------------------------------------------


def test_healthy_intraday_path_is_unchanged_and_scores_ok(monkeypatch):
    days = _weekdays("2020-01-06", "2020-01-10")
    calls: List[Dict[str, object]] = []

    def kline(args, kwargs):
        calls.append(dict(kwargs))
        return _FakeResult(_intraday_rows(days, "sz.002371"), INTRADAY_FIELDS)

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    # No extra *history* query beyond the single shard: the guard is a no-op on healthy
    # data (`query_trade_dates` is the calendar lookup, tracked separately in `ledger`).
    assert len(calls) == 1
    assert [w for w in caught if "under-return" in str(w.message)] == []
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume", "Amount"]
    assert len(df) == len(days) * 48
    assert df["Open"].dtype == "float64"
    assert pd.api.types.is_numeric_dtype(df["Volume"])
    assert df.attrs.get("adjust_basis") in (None, "raw")

    probe = provider.last_probe_result
    # Pinned: exactly two evaluations (the per-shard assertion + the range-level
    # completeness check). Neither 2026-11-01 review fix may add a third for good data.
    assert probe["attempts"] == 2
    assert probe["truncated"] is False
    assert probe["refetched_days"] == 0
    assert probe["expected_bars"] == len(days) * 48
    assert probe["returned_bars"] == len(days) * 48
    assert probe["missing_days"] == []
    assert probe["unverified_days"] == []
    assert probe["last_result"] == "ok"


def test_probe_observer_receives_every_verdict(monkeypatch):
    """Visibility channel: the documented `probe_attempts`/`last_probe_result` facts."""
    seen: List[tuple] = []

    def kline(_args, _kwargs):
        return _FakeResult([], INTRADAY_FIELDS)

    _install_baostock(monkeypatch, kline=kline, calendar=None)
    provider = BaoStockDataProvider(
        adjustflag="3", probe_observer=lambda symbol, result: seen.append((symbol, result))
    )

    provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    assert len(seen) == 1
    symbol, result = seen[0]
    assert symbol == "002371.SZ"
    assert result["last_result"] == "unknown_no_calendar"
    assert result["attempts"] >= 1


def test_probe_observer_failure_never_breaks_the_download(monkeypatch):
    days = _weekdays("2020-01-06", "2020-01-10")

    def kline(_args, _kwargs):
        return _FakeResult(_intraday_rows(days, "sz.002371"), INTRADAY_FIELDS)

    def boom(_symbol, _result):
        raise RuntimeError("telemetry sink exploded")

    _install_baostock(monkeypatch, kline=kline, calendar=_calendar_rows)
    provider = BaoStockDataProvider(adjustflag="3", probe_observer=boom)

    df = provider.fetch_ohlcv("002371.SZ", "2020-01-06", "2020-01-10", interval="5m")

    assert len(df) == len(days) * 48

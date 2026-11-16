"""`10001001` on a BaoStock stream is undecidable -- heal the chunk, never the request.

WHY (2026-11-15 Stage-2 incident, the facts this file pins):

`baostock/util/socketutil.py:71-77` `send_msg` is `while True: recv(8192)` with every
exception swallowed and **no response framing, sequence number or CRC check** (the CRC it
sends is over the *request*, `baostock/security/history.py:102-104`). A truncated response
therefore leaves the stream desynchronised, and the next `ResultData.next` can decode the
PREVIOUS message's payload -- including a stale `10001001 用户未登录`. The library cannot
tell "the server rejected my session" apart from "my stream is off by one message", and
neither can we.

Two consequences, both tested here:

1. **The heal must re-issue the failed CHUNK, not the request.** The incident's evidence
   leans desync (selective chunk success with exact row counts -- 5616/5616, 4752/4752,
   5808/5808 -- on the same socket). Re-running every chunk roughly doubled the cost for
   any symbol that lost its session mid-flight, and the retry incremented nothing
   observable, so the operator saw `relogins=0` while the socket was provably rebuilt.

2. **`10001001` is only session-suspect when the same socket already answered.** On the
   very first chunk there is no evidence that the socket works, so "not logged in" stays a
   plain business error and the relogin budget is not touched (that half must keep working
   exactly as before -- a business error must never cost a login round-trip).

3. A `10001001` chunk never has its days downgraded to `suspended`: the suspension bucket
   requires positive daily-bar evidence (`volume == 0` / frozen O=H=L=C), which a failed
   chunk cannot supply.

Everything is 100% offline: `baostock` is replaced by a fake package in `sys.modules`. No
login, no socket, no network, no real market data. The fake mirrors the harnesses in
`common/tests/test_baostock_silent_underreturn.py` (`_FakeResult`, `_FakeSocket`,
`_install_baostock`, `_calendar_rows`) and `pipeline/tests/test_baostock_relogin.py`
(`_install_fake_baostock`), duplicated here because `common/tests` has no `__init__.py` and
this module must be runnable on its own.
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
from common.financial_api import (
    BaoStockDataProvider,
    DataProviderSessionError,
    _is_session_auth_failure,
)

AS_NOLOGIN = "10001001"
AS_NOLOGIN_MSG = "用户未登录"

#: Intraday field list `_fetch_ohlcv_internal` requests for a stock.
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
    """Both counters are process-wide by design; isolate them per test."""
    monkeypatch.setitem(fin_api._BAOSTOCK_RELOGIN_STATE, "count", 0)
    monkeypatch.setitem(fin_api._BAOSTOCK_LOGIN_STATE, "logins", 0)
    monkeypatch.delenv("BAOSTOCK_MAX_RELOGINS", raising=False)
    monkeypatch.delenv("BAOSTOCK_MAX_REPAIR_DAYS", raising=False)
    monkeypatch.delenv("BAOSTOCK_ALLOW_UNVERIFIED_INTRADAY", raising=False)


# ---------------------------------------------------------------------------
# Fake `baostock` package (same contract as the two existing harnesses)
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for `baostock.data.resultset.ResultData` (page-by-page extraction)."""

    def __init__(
        self,
        rows: Optional[List[list]] = None,
        fields: Optional[List[str]] = None,
        *,
        error_code: str = "0",
        error_msg: str = "",
    ):
        self.fields = list(fields or [])
        self.data = [list(r) for r in (rows or [])]
        self.cur_row_num = 0
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:  # single page -> nothing left to fetch
        return False


class _FakeSocket:
    def __init__(self):
        self.timeouts: List[float] = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


def _install_baostock(monkeypatch, *, kline):
    """Install a fake `baostock` package and return its call ledger."""
    ledger: Dict[str, object] = {"login": 0, "logout": 0, "kline": [], "sockets": []}
    context = types.SimpleNamespace(default_socket=None)

    def _login():
        ledger["login"] += 1
        # Parity with `socketutil.SocketUtil.connect`: every login publishes a NEW socket.
        sock = _FakeSocket()
        ledger["sockets"].append(sock)
        context.default_socket = sock
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _logout():
        ledger["logout"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _kline_query(*args, **kwargs):
        ledger["kline"].append(
            {
                "code": args[0] if args else kwargs.get("code"),
                "start": kwargs.get("start_date"),
                "end": kwargs.get("end_date"),
                "frequency": kwargs.get("frequency"),
            }
        )
        return kline(args, kwargs)

    def _trade_dates(**kwargs):
        start = pd.Timestamp(kwargs["start_date"])
        end = pd.Timestamp(kwargs["end_date"])
        rows = [
            [day.strftime("%Y-%m-%d"), "1" if day.weekday() < 5 else "0"]
            for day in pd.date_range(start, end, freq="D")
        ]
        return _FakeResult(rows, ["calendar_date", "is_trading_day"])

    fake_bs = types.SimpleNamespace(
        login=_login,
        logout=_logout,
        query_history_k_data_plus=_kline_query,
        query_trade_dates=_trade_dates,
        common=types.SimpleNamespace(context=context),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    monkeypatch.setitem(sys.modules, "baostock.common", fake_bs.common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", context)
    return ledger


# ---------------------------------------------------------------------------
# Synthetic 5m bars (48 per closed session) + daily profile rows
# ---------------------------------------------------------------------------


def _weekdays(start: str, end: str) -> List[datetime.date]:
    return [ts.date() for ts in pd.date_range(start, end, freq="B")]


def _session_times(day: datetime.date) -> List[str]:
    morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min")
    return [ts.strftime("%Y%m%d%H%M%S000") for ts in list(morning) + list(afternoon)]


def _day_rows(day: datetime.date, code: str) -> List[list]:
    rows = []
    for i, stamp in enumerate(_session_times(day)):
        open_ = 100.0 + i * 0.01
        close = open_ + 0.005
        rows.append([
            day.isoformat(), stamp, code,
            f"{open_:.4f}", f"{close + 0.02:.4f}", f"{open_ - 0.02:.4f}", f"{close:.4f}",
            "1000", f"{1000 * close:.2f}", "3",
        ])
    return rows


def _daily_row(day: datetime.date) -> list:
    return [day.isoformat(), "100.0", "101.0", "99.0", "100.5", "1000"]


def _bars(days: List[datetime.date], code: str) -> List[list]:
    rows: List[list] = []
    for day in days:
        rows.extend(_day_rows(day, code))
    return rows


#: A request long enough to be split into TWO 179-day chunks by
#: `_fetch_ohlcv_internal`'s intraday sharding.
RANGE_START = "2020-01-06"
RANGE_END = "2020-07-10"
#: `curr + 179 days` from RANGE_START is the first chunk's end (2020-07-03).
CHUNK_2_START = "2020-07-04"


# ---------------------------------------------------------------------------
# 1. mid-symbol `10001001` on chunk 2 -> heal the CHUNK, keep the good frames
# ---------------------------------------------------------------------------


def test_mid_request_nologin_heals_only_the_failed_chunk(monkeypatch):
    days = _weekdays(RANGE_START, RANGE_END)
    assert len(days) > 100, "the fixture must span two 179-day shards"

    state = {"chunk2_calls": 0}

    def kline(args, kwargs):
        code = args[0]
        start = kwargs.get("start_date")
        end = kwargs.get("end_date")
        if kwargs.get("frequency") == "d":
            return _FakeResult([_daily_row(day) for day in days], DAILY_PROFILE_FIELDS)
        if start != end:
            # Bulk shard. Chunk 2 answers `10001001` on its FIRST attempt (the desync
            # signature: the same socket had just served chunk 1 with exact row counts).
            if start >= CHUNK_2_START:
                state["chunk2_calls"] += 1
                if state["chunk2_calls"] == 1:
                    return _FakeResult(
                        [], INTRADAY_FIELDS, error_code=AS_NOLOGIN, error_msg=AS_NOLOGIN_MSG
                    )
                wanted = [d for d in days if d.isoformat() >= start]
                return _FakeResult(_bars(wanted, code), INTRADAY_FIELDS)
            wanted = [d for d in days if d.isoformat() <= end]
            return _FakeResult(_bars(wanted, code), INTRADAY_FIELDS)
        day = datetime.date.fromisoformat(str(start))
        return _FakeResult(_day_rows(day, code), INTRADAY_FIELDS)

    ledger = _install_baostock(monkeypatch, kline=kline)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv("002371.SZ", RANGE_START, RANGE_END, interval="5m")

    # the final frame is complete: every expected session, 48 bars each
    assert df.index.is_unique
    assert len(df) == len(days) * 48
    assert set(df.groupby(df.index.date).size().tolist()) == {48}
    assert not any("repair budget exhausted" in str(w.message) for w in caught)

    # exactly one new socket was minted (initial login + the single heal)
    assert len(ledger["sockets"]) == 2
    assert ledger["login"] == 2
    assert ledger["logout"] == 1

    bulks = [c for c in ledger["kline"] if c["frequency"] == "5" and c["start"] != c["end"]]
    chunk_1 = [c for c in bulks if c["end"] < CHUNK_2_START]
    chunk_2 = [c for c in bulks if c["start"] >= CHUNK_2_START]
    assert len(chunk_1) == 1, f"chunk 1 must NOT be re-issued after the heal: {chunk_1}"
    assert len(chunk_2) == 2, f"chunk 2 must be re-issued exactly once: {chunk_2}"

    probe = provider.last_probe_result
    assert probe["relogins"] == 1
    # the truthful counter: the heal (and the entry login) DID establish sessions
    assert probe["sockets_built"] == 2
    assert provider.established_sessions == 2
    assert probe["missing_days"] == []
    assert probe["unverified_days"] == []
    assert probe["last_result"] == "ok"


# ---------------------------------------------------------------------------
# 2. `10001001` with no prior success = genuine business error: no heal at all
# ---------------------------------------------------------------------------


def test_nologin_on_the_first_chunk_never_heals_the_session(monkeypatch):
    def kline(_args, _kwargs):
        return _FakeResult(
            [], INTRADAY_FIELDS, error_code=AS_NOLOGIN, error_msg=AS_NOLOGIN_MSG
        )

    ledger = _install_baostock(monkeypatch, kline=kline)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(fin_api.DataProviderPayloadError) as excinfo:
            provider.fetch_ohlcv("002371.SZ", RANGE_START, RANGE_END, interval="5m")

    # a business error: unusable answer, but NOT a session failure
    assert not isinstance(excinfo.value, DataProviderSessionError)
    assert "unusable BaoStock answer for 002371.SZ" in str(excinfo.value)
    assert ledger["login"] == 1, "nothing to compare against -> never rebuild the socket"
    assert len(ledger["sockets"]) == 1
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 0
    assert fin_api._BAOSTOCK_LOGIN_STATE["logins"] == 1
    assert not any("BaoStock relogin" in str(w.message) for w in caught)


def test_session_auth_classifier_is_lexical_only():
    """The classifier is a signal, not a verdict -- the `dfs` gate lives in the fetch loop."""
    assert _is_session_auth_failure(AS_NOLOGIN, "") is True
    assert _is_session_auth_failure("0", AS_NOLOGIN_MSG) is True
    assert _is_session_auth_failure(None, "用户未登陆") is True
    assert _is_session_auth_failure("10004011", "无效的证券代码") is False
    assert _is_session_auth_failure("10002007", "") is False
    assert _is_session_auth_failure(None, None) is False


# ---------------------------------------------------------------------------
# 3. A `10001001` chunk is never reported as `suspended`
# ---------------------------------------------------------------------------


def test_nologin_chunk_days_are_unverified_never_suspended(monkeypatch):
    """The chunk stays failed after the heal; its sessions must land in `unverified_days`."""
    days = _weekdays(RANGE_START, RANGE_END)

    def kline(args, kwargs):
        code = args[0]
        start = kwargs.get("start_date")
        end = kwargs.get("end_date")
        if kwargs.get("frequency") == "d":
            # A perfectly normal daily series, i.e. lots of evidence AGAINST a suspension.
            return _FakeResult([_daily_row(day) for day in days], DAILY_PROFILE_FIELDS)
        if start != end:
            if start >= CHUNK_2_START:
                return _FakeResult(
                    [], INTRADAY_FIELDS, error_code=AS_NOLOGIN, error_msg=AS_NOLOGIN_MSG
                )
            wanted = [d for d in days if d.isoformat() <= end]
            return _FakeResult(_bars(wanted, code), INTRADAY_FIELDS)
        # per-day repair answers nothing for the lost shard
        day = datetime.date.fromisoformat(str(start))
        if day.isoformat() >= CHUNK_2_START:
            return _FakeResult([], INTRADAY_FIELDS)
        return _FakeResult(_day_rows(day, code), INTRADAY_FIELDS)

    ledger = _install_baostock(monkeypatch, kline=kline)
    provider = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    # The gate (change D) refuses the short frame by default; opt out so this test can
    # inspect the published classification.
    monkeypatch.setenv("BAOSTOCK_ALLOW_UNVERIFIED_INTRADAY", "1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = provider.fetch_ohlcv("002371.SZ", RANGE_START, RANGE_END, interval="5m")

    probe = provider.last_probe_result
    lost = [d for d in days if d.isoformat() >= CHUNK_2_START]
    assert probe["last_result"] == "transport_failure_incomplete"
    assert probe["incomplete"] is True
    assert sorted(probe["unverified_days"]) == sorted(d.isoformat() for d in lost)
    assert probe["suspended_days"] == 0, "a failed chunk is never downgraded to 停牌"
    assert probe["missing_days"] == []
    assert any("never answered" in str(w.message) for w in caught)

    # the opt-out discloses instead of hiding
    classification = df.attrs["intraday_completeness"]
    assert classification["incomplete"] is True
    assert sorted(classification["unverified_days"]) == sorted(d.isoformat() for d in lost)

    # and the default (no opt-out) really raises for the same shape
    monkeypatch.delenv("BAOSTOCK_ALLOW_UNVERIFIED_INTRADAY")
    provider_2 = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)
    with pytest.raises(DataProviderSessionError):
        provider_2.fetch_ohlcv("002371.SZ", RANGE_START, RANGE_END, interval="5m")
    assert ledger["login"] >= 2

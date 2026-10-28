"""Offline tests for BaoStock *transport* failure session self-healing (2026-10-27).

WHY (background, not duplicated from `test_financial_api_decode.py`): the Stage-1
incident ended in `returncode=-2` = SIGINT, not an uncaught `UnicodeDecodeError`.
The chain was:

    server answers with a GBK/GB18030 error page
      -> baostock `socketutil.send_msg` swallows the decode error with `print(ex)`
      -> `while True: recv; if receive[-13:] == b"<![CDATA[]]>\\n": break` never
         sees the terminator again -> permanent block -> SIGINT

`_apply_socket_timeout` (60s) only converts the hang into an observable failure; it
cannot repair the socket, and `keep_logged_in=True` never calls `login()` again. The
provider therefore keeps a "session dirty" flag and rebuilds the socket, bounded by a
PROCESS-WIDE `BAOSTOCK_MAX_RELOGINS` budget (an unbounded relogin loop is worse than a
hang: it hammers the server and never terminates either).

Everything below is offline: `baostock` is replaced by a fake package in `sys.modules`,
no login, no socket, no network.
"""

from __future__ import annotations

import logging
import socket
import sys
import types

import pytest

import common.financial_api as fin_api
from common.financial_api import (
    BaoStockDataProvider,
    DataProviderPayloadError,
    DataProviderSessionError,
    _is_transport_failure,
)

#: Daily-bar field list, exactly what `_fetch_ohlcv_internal` requests for a stock.
DAILY_FIELDS = [
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "pctChg", "isST",
]
DAILY_ROW = [
    "2026-10-23", "sh.601288", "3.00", "3.10", "2.90", "3.05", "3.02",
    "1000", "3000", "2", "0.5", "0.1", "0",
]

TRANSPORT_CODE = "10002007"  # BSERR_RECVSOCK_FAIL -- the incident's code
TRANSPORT_MSG = "网络接收错误。"


@pytest.fixture(autouse=True)
def _reset_process_budget(monkeypatch):
    """The relogin counter is process-wide by design; isolate it per test."""
    monkeypatch.setitem(fin_api._BAOSTOCK_RELOGIN_STATE, "count", 0)
    monkeypatch.delenv("BAOSTOCK_MAX_RELOGINS", raising=False)


def _provider(tmp_path, **kwargs) -> BaoStockDataProvider:
    # Explicit `cache_dir`: passing neither would fall back to the production
    # `data/` cache, which these tests must never touch.
    return BaoStockDataProvider(
        adjustflag="3", keep_logged_in=True, cache_dir=str(tmp_path / "sync.duckdb"), **kwargs
    )


class _FakeResult:
    """Stand-in for `baostock.data.resultset.ResultData` (page-by-page extraction)."""

    def __init__(self, rows=None, error_code: str = "0", error_msg: str = ""):
        self.fields = DAILY_FIELDS
        self.data = list(rows or [])
        self.cur_row_num = 0
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self):  # single page -> nothing left to fetch
        return False


class _FakeSocket:
    def __init__(self):
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True


def _install_fake_baostock(monkeypatch, query_fn):
    """Install a fake `baostock` package; `query_fn(n, args, kwargs)` answers query `n`."""
    ledger = {"login": 0, "logout": 0, "queries": 0, "sockets": []}
    context = types.SimpleNamespace(default_socket=None)

    def _login():
        ledger["login"] += 1
        # Parity with `socketutil.SocketUtil.connect`: every login publishes a NEW socket
        # on the module-level `context.default_socket`.
        sock = _FakeSocket()
        ledger["sockets"].append(sock)
        context.default_socket = sock
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _logout():
        ledger["logout"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def _query(*args, **kwargs):
        ledger["queries"] += 1
        return query_fn(ledger["queries"], args, kwargs)

    fake_bs = types.SimpleNamespace(
        login=_login,
        logout=_logout,
        query_history_k_data_plus=_query,
        common=types.SimpleNamespace(context=context),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    monkeypatch.setitem(sys.modules, "baostock.common", fake_bs.common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", context)
    return ledger, context


# ---------------------------------------------------------------------------
# 1. one timeout -> exactly one relogin, and the symbol still gets its data
# ---------------------------------------------------------------------------


def test_single_transport_failure_relogins_once_and_rescues_the_symbol(monkeypatch, tmp_path):
    def query(n, _args, _kwargs):
        if n == 1:
            raise socket.timeout("timed out")  # TimeoutError -> OSError, as on the wire
        return _FakeResult([DAILY_ROW])

    ledger, context = _install_fake_baostock(monkeypatch, query)
    provider = _provider(tmp_path)

    df = provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")

    assert not df.empty
    assert len(df) == 1
    # initial login + exactly one relogin; one logout for the teardown of the dirty socket
    assert ledger["login"] == 2
    assert ledger["logout"] == 1
    assert ledger["queries"] == 2  # the request itself was retried exactly once
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 1
    # the recv timeout is re-applied to the NEW socket (not the closed one)
    assert context.default_socket.timeouts == [pytest.approx(60.0)]
    assert context.default_socket.closed is False


# ---------------------------------------------------------------------------
# 2. continuous failure -> stops at the budget, never a (MAX+1)-th relogin
# ---------------------------------------------------------------------------


def test_repeated_transport_failures_stop_at_the_process_budget(monkeypatch, tmp_path):
    def query(_n, _args, _kwargs):
        return _FakeResult(error_code=TRANSPORT_CODE, error_msg=TRANSPORT_MSG)

    ledger, _context = _install_fake_baostock(monkeypatch, query)
    provider = _provider(tmp_path)
    max_relogins = provider._max_relogins
    assert max_relogins == fin_api._DEFAULT_MAX_RELOGINS == 3

    errors = []
    for symbol in ("601288.SH", "601398.SH", "600000.SH"):
        with pytest.raises(DataProviderSessionError) as excinfo:
            provider.fetch_raw_ohlcv(symbol, start="2026-10-20", end="2026-10-24")
        errors.append(excinfo.value)
        # a session error is still a *payload* error for existing callers
        assert isinstance(excinfo.value, DataProviderPayloadError)

    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == max_relogins
    assert ledger["login"] == 1 + max_relogins  # no (MAX+1)-th relogin
    assert ledger["logout"] == max_relogins

    # symbol 2 consumed the last unit of budget on its own request
    second = str(errors[1])
    assert "unusable BaoStock answer for 601398.SH" in second
    assert f"relogins={max_relogins}/{max_relogins}" in second
    assert "retry failed" in second
    assert TRANSPORT_CODE in second and TRANSPORT_MSG in second

    # symbol 3 fails fast: budget gone, message says so and still names symbol + code
    third = str(errors[2])
    assert "600000.SH" in third
    assert "budget exhausted" in third
    assert TRANSPORT_CODE in third


def test_request_is_never_retried_more_than_once_per_call(monkeypatch, tmp_path):
    def query(_n, _args, _kwargs):
        return _FakeResult(error_code=TRANSPORT_CODE, error_msg=TRANSPORT_MSG)

    ledger, _context = _install_fake_baostock(monkeypatch, query)
    provider = _provider(tmp_path)

    with pytest.raises(DataProviderSessionError):
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")

    # one attempt + one retry: the heal path cannot spin inside a single request
    assert ledger["queries"] == 2
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 1


# ---------------------------------------------------------------------------
# 3. healthy path stays a zero-relogin hot path
# ---------------------------------------------------------------------------


def test_healthy_path_never_relogins(monkeypatch, tmp_path):
    ledger, context = _install_fake_baostock(
        monkeypatch, lambda _n, _a, _k: _FakeResult([DAILY_ROW])
    )
    provider = _provider(tmp_path)

    first = provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")
    second = provider.fetch_raw_ohlcv("601398.SH", start="2026-10-20", end="2026-10-24")

    assert not first.empty and not second.empty
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 0
    assert ledger["login"] == 1  # keep_logged_in=True -> one login for the whole run
    assert ledger["logout"] == 0
    assert ledger["queries"] == 2
    assert context.default_socket.timeouts == [pytest.approx(60.0)]


# ---------------------------------------------------------------------------
# 4. business errors never relogin (they would only waste a login round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,msg",
    [
        ("10004006", "参数错误"),                      # BSERR_PARAM_ERR
        ("10004011", "无效的证券代码"),                 # BSERR_CODE_INVALIED
        ("10004009", "起始日期大于终止日期，请修改。"),   # BSERR_START_BIGTHAN_END
        ("-1", "no data for this symbol"),            # no transport wording
    ],
)
def test_business_error_never_relogins(monkeypatch, tmp_path, code, msg):
    ledger, _context = _install_fake_baostock(
        monkeypatch, lambda _n, _a, _k: _FakeResult(error_code=code, error_msg=msg)
    )
    provider = _provider(tmp_path)

    # Unchanged pre-existing contract: an unusable answer raises the payload error...
    with pytest.raises(DataProviderPayloadError) as excinfo:
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")

    # ...but it is NOT a session failure and nothing was rebuilt.
    assert not isinstance(excinfo.value, DataProviderSessionError)
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 0
    assert ledger["login"] == 1
    assert ledger["logout"] == 0


def test_valid_answer_with_zero_rows_is_not_a_failure(monkeypatch, tmp_path):
    ledger, _context = _install_fake_baostock(
        monkeypatch, lambda _n, _a, _k: _FakeResult([])
    )
    provider = _provider(tmp_path)

    df = provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")

    assert df.empty and list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 0
    assert ledger["login"] == 1


def test_refused_relogin_raises_instead_of_returning_an_empty_frame(monkeypatch, tmp_path):
    """A relogin that the server refuses must surface, not degrade to "no data"."""

    def query(_n, _args, _kwargs):
        return _FakeResult(error_code=TRANSPORT_CODE, error_msg=TRANSPORT_MSG)

    ledger, context = _install_fake_baostock(monkeypatch, query)
    # Only the very first login succeeds; the relogin hits BSERR_LOGIN_COUNT_LIMIT.
    real_login = sys.modules["baostock"].login
    attempts = {"n": 0}

    def _login_refused():
        attempts["n"] += 1
        if attempts["n"] > 1:
            return types.SimpleNamespace(error_code="10001005", error_msg="账号登陆数达到上限")
        return real_login()

    sys.modules["baostock"].login = _login_refused
    provider = _provider(tmp_path)

    with pytest.raises(DataProviderSessionError) as excinfo:
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")

    assert attempts["n"] == 2  # initial + the refused relogin, nothing more
    assert ledger["login"] == 1  # the refused attempt never established a session
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 1
    assert "601288.SH" in str(excinfo.value)
    assert TRANSPORT_CODE in str(excinfo.value)
    # the session stays marked dirty so the next request retries healing / fails fast
    assert provider._session_dirty is True
    # the poisoned socket was torn down and de-referenced (no half-open fd, no stale ref)
    assert ledger["sockets"][0].closed is True
    assert context.default_socket is None

    # a second request goes back through the BUDGETED heal path (not an unbudgeted login)
    with pytest.raises(DataProviderSessionError) as second:
        provider.fetch_raw_ohlcv("601398.SH", start="2026-10-20", end="2026-10-24")
    assert attempts["n"] == 3
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 2
    assert "relogin refused by the server" in str(second.value)


def test_transport_classifier_is_the_single_source_of_truth():
    # socket/connection family (baostock/common/contants.py:155-162)
    for code in ("10002001", "10002002", "10002003", "10002004",
                 "10002005", "10002006", "10002007", "10002008"):
        assert _is_transport_failure(code, "") is True

    # the swallowed-exception wording `send_msg` prints
    assert _is_transport_failure("10004006", "接收数据异常，请稍后再试。") is True
    assert _is_transport_failure("10004006", "网络接收错误。") is True
    assert _is_transport_failure(None, "Connection reset by peer") is True
    assert _is_transport_failure(None, "socket operation on non-socket") is True

    # business errors / empty answers are not transport failures
    assert _is_transport_failure("10004006", "参数错误") is False
    assert _is_transport_failure("10004011", "无效的证券代码") is False
    assert _is_transport_failure("0", "") is False
    assert _is_transport_failure(None, None) is False


# ---------------------------------------------------------------------------
# 5. the budget itself is configurable, and a bad value degrades to the default
# ---------------------------------------------------------------------------


def test_budget_env_var_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("BAOSTOCK_MAX_RELOGINS", "1")

    def query(_n, _args, _kwargs):
        return _FakeResult(error_code=TRANSPORT_CODE, error_msg=TRANSPORT_MSG)

    ledger, _context = _install_fake_baostock(monkeypatch, query)
    provider = _provider(tmp_path)
    assert provider._max_relogins == 1

    with pytest.raises(DataProviderSessionError) as excinfo:
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-20", end="2026-10-24")
    assert "relogins=1/1" in str(excinfo.value)
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 1
    assert ledger["login"] == 2

    # budget spent -> the next request fails fast instead of reloginning again
    with pytest.raises(DataProviderSessionError):
        provider.fetch_raw_ohlcv("601398.SH", start="2026-10-20", end="2026-10-24")
    assert fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 1
    assert ledger["login"] == 2


@pytest.mark.parametrize("bad", ["abc", "0", "-2", "", "1.5"])
def test_invalid_budget_falls_back_to_default_with_a_warning(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("BAOSTOCK_MAX_RELOGINS", bad)

    if bad == "":
        # empty string = unset -> silent default (no bogus config to complain about)
        _install_fake_baostock(monkeypatch, lambda _n, _a, _k: _FakeResult([]))
        assert _provider(tmp_path)._max_relogins == fin_api._DEFAULT_MAX_RELOGINS
        return

    _install_fake_baostock(monkeypatch, lambda _n, _a, _k: _FakeResult([]))
    with pytest.warns(UserWarning, match="BAOSTOCK_MAX_RELOGINS"):
        provider = _provider(tmp_path)
    assert provider._max_relogins == fin_api._DEFAULT_MAX_RELOGINS == 3


# ---------------------------------------------------------------------------
# 6. forensics: exactly one WARNING per relogin, naming n/MAX, symbol and code
# ---------------------------------------------------------------------------


def test_each_relogin_emits_exactly_one_warning(monkeypatch, tmp_path, caplog):
    def query(_n, _args, _kwargs):
        return _FakeResult(error_code=TRANSPORT_CODE, error_msg=TRANSPORT_MSG)

    _install_fake_baostock(monkeypatch, query)
    provider = _provider(tmp_path)

    with caplog.at_level(logging.WARNING, logger="common.financial_api"):
        for symbol in ("601288.SH", "601398.SH"):
            with pytest.raises(DataProviderSessionError):
                provider.fetch_raw_ohlcv(symbol, start="2026-10-20", end="2026-10-24")

    relogin_records = [r for r in caplog.records if "BaoStock relogin" in r.getMessage()]
    assert len(relogin_records) == fin_api._BAOSTOCK_RELOGIN_STATE["count"] == 3
    for index, record in enumerate(relogin_records, start=1):
        assert record.levelno == logging.WARNING
        text = record.getMessage()
        assert f"{index}/{provider._max_relogins}" in text
        assert "symbol=" in text
        assert TRANSPORT_CODE in text  # the trigger, even on the entry-time heal
        assert len(text) < 400  # message is truncated, never a whole page dump

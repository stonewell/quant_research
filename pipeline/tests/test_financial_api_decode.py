"""Offline regression tests for the 2026-10-27 Stage-1 decode / isolation defect.

THE INCIDENT (evidence: `web/livetrading/data/runs/.../2026-10-27/summary.json`,
`health_stdout` / `health_stderr`):

    -> Patching 601288.SH [FuyaoDataProvider] from 2016-01-01...
    'utf-8' codec can't decode byte 0xa1 in position 2: invalid start byte
    接收数据异常，请稍后再试。
    ...
    KeyboardInterrupt  (baostock/util/socketutil.py:72, default_socket.recv)

`health_returncode = -2` means the process died on SIGINT, i.e. the refresh *hung* in
baostock's unbounded `recv` after the server answered with a GBK/GB18030 error page;
the `UnicodeDecodeError` itself was swallowed by the library and only printed. The
surrounding contracts are what these tests pin:

1. HTTP bodies are decoded utf-8 (strict) -> gb18030 -> utf-8(errors="replace")+warning,
   never a bare utf-8 assumption;
2. a non-JSON body raises `DataProviderPayloadError` carrying host/path, status code,
   `encoding_used` and body[:200] -- never `UnicodeDecodeError` / bare `JSONDecodeError`;
3. one failing symbol is isolated: the loop continues, the failure is logged, and it is
   carried into `freshness_summary["fetch_errors"]` *separately* from "no data".

Everything here is offline: HTTP is replaced by fakes and the cache by a stub.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from common.financial_api import (
    DataProviderPayloadError,
    _decode_body,
    decode_json_body,
    decode_response_json,
    summarize_fetch_error,
)
from common.freshness import StalePriceError, assert_fresh_universe, build_freshness_summary

# The exact Chinese string baked into the incident's error page shape.
GBK_ERROR_PAGE = "<html><body>接收数据异常，请稍后再试。</body></html>"


# ---------------------------------------------------------------------------
# 1. Decode fallback chain
# ---------------------------------------------------------------------------


def test_gb18030_error_page_is_decoded_with_gb18030():
    raw = GBK_ERROR_PAGE.encode("gb18030")
    assert b"\xa1" in raw  # the byte that started the original traceback

    text, encoding_used = _decode_body(raw)

    assert encoding_used == "gb18030"
    assert text == GBK_ERROR_PAGE
    assert "接收数据异常" in text


def test_utf8_json_keeps_the_utf8_path_and_returns_the_same_object():
    raw = '{"rc": 0, "data": {"klines": ["2026-10-16,1,2,3,4,5"]}}'.encode("utf-8")

    text, encoding_used = _decode_body(raw)
    assert encoding_used == "utf-8"
    assert text == raw.decode("utf-8")

    payload = decode_json_body(raw, url="https://push2his.eastmoney.com/api/qt/stock/kline/get")
    assert payload["rc"] == 0
    assert payload["data"]["klines"] == ["2026-10-16,1,2,3,4,5"]


def test_body_that_is_neither_utf8_nor_valid_json_raises_the_contextual_error():
    # A GB18030 error page: decodable, but not JSON. Must NOT be a UnicodeDecodeError
    # and must NOT be a bare JSONDecodeError.
    raw = GBK_ERROR_PAGE.encode("gb18030")

    with pytest.raises(DataProviderPayloadError) as excinfo:
        decode_json_body(
            raw,
            url="https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.601288&klt=101",
            status_code=502,
            context=", symbol=601288.SH",
        )

    exc = excinfo.value
    message = str(exc)
    assert not isinstance(exc, UnicodeDecodeError)
    # URL host/path (query string dropped), status code, encoding, body snippet.
    assert "https://push2his.eastmoney.com/api/qt/stock/kline/get" in message
    assert "status=502" in message
    assert "encoding_used=gb18030" in message
    assert "symbol=601288.SH" in message
    assert "接收数据异常" in message

    # The same facts are available programmatically (no string parsing required).
    assert exc.status_code == 502
    assert exc.encoding_used == "gb18030"
    assert "接收数据异常" in exc.body_snippet
    assert len(exc.body_snippet) <= 200


def test_undecodable_binary_body_falls_back_to_replace_and_warns():
    raw = b"\xff\xfe\x00\x80 not valid utf-8 or gb18030 \xff"

    with pytest.warns(RuntimeWarning, match="neither utf-8 nor gb18030"):
        text, encoding_used = _decode_body(raw)

    assert encoding_used == "utf-8(errors=replace)"
    assert "not valid utf-8" in text


def test_response_double_with_only_json_method_keeps_legacy_behaviour():
    class _JsonOnlyResponse:
        status_code = 200

        def json(self):
            return {"rc": 0, "data": {"klines": []}}

    assert decode_response_json(_JsonOnlyResponse()) == {"rc": 0, "data": {"klines": []}}


def test_summarize_fetch_error_is_truncated_and_names_the_type():
    exc = DataProviderPayloadError("x" * 1000)
    rendered = summarize_fetch_error(exc)
    assert rendered.startswith("DataProviderPayloadError: ")
    assert len(rendered) == 400  # PAYLOAD_SNIPPET_CHARS * 2


# ---------------------------------------------------------------------------
# 2. EastMoney raises (instead of silently returning 0 bars) on an error page
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200, url: str = ""):
        self.content = body
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.headers = {}
        self.calls = 0

    def get(self, url, params=None, timeout=None):  # noqa: ARG002 - signature parity
        self.calls += 1
        return self._response


def test_eastmoney_provider_raises_payload_error_instead_of_empty_frame():
    from common.financial_api import EastMoneyDataProvider

    session = _FakeSession(
        _FakeResponse(
            GBK_ERROR_PAGE.encode("gb18030"),
            status_code=200,
            url="https://push2his.eastmoney.com/api/qt/stock/kline/get",
        )
    )
    provider = EastMoneyDataProvider(session=session, min_interval_sec=0.0)

    with pytest.raises(DataProviderPayloadError) as excinfo:
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-01", end="2026-10-27")

    assert excinfo.value.encoding_used == "gb18030"
    assert "601288.SH" in str(excinfo.value)
    assert "接收数据异常" in str(excinfo.value)
    assert session.calls == 1


def test_eastmoney_provider_utf8_json_path_is_unchanged():
    from common.financial_api import EastMoneyDataProvider

    body = (
        '{"rc": 0, "data": {"code": "601288", "market": 1, "klines": '
        '["2026-10-27,3.00,3.10,3.20,2.90,1000,3070000.00,3.07"]}}'
    ).encode("utf-8")
    session = _FakeSession(
        _FakeResponse(
            body, status_code=200, url="https://push2his.eastmoney.com/api/qt/stock/kline/get"
        )
    )
    provider = EastMoneyDataProvider(session=session, min_interval_sec=0.0)

    df = provider.fetch_raw_ohlcv("601288.SH", start="2026-10-01", end="2026-10-27")

    assert list(df.columns)[:5] == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 1
    assert df["Open"].iloc[0] == 3.00
    assert df["Close"].iloc[0] == 3.10
    assert df["Volume"].iloc[0] == 100000.0  # lots -> shares


# ---------------------------------------------------------------------------
# 3. Per-symbol isolation: two consecutive failures do not stop the loop
# ---------------------------------------------------------------------------


class _StubCache:
    """Minimal DuckDBCache stand-in: the patch loop only needs coverage + connection."""

    is_memory = True

    def __init__(self):
        self.stores = []

    def _get_connection(self, read_only=True):  # noqa: ARG002 - signature parity
        class _Con:
            def execute(self, *args, **kwargs):  # noqa: ARG002 - signature parity
                class _Result:
                    def fetchall(self_inner):
                        return []

                return _Result()

            def close(self_inner):
                return None

        return _Con()

    def store_bars(self, **kwargs):  # pragma: no cover - must never be reached
        self.stores.append(kwargs)


class _RaisingBaoStock:
    """BaoStock double whose transport answers with the incident's GBK error page."""

    def __init__(self, **kwargs):  # noqa: ARG002 - provider kwargs
        self.calls = []

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval)

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):  # noqa: ARG002
        self.calls.append(symbol)
        raise DataProviderPayloadError(
            f"non-JSON payload from https://baostock.example/query "
            f"(status=502, encoding_used=gb18030, symbol={symbol}): "
            f"UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1 in position 2; "
            f"body[:200]={GBK_ERROR_PAGE!r}",
            url="https://baostock.example/query?symbol=" + symbol,
            status_code=502,
            encoding_used="gb18030",
            body_snippet=GBK_ERROR_PAGE,
        )

    def logout(self):
        return None


class _EmptyEastMoney:
    """Fallback double that legitimately has nothing -- must not be called 'an error'."""

    def __init__(self, **kwargs):  # noqa: ARG002
        self.calls = []

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval)

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):  # noqa: ARG002
        self.calls.append(symbol)
        return pd.DataFrame()

    def logout(self):
        return None


def test_two_failing_symbols_are_isolated_and_surface_in_freshness_summary(
    monkeypatch, caplog
):
    import scripts.sync_china_5m_and_indices as sync

    monkeypatch.setattr(sync, "BaoStockDataProvider", _RaisingBaoStock)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", _EmptyEastMoney)
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)

    cache = _StubCache()
    symbols = ["601288.SH", "601398.SH"]

    with caplog.at_level(logging.WARNING, logger="scripts.sync_china_5m_and_indices"):
        fetch_errors = sync.patch_equity_daily_bars(cache, symbols, end="2026-10-27")

    # 1. The loop did not abort: both symbols were attempted, and the cache was never
    #    written with an empty frame (no silent "0 bars" write).
    assert set(fetch_errors) == set(symbols)
    assert cache.stores == []

    # 2. Exactly one warning per failing symbol, each naming the symbol, the exception
    #    type and a slice of the poisoned body.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    for record, sym in zip(warnings, symbols):
        message = record.getMessage()
        assert f"symbol={sym}" in message
        assert "DataProviderPayloadError" in message
        assert "接收数据异常" in message
        assert "body[:200]=" in message

    # 3. `freshness_summary` keeps the existing fields and adds the WHY: both symbols
    #    are `missing` (no frame was produced), and both are ALSO recorded as fetch
    #    errors -- "no data" would appear only in `missing_symbols`.
    summary = build_freshness_summary(
        {},  # nothing loaded: exactly the state a failed refresh leaves behind
        "2026-10-27",
        expected_symbols=symbols,
        stage="stage1",
        strict=True,
        fetch_errors=fetch_errors,
    )
    assert summary["status"] == "FAIL"
    assert summary["missing_symbols"] == symbols
    assert set(summary["fetch_errors"]) == set(symbols)
    assert all(
        reason.startswith(("BaoStockDataProvider: DataProviderPayloadError", "EastMoneyDataProvider:"))
        or "DataProviderPayloadError" in reason
        for reason in summary["fetch_errors"].values()
    )

    # The hard gate distinguishes the two conditions in its message.
    with pytest.raises(Exception) as excinfo:
        assert_fresh_universe(
            {},
            "2026-10-27",
            strict=True,
            expected_symbols=symbols,
            stage="stage1",
            fetch_errors=fetch_errors,
        )
    assert "抓取报错（不是「无数据」）" in str(excinfo.value)
    assert "601288.SH" in str(excinfo.value) and "601398.SH" in str(excinfo.value)


def test_symbol_with_zero_bars_is_not_reported_as_a_fetch_error(monkeypatch):
    import scripts.sync_china_5m_and_indices as sync

    class _EmptyProvider:
        def __init__(self, **kwargs):  # noqa: ARG002
            pass

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return self.fetch_raw_ohlcv(symbol, start, end, interval=interval)

        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):  # noqa: ARG002
            # A valid, understood answer for a symbol that simply has no new bars.
            return pd.DataFrame()

        def logout(self):
            return None

    monkeypatch.setattr(sync, "BaoStockDataProvider", _EmptyProvider)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", _EmptyProvider)
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)

    cache = _StubCache()
    fetch_errors = sync.patch_equity_daily_bars(cache, ["601288.SH"], end="2026-10-27")

    # "0 new bars" stays a non-event: no error bookkeeping, no write.
    assert fetch_errors == {}
    assert cache.stores == []


def test_baostock_socket_timeout_is_applied_after_login(monkeypatch, tmp_path):
    """The unbounded `recv` that produced rc=-2 is bounded right after login."""
    import sys
    import types

    import common.financial_api as fin_api

    settled = {}

    class _Sock:
        def settimeout(self, value):
            settled["timeout"] = value

    class _EmptyRs:
        error_code = "0"  # understood request, zero rows -> genuinely "no bars"
        error_msg = ""
        data = []
        fields = []
        cur_row_num = 0

        def next(self):
            return False

    logins = []

    fake_context = types.SimpleNamespace(default_socket=_Sock())
    fake_common = types.SimpleNamespace(context=fake_context)
    fake_bs = types.SimpleNamespace(
        common=fake_common,
        login=lambda: (logins.append(1), types.SimpleNamespace(error_code="0"))[1],
        logout=lambda: None,
        query_history_k_data_plus=lambda *a, **k: _EmptyRs(),
    )

    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    monkeypatch.setitem(sys.modules, "baostock.common", fake_common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", fake_context)
    monkeypatch.setenv("BAOSTOCK_SOCKET_TIMEOUT", "7.5")

    provider = fin_api.BaoStockDataProvider(
        adjustflag="3", keep_logged_in=True, cache_dir=str(tmp_path / "cache.duckdb")
    )
    out = provider.fetch_raw_ohlcv("601288.SH", start="2026-10-01", end="2026-10-27")

    # A real login happened, and the socket was bounded as part of that path.
    assert logins == [1]
    assert settled["timeout"] == pytest.approx(7.5)
    # Zero rows on a valid answer still means "no data", not an error.
    assert out.empty


def test_baostock_protocol_error_raises_instead_of_empty_frame(monkeypatch, tmp_path):
    """`error_code != "0"` is a transport failure, never "this symbol has no bars"."""
    import sys
    import types

    import common.financial_api as fin_api

    class _Rs:
        error_code = "-1"
        error_msg = "接收数据异常，请稍后再试。"
        data = []
        fields = []
        cur_row_num = 0

        def next(self):
            return False

    fake_context = types.SimpleNamespace(default_socket=None)
    fake_common = types.SimpleNamespace(context=fake_context)
    fake_bs = types.SimpleNamespace(
        common=fake_common,
        login=lambda: types.SimpleNamespace(error_code="0"),
        logout=lambda: None,
        query_history_k_data_plus=lambda *a, **k: _Rs(),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    monkeypatch.setitem(sys.modules, "baostock.common", fake_common)
    monkeypatch.setitem(sys.modules, "baostock.common.context", fake_context)

    provider = fin_api.BaoStockDataProvider(adjustflag="3", cache_dir=str(tmp_path / "cache.duckdb"))
    with pytest.raises(DataProviderPayloadError) as excinfo:
        provider.fetch_raw_ohlcv("601288.SH", start="2026-10-01", end="2026-10-27")

    assert "unusable BaoStock answer for 601288.SH" in str(excinfo.value)
    assert "接收数据异常" in str(excinfo.value)

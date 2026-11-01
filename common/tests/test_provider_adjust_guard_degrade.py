"""Offline pins for the "cannot honour the requested adjustment" downgrade path.

BaoStock's index query has no adjustment field, and Fuyao's index/fund endpoints have no
adjustment parameter: both warn and declare ``raw`` instead of returning bars labelled with
whatever basis the caller asked for. No socket/HTTP/DuckDB -- baostock is faked, ``fuyao_client``
is a stub module in ``sys.modules``.
"""

import logging
import os
import sys
import types

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import financial_api as fa  # noqa: E402
from common.data import _provider_native_basis  # noqa: E402


def _fuyao_items(dates):
    """Fuyao REST payload shape: one dict per bar, snake_case prices + ``date_ms``."""
    return [
        {
            "date_ms": int(pd.Timestamp(d).tz_localize("Asia/Shanghai").timestamp() * 1000),
            "open_price": 3500.0, "high_price": 3550.0, "low_price": 3490.0,
            "close_price": 3520.0, "volume": 1_000_000.0,
        }
        for d in dates
    ]


def test_baostock_index_downgrade_is_warned_and_stamped(caplog, monkeypatch):
    """`BaoStockDataProvider(adjustflag="2")` (qfq) on an index -> warned + forced raw."""

    class _FakeResult:
        """Duck-typed `baostock.ResultData`: paged `data` list + cursor (see the module OOM notes)."""

        error_code, error_msg = "0", ""

        def __init__(self, fields, rows):
            self.fields, self.data, self.cur_row_num = fields.split(","), rows, 0

        def next(self):
            return False

    class _FakeBS:
        last_fields = last_adjustflag = None

        def query_history_k_data_plus(self, code, fields, start_date, end_date, frequency, adjustflag):
            self.last_fields, self.last_adjustflag = fields, adjustflag
            row = ["2024-01-02", "sh.000300", "3500", "3550", "3490", "3520", "3490", "1000000", "3.5e9", "0.5"]
            return _FakeResult(fields, [row])

        def logout(self):
            return None

    fake_bs = _FakeBS()
    provider = fa.BaoStockDataProvider(adjustflag="2")
    assert provider.native_adjust_basis == "qfq"
    monkeypatch.setattr(provider, "_get_bs", lambda: fake_bs)
    monkeypatch.setattr(provider, "_ensure_session", lambda bs, symbol=None: True)

    with caplog.at_level(logging.WARNING, logger=fa._LOGGER.name):
        df = provider.fetch_ohlcv("000300.SH", "2024-01-02", "2024-01-03")

    # The requested qfq flag never reaches the socket: indices are forced to "3".
    assert fake_bs.last_adjustflag == "3"
    assert "adjustflag" not in fake_bs.last_fields
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("000300.SH" in m and "cannot honour adjustflag" in m for m in msgs), msgs
    assert not df.empty
    assert df["adjustflag"].tolist() == ["3"]
    assert df.attrs["adjust_basis"] == "raw"


def test_fuyao_index_and_etf_downgrade(caplog, monkeypatch):
    """Fuyao's index and fund endpoints are unadjusted by construction -> warned + raw."""
    dates = ["2024-01-02", "2024-01-03"]

    class _StubLocal:
        """Empty-but-DatetimeIndex-indexed local frame: forces the remote path offline."""

        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            self.calls.append(symbol)
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex([])
            )

    stub_local = _StubLocal()
    fake_client = types.ModuleType("fuyao_client")
    fake_client.index_prices_historical = lambda **kw: _fuyao_items(dates)
    fake_client.fund_market_historical = lambda **kw: {"item": _fuyao_items(dates)}
    monkeypatch.setitem(sys.modules, "fuyao_client", fake_client)

    provider = fa.FuyaoDataProvider(adjust="qfq", prefer_local=False)
    provider._local_provider = stub_local
    assert provider.native_adjust_basis == "qfq"

    cases = {"510300.SH": "is an ETF/fund and cannot honour", "000300.SH": "is an index and cannot honour"}
    for symbol, needle in cases.items():
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=fa._LOGGER.name):
            df = provider.fetch_ohlcv(symbol, dates[0], dates[-1])
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(symbol in m and needle in m for m in msgs), (symbol, msgs)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert not df.empty
        assert df.attrs["adjust_basis"] == "raw", symbol

    # Both fetches consulted the local stub first, then fell back to the (stubbed) remote API.
    assert stub_local.calls == ["510300.SH", "000300.SH"]


def test_marketdb_stamps_served_basis(tmp_path):
    """`MarketDBDataProvider` labels each return with the view it actually read.

    ``v_daily`` (raw) and ``v_daily_qfq`` are separate relations with DIFFERENT closes here, so
    the assertion is not a read-back of a value the test itself wrote: the served basis has to
    follow the view that produced the bars. The databases are built in ``tmp_path`` with duckdb's
    Python API -- no network, no real market.duckdb.
    """
    import duckdb

    symbol = "600000.SH"
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    raw_close = [10.0, 10.2, 10.1]
    # qfq closes are deliberately NOT equal to raw: a provider reading v_daily_qfq must not be
    # able to pass this test by mislabelling raw bars (nor raw bars pass as qfq).
    qfq_close = [7.0, 7.14, 7.07]

    db_path = tmp_path / "market.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE daily (thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, "
        "close DOUBLE, volume DOUBLE)"
    )
    con.executemany(
        "INSERT INTO daily VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (symbol, d, c, c + 0.2, c - 0.2, c, 1_000.0)
            for d, c in zip(dates, raw_close)
        ],
    )
    con.execute("CREATE VIEW v_daily AS SELECT * FROM daily")
    con.execute(
        "CREATE VIEW v_daily_qfq AS SELECT thscode, date, open * 0.7 AS open, high * 0.7 AS high, "
        "low * 0.7 AS low, close * 0.7 AS close, volume FROM daily"
    )
    con.execute("CREATE VIEW v_daily_hfq AS SELECT * FROM daily")
    con.close()

    # Raw view: the stamp is "raw" regardless of the instance's own `adjust=`.
    raw_provider = fa.MarketDBDataProvider(db_path=str(db_path), adjust="qfq")
    assert raw_provider.native_adjust_basis == "qfq"
    raw_df = raw_provider.fetch_raw_ohlcv(symbol, dates[0], dates[-1])
    assert raw_df.attrs["adjust_basis"] == "raw"
    assert raw_df["Close"].tolist() == raw_close
    assert _provider_native_basis(raw_provider, raw_df) == "raw"

    # Adjusted view: the stamp follows the instance basis, and the bars really come from v_daily_qfq.
    qfq_provider = fa.MarketDBDataProvider(db_path=str(db_path), adjust="qfq")
    qfq_df = qfq_provider.fetch_ohlcv(symbol, dates[0], dates[-1])
    assert qfq_df.attrs["adjust_basis"] == "qfq"
    assert qfq_df["Close"].round(6).tolist() == qfq_close
    assert _provider_native_basis(qfq_provider, qfq_df) == "qfq"


def test_fuyao_local_frame_basis_propagates():
    """A local frame declaring ``raw`` keeps that label through Fuyao -- the frame wins.

    `FuyaoDataProvider` must NOT re-stamp the local frame with its own ``qfq`` default: the local
    declaration is the honest one (the local DB may legitimately serve unadjusted index/ETF bars),
    and `_provider_native_basis` has to read it back, otherwise the cache records a FALSE basis.
    """

    dates = pd.date_range("2024-01-02", periods=3, freq="D")
    close = [3_500.0, 3_520.0, 3_510.0]
    local_frame = pd.DataFrame(
        {
            "Open": close,
            "High": [c + 10.0 for c in close],
            "Low": [c - 10.0 for c in close],
            "Close": close,
            "Volume": [1_000_000.0] * 3,
        },
        index=dates,
    )
    local_frame.attrs["adjust_basis"] = "raw"

    class _StubLocal:
        """Serves `local_frame` verbatim, covering the whole requested window (so no remote call)."""

        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            self.calls.append((symbol, start, end))
            return local_frame.copy()

    stub_local = _StubLocal()
    provider = fa.FuyaoDataProvider(adjust="qfq", prefer_local=False)
    provider._local_provider = stub_local
    assert provider.native_adjust_basis == "qfq"

    out = provider.fetch_ohlcv("600000.SH", "2024-01-02", "2024-01-04")
    assert stub_local.calls and stub_local.calls[0][0] == "600000.SH"
    assert out["Close"].tolist() == close
    # The frame's own declaration survived the local path untouched...
    assert out.attrs["adjust_basis"] == "raw"
    # ...and it beats the provider-level `qfq` default when the served basis is asked for.
    assert _provider_native_basis(provider, out) == "raw"
    assert _provider_native_basis(provider) == "qfq"

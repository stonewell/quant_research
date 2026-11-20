"""Offline end-to-end tests for BaoStock & DuckDBCache `Turn` column wiring (Track MS, MS10).

All tests run 100% offline using in-memory / temporary DuckDB caches and mocked
BaoStock ResultData objects.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import DuckDBCache, resample_ohlcv
from common.financial_api import BaoStockDataProvider
from common.structural_factors import _run_cyq_recurrence, cyq_chip_distribution


class _MockResultData:
    """Minimal BaoStock ResultData double supporting cursor pagination."""

    def __init__(self, fields: list[str], rows: list[list[str]]):
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = fields
        self.data = rows
        self.cur_row_num = 0

    def next(self) -> bool:
        if self.cur_row_num < len(self.data):
            self.cur_row_num += 1
            return True
        return False

    def get_row_data(self) -> list[str]:
        return self.data[self.cur_row_num - 1]


def _make_daily_bars(with_turn: bool = True, turn_vals: list[float] | None = None) -> pd.DataFrame:
    dates = pd.bdate_range("2025-02-03", periods=5)
    data = {
        "Open": [10.0, 10.5, 11.0, 11.5, 12.0],
        "High": [10.6, 11.1, 11.6, 12.1, 12.6],
        "Low": [9.8, 10.2, 10.7, 11.2, 11.7],
        "Close": [10.4, 10.9, 11.4, 11.9, 12.4],
        "Volume": [1000.0, 1200.0, 1500.0, 1300.0, 1800.0],
        "Amount": [10400.0, 13080.0, 17100.0, 15470.0, 22320.0],
    }
    if with_turn:
        data["Turn"] = turn_vals if turn_vals is not None else [2.5, 3.0, 3.5, 2.8, 4.1]
    return pd.DataFrame(data, index=dates)


def test_baostock_provider_maps_turn_for_equity_and_omits_for_index_5m():
    class _FakeBs:
        class _LoginRes:
            error_code = "0"
            error_msg = ""

        def login(self):
            return self._LoginRes()

        def logout(self):
            return self._LoginRes()

        def query_history_k_data_plus(self, code, fields, **kwargs):
            field_list = [f.strip() for f in fields.split(",")]
            if "turn" in field_list:
                row_dicts = [
                    {
                        "date": "2025-02-03", "code": code, "open": "10.0", "high": "10.5",
                        "low": "9.8", "close": "10.2", "preclose": "10.0", "volume": "1000",
                        "amount": "10200", "adjustflag": "3", "turn": "2.45", "pctChg": "2.0",
                        "isST": "0",
                    },
                    {
                        "date": "2025-02-04", "code": code, "open": "10.2", "high": "10.8",
                        "low": "10.1", "close": "10.7", "preclose": "10.2", "volume": "1200",
                        "amount": "12840", "adjustflag": "3", "turn": "", "pctChg": "4.9",
                        "isST": "0",
                    },
                ]
            elif "time" in field_list:
                row_dicts = [
                    {
                        "date": "2025-02-03", "time": "20250203093500000", "code": code,
                        "open": "10.0", "high": "10.1", "low": "9.9", "close": "10.05",
                        "volume": "500", "amount": "5025", "adjustflag": "3",
                    },
                ]
            else:
                row_dicts = [
                    {
                        "date": "2025-02-03", "code": code, "open": "3000.0", "high": "3020.0",
                        "low": "2990.0", "close": "3010.0", "preclose": "3000.0",
                        "volume": "500000", "amount": "1500000000", "pctChg": "0.33",
                    },
                ]
            rows = [[r.get(f, "") for f in field_list] for r in row_dicts]
            return _MockResultData(field_list, rows)

    fake_bs = _FakeBs()
    prov = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)
    with patch.object(prov, "_get_bs", return_value=fake_bs):
        eq_df = prov.fetch_raw_ohlcv("600519.SH", "2025-02-03", "2025-02-04", interval="1d")
        assert "Turn" in eq_df.columns
        assert eq_df["Turn"].iloc[0] == pytest.approx(2.45)
        assert np.isnan(eq_df["Turn"].iloc[1])  # empty string coerced to NaN without dropping bar
        assert len(eq_df) == 2

        idx_df = prov.fetch_raw_ohlcv("000300.SH", "2025-02-03", "2025-02-04", interval="1d")
        assert "Turn" not in idx_df.columns
        assert len(idx_df) == 1

        intraday_df = prov.fetch_raw_ohlcv("600519.SH", "2025-02-03", "2025-02-03", interval="5m")
        assert "Turn" not in intraday_df.columns
        assert len(intraday_df) == 1


def test_duckdb_schema_migration_and_column_contracts(tmp_path):
    # 1. Fresh cache schema: has `turn DOUBLE` and NO `adjustflag`
    cache = DuckDBCache(db_path=":memory:")
    con = cache._get_connection(read_only=True)
    info = con.execute("PRAGMA table_info('ohlcv_bars')").df()
    col_types = dict(zip(info["name"], info["type"]))
    assert "turn" in col_types and col_types["turn"].upper() == "DOUBLE"
    assert "adjustflag" not in col_types

    # 2. Legacy DB migration without `turn` -> idempotent `_ensure_column` adds it
    legacy_path = str(tmp_path / "legacy_no_turn.duckdb")
    raw_con = duckdb.connect(legacy_path)
    raw_con.execute(
        """
        CREATE TABLE ohlcv_bars (
            provider VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            date DATE NOT NULL,
            open DOUBLE NOT NULL,
            high DOUBLE NOT NULL,
            low DOUBLE NOT NULL,
            close DOUBLE NOT NULL,
            volume DOUBLE NOT NULL,
            amount DOUBLE,
            PRIMARY KEY (provider, symbol, date)
        );
        """
    )
    raw_con.close()

    migrated = DuckDBCache(db_path=legacy_path)
    m_con = migrated._get_connection(read_only=True)
    m_info = m_con.execute("PRAGMA table_info('ohlcv_bars')").df()
    m_con.close()
    assert "turn" in set(m_info["name"])

    # 3. Column contracts: None -> 5 cols, "all" -> 6 cols, explicit Turn -> includes Turn
    bars = _make_daily_bars(with_turn=True)
    cache.store_bars(
        "BaoStockDataProvider",
        "600519.SH",
        bars,
        requested_start="2025-02-03",
        requested_end="2025-02-07",
        adjustflag="raw",
    )

    df_default = cache.query_bars("BaoStockDataProvider", "600519.SH", adjust="raw")
    assert list(df_default.columns) == ["Open", "High", "Low", "Close", "Volume"]

    df_all = cache.query_bars("BaoStockDataProvider", "600519.SH", adjust="raw", columns="all")
    assert list(df_all.columns) == ["Open", "High", "Low", "Close", "Volume", "Amount"]

    req_cols = ["Open", "High", "Low", "Close", "Volume", "Turn"]
    df_turn = cache.query_bars("BaoStockDataProvider", "600519.SH", adjust="raw", columns=req_cols)
    assert list(df_turn.columns) == req_cols
    np.testing.assert_allclose(df_turn["Turn"].to_numpy(), bars["Turn"].to_numpy())

    univ = cache.query_universe(
        "BaoStockDataProvider", ["600519.SH"], adjust="raw", columns=req_cols
    )
    assert "600519.SH" in univ
    np.testing.assert_allclose(univ["600519.SH"]["Turn"].to_numpy(), bars["Turn"].to_numpy())


def test_coalesce_non_clobber_and_backfill_without_history_rewrite_refused():
    cache = DuckDBCache(db_path=":memory:")
    bars_with_turn = _make_daily_bars(with_turn=True, turn_vals=[2.5, 3.0, 3.5, 2.8, 4.1])
    bars_no_turn = _make_daily_bars(with_turn=False)

    # Case A: Store WITH Turn, then re-store identical OHLCV WITHOUT Turn -> Turn preserved
    cache.store_bars("BaoStockDataProvider", "600519.SH", bars_with_turn, adjustflag="raw")
    cache.store_bars("BaoStockDataProvider", "600519.SH", bars_no_turn, adjustflag="raw")
    out_a = cache.query_bars(
        "BaoStockDataProvider", "600519.SH", adjust="raw", columns=["Close", "Turn"]
    )
    np.testing.assert_allclose(out_a["Turn"].to_numpy(), [2.5, 3.0, 3.5, 2.8, 4.1])

    # Case B: Store WITHOUT Turn, then backfill WITH Turn -> Turn populated, no HistoryRewriteRefused
    cache.store_bars("BaoStockDataProvider", "000001.SZ", bars_no_turn, adjustflag="raw")
    cache.store_bars("BaoStockDataProvider", "000001.SZ", bars_with_turn, adjustflag="raw")
    out_b = cache.query_bars(
        "BaoStockDataProvider", "000001.SZ", adjust="raw", columns=["Close", "Turn"]
    )
    np.testing.assert_allclose(out_b["Turn"].to_numpy(), [2.5, 3.0, 3.5, 2.8, 4.1])


def test_qfq_adjust_does_not_scale_turn_and_feeds_cyq_real_turn_branch():
    cache = DuckDBCache(db_path=":memory:")
    bars = _make_daily_bars(with_turn=True, turn_vals=[10.0, 12.0, 15.0, 11.0, 14.0])
    cache.store_bars("BaoStockDataProvider", "600519.SH", bars, adjustflag="raw")

    factors = pd.DataFrame(
        {"factor_hfq": [1.0, 1.0, 2.0, 2.0, 2.0]},
        index=bars.index,
    )
    cache.store_adjust_factors("600519.SH", factors, source="BaoStockDataProvider")

    qfq = cache.query_bars(
        "BaoStockDataProvider",
        "600519.SH",
        adjust="qfq",
        columns=["Open", "High", "Low", "Close", "Volume", "Turn"],
    )
    # Earlier prices are scaled by 1.0 / 2.0 = 0.5, while Turn is untouched!
    assert qfq["Close"].iloc[0] == pytest.approx(bars["Close"].iloc[0] * 0.5)
    np.testing.assert_allclose(qfq["Turn"].to_numpy(), bars["Turn"].to_numpy())

    # End-to-end into CYQ: real Turn branch is taken instead of volume proxy
    _, _, _, _, used_real_turn = _run_cyq_recurrence(qfq)
    assert used_real_turn is True
    wr_real, cc_real = cyq_chip_distribution(qfq)
    wr_proxy, _ = cyq_chip_distribution(qfq.drop(columns=["Turn"]))
    assert not np.allclose(wr_real.to_numpy(), wr_proxy.to_numpy())
    assert len(cc_real) == len(qfq)

    # resample_ohlcv sums Turn with min_count=1 (all-NaN stays NaN, never 0.0)
    bars_nan_turn = bars.copy()
    bars_nan_turn["Turn"] = np.nan
    wk_nan = resample_ohlcv(bars_nan_turn, "1wk")
    assert wk_nan["Turn"].isna().all()
    wk_real = resample_ohlcv(bars, "1wk")
    assert wk_real["Turn"].iloc[0] == pytest.approx(sum([10.0, 12.0, 15.0, 11.0, 14.0]))

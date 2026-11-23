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
    assert list(df_all.columns) == ["Open", "High", "Low", "Close", "Volume", "Amount", "Turn"]
    np.testing.assert_allclose(df_all["Turn"].to_numpy(), bars["Turn"].to_numpy())

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


def test_w12_real_baostock_to_duckdb_to_load_universe_and_no_include_turn(tmp_path):
    """W1.2 end-to-end (§57.3): real BaoStockDataProvider -> real DuckDBCache -> load_universe
    delivers Turn by default, and include_turn=False (--no-include-turn) drops it."""
    import common.financial_api as fapi
    from common.data import load_universe

    class _FakeBS:
        class _LoginRes:
            error_code = "0"
            error_msg = "success"

        def login(self):
            return self._LoginRes()

        def logout(self):
            return self._LoginRes()

        def query_history_k_data_plus(self, code, fields, **kwargs):
            field_list = [f.strip() for f in fields.split(",")]
            row_dicts = [
                {
                    "date": "2024-01-02", "code": code, "open": "100.0", "high": "102.0",
                    "low": "99.0", "close": "101.0", "preclose": "100.0", "volume": "10000",
                    "amount": "1010000", "adjustflag": "3", "turn": "2.50", "pctChg": "1.0", "isST": "0",
                },
                {
                    "date": "2024-01-03", "code": code, "open": "101.0", "high": "103.0",
                    "low": "100.0", "close": "102.0", "preclose": "101.0", "volume": "11000",
                    "amount": "1122000", "adjustflag": "3", "turn": "3.10", "pctChg": "1.0", "isST": "0",
                },
                {
                    "date": "2024-01-04", "code": code, "open": "102.0", "high": "104.0",
                    "low": "101.0", "close": "103.0", "preclose": "102.0", "volume": "12000",
                    "amount": "1236000", "adjustflag": "3", "turn": "2.80", "pctChg": "1.0", "isST": "0",
                },
                {
                    "date": "2024-01-05", "code": code, "open": "103.0", "high": "105.0",
                    "low": "102.0", "close": "104.0", "preclose": "103.0", "volume": "13000",
                    "amount": "1352000", "adjustflag": "3", "turn": "3.40", "pctChg": "1.0", "isST": "0",
                },
            ]
            rows = [[r.get(f, "") for f in field_list] for r in row_dicts]
            return _MockResultData(field_list, rows)

        def query_adjust_factor(self, code, **kwargs):
            return _MockResultData(
                ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
                [[code, "2024-01-02", "1.0", "1.0", "1.0"]],
            )

    cache_dir = str(tmp_path / "bs_turn_cache")
    prov = fapi.BaoStockDataProvider(adjustflag="3", keep_logged_in=True)

    with patch.object(prov, "_get_bs", return_value=_FakeBS()):
        # Default include_turn=True -> 7 columns including Turn
        u_on = load_universe(
            ["600519.SH"],
            "2024-01-02",
            "2024-01-05",
            use_cache=True,
            cache_dir=cache_dir,
            provider=prov,
        )
        assert "600519.SH" in u_on
        assert list(u_on["600519.SH"].columns) == [
            "Open", "High", "Low", "Close", "Volume", "Amount", "Turn",
        ]
        np.testing.assert_allclose(u_on["600519.SH"]["Turn"].to_numpy(), [2.50, 3.10, 2.80, 3.40])

        # Opt-out include_turn=False -> 6 columns, Turn absent
        u_off = load_universe(
            ["600519.SH"],
            "2024-01-02",
            "2024-01-05",
            use_cache=True,
            cache_dir=cache_dir,
            provider=prov,
            include_turn=False,
        )
        assert list(u_off["600519.SH"].columns) == [
            "Open", "High", "Low", "Close", "Volume", "Amount",
        ]


def test_w12_chan_four_state_blend_zero_weight_drift_when_turn_column_present():
    """W1.1/W1.2 isolation: ChanFourStateBlendStrategy produces 100% bit-identical weights
    whether or not daily frames carry a Turn column."""
    from common.testing import make_ohlcv_from_closes
    from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy

    rng = np.random.default_rng(77)
    dates = pd.bdate_range("2023-01-02", periods=160)
    u_no_turn = {}
    u_with_turn = {}
    for sym in ["600519.SH", "000001.SZ", "600036.SH", "000858.SZ", "BIL"]:
        rets = rng.normal(0.0004, 0.015, size=len(dates))
        closes = 100.0 * np.exp(np.cumsum(rets))
        df = make_ohlcv_from_closes(closes)
        df.index = dates
        u_no_turn[sym] = df
        u_with_turn[sym] = df.assign(Turn=rng.uniform(1.0, 8.0, size=len(dates)))

    strat = ChanFourStateBlendStrategy()
    w_no = strat.generate_weights(u_no_turn, {"cash_proxy": "BIL"})
    w_with = strat.generate_weights(u_with_turn, {"cash_proxy": "BIL"})
    pd.testing.assert_frame_equal(w_with, w_no, rtol=0.0, atol=0.0)

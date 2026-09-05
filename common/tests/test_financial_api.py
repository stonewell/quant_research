"""Unit tests for common/financial_api.py (MarketDB and Fuyao Data Providers).

All tests run 100% offline without network access or live market data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import duckdb
import numpy as np
import pandas as pd
import pytest

from common.data import (
    CachedDataProvider,
    get_data_provider,
    load_ohlcv,
    load_universe,
)
from common.financial_api import (
    FuyaoDataProvider,
    MarketDBDataProvider,
    _resolve_duckdb_path,
    normalize_thscode,
)


def test_normalize_thscode():
    # Yahoo SS suffix
    assert normalize_thscode("600519.SS") == "600519.SH"
    assert normalize_thscode("600519.ss") == "600519.SH"
    assert normalize_thscode("000001.sz") == "000001.SZ"
    assert normalize_thscode("886042.ti") == "886042.TI"

    # Bare 6-digit codes
    assert normalize_thscode("600519") == "600519.SH"
    assert normalize_thscode("601398") == "601398.SH"
    assert normalize_thscode("688981") == "688981.SH"
    assert normalize_thscode("000001") == "000001.SZ"
    assert normalize_thscode("300750") == "300750.SZ"
    assert normalize_thscode("920002") == "920002.BJ"
    assert normalize_thscode("510300") == "510300.SH"
    assert normalize_thscode("159919") == "159919.SZ"
    assert normalize_thscode("881121") == "881121.TI"

    # Other symbols remain untouched
    assert normalize_thscode("SPY") == "SPY"
    assert normalize_thscode("AAPL") == "AAPL"


def test_resolve_duckdb_path(tmp_path):
    # Non-existent returns None or default
    assert _resolve_duckdb_path(folder_path=str(tmp_path / "non_existent")) is None

    # Direct duckdb file
    dummy_db = tmp_path / "test.duckdb"
    dummy_db.write_text("test")
    assert _resolve_duckdb_path(db_path=str(dummy_db)) == str(dummy_db)
    assert _resolve_duckdb_path(folder_path=str(dummy_db)) == str(dummy_db)

    # Directory containing market.duckdb
    sub_dir = tmp_path / "db_folder"
    sub_dir.mkdir()
    market_db = sub_dir / "market.duckdb"
    market_db.write_text("test")
    assert _resolve_duckdb_path(folder_path=str(sub_dir)) == str(market_db)


@pytest.fixture
def in_memory_marketdb():
    """Sets up an in-memory DuckDB database with mock v_daily_qfq and raw tables."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        );
        CREATE VIEW v_daily_qfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;
    """)

    # Populate sample rows for 600519.SH and 000001.SZ
    dates = pd.bdate_range("2024-01-01", periods=5)
    for i, d in enumerate(dates):
        d_str = d.strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO raw_kline_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("600519.SH", d_str, 100.0 + i, 105.0 + i, 95.0 + i, 102.0 + i, 1000.0 * (i + 1)),
        )
        con.execute(
            "INSERT INTO raw_kline_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("000001.SZ", d_str, 10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 5000.0 * (i + 1)),
        )

    provider = MarketDBDataProvider(con=con)
    yield provider
    con.close()


def test_marketdb_provider_fetch_ohlcv(in_memory_marketdb):
    df = in_memory_marketdb.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-03")
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["Close"].iloc[0] == 102.0

    # Test bare ticker normalization
    df_bare = in_memory_marketdb.fetch_ohlcv("600519", start="2024-01-01", end="2024-01-03")
    assert len(df_bare) == 3

    # Test interval validation
    with pytest.raises(ValueError, match="only supports interval='1d'"):
        in_memory_marketdb.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-03", interval="1h")

    # Test unknown symbol
    with pytest.raises(ValueError, match="No data returned for UNKNOWN.SH"):
        in_memory_marketdb.fetch_ohlcv("UNKNOWN.SH", start="2024-01-01", end="2024-01-03")


def test_marketdb_provider_fetch_universe(in_memory_marketdb):
    symbols = ["600519.SH", "000001.SZ", "NON_EXISTENT.SH"]
    uni = in_memory_marketdb.fetch_universe(symbols, start="2024-01-01", end="2024-01-03")

    assert "600519.SH" in uni
    assert "000001.SZ" in uni
    assert "NON_EXISTENT.SH" not in uni
    assert len(uni["600519.SH"]) == 3
    assert len(uni["000001.SZ"]) == 3


def test_marketdb_provider_drops_invalid_rows():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE v_daily_qfq (
            thscode VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        );
    """)
    # Insert one valid row and one invalid row (high < low)
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 100.0, 105.0, 95.0, 102.0, 1000.0)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-02', 100.0, 90.0, 105.0, 102.0, 1000.0)")

    provider = MarketDBDataProvider(con=con)
    df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-05")
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2024-01-01")
    con.close()


def test_fuyao_provider_fetch_ohlcv_mocked():
    mock_items = [
        {
            "date_ms": 1704153600000,  # 2024-01-02
            "open_price": 100.0,
            "high_price": 105.0,
            "low_price": 95.0,
            "close_price": 102.0,
            "volume": 10000.0,
            "turnover": 1020000.0,
        },
        {
            "date_ms": 1704240000000,  # 2024-01-03
            "open_price": 102.0,
            "high_price": 107.0,
            "low_price": 100.0,
            "close_price": 106.0,
            "volume": 12000.0,
            "turnover": 1272000.0,
        },
    ]

    provider = FuyaoDataProvider(prefer_local=False)

    with patch("fuyao_client.prices_historical", return_value=mock_items) as mock_hist:
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-02", end="2024-01-03")
        mock_hist.assert_called_once()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df["Close"].iloc[0] == 102.0


def test_fuyao_provider_index_and_etf_mocked():
    provider = FuyaoDataProvider(prefer_local=False)

    # Index routing
    mock_index_items = [
        {
            "date_ms": 1704153600000,
            "open_price": 3000.0,
            "high_price": 3050.0,
            "low_price": 2980.0,
            "close_price": 3020.0,
            "volume": 50000.0,
            "turnover": 5000000.0,
        }
    ]
    with patch("fuyao_client.index_prices_historical", return_value=mock_index_items) as mock_index:
        df = provider.fetch_ohlcv("000300.SH", start="2024-01-02", end="2024-01-02")
        mock_index.assert_called_once()
        assert len(df) == 1
        assert df["Close"].iloc[0] == 3020.0

    # ETF routing
    mock_etf_res = {
        "item": [
            {
                "date_ms": 1704153600000,
                "open_price": 3.5,
                "high_price": 3.6,
                "low_price": 3.4,
                "close_price": 3.55,
                "volume": 100000.0,
                "turnover": 355000.0,
            }
        ]
    }
    with patch("fuyao_client.fund_market_historical", return_value=mock_etf_res) as mock_fund:
        df = provider.fetch_ohlcv("510300.SH", start="2024-01-02", end="2024-01-02")
        mock_fund.assert_called_once()
        assert len(df) == 1
        assert df["Close"].iloc[0] == 3.55


def test_fuyao_provider_fetch_metadata_mocked():
    provider = FuyaoDataProvider(prefer_local=False)

    mock_val = {"item": [{"pe_ttm": 25.0, "pb_mrq": 8.0}]}
    mock_ind = {
        "abilities": [
            {
                "ability": "profitability",
                "indicators": [{"index_id": "calculate_roe", "value": "28.5"}],
            },
            {
                "ability": "growth",
                "indicators": [{"index_id": "net_profit_yoy_growth_ratio", "value": "15.2"}],
            },
            {
                "ability": "solvency",
                "indicators": [{"index_id": "asset_liability_ratio", "value": "20.0"}],
            },
        ]
    }

    with patch("fuyao_client.a_share_valuations_snapshot", return_value=mock_val), patch(
        "fuyao_client.financials_indicators", return_value=mock_ind
    ):
        meta = provider.fetch_metadata("600519.SH")
        assert meta["roe"] == 28.5
        assert meta["earnings_growth"] == 15.2
        # asset_liability_ratio 20% -> debt_to_equity = 0.2 / 0.8 = 0.25
        assert pytest.approx(meta["debt_to_equity"], rel=1e-3) == 0.25


def test_provider_registration_and_caching(tmp_path):
    # Test registered names
    p_marketdb = get_data_provider("marketdb")
    assert isinstance(p_marketdb, MarketDBDataProvider)

    p_fuyao = get_data_provider("fuyao")
    assert isinstance(p_fuyao, FuyaoDataProvider)

    p_fin = get_data_provider("financial_api")
    assert isinstance(p_fin, FuyaoDataProvider)

    # Test CachedDataProvider wrapping
    cached = CachedDataProvider(p_marketdb, cache_dir=str(tmp_path))
    df = cached.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-05")
    assert not df.empty

    cache_file = tmp_path / "MarketDBDataProvider_600519.SH_1d_2024-01-01_2024-01-05.csv"
    assert cache_file.exists()

"""Unit tests for common/financial_api.py (MarketDB and Fuyao Data Providers).

All tests run 100% offline without network access or live market data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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
    EastMoneyDataProvider,
    FuyaoDataProvider,
    MarketDBDataProvider,
    _resolve_duckdb_path,
    is_etf_code,
    normalize_thscode,
)
from common.duckdb_cache import MissingAdjustFactorsError, canonicalize_adjust_mode
import common.financial_api as financial_api


def test_is_etf_code():
    assert is_etf_code("510300.SH")
    assert is_etf_code("560010.SH")
    assert is_etf_code("588000.SH")
    assert is_etf_code("159919.SZ")
    assert is_etf_code("161725.SZ")
    assert not is_etf_code("600519.SH")
    assert not is_etf_code("000001.SZ")
    assert not is_etf_code("SPY")


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

    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.return_value = mock_items

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-02", end="2024-01-03")
        mock_fuyao.prices_historical.assert_called_once()
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
    mock_fuyao = MagicMock()
    mock_fuyao.index_prices_historical.return_value = mock_index_items
    mock_fuyao.fund_market_historical.return_value = mock_etf_res

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        df = provider.fetch_ohlcv("000300.SH", start="2024-01-02", end="2024-01-02")
        mock_fuyao.index_prices_historical.assert_called_once()
        assert len(df) == 1
        assert df["Close"].iloc[0] == 3020.0

        df = provider.fetch_ohlcv("510300.SH", start="2024-01-02", end="2024-01-02")
        mock_fuyao.fund_market_historical.assert_called_once()
        assert len(df) == 1
        assert df["Close"].iloc[0] == 3.55


def test_fuyao_provider_index_clamps_old_start_date():
    """Verify FuyaoDataProvider clamps start date for indices older than 5 years with a warning."""
    import warnings

    provider = FuyaoDataProvider(prefer_local=False)
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
    mock_fuyao = MagicMock()
    mock_fuyao.index_prices_historical.return_value = mock_index_items

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}), patch(
        "common.financial_api.BaoStockDataProvider.fetch_ohlcv", return_value=pd.DataFrame()
    ):
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            df = provider.fetch_ohlcv("000300.SH", start="2015-01-01", end="2024-01-02")

        mock_fuyao.index_prices_historical.assert_called_once()
        called_args, called_kwargs = mock_fuyao.index_prices_historical.call_args
        # Verify the start_ms passed to index_prices_historical was clamped (greater than 2015-01-01 ms)
        requested_start_ms = int(pd.to_datetime("2015-01-01").timestamp() * 1000)
        assert called_kwargs["start_ms"] > requested_start_ms
        assert len(df) == 1

        # Check warning message
        clamp_warnings = [w for w in recorded_warnings if "rolling history" in str(w.message)]
        assert len(clamp_warnings) == 1
        assert "000300.SH" in str(clamp_warnings[0].message)


def test_fuyao_provider_etf_clamps_old_start_date():
    """Verify FuyaoDataProvider clamps start date for ETFs older than 5 years with a warning,
    and calls fund_market_historical (not falling through to prices_historical)."""
    import warnings

    provider = FuyaoDataProvider(prefer_local=False)
    mock_etf_res = {
        "item": [
            {
                "date_ms": 1704153600000,
                "open_price": 1.0,
                "high_price": 1.1,
                "low_price": 0.9,
                "close_price": 1.05,
                "volume": 100000.0,
                "turnover": 105000.0,
            }
        ]
    }
    mock_fuyao = MagicMock()
    mock_fuyao.fund_market_historical.return_value = mock_etf_res

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            df = provider.fetch_ohlcv("588000.SH", start="2015-01-01", end="2024-01-02")

        mock_fuyao.fund_market_historical.assert_called_once()
        mock_fuyao.prices_historical.assert_not_called()
        called_args, called_kwargs = mock_fuyao.fund_market_historical.call_args
        requested_start_ms = int(pd.to_datetime("2015-01-01").timestamp() * 1000)
        assert called_kwargs["start_ms"] > requested_start_ms
        assert len(df) == 1
        assert df["Close"].iloc[0] == 1.05

        clamp_warnings = [w for w in recorded_warnings if "rolling history" in str(w.message)]
        assert len(clamp_warnings) == 1
        assert "588000.SH" in str(clamp_warnings[0].message)




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

    mock_fuyao = MagicMock()
    mock_fuyao.a_share_valuations_snapshot.return_value = mock_val
    mock_fuyao.financials_indicators.return_value = mock_ind

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        meta = provider.fetch_metadata("600519.SH")
        assert meta["roe"] == 28.5
        assert meta["earnings_growth"] == 15.2
        # asset_liability_ratio 20% -> debt_to_equity = 0.2 / 0.8 = 0.25
        assert pytest.approx(meta["debt_to_equity"], rel=1e-3) == 0.25


def test_provider_registration_and_caching(tmp_path, in_memory_marketdb):
    # Test registered names in registry
    from common.data import _PROVIDER_REGISTRY
    assert _PROVIDER_REGISTRY["marketdb"] is MarketDBDataProvider
    assert _PROVIDER_REGISTRY["fuyao"] is FuyaoDataProvider
    assert _PROVIDER_REGISTRY["financial_api"] is FuyaoDataProvider

    p_fuyao = get_data_provider("fuyao")
    assert isinstance(p_fuyao, FuyaoDataProvider)

    p_fin = get_data_provider("financial_api")
    assert isinstance(p_fin, FuyaoDataProvider)

    # Test CachedDataProvider wrapping using in-memory provider (100% offline, no real disk/network access)
    cached = CachedDataProvider(in_memory_marketdb, cache_dir=str(tmp_path))
    df = cached.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-05")
    cache_file = tmp_path / "cache.duckdb"
    assert cache_file.exists()
    # Second fetch is served from DuckDB cache
    df2 = cached.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-05")
    pd.testing.assert_frame_equal(df, df2)


def test_fuyao_provider_downloads_latest_data_when_marketdb_stale():
    """Verify FuyaoDataProvider downloads incremental latest data from Fuyao API
    when local MarketDB is stale.
    """
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    # Local DuckDB only has 2024-01-01 and 2024-01-02
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 100.0, 95.0, 105.0, 101.0, 1000.0)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-02', 101.0, 96.0, 106.0, 102.0, 1100.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    # Fuyao REST API returns incremental bars starting from overlap date 2024-01-02 up to 2024-01-04
    mock_items = [
        {
            "date_ms": 1704153600000,  # 2024-01-02
            "open_price": 101.0,
            "high_price": 106.0,
            "low_price": 96.0,
            "close_price": 102.0,
            "volume": 1100.0,
        },
        {
            "date_ms": 1704240000000,  # 2024-01-03
            "open_price": 102.0,
            "high_price": 107.0,
            "low_price": 100.0,
            "close_price": 105.0,
            "volume": 1200.0,
        },
        {
            "date_ms": 1704326400000,  # 2024-01-04
            "open_price": 105.0,
            "high_price": 110.0,
            "low_price": 104.0,
            "close_price": 108.0,
            "volume": 1500.0,
        },
    ]

    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.return_value = mock_items

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-04")
        mock_fuyao.prices_historical.assert_called_once()
        assert len(df) == 4
        assert df.index[0] == pd.Timestamp("2024-01-01")
        assert df.index[-1] == pd.Timestamp("2024-01-04")
        assert df.loc[pd.Timestamp("2024-01-04"), "Close"] == 108.0

    con.close()


def test_fuyao_provider_warns_when_query_date_newer_than_retrieved_latest_date():
    """Verify a UserWarning is emitted when query date is newer than retrieved data range latest date
    and trading days occurred in between.
    """
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    # Local DuckDB only has up to Thursday 2024-01-04
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-04', 100.0, 95.0, 105.0, 101.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_fuyao = MagicMock()
    # API has no newer data (returns empty items or only up to 2024-01-04)
    mock_fuyao.prices_historical.return_value = [
        {"date_ms": 1704326400000, "open_price": 100.0, "high_price": 105.0, "low_price": 95.0, "close_price": 101.0, "volume": 1000.0}
    ]

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        # Query date is Wednesday 2024-01-10; intervening weekdays (Friday Jan 5, Mon Jan 8, Tue Jan 9, Wed Jan 10) are trading days
        with pytest.warns(UserWarning, match="newer than retrieved data range latest date"):
            df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-10")
            assert df.index[-1] == pd.Timestamp("2024-01-04")

    con.close()


def test_fuyao_provider_suppresses_warning_on_weekends_and_holidays():
    """Verify warning is suppressed when query date falls on a weekend or holiday with no missed trading days."""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    # Friday 2024-01-05 bar
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-05', 100.0, 95.0, 105.0, 101.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.return_value = [
        {"date_ms": 1704412800000, "open_price": 100.0, "high_price": 105.0, "low_price": 95.0, "close_price": 101.0, "volume": 1000.0}
    ]

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        # Case 1: Querying on Sunday 2024-01-07 when latest bar is Friday 2024-01-05
        # Intervening days are Saturday Jan 6 and Sunday Jan 7 -> NO warning must be emitted
        import warnings
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-07")
            assert df.index[-1] == pd.Timestamp("2024-01-05")
            lag_warnings = [w for w in record if "newer than retrieved data range latest date" in str(w.message)]
            assert len(lag_warnings) == 0, f"Expected 0 warnings for weekend query, got: {lag_warnings}"

        # Case 2: Querying across a statutory holiday (mocked trading calendar without the holiday)
        # Friday 2023-12-29 retrieved, query is Monday 2024-01-01 (New Year holiday)
        con.execute("DELETE FROM v_daily_qfq")
        con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2023-12-29', 100.0, 95.0, 105.0, 101.0, 1000.0)")
        # Calendar only has 2023-12-29 and 2024-01-02 (2024-01-01 is a market holiday)
        mock_fuyao.calendar_trading_days.return_value = [
            {"date_ms": int(pd.Timestamp("2023-12-29", tz="Asia/Shanghai").timestamp() * 1000)},
            {"date_ms": int(pd.Timestamp("2024-01-02", tz="Asia/Shanghai").timestamp() * 1000)},
        ]
        # Clear module calendar cache for clean test
        import common.financial_api as fa
        fa._TRADING_DAYS_CACHE = None

        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            df = provider.fetch_ohlcv("600519.SH", start="2023-12-01", end="2024-01-01")
            lag_warnings = [w for w in record if "newer than retrieved data range latest date" in str(w.message)]
            assert len(lag_warnings) == 0, f"Expected 0 warnings for holiday query, got: {lag_warnings}"

    con.close()


def test_fuyao_provider_rescales_on_corporate_action():
    """Verify historical bars are properly re-scaled when a corporate action (e.g. split)
    changed the forward adjustment factor between the local snapshot and remote API.
    """
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    # Local data prior to 1-for-2 split: 2024-01-01 Close=100.0, 2024-01-02 Close=100.0
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 98.0, 96.0, 102.0, 100.0, 1000.0)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-02', 100.0, 98.0, 104.0, 100.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    # Remote API returns after split: Close on 2024-01-02 is adjusted to 50.0 (scale = 0.5)
    mock_items = [
        {
            "date_ms": 1704153600000,  # 2024-01-02
            "open_price": 50.0,
            "high_price": 52.0,
            "low_price": 49.0,
            "close_price": 50.0,
            "volume": 2000.0,
        },
        {
            "date_ms": 1704240000000,  # 2024-01-03
            "open_price": 51.0,
            "high_price": 53.0,
            "low_price": 50.0,
            "close_price": 52.0,
            "volume": 2200.0,
        },
    ]
    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.return_value = mock_items

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-03")
        assert len(df) == 3
        # 2024-01-01 historical Close was rescaled from 100.0 * 0.5 = 50.0
        assert df.loc[pd.Timestamp("2024-01-01"), "Close"] == 50.0
        assert df.loc[pd.Timestamp("2024-01-02"), "Close"] == 50.0
        assert df.loc[pd.Timestamp("2024-01-03"), "Close"] == 52.0

    con.close()


def test_fuyao_provider_fetch_universe_incremental_sync():
    """Verify fetch_universe incrementally fetches latest data for stale symbols
    and loads missing symbols fully.
    """
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 100.0, 95.0, 105.0, 101.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_items_stock = [
        {"date_ms": 1704067200000, "open_price": 100.0, "high_price": 105.0, "low_price": 95.0, "close_price": 101.0, "volume": 1000.0},
        {"date_ms": 1704153600000, "open_price": 101.0, "high_price": 106.0, "low_price": 98.0, "close_price": 103.0, "volume": 1100.0},
    ]
    mock_items_index = [
        {"date_ms": 1704067200000, "open_price": 3000.0, "high_price": 3050.0, "low_price": 2980.0, "close_price": 3020.0, "volume": 50000.0},
        {"date_ms": 1704153600000, "open_price": 3020.0, "high_price": 3060.0, "low_price": 3000.0, "close_price": 3040.0, "volume": 52000.0},
    ]

    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.return_value = mock_items_stock
    mock_fuyao.index_prices_historical.return_value = mock_items_index

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}), patch(
        "common.financial_api.BaoStockDataProvider.fetch_ohlcv", return_value=pd.DataFrame()
    ), patch(
        "common.financial_api.EastMoneyDataProvider.fetch_ohlcv", return_value=pd.DataFrame()
    ):
        uni = provider.fetch_universe(["600519.SH", "000300.SH"], start="2024-01-01", end="2024-01-02")
        assert "600519.SH" in uni
        assert "000300.SH" in uni
        assert len(uni["600519.SH"]) == 2
        assert len(uni["000300.SH"]) == 2

    con.close()


def test_fuyao_provider_graceful_fallback_when_api_fails():
    """Verify FuyaoDataProvider warns and gracefully falls back to local data if Fuyao API errors."""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 100.0, 95.0, 105.0, 101.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_fuyao = MagicMock()
    mock_fuyao.prices_historical.side_effect = RuntimeError("Network connection error")

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        with pytest.warns(UserWarning, match="Failed to fetch latest data"):
            df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-05")
            assert len(df) == 1
            assert df.index[0] == pd.Timestamp("2024-01-01")

    con.close()


def test_fuyao_provider_local_already_fresh_skips_api():
    """Verify FuyaoDataProvider skips calling the remote API when local MarketDB already covers target."""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-01', 100.0, 95.0, 105.0, 101.0, 1000.0)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-02', 101.0, 96.0, 106.0, 102.0, 1100.0)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-03', 102.0, 97.0, 107.0, 103.0, 1200.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_fuyao = MagicMock()

    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-02")
        mock_fuyao.prices_historical.assert_not_called()
        assert len(df) == 2

    con.close()


def _make_mock_bs_result(fields, data):
    mock_rs = MagicMock()
    mock_rs.error_code = "0"
    mock_rs.fields = fields
    mock_rs.data = data
    mock_rs.cur_row_num = 0
    rows = list(data)
    idx = [-1]

    def _next():
        idx[0] += 1
        return idx[0] < len(rows)

    def _get_row():
        return rows[idx[0]]

    mock_rs.next = _next
    mock_rs.get_row_data = _get_row
    return mock_rs


def test_baostock_fetch_financial_indicators(tmp_path):
    """Verify BaoStockDataProvider.fetch_financial_indicators queries all 6 endpoints,
    populates FundamentalMetrics, and caches into DuckDBCache.
    """
    from common.financial_api import BaoStockDataProvider

    mock_bs = MagicMock()
    mock_lg = MagicMock(error_code="0")
    mock_bs.login.return_value = mock_lg

    # 1. Industry
    mock_bs.query_stock_industry.return_value = _make_mock_bs_result(
        ["updateDate", "code", "code_name", "industry", "industryClassification"],
        [["2023-12-31", "sh.600519", "贵州茅台", "食品饮料", "申万一级"]],
    )
    # 2. Profit
    mock_bs.query_profit_data.return_value = _make_mock_bs_result(
        ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
        [["sh.600519", "2024-04-03", "2023-12-31", "34.12", "50.1", "91.8", "74734000000.0", "59.49", "147694000000.0", "1256197800", "1256197800"]],
    )
    # 3. Growth
    mock_bs.query_growth_data.return_value = _make_mock_bs_result(
        ["code", "pubDate", "statDate", "YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"],
        [["sh.600519", "2024-04-03", "2023-12-31", "17.8", "16.5", "18.2", "18.2", "18.2"]],
    )
    # 4. Balance
    mock_bs.query_balance_data.return_value = _make_mock_bs_result(
        ["code", "pubDate", "statDate", "currentRatio", "quickRatio", "cashRatio", "YOYLiability", "liabilityToAsset", "assetToEquity"],
        [["sh.600519", "2024-04-03", "2023-12-31", "3.5", "3.2", "3.0", "12.0", "18.5", "1.22"]],
    )
    # 5. Cash flow
    mock_bs.query_cash_flow_data.return_value = _make_mock_bs_result(
        ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset", "tangibleAssetToAsset", "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr"],
        [["sh.600519", "2024-04-03", "2023-12-31", "0.7", "0.3", "0.9", "100.0", "0.45", "0.89", "0.45"]],
    )
    # 6. Dividend
    mock_bs.query_dividend_data.return_value = _make_mock_bs_result(
        ["code", "diviPretaxPerShare", "diviPercent"],
        [["sh.600519", "30.876", "51.9"]],
    )
    # 7. K-data
    mock_bs.query_history_k_data_plus.return_value = _make_mock_bs_result(
        ["date", "close", "peTTM", "pbMRQ"],
        [["2023-12-29", "1726.0", "28.5", "8.2"]],
    )

    provider = BaoStockDataProvider(cache_dir=str(tmp_path))
    provider._get_bs = MagicMock(return_value=mock_bs)

    metrics = provider.fetch_financial_indicators("600519.SH", as_of_date="2023-12-31")

    assert metrics.symbol == "600519.SH"
    assert metrics.report_year == 2023
    assert metrics.stat_date == "2023-12-31"
    assert metrics.roe == pytest.approx(0.3412)
    assert metrics.net_profit == pytest.approx(74734000000.0)
    assert metrics.cfo_to_net_profit == pytest.approx(0.89)
    assert metrics.cfo == pytest.approx(74734000000.0 * 0.89)
    assert metrics.industry_name == "食品饮料"
    assert metrics.industry_tier == "tier2_consumer"
    assert metrics.terminal_pe == 20.0
    assert metrics.is_blacklisted is False
    assert metrics.pe_ttm == pytest.approx(28.5)
    assert metrics.pb == pytest.approx(8.2)
    assert metrics.total_shares == pytest.approx(1256197800.0)
    assert metrics.market_cap == pytest.approx(1726.0 * 1256197800.0)
    assert metrics.dividend_payout_ratio == pytest.approx(0.519)
    assert metrics.dividend_yield == pytest.approx(30.876 / 1726.0)

    # Verify query cache hit on second call
    mock_bs.query_profit_data.reset_mock()
    cached_metrics = provider.fetch_financial_indicators("600519.SH", as_of_date="2023-12-31")
    assert cached_metrics.symbol == "600519.SH"
    assert cached_metrics.roe == pytest.approx(0.3412)
    mock_bs.query_profit_data.assert_not_called()


def test_baostock_fetch_financial_indicators_error_graceful(tmp_path):
    """Verify BaoStockDataProvider handles login failure or exceptions gracefully without raising."""
    from common.financial_api import BaoStockDataProvider

    mock_bs = MagicMock()
    mock_bs.login.return_value = MagicMock(error_code="-1", error_msg="connection refused")

    # Explicit cache_dir: the no-cache_dir fallback would open the production
    # cache read-write, which `DuckDBCache`'s pytest guard (correctly) refuses.
    provider = BaoStockDataProvider(cache_dir=str(tmp_path))
    provider._get_bs = MagicMock(return_value=mock_bs)

    metrics = provider.fetch_financial_indicators("600519.SH", as_of_date="2023-12-31")
    assert metrics.symbol == "600519.SH"
    assert metrics.report_year == 2023
    assert np.isnan(metrics.roe)
    assert np.isnan(metrics.net_profit)


def test_calendar_check_missing_trading_days_and_skip_fetch():
    """Verify check_missing_trading_days accurately detects missing trading days
    and FuyaoDataProvider skips incremental remote fetching when no trading days are missing.
    """
    import datetime
    from common.financial_api import check_missing_trading_days

    # Friday to Sunday (no missing trading days)
    friday = datetime.date(2024, 1, 5)
    sunday = datetime.date(2024, 1, 7)
    assert check_missing_trading_days(friday, sunday) == []

    # Friday to next Tuesday (Monday and Tuesday are missing trading days)
    tuesday = datetime.date(2024, 1, 9)
    missing = check_missing_trading_days(friday, tuesday)
    assert datetime.date(2024, 1, 8) in missing
    assert datetime.date(2024, 1, 9) in missing

    # Test FuyaoDataProvider skips remote fetch when no missing trading days
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE v_daily_qfq (thscode VARCHAR, date DATE, open DOUBLE, low DOUBLE, high DOUBLE, close DOUBLE, volume DOUBLE)")
    con.execute("INSERT INTO v_daily_qfq VALUES ('600519.SH', '2024-01-05', 100.0, 95.0, 105.0, 101.0, 1000.0)")

    provider = FuyaoDataProvider(prefer_local=True)
    provider._local_provider = MarketDBDataProvider(con=con)

    mock_fuyao = MagicMock()
    with patch.dict(sys.modules, {"fuyao_client": mock_fuyao}):
        # Query on Sunday when latest local is Friday: skips remote fetch!
        df = provider.fetch_ohlcv("600519.SH", start="2024-01-01", end="2024-01-07")
        mock_fuyao.prices_historical.assert_not_called()
        assert df.index[-1] == pd.Timestamp("2024-01-05")

        # fetch_universe on Sunday when latest local is Friday: also skips remote fetch!
        uni = provider.fetch_universe(["600519.SH"], start="2024-01-01", end="2024-01-07")
        mock_fuyao.prices_historical.assert_not_called()
        assert uni["600519.SH"].index[-1] == pd.Timestamp("2024-01-05")

    con.close()

# ---------------------------------------------------------------------------------------------
# M3.8 factor sourcing: `FuyaoDataProvider.fetch_adjust_factors` is a PURE DELEGATION to the local
# MarketDB factor table, `EastMoneyDataProvider` deliberately has none.
#
# The synthetic series below uses exact arithmetic (no quote rounding) so the derivation-chain gate
# can be `<= 1e-6`: two corporate actions (cash dividend -> 2-for-1 split) with a raw path that is
# continuous across each ex-date, and the matching dense factor truth `F` = [1.00, 1.05, 2.10].
# ---------------------------------------------------------------------------------------------


def _synthetic_raw_qfq_and_factor_truth(periods: int = 48):
    """Raw bars, native qfq bars and the dense `calc_adjust_factor_daily` truth behind them."""
    dates = pd.DatetimeIndex(
        pd.bdate_range("2020-01-02", periods=periods), name="date"
    )
    factor = np.concatenate([np.full(16, 1.00), np.full(16, 1.05), np.full(16, 2.10)])
    raw_close = 20.0 + 0.05 * np.arange(periods, dtype=float)
    raw_close[16:] = raw_close[16:] / 1.05
    raw_close[32:] = raw_close[32:] / 2.00
    qfq_close = raw_close * factor / factor[-1]  # 前复权 = raw x F_t / F_last

    def _frame(close: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Open": close * 0.999,
                "High": close * 1.010,
                "Low": close * 0.990,
                "Close": close,
                "Volume": np.full(len(close), 1000.0),
            },
            index=dates.copy(),
        )

    factors = pd.DataFrame(
        {"factor_qfq": factor / factor[-1], "factor_hfq": factor}, index=dates.copy()
    )
    factors.index.name = "date"
    return dates, _frame(raw_close), _frame(qfq_close), factors


def _slice_bars(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    out = frame
    if start:
        out = out[out.index >= pd.to_datetime(start)]
    if end:
        out = out[out.index <= pd.to_datetime(end)]
    return out.copy()


class _FakeLocalMarketDB:
    """Stand-in for `FuyaoDataProvider._local_provider`, with call counting."""

    def __init__(self, factors: pd.DataFrame, fail: bool = False):
        self.factors = factors
        self.fail = fail
        self.calls = []

    def fetch_adjust_factors(self, symbol, start=None, end=None):
        self.calls.append((symbol, start, end))
        if self.fail:
            raise RuntimeError("market.duckdb went away")
        return _slice_bars(self.factors, start, end)


class _FuyaoStub(FuyaoDataProvider):
    """Fuyao provider whose REST calls are a locally-served synthetic pair (no network)."""

    def __init__(self, raw_df, qfq_df, local=None):
        super().__init__(prefer_local=False)  # never reaches a real market.duckdb
        self._raw_df = raw_df
        self._qfq_df = qfq_df
        self.basis_calls = []
        if local is not None:
            self._local_provider = local
            # Mirror `FuyaoDataProvider.__init__`'s dynamic capability flag.
            self.adjust_factor_source_available = hasattr(local, "fetch_adjust_factors")

    def _fetch_ohlcv_internal(self, symbol, start, end, interval="1d", adjust=None):
        self.basis_calls.append(canonicalize_adjust_mode(adjust))
        src = self._qfq_df if canonicalize_adjust_mode(adjust) == "qfq" else self._raw_df
        return _slice_bars(src, start, end)


class _EastMoneyStub(EastMoneyDataProvider):
    """EastMoney provider over the same synthetic pair, via the per-call `fqt` argument."""

    def __init__(self, raw_df, qfq_df):
        super().__init__()  # builds an offline requests.Session only
        self._raw_df = raw_df
        self._qfq_df = qfq_df

    def _fetch_ohlcv_internal(self, symbol, start, end, interval="1d", fqt=None):
        assert interval == "1d"
        return _slice_bars(self._qfq_df if fqt == "1" else self._raw_df, start, end)


def test_fuyao_fetch_adjust_factors_delegates_once_to_local_marketdb_without_upstream_calls():
    dates, raw_df, qfq_df, factors = _synthetic_raw_qfq_and_factor_truth()
    local = _FakeLocalMarketDB(factors)
    stub = _FuyaoStub(raw_df, qfq_df, local=local)

    out = stub.fetch_adjust_factors("600519.SH", start="2020-01-02", end="2020-03-10")

    # Exactly one delegation, and NOT a single upstream bar request.
    assert local.calls == [("600519.SH", "2020-01-02", "2020-03-10")]
    assert stub.basis_calls == []
    # Verbatim pass-through of MarketDB's DENSE two-column frame (not compressed into event rows).
    pd.testing.assert_frame_equal(out, factors)
    assert list(out.columns) == ["factor_qfq", "factor_hfq"]
    assert out.index.name == "date"
    assert len(out) == len(dates)


def test_fuyao_fetch_adjust_factors_factor_neutral_symbols_return_empty_frame():
    dates, raw_df, qfq_df, factors = _synthetic_raw_qfq_and_factor_truth()
    local = _FakeLocalMarketDB(factors)
    stub = _FuyaoStub(raw_df, qfq_df, local=local)

    for symbol in ("000300.SH", "511880.SH", "886042.TI"):
        out = stub.fetch_adjust_factors(symbol, start="2020-01-02", end="2020-03-10")
        assert out.empty
        assert list(out.columns) == ["factor_hfq"]
        assert isinstance(out.index, pd.DatetimeIndex)
        assert out.index.name == "date"

    # Factor-neutral short-circuits before delegation and before any upstream request.
    assert local.calls == []
    assert stub.basis_calls == []


def test_fuyao_without_local_factor_table_degrades_to_the_legacy_direct_path(tmp_path):
    """C.1: no local factor table is a CAPABILITY GAP -> legacy direct path, not a hard failure."""
    import common.data as data_module

    dates, raw_df, qfq_df, _ = _synthetic_raw_qfq_and_factor_truth()
    stub = _FuyaoStub(raw_df, qfq_df)  # prefer_local=False -> _local_provider is None
    assert stub._local_provider is None
    assert stub.adjust_factor_source_available is False

    financial_api._NO_LOCAL_FACTOR_TABLE_WARNED.discard("FuyaoDataProvider")
    data_module._FACTOR_CAPABILITY_GAP_WARNED.discard("_FuyaoStub")

    cached = CachedDataProvider(_FuyaoStub(raw_df, qfq_df), cache_dir=str(tmp_path))
    start, end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        direct = cached.fetch_ohlcv("600519.SH", start=start, end=end, adjust="qfq")

        # Direct calls still report the degraded state, once per provider ...
        first = stub.fetch_adjust_factors("600519.SH", start=start, end=end)
        second = stub.fetch_adjust_factors("000001.SZ", start=start, end=end)
        # ... and a second cache MISS (another symbol) must NOT repeat the capability gap warning.
        direct_again = cached.fetch_ohlcv("000001.SZ", start=start, end=end, adjust="qfq")

    # The legacy path served the provider's OWN series: no MissingAdjustFactorsError, no rescaling.
    assert not direct.empty
    assert list(direct["Close"]) == list(qfq_df["Close"])
    assert list(direct_again["Close"]) == list(qfq_df["Close"])
    # Stored with the provider's native basis, and NO factor table was fabricated.
    meta = cached.duckdb_cache.get_asset_metadata("_FuyaoStub", "600519.SH")
    assert meta["adjustflag"] == "qfq"
    assert cached.duckdb_cache.query_adjust_factors("600519.SH").empty

    for frame in (first, second):
        assert frame.empty
        assert list(frame.columns) == ["factor_hfq"]
        assert frame.index.name == "date"

    messages = [str(w.message) for w in caught]
    degraded = [m for m in messages if "has no local adjustment-factor table reachable" in m]
    assert len(degraded) == 1, f"the degraded notice must fire once per provider: {messages}"
    capability_gap = [m for m in messages if "cannot supply adjustment factors" in m]
    assert len(capability_gap) == 1, f"the cache must warn once, not per miss: {messages}"
    # No factor is invented: the upstream raw/qfq pair was never needed.
    assert stub.basis_calls == []


def test_fuyao_delegated_factors_reconcile_with_native_adjustments_through_cache(tmp_path):
    """Dense delegated factors -> raw bars in DuckDB -> read-time qfq/hfq derivation gate."""
    dates, raw_df, qfq_df, factors = _synthetic_raw_qfq_and_factor_truth()
    stub = _FuyaoStub(raw_df, qfq_df, local=_FakeLocalMarketDB(factors))
    cached = CachedDataProvider(stub, cache_dir=str(tmp_path))
    start, end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")

    derived_qfq = cached.fetch_ohlcv("600519.SH", start=start, end=end, adjust="qfq")
    derived_hfq = cached.fetch_ohlcv("600519.SH", start=start, end=end, adjust="hfq")

    assert list(derived_qfq.index) == list(dates)
    assert list(derived_hfq.index) == list(dates)

    # 1. qfq: raw * factor_hfq[t] / factor_hfq[latest] must reproduce the provider's native qfq.
    for col in ("Open", "High", "Low", "Close"):
        rel = (derived_qfq[col] - qfq_df[col]).abs() / qfq_df[col].abs()
        assert rel.max() <= 1e-6, f"qfq {col} max relative error {rel.max()}"
    np.testing.assert_array_equal(
        derived_qfq["Volume"].to_numpy(), raw_df["Volume"].to_numpy(),
        err_msg="Volume must never be scaled by an adjustment factor",
    )

    # 2. hfq: raw * factor_hfq[t].
    expected_hfq = raw_df.mul(factors["factor_hfq"], axis=0)
    for col in ("Open", "High", "Low", "Close"):
        rel = (derived_hfq[col] - expected_hfq[col]).abs() / expected_hfq[col].abs()
        assert rel.max() <= 1e-6, f"hfq {col} max relative error {rel.max()}"
    np.testing.assert_array_equal(
        derived_hfq["Volume"].to_numpy(), raw_df["Volume"].to_numpy()
    )

    # 3. The factors really were persisted (so the derivation above was read-time, not pass-through).
    stored = cached.duckdb_cache.query_adjust_factors("600519.SH")
    assert not stored.empty
    assert stored["factor_hfq"].iloc[-1] == pytest.approx(2.10, rel=1e-12)


def test_fuyao_delegated_factor_read_failure_is_fail_closed(tmp_path):
    dates, raw_df, qfq_df, factors = _synthetic_raw_qfq_and_factor_truth()
    local = _FakeLocalMarketDB(factors, fail=True)
    stub = _FuyaoStub(raw_df, qfq_df, local=local)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = stub.fetch_adjust_factors("600519.SH", start="2020-01-02", end="2020-03-10")
        cached = CachedDataProvider(_FuyaoStub(raw_df, qfq_df, local=local), cache_dir=str(tmp_path))
        with pytest.raises(MissingAdjustFactorsError):
            cached.fetch_ohlcv(
                "600519.SH",
                start=dates[0].strftime("%Y-%m-%d"),
                end=dates[-1].strftime("%Y-%m-%d"),
            )

    assert out.empty
    assert list(out.columns) == ["factor_hfq"]
    messages = [str(w.message) for w in caught]
    assert any("local MarketDB adjustment-factor read failed" in m for m in messages), messages
    assert stub.basis_calls == [], "a factor-read failure must not trigger an upstream fetch"
    assert local.calls, "the delegation must actually have been attempted"


def test_eastmoney_deliberately_has_no_adjust_factor_source(tmp_path):
    """Documented trade-off: EastMoney keeps the legacy fallback rather than inventing factors."""
    import common.data as data_module

    assert not hasattr(EastMoneyDataProvider, "fetch_adjust_factors")

    dates, raw_df, qfq_df, _ = _synthetic_raw_qfq_and_factor_truth()
    stub = _EastMoneyStub(raw_df, qfq_df)
    cached = CachedDataProvider(stub, cache_dir=str(tmp_path))
    start, end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")

    data_module._FACTOR_CAPABILITY_GAP_WARNED.discard("EastMoneyDataProvider")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = cached.fetch_ohlcv("600519.SH", start=start, end=end, adjust="qfq")

    messages = [str(w.message) for w in caught]
    assert any("exposes fetch_raw_ohlcv but cannot supply adjustment factors" in m for m in messages), messages
    # The legacy direct path stores the provider's own native basis and derives nothing.
    assert list(df["Close"]) == list(qfq_df["Close"])
    assert cached.duckdb_cache.query_adjust_factors("600519.SH").empty


def test_fuyao_adjust_factor_source_flag_tracks_the_local_table(tmp_path):
    """C.2: `adjust_factor_source_available` is DYNAMIC -- decided by whether MarketDB opened."""
    dates, raw_df, qfq_df, factors = _synthetic_raw_qfq_and_factor_truth()

    # (a) no market.duckdb in the folder -> no factor source, flag False.
    empty_dir = tmp_path / "no_db"
    empty_dir.mkdir()
    no_source = FuyaoDataProvider(folder_path=str(empty_dir))
    assert no_source._local_provider is None
    assert no_source.adjust_factor_source_available is False

    # (b) a db_path pointing at a non-existent file behaves the same way.
    assert FuyaoDataProvider(db_path=str(tmp_path / "nope.duckdb")).adjust_factor_source_available is False

    # (c) a real local market.duckdb -> factor source present, flag True, delegation active.
    db_dir = tmp_path / "with_db"
    db_dir.mkdir()
    db_file = db_dir / "market.duckdb"
    con = duckdb.connect(str(db_file))
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE
        );
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_qfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM raw_kline_daily;
        CREATE TABLE calc_adjust_factor_daily (
            thscode VARCHAR, date DATE, forward_factor DOUBLE, backward_factor DOUBLE
        );
    """)
    for d, (qfq_f, hfq_f) in zip(dates, zip(factors["factor_qfq"], factors["factor_hfq"])):
        con.execute(
            "INSERT INTO calc_adjust_factor_daily VALUES ('600519.SH', ?, ?, ?)",
            [d.strftime("%Y-%m-%d"), float(qfq_f), float(hfq_f)],
        )
    con.close()

    # NB `adjust` must stay in MarketDB's own vocabulary (forward/backward/none): passing "qfq"
    # makes `MarketDBDataProvider.__init__` raise, `_local_provider` stays None, and the flag
    # correctly reads False (a pre-existing quirk of that constructor's validation).
    with_source = FuyaoDataProvider(folder_path=str(db_dir))
    assert with_source.adjust_factor_source_available is True
    delegated = with_source.fetch_adjust_factors("600519.SH", start=dates[0].strftime("%Y-%m-%d"),
                                                end=dates[-1].strftime("%Y-%m-%d"))
    assert list(delegated.columns) == ["factor_qfq", "factor_hfq"]
    assert len(delegated) == len(dates)
    np.testing.assert_allclose(
        delegated["factor_hfq"].to_numpy(), factors["factor_hfq"].to_numpy(), rtol=1e-12
    )
    np.testing.assert_allclose(
        delegated["factor_qfq"].to_numpy(), factors["factor_qfq"].to_numpy(), rtol=1e-12
    )

    # And through the cache: raw stored + factors derived (the flag=True path).
    cached = CachedDataProvider(with_source, cache_dir=str(tmp_path / "cache"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        derived = cached.fetch_ohlcv(
            "600519.SH", start=dates[0].strftime("%Y-%m-%d"), end=dates[-1].strftime("%Y-%m-%d"),
            adjust="qfq",
        )
    messages = [str(w.message) for w in caught]
    assert not any("cannot supply adjustment factors" in m for m in messages), messages
    assert not derived.empty


def test_marketdb_fallback_relabels_factor_qfq_but_keeps_factor_hfq():
    """C.3: no `calc_adjust_factor_daily` -> derived `factor_qfq == factor_hfq / factor_hfq[-1]`."""
    dates = pd.bdate_range("2024-01-01", periods=4)
    raw_close = np.array([100.0, 101.0, 51.0, 52.0])
    # Cumulative backward factor 1.0 for the first two bars, 2.0 after the (2-for-1) action.
    hfq_close = raw_close * np.array([1.0, 1.0, 2.0, 2.0])

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE
        );
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_qfq AS SELECT * FROM raw_kline_daily;
    """)  # note: NO calc_adjust_factor_daily -> exercises the calibration fallback
    for i, d in enumerate(dates):
        con.execute(
            "INSERT INTO raw_kline_daily VALUES ('600519.SH', ?, ?, ?, ?, ?, 1000.0)",
            [d.strftime("%Y-%m-%d"), float(raw_close[i]), float(raw_close[i]),
             float(raw_close[i]), float(raw_close[i])],
        )
    # v_daily is the raw view; make `adjust="backward"` return the hfq series by overriding the
    # provider view selection rather than the SQL (one view can only hold one series).
    provider = MarketDBDataProvider(con=con, adjust="none")
    provider.view = "v_daily"
    con.execute("UPDATE raw_kline_daily SET close = ? WHERE date = ?", [51.0, dates[2].strftime("%Y-%m-%d")])

    import common.financial_api as fa

    original = fa.MarketDBDataProvider

    class _SplitViewProvider(original):
        """`adjust="backward"` reads a different column set than `adjust="none"` here."""

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            df = super().fetch_ohlcv(symbol, start, end, interval=interval)
            if self.adjust == "backward":
                mask = df.index >= pd.Timestamp(dates[2])
                df = df.copy()
                df.loc[mask, ["Open", "High", "Low", "Close"]] = (
                    df.loc[mask, ["Open", "High", "Low", "Close"]] * 2.0
                )
            return df

    try:
        fa.MarketDBDataProvider = _SplitViewProvider
        factors = provider.fetch_adjust_factors("600519.SH", start="2024-01-01", end="2024-01-04")
    finally:
        fa.MarketDBDataProvider = original
    con.close()

    assert list(factors.columns) == ["factor_qfq", "factor_hfq"]
    expected_hfq = np.array([1.0, 1.0, 2.0, 2.0])
    # factor_hfq is untouched by the relabelling fix.
    np.testing.assert_allclose(factors["factor_hfq"].to_numpy(), expected_hfq, rtol=1e-9)
    # factor_qfq now obeys fore = back / back_last (instead of repeating the hfq factor).
    np.testing.assert_allclose(
        factors["factor_qfq"].to_numpy() * factors["factor_hfq"].iloc[-1],
        factors["factor_hfq"].to_numpy(),
        rtol=1e-9,
    )
    np.testing.assert_allclose(factors["factor_qfq"].to_numpy(), [0.5, 0.5, 1.0, 1.0], rtol=1e-9)
    assert factors["factor_qfq"].iloc[-1] == pytest.approx(1.0, rel=1e-12)


def _mem_marketdb_without_factor_table(dates, raw_close, hfq_close):
    """In-memory market.duckdb with NO `calc_adjust_factor_daily` -> exercises the fallback.

    Two physical tables so `v_daily` (raw) and `v_daily_hfq` (backward-adjusted) can hold
    different closes without patching the provider class.
    """
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE
        );
        CREATE TABLE hfq_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE
        );
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM hfq_kline_daily;
        CREATE VIEW v_daily_qfq AS SELECT * FROM hfq_kline_daily;
    """)
    for i, d in enumerate(dates):
        for table, close in (("raw_kline_daily", raw_close), ("hfq_kline_daily", hfq_close)):
            con.execute(
                f"INSERT INTO {table} VALUES ('600519.SH', ?, ?, ?, ?, ?, 1000.0)",
                [d.strftime("%Y-%m-%d"), float(close[i]), float(close[i]),
                 float(close[i]), float(close[i])],
            )
    return con


def test_marketdb_fallback_with_degenerate_raw_close_stays_finite_and_storable():
    """C.4: a 0 / NaN raw close in the source must not leak inf/NaN factors or raise.

    Upstream `_drop_invalid_ohlcv_rows` (called by `fetch_ohlcv`) already removes such bars, so the
    fallback keeps its pre-existing shape: no exception, finite factors, storable as-is.
    """
    dates = pd.bdate_range("2024-01-01", periods=4)
    raw_close = np.array([100.0, 0.0, np.nan, 52.0])
    hfq_close = np.array([100.0, 0.0, np.nan, 104.0])
    con = _mem_marketdb_without_factor_table(dates, raw_close, hfq_close)
    provider = MarketDBDataProvider(con=con, adjust="none")
    try:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            factors = provider.fetch_adjust_factors(
                "600519.SH", start="2024-01-01", end="2024-01-04"
            )
    finally:
        con.close()

    assert list(factors.columns) == ["factor_qfq", "factor_hfq"]
    assert factors.index.name == "date"
    # The two degenerate bars are gone; what is left is finite in BOTH columns.
    assert len(factors) == 2
    for col in ("factor_qfq", "factor_hfq"):
        assert np.isfinite(factors[col].to_numpy()).all(), (col, factors[col].tolist())
    np.testing.assert_allclose(factors["factor_hfq"].to_numpy(), [1.0, 2.0], rtol=1e-12)
    np.testing.assert_allclose(factors["factor_qfq"].to_numpy(), [0.5, 1.0], rtol=1e-12)

    # And the cache accepts the frame verbatim.
    from common.duckdb_cache import DuckDBCache

    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors("600519.SH", factors)
    stored = cache.query_adjust_factors("600519.SH")
    assert len(stored) == 2
    assert np.isfinite(pd.to_numeric(stored["factor_hfq"], errors="coerce").to_numpy()).all()


def test_marketdb_fallback_does_not_call_calibrate_helper(monkeypatch):
    """C.5: the fallback constructs the factors itself -- the retired helper must never run."""
    dates = pd.bdate_range("2024-01-01", periods=4)
    raw_close = np.array([100.0, 101.0, 51.0, 52.0])
    hfq_close = raw_close * np.array([1.0, 1.0, 2.0, 2.0])
    con = _mem_marketdb_without_factor_table(dates, raw_close, hfq_close)

    def _sentinel(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError(
            "MarketDBDataProvider.fetch_adjust_factors must not call "
            "calibrate_adjust_factors_from_closes"
        )

    monkeypatch.setattr(financial_api, "calibrate_adjust_factors_from_closes", _sentinel)
    provider = MarketDBDataProvider(con=con, adjust="none")
    try:
        factors = provider.fetch_adjust_factors("600519.SH", start="2024-01-01", end="2024-01-04")
    finally:
        con.close()

    assert not factors.empty
    np.testing.assert_allclose(factors["factor_hfq"].to_numpy(), [1.0, 1.0, 2.0, 2.0], rtol=1e-12)
    np.testing.assert_allclose(factors["factor_qfq"].to_numpy(), [0.5, 0.5, 1.0, 1.0], rtol=1e-12)

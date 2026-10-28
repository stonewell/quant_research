"""Offline tests for adjust factor derivation (M3.6 & M3.8).

Validates:
- Dynamic in-memory derivation of QFQ/HFQ for both daily bars and 5m intraday bars.
- Raw bars stored once (with Volume and Amount), deriving target adjust口径 on read.
- Hard precision gate: max(|P_derived - P_direct| / P_direct) <= 1e-6 across all price columns.
- Physical volume & amount invariance: Volume_derived == Volume_raw == Volume_direct, Amount unscaled.
- Edge assets (511880.SH, 000300.SH) factor = 1.0.
- Calibrate helper and provider fetch_adjust_factors integration.
"""

import datetime
import os
import sys
import warnings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import numpy as np
import pandas as pd
import pytest

from common.data import CachedDataProvider, BaseDataProvider
from common.duckdb_cache import (
    DuckDBCache,
    MissingAdjustFactorsError,
    canonicalize_adjust_mode,
    is_factor_neutral_symbol,
)
from common.financial_api import (
    calibrate_adjust_factors_from_closes,
    MarketDBDataProvider,
    BaoStockDataProvider,
)


@pytest.fixture
def mem_cache():
    """In-memory DuckDBCache for 100% offline testing."""
    return DuckDBCache(db_path=":memory:")


def test_calibrate_adjust_factors_from_closes_exact_math():
    """Tests calibrate_adjust_factors_from_closes mathematical precision and recovery."""
    dates = pd.bdate_range("2024-01-01", periods=5)
    # Simulate stock split 10-for-5 on day 3: pre-split raw is 100, post-split raw is 50.
    # Direct QFQ: pre-split is 50, post-split is 50.
    raw_close = pd.Series([100.0, 102.0, 51.0, 52.0, 53.0], index=dates)
    qfq_close = pd.Series([50.0, 51.0, 51.0, 52.0, 53.0], index=dates)

    factors = calibrate_adjust_factors_from_closes(raw_close, qfq_close)
    assert list(factors.columns) == ["factor_qfq", "factor_hfq"]
    assert len(factors) == 5

    # Verify factor values
    np.testing.assert_allclose(factors["factor_qfq"].values, [0.5, 0.5, 1.0, 1.0, 1.0], rtol=1e-12)
    # Reconstructed QFQ close from raw * factor_qfq
    reconstructed_qfq = raw_close * factors["factor_qfq"]
    rel_dev = (reconstructed_qfq - qfq_close).abs() / qfq_close
    assert (rel_dev <= 1e-6).all(), f"Max relative deviation: {rel_dev.max()}"


def test_daily_bars_hard_gate_precision_and_volume_invariance(mem_cache):
    """Hard gate verification for daily bars:
    - max(|P_derived - P_direct| / P_direct) <= 1e-6
    - Volume_derived == Volume_raw == Volume_direct
    - Amount unscaled
    """
    dates = pd.bdate_range("2024-01-01", periods=60)
    np.random.seed(42)

    # Synthetic stock: split 10-for-5 at bar 30
    raw_prices = np.zeros((60, 4))
    base = 100.0
    for i in range(60):
        if i == 30:
            base = base / 2.0  # split
        shock = np.random.normal(0, 0.5)
        base = max(base + shock, 10.0)
        o = base
        h = base + abs(np.random.normal(0, 0.5)) + 0.1
        l = base - abs(np.random.normal(0, 0.5)) - 0.1
        c = base + np.random.normal(0, 0.2)
        raw_prices[i] = [o, h, l, c]

    volumes = np.random.uniform(1000, 50000, size=60)
    amounts = raw_prices[:, 3] * volumes  # close * volume

    raw_df = pd.DataFrame(
        {
            "Open": raw_prices[:, 0],
            "High": raw_prices[:, 1],
            "Low": raw_prices[:, 2],
            "Close": raw_prices[:, 3],
            "Volume": volumes,
            "Amount": amounts,
        },
        index=dates,
    )

    # Pre-split factor is 0.5, post-split is 1.0
    true_factors = np.ones(60)
    true_factors[:30] = 0.5

    # Direct QFQ dataframe: price scaled, volume & amount identical
    direct_qfq = raw_df.copy()
    for col in ["Open", "High", "Low", "Close"]:
        direct_qfq[col] = direct_qfq[col] * true_factors

    # Store raw bars into DuckDB
    mem_cache.store_bars("TestProvider", "600519.SH", raw_df, adjustflag="raw")

    # Store factors into DuckDB
    factors_df = pd.DataFrame(
        {"factor_qfq": true_factors, "factor_hfq": true_factors * 2.0}, index=dates
    )
    mem_cache.store_adjust_factors("600519.SH", factors_df)

    # Query derived QFQ bars (M3.6 `columns=` contract: `Amount` is opt-in)
    derived_qfq = mem_cache.query_bars(
        "TestProvider", "600519.SH", adjust="qfq", columns="all"
    )

    # Hard gate checks
    for col in ["Open", "High", "Low", "Close"]:
        p_derived = derived_qfq[col].values
        p_direct = direct_qfq[col].values
        rel_diff = np.abs(p_derived - p_direct) / p_direct
        max_rel_diff = np.max(rel_diff)
        assert max_rel_diff <= 1e-6, f"{col} max relative diff {max_rel_diff:.2e} exceeded 1e-6"

    # Volume and Amount invariance
    np.testing.assert_array_equal(derived_qfq["Volume"].values, raw_df["Volume"].values)
    np.testing.assert_array_equal(derived_qfq["Volume"].values, direct_qfq["Volume"].values)
    np.testing.assert_array_equal(derived_qfq["Amount"].values, raw_df["Amount"].values)
    np.testing.assert_array_equal(derived_qfq["Amount"].values, direct_qfq["Amount"].values)


def test_5m_intraday_bars_hard_gate_precision_and_alignment(mem_cache):
    """Hard gate verification for 5m intraday bars:
    - 5m bars on day t are strictly scaled by day t's daily factor
    - max(|P_derived - P_direct| / P_direct) <= 1e-6
    - Volume and Amount are completely unscaled
    """
    days = pd.bdate_range("2024-01-02", periods=3)
    # 4 bars per day for testing
    ts_list = []
    for d in days:
        for t_str in ["09:35:00", "10:00:00", "14:00:00", "15:00:00"]:
            ts_list.append(pd.Timestamp(f"{d.strftime('%Y-%m-%d')} {t_str}"))

    np.random.seed(123)
    n_bars = len(ts_list)
    raw_close = np.linspace(100.0, 110.0, n_bars)
    raw_df = pd.DataFrame(
        {
            "Open": raw_close - 0.5,
            "High": raw_close + 0.8,
            "Low": raw_close - 0.8,
            "Close": raw_close,
            "Volume": np.arange(1, n_bars + 1) * 500.0,
            "Amount": np.arange(1, n_bars + 1) * 50000.0,
        },
        index=ts_list,
    )

    # Ex-dividend on day 2 (index 1 in days)
    # day 0: factor 0.8, day 1: factor 1.0, day 2: factor 1.0
    daily_factors = {
        days[0].date(): 0.8,
        days[1].date(): 1.0,
        days[2].date(): 1.0,
    }

    factors_df = pd.DataFrame(
        {
            "factor_qfq": [daily_factors[d.date()] for d in days],
            "factor_hfq": [1.0, 1.25, 1.25],
        },
        index=days,
    )

    mem_cache.store_intraday_bars(
        "TestProvider", "000001.SZ", raw_df, interval="5m", adjustflag="3"
    )
    mem_cache.store_adjust_factors("000001.SZ", factors_df)

    derived_5m = mem_cache.query_intraday_bars(
        "TestProvider", "000001.SZ", interval="5m", adjustflag="qfq", columns="all"
    )

    # Direct 5m computation: each bar scaled by its day's factor
    direct_5m = raw_df.copy()
    bar_day_factors = np.array([daily_factors[pd.Timestamp(ts).date()] for ts in ts_list])
    for col in ["Open", "High", "Low", "Close"]:
        direct_5m[col] = direct_5m[col] * bar_day_factors

    # Verify precision
    for col in ["Open", "High", "Low", "Close"]:
        p_derived = derived_5m[col].values
        p_direct = direct_5m[col].values
        rel_diff = np.abs(p_derived - p_direct) / p_direct
        assert np.max(rel_diff) <= 1e-6, f"{col} 5m relative error > 1e-6"

    # Verify Volume and Amount invariance
    np.testing.assert_array_equal(derived_5m["Volume"].values, raw_df["Volume"].values)
    np.testing.assert_array_equal(derived_5m["Amount"].values, raw_df["Amount"].values)


def test_edge_assets_neutral_factor_in_derivation(mem_cache):
    """M3.8 edge assets: 511880.SH (cash ETF) and 000300.SH (index) have neutral factor 1.0."""
    dates = pd.bdate_range("2024-01-01", periods=5)
    for sym in ["511880.SH", "000300.SH"]:
        df = pd.DataFrame(
            {
                "Open": [100.0] * 5,
                "High": [101.0] * 5,
                "Low": [99.0] * 5,
                "Close": [100.5] * 5,
                "Volume": [1000.0] * 5,
                "Amount": [100500.0] * 5,
            },
            index=dates,
        )
        mem_cache.store_bars("TestProvider", sym, df)

        # Query with adjust="qfq" without prior adjust_factors stored: defaults to 1.0
        res_qfq = mem_cache.query_bars("TestProvider", sym, adjust="qfq", columns="all")
        np.testing.assert_array_equal(res_qfq["Close"].values, df["Close"].values)
        np.testing.assert_array_equal(res_qfq["Volume"].values, df["Volume"].values)
        np.testing.assert_array_equal(res_qfq["Amount"].values, df["Amount"].values)


def test_marketdb_fetch_adjust_factors_integration():
    """Tests MarketDBDataProvider.fetch_adjust_factors reading calc_adjust_factor_daily."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_kline_daily (
            thscode VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
        );
        CREATE VIEW v_daily_qfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily_hfq AS SELECT * FROM raw_kline_daily;
        CREATE VIEW v_daily AS SELECT * FROM raw_kline_daily;

        CREATE TABLE calc_adjust_factor_daily (
            thscode VARCHAR,
            date DATE,
            forward_factor DOUBLE,
            backward_factor DOUBLE
        );
    """)

    dates = pd.bdate_range("2024-01-01", periods=3)
    for d in dates:
        d_str = d.strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO calc_adjust_factor_daily VALUES ('600519.SH', ?, 0.95, 1.05)",
            [d_str],
        )

    provider = MarketDBDataProvider(con=con)

    # 1. Standard stock
    f_stock = provider.fetch_adjust_factors("600519.SH", start="2024-01-01", end="2024-01-03")
    assert len(f_stock) == 3
    assert (f_stock["factor_qfq"] == 0.95).all()
    assert (f_stock["factor_hfq"] == 1.05).all()

    # 2. Index symbol
    f_idx = provider.fetch_adjust_factors("000300.SH", start="2024-01-01", end="2024-01-03")
    assert (f_idx["factor_qfq"] == 1.0).all()

    # 3. Cash ETF
    f_cash = provider.fetch_adjust_factors("511880.SH", start="2024-01-01", end="2024-01-03")
    assert (f_cash["factor_qfq"] == 1.0).all()

    con.close()


def test_cached_data_provider_transparent_derivation(tmp_path):
    """Tests CachedDataProvider.get_bars and fetch_ohlcv transparent adjust derivation."""
    class MockStockProvider(BaseDataProvider):
        def __init__(self):
            self.dates = pd.bdate_range("2024-01-01", periods=4)

        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
            return pd.DataFrame(
                {
                    "Open": [100.0, 102.0, 52.0, 54.0],
                    "High": [105.0, 106.0, 55.0, 56.0],
                    "Low": [98.0, 100.0, 50.0, 52.0],
                    "Close": [101.0, 104.0, 53.0, 55.0],
                    "Volume": [1000.0, 1200.0, 2500.0, 2400.0],
                    "Amount": [101000.0, 124800.0, 132500.0, 132000.0],
                },
                index=self.dates,
            )

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return self.fetch_raw_ohlcv(symbol, start, end, interval=interval)

        def fetch_adjust_factors(self, symbol, start, end):
            return pd.DataFrame(
                {
                    "factor_qfq": [0.5, 0.5, 1.0, 1.0],
                    "factor_hfq": [1.0, 1.0, 2.0, 2.0],
                },
                index=self.dates,
            )

    cache_file = tmp_path / "cache.duckdb"
    inner = MockStockProvider()
    cached = CachedDataProvider(inner, cache_dir=str(cache_file))

    # Fetch with adjust="qfq" (`columns="all"` keeps the opt-in `Amount` column)
    bars_qfq = cached.get_bars(
        "600000.SH", "2024-01-01", "2024-01-04", adjust="qfq", columns="all"
    )
    assert bars_qfq["Close"].iloc[0] == 50.5
    assert bars_qfq["Close"].iloc[1] == 52.0
    assert bars_qfq["Close"].iloc[2] == 53.0
    assert bars_qfq["Close"].iloc[3] == 55.0

    # Fetch with adjust="none" (raw) from cache directly
    bars_raw = cached.get_bars(
        "600000.SH", "2024-01-01", "2024-01-04", adjust="none", columns="all"
    )
    assert bars_raw["Close"].iloc[0] == 101.0
    assert bars_raw["Close"].iloc[2] == 53.0

    # Volume and Amount are untouched
    np.testing.assert_array_equal(bars_qfq["Volume"], bars_raw["Volume"])
    np.testing.assert_array_equal(bars_qfq["Amount"], bars_raw["Amount"])


# =============================================================================
# Requirement 11: Dedicated Tests for (a) through (i)
# =============================================================================


def test_req11_a_miss_path_fake_inner_no_double_adjust(tmp_path):
    """(a) Miss path with fake inner provider (fetch_ohlcv=QFQ, fetch_raw_ohlcv=raw) -> no double adjust."""
    class FakeInnerProvider(BaseDataProvider):
        def __init__(self):
            self.dates = pd.bdate_range("2024-01-01", periods=2)

        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
            # Raw price is 100.0
            return pd.DataFrame({
                "Open": [100.0, 100.0],
                "High": [105.0, 105.0],
                "Low": [95.0, 95.0],
                "Close": [100.0, 100.0],
                "Volume": [1000.0, 1000.0],
                "Amount": [100000.0, 100000.0],
            }, index=self.dates)

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            # Direct QFQ already scaled by 0.5 -> 50.0
            return pd.DataFrame({
                "Open": [50.0, 50.0],
                "High": [52.5, 52.5],
                "Low": [47.5, 47.5],
                "Close": [50.0, 50.0],
                "Volume": [1000.0, 1000.0],
                "Amount": [100000.0, 100000.0],
            }, index=self.dates)

        def fetch_adjust_factors(self, symbol, start, end):
            # QFQ factor is 0.5 (hfq: 1.0, 2.0 -> latest is 2.0 -> 1.0/2.0 = 0.5)
            return pd.DataFrame({
                "factor_hfq": [1.0, 2.0],
            }, index=self.dates)

    cache_file = tmp_path / "cache_no_double.duckdb"
    inner = FakeInnerProvider()
    cached = CachedDataProvider(inner, cache_dir=str(cache_file))

    # Query with adjust="qfq"
    bars = cached.get_bars("600519.SH", "2024-01-01", "2024-01-02", adjust="qfq")

    # If raw (100.0) is stored and scaled by factor (0.5), Close == 50.0.
    # If double adjustment occurred (fetch_ohlcv 50.0 * 0.5), Close would be 25.0!
    assert bars["Close"].iloc[0] == 50.0
    assert bars["Close"].iloc[0] != 25.0


def test_req11_b_legacy_provider_without_fetch_raw_ohlcv(tmp_path):
    """(b) Provider without fetch_raw_ohlcv -> native basis stored, no factors applied."""
    class LegacyProvider(BaseDataProvider):
        def __init__(self):
            self.dates = pd.bdate_range("2024-01-01", periods=2)
            self.adjustflag = "2"  # native QFQ

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return pd.DataFrame({
                "Open": [50.0, 50.0],
                "High": [52.0, 52.0],
                "Low": [48.0, 48.0],
                "Close": [50.0, 50.0],
                "Volume": [1000.0, 1000.0],
                "Amount": [50000.0, 50000.0],
            }, index=self.dates)

    cache_file = tmp_path / "cache_legacy.duckdb"
    inner = LegacyProvider()
    assert not hasattr(inner, "fetch_raw_ohlcv")
    cached = CachedDataProvider(inner, cache_dir=str(cache_file))

    # Query with adjust="qfq"
    bars = cached.get_bars("600000.SH", "2024-01-01", "2024-01-02", adjust="qfq")
    # Returns native data as-is without factor transformation
    assert bars["Close"].iloc[0] == 50.0

    # Querying raw when stored is qfq should raise ValueError
    with pytest.raises(ValueError, match="stored data has basis 'qfq'"):
        cached.duckdb_cache.query_bars(type(inner).__name__, "600000.SH", adjust="raw")


def test_req11_c_qfq_normalization_uses_whole_table_latest_factor(mem_cache):
    """(c) QFQ normalization uses whole-table latest factor even when query window ends earlier."""
    all_dates = pd.bdate_range("2024-01-01", periods=10)
    raw_df = pd.DataFrame({
        "Open": [100.0] * 10,
        "High": [105.0] * 10,
        "Low": [95.0] * 10,
        "Close": [100.0] * 10,
        "Volume": [1000.0] * 10,
        "Amount": [100000.0] * 10,
    }, index=all_dates)
    mem_cache.store_bars("TestProv", "600519.SH", raw_df, adjustflag="raw")

    # Factors: day 0 has hfq=1.0, day 4 has hfq=2.0, day 9 has hfq=4.0
    factor_dates = [all_dates[0], all_dates[4], all_dates[9]]
    factors_df = pd.DataFrame({
        "factor_hfq": [1.0, 2.0, 4.0],
    }, index=factor_dates)
    mem_cache.store_adjust_factors("600519.SH", factors_df)

    # Whole-table latest factor is 4.0 (on day 9).
    assert mem_cache.get_latest_factor_hfq("600519.SH") == 4.0

    # Query earlier slice [day 0, day 3] where latest factor in slice would be 1.0
    early_end = all_dates[3].strftime("%Y-%m-%d")
    res_qfq = mem_cache.query_bars("TestProv", "600519.SH", start="2024-01-01", end=early_end, adjust="qfq")

    # Day 0 QFQ must be raw * (hfq[0] / hfq_latest) = 100.0 * (1.0 / 4.0) = 25.0
    # If slice-local normalization were mistakenly used, it would be 100.0 * (1.0 / 1.0) = 100.0.
    assert res_qfq["Close"].iloc[0] == 25.0


def test_req11_d_sparse_event_factors_asof_and_pre_first_event_1(mem_cache):
    """(d) Sparse event factors with as-of lookup and pre-first-event=1.0."""
    dates = pd.to_datetime(["2024-01-02", "2024-01-08", "2024-01-18"])
    raw_df = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0],
        "High": [100.0, 100.0, 100.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": [1000.0, 1000.0, 1000.0],
        "Amount": [100000.0, 100000.0, 100000.0],
    }, index=dates)
    mem_cache.store_bars("TestProv", "600519.SH", raw_df, adjustflag="raw")

    # Sparse event factors on 2024-01-05 (hfq=2.0) and 2024-01-15 (hfq=4.0)
    event_dates = pd.to_datetime(["2024-01-05", "2024-01-15"])
    events_df = pd.DataFrame({
        "factor_hfq": [2.0, 4.0],
    }, index=event_dates)
    mem_cache.store_adjust_factors("600519.SH", events_df)

    # HFQ derivation:
    # 2024-01-02 is before first event (2024-01-05) -> factor = 1.0 -> price = 100.0
    # 2024-01-08 is between events -> as-of factor = 2.0 -> price = 200.0
    # 2024-01-18 is after second event -> as-of factor = 4.0 -> price = 400.0
    res_hfq = mem_cache.query_bars("TestProv", "600519.SH", adjust="hfq")
    assert list(res_hfq["Close"]) == [100.0, 200.0, 400.0]


def test_req11_e_baostock_parsing_fake_twopage_real_field_names():
    """(e) BaoStock parsing with fake 2-page ResultData using real field names."""
    from unittest.mock import MagicMock, patch

    class FakeResultData:
        def __init__(self, pages):
            self.pages = pages
            self.page_idx = 0
            self.fields = [
                "code",
                "dividOperateDate",
                "foreAdjustFactor",
                "backAdjustFactor",
                "adjustFactor",
            ]
            self.data = self.pages[0] if self.pages else []
            self.cur_row_num = 0

        def next(self):
            if self.cur_row_num < len(self.data):
                return True
            self.page_idx += 1
            if self.page_idx < len(self.pages):
                self.data = self.pages[self.page_idx]
                self.cur_row_num = 0
                return True
            return False

        def get_row_data(self):
            row = self.data[self.cur_row_num]
            self.cur_row_num += 1
            return row

    page1 = [
        ["sh.600519", "2022-06-20", "0.80", "1.25", "1.0"],
        ["sh.600519", "2023-06-25", "0.64", "1.5625", "1.0"],
    ]
    page2 = [
        ["sh.600519", "2024-06-18", "0.50", "2.0", "1.0"],
    ]
    fake_rs = FakeResultData([page1, page2])

    provider = BaoStockDataProvider(adjustflag="3", cache=DuckDBCache(db_path=":memory:"))
    mock_lg = MagicMock(error_code="0")
    with patch("baostock.login", return_value=mock_lg), \
         patch("baostock.logout"), \
         patch("baostock.query_adjust_factor", return_value=fake_rs):
        factors = provider.fetch_adjust_factors("600519.SH", "2020-01-01", "2024-07-01")

    assert len(factors) == 3
    assert "factor_hfq" in factors.columns
    assert list(factors.index.strftime("%Y-%m-%d")) == ["2022-06-20", "2023-06-25", "2024-06-18"]
    assert list(factors["factor_hfq"]) == [1.25, 1.5625, 2.0]


def test_req11_f_missing_adjust_factors_error_raised(mem_cache, tmp_path):
    """(f) MissingAdjustFactorsError raised when raw bars stored and non-neutral symbol has no factors on qfq/hfq query."""
    dates = pd.bdate_range("2024-01-01", periods=3)
    raw_df = pd.DataFrame({
        "Open": [10.0, 11.0, 12.0],
        "High": [10.5, 11.5, 12.5],
        "Low": [9.5, 10.5, 11.5],
        "Close": [10.0, 11.0, 12.0],
        "Volume": [100.0, 200.0, 300.0],
        "Amount": [1000.0, 2200.0, 3600.0],
    }, index=dates)

    # Store raw bars without storing factors for non-neutral stock 600519.SH
    mem_cache.store_bars("TestProv", "600519.SH", raw_df, adjustflag="raw")

    # query_bars with qfq must raise MissingAdjustFactorsError
    with pytest.raises(MissingAdjustFactorsError, match="600519.SH"):
        mem_cache.query_bars("TestProv", "600519.SH", adjust="qfq")

    # query_bars with hfq must also raise MissingAdjustFactorsError
    with pytest.raises(MissingAdjustFactorsError, match="600519.SH"):
        mem_cache.query_bars("TestProv", "600519.SH", adjust="hfq")

    # query_bars with raw/none does NOT require factors and succeeds
    res_raw = mem_cache.query_bars("TestProv", "600519.SH", adjust="raw")
    assert len(res_raw) == 3

    # Also test CachedDataProvider fail-closed on cache miss when factor fetch fails:
    class FailingFactorProvider(BaseDataProvider):
        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
            return raw_df
        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return raw_df
        def fetch_adjust_factors(self, symbol, start, end):
            raise RuntimeError("Upstream factor timeout")

    cache_file = tmp_path / "cache_failing.duckdb"
    cached = CachedDataProvider(FailingFactorProvider(), cache_dir=str(cache_file))
    with pytest.raises(RuntimeError, match="Upstream factor timeout"):
        cached.get_bars("600519.SH", "2024-01-01", "2024-01-03", adjust="qfq")

    # Verify bars were NOT stored (fail-closed, store nothing)
    meta = cached.duckdb_cache.get_asset_metadata(type(FailingFactorProvider()).__name__, "600519.SH")
    assert meta is None


def test_req11_g_canonicalize_adjust_mode():
    """(g) Mode mapping: 1->hfq, 2->qfq, 3->raw, bad value->ValueError."""
    assert canonicalize_adjust_mode("1") == "hfq"
    assert canonicalize_adjust_mode("hfq") == "hfq"
    assert canonicalize_adjust_mode("backward") == "hfq"
    assert canonicalize_adjust_mode("back") == "hfq"

    assert canonicalize_adjust_mode("2") == "qfq"
    assert canonicalize_adjust_mode("qfq") == "qfq"
    assert canonicalize_adjust_mode("forward") == "qfq"
    assert canonicalize_adjust_mode("fore") == "qfq"

    assert canonicalize_adjust_mode("3") == "raw"
    assert canonicalize_adjust_mode("none") == "raw"
    assert canonicalize_adjust_mode("raw") == "raw"
    assert canonicalize_adjust_mode("unadjusted") == "raw"

    with pytest.raises(ValueError, match="Unknown or unsupported adjustment mode"):
        canonicalize_adjust_mode("invalid_mode")
    with pytest.raises(ValueError, match="Unknown or unsupported adjustment mode"):
        canonicalize_adjust_mode("4")


def test_req11_h_volume_amount_never_scaled(mem_cache):
    """(h) Volume and Amount never scaled under any adjustment."""
    dates = pd.bdate_range("2024-01-01", periods=3)
    raw_df = pd.DataFrame({
        "Open": [10.0, 11.0, 12.0],
        "High": [10.5, 11.5, 12.5],
        "Low": [9.5, 10.5, 11.5],
        "Close": [10.0, 11.0, 12.0],
        "Volume": [12345.0, 23456.0, 34567.0],
        "Amount": [123450.0, 234560.0, 345670.0],
    }, index=dates)
    mem_cache.store_bars("TestProv", "600519.SH", raw_df, adjustflag="raw")

    factors_df = pd.DataFrame({
        "factor_hfq": [1.0, 2.0, 4.0],
    }, index=dates)
    mem_cache.store_adjust_factors("600519.SH", factors_df)

    res_qfq = mem_cache.query_bars("TestProv", "600519.SH", adjust="qfq", columns="all")
    res_hfq = mem_cache.query_bars("TestProv", "600519.SH", adjust="hfq", columns="all")
    res_raw = mem_cache.query_bars("TestProv", "600519.SH", adjust="raw", columns="all")

    for res in [res_qfq, res_hfq, res_raw]:
        np.testing.assert_array_equal(res["Volume"].values, raw_df["Volume"].values)
        np.testing.assert_array_equal(res["Amount"].values, raw_df["Amount"].values)


def test_req11_i_basis_mismatch_raises_value_error(mem_cache):
    """(i) Basis-mismatch request raises ValueError."""
    dates = pd.bdate_range("2024-01-01", periods=2)
    df = pd.DataFrame({
        "Open": [50.0, 50.0],
        "High": [52.0, 52.0],
        "Low": [48.0, 48.0],
        "Close": [50.0, 50.0],
        "Volume": [1000.0, 1000.0],
    }, index=dates)

    # Store with adjustflag="qfq"
    mem_cache.store_bars("TestProv", "600000.SH", df, adjustflag="qfq")

    # Same basis: returns as-is
    res_qfq = mem_cache.query_bars("TestProv", "600000.SH", adjust="qfq")
    assert len(res_qfq) == 2

    # Mismatched basis: raw or hfq -> ValueError
    with pytest.raises(ValueError, match="Cannot derive 'raw' adjustment for 600000.SH"):
        mem_cache.query_bars("TestProv", "600000.SH", adjust="raw")

    with pytest.raises(ValueError, match="Cannot derive 'hfq' adjustment for 600000.SH"):
        mem_cache.query_bars("TestProv", "600000.SH", adjust="hfq")



# =============================================================================
# M3.6 review F3 / F5: provider capability gap, vectorised equivalence,
# and single-anchor / day-granularity consistency.
# =============================================================================


class _GapCapabilityProviderL236(BaseDataProvider):
    """`fetch_raw_ohlcv` WITHOUT `fetch_adjust_factors` -- the exact F3 capability gap.

    `FuyaoDataProvider` / `EastMoneyDataProvider` look like this: they can hand out raw bars but
    cannot supply the factors those raw bars would need to be re-based. The old code raised
    `MissingAdjustFactorsError` on every miss, which `fetch_universe` downgraded to
    warn-and-skip, i.e. a silently smaller universe.
    """

    adjustflag = "2"  # this provider's native basis IS qfq

    def __init__(self):
        self.dates = pd.bdate_range("2024-01-01", periods=4)
        self.calls = []

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        self.calls.append(("fetch_ohlcv", symbol, interval))
        return pd.DataFrame(
            {
                "Open": [50.0, 51.0, 52.0, 53.0],
                "High": [51.0, 52.0, 53.0, 54.0],
                "Low": [49.0, 50.0, 51.0, 52.0],
                "Close": [50.5, 51.5, 52.5, 53.5],
                "Volume": [1000.0, 1100.0, 1200.0, 1300.0],
            },
            index=self.dates,
        )

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
        raise AssertionError(
            "F3: a provider without fetch_adjust_factors must NOT be asked for raw bars -- "
            "nothing could derive the requested basis from them"
        )


def test_f3_provider_capability_gap_falls_back_to_native_basis(tmp_path):
    """DISCRIMINATING (F3): no factors + raw capability -> legacy direct path, says so ONCE."""
    cache_file = tmp_path / "gap.duckdb"
    inner = _GapCapabilityProviderL236()
    cached = CachedDataProvider(inner, cache_dir=str(cache_file))

    with pytest.warns(UserWarning, match="exposes fetch_raw_ohlcv but cannot supply adjustment factors"):
        bars = cached.get_bars("600000.SH", "2024-01-01", "2024-01-04", adjust="qfq")

    # Served from the provider's own (native QFQ) series, NOT rescaled by a missing factor.
    np.testing.assert_array_equal(bars["Close"].values, [50.5, 51.5, 52.5, 53.5])
    # The native basis was recorded, and no factor table was fabricated.
    meta = cached.duckdb_cache.get_asset_metadata("_GapCapabilityProviderL236", "600000.SH")
    assert meta["adjustflag"] == "qfq"
    assert cached.duckdb_cache.query_adjust_factors("600000.SH").empty
    # `fetch_raw_ohlcv` was never consulted (the double would have raised).
    assert all(call[0] == "fetch_ohlcv" for call in inner.calls)

    # Warn-once per provider type: a 22-symbol universe must not emit 22 copies.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cached.get_bars("600519.SH", "2024-01-01", "2024-01-04", adjust="qfq")
    assert [w for w in caught if "exposes fetch_raw_ohlcv" in str(w.message)] == []


class _EmptyFactorProviderL236(BaseDataProvider):
    """Has BOTH capabilities but the upstream factor feed is empty -> a genuine failure."""

    def __init__(self):
        self.dates = pd.bdate_range("2024-01-01", periods=2)

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
        return pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [1000.0, 1100.0],
            },
            index=self.dates,
        )

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval)

    def fetch_adjust_factors(self, symbol, start=None, end=None):
        return pd.DataFrame()


def test_f3_fetch_universe_does_not_swallow_basis_failures(tmp_path):
    """DISCRIMINATING (F3 + P2): a basis failure must NOT become a warning + a skipped symbol.

    `fetch_universe`'s generic `except Exception: warnings.warn(...); continue` is right for an
    upstream hiccup, but a `MissingAdjustFactorsError` (also `UnknownAdjustBasisError` /
    `AdjustBasisTransitionRefused`) means "this cache cannot be trusted for this symbol". Skipping
    it returns a smaller universe that downstream stages cannot tell apart from a smaller list --
    the 2026-10-23 incident shape. Fail closed.

    P2 addition: fail closed with ONE aggregated error naming EVERY affected symbol (the first
    failure chained as `__cause__`), not just the first one -- otherwise a 22-symbol book with 6
    poisoned rows needs six sequential runs to learn the whole fix list.
    """
    cache_file = tmp_path / "empty_factors.duckdb"
    cached = CachedDataProvider(_EmptyFactorProviderL236(), cache_dir=str(cache_file))

    with pytest.raises(RuntimeError, match="fetch_universe aborted") as excinfo:
        cached.fetch_universe(
            ["600000.SH", "600519.SH"], "2024-01-01", "2024-01-02", adjust="qfq"
        )
    message = str(excinfo.value)
    # BOTH symbols are reported by the single failing call...
    assert "600000.SH: MissingAdjustFactorsError" in message
    assert "600519.SH: MissingAdjustFactorsError" in message
    assert "2 of 2 symbol(s)" in message
    # ...and the first one is chained for traceback/`__cause__` inspection.
    assert isinstance(excinfo.value.__cause__, MissingAdjustFactorsError)


def test_p2_fetch_universe_basis_failure_aggregates_all_symbols(tmp_path):
    """Positive control for the P2 aggregation: a healthy symbol is not reported as a failure."""
    cache_file = tmp_path / "mixed_factors.duckdb"
    cached = CachedDataProvider(_EmptyFactorProviderL236(), cache_dir=str(cache_file))
    # 000300.SH is factor-neutral (index): exempt from the factor requirement, so it loads clean.
    with pytest.raises(RuntimeError) as excinfo:
        cached.fetch_universe(
            ["600000.SH", "000300.SH"], "2024-01-01", "2024-01-02", adjust="qfq"
        )
    message = str(excinfo.value)
    assert "1 of 2 symbol(s)" in message
    assert "000300.SH" not in message


def _reference_apply_factors(cache, frame, symbol: str, mode: str):
    """Pre-F5 implementation, verbatim in spirit: Python loop + `bisect` on `date()` objects."""
    import bisect

    hfq_latest = cache.get_latest_factor_hfq(symbol)
    max_bar_date = pd.Timestamp(frame.index.max()).date().strftime("%Y-%m-%d")
    fac = cache.query_adjust_factors(symbol, end=max_bar_date)
    event_dates = [pd.Timestamp(d).date() for d in fac.index]
    event_hfq = fac["factor_hfq"].values.astype(float)
    divisor = hfq_latest if (hfq_latest is not None and hfq_latest > 0) else 1.0

    out = frame.copy()
    for col in ["Open", "High", "Low", "Close"]:
        scaled = []
        for ts, price in zip(out.index, out[col].values):
            i = bisect.bisect_right(event_dates, pd.Timestamp(ts).date()) - 1
            hfq_t = 1.0 if i < 0 else float(event_hfq[i])
            mult = hfq_t if mode == "hfq" else hfq_t / divisor
            scaled.append(price * mult)
        out[col] = scaled
    return out


def test_apply_factors_vectorised_equivalence(mem_cache):
    """DISCRIMINATING (F5): `np.searchsorted` must be bit-for-bit the old per-bar `bisect` loop.

    The vectorised as-of join is on the hot read path (1.23M 5m bars), so it has to be provably
    identical -- including the "before the first factor -> 1.0" branch -- not merely close.
    """
    dates = pd.bdate_range("2023-01-02", periods=40)
    rng = np.random.default_rng(20261027)
    close = 100.0 + np.cumsum(rng.normal(0.0, 0.5, size=len(dates)))
    raw = pd.DataFrame(
        {
            "Open": close - 0.25,
            "High": close + 0.75,
            "Low": close - 0.75,
            "Close": close,
            "Volume": rng.integers(1_000, 9_000, size=len(dates)).astype(float),
        },
        index=dates,
    )
    mem_cache.store_bars("VecProv", "600519.VEC", raw, adjustflag="raw")

    # Factors: one BEFORE the first bar (1.0 branch), a few inside, each with an exact binary
    # fraction so a broadcast-vs-loop division difference would be a real signal, not noise.
    factor_dates = [dates[0] - pd.Timedelta(days=5)] + list(dates[[0, 7, 15, 23, 39]])
    factor_hfq = [1.0, 2.0, 1.5, 3.0, 2.25, 4.5]
    mem_cache.store_adjust_factors(
        "600519.VEC",
        pd.DataFrame({"factor_hfq": factor_hfq}, index=factor_dates),
    )

    for mode in ("hfq", "qfq"):
        got = mem_cache._apply_factors_to_df(raw, "600519.VEC", adjust_mode=mode)
        ref = _reference_apply_factors(mem_cache, raw, "600519.VEC", mode)
        for col in ["Open", "High", "Low", "Close"]:
            np.testing.assert_allclose(
                got[col].values, ref[col].values, rtol=0, atol=1e-12, err_msg=f"{mode}/{col}"
            )
        # Volume is never touched.
        np.testing.assert_array_equal(got["Volume"].values, raw["Volume"].values)


def test_apply_factors_single_anchor_across_windows(mem_cache):
    """DISCRIMINATING (F5): QFQ normalises on the WHOLE-TABLE latest factor, both windows.

    The dead `factors=` branch anchored on the last row of the passed frame instead, so an early
    window and a full-range read disagreed by a constant factor -- silently, and only for the
    window whose end fell before the last corporate action.
    """
    all_dates = pd.bdate_range("2024-01-01", periods=10)
    raw = pd.DataFrame(
        {
            "Open": [100.0] * 10,
            "High": [101.0] * 10,
            "Low": [99.0] * 10,
            "Close": [100.0] * 10,
            "Volume": [1000.0] * 10,
        },
        index=all_dates,
    )
    mem_cache.store_bars("AnchorProv", "600519.ANC", raw, adjustflag="raw")
    mem_cache.store_adjust_factors(
        "600519.ANC",
        pd.DataFrame(
            {"factor_hfq": [1.0, 2.0, 4.0]},
            index=[all_dates[0], all_dates[4], all_dates[9]],
        ),
    )
    assert mem_cache.get_latest_factor_hfq("600519.ANC") == 4.0

    full = mem_cache.query_bars("AnchorProv", "600519.ANC", adjust="qfq")
    early = mem_cache.query_bars(
        "AnchorProv",
        "600519.ANC",
        start=str(all_dates[0].date()),
        end=str(all_dates[3].date()),
        adjust="qfq",
    )

    # Same date -> same multiplier, regardless of which window asked.
    overlap = early.index
    np.testing.assert_allclose(
        early["Close"].values, full.loc[overlap, "Close"].values, rtol=0, atol=1e-12
    )
    # And the anchor really is the whole-table latest (4.0), not the window's own last (2.0).
    np.testing.assert_allclose(early["Close"].values, [25.0, 25.0, 25.0, 25.0], rtol=0, atol=1e-12)
    np.testing.assert_allclose(
        full["Close"].values, [25.0, 25.0, 25.0, 25.0, 50.0, 50.0, 50.0, 50.0, 50.0, 100.0],
        rtol=0, atol=1e-12,
    )


def test_5m_and_daily_overlap_scaling_factor_identical(mem_cache):
    """DISCRIMINATING (F5): 5m bars on day t use EXACTLY day t's daily multiplier.

    `_day_keys` normalises to the wall-clock date, which is what makes an intraday bar collide
    with its daily bar on the same factor anchor. If the key kept the time-of-day, a 5m bar would
    land on the wrong side of an ex-dividend boundary and be scaled by the neighbouring day's
    factor -- a silent percent-level error in the 5m backtest.
    """
    days = pd.bdate_range("2024-01-02", periods=3)
    daily_close = [100.0, 101.0, 102.0]
    daily = pd.DataFrame(
        {
            "Open": [c - 0.5 for c in daily_close],
            "High": [c + 0.5 for c in daily_close],
            "Low": [c - 0.5 for c in daily_close],
            "Close": daily_close,
            "Volume": [1000.0] * 3,
        },
        index=days,
    )
    mem_cache.store_bars("OverlapProv", "600519.OVL", daily, adjustflag="raw")

    intraday_ts = []
    intraday_close = []
    for day, close in zip(days, daily_close):
        for j, (hhmm, frac) in enumerate(
            [("09:35:00", 0.4), ("10:30:00", 0.7), ("14:00:00", 0.9), ("15:00:00", 1.0)]
        ):
            intraday_ts.append(pd.Timestamp(f"{day.strftime('%Y-%m-%d')} {hhmm}"))
            intraday_close.append(close * frac)
    intraday = pd.DataFrame(
        {
            "Open": [c - 0.1 for c in intraday_close],
            "High": [c + 0.1 for c in intraday_close],
            "Low": [c - 0.1 for c in intraday_close],
            "Close": intraday_close,
            "Volume": [100.0] * len(intraday_ts),
        },
        index=intraday_ts,
    )
    mem_cache.store_intraday_bars(
        "OverlapProv", "600519.OVL", intraday, interval="5m", adjustflag="raw"
    )

    # A corporate action between day 1 and day 2: hfq 2.0 -> 3.0, whole-table latest = 3.0.
    mem_cache.store_adjust_factors(
        "600519.OVL",
        pd.DataFrame({"factor_hfq": [2.0, 3.0]}, index=[days[0], days[1]]),
    )

    daily_qfq = mem_cache.query_bars("OverlapProv", "600519.OVL", adjust="qfq")
    intraday_qfq = mem_cache.query_intraday_bars(
        "OverlapProv", "600519.OVL", interval="5m", adjustflag="qfq"
    )

    daily_mult = (daily_qfq["Close"] / daily["Close"]).values
    np.testing.assert_allclose(daily_mult, [2.0 / 3.0, 1.0, 1.0], rtol=0, atol=1e-12)

    intraday_mult = (intraday_qfq["Close"] / intraday["Close"]).values
    expected = np.repeat(daily_mult, 4)
    np.testing.assert_allclose(intraday_mult, expected, rtol=0, atol=1e-12)


class _DualCapabilityProviderL236(BaseDataProvider):
    """Has raw bars AND factors: `derive_mode` decides which path is taken."""

    adjustflag = "2"  # native qfq

    def __init__(self):
        self.dates = pd.bdate_range("2024-01-01", periods=2)
        self.calls = []

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
        self.calls.append("fetch_raw_ohlcv")
        return pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [1000.0, 1100.0],
            },
            index=self.dates,
        )

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        self.calls.append("fetch_ohlcv")
        return pd.DataFrame(
            {
                "Open": [50.0, 50.5],
                "High": [50.5, 51.0],
                "Low": [49.5, 50.0],
                "Close": [50.25, 50.75],
                "Volume": [1000.0, 1100.0],
            },
            index=self.dates,
        )

    def fetch_adjust_factors(self, symbol, start=None, end=None):
        self.calls.append("fetch_adjust_factors")
        return pd.DataFrame(
            {"factor_qfq": [0.5, 0.5], "factor_hfq": [1.0, 1.0]}, index=self.dates
        )


def test_f4_derive_mode_direct_never_derives(tmp_path):
    """DISCRIMINATING (F4): `derive_mode="direct"` is the emergency fallback off the derivation path.

    A suspected factor-calibration defect needs a runtime switch that stops new raw bars and new
    factors from being written, so no read can be rescaled by a wrong factor. The switch must be
    validated eagerly (a typo must not silently mean "auto").
    """
    inner = _DualCapabilityProviderL236()

    # Eager validation: no silent fallback to the default.
    with pytest.raises(ValueError, match="derive_mode"):
        CachedDataProvider(inner, cache_dir=str(tmp_path / "bad.duckdb"), derive_mode="drect")

    cached = CachedDataProvider(
        inner, cache_dir=str(tmp_path / "direct.duckdb"), derive_mode="direct"
    )
    bars = cached.get_bars("600000.SH", "2024-01-01", "2024-01-02", adjust="qfq")

    # The provider's own series is served as-is (native qfq), no factor derivation happened.
    np.testing.assert_array_equal(bars["Close"].values, [50.25, 50.75])
    assert "fetch_raw_ohlcv" not in inner.calls
    assert "fetch_adjust_factors" not in inner.calls
    assert cached.duckdb_cache.query_adjust_factors("600000.SH").empty
    assert cached.duckdb_cache.get_asset_metadata(
        "_DualCapabilityProviderL236", "600000.SH"
    )["adjustflag"] == "qfq"

    # `auto` (the control) does take the raw+factor path, so the assertion above is meaningful.
    auto_inner = _DualCapabilityProviderL236()
    auto_cached = CachedDataProvider(auto_inner, cache_dir=str(tmp_path / "auto.duckdb"))
    auto_cached.get_bars("600000.SH", "2024-01-01", "2024-01-02", adjust="qfq")
    assert "fetch_raw_ohlcv" in auto_inner.calls
    assert "fetch_adjust_factors" in auto_inner.calls
    assert not auto_cached.duckdb_cache.query_adjust_factors("600000.SH").empty


def test_p1_derive_mode_direct_refuses_derived_read_of_stored_raw(tmp_path):
    """P1/P2 (item 2): `derive_mode="direct"` must be honest on the READ path too.

    The first revision only changed the WRITE path (`is_raw_inner` forced False), so a cache that
    already held raw bars + factors -- written while the provider was in `auto` -- kept deriving
    qfq/hfq on read. The docstring's "nothing is derived, so no read can be rescaled by a wrong
    factor" was therefore false for exactly the case the switch exists for. `direct` now refuses
    to serve a derived series out of stored raw bytes, while `raw`/`none` still pass through.
    """
    class _SplitProvider(BaseDataProvider):
        """2-for-1 split on the third session: raw 100/120/60, qfq 50/60/60."""

        def __init__(self):
            self.dates = pd.bdate_range("2024-01-01", periods=3)

        def fetch_raw_ohlcv(self, symbol, start, end, interval="1d"):
            return pd.DataFrame(
                {
                    "Open": [100.0, 120.0, 60.0],
                    "High": [101.0, 121.0, 61.0],
                    "Low": [99.0, 119.0, 59.0],
                    "Close": [100.0, 120.0, 60.0],
                    "Volume": [1000.0, 1100.0, 2200.0],
                },
                index=self.dates,
            )

        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return pd.DataFrame(
                {
                    "Open": [50.0, 60.0, 60.0],
                    "High": [50.5, 60.5, 61.0],
                    "Low": [49.5, 59.5, 59.0],
                    "Close": [50.0, 60.0, 60.0],
                    "Volume": [1000.0, 1100.0, 2200.0],
                },
                index=self.dates,
            )

        def fetch_adjust_factors(self, symbol, start=None, end=None):
            return pd.DataFrame({"factor_qfq": [0.5, 0.5, 1.0]}, index=self.dates)

    db_path = str(tmp_path / "direct_read.duckdb")

    # 1. `auto` stores raw bars + factors and derives qfq (the normal path).
    auto = CachedDataProvider(_SplitProvider(), cache_dir=db_path, derive_mode="auto")
    qfq_bars = auto.get_bars("600000.SH", "2024-01-01", "2024-01-03", adjust="qfq")
    meta = auto.duckdb_cache.get_asset_metadata("_SplitProvider", "600000.SH")
    assert meta["adjustflag"] == "raw", "setup: auto must store RAW bytes"
    assert not auto.duckdb_cache.query_adjust_factors("600000.SH").empty
    np.testing.assert_allclose(qfq_bars["Close"].values, [50.0, 60.0, 60.0], rtol=0, atol=1e-12)

    # 2. The SAME cache re-opened in `direct`: a derived request is refused, not silently derived.
    direct = CachedDataProvider(_SplitProvider(), cache_dir=db_path, derive_mode="direct")
    for mode in ("qfq", "hfq"):
        with pytest.raises(ValueError, match="derive_mode='direct'"):
            direct.get_bars("600000.SH", "2024-01-01", "2024-01-03", adjust=mode)

    # Universe reads hit the same guard (one symbol here; the error must not be a skipped symbol).
    with pytest.raises(ValueError, match="derive_mode='direct'"):
        direct.fetch_universe(["600000.SH"], "2024-01-01", "2024-01-03", adjust="qfq")

    # 3. `raw`/`none` are pass-throughs: no derivation was ever needed for them.
    raw_bars = direct.get_bars("600000.SH", "2024-01-01", "2024-01-03", adjust="raw")
    np.testing.assert_array_equal(raw_bars["Close"].values, [100.0, 120.0, 60.0])
    none_bars = direct.get_bars("600000.SH", "2024-01-01", "2024-01-03", adjust="none")
    np.testing.assert_array_equal(none_bars["Close"].values, [100.0, 120.0, 60.0])

    # 4. Control: the `auto` provider on the same cache still derives (the refusal is the mode,
    #    not a broken factor table).
    auto_again = CachedDataProvider(_SplitProvider(), cache_dir=db_path, derive_mode="auto")
    np.testing.assert_allclose(
        auto_again.get_bars("600000.SH", "2024-01-01", "2024-01-03", adjust="qfq")[
            "Close"
        ].values,
        [50.0, 60.0, 60.0],
        rtol=0,
        atol=1e-12,
    )

    # 5. A direct-mode cache holding a DECLARED-qfq series is unaffected (nothing to derive).
    direct_qfq = CachedDataProvider(
        _DualCapabilityProviderL236(),
        cache_dir=str(tmp_path / "direct_qfq.duckdb"),
        derive_mode="direct",
    )
    out = direct_qfq.get_bars("600519.SH", "2024-01-01", "2024-01-02", adjust="qfq")
    np.testing.assert_array_equal(out["Close"].values, [50.25, 50.75])
    assert direct_qfq.duckdb_cache.get_asset_metadata(
        "_DualCapabilityProviderL236", "600519.SH"
    )["adjustflag"] == "qfq"

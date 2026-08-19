"""Unit tests for common/data.py Market Data Provider Architecture."""

import os
import re
import shutil
import tempfile
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from common.data import (
    BaseDataProvider,
    CSVFolderDataProvider,
    CachedDataProvider,
    SyntheticDataProvider,
    YFinanceDataProvider,
    fetch_fund_metadata,
    get_data_provider,
    load_ohlcv,
    load_universe,
    register_provider,
    set_default_data_provider,
)


class CustomDummyProvider(BaseDataProvider):
    """Custom dummy data provider for testing registration."""

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        dates = pd.bdate_range(start or "2020-01-01", periods=10)
        df = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 105.0,
                "Low": 95.0,
                "Close": 102.0,
                "Volume": 1000.0,
            },
            index=dates,
        )
        return df

    def fetch_metadata(self, symbol: str) -> dict:
        return {"expense_ratio": 0.001, "total_assets": 1e9}


@pytest.fixture
def temp_csv_dir():
    tmp_dir = tempfile.mkdtemp()
    yield tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_synthetic_data_provider():
    provider = SyntheticDataProvider(seed=42)
    df = provider.fetch_ohlcv("AAPL", start="2020-01-01", end="2020-01-31")

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    req_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in req_cols:
        assert col in df.columns
    assert isinstance(df.index, pd.DatetimeIndex)


def test_synthetic_data_provider_universe():
    provider = SyntheticDataProvider(seed=42)
    universe = provider.fetch_universe(["AAPL", "MSFT"], start="2020-01-01", end="2020-01-15")

    assert "AAPL" in universe
    assert "MSFT" in universe
    assert not universe["AAPL"].empty
    assert not universe["MSFT"].empty


def test_csv_folder_data_provider(temp_csv_dir):
    # Create a mock CSV file in temp_csv_dir
    dates = pd.bdate_range("2020-01-01", periods=5)
    mock_df = pd.DataFrame(
        {
            "Open": [10.0] * 5,
            "High": [12.0] * 5,
            "Low": [9.0] * 5,
            "Close": [11.0] * 5,
            "Volume": [100.0] * 5,
        },
        index=dates,
    )
    csv_path = os.path.join(temp_csv_dir, "TEST_1d.csv")
    mock_df.to_csv(csv_path)

    provider = CSVFolderDataProvider(folder_path=temp_csv_dir)
    df = provider.fetch_ohlcv("TEST", start="2020-01-01", end="2020-01-10", interval="1d")

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 5
    assert (df["Close"] == 11.0).all()


def test_csv_folder_data_provider_not_found(temp_csv_dir):
    provider = CSVFolderDataProvider(folder_path=temp_csv_dir)
    with pytest.raises(FileNotFoundError):
        provider.fetch_ohlcv("NON_EXISTENT", start="2020-01-01", end="2020-01-10")


def test_cached_data_provider(temp_csv_dir):
    inner_provider = SyntheticDataProvider(seed=42)
    cached_provider = CachedDataProvider(inner_provider, cache_dir=temp_csv_dir)

    # First call: cache miss, fetches from inner and writes to cache_dir
    df1 = cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    cache_file = os.path.join(temp_csv_dir, "SPY_1d_2020-01-01_2020-01-10.csv")
    assert os.path.exists(cache_file)

    # Second call: cache hit, reads from cache_file
    df2 = cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    pd.testing.assert_frame_equal(df1, df2, check_freq=False)


def test_cached_data_provider_caches_through_fetch_universe(temp_csv_dir):
    # Regression test: fetch_universe() used to delegate straight to
    # inner_provider.fetch_universe(...), which calls the INNER provider's
    # own (uncached) fetch_ohlcv per symbol -- silently skipping this
    # class's cache entirely for any caller using load_universe() rather
    # than per-symbol load_ohlcv() (e.g. instrument_selection, research_strategy).
    inner_provider = SyntheticDataProvider(seed=42)
    cached_provider = CachedDataProvider(inner_provider, cache_dir=temp_csv_dir)

    universe = cached_provider.fetch_universe(["SPY", "QQQ"], start="2020-01-01", end="2020-01-10")
    assert set(universe.keys()) == {"SPY", "QQQ"}
    for sym in ("SPY", "QQQ"):
        assert os.path.exists(os.path.join(temp_csv_dir, f"{sym}_1d_2020-01-01_2020-01-10.csv"))


def test_synthetic_data_provider_is_reproducible_across_processes():
    # Regression test: SyntheticDataProvider used to derive its per-symbol
    # seed from Python's builtin hash(), which is randomized per-process for
    # strings (PYTHONHASHSEED) -- the same `seed` produced DIFFERENT data on
    # every process invocation, defeating the whole point of taking a seed.
    # A single test process can't observe that directly (hash() is stable
    # for the process's own lifetime), so this spawns two independent
    # subprocesses and compares their output.
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "from common.data import SyntheticDataProvider; "
        "df = SyntheticDataProvider(seed=42).fetch_ohlcv('SPY', '2020-01-01', '2020-01-10'); "
        "print(df['Close'].sum())"
    )
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=repo_root,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1], (
        f"SyntheticDataProvider produced different data across process runs for the same seed: {outputs}"
    )


@patch("yfinance.download")
def test_yfinance_data_provider(mock_yf_download):
    dates = pd.bdate_range("2020-01-01", periods=5)
    mock_df = pd.DataFrame(
        {
            "Open": [100.0] * 5,
            "High": [105.0] * 5,
            "Low": [95.0] * 5,
            "Close": [102.0] * 5,
            "Volume": [1000.0] * 5,
        },
        index=dates,
    )
    mock_yf_download.return_value = mock_df

    provider = YFinanceDataProvider()
    df = provider.fetch_ohlcv("AAPL", start="2020-01-01", end="2020-01-05")

    assert len(df) == 5
    mock_yf_download.assert_called_once_with(
        "AAPL", start="2020-01-01", end="2020-01-05", interval="1d", auto_adjust=True, progress=False
    )


def test_provider_registry_and_factory():
    register_provider("custom_dummy", CustomDummyProvider)
    prov = get_data_provider("custom_dummy")

    assert isinstance(prov, CustomDummyProvider)
    df = prov.fetch_ohlcv("ANY", start="2020-01-01", end="2020-01-10")
    assert len(df) == 10

    metadata = prov.fetch_metadata("ANY")
    assert metadata["expense_ratio"] == 0.001


def test_invalid_provider_name():
    with pytest.raises(ValueError):
        get_data_provider("invalid_provider_type")


def test_load_ohlcv_backward_compatibility():
    # Calling load_ohlcv with provider="synthetic"
    df = load_ohlcv("QQQ", start="2020-01-01", end="2020-01-15", use_cache=False, provider="synthetic")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty


def test_load_universe_backward_compatibility():
    universe = load_universe(["SPY", "TLT"], start="2020-01-01", end="2020-01-15", use_cache=False, provider="synthetic")
    assert "SPY" in universe
    assert "TLT" in universe


def test_set_default_data_provider():
    dummy = CustomDummyProvider()
    set_default_data_provider(dummy)

    try:
        prov = get_data_provider()
        assert prov is dummy
    finally:
        set_default_data_provider(None)


def test_data_provider_module_specifier_from_file():
    code = (
        "from common.data import BaseDataProvider\n"
        "import pandas as pd\n"
        "class DynamicScriptProvider(BaseDataProvider):\n"
        "    def fetch_ohlcv(self, symbol, start, end, interval='1d'):\n"
        "        dates = pd.bdate_range('2020-01-01', periods=3)\n"
        "        return pd.DataFrame({'Open': 1.0, 'High': 2.0, 'Low': 0.5, 'Close': 1.5, 'Volume': 100}, index=dates)\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        script_path = f.name

    try:
        # Test explicit class specifier
        prov1 = get_data_provider(f"{script_path}:DynamicScriptProvider")
        df1 = prov1.fetch_ohlcv("TEST", "2020-01-01", "2020-01-05")
        assert len(df1) == 3
        assert (df1["Close"] == 1.5).all()

        # Test implicit class auto-discovery
        prov2 = get_data_provider(script_path)
        df2 = prov2.fetch_ohlcv("TEST", "2020-01-01", "2020-01-05")
        assert len(df2) == 3
    finally:
        os.remove(script_path)


def test_data_provider_invalid_module_specifier():
    with pytest.raises((ValueError, ImportError, AttributeError, FileNotFoundError)):
        get_data_provider("non_existent_script.py:MissingClass")


@patch("yfinance.Ticker")
def test_yfinance_fetch_metadata_extracts_expense_ratio_and_aum(mock_ticker):
    mock_ticker.return_value.info = {"netExpenseRatio": 0.03, "totalAssets": 5e9}
    metadata = YFinanceDataProvider().fetch_metadata("SPY")
    assert metadata["expense_ratio"] == pytest.approx(0.03)
    assert metadata["total_assets"] == pytest.approx(5e9)


@patch("yfinance.Ticker")
def test_yfinance_fetch_metadata_missing_fields_returns_nan(mock_ticker):
    mock_ticker.return_value.info = {}
    metadata = YFinanceDataProvider().fetch_metadata("SPY")
    assert np.isnan(metadata["expense_ratio"])
    assert np.isnan(metadata["total_assets"])


@patch("yfinance.Ticker")
def test_yfinance_fetch_metadata_handles_ticker_exception(mock_ticker):
    mock_ticker.side_effect = RuntimeError("network error")
    metadata = YFinanceDataProvider().fetch_metadata("SPY")
    assert np.isnan(metadata["expense_ratio"])
    assert np.isnan(metadata["total_assets"])


def test_cached_data_provider_default_unlimited_age_unchanged(temp_csv_dir):
    # Regression test pinning today's default behavior: with
    # cache_max_age_days omitted, a cache hit is served no matter how old.
    calls = {"n": 0}

    class CountingProvider(SyntheticDataProvider):
        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            calls["n"] += 1
            return super().fetch_ohlcv(symbol, start, end, interval)

    cached_provider = CachedDataProvider(CountingProvider(seed=42), cache_dir=temp_csv_dir)
    cache_path = os.path.join(temp_csv_dir, "SPY_1d_2020-01-01_2020-01-10.csv")

    cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    assert calls["n"] == 1

    # Backdate the cache file's mtime far into the past -- should still hit.
    old_time = time.time() - (365 * 86400)
    os.utime(cache_path, (old_time, old_time))

    cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    assert calls["n"] == 1


def test_cached_data_provider_respects_max_age(temp_csv_dir):
    calls = {"n": 0}

    class CountingProvider(SyntheticDataProvider):
        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            calls["n"] += 1
            return super().fetch_ohlcv(symbol, start, end, interval)

    cached_provider = CachedDataProvider(
        CountingProvider(seed=42), cache_dir=temp_csv_dir, cache_max_age_days=1
    )
    cache_path = os.path.join(temp_csv_dir, "SPY_1d_2020-01-01_2020-01-10.csv")

    cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    assert calls["n"] == 1

    # Backdate the cache file's mtime past the 1-day max age -- should re-fetch.
    old_time = time.time() - (2 * 86400)
    os.utime(cache_path, (old_time, old_time))

    cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")
    assert calls["n"] == 2


def test_csv_folder_data_provider_sorts_unsorted_csv(temp_csv_dir):
    # Regression test: fetch_ohlcv() used to read the CSV, date-filter it,
    # and return WITHOUT sorting -- silently violating the documented
    # ascending-DatetimeIndex OHLCV contract (common/README.md section 1) if
    # the CSV on disk isn't already sorted by date.
    dates = pd.bdate_range("2020-01-01", periods=5)
    shuffled_dates = [dates[3], dates[0], dates[4], dates[1], dates[2]]
    mock_df = pd.DataFrame(
        {
            "Open": [10.0] * 5,
            "High": [12.0] * 5,
            "Low": [9.0] * 5,
            "Close": [11.0, 21.0, 31.0, 41.0, 51.0],
            "Volume": [100.0] * 5,
        },
        index=pd.DatetimeIndex(shuffled_dates),
    )
    csv_path = os.path.join(temp_csv_dir, "UNSORTED_1d.csv")
    mock_df.to_csv(csv_path)

    provider = CSVFolderDataProvider(folder_path=temp_csv_dir)
    df = provider.fetch_ohlcv("UNSORTED", start="2020-01-01", end="2020-01-10", interval="1d")

    assert df.index.is_monotonic_increasing
    assert list(df.index) == list(dates)


def test_load_provider_from_specifier_missing_fetch_ohlcv_raises_type_error_not_name_error():
    # Regression test: the error-raising line referenced an undefined
    # variable `target_str` instead of the actual `specifier` parameter,
    # causing an unhandled NameError instead of the intended descriptive
    # TypeError when a dynamically-loaded provider class lacks fetch_ohlcv.
    code = (
        "class BogusProvider:\n"
        "    def not_fetch_ohlcv(self, symbol, start, end, interval='1d'):\n"
        "        return None\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        script_path = f.name

    try:
        with pytest.raises(TypeError, match=re.escape(f"{script_path}:BogusProvider")):
            get_data_provider(f"{script_path}:BogusProvider")
    finally:
        os.remove(script_path)


def test_csv_folder_data_provider_rejects_path_traversal_symbol(temp_csv_dir):
    # Regression test: `symbol` was interpolated directly into a file path
    # (f"{symbol}.csv") with no sanitization, letting a symbol containing
    # "../" escape the intended folder.
    provider = CSVFolderDataProvider(folder_path=temp_csv_dir)
    with pytest.raises(ValueError):
        provider.fetch_ohlcv("../../evil", start="2020-01-01", end="2020-01-10")


def test_cached_data_provider_rejects_path_traversal_symbol(temp_csv_dir):
    inner_provider = SyntheticDataProvider(seed=42)
    cached_provider = CachedDataProvider(inner_provider, cache_dir=temp_csv_dir)
    with pytest.raises(ValueError):
        cached_provider.fetch_ohlcv("../../evil", start="2020-01-01", end="2020-01-10")


@patch("yfinance.Ticker")
def test_yfinance_fetch_metadata_handles_info_returning_none(mock_ticker):
    # Regression test: yfinance's real failure mode for a delisted/invalid
    # ticker is `.info` returning None (not raising) -- the subsequent
    # `info.get(key)` calls would then raise an uncaught AttributeError
    # instead of falling back to the same NaN-filled dict the exception path
    # already returns. No network access: `.info` is mocked directly.
    mock_ticker.return_value.info = None
    metadata = YFinanceDataProvider().fetch_metadata("DELISTED")
    assert np.isnan(metadata["expense_ratio"])
    assert np.isnan(metadata["total_assets"])


def test_cached_data_provider_recovers_from_corrupt_cache_file(temp_csv_dir):
    inner_provider = SyntheticDataProvider(seed=42)
    cached_provider = CachedDataProvider(inner_provider, cache_dir=temp_csv_dir)
    cache_path = os.path.join(temp_csv_dir, "SPY_1d_2020-01-01_2020-01-10.csv")

    os.makedirs(temp_csv_dir, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not,a,valid,ohlcv,file\n1,2,3,4,5\n")

    with pytest.warns(UserWarning, match="corrupt or invalid"):
        df = cached_provider.fetch_ohlcv("SPY", start="2020-01-01", end="2020-01-10")

    assert not df.empty
    assert "Close" in df.columns

    # The corrupt file should have been overwritten with valid data.
    df2 = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    assert "Close" in df2.columns


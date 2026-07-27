"""Unit tests for common/data.py Market Data Provider Architecture."""

import os
import shutil
import tempfile
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

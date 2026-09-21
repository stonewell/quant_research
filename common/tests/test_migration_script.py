"""Unit tests for scripts/migrate_cache_csv_to_duckdb.py."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import pandas as pd
import pytest

from common.duckdb_cache import DuckDBCache
from scripts.migrate_cache_csv_to_duckdb import migrate_csv_cache, parse_cache_filename


def test_parse_cache_filename():
    fname = "YFinanceDataProvider_SPY_1d_2015-01-01_2024-12-31.csv"
    parsed = parse_cache_filename(fname)
    assert parsed == ("YFinanceDataProvider", "SPY", "1d", "2015-01-01", "2024-12-31")

    # Invalid filename returns None
    assert parse_cache_filename("random_notes.txt") is None


def test_migrate_csv_cache_skips_synthetic_and_imports_real(tmp_path):
    data_dir = tmp_path / "csv_data"
    data_dir.mkdir()
    db_file = tmp_path / "migrated.duckdb"

    dates = pd.bdate_range("2020-01-01", periods=5)
    sample_df = pd.DataFrame(
        {
            "Open": [100.0] * 5,
            "High": [105.0] * 5,
            "Low": [95.0] * 5,
            "Close": [102.0] * 5,
            "Volume": [1000.0] * 5,
        },
        index=dates,
    )

    # 1. Create a real provider CSV
    real_csv = data_dir / "YFinanceDataProvider_AAPL_1d_2020-01-01_2020-01-07.csv"
    sample_df.to_csv(real_csv)

    # 2. Create another real provider CSV (Fuyao)
    fuyao_csv = data_dir / "FuyaoDataProvider_600519.SH_1d_2020-01-01_2020-01-07.csv"
    sample_df.to_csv(fuyao_csv)

    # 3. Create a synthetic provider CSV (must be skipped)
    synth_csv = data_dir / "SyntheticDataProvider_SPY_1d_2020-01-01_2020-01-07.csv"
    sample_df.to_csv(synth_csv)

    summary = migrate_csv_cache(
        data_dir=str(data_dir),
        db_path=str(db_file),
        dry_run=False,
        remove_csvs=False,
        verbose=False,
    )

    assert summary["total_scanned"] == 3
    assert summary["skipped_synthetic"] == 1
    assert summary["migrated_files"] == 2
    assert summary["total_bars"] == 10
    assert summary["unique_symbols"] == 2

    # Verify DuckDB contents
    cache = DuckDBCache(db_path=str(db_file))
    aapl_bars = cache.query_bars("YFinanceDataProvider", "AAPL")
    assert len(aapl_bars) == 5

    sh_bars = cache.query_bars("FuyaoDataProvider", "600519.SH")
    assert len(sh_bars) == 5

    # Verify synthetic SPY is NOT in DuckDB
    spy_bars = cache.query_bars("SyntheticDataProvider", "SPY")
    assert len(spy_bars) == 0

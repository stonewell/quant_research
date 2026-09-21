#!/usr/bin/env python3
"""Migrates legacy historical OHLCV CSV cache files into the unified DuckDB cache (cache.duckdb).

Key Features:
- Scans a directory (default: <repo_root>/data) for cached CSV files.
- Automatically skips all synthetic data files (SyntheticDataProvider_*.csv).
- Parses provider, symbol, interval, and date boundaries from filenames and file contents.
- Bulk upserts bars into `ohlcv_bars` and populates `asset_sync_metadata`.
- Supports --dry-run (inspect without modifying database) and optional --remove-csvs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

# Bootstrap repo root to reach common/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache, is_synthetic_provider, _REQUIRED_OHLCV_COLUMNS

# Pattern: {Provider}_{Symbol}_{Interval}_{Start}_{End}.csv
# Example: YFinanceDataProvider_000002.SZ_1d_2020-01-01_2026-01-01.csv
_CACHE_FILE_RE = re.compile(
    r"^([A-Za-z0-9]+)_([A-Za-z0-9_.\-^=]+)_([A-Za-z0-9]+)_([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{4}-[0-9]{2}-[0-9]{2})\.csv$"
)


def parse_cache_filename(filename: str) -> Optional[Tuple[str, str, str, str, str]]:
    """Extracts (provider, symbol, interval, start, end) from standard cache filename."""
    m = _CACHE_FILE_RE.match(filename)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    return None


def migrate_csv_cache(
    data_dir: str,
    db_path: Optional[str] = None,
    dry_run: bool = False,
    remove_csvs: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Scans `data_dir` for CSV cache files and migrates non-synthetic data into DuckDB."""
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Data directory '{data_dir}' does not exist.")

    target_db = db_path if db_path else os.path.join(data_dir, "cache.duckdb")
    cache = None if dry_run else DuckDBCache(db_path=target_db)

    csv_files = [f for f in os.listdir(data_dir) if f.endswith(".csv")]
    csv_files.sort()

    total_scanned = len(csv_files)
    skipped_synthetic = 0
    skipped_invalid = 0
    migrated_files = 0
    total_bars = 0
    symbols_seen = set()
    files_to_remove: List[str] = []

    start_time = time.time()
    if verbose:
        print(f"Starting migration in {'DRY-RUN' if dry_run else 'REAL'} mode...")
        print(f"Source folder: {data_dir}")
        print(f"Target DuckDB: {target_db}")
        print(f"Found {total_scanned} CSV files to process.\n")

    for idx, fname in enumerate(csv_files, 1):
        fpath = os.path.join(data_dir, fname)

        # 1. Skip synthetic data
        if fname.startswith("SyntheticDataProvider") or "synthetic" in fname.lower():
            skipped_synthetic += 1
            continue

        parsed = parse_cache_filename(fname)
        if parsed:
            provider, symbol, interval, req_start, req_end = parsed
        else:
            # Check if it's a {Symbol}_{Interval}.csv or {Symbol}.csv
            base_name = fname[:-4]
            if "_" in base_name:
                parts = base_name.rsplit("_", 1)
                symbol, interval = parts[0], parts[1]
            else:
                symbol, interval = base_name, "1d"
            provider = "CSVFolderDataProvider"
            req_start = None
            req_end = None

        if is_synthetic_provider(provider):
            skipped_synthetic += 1
            continue

        try:
            df = pd.read_csv(fpath, index_col=0, parse_dates=True)
            if df.empty or not all(c in df.columns for c in _REQUIRED_OHLCV_COLUMNS):
                skipped_invalid += 1
                continue

            df = df[_REQUIRED_OHLCV_COLUMNS].dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                skipped_invalid += 1
                continue

            n_bars = len(df)
            total_bars += n_bars
            symbols_seen.add(symbol)
            migrated_files += 1

            if not dry_run and cache is not None:
                cache.store_bars(
                    provider=provider,
                    symbol=symbol,
                    df=df,
                    requested_start=req_start,
                    requested_end=req_end,
                )
                if remove_csvs:
                    files_to_remove.append(fpath)

        except Exception as exc:
            skipped_invalid += 1
            if verbose:
                print(f"Warning: Failed to process '{fname}': {exc}")

        if verbose and idx % 200 == 0:
            print(f"Processed {idx}/{total_scanned} files...")

    # Optional removal of migrated CSVs
    if remove_csvs and not dry_run:
        for fpath in files_to_remove:
            try:
                os.remove(fpath)
            except Exception:
                pass

    elapsed = time.time() - start_time
    summary = {
        "total_scanned": total_scanned,
        "skipped_synthetic": skipped_synthetic,
        "skipped_invalid": skipped_invalid,
        "migrated_files": migrated_files,
        "total_bars": total_bars,
        "unique_symbols": len(symbols_seen),
        "target_db": target_db,
        "elapsed_seconds": elapsed,
    }

    if verbose:
        print("\n========================================")
        print("CSV to DuckDB Migration Summary:")
        print(f"  Total CSV files scanned:   {total_scanned}")
        print(f"  Synthetic files skipped:   {skipped_synthetic}")
        print(f"  Invalid/corrupt skipped:   {skipped_invalid}")
        print(f"  Valid files migrated:      {migrated_files}")
        print(f"  Total bars stored:         {total_bars:,}")
        print(f"  Unique symbols:            {len(symbols_seen)}")
        print(f"  Elapsed time:              {elapsed:.2f}s")
        if not dry_run and os.path.isfile(target_db):
            db_size_mb = os.path.getsize(target_db) / (1024 * 1024)
            print(f"  DuckDB file size:          {db_size_mb:.2f} MB ({target_db})")
        print("========================================")

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--data-dir",
        default=os.path.join(_REPO_ROOT, "data"),
        help="Folder containing legacy CSV cache files to migrate.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Target DuckDB file path (default: <data-dir>/cache.duckdb).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate migration without modifying or creating DuckDB file.",
    )
    parser.add_argument(
        "--remove-csvs",
        action="store_true",
        help="Delete successfully migrated CSV files after storing them in DuckDB.",
    )
    args = parser.parse_args()

    migrate_csv_cache(
        data_dir=args.data_dir,
        db_path=args.db_path,
        dry_run=args.dry_run,
        remove_csvs=args.remove_csvs,
        verbose=True,
    )


if __name__ == "__main__":
    main()

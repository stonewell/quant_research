#!/usr/bin/env python3
"""CLI utility to rebase cached OHLCV bars for corporate actions (splits, dividends).

Audited administrative path for non-China or auto-adjusting providers (L2.33).
Uses `CachedDataProvider.rebase_corporate_action` with `allow_history_rewrite=True`
and an explicit audit reason.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
from common.data import CachedDataProvider, get_data_provider
from common.duckdb_cache import DuckDBCache
from common.market_clock import MARKET_US, infer_market_from_symbols, market_now

_DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "cache.duckdb")


def default_end_date(symbol: str) -> str:
    """`--end` default: the current **market** calendar day, never the host's.

    (development_plan.md `L2.31`) Owner of market time is `common/market_clock.py`.
    """
    market = MARKET_US
    try:
        inferred = infer_market_from_symbols([symbol])
        if inferred:
            market = inferred
    except Exception:
        pass
    return market_now(market).local_date().isoformat()


def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebase cached OHLCV bars for corporate actions (splits, dividends) with an audit reason."
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="yfinance",
        help="Data provider name (e.g. 'yfinance', default: 'yfinance').",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        required=True,
        help="Ticker/symbol to rebase (e.g. 'AAPL', 'MSFT').",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date for the rebase window (YYYY-MM-DD). Defaults to earliest cached date or '2000-01-01'.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date for the rebase window (YYYY-MM-DD). Defaults to current date.",
    )
    parser.add_argument(
        "--reason",
        type=str,
        required=True,
        help="Mandatory audit explanation for the rebase (must be >= 10 non-whitespace characters).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=_DEFAULT_DB,
        help=f"Path to DuckDB cache database file (default: {_DEFAULT_DB}).",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1d",
        help="Bar interval (default: '1d').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run: inspect and report affected rows without writing changes.",
    )
    parsed = parser.parse_args(args)
    if not parsed.reason or len(parsed.reason.strip()) < 10:
        parser.error("--reason must contain at least 10 non-whitespace characters explaining the corporate action.")
    return parsed


def main(args: Optional[list[str]] = None) -> int:
    parsed = parse_args(args)
    provider_inst = get_data_provider(parsed.provider)
    provider_name = type(provider_inst).__name__

    db_path = os.path.abspath(parsed.db)
    print(f"Target DB: {db_path}")
    print(f"Provider:  {provider_name}")
    print(f"Symbol:    {parsed.symbol}")
    print(f"Interval:  {parsed.interval}")
    print(f"Reason:    {parsed.reason.strip()}")

    # Determine date range
    start = parsed.start
    end = parsed.end or default_end_date(parsed.symbol)

    # Inspect current cache state before rebase
    cached_df = pd.DataFrame()
    if os.path.exists(db_path):
        try:
            cache = DuckDBCache(db_path=db_path, read_only=True)
            cached_df = cache.query_bars(provider_name, parsed.symbol, start=start, end=end, interval=parsed.interval)
        except Exception as exc:
            print(f"Warning: Could not read existing cached bars from {db_path}: {exc}")

    if not start:
        if not cached_df.empty:
            start = cached_df.index.min().strftime("%Y-%m-%d")
        else:
            start = "2000-01-01"

    print(f"Window:    {start} .. {end}")
    print("--- BEFORE REBASE ---")
    if cached_df.empty:
        print(f"Cached bars: 0 rows found in {db_path} for {provider_name}/{parsed.symbol}.")
    else:
        print(f"Cached bars: {len(cached_df)} row(s) from {cached_df.index.min().date()} to {cached_df.index.max().date()}.")
        sample_close_before = cached_df["Close"].iloc[-1]
        print(f"Latest cached close: {sample_close_before} at {cached_df.index.max().date()}")

    if parsed.dry_run:
        print("--- DRY RUN ANALYSIS ---")
        print(f"Fetching fresh bars from {provider_name} to inspect adjustments...")
        fresh_df = provider_inst.fetch_ohlcv(parsed.symbol, start, end, interval=parsed.interval)
        if fresh_df.empty:
            print(f"Error: {provider_name} returned no data for {parsed.symbol} [{start} .. {end}].")
            return 1

        overlap = cached_df.index.intersection(fresh_df.index)
        print(f"Fresh bars fetched: {len(fresh_df)} row(s). Overlapping with cache: {len(overlap)} row(s).")
        if len(overlap) > 0:
            diff_mask = (cached_df.loc[overlap, "Close"] - fresh_df.loc[overlap, "Close"]).abs() > 1e-9 * cached_df.loc[overlap, "Close"].abs()
            n_diff = int(diff_mask.sum())
            if n_diff > 0:
                worst_ratio = (fresh_df.loc[overlap, "Close"] / cached_df.loc[overlap, "Close"]).replace([float("inf"), float("-inf")], float("nan"))
                min_ratio = float(worst_ratio.min())
                max_ratio = float(worst_ratio.max())
                print(f"Price differences detected in {n_diff}/{len(overlap)} overlapping bar(s).")
                print(f"Adjustment ratio range: min {min_ratio:.4f}, max {max_ratio:.4f}.")
            else:
                print("No price differences detected between cached bars and upstream bars.")
        print("[DRY RUN] Would call rebase_corporate_action with allow_history_rewrite=True. No changes written.")
        return 0

    print("--- EXECUTING REBASE ---")
    cached_provider = CachedDataProvider(provider_inst, cache_dir=db_path)
    result_df = cached_provider.rebase_corporate_action(
        symbol=parsed.symbol,
        start=start,
        end=end,
        reason=parsed.reason,
        interval=parsed.interval,
    )

    print("--- AFTER REBASE ---")
    print(f"Total bars now stored: {len(result_df)} row(s) from {result_df.index.min().date()} to {result_df.index.max().date()}.")
    if not result_df.empty:
        print(f"Latest cached close: {result_df['Close'].iloc[-1]} at {result_df.index.max().date()}")
    print(f"[SUCCESS] Rebase completed for {provider_name}/{parsed.symbol}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

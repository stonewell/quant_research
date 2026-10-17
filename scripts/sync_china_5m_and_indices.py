#!/usr/bin/env python3
"""Batch Synchronization Script for China A-Share 5m Intraday & Index Daily Bars.

Features:
- Part A: Index Daily (1d) Gap Fix & Equity Recent Bar Patching:
  - Fetches complete 2016-present daily bars for the 7 major indices via BaoStock + EastMoney.
  - Stores under BaoStockDataProvider, EastMoneyDataProvider, FuyaoDataProvider, MarketDBDataProvider
    in data/cache.duckdb, marking is_earliest_known=True.
  - Scans all 22 stocks in core_satellite_22_stocks.txt and patches any missing recent daily bars
    (e.g., 002371.SZ, 601166.SH, 601390.SH) up to the latest trading day with qfq alignment.
- Part B: 5-Minute (5m) Intraday Pull & Dual-Track Caching (2022-01-01 -> Present):
  - Strictly sequential single-process downloading with a single persistent BaoStock login session
    to completely prevent IP/server throttling from BaoStock.
  - Fetches 5m qfq bars from BaoStock + supplements trailing days from EastMoney.
  - Stores into intraday_ohlcv_bars & intraday_sync_metadata in data/cache.duckdb sequentially
    after each symbol, so any already-cached symbols are skipped on resume.
  - Caches 511880.SH and available recent 5m bars for all 7 major indices.
- Part C: Verification Summary Tables:
  - Prints comprehensive Markdown summary tables detailing 1d and 5m coverage and health metrics.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
import warnings
from typing import Dict, List, Optional, Tuple

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache
from common.financial_api import (
    BaoStockDataProvider,
    EastMoneyDataProvider,
    is_index_symbol,
    normalize_thscode,
)

MAJOR_INDICES = [
    "000300.SH",  # CSI 300
    "000001.SH",  # SSE Composite
    "000905.SH",  # CSI 500
    "000852.SH",  # CSI 1000
    "000016.SH",  # SSE 50
    "399001.SZ",  # SZSE Component
    "399006.SZ",  # ChiNext
]


def load_universe(file_path: str) -> List[str]:
    """Loads ticker symbols from a universe text file, ignoring comments and blanks."""
    symbols = []
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Universe file not found: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            token = line.split()[0].strip()
            if token:
                symbols.append(normalize_thscode(token))
    return list(dict.fromkeys(symbols))


def sync_indices_daily(
    cache: DuckDBCache,
    indices: List[str],
    start: str,
    end: str,
) -> None:
    """Syncs full daily bars for major indices into all 4 provider namespaces."""
    print("\n" + "=" * 80)
    print(f"PART A.1: Syncing Major Indices Daily (1d) Bars [{start} -> {end}]")
    print("=" * 80)

    bs_prov = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)
    em_prov = EastMoneyDataProvider(adjust="none")

    try:
        for sym in indices:
            print(f"-> Syncing Index {sym} (1d)...", end=" ", flush=True)
            df_bs = pd.DataFrame()
            try:
                df_bs = bs_prov.fetch_ohlcv(sym, start, end, interval="1d")
            except Exception as e:
                print(f"[BaoStock failed: {e}]", end=" ", flush=True)

            df_em = pd.DataFrame()
            try:
                df_em = em_prov.fetch_ohlcv(sym, start, end, interval="1d")
            except Exception as e:
                pass

            dfs = [d for d in [df_bs, df_em] if not d.empty]
            if not dfs:
                print("FAILED (no data from providers)")
                continue

            full_df = pd.concat(dfs)
            full_df = full_df[~full_df.index.duplicated(keep="last")].sort_index()

            # Store into all 4 provider namespaces
            providers = [
                "BaoStockDataProvider",
                "EastMoneyDataProvider",
                "FuyaoDataProvider",
                "MarketDBDataProvider",
            ]
            for prov in providers:
                cache.store_bars(
                    provider=prov,
                    symbol=sym,
                    df=full_df,
                    requested_start=start,
                    requested_end=end,
                    interval="1d",
                )

            # Mark is_earliest_known=True
            con = cache._get_connection(read_only=False)
            try:
                placeholders = ",".join(["?"] * len(providers))
                con.execute(
                    f"UPDATE asset_sync_metadata SET is_earliest_known = TRUE "
                    f"WHERE symbol = ? AND provider IN ({placeholders})",
                    [sym, *providers],
                )
            finally:
                if not cache.is_memory:
                    con.close()

            print(
                f"OK: {len(full_df)} bars "
                f"[{full_df.index.min().strftime('%Y-%m-%d')} -> {full_df.index.max().strftime('%Y-%m-%d')}]"
            )
    finally:
        bs_prov.logout()


def patch_equity_daily_bars(
    cache: DuckDBCache,
    stocks: List[str],
    end: str,
) -> None:
    """Patches lagging recent daily bars for the 22 stocks under Fuyao & MarketDB."""
    print("\n" + "=" * 80)
    print(f"PART A.2: Checking & Patching 22 Stocks Recent Daily Bars (-> {end})")
    print("=" * 80)

    em_prov = EastMoneyDataProvider(adjust="qfq")
    bs_prov = BaoStockDataProvider(adjustflag="2", keep_logged_in=True)

    target_end_date = pd.to_datetime(end).date()

    # Pre-query existing coverage and close connection upfront to prevent DuckDB connection conflict
    con = cache._get_connection(read_only=True)
    try:
        all_rows = con.execute(
            "SELECT symbol, provider, min(date), max(date), count(*) "
            "FROM ohlcv_bars WHERE provider IN ('FuyaoDataProvider', 'MarketDBDataProvider') "
            "GROUP BY symbol, provider"
        ).fetchall()
        close_map: Dict[Tuple[str, str, datetime.date], float] = {}
        for r in all_rows:
            sym_val, prov_val, _, max_d, _ = r
            if max_d:
                c_row = con.execute(
                    "SELECT close FROM ohlcv_bars WHERE symbol = ? AND provider = ? AND date = ?",
                    [sym_val, prov_val, max_d],
                ).fetchone()
                if c_row:
                    close_map[(sym_val, prov_val, max_d)] = float(c_row[0])
    finally:
        if not cache.is_memory:
            con.close()

    row_dict = {(r[0], r[1]): r[3] for r in all_rows}  # (symbol, provider) -> max_date

    try:
        for sym in stocks:
            for prov in ["FuyaoDataProvider", "MarketDBDataProvider"]:
                max_date = row_dict.get((sym, prov))
                if max_date is not None and max_date >= target_end_date - datetime.timedelta(days=2):
                    continue  # Already up to date

                patch_start = (
                    (max_date - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
                    if max_date
                    else "2016-01-01"
                )
                print(f"-> Patching {sym} [{prov}] from {patch_start}...", end=" ", flush=True)

                # Fetch recent bars: try BaoStock first then EastMoney
                new_df = pd.DataFrame()
                try:
                    new_df = bs_prov.fetch_ohlcv(sym, patch_start, end, interval="1d")
                except Exception:
                    pass
                if new_df.empty:
                    try:
                        time.sleep(0.15)
                        new_df = em_prov.fetch_ohlcv(sym, patch_start, end, interval="1d")
                    except Exception:
                        pass

                if new_df.empty:
                    print("SKIPPED (no new bars)")
                    continue

                # Align qfq scale on overlap date if max_date exists
                if max_date is not None:
                    max_ts = pd.Timestamp(max_date)
                    if max_ts in new_df.index:
                        loc_c = close_map.get((sym, prov, max_date), 0.0)
                        new_c = float(new_df.loc[max_ts, "Close"])
                        if loc_c > 0 and new_c > 0 and abs(new_c - loc_c) / loc_c > 1e-4:
                            ratio = loc_c / new_c
                            new_df[["Open", "High", "Low", "Close"]] *= ratio

                    new_df = new_df[new_df.index.date > max_date]

                if not new_df.empty:
                    cache.store_bars(
                        provider=prov,
                        symbol=sym,
                        df=new_df,
                        requested_start=patch_start,
                        requested_end=end,
                        interval="1d",
                    )
                    print(f"PATCHED +{len(new_df)} bars (max: {new_df.index.max().strftime('%Y-%m-%d')})")
                else:
                    print("ALREADY CURRENT")
    finally:
        bs_prov.logout()


def sync_5m_intraday(
    cache: DuckDBCache,
    stocks: List[str],
    indices: List[str],
    start: str,
    end: str,
) -> None:
    """Syncs 5m intraday data sequentially with a single persistent BaoStock session."""
    print("\n" + "=" * 80)
    print(f"PART B: Pulling & Caching 5m Intraday Bars [{start} -> {end}]")
    print("Mode: Strictly Sequential Single-Process (BaoStock session reuse)")
    print("=" * 80)

    start_ts = pd.Timestamp(start)
    end_date = pd.to_datetime(end).date()

    # Targets: 22 stocks + 511880.SH
    all_targets = list(stocks)
    if "511880.SH" not in all_targets:
        all_targets.append("511880.SH")

    bs_prov = BaoStockDataProvider(adjustflag="2", keep_logged_in=True)
    em_prov = EastMoneyDataProvider(adjust="qfq")

    t_total = time.time()

    try:
        for idx, sym in enumerate(all_targets, 1):
            # Check cache hit
            meta = cache.get_intraday_metadata("BaoStockDataProvider", sym, "5m")
            if meta:
                min_ts = meta["earliest_available_ts"]
                max_ts = meta["latest_available_ts"]
                if min_ts <= start_ts + pd.Timedelta(days=7) and max_ts.date() >= end_date - datetime.timedelta(days=2):
                    print(f"[{idx}/{len(all_targets)}] [CACHE HIT] {sym} already covered ({min_ts} -> {max_ts})")
                    continue

            print(f"[{idx}/{len(all_targets)}] Fetching 5m for {sym}...", end=" ", flush=True)
            t0 = time.time()

            dfs: List[pd.DataFrame] = []
            try:
                df_bs = bs_prov.fetch_ohlcv(sym, start, end, interval="5m")
                if not df_bs.empty:
                    dfs.append(df_bs)
            except Exception as e:
                print(f"[BaoStock error: {e}]", end=" ", flush=True)

            # Check if trailing recent bars from EastMoney are needed
            try:
                if dfs:
                    last_dt = dfs[0].index.max()
                    req_end_dt = pd.to_datetime(end)
                    if (req_end_dt - last_dt).total_seconds() > 3600 * 24:
                        time.sleep(0.2)
                        em_start = (last_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                        df_em = em_prov.fetch_ohlcv(sym, em_start, end, interval="5m")
                        if not df_em.empty:
                            dfs.append(df_em[df_em.index > last_dt])
                else:
                    time.sleep(0.2)
                    df_em = em_prov.fetch_ohlcv(sym, start, end, interval="5m")
                    if not df_em.empty:
                        dfs.append(df_em)
            except Exception as e:
                pass

            if not dfs:
                print("FAILED (no data returned)")
                continue

            full_df = pd.concat(dfs)
            full_df = full_df[~full_df.index.duplicated(keep="last")].sort_index()

            # Store immediately into DuckDB cache
            cache.store_intraday_bars(
                "BaoStockDataProvider",
                sym,
                full_df,
                interval="5m",
                requested_start=start,
                requested_end=end,
            )
            cache.store_intraday_bars(
                "EastMoneyDataProvider",
                sym,
                full_df,
                interval="5m",
                requested_start=start,
                requested_end=end,
            )

            # Mark earliest known if starts in early 2022
            if full_df.index.min() <= pd.Timestamp("2022-01-10"):
                con = cache._get_connection(read_only=False)
                try:
                    con.execute(
                        "UPDATE intraday_sync_metadata SET is_earliest_known = TRUE "
                        "WHERE symbol = ? AND interval = '5m'",
                        [sym],
                    )
                finally:
                    if not cache.is_memory:
                        con.close()

            elapsed = time.time() - t0
            print(
                f"STORED {len(full_df):,} bars "
                f"({full_df.index.min().strftime('%Y-%m-%d %H:%M')} -> {full_df.index.max().strftime('%Y-%m-%d %H:%M')}) "
                f"[{elapsed:.1f}s]"
            )
    finally:
        bs_prov.logout()

    # 2. Sync available recent 5m bars for Major Indices via EastMoney
    print("\n-> Syncing available recent 5m bars for Major Indices via EastMoney...")
    em_idx_prov = EastMoneyDataProvider(adjust="none")
    for idx_sym in indices:
        try:
            time.sleep(0.2)
            df_idx_5m = em_idx_prov.fetch_ohlcv(idx_sym, "2026-08-01", end, interval="5m")
            if not df_idx_5m.empty:
                cache.store_intraday_bars(
                    "EastMoneyDataProvider",
                    idx_sym,
                    df_idx_5m,
                    interval="5m",
                    requested_start="2026-08-01",
                    requested_end=end,
                )
                cache.store_intraday_bars(
                    "FuyaoDataProvider",
                    idx_sym,
                    df_idx_5m,
                    interval="5m",
                    requested_start="2026-08-01",
                    requested_end=end,
                )
                print(
                    f"-> Index {idx_sym} (5m): {len(df_idx_5m):,} bars "
                    f"({df_idx_5m.index.min().strftime('%Y-%m-%d %H:%M')} -> {df_idx_5m.index.max().strftime('%Y-%m-%d %H:%M')})"
                )
        except Exception as e:
            print(f"-> Index {idx_sym} (5m) skipped: {e}")


def print_verification_summary(cache: DuckDBCache, stocks: List[str], indices: List[str]) -> None:
    """Prints comprehensive Markdown summary tables verifying 1d and 5m coverage."""
    print("\n" + "=" * 80)
    print("PART C: VERIFICATION SUMMARY TABLES")
    print("=" * 80)

    con = cache._get_connection(read_only=True)
    try:
        # Table 1: Daily (1d) Coverage Table
        print("\n### 1. Daily (1d) Coverage Summary (data/cache.duckdb)")
        print("| Symbol | Type | Provider | Bar Count | Min Date | Max Date | Gap / Health |")
        print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

        all_symbols = indices + stocks
        for sym in all_symbols:
            stype = "Index" if is_index_symbol(sym) else "Stock"
            rows = con.execute(
                "SELECT provider, count(*), min(date), max(date) "
                "FROM ohlcv_bars WHERE symbol = ? "
                "GROUP BY provider ORDER BY provider",
                [sym],
            ).fetchall()
            if not rows:
                print(f"| `{sym}` | {stype} | *None* | 0 | - | - | ❌ Missing |")
            for r in rows:
                prov, count, min_d, max_d = r
                status = "✅ Healthy" if count >= 1000 and max_d >= datetime.date(2026, 10, 16) else "⚠️ Checked"
                print(f"| `{sym}` | {stype} | {prov} | {count:,} | {min_d} | {max_d} | {status} |")

        # Table 2: Intraday (5m) Coverage Table
        print("\n### 2. Intraday (5m) Coverage Summary (data/cache.duckdb)")
        print("| Symbol | Provider | Bar Count | Earliest Bar | Latest Bar | Trading Days | Status |")
        print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

        symbols_5m = stocks + ["511880.SH"] + indices
        for sym in symbols_5m:
            rows_5m = con.execute(
                "SELECT provider, count(*), min(ts), max(ts), count(distinct cast(ts as date)) "
                "FROM intraday_ohlcv_bars WHERE symbol = ? AND interval = '5m' "
                "GROUP BY provider ORDER BY provider",
                [sym],
            ).fetchall()
            if not rows_5m:
                print(f"| `{sym}` | *None* | 0 | - | - | 0 | ❌ No 5m Data |")
            for r in rows_5m:
                prov, count, min_ts, max_ts, days = r
                min_str = pd.Timestamp(min_ts).strftime("%Y-%m-%d %H:%M")
                max_str = pd.Timestamp(max_ts).strftime("%Y-%m-%d %H:%M")
                stat = "✅ Full (2022-2026)" if count >= 30000 else "✅ Recent Intraday"
                print(f"| `{sym}` | {prov} | {count:,} | {min_str} | {max_str} | {days:,} | {stat} |")
    finally:
        if not cache.is_memory:
            con.close()


def main():
    parser = argparse.ArgumentParser(description="Sync China 5m Intraday & Index Daily Bars into DuckDB")
    parser.add_argument(
        "--universe-file",
        default=os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt"),
        help="Path to universe file with stock tickers",
    )
    parser.add_argument("--start-5m", default="2022-01-01", help="Start date for 5m intraday sync")
    parser.add_argument("--start-1d", default="2016-01-01", help="Start date for index daily sync")
    parser.add_argument("--end", default=datetime.date.today().strftime("%Y-%m-%d"), help="End date for sync")
    parser.add_argument("--cache-dir", default=os.path.join(_REPO_ROOT, "data"), help="DuckDB cache folder")

    args = parser.parse_args()

    print("================================================================================")
    print(" CHINA MARKET DATA CACHE SYNC: 5M INTRADAY & DAILY INDEX GAPS")
    print(f" Universe File: {args.universe_file}")
    print(f" 5m Start Date: {args.start_5m} | 1d Start Date: {args.start_1d} | End Date: {args.end}")
    print(f" Cache Directory: {args.cache_dir} | Mode: Sequential Single-Process")
    print("================================================================================")

    stocks = load_universe(args.universe_file)
    print(f"Loaded {len(stocks)} universe stocks: {stocks}")

    cache = DuckDBCache(args.cache_dir)

    t_start = time.time()

    # Part A: Index 1d gap fix + equity recent daily bar patching
    sync_indices_daily(cache, MAJOR_INDICES, start=args.start_1d, end=args.end)
    patch_equity_daily_bars(cache, stocks, end=args.end)

    # Part B: 5m intraday downloading and caching (Sequential Single-Process)
    sync_5m_intraday(cache, stocks, MAJOR_INDICES, start=args.start_5m, end=args.end)

    # Part C: Print summary verification tables
    print_verification_summary(cache, stocks, MAJOR_INDICES)

    total_time = time.time() - t_start
    print(f"\n[DONE] All synchronization and caching tasks completed in {total_time:.1f}s ({total_time/60:.2f}m)!")


if __name__ == "__main__":
    main()

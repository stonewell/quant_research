#!/usr/bin/env python3
"""Manual real-data verification tool for BaoStock adjust factor derivation.

This script validates that raw OHLCV bars stored in DuckDBCache, when dynamically
multiplied by derived adjust factors (in-memory QFQ derivation), match BaoStock's
native QFQ bars (adjustflag="2") to within a relative error of <= 1e-6 on prices
and exact byte-level equality on Volume.

NOTE: This script performs LIVE network calls to BaoStock and must NEVER be
executed during automated offline pytest test runs (per AGENTS.md 100% offline policy).
It is intended for manual execution and pre-release hard-gate audits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache
from common.financial_api import BaoStockDataProvider


def verify_symbol_derivation(
    symbol: str,
    start_date: str,
    end_date: str,
    bs_raw: BaoStockDataProvider,
    bs_qfq: BaoStockDataProvider,
    tolerance: float = 1e-6,
) -> Dict[str, Any]:
    """Verifies daily and 5m derived QFQ vs native BaoStock QFQ for a single symbol."""
    cache = DuckDBCache(db_path=":memory:")
    result: Dict[str, Any] = {
        "symbol": symbol,
        "daily": {"status": "UNKNOWN"},
        "intraday_5m": {"status": "UNKNOWN"},
    }

    # 1. Fetch full factors first
    factors_df = bs_raw.fetch_adjust_factors(symbol, "1990-01-01", end_date)
    if factors_df is None or factors_df.empty:
        result["daily"]["status"] = "FAIL"
        result["daily"]["error"] = "No adjust factors returned from BaoStock"
        return result
    cache.store_adjust_factors(symbol, factors_df, source="BaoStockDataProvider")

    # 2. Daily Verification
    try:
        raw_daily = bs_raw.fetch_raw_ohlcv(symbol, start_date, end_date, interval="1d")
        direct_qfq_daily = bs_qfq.fetch_ohlcv(symbol, start_date, end_date, interval="1d")

        if raw_daily.empty or direct_qfq_daily.empty:
            result["daily"] = {
                "status": "FAIL",
                "error": f"Empty daily bars: raw={len(raw_daily)}, direct_qfq={len(direct_qfq_daily)}",
            }
        else:
            cache.store_bars(
                "BaoStockDataProvider",
                symbol,
                raw_daily,
                requested_start=start_date,
                requested_end=end_date,
                adjustflag="raw",
            )
            derived_qfq_daily = cache.query_bars(
                "BaoStockDataProvider",
                symbol,
                start_date,
                end_date,
                adjust="qfq",
            )

            common_idx = derived_qfq_daily.index.intersection(direct_qfq_daily.index)
            if len(common_idx) < 60:
                result["daily"] = {
                    "status": "FAIL",
                    "error": f"Insufficient common trading days ({len(common_idx)} < 60)",
                }
            else:
                derived_sub = derived_qfq_daily.loc[common_idx]
                direct_sub = direct_qfq_daily.loc[common_idx]

                # Price relative error check
                price_cols = ["Open", "High", "Low", "Close"]
                max_rel_errors: Dict[str, float] = {}
                price_ok = True
                for col in price_cols:
                    diff = np.abs(derived_sub[col].values - direct_sub[col].values)
                    denom = np.maximum(np.abs(direct_sub[col].values), 1e-8)
                    rel_err = float(np.max(diff / denom))
                    max_rel_errors[col] = rel_err
                    if rel_err > tolerance:
                        price_ok = False

                # Volume exact equality check
                vol_exact = bool(np.array_equal(derived_sub["Volume"].values, direct_sub["Volume"].values))

                result["daily"] = {
                    "status": "PASS" if (price_ok and vol_exact) else "FAIL",
                    "bars_count": len(common_idx),
                    "max_rel_errors": max_rel_errors,
                    "volume_exact_match": vol_exact,
                    "tolerance": tolerance,
                }
    except Exception as e:
        result["daily"] = {"status": "ERROR", "error": str(e)}

    # 3. 5m Intraday Verification (last 10 trading days to keep query manageable)
    try:
        intraday_start = (pd.to_datetime(end_date) - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
        raw_5m = bs_raw.fetch_raw_ohlcv(symbol, intraday_start, end_date, interval="5m")
        direct_qfq_5m = bs_qfq.fetch_ohlcv(symbol, intraday_start, end_date, interval="5m")

        if raw_5m.empty or direct_qfq_5m.empty:
            result["intraday_5m"] = {
                "status": "FAIL",
                "error": f"Empty 5m bars: raw={len(raw_5m)}, direct_qfq={len(direct_qfq_5m)}",
            }
        else:
            cache.store_intraday_bars(
                "BaoStockDataProvider",
                symbol,
                raw_5m,
                interval="5m",
                requested_start=intraday_start,
                requested_end=end_date,
                adjustflag="raw",
            )
            derived_qfq_5m = cache.query_intraday_bars(
                "BaoStockDataProvider",
                symbol,
                intraday_start,
                end_date,
                interval="5m",
                adjust="qfq",
            )

            common_idx_5m = derived_qfq_5m.index.intersection(direct_qfq_5m.index)
            if len(common_idx_5m) == 0:
                result["intraday_5m"] = {"status": "FAIL", "error": "No overlapping 5m bars"}
            else:
                derived_sub_5m = derived_qfq_5m.loc[common_idx_5m]
                direct_sub_5m = direct_qfq_5m.loc[common_idx_5m]

                price_cols = ["Open", "High", "Low", "Close"]
                max_rel_errors_5m: Dict[str, float] = {}
                price_5m_ok = True
                for col in price_cols:
                    diff = np.abs(derived_sub_5m[col].values - direct_sub_5m[col].values)
                    denom = np.maximum(np.abs(direct_sub_5m[col].values), 1e-8)
                    rel_err = float(np.max(diff / denom))
                    max_rel_errors_5m[col] = rel_err
                    if rel_err > tolerance:
                        price_5m_ok = False

                vol_5m_exact = bool(np.array_equal(derived_sub_5m["Volume"].values, direct_sub_5m["Volume"].values))

                result["intraday_5m"] = {
                    "status": "PASS" if (price_5m_ok and vol_5m_exact) else "FAIL",
                    "bars_count": len(common_idx_5m),
                    "max_rel_errors": max_rel_errors_5m,
                    "volume_exact_match": vol_5m_exact,
                    "tolerance": tolerance,
                }
    except Exception as e:
        result["intraday_5m"] = {"status": "ERROR", "error": str(e)}

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify in-memory adjust factor derivation against BaoStock live.")
    parser.add_argument(
        "--symbols",
        default="600519.SH,000001.SZ,000858.SZ",
        help="Comma-separated symbols to test (default: 600519.SH,000001.SZ,000858.SZ)",
    )
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-06-30", help="End date YYYY-MM-DD")
    parser.add_argument("--tolerance", type=float, default=1e-6, help="Relative error tolerance on price (default: 1e-6)")
    parser.add_argument("--output", default="verify_adjust_factor_report.json", help="Path to write JSON report")
    parser.add_argument("--dry-run", action="store_true", help="Print configuration without running live verification")

    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 80)
    print("MANUAL LIVE VERIFICATION: ADJUST FACTOR IN-MEMORY DERIVATION")
    print(f"Symbols: {symbols}")
    print(f"Date Range: {args.start} -> {args.end}")
    print(f"Price Relative Error Tolerance: {args.tolerance}")
    print(f"Output Report: {args.output}")
    print("=" * 80)

    if args.dry_run:
        print("[DRY RUN] Exiting without network calls.")
        return 0

    bs_raw = BaoStockDataProvider(adjustflag="3", keep_logged_in=True)
    bs_qfq = BaoStockDataProvider(adjustflag="2", keep_logged_in=True)

    overall_pass = True
    report: Dict[str, Any] = {
        "metadata": {
            "start": args.start,
            "end": args.end,
            "tolerance": args.tolerance,
            "symbols": symbols,
        },
        "results": {},
    }

    try:
        for sym in symbols:
            print(f"Verifying {sym}...", end=" ", flush=True)
            res = verify_symbol_derivation(
                symbol=sym,
                start_date=args.start,
                end_date=args.end,
                bs_raw=bs_raw,
                bs_qfq=bs_qfq,
                tolerance=args.tolerance,
            )
            report["results"][sym] = res

            daily_status = res.get("daily", {}).get("status", "FAIL")
            intra_status = res.get("intraday_5m", {}).get("status", "FAIL")

            if daily_status != "PASS" or intra_status != "PASS":
                overall_pass = False
                print(f"[FAIL] daily={daily_status}, 5m={intra_status}")
            else:
                print("[PASS]")
    finally:
        bs_raw.logout()
        bs_qfq.logout()

    report["overall_status"] = "PASS" if overall_pass else "FAIL"

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport written to: {args.output}")
    print(f"Overall Result: {report['overall_status']}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())

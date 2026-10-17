#!/usr/bin/env python3
"""Local batch snapshot daemon and lock-free atomic Parquet writer for intraday 5m bars.

Wave 2 Track T (Task T1.5).
Provides:
- FuyaoThrottledClient: Rate-limited Fuyao client (<= 5 QPS, min 0.2s interval) with HTTP 429
  and Fuyao error code backoff, routing equities, indices, and ETFs.
- IntradayBarAggregator: Aggregates real-time/synthetic snapshot ticks into the 48 standard A-share
  5-minute buckets (09:35..11:30 and 13:05..15:00), capturing intra-poll High/Low spikes,
  differential Volume/Amount, and cumulative VWAP.
- LockFreeParquetWriter: Writes accumulated 5m bars atomically to data/intraday_5m/{date}.parquet
  via temp file + os.replace, completely avoiding DuckDB single-writer lock contention.
- EastMoneyGapBackfiller: Backfills missing intraday 5m buckets on cold start or network outages
  using EastMoneyDataProvider.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

# Add repo root to sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.data import BaseDataProvider
from common.financial_api import (
    EastMoneyDataProvider,
    is_etf_code,
    is_index_symbol,
    normalize_thscode,
)


def get_5m_bucket(ts: Union[pd.Timestamp, datetime.datetime, str]) -> Optional[pd.Timestamp]:
    """Maps a timestamp to China A-share standard 5-minute bucket (09:35..11:30, 13:05..15:00).

    Bucket boundaries:
    - 09:25..09:35: maps to 09:35:00 (first morning bar, covers opening auction).
    - 09:35..11:30: maps to ceil-5min bucket (e.g. 09:35:01..09:40:00 -> 09:40:00).
    - 11:30..13:00: lunch break, maps to 11:30:00.
    - 13:00..13:05: maps to 13:05:00 (first afternoon bar).
    - 13:05..15:00: maps to ceil-5min bucket (e.g. 14:57..15:00 -> 15:00:00, covers closing auction).
    - Outside 09:25..15:05: returns None.
    """
    ts = pd.Timestamp(ts)
    d = ts.date()
    t = ts.time()

    # Outside trading session
    if t < datetime.time(9, 25) or t > datetime.time(15, 5):
        return None

    # Morning session: 09:25..11:30
    if datetime.time(9, 25) <= t <= datetime.time(9, 35):
        return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, 9, 35, 0))
    if datetime.time(9, 35) < t <= datetime.time(11, 30):
        m = t.hour * 60 + t.minute + (1 if t.second > 0 else 0)
        ceil_m = ((m - 1) // 5 + 1) * 5
        return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, ceil_m // 60, ceil_m % 60, 0))

    # Lunch break: 11:30..13:00
    if datetime.time(11, 30) < t < datetime.time(13, 0):
        return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, 11, 30, 0))

    # Afternoon session: 13:00..15:00
    if datetime.time(13, 0) <= t <= datetime.time(13, 5):
        return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, 13, 5, 0))
    if datetime.time(13, 5) < t <= datetime.time(15, 0):
        m = t.hour * 60 + t.minute + (1 if t.second > 0 else 0)
        ceil_m = min(15 * 60, ((m - 1) // 5 + 1) * 5)
        return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, ceil_m // 60, ceil_m % 60, 0))

    # Post-market up to 15:05
    return pd.Timestamp(datetime.datetime(d.year, d.month, d.day, 15, 0, 0))


class FuyaoThrottledClient:
    """Rate-limited Fuyao API client enforcing <= 5 QPS with exponential backoff on HTTP 429."""

    def __init__(self, client: Optional[Any] = None, min_interval_sec: float = 0.2):
        self.min_interval_sec = min_interval_sec
        self._last_call_time = 0.0
        self._lock = threading.Lock()
        self.client = client
        if self.client is None:
            try:
                import fuyao_client
                self.client = fuyao_client
            except ImportError:
                self.client = None

    def _throttle(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self.min_interval_sec:
                time.sleep(self.min_interval_sec - elapsed)
            self._last_call_time = time.time()

    def _call_with_retry(self, func: Any, *args, **kwargs) -> Any:
        max_retries = 3
        for attempt in range(max_retries):
            self._throttle()
            try:
                res = func(*args, **kwargs)
                if isinstance(res, dict):
                    err_code = str(res.get("code") or res.get("error_code") or "")
                    if err_code in ("4001", "5001", "5002", "5003"):
                        # Rate limit code from Fuyao API
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                return res
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise

    def get_snapshots(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetches market snapshots for symbols, routing equities, indices, and ETFs."""
        results: Dict[str, Dict[str, Any]] = {}
        if not symbols or self.client is None:
            return results

        equities: List[str] = []
        indices: List[str] = []
        etfs: List[str] = []

        for s in symbols:
            norm = normalize_thscode(s)
            if is_index_symbol(norm):
                indices.append(norm)
            elif is_etf_code(norm):
                etfs.append(norm)
            else:
                equities.append(norm)

        # 1. Equities
        if equities and hasattr(self.client, "prices_snapshot"):
            try:
                data = self._call_with_retry(self.client.prices_snapshot, thscodes=equities)
                items = data.get("item") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for it in items:
                    sym = normalize_thscode(it.get("thscode") or it.get("code") or "")
                    if sym:
                        results[sym] = self._parse_snapshot_item(sym, it)
            except Exception as exc:
                warnings.warn(f"Fuyao equities snapshot error: {exc}")

        # 2. Indices
        if indices and hasattr(self.client, "index_prices_snapshot"):
            try:
                data = self._call_with_retry(self.client.index_prices_snapshot, thscodes=indices)
                items = data.get("item") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for it in items:
                    sym = normalize_thscode(it.get("thscode") or it.get("code") or "")
                    if sym:
                        results[sym] = self._parse_snapshot_item(sym, it)
            except Exception as exc:
                warnings.warn(f"Fuyao index snapshot error: {exc}")

        # 3. ETFs
        if etfs:
            if len(etfs) <= 5 and hasattr(self.client, "fund_market_snapshot"):
                try:
                    data = self._call_with_retry(self.client.fund_market_snapshot, thscodes=etfs)
                    items = data.get("item") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for it in items:
                        sym = normalize_thscode(it.get("thscode") or it.get("code") or "")
                        if sym:
                            results[sym] = self._parse_snapshot_item(sym, it)
                except Exception as exc:
                    warnings.warn(f"Fuyao ETF snapshot error: {exc}")
            else:
                # Fallback to EastMoney for > 5 ETFs
                em = EastMoneyDataProvider()
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                for e_sym in etfs:
                    try:
                        em_df = em.fetch_ohlcv(e_sym, start=today_str, end=today_str, interval="5m")
                        if not em_df.empty:
                            last_row = em_df.iloc[-1]
                            results[e_sym] = {
                                "symbol": e_sym,
                                "last_price": float(last_row["Close"]),
                                "open_price": float(em_df.iloc[0]["Open"]),
                                "high_price": float(em_df["High"].max()),
                                "low_price": float(em_df["Low"].min()),
                                "volume": float(em_df["Volume"].sum()),
                                "turnover": float(em_df["Amount"].sum()) if "Amount" in em_df else float(em_df["Volume"].sum() * last_row["Close"]),
                                "timestamp": em_df.index[-1],
                            }
                    except Exception:
                        pass

        return results

    @staticmethod
    def _parse_snapshot_item(symbol: str, it: Dict[str, Any]) -> Dict[str, Any]:
        last_price = float(it.get("last_price") or it.get("latest") or it.get("price") or it.get("close") or 0.0)
        open_price = float(it.get("open_price") or it.get("open") or 0.0)
        high_price = float(it.get("high_price") or it.get("high") or last_price)
        low_price = float(it.get("low_price") or it.get("low") or last_price)
        volume = float(it.get("volume") or it.get("total_volume") or it.get("vol") or 0.0)
        turnover = float(it.get("turnover") or it.get("amount") or it.get("total_turnover") or 0.0)
        ts_val = it.get("timestamp") or it.get("date") or datetime.datetime.now()
        return {
            "symbol": symbol,
            "last_price": last_price,
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "volume": volume,
            "turnover": turnover,
            "timestamp": pd.Timestamp(ts_val),
        }


class IntradayBarAggregator:
    """Aggregates real-time/synthetic snapshot ticks into 48 standard A-share 5m bars.

    Preserves intra-poll High/Low spikes, computes differential Volume/Amount,
    and cumulative VWAP.
    """

    def __init__(self):
        # symbol -> tracking state
        self._state: Dict[str, Dict[str, Any]] = {}
        # symbol -> list of completed bar dicts
        self._bars: Dict[str, List[Dict[str, Any]]] = {}

    def process_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Processes a single snapshot record for a symbol."""
        symbol = snapshot["symbol"]
        ts = pd.Timestamp(snapshot.get("timestamp") or datetime.datetime.now())
        bucket_ts = get_5m_bucket(ts)
        if bucket_ts is None:
            return

        last_p = float(snapshot.get("last_price", 0.0))
        open_p = float(snapshot.get("open_price", 0.0))
        high_p = float(snapshot.get("high_price", last_p))
        low_p = float(snapshot.get("low_price", last_p))
        cum_vol = float(snapshot.get("volume", 0.0))
        cum_amount = float(snapshot.get("turnover", snapshot.get("amount", 0.0)))

        if symbol not in self._state:
            self._state[symbol] = {
                "prev_cum_vol": 0.0,
                "prev_cum_amount": 0.0,
                "prev_day_high": None,
                "prev_day_low": None,
                "current_bucket": None,
                "bucket_open": None,
                "bucket_high": None,
                "bucket_low": None,
                "bucket_close": None,
                "bucket_cum_vol": 0.0,
                "bucket_cum_amount": 0.0,
            }
            self._bars[symbol] = []

        st = self._state[symbol]

        # Bucket transition: finalize previous bucket
        if st["current_bucket"] is not None and st["current_bucket"] != bucket_ts:
            self._finalize_bucket(symbol)

        # Initialize new bucket or update current
        if st["current_bucket"] != bucket_ts:
            st["current_bucket"] = bucket_ts
            if bucket_ts.strftime("%H:%M") == "09:35" and open_p > 0:
                st["bucket_open"] = open_p
            else:
                st["bucket_open"] = last_p
            st["bucket_close"] = last_p
            st["bucket_high"] = max(last_p, st["bucket_open"])
            st["bucket_low"] = min(last_p, st["bucket_open"])
        else:
            if st["bucket_open"] is None:
                st["bucket_open"] = last_p
            st["bucket_close"] = last_p
            st["bucket_high"] = max(st["bucket_high"], last_p)
            st["bucket_low"] = min(st["bucket_low"], last_p)

        # Intra-poll High spike capture
        if st["prev_day_high"] is not None:
            if high_p > st["prev_day_high"]:
                st["bucket_high"] = max(st["bucket_high"], high_p)
        else:
            st["bucket_high"] = max(st["bucket_high"], high_p)

        # Intra-poll Low spike capture
        if st["prev_day_low"] is not None:
            if low_p < st["prev_day_low"]:
                st["bucket_low"] = min(st["bucket_low"], low_p)
        else:
            st["bucket_low"] = min(st["bucket_low"], low_p)

        st["bucket_cum_vol"] = cum_vol
        st["bucket_cum_amount"] = cum_amount

    def _finalize_bucket(self, symbol: str) -> None:
        st = self._state[symbol]
        if st["current_bucket"] is None:
            return

        bar_ts = st["current_bucket"]
        b_open = st["bucket_open"]
        b_close = st["bucket_close"]
        b_high = max(st["bucket_high"], max(b_open, b_close))
        b_low = min(st["bucket_low"], min(b_open, b_close))

        diff_vol = max(0.0, st["bucket_cum_vol"] - st["prev_cum_vol"])
        diff_amount = max(0.0, st["bucket_cum_amount"] - st["prev_cum_amount"])

        # VWAP: cumulative intraday turnover / volume
        if st["bucket_cum_vol"] > 0:
            vwap = st["bucket_cum_amount"] / st["bucket_cum_vol"]
        else:
            vwap = (b_high + b_low + b_close) / 3.0

        bar = {
            "symbol": symbol,
            "timestamp": bar_ts,
            "Open": b_open,
            "High": b_high,
            "Low": b_low,
            "Close": b_close,
            "Volume": diff_vol,
            "Amount": diff_amount,
            "VWAP": vwap,
        }
        self._bars[symbol].append(bar)

        # Advance cumulative baselines
        st["prev_cum_vol"] = st["bucket_cum_vol"]
        st["prev_cum_amount"] = st["bucket_cum_amount"]
        if st["prev_day_high"] is None:
            st["prev_day_high"] = b_high
        else:
            st["prev_day_high"] = max(st["prev_day_high"], b_high)

        if st["prev_day_low"] is None:
            st["prev_day_low"] = b_low
        else:
            st["prev_day_low"] = min(st["prev_day_low"], b_low)

        st["current_bucket"] = None

    def finalize_all(self) -> None:
        """Finalizes any pending in-progress buckets for all symbols."""
        for sym in list(self._state.keys()):
            if self._state[sym]["current_bucket"] is not None:
                self._finalize_bucket(sym)

    def get_bars_df(self, symbol: Optional[str] = None, finalize: bool = False) -> pd.DataFrame:
        """Returns collected bars as a DataFrame."""
        if finalize:
            self.finalize_all()

        records = []
        symbols = [symbol] if symbol else list(self._bars.keys())
        for sym in symbols:
            records.extend(self._bars.get(sym, []))

        if not records:
            return pd.DataFrame(
                columns=["symbol", "timestamp", "Open", "High", "Low", "Close", "Volume", "Amount", "VWAP"]
            )
        df = pd.DataFrame(records)
        df = df.sort_values(by=["symbol", "timestamp"]).reset_index(drop=True)
        return df


class LockFreeParquetWriter:
    """Atomic lock-free writer saving 5m bars to data/intraday_5m/{date}.parquet.

    Completely avoids DuckDB single-writer process lock contention.
    """

    def __init__(self, output_dir: str = "data/intraday_5m"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def write_bars(self, df: pd.DataFrame, date: str) -> str:
        """Atomically writes df to {output_dir}/{date}.parquet using a temp file."""
        if not isinstance(df, pd.DataFrame):
            raise TypeError("df must be a pandas DataFrame")

        target_path = os.path.join(self.output_dir, f"{date}.parquet")
        tmp_filename = f".tmp_{date}_{os.getpid()}_{time.time_ns()}.parquet"
        tmp_path = os.path.join(self.output_dir, tmp_filename)

        # Merge with existing file if present
        if os.path.isfile(target_path):
            try:
                existing_df = pd.read_parquet(target_path)
                combined = pd.concat([existing_df, df], ignore_index=True)
                if "symbol" in combined.columns and "timestamp" in combined.columns:
                    combined = combined.drop_duplicates(subset=["symbol", "timestamp"], keep="last")
                df_to_write = combined.sort_values(by=["symbol", "timestamp"]).reset_index(drop=True)
            except Exception:
                df_to_write = df
        else:
            df_to_write = df

        try:
            df_to_write.to_parquet(tmp_path, index=False)
        except Exception:
            # Fallback using DuckDB
            import duckdb
            con = duckdb.connect()
            con.register("tmp_table", df_to_write)
            con.execute(f"COPY tmp_table TO '{tmp_path}' (FORMAT PARQUET)")
            con.close()

        # Atomic rename
        os.replace(tmp_path, target_path)
        return target_path

    def cleanup_old_parquet_files(self, retention_days: int = 90) -> List[str]:
        """Cleans up parquet files older than retention_days and orphaned temp files."""
        return cleanup_old_parquet_files(self.output_dir, retention_days=retention_days)


def cleanup_old_parquet_files(directory: str, retention_days: int = 90) -> List[str]:
    """Deletes parquet files older than retention_days and temp files older than 1 day."""
    deleted: List[str] = []
    now = time.time()
    one_day_sec = 86400.0

    if not os.path.isdir(directory):
        return deleted

    for fname in os.listdir(directory):
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue

        # Orphaned temp files older than 1 day
        if fname.startswith(".tmp_"):
            try:
                if now - os.path.getmtime(fpath) > one_day_sec:
                    os.remove(fpath)
                    deleted.append(fpath)
            except Exception:
                pass
            continue

        # Daily parquet files
        if fname.endswith(".parquet"):
            stem = fname[:-8]
            try:
                file_date = datetime.datetime.strptime(stem, "%Y-%m-%d").date()
                age_days = (datetime.date.today() - file_date).days
                if age_days > retention_days:
                    os.remove(fpath)
                    deleted.append(fpath)
                    continue
            except ValueError:
                pass

            # Fallback by mtime
            if now - os.path.getmtime(fpath) > retention_days * 86400:
                try:
                    os.remove(fpath)
                    deleted.append(fpath)
                except Exception:
                    pass

    return deleted


class EastMoneyGapBackfiller:
    """Detects missing 5m buckets and backfills them from EastMoneyDataProvider."""

    def __init__(self, provider: Optional[BaseDataProvider] = None):
        if provider is None:
            self.provider: BaseDataProvider = EastMoneyDataProvider()
        else:
            self.provider = provider

    def backfill_symbol(
        self,
        symbol: str,
        date: str,
        existing_bars: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Backfills missing 5m bars for symbol on date using EastMoney."""
        try:
            em_df = self.provider.fetch_ohlcv(symbol, start=date, end=date, interval="5m")
        except Exception as exc:
            warnings.warn(f"Gap backfill failed for {symbol} on {date}: {exc}")
            return existing_bars if existing_bars is not None else pd.DataFrame()

        if em_df.empty:
            return existing_bars if existing_bars is not None else pd.DataFrame()

        em_records = []
        for ts, row in em_df.iterrows():
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
            v = float(row["Volume"])
            a = float(row["Amount"]) if "Amount" in row and not np.isnan(row["Amount"]) else float(c * v)
            vwap = float(row["VWAP"]) if "VWAP" in row and not np.isnan(row["VWAP"]) else (a / v if v > 0 else (h + l + c) / 3.0)
            em_records.append({
                "symbol": symbol,
                "timestamp": pd.Timestamp(ts),
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "Volume": v,
                "Amount": a,
                "VWAP": vwap,
            })

        new_df = pd.DataFrame(em_records)
        if existing_bars is None or existing_bars.empty:
            return new_df

        combined = pd.concat([existing_bars, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["symbol", "timestamp"], keep="last")
        combined = combined.sort_values(by="timestamp").reset_index(drop=True)
        return combined


def parse_args():
    parser = argparse.ArgumentParser(description="Record intraday 5m bars into atomic Parquet files.")
    parser.add_argument("--universe-file", type=str, default="", help="Path to universe file with symbols.")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated symbols.")
    parser.add_argument("--all-market", action="store_true", help="Fetch all market equities.")
    parser.add_argument("--output-dir", type=str, default="data/intraday_5m", help="Output Parquet directory.")
    parser.add_argument("--poll-interval", type=float, default=30.0, help="Poll interval in seconds.")
    parser.add_argument("--once", action="store_true", help="Run a single poll iteration and exit.")
    parser.add_argument("--max-iterations", type=int, default=0, help="Max polling iterations (0 = unlimited).")
    parser.add_argument("--backfill", action="store_true", help="Backfill missing buckets via EastMoney on start.")
    return parser.parse_args()


def main():
    args = parse_args()
    symbols: List[str] = []
    if args.symbols:
        symbols.extend([s.strip() for s in args.symbols.split(",") if s.strip()])
    if args.universe_file and os.path.isfile(args.universe_file):
        with open(args.universe_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    symbols.append(line)

    symbols = sorted(list(set(symbols)))
    if not symbols and not args.all_market:
        print("No symbols specified. Use --symbols or --universe-file or --all-market.")
        return

    client = FuyaoThrottledClient()
    aggregator = IntradayBarAggregator()
    writer = LockFreeParquetWriter(output_dir=args.output_dir)
    backfiller = EastMoneyGapBackfiller() if args.backfill else None

    print(f"Starting intraday 5m recorder for {len(symbols)} symbols. Output dir: {args.output_dir}")

    iteration = 0
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    while True:
        iteration += 1
        try:
            snapshots = client.get_snapshots(symbols)
            for sym, snap in snapshots.items():
                aggregator.process_snapshot(snap)

            bars_df = aggregator.get_bars_df()
            if not bars_df.empty:
                writer.write_bars(bars_df, today_str)

        except Exception as exc:
            warnings.warn(f"Iteration {iteration} error: {exc}")

        if args.once or (args.max_iterations > 0 and iteration >= args.max_iterations):
            aggregator.finalize_all()
            final_df = aggregator.get_bars_df()
            if not final_df.empty:
                writer.write_bars(final_df, today_str)
            print("Recorder finished.")
            break

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()

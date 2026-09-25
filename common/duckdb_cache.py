"""DuckDB-backed OHLCV cache system referencing MarketDB architecture.

Provides high-performance, single-file columnar storage (cache.duckdb) for historical
price bars. Bars and metadata are strictly keyed by (provider, symbol), allowing base
daily bars to dynamically serve any date range and any interval (via OHLCV resampling).
Tracks each asset's earliest available date from the upstream provider to eliminate
redundant pre-inception queries. Cached historical data never expires.
"""

from __future__ import annotations

import datetime
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import warnings

import duckdb
import numpy as np
import pandas as pd


_REQUIRED_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Resampling frequency mapping
_INTERVAL_RULES: Dict[str, Optional[str]] = {
    "1d": None,
    "d": None,
    "daily": None,
    "1wk": "W-FRI",
    "w": "W-FRI",
    "1w": "W-FRI",
    "weekly": "W-FRI",
    "1mo": "ME",
    "m": "ME",
    "1m": "ME",
    "monthly": "ME",
    "1q": "QE",
    "q": "QE",
    "quarterly": "QE",
    "1y": "YE",
    "y": "YE",
    "yearly": "YE",
}


def _get_resample_rule(interval: str) -> Optional[str]:
    """Returns pandas resample rule or None if interval is daily."""
    norm = interval.strip().lower()
    if norm in _INTERVAL_RULES:
        return _INTERVAL_RULES[norm]
    # Check pandas version for ME vs M
    if norm.endswith("mo") or norm.endswith("m"):
        return "ME"
    return norm


def resample_ohlcv(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Resamples a daily OHLCV DataFrame with DatetimeIndex to a target interval
    (e.g., '1wk', '1mo') using standard financial aggregation rules.
    """
    if df.empty:
        return df

    rule = _get_resample_rule(interval)
    if not rule:
        return df

    agg_spec: Dict[str, str] = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    }
    if "Volume" in df.columns:
        agg_spec["Volume"] = "sum"

    resampled = df.resample(rule).agg(agg_spec).dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" in resampled.columns:
        resampled["Volume"] = resampled["Volume"].fillna(0.0)

    return resampled


def is_synthetic_provider(provider_name_or_instance: Any) -> bool:
    """Returns True if the provider is a synthetic Brownian motion data generator.
    Synthetic data is generated on-the-fly and must always bypass the cache DB.
    """
    if provider_name_or_instance is None:
        return False
    if isinstance(provider_name_or_instance, str):
        name = provider_name_or_instance.strip()
    else:
        name = type(provider_name_or_instance).__name__

    name_lower = name.lower()
    if name_lower in ("synthetic", "syntheticdataprovider"):
        return True
    if name_lower.startswith("synthetic"):
        return True
    return False


class DuckDBCache:
    """DuckDB columnar cache for multi-provider OHLCV bar data and asset metadata.

    Schema:
      - ohlcv_bars: (provider, symbol, date) -> open, high, low, close, volume
      - asset_sync_metadata: (provider, symbol) -> earliest_available_date, is_earliest_known,
                                                  latest_available_date, last_sync_time
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None or db_path == ":memory:":
            self.db_path = ":memory:"
            self.is_memory = True
            self._mem_con = duckdb.connect(":memory:")
            self._init_tables(self._mem_con)
        else:
            abs_p = os.path.abspath(db_path)
            if os.path.isdir(abs_p) or not (abs_p.endswith(".duckdb") or abs_p.endswith(".db")):
                os.makedirs(abs_p, exist_ok=True)
                self.db_path = os.path.join(abs_p, "cache.duckdb")
            else:
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                self.db_path = abs_p
            self.is_memory = False
            self._mem_con = None
            # Initialize tables on disk file
            con = self._get_connection(read_only=False)
            try:
                self._init_tables(con)
            finally:
                con.close()

    def _get_connection(self, read_only: bool = False):
        if self.is_memory:
            return self._mem_con

        # Transient write-lock retry loop
        for attempt in range(5):
            try:
                return duckdb.connect(self.db_path, read_only=read_only)
            except Exception as exc:
                if "lock" in str(exc).lower() and attempt < 4:
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise

    def _init_tables(self, con) -> None:
        """Initializes ohlcv_bars and asset_sync_metadata tables."""
        con.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv_bars (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                date DATE NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume DOUBLE NOT NULL,
                PRIMARY KEY (provider, symbol, date)
            );
            CREATE TABLE IF NOT EXISTS asset_sync_metadata (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                earliest_available_date DATE NOT NULL,
                is_earliest_known BOOLEAN NOT NULL DEFAULT FALSE,
                latest_available_date DATE NOT NULL,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup ON ohlcv_bars(provider, symbol, date);
        """)

    def get_asset_metadata(self, provider: str, symbol: str) -> Optional[dict]:
        """Returns synchronization metadata for (provider, symbol), or None if not found."""
        if is_synthetic_provider(provider):
            return None

        con = self._get_connection(read_only=True)
        try:
            row = con.execute(
                "SELECT earliest_available_date, is_earliest_known, latest_available_date, last_sync_time "
                "FROM asset_sync_metadata WHERE provider = ? AND symbol = ?",
                [provider, symbol],
            ).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        if not row:
            return None

        return {
            "earliest_available_date": row[0],
            "is_earliest_known": bool(row[1]),
            "latest_available_date": row[2],
            "last_sync_time": row[3],
        }

    def is_range_covered(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        now: Optional[pd.Timestamp] = None,
    ) -> bool:
        """Checks if the requested date range [start, end] is fully covered by cached bars.
        Cached historical data never expires.
        """
        if is_synthetic_provider(provider):
            return False

        meta = self.get_asset_metadata(provider, symbol)
        if not meta:
            return False

        earliest_avail = meta["earliest_available_date"]
        is_earliest_known = meta["is_earliest_known"]
        latest_avail = meta["latest_available_date"]

        # Start coverage check
        if start:
            req_start_date = pd.to_datetime(start).date()
            if req_start_date >= earliest_avail - datetime.timedelta(days=7):
                start_covered = True
            else:
                start_covered = is_earliest_known
        else:
            start_covered = is_earliest_known

        # End coverage check
        current_time = now if now is not None else pd.Timestamp.now()
        current_date = current_time.date()
        if end:
            req_end_date = pd.to_datetime(end).date()
            if req_end_date > current_date:
                # End is in the future; data up to today covers as much as possible
                end_covered = (current_date - latest_avail) <= datetime.timedelta(days=7)
            else:
                end_covered = (req_end_date - latest_avail) <= datetime.timedelta(days=7)
        else:
            end_covered = True

        if not (start_covered and end_covered):
            return False

        # Verify bars exist in the table
        con = self._get_connection(read_only=True)
        try:
            count = con.execute(
                "SELECT count(*) FROM ohlcv_bars WHERE provider = ? AND symbol = ?",
                [provider, symbol],
            ).fetchone()[0]
        finally:
            if not self.is_memory:
                con.close()

        return count > 0

    def store_bars(
        self,
        provider: str,
        symbol: str,
        df: pd.DataFrame,
        requested_start: Optional[str] = None,
        requested_end: Optional[str] = None,
        sync_time: Optional[datetime.datetime] = None,
    ) -> None:
        """Upserts daily OHLCV bars into ohlcv_bars and updates asset_sync_metadata."""
        if is_synthetic_provider(provider) or df.empty:
            return

        # Ensure required price columns; default Volume to 0.0 if omitted
        for col in ["Open", "High", "Low", "Close"]:
            if col not in df.columns:
                raise ValueError(f"DataFrame missing required OHLCV column '{col}'")

        df_to_store = pd.DataFrame(index=pd.to_datetime(df.index))
        for col in ["Open", "High", "Low", "Close"]:
            df_to_store[col] = df[col]
        df_to_store["Volume"] = df["Volume"] if "Volume" in df.columns else 0.0
        min_date = df_to_store.index.min().date()
        max_date = df_to_store.index.max().date()

        # Update metadata state
        meta = self.get_asset_metadata(provider, symbol)
        if meta:
            earliest_avail = min(meta["earliest_available_date"], min_date)
            latest_avail = max(meta["latest_available_date"], max_date)
            is_earliest_known = meta["is_earliest_known"]
        else:
            earliest_avail = min_date
            latest_avail = max_date
            is_earliest_known = False

        # If user requested earlier than returned min_date - 7 days, provider has no earlier data
        if requested_start:
            req_start_date = pd.to_datetime(requested_start).date()
            if req_start_date < min_date - datetime.timedelta(days=7):
                earliest_avail = min_date
                is_earliest_known = True

        sync_ts = sync_time if sync_time is not None else datetime.datetime.now()

        # Prepare records for insertion
        records_df = pd.DataFrame({
            "provider": provider,
            "symbol": symbol,
            "date": df_to_store.index.date,
            "open": df_to_store["Open"].astype(float).values,
            "high": df_to_store["High"].astype(float).values,
            "low": df_to_store["Low"].astype(float).values,
            "close": df_to_store["Close"].astype(float).values,
            "volume": df_to_store["Volume"].astype(float).values,
        })

        con = self._get_connection(read_only=False)
        try:
            con.register("temp_bars_to_insert", records_df)
            con.execute("""
                INSERT OR REPLACE INTO ohlcv_bars
                SELECT provider, symbol, date, open, high, low, close, volume
                FROM temp_bars_to_insert
            """)
            con.execute("""
                INSERT OR REPLACE INTO asset_sync_metadata
                VALUES (?, ?, ?, ?, ?, ?)
            """, [provider, symbol, earliest_avail, is_earliest_known, latest_avail, sync_ts])
        finally:
            if not self.is_memory:
                con.close()

    def query_bars(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Retrieves OHLCV bars for (provider, symbol) across [start, end], resampled to interval."""
        clauses = ["provider = ?", "symbol = ?"]
        params: List[Any] = [provider, symbol]

        if start:
            clauses.append("date >= ?")
            params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
        if end:
            clauses.append("date <= ?")
            params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))

        sql = (
            f"SELECT date, open, high, low, close, volume FROM ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY date"
        )

        con = self._get_connection(read_only=True)
        try:
            res_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        if res_df.empty:
            return pd.DataFrame(columns=_REQUIRED_OHLCV_COLUMNS)

        res_df["date"] = pd.to_datetime(res_df["date"])
        res_df = res_df.set_index("date").sort_index()
        res_df = res_df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )[_REQUIRED_OHLCV_COLUMNS].astype(float)

        return resample_ohlcv(res_df, interval)

    def query_universe(
        self,
        provider: str,
        symbols: List[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
    ) -> Dict[str, pd.DataFrame]:
        """Batch-queries OHLCV bars for multiple symbols in a single SQL query."""
        if not symbols:
            return {}

        unique_symbols = list(dict.fromkeys(symbols))
        placeholders = ",".join(["?"] * len(unique_symbols))
        clauses = ["provider = ?", f"symbol IN ({placeholders})"]
        params: List[Any] = [provider, *unique_symbols]

        if start:
            clauses.append("date >= ?")
            params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
        if end:
            clauses.append("date <= ?")
            params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))

        sql = (
            f"SELECT symbol, date, open, high, low, close, volume FROM ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol, date"
        )

        con = self._get_connection(read_only=True)
        try:
            full_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        result: Dict[str, pd.DataFrame] = {}
        if full_df.empty:
            return result

        full_df["date"] = pd.to_datetime(full_df["date"])
        grouped = full_df.groupby("symbol")

        for sym in unique_symbols:
            if sym not in grouped.groups:
                continue
            group = grouped.get_group(sym)
            df = group.set_index("date").sort_index()
            df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )[_REQUIRED_OHLCV_COLUMNS].astype(float)
            result[sym] = resample_ohlcv(df, interval)

        return result

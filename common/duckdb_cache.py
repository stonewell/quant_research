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
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING
import warnings

import duckdb
import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from .data import FundamentalMetrics


_REQUIRED_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

_INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "1h"}


def is_intraday_interval(interval: str) -> bool:
    """Returns True if the interval represents an intraday frequency."""
    if not interval or not isinstance(interval, str):
        return False
    return interval.strip().lower() in _INTRADAY_INTERVALS


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
    "mo": "ME",
    "m": "ME",
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
    if not interval or not isinstance(interval, str):
        return None
    norm = interval.strip().lower()
    if is_intraday_interval(norm):
        return None
    if norm in _INTERVAL_RULES:
        return _INTERVAL_RULES[norm]
    # Check pandas version for ME vs M
    if norm.endswith("mo"):
        return "ME"
    return norm


def _parse_intraday_end(end: Any) -> Optional[pd.Timestamp]:
    """R13: If end is date-only (e.g. len <= 10 or no ':' in string), expand to 23:59:59.999999."""
    if end is None:
        return None
    end_dt = pd.to_datetime(end)
    end_str = str(end).strip()
    if isinstance(end, datetime.date) and not isinstance(end, datetime.datetime):
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif len(end_str) <= 10 or ":" not in end_str:
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif end_dt.time() == datetime.time(0, 0, 0) and ("00:00" not in end_str):
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end_dt


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
        """Initializes ohlcv_bars, asset_sync_metadata, and intraday cache tables."""
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

            CREATE TABLE IF NOT EXISTS intraday_ohlcv_bars (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                interval VARCHAR NOT NULL,
                ts TIMESTAMP NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume DOUBLE NOT NULL,
                PRIMARY KEY (provider, symbol, interval, ts)
            );

            CREATE TABLE IF NOT EXISTS intraday_sync_metadata (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                interval VARCHAR NOT NULL,
                earliest_available_ts TIMESTAMP NOT NULL,
                is_earliest_known BOOLEAN NOT NULL DEFAULT FALSE,
                latest_available_ts TIMESTAMP NOT NULL,
                latest_close_ref DOUBLE,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol, interval)
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_lookup ON intraday_ohlcv_bars(provider, symbol, interval, ts);

            CREATE TABLE IF NOT EXISTS stock_fundamentals_annual (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                report_year INTEGER NOT NULL,
                stat_date DATE NOT NULL,
                roe DOUBLE,
                net_profit DOUBLE,
                net_profit_5yr_cagr DOUBLE,
                cfo DOUBLE,
                cfo_to_net_profit DOUBLE,
                total_assets DOUBLE,
                total_liabilities DOUBLE,
                debt_to_assets DOUBLE,
                debt_to_equity DOUBLE,
                interest_bearing_debt DOUBLE,
                dividend_yield DOUBLE,
                dividend_payout_ratio DOUBLE,
                pe_ttm DOUBLE,
                pb DOUBLE,
                total_shares DOUBLE,
                market_cap DOUBLE,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol, report_year)
            );

            CREATE TABLE IF NOT EXISTS stock_industry_metadata (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                industry_name VARCHAR NOT NULL,
                industry_tier VARCHAR NOT NULL,
                terminal_pe DOUBLE NOT NULL,
                is_blacklisted BOOLEAN NOT NULL DEFAULT FALSE,
                blacklist_reason VARCHAR,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_fundamentals_lookup ON stock_fundamentals_annual(provider, symbol, report_year);
            CREATE INDEX IF NOT EXISTS idx_industry_metadata_lookup ON stock_industry_metadata(provider, symbol);
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

    def get_intraday_metadata(self, provider: str, symbol: str, interval: str = "5m") -> Optional[dict]:
        """Returns synchronization metadata for (provider, symbol, interval), or None if not found."""
        if is_synthetic_provider(provider):
            return None

        con = self._get_connection(read_only=True)
        try:
            row = con.execute(
                "SELECT earliest_available_ts, is_earliest_known, latest_available_ts, latest_close_ref, last_sync_time "
                "FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
                [provider, symbol, interval],
            ).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        if not row:
            return None

        return {
            "earliest_available_ts": pd.Timestamp(row[0]),
            "is_earliest_known": bool(row[1]),
            "latest_available_ts": pd.Timestamp(row[2]),
            "latest_close_ref": float(row[3]) if row[3] is not None else None,
            "last_sync_time": pd.Timestamp(row[4]) if row[4] is not None else None,
        }

    def is_intraday_range_covered(
        self,
        provider: str,
        symbol: str,
        interval: str = "5m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        now: Optional[pd.Timestamp] = None,
    ) -> bool:
        """Checks if the requested intraday date/time range [start, end] is fully covered by cached bars."""
        if is_synthetic_provider(provider):
            return False

        meta = self.get_intraday_metadata(provider, symbol, interval)
        if not meta:
            return False

        earliest_avail = meta["earliest_available_ts"]
        is_earliest_known = meta["is_earliest_known"]
        latest_avail = meta["latest_available_ts"]

        # Start coverage check
        if start:
            req_start_ts = pd.to_datetime(start)
            if req_start_ts >= earliest_avail - datetime.timedelta(days=7):
                start_covered = True
            else:
                start_covered = is_earliest_known
        else:
            start_covered = is_earliest_known

        # End coverage check
        current_time = now if now is not None else pd.Timestamp.now()
        current_date = current_time.date()

        if end:
            end_dt = _parse_intraday_end(end)
            req_end_date = pd.to_datetime(end).date()
        else:
            end_dt = current_time
            req_end_date = current_date

        last_sync = meta.get("last_sync_time")
        if isinstance(last_sync, (datetime.datetime, pd.Timestamp)):
            last_sync_date = last_sync.date()
        elif last_sync is not None:
            last_sync_date = pd.to_datetime(last_sync).date()
        else:
            last_sync_date = None

        latest_avail_date = latest_avail.date()

        if req_end_date >= current_date:
            if latest_avail >= current_time or latest_avail_date > current_date:
                end_covered = True
            elif (
                last_sync_date is not None
                and last_sync_date >= current_date
                and (current_date - latest_avail_date) <= datetime.timedelta(days=7)
            ):
                end_covered = True
            else:
                end_covered = False
        else:
            if latest_avail >= end_dt or latest_avail_date >= req_end_date:
                end_covered = True
            else:
                end_covered = (req_end_date - latest_avail_date) <= datetime.timedelta(days=7)

        if not (start_covered and end_covered):
            return False

        con = self._get_connection(read_only=True)
        try:
            count = con.execute(
                "SELECT count(*) FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ?",
                [provider, symbol, interval],
            ).fetchone()[0]
        finally:
            if not self.is_memory:
                con.close()

        return count > 0

    def is_range_covered(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        now: Optional[pd.Timestamp] = None,
        interval: str = "1d",
    ) -> bool:
        """Checks if the requested date range [start, end] is fully covered by cached bars.
        Cached historical data never expires.

        Coverage is a TWO-layer contract, and both layers must pass:

          1. Metadata layer (`asset_sync_metadata`): the provider's earliest/latest known
             dates plus `last_sync_time` decide whether cache *bookkeeping* says this range
             has been synced (weekend/holiday-tolerant 7-day window, plus a "did anybody
             already sync today?" short-circuit).
          2. Bar layer (`ohlcv_bars`): the maximum stored bar date must actually reach the
             requested end. The metadata layer alone records what a sync *attempted*, not
             what landed on disk, so it can never be trusted by itself.

        Why layer 2 exists (2026-10-22 live incident): a live run requested daily bars
        ending 2026-10-22. Fourteen symbols were fetched after the upstream provider had
        published that day's bar; six were fetched *before* publication, so their bars
        stopped at 2026-10-21 while `asset_sync_metadata.latest_available_date` and
        `last_sync_time` still said "synced today". The old layer-1-only `count(*) > 0`
        check therefore reported those six as covered for the rest of the calendar day,
        they were never re-fetched, and the live path fell back to `.iloc[-1]` = the
        2026-10-21 close: five held positions were marked to stale prices, NAV was
        understated by ~0.70%, and suggested limit prices came out below the true market.
        Never accept "metadata says today but bars stop yesterday".

        Contract for callers: pass a *trading-session* date as `end`. This check is
        deliberately calendar-free (the market clock is owned elsewhere), so it tolerates
        the one unambiguous non-session case it can detect without a holiday calendar: an
        `end` falling on a Saturday/Sunday is treated as covered once bars reach the
        preceding Friday -- otherwise every weekend run would look uncovered forever and
        re-fetch pointlessly. Weekday exchange holidays are NOT inferred: if a caller passes
        a weekday holiday as `end`, the bars legitimately stop on the prior session and this
        method reports "uncovered", costing one redundant upstream fetch. That is a
        deliberate fail-safe trade -- a redundant fetch is cheap, a stale mark-to-market is
        not.

        An `end` in the future (beyond today) is never judged by the bar layer: no provider
        could have a bar for a date that has not happened yet.

        The 2026-10-22 staleness defect historically existed in a second, independent
        implementation (`common/data.py::_cache_covers_requested_end`); that helper had
        zero production callers and was removed as dead code on 2026-10-22, so this
        method is now the only freshness gate. If a non-DuckDB disk cache is ever added,
        port the rule FROM HERE, not from a remembered copy.
        """
        if isinstance(now, str) and (is_intraday_interval(now) or now in ("1d", "daily")):
            interval = now
            now = None
        if is_intraday_interval(interval):
            return self.is_intraday_range_covered(
                provider, symbol, interval=interval, start=start, end=end, now=now
            )
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
        req_end_date = pd.to_datetime(end).date() if end else current_date

        last_sync = meta.get("last_sync_time")
        if isinstance(last_sync, (datetime.datetime, pd.Timestamp)):
            last_sync_date = last_sync.date()
        elif last_sync is not None:
            last_sync_date = pd.to_datetime(last_sync).date()
        else:
            last_sync_date = None

        if req_end_date >= current_date:
            # End is today or in the future
            if latest_avail >= current_date:
                end_covered = True
            elif (
                last_sync_date is not None
                and last_sync_date >= current_date
                and (current_date - latest_avail) <= datetime.timedelta(days=7)
            ):
                # Already synced today; recent latest_avail (within weekend/holiday tolerance) is as fresh as possible
                end_covered = True
            else:
                # Has not been synced today; re-query upstream for today's market data
                end_covered = False
        else:
            # End is in the past (historical query)
            if latest_avail >= req_end_date:
                end_covered = True
            else:
                end_covered = (req_end_date - latest_avail) <= datetime.timedelta(days=7)

        if not (start_covered and end_covered):
            return False

        # Layer 2 (2026-10-22 incident -- see docstring): metadata freshness is NOT proof that a
        # bar for the requested end date actually landed in the table. Read the last bar at or
        # before `req_end_date` and require it to reach `req_end_date`. A NULL result also means
        # the symbol has no bars at all in/ before this range -- this replaces the old
        # `count(*) > 0` check, which reported "covered" even for a symbol whose only bars sat
        # entirely outside the requested range.
        con = self._get_connection(read_only=True)
        try:
            max_row = con.execute(
                "SELECT MAX(date) FROM ohlcv_bars WHERE provider = ? AND symbol = ? AND date <= ?",
                [provider, symbol, req_end_date],
            ).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        max_bar_date = max_row[0] if max_row else None
        if max_bar_date is None:
            return False
        if isinstance(max_bar_date, (pd.Timestamp, datetime.datetime)):
            max_bar_date = max_bar_date.date()

        if max_bar_date < req_end_date and req_end_date <= current_date:
            # Weekend tolerance: a Saturday/Sunday `end` can never have a bar, so "bars reach the
            # preceding Friday" counts as covered. Without it every weekend run would look
            # uncovered forever and re-fetch the same data pointlessly. Weekday exchange
            # holidays are intentionally NOT inferred (see the docstring's caller contract).
            if req_end_date.weekday() < 5:
                return False

        return True

    def store_intraday_bars(
        self,
        provider: str,
        symbol: str,
        df: pd.DataFrame,
        interval: str = "5m",
        requested_start: Optional[str] = None,
        requested_end: Optional[str] = None,
        sync_time: Optional[datetime.datetime] = None,
    ) -> None:
        """Upserts intraday OHLCV bars into intraday_ohlcv_bars and updates intraday_sync_metadata.
        Enforces R4 QFQ Split/Dividend Drift detection and R13 End-Date expansion.
        """
        if is_synthetic_provider(provider) or df.empty:
            return

        for col in ["Open", "High", "Low", "Close"]:
            if col not in df.columns:
                raise ValueError(f"DataFrame missing required OHLCV column '{col}'")

        df_to_store = pd.DataFrame(index=pd.to_datetime(df.index))
        for col in ["Open", "High", "Low", "Close"]:
            df_to_store[col] = df[col].astype(float)
        df_to_store["Volume"] = df["Volume"].astype(float) if "Volume" in df.columns else 0.0
        df_to_store = df_to_store.sort_index()

        min_ts = df_to_store.index.min()
        max_ts = df_to_store.index.max()

        # R4 QFQ Split/Dividend Drift Check
        meta = self.get_intraday_metadata(provider, symbol, interval)
        if meta and meta.get("latest_close_ref") is not None and meta["latest_close_ref"] > 0:
            cached_ts = pd.Timestamp(meta["latest_available_ts"])
            if cached_ts in df_to_store.index:
                val = df_to_store.loc[cached_ts, "Close"]
                new_close = float(val.iloc[-1] if isinstance(val, (pd.Series, np.ndarray)) else val)
                ref_close = float(meta["latest_close_ref"])
                if abs(new_close - ref_close) / ref_close > 1e-4:
                    # Drift detected: purge all existing bars & metadata for this asset/interval
                    con = self._get_connection(read_only=False)
                    try:
                        con.execute(
                            "DELETE FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ?",
                            [provider, symbol, interval],
                        )
                        con.execute(
                            "DELETE FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
                            [provider, symbol, interval],
                        )
                    finally:
                        if not self.is_memory:
                            con.close()
                    meta = None

        if meta:
            earliest_avail = min(pd.Timestamp(meta["earliest_available_ts"]), pd.Timestamp(min_ts))
            latest_avail = max(pd.Timestamp(meta["latest_available_ts"]), pd.Timestamp(max_ts))
            is_earliest_known = meta["is_earliest_known"]
        else:
            earliest_avail = pd.Timestamp(min_ts)
            latest_avail = pd.Timestamp(max_ts)
            is_earliest_known = False

        if requested_start:
            req_start_ts = pd.to_datetime(requested_start)
            if req_start_ts < min_ts - datetime.timedelta(days=7):
                earliest_avail = pd.Timestamp(min_ts)
                is_earliest_known = True

        latest_close_ref = float(df_to_store["Close"].iloc[-1])
        sync_ts = sync_time if sync_time is not None else datetime.datetime.now()

        records_df = pd.DataFrame({
            "provider": provider,
            "symbol": symbol,
            "interval": interval,
            "ts": df_to_store.index,
            "open": df_to_store["Open"].values,
            "high": df_to_store["High"].values,
            "low": df_to_store["Low"].values,
            "close": df_to_store["Close"].values,
            "volume": df_to_store["Volume"].values,
        })

        con = self._get_connection(read_only=False)
        try:
            con.register("temp_intraday_bars_to_insert", records_df)
            con.execute("""
                INSERT OR REPLACE INTO intraday_ohlcv_bars
                SELECT provider, symbol, interval, ts, open, high, low, close, volume
                FROM temp_intraday_bars_to_insert
            """)
            con.execute("""
                INSERT OR REPLACE INTO intraday_sync_metadata
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                provider,
                symbol,
                interval,
                earliest_avail.to_pydatetime(),
                is_earliest_known,
                latest_avail.to_pydatetime(),
                latest_close_ref,
                sync_ts,
            ])
        finally:
            if not self.is_memory:
                con.close()

    def store_bars(
        self,
        provider: str,
        symbol: str,
        df: pd.DataFrame,
        requested_start: Optional[str] = None,
        requested_end: Optional[str] = None,
        sync_time: Optional[datetime.datetime] = None,
        interval: str = "1d",
    ) -> None:
        """Upserts daily OHLCV bars into ohlcv_bars and updates asset_sync_metadata.

        Metadata is updated with a MONOTONIC upsert (see the long comment at the write site):
        `earliest_available_date` can only move earlier, `latest_available_date` only later, and
        `is_earliest_known` becomes True at most once (never reverts). Storing a window that
        starts later than the known history therefore leaves the recorded earliest date intact --
        "we did not look earlier" is not "it is not there" (2026-10-22 downgrade incident).
        """
        if is_intraday_interval(interval):
            return self.store_intraday_bars(
                provider,
                symbol,
                df,
                interval=interval,
                requested_start=requested_start,
                requested_end=requested_end,
                sync_time=sync_time,
            )

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
        frame_min_date = df_to_store.index.min().date()
        frame_max_date = df_to_store.index.max().date()

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

            # ------------------------------------------------------------------------------
            # MONOTONIC `asset_sync_metadata` upsert (fix for the 2026-10-22 downgrade)
            #
            # 2026-10-22 21:34:31 incident: a real Fuyao sync that started fetching at
            # 2024-01-02 rewrote `earliest_available_date` to 2024-01-02 for six symbols whose
            # `ohlcv_bars` still begin in 2017-01-18 / 2020-11-30 / 2021-08-20, and cleared
            # `is_earliest_known` on 601728.SH (True -> False). The metadata then contradicted
            # the data it describes, and anything deriving a backtestable range from
            # `earliest_available_date` silently truncated ~7 years of history. Every sync from
            # a later start date damaged the field further.
            #
            # The invariant enforced below is directional on purpose:
            #   * earliest_available_date: MIN(existing, incoming) -- NEVER GREATEST. "We did not
            #     look earlier" is NOT "it isn't there": a sync beginning at 2024-01-02 has
            #     learned nothing about 2017 and must not overwrite that knowledge. Only a sync
            #     that genuinely reaches further back may move it (MIN still allows that).
            #   * is_earliest_known: monotonic True -- `existing OR incoming`, so a sync that
            #     says False can never erase a previously confirmed True (only a human/explicit
            #     UPDATE may reset it).
            #   * latest_available_date: MAX(existing, incoming) -- a stale fetch must not roll
            #     the recorded latest date backwards.
            #   * last_sync_time: overwritten freely; it records the last *attempt*, not a fact
            #     about the data.
            # This replaces a blind `INSERT OR REPLACE INTO asset_sync_metadata VALUES (...)`.
            # The Python-level candidates below are also monotone, but the SQL LEAST/GREATEST/OR
            # is what actually makes the contract atomic (wrong-by-construction incoming values,
            # or a concurrent writer, can no longer damage it). COALESCE keeps the NULL cases
            # well-defined (existing NULL -> incoming, incoming NULL -> existing, both NULL ->
            # NULL) even though the columns are currently NOT NULL.
            # ------------------------------------------------------------------------------
            prior = con.execute(
                "SELECT earliest_available_date, is_earliest_known, latest_available_date "
                "FROM asset_sync_metadata WHERE provider = ? AND symbol = ?",
                [provider, symbol],
            ).fetchone()
            bars_span = con.execute(
                "SELECT MIN(date), MAX(date) FROM ohlcv_bars WHERE provider = ? AND symbol = ?",
                [provider, symbol],
            ).fetchone()
            bars_min_date, bars_max_date = bars_span if bars_span else (None, None)

            prior_earliest = prior[0] if prior else None
            prior_earliest_known = bool(prior[1]) if prior else False
            prior_latest = prior[2] if prior else None

            # Prior metadata, the bars actually on disk, and this frame are all evidence of what
            # is available. Cross-checking the bars table matters because metadata can legitimately
            # be missing (legacy imports, interrupted syncs) while the bars are present -- falling
            # back to the frame alone is exactly how 2017-era history got "forgotten".
            known_dates = [d for d in (prior_earliest, bars_min_date, frame_min_date) if d is not None]
            earliest_candidate = min(known_dates)
            known_latest_dates = [d for d in (prior_latest, bars_max_date, frame_max_date) if d is not None]
            latest_candidate = max(known_latest_dates)

            # Asking for a start strictly earlier than everything we already know, and getting
            # nothing older back, is the ONLY evidence that the earliest date really is the first
            # bar. It is measured against the earliest KNOWN date (not this frame's first row):
            # a request starting 2024-01-02 must not be able to claim "earliest known = 2024".
            probed_before_earliest = False
            if requested_start:
                req_start_date = pd.to_datetime(requested_start).date()
                probed_before_earliest = req_start_date < earliest_candidate - datetime.timedelta(days=7)
            is_earliest_known_candidate = prior_earliest_known or probed_before_earliest

            con.execute("""
                INSERT INTO asset_sync_metadata AS existing
                    (provider, symbol, earliest_available_date, is_earliest_known,
                     latest_available_date, last_sync_time)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, symbol) DO UPDATE SET
                    earliest_available_date = LEAST(
                        COALESCE(existing.earliest_available_date, excluded.earliest_available_date),
                        COALESCE(excluded.earliest_available_date, existing.earliest_available_date)
                    ),
                    is_earliest_known = existing.is_earliest_known OR excluded.is_earliest_known,
                    latest_available_date = GREATEST(
                        COALESCE(existing.latest_available_date, excluded.latest_available_date),
                        COALESCE(excluded.latest_available_date, existing.latest_available_date)
                    ),
                    last_sync_time = excluded.last_sync_time
            """, [
                provider,
                symbol,
                earliest_candidate,
                is_earliest_known_candidate,
                latest_candidate,
                sync_ts,
            ])
        finally:
            if not self.is_memory:
                con.close()

    def query_intraday_bars(
        self,
        provider: str,
        symbol: str,
        interval: str = "5m",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Retrieves intraday OHLCV bars for (provider, symbol, interval) across [start, end]."""
        clauses = ["provider = ?", "symbol = ?", "interval = ?"]
        params: List[Any] = [provider, symbol, interval]

        if start:
            clauses.append("ts >= ?")
            params.append(pd.to_datetime(start).to_pydatetime())
        if end:
            end_dt = _parse_intraday_end(end)
            clauses.append("ts <= ?")
            params.append(end_dt.to_pydatetime())

        sql = (
            f"SELECT ts, open, high, low, close, volume FROM intraday_ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts"
        )

        con = self._get_connection(read_only=True)
        try:
            res_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        if res_df.empty:
            return pd.DataFrame(columns=_REQUIRED_OHLCV_COLUMNS)

        res_df["ts"] = pd.to_datetime(res_df["ts"])
        res_df = res_df.set_index("ts").sort_index()
        res_df.index.name = "Date"
        res_df = res_df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )[_REQUIRED_OHLCV_COLUMNS].astype(float)

        return res_df

    def query_bars(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Retrieves OHLCV bars for (provider, symbol) across [start, end], resampled to interval."""
        if is_intraday_interval(interval):
            return self.query_intraday_bars(
                provider,
                symbol,
                interval=interval,
                start=start,
                end=end,
            )

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

    def query_intraday_universe(
        self,
        provider: str,
        symbols: List[str],
        interval: str = "5m",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Batch-queries intraday OHLCV bars for multiple symbols in a single SQL query."""
        if not symbols:
            return {}

        unique_symbols = list(dict.fromkeys(symbols))
        placeholders = ",".join(["?"] * len(unique_symbols))
        clauses = ["provider = ?", "interval = ?", f"symbol IN ({placeholders})"]
        params: List[Any] = [provider, interval, *unique_symbols]

        if start:
            clauses.append("ts >= ?")
            params.append(pd.to_datetime(start).to_pydatetime())
        if end:
            end_dt = _parse_intraday_end(end)
            clauses.append("ts <= ?")
            params.append(end_dt.to_pydatetime())

        sql = (
            f"SELECT symbol, ts, open, high, low, close, volume FROM intraday_ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol, ts"
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

        full_df["ts"] = pd.to_datetime(full_df["ts"])
        grouped = full_df.groupby("symbol")

        for sym in unique_symbols:
            if sym not in grouped.groups:
                continue
            group = grouped.get_group(sym)
            df = group.set_index("ts").sort_index()
            df.index.name = "Date"
            df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )[_REQUIRED_OHLCV_COLUMNS].astype(float)
            result[sym] = df

        return result

    def query_universe(
        self,
        provider: str,
        symbols: List[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
    ) -> Dict[str, pd.DataFrame]:
        """Batch-queries OHLCV bars for multiple symbols in a single SQL query."""
        if is_intraday_interval(interval):
            return self.query_intraday_universe(
                provider,
                symbols,
                interval=interval,
                start=start,
                end=end,
            )

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

    def store_fundamentals(self, provider: str, metrics: FundamentalMetrics) -> None:
        """Stores or updates annual fundamental metrics into stock_fundamentals_annual and stock_industry_metadata."""
        if is_synthetic_provider(provider):
            return

        sync_ts = datetime.datetime.now()
        stat_date_val = pd.to_datetime(metrics.stat_date).date() if metrics.stat_date else datetime.date(metrics.report_year, 12, 31)

        def _clean_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            try:
                f = float(val)
                return None if np.isnan(f) else f
            except (ValueError, TypeError):
                return None

        con = self._get_connection(read_only=False)
        try:
            con.execute("""
                INSERT OR REPLACE INTO stock_fundamentals_annual
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                provider,
                metrics.symbol,
                int(metrics.report_year),
                stat_date_val,
                _clean_float(metrics.roe),
                _clean_float(metrics.net_profit),
                _clean_float(metrics.net_profit_5yr_cagr),
                _clean_float(metrics.cfo),
                _clean_float(metrics.cfo_to_net_profit),
                _clean_float(metrics.total_assets),
                _clean_float(metrics.total_liabilities),
                _clean_float(metrics.debt_to_assets),
                _clean_float(metrics.debt_to_equity),
                _clean_float(metrics.interest_bearing_debt),
                _clean_float(metrics.dividend_yield),
                _clean_float(metrics.dividend_payout_ratio),
                _clean_float(metrics.pe_ttm),
                _clean_float(metrics.pb),
                _clean_float(metrics.total_shares),
                _clean_float(metrics.market_cap),
                sync_ts,
            ])
            con.execute("""
                INSERT OR REPLACE INTO stock_industry_metadata
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                provider,
                metrics.symbol,
                metrics.industry_name or "Unknown",
                metrics.industry_tier or "tier3_manufacturing",
                float(metrics.terminal_pe) if metrics.terminal_pe is not None and not np.isnan(metrics.terminal_pe) else 10.0,
                bool(metrics.is_blacklisted),
                metrics.blacklist_reason,
                sync_ts,
            ])
        finally:
            if not self.is_memory:
                con.close()

    def query_fundamentals(
        self, provider: str, symbol: str, report_year: Optional[int] = None
    ) -> Optional[FundamentalMetrics]:
        """Queries annual fundamental metrics for (provider, symbol), optionally by report_year.
        Returns FundamentalMetrics instance or None if not found.
        """
        if is_synthetic_provider(provider):
            return None

        from .data import FundamentalMetrics

        clauses = ["provider = ?", "symbol = ?"]
        params: List[Any] = [provider, symbol]
        if report_year is not None:
            clauses.append("report_year = ?")
            params.append(int(report_year))
            order_clause = ""
        else:
            order_clause = "ORDER BY report_year DESC LIMIT 1"

        sql = f"""
            SELECT provider, symbol, report_year, stat_date, roe, net_profit,
                   net_profit_5yr_cagr, cfo, cfo_to_net_profit, total_assets,
                   total_liabilities, debt_to_assets, debt_to_equity,
                   interest_bearing_debt, dividend_yield, dividend_payout_ratio,
                   pe_ttm, pb, total_shares, market_cap, last_sync_time
            FROM stock_fundamentals_annual
            WHERE {' AND '.join(clauses)}
            {order_clause}
        """

        con = self._get_connection(read_only=True)
        try:
            row = con.execute(sql, params).fetchone()
            if not row:
                return None

            meta_row = con.execute("""
                SELECT industry_name, industry_tier, terminal_pe, is_blacklisted, blacklist_reason
                FROM stock_industry_metadata
                WHERE provider = ? AND symbol = ?
            """, [provider, symbol]).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        industry_name = meta_row[0] if meta_row else "Unknown"
        industry_tier = meta_row[1] if meta_row else "tier3_manufacturing"
        terminal_pe = float(meta_row[2]) if meta_row and meta_row[2] is not None else 10.0
        is_blacklisted = bool(meta_row[3]) if meta_row else False
        blacklist_reason = meta_row[4] if meta_row else None

        def _val(x):
            return float(x) if x is not None else float("nan")

        return FundamentalMetrics(
            symbol=row[1],
            report_year=int(row[2]),
            stat_date=str(row[3]),
            roe=_val(row[4]),
            net_profit=_val(row[5]),
            net_profit_5yr_cagr=_val(row[6]),
            cfo=_val(row[7]),
            cfo_to_net_profit=_val(row[8]),
            total_assets=_val(row[9]),
            total_liabilities=_val(row[10]),
            debt_to_assets=_val(row[11]),
            debt_to_equity=_val(row[12]),
            interest_bearing_debt=_val(row[13]),
            dividend_yield=_val(row[14]),
            dividend_payout_ratio=_val(row[15]),
            pe_ttm=_val(row[16]),
            pb=_val(row[17]),
            total_shares=_val(row[18]),
            market_cap=_val(row[19]),
            industry_name=industry_name,
            industry_tier=industry_tier,
            terminal_pe=terminal_pe,
            is_blacklisted=is_blacklisted,
            blacklist_reason=blacklist_reason,
        )

    def store_industry_metadata(
        self,
        provider: str,
        symbol: str,
        industry_name: str,
        industry_tier: str,
        terminal_pe: float,
        is_blacklisted: bool = False,
        blacklist_reason: Optional[str] = None,
    ) -> None:
        """Stores or updates stock industry and valuation metadata."""
        if is_synthetic_provider(provider):
            return
        sync_ts = datetime.datetime.now()
        con = self._get_connection(read_only=False)
        try:
            con.execute("""
                INSERT OR REPLACE INTO stock_industry_metadata
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                provider,
                symbol,
                industry_name,
                industry_tier,
                float(terminal_pe),
                bool(is_blacklisted),
                blacklist_reason,
                sync_ts,
            ])
        finally:
            if not self.is_memory:
                con.close()

    def query_industry_metadata(self, provider: str, symbol: str) -> Optional[dict]:
        """Queries stock industry and valuation metadata."""
        if is_synthetic_provider(provider):
            return None
        con = self._get_connection(read_only=True)
        try:
            row = con.execute("""
                SELECT provider, symbol, industry_name, industry_tier, terminal_pe, is_blacklisted, blacklist_reason, last_sync_time
                FROM stock_industry_metadata
                WHERE provider = ? AND symbol = ?
            """, [provider, symbol]).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        if not row:
            return None

        return {
            "provider": row[0],
            "symbol": row[1],
            "industry_name": row[2],
            "industry_tier": row[3],
            "terminal_pe": float(row[4]),
            "is_blacklisted": bool(row[5]),
            "blacklist_reason": row[6],
            "last_sync_time": row[7],
        }

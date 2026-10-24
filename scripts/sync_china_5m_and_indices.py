#!/usr/bin/env python3
"""Batch Synchronization Script for China A-Share 5m Intraday & Index Daily Bars.

Features:
- Part A: Index Daily (1d) Gap Fix & Equity Recent Bar Patching:
  - Fetches complete 2016-present daily bars for the 7 major indices via BaoStock + EastMoney.
  - Stores under BaoStockDataProvider, EastMoneyDataProvider, FuyaoDataProvider, MarketDBDataProvider
    in data/cache.duckdb **through `cache.store_bars` only** (L2.15): the metadata boundaries and
    `is_earliest_known` are inferred by the cache from bar facts, never written by this script. The
    old unconditional `UPDATE asset_sync_metadata SET is_earliest_known = TRUE` is gone; see the
    comment at the write site for how the flag is legitimately established (an earlier `--start-1d`).
  - Scans all 22 stocks in core_satellite_22_stocks.txt and patches any missing recent daily bars
    (e.g., 002371.SZ, 601166.SH, 601390.SH) up to the latest trading day. Since L2.28 there is
    **no qfq overlap re-basing** on this path: the patch only inserts bars the cache lacks, and
    refuses (naming symbol + date) when a re-fetched window disagrees with a stored close.
- Part B: 5-Minute (5m) Intraday Pull & Dual-Track Caching (2022-01-01 -> Present):
  - Strictly sequential single-process downloading with a single persistent BaoStock login session
    to completely prevent IP/server throttling from BaoStock.
  - Fetches 5m qfq bars from BaoStock + supplements trailing days from EastMoney.
  - Stores into intraday_ohlcv_bars & intraday_sync_metadata in data/cache.duckdb sequentially
    after each symbol, so any already-cached symbols are skipped on resume.
  - Caches 511880.SH and available recent 5m bars for all 7 major indices.
- Part C: Verification Summary Tables:
  - Prints comprehensive Markdown summary tables detailing 1d and 5m coverage and health metrics.

Write contract (L2.28): every write goes through `plan_history_write`, which reads the stored
series read-only, refuses any window that would change an already-stored close
(`QfqWriteRefused`, after a `QfqRebaseWarning`, naming symbol + date), and returns an insert-only
frame (`append` / hole `backfill` / `noop`). `assert_append_only` pins the "first row after
MAX(date)" rule; `assert_no_windowed_rebase` is the read-only evidence function. A deliberate
history re-baseline belongs to `scripts/repair_qfq_alignment.py`, never to an incremental sync.
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import statistics
import sys
import time
import warnings
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import DuckDBCache, is_intraday_interval
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


def cross_check_sync_metadata(
    cache: DuckDBCache,
    provider: str,
    symbol: str,
    interval: str = "1d",
) -> Optional[str]:
    """READ-ONLY bar-fact audit of the sync-metadata row `store_bars` just wrote (L2.15).

    Returns a human-readable mismatch description, or `None` when the row agrees with the bars.

    Why this exists: `store_bars` / `store_intraday_bars` own the ONE rule that reconciles
    `*_sync_metadata` against the bar table (`L2.6` / `L2.18` / `L2.14`). This script used to
    bypass that rule with an unconditional
    `UPDATE ... SET is_earliest_known = TRUE` across four provider namespaces, extrapolating "this
    symbol was synced just now" into "every provider's earliest boundary is a known fact". That is
    precisely the inference `L2.6` deleted from the cache layer, so it must not be reintroduced
    here. The script therefore never writes a boundary or a flag itself: it only reports
    disagreement so an operator can see it. Repair belongs to the cache (it knows the bar facts),
    not to the caller (it only knows what it asked for).
    """
    if is_intraday_interval(interval):
        meta = cache.get_intraday_metadata(provider, symbol, interval)
        earliest_field, latest_field = "earliest_available_ts", "latest_available_ts"
        bars_table = "intraday_ohlcv_bars"
    else:
        meta = cache.get_asset_metadata(provider, symbol)
        earliest_field, latest_field = "earliest_available_date", "latest_available_date"
        bars_table = "ohlcv_bars"

    if meta is None:
        return None  # never stored => nothing to audit (the caller skips these namespaces)

    con = cache._get_connection(read_only=True)
    try:
        if bars_table == "intraday_ohlcv_bars":
            span = con.execute(
                f"SELECT MIN(ts), MAX(ts) FROM {bars_table} "
                f"WHERE provider = ? AND symbol = ? AND interval = ?",
                [provider, symbol, interval],
            ).fetchone()
        else:
            span = con.execute(
                f"SELECT MIN(date), MAX(date) FROM {bars_table} WHERE provider = ? AND symbol = ?",
                [provider, symbol],
            ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()

    bars_min, bars_max = span if span else (None, None)
    earliest_meta = meta[earliest_field]
    latest_meta = meta[latest_field]
    problems = []
    if bars_min is not None and pd.Timestamp(earliest_meta) != pd.Timestamp(bars_min):
        problems.append(f"{earliest_field}={earliest_meta} != {bars_table}.MIN={bars_min}")
    if bars_max is not None and pd.Timestamp(latest_meta) != pd.Timestamp(bars_max):
        problems.append(f"{latest_field}={latest_meta} != {bars_table}.MAX={bars_max}")
    if not problems:
        return None
    return f"{provider}/{symbol}/{interval}: " + "; ".join(problems)


def _report_cross_check(cache: DuckDBCache, provider: str, symbol: str, interval: str = "1d") -> None:
    """Prints (never repairs) a metadata-vs-bar mismatch, if any (L2.15)."""
    mismatch = cross_check_sync_metadata(cache, provider, symbol, interval=interval)
    if mismatch:
        print(f"[WARN metadata/bar mismatch] {mismatch}")


# =============================================================================
# QFQ WRITE GATE (L2.28): no windowed re-basing, no silent history rewrites
# =============================================================================
#
# `docs/version.md` §13 / §15 root-caused the 2026-10-22 price corruption to a
# **windowed overlap realignment**: one scalar, derived from a *single* overlap session
# inside the current fetch window, multiplied onto already-stored float64 rows. The
# measured fingerprint (§15.3) is `new == old * k` to <= 1 ULP, `volume` byte-identical,
# row count unchanged, and a window lower bound equal to the fetch start (2024-01-02) --
# i.e. a fetch-window endpoint, NOT an ex-dividend date.
#
# This script used to contain exactly that shape in `patch_equity_daily_bars`:
#
#     ratio = loc_c / new_c                       # from ONE local overlap session
#     new_df[["Open", "High", "Low", "Close"]] *= ratio
#     new_df = new_df[new_df.index.date > max_date]
#
# The multiply is REMOVED (not narrowed to "full history only" -- see below for why the
# window factor is unsound even then):
#
#   1. The factor is a *local basis difference*, never a dividend announcement. §13.8
#      proved that feeding it back into the series turns contamination into a "fix":
#      `600362.SH`'s factor turned out to be the exact reciprocal (to 1 ULP) of the local
#      Fuyao/MarketDB ratio at 2024-07-22, and `600660.SH`'s factor cannot be attributed
#      to any local upstream difference at all (§13.3).
#   2. "Which side is stale" is not decidable from one overlap session. A genuine qfq
#      re-baseline rescales the WHOLE history before the ex-date -- an open interval
#      towards the past. The incident's footprint is a *closed* interval whose lower
#      bound is the fetch start, which no corporate action can produce (§13.2).
#   3. `store_bars`/`store_intraday_bars` are upserts: any frame that reaches back into
#      stored history silently overwrites those rows with the rescaled values. The only
#      thing standing between the old multiply and stored history was the succeeding
#      `> max_date` trim -- one line of ordering, not a contract.
#
# So the sync path may only **insert rows the cache does not already hold**. If an
# incoming window disagrees with a stored row on price, the write is REFUSED loudly
# (naming symbol + date), never applied and never silently skipped. Deliberate
# re-baselining belongs to `scripts/repair_qfq_alignment.py` (verified window / full
# prefix, with `--apply` + backup + transaction + self-check) or to a full-history
# single-basis re-pull, not to an incremental sync.
#
# Tolerance for "already aligned": anything under this is float64 noise, not a rescale.
# Measured no-op rewrites in the incident were <= 3.01e-16 relative (§13.4), while the
# material rebases were 1.8e-2 / 3.4e-3 (§15.3) -- 12 orders of magnitude above this.
QFQ_NOOP_TOLERANCE = 1e-12

#: A rescale is reported as the incident's "single constant window" fingerprint when the
#: per-session ratios agree to within this spread (observed: <= 4.5e-16, §15.3).
QFQ_CONSTANT_SPREAD_TOLERANCE = 1e-9

SHAPE_FIRST_INSERT = "first_insert"   # no stored bars for this provider/symbol -> verbatim
SHAPE_APPEND = "append"               # every written row is strictly after MAX(ts)
SHAPE_BACKFILL = "backfill"           # writes only rows absent from the store (holes / older)
SHAPE_NOOP = "noop"                   # nothing to write: the store already holds it all


class QfqRebaseWarning(UserWarning):
    """Emitted, immediately before refusing, when an incoming window rescales stored history."""


class QfqWriteRefused(RuntimeError):
    """A sync write was refused because it would rewrite already-stored history.

    Carries structured context (`symbol` / `provider` / `interval` / `date` / `violations`)
    so callers and tests can assert on the offending asset and date without parsing text.
    """

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        provider: Optional[str] = None,
        interval: Optional[str] = None,
        date: Optional[datetime.date] = None,
        violations: Sequence["RebaseViolation"] = (),
    ):
        super().__init__(message)
        self.symbol = symbol
        self.provider = provider
        self.interval = interval
        self.date = date
        self.violations = tuple(violations)


class RebaseViolation:
    """One stored session whose price the incoming window would change.

    A plain class rather than a `@dataclass`: `common/tests/test_intraday_features.py` loads
    this script with `importlib.util.spec_from_file_location` **without** registering it in
    `sys.modules`, and `dataclasses` resolves PEP 563 string annotations through
    `sys.modules[cls.__module__]` -- which then is `None` and raises at import time. Keeping
    these value types annotation-free means the module imports the same way under either
    loader.
    """

    __slots__ = ("symbol", "ts", "stored_close", "incoming_close", "ratio", "rel_dev")

    def __init__(
        self,
        symbol: str,
        ts: Any,
        stored_close: float,
        incoming_close: float,
        ratio: float,
        rel_dev: float,
    ):
        self.symbol = symbol
        self.ts = pd.Timestamp(ts)
        self.stored_close = stored_close
        self.incoming_close = incoming_close
        self.ratio = ratio
        self.rel_dev = rel_dev

    @property
    def date(self) -> datetime.date:
        return self.ts.date()

    def describe(self) -> str:
        return (
            f"{_fmt_ts(self.ts)}: stored close {self.stored_close!r} vs incoming "
            f"{self.incoming_close!r} (ratio {self.ratio:.12f}, {self.rel_dev * 100:.4f}% apart)"
        )

    def __repr__(self) -> str:
        return (
            f"RebaseViolation({self.symbol!r}, {_fmt_ts(self.ts)!r}, "
            f"ratio={self.ratio!r}, rel_dev={self.rel_dev!r})"
        )


class RebaseAudit:
    """Read-only evidence that an incoming window does (or does not) rescale stored bars.

    `violations` is the assertable structure: empty means "the overlap is aligned to
    within `tolerance`", non-empty means "these stored sessions would change".
    """

    __slots__ = ("symbol", "n_overlap", "violations", "ratios", "tolerance")

    def __init__(
        self,
        symbol: str,
        n_overlap: int,
        violations: Tuple[RebaseViolation, ...] = (),
        ratios: Tuple[float, ...] = (),
        tolerance: float = QFQ_NOOP_TOLERANCE,
    ):
        self.symbol = symbol
        self.n_overlap = n_overlap
        self.violations = tuple(violations)
        self.ratios = tuple(ratios)
        self.tolerance = tolerance

    @property
    def is_aligned(self) -> bool:
        return not self.violations

    @property
    def median_ratio(self) -> float:
        return float(statistics.median(self.ratios)) if self.ratios else 1.0

    @property
    def ratio_spread(self) -> float:
        """max - min of the per-session incoming/stored ratio (the §15.3 spread)."""
        return (max(self.ratios) - min(self.ratios)) if self.ratios else 0.0

    @property
    def max_rel_dev(self) -> float:
        return max((v.rel_dev for v in self.violations), default=0.0)

    @property
    def is_single_constant_rescale(self) -> bool:
        """True for the incident's fingerprint: one constant multiplied over a window.

        A true upstream re-fetch/re-adjust re-rounds every session, so its per-session
        ratio jitters at the 1e-4 level; a scalar multiply onto stored float64 does not
        (measured spread 2.2e-16 - 4.5e-16, §15.3).
        """
        if not self.violations:
            return False
        return self.ratio_spread <= QFQ_CONSTANT_SPREAD_TOLERANCE

    def describe(self) -> str:
        if not self.violations:
            return f"{self.symbol}: {self.n_overlap} overlapping session(s) aligned (no rescale)"
        first, last = self.violations[0], self.violations[-1]
        return (
            f"{self.symbol}: {len(self.violations)} of {self.n_overlap} overlapping session(s) "
            f"rescaled, {_fmt_ts(first.ts)} -> {_fmt_ts(last.ts)}, median ratio {self.median_ratio:.12f}, "
            f"spread {self.ratio_spread:.3e}"
            f"{' (single-constant window)' if self.is_single_constant_rescale else ''}"
        )


def _as_date(value: Any) -> datetime.date:
    """Normalises a date-like to `datetime.date` (accepts Timestamp / str / date)."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return pd.Timestamp(value).date()


def _fmt_ts(value: Any) -> str:
    """Formats a bar timestamp: `YYYY-MM-DD` for daily bars, with `HH:MM` for intraday."""
    ts = pd.Timestamp(value)
    if ts.hour or ts.minute or ts.second:
        return ts.strftime("%Y-%m-%d %H:%M")
    return ts.strftime("%Y-%m-%d")


def _close_series(frame: Any) -> pd.Series:
    """Extracts a float close series indexed by timestamp from a frame or series.

    Read-only, tolerant of both the cache's lowercase and the providers' capitalised
    column names -- the gate is called from both sides of that boundary.
    """
    if isinstance(frame, pd.DataFrame):
        lower = {str(c).lower(): c for c in frame.columns}
        if "close" not in lower:
            raise ValueError(f"frame has no close column: {list(frame.columns)}")
        ser = frame[lower["close"]]
    elif isinstance(frame, pd.Series):
        ser = frame
    else:  # pragma: no cover - defensive
        raise TypeError(f"expected DataFrame or Series, got {type(frame)!r}")

    out = pd.Series(
        pd.to_numeric(pd.Series(ser.values), errors="coerce").values,
        index=pd.to_datetime(ser.index),
        dtype=float,
    )
    return out[~out.index.duplicated(keep="last")].sort_index()


def assert_no_windowed_rebase(
    existing: Any,
    incoming: Any,
    symbol: Optional[str] = None,
    tolerance: float = QFQ_NOOP_TOLERANCE,
) -> RebaseAudit:
    """READ-ONLY check: would `incoming` rescale any bar of the `existing` series?

    Returns a `RebaseAudit` whose `violations` list is empty when every overlapping
    session agrees to within `tolerance` (float64 noise) and non-empty otherwise. The
    function never writes, never raises on a violation (the caller decides; the script's
    write sites call `plan_history_write`, which refuses) and never touches the cache --
    it takes plain frames so it can be asserted on directly in tests.

    Why a returned structure rather than a bare bool: the forensics of §13/§15 needed
    *which* dates moved and by *which* constant. `RebaseAudit.is_single_constant_rescale`
    reproduces that fingerprint mechanically (a scalar multiply leaves a ULP-flat spread;
    a genuine upstream re-adjust re-rounds every session and jitters at 1e-4).
    """
    stored = _close_series(existing)
    incoming_close = _close_series(incoming)
    label = symbol or "<unknown>"
    overlap = stored.index.intersection(incoming_close.index)
    if overlap.empty:
        return RebaseAudit(symbol=label, n_overlap=0, tolerance=tolerance)

    violations: List[RebaseViolation] = []
    ratios: List[float] = []
    for ts in overlap:
        old = float(stored.loc[ts])
        new = float(incoming_close.loc[ts])
        if not (math.isfinite(old) and math.isfinite(new)) or old <= 0.0 or new <= 0.0:
            # Unusable reference: cannot certify the basis, so it must not be overwritten.
            violations.append(
                RebaseViolation(label, pd.Timestamp(ts), old, new, float("nan"), float("inf"))
            )
            continue
        ratio = new / old
        ratios.append(ratio)
        rel_dev = abs(ratio - 1.0)
        if rel_dev > tolerance:
            violations.append(RebaseViolation(label, pd.Timestamp(ts), old, new, ratio, rel_dev))

    return RebaseAudit(
        symbol=label,
        n_overlap=int(len(overlap)),
        violations=tuple(violations),
        ratios=tuple(ratios),
        tolerance=tolerance,
    )


def assert_append_only(
    write_start: Any,
    existing_max: Any,
    *,
    symbol: Optional[str] = None,
    provider: Optional[str] = None,
    interval: Optional[str] = None,
) -> None:
    """Hard write-entry gate: a sync may only ADD rows strictly after the last stored bar.

    This is the same criterion `scripts/repair_qfq_alignment.py::assert_append_only`
    enforces for the sync contract (`docs/version.md` §14.5): `write_start <= MAX(date)`
    is refused. A history rewrite has to be declared through the repair script's
    `rebase`/`restore` shapes instead -- an incremental sync must never do it by accident.
    """
    start = _as_date(write_start)
    last = _as_date(existing_max)
    if start <= last:
        where = "/".join(part for part in (provider, symbol, interval) if part) or "<unknown asset>"
        raise QfqWriteRefused(
            f"append-only contract violated for {where}: first row to write {start} is at or "
            f"before the stored MAX(date) {last}. A sync appends; it never rewrites stored "
            f"history. Declared rewrites belong to scripts/repair_qfq_alignment.py.",
            symbol=symbol,
            provider=provider,
            interval=interval,
            date=start,
        )


def assert_no_stored_row_rewritten(
    write_index: Iterable[Any],
    stored_index: Iterable[Any],
    *,
    symbol: Optional[str] = None,
    provider: Optional[str] = None,
    interval: Optional[str] = None,
) -> None:
    """Hard write-entry gate: the frame may only contain rows the store does NOT have.

    This is the invariant that makes a windowed rescale impossible to land: the only way a
    stored close can influence a write is if that row is part of the frame, and every
    overwrite is refused here before the frame reaches `store_bars`/`store_intraday_bars`.
    """
    write_ts = pd.to_datetime(pd.Index(list(write_index)))
    stored_ts = pd.to_datetime(pd.Index(list(stored_index)))
    collisions = write_ts.intersection(stored_ts)
    if len(collisions):
        where = "/".join(part for part in (provider, symbol, interval) if part) or "<unknown asset>"
        first = _as_date(collisions.min())
        raise QfqWriteRefused(
            f"refusing to overwrite {len(collisions)} already-stored row(s) of {where} "
            f"(first {first}, last {_as_date(collisions.max())}): a sync only inserts rows the "
            f"cache lacks. Rewriting a stored bar is how a window-local scalar reaches history.",
            symbol=symbol,
            provider=provider,
            interval=interval,
            date=first,
        )


def _stored_bars(
    cache: DuckDBCache,
    provider: str,
    symbol: str,
    interval: str = "1d",
) -> pd.DataFrame:
    """READ-ONLY snapshot of the bars already stored for one (provider, symbol, interval)."""
    intraday = is_intraday_interval(interval)
    table = "intraday_ohlcv_bars" if intraday else "ohlcv_bars"
    ts_col = "ts" if intraday else "date"
    where = f"provider = ? AND symbol = ?" + (" AND interval = ?" if intraday else "")
    params: List[Any] = [provider, symbol] + ([interval] if intraday else [])

    con = cache._get_connection(read_only=True)
    try:
        rows = con.execute(
            f"SELECT {ts_col}, open, high, low, close, volume FROM {table} "
            f"WHERE {where} ORDER BY {ts_col}",
            params,
        ).fetchdf()
    finally:
        if not cache.is_memory:
            con.close()

    if rows.empty:
        return pd.DataFrame()
    rows[ts_col] = pd.to_datetime(rows[ts_col])
    rows = rows.set_index(ts_col).sort_index()
    return rows.rename(columns={c: c.capitalize() for c in rows.columns})


class WritePlan:
    """What a guarded write may actually put on disk, plus the evidence for that decision.

    Two admissible shapes, both insert-only:

    * `append` / `backfill` -- every row is strictly after `existing_max` (the §14.5
      append-only contract, enforced by `assert_append_only`), or
    * hole fill -- rows that are absent from the store while sitting inside the stored
      span (a gap the cache genuinely lacks). They are insertions, not rewrites: no
      stored value is used as a reference to rescale anything, so no basis can drift.

    Rows that *are* already stored are always dropped from `df`: rewriting them buys
    nothing and is exactly the "no-op rewrite" that manufactured the 126 `ulp_noise`
    rows in §13.4.
    """

    __slots__ = (
        "symbol",
        "provider",
        "interval",
        "shape",
        "df",
        "audit",
        "existing_min",
        "existing_max",
        "n_dropped_overlap",
        "n_appended",
        "n_holes",
    )

    def __init__(
        self,
        symbol: str,
        provider: str,
        interval: str,
        shape: str,
        df: pd.DataFrame,
        audit: RebaseAudit,
        existing_min: Optional[pd.Timestamp] = None,
        existing_max: Optional[pd.Timestamp] = None,
        n_dropped_overlap: int = 0,
        n_appended: int = 0,
        n_holes: int = 0,
    ):
        self.symbol = symbol
        self.provider = provider
        self.interval = interval
        self.shape = shape
        self.df = df
        self.audit = audit
        self.existing_min = existing_min
        self.existing_max = existing_max
        self.n_dropped_overlap = n_dropped_overlap
        self.n_appended = n_appended
        self.n_holes = n_holes

    @property
    def is_empty(self) -> bool:
        return self.df is None or self.df.empty

    @property
    def has_history(self) -> bool:
        return self.existing_max is not None

    def requested_start(self, fallback: str) -> str:
        """`requested_start` for `store_bars`/`store_intraday_bars` (L2.15 contract).

        A tail write must never speak about the past: when history exists the stored
        frame's own first bar is the only request start this frame can back up.
        """
        if not self.has_history or self.is_empty:
            return fallback
        return self.df.index.min().strftime("%Y-%m-%d")

    def describe(self) -> str:
        if self.is_empty:
            return f"{self.provider}/{self.symbol}/{self.interval}: {self.shape} (nothing to write)"
        return (
            f"{self.provider}/{self.symbol}/{self.interval}: {self.shape} +{len(self.df)} rows "
            f"[{self.df.index.min()} -> {self.df.index.max()}]"
            f" (dropped {self.n_dropped_overlap} already-stored row(s))"
        )

    def __repr__(self) -> str:
        return f"WritePlan({self.describe()!r})"


def plan_history_write(
    cache: DuckDBCache,
    provider: str,
    symbol: str,
    df: pd.DataFrame,
    interval: str = "1d",
    tolerance: float = QFQ_NOOP_TOLERANCE,
) -> WritePlan:
    """Read the store, refuse windowed re-basing, and return the insert-only write plan.

    Raises `QfqWriteRefused` (after a `QfqRebaseWarning`) when the incoming frame would
    change the price of a row that is already stored. On refusal nothing is written at
    all for this provider/symbol -- no partial frame, no silent skip.
    """
    if df is None or df.empty:
        raise ValueError(f"{provider}/{symbol}/{interval}: refusing to plan an empty frame")

    incoming = df.copy()
    incoming.index = pd.to_datetime(incoming.index)
    incoming = incoming[~incoming.index.duplicated(keep="last")].sort_index()

    stored = _stored_bars(cache, provider, symbol, interval)
    if stored.empty:
        # First-ever insert for this namespace. There is no stored basis to align to, so
        # "alignment" is trivially satisfied: the frame lands verbatim (and the cache's
        # own metadata reconciliation derives the boundaries from these bars).
        return WritePlan(
            symbol=symbol,
            provider=provider,
            interval=interval,
            shape=SHAPE_FIRST_INSERT,
            df=incoming,
            audit=RebaseAudit(symbol=symbol, n_overlap=0, tolerance=tolerance),
            n_appended=int(len(incoming)),
        )

    audit = assert_no_windowed_rebase(stored["Close"], incoming["Close"], symbol=symbol, tolerance=tolerance)
    existing_min = stored.index.min()
    existing_max = stored.index.max()

    if not audit.is_aligned:
        # Deduplicated, short excerpt: the full detail is in the structured violations.
        excerpt = "; ".join(v.describe() for v in audit.violations[:3])
        more = "" if len(audit.violations) <= 3 else f"; (+{len(audit.violations) - 3} more)"
        message = (
            f"REFUSED windowed re-basing of {provider}/{symbol}/{interval}: the incoming window "
            f"would change {len(audit.violations)} already-stored session(s), "
            f"{_fmt_ts(audit.violations[0].ts)} -> {_fmt_ts(audit.violations[-1].ts)} "
            f"(stored span {_fmt_ts(existing_min)} -> {_fmt_ts(existing_max)}; median ratio "
            f"{audit.median_ratio:.12f}, spread {audit.ratio_spread:.3e}"
            f"{', single-constant window' if audit.is_single_constant_rescale else ''}). "
            f"{excerpt}{more}. Nothing was written for this symbol. A sync appends only; a "
            f"deliberate re-baseline must go through scripts/repair_qfq_alignment.py (verified "
            f"window / full prefix, --apply with backup) or a full-history single-basis re-pull."
        )
        warnings.warn(message, QfqRebaseWarning, stacklevel=2)
        raise QfqWriteRefused(
            message,
            symbol=symbol,
            provider=provider,
            interval=interval,
            date=audit.violations[0].date,
            violations=audit.violations,
        )

    # Aligned overlap: never rewrite a stored row (no-op rewrites only add ULP noise).
    keep = ~incoming.index.isin(stored.index)
    write_df = incoming[keep]
    # Belt-and-braces: the frame handed to the cache provably contains no stored row.
    assert_no_stored_row_rewritten(
        write_df.index, stored.index, symbol=symbol, provider=provider, interval=interval
    )
    dropped = int(len(incoming) - len(write_df))
    appended = int((write_df.index > existing_max).sum()) if not write_df.empty else 0
    holes = int(len(write_df) - appended)

    if not write_df.empty and write_df.index.min() < existing_min and audit.n_overlap == 0:
        # A pre-history block (a genuine "reaches further back" backfill, `L2.6`). It is
        # INSERT-ONLY: every row is absent from the store, so no stored value is rescaled and
        # no windowed re-base is possible. What it cannot do is prove its basis against the
        # stored series (there is no shared session), so the level at the join is not
        # verifiable here -- `scripts/audit_qfq_alignment.py`'s cross-namespace seam detector
        # is the reviewable check for that, which is why this is recorded loudly and not
        # silently accepted.
        print(
            f"[backfill] {provider}/{symbol}/{interval}: +{len(write_df)} row(s) before the stored "
            f"MIN {_fmt_ts(existing_min)} (no overlapping session; insert-only, basis unverified "
            f"at the join -- run audit_qfq_alignment.py if the seam matters)"
        )

    if write_df.empty:
        shape = SHAPE_NOOP
    elif write_df.index.min() > existing_max:
        shape = SHAPE_APPEND
        # §14.5's contract, checked on the frame that is actually handed to the cache.
        assert_append_only(
            write_df.index.min(), existing_max, symbol=symbol, provider=provider, interval=interval
        )
    else:
        shape = SHAPE_BACKFILL

    return WritePlan(
        symbol=symbol,
        provider=provider,
        interval=interval,
        shape=shape,
        df=write_df,
        audit=audit,
        existing_min=existing_min,
        existing_max=existing_max,
        n_dropped_overlap=dropped,
        n_appended=appended,
        n_holes=holes,
    )


class RefusalCollector:
    """Accumulates per-symbol refusals so one bad asset cannot abort the whole batch.

    The run still fails loudly: `raise_if_any` re-raises an aggregate `QfqWriteRefused`
    naming every refused symbol and date, so the operator sees all of them in one pass
    instead of discovering them one restart at a time. Nothing is silently skipped.
    """

    def __init__(self, label: str):
        self.label = label
        self.items: List[QfqWriteRefused] = []

    def add(self, exc: QfqWriteRefused) -> None:
        print(f"[REFUSED] {exc}")
        self.items.append(exc)

    def raise_if_any(self) -> None:
        if not self.items:
            return
        if len(self.items) == 1:
            # Preserve the single refusal's structured context (symbol/provider/date) verbatim.
            raise self.items[0]
        detail = "; ".join(
            f"{exc.provider or '?'}/{exc.symbol or '?'}@{exc.date}" for exc in self.items
        )
        raise QfqWriteRefused(
            f"{self.label}: {len(self.items)} write(s) refused -- the cache still holds the "
            f"pre-sync values for these symbols (nothing was written for them). {detail}",
            symbol=",".join(sorted({exc.symbol for exc in self.items if exc.symbol})),
            provider=",".join(sorted({exc.provider for exc in self.items if exc.provider})),
            interval=",".join(sorted({exc.interval for exc in self.items if exc.interval})) or None,
            date=min(exc.date for exc in self.items if exc.date),
            violations=[v for exc in self.items for v in exc.violations],
        )


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

    refusals = RefusalCollector("PART A.1 (major indices, 1d)")

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

            # Store into all 4 provider namespaces.
            #
            # L2.15: there used to be an unconditional
            #     UPDATE asset_sync_metadata SET is_earliest_known = TRUE
            #                  WHERE symbol = ? AND provider IN (<all four namespaces>)
            # right here. It was a lie by construction: "this symbol was synced just now" says
            # nothing about whether any provider's history actually begins at the first bar, and
            # it is exactly the inference `L2.6` removed from the cache layer (which now infers
            # `is_earliest_known` ONLY from reviewable evidence: the request asked strictly
            # earlier than the oldest known bar and got nothing older back). The flag is therefore
            # left entirely to `store_bars`.
            #
            # OPERATIONAL CONSEQUENCE (deliberate, fail-safe): with `--start-1d 2016-01-01` the
            # probe is not satisfied, so these rows keep `is_earliest_known = FALSE` and a caller
            # asking for a start *before* 2016 pays one redundant re-fetch instead of being told
            # "covered" for history that was never probed. To establish the flag honestly, pass a
            # `--start-1d` earlier than the provider's own first bar (then the 7-day probe rule
            # fires on real evidence); a redundant fetch is cheap, a fabricated boundary is not.
            providers = [
                "BaoStockDataProvider",
                "EastMoneyDataProvider",
                "FuyaoDataProvider",
                "MarketDBDataProvider",
            ]
            # L2.28: a full-window re-store is only an *insert*. The gate drops every row the
            # cache already holds (so the overlap cannot be rescale-written, which is how the
            # 2026-10-22 windowed re-basing reached stored history through this very upsert),
            # and refuses the symbol if the incoming window disagrees with a stored close.
            # Indices are fetched unadjusted (`adjust="none"`), so a disagreement here means a
            # vendor re-statement or a basis change -- either way not something a sync may
            # apply silently.
            for prov in providers:
                try:
                    plan = plan_history_write(cache, prov, sym, full_df, interval="1d")
                except QfqWriteRefused as exc:
                    refusals.add(exc)
                    continue
                if plan.is_empty:
                    continue
                cache.store_bars(
                    provider=prov,
                    symbol=sym,
                    df=plan.df,
                    requested_start=plan.requested_start(start),
                    requested_end=end,
                    interval="1d",
                )
                _report_cross_check(cache, prov, sym, interval="1d")

            print(
                f"OK: {len(full_df)} bars "
                f"[{full_df.index.min().strftime('%Y-%m-%d')} -> {full_df.index.max().strftime('%Y-%m-%d')}]"
            )
    finally:
        bs_prov.logout()

    refusals.raise_if_any()


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

    # Pre-query existing coverage and close connection upfront to prevent DuckDB connection conflict.
    #
    # L2.28: this query used to also build `close_map[(sym, prov, max_date)] = close`, i.e. the
    # *stored* close of the last bar, purely so the alignment below could compute
    # `ratio = loc_c / new_c`. That read existed for one purpose only -- manufacturing a
    # window-local rescale factor -- so it is gone with the rescale. The stored series is now
    # read once, read-only, by `plan_history_write` (which needs the whole overlap to prove or
    # refuse a basis change, not one session).
    con = cache._get_connection(read_only=True)
    try:
        all_rows = con.execute(
            "SELECT symbol, provider, min(date), max(date), count(*) "
            "FROM ohlcv_bars WHERE provider IN ('FuyaoDataProvider', 'MarketDBDataProvider') "
            "GROUP BY symbol, provider"
        ).fetchall()
    finally:
        if not cache.is_memory:
            con.close()

    row_dict = {(r[0], r[1]): r[3] for r in all_rows}  # (symbol, provider) -> max_date
    refusals = RefusalCollector("PART A.2 (equity daily patch)")

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

                # ------------------------------------------------------------------------------
                # L2.28: the windowed "overlap-day alignment" that used to live here is GONE.
                #
                # It was:
                #     if max_date is not None and pd.Timestamp(max_date) in new_df.index:
                #         loc_c = close_map[(sym, prov, max_date)]          # ONE stored close
                #         if abs(new_c - loc_c) / loc_c > 1e-4:
                #             new_df[["Open","High","Low","Close"]] *= loc_c / new_c
                #     new_df = new_df[new_df.index.date > max_date]
                #
                # That single scalar is a *local basis difference*, not a corporate action
                # (docs/version.md §13.8: 600362.SH's factor was the reciprocal of the local
                # Fuyao/MarketDB ratio to 1 ULP; 600660.SH's could not be attributed to any
                # upstream difference at all). Multiplying by it turns a contaminated
                # reference into a "fix", and the only thing that kept the multiply off
                # stored rows was the ordering of the `> max_date` trim -- one line, not a
                # contract (the 2026-10-22 writer that did reach stored history produced
                # exactly this fingerprint: single constant, `volume` untouched, window lower
                # bound == fetch start, §15.3).
                #
                # A real qfq re-baseline rescales the whole history before the ex-date (an
                # open interval towards the past); this path cannot know that, so it does not
                # rescale at all. It only INSERTS rows the cache lacks, and refuses the symbol
                # (naming it and the date) when the incoming window disagrees with a stored
                # close. Re-baselining is `scripts/repair_qfq_alignment.py`'s job.
                # ------------------------------------------------------------------------------
                try:
                    plan = plan_history_write(cache, prov, sym, new_df, interval="1d")
                except QfqWriteRefused as exc:
                    refusals.add(exc)
                    continue

                if plan.is_empty:
                    # Nothing to insert: the cache already holds every bar in this window on
                    # the stored basis. Deliberately no write and no warning -- rewriting
                    # identical rows is what produced the 126 `ulp_noise` rows in §13.4.
                    print(f"ALREADY CURRENT (dropped {plan.n_dropped_overlap} stored row(s))")
                    continue

                if plan.shape == SHAPE_BACKFILL:
                    # A hole inside the fetched look-back (rows the store genuinely lacks).
                    # Inserting them is not a rewrite: `plan_history_write` only ever puts
                    # rows in `plan.df` that are ABSENT from the store, so no stored close is
                    # used as a rescale reference and the strict append-only gate (which is
                    # about the tail) does not apply here.
                    print(f"[backfill] {sym} [{prov}]: +{plan.n_holes} missing bar(s)", end=" ")

                # L2.15 + L2.28: `requested_start` must describe a request that THIS stored
                # frame can back up. The old code always passed `patch_start` -- a look-back
                # anchor -- even after the alignment front-truncated the frame to `> max_date`.
                # That is the exact shape the cache used to misread: "the earliest date this
                # sync could reach is `patch_start`". A tail patch must never speak about the
                # past, so when history exists we pass the frame's own first bar (making the
                # probe inference structurally impossible); only a never-truncated fetch
                # (first-ever sync, no stored bars) may pass the real request start. The
                # metadata boundaries themselves remain the bar facts -- `store_bars`
                # cross-checks `ohlcv_bars`' MIN/MAX and never the window.
                cache.store_bars(
                    provider=prov,
                    symbol=sym,
                    df=plan.df,
                    requested_start=plan.requested_start(patch_start),
                    requested_end=end,
                    interval="1d",
                )
                _report_cross_check(cache, prov, sym, interval="1d")
                print(f"PATCHED +{len(plan.df)} bars (max: {plan.df.index.max().strftime('%Y-%m-%d')})")
    finally:
        bs_prov.logout()

    refusals.raise_if_any()


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

    refusals = RefusalCollector("PART B (5m intraday)")
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

            # Store immediately into DuckDB cache.
            #
            # L2.15: the old code followed these two stores with
            #     UPDATE intraday_sync_metadata SET is_earliest_known = TRUE
            #            WHERE symbol = ? AND interval = '5m'
            # whenever the frame happened to start at/before 2022-01-10, i.e. it turned a
            # hard-coded guess about the upstream's history into a stored fact, and did so for
            # EVERY provider row of that symbol (BaoStock and EastMoney get the same frame here,
            # but a third namespace serving the symbol would have been marked True too without
            # ever being synced). `store_intraday_bars` now owns the flag via the same
            # `L2.6`/`L2.18` rule the daily table uses, on reviewable evidence only.
            #
            # OPERATIONAL CONSEQUENCE (deliberate): with the default `--start-5m 2022-01-01` the
            # probe is not satisfied (the request does not start >7 days before the first bar), so
            # these rows keep `is_earliest_known = FALSE`. `is_intraday_range_covered` still
            # reports this script's own window as covered via its 7-day start tolerance, so no
            # re-fetch loop is introduced; requests starting well before 2022 now correctly
            # re-fetch instead of trusting an unverified claim.
            provider_names = ["BaoStockDataProvider", "EastMoneyDataProvider"]
            n_new = 0
            for prov in provider_names:
                # L2.28 write gate. This store is the one that used to reach furthest into
                # stored history: the frame spans the whole `--start-5m` window, so every
                # session since 2022 was upserted on each run. Two consequences are now
                # structural instead of accidental:
                #   * only rows the cache lacks are written (no history rewrite, no
                #     "no-op rewrite" ULP noise), and
                #   * if the incoming window disagrees with a stored close, the symbol is
                #     refused by name + date BEFORE the write. That also disarms
                #     `store_intraday_bars`' R4 drift branch (`DELETE` all bars for the
                #     asset on a >1e-4 overlap drift): the gate refuses first, so a basis
                #     change can no longer silently wipe and repopulate the history.
                try:
                    plan = plan_history_write(cache, prov, sym, full_df, interval="5m")
                except QfqWriteRefused as exc:
                    refusals.add(exc)
                    continue
                if plan.is_empty:
                    continue
                n_new = max(n_new, len(plan.df))
                cache.store_intraday_bars(
                    prov,
                    sym,
                    plan.df,
                    interval="5m",
                    requested_start=plan.requested_start(start),
                    requested_end=end,
                )
                _report_cross_check(cache, prov, sym, interval="5m")

            elapsed = time.time() - t0
            print(
                f"STORED {len(full_df):,} fetched / {n_new:,} new bars "
                f"({full_df.index.min().strftime('%Y-%m-%d %H:%M')} -> {full_df.index.max().strftime('%Y-%m-%d %H:%M')}) "
                f"[{elapsed:.1f}s]"
            )
    finally:
        bs_prov.logout()

    refusals.raise_if_any()

    # 2. Sync available recent 5m bars for Major Indices via EastMoney
    print("\n-> Syncing available recent 5m bars for Major Indices via EastMoney...")
    em_idx_prov = EastMoneyDataProvider(adjust="none")
    for idx_sym in indices:
        try:
            time.sleep(0.2)
            df_idx_5m = em_idx_prov.fetch_ohlcv(idx_sym, "2026-08-01", end, interval="5m")
            if not df_idx_5m.empty:
                for prov in ("EastMoneyDataProvider", "FuyaoDataProvider"):
                    try:
                        plan = plan_history_write(cache, prov, idx_sym, df_idx_5m, interval="5m")
                    except QfqWriteRefused as exc:
                        # Deliberately NOT swallowed by the network-error handler below:
                        # a basis change must fail the run, not print "skipped: ...".
                        refusals.add(exc)
                        continue
                    if plan.is_empty:
                        continue
                    cache.store_intraday_bars(
                        prov,
                        idx_sym,
                        plan.df,
                        interval="5m",
                        requested_start=plan.requested_start("2026-08-01"),
                        requested_end=end,
                    )
                    _report_cross_check(cache, prov, idx_sym, interval="5m")
                print(
                    f"-> Index {idx_sym} (5m): {len(df_idx_5m):,} fetched bars "
                    f"({df_idx_5m.index.min().strftime('%Y-%m-%d %H:%M')} -> {df_idx_5m.index.max().strftime('%Y-%m-%d %H:%M')})"
                )
        except QfqWriteRefused:
            raise
        except Exception as e:
            print(f"-> Index {idx_sym} (5m) skipped: {e}")

    refusals.raise_if_any()


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

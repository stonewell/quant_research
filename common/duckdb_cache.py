"""DuckDB-backed OHLCV cache system referencing MarketDB architecture.

Provides high-performance, single-file columnar storage (cache.duckdb) for historical
price bars. Bars and metadata are strictly keyed by (provider, symbol), allowing base
daily bars to dynamically serve any date range and any interval (via OHLCV resampling).
Tracks each asset's earliest available date from the upstream provider to eliminate
redundant pre-inception queries. Cached historical data never expires.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union, TYPE_CHECKING
import warnings

import duckdb
import numpy as np
import pandas as pd

from common.market_clock import (
    UnknownSymbolMarketError,
    infer_market_from_symbol,
    last_expected_bar_ts,
)

if TYPE_CHECKING:
    from .data import FundamentalMetrics


#: Self-heal messages (`asset_sync_metadata` rebuilt/corrected against the bar table) must be
#: visible to whoever is debugging a cache: L2.18 requires that a repair is never silent.
logger = logging.getLogger(__name__)


_REQUIRED_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

#: Intraday interval -> bar length in minutes. This is the single source of both
#: `_INTRADAY_INTERVALS` (what counts as intraday at all) and `_intraday_bar_interval`
#: (how much tolerance the L2.26 session-reachability check allows), so the two can
#: never drift apart.
_INTRADAY_BAR_MINUTES: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1h": 60,
}

_INTRADAY_INTERVALS = set(_INTRADAY_BAR_MINUTES)


def is_intraday_interval(interval: str) -> bool:
    """Returns True if the interval represents an intraday frequency."""
    if not interval or not isinstance(interval, str):
        return False
    return interval.strip().lower() in _INTRADAY_INTERVALS


def _intraday_bar_interval(interval: str) -> Optional[datetime.timedelta]:
    """Bar length of a known intraday interval (`5m` -> 5 minutes), else `None`.

    `None` is a *caller/provisioning* problem, never "no check needed": the L2.26 gate
    logs it and degrades to the date-only comparison rather than inventing a length.
    """
    if not isinstance(interval, str):
        return None
    minutes = _INTRADAY_BAR_MINUTES.get(interval.strip().lower())
    return None if minutes is None else datetime.timedelta(minutes=minutes)


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


# ---------------------------------------------------------------------------
# Pytest-only safety net: the production market cache must never be opened for
# writing by a test (L2.13)
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (this net is the last line of a real incident, not paranoia):
#   2026-10-22: a test (and an ad-hoc script) opened the production
#   `<repo>/data/cache.duckdb` in READ-WRITE mode through `DuckDBCache.__init__`,
#   which creates/repairs schema and then lets `store_bars` upsert metadata. The
#   same class is reached by `CachedDataProvider`, by `common/cli_utils.py::
#   shared_data_dir()`'s `<repo>/data` default, by the three live scripts
#   (`check_live_portfolio_health.py`, `run_live_strategy.py`,
#   `sync_china_5m_and_indices.py`) and by callers that hand-assemble a
#   `"data/cache.duckdb"` relative path (`common/financial_api.py`,
#   `common/intraday_features.py`). `web/livetrading/storage.py` (L2.12) only
#   guards paths that go through `StorageManager`, so this constructor is the
#   ONE remaining write chokepoint every one of those paths must pass through.
#
#   The net fires only when ALL THREE of the following hold:
#     1. the connection would be writable (`read_only=False`),
#     2. the final resolved database file is inside `<repo>/data`,
#     3. the process is pytest (`PYTEST_CURRENT_TEST` is set).
#   Read-only opens are deliberately NOT blocked: legitimate audit scripts and
#   tests inspect the real cache read-only, and rejecting `read_only=True` would
#   force them onto copies. Non-pytest processes are untouched: the env lookup
#   is the first statement, so production behaviour is identical to HEAD (no
#   path resolution, no `realpath`, no raise).
#
#   Escape hatch: `LIVETRADING_ALLOW_PRODUCTION_PATHS=1` (the same operator
#   switch L2.12 honours) re-enables writable opens for the rare test that
#   genuinely must mutate production state.
#
#   The exception is `ProductionCachePathError` and is intentionally NOT
#   `web/livetrading/storage.py::ProductionPathError`: `common/` must not import
#   the web package (layering, and `web/` is not on every sys.path), and a
#   distinct type keeps the two nets from being confused when reading a
#   traceback. Both derive from `RuntimeError`, so a caller that wants one net
#   to cover the other can catch `RuntimeError`.

_PYTEST_MARKER_ENV = "PYTEST_CURRENT_TEST"
_ALLOW_PRODUCTION_ENV = "LIVETRADING_ALLOW_PRODUCTION_PATHS"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
#: Markers used to discover the workspace root from this file's own location
#: (mirrors `web/livetrading/storage.py::find_repo_root`, incl. the 10-level cap
#: so a relocated/frozen install cannot walk to `/`).
_REPO_ROOT_MARKERS = ("AGENTS.md", ".git")


class ProductionCachePathError(RuntimeError):
    """A pytest process tried to open the live market cache read-write."""


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in _TRUE_ENV_VALUES


def production_cache_guard_active() -> bool:
    """True only under pytest without the operator opt-in.

    Single source of truth for the gate. This is the first thing the guard
    checks so a normal (non-pytest) process never pays for path resolution.
    """
    return bool(os.environ.get(_PYTEST_MARKER_ENV)) and not _truthy_env(_ALLOW_PRODUCTION_ENV)


def _find_repo_root() -> str:
    """Workspace root, discovered from this file (`common/duckdb_cache.py`).

    Markers match `web/livetrading/storage.py::find_repo_root` so both guards
    agree on what `<repo>` means; the two-level fallback covers a checkout whose
    markers were stripped (e.g. an exported tarball).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(10):
        if any(os.path.exists(os.path.join(d, m)) for m in _REPO_ROOT_MARKERS):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.abspath(os.path.join(here, ".."))


def production_cache_roots() -> List[str]:
    """Directories a pytest process must never open read-write.

    Deliberately `<repo>/data` only: that is the production OHLCV cache
    (`cache.duckdb`). Live *portfolio* directories outside the repo are
    `StorageManager`'s concern (L2.12); this net is about the market cache, so a
    test may still write its own scratch directories anywhere else.
    """
    return [os.path.join(_find_repo_root(), "data")]


def _canonical_path(path: Any) -> str:
    """Absolute, symlink- and `..`-resolved form for alias-proof comparison."""
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _is_within(path: Any, root: Any) -> bool:
    """True when `path` is `root` or a descendant of it (both canonicalized)."""
    path_c = _canonical_path(path)
    root_c = _canonical_path(root)
    if path_c == root_c:
        return True
    try:
        return os.path.commonpath([path_c, root_c]) == root_c
    except ValueError:  # different drives (Windows): cannot be nested
        return False


def refuse_production_cache_path(db_path: Any, read_only: bool = False) -> None:
    """Raise `ProductionCachePathError` if a writable open targets `<repo>/data`.

    `db_path` must be the FINAL database file (after the "directory ->
    `<dir>/cache.duckdb`" resolution), so `<repo>/data`, `<repo>/data/`, and
    `<repo>/data/../data/cache.duckdb` are all caught by containment.
    """
    if read_only or not production_cache_guard_active():
        return
    for root in production_cache_roots():
        if _is_within(db_path, root):
            raise ProductionCachePathError(
                f"Refusing to open the DuckDB cache read-write at "
                f"{_canonical_path(db_path)} while running under pytest "
                f"({_PYTEST_MARKER_ENV} is set): it resolves inside the production "
                f"market cache directory {_canonical_path(root)}, which holds the "
                f"live data/cache.duckdb. A test must never write there.\n"
                f"  fix: point the cache at a temporary directory, e.g. "
                f"DuckDBCache(db_path=tmp_path / 'cache.duckdb') (or pass "
                f"cache_dir=tmp_path to CachedDataProvider).\n"
                f"  read-only inspection is still allowed: "
                f"DuckDBCache(db_path=..., read_only=True).\n"
                f"  operator escape hatch: set {_ALLOW_PRODUCTION_ENV}=1."
            )


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


class _MetadataReconciliation(NamedTuple):
    """Reconciled sync-metadata boundary values plus a human-readable repair log.

    Field names are axis-neutral (`earliest` / `latest`) because this one type serves BOTH axes:
    `asset_sync_metadata.earliest_available_date` (daily, `date`) and
    `intraday_sync_metadata.earliest_available_ts` (intraday, `timestamp`) -- L2.14 requires the
    intraday side to be structurally identical to the daily side, not a second drifting copy.
    """

    earliest: Optional[Any]
    is_earliest_known: bool
    latest: Optional[Any]
    #: One entry per repair performed; `store_bars` / `store_intraday_bars` turn each into a
    #: `logger.warning` (L2.18 / L2.14).
    repairs: List[str]


def _reconcile_sync_metadata_with_bars(
    *,
    prior: Optional[Tuple[Any, Any, Any]],
    bars_min: Optional[Any],
    bars_max: Optional[Any],
    frame_min: Optional[Any],
    frame_max: Optional[Any],
    requested_start: Optional[str] = None,
    table: str = "asset_sync_metadata",
    earliest_field: str = "earliest_available_date",
    latest_field: str = "latest_available_date",
    bars_table: str = "ohlcv_bars",
) -> _MetadataReconciliation:
    """THE reconciliation core, shared by the daily and intraday sync-metadata tables (L2.18/L2.14)."""

    prior_earliest = prior[0] if prior else None
    prior_earliest_known = bool(prior[1]) if prior else False
    prior_latest = prior[2] if prior else None

    repairs: List[str] = []

    if (
        prior is None
        and bars_min is not None
        and bars_max is not None
        and frame_min is not None
        and frame_max is not None
        and (bars_min < frame_min or bars_max > frame_max)
    ):
        # The row is gone but bars this window never supplied are still on disk -> the row was
        # deleted (or lost) rather than never created. A genuinely first-ever store has
        # bars_min == frame_min and bars_max == frame_max, so it is (correctly) not reported.
        repairs.append(
            f"{table} row was missing while {bars_table} already held "
            f"{bars_min}..{bars_max}; rebuilt the boundaries from the bar table "
            f"(deleting the row is not a durable exclusion -- see docs/version.md L2.18)"
        )
    if prior_earliest is not None and bars_min is not None and prior_earliest < bars_min:
        repairs.append(
            f"recorded {earliest_field} {prior_earliest} is older than the oldest stored "
            f"bar {bars_min}; clamped to the bar fact (the cache cannot serve history it "
            f"no longer holds)"
        )
    if prior_latest is not None and bars_max is not None and prior_latest > bars_max:
        repairs.append(
            f"recorded {latest_field} {prior_latest} is newer than the newest stored bar "
            f"{bars_max}; clamped to the bar fact (metadata claimed a bar that never landed -- "
            f"the 2026-10-22 stale-mark shape)"
        )
    if (
        prior_earliest is not None
        and bars_min is not None
        and frame_min is not None
        and prior_earliest > bars_min
        and frame_min > bars_min
    ):
        # This frame did not contribute the older bar, so the contradiction pre-existed the sync:
        # metadata claimed a later start than the data on disk (the 2026-10-22 six-symbol shape).
        repairs.append(
            f"recorded {earliest_field} {prior_earliest} was later than the oldest stored "
            f"bar {bars_min} which this window did not supply; restored to the bar fact"
        )

    if bars_min is not None:
        earliest_candidate: Optional[Any] = bars_min
    else:
        known_earliest = [d for d in (prior_earliest, frame_min) if d is not None]
        earliest_candidate = min(known_earliest) if known_earliest else None

    if bars_max is not None:
        latest_candidate: Optional[Any] = bars_max
    else:
        known_latest = [d for d in (prior_latest, frame_max) if d is not None]
        latest_candidate = max(known_latest) if known_latest else None

    # "Asked for a start strictly earlier than everything we know, got nothing older back" is the
    # ONLY evidence that the earliest date is the first bar. Measured against the reconciled
    # earliest (>= bars_min), so a window that merely starts later can never claim it.
    probed_before_earliest = False
    if requested_start and earliest_candidate is not None:
        req_start_date = pd.to_datetime(requested_start).date()
        probed_before_earliest = (
            req_start_date < pd.Timestamp(earliest_candidate).date() - datetime.timedelta(days=7)
        )

    return _MetadataReconciliation(
        earliest=earliest_candidate,
        is_earliest_known=prior_earliest_known or probed_before_earliest,
        latest=latest_candidate,
        repairs=repairs,
    )


def _reconcile_asset_metadata_with_bars(
    *,
    prior: Optional[Tuple[Any, Any, Any]],
    bars_min: Optional[datetime.date],
    bars_max: Optional[datetime.date],
    frame_min: Optional[datetime.date],
    frame_max: Optional[datetime.date],
    requested_start: Optional[str] = None,
) -> _MetadataReconciliation:
    """Reconciles the sync-metadata boundaries against the ONLY authority: `ohlcv_bars` (L2.18).

    Priority order, highest first (this is the whole point of the function):

      1. **bar facts** -- `MIN(date)` / `MAX(date)` actually present in `ohlcv_bars` for
         (provider, symbol) after the current insert. When they exist they ARE the boundaries.
      2. **prior metadata** -- only as a fallback for the (degenerate) case of a symbol with an
         existing row and zero bars, and to carry `is_earliest_known` forward.
      3. **this frame / `requested_start`** -- the *window a caller happened to ask for*. It never
         describes availability: asking for 2024-01-02 while 2017 rows sit on disk says nothing
         about 2017, and a "narrow window" sync must not shrink the recorded boundaries.

    2026-10-22 forensic context (why this exists): deleting an `asset_sync_metadata` row is NOT a
    durable way to exclude an asset -- the next sync enumerates the universe and re-inserts the row
    (measured: rebuilt within **156 seconds** by a real Stage-1 health check). Exclusion belongs in
    the universe/config layer and `docs/black_list.md`. Because such surgery happens anyway, the
    cache must heal itself: a missing row, or a row whose boundaries contradict the bars it
    describes, is rebuilt here from the bar table -- never from the request window that triggered
    the sync. The durable, human-facing version of this rule lives in `docs/version.md` (L2.18 SOP).

    Three contradictions are repaired (each returns a `repairs` entry so the caller can log a
    WARNING -- a silent repair is indistinguishable from a silent corruption):

      * the row was **missing** while bars this window did not supply were already on disk ->
        rebuilt from the bar span (a genuinely first-ever store is silent: its frame *is* the
        whole bar span, so there is nothing to reconcile);
      * recorded earliest is **older than any stored bar** -> clamped up to `bars_min` (the row
        claimed history the cache cannot serve, which is how a hand-trimmed/purged symbol can be
        reported as "covered" for a range it no longer holds);
      * recorded latest is **newer than the newest stored bar** -> clamped down to `bars_max` (the
        2026-10-22 stale-mark shape: bookkeeping said "through today" while `ohlcv_bars` stopped
        on the previous session). `L2.6`'s monotone `GREATEST` deliberately cannot fix this one --
        it only ever refuses to move the date *backwards* -- so it is repaired here explicitly.

    `is_earliest_known` keeps `L2.6`'s sticky-True contract: `prior OR probed`, where the probe is
    "this request asked for a start strictly earlier than everything we know and got nothing older
    back". Nothing in this function can clear it.
    """
    # L2.14: the body lives ONCE, in `_reconcile_sync_metadata_with_bars`, and is shared with the
    # intraday table. Fields/table names are passed in only to keep the repair log readable.
    return _reconcile_sync_metadata_with_bars(
        prior=prior,
        bars_min=bars_min,
        bars_max=bars_max,
        frame_min=frame_min,
        frame_max=frame_max,
        requested_start=requested_start,
        table="asset_sync_metadata",
        earliest_field="earliest_available_date",
        latest_field="latest_available_date",
        bars_table="ohlcv_bars",
    )


def _reconcile_intraday_metadata_with_bars(
    *,
    prior: Optional[Tuple[Any, Any, Any]],
    bars_min: Optional[Any],
    bars_max: Optional[Any],
    frame_min: Optional[Any],
    frame_max: Optional[Any],
    requested_start: Optional[str] = None,
) -> _MetadataReconciliation:
    """Intraday-facing wrapper over the SAME reconciliation used by the daily side (L2.14).

    `L2.14`'s whole point is that the intraday metadata table was a byte-for-byte copy of `L2.6`'s
    defect, one axis over: `store_intraday_bars` wrote `intraday_sync_metadata` with a blind
    `INSERT OR REPLACE`, and its `requested_start` branch *moved the recorded earliest boundary
    forward* to the first bar of the current window. Both are fixed by routing intraday writes
    through this wrapper instead of maintaining a parallel rule set (a second copy would drift --
    the exact bug class L2.14 exists to remove, see also L2.7 on the freshness gate).

    Only the table/column names and the bar-table name differ; the priority order is identical:
    `intraday_ohlcv_bars`' `MIN(ts)`/`MAX(ts)` are the authority, prior metadata is a fallback,
    and the request window never overrides either.
    """
    return _reconcile_sync_metadata_with_bars(
        prior=prior,
        bars_min=bars_min,
        bars_max=bars_max,
        frame_min=frame_min,
        frame_max=frame_max,
        requested_start=requested_start,
        table="intraday_sync_metadata",
        earliest_field="earliest_available_ts",
        latest_field="latest_available_ts",
        bars_table="intraday_ohlcv_bars",
    )


# -----------------------------------------------------------------------------------------------
# L2.2 / L2.7 -- THE expected-session freshness contract (one implementation, two axes)
#
# `is_range_covered` (daily) and `is_intraday_range_covered` (intraday) both have to answer the
# same question for their own axis: "is the session the caller says should already be on disk
# actually there?". Historically the intraday side answered a weaker question (metadata only,
# `count(*) > 0`), i.e. exactly the shape that produced the 2026-10-22 stale daily bars. `L2.7`
# requires bringing it up to the daily rule -- and the only acceptable way to do that is to share
# the code, because a second copy would drift (design §7: "判断必须有且只有一个所有者").
#
# TRADING-DAY SEMANTICS ARE *NOT* RE-DERIVED HERE. `expected_last_session` is by contract a trading
# session produced by `common/market_clock.py::expected_last_session` (== `as_of_trading_date`,
# i.e. close + publication grace). That module owns the holiday/weekend calendar; this one must not
# invent a parallel weekday guess (design §6, §10). Consequence: in expected-session mode there is
# no "7-day tolerance" and no "already synced today" short-circuit -- those existed only to guess
# what the calendar already knows.
#
# L2.26: SESSION TIME IS RE-DERIVED THERE TOO. On the intraday axis a date match is not freshness:
# `MAX(ts).date() == expected` also holds when the bars stopped at 11:30 (lunch) or at the first
# 09:35 bar. The bar-layer rule therefore takes an OPTIONAL session-time input
# (`expected_session_deadline` / `expected_session_max_ts` in `_bar_layer_reaches_end`), defaulting
# to `None` == date-only (the daily axis, and the pre-L2.26 intraday answer). The deadline comes
# from `common/market_clock.py::last_expected_bar_ts` (market close - one bar length), never from a
# 15:00/16:00 literal in this file.
# -----------------------------------------------------------------------------------------------


def _coerce_session_date(value: Any, what: str = "expected_last_session") -> Optional[datetime.date]:
    """Coerce a date-like (ISO str / `date` / `datetime` / `Timestamp`) to a plain `date`.

    An unparseable expectation is a *caller bug*, and it fails loudly here rather than being
    silently swallowed into "no expectation" -- a freshness gate that quietly degrades to the
    never-raise legacy path is precisely the failure class this module exists to remove.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{what}={value!r} is not a parseable date; the expected-session contract needs an "
            f"ISO date / date object (see common/market_clock.py::expected_last_session)"
        ) from exc


def _expected_session_is_fresh(
    latest_available: Any,
    expected_last_session: datetime.date,
) -> bool:
    """METADATA half of the contract: `latest_available_date >= expected_last_session`.

    That is the whole rule (design §6): once an expectation is supplied, no tolerance window and
    no "we synced today" short-circuit may override it. `latest_available` may be a `date`, a
    `Timestamp` (intraday tracks a bar timestamp) or `None` (never synced => not fresh).
    """
    latest = _coerce_session_date(latest_available, "latest_available_date")
    return latest is not None and latest >= expected_last_session


def _bar_layer_reaches_end(
    max_bar_session: Any,
    req_end_date: datetime.date,
    expected_last_session: Optional[datetime.date],
    current_date: datetime.date,
    expected_session_deadline: Optional[datetime.datetime] = None,
    expected_session_max_ts: Any = None,
    log_context: str = "",
) -> bool:
    """BAR half of the contract, shared by the daily and intraday gates.

    Bar facts outrank metadata bookkeeping (L2.9 / L2.18): metadata records what a sync
    *attempted*, the bar table records what landed. Two rules:

      * expected-session mode -- the newest bar must reach `expected_last_session`. No weekday
        exception: the expectation is a session by contract (see the block comment above), so a
        shorter bar span is unambiguous staleness.
      * the request-end rule (identical in both modes) -- when the caller explicitly asked for a
        *past* end, the bars must reach it too; a Saturday/Sunday end is satisfied by the
        preceding Friday, because no exchange session could ever produce that bar. Weekday
        exchange holidays are deliberately NOT inferred here (a redundant re-fetch is cheap, a
        stale mark-to-market is not -- see `is_range_covered`'s long docstring).

    An `end` beyond `current_date` is not judged (no provider can have that bar yet).

    `L2.26` -- the optional **session-time** half (`expected_session_deadline`,
    `expected_session_max_ts`): on an intraday axis a DATE match is not freshness. "Metadata
    says today, bars stopped at 11:30 / at the very first 09:35 bar" would pass a date-only
    comparison, which is the 2026-10-22 incident copied onto 5m. So when the caller supplies
    the deadline of `expected_last_session` (market close minus one bar length, obtained from
    `common/market_clock.py::last_expected_bar_ts` -- never a literal here), the newest bar
    **inside that session** must be `>= deadline`. Both parameters default to `None`, i.e.
    "date only", which is exactly the daily axis and the pre-L2.26 behaviour -- the rule stays
    ONE function with an extra input, not a second branch that could drift.

    Why `expected_session_max_ts` is measured separately instead of reusing `max_bar_session`:
    the query bound is widened to `max(req_end, expected)` so a caller asking for an earlier
    end cannot hide today's bar; consequently `max_bar_session` may belong to a *later*
    session, and comparing that timestamp to this session's deadline would let a later day
    mask a truncated expected day. The caller therefore passes the max ts *within*
    `expected_last_session`'s own calendar day.
    """
    if max_bar_session is None:
        # No bar at all in/before the range: never covered (replaces the old `count(*) > 0`,
        # which reported coverage for a symbol whose only bars sat outside the range).
        return False
    max_bar_date = _coerce_session_date(max_bar_session, "max_bar_session")
    if expected_last_session is not None and max_bar_date < expected_last_session:
        return False
    if max_bar_date < req_end_date and req_end_date <= current_date:
        if req_end_date.weekday() < 5:
            return False
    if expected_session_deadline is not None and expected_last_session is not None:
        # L2.26 time-of-day rule (intraday only). Lunch break needs no state of its own: the
        # deadline is anchored to the afternoon close, so an 11:30 / 12:00 last bar is hours
        # short of it, while a 14:55+ / 15:55+ bar clears it. One bar of tolerance absorbs the
        # bar-open vs bar-close timestamp conventions (see `last_expected_bar_ts`).
        if expected_session_max_ts is None:
            logger.warning(
                "Intraday cache is stale on the session axis%s: no bar at all on the expected "
                "session %s (deadline %s)",
                f" [{log_context}]" if log_context else "",
                expected_last_session.isoformat(),
                pd.Timestamp(expected_session_deadline).isoformat(),
            )
            return False
        session_last_ts = pd.Timestamp(expected_session_max_ts)
        if session_last_ts < pd.Timestamp(expected_session_deadline):
            logger.warning(
                "Intraday cache is stale on the session axis%s: expected a bar on %s reaching "
                ">= %s (session close minus one bar), newest bar that day is %s",
                f" [{log_context}]" if log_context else "",
                expected_last_session.isoformat(),
                pd.Timestamp(expected_session_deadline).isoformat(),
                session_last_ts.isoformat(),
            )
            return False
    return True


def _intraday_expected_session_deadline(
    symbol: str,
    interval: str,
    expected_session: datetime.date,
) -> Optional[datetime.datetime]:
    """Deadline the expected intraday session's final bar must reach (L2.26).

    Single source of the session geometry: `common/market_clock.py` (market spec close time
    minus one bar length). No 15:00 / 16:00 literal appears in this module.

    `None` means "cannot judge the time of day" -- unknown interval length, or a symbol whose
    market cannot be inferred by `infer_market_from_symbol`. Both are loud (logged) degraded
    checks, and the caller then falls back to the date-only rule. Degrading is deliberate: this
    is a *coverage predicate* whose caller (`common/data.py::fetch_universe`) turns an exception
    into "skip this symbol", so raising here would silently drop an asset from the universe --
    the exact silent-degradation failure class this track removes. A wrong market, on the other
    hand, cannot happen silently for a real universe: every live symbol carries .SH/.SZ/.BJ/.HK
    /a US ticker shape, and the clock layer already fails loud at `resolve_market`.
    """
    bar_interval = _intraday_bar_interval(interval)
    if bar_interval is None:
        logger.warning(
            "Intraday freshness: unknown bar length for interval %r (symbol %s); the "
            "time-of-day half of the session check is skipped",
            interval,
            symbol,
        )
        return None
    try:
        market = infer_market_from_symbol(symbol)
    except UnknownSymbolMarketError as exc:
        logger.warning(
            "Intraday freshness: cannot infer the market of %r (%s); the time-of-day half of "
            "the session check is skipped and only the last bar's DATE is verified",
            symbol,
            exc,
        )
        return None
    return last_expected_bar_ts(market, expected_session, bar_interval)


def _bar_layer_query_bound(
    req_end_date: datetime.date,
    expected_last_session: Optional[datetime.date],
) -> datetime.date:
    """Upper bound for the bar-span query: wide enough to *see* the expected session.

    Legacy mode asks exactly what the caller asked for (bit-identical to the pre-L2.2 SQL). In
    expected-session mode the bound must at least cover the expected session, otherwise a caller
    requesting an earlier `end` would hide today's bar from the check (and pass by accident).
    """
    if expected_last_session is None:
        return req_end_date
    return max(req_end_date, expected_last_session)


class DuckDBCache:
    """DuckDB columnar cache for multi-provider OHLCV bar data and asset metadata.

    Schema:
      - ohlcv_bars: (provider, symbol, date) -> open, high, low, close, volume
      - asset_sync_metadata: (provider, symbol) -> earliest_available_date, is_earliest_known,
                                                  latest_available_date, last_sync_time

    `read_only=True` opens an EXISTING database without creating directories or
    tables, for audit scripts / tests that only inspect a cache. It is never
    blocked by the pytest production-path net (see `refuse_production_cache_path`);
    a read-only instance refuses every write connection with `RuntimeError`.
    """

    def __init__(self, db_path: Optional[str] = None, read_only: bool = False):
        if db_path is None or db_path == ":memory:":
            if read_only:
                raise ValueError("':memory:' caches are per-process scratch state; read_only=False only")
            self.db_path = ":memory:"
            self.is_memory = True
            self.read_only = False
            self._mem_con = duckdb.connect(":memory:")
            self._init_tables(self._mem_con)
        else:
            abs_p = os.path.abspath(db_path)
            if os.path.isdir(abs_p) or not (abs_p.endswith(".duckdb") or abs_p.endswith(".db")):
                final_path = os.path.join(abs_p, "cache.duckdb")
            else:
                final_path = abs_p

            # L2.13: the single write chokepoint. Checked on the FINAL file path and
            # BEFORE `makedirs`, so a refused open leaves no directory behind either.
            refuse_production_cache_path(final_path, read_only=read_only)

            if read_only:
                # Fail at construction, not at the first query, when the file is absent.
                if not os.path.exists(final_path):
                    raise FileNotFoundError(
                        f"DuckDBCache(read_only=True) requires an existing cache file, "
                        f"but {os.path.abspath(final_path)} does not exist."
                    )
            else:
                os.makedirs(os.path.dirname(final_path), exist_ok=True)

            self.db_path = final_path
            self.is_memory = False
            self.read_only = bool(read_only)
            self._mem_con = None
            if read_only:
                # Validate readability eagerly (missing/corrupt/permission-denied file).
                con = self._get_connection(read_only=True)
                con.close()
            else:
                # Initialize tables on disk file
                con = self._get_connection(read_only=False)
                try:
                    self._init_tables(con)
                finally:
                    con.close()

    def _get_connection(self, read_only: bool = False):
        if self.is_memory:
            return self._mem_con
        if not read_only and getattr(self, "read_only", False):
            raise RuntimeError(
                f"This DuckDBCache was opened read-only ({self.db_path}); refusing to "
                f"request a writable connection."
            )

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

        # L2.2 schema migration: expected-session probe bookkeeping on BOTH sync-metadata tables.
        # `ADD COLUMN IF NOT EXISTS` is not supported uniformly across DuckDB versions, so the
        # existence probe + ALTER is done defensively per column. Idempotent by construction:
        # re-running `_init_tables` (every writable open) on an already-migrated DB is a no-op,
        # and an old on-disk cache is upgraded in place -- no data migration is needed because
        # `latest_available_date` / `last_sync_time` keep their meaning and existing rows simply
        # take the `probe_attempts` default of 0.
        for table in ("asset_sync_metadata", "intraday_sync_metadata"):
            self._ensure_column(con, table, "expected_last_session", "DATE")
            self._ensure_column(con, table, "probe_attempts", "INTEGER DEFAULT 0")
            self._ensure_column(con, table, "last_probe_result", "VARCHAR")
            self._ensure_column(con, table, "last_probe_time", "TIMESTAMP")

    @staticmethod
    def _ensure_column(con, table: str, column: str, column_type: str) -> None:
        """Idempotent `ALTER TABLE ADD COLUMN` -- the L2.2 migration primitive.

        Probe first, alter only when absent: a second call (or a second process opening the same
        legacy file) must never raise `Binder Error: column already exists`.
        """
        existing = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if column in existing:
            return
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

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
        expected_last_session: Optional[datetime.date] = None,
    ) -> bool:
        """Checks if the requested intraday date/time range [start, end] is fully covered by cached bars.

        Two contracts, selected by `expected_last_session`:

          * `None` (backtest / research / historical replay) -- the LEGACY heuristic, kept
            bit-for-bit: metadata end-coverage with its 7-day tolerance plus the "already synced
            today" short-circuit, then `count(*) > 0`. L2.2's hard constraint is that a
            `None` caller must observe exactly the pre-L2.2 answers (see case 10 in
            `common/tests/test_duckdb_cache.py`), so nothing on this branch may be tightened.
          * a trading session (`common/market_clock.py::expected_last_session`, live path) --
            `L2.7` closes the same hole the daily gate closed in `L2.9`: metadata AND the bar
            layer's `MAX(ts)` must both reach that session, via the SHARED helpers
            `_expected_session_is_fresh` / `_bar_layer_reaches_end` (no third copy of the rule).
            `L2.26` adds the session-TIME half: the newest bar inside the expected session must
            also reach that session's close minus one bar length, taken from the market spec in
            `common/market_clock.py` (A-share close 15:00, HK/US 16:00). Without it, "metadata
            says today, bars stopped at 11:30 (lunch)" or even "bars stopped at the first 09:35
            bar" passed on the date alone -- the 2026-10-22 staleness copied onto 5m. Lunch
            breaks need no special state: an 11:30 bar is hours short of the afternoon-close
            deadline, so "mid-lunch" and "closed early" are told apart by the clock itself.
            Known gap (deliberately not faked): half-days / early closes are NOT modelled
            (`common/market_holidays.py` tracks full-day closures only), so a US 13:00 early
            close looks "short" until the next session. See `L2.26` in
            `docs/development_plan.md`.

        L2.7 disclosure: this is a PREVENTATIVE fix. Fuyao does not serve 5m at all
        (`common/financial_api.py` raises for any non-`1d` interval), so no real intraday cache
        is currently populated upstream and no incident has happened here. The defect was latent:
        once a real 5m source is wired in, "metadata says today, bars stop yesterday" would have
        been copied from the daily path into the intraday one.
        """
        if is_synthetic_provider(provider):
            return False

        # Caller-bug check FIRST: a garbage expectation must fail loudly even for a symbol with no
        # metadata at all (an early `return False` would otherwise swallow it silently).
        expected_date = _coerce_session_date(expected_last_session)

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

        if expected_date is not None:
            # L2.2 contract (shared with the daily gate): freshness is decided by the expected
            # session alone -- the 7-day tolerance and the "synced today" short-circuit are gone.
            end_covered = _expected_session_is_fresh(latest_avail, expected_date)
        elif req_end_date >= current_date:
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

        # L2.26: the session-TIME half, computed only when an expectation exists (the `None`
        # path stays bit-for-bit pre-L2.2, so it must not even resolve a market). `None` here
        # means "cannot judge the time of day" (unknown interval length / inferable market),
        # which the helper logs; the date-only rule below still applies.
        session_deadline = (
            _intraday_expected_session_deadline(symbol, interval, expected_date)
            if expected_date is not None
            else None
        )

        con = self._get_connection(read_only=True)
        try:
            if expected_date is None:
                count = con.execute(
                    "SELECT count(*) FROM intraday_ohlcv_bars WHERE provider = ? AND symbol = ? AND interval = ?",
                    [provider, symbol, interval],
                ).fetchone()[0]
                return count > 0

            # L2.7 / L2.26: same bar-layer end check as the daily gate, on the intraday axis,
            # plus the session-time half. `ts` is a TIMESTAMP, so the request end (a DATE) is
            # turned into an exclusive next-midnight bound. The second aggregate is the max ts
            # *inside the expected session's own calendar day* -- that is the value the deadline
            # is measured against, so a later session in the cache cannot mask a truncated
            # expected one (see `_bar_layer_reaches_end`).
            upper_date = _bar_layer_query_bound(req_end_date, expected_date)
            session_start = pd.Timestamp(expected_date)
            session_end = session_start + pd.Timedelta(days=1)
            row = con.execute(
                "SELECT MAX(ts), MAX(CASE WHEN ts >= ? AND ts < ? THEN ts END) "
                "FROM intraday_ohlcv_bars "
                "WHERE provider = ? AND symbol = ? AND interval = ? AND ts < ?",
                [
                    session_start,
                    session_end,
                    provider,
                    symbol,
                    interval,
                    pd.Timestamp(upper_date) + pd.Timedelta(days=1),
                ],
            ).fetchone()
            max_ts, session_max_ts = row if row else (None, None)
        finally:
            if not self.is_memory:
                con.close()

        return _bar_layer_reaches_end(
            max_ts,
            req_end_date,
            expected_date,
            current_date,
            expected_session_deadline=session_deadline,
            expected_session_max_ts=session_max_ts,
            log_context=f"{provider}/{symbol} {interval}",
        )

    def is_range_covered(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        now: Optional[pd.Timestamp] = None,
        interval: str = "1d",
        expected_last_session: Optional[datetime.date] = None,
    ) -> bool:
        """Checks if the requested date range [start, end] is fully covered by cached bars.
        Cached historical data never expires.

        Coverage is a TWO-layer contract, and both layers must pass:

          1. Metadata layer (`asset_sync_metadata`): the provider's earliest/latest known
             dates plus `last_sync_time` decide whether cache *bookkeeping* says this range
             has been synced (weekend/holiday-tolerant 7-day window, plus a "did anybody
             already sync today?" short-circuit) -- **legacy mode only**, see below.
          2. Bar layer (`ohlcv_bars`): the maximum stored bar date must actually reach the
             requested end, and (L2.18) a requested start may not fall into a front hole that
             the metadata cannot justify. The metadata layer alone records what a sync
             *attempted*, not what landed on disk, so it can never be trusted by itself.

        `expected_last_session` (L2.2) selects the freshness contract:

          * `None` -- legacy/backtest path, kept BIT-FOR-BIT as it was: the 7-day tolerance, the
            `last_sync_date >= current_date` short-circuit and the weekend branch all stay.
            Backtests, research replays and every `run_backtest.py` / `pipeline/*` /
            `common/order_netting.py` caller live here, so their results must not move by a
            single bit (`common/tests/test_duckdb_cache.py` case 10 pins this against a table
            recorded from the pre-L2.2 implementation).
          * a trading session (`common/market_clock.py::expected_last_session`, i.e. close +
            publication grace) -- `covered <=> latest_available_date >= expected_last_session`,
            with the 7-day tolerance and the "already synced today" short-circuit **abolished**,
            and the bar layer's `MAX(date)` also required to reach that session. This is exactly
            the six-symbol state of 2026-10-22: "we synced today" can no longer stand in for
            "today's bar is on disk".

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

        Contract for callers: pass a *trading-session* date as `end` (and as
        `expected_last_session`). This check is deliberately calendar-free (the market clock is
        owned elsewhere, design §6/§10 -- no second holiday/weekday system may be derived here),
        so it tolerates the one unambiguous non-session case it can detect without a holiday
        calendar: an `end` falling on a Saturday/Sunday is treated as covered once bars reach the
        preceding Friday -- otherwise every weekend run would look uncovered forever and
        re-fetch pointlessly. Weekday exchange holidays are NOT inferred: if a caller passes
        a weekday holiday as `end`, the bars legitimately stop on the prior session and this
        method reports "uncovered", costing one redundant upstream fetch. That is a
        deliberate fail-safe trade -- a redundant fetch is cheap, a stale mark-to-market is
        not. (`expected_last_session` needs no such tolerance: it is a session by contract.)

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
                provider,
                symbol,
                interval=interval,
                start=start,
                end=end,
                now=now,
                expected_last_session=expected_last_session,
            )
        if is_synthetic_provider(provider):
            return False

        # Caller-bug check FIRST (see the intraday gate): never let a `return False` for an
        # unknown symbol mask a malformed `expected_last_session`.
        expected_date = _coerce_session_date(expected_last_session)

        meta = self.get_asset_metadata(provider, symbol)
        if not meta:
            return False

        earliest_avail = meta["earliest_available_date"]
        is_earliest_known = meta["is_earliest_known"]
        latest_avail = meta["latest_available_date"]

        # Start coverage check
        req_start_date: Optional[datetime.date] = None
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

        if expected_date is not None:
            # L2.2 (live path): one rule, no tolerance, no wall-clock proxy. "We already synced
            # today" says nothing about whether today's bar is on disk -- that was the whole
            # 2026-10-22 defect. The bar layer below then independently requires MAX(date) to
            # reach the same session, so the two layers must agree.
            end_covered = _expected_session_is_fresh(latest_avail, expected_date)
        elif req_end_date >= current_date:
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
        #
        # L2.18: the same query also fetches the oldest bar in range (same round-trip, so the
        # normal path pays nothing extra) because the metadata's START side can lie in exactly the
        # same way -- a row rebuilt from a narrow window, a legacy import, or a hand-trimmed bar
        # table can claim history the cache no longer holds. `store_bars` repairs that state on the
        # next write; until then this gate must not report coverage for it.
        #
        # L2.2: in expected-session mode the upper bound widens to at least the expected session
        # (`_bar_layer_query_bound`) so a caller asking for an *earlier* end cannot hide today's
        # bar from the check. `MIN` keeps its original `date <= req_end_date` bound via the CASE, so
        # the L2.18 front-hole rule is unchanged even when the bound widens.
        bar_upper_bound = _bar_layer_query_bound(req_end_date, expected_date)
        con = self._get_connection(read_only=True)
        try:
            span_row = con.execute(
                "SELECT MIN(CASE WHEN date <= ? THEN date END), MAX(date) FROM ohlcv_bars "
                "WHERE provider = ? AND symbol = ? AND date <= ?",
                [req_end_date, provider, symbol, bar_upper_bound],
            ).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        min_bar_date, max_bar_date = span_row if span_row else (None, None)
        if isinstance(max_bar_date, (pd.Timestamp, datetime.datetime)):
            max_bar_date = max_bar_date.date()
        if isinstance(min_bar_date, (pd.Timestamp, datetime.datetime)):
            min_bar_date = min_bar_date.date()

        if not _bar_layer_reaches_end(max_bar_date, req_end_date, expected_date, current_date):
            return False

        # Layer 3 (L2.18): reject a *front hole* -- the request starts strictly before the oldest
        # bar in range while the metadata has NOT established "nothing exists earlier"
        # (`is_earliest_known` False). This is the same "never accept a metadata claim the bars do
        # not back" rule as layer 2, on the other boundary; the 7-day tolerance mirrors the start
        # check above (weekend/holiday gap, not a hole). Deliberately fail-safe: a redundant
        # upstream fetch is cheap, silently serving a truncated history is not.
        if (
            req_start_date is not None
            and not is_earliest_known
            and min_bar_date is not None
            and min_bar_date > req_start_date + datetime.timedelta(days=7)
        ):
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

        L2.14: the metadata half is NOT a blind `INSERT OR REPLACE` any more. It is reconciled
        against `intraday_ohlcv_bars` (`_reconcile_intraday_metadata_with_bars`, the SAME core as
        the daily table), so the recorded earliest/latest boundaries are monotone and always
        reportable as bar facts: a later window cannot downgrade the earliest boundary, a
        front-truncated window cannot roll it forward, `is_earliest_known` is sticky True, and the
        request window never overrides what the bars say. See the long comment at the write site.
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
                    # The purge removed the row with the bars; the reconciliation below re-reads
                    # whatever survives instead of trusting the pre-purge snapshot.

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
            # ------------------------------------------------------------------------------
            # MONOTONIC + SELF-HEALING `intraday_sync_metadata` upsert (L2.14)
            #
            # This is `store_bars`' `L2.6`/`L2.18` contract on the intraday axis, DELIBERATELY
            # sharing `_reconcile_sync_metadata_with_bars` rather than re-deriving it:
            #
            #   * The previous implementation was a blind `INSERT OR REPLACE` of values computed in
            #     Python from (prior meta, this frame, `requested_start`), so it also destroyed the
            #     `L2.2` probe bookkeeping columns on every sync. Worse, its `requested_start`
            #     branch ran `earliest_avail = min_ts` UNCONDITIONALLY -- a sync that asked for an
            #     early window but was served a later one ROLLED THE RECORDED EARLIEST BOUNDARY
            #     FORWARD (e.g. 2022-01-04 -> the window's first bar), i.e. metadata stopped
            #     describing the bars still on disk. That is `L2.6`'s 2026-10-22 downgrade incident
            #     copied onto the 5m axis. It was LATENT only because no real 5m upstream exists
            #     (Fuyao serves no intraday; see `is_intraday_range_covered`'s disclosure).
            #   * `intraday_ohlcv_bars`' `MIN(ts)`/`MAX(ts)` are the authority; prior metadata is
            #     only a fallback (row exists, zero bars) and to carry `is_earliest_known`; this
            #     frame and `requested_start` never override either. A row claiming a boundary the
            #     bars contradict -- or a row that was deleted outright -- is rebuilt/clamped from
            #     the bar table with a WARNING per repair, never from the request window.
            #   * `is_earliest_known` stays monotonic True (`existing OR excluded`); only an
            #     explicit human UPDATE may reset it.
            #   * `latest_close_ref` is taken from the bar fact at `MAX(ts)` (not from this
            #     frame's last close), so a backfill of an older window can never make the R4
            #     drift reference point at a bar that is not the newest cached one.
            # ------------------------------------------------------------------------------
            prior_row = con.execute(
                "SELECT earliest_available_ts, is_earliest_known, latest_available_ts "
                "FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
                [provider, symbol, interval],
            ).fetchone()
            bars_span = con.execute(
                "SELECT MIN(ts), MAX(ts) FROM intraday_ohlcv_bars "
                "WHERE provider = ? AND symbol = ? AND interval = ?",
                [provider, symbol, interval],
            ).fetchone()
            bars_min_ts, bars_max_ts = bars_span if bars_span else (None, None)

            reconciled = _reconcile_intraday_metadata_with_bars(
                prior=prior_row,
                bars_min=bars_min_ts,
                bars_max=bars_max_ts,
                frame_min=min_ts,
                frame_max=max_ts,
                requested_start=requested_start,
            )
            for repair in reconciled.repairs:
                logger.warning(
                    "intraday_sync_metadata self-heal for (%s, %s, %s): %s",
                    provider, symbol, interval, repair,
                )

            latest_close_ref: Optional[float] = None
            if bars_max_ts is not None:
                close_row = con.execute(
                    "SELECT close FROM intraday_ohlcv_bars "
                    "WHERE provider = ? AND symbol = ? AND interval = ? AND ts = ?",
                    [provider, symbol, interval, bars_max_ts],
                ).fetchone()
                if close_row and close_row[0] is not None:
                    latest_close_ref = float(close_row[0])

            # The column list is EXPLICIT (no bare `VALUES`): the table carries the four `L2.2`
            # probe columns, which must not be enumerated here (they are owned by the probe
            # bookkeeping, not by a sync). `ON CONFLICT DO UPDATE` instead of `INSERT OR REPLACE`:
            # DuckDB implements the latter as a per-column `DO UPDATE` over the *listed* columns,
            # so the practical difference here is intent and blast radius, not semantics -- with a
            # blind replace, any future column added to the list is silently overwritten with
            # whatever this sync computed, and there is no place to state the `LEAST`/`GREATEST`
            # merge at all. Verified empirically: the two forms behave identically on the probe
            # columns, which is why the `L2.14` regression tests pin the directional merge
            # behaviourally and the statement shape statically.
            con.execute("""
                INSERT INTO intraday_sync_metadata AS existing
                    (provider, symbol, interval, earliest_available_ts, is_earliest_known,
                     latest_available_ts, latest_close_ref, last_sync_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, symbol, interval) DO UPDATE SET
                    earliest_available_ts = COALESCE(
                        CAST(? AS TIMESTAMP),
                        LEAST(
                            COALESCE(existing.earliest_available_ts, excluded.earliest_available_ts),
                            COALESCE(excluded.earliest_available_ts, existing.earliest_available_ts)
                        )
                    ),
                    is_earliest_known = existing.is_earliest_known OR excluded.is_earliest_known,
                    latest_available_ts = COALESCE(
                        CAST(? AS TIMESTAMP),
                        GREATEST(
                            COALESCE(existing.latest_available_ts, excluded.latest_available_ts),
                            COALESCE(excluded.latest_available_ts, existing.latest_available_ts)
                        )
                    ),
                    latest_close_ref = COALESCE(
                        CAST(? AS DOUBLE), existing.latest_close_ref
                    ),
                    last_sync_time = excluded.last_sync_time
            """, [
                provider,
                symbol,
                interval,
                reconciled.earliest,
                reconciled.is_earliest_known,
                reconciled.latest,
                latest_close_ref,
                sync_ts,
                bars_min_ts,
                bars_max_ts,
                latest_close_ref,
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

        Metadata is reconciled against the bar table (see `_reconcile_asset_metadata_with_bars`
        and the long comment at the write site): `ohlcv_bars`' `MIN(date)`/`MAX(date)` are the
        authority, `L2.6`'s monotone upsert applies when no bars exist at all, and the current
        request window (`requested_start`/`requested_end`) NEVER overrides either. Storing a window
        that starts later than the known history therefore leaves the recorded earliest date
        intact -- "we did not look earlier" is not "it is not there" (2026-10-22 downgrade
        incident) -- and a missing or contradicting metadata row is rebuilt/corrected from the
        bars instead of being taken at face value (L2.18), with a WARNING per repair.
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
            # MONOTONIC + SELF-HEALING `asset_sync_metadata` upsert
            #
            # 2026-10-22 21:34:31 incident (monotonicity, L2.6): a real Fuyao sync that started
            # fetching at 2024-01-02 rewrote `earliest_available_date` to 2024-01-02 for six
            # symbols whose `ohlcv_bars` still begin in 2017-01-18 / 2020-11-30 / 2021-08-20, and
            # cleared `is_earliest_known` on 601728.SH (True -> False). The metadata then
            # contradicted the data it describes, and anything deriving a backtestable range from
            # `earliest_available_date` silently truncated ~7 years of history. Every sync from a
            # later start date damaged the field further.
            #
            # 2026-10-22 (self-healing, L2.18): the same day's two failed attempts to exclude an
            # asset by DELETEing its metadata row proved the row comes back within seconds (the
            # sync enumerates the universe), so the cache must be able to rebuild/correct the row
            # from `ohlcv_bars` instead of trusting whatever the row happens to say. The decision
            # logic lives in `_reconcile_asset_metadata_with_bars`; this site only persists it.
            #
            # The invariant enforced below is directional on purpose:
            #   * earliest_available_date: the oldest bar on disk when there is one (a row claiming
            #     history the cache no longer holds is clamped UP), else MIN(existing, incoming) --
            #     NEVER GREATEST. "We did not look earlier" is NOT "it isn't there": a sync
            #     beginning at 2024-01-02 has learned nothing about 2017 and must not overwrite that
            #     knowledge. Only a sync that genuinely reaches further back may move it.
            #   * is_earliest_known: monotonic True -- `existing OR incoming`, so a sync that
            #     says False can never erase a previously confirmed True (only a human/explicit
            #     UPDATE may reset it).
            #   * latest_available_date: the newest bar on disk when there is one (a row claiming a
            #     bar that never landed is clamped DOWN -- L2.6's GREATEST alone cannot do this,
            #     since it only refuses to move backwards), else MAX(existing, incoming).
            #   * last_sync_time: overwritten freely; it records the last *attempt*, not a fact
            #     about the data.
            # Both bar-fact bounds are passed as SQL parameters and win over the monotone merge via
            # COALESCE, so a wrong-by-construction incoming value (or a concurrent writer) still
            # cannot damage the row: the bar table is the last word. A NULL bound (symbol with a
            # metadata row but zero bars -- legacy/partially purged state) degrades to the L2.6
            # monotone merge, never to the request window.
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

            reconciled = _reconcile_asset_metadata_with_bars(
                prior=prior,
                bars_min=bars_min_date,
                bars_max=bars_max_date,
                frame_min=frame_min_date,
                frame_max=frame_max_date,
                requested_start=requested_start,
            )
            for repair in reconciled.repairs:
                logger.warning(
                    "asset_sync_metadata self-heal for (%s, %s): %s", provider, symbol, repair
                )

            con.execute("""
                INSERT INTO asset_sync_metadata AS existing
                    (provider, symbol, earliest_available_date, is_earliest_known,
                     latest_available_date, last_sync_time)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, symbol) DO UPDATE SET
                    earliest_available_date = COALESCE(
                        CAST(? AS DATE),
                        LEAST(
                            COALESCE(existing.earliest_available_date, excluded.earliest_available_date),
                            COALESCE(excluded.earliest_available_date, existing.earliest_available_date)
                        )
                    ),
                    is_earliest_known = existing.is_earliest_known OR excluded.is_earliest_known,
                    latest_available_date = COALESCE(
                        CAST(? AS DATE),
                        GREATEST(
                            COALESCE(existing.latest_available_date, excluded.latest_available_date),
                            COALESCE(excluded.latest_available_date, existing.latest_available_date)
                        )
                    ),
                    last_sync_time = excluded.last_sync_time
            """, [
                provider,
                symbol,
                reconciled.earliest,
                reconciled.is_earliest_known,
                reconciled.latest,
                sync_ts,
                bars_min_date,
                bars_max_date,
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

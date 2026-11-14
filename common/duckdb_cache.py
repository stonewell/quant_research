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
from common.data_coverage import find_volume_column

if TYPE_CHECKING:
    from .data import FundamentalMetrics


#: Self-heal messages (`asset_sync_metadata` rebuilt/corrected against the bar table) must be
#: visible to whoever is debugging a cache: L2.18 requires that a repair is never silent.
logger = logging.getLogger(__name__)


_REQUIRED_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

#: Opt-in token for `query_bars(columns=...)` / `query_intraday_bars(columns=...)` (and the
#: `query_*_universe` twins) meaning "every column the table can serve", including `Amount`.
_COLUMNS_ALL = "all"

#: Sentinel written into `asset_sync_metadata.adjustflag` / `intraday_sync_metadata.adjustflag`
#: when a writer stored bars WITHOUT declaring a basis (`store_bars(adjustflag=None)`).
#:
#: Why a sentinel exists at all (M3.6 review F1/F2): SQL NULL in that column has exactly one
#: meaning -- a LEGACY row, i.e. one that predates the `_ensure_column` migration, written when
#: the cache recorded no basis at all. That state is read-refused (`UnknownAdjustBasisError`)
#: precisely because it is the state in which a QFQ series can be served to a `raw` request. A
#: modern write that declines to label its bytes is a DIFFERENT fact ("the caller had no basis to
#: declare"), and it must stay distinguishable: collapsing it into NULL would either launder
#: legacy rows into "declared-unknown" -- destroying the migration signal `--dry-run` reports --
#: or hard-fail every caller that never had a basis to declare in the first place.
#:
#: So the sentinel is stored for undeclared writes, is NEVER forward-filled onto a legacy NULL
#: row (see the `CASE` in both metadata upserts), and -- since the P1 second pass -- reads
#: EXACTLY like a legacy NULL row: refused without `allow_legacy_basis=True`. It is the same fact
#: for the reader ("nobody stated what these bytes are"), and treating it as a silent pass-through
#: would have re-opened the F1 hazard on freshly written rows. `AdjustBasisTransitionRefused`
#: likewise gates both states: it is still an undeclared basis, so it may not later be stamped
#: with a real one.
_UNDECLARED_BASIS = "unknown"

#: Relative tolerance of the `store_bars` history-rewrite gate (`HistoryRewriteRefused`).
#: Chosen an order of magnitude above float ULP noise and far below any real re-basing:
#:   * a full re-derivation of the SAME price basis rewrites every stored row with <= ~4e-16
#:     relative wobble (double rounding on a handful of arithmetic ops), so 1e-9 lets an
#:     idempotent re-fetch through silently -- which is what every legitimate incremental sync
#:     produces, since its window starts in the past and therefore overlaps by construction;
#:   * a genuine QFQ basis shift is a percent-level move (the 2026-10-23 incident's per-symbol
#:     rescale factors were 0.9742-1.0127, i.e. 1e-2 relative), so it trips the gate.
#: Absolute floor is 0.0 on purpose (`atol=0`): the columns are prices/volumes where "close to
#: zero" is not a meaningful claim, and two genuinely-zero values compare exactly equal anyway.
_HISTORY_REWRITE_RTOL = 1e-9

#: Read-side plausibility band for the `adjust_factors.factor_hfq` values a read APPLIES (D9,
#: docs/development_plan.md).
#:
#: Why 4x: a backward (HFQ) factor is a cumulative product of corporate actions, so it only ever
#: walks UP, one step per split / bonus issue / dividend. Inside one read's applied segment the
#: factors live on ONE basis, and the steps a real action produces are percent-level (the largest
#: single sessions observed in this cache are ~1.3x, e.g. a 10-for-3 bonus). 4x against the median
#: therefore leaves an enormous margin for a genuinely split-heavy name while still catching a
#: value that cannot belong to the same series at all: the D9 incident stamped
#: `600660.SH / 2026-09-24 / 81.588562` into a series whose neighbours were 1.2452-1.3895, i.e.
#: a 58.7x deviation from the median. The band is symmetric because an as-of lookup can just as
#: well see a *too small* factor (a foreign window's anchor) and divide by it.
#:
#: Scope (D9 follow-up): the sample is the distinct as-of hits of THIS read plus, in `qfq` mode,
#: the normalisation anchor -- NOT the symbol's whole stored history. `query_adjust_factors` has
#: no `start` filter, so a foreign 1993-2023 prefix would otherwise fail every 2024+ read of a
#: symbol whose applied segment is perfectly consistent.
IMPLAUSIBLE_ADJUST_FACTOR_RATIO = 4.0
IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO = 0.25

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


class MissingAdjustFactorsError(RuntimeError):
    """Raised when raw bars are queried with qfq/hfq adjustment but no adjustment factors are found for a non-neutral symbol."""

    def __init__(self, symbol: str, msg: Optional[str] = None):
        self.symbol = symbol
        super().__init__(
            msg
            or f"Missing adjustment factors for non-neutral symbol '{symbol}'. Cannot derive adjusted prices from raw bars."
        )


class ImplausibleAdjustFactorStep(RuntimeError):
    """A read found an APPLIED `adjust_factors.factor_hfq` that cannot belong to its own series.

    D9 (docs/development_plan.md): `adjust_factors` is keyed on `(symbol, date)` only, while the
    table carries a `source` column. Two writers use two different normalisation bases for the
    same symbol (Fuyao anchors at its window start, BaoStock accumulates from the IPO), so a
    sparse BaoStock event row that lands on a date already covered by the dense Fuyao series
    *replaces* the dense row (`INSERT OR REPLACE`). The surviving value is 58.7x its neighbours,
    which is not a corporate action but a different unit, and applying it silently rescales every
    bar of that session (the incident turned a 52.47 qfq close into 3080.96 and moved `sma_50`
    from 54.21 to 114.75).

    The band is judged on the factor values the read ACTUALLY applies (as-of hits + the `qfq`
    anchor), never on the symbol's whole stored history: an old foreign-basis prefix that the
    requested window does not touch is the repair script's business, not a read failure (D9
    follow-up -- banding the full array made every 2024+ read of `600660.SH` raise). So this
    exception means "a value that reached a price is off its own series by 4x", and it is
    deliberately a hard failure rather than a warning: a wrong multiplier on the hot read path
    propagates into breadth / exposure / dividend flags with no other visible trace.

    A window that SPANS the foreign-basis prefix can raise this without any overwrite having
    happened (2026-11-13, `docs/black_list.md`-adjacent reader confusion): for the 20 live
    symbols the pre-2024 rows are BaoStock trade-multiple factors while the dense 2024+ rows are
    the 2024-anchored Fuyao series, so a single `qfq` read of e.g. 2000-2026 applies rows from
    BOTH bases and the 4x band -- correctly -- fires on the mix. The message below names that
    cause and the workaround; the exception itself, its fields and its "hard failure" severity
    are unchanged.
    """

    def __init__(
        self,
        symbol: str,
        date: Any,
        value: float,
        median: float,
        *,
        source: Optional[str] = None,
        n_offending: int = 1,
        n_factors: Optional[int] = None,
        bar_window: Optional[str] = None,
    ):
        self.symbol = symbol
        self.date = date
        self.value = float(value)
        self.median = float(median)
        self.source = source
        self.n_offending = int(n_offending)
        self.n_factors = n_factors
        #: Requested bar range that hit the offending factor, e.g. ``"2024-09-02..2026-09-30"``.
        self.bar_window = bar_window
        ratio = self.value / self.median if self.median else float("nan")
        source_label = "unknown" if source is None else repr(source)
        extra = "" if n_offending <= 1 else f", {n_offending} implausible applied value(s)"
        hit_bars = "" if bar_window is None else f"\n  hit bars: {bar_window}"
        super().__init__(
            f"Implausible adjustment factor for {symbol!r} on {date}: factor_hfq="
            f"{self.value!r} is {ratio:.2f}x the median {self.median!r} of the "
            f"{'applied' if n_factors is None else n_factors} distinct factor(s) this read "
            f"applies (allowed band {IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO}-"
            f"{IMPLAUSIBLE_ADJUST_FACTOR_RATIO}x), source={source_label}{extra}."
            f"{hit_bars}\n"
            f"  This is the signature of two normalisation bases in one symbol's "
            f"`adjust_factors` rows (the D9 cross-source overwrite), not of a corporate "
            f"action: a real split/bonus/dividend moves the factor by percent-level steps.\n"
            f"  Remedy: `python scripts/repair_cross_source_adjust_factors.py "
            f"--symbol {symbol} --dry-run` lists the foreign-source rows (dry-run is the "
            f"default; `--apply` writes a JSON backup before deleting), then re-pull the "
            f"symbol from a single source.\n"
            f"  If the requested window spans a foreign-basis PREFIX instead (for the 20 live "
            f"symbols: anything starting before 2024, where BaoStock's price-multiple scale sits "
            f"in front of the Fuyao-anchored dense rows), no overwrite happened -- the read "
            f"itself is mixing two bases. Workaround: read `raw` bars plus the factor explicitly, "
            f"or start the window at/after the dense range (2024-01-02 for those symbols) so the "
            f"applied segment has one basis."
        )


class CrossSourceAdjustFactorRefused(UserWarning):
    """An `adjust_factors` write was refused because it would mix two normalisation bases.

    Emitted (and then the batch is skipped -- never raised) by
    `DuckDBCache.store_adjust_factors`, mirroring `scripts/sync_china_5m_and_indices.py`'s
    `QfqRebaseWarning` contract: the caller is told, everything else keeps running, and nothing
    is written. The message starts with a fixed prefix so a sync log can be grepped for it.

    Two rules can produce this warning, distinguished by `rule` (and by the message text):

      * `RULE_INSIDE_INTERVAL` -- the batch has dates INSIDE a foreign source's covered interval,
        so `INSERT OR REPLACE` would overwrite those rows (the D9 incident shape);
      * `RULE_FRONTIER_APPEND` -- the batch has no date collision, but it reaches PAST the
        symbol's latest stored row while being stamped with a source that does not own that row.
        The new rows would become the latest row for the symbol, i.e. the QFQ anchor
        (`docs/version.md` §37.5 residual (c), closed 2026-11-13).
    """

    #: Greppable prefix -- every message this class carries starts with it.
    PREFIX = "[REFUSED] cross-source adjust-factor write"

    #: The batch collides with (or interleaves) a foreign source's covered interval.
    RULE_INSIDE_INTERVAL = "inside-foreign-interval"
    #: The batch would become the latest row for the symbol on a foreign basis.
    RULE_FRONTIER_APPEND = "frontier-append"

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        stored_source: Optional[str] = None,
        stored_rows: Optional[int] = None,
        incoming_source: Optional[str] = None,
        refused_dates: int = 0,
        batch_dates: int = 0,
        examples: Any = (),
        rule: str = RULE_INSIDE_INTERVAL,
    ):
        super().__init__(message)
        self.symbol = symbol
        self.stored_source = stored_source
        self.stored_rows = stored_rows
        self.incoming_source = incoming_source
        self.refused_dates = refused_dates
        self.batch_dates = batch_dates
        self.examples = tuple(examples)
        self.rule = rule


class UnknownAdjustBasisError(RuntimeError):
    """The stored bars' adjustment basis was never recorded, so no read can be served honestly.

    Legacy state (M3.6 review F1): `asset_sync_metadata.adjustflag` /
    `intraday_sync_metadata.adjustflag` were added by `_ensure_column`, which -- by design --
    only adds the column and backfills nothing. Every row that existed before that migration
    therefore carries SQL NULL, i.e. "this cache holds bytes whose price basis nobody ever
    recorded". The old read path treated NULL as `stored_basis is None -> return as-is`:

      * a cache holding **QFQ** bars answered `adjust="qfq"` by returning them unscaled -- which
        *looks* right and silently breaks the moment the request window and the factor anchor
        differ (QFQ is normalised at the LATEST factor, so a window ending earlier must still be
        rescaled by `hfq[t]/hfq[latest]`);
      * the same cache answered `adjust="raw"` by returning QFQ bytes labelled raw -- a
        percent-level price error with no warning anywhere (the 2026-10-23 incident class).

    Neither can be fixed by inference (the prices alone do not say which basis they are in), so
    the read now FAILS CLOSED and points at the only correct remedy: a human declares the basis
    once (`scripts/migrate_adjust_basis.py --set-basis ...`). `allow_legacy_basis=True` is the
    explicit, warned opt-in for a caller that has independently verified the stored basis.

    Factor-neutral symbols (indices, 511880.SH) are exempt: `_apply_factors_to_df` is the
    identity for them, so an unknown basis cannot change a single price.
    """

    def __init__(
        self,
        provider: str,
        symbol: str,
        requested_mode: str,
        interval: str = "1d",
        meta_table: str = "asset_sync_metadata",
    ):
        self.provider = provider
        self.symbol = symbol
        self.requested_mode = requested_mode
        self.interval = interval
        super().__init__(
            f"Unknown adjustment basis for {symbol!r} ({provider}, interval={interval!r}): "
            f"{meta_table}.adjustflag is NULL, so the cached bars cannot be labelled "
            f"'{requested_mode}' (and no derivation can be applied to them).\n"
            f"  Remedy (once, offline, no price rewriting): "
            f"`python scripts/migrate_adjust_basis.py --dry-run` lists every such "
            f"(provider, symbol); then declare the basis you verified with "
            f"`python scripts/migrate_adjust_basis.py --set-basis {{raw|qfq|hfq}} "
            f"--provider {provider} --symbol {symbol}`.\n"
            f"  If you have already verified out-of-band that the stored bars are exactly the "
            f"'{requested_mode}' series, re-run the read with `allow_legacy_basis=True` to get "
            f"the legacy pass-through (no derivation, no scaling)."
        )


class AdjustBasisTransitionRefused(RuntimeError):
    """A write tried to stamp a basis onto already-stored bars whose basis was never declared.

    M3.6 review F2: the metadata upsert is `COALESCE(excluded.adjustflag, existing.adjustflag)`
    in both `store_bars` and `store_intraday_bars`, so a NULL (or explicitly undeclared) existing
    basis was silently "upgraded" to whatever the next writer happened to pass -- e.g. legacy QFQ
    rows (metadata NULL) flipped to `"raw"` by the next raw sync, after which every read
    multiplied those already-QFQ prices by the factor again (double adjustment), and
    `store_bars`' own history-rewrite gate saw nothing because it compares prices, not labels.

    The label is a claim about the bytes already on disk, and only a human can make it
    truthfully. Declared rewrites are still possible (`allow_basis_transition=True`, logged as a
    WARNING), and `scripts/migrate_adjust_basis.py --set-basis` exists for the ordinary case.
    """

    def __init__(
        self,
        provider: str,
        symbol: str,
        incoming_basis: str,
        existing_basis: Optional[str],
        meta_table: str = "asset_sync_metadata",
        interval: str = "1d",
        n_rows: Optional[int] = None,
    ):
        self.provider = provider
        self.symbol = symbol
        self.incoming_basis = incoming_basis
        self.existing_basis = existing_basis
        existing_label = "NULL" if existing_basis is None else repr(existing_basis)
        super().__init__(
            f"Refusing to declare basis '{incoming_basis}' for {symbol!r} ({provider}, "
            f"interval={interval!r}): {meta_table}.adjustflag is {existing_label} while "
            f"{'an existing' if n_rows is None else n_rows} row(s) of this symbol are already "
            f"stored. Stamping a label onto bytes whose basis was never recorded is how legacy "
            f"QFQ rows get re-multiplied by the factor (double adjustment).\n"
            f"  Remedy: declare the true basis once, offline, with "
            f"`python scripts/migrate_adjust_basis.py --set-basis {{raw|qfq|hfq}} "
            f"--provider {provider} --symbol {symbol}` (it rewrites NO prices), or delete and "
            f"re-fetch the symbol. `allow_basis_transition=True` is the audited escape hatch for "
            f"a write that genuinely replaces every stored row."
        )


class AdjustBasisRequiredError(RuntimeError):
    """A write tried to store bars without saying which adjustment basis they are in.

    M3.6 review P1 (second pass): the previous revision let `store_bars(..., adjustflag=None)`
    record the `_UNDECLARED_BASIS` sentinel and then *read it back silently* -- no
    `allow_legacy_basis`, no `AdjustBasisTransitionRefused`. That moved the F1/F2 hazard onto
    freshly-written rows instead of closing it: `rebase_corporate_action` and
    `scripts/migrate_cache_csv_to_duckdb.py` produced rows where `adjust="raw"` returned
    possibly-QFQ bytes and `adjust="qfq"` returned possibly-raw bytes, with nothing said.

    The basis is a claim about the bytes being written, and only the writer can make it. So the
    write is now FAIL-CLOSED for every non-factor-neutral symbol:

      * declare it -- `adjustflag="raw"` / `"qfq"` / `"hfq"`;
      * or, if the caller genuinely cannot say (an opaque provider or a legacy CSV dump), say so
        out loud with `allow_undeclared_basis=True`. That records the sentinel, and every read of
        that symbol then requires `allow_legacy_basis=True` (or a declaration via
        `scripts/migrate_adjust_basis.py`), i.e. the same fail-closed treatment as a pre-M3.6
        NULL row.

    Factor-neutral symbols (indices, 511880) are exempt: `_apply_factors_to_df` is the identity
    for them, so their label cannot change a single returned price.
    """

    def __init__(self, provider: str, symbol: str, interval: str = "1d", store: str = "store_bars"):
        self.provider = provider
        self.symbol = symbol
        self.interval = interval
        super().__init__(
            f"{store}({provider}, {symbol!r}, interval={interval!r}) was called without "
            f"`adjustflag`: the cache cannot record bytes whose adjustment basis nobody states. "
            f"Declare it -- `adjustflag=\"raw\"` (unadjusted), `\"qfq\"` or `\"hfq\"` -- because "
            f"the label is a claim about the prices being written and a later reader cannot "
            f"recover it from the numbers alone (a corporate action and a basis shift are "
            f"indistinguishable).\n"
            f"  If the caller genuinely cannot know (opaque provider, legacy CSV dump), pass "
            f"`allow_undeclared_basis=True`: the row is then recorded as undeclared and every "
            f"read of it fails closed (`UnknownAdjustBasisError`) until a human declares the "
            f"truth with `python scripts/migrate_adjust_basis.py --set-basis ...`."
        )


def canonicalize_adjust_mode(mode: Any) -> str:
    """Canonicalize adjustment mode into one of: 'raw', 'qfq', 'hfq'.

    Accepted mappings:
      - raw: 'none', 'raw', '3', 'unadjusted'
      - qfq: 'qfq', '2', 'forward', 'fore'
      - hfq: 'hfq', '1', 'backward', 'back'
    Raises ValueError for any other input (or None).
    """
    if mode is None:
        raise ValueError("Adjustment mode cannot be None")
    m = str(mode).strip().lower()
    if m in ("none", "raw", "3", "unadjusted"):
        return "raw"
    if m in ("qfq", "2", "forward", "fore"):
        return "qfq"
    if m in ("hfq", "1", "backward", "back"):
        return "hfq"
    raise ValueError(f"Unknown or unsupported adjustment mode: {mode!r}")


def parse_stored_basis(raw: Any) -> Optional[str]:
    """Maps a stored metadata `adjustflag` value to a canonical basis, or `None`.

    Three states, deliberately kept apart (see `_UNDECLARED_BASIS`):
      * `None` (SQL NULL) -> legacy row, basis never recorded: NOT the same as "raw";
      * `'unknown'` -> a modern write that declined to declare a basis;
      * `raw` / `qfq` / `hfq` -> a declared basis.

    Both of the first two return `None` here (no derivation is possible for either), and the
    READ path refuses BOTH (P1 second pass) -- `is_legacy_basis_unknown` only picks the wording
    of the `allow_legacy_basis` warning / the `UnknownAdjustBasisError` origin, because a
    sentinel row hides its basis just as effectively as a pre-M3.6 NULL row.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text == _UNDECLARED_BASIS:
        return None
    return canonicalize_adjust_mode(text)


def is_legacy_basis_unknown(raw: Any) -> bool:
    """True when the stored metadata basis is SQL NULL (pre-M3.6 row), False otherwise.

    `'unknown'` is False on purpose: that row's writer did run the M3.6 path and said out loud
    that no basis was declared, so it is not part of the legacy-migration population.
    """
    return raw is None


def _source_at(fac_df: pd.DataFrame, position: int) -> Optional[str]:
    """`source` of the factor row at ``position`` (``None`` when the column is absent/NULL).

    Used only on the cold `ImplausibleAdjustFactorStep` path, so the defensive shape costs
    nothing on the read hot path.
    """
    if "source" not in fac_df.columns:
        return None
    try:
        value = fac_df["source"].iloc[position]
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return None
    return None if pd.isna(value) else str(value)


def _day_keys(index: Any) -> np.ndarray:
    """Day-granularity keys for the as-of factor lookup (`datetime.date` semantics).

    The pre-vectorisation implementation compared `pd.Timestamp(ts).date()` objects, i.e. it
    dropped the intraday time-of-day and any tz info. `normalize()` + `tz_localize(None)` does
    exactly that on the wall clock, so a 5m bar at 09:35 and its daily bar collide on the same
    key -- which is what makes the 5m and daily factor scaling agree (pinned by
    `test_5m_and_daily_overlap_scaling_factor_identical`).
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize().values.astype("datetime64[ns]")


def _resolve_requested_columns(columns: Any) -> Optional[List[str]]:
    """Normalises the `columns=` argument of the bar queries.

    `None` -> `None`, meaning "the required OHLCV columns only" -- `Amount` is NOT included, per
    the M3.6 rule that a newer column must be explicitly requested (a cache read must not change
    shape because a *different* symbol happened to have an amount on disk).
    `"all"` (case-insensitive) -> required OHLCV + `Amount`.
    A sequence (or a single column name) -> exactly that, validated by the caller.
    """
    if columns is None:
        return None
    if isinstance(columns, str):
        if columns.strip().lower() == _COLUMNS_ALL:
            return list(_REQUIRED_OHLCV_COLUMNS) + ["Amount"]
        return [columns]
    return [str(c) for c in columns]


def _select_output_columns(df: pd.DataFrame, requested: Optional[List[str]], *, context: str) -> pd.DataFrame:
    """Applies the `columns=` contract to an already-loaded, renamed frame.

    Raises `ValueError` for a name the table cannot serve -- silently dropping it would make a
    caller believe it asked for a column it never got.
    """
    if requested is None:
        target = list(_REQUIRED_OHLCV_COLUMNS)
    else:
        target = requested
    missing = [c for c in target if c not in df.columns]
    if missing:
        raise ValueError(
            f"{context}: requested column(s) {missing} are not available; this table can serve "
            f"{[c for c in _REQUIRED_OHLCV_COLUMNS + ['Amount'] if c in df.columns]}."
        )
    return df[list(target)].astype(float)


#: Legacy-basis pass-through warnings, keyed by (provider, symbol, interval). Warn ONCE per key:
#: a universe read would otherwise emit one warning per symbol per call and drown the log.
_LEGACY_BASIS_WARNED: set = set()


def is_factor_neutral_symbol(symbol: str) -> bool:
    """Returns True for structurally factor-neutral symbols (indices, cash ETF 511880)."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if "511880" in s:
        return True
    if s.endswith(".TI"):
        return True
    try:
        from common.financial_api import is_index_symbol

        return is_index_symbol(s)
    except Exception:
        ticker, _, suffix = s.partition(".")
        if suffix == "SZ" and ticker.startswith("399"):
            return True
        if suffix == "SH" and (ticker.startswith("000") or ticker.startswith("001")):
            return True
        return False


def _resolve_write_basis(
    provider: str,
    symbol: str,
    interval: str,
    canon_adjust: Optional[str],
    *,
    allow_undeclared_basis: bool,
    store: str,
) -> str:
    """M3.6 review P1 write-side gate: the value to persist into the metadata `adjustflag`.

    Returns the declared basis, or the `_UNDECLARED_BASIS` sentinel when the caller explicitly
    opted into `allow_undeclared_basis=True` (or the symbol is factor-neutral). Raises
    `AdjustBasisRequiredError` otherwise -- an omission is not a declaration.
    """
    if canon_adjust is not None:
        return canon_adjust
    if allow_undeclared_basis or is_factor_neutral_symbol(symbol):
        return _UNDECLARED_BASIS
    raise AdjustBasisRequiredError(provider, symbol, interval, store)


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
    if "Amount" in df.columns:
        agg_spec["Amount"] = "sum"

    resampled = df.resample(rule).agg(agg_spec).dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" in resampled.columns:
        resampled["Volume"] = resampled["Volume"].fillna(0.0)
    if "Amount" in resampled.columns:
        resampled["Amount"] = resampled["Amount"].fillna(0.0)

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


class HistoryRewriteRefused(RuntimeError):
    """store_bars refused to overwrite stored history with a materially different value.

    Raised (before anything is written) when the incoming frame collides with rows already in
    `ohlcv_bars` on `(provider, symbol, date)` AND at least one of those stored rows differs by
    more than `_HISTORY_REWRITE_RTOL`. Deliberately independent of the sync script's
    `QfqWriteRefused`: this module is shared infrastructure and must not import from `scripts/`.

    2026-10-23 incident (motivating case): `CachedDataProvider.fetch_universe`
    (`common/data.py:743-836`) saw a range as "not covered", re-fetched, and
    `common/financial_api.py:802-808` applied ONE scalar per symbol
    (`scale = rem_c / loc_c`, remote same-day close over local-MarketDB last close) to a whole
    local block before handing it to `store_bars`. `store_bars`' plain `INSERT OR REPLACE`
    (`ohlcv_bars` PK is `(provider, symbol, date)`, see `_init_tables`) then rewrote
    2020-11-30..2023-12-29 in place -- 13,303 rows, `volume` untouched -- silently reverting four
    symbols to values that had already been repaired away.
    """


class NullVolumeRefused(RuntimeError):
    """store_bars refused a row whose `Volume` CELL is null/NaN -- it never wrote `0.0` instead.

    Frozen contract: `docs/data_gap_visibility_schema.md` §4.1-§4.3 (`data_gap/v1`). The payload
    below is that section's field list, field for field; do not drop or rename one.

    Why this is not a `Constraint Error` (the pre-LV5 behaviour): `ohlcv_bars.volume` is
    `DOUBLE NOT NULL`, so a null volume used to reach `INSERT OR REPLACE`, raise DuckDB's
    constraint error inside the transaction and roll the whole batch back. That exception carries
    no symbol, no provider and no date, so the only thing a caller could do with it was print a
    string (`scripts/check_live_portfolio_health.py:928` printed one generic WARN for 22 symbols).
    This type is that error made nameable and attributable.

    Scope (frozen §4.3, "per-row refuse, never per-batch die"): only the offending ROWS are
    removed and reported; the rest of the batch is written normally, and this exception is raised
    AFTER that write commits. A caller isolating per symbol
    (`scripts/sync_china_5m_and_indices.py::patch_equity_daily_bars`) therefore continues with the
    next symbol instead of losing the remaining tail of the run.

    `value_repr` is `"nan"` when every refused cell is a float NaN and `"null"` otherwise (a
    `None`/`pd.NA` cell) -- the literal representations frozen by §4.1.
    """

    def __init__(
        self,
        *,
        symbol: str,
        provider: str,
        interval: str,
        dates,
        value_repr: str = "null",
    ):
        normalized = sorted(
            {d.date() if hasattr(d, "date") else d for d in dates}
        )
        if not normalized:
            raise ValueError("NullVolumeRefused requires at least one refused date")
        self.symbol = symbol
        self.provider = provider
        self.interval = interval
        self.dates = list(normalized)
        self.n_rows = len(self.dates)
        self.first_date = self.dates[0]
        self.last_date = self.dates[-1]
        self.value_repr = value_repr
        super().__init__(
            f"store_bars refused {self.n_rows} row(s) with a null volume "
            f"({self.value_repr}) for {provider}/{symbol} interval={interval} over "
            f"{self.first_date}..{self.last_date}"
            + ("" if self.n_rows == 1 else f" ({self.n_rows} dates: {self.dates})")
            + "; 0.0 was NOT substituted -- a null volume means 'not collected', not 'no trades'"
            " (docs/data_gap_visibility_schema.md §2.4). Those dates are NOT stored."
        )


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


#: Suffix every concrete `DataProvider` class carries (`FuyaoDataProvider`,
#: `MarketDBDataProvider`, `BaoStockDataProvider`, `EastMoneyDataProvider`).
#:
#: WHY IT MATTERS (2026-10-27 incident, real live-deployment blocker): every writer keys
#: `ohlcv_bars` by `type(self.inner_provider).__name__` (`common/data.py`), i.e. the CLASS NAME
#: `FuyaoDataProvider`; every reader on the live-station side speaks the SHORT provider name
#: (`fuyao`, from `settings.local.json` > `default_data_provider`). `query_bars` builds
#: `WHERE provider = ?` from that short name, gets zero rows, returns an EMPTY frame
#: (see `query_bars`: empty results return before `_resolve_read_basis`), and `compute_nav`
#: then reports the holding as "no price at all" -> classified `stale_price`, i.e. "行情陈旧" --
#: a completely wrong diagnosis for a cache that holds 680 clean bars for the symbol.
#: That mismatch sent an operator looking at market data instead of at a provider string.
_PROVIDER_KEY_SUFFIX = "dataprovider"


def canonicalize_provider_key(name: Any) -> str:
    """Case-folded, `DataProvider`-suffix-stripped form used to COMPARE provider keys.

    `fuyao`, `Fuyao` and `FuyaoDataProvider` all canonicalise to `fuyao`. This is a
    comparison key ONLY -- never a value to read bars with, because the cache's primary key
    is the exact stored string (`selected_provider` etc. stay untouched). Collapsing the
    suffix is deliberately narrow: it must not turn `MarketDBDataProvider` into anything
    that also matches `FuyaoDataProvider`.
    """
    key = str(name).strip().lower()
    if key.endswith(_PROVIDER_KEY_SUFFIX) and len(key) > len(_PROVIDER_KEY_SUFFIX):
        key = key[: -len(_PROVIDER_KEY_SUFFIX)]
    return key


def sync_timestamp_utc_naive(explicit: Optional[datetime.datetime] = None) -> datetime.datetime:
    """The value written to every `last_sync_time` column: **naive UTC**.

    2026-11-05 audit (development_plan `LV19` item 2): the columns used to be stamped with
    `datetime.datetime.now()`, i.e. the HOST's wall clock with the tzinfo thrown away. On the
    live station (UTC+08:00) a write at 04:23:28 UTC was stored as `20:23:28`, so an operator
    reading `20:23` placed it on the previous evening -- 16 hours from the market clock, and the
    one number an auditor uses to decide "was this bar written by THIS run". The comparison is
    now unambiguous and tz-independent.

    Invariants this function is the single owner of:
      * the returned value is ALWAYS tz-naive (a `TIMESTAMP` column has no offset; storing an
        aware object would make the driver's cast decide the offset silently);
      * it is ALWAYS UTC. Rendering market-local time (`Asia/Shanghai`) is a READ/report-side
        conversion (`.replace(tzinfo=timezone.utc).astimezone(MARKET_TZ)`), never a different
        stored value -- see `docs/lv19_freshness_contract.md` §3;
      * an explicit `explicit=` is treated as already-UTC when naive, and converted to UTC when
        aware, so the column invariant holds for every caller instead of only the default path.

    DISCLOSED CONSEQUENCE: `is_range_covered`'s legacy "already synced today" shortcut compares
    this stored *date* against the host's local date, so between 00:00 and 08:00 +08:00 it can
    read "not synced today" for a row written minutes earlier. That shortcut is bypassed whenever
    `expected_date` is supplied (the whole live path, `L2.2`), and it never decides bar freshness
    on its own -- the bar layer still has to reach the expected session.
    """
    if explicit is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if explicit.tzinfo is not None:
        return explicit.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return explicit


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

    Time convention (LV19, `docs/lv19_freshness_contract.md` §3): every `last_sync_time`
    column below holds **naive UTC** -- no offset, written by `sync_timestamp_utc_naive`.
    Rendering market-local (`Asia/Shanghai`) time is a read/report-side conversion; the stored
    number is never local time and must not be printed as if it were.

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
                amount DOUBLE,
                PRIMARY KEY (provider, symbol, date)
            );
            CREATE TABLE IF NOT EXISTS asset_sync_metadata (
                provider VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                earliest_available_date DATE NOT NULL,
                is_earliest_known BOOLEAN NOT NULL DEFAULT FALSE,
                latest_available_date DATE NOT NULL,
                -- LV19 §3: NAIVE UTC (see `sync_timestamp_utc_naive`); never local time.
                last_sync_time TIMESTAMP NOT NULL,
                adjustflag VARCHAR,
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
                amount DOUBLE,
                adjustflag VARCHAR,
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
                -- LV19 §3: NAIVE UTC (see `sync_timestamp_utc_naive`); never local time.
                last_sync_time TIMESTAMP NOT NULL,
                adjustflag VARCHAR,
                PRIMARY KEY (provider, symbol, interval)
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_lookup ON intraday_ohlcv_bars(provider, symbol, interval, ts);

            CREATE TABLE IF NOT EXISTS adjust_factors (
                symbol VARCHAR NOT NULL,
                date DATE NOT NULL,
                factor_hfq DOUBLE NOT NULL,
                source VARCHAR,
                last_sync_time TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, date)
            );
            CREATE INDEX IF NOT EXISTS idx_adjust_factors_lookup ON adjust_factors(symbol, date);

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

        # M3.6 schema migration: amount and adjustflag columns for raw storage and in-memory derivation
        self._ensure_column(con, "ohlcv_bars", "amount", "DOUBLE")
        self._ensure_column(con, "asset_sync_metadata", "adjustflag", "VARCHAR")
        self._ensure_column(con, "intraday_ohlcv_bars", "amount", "DOUBLE")
        self._ensure_column(con, "intraday_ohlcv_bars", "adjustflag", "VARCHAR")
        self._ensure_column(con, "intraday_sync_metadata", "adjustflag", "VARCHAR")

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
                "SELECT earliest_available_date, is_earliest_known, latest_available_date, last_sync_time, adjustflag "
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
            "adjustflag": row[4],
        }

    def get_intraday_metadata(
        self,
        provider: str,
        symbol: str,
        interval: str = "5m",
        include_probe: bool = False,
    ) -> Optional[dict]:
        """Returns synchronization metadata for (provider, symbol, interval), or None if not found."""
        if is_synthetic_provider(provider):
            return None

        con = self._get_connection(read_only=True)
        try:
            if include_probe:
                row = con.execute(
                    "SELECT earliest_available_ts, is_earliest_known, latest_available_ts, latest_close_ref, last_sync_time, adjustflag, probe_attempts, last_probe_result "
                    "FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
                    [provider, symbol, interval],
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT earliest_available_ts, is_earliest_known, latest_available_ts, latest_close_ref, last_sync_time, adjustflag "
                    "FROM intraday_sync_metadata WHERE provider = ? AND symbol = ? AND interval = ?",
                    [provider, symbol, interval],
                ).fetchone()
        finally:
            if not self.is_memory:
                con.close()

        if not row:
            return None

        res = {
            "earliest_available_ts": pd.Timestamp(row[0]),
            "is_earliest_known": bool(row[1]),
            "latest_available_ts": pd.Timestamp(row[2]),
            "latest_close_ref": float(row[3]) if row[3] is not None else None,
            "last_sync_time": pd.Timestamp(row[4]) if row[4] is not None else None,
            "adjustflag": row[5],
        }
        if include_probe:
            res["probe_attempts"] = int(row[6]) if row[6] is not None else None
            res["last_probe_result"] = row[7]
        return res

    def record_intraday_probe(
        self,
        provider: str,
        symbol: str,
        attempts: int,
        last_result: str,
        interval: str = "5m",
    ) -> None:
        """Records intraday probe verdict into intraday_sync_metadata.

        Updates `probe_attempts` and `last_probe_result` for an existing (provider, symbol, interval)
        row. If no metadata row exists for the given key, logs a warning and does not write or
        fabricate any timestamps. Never touches `intraday_ohlcv_bars`.
        """
        if is_synthetic_provider(provider):
            return

        con = self._get_connection(read_only=False)
        try:
            updated = con.execute(
                "UPDATE intraday_sync_metadata SET probe_attempts = ?, last_probe_result = ? "
                "WHERE provider = ? AND symbol = ? AND interval = ? "
                "RETURNING 1",
                [int(attempts), str(last_result) if last_result is not None else None, provider, symbol, interval],
            ).fetchall()
            if not updated:
                logger.warning(
                    "record_intraday_probe: no existing intraday_sync_metadata row for (%s, %s, %s); "
                    "probe verdict not persisted",
                    provider,
                    symbol,
                    interval,
                )
        finally:
            if not self.is_memory:
                con.close()

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
            Early closes are now modelled: `common/market_holidays.py`'s per-market
            early-close tables + `MarketSpec.close_time_on(day)` / `MarketSpec.early_close_on(day)`
            feed `market_clock.last_expected_bar_ts(...)` (session-date-aware: US 13:00 -> 12:55,
            HK 12:00 -> 11:55, 5m). 2026/2027 dates `PROVISIONAL`. See `L2.26`, `docs/development_plan.md`.

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
        adjustflag: Optional[str] = None,
        *,
        allow_basis_transition: bool = False,
        allow_undeclared_basis: bool = False,
    ) -> None:
        """Upserts intraday OHLCV bars into intraday_ohlcv_bars and updates intraday_sync_metadata.
        Enforces R4 QFQ Split/Dividend Drift detection and R13 End-Date expansion.

        L2.14: the metadata half is NOT a blind `INSERT OR REPLACE` any more. It is reconciled
        against `intraday_ohlcv_bars` (`_reconcile_intraday_metadata_with_bars`, the SAME core as
        the daily table), so the recorded earliest/latest boundaries are monotone and always
        reportable as bar facts: a later window cannot downgrade the earliest boundary, a
        front-truncated window cannot roll it forward, `is_earliest_known` is sticky True, and the
        request window never overrides what the bars say. See the long comment at the write site.

        M3.6 review F2: like `store_bars`, an already-stored symbol whose basis was never declared
        (metadata NULL, or the `_UNDECLARED_BASIS` sentinel) refuses to be stamped with a real
        basis (`AdjustBasisTransitionRefused`) unless the caller declares the transition with
        `allow_basis_transition=True`. An undeclared write (`adjustflag=None`) is recorded as the
        sentinel, and never launders an existing legacy NULL into it.

        M3.6 review P1: `adjustflag=None` is no longer a legal silent default -- omitting it on a
        non-neutral symbol raises `AdjustBasisRequiredError`. `allow_undeclared_basis=True` is the
        explicit opt-in, and it makes the row fail-closed on every read until declared.
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
        if "Amount" in df.columns:
            df_to_store["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
        elif "amount" in df.columns:
            df_to_store["Amount"] = pd.to_numeric(df["amount"], errors="coerce")
        else:
            df_to_store["Amount"] = None
        df_to_store = df_to_store.sort_index()

        # P1 write-side gate: an omission is not a declaration. See `_resolve_write_basis` and
        # `AdjustBasisRequiredError`. See `_UNDECLARED_BASIS`: an explicitly undeclared write is
        # recorded as the sentinel, so SQL NULL stays reserved for pre-M3.6 rows.
        #
        # Resolved HERE, before the R4 drift check below, because that check can DELETE every
        # existing bar + metadata row for this symbol. Validating the write after a destructive
        # purge would leave the symbol wiped and nothing written when the basis was simply omitted.
        canon_adjust = canonicalize_adjust_mode(adjustflag) if adjustflag is not None else None
        stored_basis = _resolve_write_basis(
            provider,
            symbol,
            interval,
            canon_adjust,
            allow_undeclared_basis=allow_undeclared_basis,
            store="store_intraday_bars",
        )

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

        # LV19 §3: NAIVE UTC, always. This was `datetime.datetime.now()` -- a bare local wall
        # clock -- and the resulting 16-hour skew made a same-run write read as "last evening".
        sync_ts = sync_timestamp_utc_naive(sync_time)

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
            "amount": df_to_store["Amount"].values if ("Amount" in df_to_store.columns and df_to_store["Amount"].notna().any()) else None,
            "adjustflag": stored_basis,
        })

        con = self._get_connection(read_only=False)
        try:
            # BASIS-TRANSITION GATE (M3.6 review F2): runs before the INSERT so the population it
            # inspects is the pre-existing one. The R4 purge above, when it fired, already deleted
            # both the bars and the row -- a genuinely empty symbol is not a transition.
            self._guard_basis_transition(
                con,
                provider,
                symbol,
                stored_basis,
                meta_table="intraday_sync_metadata",
                bars_table="intraday_ohlcv_bars",
                interval=interval,
                allow_basis_transition=allow_basis_transition,
            )
            con.register("temp_intraday_bars_to_insert", records_df)
            con.execute("""
                INSERT OR REPLACE INTO intraday_ohlcv_bars
                SELECT provider, symbol, interval, ts, open, high, low, close, volume, amount, adjustflag
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
            con.execute(f"""
                INSERT INTO intraday_sync_metadata AS existing
                    (provider, symbol, interval, earliest_available_ts, is_earliest_known,
                     latest_available_ts, latest_close_ref, last_sync_time, adjustflag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    last_sync_time = excluded.last_sync_time,
                    adjustflag = CASE
                        WHEN existing.adjustflag IS NULL
                             AND excluded.adjustflag = '{_UNDECLARED_BASIS}'
                            THEN NULL
                        ELSE COALESCE(excluded.adjustflag, existing.adjustflag)
                    END
            """, [
                provider,
                symbol,
                interval,
                reconciled.earliest,
                reconciled.is_earliest_known,
                reconciled.latest,
                latest_close_ref,
                sync_ts,
                stored_basis,
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
        adjustflag: Optional[str] = None,
        *,
        allow_history_rewrite: bool = False,
        rewrite_reason: Optional[str] = None,
        allow_basis_transition: bool = False,
        allow_undeclared_basis: bool = False,
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

        NULL-VOLUME CONTRACT (LV5, `docs/data_gap_visibility_schema.md`): `Volume` is defaulted to
        `0.0` **only when the `Volume` COLUMN IS ABSENT from `df`** -- and "absent" is decided
        case-insensitively (`Volume` / `volume` / `VOLUME`), because a case mismatch is not a
        missing axis: treating it as one stored every real volume as `0.0` with no trace.
        A present-but-null/NaN volume CELL is a different fact and is **refused** -- those rows
        are dropped from this write and
        the call raises `NullVolumeRefused` (symbol/provider/interval/every refused date/row
        count/first+last date/value repr) AFTER the batch's remaining legal rows commit. It is
        never coerced to `0.0`: `0.0` asserts "no trades that day", the source asserted nothing.
        An explicitly stored `0.0` (a real no-trade session) is accepted unchanged.

        HISTORY-REWRITE INVARIANT (L2.32): an incoming row whose `(provider, symbol, date)` is
        already stored may only replace the stored row when its OHLCV agrees with the stored
        values within `_HISTORY_REWRITE_RTOL` (rtol 1e-9, absolute floor 0.0). Any materially
        different value raises `HistoryRewriteRefused` before a single row is written, unless the
        caller declares the rewrite with `allow_history_rewrite=True` AND a non-blank
        `rewrite_reason` (validated up front: flag-without-reason raises `ValueError` before any
        work, because an unaudited rewrite is indistinguishable from the incident this gate
        exists for). The OPT-IN IS STILL THE FLAG, never the reason -- `rewrite_reason` is only
        consulted for the audit WARNING, and supplying it without the flag permits nothing.
        `ohlcv_bars`' PK is `(provider, symbol, date)` and the write below is
        `INSERT OR REPLACE`, i.e. an unconditional IN-PLACE OVERWRITE -- there was no date-collision
        guard anywhere in the cache layer.

        Why the gate is VALUE-based and not DATE-based: a pure "refuse any date collision" rule is
        unusable, because every normal incremental sync overlaps by construction (its fetch window
        starts in the past so it can see revisions at the seam). Refusing collisions outright would
        reject every legitimate daily append. What distinguishes an append from a history rewrite
        is not that a date repeats -- it is whether the repeated date's VALUES changed.

        Why 1e-9: ULP-level float noise is ~2e-16, so a full re-derivation of the *same* basis
        (which legitimately rewrites every overlapping row) must not trip the gate; a genuine QFQ
        basis shift is percent-level (the 2026-10-23 incident's rescale ratios were 0.9742-1.0127).
        1e-9 sits ~7 orders of magnitude above the noise floor and ~7 below any real re-basing.

        The motivating incident (2026-10-23): `CachedDataProvider.fetch_universe`
        (`common/data.py:743-836`) judged a range uncovered, re-fetched, and
        `common/financial_api.py:802-808` multiplied the whole local block by ONE scalar per
        symbol (`scale = rem_c / loc_c`). Because this method overwrote unconditionally, that
        scalar reached 13,303 stored rows covering 2020-11-30..2023-12-29 (`volume` untouched)
        and silently reverted four symbols to already-repaired-away damaged values. The sync
        script's insert-only planner (commit `3a9fc11`) only protected that one writer; this gate
        is the chokepoint every writer passes through.

        `store_intraday_bars` is NOT gated here: it has its own R4 overlap-drift check (a
        >1e-4 close drift triggers a declared purge + rewrite, a different policy).
        """
        # FAIL-CLOSED on an unaudited rewrite (L2.32 review item): the escape hatch exists so a
        # deliberate basis change can be RECORDED, not so a caller can flip a boolean and leave no
        # trace. The cache keeps no audit table, so the reason in the WARNING below is the only
        # durable record of who rewrote which span and why -- which is precisely what the
        # 2026-10-23 incident lacked (13,303 rows rewritten, nothing logged). Checked before the
        # intraday dispatch so the flag can never be silently accepted by a path that does not
        # implement this policy (intraday has its own R4 drift gate).
        if allow_history_rewrite and not str(rewrite_reason or "").strip():
            raise ValueError(
                "store_bars(allow_history_rewrite=True) requires a non-blank rewrite_reason: an "
                "unaudited history rewrite is indistinguishable from the 2026-10-23 incident "
                "(13,303 already-repaired rows silently overwritten). State why the declared "
                "re-derivation is legitimate, or leave the flag off and route the fix through "
                "scripts/repair_qfq_alignment.py."
            )

        canon_adjust = canonicalize_adjust_mode(adjustflag) if adjustflag is not None else None

        if is_intraday_interval(interval):
            return self.store_intraday_bars(
                provider,
                symbol,
                df,
                interval=interval,
                requested_start=requested_start,
                requested_end=requested_end,
                sync_time=sync_time,
                adjustflag=canon_adjust,
                allow_basis_transition=allow_basis_transition,
                allow_undeclared_basis=allow_undeclared_basis,
            )

        if is_synthetic_provider(provider) or df.empty:
            return

        # P1 write-side gate: an omission is not a declaration. See `_resolve_write_basis` and
        # `AdjustBasisRequiredError`. When explicitly opted into, the undeclared write records the
        # `_UNDECLARED_BASIS` sentinel rather than SQL NULL: NULL on this column is RESERVED for
        # pre-M3.6 rows, and laundering a modern write into it would erase that migration signal.
        stored_basis = _resolve_write_basis(
            provider,
            symbol,
            interval,
            canon_adjust,
            allow_undeclared_basis=allow_undeclared_basis,
            store="store_bars",
        )

        # Ensure required price columns. `Volume` falls back to 0.0 ONLY when the COLUMN IS
        # ABSENT from the input frame -- that is a declared convention ("this caller's frame has
        # no volume axis"), not a repair of a bad cell. A present-but-null cell is a different
        # fact ("the source returned a row without the value") and is REFUSED by name below,
        # never coerced to 0.0: once stored, `0.0` and "really zero" are byte-identical
        # (`docs/data_gap_visibility_schema.md` §2.4).
        for col in ["Open", "High", "Low", "Close"]:
            if col not in df.columns:
                raise ValueError(f"DataFrame missing required OHLCV column '{col}'")

        df_to_store = pd.DataFrame(index=pd.to_datetime(df.index))
        for col in ["Open", "High", "Low", "Close"]:
            df_to_store[col] = df[col]
        # CASE-INSENSITIVE VOLUME LOOKUP (LV5 phase 2 fix). The pre-fix code tested the literal
        # `"Volume"` only, so a frame carrying lowercase `volume` looked like "no volume column"
        # and fell into this branch's `else 0.0`: every real volume in it was stored as `0.0`. A
        # case-mismatch silently converting real volume to 0.0 falsifies dollar-volume filters
        # and breadth with no trace at all -- the store looks fully populated -- which is the
        # same defect class as `fillna(0.0)` on a null cell
        # (`docs/data_gap_visibility_schema.md` §2.4), only harder to notice.
        # `find_volume_column` is shared with `common/data_coverage.py` so the reader and the
        # writer agree on what "the volume column" is; a frame that genuinely has no volume axis
        # still gets the documented `0.0` convention.
        volume_col = find_volume_column(df.columns)
        df_to_store["Volume"] = df[volume_col] if volume_col is not None else 0.0
        if "Amount" in df.columns:
            df_to_store["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
        elif "amount" in df.columns:
            df_to_store["Amount"] = pd.to_numeric(df["amount"], errors="coerce")
        else:
            df_to_store["Amount"] = None

        # ------------------------------------------------------------------------------
        # NULL-VOLUME GUARD (LV5; frozen contract `docs/data_gap_visibility_schema.md`
        # §4.1-§4.3).
        #
        # BEFORE `frame_min_date`/`frame_max_date` and before the `asset_sync_metadata` upsert on
        # purpose: those are the "this date is covered" bookkeeping, and a refused day must never
        # be recorded there. The history-rewrite gate further down compares only dates present in
        # `temp_bars_to_insert`, so the refusal cannot leak into it either.
        #
        # `pd.isna` at the PANDAS boundary, not a SQL `IS NULL`: `register` maps pandas NaN to SQL
        # NULL, so a SQL-side check could not tell "NaN" from "NULL" and the contract would be
        # decided by the storage layer instead of stated once, here.
        #
        # SCOPE = PER ROW (frozen §4.3). The offending rows are removed and reported; the rest of
        # the batch is written normally. A whole-batch refusal would make "one bad row" and "the
        # whole batch is bad" indistinguishable to the caller, which is exactly the collapse this
        # contract exists to prevent. The refusal is raised AFTER the legal rows commit.
        # ------------------------------------------------------------------------------
        null_volume_refusal: Optional[NullVolumeRefused] = None
        if volume_col is not None:
            null_volume_mask = pd.isna(df_to_store["Volume"]).to_numpy(dtype=bool)
            if null_volume_mask.any():
                refused_index = df_to_store.index[null_volume_mask]
                refused_values = df_to_store["Volume"].to_numpy(dtype=object)[null_volume_mask]
                # §4.1: the literal repr of the offending cells -- "nan" only when EVERY one of
                # them is a float NaN, "null" otherwise (None / pd.NA / NaT-like).
                all_float_nan = all(
                    isinstance(v, float) and v != v for v in refused_values
                )
                null_volume_refusal = NullVolumeRefused(
                    symbol=symbol,
                    provider=provider,
                    interval=interval,
                    dates=[ts.date() for ts in refused_index],
                    value_repr="nan" if all_float_nan else "null",
                )
                df_to_store = df_to_store.loc[~null_volume_mask]
                if df_to_store.empty:
                    # Frozen §4.3: an empty post-removal batch must not INSERT, and there is no
                    # metadata to reconcile -- so nothing at all is written or recorded.
                    raise null_volume_refusal

        frame_min_date = df_to_store.index.min().date()
        frame_max_date = df_to_store.index.max().date()

        # LV19 §3: NAIVE UTC, always. This was `datetime.datetime.now()` -- a bare local wall
        # clock -- and the resulting 16-hour skew made a same-run write read as "last evening".
        sync_ts = sync_timestamp_utc_naive(sync_time)

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
            "amount": df_to_store["Amount"].values if ("Amount" in df_to_store.columns and df_to_store["Amount"].notna().any()) else None,
        })

        con = self._get_connection(read_only=False)
        # Never ROLLBACK a transaction this call did not open: on the shared in-memory
        # connection a caller could theoretically own one, and a failed `BEGIN` (nested
        # transaction) must not destroy their work on the way out.
        started_txn = False
        try:
            con.register("temp_bars_to_insert", records_df)
            # ------------------------------------------------------------------------------
            # ATOMICITY (L2.32 review item 2): the gate's SELECTs, the INSERT OR REPLACE and the
            # `asset_sync_metadata` upsert run inside ONE explicit transaction (COMMIT at the end
            # of this block, ROLLBACK in the handler below). DuckDB autocommits per statement, so
            # before this the gate and the write were separate implicit transactions and a second
            # writer could land rows in the gap -- the docstring's "same transaction" claim was an
            # overstatement (the window was assessed benign because the check is value-level and
            # self-consistent, but a caveat in a docstring is weaker than a transaction). It also
            # makes partial failure impossible: previously a metadata-upsert error left the bars
            # written and the metadata describing the old span.
            # `register` stays OUTSIDE the transaction: it only publishes this call's frame as a
            # view on this connection, holds no lock and writes nothing.
            # ------------------------------------------------------------------------------
            con.execute("BEGIN TRANSACTION")
            started_txn = True
            # ------------------------------------------------------------------------------
            # BASIS-TRANSITION GATE (M3.6 review F2) -- before the bar INSERT, so the row count it
            # reads is the PRE-EXISTING one.
            #
            # `adjustflag = COALESCE(excluded.adjustflag, existing.adjustflag)` used to let a writer
            # stamp a label onto already-stored rows whose basis was never recorded: legacy QFQ
            # rows (metadata NULL) became `"raw"` after one raw sync, and every later read then
            # multiplied already-QFQ prices by the factor again. The history-rewrite gate below
            # cannot see this -- it compares prices only, and the *next* write of the same window
            # is price-identical. So the label claim gets its own gate.
            # ------------------------------------------------------------------------------
            self._guard_basis_transition(
                con,
                provider,
                symbol,
                stored_basis,
                meta_table="asset_sync_metadata",
                bars_table="ohlcv_bars",
                allow_basis_transition=allow_basis_transition,
            )
            # ------------------------------------------------------------------------------
            # HISTORY-REWRITE GATE (L2.32) -- runs before the INSERT OR REPLACE below, on the
            # same connection, in the same transaction, with `temp_bars_to_insert` already
            # populated so the comparison is a JOIN rather than a pandas round-trip (ohlcv_bars
            # holds ~568k rows and this is the hot daily path).
            #
            # Why here: `INSERT OR REPLACE` with PK `(provider, symbol, date)` is an
            # unconditional in-place overwrite. On 2026-10-23 a per-symbol rescale factor from
            # `common/financial_api.py:802-808` (`scale = rem_c / loc_c`) reached this statement
            # through `CachedDataProvider.fetch_ohlcv` and rewrote 13,303 stored rows
            # (2020-11-30..2023-12-29) with `volume` untouched, reverting four symbols to
            # already-repaired-away values. The sync script's insert-only planner (commit
            # `3a9fc11`) only covered that one caller; this is the chokepoint every writer hits.
            #
            # Why VALUE-based, not DATE-based: incremental syncs overlap by construction, so
            # "any collision is a rewrite" would refuse every legitimate daily append. A repeated
            # date whose values did not change is an idempotent re-derivation.
            #
            # Why only-if-collisions: the pure append path (new tail dates, fresh symbol) must
            # stay a single cheap COUNT; the two heavier queries below only run once a collision
            # exists, and all three are pinned to this (provider, symbol) + the frame's date span
            # so they ride `idx_ohlcv_lookup` instead of scanning the whole table.
            # ------------------------------------------------------------------------------
            n_collided = con.execute(
                """
                SELECT count(*)
                FROM ohlcv_bars o
                JOIN temp_bars_to_insert t
                  ON o.provider = t.provider AND o.symbol = t.symbol AND o.date = t.date
                WHERE o.provider = ? AND o.symbol = ? AND o.date BETWEEN ? AND ?
                """,
                [provider, symbol, frame_min_date, frame_max_date],
            ).fetchone()[0]
            if n_collided:
                # Null-safe, tolerance-aware per-column difference. `o.<col>` is stored,
                # `t.<col>` incoming: both NULL == equal, exactly one NULL == a difference (a
                # vanished/added value is material), otherwise use rtol with a 0.0 absolute
                # floor. The predicate is written as NOT(x <= tol) rather than x > tol so the
                # one-NULL case cannot slip through by yielding SQL NULL instead of TRUE.
                #
                # NaN: `ohlcv_bars` declares all five value columns NOT NULL, and a pandas NaN
                # arrives at DuckDB as SQL NULL through `register` (verified, L2.32 review item
                # 3.2) -- so an incoming NaN at a colliding date takes the one-NULL branch and is
                # refused as material. A native DuckDB NaN cannot reach either side of this
                # comparison (stored values went through this same coercion), which is why the
                # predicate does not special-case `isnan()`: DuckDB's total order makes
                # `NaN <= NaN` TRUE, so a hypothetical NaN would compare EQUAL, not different.
                diff_columns = ["open", "high", "low", "close", "volume"]
                diff_predicate = " OR ".join(
                    f"""(
                        CASE
                            WHEN o.{col} IS NULL AND t.{col} IS NULL THEN FALSE
                            WHEN o.{col} IS NULL OR t.{col} IS NULL THEN TRUE
                            ELSE NOT (
                                abs(o.{col} - t.{col})
                                <= {_HISTORY_REWRITE_RTOL!r}
                                   * GREATEST(abs(o.{col}), abs(t.{col}))
                            )
                        END
                    )"""
                    for col in diff_columns
                )
                join_clause = (
                    "FROM ohlcv_bars o JOIN temp_bars_to_insert t "
                    "ON o.provider = t.provider AND o.symbol = t.symbol AND o.date = t.date "
                    "WHERE o.provider = ? AND o.symbol = ? AND o.date BETWEEN ? AND ?"
                )
                gate_params = [provider, symbol, frame_min_date, frame_max_date]
                n_diff, diff_min, diff_max = con.execute(
                    f"""
                    SELECT count(*), MIN(o.date), MAX(o.date)
                    {join_clause} AND ({diff_predicate})
                    """,
                    gate_params,
                ).fetchone()

                if n_diff:
                    # Worst deviation, as its own row: unpivot the 5 columns over only the
                    # differing rows, then take the argmax. Gives the message the date/column
                    # that a human needs to confirm "this is a basis shift, not noise".
                    per_column = " UNION ALL ".join(
                        f"""
                        SELECT o.date AS d, '{col}' AS col,
                               CASE
                                   WHEN o.{col} IS NULL OR t.{col} IS NULL
                                       THEN CAST('Infinity' AS DOUBLE)
                                   ELSE abs(o.{col} - t.{col})
                                        / NULLIF(
                                              GREATEST(abs(o.{col}), abs(t.{col})), 0
                                          )
                               END AS dev
                        {join_clause}
                          AND CASE
                                WHEN o.{col} IS NULL AND t.{col} IS NULL THEN FALSE
                                WHEN o.{col} IS NULL OR t.{col} IS NULL THEN TRUE
                                ELSE NOT (
                                    abs(o.{col} - t.{col})
                                    <= {_HISTORY_REWRITE_RTOL!r}
                                       * GREATEST(abs(o.{col}), abs(t.{col}))
                                )
                              END
                        """
                        for col in diff_columns
                    )
                    worst_date, worst_col, worst_dev = con.execute(
                        f"SELECT d, col, dev FROM ({per_column}) "
                        "ORDER BY dev DESC NULLS LAST, d, col LIMIT 1",
                        gate_params * len(diff_columns),
                    ).fetchone()

                    if not allow_history_rewrite:
                        raise HistoryRewriteRefused(
                            f"store_bars refused to rewrite {n_diff} stored row(s) for "
                            f"{provider}/{symbol} over {diff_min}..{diff_max} "
                            f"(worst rel dev {float(worst_dev):.1e} on {worst_col} at "
                            f"{worst_date}); this is a QFQ basis change, not an append. "
                            f"Route it through scripts/sync_china_5m_and_indices.py's "
                            f"insert-only planner or scripts/repair_qfq_alignment.py, or pass "
                            f"allow_history_rewrite=True with rewrite_reason=... for a declared, "
                            f"audited re-derivation."
                        )

                    # Declared rewrite: allowed, but never silent -- the cache keeps no audit
                    # table of its own, so the logger is the only durable trace of who rewrote
                    # which span and why (mirrors the L2.18 self-heal WARNING convention above).
                    # `rewrite_reason` is non-blank here by construction: the up-front guard
                    # rejects the flag without a reason, so the old "<not supplied>" fallback was
                    # removed rather than left as an unreachable branch.
                    logger.warning(
                        "store_bars HISTORY REWRITE allowed for (%s, %s): %s stored row(s) "
                        "overwritten, %s..%s, worst rel dev %.1e on %s at %s. reason=%s",
                        provider,
                        symbol,
                        n_diff,
                        diff_min,
                        diff_max,
                        float(worst_dev),
                        worst_col,
                        worst_date,
                        rewrite_reason,
                    )

            con.execute("""
                INSERT OR REPLACE INTO ohlcv_bars
                SELECT provider, symbol, date, open, high, low, close, volume, amount
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

            con.execute(f"""
                INSERT INTO asset_sync_metadata AS existing
                    (provider, symbol, earliest_available_date, is_earliest_known,
                     latest_available_date, last_sync_time, adjustflag)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    last_sync_time = excluded.last_sync_time,
                    adjustflag = CASE
                        WHEN existing.adjustflag IS NULL
                             AND excluded.adjustflag = '{_UNDECLARED_BASIS}'
                            THEN NULL
                        ELSE COALESCE(excluded.adjustflag, existing.adjustflag)
                    END
            """, [
                provider,
                symbol,
                reconciled.earliest,
                reconciled.is_earliest_known,
                reconciled.latest,
                sync_ts,
                stored_basis,
                bars_min_date,
                bars_max_date,
            ])
            con.execute("COMMIT")
        except BaseException:
            # Atomicity (L2.32 review item 2): the gate's SELECTs, the INSERT OR REPLACE and the
            # metadata upsert are one transaction, so a failure anywhere (a refused rewrite, a
            # NOT NULL violation on a NaN bar, a metadata error) leaves the cache EXACTLY as it
            # was -- never bars written with stale metadata. `ROLLBACK` is best-effort and only
            # fires for OUR transaction: if `BEGIN` itself failed (nested transaction) or DuckDB
            # already aborted it, the original exception is the one worth propagating and the
            # caller's state must be left alone. BaseException so Ctrl-C is also atomic.
            if started_txn:
                try:
                    con.execute("ROLLBACK")
                except Exception as rollback_exc:  # pragma: no cover - already-aborted txn
                    logger.debug("store_bars ROLLBACK after failure was a no-op: %s", rollback_exc)
            raise
        finally:
            if not self.is_memory:
                con.close()

        # Frozen §4.3 scope: the legal rows of this batch were just COMMITTED, so the refusal is
        # raised AFTER the transaction rather than inside it. Raising earlier would roll the good
        # rows back and turn "one null-volume row" into "nothing stored for this symbol", which is
        # the batch-wide collapse §4.3 forbids. The payload names every refused date so the
        # caller can attribute the loss to exactly those sessions.
        if null_volume_refusal is not None:
            raise null_volume_refusal

    def store_adjust_factors(
        self,
        symbol: str,
        df: pd.DataFrame,
        source: str = "baostock",
        sync_time: Optional[datetime.datetime] = None,
        allow_cross_source: bool = False,
    ) -> None:
        """Stores or updates daily backward adjustment factors in adjust_factors table.

        ONE SOURCE PER SYMBOL (D9, `docs/development_plan.md`). The table is keyed on
        `(symbol, date)` while it also stores `source`, so `INSERT OR REPLACE` lets a writer whose
        factors are normalised on a DIFFERENT basis silently destroy the other source's rows.
        That is the D9 incident: `scripts/sync_china_5m_and_indices.py` writes BaoStock's sparse
        event rows (cumulative from the IPO: `600660.SH` reaches 81.588562 by 2026-09-24) into a
        symbol whose dense daily rows come from Fuyao (2024-anchored: 1.2452-1.3895). The
        surviving row is 58.7x its neighbours, and applying it rescaled that session's qfq close
        from 52.47 to 3080.96.

        So a batch whose `source` differs from a source already holding rows *inside the same
        date interval* is refused as a whole: nothing is written, a
        `CrossSourceAdjustFactorRefused` warning names the clash, and the call returns
        normally -- a sync must be able to finish (the alternative, raising, would abort the
        remaining symbols of a scheduled run).

        The overlap rule alone was NOT enough (§37.5 residual (c), closed 2026-11-13): a batch
        that continues a series *outside* a foreign interval overwrites no row, but it can become
        the LATEST row for the symbol -- and `get_latest_factor_hfq` picks the latest row by date
        ACROSS sources with no source filter, so a foreign-basis append silently becomes the QFQ
        anchor and rescales every qfq price by the ratio of the two bases. If that ratio happens
        to sit inside the read-side `[0.25, 4.0]x` band, nothing else fires at all. Hence the
        second rule (`RULE_FRONTIER_APPEND`): a batch that reaches PAST the symbol's latest
        stored row is refused unless its own source is the one that owns that row.

        What is still allowed, deliberately:

        * an append past a foreign SPAN from the source that owns the current latest row -- the
          everyday Fuyao incremental sync on a legacy-mixed symbol (BaoStock prefix, Fuyao dense
          series). Refusing that would freeze factor updates on every legacy-mixed symbol;
        * a backfill BEHIND the frontier (dates earlier than the latest stored row, outside every
          foreign interval) -- the pre-dense-span prefix shape;
        * an empty symbol accepts any source;
        * same-source writes keep `INSERT OR REPLACE` semantics.

        `allow_cross_source=True` is the audited escape hatch for a tool that has verified both
        series share a basis (or that the overwrite is intended) -- and it is also what a
        deliberate SOURCE SWITCH past the frontier has to pass, because the two shapes are
        indistinguishable from dates and values alone.

        The schema is deliberately NOT changed (no `source` in the primary key, no migration):
        the read path already anchors QFQ on exactly one value
        (`get_latest_factor_hfq` over the whole table), and `_apply_factors_to_df` keeps a
        plausibility band as the second line of defence for rows written before this gate.
        """
        if df is None or df.empty:
            return

        fac_df = df.copy()
        if "date" in fac_df.columns:
            dates = pd.to_datetime(fac_df["date"]).dt.date
        elif "Date" in fac_df.columns:
            dates = pd.to_datetime(fac_df["Date"]).dt.date
        else:
            dates = pd.to_datetime(fac_df.index).date

        # Resolve factor_hfq (backward factor)
        hfq_col = None
        for cand in [
            "factor_hfq", "backward_factor", "back_adjust_factor",
            "backAdjustFactor", "adjust_factor", "adjustFactor", "hfq"
        ]:
            if cand in fac_df.columns:
                hfq_col = cand
                break
        if hfq_col:
            hfq_vals = pd.to_numeric(fac_df[hfq_col], errors="coerce").fillna(1.0).values
        else:
            qfq_col = None
            for cand in ["factor_qfq", "forward_factor", "fore_adjust_factor", "foreAdjustFactor", "qfq", "factor"]:
                if cand in fac_df.columns:
                    qfq_col = cand
                    break
            if qfq_col:
                qfq_raw = pd.to_numeric(fac_df[qfq_col], errors="coerce").fillna(1.0).values
                first_val = qfq_raw[0] if len(qfq_raw) > 0 and qfq_raw[0] != 0 else 1.0
                hfq_vals = qfq_raw / first_val
            else:
                hfq_vals = np.ones(len(fac_df))

        now_ts = sync_time if sync_time is not None else datetime.datetime.now()

        records = pd.DataFrame({
            "symbol": symbol,
            "date": dates,
            "factor_hfq": hfq_vals.astype(float),
            "source": source,
            "last_sync_time": now_ts,
        })
        records = records.drop_duplicates(subset=["date"], keep="last")

        con = self._get_connection(read_only=False)
        try:
            refusal = (
                None
                if allow_cross_source
                else self._cross_source_conflict(con, symbol, source, records)
            )
            if refusal is not None:
                warnings.warn(refusal, CrossSourceAdjustFactorRefused, stacklevel=3)
                return
            con.register("temp_factors_to_insert", records)
            con.execute("""
                INSERT OR REPLACE INTO adjust_factors (symbol, date, factor_hfq, source, last_sync_time)
                SELECT symbol, date, factor_hfq, source, last_sync_time
                FROM temp_factors_to_insert
            """)
        finally:
            if not self.is_memory:
                con.close()

    def _cross_source_conflict(
        self,
        con,
        symbol: str,
        source: str,
        records: pd.DataFrame,
    ) -> Optional["CrossSourceAdjustFactorRefused"]:
        """Build the D9 refusal warning, or `None` when this batch may be written.

        A conflict exists when the symbol already holds rows from a DIFFERENT `source` and either

        1. at least one date of the incoming batch falls inside that source's covered interval
           `[min(date), max(date)]` (`RULE_INSIDE_INTERVAL`); or
        2. the batch reaches PAST the symbol's latest stored row while being stamped with a
           source that does not own that row (`RULE_FRONTIER_APPEND`).

        (1) is load-bearing for the key: the primary key is `(symbol, date)`, so only an
        overlapping batch can destroy a row, and only an interleaved one can put two
        normalisation bases into a single as-of sequence.

        (2) closes §37.5 residual (c). The overlap rule alone left a hole: a batch dated AFTER
        every stored row overwrites nothing, yet `get_latest_factor_hfq` picks the latest row by
        DATE across sources, so the appended foreign-basis row becomes the qfq divisor and
        rescales the entire qfq series by the ratio of the two bases -- silently, if that ratio
        lands inside the read-side `[0.25, 4.0]x` band. The fix refuses exactly the appends that
        would take the anchor away from the source that currently owns it, which keeps the
        legitimate shapes alive:

        * incoming source == the source owning the latest stored row -> allowed (the everyday
          Fuyao incremental sync on a legacy-mixed symbol);
        * `max(incoming) <= max(stored)` -> allowed (a backfill behind the frontier, e.g. the
          pre-dense-span prefix);
        * empty symbol, or only same-source rows -> no foreign source, so nothing is refused.

        SOURCE STRINGS ARE COMPARED CANONICALISED (2026-11-13 review fix): writers stamp either
        the CLASS NAME (`BaoStockDataProvider`, what `common/data.py` uses) or the SHORT alias
        (`baostock`, which is this function's own default). A raw `!=` therefore declared a
        provider to be its own foreign source: a caller relying on the default `source="baostock"`
        had its legitimate update refused as `[REFUSED] cross-source` against rows it had itself
        written as `BaoStockDataProvider`. `canonicalize_provider_key` (the single definition of
        "same provider, different spelling" in this module) folds the `DataProvider` suffix and
        case, so `baostock` == `BaoStockDataProvider` and `fuyao` == `FuyaoDataProvider`. It stays
        narrow on purpose: `FuyaoDataProvider` and `BaoStockDataProvider` must remain different,
        because that pair is the whole reason this guard exists.

        `source IS NULL` rows are ignored by BOTH rules: a NULL records no basis claim at all
        (every writer in this repo passes a real source), so it cannot prove a clash -- and it
        must not make the anchor look ownerless either, which would refuse every append to a
        symbol carrying legacy NULL rows.
        """
        existing = con.execute(
            "SELECT source, count(*), min(date), max(date) FROM adjust_factors "
            "WHERE symbol = ? AND source IS NOT NULL GROUP BY source",
            [symbol],
        ).fetchall()
        incoming_key = canonicalize_provider_key(source)
        foreign = [r for r in existing if canonicalize_provider_key(r[0]) != incoming_key]
        if not foreign:
            return None

        incoming_dates = pd.to_datetime(pd.Series(records["date"])).dt.date
        incoming = pd.Series(sorted(set(incoming_dates)))

        hits: List[Tuple[str, int, Dict[Any, float], Any, Tuple[Any, Any]]] = []
        for f_source, f_count, f_min, f_max in foreign:
            inside = incoming[(incoming >= f_min) & (incoming <= f_max)]
            if inside.empty:
                continue
            stored = con.execute(
                "SELECT date, factor_hfq FROM adjust_factors "
                f"WHERE symbol = ? AND source = ? AND date BETWEEN ? AND ?",
                [symbol, f_source, f_min, f_max],
            ).fetchall()
            stored_map = {
                (d if isinstance(d, datetime.date) else d.date()): float(v)
                for d, v in stored
            }
            hits.append((f_source, int(f_count), stored_map, inside, (f_min, f_max)))
        if not hits:
            return self._cross_source_frontier_refusal(
                symbol, source, incoming_key, existing, incoming, records
            )

        f_source, f_count, stored_map, inside, (f_min, f_max) = max(
            hits, key=lambda h: len(h[3])
        )
        clash_dates = list(inside.tolist())
        incoming_by_day = {
            (d if isinstance(d, datetime.date) else pd.Timestamp(d).date()): float(v)
            for d, v in zip(pd.to_datetime(records["date"]).dt.date, records["factor_hfq"])
        }
        # Examples prefer real collisions (a stored row at that very date) -- those are the rows
        # the D9 bug would have overwritten, so the operator sees both sides of the clash.
        collisions = [d for d in clash_dates if d in stored_map]
        examples = []
        for d in (collisions or clash_dates)[:2]:
            examples.append(
                {
                    "date": d.isoformat(),
                    "stored": stored_map.get(d),
                    "incoming": incoming_by_day[d],
                    "collision": d in stored_map,
                }
            )

        lines = [
            f"{CrossSourceAdjustFactorRefused.PREFIX} for {symbol!r}: "
            f"{f_count} row(s) from {f_source!r} are already stored "
            f"({f_min.isoformat()}..{f_max.isoformat()}), and this batch is stamped "
            f"{source!r}. {len(clash_dates)} of {len(incoming)} date(s) in the batch fall "
            f"inside that interval and would be written on a different normalisation basis "
            f"(the key is (symbol, date), so an existing row would be REPLACED). "
            f"Nothing was written.",            "  examples: "
            + "; ".join(
                f"{e['date']} "
                + (
                    f"stored={e['stored']!r} ({f_source}) -> incoming={e['incoming']!r}"
                    if e["stored"] is not None
                    else f"incoming={e['incoming']!r} (no stored row; interleaves the "
                    f"{f_source} series)"
                )
                for e in examples
            ),
            "  Remedy: one source per symbol. List the foreign rows with "
            "`python scripts/repair_cross_source_adjust_factors.py --symbol "
            f"{symbol} --dry-run` (dry-run is the default; `--apply` writes a JSON backup "
            "first), then re-pull the symbol from a single source. Pass "
            "`allow_cross_source=True` only for a tool that has verified both series share "
            "a basis.",
        ]
        return CrossSourceAdjustFactorRefused(
            "\n".join(lines),
            symbol=symbol,
            stored_source=f_source,
            stored_rows=f_count,
            incoming_source=source,
            refused_dates=len(clash_dates),
            batch_dates=len(incoming),
            examples=examples,
            rule=CrossSourceAdjustFactorRefused.RULE_INSIDE_INTERVAL,
        )

    def _cross_source_frontier_refusal(
        self,
        symbol: str,
        source: str,
        incoming_key: str,
        existing: List[Tuple[str, int, Any, Any]],
        incoming: "pd.Series",
        records: pd.DataFrame,
    ) -> Optional["CrossSourceAdjustFactorRefused"]:
        """Second D9 rule: refuse an append that would take the QFQ anchor away from its owner.

        `existing` is the per-source census the caller already read
        (`source, count, min(date), max(date)`, NULL sources excluded). The frontier is the
        LATEST stored row for the symbol, across sources -- the row `get_latest_factor_hfq`
        would return, because that query has no source filter.

        Refuse when the batch reaches past that frontier AND its own (canonicalised) source is
        not among the sources owning the frontier row. Two consequences, both intended:

        * a legitimate Fuyao incremental sync on a legacy-mixed symbol (BaoStock prefix, Fuyao
          dense series) stays allowed, because Fuyao owns the latest row;
        * a foreign batch whose LATEST date is still behind the frontier stays allowed -- that is
          a backfill of the pre-dense-span prefix, not an anchor takeover.

        Returns `None` when the frontier is not crossed, which is the common case and must stay
        cheap: a symbol with a foreign prefix and its own rows extending to the newest session
        never even builds a warning.
        """
        if not existing:
            return None
        frontier = max(r[3] for r in existing)
        # Any source tied AT the frontier owns the anchor, so a batch from it is a continuation.
        if any(
            canonicalize_provider_key(r[0]) == incoming_key and r[3] == frontier
            for r in existing
        ):
            return None  # the batch continues the series that owns the anchor
        # Messaging: the frontier owner with the most rows, so the warning names the source that
        # actually holds the bulk of the series rather than an arbitrary tied one.
        anchor_source, anchor_rows, anchor_min, _ = max(
            (r for r in existing if r[3] == frontier), key=lambda r: int(r[1])
        )

        incoming_max = max(incoming.tolist())
        if incoming_max <= frontier:
            return None  # backfill behind the frontier: no row becomes the latest one

        past = [d for d in incoming.tolist() if d > frontier]
        incoming_by_day = {
            (d if isinstance(d, datetime.date) else pd.Timestamp(d).date()): float(v)
            for d, v in zip(pd.to_datetime(records["date"]).dt.date, records["factor_hfq"])
        }
        examples = [
            {
                "date": d.isoformat(),
                "stored": None,
                "incoming": incoming_by_day[d],
                "collision": False,
            }
            for d in past[:2]
        ]

        lines = [
            f"{CrossSourceAdjustFactorRefused.PREFIX} for {symbol!r}: the FRONTIER rule refused "
            f"this batch. The latest stored row for this symbol is {frontier.isoformat()} and "
            f"belongs to {anchor_source!r} ({anchor_rows} row(s), "
            f"{anchor_min.isoformat()}..{frontier.isoformat()}); this batch is stamped "
            f"{source!r} and reaches {incoming_max.isoformat()}, so {len(past)} of "
            f"{len(incoming)} date(s) would be PAST that frontier and would become the LATEST row "
            f"for the symbol. `get_latest_factor_hfq` selects the latest row by date ACROSS "
            f"sources with no source filter, so those rows would become the QFQ anchor and "
            f"rescale every stored qfq price by the ratio of the two normalisation bases "
            f"(nothing else catches it when that ratio happens to sit inside the read-side "
            f"plausibility band). Nothing was written.",
            "  examples: "
            + "; ".join(
                f"{e['date']} incoming={e['incoming']!r} (past the {anchor_source} frontier "
                f"{frontier.isoformat()}; no stored row, but it would take over the anchor)"
                for e in examples
            ),
            "  Remedy: one source per symbol -- a batch may extend a symbol only when its own "
            "source owns the latest stored row. List the foreign rows with "
            "`python scripts/repair_cross_source_adjust_factors.py --symbol "
            f"{symbol} --dry-run` (dry-run is the default; `--apply` writes a JSON backup "
            "first), then re-pull the symbol from a single source. A deliberate SOURCE SWITCH "
            "past the frontier has to pass `allow_cross_source=True` after verifying both series "
            "share a basis: by dates and values alone it is indistinguishable from this refusal.",
        ]
        return CrossSourceAdjustFactorRefused(
            "\n".join(lines),
            symbol=symbol,
            stored_source=anchor_source,
            stored_rows=int(anchor_rows),
            incoming_source=source,
            refused_dates=len(past),
            batch_dates=len(incoming),
            examples=examples,
            rule=CrossSourceAdjustFactorRefused.RULE_FRONTIER_APPEND,
        )

    def get_latest_factor_hfq(self, symbol: str) -> Optional[float]:
        """Returns the latest factor_hfq across the entire adjust_factors table for symbol."""
        con = self._get_connection(read_only=True)
        try:
            row = con.execute(
                "SELECT factor_hfq FROM adjust_factors WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                [symbol],
            ).fetchone()
        finally:
            if not self.is_memory:
                con.close()
        return float(row[0]) if row and row[0] is not None else None

    def query_adjust_factors(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Retrieves adjust factors for symbol up to end (not filtered by start)."""
        clauses = ["symbol = ?"]
        params: List[Any] = [symbol]

        if end:
            clauses.append("date <= ?")
            params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))

        sql = (
            f"SELECT date, factor_hfq, source, last_sync_time "
            f"FROM adjust_factors WHERE {' AND '.join(clauses)} ORDER BY date"
        )
        con = self._get_connection(read_only=True)
        try:
            res_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        if res_df.empty:
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        res_df["date"] = pd.to_datetime(res_df["date"])
        res_df = res_df.set_index("date").sort_index()
        res_df.index.name = "date"
        return res_df

    def _assert_plausible_adjust_factors(
        self,
        symbol: str,
        fac_df: pd.DataFrame,
        event_hfq: np.ndarray,
        pos: np.ndarray,
        divisor: float,
        mode: str,
        bar_index: pd.Index,
    ) -> None:
        """Second line of defence for D9: refuse an APPLIED factor that cannot belong to its series.

        WHAT IS GUARANTEED (and what is not). The band is computed over the factor values this
        read actually multiplies into prices -- the distinct as-of hits `hfq_t[pos >= 0]`, plus,
        in `qfq` mode, the normalisation anchor `hfq_latest` (it divides every bar). It therefore
        guarantees "the segment of the series that reached the prices is single-magnitude". It
        does NOT guarantee "the `adjust_factors` table holds no foreign-basis rows": a symbol may
        legitimately carry an old prefix that the requested window never touches, and judging the
        whole array would fail every modern read of such a symbol (D9 follow-up: `600660.SH`'s
        1993-2023 BaoStock prefix made every 2024+ read raise, including the live breadth path --
        a wrong error replacing a wrong price). Cleaning the table is
        `scripts/repair_cross_source_adjust_factors.py`'s job, not the read path's.

        The write gate (`store_adjust_factors`) stops NEW cross-source overwrites, but rows
        polluted before that gate existed are already on disk, and `allow_cross_source=True`
        can produce them again. Applying such a row is silent otherwise: `qfq = raw *
        hfq[t]/hfq[latest]` turns `600660.SH`'s 52.47 close on 2026-09-24 into 3080.96, and the
        only downstream trace is a breadth / target-exposure / dividend-flag shift.

        The test is deliberately coarse -- median-relative, scale only -- because the legitimate
        signal (a monotone step per corporate action) and the defect (a value in a different
        UNIT) differ by orders of magnitude. See `IMPLAUSIBLE_ADJUST_FACTOR_RATIO` for why the
        band is 4x.
        """
        # Distinct values this read applies. A dict keyed on the VALUE (not the position) makes the
        # sample a set: one factor row typically serves hundreds of bars, and duplicates would
        # bias the median towards the length of their run. Event rows win over the `-1` anchor
        # marker so a poisoned latest row is reported by its own date, not as "the anchor".
        applied: Dict[float, int] = {}
        for i in np.unique(pos[pos >= 0]):
            applied.setdefault(float(event_hfq[i]), int(i))
        if mode == "qfq" and applied and divisor > 0:
            applied.setdefault(float(divisor), -1)

        if not applied:  # every bar precedes the first factor row -> nothing is applied
            return
        values = np.asarray(sorted(applied), dtype=float)
        sample_positions = [applied[v] for v in values]
        positive_mask = values > 0
        if not positive_mask.any():
            return
        positive = values[positive_mask]
        median = float(np.median(positive))
        if not np.isfinite(median) or median <= 0:
            return
        ratio = positive / median
        bad = np.flatnonzero(
            (ratio > IMPLAUSIBLE_ADJUST_FACTOR_RATIO)
            | (ratio < IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO)
        )
        if bad.size == 0:
            return

        # Report the worst offender, preferring the TOO-LARGE side: the D9 signature is a foreign
        # basis pasted in, which is always the larger number. When only the small side is out of
        # band (the whole applied segment sits above one small foreign anchor) that is reported
        # instead. Either way the operator gets a date they can find in the repair dry-run.
        pos_positive = np.flatnonzero(positive_mask)
        bad_ratios = ratio[bad]
        worst_local = int(
            bad[int(np.argmax(bad_ratios))] if (bad_ratios > 1).any() else bad[int(np.argmin(bad_ratios))]
        )
        worst = pos_positive[worst_local]
        origin = sample_positions[worst]
        if origin >= 0:
            date = pd.Timestamp(fac_df.index[origin]).date().isoformat()
            source = _source_at(fac_df, origin)
        else:
            # The anchor is the table's latest row, which may lie AFTER this window's `end`.
            row = self._get_connection(read_only=True)
            try:
                latest = row.execute(
                    "SELECT date, source FROM adjust_factors WHERE symbol = ? "
                    "ORDER BY date DESC LIMIT 1",
                    [symbol],
                ).fetchone()
            finally:
                if not self.is_memory:
                    row.close()
            date = (
                "the qfq anchor"
                if latest is None
                else (latest[0] if isinstance(latest[0], str) else latest[0].isoformat())
                + " (qfq anchor, latest factor row)"
            )
            source = None if latest is None else latest[1]

        bars = pd.Timestamp(bar_index.min()).date().isoformat() + ".." + pd.Timestamp(
            bar_index.max()
        ).date().isoformat()
        raise ImplausibleAdjustFactorStep(
            symbol,
            date,
            values[worst],
            median,
            source=source,
            n_offending=int(bad.size),
            n_factors=int(positive.size),
            bar_window=bars,
        )

    def _resolve_read_basis(
        self,
        provider: str,
        symbol: str,
        req_mode: str,
        *,
        interval: Optional[str] = None,
        allow_legacy_basis: bool = False,
    ) -> Optional[str]:
        """Three-state basis resolution shared by all four bar-read entry points.

        Returns the declared stored basis (`raw`/`qfq`/`hfq`), or `None` for the legacy
        pass-through (only reachable with `allow_legacy_basis=True`, or for a factor-neutral
        symbol). Raises:
          * `UnknownAdjustBasisError` -- stored basis is SQL NULL (pre-M3.6 row) and the caller
            did not opt into the legacy pass-through (M3.6 review F1). NOT downgraded to a warning
            and NOT silently widened to a smaller universe: a wrong-basis read is a percent-level
            price error, not a missing symbol;
          * `ValueError` -- stored basis is declared but different from the request and cannot be
            derived (e.g. stored `qfq`, asked `raw`).

        Factor-neutral symbols never raise: `_apply_factors_to_df` is the identity for them, so
        their basis cannot change a single returned price.
        """
        if interval is not None and is_intraday_interval(interval):
            meta = self.get_intraday_metadata(provider, symbol, interval)
            meta_table = "intraday_sync_metadata"
        else:
            meta = self.get_asset_metadata(provider, symbol)
            meta_table = "asset_sync_metadata"

        raw_basis = meta.get("adjustflag") if meta else None
        stored_basis = parse_stored_basis(raw_basis)

        if stored_basis is None:
            if is_factor_neutral_symbol(symbol):
                return None
            # P1: SQL NULL (pre-M3.6) and the `_UNDECLARED_BASIS` sentinel are ONE state for the
            # reader -- "nobody ever stated what these bytes are". Both fail closed; the only
            # difference is the audit trail (`is_legacy_basis_unknown` picks the wording), because
            # before the P1 fix the sentinel was silently readable and became a fresh bypass for
            # exactly the hazard F1 exists to stop.
            key = (provider, symbol, interval or "1d")
            if not allow_legacy_basis:
                raise UnknownAdjustBasisError(
                    provider, symbol, req_mode, interval or "1d", meta_table
                )
            if key not in _LEGACY_BASIS_WARNED:
                _LEGACY_BASIS_WARNED.add(key)
                origin = (
                    "carries NO recorded adjustment basis (pre-M3.6 row: the metadata column was "
                    "added by an in-place migration that backfilled nothing)"
                    if is_legacy_basis_unknown(raw_basis)
                    else "was stored with the undeclared-basis sentinel "
                    "(a writer that could not or would not state its basis)"
                )
                warnings.warn(
                    f"allow_legacy_basis pass-through for {symbol} ({provider}, "
                    f"interval={interval or '1d'}): the cached bars {origin}, so the "
                    f"'{req_mode}' request is answered with the stored values UNCHANGED (no "
                    f"factor derivation, no rescaling). This is only correct if the stored series "
                    f"already is '{req_mode}'. Declare the basis with "
                    f"scripts/migrate_adjust_basis.py to make this permanent."
                )
            return None

        if stored_basis != "raw" and stored_basis != req_mode:
            raise ValueError(
                f"Cannot derive '{req_mode}' adjustment for {symbol} ({provider}): "
                f"stored data has basis '{stored_basis}'."
            )
        return stored_basis

    def _guard_basis_transition(
        self,
        con,
        provider: str,
        symbol: str,
        incoming_basis: Optional[str],
        *,
        meta_table: str,
        bars_table: str,
        interval: Optional[str] = None,
        allow_basis_transition: bool = False,
    ) -> None:
        """Refuses undeclared -> declared basis flips on an already-populated symbol (F2/P1).

        Must run BEFORE the bar INSERT so the row count it reads is the *pre-existing* one.
        Silent in these cases:
          * `allow_basis_transition=True` (audited escape hatch; the caller logs it);
          * the incoming write declares nothing (`None` / `_UNDECLARED_BASIS`) -- nothing is
            being claimed, and the upsert's `CASE` keeps an existing undeclared marker intact;
          * no metadata row, or a row that already carries a declared basis;
          * factor-neutral symbols -- no derivation is ever applied to them, so the label is
            cosmetic and refusing here would only block index/ETF syncs;
          * zero stored bars -- there is nothing whose basis could be mislabelled.

        SCOPE (P1 revision): BOTH undeclared states are gated -- SQL NULL (pre-M3.6) and the
        `_UNDECLARED_BASIS` sentinel. The previous revision gated only NULL, on the argument that
        a sentinel row was readable pass-through anyway; that argument died with the P1 fix, which
        made the sentinel fail closed on read. Both are now "the reader is refused", so both are
        states in which a wrong label is unrecoverable -- the 2026-10-23 incident shape exactly.
        """
        if allow_basis_transition or incoming_basis is None or incoming_basis == _UNDECLARED_BASIS:
            return
        if is_factor_neutral_symbol(symbol):
            return

        extra = " AND interval = ?" if interval is not None else ""
        params: List[Any] = [provider, symbol] + ([interval] if interval is not None else [])
        row = con.execute(
            f"SELECT adjustflag FROM {meta_table} WHERE provider = ? AND symbol = ?{extra}",
            params,
        ).fetchone()
        if row is None:
            return
        existing = row[0]
        if parse_stored_basis(existing) is not None:
            # A declared basis is already recorded; only the two undeclared states are gated.
            return

        has_rows = con.execute(
            f"SELECT 1 FROM {bars_table} WHERE provider = ? AND symbol = ?{extra} LIMIT 1",
            params,
        ).fetchone()
        if has_rows is None:
            return

        raise AdjustBasisTransitionRefused(
            provider,
            symbol,
            incoming_basis,
            None,
            meta_table,
            interval or "1d",
        )

    def _apply_factors_to_df(
        self,
        df: pd.DataFrame,
        symbol: str,
        adjust_mode: str = "qfq",
    ) -> pd.DataFrame:
        """Scales Open, High, Low, Close by factor_hfq or derived QFQ in memory.

        Derivation rules:
        - hfq price = raw * hfq[t] (as-of / backward lookup: latest factor with date <= bar date)
        - qfq price = raw * hfq[t] / hfq[latest in whole table for symbol]
        - Bars before the first factor date use 1.0.
        - Volume and Amount are NEVER scaled!

        The as-of lookup is a single `np.searchsorted` over DAY-normalised dates
        (M3.6 review F5): the previous implementation did a Python loop with `bisect` per bar,
        i.e. one million iterations on a 1.23M-bar 5m series, on the hot read path. The
        replacement is bit-for-bit equivalent -- `searchsorted(side="right") - 1` is exactly
        `bisect.bisect_right(...) - 1`, the `-1` case is exactly the old
        `d < event_dates[0] -> 1.0` branch, and the per-element division is the same IEEE double
        division, just broadcast -- and pinned by
        `pipeline/tests/test_adjust_factor_derivation.py::test_apply_factors_vectorised_equivalence`.

        The former `factors=` parameter is gone (M3.6 review F5): all four call sites passed
        `None`, and its branch anchored QFQ normalisation on `event_hfq[-1]` (the last row of the
        passed frame) while the main path anchors on `get_latest_factor_hfq` (the whole table).
        Two different anchors for the same quantity is precisely the kind of drift that makes a
        windowed read disagree with a full-range read, so the dead branch was removed rather than
        "unified" -- there is now exactly one anchor.
        """
        if df.empty:
            return df.copy()

        mode = canonicalize_adjust_mode(adjust_mode)
        if mode == "raw":
            return df.copy()

        if is_factor_neutral_symbol(symbol):
            return df.copy()

        out = df.copy()

        hfq_latest = self.get_latest_factor_hfq(symbol)
        if hfq_latest is None:
            raise MissingAdjustFactorsError(symbol)

        max_bar_date = pd.Timestamp(df.index.max()).date().strftime("%Y-%m-%d")
        fac_df = self.query_adjust_factors(symbol, end=max_bar_date)
        if fac_df.empty:
            raise MissingAdjustFactorsError(symbol)

        event_hfq = fac_df["factor_hfq"].values.astype(float)
        event_days = _day_keys(fac_df.index)
        bar_days = _day_keys(out.index)

        # as-of join, vectorised: index of the last event day <= bar day (-1 when none).
        pos = np.searchsorted(event_days, bar_days, side="right") - 1
        hfq_t = np.where(pos >= 0, event_hfq[np.clip(pos, 0, None)], 1.0)

        divisor = hfq_latest if (hfq_latest is not None and hfq_latest > 0) else 1.0
        # Judged on the factors this read actually APPLIES (D9 follow-up): `query_adjust_factors`
        # has no `start` filter, so `event_hfq` carries the symbol's whole history including a
        # foreign-basis prefix the requested window never touches. Banding the raw array turned
        # every 2024+ read of `600660.SH` into `ImplausibleAdjustFactorStep` even though the
        # prices being scaled were all dense-basis -- a wrong error replacing a wrong price.
        self._assert_plausible_adjust_factors(
            symbol, fac_df, event_hfq, pos, divisor, mode, out.index
        )

        if mode == "hfq":
            applied_mults = hfq_t
        else:  # qfq
            applied_mults = hfq_t / divisor

        for col in ["Open", "High", "Low", "Close"]:
            if col in out.columns:
                out[col] = out[col] * applied_mults

        return out

    def query_intraday_bars(
        self,
        provider: str,
        symbol: str,
        interval: str = "5m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        adjustflag: str = "qfq",
        *,
        allow_legacy_basis: bool = False,
        columns: Optional[Any] = None,
    ) -> pd.DataFrame:
        """Retrieves intraday OHLCV bars for (provider, symbol, interval) across [start, end].

        `columns=` follows the M3.6 contract: `None` (default) returns exactly
        `_REQUIRED_OHLCV_COLUMNS` and NOT `Amount`; `"all"` adds `Amount`; an explicit sequence
        returns exactly those columns (an unavailable name raises `ValueError`). The old
        behaviour -- shape silently changing with whether `amount` happened to be non-NULL on
        disk -- is what that rule exists to stop.

        `allow_legacy_basis=True` is the warned opt-in for pre-M3.6 rows whose basis was never
        recorded; without it (and for a non-neutral symbol) the read raises
        `UnknownAdjustBasisError` instead of returning possibly-mislabelled prices.
        """
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
            f"SELECT ts, open, high, low, close, volume, amount FROM intraday_ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts"
        )

        requested_cols = _resolve_requested_columns(columns)

        con = self._get_connection(read_only=True)
        try:
            res_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        if res_df.empty:
            return pd.DataFrame(
                columns=requested_cols if requested_cols is not None else list(_REQUIRED_OHLCV_COLUMNS)
            )

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
                "amount": "Amount",
            }
        )
        res_df = _select_output_columns(
            res_df, requested_cols, context=f"query_intraday_bars({provider}, {symbol}, {interval})"
        )

        req_mode = canonicalize_adjust_mode(adjustflag)
        stored_basis = self._resolve_read_basis(
            provider,
            symbol,
            req_mode,
            interval=interval,
            allow_legacy_basis=allow_legacy_basis,
        )
        if stored_basis == "raw":
            res_df = self._apply_factors_to_df(res_df, symbol, adjust_mode=req_mode)

        return res_df

    get_intraday_bars = query_intraday_bars

    # ------------------------------------------------------------------
    # provider-key reconciliation (read path only; see `_PROVIDER_KEY_SUFFIX`)
    # ------------------------------------------------------------------
    def distinct_provider_keys(self) -> List[str]:
        """Every provider key this cache has ever been written with, bars first.

        `ohlcv_bars` is authoritative for reads; `asset_sync_metadata` is unioned in because a
        key that only exists there is still the cache telling us "this upstream name was used
        here", which is exactly what a resolution warning should report. A missing table (a
        pre-migration cache) is skipped rather than fatal -- resolution is a best-effort
        diagnostic, never a reason to fail a read.
        """
        con = self._get_connection(read_only=True)
        try:
            rows: List[Any] = []
            for table in ("ohlcv_bars", "asset_sync_metadata"):
                try:
                    rows.extend(con.execute(f"SELECT DISTINCT provider FROM {table}").fetchall())
                except Exception:
                    continue
        finally:
            if not self.is_memory:
                con.close()

        keys: List[str] = []
        for row in rows:
            key = row[0] if row else None
            if key is not None and str(key) not in keys:
                keys.append(str(key))
        return keys

    def resolve_provider_key(self, provider: str) -> str:
        """Map a caller's provider name onto the key the cache actually stores.

        The defect this closes: `common/data.py` writes bars keyed by
        `type(inner_provider).__name__` (`FuyaoDataProvider`), while the live station passes the
        short name (`fuyao`). A `query_bars` against the short name returns an EMPTY frame
        instead of raising (`query_bars` returns before `_resolve_read_basis` when the result is
        empty), so the mismatch surfaces as `StalePriceError: no price at all` -- a staleness
        diagnosis for a fully populated cache.

        Resolution rules, in order:
          1. EXACT stored key -> returned unchanged (no rewrite, no warning beyond an
             ambiguity note). A key literally spelled `fuyao` is the most specific evidence
             available and always wins.
          2. Else exactly ONE stored key whose `canonicalize_provider_key` form matches ->
             that key is returned.
          3. Else (zero matches, or >= 2 matches) -> the ORIGINAL value is returned unchanged.

        `query_bars` is deliberately NOT taught to do this internally: a silent fallback inside
        the read path would also launder a genuine typo (`fuyoa`) into a successful read, and a
        multi-match cache has no defensible answer to guess. Callers opt in, and every step is
        logged at WARNING as `requested -> resolved` so the reconciliation is visible in the
        live log (L2.18: a repair is never silent).

        Synthetic providers short-circuit: their bars are generated on the fly and never
        cached (`is_synthetic_provider`), so there is no stored key to reconcile and a
        "no match" warning would be noise on every synthetic run.
        """
        if provider is None:
            return provider
        requested = str(provider)
        if is_synthetic_provider(requested):
            return requested

        known = self.distinct_provider_keys()

        if requested in known:
            collisions = [
                k
                for k in known
                if canonicalize_provider_key(k) == canonicalize_provider_key(requested)
            ]
            if len(collisions) > 1:
                # Two distinct upstreams can share a canonical form (`fuyao` and
                # `FuyaoDataProvider` hold DIFFERENT bytes). The caller named one of them
                # exactly, so no substitution happens -- but the operator must know the cache
                # is storing both, because the two rows sets will diverge (e.g. one symbol
                # present under one key and absent under the other).
                logger.warning(
                    "DuckDBCache.resolve_provider_key: %r is stored exactly, but %d provider "
                    "keys share its canonical form %r (%s); using the EXACT key %r and NOT "
                    "guessing between them",
                    requested,
                    len(collisions),
                    canonicalize_provider_key(requested),
                    ", ".join(sorted(collisions)),
                    requested,
                )
            return requested

        target = canonicalize_provider_key(requested)
        matches = [k for k in known if canonicalize_provider_key(k) == target]

        if len(matches) == 1:
            resolved = matches[0]
            logger.warning(
                "DuckDBCache.resolve_provider_key: requested provider %r has no stored bars; "
                "resolved to the only canonical match %r (requested -> resolved). The cache is "
                "keyed by provider CLASS NAME (`common/data.py` stores "
                "`type(inner_provider).__name__`), while the CLI / settings.local.json use the "
                "short name.",
                requested,
                resolved,
            )
            return resolved

        if not matches:
            logger.warning(
                "DuckDBCache.resolve_provider_key: requested provider %r matches NO stored key "
                "(stored: %s); keeping it as-is -- the read will return an empty frame. A typo "
                "is NOT silently repaired here.",
                requested,
                ", ".join(sorted(known)) or "<cache has no provider keys>",
            )
            return requested

        logger.warning(
            "DuckDBCache.resolve_provider_key: requested provider %r is AMBIGUOUS -- %d stored "
            "keys share its canonical form %r (%s); keeping %r as-is rather than guessing. Pass "
            "one of the stored keys exactly.",
            requested,
            len(matches),
            target,
            ", ".join(sorted(matches)),
            requested,
        )
        return requested

    # -- LV19: per-namespace freshness trace of the last daily-bar patch pass -- #
    #: The three states are exhaustive and their meanings are exact (`docs/lv19_freshness_contract.md`
    #: §4). `skipped` does NOT prove the namespace satisfies the zero-tolerance criterion -- it says
    #: only "this pass wrote nothing here" -- which is precisely the gap `per_namespace` exists to
    #: expose next to `fetch_errors`.
    NAMESPACE_TRACE_STATES = ("skipped", "patched", "failed")

    def record_namespace_trace(self, trace: Dict[str, str]) -> None:
        """Publish the last `patch_equity_daily_bars` pass's `{provider -> state}` trace.

        WHY ON THE CACHE (and not in the return value): `patch_equity_daily_bars` returns
        `symbol -> "<provider>: Type: msg"`, a shape three production call sites and ~10 offline
        tests already depend on, so LV19's mandatory `per_namespace` output gets its own channel
        instead of changing that mapping's meaning. The cache is the object the call already
        owns and the object the station already holds open, so the trace cannot be lost between
        the primitive and its caller.

        Scope: PROCESS-LOCAL and LAST-PASS-ONLY -- it is not persisted to DuckDB, because it
        describes one refresh attempt, not a data fact (the stored facts are `ohlcv_bars` and
        `*_sync_metadata`). A read-only handle accepts the assignment too: it is an attribute,
        not a write.
        """
        unknown = sorted({str(v) for v in (trace or {}).values()} - set(self.NAMESPACE_TRACE_STATES))
        if unknown:
            raise ValueError(
                f"record_namespace_trace: unknown state(s) {unknown}; allowed are "
                f"{list(self.NAMESPACE_TRACE_STATES)}"
            )
        self._namespace_trace = {str(k): str(v) for k, v in (trace or {}).items()}

    def namespace_trace(self) -> Dict[str, str]:
        """`{provider -> skipped|patched|failed}` from the last patch pass ({} if none ran)."""
        return dict(getattr(self, "_namespace_trace", {}) or {})

    def query_bars(
        self,
        provider: str,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
        adjust: str = "qfq",
        *,
        allow_legacy_basis: bool = False,
        columns: Optional[Any] = None,
    ) -> pd.DataFrame:
        """Retrieves OHLCV bars for (provider, symbol) across [start, end], resampled to interval.

        See `query_intraday_bars` for the `columns=` and `allow_legacy_basis=` contracts.
        """
        if is_intraday_interval(interval):
            return self.query_intraday_bars(
                provider,
                symbol,
                interval=interval,
                start=start,
                end=end,
                adjustflag=adjust,
                allow_legacy_basis=allow_legacy_basis,
                columns=columns,
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
            f"SELECT date, open, high, low, close, volume, amount FROM ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY date"
        )

        requested_cols = _resolve_requested_columns(columns)

        con = self._get_connection(read_only=True)
        try:
            res_df = con.execute(sql, params).df()
        finally:
            if not self.is_memory:
                con.close()

        if res_df.empty:
            return pd.DataFrame(
                columns=requested_cols if requested_cols is not None else list(_REQUIRED_OHLCV_COLUMNS)
            )

        res_df["date"] = pd.to_datetime(res_df["date"])
        res_df = res_df.set_index("date").sort_index()
        res_df = res_df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        res_df = _select_output_columns(
            res_df, requested_cols, context=f"query_bars({provider}, {symbol})"
        )

        req_mode = canonicalize_adjust_mode(adjust)
        stored_basis = self._resolve_read_basis(
            provider,
            symbol,
            req_mode,
            allow_legacy_basis=allow_legacy_basis,
        )
        if stored_basis == "raw":
            res_df = self._apply_factors_to_df(res_df, symbol, adjust_mode=req_mode)

        return resample_ohlcv(res_df, interval)

    get_bars = query_bars

    def query_intraday_universe(
        self,
        provider: str,
        symbols: List[str],
        interval: str = "5m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        adjustflag: str = "qfq",
        *,
        allow_legacy_basis: bool = False,
        columns: Optional[Any] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Batch-queries intraday OHLCV bars for multiple symbols in a single SQL query.

        Same `columns=` / `allow_legacy_basis=` contracts as `query_intraday_bars`; the callers
        that wrap a batch read (`CachedDataProvider.fetch_universe`) must NOT swallow the
        exceptions below -- a shrunken universe is the failure mode this cache exists to refuse.
        """
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
            f"SELECT symbol, ts, open, high, low, close, volume, amount FROM intraday_ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol, ts"
        )

        requested_cols = _resolve_requested_columns(columns)

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

        req_mode = canonicalize_adjust_mode(adjustflag)

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
                    "amount": "Amount",
                }
            )
            df = _select_output_columns(
                df, requested_cols, context=f"query_intraday_universe({provider}, {sym}, {interval})"
            )
            stored_basis = self._resolve_read_basis(
                provider,
                sym,
                req_mode,
                interval=interval,
                allow_legacy_basis=allow_legacy_basis,
            )
            if stored_basis == "raw":
                df = self._apply_factors_to_df(df, sym, adjust_mode=req_mode)
            result[sym] = df

        return result

    def query_universe(
        self,
        provider: str,
        symbols: List[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        interval: str = "1d",
        adjust: str = "qfq",
        *,
        allow_legacy_basis: bool = False,
        columns: Optional[Any] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Batch-queries OHLCV bars for multiple symbols in a single SQL query.

        Same `columns=` / `allow_legacy_basis=` contracts as `query_bars`.
        """
        if is_intraday_interval(interval):
            return self.query_intraday_universe(
                provider,
                symbols,
                interval=interval,
                start=start,
                end=end,
                adjustflag=adjust,
                allow_legacy_basis=allow_legacy_basis,
                columns=columns,
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
            f"SELECT symbol, date, open, high, low, close, volume, amount FROM ohlcv_bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol, date"
        )

        requested_cols = _resolve_requested_columns(columns)

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

        req_mode = canonicalize_adjust_mode(adjust)

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
                    "amount": "Amount",
                }
            )
            df = _select_output_columns(
                df, requested_cols, context=f"query_universe({provider}, {sym})"
            )
            stored_basis = self._resolve_read_basis(
                provider,
                sym,
                req_mode,
                allow_legacy_basis=allow_legacy_basis,
            )
            if stored_basis == "raw":
                df = self._apply_factors_to_df(df, sym, adjust_mode=req_mode)
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


# ---------------------------------------------------------------------------
# Observability: does `adjust_factors` reach the same session as `ohlcv_bars`?
# ---------------------------------------------------------------------------
#: Aggregate states of `inspect_adjust_factor_freshness`. Worst-first precedence is
#: `missing` > `lagging` > `ok`; `unknown` is reserved for "the census could not be read at
#: all" (never conflated with "no factors", which IS a measured, nameable fact).
ADJUST_FACTOR_FRESHNESS_OK = "ok"
ADJUST_FACTOR_FRESHNESS_LAGGING = "lagging"
ADJUST_FACTOR_FRESHNESS_MISSING = "missing"
ADJUST_FACTOR_FRESHNESS_UNKNOWN = "unknown"


def inspect_adjust_factor_freshness(
    db_path: str,
    *,
    symbols: Optional[Sequence[str]] = None,
    bar_provider: Optional[str] = None,
    worst_limit: int = 5,
) -> Dict[str, Any]:
    """READ-ONLY OBSERVATION: per symbol, how far `adjust_factors` lags `ohlcv_bars`.

    WHY THIS EXISTS (2026-11-13). `adjust_factors` is a SEPARATE table from `ohlcv_bars`, written
    by a separate provider path (`DuckDBCache.store_adjust_factors` <- the local MarketDB factor
    table) while raw bars keep arriving from the Fuyao REST feed. Nothing compared the two maxima,
    and `get_latest_factor_hfq` is non-None for such a symbol -- so `MissingAdjustFactorsError`
    never fired while 12 sessions of qfq prices were derived from a factor set that stopped at
    2026-10-27. The read is `qfq = raw * factor_hfq[t] / factor_hfq[latest]`: the ANCHOR is the
    latest stored factor, so a stale tail is invisible in the output shape and only becomes a
    priced error once a corporate action lands inside the gap (on 2026-11-13 none had: the remote
    Fuyao qfq series equalled the cached raw closes for every one of the 12 sessions). This
    function makes that state NAMEABLE instead of silently benign.

    CONTRACT -- IT IS NOT A GATE.
      * It never raises: an unreadable database returns `status="unknown"` plus `error`.
      * It writes nothing, opens `read_only=True`, and creates no directory.
      * It is consumed by the Stage-1 report as `adjust_factor_freshness` and by one `[WARN]`
        line. NO order path, weight path, freshness gate or fail-closed decision reads it --
        Stage 2 behaves exactly as it did before this function existed. If a later decision wants
        it to gate, that is a deliberate change to be made THERE, not a side effect of wiring an
        observation in.

    SCOPE. Factor-neutral symbols (indices, cash ETF 511880 -- `is_factor_neutral_symbol`) are
    excluded: they legitimately carry no factor rows, and reporting them as `missing` would turn
    a correct state into a permanent warning. A non-neutral symbol WITH bars but no factor rows is
    `missing`; a symbol with no bars at all is `missing` with `reason="no_bars"` (not measurable,
    and `assert_fresh_universe` owns that failure).

    `lag_sessions` counts DISTINCT bar dates strictly greater than that symbol's
    `max(factor_date)` -- the exact sessions whose qfq derivation carries the last factor forward
    by as-of lookup. `lag_days` is the plain calendar difference of the two maxima.

    `bar_provider` selects the write namespace the bars are read from (`None` = every provider,
    i.e. the freshest row any namespace holds). Stage 1 passes the provider it just refreshed, so
    the census describes the namespace the run actually read.

    Returned block: `status` (`ok` | `lagging` | `missing` | `unknown`), the counts, the maxima,
    `worst_symbols` (the `worst_limit` deepest lags) and `per_symbol` (ISO date strings, so the
    block drops straight into `stage1_health_report.json`).
    """
    block: Dict[str, Any] = {
        "status": ADJUST_FACTOR_FRESHNESS_UNKNOWN,
        "enforcement": "observation_only",
        "gates_orders": False,
        "bar_provider": bar_provider,
        "n_symbols": 0,
        "n_ok": 0,
        "n_lagging": 0,
        "n_missing": 0,
        "max_lag_sessions": 0,
        "max_lag_days": 0,
        "worst_symbols": [],
        "per_symbol": {},
        "error": None,
    }
    con = None
    try:
        if not db_path or str(db_path) == ":memory:":
            raise ValueError("inspect_adjust_factor_freshness needs a cache FILE path")
        if not os.path.exists(str(db_path)):
            raise FileNotFoundError(f"cache file not found: {db_path}")

        con = duckdb.connect(str(db_path), read_only=True)
        want = [str(s).strip() for s in symbols] if symbols is not None else None

        fac = {
            row[0]: (row[1], int(row[2]))
            for row in con.execute(
                "SELECT symbol, max(date), count(*) FROM adjust_factors GROUP BY 1"
            ).fetchall()
        }

        bar_clauses = ["1=1"]
        bar_params: List[Any] = []
        if bar_provider:
            bar_clauses.append("provider = ?")
            bar_params.append(str(bar_provider))
        if want is not None and want:
            bar_clauses.append(f"symbol IN ({','.join('?' * len(want))})")
            bar_params.extend(want)
        bars = {
            row[0]: (row[1], int(row[2]))
            for row in con.execute(
                "SELECT symbol, max(date), count(*) FROM ohlcv_bars WHERE "
                + " AND ".join(bar_clauses)
                + " GROUP BY 1",
                bar_params,
            ).fetchall()
        }

        candidates = sorted(set(fac) | set(bars)) if want is None else list(dict.fromkeys(want))
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for sym in candidates:
            if is_factor_neutral_symbol(sym):
                continue
            factor_max, n_factor_rows = fac.get(sym, (None, 0))
            bar_max, n_bar_rows = bars.get(sym, (None, 0))
            entry: Dict[str, Any] = {
                "symbol": sym,
                "factor_max_date": factor_max,
                "bar_max_date": bar_max,
                "factor_rows": n_factor_rows,
                "bar_rows": n_bar_rows,
                "reason": None,
                "lag_sessions": None,
                "lag_days": None,
            }
            if n_bar_rows == 0:
                entry.update(status=ADJUST_FACTOR_FRESHNESS_MISSING, reason="no_bars")
            elif n_factor_rows == 0:
                entry.update(status=ADJUST_FACTOR_FRESHNESS_MISSING, reason="no_factor_rows")
            else:
                lag_days = int((bar_max - factor_max).days)
                entry["lag_days"] = lag_days
                if lag_days > 0:
                    clauses = ["symbol = ?", "date > ?"]
                    sparams: List[Any] = [sym, str(factor_max)]
                    if bar_provider:
                        clauses.append("provider = ?")
                        sparams.append(str(bar_provider))
                    n_sessions = con.execute(
                        "SELECT count(DISTINCT date) FROM ohlcv_bars WHERE "
                        + " AND ".join(clauses),
                        sparams,
                    ).fetchone()[0]
                    entry.update(
                        status=ADJUST_FACTOR_FRESHNESS_LAGGING,
                        lag_sessions=int(n_sessions or 0),
                        lag_days=lag_days,
                    )
                else:
                    entry.update(status=ADJUST_FACTOR_FRESHNESS_OK, lag_sessions=0)
            per_symbol[sym] = entry

        # Aggregate. Worst-first: a missing factor set outranks a measurable lag, which outranks
        # ok -- "19 of 20 fresh" must never render as `ok`. Serialisation (ISO date strings) is
        # applied first so `worst_symbols` carries exactly the same JSON-safe shape as
        # `per_symbol` and can be dropped into `stage1_health_report.json` unmodified.
        serialised = {
            sym: {
                **{k: v for k, v in e.items() if k not in ("factor_max_date", "bar_max_date")},
                "factor_max_date": (
                    None if e["factor_max_date"] is None else str(e["factor_max_date"])
                ),
                "bar_max_date": None if e["bar_max_date"] is None else str(e["bar_max_date"]),
            }
            for sym, e in per_symbol.items()
        }
        lagging = [e for e in serialised.values() if e["status"] == ADJUST_FACTOR_FRESHNESS_LAGGING]
        missing = [e for e in serialised.values() if e["status"] == ADJUST_FACTOR_FRESHNESS_MISSING]
        block["n_symbols"] = len(serialised)
        block["n_ok"] = sum(
            1 for e in serialised.values() if e["status"] == ADJUST_FACTOR_FRESHNESS_OK
        )
        block["n_lagging"] = len(lagging)
        block["n_missing"] = len(missing)
        block["max_lag_sessions"] = max((e["lag_sessions"] or 0) for e in lagging) if lagging else 0
        block["max_lag_days"] = max((e["lag_days"] or 0) for e in lagging) if lagging else 0
        worst = sorted(
            lagging, key=lambda e: (-(e["lag_sessions"] or 0), -(e["lag_days"] or 0), e["symbol"])
        )
        block["worst_symbols"] = [dict(e) for e in worst[: max(0, int(worst_limit))]]
        if missing:
            block["status"] = ADJUST_FACTOR_FRESHNESS_MISSING
        elif lagging:
            block["status"] = ADJUST_FACTOR_FRESHNESS_LAGGING
        elif serialised:
            block["status"] = ADJUST_FACTOR_FRESHNESS_OK
        else:
            # Nothing to inspect is NOT a pass: no factor-bearing, non-neutral symbol was seen.
            block["status"] = ADJUST_FACTOR_FRESHNESS_UNKNOWN
            block["error"] = "no non-neutral symbol with bars or factor rows was found"
        block["per_symbol"] = serialised
    except Exception as exc:  # noqa: BLE001 -- an observation must never break the caller
        block["status"] = ADJUST_FACTOR_FRESHNESS_UNKNOWN
        block["error"] = f"{type(exc).__name__}: {exc}"
        return block
    finally:
        if con is not None:
            con.close()
    return block


def format_adjust_factor_freshness_warning(block: Optional[Dict[str, Any]]) -> Optional[str]:
    """One grep-able `[WARN]` line for a `lagging`/`missing` census, else `None`.

    Kept separate from `inspect_adjust_factor_freshness` so the printed line and the persisted
    JSON can never disagree, and so the reader of a Stage-1 log sees the SAME numbers the report
    carries. Pure formatting: it cannot raise on a well-formed block, returns `None` for
    `ok`/`unknown` (an unavailable census is already announced by its own `error`), and states
    that nothing is gated.
    """
    if not isinstance(block, dict):
        return None
    status = block.get("status")
    if status not in (ADJUST_FACTOR_FRESHNESS_LAGGING, ADJUST_FACTOR_FRESHNESS_MISSING):
        return None
    n_bad = int(block.get("n_lagging") or 0) + int(block.get("n_missing") or 0)
    n_total = int(block.get("n_symbols") or 0)
    worst = block.get("worst_symbols") or []
    detail = "、".join(
        f"{w.get('symbol')} 因子 {w.get('factor_max_date')} < 行情 {w.get('bar_max_date')}"
        f"（{w.get('lag_sessions')} 个交易日）"
        for w in worst[:3]
    )
    missing_syms = [
        sym
        for sym, e in (block.get("per_symbol") or {}).items()
        if e.get("status") == ADJUST_FACTOR_FRESHNESS_MISSING
    ]
    if missing_syms:
        detail = (detail + "；" if detail else "") + f"无因子: {', '.join(missing_syms[:5])}"
    return (
        f"[WARN] adjust_factor_freshness={status}: {n_bad}/{n_total} 个标的复权因子落后/缺失"
        f"（最多 {block.get('max_lag_sessions')} 个交易日）。{detail}。"
        f" 仅观测：不参与任何门禁、权重或下单路径。"
    )

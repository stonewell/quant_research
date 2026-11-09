"""Advisory live IC / ICIR / half-life decay monitor (task `AT3`).

WHAT THIS IS
============
`common/signal_snapshot.py` pins *what* a daily cross-sectional score looks like.
This module consumes those snapshots plus subsequent close prices and reports, per
`(market, universe_id, strategy)` leg, the live signal-decay diagnostics:

* daily cross-sectional **Rank IC**,
* annualised **IC-IR** and the Student-t statistic / p-value,
* the **AR(1) half-life** of the signal,

plus an explicit `status` that refuses to turn "not measurable yet" into a number.

Every one of those numbers is produced by `common/factor_analyzer.py` -- this
module **does not reimplement the math** and must not start doing so:

==============================================  ====================================
delegation target (line)                        used for
==============================================  ====================================
`factor_analyzer.compute_forward_returns`       close-to-close forward returns
(common/factor_analyzer.py:29)
`factor_analyzer.compute_rank_ic`               daily Rank IC series
(common/factor_analyzer.py:60)
`factor_analyzer.factor_ic_summary`             mean IC / std / IC-IR / t / p, and
(common/factor_analyzer.py:162)                 (internally) the half-life from
                                                `compute_factor_autocorrelation_half_life`
                                                (common/factor_analyzer.py:115)
==============================================  ====================================

FROZEN HORIZON AND RETURN BASIS (书面冻结, `AT3` condition 3)
==========================================================
These are **frozen in writing here**, not chosen per run:

* **Return basis: close-to-close.** `FROZEN_RETURN_BASIS = "close_to_close"` and
  `FROZEN_PRICE_FIELD = "Close"`. Forward return at `t` for horizon `h` is
  `Close[t+h] / Close[t] - 1` (that is exactly `compute_forward_returns`).
* **Horizon: `FROZEN_HORIZON_BARS = 5` trading bars** (one trading week). Any
  other value has to be passed explicitly and is echoed in every report as
  `horizon_bars`, so a consumer can never mistake two horizons for one history.
  Changing the frozen value requires a dated docstring amendment here.
* **Information set: close only.** `--cutoff-time` defaults to 14:15, which is an
  **intraday** information set: a score computed from a 14:15 snapshot knows
  nothing about the 14:15->15:00 move, while the return it would be scored
  against does. Mixing the two manufactures look-ahead. A snapshot therefore
  declares `information_set`, and `add_snapshot` rejects anything other than
  `"close"` (`IntradayInformationSetError`) instead of quietly comparing them.
  Limitation, disclosed: the field is declared by the producer, so this is an
  enforced *contract*, not a cryptographic proof of when the score was computed.

COMPLETENESS IS MANDATORY, NOT CALLER-OPTIONAL (`AT3` condition 2)
=================================================================
`SignalSnapshot` can only *detect* selection truncation when the producer declares its
candidate universe: `is_complete` is `bool(candidates) and set(candidates) == set(scores)`,
and the snapshot module itself rejects a declared-but-uncovered universe
(`SelectionTruncatedSnapshotError`) at construction time. That guard is therefore *opt-in
at write time*: a snapshot recorded **without** `candidates=` is `is_complete == False`, and
is indistinguishable from "only the held names were ever scored".

This monitor does not inherit that leniency. Every snapshot offered to
`ICDecayMonitor.add_snapshot` / `add_snapshots` / `evaluate_ic_decay` must be complete, and
ingestion is where that is enforced (`IncompleteSnapshotError`, which is both a
`MonitorInputError` and a `SelectionTruncatedSnapshotError` so either contract's callers
catch it):

* **no declared `candidates`** -> rejected as *completeness undeclared*: the monitor cannot
  tell a full cross-section from a selected-only one, and guessing is not allowed;
* **declared `candidates` the scores do not cover** -> rejected as *selection-truncated*.

Why fail loud instead of skipping the offending snapshot: a cross-section truncated by the
selection itself ranks only the names that already won against each other, so every IC /
IC-IR / half-life number derived from it is **structurally inflated**. Because snapshots are
not recomputable (see "NO BACKFILL" below), an accepted truncated snapshot is also a
permanently biased history. The rejection therefore happens before any score panel is built
or any `factor_analyzer` primitive is called -- this monitor never produces an IC it would
have to disclaim afterwards.

THE 15-SYMBOL FLOOR AND THE NO-ZERO-FILL RULE (`AT3` condition 4)
=================================================================
A single-day cross-section with fewer than `MIN_CROSS_SECTION_ASSETS = 15`
symbols cannot support a Rank IC estimate. Such a day is **excluded** and counted
(`n_dates_below_cross_section_floor`); it is **never** entered as `0.0`, never
forward-filled, and never silently dropped from the count. If no day clears the
floor the leg reports `INSUFFICIENT_HISTORY` with `mean_ic = nan` -- not `0.0` --
because a zero would read as "measured, no predictive power", which is a
different and much stronger claim. A NaN cell inside a day (missing score or
missing forward return) stays NaN; `compute_rank_ic` drops it from that day's
cross-section, and only the *count* of usable names decides the floor.

NO BACKFILL (`AT3` condition 5)
===============================
Snapshots are **not recomputable**: prices get qfq-revised, and strategy config /
dumped weights drift, so today's code cannot reproduce what the strategy said ten
days ago. Consequences enforced structurally in `add_snapshot`:

* the monitor has a `start_date`; a snapshot dated before it raises
  `BackfilledSnapshotError`,
* a snapshot whose `recorded_at` is later than its evaluation date by more than
  `max_recording_lag_days` (default 1 day) raises `BackfilledSnapshotError` -- that
  is what "reconstructed this morning from today's data" looks like,
* history files are append-only (`write_snapshot(..., append=True)`); nothing here
  rewrites a past snapshot.

FIRST POSSIBLE CONCLUSION (`AT3` condition 6)
=============================================
Even with a perfect score source, the earliest defensible conclusion is **after 10
valid evaluation days plus the horizon**: the 10th valid day's IC is only known
once its forward return completes. `LegICReport.first_conclusion_available` is
computed from exactly that rule and is `None` while the data does not reach it.

ADVISORY ONLY
=============
Nothing in this module gates, reweights, resizes or orders. It imports no
`subprocess`, no `requests`, no `scripts/`, no `web/`; its output is a report
object. It is also deliberately *not* wired into `scripts/run_live_strategy.py`:
the live chain has no cross-sectional score to feed it yet (see the
`common/signal_snapshot.py` docstring on `detail_reason.dimension_signals`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from common import factor_analyzer as _fa
from common.market_clock import UnknownSymbolMarketError, infer_market_from_symbol
from common.signal_snapshot import (
    CLOSE_INFORMATION_SET,
    MIN_CROSS_SECTION_ASSETS,
    MIN_VALID_EVAL_DAYS,
    SelectionTruncatedSnapshotError,
    SignalSnapshot,
)

#: Frozen return basis. See the module docstring; do not vary per run.
FROZEN_RETURN_BASIS = "close_to_close"
FROZEN_PRICE_FIELD = "Close"
#: Frozen horizon in trading bars (one trading week).
FROZEN_HORIZON_BARS = 5
#: A snapshot may be recorded at most this many calendar days after its session.
MAX_RECORDING_LAG_DAYS = 1

STATUS_OK = "OK"
STATUS_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

LegKey = Tuple[str, str, str]  # (market, universe_id, strategy)


class MonitorInputError(ValueError):
    """Base class for monitor input rejections."""


class BackfilledSnapshotError(MonitorInputError):
    """A snapshot predates the monitor's start, or was written after its own session."""


class IntradayInformationSetError(MonitorInputError):
    """An intraday (e.g. `--cutoff-time` 14:15) snapshot was offered to a close-to-close monitor."""


class IncompleteSnapshotError(MonitorInputError, SelectionTruncatedSnapshotError):
    """A snapshot's candidate universe is undeclared or not fully covered.

    Raised at ingest, never later, for the `AT3` condition 2 reason: an IC computed
    on a cross-section truncated by the selection itself is structurally inflated,
    and snapshots are not recomputable, so the defect is unrecoverable once stored.

    Inherits **both** contracts on purpose -- `MonitorInputError` (this monitor's input
    rejection) and `SelectionTruncatedSnapshotError` (the snapshot module's truncation
    type) -- so a caller catching either one still fails loud on this class, instead of
    silently letting the snapshot through.
    """


def _require_complete_snapshot(snapshot: Any) -> SignalSnapshot:
    """Reject a non-snapshot or an incomplete one, before any ingestion side effect.

    Completeness is *mandatory* here rather than caller-opt-in: `SignalSnapshot`
    only records `candidates` when the producer declares them, so an undeclared
    snapshot looks exactly like a snapshot of the selected names only. See the
    module docstring section "COMPLETENESS IS MANDATORY".
    """
    if not isinstance(snapshot, SignalSnapshot):
        raise MonitorInputError(
            f"add_snapshot expects a SignalSnapshot, got {type(snapshot).__name__}; "
            "build one with SignalSnapshot.from_frame / record_signal_snapshot"
        )
    if snapshot.is_complete:
        return snapshot

    label = (
        f"snapshot for universe_id={snapshot.universe_id!r} "
        f"strategy={snapshot.strategy!r} on {snapshot.date.date()}"
    )
    declared = set(snapshot.candidates)
    if not declared:
        raise IncompleteSnapshotError(
            f"{label} declares no candidates, so snapshot completeness is undeclared and "
            f"cannot be established ({snapshot.n_symbols} score(s), no declared candidate "
            "universe). Without it the monitor cannot tell a full cross-section from one "
            "truncated by the selection, and IC on the selected names only is structurally "
            "inflated (AT3 condition 2). Re-record it with candidates=<the full evaluation "
            "universe, including the names that were not selected>."
        )

    held = set(snapshot.scores)
    absent = sorted(declared - held)
    if absent:
        raise IncompleteSnapshotError(
            f"{label} is selection-truncated: its scores cover {len(held)} of "
            f"{len(declared)} declared candidates (absent e.g. {absent[:5]}). A "
            "cross-section truncated by the selection itself ranks only the winners "
            "against each other, so its IC/IC-IR/half-life are structurally inflated "
            "(AT3 condition 2); the snapshot is rejected instead of being scored."
        )

    outside = sorted(held - declared)
    raise IncompleteSnapshotError(
        f"{label} holds {len(outside)} symbol(s) outside its declared candidate universe "
        f"(e.g. {outside[:5]}), so completeness cannot be established: the declared "
        "universe must match the scored symbols exactly (AT3 condition 2)."
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _normalise_date(value: Any) -> pd.Timestamp:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise MonitorInputError(f"date is not a valid timestamp: {value!r}") from exc
    if ts is pd.NaT or pd.isna(ts):
        raise MonitorInputError(f"date is missing: {value!r}")
    if ts.tzinfo is not None:
        raise MonitorInputError(f"date must be tz-naive session date, got tz-aware {value!r}")
    return ts.normalize()


def _as_close_panel(close: pd.DataFrame) -> pd.DataFrame:
    """Validate the price panel: DatetimeIndex, finite-or-NaN numeric columns."""
    if not isinstance(close, pd.DataFrame):
        raise MonitorInputError(f"close must be a pandas.DataFrame, got {type(close).__name__}")
    if not isinstance(close.index, pd.DatetimeIndex):
        raise MonitorInputError("close index must be a DatetimeIndex (session dates)")
    if close.index.tz is not None:
        raise MonitorInputError("close index must be tz-naive session dates")
    if close.empty:
        raise MonitorInputError("close panel is empty")
    panel = close.copy()
    panel.index = panel.index.normalize()
    if panel.index.has_duplicates:
        raise MonitorInputError("close index has duplicate session dates")
    panel = panel.sort_index()
    panel.columns = [str(c) for c in panel.columns]
    try:
        panel = panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise MonitorInputError(f"close panel must be numeric: {exc}") from exc
    if not np.isfinite(panel.to_numpy(dtype=float)).any():
        raise MonitorInputError("close panel holds no finite price")
    return panel


def _bar_shift(index: pd.DatetimeIndex, anchor: pd.Timestamp, bars: int) -> Optional[pd.Timestamp]:
    """The session `bars` trading days after `anchor` in `index`, or None.

    `index` is the close panel's own index, so the horizon arithmetic follows the
    sessions that actually have prices -- no second holiday table is introduced.
    An `anchor` that is not an exact session resolves to the first session at or
    after it; the shift never extrapolates backwards past available data.
    """
    pos = int(index.searchsorted(anchor, side="left"))
    target = pos + int(bars)
    if target >= len(index):
        return None
    return pd.Timestamp(index[target])


# ---------------------------------------------------------------------------
# report objects
# ---------------------------------------------------------------------------
def _opt_float(value: Any) -> Optional[float]:
    """Non-finite metrics become `None` (explicit "unmeasured"), never 0.0."""
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True, eq=False)
class LegICReport:
    """Advisory IC/ICIR/half-life report for one `(market, universe_id, strategy)` leg."""

    market: str
    universe_id: str
    strategy: str
    status: str
    horizon_bars: int
    return_basis: str
    min_cross_section: int
    min_valid_eval_days: int
    n_snapshot_dates: int
    n_valid_eval_dates: int
    n_dates_below_cross_section_floor: int
    n_dates_degenerate_cross_section: int
    n_dates_missing_returns: int
    mean_ic: float
    std_ic: float
    ic_ir: float
    half_life: float
    t_stat: float
    p_value: float
    first_conclusion_available: Optional[pd.Timestamp]
    ic_series: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    notes: Tuple[str, ...] = ()

    @property
    def key(self) -> LegKey:
        return (self.market, self.universe_id, self.strategy)

    @property
    def advisory(self) -> bool:
        return True

    @property
    def conclusion_ready(self) -> bool:
        return self.first_conclusion_available is not None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe view (the `ic_series` becomes `[[iso_date, ic], ...]`)."""
        return {
            "market": self.market,
            "universe_id": self.universe_id,
            "strategy": self.strategy,
            "status": self.status,
            "advisory": True,
            "horizon_bars": int(self.horizon_bars),
            "return_basis": self.return_basis,
            "min_cross_section": int(self.min_cross_section),
            "min_valid_eval_days": int(self.min_valid_eval_days),
            "n_snapshot_dates": int(self.n_snapshot_dates),
            "n_valid_eval_dates": int(self.n_valid_eval_dates),
            "n_dates_below_cross_section_floor": int(self.n_dates_below_cross_section_floor),
            "n_dates_degenerate_cross_section": int(self.n_dates_degenerate_cross_section),
            "n_dates_missing_returns": int(self.n_dates_missing_returns),
            "mean_ic": _opt_float(self.mean_ic),
            "std_ic": _opt_float(self.std_ic),
            "ic_ir": _opt_float(self.ic_ir),
            "half_life": _opt_float(self.half_life),
            "t_stat": _opt_float(self.t_stat),
            "p_value": _opt_float(self.p_value),
            "first_conclusion_available": (
                None
                if self.first_conclusion_available is None
                else self.first_conclusion_available.strftime("%Y-%m-%d")
            ),
            "ic_series": [
                [idx.strftime("%Y-%m-%d"), float(val)] for idx, val in self.ic_series.items()
            ],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, eq=False)
class ICDecayReport:
    """Container for every leg's advisory IC decay report."""

    legs: Tuple[LegICReport, ...]
    horizon_bars: int
    return_basis: str
    min_cross_section: int
    min_valid_eval_days: int
    start_date: Optional[pd.Timestamp] = None

    @property
    def advisory(self) -> bool:
        return True

    @property
    def statuses(self) -> Dict[LegKey, str]:
        return {leg.key: leg.status for leg in self.legs}

    def leg(self, market: str, universe_id: str, strategy: str) -> LegICReport:
        for candidate in self.legs:
            if candidate.key == (market, universe_id, strategy):
                return candidate
        raise KeyError(f"no leg report for {(market, universe_id, strategy)}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advisory": True,
            "horizon_bars": int(self.horizon_bars),
            "return_basis": self.return_basis,
            "min_cross_section": int(self.min_cross_section),
            "min_valid_eval_days": int(self.min_valid_eval_days),
            "start_date": None if self.start_date is None else self.start_date.strftime("%Y-%m-%d"),
            "legs": [leg.to_dict() for leg in self.legs],
        }


# ---------------------------------------------------------------------------
# the monitor
# ---------------------------------------------------------------------------
class ICDecayMonitor:
    """Accumulates snapshots under the no-backfill rules and evaluates each leg.

    Ingestion demands **complete** snapshots (declared candidate universe, fully
    covered) and rejects anything else with `IncompleteSnapshotError` before it is
    stored -- see "COMPLETENESS IS MANDATORY" in the module docstring. Snapshot
    completeness is not caller-optional here.

    Parameters
    ----------
    close : pandas.DataFrame
        Close prices, `DatetimeIndex` (session dates) x symbol. Its index **is** the
        trading calendar used for the horizon arithmetic -- no second holiday table
        is introduced.
    start_date : optional
        First session this monitor may accept. A snapshot dated earlier raises
        `BackfilledSnapshotError` (`AT3` condition 5).
    horizon_bars : int
        Forward-return horizon in trading bars. Defaults to the frozen
        `FROZEN_HORIZON_BARS`; any override is echoed in the report.
    min_cross_section : int
        Per-day floor (default `MIN_CROSS_SECTION_ASSETS = 15`).
    min_valid_eval_days : int
        Valid evaluation days needed before `status` can be `OK` (default 10).
    market_map : optional mapping
        Explicit symbol -> market overrides for names
        `infer_market_from_symbol` cannot classify. Never guessed: an unresolvable
        symbol raises.
    max_recording_lag_days : int
        Calendar-day tolerance between a snapshot's session and its `recorded_at`.
    """

    def __init__(
        self,
        close: pd.DataFrame,
        *,
        start_date: Any = None,
        horizon_bars: int = FROZEN_HORIZON_BARS,
        min_cross_section: int = MIN_CROSS_SECTION_ASSETS,
        min_valid_eval_days: int = MIN_VALID_EVAL_DAYS,
        market_map: Optional[Mapping[str, str]] = None,
        max_recording_lag_days: int = MAX_RECORDING_LAG_DAYS,
    ) -> None:
        if int(horizon_bars) < 1:
            raise MonitorInputError(f"horizon_bars must be >= 1, got {horizon_bars!r}")
        if int(min_cross_section) < 3:
            raise MonitorInputError(
                "min_cross_section must be >= 3 (a Spearman rank correlation over fewer "
                f"names is not estimable), got {min_cross_section!r}"
            )
        if int(min_valid_eval_days) < 1:
            raise MonitorInputError(
                f"min_valid_eval_days must be >= 1, got {min_valid_eval_days!r}"
            )
        if int(max_recording_lag_days) < 0:
            raise MonitorInputError(
                f"max_recording_lag_days must be >= 0, got {max_recording_lag_days!r}"
            )
        self._close = _as_close_panel(close)
        self.horizon_bars = int(horizon_bars)
        self.min_cross_section = int(min_cross_section)
        self.min_valid_eval_days = int(min_valid_eval_days)
        self.max_recording_lag_days = int(max_recording_lag_days)
        self.start_date = None if start_date is None else _normalise_date(start_date)
        self._market_map = {str(k): str(v) for k, v in (market_map or {}).items()}
        self._snapshots: Dict[Tuple[pd.Timestamp, str, str], SignalSnapshot] = {}

    # -- ingestion ---------------------------------------------------------
    def add_snapshot(self, snapshot: SignalSnapshot) -> SignalSnapshot:
        """Register one snapshot, enforcing completeness + information set + no-backfill.

        Completeness (`AT3` condition 2) is checked **first** and unconditionally: a
        snapshot that cannot prove it covers its whole declared candidate universe is
        never stored, so no later code path can score a selection-truncated
        cross-section. See the module docstring.
        """
        _require_complete_snapshot(snapshot)
        if snapshot.information_set != CLOSE_INFORMATION_SET:
            raise IntradayInformationSetError(
                f"snapshot for {snapshot.date.date()} declares information_set="
                f"{snapshot.information_set!r}. Only close-anchored scores may be scored "
                "against close-to-close returns: a --cutoff-time 14:15 snapshot is an "
                "intraday information set and mixing the two manufactures look-ahead."
            )
        if self.start_date is not None and snapshot.date < self.start_date:
            raise BackfilledSnapshotError(
                f"snapshot dated {snapshot.date.date()} precedes the monitor start "
                f"{self.start_date.date()}; history is not recomputable (qfq revisions, "
                "config/dump drift), so a late snapshot cannot be back-filled from today's data"
            )
        if snapshot.recorded_at is not None:
            lag_days = int((snapshot.recorded_at.normalize() - snapshot.date).days)
            if lag_days > self.max_recording_lag_days:
                raise BackfilledSnapshotError(
                    f"snapshot for {snapshot.date.date()} was recorded at "
                    f"{snapshot.recorded_at} ({lag_days} days later, tolerance "
                    f"{self.max_recording_lag_days}); that is a reconstruction from "
                    "post-hoc data, not a live snapshot"
                )
        key = (snapshot.date, snapshot.universe_id, snapshot.strategy)
        if key in self._snapshots:
            raise MonitorInputError(
                f"duplicate snapshot for {snapshot.date.date()} "
                f"universe_id={snapshot.universe_id!r} strategy={snapshot.strategy!r}; "
                "two snapshots for one cell would make the cross-section ambiguous"
            )
        self._snapshots[key] = snapshot
        return snapshot

    def add_snapshots(self, snapshots: Iterable[SignalSnapshot]) -> int:
        """Register many snapshots, rejecting the batch if any member is incomplete.

        Completeness is validated for the **whole batch before any member is stored**,
        so a batch containing one undeclared/truncated snapshot leaves the monitor
        holding nothing rather than a cross-section that is half ingested. The
        remaining per-snapshot rules (information set, backfill, duplicates) stay in
        `add_snapshot`, where they also apply to single-snapshot ingestion.
        """
        pending = list(snapshots)
        for snapshot in pending:
            _require_complete_snapshot(snapshot)
        count = 0
        for snapshot in pending:
            self.add_snapshot(snapshot)
            count += 1
        return count

    @property
    def snapshots(self) -> Tuple[SignalSnapshot, ...]:
        return tuple(self._snapshots[k] for k in sorted(self._snapshots))

    # -- market resolution -------------------------------------------------
    def market_of(self, symbol: str) -> str:
        if symbol in self._market_map:
            return self._market_map[symbol]
        try:
            return infer_market_from_symbol(symbol)
        except UnknownSymbolMarketError as exc:
            raise MonitorInputError(
                f"cannot infer market for {symbol!r} ({exc}); pass market_map="
                f"{{{symbol!r}: 'china'|'hk'|'us'}} instead of letting the monitor guess"
            ) from exc

    # -- evaluation --------------------------------------------------------
    def evaluate(self) -> ICDecayReport:
        """Compute every leg's advisory diagnostics. Pure: no mutation, no IO."""
        grouped: Dict[LegKey, List[SignalSnapshot]] = {}
        for snapshot in self.snapshots:
            key: LegKey = (self.market_of_snapshot(snapshot), snapshot.universe_id, snapshot.strategy)
            grouped.setdefault(key, []).append(snapshot)
        legs = tuple(
            self._evaluate_leg(key, sorted(grouped[key], key=lambda s: s.date))
            for key in sorted(grouped)
        )
        return ICDecayReport(
            legs=legs,
            horizon_bars=self.horizon_bars,
            return_basis=FROZEN_RETURN_BASIS,
            min_cross_section=self.min_cross_section,
            min_valid_eval_days=self.min_valid_eval_days,
            start_date=self.start_date,
        )

    def market_of_snapshot(self, snapshot: SignalSnapshot) -> str:
        markets = {self.market_of(symbol) for symbol in snapshot.symbols}
        if len(markets) > 1:
            raise MonitorInputError(
                f"snapshot for universe_id={snapshot.universe_id!r} "
                f"strategy={snapshot.strategy!r} on {snapshot.date.date()} mixes markets "
                f"{sorted(markets)}; AT3 requires per-market evaluation, and a merged "
                "cross-section would rank names against the wrong session clock"
            )
        return next(iter(markets)) if markets else "unknown"

    def _evaluate_leg(
        self, key: LegKey, snapshots: Sequence[SignalSnapshot]
    ) -> LegICReport:
        market, universe_id, strategy = key
        dates = [s.date for s in snapshots]
        symbols = sorted({sym for s in snapshots for sym in s.symbols})

        # Panel: absent symbol on a date stays NaN. It is NEVER 0.0 -- a zero would
        # claim "this candidate scored neutral", which is a fabricated observation.
        panel = pd.DataFrame(np.nan, index=pd.DatetimeIndex(dates), columns=symbols, dtype=float)
        for snapshot in snapshots:
            for symbol, score in snapshot.scores.items():
                panel.at[snapshot.date, symbol] = float(score)

        notes: List[str] = [
            "advisory only: this report never gates, reweights or sizes a position",
            f"return basis frozen to {FROZEN_RETURN_BASIS} on the {FROZEN_PRICE_FIELD!r} "
            f"field, horizon {self.horizon_bars} trading bars",
        ]

        # A symbol with no price column at all stays an all-NaN column (never a 0.0
        # price, never a dropped symbol): it simply fails the per-day floor count,
        # which is the honest reading of "we have no return basis for it".
        missing_price = [symbol for symbol in symbols if symbol not in self._close.columns]
        if missing_price:
            notes.append(
                f"{len(missing_price)} snapshot symbol(s) have no column in the price "
                f"panel (e.g. {missing_price[:5]}) and therefore no forward return"
            )
        close_sub = self._close.reindex(columns=symbols)

        # Forward returns: delegated, close-to-close, same horizon for every leg.
        forward = _fa.compute_forward_returns(close_sub, periods=self.horizon_bars)
        forward = forward.reindex(index=panel.index, columns=symbols)

        valid_dates: List[pd.Timestamp] = []
        n_below_floor = 0
        n_degenerate = 0
        n_missing_returns = 0
        for dt in panel.index:
            scores_row = panel.loc[dt]
            returns_row = forward.loc[dt]
            both = scores_row.notna() & returns_row.notna()
            n_valid = int(both.sum())
            if n_valid == 0:
                # Either no price at t+h yet (the horizon has not matured) or no
                # price at t at all. Both mean "not measurable", not "IC = 0".
                n_missing_returns += 1
                continue
            if n_valid < self.min_cross_section:
                n_below_floor += 1
                continue
            if scores_row[both].nunique() < 2 or returns_row[both].nunique() < 2:
                # A constant cross-section has no rank correlation; factor_analyzer
                # would report 0.0 for it, which is a manufactured value. Exclude it
                # and count it instead.
                n_degenerate += 1
                continue
            valid_dates.append(pd.Timestamp(dt))

        if n_below_floor:
            notes.append(
                f"{n_below_floor} snapshot date(s) had fewer than "
                f"{self.min_cross_section} usable symbols and were excluded, not zero-filled"
            )
        if n_degenerate:
            notes.append(
                f"{n_degenerate} snapshot date(s) had a constant score/return "
                "cross-section (undefined rank correlation) and were excluded"
            )
        if n_missing_returns:
            notes.append(
                f"{n_missing_returns} snapshot date(s) had no matured {self.horizon_bars}-bar "
                "forward return yet and were excluded"
            )

        if not valid_dates:
            return LegICReport(
                market=market,
                universe_id=universe_id,
                strategy=strategy,
                status=STATUS_INSUFFICIENT_HISTORY,
                horizon_bars=self.horizon_bars,
                return_basis=FROZEN_RETURN_BASIS,
                min_cross_section=self.min_cross_section,
                min_valid_eval_days=self.min_valid_eval_days,
                n_snapshot_dates=len(dates),
                n_valid_eval_dates=0,
                n_dates_below_cross_section_floor=n_below_floor,
                n_dates_degenerate_cross_section=n_degenerate,
                n_dates_missing_returns=n_missing_returns,
                mean_ic=float("nan"),
                std_ic=float("nan"),
                ic_ir=float("nan"),
                half_life=float("nan"),
                t_stat=float("nan"),
                p_value=float("nan"),
                first_conclusion_available=None,
                ic_series=pd.Series(dtype=float),
                notes=tuple(notes + ["no valid evaluation day: IC is unmeasured, not zero"]),
            )

        panel_valid = panel.loc[valid_dates]
        forward_valid = forward.loc[valid_dates]
        ic_series = _fa.compute_rank_ic(
            panel_valid, forward_valid, min_assets=self.min_cross_section
        )
        summary = _fa.factor_ic_summary(
            panel_valid, forward_valid, min_assets=self.min_cross_section
        )
        ic_series = ic_series.dropna()

        n_valid = len(valid_dates)
        if len(ic_series) != n_valid:  # pragma: no cover - defensive, primitives agree today
            notes.append(
                f"factor_analyzer returned {len(ic_series)} IC observation(s) for "
                f"{n_valid} pre-screened valid day(s); the counts differ, so a date was "
                "rejected inside the primitive as well"
            )
        status = STATUS_OK if n_valid >= self.min_valid_eval_days else STATUS_INSUFFICIENT_HISTORY
        if status != STATUS_OK:
            notes.append(
                f"only {n_valid} valid evaluation day(s) < {self.min_valid_eval_days}; "
                "the metrics below are partial and are NOT a conclusion"
            )
        else:
            notes.append(
                f"{n_valid} valid evaluation day(s); earliest defensible conclusion after "
                f"{self.min_valid_eval_days} valid days + {self.horizon_bars}-bar horizon"
            )

        conclusion_date = self._first_conclusion_date(valid_dates)
        if conclusion_date is None and status == STATUS_OK:
            notes.append(
                "first conclusion date not yet reachable in the supplied price panel"
            )

        return LegICReport(
            market=market,
            universe_id=universe_id,
            strategy=strategy,
            status=status,
            horizon_bars=self.horizon_bars,
            return_basis=FROZEN_RETURN_BASIS,
            min_cross_section=self.min_cross_section,
            min_valid_eval_days=self.min_valid_eval_days,
            n_snapshot_dates=len(dates),
            n_valid_eval_dates=n_valid,
            n_dates_below_cross_section_floor=n_below_floor,
            n_dates_degenerate_cross_section=n_degenerate,
            n_dates_missing_returns=n_missing_returns,
            mean_ic=float(summary.get("mean_ic", float("nan"))),
            std_ic=float(summary.get("std_ic", float("nan"))),
            ic_ir=float(summary.get("ic_ir", float("nan"))),
            half_life=float(summary.get("half_life", float("nan"))),
            t_stat=float(summary.get("t_stat", float("nan"))),
            p_value=float(summary.get("p_value", float("nan"))),
            first_conclusion_available=conclusion_date,
            ic_series=ic_series,
            notes=tuple(notes),
        )

    def _first_conclusion_date(self, valid_dates: Sequence[pd.Timestamp]) -> Optional[pd.Timestamp]:
        """`10th valid evaluation day + horizon bars`, or None while unreachable."""
        if len(valid_dates) < self.min_valid_eval_days:
            return None
        anchor = pd.Timestamp(valid_dates[self.min_valid_eval_days - 1])
        return _bar_shift(self._close.index, anchor, self.horizon_bars)


def evaluate_ic_decay(
    snapshots: Iterable[SignalSnapshot],
    close: pd.DataFrame,
    *,
    start_date: Any = None,
    horizon_bars: int = FROZEN_HORIZON_BARS,
    min_cross_section: int = MIN_CROSS_SECTION_ASSETS,
    min_valid_eval_days: int = MIN_VALID_EVAL_DAYS,
    market_map: Optional[Mapping[str, str]] = None,
) -> ICDecayReport:
    """One-shot convenience wrapper: build a monitor, ingest, evaluate.

    Ingestion enforces snapshot completeness: a snapshot whose `candidates` are
    undeclared or not fully covered raises `IncompleteSnapshotError` here, before
    any IC math runs (see `ICDecayMonitor.add_snapshot`).
    """
    monitor = ICDecayMonitor(
        close,
        start_date=start_date,
        horizon_bars=horizon_bars,
        min_cross_section=min_cross_section,
        min_valid_eval_days=min_valid_eval_days,
        market_map=market_map,
    )
    monitor.add_snapshots(snapshots)
    return monitor.evaluate()

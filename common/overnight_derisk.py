"""`R6` (rule spec `r6.3`) -- the **overnight de-risking** judgement (pure module, decision only).

FROZEN JUDGEMENT: `docs/overnight_derisk_judgement_freeze.md` (`overnight_derisk/v1`). Read that
document, not this docstring, if the two ever disagree; this module implements it and is not
allowed to re-interpret it. `DECISION_SPEC_VERSION = "r6.3"` is a **judgement spec tag**, NOT a
strategy version -- no `BASE_VERSION` / per-class `version` obligation attaches to a pure module
(AGENTS §7), and none may be invented "for symmetry".

`r6.3` (freeze document `## Revision 2026-11-07b`) changes **one** thing: *which symbols* enter the
cross-sectional mean. The caller must pass the **loaded universe for that market minus the cash
proxies** (`scripts/run_live_strategy.py` does exactly that); `r6.2`'s "today's target book" source
is retired because it made the risk reading depend on the book it de-risks (rotation instability +
a historical-σ look-ahead). Everything else below is unchanged and still frozen.

WHAT THIS IS (positioning before arithmetic)
--------------------------------------------
One question, asked **before the close**: *how much market exposure do we want to carry through
tonight's gap?* The answer is a **market-level multiplier** on the **non-cash-proxy** target
weights (`1.0` unchanged, `rho = 0.5` de-risked) plus the evidence used.

It is explicitly NOT: intraday timing (no path forecast, no long/short flipping inside the
session); per-symbol selection (no ranking or picking inside the rule -- the gap series is a
**cross-sectional equal-weight mean**, a disclosed proxy); or a volatility-targeting overlay (the
`sigma_gap` reading is a dispersion of *overnight gaps*, not a forecast of return volatility, and
it is never merged with a strategy's own vol-target parameters).

WHY `M3.2`'s NEGATIVE RESULT DOES NEITHER REFUTE NOR SUPPORT THIS
-----------------------------------------------------------------
`M3.2` studied **execution-price** bias (does a window VWAP fill deviate from the booked price?);
this rule studies **overnight gap risk** (is de-risking before the close worth it?). Different
dependent variables, so `M3.2` is neither evidence against nor for it. `R6` stands on its own
acceptance suite, and closing it as a **negative result** is explicitly allowed (see the `R6` row
in `docs/development_plan.md` §2.20).

DISCLOSED SOURCE OF THE NUMBERS (no paper is cited because none is being claimed)
---------------------------------------------------------------------------------
``minutes_before_close = 45`` / ``gap_window = 20`` / ``baseline_window = 252`` /
``sigma_multiplier = 2.0`` / ``downscale_ratio = 0.5`` are an **operational risk-budget convention
fixed by the owner** (approved 2026-11-07, restated in the freeze document). They are *not*
sample-optimised, not "calibrated optima", and not quotations from the literature. Do not present
them as research output and do not retune them here: a different budget is a different decision by
the owner and belongs in the freeze document first (append a `## Revision` section, then change the
implementation).

THE JUDGEMENT (nothing hidden)
------------------------------
1. **Decision instant** ``D = session_close_time(market, day) - 45min``, computed through
   :func:`common.market_clock.session_close_time` / ``MarketSpec.close_time_on`` so a **half-day is
   honoured**. China full day 15:00 -> 14:15; US 16:00 -> 15:15 and half-day 13:00 -> 12:15; HK
   16:00 -> 15:15 and half-day 12:00 -> 11:15. A hard-coded `14:15` is **forbidden**: on a US / HK
   half-day it is *after* the close, which makes the decision a function of a price it acts on (the
   look-ahead the freeze document calls out). ``D >= close`` or ``D <= session open`` raises --
   never silently clamp, never silently continue.
2. **Trigger** ``gap_t = (1/N_t) * sum_i( Open_{i,t} / Close_{i,t-1} - 1 )`` over symbols that have
   **both** bars; excluded symbols are **counted, not imputed** (never ``fillna(0)`` -- a missing
   gap is not a flat gap). The symbol set is the caller's responsibility (`r6.3`): **the loaded
   universe for that market minus the cash proxies**, never the target book. An empty set is
   ``INSUFFICIENT_DATA``, never a book fallback. Then ``sigma_gap(t) = std(gap[t-20+1..t], ddof=1)``
   and ``B(t) = median({sigma_gap(s) : the most recent 252 sigma values ending at t})``; trigger is
   ``sigma_gap(t) > 2.0 * B(t)`` (**strict**). Fewer than 20 usable gaps, or fewer than 252 sigma
   values, is ``INSUFFICIENT_DATA`` -- never a shorter window and never "not triggered".
3. **Downscale** ``rho = 0.5`` (construction-time assertion ``0 < rho < 1``), applied **only** to
   non-cash-proxy weights; the cash proxy is untouched and the difference stays in cash. **No
   renormalisation** (that would silently undo the de-risking by scaling the residual back up).
4. **Closed tri-state**: ``TRIGGERED`` / ``NOT_TRIGGERED`` / ``INSUFFICIENT_DATA``.
   ``INSUFFICIENT_DATA`` is **neither** "not triggered" **nor** ``0``: it does not de-risk
   (``ratio = 1.0`` is only "no multiplier was applied"), but it MUST be visible on every ticket
   field and MUST emit a ``WARN``. The owner's 2026-11-07 ruling: default ON, insufficient data
   only warns, it does **not** refuse the ticket (this is deliberately different from the Stage-1
   NAV gate, where missing means refuse -- see the freeze document §5).
5. **Pure**: no clock reads, no I/O, no network, no random, no pandas global state. Same inputs =>
   equal outputs. **One disclosed side effect**: an ``INSUFFICIENT_DATA`` verdict emits a
   ``RuntimeWarning`` (diagnostic only, no state) so "insufficient" can never be mistaken for "not
   triggered" by a caller that forgets to branch on ``status``. The execution-price convention is
   **not** an input, so ``close`` and ``intraday_twap`` cannot produce different decisions by
   construction; the testable form of the look-ahead guard is the ordering assertion
   ``decision_instant < pricing_point`` (see :func:`assert_decision_precedes_pricing`).

BOUNDARIES (do not "helpfully" fold these in)
---------------------------------------------
No 5m dependency (the 5m switch surface is `R7`); no `blend_base.py` / strategy-class / version-axis
change; not the `R1` / `R4` / `R5` judgements (their thresholds, windows and instants are separate
and must never be shared -- see the freeze document §8); no calendar inference (the market clock is
the single owner of sessions and half-days).
"""

from __future__ import annotations

import datetime as _dt
import enum
import math
import numbers
import warnings
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common.market_clock import market_spec, session_close_time

#: Judgement spec tag (`overnight_derisk/v1`, rule `r6.3`). NOT a strategy version.
DECISION_SPEC_VERSION = "r6.3"

#: Frozen defaults (owner-approved 2026-11-07). Placeholder baselines, not "calibrated optima" --
#: re-register in the freeze document before changing any of them.
DEFAULT_MINUTES_BEFORE_CLOSE = 45
DEFAULT_GAP_WINDOW = 20
DEFAULT_BASELINE_WINDOW = 252
DEFAULT_SIGMA_MULTIPLIER = 2.0
DEFAULT_DOWNSCALE_RATIO = 0.5


class OvernightDeriskStatus(str, enum.Enum):
    """Closed tri-state vocabulary (the `.value` strings are the on-disk contract).

    ``INSUFFICIENT_DATA`` is deliberately its own member: folding it into
    ``NOT_TRIGGERED`` is the "missing data reads as a risk conclusion" bug class this repo
    exists to avoid, and mapping it to a ``0.0`` ratio would liquidate the book.
    """

    TRIGGERED = "TRIGGERED"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


STATUSES: Tuple[str, ...] = tuple(s.value for s in OvernightDeriskStatus)

#: Multiplier used when nothing is de-risked: the literal ``1.0``, never ``0.9999`` / missing.
RATIO_UNCHANGED = 1.0


@dataclass(frozen=True)
class OvernightDeriskConfig:
    """Frozen judgement parameters (validated at construction, so a bad budget cannot run).

    ``minutes_before_close`` is validated only for *type* here (an int): a negative value is
    **deliberately not rejected in the config**, because the geometry guard in
    :func:`resolve_decision_instant` is the single owner of "D must be inside the session". That is
    what makes the mutation ``minutes_before_close = -5`` fail at the guard (the intended red state
    for the ordering regression) instead of failing at config construction, where the guard would
    never be exercised.
    """

    minutes_before_close: int = DEFAULT_MINUTES_BEFORE_CLOSE
    gap_window: int = DEFAULT_GAP_WINDOW
    baseline_window: int = DEFAULT_BASELINE_WINDOW
    sigma_multiplier: float = DEFAULT_SIGMA_MULTIPLIER
    downscale_ratio: float = DEFAULT_DOWNSCALE_RATIO
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.minutes_before_close, int) or isinstance(self.minutes_before_close, bool):
            raise ValueError(
                "minutes_before_close must be an int (minutes), got "
                f"{type(self.minutes_before_close).__name__}: {self.minutes_before_close!r}"
            )
        for name in ("gap_window", "baseline_window"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an int, got {type(value).__name__}: {value!r}")
            # `> 1` because `sigma_gap` is a sample standard deviation (ddof=1): a window of 1
            # would divide by zero and a window of 0 is not a window.
            if value <= 1:
                raise ValueError(f"{name} must be > 1, got {value!r}")
        if not isinstance(self.sigma_multiplier, (int, float)) or isinstance(self.sigma_multiplier, bool):
            raise ValueError(
                f"sigma_multiplier must be a number, got {type(self.sigma_multiplier).__name__}"
            )
        if not math.isfinite(float(self.sigma_multiplier)) or float(self.sigma_multiplier) <= 0.0:
            raise ValueError(f"sigma_multiplier must be finite and > 0, got {self.sigma_multiplier!r}")
        if not isinstance(self.downscale_ratio, (int, float)) or isinstance(self.downscale_ratio, bool):
            raise ValueError(
                f"downscale_ratio must be a number, got {type(self.downscale_ratio).__name__}"
            )
        ratio = float(self.downscale_ratio)
        # Hard assertion from the freeze document: 0.0 is liquidation (not "de-risking") and
        # >= 1.0 is leverage (not a downscale). Both are outside this rule's semantics.
        if not math.isfinite(ratio) or not (0.0 < ratio < 1.0):
            raise ValueError(f"downscale_ratio must satisfy 0 < ratio < 1, got {self.downscale_ratio!r}")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {type(self.enabled).__name__}")


# --------------------------------------------------------------------------- #
# Decision instant
# --------------------------------------------------------------------------- #
def _market_tz(market: str) -> ZoneInfo:
    """Market zone (`zoneinfo`; DST is tzdata's job, never this module's)."""
    return ZoneInfo(market_spec(market).tz_name)


def _require_plain_date(session_date: Any) -> _dt.date:
    """Reject a `datetime` (a `date` subclass that never equals a `date`) and non-dates."""
    if not isinstance(session_date, _dt.date) or isinstance(session_date, _dt.datetime):
        raise TypeError(
            "session_date must be a plain datetime.date (not datetime), got "
            f"{type(session_date).__name__}"
        )
    return session_date


def _require_whole_minutes(minutes_before_close: Any) -> int:
    """Return an `int` minute offset, refusing anything that is not a whole number of minutes.

    `OvernightDeriskConfig` already rejects a non-`int`; this closes the same hole on the free
    function, whose previous `int(minutes_before_close)` silently truncated `45.5` to `45`. An
    integral float (`45.0`) is accepted as the value it unambiguously denotes; a non-integral one
    (or a `NaN` / `inf` / `bool` / string) raises rather than being rounded or truncated.
    """
    if isinstance(minutes_before_close, bool):
        raise ValueError(
            f"minutes_before_close must be a whole number of minutes, got bool: {minutes_before_close!r}"
        )
    if isinstance(minutes_before_close, numbers.Integral):
        return int(minutes_before_close)
    if isinstance(minutes_before_close, numbers.Real):
        value = float(minutes_before_close)
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(
                "minutes_before_close must be a whole number of minutes (no truncation, no "
                f"rounding), got {minutes_before_close!r}"
            )
        return int(value)
    raise ValueError(
        "minutes_before_close must be a whole number of minutes, got "
        f"{type(minutes_before_close).__name__}: {minutes_before_close!r}"
    )


def resolve_decision_instant(
    market: str,
    session_date: _dt.date,
    minutes_before_close: int = DEFAULT_MINUTES_BEFORE_CLOSE,
) -> _dt.datetime:
    """`D = session_close_time(market, day) - minutes_before_close`, tz-aware market-local.

    The close comes from :func:`common.market_clock.session_close_time`, i.e. from
    `MarketSpec.close_time_on`, so **a half-day moves `D` earlier** (US 13:00 -> 12:15, HK
    12:00 -> 11:15) instead of producing a decision instant after the session has ended.

    Raises
    ------
    TypeError
        `session_date` is not a plain `datetime.date`.
    ValueError
        `minutes_before_close` is not a whole number of minutes (a float such as `45.5` is
        **refused**, never truncated -- silently reading `45.5` as `45` would decide at an instant
        the caller never asked for, which is exactly the kind of quiet drift the geometry guard
        exists to prevent), or the resulting `D` is `>= close` / `<= session open` -- the geometry
        is impossible, so the caller's numbers are wrong (e.g. a negative `minutes_before_close`).
        Fail loud: on a half-day the naive "14:15" answer is a look-ahead, and clamping it to the
        close would silently decide at the very instant the price is taken.
    """
    day = _require_plain_date(session_date)
    minutes = _require_whole_minutes(minutes_before_close)
    spec = market_spec(market)
    tz = _market_tz(market)
    close_at = _dt.datetime.combine(day, session_close_time(market, day), tzinfo=tz)
    open_at = _dt.datetime.combine(day, spec.morning_open, tzinfo=tz)
    decision = close_at - _dt.timedelta(minutes=minutes)
    if decision >= close_at:
        raise ValueError(
            f"overnight-derisk decision instant {decision.isoformat()} is at/after the "
            f"{market} close {close_at.isoformat()} (minutes_before_close="
            f"{minutes_before_close}). A decision at or after the price-taking moment is a "
            "look-ahead; refusing to continue."
        )
    if decision <= open_at:
        raise ValueError(
            f"overnight-derisk decision instant {decision.isoformat()} is at/before the "
            f"{market} session open {open_at.isoformat()} (minutes_before_close="
            f"{minutes_before_close}); the judgement must run inside the session."
        )
    return decision


def assert_decision_precedes_pricing(
    decision_instant: _dt.datetime,
    pricing_point: _dt.datetime,
) -> None:
    """Hard structural guard: the decision must be stamped **strictly before** the price is taken.

    `pricing_point` is the `close` convention's close instant, or the last 5m bar END of the
    session for `intraday_twap` (both <= the close). Violation raises `ValueError` -- this is the
    "execution-price look-ahead" the freeze document §6 freezes: if the decision could see a price
    that only exists afterwards, the same rule would be two different strategies under `close` and
    `intraday_twap`.
    """
    if decision_instant.tzinfo is None or pricing_point.tzinfo is None:
        raise ValueError("both instants must be timezone-aware market-local datetimes")
    if not decision_instant < pricing_point:
        raise ValueError(
            f"overnight-derisk decision instant {decision_instant.isoformat()} is not strictly "
            f"before the pricing point {pricing_point.isoformat()}: the judgement would be a "
            "function of the price it acts on."
        )


def session_pricing_point(market: str, session_date: _dt.date, execution_price: str = "close") -> _dt.datetime:
    """Market-local instant at which `execution_price` takes its price (for the ordering guard).

    Only the two conventions `R6` is judged against are modelled (`close` -> the session close,
    `intraday_twap` -> the session's last 5m bar END = close, since the TWAP slices the whole
    session). `next_open` / `next_vwap` price *after* the close, so they are trivially later and
    need no special case here. Any other value raises rather than guessing.
    """
    day = _require_plain_date(session_date)
    if execution_price not in ("close", "intraday_twap"):
        raise ValueError(
            "execution_price must be 'close' or 'intraday_twap' for the overnight-derisk ordering "
            f"guard, got {execution_price!r}"
        )
    return _dt.datetime.combine(day, session_close_time(market, day), tzinfo=_market_tz(market))


# --------------------------------------------------------------------------- #
# Gap series
# --------------------------------------------------------------------------- #
def _frame_on_panel(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """`Open`/`Close` columns of one symbol on a **date-normalised** index (or `None`)."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    missing = [c for c in ("Open", "Close") if c not in df.columns]
    if missing:
        return None
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(idx))
        except Exception:
            return None
    if idx.tz is not None:
        # Daily bar labels are market-local wall-clock midnights; dropping the tz keeps the
        # calendar date intact (`tz_convert(None)` would shift it through UTC and can move a
        # 00:00+08:00 label to the previous day).
        idx = idx.tz_localize(None)
    out = df.loc[:, ["Open", "Close"]].copy()
    out.index = pd.DatetimeIndex(idx).normalize()
    # Duplicate sessions have no defensible "the" open/close: keep the last row, deterministically.
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def compute_gap_series(
    universe: Mapping[str, pd.DataFrame],
    as_of_date: Any,
) -> Tuple[pd.Series, pd.Series]:
    """Cross-sectional equal-weight overnight gap, plus the per-day symbol count.

    ``gap_t = mean_i( Open_{i,t} / Close_{i,t-1} - 1 )`` where ``t-1`` is the **previous session on
    the panel calendar** (the union of every supplied symbol's session labels up to
    ``as_of_date``), not merely "the previous row of that symbol's frame". Requiring an actual
    adjacent session is what makes the "two adjacent bars" contract enforceable instead of
    accidentally measuring a multi-day return across a suspension.

    A symbol contributes on ``t`` only when **both** ``Open_{i,t}`` and ``Close_{i,t-1}`` are
    present and finite. Anything else is **excluded and counted** (the returned `symbols_used`
    series), never imputed: ``close.shift(1)`` + ``reindex`` yields `NaN` for a missing prior
    session and no ``fillna`` is applied anywhere. A flat open is a real ``0.0`` gap; "no data" is
    not, and conflating them would let absence read as "no gap" downstream.

    Returns
    -------
    (gap, symbols_used)
        Both indexed by session date (ascending, `<= as_of_date`). `gap` is `NaN` on days with
        `N_t == 0`; `symbols_used` is an `int` count per day (0 where nothing was usable).
        A non-positive prior close is treated as unusable (the ratio is undefined / sign-flipped),
        so it is excluded rather than turned into an `inf`.
    """
    cutoff = pd.Timestamp(as_of_date)
    if isinstance(cutoff, pd.Timestamp) and cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    cutoff = cutoff.normalize()

    frames: Dict[str, pd.DataFrame] = {}
    for sym, df in (universe or {}).items():
        frame = _frame_on_panel(df)
        if frame is None:
            continue
        frame = frame.loc[frame.index <= cutoff]
        if frame.empty:
            continue
        frames[str(sym)] = frame

    if not frames:
        empty_idx = pd.DatetimeIndex([], name="date")
        return (
            pd.Series(dtype="float64", index=empty_idx, name="gap"),
            pd.Series(dtype="int64", index=empty_idx, name="symbols_used"),
        )

    panel = frames[next(iter(frames))].index
    for frame in frames.values():
        panel = panel.union(frame.index)
    panel = panel.sort_values()

    rows: Dict[str, pd.Series] = {}
    for sym, frame in frames.items():
        aligned = frame.reindex(panel)
        opens = pd.to_numeric(aligned["Open"], errors="coerce")
        closes = pd.to_numeric(aligned["Close"], errors="coerce")
        prev_close = closes.shift(1)
        usable = prev_close.notna() & (prev_close > 0) & opens.notna()
        ratio = (opens / prev_close - 1.0).replace([np.inf, -np.inf], np.nan)
        rows[sym] = ratio.where(usable)

    matrix = pd.DataFrame(rows, index=panel)
    valid = matrix.notna()
    symbols_used = valid.sum(axis=1).astype("int64")
    gap = matrix.mean(axis=1)  # skipna=True; a day with N_t == 0 stays NaN
    gap = gap.where(symbols_used > 0)
    gap.name = "gap"
    symbols_used.name = "symbols_used"
    return gap, symbols_used


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OvernightDeriskDecision:
    """One frozen judgement: status + evidence + the ratio the caller must apply."""

    status: OvernightDeriskStatus
    market: str
    session_date: _dt.date
    decision_instant: _dt.datetime
    ratio: float
    sigma_gap: Optional[float]
    baseline_median: Optional[float]
    sessions_used: int
    symbols_used: int
    reason: str

    def __post_init__(self) -> None:
        # Invariants that must survive even a hand-built instance: the tri-state owns the ratio,
        # and INSUFFICIENT_DATA may never carry a de-risking multiplier.
        if not isinstance(self.status, OvernightDeriskStatus):
            raise ValueError(f"status must be OvernightDeriskStatus, got {self.status!r}")
        if self.status is OvernightDeriskStatus.TRIGGERED:
            if not (0.0 < float(self.ratio) < 1.0):
                raise ValueError(f"TRIGGERED must carry a ratio in (0, 1), got {self.ratio!r}")
            if self.sigma_gap is None or self.baseline_median is None:
                raise ValueError("TRIGGERED must carry sigma_gap and baseline_median")
        else:
            if float(self.ratio) != RATIO_UNCHANGED:
                raise ValueError(
                    f"{self.status.value} must carry ratio == 1.0 (no multiplier applied), "
                    f"got {self.ratio!r}"
                )
        if self.status is OvernightDeriskStatus.NOT_TRIGGERED:
            if self.sigma_gap is None or self.baseline_median is None:
                raise ValueError("NOT_TRIGGERED must carry sigma_gap and baseline_median")
        if self.decision_instant.tzinfo is None:
            raise ValueError("decision_instant must be timezone-aware (market-local)")

    @property
    def de_risked(self) -> bool:
        return self.status is OvernightDeriskStatus.TRIGGERED

    def ticket_block(self, config: Optional[OvernightDeriskConfig] = None) -> Dict[str, Any]:
        """JSON-safe block for `detail_reason["overnight_derisk"]` / `summary.json` (§9).

        `ratio_applied` alone is **not** sufficient to read the outcome -- `1.0` covers both
        `NOT_TRIGGERED` and `INSUFFICIENT_DATA` -- so `status` and `reason` are always present.
        """
        cfg = config or OvernightDeriskConfig()
        return {
            "spec_version": DECISION_SPEC_VERSION,
            "status": self.status.value,
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "decision_instant": self.decision_instant.isoformat(),
            "sigma_gap": None if self.sigma_gap is None else float(self.sigma_gap),
            "baseline_median": None if self.baseline_median is None else float(self.baseline_median),
            "sigma_multiplier": float(cfg.sigma_multiplier),
            "ratio_applied": float(self.ratio),
            "sessions_used": int(self.sessions_used),
            "symbols_used": int(self.symbols_used),
            "reason": self.reason,
        }


def _tail_valid(gap: pd.Series, cutoff: pd.Timestamp) -> pd.Series:
    """The usable gap observations up to and including `cutoff` (order preserved, no filling)."""
    if gap is None:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(gap, errors="coerce") if not gap.empty else gap.astype("float64")
    if isinstance(series.index, pd.DatetimeIndex):
        cut = pd.Timestamp(cutoff)
        if cut.tzinfo is not None:
            cut = cut.tz_localize(None)
        series = series.loc[series.index.normalize() <= cut.normalize()]
    return series.dropna()


def evaluate_overnight_derisk(
    gap: pd.Series,
    symbols_used: pd.Series,
    market: str,
    session_date: _dt.date,
    config: Optional[OvernightDeriskConfig] = None,
) -> OvernightDeriskDecision:
    """Evaluate the frozen judgement on a gap series (see the module docstring for the rule).

    `gap` / `symbols_used` come from :func:`compute_gap_series`; `session_date` is the session
    whose close `D` is derived from (normally the run's `as_of_date`, i.e. a session that has
    already closed). `gap` may extend past `session_date` -- it is truncated, never trusted.
    """
    cfg = config or OvernightDeriskConfig()
    day = _require_plain_date(session_date)
    decision_instant = resolve_decision_instant(market, day, cfg.minutes_before_close)
    assert_decision_precedes_pricing(decision_instant, session_pricing_point(market, day, "close"))

    cutoff = pd.Timestamp(day)
    valid_gaps = _tail_valid(gap, cutoff)

    # `symbols_used` at the session, for the evidence field. It is read, never used to impute a
    # gap: a day with zero usable symbols already holds NaN in `gap`.
    n_symbols = 0
    try:
        if symbols_used is not None and len(symbols_used) > 0:
            idx = pd.to_datetime(symbols_used.index)
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_localize(None)
            hits = idx.normalize() == cutoff.normalize()
            if hits.any():
                n_symbols = int(symbols_used.iloc[int(hits.argmax())])
    except Exception:  # pragma: no cover - defensive: evidence only, never the verdict
        n_symbols = 0

    sessions_used = int(valid_gaps.shape[0])
    w = int(cfg.gap_window)
    b_window = int(cfg.baseline_window)

    def _insufficient(sigma: Optional[float], baseline: Optional[float], reason: str) -> OvernightDeriskDecision:
        """Build the INSUFFICIENT_DATA decision and emit the mandated `WARN` (freeze §5).

        The warning is the one non-pure side effect this module allows itself: a diagnostic only,
        no state, no I/O. It exists so "insufficient data" cannot be mistaken for "not triggered"
        by a caller that forgets to branch on `status` -- and so `pytest.warns` can pin it.
        """
        decision = OvernightDeriskDecision(
            status=OvernightDeriskStatus.INSUFFICIENT_DATA,
            market=str(market),
            session_date=day,
            decision_instant=decision_instant,
            ratio=RATIO_UNCHANGED,
            sigma_gap=sigma,
            baseline_median=baseline,
            sessions_used=sessions_used,
            symbols_used=n_symbols,
            reason=reason,
        )
        warnings.warn(f"overnight_derisk[{market}] {reason}", RuntimeWarning, stacklevel=2)
        return decision

    if sessions_used < w:
        return _insufficient(
            None,
            None,
            f"INSUFFICIENT_DATA: 有效隔夜跳空交易日仅 {sessions_used} 个，不足冻结窗口 W={w}，"
            "无法计算 σ_gap；不降暴露（这不是「未触发」）",
        )

    sigma_series = valid_gaps.rolling(window=w, min_periods=w).std(ddof=1).dropna()
    sigma_gap = float(sigma_series.iloc[-1])
    if sigma_series.shape[0] < b_window:
        return _insufficient(
            sigma_gap,
            None,
            f"INSUFFICIENT_DATA: 可得的 σ_gap 读数仅 {sigma_series.shape[0]} 个，不足基线窗口 "
            f"B={b_window}（需 {b_window + w - 1} 个有效 gap 日）；不降暴露（这不是「未触发」）",
        )

    baseline = float(sigma_series.iloc[-b_window:].median())
    threshold = float(cfg.sigma_multiplier) * baseline
    if not math.isfinite(sigma_gap) or not math.isfinite(baseline):
        return _insufficient(
            None if not math.isfinite(sigma_gap) else sigma_gap,
            None if not math.isfinite(baseline) else baseline,
            "INSUFFICIENT_DATA: σ_gap 或基线中位数非有限值，无法判定；不降暴露",
        )

    if sigma_gap > threshold:
        return OvernightDeriskDecision(
            status=OvernightDeriskStatus.TRIGGERED,
            market=str(market),
            session_date=day,
            decision_instant=decision_instant,
            ratio=float(cfg.downscale_ratio),
            sigma_gap=sigma_gap,
            baseline_median=baseline,
            sessions_used=sessions_used,
            symbols_used=n_symbols,
            reason=(
                f"TRIGGERED: σ_gap={sigma_gap:.6f} > {cfg.sigma_multiplier:g}×基线中位数="
                f"{threshold:.6f}（基线={baseline:.6f}，W={w}，B={b_window}）"
                f"⇒ 非现金目标权重 ×{cfg.downscale_ratio:g}，差额留现金"
            ),
        )

    return OvernightDeriskDecision(
        status=OvernightDeriskStatus.NOT_TRIGGERED,
        market=str(market),
        session_date=day,
        decision_instant=decision_instant,
        ratio=RATIO_UNCHANGED,
        sigma_gap=sigma_gap,
        baseline_median=baseline,
        sessions_used=sessions_used,
        symbols_used=n_symbols,
        reason=(
            f"NOT_TRIGGERED: σ_gap={sigma_gap:.6f} <= {cfg.sigma_multiplier:g}×基线中位数="
            f"{threshold:.6f}（基线={baseline:.6f}，W={w}，B={b_window}）⇒ 暴露不变"
        ),
    )


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def apply_overnight_downscale(
    targets: Mapping[str, float],
    decision: OvernightDeriskDecision,
    cash_proxy_symbols: Iterable[str],
) -> Dict[str, float]:
    """Multiply **non-cash-proxy** target weights by `decision.ratio`; cash stays put.

    * `status != TRIGGERED` -> a plain copy: same keys, same order, same values (float-for-float).
      Nothing is rounded or renormalised, so "not triggered" is bit-identical to not having this
      module.
    * `TRIGGERED` -> non-cash weights `* ratio`; the cash proxy is copied untouched and the
      difference is **left in cash** (``sum`` drops by exactly the de-risked amount -- no
      renormalisation, because normalising would silently scale the residual right back up and
      cancel the de-risking).
    * An explicit ``0.0`` stays ``0.0`` (never `NaN`, never dropped); `NaN` anywhere in the input
      raises, because a `NaN` weight is the exact bug class (`NaN` vs `0.0`) the repo has paid for
      twice.
    """
    if not isinstance(targets, Mapping):
        raise TypeError(f"targets must be a mapping, got {type(targets).__name__}")
    cash = {str(s) for s in (cash_proxy_symbols or ())}
    ratio = float(decision.ratio)

    out: Dict[str, float] = {}
    for sym, weight in targets.items():
        value = float(weight)
        if math.isnan(value):
            raise ValueError(f"targets[{sym!r}] is NaN: a NaN weight cannot be de-risked")
        if decision.status is not OvernightDeriskStatus.TRIGGERED:
            out[sym] = value
            continue
        out[sym] = value if str(sym) in cash else value * ratio
    return out


__all__ = [
    "DECISION_SPEC_VERSION",
    "DEFAULT_BASELINE_WINDOW",
    "DEFAULT_DOWNSCALE_RATIO",
    "DEFAULT_GAP_WINDOW",
    "DEFAULT_MINUTES_BEFORE_CLOSE",
    "DEFAULT_SIGMA_MULTIPLIER",
    "RATIO_UNCHANGED",
    "STATUSES",
    "OvernightDeriskConfig",
    "OvernightDeriskDecision",
    "OvernightDeriskStatus",
    "apply_overnight_downscale",
    "assert_decision_precedes_pricing",
    "compute_gap_series",
    "evaluate_overnight_derisk",
    "resolve_decision_instant",
    "session_pricing_point",
]

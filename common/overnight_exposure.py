"""`R6` -- the fixed-time **overnight exposure** decision (pure function, decision only).

WHAT THIS IS (read the positioning before touching the thresholds)
------------------------------------------------------------------
This module answers exactly one question, asked **before the close**: *how much market
exposure should we carry through the overnight gap?*  It returns a **market-level
multiplier** (`1.0` unchanged, `0.5` de-risked) plus the evidence it used.

It is explicitly NOT any of the following, and the alternative readings are what a future
"improvement" would turn it into:

* **not intraday timing** -- it does not forecast the intraday path and does not flip long /
  short inside the session. The decision is made once, at a fixed clock time, and it is
  about the *night*, not the next ten minutes;
* **not per-symbol selection** -- there is no symbol dimension anywhere in this module. The
  output multiplies the whole book; editing individual weights is a different task and does
  not belong here;
* **not a volatility-targeting overlay** -- no realised-vol estimate, no vol ratio, no
  scalar derived from a variance forecast. The trigger is an *intraday drawdown* reading.

WHY THIS IS NOT REFUTED BY `M3.2`'s NEGATIVE RESULT (and not supported by it either)
------------------------------------------------------------------------------------
`M3.2` studied **execution-VWAP bias**: whether a window-VWAP fill price is systematically
biased versus the price the engine books, i.e. a statement about the *execution price*
variable. `R6` studies **overnight gap risk**: whether reducing exposure before the close
pays for itself through the gap, i.e. a statement about the *overnight return* variable.
Different dependent variables. So `M3.2`'s negative finding is neither evidence against this
module nor evidence for it; `R6` has to stand on its own acceptance suite, and closing it as
a negative result is an explicitly allowed outcome (see the `R6` row in
`docs/development_plan.md` §2.20).

DISCLOSURE -- where the numbers come from (no paper is cited because none is being claimed)
------------------------------------------------------------------------------------------
``cutoff_time = 14:45`` / ``drawdown_threshold = -0.15`` / ``scale_on_trigger = 0.5`` are an
**operational risk-budget convention fixed by the owner** (frozen 2026-11-06, restated in
`docs/overnight_exposure_contract.md`). They are *not* empirically optimised parameters, not
fitted on any sample, and not a quotation from the literature. Do not present them as
research output, and do not "tune" them here -- a different budget is a different decision by
the owner, and it belongs in the contract document first.

FROZEN DECISION RULE (the whole algorithm, nothing hidden)
----------------------------------------------------------
1. **Decision time point** ``cutoff`` = ``14:45:00`` in ``session_timezone``
   (``Asia/Shanghai``) = the END of the 14:45 A-share 5m bar. Only bars whose **bar-END is
   ``<= cutoff``** may be used. This strictly precedes BOTH price-taking moments the
   backtester knows about: the ``close`` convention (price taken at the 15:00 close) and the
   ``intraday_twap`` convention (price taken across the session, whose last slice is at or
   after 14:45). Therefore the decision can never be a function of a price it is trying to
   act on -- that is the entire point of the fixed clock time, and the mutation
   (``cutoff_time = time(15, 5)``) that must turn the suite red is exactly "the decision
   started ingesting bars that only exist after the price was taken".
2. **Trigger**: intraday drawdown of the session's running high, defined *minimally and
   reproducibly* as

       running_high = max(high) over the usable bars with bar-END <= cutoff  (today only)
       drawdown     = (close_of_cutoff_bar - running_high) / running_high

   "Today only" is the *session-local* date; there is deliberately **no lower time bound**, so a
   bar stamped before 09:30 on the same date participates in the running high. Such a bar is a
   feed defect (`R3`'s ``pre_session`` classification) and it is disclosed rather than filtered
   here: silently dropping rows inside the decision rule would be a second, hidden rule that the
   contract does not freeze. Fix the feed, don't teach the decision to ignore it.

   Trigger when ``drawdown <= drawdown_threshold`` (``-0.15``). **The boundary is
   inclusive**: exactly ``-0.15`` triggers, ``-0.1499`` does not. (The subtraction-then-divide
   order is deliberate: it is what makes "exactly -0.15" representable at all -- with
   ``close / running_high - 1.0`` no float pair reproduces the literal ``-0.15``.)
3. **Scale**: ``0.5`` on trigger, ``1.0`` otherwise. Not triggered is *bit-identical* to not
   having this module: the caller multiplies by ``1.0`` and nothing changes.
4. **Status vocabulary is closed**: ``TRIGGERED`` / ``NOT_TRIGGERED`` / ``INSUFFICIENT_DATA``.
   ``INSUFFICIENT_DATA`` carries ``scale is None`` and ``drawdown is None`` and **must never
   be read as 0** (a 0.0 scale would liquidate the book) and **must never be read as "no
   drawdown"** (that would silently pass). Insufficient means: empty frame, fewer than 2
   usable bars at or before the cutoff, no bar ending exactly at the cutoff (a half-day
   session with no afternoon), a cutoff bar that is itself NaN, or a non-positive / all-NaN
   price base. Never silently pass.
5. **Determinism**: a pure function of ``(bars, as_of_date, cutoff_time, thresholds,
   session_timezone)``. No clock reads (no ``datetime.now``), no I/O, no globals mutated, no
   randomness. Two calls return equal results, and the **execution-price convention is not an
   input** -- so ``close`` and ``intraday_twap`` cannot yield different decisions *by
   construction*: there is no price-mode parameter for them to differ on. The testable form of
   that claim is the cutoff-ordering regression
   (``test_frozen_cutoff_precedes_both_price_taking_moments``) plus a signature-structure pin
   (``test_decision_is_independent_of_execution_price_by_construction``); running the same
   function twice on the same frame and asserting the two runs agree proves nothing.

INPUT CONTRACT
--------------
``bars`` is a ``pandas.DataFrame`` indexed by a **tz-aware** ``DatetimeIndex`` of bar-**END**
labels (the `R3` frozen panel spec, ``common/intraday_synthetic``: ``END`` labels 09:35 ...
15:00, ``Asia/Shanghai``), with float columns ``high`` and ``close``. It may span several
sessions; only the rows whose *session-local* date equals ``as_of_date`` are read.

A **tz-naive index is rejected with ``ValueError``** rather than guessed: an unqualified
``14:45`` is an ambiguous instant, and guessing the zone is how an off-by-one-hour feed
silently decides at 13:45 or 15:45. Localise the frame yourself (you know the feed's zone) --
this module will not invent it for you. For the same reason duplicate timestamps raise: two
closes for one bar have no defensible "the" value.

NOT IN THIS MODULE (deliberately)
---------------------------------
No ticket field, no Stage-2 scaling, no wiring into ``scripts/run_live_strategy.py`` or
``scripts/check_live_portfolio_health.py`` -- the consumer surface of `R6` is the part the
plan marks 待确认. No 5m switch plumbing (that is `R7`: ``enable_intraday_5m`` /
``enable_5m_interval_nesting`` / ``use_intraday_downside_vol`` in ``blend_base.py``). No
version axis: this is a new pure function, no strategy / template / ``BASE_VERSION``
obligation attaches to it -- ``DECISION_SPEC_VERSION`` below is a spec tag for the *panel
decision rule*, not a strategy version.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------------------------
# frozen decision spec -- quoted by the contract doc and by the offline suite
# ---------------------------------------------------------------------------------------------
#: Spec revision of THIS module's rule. Deliberately NOT part of the strategy version axis
#: (no ``BASE_VERSION``, no per-class ``version``): there is no strategy, template or blend
#: engine involved, so AGENTS §7 does not apply. This tag exists so a downstream consumer can
#: say "I was written against overnight-decision spec r6.1" without pretending otherwise.
DECISION_SPEC_VERSION = "r6.1"

#: The closed status vocabulary. Anything else is a bug in the caller, not a new state.
DECISION_TRIGGERED = "TRIGGERED"
DECISION_NOT_TRIGGERED = "NOT_TRIGGERED"
DECISION_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
DECISIONS: tuple[str, ...] = (DECISION_TRIGGERED, DECISION_NOT_TRIGGERED, DECISION_INSUFFICIENT_DATA)

#: Frozen decision inputs (owner-fixed operational convention -- see the docstring disclosure).
DEFAULT_CUTOFF_TIME = dt.time(14, 45)
DEFAULT_DRAWDOWN_THRESHOLD = -0.15
DEFAULT_SCALE_ON_TRIGGER = 0.5
DEFAULT_SESSION_TIMEZONE = "Asia/Shanghai"

#: "Do nothing" is a literal 1.0, not a missing key: not-triggered must be bit-identical.
SCALE_UNCHANGED = 1.0

#: Machine-stable reason codes. A human sentence travels alongside in ``reason``; neither is
#: free text a caller should parse.
REASON_TRIGGERED = "drawdown_at_or_below_threshold"
REASON_NOT_TRIGGERED = "drawdown_within_threshold"
REASON_EMPTY_FRAME = "empty_frame"
REASON_NO_BARS_FOR_DATE = "no_bars_for_date"
REASON_TOO_FEW_USABLE_BARS = "too_few_usable_bars"
REASON_NO_BAR_AT_CUTOFF = "no_bar_ending_at_cutoff"
REASON_CUTOFF_BAR_UNUSABLE = "cutoff_bar_unusable"
REASON_NON_POSITIVE_PRICE = "non_positive_price_base"

#: Reason codes that belong to the insufficient bucket (one place to check the invariant).
INSUFFICIENT_REASON_CODES: tuple[str, ...] = (
    REASON_EMPTY_FRAME,
    REASON_NO_BARS_FOR_DATE,
    REASON_TOO_FEW_USABLE_BARS,
    REASON_NO_BAR_AT_CUTOFF,
    REASON_CUTOFF_BAR_UNUSABLE,
    REASON_NON_POSITIVE_PRICE,
)

#: Minimum usable bars at/before the cutoff. One bar is not a path: with a single observation
#: the "running high" and the "current price" are the same bar, so the drawdown would be a
#: tautology (0.0 for a bar whose high == close, or an artifact of the bar's own range).
MIN_USABLE_BARS = 2


@dataclass(frozen=True)
class OvernightExposureDecision:
    """One market-level overnight-exposure decision, with the evidence it was made from.

    Frozen and fully comparable: two calls on the same inputs are ``==``, which is what
    "deterministic" means operationally (the suite asserts it instead of trusting the prose).

    ``scale`` / ``drawdown`` are ``None`` **only** for ``INSUFFICIENT_DATA``. Do not coerce
    them: ``None`` means "we do not know", ``0.0`` means "liquidate" / "no drawdown", and
    conflating the two is the failure mode this dataclass is shaped to prevent.
    """

    as_of_date: dt.date
    decision: str
    scale: float | None
    drawdown: float | None
    cutoff: pd.Timestamp
    bars_used: int
    reason_code: str
    reason: str
    running_high: float | None = None
    cutoff_close: float | None = None

    def __post_init__(self) -> None:
        # Invariants enforced at construction: a hand-built instance cannot claim a state the
        # decision rule can never produce (e.g. TRIGGERED with scale None).
        if self.decision not in DECISIONS:
            raise ValueError(f"decision={self.decision!r} is outside the closed vocabulary {DECISIONS}")
        if self.decision == DECISION_INSUFFICIENT_DATA:
            if self.scale is not None or self.drawdown is not None:
                raise ValueError(
                    "INSUFFICIENT_DATA must carry scale=None and drawdown=None; a numeric scale "
                    "here would be read as 0 (liquidation) or as 'no drawdown' downstream"
                )
            if self.reason_code not in INSUFFICIENT_REASON_CODES:
                raise ValueError(
                    f"INSUFFICIENT_DATA reason_code={self.reason_code!r} is not one of "
                    f"{INSUFFICIENT_REASON_CODES}"
                )
        elif self.scale is None or self.drawdown is None:
            raise ValueError(f"{self.decision} must carry a numeric scale and drawdown")
        elif self.decision == DECISION_TRIGGERED and self.reason_code != REASON_TRIGGERED:
            raise ValueError(f"TRIGGERED must carry reason_code={REASON_TRIGGERED!r}")
        elif self.decision == DECISION_NOT_TRIGGERED and self.reason_code != REASON_NOT_TRIGGERED:
            raise ValueError(f"NOT_TRIGGERED must carry reason_code={REASON_NOT_TRIGGERED!r}")

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for a future ticket / report field (no consumer is wired yet)."""
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "decision": self.decision,
            "scale": self.scale,
            "drawdown": self.drawdown,
            "cutoff": self.cutoff.isoformat(),
            "bars_used": self.bars_used,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "running_high": self.running_high,
            "cutoff_close": self.cutoff_close,
            "spec_version": DECISION_SPEC_VERSION,
        }


# ---------------------------------------------------------------------------------------------
# validation helpers -- each one refuses instead of guessing
# ---------------------------------------------------------------------------------------------
def _as_session_date(value: Any) -> dt.date:
    """Normalise ``as_of_date`` to a plain ``datetime.date`` (accepting an ISO string)."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return pd.Timestamp(value).date()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"as_of_date={value!r} is not a parseable date: {exc}") from exc
    raise TypeError(f"as_of_date={value!r} must be a datetime.date / ISO string, got {type(value).__name__}")


def _resolve_cutoff(day: dt.date, cutoff_time: dt.time, session_timezone: str) -> pd.Timestamp:
    """Localise ``day@cutoff_time`` into ``session_timezone``; refuse an ambiguous instant.

    ``Asia/Shanghai`` has observed no DST since 1991, so for the default zone this is always a
    single instant. The guard still exists because the parameter is public: a zone that folds
    back over this clock time would make "14:45" name two instants, and picking one silently
    is exactly the bug the tz-aware-input rule exists to prevent.
    """
    if not isinstance(cutoff_time, dt.time) or isinstance(cutoff_time, dt.datetime):
        raise TypeError(f"cutoff_time={cutoff_time!r} must be a datetime.time, not a datetime")
    naive = pd.Timestamp(dt.datetime.combine(day, cutoff_time))
    try:
        return naive.tz_localize(session_timezone)
    except Exception as exc:  # unknown zone, or an ambiguous / non-existent local time
        raise ValueError(
            f"cannot localise the decision cutoff {naive.isoformat()} into "
            f"session_timezone={session_timezone!r}: {exc}. An ambiguous cutoff would make the "
            f"decision time point unknowable, so it is refused instead of resolved by guess."
        ) from exc


def _validate_bars_frame(bars: Any) -> pd.DataFrame:
    """Type / tz / column / duplicate checks -- the input contract from the module docstring."""
    if not isinstance(bars, pd.DataFrame):
        raise TypeError(f"bars must be a pandas.DataFrame, got {type(bars).__name__}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError(
            "bars must be indexed by a DatetimeIndex of bar-END timestamps (END convention: the "
            f"label 14:45 closes the 14:40-14:45 bar); got index type {type(bars.index).__name__}"
        )
    if bars.index.tz is None:
        raise ValueError(
            "bars.index is tz-naive, so the 14:45 decision cutoff is an ambiguous instant and "
            "this module will not guess a zone. Localise the frame first "
            "(e.g. bars.index = bars.index.tz_localize('Asia/Shanghai')) -- only do that if you "
            "actually know which zone the feed labelled the bars in."
        )
    missing = [c for c in ("high", "close") if c not in bars.columns]
    if missing:
        raise ValueError(f"bars is missing required column(s) {missing}; got {list(bars.columns)}")
    if bars.index.has_duplicates:
        raise ValueError(
            "bars.index has duplicate bar-END timestamps; two closes for one bar have no "
            "defensible 'the' value. Collapse the duplicates explicitly before deciding "
            "(and if the frame holds several symbols, pass one market-level series instead)."
        )
    return bars


def _insufficient(*, day: dt.date, cutoff: pd.Timestamp, reason_code: str, reason: str,
                  bars_used: int) -> OvernightExposureDecision:
    return OvernightExposureDecision(
        as_of_date=day,
        decision=DECISION_INSUFFICIENT_DATA,
        scale=None,
        drawdown=None,
        cutoff=cutoff,
        bars_used=int(bars_used),
        reason_code=reason_code,
        reason=reason,
        running_high=None,
        cutoff_close=None,
    )


# ---------------------------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------------------------
def decide_overnight_exposure(
    bars: pd.DataFrame,
    as_of_date: dt.date | str,
    *,
    cutoff_time: dt.time = DEFAULT_CUTOFF_TIME,
    drawdown_threshold: float = DEFAULT_DRAWDOWN_THRESHOLD,
    scale_on_trigger: float = DEFAULT_SCALE_ON_TRIGGER,
    session_timezone: str = DEFAULT_SESSION_TIMEZONE,
) -> OvernightExposureDecision:
    """Decide whether to cut overnight market exposure, using only bars that closed by 14:45.

    Args:
        bars: tz-aware ``DatetimeIndex`` of bar-END labels + float ``high`` / ``close`` columns.
            May span several sessions; only ``as_of_date``'s rows participate.
        as_of_date: session-local trading date (``datetime.date`` or ISO string).
        cutoff_time: decision time point, session-local. Default ``14:45:00`` -- the END of the
            14:45 5m bar, strictly before the 15:00 close and at/before the last
            ``intraday_twap`` slice.
        drawdown_threshold: trigger level, **inclusive** (``drawdown <= threshold``).
        scale_on_trigger: market-level exposure multiplier applied on trigger.
        session_timezone: zone the cutoff is resolved in.

    Returns:
        :class:`OvernightExposureDecision`. ``scale`` is ``0.5`` / ``1.0`` on a real decision
        and ``None`` for ``INSUFFICIENT_DATA`` -- never ``0.0``.

    Raises:
        TypeError: ``bars`` is not a DataFrame, or ``cutoff_time`` is not a ``time``.
        ValueError: naive / non-datetime index, missing columns, duplicate timestamps, an
            ambiguous cutoff, or an out-of-domain threshold / scale.
    """
    day = _as_session_date(as_of_date)
    cutoff = _resolve_cutoff(day, cutoff_time, session_timezone)

    if not isinstance(drawdown_threshold, (int, float)) or isinstance(drawdown_threshold, bool):
        raise TypeError(f"drawdown_threshold={drawdown_threshold!r} must be a negative float")
    threshold = float(drawdown_threshold)
    if not math.isfinite(threshold):
        raise ValueError(f"drawdown_threshold={drawdown_threshold!r} must be finite")
    if threshold >= 0.0:
        # A non-negative threshold would fire on every session, i.e. "always de-risk" -- a
        # different policy that must not be smuggled in through a parameter.
        raise ValueError(
            f"drawdown_threshold={drawdown_threshold!r} must be strictly negative; a "
            f"non-negative threshold would trigger on every session regardless of the path"
        )

    if not isinstance(scale_on_trigger, (int, float)) or isinstance(scale_on_trigger, bool):
        raise TypeError(f"scale_on_trigger={scale_on_trigger!r} must be a float in (0, 1]")
    scale_on_trigger = float(scale_on_trigger)
    if not math.isfinite(scale_on_trigger) or not 0.0 < scale_on_trigger <= 1.0:
        raise ValueError(
            f"scale_on_trigger={scale_on_trigger!r} must be in (0, 1]; 0.0 would liquidate the "
            f"book and >1 would lever it, neither is an 'exposure reduction'"
        )

    bars = _validate_bars_frame(bars)

    if len(bars) == 0:
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_EMPTY_FRAME,
            reason="the bars frame is empty, so no session can be evaluated",
            bars_used=0,
        )

    # --- session-local view: the decision date is a *session* date, so the feed's zone is
    # --- converted to the session zone (instant-preserving) rather than its wall label reused.
    local_index = bars.index.tz_convert(session_timezone)
    on_date = np.asarray([ts.date() == day for ts in local_index], dtype=bool)
    if not on_date.any():
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_NO_BARS_FOR_DATE,
            reason=f"the frame carries no bars for {day.isoformat()} in {session_timezone}",
            bars_used=0,
        )

    # --- the only cut that matters: bar-END <= cutoff. Absolute instants, so a feed stamped in
    # --- another zone is compared correctly (14:45+08:00 == 06:45+00:00).
    at_or_before_cutoff = np.asarray(bars.index <= cutoff, dtype=bool)
    highs = pd.to_numeric(bars["high"], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    usable = on_date & at_or_before_cutoff & np.isfinite(highs) & np.isfinite(closes)
    bars_used = int(usable.sum())

    if bars_used < MIN_USABLE_BARS:
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_TOO_FEW_USABLE_BARS,
            reason=(
                f"only {bars_used} usable bar(s) ended at or before the cutoff "
                f"({cutoff.isoformat()}); a running high needs at least {MIN_USABLE_BARS} "
                f"observations, otherwise the 'drawdown' is a tautology on one bar"
            ),
            bars_used=bars_used,
        )

    # The cutoff bar is required by identity, never by proximity: a half-day session whose last
    # bar is 11:30 simply has no price at 14:45, and interpolating one would be invention.
    at_cutoff = np.asarray(bars.index == cutoff, dtype=bool)
    if not at_cutoff.any():
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_NO_BAR_AT_CUTOFF,
            reason=(
                f"no bar ends exactly at the cutoff {cutoff.isoformat()} (half-day session, a "
                f"5m gap, or a START-labelled feed); the session's price at the decision time "
                f"point is unknown, so it is not evaluated"
            ),
            bars_used=bars_used,
        )
    if not usable[at_cutoff].any():
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_CUTOFF_BAR_UNUSABLE,
            reason=f"the bar ending at the cutoff {cutoff.isoformat()} has a NaN high/close",
            bars_used=bars_used,
        )

    running_high = float(np.max(highs[usable]))
    cutoff_close = float(closes[at_cutoff & usable][0])

    if running_high <= 0.0 or cutoff_close <= 0.0 or np.any(highs[usable] <= 0.0):
        return _insufficient(
            day=day, cutoff=cutoff, reason_code=REASON_NON_POSITIVE_PRICE,
            reason=(
                f"non-positive price base (running_high={running_high!r}, "
                f"cutoff_close={cutoff_close!r}); a percentage drawdown is undefined here and "
                f"must not be reported as 0"
            ),
            bars_used=bars_used,
        )

    # (close - high) / high, NOT close / high - 1.0: only this order makes the frozen boundary
    # representable ("exactly -0.15" exists as a float pair here, it does not in the other form).
    drawdown = (cutoff_close - running_high) / running_high

    if drawdown <= threshold:
        return OvernightExposureDecision(
            as_of_date=day,
            decision=DECISION_TRIGGERED,
            scale=scale_on_trigger,
            drawdown=drawdown,
            cutoff=cutoff,
            bars_used=bars_used,
            reason_code=REASON_TRIGGERED,
            reason=(
                f"intraday drawdown {drawdown:.6f} from the running high {running_high:.6f} to "
                f"the {cutoff.strftime('%H:%M')} close {cutoff_close:.6f} is at or below the "
                f"threshold {threshold:.6f}: carry {scale_on_trigger:.4f} of market exposure "
                f"through the overnight gap"
            ),
            running_high=running_high,
            cutoff_close=cutoff_close,
        )

    return OvernightExposureDecision(
        as_of_date=day,
        decision=DECISION_NOT_TRIGGERED,
        scale=SCALE_UNCHANGED,
        drawdown=drawdown,
        cutoff=cutoff,
        bars_used=bars_used,
        reason_code=REASON_NOT_TRIGGERED,
        reason=(
            f"intraday drawdown {drawdown:.6f} from the running high {running_high:.6f} is "
            f"inside the threshold {threshold:.6f}: market exposure is unchanged at "
            f"{SCALE_UNCHANGED:.1f}"
        ),
        running_high=running_high,
        cutoff_close=cutoff_close,
    )


__all__ = [
    "DECISIONS",
    "DECISION_INSUFFICIENT_DATA",
    "DECISION_NOT_TRIGGERED",
    "DECISION_SPEC_VERSION",
    "DECISION_TRIGGERED",
    "DEFAULT_CUTOFF_TIME",
    "DEFAULT_DRAWDOWN_THRESHOLD",
    "DEFAULT_SCALE_ON_TRIGGER",
    "DEFAULT_SESSION_TIMEZONE",
    "INSUFFICIENT_REASON_CODES",
    "MIN_USABLE_BARS",
    "OvernightExposureDecision",
    "SCALE_UNCHANGED",
    "decide_overnight_exposure",
]

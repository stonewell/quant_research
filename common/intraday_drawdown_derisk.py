"""`R1` -- pre-open **intraday drawdown** (`hwm_intra` / `dd_trough`) => market-layer exposure downshift.

FROZEN JUDGEMENT: `docs/intraday_drawdown_derisk.md` (`intraday_drawdown/v1`, rule `r1.1`). Read that
document, not this docstring, if the two ever disagree; this module implements it and may not
re-interpret it. `DECISION_SPEC_VERSION = "r1.1"` is a **judgement spec tag**, NOT a strategy
version -- no `BASE_VERSION` / per-class `version` obligation attaches to a pure module
(AGENTS §7), and none may be invented "for symmetry".

WHAT THIS IS (positioning before arithmetic)
--------------------------------------------
One question, asked **before the open**: *yesterday's session ended `dd` below its own intraday
high-water mark -- should today's target exposure be carried at a discount?* The answer is a
**market-level multiplier** (`1.0` unchanged, `0.75` / `0.50` / `0.25` de-risked) plus the evidence
that produced it.

It is explicitly NOT: per-symbol selection (there is no symbol dimension anywhere in this module --
the input is one portfolio-level drawdown reading); intraday timing (no path forecast, no flipping
inside a session); a volatility-targeting overlay (a drawdown reading is not a variance forecast);
and NOT a replacement for the drawdown circuit breakers (`dd_reduce_thresh` / `dd_defensive_thresh`
/ `dd_stop_thresh`, `pipeline/research/blend/blend_base.py:654-656`), which stay the sole owners of
tiered de-risking inside the engine.

THE QUANTITY, AND ITS REFERENCE PRICE (chosen, and why)
------------------------------------------------------
    drawdown = price / hwm_intra - 1            # <= 0; the producer's own definition

`hwm_intra` is **not** "the session's own high". The producer
(`scripts/check_live_portfolio_health.py:2856`) computes

    hwm_intra = max(float(dd_info["peak_nav"]), intra_peak)

i.e. the **carried high-water mark from the prior state** (`peak_nav`, the All-Time/trailing peak
that `account_state.json` persists) raised by the session's intraday peak when the session made a
new high. The reference therefore is the **previous close / carried prior peak**, and this module
freezes it as `REFERENCE_PRICE_SEMANTICS = "previous_close"`:

* **Previous close, not the session open.** Anchoring on the session open would report `0%`
  drawdown on a *gap-down* day whose book has already lost money from the previous close -- the
  exact day `R1` exists for -- and it would also be a *different quantity* from the producer's
  `dd_trough`, which is what `R1` claims to consume (measure the same thing or do not measure it).
* The open is still refused as a substitute: a gap-up that then bleeds back to the open is not
  "no drawdown" either, and the carried-peak anchor already captures it.
* `anchor_hwm_intra()` implements exactly this anchor (mirror of `:2856`); `evaluate_intraday_drawdown`
  accepts `reference_price` **only** as a self-consistency guard (a carried peak can never sit
  *below* the previous close, so `hwm_intra < reference_price` is an inconsistent anchor and is
  reported as `INSUFFICIENT_DATA`, never silently clamped).

FROZEN LADDER (see the freeze document for the dimensional derivation; `<=` de-risks)
------------------------------------------------------------------------------------
    dd >  -2.0%                     => 1.0     (bit-identical no-op)
    -3.0% < dd <= -2.0%             => 0.75
    -4.0% < dd <= -3.0%             => 0.50
    dd <= -4.0%                     => 0.25     (never 0.0 -- see below)

THESE ARE **NEW** PARAMETERS, not borrowed ones. The multi-day circuit-breaker tiers are
`10% / 15% / 20%` (`pipeline/research/blend/blend_base.py:654-656`); a single session's
intraday drawdown runs at roughly **one fifth** of a multi-day drawdown's scale, so the ladder is
`2 / 3 / 4%`. `0.25` is the floor and **is not a liquidation**: a full-to-zero multiplier is the
`R7` / circuit-breaker family's job (and `R7` owns the missing-5m freeze this module must never
consume), while `R1` is the advisory first round. `1.0` is returned as the literal
`MULTIPLIER_UNCHANGED`, so "no de-risk" is *bit-identical* to not calling this module.

Stateless reset: the ladder is a pure function of today's reading -- `dd` back above `-2.0%`
returns `1.0` immediately. **No hysteresis, no cooldown**: round 1 is advisory, so a cooldown would
have to live in a mutable cache, i.e. it would smuggle state into a pure function and make the
verdict depend on how often it was called.

TRI-STATE (never "missing reads as safe")
-----------------------------------------
`INSUFFICIENT_DATA` is its own member and is **neither** `NOT_TRIGGERED` **nor** `0.0`:
`multiplier = 1.0` only says "no multiplier was applied", `drawdown is None` says "not measurable"
(**never `0.0`** -- a zero would read as "measured, sitting exactly at the high", a far stronger and
false claim). Unmeasurable inputs: `hwm_intra` missing / `NaN` / non-finite / `<= 0`; the price (or
`dd_trough`) missing / `NaN` / non-finite / `<= 0`; a `dd_trough` that is *positive* (violates the
definition of a drawdown); and an anchor inconsistent with the reference price. No warnings are
emitted -- the closed `status` vocabulary is the only signal, so a caller sweeping hundreds of
sessions is not spammed and "insufficient" can never be lost in a warning filter.

BOUNDARIES (do not "helpfully" fold these in)
---------------------------------------------
* **Not `R4`.** `R4` (`common/cs_dispersion_exposure.py`) judges *cross-sectional structure* on a
  relative `D/B` band pair (`0.75 * B` / `0.50 * B`). `R1` judges *one portfolio's* drawdown on
  **absolute** percentage bands. `R1` must **never** reuse `R4`'s `D`/`B` bands, never call it and
  never be called by it; a "combined risk score" would corrupt both sides
  (`docs/development_plan.md:334` item 5 says this verbatim, and `R4`'s own module says it back).
* **Not `R7`.** Missing-5m-bar freezing of new entries is `R7`'s ownership. `R1` consumes the
  already-measured `dd_trough` and never freezes orders; `gates_orders=False` travels on every
  result.
* **Not `R5` / `R6`.** `R5` owns the intraday drawdown *gate* (the `Diagnostic only (R5)` label at
  `scripts/check_live_portfolio_health.py:2216`, and the "without forced tier escalation" line at
  `:1727`), `R6` owns the overnight gap judgement: same market layer, different dependent variables,
  thresholds never shared. (The ledger's `:1364` citation for that label is stale -- `:1364` is now
  `def evaluate_market_breadth_and_thrust`; the label lives at the two lines above.)
* **Round 1 is advisory only.** `advisory=True` / `gates_orders=False` travel on every result and
  the dataclass **refuses** to be built any other way. This round wires nothing: no
  `scripts/run_live_strategy.py` change, no ticket field, no weight rewrite, no `blend_base.py`
  touch, no strategy-class change, no version-axis movement. Deliberately absent: any
  `apply_*_downscale` helper -- the only thing a caller may do with a result this round is print it
  (the fence idiom of `common/specific_alpha.py:536-544`).
* **Pure**: no I/O, no clock reads, no network, no randomness, no global state, no warnings. Same
  inputs => equal outputs.
"""

from __future__ import annotations

import enum
import math
import numbers
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

#: Judgement spec tag (`intraday_drawdown/v1`, rule `r1.1`). NOT a strategy version.
DECISION_SPEC_VERSION = "r1.1"

#: Frozen parameters (see `docs/intraday_drawdown_derisk.md` §2 for the parameter table and §3 for
#: each value's dimensional derivation). These are **NEW** to `R1`; re-register them in the freeze
#: document (append a `## Revision`) before changing any of them.
DEFAULT_LIGHT_DRAWDOWN_THRESH = -0.02   # dd > -2.0%      => 1.0
DEFAULT_MEDIUM_DRAWDOWN_THRESH = -0.03  # dd <= -3.0%     => 0.50
DEFAULT_DEEP_DRAWDOWN_THRESH = -0.04    # dd <= -4.0%     => 0.25

#: The literal ``1.0`` -- never a computed`` min(1.0, ...)`` float: "not triggered" must be
#: bit-identical to not calling this module at all.
MULTIPLIER_UNCHANGED = 1.0
MULTIPLIER_LIGHT = 0.75
MULTIPLIER_MEDIUM = 0.50
MULTIPLIER_DEEP = 0.25

#: The reference price `hwm_intra` is anchored on. Frozen as the previous close / carried prior
#: peak (see the module docstring: the producer does `max(peak_nav, intra_peak)`), explicitly NOT
#: the session open.
REFERENCE_PRICE_SEMANTICS = "previous_close"

_SOURCES: Tuple[str, ...] = ("price", "dd_trough")


class IntradayDrawdownStatus(str, enum.Enum):
    """Closed five-state vocabulary (the `.value` strings are the observable contract).

    `INSUFFICIENT_DATA` is deliberately its own member and is **neither** `NOT_TRIGGERED` **nor**
    `0.0`: folding "we cannot measure the drawdown" into "the drawdown was acceptable" is the
    missing-data-reads-as-a-risk-conclusion bug class this repo exists to avoid
    (`common/overnight_derisk.py:115-125`, `common/cs_dispersion_exposure.py:143-155` set the
    precedent).
    """

    NOT_TRIGGERED = "NOT_TRIGGERED"
    DERISK_LIGHT = "DERISK_LIGHT"
    DERISK_MEDIUM = "DERISK_MEDIUM"
    DERISK_DEEP = "DERISK_DEEP"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


STATUSES: Tuple[str, ...] = tuple(s.value for s in IntradayDrawdownStatus)


def _finite_positive(value: Any) -> Optional[float]:
    """`float(value)` when it is a finite number `> 0`, else `None` (never a coercion of junk)."""
    if value is None or isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


@dataclass(frozen=True)
class IntradayDrawdownConfig:
    """Frozen judgement parameters (validated at construction, so a bad budget cannot run).

    `drawdown` is a **fraction** (`-0.02` means `-2.0%`), matching the producer's `dd_trough`
    (`(intra_min - hwm_intra) / hwm_intra`) so no percent/fraction conversion can drift between
    producer and consumer.

    All three thresholds are **negative** and **strictly ordered** (`deep < medium < light < 0`):
    a non-negative "drawdown trigger" would be a different quantity (a return level, not a
    drawdown) and would put `R1` into `R4`'s / `R5`'s domain. Equal thresholds would make the
    middle band unreachable -- a silently dead threshold rather than a tighter one.
    """

    light_drawdown_thresh: float = DEFAULT_LIGHT_DRAWDOWN_THRESH
    medium_drawdown_thresh: float = DEFAULT_MEDIUM_DRAWDOWN_THRESH
    deep_drawdown_thresh: float = DEFAULT_DEEP_DRAWDOWN_THRESH
    light_multiplier: float = MULTIPLIER_LIGHT
    medium_multiplier: float = MULTIPLIER_MEDIUM
    deep_multiplier: float = MULTIPLIER_DEEP

    def __post_init__(self) -> None:
        for name in ("light_drawdown_thresh", "medium_drawdown_thresh",
                     "deep_drawdown_thresh"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number, got {type(value).__name__}")
            if not math.isfinite(float(value)) or float(value) >= 0.0:
                raise ValueError(
                    f"{name} must be a finite, strictly negative fraction "
                    f"(e.g. -0.02 for -2.0%), got {value!r}"
                )
        if not float(self.deep_drawdown_thresh) < float(self.medium_drawdown_thresh) \
                < float(self.light_drawdown_thresh):
            raise ValueError(
                "drawdown thresholds must be strictly ordered deep < medium < light "
                f"(got {self.deep_drawdown_thresh!r} / {self.medium_drawdown_thresh!r} / "
                f"{self.light_drawdown_thresh!r}); equal thresholds make a band unreachable"
            )
        for name in ("light_multiplier", "medium_multiplier", "deep_multiplier"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number, got {type(value).__name__}")
            # 0.0 is liquidation (not "de-risking") and >= 1.0 is leverage (not a downshift);
            # both are outside this rule's semantics (`overnight_derisk` / `cs_dispersion_exposure`
            # make the same check).
            if not math.isfinite(float(value)) or not (0.0 < float(value) < 1.0):
                raise ValueError(f"{name} must satisfy 0 < {name} < 1, got {value!r}")
        if not float(self.deep_multiplier) < float(self.medium_multiplier) \
                < float(self.light_multiplier):
            raise ValueError(
                "multipliers must be strictly ordered deep < medium < light "
                f"(got {self.deep_multiplier!r} / {self.medium_multiplier!r} / "
                f"{self.light_multiplier!r})"
            )


@dataclass(frozen=True)
class IntradayDrawdownDecision:
    """The judgement for one session: a market-layer multiplier plus the evidence behind it.

    Field semantics worth pinning:

    * ``multiplier`` -- `1.0` is the literal `MULTIPLIER_UNCHANGED`, so "not triggered" is
      bit-identical to not calling this module. Never `0.999...`, never `0.0`.
    * ``drawdown`` -- the fraction the ladder was applied to, or `None` when **not measurable**
      (never `0.0`, which would read as "measured, sitting exactly at the high").
    * ``status`` -- one of `IntradayDrawdownStatus`; `INSUFFICIENT_DATA` is neither "not triggered"
      nor a de-risk band.
    * ``hwm_intra`` / ``price`` / ``dd_trough`` -- the raw inputs, echoed for audit; `None` where
      the caller did not supply them or they were unusable.
    * ``dd_source`` -- `"price"` or `"dd_trough"` (which input the reading came from), `"none"`.
    * ``reference_semantics`` -- the frozen anchor (`REFERENCE_PRICE_SEMANTICS`), carried so a
      stored decision can never be re-read under a different anchor convention.
    * ``advisory`` / ``gates_orders`` -- round-1 fence: this result may be printed, never obeyed.
      Enforced in `__post_init__`, so a "gate" instance cannot even be constructed.
    """

    multiplier: float
    drawdown: Optional[float]
    status: str
    reason: str
    hwm_intra: Optional[float] = None
    price: Optional[float] = None
    dd_trough: Optional[float] = None
    reference_price: Optional[float] = None
    reference_semantics: str = REFERENCE_PRICE_SEMANTICS
    dd_source: str = "none"
    advisory: bool = True
    gates_orders: bool = False

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")
        if self.dd_source not in _SOURCES + ("none",):
            raise ValueError(
                f"dd_source must be one of {_SOURCES + ('none',)}, got {self.dd_source!r}"
            )
        if self.reference_semantics != REFERENCE_PRICE_SEMANTICS:
            raise ValueError(
                "reference_semantics must stay "
                f"{REFERENCE_PRICE_SEMANTICS!r} (the frozen anchor); got "
                f"{self.reference_semantics!r}"
            )
        multiplier = float(self.multiplier)
        if self.status == IntradayDrawdownStatus.INSUFFICIENT_DATA.value:
            if multiplier != MULTIPLIER_UNCHANGED:
                raise ValueError(
                    f"INSUFFICIENT_DATA must carry multiplier == 1.0 (no multiplier applied), "
                    f"got {self.multiplier!r}"
                )
            if self.drawdown is not None:
                raise ValueError(
                    "INSUFFICIENT_DATA must carry drawdown is None (never 0.0: a zero would read "
                    f"as a measured 'at the high'), got {self.drawdown!r}"
                )
        elif self.status == IntradayDrawdownStatus.NOT_TRIGGERED.value:
            if multiplier != MULTIPLIER_UNCHANGED:
                raise ValueError(
                    f"NOT_TRIGGERED must carry multiplier == 1.0, got {self.multiplier!r}"
                )
            if self.drawdown is None:
                raise ValueError("NOT_TRIGGERED must carry the measured drawdown")
        else:
            if not (0.0 < multiplier < 1.0):
                raise ValueError(
                    f"{self.status} must carry a multiplier in (0, 1), got {self.multiplier!r}"
                )
            if self.drawdown is None:
                raise ValueError(f"{self.status} must carry the measured drawdown")
        # Round-1 fence, enforced structurally: no code path may hand out an order gate.
        if self.advisory is not True:
            raise ValueError("advisory must be True: round 1 is advisory only")
        if self.gates_orders is not False:
            raise ValueError("gates_orders must be False: R1 never gates orders in round 1")


# --------------------------------------------------------------------------- #
# the quantity
# --------------------------------------------------------------------------- #
def compute_intraday_drawdown(hwm_intra: Any, price: Any) -> Optional[float]:
    """`price / hwm_intra - 1` (<= 0), or `None` when either input is unusable.

    `None` -- never `0.0` -- is the "not measurable" answer: `0.0` means "the price sits exactly at
    the high-water mark", which is a measurement, and conflating the two is the bug class this repo
    has paid for. A non-finite / non-positive `hwm_intra` (division by zero, sign flip) or price is
    unusable, and so is a *positive* result (the HWM was not a peak, or the arguments were swapped).
    """
    high = _finite_positive(hwm_intra)
    last = _finite_positive(price)
    if high is None or last is None:
        return None
    drawdown = last / high - 1.0
    if not math.isfinite(drawdown) or drawdown > 0.0:
        return None
    return drawdown


def anchor_hwm_intra(reference_price: Any, prices: Optional[Iterable[Any]] = None) -> Optional[float]:
    """`hwm_intra = max(reference_price, max(prices))` -- the producer's own anchor, reproduced.

    Mirror of `scripts/check_live_portfolio_health.py:2856`
    (`hwm_intra = max(float(dd_info["peak_nav"]), intra_peak)`): the reference is the **previous
    close / carried prior peak**, and the session's own peak can only raise it. Non-positive /
    non-finite prices are **excluded** (never read as a `0.0` price -- a zero print is missing data,
    not a free asset). All inputs excluded => `None` ("not measurable"), which the evaluator turns
    into `INSUFFICIENT_DATA`.
    """
    candidates = []
    reference = _finite_positive(reference_price)
    if reference is not None:
        candidates.append(reference)
    if prices is not None:
        if isinstance(prices, (str, bytes)):
            raise TypeError("prices must be an iterable of numbers, not a string")
        try:
            iterator = list(prices)
        except TypeError as exc:  # pragma: no cover - defensive: wrong container type
            raise TypeError(f"prices must be an iterable of numbers, got {type(prices).__name__}") from exc
        for value in iterator:
            usable = _finite_positive(value)
            if usable is not None:
                candidates.append(usable)
    if not candidates:
        return None
    return max(candidates)


# --------------------------------------------------------------------------- #
# the judgement
# --------------------------------------------------------------------------- #
def classify_intraday_drawdown(drawdown: Optional[float],
                               config: Optional[IntradayDrawdownConfig] = None) -> Tuple[str, float, str]:
    """Map a drawdown fraction onto `(status, multiplier, reason)` -- the frozen ladder, nothing else.

    Split out from the input plumbing on purpose: the band boundaries are pinned by tests on
    **exact** floats here (`dd == -0.02` de-risks to `0.75`; one ULP above does not), which is not
    achievable through price-ratio arithmetic.

    `<=` de-risks: equality belongs to the **deeper** band, because the thresholds are a *risk
    budget* -- being exactly at the budget means the budget was reached. So the bands are evaluated
    deepest-first. `1.0` is returned as the literal `MULTIPLIER_UNCHANGED` so "not triggered" is
    bit-identical.
    """
    cfg = config or IntradayDrawdownConfig()
    if drawdown is None:
        return (IntradayDrawdownStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                "intraday drawdown is not measurable (missing / non-finite / non-positive / "
                "positive-implying-broken-HWM input); exposure unchanged, this is NOT "
                "'not triggered'")
    try:
        value = float(drawdown)
    except (TypeError, ValueError):
        return (IntradayDrawdownStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                f"intraday drawdown {drawdown!r} is not a number; exposure unchanged")
    if not math.isfinite(value):
        return (IntradayDrawdownStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                f"intraday drawdown {value!r} is not finite; exposure unchanged")
    if value > 0.0:
        # Out of the quantity's domain: `dd = price/hwm - 1 > 0` says the "high-water mark" is
        # below the current price, i.e. it is not a peak. Refusing beats a silent clean no-op.
        return (IntradayDrawdownStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                f"intraday drawdown {value!r} is positive, so hwm_intra is not a peak "
                "(swapped arguments or a stale HWM); exposure unchanged, not measurable")

    if value <= float(cfg.deep_drawdown_thresh):
        return (IntradayDrawdownStatus.DERISK_DEEP.value, MULTIPLIER_DEEP,
                f"intraday drawdown {value:.4%} <= deep threshold "
                f"{cfg.deep_drawdown_thresh:.2%} -> market-layer exposure x{MULTIPLIER_DEEP:.2f}")
    if value <= float(cfg.medium_drawdown_thresh):
        return (IntradayDrawdownStatus.DERISK_MEDIUM.value, MULTIPLIER_MEDIUM,
                f"intraday drawdown {value:.4%} <= medium threshold "
                f"{cfg.medium_drawdown_thresh:.2%} -> market-layer exposure x{MULTIPLIER_MEDIUM:.2f}")
    if value <= float(cfg.light_drawdown_thresh):
        return (IntradayDrawdownStatus.DERISK_LIGHT.value, MULTIPLIER_LIGHT,
                f"intraday drawdown {value:.4%} <= light threshold "
                f"{cfg.light_drawdown_thresh:.2%} -> market-layer exposure x{MULTIPLIER_LIGHT:.2f}")
    return (IntradayDrawdownStatus.NOT_TRIGGERED.value, MULTIPLIER_UNCHANGED,
            f"intraday drawdown {value:.4%} > light threshold "
            f"{cfg.light_drawdown_thresh:.2%} -> exposure unchanged (x1.00)")


def evaluate_intraday_drawdown(hwm_intra: Any,
                               *, price: Any = None,
                               dd_trough: Any = None,
                               reference_price: Any = None,
                               config: Optional[IntradayDrawdownConfig] = None) -> IntradayDrawdownDecision:
    """Judge one session's market-layer multiplier from its `hwm_intra` and trough reading.

    Parameters
    ----------
    hwm_intra
        The high-water mark the drawdown is measured against (`max(prior carried peak, session
        peak)` -- see :func:`anchor_hwm_intra`). Missing / non-finite / `<= 0` => `INSUFFICIENT_DATA`.
    price
        The trough (or current) price, in the same unit as `hwm_intra`. Converted through
        :func:`compute_intraday_drawdown`.
    dd_trough
        The **already-computed** drawdown fraction (`(trough - hwm_intra) / hwm_intra`, `<= 0`), for
        callers that consume the Stage-1 field directly. A positive value is out of the quantity's
        domain and is reported as `INSUFFICIENT_DATA`.
    reference_price
        Optional self-consistency anchor (the previous close / carried prior peak). The carried peak
        can never sit *below* the previous close, so `hwm_intra < reference_price` means the anchor
        is not a peak: `INSUFFICIENT_DATA`, never a silent clamp towards `0.0`.
    config
        Frozen ladder; defaults to :class:`IntradayDrawdownConfig`.

    Exactly **one** of `price` / `dd_trough` must be supplied: both together is ambiguous wiring and
    raises (`ValueError`), neither is simply "not measurable" (`INSUFFICIENT_DATA`), because a caller
    that lost its data must not be turned into a caller that got a clean verdict.
    """
    cfg = config or IntradayDrawdownConfig()
    if price is not None and dd_trough is not None:
        raise ValueError(
            "pass exactly one of price / dd_trough (both supplied: ambiguous, and silently picking "
            "one would let two readings of the same quantity disagree without a trace)"
        )

    high = _finite_positive(hwm_intra)
    reference = _finite_positive(reference_price)
    echoed_price = price if isinstance(price, numbers.Real) and not isinstance(price, bool) else None
    echoed_dd = (dd_trough if isinstance(dd_trough, numbers.Real) and not isinstance(dd_trough, bool)
                 else None)

    def _insufficient(status_reason: str) -> IntradayDrawdownDecision:
        return IntradayDrawdownDecision(
            multiplier=MULTIPLIER_UNCHANGED,
            drawdown=None,
            status=IntradayDrawdownStatus.INSUFFICIENT_DATA.value,
            reason=status_reason,
            hwm_intra=float(high) if high is not None else None,
            price=float(echoed_price) if echoed_price is not None else None,
            dd_trough=float(echoed_dd) if echoed_dd is not None else None,
            reference_price=reference,
            dd_source="none",
        )

    if hwm_intra is None or (isinstance(hwm_intra, numbers.Real) and not isinstance(hwm_intra, bool)
                             and not math.isfinite(float(hwm_intra))):
        return _insufficient(
            "INSUFFICIENT_DATA: hwm_intra is missing or not finite; the drawdown denominator does "
            "not exist, so exposure is unchanged (this is NOT 'not triggered')"
        )
    if high is None:
        return _insufficient(
            f"INSUFFICIENT_DATA: hwm_intra={hwm_intra!r} is not a usable positive number (a "
            "zero/negative high-water mark makes the ratio undefined); exposure unchanged"
        )
    if reference is not None and high < reference:
        return _insufficient(
            f"INSUFFICIENT_DATA: hwm_intra={high!r} is below the reference price "
            f"({REFERENCE_PRICE_SEMANTICS}) {reference!r}, so the anchor is not a carried peak; "
            "exposure unchanged"
        )

    if price is not None:
        drawdown = compute_intraday_drawdown(high, price)
        source = "price"
        if drawdown is None:
            return _insufficient(
                f"INSUFFICIENT_DATA: price={price!r} is not a usable positive number (or exceeds "
                "hwm_intra, which would make the 'drawdown' positive); exposure unchanged"
            )
    elif dd_trough is not None:
        source = "dd_trough"
        try:
            drawdown = float(dd_trough)
        except (TypeError, ValueError):
            return _insufficient(
                f"INSUFFICIENT_DATA: dd_trough={dd_trough!r} is not a number; exposure unchanged"
            )
        if not math.isfinite(drawdown):
            return _insufficient(
                f"INSUFFICIENT_DATA: dd_trough={drawdown!r} is not finite; exposure unchanged"
            )
    else:
        return _insufficient(
            "INSUFFICIENT_DATA: neither price nor dd_trough was supplied, so there is no trough "
            "reading at all; exposure unchanged (this is NOT 'not triggered')"
        )

    status, multiplier, reason = classify_intraday_drawdown(drawdown, cfg)
    return IntradayDrawdownDecision(
        multiplier=multiplier,
        drawdown=float(drawdown),
        status=status,
        reason=reason,
        hwm_intra=float(high),
        price=float(echoed_price) if echoed_price is not None else None,
        dd_trough=float(echoed_dd) if echoed_dd is not None else None,
        reference_price=reference,
        dd_source=source,
    )


__all__ = [
    "DECISION_SPEC_VERSION",
    "DEFAULT_DEEP_DRAWDOWN_THRESH",
    "DEFAULT_LIGHT_DRAWDOWN_THRESH",
    "DEFAULT_MEDIUM_DRAWDOWN_THRESH",
    "MULTIPLIER_DEEP",
    "MULTIPLIER_LIGHT",
    "MULTIPLIER_MEDIUM",
    "MULTIPLIER_UNCHANGED",
    "REFERENCE_PRICE_SEMANTICS",
    "STATUSES",
    "IntradayDrawdownConfig",
    "IntradayDrawdownDecision",
    "IntradayDrawdownStatus",
    "anchor_hwm_intra",
    "classify_intraday_drawdown",
    "compute_intraday_drawdown",
    "evaluate_intraday_drawdown",
]

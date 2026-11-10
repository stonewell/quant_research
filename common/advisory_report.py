"""Shared advisory block builder for the `AT7` advisory-layer hookup (`P1`/`P2`/`P3`).

WHY THIS MODULE EXISTS
======================
`AT7` (`docs/development_plan.md` §2.24) wires the already-delivered advisory
layers (`AT5` alert matrix, `AT4` capacity labels, `R1` intraday de-risk) into
the live chain in three separately-approved passes:

* **P1** -- report fields only: write the block into the Stage-1 report and the
  Stage-2 ticket `detail_reason`. **Zero order impact.**
* **P2** -- shadow: compute "what the multiplier would have been" next to the
  real sizing path, without changing any order.
* **P3** -- enforcement (frozen as multiplicative: `effective = real_buy_scale
  × r1.multiplier`, buy increments only, sells/cash untouched).

If each pass built its own dict this would fork into three shapes that a reader
could not compare across days. This module is the single builder; every pass
calls it with more inputs and nothing in the shape changes.

ADVISORY-ONLY FENCE
-------------------
Every block carries `advisory: True` and `gates_orders: False`. **No order is
gated, resized, delayed or re-priced by anything in this module.** In `P1` the
block is write-only: `scripts/run_live_strategy.py` stores it under
`detail_reason["advisory"]`, and no sizing function (neither
`apply_intraday_distribution_gate`, the purchasing-power rescale, nor
`apply_market_value_cap`) ever reads it back.

WHY P1 LEGITIMATELY RENDERS `INSUFFICIENT` / `MISSING` TODAY
------------------------------------------------------------
This is the honest state of the live chain, not a bug and not something to be
filled in with a plausible-looking number:

* **No cross-sectional score source exists in the live chain.** The only
  per-symbol, per-strategy numbers Stage 2 writes are
  `detail_reason.dimension_signals`, which are sub-strategy weights inside the
  already-held book -- selection-truncated and already portfolio weights, so
  feeding them to an IC calculation would be circular. See
  `common/signal_snapshot.py:12-38` for the full reasoning and the deferred
  producer contract.
* **No executed-notional ledger exists**, so turnover cannot be measured from
  the live run; and participation is tri-state with no cut-off defined here.
  `common/live_capacity.py` reports both as `MISSING` rather than zero- or
  mean-filling them.

Consequently `P1` always calls the builder with `leg_reports=()`,
`turnover=None` and `participation=None`: the matrix level is `INSUFFICIENT`
and both capacity labels are `MISSING`. An empty input makes the matrix
`INSUFFICIENT` by construction (`signal_alerts.combine_levels`), so it can never
be downgraded to `GREEN`/normal.

NO THRESHOLD LITERALS HERE
--------------------------
The `R1` (AT5/R1) bands and the `AT4` `L2.36` 8.0x/10.0x bands live in their own
modules and are consumed, never re-declared. This module contains **no numeric
threshold literal at all** -- the only numbers it can emit are those handed to
it by a caller.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Optional

from common.live_capacity import (
    PARTICIPATION_LABEL_MISSING,
    TURNOVER_LABEL_MISSING,
    capacity_labels,
)
from common.signal_alerts import build_alert_matrix

__all__ = [
    "ADVISORY_PHASE_P1",
    "ADVISORY_PHASE_P2",
    "ADVISORY_PHASE_P3",
    "ADVISORY_PHASES",
    "build_advisory_block",
    "r1_decision_to_dict",
]

ADVISORY_PHASE_P1 = "P1"
ADVISORY_PHASE_P2 = "P2"
ADVISORY_PHASE_P3 = "P3"
ADVISORY_PHASES = (ADVISORY_PHASE_P1, ADVISORY_PHASE_P2, ADVISORY_PHASE_P3)


def _json_number(value: Any) -> Optional[float]:
    """`float` iff finite, else `None` -- a strict-JSON report never carries NaN/Infinity.

    Same rule as `signal_alerts._json_number`: unknown is `None`, never a
    plausible-looking zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_of_text(as_of: Any) -> Optional[str]:
    """Render any clock/date object as text; never parse it, never do arithmetic."""
    if as_of is None:
        return None
    return str(as_of)


def r1_decision_to_dict(decision: Any) -> Dict[str, Any]:
    """Pure field extraction from `common.intraday_drawdown_derisk.IntradayDrawdownDecision`.

    The decision dataclass has no `to_dict()` (deliberately -- it is an internal
    judgement, not a report object), so the JSON view is assembled here. Only
    public fields are read; no threshold is re-derived.

    `drawdown` stays `None` when the module could not measure it (`R1` never
    writes `0.0` for "unmeasurable", which would read as "measured, sitting
    exactly at the high"). `advisory` / `gates_orders` are re-asserted here so a
    consumer that only keeps this dict still sees the round-1 fence.
    """
    return {
        "advisory": True,
        "gates_orders": False,
        "status": getattr(decision, "status", None),
        "multiplier": _json_number(getattr(decision, "multiplier", None)),
        "drawdown": _json_number(getattr(decision, "drawdown", None)),
        "reason": getattr(decision, "reason", None),
        "dd_source": getattr(decision, "dd_source", None),
    }


def build_advisory_block(
    *,
    phase: str,
    leg_reports: Iterable[Any] = (),
    turnover: Any = None,
    participation: Any = None,
    as_of: Any = None,
    r1: Any = None,
    real_buy_scale: Any = None,
) -> Dict[str, Any]:
    """Build the one advisory dict every pass (`P1`/`P2`/`P3`) writes.

    Parameters
    ----------
    phase:
        `ADVISORY_PHASE_P1` / `_P2` / `_P3`. Anything else raises -- the phase
        is the machine-readable record of how far the contract was approved.
    leg_reports:
        `AT3` `LegICReport` objects. Empty means "no leg measurement at all"
        and renders `INSUFFICIENT`.
    turnover / participation:
        `AT4` observation objects, or `None`. `None` renders the explicit
        `MISSING` label -- never `0.0`, never a mean.
    as_of:
        Session/date string echoed into the report (never parsed, never used
        arithmetically -- this module has no clock).
    r1:
        An `IntradayDrawdownDecision` (converted via `r1_decision_to_dict`) or
        an already-built dict. Optional; `P1` passes nothing.
    real_buy_scale:
        The buy-side scale that actually drove the ticket. Optional; `P1`
        passes nothing. When both it and an R1 multiplier are present, the
        frozen `P3` composition (multiplicative) is reported as
        `shadow_buy_scale`, a diary entry only.

    Returns
    -------
    A strictly JSON-native dict: no numpy/pandas scalars, no NaN/Infinity, no
    `Timestamp` (the Stage-2 ticket writer uses `json.dump(...,
    ensure_ascii=False)` **without** `default=str`/`allow_nan=False`).
    """
    if phase not in ADVISORY_PHASES:
        raise ValueError(f"phase must be one of {ADVISORY_PHASES}, got {phase!r}")

    as_of_text = _as_of_text(as_of)

    capacity = capacity_labels(turnover=turnover, participation=participation)
    matrix = build_alert_matrix(
        leg_reports=leg_reports or (),
        capacity=capacity,
        as_of=as_of_text,
    )
    matrix_dict = matrix.to_dict()

    # `AT4` is the owner of both labels. When there is no source it already
    # says MISSING; the `or` is a shape guard so this block can never emit a
    # null where the contract promises a tri-state label string.
    turnover_label = capacity.get("turnover_label") or TURNOVER_LABEL_MISSING
    participation_label = capacity.get("participation_label") or PARTICIPATION_LABEL_MISSING

    block: Dict[str, Any] = {
        "advisory": True,
        "gates_orders": False,
        "phase": phase,
        "as_of": as_of_text,
        "alert_matrix": matrix_dict,
        "turnover_label": turnover_label,
        "participation_label": participation_label,
        "worst_measured_level": matrix_dict.get("worst_measured_level"),
        # `P1` never lets the multiplier reach a sizing path. Later passes flip
        # this only together with the user's explicit approval.
        "r1_effective": False,
    }

    if r1 is not None:
        block["r1"] = r1 if isinstance(r1, dict) else r1_decision_to_dict(r1)

    buy_scale = _json_number(real_buy_scale)
    if buy_scale is not None:
        block["real_buy_scale"] = buy_scale
        # Frozen `P3` composition semantics (user ruling 2026-11-09):
        # effective = real_buy_scale × r1.multiplier. Reported here as a shadow
        # value only -- nothing consumes it.
        multiplier = _json_number((block.get("r1") or {}).get("multiplier"))
        if multiplier is not None:
            block["shadow_buy_scale"] = _json_number(buy_scale * multiplier)

    return block

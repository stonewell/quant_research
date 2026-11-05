"""Canonical live **execution-timing** vocabulary (`LV2`, 2026-11-03).

WHY THIS MODULE EXISTS
----------------------
A live order ticket is a list of *prices and share counts*, not a schedule.  Nothing on the
ticket said WHICH session those numbers execute in, so the same file could be read two ways:

* the operator reading ``local_now = 2026-11-03T05:53+08:00`` (pre-open, after the previous
  close) believes the prices are yesterday's close and the orders execute at *today's open*;
* a reader who trusts the Stage-1 report header ("Generated during the market-local
  13:55 – 14:20 inspection window") believes the same prices are *today's* intraday marks
  and the orders are *today's* close.

Both readings existed in the archive simultaneously (two archived rounds: `summary.json`
`local_now` = `2026-11-02T02:02:37+08:00` / `2026-11-03T05:53:40+08:00`, while
`stage1_health_report.md` L8 claimed the 13:55–14:20 window on both days).  The ticket's
price cells were `as_of` closes in every case, so the *correct* reading is "close-price
decision -> next session's open fill", and the header was simply lying.

FROZEN VOCABULARY (do not add a third spelling anywhere)
-------------------------------------------------------
``execution_session``:
  * ``as_of``     -- the orders execute inside the session the prices were taken from;
  * ``as_of+1``   -- the orders execute in the NEXT trading session.

``price_reference``:
  * ``close``     -- the ticket's ``price`` column is that session's close;
  * ``next_open`` -- the ticket's ``price`` is a close, but the executable price is the
    next session's OPEN (the decision and the fill are in different sessions).

The value domain is aligned with the backtester's ``EXECUTION_PRICE_MODES``
(``common/allocation_backtester.py``) so research and live never speak two dialects.

USER RULING (2026-11-03, `docs/development_plan.md` §4.29 and the `LV2` row) -- READ THIS
BEFORE "FIXING" IT
---------------------------------------------------------------------------------
`assert_run_window` (`common/cli_utils.py:228`) resolves ``as_of`` to the last **closed**
session, always: an in-window intraday run still values on the previous published close
(today's unfinished daily bar must never be marked to).  So a live ticket is *never* a
same-session execution, and the plan's original acceptance ("in-window => ``as_of``") was
replaced by:

    execution_session is ALWAYS ``as_of+1``;  price_reference is ALWAYS ``next_open``.

Being inside or outside the design run window changes **only** the provenance of the report
header (and therefore the overnight-gap hint), never the two enum values.  Do not reintroduce
an "in-window => as_of" branch: it would re-create the very contradiction this module removes.

The header rule that goes with it: a run **outside** the design window must not print the
window at all (that string is what made the archive lie); a run inside it may name the window
as provenance.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Frozen enum domain
# ---------------------------------------------------------------------------

EXECUTION_SESSION_AS_OF = "as_of"
EXECUTION_SESSION_AS_OF_NEXT = "as_of+1"
EXECUTION_SESSIONS: Tuple[str, ...] = (
    EXECUTION_SESSION_AS_OF,
    EXECUTION_SESSION_AS_OF_NEXT,
)

PRICE_REFERENCE_CLOSE = "close"
PRICE_REFERENCE_NEXT_OPEN = "next_open"
PRICE_REFERENCES: Tuple[str, ...] = (PRICE_REFERENCE_CLOSE, PRICE_REFERENCE_NEXT_OPEN)

#: The two values every live ticket carries (user ruling -- see the module docstring).
FROZEN_EXECUTION_SESSION = EXECUTION_SESSION_AS_OF_NEXT
FROZEN_PRICE_REFERENCE = PRICE_REFERENCE_NEXT_OPEN

#: Provenance of the report header. `in_window` is the ONLY value allowed to name the
#: design window in human-facing text.
PROVENANCE_IN_WINDOW = "in_window"
PROVENANCE_OUTSIDE_WINDOW = "outside_window"
PROVENANCE_REPLAY = "replay"
PROVENANCE_NOT_APPLICABLE = "not_applicable"

#: Ticket envelope. The order rows stay exactly as they were (a list of dicts, one per
#: symbol); the timing fields become their SIBLINGS under this envelope rather than extra
#: keys on every row. `ticket_order_rows` is the single normalizer every reader must use so
#: that a pre-LV2 ticket (a bare list, still present in every archived run) keeps working.
TICKET_SCHEMA_VERSION = "live_trading_ticket/v2"
TICKET_ORDERS_KEY = "orders"
TICKET_SCHEMA_KEY = "ticket_schema"

#: Written next to the ticket, and merged into `summary.json`, so a consumer that only knows
#: the pre-LV2 list shape still has a machine-readable place to read the timing from.
EXECUTION_TIMING_FILENAME = "execution_timing.json"


class ExecutionTimingError(ValueError):
    """Raised when a required timing enum is missing or out of domain.

    Deliberately a `ValueError` (unlike the run-window refusal in `common/cli_utils.py`,
    which is an operational-policy error): these are malformed *values*, and ticket
    generation must abort on them rather than default one in.
    """


def validate_execution_session(value: Any) -> str:
    """Return `value` if it is a member of `EXECUTION_SESSIONS`, else raise."""
    if value is None or value == "":
        raise ExecutionTimingError(
            "execution_session is required on a live ticket (domain: "
            f"{EXECUTION_SESSIONS}); got {value!r}. Never default it -- a defaulted session "
            "is exactly the ambiguity LV2 removes."
        )
    text = str(value)
    if text not in EXECUTION_SESSIONS:
        raise ExecutionTimingError(
            f"execution_session={text!r} is out of domain {EXECUTION_SESSIONS}"
        )
    return text


def validate_price_reference(value: Any) -> str:
    """Return `value` if it is a member of `PRICE_REFERENCES`, else raise."""
    if value is None or value == "":
        raise ExecutionTimingError(
            "price_reference is required on a live ticket (domain: "
            f"{PRICE_REFERENCES}); got {value!r}. Never default it."
        )
    text = str(value)
    if text not in PRICE_REFERENCES:
        raise ExecutionTimingError(
            f"price_reference={text!r} is out of domain {PRICE_REFERENCES}"
        )
    return text


def validate_execution_timing(
    execution_session: Any, price_reference: Any
) -> Tuple[str, str]:
    """Validate both enums together; returns the normalized pair."""
    return (
        validate_execution_session(execution_session),
        validate_price_reference(price_reference),
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _window_label(window: Any) -> Optional[str]:
    """`"13:55 – 14:20"` for an `(open, close)` time pair, else None."""
    try:
        start, end = window
        return f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}"
    except Exception:
        return None


def _next_trading_session(clock: Any, as_of: Optional[str]) -> Optional[str]:
    """Best-effort next trading day AFTER `as_of` using the market's own calendar.

    Returns None when the calendar is unavailable (synthetic fixtures, no clock) -- the
    caller must render the hint without a concrete date rather than guess one.
    """
    if clock is None or not as_of:
        return None
    try:
        day = _dt.date.fromisoformat(str(as_of))
        for _ in range(14):
            day = day + _dt.timedelta(days=1)
            if clock.is_trading_day(day):
                return day.isoformat()
    except Exception:
        return None
    return None


def resolve_execution_timing(
    clock_ctx: Any = None,
    *,
    as_of: Optional[str] = None,
    run_mode: str = "live",
) -> Dict[str, Any]:
    """Build this run's timing block, plus the provenance a report header may quote.

    `clock_ctx` is duck-typed (any object carrying `window_gate` / `window` /
    `local_now()` / `clock`, i.e. `common/cli_utils.LiveClockContext`), so both live stages
    and the offline tests can call this without constructing an argparse namespace.

    Per the user ruling the two enums are FROZEN; the only thing that varies is `provenance`
    and the human text built from it.  See the module docstring before changing that.
    """
    gate = str(getattr(clock_ctx, "window_gate", "") or "")
    window = getattr(clock_ctx, "window", None)
    window_label = _window_label(window) if window is not None else None
    as_of_value = as_of if as_of is not None else getattr(clock_ctx, "as_of_date", None)
    as_of_value = str(as_of_value) if as_of_value else None

    local_now: Optional[str] = None
    try:
        local_now = clock_ctx.local_now().isoformat()
    except Exception:
        local_now = None
    session_state: Optional[str] = None
    try:
        session_state = clock_ctx.clock.session_state().value
    except Exception:
        session_state = None

    if run_mode == "replay" or gate == "not_applicable_replay":
        provenance = PROVENANCE_REPLAY
    elif gate == "in_window":
        provenance = PROVENANCE_IN_WINDOW
    elif gate in ("not_applicable", "not_applicable_non_trading", ""):
        # Weekend / holiday / post-close: there is no continuous session to be early or
        # late for, so this is "not applicable", NOT "in window".
        provenance = PROVENANCE_NOT_APPLICABLE
    else:
        # `intraday_allowed` / `downgraded_outside_window` / anything else unknown: treat as
        # outside. Being generous here would re-create the false "in window" claim.
        provenance = PROVENANCE_OUTSIDE_WINDOW

    in_window = provenance == PROVENANCE_IN_WINDOW
    timing: Dict[str, Any] = {
        "execution_session": FROZEN_EXECUTION_SESSION,
        "price_reference": FROZEN_PRICE_REFERENCE,
        "as_of": as_of_value,
        "execution_date": _next_trading_session(getattr(clock_ctx, "clock", None), as_of_value),
        "run_mode": run_mode,
        "window_gate": gate or None,
        "in_window": in_window,
        "window_label": window_label,
        "session_state": session_state,
        "local_now": local_now,
        "provenance": provenance,
    }
    timing["header_text"] = execution_window_header(timing)
    timing["overnight_gap_hint"] = overnight_gap_hint(timing)
    return timing


def execution_window_header(timing: Mapping[str, Any]) -> str:
    """One human-readable line describing WHEN this run actually happened.

    The 2026-11-02/11-03 archive lied here: both reports claimed the 13:55 – 14:20 window
    while the runs were pre-open (02:02 / 05:53 market-local).  Hence the rule enforced by
    `test_execution_session_labeling.py`: the window string may appear ONLY when the run was
    genuinely inside it.
    """
    timing = timing or {}
    label = timing.get("window_label")
    local_now = timing.get("local_now") or "unknown instant"
    gate = timing.get("window_gate") or "unknown"
    provenance = timing.get("provenance")

    if provenance == PROVENANCE_IN_WINDOW and label:
        return (
            f"Generated inside the market-local {label} inspection window "
            f"(at {local_now}, gate={gate})"
        )
    if provenance == PROVENANCE_REPLAY:
        return (
            f"HISTORICAL REPLAY (run_mode=replay) generated at {local_now}; the run-window "
            f"gate does not apply by design (gate={gate})"
        )
    if provenance == PROVENANCE_NOT_APPLICABLE:
        return (
            f"Generated at {local_now} with no continuous session in progress "
            f"(session={timing.get('session_state') or 'unknown'}, gate={gate}); the designed "
            f"inspection window is not applicable"
        )
    return (
        f"Generated OUTSIDE the designed inspection window at {local_now} "
        f"(session={timing.get('session_state') or 'unknown'}, gate={gate})"
    )


def overnight_gap_hint(timing: Mapping[str, Any]) -> str:
    """At-the-time (ATT) warning for the "close decision -> next-open fill" convention.

    This is NOT a slippage model and must never be quoted as evidence that slippage was
    measured (`docs/development_plan.md` LV2 boundary condition 5).  It only says: the
    ticket's price is a CLOSE, the fill is the NEXT OPEN, and the gap between them is
    unmodelled -- so a limit order derived from the close is already stale at the fill.
    """
    timing = timing or {}
    as_of = timing.get("as_of") or "as_of"
    execution_date = timing.get("execution_date")
    target = f"{execution_date} 开盘" if execution_date else "次一交易日开盘"
    return (
        f"口径: 票面价格是 {as_of} 收盘价, 成交时点是 {target} "
        f"(execution_session={timing.get('execution_session') or FROZEN_EXECUTION_SESSION}, "
        f"price_reference={timing.get('price_reference') or FROZEN_PRICE_REFERENCE})。"
        "决策与成交跨了一个隔夜跳空: 该跳空**未被建模**, 不是「已含滑点」的证据; "
        "按收盘价推导的限价单在开盘时可能已失效。"
    )


# ---------------------------------------------------------------------------
# Ticket envelope (LV2) -- readers must go through `ticket_order_rows`
# ---------------------------------------------------------------------------


def ticket_order_rows(payload: Any) -> List[Dict[str, Any]]:
    """Order rows from a ticket payload of EITHER shape.

    * pre-LV2 archive: a bare list of row dicts -> returned as-is;
    * LV2 envelope: ``{"orders": [...], "execution_session": ...}`` -> its `orders` list.

    Every reader must call this instead of ``isinstance(payload, list)``: the envelope exists
    because LV2 needs the two enums at the ticket's top level, and a reader that keeps
    assuming a bare list would silently see "no ticket" for every new run.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, Mapping):
        rows = payload.get(TICKET_ORDERS_KEY)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def build_ticket_payload(
    orders: Sequence[Mapping[str, Any]], timing: Mapping[str, Any]
) -> Dict[str, Any]:
    """Wrap `orders` in the LV2 envelope, validating both enums (no defaulting).

    Raises `ExecutionTimingError` when the caller did not resolve a timing block -- that is
    the "两字段缺失或取值越界 => 生成票据时直接报错" acceptance criterion.
    """
    session, reference = validate_execution_timing(
        (timing or {}).get("execution_session"), (timing or {}).get("price_reference")
    )
    return {
        TICKET_SCHEMA_KEY: TICKET_SCHEMA_VERSION,
        "execution_session": session,
        "price_reference": reference,
        "as_of": (timing or {}).get("as_of"),
        "execution_date": (timing or {}).get("execution_date"),
        "run_mode": (timing or {}).get("run_mode"),
        "window_gate": (timing or {}).get("window_gate"),
        "provenance": (timing or {}).get("provenance"),
        "overnight_gap_hint": (timing or {}).get("overnight_gap_hint")
        or overnight_gap_hint(timing or {}),
        TICKET_ORDERS_KEY: [dict(r) for r in orders],
    }


def replace_ticket_orders(payload: Any, orders: Sequence[Mapping[str, Any]]) -> Any:
    """Return `payload` with its order rows replaced, preserving any timing envelope.

    Used by writers that re-serialize a ticket after normalizing stock names: writing a bare
    list back would silently DELETE the LV2 timing fields from an archived run.
    """
    if isinstance(payload, Mapping):
        updated = dict(payload)
        updated[TICKET_ORDERS_KEY] = [dict(r) for r in orders]
        return updated
    return [dict(r) for r in orders]


def merge_timing_into_summary(timing: Mapping[str, Any]) -> Dict[str, Any]:
    """The `summary.json` half of LV2 (the ticket's half lives in `build_ticket_payload`).

    `summary.json` is the per-run key/value store both live stages merge into
    (`common/cli_utils.persist_run_metadata`) and the web layer `update()`s afterwards, so the
    timing survives a Stage-1 -> Stage-2 -> web sequence without a second source of truth.
    """
    session, reference = validate_execution_timing(
        (timing or {}).get("execution_session"), (timing or {}).get("price_reference")
    )
    return {
        "execution_session": session,
        "price_reference": reference,
        "execution_timing": {
            "as_of": (timing or {}).get("as_of"),
            "execution_date": (timing or {}).get("execution_date"),
            "run_mode": (timing or {}).get("run_mode"),
            "window_gate": (timing or {}).get("window_gate"),
            "in_window": (timing or {}).get("in_window"),
            "window_label": (timing or {}).get("window_label"),
            "provenance": (timing or {}).get("provenance"),
            "session_state": (timing or {}).get("session_state"),
            "local_now": (timing or {}).get("local_now"),
            "overnight_gap_hint": (timing or {}).get("overnight_gap_hint"),
        },
    }


def write_execution_timing(output_dir: str, timing: Mapping[str, Any]) -> str:
    """Persist the timing block beside the ticket (import-safe, no web-layer dependency)."""
    import json
    import os

    session, reference = validate_execution_timing(
        (timing or {}).get("execution_session"), (timing or {}).get("price_reference")
    )
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, EXECUTION_TIMING_FILENAME)
    payload = {
        "execution_session": session,
        "price_reference": reference,
        "ticket_schema": TICKET_SCHEMA_VERSION,
        **{
            k: (timing or {}).get(k)
            for k in (
                "as_of",
                "execution_date",
                "run_mode",
                "window_gate",
                "in_window",
                "window_label",
                "provenance",
                "session_state",
                "local_now",
                "header_text",
                "overnight_gap_hint",
            )
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path

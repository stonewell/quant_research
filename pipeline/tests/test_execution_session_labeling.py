"""Offline regression suite for `LV2` -- ticket / report execution-timing alignment.

FROZEN CONTRACT UNDER TEST (owner: `common/live_execution_timing.py`'s module docstring --
read that, not this file, if the two ever disagree):

(a) **Vocabulary.** A live ticket carries exactly two timing enums:
    ``execution_session`` in ``{as_of, as_of+1}`` and ``price_reference`` in
    ``{close, next_open}``. Both are REQUIRED: a missing or out-of-domain value raises at
    TICKET GENERATION time (`ExecutionTimingError`), it is never defaulted in.
(b) **The user ruling (2026-11-03, `docs/development_plan.md` §4.29 / the `LV2` row).**
    ``assert_run_window`` resolves ``as_of`` to the last **closed** session ALWAYS, so a live
    ticket is never a same-session execution:
        ``execution_session`` is ALWAYS ``as_of+1``; ``price_reference`` is ALWAYS
        ``next_open``.
    Being inside vs outside the design run window changes **only** the report's provenance
    and therefore its header text -- never the two enum values. The original plan wording
    ("in-window => ``execution_session = as_of``") was explicitly superseded; do NOT
    reintroduce an "in-window => as_of" branch.
(c) **Header.** The 13:55 -- 14:20 window may be named in human-facing text ONLY by a run
    that was genuinely inside it. The two archived rounds that motivated LV2 ran pre-open
    (02:02 / 05:53 market-local) and still claimed the window -- that is the lie this suite
    pins shut.
(d) **Ticket shape.** ``live_trading_ticket.json`` is a versioned ENVELOPE
    (``{"ticket_schema": ..., "execution_session": ..., "orders": [...]}``). Every archived
    run still holds the pre-LV2 bare list, so every reader must go through
    ``ticket_order_rows`` -- and every writer through ``replace_ticket_orders`` so
    re-serializing rows never silently deletes the timing fields.

100% offline: no network, no real market data, no `data/runs/**`.
"""

import json
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common import live_execution_timing as timing_mod
from common.live_execution_timing import (
    EXECUTION_SESSION_AS_OF,
    EXECUTION_SESSION_AS_OF_NEXT,
    EXECUTION_TIMING_FILENAME,
    FROZEN_EXECUTION_SESSION,
    FROZEN_PRICE_REFERENCE,
    PRICE_REFERENCE_CLOSE,
    PRICE_REFERENCE_NEXT_OPEN,
    PROVENANCE_IN_WINDOW,
    PROVENANCE_NOT_APPLICABLE,
    PROVENANCE_OUTSIDE_WINDOW,
    PROVENANCE_REPLAY,
    TICKET_SCHEMA_VERSION,
    ExecutionTimingError,
    build_ticket_payload,
    execution_window_header,
    merge_timing_into_summary,
    overnight_gap_hint,
    replace_ticket_orders,
    resolve_execution_timing,
    ticket_order_rows,
    validate_execution_session,
    validate_price_reference,
    write_execution_timing,
)

#: The two archived instants LV2 was written against (`summary.json::local_now`):
#: 2026-10-30 dir = 2026-11-02T02:02:37+08:00; 2026-11-02 dir = 2026-11-03T05:53:40+08:00.
#: Both are pre-open and both reports wrongly claimed the 13:55 -- 14:20 window.
_ARCHIVED_PREOPEN_INSTANTS = ("2026-11-02T02:02:37+08:00", "2026-11-03T05:53:40+08:00")
_WINDOW = (types.SimpleNamespace(strftime=lambda f: "13:55"),
           types.SimpleNamespace(strftime=lambda f: "14:20"))


def _clock_ctx(gate, *, local_now="2026-11-03T10:15:00+08:00", as_of="2026-11-03"):
    """Duck-typed stand-in for `common.cli_utils.LiveClockContext` (the real seam)."""

    class _Clock:
        @staticmethod
        def session_state():
            return types.SimpleNamespace(value="open")

        @staticmethod
        def is_trading_day(day):
            # Mon-Fri only -- enough to prove the hint names a CONCRETE next session
            # (2026-11-03 is a Tuesday, so the next trading day is 2026-11-04).
            return day.weekday() < 5

    return types.SimpleNamespace(
        window_gate=gate,
        window=_WINDOW,
        as_of_date=as_of,
        clock=_Clock(),
        local_now=lambda: _dt_fromisoformat(local_now),
    )


def _dt_fromisoformat(text):
    import datetime as _dt

    return _dt.datetime.fromisoformat(text)


def _load_script(module_name, relative_path):
    """Load a standalone script (not an importable package module) via importlib."""
    import importlib.util

    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# (a) Vocabulary -- required, never defaulted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "as_of+2", "same-day", 0])
def test_execution_session_rejects_missing_and_out_of_domain(value):
    """A missing/unknown session must RAISE -- the acceptance criterion is "generate => error".

    re-break: return `EXECUTION_SESSION_AS_OF_NEXT` for a falsy value (a "helpful" default)
    and this goes green-with-a-lie instead of red.
    """
    with pytest.raises(ExecutionTimingError):
        validate_execution_session(value)


@pytest.mark.parametrize("value", [None, "", "open", "prev_close", False])
def test_price_reference_rejects_missing_and_out_of_domain(value):
    with pytest.raises(ExecutionTimingError):
        validate_price_reference(value)


def test_valid_enums_round_trip_unchanged():
    assert validate_execution_session(EXECUTION_SESSION_AS_OF) == "as_of"
    assert validate_execution_session(EXECUTION_SESSION_AS_OF_NEXT) == "as_of+1"
    assert validate_price_reference(PRICE_REFERENCE_CLOSE) == "close"
    assert validate_price_reference(PRICE_REFERENCE_NEXT_OPEN) == "next_open"


# ---------------------------------------------------------------------------
# (b)+(c) Resolution + header provenance
# ---------------------------------------------------------------------------


def test_in_window_run_names_the_window_and_keeps_the_frozen_enums():
    """Acceptance ①: in-window => header names the window AND session == `as_of+1`."""
    timing = resolve_execution_timing(_clock_ctx("in_window"), as_of="2026-11-03")

    assert timing["execution_session"] == FROZEN_EXECUTION_SESSION == "as_of+1"
    assert timing["price_reference"] == FROZEN_PRICE_REFERENCE == "next_open"
    assert timing["provenance"] == PROVENANCE_IN_WINDOW
    assert timing["in_window"] is True
    assert "13:55" in timing["header_text"] and "14:20" in timing["header_text"]


@pytest.mark.parametrize(
    "gate",
    ["downgraded_outside_window", "intraday_allowed", "something_new"],
)
def test_out_of_window_run_must_not_print_the_window_but_keeps_the_frozen_enums(gate):
    """Acceptance ②: out-of-window => NO `13:55` / `14:20` anywhere, same `as_of+1`.

    re-break: hard-code the window string (what the pre-LV2 `generate_reports` did) and the
    two `assert "13:55" not in ...` lines go red.
    """
    timing = resolve_execution_timing(_clock_ctx(gate), as_of="2026-11-03")

    assert timing["provenance"] == PROVENANCE_OUTSIDE_WINDOW
    assert timing["in_window"] is False
    assert "13:55" not in timing["header_text"]
    assert "14:20" not in timing["header_text"]
    assert timing["execution_session"] == "as_of+1"
    assert timing["price_reference"] == "next_open"


@pytest.mark.parametrize("local_now", _ARCHIVED_PREOPEN_INSTANTS)
def test_the_two_archived_preopen_instants_never_claim_the_window(local_now):
    """The actual archived instants, replayed: pre-open must NOT read as "in window"."""
    timing = resolve_execution_timing(
        _clock_ctx("downgraded_outside_window", local_now=local_now),
        as_of="2026-11-02",
    )
    assert "13:55" not in timing["header_text"]
    assert "14:20" not in timing["header_text"]
    assert local_now in timing["header_text"]


def test_not_applicable_and_replay_provenance_are_distinct_from_in_window():
    """Weekend/holiday/post-close is NOT "in window"; a replay is not either."""
    na = resolve_execution_timing(_clock_ctx("not_applicable"), as_of="2026-11-01")
    assert na["provenance"] == PROVENANCE_NOT_APPLICABLE
    assert "13:55" not in na["header_text"]

    replay = resolve_execution_timing(
        _clock_ctx("not_applicable_replay"), as_of="2026-10-01", run_mode="replay"
    )
    assert replay["provenance"] == PROVENANCE_REPLAY
    assert "13:55" not in replay["header_text"]
    assert "REPLAY" in replay["header_text"]


def test_execution_date_is_the_next_trading_session_not_the_as_of():
    """`execution_session=as_of+1` must name a CONCRETE next session when the calendar allows."""
    timing = resolve_execution_timing(_clock_ctx("in_window"), as_of="2026-11-03")
    assert timing["execution_date"] == "2026-11-04"


def test_header_text_is_derived_from_provenance_only():
    """`execution_window_header` must be a pure function of the timing block."""
    timing = resolve_execution_timing(_clock_ctx("in_window"), as_of="2026-11-03")
    assert execution_window_header(timing) == timing["header_text"]


def test_overnight_gap_hint_states_the_convention_without_claiming_slippage():
    """The ATT hint must say "unmodelled gap", and must never read as "slippage included"."""
    hint = overnight_gap_hint(
        {
            "as_of": "2026-11-03",
            "execution_date": "2026-11-04",
            "execution_session": "as_of+1",
            "price_reference": "next_open",
        }
    )
    assert "2026-11-03" in hint and "2026-11-04" in hint
    assert "未" in hint  # 未被建模 / 未含滑点
    assert "滑点" in hint
    assert "execution_session=as_of+1" in hint
    assert "price_reference=next_open" in hint


# ---------------------------------------------------------------------------
# (d) Ticket envelope -- both shapes, and the writers must not drop the timing
# ---------------------------------------------------------------------------


def _timing(as_of="2026-11-03", **overrides):
    timing = resolve_execution_timing(_clock_ctx("in_window"), as_of=as_of)
    timing.update(overrides)
    return timing


def test_ticket_payload_carries_both_enums_and_the_orders():
    orders = [{"symbol": "601872.SH", "action": "BUY", "target_shares": 100}]
    payload = build_ticket_payload(orders, _timing())

    assert payload["ticket_schema"] == TICKET_SCHEMA_VERSION
    assert payload["execution_session"] == "as_of+1"
    assert payload["price_reference"] == "next_open"
    assert payload["as_of"] == "2026-11-03"
    assert payload["execution_date"] == "2026-11-04"
    assert payload["orders"] == orders
    assert payload["overnight_gap_hint"]
    # The order rows themselves are untouched -- no timing key leaks onto a row.
    assert not any("execution_session" in row for row in payload["orders"])


def test_ticket_generation_refuses_a_timing_block_without_both_enums():
    """Acceptance ③: generating a ticket with a missing/out-of-domain field RAISES."""
    with pytest.raises(ExecutionTimingError):
        build_ticket_payload([], {})

    broken = _timing()
    broken["price_reference"] = "vwap"
    with pytest.raises(ExecutionTimingError):
        build_ticket_payload([], broken)


def test_ticket_order_rows_accepts_both_the_envelope_and_the_legacy_bare_list():
    rows = [{"symbol": "A"}, {"symbol": "B"}]
    assert ticket_order_rows(rows) == rows                      # pre-LV2 archive
    assert ticket_order_rows({"orders": rows}) == rows          # LV2 envelope
    assert ticket_order_rows(None) == []
    assert ticket_order_rows({}) == []


def test_replace_ticket_orders_preserves_an_envelope_and_upgrades_a_bare_list():
    timing = _timing()
    payload = build_ticket_payload([{"symbol": "A"}], timing)
    updated = replace_ticket_orders(payload, [{"symbol": "A", "name": "Renamed"}])

    # A bare-list re-serialization would delete the LV2 fields from the archive.
    assert updated["execution_session"] == "as_of+1"
    assert updated["price_reference"] == "next_open"
    assert updated["ticket_schema"] == TICKET_SCHEMA_VERSION
    assert ticket_order_rows(updated) == [{"symbol": "A", "name": "Renamed"}]
    # ... and a legacy bare list stays a bare list (no envelope is invented for it).
    assert replace_ticket_orders([{"symbol": "A"}], [{"symbol": "B"}]) == [{"symbol": "B"}]


def test_summary_merge_carries_the_two_top_level_enums():
    merged = merge_timing_into_summary(_timing())
    assert merged["execution_session"] == "as_of+1"
    assert merged["price_reference"] == "next_open"
    assert merged["execution_timing"]["as_of"] == "2026-11-03"
    assert merged["execution_timing"]["window_label"]
    assert merged["execution_timing"]["overnight_gap_hint"]
    with pytest.raises(ExecutionTimingError):
        merge_timing_into_summary({"execution_session": "as_of"})


def test_timing_sidecar_is_written_and_readable(tmp_path):
    timing = _timing()
    path = write_execution_timing(str(tmp_path), timing)
    assert os.path.basename(path) == EXECUTION_TIMING_FILENAME

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["execution_session"] == "as_of+1"
    assert payload["price_reference"] == "next_open"
    assert payload["ticket_schema"] == TICKET_SCHEMA_VERSION
    assert payload["header_text"] == timing["header_text"]


# ---------------------------------------------------------------------------
# (c) The Stage-1 DASHBOARD must obey the same rule
# ---------------------------------------------------------------------------


def _dashboard_kwargs(timing):
    """Minimal real `dd_info` / `macro_info` / `vol_info` for `print_dashboard`."""
    dd_info = {
        "current_nav": 100000.0,
        "peak_nav": 100000.0,
        "drawdown_pct": 0.0,
        "tier_label": "⚪ NORMAL",
        "linear_equity_scale": 1.0,
        "tier1_counter": 0,
        "tier1_max": 5,
        "tier3_counter": 0,
        "tier3_max": 3,
        "freeze_remaining_bars": 0,
        "auto_healed": False,
        "heal_reason": "",
        "fast_recovery_nav": False,
        "nav_10d_status": "insufficient_history",
        "nav_10d_history_len": 0,
        "nav_source": "mtm",
        "nav_form": 100000.0,
        "nav_deviation_pct": 0.0,
        "nav_deviation_exceeded": False,
        "nav_breakdown_status": "OK",
    }
    macro_info = {
        "breadth": 0.5,
        "breadth_thresh": 0.3,
        "thrust": 0.5,
        "thrust_thresh": 0.6,
        "target_exposure": 0.5,
        "tracked_symbols_count": 2,
        "leaders": [],
        "breadth_active": True,
        "thrust_active": False,
    }
    vol_info = {
        "vol_elevated": False,
        "realized_vol_21d": 0.1,
        "target_vol": 0.12,
        "vol_scaling_factor": 1.0,
    }
    return dict(
        as_of_date="2026-11-03",
        universe_path="/tmp/univ.txt",
        strategy_path="/tmp/strat.json",
        strategy_info={"strategy_name": "X", "template_name": "x", "weighting_mode": "m"},
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code="GO",
        gate_headline="GO",
        actions=["do the thing"],
        timing=timing,
    )


def test_dashboard_withdraws_the_clock_window_outside_the_window(capsys):
    """The Stage-1 dashboard's "act between 14:15 and 14:50" line must not lie.

    It was unconditional, so the two archived PRE-OPEN rounds printed a clock appointment
    that had already passed / not yet arrived. Only an in-window run may name a window; an
    out-of-window one still lists the actions but withdraws the clock promise.
    """
    dash = _load_script("check_live_portfolio_health_dash", os.path.join("scripts", "check_live_portfolio_health.py"))

    inside = resolve_execution_timing(_clock_ctx("in_window"), as_of="2026-11-03")
    dash.print_dashboard(**_dashboard_kwargs(inside))
    out_in_window = capsys.readouterr().out
    assert "MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (14:15 – 14:50):" in out_in_window
    assert "13:55" in out_in_window  # the header may name the window: it was inside it

    outside = resolve_execution_timing(
        _clock_ctx("downgraded_outside_window"), as_of="2026-11-03"
    )
    dash.print_dashboard(**_dashboard_kwargs(outside))
    out_outside = capsys.readouterr().out
    assert "MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (" in out_outside
    assert "(14:15 – 14:50)" not in out_outside
    assert "13:55" not in out_outside and "14:20" not in out_outside
    # The actions themselves are NOT withheld -- only the false clock promise is.
    assert "do the thing" in out_outside


# ---------------------------------------------------------------------------
# Acceptance ④ -- "move the decision instant AFTER the pricing instant => red"
# ---------------------------------------------------------------------------


def test_wrong_enum_domain_is_the_only_thing_this_module_would_let_move():
    """A guard on the RULING itself, so re-adding "in-window => as_of" cannot pass silently.

    The original plan's acceptance asked for `execution_session = as_of` inside the window.
    The user ruling replaced it because `as_of` is always the last CLOSED session -- so the
    in-window and out-of-window branches must produce the SAME pair of enum values. Anyone
    reintroducing the old branch makes one of these two comparisons fail.
    """
    in_window = resolve_execution_timing(_clock_ctx("in_window"), as_of="2026-11-03")
    outside = resolve_execution_timing(
        _clock_ctx("downgraded_outside_window"), as_of="2026-11-03"
    )
    assert in_window["execution_session"] == outside["execution_session"] == "as_of+1"
    assert in_window["price_reference"] == outside["price_reference"] == "next_open"
    # What DOES differ: the provenance, and therefore the human text.
    assert in_window["provenance"] != outside["provenance"]
    assert in_window["header_text"] != outside["header_text"]
    assert set(timing_mod.EXECUTION_SESSIONS) == {"as_of", "as_of+1"}
    assert set(timing_mod.PRICE_REFERENCES) == {"close", "next_open"}

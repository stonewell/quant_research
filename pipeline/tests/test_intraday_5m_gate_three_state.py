"""The 5m intraday gate is THREE-STATE, and "never measured" is never `0.50`.

FORENSIC CONTEXT (defects A + B, 2026-11-12)
-------------------------------------------
Both Stage-2 read sites resolved the buy scale as::

    float(health_report.get("intraday_buy_scale",
                            account_state.get("intraday_buy_scale", 0.50)))

so a MISSING key -- "this run measured nothing" -- silently meant "halve the
buys". That is the opposite of this repo's standing discipline (Stage-1's own
SKIPPED branch writes `intraday_buy_scale = 1.0` + `distribution_gate_status =
"SKIPPED"` instead of fabricating a scale). The sibling defect: a `missing` 5m
layer only PRINTED a warning and never entered the decision, so a run without
5m data still opened new positions (`R7`'s freeze half was never implemented).

WHAT IS PINNED HERE
-------------------
1. switch OFF / nothing engaging the gate -> `not_applicable`, scale 1.0, and the
   target vector is returned as the CALLER'S OWN OBJECT (bit-identical);
2. 5m engaged + `missing` -> `unknown_fail_closed`, scale 0.0, buy increments land
   exactly on `current`, and SELLS / trims stay bit-identical (missing intraday
   data must never block a de-risking sell);
3. 5m engaged + `pending_publication` -> NOT frozen (a 15:30-20:00 pre-open run is
   the NORMAL case), status `pending_publication`, and a measured scale is still
   honoured;
4. 5m engaged + `evaluated` + measured 0.50 -> the legacy distribution scaling,
   byte for byte (`cur + delta * 0.5`);
5. key missing AND the state cannot be determined -> an explicit fail-closed 0.0
   (never 0.50) plus the grep-able `[WARN/INTRADAY GATE]` line;
6. the two read sites share ONE resolver (`resolve_intraday_gate_state`) and no
   `0.50` default survives anywhere in the resolution path;
7. the ticket and `summary.json` land `intraday_gate_status` /
   `intraday_gate_reason` / `intraday_buy_scale_applied`.

100% offline: hand-written pandas Series and dicts, no market data, no network.
"""

import ast
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    INTRADAY_GATE_EVALUATED,
    INTRADAY_GATE_NOT_APPLICABLE,
    INTRADAY_GATE_PENDING_PUBLICATION,
    INTRADAY_GATE_REASON_5M_MISSING,
    INTRADAY_GATE_REASON_PENDING,
    INTRADAY_GATE_REASON_SCALE_MISSING,
    INTRADAY_GATE_UNKNOWN_FAIL_CLOSED,
    apply_intraday_distribution_gate,
    format_intraday_gate_line,
    resolve_intraday_gate_state,
)

_STAGE2_PATH = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")

CASH = "BIL"
CAUTION = "CAUTION_INTRADAY_DISTRIBUTION"


def _book():
    """The same hand-written shape the existing gate tests use.

    `SELL` is a trim (target < current), `BUY` a new open, `ADD` an increase,
    `HOLD` unchanged, and the cash leg must never be touched.
    """
    target = pd.Series(
        {"SELL": 0.10, "BUY": 0.20, "ADD": 0.30, "HOLD": 0.15, CASH: 0.25}
    )
    current = pd.Series(
        {"SELL": 0.20, "BUY": 0.00, "ADD": 0.10, "HOLD": 0.15, CASH: 0.55}
    )
    return target, current


def _source():
    with open(_STAGE2_PATH, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Not applicable -> 1.0, bit-identical
# ---------------------------------------------------------------------------
def test_switch_off_is_not_applicable_and_bit_identical():
    """`enable_intraday_5m` off: no scaling, and the SAME Series object out."""
    target, current = _book()
    state = resolve_intraday_gate_state(
        {"gate_code": "NORMAL"}, {}, intraday_5m_enabled=False
    )

    assert state["intraday_gate_status"] == INTRADAY_GATE_NOT_APPLICABLE
    assert state["intraday_buy_scale_applied"] == 1.0
    assert state["intraday_gate_reason"] == "intraday_5m_not_enabled"

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report={"gate_code": "NORMAL"}, account_state={}
    )
    # Identity: nothing was measured, nothing may be rewritten -- a `cur + delta*1.0`
    # round-trip would already be a (tiny) float change.
    assert out is target
    assert list(out.items()) == list(target.items())

    # Even an intraday directive with no 5m layer engaged must not scale anything:
    # the switch is the authority on whether the 5m gate exists at all.
    fired_but_off = resolve_intraday_gate_state(
        {"gate_code": CAUTION}, {}, intraday_5m_enabled=False
    )
    assert fired_but_off["intraday_gate_status"] == INTRADAY_GATE_NOT_APPLICABLE
    assert fired_but_off["intraday_buy_scale_applied"] == 1.0


# ---------------------------------------------------------------------------
# 2. 5m engaged + missing -> freeze new buys, sells untouched
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "metrics",
    [
        {"status": "missing"},
        # Stage-1's publication axis alone (the defect-B evidence shape).
        {"publication_state": "missing"},
        {"status": "provider_unsupported"},
        {"status": "degraded_stale_intraday_cache"},
        {"status": "degraded_unmeasured_features"},
    ],
)
def test_missing_5m_freezes_new_buys_and_never_sells(metrics):
    target, current = _book()
    report = {"gate_code": "NORMAL", "intraday_5m_metrics": metrics}

    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_UNKNOWN_FAIL_CLOSED
    assert state["intraday_buy_scale_applied"] == 0.0
    assert state["intraday_gate_reason"] == INTRADAY_GATE_REASON_5M_MISSING
    assert state["intraday_5m_status"] is not None

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    # New open + add land exactly on the CURRENT weight (increment = 0).
    assert out["BUY"] == pytest.approx(0.00)
    assert out["ADD"] == pytest.approx(0.10)
    # SELL/trim and HOLD are bit-identical -- missing 5m must never block a sell.
    assert out["SELL"] == target["SELL"] == 0.10
    assert out["HOLD"] == current["HOLD"]
    # The cash leg never enters the loop.
    assert out[CASH] == target[CASH]


def test_missing_5m_does_not_invent_a_0_50_scale():
    """The freeze is 0.0, never the old 0.50 default (defect A's exact bug)."""
    state = resolve_intraday_gate_state(
        {"gate_code": CAUTION, "intraday_5m_metrics": {"status": "missing"}},
        {},
        intraday_5m_enabled=True,
    )
    assert state["intraday_buy_scale_applied"] == 0.0
    assert state["intraday_buy_scale_applied"] != 0.50


# ---------------------------------------------------------------------------
# 3. pending_publication -> NOT frozen
# ---------------------------------------------------------------------------
def test_pending_publication_does_not_freeze():
    """A 15:30-20:00 run is normal: the gate must not lock the book down."""
    target, current = _book()
    report = {
        "gate_code": "NORMAL",
        "intraday_5m_metrics": {
            "status": "pending_publication",
            "degraded_reason_code": "intraday_5m_pending_publication",
            "expected_after": "20:00",
        },
    }

    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_PENDING_PUBLICATION
    assert state["intraday_gate_reason"] == INTRADAY_GATE_REASON_PENDING
    # Default ALLOW value -- not 0.0, and not the old 0.50 either.
    assert state["intraday_buy_scale_applied"] == 1.0

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    assert out is target
    assert list(out.items()) == list(target.items())


def test_pending_publication_honours_a_measured_scale():
    """If a scale WAS measured, pending publication does not discard it."""
    target, current = _book()
    report = {
        "gate_code": CAUTION,
        "intraday_buy_scale": 0.40,
        "intraday_5m_metrics": {"status": "pending_publication"},
    }
    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_PENDING_PUBLICATION
    assert state["intraday_buy_scale_applied"] == pytest.approx(0.40)

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    assert out["BUY"] == pytest.approx(0.20 * 0.40)
    assert out["SELL"] == 0.10


# ---------------------------------------------------------------------------
# 4. evaluated + measured 0.50 -> unchanged legacy behaviour
# ---------------------------------------------------------------------------
def test_evaluated_half_scale_matches_legacy_formula():
    target, current = _book()
    report = {
        "gate_code": CAUTION,
        "intraday_buy_scale": 0.50,
        "intraday_5m_metrics": {
            "status": "evaluated",
            "distribution_alert": True,
            "distribution_gate_status": "EVALUATED",
        },
    }
    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_EVALUATED
    assert state["intraday_buy_scale_applied"] == 0.50
    assert state["intraday_gate_reason"] is None

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    # The formula itself is untouched: cur + delta * scale, sells/holds/cash ignored.
    expected = target.copy()
    for sym in ("BUY", "ADD"):
        cur = float(current[sym])
        expected[sym] = cur + (float(target[sym]) - cur) * 0.50
    assert list(out.items()) == list(expected.items())
    assert out["SELL"] == 0.10
    assert out["HOLD"] == 0.15
    assert out[CASH] == 0.25


def test_evaluated_without_alert_is_a_no_op():
    """Measured 1.0 (no distribution) -> the vector is returned untouched."""
    target, current = _book()
    report = {
        "gate_code": "NORMAL",
        "intraday_buy_scale": 1.0,
        "intraday_5m_metrics": {"status": "evaluated"},
    }
    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_EVALUATED
    assert state["intraday_buy_scale_applied"] == 1.0
    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    assert list(out.items()) == list(target.items())


def test_account_state_scale_is_still_honoured():
    """The `account_state` mirror keeps working as a MEASURED source."""
    state = resolve_intraday_gate_state(
        {"gate_code": CAUTION},
        {"gate_code": CAUTION, "intraday_buy_scale": 0.25},
        intraday_5m_enabled=True,
    )
    assert state["intraday_gate_status"] == INTRADAY_GATE_EVALUATED
    assert state["intraday_buy_scale_applied"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 5. Undeterminable (key missing, no 5m status) -> explicit fail-closed + WARN
# ---------------------------------------------------------------------------
def test_undeterminable_fails_closed_with_warn(capsys):
    """Engaged but nothing measurable: 0.0 + a grep-able WARN -- never 0.50."""
    target, current = _book()
    report = {"gate_code": CAUTION}  # engaged by the directive, nothing measured

    state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
    assert state["intraday_gate_status"] == INTRADAY_GATE_UNKNOWN_FAIL_CLOSED
    assert state["intraday_buy_scale_applied"] == 0.0
    assert state["intraday_gate_reason"] == INTRADAY_GATE_REASON_SCALE_MISSING

    out = apply_intraday_distribution_gate(
        target, current, CASH, health_report=report, account_state={}
    )
    stdout = capsys.readouterr().out
    assert "[WARN/INTRADAY GATE]" in stdout
    assert INTRADAY_GATE_UNKNOWN_FAIL_CLOSED in stdout
    # Explicit fail-closed behaviour: buys frozen, sells intact.
    assert out["BUY"] == pytest.approx(0.0)
    assert out["SELL"] == 0.10

    # And the SAME state, produced by a 5m block whose status carries no scale.
    no_scale = resolve_intraday_gate_state(
        {"gate_code": CAUTION, "intraday_5m_metrics": {"status": "evaluated"}},
        {},
        intraday_5m_enabled=True,
    )
    assert no_scale["intraday_gate_status"] == INTRADAY_GATE_UNKNOWN_FAIL_CLOSED
    assert no_scale["intraday_buy_scale_applied"] == 0.0


def test_non_finite_scale_fails_closed():
    """`NaN`/`inf` is not a measurement: fail closed instead of poisoning weights."""
    target, current = _book()
    for bad in (float("nan"), float("inf")):
        report = {
            "gate_code": CAUTION,
            "intraday_buy_scale": bad,
            "intraday_5m_metrics": {"status": "evaluated"},
        }
        state = resolve_intraday_gate_state(report, {}, intraday_5m_enabled=True)
        assert state["intraday_gate_status"] == INTRADAY_GATE_UNKNOWN_FAIL_CLOSED
        assert state["intraday_buy_scale_applied"] == 0.0
        out = apply_intraday_distribution_gate(
            target, current, CASH, health_report=report, account_state={}
        )
        assert all(pd.notna(out.values))
        assert out["BUY"] == pytest.approx(0.0)


def test_gate_line_names_the_state():
    """The announce line is greppable and states which of the three states applies."""
    for status, scale, marker in (
        (INTRADAY_GATE_NOT_APPLICABLE, 1.0, "[INFO/INTRADAY GATE]"),
        (INTRADAY_GATE_PENDING_PUBLICATION, 1.0, "[INFO/INTRADAY GATE]"),
        (INTRADAY_GATE_EVALUATED, 0.5, "[INFO/INTRADAY GATE]"),
        (INTRADAY_GATE_UNKNOWN_FAIL_CLOSED, 0.0, "[WARN/INTRADAY GATE]"),
    ):
        line = format_intraday_gate_line(
            {
                "intraday_gate_status": status,
                "intraday_gate_reason": "r",
                "intraday_buy_scale_applied": scale,
                "intraday_5m_status": "s",
                "intraday_5m_enabled": True,
            }
        )
        assert line.startswith(marker), line
        assert f"status={status}" in line
        assert f"buy_scale={scale:.2f}" in line


# ---------------------------------------------------------------------------
# 6. Single owner: no duplicated fallback chain, no 0.50 default
# ---------------------------------------------------------------------------
def test_single_resolver_and_no_half_default():
    source = _source()
    tree = ast.parse(source)
    # The exact old defect: a 0.50 default keyed on a MISSING field. Asserted at the
    # AST level (so the forensic COMMENT that quotes the old line does not count):
    # no `.get("intraday_buy_scale", <anything>)` survives anywhere in the module.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "intraday_buy_scale"
        ):
            assert len(node.args) == 1, ast.dump(node)
            assert not node.keywords, ast.dump(node)
    # One resolver, consumed by both sites: its own definition, the gate's
    # self-resolution fallback, and `main()`'s single resolution.
    assert source.count("def resolve_intraday_gate_state(") == 1
    assert source.count("resolve_intraday_gate_state(") == 3
    assert source.count('intraday_gate_state["intraday_buy_scale_applied"]') >= 2


def test_ticket_carries_the_three_explicit_fields():
    source = _source()
    for key in (
        "intraday_gate_status",
        "intraday_gate_reason",
        "intraday_buy_scale_applied",
    ):
        # Ticket top level.
        assert f'ticket_payload["{key}"]' in source, key
        # summary.json (merged through `persist_run_metadata`).
        assert f'"{key}": intraday_gate_state' in source, key

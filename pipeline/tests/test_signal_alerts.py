"""Offline regression suite for `common/signal_alerts.py` (development_plan `AT5`).

`AT5` (`docs/development_plan.md:411`) asks for the signal-layer Yellow / Orange /
Red advisory matrix, with the `指标 -> 层级 -> 动作` mapping **frozen first**, no
reuse/rewrite of any existing threshold (drawdown 10/15/20, `LV1`, `R1`, `R4`,
`R6`, `L2.36` 8.0/10.0), data gaps passed through as `INSUFFICIENT_HISTORY`
(never downgraded to "正常"), and report text only -- no de-risking, no
liquidation, nothing wired into the order path.

What these tests pin, in order:

1. the frozen table in `docs/signal_alerts.md` and `FROZEN_MAPPING` still agree
   row for row (doc <-> code anti-drift);
2. every mapping row has at least one table-driven case, including the exact
   band edges (`-0.50`, `-0.25`, `0`) and the AT4 label boundaries;
3. aggregation: worst severity wins; empty input and all-`INSUFFICIENT` input are
   `INSUFFICIENT`; an `INSUFFICIENT` mixed with a normal level is `INSUFFICIENT`
   with the missing dimension named (完整性优先于严重度, and the worst *measured*
   level stays visible in `worst_measured_level`);
4. `to_dict()` is strict JSON: `json.dumps(..., allow_nan=False)` succeeds and no
   NaN / Infinity survives anywhere in the tree;
5. the advisory fence: `advisory=True`, `gates_orders=False`, and constructing a
   matrix with `gates_orders=True` (or `advisory=False`) raises;
6. AST guards: no `scripts` / `web` / `subprocess` / `requests` import, no IO
   builtin, and **no** import of `cs_dispersion_exposure` /
   `intraday_drawdown_derisk` / `live_capacity` / `ic_decay_monitor` -- AT5 reads
   their public objects/labels, it does not link against their thresholds;
7. AST float-literal scan: the module's *entire* float vocabulary is
   `{-0.50, -0.25, 0.0}` and therefore contains none of `8.0`, `10.0`, `0.75`,
   `0.50`, `0.10`, `0.15`, `0.20`, `-0.02` -- the "no hidden reuse" claim is
   asserted, not narrated.

100% offline: hand-written objects, no network, no real market data, no writes
(the doc read is a read-only provenance check on a repo file).
"""

import ast
import json
import os
import sys
from dataclasses import dataclass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common import signal_alerts as sa
from common.signal_alerts import (
    ALERT_LEVEL_GREEN,
    ALERT_LEVEL_INSUFFICIENT,
    ALERT_LEVEL_ORANGE,
    ALERT_LEVEL_RED,
    ALERT_LEVEL_YELLOW,
    ALERT_LEVELS,
    AdvisoryContractError,
    AlertInputError,
    AlertMatrix,
    FROZEN_MAPPING,
    alert_for_capacity,
    alert_for_leg,
    build_alert_matrix,
    combine_levels,
    severity_rank,
)

_MODULE_PATH = os.path.join(_REPO_ROOT, "common", "signal_alerts.py")
_DOC_PATH = os.path.join(_REPO_ROOT, "docs", "signal_alerts.md")

# The existing threshold numerals AT5 must NOT have borrowed (AT5 clause 2).
_EXISTING_THRESHOLDS = {8.0, 10.0, 0.75, 0.50, 0.10, 0.15, 0.20, -0.02}


# ---------------------------------------------------------------------------
# fixtures / helpers (hand-written, no files, no network)
# ---------------------------------------------------------------------------
@dataclass
class FakeLeg:
    """Stand-in for `ic_decay_monitor.LegICReport`'s two public fields."""

    status: str = "OK"
    ic_ir: float = 0.10
    market: str = "us"
    universe_id: str = "us_core"
    strategy: str = "momentum"


def cap(turnover="OK", avail=True, daily=False, average=False, label=None):
    """The flat dict `live_capacity.capacity_labels(...)` produces."""
    return {
        "turnover_label": turnover,
        "participation_label": label if label is not None else ("OBSERVED" if avail else "MISSING"),
        "participation_available": avail,
        "participation_exceeds_daily_volume": daily if avail else None,
        "participation_exceeds_average_volume": average if avail else None,
    }


def _module_source():
    with open(_MODULE_PATH, encoding="utf-8") as fh:
        return fh.read()


def _doc_source():
    with open(_DOC_PATH, encoding="utf-8") as fh:
        return fh.read()


def _signed_float_constants():
    """Every float literal in the module, with a leading unary minus folded in."""
    tree = ast.parse(_module_source())
    negated = set()
    values = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, float)
        ):
            values.append(-node.operand.value)
            negated.add(id(node.operand))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and id(node) not in negated
        ):
            values.append(node.value)
    return values


def _json_tree_numbers(obj):
    """Every number in a JSON-ready tree (dicts / lists / scalars)."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _json_tree_numbers(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _json_tree_numbers(value)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield obj


# ---------------------------------------------------------------------------
# 1. The frozen mapping: doc <-> code, row for row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("row", FROZEN_MAPPING, ids=lambda row: row["code"])
def test_frozen_mapping_rows_are_recorded_verbatim_in_the_doc(row):
    """Every frozen row's condition AND action text exists in the frozen doc."""
    doc = _doc_source()
    assert row["source"] in doc
    assert row["input"] in doc
    assert row["condition"] in doc, row
    assert row["action"] in doc, row
    assert row["level"] in doc
    # ...and the row is an actual member of the level vocabulary.
    assert row["level"] in sa.ALERT_LEVELS_ALL


def test_frozen_mapping_covers_every_documented_row_once():
    codes = [row["code"] for row in FROZEN_MAPPING]
    assert len(codes) == len(set(codes)) == 10, codes
    # 5 AT3 IC rows + 3 AT4 turnover rows + 2 AT4 participation rows.
    assert sum(row["source"] == "AT3" for row in FROZEN_MAPPING) == 5
    assert sum(row["source"] == "AT4" for row in FROZEN_MAPPING) == 5


# ---------------------------------------------------------------------------
# 2. Every mapping row, table-driven, including band edges
# ---------------------------------------------------------------------------
LEG_CASES = [
    # (status, ic_ir, expected level, expected code / action key)
    ("INSUFFICIENT_HISTORY", -0.90, ALERT_LEVEL_INSUFFICIENT, "leg_status_insufficient_history"),
    ("OK", -0.60, ALERT_LEVEL_RED, "leg_ic_ir_red"),
    ("OK", -0.50, ALERT_LEVEL_RED, "leg_ic_ir_red"),          # exact lower edge -> RED
    ("OK", -0.5000001, ALERT_LEVEL_RED, "leg_ic_ir_red"),
    ("OK", -0.4999999, ALERT_LEVEL_ORANGE, "leg_ic_ir_orange"),
    ("OK", -0.25, ALERT_LEVEL_ORANGE, "leg_ic_ir_orange"),    # exact edge -> ORANGE
    ("OK", -0.2499999, ALERT_LEVEL_YELLOW, "leg_ic_ir_yellow"),
    ("OK", -0.0001, ALERT_LEVEL_YELLOW, "leg_ic_ir_yellow"),
    ("OK", 0.0, ALERT_LEVEL_GREEN, "leg_ic_ir_green"),        # >= 0 -> GREEN
    ("OK", 0.42, ALERT_LEVEL_GREEN, "leg_ic_ir_green"),
]


@pytest.mark.parametrize("status, ic_ir, level, code", LEG_CASES)
def test_alert_for_leg_table(status, ic_ir, level, code):
    item = alert_for_leg(FakeLeg(status=status, ic_ir=ic_ir))
    assert item.level == level
    assert item.code == code
    assert item.dimension == "leg_ic"
    assert item.source == "AT3"
    assert item.subject == "us:us_core:momentum"
    expected_action = next(row["action"] for row in FROZEN_MAPPING if row["code"] == code)
    assert item.action == expected_action
    # The action is report text, never an instruction.
    assert item.advisory is True and item.gates_orders is False


def test_alert_for_leg_passes_through_gaps_and_never_reads_nan_as_normal():
    nan = float("nan")
    # status insufficient wins even when ic_ir looks good or looks red
    assert alert_for_leg(FakeLeg(status="INSUFFICIENT_HISTORY", ic_ir=0.9)).level == ALERT_LEVEL_INSUFFICIENT
    # status OK but ic_ir unmeasurable -> INSUFFICIENT (NaN must not fall into ">= 0")
    for bad in (nan, float("inf"), float("-inf"), None, "n/a"):
        item = alert_for_leg(FakeLeg(status="OK", ic_ir=bad))
        assert item.level == ALERT_LEVEL_INSUFFICIENT, bad
        assert item.missing == ("leg_ic",)
        assert item.observed is None
    # unknown / absent status -> INSUFFICIENT, never a guessed band
    assert alert_for_leg(FakeLeg(status="SOMETHING_NEW", ic_ir=-0.9)).level == ALERT_LEVEL_INSUFFICIENT
    assert alert_for_leg(object()).level == ALERT_LEVEL_INSUFFICIENT


CAPACITY_CASES = [
    # (kwargs for `cap`, expected overall level, expected code, expected missing)
    (dict(turnover="BREACH"), ALERT_LEVEL_ORANGE, "turnover_breach", ()),
    (dict(turnover="WATCH"), ALERT_LEVEL_YELLOW, "turnover_watch", ()),
    (dict(turnover="MISSING"), ALERT_LEVEL_INSUFFICIENT, "turnover_missing", ("turnover",)),
    (dict(turnover="OK", avail=False), ALERT_LEVEL_INSUFFICIENT, "participation_missing",
     ("participation",)),
    (dict(turnover="OK", daily=True), ALERT_LEVEL_YELLOW, "participation_exceeds", ()),
    (dict(turnover="OK", average=True), ALERT_LEVEL_YELLOW, "participation_exceeds", ()),
    (dict(turnover="OK"), ALERT_LEVEL_GREEN, "capacity_ok", ()),
]


@pytest.mark.parametrize("kwargs, level, code, missing", CAPACITY_CASES)
def test_alert_for_capacity_table(kwargs, level, code, missing):
    item = alert_for_capacity(cap(**kwargs))
    assert item.level == level
    assert item.code == code
    assert item.missing == missing
    assert item.dimension == "capacity" and item.source == "AT4"
    assert item.advisory is True
    # Every emitted action comes from the frozen table (or is "正常").
    actions = {row["action"] for row in FROZEN_MAPPING} | {"正常"}
    assert item.action in actions, item.action


def test_alert_for_capacity_accepts_observation_pairs_and_rejects_bare_objects():
    @dataclass
    class Obs:
        available: bool = True
        exceeds_daily_volume: bool = True
        exceeds_average_volume: bool = False

    @dataclass
    class Turn:
        turnover_label: str = "WATCH"

    pair = alert_for_capacity((Turn(), Obs()))
    assert pair.level == ALERT_LEVEL_YELLOW
    assert pair.code == "turnover_watch"  # both halves YELLOW; first in table order
    assert pair.missing == ()
    # Both non-green halves are spelled out, so neither is hidden by the aggregate.
    assert any("turnover: YELLOW" in note for note in pair.notes)
    assert any("participation: YELLOW" in note for note in pair.notes)
    # A raw object without AT4's label cannot be banded here: fail closed, do not invent a band.
    bare = alert_for_capacity(object())
    assert bare.level == ALERT_LEVEL_INSUFFICIENT
    assert bare.missing == ("participation", "turnover")
    # Nothing supplied at all is still "无法判断", never GREEN.
    assert alert_for_capacity(None).level == ALERT_LEVEL_INSUFFICIENT


# ---------------------------------------------------------------------------
# 3. Aggregation
# ---------------------------------------------------------------------------
def test_severity_rank_is_the_frozen_ordinal_axis():
    assert ALERT_LEVELS == ("GREEN", "YELLOW", "ORANGE", "RED")
    assert [severity_rank(level) for level in ALERT_LEVELS] == [0, 1, 2, 3]
    # INSUFFICIENT is a completeness state, not a severity.
    with pytest.raises(AlertInputError):
        severity_rank(ALERT_LEVEL_INSUFFICIENT)
    with pytest.raises(AlertInputError):
        severity_rank("PURPLE")


@pytest.mark.parametrize(
    "levels, expected",
    [
        ([], ALERT_LEVEL_INSUFFICIENT),
        ([ALERT_LEVEL_GREEN], ALERT_LEVEL_GREEN),
        ([ALERT_LEVEL_GREEN, ALERT_LEVEL_YELLOW], ALERT_LEVEL_YELLOW),
        ([ALERT_LEVEL_YELLOW, ALERT_LEVEL_ORANGE], ALERT_LEVEL_ORANGE),
        ([ALERT_LEVEL_GREEN, ALERT_LEVEL_RED, ALERT_LEVEL_YELLOW], ALERT_LEVEL_RED),
        ([ALERT_LEVEL_RED, ALERT_LEVEL_ORANGE], ALERT_LEVEL_RED),
        ([ALERT_LEVEL_INSUFFICIENT], ALERT_LEVEL_INSUFFICIENT),
        ([ALERT_LEVEL_INSUFFICIENT, ALERT_LEVEL_GREEN], ALERT_LEVEL_INSUFFICIENT),
        ([ALERT_LEVEL_INSUFFICIENT, ALERT_LEVEL_RED], ALERT_LEVEL_INSUFFICIENT),
    ],
)
def test_combine_levels_frozen_aggregation(levels, expected):
    assert combine_levels(levels) == expected


def test_matrix_takes_the_most_severe_measured_level():
    matrix = build_alert_matrix(
        [FakeLeg(ic_ir=0.3), FakeLeg(ic_ir=-0.30, strategy="value"), FakeLeg(ic_ir=-0.9, strategy="rev")],
        capacity=cap(turnover="WATCH"),
    )
    assert [item.level for item in matrix.items] == ["GREEN", "ORANGE", "RED", "YELLOW"]
    assert matrix.level == ALERT_LEVEL_RED
    assert matrix.worst_measured_level == ALERT_LEVEL_RED
    assert matrix.missing_dimensions == ()


def test_all_insufficient_and_empty_input_are_insufficient_never_green():
    empty = build_alert_matrix()
    assert empty.items == ()
    assert empty.level == ALERT_LEVEL_INSUFFICIENT
    assert empty.worst_measured_level == ALERT_LEVEL_GREEN  # nothing ordinal was measured
    assert any("never GREEN" in note or "无任何信号层输入" in note for note in empty.notes)
    json.dumps(empty.to_dict(), allow_nan=False)

    all_gaps = build_alert_matrix(
        [FakeLeg(status="INSUFFICIENT_HISTORY"), FakeLeg(status="INSUFFICIENT_HISTORY", strategy="v")],
        capacity=cap(turnover="MISSING", avail=False),
    )
    assert all_gaps.level == ALERT_LEVEL_INSUFFICIENT
    assert set(all_gaps.missing_dimensions) == {"leg_ic", "turnover", "participation"}


def test_mixed_insufficient_and_normal_is_insufficient_and_names_the_gap():
    matrix = build_alert_matrix(
        [FakeLeg(ic_ir=0.5), FakeLeg(status="INSUFFICIENT_HISTORY", strategy="value")],
        capacity=cap(turnover="OK"),
    )
    assert matrix.level == ALERT_LEVEL_INSUFFICIENT
    assert "leg_ic" in matrix.missing_dimensions
    assert any("leg_ic" in note for note in matrix.notes)
    assert any("缺失维度" in note for note in matrix.notes)
    # The measured half is still reported, and is NOT erased by the gap.
    assert matrix.worst_measured_level == ALERT_LEVEL_GREEN
    assert {item.level for item in matrix.items} == {"GREEN", "INSUFFICIENT"}

    # A red leg next to an unmeasurable turnover keeps the red visible but the
    # matrix stays INSUFFICIENT (completeness outranks severity).
    red_plus_gap = build_alert_matrix([FakeLeg(ic_ir=-0.9)], capacity=cap(turnover="MISSING"))
    assert red_plus_gap.level == ALERT_LEVEL_INSUFFICIENT
    assert red_plus_gap.worst_measured_level == ALERT_LEVEL_RED
    assert red_plus_gap.missing_dimensions == ("turnover",)


def test_capacity_none_is_not_supplied_but_supplied_and_empty_is_a_gap():
    supplied_none = build_alert_matrix([FakeLeg(ic_ir=0.1)], capacity=None)
    assert supplied_none.level == ALERT_LEVEL_GREEN
    assert "turnover" not in supplied_none.missing_dimensions

    supplied_blank = build_alert_matrix([FakeLeg(ic_ir=0.1)], capacity={})
    assert supplied_blank.level == ALERT_LEVEL_INSUFFICIENT
    assert "turnover" in supplied_blank.missing_dimensions


# ---------------------------------------------------------------------------
# 4. JSON safety
# ---------------------------------------------------------------------------
def test_to_dict_is_strict_json_without_nan_or_infinity():
    matrix = build_alert_matrix(
        [FakeLeg(ic_ir=float("nan")), FakeLeg(ic_ir=-0.9, strategy="rev")],
        capacity=cap(turnover="BREACH", daily=True),
        as_of="2026-11-06",
    )
    payload = matrix.to_dict()
    text = json.dumps(payload, allow_nan=False, ensure_ascii=False)  # must not raise
    assert "NaN" not in text and "Infinity" not in text
    for number in _json_tree_numbers(payload):
        assert number == number and abs(number) != float("inf"), number
    round_trip = json.loads(text)
    assert round_trip["advisory"] is True and round_trip["gates_orders"] is False
    assert round_trip["level"] == ALERT_LEVEL_INSUFFICIENT
    assert round_trip["missing_dimensions"] == ["leg_ic"]
    assert round_trip["as_of"] == "2026-11-06"
    # the unmeasurable leg carries observed=None, never 0.0
    nan_leg = [item for item in round_trip["items"] if item["dimension"] == "leg_ic"][0]
    assert nan_leg["observed"] is None


# ---------------------------------------------------------------------------
# 5. Advisory fence
# ---------------------------------------------------------------------------
def test_advisory_fence_is_enforced_not_just_documented():
    matrix = build_alert_matrix([FakeLeg()], capacity=cap())
    assert matrix.advisory is True
    assert matrix.gates_orders is False
    assert matrix.to_dict()["gates_orders"] is False
    for item in matrix.items:
        assert item.advisory is True and item.gates_orders is False

    with pytest.raises(AdvisoryContractError):
        AlertMatrix(items=(), level=ALERT_LEVEL_GREEN, worst_measured_level=ALERT_LEVEL_GREEN,
                    gates_orders=True)
    with pytest.raises(AdvisoryContractError):
        AlertMatrix(items=(), level=ALERT_LEVEL_GREEN, worst_measured_level=ALERT_LEVEL_GREEN,
                    advisory=False)
    # A matrix that claims to gate is structurally impossible, so no caller can
    # get one by catching a broader ValueError either.
    assert issubclass(AdvisoryContractError, ValueError)


# ---------------------------------------------------------------------------
# 6. AST guard: no order path, no IO, no linkage to the other threshold owners
# ---------------------------------------------------------------------------
def test_module_has_no_order_path_import_no_io_and_no_threshold_owner_import():
    tree = ast.parse(_module_source())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"math", "dataclasses", "typing", "__future__"}, imported
    for forbidden in (
        "scripts", "web", "subprocess", "requests", "os", "sys",
        "cs_dispersion_exposure", "intraday_drawdown_derisk", "live_capacity",
        "ic_decay_monitor", "allocation_backtester", "backtester", "common",
    ):
        assert forbidden not in imported, f"{forbidden} must not be imported by AT5"

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & {"open", "print", "eval", "exec", "input", "compile", "__import__"}), called

    # No attribute that could look like an order/weight control surface.
    report_keys = set(build_alert_matrix().to_dict())
    assert not {key for key in report_keys if "order" in key and key != "gates_orders"}
    assert "gates_orders" in report_keys


def test_module_float_literals_do_not_contain_existing_thresholds():
    values = _signed_float_constants()
    # THE tight pin: AT5's entire float vocabulary is its own three IC-IR edges.
    assert set(values) == {-0.50, -0.25, 0.0}, sorted(set(values))
    for forbidden in _EXISTING_THRESHOLDS:
        assert forbidden not in values, (
            f"{forbidden} is an existing threshold (10/15/20 drawdown, LV1, R1, R4, "
            "L2.36 ...); AT5 must not reuse it"
        )
    # ...and the documented band edges are exactly those constants.
    assert sa.IC_IR_RED == -0.50 and sa.IC_IR_ORANGE == -0.25
    assert sa.IC_IR_YELLOW_FLOOR == 0.0


# ---------------------------------------------------------------------------
# 7. Provenance: AT3 status strings are mirrored, not invented
# ---------------------------------------------------------------------------
def test_at3_status_literals_mirror_upstream_public_constants():
    from common import ic_decay_monitor as icm

    assert sa.AT3_STATUS_OK == icm.STATUS_OK
    assert sa.AT3_STATUS_INSUFFICIENT_HISTORY == icm.STATUS_INSUFFICIENT_HISTORY
    # The upstream report's status is exactly what the matrix consumes: no rename,
    # so a report built by AT3 lands on the INSUFFICIENT row, not on a guessed band.
    leg = icm.LegICReport(
        market="us", universe_id="us_core", strategy="momentum",
        status=icm.STATUS_INSUFFICIENT_HISTORY, horizon_bars=5,
        return_basis="close_to_close", min_cross_section=15, min_valid_eval_days=10,
        n_snapshot_dates=3, n_valid_eval_dates=1, n_dates_below_cross_section_floor=2,
        n_dates_degenerate_cross_section=0, n_dates_missing_returns=0,
        mean_ic=float("nan"), std_ic=float("nan"), ic_ir=float("nan"), half_life=float("nan"),
        t_stat=float("nan"), p_value=float("nan"), first_conclusion_available=None,
    )
    item = alert_for_leg(leg)
    assert item.level == ALERT_LEVEL_INSUFFICIENT
    assert item.subject == "us:us_core:momentum"
    assert item.action == "样本不足，不出结论"


# ---------------------------------------------------------------------------
# 8. Purity: deterministic, no mutation, no accumulated state
# ---------------------------------------------------------------------------
def test_matrix_is_pure_deterministic_and_does_not_mutate_inputs():
    legs = [FakeLeg(ic_ir=-0.3), FakeLeg(ic_ir=0.2, strategy="value")]
    capacity = cap(turnover="WATCH", daily=True)
    snapshot = (list(legs), dict(capacity))

    first = build_alert_matrix(legs, capacity=capacity)
    second = build_alert_matrix(legs, capacity=capacity)
    assert first.to_dict() == second.to_dict()
    assert first.level == second.level == ALERT_LEVEL_ORANGE
    assert (list(legs), dict(capacity)) == snapshot  # inputs untouched
    # A tuple is accepted just as a list is.
    assert build_alert_matrix(tuple(legs), capacity=capacity).to_dict() == first.to_dict()

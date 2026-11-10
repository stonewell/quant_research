"""Offline regression suite for the `AT7` **P1/P2/P3** advisory wiring.

`AT7` (`docs/development_plan.md:413`) is a three-pass approval gate:
**P1** report fields, **P2** shadow, **P3** enforcement. `P1`/`P2` are strictly
write-only: the advisory block lands in the Stage-1 report and the Stage-2 ticket
`detail_reason`, and no sizing path reads it. `P3` (user-approved 2026-11-09) is
the one pass that may resize an order, and only with the frozen composition
`effective_buy_scale = real_buy_scale x R1 multiplier` on **buy increments only**,
behind a switch that defaults to OFF and an env kill-switch that overrides it.

What this file pins:

1. the shared builder's P1 field surface is present and stable
   (`common/advisory_report.py` is the single shape all three passes use);
2. the block is **strict JSON** -- `json.dumps(..., allow_nan=False)` succeeds,
   no `NaN`/`Infinity` text survives, every number is finite or `None`;
3. unmeasurable input is passed through verbatim: empty `leg_reports` +
   `turnover=None` -> matrix level `INSUFFICIENT`, turnover/participation
   labels `MISSING`, and **no `0.0` appears where the source is unknown**;
4. the real Stage-1 script writes the block into
   `stage1_health_report.json` and the file stays `json.load`-able;
5. `common/advisory_report.py` contains **no numeric threshold literal at all**
   (the `R1`/`AT4`/`AT5` bands are consumed from their own modules);
6. the fence around the untouched modules holds: neither the new module nor the
   two edited scripts import `blend_base` / `cs_dispersion_exposure`, and
   `pipeline/research/blend/blend_base.py` carries no advisory import and no
   re-declared `R1` threshold;
7. (`P2`) the shadow line never moves an order: the Stage-2 sizing path returns
   a bit-identical Series with and without an advisory block attached, the three
   `R1` tiers surface as `0.75` / `0.50` / `0.25` in the shadow only, an
   unmeasurable session yields **no** shadow keys at all (never `0.0` / `1.0`),
   and every block stays strictly JSON;
8. (`P3`) the enforcement switch is OFF by default and env-killable, the composed
   sizing output is bit-identical when it is off, buy increments shrink by
   exactly `real x multiplier` / sells + holds + cash stay bit-identical when it
   is on, the offline before/after report is strict-JSON and deterministic, and
   `R4` stays out of scope.

100% offline: synthetic/hand-written inputs only, no network, no market data,
no writes outside `tmp_path`.
"""

import ast
import importlib.util
import inspect
import json
import math
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.advisory_report import (
    ADVISORY_PHASE_P1,
    ADVISORY_PHASE_P2,
    ADVISORY_PHASE_P3,
    build_advisory_block,
    r1_decision_to_dict,
)
from common.signal_alerts import ALERT_LEVEL_INSUFFICIENT

_ADVISORY_PATH = os.path.join(_REPO_ROOT, "common", "advisory_report.py")
_STAGE1_PATH = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
_STAGE2_PATH = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
_BLEND_BASE_PATH = os.path.join(
    _REPO_ROOT, "pipeline", "research", "blend", "blend_base.py"
)

# The `R1` drawdown tiers and the `AT4` / `AT5` bands. `advisory_report.py` must
# not redeclare *any* of them; the Stage-1/Stage-2 wiring must not import the
# modules that own the blend/R4 threshold axes.
_FORBIDDEN_FLOAT_LITERALS = {
    -0.02,
    -0.03,
    -0.04,
    -0.50,
    -0.25,
    0.0,
    0.10,
    0.15,
    0.20,
    0.25,
    0.50,
    0.75,
    8.0,
    10.0,
}


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _signed_float_constants(path):
    """Every float literal in a file, with a leading unary minus folded in."""
    tree = ast.parse(_source(path))
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


def _imported_modules(path):
    tree = ast.parse(_source(path))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _json_tree_numbers(obj):
    """Every number in a JSON-ready tree (dicts / lists / scalars)."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _json_tree_numbers(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _json_tree_numbers(value)
    elif isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return
    elif isinstance(obj, (int, float)):
        yield obj


# ---------------------------------------------------------------------------
# 1. The P1 field surface
# ---------------------------------------------------------------------------
def test_build_advisory_block_p1_fields_present():
    block = build_advisory_block(phase=ADVISORY_PHASE_P1, as_of="2026-11-07")

    assert block["advisory"] is True
    assert block["gates_orders"] is False
    assert block["phase"] == ADVISORY_PHASE_P1
    assert block["as_of"] == "2026-11-07"
    assert isinstance(block["alert_matrix"], dict)
    assert block["alert_matrix"]["advisory"] is True
    assert block["alert_matrix"]["gates_orders"] is False
    assert block["turnover_label"] == "MISSING"
    assert block["participation_label"] == "MISSING"
    assert isinstance(block["worst_measured_level"], str)
    # P1 never lets the R1 multiplier reach an order path.
    assert block["r1_effective"] is False
    # Optional fields stay absent unless a later pass supplies their sources.
    assert "r1" not in block
    assert "real_buy_scale" not in block
    assert "shadow_buy_scale" not in block


def test_phase_constants_are_distinct_and_phase_is_validated():
    assert {ADVISORY_PHASE_P1, ADVISORY_PHASE_P2, ADVISORY_PHASE_P3} == {"P1", "P2", "P3"}
    with pytest.raises(ValueError):
        build_advisory_block(phase="P4")


# ---------------------------------------------------------------------------
# 2. Strict-JSON round trip (the Stage-2 writer has no default=/allow_nan crutch)
# ---------------------------------------------------------------------------
def test_p1_roundtrip_json_strict():
    block = build_advisory_block(
        phase=ADVISORY_PHASE_P1,
        as_of="2026-11-07",
        r1=types.SimpleNamespace(
            status="NOT_TRIGGERED",
            multiplier=1.0,
            drawdown=None,
            reason="no intraday drawdown input",
            dd_source="none",
        ),
        real_buy_scale=1.0,
    )

    text = json.dumps(block, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text) == block

    numbers = list(_json_tree_numbers(block))
    assert numbers, "the block must carry at least the R1 multiplier / buy scale"
    for number in numbers:
        assert math.isfinite(number), number

    # Frozen P3 composition semantics, reported as a shadow value only.
    assert block["shadow_buy_scale"] == block["real_buy_scale"] * block["r1"]["multiplier"]


# ---------------------------------------------------------------------------
# 3. Unknown stays unknown -- never downgraded, never zero-filled
# ---------------------------------------------------------------------------
def test_p1_insufficient_and_missing_never_filled():
    block = build_advisory_block(
        phase=ADVISORY_PHASE_P1,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of="2026-11-07",
    )

    assert block["turnover_label"] == "MISSING"
    assert block["participation_label"] == "MISSING"
    matrix = block["alert_matrix"]
    assert matrix["level"] == ALERT_LEVEL_INSUFFICIENT
    assert matrix["level"] != "GREEN"
    assert set(matrix["missing_dimensions"]) == {"turnover", "participation"}

    # No fabricated zero: nothing observed, so every item's `observed` is null.
    for item in matrix["items"]:
        assert item["observed"] is None, item
        assert item["level"] == ALERT_LEVEL_INSUFFICIENT, item

    # Top-level unknown fields are absent rather than 0.0.
    assert "real_buy_scale" not in block
    assert "shadow_buy_scale" not in block


# ---------------------------------------------------------------------------
# 4. The real Stage-1 script emits the key
# ---------------------------------------------------------------------------
def test_stage1_report_emits_advisory_key(tmp_path):
    mod = _load_script("stage1_health_for_advisory_test", "scripts/check_live_portfolio_health.py")

    flat_state = {
        "as_of_date": None,
        "peak_nav": None,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [],
    }
    dd_info = mod.inspect_portfolio_drawdown(
        current_nav=1_030_000.0,
        peak_nav=1_100_000.0,
        state=flat_state,
        params={},
        as_of_date="2026-11-07",
    )
    advisory = build_advisory_block(phase=ADVISORY_PHASE_P1, as_of="2026-11-07")

    mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-07",
        strategy_info={"strategy_name": "unit-test", "template_name": "unit-test"},
        dd_info=dd_info,
        macro_info={
            "breadth": 0.40,
            "thrust": 0.45,
            "breadth_active": True,
            "thrust_active": False,
            "target_exposure": 0.50,
            "breadth_thresh": 0.30,
            "thrust_thresh": 0.60,
            "leaders": [],
            "tracked_symbols_count": 3,
        },
        vol_info={
            "realized_vol_21d": 0.18,
            "target_vol": 0.20,
            "vol_scaling_factor": 1.0,
            "vol_elevated": False,
        },
        gate_code="GO",
        gate_headline="GO",
        actions=[],
        advisory=advisory,
    )

    report_path = os.path.join(str(tmp_path), mod.STAGE1_HEALTH_REPORT_JSON)
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)

    assert report["advisory"]["phase"] == "P1"
    assert report["advisory"]["advisory"] is True
    assert report["advisory"]["gates_orders"] is False
    assert report["advisory"]["turnover_label"] == "MISSING"
    assert report["advisory"]["participation_label"] == "MISSING"


def test_stage1_default_advisory_is_none(tmp_path):
    """Omitting the argument preserves the pre-P1 report shape exactly."""
    mod = _load_script("stage1_health_for_advisory_default_test", "scripts/check_live_portfolio_health.py")

    dd_info = mod.inspect_portfolio_drawdown(
        current_nav=1_030_000.0,
        peak_nav=1_100_000.0,
        state={"as_of_date": None, "peak_nav": None, "tier1_consecutive_bars": 0,
               "tier3_consecutive_bars": 0, "freeze_remaining_bars": 0,
               "nav_history_10d": []},
        params={},
        as_of_date="2026-11-07",
    )
    mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-07",
        strategy_info={"strategy_name": "unit-test", "template_name": "unit-test"},
        dd_info=dd_info,
        macro_info={
            "breadth": 0.40, "thrust": 0.45, "breadth_active": True,
            "thrust_active": False, "target_exposure": 0.50,
            "breadth_thresh": 0.30, "thrust_thresh": 0.60, "leaders": [],
            "tracked_symbols_count": 3,
        },
        vol_info={
            "realized_vol_21d": 0.18, "target_vol": 0.20,
            "vol_scaling_factor": 1.0, "vol_elevated": False,
        },
        gate_code="GO",
        gate_headline="GO",
        actions=[],
    )
    with open(os.path.join(str(tmp_path), mod.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["advisory"] is None


# ---------------------------------------------------------------------------
# 5. No threshold literal in the shared builder
# ---------------------------------------------------------------------------
def test_advisory_module_has_no_threshold_literals():
    values = _signed_float_constants(_ADVISORY_PATH)
    assert set(values) == set(), sorted(set(values))
    assert not (set(values) & _FORBIDDEN_FLOAT_LITERALS)


def test_r1_field_extraction_is_pure_and_json_safe():
    decision = types.SimpleNamespace(
        status="DERISK_LIGHT",
        multiplier=0.75,
        drawdown=-0.025,
        reason="dd -2.50% in (-2%, -4%]",
        dd_source="price",
    )
    extracted = r1_decision_to_dict(decision)
    assert extracted == {
        "advisory": True,
        "gates_orders": False,
        "status": "DERISK_LIGHT",
        "multiplier": 0.75,
        "drawdown": -0.025,
        "reason": "dd -2.50% in (-2%, -4%]",
        "dd_source": "price",
    }
    # Unmeasured drawdown stays `None`, never 0.0.
    unmeasured = r1_decision_to_dict(
        types.SimpleNamespace(
            status="INSUFFICIENT_DATA",
            multiplier=1.0,
            drawdown=None,
            reason="no price input",
            dd_source="none",
        )
    )
    assert unmeasured["drawdown"] is None
    assert unmeasured["multiplier"] == 1.0


# ---------------------------------------------------------------------------
# 6. Fence: the untouched modules stay untouched
# ---------------------------------------------------------------------------
def test_r4_and_blend_base_untouched():
    # NOTE (deviation from the brief, verified against HEAD): `scripts/run_live_strategy.py`
    # ALREADY lazily imports `pipeline.research.blend.blend_base._get_symbol_sector`
    # (line ~222, inside a function) long before `AT7` P1 -- that is a pre-existing
    # sector-throttle helper, not an advisory hook. So the fence asserted here is the
    # one that is actually true and load-bearing: the NEW advisory code links only to
    # `common.advisory_report`, never to `blend_base` / `cs_dispersion_exposure`, and
    # `blend_base.py` itself stays free of any advisory import or R1 threshold.
    assert "blend_base" not in _imported_modules(_ADVISORY_PATH)
    assert "cs_dispersion_exposure" not in _imported_modules(_ADVISORY_PATH)

    for path in (_STAGE1_PATH, _STAGE2_PATH):
        advisory_imports = {m for m in _imported_modules(path) if "advisory" in m}
        assert advisory_imports == {"common.advisory_report"}, advisory_imports
        assert "cs_dispersion_exposure" not in _imported_modules(path), path

    # The advisory hook itself must never reach into the blend engine.
    for path in (_STAGE1_PATH, _STAGE2_PATH):
        source = _source(path)
        for line in source.splitlines():
            if "build_advisory_block" in line or "ADVISORY_PHASE_P1" in line:
                assert "blend_base" not in line, line

    blend_source = _source(_BLEND_BASE_PATH)
    assert "advisory" not in blend_source.lower()
    assert "intraday_drawdown_derisk" not in blend_source
    # No re-declared R1 drawdown tier (`-2%` / `-4%`) anywhere on the blend base.
    blend_floats = set(_signed_float_constants(_BLEND_BASE_PATH))
    assert not (blend_floats & {-0.02, -0.03, -0.04}), (
        "blend_base.py must not redeclare the R1 drawdown tiers"
    )


# ---------------------------------------------------------------------------
# 7. P2 shadow: the advisory block is WRITE-ONLY on the sizing path
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stage2():
    """The real Stage-2 script, imported once (it is the code under test, not a stub)."""
    import scripts.run_live_strategy as mod  # noqa: PLC0415 -- module has import-time sys.path setup

    return mod


def _shadow_block(mod, *, real_buy_scale=0.50, metrics=None):
    """A P2 block exactly as `main()` builds it for the shadow line."""
    metrics = {"hwm_intra": 100.0, "dd_trough": -0.035} if metrics is None else metrics
    return build_advisory_block(
        phase=ADVISORY_PHASE_P2,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of="2026-11-09",
        r1=mod.compute_r1_advisory({"intraday_5m_metrics": metrics}),
        real_buy_scale=real_buy_scale,
    )


def test_p2_shadow_does_not_change_orders(stage2):
    """Same inputs, with vs. without the shadow block attached => bit-identical orders.

    This is the load-bearing P2 acceptance criterion (`docs/development_plan.md`
    `AT7`: "shadow 期下单量与接入前 bit-identical"). The block is attached where
    the run would attach it (`health_report["advisory"]`, and a row's
    `detail_reason["advisory"]`) and the two real sizing functions are called;
    neither may notice it.
    """
    mod = stage2
    cash_proxy = "511880.SH"
    # Synthetic, hand-written weights: two risk assets plus the cash leg.
    target = pd.Series({"000001.SZ": 0.32, "600519.SH": 0.18, cash_proxy: 0.50})
    current = pd.Series({"000001.SZ": 0.10, "600519.SH": 0.24, cash_proxy: 0.66})

    plain_report = {"gate_code": "CAUTION_INTRADAY_DISTRIBUTION", "intraday_buy_scale": 0.50}
    shadow_block = _shadow_block(mod, real_buy_scale=0.50)
    shadowed_report = dict(plain_report, advisory=shadow_block)

    # The gate that consumes `intraday_buy_scale` is the ONLY order-affecting reader.
    plain_target = mod.apply_intraday_distribution_gate(
        target.copy(), current, cash_proxy, health_report=plain_report, account_state={}
    )
    shadowed_target = mod.apply_intraday_distribution_gate(
        target.copy(), current, cash_proxy, health_report=shadowed_report, account_state={}
    )

    assert isinstance(plain_target, pd.Series) and isinstance(shadowed_target, pd.Series)
    assert plain_target.equals(shadowed_target)
    assert list(plain_target.items()) == list(shadowed_target.items())
    assert plain_target.dtype == shadowed_target.dtype
    assert plain_target.index.equals(shadowed_target.index)
    # And the block genuinely carries a would-be de-risk (nothing is being tested
    # against a multiplier of 1.0 by accident).
    assert shadow_block["shadow_buy_scale"] < shadow_block["real_buy_scale"]

    # Same fence on the delivered market-value cap: identical rows, one carrying an
    # advisory block, must produce identical sizing fields.
    def _row(with_advisory):
        detail = {"summary_reason": "unit-test"}
        if with_advisory:
            detail["advisory"] = shadow_block
        return {
            "symbol": "600519.SH",
            "price": 1680.0,
            "target_shares": 100,
            "current_shares": 0,
            "delta_shares": 100,
            "current_weight": 0.0,
            "trade_value": 168000.0,
            "trade_value_rmb": 168000.0,
            "reason": "",
            "summary_reason": "",
            "detail_reason": detail,
        }

    row_plain = mod.apply_market_value_cap(
        _row(False), portfolio_value=1_000_000.0, max_weight=0.10, lot_size=100, enforce=True
    )
    row_shadow = mod.apply_market_value_cap(
        _row(True), portfolio_value=1_000_000.0, max_weight=0.10, lot_size=100, enforce=True
    )
    _sizing_keys = (
        "target_shares",
        "current_shares",
        "delta_shares",
        "trade_value",
        "trade_value_rmb",
        "target_weight",
        "delta_weight",
    )
    for key in _sizing_keys:
        assert row_plain[key] == row_shadow[key], key
    assert row_plain["detail_reason"]["mv_cap"] == row_shadow["detail_reason"]["mv_cap"]

    # Static fence: the two sizing functions must not mention the advisory layer at all.
    for func in (mod.apply_intraday_distribution_gate, mod.apply_market_value_cap):
        source = inspect.getsource(func)
        assert "advisory" not in source.lower(), func.__name__
        assert "intraday_drawdown_derisk" not in source, func.__name__


def test_p2_shadow_side_by_side(stage2):
    """The shadow reports `real × multiplier` beside the real scale -- never instead of it."""
    mod = stage2
    r1 = mod.compute_r1_advisory(
        {"intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": -0.05}}
    )
    assert r1 is not None
    assert r1["multiplier"] == 0.25
    assert r1["status"] == "DERISK_DEEP"
    assert r1["advisory"] is True
    assert r1["gates_orders"] is False

    block = build_advisory_block(
        phase=ADVISORY_PHASE_P2,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of="2026-11-09",
        r1=r1,
        real_buy_scale=0.50,
    )
    assert block["phase"] == "P2"
    assert block["real_buy_scale"] == 0.50
    assert block["shadow_buy_scale"] == block["real_buy_scale"] * block["r1"]["multiplier"]
    assert block["shadow_buy_scale"] == pytest.approx(0.125)
    assert block["r1_effective"] is False

    # Tier walk: the ladder's three de-risk steps surface unchanged (consumed from
    # `common.intraday_drawdown_derisk`, never re-declared in the Stage-2 script).
    tiers = {
        -0.025: (0.75, "DERISK_LIGHT"),
        -0.035: (0.50, "DERISK_MEDIUM"),
        -0.045: (0.25, "DERISK_DEEP"),
    }
    for dd, (multiplier, status) in tiers.items():
        tier = mod.compute_r1_advisory(
            {"intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": dd}}
        )
        assert tier["multiplier"] == multiplier, dd
        assert tier["status"] == status, dd


def test_p2_unmeasurable_yields_no_shadow(stage2):
    """No reading => no shadow line. Unknown is `None`, never `0.0` and never `1.0`."""
    mod = stage2

    assert mod.compute_r1_advisory({}) is None
    assert mod.compute_r1_advisory(None) is None
    assert mod.compute_r1_advisory({"intraday_5m_metrics": {}}) is None
    assert mod.compute_r1_advisory({"intraday_5m_metrics": {"hwm_intra": 100.0}}) is None
    assert mod.compute_r1_advisory({"intraday_5m_metrics": {"dd_trough": -0.03}}) is None
    # Stage-1's SKIPPED path writes explicit nulls, not absent keys.
    assert mod.compute_r1_advisory(
        {"intraday_5m_metrics": {"hwm_intra": None, "dd_trough": None}}
    ) is None
    assert mod.compute_r1_advisory(
        {"intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": None}}
    ) is None

    block = build_advisory_block(
        phase=ADVISORY_PHASE_P2,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of="2026-11-09",
        r1=mod.compute_r1_advisory({"intraday_5m_metrics": {}}),
        real_buy_scale=0.50,
    )
    assert "r1" not in block
    assert "shadow_buy_scale" not in block
    # No fabricated substitute for the missing verdict: the only number left in the
    # block is the real buy scale it was handed (0.50), never an R1 `0.0` / `1.0`.
    assert set(_json_tree_numbers(block)) == {0.50}, sorted(set(_json_tree_numbers(block)))
    assert block["r1_effective"] is False


def test_p2_block_strict_json(stage2):
    """The Stage-2 writer has no `default=str` / `allow_nan=False` crutch."""
    mod = stage2
    for metrics in (
        {"hwm_intra": 100.0, "dd_trough": -0.05},
        {"hwm_intra": None, "dd_trough": None},
        {"hwm_intra": 2_400_000.0, "dd_trough": -0.021},
    ):
        block = _shadow_block(mod, metrics=metrics)
        text = json.dumps(block, ensure_ascii=False, allow_nan=False)
        assert "NaN" not in text
        assert "Infinity" not in text
        assert json.loads(text) == block
        for number in _json_tree_numbers(block):
            assert math.isfinite(number), number
        # JSON-native types only: no numpy scalars survive a `==` round trip.
        assert json.loads(json.dumps(block, allow_nan=False))["phase"] == "P2"
        assert json.dumps(json.loads(text), ensure_ascii=False, allow_nan=False) == text


# ---------------------------------------------------------------------------
# 8. (P3) Enforcement: OFF by default, multiplicative when ON, R4 out of scope
# ---------------------------------------------------------------------------
_R1_REPORT_PATH = os.path.join(_REPO_ROOT, "common", "r1_derisk_report.py")
_P2_BASELINE_ENV = "LIVETRADING_DISABLE_R1_DERISK"


def _composed_sizing(
    mod, target, current, cash_proxy, health_report, *, enforce_r1, account_state=None
):
    """The exact composition order `main()` uses: Stage-1 gate, THEN R1 (P3 opt-in)."""
    out = mod.apply_intraday_distribution_gate(
        target.copy(),
        current,
        cash_proxy,
        health_report=health_report,
        account_state=account_state or {},
    )
    r1 = mod.compute_r1_advisory(health_report)
    multiplier = None if r1 is None else r1.get("multiplier")
    # Same single predicate `main()` uses -- never a re-implemented `if`.
    out, _ = mod.resolve_r1_derisk_target(
        out,
        current,
        cash_proxy,
        enforced=mod.r1_derisk_enforcement_enabled(enforce_r1),
        r1_multiplier=multiplier,
    )
    return out


def test_p3_default_off_is_identical(stage2, monkeypatch):
    """Off (flag absent / False / env kill-switch) => byte-for-byte the P2 output."""
    mod = stage2
    cash_proxy = "511880.SH"
    target = pd.Series({"000001.SZ": 0.32, "600519.SH": 0.18, cash_proxy: 0.50})
    current = pd.Series({"000001.SZ": 0.10, "600519.SH": 0.24, cash_proxy: 0.66})
    # A CAUTION gate with a real R1 verdict, so the OFF path is exercised with a
    # multiplier that WOULD de-risk (0.25) -- not a vacuous multiplier of 1.0.
    health_report = {
        "gate_code": "CAUTION_INTRADAY_DISTRIBUTION",
        "intraday_buy_scale": 0.50,
        "intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": -0.05},
    }
    assert mod.compute_r1_advisory(health_report)["multiplier"] == 0.25

    monkeypatch.delenv(_P2_BASELINE_ENV, raising=False)
    baseline = _composed_sizing(
        mod, target, current, cash_proxy, health_report, enforce_r1=False
    )
    p2_reference = mod.apply_intraday_distribution_gate(
        target.copy(), current, cash_proxy, health_report=health_report, account_state={}
    )
    assert baseline.equals(p2_reference)
    assert list(baseline.items()) == list(p2_reference.items())
    assert baseline.dtype == p2_reference.dtype
    assert baseline.index.equals(p2_reference.index)

    for requested in (None, False):
        out = _composed_sizing(
            mod, target, current, cash_proxy, health_report, enforce_r1=requested
        )
        assert list(out.items()) == list(p2_reference.items()), requested

    # Env kill-switch wins even over an explicit request.
    monkeypatch.setenv(_P2_BASELINE_ENV, "1")
    killed = _composed_sizing(
        mod, target, current, cash_proxy, health_report, enforce_r1=True
    )
    assert list(killed.items()) == list(p2_reference.items())

    # The switch itself, exactly as specified.
    monkeypatch.delenv(_P2_BASELINE_ENV, raising=False)
    assert mod.r1_derisk_enforcement_enabled(None) is False
    assert mod.r1_derisk_enforcement_enabled(False) is False
    assert mod.r1_derisk_enforcement_enabled(True) is True
    monkeypatch.setenv(_P2_BASELINE_ENV, "true")
    assert mod.r1_derisk_enforcement_enabled(None) is False
    assert mod.r1_derisk_enforcement_enabled(False) is False
    assert mod.r1_derisk_enforcement_enabled(True) is False

    # Default OFF at the CLI surface too.
    assert mod.parse_args(["--strategy", "equal_weight"]).enforce_r1_derisk is False
    assert mod.parse_args(
        ["--strategy", "equal_weight", "--enforce-r1-derisk"]
    ).enforce_r1_derisk is True


def test_p3_enabled_scales_buys_only(stage2, monkeypatch):
    """Every buy increment shrinks by exactly the multiplier; sells/holds/cash verbatim."""
    mod = stage2
    monkeypatch.delenv(_P2_BASELINE_ENV, raising=False)
    cash_proxy = "511880.SH"
    target = pd.Series(
        {
            "000001.SZ": 0.32,   # BUY  (0.10 -> 0.32)
            "600519.SH": 0.10,   # SELL (0.24 -> 0.10)
            "000002.SZ": 0.05,   # HOLD (0.05 -> 0.05)
            cash_proxy: 0.53,
        }
    )
    current = pd.Series(
        {"000001.SZ": 0.10, "600519.SH": 0.24, "000002.SZ": 0.05, cash_proxy: 0.61}
    )
    multiplier = 0.25

    scaled = mod.apply_r1_intraday_derisk(target, current, cash_proxy, multiplier)
    assert isinstance(scaled, pd.Series)
    assert scaled is not target

    # Buy increment: cur + (tgt - cur) * multiplier, bit-identical to the rule.
    assert scaled["000001.SZ"] == current["000001.SZ"] + (
        target["000001.SZ"] - current["000001.SZ"]
    ) * multiplier
    assert scaled["000001.SZ"] < target["000001.SZ"]
    # Sell / hold / cash are untouched, exactly (not approximately).
    for sym in ("600519.SH", "000002.SZ", cash_proxy):
        assert scaled[sym] == target[sym], sym
    assert scaled[cash_proxy] == target[cash_proxy]

    # Nothing else moved: the index and the untouched values are bit-identical.
    assert scaled.index.equals(target.index)
    untouched_before = [target[s] for s in ("600519.SH", "000002.SZ", cash_proxy)]
    untouched_after = [scaled[s] for s in ("600519.SH", "000002.SZ", cash_proxy)]
    assert untouched_before == untouched_after

    # And the P3 block reports the fact, once, with the composed scale.
    health_report = {
        "gate_code": "CAUTION_INTRADAY_DISTRIBUTION",
        "intraday_buy_scale": 0.50,
        "intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": -0.05},
    }
    composed = _composed_sizing(
        mod, target, current, cash_proxy, health_report, enforce_r1=True
    )
    # Gate: 0.10 + 0.22*0.50 = 0.21; then R1: 0.10 + 0.11*0.25 = 0.1275.
    assert composed["000001.SZ"] == pytest.approx(0.1275)
    assert composed["600519.SH"] == target["600519.SH"]

    r1 = mod.compute_r1_advisory(health_report)
    block = build_advisory_block(
        phase=ADVISORY_PHASE_P3,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of="2026-11-09",
        r1=r1,
        real_buy_scale=0.50,
    )
    assert block["r1_effective"] is False  # builder stays conservative...
    block["r1_effective"] = True           # ...the caller flips it (as main() does)
    block["effective_buy_scale"] = block["real_buy_scale"] * block["r1"]["multiplier"]
    assert block["phase"] == ADVISORY_PHASE_P3
    assert block["r1_effective"] is True
    assert block["effective_buy_scale"] == 0.125
    assert block["effective_buy_scale"] == block["shadow_buy_scale"]
    json.dumps(block, ensure_ascii=False, allow_nan=False)


def test_p3_composition_is_multiplicative(stage2, monkeypatch):
    """`effective == real x multiplier` -- NOT `min()` and NOT "replace"."""
    mod = stage2
    monkeypatch.delenv(_P2_BASELINE_ENV, raising=False)
    cash_proxy = "511880.SH"
    real_scale, multiplier = 0.50, 0.75
    # A case where the three candidate semantics disagree: real*mult = 0.375,
    # min(real, mult) = 0.50, "replace with the multiplier" would be 0.75.
    assert real_scale * multiplier == 0.375
    assert min(real_scale, multiplier) != real_scale * multiplier
    assert multiplier != real_scale * multiplier

    target = pd.Series({"000001.SZ": 1.00, cash_proxy: 0.00})
    current = pd.Series({"000001.SZ": 0.00, cash_proxy: 1.00})
    health_report = {
        "gate_code": "CAUTION_INTRADAY_DISTRIBUTION",
        "intraday_buy_scale": real_scale,
        "intraday_5m_metrics": {"hwm_intra": 100.0, "dd_trough": -0.025},
    }
    assert mod.compute_r1_advisory(health_report)["multiplier"] == multiplier

    composed = _composed_sizing(
        mod, target, current, cash_proxy, health_report, enforce_r1=True
    )
    # 0 -> *0.50 -> 0.50 -> *0.75 -> 0.375 (exact in binary: 3/8).
    assert composed["000001.SZ"] == 0.375
    assert composed["000001.SZ"] == current["000001.SZ"] + (
        target["000001.SZ"] - current["000001.SZ"]
    ) * (real_scale * multiplier)
    assert composed["000001.SZ"] != min(real_scale, multiplier)
    assert composed["000001.SZ"] != multiplier

    effective = real_scale * multiplier
    applied_scale = (
        composed["000001.SZ"] - current["000001.SZ"]
    ) / (target["000001.SZ"] - current["000001.SZ"])
    assert applied_scale == effective


def test_p3_before_after_report_offline(stage2):
    """The P3 deliverable: strict-JSON, deterministic, offline before/after report."""
    from common.r1_derisk_report import build_r1_derisk_report

    rows = [
        {"symbol": "000001.SZ", "price": 20.0, "target_weight": 0.32, "current_weight": 0.10},
        {"symbol": "600519.SH", "price": 16.80, "target_weight": 0.10, "current_weight": 0.24},
        {"symbol": "000002.SZ", "price": 30.0, "target_weight": 0.05, "current_weight": 0.05},
    ]
    # The SAME lot rule the live path applies to a BUY (`floor` to whole lots).
    def rounder(symbol, raw_shares):
        return int(math.floor(raw_shares / 100) * 100)

    kwargs = dict(
        as_of="2026-11-09",
        run_id="synthetic-p3-report",
        strategy_id="chan_four_state_blend",
        strategy_version="2.1.0",
        base_version="2.0.0",
        config_hash="deadbeef",
        r1={"multiplier": 0.25, "status": "DERISK_DEEP", "drawdown": -0.045},
        real_buy_scale=0.50,
        portfolio_value=1_000_000.0,
        rounder=rounder,
        cash_proxy="511880.SH",
    )
    report = build_r1_derisk_report(rows, **kwargs)

    for key in (
        "as_of",
        "run_id",
        "strategy_id",
        "strategy_version",
        "base_version",
        "config_hash",
        "r1",
        "real_buy_scale",
        "effective_buy_scale",
        "symbols",
        "total_notional_before",
        "total_notional_after",
        "total_notional_delta",
    ):
        assert key in report, key
    assert report["effective_buy_scale"] == 0.125
    assert report["r1"] == {"multiplier": 0.25, "status": "DERISK_DEEP", "drawdown": -0.045}

    per_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert set(per_symbol) == {"000001.SZ", "600519.SH", "000002.SZ"}
    for row in report["symbols"]:
        for key in (
            "symbol",
            "target_weight_before",
            "target_weight_after",
            "qty_before",
            "qty_after",
            "delta_qty",
            "notional_before",
            "notional_after",
            "delta_notional",
        ):
            assert key in row, (row["symbol"], key)
        assert row["delta_qty"] == row["qty_after"] - row["qty_before"]
        assert row["delta_notional"] == pytest.approx(
            row["notional_after"] - row["notional_before"]
        )

    buy = per_symbol["000001.SZ"]
    assert buy["target_weight_before"] == 0.32
    assert buy["target_weight_after"] == pytest.approx(0.155)
    assert buy["qty_before"] == 16000
    assert buy["qty_after"] == 7700
    assert buy["delta_qty"] == -8300
    assert buy["delta_notional"] == -166000.0
    # Sells / holds are untouched: the fence is visible in the report itself.
    for sym in ("600519.SH", "000002.SZ"):
        assert per_symbol[sym]["target_weight_after"] == per_symbol[sym]["target_weight_before"]
        assert per_symbol[sym]["delta_qty"] == 0
        assert per_symbol[sym]["delta_notional"] == 0.0

    assert report["total_notional_delta"] == pytest.approx(
        sum(row["delta_notional"] for row in report["symbols"])
    )
    assert report["total_notional_after"] == pytest.approx(
        report["total_notional_before"] + report["total_notional_delta"]
    )

    # Strict JSON: no NaN/Infinity, round-trips exactly.
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == report
    for number in _json_tree_numbers(report):
        assert math.isfinite(number), number

    # Determinism: identical inputs => identical bytes.
    again = build_r1_derisk_report(rows, **kwargs)
    assert json.dumps(again, ensure_ascii=False, allow_nan=False, sort_keys=True) == text
    assert again == report

    # Unmeasurable inputs never become plausible numbers.
    empty = build_r1_derisk_report(
        [{"symbol": "000001.SZ", "price": 20.0, "target_weight": 0.32, "current_weight": 0.0}],
        **{**kwargs, "r1": None, "portfolio_value": 0.0},
    )
    assert empty["r1"] == {"multiplier": None, "status": None, "drawdown": None}
    assert empty["effective_buy_scale"] == 0.50
    assert empty["symbols"][0]["qty_before"] == 0
    assert empty["symbols"][0]["notional_before"] == 0.0
    json.dumps(empty, ensure_ascii=False, allow_nan=False)


def test_p3_r4_untouched(stage2):
    """`R4` stays out of scope: no import, no market-level de-risk, said out loud."""
    for path in (_R1_REPORT_PATH, _STAGE2_PATH):
        imported = " ".join(sorted(_imported_modules(path))).lower()
        assert "dispersion" not in imported, path
        assert "r4" not in imported, path
        source = _source(path)
        assert "cs_dispersion_exposure" not in source, path
        # Statement-level (not substring) check: a COMMENT may name `R4` -- it
        # must -- but no import statement may reach for it.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                assert "r4" not in stripped.lower(), line
                assert "dispersion" not in stripped.lower(), line

    # The scope fence is documented at the site where `intraday_buy_scale` is
    # resolved (the surrounding comment block, not the frozen gate function).
    stage2_lines = _source(_STAGE2_PATH).splitlines()
    scope_start = next(
        i for i, line in enumerate(stage2_lines) if "real_intraday_buy_scale = float(" in line
    )
    scope_block = "\n".join(stage2_lines[max(0, scope_start - 2) : scope_start + 24])
    assert "R4" in scope_block
    assert "OUT OF SCOPE" in scope_block
    assert "intraday_buy_scale" in scope_block

    # And the new module says the same thing in its own words.
    report_source = _source(_R1_REPORT_PATH)
    assert "R4" in report_source
    assert "OUT OF SCOPE" in report_source


# ---------------------------------------------------------------------------
# 9. (P3 review follow-ups) gating predicate, lot-size warning, NaN guard
# ---------------------------------------------------------------------------
def _r1_series():
    """The same hand-written buy/sell/hold/cash shape the P3 tests use."""
    cash_proxy = "511880.SH"
    target = pd.Series(
        {
            "000001.SZ": 0.32,   # BUY  (0.10 -> 0.32)
            "600519.SH": 0.10,   # SELL (0.24 -> 0.10)
            "000002.SZ": 0.05,   # HOLD (0.05 -> 0.05)
            cash_proxy: 0.53,
        }
    )
    current = pd.Series(
        {"000001.SZ": 0.10, "600519.SH": 0.24, "000002.SZ": 0.05, cash_proxy: 0.61}
    )
    return target, current, cash_proxy


def test_resolve_r1_derisk_target_off_returns_same_object(stage2):
    """`enforced=False` is a pure pass-through: IDENTICAL object, `applied=False`.

    Identity (not `equals`) is the contract the default/OFF path relies on -- any
    copy would let a downstream in-place edit leak between P2 and P3.
    """
    mod = stage2
    target, current, cash_proxy = _r1_series()

    res, applied = mod.resolve_r1_derisk_target(
        target, current, cash_proxy, enforced=False, r1_multiplier=0.25
    )
    assert res is target
    assert applied is False
    assert res.dtype == target.dtype

    # A de-risking multiplier that WOULD move a buy is present, so this is not a
    # vacuous pass through a `1.0` multiplier.
    assert 0.25 < 1.0
    assert list(res.items()) == list(target.items())


def test_resolve_r1_derisk_target_kill_switch(stage2, monkeypatch):
    """The env kill-switch resolves `enforced` to False before the helper runs."""
    mod = stage2
    target, current, cash_proxy = _r1_series()

    monkeypatch.delenv(_P2_BASELINE_ENV, raising=False)
    assert mod.r1_derisk_enforcement_enabled(True) is True
    monkeypatch.setenv(_P2_BASELINE_ENV, "1")
    enforced = mod.r1_derisk_enforcement_enabled(True)
    assert enforced is False

    res, applied = mod.resolve_r1_derisk_target(
        target, current, cash_proxy, enforced=enforced, r1_multiplier=0.25
    )
    assert res is target
    assert applied is False
    assert list(res.items()) == list(target.items())


def test_resolve_r1_derisk_target_multiplier_ge_one(stage2):
    """`None` / `1.0` / `1.2` / non-finite => identity, `applied=False`, no copy."""
    mod = stage2
    target, current, cash_proxy = _r1_series()

    for multiplier in (None, 1.0, 1.2, float("nan")):
        res, applied = mod.resolve_r1_derisk_target(
            target, current, cash_proxy, enforced=True, r1_multiplier=multiplier
        )
        assert res is target, multiplier
        assert applied is False, multiplier
        assert all(math.isfinite(float(v)) for v in res.to_numpy()), multiplier


def test_resolve_r1_derisk_target_applies(stage2):
    """`0.25` scales buy increments only; `applied=True`; sells/holds/cash verbatim."""
    mod = stage2
    target, current, cash_proxy = _r1_series()

    res, applied = mod.resolve_r1_derisk_target(
        target, current, cash_proxy, enforced=True, r1_multiplier=0.25
    )
    assert applied is True
    assert res is not target

    # Buy: cur + (tgt - cur) * 0.25, bit-identical to the frozen rule.
    assert res["000001.SZ"] == current["000001.SZ"] + (
        target["000001.SZ"] - current["000001.SZ"]
    ) * 0.25
    assert res["000001.SZ"] < target["000001.SZ"]
    for sym in ("600519.SH", "000002.SZ", cash_proxy):
        assert res[sym] == target[sym], sym
    # The caller's own input is untouched (the helper never mutates in place).
    assert target["000001.SZ"] == 0.32


def test_r1_report_warns_on_missing_lot_size(tmp_path, capsys):
    """A missing `lot_rounding.lot_size` is announced per symbol, and still reported."""
    from common.r1_derisk_report import main as report_main  # noqa: PLC0415

    ticket = {
        "as_of": "2026-11-09",
        "portfolio_value": 1_000_000.0,
        "orders": [
            # HK board lot (500/1000) with no recorded lot => the 100 default is wrong.
            {
                "symbol": "00700.HK",
                "price": 168.0,
                "target_weight": 0.10,
                "current_weight": 0.0,
                "detail_reason": {"lot_rounding": {}},
            },
            # A row that DOES carry its lot must stay silent.
            {
                "symbol": "000001.SZ",
                "price": 20.0,
                "target_weight": 0.10,
                "current_weight": 0.0,
                "detail_reason": {"lot_rounding": {"lot_size": 100}},
            },
        ],
    }
    ticket_path = tmp_path / "ticket.json"
    ticket_path.write_text(json.dumps(ticket), encoding="utf-8")
    out_path = tmp_path / "report.json"

    assert (
        report_main(
            [
                "--ticket",
                str(ticket_path),
                "--r1-multiplier",
                "0.25",
                "--output",
                str(out_path),
            ]
        )
        == 0
    )

    err = capsys.readouterr().err
    assert "00700.HK" in err
    assert "assuming lot size 100" in err
    assert err.count("warning") == 1, err
    assert "000001.SZ" not in err

    # The fallback kept the operator tool alive and the report is still strict JSON.
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert [row["symbol"] for row in report["symbols"]] == ["00700.HK", "000001.SZ"]


def test_non_finite_effective_buy_scale_never_reaches_the_block(stage2):
    """`effective_buy_scale` is `_json_number`-guarded: NaN/Infinity => absent, never NaN.

    The ticket writer uses `json.dump(...)` WITHOUT `allow_nan=False`, so a raw
    `real_scale * multiplier` could emit a bare `NaN` token into the ticket.
    """
    mod = stage2
    # The guard itself (the block's other numerics use the same one).
    assert mod._json_number(float("nan")) is None
    assert mod._json_number(float("inf")) is None
    assert mod._json_number(float("-inf")) is None
    assert mod._json_number(0.125) == 0.125
    assert mod._json_number(None) is None

    # Structural proof that the ASSEMBLY site still routes through it.
    source = _source(_STAGE2_PATH)
    tree = ast.parse(source)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == "advisory_block"
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "effective_buy_scale"
    ]
    assert len(assignments) == 1, assignments
    # The written value is NOT the raw product (`real_scale * multiplier`)...
    assert not isinstance(assignments[0].value, ast.BinOp), ast.dump(assignments[0].value)
    # ...and `_json_number` guards it in the same branch.
    applied_blocks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "r1_applied"
    ]
    assert applied_blocks
    guarded = [
        node
        for block in applied_blocks
        for node in ast.walk(block)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_json_number"
        and "real_intraday_buy_scale"
        in (ast.get_source_segment(source, node) or "")
    ]
    assert guarded, "effective_buy_scale must be _json_number-guarded at the assembly site"


def test_p3_enforced_run_marks_r1_gates_orders(stage2):
    """Enforced runs set `r1.gates_orders=True`; the TOP-LEVEL fence stays `False`.

    `common/advisory_report.py:123` hard-codes the conservative `False` inside the
    `r1` sub-block, so the caller must flip it exactly where it also flips
    `r1_effective` -- otherwise an audit consumer reads "orders not resized" next
    to `effective_buy_scale=0.125`.
    """
    source = _source(_STAGE2_PATH)
    tree = ast.parse(source)

    def _is_subscript(node, *keys):
        if not isinstance(node, ast.Subscript):
            return False
        if not isinstance(node.slice, ast.Constant) or node.slice.value != keys[-1]:
            return False
        if len(keys) == 1:
            return isinstance(node.value, ast.Name) and node.value.id == "advisory_block"
        return _is_subscript(node.value, *keys[:-1])

    def _assigns_true(node):
        return isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
            and node.value.value is True

    # Top-level `gates_orders` is NEVER written by the caller (builder owns it).
    for node in ast.walk(tree):
        for target in getattr(node, "targets", []):
            assert not _is_subscript(target, "gates_orders"), (
                "main() must not rewrite the top-level advisory gates_orders fence"
            )

    applied_blocks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "r1_applied"
    ]
    assert applied_blocks, "main() must still branch on `r1_applied`"
    seen = []
    for block in applied_blocks:
        for node in block.body:
            for target in getattr(node, "targets", []):
                if _is_subscript(target, "r1", "gates_orders"):
                    seen.append(node)
    assert len(seen) == 1, "exactly one `advisory_block['r1']['gates_orders'] = True`"
    assert _assigns_true(seen[0])

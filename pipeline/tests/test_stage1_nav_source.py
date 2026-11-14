"""Offline regression suite for the LV1 Stage-1 gate-NAV source
(`scripts/check_live_portfolio_health.py`).

FROZEN CONTRACT UNDER TEST (mirrored verbatim from the module-level block that sits
above `NAV_BREAKDOWN_FILENAME` in the script -- read that block, not this docstring,
as the owner):

(a) SOURCE OF TRUTH. The drawdown / circuit-breaker / tier math in
    `inspect_portfolio_drawdown` is fed the MARKED-TO-MARKET (MTM) NAV read from
    `nav_breakdown.json::nav_effective`. The operator form value
    (`--portfolio-value`) is NOT the gate NAV.
(b) DISAGREEMENT. When MTM and the form value differ, `tier`, `drawdown_pct` and
    `tier1_consecutive_bars` are recomputed FROM MTM; the form value survives only as
    an input-sanity cross-check that is persisted and rendered.
(c) MISSING MTM. An absent/unreadable/non-positive `nav_effective` is an EXPLICIT
    degradation (`nav_source = "form_fallback_refused"`,
    `nav_gate_status = "DEGRADED_NAV_UNAVAILABLE"`, `tier_status = "UNKNOWN"`,
    `nav_deviation_pct`/`nav_deviation_exceeded` left `None`), loudly warned and
    rendered as UNKNOWN. Missing != inside-threshold, and missing is never reported as
    "breaker not triggered".
INVALIDATION CONDITION: void if the valuation layer stops writing `nav_effective`
    beside `account_state.json`, or if the form value re-enters the gate arithmetic.

100% OFFLINE: every fixture below is a hand-written `nav_breakdown.json` /
synthetic state dict under `tmp_path`. No network, no market data, and **no** read of
any archived `account_state.json` / production `nav_breakdown.json` under `data/runs/`.

Reverse-mutation notice: each test below was verified to go RED when its criterion is
deleted (details recorded in the LV1 execution record): flipping the gate back to
`current_nav=args.portfolio_value`, coercing a `None` deviation to `0.0`/`False`, and
dropping the UNKNOWN relabel each fail a specific assertion here.
"""

import importlib.util
import json
import os
import sys
import types

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common.duckdb_cache import DuckDBCache  # noqa: E402  (offline in-memory/on-disk cache)
from scripts import sync_china_5m_and_indices as sync  # noqa: E402  (LV24 Stage-1 patch primitive)
from scripts.live_preflight import (  # noqa: E402  (repo root is on sys.path just above)
    LiveDumpPreflightError,
    assert_account_state_version_parity,
    assert_dump_version_parity,
)


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script("check_live_portfolio_health", os.path.join("scripts", "check_live_portfolio_health.py"))

# The archived two-day facts (10-30 / 11-02) that LV1 is written against. Reproduced
# as literals here so the suite never touches `data/runs/**`.
FORM_NAV_FROZEN = 199935.46   # summary.json::portfolio_value, identical on both days
PEAK_NAV = 203049.96          # account_state.json::peak_nav (L4)
MTM_NAV_1030 = 191630.06      # nav_breakdown.json::nav_effective (L3, 2026-10-30)
MTM_NAV_1102 = 192096.06      # nav_breakdown.json::nav_effective (L3, 2026-11-02)
RECORDED_DD = -0.015338589576673667  # what the form value produced (L15)


def _write_breakdown(tmp_path, payload, name="nav_breakdown.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _args(breakdown_file, form_nav=FORM_NAV_FROZEN):
    return types.SimpleNamespace(portfolio_value=form_nav, nav_breakdown_file=breakdown_file)


def _flat_state():
    """Zeroed counters + empty 10d history.

    The counters MUST be zero: `inspect_portfolio_drawdown` auto-heals (resets HWM and
    `current_dd` to 0) once a tier counter reaches its cooldown, which would mask the
    very assertion under test.
    """
    return {
        "as_of_date": None,
        "peak_nav": None,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [],
    }


def _dd_for(nav, as_of_date="2026-11-02"):
    return _mod.inspect_portfolio_drawdown(
        current_nav=nav,
        peak_nav=PEAK_NAV,
        state=_flat_state(),
        params={},
        as_of_date=as_of_date,
    )


_MACRO_INFO = {
    "breadth": 0.40,
    "thrust": 0.45,
    "breadth_active": True,
    "thrust_active": False,
    "target_exposure": 0.50,
    "breadth_thresh": 0.30,
    "thrust_thresh": 0.60,
    "leaders": [],
    "tracked_symbols_count": 3,
}

_VOL_INFO = {
    "realized_vol_21d": 0.18,
    "target_vol": 0.20,
    "vol_scaling_factor": 1.0,
    "vol_elevated": False,
}

_STRATEGY_INFO = {"strategy_name": "unit-test", "template_name": "unit-test", "strategy_version": "0.0.0"}


def _render(out_dir, dd_info):
    """Write the real Stage-1 reports and return (report_dict, markdown_text)."""
    _mod.generate_reports(
        output_dir=str(out_dir),
        as_of_date="2026-11-02",
        strategy_info=_STRATEGY_INFO,
        dd_info=dd_info,
        macro_info=_MACRO_INFO,
        vol_info=_VOL_INFO,
        gate_code="GO",
        gate_headline="🟢 GO",
        actions=[],
    )
    with open(os.path.join(str(out_dir), _mod.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8") as f:
        report = json.load(f)
    with open(os.path.join(str(out_dir), "stage1_health_report.md"), encoding="utf-8") as f:
        md = f.read()
    return report, md


# ---------------------------------------------------------------------------
# Case 1 -- DEVIATION STATE: MTM != form value -> tiers/counters recomputed from MTM
# ---------------------------------------------------------------------------


def test_case1_deviation_state_gates_on_mtm_and_persists_deviation(tmp_path, capsys):
    breakdown = _write_breakdown(
        tmp_path, {"as_of": "2026-11-02", "nav_effective": MTM_NAV_1030, "cash": 47800.06}
    )

    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    captured = capsys.readouterr()
    # The deviation must be LOUD, not a log-only footnote.
    assert "[WARN] (LV1)" in captured.out

    assert resolution["nav_source"] == _mod.NAV_SOURCE_MTM
    assert resolution["nav_mtm"] == pytest.approx(MTM_NAV_1030)
    assert resolution["nav_form"] == pytest.approx(FORM_NAV_FROZEN)
    assert resolution["nav_gate_nav"] == pytest.approx(MTM_NAV_1030)
    # signed deviation measured against the form value: the form OVERSTATES NAV here
    assert resolution["nav_deviation_pct"] == pytest.approx((MTM_NAV_1030 - FORM_NAV_FROZEN) / FORM_NAV_FROZEN)
    assert resolution["nav_deviation_exceeded"] is True

    dd_info = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav"]), resolution)

    # (b) the drawdown is the MTM one, not the recorded form-value one.
    assert dd_info["drawdown_pct"] == pytest.approx(MTM_NAV_1030 / PEAK_NAV - 1.0)
    assert dd_info["drawdown_pct"] == pytest.approx(-0.056241823440891126)
    assert dd_info["drawdown_pct"] != pytest.approx(RECORDED_DD)
    assert dd_info["current_nav"] == pytest.approx(MTM_NAV_1030)
    assert dd_info["tier_status"] == "EVALUATED"
    assert dd_info["nav_breaker_evaluated"] is True
    # -5.62% is still below the (unchanged, frozen) Tier-1 10% threshold.
    assert dd_info["tier"] == "NORMAL"
    assert dd_info["tier1_max"] == 15

    # The tier-1 COUNTER must be driven by MTM as well: at the same peak, a form value
    # of 199935.46 never crosses 10%, while a 10.37% MTM drawdown advances the counter.
    assert _dd_for(FORM_NAV_FROZEN)["tier1_counter"] == 0
    deep = _mod.attach_nav_provenance(
        _dd_for(182000.0),
        _mod.resolve_gate_nav({"nav_effective": 182000.0}, FORM_NAV_FROZEN),
    )
    assert deep["drawdown_pct"] == pytest.approx(182000.0 / PEAK_NAV - 1.0)
    assert deep["tier1_counter"] == 1
    assert deep["tier"] == "TIER_1_DAMPING"

    report, md = _render(tmp_path / "out", dd_info)
    health = report["portfolio_health"]
    # (3) structured deviation fields in the report JSON, alongside the gate numbers.
    assert health["nav_source"] == "mtm"
    assert health["nav_mtm"] == pytest.approx(MTM_NAV_1030)
    assert health["nav_form"] == pytest.approx(FORM_NAV_FROZEN)
    assert health["nav_deviation_pct"] == pytest.approx((MTM_NAV_1030 - FORM_NAV_FROZEN) / FORM_NAV_FROZEN)
    assert health["nav_deviation_exceeded"] is True
    assert health["drawdown_pct"] == pytest.approx(-0.056241823440891126)

    # (3) the Markdown NAV row must show the MTM number AND the deviation -- a log line
    # alone would not satisfy this.
    assert "Gate NAV Source (LV1)" in md
    assert "191,630.06" in md
    assert "-4.15%" in md
    assert "NAV SOURCE DEVIATION" in md
    assert "199,935.46" in md


# ---------------------------------------------------------------------------
# Case 2 -- EQUAL STATE: MTM == form -> bit-identical gate output (added fields only)
# ---------------------------------------------------------------------------


def test_case2_equal_state_is_bit_identical_apart_from_added_fields(tmp_path, capsys):
    breakdown = _write_breakdown(tmp_path, {"as_of": "2026-11-02", "nav_effective": FORM_NAV_FROZEN})

    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    captured = capsys.readouterr()
    # No deviation warning and no degradation warning on a clean, agreeing run.
    assert "[WARN] (LV1)" not in captured.out
    assert resolution["nav_source"] == _mod.NAV_SOURCE_MTM
    assert resolution["nav_deviation_pct"] == pytest.approx(0.0)
    assert resolution["nav_deviation_exceeded"] is False

    gate_dd = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav"]), resolution)
    # The pre-LV1 code path, reproduced verbatim: gate NAV == form value.
    legacy_dd = _dd_for(FORM_NAV_FROZEN)

    added = {
        "nav_source",
        "nav_mtm",
        "nav_form",
        "nav_deviation_pct",
        "nav_deviation_exceeded",
        "nav_deviation_threshold_pct",
        "nav_breakdown_status",
        "nav_breakdown_as_of",
        "nav_gate_status",
        "nav_degradation_reason",
        "tier_status",
        "nav_breaker_evaluated",
    }
    assert set(gate_dd) - set(legacy_dd) == added
    # BIT-IDENTICAL on every pre-existing key: no threshold, scale, counter or label moved.
    for key in legacy_dd:
        assert gate_dd[key] == legacy_dd[key], f"{key} changed on an equal-NAV run"

    report, md = _render(tmp_path / "out", gate_dd)
    assert report["portfolio_health"]["drawdown_pct"] == pytest.approx(RECORDED_DD)
    assert report["portfolio_health"]["tier"] == "NORMAL"
    assert "199,935.46" in md
    # No deviation banner when the two agree.
    assert "NAV SOURCE DEVIATION" not in md
    assert "GATE NAV DEGRADED" not in md


# ---------------------------------------------------------------------------
# Case 3 -- MTM MISSING: explicit degradation, no silent form fallback, not "not triggered"
# ---------------------------------------------------------------------------


def test_case3_missing_mtm_degrades_explicitly_and_never_claims_not_triggered(tmp_path, capsys):
    missing_path = str(tmp_path / "nav_breakdown.json")  # deliberately never written

    resolution = _mod.resolve_gate_nav_context(_args(missing_path))
    captured = capsys.readouterr()
    assert "[WARN] (LV1)" in captured.out  # loud
    assert "UNKNOWN" in captured.out       # ... and truthful

    assert resolution["nav_source"] == _mod.NAV_SOURCE_FORM_FALLBACK_REFUSED
    assert resolution["nav_gate_status"] == _mod.NAV_GATE_STATUS_DEGRADED
    # (LV11) An absent snapshot in an otherwise EMPTY run dir is a TIMING GAP, not a loss:
    # the two used to collapse into `FILE_MISSING`, which made "not staged yet" and "should
    # be there but isn't" indistinguishable in the archive.
    assert (
        resolution["nav_breakdown_status"]
        == _mod.NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN
    )
    # The gate NAV itself is NOT silently the form value.
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_mtm"] is None
    assert resolution["nav_deviation_pct"] is None
    assert resolution["nav_deviation_exceeded"] is None
    assert resolution["nav_degradation_reason"]

    dd_info = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav_used"]), resolution)
    assert dd_info["tier_status"] == "UNKNOWN"
    assert dd_info["nav_breaker_evaluated"] is False
    assert "UNKNOWN" in dd_info["tier_label"]
    assert "Full Risk Budget" not in dd_info["tier_label"]

    report, md = _render(tmp_path / "out", dd_info)
    health = report["portfolio_health"]
    assert health["nav_gate_status"] == "DEGRADED_NAV_UNAVAILABLE"
    assert health["nav_source"] == "form_fallback_refused"
    assert health["tier_status"] == "UNKNOWN"
    assert health["nav_mtm"] is None
    assert health["nav_deviation_exceeded"] is None

    assert "GATE NAV DEGRADED" in md
    # Must not be presented as a clean, passing breaker state.
    assert "🟢 NORMAL (Full Risk Budget)" not in md
    assert "UNKNOWN" in md
    # The NAV cell itself must stop claiming to be a tracked NAV.
    assert "UNVERIFIED (表单占位值，不是盯市净值)" in md


def test_case3b_empty_or_invalid_nav_effective_is_also_degraded(tmp_path, capsys):
    """A present-but-unusable `nav_effective` degrades exactly like a missing file."""
    for payload, expected in (
        ({"as_of": "2026-11-02"}, _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_MISSING),
        ({"nav_effective": None}, _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_MISSING),
        ({"nav_effective": "not-a-number"}, _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID),
        ({"nav_effective": 0.0}, _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID),
        ({"nav_effective": -5.0}, _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID),
    ):
        path = _write_breakdown(tmp_path, payload)
        resolution = _mod.resolve_gate_nav_context(_args(path))
        assert resolution["nav_breakdown_status"] == expected, payload
        assert resolution["nav_gate_nav"] is None, payload
        assert resolution["nav_source"] == _mod.NAV_SOURCE_FORM_FALLBACK_REFUSED, payload
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Case 4 -- MISSING != INSIDE THRESHOLD: `None` must never be coerced to 0.0 / False
# ---------------------------------------------------------------------------


def test_case4_missing_deviation_is_distinguishable_from_inside_threshold(tmp_path):
    missing = _mod.resolve_gate_nav(None, FORM_NAV_FROZEN, breakdown_status=_mod.NAV_BREAKDOWN_STATUS_FILE_MISSING)
    equal = _mod.resolve_gate_nav({"nav_effective": FORM_NAV_FROZEN}, FORM_NAV_FROZEN)
    within = _mod.resolve_gate_nav({"nav_effective": FORM_NAV_FROZEN * 1.005}, FORM_NAV_FROZEN)

    # measured-but-inside-threshold == False; not-measured == None. Never the same value.
    assert equal["nav_deviation_exceeded"] is False
    assert within["nav_deviation_exceeded"] is False
    assert missing["nav_deviation_exceeded"] is None
    assert missing["nav_deviation_exceeded"] is not False
    assert missing["nav_deviation_exceeded"] != within["nav_deviation_exceeded"]

    # ... and the percentage is absent, not a fabricated flat 0.00%.
    assert equal["nav_deviation_pct"] == pytest.approx(0.0)
    assert missing["nav_deviation_pct"] is None
    assert missing["nav_deviation_pct"] != 0.0

    # The distinction survives the JSON round-trip consumers actually read.
    res_missing = _mod.resolve_gate_nav_context(_args(str(tmp_path / "absent.json")))
    report_missing, md_missing = _render(
        tmp_path / "out_missing",
        _mod.attach_nav_provenance(_dd_for(res_missing["nav_gate_nav_used"]), res_missing),
    )
    assert report_missing["portfolio_health"]["nav_deviation_exceeded"] is None
    assert report_missing["portfolio_health"]["nav_deviation_pct"] is None
    assert "not measurable" not in md_missing or "UNKNOWN" in md_missing

    res_equal = _mod.resolve_gate_nav_context(
        _args(_write_breakdown(tmp_path, {"nav_effective": FORM_NAV_FROZEN}, name="eq.json"))
    )
    report_equal, _ = _render(
        tmp_path / "out_equal",
        _mod.attach_nav_provenance(_dd_for(res_equal["nav_gate_nav"]), res_equal),
    )
    assert report_equal["portfolio_health"]["nav_deviation_exceeded"] is False
    assert report_equal["portfolio_health"]["nav_deviation_pct"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Wiring guard -- the gate must be fed the resolved NAV, never `args.portfolio_value`
# ---------------------------------------------------------------------------


def test_gate_call_site_uses_resolved_nav_not_the_form_value():
    """Source-level guard: reverting the wiring must fail loudly.

    `inspect_portfolio_drawdown(current_nav=args.portfolio_value, ...)` is precisely
    the LV1 defect (the form value used as the NAV), so its absence is asserted
    literally rather than inferred from behaviour.
    """
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()

    assert "current_nav=args.portfolio_value" not in source
    assert "current_nav=gate_nav" in source
    assert "resolve_gate_nav_context(args)" in source
    # (LV12, 2026-11-05) Stage 2's argv NO LONGER speaks the operator's form value -- LV1 left it
    # that way, and LV12 closed exactly that gap: the child is launched with the SAME resolved
    # gate NAV (`gate_nav`, i.e. MTM). The guard is inverted rather than dropped, so a revert to
    # `str(args.portfolio_value)` still fails loudly here.
    assert "str(args.portfolio_value)" not in source
    assert "portfolio_value=gate_nav" in source


# ---------------------------------------------------------------------------
# LV11 -- the two absences are different facts, and a pinned snapshot fails closed
# ---------------------------------------------------------------------------


def test_lv11_absent_snapshot_splits_timing_gap_from_true_loss(tmp_path, capsys):
    """`SNAPSHOT_NOT_YET_WRITTEN` (timing gap) must not read like `SNAPSHOT_MISSING`.

    Both used to collapse into `FILE_MISSING`, so an auditor could not tell "Stage 1 ran
    before anything was marked to market" from "this run dir already had a snapshot and it
    is gone". Reverse-mutation: return `NAV_BREAKDOWN_STATUS_FILE_MISSING` for every absent
    file again -> both asserts below go red.
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    gap_status = _mod.resolve_gate_nav_context(_args(str(empty_dir / "nav_breakdown.json")))[
        "nav_breakdown_status"
    ]
    assert gap_status == _mod.NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN

    # A run dir that ALREADY carries a round artifact proves a valuation round happened, so
    # an absent snapshot there is a LOSS, not a deferral.
    used_dir = tmp_path / "used"
    used_dir.mkdir()
    (used_dir / "summary.json").write_text("{}", encoding="utf-8")
    loss_status = _mod.resolve_gate_nav_context(_args(str(used_dir / "nav_breakdown.json")))[
        "nav_breakdown_status"
    ]
    assert loss_status == _mod.NAV_BREAKDOWN_STATUS_SNAPSHOT_MISSING
    assert gap_status != loss_status

    out = capsys.readouterr().out
    assert "SNAPSHOT_NOT_YET_WRITTEN" in out
    assert "SNAPSHOT_MISSING" in out


def test_lv11_explicitly_pinned_snapshot_must_exist(tmp_path):
    """`--nav-breakdown-file` pointing at nothing RAISES -- it does not degrade and continue.

    Reverse-mutation: drop the `explicit` branch in `load_nav_breakdown` -> no exception and
    the acceptance criterion "指向不存在或不可解析的快照 => 抛错" is silently gone.
    """
    missing = str(tmp_path / "pinned.json")
    args = _args(missing)
    # `parse_args` is the ONLY place that records "the operator pinned this path"; the LV1
    # test fixtures deliberately do not set it (so their degraded-path behaviour is intact).
    args.nav_breakdown_file_explicit = True
    with pytest.raises(_mod.NavSnapshotError):
        _mod.resolve_gate_nav_context(args)

    # Unparsable JSON under a pinned path is the same refusal, not a degraded report.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    args_broken = _args(str(broken))
    args_broken.nav_breakdown_file_explicit = True
    with pytest.raises(_mod.NavSnapshotError):
        _mod.resolve_gate_nav_context(args_broken)

    # ... and a readable pinned snapshot still resolves normally (no over-refusal).
    good = _write_breakdown(tmp_path, {"nav_effective": MTM_NAV_1102}, name="good.json")
    args_good = _args(good)
    args_good.nav_breakdown_file_explicit = True
    assert _mod.resolve_gate_nav_context(args_good)["nav_gate_nav"] == pytest.approx(MTM_NAV_1102)


def test_lv11_parse_args_records_whether_the_snapshot_path_was_pinned(tmp_path):
    """The explicit/default distinction must survive `parse_args` itself (deployed path)."""
    parsed_default = _mod.parse_args(
        ["--output-dir", str(tmp_path), "--data-provider", "synthetic"]
    )
    assert parsed_default.nav_breakdown_file_explicit is False
    assert parsed_default.nav_breakdown_file == str(
        os.path.join(str(tmp_path), _mod.NAV_BREAKDOWN_FILENAME)
    )

    pinned = str(tmp_path / "pinned.json")
    parsed_pinned = _mod.parse_args(
        ["--output-dir", str(tmp_path), "--data-provider", "synthetic", "--nav-breakdown-file", pinned]
    )
    assert parsed_pinned.nav_breakdown_file_explicit is True
    assert parsed_pinned.nav_breakdown_file == pinned


# ---------------------------------------------------------------------------
# LV11 item 6 -- the degraded state must fail closed in the MACHINE-READABLE layer
# ---------------------------------------------------------------------------


def test_lv11_degraded_run_persists_unknown_tier_and_leaves_nav_series_alone(tmp_path, capsys):
    """Two specific lies, both asserted against (2026-11-03 post-commit audit).

    1. `circuit_breaker_tier` used to be persisted as the placeholder's "NORMAL" while the
       report said UNKNOWN -- Stage 2 reads only that enum, so "archive says NORMAL" would
       have authorised orders on an unevaluated gate.
    2. `nav_history_10d` used to be fed the FORM PLACEHOLDER, polluting the 10-day recovery
       metric. A degraded run must leave the series untouched.

    Reverse-mutation: drop either branch condition in `build_account_state_update` and one of
    the asserts below goes red.
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    resolution = _mod.resolve_gate_nav_context(_args(str(empty_dir / "nav_breakdown.json")))
    degraded_dd = _mod.attach_nav_provenance(
        _dd_for(resolution["nav_gate_nav_used"]), resolution
    )
    assert degraded_dd["nav_gate_status"] == _mod.NAV_GATE_STATUS_DEGRADED

    baseline = {
        "nav_history_10d": [100000.0, 101000.0],
        "current_nav": 101000.0,
        "peak_nav": 105000.0,
        "circuit_breaker_tier": "NORMAL",
    }
    updated = _mod.build_account_state_update(
        dict(baseline),
        baseline,
        degraded_dd,
        as_of_date="2026-11-02",
        gate_code="GO",
        prior_state={"as_of_date": "2026-10-30"},
    )
    # 1. never a passing-looking tier
    assert updated["circuit_breaker_tier"] == "UNKNOWN"
    assert updated["circuit_breaker_tier"] != "NORMAL"
    assert updated["tier_status"] == "UNKNOWN"
    assert updated["nav_gate_status"] == _mod.NAV_GATE_STATUS_DEGRADED
    assert updated["nav_degraded_as_of"] == "2026-11-02"
    # 2. the 10-day series is NOT extended with a placeholder
    assert updated["nav_history_10d"] == [100000.0, 101000.0]
    # ... and the HWM chain is not overwritten with the form value either
    assert updated["current_nav"] == 101000.0
    assert updated["peak_nav"] == 105000.0
    assert "LV11" in capsys.readouterr().out

    # MEASURED run: byte-identical to pre-LV11 behaviour -- append the effective NAV and
    # persist the tier the math actually produced.
    good = _write_breakdown(tmp_path, {"nav_effective": MTM_NAV_1102}, name="good.json")
    resolution_ok = _mod.resolve_gate_nav_context(_args(good))
    ok_dd = _mod.attach_nav_provenance(_dd_for(resolution_ok["nav_gate_nav"]), resolution_ok)
    updated_ok = _mod.build_account_state_update(
        dict(baseline), baseline, ok_dd, as_of_date="2026-11-02", gate_code="GO"
    )
    assert updated_ok["circuit_breaker_tier"] == ok_dd["tier"] == "NORMAL"
    assert updated_ok["current_nav"] == pytest.approx(MTM_NAV_1102)
    assert len(updated_ok["nav_history_10d"]) == 3
    assert updated_ok["nav_history_10d"][-1] == pytest.approx(MTM_NAV_1102)
    assert updated_ok["tier_status"] == "EVALUATED"
    assert "nav_degraded_as_of" not in updated_ok


# ---------------------------------------------------------------------------
# LV7 -- frozen prior_state annotation, `nav_history_10d` distinct-count threshold,
#        and read-only dump/state version parity.
#
# Frozen contract: `docs/lv7_prior_state_contract.md` (§2 annotation, §3 threshold,
# §4 status vocabulary, §5 version parity). 100% offline: every fixture is a literal
# dict or a `tmp_path` JSON written here -- NOTHING reads `data/runs/**`.
# ---------------------------------------------------------------------------

# The 2026-11-02 archive's `nav_history_10d` (L8-L13), reproduced as literals: FIVE raw
# points whose last two are identical, i.e. FOUR distinct sessions.
_ARCHIVED_NAV_HISTORY = [199595.4, 200658.01, 197351.17, 199935.46, 199935.46]


def _dd_with_history(history, *, current_nav=105000.0):
    """`inspect_portfolio_drawdown` over an explicit 10-day series (no peak -> dd 0)."""
    state = dict(_flat_state())
    state["nav_history_10d"] = list(history)
    return _mod.inspect_portfolio_drawdown(
        current_nav=current_nav,
        peak_nav=None,
        state=state,
        params={},
        as_of_date="2026-11-02",
    )


def test_frozen_suspect_equal(tmp_path, capsys):
    """`prior_state.current_nav ==` today's NAV must be ANNOTATED, in BOTH write branches.

    Semantics (contract §2): `prior_state` is the previous session's confirmed snapshot.
    When the two values are bit-for-bit equal the carrier looks frozen; the run must say
    so. Reverse-mutation: delete the `detect_frozen_prior_state(...)` call / the
    `prior_state_frozen_suspect` kwarg pass-through and this test goes red.
    """
    prior_state = {
        "as_of_date": "2026-11-02",
        "current_nav": 199935.46,
        "nav_history_10d": list(_ARCHIVED_NAV_HISTORY),
    }

    reason = _mod.detect_frozen_prior_state(prior_state, 199935.46)
    assert reason, "identical NAV must produce a reason"
    assert "199,935.46" in reason
    assert "2026-11-02" in reason

    # (a) MEASURED branch
    breakdown = _write_breakdown(tmp_path, {"nav_effective": 199935.46})
    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    dd = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav"]), resolution)
    updated = _mod.build_account_state_update(
        {},
        {},
        dd,
        as_of_date="2026-11-02",
        gate_code="GO",
        prior_state=prior_state,
        prior_state_frozen_suspect=reason,
    )
    assert updated["prior_state_frozen_suspect"] is True
    assert "199,935.46" in updated["prior_state_frozen_reason"]
    # Annotation only: the NAV numbers themselves are untouched.
    assert updated["current_nav"] == pytest.approx(199935.46)
    assert updated["prior_state"] is prior_state
    # JSON-safe `true`, not the string "True".
    assert json.loads(json.dumps(updated))["prior_state_frozen_suspect"] is True
    assert "LV7" in capsys.readouterr().out

    # (b) DEGRADED branch -- a frozen carrier must not hide behind a degraded/green report.
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    res_bad = _mod.resolve_gate_nav_context(_args(str(empty / "nav_breakdown.json")))
    assert res_bad["nav_gate_status"] == _mod.NAV_GATE_STATUS_DEGRADED
    dd_bad = _mod.attach_nav_provenance(_dd_for(res_bad["nav_gate_nav_used"]), res_bad)
    updated_bad = _mod.build_account_state_update(
        {},
        {},
        dd_bad,
        as_of_date="2026-11-02",
        gate_code="GO",
        prior_state=prior_state,
        prior_state_frozen_suspect=reason,
    )
    assert updated_bad["circuit_breaker_tier"] == "UNKNOWN"
    assert updated_bad["prior_state_frozen_suspect"] is True
    assert updated_bad["prior_state_frozen_reason"] == reason

    # (c) WIRING. A pure-function test cannot see `main()`, so the production path is
    # pinned at the source level: the detector must be CALLED, its result passed into
    # `build_account_state_update`, and the annotation stamped in BOTH branches. Delete
    # any of those and this test goes red even though the helper itself still passes.
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert source.count("detect_frozen_prior_state(") == 2, "definition + one call site"
    assert "prior_state_frozen_suspect=prior_state_frozen_reason" in source
    assert source.count("**frozen_annotation,") == 2
    # The comparison value is the MEASURED gate NAV, never the display placeholder.
    assert 'nav_resolution["nav_gate_nav"],\n    )' in source


def test_no_marker_when_distinct():
    """A genuinely different prior NAV must emit NO key at all -- never `false`.

    Old consumers must not gain a field they never agreed to read; "absent" is the
    clean signal (contract §2).
    """
    prior_state = {"as_of_date": "2026-11-02", "current_nav": 199935.46}

    assert _mod.detect_frozen_prior_state(prior_state, 200658.01) is None
    # Unmeasurable on either side is "cannot tell", not "clean" and not "frozen".
    assert _mod.detect_frozen_prior_state(prior_state, None) is None
    assert _mod.detect_frozen_prior_state({}, 199935.46) is None
    assert _mod.detect_frozen_prior_state(None, 199935.46) is None
    assert _mod.detect_frozen_prior_state({"current_nav": "n/a"}, 199935.46) is None

    clean = _mod.build_account_state_update(
        {},
        {},
        _dd_for(200658.01),
        as_of_date="2026-11-02",
        gate_code="GO",
        prior_state=prior_state,
        prior_state_frozen_suspect=_mod.detect_frozen_prior_state(prior_state, 200658.01),
    )
    assert "prior_state_frozen_suspect" not in clean
    assert "prior_state_frozen_reason" not in clean
    assert clean.get("prior_state_frozen_suspect") is not False


def test_nav10d_distinct_three_states():
    """4 / 5 / 10 distinct observations -> INSUFFICIENT_HISTORY / INSUFFICIENT_HISTORY / numeric.

    The threshold's denominator is the DISTINCT count (contract §3), so 4 and 5 stay
    unmeasured and only 10 produces a number.
    """
    four = _dd_with_history([100000.0, 100100.0, 100200.0, 100300.0])
    five = _dd_with_history([100000.0, 100100.0, 100200.0, 100300.0, 100400.0])
    ten = _dd_with_history([100000.0 + 100.0 * i for i in range(10)], current_nav=100900.0)

    assert four["nav_10d_distinct_len"] == 4
    assert four["nav_10d_status"] == _mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
    assert four["nav_10d_return"] is None
    assert four["fast_recovery_nav"] is False

    assert five["nav_10d_distinct_len"] == 5
    assert five["nav_10d_status"] == _mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
    assert five["nav_10d_return"] is None

    assert ten["nav_10d_distinct_len"] == 10
    assert ten["nav_10d_status"] == _mod.NAV_10D_STATUS_OK
    assert ten["nav_10d_return"] == pytest.approx(100900.0 / 100000.0 - 1.0)
    assert ten["nav_10d_unavailable_reason"] is None
    assert ten["fast_recovery_nav"] is True


def test_duplicate_tail_counts_once():
    """The archived 5-point series has a duplicated tail -> FOUR distinct sessions.

    Reverse-mutation: revert `count_distinct_nav_points` to a plain `len(...)` and the
    distinct assertions below go red (raw length is 5, not 4).
    """
    assert _mod.count_distinct_nav_points(_ARCHIVED_NAV_HISTORY) == 4
    # Non-adjacent repeats are real observations and DO count (A, B, A -> 3).
    assert _mod.count_distinct_nav_points([1.0, 2.0, 1.0]) == 3
    # Unparseable points are skipped, not counted and not fatal.
    assert _mod.count_distinct_nav_points([1.0, "n/a", None, 2.0]) == 2
    assert _mod.count_distinct_nav_points([]) == 0

    dd = _dd_with_history(_ARCHIVED_NAV_HISTORY, current_nav=199935.46)
    assert dd["nav_10d_history_len"] == 5, "the raw record count must stay readable"
    assert dd["nav_10d_distinct_len"] == 4
    assert dd["nav_10d_status"] == _mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
    assert dd["nav_10d_return"] is None
    assert "去重后仅 4" in (dd["nav_10d_unavailable_reason"] or "")

    # Ten RAW records that are only nine distinct sessions never reach the threshold --
    # the freeze would otherwise pad itself to 10 by copying its own tail.
    padded = _dd_with_history([100000.0 + 100.0 * i for i in range(9)] + [100800.0])
    assert padded["nav_10d_history_len"] == 10
    assert padded["nav_10d_distinct_len"] == 9
    assert padded["nav_10d_status"] == _mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
    assert padded["nav_10d_return"] is None


def test_insufficient_never_zero():
    """Short / absent history must NEVER produce a numeric 0.0 return (contract §4).

    `INSUFFICIENT_HISTORY` is the only spelling of "unmeasured"; nothing anywhere may be
    read as "measured a flat 0.00%".
    """
    for history in ([], [100000.0], [100000.0, 100000.0, 100000.0], _ARCHIVED_NAV_HISTORY):
        dd = _dd_with_history(history, current_nav=100000.0)
        assert dd["nav_10d_status"] == _mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
        assert dd["nav_10d_status"] != _mod.NAV_10D_STATUS_OK
        assert dd["nav_10d_return"] is None
        assert dd["nav_10d_return"] != 0.0
        assert dd["fast_recovery_nav"] is False
        # Survives JSON as an explicit null, not as 0 / "0.0" / false.
        payload = json.loads(json.dumps(dd, default=str))
        assert payload["nav_10d_return"] is None
        assert payload["nav_10d_status"] == "INSUFFICIENT_HISTORY"
        assert payload["nav_10d_required_len"] == 10
        assert payload["nav_10d_history_len"] == len(history)


# ---------------------------------------------------------------------------
# LV7 §5 -- version parity is READ-ONLY and fails closed
# ---------------------------------------------------------------------------


def test_version_mismatch_raises():
    """Dump-vs-class and state-vs-class disagreements must raise, naming both values.

    Reverse-mutation: make `_assert_version_axis` return early (skip the comparison) and
    every assertion below goes red.
    """
    dump = {"strategy_version": "1.2.2", "base_version": "1.8.0"}
    with pytest.raises(LiveDumpPreflightError) as exc:
        assert_dump_version_parity(dump, "1.3.0", "1.8.0", dump_display="chan_x_strategy.json")
    message = str(exc.value)
    assert "1.2.2" in message and "1.3.0" in message
    assert "chan_x_strategy.json" in message

    # The base axis is checked independently: same strategy version, older engine base.
    with pytest.raises(LiveDumpPreflightError) as exc_base:
        assert_dump_version_parity(
            {"strategy_version": "1.2.2", "base_version": "1.7.0"}, "1.2.2", "1.8.0"
        )
    assert "1.7.0" in str(exc_base.value) and "1.8.0" in str(exc_base.value)

    # A carried-forward account_state written by another engine version is the same class
    # of failure (the archived 2026-11-02 state: 1.2.2 / 1.7.0 vs live 1.2.2 / 1.8.0).
    with pytest.raises(LiveDumpPreflightError) as exc_state:
        assert_account_state_version_parity(
            {"strategy_version": "1.2.2", "base_version": "1.7.0"},
            "1.2.2",
            "1.8.0",
            state_display="account_state.json",
        )
    assert "account_state.json" in str(exc_state.value)


def test_match_passes():
    """Equal metadata (and carriers that assert nothing) must pass silently."""
    assert_dump_version_parity(
        {"strategy_version": "1.2.2", "base_version": "1.8.0"}, "1.2.2", "1.8.0"
    )
    # `base_version: null` is the dumper's canonical spelling for a non-blend strategy.
    assert_dump_version_parity({"strategy_version": "1.0.0", "base_version": None}, "1.0.0", None)
    # A carrier with no version keys asserts nothing -> nothing to compare.
    assert_dump_version_parity({"template_name": "equal_weight"}, "1.0.0", None)
    assert_account_state_version_parity({}, "1.2.2", "1.8.0")
    assert_account_state_version_parity({"strategy_version": "1.2.2"}, "1.2.2", "1.8.0")
    # Read-only: neither call mutates its container.
    dump = {"strategy_version": "1.2.2", "base_version": "1.8.0"}
    state = {"strategy_version": "1.2.2", "base_version": "1.8.0"}
    assert_dump_version_parity(dump, "1.2.2", "1.8.0")
    assert_account_state_version_parity(state, "1.2.2", "1.8.0")
    assert dump == {"strategy_version": "1.2.2", "base_version": "1.8.0"}
    assert state == {"strategy_version": "1.2.2", "base_version": "1.8.0"}


def test_missing_recorded_version_raises():
    """An EXPLICITLY recorded but empty version field is malformed, not "no claim"."""
    with pytest.raises(LiveDumpPreflightError):
        assert_dump_version_parity({"strategy_version": ""}, "1.3.0")
    with pytest.raises(LiveDumpPreflightError):
        assert_dump_version_parity({"strategy_version": "   "}, "1.3.0")
    # A null base on a carrier whose live class DOES carry one cannot be confirmed.
    with pytest.raises(LiveDumpPreflightError):
        assert_dump_version_parity({"base_version": None}, "1.2.2", "1.8.0")
    with pytest.raises(LiveDumpPreflightError):
        assert_account_state_version_parity({"strategy_version": None}, "1.2.2", None)


# ---------------------------------------------------------------------------
# LV24 -- per-namespace trace on the Stage-1 FULL-UNIVERSE daily-bar refresh
#
# Contract: freeze doc `docs/lv19_freshness_contract.md` §4.1 (three mutually exclusive
# states, worst-first aggregation), §4.4 (`fetch_errors` shape/meaning FROZEN -- this suite
# must never change them), §6.6 (the gap LV24 closes: the Stage-1 child passed no
# `strict_namespaces` and produced no `per_namespace`, so "we skipped a namespace by
# tolerance" was invisible).
#
# WHAT THIS SECTION PINS
#   1. every namespace patched  -> the trace says `patched` for both, and `fetch_errors` is
#      BYTE-IDENTICAL to the pre-LV24 return value (same dict type, same emptiness);
#   2. a namespace skipped as fresh -> `skipped` + the REASON (`fresh_strict` vs
#      `fresh_tolerance`), and the refresh still reports success overall;
#   3. a failure -> `failed` with its reason, plus `rule` for a refused write (including the
#      CROSS-SOURCE refusal raised by `plan_history_write`), the rest of the trace unaffected;
#   4. worst-first aggregation (§4.2) is pinned, in BOTH symbol orders;
#   5. `fetch_errors` cannot drift: exact serialized bytes + a source-level guard.
#
# 100% OFFLINE: provider doubles return prepared synthetic frames (or raise); the cache is a
# `tmp_path` DuckDB file; `sync_indices_daily` is stubbed. No network, no `data/runs/**`, and
# no read of the production cache.
# ---------------------------------------------------------------------------

LV24_SYMBOL = "600660.SH"
LV24_SYMBOL_B = "601288.SH"
LV24_END = "2026-10-28"
FUYAO = sync.FUYAO_NAMESPACE
MARKETDB = sync.MARKETDB_NAMESPACE


def _lv24_frame(dates, base=40.0, step=0.37):
    """Deterministic OHLCV frame; non-round floats so a rescale could never be mistaken for it."""
    rows = []
    for i, _ in enumerate(dates):
        price = base + i * step
        rows.append(
            {
                "Open": price * 0.998712345678,
                "High": price * 1.004321098765,
                "Low": price * 0.995432109876,
                "Close": price,
                "Volume": 1_000_000.0 + 137.0 * i,
            }
        )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates))


_LV24_CONTINUOUS = _lv24_frame(pd.bdate_range("2026-10-01", LV24_END))


def _lv24_up_to(day):
    return _LV24_CONTINUOUS.loc[_LV24_CONTINUOUS.index <= day].copy()


def _lv24_refetch():
    """The look-back window plus the missing tail, i.e. what a real re-fetch returns."""
    return _LV24_CONTINUOUS.loc[_LV24_CONTINUOUS.index >= "2026-10-15"].copy()


class _Lv24Provider:
    """Provider double: one prepared frame per call (or a raise), recording every request."""

    def __init__(self, frame=None, error=None, error_symbols=(), **kwargs):
        self._frame = frame
        self._error = error
        self._error_symbols = set(error_symbols)
        self.kwargs = kwargs
        self.starts = []

    def fetch_raw_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        assert interval == "1d"
        self.starts.append((symbol, start))
        if self._error is not None and (not self._error_symbols or symbol in self._error_symbols):
            raise self._error
        return self._frame.copy()

    #: `getattr(prov, "fetch_raw_ohlcv", prov.fetch_ohlcv)` evaluates the fallback EAGERLY, so a
    #: double without this attribute fails on attribute access rather than on the call.
    def fetch_ohlcv(self, symbol, start, end, interval="1d", **kwargs):
        return self.fetch_raw_ohlcv(symbol, start, end, interval=interval, **kwargs)

    def logout(self):
        return None


def _lv24_store(cache, df, provider, symbol=LV24_SYMBOL):
    cache.store_bars(
        provider=provider,
        symbol=symbol,
        df=df,
        requested_start=df.index.min().strftime("%Y-%m-%d"),
        requested_end=df.index.max().strftime("%Y-%m-%d"),
        interval="1d",
        adjustflag="raw",
    )


def _lv24_seed(tmp_path):
    """Fresh `tmp_path` cache where BOTH namespaces lag beyond their own tolerance.

    MarketDB is the Stage-1 default strict namespace (zero tolerance), so `end - 1` already
    lags. Fuyao is NOT strict here (the Stage-1 child passes no `strict_namespaces`), so it must
    be more than two days behind to be fetched at all -- `end - 3` = Friday 2026-10-23.
    """
    db_file = str(tmp_path / "cache.duckdb")
    cache = DuckDBCache(db_file)
    _lv24_store(cache, _lv24_up_to("2026-10-27"), MARKETDB)
    _lv24_store(cache, _lv24_up_to("2026-10-23"), FUYAO)
    return db_file


def _lv24_install(monkeypatch, bs, em, capture=False):
    monkeypatch.setattr(sync, "BaoStockDataProvider", lambda **kw: bs)
    monkeypatch.setattr(sync, "EastMoneyDataProvider", lambda **kw: em)
    monkeypatch.setattr(sync, "sync_indices_daily", lambda *a, **kw: {})
    captured = {}
    if capture:
        real_patch = sync.patch_equity_daily_bars

        def _wrapped(*args, **kwargs):
            result = real_patch(*args, **kwargs)
            captured["result"] = result
            return result

        monkeypatch.setattr(sync, "patch_equity_daily_bars", _wrapped)
    return captured


def _lv24_run(db_file, symbols=(LV24_SYMBOL,)):
    return _mod.refresh_stage1_data(
        list(symbols),
        LV24_END,
        "marketdb",  # only used for the synthetic/no-cache short circuit
        cache_dir=db_file,
    )


# ---------------------------------------------------------------------------
# 1. All namespaces patched -> both `patched`; `fetch_errors` unchanged
# ---------------------------------------------------------------------------


def test_lv24_all_namespaces_patched_are_traced(tmp_path, monkeypatch, capsys):
    """Both write namespaces lag beyond their own tolerance => both fetched, both `patched`.

    Reverse-mutation: drop the LV24 trace population (`trace_out` never filled) and this test
    goes red on the missing report block -- the refresh itself keeps working, which is exactly
    the defect LV24 records (a trace gap, not a data defect).
    """
    db_file = _lv24_seed(tmp_path)
    captured = _lv24_install(
        monkeypatch,
        _Lv24Provider(_lv24_refetch()),
        _Lv24Provider(_lv24_refetch()),
        capture=True,
    )

    summary = _lv24_run(db_file)

    trace = summary["equity_daily_patch"]
    assert trace["namespace_trace"] == {FUYAO: "patched", MARKETDB: "patched"}
    assert trace["namespace_detail"] == {
        FUYAO: {"state": "patched", "reason": None, "symbol": LV24_SYMBOL},
        MARKETDB: {"state": "patched", "reason": None, "symbol": LV24_SYMBOL},
    }
    # FROZEN `fetch_errors`: a plain dict, empty on a clean run -- unchanged by LV24.
    assert type(captured["result"]) is dict
    assert captured["result"] == {}
    assert summary["status"] == "completed"
    assert summary["steps"]["patch_equity_daily_bars"] == "ok"
    assert "fetch_errors" not in summary
    # Nothing to warn about: every namespace was refreshed (no noise on a green run).
    assert "[STAGE 1 NAMESPACE TRACE]" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. A namespace skipped as FRESH -> `skipped` + reason, refresh still a success
# ---------------------------------------------------------------------------


def test_lv24_fresh_namespace_skip_records_reason_and_still_succeeds(
    tmp_path, monkeypatch, capsys
):
    """MarketDB at `end` (strict, zero tolerance) + Fuyao one day behind (holiday tolerance).

    The reason field is the whole point: `skipped` alone cannot distinguish "current" from
    "a tolerance said close enough". `fresh_tolerance` is the one that must be visible, because
    it is the namespace the valuation read may take a stale bar from (freeze §6.6).
    """
    db_file = str(tmp_path / "cache.duckdb")
    seed = DuckDBCache(db_file)
    _lv24_store(seed, _lv24_up_to(LV24_END), MARKETDB)   # strict + genuinely fresh
    _lv24_store(seed, _lv24_up_to("2026-10-27"), FUYAO)  # within the 2-day tolerance

    bs = _Lv24Provider(_lv24_refetch())
    em = _Lv24Provider(_lv24_refetch())
    _lv24_install(monkeypatch, bs, em)

    summary = _lv24_run(db_file)

    trace = summary["equity_daily_patch"]
    assert trace["namespace_trace"] == {FUYAO: "skipped", MARKETDB: "skipped"}
    assert trace["namespace_detail"][MARKETDB]["reason"] == sync.FRESH_STRICT
    assert trace["namespace_detail"][FUYAO]["reason"] == sync.FRESH_TOLERANCE
    # Nothing lagged: no request may be issued at all (the tolerance is still intact).
    assert bs.starts == [] and em.starts == []

    # The refresh itself is still a SUCCESS -- observation, not enforcement.
    assert summary["status"] == "completed"
    assert summary["steps"]["patch_equity_daily_bars"] == "ok"
    assert "fetch_errors" not in summary

    out = capsys.readouterr().out
    assert "[STAGE 1 NAMESPACE TRACE]" in out
    assert f"{FUYAO}=skipped(fresh_tolerance)" in out
    assert "容忍度" in out


# ---------------------------------------------------------------------------
# 3. A failure (fetch error AND refused write) -> `failed` with its reason/rule
# ---------------------------------------------------------------------------


def test_lv24_fetch_error_is_failed_and_rest_unaffected(tmp_path, monkeypatch, capsys):
    """One failing symbol => its lagging namespace is `failed`, the fresh one stays `skipped`.

    `fetch_errors` keeps its exact frozen value: `"<Provider>: <Type>: <msg>"` joined per
    attempted provider, one entry per failing symbol.
    """
    db_file = str(tmp_path / "cache.duckdb")
    seed = DuckDBCache(db_file)
    _lv24_store(seed, _lv24_up_to("2026-10-27"), MARKETDB)  # strict, lagging
    _lv24_store(seed, _lv24_up_to(LV24_END), FUYAO)         # fresh -- by the 2-day tolerance only

    _lv24_install(
        monkeypatch,
        _Lv24Provider(error=RuntimeError("bs boom")),
        _Lv24Provider(error=RuntimeError("em boom")),
    )

    summary = _lv24_run(db_file)

    trace = summary["equity_daily_patch"]
    assert trace["namespace_trace"] == {MARKETDB: "failed", FUYAO: "skipped"}
    assert trace["namespace_detail"][MARKETDB] == {
        "state": "failed",
        "reason": sync.FETCH_ERROR,
        "symbol": LV24_SYMBOL,
    }
    assert trace["namespace_detail"][FUYAO]["reason"] == sync.FRESH_TOLERANCE
    # FROZEN shape: the exact pre-LV24 string, no extra keys, no trace leakage.
    assert summary["fetch_errors"] == {
        LV24_SYMBOL: "BaoStockDataProvider: RuntimeError: bs boom; "
        "EastMoneyDataProvider: RuntimeError: em boom"
    }
    assert summary["steps"]["fetch_errors"] == 1
    assert summary["status"] == "completed"
    assert "[STAGE 1 NAMESPACE TRACE]" in capsys.readouterr().out


def test_lv24_refused_write_is_failed_with_its_rule(tmp_path, monkeypatch, capsys):
    """The CROSS-SOURCE refusal (`plan_history_write` -> `QfqWriteRefused`) is `failed` + rule.

    A re-based incoming window contradicts the stored closes, so NOTHING is written for that
    namespace: calling it `skipped` would say "we did not need to", which is a lie. The trace
    must survive the aggregate re-raise (`refusals.raise_if_any`), which is why `trace_out` is
    filled before it.
    """
    db_file = str(tmp_path / "cache.duckdb")
    seed = DuckDBCache(db_file)
    _lv24_store(seed, _lv24_up_to("2026-10-27"), MARKETDB)
    _lv24_store(seed, _lv24_up_to(LV24_END), FUYAO)

    refetched = _lv24_refetch()
    for col in ("Open", "High", "Low", "Close"):
        refetched[col] = refetched[col] * 0.75

    _lv24_install(monkeypatch, _Lv24Provider(refetched), _Lv24Provider(refetched))

    summary = _lv24_run(db_file)

    trace = summary["equity_daily_patch"]
    assert trace["namespace_trace"] == {MARKETDB: "failed", FUYAO: "skipped"}
    assert trace["namespace_detail"][MARKETDB] == {
        "state": "failed",
        "reason": sync.REFUSED_WRITE,
        "symbol": LV24_SYMBOL,
        "rule": sync.RULE_QFQ_REBASE_REFUSED,
    }
    # The refusal still aborts the pass the way it always did (Stage-1 degrades to a warn) --
    # LV24 changes no control flow, it only records what happened.
    assert summary["steps"]["patch_equity_daily_bars"].startswith("warn: ")
    assert "fetch_errors" not in summary

    out = capsys.readouterr().out
    assert "[STAGE 1 NAMESPACE TRACE]" in out
    assert f"{MARKETDB}=failed(refused_write)[qfq_rebase_refused]" in out


# ---------------------------------------------------------------------------
# 4. Worst-first aggregation (§4.2) is pinned, in BOTH symbol orders
# ---------------------------------------------------------------------------


def _lv24_two_symbols(tmp_path, monkeypatch, symbols):
    """A patched for both, B's fetch raising => MarketDB is `patched` AND `failed`."""
    db_file = str(tmp_path / "cache.duckdb")
    seed = DuckDBCache(db_file)
    for symbol in (LV24_SYMBOL, LV24_SYMBOL_B):
        _lv24_store(seed, _lv24_up_to("2026-10-27"), MARKETDB, symbol)
        _lv24_store(seed, _lv24_up_to(LV24_END), FUYAO, symbol)

    bs = _Lv24Provider(
        _lv24_refetch(), error=RuntimeError("bs boom"), error_symbols={LV24_SYMBOL_B}
    )
    em = _Lv24Provider(
        _lv24_refetch(), error=RuntimeError("em boom"), error_symbols={LV24_SYMBOL_B}
    )
    _lv24_install(monkeypatch, bs, em)
    return _lv24_run(db_file, symbols)


def test_lv24_worst_first_aggregation_is_order_independent(tmp_path, monkeypatch, capsys):
    """One `patched` + one `failed` is `failed` (freeze §4.2) -- whichever symbol comes first."""
    forward = _lv24_two_symbols(
        tmp_path / "forward", monkeypatch, [LV24_SYMBOL, LV24_SYMBOL_B]
    )
    backward = _lv24_two_symbols(
        tmp_path / "backward", monkeypatch, [LV24_SYMBOL_B, LV24_SYMBOL]
    )

    for summary in (forward, backward):
        trace = summary["equity_daily_patch"]
        # `patched` (A) must never mask `failed` (B), and `failed` must never be downgraded
        # by a later `patched`.
        assert trace["namespace_trace"][MARKETDB] == "failed"
        # ... and the detail keeps the outcome that CAUSED the worst state, not the last one.
        assert trace["namespace_detail"][MARKETDB] == {
            "state": "failed",
            "reason": sync.FETCH_ERROR,
            "symbol": LV24_SYMBOL_B,
        }
        assert summary["fetch_errors"] == {
            LV24_SYMBOL_B: "BaoStockDataProvider: RuntimeError: bs boom; "
            "EastMoneyDataProvider: RuntimeError: em boom"
        }
    # The rank table itself is pinned: `failed` > `patched` > `skipped`.
    assert sync._STATE_RANK == {"skipped": 0, "patched": 1, "failed": 2}


# ---------------------------------------------------------------------------
# 5. `fetch_errors` freeze (freeze doc §4.4) -- exact bytes + a source-level guard
#
# Existing coverage this relies on (do not delete): `test_failed_reported`,
# `test_refused_write_is_reported_as_failed` and
# `test_symbol_with_zero_bars_is_not_reported_as_a_fetch_error` in
# `pipeline/tests/test_patch_equity_daily_bars_contract.py`, which pin the primitive's own
# error mapping; the assertions below pin it as seen from the Stage-1 caller.
# ---------------------------------------------------------------------------


def test_lv24_fetch_errors_shape_is_frozen(tmp_path, monkeypatch, capsys):
    """Same input => the SAME output object as before LV24 (a plain `dict`, same literals).

    Nothing about the trace may enter this mapping: no state keys, no `failed` marker, no
    reason strings -- it stays `symbol -> "<provider>: <Type>: <msg>"` and nothing else.
    """
    db_file = str(tmp_path / "cache.duckdb")
    seed = DuckDBCache(db_file)
    _lv24_store(seed, _lv24_up_to("2026-10-27"), MARKETDB)
    _lv24_store(seed, _lv24_up_to(LV24_END), FUYAO)

    captured = _lv24_install(
        monkeypatch,
        _Lv24Provider(error=RuntimeError("bs boom")),
        _Lv24Provider(error=RuntimeError("em boom")),
        capture=True,
    )
    capsys.readouterr()
    _lv24_run(db_file)

    result = captured["result"]
    assert type(result) is dict, "the return type must stay a plain dict"
    assert json.dumps(result, sort_keys=True) == (
        '{"600660.SH": "BaoStockDataProvider: RuntimeError: bs boom; '
        'EastMoneyDataProvider: RuntimeError: em boom"}'
    )
    # No trace vocabulary leaks into the frozen mapping.
    for token in ("skipped", "patched", "failed", "fresh_tolerance", "qfq_rebase_refused"):
        assert token not in json.dumps(result)


def test_lv24_primitive_keeps_the_frozen_return_statement():
    """Source-level guard: the trace is an out-parameter, never a second return value."""
    path = os.path.join(_REPO_ROOT, "scripts", "sync_china_5m_and_indices.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()

    assert ") -> Dict[str, str]:" in source  # the frozen return annotation
    assert "return fetch_errors" in source
    assert 'fetch_errors[sym] = "; ".join(' in source
    # The trace lives in its own dict; it is never merged into `fetch_errors`.
    assert "fetch_errors.update(" not in source
    assert "trace_out" in source


def test_lv24_summary_line_is_absent_without_a_reason():
    """`summarise_namespace_trace` says NOTHING about a clean trace (no noise on green runs)."""
    assert _mod.summarise_namespace_trace(None) is None
    assert _mod.summarise_namespace_trace({}) is None
    assert (
        _mod.summarise_namespace_trace(
            {
                "namespace_trace": {MARKETDB: "patched", FUYAO: "skipped"},
                "namespace_detail": {
                    MARKETDB: {"state": "patched", "reason": None, "symbol": LV24_SYMBOL},
                    FUYAO: {
                        "state": "skipped",
                        "reason": sync.FRESH_STRICT,
                        "symbol": LV24_SYMBOL,
                    },
                },
            }
        )
        is None
    )
    line = _mod.summarise_namespace_trace(
        {
            "namespace_trace": {MARKETDB: "failed"},
            "namespace_detail": {
                MARKETDB: {
                    "state": "failed",
                    "reason": sync.REFUSED_WRITE,
                    "symbol": LV24_SYMBOL,
                    "rule": sync.RULE_NULL_VOLUME_REFUSED,
                }
            },
        }
    )
    assert line.startswith("[STAGE 1 NAMESPACE TRACE]")
    assert f"{MARKETDB}=failed(refused_write)[null_volume_refused]" in line


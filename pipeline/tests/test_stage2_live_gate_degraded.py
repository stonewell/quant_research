"""Regression suite for the 2026-11-12 defect A: the Stage-2 degraded-gate dead branch.

FORENSIC CONTEXT
----------------
`stage1_health_report.json` publishes the gate verdict INSIDE `portfolio_health`
(`tier_status`, `nav_gate_status`, `nav_breaker_evaluated`, `nav_source`, `nav_mtm`, ...).
Pre-fix, `scripts/run_live_strategy.py` read `tier_status` / `nav_gate_status` from the
report's TOP level -- where they did not exist -- so both were always `None`, and the only
remaining fail-closed leg was `circuit_breaker_tier`, which was read from the TOP level of
`account_state.json` first. That archive is carried over from the previous round
(`web/livetrading/execution_service.py`), so a stale-green `"NORMAL"` could pass the gate
while this round's report said `tier_status="UNKNOWN"`.

WHAT IS PINNED HERE
-------------------
1. nested-only `portfolio_health.tier_status="UNKNOWN"` + a stale `EVALUATED` account_state
   -> `LiveGateDegradedError` (the report wins on the WORSE answer);
2. `nav_breaker_evaluated=False` even with both `tier_status` strings saying `EVALUATED`
   -> still refused (a string-independent guard);
3. a placeholder `circuit_breaker_tier="NORMAL"` may not outvote a nested UNKNOWN verdict;
4. both archives healthy -> NOT refused (no false positive), tier resolves to NORMAL;
5. the operator acknowledgement (`LIVETRADING_ALLOW_DEGRADED_GATE=1`) still allows the run,
   loudly, and reports the same degraded verdict;
6. Stage-1 side: a degraded run now writes `dd_info["tier"]="UNKNOWN"` (agreeing with
   `account_state.circuit_breaker_tier`) and mirrors `"UNKNOWN"` into the report's
   top-level `circuit_breaker_tier`, so no consumer can read the placeholder "NORMAL".

100% offline: hand-written dicts + `tmp_path`; no market data, no network.
"""

import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    ALLOW_DEGRADED_GATE_ENV,
    LiveGateDegradedError,
    resolve_stage1_health_gate,
    assert_stage1_gate_usable,
)


def _load_script(module_name, relative_path):
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_stage1 = _load_script(
    "check_live_portfolio_health_defect_a", os.path.join("scripts", "check_live_portfolio_health.py")
)

#: A healthy, EVALUATED archive: what a stale carry-over looks like on a green day.
HEALTHY_ACCOUNT_STATE = {
    "tier_status": "EVALUATED",
    "nav_gate_status": "OK",
    "circuit_breaker_tier": "NORMAL",
    "nav_breaker_evaluated": True,
    "nav_source": "mtm",
    "nav_mtm": 200000.0,
}

#: The pre-fix report shape: verdict ONLY inside `portfolio_health`, nothing at the top level.
NESTED_UNKNOWN_REPORT = {
    "as_of_date": "2026-11-12",
    "gate_code": "CAUTION_UNVERIFIED_NAV",
    # NOTE: no `tier_status` / `nav_gate_status` / `circuit_breaker_tier` at this level.
    "portfolio_health": {
        "tier": "UNKNOWN",
        "tier_label": "⚪ UNKNOWN (NAV DEGRADED — 盯市净值不可用，熔断层级未判定；缺失 ≠ 未触发)",
        "tier_status": "UNKNOWN",
        "nav_gate_status": "DEGRADED_NAV_UNAVAILABLE",
        "nav_breaker_evaluated": False,
        "nav_source": "form_fallback_refused",
        "nav_mtm": None,
    },
}


# ---------------------------------------------------------------------------
# 1. nested UNKNOWN + stale EVALUATED account_state -> REFUSE
# ---------------------------------------------------------------------------


def test_nested_unknown_report_beats_a_stale_evaluated_account_state():
    with pytest.raises(LiveGateDegradedError) as excinfo:
        assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), NESTED_UNKNOWN_REPORT)

    message = str(excinfo.value)
    # (1) the WORSE source wins: the carried-over EVALUATED row cannot launder it
    assert "tier_status='UNKNOWN'" in message
    assert "nav_gate_status='DEGRADED_NAV_UNAVAILABLE'" in message
    assert "circuit_breaker_tier='UNKNOWN'" in message
    # (3) the diagnostic now reads the NESTED provenance instead of printing None
    assert "nav_source='form_fallback_refused'" in message
    assert "nav_mtm=None" in message
    assert "nav_breaker_evaluated=False" in message


def test_resolver_reports_the_worse_of_both_archives():
    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), NESTED_UNKNOWN_REPORT)
    assert info["degraded"] is True
    assert info["tier_status"] == "UNKNOWN"
    assert info["nav_gate_status"] == "DEGRADED_NAV_UNAVAILABLE"
    assert info["nav_breaker_evaluated"] is False
    assert info["circuit_breaker_tier"] == "UNKNOWN"
    assert any("tier_status=UNKNOWN" in reason for reason in info["degraded_reasons"])


# ---------------------------------------------------------------------------
# 2. string-independent guard: `nav_breaker_evaluated=false`
# ---------------------------------------------------------------------------


def test_nav_breaker_evaluated_false_is_refused_even_when_status_strings_say_evaluated():
    account_state = {
        "tier_status": "EVALUATED",
        "nav_gate_status": "OK",
        "circuit_breaker_tier": "NORMAL",
        "nav_breaker_evaluated": True,
    }
    report = {
        "portfolio_health": {
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "circuit_breaker_tier": "NORMAL",
            "nav_breaker_evaluated": False,   # the only honest signal in this combo
        }
    }

    with pytest.raises(LiveGateDegradedError) as excinfo:
        assert_stage1_gate_usable(account_state, report)

    assert "nav_breaker_evaluated=False" in str(excinfo.value)
    info = resolve_stage1_health_gate(account_state, report)
    assert info["degraded"] is True
    assert info["circuit_breaker_tier"] == "UNKNOWN"


def test_account_state_flag_false_alone_is_also_refused():
    account_state = dict(HEALTHY_ACCOUNT_STATE)
    account_state["nav_breaker_evaluated"] = False
    report = {
        "portfolio_health": {
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "nav_breaker_evaluated": True,
        }
    }
    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(account_state, report)


# ---------------------------------------------------------------------------
# 3. placeholder "NORMAL" may not outvote a nested UNKNOWN
# ---------------------------------------------------------------------------


def test_top_level_placeholder_normal_never_passes_for_an_evaluated_breaker():
    report = {
        "circuit_breaker_tier": "NORMAL",       # the placeholder Stage-1 used to publish
        "portfolio_health": {
            "tier": "UNKNOWN",
            "tier_status": "UNKNOWN",
            "nav_breaker_evaluated": False,
        },
    }
    account_state = {"circuit_breaker_tier": "NORMAL"}

    info = resolve_stage1_health_gate(account_state, report)
    assert info["circuit_breaker_tier"] == "UNKNOWN"
    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(account_state, report)


# ---------------------------------------------------------------------------
# 4. healthy report AND healthy account_state -> no false refusal
# ---------------------------------------------------------------------------


def test_both_archives_healthy_are_not_refused():
    report = {
        "portfolio_health": {
            "tier": "NORMAL",
            "tier_label": "🟢 NORMAL (Full Risk Budget)",
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "nav_breaker_evaluated": True,
            "nav_source": "mtm",
            "nav_mtm": 200000.0,
        }
    }
    info = assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), report)
    assert info["degraded"] is False
    assert info["degraded_reasons"] == []
    assert info["tier_status"] == "EVALUATED"
    assert info["circuit_breaker_tier"] == "NORMAL"
    assert info["nav_breaker_evaluated"] is True


def test_healthy_report_without_tier_status_keys_is_still_accepted():
    """Legacy archives have no `tier_status`; a green tier must not be invented into UNKNOWN."""
    info = assert_stage1_gate_usable({}, {"circuit_breaker_tier": "TIER_1_DAMPING"})
    assert info["degraded"] is False
    assert info["circuit_breaker_tier"] == "TIER_1_DAMPING"


# ---------------------------------------------------------------------------
# 5. explicit operator acknowledgement still proceeds (loudly)
# ---------------------------------------------------------------------------


def test_acknowledged_degraded_gate_proceeds_and_still_reports_the_verdict(monkeypatch, capsys):
    monkeypatch.setenv(ALLOW_DEGRADED_GATE_ENV, "1")
    info = assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), NESTED_UNKNOWN_REPORT)
    out = capsys.readouterr().out
    assert info["degraded"] is True
    assert info["circuit_breaker_tier"] == "UNKNOWN"
    assert "[WARN] (LV11)" in out
    assert "tier_status='UNKNOWN'" in out

    # explicit `acknowledged=True` works without the env var, and False refuses regardless
    info2 = assert_stage1_gate_usable(
        dict(HEALTHY_ACCOUNT_STATE), NESTED_UNKNOWN_REPORT, acknowledged=True
    )
    assert info2["degraded"] is True
    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(
            dict(HEALTHY_ACCOUNT_STATE), NESTED_UNKNOWN_REPORT, acknowledged=False
        )


# ---------------------------------------------------------------------------
# 6. Stage-1 side: the report/archive enum agrees on UNKNOWN and the label is preserved
# ---------------------------------------------------------------------------


def _dd_for(nav, peak_nav=203049.96, as_of_date="2026-11-12"):
    return _stage1.inspect_portfolio_drawdown(
        current_nav=nav,
        peak_nav=peak_nav,
        state={
            "as_of_date": None,
            "peak_nav": None,
            "tier1_consecutive_bars": 0,
            "tier3_consecutive_bars": 0,
            "freeze_remaining_bars": 0,
            "nav_history_10d": [],
        },
        params={},
        as_of_date=as_of_date,
    )


def _degraded_resolution():
    return {
        "nav_source": _stage1.NAV_SOURCE_FORM_FALLBACK_REFUSED,
        "nav_mtm": None,
        "nav_form": 199935.46,
        "nav_deviation_pct": None,
        "nav_deviation_exceeded": None,
        "nav_deviation_threshold_pct": 0.05,
        "nav_breakdown_status": _stage1.NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN,
        "nav_breakdown_as_of": None,
        "nav_gate_status": _stage1.NAV_GATE_STATUS_DEGRADED,
        "nav_degradation_reason": "nav_breakdown.json missing",
        "nav_gate_nav": None,
        "nav_gate_nav_used": 199935.46,
        "nav_timebase_status": None,
    }


def test_stage1_degraded_run_writes_unknown_tier_and_preserves_the_label():
    dd_info = _stage1.attach_nav_provenance(_dd_for(199935.46), _degraded_resolution())

    # The enum now AGREES with `account_state.circuit_breaker_tier` (UNKNOWN), instead of
    # the contradictory placeholder "NORMAL".
    assert dd_info["tier"] == "UNKNOWN"
    assert dd_info["tier_status"] == "UNKNOWN"
    assert dd_info["nav_breaker_evaluated"] is False
    # ... and the human label still says exactly what is unknown.
    assert "UNKNOWN" in dd_info["tier_label"]
    assert "Full Risk Budget" not in dd_info["tier_label"]


def test_stage1_report_mirrors_unknown_at_top_level_on_a_degraded_run(tmp_path):
    dd_info = _stage1.attach_nav_provenance(_dd_for(199935.46), _degraded_resolution())
    _stage1.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-12",
        strategy_info={"strategy_name": "unit-test", "strategy_version": "0.0.0"},
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
        vol_info={"realized_vol_21d": 0.18, "target_vol": 0.20, "vol_scaling_factor": 1.0, "vol_elevated": False},
        gate_code="CAUTION_UNVERIFIED_NAV",
        gate_headline="⚪ UNKNOWN",
        actions=[],
    )
    with open(os.path.join(str(tmp_path), _stage1.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8") as f:
        report = json.load(f)

    assert report["portfolio_health"]["tier"] == "UNKNOWN"
    # (2026-11-12 defect A) top-level mirror exists and is NOT the placeholder "NORMAL"
    assert report["circuit_breaker_tier"] == "UNKNOWN"
    assert report["circuit_breaker_tier"] != "NORMAL"


def test_stage1_report_top_level_mirror_is_the_measured_tier_on_a_healthy_run(tmp_path):
    dd_info = _stage1.attach_nav_provenance(
        _dd_for(200000.0),
        {
            "nav_source": _stage1.NAV_SOURCE_MTM,
            "nav_mtm": 200000.0,
            "nav_form": 200000.0,
            "nav_deviation_pct": 0.0,
            "nav_deviation_exceeded": False,
            "nav_deviation_threshold_pct": 0.05,
            "nav_breakdown_status": None,
            "nav_breakdown_as_of": "2026-11-12",
            "nav_gate_status": None,
            "nav_degradation_reason": None,
            "nav_gate_nav": 200000.0,
            "nav_gate_nav_used": 200000.0,
            "nav_timebase_status": None,
        },
    )
    _stage1.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-12",
        strategy_info={"strategy_name": "unit-test", "strategy_version": "0.0.0"},
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
        vol_info={"realized_vol_21d": 0.18, "target_vol": 0.20, "vol_scaling_factor": 1.0, "vol_elevated": False},
        gate_code="GO",
        gate_headline="🟢 GO",
        actions=[],
    )
    with open(os.path.join(str(tmp_path), _stage1.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8") as f:
        report = json.load(f)
    assert report["circuit_breaker_tier"] == report["portfolio_health"]["tier"]
    assert report["circuit_breaker_tier"] == "NORMAL"

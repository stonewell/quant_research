"""Offline regression suite for the LV1-TIMEBASE gate-NAV check
(`scripts/check_live_portfolio_health.py`).

DEFECT UNDER TEST (2026-11-10 / 11-11 live runs, reproduced as literals below).

`nav_breakdown.json::nav_effective` is `equity + cash`, but in the live path the two legs
are sourced at DIFFERENT times:

* the EQUITY leg is marked at the snapshot's `as_of` (today's close, after the LV11
  held-book bar refresh), while
* the CASH leg is the PREVIOUS session's settled balance -- the LV11 pre-staged snapshot
  gets it from `_archived_cash_for(...)` (the last archived round's `summary.json::cash`)
  and records that provenance as a `cash_from_archive:` line in `nav_warnings`.

Live evidence:

    11-10: nav_effective 208,067.60 = equity 182,471.00 + cash 25,596.60 (11-09's cash)
           same-instant NAV was 196,852.32 (= 182,471.00 + 14,381.32) -> +5.14% phantom GAIN
    11-11: nav_effective 101,093.32 = equity 86,712.00 + cash 14,381.32 (11-10's cash)
           same-instant NAV was 196,397.11 (= 86,712.00 + 109,685.11) -> -51.59% phantom
           LOSS -> TIER_3_HALT / linear_equity_scale 0.0 / freeze 21 bars -> full
           liquidation, while the book had moved -0.23%.

The gate must therefore REFUSE a `nav_effective` whose legs span two instants: tier
UNKNOWN (Stage 2 then refuses to issue a ticket) -- never a `nav_form` fallback (the frozen
operator value LV1 already banned from the gate arithmetic) and never a "repair" from legs
that do not belong to one instant.

100% OFFLINE: every fixture is a hand-written `nav_breakdown.json` / dict under `tmp_path`.
No network, no market data, no read of `data/runs/**` or `data/cache.duckdb`.

Reverse-mutation notice: delete the `assess_nav_timebase` call in `resolve_gate_nav` and
both `*_refused_*` tests go RED (the gate would accept 101,093.32 / 208,067.60 again);
drop the `cash_from_archive:` detector and `test_archived_cash_leg_...` goes RED while the
legs-tolerance test stays green (they are deliberately independent detectors).
"""

import importlib.util
import json
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "check_live_portfolio_health_timebase",
    os.path.join("scripts", "check_live_portfolio_health.py"),
)

# --- archived live facts (literals on purpose; never read from `data/runs/**`) -----------
PEAK_NAV = 208478.46          # HWM carried into 11-10 / 11-11
FORM_NAV = 196397.11          # the operator form value on 11-11 (same-instant truth)

EQUITY_1110 = 182471.00       # today's mark
CASH_1110_STALE = 25596.60    # = 11-09's settled cash (archived)
CASH_1110_TRUE = 14381.32     # 11-10's same-instant cash
NAV_1110_MIXED = 208067.60    # what the gate was fed (equity + stale cash)

EQUITY_1111 = 86712.00
CASH_1111_STALE = 14381.32    # = 11-10's settled cash (archived)
CASH_1111_TRUE = 109685.11    # 11-11's same-instant cash
NAV_1111_MIXED = 101093.32    # what the gate was fed (equity + stale cash)




def _archive_warning(round_date, cash=CASH_1111_STALE):
    """The producer's provenance line, with the archived ROUND date in the path.

    That date is what distinguishes "previous ROUND of the same session" (same instant)
    from "previous SESSION" (the defect): `_archive_cash_is_same_session` compares it to
    the snapshot's `as_of`.
    """
    return (
        f"cash_from_archive: cash={cash:.2f} 取自上一轮 summary.json "
        f"(path=/runs/strat/univ/{round_date}/summary.json)"
    )


def _write_breakdown(tmp_path, payload, name="nav_breakdown.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _args(breakdown_file, form_nav=FORM_NAV):
    return types.SimpleNamespace(
        portfolio_value=form_nav,
        nav_breakdown_file=breakdown_file,
        nav_cash_leg=_mod.NAV_CASH_LEG_AUTO,
    )


def _flat_state():
    """Zeroed counters + empty 10d history (see `test_stage1_nav_source._flat_state`)."""
    return {
        "as_of_date": None,
        "peak_nav": None,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [],
    }


def _dd_for(nav, as_of_date="2026-11-11"):
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

_STRATEGY_INFO = {
    "strategy_name": "unit-test",
    "template_name": "unit-test",
    "strategy_version": "0.0.0",
}


def _render(out_dir, dd_info):
    """Write the real Stage-1 reports and return (report_dict, markdown_text)."""
    _mod.generate_reports(
        output_dir=str(out_dir),
        as_of_date="2026-11-11",
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
# Case A -- the live defect: cash leg from the PREVIOUS round's archive
# ---------------------------------------------------------------------------


def test_archived_cash_leg_is_refused_and_can_never_trigger_tier3(tmp_path, capsys):
    """11-11, reproduced: the mixed NAV is refused instead of halting the account.

    First the hazard is pinned down -- gating on 101,093.32 against a 208,478.46 HWM really
    does produce TIER_3_HALT + a 21-bar freeze. Then the same snapshot is fed through the
    resolver and must be refused, so that number can never reach the breaker.
    """
    hazard = _dd_for(NAV_1111_MIXED)
    assert hazard["tier"] == "TIER_3_HALT"
    assert hazard["freeze_remaining_bars"] == 21
    assert hazard["linear_equity_scale"] == pytest.approx(0.0)
    assert hazard["drawdown_pct"] == pytest.approx(NAV_1111_MIXED / PEAK_NAV - 1.0, abs=1e-9)
    assert hazard["drawdown_pct"] < -0.5  # -51.5%

    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-11",
            "nav_effective": NAV_1111_MIXED,
            "nav_computed": NAV_1111_MIXED,
            "nav_source": "computed",
            "equity": EQUITY_1111,
            "cash": CASH_1111_STALE,
            "cash_source": "explicit",
            "nav_warnings": [_archive_warning("2026-11-10")],
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    printed = capsys.readouterr().out

    # The refusal is LOUD and carries the frozen, grep-able prefix.
    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX in printed
    assert "cash_from_archive" in printed

    # ... and machine-readable: no gate NAV, no deviation, explicit provenance.
    assert resolution["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert resolution["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
    assert resolution["nav_gate_status"] == _mod.NAV_GATE_STATUS_DEGRADED
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_mtm"] is None
    assert resolution["nav_deviation_pct"] is None
    assert resolution["nav_deviation_exceeded"] is None
    assert resolution["nav_rejected_nav_effective"] == pytest.approx(NAV_1111_MIXED)
    assert resolution["nav_equity"] == pytest.approx(EQUITY_1111)
    assert resolution["nav_cash"] == pytest.approx(CASH_1111_STALE)
    assert resolution["nav_degradation_reason"].startswith(_mod.NAV_TIMEBASE_MISMATCH_PREFIX)

    # Through the REAL wiring (`main()` uses `nav_gate_nav_used` as a display placeholder and
    # `attach_nav_provenance` relabels the verdict): tier is UNKNOWN, never TIER_3.
    dd_info = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav_used"]), resolution)
    assert dd_info["tier_status"] == "UNKNOWN"
    assert dd_info["nav_breaker_evaluated"] is False
    assert dd_info["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
    assert dd_info["nav_rejected_nav_effective"] == pytest.approx(NAV_1111_MIXED)

    # FAIL-CLOSED ARCHIVE: Stage 2 only reads `circuit_breaker_tier`, so that enum must be
    # UNKNOWN -- and the tier-3 counters / drawdown / scale must NOT be written back.
    updated = _mod.build_account_state_update(
        {},
        {"nav_history_10d": []},
        dd_info,
        as_of_date="2026-11-11",
        gate_code="GO",
    )
    assert updated["circuit_breaker_tier"] == "UNKNOWN"
    assert updated["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
    assert "freeze_remaining_bars" not in updated
    assert "drawdown_pct" not in updated

    report, md = _render(tmp_path / "out", dd_info)
    health = report["portfolio_health"]
    assert health["nav_source"] == "timebase_mismatch_refused"
    assert health["nav_mtm"] is None
    assert health["tier_status"] == "UNKNOWN"
    assert health["nav_equity"] == pytest.approx(EQUITY_1111)
    assert "TIER_3_HALT" not in health["tier"]
    assert "UNKNOWN" in md
    # The mixed number must not be presented as the measured gate NAV.
    assert "101,093.32" not in md


def test_archived_cash_leg_on_11_10_would_have_been_refused_too(tmp_path, capsys):
    """The 11-10 phantom GAIN (+5.14%) is refused as well: both directions, same rule.

    A reverse-signed cash move masks real risk instead of inventing a fake loss, so the
    check must not be one-sided (e.g. only "NAV too low").
    """
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-10",
            "nav_effective": NAV_1110_MIXED,
            "equity": EQUITY_1110,
            "cash": CASH_1110_STALE,
            "cash_source": "explicit",
            "nav_warnings": [_archive_warning("2026-11-09", cash=CASH_1110_STALE)],
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown, form_nav=196852.32))
    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX in capsys.readouterr().out
    assert resolution["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_rejected_nav_effective"] == pytest.approx(NAV_1110_MIXED)


def test_archive_cash_from_the_same_session_is_not_refused(tmp_path, capsys):
    """A same-day re-run's pre-staged cash is TODAY's residual -> no false positive.

    `cash_from_archive:` only says "an earlier ROUND"; when that round is the SAME session
    (`.../<as_of>/summary.json`) the cash leg and the equity leg do share one instant, so
    refusing it would block every intraday re-run. The archived round date, embedded in the
    provenance text, is what separates the two cases.
    """
    same_day_nav = EQUITY_1111 + CASH_1111_TRUE  # 196,397.11, a same-instant pair
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-11",
            "nav_effective": same_day_nav,
            "equity": EQUITY_1111,
            "cash": CASH_1111_TRUE,
            "cash_source": "explicit",
            "nav_warnings": [_archive_warning("2026-11-11", cash=CASH_1111_TRUE)],
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown, form_nav=same_day_nav))
    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX not in capsys.readouterr().out
    assert resolution["nav_source"] == _mod.NAV_SOURCE_MTM
    assert resolution["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_OK
    assert resolution["nav_mtm"] == pytest.approx(same_day_nav)


# ---------------------------------------------------------------------------
# Case B -- the snapshot contradicts itself (legs tolerance)
# ---------------------------------------------------------------------------

def test_legs_mismatch_is_refused_without_any_archive_marker(tmp_path, capsys):
    """No provenance line, but `nav_effective != equity + cash` -> still refused.

    11-10's mixed NAV against the SAME-instant cash field: |208,067.60 - 196,852.32| =
    11,215.28, far outside the 1.00 tolerance. Independent detector from Case A.
    """
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-10",
            "nav_effective": NAV_1110_MIXED,
            "equity": EQUITY_1110,
            "cash": CASH_1110_TRUE,
            "cash_source": "explicit",
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown, form_nav=196852.32))
    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX in capsys.readouterr().out
    assert resolution["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_LEGS_MISMATCH
    assert resolution["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_legs_nav"] == pytest.approx(196852.32)
    assert resolution["nav_legs_delta"] == pytest.approx(NAV_1110_MIXED - 196852.32)


# ---------------------------------------------------------------------------
# Case C -- a same-instant snapshot is untouched (no new false positives)
# ---------------------------------------------------------------------------


def test_same_instant_snapshot_keeps_mtm_and_drawdown(tmp_path, capsys):
    """`nav_effective == equity + cash` -> accepted, `nav_mtm`/`drawdown_pct` unchanged."""
    same_instant_nav = EQUITY_1110 + CASH_1110_TRUE  # 196,852.32
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-10",
            "nav_effective": same_instant_nav,
            "nav_computed": same_instant_nav,
            "equity": EQUITY_1110,
            "cash": CASH_1110_TRUE,
            "cash_source": "explicit",
            "nav_warnings": [],
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown, form_nav=same_instant_nav))
    printed = capsys.readouterr().out
    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX not in printed
    assert "[WARN] (LV1)" not in printed  # no deviation, no degradation

    assert resolution["nav_source"] == _mod.NAV_SOURCE_MTM
    assert resolution["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_OK
    assert resolution["nav_mtm"] == pytest.approx(same_instant_nav)
    assert resolution["nav_gate_nav"] == pytest.approx(same_instant_nav)
    assert resolution["nav_deviation_pct"] == pytest.approx(0.0)

    dd_info = _mod.attach_nav_provenance(_dd_for(resolution["nav_gate_nav"]), resolution)
    assert dd_info["tier_status"] == "EVALUATED"
    # Identical to the pre-change maths: the tier math is untouched.
    assert dd_info["drawdown_pct"] == pytest.approx(same_instant_nav / PEAK_NAV - 1.0)
    assert dd_info["tier"] == "NORMAL"  # -5.58%, below the unchanged 10% Tier-1 band
    # Clean run adds NO timebase key to the published dict (frozen LV1 key set).
    assert "nav_timebase_status" not in dd_info
    assert "nav_rejected_nav_effective" not in dd_info


def test_tolerance_absorbs_rounding_but_not_a_real_leg_gap(tmp_path):
    """1.00 money unit is rounding; 1.01 is a measured inconsistency."""
    at_tol = _mod.resolve_gate_nav(
        {
            "as_of": "2026-11-10",
            "nav_effective": 196852.32,
            "equity": 182471.00,
            "cash": 14381.32 + 0.99,
            "cash_source": "explicit",
        },
        196852.32,
    )
    assert at_tol["nav_source"] == _mod.NAV_SOURCE_MTM

    over_tol = _mod.resolve_gate_nav(
        {
            "as_of": "2026-11-10",
            "nav_effective": 196852.32,
            "equity": 182471.00,
            "cash": 14381.32 + 1.01,
            "cash_source": "explicit",
        },
        196852.32,
    )
    assert over_tol["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert over_tol["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_LEGS_MISMATCH


# ---------------------------------------------------------------------------
# Case C2 -- STRUCTURED provenance (`cash_as_of`), the producer's own reason
# ---------------------------------------------------------------------------


def test_structured_cash_as_of_is_refused_without_any_text_marker(tmp_path, capsys):
    """`cash_as_of != as_of` alone is decisive -- no log-line parsing involved.

    This is the contract the valuation layer writes (`_write_nav_breakdown`): the cash leg
    carries the session it belongs to. The text marker (`cash_from_archive:`) stays as a
    fallback for older archives only.
    """
    same_instant_arithmetic = EQUITY_1111 + CASH_1111_STALE  # self-consistent, still mixed
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-11",
            "nav_effective": same_instant_arithmetic,
            "equity": EQUITY_1111,
            "cash": CASH_1111_STALE,
            "cash_source": "explicit",
            "cash_as_of": "2026-11-10",  # <- the structured timebase
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    printed = capsys.readouterr().out

    assert _mod.NAV_TIMEBASE_MISMATCH_PREFIX in printed
    assert resolution["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert resolution["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_cash_as_of"] == "2026-11-10"
    assert "cash_as_of" in resolution["nav_timebase_detail"]


def test_producer_withheld_nav_is_surfaced_with_its_own_reason(tmp_path, capsys):
    """The staged payload's `nav_effective=None` + reason reaches the report verbatim.

    The producer refuses to publish a cross-timebase NAV (LV1-TIMEBASE); Stage 1 must not
    re-describe that as a generic "missing snapshot" -- the operator needs to know the book
    was valued but the CASH was from another session.
    """
    producer_reason = (
        "(LV1-TIMEBASE) 预置快照拒绝跨时点拼装净值：权益腿已按 as_of=2026-11-11 盯市，"
        "现金腿却来自 as_of=2026-11-10 的归档残差（cash=25596.6）"
    )
    breakdown = _write_breakdown(
        tmp_path,
        {
            "as_of": "2026-11-11",
            "nav_effective": None,
            "nav_computed": None,
            "nav_source": "staged_cash_timebase_mismatch",
            "equity": EQUITY_1111,
            "cash": CASH_1111_STALE,
            "cash_source": "explicit",
            "cash_as_of": "2026-11-10",
            "nav_degradation_reason": producer_reason,
            "nav_warnings": [
                "cash_timebase_mismatch: cash_as_of=2026-11-10 != as_of=2026-11-11"
            ],
        },
    )
    resolution = _mod.resolve_gate_nav_context(_args(breakdown))
    capsys.readouterr()

    assert resolution["nav_source"] == _mod.NAV_SOURCE_FORM_FALLBACK_REFUSED
    assert resolution["nav_gate_nav"] is None
    assert resolution["nav_breakdown_status"] == _mod.NAV_BREAKDOWN_STATUS_EFFECTIVE_MISSING
    assert "(LV1-TIMEBASE)" in resolution["nav_degradation_reason"]
    assert "生产端声明" in resolution["nav_degradation_reason"]

    dd_info = _mod.attach_nav_provenance(_dd_for(FORM_NAV), resolution)
    assert dd_info["tier_status"] == "UNKNOWN"
    assert dd_info["nav_breaker_evaluated"] is False


# ---------------------------------------------------------------------------
# Case D -- the explicit (a)/(b) switch, and the cases that must NOT be accused
# ---------------------------------------------------------------------------


def test_nav_cash_leg_flag_replaces_the_inference(tmp_path):
    """`--nav-cash-leg` is the explicit (a)/(b) declaration the contract asks for."""
    payload = {
        "as_of": "2026-11-11",
        "nav_effective": NAV_1111_MIXED,
        "equity": EQUITY_1111,
        "cash": CASH_1111_STALE,
        "cash_source": "explicit",
        "nav_warnings": [_archive_warning("2026-11-10")],
    }

    # (b) The caller re-entered a same-instant broker balance: the archive marker is
    # history, the legs are consistent -> usable.
    same_instant = _mod.resolve_gate_nav(
        payload, FORM_NAV, cash_leg_basis=_mod.NAV_CASH_LEG_SAME_INSTANT
    )
    assert same_instant["nav_source"] == _mod.NAV_SOURCE_MTM
    assert same_instant["nav_mtm"] == pytest.approx(NAV_1111_MIXED)
    assert same_instant["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_OK

    # (a) The caller asserts the cash leg is the previous close's settled balance: a mixed
    # NAV is by definition not same-instant -> refused even for a self-consistent file.
    prior_close = _mod.resolve_gate_nav(
        {
            "as_of": "2026-11-11",
            "nav_effective": EQUITY_1111 + CASH_1111_STALE,
            "equity": EQUITY_1111,
            "cash": CASH_1111_STALE,
            "cash_source": "explicit",
        },
        FORM_NAV,
        cash_leg_basis=_mod.NAV_CASH_LEG_PRIOR_CLOSE,
    )
    assert prior_close["nav_source"] == _mod.NAV_SOURCE_TIMEBASE_REFUSED
    assert prior_close["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
    assert prior_close["nav_cash_leg_basis"] == _mod.NAV_CASH_LEG_PRIOR_CLOSE


def test_override_and_legless_snapshots_are_not_refused():
    """Documented exceptions: an operator override, and a snapshot without legs.

    `nav_source="override"` is a signed human assertion (the engine's legs are only the
    diff reference), and a snapshot that carries no `equity`/`cash` cannot be accused of
    anything -- both must keep the pre-change behaviour.
    """
    override = _mod.resolve_gate_nav(
        {
            "as_of": "2026-11-11",
            "nav_effective": 210000.0,
            "equity": 190000.0,
            "cash": 5000.0,
            "cash_source": "explicit",
            "nav_source": "override",
        },
        196397.11,
    )
    assert override["nav_source"] == _mod.NAV_SOURCE_MTM
    assert override["nav_mtm"] == pytest.approx(210000.0)
    assert override["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_UNVERIFIABLE

    legless = _mod.resolve_gate_nav({"as_of": "2026-11-11", "nav_effective": 196397.11}, 196397.11)
    assert legless["nav_source"] == _mod.NAV_SOURCE_MTM
    assert legless["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_UNVERIFIABLE

    # `cash_source="missing"` means the engine wrote an explicit 0.0 UNDERSTATEMENT; that is
    # not a same-instant NAV, so the legs cannot be used as evidence either way.
    missing_cash = _mod.resolve_gate_nav(
        {
            "as_of": "2026-11-11",
            "nav_effective": 86712.0,
            "equity": 86712.0,
            "cash": 0.0,
            "cash_source": "missing",
        },
        196397.11,
    )
    assert missing_cash["nav_source"] == _mod.NAV_SOURCE_MTM
    assert missing_cash["nav_timebase_status"] == _mod.NAV_TIMEBASE_STATUS_UNVERIFIABLE


def test_parse_args_exposes_the_cash_leg_switch(tmp_path):
    """The deployed CLI must carry the explicit switch, defaulting to `auto`."""
    parsed = _mod.parse_args(["--output-dir", str(tmp_path), "--data-provider", "synthetic"])
    assert parsed.nav_cash_leg == _mod.NAV_CASH_LEG_AUTO

    pinned = _mod.parse_args(
        [
            "--output-dir",
            str(tmp_path),
            "--data-provider",
            "synthetic",
            "--nav-cash-leg",
            _mod.NAV_CASH_LEG_PRIOR_CLOSE,
        ]
    )
    assert pinned.nav_cash_leg == _mod.NAV_CASH_LEG_PRIOR_CLOSE

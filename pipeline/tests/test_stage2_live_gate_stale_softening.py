"""Regression suite for the four residuals found by the gate re-review (2026-11-12).

FORENSIC CONTEXT
----------------
`pipeline/tests/test_stage2_live_gate_degraded.py` pinned defect A: the gate verdict lives
INSIDE `stage1_health_report.json::portfolio_health`, and both archives are read with the
WORSE answer winning. A second, read-only review found four ways a STALE archive could still
soften (or bypass) this round's verdict:

1. MISSING != EVALUATED was only implemented for EXPLICIT signals. An `account_state.json`
   carrying nothing but the placeholder `circuit_breaker_tier="NORMAL"` (or a loaded report
   with no gate block at all) resolved to `["NORMAL"]` and PASSED, because "no key" is not
   the string "UNKNOWN".
2. `nav_breaker_evaluated` was tested with `is False`, so the JSON-ish spellings `"false"` /
   `"0"` / `0` (hand-edited or re-serialised archives) read as "no opinion" and passed.
3. `gate_code` and `linear_equity_scale` were read "account_state first", so a stale
   `gate_code="CAUTION_RECOVERING"` marked a Tier-3-halted account as recovering -- skipping
   the 100% cash sweep -- and a stale `linear_equity_scale=1.0` cancelled this round's 0.5.
4. `web/livetrading/execution_service.py::_get_subprocess_env` seeds every child env with
   `dict(os.environ)`, so `LIVETRADING_ALLOW_DEGRADED_GATE` preset in the web service process
   is inherited by EVERY run it launches, without any per-run human acknowledgement.

WHAT IS PINNED HERE
-------------------
a. placeholder `NORMAL` + no status keys + non-replay -> REFUSED; the same fixture with
   `run_mode="replay"` -> allowed (the documented backward-compatible exemption);
b. `nav_breaker_evaluated` spelled `"false"` / `"0"` / `0` -> all REFUSED;
c. stale `gate_code="CAUTION_RECOVERING"` + report `circuit_breaker_tier="TIER_3_HALT"`
   -> `is_recovering` is False and the ticket is a 100% cash sweep (end to end through
   `main()`), because a stale archive may never soften the latest report;
d. `linear_equity_scale` account_state `1.0` + report `0.5` -> resolved `0.5` (the min);
e. an inherited `LIVETRADING_ALLOW_DEGRADED_GATE` (degraded gate, `nav_source != "override"`)
   is reported as inherited and lands as `gate_degraded_ack_env_inherited=true` in
   `summary.json`; a genuine `nav_source="override"` is NOT flagged;
f. healthy archives are still not refused (no false positives).

100% offline: hand-written dicts + synthetic data + `tmp_path`; no market data, no network.
"""

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
    assert_stage1_gate_usable,
    degraded_gate_ack_env_inherited,
    resolve_stage1_health_gate,
)

#: A 2024 session: an explicit `--as-of-date` earlier than the market clock is `run_mode=replay`
#: (see `resolve_run_mode`), which the end-to-end cases below rely on for the no-gate-block
#: exemption. The halt/scale semantics under test are identical in both run modes.
_AS_OF = "2024-05-10"

HEALTHY_ACCOUNT_STATE = {
    "tier_status": "EVALUATED",
    "nav_gate_status": "OK",
    "circuit_breaker_tier": "NORMAL",
    "nav_breaker_evaluated": True,
    "nav_source": "mtm",
    "nav_mtm": 200000.0,
}

HEALTHY_REPORT = {
    "as_of_date": _AS_OF,
    "portfolio_health": {
        "tier": "NORMAL",
        "tier_label": "🟢 NORMAL (Full Risk Budget)",
        "tier_status": "EVALUATED",
        "nav_gate_status": "OK",
        "nav_breaker_evaluated": True,
        "nav_source": "mtm",
        "nav_mtm": 200000.0,
    },
}

DEGRADED_REPORT = {
    "as_of_date": _AS_OF,
    "gate_code": "CAUTION_UNVERIFIED_NAV",
    "portfolio_health": {
        "tier": "UNKNOWN",
        "tier_status": "UNKNOWN",
        "nav_gate_status": "DEGRADED_NAV_UNAVAILABLE",
        "nav_breaker_evaluated": False,
        "nav_source": "form_fallback_refused",
        "nav_mtm": None,
    },
}


# ---------------------------------------------------------------------------
# 1. MISSING != EVALUATED: a placeholder-only archive is NOT an evaluated breaker
# ---------------------------------------------------------------------------


def test_placeholder_normal_without_status_keys_is_refused_outside_replay():
    """`account_state` = only `circuit_breaker_tier="NORMAL"`, no report at all -> REFUSE."""
    placeholder = {"circuit_breaker_tier": "NORMAL"}

    with pytest.raises(LiveGateDegradedError) as excinfo:
        assert_stage1_gate_usable(placeholder, {})

    message = str(excinfo.value)
    assert "tier_status='UNKNOWN'" in message
    assert "circuit_breaker_tier='UNKNOWN'" in message
    info = resolve_stage1_health_gate(placeholder, {})
    assert info["degraded"] is True
    assert info["circuit_breaker_tier"] == "UNKNOWN"
    assert info["unevaluated_source"] is not None


def test_loaded_report_without_any_gate_block_is_refused_outside_replay():
    """A report was loaded but says nothing about the gate -> UNKNOWN, not green."""
    report = {"as_of_date": _AS_OF, "market_macro": {"thrust": 0.70, "breadth": 0.50}}

    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), report)

    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), report)
    assert info["degraded"] is True
    assert info["tier_status"] == "UNKNOWN"
    assert info["circuit_breaker_tier"] == "UNKNOWN"
    assert "report_has_no_gate_block" in " ".join(info["degraded_reasons"])


def test_placeholder_only_archive_is_allowed_in_replay_mode():
    """The documented exemption: a historical replay has no live gate to evaluate."""
    placeholder = {"circuit_breaker_tier": "NORMAL"}
    report = {"as_of_date": _AS_OF}

    info = assert_stage1_gate_usable(placeholder, report, run_mode="replay")
    assert info["degraded"] is False
    assert info["circuit_breaker_tier"] == "NORMAL"
    assert info["run_mode"] == "replay"

    # ... while an EXPLICIT degraded verdict is never exempted by the replay flag.
    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(
            dict(HEALTHY_ACCOUNT_STATE), DEGRADED_REPORT, run_mode="replay"
        )


def test_report_gate_block_outvotes_a_placeholder_account_state():
    """A placeholder archive is harmless once THIS round's report evaluates the gate."""
    info = assert_stage1_gate_usable(
        {"circuit_breaker_tier": "NORMAL"}, dict(HEALTHY_REPORT)
    )
    assert info["degraded"] is False
    assert info["circuit_breaker_tier"] == "NORMAL"


# ---------------------------------------------------------------------------
# 2. `nav_breaker_evaluated` falsy spellings are all refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("false_spelling", ["false", "0", 0, False, "FALSE", " 0 "])
def test_nav_breaker_evaluated_false_spellings_are_all_refused(false_spelling):
    account_state = dict(HEALTHY_ACCOUNT_STATE)
    account_state["nav_breaker_evaluated"] = false_spelling

    with pytest.raises(LiveGateDegradedError) as excinfo:
        assert_stage1_gate_usable(account_state, dict(HEALTHY_REPORT))

    assert "nav_breaker_evaluated=False" in str(excinfo.value)
    info = resolve_stage1_health_gate(account_state, dict(HEALTHY_REPORT))
    assert info["nav_breaker_evaluated"] is False
    assert info["circuit_breaker_tier"] == "UNKNOWN"


@pytest.mark.parametrize("false_spelling", ["false", "0", 0])
def test_nav_breaker_evaluated_false_spelling_in_the_report_is_also_refused(false_spelling):
    report = json.loads(json.dumps(HEALTHY_REPORT))
    report["portfolio_health"]["nav_breaker_evaluated"] = false_spelling

    with pytest.raises(LiveGateDegradedError):
        assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), report)


def test_nav_breaker_evaluated_true_spellings_do_not_change_a_healthy_verdict():
    for true_spelling in ("true", "1", 1, True):
        account_state = dict(HEALTHY_ACCOUNT_STATE)
        account_state["nav_breaker_evaluated"] = true_spelling
        info = assert_stage1_gate_usable(account_state, dict(HEALTHY_REPORT))
        assert info["degraded"] is False
        assert info["nav_breaker_evaluated"] is True


# ---------------------------------------------------------------------------
# 3. A stale `gate_code` may not soften the report's TIER_3_HALT
# ---------------------------------------------------------------------------


def _stale_recovering_state():
    return {
        "current_nav": 100000.0,
        "peak_nav": 120000.0,
        "tier_status": "EVALUATED",
        "nav_gate_status": "OK",
        "nav_breaker_evaluated": True,
        # STALE carry-over: the account already recovered ...
        "circuit_breaker_tier": "NORMAL",
        "gate_code": "CAUTION_RECOVERING",
        "linear_equity_scale": 1.0,
        "freeze_remaining_bars": 0,
    }


def _halted_report(linear_equity_scale=None):
    report = {
        "as_of_date": _AS_OF,
        "gate_code": "TIER_3_HALT",
        "portfolio_health": {
            "tier": "TIER_3_HALT",
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "nav_breaker_evaluated": True,
            "circuit_breaker_tier": "TIER_3_HALT",
            "nav_source": "mtm",
            "nav_mtm": 100000.0,
        },
    }
    if linear_equity_scale is not None:
        report["linear_equity_scale"] = linear_equity_scale
    return report


def test_stale_caution_recovering_cannot_mark_a_tier3_halt_as_recovering():
    """... but THIS round's report says TIER_3_HALT -> the halt verdict wins."""
    info = resolve_stage1_health_gate(_stale_recovering_state(), _halted_report())
    assert info["circuit_breaker_tier"] == "TIER_3_HALT"
    assert info["gate_code"] == "TIER_3_HALT"     # max severity, not "account_state first"
    assert info["is_recovering"] is False


def test_active_freeze_also_vetoes_the_recovery_branch():
    state = _stale_recovering_state()
    state["circuit_breaker_tier"] = "CAUTION_RECOVERING"
    state["freeze_remaining_bars"] = 3
    info = resolve_stage1_health_gate(state, {})
    assert info["is_recovering"] is False
    assert info["freeze_remaining_bars"] == 3


def test_genuine_recovery_still_recovers_when_nothing_is_more_severe():
    """No false positive: a real CAUTION_RECOVERING (nothing worse anywhere) keeps working."""
    state = _stale_recovering_state()
    state["circuit_breaker_tier"] = "CAUTION_RECOVERING"
    report = {
        "as_of_date": _AS_OF,
        "portfolio_health": {
            "tier": "CAUTION_RECOVERING",
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "nav_breaker_evaluated": True,
            "circuit_breaker_tier": "CAUTION_RECOVERING",
            "linear_equity_scale": 0.30,
            "nav_source": "mtm",
        },
    }
    info = resolve_stage1_health_gate(state, report)
    assert info["is_recovering"] is True
    assert info["linear_equity_scale"] == 0.30


# ---------------------------------------------------------------------------
# 4. `linear_equity_scale` keeps the more conservative of the two archives
# ---------------------------------------------------------------------------


def test_linear_equity_scale_takes_the_minimum_of_both_archives():
    state = _stale_recovering_state()                  # stale 1.0
    report = _halted_report(linear_equity_scale=0.5)   # this round: 0.5
    info = resolve_stage1_health_gate(state, report)
    assert info["linear_equity_scale"] == pytest.approx(0.5)


def test_linear_equity_scale_ignores_the_stale_green_value_when_only_it_exists():
    state = _stale_recovering_state()
    info = resolve_stage1_health_gate(state, dict(HEALTHY_REPORT))
    assert info["linear_equity_scale"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 5. `LIVETRADING_ALLOW_DEGRADED_GATE` inherited from the process environment
# ---------------------------------------------------------------------------


def test_inherited_ack_has_no_override_evidence(monkeypatch):
    monkeypatch.setenv(ALLOW_DEGRADED_GATE_ENV, "1")
    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), DEGRADED_REPORT)
    assert degraded_gate_ack_env_inherited(info) is True


def test_ack_with_an_override_nav_source_is_not_flagged(monkeypatch):
    monkeypatch.setenv(ALLOW_DEGRADED_GATE_ENV, "1")
    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), DEGRADED_REPORT)
    info["nav_source"] = "override"
    assert degraded_gate_ack_env_inherited(info) is False


def test_ack_without_a_degraded_gate_is_not_flagged(monkeypatch):
    monkeypatch.setenv(ALLOW_DEGRADED_GATE_ENV, "1")
    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), dict(HEALTHY_REPORT))
    assert info["degraded"] is False
    assert degraded_gate_ack_env_inherited(info) is False


def test_env_ack_without_override_is_never_silent():
    """No env var -> no flag, whatever the provenance (nothing was acknowledged)."""
    info = resolve_stage1_health_gate(dict(HEALTHY_ACCOUNT_STATE), DEGRADED_REPORT)
    assert degraded_gate_ack_env_inherited(info) is False


# ---------------------------------------------------------------------------
# 6. Existing healthy verdicts are not falsely refused
# ---------------------------------------------------------------------------


def test_healthy_archives_are_still_accepted():
    info = assert_stage1_gate_usable(dict(HEALTHY_ACCOUNT_STATE), dict(HEALTHY_REPORT))
    assert info["degraded"] is False
    assert info["degraded_reasons"] == []
    assert info["circuit_breaker_tier"] == "NORMAL"
    assert info["is_recovering"] is False


def test_legacy_flat_report_without_status_keys_is_still_accepted():
    """Legacy flat vintage: only a top-level tier, no `tier_status` -> still a gate block."""
    info = assert_stage1_gate_usable({}, {"circuit_breaker_tier": "TIER_1_DAMPING"})
    assert info["degraded"] is False
    assert info["circuit_breaker_tier"] == "TIER_1_DAMPING"


# ---------------------------------------------------------------------------
# 7. End to end through `main()`: TIER_3_HALT in THIS round must liquidate,
#    even though the carried account_state still says CAUTION_RECOVERING.
# ---------------------------------------------------------------------------


def _write_json(path_dir, name, payload):
    path = os.path.join(str(path_dir), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, default=str)
    return path


def _write_universe(path_dir, symbols):
    path = os.path.join(str(path_dir), "universe.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _live_args(out_dir, universe_file, state_file, extra=None):
    cmd = [
        "--strategy", "equal_weight",
        "--universe-file", universe_file,
        "--account-state-file", state_file,
        "--output-dir", str(out_dir),
        "--as-of-date", _AS_OF,
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--no-cache",
    ]
    if extra:
        cmd.extend(extra)
    return cmd


@pytest.fixture
def fixed_weights():
    """Pin the strategy's targets so the assertion is about the GATE, not the signal."""
    from unittest.mock import patch

    from common.allocation_templates import EqualWeightAllocation

    orig_gen = EqualWeightAllocation.generate_weights

    def mock_gen(self, universe, params=None):
        df = orig_gen(self, universe, params)
        for sym, weight in {"600000.SH": 0.50, "BIL": 0.50}.items():
            if sym in df.columns:
                df[sym] = weight
        return df

    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        yield


def _lifecycle_row():
    return {
        "symbol": "600000.SH",
        "state": "HELD_GESTATION",
        "held_bars": 3,
        "current_weight": 0.50,
        "broker_weight": 0.50,
        "entry_price": 10.0,
        "peak_price": 10.0,
    }


def test_end_to_end_tier3_halt_liquidates_despite_stale_recovering(tmp_path, fixed_weights, capsys):
    from scripts.run_live_strategy import main as live_main

    run_dir = tmp_path / "halt"
    run_dir.mkdir()
    universe_file = _write_universe(run_dir, ["600000.SH", "BIL"])
    holdings_file = _write_json(run_dir, "holdings.json", {"600000.SH": 0.50, "BIL": 0.50})
    state = _stale_recovering_state()
    state["lifecycle_states"] = {"600000.SH": _lifecycle_row()}
    state_file = _write_json(run_dir, "account_state.json", state)
    _write_json(run_dir, "stage1_health_report.json", _halted_report())

    order_df = live_main(
        _live_args(
            run_dir,
            universe_file,
            state_file,
            extra=[
                "--current-holdings-file", holdings_file,
                "--health-report-file", os.path.join(str(run_dir), "stage1_health_report.json"),
                "--min-holding-bars", "2",
            ],
        )
    )
    out = capsys.readouterr().out
    assert "CIRCUIT BREAKER ENGAGED" in out
    assert "FAST RECOVERY OVERRIDE" not in out

    row = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert row["action"] == "SELL"
    assert float(row["target_weight"]) == pytest.approx(0.0)
    bil_row = order_df[order_df["symbol"] == "BIL"].iloc[0]
    assert float(bil_row["target_weight"]) >= 0.999


def test_end_to_end_inherited_env_ack_is_recorded_in_summary(tmp_path, monkeypatch, capsys):
    """A degraded gate released by an INHERITED env var must say so in the archive."""
    from scripts.run_live_strategy import main as live_main

    monkeypatch.setenv(ALLOW_DEGRADED_GATE_ENV, "1")
    run_dir = tmp_path / "degraded"
    run_dir.mkdir()
    universe_file = _write_universe(run_dir, ["600000.SH", "BIL"])
    holdings_file = _write_json(run_dir, "holdings.json", {"BIL": 1.0})
    state_file = _write_json(
        run_dir,
        "account_state.json",
        {
            "tier_status": "EVALUATED",
            "nav_gate_status": "OK",
            "circuit_breaker_tier": "NORMAL",
            "nav_breaker_evaluated": True,
            "nav_source": "mtm",
            "nav_mtm": 100000.0,
        },
    )
    _write_json(run_dir, "stage1_health_report.json", DEGRADED_REPORT)

    live_main(
        _live_args(
            run_dir,
            universe_file,
            state_file,
            extra=[
                "--current-holdings-file", holdings_file,
                "--health-report-file", os.path.join(str(run_dir), "stage1_health_report.json"),
            ],
        )
    )
    out = capsys.readouterr().out
    assert "[WARN] 降级放行来自进程环境而非当次人工确认" in out

    with open(os.path.join(str(run_dir), "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["gate_degraded_ack_env_inherited"] is True

"""Offline regression suite for the 2026-11-12 `account_state.json` version race.

FORENSIC CONTEXT (why this file exists)
---------------------------------------
Two instances of the SAME session disagreed:

* ``2026-11-12/162625`` FAILED with ``LiveDumpPreflightError``: its
  ``account_state.json`` recorded ``strategy_version='1.2.2'`` while the live class was
  ``1.3.0``.
* ``2026-11-12/162857`` SUCCEEDED from the same inputs, with ``strategy_version='1.3.0'``
  in its state.

The ``1.2.2`` was not a version-upgrade cost: it was INHERITED, the way a carried-forward
snapshot is inherited. The state a round starts from is a COPY of the previous session's
``account_state.json`` (``execution_service`` seeds it, ``prior_state`` records where it came
from), and ``build_account_state_update`` starts from ``dict(state)`` -- so the old engine's
version survived every rewrite until Stage 2's fail-closed parity gate refused the round.
Which of the two same-day instances won was decided by write ORDER, i.e. a race.

THE CONTRACT PINNED HERE (the two halves must NOT be conflated):

* the state a round creates FOR ITS OWN SESSION (``as_of_date == this round``) carries the
  CURRENT round's triple -- ``strategy_version`` / ``base_version`` /
  ``strategy_version_info`` -- never the triple inherited from the snapshot it was seeded
  from;
* an ARCHIVED snapshot is lineage evidence. Wherever its own block survives (``prior_state``),
  its ``strategy_version`` still says ``1.2.2``. Nothing re-stamps history
  (``docs/lv7_prior_state_contract.md`` §5.4);
* a state that already claims THIS session while carrying an older triple is reported with
  the grep-able ``[STATE VERSION CLOBBERED]`` marker, NOT with the parity gate's
  "regenerate the dump" wording (the dump check has already passed by then).

100% OFFLINE: hand-written dicts plus ``tmp_path`` files. No market data, no network, and no
read of anything under ``data/runs/``.
"""

import importlib.util
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from scripts.live_preflight import (  # noqa: E402  (repo root is on sys.path just above)
    ACCOUNT_STATE_VERSION_FIELDS,
    LiveDumpPreflightError,
    STATE_VERSION_CLOBBER_MARKER,
    assert_account_state_version_parity,
    assert_state_version_not_clobbered,
    detect_state_version_clobber,
    stamp_account_state_version,
    state_version_mismatches,
    version_triple_from_strategy_def,
)


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "check_live_portfolio_health", os.path.join("scripts", "check_live_portfolio_health.py")
)

TODAY = "2026-11-12"
YESTERDAY = "2026-11-11"

#: The live engine of the incident day, and the version the seeded snapshot carried.
LIVE_TRIPLE = {
    "strategy_version": "1.3.0",
    "base_version": "2.0.0",
    "strategy_version_info": {"version": "1.3.0", "base_version": "2.0.0"},
}
PRIOR_TRIPLE = {"strategy_version": "1.2.2", "base_version": "2.0.0"}


def _snapshot(**overrides):
    """The 11-11 snapshot a round is seeded from (`PRIOR_TRIPLE`, NAV 101,000.00)."""
    state = {
        "as_of_date": YESTERDAY,
        "current_nav": 101000.00,
        "peak_nav": 105000.00,
        "drawdown_pct": -0.0380952381,
        "circuit_breaker_tier": "NORMAL",
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "linear_equity_scale": 1.0,
        "nav_history_10d": [100000.0, 101000.0],
        "strategy_version": PRIOR_TRIPLE["strategy_version"],
        "base_version": PRIOR_TRIPLE["base_version"],
        "repair_provenance": {
            "repaired_at": "2026-11-11T07:37:10",
            "script_version": "repair_archive_2026-11-11/v2",
        },
    }
    state.update(overrides)
    return state


def _measured_dd_info(**overrides):
    """A MEASURED gate verdict (the non-degraded branch of `build_account_state_update`)."""
    info = {
        "current_nav": 101500.00,
        "peak_nav": 105000.00,
        "drawdown_pct": -0.0333333333,
        "tier": "NORMAL",
        "tier_status": "NORMAL",
        "linear_equity_scale": 1.0,
        "tier1_counter": 0,
        "tier3_counter": 0,
        "freeze_remaining_bars": 0,
        "nav_source": "mark_to_market",
        "nav_mtm": 101500.00,
        "nav_form": 101500.00,
        "nav_deviation_pct": 0.0,
        "nav_deviation_exceeded": False,
        "nav_gate_status": "MEASURED",
        "nav_timebase_status": "single_instant",
    }
    info.update(overrides)
    return info


# ---------------------------------------------------------------------------
# 1. The round's own state: current triple on top, history intact inside
#    `prior_state` (the seeding-race fix itself)
# ---------------------------------------------------------------------------


def test_new_round_state_carries_live_version_and_prior_state_keeps_history():
    """``1.2.2`` seeded + ``1.3.0`` running -> state says 1.3.0, `prior_state` still 1.2.2."""
    seeded = _snapshot()
    updated = _mod.build_account_state_update(
        seeded,
        seeded,
        _measured_dd_info(),
        as_of_date=TODAY,
        gate_code="NORMAL",
        intraday_5m_info={"intraday_buy_scale": 1.0},
        prior_state=_snapshot(),
        version_triple=version_triple_from_strategy_def(
            {
                "strategy_version": "1.3.0",
                "base_version": "2.0.0",
                "version_info": {"version": "1.3.0", "base_version": "2.0.0"},
            }
        ),
    )

    # THIS round's own file: the live triple, on every axis.
    assert updated["as_of_date"] == TODAY
    assert updated["strategy_version"] == "1.3.0"
    assert updated["base_version"] == "2.0.0"
    assert updated["strategy_version_info"] == LIVE_TRIPLE["strategy_version_info"]

    # The carried snapshot inside `prior_state` is untouched lineage evidence.
    assert updated["prior_state"]["strategy_version"] == "1.2.2"
    assert updated["prior_state"]["base_version"] == "2.0.0"
    assert updated["prior_state"]["repair_provenance"]["repaired_at"] == "2026-11-11T07:37:10"

    # And the fail-closed gate that refused the incident's round now passes.
    assert_account_state_version_parity(updated, "1.3.0", "2.0.0")


def test_round_state_without_a_triple_is_byte_identical_to_the_old_behaviour():
    """Omitting `version_triple` keeps every existing direct caller's output unchanged."""
    seeded = _snapshot()
    updated = _mod.build_account_state_update(
        seeded,
        seeded,
        _measured_dd_info(),
        as_of_date=TODAY,
        gate_code="NORMAL",
        intraday_5m_info={"intraday_buy_scale": 1.0},
        prior_state=_snapshot(),
    )

    # The defect, still reproducible when the caller supplies nothing: the inherited 1.2.2
    # survives, which is exactly why the `main()` call site MUST pass the triple.
    assert updated["strategy_version"] == "1.2.2"
    assert "strategy_version_info" not in updated


def test_stage1_main_passes_the_round_triple_and_verifies_what_landed():
    """Source pin: the stamp is wired into the only writer of the round's own state."""
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()

    assert "version_triple=_round_triple" in source
    assert "_round_triple = _round_version_triple(strategy_def)" in source
    assert "detect_state_version_clobber(" in source
    # The landed-file check must run AFTER the save, not instead of it.
    assert source.index("save_account_state(args.account_state_file, updated_state)") < (
        source.index("_landed = load_account_state(args.account_state_file)")
    )


# ---------------------------------------------------------------------------
# 2. The stamp primitive: it may only ever write the CURRENT triple
# ---------------------------------------------------------------------------


def test_stamp_never_descends_into_prior_state():
    state = {
        "as_of_date": TODAY,
        "prior_state": _snapshot(),
        "strategy_version": "1.2.2",
    }

    stamp_account_state_version(state, LIVE_TRIPLE)

    assert state["strategy_version"] == "1.3.0"
    assert state["base_version"] == "2.0.0"
    assert state["prior_state"]["strategy_version"] == "1.2.2"
    assert state["prior_state"]["as_of_date"] == YESTERDAY


def test_stamp_removes_an_axis_the_current_engine_no_longer_has():
    """A blend-era `base_version` must not survive on a non-blend template."""
    state = {"as_of_date": TODAY, "strategy_version": "1.2.2", "base_version": "2.0.0"}

    stamp_account_state_version(state, {"strategy_version": "1.0.0"})

    assert state == {"as_of_date": TODAY, "strategy_version": "1.0.0"}
    # Parity sees "no such axis" on both sides, i.e. the canonical non-blend spelling.
    assert_account_state_version_parity(state, "1.0.0", None)


def test_version_triple_from_strategy_def_emits_only_truthy_axes():
    assert version_triple_from_strategy_def({"strategy_version": "1.3.0"}) == {
        "strategy_version": "1.3.0"
    }
    assert version_triple_from_strategy_def({})["strategy_version"] == "1.0.0"
    assert version_triple_from_strategy_def(None)["strategy_version"] == "1.0.0"
    assert version_triple_from_strategy_def(
        {"strategy_version": "1.3.0", "base_version": "2.0.0", "version_info": {"v": 1}}
    ) == {
        "strategy_version": "1.3.0",
        "base_version": "2.0.0",
        "strategy_version_info": {"v": 1},
    }
    assert set(ACCOUNT_STATE_VERSION_FIELDS) == {
        "strategy_version",
        "base_version",
        "strategy_version_info",
    }


# ---------------------------------------------------------------------------
# 3. The guard: a polluted state dated TODAY is a seeding race, not a stale dump
# ---------------------------------------------------------------------------


def test_clobber_guard_raises_the_grepable_marker_for_a_state_dated_today():
    polluted = {"as_of_date": TODAY, "strategy_version": "1.2.2", "base_version": "2.0.0"}

    with pytest.raises(LiveDumpPreflightError) as exc:
        assert_state_version_not_clobbered(
            polluted, "1.3.0", "2.0.0", as_of_date=TODAY, state_display="state.json"
        )

    message = str(exc.value)
    assert STATE_VERSION_CLOBBER_MARKER in message
    assert "[STATE VERSION CLOBBERED]" in message
    assert "播种/结转竞态" in message
    # The remedy must NOT be "regenerate the dump" -- the dump gate already passed.
    assert "重新生成 dump" in message and "不是" in message


def test_clobber_guard_is_scoped_to_this_rounds_own_state():
    """A carried snapshot dated YESTERDAY keeps its 1.2.2 -- no clobber diagnosis."""
    carried = _snapshot()

    assert detect_state_version_clobber(carried, "1.3.0", "2.0.0", as_of_date=TODAY) is None
    assert_state_version_not_clobbered(carried, "1.3.0", "2.0.0", as_of_date=TODAY)

    # ... and with no as-of scope at all the guard is inert (it cannot know the session).
    assert detect_state_version_clobber(carried, "1.3.0", "2.0.0") is None


def test_clobber_guard_is_silent_once_the_state_carries_the_live_version():
    clean = {"as_of_date": TODAY, "strategy_version": "1.3.0", "base_version": "2.0.0"}

    assert state_version_mismatches(clean, "1.3.0", "2.0.0") == []
    assert detect_state_version_clobber(clean, "1.3.0", "2.0.0", as_of_date=TODAY) is None
    assert_account_state_version_parity(clean, "1.3.0", "2.0.0")


def test_parity_still_refuses_when_the_state_is_forced_back_to_the_old_version():
    """The pre-existing fail-closed refusal is NOT weakened by the new guard."""
    state = {"as_of_date": TODAY, "strategy_version": "1.3.0", "base_version": "2.0.0"}
    assert_account_state_version_parity(state, "1.3.0", "2.0.0")

    state["strategy_version"] = "1.2.2"

    with pytest.raises(LiveDumpPreflightError) as parity_exc:
        assert_account_state_version_parity(state, "1.3.0", "2.0.0")
    # The parity message keeps its own wording...
    assert "版本元数据不一致" in str(parity_exc.value)
    assert STATE_VERSION_CLOBBER_MARKER not in str(parity_exc.value)

    # ... while the guard placed BEFORE it gives the operator the right diagnosis.
    with pytest.raises(LiveDumpPreflightError) as guard_exc:
        assert_state_version_not_clobbered(state, "1.3.0", "2.0.0", as_of_date=TODAY)
    assert STATE_VERSION_CLOBBER_MARKER in str(guard_exc.value)


def test_mismatch_helper_mirrors_the_parity_gates_axes_exactly():
    """Absent key == nothing asserted; an explicit empty value is a real mismatch."""
    assert state_version_mismatches({"as_of_date": TODAY}, "1.3.0", "2.0.0") == []
    assert state_version_mismatches(
        {"strategy_version": "", "as_of_date": TODAY}, "1.3.0"
    ) == ["strategy_version None != '1.3.0'"]
    assert state_version_mismatches(
        {"strategy_version": "1.3.0", "base_version": "2.0.0"}, "1.3.0", "1.9.0"
    ) == ["base_version '2.0.0' != '1.9.0'"]


# ---------------------------------------------------------------------------
# 4. Stage 2 wires the guard before BOTH parity calls (read-only, no auto-heal)
# ---------------------------------------------------------------------------


def test_stage2_calls_the_clobber_guard_before_both_parity_checks():
    path = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()

    guard_calls = source.count("assert_state_version_not_clobbered(")
    parity_calls = source.count("assert_account_state_version_parity(")
    # Two call sites for the guard; the parity import line has no `(` but both of its calls do.
    assert guard_calls == 2, guard_calls
    assert parity_calls == 2, parity_calls

    for checkpoint in (
        "assert_state_version_not_clobbered(\n        account_state,\n        _live_strategy_version,\n        _live_base_version,\n        as_of_date=as_of_date,\n        state_display=args.account_state_file,\n    )\n    assert_account_state_version_parity(",
        "assert_state_version_not_clobbered(\n            account_state,\n            _live_strategy_version,\n            _live_base_version,\n            as_of_date=as_of_date,\n            state_display=args.account_state_file,\n        )\n        assert_account_state_version_parity(",
    ):
        assert checkpoint in source

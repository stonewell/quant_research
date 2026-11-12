"""`D2` -- the web layer's carry-forward must obey Stage 2's `prior_state` rule.

FORENSIC CONTEXT (archive `2026-11-09/204956`)
----------------------------------------------
`prior_state.as_of_date` was `2026-11-05` while a SUCCESSFUL `2026-11-06/185016`
(`current_nav=199,480.90`, `base_version=2.0.0`) existed.  The two directories the old
"first earlier dir that HOLDS an `account_state.json`" sweep preferred were:

* `2026-11-06/184826` -- `health_status=SUCCESS` but `deploy_status=FAILED`
  (`nav=186,476.57`, a Stage-1 placeholder, not a settlement);
* `2026-11-08/184755` -- an ORPHAN: the `--as-of-date 2026-11-08` refusal left a directory
  holding a *copied* 11-05 book (`base_version=1.8.0`), i.e. a state whose OWN `as_of_date`
  is not its run date.

Consequences downstream: `nav_history_10d` never received 11-06's value, the 10-day
recovery measure fell to `INSUFFICIENT_HISTORY`, and the "-5.23%" reported on 11-09 was
really a two-session move.

THE RULE PINNED HERE
--------------------
`StorageManager._carry_forward_account_state` no longer has a rule of its own: it calls
`scripts/live_preflight.select_prior_state` (import lines in `storage.py`), so the web
layer and Stage 2 cannot disagree.  Cases covered below:

1. orphan (`state.as_of_date != its run date`) is skipped, never carried;
2. a recorded non-SUCCESS verdict (`deploy_status` wins over `health_status`) is skipped;
3. a SAME-DAY RERUN (`<date>/<HHMMSS>` whose `as_of` equals its date) is NOT an orphan;
4. `base_version` mismatch is skipped when the caller can state the expected engine;
5. no admissible candidate -> `[PRIOR STATE] unresolved` is logged and the pre-D2 sweep is
   kept as the last resort (an existing `prior_state` is never destroyed or rewritten);
6. the snapshot is returned verbatim -- `base_version` / `strategy_version` are NOT
   re-stamped (`docs/lv7_prior_state_contract.md` §5.4);
7. the audit block (`prior_state_source`) travels with the snapshot and is installed NEXT
   TO `prior_state`, not nested inside it (`ExecutionService._install_prior_state`).

100% offline: `tmp_path` archives only, no market data, no network, no `data/runs/**`.
"""

import json
import logging
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution_service  # noqa: E402  (bare name, exactly how `server.py` imports it)
import storage  # noqa: E402

from scripts.live_preflight import (  # noqa: E402  (repo root is on sys.path just above)
    PRIOR_STATE_LOG_PREFIX,
    SKIP_AS_OF_MISMATCH,
    SKIP_BASE_VERSION_MISMATCH,
    SKIP_STATUS_NOT_SUCCESS,
)

STRATEGY_KEY = "chan_four_state_blend"
UNIVERSE_KEY = "custom_astock_202609_20"


@pytest.fixture
def temp_storage(tmp_path):
    """Hermetic `StorageManager`: every directory under `tmp_path`, never the real tree."""
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _snapshot(
    store,
    run_date,
    instance=None,
    *,
    as_of=None,
    nav=100000.0,
    base_version="2.0.0",
    strategy_version="1.2.2",
    deploy_status="SUCCESS",
    health_status="SUCCESS",
    timestamp=None,
):
    """Create `<archive>/<strat>/<univ>/<date>[/<HHMMSS>]/` with state + summary."""
    session_root = os.path.join(store.archive_dir, STRATEGY_KEY, UNIVERSE_KEY)
    run_dir = (
        os.path.join(session_root, run_date, instance)
        if instance
        else os.path.join(session_root, run_date)
    )
    os.makedirs(run_dir, exist_ok=True)
    state = {
        "as_of_date": as_of or run_date,
        "current_nav": nav,
        "peak_nav": nav,
        "base_version": base_version,
        "strategy_version": strategy_version,
        "nav_history_10d": [nav] * 8,
    }
    _write_json(os.path.join(run_dir, "account_state.json"), state)
    summary = {
        "date": run_date,
        "timestamp": timestamp or f"{run_date}T{instance or '000000'}",
        "strategy_key": STRATEGY_KEY,
        "universe_key": UNIVERSE_KEY,
    }
    if health_status is not None:
        summary["health_status"] = health_status
    if deploy_status is not None:
        summary["deploy_status"] = deploy_status
    _write_json(os.path.join(run_dir, "summary.json"), summary)
    return run_dir


def _skipped(carried):
    return {
        s["instance"]: s
        for s in carried["prior_state_source"].get("skipped", [])
    }


# ---------------------------------------------------------------------------
# 1. The archived 11-05 / 11-06 / 11-08 triple
# ---------------------------------------------------------------------------


def test_newest_successful_snapshot_wins_over_failed_and_orphan(temp_storage):
    """THE INCIDENT: the orphan 11-08 copy and the FAILED 11-06 deploy must both lose."""
    _snapshot(temp_storage, "2026-11-05", "202418", nav=208830.78)
    _snapshot(
        temp_storage,
        "2026-11-06",
        "184826",
        nav=186476.57,
        deploy_status="FAILED",
        health_status="SUCCESS",
    )
    success_dir = _snapshot(temp_storage, "2026-11-06", "185016", nav=199480.90)
    _snapshot(temp_storage, "2026-11-08", "184755", as_of="2026-11-05", nav=208830.78)

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried is not None
    assert carried["current_nav"] == pytest.approx(199480.90)
    source = carried["prior_state_source"]
    assert source["path"] == os.path.join(success_dir, "account_state.json")
    assert source["as_of"] == "2026-11-06"
    assert source["instance"] == "185016"
    assert source["run_status"] == "SUCCESS"
    assert source["status_source"] == "deploy_status"

    skipped = _skipped(carried)
    # The orphan is rejected by the `as_of` signature, the sibling by its deploy verdict.
    assert skipped["184755"]["reason"] == SKIP_AS_OF_MISMATCH
    assert skipped["184826"]["reason"] == SKIP_STATUS_NOT_SUCCESS
    # The older, perfectly healthy 11-05 date is NOT what was carried.
    assert "2026-11-05" not in source["path"]


def test_same_day_rerun_is_not_treated_as_an_orphan(temp_storage):
    """`<date>/<HHMMSS>` with `as_of == date` is a LEGAL snapshot, not an orphan copy."""
    _snapshot(temp_storage, "2026-11-05", "202418", nav=208830.78)
    _snapshot(temp_storage, "2026-11-08", "091500", nav=199000.00)
    rerun = _snapshot(temp_storage, "2026-11-08", "143000", nav=199480.90)

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried["current_nav"] == pytest.approx(199480.90)
    assert carried["prior_state_source"]["path"] == os.path.join(
        rerun, "account_state.json"
    )
    assert carried["prior_state_source"]["instance"] == "143000"
    # Nothing was skipped: a same-day re-execution is documented, not suspicious.
    assert carried["prior_state_source"]["skipped"] == []


def test_failed_newest_instance_falls_back_inside_the_same_date(temp_storage):
    """A newer FAILED instance must not push the carry back to an older DATE."""
    _snapshot(temp_storage, "2026-11-05", "202418", nav=208830.78)
    earlier_success = _snapshot(temp_storage, "2026-11-06", "184826", nav=186476.57)
    _snapshot(
        temp_storage,
        "2026-11-06",
        "185016",
        nav=199480.90,
        deploy_status="FAILED",
        timestamp="2026-11-06T18:50:16",
    )

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried["prior_state_source"]["run_date"] == "2026-11-06"
    assert carried["prior_state_source"]["path"] == os.path.join(
        earlier_success, "account_state.json"
    )


# ---------------------------------------------------------------------------
# 2. The version axis (opt-in: only the caller knows today's live engine)
# ---------------------------------------------------------------------------


def test_base_version_mismatch_is_skipped_when_the_expected_engine_is_known(temp_storage):
    """A snapshot written by another engine (`1.8.0` vs `2.0.0`) must lose."""
    _snapshot(temp_storage, "2026-11-05", "202418", nav=208830.78, base_version="1.8.0")
    good = _snapshot(temp_storage, "2026-11-05", "210000", nav=209000.00, base_version="2.0.0")

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY, expected_base_version="2.0.0"
    )

    assert carried["current_nav"] == pytest.approx(209000.00)
    assert carried["prior_state_source"]["path"] == os.path.join(
        good, "account_state.json"
    )
    assert _skipped(carried)["202418"]["reason"] == SKIP_BASE_VERSION_MISMATCH


def test_versions_are_carried_verbatim_never_re_stamped(temp_storage):
    """§5.4: the web layer READS versions, it never re-stamps them."""
    _snapshot(
        temp_storage,
        "2026-11-06",
        "185016",
        nav=199480.90,
        base_version="2.0.0",
        strategy_version="1.2.2",
    )

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried["base_version"] == "2.0.0"
    assert carried["strategy_version"] == "1.2.2"
    # No `prior_state` is fabricated here: callers install the snapshot AS `prior_state`.
    assert "prior_state" not in carried or carried["prior_state"] == {}
    assert "prior_state_source" in carried


# ---------------------------------------------------------------------------
# 3. Unresolved: keep the pre-D2 sweep, destroy nothing, say so
# ---------------------------------------------------------------------------


def test_no_admissible_candidate_keeps_the_last_resort_state_and_logs(
    temp_storage, caplog
):
    """Every candidate rejected -> legacy sweep still seeds, and the reason is logged."""
    failed_dir = _snapshot(
        temp_storage, "2026-11-08", "184755", as_of="2026-11-05", nav=208830.78
    )

    caplog.set_level(logging.WARNING)
    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    # The sweep is the LAST resort: the state comes back exactly as written.
    assert carried is not None
    assert carried["current_nav"] == pytest.approx(208830.78)
    assert "prior_state_source" not in carried, "an unresolved selection stamps nothing"
    assert carried["as_of_date"] == "2026-11-05", "never rewritten into the run date"
    unresolved = [
        r.getMessage()
        for r in caplog.records
        if PRIOR_STATE_LOG_PREFIX in r.getMessage() and "unresolved" in r.getMessage()
    ]
    assert unresolved, [r.getMessage() for r in caplog.records]
    assert "reason=no_admissible_candidate" in unresolved[0]


def test_empty_archive_returns_none_and_logs_unresolved_quietly(temp_storage, caplog):
    """First-ever run: no candidates at all -> `None`, logged at INFO, never a warning."""
    caplog.set_level(logging.INFO)
    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried is None
    records = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if "unresolved" in r.getMessage()
    ]
    assert records, "the unresolved verdict must be visible"
    assert records[0][0] == logging.INFO
    assert "reason=no_candidate_directories_under_session_root" in records[0][1]


def test_unreadable_state_is_never_carried_without_a_source_block(temp_storage):
    """A directory holding an EMPTY/unreadable state is reported, not silently used."""
    run_dir = os.path.join(
        temp_storage.archive_dir, STRATEGY_KEY, UNIVERSE_KEY, "2026-11-06"
    )
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "account_state.json"), "w", encoding="utf-8") as f:
        f.write("")

    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    assert carried is None


# ---------------------------------------------------------------------------
# 4. Wiring: `get_account_state` and the deploy seed both go through the resolver
# ---------------------------------------------------------------------------


def test_get_account_state_carries_the_successful_snapshot_with_its_source(temp_storage):
    _snapshot(temp_storage, "2026-11-05", "202418", nav=208830.78)
    _snapshot(temp_storage, "2026-11-06", "185016", nav=199480.90)
    _snapshot(temp_storage, "2026-11-08", "184755", as_of="2026-11-05", nav=208830.78)

    state = temp_storage.get_account_state("2026-11-09", STRATEGY_KEY, UNIVERSE_KEY)

    assert state["current_nav"] == pytest.approx(199480.90)
    assert state["prior_state_source"]["as_of"] == "2026-11-06"


def test_install_prior_state_splits_the_snapshot_from_its_provenance(temp_storage):
    """`prior_state` = the SNAPSHOT, `prior_state_source` = the audit block, side by side."""
    _snapshot(temp_storage, "2026-11-06", "185016", nav=199480.90)
    carried = temp_storage._carry_forward_account_state(
        "2026-11-09", STRATEGY_KEY, UNIVERSE_KEY
    )

    service = execution_service.ExecutionService(temp_storage)
    container = {"as_of_date": "2026-11-09", "current_nav": 200000.0}
    service._install_prior_state(container, carried)

    assert container["prior_state"]["current_nav"] == pytest.approx(199480.90)
    assert "prior_state_source" not in container["prior_state"], (
        "nesting the block inside the snapshot would deepen it every round"
    )
    assert container["prior_state_source"]["as_of"] == "2026-11-06"
    assert container["prior_state_source"]["path"].endswith("account_state.json")


def test_install_prior_state_is_a_no_op_without_a_snapshot(temp_storage):
    service = execution_service.ExecutionService(temp_storage)
    container = {"as_of_date": "2026-11-09"}

    service._install_prior_state(container, None)
    service._install_prior_state(container, {})

    assert container == {"as_of_date": "2026-11-09"}

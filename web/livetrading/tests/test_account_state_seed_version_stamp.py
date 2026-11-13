"""Offline regression suite for the 2026-11-12 `account_state.json` seeding race (web layer).

FORENSIC CONTEXT (why this file exists)
---------------------------------------
`2026-11-12/162625` FAILED with ``LiveDumpPreflightError``: its `account_state.json`
recorded ``strategy_version='1.2.2'`` while the live class was ``1.3.0``. The sibling
instance `2026-11-12/162857` (same inputs, four minutes later) SUCCEEDED with `1.3.0`.

The `1.2.2` was INHERITED: `ExecutionService.run_health_check` seeds a fresh instance
directory by COPYING the newest state at-or-before the session, and
`build_account_state_update` starts from `dict(state)`. So the round published a state for
TODAY carrying YESTERDAY's engine version, and Stage 2's fail-closed parity gate refused it.
Which of the two instances won was decided by write ORDER -- a race, not a version-upgrade
cost.

WHAT THIS FILE PINS (`ExecutionService._seed_round_account_state`)
-----------------------------------------------------------------
1. CREATE-ONLY. An `account_state.json` that already exists for this round is never
   rewritten by seeding; only the `prior_state` / `prior_state_source` audit block may be
   merged (top-level version fields and business fields untouched).
2. A seed that already describes THIS round's session (`as_of_date == val_date`) is stamped
   with the CURRENT round's triple -- never the triple it was copied with -- and the landed
   file is verified.
3. A seed that describes an EARLIER session is copied VERBATIM. It becomes `prior_state` at
   Stage 1, and that block is lineage evidence: restamping it here would BOTH mislabel
   yesterday's book as today's AND wipe the `strategy_version` the state contract requires
   it to keep (`docs/lv7_prior_state_contract.md` §5.4). The state for the new session gets
   its triple from the writer that creates it
   (`scripts/check_live_portfolio_health.py::build_account_state_update`, pinned in
   `pipeline/tests/test_state_version_clobber_guard.py`).
4. The guard's failure is the grep-able ``[STATE VERSION CLOBBERED]`` marker, not the parity
   gate's "regenerate the dump" wording.

100% OFFLINE: every snapshot/summary/dump below is hand-written under `tmp_path`; nothing
reads `data/runs/**` and no child process is launched.
"""

import json
import os
import sys

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from execution_service import ExecutionService  # noqa: E402
from scripts.live_preflight import (  # noqa: E402
    LiveDumpPreflightError,
    STATE_VERSION_CLOBBER_MARKER,
)
from storage import StorageManager  # noqa: E402

STRATEGY_KEY = "strat_x"
UNIVERSE_KEY = "univ_y"
TODAY = "2026-11-12"
YESTERDAY = "2026-11-11"

#: What the incident's live class declares, and what the 11-11 snapshot carried.
LIVE_DUMP = {
    "strategy_name": "Chan Four State Blend",
    "template_name": "chan_four_state_blend",
    "strategy_version": "1.3.0",
    "base_version": "2.0.0",
    "version_info": {"version": "1.3.0", "base_version": "2.0.0"},
}
PRIOR_VERSION = "1.2.2"


@pytest.fixture
def temp_storage(tmp_path):
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


@pytest.fixture
def service(temp_storage):
    return ExecutionService(storage=temp_storage)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_dump(tmp_path, payload=None):
    return _write_json(str(tmp_path / "strategy_dump.json"), payload or dict(LIVE_DUMP))


def _snapshot(as_of=YESTERDAY, version=PRIOR_VERSION, nav=101000.00, **overrides):
    state = {
        "as_of_date": as_of,
        "current_nav": nav,
        "peak_nav": 105000.00,
        "circuit_breaker_tier": "NORMAL",
        "nav_history_10d": [100000.0, nav],
        "strategy_version": version,
        "base_version": "2.0.0",
        # Carried over verbatim from the 11-11 repair -- lineage, not a fresh stamp.
        "repair_provenance": {
            "repaired_at": "2026-11-11T07:37:10",
            "script_version": "repair_archive_2026-11-11/v2",
        },
    }
    state.update(overrides)
    return state


def _make_instance(storage, date_str, stamp, *, state, summary=None):
    """Materialise one archived instance dir that is admissible as a seed source."""
    run_dir = os.path.join(storage.archive_dir, STRATEGY_KEY, UNIVERSE_KEY, date_str, stamp)
    os.makedirs(run_dir, exist_ok=True)
    payload = {
        "date": date_str,
        "as_of": date_str,
        "strategy_key": STRATEGY_KEY,
        "universe_key": UNIVERSE_KEY,
        "timestamp": f"{date_str}T{stamp[:2]}:{stamp[2:4]}:{stamp[4:]}",
        "health_status": "SUCCESS",
    }
    payload.update(summary or {})
    _write_json(os.path.join(run_dir, "summary.json"), payload)
    # A Stage-2 artifact, so `list_runs` lists this dir even if a variant drops the summary.
    _write_json(os.path.join(run_dir, "live_trading_ticket.json"), [])
    if state is not None:
        _write_json(os.path.join(run_dir, "account_state.json"), state)
    return run_dir


def _new_round_file(storage, date_str=TODAY, stamp="083000"):
    run_dir = os.path.join(storage.archive_dir, STRATEGY_KEY, UNIVERSE_KEY, date_str, stamp)
    os.makedirs(run_dir, exist_ok=True)
    return os.path.join(run_dir, "account_state.json")


def _seed(service, target_file, *, dump_path, val_date=TODAY, install_prior_state=True):
    triple = service._round_version_triple(dump_path)
    service._seed_round_account_state(
        target_file,
        val_date=val_date,
        strat_key=STRATEGY_KEY,
        univ_key=UNIVERSE_KEY,
        version_triple=triple,
        install_prior_state=install_prior_state,
    )
    return triple


# ---------------------------------------------------------------------------
# 0. The triple's source is THIS round's dump, never the carried state
# ---------------------------------------------------------------------------


def test_round_triple_comes_from_the_strategy_dump(service, tmp_path):
    triple = service._round_version_triple(_write_dump(tmp_path))

    assert triple == {
        "strategy_version": "1.3.0",
        "base_version": "2.0.0",
        "strategy_version_info": {"version": "1.3.0", "base_version": "2.0.0"},
    }
    # An unreadable/missing dump degrades to the documented default, never to a prior state.
    assert service._round_version_triple(str(tmp_path / "missing.json")) == {
        "strategy_version": "1.0.0"
    }


# ---------------------------------------------------------------------------
# 1. A same-day seed is re-stamped with the round's triple; `prior_state` keeps history
# ---------------------------------------------------------------------------


def test_same_day_seed_is_stamped_with_the_round_triple_and_keeps_prior_state(
    service, temp_storage, tmp_path
):
    """`prior` says 1.2.2, live class 1.3.0 -> the new state says 1.3.0, history says 1.2.2."""
    prior = _snapshot(as_of=YESTERDAY, version=PRIOR_VERSION)
    # The earlier instance of the SAME session, polluted exactly like 162625's file: its
    # top level already claims TODAY while carrying the inherited 1.2.2.
    _make_instance(
        temp_storage,
        TODAY,
        "080000",
        state=_snapshot(as_of=TODAY, version=PRIOR_VERSION, prior_state=prior),
    )

    target = _new_round_file(temp_storage)
    _seed(service, target, dump_path=_write_dump(tmp_path))

    landed = _read_json(target)
    assert landed["strategy_version"] == "1.3.0"
    assert landed["base_version"] == "2.0.0"
    assert landed["strategy_version_info"] == LIVE_DUMP["version_info"]
    # The snapshot inside `prior_state` is history: untouched.
    assert landed["prior_state"]["strategy_version"] == PRIOR_VERSION
    assert landed["prior_state"]["repair_provenance"]["repaired_at"] == "2026-11-11T07:37:10"
    # Business fields are carried, not invented.
    assert landed["current_nav"] == 101000.00


def test_same_day_seed_gains_the_prior_state_block_without_relabelling_versions(
    service, temp_storage, tmp_path
):
    """A same-day seed with no `prior_state` gets the audit block merged in; versions stay live."""
    _make_instance(temp_storage, YESTERDAY, "184826", state=_snapshot())
    _make_instance(temp_storage, TODAY, "080000", state=_snapshot(as_of=TODAY, version="1.3.0"))

    target = _new_round_file(temp_storage)
    _seed(service, target, dump_path=_write_dump(tmp_path))

    landed = _read_json(target)
    assert landed["strategy_version"] == "1.3.0"
    assert landed["prior_state"]["as_of_date"] == YESTERDAY
    assert landed["prior_state"]["strategy_version"] == PRIOR_VERSION
    assert landed["prior_state_source"]["run_date"] == YESTERDAY


# ---------------------------------------------------------------------------
# 2. Create-only: an existing round state is never rolled back
# ---------------------------------------------------------------------------


def test_existing_round_state_is_never_rewritten_by_a_later_seed(
    service, temp_storage, tmp_path
):
    """Stage 1 wrote 1.3.0 for today; a second seeding pass must not revert it to 1.2.2."""
    _make_instance(
        temp_storage,
        TODAY,
        "080000",
        state=_snapshot(as_of=TODAY, version=PRIOR_VERSION, prior_state=_snapshot()),
    )
    target = _new_round_file(temp_storage)
    _write_json(
        target,
        {
            "as_of_date": TODAY,
            "current_nav": 101500.00,
            "strategy_version": "1.3.0",
            "base_version": "2.0.0",
            "prior_state": _snapshot(),
        },
    )
    before = _read_json(target)
    before_mtime = os.stat(target).st_mtime_ns

    _seed(service, target, dump_path=_write_dump(tmp_path))

    assert _read_json(target) == before
    assert os.stat(target).st_mtime_ns == before_mtime, "seeding must not touch the file at all"
    assert _read_json(target)["strategy_version"] == "1.3.0"


def test_existing_round_state_lacking_prior_state_only_gains_the_audit_block(
    service, temp_storage, tmp_path
):
    _make_instance(temp_storage, YESTERDAY, "184826", state=_snapshot())
    target = _new_round_file(temp_storage)
    _write_json(
        target,
        {
            "as_of_date": TODAY,
            "current_nav": 101500.00,
            "circuit_breaker_tier": "NORMAL",
            "strategy_version": "1.3.0",
            "base_version": "2.0.0",
        },
    )

    _seed(service, target, dump_path=_write_dump(tmp_path))

    landed = _read_json(target)
    assert landed["strategy_version"] == "1.3.0"
    assert landed["current_nav"] == 101500.00, "business fields are not overwritten"
    assert landed["prior_state"]["strategy_version"] == PRIOR_VERSION
    assert "prior_state_source" in landed


# ---------------------------------------------------------------------------
# 3. A cross-session snapshot is seeded verbatim (its triple is lineage, not today's)
# ---------------------------------------------------------------------------


def test_cross_session_snapshot_is_seeded_verbatim_and_never_relabelled(
    service, temp_storage, tmp_path
):
    """Seeding yesterday's book must not claim today's session or today's engine version."""
    _make_instance(temp_storage, YESTERDAY, "184826", state=_snapshot())

    target = _new_round_file(temp_storage)
    _seed(service, target, dump_path=_write_dump(tmp_path))

    landed = _read_json(target)
    assert landed["as_of_date"] == YESTERDAY
    assert landed["strategy_version"] == PRIOR_VERSION, (
        "restamping here would be copied into `prior_state` at Stage 1, destroying the "
        "lineage the contract requires it to keep"
    )
    assert landed["repair_provenance"]["script_version"] == "repair_archive_2026-11-11/v2"


def test_deploy_path_seeds_without_installing_a_prior_state_block(
    service, temp_storage, tmp_path
):
    """Stage 2 resolves `prior_state` itself; the deploy seed keeps its historical shape."""
    _make_instance(temp_storage, YESTERDAY, "184826", state=_snapshot())

    target = _new_round_file(temp_storage)
    _seed(service, target, dump_path=_write_dump(tmp_path), install_prior_state=False)

    landed = _read_json(target)
    assert "prior_state" not in landed
    assert landed["as_of_date"] == YESTERDAY


# ---------------------------------------------------------------------------
# 4. The landed-file assertion and its grep-able marker
# ---------------------------------------------------------------------------


def test_landed_state_rolled_back_to_the_old_version_raises_the_clobber_marker(
    service, temp_storage
):
    """The marker distinguishes "seeding race" from "regenerate the dump"."""
    polluted = _new_round_file(temp_storage)
    _write_json(
        polluted,
        {"as_of_date": TODAY, "strategy_version": PRIOR_VERSION, "base_version": "2.0.0"},
    )
    triple = {
        "strategy_version": "1.3.0",
        "base_version": "2.0.0",
        "strategy_version_info": dict(LIVE_DUMP["version_info"]),
    }

    with pytest.raises(LiveDumpPreflightError) as exc:
        service._assert_seeded_state_version(polluted, triple, TODAY)

    message = str(exc.value)
    assert STATE_VERSION_CLOBBER_MARKER in message
    assert "1.2.2" in message and "1.3.0" in message
    assert "不是「需要重新生成 dump」" in message


def test_landed_state_with_the_round_triple_passes_the_assertion(
    service, temp_storage, tmp_path
):
    _make_instance(
        temp_storage, TODAY, "080000", state=_snapshot(as_of=TODAY, version=PRIOR_VERSION)
    )
    target = _new_round_file(temp_storage)
    triple = _seed(service, target, dump_path=_write_dump(tmp_path))

    # No exception: this is the same assertion the seeding path runs after it writes.
    service._assert_seeded_state_version(target, triple, TODAY)
    assert _read_json(target)["strategy_version"] == triple["strategy_version"]

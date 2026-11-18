"""Offline regression suite for `D2` -- `prior_state` must be the newest SUCCESSFUL snapshot.

THE DEFECT UNDER TEST. `2026-11-09/204956/account_state.json` carried
`prior_state.as_of_date = "2026-11-05"` even though `2026-11-06/185016/` was a SUCCESSFUL
round (`current_nav=199,480.90`, `base_version=2.0.0`) and `2026-11-06/184826/` was a
FAILED deploy (`nav=186,476.57`). Consequences downstream: `nav_history_10d` never got
11-06's value, the 10-day recovery measure fell to `INSUFFICIENT_HISTORY`, and the "-5.23%"
reported on 11-09 was a two-session move. The 2026-11-08 instance was an orphan: a copy of
the 11-05 book inside an 11-08 directory.

THE RULES PINNED HERE (`scripts/live_preflight.py`, section "(D2)"):

1. Candidates are DIRECTORIES THAT HOLD AN `account_state.json` -- `<date>/` or
   `<date>/<HHMMSS>/`. There is NO implicit "date-root-level file" fallback: a date whose
   snapshots live only in instance subdirs is a normal candidate, not a reason to jump to an
   older date (that implicit fallback is what skipped 11-06/185016).
2. Inadmissible: a recorded non-SUCCESS verdict (`deploy_status` wins over `health_status`),
   a state whose OWN `as_of_date` is not its run date (the orphan signature), or a
   `base_version` from a DIFFERENT MAJOR than today's live engine. A same-major bump (patch
   / minor) IS admissible -- exact string equality here cold-started the book on every
   `BASE_VERSION` bump (`NV8`).
3. Newest date wins; within one date the newest instance wins; a failed newest instance
   falls back to the next-newest instance of the SAME date before any older date.
4. Every rejection is recorded with a machine-readable reason -- a FAILED snapshot is never
   skipped silently.

100% offline: `tmp_path` archives only, no network, no real market data, no `data/runs/**`.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from scripts.live_preflight import (
    PRIOR_STATE_LOG_PREFIX,
    SKIP_AS_OF_MISMATCH,
    SKIP_BASE_VERSION_MISMATCH,
    SKIP_BASE_VERSION_MISSING,
    SKIP_STATUS_NOT_SUCCESS,
    _version_majors_match,
    apply_prior_state_selection,
    derive_session_root,
    list_prior_state_candidates,
    select_prior_state,
)

_STRAT = "chan_four_state_blend"
_UNIV = "custom_astock_202609_20"
_ARCHIVE = "runs"


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _snapshot(
    root,
    run_date,
    instance=None,
    *,
    as_of=None,
    nav=100000.0,
    base_version="2.0.0",
    deploy_status="SUCCESS",
    health_status="SUCCESS",
    timestamp=None,
):
    """Create `<root>/<strat>/<univ>/<date>[/<HHMMSS>]/` with state + summary."""
    session_root = os.path.join(str(root), _ARCHIVE, _STRAT, _UNIV)
    run_dir = os.path.join(session_root, run_date, instance) if instance else os.path.join(
        session_root, run_date
    )
    os.makedirs(run_dir, exist_ok=True)
    _write_json(
        os.path.join(run_dir, "account_state.json"),
        {
            "as_of_date": as_of or run_date,
            "current_nav": nav,
            "base_version": base_version,
            "strategy_version": "1.2.2",
            "nav_history_10d": [nav] * 8,
        },
    )
    summary = {"date": run_date, "timestamp": timestamp or f"{run_date}T{instance or '000000'}"}
    if health_status is not None:
        summary["health_status"] = health_status
    if deploy_status is not None:
        summary["deploy_status"] = deploy_status
    _write_json(os.path.join(run_dir, "summary.json"), summary)
    return run_dir


def _selection(root, as_of="2026-11-09", expected="2.0.0"):
    return select_prior_state(
        os.path.join(str(root), _ARCHIVE, _STRAT, _UNIV),
        as_of,
        expected_base_version=expected,
    )


# ---------------------------------------------------------------------------
# Rule 1 + 3 + 4: the archived 11-05 / 11-06 pair
# ---------------------------------------------------------------------------


def test_picks_the_newest_successful_instance_not_the_older_date(tmp_path):
    """THE INCIDENT: older success + newer failed + same-day failed/success pair.

    The winner MUST be `2026-11-06/185016` -- not the older 11-05 date, and not the FAILED
    11-06/184826 that a "newest directory wins" walk would have taken.
    """
    _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78, base_version="2.0.0")
    _snapshot(
        tmp_path,
        "2026-11-06",
        "184826",
        nav=186476.57,
        deploy_status="FAILED",
        health_status="SUCCESS",
    )
    success_dir = _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)

    selection = _selection(tmp_path)

    assert selection.source is not None
    assert selection.source["path"] == os.path.join(success_dir, "account_state.json")
    assert selection.source["as_of"] == "2026-11-06"
    assert selection.source["instance"] == "185016"
    assert selection.source["run_status"] == "SUCCESS"
    assert selection.source["status_source"] == "deploy_status"
    assert selection.source["base_version"] == "2.0.0"
    assert selection.state["current_nav"] == pytest.approx(199480.90)
    # The FAILED sibling is recorded, never silently skipped.
    skipped = {s["instance"]: s for s in selection.source["skipped"]}
    assert skipped["184826"]["reason"] == SKIP_STATUS_NOT_SUCCESS
    assert skipped["184826"]["run_status"] == "FAILED"
    # And the older date's snapshot is NOT the answer.
    assert "2026-11-05" not in selection.source["path"]


def test_newest_date_itself_wins_even_when_a_previous_date_is_successful(tmp_path):
    """A successful snapshot on the NEWEST date must not lose to an older successful one."""
    _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78)
    newest = _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)

    selection = _selection(tmp_path)

    assert selection.source["path"] == os.path.join(newest, "account_state.json")


def test_falls_back_to_next_newest_instance_within_the_same_date(tmp_path):
    """Newest instance FAILED -> the SAME date's earlier SUCCESS is used, not an older date."""
    _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78)
    earlier_success = _snapshot(tmp_path, "2026-11-06", "184826", nav=186476.57)
    _snapshot(
        tmp_path,
        "2026-11-06",
        "185016",
        nav=199480.90,
        deploy_status="FAILED",
        health_status="SUCCESS",
        timestamp="2026-11-06T18:50:16",
    )
    # `selection` must pick the same-date success even though a NEWER instance exists.
    selection = _selection(tmp_path)

    assert selection.source["path"] == os.path.join(earlier_success, "account_state.json")
    assert selection.source["instance"] == "184826"


def test_all_dates_failed_falls_back_to_the_older_successful_date(tmp_path):
    """When the newest date has NO admissible instance, the older date is used (not "none")."""
    older = _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78)
    _snapshot(tmp_path, "2026-11-06", "184826", nav=186476.57, deploy_status="FAILED")

    selection = _selection(tmp_path)

    assert selection.source["path"] == os.path.join(older, "account_state.json")
    assert selection.source["run_date"] == "2026-11-05"


# ---------------------------------------------------------------------------
# Rule 2: the orphan signature and the version axis
# ---------------------------------------------------------------------------


def test_orphan_snapshot_whose_as_of_is_an_older_date_is_skipped(tmp_path):
    """The 11-08 orphan: an 11-05 book copied into a 11-08 directory with a SUCCESS summary.

    Directory freshness is NOT evidence of a settlement; the state's own `as_of_date` must
    equal its run date, otherwise the copied book is skipped (2026-11-08/184755 signature).
    """
    older = _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78)
    _snapshot(tmp_path, "2026-11-08", "184755", as_of="2026-11-05", nav=208830.78)

    selection = _selection(tmp_path)

    assert selection.source["path"] == os.path.join(older, "account_state.json")
    skipped = {s["instance"]: s for s in selection.source["skipped"]}
    assert skipped["184755"]["reason"] == SKIP_AS_OF_MISMATCH


def test_base_version_mismatch_is_skipped_and_not_silently_used(tmp_path):
    """A stale-engine snapshot (1.8.0 vs today's 2.0.0) must lose to a matching one."""
    _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78, base_version="1.8.0")
    good = _snapshot(tmp_path, "2026-11-05", "210000", nav=209000.00, base_version="2.0.0")

    selection = _selection(tmp_path)

    assert selection.source["path"] == os.path.join(good, "account_state.json")
    skipped = {s["instance"]: s for s in selection.source["skipped"]}
    assert skipped["202418"]["reason"] == SKIP_BASE_VERSION_MISMATCH


# ---------------------------------------------------------------------------
# Rule 2, the version axis: SAME-MAJOR tolerance (`NV8`, 2026-11-17 owner decision A)
# ---------------------------------------------------------------------------
# THE DEFECT PINNED HERE. The comparison used to be EXACT STRING EQUALITY, so any
# `BASE_VERSION` patch/minor bump made the previous session's snapshot ineligible and
# COLD-STARTED the book: `entry_prices` / `holding_bars` / cooldown state were lost and the
# `-8%` stop-loss lost its reference anchor. The archive already holds that failure mode as
# `LiveDumpPreflightError base_version` deploy failures (2026-11-06 / 2026-11-09) and a
# `strategy_version` one (2026-11-12), so this is not hypothetical.
#
# The rule is now "same engine ERA" = same MAJOR component. Every other branch is pinned
# right next to the tolerance -- different major, missing, blank and unparsable -- so the
# tolerance cannot be widened into "always admissible" without a red test.


def _single_candidate(tmp_path, recorded, expected):
    """One flat snapshot carrying `recorded` as `base_version`, resolved against `expected`."""
    _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90, base_version=recorded)
    selection = _selection(tmp_path, expected=expected)
    (candidate,) = selection.candidates
    return selection, candidate


@pytest.mark.parametrize(
    "recorded, expected",
    [
        ("2.0.0", "2.1.0"),  # patch differs -> same era, admissible
        ("2.1.0", "2.3.4"),  # minor differs -> same era, admissible
        ("2.4.0", "2.0.0"),  # the post-bump shape: OLD snapshot, NEW live engine
    ],
)
def test_same_major_bump_keeps_the_snapshot_admissible(tmp_path, recorded, expected):
    selection, candidate = _single_candidate(tmp_path, recorded, expected)

    assert candidate.eligible is True
    assert candidate.reason == "eligible"
    assert selection.state is not None
    assert selection.source["base_version"] == recorded
    # The snapshot is adopted verbatim: its recorded version is NOT re-stamped.
    assert selection.state["base_version"] == recorded
    assert selection.source["skipped"] == []


@pytest.mark.parametrize(
    "recorded, expected",
    [
        ("2.9.9", "3.0.0"),  # old major -> live major
        ("3.0.0", "2.9.9"),  # and the reverse, so the rule is symmetric
    ],
)
def test_different_major_is_still_refused_with_the_original_reason(
    tmp_path, recorded, expected
):
    """The guard this task must NOT weaken: a different major is a different engine era."""
    selection, candidate = _single_candidate(tmp_path, recorded, expected)

    assert candidate.eligible is False
    assert candidate.reason == SKIP_BASE_VERSION_MISMATCH
    assert selection.state is None
    assert selection.source is None
    assert "reason=no_admissible_candidate" in selection.log_line


def test_missing_recorded_version_keeps_its_own_reason(tmp_path):
    """`base_version: null` stays INADMISSIBLE -- never "no constraint" and never compatible."""
    selection, candidate = _single_candidate(tmp_path, None, "2.0.0")

    assert candidate.eligible is False
    assert candidate.reason == SKIP_BASE_VERSION_MISSING
    assert selection.state is None


def test_blank_recorded_version_keeps_its_own_reason(tmp_path):
    """`""` is the dumper's other spelling of "no value": MISSING, not "compatible"."""
    selection, candidate = _single_candidate(tmp_path, "", "2.0.0")

    assert candidate.eligible is False
    assert candidate.reason == SKIP_BASE_VERSION_MISSING
    assert selection.state is None


@pytest.mark.parametrize("garbage", ["abc", "2.x", "v2.0.0", "2..0", "2.0.0.0.x"])
def test_unparsable_recorded_version_fails_closed(tmp_path, garbage):
    """A garbage string must never become compatible.

    The reason stays `base_version_mismatch`: the skip-reason vocabulary deliberately has no
    `malformed` code (adding one would change the meaning existing archives were audited
    under), so a fail-closed rejection reuses the mismatch reason.
    """
    selection, candidate = _single_candidate(tmp_path, garbage, "2.0.0")

    assert candidate.eligible is False
    assert candidate.reason == SKIP_BASE_VERSION_MISMATCH
    assert selection.state is None


@pytest.mark.parametrize("recorded", ["1.8.0", "2.0.0", "2.7.3", "", "abc", None])
def test_no_expected_version_means_no_version_constraint(tmp_path, recorded):
    """`expected_base_version=None` is unchanged: no version filter at all.

    That is the web layer's default (it has no authoritative live value to offer) and it must
    stay permissive even for a record today's engine could not parse -- the fail-closed side
    of that path is Stage 2's exact parity gate, not this resolver.
    """
    selection, candidate = _single_candidate(tmp_path, recorded, None)

    assert candidate.eligible is True
    assert selection.state is not None
    assert selection.source["skipped"] == []


def test_the_major_helper_is_never_unconditionally_true():
    """Guard: the tolerance may not be "simplified" into a constant True."""
    assert _version_majors_match("2.9.9", "3.0.0") is False
    assert _version_majors_match("3.0.0", "2.9.9") is False
    assert _version_majors_match("2.0.0", None) is False
    assert _version_majors_match(None, "2.0.0") is False
    assert _version_majors_match(None, None) is False
    assert _version_majors_match("2.0.0", "abc") is False
    assert _version_majors_match("abc", "2.0.0") is False
    assert _version_majors_match("2.0.0", "2.0.0") is True
    assert _version_majors_match("2.0.0", "2.99.1") is True


def test_candidates_come_from_instance_dirs_not_only_date_root_files(tmp_path):
    """No implicit "date-root-level only" fallback: instance subdirs are ordinary candidates."""
    _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)
    session_root = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)
    candidates = list_prior_state_candidates(session_root, "2026-11-09", expected_base_version="2.0.0")

    assert [c.instance for c in candidates] == ["185016"]
    assert candidates[0].eligible is True


def test_today_and_future_dates_are_never_prior_state(tmp_path):
    """`prior_state` is the PREVIOUS session: today's own dirs and the future are excluded."""
    _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)
    _snapshot(tmp_path, "2026-11-09", "204956", nav=197899.60)
    _snapshot(tmp_path, "2026-11-10", "202644", nav=198000.00)

    selection = _selection(tmp_path, as_of="2026-11-09")

    assert selection.source["run_date"] == "2026-11-06"


# ---------------------------------------------------------------------------
# Rule 3/4 applied to the state dict + auditability
# ---------------------------------------------------------------------------


def test_apply_replaces_a_stale_prior_state_and_records_the_source(tmp_path):
    """The 11-09 shape: carried prior_state=11-05, newest successful snapshot=11-06."""
    _snapshot(tmp_path, "2026-11-05", "202418", nav=208830.78)
    _snapshot(tmp_path, "2026-11-06", "184826", nav=186476.57, deploy_status="FAILED")
    success_dir = _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)

    state = {
        "as_of_date": "2026-11-09",
        "current_nav": 197899.60,
        "prior_state": {"as_of_date": "2026-11-05", "current_nav": 208830.78},
    }
    log_line = apply_prior_state_selection(state, _selection(tmp_path))

    assert state["prior_state"]["as_of_date"] == "2026-11-06"
    assert state["prior_state"]["current_nav"] == pytest.approx(199480.90)
    source = state["prior_state_source"]
    assert source["path"] == os.path.join(success_dir, "account_state.json")
    assert source["as_of"] == "2026-11-06"
    assert source["run_status"] == "SUCCESS"
    assert source["selection_reason"] == "carried_prior_state_skipped_a_newer_admissible_snapshot"
    assert source["carried_prior_state_as_of"] == "2026-11-05"
    assert {s["reason"] for s in source["skipped"]} == {SKIP_STATUS_NOT_SUCCESS}
    assert log_line.startswith(PRIOR_STATE_LOG_PREFIX)
    assert f"path={os.path.join(success_dir, 'account_state.json')}" in log_line
    assert "action=replaced" in log_line
    assert "184826:run_status_not_success" in log_line


def test_apply_keeps_an_already_correct_prior_state(tmp_path):
    """No churn when the carried block IS the newest admissible snapshot."""
    _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)
    state = {
        "as_of_date": "2026-11-09",
        "prior_state": {"as_of_date": "2026-11-06", "current_nav": 199480.90},
    }
    original = dict(state["prior_state"])

    log_line = apply_prior_state_selection(state, _selection(tmp_path))

    assert state["prior_state"] == original
    assert state["prior_state_source"]["selection_reason"] == (
        "carried_prior_state_is_the_newest_admissible_snapshot"
    )
    assert "action=kept" in log_line


def test_apply_never_destroys_state_when_nothing_is_admissible(tmp_path):
    """No candidate -> log the unresolved verdict, leave `prior_state` byte-identical."""
    state = {"as_of_date": "2026-11-09", "prior_state": {"as_of_date": "2026-11-05"}}
    before = json.dumps(state, sort_keys=True)

    log_line = apply_prior_state_selection(state, _selection(tmp_path, as_of="2026-11-01"))

    assert json.dumps(state, sort_keys=True) == before
    assert log_line.startswith(PRIOR_STATE_LOG_PREFIX)
    assert "unresolved" in log_line


def test_apply_fills_a_missing_prior_state(tmp_path):
    _snapshot(tmp_path, "2026-11-06", "185016", nav=199480.90)
    state = {"as_of_date": "2026-11-09"}

    apply_prior_state_selection(state, _selection(tmp_path))

    assert state["prior_state"]["as_of_date"] == "2026-11-06"
    assert state["prior_state_source"]["selection_reason"] == "carried_prior_state_missing"


# ---------------------------------------------------------------------------
# Archive-shape derivation (no guessing for a non-archive output dir)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output_dir, expected_suffix",
    [
        ("/a/b/u/2026-11-09/204956", "/a/b/u"),
        ("/a/b/u/2026-11-09", "/a/b/u"),
        ("/a/b/u/2026-11-09/", "/a/b/u"),
    ],
)
def test_derive_session_root_recognises_the_archive_layout(output_dir, expected_suffix):
    assert derive_session_root(output_dir) == expected_suffix


@pytest.mark.parametrize(
    "output_dir",
    ["/repo/scripts/results/live/chan_four_state_blend", "/tmp/whatever/2026-11-09-extra", None],
)
def test_derive_session_root_refuses_to_guess(output_dir):
    """The CLI default (`scripts/results/live/<key>`) is NOT an archive: resolver no-ops."""
    assert derive_session_root(output_dir) is None


# ---------------------------------------------------------------------------
# Wiring: the resolution lands in the `account_state.json` a real run writes
# ---------------------------------------------------------------------------


def test_run_live_strategy_records_prior_state_source(tmp_path, monkeypatch):
    """End-to-end (offline, synthetic): one `main()` run against an archive-shaped output dir.

    The written `account_state.json` must carry BOTH the corrected `prior_state` (the newest
    successful snapshot) and the audit block naming the file it came from.
    """
    import sys as _sys

    from scripts.run_live_strategy import main

    archive = os.path.join(str(tmp_path), _ARCHIVE, _STRAT, _UNIV)
    _snapshot(tmp_path, "2024-05-08", "090000", nav=100500.0, base_version=None)
    _snapshot(
        tmp_path,
        "2024-05-09",
        "091500",
        nav=101000.0,
        base_version=None,
        deploy_status="FAILED",
    )
    newest = _snapshot(tmp_path, "2024-05-09", "100000", nav=101800.0, base_version=None)

    out_dir = os.path.join(archive, "2024-05-10", "110000")
    os.makedirs(out_dir, exist_ok=True)
    # Seed the carried state with the OLDER snapshot as `prior_state` (the defect shape).
    with open(os.path.join(out_dir, "account_state.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "as_of_date": "2024-05-10",
                "current_nav": 102000.0,
                "prior_state": {"as_of_date": "2024-05-08", "current_nav": 100500.0},
            },
            f,
        )

    universe_file = os.path.join(str(tmp_path), "univ.txt")
    with open(universe_file, "w", encoding="utf-8") as f:
        f.write("600000.SH\n")
    holdings_file = os.path.join(str(tmp_path), "holdings.json")
    with open(holdings_file, "w", encoding="utf-8") as f:
        json.dump({"600000.SH": 1000}, f)

    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "run_live_strategy.py",
            "--strategy", "equal_weight",
            "--universe-file", universe_file,
            "--current-holdings-file", holdings_file,
            "--account-state-file", os.path.join(out_dir, "account_state.json"),
            "--output-dir", out_dir,
            "--as-of-date", "2024-05-10",
            "--start-date", "2024-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", "100000",
        ],
    )

    main()

    with open(os.path.join(out_dir, "account_state.json"), "r", encoding="utf-8") as f:
        state = json.load(f)
    source = state["prior_state_source"]
    assert source["path"] == os.path.join(str(newest), "account_state.json")
    assert source["as_of"] == "2024-05-09"
    assert source["run_status"] == "SUCCESS"
    assert source["selection_reason"] == "carried_prior_state_skipped_a_newer_admissible_snapshot"
    assert state["prior_state"]["as_of_date"] == "2024-05-09"
    assert state["prior_state"]["current_nav"] == pytest.approx(101800.0)

    with open(os.path.join(out_dir, "summary.json"), "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["prior_state_log"].startswith(PRIOR_STATE_LOG_PREFIX)
    assert "action=replaced" in summary["prior_state_log"]
    # The FAILED same-day sibling is on the record, with its reason.
    assert source["skipped"][0]["run_status"] == "FAILED"
    assert source["skipped"][0]["reason"] == SKIP_STATUS_NOT_SUCCESS


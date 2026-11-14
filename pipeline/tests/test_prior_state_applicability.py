"""`D3` follow-up: an absent `prior_state_source` must say WHY it is absent.

THE DEFECT UNDER TEST
---------------------
`prior_state` is "the previous session's CONFIRMED snapshot". It is resolved by
`scripts/live_preflight.select_prior_state`, and the resolution that succeeded records its
provenance as a DICT in `account_state["prior_state_source"]`.

When nothing was selected, NOTHING was recorded -- the field was simply absent, which reads
exactly like `null`. Two completely different facts therefore had the same shape:

* the resolver NEVER LOOKED, because `derive_session_root` refused the output directory. A
  FLAT output dir (`scripts/results/live/<strategy_key>`, the CLI default) is not the
  `<root>/<strategy>/<universe>/<date>` archive shape, so "which snapshot should this resume
  from?" does not apply at all;
* the resolver LOOKED and found nothing admissible -- `select_prior_state` logs
  `reason=no_candidate_directories_under_session_root` / `no_admissible_candidate`.

`docs/lv7_prior_state_contract.md`-style auditing cannot tell those apart from the archive,
and the only hint was a `[PRIOR STATE] unresolved ...` line inside `prior_state_log`.

THE SHAPE PINNED HERE (three distinguishable states, never collapsed)
--------------------------------------------------------------------
* ``prior_state_source="not_applicable"`` + ``prior_state_source_reason="flat_run_dir"`` --
  the resolver did not look (flat/non-archive output dir). Nothing is wrong with the archive;
  the question does not apply.
* ``prior_state_source="unresolved"`` + ``prior_state_source_reason="resolver_error"`` --
  the resolver ran and RAISED; the carried state was kept and the failure is now in-band.
* NO marker at all -- the resolver looked and found no admissible candidate. Pre-existing
  shape, unchanged on purpose (`select_prior_state`'s log line still names the reason).

Plus the non-destructiveness rule: a real provenance DICT in `prior_state_source` is never
overwritten by a marker. Losing it would re-create the D2 blind spot.

100% offline: `tmp_path` only, synthetic provider, no network, no `data/runs/**`.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from scripts.run_live_strategy import (
    PRIOR_STATE_NOT_APPLICABLE_FLAT_RUN_DIR,
    PRIOR_STATE_SOURCE_NOT_APPLICABLE,
    PRIOR_STATE_SOURCE_UNRESOLVED,
    PRIOR_STATE_UNRESOLVED_RESOLVER_ERROR,
)

_STRAT = "equal_weight"
_UNIV = "custom_universe"
_AS_OF = "2024-05-10"
_SYMBOL = "600000.SH"


# --------------------------------------------------------------------------- #
# Harness: one REAL `main()` run (only the files it reads are fixtures)
# --------------------------------------------------------------------------- #
def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _snapshot(root, run_date, instance, *, nav=100500.0, deploy_status="SUCCESS"):
    """One archived prior round: `<root>/<strat>/<univ>/<date>/<HHMMSS>/`."""
    run_dir = os.path.join(str(root), _STRAT, _UNIV, run_date, instance)
    _write_json(
        os.path.join(run_dir, "account_state.json"),
        {
            "as_of_date": run_date,
            "current_nav": nav,
            "strategy_version": "1.2.2",
            "nav_history_10d": [nav] * 8,
        },
    )
    _write_json(
        os.path.join(run_dir, "summary.json"),
        {
            "date": run_date,
            "timestamp": f"{run_date}T{instance}",
            "deploy_status": deploy_status,
        },
    )
    return run_dir


def _run_main(tmp_path, output_dir, monkeypatch, account_state_file=None):
    """One REAL `run_live_strategy.main()` against `output_dir`; returns the written state."""
    from scripts.run_live_strategy import main

    universe_file = os.path.join(str(tmp_path), "univ.txt")
    with open(universe_file, "w", encoding="utf-8") as f:
        f.write(_SYMBOL + "\n")
    holdings_file = os.path.join(str(tmp_path), "holdings.json")
    _write_json(holdings_file, {_SYMBOL: 1000})
    state_file = account_state_file or os.path.join(output_dir, "account_state.json")
    os.makedirs(output_dir, exist_ok=True)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_strategy.py",
            "--strategy", _STRAT,
            "--universe-file", universe_file,
            "--current-holdings-file", holdings_file,
            "--account-state-file", state_file,
            "--output-dir", output_dir,
            "--as-of-date", _AS_OF,
            "--start-date", "2024-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", "100000",
        ],
    )

    main()
    return _read_json(state_file), _read_json(os.path.join(output_dir, "summary.json"))


# --------------------------------------------------------------------------- #
# 1. Flat output dir => explicit "not applicable", never a bare absence
# --------------------------------------------------------------------------- #
def test_flat_run_dir_stamps_an_explicit_not_applicable_marker(tmp_path, monkeypatch):
    """The CLI default layout (`scripts/results/live/<strategy_key>`) is not an archive.

    The resolver therefore never looks, and that fact is now WRITTEN, not implied by a
    missing key: `prior_state_source="not_applicable"`,
    `prior_state_source_reason="flat_run_dir"`.
    """
    out_dir = os.path.join(str(tmp_path), "scripts", "results", "live", _STRAT)

    state, summary = _run_main(tmp_path, out_dir, monkeypatch)

    assert state["prior_state_source"] == PRIOR_STATE_SOURCE_NOT_APPLICABLE
    assert state["prior_state_source_reason"] == PRIOR_STATE_NOT_APPLICABLE_FLAT_RUN_DIR
    # The pre-existing console/summary log is KEPT, and now names the marker too, so
    # "did we not look?" is answerable from `summary.json` as well.
    log = summary["prior_state_log"]
    assert log.startswith("[PRIOR STATE]")
    assert f"prior_state_source={PRIOR_STATE_SOURCE_NOT_APPLICABLE}" in log
    assert f"reason={PRIOR_STATE_NOT_APPLICABLE_FLAT_RUN_DIR}" in log


def test_flat_run_dir_never_overwrites_a_real_provenance_block(tmp_path, monkeypatch):
    """Non-destructive: a carried dict names the snapshot the book came from. Keep it."""
    out_dir = os.path.join(str(tmp_path), "scripts", "results", "live", _STRAT)
    os.makedirs(out_dir, exist_ok=True)
    carried_source = {
        "path": "/archive/2024-05-09/100000/account_state.json",
        "as_of": "2024-05-09",
        "selection_reason": "newest_successful_snapshot_at_or_before_as_of",
    }
    state_file = os.path.join(out_dir, "account_state.json")
    _write_json(
        state_file,
        {"as_of_date": _AS_OF, "current_nav": 100000.0, "prior_state_source": carried_source},
    )

    state, _summary = _run_main(tmp_path, out_dir, monkeypatch, account_state_file=state_file)

    assert state["prior_state_source"] == carried_source, (
        "a resolved provenance block must survive a round that could not resolve"
    )
    assert state["prior_state_source_reason"] == PRIOR_STATE_NOT_APPLICABLE_FLAT_RUN_DIR


# --------------------------------------------------------------------------- #
# 2. Nested (archive-shaped) dir => selection behaviour byte-identical
# --------------------------------------------------------------------------- #
def test_nested_archive_dir_keeps_the_resolved_selection_unchanged(tmp_path, monkeypatch):
    """An archive-shaped dir still records the resolver's DICT, with no applicability key."""
    out_dir = os.path.join(str(tmp_path), _STRAT, _UNIV, _AS_OF)
    prior = _snapshot(tmp_path, "2024-05-09", "100000", nav=101800.0)

    state, _summary = _run_main(tmp_path, out_dir, monkeypatch)

    source = state["prior_state_source"]
    assert isinstance(source, dict), source
    assert source["path"] == os.path.join(prior, "account_state.json")
    assert source["as_of"] == "2024-05-09"
    assert source["selection_reason"] == "carried_prior_state_missing"
    assert "prior_state_source_reason" not in state, (
        "a resolved selection has no applicability reason to record"
    )
    assert state["prior_state"]["as_of_date"] == "2024-05-09"


def test_nested_archive_dir_with_no_candidates_records_no_marker(tmp_path, monkeypatch):
    """The resolver LOOKED and found nothing: the pre-existing absence is not re-labelled.

    This is the state the defect must keep distinguishable from `not_applicable`: there is a
    real session root, the resolver ran, and the log line names why nothing was selected.
    """
    out_dir = os.path.join(str(tmp_path), _STRAT, _UNIV, _AS_OF)

    state, summary = _run_main(tmp_path, out_dir, monkeypatch)

    assert "prior_state_source" not in state
    assert "prior_state_source_reason" not in state
    assert "reason=no_candidate_directories_under_session_root" in summary["prior_state_log"]


# --------------------------------------------------------------------------- #
# 3. Resolver FAILURE => distinguishable from "not applicable"
# --------------------------------------------------------------------------- #
def test_resolver_error_is_marked_unresolved_and_keeps_the_carried_state(tmp_path, monkeypatch):
    """A raising resolver is a defect, not a shape mismatch: `unresolved`/`resolver_error`."""
    import scripts.run_live_strategy as rls

    out_dir = os.path.join(str(tmp_path), "scripts", "results", "live", _STRAT)
    os.makedirs(out_dir, exist_ok=True)
    state_file = os.path.join(out_dir, "account_state.json")
    _write_json(
        state_file,
        {
            "as_of_date": _AS_OF,
            "current_nav": 100000.0,
            "prior_state": {"as_of_date": "2024-05-08", "current_nav": 100100.0},
        },
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("archive unreadable")

    monkeypatch.setattr(rls, "select_prior_state", _boom)

    state, _summary = _run_main(tmp_path, out_dir, monkeypatch, account_state_file=state_file)

    assert state["prior_state_source"] == PRIOR_STATE_SOURCE_UNRESOLVED
    assert state["prior_state_source_reason"] == PRIOR_STATE_UNRESOLVED_RESOLVER_ERROR
    assert state["prior_state_source"] != PRIOR_STATE_SOURCE_NOT_APPLICABLE, (
        "'we tried and failed' must not be reported as 'the question does not apply'"
    )
    # "a resolver failure must not eat the carried state" -- unchanged invariant (the
    # persistence block only ADDS missing lifecycle/streak keys to `prior_state`, so the
    # snapshot identity is asserted on the fields that carry it).
    assert state["prior_state"]["as_of_date"] == "2024-05-08"
    assert state["prior_state"]["current_nav"] == 100100.0

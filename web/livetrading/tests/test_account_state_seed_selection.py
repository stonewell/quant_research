"""Seed selection + fail-before-side-effects gates for the 2026-11-08/184755 incident.

FORENSIC CONTEXT (why this file exists)
---------------------------------------
The operator ran the Stage-1 -> Stage-2 live chain with ``--as-of-date 2026-11-08``, a
Sunday.  The market-clock gate correctly refused it -- but only inside the Stage-1
CHILD, i.e. AFTER ``ExecutionService.run_health_check`` had already:

* allocated ``<archive>/<strategy>/<universe>/2026-11-08/184755/``, and
* seeded that instance with ``latest_account_state_at_or_before("2026-11-08")``.

The rejection therefore left an ORPHAN instance: a copied 2026-11-05 book
(``base_version=1.8.0``) plus a ``summary.json`` whose ``health_status`` is
``"FAILED"`` (``error_kind="as_of_not_honourable"``), and no ``account_state.json``
version stamping on top of it.  ``latest_account_state_at_or_before`` used to return
the FIRST existing ``account_state.json`` walking dates/instances newest-first, so the
orphan beat the healthy 2026-11-06 dirs (which held the real ``2.0.0`` state) and
Stage 2 refused with ``LiveDumpPreflightError`` (1.8.0 vs 2.0.0).

The fix has two independent halves, both pinned here:

1. ``storage._instance_state_is_trustworthy`` -- a run dir whose ``summary.json``
   records a non-``SUCCESS`` ``health_status`` (or a refusal without any health status)
   is not a seed source.  Legacy dirs with no summary, and healthy-but-undeployed
   rounds (``health_status="SUCCESS"`` / ``deploy_status="FAILED"``) MUST still seed.
   This changes WHICH instance is seeded, never the version stamping: the fail-closed
   ``scripts/live_preflight.py::assert_account_state_version_parity`` contract is
   untouched.
2. ``ExecutionService._assert_explicit_as_of_is_trading_session`` -- a pinned as-of
   that is not a trading session is refused BEFORE ``get_run_dir`` /
   ``_allocate_stage1_run_dir`` / ``os.makedirs`` / the ``account_state.json`` seed, so
   no orphan instance (and no half-built archive entry) can be created at all.

Everything is offline: the holiday calendar is local data, ``subprocess.run`` is
monkeypatched so no live runner is ever launched, and the package ``conftest.py``
already re-points every ``LIVETRADING_*`` directory at the test's ``tmp_path``.
"""

import inspect
import json
import os
import subprocess
import sys

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import storage as storage_mod  # noqa: E402
from execution_service import ExecutionService  # noqa: E402
from storage import StorageManager, _instance_state_is_trustworthy  # noqa: E402

_STRATEGY = "strat_x"
_UNIVERSE = "univ_y"
_DATE = "2026-11-09"
#: A newer date whose only instance predates `summary.json` (the legacy layout).
_LEGACY_DATE = "2026-11-10"
#: The incident's exact (non-trading) pinned as-of: a Sunday.
_SUNDAY = "2026-11-08"
#: The previous trading session the incident should have been run against.
_PREV_SESSION = "2026-11-06"


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


def _make_instance(
    storage,
    date_str,
    stamp,
    *,
    state,
    summary=None,
    write_summary=True,
    ticket=True,
):
    """Materialise one archived instance dir under ``<archive>/<strat>/<univ>/<date>/<stamp>``."""
    run_dir = os.path.join(storage.archive_dir, _STRATEGY, _UNIVERSE, date_str, stamp)
    os.makedirs(run_dir, exist_ok=True)
    if write_summary:
        payload = {
            "date": date_str,
            "as_of": date_str,
            "strategy_key": _STRATEGY,
            "universe_key": _UNIVERSE,
        }
        payload.update(summary or {})
        _write_json(os.path.join(run_dir, "summary.json"), payload)
    if ticket:
        # A Stage-2 artifact, so `list_runs` lists this dir even without a summary
        # (the legacy archives this predicate must stay compatible with).
        _write_json(os.path.join(run_dir, "live_trading_ticket.json"), [])
    if state is not None:
        _write_json(os.path.join(run_dir, "account_state.json"), state)
    return run_dir


def _seed(storage, date_str):
    return storage.latest_account_state_at_or_before(
        date_str, strategy_key=_STRATEGY, universe_key=_UNIVERSE
    )


# --------------------------------------------------------------------------- #
# 1. A failed round must never win the seed walk
# --------------------------------------------------------------------------- #
def test_failed_newest_instance_is_skipped_for_the_older_healthy_one(temp_storage):
    """THE incident: the newest dir is an orphan whose round never completed Stage 1.

    Pre-fix, `latest_account_state_at_or_before` returned the FAILED instance's copied
    `base_version=1.8.0` state and Stage 2 refused (1.8.0 vs 2.0.0).  The seed must come
    from the last round that actually ran successfully instead.
    """
    _make_instance(
        temp_storage,
        _DATE,
        "090000",
        state={"note": "HEALTHY 2.0.0", "base_version": "2.0.0"},
        summary={"timestamp": f"{_DATE}T01:00:00+00:00", "health_status": "SUCCESS"},
    )
    orphan = _make_instance(
        temp_storage,
        _DATE,
        "184755",
        state={"note": "ORPHAN 1.8.0", "base_version": "1.8.0"},
        summary={
            "timestamp": f"{_DATE}T02:47:59+00:00",
            "health_status": "FAILED",
            "error_kind": "as_of_not_honourable",
        },
    )

    assert _instance_state_is_trustworthy(orphan) is False
    state = _seed(temp_storage, _DATE)

    assert state is not None
    assert state["note"] == "HEALTHY 2.0.0"
    assert state["base_version"] == "2.0.0"


def test_failed_instance_without_health_status_is_also_skipped(temp_storage):
    """A refusal recorded only through `error_kind` / `error_headline` is not healthy either."""
    _make_instance(
        temp_storage,
        _DATE,
        "090000",
        state={"note": "HEALTHY"},
        summary={"timestamp": f"{_DATE}T01:00:00+00:00"},
    )
    refused = _make_instance(
        temp_storage,
        _DATE,
        "120000",
        state={"note": "REFUSED ROUND"},
        summary={
            "timestamp": f"{_DATE}T02:00:00+00:00",
            "error_kind": "as_of_not_honourable",
            "error_headline": "估值日被拒",
        },
    )

    assert _instance_state_is_trustworthy(refused) is False
    assert _seed(temp_storage, _DATE)["note"] == "HEALTHY"


# --------------------------------------------------------------------------- #
# 2. Legacy archives (no summary.json at all) keep seeding
# --------------------------------------------------------------------------- #
def test_newest_instance_without_any_summary_is_still_returned(temp_storage):
    """A pre-summary instance carries no verdict -- "unknown" must not mean "failed".

    The newest DATE is a legacy instance dir (a ticket + state, no `summary.json` at
    all).  If the new predicate treated "no summary" as "no healthy Stage 1", this walk
    would fall through to the previous session's state -- silently rewinding the book.
    """
    _make_instance(
        temp_storage,
        _PREV_SESSION,
        "090000",
        state={"note": "PREVIOUS SESSION"},
        summary={
            "timestamp": f"{_PREV_SESSION}T01:00:00+00:00",
            "health_status": "SUCCESS",
        },
    )
    legacy = _make_instance(
        temp_storage, _LEGACY_DATE, "120000", state={"note": "LEGACY NEWEST"}, write_summary=False
    )

    assert not os.path.exists(os.path.join(legacy, "summary.json"))
    assert _instance_state_is_trustworthy(legacy) is True
    assert _seed(temp_storage, _LEGACY_DATE)["note"] == "LEGACY NEWEST"


# --------------------------------------------------------------------------- #
# 3. A healthy Stage-1 round that failed to DEPLOY still owns the state
# --------------------------------------------------------------------------- #
def test_successful_health_with_failed_deploy_is_still_returned(temp_storage):
    """`deploy_status` is deliberately ignored: the lifecycle/streak state is real."""
    _make_instance(
        temp_storage,
        _DATE,
        "090000",
        state={"note": "OLDER"},
        summary={"timestamp": f"{_DATE}T01:00:00+00:00", "health_status": "SUCCESS"},
    )
    healthy_undeployed = _make_instance(
        temp_storage,
        _DATE,
        "120000",
        state={"note": "HEALTHY BUT UNDEPLOYED"},
        summary={
            "timestamp": f"{_DATE}T02:00:00+00:00",
            "health_status": "SUCCESS",
            "deploy_status": "FAILED",
        },
    )

    assert _instance_state_is_trustworthy(healthy_undeployed) is True
    assert _seed(temp_storage, _DATE)["note"] == "HEALTHY BUT UNDEPLOYED"


# --------------------------------------------------------------------------- #
# 4. A pinned non-trading as-of must fail BEFORE any archive side effect
# --------------------------------------------------------------------------- #
def _patch_subprocess(monkeypatch):
    """Fail the test if ANY child process is launched (defence in depth)."""
    launched = []

    class _Never:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, *args, **kwargs):  # pragma: no cover - only on regression
        launched.append(list(cmd))
        return _Never()

    monkeypatch.setattr(subprocess, "run", _run)
    return launched


def _date_dir(storage, date_str):
    return os.path.join(storage.archive_dir, _STRATEGY, _UNIVERSE, date_str)


def _assert_no_archive_entry(storage, date_str):
    date_dir = _date_dir(storage, date_str)
    assert not os.path.exists(date_dir), (
        f"a refused as-of must leave NO date dir behind, found {date_dir}"
    )


def test_explicit_sunday_as_of_is_refused_before_allocating_or_seeding(service, temp_storage, monkeypatch):
    """THE incident, end to end through the real entry point.

    Before the fix this call created `2026-11-08/184755/` with a copied state file and a
    FAILED summary, because the calendar was only checked in the child.  Now the refusal
    is decided in the web layer, before `get_run_dir` / `_allocate_stage1_run_dir` /
    `os.makedirs` / the `account_state.json` seed.
    """
    launched = _patch_subprocess(monkeypatch)
    strat_file = _write_json(
        os.path.join(temp_storage.data_dir, "dummy_strategy.json"),
        {"template_name": "dummy", "params": {}},
    )

    with pytest.raises(ValueError) as excinfo:
        service.run_health_check(
            strategy_file=strat_file,
            custom_symbols=["600519.SH"],
            as_of_date=_SUNDAY,
            portfolio_value=100000.0,
            strategy_key=_STRATEGY,
            universe_key=_UNIVERSE,
        )

    message = str(excinfo.value)
    assert _SUNDAY in message
    assert "不是 china 的交易日" in message
    assert "估值日必须是交易日" in message
    assert "归档未被改动" in message
    assert launched == [], "no child may be launched for a refused as-of"
    _assert_no_archive_entry(temp_storage, _SUNDAY)


def test_explicit_sunday_as_of_is_refused_before_stage2_run_dir(service, temp_storage, monkeypatch):
    """Stage 2 gets the same fail-before-`get_run_dir` gate (no hand-off is consumed)."""
    launched = _patch_subprocess(monkeypatch)
    strat_file = _write_json(
        os.path.join(temp_storage.data_dir, "dummy_strategy.json"),
        {"template_name": "dummy", "params": {}},
    )

    with pytest.raises(ValueError) as excinfo:
        service.run_live_deploy(
            strategy_file=strat_file,
            custom_symbols=["600519.SH"],
            as_of_date=_SUNDAY,
            portfolio_value=100000.0,
            current_holdings={"600519.SH": 100},
            strategy_key=_STRATEGY,
            universe_key=_UNIVERSE,
        )

    assert "的交易日" in str(excinfo.value)
    assert launched == []
    _assert_no_archive_entry(temp_storage, _SUNDAY)


def test_previous_trading_session_passes_the_gate(service):
    """Discrimination: the gate must refuse ONLY non-sessions, not the honest replay date."""
    # Raises nothing for a real Friday session; the incident's answer was 2026-11-06.
    service._assert_explicit_as_of_is_trading_session(_PREV_SESSION, None, ["600519.SH"])


def _call_line_numbers(func, attr_names):
    """Line numbers of calls to `<anything>.<attr>` inside `func` (comments never count)."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in attr_names:
                lines.append(node.lineno)
    return lines


def _assigned_line_number(func, target_name):
    """Line number of the `target_name = ...` assignment inside `func` (None if absent)."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == target_name:
                    return node.lineno
    return None


def test_gate_runs_before_every_archive_side_effect_in_both_methods():
    """Structural guard: the call sits BEFORE `get_run_dir` / `_allocate_stage1_run_dir`.

    The behavioural tests above can only observe the outcome; this pins the ORDERING that
    makes the outcome true for every future refusal path too.  AST-based, so the prose in
    the surrounding comments (which names those callees) can never satisfy the guard --
    the same reason `pipeline/tests/test_live_market_clock_wiring.py` guards wiring at the
    source level.
    """
    gate_lines = {
        "run_health_check": None,
        "run_live_deploy": None,
    }
    for name, method in (
        ("run_health_check", ExecutionService.run_health_check),
        ("run_live_deploy", ExecutionService.run_live_deploy),
    ):
        gate_hits = _call_line_numbers(method, {"_assert_explicit_as_of_is_trading_session"})
        assert gate_hits, f"{name} must call the trading-session gate"
        gate_at = min(gate_hits)
        gate_lines[name] = gate_at

        side_effects = _call_line_numbers(method, {"get_run_dir", "_allocate_stage1_run_dir"})
        assert side_effects, f"{name}: expected the archive allocation calls to still exist"
        assert min(side_effects) > gate_at, f"{name}: the gate must precede run-dir allocation"

        seed_at = _assigned_line_number(method, "account_state_file")
        assert seed_at is not None, f"{name}: expected the account_state seed block"
        assert gate_at < seed_at, f"{name}: the gate must precede the account_state seed"

    assert gate_lines["run_health_check"] is not None


# --------------------------------------------------------------------------- #
# 5. The predicate itself: tolerant where the summary cannot answer
# --------------------------------------------------------------------------- #
def test_predicate_tolerates_missing_malformed_and_non_object_summaries(tmp_path):
    """An unreadable summary is "no verdict", never "failed" (legacy chains keep working)."""
    assert _instance_state_is_trustworthy(str(tmp_path / "does_not_exist")) is True
    assert _instance_state_is_trustworthy(None) is True

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "summary.json").write_text("{not json", encoding="utf-8")
    assert _instance_state_is_trustworthy(str(bad)) is True

    non_object = tmp_path / "list"
    non_object.mkdir()
    (non_object / "summary.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert _instance_state_is_trustworthy(str(non_object)) is True

    blank_status = tmp_path / "blank"
    blank_status.mkdir()
    (blank_status / "summary.json").write_text(
        json.dumps({"health_status": "  "}), encoding="utf-8"
    )
    assert _instance_state_is_trustworthy(str(blank_status)) is True


def test_predicate_is_case_insensitive_on_success(tmp_path):
    """The child writes uppercase today, but the predicate must not hinge on spelling."""
    lower = tmp_path / "lower"
    lower.mkdir()
    (lower / "summary.json").write_text(
        json.dumps({"health_status": "success"}), encoding="utf-8"
    )
    assert _instance_state_is_trustworthy(str(lower)) is True


def test_version_parity_contract_is_not_implemented_here():
    """Boundary guard: seed selection must NOT grow its own version check.

    The one owner of "state version must match the engine" is
    `scripts/live_preflight.py::assert_account_state_version_parity`; a second copy here
    could disagree with it and silently accept a wrong-version book.  The docstring above
    is allowed to narrate the incident's `base_version`, so only the CODE is inspected.
    """
    import ast
    import textwrap

    source = inspect.getsource(storage_mod._instance_state_is_trustworthy)
    tree = ast.parse(textwrap.dedent(source))
    # Drop the docstring (it narrates the incident's `base_version`) before inspecting.
    func_def = tree.body[0]
    func_def.body = [
        node
        for node in func_def.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    code_names = set()
    for node in ast.walk(func_def):
        if isinstance(node, ast.Name):
            code_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            code_names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            code_names.add(node.value)

    for forbidden in ("base_version", "strategy_version"):
        assert forbidden not in code_names, (
            f"{forbidden} belongs to the preflight parity check, not to seed selection"
        )

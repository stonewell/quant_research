"""L2.62: a same-day rerun must write its OWN instance directory, not merge into one.

FORENSIC CONTEXT (why this file exists)
---------------------------------------
``2026-10-27/summary.json`` in the live archive ended up carrying BOTH
``deploy_status="SUCCESS"`` and ``error_detail="KeyboardInterrupt"``, with a
``deploy_stdout`` (UTC 03:06) **older** than the ``health_stdout`` (UTC 04:31).
Three rounds had written into one directory and the fields were MERGED (the child
runs append via ``common.cli_utils.persist_run_metadata``), so "did the 04:31 run
actually deploy?" had no single answer. Nothing in the archive could be trusted to
describe one round.

The fix is structural: the first run of a session keeps the flat ``<date>/`` layout
(so every pre-existing archive stays readable, untouched), and every later run of
that session gets ``<date>/<HHMMSS>/``. This file pins:

1. **allocation** -- first-run-flat, later-run-instance, same-second idempotency,
   collision handling that keeps the name matchable, and the grey-release switch;
2. **selection** -- newest instance picked LEXICOGRAPHICALLY (never by mtime);
3. **readers** -- ``get_run`` / ``get_holdings`` / ``get_account_state`` walk a
   session's instances newest-first, and legacy flat archives are unaffected;
4. **de-merge** -- a round's summary cannot inherit another round's fields;
5. **wiring** -- Stage 2 reuses Stage 1's directory, a double-click does not
   fragment the archive, a fresh instance is seeded with the session's state,
   ``_derive_anchors`` refuses to treat a SAME-DAY run as a position's open date,
   and a **restart between the two stages** still hands the earlier instance's
   ``stage1_health_report.json`` to the child (or refuses to deploy when that
   report is unreachable) instead of silently dropping the macro gate.

Everything is offline: ``subprocess.run`` is always monkeypatched (no live runner is
ever launched) and ``temp_workspace`` keeps every writable path under the OS temp
root. The clock is injected through the single allocation seam
(``storage.allocate_run_instance_dir``), because two rounds inside one test would
otherwise land in the same second and -- by design -- share one directory.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import storage as storage_mod
from execution_service import (
    SUMMARY_INHERITANCE_WHITELIST,
    ExecutionService,
    Stage1HealthReportMissingError,
    de_merge_run_summary,
)
from server import create_app
from storage import (
    StorageManager,
    allocate_run_instance_dir,
    is_instance_dir_name,
    list_instances_for_date,
    resolve_latest_instance_dir,
)


@pytest.fixture
def temp_workspace():
    """Hermetic workspace (see ``conftest.py``'s autouse hermetic fixture too)."""
    temp_dir = tempfile.mkdtemp()
    data_dir = os.path.join(temp_dir, "data")
    archive_dir = os.path.join(temp_dir, "runs")
    cache_dir = os.path.join(temp_dir, "duckdb_cache")
    settings_dir = os.path.join(temp_dir, "settings")
    for d in (data_dir, archive_dir, cache_dir, settings_dir):
        os.makedirs(d, exist_ok=True)

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=data_dir,
        archive_dir=archive_dir,
        cache_dir=cache_dir,
        settings_dir=settings_dir,
    )
    yield storage, temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def advancing_clock(monkeypatch):
    """Force every allocation to a DISTINCT second, one per call.

    ``allocate_run_instance_dir`` is deliberately same-second idempotent, so a test that
    wants "two rounds" must not rely on wall-clock luck. Patching the module-level
    function (the only place the timestamp is read) leaves the layout rule itself under
    test -- only the clock is pinned. Returns the mutable state so a test can assert the
    stamps were actually distinct.
    """
    real = storage_mod.allocate_run_instance_dir
    state = {"now": dt.datetime(2026, 10, 29, 9, 0, 0), "calls": 0}

    def _wrapper(base_dir, *, now=None, split=None):
        stamp = state["now"]
        state["now"] = stamp + dt.timedelta(seconds=1)
        state["calls"] += 1
        return real(base_dir, now=stamp, split=split)

    monkeypatch.setattr(storage_mod, "allocate_run_instance_dir", _wrapper)
    return state


class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, stdout="", returncode=0):
    """Capture argv and never launch a real process."""
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _MockProc(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _arg_value(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_run(storage, strat, univ, date, holdings=None, instance=None, summary=None):
    """Materialise one archived run dir (`<archive>/<strat>/<univ>/<date>[/<HHMMSS>]`)."""
    base = os.path.join(storage.archive_dir, strat, univ, date)
    run_dir = os.path.join(base, instance) if instance else base
    os.makedirs(run_dir, exist_ok=True)
    payload = {
        "date": date,
        "strategy_key": strat,
        "universe_key": univ,
        "timestamp": f"{date}T09:00:00+00:00",
    }
    payload.update(summary or {})
    _write_json(os.path.join(run_dir, "summary.json"), payload)
    if holdings is not None:
        _write_json(os.path.join(run_dir, "holdings.json"), holdings)
    return run_dir


def _dummy_strategy_file(temp_dir):
    path = os.path.join(temp_dir, "dummy_strategy.json")
    _write_json(path, {"template_name": "dummy", "params": {}})
    return path


# --------------------------------------------------------------------------- #
# 1. Allocation: first run flat, later runs get `<HHMMSS>`
# --------------------------------------------------------------------------- #
def test_first_run_of_a_session_stays_flat_and_the_next_gets_an_instance(tmp_path):
    """The first round must NOT be moved: every existing archive keeps its layout."""
    base = tmp_path / "runs" / "strat" / "univ" / "2026-10-29"

    first = allocate_run_instance_dir(str(base), now=dt.datetime(2026, 10, 29, 9, 15, 0))
    assert first == str(base), "the session's first run stays in the flat <date>/ dir"
    assert os.path.isdir(first)

    second = allocate_run_instance_dir(str(base), now=dt.datetime(2026, 10, 29, 13, 0, 0))
    assert second == os.path.join(str(base), "130000")
    assert os.path.isdir(second)
    # The flat dir is untouched and still a directory, not replaced by the instance.
    assert os.path.isdir(str(base))


def test_same_second_allocation_returns_the_same_directory(tmp_path):
    """A double-click must not fragment one round into two archive entries."""
    base = tmp_path / "runs" / "strat" / "univ" / "2026-10-29"
    os.makedirs(str(base))
    now = dt.datetime(2026, 10, 29, 9, 15, 0)

    a = allocate_run_instance_dir(str(base), now=now)
    b = allocate_run_instance_dir(str(base), now=now)

    assert a == b
    assert len(list_instances_for_date(str(base))) == 1


def test_collision_with_a_preexisting_stamp_advances_and_stays_six_digits(tmp_path):
    """A stamp taken by another process (or a pre-seeded archive) is advanced, not reused.

    The advanced name MUST still match ``^\\d{6}$``: a ``091500_1`` suffix would make the
    newest run invisible to ``resolve_latest_instance_dir``, i.e. readers would silently
    keep serving the older round.
    """
    base = tmp_path / "runs" / "strat" / "univ" / "2026-10-29"
    os.makedirs(base / "091500")  # another process already took this second

    got = allocate_run_instance_dir(str(base), now=dt.datetime(2026, 10, 29, 9, 15, 0))

    assert got != os.path.join(str(base), "091500")
    assert is_instance_dir_name(os.path.basename(got))
    assert resolve_latest_instance_dir(str(base)) == got, "the new round must be the latest"


def test_grey_release_switch_collapses_back_to_the_flat_directory(tmp_path, monkeypatch):
    """``LIVETRADING_RUN_INSTANCE_SPLIT=0`` is the documented rollback path."""
    base = tmp_path / "runs" / "strat" / "univ" / "2026-10-29"
    os.makedirs(str(base))
    monkeypatch.setenv("LIVETRADING_RUN_INSTANCE_SPLIT", "0")

    got = allocate_run_instance_dir(str(base), now=dt.datetime(2026, 10, 29, 9, 15, 0))

    assert got == str(base)
    assert [n for n in os.listdir(str(base)) if is_instance_dir_name(n)] == []


# --------------------------------------------------------------------------- #
# 2. Selection: newest instance, lexicographic, mtime never consulted
# --------------------------------------------------------------------------- #
def test_resolve_latest_instance_dir_picks_the_max_stamp_and_ignores_mtime(tmp_path):
    """A restored/copied archive can carry arbitrary mtimes; the name is the total order."""
    base = tmp_path / "2026-10-29"
    os.makedirs(str(base))
    _write_json(str(base / "summary.json"), {"date": "2026-10-29"})
    for stamp in ("091500", "123000"):
        os.makedirs(str(base / stamp))
    # Make the OLDEST stamp look the newest on disk.
    far_future = 2_000_000_000
    os.utime(str(base / "091500"), (far_future, far_future))

    assert resolve_latest_instance_dir(str(base)) == str(base / "123000")


def test_list_instances_for_date_orders_flat_then_instances_old_to_new(tmp_path):
    base = tmp_path / "2026-10-29"
    os.makedirs(str(base))
    _write_json(str(base / "summary.json"), {"date": "2026-10-29"})
    os.makedirs(str(base / "123000"))
    os.makedirs(str(base / "091500"))

    assert list_instances_for_date(str(base)) == [
        str(base),
        str(base / "091500"),
        str(base / "123000"),
    ]


def test_a_container_with_only_instances_is_not_itself_listed_as_a_run(tmp_path):
    """Only real run dirs are returned; a bare container can never be read as a run."""
    base = tmp_path / "2026-10-29"
    os.makedirs(str(base / "091500"))

    assert list_instances_for_date(str(base)) == [str(base / "091500")]
    assert resolve_latest_instance_dir(str(base)) == str(base / "091500")


def test_flat_only_archive_resolves_to_itself(tmp_path):
    """Pre-L2.62 layout: no instance subdir means the flat dir IS the run."""
    base = tmp_path / "2026-10-22"
    os.makedirs(str(base))

    assert list_instances_for_date(str(base)) == []
    assert resolve_latest_instance_dir(str(base)) == str(base)


# --------------------------------------------------------------------------- #
# 3. Readers walk a session's instances newest-first
# --------------------------------------------------------------------------- #
def test_get_run_resolves_to_the_newest_instance_and_reports_which_one(temp_workspace):
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"AAA": 100})
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"BBB": 200}, instance="091500")
    newest = _make_run(
        storage, "s1", "u1", "2026-10-29", holdings={"CCC": 300}, instance="123000"
    )

    bundle = storage.get_run("2026-10-29", strategy_key="s1", universe_key="u1")

    assert bundle is not None
    assert bundle["instance"] == "123000"
    assert bundle["path"] == newest
    assert bundle["holdings"] == {"CCC": 300}


def test_legacy_flat_run_has_no_instance_and_reads_unchanged(temp_workspace):
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-22", holdings={"AAA": 100})

    bundle = storage.get_run("2026-10-22", strategy_key="s1", universe_key="u1")

    assert bundle is not None
    assert bundle["instance"] is None
    assert bundle["holdings"] == {"AAA": 100}


def test_get_holdings_walks_same_day_instances_newest_first(temp_workspace):
    """A rerun's baseline is the book THIS session started from, not yesterday's."""
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-28", holdings={"OLD": 1})
    # The flat (first) round of the session has no snapshot; the second one does.
    _make_run(storage, "s1", "u1", "2026-10-29")
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"SAME_DAY": 7}, instance="091500")

    assert storage.get_holdings(date_str="2026-10-29", strategy_key="s1", universe_key="u1") == {
        "SAME_DAY": 7
    }


def test_latest_account_state_at_or_before_prefers_the_same_day_instance(temp_workspace):
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-28")
    _write_json(
        os.path.join(storage.archive_dir, "s1", "u1", "2026-10-28", "account_state.json"),
        {"as_of_date": "2026-10-28", "tier1_consecutive_bars": 0},
    )
    _make_run(storage, "s1", "u1", "2026-10-29", instance="091500")
    _write_json(
        os.path.join(
            storage.archive_dir, "s1", "u1", "2026-10-29", "091500", "account_state.json"
        ),
        {"as_of_date": "2026-10-29", "tier1_consecutive_bars": 1},
    )

    state = storage.latest_account_state_at_or_before("2026-10-29", "s1", "u1")

    assert state == {"as_of_date": "2026-10-29", "tier1_consecutive_bars": 1}


def test_list_runs_reports_every_instance_and_latest_only_dedupes(temp_workspace):
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-29", instance="091500")
    _make_run(storage, "s1", "u1", "2026-10-29", instance="123000")

    every = storage.list_runs(strategy_key="s1", universe_key="u1", require_both=True)
    latest = storage.list_runs(
        strategy_key="s1", universe_key="u1", require_both=True, latest_only=True
    )

    assert [r["instance"] for r in every] == ["123000", "091500"]
    assert [r["instance"] for r in latest] == ["123000"]


def test_summary_less_instance_is_dated_by_its_parent_not_its_stamp(temp_workspace):
    """A half-written instance must not be listed as a run dated `091500` (which would
    sort before every real session and poison `get_previous_run`)."""
    storage, _ = temp_workspace
    run_dir = os.path.join(storage.archive_dir, "s1", "u1", "2026-10-29", "091500")
    os.makedirs(run_dir)
    # A ticket, but no summary.json -- i.e. the child wrote its artifact and then the
    # process died before the round's summary was merged in.
    _write_json(os.path.join(run_dir, "live_trading_ticket.json"), [])

    runs = storage.list_runs(strategy_key="s1", universe_key="u1", require_both=True)

    assert [r["date"] for r in runs] == ["2026-10-29"]


# --------------------------------------------------------------------------- #
# 4. Summary de-merge (pure): no field may survive from another round
# --------------------------------------------------------------------------- #
def test_summary_inheritance_whitelist_is_empty():
    """Pins the documented contract: nothing is inherited across rounds by default.

    If inheritance is ever needed, ``SUMMARY_INHERITANCE_WHITELIST`` grows AND the summary
    must record ``inherited_from`` / ``inherited_fields`` -- not silently leak.
    """
    assert SUMMARY_INHERITANCE_WHITELIST == ()


def test_de_merge_drops_cross_round_fields_and_keeps_this_rounds():
    pre = {
        "deploy_status": "SUCCESS",
        "deploy_stdout": "utc 03:06",
        "health_stdout": "utc 04:31",
        "error_detail": "KeyboardInterrupt",
        "date": "2026-10-27",
    }
    post = {**pre, "health_status": "SUCCESS", "date": "2026-10-27"}

    cleaned, dropped = de_merge_run_summary(
        pre, post, owned_keys={"health_status", "date"}
    )

    assert cleaned == {"health_status": "SUCCESS", "date": "2026-10-27"}
    assert dropped == [
        "deploy_status",
        "deploy_stdout",
        "error_detail",
        "health_stdout",
    ]


def test_de_merge_is_a_noop_for_a_fresh_instance():
    """An empty directory means the layout already guarantees the contract."""
    post = {"date": "2026-10-29", "health_status": "SUCCESS"}

    cleaned, dropped = de_merge_run_summary({}, post, owned_keys=set())

    assert cleaned == post
    assert dropped == []


def test_de_merge_keeps_a_key_only_the_child_script_wrote():
    """Keys absent from the PRE-round file were produced by THIS round's child, not leaked."""
    post = {"date": "2026-10-29", "matrix_id": "m1"}

    cleaned, dropped = de_merge_run_summary({"date": "2026-10-28"}, post, owned_keys={"date"})

    assert cleaned == post
    assert dropped == []


# --------------------------------------------------------------------------- #
# 5. Wiring: Stage 1 -> Stage 2 share one dir, reruns do not
# --------------------------------------------------------------------------- #
def test_same_day_rerun_writes_an_independent_summary(
    temp_workspace, advancing_clock, monkeypatch
):
    """THE core regression: two rounds, two dirs, no field from round 1 in round 2."""
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)

    first_cmds = []

    def _run_round1(cmd, *args, **kwargs):
        first_cmds.append(list(cmd))
        return _MockProc(returncode=0, stdout="ROUND-1-STDOUT")

    monkeypatch.setattr(subprocess, "run", _run_round1)
    res1 = service.run_health_check(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
    )
    assert res1["status"] == "success"

    second_cmds = []

    def _run_round2(cmd, *args, **kwargs):
        second_cmds.append(list(cmd))
        return _MockProc(returncode=0, stdout="ROUND-2-STDOUT")

    monkeypatch.setattr(subprocess, "run", _run_round2)
    res2 = service.run_health_check(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
    )
    assert res2["status"] == "success"

    dir1 = _arg_value(first_cmds[0], "--output-dir")
    dir2 = _arg_value(second_cmds[0], "--output-dir")

    assert dir1 != dir2, "a same-day rerun must not reuse the previous round's directory"
    assert is_instance_dir_name(os.path.basename(dir2))
    assert os.path.dirname(dir2) == dir1, "the rerun lives INSIDE the session's dated dir"

    summary1 = _read_json(os.path.join(dir1, "summary.json"))
    summary2 = _read_json(os.path.join(dir2, "summary.json"))

    assert summary1["health_stdout"] == "ROUND-1-STDOUT"
    assert summary2["health_stdout"] == "ROUND-2-STDOUT"
    # ... and nothing from round 1 leaked into round 2 (the incident's exact failure).
    assert summary1["timestamp"] != summary2["timestamp"]
    assert "ROUND-1-STDOUT" not in json.dumps(summary2)


def test_stage2_reuses_the_directory_stage1_allocated(temp_workspace, advancing_clock, monkeypatch):
    """Both stages of ONE deploy must share one dir (`--health-report-file` depends on it)."""
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    captured = []

    def _run(cmd, *args, **kwargs):
        """Materialise the child's own artifact, like the real Stage-1 runner does."""
        cmd_list = list(cmd)
        captured.append(cmd_list)
        if "--output-dir" in cmd_list:
            run_dir = cmd_list[cmd_list.index("--output-dir") + 1]
            _write_json(
                os.path.join(run_dir, "stage1_health_report.json"),
                {"portfolio_health": {"tier": "NORMAL"}},
            )
        return _MockProc(returncode=0, stdout="OK")

    monkeypatch.setattr(subprocess, "run", _run)
    service.run_health_check(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
    )
    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
        nav_override=True,
    )

    stage1_dir = _arg_value(captured[0], "--output-dir")
    stage2_dir = _arg_value(captured[1], "--output-dir")

    assert stage2_dir == stage1_dir
    assert _arg_value(captured[1], "--health-report-file") == os.path.join(
        stage1_dir, "stage1_health_report.json"
    )
    assert _arg_value(captured[1], "--account-state-file") == os.path.join(
        stage1_dir, "account_state.json"
    )
    assert _arg_value(captured[1], "--current-holdings-file") == os.path.join(
        stage1_dir, "holdings.json"
    )

    # (L2.62) The summary must describe BOTH halves of THIS round: the Stage-1 health fields
    # are re-owned by Stage 2 (same round, same dir), so the de-merge must not delete them --
    # while anything from an EARLIER round would still be dropped.
    summary = _read_json(os.path.join(stage1_dir, "summary.json"))
    assert summary["health_stdout"] == "OK"
    assert summary["health_status"] == "SUCCESS"
    assert summary["deploy_status"] == "SUCCESS"


def test_stage2_double_click_does_not_fragment_the_archive(temp_workspace, monkeypatch):
    """Within ``PENDING_STAGE2_REUSE_SECONDS`` a second Stage-2 call reuses the same dir."""
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    captured = _patch_subprocess(monkeypatch)

    kwargs = dict(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
        nav_override=True,
    )
    service.run_live_deploy(**kwargs)
    service.run_live_deploy(**kwargs)

    assert _arg_value(captured[0], "--output-dir") == _arg_value(captured[1], "--output-dir")

    # ... but once the window has passed, the next click IS a new round.
    monkeypatch.setattr(ExecutionService, "PENDING_STAGE2_REUSE_SECONDS", -1.0)
    service.run_live_deploy(**kwargs)
    assert _arg_value(captured[2], "--output-dir") != _arg_value(captured[0], "--output-dir")


def test_stage2_seeds_account_state_from_the_same_day_earlier_instance(
    temp_workspace, advancing_clock, monkeypatch
):
    """A fresh instance dir starts empty; the session's lifecycle/streak state must carry."""
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    _patch_subprocess(monkeypatch)

    earlier = _make_run(storage, "s1", "u1", "2026-10-29", instance="091500")
    _write_json(
        os.path.join(earlier, "account_state.json"),
        {
            "as_of_date": "2026-10-29",
            "tier1_consecutive_bars": 2,
            "holding_bars": {"600519.SH": 5},
        },
    )

    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
        nav_override=True,
    )

    new_dir = os.path.join(storage.archive_dir, "s1", "u1", "2026-10-29", "090000")
    assert os.path.isdir(new_dir)
    seeded = _read_json(os.path.join(new_dir, "account_state.json"))
    assert seeded["tier1_consecutive_bars"] == 2
    assert seeded["holding_bars"] == {"600519.SH": 5}


def test_restart_between_stages_inherits_the_same_sessions_gate_report(
    temp_workspace, monkeypatch
):
    """A RESTART (or another uvicorn worker) must not silently drop the macro gate.

    ``_pending_stage1`` is a plain in-process dict, so a Stage 2 served by a different
    process finds no hand-off. Before the fix it allocated a fresh instance dir, looked
    for `stage1_health_report.json` INSIDE it (absent), omitted
    `--health-report-file`, and the child then defaulted to the same missing path -- so
    the Stage-1 macro gate / circuit-breaker directive was silently not applied to the
    ticket. The report must be re-discovered from the session's archive, handed over as
    the ORIGINAL path, and recorded as inherited provenance in the new round's summary.
    """
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    health_cmds = []

    def _run_stage1(cmd, *args, **kwargs):
        """Materialise the gate report, like the real Stage-1 runner does."""
        cmd_list = list(cmd)
        health_cmds.append(cmd_list)
        run_dir = _arg_value(cmd_list, "--output-dir")
        _write_json(
            os.path.join(run_dir, "stage1_health_report.json"),
            {
                "gate_code": "CAUTION_DRAWDOWN",
                "directive": "CAUTION",
                "circuit_breaker_tier": 2,
                "linear_equity_scale": 0.8,
                "unrelated_note": "not a gate field",
            },
        )
        return _MockProc(returncode=0, stdout="ROUND-1-HEALTH")

    monkeypatch.setattr(subprocess, "run", _run_stage1)
    res1 = service.run_health_check(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
    )
    assert res1["status"] == "success"

    stage1_dir = _arg_value(health_cmds[0], "--output-dir")
    stage1_report = os.path.join(stage1_dir, "stage1_health_report.json")
    assert os.path.exists(stage1_report)
    stage1_report_bytes = _read_json(stage1_report)

    # The previous round's summary is exactly where a naive "inherit the old round"
    # implementation would take the gate fields from -- poison it with values that must
    # never reach the new round.
    stage1_summary = _read_json(os.path.join(stage1_dir, "summary.json"))
    _write_json(
        os.path.join(stage1_dir, "summary.json"),
        {
            **stage1_summary,
            "deploy_status": "SUCCESS",
            "deploy_stdout": "ROUND-1-DEPLOY",
            "nav_effective": 999999.0,
            "error_detail": "KeyboardInterrupt",
        },
    )

    # ---- RESTART: both hand-off dicts are gone; this is a fresh "worker" ----
    service._pending_stage1.clear()
    service._pending_stage2.clear()

    deploy_cmds = []

    def _run_stage2(cmd, *args, **kwargs):
        deploy_cmds.append(list(cmd))
        return _MockProc(returncode=0, stdout="ROUND-2-DEPLOY")

    monkeypatch.setattr(subprocess, "run", _run_stage2)
    res2 = service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
        nav_override=True,
    )
    assert res2["status"] == "success"

    new_dir = _arg_value(deploy_cmds[0], "--output-dir")
    assert new_dir != stage1_dir, "the restarted Stage 2 still gets its OWN round dir"
    assert _arg_value(deploy_cmds[0], "--health-report-file") == stage1_report, (
        "the gate report of the EARLIER instance must be handed to the child, not a "
        "path inside the new (empty) round directory"
    )

    summary = _read_json(os.path.join(new_dir, "summary.json"))
    assert summary["health_gate_applied"] is True
    assert summary["health_report_file"] == stage1_report
    # Provenance: which round's gate this ticket obeyed, and which fields of it.
    assert summary["health_report_source"] == os.path.basename(stage1_dir)
    assert summary["inherited_from"] == os.path.relpath(stage1_dir, storage.archive_dir)
    assert summary["inherited_fields"] == [
        "circuit_breaker_tier",
        "directive",
        "gate_code",
        "linear_equity_scale",
    ]
    assert "unrelated_note" not in summary["inherited_fields"]
    # ... derived from the REPORT, never copied from the previous round's `summary.json`:
    # no deploy_* / nav_* / error_* survived, and the health half is not claimed as this
    # round's own Stage-1 run.
    assert summary["deploy_stdout"] == "ROUND-2-DEPLOY"
    assert summary["deploy_status"] == "SUCCESS"
    assert summary["nav_effective"] != 999999.0
    assert "error_detail" not in summary
    assert "health_stdout" not in summary, "Stage 2 never claims Stage 1's process output"

    # The earlier instance's archive must not be mutated by the inheriting round: the
    # post-run 5m normalization is written to THIS round's own copy instead.
    assert _read_json(stage1_report) == stage1_report_bytes
    local_report = os.path.join(new_dir, "stage1_health_report.json")
    assert os.path.exists(local_report), "the inherited report is archived in this round too"
    assert _read_json(local_report)["gate_code"] == "CAUTION_DRAWDOWN"


def test_restart_with_no_reachable_gate_report_refuses_the_deploy(temp_workspace, monkeypatch):
    """Stage 1 ran (its summary proves it) but its report is gone -> refuse, loudly.

    Deploying here would produce a ticket with the macro gate / circuit breaker silently
    switched off -- the exact regression this test exists to prevent. A hard refusal is
    the only alternative that keeps the gate's meaning.
    """
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    earlier = _make_run(
        storage,
        "s1",
        "u1",
        "2026-10-29",
        instance="091500",
        summary={"health_status": "SUCCESS", "health_returncode": 0},
    )
    assert not os.path.exists(os.path.join(earlier, "stage1_health_report.json"))

    service._pending_stage1.clear()
    service._pending_stage2.clear()
    captured = _patch_subprocess(monkeypatch)

    with pytest.raises(Stage1HealthReportMissingError) as excinfo:
        service.run_live_deploy(
            strategy_file=strat_file,
            custom_symbols=["600519.SH"],
            as_of_date="2026-10-29",
            portfolio_value=100000.0,
            current_holdings={"600519.SH": 100},
            data_provider="synthetic",
            strategy_key="s1",
            universe_key="u1",
            nav_override=True,
        )

    assert captured == [], "no child may be launched without a macro gate"
    message = str(excinfo.value)
    assert "stage1_health_report.json" in message
    # Actionable: names the fix, not just the failure.
    assert "/api/run-health" in message
    assert "091500" in message, "the evidence it found must be named"
    # Refusal is the only side effect: no round was written into the archive.
    session_dir = os.path.join(storage.archive_dir, "s1", "u1", "2026-10-29")
    written = [
        d
        for d in list_instances_for_date(session_dir)
        if os.path.exists(os.path.join(d, "summary.json"))
    ]
    assert written == [earlier]


def test_a_session_with_no_stage1_at_all_deploys_ungated_but_says_so(
    temp_workspace, monkeypatch
):
    """Pre-L2.62 semantics stay reachable, but never SILENTLY.

    Stage 1 was never invoked for this session (no evidence anywhere), so there is no
    gate to inherit and no gate to refuse. The deploy proceeds -- exactly as it did
    before L2.62 -- while `health_gate_applied=False` records it in the archive and a
    warning is logged, so an ungated ticket can never be mistaken for a gated one.
    """
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)
    strat_file = _dummy_strategy_file(temp_dir)
    captured = _patch_subprocess(monkeypatch)

    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-29",
        portfolio_value=100000.0,
        current_holdings={"600519.SH": 100},
        data_provider="synthetic",
        strategy_key="s1",
        universe_key="u1",
        nav_override=True,
    )

    run_dir = _arg_value(captured[0], "--output-dir")
    assert "--health-report-file" not in captured[0]
    summary = _read_json(os.path.join(run_dir, "summary.json"))
    assert summary["health_gate_applied"] is False
    assert summary["health_report_file"] is None
    assert "health_report_source" not in summary, "nothing was inherited"


def test_derive_anchors_never_uses_a_same_day_run_as_the_open_date(temp_workspace):
    """(L2.62 ⑤) A symbol first seen TODAY is NOT anchored to today.

    Anchoring it would make the total-return ratio identically 1.0 while the summary
    still claims ``dividend_basis="total_return"`` -- i.e. a raw mark mislabelled as
    dividend-inclusive. Excluding the whole day keeps it on ``anchor_unavailable``.
    """
    storage, _ = temp_workspace
    service = ExecutionService(storage=storage)
    _make_run(storage, "s1", "u1", "2026-10-28", holdings={"OLD": 100})
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"OLD": 100, "TODAY": 100})

    anchors, missing = service._derive_anchors(
        {"OLD": 100, "TODAY": 100},
        as_of="2026-10-29",
        strategy_key="s1",
        universe_key="u1",
    )

    assert anchors == {"OLD": "2026-10-28"}, "same-day runs are not an open date"
    assert missing == ["TODAY"]


# --------------------------------------------------------------------------- #
# 6. API: `latest_only` + `instance` are the only front-end contract changes
# --------------------------------------------------------------------------- #
def test_api_runs_returns_every_instance_unless_latest_only(temp_workspace):
    """`/api/runs` default = every instance (nothing hidden); `latest_only` = one per date."""
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-29", instance="091500")
    _make_run(storage, "s1", "u1", "2026-10-29", instance="123000")

    from fastapi.testclient import TestClient

    with TestClient(create_app(storage)) as client:
        every = client.get("/api/runs?strategy=s1&universe=u1&require_both=true").json()
        latest = client.get(
            "/api/runs?strategy=s1&universe=u1&require_both=true&latest_only=true"
        ).json()

    assert [r["instance"] for r in every] == ["123000", "091500"]
    assert [r["instance"] for r in latest] == ["123000"]


def test_api_keyed_run_reads_the_newest_instance_and_says_which(temp_workspace):
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"AAA": 100})
    _make_run(storage, "s1", "u1", "2026-10-29", holdings={"BBB": 200}, instance="123000")

    from fastapi.testclient import TestClient

    with TestClient(create_app(storage)) as client:
        body = client.get("/api/runs/s1/u1/2026-10-29").json()

    assert body["instance"] == "123000"
    assert body["holdings"] == {"BBB": 200}


# --------------------------------------------------------------------------- #
# 7. Seed precedence: a completed Stage-2 round's planned post book must beat
#    its own stale pre-trade `holdings.json` (the 2026-10-30 incident)
#
# `holdings.json` has TWO writers: `save_holdings` (the operator's hand-entered
# book, which is what `operator_snapshot` claims) and `save_daily_run` (the
# Stage-2 deploy persisting its own PRE-trade seed before launching the child).
# In a dir that already ran Stage 2 the file is therefore stale, and the same
# dir's `post_holdings.json` is the authoritative book.  Real evidence: the live
# archive's `.../2026-10-29/215823/` holds a ghost `holdings.json`
# (`600584.SH:100`, `600362.SH:100`) beside the corrected `post_holdings.json`.
# --------------------------------------------------------------------------- #
def test_completed_stage2_round_seeds_from_its_post_book_not_the_ghost_snapshot(
    temp_workspace,
):
    """(2026-10-30 incident) A ghost pre-trade `holdings.json` must never win.

    re-break: restore the plain `_load_snapshot_holdings`-first walk in
    `resolve_seed_holdings` -- the ghost book claiming already-sold lots would seed the
    rerun again as `operator_snapshot`.
    """
    storage, _ = temp_workspace
    run_dir = _make_run(
        storage,
        "s1",
        "u1",
        "2026-10-29",
        holdings={"600584.SH": 100, "600362.SH": 100},
        instance="215823",
    )
    planned = {
        "600900.SH": 800,
        "600660.SH": 400,
        "000999.SZ": 600,
        "601728.SH": 5800,
        "600519.SH": 100,
    }
    _write_json(os.path.join(run_dir, "post_holdings.json"), planned)

    seed, source = storage.resolve_seed_holdings("2026-10-29", "s1", "u1")

    assert seed == planned, "the planned post book, not the ghost pre-trade input"
    assert source == "operator_snapshot"


def test_snapshot_only_round_without_stage2_artifacts_stays_the_operator_book(
    temp_workspace,
):
    """The legitimate case: a hand-entered book in a dir that never ran Stage 2.

    re-break: skip every candidate that lacks a Stage-2 artifact (or treat any
    `holdings.json` as stale) in `resolve_seed_holdings` -- the operator's own book would
    become unreachable and the deploy would refuse (`LiveSeedBookMissingError`).
    """
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-30", holdings={"BBB.SH": 250})

    seed, source = storage.resolve_seed_holdings("2026-10-30", "s1", "u1")

    assert seed == {"BBB.SH": 250}
    assert source == "operator_snapshot"


def test_stage2_artifact_without_a_planned_book_never_returns_the_stale_snapshot(
    temp_workspace,
):
    """Stage 2 ran here (ticket), yet no plan is recoverable: skip its `holdings.json`.

    The ticket below carries NO `target_shares`, so `post_holdings_from_ticket` cannot
    derive a plan.  Returning the pre-trade `holdings.json` here is exactly the ghost-book
    failure, so the method must fall through to `(None, "none")` instead.
    re-break: let a Stage-2-artifact dir fall through to `_load_snapshot_holdings`
    (instead of `continue`) in `resolve_seed_holdings`.
    """
    storage, _ = temp_workspace
    run_dir = _make_run(storage, "s1", "u1", "2026-10-30", holdings={"600584.SH": 100})
    _write_json(
        os.path.join(run_dir, "live_trading_ticket.json"),
        [{"symbol": "600584.SH", "action": "HOLD"}],
    )

    seed, source = storage.resolve_seed_holdings("2026-10-30", "s1", "u1")

    assert seed is None, "an unresolvable plan must not degrade to the stale snapshot"
    assert source == "none"


def test_newest_instance_post_book_beats_the_flat_dir_snapshot(temp_workspace):
    """Within one session the newest instance's planned book is the current book.

    re-break: walk the flat dir first (i.e. stop `reversed(...)`-ing
    `list_instances_for_date`, or append `base_dir` first instead of last) in
    `resolve_seed_holdings`.
    """
    storage, _ = temp_workspace
    _make_run(storage, "s1", "u1", "2026-10-30", holdings={"FLAT.SH": 1})
    newest = _make_run(storage, "s1", "u1", "2026-10-30", instance="215823")
    _write_json(os.path.join(newest, "post_holdings.json"), {"NEW.SH": 2})

    seed, source = storage.resolve_seed_holdings("2026-10-30", "s1", "u1")

    assert seed == {"NEW.SH": 2}
    assert source == "operator_snapshot"


def test_empty_archive_still_resolves_to_unknown(temp_workspace):
    """No archive book at all -> `(None, "none")`, i.e. UNKNOWN (the deploy must refuse).

    re-break: make `resolve_seed_holdings` end with `return {}, "none"` (or
    `self.get_holdings(...)`) -- an unknown book would look like a confirmed flat one and
    every target would be sized as a fresh BUY.
    """
    storage, _ = temp_workspace

    seed, source = storage.resolve_seed_holdings("2026-10-30", "s1", "u1")

    assert seed is None
    assert source == "none"


def test_confirmed_all_cash_post_book_in_the_candidate_dir_is_not_unknown(
    temp_workspace,
):
    """`{}` in the candidate dir's `post_holdings.json` is CONFIRMED all-cash, not unknown.

    re-break: change the `if plan is not None:` guard to `if plan:` (or fall through on a
    falsy plan) in `resolve_seed_holdings` -- a fully-liquidated round would become
    `(None, "none")` and the next deploy would wrongly refuse.
    """
    storage, _ = temp_workspace
    run_dir = _make_run(storage, "s1", "u1", "2026-10-30", instance="215823")
    _write_json(os.path.join(run_dir, "post_holdings.json"), {})

    seed, source = storage.resolve_seed_holdings("2026-10-30", "s1", "u1")

    assert seed == {}
    assert seed is not None, "an empty planned book is a CONFIRMED flat book"
    assert source == "operator_snapshot"


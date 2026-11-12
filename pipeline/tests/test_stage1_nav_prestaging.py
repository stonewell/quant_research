"""`LV11` -- the valuation snapshot must be PRE-STAGED before the Stage-1 gate runs.

FORENSIC CONTEXT
----------------
`nav_breakdown.json` used to have exactly one producer: Stage 2 (`run_live_deploy`).  So the
FIRST round of every session asked Stage 1's circuit breaker for a verdict on a book whose
mark did not exist yet on disk.  Stage 1 degraded (`SNAPSHOT_NOT_YET_WRITTEN`), persisted the
placeholder's `circuit_breaker_tier="NORMAL"` while its report said `UNKNOWN`, and Stage 2 was
then handed a report that could neither authorise nor forbid risk.

`LV11` (option (a), "move the timing forward") stages the snapshot from the web launcher BEFORE
the gate child is launched, so both stages of one round read ONE valuation snapshot.  This
suite pins that ordering and the fail-closed contract around it:

1. **Ordering.** At the instant the child is launched, `<run_dir>/nav_breakdown.json` already
   exists and `--nav-breakdown-file` points at it.  (A snapshot written after the child would
   still fail the archived-instants audit: the gate would already have degraded.)
2. **No book => no pin, still launch.** On a brand-new account `resolve_seed_holdings` returns
   `(None, "none")`.  Pre-staging is BEST-EFFORT: it must not pin a path, must not invent a
   NAV, and must not turn the health check into a hard failure -- Stage 1 then classifies the
   absence itself and Stage 2 refuses to issue a ticket (fail closed, not fail silent).
3. **Unmarkable book => no pin, still launch.** A valuation failure (e.g. an uninitialised
   cache) is logged and skipped for the same reason.  Silently pinning a bogus file would be
   worse than degrading, and the gate has its own loud classification.
4. **The pin is what makes an unreadable file FATAL.** `--nav-breakdown-file` is only added
   for a path this launcher just wrote, which is what lets Stage 1 treat a missing pinned file
   as `NavSnapshotError` instead of a silent degradation.
5. **(`LV11b`, 2026-11-04) The held book's daily bars are refreshed BEFORE the mark.**  The
   preset used to be the first reader of the cache while the data refresh ran in the Stage-1
   child minutes later, so the FIRST run of every new `as_of` date fail-fasted on the previous
   session's bar (`StalePriceError`) and the second always succeeded.  Section 1c pins the
   refresh-before-mark ordering, its falsifiable twin, and -- critically -- that a symbol which
   is STILL missing its `as_of` bar after the refresh keeps failing closed.  A "fall back to
   the form value / previous snapshot" shortcut would turn all three red.

100% offline: synthetic bars in a throwaway DuckDB file, `subprocess.run` always monkeypatched,
no path under the real `data/` tree is opened.

WHY THIS FILE LIVES IN `pipeline/tests/`
----------------------------------------
Same reason as `test_live_nav_computed.py`: `web/livetrading/tests` needs `fastapi`/`pydantic`
which the shared `pipeline` venv does not have.  `execution_service.py` itself imports only
stdlib + pandas + `common.*`.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WEB_DIR = os.path.join(_REPO_ROOT, "web", "livetrading")
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.duckdb_cache import DuckDBCache  # noqa: E402

#: `execution_service` imports `storage`/`valuation` by BARE name after `web/livetrading` is on
#: sys.path (exactly how `server.py` does it), so those are imported the same way here.
import storage  # noqa: E402


def _load_execution_service():
    """Load `execution_service.py` under a UNIQUE module name.

    Its own bare imports (`storage`, `valuation`) still resolve through `sys.path`, which is
    what we want; loading it under the bare name here would collide with other suites.
    """
    path = os.path.join(_WEB_DIR, "execution_service.py")
    spec = importlib.util.spec_from_file_location("web_livetrading_execution_service_prestage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


execution_service = _load_execution_service()

PROVIDER = "marketdb"  # NOT "synthetic": the engine reads the cache for its mark
AS_OF = "2026-01-06"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, on_launch=None):
    """Capture argv and never launch a real process.

    ``on_launch(cmd)`` runs at the exact instant the child WOULD start -- that is how the
    "the file already existed before the gate ran" assertion is made without touching the
    production ordering (a snapshot written after `subprocess.run` still passes a naive test).
    """
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        if on_launch is not None:
            on_launch(list(cmd))
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _bars(rows):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    close = pd.Series([float(c) for _, c in items], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1_000_000.0},
        index=idx,
    )


def _factors(rows):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    return pd.DataFrame({"factor_hfq": pd.Series([float(v) for _, v in items], index=idx)}, index=idx)


def _seed_cache(cache, symbol, bars, factors=None):
    cache.store_bars(PROVIDER, symbol, _bars(bars), adjustflag="raw")
    if factors is not None:
        cache.store_adjust_factors(symbol, _factors(factors))


@pytest.fixture
def workspace(tmp_path):
    """Hermetic `StorageManager` + a REAL file-backed cache under ``tmp_path``."""
    dirs = {}
    for name in ("data", "runs", "cache", "settings"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d
    mgr = storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(dirs["data"]),
        archive_dir=str(dirs["runs"]),
        cache_dir=str(dirs["cache"]),
        settings_dir=str(dirs["settings"]),
    )
    return {"storage": mgr, "dirs": dirs, "tmp_path": tmp_path}


def _strategy_file(workspace):
    path = os.path.join(str(workspace["tmp_path"]), "strat.json")
    workspace["storage"]._save_json(path, {"template_name": "dummy", "params": {}})
    return path


def _seed_book(workspace):
    """``AAA``: 10.00 -> 10.00, factor 1.0 -> 1.0, so 100 shares mark to exactly 1,000."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(
        cache,
        "AAA",
        {"2026-01-05": 10.0, AS_OF: 10.0},
        {"2026-01-05": 1.0, AS_OF: 1.0},
    )
    return cache


def _seed_book_missing_as_of(workspace):
    """The 2026-11-04 shape: bars stop at ``as_of - 1``, i.e. NOTHING refreshed today yet.

    This is what the cache actually looked like on the first run of a new valuation date
    (`203129/`): `compute_nav` has a bar for 01-05 but none for 01-06, so the fail-closed
    staleness check fires.  The held-book refresh is what is supposed to close that gap.
    """
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0}, {"2026-01-05": 1.0})
    return cache


def _stage1_module():
    """`scripts/check_live_portfolio_health.py` under a UNIQUE module name (heavy import, once)."""
    cached = getattr(_stage1_module, "_cached", None)
    if cached is not None:
        return cached
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    spec = importlib.util.spec_from_file_location("check_live_portfolio_health_lv11b", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _stage1_module._cached = mod
    return mod


def _archive_round_with_cash(workspace, date_str, cash, cash_source="explicit", holdings=None):
    """Write a REAL earlier round whose `summary.json` records a measured cash residual.

    Cash is not a holding (`post_holdings_from_ticket` skips BIL/CASH rows), so this archived
    scalar is the only pre-form source of it -- see `ExecutionService._archived_cash_for`.
    """
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )
    return workspace["storage"].save_daily_run(
        date_str=date_str,
        holdings=dict(holdings or {"AAA": 100}),
        summary={
            "date": date_str,
            "cash": cash,
            "cash_source": cash_source,
            "nav_effective": 1000.0 + float(cash),
        },
        strategy_key=strat_key,
        universe_key=univ_key,
    )


# ---------------------------------------------------------------------------
# 1. Ordering -- the snapshot exists BEFORE the gate child launches
# ---------------------------------------------------------------------------
def test_snapshot_is_staged_before_the_gate_child_launches(workspace, monkeypatch):
    """DISCRIMINATING: pre-LV11 the file did not exist until Stage 2, so this goes red.

    `on_launch` reads the file *inside* the subprocess mock, i.e. exactly when the gate
    process would start.  A writer placed after `subprocess.run` would still fail here.
    """
    _seed_book(workspace)
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0)
    seen = {}

    def _inspect(cmd):
        run_dir = _flag(cmd, "--output-dir")
        path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME)
        seen["existed"] = os.path.exists(path)
        if seen["existed"]:
            with open(path, "r", encoding="utf-8") as f:
                seen["payload"] = json.load(f)
        seen["pinned"] = _flag(cmd, "--nav-breakdown-file")

    captured = _patch_subprocess(monkeypatch, on_launch=_inspect)

    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        data_provider=PROVIDER,
        refresh_data=False,
        # (LV1-TIMEBASE) same-instant cash: the archived residual is from 2026-01-05, which
        # would (correctly) be refused, so the operator's own balance is what makes this a
        # normal mark. Cash itself is unrelated to the ordering assertion below.
        cash=0.0,
    )

    assert len(captured) == 1, captured  # exactly one child: the gate
    cmd = captured[0]
    assert cmd[1] == workspace["storage"].stage1_script
    # (1) the file ALREADY existed when the child was launched ...
    assert seen["existed"] is True
    assert seen["payload"]["nav_effective"] == pytest.approx(1000.0)
    assert seen["payload"]["as_of"] == AS_OF
    # (2) ... and it is the SAME file that was pinned on the command line.
    assert seen["pinned"] == os.path.join(
        _flag(cmd, "--output-dir"), execution_service.NAV_BREAKDOWN_FILENAME
    )
    assert cmd.count("--nav-breakdown-file") == 1


def test_staged_snapshot_is_the_engine_mark_not_the_form_value(workspace, monkeypatch):
    """The staged NAV is a real dividend-aware mark, NOT `--portfolio-value` re-badged.

    The form says 100,000; the book is 100 x 10.00 + 0 cash = 1,000.  If pre-staging ever
    "staged" the form value the gate would be measured against a number with no relationship
    to the book -- which is precisely the LV1 defect, revived one layer up.
    """
    _seed_book(workspace)
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0)
    captured = _patch_subprocess(monkeypatch)

    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        portfolio_value=100000.0,
        data_provider=PROVIDER,
        refresh_data=False,
        cash=0.0,  # (LV1-TIMEBASE) same-instant cash: see the test above
    )

    run_dir = _flag(captured[0], "--output-dir")
    with open(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["nav_effective"] == pytest.approx(1000.0)
    assert payload["nav_effective"] != pytest.approx(100000.0)
    assert payload["nav_source"] == "computed"
    # The form value still travels to the child unchanged: it is the audit record of what was
    # ASKED for, and LV1 pins how Stage 1 reconciles the two.
    assert _flag(captured[0], "--portfolio-value") == "100000.0"


# ---------------------------------------------------------------------------
# 1b. Where the staged cash came from -- and the one thing it must never do
# ---------------------------------------------------------------------------
def test_cash_is_taken_from_the_previous_rounds_measured_residual(workspace):
    """A seed book has no cash in it, so the previous round's measured residual is used.

    Provenance is recorded on the staged snapshot (`cash_from_archive`) so an auditor can see
    that the gate marked `equity + <last round's cash>` rather than a freshly asserted number.
    """
    _archive_round_with_cash(workspace, "2026-01-05", cash=250.0)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )

    cash, cash_as_of, origin = svc._archived_cash_for(AS_OF, strat_key, univ_key)

    assert cash == pytest.approx(250.0)
    # (LV1-TIMEBASE) the cash's OWN session travels with it -- the equity leg is marked at
    # `AS_OF`, so callers must be able to tell a same-instant residual from yesterday's.
    assert cash_as_of == "2026-01-05"
    assert origin and origin.endswith("summary.json")


def test_an_unmeasured_cash_is_never_recycled_as_zero(workspace):
    """`cash_source="missing"` means UNKNOWN, not 0.0 -- returning it would be the inversion.

    Missing is not "not triggered" all over again: a 0.0 that was never measured would mark
    every position as the whole portfolio and shrink the gate's NAV to the bare equity.
    """
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0, cash_source="missing")
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )

    assert svc._archived_cash_for(AS_OF, strat_key, univ_key) == (None, None, None)


def test_a_future_rounds_cash_is_not_borrowed(workspace):
    """Only rounds at-or-before the valuation session may seed its cash (no look-ahead)."""
    _archive_round_with_cash(workspace, "2026-02-02", cash=999.0)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )

    assert svc._archived_cash_for(AS_OF, strat_key, univ_key) == (None, None, None)


# ---------------------------------------------------------------------------
# 1c. (LV11b, 2026-11-04) The held book's bars are refreshed BEFORE the mark
# ---------------------------------------------------------------------------
def _track_nav_calls(monkeypatch, events):
    """Append ``("resolve_nav",)`` at the exact instant the preset marks the book."""
    real = execution_service.ExecutionService._resolve_nav

    def _tracked(self, *a, **k):
        events.append(("resolve_nav",))
        return real(self, *a, **k)

    monkeypatch.setattr(execution_service.ExecutionService, "_resolve_nav", _tracked)


def _inspect_launch(cmd, seen):
    """Record, at child-launch time, whether the snapshot exists and what was pinned."""
    run_dir = _flag(cmd, "--output-dir")
    seen["snapshot_exists"] = os.path.exists(
        os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME)
    )
    seen["pinned"] = _flag(cmd, "--nav-breakdown-file")


def test_held_book_is_refreshed_before_the_snapshot_is_marked(workspace, monkeypatch):
    """DISCRIMINATING (LV11b): the 2026-11-04 `203129/` FAILED -> `203553/` SUCCESS sequence.

    The cache stops at `as_of - 1` -- nothing has refreshed the new session yet.  The
    held-book refresh is the ONLY thing that can make the mark succeed, and it must run
    BEFORE `_resolve_nav`, or the preset fail-fasts on `StalePriceError` and no snapshot is
    written (so the gate degrades and Stage 2 refuses).

    Remove/disable the refresh and this test goes RED on its first assertion -- pinned by
    `test_without_the_held_book_refresh_the_first_run_fails_closed` below.
    """
    _seed_book_missing_as_of(workspace)
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0)
    events = []
    seen = {}

    def _fake_refresh(cache_dir, symbols, *, as_of, market="china", data_provider=None):
        events.append(("refresh", list(symbols), as_of))
        seen["cache_dir"] = str(cache_dir)
        # The real call fetches the missing session's bar; do the same, offline.
        _seed_cache(DuckDBCache(db_path=str(cache_dir)), "AAA", {AS_OF: 10.0}, {AS_OF: 1.0})
        return {"status": "completed", "symbols": list(symbols), "fetch_errors": {}}

    monkeypatch.setattr(execution_service, "refresh_held_book_daily_bars", _fake_refresh)
    _track_nav_calls(monkeypatch, events)

    captured = _patch_subprocess(
        monkeypatch, on_launch=lambda cmd: _inspect_launch(cmd, seen)
    )
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        data_provider=PROVIDER,
        refresh_data=True,
        cash=0.0,  # (LV1-TIMEBASE) same-instant cash; the archive's is from 2026-01-05
    )

    # (1) The refresh runs FIRST, on the HELD BOOK, for the valuation date.
    assert [e[0] for e in events] == ["refresh", "resolve_nav"], events
    assert events[0][1] == ["AAA"], "held-book symbols only, never the whole universe"
    assert events[0][2] == AS_OF
    assert seen["cache_dir"] == str(workspace["dirs"]["cache"])
    # (2) The mark therefore succeeds and the snapshot exists when the child launches.
    assert len(captured) == 1, captured
    assert seen["snapshot_exists"] is True
    assert seen["pinned"] == os.path.join(
        _flag(captured[0], "--output-dir"), execution_service.NAV_BREAKDOWN_FILENAME
    )
    # (3) The child keeps its own UNIVERSE-WIDE refresh: the pre-refresh is additive.
    assert "--no-refresh-data" not in captured[0]


def test_without_the_held_book_refresh_the_first_run_fails_closed(
    workspace, monkeypatch, caplog
):
    """FALSIFIABLE TWIN of the test above -- the pre-fix tree, reproduced.

    Same fixture (bars stop at `as_of - 1`), but the pre-refresh is disabled, which is
    exactly what deleting the call site does.  The preset is then the first and only cache
    reader: `compute_nav` fail-fasts, nothing is staged or pinned, and the child is still
    launched on an unwritten snapshot.  Asserting this NEGATIVE is what makes the ordering
    test above load-bearing rather than incidental.
    """
    _seed_book_missing_as_of(workspace)
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0)
    events = []
    seen = {}

    monkeypatch.setattr(
        execution_service.ExecutionService,
        "_refresh_held_book_daily_bars",
        lambda self, book, **k: {"status": "skipped", "reason": "disabled_for_test"},
    )
    _track_nav_calls(monkeypatch, events)

    captured = _patch_subprocess(
        monkeypatch, on_launch=lambda cmd: _inspect_launch(cmd, seen)
    )
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    with caplog.at_level(logging.ERROR):
        svc.run_health_check(
            strategy_file=_strategy_file(workspace),
            custom_symbols=["AAA"],
            as_of_date=AS_OF,
            data_provider=PROVIDER,
            refresh_data=True,
        )

    assert [e[0] for e in events] == ["resolve_nav"], "no refresh ran before the mark"
    assert seen["snapshot_exists"] is False
    assert seen["pinned"] is None
    assert len(captured) == 1, "fail closed, not fail silent: the gate still runs"
    assert "--nav-breakdown-file" not in captured[0]
    # The failure is the STALENESS check -- not some unrelated breakage.
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("reason=stale_price" in m for m in messages), messages


def test_a_symbol_still_missing_its_as_of_bar_after_the_refresh_fails_closed(
    workspace, monkeypatch, caplog
):
    """HONESTY GUARD: the refresh deletes the SPURIOUS stale read, never a real one.

    The refresh runs and REPORTS that it could not obtain `AAA`'s `as_of` bar (suspended /
    delisted / provider gap).  The preset must therefore STILL fail closed:

      * no snapshot written, nothing pinned, child still launched;
      * Stage 1's own verdict on that absence is `DEGRADED_NAV_UNAVAILABLE` with
        `nav_gate_nav=None` -- the form value stays a display placeholder, never the gate NAV.

    The last clause is the anti-shortcut assertion: if anyone later adds a "fall back to the
    form value / previous round's snapshot" escape, the snapshot WOULD exist and be pinned
    here, and both this test and the ordering test go red.
    """
    _seed_book_missing_as_of(workspace)
    _archive_round_with_cash(workspace, "2026-01-05", cash=0.0)
    seen = {}
    refreshed = []

    def _fake_refresh(cache_dir, symbols, *, as_of, market="china", data_provider=None):
        refreshed.append((list(symbols), as_of))
        # Deliberately writes NOTHING: the provider gap is real, not a test artefact.
        return {
            "status": "completed",
            "symbols": list(symbols),
            "fetch_errors": {"AAA": "FuyaoDataProvider: TimeoutError: provider gap"},
        }

    monkeypatch.setattr(execution_service, "refresh_held_book_daily_bars", _fake_refresh)

    captured = _patch_subprocess(
        monkeypatch, on_launch=lambda cmd: _inspect_launch(cmd, seen)
    )
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    with caplog.at_level(logging.ERROR):
        svc.run_health_check(
            strategy_file=_strategy_file(workspace),
            custom_symbols=["AAA"],
            as_of_date=AS_OF,
            portfolio_value=100000.0,
            data_provider=PROVIDER,
            refresh_data=True,
        )

    # The refresh WAS attempted (so this is not Test B in disguise) ...
    assert refreshed == [(["AAA"], AS_OF)]
    # ... and it still refused to mark an unmarkable book.
    assert seen["snapshot_exists"] is False
    assert seen["pinned"] is None
    assert "--nav-breakdown-file" not in captured[0]
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("reason=stale_price" in m for m in messages), messages

    # The child's own classifier on that absent snapshot: explicit degradation, no fallback.
    stage1 = _stage1_module()
    snapshot = os.path.join(
        _flag(captured[0], "--output-dir"), execution_service.NAV_BREAKDOWN_FILENAME
    )
    payload, status = stage1.load_nav_breakdown(snapshot)
    assert payload is None, "no snapshot may be invented"
    gate = stage1.resolve_gate_nav(payload, 100000.0, status)
    assert gate["nav_gate_status"] == stage1.NAV_GATE_STATUS_DEGRADED
    assert gate["nav_source"] == stage1.NAV_SOURCE_FORM_FALLBACK_REFUSED
    assert gate["nav_gate_nav"] is None, "the form value must NEVER become the gate NAV"
    assert gate["nav_form"] == pytest.approx(100000.0), "the form value is kept as display only"


# ---------------------------------------------------------------------------
# 2. No resolvable book -- no pin, no failure, child still launches
# ---------------------------------------------------------------------------
def test_no_seed_book_does_not_pin_and_does_not_fail(workspace, monkeypatch, caplog):
    """The brand-new-account state: `resolve_seed_holdings` -> `(None, "none")`.

    Must NOT pin (Stage 1 classifies the absence itself), must NOT write a fabricated
    snapshot, and must NOT abort the health check -- a bootstrap account is not an error.
    """
    _seed_book(workspace)
    captured = _patch_subprocess(monkeypatch)

    monkeypatch.setattr(workspace["storage"], "resolve_seed_holdings", lambda *a, **k: (None, "none"))
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        data_provider=PROVIDER,
        refresh_data=False,
    )

    assert len(captured) == 1, captured
    assert "--nav-breakdown-file" not in captured[0]
    run_dir = _flag(captured[0], "--output-dir")
    assert not os.path.exists(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME))


def test_unmarkable_book_does_not_pin_and_still_launches_the_gate(workspace, monkeypatch):
    """A valuation failure must degrade LOUDLY, not pin a bogus file or abort.

    `resolve_seed_holdings` says there is a book; the engine refuses to mark it (deleted /
    uninitialised cache).  Pre-staging is best-effort by design: no pin, child still launched,
    and Stage 1 records the failure class itself (`cache_uninitialized` etc.).
    """
    captured = _patch_subprocess(monkeypatch)  # no cache rows seeded at all
    called = {"n": 0}

    def _boom(self, *args, **kwargs):
        called["n"] += 1
        raise execution_service.NavValuationError("cache_uninitialized: no cache")

    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: ({"AAA": 100}, "operator_snapshot"),
    )
    monkeypatch.setattr(execution_service.ExecutionService, "_resolve_nav", _boom)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        data_provider=PROVIDER,
        refresh_data=False,
    )

    assert called["n"] == 1, "pre-staging must actually attempt the mark"
    assert len(captured) == 1, captured
    assert "--nav-breakdown-file" not in captured[0]
    run_dir = _flag(captured[0], "--output-dir")
    assert not os.path.exists(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME))


# ---------------------------------------------------------------------------
# 3. Stage 1's side of the contract -- "pinned" means FATAL when unreadable
# ---------------------------------------------------------------------------
def test_the_gate_ack_env_has_one_spelling():
    """The web layer and the child must agree on the escape hatch's variable name.

    They are two separate constants (the web layer cannot import the whole runner), so this
    assertion is the only thing standing between a rename and a silently dead bootstrap
    escape -- a mismatch would not error anywhere: the child would simply never see the ack
    and keep refusing, which reads as "the checkbox does nothing".
    """
    from scripts import run_live_strategy

    assert (
        execution_service.ALLOW_DEGRADED_GATE_ENV
        == run_live_strategy.ALLOW_DEGRADED_GATE_ENV
        == "LIVETRADING_ALLOW_DEGRADED_GATE"
    )


def test_stage1_treats_a_pinned_but_missing_snapshot_as_fatal(tmp_path):
    """The pin is only safe because Stage 1 REFUSES an unreadable pinned path.

    Pinning a file that Stage 1 would silently ignore is the "UNKNOWN reported as NORMAL"
    failure again, one layer up: the operator would read a NORMAL tier while no snapshot was
    ever consulted.  `load_nav_breakdown(explicit=True)` must raise instead of returning None.
    """
    import importlib.util as _ilu

    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    spec = _ilu.spec_from_file_location("check_live_portfolio_health_prestage", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # No pin (defaulted path): absence is a recognised, non-fatal state, and the
    # classification says WHICH kind of absence it is (`SNAPSHOT_NOT_YET_WRITTEN`, i.e. a
    # timing gap, versus `SNAPSHOT_MISSING`).
    assert mod.load_nav_breakdown(str(tmp_path / "absent.json")) == (
        None,
        mod.NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN,
    )
    # Pinned path: absence is a hard refusal, never a silent degradation.
    with pytest.raises(mod.NavSnapshotError):
        mod.load_nav_breakdown(str(tmp_path / "absent.json"), explicit=True)


# ---------------------------------------------------------------------------
# 3b. The refresh NEVER recreates the cache (identical rule to the read-only open)
# ---------------------------------------------------------------------------
def test_the_refresh_never_creates_a_missing_cache(tmp_path):
    """`cache_missing` is a SKIP, never a rebuild.

    A writable `DuckDBCache.__init__` `makedirs` + `_init_tables`s, so a pre-refresh that
    opened the path unconditionally would leave a brand-new EMPTY cache behind -- the exact
    hazard `open_live_valuation_cache`'s read-only open and the child's
    `allow_create_cache=False` refusal exist to prevent.  The existence check must run
    BEFORE anything is opened: `duckdb_cache` absent afterwards is the assertion.
    """
    cache_dir = tmp_path / "empty_cache_dir"
    cache_dir.mkdir()

    summary = execution_service.refresh_held_book_daily_bars(
        str(cache_dir), ["600584.SH"], as_of=AS_OF, data_provider=PROVIDER
    )

    assert summary["status"] == "skipped"
    assert summary["reason"] == "cache_missing"
    assert not os.path.exists(os.path.join(str(cache_dir), "cache.duckdb")), (
        "the pre-refresh must never leave an empty cache behind"
    )


def test_the_refresh_short_circuits_on_synthetic_data(tmp_path):
    """The repo's 100% offline rule: synthetic/csv bars are generated on read, never cached.

    Same short-circuit as the child's `refresh_stage1_data`, and it must fire BEFORE any
    filesystem or network access.
    """
    for provider in ("synthetic", "csv"):
        summary = execution_service.refresh_held_book_daily_bars(
            str(tmp_path / "does-not-exist"), ["600584.SH"], as_of=AS_OF,
            data_provider=provider,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "synthetic_or_no_cache"
    assert not os.path.exists(str(tmp_path / "does-not-exist"))

    # An unrecognised provider is NOT treated as synthetic: it falls through to the real
    # (already-gated) path, which here stops at the missing cache -- and still creates nothing.
    unknown = execution_service.refresh_held_book_daily_bars(
        str(tmp_path / "also-absent"), ["600584.SH"], as_of=AS_OF, data_provider=""
    )
    assert unknown["status"] == "skipped"
    assert unknown["reason"] == "cache_missing"
    assert not os.path.exists(str(tmp_path / "also-absent"))


# ---------------------------------------------------------------------------
# 4. (2026-11-04) A DETERMINED refusal must not be archived as "unknown"
# ---------------------------------------------------------------------------
#: The exact stderr shape of a degraded-gate Stage-2 refusal: the traceback header line
#: carries the class name and the multi-line message starts with `\n`, exactly as
#: `scripts/run_live_strategy.py::LiveGateDegradedError` is raised.
_LIVE_GATE_DEGRADED_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "/repo/scripts/run_live_strategy.py", line 2210, in main\n'
    "scripts.run_live_strategy.LiveGateDegradedError: \n"
    + "!" * 110
    + "\n"
    "[REFUSED / 拒绝出票] Stage 1 门禁净值不可用：本次回撤/熔断判定为 UNKNOWN，\n"
    "**不是**「未触发」。缺失 ≠ 未触发 —— 在未取得盯市净值前不得生成任何工单。\n"
    "  tier_status='UNKNOWN' nav_gate_status='DEGRADED_NAV_UNAVAILABLE' "
    "circuit_breaker_tier='UNKNOWN' nav_source='form_fallback_refused'\n"
    "处置: 恢复/预置 nav_breakdown.json 后重跑 Stage 1\n" + "!" * 110 + "\n"
)


def test_live_gate_degraded_error_is_classified_not_unknown():
    """A fully-determined refusal used to be archived under the generic "未识别" headline.

    `LiveGateDegradedError` is raised BEFORE any order math precisely when Stage 1 could not
    measure the gate NAV, so there is nothing unknown about it.  It must map to its own
    `error_kind` with an actionable headline/hint -- and the generic fallback wording (which
    sends the operator hunting through stderr for a cause that is already stated) must be gone.
    """
    failure = execution_service.summarize_execution_failure(
        _LIVE_GATE_DEGRADED_STDERR, returncode=1, label="Stage 2"
    )

    assert failure["error_kind"] == "gate_nav_unavailable"
    assert failure["error_kind"] != "unknown"
    assert "门禁净值不可用" in failure["error_headline"]
    assert "未识别出已知的门禁" not in failure["error_headline"]
    assert execution_service.ALLOW_DEGRADED_GATE_ENV in failure["error_hint"]
    # Not swallowed: the class name and the raw stderr still survive verbatim.
    assert "LiveGateDegradedError" in failure["error_detail"]
    # `error_excerpt` is the LAST 8 non-empty lines, so it shows the refusal body + the
    # exception line rather than the traceback header.
    assert "LiveGateDegradedError" in failure["error_excerpt"]
    assert "缺失 ≠ 未触发" in failure["error_excerpt"]

    # The same refusal quoted WITHOUT the class name (e.g. re-raised, or a summary field
    # pasted into stderr) is the same operational fact, so it maps the same way.
    quoted = (
        "ValueError: 拒绝出票: tier_status='UNKNOWN' "
        "nav_gate_status='DEGRADED_NAV_UNAVAILABLE'\n"
    )
    assert execution_service.summarize_execution_failure(quoted)["error_kind"] == (
        "gate_nav_unavailable"
    )


"""Unit tests for Live Trading Web Application: Storage, API, and Execution Service."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict

import pytest
from fastapi.testclient import TestClient

# Ensure web/livetrading and repo root are in sys.path
_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for p in [_WEB_DIR, _REPO_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from execution_service import ExecutionService
from server import create_app
from storage import (
    ProductionPathError,
    StorageManager,
    find_repo_root,
    production_guard_active,
    production_path_roots,
)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory for test data and archives.

    ``cache_dir`` is pinned here explicitly (defence in depth): without it
    ``StorageManager`` falls back to ``<repo_root>/data``, the real production
    directory holding ``cache.duckdb``.  The package-wide autouse fixture in
    ``conftest.py`` already re-points every derived directory at ``tmp_path``,
    but this fixture must stay hermetic even in isolation.
    """
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


# ---------------------------------------------------------------------------
# Hermeticity guard: the production DuckDB cache must be structurally
# unreachable from this test package.
# ---------------------------------------------------------------------------
#: Paths no web test may ever treat as a writable workspace.  ``data`` is the
#: live market cache (``data/cache.duckdb``); ``web/livetrading/data`` is the
#: live ``settings.json`` / holdings / account-state directory whose
#: ``settings.json`` itself pins ``cache_dir`` to the production cache.
_PRODUCTION_CACHE_DIR = os.path.join(_REPO_ROOT, "data")
_PRODUCTION_WEB_DATA_DIR = os.path.join(_WEB_DIR, "data")


def _canonical(path):
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _is_inside(path, root):
    path_c, root_c = _canonical(path), _canonical(root)
    return path_c == root_c or path_c.startswith(root_c + os.sep)


def _assert_hermetic(path, label):
    """Loudly fail if ``path`` can reach the production cache or live state."""
    resolved = _canonical(path)
    for forbidden in (_PRODUCTION_CACHE_DIR, _PRODUCTION_WEB_DATA_DIR):
        assert not _is_inside(resolved, forbidden), (
            f"PRODUCTION-CACHE HAZARD: {label}={resolved} resolves into the "
            f"production directory {forbidden}. A test in this suite would write to "
            f"the live DuckDB market cache (data/cache.duckdb) or live portfolio "
            f"state. Fix the hermetic autouse fixture in "
            f"web/livetrading/tests/conftest.py (LIVETRADING_CACHE_DIR / "
            f"LIVETRADING_DATA_DIR / LIVETRADING_ARCHIVE_DIR) instead of relaxing "
            f"this guard."
        )
    assert not _is_inside(resolved, _REPO_ROOT), (
        f"PRODUCTION-CACHE HAZARD: {label}={resolved} lives inside the repository "
        f"checkout {_REPO_ROOT}. Tests must keep every writable storage path under "
        f"the OS temporary root so a dropped data_provider kwarg cannot touch "
        f"data/cache.duckdb."
    )
    assert _is_inside(resolved, tempfile.gettempdir()), (
        f"PRODUCTION-CACHE HAZARD: {label}={resolved} is outside the OS temporary "
        f"root {tempfile.gettempdir()}; the test workspace is no longer hermetic."
    )
    return resolved


def test_storage_cache_dir_never_escapes_the_temp_workspace(temp_workspace):
    """GUARD: storage/execution paths must resolve under tmp, never to repo data.

    A ``StorageManager`` built with only ``repo_root`` exercises the full
    default resolution chain (environment -> checked-in ``settings.json`` ->
    ``<repo_root>/data``).  If the conftest hermetic fixture is deleted or
    renamed, or ``StorageManager``'s precedence changes, this test fails loudly
    instead of letting a later test silently write to the production cache.
    """
    storage, temp_dir = temp_workspace

    # 1. Default resolution chain (the exact code path the audit flagged).
    default_storage = StorageManager(repo_root=_REPO_ROOT)
    for attr in ("cache_dir", "data_dir", "archive_dir"):
        _assert_hermetic(getattr(default_storage, attr), f"StorageManager().{attr}")

    # 2. Explicitly the same check for the production file the layer would open.
    db_path = os.path.join(default_storage.cache_dir, "cache.duckdb")
    _assert_hermetic(db_path, "StorageManager().cache_dir/cache.duckdb")
    assert _canonical(default_storage.cache_dir) != _canonical(_PRODUCTION_CACHE_DIR)

    # 3. The shared per-test fixture must be hermetic on its own as well.
    assert _assert_hermetic(storage.cache_dir, "temp_workspace.cache_dir") == _canonical(
        os.path.join(temp_dir, "duckdb_cache")
    )
    _assert_hermetic(storage.archive_dir, "temp_workspace.archive_dir")
    _assert_hermetic(storage.data_dir, "temp_workspace.data_dir")


def test_execution_service_argv_never_targets_production_cache(temp_workspace, monkeypatch):
    """GUARD: run_live_deploy must shell out with a hermetic ``--cache-dir``.

    This mirrors the audit's worst case: no ``data_provider`` kwarg is passed, so
    the default provider chain (``"marketdb"`` when available) is exercised, and
    only the monkeypatched ``subprocess.run`` keeps the command from executing.
    """
    import subprocess

    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)

    captured_cmds = []

    def mock_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)

        class MockProc:
            returncode = 0
            stdout = ""
            stderr = ""

        return MockProc()

    monkeypatch.setattr(subprocess, "run", mock_run)

    strat_file = os.path.join(temp_dir, "guard_strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2024-05-10",
        portfolio_value=100000.0,
    )

    assert captured_cmds, "expected run_live_deploy to invoke run_live_strategy.py"
    cmd = captured_cmds[0]
    assert "--cache-dir" in cmd, f"run_live_deploy must pin --cache-dir explicitly: {cmd}"
    cache_arg = cmd[cmd.index("--cache-dir") + 1]
    _assert_hermetic(cache_arg, "run_live_deploy --cache-dir")
    assert _canonical(cache_arg) == _canonical(storage.cache_dir)


# ---------------------------------------------------------------------------
# Resolver-level production-path guard (``storage.ProductionPathError``).
#
# ``conftest.py``'s autouse fixture protects THIS package by setting
# ``LIVETRADING_{CACHE,DATA,ARCHIVE}_DIR``; the tests below prove the second,
# independent layer: ``StorageManager`` itself refuses to resolve a writable
# directory onto live production state whenever ``PYTEST_CURRENT_TEST`` is set,
# so a scratch test elsewhere in the repo (or a future module that drops the
# fixture) cannot write to ``data/cache.duckdb`` or the live portfolio dir.
# ---------------------------------------------------------------------------
#: Env vars the conftest fixture uses to isolate the workspace; cleared in the
#: tests that need to exercise the *un-isolated* resolution chain.
_ISOLATION_ENV_VARS = (
    "LIVETRADING_CACHE_DIR",
    "LIVETRADING_DATA_DIR",
    "LIVETRADING_ARCHIVE_DIR",
    "LIVETRADING_SETTINGS_DIR",
)


def _clear_isolation_env(monkeypatch, settings_dir=None):
    """Undo the conftest isolation (and any opt-in) so the raw chain is exercised.

    When `settings_dir` is given, the settings directory is re-pointed there instead of
    being deleted: deleting it would fall back to the repository's
    ``web/livetrading/data`` and pick up an operator's real ``settings.local.json``,
    making these tests machine-dependent (and, on a deployment host, non-hermetic).
    Pass ``None`` to exercise a *specific* repo_root's own settings directory.
    """
    for name in _ISOLATION_ENV_VARS + ("LIVETRADING_ALLOW_PRODUCTION_PATHS",):
        monkeypatch.delenv(name, raising=False)
    if settings_dir is not None:
        os.makedirs(settings_dir, exist_ok=True)
        monkeypatch.setenv("LIVETRADING_SETTINGS_DIR", str(settings_dir))


def _live_portfolio_dir_from_local_override(settings_dir, live_dir):
    """Write a synthetic operator override pinning `live_dir`; return its path.

    Replaces the pre-L2.16 helper that read the live portfolio directory straight out
    of the committed settings file.  The committed file is now placeholder-only, so the
    guard's second root can only be learned from the untracked local override -- which
    is exactly what this helper exercises (without ever touching a real deployment).
    """
    os.makedirs(settings_dir, exist_ok=True)
    with open(os.path.join(settings_dir, "settings.local.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "data_dir": str(live_dir),
                "archive_dir": os.path.join(str(live_dir), "runs"),
            },
            f,
        )
    return str(live_dir)


def test_pytest_guard_refuses_repo_data_cache_dir(monkeypatch, tmp_path):
    """GUARD: un-isolated resolution must not hand out ``<repo>/data`` under pytest.

    ``data_dir`` is pinned to ``tmp_path`` on purpose and the settings directory is
    empty, so only the dynamic default ``<repo_root>/data`` for ``cache_dir`` is under
    test: the resolver must refuse it with an actionable message instead of silently
    returning the production path.  Note this root is HARDCODED in
    ``production_path_roots`` -- it must keep holding even though the committed
    ``settings.json`` no longer names any production directory.
    """
    _clear_isolation_env(monkeypatch, tmp_path / "settings")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_web_livetrading.py::test_x (call)")
    assert production_guard_active() is True

    with pytest.raises(ProductionPathError) as excinfo:
        StorageManager(repo_root=_REPO_ROOT, data_dir=str(tmp_path / "data"))

    msg = str(excinfo.value)
    assert _canonical(_PRODUCTION_CACHE_DIR) in msg, msg
    assert "production market cache" in msg, msg
    assert "cache_dir=" in msg, msg
    # ... and the message must name the fix, both escape routes.
    assert "LIVETRADING_CACHE_DIR" in msg, msg
    assert "LIVETRADING_ALLOW_PRODUCTION_PATHS=1" in msg, msg
    # The refused value is the repo data dir itself, never some tmp surrogate.
    assert not _is_inside(tmp_path / "data", _PRODUCTION_CACHE_DIR)


def test_pytest_guard_refuses_live_portfolio_dir_and_wins_before_makedirs(monkeypatch, tmp_path):
    """GUARD: a live portfolio dir pinned by the LOCAL override is refused too.

    Before L2.16 this directory was read out of the committed ``settings.json``.  The
    committed file is now placeholder-only, so the operator's untracked
    ``settings.local.json`` is the only source of that second guarded root -- and that
    is what this test exercises.  The rejection must happen BEFORE ``os.makedirs``: a
    refused path may never be created on disk.
    """
    settings_dir = tmp_path / "settings"
    live_dir = _live_portfolio_dir_from_local_override(settings_dir, tmp_path / "live_portfolio")

    _clear_isolation_env(monkeypatch, settings_dir)  # re-asserts the same settings dir
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_web_livetrading.py::test_y (call)")

    created = []
    real_makedirs = os.makedirs

    def spy_makedirs(path, *args, **kwargs):
        created.append(_canonical(path))
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", spy_makedirs)

    with pytest.raises(ProductionPathError) as excinfo:
        StorageManager(repo_root=_REPO_ROOT, data_dir=str(tmp_path / "portfolio_input"))

    msg = str(excinfo.value)
    assert "live portfolio directory" in msg, msg
    assert _canonical(live_dir) in msg, msg
    # The leg that fired is the local override's `archive_dir` key (data_dir was
    # passed explicitly, so the chain reaches the archive path next).
    assert "archive_dir=" in msg, msg
    assert "settings.local.json" in msg, msg
    # The guarded root was learned without ever reading the committed template.
    roots = production_path_roots(_REPO_ROOT)
    assert any(_is_inside(live_dir, root) for _, root in roots)

    leaked = [p for p in created if _is_inside(p, live_dir)]
    assert not leaked, f"guard must fire before makedirs; created live dirs: {leaked}"


def test_pytest_guard_opt_in_allows_production_cache(monkeypatch, tmp_path):
    """``LIVETRADING_ALLOW_PRODUCTION_PATHS=1`` must restore the legacy resolution."""
    _clear_isolation_env(monkeypatch, tmp_path / "settings")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_web_livetrading.py::test_z (call)")
    monkeypatch.setenv("LIVETRADING_ALLOW_PRODUCTION_PATHS", "1")

    assert production_guard_active() is False
    storage = StorageManager(repo_root=_REPO_ROOT, data_dir=str(tmp_path / "data"))
    # Same construction that raised above now resolves exactly as production does...
    assert _canonical(storage.cache_dir) == _canonical(_PRODUCTION_CACHE_DIR)
    # ...without opening/creating anything (a path string is all we asked for).
    assert not os.path.exists(os.path.join(str(tmp_path / "settings"), "settings.json"))


def test_resolution_is_untouched_outside_pytest(monkeypatch, tmp_path):
    """ZERO-BEHAVIOUR-CHANGE PROOF: with ``PYTEST_CURRENT_TEST`` absent, legacy wins.

    This is the production path: no marker, so the guard is never even consulted and
    ``cache_dir`` still falls back to ``<repo_root>/data`` exactly as before.
    """
    _clear_isolation_env(monkeypatch, tmp_path / "settings")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert production_guard_active() is False

    storage = StorageManager(repo_root=_REPO_ROOT, data_dir=str(tmp_path / "data"))
    assert _canonical(storage.cache_dir) == _canonical(_PRODUCTION_CACHE_DIR)
    assert os.path.isdir(storage.data_dir)
    assert os.path.isdir(storage.archive_dir)


def test_pytest_guard_catches_path_aliasing_and_symlinks(monkeypatch, tmp_path):
    """``data/../data``, ``./data`` and a symlink to ``<repo>/data`` must all be caught."""
    _clear_isolation_env(monkeypatch, tmp_path / "settings")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_web_livetrading.py::test_alias (call)")

    # 1. Dot-dot aliasing through the environment-variable leg.
    aliased = os.path.join(_PRODUCTION_CACHE_DIR, "..", "data")
    monkeypatch.setenv("LIVETRADING_CACHE_DIR", aliased)
    with pytest.raises(ProductionPathError) as excinfo:
        StorageManager(
            repo_root=_REPO_ROOT,
            data_dir=str(tmp_path / "data"),
            archive_dir=str(tmp_path / "runs"),
        )
    assert "LIVETRADING_CACHE_DIR" in str(excinfo.value), str(excinfo.value)

    # 2. A symlink pointing at the production cache.
    link = tmp_path / "symlinked_cache"
    os.symlink(_PRODUCTION_CACHE_DIR, str(link), target_is_directory=True)
    monkeypatch.setenv("LIVETRADING_CACHE_DIR", str(link))
    with pytest.raises(ProductionPathError) as excinfo:
        StorageManager(
            repo_root=_REPO_ROOT,
            data_dir=str(tmp_path / "data"),
            archive_dir=str(tmp_path / "runs"),
        )
    assert _canonical(_PRODUCTION_CACHE_DIR) in str(excinfo.value), str(excinfo.value)

    # 3. The same alias on the data_dir leg (guarded before makedirs).
    monkeypatch.delenv("LIVETRADING_CACHE_DIR", raising=False)
    monkeypatch.setenv("LIVETRADING_DATA_DIR", os.path.join(_REPO_ROOT, "data", "..", "data"))
    with pytest.raises(ProductionPathError) as excinfo:
        StorageManager(repo_root=_REPO_ROOT)
    assert "data_dir=" in str(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------------------
# L2.16: the committed settings must be a placeholder template, and every default
# must resolve UNDER the repository root instead of onto production state.
# ---------------------------------------------------------------------------
_SETTINGS_PATH_KEYS = (
    "repo_root",
    "strategy_dir",
    "universe_dir",
    "data_dir",
    "archive_dir",
    "cache_dir",
    "stage1_script",
    "stage2_script",
    "python_executable",
)
#: Machine-specific path fragments that must never appear as an executable config.
#: Built from parts so this test file itself does not contain the literal it hunts for.
_MACHINE_PATH_FRAGMENTS = (("home", "stone"), ("Work", "stock"))


def _machine_path_needles():
    return [os.sep + os.sep.join(fragment) for fragment in _MACHINE_PATH_FRAGMENTS]


def test_committed_settings_json_is_placeholder_only():
    """L2.16: the committed template must leave every path key empty.

    Empty (not "a harmless default") is deliberate: a placeholder that happens to be
    an absolute path is exactly how production state got pinned in the first place.
    """
    template_path = Path(_PRODUCTION_WEB_DATA_DIR) / "settings.json"
    assert template_path.is_file(), f"missing committed settings template: {template_path}"
    data = json.loads(template_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "template must be a non-empty JSON object"
    for key in _SETTINGS_PATH_KEYS:
        value = data.get(key, "")
        assert value == "", (
            f"committed settings template must leave '{key}' empty (got {value!r}); "
            f"machine-specific paths belong in the untracked settings.local.json"
        )
        assert not os.path.isabs(str(value)), (key, value)


def test_bare_storage_defaults_stay_under_repo_root(monkeypatch, tmp_path):
    """L2.16 ACCEPTANCE: bare ``StorageManager(repo_root=tmp)`` resolves all under tmp.

    This is the structural fix for the 2026-10-22 incident: with an EMPTY settings
    directory there is no leg left that can hand out a path outside the repo.  The
    pytest marker is removed because L2.12's net deliberately refuses ``<repo>/data``
    under pytest; the property under test is the DEFAULT VALUE itself.
    """
    _clear_isolation_env(monkeypatch, settings_dir=None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    fake_repo = tmp_path / "fake_repo"
    fake_settings_dir = fake_repo / "web" / "livetrading" / "data"
    fake_settings_dir.mkdir(parents=True)
    # Same placeholder template the repository ships, in a throwaway checkout.
    shutil.copyfile(
        Path(_PRODUCTION_WEB_DATA_DIR) / "settings.json",
        fake_settings_dir / "settings.json",
    )

    storage = StorageManager(repo_root=str(fake_repo))

    for attr in ("cache_dir", "data_dir", "archive_dir"):
        resolved = getattr(storage, attr)
        assert _is_inside(resolved, fake_repo), f"{attr}={resolved} escaped {fake_repo}"
    assert _canonical(storage.data_dir) == _canonical(fake_settings_dir)
    assert _canonical(storage.archive_dir) == _canonical(fake_settings_dir / "runs")
    assert _canonical(storage.cache_dir) == _canonical(fake_repo / "data")


def test_repo_default_resolution_stays_inside_checkout(monkeypatch, tmp_path):
    """L2.16 ACCEPTANCE on the REAL checkout: defaults are repo-local, not production.

    The settings directory is an empty temp dir so an operator's real
    ``settings.local.json`` cannot influence the result; the template is therefore the
    only file layer, and it contributes nothing.
    """
    _clear_isolation_env(monkeypatch, settings_dir=tmp_path / "settings")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    storage = StorageManager(repo_root=_REPO_ROOT)

    for attr in ("cache_dir", "data_dir", "archive_dir"):
        _assert_inside_repo(getattr(storage, attr), attr)
    assert _canonical(storage.data_dir) == _canonical(_PRODUCTION_WEB_DATA_DIR)
    assert _canonical(storage.cache_dir) == _canonical(_PRODUCTION_CACHE_DIR)


def _assert_inside_repo(path, label):
    assert _is_inside(path, _REPO_ROOT), f"{label}={path} escaped the repository root"


def test_local_override_wins_over_template(monkeypatch, tmp_path):
    """L2.16: ``settings.local.json`` overrides the template; template still falls back."""
    _clear_isolation_env(monkeypatch, settings_dir=None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    fake_repo = tmp_path / "fake_repo"
    settings_dir = fake_repo / "web" / "livetrading" / "data"
    settings_dir.mkdir(parents=True)

    template_cache = tmp_path / "template_cache"
    local_cache = tmp_path / "local_cache"
    local_data = tmp_path / "local_portfolio"
    local_archive = tmp_path / "local_archive"

    (settings_dir / "settings.json").write_text(
        json.dumps({"cache_dir": str(template_cache)}), encoding="utf-8"
    )
    (settings_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "cache_dir": str(local_cache),
                "data_dir": str(local_data),
                "archive_dir": str(local_archive),
            }
        ),
        encoding="utf-8",
    )

    overridden = StorageManager(repo_root=str(fake_repo))
    assert _canonical(overridden.cache_dir) == _canonical(local_cache)
    # `data_dir` is overridable from the local file precisely because the settings
    # directory is resolved from repo_root rather than from data_dir (no chicken/egg).
    assert _canonical(overridden.data_dir) == _canonical(local_data)
    assert _canonical(overridden.archive_dir) == _canonical(local_archive)

    # Removing the override falls back to the template, still without production paths.
    (settings_dir / "settings.local.json").unlink()
    fallback = StorageManager(repo_root=str(fake_repo))
    assert _canonical(fallback.cache_dir) == _canonical(template_cache)
    assert _canonical(fallback.data_dir) == _canonical(settings_dir)  # dynamic default


def test_executable_web_config_carries_no_machine_specific_paths():
    """L2.16 ACCEPTANCE: no executable config under ``web/`` names a personal path.

    Scope notes:
      * ``web/livetrading/data`` is runtime state (git-ignored snapshots, archive runs);
        its two *configuration* files are checked explicitly below, the archived run
        artifacts are historical records and out of scope.
      * ``docs/`` retains ``file:///...`` provenance links and the L2.16/L2.17 audit
        rows that quote the production paths -- historical disclosure, not executable
        configuration, so it is not scanned here.
    """
    needles = _machine_path_needles()
    assert needles, "needle list must not be empty"

    web_root = Path(_WEB_DIR).parent  # <repo>/web
    skip_dirs = {".venv", "__pycache__", ".pytest_cache", "node_modules"}
    offenders = []
    for path in web_root.rglob("*"):
        if not path.is_file():
            continue
        if skip_dirs & set(path.parts):
            continue
        if "data" in path.parts and path.name not in (
            "settings.json",
            "settings.local.json",
            "settings.local.json.example",
        ):
            # Archived runs / holdings snapshots are runtime state, not configuration.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for needle in needles:
            if needle in text:
                offenders.append((str(path.relative_to(web_root)), needle))

    assert not offenders, (
        "machine-specific absolute paths must live in the untracked "
        f"settings.local.json / environment, never in the repository: {offenders}"
    )


def test_conftest_isolation_wins_before_the_pytest_guard(monkeypatch):
    """ORDERING: env vars are consulted before the guard, so the fixture still wins.

    ``PYTEST_CURRENT_TEST`` is genuinely set here (pytest sets it for every test) and
    the autouse fixture has exported the isolation variables.  A bare
    ``StorageManager`` therefore resolves to ``tmp_path`` and the guard never fires --
    which is exactly why the pre-existing hermeticity tests keep passing.
    """
    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest must set PYTEST_CURRENT_TEST"
    assert production_guard_active() is True
    for name in _ISOLATION_ENV_VARS:
        assert os.environ.get(name), f"conftest must export {name}"

    storage = StorageManager(repo_root=_REPO_ROOT)  # bare: no explicit dirs at all
    for attr, env_name in (
        ("cache_dir", "LIVETRADING_CACHE_DIR"),
        ("data_dir", "LIVETRADING_DATA_DIR"),
        ("archive_dir", "LIVETRADING_ARCHIVE_DIR"),
    ):
        resolved = getattr(storage, attr)
        assert _canonical(resolved) == _canonical(os.environ[env_name]), (attr, resolved)
        _assert_hermetic(resolved, f"bare StorageManager().{attr}")

    # The settings directory is isolated too, so `update_settings` can never write a
    # machine-specific settings.local.json into the checkout.
    assert _canonical(storage.settings_dir) == _canonical(os.environ["LIVETRADING_SETTINGS_DIR"])
    assert not _is_inside(storage.settings_dir, _REPO_ROOT)

    # The HARDCODED first guarded root (<repo>/data) is always present, independent of
    # any settings file content -- this is the L2.16 decoupling requirement.
    roots = production_path_roots(_REPO_ROOT)
    assert any(
        _canonical(root) == _canonical(_PRODUCTION_CACHE_DIR) for _, root in roots
    ), roots


def test_storage_manager_path_resolution(temp_workspace):
    storage, temp_dir = temp_workspace
    assert storage.repo_root == _REPO_ROOT
    assert os.path.isdir(storage.data_dir)
    assert os.path.isdir(storage.archive_dir)

    settings = storage.get_settings()
    assert settings["repo_root"] == _REPO_ROOT
    assert settings["data_dir"] == storage.data_dir
    assert settings["archive_dir"] == storage.archive_dir


def test_custom_universe_management(temp_workspace):
    storage, _ = temp_workspace

    # Initially empty
    assert len(storage.list_custom_universes()) == 0

    # Save custom universe
    univ = storage.save_custom_universe(
        name="AI Leaders",
        symbols=["NVDA", "msft", "GOOGL "],
        description="Top AI tech platforms",
    )
    assert univ["name"] == "AI Leaders"
    assert univ["symbols"] == ["NVDA", "MSFT", "GOOGL"]
    assert univ["count"] == 3

    # Check listing
    universes = storage.list_custom_universes()
    assert len(universes) == 1
    assert universes[0]["name"] == "AI Leaders"

    # Delete
    deleted = storage.delete_custom_universe("AI Leaders")
    assert deleted is True
    assert len(storage.list_custom_universes()) == 0


def test_holdings_and_account_state_daily_snapshot(temp_workspace):
    storage, _ = temp_workspace

    # 1. Global holdings
    test_holdings = {"601872.SH": 1000, "300394.SZ": 500}
    storage.save_holdings(test_holdings)
    loaded_global = storage.get_holdings()
    assert loaded_global == test_holdings

    # 2. Historical holdings snapshot for 2026-09-20
    hist_holdings = {"601872.SH": 800, "601728.SH": 2000}
    storage.save_holdings(hist_holdings, date_str="2026-09-20")

    assert storage.get_holdings(date_str="2026-09-20") == hist_holdings
    # Global holdings must remain updated
    assert storage.get_holdings() == hist_holdings

    # 3. Account state snapshot
    state_day1 = {"as_of_date": "2026-09-20", "current_nav": 105000.0, "peak_nav": 105000.0}
    storage.save_account_state(state_day1, date_str="2026-09-20")

    state_day2 = {"as_of_date": "2026-09-21", "current_nav": 102000.0, "peak_nav": 105000.0}
    storage.save_account_state(state_day2, date_str="2026-09-21")

    assert storage.get_account_state(date_str="2026-09-20")["current_nav"] == 105000.0
    assert storage.get_account_state(date_str="2026-09-21")["current_nav"] == 102000.0


def test_daily_run_archival_and_listing(temp_workspace):
    storage, _ = temp_workspace

    date_str = "2026-09-25"
    health_data = {"directive": "GO", "circuit_breaker_tier": "NORMAL", "drawdown": -0.02}
    ticket_data = [
        {"action": "SELL", "symbol": "601872.SH", "delta_shares": -200, "trade_value": -2000.0},
        {"action": "BUY", "symbol": "300394.SZ", "delta_shares": 500, "trade_value": 15000.0},
    ]
    csv_text = "action,symbol,delta_shares,trade_value\nSELL,601872.SH,-200,-2000.0\nBUY,300394.SZ,500,15000.0\n"
    summary = {"date": date_str, "status": "SUCCESS"}
    holdings = {"601872.SH": 800, "300394.SZ": 500}
    account_state = {"current_nav": 100000.0, "peak_nav": 102000.0}

    run_dir = storage.save_daily_run(
        date_str=date_str,
        health_data=health_data,
        ticket_data=ticket_data,
        csv_content=csv_text,
        summary=summary,
        holdings=holdings,
        account_state=account_state,
    )
    assert os.path.isdir(run_dir)

    # List runs
    runs = storage.list_runs()
    assert len(runs) == 1
    assert runs[0]["date"] == date_str
    assert runs[0]["directive"] == "GO"
    assert runs[0]["trade_count"] == 2

    # Get single run bundle
    bundle = storage.get_run(date_str)
    assert bundle is not None
    assert bundle["health_report"]["directive"] == "GO"
    assert len(bundle["trading_ticket"]) == 2
    assert bundle["holdings"] == holdings
    assert bundle["account_state"]["current_nav"] == 100000.0

    # Get CSV
    retrieved_csv = storage.get_run_csv(date_str)
    assert retrieved_csv == csv_text


def test_fastapi_endpoints(temp_workspace):
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # 1. GET /api/config
    res = client.get("/api/config")
    assert res.status_code == 200
    cfg = res.json()
    assert "strategies" in cfg
    assert "universes" in cfg
    assert "settings" in cfg
    assert cfg.get("default_strategy_key") == "chan_four_state_blend"

    # 2. Settings update
    res = client.post("/api/settings", json={"default_portfolio_value": 150000.0})
    assert res.status_code == 200
    assert res.json()["default_portfolio_value"] == 150000.0

    # 3. Custom universe via API
    res = client.post(
        "/api/universes",
        json={"name": "TestETF", "symbols": ["SPY", "QQQ", "BIL"], "description": "ETF basket"},
    )
    assert res.status_code == 200
    assert res.json()["name"] == "TestETF"

    # List universes includes custom
    res = client.get("/api/universes")
    assert res.status_code == 200
    names = [u["name"] for u in res.json()]
    assert any("TestETF" in n for n in names)

    # Delete custom universe
    res = client.delete("/api/universes/TestETF")
    assert res.status_code == 200

    # 4. Holdings API
    holdings_payload = {"holdings": {"AAPL": 100, "MSFT": 50}, "date": "2026-09-28"}
    res = client.post("/api/holdings", json=holdings_payload)
    assert res.status_code == 200

    res = client.get("/api/holdings?date=2026-09-28")
    assert res.status_code == 200
    assert res.json() == {"AAPL": 100, "MSFT": 50}

    # 5. Index page
    res = client.get("/")
    assert res.status_code == 200
    assert "Live Trading Station" in res.text

    # 6. Runs Archive API
    test_date = "2026-09-22"
    storage.save_daily_run(
        date_str=test_date,
        health_data={"directive": "GO"},
        ticket_data=[{"action": "BUY", "symbol": "AAPL"}],
        csv_content="action,symbol\nBUY,AAPL\n",
        holdings={"AAPL": 100},
        account_state={"current_nav": 100000.0},
    )

    res = client.get(f"/api/runs/{test_date}")
    assert res.status_code == 200
    bundle = res.json()
    assert bundle["health_report"]["directive"] == "GO"
    assert bundle["holdings"] == {"AAPL": 100}
    assert len(bundle["trading_ticket"]) == 1

    res = client.get(f"/api/runs/{test_date}/csv")
    assert res.status_code == 200
    assert "BUY,AAPL" in res.text


def test_execution_service_synthetic_runs(temp_workspace):
    """End-to-end offline execution test of Stage 1 Health and Stage 2 Live Deploy with synthetic data."""
    storage, _ = temp_workspace
    service = ExecutionService(storage)

    # Pick a standard strategy dump and universe
    strategy_file = os.path.join(
        _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps", "chan_four_state_blend_strategy.json"
    )
    if not os.path.exists(strategy_file):
        pytest.skip(f"Strategy file not found at {strategy_file}")

    # Use a small universe with 2 assets for fast synthetic testing
    custom_symbols = ["601872.SH", "300394.SZ"]
    val_date = "2024-05-10"

    # 1. Stage 1 Health Check
    health_res = service.run_health_check(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=100000.0,
        data_provider="synthetic",
    )
    assert health_res["status"] == "success"
    assert health_res["health_report"] is not None
    assert "directive" in health_res["health_report"]
    assert health_res["health_report"]["directive"] in ("GO", "CAUTION", "NO_GO")
    # (2026-10-22 live-path hardening, FIX 1 reconciliation) The coarse
    # `directive` enum is NOT the whole story: Stage 1 also emits the granular
    # `gate_code` (e.g. CAUTION_INTRADAY_DISTRIBUTION), which is what tells an
    # operator "CAUTION because measured afternoon distribution" apart from
    # "CAUTION because the drawdown tier is damping". Pin the relationship so
    # neither side can drift: the granular code must map into the coarse family.
    gate_code = health_res["health_report"].get("gate_code")
    assert gate_code, "Stage 1 must keep emitting the granular gate_code"
    assert gate_code == health_res["health_report"]["directive"] or gate_code.startswith(
        health_res["health_report"]["directive"]
    ), f"gate_code {gate_code} does not belong to directive {health_res['health_report']['directive']}"

    # 2. Stage 2 Live Deployment
    initial_holdings = {"601872.SH": 1000}
    deploy_res = service.run_live_deploy(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=100000.0,
        current_holdings=initial_holdings,
        data_provider="synthetic",
    )
    assert deploy_res["status"] == "success"
    assert deploy_res["trading_ticket"] is not None
    assert len(deploy_res["trading_ticket"]) > 0

    # Verify that artifacts were archived under runs/2024-05-10/
    archived_run = storage.get_run(val_date)
    assert archived_run is not None
    assert archived_run["health_report"] is not None
    assert len(archived_run["trading_ticket"]) > 0
    assert archived_run["holdings"] == initial_holdings
    assert archived_run["has_csv"] is True


def test_strategy_name_extraction_and_discovery(temp_workspace):
    """Test that discover_strategies returns human-readable strategy_name without .json filenames."""
    storage, temp_dir = temp_workspace

    # Create dummy strategy files with different metadata structures
    strat_dir = os.path.join(temp_dir, "custom_strats")
    os.makedirs(strat_dir, exist_ok=True)
    storage.strategy_dir = strat_dir

    # 1. Spec with research_strategy_spec.entry_data.name
    spec1 = {
        "template_name": "chan_four_state_blend",
        "research_strategy_spec": {
            "entry_data": {"name": "Chan Four-State Structural Blend"}
        },
        "params": {"cash_proxy": "BIL"},
    }
    storage._save_json(os.path.join(strat_dir, "chan_blend_strategy.json"), spec1)

    # 2. Spec with top-level strategy_name
    spec2 = {
        "strategy_name": "Dual Momentum Trend Following",
        "template_name": "dual_momentum",
        "params": {"cash_proxy": "SHV"},
    }
    storage._save_json(os.path.join(strat_dir, "dual_mom_strategy.json"), spec2)

    # 3. Spec with only template_name
    spec3 = {
        "template_name": "adaptive_asset_allocation",
        "params": {},
    }
    storage._save_json(os.path.join(strat_dir, "adaptive_strategy.json"), spec3)

    all_discovered = storage.discover_strategies()
    discovered = [
        s for s in all_discovered
        if s["filename"] in ("chan_blend_strategy.json", "dual_mom_strategy.json", "adaptive_strategy.json")
    ]
    assert len(discovered) == 3

    names = {s["strategy_name"]: s for s in discovered}
    assert "Chan Four-State Structural Blend" in names
    assert names["Chan Four-State Structural Blend"]["strategy_key"] == "chan_four_state_blend"
    assert names["Chan Four-State Structural Blend"]["is_blend"] is True
    assert names["Chan Four-State Structural Blend"]["category"] == "Blend Ensemble"
    assert not names["Chan Four-State Structural Blend"]["strategy_name"].endswith(".json")

    assert "Dual Momentum Trend Following" in names
    assert names["Dual Momentum Trend Following"]["strategy_key"] == "dual_momentum"
    assert names["Dual Momentum Trend Following"]["is_blend"] is False

    assert "Adaptive Asset Allocation" in names
    assert names["Adaptive Asset Allocation"]["strategy_key"] == "adaptive_asset_allocation"
    assert names["Adaptive Asset Allocation"]["is_blend"] is False

    # Production blend strategy must be prioritized at the top of discovered results
    assert discovered[0]["strategy_key"] == "chan_four_state_blend"


def test_keyed_archive_hierarchy_and_strict_scoping(temp_workspace):
    """Test that runs are archived under <strategy_key>/<universe_key>/<date> and strictly scoped."""
    storage, _ = temp_workspace

    date_str = "2026-09-30"

    # Save run for Strategy A + Universe A
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="strat_a",
        strategy_name="Strategy Alpha",
        universe_key="univ_a",
        universe_name="Universe Alpha",
        health_data={"directive": "GO", "score": 95},
        ticket_data=[{"action": "BUY", "symbol": "AAPL", "delta_shares": 100}],
        csv_content="action,symbol,delta_shares\nBUY,AAPL,100\n",
        holdings={"AAPL": 100},
    )

    # Save run for Strategy B + Universe B on the SAME date
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="strat_b",
        strategy_name="Strategy Beta",
        universe_key="univ_b",
        universe_name="Universe Beta",
        health_data={"directive": "CAUTION", "score": 60},
        ticket_data=[{"action": "SELL", "symbol": "MSFT", "delta_shares": -50}],
        csv_content="action,symbol,delta_shares\nSELL,MSFT,-50\n",
        holdings={"MSFT": 0},
    )

    # Verify hierarchical directory structure
    dir_a = storage.get_run_dir("strat_a", "univ_a", date_str)
    dir_b = storage.get_run_dir("strat_b", "univ_b", date_str)
    assert os.path.isdir(dir_a)
    assert os.path.isdir(dir_b)
    assert dir_a != dir_b

    # Verify Strict Scoped listing: require_both=True
    # 1. Missing one or both keys must return empty list
    assert storage.list_runs(require_both=True) == []
    assert storage.list_runs(strategy_key="strat_a", require_both=True) == []
    assert storage.list_runs(universe_key="univ_a", require_both=True) == []

    # 2. Both keys provided returns strictly matching runs
    runs_a = storage.list_runs(strategy_key="strat_a", universe_key="univ_a", require_both=True)
    assert len(runs_a) == 1
    assert runs_a[0]["strategy_key"] == "strat_a"
    assert runs_a[0]["universe_key"] == "univ_a"
    assert runs_a[0]["directive"] == "GO"

    runs_b = storage.list_runs(strategy_key="strat_b", universe_key="univ_b", require_both=True)
    assert len(runs_b) == 1
    assert runs_b[0]["strategy_key"] == "strat_b"
    assert runs_b[0]["universe_key"] == "univ_b"
    assert runs_b[0]["directive"] == "CAUTION"

    # Mismatched pair returns empty list
    assert storage.list_runs(strategy_key="strat_a", universe_key="univ_b", require_both=True) == []

    # Verify scoped get_run and get_run_csv
    bundle_a = storage.get_run(date_str, strategy_key="strat_a", universe_key="univ_a")
    assert bundle_a is not None
    assert bundle_a["health_report"]["score"] == 95
    assert bundle_a["holdings"] == {"AAPL": 100}
    assert "BUY,AAPL,100" in storage.get_run_csv(date_str, strategy_key="strat_a", universe_key="univ_a")

    bundle_b = storage.get_run(date_str, strategy_key="strat_b", universe_key="univ_b")
    assert bundle_b is not None
    assert bundle_b["health_report"]["score"] == 60
    assert bundle_b["holdings"] == {"MSFT": 0}
    assert "SELL,MSFT,-50" in storage.get_run_csv(date_str, strategy_key="strat_b", universe_key="univ_b")


def test_latest_run_and_api_scoped_routes(temp_workspace):
    """Test get_latest_run and FastAPI endpoints with scoped parameters."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # Setup 2 runs for (strat_a, univ_a): day 1 and day 2
    storage.save_daily_run(
        date_str="2026-09-28",
        strategy_key="strat_a",
        strategy_name="Strat A",
        universe_key="univ_a",
        universe_name="Univ A",
        health_data={"directive": "CAUTION", "day": 1},
    )
    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key="strat_a",
        strategy_name="Strat A",
        universe_key="univ_a",
        universe_name="Univ A",
        health_data={"directive": "GO", "day": 2},
        ticket_data=[{"action": "BUY", "symbol": "NVDA"}],
        csv_content="action,symbol\nBUY,NVDA\n",
    )

    # 1. get_latest_run
    latest = storage.get_latest_run("strat_a", "univ_a")
    assert latest is not None
    assert latest["date"] == "2026-09-29"
    assert latest["health_report"]["day"] == 2

    # None for non-existent pair
    assert storage.get_latest_run("strat_nonexistent", "univ_a") is None

    # 2. GET /api/runs?require_both=true
    res = client.get("/api/runs?require_both=true")
    assert res.status_code == 200
    assert res.json() == []

    res = client.get("/api/runs?strategy=strat_a&universe=univ_a&require_both=true")
    assert res.status_code == 200
    assert len(res.json()) == 2
    assert res.json()[0]["date"] == "2026-09-29"

    # 3. GET /api/runs/latest
    res = client.get("/api/runs/latest")
    assert res.status_code == 400

    res = client.get("/api/runs/latest?strategy=strat_a&universe=univ_a")
    assert res.status_code == 200
    assert res.json()["date"] == "2026-09-29"
    assert res.json()["health_report"]["day"] == 2

    # 4. GET /api/runs/{strategy_key}/{universe_key}/{date}
    res = client.get("/api/runs/strat_a/univ_a/2026-09-29")
    assert res.status_code == 200
    assert res.json()["date"] == "2026-09-29"
    assert len(res.json()["trading_ticket"]) == 1

    # CSV download
    res = client.get("/api/runs/strat_a/univ_a/2026-09-29/csv")
    assert res.status_code == 200
    assert "BUY,NVDA" in res.text


def test_scoped_holdings_and_account_state_api(temp_workspace):
    """Test scoped holdings and account state endpoints."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # 1. Save scoped holdings for strat_a / univ_a on 2026-09-29
    res = client.post(
        "/api/holdings",
        json={
            "holdings": {"NVDA": 200, "AAPL": 50},
            "date": "2026-09-29",
            "strategy_key": "strat_a",
            "universe_key": "univ_a",
        },
    )
    assert res.status_code == 200

    # 2. Save scoped holdings for strat_b / univ_b on same date
    res = client.post(
        "/api/holdings",
        json={
            "holdings": {"600519.SH": 300},
            "date": "2026-09-29",
            "strategy_key": "strat_b",
            "universe_key": "univ_b",
        },
    )
    assert res.status_code == 200

    # 3. Retrieve scoped holdings
    res_a = client.get("/api/holdings?date=2026-09-29&strategy=strat_a&universe=univ_a")
    assert res_a.status_code == 200
    assert res_a.json() == {"NVDA": 200, "AAPL": 50}

    res_b = client.get("/api/holdings?date=2026-09-29&strategy=strat_b&universe=univ_b")
    assert res_b.status_code == 200
    assert res_b.json() == {"600519.SH": 300}

    # 4. Scoped account state
    res = client.post(
        "/api/account-state",
        json={
            "as_of_date": "2026-09-29",
            "strategy_key": "strat_a",
            "universe_key": "univ_a",
            "current_nav": 125000.0,
            "peak_nav": 130000.0,
        },
    )
    assert res.status_code == 200

    res_state = client.get("/api/account-state?date=2026-09-29&strategy=strat_a&universe=univ_a")
    assert res_state.status_code == 200
    assert res_state.json()["current_nav"] == 125000.0


def test_stock_name_resolution_and_custom_fix(temp_workspace):
    """Test that 601872.SH resolves to 招商轮船 and legacy 'Custom' names are sanitized."""
    storage, _ = temp_workspace
    from common.universe import get_stock_name

    # 1. Base resolution
    assert get_stock_name("601872.SH") == "招商轮船"
    assert get_stock_name("000938.SZ") == "紫光股份"

    # 2. Saving a ticket that mistakenly contains 'Custom' for 601872.SH
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="adaptive_grid",
        universe_key="astock_custom",
        ticket_data=[
            {"symbol": "601872.SH", "name": "Custom", "action": "BUY", "delta_shares": 100},
            {"symbol": "000938.SZ", "name": "紫光股份", "action": "BUY", "delta_shares": 200},
        ],
    )

    # 3. Reading via get_run ensures 'Custom' is sanitized to '招商轮船'
    bundle = storage.get_run(date_str, strategy_key="adaptive_grid", universe_key="astock_custom")
    assert bundle is not None
    tickets = bundle["trading_ticket"]
    assert len(tickets) == 2
    sym_to_name = {t["symbol"]: t["name"] for t in tickets}
    assert sym_to_name["601872.SH"] == "招商轮船"
    assert sym_to_name["000938.SZ"] == "紫光股份"


def test_load_symbol_names_with_custom_universe_header(tmp_path):
    """Test that load_symbol_names never misinterprets header comments as stock names."""
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    from scripts.run_live_strategy import load_symbol_names

    # Universe file generated with header comment
    univ_file = tmp_path / "resolved_universe.txt"
    univ_file.write_text(
        "# Custom universe generated 2026-10-03T10:02:22.960270\n"
        "601872.SH\n"
        "601728.SH\n"
        "000938.SZ\n",
        encoding="utf-8",
    )

    names = load_symbol_names(str(univ_file))
    assert names["601872.SH"] == "招商轮船"
    assert names["601872.SH"] != "Custom"
    assert names["601728.SH"] == "中国电信"
    assert names["000938.SZ"] == "紫光股份"


def test_resolve_universe_materialization_and_clean_parsing(temp_workspace):
    """Test ExecutionService._resolve_universe generates clean headers and stock annotations."""
    storage, _ = temp_workspace
    service = ExecutionService(storage)

    run_dir = os.path.join(storage.archive_dir, "test_run")
    os.makedirs(run_dir, exist_ok=True)

    resolved_path = service._resolve_universe(
        universe_file="__CUSTOM__",
        custom_symbols=["601872.SH", "300394.SZ"],
        run_dir=run_dir,
    )
    assert os.path.isfile(resolved_path)

    content = open(resolved_path, "r", encoding="utf-8").read()
    assert "# ==============================================================================" in content
    assert "601872.SH" in content
    assert "招商轮船" in content

    # Verify load_symbol_names on this generated file
    from scripts.run_live_strategy import load_symbol_names
    names = load_symbol_names(resolved_path)
    assert names["601872.SH"] == "招商轮船"
    assert names["300394.SZ"] == "天孚通信"


def test_data_provider_local_storage_persistence():
    """Verify app.js includes localStorage persistence for selected data provider."""
    app_js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
    assert os.path.isfile(app_js_path)
    content = open(app_js_path, "r", encoding="utf-8").read()

    # 1. SESSION_KEYS definition includes DATA_PROVIDER
    assert "DATA_PROVIDER: \"livetrading_session_data_provider\"" in content

    # 2. saveSessionState saves selectProvider.value
    assert "localStorage.setItem(SESSION_KEYS.DATA_PROVIDER, el.selectProvider.value)" in content

    # 3. restoreSessionState restores selectProvider value
    assert "localStorage.getItem(SESSION_KEYS.DATA_PROVIDER)" in content

    # 4. change event listener on selectProvider triggers saveSessionState
    assert "el.selectProvider.addEventListener(\"change\"" in content


def test_custom_universe_key_alias_and_prefix_suffix_equivalence(temp_workspace):
    """Test that custom universe keys with prefix 'custom_' and suffix '_custom' are equivalent."""
    storage, _ = temp_workspace
    from storage import normalize_universe_key, keys_match, get_universe_key_variants

    # 1. Normalization & Matching
    assert normalize_universe_key("custom_astock_202609_20") == "astock_202609_20"
    assert normalize_universe_key("astock_202609_20_custom") == "astock_202609_20"
    assert normalize_universe_key("astock_202609_20") == "astock_202609_20"

    assert keys_match("custom_astock_202609_20", "astock_202609_20_custom") is True
    assert keys_match("astock_202609_20_custom", "custom_astock_202609_20") is True
    assert keys_match("custom_astock_202609_20", "astock_202609_20") is True
    assert keys_match("chan_four_state_blend", "CHAN_FOUR_STATE_BLEND") is True
    assert keys_match("sp500", "nasdaq100") is False

    # 2. Variants generation
    variants = get_universe_key_variants("custom_astock_202609_20")
    assert "astock_202609_20_custom" in variants
    assert "astock_202609_20" in variants

    # 3. Save run under astock_202609_20_custom on disk
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="chan_four_state_blend",
        universe_key="astock_202609_20_custom",
        ticket_data=[{"symbol": "601872.SH", "action": "BUY", "delta_shares": 100}],
        csv_content="symbol,name,action,delta_shares\n601872.SH,招商轮船,BUY,100\n",
        summary={"strategy_key": "chan_four_state_blend", "universe_key": "astock_202609_20_custom"},
    )

    # 4. Lookup using custom_astock_202609_20
    bundle = storage.get_run(date_str, strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20")
    assert bundle is not None
    assert bundle["date"] == date_str
    assert bundle["strategy_key"] == "chan_four_state_blend"

    # CSV lookup using custom_astock_202609_20
    csv_text = storage.get_run_csv(date_str, strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20")
    assert csv_text is not None
    assert "601872.SH" in csv_text

    # list_runs filtering using custom_astock_202609_20
    runs = storage.list_runs(strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20", require_both=True)
    assert len(runs) == 1
    assert runs[0]["date"] == date_str

    # get_latest_run
    latest = storage.get_latest_run("chan_four_state_blend", "custom_astock_202609_20")
    assert latest is not None
    assert latest["date"] == date_str


def test_keyed_runs_api_custom_universe_prefix_suffix_resolution(temp_workspace):
    """Test FastAPI endpoints resolve custom universe runs across prefix and suffix conventions."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # Save run under suffix convention: astock_202609_20_custom
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="chan_four_state_blend",
        universe_key="astock_202609_20_custom",
        ticket_data=[{"symbol": "601872.SH", "action": "BUY", "delta_shares": 500}],
        csv_content="symbol,name,action,delta_shares\n601872.SH,招商轮船,BUY,500\n",
        summary={"strategy_key": "chan_four_state_blend", "universe_key": "astock_202609_20_custom"},
    )

    # 1. GET /api/runs/{strategy_key}/{universe_key}/{date} with prefix 'custom_astock_202609_20'
    res = client.get(f"/api/runs/chan_four_state_blend/custom_astock_202609_20/{date_str}")
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == date_str
    assert data["strategy_key"] == "chan_four_state_blend"
    assert len(data["trading_ticket"]) == 1

    # 2. GET /api/runs/.../csv with prefix 'custom_astock_202609_20'
    res_csv = client.get(f"/api/runs/chan_four_state_blend/custom_astock_202609_20/{date_str}/csv")
    assert res_csv.status_code == 200
    assert "601872.SH" in res_csv.text

    # 3. GET /api/runs?strategy=...&universe=...&require_both=true
    res_list = client.get("/api/runs?strategy=chan_four_state_blend&universe=custom_astock_202609_20&require_both=true")
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1
    assert res_list.json()[0]["date"] == date_str

    # 4. GET /api/runs/latest?strategy=...&universe=...
    res_latest = client.get("/api/runs/latest?strategy=chan_four_state_blend&universe=custom_astock_202609_20")
    assert res_latest.status_code == 200
    assert res_latest.json()["date"] == date_str


def test_extract_post_run_holdings(temp_workspace):
    """Test extracting post-run target holdings from ticket CSV/JSON or snapshot files."""
    storage, temp_dir = temp_workspace

    # Case 1: live_trading_ticket.json with target_shares > 0
    run_dir1 = os.path.join(temp_dir, "run1")
    os.makedirs(run_dir1, exist_ok=True)
    ticket1 = [
        {"symbol": "600519.SH", "action": "BUY", "current_shares": 0, "target_shares": 200},
        {"symbol": "000858.SZ", "action": "HOLD", "current_shares": 500, "target_shares": 500},
        {"symbol": "601872.SH", "action": "SELL", "current_shares": 1000, "target_shares": 0},
        {"symbol": "BIL", "action": "HOLD", "current_shares": 100, "target_shares": 100},
    ]
    storage._save_json(os.path.join(run_dir1, "live_trading_ticket.json"), ticket1)
    h1 = storage.extract_post_run_holdings(run_dir1)
    assert h1 == {"600519.SH": 200, "000858.SZ": 500}
    assert "601872.SH" not in h1
    assert "BIL" not in h1

    # Case 2: post_holdings.json takes highest priority
    storage._save_json(os.path.join(run_dir1, "post_holdings.json"), {"601318.SH": 800})
    h2 = storage.extract_post_run_holdings(run_dir1)
    assert h2 == {"601318.SH": 800}

    # Case 3: ticket CSV only
    run_dir3 = os.path.join(temp_dir, "run3")
    os.makedirs(run_dir3, exist_ok=True)
    csv_content = (
        "symbol,name,action,target_shares\n"
        "601872.SH,招商轮船,BUY,1200\n"
        "300394.SZ,天孚通信,HOLD,600\n"
        "000001.SZ,平安银行,SELL,0\n"
    )
    with open(os.path.join(run_dir3, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write(csv_content)
    h3 = storage.extract_post_run_holdings(run_dir3)
    assert h3 == {"601872.SH": 1200, "300394.SZ": 600}
    assert "000001.SZ" not in h3


def test_previous_run_discovery_and_fallback(temp_workspace):
    """Test get_previous_run and fallback for get_holdings and get_account_state."""
    storage, _ = temp_workspace

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Day 1: 2026-10-01
    storage.save_daily_run(
        date_str="2026-10-01",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[{"symbol": "600519.SH", "action": "BUY", "target_shares": 100}],
        account_state={"current_nav": 100000.0, "peak_nav": 100000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "timestamp": "2026-10-01T15:00:00"},
    )

    # Day 2: 2026-10-03
    storage.save_daily_run(
        date_str="2026-10-03",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "600519.SH", "action": "HOLD", "target_shares": 100},
            {"symbol": "601872.SH", "action": "BUY", "target_shares": 500},
        ],
        account_state={"current_nav": 105000.0, "peak_nav": 105000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "timestamp": "2026-10-03T15:00:00"},
    )

    # 1. Day 3: 2026-10-04 (no run) -> previous run should be Day 2 (2026-10-03)
    prev = storage.get_previous_run("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert prev is not None
    assert prev["date"] == "2026-10-03"

    # Holdings for 2026-10-04 should fallback to post-trade holdings of 2026-10-03
    h = storage.get_holdings("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert h == {"600519.SH": 100, "601872.SH": 500}

    # Account state for 2026-10-04 should fallback to 2026-10-03
    acc = storage.get_account_state("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert acc["current_nav"] == 105000.0
    assert acc["peak_nav"] == 105000.0

    # 2. Date between Day 1 and Day 2: 2026-10-02 -> previous run should be Day 1 (2026-10-01)
    prev_mid = storage.get_previous_run("2026-10-02", strategy_key=strat_k, universe_key=univ_k)
    assert prev_mid is not None
    assert prev_mid["date"] == "2026-10-01"

    # 3. Date before any run: 2026-09-30 -> no previous run
    prev_none = storage.get_previous_run("2026-09-30", strategy_key=strat_k, universe_key=univ_k)
    assert prev_none is None


def test_api_previous_run_endpoint(temp_workspace):
    """Test GET /api/runs/previous endpoint returns previous available run data."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Save run for 2026-10-03
    storage.save_daily_run(
        date_str="2026-10-03",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601872.SH", "action": "BUY", "target_shares": 800},
            {"symbol": "300394.SZ", "action": "BUY", "target_shares": 400},
        ],
        account_state={"current_nav": 102000.0, "peak_nav": 102000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )

    # 1. Query for date 2026-10-04 (which has no run data)
    res = client.get(f"/api/runs/previous?date=2026-10-04&strategy={strat_k}&universe={univ_k}")
    assert res.status_code == 200
    data = res.json()
    assert data["has_previous"] is True
    assert data["previous_date"] == "2026-10-03"
    assert data["holdings"] == {"601872.SH": 800, "300394.SZ": 400}
    assert data["account_state"]["current_nav"] == 102000.0

    # 2. Query for date before 2026-10-03
    res_before = client.get(f"/api/runs/previous?date=2026-10-01&strategy={strat_k}&universe={univ_k}")
    assert res_before.status_code == 200
    data_before = res_before.json()
    assert data_before["has_previous"] is False
    assert data_before["previous_date"] is None

    # 3. Query without params should not crash
    res_empty = client.get("/api/runs/previous")
    assert res_empty.status_code == 200
    assert res_empty.json()["has_previous"] is False


def test_app_js_date_change_and_previous_fallback():
    """Verify app.js includes handleDateSelected, calls /api/runs/previous, and updates holdings."""
    app_js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
    assert os.path.isfile(app_js_path)
    content = open(app_js_path, "r", encoding="utf-8").read()

    # 1. handleDateSelected is defined
    assert "async function handleDateSelected(targetDate" in content

    # 2. handleDateSelected fetches /api/runs/previous
    assert "/api/runs/previous?date=" in content

    # 3. handleDateSelected updates state.holdings and calls renderHoldingsTable
    assert "renderHoldingsTable(state.holdings)" in content
    assert "recalcHoldings()" in content

    # 4. handleDateSelected resets displays when no run exists
    assert "resetRunDisplaysToAwaiting(" in content

    # 5. onTopDateChange calls handleDateSelected
    assert "await handleDateSelected(newDate, true)" in content


def test_empty_date_dir_is_not_treated_as_valid_run_and_falls_back_to_previous(temp_workspace):
    """Test that an empty date directory on disk (e.g. 10-03) is not treated as a valid run and falls back to 09-29."""
    storage, temp_dir = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Day 1: 2026-09-29 with valid ticket and account state
    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601288.SH", "action": "SELL", "current_shares": 4900, "target_shares": 3300},
            {"symbol": "300394.SZ", "action": "HOLD", "current_shares": 100, "target_shares": 100},
        ],
        account_state={"current_nav": 203049.96, "peak_nav": 203049.96, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )

    # Day 2: 2026-10-03 - empty directory created on disk without any run files
    empty_1003_dir = storage.get_run_dir(strat_k, univ_k, "2026-10-03", prefer_existing=False)
    os.makedirs(empty_1003_dir, exist_ok=True)
    assert os.path.exists(empty_1003_dir)

    # 1. get_run for 2026-10-03 must return None because it has no artifacts
    assert storage.get_run("2026-10-03", strategy_key=strat_k, universe_key=univ_k) is None

    # 2. GET /api/runs/.../2026-10-03 must return 404
    res_run = client.get(f"/api/runs/{strat_k}/{univ_k}/2026-10-03")
    assert res_run.status_code == 404

    # 3. GET /api/runs/previous for 2026-10-03 must return 2026-09-29 post-trade holdings
    res_prev = client.get(f"/api/runs/previous?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res_prev.status_code == 200
    data_prev = res_prev.json()
    assert data_prev["has_previous"] is True
    assert data_prev["previous_date"] == "2026-09-29"
    assert data_prev["holdings"] == {"601288.SH": 3300, "300394.SZ": 100}
    assert data_prev["account_state"]["current_nav"] == 203049.96

    # 4. GET /api/holdings for 2026-10-03 must also fall back to 2026-09-29 holdings
    res_h = client.get(f"/api/holdings?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res_h.status_code == 200
    assert res_h.json() == {"601288.SH": 3300, "300394.SZ": 100}


def test_quotes_endpoint_and_storage_quotes(temp_workspace):
    """Test get_quotes method and /api/quotes GET/POST endpoints."""
    storage, temp_dir = temp_workspace
    client = TestClient(create_app(storage))

    # 1. Direct storage.get_quotes with synthetic data
    res = storage.get_quotes(
        symbols=["AAPL", "BIL"],
        as_of_date="2026-09-29",
        data_provider="synthetic",
    )
    assert "quotes" in res
    assert res["quotes"]["BIL"]["price"] == 1.0
    assert res["quotes"]["BIL"]["source"] == "cash_proxy"
    assert "AAPL" in res["quotes"]
    assert res["quotes"]["AAPL"]["price"] is not None
    assert res["quotes"]["AAPL"]["price"] > 0

    # 2. GET /api/quotes
    res_get = client.get("/api/quotes?symbols=AAPL,BIL&as_of_date=2026-09-29&data_provider=synthetic")
    assert res_get.status_code == 200
    data_get = res_get.json()
    assert "quotes" in data_get
    assert data_get["quotes"]["BIL"]["price"] == 1.0
    assert data_get["quotes"]["AAPL"]["price"] is not None

    # 3. POST /api/quotes
    res_post = client.post("/api/quotes", json={
        "symbols": ["AAPL", "BIL"],
        "as_of_date": "2026-09-29",
        "data_provider": "synthetic"
    })
    assert res_post.status_code == 200
    data_post = res_post.json()
    assert "quotes" in data_post
    assert data_post["quotes"]["BIL"]["price"] == 1.0
    assert data_post["quotes"]["AAPL"]["price"] is not None


def test_get_quotes_pins_the_expected_session_and_flags_a_stale_bar(temp_workspace, monkeypatch):
    """(L2.3) The LIVE quote path must ask the cache for the *expected* session.

    A display price computed from an older bar is tolerable, but it must be labelled
    ``stale`` and carry the session it was supposed to be -- the 2026-10-22 defect in
    display form was a 10-21 close looking like a 10-22 price with no marker at all.
    """
    import datetime as dt

    import pandas as pd

    import common.data as common_data

    storage, _temp_dir = temp_workspace
    calls = {}

    def _fake_load_universe(symbols, start=None, end=None, interval="1d", **kwargs):
        calls["symbols"] = list(symbols)
        calls["expected_last_session"] = kwargs.get("expected_last_session")
        idx = pd.DatetimeIndex(["2026-09-25"])
        return {s: pd.DataFrame({"Close": [123.0]}, index=idx.copy()) for s in symbols}

    monkeypatch.setattr(common_data, "load_universe", _fake_load_universe)

    res = storage.get_quotes(["AAPL"], as_of_date="2026-09-29", data_provider="synthetic")
    quote = res["quotes"]["AAPL"]

    # The session was DEMANDED, not inferred from whatever happened to be on disk.
    assert calls["expected_last_session"] == dt.date(2026, 9, 29)
    assert quote["expected_session"] == "2026-09-29"
    assert quote["date"] == "2026-09-25"
    assert quote["stale"] is True


def test_get_quotes_without_an_explicit_date_uses_the_market_clock(temp_workspace, monkeypatch):
    """No `as_of_date` => the market clock's session, never the host's `date.today()`."""
    import datetime as dt

    import pandas as pd

    import common.data as common_data
    import common.market_clock as market_clock_module

    storage, _temp_dir = temp_workspace
    session = dt.date(2026, 9, 29)

    real_expected = market_clock_module.expected_last_session

    def _frozen(market="china", now=None, **kwargs):
        return real_expected(
            market,
            now=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.timezone.utc),
            **kwargs,
        )

    monkeypatch.setattr(market_clock_module, "expected_last_session", _frozen)

    calls = {}

    def _fake_load_universe(symbols, start=None, end=None, interval="1d", **kwargs):
        calls["expected_last_session"] = kwargs.get("expected_last_session")
        idx = pd.DatetimeIndex([session.isoformat()])
        return {s: pd.DataFrame({"Close": [123.0]}, index=idx.copy()) for s in symbols}

    monkeypatch.setattr(common_data, "load_universe", _fake_load_universe)

    res = storage.get_quotes(["AAPL"], data_provider="synthetic")
    assert res["as_of_date"] == session.isoformat()
    assert calls["expected_last_session"] == session
    assert res["quotes"]["AAPL"]["stale"] is False
    assert res["quotes"]["AAPL"]["expected_session"] == session.isoformat()


def test_previous_run_includes_prices(temp_workspace):
    """Test that /api/runs/previous returns prices mapping along with holdings and account state."""
    storage, temp_dir = temp_workspace
    client = TestClient(create_app(storage))

    strat_k = "test_strat"
    univ_k = "test_univ"

    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601288.SH", "action": "SELL", "price": 6.94, "current_shares": 4900, "target_shares": 3300},
            {"symbol": "300394.SZ", "action": "HOLD", "price": 255.99, "current_shares": 100, "target_shares": 100},
            {"symbol": "BIL", "action": "RELEASE_CASH", "price": 1.0, "current_shares": 0, "target_shares": 0},
        ],
        account_state={"current_nav": 203049.96, "peak_nav": 203049.96},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "data_provider": "marketdb"},
    )

    res = client.get(f"/api/runs/previous?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res.status_code == 200
    data = res.json()
    assert data["has_previous"] is True
    assert data["previous_date"] == "2026-09-29"
    assert data["holdings"] == {"601288.SH": 3300, "300394.SZ": 100}
    assert "prices" in data
    assert data["prices"]["601288.SH"] == 6.94
    assert data["prices"]["300394.SZ"] == 255.99
    assert data["prices"]["BIL"] == 1.0


def test_app_js_holdings_value_and_provider_logic():
    """Verify app.js includes price handling, value calculation, and quote fetching."""
    js_path = os.path.join(find_repo_root(), "web", "livetrading", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "fetchQuotesForCurrentHoldings" in content
    assert "priceMap" in content
    assert "dataset.price" in content
    assert "valTd.dataset.val" in content
    assert "totalEquity += rowVal" in content


def test_manual_same_day_holdings_override_ticket_fills(temp_workspace):
    """Verify manual same-day holdings save overrides assumed ticket fills."""
    storage, _ = temp_workspace
    strat_k = "test_strat"
    univ_k = "test_univ"
    date_str = "2026-10-07"

    # 1. Run completed with ticket targets 50 AAPL, 20 MSFT
    run_dir = storage.get_run_dir(strat_k, univ_k, date_str)
    os.makedirs(run_dir, exist_ok=True)
    storage._save_json(os.path.join(run_dir, "post_holdings.json"), {"AAPL": 50, "MSFT": 20})

    # 2. User manually corrects actual fill: only 30 AAPL filled, 0 MSFT
    storage.save_holdings({"AAPL": 30}, date_str=date_str, strategy_key=strat_k, universe_key=univ_k)

    # 3. get_holdings for same date MUST return user's explicit snapshot {"AAPL": 30}
    loaded = storage.get_holdings(date_str, strategy_key=strat_k, universe_key=univ_k)
    assert loaded == {"AAPL": 30}


def test_path_traversal_sanitization(temp_workspace):
    """Verify path traversal in strategy_key, universe_key, or date_str is blocked and sanitized."""
    storage, _ = temp_workspace

    # Keys containing path traversal sequences are sanitized
    run_dir = storage.get_run_dir("../../escape", "../evil_univ", "2026-10-07")
    # Must remain strictly inside archive_dir
    real_archive = os.path.realpath(storage.archive_dir)
    real_run = os.path.realpath(run_dir)
    assert os.path.commonpath([real_archive, real_run]) == real_archive
    assert ".." not in run_dir

    # Invalid date string raises ValueError
    with pytest.raises(ValueError):
        storage.save_holdings({"AAPL": 10}, date_str="../../etc/passwd", strategy_key="strat", universe_key="univ")


def test_previous_run_strict_universe_scoping_no_cross_contamination(temp_workspace):
    """Verify get_previous_run never returns runs from a different universe when both keys are supplied."""
    storage, _ = temp_workspace

    # Archive a run for strat_a with china_stocks
    storage.save_daily_run(
        date_str="2026-10-01",
        strategy_key="strat_a",
        universe_key="china_stocks",
        ticket_data=[{"symbol": "601872.SH", "action": "BUY", "target_shares": 1000}],
        summary={"strategy_key": "strat_a", "universe_key": "china_stocks"},
    )

    # Query previous run for strat_a with us_stocks (which has no runs)
    prev = storage.get_previous_run("2026-10-05", strategy_key="strat_a", universe_key="us_stocks")
    assert prev is None


def test_full_liquidation_carry_forward(temp_workspace):
    """Verify full liquidation (target_shares=0) carries forward as empty holdings, not resurrecting old holdings."""
    storage, _ = temp_workspace
    strat_k = "strat_liq"
    univ_k = "univ_liq"

    # Day 1: Hold 100 shares of AAPL
    storage.save_daily_run(
        date_str="2026-10-01",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[{"symbol": "AAPL", "action": "BUY", "current_shares": 0, "target_shares": 100}],
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )

    # Day 2: Liquidate AAPL to 0
    run_dir_day2 = storage.save_daily_run(
        date_str="2026-10-02",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[{"symbol": "AAPL", "action": "SELL", "current_shares": 100, "target_shares": 0}],
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )
    # Target holdings from ticket should be empty dict {}
    post_h = storage.extract_post_run_holdings(run_dir_day2)
    assert post_h == {}

    # Day 3: Query holdings for 2026-10-03 -> must carry forward {} (no AAPL revived)
    h_day3 = storage.get_holdings("2026-10-03", strategy_key=strat_k, universe_key=univ_k)
    assert h_day3 == {}


def test_multi_strategy_configs_and_netting_endpoints(temp_workspace):
    """Verify multi-strategy configuration endpoints and netting execution."""
    storage, _ = temp_workspace
    client = TestClient(create_app(storage))

    # 1. GET /api/config includes multi_strategy_presets
    res_cfg = client.get("/api/config")
    assert res_cfg.status_code == 200
    cfg_data = res_cfg.json()
    assert "multi_strategy_presets" in cfg_data
    assert len(cfg_data["multi_strategy_presets"]) >= 3
    matrix_ids = [m["matrix_id"] for m in cfg_data["multi_strategy_presets"]]
    assert "tri_core_blend" in matrix_ids

    # 2. GET /api/multi-strategy/configs
    res_m = client.get("/api/multi-strategy/configs")
    assert res_m.status_code == 200
    configs = res_m.json()
    assert len(configs) >= 3

    # 3. POST /api/multi-strategy/configs
    new_preset = {
        "matrix_id": "test_matrix",
        "name": "Test Custom Matrix",
        "description": "Test Matrix Description",
        "portfolio_value": 150000.0,
        "strategies": [
            {"strategy_key": "chan_crisis_shield_blend", "capital_weight": 0.60},
            {"strategy_key": "chan_risk_managed_blend", "capital_weight": 0.40},
        ],
    }
    res_save = client.post("/api/multi-strategy/configs", json=new_preset)
    assert res_save.status_code == 200
    saved_list = res_save.json()
    assert any(m["matrix_id"] == "test_matrix" for m in saved_list)

    # 4. Storage get_multi_netting_run_dir
    run_dir = storage.get_multi_netting_run_dir("test_matrix", "2026-10-12")
    assert "multi_netted_test_matrix" in run_dir
    os.makedirs(run_dir, exist_ok=True)

    # 5. Populate netting run files
    netted_ticket = [
        {
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "action": "BUY",
            "price": 1800.0,
            "current_weight": 0.0,
            "target_weight": 0.25,
            "delta_weight": 0.25,
            "current_shares": 0,
            "target_shares": 200,
            "delta_shares": 200,
            "crossed_shares": 100,
            "trade_value": 360000.0,
            "reason": "多策略净额加仓",
        },
        {
            "symbol": "000858.SZ",
            "name": "五粮液",
            "action": "HOLD_FILTERED",
            "price": 150.0,
            "current_weight": 0.15,
            "target_weight": 0.15,
            "delta_weight": 0.0,
            "current_shares": 1000,
            "target_shares": 1000,
            "delta_shares": 0,
            "crossed_shares": 0,
            "trade_value": 0.0,
            "reason": "全局调仓未超惰性阈值 (< 5%)",
        },
    ]
    friction_savings = {
        "total_gross_shares": 500,
        "total_net_shares": 300,
        "total_crossed_shares": 100,
        "churn_reduction_ratio": 0.40,
        "total_gross_turnover": 720000.0,
        "total_net_turnover": 360000.0,
        "saved_turnover": 360000.0,
        "saved_stamp_duty": 180.0,
        "saved_commission": 216.0,
        "saved_slippage": 360.0,
        "total_friction_saved": 756.0,
    }
    sub_ledgers = {
        "chan_crisis_shield_blend": {
            "strategy_key": "chan_crisis_shield_blend",
            "strategy_name": "危机盾牌对冲策略",
            "capital_weight": 0.60,
            "allocated_nav": 600000.0,
            "virtual_equity_value": 360000.0,
            "virtual_cash": 240000.0,
            "equity_ratio": 0.60,
            "cash_ratio": 0.40,
            "holdings": {"600519.SH": 200},
        }
    }
    storage._save_json(os.path.join(run_dir, "netted_trading_ticket.json"), netted_ticket)
    storage._save_json(os.path.join(run_dir, "friction_savings_report.json"), friction_savings)
    storage._save_json(os.path.join(run_dir, "sub_ledgers.json"), sub_ledgers)
    storage._save_json(os.path.join(run_dir, "summary.json"), {
        "matrix_id": "test_matrix",
        "date": "2026-10-12",
        "directive": "GO",
        "net_orders_count": 2,
    })

    csv_text = (
        "symbol,name,action,price,target_shares,delta_shares,crossed_shares,reason\n"
        "600519.SH,贵州茅台,BUY,1800.0,200,200,100,多策略净额加仓\n"
        "000858.SZ,五粮液,HOLD_FILTERED,150.0,1000,0,0,全局调仓未超惰性阈值 (< 5%)\n"
    )
    with open(os.path.join(run_dir, "netted_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write(csv_text)

    # 6. GET /api/deploy/multi/latest
    res_latest = client.get("/api/deploy/multi/latest?matrix_id=test_matrix&date=2026-10-12")
    assert res_latest.status_code == 200
    bundle = res_latest.json()
    assert len(bundle["trading_ticket"]) == 2
    assert bundle["friction_savings"]["total_crossed_shares"] == 100
    assert bundle["sub_ledgers"]["chan_crisis_shield_blend"]["capital_weight"] == 0.60

    # 7. GET /api/deploy/multi/latest/csv
    res_csv = client.get("/api/deploy/multi/latest/csv?matrix_id=test_matrix&date=2026-10-12")
    assert res_csv.status_code == 200
    assert "attachment; filename=netted_trading_ticket_test_matrix_2026-10-12.csv" in res_csv.headers.get("content-disposition", "")
    assert "600519.SH" in res_csv.text

    # 8. Transparent load via storage.get_run and get_run_csv
    run_arch = storage.get_run("2026-10-12", strategy_key="test_matrix")
    assert run_arch is not None
    assert len(run_arch["trading_ticket"]) == 2
    assert run_arch["friction_savings"]["total_friction_saved"] == 756.0

    csv_arch = storage.get_run_csv("2026-10-12", strategy_key="test_matrix")
    assert csv_arch is not None
    assert "600519.SH" in csv_arch


def test_same_day_rerun_idempotency(temp_workspace):
    """Test that re-running Stage 1 and Stage 2 on the same day produces strictly idempotent results."""
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage)

    strategy_file = os.path.join(
        _REPO_ROOT,
        "pipeline",
        "research_strategy",
        "results",
        "strategy_dumps",
        "chan_four_state_blend_strategy.json",
    )
    if not os.path.exists(strategy_file):
        pytest.skip(f"Strategy file not found at {strategy_file}")

    custom_symbols = ["601872.SH", "300394.SZ"]
    val_date = "2024-05-10"

    # Seed an initial baseline state from prior date
    prior_state = {
        "as_of_date": "2024-05-09",
        "current_nav": 100000.0,
        "peak_nav": 100000.0,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [100000.0] * 5,
        "holding_bars": {"601872.SH": 3},
    }
    storage.save_account_state(prior_state, date_str="2024-05-09", strategy_key="chan_four_state_blend", universe_key="custom")
    storage._save_json(storage.account_state_file, prior_state)

    # -------------------------------------------------------------
    # 1. Stage 1 Health Check - Run 1 (Drawdown triggers Tier 1)
    # -------------------------------------------------------------
    res1 = service.run_health_check(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=88000.0,
        peak_nav=100000.0,
        data_provider="synthetic",
    )
    assert res1["status"] == "success"
    st1 = res1["account_state"]
    assert st1["tier1_consecutive_bars"] == 1
    assert len(st1["nav_history_10d"]) == 6
    assert st1["nav_history_10d"][-1] == 88000.0

    # -------------------------------------------------------------
    # 2. Stage 1 Health Check - Run 2 (Same day re-run must be idempotent!)
    # -------------------------------------------------------------
    res2 = service.run_health_check(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=88000.0,
        peak_nav=100000.0,
        data_provider="synthetic",
    )
    assert res2["status"] == "success"
    st2 = res2["account_state"]
    # tier1_consecutive_bars MUST remain 1, NOT increment to 2!
    assert st2["tier1_consecutive_bars"] == 1
    # nav_history_10d MUST NOT duplicate 88000.0!
    assert len(st2["nav_history_10d"]) == 6
    assert st2["nav_history_10d"][-1] == 88000.0
    assert res2["health_report"]["portfolio_health"]["tier1_counter"] == 1

    # -------------------------------------------------------------
    # 3. Stage 2 Live Deploy - Run 1
    # -------------------------------------------------------------
    initial_holdings = {"601872.SH": 1000}
    dep1 = service.run_live_deploy(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=88000.0,
        current_holdings=initial_holdings,
        data_provider="synthetic",
    )
    assert dep1["status"] == "success"
    ticket1 = dep1["trading_ticket"]
    assert ticket1 is not None and len(ticket1) > 0

    # Verify storage.get_run returns pre-trade holdings for UI reconciliation
    run_bundle = storage.get_run(val_date)
    assert run_bundle is not None
    assert run_bundle["holdings"] == initial_holdings
    assert "post_holdings" in run_bundle
    assert isinstance(run_bundle["post_holdings"], dict)

    # -------------------------------------------------------------
    # 4. Stage 2 Live Deploy - Run 2 (Same day re-run with same pre-trade holdings)
    # -------------------------------------------------------------
    dep2 = service.run_live_deploy(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=88000.0,
        current_holdings=run_bundle["holdings"],
        data_provider="synthetic",
    )
    assert dep2["status"] == "success"
    ticket2 = dep2["trading_ticket"]
    assert ticket2 is not None

    # Actions and target shares must be strictly identical across re-runs
    sym_to_action1 = {t["symbol"]: (t["action"], t.get("target_shares"), t.get("delta_shares")) for t in ticket1}
    sym_to_action2 = {t["symbol"]: (t["action"], t.get("target_shares"), t.get("delta_shares")) for t in ticket2}
    assert sym_to_action1 == sym_to_action2


def test_parse_ticket_csv_detail_reason(tmp_path):
    """Verify parse_ticket_csv parses detail_reason JSON string into dict."""
    import json
    from web.livetrading.storage import parse_ticket_csv

    csv_file = tmp_path / "live_trading_ticket.csv"
    dr_data = {
        "summary_reason": "突破均线买入",
        "dimension_signals": {"trend": 0.6},
        "risk_checks": {"passed": True},
    }
    content = (
        "symbol,action,price,current_shares,target_shares,delta_shares,detail_reason\n"
        f'600519,BUY,1800.0,0,100,100,"{json.dumps(dr_data).replace(chr(34), chr(34)+chr(34))}"\n'
    )
    csv_file.write_text(content, encoding="utf-8")
    records = parse_ticket_csv(str(csv_file))
    assert len(records) == 1
    assert records[0]["symbol"] == "600519"
    assert isinstance(records[0]["detail_reason"], dict)
    assert records[0]["detail_reason"]["summary_reason"] == "突破均线买入"
    assert records[0]["detail_reason"]["dimension_signals"]["trend"] == 0.6


def test_run_strategy_request_guard_latency_bars():
    """Verify RunDeployRequest and RunStrategyRequest models support guard_latency_bars."""
    from web.livetrading.models import RunDeployRequest, RunStrategyRequest

    req = RunDeployRequest(strategy_file="dummy.json")
    assert req.guard_latency_bars == 2

    req2 = RunStrategyRequest(strategy_file="dummy.json", guard_latency_bars=3)
    assert req2.guard_latency_bars == 3


def test_execution_service_passes_guard_latency_bars(temp_workspace, monkeypatch):
    """Verify execution_service passes --guard-latency-bars to run_live_strategy.py."""
    import subprocess
    storage, temp_dir = temp_workspace
    service = ExecutionService(storage=storage)

    captured_cmds = []
    def mock_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        class MockProc:
            returncode = 0
            stdout = ""
            stderr = ""
        return MockProc()

    monkeypatch.setattr(subprocess, "run", mock_run)

    strat_file = os.path.join(temp_dir, "test_strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    service.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2024-05-10",
        portfolio_value=100000.0,
        guard_latency_bars=4,
    )

    assert len(captured_cmds) == 1
    assert "--guard-latency-bars" in captured_cmds[0]
    idx = captured_cmds[0].index("--guard-latency-bars")
    assert captured_cmds[0][idx + 1] == "4"









def _freeze_market_clock(monkeypatch, session):
    """Pin `common.market_clock.expected_last_session` to one fixed session.

    The frozen date is deliberately NOT the host's `date.today()`: if any
    archive-key resolver still falls back to the host calendar, the assertions
    below fail instead of coincidentally matching.
    """
    import datetime as dt

    import common.market_clock as market_clock_module

    real_expected = market_clock_module.expected_last_session

    def _frozen(market="china", now=None, **kwargs):
        return real_expected(
            market,
            now=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.timezone.utc),
            **kwargs,
        )

    monkeypatch.setattr(market_clock_module, "expected_last_session", _frozen)
    assert session != dt.date.today(), "frozen session must differ from the host's today"
    return _frozen


def test_archive_date_key_has_exactly_one_source(temp_workspace, monkeypatch):
    """L2.5: writer and reader resolve the SAME archive date key (never `date.today()`).

    Covers the three places that used to drift apart independently --
    `market_clock_date`, `get_run_dir`, and `get_multi_netting_run_dir` -- plus the
    malformed/omitted fallbacks that used to substitute a host-local day.
    """
    import datetime as dt

    storage, _temp_dir = temp_workspace
    session = dt.date(2026, 9, 30)
    _freeze_market_clock(monkeypatch, session)

    key = storage.market_clock_date()
    assert key == session.isoformat()

    # 1. Single-run directory: the date component IS the clock key.
    run_dir = storage.get_run_dir("strat_a", "univ_a", key)
    assert os.path.basename(run_dir) == key

    # 2. Multi-netting directory: same key, different layout.
    multi_dir = storage.get_multi_netting_run_dir("test_matrix", key)
    assert os.path.basename(os.path.dirname(multi_dir)) == key

    # 3. An OMITTED date resolves to the SAME key instead of the host calendar...
    assert storage.get_multi_netting_run_dir("test_matrix", None) == multi_dir
    assert storage.get_multi_netting_run_dir("test_matrix", "") == multi_dir
    # ...while a MALFORMED one is rejected outright (it is a path component, so
    # substituting any day would hide the caller's bug, not fix it).
    with pytest.raises(ValueError):
        storage.get_multi_netting_run_dir("test_matrix", "not-a-date")


def test_netting_latest_endpoints_default_to_the_market_clock(temp_workspace, monkeypatch):
    """L2.5: `/api/deploy/multi/latest[/csv]` with no `date` reads the writer's key.

    Both endpoints (and the JSON + CSV lookups behind them) used
    `date.today().isoformat()`; on a weekend, or whenever the host calendar and the
    last published session split, they 404'd on a run that existed.
    """
    import datetime as dt

    storage, _temp_dir = temp_workspace
    session = dt.date(2026, 9, 30)
    _freeze_market_clock(monkeypatch, session)

    run_dir = storage.get_multi_netting_run_dir("test_matrix", session.isoformat())
    os.makedirs(run_dir, exist_ok=True)
    storage._save_json(os.path.join(run_dir, "netted_trading_ticket.json"), [{"symbol": "600519.SH", "action": "BUY"}])
    storage._save_json(os.path.join(run_dir, "friction_savings_report.json"), {})
    storage._save_json(os.path.join(run_dir, "sub_ledgers.json"), {})
    storage._save_json(os.path.join(run_dir, "summary.json"), {"matrix_id": "test_matrix", "date": session.isoformat()})
    with open(os.path.join(run_dir, "netted_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write("symbol,action\n600519.SH,BUY\n")

    client = TestClient(create_app(storage))

    res = client.get("/api/deploy/multi/latest?matrix_id=test_matrix")
    assert res.status_code == 200, res.text
    assert res.json()["date"] == session.isoformat()

    res_csv = client.get("/api/deploy/multi/latest/csv?matrix_id=test_matrix")
    assert res_csv.status_code == 200, res_csv.text
    disposition = res_csv.headers["content-disposition"]
    assert session.isoformat() in disposition
    assert dt.date.today().isoformat() not in disposition


# ---------------------------------------------------------------------------
# L2.20 / L2.25 -- ticket action badges: three distinct states, and the badge is
# never derived from `target_weight == 0`.
#
# FORENSIC CONTEXT (why these tests are behavioural, not string greps): the
# pre-L2.20 badge logic handled only BUY / SELL / HOLD_FILTERED / cash, and
# everything else fell through to a single generic `⚪ HOLD (维持)` badge. That
# made `CANCEL_PENDING` (an aborted PENDING_ENTRY: never held, no order) and a
# market-closed freeze row (nothing evaluated at all) render IDENTICALLY to a
# genuine "position maintained" row -- i.e. the two states an operator most needs
# to tell apart were collapsed into the one that requires no action. Both of them
# carry `target_weight == 0.0`, so a weight-based renderer cannot recover them.
# ---------------------------------------------------------------------------


def _app_js_source() -> str:
    js_path = os.path.join(find_repo_root(), "web", "livetrading", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        return f.read()


def _js_brace_block(source: str, start: int) -> str:
    """Return the balanced `{...}` block that begins at `start` (inclusive)."""
    assert source[start] == "{", "expected a '{' at the start of a balanced block"
    depth = 0
    for idx in range(start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError("unbalanced braces while extracting a JS block from app.js")


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    idx = source.find(marker)
    assert idx >= 0, f"`{name}` is missing from app.js"
    brace = source.find("{", idx)
    return f"function {name}(" + source[source.find("(", idx) + 1 : brace] + _js_brace_block(source, brace)


def _extract_js_const_object(source: str, name: str) -> str:
    idx = source.find(f"const {name} = ")
    assert idx >= 0, f"`const {name}` is missing from app.js"
    brace = source.find("{", idx)
    return f"const {name} = " + _js_brace_block(source, brace) + ";"


def _run_badge_resolver(rows):
    """Execute the REAL `resolveTicketActionBadge` from app.js under node.

    Returns the list of `{key, cls, text}` verdicts, or `None` when no JS runtime
    exists -- in that case the caller falls back to a structural assertion that
    still fails against the pre-L2.20 source (which contains neither label).
    Running the shipped function (instead of asserting on substrings) is what
    makes these tests able to distinguish "badge decided by action" from "badge
    decided by anything else".
    """
    import subprocess

    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        return None
    src = _app_js_source()
    script = "\n".join(
        [
            _extract_js_const_object(src, "BLOCKING_REASON_LABELS"),
            _extract_js_function(src, "resolveTicketActionBadge"),
            f"const rows = {json.dumps(rows)};",
            "process.stdout.write(JSON.stringify(rows.map(resolveTicketActionBadge)));",
        ]
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"extracted app.js badge resolver failed under node: {proc.stderr}"
    return json.loads(proc.stdout)


def test_app_js_badge_distinguishes_hold_freeze_and_cancelled_entry():
    """Three states, three DIFFERENT badges (fails against the pre-L2.20 source)."""
    rows = [
        {"action": "HOLD", "blocking_reason": "NONE", "target_weight": 0.10},
        {"action": "HOLD_FILTERED", "blocking_reason": "MARKET_CLOSED_FREEZE", "target_weight": 0.0},
        {"action": "CANCEL_PENDING", "blocking_reason": "NONE", "target_weight": 0.0},
    ]
    verdicts = _run_badge_resolver(rows)
    if verdicts is None:  # no JS runtime on this box -- structural fallback
        src = _app_js_source()
        for literal in ("⚪ HOLD (维持)", "休市未评估", "已取消开仓"):
            assert literal in src, f"missing badge label {literal!r} in app.js (L2.20/L2.25)"
        return

    hold, frozen, cancelled = verdicts

    # 1. 维持 (the only state that needs no operator action).
    assert hold["key"] == "HOLD"
    assert "维持" in hold["text"]

    # 2. 休市未评估 -- nothing was evaluated; must NOT read as "held" or "filtered".
    assert frozen["key"] == "MARKET_CLOSED_FREEZE"
    assert "休市未评估" in frozen["text"]
    assert "维持" not in frozen["text"]
    assert "badge-freeze" in frozen["cls"]

    # 3. 已取消开仓 -- an aborted entry that never held anything (L2.20 proper).
    assert cancelled["key"] == "CANCEL_PENDING"
    assert "已取消开仓" in cancelled["text"]
    assert "维持" not in cancelled["text"]
    assert "badge-cancel" in cancelled["cls"]
    assert cancelled["key"] != "SELL"  # never counted as a trade

    # The three states are pairwise distinct -- in the old code states 2 and 3
    # both rendered the generic "⚪ HOLD (维持)" string.
    assert len({v["key"] for v in verdicts}) == 3
    assert len({v["text"] for v in verdicts}) == 3
    assert frozen["text"] != cancelled["text"]


def test_app_js_badge_ignores_target_weight_entirely():
    """Same action + blocking_reason => same badge, whatever `target_weight` says.

    This is the structural guarantee: the badge cannot be a function of the
    weight, because varying the weight alone changes nothing.
    """
    rows = [
        {"action": "CANCEL_PENDING", "blocking_reason": "NONE", "target_weight": 0.0},
        {"action": "CANCEL_PENDING", "blocking_reason": "NONE", "target_weight": 0.35},
        {"action": "HOLD_FILTERED", "blocking_reason": "TENURE_LOCKED", "target_weight": 0.0},
        {"action": "HOLD_FILTERED", "blocking_reason": "TENURE_LOCKED", "target_weight": 0.20},
    ]
    verdicts = _run_badge_resolver(rows)
    if verdicts is None:
        return  # covered by the structural fallback in the test above
    assert verdicts[0] == verdicts[1]
    assert verdicts[2] == verdicts[3]
    assert "未满最低持仓周期" in verdicts[2]["text"]  # label mirrored from Python


def test_app_js_blocking_reason_labels_mirror_python_enum():
    """The web badge and the console ticket must not drift apart in wording.

    `scripts/run_live_strategy.py` renders its labels from
    `common.position_lifecycle.BLOCKING_REASON_LABELS`; the web badge carries a
    JS copy (browsers cannot import Python). The operator reads both side by side,
    so an exact-match assertion is the only honest way to keep them equal.
    """
    import re

    from common.position_lifecycle import BLOCKING_REASON_LABELS

    block = _extract_js_const_object(_app_js_source(), "BLOCKING_REASON_LABELS")
    js_pairs = dict(re.findall(r'"([A-Z_]+)"\s*:\s*"([^"]*)"', block))
    assert js_pairs == dict(BLOCKING_REASON_LABELS)


def test_app_js_counts_only_executable_actions_in_the_ticket_summary():
    """`sellsCount` / `buysCount` must key off the same verdict as the badge."""
    src = _app_js_source()
    expectations = {
        "sellsCount++": 'verdict.key === "SELL"',
        "totalSellVal +=": 'verdict.key === "SELL"',
        "buysCount++": 'verdict.key === "BUY"',
        "totalBuyVal +=": 'verdict.key === "BUY"',
    }
    for counter, guard in expectations.items():
        idx = src.find(counter)
        assert idx > 0, f"{counter} disappeared from app.js"
        window = src[max(0, idx - 300) : idx]
        assert guard in window, (
            f"{counter} is no longer guarded by {guard} -- a CANCEL_PENDING / frozen "
            "row must never be summed into the executable sell/buy totals"
        )


def test_app_js_badge_survives_legacy_archive_rows_without_blocking_reason():
    """Pre-L2.24 archives have no `blocking_reason` column: badges must still work.

    Disclosed limit: a legacy market-closed freeze row is indistinguishable from a
    genuine gate rejection in the archive (both are `HOLD_FILTERED` with no label),
    so it renders as "锁仓/过滤". Reclassifying it would require exactly the
    `target_weight == 0` inference this task removes -- so it stays unresolved
    rather than guessed.
    """
    rows = [
        {"action": "CANCEL_PENDING", "target_weight": 0.0},
        {"action": "HOLD_FILTERED", "target_weight": 0.0},
        {"action": "HOLD", "target_weight": 0.0},
    ]
    verdicts = _run_badge_resolver(rows)
    if verdicts is None:
        return
    assert [v["key"] for v in verdicts] == ["CANCEL_PENDING", "HOLD_FILTERED", "HOLD"]
    assert "已取消开仓" in verdicts[0]["text"]
    assert verdicts[1]["text"] == "🟡 HOLD (锁仓/过滤)"
    assert verdicts[2]["text"] == "⚪ HOLD (维持)"


def test_request_models_intraday_and_refresh_defaults():
    """Verify RunHealthRequest, RunDeployRequest, and MultiStrategyDeployRequest defaults."""
    from models import RunHealthRequest, RunDeployRequest, MultiStrategyDeployRequest

    health_req = RunHealthRequest(strategy_file="dummy.json")
    assert health_req.allow_intraday is True
    assert health_req.refresh_data is True

    deploy_req = RunDeployRequest(strategy_file="dummy.json")
    assert deploy_req.allow_intraday is True

    multi_req = MultiStrategyDeployRequest()
    assert multi_req.allow_intraday is True


def test_execution_service_intraday_and_refresh_flags(temp_workspace, monkeypatch):
    """Verify execution_service appends --allow-intraday, --no-allow-intraday, --no-refresh-data."""
    from execution_service import ExecutionService
    import subprocess

    storage, _ = temp_workspace
    service = ExecutionService(storage)

    captured_cmds = []

    def mock_run(cmd, *args, **kwargs):
        captured_cmds.append(list(cmd))
        class MockProc:
            returncode = 0
            stdout = ""
            stderr = ""
        return MockProc()

    monkeypatch.setattr(subprocess, "run", mock_run)

    # 1. Health check with default allow_intraday=True, refresh_data=True
    service.run_health_check(
        strategy_file="dummy.json",
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=True,
        refresh_data=True,
    )
    cmd1 = captured_cmds[-1]
    assert "--allow-intraday" in cmd1
    assert "--no-refresh-data" not in cmd1

    # 2. Health check with allow_intraday=False, refresh_data=False
    service.run_health_check(
        strategy_file="dummy.json",
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=False,
        refresh_data=False,
    )
    cmd2 = captured_cmds[-1]
    assert "--no-allow-intraday" in cmd2
    assert "--no-refresh-data" in cmd2

    # 3. Deploy check with allow_intraday=True and allow_intraday=False
    service.run_live_deploy(
        strategy_file="dummy.json",
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=True,
    )
    cmd3 = captured_cmds[-1]
    assert "--allow-intraday" in cmd3

    service.run_live_deploy(
        strategy_file="dummy.json",
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=False,
    )
    cmd3_no = captured_cmds[-1]
    assert "--no-allow-intraday" in cmd3_no

    # 4. Multi-strategy deploy with allow_intraday=True and allow_intraday=False
    service.run_multi_strategy_netted_deploy(
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=True,
    )
    cmd4 = captured_cmds[-1]
    assert "--allow-intraday" in cmd4

    service.run_multi_strategy_netted_deploy(
        universe_file="dummy.txt",
        data_provider="synthetic",
        allow_intraday=False,
    )
    cmd4_no = captured_cmds[-1]
    assert "--no-allow-intraday" in cmd4_no


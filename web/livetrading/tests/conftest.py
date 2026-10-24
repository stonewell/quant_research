"""Hermetic test environment for the ``web/livetrading`` test package.

FORENSIC CONTEXT (test-harness hardening only -- no production behaviour change)
-------------------------------------------------------------------------------
``StorageManager.cache_dir`` falls back to ``<repo_root>/data``
(``web/livetrading/storage.py``), i.e. the REAL production directory that holds
``cache.duckdb``.  ``StorageManager`` also reads the committed settings TEMPLATE
``web/livetrading/data/settings.json`` plus the untracked
``web/livetrading/data/settings.local.json`` operator override.  (Before L2.16 the
committed file itself pinned ``cache_dir=<repo_root>/data`` and a live portfolio
``data_dir`` outside the repo; it is now placeholder-only, but an operator's local
override can still pin production paths.)

The pre-existing ``temp_workspace`` fixture isolated ``data_dir``/``archive_dir``
but never overrode ``cache_dir``, and the default data provider chain is
``"marketdb"`` (``storage.py`` / ``execution_service.py``).  The suite therefore
only stayed offline because every workflow test happens to pass
``data_provider="synthetic"`` and one test monkeypatches ``subprocess.run``.
One dropped kwarg -- or one removed/renamed monkeypatch -- would silently turn a
unit test into a production-cache writer.

The autouse fixture below removes that footgun structurally: for EVERY test in
this package the storage/execution-service derived directories (``data_dir``,
``archive_dir``, ``cache_dir``) are re-pointed at the test's ``tmp_path`` through
the environment-variable layer that ``StorageManager`` consults BEFORE the
settings files.  Explicit constructor arguments still take precedence, so
existing fixtures keep behaving exactly as before.

``LIVETRADING_SETTINGS_DIR`` additionally redirects the settings directory itself
(``web/livetrading/data`` by default) at ``tmp_path``.  Needed since L2.16 made
``settings.local.json`` the write target of ``update_settings``: without it the
``POST /api/settings`` API test would write an operator override into the real
checkout.

Any future test that needs the real provider chain still gets a throwaway cache
directory instead of the production one, and the companion guard test
``test_storage_cache_dir_never_escapes_the_temp_workspace`` in
``test_web_livetrading.py`` fails loudly if this isolation ever regresses.
"""

import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))

# Match the sys.path setup the test module itself performs, so conftest-level
# imports of ``storage``/``execution_service`` work regardless of invocation dir.
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Locations the test suite must NEVER be handed as a writable workspace.
PRODUCTION_CACHE_DIR = os.path.join(_REPO_ROOT, "data")
PRODUCTION_WEB_DATA_DIR = os.path.join(_WEB_DIR, "data")
FORBIDDEN_DIRS = (PRODUCTION_CACHE_DIR, PRODUCTION_WEB_DATA_DIR)


def canonical_path(path) -> str:
    """Absolute, symlink-resolved form of ``path`` for safe containment checks."""
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def is_inside(path, root) -> bool:
    """True when ``path`` is ``root`` or a descendant of it."""
    path_c = canonical_path(path)
    root_c = canonical_path(root)
    return path_c == root_c or path_c.startswith(root_c + os.sep)


@pytest.fixture(autouse=True)
def hermetic_livetrading_dirs(tmp_path, monkeypatch):
    """Re-point every ``StorageManager``-derived directory at ``tmp_path``.

    ``autouse=True`` is the whole point: no individual test can forget to opt
    in, so a new test that builds a ``StorageManager`` (or drives
    ``ExecutionService`` with its default ``"marketdb"`` provider fallback) can
    no longer reach ``<repo_root>/data/cache.duckdb``.

    The env-var layer is used because ``StorageManager`` resolves in the order
    parameter > environment > ``settings.local.json`` > ``settings.json`` template
    > dynamic default, so this both overrides any operator settings (the template
    is placeholder-only as of L2.16) and leaves every explicit constructor
    argument in the suite untouched.
    """
    cache_dir = tmp_path / "duckdb_cache"
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "runs"
    settings_dir = tmp_path / "settings"
    for directory in (cache_dir, data_dir, archive_dir, settings_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LIVETRADING_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("LIVETRADING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LIVETRADING_ARCHIVE_DIR", str(archive_dir))
    # Redirect the settings TEMPLATE/local-override directory too, so
    # `update_settings` (POST /api/settings) can never write a machine-specific
    # settings.local.json into the real checkout.
    monkeypatch.setenv("LIVETRADING_SETTINGS_DIR", str(settings_dir))

    # Fail fast on the spot if anything about the resolution chain drifts.
    for label, value in (
        ("cache_dir", cache_dir),
        ("data_dir", data_dir),
        ("archive_dir", archive_dir),
        ("settings_dir", settings_dir),
    ):
        for forbidden in FORBIDDEN_DIRS:
            if is_inside(value, forbidden):
                raise AssertionError(
                    f"Hermetic web test workspace leaked {label}={value} into the "
                    f"production directory {forbidden}. Tests must never touch the "
                    f"real DuckDB market cache (data/cache.duckdb) or live "
                    f"settings/holdings state."
                )

    yield {
        "cache_dir": str(cache_dir),
        "data_dir": str(data_dir),
        "archive_dir": str(archive_dir),
        "settings_dir": str(settings_dir),
    }

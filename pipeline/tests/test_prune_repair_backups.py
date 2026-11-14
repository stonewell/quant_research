"""Offline contract tests for `scripts/prune_repair_backups.py` (backup retention).

WHY THIS NEEDS A TEST
---------------------
`data/repair_backups/` is the only place the *deleted* rows and the pre-repair database bytes
survive: the repair tools export before deleting and explicitly promise "backups are never
deleted by this script". A retention tool is therefore a data-destroying tool from the first
line of code, and every one of its safety rules needs a mechanical demonstration rather than a
docstring claim:

  * the default is a dry run and it deletes **nothing**;
  * `--keep-last N` keeps the newest N (mixed directory-name shapes included);
  * a directory pointed at by a `repair_manifest.json` is **never** deleted, even when its rank
    would put it in the removal set;
  * a directory the tool cannot reason about (unparseable name) is untouched;
  * an unreadable manifest makes the reference set unverifiable -> refuse to delete anything;
  * `--apply` outside a temp directory is refused unless `--i-know-this-is-production` is given.

100% offline: everything runs under `tmp_path` with synthetic directories and a synthetic
`repair_manifest.json`; the real `data/repair_backups` tree is never touched or read. The
module is loaded through `importlib` (it lives in `scripts/`, not in an importable package).
"""

import importlib.util
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest


def _load_script(module_name, relative_path):
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves `cls.__module__` through `sys.modules`,
    # so a script defining dataclasses cannot be loaded otherwise.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "prune_repair_backups_under_test",
    os.path.join("scripts", "prune_repair_backups.py"),
)

#: The three name shapes actually present in `data/repair_backups/`, verbatim.
REAL_NAME_SHAPES = ["20261111-215145", "20261112_222030", "20261113_040659Z"]


def _make_backup(root, name, *, files=("cache.duckdb.bak",)):
    directory = root / name
    directory.mkdir()
    for filename in files:
        (directory / filename).write_text("synthetic backup bytes\n", encoding="utf-8")
    return directory


def _write_manifest(directory, *, db_copy_name="cache.duckdb.bak", extra=None):
    payload = {
        "script": "repair_cross_source_adjust_factors.py",
        "mode": "apply",
        "db_copy": str(directory / db_copy_name),
        "db_copy_md5": "0123456789abcdef0123456789abcdef",
        "removed_json": str(directory / "adjust_factors_removed.json"),
    }
    if extra:
        payload.update(extra)
    (directory / "repair_manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (directory / "adjust_factors_removed.json").write_text("[]", encoding="utf-8")


def _tree(root):
    return sorted(entry.name for entry in root.iterdir())


# ---------------------------------------------------------------------------
# 1. Name parsing / ordering
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, expected",
    [
        ("20261111-215145", "2026-11-11 21:51:45"),
        ("20261112_222030", "2026-11-12 22:20:30"),
        ("20261113_040659Z", "2026-11-13 04:06:59"),
    ],
)
def test_parse_backup_stamp_handles_the_shapes_already_on_disk(name, expected):
    parsed = _mod.parse_backup_stamp(name)
    assert parsed.strftime("%Y-%m-%d %H:%M:%S") == expected


def test_parse_backup_stamp_returns_none_for_an_unknown_shape():
    assert _mod.parse_backup_stamp("extra-old") is None
    assert _mod.parse_backup_stamp("20261345-999999") is None


# ---------------------------------------------------------------------------
# 2. Dry run is the default and it deletes nothing
# ---------------------------------------------------------------------------
def test_dry_run_deletes_nothing(tmp_path, capsys):
    root = tmp_path / "repair_backups"
    root.mkdir()
    for name in ["20261101-000000", "20261102-000000", "20261103-000000"]:
        _make_backup(root, name)
    before = _tree(root)

    exit_code = _mod.main(["--root", str(root), "--keep-last", "1"])

    assert exit_code == _mod.EXIT_OK
    assert _tree(root) == before, "dry-run must not delete anything"
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "REMOVE (2)" in out
    assert "nothing was deleted" in out


def test_dry_run_plan_names_exactly_the_doomed_directories(tmp_path, capsys):
    root = tmp_path / "repair_backups"
    root.mkdir()
    for name in REAL_NAME_SHAPES:
        _make_backup(root, name)

    plan = _mod.build_plan(root, keep_last=2)

    assert [entry.name for entry in plan.keep] == ["20261113_040659Z", "20261112_222030"]
    assert [entry.name for entry in plan.remove] == ["20261111-215145"]


# ---------------------------------------------------------------------------
# 3. `--keep-last 1` keeps the newest, and only the newest
# ---------------------------------------------------------------------------
def test_apply_keep_last_1_keeps_only_the_newest(tmp_path):
    root = tmp_path / "repair_backups"
    root.mkdir()
    for name in REAL_NAME_SHAPES:
        _make_backup(root, name)

    exit_code = _mod.main(["--root", str(root), "--keep-last", "1", "--apply"])

    assert exit_code == _mod.EXIT_OK
    assert _tree(root) == ["20261113_040659Z"]


# ---------------------------------------------------------------------------
# 4. A manifest-referenced directory is never deleted, even at the bottom of the ranking
# ---------------------------------------------------------------------------
def test_manifest_referenced_backup_is_protected(tmp_path, capsys):
    root = tmp_path / "repair_backups"
    root.mkdir()
    oldest = _make_backup(root, "20261101-000000")
    _write_manifest(oldest)
    newest = _make_backup(root, "20261113_040659Z")

    exit_code = _mod.main(["--root", str(root), "--keep-last", "1", "--apply"])

    assert exit_code == _mod.EXIT_OK
    assert oldest.exists(), "a manifest reference outranks the keep-last ranking"
    assert newest.exists()
    out = capsys.readouterr().out
    assert "referenced by" in out


def test_reference_is_found_even_when_written_in_a_sibling_manifest(tmp_path):
    """The reference set is global: any manifest under the root can protect any directory."""
    root = tmp_path / "repair_backups"
    root.mkdir()
    old = _make_backup(root, "20261101-000000")
    keeper = _make_backup(root, "20261113_040659Z")
    (keeper / "repair_manifest.json").write_text(
        json.dumps({"db_copy": str(old / "cache.duckdb.bak")}), encoding="utf-8"
    )

    plan = _mod.build_plan(root, keep_last=1)

    assert [entry.name for entry in plan.remove] == []
    assert [entry.name for entry in plan.protected] == ["20261101-000000"]


# ---------------------------------------------------------------------------
# 5. Directories the tool cannot reason about are untouched
# ---------------------------------------------------------------------------
def test_unparseable_directory_is_protected_and_never_takes_a_keep_slot(tmp_path):
    root = tmp_path / "repair_backups"
    root.mkdir()
    _make_backup(root, "20261112_222030")
    _make_backup(root, "20261113_040659Z")
    _make_backup(root, "extra-old")

    plan = _mod.build_plan(root, keep_last=1)

    assert [entry.name for entry in plan.keep] == ["20261113_040659Z"]
    assert [entry.name for entry in plan.remove] == ["20261112_222030"]
    assert [entry.name for entry in plan.protected] == ["extra-old"]


def test_an_unreadable_manifest_refuses_the_whole_apply(tmp_path):
    root = tmp_path / "repair_backups"
    root.mkdir()
    _make_backup(root, "20261112_222030")
    broken = _make_backup(root, "20261113_040659Z")
    (broken / "repair_manifest.json").write_text("{not json", encoding="utf-8")

    exit_code = _mod.main(["--root", str(root), "--keep-last", "1", "--apply"])

    assert exit_code == _mod.EXIT_REFUSED
    assert _tree(root) == ["20261112_222030", "20261113_040659Z"], "nothing may be deleted"


# ---------------------------------------------------------------------------
# 6. Guards: usage errors and the production gate
# ---------------------------------------------------------------------------
def test_missing_root_is_a_usage_error(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert _mod.main(["--root", str(missing)]) == _mod.EXIT_USAGE
    assert not missing.exists(), "the pruner must never create its root"
    assert "does not exist" in capsys.readouterr().err


def test_empty_root_is_a_usage_error(tmp_path, capsys):
    root = tmp_path / "repair_backups"
    root.mkdir()
    (root / "stray-file.txt").write_text("not a backup\n", encoding="utf-8")

    assert _mod.main(["--root", str(root)]) == _mod.EXIT_USAGE
    assert "holds no backup directories" in capsys.readouterr().err


def test_keep_last_zero_is_rejected(tmp_path):
    root = tmp_path / "repair_backups"
    root.mkdir()
    _make_backup(root, "20261112_222030")
    assert _mod.main(["--root", str(root), "--keep-last", "0", "--apply"]) == _mod.EXIT_USAGE
    assert _tree(root) == ["20261112_222030"]


def test_apply_outside_temp_requires_the_production_flag(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repair_backups"
    root.mkdir()
    _make_backup(root, "20261112_222030")
    _make_backup(root, "20261113_040659Z")
    # Pretend no temp directory exists, so `tmp_path` reads as "production".
    monkeypatch.setattr(_mod, "_temp_roots", lambda: [tmp_path / "not-a-temp-root"])

    exit_code = _mod.main(["--root", str(root), "--keep-last", "1", "--apply"])

    assert exit_code == _mod.EXIT_REFUSED
    assert _tree(root) == ["20261112_222030", "20261113_040659Z"]
    assert "--i-know-this-is-production" in capsys.readouterr().err


def test_production_flag_admits_the_apply_outside_temp(tmp_path, monkeypatch):
    root = tmp_path / "repair_backups"
    root.mkdir()
    _make_backup(root, "20261112_222030")
    _make_backup(root, "20261113_040659Z")
    monkeypatch.setattr(_mod, "_temp_roots", lambda: [tmp_path / "not-a-temp-root"])

    exit_code = _mod.main(
        ["--root", str(root), "--keep-last", "1", "--apply", "--i-know-this-is-production"]
    )

    assert exit_code == _mod.EXIT_OK
    assert _tree(root) == ["20261113_040659Z"]


# ---------------------------------------------------------------------------
# 7. The repair tools keep their own promise (nothing here deletes their backups by default)
# ---------------------------------------------------------------------------
def test_default_invocation_is_a_dry_run():
    assert _mod.build_arg_parser().parse_args([]).apply is False
    assert _mod.build_arg_parser().parse_args([]).allow_production is False
    assert _mod.DEFAULT_KEEP_LAST == 5

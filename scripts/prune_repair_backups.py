#!/usr/bin/env python3
"""Repair-backup retention pruner: dry-run by default, guarded ``--apply``.

WHY THIS IS A SEPARATE SCRIPT (and not a flag on the repair tools)
------------------------------------------------------------------
``scripts/repair_cross_source_adjust_factors.py`` states, in its docstring and its contract,
that **"Backups are never deleted by this script, and a failed apply keeps them"** (see its
lines 44-48). That property is load-bearing: the JSON export is the *only* surviving copy of
every deleted row, and the byte copy is the only rollback point for the whole database. A
repair tool that could also delete its own backups is one bug away from unrecoverable data, so
the repair path keeps its hands off ``data/repair_backups`` and retention lives here instead,
behind an explicit operator action.

WHAT IT DOES
------------
Lists the timestamped backup directories under ``--root`` (default ``data/repair_backups``),
keeps the newest ``--keep-last`` of them (default **5**) and -- only with ``--apply`` -- deletes
the rest. Default mode is ``--dry-run``: it prints what it *would* remove, exits ``0`` and
touches nothing.

WHAT IS NEVER DELETED (each rule is deliberately over-protective)
----------------------------------------------------------------
1. **Anything referenced by a ``repair_manifest.json``.** Every manifest under the root is
   read; any path-shaped value it carries (``removed_json``, ``db_copy``, ...) protects the
   directory that path lives in. A manifest that cannot be parsed protects *everything*: an
   unverified reference must not become a deletion. Note the consequence: a directory holding
   its own manifest references itself and is therefore **permanent** -- by design, since that
   is the directory holding the only copy of the deleted rows.
2. **Anything whose name cannot be parsed as a timestamp** -- its age is unknown, so its rank
   in "the newest N" is unknown.
3. **Anything whose mtime is later than the newest name timestamp** -- that is what an
   in-progress write with a not-yet-final name looks like.
4. **The newest directory overall**, which is also what rule "keep the newest N" keeps when
   ``--keep-last`` is valid (``>= 1``, enforced).

Name sorting is robust to the mixed names already on disk (``20261111-215145``,
``20261112_222030``, ``20261113_040659Z``): digits are extracted and the first 14/12/8 of them
are read as ``%Y%m%d%H%M%S`` / ``%Y%m%d%H%M`` / ``%Y%m%d``. A name that parses by none of those
sorts **after** every parseable one (it cannot claim a keep slot on a rank it does not have) and
is protected by rule 2.

Directory mtimes are printed for the operator but deliberately **not** used to decide
deletion: copying, restoring or syncing the backup tree rewrites every mtime, and an
"is this mtime newer than the newest name stamp?" rule then protects the whole tree and makes
the pruner permanently inert (observed on a restored tree while building this script). "Newest"
is therefore defined by the name timestamp, and the newest entry is protected unconditionally --
nothing can be newer than the newest, and anything that were would be the newest.

GUARDS (mirrors ``scripts/repair_cross_source_adjust_factors.py``)
-----------------------------------------------------------------
* ``--apply`` is refused unless ``--i-know-this-is-production`` is also passed whenever the
  root resolves outside a temp directory;
* ``--apply`` is refused inside a pytest process when the root resolves into ``<repo>/data``
  (the ``L2.13`` guard, reused unchanged: ``common.duckdb_cache.refuse_production_cache_path``);
* a missing or empty root is a usage/data error (exit ``2``), never a silent no-op.

EXIT CODES
----------
``0``  dry-run: plan printed / apply: everything planned was removed
``1``  a guard refused (nothing written), or ``--apply`` with an unreadable manifest
``2``  usage / data error (missing or empty root, invalid ``--keep-last``)

USAGE
-----
    # dry-run (default) -- read-only, safe against the production tree
    uv run --offline --project pipeline python scripts/prune_repair_backups.py

    # delete everything but the newest 2 (production tree needs the flag)
    uv run --offline --project pipeline python scripts/prune_repair_backups.py \\
        --keep-last 2 --apply --i-know-this-is-production
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

DEFAULT_ROOT = os.path.join(_REPO_ROOT, "data", "repair_backups")
DEFAULT_KEEP_LAST = 5

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

#: The manifest a repair tool writes next to its export (`repair_cross_source_adjust_factors.py`
#: and the qfq repair both use this name). Its path-shaped values are the reference set.
MANIFEST_NAME = "repair_manifest.json"

#: Keys whose values are digests, not paths -- never resolved as references.
_DIGEST_KEY_HINTS = ("md5", "sha", "hash", "digest")

#: Temp roots that count as "not production". `tempfile.gettempdir()` alone is not enough on
#: this workspace (runs live under `/tmp/opencode`), so both are checked by containment.
_EXTRA_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp")


class PruneRefused(RuntimeError):
    """A guard refused the deletion (nothing was written)."""


@dataclass
class BackupDir:
    """One timestamped directory under the backup root."""

    path: Path
    name: str
    stamp: Optional[dt.datetime]
    mtime: dt.datetime
    protected_reason: Optional[str] = None

    @property
    def sort_key(self) -> Tuple[int, dt.datetime]:
        """`(0, name timestamp)` for parseable names, `(1, min)` for unparseable ones.

        See the module docstring: the unparseable bucket sorts after every parseable entry and
        is protected separately, so a name it cannot parse can never cost a real backup its slot.
        """
        if self.stamp is None:
            return 1, dt.datetime.min
        return 0, self.stamp

    def file_stats(self) -> Tuple[int, int]:
        """`(file_count, total_bytes)` of the directory tree -- for the removal manifest."""
        count = 0
        total = 0
        for path in self.path.rglob("*"):
            if path.is_file():
                count += 1
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return count, total


@dataclass
class PrunePlan:
    """What the pruner decided: ordered survivors, ordered removals, protected entries."""

    root: Path
    keep_last: int
    ordered: List[BackupDir] = field(default_factory=list)
    keep: List[BackupDir] = field(default_factory=list)
    remove: List[BackupDir] = field(default_factory=list)
    protected: List[BackupDir] = field(default_factory=list)
    manifest_errors: List[str] = field(default_factory=list)
    references: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Name parsing
# --------------------------------------------------------------------------- #
def parse_backup_stamp(name: str) -> Optional[dt.datetime]:
    """Parse a backup directory name into a naive timestamp, or `None` when unparseable.

    Handles the three shapes already on disk (`20261111-215145`, `20261112_222030`,
    `20261113_040659Z`) and any other separator, by extracting digits first:
    `%Y%m%d%H%M%S` (14+ digits) -> `%Y%m%d%H%M` (12+) -> `%Y%m%d` (8+).
    """
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    for width, fmt in ((14, "%Y%m%d%H%M%S"), (12, "%Y%m%d%H%M"), (8, "%Y%m%d")):
        if len(digits) >= width:
            try:
                return dt.datetime.strptime(digits[:width], fmt)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# Manifest references
# --------------------------------------------------------------------------- #
def _string_leaves(node: Any, key: str = "") -> Iterable[Tuple[str, str]]:
    """Every `(key, string)` leaf of a JSON document, digest keys excluded."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            yield from _string_leaves(value, str(child_key))
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _string_leaves(value, key)
    elif isinstance(node, str):
        if not any(hint in key.lower() for hint in _DIGEST_KEY_HINTS):
            yield key, node


def _looks_like_path(value: str) -> bool:
    if not value or len(value) > 4096:
        return False
    if value.startswith(("http://", "https://", "s3://")):
        return False
    return os.sep in value or "/" in value


def _resolved_within(value: str, manifest: Path) -> Optional[Path]:
    """Resolve a manifest path value, accepting it only if it lands inside the backup root.

    Manifests written by the repair tools carry absolute paths, but a relative one is resolved
    against the manifest's own directory and then against the repo root -- never guessed wider
    than that.
    """
    candidates = []
    raw = Path(value)
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(manifest.parent / raw)
        candidates.append(Path(_REPO_ROOT) / raw)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.exists() or resolved.parent.exists():
            return resolved
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


def collect_manifest_references(
    root: Path,
) -> Tuple[Dict[Path, str], List[str]]:
    """`({directory: manifest}, errors)` -- which backup directories a manifest points into.

    An unreadable/unparseable manifest is recorded in `errors`; the caller turns that into
    "protect everything / refuse to apply", because an unverified reference must never become
    a deletion.
    """
    references: Dict[Path, str] = {}
    errors: List[str] = []
    if not root.exists():
        return references, errors
    for manifest in sorted(root.rglob(MANIFEST_NAME)):
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{manifest}: {exc}")
            continue
        for _key, value in _string_leaves(document):
            if not _looks_like_path(value):
                continue
            resolved = _resolved_within(value, manifest)
            if resolved is None:
                continue
            directory = resolved if resolved.is_dir() else resolved.parent
            if _is_within(directory, root):
                references[directory.resolve()] = str(manifest)
    return references, errors


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def build_plan(root: Path, keep_last: int = DEFAULT_KEEP_LAST) -> PrunePlan:
    """Decide what to keep and what to remove. Pure: reads the filesystem, writes nothing."""
    if keep_last < 1:
        raise ValueError(f"--keep-last must be >= 1, got {keep_last!r}")
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"{root} does not exist or is not a directory")
    dirs = [entry for entry in sorted(root.iterdir()) if entry.is_dir()]
    if not dirs:
        raise FileNotFoundError(f"{root} holds no backup directories")

    references, errors = collect_manifest_references(root)

    entries: List[BackupDir] = []
    for directory in dirs:
        try:
            mtime = dt.datetime.fromtimestamp(directory.stat().st_mtime)
        except OSError:
            mtime = dt.datetime.fromtimestamp(0)
        stamp = parse_backup_stamp(directory.name)
        entry = BackupDir(path=directory, name=directory.name, stamp=stamp, mtime=mtime)
        if stamp is None:
            entry.protected_reason = "name is not a parseable timestamp (age unknown)"
        elif directory.resolve() in references:
            entry.protected_reason = f"referenced by {references[directory.resolve()]}"
        entries.append(entry)

    plan = PrunePlan(root=root, keep_last=keep_last, ordered=entries,
                     manifest_errors=list(errors), references={
                         str(k): v for k, v in references.items()
                     })
    if errors:
        # Cannot verify what the manifests protect -> propose nothing at all.
        for entry in entries:
            if entry.protected_reason is None:
                entry.protected_reason = (
                    "a repair_manifest.json could not be read: references unverified"
                )
        plan.protected = list(entries)
        plan.keep = list(entries)
        return plan

    parseable = [entry for entry in entries if entry.stamp is not None]
    if not parseable:
        # Nothing carries a usable age, so nothing can be ranked, so nothing may go.
        plan.protected = list(entries)
        plan.keep = list(entries)
        return plan

    ordered = sorted(
        entries,
        key=lambda entry: (
            0 if entry.stamp is not None else 1,
            -(entry.stamp.timestamp() if entry.stamp is not None else entry.mtime.timestamp()),
            entry.name,
        ),
    )
    plan.ordered = ordered
    plan.keep = ordered[:keep_last]
    keep_paths = {entry.path for entry in plan.keep}
    newest_path = ordered[0].path if ordered else None
    for entry in ordered[keep_last:]:
        if entry.path in keep_paths:  # defensive: keep-last always wins
            continue
        if entry.protected_reason is not None:
            plan.protected.append(entry)
        elif entry.path == newest_path:
            # `keep_last >= 1` already guarantees this, but the rule is stated unconditionally
            # so it cannot be lost to a future edit of the slice above.
            entry.protected_reason = (
                f"newest directory (name timestamp {entry.stamp.isoformat(timespec='seconds')})"
            )
            plan.protected.append(entry)
        else:
            plan.remove.append(entry)
    return plan


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def _temp_roots() -> List[Path]:
    roots = [Path(tempfile.gettempdir())]
    roots.extend(Path(p) for p in _EXTRA_TEMP_ROOTS)
    return roots


def is_temp_path(path: str | Path) -> bool:
    """True when `path` resolves inside a temp directory (i.e. not production)."""
    target = Path(path)
    return any(_is_within(target, root) for root in _temp_roots() if root.exists())


def refuse_production_apply(root: str | Path, allow_production: bool) -> None:
    """Refuse `--apply` outside a temp directory unless the operator says so explicitly.

    Same shape as ``scripts/repair_qfq_alignment.py::refuse_production_apply``: the dry-run
    default plus this refusal means an unattended invocation can never delete production
    backups.
    """
    if is_temp_path(root) or allow_production:
        return
    raise PruneRefused(
        f"{root} is outside any temp directory. --apply refuses it by default; rehearse on a "
        "copy under /tmp/opencode first, and only then re-run with "
        "--i-know-this-is-production."
    )


def refuse_under_pytest(root: str | Path) -> None:
    """Reuse the `L2.13` gate: no pytest process may write into `<repo>/data`."""
    try:
        from common.duckdb_cache import refuse_production_cache_path
    except Exception:  # pragma: no cover - repository layout changed
        return
    refuse_production_cache_path(str(root), read_only=False)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_plan(plan: PrunePlan, *, apply: bool) -> str:
    """Human-readable manifest of the decision (what goes, what stays, and why)."""
    lines = [
        f"root        : {plan.root}",
        f"mode        : {'APPLY' if apply else 'DRY-RUN (nothing is deleted)'}",
        f"keep-last   : {plan.keep_last}",
        f"directories : {len(plan.ordered)}   manifest references: {len(plan.references)}",
        "",
    ]
    for label, entries in (("KEEP", plan.keep), ("PROTECTED", plan.protected),
                           ("REMOVE", plan.remove)):
        lines.append(f"{label} ({len(entries)})")
        if not entries:
            lines.append("  -")
        for entry in entries:
            count, size = entry.file_stats()
            detail = f"{count} file(s), {size} bytes"
            if entry.protected_reason:
                detail = f"{detail}, {entry.protected_reason}"
            lines.append(f"  {entry.name:<24} {detail}")
        lines.append("")
    if plan.manifest_errors:
        lines.append("manifest errors (deletion refused while any exists)")
        lines.extend(f"  {error}" for error in plan.manifest_errors)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    """The CLI surface, split out so tests can pin it without touching a real backup tree."""
    ap = argparse.ArgumentParser(
        description="Prune data/repair_backups: keep the newest N, delete nothing else "
                    "(dry-run by default)."
    )
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="backup root (default: data/repair_backups)")
    ap.add_argument("--keep-last", type=int, default=DEFAULT_KEEP_LAST,
                    help=f"number of newest directories to keep (default: {DEFAULT_KEEP_LAST})")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is --dry-run: prints the plan, deletes nothing)")
    ap.add_argument("--i-know-this-is-production", action="store_true", dest="allow_production",
                    help="required for --apply outside a temp directory")
    return ap


def run(plan: PrunePlan, *, apply: bool) -> int:
    """Print the plan and, when `apply`, delete exactly the planned directories."""
    if plan.manifest_errors:
        print(render_plan(plan, apply=False), end="")
        print("[ERROR] an unreadable repair_manifest.json makes the reference set unverifiable; "
              "delete nothing until it is readable", file=sys.stderr)
        return EXIT_REFUSED
    print(render_plan(plan, apply=apply), end="")
    if not apply:
        verb = "would remove" if plan.remove else "would remove nothing,"
        print(f"[DRY-RUN] {verb} {len(plan.remove)} directories; nothing was deleted")
        return EXIT_OK
    for entry in plan.remove:
        shutil.rmtree(entry.path, ignore_errors=False)
    print(f"[APPLIED] removed {len(plan.remove)} directories, kept {len(plan.keep)}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    root = Path(args.root).expanduser()

    if args.keep_last < 1:
        print(f"[ERROR] --keep-last must be >= 1, got {args.keep_last}", file=sys.stderr)
        return EXIT_USAGE
    if not root.exists() or not root.is_dir():
        print(f"[ERROR] backup root {root} does not exist (refusing to create it)", file=sys.stderr)
        return EXIT_USAGE
    if not any(entry.is_dir() for entry in root.iterdir()):
        print(f"[ERROR] backup root {root} holds no backup directories", file=sys.stderr)
        return EXIT_USAGE

    try:
        plan = build_plan(root, args.keep_last)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.apply:
        try:
            refuse_under_pytest(root)
            refuse_production_apply(root, args.allow_production)
        except Exception as exc:  # `PruneRefused`, `ProductionCachePathError`, ...
            print(render_plan(plan, apply=False), end="")
            print(f"[REFUSED] {exc}", file=sys.stderr)
            return EXIT_REFUSED

    return run(plan, apply=bool(args.apply))


if __name__ == "__main__":
    sys.exit(main())

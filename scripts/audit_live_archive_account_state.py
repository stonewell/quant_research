#!/usr/bin/env python3
"""Read-only forensic audit of archived `account_state.json` files (D8).

WHY THIS EXISTS (2026-11-11, D8)
================================
Two archived rounds carry an `account_state.json` that a HUMAN EDITOR rewrote
after the round had already finished:

    <archive>/.../2026-11-09/204716/account_state.json   mtime 20:49:51
    <archive>/.../2026-11-06/184826/account_state.json   mtime 11-08 18:50:12

In both cases `summary.json` (written by the engine at the end of the round)
still says the engine REFUSED to issue a ticket because the ledger recorded
`base_version = '1.8.0'` while the live class was `2.0.0`.  The file on disk,
however, now records `2.0.0` in four places -- i.e. the archive contradicts
itself, and the only remaining witness of what the engine actually saw is the
summary's `error_detail` / `deploy_stderr` text.  Re-stamping a ledger is
exactly what `docs/lv7_prior_state_contract.md` §5.4 forbids; it cannot be
undone by a code change, so the archive needs a detector that survives in CI.

WHAT IT CHECKS (three INDEPENDENT fingerprints -- any one is a finding)
-----------------------------------------------------------------------
A. **A failed round was rewritten afterwards**: `mtime(account_state.json) -
   mtime(summary.json) > 60s`.  The engine writes both within the same round
   (seconds apart at most); a later hand edit is minutes later.  The 60s
   threshold is why `2026-10-29/215823` (delta +0.0013s, the documented
   `repair_live_run_snapshots.py` repair) is NOT reported.

B. **The file was not produced by `json.dump`**: every writer in this repo
   (`StorageManager._save_json`) dumps WITHOUT a trailing newline, so the last
   byte of a machine-written file is always `}`.  A trailing newline (or any
   other trailing byte) means an editor saved it.  This is checked on RAW
   BYTES, never on the parsed JSON (parsing would erase the evidence).

C. **The file contradicts the round's own summary**: if `summary.json`'s
   `error_detail` / `deploy_stderr` quotes a recorded
   ``base_version = 'X'`` and the CURRENT file records anything else at
   `/base_version`, `/prior_state/base_version` or
   `/strategy_version_info/base_version`, the two cannot both be true.  A file
   with no `base_version` claim at all is "not comparable" and is NOT flagged
   (missing evidence is not proof of tampering).

SEVERITY (why the report has two grades)
----------------------------------------
`B` and `C` are content/byte evidence: the file cannot have been produced by the
engine (`B`) or it directly contradicts the round's own summary (`C`).  Those are
`confirmed`.  `A` alone is `mtime_only`: the file was written minutes-to-days
after the round ended, but its content may still match what the round recorded.
It is still reported -- a later write IS the hazard D8 is about -- and it is
still exit code 1, but the grade says "provenance anomaly, no content proof".
On the 2026-11 archive the two known re-stamps are `confirmed`; one older round
(`.../2026-10-15`) is `mtime_only` because only its `account_state.json` was
written 2.9 days after everything else in the directory.

READ-ONLY BY CONSTRUCTION
-------------------------
There is deliberately NO `--apply`: this script never opens an archive path for
writing, never re-stamps a version, and never repairs anything.  It exists to
produce evidence, and evidence that edits itself is worthless.

WHERE THE ARCHIVE ROOT COMES FROM (no personal path is committed)
------------------------------------------------------------------
An earlier revision hardcoded this machine's live-station archive directory as
`DEFAULT_ROOT`, which `pipeline/tests/test_config_wiring.py::
test_no_hardcoded_personal_paths_in_python_scripts` rejects (P1.6: no
machine-specific absolute path may be committed, `.py` or `.sh`).  The root is
now resolved at run time, in this order:

1. ``--root <dir>`` -- always wins when given (explicit operator intent).
2. ``archive_dir`` from the live station's own settings file
   ``web/livetrading/data/settings.local.json`` (read-only; the file an operator
   already keeps out of git).  Only the ``archive_dir`` key is consumed.
3. ``$QUANT_LIVE_ARCHIVE_DIR`` -- for CI / another host whose archive lives
   elsewhere and has no settings file.
4. Nothing.  There is NO fallback default: the script prints the resolution
   chain plus a usage example and exits ``2``.  Guessing a directory would be
   exactly the class of silent wrong-target scan this audit is meant to catch.

A resolved-but-missing directory is also exit ``2`` (same as an explicit bad
`--root`): the operator learns which path was tried, instead of the audit
silently reporting "0 findings" on an empty walk.

Exit codes: ``0`` = clean, ``1`` = at least one finding (CI-gateable),
``2`` = usage error (root unresolved, or the root does not exist).

Usage::

    # explicit root (what CI should do -- never depends on local state)
    python scripts/audit_live_archive_account_state.py --root <runs-root> --json

    # local station: archive_dir from web/livetrading/data/settings.local.json
    python scripts/audit_live_archive_account_state.py

    # another host: no settings file, point the env var at the tree
    QUANT_LIVE_ARCHIVE_DIR=<runs-root> python scripts/audit_live_archive_account_state.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Repository root (this file lives in `<repo>/scripts/`).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The live station's own (git-ignored) settings file.  `archive_dir` is the
#: operator-declared archive root, so the audit reuses that declaration instead
#: of committing a machine-specific path of its own.  Read-only, and only when
#: `--root` is absent.  `settings.local.json.example` is deliberately NOT read:
#: the example carries placeholder values, not this machine's layout.
SETTINGS_RELATIVE_PATH = os.path.join("web", "livetrading", "data", "settings.local.json")

#: Env override for hosts without that settings file (CI, a second machine).
ARCHIVE_DIR_ENV_VAR = "QUANT_LIVE_ARCHIVE_DIR"

#: `A`'s threshold.  Both known hand edits are ~78-80s later; the documented
#: repair (`2026-10-29/215823`) is 0.0013s later, so 60s separates them cleanly
#: without any tuning.
DEFAULT_MTIME_THRESHOLD_SECONDS = 60.0

ACCOUNT_STATE_FILENAME = "account_state.json"
SUMMARY_FILENAME = "summary.json"

#: Fields of `summary.json` that may quote the ledger version the engine saw.
#: `error_detail` is the classified refusal text; `deploy_stderr` is the raw
#: child traceback -- a hand edit cannot rewrite either without touching a
#: second file, which is precisely the asymmetry being detected.
SUMMARY_CLAIM_FIELDS = ("error_detail", "deploy_stderr")

#: ``base_version` = '1.8.0'`` -> ``1.8.0``.  The separator tolerates the
#: backticks / markdown the refusal message wraps the key in, and the
#: surrounding quotes are the point (an unquoted mention is not a value claim).
_BASE_VERSION_CLAIM_RE = re.compile(
    r"base_version[`'\"\s]*=[\s'\"`]*([0-9][0-9A-Za-z.\-+]*)"
)

#: Structured locations a ledger may stamp its engine version.  Reported
#: individually: "which of them disagree" is part of the evidence.
BASE_VERSION_PATHS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("/base_version", ("base_version",)),
    ("/prior_state/base_version", ("prior_state", "base_version")),
    ("/strategy_version_info/base_version", ("strategy_version_info", "base_version")),
)

FINGERPRINT_LABELS = {
    "A": "state mtime - summary mtime > 60s (round was rewritten after it ended)",
    "B": "file does not end with '}' (not a json.dump product; editor-saved)",
    "C": "file contradicts summary.json's quoted base_version (禁止重新盖章)",
}


def _dig(payload: Any, path: Sequence[str]) -> Any:
    node = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def extract_summary_claim(summary: Any) -> Optional[Dict[str, str]]:
    """First quoted ``base_version = 'X'`` in the summary's refusal text, or `None`.

    Only the FIRST captured value is used: the refusal message reads
    "记录 base_version = '1.8.0'，而今日活类 base_version = '2.0.0'", where the
    first number is the RECORDED one and the second is the live class's.  The
    claim is about what the ledger said, so the first match is the witness.
    """
    if not isinstance(summary, dict):
        return None
    for field in SUMMARY_CLAIM_FIELDS:
        text = summary.get(field)
        if not isinstance(text, str) or not text.strip():
            continue
        match = _BASE_VERSION_CLAIM_RE.search(text)
        if match:
            return {"source": field, "value": match.group(1)}
    return None


def extract_file_base_versions(state: Any) -> Dict[str, Any]:
    """`{json path: value}` for every structured `base_version` the file records."""
    found: Dict[str, Any] = {}
    for label, path in BASE_VERSION_PATHS:
        value = _dig(state, path)
        if value is not None:
            found[label] = value
    return found


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def audit_run_dir(run_dir: str, mtime_threshold: float = DEFAULT_MTIME_THRESHOLD_SECONDS) -> Dict[str, Any]:
    """Audit ONE directory holding `account_state.json`. Pure read, never writes."""
    state_path = os.path.join(run_dir, ACCOUNT_STATE_FILENAME)
    summary_path = os.path.join(run_dir, SUMMARY_FILENAME)

    result: Dict[str, Any] = {
        "dir": os.path.abspath(run_dir),
        "flagged": False,
        "fingerprints": [],
        "severity": "clean",
        "account_state_mtime": None,
        "summary_mtime": None,
        "mtime_delta_seconds": None,
        "ends_with_brace": None,
        "json_valid": None,
        "summary_claim": None,
        "file_base_versions": {},
        "differing_paths": [],
    }

    fingerprints: List[str] = []

    # ---- raw bytes: fingerprint B (must be read BEFORE parsing) ----
    try:
        with open(state_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        result["error"] = f"unreadable account_state.json: {exc}"
        result["fingerprints"] = ["B"]
        result["severity"] = "confirmed"
        result["flagged"] = True
        return result

    result["ends_with_brace"] = raw.endswith(b"}")
    if not result["ends_with_brace"]:
        fingerprints.append("B")

    # ---- mtimes: fingerprint A ----
    state_mtime = _mtime(state_path)
    summary_mtime = _mtime(summary_path)
    result["account_state_mtime"] = state_mtime
    result["summary_mtime"] = summary_mtime
    if state_mtime is not None and summary_mtime is not None:
        delta = state_mtime - summary_mtime
        result["mtime_delta_seconds"] = round(delta, 6)
        if delta > mtime_threshold:
            fingerprints.append("A")

    # ---- parse both JSON documents ----
    try:
        state = json.loads(raw.decode("utf-8"))
        result["json_valid"] = True
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        state = None
        result["json_valid"] = False
        result["error"] = f"account_state.json is not valid JSON: {exc}"

    summary = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as fh:
                summary = json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            summary = None

    result["summary_claim"] = extract_summary_claim(summary)

    if isinstance(state, dict):
        result["file_base_versions"] = extract_file_base_versions(state)

    # ---- fingerprint C: self-contradiction ----
    claim = result["summary_claim"]
    if claim and result["file_base_versions"]:
        claimed = str(claim["value"])
        differing = [
            label
            for label, value in result["file_base_versions"].items()
            if str(value) != claimed
        ]
        result["differing_paths"] = differing
        if differing:
            fingerprints.append("C")

    result["fingerprints"] = fingerprints
    result["flagged"] = bool(fingerprints)
    result["severity"] = (
        "confirmed" if ("B" in fingerprints or "C" in fingerprints)
        else "mtime_only" if fingerprints
        else "clean"
    )
    return result


def find_account_state_dirs(root: str) -> List[str]:
    """Every directory at/below `root` that holds an `account_state.json`, sorted."""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if ACCOUNT_STATE_FILENAME in filenames:
            found.append(dirpath)
    return sorted(found)


def audit_tree(root: str, mtime_threshold: float = DEFAULT_MTIME_THRESHOLD_SECONDS) -> Dict[str, Any]:
    results = [audit_run_dir(d, mtime_threshold) for d in find_account_state_dirs(root)]
    findings = [r for r in results if r["flagged"]]
    return {
        "root": os.path.abspath(root),
        "mtime_threshold_seconds": mtime_threshold,
        "scanned": len(results),
        "flagged": len(findings),
        "confirmed": sum(1 for r in findings if r["severity"] == "confirmed"),
        "mtime_only": sum(1 for r in findings if r["severity"] == "mtime_only"),
        "findings": findings,
        "results": results,
    }


def _fmt_value(value: Any) -> str:
    return "null" if value is None else str(value)


def _summary_line(result: Dict[str, Any]) -> str:
    claim = result.get("summary_claim") or {}
    if not claim:
        return "summary: no base_version claim"
    return f"summary {claim.get('source')} says base_version={_fmt_value(claim.get('value'))}"


def _file_line(result: Dict[str, Any]) -> str:
    versions = result.get("file_base_versions") or {}
    if not versions:
        return "file: no base_version field"
    rendered = ", ".join(f"{path}={_fmt_value(value)}" for path, value in versions.items())
    differing = result.get("differing_paths") or []
    if differing:
        rendered += f"  [differs: {', '.join(differing)}]"
    return f"file: {rendered}"


def render_report(audit: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"archive root : {audit['root']}")
    lines.append(
        f"scanned      : {audit['scanned']} dir(s) with {ACCOUNT_STATE_FILENAME}; "
        f"flagged: {audit['flagged']} (confirmed: {audit['confirmed']}, "
        f"mtime-only: {audit['mtime_only']}; threshold {audit['mtime_threshold_seconds']}s)"
    )
    lines.append("")
    if not audit["findings"]:
        lines.append("CLEAN: no account_state.json carries any of the three fingerprints.")
        return "\n".join(lines)

    header = f"{'A':<2}{'B':<2}{'C':<2}  {'severity':<11}  {'mtime delta':>12}  directory"
    lines.append(header)
    lines.append("-" * len(header))
    for result in audit["findings"]:
        marks = ["x" if code in result["fingerprints"] else "." for code in ("A", "B", "C")]
        delta = result["mtime_delta_seconds"]
        delta_text = "--" if delta is None else f"{delta:+.3f}s"
        lines.append(
            f"{marks[0]:<2}{marks[1]:<2}{marks[2]:<2}  {result['severity']:<11}  "
            f"{delta_text:>12}  {result['dir']}"
        )
        lines.append(f"      {_summary_line(result)}")
        lines.append(f"      {_file_line(result)}")
        lines.append("")
    lines.append("fingerprints:")
    for code, label in FINGERPRINT_LABELS.items():
        lines.append(f"  {code}: {label}")
    lines.append(
        "severity: confirmed = B/C (byte or content proof); mtime_only = A alone "
        "(the file was written after the round, content not contradicted)."
    )
    return "\n".join(lines)


def _archive_dir_from_settings() -> Optional[str]:
    """`archive_dir` from the live station's `settings.local.json`, or `None`.

    Strictly read-only and non-fatal: a missing / unparsable / key-less settings
    file means "this host has not declared an archive", not an error -- the next
    step of the resolution chain (env var) is still allowed to answer.  A
    malformed JSON is therefore NOT swallowed into a silent success: the caller
    ends up at the explicit "no root" usage error if nothing else resolves.
    """
    settings_path = os.path.join(_REPO_ROOT, SETTINGS_RELATIVE_PATH)
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(settings, dict):
        return None
    value = settings.get("archive_dir")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_default_root() -> Tuple[Optional[str], str]:
    """``(root, provenance)`` -- the implicit `--root`, or ``(None, reason)``.

    Order (see module docstring): settings file -> `$QUANT_LIVE_ARCHIVE_DIR` ->
    nothing.  An explicit `--root` never reaches here; it is handled first in
    `main()`, so this function only answers "what would we scan by default".
    """
    from_settings = _archive_dir_from_settings()
    if from_settings:
        return from_settings, f"settings.local.json archive_dir ({SETTINGS_RELATIVE_PATH})"

    from_env = os.environ.get(ARCHIVE_DIR_ENV_VAR, "").strip()
    if from_env:
        return from_env, f"${ARCHIVE_DIR_ENV_VAR}"

    return None, (
        f"neither {SETTINGS_RELATIVE_PATH} ('archive_dir') nor ${ARCHIVE_DIR_ENV_VAR} "
        f"declares an archive root"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of archived account_state.json files: mtime skew vs "
            "summary.json, non-json.dump byte tail, and self-contradictory base_version. "
            "Never writes anything."
        )
    )
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "archive root to scan recursively. When omitted it is resolved from "
            f"{SETTINGS_RELATIVE_PATH} ('archive_dir'), then ${ARCHIVE_DIR_ENV_VAR}; "
            "if neither is set there is NO default and the run exits 2."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the human table",
    )
    parser.add_argument(
        "--mtime-threshold-seconds",
        type=float,
        default=DEFAULT_MTIME_THRESHOLD_SECONDS,
        help=(
            "fingerprint A threshold: flag when mtime(account_state) - mtime(summary) "
            f"exceeds this many seconds (default: {DEFAULT_MTIME_THRESHOLD_SECONDS})"
        ),
    )
    return parser


def _print_missing_root_usage(reason: str) -> None:
    """Explain the (empty) resolution chain and how to fix it. Exit code 2."""
    print(
        "error: no archive root to scan -- "
        f"{reason}.\n"
        "\n"
        "Pass the tree explicitly, or declare it once:\n"
        f"  1. --root <runs-root>                       (always wins)\n"
        f"  2. {SETTINGS_RELATIVE_PATH}  ->  \"archive_dir\"\n"
        f"  3. ${ARCHIVE_DIR_ENV_VAR}=<runs-root>\n"
        "\n"
        "There is no built-in default on purpose: scanning the wrong tree would\n"
        "report a clean archive that was never looked at.\n"
        "\n"
        "Example:\n"
        "  python scripts/audit_live_archive_account_state.py --root <runs-root> --json",
        file=sys.stderr,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.root is not None:
        root, provenance = args.root, "--root"
    else:
        root, provenance = resolve_default_root()
        if root is None:
            _print_missing_root_usage(provenance)
            return 2

    if not os.path.isdir(root):
        print(
            f"error: archive root is not a directory: {root} (from {provenance})",
            file=sys.stderr,
        )
        return 2

    audit = audit_tree(root, args.mtime_threshold_seconds)
    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(render_report(audit))
    return 1 if audit["flagged"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

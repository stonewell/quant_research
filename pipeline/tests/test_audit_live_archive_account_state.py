"""Offline tests for `scripts/audit_live_archive_account_state.py` (D8).

FORENSIC CONTEXT (why this script exists -- the three fingerprints)
-------------------------------------------------------------------
Two archived rounds carry an `account_state.json` that was rewritten by hand AFTER the
round finished (2026-11-09/204716 and 2026-11-06/184826):

* the file's mtime is ~78-80s LATER than its `summary.json`;
* the file ends with a newline -- `StorageManager._save_json` uses `json.dump` and
  therefore never emits one (`tail == "}\n"` vs `"}"`);
* the file records `base_version = "2.0.0"` while the round's own `summary.json`
  refusal text still quotes the engine's view: `base_version = '1.8.0'`.

The third one is the strong evidence: the archive contradicts ITSELF, so no single
file has to be trusted.  The documented repair `2026-10-29/215823` has none of the
three (state and summary mtimes differ by 0.0013s, the file ends with `}`, and no
version claim is contradicted) and must NOT be reported.

This suite builds each fingerprint in `tmp_path` and pins:
  * one directory per fingerprint (A / B / C), each flagged for exactly that reason;
  * a clean directory that is NOT flagged;
  * the mtime threshold boundary (60s rather than "any positive delta");
  * exit codes (0 clean / 1 findings / 2 usage error) and the `--json` shape;
  * read-only behaviour (no file is touched by the scan).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_live_archive_account_state.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("audit_live_archive_account_state", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


audit = _load_script()

REAL_REFUSAL_TEXT = (
    "scripts.live_preflight.LiveDumpPreflightError: 版本元数据不一致（base_version）："
    "账户状态 `/tmp/x/account_state.json` 记录 `base_version` = '1.8.0'，"
    "而今日活类 base_version = '2.0.0'。票据不得以旧引擎的版本号写出；本条**不会**"
    "自动改写任何版本字段（禁止重新盖章）——请重新生成 dump。"
)


# ---------------------------------------------------------------------------
# fixtures: one directory per fingerprint, built from scratch
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exactly what `StorageManager._save_json` does: no trailing newline.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def _set_mtime(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def _state(base_version: str = "2.0.0", prior_base_version: str = "2.0.0"):
    return {
        "as_of_date": "2026-11-09",
        "current_nav": 197899.9,
        "base_version": base_version,
        "strategy_version": "1.2.2",
        "prior_state": {"as_of_date": "2026-11-06", "base_version": prior_base_version},
        "strategy_version_info": {"version": "1.2.2", "base_version": base_version},
    }


def _summary(error_detail: str = REAL_REFUSAL_TEXT):
    return {
        "date": "2026-11-09",
        "deploy_status": "FAILED",
        "error_detail": error_detail,
        "deploy_stderr": "",
    }


def _make_dir(root: Path, name: str, state, summary, *, state_mtime: float, summary_mtime: float,
              trailing_newline: bool = False) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "account_state.json"
    _write_json(state_path, state)
    if trailing_newline:
        with open(state_path, "ab") as fh:
            fh.write(b"\n")
    if summary is not None:
        _write_json(run_dir / "summary.json", summary)
        _set_mtime(run_dir / "summary.json", summary_mtime)
    _set_mtime(state_path, state_mtime)
    return run_dir


BASE = 1_790_000_000.0


@pytest.fixture
def tree(tmp_path):
    """A -> mtime skew; B -> trailing newline; C -> version contradiction; clean -> nothing."""
    root = tmp_path / "runs"

    a = _make_dir(
        root, "a_rewritten_after_failure", _state(), _summary(error_detail=""),
        state_mtime=BASE + 90.0, summary_mtime=BASE,
    )

    b = _make_dir(
        root, "b_editor_saved", _state(base_version="1.8.0", prior_base_version="1.8.0"),
        _summary(error_detail=""),
        state_mtime=BASE + 1.0, summary_mtime=BASE, trailing_newline=True,
    )

    c = _make_dir(
        root, "c_self_contradictory", _state(base_version="2.0.0"),
        _summary(),  # quotes 1.8.0
        state_mtime=BASE + 1.0, summary_mtime=BASE,
    )

    clean = _make_dir(
        root, "clean_matching", _state(base_version="1.8.0", prior_base_version="1.8.0"),
        _summary(),  # quotes 1.8.0 == what the file says
        state_mtime=BASE + 0.0013, summary_mtime=BASE,
    )

    return {"root": root, "a": a, "b": b, "c": c, "clean": clean}


# ---------------------------------------------------------------------------
# 1. Each fingerprint is independently sufficient, and reported by name
# ---------------------------------------------------------------------------
def test_each_fingerprint_is_reported_independently(tree):
    result = audit.audit_tree(str(tree["root"]))
    by_dir = {Path(item["dir"]).name: item for item in result["results"]}

    assert by_dir["a_rewritten_after_failure"]["fingerprints"] == ["A"]
    assert by_dir["a_rewritten_after_failure"]["severity"] == "mtime_only"

    assert by_dir["b_editor_saved"]["fingerprints"] == ["B"]
    assert by_dir["b_editor_saved"]["ends_with_brace"] is False

    assert by_dir["c_self_contradictory"]["fingerprints"] == ["C"]
    assert by_dir["c_self_contradictory"]["severity"] == "confirmed"
    assert by_dir["c_self_contradictory"]["summary_claim"] == {
        "source": "error_detail",
        "value": "1.8.0",
    }
    assert by_dir["c_self_contradictory"]["file_base_versions"] == {
        "/base_version": "2.0.0",
        "/prior_state/base_version": "2.0.0",
        "/strategy_version_info/base_version": "2.0.0",
    }
    assert by_dir["c_self_contradictory"]["differing_paths"] == [
        "/base_version",
        "/prior_state/base_version",
        "/strategy_version_info/base_version",
    ]

    assert by_dir["clean_matching"]["fingerprints"] == []
    assert by_dir["clean_matching"]["flagged"] is False
    assert by_dir["clean_matching"]["severity"] == "clean"

    assert result["scanned"] == 4
    assert result["flagged"] == 3
    # B and C are content/byte proof; A alone is mtime-only.
    assert result["confirmed"] == 2
    assert result["mtime_only"] == 1


def test_documented_repair_shape_is_not_reported(tmp_path):
    """`2026-10-29/215823`: +0.0013s, ends with `}`, no version claim -> CLEAN."""
    root = tmp_path / "runs"
    _make_dir(
        root, "2026-10-29/215823",
        {
            "as_of_date": "2026-10-29",
            "base_version": "1.6.0",
            "prior_state": {"as_of_date": "2026-10-27", "base_version": "1.6.0"},
        },
        {"date": "2026-10-29", "deploy_status": "SUCCESS", "error_detail": None},
        state_mtime=BASE + 0.001336, summary_mtime=BASE,
    )

    result = audit.audit_tree(str(root))
    assert result["flagged"] == 0
    assert result["findings"] == []


def test_threshold_boundary_is_sixty_seconds(tree):
    """A delta of exactly 60s is not '> 60s'; 60.5s is."""
    state = tree["a"] / "account_state.json"
    summary = tree["a"] / "summary.json"
    _set_mtime(summary, BASE)
    _set_mtime(state, BASE + 60.0)
    assert audit.audit_run_dir(str(tree["a"]))["fingerprints"] == []

    _set_mtime(state, BASE + 60.5)
    assert audit.audit_run_dir(str(tree["a"]))["fingerprints"] == ["A"]


# ---------------------------------------------------------------------------
# 2. Claim extraction: first quoted value wins, backticks tolerated, text-only
# ---------------------------------------------------------------------------
def test_claim_extraction_takes_the_recorded_value_not_the_live_one():
    claim = audit.extract_summary_claim({"error_detail": REAL_REFUSAL_TEXT})
    assert claim == {"source": "error_detail", "value": "1.8.0"}


def test_claim_extraction_falls_back_to_deploy_stderr():
    claim = audit.extract_summary_claim(
        {"error_detail": None, "deploy_stderr": "recorded base_version=\"1.7.0\" vs live 2.0.0"}
    )
    assert claim == {"source": "deploy_stderr", "value": "1.7.0"}


def test_claim_extraction_ignores_a_bare_mention_without_a_value():
    assert audit.extract_summary_claim({"error_detail": "base_version mismatch, see §5.4"}) is None
    assert audit.extract_summary_claim(None) is None


def test_contradiction_requires_a_claim_and_a_file_value(tmp_path):
    """No claim, or no version field, is 'not comparable' -- never a finding."""
    root = tmp_path / "runs"
    # claim in summary, no version field in the state -> C cannot fire
    _make_dir(
        root, "no_file_version",
        {"as_of_date": "2026-11-09"},
        _summary(),
        state_mtime=BASE, summary_mtime=BASE,
    )
    # version field in the state, silent summary -> C cannot fire
    _make_dir(
        root, "no_claim",
        _state(),
        {"error_detail": None, "deploy_stderr": ""},
        state_mtime=BASE, summary_mtime=BASE,
    )
    result = audit.audit_tree(str(root))
    assert result["flagged"] == 0


def test_only_the_differing_path_is_listed(tmp_path):
    root = tmp_path / "runs"
    _make_dir(
        root, "nested_only",
        _state(base_version="1.8.0", prior_base_version="2.0.0"),
        _summary(),  # quotes 1.8.0
        state_mtime=BASE, summary_mtime=BASE,
    )
    finding = audit.audit_run_dir(str(root / "nested_only"))
    assert finding["fingerprints"] == ["C"]
    assert finding["differing_paths"] == ["/prior_state/base_version"]


# ---------------------------------------------------------------------------
# 3. CLI surface: human table, --json, exit codes, read-only
# ---------------------------------------------------------------------------
def test_cli_exit_codes_and_json_payload(tree, capsys):
    assert audit.main(["--root", str(tree["root"])]) == 1
    human = capsys.readouterr().out
    assert "c_self_contradictory" in human
    assert "summary error_detail says base_version=1.8.0" in human
    assert "confirmed" in human

    assert audit.main(["--root", str(tree["root"]), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 4
    assert payload["flagged"] == 3
    assert {item["severity"] for item in payload["findings"]} == {"confirmed", "mtime_only"}

    clean_root = tree["root"] / "clean_matching"
    assert audit.main(["--root", str(clean_root)]) == 0
    assert "CLEAN" in capsys.readouterr().out


def test_cli_usage_error_for_a_missing_root(tmp_path):
    assert audit.main(["--root", str(tmp_path / "does-not-exist")]) == 2


def test_scan_is_read_only(tree):
    before = {
        os.path.join(dirpath, name): (os.path.getmtime(os.path.join(dirpath, name)),
                                      os.path.getsize(os.path.join(dirpath, name)))
        for dirpath, _dirnames, filenames in os.walk(str(tree["root"]))
        for name in filenames
    }
    assert audit.main(["--root", str(tree["root"]), "--json"]) == 1
    after = {
        os.path.join(dirpath, name): (os.path.getmtime(os.path.join(dirpath, name)),
                                      os.path.getsize(os.path.join(dirpath, name)))
        for dirpath, _dirnames, filenames in os.walk(str(tree["root"]))
        for name in filenames
    }
    assert after == before


def test_module_exposes_no_write_path():
    """The audit must not grow an `--apply`-style escape hatch."""
    parser = audit.build_parser()
    flags = {action.dest for action in parser._actions}
    assert flags == {"help", "root", "json", "mtime_threshold_seconds"}

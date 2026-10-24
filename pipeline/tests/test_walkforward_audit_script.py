"""Regression tests for two SILENT-FALLBACK defects in the `walkforward-audit` skill script.

`pipeline/.agents/skills/walkforward-audit/scripts/run_audit.py` had two fallbacks that
produced output which looked completely normal while being computed from the wrong input:

DEFECT 1 -- `deep_analyze_top_strategies()` used `if universe_file and os.path.isfile(...)`,
    so `--universe-file <typo / wrong-cwd path>` silently pruned against
    `sorted(all_observed_symbols)` instead. The Stage-5 "RECOMMENDED PRUNED UNIVERSE (N of M
    assets retained)" table then described a *different* base pool, with no hint anything
    had gone wrong. Fix: an explicitly supplied but unresolvable path is a hard error
    (`Error: universe file not found at '<path>'`, exit 1) raised by one shared helper
    (`_require_existing_input`) used by both path flags. Omitting the flag is still allowed
    (a legitimate convenience) but now prints a one-line stderr warning naming the fallback.

DEFECT 2 -- `find_results_dir()` fell back to `Path.cwd()/"results"`, so invoking the CLI
    from `pipeline/` silently audited `pipeline/results` -- exactly the throwaway/experimental
    directory `SKILL.md` forbids. Fix: the auto-discovered `<repo>/pipeline/results` is
    refused loudly (unless the operator passes it via `--results-dir`, which is honoured with
    a warning), and `main()` now prints which directory was chosen and why.

100% offline: a minimal `walkforward_summary.json` fixture only (no rebalances CSV, so the
script never reaches its optional universe-price loader). Nothing is written outside
`tmp_path`.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _PIPELINE_ROOT / ".agents" / "skills" / "walkforward-audit" / "scripts" / "run_audit.py"

# Minimal schema the loader actually requires: `load_all_summaries` only needs a parseable
# walkforward_summary.json; `run_ranking` reads mean_sharpe_ratio / mean_cagr /
# mean_max_drawdown / deflated_sharpe_ratio / rolling_window_performance, and Stage 4 reads
# rolling_window_performance[*].cagr. No `walkforward_rebalances.csv` is written, so the
# trade/asset auditors return None and no price loading is attempted.
_SUMMARY_PAYLOAD = {
    "strategy_name": "Synthetic Audit Fixture",
    "mean_sharpe_ratio": 0.90,
    "mean_cagr": 0.10,
    "mean_max_drawdown": 0.15,
    "mean_calmar_ratio": 0.60,
    "deflated_sharpe_ratio": 0.60,
    "n_folds": 2,
    "rolling_window_performance": [
        {"start_date": "2020-01-01", "end_date": "2020-12-31",
         "cagr": 0.12, "sharpe_ratio": 1.0, "max_drawdown": 0.10},
        {"start_date": "2021-01-01", "end_date": "2021-12-31",
         "cagr": 0.08, "sharpe_ratio": 0.8, "max_drawdown": 0.20},
    ],
}


@pytest.fixture(scope="module")
def audit():
    """Import the skill script by path (its dotted `.agents` parent is not importable)."""
    spec = importlib.util.spec_from_file_location("wf_run_audit_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_results_dir(base: Path, run_name: str = "run_a") -> Path:
    """Create `<base>/<run_name>/walkforward_summary.json` -- one auditable strategy run."""
    run = base / run_name
    run.mkdir(parents=True, exist_ok=True)
    (run / "walkforward_summary.json").write_text(json.dumps(_SUMMARY_PAYLOAD))
    return base


def _run_main(audit_module, monkeypatch, argv, cwd):
    """Run `main()` in-process; return its exit code (0 when it returns normally)."""
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "argv", ["run_audit.py", *argv])
    try:
        audit_module.main()
        return 0
    except SystemExit as exc:  # argparse + the script's own sys.exit(...) paths
        return exc.code if exc.code is not None else 0


# ---------------------------------------------------------------------------
# DEFECT 1 -- explicit `--universe-file` must never degrade into the fallback
# ---------------------------------------------------------------------------
def test_missing_universe_file_fails_fast_and_does_not_audit(audit, monkeypatch, capsys, tmp_path):
    missing = "/nonexistent/definitely_not_a_pool.txt"
    results = _write_results_dir(tmp_path / "results")

    code = _run_main(
        audit, monkeypatch,
        ["--results-dir", str(results), "--exclude-losers", "--universe-file", missing],
        tmp_path,
    )

    assert code == 1, "a supplied but unresolvable --universe-file must exit non-zero"
    out, err = capsys.readouterr()
    assert f"Error: universe file not found at '{missing}'" in err
    assert "when --universe-file is omitted entirely" in err, (
        f"the guard must state when the fallback is legitimate; stderr was:\n{err}"
    )
    assert "RECOMMENDED PRUNED UNIVERSE" not in out, "no analysis may run after the guard fires"
    assert "observed-symbol fallback set" not in err, "the fallback warning is not an error path"


def test_omitted_universe_file_still_falls_back_but_warns(audit, monkeypatch, capsys, tmp_path):
    results = _write_results_dir(tmp_path / "results")

    code = _run_main(
        audit, monkeypatch, ["--results-dir", str(results), "--exclude-losers"], tmp_path
    )

    assert code == 0
    out, err = capsys.readouterr()
    assert "Warning: no --universe-file supplied" in err
    assert "observed-symbol fallback set" in err
    assert "NOT a filtered or complete universe" in err
    # No rebalances CSV -> no observed symbols -> the fallback base is provably empty.
    assert "RECOMMENDED PRUNED UNIVERSE (0 of 0 assets retained)" in out


def test_relative_universe_file_resolves_without_fallback(audit, monkeypatch, capsys, tmp_path):
    results = _write_results_dir(tmp_path / "results")
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "universe.txt").write_text("# synthetic pool\nAAA, BBB\nCCC\n")

    code = _run_main(
        audit, monkeypatch,
        ["--results-dir", str(results), "--exclude-losers", "--universe-file", "pool/universe.txt"],
        tmp_path,
    )

    assert code == 0
    out, err = capsys.readouterr()
    assert "no --universe-file supplied" not in err, "a resolved pool must not warn about the fallback"
    assert "RECOMMENDED PRUNED UNIVERSE (3 of 3 assets retained)" in out, (
        "the pruning base must be the 3 symbols parsed from the supplied relative path"
    )


# ---------------------------------------------------------------------------
# DEFECT 2 -- auto-discovered <repo>/pipeline/results must be refused loudly
# ---------------------------------------------------------------------------
def test_repo_pipeline_results_is_the_guarded_path(audit, tmp_path):
    assert audit._PIPELINE_PROJECT_DIR.name == "pipeline" and (
        audit._PIPELINE_PROJECT_DIR.parent / "common"
    ).is_dir(), "the guard's repo anchor drifted; refusing the wrong directory is meaningless"
    assert audit._is_pipeline_throwaway_results(audit._PIPELINE_PROJECT_DIR / "results")
    assert not audit._is_pipeline_throwaway_results(tmp_path / "results")


def test_auto_discovered_pipeline_results_is_refused(audit, monkeypatch, capsys, tmp_path):
    fake_pipeline = tmp_path / "repo" / "pipeline"
    fake_results = _write_results_dir(fake_pipeline / "results")
    monkeypatch.setattr(audit, "_PIPELINE_PROJECT_DIR", fake_pipeline)

    code = _run_main(audit, monkeypatch, ["--summary"], fake_pipeline)

    assert code == 1, "cwd-relative discovery of pipeline/results must fail, not audit it"
    out, err = capsys.readouterr()
    assert f"Error: refusing to auto-audit '{fake_results.resolve()}'" in err
    assert "throwaway/experimental runs" in err
    assert str(fake_results.resolve()) in err
    assert "Using results directory" not in out, "nothing may be reported as audited"
    assert "ANOMALY-ADJUSTED STRATEGY RANKINGS" not in out


def test_explicit_results_dir_on_pipeline_results_is_honoured_with_a_warning(
    audit, monkeypatch, capsys, tmp_path
):
    fake_pipeline = tmp_path / "repo" / "pipeline"
    fake_results = _write_results_dir(fake_pipeline / "results")
    monkeypatch.setattr(audit, "_PIPELINE_PROJECT_DIR", fake_pipeline)

    code = _run_main(audit, monkeypatch, ["--results-dir", str(fake_results), "--summary"], tmp_path)

    assert code == 0, "explicit operator intent wins over the throwaway-dir guard"
    out, err = capsys.readouterr()
    assert "Warning: --results-dir explicitly selects" in err
    assert f"(selected because --results-dir was passed explicitly)" in out
    assert "Synthetic Audit Fixture" in out


# ---------------------------------------------------------------------------
# Explicit `--results-dir` must resolve and must contain summaries
# ---------------------------------------------------------------------------
def test_explicit_results_dir_is_reported_with_its_reason(audit, monkeypatch, capsys, tmp_path):
    results = _write_results_dir(tmp_path / "results_ok")

    code = _run_main(audit, monkeypatch, ["--results-dir", str(results), "--summary"], tmp_path)
    out, err = capsys.readouterr()

    assert code == 0
    assert f"Using results directory: {results.resolve()}" in out
    assert "(selected because --results-dir was passed explicitly)" in out
    assert err == "", f"a clean explicit run must not warn; stderr was:\n{err}"


def test_auto_discovered_results_dir_is_reported_with_its_reason(
    audit, monkeypatch, capsys, tmp_path
):
    _write_results_dir(tmp_path / "results")

    code = _run_main(audit, monkeypatch, ["--summary"], tmp_path)
    out, err = capsys.readouterr()

    assert code == 0
    assert f"Using results directory: {(tmp_path / 'results').resolve()}" in out
    assert "auto-discovered under cwd=" in out
    assert str(tmp_path) in out
    assert err == ""


def test_explicit_results_dir_without_summaries_is_not_a_silent_success(
    audit, monkeypatch, capsys, tmp_path
):
    empty = tmp_path / "empty_results"
    empty.mkdir()

    code = _run_main(audit, monkeypatch, ["--results-dir", str(empty), "--summary"], tmp_path)

    assert code == 1
    out, err = capsys.readouterr()
    assert "No walkforward_summary.json files found." in err, "pre-existing message kept verbatim"
    assert "was given explicitly" in err
    assert "Total strategies" not in out


def test_missing_explicit_results_dir_fails_cleanly(audit, monkeypatch, capsys, tmp_path):
    code = _run_main(audit, monkeypatch, ["--results-dir", "no_such_dir", "--summary"], tmp_path)

    assert code == 1
    _, err = capsys.readouterr()
    assert "Error: results directory not found at 'no_such_dir'" in err
    assert "Traceback" not in err


def test_help_still_exits_zero(audit, monkeypatch, capsys, tmp_path):
    code = _run_main(audit, monkeypatch, ["--help"], tmp_path)

    assert code == 0
    out, _ = capsys.readouterr()
    assert "--universe-file" in out and "--results-dir" in out

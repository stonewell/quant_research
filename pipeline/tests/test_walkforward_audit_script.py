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


# ---------------------------------------------------------------------------
# Turnover ceiling on the letter Safety Grade (`TURNOVER_CEILING_ANN`, 2026-10-25 ruling)
#
# The ceiling is a HARD cap on TWO-SIDED annualised turnover: a run above 10.0x/yr cannot be
# graded A. What is asserted here:
#   * the metric is per-fold annualised on calendar 365.25 d/y and THEN averaged -- never
#     `Σturnover / Σyears`;
#   * unmeasurable turnover withholds A (never silently scores 0.0) and renders `—`;
#   * a degenerate zero-length fold is excluded and reported in the `n/N` count;
#   * only the LETTER moves: the numeric Overfit Risk Index and the ranking's Adj Sharpe are
#     byte-identical with and without the cap.
#
# Fixtures are synthetic summaries only -- no rebalances CSV, no prices, no network.
# `total_turnover` is built from the target annualised value via the same calendar formula the
# metric uses, so the fixture states "this fold ran at Nx/yr" and the assertion is about the
# ceiling, not about float luck.
# ---------------------------------------------------------------------------
_GRADE_CEILING_APPLIED = "Grade ceiling applied"
_CAPPED_A_TO_B = "Grade B (capped from A by turnover ceiling)"
_A_LABEL = "Grade A (Institutional Grade - Highly Robust)"
_B_LABEL = "Grade B (Moderate Caution - Viable with Sizing Rules)"


def _fold(start, end, ann_turnover, cagr=0.10):
    """One `rolling_window_performance` fold. `ann_turnover=None` omits `total_turnover`."""
    from datetime import date

    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    fold = {
        "start_date": start,
        "end_date": end,
        "cagr": cagr,
        "sharpe_ratio": 1.0,
        "max_drawdown": 0.10,
    }
    if ann_turnover is not None:
        fold["total_turnover"] = ann_turnover * (days / 365.25)
    return fold


def _write_turnover_run(base: Path, folds, run_name: str = "run_a", **overrides) -> Path:
    """Write one summary. `**overrides` patches top-level payload fields (e.g. the DSR)."""
    payload = json.loads(json.dumps(_SUMMARY_PAYLOAD))
    payload["rolling_window_performance"] = folds
    payload["n_folds"] = len(folds)
    payload.update(overrides)
    run = base / run_name
    run.mkdir(parents=True, exist_ok=True)
    (run / "walkforward_summary.json").write_text(json.dumps(payload))
    return base


def _run_turnover_audit(audit_module, monkeypatch, capsys, base: Path, folds,
                        **overrides) -> str:
    """Audit one synthetic run; return stdout. Clears the degradation registry first.

    `_DEGRADED_RUNS` is module-global and survives in-process `main()` calls, so a section
    flagged by an earlier test would otherwise turn this exit code into 3.
    """
    results = _write_turnover_run(base, folds, **overrides)
    audit_module._DEGRADED_RUNS.clear()
    code = _run_main(
        audit_module, monkeypatch,
        ["--results-dir", str(results), "--abnormal-overfit",
         "--top-n", "1", "--deep-top-n", "1"],
        base,
    )
    out, err = capsys.readouterr()
    assert code == 0, f"turnover assertion fixture must exit 0; stderr was:\n{err}"
    return out


def _ranking_row(out: str) -> str:
    """The one ANOMALY-ADJUSTED ranking row (the fixture has a single strategy)."""
    section = out.split("ANOMALY-ADJUSTED STRATEGY RANKINGS")[1].split("Turnover convention:")[0]
    rows = [
        ln for ln in section.splitlines()
        if "Synthetic Audit Fixture" in ln and not ln.lstrip().startswith("[")
    ]
    assert len(rows) == 1, f"expected one ranking row, got:\n{section}"
    return rows[0]


def test_turnover_ann_exactly_at_ceiling_does_not_cap(audit, monkeypatch, capsys, tmp_path):
    # Two 1-year folds, each exactly 10.0x/yr -> mean 10.0. Strict `>` means 10.0 passes.
    folds = [
        _fold("2020-01-01", "2021-01-01", 10.0),
        _fold("2021-01-01", "2022-01-01", 10.0),
    ]

    out = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "results", folds)

    assert _A_LABEL in out, "exactly 10.0x/yr is NOT above the ceiling and must stay Grade A"
    assert _GRADE_CEILING_APPLIED not in out
    assert _CAPPED_A_TO_B not in out
    tail = _ranking_row(out).split()[-3:]
    assert tail[0] == "10.00" and tail[2] == "2/2", f"unexpected turnover columns: {tail}"


def test_turnover_ann_above_ceiling_caps_a_to_b(audit, monkeypatch, capsys, tmp_path):
    folds = [
        _fold("2020-01-01", "2021-01-01", 10.0001),
        _fold("2021-01-01", "2022-01-01", 10.0001),
    ]

    out = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "results", folds)

    assert _CAPPED_A_TO_B in out, "10.0001x/yr is above the ceiling and must withhold the A"
    assert f"{_GRADE_CEILING_APPLIED}: two-sided annualised turnover" in out
    assert "> 10.0 ceiling" in out
    assert _A_LABEL not in out


def test_turnover_unmeasurable_withholds_a(audit, monkeypatch, capsys, tmp_path):
    # No `total_turnover` on any fold: the metric is UNKNOWN, which must not be scored 0.0.
    folds = [
        _fold("2020-01-01", "2021-01-01", None),
        _fold("2021-01-01", "2022-01-01", None),
    ]

    out = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "results", folds)

    assert _CAPPED_A_TO_B in out
    assert f"{_GRADE_CEILING_APPLIED}: turnover unmeasurable ⇒ A withheld" in out
    assert _A_LABEL not in out
    tail = _ranking_row(out).split()[-3:]
    assert tail == ["—", "—", "0/2"], (
        f"an unmeasurable turnover must render as an em dash (never 0.00): {tail}"
    )


def test_turnover_ceiling_leaves_score_and_index_untouched(audit, monkeypatch, capsys, tmp_path):
    low = [_fold("2020-01-01", "2021-01-01", 3.0), _fold("2021-01-01", "2022-01-01", 3.0)]
    high = [_fold("2020-01-01", "2021-01-01", 30.0), _fold("2021-01-01", "2022-01-01", 30.0)]

    out_low = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "low", low)
    out_high = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "high", high)

    import re

    def indices(out):
        # The audit prints exactly one safety block for the single strategy.
        return re.search(r"Overfit Risk Index: (\d+) / 100", out).group(1)

    def adj(out):
        return re.search(r"(\d+\.\d{3})\s+(-?\d+\.\d{3})\s", _ranking_row(out)).group(2)

    assert indices(out_low) == indices(out_high), (
        "the turnover ceiling must not move the numeric Overfit Risk Index"
    )
    assert adj(out_low) == adj(out_high), (
        "the turnover ceiling must not move the ranking index (adj Sharpe)"
    )
    assert _A_LABEL in out_low and _CAPPED_A_TO_B in out_high


def test_turnover_annualise_per_fold_then_mean_not_sum_over_sum(audit):
    # Fold 1: 1 year at 5x/yr. Fold 2: 2 years at 20x/yr.
    # Per-fold-then-mean  -> (5 + 20) / 2 = 12.5
    # Σturnover/Σyears    -> (5.01 + 39.97) / 3.00 = 14.99  (WRONG: fold length leaks in)
    from datetime import date

    folds = [
        _fold("2020-01-01", "2021-01-01", 5.0),
        _fold("2021-01-01", "2023-01-01", 20.0),
    ]

    ann, raw, measurable = audit.turnover_annualised_from_folds(folds)
    span_years = (date(2023, 1, 1) - date(2020, 1, 1)).days / 365.25
    sum_over_sum = sum(f["total_turnover"] for f in folds) / span_years

    assert measurable == "2/2"
    assert ann == pytest.approx(12.5, abs=1e-9), "per-fold annualise THEN mean"
    assert sum_over_sum == pytest.approx(14.99, rel=1e-3), "the rejected aggregation's value"
    assert ann != pytest.approx(sum_over_sum), "the two aggregations must genuinely differ"
    assert raw == pytest.approx((5.0 * 366 / 365.25 + 20.0 * 730 / 365.25) / 2, rel=1e-9)


def test_turnover_skips_nonpositive_day_fold(audit, monkeypatch, capsys, tmp_path):
    # A zero-length fold has no annualisation; it stays in N but is not measurable.
    folds = [
        _fold("2021-01-01", "2021-01-01", 11.0),
        _fold("2021-01-01", "2022-01-01", 11.0),
    ]

    ann, _, measurable = audit.turnover_annualised_from_folds(folds)
    assert measurable == "1/2"
    assert ann == pytest.approx(11.0, abs=1e-9), "the surviving fold must be annualised alone"

    out = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "results", folds)
    tail = _ranking_row(out).split()[-3:]
    assert tail[2] == "1/2", f"the Folds column must report 1/2: {tail}"
    assert _CAPPED_A_TO_B in out


def test_turnover_convention_note_is_one_line(audit, monkeypatch, capsys, tmp_path):
    folds = [_fold("2020-01-01", "2021-01-01", 3.0), _fold("2021-01-01", "2022-01-01", 3.0)]

    out = _run_turnover_audit(audit, monkeypatch, capsys, tmp_path / "results", folds)

    note_lines = [ln for ln in out.splitlines() if ln.startswith("Turnover convention:")]
    assert len(note_lines) == 1, "the convention disclosure must be printed exactly once"
    note = note_lines[0]
    assert "two-sided" in note and "365.25" in note
    assert "NOT Σturnover/Σyears" in note


def test_turnover_ceiling_does_not_touch_grade_below_a(audit, monkeypatch, capsys, tmp_path):
    # The ceiling is an A-WITHHOLDING device, never a general de-grader: a run that already
    # grades B/C on merit must come out with the SAME grade no matter how much it trades.
    # This fixture is B on merit. Without a rebalances.csv the only score lever reachable from
    # the summary is the DSR gate, so `deflated_sharpe_ratio = 0.0` (< 0.05) contributes +25
    # => Overfit Risk Index 25 => Grade B -- already below A before turnover is looked at.
    # Turnover is 20x/yr, i.e. TWICE the ceiling, so if the cap ever widened from "only A" to
    # "all grades", the label would flip to `Grade B (capped from A by turnover ceiling)` and
    # every assertion below would go red.
    folds = [
        _fold("2020-01-01", "2021-01-01", 20.0),
        _fold("2021-01-01", "2022-01-01", 20.0),
        _fold("2022-01-01", "2023-01-01", 20.0),
    ]

    out = _run_turnover_audit(
        audit, monkeypatch, capsys, tmp_path / "results", folds,
        deflated_sharpe_ratio=0.0,
    )

    # Precondition: this fixture really is above the ceiling and really is B on merit.
    assert "Turnover/yr" in out
    tail = _ranking_row(out).split()[-3:]
    assert tail[0] == "20.00", f"the fixture must trade well above the 10.0 ceiling: {tail}"
    assert "Overfit Risk Index: 25 / 100" in out, "expected a merit-based B (index 25)"
    assert _B_LABEL in out, "a merit-based B must survive a 20x/yr turnover unchanged"

    # ... and the ceiling must not have fired at all.
    assert _CAPPED_A_TO_B not in out, "the A-withholding ceiling must never re-label a B"
    assert _GRADE_CEILING_APPLIED not in out, (
        "no `Grade ceiling applied:` line may be emitted for a grade that was never A"
    )
    assert _A_LABEL not in out, "sanity: this fixture is B on merit, not a capped A"


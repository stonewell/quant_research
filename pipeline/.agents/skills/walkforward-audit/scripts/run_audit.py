#!/usr/bin/env python3
"""Walkforward Audit & Trading Record Anomaly Analyzer.

Automates the 3-stage quantitative evaluation process:
1. Stage 1: Cross-strategy walkforward performance aggregation & DSR calculation
2. Stage 2: Fold-level trading record anomaly detection (concentration, warmup, turnover, jumps, look-ahead)
3. Stage 3: Anomaly-adjusted Sharpe ranking and live trading risk scoring

Usage:
  python run_audit.py [--all] [--summary] [--audit] [--rank] [--top-n 10] [--results-dir PATH]

Exit codes:
  0 = clean run  |  1 = input/validation error  |  2 = missing third-party dependency
  3 = completed, but at least one section ran on degraded inputs (numbers DISQUALIFIED)
"""

import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# Resolved location of the `pipeline/` project this script ships inside. Used only to
# recognise `<repo>/pipeline/results`, the throwaway/experimental directory SKILL.md forbids
# auditing, so an auto-discovered fallback can be refused instead of silently audited.
# `parents[4]` is `pipeline/`: scripts -> walkforward-audit -> skills -> .agents -> pipeline.
_PIPELINE_PROJECT_DIR = Path(__file__).resolve().parents[4]

_UNIVERSE_FALLBACK_HINT = (
    "The observed-symbol fallback is used ONLY when --universe-file is omitted entirely; "
    "a supplied path is never silently ignored."
)
_RESULTS_DIR_HINT = (
    "--results-dir must name an existing directory holding walkforward_summary.json files."
)
_PIPELINE_RESULTS_REFUSAL_HINT = (
    "<repo>/pipeline/results holds throwaway/experimental runs, not auditable walkforward output. "
    "Pass --results-dir explicitly (or run from the repo root) to select the intended directory."
)


# --------------------------------------------------------------------------- #
# Startup preconditions: missing third-party deps are FATAL, never degradable
# --------------------------------------------------------------------------- #
# Every asset-level number Stages 4-6 print is marked to the *final close* of each fold,
# which needs `common.cli_utils.load_universe_with_banner` -> `common.data` /
# `common.duckdb_cache`, and those modules import pandas / numpy / yfinance / duckdb at
# module scope. On an interpreter without them the import raises ImportError.
#
# Until 2026-10-25 that ImportError landed in a bare `except Exception:` inside
# `audit_strategy_assets()` which set `universe = {}` and carried on in a degraded mode:
# every still-open position was then valued at its LAST TRADE PRICE instead of the final
# close. Measured on real output (backtests_research/20261025_125300, system python3 vs
# pipeline/.venv/bin/python3) that under-counted per-ticker PnL by 10.7k-31.0k RMB per
# run, flipped one strategy's net PnL from +28,697.00 to -2,259.06 RMB, made the universe
# pruner wrongly exclude 10 assets, and surfaced the degenerate `999.0%` / `100.0%`
# placeholders in the Superstar-Fragility / Friction-Bleed rows (131.8% / 22.8% instead of
# the correct 69.8% / 8.7%).
#
# A diagnostic that silently degrades to wrong numbers is worse than one that crashes, so
# the deps are verified once, up front, and a missing one is a hard `sys.exit(2)` naming
# the interpreter that was actually used.
_REQUIRED_THIRD_PARTY_DEPS = ("pandas", "numpy", "scipy", "yfinance", "duckdb")

# Columns `walkforward_rebalances.csv` must carry for the audit to mean anything. Until
# 2026-10-25 a missing column was read as 0.0 by `row.get(k, 0.0)`, i.e. a silently wrong
# metric instead of an error; both CSV readers now check this tuple up front.
_REBALANCE_CSV_COLUMNS = (
    "fold", "rebalance_id", "date", "symbol", "action", "price", "prior_weight",
    "target_weight", "weight_change", "trade_value", "shares", "prior_shares",
    "target_shares", "commission", "slippage", "total_cost", "portfolio_equity",
)


# --------------------------------------------------------------------------- #
# Turnover ceiling on the LETTER safety grade (added 2026-10-25)
# --------------------------------------------------------------------------- #
# A hard ceiling on TWO-SIDED annualised turnover: a run above it cannot be graded A. Only
# the LETTER changes -- the numeric Overfit Risk Index and the ranking sort key are untouched
# (see `audit_abnormal_and_overfit`), so historical Index values stay comparable.
#
# METRIC BASIS -- two-sided, NOT one-way. `total_turnover` in a fold of
# `walkforward_summary.json`'s `rolling_window_performance` is `Σ|trade_value| / equity`,
# i.e. the absolute traded notional summed over BOTH legs and divided by equity; a
# buy+sell round trip therefore counts twice. Measured on `backtests_research/20261025_125300`
# (18 auditable runs, per-fold annualised): min 9.74x/yr, median 14.59x/yr, mean 16.44x/yr,
# max 31.81x/yr, with 17 of the 18 above the 10.0 ceiling. The single-point example
# `03_r2b_oos/chan_dual_hybrid_blend` measures 15.09x/yr -- just above the median, NOT the
# upper bound (the generic blends reach 27.06 / 28.83 / 31.81x/yr). At the 10.0-12.5bp one-way
# cost this batch was run with, 15.09x/yr implies 1.51-1.89%/yr of friction -- which is exactly
# the 1.4-1.9%/yr that `backtests_research/20261025_125300/FORENSIC_AUDIT.md` (section 6.4)
# derives independently from the fee rows. Read as ONE-WAY, the same data would predict only
# 0.76-0.95%/yr and could not reproduce the independently measured friction. The misreading
# breaks harder at the top of the range: the 31.81x/yr maximum is 3.18-3.98%/yr two-sided,
# but only 1.59-1.99%/yr read as one-way. So: never describe this metric as one-way.
#
# NORMALISATION -- per-fold annualise, then mean (see `turnover_annualised_from_folds`), NOT
# `Σturnover / Σyears`. The alternative divides a sum of per-fold totals by the whole
# calendar span, so it silently mixes fold length into the answer: on
# `01_r1_longwin/chan_risk_managed_blend` it reports 36.70x/yr (Σ 104.99 / Σ 2.86 y) purely
# because that run's folds span 756 days while `03_r2b_oos`'s span 97-112 days -- the raw
# per-fold totals of the two batches (19-29 vs 2.5-5.9) differ by the ~8x fold-length ratio,
# not by behaviour. Per-fold-then-mean reports the same run at 12.67x/yr.
#
# COMPARISON IS STRICT `>`: exactly 10.0 passes; 10.0001 is capped.
#
# PRE-REGISTRATION CONTEXT -- `docs/development_plan.md` L2.49 / L2.36 pre-register an
# 8.0x/yr PASS line and a 10.0x/yr HARD CAP on a SINGLE-SIDED basis. Two-sided 10.0 ==
# single-sided 5.0, so this ceiling is ~37.5% stricter than that pass line and 2x stricter
# than that hard cap. The tightening is a DELIBERATE USER RULING on 2026-10-25
# ("选的 C = 双边 10.0"), not an oversight; the plan rows are being amended separately.
# Do not "reconcile" the two numbers by adopting the looser one.
TURNOVER_CEILING_ANN = 10.0

# One-line convention disclosure printed next to the turnover columns (never a new section).
TURNOVER_CONVENTION_NOTE = (
    "Turnover convention: two-sided Σ|trade_value|/equity, per-fold annualised on calendar "
    "365.25 d/y then averaged over folds (NOT Σturnover/Σyears). Trading-day convention "
    "(63 bars ≈ 0.25 y) differs by ≈3.5%; the 10.0 ceiling sits 2.7% above the lowest "
    "observed 9.738×/yr, so it is a knife-edge — stated for the record."
)


def _venv_python_hint() -> str:
    """The `pipeline/` venv interpreter as it is usually invoked (repo-relative)."""
    rel = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python3")
    return str(Path("pipeline/.venv") / rel)


def _require_third_party_deps() -> None:
    """Hard-fail (`sys.exit(2)`) when a dependency the audit genuinely needs is missing.

    Called from `main()` before any input validation or audit phase runs, so a degraded
    run cannot start at all. Deliberately intolerant -- see the block comment above for
    the silent-degradation bug this replaces.
    """
    rel = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python3")
    venv_python = _PIPELINE_PROJECT_DIR / ".venv" / rel
    missing = []
    for name in _REQUIRED_THIRD_PARTY_DEPS:
        try:
            __import__(name)
        except Exception as exc:  # ImportError, but also a broken .so (OSError) -- both fatal
            missing.append(f"{name} ({type(exc).__name__}: {exc})")
    if not missing:
        return
    print(
        "Error: this script requires "
        + "/".join(_REQUIRED_THIRD_PARTY_DEPS)
        + f"; current interpreter: {sys.executable} "
        f"(python {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}). "
        f"Re-run with {_venv_python_hint()}.",
        file=sys.stderr,
    )
    for item in missing:
        print(f"       missing: {item}", file=sys.stderr)
    print(f"       venv interpreter: {venv_python}", file=sys.stderr)
    sys.exit(2)


# Data-quality degradation registry: `(strategy, reason)` pairs, printed as one end-of-run
# banner by `_report_degradation_summary()`. A run that lands here still prints numbers,
# but those numbers are explicitly disqualified instead of silently trusted.
_DEGRADED_RUNS: list = []


def _warn(message: str) -> None:
    """Loud, non-fatal warning; always stderr, never swallowed."""
    print(f"Warning: {message}", file=sys.stderr)


def _flag_degraded(strategy: str, reason: str, *, disqualifying: bool = True) -> None:
    """Report a degraded computation on BOTH streams (stdout survives a `2>/dev/null`).

    `disqualifying=True` additionally records the section in `_DEGRADED_RUNS` so the run
    ends with an unmissable summary: the numbers were produced from incomplete inputs and
    must not be reported as findings.
    """
    where = f"[{strategy}] " if strategy else ""
    label = "WARNING (numbers disqualified)" if disqualifying else "Warning (degraded metric)"
    line = f"{label}: {where}{reason}"
    print(line, file=sys.stderr)
    # `flush=True` so this lands AFTER any buffered report text in a `2>&1` capture.
    print(f"  \u26a0\ufe0f  {line}", flush=True)
    if disqualifying:
        _DEGRADED_RUNS.append((strategy or "<run>", reason))


def _report_degradation_summary() -> None:
    """End-of-run banner listing every section whose numbers are not trustworthy.

    Exits with code **3** when anything was disqualified, so a degraded run can never be
    mistaken for a clean one by a script (or a human skimming the tail). Exit-code contract:
    0 = clean, 1 = input/validation error, 2 = missing third-party dependency,
    3 = completed but the numbers are DISQUALIFIED.
    """
    if not _DEGRADED_RUNS:
        return
    print("\n" + "!" * 140, file=sys.stderr)
    print(
        f"DATA-QUALITY FAILURE: {len(_DEGRADED_RUNS)} audit section(s) ran on degraded inputs. "
        "Their numbers are NOT trustworthy and must not be reported as findings:",
        file=sys.stderr,
    )
    for strat, reason in _DEGRADED_RUNS:
        print(f"  - {strat}: {reason}", file=sys.stderr)
    print("!" * 140, file=sys.stderr)
    exit_line = (
        f"Exiting with code 3: {len(_DEGRADED_RUNS)} audit section(s) ran on degraded inputs."
    )
    print(exit_line, file=sys.stderr)
    # Mirrored to stdout (flushed last) so the reason survives a `> report.txt 2>/dev/null`.
    print(exit_line, flush=True)
    sys.exit(3)


class AuditInputError(FileNotFoundError):
    """An explicitly supplied CLI input could not be resolved.

    Subclasses FileNotFoundError so any pre-existing caller catching the old
    FileNotFoundError contract keeps working. Raised instead of falling back, because
    both fallbacks this replaces (universal observed-symbol set / `./results`) produced
    output that looked completely normal while being computed from the wrong input.
    """

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _require_existing_input(explicit_path, label: str, hint: str, kind: str = "file"):
    """Resolve an explicitly supplied CLI path, or hard-fail -- never silently substitute.

    Shared by `--universe-file` and `--results-dir` so the "given but missing" rule and its
    error wording live in exactly one place. `None` (flag omitted) returns `None`; the
    caller then owns whatever deliberate fallback it documents.
    """
    if explicit_path is None:
        return None
    p = Path(explicit_path)
    ok = p.is_dir() if kind == "dir" else p.is_file()
    if not ok:
        raise AuditInputError(f"{label} not found at '{explicit_path}'", hint)
    return p


def _is_pipeline_throwaway_results(path: Path) -> bool:
    """True when `path` is `<repo>/pipeline/results` (the directory to never auto-audit)."""
    try:
        return Path(path).resolve() == (_PIPELINE_PROJECT_DIR / "results").resolve()
    except OSError:
        return False


def find_results_dir(explicit_path=None) -> Path:
    explicit = _require_existing_input(
        explicit_path, "results directory", _RESULTS_DIR_HINT, kind="dir"
    )
    if explicit is not None:
        resolved = explicit.resolve()
        if _is_pipeline_throwaway_results(resolved):
            # Explicit operator intent wins, but say so out loud: this is the one directory
            # SKILL.md tells agents not to use, so an explicit pass is worth flagging.
            print(
                f"Warning: --results-dir explicitly selects {resolved}, which holds "
                "throwaway/experimental runs; continuing because it was requested.",
                file=sys.stderr,
            )
        return resolved

    # Only check ./results in the current working directory if no explicit path given
    cwd_results = Path.cwd() / "results"
    if cwd_results.is_dir():
        resolved = cwd_results.resolve()
        if _is_pipeline_throwaway_results(resolved):
            # Previously this silently audited pipeline/results when the CLI was invoked
            # from pipeline/ -- a clean-looking report over throwaway runs.
            raise AuditInputError(
                f"refusing to auto-audit '{resolved}'",
                _PIPELINE_RESULTS_REFUSAL_HINT,
            )
        return resolved

    raise FileNotFoundError("No results directory specified. Pass --results-dir explicitly.")


def _lookup_canonical_name_by_key(key: str, search_dir: Path = None) -> str:
    """Resolves human-readable strategy name from strategies_config.json or strategy_dumps."""
    clean_key = key.replace("_strategy", "").strip()
    cur = (search_dir or Path.cwd()).resolve()
    for p in [cur, cur.parent, cur.parent.parent, Path.cwd(), Path.cwd().parent]:
        for candidate_path in [
            p / "pipeline/research_strategy/strategies_config.json",
            p / "research_strategy/strategies_config.json",
            p / "strategies_config.json",
        ]:
            if candidate_path.is_file():
                try:
                    with open(candidate_path) as f:
                        cfg = json.load(f)
                    if clean_key in cfg and "name" in cfg[clean_key]:
                        return str(cfg[clean_key]["name"])
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    # Name cosmetics only, but a corrupt config must still be visible:
                    # the old bare `except Exception: pass` hid it completely.
                    _warn(f"could not read strategy name from {candidate_path}: {exc}")

        for dump_dir in [
            p / "pipeline/research_strategy/results/strategy_dumps",
            p / "research_strategy/results/strategy_dumps",
        ]:
            dump_file = dump_dir / f"{clean_key}_strategy.json"
            if dump_file.is_file():
                try:
                    with open(dump_file) as f:
                        sdata = json.load(f)
                    name = (
                        sdata.get("strategy_name")
                        or sdata.get("name")
                        or sdata.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
                    )
                    if name:
                        return str(name)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    _warn(f"could not read strategy name from {dump_file}: {exc}")
    return None


def _detect_strategy_name(results_dir: Path, strategy_name: str = None) -> str:
    if strategy_name:
        return strategy_name

    # 1. Check if walkforward_summary.json or comparison_report.json directly specifies the strategy name
    for sum_name in ["walkforward_summary.json", "comparison_report.json"]:
        sum_file = results_dir / sum_name
        if sum_file.is_file():
            try:
                with open(sum_file) as f:
                    data = json.load(f)
                name = data.get("strategy_name") or data.get("strategy")
                if name:
                    return str(name)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                _warn(f"could not read strategy name from {sum_file}: {exc}")

    # 2. Check if strategy.json exists in results_dir or its parent
    for candidate in [results_dir / "strategy.json", results_dir.parent / "strategy.json"]:
        if candidate.is_file():
            try:
                with open(candidate) as f:
                    sdata = json.load(f)
                name = (
                    sdata.get("strategy_name")
                    or sdata.get("name")
                    or sdata.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
                )
                if name:
                    return str(name)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                _warn(f"could not read strategy name from {candidate}: {exc}")

    # 3. Look up key in strategies_config.json or strategy_dumps
    canonical = _lookup_canonical_name_by_key(results_dir.name, results_dir)
    if canonical:
        return canonical

    # 4. Humanize directory name as last resort (never raw snake_case or filename)
    clean_name = results_dir.name.replace("_strategy", "").replace("_", " ").title()
    if clean_name and clean_name.lower() not in ("results", "output", "backtest"):
        return clean_name
    return "Walkforward Strategy"


def load_all_summaries(results_dir: Path, strategy_name: str = None):
    strategies = []
    direct_summary = results_dir / "walkforward_summary.json"
    if direct_summary.is_file():
        try:
            with open(direct_summary) as f:
                data = json.load(f)
            strat = (
                data.get("strategy_name")
                or data.get("strategy")
                or _detect_strategy_name(results_dir, strategy_name)
            )
            data["dir_name"] = ""
            data["strategy"] = strat
            strategies.append(data)
            return strategies
        except Exception as e:
            print(f"Warning: Failed to parse {direct_summary}: {e}", file=sys.stderr)

    for entry in sorted(os.listdir(results_dir)):
        sub = results_dir / entry
        if not sub.is_dir():
            continue
        summary_path = sub / "walkforward_summary.json"
        if not summary_path.is_file():
            continue
        try:
            with open(summary_path) as f:
                data = json.load(f)
            strat = (
                data.get("strategy_name")
                or data.get("strategy")
                or _detect_strategy_name(sub, strategy_name)
            )
            data["dir_name"] = entry
            data["strategy"] = strat
            strategies.append(data)
        except Exception as e:
            print(f"Warning: Failed to parse {summary_path}: {e}", file=sys.stderr)

    if not strategies:
        for entry in sorted(os.listdir(results_dir)):
            sub = results_dir / entry
            if not sub.is_dir():
                continue
            for sub_entry in sorted(os.listdir(sub)):
                sub_sub = sub / sub_entry
                if not sub_sub.is_dir():
                    continue
                summary_path = sub_sub / "walkforward_summary.json"
                if not summary_path.is_file():
                    continue
                try:
                    with open(summary_path) as f:
                        data = json.load(f)
                    strat = (
                        data.get("strategy_name")
                        or data.get("strategy")
                        or _detect_strategy_name(sub_sub, strategy_name)
                    )
                    data["dir_name"] = f"{entry}/{sub_entry}"
                    data["strategy"] = f"{strat} [Batch {entry}]"
                    strategies.append(data)
                except Exception as e:
                    print(f"Warning: Failed to parse {summary_path}: {e}", file=sys.stderr)

    return strategies


def run_summary(strategies):
    print("=" * 135)
    print(f"{'Strategy':<38} {'Sharpe':>8} {'CAGR%':>8} {'MaxDD%':>8} {'Calmar':>8} {'DSR':>10} {'Folds':>6} {'Period':>25}")
    print("=" * 135)

    strategies_sorted = sorted(strategies, key=lambda s: s["mean_sharpe_ratio"], reverse=True)

    for s in strategies_sorted:
        dsr = s.get("deflated_sharpe_ratio")
        dsr_str = f"{dsr:.4f}" if dsr is not None and abs(dsr) > 1e-10 else "~0"
        period = f"{s.get('actual_first_fold_start', '?')} → {s.get('actual_last_fold_end', '?')}"
        print(f"{s['strategy']:<38} {s['mean_sharpe_ratio']:>8.3f} {s['mean_cagr']*100:>8.2f} "
              f"{s['mean_max_drawdown']*100:>8.1f} {s['mean_calmar_ratio']:>8.3f} "
              f"{dsr_str:>10} {s['n_folds']:>6} {period:>25}")

    print("=" * 135)
    print(f"Total strategies: {len(strategies)} | "
          f"Positive Sharpe: {sum(1 for s in strategies if s['mean_sharpe_ratio'] > 0)} | "
          f"Profitable (CAGR > 0): {sum(1 for s in strategies if s['mean_cagr'] > 0)} | "
          f"DSR > 0.05: {sum(1 for s in strategies if (s.get('deflated_sharpe_ratio') or 0) > 0.05)}")
    return strategies_sorted


def turnover_annualised_from_folds(folds) -> tuple:
    """Annualised TWO-SIDED turnover from `rolling_window_performance`, per fold then mean.

    Returns `(ann_calendar, mean_raw_per_fold, measurable)`:

    * `ann_calendar` -- mean over usable folds of `total_turnover / (days / 365.25)`;
    * `mean_raw_per_fold` -- mean of the raw per-fold `total_turnover` values (undivided), so
      the fold-length artefact below stays visible in the report;
    * `measurable` -- `"n/N"`, usable folds over total folds.

    Conventions, all load-bearing:

    * `total_turnover` is TWO-SIDED (`Σ|trade_value| / equity`, both legs), never one-way.
      See `TURNOVER_CEILING_ANN` for the cross-check against the independently measured
      friction in `FORENSIC_AUDIT.md`.
    * Annualisation is on a CALENDAR 365.25 d/y basis from each fold's own dates.
    * Per-fold annualise THEN mean -- NOT `Σturnover / Σyears`. The latter mixes fold length
      into the metric: on `01_r1_longwin/chan_risk_managed_blend` it reports 36.70x/yr, while
      the correct per-fold figure is 12.67x/yr. The raw per-fold totals of the long-window
      batch (19-29, 756-day folds) and of `03_r2b_oos` (2.5-5.9, 97-112-day folds) differ by
      ~8x, which is exactly the fold-length ratio -- raw per-fold totals are not comparable
      across fold lengths, and neither is a sum-over-span.

    A fold is UNUSABLE (counted in `N`, excluded from both means) when `total_turnover` is
    missing / non-numeric / non-finite, when the dates cannot be parsed, or when
    `(end - start).days <= 0` (a degenerate zero-length fold has no annualisation).
    All folds unusable => `(None, None, "0/N")`; the caller must treat that as "unknown", not
    as `0.0`.
    """
    fold_list = folds if isinstance(folds, list) else []
    total = len(fold_list)
    raw_vals = []
    ann_vals = []
    for fold in fold_list:
        if not isinstance(fold, dict):
            continue
        try:
            raw = float(fold.get("total_turnover"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(raw):
            continue
        try:
            start = datetime.strptime(str(fold.get("start_date", "")), "%Y-%m-%d")
            end = datetime.strptime(str(fold.get("end_date", "")), "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        days = (end - start).days
        if days <= 0:
            continue
        raw_vals.append(raw)
        ann_vals.append(raw / (days / 365.25))

    if not ann_vals:
        return None, None, f"0/{total}"
    return statistics.mean(ann_vals), statistics.mean(raw_vals), f"{len(ann_vals)}/{total}"


def _format_turnover_ann(value) -> str:
    """Render an annualised turnover; `—` (never `0.00`) when it is unmeasurable."""
    return "—" if value is None else f"{value:.2f}"


def audit_strategy_trades(results_dir: Path, dir_name: str, summary_data: dict):
    path = (results_dir / dir_name / "walkforward_rebalances.csv") if dir_name else (results_dir / "walkforward_rebalances.csv")
    if not path.is_file():
        return None

    label = dir_name or path.parent.name

    trades = []
    parse_failures = 0
    first_parse_error = None
    with open(path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        # Absent columns used to be read as 0.0 by `row.get(k, 0.0)` below, i.e. a silently
        # wrong metric rather than an error. Report it explicitly.
        missing_cols = [c for c in _REBALANCE_CSV_COLUMNS if c not in fields]
        if missing_cols:
            _flag_degraded(
                label,
                f"{path.name} is missing column(s) {missing_cols}; every missing numeric "
                "field is read as 0.0, so the derived anomaly metrics are disqualified.",
            )
        for row in reader:
            try:
                row["fold"] = int(row["fold"])
                row["rebalance_id"] = int(row["rebalance_id"])
                for k in ["price", "prior_weight", "target_weight", "weight_change",
                           "trade_value", "shares", "prior_shares", "target_shares",
                           "commission", "slippage", "total_cost", "portfolio_equity"]:
                    row[k] = float(row.get(k, 0.0) or 0.0)
                trades.append(row)
            except Exception as exc:
                # Still skip the row (one malformed line must not abort the whole audit),
                # but never silently: the old bare `continue` could drop every row and turn
                # the whole strategy into an apparently clean profile.
                parse_failures += 1
                if first_parse_error is None:
                    first_parse_error = f"{type(exc).__name__}: {exc}"
                continue

    if parse_failures:
        _flag_degraded(
            label,
            f"{parse_failures}/{parse_failures + len(trades)} row(s) of {path.name} failed to "
            f"parse and were dropped (first error: {first_parse_error}); "
            + ("ALL rows were dropped, so no trade-level anomaly metric exists at all."
               if not trades else "the reported metrics exclude those rows."),
            disqualifying=not trades,
        )

    if not trades:
        return None

    folds = sorted(set(t["fold"] for t in trades))
    fold_perf = summary_data.get("rolling_window_performance", [])

    # Anomaly checks:
    # 1. Concentration >= 80%
    concentrated_trades = [t for t in trades if abs(t["target_weight"]) >= 0.80]
    all_in_trades = [t for t in trades if abs(t["target_weight"]) >= 0.99]

    # 2. Stateful tracking of actual held portfolio weights per fold
    # NOTE: walkforward_rebalances.csv only records assets adjusted during a rebalance event
    # (|weight_change| > 1e-7). In asynchronous or asset-level inertia strategies, untouched
    # assets from prior rebalances do not emit rows. Naively summing target_weight in trade rows
    # severely distorts the average invested weight (e.g. reporting 0.12 instead of 0.65).
    # We maintain a stateful tracker of active positions across rebalance events per fold.
    rebal_groups = defaultdict(dict)
    held_weight_sums = []
    underinvested_rebals = 0

    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        event_dict = defaultdict(list)
        events = []
        for t in ft:
            rebal_key = (t["fold"], t["rebalance_id"], t["date"])
            rebal_groups[rebal_key][t["symbol"]] = t["target_weight"]

            event_key = (t["rebalance_id"], t["date"])
            if event_key not in event_dict:
                events.append(event_key)
            event_dict[event_key].append(t)

        held_pos = {}
        for ev in events:
            for t in event_dict[ev]:
                sym = t["symbol"]
                tw = t["target_weight"]
                if tw > 1e-7:
                    held_pos[sym] = tw
                else:
                    held_pos.pop(sym, None)
            total_invested = sum(held_pos.values())
            held_weight_sums.append(total_invested)
            if total_invested < 0.80:
                underinvested_rebals += 1

    avg_weight_sum = statistics.mean(held_weight_sums) if held_weight_sums else 1.0
    if not held_weight_sums:
        # 1.0 = "fully invested". Fabricating that here used to hide a CSV with no usable
        # rebalance events entirely (and silently zeroed the under-investment penalty).
        _flag_degraded(
            label,
            "no usable rebalance events in walkforward_rebalances.csv, so the average "
            "invested weight is unknown; 1.0 (fully invested) is substituted and the "
            "under-investment / cash-drag metrics below are disqualified.",
        )

    # 3. Warmup delay in Fold 1
    fold1_trades = [t for t in trades if t["fold"] == 1]
    warmup_days = 0
    if fold1_trades and fold_perf:
        try:
            d_start = datetime.strptime(fold_perf[0].get("start_date", ""), "%Y-%m-%d")
            d_trade = datetime.strptime(min(t["date"] for t in fold1_trades), "%Y-%m-%d")
            warmup_days = (d_trade - d_start).days
        except (KeyError, TypeError, ValueError) as exc:
            # 0 days reads as "immediate trade execution" -- say out loud that it is unknown.
            _warn(
                f"[{label}] Fold-1 warmup delay could not be computed ({type(exc).__name__}: "
                f"{exc}); reporting 0 days (= 'no warmup padding'), which is unverified."
            )
            warmup_days = 0
    elif fold1_trades and not fold_perf:
        _warn(
            f"[{label}] no rolling_window_performance in walkforward_summary.json; Fold-1 "
            "warmup delay is unmeasurable and reported as 0 days (unverified)."
        )

    # 4. Single-rebalance equity jumps
    fold_equities = defaultdict(dict)
    for t in trades:
        fold_equities[t["fold"]][(t["rebalance_id"], t["date"])] = t["portfolio_equity"]
    max_jump = 0.0
    for fold_num, rebal_eq in fold_equities.items():
        equities = [v for k, v in sorted(rebal_eq.items())]
        for i in range(1, len(equities)):
            if equities[i-1] > 0:
                jump = (equities[i] - equities[i-1]) / equities[i-1]
                max_jump = max(max_jump, jump)

    # 5. Turnover ratio per fold
    fold_turnovers = []
    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        tot_val = sum(abs(t["trade_value"]) for t in ft)
        avg_eq = statistics.mean(t["portfolio_equity"] for t in ft) if ft else 1.0
        fold_turnovers.append(tot_val / avg_eq if avg_eq > 0 else 0.0)
    avg_turnover = statistics.mean(fold_turnovers) if fold_turnovers else 0.0

    # 5b. Two-sided calendar-annualised turnover, straight from the summary's own per-fold
    # `total_turnover`. Deliberately a SECOND metric next to `avg_turnover` above: that one is
    # a mean of per-fold `Σ|trade_value| / mean-equity` in fold units, so it is not comparable
    # across fold lengths and is no basis for an annual ceiling. See
    # `turnover_annualised_from_folds` and `TURNOVER_CEILING_ANN`.
    turnover_ann, turnover_raw, turnover_folds = turnover_annualised_from_folds(fold_perf)

    # 6. Directional hit rate (look-ahead check)
    sym_trades = defaultdict(list)
    for t in trades:
        sym_trades[t["symbol"]].append(t)
    buys_up = 0
    tot_buys = 0
    for sym, slist in sym_trades.items():
        for i in range(len(slist) - 1):
            if slist[i]["action"] == "BUY" and slist[i]["price"] > 0:
                tot_buys += 1
                if slist[i+1]["price"] > slist[i]["price"]:
                    buys_up += 1
    buy_hit_rate = buys_up / tot_buys if tot_buys > 0 else 0.50
    if tot_buys == 0:
        # 0.50 is the "coin flip" prior, which grades CLEAN on the look-ahead check while
        # every symbol has a single trade (no follow-up price to compare against).
        _warn(
            f"[{label}] no BUY has a follow-up trade of the same symbol, so the directional "
            "hit rate is unmeasurable; 0.50 (coin-flip prior) is reported and the look-ahead "
            "check is unverified."
        )

    return {
        "dir_name": dir_name,
        "strategy": (
            summary_data.get("strategy")
            or summary_data.get("strategy_name")
            or _detect_strategy_name(results_dir / dir_name if dir_name else results_dir)
        ),
        "total_trades": len(trades),
        "total_rebals": len(rebal_groups),
        "unique_symbols": len(set(t["symbol"] for t in trades)),
        "concentrated_trades": len(concentrated_trades),
        "all_in_trades": len(all_in_trades),
        "avg_weight_sum": avg_weight_sum,
        "underinvested_rebals": underinvested_rebals,
        "warmup_days_fold1": warmup_days,
        "max_equity_jump": max_jump,
        "avg_turnover": avg_turnover,
        "turnover_ann": turnover_ann,
        "turnover_raw": turnover_raw,
        "turnover_folds": turnover_folds,
        "buy_hit_rate": buy_hit_rate,
    }


def run_ranking(strategies, results_dir: Path, top_n: int = 10):
    sorted_strats = sorted(strategies, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:top_n]
    results = []

    for s in sorted_strats:
        audit = audit_strategy_trades(results_dir, s["dir_name"], s)
        raw_sharpe = s["mean_sharpe_ratio"]
        dsr = s.get("deflated_sharpe_ratio") or 0.0
        folds = s.get("rolling_window_performance", [])
        sharpe_std = s.get("fold_sharpe_std", 1.0)
        winning_folds = sum(1 for f in folds if (f.get("sharpe_ratio") or 0) > 0)
        # Two-sided calendar-annualised turnover; available even with no rebalances.csv because
        # it comes from the summary's own per-fold `total_turnover`.
        t_ann, t_raw, t_folds = turnover_annualised_from_folds(folds)

        if not audit:
            results.append({
                "strategy": s["strategy"],
                "raw_sharpe": raw_sharpe,
                "adj_sharpe": raw_sharpe,
                "cagr": s["mean_cagr"],
                "maxdd": s["mean_max_drawdown"],
                "dsr": dsr,
                "win": f"{winning_folds}/{len(folds)}",
                "turnover_ann": t_ann,
                "turnover_raw": t_raw,
                "turnover_folds": t_folds,
                "notes": "No rebalances.csv found",
            })
            continue

        # Penalties:
        # 1. Concentration penalty: 0.05 per all-in trade, capped at 0.40
        p_conc = min(audit["all_in_trades"] * 0.05, 0.40)

        # 2. Under-investment penalty
        p_under = max(0.0, (1.0 - audit["avg_weight_sum"]) * 0.50)

        # 3. Warmup bias penalty (idle > 60 days in fold 1)
        p_warm = 0.05 if audit["warmup_days_fold1"] > 60 else 0.0

        # 4. Outlier equity jump penalty (>30% jump)
        p_jump = max(0.0, (audit["max_equity_jump"] - 0.30) * 0.30) if audit["max_equity_jump"] > 0.30 else 0.0

        # 5. Turnover cost penalty (20bps extra per turnover unit annualized)
        p_tover = min(audit["avg_turnover"] * 0.002 * 2 * 0.50, 0.30)

        # 6. Tradability friction (near-daily rebalancing >50 dates per fold)
        avg_dates = audit["total_rebals"] / max(len(folds), 1)
        p_trade = 0.10 if avg_dates > 50 else (0.05 if avg_dates > 30 else 0.0)

        # Bonuses:
        b_dsr = 0.15 if dsr > 0.95 else (0.10 if dsr > 0.50 else (0.05 if dsr > 0.05 else 0.0))
        b_cons = (0.10 if winning_folds == len(folds) else (0.05 if winning_folds >= len(folds) - 1 else 0.0))
        if sharpe_std < 1.0:
            b_cons += 0.05

        adj_sharpe = raw_sharpe - (p_conc + p_under + p_warm + p_jump + p_tover + p_trade) + (b_dsr + b_cons)

        results.append({
            "strategy": s["strategy"],
            "dir_name": s.get("dir_name", ""),
            "summary": s,
            "raw_sharpe": raw_sharpe,
            "adj_sharpe": adj_sharpe,
            "cagr": s["mean_cagr"],
            "maxdd": s["mean_max_drawdown"],
            "dsr": dsr,
            "win": f"{winning_folds}/{len(folds)}",
            "p_conc": p_conc,
            "p_under": p_under,
            "p_warm": p_warm,
            "p_jump": p_jump,
            "p_tover": p_tover,
            "p_trade": p_trade,
            "b_dsr": b_dsr,
            "b_cons": b_cons,
            "all_in": audit["all_in_trades"],
            "avg_wsum": audit["avg_weight_sum"],
            "max_jump": audit["max_equity_jump"],
            "turnover": audit["avg_turnover"],
            "turnover_ann": t_ann,
            "turnover_raw": t_raw,
            "turnover_folds": t_folds,
        })

    results.sort(key=lambda r: r["adj_sharpe"], reverse=True)

    # The 12 pre-existing columns are untouched (same order, same names, same widths); the
    # three turnover columns are APPENDED after `Jump%` (last of the pre-existing block) so
    # the table stays aligned:
    # 4+35+7+7+7+7+7+5+6+5+6+6 (+11 spaces) = 113, plus 'Turnover/yr'(11) 'Turnover raw'(12)
    # 'Folds'(5) and 3 spaces = 144.
    header = (f"{'Rank':>4} {'Strategy':<35} {'Raw':>7} {'Adj':>7} {'CAGR%':>7} {'MaxDD%':>7} {'DSR':>7} "
              f"{'Win':>5} {'AllIn':>6} {'WSum':>5} {'Tover':>6} {'Jump%':>6} "
              f"{'Turnover/yr':>11} {'Turnover raw':>12} {'Folds':>5}")

    table_rows = []
    for i, r in enumerate(results, 1):
        dsr_str = f"{r['dsr']:.3f}" if r.get("dsr", 0) > 1e-5 else "~0"
        allin = r.get("all_in", "-")
        wsum = f"{r.get('avg_wsum', 1.0):.2f}"
        tover = f"{r.get('turnover', 0.0):.1f}"
        jump = f"{r.get('max_jump', 0.0)*100:.0f}%"
        t_ann = _format_turnover_ann(r.get("turnover_ann"))
        raw_val = r.get("turnover_raw")
        t_raw = "—" if raw_val is None else f"{raw_val:.3f}"
        t_folds = r.get("turnover_folds", "0/0")
        table_rows.append(
            f"{i:>4} {r['strategy']:<35} {r['raw_sharpe']:>7.3f} {r['adj_sharpe']:>7.3f} "
            f"{r['cagr']*100:>7.2f} {r['maxdd']*100:>7.1f} {dsr_str:>7} "
            f"{r['win']:>5} {allin:>6} {wsum:>5} {tover:>6} {jump:>6} "
            f"{t_ann:>11} {t_raw:>12} {t_folds:>5}"
        )

    # A strategy name longer than the 35-char column overflows it (pre-existing behaviour --
    # it must not be truncated, the name is the identity). Size the frame to the widest line
    # so the box still encloses every row instead of being overrun by the new columns.
    table_width = max([len(header), len("ANOMALY-ADJUSTED STRATEGY RANKINGS (for live trading readiness)")]
                      + [len(ln) for ln in table_rows])

    print("=" * table_width)
    print("ANOMALY-ADJUSTED STRATEGY RANKINGS (for live trading readiness)")
    print("=" * table_width)
    print(header)
    print("-" * table_width)
    for ln in table_rows:
        print(ln)

    print("=" * table_width)
    # One line, right under the table's own legend -- never a new section.
    print(TURNOVER_CONVENTION_NOTE)
    return results


def audit_strategy_assets(results_dir: Path, dir_name: str = ""):
    """Performs stateful asset-level trade analysis and PnL attribution from walkforward_rebalances.csv.

    Hard rules (added 2026-10-25 after the silent-degradation incident):
    * a missing third-party dependency is FATAL (`sys.exit(2)`), never a `universe = {}`;
    * an empty price universe while fold-end positions are open makes every net-PnL /
      ROI / winner-loser number DISQUALIFIED and says so on both streams;
    * any fold-end position valued at its last trade price instead of the final close is
      counted and reported (that fallback was previously invisible).
    """
    strat_dir = (results_dir / dir_name) if dir_name else Path(results_dir)
    path = strat_dir / "walkforward_rebalances.csv"
    if not path.is_file():
        return None

    label = dir_name or strat_dir.name

    # Read fold end dates from walkforward_report.csv or walkforward_summary.json
    fold_ends = {}
    fold_ends_errors = []
    report_csv = strat_dir / "walkforward_report.csv"
    summary_json = strat_dir / "walkforward_summary.json"
    if report_csv.is_file():
        try:
            with open(report_csv) as f:
                r = csv.DictReader(f)
                for idx, row in enumerate(r, 1):
                    if "end_date" in row and row["end_date"]:
                        fold_ends[idx] = row["end_date"].strip()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Was `except Exception: pass` -- a corrupt report silently made every fold-end
            # valuation fall back to last-trade-price.
            fold_ends_errors.append(f"walkforward_report.csv ({type(exc).__name__}: {exc})")
    if not fold_ends and summary_json.is_file():
        try:
            with open(summary_json) as f:
                sdata = json.load(f)
            for idx, rwp in enumerate(sdata.get("rolling_window_performance", []), 1):
                if "end_date" in rwp and rwp["end_date"]:
                    fold_ends[idx] = rwp["end_date"].strip()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            fold_ends_errors.append(f"walkforward_summary.json ({type(exc).__name__}: {exc})")
    if not fold_ends and fold_ends_errors:
        _warn(
            f"[{label}] could not read fold end dates ({'; '.join(fold_ends_errors)}); "
            "fold-end positions will be valued at their last trade price, not the final close."
        )

    trades = []
    parse_failures = 0
    first_parse_error = None
    with open(path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        missing_cols = [c for c in _REBALANCE_CSV_COLUMNS if c not in fields]
        if missing_cols:
            _flag_degraded(
                label,
                f"{path.name} is missing column(s) {missing_cols}; every missing numeric "
                "field is read as 0.0, so the PnL attribution below is disqualified.",
            )
        for row in reader:
            try:
                row["fold"] = int(row["fold"])
                row["rebalance_id"] = int(row["rebalance_id"])
                for k in ["price", "prior_weight", "target_weight", "weight_change",
                           "trade_value", "shares", "prior_shares", "target_shares",
                           "commission", "slippage", "total_cost", "portfolio_equity"]:
                    row[k] = float(row.get(k, 0.0) or 0.0)
                trades.append(row)
            except Exception as exc:
                parse_failures += 1
                if first_parse_error is None:
                    first_parse_error = f"{type(exc).__name__}: {exc}"
                continue

    if parse_failures:
        _flag_degraded(
            label,
            f"{parse_failures}/{parse_failures + len(trades)} row(s) of {path.name} failed to "
            f"parse and were dropped (first error: {first_parse_error}); "
            + ("ALL rows were dropped, so no PnL attribution exists at all."
               if not trades else "the PnL attribution below excludes those rows."),
            disqualifying=not trades,
        )

    if not trades:
        return None

    # Load universe close prices for mark-to-market valuation at fold end
    universe = {}
    universe_error = None
    symbols = sorted(list(set(t["symbol"] for t in trades)))
    if symbols:
        try:
            cur_p = Path(__file__).resolve()
            for parent_p in [cur_p.parents[4], Path.cwd(), Path.cwd().parent]:
                if (parent_p / "common").is_dir() and str(parent_p) not in sys.path:
                    sys.path.insert(0, str(parent_p))
            from common.cli_utils import load_universe_with_banner
        except ImportError as exc:
            # FATAL, never swallowed. This is the exact import that failed on a pandas-less
            # interpreter and used to degrade every fold-end valuation to last-trade-price;
            # `_require_third_party_deps()` should already have caught it before main() ran.
            sys.stdout.flush()
            print(
                "Error: this script requires "
                + "/".join(_REQUIRED_THIRD_PARTY_DEPS)
                + " to value fold-end positions at the final close, but importing the "
                f"universe loader failed ({type(exc).__name__}: {exc}); current interpreter: "
                f"{sys.executable} (python {sys.version_info.major}."
                f"{sys.version_info.minor}.{sys.version_info.micro}). "
                f"Re-run with {_venv_python_hint()}.",
                file=sys.stderr,
            )
            sys.exit(2)
        except Exception as exc:
            universe_error = f"{type(exc).__name__}: {exc}"
        else:
            try:
                start_date = min(t["date"] for t in trades)
                end_date_all = max(fold_ends.values()) if fold_ends else max(t["date"] for t in trades)
                universe = load_universe_with_banner(
                    symbols, start_date, end_date_all, '1d',
                    data_kwargs={'provider': 'fuyao'},
                    require_nonempty=False,
                    loading_msg=""
                )
            except Exception as exc:
                universe = {}
                universe_error = f"price load failed ({type(exc).__name__}: {exc})"

    folds = sorted(set(t["fold"] for t in trades))
    symbol_stats = defaultdict(lambda: {
        "buys": 0.0, "sells": 0.0, "costs": 0.0, "end_val": 0.0,
        "trades": 0, "buy_count": 0, "sell_count": 0, "folds_present": set(),
    })

    # Fold-end valuation provenance counters. `_fallback_reasons` records WHY a position was
    # priced at its last trade price, so the warning can name the actual cause.
    positions_at_fold_end = 0
    close_marks = 0
    price_fallbacks = 0
    fallback_reasons = []
    first_mark_error = None

    def _note_fallback(reason):
        if reason not in fallback_reasons:
            fallback_reasons.append(reason)

    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        events = sorted(set((t["rebalance_id"], t["date"]) for t in ft))
        event_dict = defaultdict(list)
        for t in ft:
            event_dict[(t["rebalance_id"], t["date"])].append(t)

        held = {}
        for ev in events:
            for t in event_dict[ev]:
                sym = t["symbol"]
                val = t["trade_value"]
                cost = t["total_cost"]
                act = t["action"]
                st = symbol_stats[sym]
                st["costs"] += cost
                st["trades"] += 1
                st["folds_present"].add(f_num)
                if act == "BUY":
                    st["buys"] += val
                    st["buy_count"] += 1
                elif act == "SELL":
                    st["sells"] += val
                    st["sell_count"] += 1

                if t["target_shares"] > 1e-4:
                    held[sym] = (t["target_shares"], t["price"])
                else:
                    held.pop(sym, None)

        end_date = fold_ends.get(f_num)
        for sym, (sh, pr) in held.items():
            positions_at_fold_end += 1
            marked_at_close = False
            if fold_ends and (sym in universe) and end_date:
                try:
                    close_p = float(universe[sym].loc[:end_date, 'Close'].iloc[-1])
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    # Was a bare `except Exception:` that silently valued the position at
                    # its last trade price. Keep the fallback (one bad bar must not abort
                    # the audit) but count it and remember why.
                    if first_mark_error is None:
                        first_mark_error = f"{type(exc).__name__}: {exc}"
                    _note_fallback(f"fold {f_num} {sym}: close lookup failed")
                else:
                    symbol_stats[sym]["end_val"] += sh * close_p
                    marked_at_close = True
            if marked_at_close:
                close_marks += 1
                continue
            if not universe:
                _note_fallback("price universe is empty")
            elif not fold_ends:
                _note_fallback("fold end dates unavailable")
            elif sym not in universe:
                _note_fallback(f"{sym} absent from the loaded price universe")
            elif end_date is None:
                _note_fallback(f"fold {f_num} has no end_date")
            symbol_stats[sym]["end_val"] += sh * pr
            price_fallbacks += 1

    # --- Valuation provenance gate (the 2026-10-25 incident) ---------------------------
    # Silent `universe = {}` used to make EVERY fold-end position fall back to its last
    # trade price, which looked like a normal report with slightly smaller PnL. These
    # checks make that state impossible to miss.
    if positions_at_fold_end == 0:
        _flag_degraded(
            label,
            "parsed trades but no fold ends with an open position (target_shares <= 0 "
            "everywhere), so end-of-period valuation is 0.0 and net PnL reduces to "
            "realized sells - buys - costs; if the strategy was actually holding assets "
            "this is a broken CSV, not a flat book.",
            disqualifying=False,
        )
    if positions_at_fold_end and not universe:
        _flag_degraded(
            label,
            f"loaded price universe is EMPTY while {positions_at_fold_end} fold-end "
            f"position(s) over {len(symbols)} symbol(s) remain open"
            + (f" [{universe_error}]" if universe_error else " (loader returned no data)")
            + "; every one of them was valued at its LAST TRADE PRICE instead of the final "
              "close, so the net PnL / ROI / win-loss / fragility numbers below are "
              "DISQUALIFIED.",
        )
    elif universe_error:
        _flag_degraded(
            label,
            f"price universe load failed [{universe_error}]; fold-end positions that could "
            "not be marked to close were valued at their last trade price, so the PnL "
            "attribution below is DISQUALIFIED.",
        )
    elif price_fallbacks:
        _flag_degraded(
            label,
            f"{price_fallbacks}/{positions_at_fold_end} fold-end position valuation(s) fell "
            f"back to the LAST TRADE PRICE instead of the final close ({'; '.join(fallback_reasons)}"
            + (f"; first close-lookup error: {first_mark_error}" if first_mark_error else "")
            + f"); only {close_marks} were marked to close, so those marks are approximate and "
              "the affected PnL numbers are disqualified.",
        )

    if universe:
        missing_price_syms = [s for s in symbols if s not in universe]
        if missing_price_syms:
            _warn(
                f"[{label}] {len(missing_price_syms)}/{len(symbols)} traded symbol(s) have no "
                f"price data ({', '.join(missing_price_syms[:10])}"
                + (", ..." if len(missing_price_syms) > 10 else "")
                + "); their fold-end positions are valued at the last trade price."
            )

    assets = []
    no_buy_symbols = 0
    for sym, st in symbol_stats.items():
        net_pnl = st["sells"] + st["end_val"] - st["buys"] - st["costs"]
        roi = (net_pnl / st["buys"] * 100.0) if st["buys"] > 0 else 0.0
        if st["buys"] <= 0:
            # Genuinely undefined (zero denominator), kept as 0.0 for layout stability, but
            # a traded symbol with no recorded BUY means the CSV is missing rows.
            no_buy_symbols += 1
        assets.append({
            "symbol": sym,
            "net_pnl": net_pnl,
            "buys": st["buys"],
            "sells": st["sells"],
            "costs": st["costs"],
            "end_val": st["end_val"],
            "trades": st["trades"],
            "buy_count": st["buy_count"],
            "sell_count": st["sell_count"],
            "folds": len(st["folds_present"]),
            "roi": roi,
        })

    if no_buy_symbols:
        _warn(
            f"[{label}] {no_buy_symbols}/{len(symbol_stats)} traded symbol(s) record no BUY "
            "value at all, so their ROI is not computable and is reported as 0.0%."
        )

    assets.sort(key=lambda a: a["net_pnl"], reverse=True)
    winners = [a for a in assets if a["net_pnl"] > 0]
    losers = [a for a in assets if a["net_pnl"] < 0]

    return {
        "trades": trades,
        "assets": assets,
        "winners": winners,
        "losers": losers,
        "total_cost": sum(a["costs"] for a in assets),
        "total_bought": sum(a["buys"] for a in assets),
        "total_sold": sum(a["sells"] for a in assets),
        "total_net_pnl": sum(a["net_pnl"] for a in assets),
    }


def deep_analyze_top_strategies(
    ranked_results: list,
    results_dir: Path,
    top_n: int = 3,
    universe_file: str = None,
    export_pruned_path: str = None,
):
    """Performs deep behavioral, regime, and asset-attribution audit on top N strategies and recommends losing asset exclusions."""
    if not ranked_results:
        print("No ranked strategies available for deep analysis.", file=sys.stderr)
        return

    top_strats = ranked_results[:top_n]
    print("\n" + "=" * 140)
    print(f"STAGE 4: TOP {len(top_strats)} STRATEGY DEEP BEHAVIORAL & ANOMALY ANALYSIS")
    print("=" * 140)

    agg_symbol_stats = defaultdict(lambda: {
        "net_pnl": 0.0, "buys": 0.0, "sells": 0.0, "costs": 0.0,
        "trades": 0, "win_strats": 0, "loss_strats": 0, "strats": set(),
    })
    all_observed_symbols = set()

    for rank_idx, r in enumerate(top_strats, 1):
        strat_name = r["strategy"]
        dir_name = r.get("dir_name", "")
        summary = r.get("summary", {})
        folds_perf = summary.get("rolling_window_performance", [])

        asset_data = audit_strategy_assets(results_dir, dir_name)
        trade_audit = audit_strategy_trades(results_dir, dir_name, summary)

        # Was silent: with no asset attribution this section still printed
        # "Total Friction Cost: 0.00 RMB | Net PnL: 0.00 RMB" as if measured.
        if not asset_data:
            _warn(
                f"[{strat_name}] no asset attribution available (missing or unparseable "
                "walkforward_rebalances.csv); Friction Cost / Net PnL below print as 0.00 "
                "RMB and are NOT measurements."
            )
        if not folds_perf:
            _warn(
                f"[{strat_name}] walkforward_summary.json carries no rolling_window_performance; "
                "fold dynamics below are omitted and fold-derived metrics are unverified."
            )

        # Behavioral & Fold Metrics
        cagrs = [(f.get("cagr") or 0.0) for f in folds_perf]
        win_folds = sum(1 for c in cagrs if c > 0)

        best_fold_idx = cagrs.index(max(cagrs)) if cagrs else 0
        worst_fold_idx = cagrs.index(min(cagrs)) if cagrs else 0
        pos_cagr_sum = sum(c for c in cagrs if c > 0)
        max_fold_share = (max(cagrs) / pos_cagr_sum) if pos_cagr_sum > 0 else 0.0

        # Capital & Friction
        total_costs = asset_data["total_cost"] if asset_data else 0.0
        total_pnl = asset_data["total_net_pnl"] if asset_data else 0.0
        active_wsum = r.get("avg_wsum", 1.0)
        idle_cash = max(0.0, 1.0 - active_wsum)

        # Anomaly Diagnostics
        anomalies = []
        if r.get("all_in", 0) > 0:
            anomalies.append(f"CONCENTRATION: {r['all_in']} all-in (>=99%) single-stock bets detected")
        if idle_cash > 0.50:
            anomalies.append(f"CASH DRAG: {idle_cash*100:.1f}% average uninvested idle capital cushion")
        if r.get("turnover", 0.0) > 3.0:
            anomalies.append(f"HIGH TURNOVER: {r['turnover']:.1f}x turnover per fold incurs severe execution friction")
        if max_fold_share > 0.40 and len(cagrs) > 2:
            anomalies.append(f"PROFIT CONCENTRATION: Best Fold {best_fold_idx+1} accounts for {max_fold_share*100:.1f}% of positive CAGR")
        if r.get("p_warm", 0.0) > 0:
            anomalies.append("WARMUP DELAY: Fold 1 delayed first trade > 60 days")
        if r.get("max_jump", 0.0) > 0.30:
            anomalies.append(f"OUTLIER EQUITY JUMP: Single rebalance equity jumped {r['max_jump']*100:.1f}%")
        if trade_audit and trade_audit.get("buy_hit_rate", 0.5) > 0.70:
            anomalies.append(f"LOOK-AHEAD RISK: Buy hit rate is {trade_audit['buy_hit_rate']*100:.1f}% (suspiciously high for equities)")

        print(f"\n[{rank_idx}] {strat_name}")
        print("-" * 140)
        dsr_val = r.get("dsr", 0.0)
        dsr_str = f"{dsr_val:.3f}" if dsr_val > 1e-5 else "~0"
        print(f"  Overall: Adj Sharpe: {r.get('adj_sharpe', 0.0):.3f} | Raw Sharpe: {r.get('raw_sharpe', 0.0):.3f} | "
              f"CAGR: {r.get('cagr', 0.0)*100:.2f}% | MaxDD: {r.get('maxdd', 0.0)*100:.1f}% | DSR: {dsr_str}")
        if folds_perf:
            best_f = folds_perf[best_fold_idx]
            worst_f = folds_perf[worst_fold_idx]
            best_cagr = (best_f.get('cagr') or 0.0) * 100
            best_mdd = (best_f.get('max_drawdown') or 0.0) * 100
            worst_cagr = (worst_f.get('cagr') or 0.0) * 100
            worst_mdd = (worst_f.get('max_drawdown') or 0.0) * 100
            print(f"  Fold Dynamics: {win_folds}/{len(folds_perf)} Winning Folds | "
                  f"Best: Fold {best_fold_idx+1} ({best_f.get('start_date')} to {best_f.get('end_date')}, CAGR: {best_cagr:.1f}%, MaxDD: {best_mdd:.1f}%) | "
                  f"Worst: Fold {worst_fold_idx+1} ({worst_f.get('start_date')} to {worst_f.get('end_date')}, CAGR: {worst_cagr:.1f}%, MaxDD: {worst_mdd:.1f}%)")
        print(f"  Capital & Friction: Active Weight Sum: {active_wsum:.2f} (Idle Cash: {idle_cash*100:.0f}%) | "
              f"Turnover: {r.get('turnover', 0.0):.1f}x | Total Friction Cost: {total_costs:,.2f} RMB | Net PnL: {total_pnl:,.2f} RMB")

        if anomalies:
            print("  Identified Anomalies & Friction Risks:")
            for a in anomalies:
                print(f"    - ⚠️  {a}")
        else:
            print("  Identified Anomalies: None (Clean institutional execution profile)")

        if asset_data and asset_data["assets"]:
            top_winners = asset_data["winners"][:3]
            top_losers = asset_data["losers"][-3:]
            w_str = ", ".join(f"{w['symbol']} (+{w['net_pnl']:,.0f} RMB, {w['roi']:+.1f}%)" for w in top_winners) if top_winners else "None"
            l_str = ", ".join(f"{l['symbol']} ({l['net_pnl']:,.0f} RMB, {l['roi']:+.1f}%)" for l in top_losers) if top_losers else "None"
            print(f"  Top Alpha Drivers : {w_str}")
            print(f"  Severe Loss Drags : {l_str}")

            for a in asset_data["assets"]:
                sym = a["symbol"]
                all_observed_symbols.add(sym)
                st = agg_symbol_stats[sym]
                st["net_pnl"] += a["net_pnl"]
                st["buys"] += a["buys"]
                st["sells"] += a["sells"]
                st["costs"] += a["costs"]
                st["trades"] += a["trades"]
                st["strats"].add(strat_name)
                if a["net_pnl"] < -1e-4:
                    st["loss_strats"] += 1
                elif a["net_pnl"] > 1e-4:
                    st["win_strats"] += 1

    # Stage 5: Consolidated Losing Assets & Exclusion Recommendations
    print("\n" + "=" * 140)
    print("STAGE 5: ASSET-LEVEL LOSS DRAG ATTRIBUTION & UNIVERSE EXCLUSION RECOMMENDATIONS")
    print("=" * 140)

    consolidated = []
    for sym, st in agg_symbol_stats.items():
        net = st["net_pnl"]
        roi = (net / st["buys"] * 100.0) if st["buys"] > 0 else 0.0
        consolidated.append({
            "symbol": sym,
            "net_pnl": net,
            "buys": st["buys"],
            "sells": st["sells"],
            "costs": st["costs"],
            "trades": st["trades"],
            "roi": roi,
            "loss_strats": st["loss_strats"],
            "win_strats": st["win_strats"],
            "n_strats": len(st["strats"]),
        })

    consolidated.sort(key=lambda x: x["net_pnl"])
    recommended_losers = [c for c in consolidated if c["net_pnl"] < -1e-4]

    if recommended_losers:
        print(f"{'Symbol':<12} {'Net PnL (RMB)':>15} {'ROI%':>8} {'Trades':>8} {'Costs (RMB)':>14} {'Loss/Traded Strats':>20} {'Exclusion Diagnosis':<45}")
        print("-" * 140)
        for l in recommended_losers:
            diag = []
            if l["net_pnl"] < -2000:
                diag.append("Heavy capital loss")
            if l["roi"] < -3.0:
                diag.append("Negative ROI drift")
            if l["costs"] > abs(l["net_pnl"]) * 0.5:
                diag.append("High turnover friction")
            if l["loss_strats"] >= 2:
                diag.append(f"Multi-strategy failure ({l['loss_strats']}/{l['n_strats']})")
            diag_str = "; ".join(diag) if diag else "Net negative alpha contribution"

            strat_ratio = f"{l['loss_strats']}/{l['n_strats']}"
            print(f"{l['symbol']:<12} {l['net_pnl']:>15,.2f} {l['roi']:>7.1f}% {l['trades']:>8} {l['costs']:>14,.2f} {strat_ratio:>20} {diag_str:<45}")

        total_loss_drag = sum(l["net_pnl"] for l in recommended_losers)
        total_costs_saved = sum(l["costs"] for l in recommended_losers)
        print("-" * 140)
        print(f"SUMMARY: Identified {len(recommended_losers)} losing assets dragging down portfolio performance.")
        print(f"Total Loss Drag Eliminated : {total_loss_drag:>15,.2f} RMB")
        print(f"Total Friction Fees Saved  : {total_costs_saved:>15,.2f} RMB")
    else:
        print("No severe losing assets detected across the evaluated top strategies.")

    # Determine base universe
    base_universe = []
    if universe_file:
        # main() validates this before any analysis runs; re-checking here keeps a direct
        # caller from resurrecting the old silent fallback to observed symbols.
        _require_existing_input(universe_file, "universe file", _UNIVERSE_FALLBACK_HINT)
        with open(universe_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    for sym in line.replace(",", " ").replace('"', '').replace("'", "").split():
                        if sym:
                            base_universe.append(sym)
    else:
        base_universe = sorted(all_observed_symbols)
        # Deliberate convenience fallback: with no --universe-file we can only prune against
        # what these backtests touched. Say so, so it can never be mistaken for a filtered
        # (or complete) universe -- the pruned list is indicative, not authoritative.
        print(
            f"Warning: no --universe-file supplied; pruning against the {len(base_universe)} "
            "observed-symbol fallback set (NOT a filtered or complete universe).",
            file=sys.stderr,
        )

    losing_syms = set(l["symbol"] for l in recommended_losers)
    pruned_universe = [s for s in base_universe if s not in losing_syms]

    print("\n" + "-" * 140)
    print(f"RECOMMENDED PRUNED UNIVERSE ({len(pruned_universe)} of {len(base_universe)} assets retained):")
    print("-" * 140)
    print(" ".join(pruned_universe))

    if export_pruned_path:
        out_p = Path(export_pruned_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w") as f:
            for sym in pruned_universe:
                f.write(f"{sym}\n")
        print(f"\nSuccessfully exported pruned universe to: {out_p.resolve()}")

    print("\nRecommended CLI Execution Snippet:")
    if export_pruned_path:
        print(f"  --universe-file {export_pruned_path}")
    else:
        print(f"  --universe {' '.join(pruned_universe[:10])} ...")
    print("=" * 140)


def audit_abnormal_and_overfit(
    ranked_results: list,
    results_dir: Path,
    top_n: int = 3,
    export_report_path: str = None,
):
    """Conducts a forensic quantitative anomaly & overfitting audit on top N strategies.

    Inspects:
    1. Look-Ahead Bias & Directional Timing Anomalies (buy hit rate > 70%)
    2. Single-Stock All-In Concentration (bets >= 80% or 99%)
    3. Fold 1 Warmup Padding (first trade delayed > 60d)
    4. Outlier Equity Jumps (single-rebalance jump > 30%)
    5. Statistical Alpha Deflation (DSR < 0.05 vs > 0.50)
    6. Profit Concentration / Regime Fragility ("One-Hit Wonder" fold share > 40%)
    7. Superstar Asset Fragility & Leave-One-Out (LOO) Sensitivity Test
    8. Turnover Friction Bleed (Transaction fees as % of gross/net PnL)
    9. Capital Efficiency vs Cash Flattery (Cash-normalized MaxDD)
    10. Overfitting Risk Score (0-100) & Live-Trading Safety Grade (A / B / C / F) -- the
        LETTER is additionally capped by `TURNOVER_CEILING_ANN` (two-sided annualised
        turnover, 2026-10-25 ruling); the numeric score and the ranking are never moved.
    """
    if not ranked_results:
        print("No ranked strategies available for anomaly & overfit audit.", file=sys.stderr)
        return

    top_strats = ranked_results[:top_n]
    print("\n" + "=" * 140)
    print(f"STAGE 6: FORENSIC ANOMALY & OVERFITTING AUDIT (TOP {len(top_strats)} STRATEGIES)")
    print("=" * 140)

    report_markdown_blocks = []

    for idx, r in enumerate(top_strats, 1):
        strat_name = r["strategy"]
        dir_name = r.get("dir_name", "")
        summary = r.get("summary", {})
        folds_perf = summary.get("rolling_window_performance", [])

        asset_data = audit_strategy_assets(results_dir, dir_name)
        trade_audit = audit_strategy_trades(results_dir, dir_name, summary)

        # Was silent: with no asset attribution every asset-level row below degrades to
        # 0.0 and can grade CLEAN. Say so before the matrix is printed.
        if not asset_data:
            _warn(
                f"[{strat_name}] no asset attribution available (missing or unparseable "
                "walkforward_rebalances.csv); the fragility / LOO / friction rows below are "
                "computed from 0.0 and are NOT measurements."
            )

        # 1. Look-ahead bias
        # With no trade record every trade-derived row below silently collapses to its
        # cleanest default (0.50 hit rate, 0.0% position, +0d warmup, 0.0% jump) and grades
        # CLEAN. The numbers are kept for layout/threshold stability but labelled unverified.
        no_trade_data = not trade_audit
        hit_rate = trade_audit.get("buy_hit_rate", 0.50) if trade_audit else 0.50
        hit_rate_pct = hit_rate * 100.0
        lookahead_status = "CLEAN" if hit_rate <= 0.65 else ("WARNING" if hit_rate <= 0.75 else "DATA LEAKAGE RISK")
        lookahead_diag = "Realistic trend hit rate; no forward leakage" if hit_rate <= 0.65 else ("Elevated timing hit rate" if hit_rate <= 0.75 else "Suspiciously high directional accuracy")
        if no_trade_data:
            lookahead_diag = "N/A: no trade record, so directional accuracy is unmeasured (CLEAN is a default, not a result)"

        # 2. All-in bets & concentration
        all_in_cnt = r.get("all_in", 0)
        trades = asset_data.get("trades", []) if asset_data else []
        max_pos = max((abs(t["target_weight"]) for t in trades), default=0.0) * 100.0
        allin_status = "CLEAN" if (all_in_cnt == 0 and max_pos <= 25.0) else ("WARNING" if max_pos <= 50.0 else "HIGH CONCENTRATION")
        allin_diag = f"Strict position caps (Max pos: {max_pos:.1f}%)" if (all_in_cnt == 0 and max_pos <= 25.0) else f"{all_in_cnt} all-in trades; Max pos: {max_pos:.1f}%"
        if no_trade_data:
            allin_diag = "N/A: no trade record, so the maximum position size is unmeasured"

        # 3. Warmup padding
        warmup_days = trade_audit.get("warmup_days_fold1", 0) if trade_audit else 0
        warmup_status = "CLEAN" if warmup_days <= 30 else ("WARNING" if warmup_days <= 60 else "PADDED")
        warmup_diag = f"Immediate trade execution (+{warmup_days}d)" if warmup_days <= 30 else f"Delayed trading by +{warmup_days} days in Fold 1"
        if no_trade_data:
            warmup_diag = "N/A: no trade record, so the Fold-1 first-trade delay is unmeasured"

        # 4. Outlier equity jump
        max_jump = (r.get("max_jump", 0.0) or 0.0) * 100.0
        jump_status = "CLEAN" if max_jump <= 15.0 else ("MODERATE" if max_jump <= 30.0 else "OUTLIER DRIVEN")
        jump_diag = f"Equity curve grew incrementally (Max: {max_jump:.1f}%)" if max_jump <= 15.0 else f"Single rebalance jump of {max_jump:.1f}%"
        if no_trade_data:
            jump_diag = "N/A: no trade record, so the per-rebalance equity jump is unmeasured"

        # 5. DSR statistical edge
        dsr = r.get("dsr", 0.0) or 0.0
        dsr_status = "EXCELLENT" if dsr >= 0.50 else ("PASS" if dsr >= 0.05 else "FAIL / MINED")
        dsr_diag = f"Deflated Sharpe confirms statistical alpha (DSR: {dsr:.4f})" if dsr >= 0.05 else f"DSR {dsr:.4f} indicates high data-mining / noise risk"

        # 6. Profit concentration ("One-Hit Wonder")
        cagrs = [(f.get("cagr") or 0.0) for f in folds_perf]
        pos_cagr_sum = sum(c for c in cagrs if c > 0)
        top_fold_share = (max(cagrs) / pos_cagr_sum * 100.0) if pos_cagr_sum > 0 else 0.0
        best_fold_idx = (cagrs.index(max(cagrs)) + 1) if cagrs else 1
        worst_fold_idx = (cagrs.index(min(cagrs)) + 1) if cagrs else 1
        fold_status = "CLEAN" if top_fold_share <= 40.0 else ("WARNING" if top_fold_share <= 60.0 else "HIGH REGIME RISK")
        if pos_cagr_sum > 0:
            fold_metric = f"{top_fold_share:.1f}% from Fold {best_fold_idx}"
            fold_diag = f"Evenly distributed across folds (Fold {best_fold_idx}: {top_fold_share:.1f}%)" if top_fold_share <= 40.0 else f"Fold {best_fold_idx} generated {top_fold_share:.1f}% of positive returns"
        else:
            # No positive-CAGR fold => "share of positive CAGR" has no denominator. 0.0 used
            # to print as a clean "0.0% from Fold 1", i.e. a fake pass on a dead strategy.
            fold_metric = "N/A (no positive-CAGR fold)"
            fold_diag = (
                f"N/A: no fold had CAGR > 0 ({len(cagrs)} fold(s) evaluated), so the "
                "concentration share is undefined; status below is unverified, not clean"
            )
            _warn(
                f"[{strat_name}] Profit-concentration share is not computable: no fold has "
                "CAGR > 0; reported as N/A instead of a fake 0.0%."
            )

        # 7. Superstar asset fragility & Leave-One-Out (LOO) test
        total_net_pnl = asset_data.get("total_net_pnl", 0.0) if asset_data else 0.0
        winners = asset_data.get("winners", []) if asset_data else []
        losers = asset_data.get("losers", []) if asset_data else []
        assets_list = asset_data.get("assets", []) if asset_data else []

        top1_pnl = winners[0]["net_pnl"] if len(winners) > 0 else 0.0
        top2_pnl = (winners[0]["net_pnl"] + winners[1]["net_pnl"]) if len(winners) > 1 else top1_pnl
        top3_pnl = sum(w["net_pnl"] for w in winners[:3]) if len(winners) > 0 else 0.0

        top3_share = (top3_pnl / total_net_pnl * 100.0) if total_net_pnl > 1e-4 else (999.0 if top3_pnl > 0 else 0.0)
        fragility_status = "ROBUST" if top3_share <= 60.0 else ("WARNING" if top3_share <= 100.0 else "HIGH FRAGILITY")
        if total_net_pnl > 1e-4:
            fragility_metric = f"{top3_share:.1f}% from Top 3"
            fragility_diag = f"Top 3 assets contributed {top3_share:.1f}% of net gain" if top3_share <= 100.0 else f"Top 3 contributed {top3_share:.1f}% (losers drained remaining gain)"
        else:
            # The share is a fraction of net PnL; with net PnL <= 0 it is genuinely undefined.
            # The numeric sentinel is KEPT so no threshold/score above changes, but the report
            # must not present `999.0%` / `0.0%` as if it were a measured percentage.
            fragility_metric = "N/A (net PnL <= 0)"
            fragility_diag = (
                f"N/A: net PnL {total_net_pnl:,.2f} RMB <= 0, so the top-3 gross gain "
                f"({top3_pnl:,.2f} RMB) has no positive denominator (sentinel "
                f"{top3_share:.1f} kept for the fragility / overfit thresholds only)"
            )
            _warn(
                f"[{strat_name}] Superstar-Asset-Fragility share is not computable: net PnL "
                f"{total_net_pnl:,.2f} RMB <= 0 (top-3 gross gain {top3_pnl:,.2f} RMB); "
                f"reported as N/A, threshold sentinel {top3_share:.1f} kept internally."
            )

        # LOO Calculations
        loo_results = []
        loo_results.append({
            "scenario": f"Baseline (All {len(assets_list)} Assets)",
            "pnl": total_net_pnl,
            "drop_pct": 0.0,
            "status": "Normal Operation",
        })
        if len(winners) >= 1:
            pnl_ex1 = total_net_pnl - top1_pnl
            drop_ex1 = (top1_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex1 = "VIABLE" if pnl_ex1 > 0 else "FLIPS UNPROFITABLE (CRITICAL)"
            loo_results.append({
                "scenario": f"Exclude Top 1 ({winners[0]['symbol']})",
                "pnl": pnl_ex1,
                "drop_pct": -drop_ex1,
                "status": stat_ex1,
            })
        if len(winners) >= 2:
            pnl_ex2 = total_net_pnl - top2_pnl
            drop_ex2 = (top2_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex2 = "VIABLE" if pnl_ex2 > 0 else "UNPROFITABLE"
            loo_results.append({
                "scenario": f"Exclude Top 2 ({winners[0]['symbol']}, {winners[1]['symbol']})",
                "pnl": pnl_ex2,
                "drop_pct": -drop_ex2,
                "status": stat_ex2,
            })
        if len(winners) >= 3:
            pnl_ex3 = total_net_pnl - top3_pnl
            drop_ex3 = (top3_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex3 = "VIABLE" if pnl_ex3 > 0 else "SEVERE LOSS DRAIN"
            loo_results.append({
                "scenario": f"Exclude Top 3 ({winners[0]['symbol']}, {winners[1]['symbol']}, {winners[2]['symbol']})",
                "pnl": pnl_ex3,
                "drop_pct": -drop_ex3,
                "status": stat_ex3,
            })

        # 8. Friction drag ratio
        total_costs = asset_data.get("total_cost", 0.0) if asset_data else 0.0
        friction_net_drag = (total_costs / total_net_pnl * 100.0) if total_net_pnl > 1e-4 else (100.0 if total_costs > 0 else 0.0)
        friction_status = "MINIMAL" if friction_net_drag <= 15.0 else ("ELEVATED" if friction_net_drag <= 30.0 else "SEVERE BLEED")
        if total_net_pnl > 1e-4:
            friction_metric = f"{friction_net_drag:.1f}% Fee / Net PnL"
            friction_diag = f"Friction fees consumed {friction_net_drag:.1f}% of net profit ({total_costs:,.2f} RMB)"
        else:
            # Fees as a % of a non-positive net PnL is undefined, not "100%": keep the
            # numeric sentinel for the thresholds/score, print N/A with the raw fees.
            friction_metric = "N/A (net PnL <= 0)"
            friction_diag = (
                f"N/A: net PnL {total_net_pnl:,.2f} RMB <= 0, so friction cannot be expressed "
                f"as a share of net profit (raw fees {total_costs:,.2f} RMB; sentinel "
                f"{friction_net_drag:.1f} kept for the overfit threshold only)"
            )
            _warn(
                f"[{strat_name}] Friction-Bleed ratio is not computable: net PnL "
                f"{total_net_pnl:,.2f} RMB <= 0 with {total_costs:,.2f} RMB of fees; reported "
                f"as N/A, threshold sentinel {friction_net_drag:.1f} kept internally."
            )

        # 9. Capital drag & Cash-normalized MaxDD
        active_wsum = r.get("avg_wsum", 1.0)
        idle_cash = max(0.0, 1.0 - active_wsum) * 100.0
        raw_maxdd = (r.get("maxdd", 0.0) or 0.0) * 100.0
        raw_cagr = (r.get("cagr", 0.0) or 0.0) * 100.0
        if active_wsum < 0.10:
            # The denominator is clamped to 0.10 to keep the normalisation finite; without a
            # warning that clamp reads as a measured cash-normalised MaxDD.
            _warn(
                f"[{strat_name}] active weight sum is {active_wsum:.4f} (< 0.10); the "
                "cash-normalised MaxDD/CAGR below use the 0.10 floor, so treat them as "
                "approximate rather than measured."
            )
        norm_maxdd = raw_maxdd / max(active_wsum, 0.10)
        norm_cagr = raw_cagr / max(active_wsum, 0.10)
        drag_status = "LOW" if idle_cash <= 25.0 else ("MODERATE" if idle_cash <= 50.0 else "HIGH CASH CUSHION")
        drag_diag = f"Avg idle cash: {idle_cash:.1f}% | Norm MaxDD: {norm_maxdd:.1f}% (vs {raw_maxdd:.1f}% raw)"

        # 10. Quantitative Overfitting Risk Score & Grade
        overfit_score = 0
        overfit_flags = []
        if dsr < 0.05:
            overfit_score += 25
            overfit_flags.append(f"DSR {dsr:.4f} < 0.05 (High probability of data-mining / noise)")
        if len(loo_results) > 1 and loo_results[1]["pnl"] <= 0:
            overfit_score += 25
            overfit_flags.append(f"Superstar Fragility: Omitting {winners[0]['symbol']} flips Net PnL to negative ({loo_results[1]['pnl']:,.2f} RMB)")
        elif top3_share > 80.0:
            overfit_score += 15
            if total_net_pnl > 1e-4:
                overfit_flags.append(f"High Winner Concentration: Top 3 generate {top3_share:.1f}% of net gain")
            else:
                # Same flag, same score contribution -- but never print the 999.0 sentinel as
                # if it were a measured share of net gain.
                overfit_flags.append(
                    f"High Winner Concentration: top-3 gross gain {top3_pnl:,.2f} RMB on a "
                    f"non-profitable book (net PnL {total_net_pnl:,.2f} RMB), so the share is N/A"
                )
        if top_fold_share > 50.0 and len(cagrs) > 2:
            overfit_score += 15
            overfit_flags.append(f"One-Hit Wonder Fold: Best Fold {best_fold_idx} accounts for {top_fold_share:.1f}% of positive returns")
        if friction_net_drag > 25.0:
            overfit_score += 10
            if total_net_pnl > 1e-4:
                overfit_flags.append(f"Friction Bleed: Transaction friction consumes {friction_net_drag:.1f}% of net profits")
            else:
                # Sentinels keep the score identical; only the printed wording becomes honest.
                overfit_flags.append(
                    f"Friction Bleed: {total_costs:,.2f} RMB of fees against a non-profitable "
                    f"book (net PnL {total_net_pnl:,.2f} RMB), so the fee ratio is N/A"
                )
        if max_pos >= 80.0:
            overfit_score += 15
            overfit_flags.append(f"Extreme Concentration: Max single position reached {max_pos:.1f}%")
        if max_jump > 30.0:
            overfit_score += 10
            overfit_flags.append(f"Outlier Equity Jump: Single rebalance equity jumped {max_jump:.1f}%")
        if hit_rate > 0.70:
            overfit_score += 25
            overfit_flags.append(f"Look-Ahead Leakage Risk: Buy hit rate is {hit_rate_pct:.1f}%")
        if warmup_days > 60:
            overfit_score += 10
            overfit_flags.append(f"Warmup Padding: Fold 1 delayed first trade by {warmup_days} days")

        overfit_score = min(100, overfit_score)
        if overfit_score <= 20:
            grade_label = "Grade A (Institutional Grade - Highly Robust)"
        elif overfit_score <= 40:
            grade_label = "Grade B (Moderate Caution - Viable with Sizing Rules)"
        elif overfit_score <= 65:
            grade_label = "Grade C (High Overfit Risk - Fragile / Over-Parameterized)"
        else:
            grade_label = "Grade F (Rejected - Severe Overfit / Structural Dependency)"

        # 10b. Turnover ceiling -- LETTER grade ONLY, applied AFTER the score-based grade above.
        # The numeric `overfit_score` and the ranking sort key (`adj_sharpe`) are deliberately
        # NOT touched: the index stays additive/unranked (0 stays 0) so historical Index values
        # remain comparable across this change. Only a grade that would have been A is withheld.
        # Unmeasurable turnover is NEVER scored as 0.0 -- "unknown" cannot earn an A, but it must
        # not look like a clean pass either, so the reason is printed on its own line.
        # `run_ranking` already computes this metric for every row (including rows with no
        # rebalances.csv), so prefer that value and only fall back to the summary's folds when
        # the caller passed a hand-built result dict.
        if "turnover_ann" in r:
            turnover_ann = r["turnover_ann"]
        else:
            turnover_ann = turnover_annualised_from_folds(folds_perf)[0]
        turnover_cap_note = ""
        if grade_label.startswith("Grade A"):
            if turnover_ann is None:
                grade_label = "Grade B (capped from A by turnover ceiling)"
                turnover_cap_note = "turnover unmeasurable ⇒ A withheld"
            elif turnover_ann > TURNOVER_CEILING_ANN:
                grade_label = "Grade B (capped from A by turnover ceiling)"
                turnover_cap_note = (
                    f"two-sided annualised turnover {turnover_ann:.2f}×/yr > "
                    f"{TURNOVER_CEILING_ANN:.1f} ceiling"
                )

        # Console Output
        print(f"\n[{idx}] {strat_name}")
        print("-" * 140)
        print(f"  Safety Grade: {grade_label} | Overfit Risk Index: {overfit_score} / 100")
        if turnover_cap_note:
            print(f"  Grade ceiling applied: {turnover_cap_note}")
        if overfit_flags:
            print("  Overfitting & Fragility Alerts:")
            for flag in overfit_flags:
                print(f"    - ⚠️  {flag}")
        else:
            print("  Overfitting Alerts: None detected (Institutional-grade robustness)")

        print("\n  QUANTITATIVE ANOMALY & OVERFIT INSPECTION MATRIX:")
        print("  " + "-" * 136)
        print(f"  {'Inspection Dimension':<30} {'Audited Metric':<24} {'Safety Benchmark':<24} {'Status':<12} {'Forensic Diagnostic':<44}")
        print("  " + "-" * 136)

        def _mv(text):
            """Metric text; explicitly tagged when the whole trade record is missing."""
            return f"{text} (unverified)" if no_trade_data else text

        matrix_rows = [
            ("Look-Ahead Bias Check", _mv(f"{hit_rate_pct:.1f}% Hit Rate"), "45.0% - 55.0%", lookahead_status, lookahead_diag),
            ("Single-Stock Concentration", _mv(f"Max Pos: {max_pos:.1f}%"), "< 25.0% (Cap: 20%)", allin_status, allin_diag),
            ("Fold 1 Warmup Padding", _mv(f"+{warmup_days} Days"), "< 60 Days", warmup_status, warmup_diag),
            ("Outlier Equity Jumps", _mv(f"{max_jump:.1f}% Single Jump"), "< 30.0%", jump_status, jump_diag),
            ("Statistical Edge (DSR)", f"{dsr:.4f}", "> 0.0500", dsr_status, dsr_diag),
            ("Profit Concentration Skew", fold_metric, "< 40.0%", fold_status, fold_diag),
            ("Superstar Asset Fragility", fragility_metric, "< 60.0%", fragility_status, fragility_diag),
            ("Friction Bleed Ratio", friction_metric, "< 15.0%", friction_status, friction_diag),
            ("Capital Drag & Cash Flattery", f"{idle_cash:.1f}% Idle Cash", "< 25.0%", drag_status, drag_diag),
        ]
        for dim, met, bench, stat, diag in matrix_rows:
            print(f"  {dim:<30} {met:<24} {bench:<24} {stat:<12} {diag:<44}")
        print("  " + "-" * 136)

        print("\n  LEAVE-ONE-OUT (LOO) ASSET FRAGILITY ANALYSIS:")
        print("  " + "-" * 136)
        print(f"  {'Exclusion Scenario':<55} {'Resulting Net PnL':>22} {'PnL Drop %':>15} {'Strategy Viability Assessment':<40}")
        print("  " + "-" * 136)
        for loo in loo_results:
            pnl_str = f"{loo['pnl']:>+18,.2f} RMB"
            drop_str = f"{loo['drop_pct']:>14.1f}%"
            print(f"  {loo['scenario']:<55} {pnl_str} {drop_str} {loo['status']:<40}")
        print("  " + "-" * 136)

        # Build Markdown section
        # The markdown must not present a ceiling-capped grade as a natural B.
        turnover_md_line = (
            f"- **Turnover Grade Ceiling**: applied — {turnover_cap_note}\n"
            if turnover_cap_note
            else ""
        )
        md_block = f"""## {idx}. {strat_name}
- **Safety Grade**: `{grade_label}`
- **Overfit Risk Index**: `{overfit_score} / 100`
{turnover_md_line}
### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
"""
        for dim, met, bench, stat, diag in matrix_rows:
            md_block += f"| **{dim}** | **{met}** | {bench} | `{stat}` | {diag} |\n"

        md_block += f"""
### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
"""
        for loo in loo_results:
            md_block += f"| **{loo['scenario']}** | **{loo['pnl']:+,.2f} RMB** | `{loo['drop_pct']:+.1f}%` | {loo['status']} |\n"

        if overfit_flags:
            md_block += "\n### Identified Overfitting & Fragility Alerts\n"
            for flag in overfit_flags:
                md_block += f"- ⚠️ **{flag}**\n"

        report_markdown_blocks.append(md_block)

    if export_report_path:
        out_p = Path(export_report_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        full_md = f"""# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `{results_dir}`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: {len(top_strats)}  

---

""" + "\n---\n\n".join(report_markdown_blocks)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(full_md)
        print(f"\nSuccessfully exported forensic anomaly & overfit audit report to: {out_p.resolve()}")

    print("=" * 140)


def _print_stage2_trade_line(audit: dict) -> None:
    """One-line Stage-2 digest, shared so `--all` and `--audit` can never drift apart.

    `Turnover:` is the legacy fold-unit ratio (`avg_turnover`); `Turnover/yr` and `Folds` are
    the two-sided, calendar-annualised metric that drives the grade ceiling. `—` means the
    metric is unmeasurable, never 0.0.
    """
    print(f"\n[{audit['strategy']}] Trades: {audit['total_trades']}, "
          f"All-In Events: {audit['all_in_trades']}, "
          f"Avg Weight Sum: {audit['avg_weight_sum']:.2f}, "
          f"Fold 1 Warmup: +{audit['warmup_days_fold1']}d, "
          f"Max Equity Jump: {audit['max_equity_jump']*100:.1f}%, "
          f"Turnover: {audit['avg_turnover']:.1f}x, "
          f"Turnover/yr: {_format_turnover_ann(audit.get('turnover_ann'))}, "
          f"Folds: {audit.get('turnover_folds', '0/0')}, "
          f"Buy Hit Rate: {audit['buy_hit_rate']*100:.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Walkforward Audit & Trading Record Anomaly Analyzer")
    parser.add_argument("--results-dir", type=str, default=None, help="Path to results directory")
    parser.add_argument("--all", action="store_true", help="Run full pipeline: summary, trade audit, adjusted ranking, deep top-3 analysis, losing asset exclusions, and forensic anomaly/overfit audit")
    parser.add_argument("--summary", action="store_true", help="Run cross-strategy performance aggregation")
    parser.add_argument("--audit", action="store_true", help="Run deep-dive trading record anomaly audit")
    parser.add_argument("--rank", action="store_true", help="Compute anomaly-adjusted rankings")
    parser.add_argument("--deep-analyze", "--deep", action="store_true", help="Run deep behavioral & asset attribution audit on top strategies")
    parser.add_argument("--deep-top-n", type=int, default=3, help="Number of top strategies for deep behavioral & asset analysis (default: 3)")
    parser.add_argument("--exclude-losers", action="store_true", help="Identify and recommend losing assets to exclude from trading universe")
    parser.add_argument("--abnormal-overfit", "--overfit", "--audit-overfit", action="store_true", help="Run forensic quantitative anomaly & overfit audit (LOO fragility, superstar concentration, friction bleed, cash flattery)")
    parser.add_argument("--export-overfit-report", type=str, default=None, help="Export detailed anomaly & overfitting markdown report to file")
    parser.add_argument("--universe-file", type=str, default=None, help="Path to original universe file to prune")
    parser.add_argument("--export-pruned-universe", type=str, default=None, help="Path to export the pruned universe file")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top strategies to evaluate in ranking")
    parser.add_argument("--strategy", type=str, default=None, help="Analyze single strategy by name")

    args = parser.parse_args()

    # Startup precondition, BEFORE any input validation or audit phase: a missing
    # pandas/numpy/scipy/yfinance/duckdb used to degrade every fold-end valuation to
    # last-trade-price via a swallowed ImportError. Crash loudly instead (exit 2).
    _require_third_party_deps()

    # Validate explicitly supplied inputs BEFORE any analysis: a typo'd / wrong-cwd
    # --universe-file used to silently prune against the observed-symbol set instead.
    try:
        universe_path = _require_existing_input(
            args.universe_file, "universe file", _UNIVERSE_FALLBACK_HINT
        )
        results_dir = find_results_dir(args.results_dir)
    except AuditInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"       {exc.hint}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:  # pre-existing message, wording unchanged
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    print(f"Using results directory: {results_dir}")
    if args.results_dir:
        print("  (selected because --results-dir was passed explicitly)\n")
    else:
        print(f"  (selected because ./results was auto-discovered under cwd={Path.cwd()})\n")

    summaries = load_all_summaries(results_dir, strategy_name=args.strategy)
    if not summaries:
        print("No walkforward_summary.json files found.", file=sys.stderr)
        if args.results_dir:
            print(
                f"       --results-dir '{args.results_dir}' was given explicitly but no "
                f"walkforward_summary.json exists in '{results_dir}' or its subdirectories.",
                file=sys.stderr,
            )
        sys.exit(1)

    has_specific_action = (
        args.summary or args.audit or args.rank or args.deep_analyze or args.exclude_losers or args.abnormal_overfit
    )

    if args.all or not has_specific_action:
        # Full institutional audit pipeline
        run_summary(summaries)
        print("\n" + "#" * 100)
        print("# STAGE 2: TRADING RECORD ANOMALY AUDIT")
        print("#" * 100)
        sorted_s = sorted(summaries, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:args.top_n]
        for s in sorted_s:
            audit = audit_strategy_trades(results_dir, s["dir_name"], s)
            if audit:
                _print_stage2_trade_line(audit)
        print("\n")
        ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
        deep_analyze_top_strategies(
            ranked,
            results_dir,
            top_n=args.deep_top_n,
            universe_file=str(universe_path) if universe_path else None,
            export_pruned_path=args.export_pruned_universe,
        )
        audit_abnormal_and_overfit(
            ranked,
            results_dir,
            top_n=args.deep_top_n,
            export_report_path=args.export_overfit_report,
        )
    else:
        ranked = None
        if args.summary:
            run_summary(summaries)
        if args.audit:
            sorted_s = sorted(summaries, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:args.top_n]
            for s in sorted_s:
                audit = audit_strategy_trades(results_dir, s["dir_name"], s)
                if audit:
                    _print_stage2_trade_line(audit)
            # Bottom-of-output convention line for the plain `--audit` digest. When a ranking
            # table is about to be printed it emits the same line, so never print it twice.
            if not (
                args.rank or args.deep_analyze or args.exclude_losers or args.abnormal_overfit
            ):
                print(TURNOVER_CONVENTION_NOTE)
        if args.rank or args.deep_analyze or args.exclude_losers or args.abnormal_overfit:
            ranked = run_ranking(summaries, results_dir, top_n=args.top_n)

        if args.deep_analyze or args.exclude_losers:
            if ranked is None:
                ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
            deep_analyze_top_strategies(
                ranked,
                results_dir,
                top_n=args.deep_top_n,
                universe_file=str(universe_path) if universe_path else None,
                export_pruned_path=args.export_pruned_universe,
            )

        if args.abnormal_overfit:
            if ranked is None:
                ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
            audit_abnormal_and_overfit(
                ranked,
                results_dir,
                top_n=args.deep_top_n,
                export_report_path=args.export_overfit_report,
            )

    # End-of-run honesty gate: name every section whose numbers were produced from
    # degraded inputs, so a partially-broken audit can never be reported as clean.
    # Exits 3 (not 0) when anything was disqualified; 1 = bad input, 2 = missing dep.
    _report_degradation_summary()


if __name__ == "__main__":
    main()


#!/usr/bin/env python
"""CLI entry point: run the full research_strategy -> instrument_selection ->
strategy_generator -> backtester pipeline end-to-end, chaining each project's
own documented CLI (see root README's "End-to-End Quantitative Workflow") as
an opaque subprocess call and auto-wiring each step's output file into the
next step's input flag, using the existing results/ conventions already
established by each project. Exposes only the flags a real end-to-end run
needs to vary -- for anything else, run the 4 steps manually per the root
README.
"""
import argparse
import os
import subprocess
import sys
import time

from common.cli_utils import default_results_dir
from common.reporting import utc_timestamp, write_json_report

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = default_results_dir(__file__)

SCRIPTS = {
    "research_strategy": os.path.join(REPO_ROOT, "research_strategy", "run_research_strategy.py"),
    "instrument_selection": os.path.join(REPO_ROOT, "instrument_selection", "run_screener.py"),
    "strategy_generator": os.path.join(REPO_ROOT, "strategy_generator", "run_strategygen.py"),
    "backtester": os.path.join(REPO_ROOT, "backtester", "run_backtest.py"),
}
FACTOR_SUMMARY_PATH = os.path.join(REPO_ROOT, "research_strategy", "results", "factor_summary.json")
BASKET_PATH = os.path.join(REPO_ROOT, "instrument_selection", "results", "basket.json")
STRATEGY_PATH = os.path.join(REPO_ROOT, "strategy_generator", "results", "strategy.json")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--universe", "-u", nargs="+", default=None,
        help="Ticker universe for instrument_selection's screening step (step 2 only -- "
             "step 1's factor-research universe is untouched). Omit to use "
             "instrument_selection's own default universe.")
    p.add_argument("--data-provider", default="synthetic",
        help="Applied IDENTICALLY to all 4 steps. Default 'synthetic' matches this "
             "workspace's no-real-market-data-by-default policy: it's a no-op for "
             "research_strategy (already defaults to 'synthetic') but downgrades "
             "instrument_selection/strategy_generator/backtester from their own "
             "'yfinance' default so one flag controls the whole run consistently. "
             "Pass 'yfinance' for a real end-to-end run.")
    p.add_argument("--select-method",
        choices=["top_k", "cluster", "greedy", "threshold", "max_diversification"],
        default="threshold", help="instrument_selection --select-method passthrough (step 2).")
    p.add_argument("--select-max-k", type=int, default=8,
        help="instrument_selection --select-max-k passthrough (step 2).")
    p.add_argument("--mine-patterns", action="store_true",
        help="strategy_generator --mine-patterns passthrough (step 3).")
    p.add_argument("--mode", choices=["standard", "walkforward"], default="standard",
        help="backtester --mode passthrough (step 4).")
    p.add_argument("--baseline-symbol", default=None,
        help="backtester --baseline-symbol passthrough (step 4).")
    p.add_argument("--baseline-template", default=None,
        help="backtester --baseline-template passthrough (step 4); only meaningful "
             "together with --baseline-symbol.")
    p.add_argument("--no-plots", action="store_true",
        help="backtester/strategy_generator --no-plots passthrough -- skip equity-curve charts.")
    p.add_argument("--dry-run", action="store_true",
        help="Print the 4 resolved commands without executing anything.")
    return p


def run_step(step_num, name, cmd):
    print(f"\n{'='*80}\n[Step {step_num}/4] {name}\n{'='*80}")
    print("Running: " + " ".join(cmd))
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=REPO_ROOT, stderr=subprocess.PIPE, text=True)
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        print(f"\n[Step {step_num}/4] {name} FAILED (exit code {result.returncode}, {elapsed:.1f}s).", file=sys.stderr)
        print("---- captured stderr ----", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
    else:
        print(f"[Step {step_num}/4] {name} completed in {elapsed:.1f}s.")
    return result


def _write_manifest(started_at, args, step_records, status):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    manifest = {
        "started_at": started_at,
        "finished_at": utc_timestamp(),
        "status": status,
        "resolved_args": vars(args),
        "steps": step_records,
        "artifacts": {
            "factor_summary": FACTOR_SUMMARY_PATH,
            "basket": BASKET_PATH,
            "strategy": STRATEGY_PATH,
            "backtest_equity": os.path.join(REPO_ROOT, "backtester", "results", "backtest_equity.csv"),
            "backtest_weights": os.path.join(REPO_ROOT, "backtester", "results", "backtest_weights.csv"),
            "walkforward_report": os.path.join(REPO_ROOT, "backtester", "results", "walkforward_report.csv"),
        },
    }
    timestamp_compact = started_at.replace(":", "").replace("-", "").replace(".", "")
    path = os.path.join(RESULTS_DIR, f"pipeline_manifest_{timestamp_compact}.json")
    write_json_report(manifest, path)
    return path


def main():
    args = build_arg_parser().parse_args()
    started_at = utc_timestamp()
    step_records = []

    def do_step(n, name, key, extra_args):
        cmd = [sys.executable, SCRIPTS[key]] + extra_args
        if args.dry_run:
            print(f"[DRY RUN] [Step {n}/4] {name}: " + " ".join(cmd))
            return
        result = run_step(n, name, cmd)
        step_records.append({"step": n, "name": name, "argv": cmd, "returncode": result.returncode})
        if result.returncode != 0:
            manifest_path = _write_manifest(started_at, args, step_records, status="failed")
            print(f"\nPipeline failed at step {n}/4. Manifest: {manifest_path}", file=sys.stderr)
            sys.exit(result.returncode)

    step1_args = ["--strategy", "all", "--data-provider", args.data_provider]

    step2_args = ["--select-method", args.select_method, "--data-provider", args.data_provider]
    if args.universe:
        step2_args = ["--universe", *args.universe] + step2_args
    if args.select_max_k is not None:
        step2_args += ["--select-max-k", str(args.select_max_k)]

    step3_args = ["--universe-file", BASKET_PATH, "--factor-report", FACTOR_SUMMARY_PATH,
                  "--mode", "generate", "--data-provider", args.data_provider]
    if args.mine_patterns:
        step3_args.append("--mine-patterns")
    if args.no_plots:
        step3_args.append("--no-plots")

    step4_args = ["--strategy-file", STRATEGY_PATH, "--universe-file", BASKET_PATH,
                  "--mode", args.mode, "--data-provider", args.data_provider]
    if args.baseline_symbol:
        step4_args += ["--baseline-symbol", args.baseline_symbol]
        if args.baseline_template:
            step4_args += ["--baseline-template", args.baseline_template]
    if args.no_plots:
        step4_args.append("--no-plots")

    do_step(1, "research_strategy (factor research)", "research_strategy", step1_args)
    do_step(2, "instrument_selection (universe screening)", "instrument_selection", step2_args)
    do_step(3, "strategy_generator (allocation strategy search)", "strategy_generator", step3_args)
    do_step(4, "backtester (out-of-sample evaluation)", "backtester", step4_args)

    if args.dry_run:
        print("\n[DRY RUN] No steps were executed.")
        return

    manifest_path = _write_manifest(started_at, args, step_records, status="ok")
    print(f"\n{'='*80}\nPipeline completed successfully.\n{'='*80}")
    print(f"  Factor summary:      {FACTOR_SUMMARY_PATH}")
    print(f"  Basket:              {BASKET_PATH}")
    print(f"  Strategy:            {STRATEGY_PATH}")
    print(f"  Backtest results:    backtester/results/")
    print(f"  Manifest:            {manifest_path}")


if __name__ == "__main__":
    main()

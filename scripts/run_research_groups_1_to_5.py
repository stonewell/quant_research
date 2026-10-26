#!/usr/bin/env python3
"""Runner script for backtest research Groups 1-5 (§6.1 - §6.5, §7).

Executes research backtest tasks sequentially, streams output, captures logs,
and summarizes key performance metrics.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Repo root derived from this file (scripts/ -> <repo>), so the script is checkout-portable.
REPO_ROOT = Path(__file__).resolve().parents[1]
PY_BIN = REPO_ROOT / "pipeline/.venv/bin/python"
RUN_TS = "20261025_125300"
BT_ROOT = REPO_ROOT / f"backtests_research/{RUN_TS}"
UNIV_FILE = BT_ROOT / "universe_20_stocks.txt"
DUMPS_DIR = REPO_ROOT / "pipeline/research_strategy/results/strategy_dumps"

ALL_6_STRATEGIES = [
    "chan_four_state_blend",
    "chan_risk_managed_blend",
    "chan_crisis_shield_blend",
    "chan_four_state_shield_blend",
    "chan_dual_hybrid_blend",
    "generic_blend",
]

R2A_3_STRATEGIES = [
    "chan_four_state_blend",
    "chan_risk_managed_blend",
    "generic_blend",
]


def check_completed(out_dir: Path) -> bool:
    manifest_path = out_dir / "run_manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("status") == "completed"
        except Exception:
            return False
    return False


def run_single_backtest(task_name: str, cmd: list[str], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "run.log"

    if check_completed(out_dir):
        print(f"[{task_name}] Already completed in {out_dir}. Skipping.")
        return {"status": "skipped", "duration_sec": 0.0}

    print(f"[{task_name}] Starting backtest...")
    print(f"[{task_name}] Command: {' '.join(cmd)}")
    start_time = time.time()

    with open(log_file, "w", encoding="utf-8") as f_log:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(REPO_ROOT),
        )
        for line in process.stdout:
            sys.stdout.write(line)
            f_log.write(line)
        process.wait()

    duration = time.time() - start_time
    print(f"[{task_name}] Finished in {duration:.1f}s with return code {process.returncode}")

    if process.returncode != 0:
        raise RuntimeError(f"[{task_name}] Backtest failed with code {process.returncode}. See {log_file}")

    return {"status": "completed", "duration_sec": duration}


def run_group_1():
    print("\n" + "=" * 80)
    print("=== GROUP 1: §6.2 Round 1: Long Window (4 Folds, 2.0y / 0.25y) ===")
    print("=" * 80)
    for s in ALL_6_STRATEGIES:
        out_dir = BT_ROOT / f"01_r1_longwin/{s}"
        cmd = [
            str(PY_BIN),
            "backtester/run_backtest.py",
            "--strategy-file", str(DUMPS_DIR / f"{s}_strategy.json"),
            "--universe-file", str(UNIV_FILE),
            "--start", "2022-01-04",
            "--end", "2024-12-31",
            "--mode", "walkforward",
            "--window-years", "2.0",
            "--step-years", "0.25",
            "--data-provider", "fuyao",
            "--min-shares", "100",
            "--china-trading",
            "--baseline-symbol", "000300.SH",
            "--results-dir", str(out_dir),
            "--n-jobs", "1",
        ]
        run_single_backtest(f"G1_R1_{s}", cmd, out_dir)


def run_group_2():
    print("\n" + "=" * 80)
    print("=== GROUP 2: §6.3 Round 2a: Continuous Standard (2022-01-04 -> 2026-10-22) ===")
    print("=" * 80)
    for s in R2A_3_STRATEGIES:
        out_dir = BT_ROOT / f"02_r2a_std/{s}"
        cmd = [
            str(PY_BIN),
            "backtester/run_backtest.py",
            "--strategy-file", str(DUMPS_DIR / f"{s}_strategy.json"),
            "--universe-file", str(UNIV_FILE),
            "--start", "2022-01-04",
            "--end", "2026-10-22",
            "--mode", "standard",
            "--data-provider", "fuyao",
            "--min-shares", "100",
            "--china-trading",
            "--baseline-symbol", "000300.SH",
            "--results-dir", str(out_dir),
            "--n-jobs", "1",
        ]
        run_single_backtest(f"G2_R2A_{s}", cmd, out_dir)


def run_group_3():
    print("\n" + "=" * 80)
    print("=== GROUP 3: §6.4 Round 2b: OOS Walkforward (6 Folds, 0.25y / 0.25y) ===")
    print("=" * 80)
    for s in ALL_6_STRATEGIES:
        out_dir = BT_ROOT / f"03_r2b_oos/{s}"
        cmd = [
            str(PY_BIN),
            "backtester/run_backtest.py",
            "--strategy-file", str(DUMPS_DIR / f"{s}_strategy.json"),
            "--universe-file", str(UNIV_FILE),
            "--start", "2025-01-02",
            "--end", "2026-10-22",
            "--mode", "walkforward",
            "--window-years", "0.25",
            "--step-years", "0.25",
            "--data-provider", "fuyao",
            "--min-shares", "100",
            "--china-trading",
            "--baseline-symbol", "000300.SH",
            "--results-dir", str(out_dir),
            "--n-jobs", "1",
        ]
        run_single_backtest(f"G3_R2B_{s}", cmd, out_dir)


def run_group_4():
    print("\n" + "=" * 80)
    print("=== GROUP 4: §6.5 Round 1 Circuit Breaker Compare (chan_four_state_blend) ===")
    print("=" * 80)
    arms = [
        ("baseline", "{}"),
        ("no_circuit", '{"cfsb_dd_reduce_thresh": 10.0, "cfsb_dd_defensive_thresh": 10.0, "cfsb_dd_stop_thresh": 10.0}'),
    ]
    for arm, extra in arms:
        out_dir = BT_ROOT / f"04_r1_std_cb_compare/{arm}/chan_four_state_blend"
        cmd = [
            str(PY_BIN),
            "backtester/run_backtest.py",
            "--strategy-file", str(DUMPS_DIR / "chan_four_state_blend_strategy.json"),
            "--universe-file", str(UNIV_FILE),
            "--start", "2022-01-04",
            "--end", "2024-12-31",
            "--mode", "standard",
            "--data-provider", "fuyao",
            "--min-shares", "100",
            "--china-trading",
            "--baseline-symbol", "000300.SH",
            "--strategy-params", extra,
            "--results-dir", str(out_dir),
            "--n-jobs", "1",
        ]
        run_single_backtest(f"G4_CB_{arm}", cmd, out_dir)


def run_group_5():
    print("\n" + "=" * 80)
    print("=== GROUP 5: §7 Anatomy: 2022-2026 Standard + 1.5% Carry + Market Impact ===")
    print("=" * 80)
    for s in ALL_6_STRATEGIES:
        out_dir = BT_ROOT / f"05_anatomy/{s}"
        cmd = [
            str(PY_BIN),
            "backtester/run_backtest.py",
            "--strategy-file", str(DUMPS_DIR / f"{s}_strategy.json"),
            "--universe-file", str(UNIV_FILE),
            "--start", "2022-01-04",
            "--end", "2026-10-22",
            "--mode", "standard",
            "--data-provider", "fuyao",
            "--min-shares", "100",
            "--china-trading",
            "--baseline-symbol", "000300.SH",
            "--cash-sweep-rate", "0.015",
            "--market-impact",
            "--max-volume-participation", "0.05",
            "--results-dir", str(out_dir),
            "--n-jobs", "1",
        ]
        run_single_backtest(f"G5_ANATOMY_{s}", cmd, out_dir)


def collect_metrics_and_report():
    print("\n" + "=" * 80)
    print("=== AGGREGATING RESULTS ACROSS GROUPS 1 - 5 ===")
    print("=" * 80)

    summary = {}

    # Define targets to collect
    targets = []

    # Group 1
    for s in ALL_6_STRATEGIES:
        targets.append(("01_r1_longwin", s, BT_ROOT / f"01_r1_longwin/{s}"))

    # Group 2
    for s in R2A_3_STRATEGIES:
        targets.append(("02_r2a_std", s, BT_ROOT / f"02_r2a_std/{s}"))

    # Group 3
    for s in ALL_6_STRATEGIES:
        targets.append(("03_r2b_oos", s, BT_ROOT / f"03_r2b_oos/{s}"))

    # Group 4
    for arm in ["baseline", "no_circuit"]:
        targets.append(("04_r1_std_cb_compare", f"chan_four_state_blend_{arm}", BT_ROOT / f"04_r1_std_cb_compare/{arm}/chan_four_state_blend"))

    # Group 5
    for s in ALL_6_STRATEGIES:
        targets.append(("05_anatomy", s, BT_ROOT / f"05_anatomy/{s}"))

    records = []
    for grp, strat_key, path in targets:
        manifest_file = path / "run_manifest.json"
        comp_file = path / "comparison_report.json"
        wf_file = path / "walkforward_summary.json"

        rec = {
            "group": grp,
            "strategy": strat_key,
            "dir": str(path),
            "manifest_exists": manifest_file.exists(),
        }

        if manifest_file.exists():
            with open(manifest_file, "r", encoding="utf-8") as f:
                man = json.load(f)
            rec["status"] = man.get("status")
            rec["completed_at"] = man.get("completed_at_utc")
            rec["mode"] = man.get("mode")
            metrics = man.get("metrics") or {}
            rec["sharpe"] = metrics.get("sharpe")
            rec["cagr"] = metrics.get("cagr")
            rec["max_dd"] = metrics.get("max_drawdown")
            rec["calmar"] = metrics.get("calmar")
            rec["n_folds"] = metrics.get("n_folds")
            rec["dsr"] = metrics.get("deflated_sharpe")

        if comp_file.exists():
            with open(comp_file, "r", encoding="utf-8") as f:
                comp = json.load(f)
            rec["baseline_sharpe"] = comp.get("mean_baseline_sharpe_ratio", comp.get("baseline_sharpe_ratio"))
            rec["baseline_cagr"] = comp.get("mean_baseline_cagr", comp.get("baseline_cagr"))
            rec["baseline_max_dd"] = comp.get("mean_baseline_max_drawdown", comp.get("baseline_max_drawdown"))
            rec["outperformance_cagr"] = comp.get("mean_outperformance_cagr", comp.get("outperformance_cagr"))

        records.append(rec)

    # Save summary JSON
    out_json = BT_ROOT / "groups_1_to_5_summary.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    # Print summary table
    print(f"\nSaved summary JSON to {out_json}")
    print(f"\n{'Group':<22} | {'Strategy':<30} | {'Sharpe':<7} | {'CAGR':<8} | {'MaxDD':<7} | {'Calmar':<7} | {'Alpha/Outp':<10}")
    print("-" * 105)
    for r in records:
        sh = f"{r.get('sharpe', 0.0):.2f}" if r.get("sharpe") is not None else "N/A"
        cg = f"{r.get('cagr', 0.0)*100:.1f}%" if r.get("cagr") is not None else "N/A"
        dd = f"{r.get('max_dd', 0.0)*100:.1f}%" if r.get("max_dd") is not None else "N/A"
        cal = f"{r.get('calmar', 0.0):.2f}" if r.get("calmar") is not None else "N/A"
        outp = f"{r.get('outperformance_cagr', 0.0)*100:.1f}%" if r.get("outperformance_cagr") is not None else "N/A"
        print(f"{r['group']:<22} | {r['strategy']:<30} | {sh:<7} | {cg:<8} | {dd:<7} | {cal:<7} | {outp:<10}")


def main():
    print(f"Executing Research Backtests for Groups 1-5")
    print(f"Timestamp: {RUN_TS}")
    print(f"Root: {BT_ROOT}")
    t0 = time.time()

    run_group_1()
    run_group_2()
    run_group_3()
    run_group_4()
    run_group_5()

    collect_metrics_and_report()

    total_time = time.time() - t0
    print(f"\nAll Groups 1-5 finished in {total_time:.1f}s ({total_time/60:.2f}m)!")


if __name__ == "__main__":
    main()

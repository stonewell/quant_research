#!/usr/bin/env python3
"""Group 6 (§8 Ablation Study 06_ablate) Runner for Arms 8-13.

Arms:
  - no_sectorthrot
  - no_hysteresis
  - no_thrust
  - no_inertia
  - bull_full
  - no_circuit

Strategies:
  - chan_four_state_blend
  - chan_risk_managed_blend
  - chan_dual_hybrid_blend
"""

import json
import os
import re
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

ARMS = [
    "no_sectorthrot",
    "no_hysteresis",
    "no_thrust",
    "no_inertia",
    "bull_full",
    "no_circuit",
]

STRATEGIES = [
    "chan_four_state_blend",
    "chan_risk_managed_blend",
    "chan_dual_hybrid_blend",
]


def pfx_of(strat: str) -> str:
    if strat == "chan_four_state_blend":
        return "cfsb"
    elif strat == "chan_risk_managed_blend":
        return "crb"
    elif strat == "chan_dual_hybrid_blend":
        return "cdhb"
    return "blend"


def get_extra_params(arm: str, strat: str) -> str:
    pfx = pfx_of(strat)
    if arm == "no_sectorthrot":
        return json.dumps({
            f"{pfx}_max_assets_per_sector": 20,
            "blend_max_assets_per_sector": 20,
            f"{pfx}_adaptive_sector_throttle": False,
        })
    elif arm == "no_hysteresis":
        return json.dumps({"blend_enable_regime_hysteresis": False})
    elif arm == "no_thrust":
        return json.dumps({f"{pfx}_dynamic_cash_deployment": False})
    elif arm == "no_inertia":
        return json.dumps({
            f"{pfx}_adaptive_inertia": False,
            f"{pfx}_min_weight_change": 0.0,
            f"{pfx}_min_holding_bars": 0,
            f"{pfx}_entry_threshold": 0.0,
        })
    elif arm == "bull_full":
        return json.dumps({f"{pfx}_target_bull_exposure": 1.0})
    elif arm == "no_circuit":
        return json.dumps({
            f"{pfx}_dd_reduce_thresh": 10.0,
            f"{pfx}_dd_defensive_thresh": 10.0,
            f"{pfx}_dd_stop_thresh": 10.0,
        })
    else:
        raise ValueError(f"Unknown arm: {arm}")


def check_completed(out_dir: Path) -> bool:
    manifest_path = out_dir / "run_manifest.json"
    log_path = out_dir / "run.log"
    if manifest_path.exists() and log_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") == "completed":
                with open(log_path, "r", encoding="utf-8") as f:
                    text = f.read()
                return "Sharpe Ratio:" in text
        except Exception:
            return False
    return False


def run_single_backtest(arm: str, strat: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "run.log"
    task_name = f"{arm}/{strat}"

    if check_completed(out_dir):
        print(f"[{task_name}] Already completed. Skipping.")
        return {"status": "skipped", "duration_sec": 0.0}

    extra_str = get_extra_params(arm, strat)
    cmd = [
        str(PY_BIN),
        "backtester/run_backtest.py",
        "--strategy-file", str(DUMPS_DIR / f"{strat}_strategy.json"),
        "--universe-file", str(UNIV_FILE),
        "--start", "2022-01-04",
        "--end", "2026-10-22",
        "--mode", "standard",
        "--data-provider", "fuyao",
        "--min-shares", "100",
        "--china-trading",
        "--baseline-symbol", "000300.SH",
        "--cash-sweep-rate", "0.015",
        "--strategy-params", extra_str,
        "--results-dir", str(out_dir),
        "--n-jobs", "1",
    ]

    print(f"\n{'='*70}")
    print(f"[{task_name}] Starting backtest...")
    print(f"[{task_name}] Extra params: {extra_str}")
    print(f"{'='*70}")
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


def parse_results():
    results = []
    for arm in ARMS:
        for strat in STRATEGIES:
            out_dir = BT_ROOT / f"06_ablate/{arm}/{strat}"
            log_file = out_dir / "run.log"
            entry = {
                "arm": arm,
                "strategy": strat,
                "dir": str(out_dir),
                "sharpe": None,
                "cagr": None,
                "max_drawdown": None,
                "calmar": None,
                "total_turnover": None,
            }
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    log_text = f.read()

                m_std = re.search(r"Sharpe Ratio:\s*([\d.-]+)\s*\|\s*CAGR:\s*([\d.-]+)%\s*\|\s*Max Drawdown:\s*([\d.-]+)%", log_text)
                if m_std:
                    entry["sharpe"] = float(m_std.group(1))
                    entry["cagr"] = float(m_std.group(2)) / 100.0
                    entry["max_drawdown"] = float(m_std.group(3)) / 100.0

                m_cal = re.search(r"Calmar Ratio:\s*([\d.-]+)\s*\|\s*Win Rate:\s*([\d.-]+)%\s*\|\s*Profit Factor:\s*([\d.-]+)", log_text)
                if m_cal:
                    entry["calmar"] = float(m_cal.group(1))

                m_turn = re.search(r"Total Turnover:\s*([\d.-]+)", log_text)
                if m_turn:
                    entry["total_turnover"] = float(m_turn.group(1))

            results.append(entry)

    summary_file = BT_ROOT / "06_ablate/ablation_arms_8_13_summary.json"
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print("\n" + "=" * 90)
    print("=== SUMMARY: §8 Ablation Study 06_ablate (Arms 8-13) ===")
    print("=" * 90)
    header = f"{'Arm':<16} | {'Strategy':<26} | {'Sharpe':<7} | {'CAGR':<8} | {'MaxDD':<7} | {'Calmar':<7} | {'Turnover':<8}"
    print(header)
    print("-" * len(header))
    for r in results:
        arm_s = r["arm"]
        strat_s = r["strategy"].replace("chan_", "").replace("_blend", "")
        sh_s = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        cg_s = f"{r['cagr']*100:.2f}%" if r["cagr"] is not None else "N/A"
        dd_s = f"{r['max_drawdown']*100:.2f}%" if r["max_drawdown"] is not None else "N/A"
        cal_s = f"{r['calmar']:.2f}" if r["calmar"] is not None else "N/A"
        turn_s = f"{r['total_turnover']:.2f}" if r["total_turnover"] is not None else "N/A"
        print(f"{arm_s:<16} | {strat_s:<26} | {sh_s:<7} | {cg_s:<8} | {dd_s:<7} | {cal_s:<7} | {turn_s:<8}")
    print("=" * 90)
    return results


def main():
    total_start = time.time()
    for arm in ARMS:
        for strat in STRATEGIES:
            out_dir = BT_ROOT / f"06_ablate/{arm}/{strat}"
            run_single_backtest(arm, strat, out_dir)

    total_duration = time.time() - total_start
    print(f"\nAll assigned ablation runs completed in {total_duration/60:.1f} minutes.")
    parse_results()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Parse and aggregate all metrics from Groups 1-5 runs."""

import json
import re
from pathlib import Path

# Repo root derived from this file (scripts/ -> <repo>), so the script is checkout-portable.
# The run id stays pinned deliberately: this report targets the archived 20261025_125300 batch.
BT_ROOT = Path(__file__).resolve().parents[1] / "backtests_research" / "20261025_125300"
results = []

for log_path in sorted(BT_ROOT.glob("**/run.log")):
    rel_path = log_path.relative_to(BT_ROOT)
    parts = rel_path.parts
    group = parts[0]
    strat = "/".join(parts[1:-1])

    comp_file = log_path.parent / "comparison_report.json"
    wf_file = log_path.parent / "walkforward_summary.json"
    manifest_file = log_path.parent / "run_manifest.json"

    entry = {
        "group": group,
        "strategy": strat,
        "dir": str(log_path.parent),
    }

    with open(log_path, "r", encoding="utf-8") as f:
        log_text = f.read()

    # Extract standard mode metrics from log
    m_std = re.search(r"Sharpe Ratio:\s*([\d.-]+)\s*\|\s*CAGR:\s*([\d.-]+)%\s*\|\s*Max Drawdown:\s*([\d.-]+)%", log_text)
    if m_std:
        entry["sharpe"] = float(m_std.group(1))
        entry["cagr"] = float(m_std.group(2)) / 100.0
        entry["max_drawdown"] = float(m_std.group(3)) / 100.0

    m_cal = re.search(r"Calmar Ratio:\s*([\d.-]+)\s*\|\s*Win Rate:\s*([\d.-]+)%\s*\|\s*Profit Factor:\s*([\d.-]+)", log_text)
    if m_cal:
        entry["calmar"] = float(m_cal.group(1))
        entry["win_rate"] = float(m_cal.group(2)) / 100.0
        entry["profit_factor"] = float(m_cal.group(3))

    m_reb = re.search(r"Total Rebalances:\s*(\d+)", log_text)
    if m_reb:
        entry["total_rebalances"] = int(m_reb.group(1))

    m_turn = re.search(r"Total Turnover:\s*([\d.-]+)", log_text)
    if m_turn:
        entry["total_turnover"] = float(m_turn.group(1))

    # Extract walkforward summary if present
    if wf_file.exists():
        with open(wf_file, "r", encoding="utf-8") as f:
            wf = json.load(f)
        entry["mean_sharpe"] = wf.get("mean_sharpe_ratio")
        entry["mean_cagr"] = wf.get("mean_cagr")
        entry["mean_max_drawdown"] = wf.get("mean_max_drawdown")
        entry["mean_calmar"] = wf.get("mean_calmar_ratio")
        entry["deflated_sharpe"] = wf.get("deflated_sharpe_ratio")
        entry["n_folds"] = wf.get("n_folds")

    # Extract comparison report
    if comp_file.exists():
        with open(comp_file, "r", encoding="utf-8") as f:
            comp = json.load(f)
        entry["baseline_sharpe"] = comp.get("mean_baseline_sharpe_ratio", comp.get("baseline_sharpe_ratio"))
        entry["baseline_cagr"] = comp.get("mean_baseline_cagr", comp.get("baseline_cagr"))
        entry["baseline_max_drawdown"] = comp.get("mean_baseline_max_drawdown", comp.get("baseline_max_drawdown"))
        entry["outperformance_cagr"] = comp.get("mean_outperformance_cagr", comp.get("outperformance_cagr"))
        entry["alpha"] = comp.get("alpha")
        entry["beta"] = comp.get("beta")
        entry["information_ratio"] = comp.get("information_ratio")

    results.append(entry)

with open(BT_ROOT / "groups_1_to_5_metrics_detailed.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"Total entries parsed: {len(results)}\n")
header = f"{'Group':<22} | {'Strategy':<35} | {'Sharpe':<7} | {'CAGR':<8} | {'MaxDD':<7} | {'Calmar':<7} | {'Outperf':<8} | {'Turnover':<8}"
print(header)
print("-" * len(header))
for r in results:
    sh = r.get("mean_sharpe", r.get("sharpe"))
    cg = r.get("mean_cagr", r.get("cagr"))
    dd = r.get("mean_max_drawdown", r.get("max_drawdown"))
    cal = r.get("mean_calmar", r.get("calmar"))
    outp = r.get("outperformance_cagr")
    turn = r.get("total_turnover")

    sh_s = f"{sh:.2f}" if sh is not None else "N/A"
    cg_s = f"{cg*100:.1f}%" if cg is not None else "N/A"
    dd_s = f"{dd*100:.1f}%" if dd is not None else "N/A"
    cal_s = f"{cal:.2f}" if cal is not None else "N/A"
    outp_s = f"{outp*100:.1f}%" if outp is not None else "N/A"
    turn_s = f"{turn:.1f}" if turn is not None else "N/A"

    grp_s = r["group"]
    strat_s = r["strategy"]
    print(f"{grp_s:<22} | {strat_s:<35} | {sh_s:<7} | {cg_s:<8} | {dd_s:<7} | {cal_s:<7} | {outp_s:<8} | {turn_s:<8}")

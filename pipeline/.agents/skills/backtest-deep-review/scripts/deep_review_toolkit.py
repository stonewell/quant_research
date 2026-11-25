#!/usr/bin/env python3
"""Reusable offline white-box audit toolkit & CLI for the `backtest-deep-review` skill.

Provides 6 composable quantitative audit helpers plus a zero-network CLI:
1. `verify_rule10_weight_diff` — Rule 10 discriminating power & zero-delta detector
2. `evaluate_option_b_gate` — §2.21 Option B paired 18-fold gate & DSR verifier
3. `compute_cash_normalized_metrics` — Cash-normalized CAGR/MaxDD & cash-drag severity
4. `audit_lot_rounding_realism` — A-share 100-share lot-truncation & distortion auditor
5. `summarize_stage_waterfall` — 9-stage exposure waterfall & primary bottleneck detector
6. `compute_superstar_and_group_attribution` — Superstar skew, group PnL & chronic losers

Run from repo root via:
  pipeline/.venv/bin/python3 pipeline/.agents/skills/backtest-deep-review/scripts/deep_review_toolkit.py --demo-synthetic
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


def verify_rule10_weight_diff(
    w_candidate: pd.DataFrame,
    w_baseline: pd.DataFrame,
    probe_name: Optional[str] = None,
    probe_cand: Any = None,
    probe_base: Any = None,
    tol: float = 1e-6,
) -> Dict[str, Any]:
    """Compare two weight panels and enforce AGENTS.md Rule 10 discriminating power.

    A zero-delta arm (`n_changed_weight_cells == 0` and unchanged internal probe) is
    flagged as `UNADJUDICATED_ZERO_DELTA` so it is never misreported as 'inert'.
    """
    all_idx = w_candidate.index.union(w_baseline.index)
    all_cols = w_candidate.columns.union(w_baseline.columns)
    cand = w_candidate.reindex(index=all_idx, columns=all_cols).fillna(0.0)
    base = w_baseline.reindex(index=all_idx, columns=all_cols).fillna(0.0)
    diff = (cand - base).abs()

    if diff.empty:
        n_changed_cells = 0
        n_changed_rows = 0
        max_abs_diff = 0.0
        first_div_date: Optional[str] = None
    else:
        arr = diff.to_numpy(dtype=np.float64)
        n_changed_cells = int((arr > tol).sum())
        row_max = diff.max(axis=1)
        changed_rows = row_max[row_max > tol]
        n_changed_rows = int(len(changed_rows))
        max_abs_diff = round(float(arr.max()), 6)
        if n_changed_rows > 0:
            first_idx = changed_rows.index[0]
            first_div_date = (
                str(first_idx.date()) if hasattr(first_idx, "date") else str(first_idx)
            )
        else:
            first_div_date = None

    probe_differs = bool(probe_name is not None and probe_cand != probe_base)
    adjudicated = bool(n_changed_cells > 0 or probe_differs)
    return {
        "n_changed_weight_cells": n_changed_cells,
        "n_changed_rebalance_rows": n_changed_rows,
        "max_abs_weight_diff": max_abs_diff,
        "first_divergence_date": first_div_date,
        "probe_name": probe_name,
        "probe_cand": probe_cand,
        "probe_base": probe_base,
        "probe_differs": probe_differs,
        "adjudicated": adjudicated,
        "verdict": "ADJUDICATED" if adjudicated else "UNADJUDICATED_ZERO_DELTA",
    }


def evaluate_option_b_gate(
    cand_fold_sharpes: Sequence[float],
    base_fold_sharpes: Sequence[float],
    dsr_val: float,
    min_delta_sharpe: float = 0.15,
    min_positive_folds: int = 14,
    min_paired_t: float = 2.0,
    min_dsr: float = 0.95,
) -> Dict[str, Any]:
    """Evaluate the §2.21 Option B paired multi-fold promotion gate."""
    if len(cand_fold_sharpes) != len(base_fold_sharpes) or len(cand_fold_sharpes) == 0:
        raise ValueError("cand_fold_sharpes and base_fold_sharpes must be non-empty and equal length")

    diffs = np.array(
        [float(c) - float(b) for c, b in zip(cand_fold_sharpes, base_fold_sharpes)],
        dtype=np.float64,
    )
    mean_delta = float(np.mean(diffs))
    pos_folds = int(np.sum(diffs > 0.0))
    total_folds = int(len(diffs))
    if total_folds >= 2:
        std_d = float(np.std(diffs, ddof=1))
        paired_t = float(mean_delta / (std_d / math.sqrt(total_folds))) if std_d > 1e-12 else (
            float("inf") if mean_delta > 0 else 0.0
        )
    else:
        paired_t = 0.0

    dsr_f = float(dsr_val)
    c_delta = bool(mean_delta >= min_delta_sharpe)
    c_folds = bool(pos_folds >= min_positive_folds)
    c_tstat = bool(paired_t >= min_paired_t)
    c_dsr = bool(dsr_f >= min_dsr)
    passed = bool(c_delta and c_folds and c_tstat and c_dsr)

    return {
        "mean_delta_sharpe": round(mean_delta, 4),
        "positive_folds": pos_folds,
        "total_folds": total_folds,
        "paired_t": round(paired_t, 4) if math.isfinite(paired_t) else paired_t,
        "dsr": round(dsr_f, 4),
        "delta_sharpe_passed": c_delta,
        "positive_folds_passed": c_folds,
        "paired_t_passed": c_tstat,
        "dsr_passed": c_dsr,
        "option_b_passed": passed,
    }


def compute_cash_normalized_metrics(
    raw_cagr: float,
    raw_max_dd: float,
    avg_invested_weight: float,
    exposure_floor: float = 0.10,
) -> Dict[str, Any]:
    """Compute cash-normalized CAGR, MaxDD, and cash-drag severity classification."""
    inv = float(avg_invested_weight)
    idle = max(0.0, 1.0 - inv)
    floor_applied = bool(inv < exposure_floor)
    denom = max(inv, float(exposure_floor))
    norm_cagr = float(raw_cagr) / denom
    norm_max_dd = abs(float(raw_max_dd)) / denom

    if idle <= 0.25:
        severity = "LOW"
    elif idle <= 0.50:
        severity = "MODERATE"
    else:
        severity = "HEAVY_CASH_DRAG"

    return {
        "avg_invested_weight": round(inv, 4),
        "idle_cash_weight": round(idle, 4),
        "effective_denom": round(denom, 4),
        "floor_applied": floor_applied,
        "raw_cagr": round(float(raw_cagr), 4),
        "raw_max_dd": round(abs(float(raw_max_dd)), 4),
        "norm_cagr": round(norm_cagr, 4),
        "norm_max_dd": round(norm_max_dd, 4),
        "cash_drag_severity": severity,
    }


def audit_lot_rounding_realism(
    target_weights: pd.DataFrame,
    close_prices: pd.DataFrame,
    portfolio_value: float = 200_000.0,
    lot_size: int = 100,
    distortion_thresh: float = 0.25,
    cash_proxy: str = "BIL",
) -> Dict[str, Any]:
    """Quantify A-share round-lot drop rate (< 1 lot) and >25% weight distortion."""
    rebal = target_weights.dropna(how="all")
    n_positive = 0
    n_dropped = 0
    n_distorted = 0
    uninvested_drags: List[float] = []
    dropped_counts: Dict[str, int] = {}

    for dt, row in rebal.iterrows():
        if dt not in close_prices.index:
            continue
        row_drag = 0.0
        has_pos = False
        for sym, wt in row.items():
            if sym == cash_proxy or pd.isna(wt):
                continue
            w_val = float(wt)
            if w_val <= 1e-6 or sym not in close_prices.columns:
                continue
            px = close_prices.loc[dt, sym]
            if pd.isna(px) or float(px) <= 0.0:
                continue
            px_f = float(px)
            has_pos = True
            n_positive += 1
            target_notional = w_val * portfolio_value
            one_lot_cost = float(lot_size) * px_f
            lot_shares = int(target_notional // one_lot_cost) * int(lot_size)
            delivered_weight = (lot_shares * px_f) / portfolio_value
            rel_distortion = abs(delivered_weight - w_val) / w_val
            row_drag += max(0.0, w_val - delivered_weight)
            if lot_shares == 0:
                n_dropped += 1
                dropped_counts[str(sym)] = dropped_counts.get(str(sym), 0) + 1
            elif rel_distortion > distortion_thresh:
                n_distorted += 1
        if has_pos:
            uninvested_drags.append(row_drag)

    worst_dropped = sorted(dropped_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {
        "portfolio_value": float(portfolio_value),
        "lot_size": int(lot_size),
        "n_positive_target_cells": n_positive,
        "n_dropped_below_1_lot": n_dropped,
        "drop_rate_pct": round(100.0 * n_dropped / max(n_positive, 1), 2),
        "n_distorted_above_thresh": n_distorted,
        "distortion_rate_pct": round(100.0 * n_distorted / max(n_positive, 1), 2),
        "mean_uninvested_lot_drag_pct": round(
            100.0 * float(np.mean(uninvested_drags)), 2
        ) if uninvested_drags else 0.0,
        "worst_dropped_symbols": [{"symbol": s, "drops": c} for s, c in worst_dropped],
    }


def summarize_stage_waterfall(
    stage_means_a: Dict[str, float],
    stage_means_b: Dict[str, float],
    label_a: str = "N=20",
    label_b: str = "N=100",
) -> Dict[str, Any]:
    """Compare ordered stage-by-stage exposure waterfalls and pinpoint the bottleneck."""
    ordered_stages = list(dict.fromkeys(list(stage_means_b.keys()) + list(stage_means_a.keys())))
    rows: List[Dict[str, Any]] = []
    prev_a: Optional[float] = None
    prev_b: Optional[float] = None
    worst_step_stage: Optional[str] = None
    worst_step_drop = 0.0

    for st in ordered_stages:
        va = float(stage_means_a.get(st, 0.0))
        vb = float(stage_means_b.get(st, 0.0))
        step_a = (va - prev_a) if prev_a is not None else 0.0
        step_b = (vb - prev_b) if prev_b is not None else 0.0
        cross_delta = vb - va
        rows.append({
            "stage": st,
            label_a: round(va, 4),
            label_b: round(vb, 4),
            "step_delta_a": round(step_a, 4),
            "step_delta_b": round(step_b, 4),
            "cross_delta_b_minus_a": round(cross_delta, 4),
        })
        if prev_b is not None and step_b < worst_step_drop:
            worst_step_drop = step_b
            worst_step_stage = st
        prev_a, prev_b = va, vb

    return {
        "label_a": label_a,
        "label_b": label_b,
        "stages": rows,
        "primary_bottleneck_stage": worst_step_stage,
        "primary_bottleneck_step_drop": round(worst_step_drop, 4),
    }


def compute_superstar_and_group_attribution(
    asset_pnl_rows: Sequence[Dict[str, Any]],
    group_map: Optional[Dict[str, str]] = None,
    total_universe_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Compute superstar winner skew, Core/Satellite group breakdown, and chronic losers."""
    normalized: List[Dict[str, Any]] = []
    for r in asset_pnl_rows:
        sym = str(r["symbol"])
        net_pnl = float(r.get("net_pnl", 0.0))
        buys = float(r.get("buys", r.get("gross_buys", 0.0)) or 0.0)
        costs = float(r.get("costs", r.get("friction", 0.0)) or 0.0)
        trades = int(r.get("trades", 0) or 0)
        roi_pct = float(r["roi_pct"]) if "roi_pct" in r else (
            100.0 * net_pnl / buys if buys > 1e-6 else 0.0
        )
        grp = (group_map.get(sym) if group_map else None) or str(r.get("bucket", "All"))
        normalized.append({
            "symbol": sym,
            "group": grp,
            "net_pnl": round(net_pnl, 2),
            "buys": round(buys, 2),
            "costs": round(costs, 2),
            "trades": trades,
            "roi_pct": round(roi_pct, 2),
        })

    normalized.sort(key=lambda x: x["net_pnl"], reverse=True)
    n_traded = len(normalized)
    n_untraded = (
        max(0, int(total_universe_size) - n_traded) if total_universe_size is not None else None
    )
    tot_pnl = sum(r["net_pnl"] for r in normalized)
    tot_costs = sum(r["costs"] for r in normalized)
    friction_bleed_pct = round(100.0 * tot_costs / abs(tot_pnl), 2) if abs(tot_pnl) > 1e-6 else 0.0

    top1 = normalized[0]["net_pnl"] if n_traded >= 1 else 0.0
    top3 = sum(r["net_pnl"] for r in normalized[:3])
    top5 = sum(r["net_pnl"] for r in normalized[:5])
    loo_ex_top1 = round(tot_pnl - top1, 2)

    chronic_losers = [
        r for r in sorted(normalized, key=lambda x: x["net_pnl"])
        if r["net_pnl"] < 0.0 and (
            r["net_pnl"] < -1000.0
            or r["roi_pct"] < -3.0
            or (r["costs"] > 0.5 * abs(r["net_pnl"]))
        )
    ]

    group_breakdown: Dict[str, Dict[str, Any]] = {}
    for g in sorted(set(r["group"] for r in normalized)):
        sub = [r for r in normalized if r["group"] == g]
        n_win = sum(1 for r in sub if r["net_pnl"] > 0.0)
        group_breakdown[g] = {
            "n_traded": len(sub),
            "n_winners": n_win,
            "win_rate_pct": round(100.0 * n_win / max(len(sub), 1), 1),
            "net_pnl": round(sum(r["net_pnl"] for r in sub), 2),
            "trades": sum(r["trades"] for r in sub),
            "costs": round(sum(r["costs"] for r in sub), 2),
        }

    return {
        "n_traded": n_traded,
        "n_untraded": n_untraded,
        "total_net_pnl": round(tot_pnl, 2),
        "total_costs": round(tot_costs, 2),
        "friction_bleed_pct": friction_bleed_pct,
        "top1_symbol": normalized[0]["symbol"] if normalized else None,
        "top1_share_pct": round(100.0 * top1 / tot_pnl, 1) if tot_pnl > 1e-6 else 0.0,
        "top3_share_pct": round(100.0 * top3 / tot_pnl, 1) if tot_pnl > 1e-6 else 0.0,
        "top5_share_pct": round(100.0 * top5 / tot_pnl, 1) if tot_pnl > 1e-6 else 0.0,
        "loo_ex_top1_pnl": loo_ex_top1,
        "loo_ex_top1_flips_negative": bool(tot_pnl > 0.0 and loo_ex_top1 <= 0.0),
        "chronic_losers": chronic_losers,
        "group_breakdown": group_breakdown,
    }


def _audit_json_rule10(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Inspect counterfactuals or exit-gate Rule 10 probes in a saved review JSON."""
    findings: List[Dict[str, Any]] = []
    if "section5_counterfactuals" in data:
        cfs = data["section5_counterfactuals"]
        for arm, info in cfs.items():
            if arm == "CF0":
                continue
            pr = info.get("rule10_probe", {})
            n_cells = int(pr.get("n_changed_weight_cells", 0) or 0)
            spec = str(info.get("specific_probe", ""))
            # Detect lifecycle-only counterfactuals (CF4) where weight matrix is identical by design
            # but backtester trade/turnover probe moved.
            is_bt_only = ("lifecycle" in str(info.get("desc", "")).lower()) and ("200k_turn=" in spec)
            adjudicated = bool(n_cells > 0 or is_bt_only)
            findings.append({
                "arm": arm,
                "n_changed_weight_cells": n_cells,
                "n_changed_rebalance_rows": int(pr.get("n_changed_rebalance_rows", 0) or 0),
                "max_abs_weight_diff": float(pr.get("max_abs_weight_diff", 0.0) or 0.0),
                "specific_probe": spec,
                "adjudicated": adjudicated,
                "verdict": "ADJUDICATED" if adjudicated else "UNADJUDICATED_ZERO_DELTA",
            })
    for tkey in ("track_a", "track_b"):
        if tkey in data and "exit_gates" in data[tkey]:
            probes = data[tkey]["exit_gates"].get("gate6_rule10_probes", {})
            for pname, pval in probes.items():
                n_cells = int(pval.get("n_changed_weight_cells", 0) or 0)
                adjudicated = n_cells > 0
                findings.append({
                    "arm": f"{tkey}:{pname}",
                    "n_changed_weight_cells": n_cells,
                    "n_changed_rebalance_rows": int(pval.get("n_changed_rebalance_rows", 0) or 0),
                    "max_abs_weight_diff": float(pval.get("max_abs_weight_diff", 0.0) or 0.0),
                    "specific_probe": f"first_div={pval.get('first_divergence_date')}",
                    "adjudicated": adjudicated,
                    "verdict": "ADJUDICATED" if adjudicated else "UNADJUDICATED_ZERO_DELTA",
                })
    return findings


def _run_demo_synthetic() -> str:
    dates = pd.bdate_range("2024-01-02", periods=6)
    syms = [f"SYM_{i:02d}" for i in range(10)]
    w_base = pd.DataFrame(0.05, index=dates, columns=syms)
    w_cand = w_base.copy()
    w_cand.iloc[2, 0] = 0.12
    w_zero = w_base.copy()

    r10_ok = verify_rule10_weight_diff(w_cand, w_base, "active_rank", 2, 4)
    r10_zero = verify_rule10_weight_diff(w_zero, w_base)
    opt_b = evaluate_option_b_gate([1.0] * 15 + [0.4] * 3, [0.7] * 18, dsr_val=0.98)
    cash_m = compute_cash_normalized_metrics(0.0619, 0.0766, 0.2705)
    prices = pd.DataFrame(
        {s: np.full(len(dates), 20.0 + 22.0 * i) for i, s in enumerate(syms)}, index=dates
    )
    lot_200k = audit_lot_rounding_realism(w_base, prices, portfolio_value=200_000.0)
    wf = summarize_stage_waterfall(
        {"6_post_vol_target": 0.6407, "7_post_entry_confirm": 0.5876},
        {"6_post_vol_target": 0.6388, "7_post_entry_confirm": 0.2802},
    )
    attr = compute_superstar_and_group_attribution(
        [
            {"symbol": "SYM_00", "net_pnl": 5000.0, "buys": 40000.0, "costs": 120.0, "trades": 6},
            {"symbol": "SYM_01", "net_pnl": 2500.0, "buys": 30000.0, "costs": 90.0, "trades": 4},
            {"symbol": "SYM_02", "net_pnl": -1400.0, "buys": 25000.0, "costs": 80.0, "trades": 5},
        ],
        group_map={"SYM_00": "Core", "SYM_01": "Core", "SYM_02": "Satellite"},
        total_universe_size=10,
    )

    return "\n".join([
        "# Backtest Deep-Review Synthetic Demo",
        "",
        f"- **Rule 10 (Changed)**: `{r10_ok['verdict']}` (`cells={r10_ok['n_changed_weight_cells']}`, `first_div={r10_ok['first_divergence_date']}`)",
        f"- **Rule 10 (Zero-Delta)**: `{r10_zero['verdict']}` (`adjudicated={r10_zero['adjudicated']}`)",
        f"- **Option B Gate**: `passed={opt_b['option_b_passed']}` (`delta={opt_b['mean_delta_sharpe']:+.4f}`, `{opt_b['positive_folds']}/{opt_b['total_folds']}`, `t={opt_b['paired_t']:.2f}`)",
        f"- **Cash-Normalized Metrics**: `norm_cagr={cash_m['norm_cagr']:.2%}`, `norm_max_dd={cash_m['norm_max_dd']:.2%}`, `severity={cash_m['cash_drag_severity']}`",
        f"- **200k Lot Rounding**: `drop_rate={lot_200k['drop_rate_pct']}%`, `distortion={lot_200k['distortion_rate_pct']}%`",
        f"- **Waterfall Bottleneck**: `{wf['primary_bottleneck_stage']}` (`step_drop={wf['primary_bottleneck_step_drop']:+.2%}`)",
        f"- **Attribution**: `top1_share={attr['top1_share_pct']}%`, `chronic_losers={len(attr['chronic_losers'])}`, `groups={list(attr['group_breakdown'].keys())}`",
    ])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest Deep-Review Offline Audit Toolkit")
    parser.add_argument("--demo-synthetic", action="store_true", help="Run 100% offline synthetic demo")
    parser.add_argument("--summarize-json", type=str, default=None, help="Summarize a saved deep-review JSON")
    parser.add_argument("--check-rule10", type=str, default=None, help="Audit Rule 10 discriminating power in JSON")
    args = parser.parse_args(argv)

    if args.demo_synthetic:
        print(_run_demo_synthetic())
        return 0

    target = args.check_rule10 or args.summarize_json
    if not target:
        parser.print_help(sys.stderr)
        return 1
    if not os.path.isfile(target):
        print(f"Error: JSON file not found: {target}", file=sys.stderr)
        return 1

    with open(target, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.check_rule10:
        findings = _audit_json_rule10(data)
        print(f"# Rule 10 Discriminating Power Audit (`{os.path.basename(target)}`)")
        print("")
        print("| Arm / Comparison | Changed Cells | Changed Rows | Max |dw| | Verdict | Probe |")
        print("| :--- | :---: | :---: | :---: | :---: | :--- |")
        for row in findings:
            print(
                f"| `{row['arm']}` | {row['n_changed_weight_cells']} | {row['n_changed_rebalance_rows']} | "
                f"{row['max_abs_weight_diff']:.4f} | **{row['verdict']}** | `{row['specific_probe']}` |"
            )
        return 0

    # --summarize-json
    print(f"# Deep-Review JSON Summary (`{os.path.basename(target)}`)")
    print("")
    if "section1_waterfall" in data:
        wf = summarize_stage_waterfall(
            data["section1_waterfall"]["trace_n20"]["stage_mean_exposure"],
            data["section1_waterfall"]["trace_n100"]["stage_mean_exposure"],
        )
        a100 = data["section3_audit_n100"]
        print(f"- **Primary Bottleneck**: `{wf['primary_bottleneck_stage']}` (`step_drop={wf['primary_bottleneck_step_drop']:+.2%}`)")
        print(f"- **N=100 Cash-Normalized**: `raw_cagr={a100['raw_cagr_pct']}% -> norm={a100['cash_normalized_cagr_pct']}%`, `raw_mdd={a100['raw_max_dd_pct']}% -> norm={a100['cash_normalized_max_dd_pct']}%`, `ERS={a100['ers']}`")
        print(f"- **200k Lot Rounding**: `drop_rate={data['section4_lot_realism']['200k']['lot_audit']['dropped_pct']}%`, `200k SR={data['section4_lot_realism']['200k']['sharpe']}`")
    for tkey in ("track_a", "track_b"):
        if tkey in data:
            tr = data[tkey]
            eg = tr["exit_gates"]
            print(
                f"- **{tr['track']}** (Winner `{tr['best_candidate_arm']}`): verdict=**{eg['overall_verdict']}**, "
                f"OptionB_vs_base=`{eg['gate1_option_b_vs_baseline_ms7']['option_b_passed']}`, "
                f"ERS=`{eg['gate2_single_winner_ers']['percentile']}` (`trusted={eg['gate2_single_winner_ers']['trusted']}`)"
            )
    findings = _audit_json_rule10(data)
    unadj = [r["arm"] for r in findings if not r["adjudicated"]]
    print(f"- **Rule 10 Audit**: {len(findings)} comparisons checked; UNADJUDICATED_ZERO_DELTA = `{unadj}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

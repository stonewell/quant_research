#!/usr/bin/env python3
"""`L2.49` scoping -- MEASURE the annualised friction rate directly (no turnover convention).

Why this script exists
----------------------
Two conventions are floating around the repo for turning turnover into friction, and they
differ by a factor of two:

  * `docs/version.md` §16.15.13: friction = **one-sided** turnover x 12.0817 bp
    ("稳态摩擦 85.26 bp/年" at 单边 7.0567x, 毛额 98.83 bp/年 at 8.1804x).
  * `FORENSIC_AUDIT.md` §6.4 / `L2.51` item 3: friction = **two-sided** turnover x
    (10-12.5) bp ("1.4%-1.9%/年" for 9.74-15.09x runs).

`total_turnover` accumulates `trade_val` for BOTH sells and buys
(`common/allocation_backtester.py:1803` and `:1825` -> `total_turnover += notional / equity`
at `:1828`), and `total_cost / trade_value` is a per-leg-VALUE rate, so the two-sided
reading is the dimensionally correct one. But rather than argue, measure the cost
directly against equity and time -- that number owes nothing to either convention:

    friction_bp_per_yr(fold) = sum(total_cost) / mean(portfolio_equity of the fold's rows)
                               / ((end_date - start_date).days / 365.25)

then average over folds (same per-fold-then-mean discipline as
`run_audit.turnover_annualised_from_folds`, so the two numbers share no aggregation bias).
Cross-checks emitted alongside:
  * `total_cost / sum(trade_value)` (bp) -- the per-leg-value cost rate,
  * `sum(trade_value) / mean(equity)` per fold -- the turnover measured from the ROWS
    (vs the engine's own `total_turnover` in `walkforward_report.csv`),
  * the two competing conversions, so the gap is explicit.

Read-only; no network; no DB.

USAGE
    pipeline/.venv/bin/python friction_rate.py
"""

from __future__ import annotations

import glob
import json
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
# 4 levels up (script lives in `l2_49_scope/_provenance/` since 2026-10-29): repo root.
SCOPE = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
B17 = os.path.join(SCOPE, "pipeline", "results", "blend_17_baseline")


def load_folds() -> pd.DataFrame:
    """One row per (batch, strategy, fold): cost, notional, equity, span."""
    rows = []
    for reb_path in sorted(glob.glob(os.path.join(B17, "[12]", "*", "walkforward_rebalances.csv"))):
        strat = reb_path.split(os.sep)[-2].replace("_strategy", "")
        batch = reb_path.split(os.sep)[-3]
        d = pd.read_csv(reb_path)
        rep_path = os.path.join(os.path.dirname(reb_path), "walkforward_report.csv")
        rep = pd.read_csv(rep_path)
        # `walkforward_report.csv` has NO `fold` column: row order == fold order 1..n
        # (verified against `batch_folds.csv`).
        fold_of_row = {i + 1: i for i in range(len(rep))}
        for fid, g in d.groupby("fold"):
            if fid not in fold_of_row:
                continue
            r = rep.iloc[fold_of_row[fid]]
            days = (pd.to_datetime(r["end_date"]) - pd.to_datetime(r["start_date"])).days
            if days <= 0:
                continue
            mean_eq = float(g["portfolio_equity"].mean())
            rows.append({
                "batch": batch,
                "strategy": strat,
                "fold": int(fid),
                "cal_days": days,
                "years": days / 365.25,
                "total_cost": float(g["total_cost"].sum()),
                "trade_value": float(g["trade_value"].sum()),
                "mean_equity": mean_eq,
                "engine_turnover": float(r["total_turnover"]),
                "cagr": float(r["cagr"]),
            })
    f = pd.DataFrame(rows)
    f["friction_bp_yr"] = (f["total_cost"] / f["mean_equity"] / f["years"] * 1e4)
    f["rows_turnover_2s"] = f["trade_value"] / f["mean_equity"]          # two-sided, from rows
    f["rows_turnover_ann"] = f["rows_turnover_2s"] / f["years"]
    f["cost_rate_bp"] = f["total_cost"] / f["trade_value"] * 1e4
    return f


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    f = load_folds()
    f.to_csv(os.path.join(OUT, "friction_per_fold.csv"), index=False)

    g = f.groupby("strategy")
    pooled = {
        "n_folds": int(len(f)),
        "n_folds_per_strategy": g.size().to_dict(),
        # authoritative, convention-free
        "friction_bp_yr_mean_over_folds": float(f["friction_bp_yr"].mean()),
        "friction_bp_yr_median": float(f["friction_bp_yr"].median()),
        "friction_bp_yr_by_strategy": g["friction_bp_yr"].mean().to_dict(),
        # the row-level cost rate (per unit of executed trade value)
        "cost_rate_bp_by_strategy": g["cost_rate_bp"].mean().to_dict(),
        "cost_rate_bp_all": float(f["total_cost"].sum() / f["trade_value"].sum() * 1e4),
        # turnover, measured two ways
        "engine_turnover_ann_mean_over_folds": float(
            (f["engine_turnover"] / f["years"]).mean()),
        "rows_turnover_ann_mean_over_folds": float(f["rows_turnover_ann"].mean()),
        "rows_over_engine_ratio": float(
            (f["rows_turnover_2s"] / f["engine_turnover"]).mean()),
        # the two competing conversions of THE SAME turnover number
        "conv_one_sided_x_cost_rate": float(
            (f["engine_turnover"] / f["years"]).mean() / 2.0 * f["cost_rate_bp"].mean()
            if False else 0.0),
    }
    mean_x = pooled["engine_turnover_ann_mean_over_folds"]
    cr = pooled["cost_rate_bp_all"]
    pooled["conv_one_sided_x_cost_rate"] = mean_x / 2.0 * cr
    pooled["conv_two_sided_x_cost_rate"] = mean_x * cr
    pooled["measured_equals_two_sided"] = bool(
        abs(pooled["friction_bp_yr_mean_over_folds"] - mean_x * cr) < 0.05 * mean_x * cr)
    with open(os.path.join(OUT, "friction_rate.json"), "w", encoding="utf-8") as fh:
        json.dump(pooled, fh, ensure_ascii=False, indent=2)
    print(json.dumps(pooled, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

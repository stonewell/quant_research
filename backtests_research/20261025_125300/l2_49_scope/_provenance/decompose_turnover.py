#!/usr/bin/env python3
"""`L2.49` scoping phase -- turnover decomposition (measurement only, zero backtests).

WHY THIS FILE EXISTS
--------------------
`L2.49` is blocked on one question that no existing artifact answers: the 16.36x/yr
gross two-sided turnover is a *total*, and every reduction lever that has so far been
falsified (`L2.36` 2-bar exit confirmation; `§4.18` cooldown extension) was falsified
against a *slice* of that total. Before any further lever is proposed, the total has to
be split into pieces that are attributable to distinct mechanical causes:

  (1) fold-initial allocation      -- universe construction, not a decision
  (2) re-entry after a full exit   -- the `L2.52` round-trip pump (already measured there)
  (3) fresh entry of a new name    -- basket composition change
  (4) full exit                    -- risk / signal sell
  (5) trim / add of a held name    -- *within* this bucket we separate:
        (5a) signal   -- the target weight itself moved  (T_new - T_prev)
        (5b) drift    -- the target did NOT move, the *actual* weight drifted away
                         from it through price/cash movement and is snapped back
                         (T_prev - prior_weight)

The (5a)/(5b) split is the whole point. `apply_asset_inertia` gates on
``|ideal_w - current_w|`` where ``current_w`` is the **previous target**, not the drifted
actual weight (`common/allocation_templates.py:217-340`): the inertia gate is a no-trade
band on *target changes*, so (5b) is **not** covered by `min_weight_change` today. Every
existing falsification (inertia arms in `09_param_grid`, the `L2.36` k=2 set) acted on
the *signal* axis. If (5b) is large, it is the only untouched axis left.

METHOD (exact, no approximation in the split itself)
---------------------------------------------------
`walkforward_rebalances.csv` carries `prior_weight` (the *drifted* actual weight, built
from `drifted_w` at `common/allocation_backtester.py:2154`) and `target_weight` (the
instruction). For each (fold, symbol) the rows are ordered; the target in force *before*
row k is the `target_weight` of row k-1 for that symbol (0.0 if none), because a target
only changes when the symbol trades -- the sparse-weights contract freezes it otherwise.
Then

    delta_w  = target_weight[k] - prior_weight[k]                      (CSV)
    T_prev   = target_weight[k-1]  (per fold,symbol; 0.0 if first row)
    signal_w = target_weight[k] - T_prev
    drift_w  = T_prev - prior_weight[k]
    signal_w + drift_w == delta_w                                      (identity)

The realized `trade_value` (which is lot-rounded and cash-capped, so it is NOT
``delta_w * equity``) is split *pro rata* by ``signal_w/delta_w`` and ``drift_w/delta_w``,
so the two parts still sum exactly to the executed notional.

DISCLOSED RESIDUALS (see memo §7)
---------------------------------
* A target that changed `min_weight_change`-wise but produced no fill (below `min_shares`,
  cash cap, suspension) is invisible in the CSV; the next fill's ``drift_w`` then absorbs
  that hidden target change, **over**-stating (5b). Bound is given in the memo.
* Lot rounding means ``sum|delta_w| <= total_turnover`` in general; the memo anchors the
  annualized x/yr numbers on the official `total_turnover` series
  (`batch_folds.csv` / `walkforward_report.csv`) and applies the shares measured here,
  which keeps the decomposition consistent with the 16.3608x baseline convention.

USAGE (offline, read-only, no DB access needed -- CSVs only)
    pipeline/.venv/bin/python decompose_turnover.py --out out
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

# 4 levels up: `l2_49_scope/_provenance/` -> `l2_49_scope` -> `20261025_125300`
# -> `backtests_research` -> repo root. (Was 3 before the script moved into
# `_provenance/` on 2026-10-29; scripts stay runnable from the repo root.)
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
B17 = os.path.join(REPO, "pipeline", "results", "blend_17_baseline")
SCOPE = os.path.join(REPO, "backtests_research", "20261025_125300")

EPS = 1e-12
TOL_ZERO = 1e-9


def load_b17() -> pd.DataFrame:
    frames = []
    for p in sorted(glob.glob(os.path.join(B17, "*", "*", "walkforward_rebalances.csv"))):
        df = pd.read_csv(p)
        df["batch"] = p.split(os.sep)[-3]
        df["strategy"] = p.split(os.sep)[-2].replace("_strategy", "")
        df["run"] = "blend_17_baseline"
        frames.append(df)
    if not frames:
        raise SystemExit(f"no blend_17_baseline CSVs under {B17}")
    return pd.concat(frames, ignore_index=True)


def load_scope_dirs() -> Dict[str, str]:
    """name -> run dir, for the L2.52 primary-scope cross-check."""
    out = {}
    for name in ("01_r1_longwin", "03_r2b_oos", "07_r3_oos_bear"):
        out[name] = os.path.join(SCOPE, name)
    out["08_ablate_wf/baseline"] = os.path.join(SCOPE, "08_ablate_wf", "baseline")
    return out


def load_run(run_dir: str, run_name: str) -> pd.DataFrame:
    frames = []
    for p in sorted(glob.glob(os.path.join(run_dir, "*", "walkforward_rebalances.csv"))):
        df = pd.read_csv(p)
        df["strategy"] = p.split(os.sep)[-2]
        df["batch"] = "na"
        df["run"] = run_name
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """Attach T_prev / signal_w / drift_w / bucket to every trade row."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    # DISCLOSED GOTCHA: `fold` restarts at 1 for batch 2, so `fold` alone is NOT a key --
    # keying on it silently stacks batch-1 fold 1 on batch-2 fold 1 and corrupts both the
    # fold-initial-allocation mark and the T_prev chain. `batch` is part of the key.
    df = df.sort_values(["run", "batch", "strategy", "fold", "date", "rebalance_id", "symbol"],
                        kind="mergesort").reset_index(drop=True)

    key = ["run", "batch", "strategy", "fold", "symbol"]
    foldkey = ["run", "batch", "strategy", "fold"]
    df["T_prev"] = df.groupby(key, sort=False)["target_weight"].shift(1)
    df["first_seen"] = df["T_prev"].isna()
    df["T_prev"] = df["T_prev"].fillna(0.0)

    df["signal_w"] = df["target_weight"] - df["T_prev"]
    df["drift_w"] = df["T_prev"] - df["prior_weight"]
    # identity check
    resid = (df["signal_w"] + df["drift_w"] - df["weight_change"]).abs().max()
    if resid > 1e-9:
        raise SystemExit(f"decomposition identity broken: max residual {resid:g}")

    dw = df["weight_change"]
    p = df["prior_weight"]
    t = df["target_weight"]
    is_entry = (p <= TOL_ZERO) & (t > TOL_ZERO)
    is_exit = (p > TOL_ZERO) & (t <= TOL_ZERO)

    # first trade date of each fold -> fold-initial allocation
    first_date = df.groupby(foldkey)["date"].transform("min")
    is_init = is_entry & (df["date"] == first_date)

    # re-entry = entry of a symbol that already had an *exit* row earlier in this fold
    df["_is_exit"] = is_exit
    # cumulative count of prior exits inside (fold,symbol)
    g = df.groupby(key, sort=False)
    df["_prior_exits"] = g["_is_exit"].cumsum() - df["_is_exit"].astype(int)
    is_reentry = is_entry & (~is_init) & (df["_prior_exits"] > 0)
    is_fresh = is_entry & (~is_init) & (~is_reentry)

    bucket = np.where(is_init, "1_init_entry",
             np.where(is_reentry, "2_reentry",
             np.where(is_fresh, "3_fresh_entry",
             np.where(is_exit, "4_exit",
             np.where((t > 0) & (t < p), "5_trim", "6_add")))))
    df["bucket"] = bucket

    # sign-corrected realized notional, then pro-rata split
    sgn = np.sign(dw)
    df["tv_signed"] = df["trade_value"] * sgn
    frac_sig = np.where(dw.abs() > EPS, df["signal_w"] / dw, 0.0)
    df["tv_signal"] = df["tv_signed"] * frac_sig
    df["tv_drift"] = df["tv_signed"] * (1.0 - frac_sig)

    # resize-only view (buckets 5/6): signal vs drift
    df["is_resize"] = df["bucket"].isin(["5_trim", "6_add"])
    # "pure drift snap-back" = resize row whose target did not move at all
    df["pure_drift"] = df["is_resize"] & (df["signal_w"].abs() <= 1e-7)
    df["drift_only_row"] = df["is_resize"] & (df["signal_w"].abs() > 1e-7) & (df["drift_w"].abs() > 1e-7)
    return df


def fold_years(df: pd.DataFrame) -> pd.DataFrame:
    """fold -> calendar years covered (for annualization of non-B17 runs)."""
    g = df.groupby(["run", "batch", "strategy", "fold"])["date"]
    f = pd.DataFrame({"start": g.min(), "end": g.max(), "n_days": g.nunique()})
    f["cal_days"] = (f["end"] - f["start"]).dt.days + 1
    return f.reset_index()


def official_turnover_b17() -> pd.DataFrame:
    frames = []
    for b in ("1", "2"):
        p = os.path.join(B17, b, "batch_folds.csv")
        if not os.path.exists(p):
            continue
        f = pd.read_csv(p)
        f["batch"] = b
        frames.append(f)
    d = pd.concat(frames, ignore_index=True)
    # map display name -> module-style strategy key used in the CSV paths
    name_map = {
        "Chan Single Crisis Shield Blend Strategy": "chan_crisis_shield_blend",
        "Chan Dual Hybrid Alpha Blend Strategy": "chan_dual_hybrid_blend",
        "Chan Four-State Risk-Managed Blend Strategy": "chan_four_state_blend",
        "Chan Four-State Shield Blend Strategy": "chan_four_state_shield_blend",
        "Chan Risk-Managed Blend Strategy": "chan_risk_managed_blend",
        "Generic Multi-Strategy Blend (Adaptive)": "generic_blend",
    }
    d["strategy"] = d["strategy"].map(name_map).fillna(d["strategy"])
    d["start"] = pd.to_datetime(d["start_date"])
    d["end"] = pd.to_datetime(d["end_date"])
    # CANONICAL convention (pipeline/.agents/skills/walkforward-audit/scripts/run_audit.py::
    # turnover_annualised_from_folds): per-fold annualise on `(end - start).days`, then mean.
    # The +1 variant is reported alongside in `ann_cal_plus1` so the difference is visible
    # rather than silently picked.
    d["cal_days"] = (d["end"] - d["start"]).dt.days
    d["ann_cal"] = d["total_turnover"] / (d["cal_days"] / 365.25)
    d["ann_cal_plus1"] = d["total_turnover"] / ((d["cal_days"] + 1) / 365.25)
    d["ann_63"] = d["total_turnover"] * (252.0 / 63.0)
    return d


def shares_table(df: pd.DataFrame) -> pd.DataFrame:
    """Weight-space notional per bucket per (run,strategy,fold), from the CSVs."""
    df = df.copy()
    df["abs_tv"] = df["trade_value"]
    rows = []
    for (run, batch, strat, fold), sub in df.groupby(["run", "batch", "strategy", "fold"], sort=False):
        base = sub["abs_tv"].sum()
        rec = {"run": run, "batch": batch, "strategy": strat, "fold": fold, "abs_tv_total": base,
               "n_rows": len(sub), "n_dates": sub["date"].nunique(),
               "abs_dw_total": sub["weight_change"].abs().sum()}
        for b, s in sub.groupby("bucket"):
            rec[f"tv_{b}"] = s["abs_tv"].sum()
            rec[f"n_{b}"] = len(s)
        rec["tv_signal_resize"] = sub.loc[sub["is_resize"], "tv_signal"].abs().sum()
        rec["tv_drift_resize"] = sub.loc[sub["is_resize"], "tv_drift"].abs().sum()
        rec["tv_pure_drift"] = sub.loc[sub["pure_drift"], "abs_tv"].sum()
        rec["n_pure_drift"] = int(sub["pure_drift"].sum())
        rows.append(rec)
    out = pd.DataFrame(rows).fillna(0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # ---------------------------------------------------------------- baseline
    b17 = classify(load_b17())
    b17.to_csv(os.path.join(args.out, "b17_rows_classified.csv"), index=False)
    off = official_turnover_b17()
    sh = shares_table(b17)

    # anchor: per (strategy,fold) official total_turnover; shares from CSV
    key = ["strategy", "fold"]
    m = off.merge(sh, on=["strategy", "fold", "batch"], how="inner", suffixes=("", "_csv"))
    if len(m) != len(sh) or len(m) != len(off):
        print(f"[warn] join mismatch: official={len(off)} csv_folds={len(sh)} matched={len(m)}",
              file=sys.stderr)

    share_cols = [c for c in m.columns if c.startswith("tv_") and c != "tv_signal_resize"
                  and c != "tv_drift_resize" and c != "tv_pure_drift"]
    total_share_abs = m["abs_tv_total"].sum()
    weighted = {}
    for c in share_cols:
        weighted[c] = float((m[c].sum()) / total_share_abs) if total_share_abs else 0.0
    # annualized x/yr per bucket = sum(bucket tv) / sum(abs_tv) * official ann mean
    ann_anchor = float(m["ann_cal"].mean())
    ann_63 = float(m["ann_63"].mean())
    decomp = {c.replace("tv_", ""): {"share": weighted[c], "x_per_yr": weighted[c] * ann_anchor}
              for c in share_cols}
    for k in ("signal_resize", "drift_resize", "pure_drift"):
        v = float(m[f"tv_{k}"].sum()) / total_share_abs if total_share_abs else 0.0
        decomp[k] = {"share": v, "x_per_yr": v * ann_anchor}

    # consistency of the CSV total against the official series
    ratio = float(m["abs_tv_total"].sum() / (m["total_turnover"] * 100_000.0).sum())

    summary = {
        "generated_by": "backtests_research/20261025_125300/l2_49_scope/decompose_turnover.py",
        "scope": "blend_17_baseline (108 folds, 6 strategies, batch 1 = 15 folds + batch 2 = 3 folds)",
        "n_rows": int(len(b17)),
        "n_folds": int(len(m)),
        "anchor_ann_cal_mean": ann_anchor,
        "anchor_ann_fixed63_mean": ann_63,
        "csv_abs_tv_over_official_notional": ratio,
        "buckets": decomp,
        "per_strategy": {},
        "n_pure_drift_rows": int(b17["pure_drift"].sum()),
        "resize_rows": int(b17["is_resize"].sum()),
        "drift_only_rows": int(b17["drift_only_row"].sum()),
        "checks": {
            "max_abs_dw": float(b17["weight_change"].abs().max()),
            "n_symbols": int(b17["symbol"].nunique()),
            "symbols": sorted(b17["symbol"].unique().tolist()),
            "min_resize_abs_dw": float(b17.loc[b17["is_resize"], "weight_change"].abs().min()),
        },
    }

    for strat, sub in b17.groupby("strategy"):
        ss = sh[sh["strategy"] == strat]
        tot = ss["abs_tv_total"].sum()
        o = off[off["strategy"] == strat]
        summary["per_strategy"][strat] = {
            "ann_cal": float(o["ann_cal"].mean()),
            "share_init": float(ss.get("tv_1_init_entry", pd.Series(dtype=float)).sum() / tot),
            "share_reentry": float(ss.get("tv_2_reentry", pd.Series(dtype=float)).sum() / tot),
            "share_fresh": float(ss.get("tv_3_fresh_entry", pd.Series(dtype=float)).sum() / tot),
            "share_exit": float(ss.get("tv_4_exit", pd.Series(dtype=float)).sum() / tot),
            "share_trim": float(ss.get("tv_5_trim", pd.Series(dtype=float)).sum() / tot),
            "share_add": float(ss.get("tv_6_add", pd.Series(dtype=float)).sum() / tot),
            "share_drift_resize": float(ss["tv_drift_resize"].sum() / tot),
            "share_signal_resize": float(ss["tv_signal_resize"].sum() / tot),
            "share_pure_drift": float(ss["tv_pure_drift"].sum() / tot),
        }

    # ------------------------------------------------------- L2.52 cross-check
    cross = {}
    for run_name, rd in load_scope_dirs().items():
        if not os.path.isdir(rd):
            continue
        d = load_run(rd, run_name)
        if d.empty:
            continue
        d = classify(d)
        fy = fold_years(d)
        d = d.merge(fy[["run", "batch", "strategy", "fold", "cal_days"]],
                    on=["run", "batch", "strategy", "fold"], how="left")
        # per fold: turnover multiple = sum|tv| / fold start equity (use portfolio_equity at first row)
        eq0 = d.groupby(["run", "batch", "strategy", "fold"])["portfolio_equity"].transform("first")
        d["tv_mult"] = d["trade_value"] / eq0
        # per (strategy,fold,bucket) -> annualized bucket turnover, then average over folds
        per = d.groupby(["batch", "strategy", "fold", "bucket"])["tv_mult"].sum().reset_index()
        per = per.merge(fy, on=["batch", "strategy", "fold"], how="left")
        per["ann"] = per["tv_mult"] / (per["cal_days"] / 365.25)
        tot = per.groupby(["batch", "strategy", "fold"])["ann"].sum().mean()
        # mean of PER-FOLD shares (sums to 1 by construction); see lever_ceilings.py for the
        # missing-bucket inflation this replaces.
        wide = (per.pivot_table(index=["batch", "strategy", "fold"], columns="bucket",
                                values="tv_mult", aggfunc="sum").fillna(0.0))
        shm = wide.div(wide.sum(axis=1), axis=0).mean()
        cross[run_name] = {"total_ann_x": float(tot),
                           "buckets": {k: float(v * tot) for k, v in shm.items()},
                           "shares": {k: float(v) for k, v in shm.items()},
                           "n_rows": int(len(d))}

    summary["cross_check_scope"] = cross
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=float)

    sh.to_csv(os.path.join(args.out, "per_fold_shares.csv"), index=False)
    m.to_csv(os.path.join(args.out, "per_fold_anchored.csv"), index=False)

    print(json.dumps({k: summary[k] for k in
                      ("n_rows", "n_folds", "anchor_ann_cal_mean", "anchor_ann_fixed63_mean",
                       "csv_abs_tv_over_official_notional")}, ensure_ascii=False, indent=2))
    print("\nBUCKET DECOMPOSITION (share of |trade_value|, annualized x/yr on the "
          f"{ann_anchor:.4f}x anchor)")
    for k, v in sorted(decomp.items(), key=lambda kv: -kv[1]["share"]):
        print(f"  {k:16s} share={v['share']:.4f}  x/yr={v['x_per_yr']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

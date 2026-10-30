#!/usr/bin/env python3
"""`L2.49` scoping phase -- lever ceiling arithmetic (measurement only).

Companion to `decompose_turnover.py`. Every number produced here is a *ceiling*: "how
many x/yr of gross two-sided turnover could this lever mechanically touch if it worked
perfectly and cost nothing", measured on already-executed trade records. No backtest is
run, no strategy code is touched, nothing is downloaded.

The ceilings are deliberately generous:
  * no alpha loss is charged to the lever (that is the *net* test, and `09_param_grid`
    already ran it for the band family -- see the memo's §5.4);
  * the merge/"coarse-graining" estimate uses the realized signal path, i.e. it assumes
    the trade you skip is exactly re-placed by the next one (first-order; disclosed).

USAGE
    pipeline/.venv/bin/python lever_ceilings.py --out out
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decompose_turnover as dt  # noqa: E402

SCOPE = dt.SCOPE
B17 = dt.B17



def fold_spans(pattern: str) -> pd.DataFrame:
    """(strategy, fold) -> calendar days, taken from `walkforward_report.csv`.

    WHY NOT from the rebalances CSV: `max(date) - min(date)` over *fill* dates
    collapses to a few weeks in arms that trade rarely (a 63-bar fold with 6 fills
    looks like a 6-week fold), which then divides the same turnover by a much smaller
    year fraction and INFLATES the annualized number by up to 4x. That artefact made
    an early draft of this script report 40.85x/yr for an arm whose realized sum of
    turnover fell 54.7% -- the two were not even in the same direction. The fold span
    must come from the walkforward fold definition, never from the fill dates.
    """
    rows = []
    for p in sorted(glob.glob(pattern)):
        rep = pd.read_csv(p)
        st = p.split(os.sep)[-2]
        # DISCLOSED GOTCHA: `walkforward_report.csv` has NO `fold` column -- folds are
        # row order, 1..n. Verified against `pipeline/results/blend_17_baseline/*/batch_folds.csv`
        # (which does carry `fold`): start_date matches row order exactly, batch 1 and 2.
        for i, r in enumerate(rep.itertuples(), start=1):
            rows.append({"strategy": st, "fold": i,
                         "start": r.start_date, "end": r.end_date,
                         "span_days": (pd.to_datetime(r.end_date)
                                       - pd.to_datetime(r.start_date)).days,
                         "total_turnover": float(r.total_turnover)})
    return pd.DataFrame(rows)


def bucket_table(d: pd.DataFrame, off: pd.DataFrame, out: dict) -> pd.DataFrame:
    """bucket share of |trade_value| per strategy, plus x/yr on the official anchor."""
    rows = []
    for strat, sub in d.groupby("strategy"):
        o = off[off["strategy"] == strat]
        ann = float(o["ann_cal"].mean())
        tot = sub["trade_value"].sum()
        for b, s in sub.groupby("bucket"):
            rows.append({"strategy": strat, "bucket": b, "n": len(s),
                         "notional": float(s["trade_value"].sum()),
                         "share": float(s["trade_value"].sum() / tot),
                         "x_per_yr": float(s["trade_value"].sum() / tot * ann),
                         "ann_x": ann})
    t = pd.DataFrame(rows)
    out["per_strategy_buckets"] = t.to_dict(orient="records")
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    d = dt.classify(dt.load_b17())
    off = dt.official_turnover_b17()
    res: dict = {"generated_by": "backtests_research/20261025_125300/l2_49_scope/lever_ceilings.py"}
    ann_all = float(off["ann_cal"].mean())
    tot_tv = float(d["trade_value"].sum())
    res["ann_cal_mean_all"] = ann_all
    res["n_rows"] = int(len(d))
    res["n_folds"] = int(d.groupby(["strategy", "batch", "fold"]).ngroups)

    tb = bucket_table(d, off, res)
    tb.to_csv(os.path.join(args.out, "bucket_by_strategy.csv"), index=False)

    # ---- 1. drift magnitude: is the drift axis even there? ------------------
    rz = d[d["is_resize"]]
    dr = rz["drift_w"].abs()
    res["drift_magnitude"] = {
        "n_resize_rows": int(len(rz)),
        "resize_notional_share_of_total": float(rz["trade_value"].sum() / tot_tv),
        "median_abs_drift_w": float(dr.median()),
        "mean_abs_drift_w": float(dr.mean()),
        "p90_abs_drift_w": float(dr.quantile(0.90)),
        # signal-vs-drift magnitude on the SAME rows: this is the structural reason a
        # drift band cannot reach the gate -- drift is ~an order of magnitude smaller
        # per event than the signal it is bundled with.
        "median_abs_signal_w": float(rz["signal_w"].abs().median()),
        "mean_abs_signal_w": float(rz["signal_w"].abs().mean()),
        "median_abs_delta_w": float(rz["weight_change"].abs().median()),
        "median_abs_drift_over_signal": float((dr / rz["signal_w"].abs()).median()),
        "notional_share_of_resize_with_drift_gt": {
            str(th): float(rz.loc[dr > th, "trade_value"].sum() / rz["trade_value"].sum())
            for th in (0.005, 0.01, 0.02, 0.03, 0.05)
        },
        "drift_component_share_of_total": float(rz["tv_drift"].abs().sum() / tot_tv),
        "drift_component_x_per_yr": float(rz["tv_drift"].abs().sum() / tot_tv * ann_all),
        "signal_component_share_of_resize": float(rz["tv_signal"].abs().sum()
                                                  / (rz["tv_signal"].abs().sum()
                                                     + rz["tv_drift"].abs().sum())),
        "obs_note": "|drift_w| = |T_prev - prior_weight|; a size-gated drift band can only "
                    "remove turnover that BOTH exceeds the band AND is drift (not signal).",
    }

    # ---- 2. per-asset |delta_w| histogram (band family reach) ---------------
    bins = [0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.25, 1.01]
    d["dwbin"] = pd.cut(d["weight_change"].abs(), bins=bins, right=False)
    hist = (d.groupby(["bucket", "dwbin"], observed=True)
             .agg(n=("trade_value", "size"), notional=("trade_value", "sum"))
             .reset_index())
    hist["dwbin"] = hist["dwbin"].astype(str)
    hist["share_of_total"] = hist["notional"] / tot_tv
    hist["x_per_yr"] = hist["share_of_total"] * ann_all
    hist.to_csv(os.path.join(args.out, "delta_w_histogram.csv"), index=False)
    res["delta_w_histogram"] = hist.to_dict(orient="records")

    # reach of a *widened* per-asset band: notional whose |dw| sits below the new band
    def reach_below(th: float) -> dict:
        s = d[d["weight_change"].abs() < th]
        return {"th": th, "n": int(len(s)),
                "share_of_total": float(s["trade_value"].sum() / tot_tv),
                "x_per_yr": float(s["trade_value"].sum() / tot_tv * ann_all),
                "share_of_total_sells": float(
                    s[s["weight_change"] < 0]["trade_value"].sum()
                    / d[d["weight_change"] < 0]["trade_value"].sum()),
                "share_of_total_buys": float(
                    s[s["weight_change"] > 0]["trade_value"].sum()
                    / d[d["weight_change"] > 0]["trade_value"].sum())}
    res["band_widening_reach"] = [reach_below(x) for x in (0.05, 0.06, 0.08, 0.10, 0.15)]

    # ---- 3. asymmetric sell band: only cdhb is wired -------------------------
    res["asymmetric_sell_wiring"] = {
        "note": "blend_base.py:609 -> asymmetric_sells defaults to True ONLY for the "
                "'cdhb' prefix; sell band = thresh_t * asymmetric_sell_ratio (0.60). "
                "For the other five strategies the lever is not connected at all.",
        "per_strategy_trim_share": {},
    }
    trims = d[d["bucket"] == "5_trim"]
    for strat, sub in d.groupby("strategy"):
        t = trims[trims["strategy"] == strat]
        tot = sub["trade_value"].sum()
        rec = {"trim_share_of_strategy": float(t["trade_value"].sum() / tot),
               "trim_x_per_yr": float(t["trade_value"].sum() / tot * float(
                   off[off["strategy"] == strat]["ann_cal"].mean()))}
        for th in (0.03, 0.05, 0.08, 0.10):
            rec[f"trim_share_absdw_lt_{th}"] = float(
                t.loc[t["weight_change"].abs() < th, "trade_value"].sum() / tot)
        res["asymmetric_sell_wiring"]["per_strategy_trim_share"][strat] = rec

    # ---- 4. portfolio-level band: is the book-level object even non-degenerate?
    per_date = (d.groupby(["strategy", "batch", "fold", "date"])
                 .agg(net_signal=("signal_w", "sum"),
                      gross_signal=("signal_w", lambda s: s.abs().sum()),
                      tv=("trade_value", "sum"))
                 .reset_index())
    per_date["net_over_gross"] = per_date["net_signal"].abs() / per_date["gross_signal"]
    res["portfolio_level_band"] = {
        "n_rebalance_dates": int(len(per_date)),
        "median_abs_net_signal_w": float(per_date["net_signal"].abs().median()),
        "p90_abs_net_signal_w": float(per_date["net_signal"].abs().quantile(0.90)),
        "median_gross_signal_w": float(per_date["gross_signal"].median()),
        "median_net_over_gross": float(per_date["net_over_gross"].median()),
        "share_of_notional_on_dates_with_abs_net_lt": {
            str(th): float(per_date.loc[per_date["net_signal"].abs() < th, "tv"].sum()
                           / per_date["tv"].sum())
            for th in (0.01, 0.02, 0.05, 0.10)},
        "obs_note": "A ~fully-invested book has sum(target) pinned at the exposure target, "
                    "so the NET book-level weight change is small by construction while the "
                    "GROSS per-asset change is 10-50x larger. A portfolio-level band therefore "
                    "either never triggers or suppresses every rebalance date wholesale -- it "
                    "cannot separate churn from repositioning.",
    }

    # ---- 5. coarse-graining / rebalance-frequency ceiling -------------------
    # per (fold,symbol) sequence of events; skip every 2nd (or 2 of 3) event.
    pairs = []
    for k in (2, 3):
        kept, orig = 0.0, 0.0
        for _, s in d.groupby(["strategy", "batch", "fold", "symbol"], sort=False):
            sig = s["signal_w"].to_numpy()
            if len(sig) == 0:
                continue
            orig += np.abs(sig).sum()
            i = 0
            while i < len(sig):
                kept += abs(sig[i:i + k].sum())
                i += k
        pairs.append({"merge_k": k, "retained_share_of_signal": kept / orig if orig else None,
                      "x_per_yr_after": (kept / orig) * ann_all if orig else None})
    res["coarse_grain_ceiling"] = {
        "method": "merge k consecutive rebalance events per (fold,symbol); retained notional "
                  "= |sum of the merged signal_w| / sum|signal_w|. FIRST-ORDER estimate: it "
                  "assumes the skipped trade is re-placed exactly at the next event and that "
                  "the held weight at the merged event equals the realized one. It is an "
                  "upper bound on the saving (real coarse-graining also changes holdings).",
        "estimates": pairs,
    }

    # ---- 6. what the already-run ablations do to turnover -------------------
    abl = []
    for arm_dir in sorted(glob.glob(os.path.join(SCOPE, "08_ablate_wf", "*"))):
        arm = os.path.basename(arm_dir)
        sp = fold_spans(os.path.join(arm_dir, "*", "walkforward_report.csv"))
        for p in sorted(glob.glob(os.path.join(arm_dir, "*", "walkforward_rebalances.csv"))):
            df = pd.read_csv(p)
            strat = p.split(os.sep)[-2]
            df["m"] = df["trade_value"] / df["portfolio_equity"]
            g = df.groupby("fold")
            for fold, sub in g:
                # NB: never name this `d` -- it shadows the classified trade frame.
                hit = sp.loc[(sp["strategy"] == strat) & (sp["fold"] == fold)]
                if hit.empty or float(hit["span_days"].iloc[0]) <= 0:
                    continue
                abl.append({"arm": arm, "strategy": strat, "fold": fold,
                            # canonical: report `total_turnover` (already equity-normalised),
                            # annualised per fold on calendar days, then averaged.
                            "ann_x": float(hit["total_turnover"].iloc[0])
                                     / (float(hit["span_days"].iloc[0]) / 365.25),
                            "n_rebal": sub["rebalance_id"].nunique()})
    ab = pd.DataFrame(abl)
    ab.to_csv(os.path.join(args.out, "ablation_turnover.csv"), index=False)
    if not ab.empty:
        res["ablation_turnover"] = (
            ab.groupby("arm").agg(ann_x=("ann_x", "mean"),
                                  n_folds=("ann_x", "size"),
                                  n_rebal=("n_rebal", "mean"))
              .reset_index().sort_values("ann_x").to_dict(orient="records"))

    # ---- 6b. trade coarseness: fills per rebalance date --------------------
    fpd = d.groupby(["strategy", "batch", "fold", "date"]).size()
    res["fills_per_date"] = {
        "n_dates": int(len(fpd)),
        "share_dates_with_1_fill": float((fpd == 1).mean()),
        "share_dates_with_le_2_fills": float((fpd <= 2).mean()),
        "mean_fills": float(fpd.mean()), "median_fills": float(fpd.median()),
        "p90_fills": float(fpd.quantile(0.90)),
        "obs_note": "the book does NOT rebalance as one block: the median rebalance date "
                    "carries a single fill, so there is no 'all names nudge together' event "
                    "for a portfolio-level band to suppress.",
    }

    # ---- 6c. zero-fill instructions (must NOT be merged with real friction) --
    zf = []
    for p_csv in sorted(glob.glob(os.path.join(B17, "*", "*", "walkforward_rebalances.csv"))):
        rep = pd.read_csv(p_csv.replace("rebalances", "report"))
        strat = p_csv.split(os.sep)[-2].replace("_strategy", "")
        zf.append({"strategy": strat, "batch": p_csv.split(os.sep)[-3],
                   "instructions": int(rep["total_rebalances"].sum())})
    zf = pd.DataFrame(zf)
    filled = (d.groupby(["strategy", "batch"])["date"].nunique().rename("fill_dates")).reset_index()
    zf = zf.merge(filled, on=["strategy", "batch"], how="left")
    zf["zero_fill"] = zf["instructions"] - zf["fill_dates"]
    res["zero_fill_instructions"] = {
        "note": "`total_rebalances` = is_rebalance.sum() (allocation_backtester.py:2554) = "
                "the number of INSTRUCTIONS (rows of the sparse weight frame); a fill only "
                "happens when at least one |delta_w| clears min_shares/lot size. An "
                "instruction with no fill trades nothing, so it contributes 0 to turnover "
                "and 0 to friction. Reported separately because L2.49 item 6 requires it; "
                "it must never be added to or subtracted from a friction number.",
        "by_strategy": zf.to_dict(orient="records"),
        "total_instructions": int(zf["instructions"].sum()),
        "total_fill_dates": int(zf["fill_dates"].sum()),
        "total_zero_fill": int(zf["zero_fill"].sum()),
        "zero_fill_share": float(zf["zero_fill"].sum() / zf["instructions"].sum()),
    }

    # ---- 6d. churn / reversal: total-variation vs net-position decomposition
    # A naive "consecutive opposite signs" metric double-counts the middle element of
    # every alternating chain (it appears in two pairs) and can exceed 1.0. The
    # well-defined quantity is the total-variation excess:
    #     gross = sum_k |s_k|          (path length of the target weight)
    #     net   = |sum_k s_k|          (net repositioning)
    #     reversal = gross - net       (notional that cancels itself out)
    # A clean stop-out -> re-entry round trip of size T has gross = 2T, net = 0, so
    # reversal = 2T = 100% of that path. Reported per path family.
    def tv_excess(sub: pd.DataFrame) -> tuple[float, float]:
        g = n = 0.0
        for _, sg in sub.groupby(["strategy", "batch", "fold", "symbol"], sort=False):
            v = sg["signal_w"].to_numpy()
            if len(v) == 0:
                continue
            g += float(np.abs(v).sum())
            n += abs(float(v.sum()))
        return g, n

    g_all, n_all = tv_excess(d)
    rz_only = d[d["is_resize"]]
    g_rz, n_rz = tv_excess(rz_only)
    res["churn_reversal"] = {
        "method": "total-variation excess per (fold,symbol) target-weight path: "
                  "reversal = sum|s_k| - |sum s_k|; share = reversal / sum|s_k|.",
        "all_events": {"gross_w": g_all, "net_w": n_all,
                       "reversal_share": (g_all - n_all) / g_all if g_all else None,
                       "reversal_x_per_yr": (g_all - n_all) / g_all * ann_all if g_all else None},
        "resize_only": {"gross_w": g_rz, "net_w": n_rz,
                        "reversal_share": (g_rz - n_rz) / g_rz if g_rz else None,
                        "reversal_x_per_yr": (g_rz - n_rz) / g_rz * ann_all if g_rz else None},
        "obs_note": "`resize_only` is the ceiling for a cost-aware / smoothed target layer "
                    "that leaves entries and exits untouched: it is the part of held-name "
                    "re-estimation that cancels itself out. `all_events` additionally contains "
                    "the stop-out -> re-entry round trips, which belong to L2.52 and are closed.",
    }

    # ---- 7. cross-check on the L2.52 primary 21-CSV scope -------------------
    cross = {}
    for run_name, rd in dt.load_scope_dirs().items():
        if not os.path.isdir(rd):
            continue
        x = dt.load_run(rd, run_name)
        if x.empty:
            continue
        x = dt.classify(x)
        fy = dt.fold_years(x)
        sp = fold_spans(os.path.join(rd, "*", "walkforward_report.csv"))
        fy = fy.drop(columns=["cal_days"], errors="ignore").merge(
            sp.rename(columns={"span_days": "cal_days"}),
            on=["strategy", "fold"], how="left")
        if fy["cal_days"].isna().any():
            print(f"[warn] {run_name}: {int(fy['cal_days'].isna().sum())} folds without a "
                  f"walkforward_report span; those folds are dropped from the annualization",
                  file=sys.stderr)
            fy = fy.dropna(subset=["cal_days"])
        # engine convention: total_turnover = notional / exec_equity PER REBALANCE,
        # so the per-row portfolio_equity is the right denominator (using fold-start
        # equity inflates long folds where the book compounds).
        x["m"] = x["trade_value"] / x["portfolio_equity"]
        agg = x.groupby(["batch", "strategy", "fold", "bucket"])["m"].sum().reset_index() \
               .merge(fy, on=["batch", "strategy", "fold"], how="left")
        agg = agg.dropna(subset=["cal_days"])
        agg["ann"] = agg["m"] / (agg["cal_days"] / 365.25)
        tot_fold = agg.groupby(["batch", "strategy", "fold"])["ann"].sum().mean()
        # Shares = mean over folds of PER-FOLD shares (each fold's six shares sum to 1).
        # NOT the mean of per-(fold,bucket) annuals: a bucket absent from some folds would
        # have its mean taken over fewer folds and inflate every share. That bug made the
        # `08_ablate_wf/baseline` shares sum to 100.27% and its x/yr column to 15.86 on a
        # 15.23 total. Per-fold shares sum to 1 by construction and keep equal fold weight.
        per = (agg.groupby(["batch", "strategy", "fold", "bucket"])["m"].sum()
                  .unstack(fill_value=0.0))
        sh_fold = per.div(per.sum(axis=1), axis=0)
        sh_mean = sh_fold.mean()
        cross[run_name] = {"total_ann_x": float(tot_fold),
                           "shares": {k: float(v) for k, v in sh_mean.items()},
                           "x_per_yr": {k: float(v * tot_fold) for k, v in sh_mean.items()},
                           "n_rows": int(len(x)),
                           "median_fold_cal_days": float(fy["cal_days"].median())}
    res["cross_check_scope"] = cross

    # ---- 8. measured (not estimated) turnover of the already-run band arms -----
    # `09_param_grid` is the ONLY batch in the repo that ran a causal, deployable
    # turnover cut. Its inertia arms are exactly the "widen the per-asset band" lever;
    # reading their realized turnover is therefore a MEASUREMENT of that lever's reach,
    # not an estimate. Same 18 x 63-bar geometry as `08_ablate_wf/baseline`.
    pg = []
    for gdir in sorted(glob.glob(os.path.join(SCOPE, "09_param_grid", "*", "*"))):
        grid, arm = gdir.split(os.sep)[-2], os.path.basename(gdir)
        if not os.path.isdir(gdir):
            continue
        sp = fold_spans(os.path.join(gdir, "*", "walkforward_report.csv"))
        for p_csv in sorted(glob.glob(os.path.join(gdir, "*", "walkforward_rebalances.csv"))):
            df = pd.read_csv(p_csv)
            strat = p_csv.split(os.sep)[-2]
            df["m"] = df["trade_value"] / df["portfolio_equity"]
            rows = []
            for fold, sub in df.groupby("fold"):
                hit = sp.loc[(sp["strategy"] == strat) & (sp["fold"] == fold)]
                if hit.empty or float(hit["span_days"].iloc[0]) <= 0:
                    continue
                rows.append(float(hit["total_turnover"].iloc[0])
                            / (float(hit["span_days"].iloc[0]) / 365.25))
            pg.append({"grid": grid, "arm": arm, "strategy": strat,
                       "ann_x": float(np.mean(rows)), "n_folds": len(rows),
                       "n_rebal": int(df.groupby("fold")["date"].nunique().sum())})
    pgt = pd.DataFrame(pg)
    pgt.to_csv(os.path.join(args.out, "param_grid_turnover.csv"), index=False)
    if not pgt.empty:
        pivot = pgt.pivot_table(index=["grid", "arm"], columns="strategy", values="ann_x")
        pivot["mean"] = pivot.mean(axis=1)
        base = pivot.loc[("atr", "atr_none")] if ("atr", "atr_none") in pivot.index else None
        # honest baseline = the 08_ablate_wf/baseline arm, same geometry
        b = ab[ab["arm"] == "baseline"].groupby("strategy")["ann_x"].mean()
        pivot = pivot.reindex(columns=sorted(pivot.columns))
        out_rows = []
        for idx, row in pivot.iterrows():
            rec = {"grid": idx[0], "arm": idx[1]}
            deltas = []
            for st in b.index:
                if st in pivot.columns:
                    rec[f"x_{st}"] = float(row[st]) if pd.notna(row[st]) else None
                    deltas.append(float(row[st] / b[st]) - 1.0)
            rec["mean_x"] = float(row.get("mean", float("nan")))
            rec["mean_rel_vs_baseline"] = float(np.mean(deltas)) if deltas else None
            out_rows.append(rec)
        res["param_grid_turnover"] = {
            "note": "MEASURED annualized two-sided turnover (trade_value / per-row "
                    "portfolio_equity, per-fold calendar-day annualization) of the arms "
                    "`09_param_grid` already ran. The inertia_* arms ARE the per-asset band "
                    "lever; their reach is a measurement, not an estimate. Baseline for the "
                    "relative column = 08_ablate_wf/baseline (same 18 x 63-bar geometry).",
            "baseline_by_strategy": {k: float(v) for k, v in b.items()},
            "rows": out_rows,
        }

    with open(os.path.join(args.out, "ceilings.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2, default=float)

    print(json.dumps({k: res[k] for k in ("drift_magnitude", "portfolio_level_band",
                                         "coarse_grain_ceiling", "band_widening_reach")},
                     ensure_ascii=False, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

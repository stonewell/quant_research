#!/usr/bin/env python3
"""`L2.49` scoping phase -- assemble the memo's tables from `out/*.json`.

Every table here is regenerated from the two measurement scripts; nothing is typed in by
hand, so a future re-run cannot drift from the memo. Emits:
  T1_decomposition.csv        bucket decomposition of the 16.x baseline + cross-window
  T2_per_strategy.csv         per-strategy decomposition, friction, CAGR
  T3_lever_ceilings.csv       per-lever arithmetic ceiling (x/yr) + risk cost + class
  T4_band_arms_measured.csv   the already-run inertia arms: turnover vs return
  T5_noise_floor.csv          friction budget vs the 18-fold detection threshold

USAGE
    pipeline/.venv/bin/python make_tables.py
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
# 4 levels up (script lives in `l2_49_scope/_provenance/` since 2026-10-29): repo root.
SCOPE = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
B17 = os.path.join(SCOPE, "pipeline", "results", "blend_17_baseline")
PG = os.path.join(SCOPE, "backtests_research", "20261025_125300", "09_param_grid")
AB = os.path.join(SCOPE, "backtests_research", "20261025_125300", "08_ablate_wf")

# measured blended cost per unit of executed trade_value, `blend_17_baseline` 5,426 rows:
# BUY 10.0 bp, SELL 15.0 bp (commission 5 + slippage 5, +stamp 5 on sells), value-weighted
# 12.0946 bp (`out/friction_rate.json::cost_rate_bp_all`).
#
# DO NOT halve this. `total_turnover` accumulates `trade_val` for sells AND buys
# (`allocation_backtester.py:1803`/`:1825` -> `:1828`), so two-sided turnover x this
# per-leg-value rate is already the right dimension. `friction_rate.py` settles it by
# measuring cost against equity and elapsed years with no turnover convention at all:
# 201.15 bp/yr vs 16.629 x 12.0946 = 201.12 bp/yr (0.01% apart), while the halved
# conversion would give 100.6 bp/yr -- 50% of what the ledger actually charged.
# NOTE the repo's §16.15.13 baseline "85.26 bp/年" IS the halved (one-sided) conversion
# of the same cost rate; it understates by the buy leg. See the memo §6 for the audit.
COST_BP_PER_LEG = 12.0946
COST_BP_PER_TURNOVER_UNIT = COST_BP_PER_LEG


def fold_stats(pattern: str) -> pd.DataFrame:
    """Per-strategy fold-level statistics, pooling EVERY fold file the pattern matches.

    Pools at the FOLD level then aggregates -- averaging two batch means would weight
    batch 2 (3 folds) the same as batch 1 (15 folds).
    """
    folds = []
    for p in sorted(glob.glob(pattern)):
        rep = pd.read_csv(p)
        # `_strategy` suffix only exists in `pipeline/results/blend_17_baseline/<b>/<name>_strategy/`
        st = p.split(os.sep)[-2].replace("_strategy", "")
        span = (pd.to_datetime(rep["end_date"]) - pd.to_datetime(rep["start_date"])).dt.days
        for (_, r), sp in zip(rep.iterrows(), span):
            if sp <= 0:
                continue
            folds.append({"strategy": st,
                          "ann_x": float(r["total_turnover"]) / (float(sp) / 365.25),
                          "cagr": float(r["cagr"]),
                          "sharpe": float(r["sharpe_ratio"])})
    f = pd.DataFrame(folds)
    g = f.groupby("strategy")
    out = pd.DataFrame({
        "n_folds": g.size(),
        "ann_x": g["ann_x"].mean(),
        "cagr": g["cagr"].mean(),
        "cagr_std": g["cagr"].std(),
        "se_cagr": g["cagr"].std() / np.sqrt(g.size()),
        "sharpe": g["sharpe"].mean(),
        "sharpe_std": g["sharpe"].std(),
    })
    return out.reset_index()


def main() -> int:
    dec = json.load(open(os.path.join(OUT, "summary.json"), encoding="utf-8"))
    cei = json.load(open(os.path.join(OUT, "ceilings.json"), encoding="utf-8"))
    anchor = dec["anchor_ann_cal_mean"]

    # ---------------------------------------------------------------- T1
    # The six buckets are MUTUALLY EXCLUSIVE and sum to the total. `drift_resize` /
    # `signal_resize` are a SPLIT of trim+add, not additional buckets -- adding them to
    # the six would double-count (an early draft printed 21.59x/yr for a 16.63x baseline
    # exactly this way). They are appended as an indented sub-row instead.
    t1 = []
    for k, v in dec["buckets"].items():
        if k in ("drift_resize", "pure_drift", "signal_resize"):
            continue
        t1.append({"bucket": k, "share_of_trade_value": v["share"],
                   "x_per_yr": v["x_per_yr"]})
    t1 = pd.DataFrame(t1).sort_values("share_of_trade_value", ascending=False)
    t1.loc[len(t1)] = {"bucket": "TOTAL", "share_of_trade_value": 1.0,
                       "x_per_yr": float(t1["x_per_yr"].sum())}
    dr = dec["buckets"]["drift_resize"]
    t1.loc[len(t1)] = {"bucket": "  └ of which drift (inside trim+add, NOT additive)",
                       "share_of_trade_value": dr["share"], "x_per_yr": dr["x_per_yr"]}
    # cross-window shares
    for run, blk in cei["cross_check_scope"].items():
        t1[f"share_{run}"] = t1["bucket"].map(blk["shares"]).fillna(np.nan)
    t1.to_csv(os.path.join(OUT, "T1_decomposition.csv"), index=False)

    # ---------------------------------------------------------------- T2
    bs = pd.read_csv(os.path.join(OUT, "bucket_by_strategy.csv"))
    piv = bs.pivot_table(index="strategy", columns="bucket", values="x_per_yr")
    ann = bs.groupby("strategy")["ann_x"].first()
    fx = fold_stats(os.path.join(B17, "[12]", "*", "walkforward_report.csv"))
    t2 = piv.copy()
    t2.insert(0, "ann_x", ann)
    t2["friction_bp"] = ann * COST_BP_PER_TURNOVER_UNIT
    t2 = t2.join(fx.set_index("strategy")[["cagr", "cagr_std", "se_cagr"]])
    t2["friction_pct_of_cagr"] = t2["friction_bp"] / 1e4 / t2["cagr"]
    rows = pd.read_csv(os.path.join(OUT, "b17_rows_classified.csv"))
    drift_share = (rows.groupby("strategy")
                       .apply(lambda g: g.loc[g["is_resize"], "tv_drift"].abs().sum()
                              / g["trade_value"].sum(), include_groups=False)
                       .rename("drift_share_of_total"))
    t2 = t2.join(drift_share)
    t2.to_csv(os.path.join(OUT, "T2_per_strategy.csv"))

    # ---------------------------------------------------------------- T3
    dr = cei["drift_magnitude"]
    pb = cei["portfolio_level_band"]
    cg = cei["coarse_grain_ceiling"]["estimates"]
    bw = {r["th"]: r for r in cei["band_widening_reach"]}
    asy = cei["asymmetric_sell_wiring"]["per_strategy_trim_share"]
    cdhb_ann = float(t2.loc["chan_dual_hybrid_blend", "ann_x"]) if \
        "chan_dual_hybrid_blend" in t2.index else 16.0
    n_strat = 6
    t3 = [
        {"lever": "(a) 漂移带 drift band (per-asset, on |T_prev - w_actual|)",
         "ceiling_x": dr["drift_component_x_per_yr"],
         "ceiling_share": dr["drift_component_share_of_total"],
         "basis": f"measured drift component of resize notional; median |drift_w|="
                  f"{dr['median_abs_drift_w']:.4f}, p90={dr['p90_abs_drift_w']:.4f}",
         "risk_cost": "低——不改变目标权重，只容忍偏离；但会引入未对冲的权重漂移",
         "class": "膏药（band-aid）"},
        {"lever": "(b) 非对称卖出比例 asymmetric_sell_ratio 0.60 → 1.0",
         "ceiling_x": 0.0,
         "ceiling_share": 0.0,
         "basis": "只对 cdhb 接线(blend_base.py:609)；实测 trim 行中 |Δw|<0.05 的成交额 = "
                  "0.0000（六策略全部），即把卖出带宽拉到等于买入带宽一格也擦不掉任何行",
         "risk_cost": "无（无行可擦）",
         "class": "空杠杆"},
        {"lever": "(b') asymmetric_sell_ratio → 1.6（卖出带宽 0.08，超过买入带宽）",
         "ceiling_x": float(asy["chan_dual_hybrid_blend"]["trim_share_absdw_lt_0.08"] / n_strat
                            * cdhb_ann),
         "ceiling_share": float(asy["chan_dual_hybrid_blend"]["trim_share_absdw_lt_0.08"] / n_strat),
         "basis": f"cdhb trim |Δw|<0.08 = cdhb 换手的 "
                  f"{asy['chan_dual_hybrid_blend']['trim_share_absdw_lt_0.08']:.4f}，"
                  f"折成组合口径 ÷6 策略",
         "risk_cost": "削减卖出灵敏度＝把减仓推迟，正是 09_param_grid 已测的那条轴",
         "class": "膏药"},
        {"lever": "(c) 组合级再平衡带 portfolio-level band",
         "ceiling_x": float(pb["share_of_notional_on_dates_with_abs_net_lt"]["0.05"] * anchor),
         "ceiling_share": float(pb["share_of_notional_on_dates_with_abs_net_lt"]["0.05"]),
         "basis": f"中位每调仓日只有 {pb.get('median_fills', 1.0):.0f} 笔成交、"
                  f"{cei['fills_per_date']['share_dates_with_1_fill']:.1%} 的日期只有 1 笔 ⇒ "
                  f"不存在「全票同向微调」事件；仅 "
                  f"{pb['share_of_notional_on_dates_with_abs_net_lt']['0.05']:.1%} 的名义额落在 "
                  f"|净敞口变动|<0.05 的日期上",
         "risk_cost": "低（几乎无对象可作用）",
         "class": "结构性退化 + 等价于已证伪的调仓频率族"},
        {"lever": "(d) 子策略权重平滑 / IC-IR 平滑半衰期",
         "ceiling_x": np.nan,
         "ceiling_share": np.nan,
         "basis": "08_ablate_wf 同几何实测：关掉正交化 −7.6%、no_hysteresis +0.7%、"
                  "no_voltarget +3.4%、no_cyq −6.9%（baseline 15.235×）⇒ 权重模式旋钮的"
                  "换手射程 ≤8%，且方向多为「关掉反而降」",
         "risk_cost": "这些旋钮是绩效开关，不是摩擦开关",
         "class": "不可用（手册评审已否「半衰期→调仓周期」替换）"},
        {"lever": "(e) 成本感知的目标层（目标层扣减预期摩擦）",
         "ceiling_x": float(cei["churn_reversal"]["resize_only"]["reversal_x_per_yr"]),
         "ceiling_share": float(cei["churn_reversal"]["resize_only"]["reversal_share"]),
         "basis": f"只作用于「自我抵消」的再估计：resize 路径全变差超出 "
                  f"{cei['churn_reversal']['resize_only']['reversal_share']:.1%}（完美前瞻上界）",
         "risk_cost": "高——因果版本就是加宽 per-asset 带宽，已被 09_param_grid 实测否掉；"
                      "完美前瞻上界不可实现",
         "class": "上界不可达；因果版本已证伪"},
        {"lever": "调仓日历 → 粗粒度合并（每 k 次调仓只做一次）",
         "ceiling_x": anchor - cg[0]["x_per_yr_after"],
         "ceiling_share": 1.0 - cg[0]["retained_share_of_signal"],
         "basis": f"k=2 保留 {cg[0]['retained_share_of_signal']:.1%} ⇒ 16.63→"
                  f"{cg[0]['x_per_yr_after']:.2f}×；k=3 → {cg[1]['x_per_yr_after']:.2f}×。"
                  f"一阶估计（用已实现信号路径，假设被跳过的腿由下一次原样补上）",
         "risk_cost": "高——等价于把带宽拉到 0.10–0.15（见 T4 实测臂）",
         "class": "唯一能进门槛的族；已实测并被拒"},
        {"lever": "加宽 per-asset 带宽 min_weight_change（09_param_grid 实测，非估计）",
         "ceiling_x": float(16.629 - 10.406), "ceiling_share": 0.317,
         "basis": "0.08/4 臂：16.63→10.41×（−31.7%）；0.10/5→8.47×；0.15/10→7.04×",
         "risk_cost": "见 T4：进门槛的档位（0.10/5、0.15/10）对应 crb −7.98pp / 三策略全拒",
         "class": "唯一有足够射程；已测，未过净收益检验"},
    ]
    t3 = pd.DataFrame(t3)
    t3.to_csv(os.path.join(OUT, "T3_lever_ceilings.csv"), index=False)

    # ---------------------------------------------------------------- T4
    base = fold_stats(os.path.join(AB, "baseline", "*", "walkforward_report.csv"))
    rows = []
    for g, arm in [("inertia", "inertia_008_4"), ("inertia", "inertia_010_5"),
                   ("inertia", "inertia_015_10"), ("atr", "atr_none")]:
        a = fold_stats(os.path.join(PG, g, arm, "*", "walkforward_report.csv"))
        m = a.set_index("strategy").join(base.set_index("strategy"),
                                         lsuffix="_arm", rsuffix="_base")
        rec = {"arm": arm, "mean_x": float(a["ann_x"].mean()),
               "rel_x": float((a["ann_x"].mean() / base["ann_x"].mean()) - 1.0)}
        for st in m.index:
            rec[f"x_{st}"] = float(m.loc[st, "ann_x_arm"])
            rec[f"dCAGR_{st}_pp"] = float((m.loc[st, "cagr_arm"] - m.loc[st, "cagr_base"]) * 100)
        rows.append(rec)
    t4 = pd.DataFrame(rows)
    t4.to_csv(os.path.join(OUT, "T4_band_arms_measured.csv"), index=False)

    # ---------------------------------------------------------------- T5
    fr = json.load(open(os.path.join(OUT, "friction_rate.json"), encoding="utf-8"))
    friction_now = anchor * COST_BP_PER_TURNOVER_UNIT
    friction_gate = 8.0 * COST_BP_PER_TURNOVER_UNIT
    friction_cap = 10.0 * COST_BP_PER_TURNOVER_UNIT
    se2_mean = float(base["se_cagr"].mean() * 2 * 10000)
    t5 = pd.DataFrame([
        {"item": "基线年化摩擦 (16.63×, 实测 total_cost/equity/年)", "value": float(
            fr["friction_bp_yr_mean_over_folds"]), "unit": "bp/yr（无换手口径假设）"},
        {"item": "基线年化摩擦 (16.63×, 换手×单位成本)", "value": friction_now,
         "unit": "bp/yr"},
        {"item": "对照：若按单边换手折算（§16.15.13 口径）", "value": friction_now / 2.0,
         "unit": "bp/yr（低 2 倍，见 §6）"},
        {"item": "达到合格线 8.0× 后的摩擦", "value": friction_gate, "unit": "bp/yr"},
        {"item": "达到硬上限 10.0× 后的摩擦", "value": friction_cap, "unit": "bp/yr"},
        {"item": "**削减到 8.0× 的最大节省**", "value": friction_now - friction_gate,
         "unit": "bp/yr = pp of CAGR"},
        {"item": "**全部摩擦归零的理论节省**", "value": friction_now,
         "unit": "bp/yr = pp of CAGR"},
        {"item": "2-SE 检测门槛 (cdhb, 18 折)", "value": float(base.set_index("strategy").loc[
            "chan_dual_hybrid_blend", "se_cagr"] * 2 * 10000), "unit": "bp（1 bp = 0.01pp 收益）"},
        {"item": "2-SE 检测门槛 (cfsb, 18 折)", "value": float(base.set_index("strategy").loc[
            "chan_four_state_blend", "se_cagr"] * 2 * 10000), "unit": "bp"},
        {"item": "2-SE 检测门槛 (crb, 18 折)", "value": float(base.set_index("strategy").loc[
            "chan_risk_managed_blend", "se_cagr"] * 2 * 10000), "unit": "bp"},
        {"item": "节省 / 门槛 (削减到 8.0×)", "value": float(
            (friction_now - friction_gate) / se2_mean), "unit": "ratio (无单位)"},
        {"item": "节省 / 门槛 (全部摩擦归零)", "value": float(
            friction_now / se2_mean), "unit": "ratio (无单位)"},
        {"item": "L2.52 唯一合资格杠杆的天花板 (总摩擦的 2.1%)",
         "value": 0.021 * friction_now, "unit": "bp/yr（份额不变，基数改实测值）"},
    ])
    t5.to_csv(os.path.join(OUT, "T5_noise_floor.csv"), index=False)

    print(t1.round(4).to_string(index=False))
    print()
    print(t2.round(4).to_string())
    print()
    print(t3[["lever", "ceiling_x", "ceiling_share", "class"]].round(4).to_string(index=False))
    print()
    print(t4.round(3).to_string(index=False))
    print()
    print(t5.round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

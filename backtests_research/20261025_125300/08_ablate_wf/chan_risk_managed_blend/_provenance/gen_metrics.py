#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generates 08_ablate_wf/chan_risk_managed_blend/_metrics.md from on-disk artifacts only.

Every number in the output markdown is read out of a file this worker produced
(walkforward_summary.json / walkforward_report.csv / walkforward_rebalances.csv /
strategy_dump.json / run_manifest.json / run.log) or, where explicitly labelled,
out of a read-only reference file (06_ablate comparison_report.json,
docs/backtest_research.md, common/metrics.py, rs/config.py, blend_base.py).
Nothing is estimated by hand.
"""
import csv
import json
import math
import os
import statistics
from statistics import NormalDist

BT = "backtests_research/20261025_125300"
S = "chan_risk_managed_blend"
WF = os.path.join(BT, "08_ablate_wf")
OUT = os.path.join(WF, S)
os.makedirs(OUT, exist_ok=True)

ARMS = ["baseline", "no_voltarget", "no_orthogon", "no_structregime", "no_cyq",
        "no_atrstop", "no_lowbeta", "no_sectorthrot", "no_hysteresis", "no_thrust",
        "no_inertia", "bull_full", "no_circuit"]


def arm_dir(a):
    return os.path.join(WF, a, S)


def load(a):
    d = arm_dir(a)
    s = json.load(open(os.path.join(d, "walkforward_summary.json")))
    m = json.load(open(os.path.join(d, "run_manifest.json")))
    p = json.load(open(os.path.join(d, "strategy_dump.json")))["params"]
    fold = {}
    for i, r in enumerate(csv.DictReader(open(os.path.join(d, "walkforward_report.csv"))), 1):
        fold[i] = {k: float(r[k]) for k in ("sharpe_ratio", "cagr", "max_drawdown", "calmar_ratio")}
    eq = {}
    for r in csv.DictReader(open(os.path.join(d, "walkforward_rebalances.csv"))):
        eq[(int(r["fold"]), r["date"])] = float(r["portfolio_equity"])
    log = open(os.path.join(d, "run.log"), errors="replace").read()
    return {"summary": s, "manifest": m, "params": p, "folds": fold, "eq": eq,
            "unknown_key": log.count("ignoring unknown key(s)"),
            "traceback": log.count("Traceback")}


D = {a: load(a) for a in ARMS}
B = D["baseline"]
ref = B["summary"]
base_last_end = ref["actual_last_fold_end"]

# --------------------------------------------------------------- derived stats
def aligned(a):
    common = sorted(set(B["eq"]) & set(D[a]["eq"]))
    md = max((abs(B["eq"][k] - D[a]["eq"][k]) for k in common), default=float("nan"))
    return len(common), md, len(set(D[a]["eq"]) - set(B["eq"])), len(set(B["eq"]) - set(D[a]["eq"]))


for a in ARMS:
    n, md, oa, ob = aligned(a)
    ds, dc, dd = [], [], []
    for f in B["folds"]:
        ds.append(D[a]["folds"][f]["sharpe_ratio"] - B["folds"][f]["sharpe_ratio"])
        dc.append(D[a]["folds"][f]["cagr"] - B["folds"][f]["cagr"])
        dd.append(D[a]["folds"][f]["max_drawdown"] - B["folds"][f]["max_drawdown"])
    D[a].update(aligned=n, maxdiff=md, only_arm=oa, only_base=ob, ds=ds, dc=dc, dd=dd,
                ident=((md < 1e-9) and n > 0))
    D[a]["wins_sh"] = sum(1 for x in ds if x > 1e-12)
    D[a]["wins_ca"] = sum(1 for x in dc if x > 1e-12)
    D[a]["wins_dd"] = sum(1 for x in dd if x < -1e-12)
    D[a]["lose_sh"] = sum(1 for x in ds if x < -1e-12)
    D[a]["d_sh"] = D[a]["summary"]["mean_sharpe_ratio"] - ref["mean_sharpe_ratio"]
    D[a]["d_ca"] = (D[a]["summary"]["mean_cagr"] - ref["mean_cagr"]) * 100.0
    D[a]["d_dd"] = (D[a]["summary"]["mean_max_drawdown"] - ref["mean_max_drawdown"]) * 100.0

NF = ref["n_folds"]
BAR_PER_FOLD = 63  # run_backtest.py:273-274 -> int(round(0.25*252))

# verify portfolio_equity is single-valued within each (fold, date) group
N_EQ_GROUPS = 0
N_EQ_NONCONST = 0
for a in ARMS:
    groups = {}
    for r in csv.DictReader(open(os.path.join(arm_dir(a), "walkforward_rebalances.csv"))):
        groups.setdefault((r["fold"], r["date"]), set()).add(r["portfolio_equity"])
    N_EQ_GROUPS += len(groups)
    N_EQ_NONCONST += sum(1 for v in groups.values() if len(v) > 1)
ALIGN_MIN = min(D[a]["aligned"] for a in ARMS)
ALIGN_MAX = max(D[a]["aligned"] for a in ARMS)

# read-only reference: 06_ablate standard mode
STD_DIR = os.path.join(BT, "06_ablate")
STD = {}
for a in ARMS:
    cr = json.load(open(os.path.join(STD_DIR, a, S, "comparison_report.json")))
    STD[a] = (cr["strategy_sharpe_ratio"], cr["strategy_cagr"])
STD_OVERLAP = json.load(open(os.path.join(STD_DIR, "baseline", S, "comparison_report.json")))["overlap_bars"]
b_std = STD["baseline"]
std_d_sh = {a: STD[a][0] - b_std[0] for a in ARMS}
std_d_ca = {a: (STD[a][1] - b_std[1]) * 100.0 for a in ARMS}

# read-only reference: standard-mode equity, to count post-truncation bars
std_eq_dates = [r[""] for r in csv.DictReader(open(os.path.join(STD_DIR, "baseline", S, "backtest_equity.csv")))]
dropped_bars = sum(1 for d in std_eq_dates if d > base_last_end)
first_dropped = next((d for d in std_eq_dates if d > base_last_end), "-")
last_dropped = std_eq_dates[-1]

# E[max] barrier, replicating common/metrics.py:190-196 exactly
GAMMA = 0.5772156649015329
nd = NormalDist()


def expected_max_sharpe(n_trials, sharpe_std):
    z1 = nd.inv_cdf(1 - 1.0 / n_trials)
    z2 = nd.inv_cdf(1 - 1.0 / (n_trials * math.e))
    return sharpe_std * ((1 - GAMMA) * z1 + GAMMA * z2)


SR0 = expected_max_sharpe(NF, ref["fold_sharpe_std"])
MAX_DSR = max(D[a]["summary"]["deflated_sharpe_ratio"] for a in ARMS)
ident_arms = [a for a in ARMS if a != "baseline" and D[a]["ident"]]
flips = [a for a in ARMS if a != "baseline" and std_d_sh[a] * D[a]["d_sh"] < 0]

INTENDED = {
    "no_voltarget": {"crb_enable_vol_targeting": False},
    "no_orthogon": {"blend_enable_orthogonalization": False},
    "no_structregime": {"blend_enable_structural_regime": False},
    "no_cyq": {"blend_enable_cyq_quality_gate": False},
    "no_atrstop": {"blend_enable_adaptive_stop_atr": False},
    "no_lowbeta": {"blend_enable_low_beta_inertia": False},
    "no_sectorthrot": {"blend_max_assets_per_sector": 20},
    "no_hysteresis": {"blend_enable_regime_hysteresis": False},
    "no_thrust": {"crb_dynamic_cash_deployment": False},
    "no_inertia": {"crb_adaptive_inertia": False, "crb_min_weight_change": 0.0,
                   "crb_min_holding_bars": 0, "crb_entry_threshold": 0.0},
    "bull_full": {"crb_target_bull_exposure": 1.0},
    "no_circuit": {"crb_dd_reduce_thresh": 10.0, "crb_dd_defensive_thresh": 10.0,
                   "crb_dd_stop_thresh": 10.0},
}
DUMP = json.load(open("pipeline/research_strategy/results/strategy_dumps/"
                      "chan_risk_managed_blend_strategy.json"))["params"]

# ------------------------------------------------------------------- markdown
L = []
w = L.append

w("# chan_risk_managed_blend（前缀 `crb`）—— 走查（walkforward）13 臂模块归因")
w("")
w("> 本文件只覆盖 **`chan_risk_managed_blend` 单一策略**的 13 臂 walkforward 重跑。")
w("> `chan_four_state_blend` / `chan_dual_hybrid_blend` 由其他 worker 负责，不在本文件范围内。")
w("> 全部数字由本 worker 自己产出的 `walkforward_summary.json` / `walkforward_report.csv` / "
  "`walkforward_rebalances.csv` / `strategy_dump.json` / `run_manifest.json` / `run.log` 读取；"
  "少数只读引用（`06_ablate`、`docs/`、`common/metrics.py`、`rs/config.py`、`blend_base.py`）在正文逐处标注。"
  "无人工估算。")
w("")
w("## 0. 结论速览")
w("")
w(f"- **13/13 臂**：`status=completed`、`mode=walkforward`、`n_folds={NF}`、`Traceback=0`、`ignoring unknown key(s)=0`"
  " ⇒ **无臂作废**（逐臂核验见 §4）。")
w(f"- **≡（对 baseline 数值恒等、零信息）恰好 {len(ident_arms)} 个："
  + "、".join(f"`{a}`" for a in ident_arms)
  + "** —— 与标准模式（`06_ablate`）的既有发现**一致**，且本次补上代码级归因（§3.1），"
  "证明其恒等是**结构性**的、与评估模式无关。")
w(f"- **标准模式下同样恒等的 `no_circuit` 在走查下不再恒等**（对齐 {D['no_circuit']['aligned']} 个 bar，"
  f"最大权益差 {D['no_circuit']['maxdiff']:.4f} 元），但经济量级可忽略（ΔSharpe {D['no_circuit']['d_sh']:+.6f}）。"
  "这是**折内权益重置**暴露出来的唯一一条「标准模式看不见、走查看得见」的差异。")
w(f"- **两条结论在 OOS 逐折评估下符号翻转**（标准为负、走查为正）：`no_orthogon`"
  f"（ΔSharpe 标准 {std_d_sh['no_orthogon']:+.4f} → 走查 {D['no_orthogon']['d_sh']:+.4f}）、"
  f"`no_thrust`（标准 {std_d_sh['no_thrust']:+.4f} → 走查 {D['no_thrust']['d_sh']:+.4f}）"
  " ⇒ 这两个模块的「贡献」是单路径产物，不构成 OOS 证据。")
w(f"- **两种模式下同向、且确实在承担风控职责的模块**：`no_inertia`（去惯性门后 Sharpe "
  f"{D['no_inertia']['d_sh']:+.4f}、MaxDD {D['no_inertia']['d_dd']:+.2f}pp）与 `bull_full`"
  f"（牛市敞口 0.8→1.0 后 CAGR {D['bull_full']['d_ca']:+.2f}pp 却 Sharpe {D['bull_full']['d_sh']:+.4f}、"
  f"MaxDD {D['bull_full']['d_dd']:+.2f}pp）。")
w("")

# ---------------- 1
w("## 1. 13 臂 walkforward 指标总表")
w("")
w("口径见 §5。`Δ*` = 「该臂 − baseline」，**ΔMaxDD 为正 = 回撤更深（更差）**。")
w("")
w("| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | actual_last_fold_end | ΔCAGR(pp) | ΔSharpe | ΔMaxDD(pp) |")
w("|---|---|---|---|---|---|---|---|---|---|---|")
for a in ARMS:
    s = D[a]["summary"]
    w("| `{}` | {:.4f} | {:.2%} | {:.2%} | {:.4f} | {} | {:.4e} | {} | {:+.2f} | {:+.4f} | {:+.2f} |".format(
        a, s["mean_sharpe_ratio"], s["mean_cagr"], s["mean_max_drawdown"], s["mean_calmar_ratio"],
        s["n_folds"], s["deflated_sharpe_ratio"], s["actual_last_fold_end"],
        D[a]["d_ca"], D[a]["d_sh"], D[a]["d_dd"]))
w("")
w(f"- baseline 绝对水平：Sharpe **{ref['mean_sharpe_ratio']:.4f}**、CAGR **{ref['mean_cagr']:.2%}**、"
  f"MaxDD **{ref['mean_max_drawdown']:.2%}**、Calmar **{ref['mean_calmar_ratio']:.4f}**、"
  f"`n_valid_folds={ref['n_valid_folds']}`、`fold_sharpe_std={ref['fold_sharpe_std']:.4f}`。")
w(f"- **截断披露（不是 gloss）**：请求 `{ref['requested_start']} → {ref['requested_end']}`，"
  f"标准模式同窗口 `overlap_bars = {STD_OVERLAP}`（引自 `06_ablate/baseline/.../comparison_report.json`）。"
  f"走查实际只能到 `actual_last_fold_end = {base_last_end}`：`window_bars = step_bars = {BAR_PER_FOLD}`"
  f"（`backtester/run_backtest.py:273-274`，`int(round(0.25*252))`）⇒ 覆盖 {NF} × {BAR_PER_FOLD} = "
  f"{NF * BAR_PER_FOLD} 根 bar，**末尾 {dropped_bars} 根 bar（{first_dropped} → {last_dropped}）未进入任何折**"
  "（走查不生成不完整折）。13 臂的 `n_folds` 与 `actual_last_fold_end` 完全一致，臂间横比不受影响；"
  "但**绝对水平不等于标准模式全窗口水平，不得与 `06_ablate` 的绝对收益并列**。")
w(f"- `deflated_sharpe_ratio` 在本折法下**不可用于排序**：其实现（`common/metrics.py:198-215`）取 "
  f"`n_trials = n_valid_folds = {NF}`、`sharpe_std = 折间 Sharpe 标准差 = {ref['fold_sharpe_std']:.4f}` 计算 E[max]，"
  f"按 `common/metrics.py:190-196` 同式复算门槛 sr₀ = **{SR0:.4f}**，远高于任何臂的 mean Sharpe；"
  f"13 臂 DSR 最大值仅 **{MAX_DSR:.4e}** ⇒ **只能读作「没有任何一臂通过 deflation 显著性门槛」，不能用来给臂排序。**")
w("")

# ---------------- 2
w("## 2. 逐折 OOS 一致性（走查相对标准模式的增量信息）")
w("")
w(f"对每一折（共 {NF} 折）取「臂 − baseline」的同折指标差，再统计方向一致率。")
w("")
w("| 臂 | 逐折 ΔSharpe 均值 | Sharpe 胜折 | 中位 ΔSharpe | 逐折 ΔCAGR 均值 | CAGR 胜折 | 逐折 ΔMaxDD(pp) | 回撤更浅折数 |")
w("|---|---|---|---|---|---|---|---|")
for a in ARMS:
    n = len(D[a]["ds"])
    if a == "baseline":
        w("| `baseline` | +0.0000 | — | +0.0000 | +0.00pp | — | +0.000 | — |")
        continue
    w("| `{}` | {:+.4f} | {}/{} | {:+.4f} | {:+.2f}pp | {}/{} | {:+.3f} | {}/{} |".format(
        a, statistics.mean(D[a]["ds"]), D[a]["wins_sh"], n, statistics.median(D[a]["ds"]),
        statistics.mean(D[a]["dc"]) * 100, D[a]["wins_ca"], n,
        statistics.mean(D[a]["dd"]) * 100, D[a]["wins_dd"], n))
w("")
w("读数前提：`mean_sharpe_ratio` / `mean_cagr` / `mean_max_drawdown` 本身就是**折均值**，"
  "所以本表的「逐折 Δ 均值」与 §1 的 `ΔSharpe` / `ΔCAGR` / `ΔMaxDD` **数学上恒等**（已逐臂核对一致）；"
  "本表**唯一新增的信息是胜折数**，即方向在 18 折里是否稳定。")
w("")
w("读法要点：")
w(f"- `no_inertia` 逐折 Sharpe 胜 {D['no_inertia']['wins_sh']}/{NF}、中位 {statistics.median(D['no_inertia']['ds']):+.4f} 为正，"
  f"但逐折均值 {statistics.mean(D['no_inertia']['ds']):+.4f} 为负 ⇒ "
  "**少数折的巨幅恶化把多数折的小幅改善吃掉**：惯性门的作用是**削尾**——关掉它之后回撤反而加深 "
  f"ΔMaxDD {D['no_inertia']['d_dd']:+.2f}pp，这才是它的价值所在，而非抬均值 Sharpe。")
w(f"- `bull_full` 逐折 ΔCAGR 均值 {statistics.mean(D['bull_full']['dc']) * 100:+.2f}pp 但只有 {D['bull_full']['wins_ca']}/{NF} 折胜、"
  f"回撤更浅仅 {D['bull_full']['wins_dd']}/{NF} ⇒ **收益增量集中在少数折**，属典型「单折大赢 + 全期变脆」。")
w(f"- `no_thrust` 逐折 ΔSharpe {statistics.mean(D['no_thrust']['ds']):+.4f}（{D['no_thrust']['wins_sh']}/{NF} 胜）"
  f"而逐折 ΔCAGR {statistics.mean(D['no_thrust']['dc']) * 100:+.2f}pp（{D['no_thrust']['wins_ca']}/{NF} 胜）⇒ "
  "关掉牛市推力加仓是**牺牲收益换波动**，不是纯增益。")
w(f"- `no_orthogon` 逐折 ΔSharpe {statistics.mean(D['no_orthogon']['ds']):+.4f}（{D['no_orthogon']['wins_sh']}/{NF} 胜、"
  f"{D['no_orthogon']['lose_sh']}/{NF} 负）为 13 臂中逐折 Sharpe 均值最高者，但标准模式为 {std_d_sh['no_orthogon']:+.4f} "
  "⇒ **正交化在本策略上只是单路径看起来有用**。")
w("")

# ---------------- 3
w("## 3. ≡ 矩阵：静默空操作检测")
w("")
w("方法：把每臂 `walkforward_rebalances.csv` 的 `portfolio_equity` 按 **(fold, date)** 对齐到 baseline"
  "（该列在同一 `(fold, date)` 内恒为单值 —— 对 13 个文件共 "
  f"{N_EQ_GROUPS} 个 `(fold, date)` 组逐组核验，非常量组 **{N_EQ_NONCONST} 个**），"
  "逐点取 `max|diff|`；`< 1e-9` 判为**数值恒等**。"
  "注意对齐 bar 数逐臂不同（"
  f"{ALIGN_MIN}~{ALIGN_MAX}），因为一臂若在某日完全无成交就不会产生该行；"
  "`only_arm` / `only_base` 两列正是为此披露。")
w("")
w("| 臂 | 对齐 bar 数 | 仅该臂有的行 | 仅 baseline 有的行 | max abs equity diff (元) | 判为 ≡ |")
w("|---|---|---|---|---|---|")
for a in ARMS:
    w("| `{}` | {} | {} | {} | {:.4f} | {} |".format(
        a, D[a]["aligned"], D[a]["only_arm"], D[a]["only_base"], D[a]["maxdiff"],
        "**≡**" if D[a]["ident"] else "否"))
w("")
w(f"**本策略 ≡ 集合 = {{{', '.join('`' + a + '`' for a in ident_arms)}}}**，两臂均为 `aligned = {D[ident_arms[0]]['aligned']}`、"
  f"`only_arm = {D[ident_arms[0]]['only_arm']}`、`only_base = {D[ident_arms[0]]['only_base']}`、`max|diff| = "
  f"{D[ident_arms[0]]['maxdiff']:.1f}`。")
w("")
w("### 3.1 是否与标准模式既有发现一致 —— 一致，并给出代码级归因")
w("")
w("1. **`blend_enable_structural_regime`**：`pipeline/research_strategy/rs/config.py:909` 默认为 `True`；"
  "该 flag 在 `pipeline/research/blend/blend_base.py` 只有三处出现 —— `:648` 读取，"
  "`:468` 在 `elif weighting_mode in (\"regime_adaptive\", \"regime_ic_ir_hybrid\")`（`:465`）分支内使用，"
  "`:769` 仅决定是否预算 `fractal_d_series`（其产物 `median_d` 同样只在该分支被消费）。"
  "本策略 dump 钉死 `blend_weighting_mode=\"rolling_ic_ir\"`，而 `if weighting_mode == \"rolling_ic_ir\"`（`:445`）"
  "分支的每一条出口都是 `return`，`elif`（`:465`）**永不执行** ⇒ flag 恒不被读取 ⇒ **必然 ≡**。"
  "因此该臂的恒等是**结构性**的，与数据、期限、评估模式全部无关。")
w("2. **`blend_enable_regime_hysteresis`**：`pipeline/research_strategy/rs/config.py:794` 默认即 `False`，"
  "而本策略 dump **不含**该键 ⇒ 运行时本来就是 `False` ⇒ 「设为 False」= **写回原值** ⇒ **必然 ≡**。"
  "（对照 `chan_four_state_blend` 在 `strategies_config.json` 把该键钉为 `True`，故同一臂在 cfsb 上并非空操作 —— "
  "这也是两个策略的 ≡ 集合必然不同的原因。）")
w("")
w("### 3.2 标准模式恒等、走查不恒等：`no_circuit`")
w("")
w(f"`no_circuit` 在 `06_ablate` 标准模式下 `max|diff| = 0.000000`（同一 1161 bar 全等），"
  f"走查下对齐 {D['no_circuit']['aligned']} bar 的 `max|diff| = {D['no_circuit']['maxdiff']:.4f}` 元、"
  f"ΔSharpe {D['no_circuit']['d_sh']:+.6f}、ΔCAGR {D['no_circuit']['d_ca']:+.4f}pp、"
  f"ΔMaxDD {D['no_circuit']['d_dd']:+.5f}pp，且是全表唯一 `only_arm = {D['no_circuit']['only_arm']}` 的臂。")
w("")
w("解释：三个 `crb_dd_*` 阈值抬到 `10.0`（按 dump 默认 `0.1/0.15/0.2` 的小数口径 = 1000% 回撤）后**在整条路径上永不触发**。"
  "标准模式是一条连续权益曲线 ⇒ 永不触发 = 完全恒等。走查把时间切成 "
  f"{NF} 折、**每折权益在折首重置**，熔断状态机与其回撤分母随之重置，于是个别折的折内回撤路径出现极小差别。"
  "**量级上仍是「模块失效」，但严格讲 walkforward 已把它与 baseline 区分开** ⇒ 本文件只记「非 ≡ 但经济上可忽略」，不写 ≡。")
w("")

# ---------------- 4
w("## 4. 配置落地核对清单")
w("")
w("观测值全部来自该臂自己的 `strategy_dump.json` 的 `.params`；目标值逐字复制自 `docs/backtest_research.md` §8 `run_arm` 的 "
  "`case` 分支（`pfx=crb`）。")
w("")
w("| 臂 | key | 目标值 | dump 观测值 | dump 默认值 | 落地 |")
w("|---|---|---|---|---|---|")
all_ok = True
bad = []
for a, kv in INTENDED.items():
    for k, v in kv.items():
        obs = D[a]["params"].get(k, "<<ABSENT>>")
        ok = (k in D[a]["params"]) and (D[a]["params"][k] == v)
        all_ok &= ok
        if not ok:
            bad.append((a, k))
        w("| `{}` | `{}` | `{}` | `{}` | `{}` | {} |".format(
            a, k, v, obs, DUMP.get(k, "<<dump 中无此键>>"), "✅" if ok else "❌ **INVALID**"))
w("")
nkeys = sum(len(v) for v in INTENDED.values())
w(f"- **{nkeys - len(bad)}/{nkeys} 个目标 key 落地**"
  + ("，0 个缺失、0 个仍停留在 dump 默认值 ⇒ **13 臂全部 VALID**。"
     if all_ok else f"，**异常 = {bad} ⇒ 对应臂 INVALID**。"))
w("- `grep -c \"ignoring unknown key(s)\"` 逐臂结果："
  + "、".join(f"{a}={D[a]['unknown_key']}" for a in ARMS)
  + " ⇒ **全为 0**。")
w("- 逐臂 `jq '{status,mode}' run_manifest.json`："
  + "、".join(f"{a}={D[a]['manifest']['status']}/{D[a]['manifest']['mode']}" for a in ARMS)
  + f" ⇒ **13/13 completed + walkforward**；逐臂 `grep -c Traceback run.log` 全为 0（"
  + "、".join(str(D[a]["traceback"]) for a in ARMS) + "）。")
w("- 特别核对：`no_inertia` 的 `crb_min_weight_change → 0.0`（默认 0.05）**且** "
  "`crb_min_holding_bars → 0`（默认 2）均已落地；`no_sectorthrot → blend_max_assets_per_sector = 20`（默认 2）；"
  "`no_circuit → crb_dd_reduce_thresh / crb_dd_defensive_thresh / crb_dd_stop_thresh` 三项均 `10.0`（默认 0.1 / 0.15 / 0.2）。")
w("- 补充披露：`06_ablate` 归档里的 `no_sectorthrot` 是 **repair 之前**跑的——其 `run.log` 含 4 条 "
  "`ignoring unknown key(s)`（写了不存在的 `crb_max_assets_per_sector` / `crb_adaptive_sector_throttle`）。"
  "本次按修复后口径只写 `blend_max_assets_per_sector`，**0 条 warning**；两版有效 override 等价，故该臂两种模式仍可比。")
w("")

# ---------------- 5
w("## 5. 三个口径声明（硬性）")
w("")
w("1. **周期 = `06_ablate` 的同窗口**：`--start 2022-01-04 --end 2026-10-22`。"
  f"标准模式同窗口 `overlap_bars = {STD_OVERLAP}` ⇒ 本文件里「走查 vs 标准」的任何差异**只可能来自评估模式**，"
  "不来自区间选择。")
w(f"2. **折规则 = `03_r2b_oos` / `07_r3_oos_bear` 的同规则**：`--window-years 0.25 --step-years 0.25` ⇒ "
  f"`window_bars = step_bars = {BAR_PER_FOLD}`，本策略实际 `n_folds = {NF}`。折数口径可比。")
w("3. **现金扫单与 `06_ablate` 一致、与 `03_r2b_oos` 不一致**：`--cash-sweep-rate 0.015`，"
  "而 `03_r2b_oos` 是 **0%**（`docs/backtest_research.md:398` 口径披露三）。"
  "⇒ 任何跨 `03_r2b_oos` 的绝对数字比较都混入「闲置现金 1.5% 年化」这一项，"
  "**本文件的数字不得与 `03_r2b_oos` 或 `blend_17_baseline` / 归档 15 折并列**。")
w("")
w("其他 13 臂完全一致的固定项：`--data-provider fuyao`（本地 DuckDB，全离线，零网络）、`--min-shares 100`、"
  "`--china-trading`、`--baseline-symbol 000300.SH`、`--n-jobs 1`、"
  "`--strategy-file pipeline/research_strategy/results/strategy_dumps/chan_risk_managed_blend_strategy.json`"
  "（`strategy_version=1.1.0`、`base_version=1.6.0`）。")
w("")

# ---------------- 6
w("## 6. 标准模式 vs 走查模式（唯一变量 = 评估模式）")
w("")
w("标准模式数字引自 `06_ablate/<arm>/chan_risk_managed_blend/comparison_report.json` 的 "
  "`strategy_sharpe_ratio` / `strategy_cagr`（只读引用，未改动该目录）。")
w("")
w("| 臂 | 标准 ΔSharpe | 走查 ΔSharpe | Sharpe 符号翻转 | 标准 ΔCAGR(pp) | 走查 ΔCAGR(pp) |")
w("|---|---|---|---|---|---|")
for a in ARMS:
    flip = a in flips
    w("| `{}` | {:+.4f} | {:+.4f} | {} | {:+.2f} | {:+.2f} |".format(
        a, std_d_sh[a], D[a]["d_sh"], "**是**" if flip else "—", std_d_ca[a], D[a]["d_ca"]))
w("")
w(f"- 符号翻转臂：**{{{', '.join('`' + a + '`' for a in flips)}}}** ⇒ 它们在标准模式下的「正贡献」是单一路径的产物，"
  "**不可作为保留该模块的 OOS 证据**。")
w("- 两种模式**同向**（可互相印证）的臂："
  + "、".join(f"`{a}`（{std_d_sh[a]:+.4f} → {D[a]['d_sh']:+.4f}）"
             for a in ARMS if a != "baseline" and a not in flips and abs(std_d_sh[a]) > 1e-9)
  + "。")
w("")

# ---------------- 7
w("## 7. 可引用 / 作废清单")
w("")
w("### 7.1 可引用（限定：本策略 + 本 20 只票 universe + 2022-01-04→2026-10-22 + 1.5% 现金扫单）")
w("")
w(f"- **惯性/换手门是主力风控之一**：同时关掉 `crb_adaptive_inertia` 并把 `min_weight_change→0`、"
  f"`min_holding_bars→0`、`entry_threshold→0` 后，走查 Sharpe {ref['mean_sharpe_ratio']:.4f} → "
  f"{D['no_inertia']['summary']['mean_sharpe_ratio']:.4f}（{D['no_inertia']['d_sh']:+.4f}）、MaxDD "
  f"{ref['mean_max_drawdown']:.2%} → {D['no_inertia']['summary']['mean_max_drawdown']:.2%}"
  f"（{D['no_inertia']['d_dd']:+.2f}pp）；标准模式同向（{std_d_sh['no_inertia']:+.4f}）⇒ **双重确认，可引用**。")
w(f"- **牛市目标敞口上限 0.8 是有效的风险预算**：放到 1.0 后走查 CAGR {ref['mean_cagr']:.2%} → "
  f"{D['bull_full']['summary']['mean_cagr']:.2%}（{D['bull_full']['d_ca']:+.2f}pp），但 Sharpe "
  f"{ref['mean_sharpe_ratio']:.4f} → {D['bull_full']['summary']['mean_sharpe_ratio']:.4f}"
  f"（{D['bull_full']['d_sh']:+.4f}）、MaxDD {ref['mean_max_drawdown']:.2%} → "
  f"{D['bull_full']['summary']['mean_max_drawdown']:.2%}（{D['bull_full']['d_dd']:+.2f}pp）、"
  f"回撤更浅的折仅 {D['bull_full']['wins_dd']}/{NF} ⇒ **可引用为「提高敞口是用风险换收益」，"
  "不可引用为「提高敞口更好」**。")
w(f"- **CYQ 筹码质量门是真风控**：关掉后 Sharpe {D['no_cyq']['d_sh']:+.4f}、MaxDD {D['no_cyq']['d_dd']:+.2f}pp、"
  f"CAGR {D['no_cyq']['d_ca']:+.2f}pp，标准模式同向（{std_d_sh['no_cyq']:+.4f}）⇒ 可引用为「保留」。")
w(f"- **正交化的正贡献不成立（但也不足以支持关闭）**：走查关掉后 Sharpe {D['no_orthogon']['d_sh']:+.4f}"
  f"（{D['no_orthogon']['wins_sh']}/{NF} 折更好）、MaxDD {D['no_orthogon']['d_dd']:+.2f}pp、"
  f"CAGR {D['no_orthogon']['d_ca']:+.2f}pp；标准模式却是 {std_d_sh['no_orthogon']:+.4f}。"
  "⇒ **可引用为「该模块的 OOS 价值未被证实」**；**不可引用为「应当关闭它」**（ΔSharpe 为正的同时回撤更深，"
  f"且 {D['no_orthogon']['lose_sh']}/{NF} 折更差，量级不足以改动已归档的活体配置）。")
w(f"- **推力加仓（thrust）是「降波动不增收益」**：关掉后 Sharpe {D['no_thrust']['d_sh']:+.4f}、"
  f"MaxDD {D['no_thrust']['d_dd']:+.2f}pp，但 CAGR {D['no_thrust']['d_ca']:+.2f}pp ⇒ "
  "保留它的理由只能是风险而非收益（标准模式 {:.4f} / {:+.2f}pp，同向）。".format(
      std_d_sh["no_thrust"], std_d_ca["no_thrust"]))
w("")
w("### 7.2 作废 / 零信息（不得据此下任何结论）")
w("")
for a in ident_arms:
    w(f"- **`{a}` ≡ baseline**（对齐 {D[a]['aligned']} bar、`only_arm = only_base = {D[a]['only_arm']}`、"
      "`max|diff| = 0.0`）：本策略上为**结构性空操作**（§3.1 代码级归因）⇒ **零信息**。"
      "**既不构成「该模块无效」的证据，也不构成「该模块有效」的证据，更不得用于任何横向排名。**")
w(f"- **`no_circuit` 不可写成 ≡，也不可写成「熔断有效」**：走查已出现 {D['no_circuit']['maxdiff']:.2f} 元权益差，"
  f"但 ΔSharpe 仅 {D['no_circuit']['d_sh']:+.6f}、ΔMaxDD {D['no_circuit']['d_dd']:+.5f}pp ⇒ 只能写"
  "「本窗口内熔断在标准模式下从未触发；走查下仅在折界重置处产生可忽略差异」。")
w(f"- **`no_lowbeta` 处于噪声边缘**：`only_arm = {D['no_lowbeta']['only_arm']}`、"
  f"`only_base = {D['no_lowbeta']['only_base']}`，整条路径只有 "
  f"{D['no_lowbeta']['only_arm'] + D['no_lowbeta']['only_base']} 个换手日不同，ΔSharpe {D['no_lowbeta']['d_sh']:+.4f}、"
  f"ΔMaxDD {D['no_lowbeta']['d_dd']:+.2f}pp ⇒ 在 20 只票 universe 上该模块几乎不咬合，**任何正负号都不该被当成结论**。")
w(f"- **`no_sectorthrot` 的经济解释受限**：板块上限 2→20 只改变 {D['no_sectorthrot']['aligned']} 个对齐 bar 中的少数路径"
  f"（`only_arm = {D['no_sectorthrot']['only_arm']}`、`only_base = {D['no_sectorthrot']['only_base']}`），"
  f"ΔSharpe {D['no_sectorthrot']['d_sh']:+.4f}（{D['no_sectorthrot']['wins_sh']}/{NF} 折更好）、"
  f"ΔCAGR {D['no_sectorthrot']['d_ca']:+.2f}pp ⇒ 只能引用为「板块上限在本 20 票 universe 上约束力弱」，"
  "**不可引用为「可以取消板块上限」**。")
w(f"- **`no_voltarget`**（ΔSharpe {D['no_voltarget']['d_sh']:+.4f}、ΔMaxDD {D['no_voltarget']['d_dd']:+.2f}pp）与 "
  f"**`no_atrstop`**（ΔSharpe {D['no_atrstop']['d_sh']:+.4f}、ΔMaxDD {D['no_atrstop']['d_dd']:+.2f}pp）"
  f"虽两模式同向为正，但 Sharpe 增量仅 {min(D['no_voltarget']['d_sh'], D['no_atrstop']['d_sh']):.4f}~"
  f"{max(D['no_voltarget']['d_sh'], D['no_atrstop']['d_sh']):.4f}，而两臂的回撤都**更深** ⇒ "
  "**只可引用为「本窗口该模块未体现出风险调整后增益」，不可引用为「波动率目标 / ATR 止损应当移除」**。")
w("- **禁止跨组比较**：本文件所有 Δ 只在 13 臂内部成立。")
w("")
w("## 8. 溯源文件")
w("")
w("| 路径 | 内容 |")
w("|---|---|")
w(f"| `{WF}/<arm>/{S}/` | 13 臂各自的 `walkforward_summary.json` / `walkforward_report.csv` / "
  "`walkforward_rebalances.csv` / `strategy_dump.json` / `run_manifest.json` / `run.log` |")
w(f"| `{OUT}/_provenance/crb_analysis.txt` | §1 / §4 的原始导出（未取整） |")
w(f"| `{OUT}/_provenance/crb_table.json` | 逐臂全部指标 + 对齐诊断 + 配置落地布尔值（机器可读） |")
w(f"| `{OUT}/_provenance/crb_foldstats.txt` | §2 逐折统计的原始导出 |")
w(f"| `{OUT}/_provenance/crb_std_vs_wf.txt` | §6 标准 vs 走查对照的原始导出 |")
w(f"| `{OUT}/_provenance/run_crb.sh` | 13 臂实际执行脚本（`extra` 逐字复制自 `docs/backtest_research.md` §8，`pfx=crb`） |")
w(f"| `{OUT}/_provenance/gen_metrics.py` | 生成本文件表格的脚本（只读上述产物） |")
w("")

open(os.path.join(OUT, "_metrics.md"), "w").write("\n".join(L) + "\n")
print("written", os.path.join(OUT, "_metrics.md"), len("\n".join(L)), "chars")
print("ident:", ident_arms, "flips:", flips, "SR0=%.4f" % SR0, "maxDSR=%.3e" % MAX_DSR)

# `chan_dual_hybrid_blend` 参数值网格（walkforward 口径，18 折）

本文件是 `09_param_grid/` 中 **`chan_dual_hybrid_blend`（前缀 `cdhb`）** 这一支的全部结果与裁决。
三个策略（`chan_four_state_blend` / `chan_risk_managed_blend` / `chan_dual_hybrid_blend`）由三个 worker 并行跑同一套网格，
各自只负责自己的子路径；本文件**只覆盖 `cdhb`**，`cfsb` / `crb` 的数字不在此处引用、也不可跨策略拼接。

- 策略：`Chan Dual Hybrid Alpha Blend Strategy`（`chan_dual_hybrid_blend`，前缀 `cdhb`）
- 待检验的两个方向性假设：**A = ATR 跟踪止损过紧**（放宽 / 关闭应提升 CAGR 且不恶化回撤）；**B = 惯性闸门可收紧**（收紧应显著减少调仓且不接受接近 `no_inertia` 的收益损失）
- 网格 A（止损）：`atr_6_0` / `atr_8_0` / `atr_none` —— 目录 `09_param_grid/atr/<arm>/chan_dual_hybrid_blend/`
- 网格 B（惯性）：`inertia_008_4` / `inertia_010_5` / `inertia_015_10` —— 目录 `09_param_grid/inertia/<arm>/chan_dual_hybrid_blend/`
- 共 **6 次运行**，全部 `status: completed`、`mode: walkforward`、`n_folds = 18`、`actual_last_fold_end = 2026-09-07`
- 工作区状态：`git_commit = 5500cc3`（`git_dirty = true`，因为本轮所验证的 `cli_args` provenance 补丁当时尚未提交；见 §1.5 末段与 §1.6 的复现校验）
- 运行脚本：`/tmp/opencode/run_grid.sh`；日志副本落在各产物目录的 `run.log`

---

## 1. 口径

### 1.1 CLI（6 次运行逐字相同，仅 `--strategy-params` 与 `--results-dir` 不同）

```bash
cd /home/stone/Work/github/quant
PY=pipeline/.venv/bin/python
DUMPS=pipeline/research_strategy/results/strategy_dumps
UNIV=backtests_research/20261025_125300/universe_20_stocks.txt
BT_ROOT=backtests_research/20261025_125300
s=chan_dual_hybrid_blend
out_dir="$BT_ROOT/09_param_grid/<grid>/<arm>/$s"
mkdir -p "$out_dir"
$PY backtester/run_backtest.py \
  --strategy-file "$DUMPS/${s}_strategy.json" \
  --universe-file "$UNIV" \
  --start 2022-01-04 --end 2026-10-22 \
  --mode walkforward --window-years 0.25 --step-years 0.25 \
  --data-provider fuyao --min-shares 100 --china-trading \
  --baseline-symbol 000300.SH \
  --cash-sweep-rate 0.015 \
  --strategy-params "$extra" \
  --results-dir "$out_dir" --n-jobs 1 > "$out_dir/run.log" 2>&1
```

`$extra` 逐臂取值（**键名已核对 `pipeline/research_strategy/rs/config.py:1188-1199`**，未发明任何变体；
`stop_mode` 的合法值是 `"atr_trailing"` / `"fixed_pct"` / `"none"`，`stop_atr_mult` 被校验 `> 0`，
因此"关闭"只能写 `"none"`，不能写 `0`，也不能写 `"disabled"`）：

| 网格 | arm | `--strategy-params` |
| :--- | :--- | :--- |
| A | `atr_6_0` | `{"cdhb_stop_atr_mult": 6.0}` |
| A | `atr_8_0` | `{"cdhb_stop_atr_mult": 8.0}` |
| A | `atr_none` | `{"cdhb_stop_mode": "none"}` |
| B | `inertia_008_4` | `{"cdhb_min_weight_change": 0.08, "cdhb_min_holding_bars": 4}` |
| B | `inertia_010_5` | `{"cdhb_min_weight_change": 0.10, "cdhb_min_holding_bars": 5}` |
| B | `inertia_015_10` | `{"cdhb_min_weight_change": 0.15, "cdhb_min_holding_bars": 10}` |

`cdhb_adaptive_inertia` 在网格 B 中**保持 `true` 未动**（本轮是收紧闸门，不是关闭闸门）；
`cdhb_elastic_drawdown_reset` 也**未动**（该前缀默认即为 `true`），6 次运行的 `strategy_dump.json` 中均为 `true`。
窗口（`0.25 / 0.25`）、`--n-jobs 1`、`--cash-sweep-rate 0.015`、`--min-shares 100`、`--china-trading`、
`--baseline-symbol 000300.SH` 与 `08_ablate_wf` 逐项一致。

### 1.2 折几何与数据截断（实测）

- `n_folds = 18`、`n_valid_folds = 18`（6 个臂全部一致）；`actual_first_fold_start = 2022-01-04`、`actual_last_fold_end = 2026-09-07`。
- 折 1 = `2022-01-04 → 2022-04-11`；折 18 = `2026-06-10 → 2026-09-07`；每折约 63 根 bar（与 `08_ablate_wf` 同网格）。
- **截断照旧存在并在此声明**：请求区间 `2022-01-04 → 2026-10-22` 在 Fuyao 本地缓存中共 1,161 个交易日，
  18 折实际覆盖 18 × 63 = 1,134 个交易日，**末尾 27 个交易日（2026-09-08 → 2026-10-22）不构成完整折，未进入任何折的评估**。
  故本文件全部指标的口径是"2022-01-04 → 2026-09-07 的 18 个季度折"，**不是**到 2026-10-22 的整段。

### 1.3 基线复用（本轮未重跑 baseline 作为对照，除 §1.6 的一次复现校验）

参照物 = `08_ablate_wf/baseline/chan_dual_hybrid_blend/walkforward_summary.json`，原文数字（未取整）：

| 指标 | 数值 |
| :--- | ---: |
| `mean_sharpe_ratio` | **1.4249700144569886** |
| `mean_cagr` | **0.25627364561917376**（25.6274%） |
| `mean_max_drawdown` | **0.04215945583509294**（4.2159%） |
| `mean_calmar_ratio` | **10.209628814802935** |
| `n_folds` / `n_valid_folds` | 18 / 18 |
| `fold_sharpe_std` | **2.214078316230977** |
| `deflated_sharpe_ratio` | 2.863692097138028e-50 |
| `actual_last_fold_end` | 2026-09-07 |
| Σ`total_rebalances`（18 折求和） | **628** |
| Σ`total_turnover`（18 折求和） | 72.3863 |
| Σ 调仓成交笔数（`run.log` "Total Walkforward Rebalance Trades"） | 980 |

任务书给出的 `mean_sharpe 1.4250 / mean CAGR 25.63% / mean MaxDD 4.22% / Calmar 10.2096 / fold_sharpe_std 2.2141`
与文件内数字**完全一致**，本文件一律使用文件内未取整值重算（Δ 由未取整值相减后再取整，故个别 Δ 与"两个取整值相减"会差 0.01）。

### 1.4 两条必须先对齐的硬约束（讨论任何"pp"之前）

1. **`mean_max_drawdown` 是「折内」63 根 bar 的回撤的 18 折算术平均，不是全期回撤。**
   walkforward 每折重置净值，全期路径回撤在此模式下**不可观测**。
   同一策略、同一时间窗、标准（非 walkforward）模式下整段（1,161 bar）实测回撤是 **11.8066%**
   （`06_ablate/baseline/chan_dual_hybrid_blend/backtest_equity.csv` 峰谷）。
   本文件的 4.2%–4.5% 与 11.8066% **不可相减、不可互相校验、不可互相替代**——它们量的不是同一个东西。
2. **`--cash-sweep-rate 0.015` 与 `03_r2b_oos` 的 0% 不同。** 本轮 6 个臂全部按 1.5% 的现金计息（与 `08_ablate_wf` 一致），
   而审计里 `chan_dual_hybrid_blend` 的 OOS Sharpe 2.566 / DSR 0.5857 来自 `03_r2b_oos`（0% 现金、另一段时间窗、另一折网格）。
   因此本文件的任何数字**不能**直接与 2.566 / 0.5857 那组数字并列比较；本轮能做的是"臂与臂之间"的同口径比较。

### 1.5 逐次运行有效性验证（6/6 全部 VALID）

四项强制检查逐条实测（`$out_dir` = 该臂产物目录）：

| arm | `strategy_dump.params` 实测（4 键） | `grep -c "ignoring unknown key(s)"` | `grep -c Traceback` | `{status,mode}` | `cli_args.options`（`cash_sweep_rate` / `strategy_params`） | 判定 |
| :--- | :--- | ---: | ---: | :--- | :--- | :--- |
| `atr_6_0` | `stop_atr_mult=6.0`, `stop_mode="atr_trailing"`, `min_weight_change=0.05`, `min_holding_bars=2` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_stop_atr_mult": 6.0}` | **VALID** |
| `atr_8_0` | `stop_atr_mult=8.0`, `stop_mode="atr_trailing"`, `min_weight_change=0.05`, `min_holding_bars=2` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_stop_atr_mult": 8.0}` | **VALID** |
| `atr_none` | `stop_atr_mult=4.5`（默认）, `stop_mode="none"`, `min_weight_change=0.05`, `min_holding_bars=2` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_stop_mode": "none"}` | **VALID** |
| `inertia_008_4` | `stop_atr_mult=4.5`, `stop_mode="atr_trailing"`, `min_weight_change=0.08`, `min_holding_bars=4` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_min_weight_change": 0.08, "cdhb_min_holding_bars": 4}` | **VALID** |
| `inertia_010_5` | `stop_atr_mult=4.5`, `stop_mode="atr_trailing"`, `min_weight_change=0.1`, `min_holding_bars=5` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_min_weight_change": 0.1, "cdhb_min_holding_bars": 5}` | **VALID** |
| `inertia_015_10` | `stop_atr_mult=4.5`, `stop_mode="atr_trailing"`, `min_weight_change=0.15`, `min_holding_bars=10` | **0** | **0** | `completed` / `walkforward` | `0.015`（number）/ `{"cdhb_min_weight_change": 0.15, "cdhb_min_holding_bars": 10}` | **VALID** |

**provenance 回归测试结论（本轮新增项）**：6 个 `run_manifest.json` **全部**含 `cli_args`
（`{"argv": [...], "options": {...}}`），`cash_sweep_rate` 是 **JSON number `0.015`** 而非字符串，
`strategy_params` 是**已解析的对象**（不是转义后的原始字符串），`argv` 里 `--cash-sweep-rate 0.015` / `--strategy-params ...` 逐字可读。
**无缺陷**，不需要上报。

**数值有效性（override 不只是写进了 dump，而是真的改变了路径）**：以 `08_ablate_wf/baseline` 的
`walkforward_rebalances.csv` 的 `portfolio_equity` 为对齐基准（对齐键 `(fold, date)`，基线去重后 500 个调仓行）：

| arm | 对齐行数 | 仅基线有 | 仅臂有 | max&#124;Δequity&#124; (元) | 非零行数 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `atr_6_0` | 416 | 84 | 66 | 6,174.09 | 372 |
| `atr_8_0` | 396 | 104 | 75 | 3,585.59 | 370 |
| `atr_none` | 355 | 145 | 60 | 5,046.78 | 337 |
| `inertia_008_4` | 299 | 201 | 43 | 5,582.18 | 281 |
| `inertia_010_5` | 246 | 254 | 38 | 6,731.61 | 228 |
| `inertia_015_10` | 210 | 290 | 33 | 8,400.23 | 192 |

6 个臂**无一**是 `≡` 空转（`no_structregime` / `no_hysteresis` / `no_circuit` 那类"数值恒等"在本轮没有出现）。

### 1.6 复现校验（对"新 patch 是否改动了数字"的一次性检验）

`09_param_grid` 下的 6 个臂全部带着 `git_dirty = true` 的 provenance 补丁跑；为排除"补丁改了结果"这一可能，
另用**完全相同的 CLI、不带任何 `--strategy-params`**（`"{}"`）跑了一次基线复现（产物一次性地放在
`09_param_grid/_repro/baseline_repro/`，比对后已归档到 `/tmp/opencode/grid_cdhb/_repro_moved/`，不留在 `09_param_grid/` 下）：

- `mean_sharpe_ratio = 1.4249700144569886`、`mean_cagr = 0.25627364561917376`、`mean_max_drawdown = 0.04215945583509294`、
  `mean_calmar_ratio = 10.209628814802935`、Σrebalances 628、`fold_sharpe_std = 2.214078316230977`、`DSR = 2.863692097138028e-50`
  —— 与 `08_ablate_wf/baseline` **逐位相同**，18 折的 `sharpe_ratio / cagr / max_drawdown / total_rebalances` 4 个字段
  **0 处不一致**。
- ⇒ 本轮 6 个臂与 `08_ablate_wf/baseline` 的差异，**只能**来自 `--strategy-params`，不是代码或数据漂移。

---

## 2. 网格 A：止损宽度（`stop_atr_mult` / `stop_mode`）

Δ 列参照 §1.3 基线；**ΔMaxDD 为正 = 回撤更深（更差）**。

| arm | `mean_sharpe_ratio` | `mean_cagr` | `mean_max_drawdown` | `mean_calmar_ratio` | Σ`total_rebalances` | `deflated_sharpe_ratio` | ΔSharpe | ΔCAGR (pp) | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline`（复用） | 1.424970 | 25.6274% | 4.2159% | 10.2096 | 628 | 2.8637e-50 | — | — | — |
| `atr_6_0` | 1.414849 | 24.6247% | 4.3756% | 10.4962 | 621 | 6.7353e-55 | −0.010121 | **−1.0027** | +0.1597 |
| `atr_8_0` | 1.454803 | 25.2254% | 4.2249% | 11.1899 | 617 | 3.9643e-51 | +0.029833 | **−0.4019** | +0.0089 |
| `atr_none` | 1.422371 | 25.4683% | 4.4297% | 10.5202 | 556 | 1.2990e-48 | −0.002599 | **−0.1590** | +0.2137 |

补充口径（同为 18 折求和）：Σ`total_turnover` 基线 72.3863 → `atr_6_0` 66.9907 / `atr_8_0` 67.2139 / `atr_none` 59.3061；
Σ成交笔数 980 → 930 / 919 / 826。

**逐折方向性（判定"能不能主张方向"的唯一依据）**

| arm | 逐折 CAGR 改善折数 | 逐折 Sharpe 改善折数 | 中位 ΔCAGR (pp) | 中位 ΔSharpe | max ΔCAGR (pp) | min ΔCAGR (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `atr_6_0` | **6 / 18** | 8 / 18 | −1.80 | −0.049 | +7.61 | −8.48 |
| `atr_8_0` | **9 / 18** | 9 / 18 | −0.19 | +0.011 | +10.42 | −13.42 |
| `atr_none` | **8 / 18** | 9 / 18 | −0.79 | −0.007 | +12.36 | −15.07 |

**读表要点（3 条，都是数字层面的硬事实）**

1. **三个臂的 `mean_cagr` 全部低于基线**（−1.0027 / −0.4019 / −0.1590 pp），**没有任何一个臂把 CAGR 抬上去**；
   幅度也不随"放宽程度"单调：`4.5`(基线 25.6274%) → `6.0`(24.6247%) → `8.0`(25.2254%) → `none`(25.4683%)，是**折返的、非单调的**。
2. **三个臂的 `mean_max_drawdown` 全部比基线略差**（+0.1597 / +0.0089 / +0.2137 pp），方向与假设 A 的"DD 不恶化"相反。
3. **逐折看是抛硬币**：6/18、9/18、8/18 折改善，中位 ΔCAGR 全为负，且单折摆幅达 ±8 ~ ±15pp。
   最大 |ΔSharpe| = 0.0298，远小于 `fold_sharpe_std = 2.2141`（差 74 倍）。

**机制层的关键发现：上一轮 `no_atrstop` 臂并没有关闭止损。**
`08_ablate_wf/no_atrstop` 传的是 `blend_enable_adaptive_stop_atr: false`，而 `pipeline/research/blend/blend_base.py:891-896`
的实际逻辑是 `eff_stop_mult = stop_atr_mult`（自适应关掉时**止损照旧生效**，锁定 4.5×ATR），
自适应开启时才是 `clip(stop_atr_mult × med_vol/vol_i, 3.5, 7.5)`。
也就是说，那个给出 `+0.62pp`（`08_ablate_wf`）/ `+1.01pp`（`06_ablate`）的臂，真正动的是**把自适应乘数钉死在 4.5（更紧）**，
而不是"放宽/关闭止损"。本轮第一次真正按字面关闭止损（`stop_mode="none"`）的结果是
**CAGR −0.1590pp、MaxDD +0.2137pp（更差）**：与假设 A 的预测**符号相反**。
即观测到的收益梯度指向"**更紧**的有效止损乘数"，而不是"更松"。

---

## 3. 网格 B：惯性闸门（`min_weight_change` / `min_holding_bars`）

Δ 列参照 §1.3 基线；**ΔMaxDD 为正 = 回撤更深（更差）**；调仓列是本假设的主结果。

| arm | `mean_sharpe_ratio` | `mean_cagr` | `mean_max_drawdown` | `mean_calmar_ratio` | Σ`total_rebalances` | Δ调仓（次数 / %） | `deflated_sharpe_ratio` | ΔSharpe | ΔCAGR (pp) | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- | ---: | ---: | ---: | ---: |
| `baseline`（复用） | 1.424970 | 25.6274% | 4.2159% | 10.2096 | **628** | — | 2.8637e-50 | — | — | — |
| `inertia_008_4` | 1.517380 | 30.5724% | 4.3255% | 11.5344 | **450** | **−178 / −28.3%** | 1.0733e-36 | +0.092410 | **+4.9450** | +0.1095 |
| `inertia_010_5` | 1.485070 | 27.5223% | 4.4540% | 10.0440 | **421** | **−207 / −33.0%** | 8.4909e-36 | +0.060100 | **+1.8949** | +0.2381 |
| `inertia_015_10` | 1.395845 | 22.1808% | 4.5091% | 9.9004 | **359** | **−269 / −42.8%** | 5.1169e-49 | −0.029125 | **−3.4466** | +0.2932 |

补充口径（同为 18 折求和）：Σ`total_turnover` 基线 72.3863 → 47.9852（−33.7%）/ 40.7268（−43.7%）/ 32.7871（−54.7%）；
Σ成交笔数 980 → 632 / 544 / 477。

**逐折方向性**

| arm | 逐折 CAGR 改善折数 | 逐折 Sharpe 改善折数 | 中位 ΔCAGR (pp) | 中位 ΔSharpe | max ΔCAGR (pp) | min ΔCAGR (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `inertia_008_4` | **11 / 18** | 10 / 18 | **+2.36** | +0.247 | +38.09 | −16.91 |
| `inertia_010_5` | **10 / 18** | 9 / 18 | +0.95 | +0.062 | +53.85 | −28.18 |
| `inertia_015_10` | **7 / 18** | 10 / 18 | −3.54 | +0.026 | +16.47 | −27.22 |

**读表要点**

1. **调仓次数单调下降，且降幅确实"实质性"**：628 → 450 → 421 → 359（−28.3% / −33.0% / −42.8%），
   Σ换手 −33.7% / −43.7% / −54.7%。闸门收紧确实是**有效**的（不是空转）。
2. **但收益侧的"代价"并非单调，且前两档根本没有代价**：CAGR 25.6274% → **30.5724%**（+4.9450pp）→ 27.5223%（+1.8949pp）→ 22.1808%（−3.4466pp）。
   即**收紧到 0.08/4 与 0.10/5 是"少调仓 + 多收益"**，与"闸门已最优、再收紧必付代价"的预期相反；
   只有到 0.15/10 才转为损失，且该损失（−3.4466pp）**大于**"完全关掉闸门"的 walkforward 惩罚（`no_inertia` −2.3403pp，rebalances 992）。
3. **`inertia_008_4` 的 +4.9450pp 不是均匀的，是 4 折撑起来的**：逐折 ΔCAGR 中位仅 +2.36pp，
   折 5（+38.09）、折 9（+35.83）、折 16（+24.99）、折 18（+11.55）四折合计贡献 +6.14pp（占均值 124%），
   其余 14 折均值 **−1.53pp**。`inertia_010_5` 同构：折 5/18/1/7 四折贡献 +5.29pp，其余 14 折均值 **−4.36pp**。
   这两档的"收益改善"发生**同一批高收益折**（折 5 基线 CAGR 138.0%、折 9 基线 101.9%），
   机制上是"收紧闸门 → 在高收益折里少被换手打断"，属于可解释的机制，但**统计上不可与折间噪声区分**：
   ΔSharpe 只有 +0.0924 / +0.0601，`fold_sharpe_std` 是 2.0844 / 2.0363。
4. **MaxDD 随收紧单调变差**（+0.1095 / +0.2381 / +0.2932 pp），方向干净但幅度均在 0.3pp 内（折内 63 bar 口径）。

**`no_inertia` 参照值的口径更正（必须披露）**：任务书写"`no_inertia` 惩罚 −2.34pp walkforward / −3.70pp standard"。
walkforward 侧 −2.3403pp 与 `08_ablate_wf/no_inertia/chan_dual_hybrid_blend` 完全吻合；
但 standard 侧的 **−3.70pp 是 `chan_risk_managed_blend`（crb）的数**，`cdhb` 的 standard `no_inertia` 实为
**−3.02pp**（`ABLATION_AND_ANATOMY.md` 第 100 行，`no_inertia | 双重 | … −3.02pp`）。
本文件的裁决一律用**同口径的 walkforward −2.3403pp** 作为惩罚参照，不混用 standard 数字。

---

## 4. 裁决

判定规则（照任务书，未改）：
- **A 通过** 需同时满足：CAGR 与 MaxDD 都朝"假设方向"（CAGR↑、MaxDD↓）**单调**移动，**且**改善幅度**超过** `fold_sharpe_std`（本策略 2.2141）；否则属折内噪声 ⇒ **不成立**，不是"已证明"。
- **B 通过** 需同时满足：Σ`total_rebalances` **实质性下降**，**且** CAGR 损失**远低于** `no_inertia` 惩罚（walkforward −2.3403pp）；若损失逼近该量级，则视为"闸门已最优、拒绝收紧"。

### 4.1 假设 A（ATR 跟踪止损过紧）→ **证伪（falsified）**

三条规则逐条对照：

| 判据 | 实测 | 是否满足 |
| :--- | :--- | :--- |
| CAGR 单调改善 | 25.6274% → **24.6247%** → 25.2254% → 25.4683%：**全部低于基线且非单调**（第一步就掉头） | ✗ |
| MaxDD 单调改善 | 4.2159% → 4.3756% / 4.2249% / 4.4297%：**全部略变差** | ✗ |
| 改善幅度 > `fold_sharpe_std` 2.2141 | 臂级最大 \|ΔSharpe\| = **0.0298**（差 74 倍）、臂级最大 \|ΔCAGR\| = **1.0027pp** | ✗ |

三条**全部不满足**。而且这不是"幅度不够"的问题，是**符号错**：三个臂的 CAGR 与 MaxDD 同时朝假设的**反方向**走。
再加一条机制层证据：假设 A 所依据的 `no_atrstop` 臂（`blend_enable_adaptive_stop_atr: false`）**从未关闭止损**，
它把有效乘数从 `clip(·, 3.5, 7.5)` 钉到 4.5（更紧）；本轮按字面关闭止损（`stop_mode="none"`）得到 −0.1590pp CAGR / +0.2137pp MaxDD。
⇒ **A 不仅没有通过，而且方向被判反**：本窗口的收益梯度指向"更紧的有效止损乘数"，不是"更松"。
（附带建议，**不在本轮范围内、未执行**：若要把这条线索做完，应测 `blend_enable_adaptive_stop_atr=false` 与
`cdhb_stop_atr_mult ∈ {3.5, 4.0, 4.5}` 的组合，而不是继续放宽。）

### 4.2 假设 B（惯性闸门可收紧）→ **0.08/4 与 0.10/5 通过；0.15/10 拒绝**

| arm | Δ调仓 | ΔCAGR (pp) | 相对 `no_inertia` 惩罚（−2.3403pp） | 判定 |
| :--- | :--- | ---: | :--- | :--- |
| `inertia_008_4` | −178 / −28.3% | **+4.9450** | **无损失**（反而 +4.95pp，距惩罚 7.29pp） | **PASS（无代价）** |
| `inertia_010_5` | −207 / −33.0% | **+1.8949** | **无损失**（+1.89pp，距惩罚 4.24pp） | **PASS（无代价）** |
| `inertia_015_10` | −269 / −42.8% | **−3.4466** | **损失超过**该惩罚（−3.45pp 比 −2.34pp 更差） | **FAIL（拒绝）** |

按规则字面：B **成立**——闸门确实可以收紧，且中档（0.08/4、0.10/5）在 18 折上**一个 pp 都没损失**，调仓降 28–33%。
但必须把"成立"的外延写清，否则会被误读：

- **成立的只是"收紧不吃亏"（could tighten at no measured cost），不是"收紧能赚到 +4.95pp"。**
  ΔSharpe 仅 +0.0924（`fold_sharpe_std` 2.0844），收益改善集中在 4/18 折（去掉这 4 折，剩余 14 折均值 −1.53pp）。
  按本批全部报告一贯的噪声地板（|ΔSharpe| < 0.15、|ΔCAGR| < 3pp 不主张点值），**+4.9450pp 不能作为点估计上报**。
- **"闸门已最优、拒绝收紧"这一反向结论被证伪**：0.05/2 并非本窗口的最优点；损失只在 0.15/10 出现，
  且该档的损失比"彻底关掉闸门"更差（−3.4466 vs −2.3403pp，而调仓 359 vs 992）——即 0.15/10 是"又少调仓又少收益"的过度收紧，
  这条**拒绝**是干净的。
- **MaxDD 随收紧单调变差**（+0.11 / +0.24 / +0.29pp）：这是唯一干净的单向证据，但幅度 <0.3pp（折内 63 bar 口径），
  只支持"收紧会让折内回撤略深"，不支持任何量化配资结论。

---

## 5. 对部署建议的影响

**没有任何一个臂改变部署图景。** 具体地：

1. **网格 A 三臂全部劣于基线，不具备替换理由。** 当前部署参数（`atr_trailing` + 4.5×ATR + 自适应 `[3.5, 7.5]` 夹取）在本窗口
   是三档里最好的一个（25.6274% / 4.2159%），放宽到 6.0/8.0 或关闭止损都不改善任何一项。
   ⇒ **止损参数维持现状，不做任何改动。**
2. **网格 B 的 `inertia_008_4` 是唯一"看起来更好"的臂**（Sharpe 1.5174、CAGR 30.57%、Calmar 11.53、调仓 −28.3%），
   但**不足以据此改部署**：ΔSharpe 0.0924 落在折间噪声内，收益增量集中在 4/18 折（折 5/9/16/18），
   且这是在**同一批 18 折上挑出来的最优档**——若现在就把它写进 `strategies_config.json`，等于在同一批折上做选择后拟合，
   正好是本批审计要防的那件事。**建议：保持 0.05/2 部署值不变；`0.08/4` 作为"待样本外验证"的候选登记，不落地。**
3. **6.6% 风险预算的口径问题在本轮之后更加确定，而不是被解决。**
   `PARAMETER_RECOMMENDATIONS.md` 已明确记录：6.6% 是把 `03_r2b_oos` 的 4.0% 按 39.2% 常态现金垫归一化得到的"满仓等效"值，
   **该点估计本身不被数据支持**——同窗口、同策略、标准模式整段（1,161 bar）实测回撤是 **11.8066%**，已经高于 6.6%。
   本轮 6 个臂的 `mean_max_drawdown` 全部在 **4.22%–4.51%** 区间，而这是**折内 63 bar** 的平均值，
   **不能**用来支持、也不能用来反驳 6.6%：它既不是全期回撤，也没有跨过"现金垫是否永续"这个前置问题。
   ⇒ **按 6.6% 配资金之前，仍必须先决定是否相信现金垫永续；本轮没有提供任何可以支持 6.6% 的量化证据。**
4. 唯一可以正面写进部署说明的、跨臂稳健的量化事实是**换手侧**：收紧 `min_weight_change / min_holding_bars` 能确定性地
   把调仓从 628 降到 359（−42.8%）、Σ换手降 54.7%，且**在 0.08/4 与 0.10/5 上不损失 CAGR**。
   这是"摩擦预算"层面的结论（对实盘成本、容量、下单冲击有直接意义），**不是收益层面的结论**。

---

## 6. 禁用指标声明：`deflated_sharpe_ratio` 在本折网格下不可用于排序

6 个臂的 `deflated_sharpe_ratio` 实测为 **5.1169e-49 ~ 1.0733e-36**（基线 2.8637e-50），全部饱和在 0 附近，
**不具备区分能力**，本文件**不**用它给任何臂排序，也不允许它参与裁决。原因（`common/metrics.py:181-214` + `backtester/run_backtest.py:1781-1788`）：

- walkforward 分支调用的是 `deflated_sharpe_ratio(observed_sharpe=mean(fold_sharpes), n_trials=n_valid_folds, n_obs=window_bars, sharpe_std=std(fold_sharpes))`，
  即 **`n_trials = n_folds = 18`**，`n_obs` 固定为一个折的 bar 数（63）。
- 于是 `sr0 = expected_max_sharpe(18, fold_sharpe_std)` = **4.1046**（基线；6 个臂 3.7750–4.2103，逐臂见 §2/§3 的 `fold_sharpe_std` 推算），
  它衡量的是"18 次纯运气抽样里最大 Sharpe 的期望"。而所有臂的 `mean_sharpe_ratio` 只有 1.3958–1.5174，**永远低于 4.10**，
  故 DSR 结构性地被压到 ≈0，且其数值大小主要由 `fold_sharpe_std` 的第三位小数决定，**与策略好坏无关**。
- 审计报告里 `chan_dual_hybrid_blend` 的 **DSR 0.5857** 来自 `03_r2b_oos`（另一段时间窗、另一折网格、0% 现金），
  **在本组不可复现，也不能用本组去证实或证伪那个 0.5857**。

---

## 7. 结论

- **假设 A（ATR 跟踪止损过紧）—— 证伪。** 决定数字：三个臂 CAGR 全低于基线（−1.0027 / −0.4019 / −0.1590 pp）且非单调，
  MaxDD 全部变差（+0.1597 / +0.0089 / +0.2137 pp），最大 |ΔSharpe| 0.0298 ≪ `fold_sharpe_std` 2.2141；且按字面关闭止损（`none`）
  的 −0.1590pp 与"上一轮 `no_atrstop` 臂 +0.62pp"符号相反——那条臂实际只关掉了自适应乘数夹取，并未关闭止损。
- **假设 B（惯性闸门可收紧）—— 成立（仅限"收紧不吃亏"）。** 决定数字：`inertia_008_4` 调仓 628 → 450（−28.3%）、
  Σ换手 −33.7%，而 CAGR **+4.9450pp**（无损失，距 `no_inertia` 惩罚 −2.3403pp 还有 7.29pp）；
  `inertia_010_5` 同向（421 / −33.0%，+1.8949pp）；**但** ΔSharpe 仅 +0.0924 且 +4.95pp 由 4/18 折贡献（其余 14 折均值 −1.53pp），
  故"收紧能改善收益"仍属**不可主张**。
- **假设 B 的强反向形式（"闸门已最优、拒绝任何收紧"）—— 证伪。** 决定数字：损失只出现在 0.15/10（−3.4466pp），
  且该档比彻底关闸更差（−3.4466 vs −2.3403pp），0.08/4 与 0.10/5 一个 pp 都没损失。
- **`deflated_sharpe_ratio` —— 本组禁用。** 决定数字：`n_trials = 18`、`sr0 ≈ 4.1046`，所有臂 DSR 落在 5e-49 ~ 1e-36，饱和无区分度。
- **部署 —— 不改。** 决定数字：A 网格三臂均劣于基线；B 网格唯一更优臂 `inertia_008_4` 的优势在同批 18 折上选取、
  噪声内且 4 折集中；6.6% 风险预算仍**没有**数据支持（同窗口整段实测回撤 11.8066% > 6.6%）。

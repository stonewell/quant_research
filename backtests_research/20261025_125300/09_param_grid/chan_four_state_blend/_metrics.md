# `chan_four_state_blend`（`cfsb`）— 参数值网格（09_param_grid）

- **组**：`backtests_research/20261025_125300/09_param_grid/`
- **策略**：`chan_four_state_blend`（前缀 `cfsb`，dump 为 `pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json`）
- **网格**：A（ATR 追踪止损，3 臂）+ B（惯性闸门，3 臂）= **6 次运行**，全部 `--mode walkforward`
- **数据**：`--data-provider fuyao`（本地 `data/cache.duckdb` 只读，**全离线、零网络**）；股池 `universe_20_stocks.txt`（20 只 A 股）；`--min-shares 100 --china-trading --baseline-symbol 000300.SH`
- **驱动脚本**：`/tmp/opencode/grid_cfsb/run_cfsb_grid.sh`；总日志 `/tmp/opencode/grid_cfsb/driver.log`；抽取脚本 `/tmp/opencode/grid_cfsb/analyze.py` → `analysis.json`
- **本文所有数字均直接读自各目录落盘产物**（`walkforward_summary.json` / `strategy_dump.json` / `run_manifest.json` / `run.log`），**不采用任何转述值**。基准臂全部数字同样从 `08_ablate_wf/baseline/chan_four_state_blend/walkforward_summary.json` 重读。

> **与任务简报的一处数字出入（以落盘文件为准）**：简报里写的惯性臂再平衡笔数「442 → 839」在本策略的 walkforward 产物中**不存在**。本目录实测为 **535 → 970**（`08_ablate_wf` 的 `baseline` 与 `no_inertia` 的逐折 `total_rebalances` 求和）。本文一律采用文件值。

---

## 1. 口径

### 1.1 命令行（逐字）

6 次运行唯一变化的只有 `--strategy-params` 与 `--results-dir`，其余完全一致：

```
pipeline/.venv/bin/python backtester/run_backtest.py \
  --strategy-file pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json \
  --universe-file backtests_research/20261025_125300/universe_20_stocks.txt \
  --start 2022-01-04 --end 2026-10-22 \
  --mode walkforward --window-years 0.25 --step-years 0.25 \
  --data-provider fuyao --min-shares 100 --china-trading \
  --baseline-symbol 000300.SH \
  --cash-sweep-rate 0.015 \
  --strategy-params "$extra" \
  --results-dir backtests_research/20261025_125300/09_param_grid/<grid>/<arm>/chan_four_state_blend \
  --n-jobs 1
```

`$extra` 逐臂取值（**键名取自 `pipeline/research_strategy/rs/config.py:590/608/609/613`，无一字改动**）：

| 网格 | 臂 | `$extra` |
| :--- | :--- | :--- |
| A | `atr_6_0` | `{"cfsb_stop_atr_mult": 6.0}` |
| A | `atr_8_0` | `{"cfsb_stop_atr_mult": 8.0}` |
| A | `atr_none` | `{"cfsb_stop_mode": "none"}` |
| B | `inertia_008_4` | `{"cfsb_min_weight_change": 0.08, "cfsb_min_holding_bars": 4}` |
| B | `inertia_010_5` | `{"cfsb_min_weight_change": 0.10, "cfsb_min_holding_bars": 5}` |
| B | `inertia_015_10` | `{"cfsb_min_weight_change": 0.15, "cfsb_min_holding_bars": 10}` |

**未设 `--market-impact` / `--max-volume-participation`**（与 `08_ablate_wf` 一致，故不构成差异来源）。
网格 B 的 `cfsb_adaptive_inertia` **保持默认 `true` 不变**——本网格是「收紧闸门」，不是「拆除闸门」。

### 1.2 折几何（18 折，实测）

`window_bars = step_bars = int(round(0.25 × 252)) = 63`，`0.25y` 窗 / `0.25y` 步 ⇒ 折不重叠、逐折重置净值。

| 项 | 实测值 | 证据 |
| :--- | :--- | :--- |
| 请求区间 bar 数 | **1,161** | 与 `08_ablate_wf` / `06_ablate` 同窗（`comparison_report.json` 的 `overlap_bars = 1161`） |
| 折数 | **18** | 6/6 次运行 `n_folds = 18`、`n_valid_folds = 18` |
| 进入折内评分的 bar 数 | **1,134** = 18 × 63 | 同上 |
| 首折 | `2022-01-04 → 2022-04-11` | 6/6 的 `actual_first_fold_start = 2022-01-04` |
| 末折 | `2026-06-10 → 2026-09-07` | 6/6 的 `actual_last_fold_end = 2026-09-07` |
| 不入任何折的尾部 bar | **27** 根，`2026-09-08 → 2026-10-22` | 1,161 − 1,134 = 27 |
| 折被丢弃数 | **0** | `n_folds = n_valid_folds` |

⇒ **实际评分区间是 `2022-01-04 → 2026-09-07`（18 个不重叠季度折），不是到 `2026-10-22`。**

### 1.3 基准复用：`08_ablate_wf/baseline`

本网格**不重跑 baseline**，直接复用 `08_ablate_wf/baseline/chan_four_state_blend/`（同一 CLI、同一窗口、同一 `--cash-sweep-rate 0.015`、同 18 折、同首末折日期）。重读其 `walkforward_summary.json`：

| 指标 | 值 |
| :--- | ---: |
| `mean_sharpe_ratio` | **1.432870** |
| `mean_cagr` | **30.1513%** |
| `mean_max_drawdown` | **4.6877%** |
| `mean_calmar_ratio` | **10.913502** |
| `total_rebalances`（18 折求和） | **535** |
| `fold_sharpe_std` | **2.283648** |
| `deflated_sharpe_ratio` | 1.987444e-54 |
| `n_folds` / `n_valid_folds` | 18 / 18 |
| `actual_last_fold_end` | 2026-09-07 |

### 1.4 两条必须随数字一起引用的口径披露

1. **`mean_max_drawdown` 是「折内」63 根 bar 的最大回撤的 18 折算术平均，不是全期（1,161 根）最大回撤。** walkforward 每折重置净值，**全期回撤在此模式下不可观测**。因此本表的 ΔMaxDD 只能读作「折内回撤是否变深」，**绝不可**与 `06_ablate` / `03_r2b_oos` 的全路径回撤相减或互相校验。折内 MaxDD 的折间标准差为 **2.08 pp**（baseline 逐折，`min 1.67% / max 8.53%`），这本身就是 ΔMaxDD 的比较基准。
2. **`--cash-sweep-rate 0.015` 与 `03_r2b_oos` 的 0% 不同。** 本网格所有臂（含基准）都按闲置现金 1.5% 年化计息；`03_r2b_oos`（`2025-01-02 → 2026-10-22`，6 折）用的是 0%。两者**区间与折网格亦不同**，任何数字都不得跨组并列或合并。

---

## 2. 逐次运行的落地自检（6/6 全部有效）

每条覆盖必须真的进到 `StrategyConfig`，否则该臂是**假结果**。逐项实测：

| 网格 | 臂 | `strategy_dump.json` 的 `params`（`stop_atr_mult` / `stop_mode` / `min_weight_change` / `min_holding_bars`） | `ignoring unknown key(s)` 命中 | `Traceback` 命中 | `status` / `mode` |
| :--- | :--- | :--- | :---: | :---: | :--- |
| A | `atr_6_0` | **6.0** / `atr_trailing` / 0.05 / 2 | **0** | **0** | `completed` / `walkforward` |
| A | `atr_8_0` | **8.0** / `atr_trailing` / 0.05 / 2 | **0** | **0** | `completed` / `walkforward` |
| A | `atr_none` | 4.5 / **`none`** / 0.05 / 2 | **0** | **0** | `completed` / `walkforward` |
| B | `inertia_008_4` | 4.5 / `atr_trailing` / **0.08** / **4** | **0** | **0** | `completed` / `walkforward` |
| B | `inertia_010_5` | 4.5 / `atr_trailing` / **0.10** / **5** | **0** | **0** | `completed` / `walkforward` |
| B | `inertia_015_10` | 4.5 / `atr_trailing` / **0.15** / **10** | **0** | **0** | `completed` / `walkforward` |

读法：每个臂**只有被覆盖的键**变成新值，**未触碰的键保持默认**（`atr_*` 臂的惯性键为 0.05/2；`inertia_*` 臂的止损键为 4.5/`atr_trailing`）。`atr_none` 用 `stop_mode="none"` 关闭止损（**不是** `0`、**不是** `"disabled"`——`config.py` 的合法集是 `"atr_trailing" / "fixed_pct" / "none"`，且 `stop_atr_mult` 被校验 `> 0`，写 0 会直接抛错）。

### 2.1 溯源回归（本轮新增的 manifest `cli_args` 修复）

`jq '.cli_args.options | {cash_sweep_rate, strategy_params, start, end, mode}'` 实测：

| 臂 | `cash_sweep_rate`（+ 类型） | `strategy_params` | `start` / `end` / `mode` |
| :--- | :--- | :--- | :--- |
| `atr_6_0` | `0.015`（**number**） | `{"cfsb_stop_atr_mult": 6.0}` | `2022-01-04` / `2026-10-22` / `walkforward` |
| `atr_8_0` | `0.015`（**number**） | `{"cfsb_stop_atr_mult": 8.0}` | 同上 |
| `atr_none` | `0.015`（**number**） | `{"cfsb_stop_mode": "none"}` | 同上 |
| `inertia_008_4` | `0.015`（**number**） | `{"cfsb_min_weight_change": 0.08, "cfsb_min_holding_bars": 4}` | 同上 |
| `inertia_010_5` | `0.015`（**number**） | `{"cfsb_min_weight_change": 0.1, "cfsb_min_holding_bars": 5}` | 同上 |
| `inertia_015_10` | `0.015`（**number**） | `{"cfsb_min_weight_change": 0.15, "cfsb_min_holding_bars": 10}` | 同上 |

`cli_args` 存在、`cash_sweep_rate` 是**数值**而非字符串、`strategy_params` 是**解析后的对象**而非原始字符串 ⇒ 该溯源修复在 6/6 上工作正常，**无缺陷上报**。（对照：`08_ablate_wf` 的旧 manifest 里 `cli_args` 键整体缺失，`jq '.cli_args'` 返回 `null`——修复只对修复后新产生的运行生效，旧产物按「不得修改任何运行产物」原样保留。）

---

## 3. Grid A 表 —— ATR 追踪止损（`stop_atr_mult` / `stop_mode`）

口径：18 折算术均值；Δ 一律为「该臂 − `08_ablate_wf/baseline`」；CAGR / MaxDD 以 pp 计；**MaxDD 为折内 63-bar 回撤**。`total_rebalances` 为 18 折求和。

| 臂 | `stop_atr_mult` / `stop_mode` | mean_sharpe | mean_cagr | mean_max_dd | mean_calmar | total_rebalances | `deflated_sharpe_ratio` |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 4.5 / `atr_trailing` | 1.4329 | 30.1513% | 4.6877% | 10.9135 | 535 | 1.987e-54 |
| `atr_6_0` | 6.0 / `atr_trailing` | 1.4783 | 30.4500% | 4.5193% | 11.1403 | 520 | 1.482e-53 |
| `atr_8_0` | 8.0 / `atr_trailing` | 1.4574 | 30.2912% | 4.6759% | 10.5813 | 530 | 6.999e-56 |
| `atr_none` | 4.5 / **`none`** | **1.6292** | **33.5425%** | 4.6059% | 11.2674 | 495 | 1.113e-37 |

Δ vs baseline：

| 臂 | ΔSharpe | ΔCAGR (pp) | ΔMaxDD (pp) | ΔCalmar | ΔRebalances | ΔSharpe 更优折数 | 折级 ΔCAGR 中位数 | 折内 MaxDD 更浅折数 |
| :--- | ---: | ---: | ---: | ---: | ---: | :---: | ---: | :---: |
| `atr_6_0` | +0.045 | **+0.299** | **−0.168**（更浅） | +0.227 | −15 | 6/18 | +0.00 pp | 9/18 |
| `atr_8_0` | +0.024 | +0.140 | −0.012（更浅） | −0.332 | −5 | 8/18 | +0.02 pp | 7/18 |
| `atr_none` | +0.196 | **+3.391** | −0.082（更浅） | +0.354 | −40 | 12/18 | +1.38 pp | 7/18 |

### 3.1 关键机制披露：Grid A 的 6.0 / 8.0 两臂被自适应裁剪「吸收」了

`pipeline/research/blend/blend_base.py:891-897`：

```python
eff_stop_mult = stop_atr_mult
if enable_adaptive_stop_atr:                       # 本策略默认 True，Grid A 全部臂未关闭
    eff_stop_mult = float(np.clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5))
trailing_stop_p = max(asset_entry_price[s] * 0.92, peak_p - eff_stop_mult * cur_atr)
```

- `stop_atr_mult = 8.0` 时，只要 `med_vol / s_vol > 0.9375`（中位数资产即如此），`eff_stop_mult` 被**上限 7.5 截断**。⇒ `atr_8_0` 的实际生效值大多停在 **7.5**，而不是 8.0。
- `stop_atr_mult = 6.0` 时生效值仍随时段/个股在 `[3.5, 7.5]` 内浮动。
- 因此 **`atr_6_0 → atr_8_0` 的名义 +33% 步长在实盘口径上被压缩到约 +25%（6.0 → 7.5），且逐日逐股不同**。这是 Grid A 前两臂效应微小、且 `8.0` 反而比 `6.0` 更差的直接机制原因，**不是**「止损越松越好」这一假设的证据。
- 只有 **`atr_none`（`stop_mode="none"`）完全跳出该裁剪**，是 Grid A 中唯一真正改变了止损行为的臂。

### 3.2 另一条关键披露：`08_ablate_wf` 的 `no_atrstop` 并没有关掉 ATR 止损

- `08_ablate_wf/no_atrstop` 的 `$extra` 是 `{"blend_enable_adaptive_stop_atr": false}`，其 `strategy_dump.json` 实测为 `cfsb_stop_mode = "atr_trailing"`、`cfsb_stop_atr_mult = 4.5` ⇒ **底层 ATR 追踪止损依然存在**，它只关掉了 `med_vol/s_vol` 的自适应放大/收缩（`eff_stop_mult` 恒等于 4.5）。
- 所以「关掉止损只涨 +0.23 pp」这个上游读数**测的不是「止损关闭」**，而是「止损倍数固定为 4.5」。本轮 `atr_none` 才是真正的「止损关闭」，ΔCAGR **+3.39 pp**，比 `no_atrstop` 的 +0.23 pp 大一个数量级。
  ⇒ **假设 A 在 `08_ablate_wf` 里其实从未被正确检验过**（见 §6 裁决）。

---

## 4. Grid B 表 —— 惯性闸门（`min_weight_change` / `min_holding_bars`）

口径同 §3。**再平衡笔数是 B 网格的首要观测量（primary outcome）**，因此单列「笔数降幅」与「逐折笔数下降折数」。

| 臂 | `min_weight_change` / `min_holding_bars` | mean_sharpe | mean_cagr | mean_max_dd | mean_calmar | total_rebalances | 笔数降幅 | 笔数下降折数 | `deflated_sharpe_ratio` |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: |
| `baseline` | 0.05 / 2 | 1.4329 | 30.1513% | 4.6877% | 10.9135 | 535 | — | — | 1.987e-54 |
| `inertia_008_4` | 0.08 / 4 | 1.6449 | 33.3323% | 5.2037% | 9.8469 | **340** | **−36.4%** | **17/18** | 7.851e-21 |
| `inertia_010_5` | 0.10 / 5 | **1.7626** | **35.8977%** | 5.4144% | 10.6761 | **239** | **−55.3%** | **18/18** | 1.107e-21 |
| `inertia_015_10` | 0.15 / 10 | 1.4428 | 28.8800% | 5.2829% | 9.1561 | 252 | −52.9% | 17/18 | 2.960e-46 |

Δ vs baseline：

| 臂 | ΔSharpe | ΔCAGR (pp) | ΔMaxDD (pp) | ΔCalmar | ΔRebalances | ΔSharpe 更优折数 | 折级 ΔCAGR 中位数 | 折内 MaxDD 更浅折数 |
| :--- | ---: | ---: | ---: | ---: | ---: | :---: | ---: | :---: |
| `inertia_008_4` | +0.212 | **+3.181** | +0.516（更深） | −1.067 | **−195** | 10/18 | +3.82 pp | 5/18 |
| `inertia_010_5` | +0.330 | **+5.746** | +0.727（更深） | −0.237 | **−296** | 9/18 | +4.00 pp | 5/18 |
| `inertia_015_10` | +0.010 | **−1.271** | +0.595（更深） | −1.757 | **−283** | 8/18 | +1.84 pp | 6/18 |

### 4.1 参照：`no_inertia`（拆除闸门）的代价

`08_ablate_wf/no_inertia` 的 `$extra` 是 `{"cfsb_adaptive_inertia": false, "cfsb_min_weight_change": 0.0, "cfsb_min_holding_bars": 0, "cfsb_entry_threshold": 0.0}`（必须四键同时归零才让 `use_inertia = (min_weight_change > 0 or min_holding_bars > 0)` 变假）。重读其 `walkforward_summary.json`：

| 臂 | mean_sharpe | mean_cagr | mean_max_dd | total_rebalances | ΔCAGR (pp) | ΔSharpe |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `no_inertia` | 1.2493 | 28.1991% | 4.8589% | **970** | **−1.951** | −0.184 |
| `no_atrstop` | 1.4249 | 30.3776% | 4.6119% | 534 | +0.226 | −0.008 |

⇒ 闸门确为**承重模块（load-bearing）**：拆掉后笔数从 535 涨到 970（**+81%**，逐折近乎翻倍），CAGR **−1.95 pp**，Sharpe **−0.184**。这是评价 Grid B 的**代价上限**（简报另引标准模式 −2.73 pp）。

### 4.2 逐折再平衡笔数（B 网格的主证据）

| 折 | baseline | 008_4 | 010_5 | 015_10 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 33 | 34 | 25 | 26 |
| 2 | 31 | 24 | 26 | 35 |
| 3 | 34 | 27 | 24 | 27 |
| 4 | 26 | 21 | 16 | 22 |
| 5 | 23 | 20 | 16 | 15 |
| 6 | 30 | 24 | 17 | 23 |
| 7 | 34 | 18 | 9 | 16 |
| 8 | 30 | 9 | 6 | 7 |
| 9 | 29 | 15 | 11 | 8 |
| 10 | 32 | 28 | 10 | 17 |
| 11 | 26 | 14 | 11 | 9 |
| 12 | 27 | 10 | 10 | 5 |
| 13 | 31 | 17 | 5 | 4 |
| 14 | 26 | 13 | 1 | 1 |
| 15 | 22 | 6 | 6 | 3 |
| 16 | 39 | 18 | 8 | 7 |
| 17 | 32 | 17 | 15 | 5 |
| 18 | 30 | 25 | 23 | 22 |
| **合计** | **535** | **340** | **239** | **252** |

笔数下降是**机械性的、逐折可数、不带统计噪声**的结果（`010_5` 在 18/18 折全部下降），与 SR 的折间噪声无关。

> **折均值 vs 折中位数**：B 三臂的折级 ΔCAGR **中位数全部为正**（+3.82 / +4.00 / +1.84 pp），但 `015_10` 的折均值却是 **−1.271 pp** ⇒ 它的亏损由**少数几折的大幅负值**驱动，中位数仍为正。同理 Grid A 的 `atr_none` 折均值 +3.391 pp、中位数仅 +1.38 pp，收益也集中在少数折。**这正是「折均值差不得单独当显著性」的又一次直接证据**（折级 CAGR 标准差 59.6 pp）。

---

## 5. 噪声下限与显著性（裁决的依据）

| 量 | 值 | 用途 |
| :--- | ---: | :--- |
| `fold_sharpe_std`（baseline，样本标准差） | **2.2836** | 简报里「改善需超过 ≈2.28」的那条阈值；已复核 = `st.stdev(baseline 逐折 sharpe)`（总体标准差为 2.2193） |
| 折均值 Sharpe 的**标准误** `fold_sharpe_std / √18` | **0.5382** | **严格更相关的噪声下限**：任何两臂的 ΔSharpe 必须与它比较，而不是与 2.2836 比较 |
| 折级 CAGR 标准差（baseline） | **59.58 pp**（折范围 −22.70% … +182.65%） | 说明「折均值 CAGR 差」为何不能单独当显著 |
| 折级 MaxDD 标准差（baseline） | **2.077 pp**（折范围 1.67% … 8.53%） | ΔMaxDD 的比较基准 |
| 配对 ΔSharpe 的标准误（各臂，n=18） | 0.088 / 0.078 / 0.110（A）；0.206 / 0.272 / 0.326（B） | 见下 |

配对 t 值（折级配对，n = 18，仅作描述性诊断，不做多重比较校正后的显著性主张）：

| 臂 | ΔSharpe 配对 t | ΔCAGR 配对 t | ΔSharpe 配对 SD |
| :--- | ---: | ---: | ---: |
| `atr_6_0` | +0.52 | +0.30 | 0.374 |
| `atr_8_0` | +0.32 | +0.17 | 0.329 |
| `atr_none` | **+1.79** | **+2.22** | 0.465 |
| `inertia_008_4` | +1.03 | +0.75 | 0.875 |
| `inertia_010_5` | +1.21 | +1.35 | 1.152 |
| `inertia_015_10` | +0.03 | −0.18 | 1.381 |

⇒ 没有一个臂的 ΔSharpe 或其折均值能达到常规双边 5% 门槛（|t| ≥ 2.1，n=18）。**所有 Sharpe 侧的量级都在噪声带内**；唯一在量级上远离噪声的，是 Grid B 的**再平衡笔数**（机械量，无噪声）和 `atr_none` 的 ΔCAGR（t = 2.22，但仍未过校正门槛）。

---

## 6. 裁决

### 6.1 假设 A（ATR 追踪止损过紧）—— 检验

判据（简报给定）：**A 通过，当且仅当 CAGR 与 MaxDD 都随「放宽」单调朝假设方向移动，且改善幅度超过 `fold_sharpe_std`（≈2.28）；否则效应落在折噪声内，A 判为「未证实」而非「已证实」。**

放宽序列：`4.5` → `6.0` → `8.0` → `none`

| 判据分量 | 实测 | 判定 |
| :--- | :--- | :---: |
| CAGR 单调朝上？ | 30.151 → 30.450 (**+0.299**) → 30.291 (**+0.140**) → 33.542 (**+3.391**) — 第 2 步**反向**（−0.159 pp） | **FAIL** |
| MaxDD 单调不恶化？ | 4.688 → 4.519 (−0.168) → 4.676 (**+0.157 反向**) → 4.606 (−0.070) — 第 2 步**反向** | **FAIL** |
| 改善幅度 > `fold_sharpe_std` = 2.2836？ | 最大 ΔSharpe = **+0.196**（`atr_none`）= 阈值的 **8.6%**；即使按更宽松的折均值标准误 0.5382 也只有 36% | **FAIL** |
| 改善幅度 > 折均值 CAGR 噪声？ | `atr_none` ΔCAGR +3.391 pp，配对 SEM 1.526 pp，t = +2.22 —— 是本网格最大信号，但仍低于 59.6 pp 的折级 CAGR 离散度两个数量级 | 弱信号，未过校正门槛 |

**判定：A 不通过（两项单调性判据均 FAIL，且幅度在折噪声内）。**

补充两条必须一起读的限制：
- **机制上限**：`6.0` / `8.0` 两臂被 `np.clip(..., 3.5, 7.5)` 吸收（§3.1），名义放宽在生效口径上被压缩。因此「`6.0`/`8.0` 无效」**不能**推出「放宽止损无效」，它只能推出「在自适应裁剪带内放宽无效」。
- **上游读数是错标的**：`08_ablate_wf/no_atrstop` 只关了 `blend_enable_adaptive_stop_atr`，止损仍在（§3.2）。真正的「止损关闭」= `atr_none`，ΔCAGR **+3.39 pp**（≠ +0.23 pp）。

### 6.2 假设 B（惯性闸门可收紧）—— 检验

判据（简报给定）：**B 通过，当且仅当 `total_rebalances` 实质下降，且 CAGR 损失远低于 `no_inertia` 的惩罚（walkforward −1.95 pp / 标准 −2.73 pp）；若损失接近该量级，则闸门已在最优处，拒绝收紧。**

| 臂 | 笔数降幅 | ΔCAGR (pp) | 占 `no_inertia` 罚分（−1.951 pp）的比例 | 判定 |
| :--- | ---: | ---: | ---: | :--- |
| `inertia_008_4` | −36.4%（535→340，17/18 折下降） | **+3.181**（**变好**） | **−163%**（反向，是收益而非损失） | **PASS** |
| `inertia_010_5` | −55.3%（535→239，18/18 折下降） | **+5.746**（**变好**） | **−295%**（反向，是收益） | **PASS** |
| `inertia_015_10` | −52.9%（535→252，17/18 折下降） | **−1.271**（损失） | **+65%**（已逼近罚分量级） | **REFUSED** |

**判定：B 在 `0.08/4` 与 `0.10/5` 两臂通过；`0.15/10` 被拒绝（损失已达 `no_inertia` 罚分的 65%，按判据属「闸门已在最优处」）。**

必须一起读的三点限制：
- **方向与预期相反**：简报预设「收紧必然损失收益，只需检查损失是否『远低于』罚分」。实测 `0.08/4` 与 `0.10/5` 是**收益改善**（+3.18 / +5.75 pp），不是「可接受的损失」。这与 `no_inertia` 的 −1.95 pp 并不矛盾：`no_inertia` 是把闸门**全部拆掉**（含 `entry_threshold` 归零、`adaptive_inertia` 关闭），而 B 网格只是把闸门**调紧**，两者不是同一条曲线上的点。B 的真实形状是**单峰**：`0.05/2`(基线) < `0.08/4` < **`0.10/5`(峰)** > `0.15/10`(崩)。
- **改善本身不可当显著**：`0.10/5` 的 ΔSharpe +0.330 只有配对 SEM 0.272 的 1.21 倍（t = 1.21），ΔCAGR +5.75 pp 的配对 t = 1.35。⇒ **可辩护的结论是「收紧不损收益」（成本为零），不是「收紧提高收益」。**
- **回撤是单向变深的**：`+0.516 / +0.727 / +0.595` pp，三臂全部比基线更深（各自只有 5–6/18 折变浅），Calmar 从 10.914 掉到 9.847 / 10.676 / 9.156。幅度约为折内 MaxDD 折间标准差（2.08 pp）的 1/4–1/3，**不足以单独否决**，但不可声称「无代价」。`0.10/5` 的 Calmar（10.676）是三臂中最接近基线的。

---

## 7. 禁用指标声明（DSR）

本网格**禁止**用 `deflated_sharpe_ratio` 给臂排序，理由与 `08_ablate_wf` 一致且在本轮复现：

- 该字段是「扣掉 best-of-`n_trials` 选择偏差后真实 Sharpe > 0 的概率」，此处 `n_trials = n_valid_folds = 18`。
- 门槛 `sr₀ = fold_sharpe_std × k(18)`，`k(18) = (1−γ)·Φ⁻¹(1−1/18) + γ·Φ⁻¹(1−1/(18e)) = 1.8539`（γ = 0.5772）。逐臂实测：

| 臂 | `fold_sharpe_std` | **`sr₀`** | `mean_sharpe_ratio` | `deflated_sharpe_ratio` |
| :--- | ---: | ---: | ---: | ---: |
| `baseline` | 2.2836 | **4.2336** | 1.4329 | 1.987e-54 |
| `atr_6_0` | 2.3198 | **4.3006** | 1.4783 | 1.482e-53 |
| `atr_8_0` | 2.3310 | **4.3214** | 1.4574 | 6.999e-56 |
| `atr_none` | 2.2140 | **4.1045** | 1.6292 | 1.113e-37 |
| `inertia_008_4` | 1.8633 | **3.4542** | 1.6449 | 7.851e-21 |
| `inertia_010_5` | 1.9901 | **3.6894** | 1.7626 | 1.107e-21 |
| `inertia_015_10` | 2.1709 | **4.0246** | 1.4428 | 2.960e-46 |

- 6 臂的折均值 Sharpe 落在 **1.433–1.763**，门槛 `sr₀` 落在 **3.454–4.321** ⇒ **每一臂的均值 Sharpe 都低于自己的门槛一半以上**，DSR 全部数值为 0（`1.1e-21 ~ 1.5e-53`）。
- 这不是某一臂的缺陷，是本组的**噪声下限**：折间 Sharpe 离散度（1.86–2.33）大于折均值 Sharpe（1.43–1.76）本身。**`deflated_sharpe_ratio` 在本网格上已饱和，读数为 0 无排序信息**；不得把 `atr_none` 的 `1.113e-37` 与 `atr_8_0` 的 `6.999e-56` 说成前者优于后者（两者指数差距只反映 `sr₀` 与 σ 的相对位置，不是可信度排序）。

---

## 8. 结论（一行一条，附决定性的那个数）

| 假设 | 结论 | 决定性的数 |
| :--- | :--- | :--- |
| **A —— ATR 追踪止损过紧，放宽有益** | **仍不可判定（未证实）** | CAGR 在 `6.0→8.0` 一步**反向 −0.159 pp**（非单调），且最大 ΔSharpe 仅 **+0.196**，是 `fold_sharpe_std` 2.2836 的 8.6% / 折均值标准误 0.5382 的 36% ⇒ 效应在折噪声内。且 `6.0`/`8.0` 被 `clip(3.5, 7.5)` 吸收（§3.1），该两臂无法证伪 A；唯一真正关掉止损的 `atr_none` 给出 ΔCAGR **+3.391 pp**（t = 2.22），方向对但未过校正门槛、且 `MaxDD` 折内中位数反而 +0.074 pp。 |
| **B —— 惯性闸门可收紧** | **成立（限 `0.08/4` 与 `0.10/5`；`0.15/10` 被拒绝）** | `0.10/5` 把再平衡从 **535 降到 239（−55.3%，18/18 折全降）**，而 CAGR **+5.746 pp**（不是损失，距 `no_inertia` 的 −1.951 pp 罚分反向 295%）。`0.15/10` 虽再降笔数到 252，但 CAGR **−1.271 pp = 罚分的 65%**，按判据拒绝。代价：MaxDD 折内均值 **+0.727 pp**、Calmar 10.914→10.676。 |

**若要把 A 变成可判定**，下一轮必须**同时**关掉 `blend_enable_adaptive_stop_atr`（或把 `stop_atr_mult` 提到 `> 7.5`），否则网格永远落在裁剪带内、测量不到真实放宽；并且只有在 `stop_mode="none"` 与 `stop_atr_mult ≥ 8` 之间做**多值**扫描才可能验证单调性。本轮证据不足以支撑「放宽有益」，也不足以支撑「放宽有害」——**A 只是从未被正确检验过**（`no_atrstop` 关的是自适应乘子，`atr_none` 才是止损本身）。

---

## 9. 产物与版本控制

- **运行产物**：`09_param_grid/atr/{atr_6_0,atr_8_0,atr_none}/chan_four_state_blend/` 与 `09_param_grid/inertia/{inertia_008_4,inertia_010_5,inertia_015_10}/chan_four_state_blend/`，每目录 8 个文件（`walkforward_summary.json`、`walkforward_report.csv`、`walkforward_rebalances.csv`、`walkforward_weights.csv`、`comparison_report.json`、`strategy_dump.json`、`run_manifest.json`、`run.log`），共 48 个。
- **这些运行产物不进 git**：`.gitignore:17` 的 `/backtests_research/**` 屏蔽全部产物，仅由 `!/backtests_research/**/*.md` 等白名单放行人类撰写的报告（`git check-ignore -v` 实测：`run.log` / `walkforward_summary.json` 命中 `.gitignore:17`；本文件命中 `.gitignore:20` 的**反向**规则，即**不被忽略**）。
- **因此 `git status --porcelain` 里只会出现本文件这一条**（`09_param_grid/chan_four_state_blend/_metrics.md`），**不会**出现 48 个运行产物——这是 `5500cc3` 之后的既定策略，不是产物缺失。
- 本 worker 未修改 `backtester/`、`pipeline/`、`common/`、`docs/` 或任何既有报告；`08_ablate_wf/`、`06_ablate/`、`07_r3_oos_bear/` 仅被**只读**引用。

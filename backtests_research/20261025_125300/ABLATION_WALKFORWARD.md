# 消融实验的 walkforward 复核（08_ablate_wf）

> 本文件是 `20261025_125300` 消融实验的**跨策略合并报告**，口径为**走查（walkforward）**。
> 单策略细节（配置落地逐键核对、逐折胜折数、溯源脚本）保留在三个子文件：
> `08_ablate_wf/chan_four_state_blend/_metrics.md`、`08_ablate_wf/chan_risk_managed_blend/_metrics.md`、
> `08_ablate_wf/chan_dual_hybrid_blend/_metrics.md`（以及 `chan_risk_managed_blend/_provenance/` 的 6 个文件）。
> 标准模式（连续净值）的对应结论见同目录 `ABLATION_AND_ANATOMY.md`（`06_ablate/`）。
>
> **本文所有数字均由本 worker 从落盘产物重新读取/重算，不采用任何转述值。** 数据源逐节标注。

---

## 1. 方法与口径

### 1.1 运行矩阵

**13 臂 × 3 策略 = 39 次运行**，全部落在 `backtests_research/20261025_125300/08_ablate_wf/<arm>/<strategy>/`，每次运行 8 个产物（`walkforward_summary.json`、`walkforward_report.csv`、`walkforward_rebalances.csv`、`walkforward_weights.csv`、`comparison_report.json`、`strategy_dump.json`、`run_manifest.json`、`run.log`）。

13 个臂 = `baseline` + 12 个单模块拆除臂：`no_voltarget`、`no_orthogon`、`no_structregime`、`no_cyq`、`no_atrstop`、`no_lowbeta`、`no_sectorthrot`、`no_hysteresis`、`no_thrust`、`no_inertia`、`bull_full`、`no_circuit`。臂定义与 `extra` JSON 逐字取自 `docs/backtest_research.md` §8 `run_arm` / `06_ablate`，仅把 `${pfx}` 代换为 `cfsb` / `crb` / `cdhb`。

三个策略与其前缀、规范名（**规范名取自各运行的 `walkforward_summary.json` 的 `strategy` 字段**，非目录名）：

| 目录名 | 前缀 | `walkforward_summary.json` 的 `strategy`（39/39 一致） |
| :--- | :---: | :--- |
| `chan_four_state_blend` | `cfsb` | `Chan Four-State Risk-Managed Blend Strategy` |
| `chan_risk_managed_blend` | `crb` | `Chan Risk-Managed Blend Strategy` |
| `chan_dual_hybrid_blend` | `cdhb` | `Chan Dual Hybrid Alpha Blend Strategy` |

### 1.2 命令行口径（三份 runner 脚本逐字一致）

三个 worker 的实际执行脚本（`/tmp/opencode/ablwf/run_cfsb.sh`、`run_crb.sh`、`run_cdhb.sh`；`crb` 的那份已归档为 `08_ablate_wf/chan_risk_managed_blend/_provenance/run_crb.sh`）的 `run_backtest.py` 调用段**完全相同**：

```
--start 2022-01-04 --end 2026-10-22 \
--mode walkforward --window-years 0.25 --step-years 0.25 \
--data-provider fuyao --min-shares 100 --china-trading \
--baseline-symbol 000300.SH \
--cash-sweep-rate 0.015 \
--strategy-params "$extra" \
--results-dir "$out_dir" --n-jobs 1
```

- `--data-provider fuyao`：本地 `data/cache.duckdb` 只读，**全离线，零网络**。
- `--n-jobs 1`：单进程，39 次运行串行。
- 未加 `--market-impact` / `--max-volume-participation`（与 `06_ablate` 一致，故 08 与 06 的这一项也不构成差异来源）。
- 落地自检（本 worker 重跑）：39/39 个 `run_manifest.json` 为 `status=completed`、`mode=walkforward`；39 个 `run.log` 中 `ignoring unknown key(s)` 命中 **0** 个文件、`Traceback` 命中 **0** 个文件。

### 1.3 折规则与截断（1,134 / 1,161）

`window_bars = step_bars = int(round(0.25 × 252)) = 63`，`0.25y` 窗 / `0.25y` 步 ⇒ 折不重叠、逐折重置净值。

| 项 | 实测值 | 证据 |
| :--- | :--- | :--- |
| 请求区间 bar 数 | **1,161** | `06_ablate/baseline/chan_four_state_blend/backtest_equity.csv` 共 1,161 行，首 `2022-01-04`、末 `2026-10-22`；同目录 `comparison_report.json` 的 `overlap_bars = 1161`（`crb` / `cdhb` 基线同为 1,161） |
| 折数 | **18**（13 臂 × 3 策略全部 `n_folds = 18`、`n_valid_folds = 18`） | 39 个 `walkforward_summary.json` |
| 进入折内评分的 bar 数 | **1,134** = 18 × 63 | 同上 |
| 首折 | 折 1 `2022-01-04 → 2022-04-11` | 三策略 `rolling_window_performance[0]` 逐字一致 |
| 末折 | 折 18 `2026-06-10 → 2026-09-07` | 三策略 `rolling_window_performance[17]` 逐字一致 |
| **不入任何折的尾部 bar** | **27 根**，`2026-09-08 → 2026-10-22` | 1,161 − 1,134 = 27；`06_ablate` 基线权益曲线中 `date > 2026-09-07` 的交易日恰 27 个（首 `2026-09-08`、末 `2026-10-22`）；39/39 个 `walkforward_summary.json` 的 `actual_last_fold_end = 2026-09-07` |
| 折被丢弃数 | **0** | 13 臂 × 3 策略全部 `n_folds = n_valid_folds = 18`、`actual_first_fold_start = 2022-01-04` |

⇒ **走查的实际评分区间是 `2022-01-04 → 2026-09-07`（18 个季度折），不是到 `2026-10-22`。** 臂间横比不受影响（39 次运行的首末折日期逐字相同），但**任何绝对水平都不得与 `06_ablate` 的全窗口绝对值并列**。

### 1.4 为什么可以和 `06_ablate` 直接对比

`08_ablate_wf` 的请求窗（`2022-01-04 → 2026-10-22`）与 `06_ablate` **完全相同**（两者同窗 `overlap_bars = 1161`），`--cash-sweep-rate 0.015` 也相同，universe、`--min-shares 100`、`--china-trading`、`--baseline-symbol` 亦相同。

⇒ **08 与 06 之间的任何差异只可能来自「评估模式」这一个变量**（逐折重置净值 vs 一条连续净值曲线），不含区间差异、不含现金收益口径差异。这正是本报告 §3–§5「同一模块在两种模式下的可达性/符号是否一致」能成立的前提。

反过来，**本文数字不得与 `03_r2b_oos`（`2025-01-02 → 2026-10-22`，`n_folds = 6`，现金 0%）或 `07_r3_oos_bear`（`2021-06-01 → 2022-12-16`，6 折）的折均值并列或合并**——区间与折网格均不同即口径污染。

### 1.5 三条必须随数字一起引用的披露

1. **`mean_max_drawdown` 是「折内」63 根 bar 的最大回撤的 18 折算术平均**，**不是全期（1,161 根）最大回撤**。walkforward 每折重置净值，全期回撤在此模式下**不可观测**。量级差举例（cdhb）：折内均值 `4.22%` vs 同窗口标准模式整段 `11.81%`（`06_ablate/baseline/chan_dual_hybrid_blend/backtest_equity.csv` 峰谷实测）。**两者绝不可相减或互相校验。**
2. **`mean_sharpe_ratio` / `mean_cagr` / `mean_calmar_ratio` 同样是 18 折的算术均值**，不是 4.8 年整体年化。折级 CAGR 极差比任何两臂之差大一个数量级（`cfsb` 基线逐折 CAGR：`−22.70% … +182.65%`）⇒ **折均值差值不得当显著性用**。
3. **`deflated_sharpe_ratio` 在本网格上恒为数值 0，不可用于排序**（§7）。

> **溯源残留（不影响任何数字，但必须披露）**：`chan_dual_hybrid_blend` 的那一轮运行把 `--results-dir` 写成了 `<arm>` 在前、策略在后（本报告 §9.2 已把目录规范化为 `<arm>/<strategy>`），因此**13 个 cdhb 运行的 `run_manifest.json` 里 `group` 字段仍是 `08_ablate_wf/chan_dual_hybrid_blend`（不随臂变化）**，而非 `08_ablate_wf/<arm>`。产物内容与其余两策略同构，`mode` / `status` / 指标均正常；该字段为历史写入值，按「不得修改任何运行产物」的约束原样保留，仅在此标注。

---

## 2. 三策略 13 臂主表（walkforward，18 折）

口径：全部取自各目录 `walkforward_summary.json` 的字段（`mean_sharpe_ratio` / `mean_cagr` / `mean_max_drawdown` / `mean_calmar_ratio` / `n_folds` / `deflated_sharpe_ratio`）；Δ 一律为「该臂 − 同策略 `baseline`」，CAGR / MaxDD 以 pp 计；**ΔMaxDD 为正 = 回撤更深（更差）**。`baseline` 行的 Δ 记为 `—`。

> [!CAUTION]
> **口径陷阱（2026-10-25 补记）：同一份 `walkforward_summary.json` 内部的 `baseline_*` 字段不是「策略基线」，绝不能拿来算 Δ。**
> 每个**臂**目录的逐折行 `rolling_window_performance[*]` 都带 `baseline_cagr` / `baseline_sharpe_ratio` / `baseline_max_drawdown`；它们是 **`000300.SH` 的 `equal_weight` 基准**（见同目录 `comparison_report.json` 的 `baseline_symbol` / `baseline_template`），因此**在同一折上跨臂、跨策略取值完全相同**（实测 39 个叶 × 18 折一致；首折 `baseline_sharpe_ratio = −3.0299`，折均值 `baseline_cagr = 6.02%`），与真正的策略基线 `08_ablate_wf/baseline/<strategy>/` 的 `mean_cagr` 相差 **逐折最大 147–192pp**（cfsb −192.1pp / crb −162.3pp / cdhb −147.5pp，均出现在 `2023-01-17` 折）、**折均值口径 19.6–24.1pp**。
> ⇒ **本报告（及任何后续复核）的 Δ 一律以 `08_ablate_wf/baseline/<strategy>/walkforward_summary.json` 为基准计算；读到臂目录内联的 `baseline_*` 就当它是「基准指数列」，不是「策略基线列」。**（同目录 `comparison_report.json` 的 `mean_baseline_sharpe_ratio` / `mean_baseline_cagr` 同样指这个指数基准——实测 `mean_baseline_cagr = 6.02%`，恰为上述冻结列的折均值，不是策略基线。）

### 2.1 三个基线的显式核对（逐字复验，未采用任何转述值）

| 策略 | `mean_sharpe_ratio` | `mean_cagr` | `mean_max_drawdown` | `mean_calmar_ratio` | `n_folds` | `fold_sharpe_std` |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `chan_four_state_blend` (cfsb) | **1.4329** | **30.15%** | **4.69%** | 10.9135 | 18 | 2.2836 |
| `chan_risk_managed_blend` (crb) | **1.5370** | **27.88%** | **4.15%** | 10.6352 | 18 | 2.1844 |
| `chan_dual_hybrid_blend` (cdhb) | **1.4250** | **25.63%** | **4.22%** | 10.2096 | 18 | 2.2141 |

原始值（未取整，供复核）：cfsb `1.4328704609192824 / 0.30151320606816717 / 0.04687693080691772`；crb `1.5369624775774695 / 0.278799920100602 / 0.04153871466951203`；cdhb `1.4249700144569886 / 0.25627364561917376 / 0.04215945583509294`。三个基线数字与任务书给定的 cfsb `1.433 / 30.15% / 4.69%`、crb `1.537 / 27.88% / 4.15%`、cdhb `1.425 / 25.63% / 4.22%` **完全一致**。

### 2.2 `chan_four_state_blend`（cfsb，`blend_weighting_mode = regime_ic_ir_hybrid`）

| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | ΔCAGR (pp) | ΔSharpe | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 1.4329 | 30.15% | 4.69% | 10.9135 | 18 | 1.987e-54 | — | — | — |
| `no_voltarget` | 1.3854 | 30.57% | 4.93% | 10.5704 | 18 | 4.952e-49 | +0.42 | −0.0474 | +0.24 |
| `no_orthogon` | 1.5367 | 29.94% | 4.50% | 10.5336 | 18 | 4.647e-47 | −0.22 | +0.1038 | −0.19 |
| `no_structregime` | 1.3959 | 30.41% | 4.63% | 10.8756 | 18 | 3.480e-53 | +0.26 | −0.0370 | −0.06 |
| `no_cyq` | 1.4591 | 28.76% | 4.84% | 10.1553 | 18 | 1.289e-51 | −1.39 | +0.0263 | +0.16 |
| `no_atrstop` | 1.4249 | 30.38% | 4.61% | 10.6208 | 18 | 3.936e-52 | +0.23 | −0.0080 | −0.08 |
| `no_lowbeta` | 1.3639 | 29.62% | 4.69% | 10.7018 | 18 | 2.373e-58 | −0.53 | −0.0690 | +0.00 |
| `no_sectorthrot` | 1.3618 | 29.71% | 4.82% | 10.2966 | 18 | 4.071e-58 | −0.45 | −0.0710 | +0.13 |
| `no_hysteresis` | 1.4317 | 30.04% | 4.60% | 10.8279 | 18 | 1.260e-50 | −0.11 | −0.0011 | −0.09 |
| `no_thrust` | 1.3573 | 24.21% | 3.99% | 10.5871 | 18 | 4.323e-61 | **−5.94** | −0.0756 | −0.70 |
| `no_inertia` | 1.2493 | 28.20% | 4.86% | 10.1816 | 18 | 5.242e-79 | −1.95 | **−0.1835** | +0.17 |
| `bull_full` | 1.1272 | 28.21% | 5.24% | 9.1894 | 18 | 1.307e-69 | −1.94 | **−0.3057** | **+0.56** |
| `no_circuit` | 1.4318 | 30.14% | 4.69% | 10.9105 | 18 | 1.553e-54 | −0.01 | −0.0010 | +0.01 |

### 2.3 `chan_risk_managed_blend`（crb，`blend_weighting_mode = rolling_ic_ir`）

| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | ΔCAGR (pp) | ΔSharpe | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.192e-41 | — | — | — |
| `no_voltarget` | 1.6297 | 31.41% | 4.38% | 10.8425 | 18 | 1.509e-33 | +3.53 | +0.0927 | +0.22 |
| `no_orthogon` | 1.7821 | 28.91% | 4.38% | 8.8928 | 18 | 1.377e-19 | +1.03 | **+0.2452** | +0.23 |
| `no_structregime` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.192e-41 | +0.00 | +0.0000 | +0.00 |
| `no_cyq` | 1.5015 | 26.91% | 4.78% | 10.0938 | 18 | 3.954e-45 | −0.97 | −0.0355 | +0.62 |
| `no_atrstop` | 1.6615 | 29.96% | 4.24% | 11.5584 | 18 | 2.993e-37 | **+2.08** | +0.1245 | +0.09 |
| `no_lowbeta` | 1.5637 | 28.10% | 4.11% | 10.8378 | 18 | 3.353e-40 | +0.22 | +0.0267 | −0.04 |
| `no_sectorthrot` | 1.5653 | 27.68% | 4.02% | 10.6597 | 18 | 2.374e-37 | −0.20 | +0.0284 | −0.13 |
| `no_hysteresis` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.192e-41 | +0.00 | +0.0000 | +0.00 |
| `no_thrust` | 1.7228 | 26.81% | 3.84% | 9.9178 | 18 | 3.353e-20 | −1.07 | **+0.1858** | −0.31 |
| `no_inertia` | 1.3378 | 27.54% | 4.67% | 10.2711 | 18 | 1.905e-68 | −0.34 | **−0.1992** | +0.52 |
| `bull_full` | 1.3554 | 33.97% | 5.40% | 9.6535 | 18 | 1.398e-53 | +6.09 | **−0.1816** | **+1.25** |
| `no_circuit` | 1.5384 | 27.94% | 4.15% | 10.6474 | 18 | 3.936e-41 | +0.06 | +0.0014 | −0.00 |

### 2.4 `chan_dual_hybrid_blend`（cdhb，`blend_weighting_mode = rolling_ic_ir`）

| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | ΔCAGR (pp) | ΔSharpe | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 1.4250 | 25.63% | 4.22% | 10.2096 | 18 | 2.864e-50 | — | — | — |
| `no_voltarget` | 1.4909 | 26.48% | 4.41% | 10.0840 | 18 | 1.298e-42 | +0.86 | +0.0659 | +0.19 |
| `no_orthogon` | 1.3682 | 27.23% | 4.36% | 10.4206 | 18 | 4.587e-46 | +1.60 | −0.0568 | +0.15 |
| `no_structregime` | 1.4250 | 25.63% | 4.22% | 10.2096 | 18 | 2.864e-50 | +0.00 | +0.0000 | +0.00 |
| `no_cyq` | 1.4597 | 26.95% | 4.35% | 10.2846 | 18 | 1.534e-52 | +1.32 | +0.0348 | +0.13 |
| `no_atrstop` | 1.4920 | 26.25% | 4.09% | 10.6841 | 18 | 2.534e-46 | **+0.62** | +0.0670 | −0.13 |
| `no_lowbeta` | 1.4336 | 25.56% | 4.23% | 10.1723 | 18 | 1.995e-48 | −0.07 | +0.0086 | +0.01 |
| `no_sectorthrot` | 1.4734 | 25.05% | 4.26% | 10.0524 | 18 | 5.734e-46 | −0.58 | +0.0484 | +0.05 |
| `no_hysteresis` | 1.4250 | 25.63% | 4.22% | 10.2096 | 18 | 2.864e-50 | +0.00 | +0.0000 | +0.00 |
| `no_thrust` | 1.5155 | 20.89% | 3.38% | 9.0594 | 18 | 1.188e-32 | **−4.74** | +0.0905 | **−0.83** |
| `no_inertia` | 1.2858 | 23.29% | 4.87% | 8.8343 | 18 | 5.227e-56 | −2.34 | **−0.1392** | +0.65 |
| `bull_full` | 1.1221 | 25.50% | 5.09% | 8.4288 | 18 | 5.401e-70 | −0.13 | **−0.3029** | **+0.87** |
| `no_circuit` | 1.4250 | 25.63% | 4.22% | 10.2096 | 18 | 2.864e-50 | +0.00 | +0.0000 | +0.00 |

### 2.5 主表读数（三表共用）

- **三策略的 Sharpe 极差**：cfsb `1.1272 … 1.5367`、crb `1.3378 … 1.7821`、cdhb `1.1221 … 1.5155`。
- **&#124;ΔSharpe&#124; 全部小于一个折的 Sharpe 标准差**（cfsb `fold_sharpe_std = 2.2836`、crb `2.1844`、cdhb `2.2141`）：最大位移为 cfsb `bull_full` 的 `−0.3057`。⇒ **没有一臂能在统计上把某策略的 Sharpe 打进负值，也没有一臂能把 Sharpe 抬过自己的折间噪声**。
- **Δ 的方向可复现性必须回到逐折胜折数看**（逐折统计见三份 `_metrics.md`）。折级 8–10/18（近抛硬币）的臂，其折均值差值**不具备方向可复现性**。

---

## 3. ≡ 静默空操作矩阵（walkforward 口径）

### 3.1 判据与独立复算

以同策略 `baseline` 为参照，逐折对齐 `walkforward_rebalances.csv` 的 **`(fold, date)`** 键（同一 `(fold, date)` 内 `portfolio_equity` 恒为单值——本 worker 对 39 个文件逐组断言通过，非常量组 0 个），取 `portfolio_equity` 的首行值，逐点算 `max&#124;Δ&#124;`；**`max&#124;Δ&#124; < 1e-9` 且对齐集合与参照集合完全相等 ⇒ 判为 `≡`**。

> 对齐 bar 数逐臂不同（本表区间 `317 … 500`），原因是**一臂若在某调仓日完全无成交就不会产生该行**。因此「对齐数 < 参照总数」本身不是 ≡，必须看 `only_arm` / `only_base` 两列。这一点是本节的判读前提，`no_lowbeta` 的整条结论都挂在它上面。

下表为本 worker 从 39 个 `walkforward_rebalances.csv` **独立重算**（非转述三个子文件，但结果与之一致）：

| 臂 | cfsb 对齐 / 仅臂 / 仅基 / max&#124;Δ&#124; | crb 对齐 / 仅臂 / 仅基 / max&#124;Δ&#124; | cdhb 对齐 / 仅臂 / 仅基 / max&#124;Δ&#124; |
| :--- | :--- | :--- | :--- |
| `baseline`（参照总数） | 442 | 467 | 500 |
| `no_voltarget` | 395 / 48 / 47 / 4,342.69 | 433 / 36 / 34 / 7,787.98 | 492 / 12 / 8 / 3,831.77 |
| `no_orthogon` | 350 / 76 / 92 / 7,592.77 | 356 / 80 / 111 / 6,583.62 | 388 / 90 / 112 / 5,902.06 |
| **`no_structregime`** | 377 / 71 / 65 / 5,136.80 | **467 / 0 / 0 / 0.00 ⇒ ≡** | **500 / 0 / 0 / 0.00 ⇒ ≡** |
| `no_cyq` | 322 / 134 / 120 / 4,994.42 | 349 / 132 / 118 / 6,709.24 | 393 / 84 / 107 / 3,743.36 |
| `no_atrstop` | 391 / 57 / 51 / 5,911.02 | 414 / 65 / 53 / 4,458.36 | 426 / 77 / 74 / 5,285.17 |
| `no_lowbeta` | 440 / 4 / 2 / 2,123.30 | 464 / 2 / 3 / 762.89 | 492 / 7 / 8 / 792.52 |
| `no_sectorthrot` | 423 / 20 / 19 / 4,987.15 | 438 / 21 / 29 / 2,355.32 | 472 / 20 / 28 / 4,075.30 |
| **`no_hysteresis`** | 430 / 28 / 12 / 1,681.30 | **467 / 0 / 0 / 0.00 ⇒ ≡** | **500 / 0 / 0 / 0.00 ⇒ ≡** |
| `no_thrust` | 317 / 60 / 125 / 7,631.65 | 330 / 43 / 137 / 6,643.52 | 347 / 49 / 153 / 5,585.39 |
| `no_inertia` | 397 / 442 / 45 / 5,452.42 | 415 / 448 / 52 / 7,354.17 | 443 / 409 / 57 / 6,669.02 |
| `bull_full` | 369 / 128 / 73 / 5,897.53 | 385 / 139 / 82 / 9,591.18 | 433 / 116 / 67 / 5,511.98 |
| **`no_circuit`** | 442 / 0 / 0 / **138.84** | 467 / **1** / 0 / **219.91** | **500 / 0 / 0 / 0.00 ⇒ ≡** |

**结论：walkforward 下的 `≡` 集合**

| 策略 | ≡ 集合 | 说明 |
| :--- | :--- | :--- |
| `cfsb` | **空集** | 12 个拆除臂全部产生可观测净值分叉。**`no_structregime` 在 cfsb 上非 ≡**（377 对齐中 355 个有差异，max&#124;Δ&#124; 5,136.80）——这是三策略中唯一「结构 regime 可达」的策略 |
| `crb` | **{`no_structregime`, `no_hysteresis`}** | 两臂均 467/467 全对齐、`only_arm = only_base = 0`、`max&#124;Δ&#124; = 0.0` |
| `cdhb` | **{`no_structregime`, `no_hysteresis`, `no_circuit`}** | 三臂均 500/500 全对齐、`max&#124;Δ&#124; = 0.0` |

### 3.2 `no_structregime` ≡ on crb / cdhb —— 代码级根因：该开关在 `rolling_ic_ir` 下**结构性不可达**

**已由本 worker 直接读源码确证，不是转述。** 证据链：

1. `pipeline/research/blend/blend_base.py:434` 与 `:648` 读取该开关：`enable_structural_regime = self.resolve_param(p, "enable_structural_regime", True, bool)`。
2. 全文件对该变量的**消费点只有两处**：
   - `:468` `if enable_structural_regime and median_d is not None and not np.isnan(median_d):` —— 该行位于 `:465` 的 `elif weighting_mode in ("regime_adaptive", "regime_ic_ir_hybrid"):` 分支体内。
   - `:769` `if enable_structural_regime:` —— 只门控 `fractal_d_series` 的**预计算**；其产物 `median_d` 的唯一消费点就是 `:468-471`（`grep -n median_d` 仅命中 `:411` 形参、`:468/:469/:471` 该分支、`:1038` 调用处传参）。
3. `:445` 的 `if weighting_mode == "rolling_ic_ir":` 分支内**每一条出口都是 `return`**（`:447`、`:451`、`:462`、`:463` 四个 `return base_weights` / 归一化 return），因此 `:465` 的 `elif` **永远不会执行**。
4. 三个策略 dump 实测（`pipeline/research_strategy/results/strategy_dumps/*_strategy.json` 的 `params`）：

   | 策略 | `blend_weighting_mode` | `blend_enable_structural_regime` | 该开关是否可达 |
   | :--- | :--- | :--- | :--- |
   | `cfsb` | `regime_ic_ir_hybrid` | `True` | **可达**（走 `:465` 分支） |
   | `crb` | `rolling_ic_ir` | `True` | **不可达** |
   | `cdhb` | `rolling_ic_ir` | `True` | **不可达** |

⇒ `crb` / `cdhb` 上把 `blend_enable_structural_regime` 设为 `False`，**唯一效果是跳过 `fractal_d_series` 的预计算（省 CPU），消费端根本到不了**，因此**必然逐点恒等**。这个 `≡` 是**结构性的**，与数据、期限、评估模式全部无关（§4 中它在 standard 模式下同样 ≡）。而 `cfsb` 的 `regime_ic_ir_hybrid`（AGENTS.md 记录的**有意例外**，不得"规范化"回 `rolling_ic_ir`）恰好让它可达——同一臂在 `cfsb` 上不仅非 ≡，`no_structregime` 在 standard 模式下甚至给出 `+5.57pp` 的等效 CAGR 变化（§5.3）。

### 3.3 `no_hysteresis` ≡ on crb / cdhb —— 代码级根因：**基线本来就是 `False`**，不是不可达

- `pipeline/research_strategy/rs/config.py:794`：`blend_enable_regime_hysteresis: bool = False`（默认即关闭）。
- dump 实测：`cfsb` 的 `params` 里该键存在且为 **`True`**；`crb` 与 `cdhb` 的 `params` 里该键 **`<<ABSENT>>`**（不存在）。
- ⇒ `crb` / `cdhb` 上「设为 `False`」= **把当前生效值写回当前生效值**，是**空操作**。

这两条与 `no_structregime` 的根因**性质不同**，含义也不同：
- `no_structregime` / crb,cdhb = **机制不可达**（该权重模式下不存在此机制）；
- `no_hysteresis` / crb,cdhb = **机制可达但基线从未开启**。

⇒ 两者都**零信息量**：既**不得**读作「该模块无效」，也**不得**读作「该模块有效」，更**不得**参与任何排序。要评价 hysteresis 必须先造一个「基线开启」的对照臂（`cfsb` 正是那种情形，它在 walkforward 下非 ≡，`ΔSharpe −0.0011`、折级 9/18）。

### 3.4 `no_circuit` 在 cfsb 上是**非 ≡ 但近空转**，且在 crb 上「非 ≡ 但可忽略」

- **cfsb**：对齐 442 = 442、`only_arm = only_base = 0`（调仓日集合与 baseline **完全一致**），仅 **20 个调仓日**出现差异，`max&#124;Δ&#124; = 138.84` 元（相对 1.447e-03，baseline 净值区间 91,516.67 – 133,755.01）。**全部 20 个分叉点都落在折 4（`2022-10-19 → 2023-01-16`）**，首个分叉 `2022-10-26`、最大 `2022-12-22`、末个 `2023-01-09` ⇒ **18 折中只有 1 折的折内回撤路径真正触到了熔断三档阈值，其余 17 折熔断从未触发。**
- **crb**：对齐 467、`only_arm = 1`（多出 1 个调仓行）、`only_base = 0`、`max&#124;Δ&#124; = 219.91` 元、`ΔSharpe +0.0014`、`ΔCAGR +0.06pp`、`ΔMaxDD −0.00pp` ⇒ **非 ≡，但经济量级可忽略**。按本 worker 的判定纪律：**记「非 ≡ 但可忽略」，不得写成 ≡**（写 ≡ 需要 `max&#124;Δ&#124; < 1e-9`，它不满足）。
- **cdhb**：500/500 全对齐、`max&#124;Δ&#124; = 0.0` ⇒ **真 ≡**。

### 3.5 一个反向收窄的提醒

上面「crb/cdhb 的 `no_structregime`、`no_hysteresis` ≡」**只说明这两臂是零信息量的空转**，它**不能**外推成「结构 regime / hysteresis 模块无用」。真正被证伪的恰恰是**「standard 模式的 ≡ 等于模块无效」这个推论本身**——见 §4。

---

## 4. standard 模式 vs walkforward —— 可达性会变（核心发现）

### 4.1 方法

两套模式用同一窗口、同一 universe、同一 1.5% 现金收益，**唯一变量是评估模式**（§1.4）。standard 侧的数据源为 `06_ablate/<arm>/<strategy>/backtest_equity.csv`（1,161 行，日期索引逐行全对齐，无日期缺失）与 `comparison_report.json`，由本 worker 重新计算 `max&#124;Δeq&#124;` 与 `only_*`；walkforward 侧用 §3 的 `(fold, date)` 对齐。

**分级沿用 `ABLATION_AND_ANATOMY.md` §3.1**：`≡` = `max&#124;Δ&#124; < 1e-9`；`≈` = 有差但相对差 `< 1e-3`；`✓` = 相对差 `≥ 1e-3`。

### 4.2 逐臂可达性矩阵

| 臂 | cfsb: standard → walkforward | crb: standard → walkforward | cdhb: standard → walkforward |
| :--- | :--- | :--- | :--- |
| `no_voltarget` | ✓ (20,356.82) → ✓ | ✓ (22,503.60) → ✓ | ✓ (12,042.06) → ✓ |
| `no_orthogon` | ✓ (18,102.59) → ✓ | ✓ (48,937.03) → ✓ | ✓ (19,588.16) → ✓ |
| `no_structregime` | ✓ (66,413.71) → ✓ | **≡ (0.00) → ≡ (0.00)** | **≡ (0.00) → ≡ (0.00)** |
| `no_cyq` | ✓ (27,663.27) → ✓ | ✓ (17,923.91) → ✓ | ✓ (18,771.71) → ✓ |
| `no_atrstop` | ✓ (32,944.53) → ✓ | ✓ (29,164.56) → ✓ | ✓ (15,337.73) → ✓ |
| `no_lowbeta` | **≈ (47.94) → ✓ (2,123.30)** | ✓ (8,391.31) → ✓ (762.89) | **≡ (0.00) → ✓ (792.52)** ⟵ **反称 #1** |
| `no_sectorthrot` | ✓ (6,032.83) → ✓ | ✓ (8,465.55) → ✓ | ✓ (5,995.41) → ✓ |
| `no_hysteresis` | ✓ (2,156.34) → ✓ | **≡ (0.00) → ≡ (0.00)** | **≡ (0.00) → ≡ (0.00)** |
| `no_thrust` | ✓ (21,876.42) → ✓ | ✓ (82,947.34) → ✓ | ✓ (18,690.82) → ✓ |
| `no_inertia` | ✓ (44,516.73) → ✓ | ✓ (74,296.80) → ✓ | ✓ (42,846.89) → ✓ |
| `bull_full` | ✓ (18,908.00) → ✓ | ✓ (49,465.48) → ✓ | ✓ (38,759.08) → ✓ |
| `no_circuit` | **≡ (0.00) → ✓ (138.84)** ⟵ **反称 #2** | **≡ (0.00) → ✓ (219.91)** ⟵ **反称 #3** | **≈ (43.28) → ≡ (0.00)** ⟵ **反称 #4** |

> 括号内为 `max&#124;Δ&#124;`（standard 为权益元；walkforward 同为权益元）。`no_lowbeta / cfsb` 的 `≈` 转 `✓` 也是同一方向的可达性变化，但它在 standard 侧只是 `≈` 而非 `≡`，不列入「反称」，见 4.3 末条。

矩阵的读法：**4 个格子把「≡」这一判定翻转**（3 个 `≡ → 非 ≡`：`no_lowbeta`/cdhb、`no_circuit`/cfsb、`no_circuit`/crb；1 个 `≈ → ≡`：`no_circuit`/cdhb），另有 1 个 `≈ → ✓`（`no_lowbeta`/cfsb）属同类可达性变化、但两侧都不是 ≡。**本表共 12 臂 × 3 策略 = 36 个格子，除上述 5 个外，其余 31 个两模式判定一致。**

### 4.3 四个反称情形（逐条给数）

**反称 #1 —— `no_lowbeta` / cdhb：standard ≡，walkforward 非 ≡。**

- walkforward：对齐 **492** 行、未对齐 **15** 行（`only_arm = 7` + `only_base = 8`）、`max|diff| = **792.52**` 元。
- standard：1,161/1,161 全对齐、`max|diff| = 0.000000`。
- ⇒ **standard 模式下低 beta 惯性模块的每一次动作都被后续路径抵消成 0，它的「≡」是抵消（cancellation），不是惰性（inertness）。** 折内一重置，抵消关系断开，模块的成交差异立刻露出来（15 个调仓日只在单侧出现 ⇒ 它确实改变了实际成交）。

**反称 #2 —— `no_circuit` / cfsb：standard ≡，walkforward 非 ≡。**

- walkforward：对齐 **442**、`only_arm = only_base = 0`、**20 个调仓日有差异、全部在折 4（`2022-10-19 → 2023-01-16`）**、`max|diff| = **138.84**` 元。
- standard：1,161 根逐点 `max|diff| = 0.0`（真 ≡）。
- ⇒ **熔断在 18 折中只上场了 1 折。** standard 的那条连续净值曲线在 2022-01→2026-10 上**从未让熔断改变过任何持仓**，而折内重置后的折 4 让三档阈值真的被触碰。**同样的配置，同一段时间，「熔断是否被触发」这个二元问题的答案取决于评估模式。**

**反称 #3 —— `no_circuit` / crb：standard ≡（1,161 根全 0.0），walkforward 非 ≡ 但可忽略。**

- walkforward：对齐 467、`only_arm = 1`（**全表唯一 `only_arm = 1` 的臂**）、`only_base = 0`、`max|diff| = 219.91` 元、`ΔSharpe +0.0014`、`ΔCAGR +0.06pp`、`ΔMaxDD −0.00pp`。
- ⇒ **必须分类为「非 ≡ 但可忽略」，而不是 ≡。** 判定 `≡` 需要 `max&#124;Δ&#124; < 1e-9`，219.91 元不满足；把它写成 ≡ 会掩盖「折界重置确实改变了熔断状态机的一个动作」这一事实。经济上它可忽略（`ΔSharpe` 四位小数只有 `+0.0014`），所以也不能当作「熔断有效/无效」的证据。

**反称 #4 —— `no_circuit` / cdhb：standard ≈（43.28 元，0.04%），walkforward ≡。**

- walkforward：500/500 全对齐、`only_arm = only_base = 0`、`max|diff| = 0.0` ⇒ **真 ≡**。
- standard：1,161 根全对齐但 `max|diff| = 43.28` 元（相对 ~10 万本金约 **0.04%**，`ΔSharpe +0.0003`）。
- ⇒ **反向：模式切换把 `≈` 变成了 `≡`。** 这与 #2/#3 不矛盾——它说明「折内重置」不是单调地把差异放大，而是**重排了回撤路径本身**，因此一个模块是否被触发、触发几次，在两种模式下可以朝任一方向给出不同答案。cdhb 侧的机制候选：`elastic_drawdown_reset`（`blend_base.py:622` 的 `param_prefix == "cdhb"` 默认式）在 `fast_recovery` 时把 `peak_nav` 顶到当前 NAV，与熔断器构成互相覆盖的两套机制——**本报告只确证观测事实，不宣称已定位根因**。

**补充（同方向但严格说不算反称）**：`no_lowbeta / cfsb` 从标准 `≈`（47.94 元，相对 4.8e-4）变成 walkforward `✓`（2,123.30 元，440 对齐 / 20 个差异调仓日）。方向与 #1 相同：**低 beta 惯性在 cdhb 上被完全抵消、在 cfsb 上被抵消到 4 位有效数字以下**，两者都不是「模块没做事」。

### 4.4 核心结论（本节存在的理由）

> **standard 模式的 `≡` 不许可任何「该模块什么都没做 / 该模块无效」的断言。**

三组证据各自独立地支持这句话：

1. `no_circuit / cfsb, crb` 在 standard 下是**逐位 0**，但 walkforward 下分别出现 20 个差异调仓日与 1 个额外调仓行 ⇒ 熔断**曾经**上场，只是连续路径上看不到。`04_r1_std_cb_compare` 在另一个 726 根窗口上第二次复现了逐位 0（两臂 `max&#124;Δ&#124; = 0.0`），也**同样不能**升级为「熔断无效」——它仍然只是「在那条连续路径上没触发」。
2. `no_lowbeta / cdhb` 在 standard 下是**逐位 0**，walkforward 下 792.52 元分叉 + 15 个单侧调仓日 ⇒ standard 的 0 是**抵消**出来的。prior 报告把它判为「作废（同 7.3 的实质惰性）」是**结论正确、理由需修正**：它不是惰性，是抵消。
3. `no_circuit / cdhb` 反向：standard 有小差（43.28）、walkforward 是 0 ⇒ **可达性不是单调的**，不能把任一模式的 ≡ 外推为机制属性。

相反方向也要写清：**walkforward 的 ≡ 同样不许可「模块无效」**。`no_structregime` / crb,cdhb 与 `no_hysteresis` / crb,cdhb 的 ≡ 有 **代码级根因**（§3.2、§3.3），它们是「机制不可达」与「基线本已关闭」，与数据无关，换窗口也不会变——这两类 ≡ 才是可以引用为「零信息量」的；而 `no_circuit` 那种「本窗口内没触发」的 ≡ 只是**样本缺失**，换窗口、换折网格就可能翻转。

---

## 5. 跨模式符号翻转（必须判废的臂）

### 5.1 标准模式一侧的口径（一次性说明，全节适用）

- **ΔSharpe（standard）**：取自 `06_ablate/<arm>/<strategy>/comparison_report.json` 的 `strategy_sharpe_ratio` 减同策略 `06_ablate/baseline` 的同字段。该量为日收益年化 Sharpe，**不依赖年化长度约定**，两模式可直接比。
- **ΔCAGR / ΔMaxDD（standard）**：由本 worker 从 `06_ablate/<arm>/<strategy>/backtest_equity.csv`（1,161 行）重算 —— `CAGR = (eq_n/eq_0)^(252/n) − 1`（`n = 1161`）、MaxDD 为**全期峰谷**。**这是与三个子文件里已发表值完全一致的算法**（复算校验：cdhb `no_thrust` `+2.1446pp`、`no_inertia` `−3.1666pp`、`bull_full` `+3.3077pp` / `+1.7661pp`、`no_orthogon` `−0.7247pp`，逐位吻合约 4 位小数）。
  - **注意**：`comparison_report.json` 自带的 `strategy_cagr` 用的是另一套年化（cfsb/crb/cdhb 基线分别为 `24.76% / 27.83% / 17.46%`，而权益重算为 `25.90% / 29.13% / 18.24%`）。`INDEX.md` 的 `06_ablate` 行取的就是 `strategy_cagr`，故与本表的 standard ΔCAGR **不能混用**；本表一律用权益重算值。
- **一条必须写在数字前面的警告**：`mean_max_drawdown`（walkforward）是**折内 63 bar 回撤的 18 折均值**，standard 的 MaxDD 是**全期 1,161 bar 峰谷**。因此 **ΔMaxDD 的跨模式符号比较是在比两个不同的量**，它的证据力**弱于** ΔSharpe 的跨模式比较。下表照录（因为该符号确实翻转了），但**不得**把「ΔMaxDD 符号翻转」当作强有力的反证，它只是「连方向都不稳」的又一条旁证。

### 5.2 五个主判废臂（数字逐条）

| # | 臂 / 策略 | 指标 | standard | walkforward | 翻转方式 |
| :---: | :--- | :--- | ---: | ---: | :--- |
| 1 | `no_orthogon` / **crb** | **ΔSharpe** | **−0.1935** | **+0.2452** | **符号翻转**，且是 13 臂里 walkforward 最大正位移 |
| | | ΔCAGR | −3.92pp | +1.03pp | **符号翻转**（standard 是最大负 CAGR 之一） |
| | | ΔMaxDD | +1.34pp | +0.23pp | 同号，量级缩 5.8× |
| 2 | `no_thrust` / **crb** | **ΔSharpe** | **−0.1270** | **+0.1858** | **符号翻转** |
| | | ΔCAGR | −7.54pp | −1.07pp | 同号，量级缩 7× |
| | | ΔMaxDD | −2.31pp | −0.31pp | 同号，量级缩 7.5× |
| 3 | `no_thrust` / **cdhb** | **ΔCAGR** | **+2.14pp** | **−4.74pp** | **符号翻转**，且 walkforward 侧是 cdhb 最大的拆除损失 |
| | | ΔSharpe | +0.3690 | +0.0905 | 同号，量级缩 4× |
| | | ΔMaxDD | −2.74pp | −0.83pp | 同号，量级缩 3.3× |
| 4 | `bull_full` / **cdhb** | **ΔSharpe** | **+0.1210** | **−0.3029** | **符号翻转**，且 walkforward 侧是 13 臂最差 |
| | | ΔCAGR | +3.31pp | −0.13pp | **符号翻转** |
| | | ΔMaxDD | +1.77pp | +0.87pp | 同号（唯一跨模式稳定的方向：敞口↑ ⇒ 回撤↑） |
| 5 | `no_orthogon` / **cdhb** | **ΔSharpe** | **+0.1230** | **−0.0568** | **符号翻转** |
| | | **ΔMaxDD** | **−0.72pp** | **+0.15pp** | **符号翻转**（标准模式关掉正交化「更安全」，walkforward「更危险」） |
| | | ΔCAGR | +1.72pp | +1.60pp | 同号，量级几乎不变 |

**对以上 5 条的一致处置：`不可解释，不得引用`。** 理由不是「某一侧算错了」，而是两侧都合规：同一个 override、同一段数据、同一 universe，只换了评估模式就产生反号。**任何一侧的符号都不能被当作该模块的贡献证据**，也不能挑「更符合直觉」的那一侧来用。

### 5.3 补充：符号翻转但两侧都落在噪声带内（同样是「不可判定」，不是「作废」）

下列 12 条的跨模式对比中**至少有一项指标反号**（多数是 ΔSharpe 或 ΔMaxDD / ΔCAGR），且**两侧幅度都落在噪声带内**（|ΔSharpe| ≲ 0.07–0.15，折级胜折数在 3/18–10/18 之间摆动）。按 §6 引用的判定规则，它们**不是「作废」，而是「不可判定」**——差别在于：不可判定=「本样本量下无法分辨」，作废=「这个实验测不到那个模块」。两者都**不得**用于排序。

| 臂 / 策略 | ΔSharpe standard | ΔSharpe walkforward | 备注 |
| :--- | ---: | ---: | :--- |
| `no_structregime` / **cfsb** | **+0.2453** | **−0.0370** | **幅度差 0.28，是本补充表里唯一不容忽视的一条**：机制在 cfsb 上可达（§3.2），而 ΔCAGR 从 `+5.57pp` 塌到 `+0.26pp`。标准模式的 +5.57pp 是**单一路径产物**，walkforward 只留下 1/20 的残余 ⇒ 这条在 `ABLATION_AND_ANATOMY.md` §3.3 里被当作「cfsb 上 reachable 且有效」的正例，**该用法必须收回**（见 §8） |
| `no_atrstop` / cfsb | +0.1529 | −0.0080 | 两侧一正一近零；ΔCAGR 两侧同号为正（+2.65pp → +0.23pp）⇒ 该「同号」只支持「把自适应乘数固定为 4.5× 后收益不降」，**不支持**任何「止损是净拖累」的读法（该臂未关止损，见 §6.1 勘误） |
| `no_cyq` / cfsb | −0.1005 | +0.0263 | ΔCAGR 两侧同号为负（−0.48pp → −1.39pp），ΔMaxDD 两侧同号为正（+6.06pp → +0.16pp）⇒ 回撤保护的方向可保留（§6） |
| `no_thrust` / cfsb | +0.1450 | −0.0756 | ΔCAGR 两侧反号（+0.15pp → −5.94pp）；walkforward 侧的 −5.94pp 是该策略最大，但 standard 侧为 +0.15pp ⇒ **量级结论只能引 walkforward 单侧并标注模式依赖** |
| `no_voltarget` / cfsb | +0.0555 | −0.0474 | 两侧都在噪声带内 |
| `no_hysteresis` / cfsb | +0.0092 | −0.0011 | 两侧都在 0.01 量级 |
| `no_sectorthrot` / crb | +0.0162 | +0.0284 | ΔSharpe 同号；但 ΔCAGR（+0.12 → −0.20）与 ΔMaxDD（+2.39 → **−0.13**）均反号 |
| `no_sectorthrot` / cdhb | +0.0235 | +0.0484 | ΔSharpe 同号；ΔCAGR（+0.46 → −0.58）、ΔMaxDD（−1.03 → +0.05）均反号 |
| `no_cyq` / cdhb | +0.0497 | +0.0348 | ΔSharpe 同号；ΔMaxDD 反号（−0.48 → +0.13） |
| `no_inertia` / cdhb | −0.1322 | −0.1392 | ΔSharpe 同号；ΔMaxDD 反号（−2.09 → +0.65） |
| `bull_full` / crb | −0.2501 | −0.1816 | ΔSharpe 同号且量级大；ΔCAGR 反号（−2.65 → **+6.09**）⇒ 收益侧不可引用，Sharpe/回撤侧可（§6） |
| `no_orthogon` / cfsb | +0.0980 | +0.1038 | ΔSharpe 同号；ΔCAGR 反号（+1.00 → −0.22） |

⇒ **本节合计 5 条「不可解释、不得引用」+ 12 条「不可判定」。** 一个模块在本网格上能留下**跨模式同号且幅度可控**的证据的比例，远低于「13 臂 × 3 策略 = 39 个格子」给人的印象。

> [!WARNING]
> **勘误（2026-10-25）：上一版本节 `no_atrstop` 行的备注曾写「CAGR 侧的『净拖累』方向可保留（§6）」——该推断作废。**
>
> **原文（逐字）**：`| no_atrstop / cfsb | +0.1529 | −0.0080 | 两侧一正一近零；但 **ΔCAGR 两侧同号为正**（+2.65pp → +0.23pp）⇒ CAGR 侧的「净拖累」方向可保留（§6） |`
>
> **为什么错**：ΔCAGR 两侧同号为正这是**数字事实**（+2.65pp / +0.23pp 均未改动），但它被用来支撑一个**错误的主体**——`no_atrstop` 臂只把 `blend_enable_adaptive_stop_atr` 置 `false`，`*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`，**ATR 止损从未被关闭**（机制与三份产物证据见 §6.1 勘误）。所以「同号为正」只能支持「把自适应乘数固定为常数 4.5× 后收益不降」，不能支持「止损削收益」。
>
> **正确写法**：本节该行的备注已改为「该『同号』只支持『把自适应乘数固定为 4.5× 后收益不降』」，判定类别（本表=「不可判定」）不变。

---

## 6. 跨策略一致（可引用的结论）

**引用规则（沿用三个子文件与 `ABLATION_AND_ANATOMY.md` §2.2 / `docs/backtest_research.md` §11）：**

- 只有**方向在三策略上一致**、且**幅度超过噪声下限**的项才可引用；
- **&#124;ΔSharpe&#124; ≤ ~0.07 且折级胜折数在 3/18–10/18 之间广泛分裂 ⇒ 判「不可判定」，不是「惰性」、不是「无效」、更不是「有贡献」**；
- 跨模式符号翻转的臂（§5）一律不引用；
- 单折 CAGR 极差（cfsb `−22.70% … +182.65%`）比任何两臂之差大一个数量级 ⇒ 折均值差值本身不是显著性。

### 6.1 可引用（方向三策略一致）

| 结论 | cfsb | crb | cdhb | 折级稳定性 | 判定 |
| :--- | ---: | ---: | ---: | :--- | :---: |
| **`no_inertia`：惯性/换手门真实存在，且同时是收益门与换手门** | ΔSharpe **−0.1835** | ΔSharpe **−0.1992** | ΔSharpe **−0.1392** | 胜折 9/18、10/18、8/18（分裂）；但 ΔSharpe 幅度是三策略里最大的负位移之一，且**standard 模式同向**（−0.0603 / −0.1054 / −0.1322） | **可引用**（附折级分裂限定） |
| ↳ 换手侧独立证据（**不是** Sharpe 侧） | 调仓日 **442 → 839**（1.90×） | 467 → 863 | 500 → 852 | 由 `walkforward_rebalances.csv` 的去重 `(fold,date)` 计数直接得出 | **可引用**：关掉惯性后换手近似翻倍，同时折均值 Sharpe 下降 ⇒ 惯性门不是「什么都不做」的装饰 |
| **`bull_full`（牛市敞口 0.8→1.0）：系统性放大折内回撤** | ΔMaxDD **+0.56pp**，折内回撤在 **14/18** 折变深 | ΔMaxDD **+1.25pp**，**17/18** 折变深 | ΔMaxDD **+0.87pp**，**15/18** 折变深 | **三策略 46/54 折同向变深**，是本网格唯一「回撤方向逐折几乎一致」的臂 | **可引用**：「0.8 的上限在赚它的钱」 |
| ↳ Sharpe 侧 | **−0.3057**（13 臂最差） | **−0.1816** | **−0.3029** | 胜折 6/18、7/18、5/18 | **可引用**：三种策略上 Sharpe 全部下降，且 cfsb/cdhb 的降幅远超噪声带 |
| ↳ 收益侧 | ΔCAGR −1.94pp | **+6.09pp** | −0.13pp | 8/18、8/18、7/18 折 CAGR 更好 | **不可引用**：符号不一致 ⇒ 只能说「提高敞口有时能换到收益，但不稳定」 |
| **`no_atrstop`：把自适应止损乘数固定为常数 4.5× 后折均值收益上升**（该臂**未**关闭止损，见下方勘误） | ΔCAGR **+0.23pp** | ΔCAGR **+2.08pp** | ΔCAGR **+0.62pp** | CAGR 更优折数 7/18、11/18、10/18 | **可引用**（仅限主体「自适应乘数 vs 常数 4.5×」）：三策略固定乘数后 CAGR 全部上升 |
| ↳ 但 Sharpe 侧不可引用，且「不要移除止损」这半条被新证据加强 | ΔSharpe −0.0080 | +0.1245 | +0.0670 | 胜折 5/18、11/18、10/18；cfsb 侧符号还是反的（§5.3） | **不可判定**：只能说「本窗口未观测到**自适应乘数**的风险调整后增益」；而「**不得**移除 ATR 止损」不仅仍成立，还被 `09_param_grid/atr/atr_none` 的真实移除臂加强（`cdhb_stop_mode="none"`：ΔCAGR **−0.1590pp**、ΔMaxDD **+0.2137pp**，收益没升、回撤更深） |
| **`no_cyq`：CYQ 筹码质量门是回撤保护器** | ΔMaxDD **+0.16pp** | ΔMaxDD **+0.62pp** | ΔMaxDD **+0.13pp** | 折内回撤变深折数 9/18、13/18、12/18 | **cfsb / crb 可引用**（且 standard 侧同向：+6.06pp / +3.75pp）；**cdhb 的 +0.13pp 在噪声内**，不引用 |

补充（`no_inertia` 的收益侧）：ΔCAGR `−1.95pp / −0.34pp / −2.34pp`，三策略同号为负，但 crb 侧只有 `−0.34pp`。⇒ 可引用为「**方向一致**」，量级以 cfsb / cdhb 为准。

> [!WARNING]
> **勘误（2026-10-25）：本节 `no_atrstop` 行的结论主体写错了——该臂从未关闭 ATR 跟踪止损，上一版「ATR 跟踪止损在本窗口是净拖累 / 三策略关掉止损后 CAGR 全部上升」作废。**
>
> **原文（逐字保留，以便对照）**：
>
> ```
> | **`no_atrstop`：ATR 跟踪止损在本窗口是净拖累** | ΔCAGR **+0.23pp** | ΔCAGR **+2.08pp** | ΔCAGR **+0.62pp** | CAGR 更优折数 7/18、11/18、10/18 | **可引用**（方向）：三策略关掉止损后 CAGR 全部上升 |
> | ↳ 但 Sharpe 侧不可引用 | ΔSharpe −0.0080 | +0.1245 | +0.0670 | 胜折 5/18、11/18、10/18；cfsb 侧符号还是反的（§5.3） | **不可判定**：只能说「本窗口未观测到止损的风险调整后增益」，**不得**写成「应当移除 ATR 止损」 |
> ```
>
> **为什么错（机制层）**：`no_atrstop` 的唯一 override 是 `{"blend_enable_adaptive_stop_atr": false}`，**未触碰 `stop_mode`**。该符号在 `pipeline/research/blend/blend_base.py` 全文件只出现两处——`:612`（`resolve_param` 读取）与 `:892`（`if enable_adaptive_stop_atr:`）——而止损块由 `:875` 的 `if stop_mode in ("atr_trailing", "fixed_pct")` 守卫、由 `:914` 的 `if curr_p < trailing_stop_p:` 触发，与它无关；开关为 `False` 时唯一差别是 `:891` 的 `eff_stop_mult = stop_atr_mult`（4.5）不再被 `:892–896` 的 `clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5)` 覆盖。⇒ **该臂测的是「自适应乘数 → 常数 4.5×」，「止损」始终在岗。**
>
> **三份产物证据（6/6 叶：`06_ablate` 与 `08_ablate_wf` 各 3 策略，逐字复核）**：① `run_manifest.json` → `cli_overrides` 仅 `{"blend_enable_adaptive_stop_atr": false}`；② `strategy_dump.json` → `params` 里 `*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`；③ `run.log` 第 2 行 `Parameters: {...}` 运行期生效参数同值（本文件依赖的 `dump_vs_log_agree` 前提未被破坏）。
>
> **为什么错（流程层）**：本报告 §1.1 明言「臂定义与 `extra` JSON 逐字取自 `docs/backtest_research.md` §8 `run_arm`」，而该处 `no_atrstop` 行及其「语义边界披露」早已写明：关闭后「回退至固定 4.5x ATR 追踪止损，**并不是**彻底关闭底层 ATR 止损机制本身（彻底关闭需设置 `"${pfx}_stop_mode": "none"`）」。**臂定义读对了、解释结论时却按标签字面意思走** —— 这是本次误标的根因。
>
> **反向证据（真正的「止损关闭」臂）**：`09_param_grid/atr/atr_none`（`cdhb_stop_mode = "none"`，18 折、`2022-01-04`→`2026-10-22`、0.25y/0.25y、`--cash-sweep-rate 0.015`）对 cdhb 为 **ΔCAGR −0.1590pp、ΔMaxDD +0.2137pp（更差）**（`09_param_grid/atr/atr_none/chan_dual_hybrid_blend/walkforward_summary.json` 对 `08_ablate_wf/baseline/chan_dual_hybrid_blend/`；该臂 `run.log` 生效参数为 `blend_enable_adaptive_stop_atr: True` + `cdhb_stop_mode: 'none'`，与 `no_atrstop` 恰好互补）。**把止损真正拿掉，收益没升、回撤反而更深。**
>
> **仍然成立的（主体已更换）**：
> - 「把自适应乘数固定为 4.5× 后，三策略折均值 CAGR 全部上升（+0.23 / +2.08 / +0.62pp）」这个**描述性事实**不变（数字、折数、`7/18、11/18、10/18` 均未改动），可引用；主体必须写成「自适应乘数」。
> - 该臂**非 ≡**（§3.1 的 `391 / 57 / 51 / 5,911.02` 等、§4.2 的 `✓ → ✓`）仍然成立且自洽——固定乘数确实改变了 `trailing_stop_p`，从而改变止损价格与成交。
> - 「**不得**移除 ATR 止损」这半条**证据变强**：`atr_none` 是这一禁令的第一份正向实证。
>
> **判废范围**：作废的是**主体为「ATR 跟踪止损」的因果解释**与**该行「可引用（方向）」的评级**；所有数字、折数、≡/非≡ 判定全部保留。

### 6.2 不可判定（不得排序、不得说「无效」）

| 臂 | 三策略 &#124;ΔSharpe&#124; | 折级胜折数 | 判定 |
| :--- | :--- | :--- | :---: |
| `no_voltarget` | 0.0474 / 0.0927 / 0.0659 | 7/18、12/18、5/18 | **不可判定** |
| `no_structregime` | 0.0370 (cfsb) / **≡** (crb, cdhb) | 9/18、—、— | cfsb 不可判定；crb/cdhb 零信息量（§3.2） |
| `no_cyq`（Sharpe 侧） | 0.0263 / 0.0355 / 0.0348 | 10/18、8/18、9/18 | Sharpe 侧**不可判定**；只有 ΔMaxDD 侧可引用（§6.1） |
| `no_atrstop`（Sharpe 侧） | 0.0080 / 0.1245 / 0.0670 | 5/18、11/18、10/18 | **不可判定** |
| `no_lowbeta` | 0.0690 / 0.0267 / 0.0086 | **0/18**、3/18、2/18 | 方向在 cfsb 上逐折一致（0/18 反证）但幅度落在噪声带内 ⇒ 只能写「方向一致、量级不可辨」 |
| `no_sectorthrot` | 0.0710 / 0.0284 / 0.0484 | 3/18、6/18、7/18 | **不可判定**；且 cfsb / crb 的该臂只测了「行业上限 2→20」一个杠杆（throttle 本就为 `False`），与 cdhb 的复合杠杆**不是同一实验，不得横向对比** |
| `no_hysteresis` | ≡ 之外只有 cfsb 非 ≡：0.0011 | 9/18 | **不可判定**（cfsb）；crb/cdhb 零信息量（§3.3） |
| `no_circuit` | 0.0010 / 0.0014 / ≡ | 0/18、1/18、— | **样本缺失**，不是「不可判定」：熔断在 18 折里只上场 1 折（cfsb）⇒ 任何「有效/无效」判断都缺样本（§3.4、§4.4） |

### 6.3 一句话总结本节的边界

**在 39 个格子里，能跨三策略同向、且幅度超过噪声带、且不跨模式反号的，只有三条**：① `no_inertia`（惯性门有真实收益与换手成本）；② `bull_full` 的**回撤侧**（0.8 的上限确实压住了折内回撤）；③ `no_atrstop` 的**收益侧方向**——精确主体是「把**自适应止损乘数**固定为常数 4.5× 后收益上升」，**不是**「止损在本窗口削收益」（该臂从未关闭止损，见 §6.1 勘误）。其余全部落在「不可判定 / 零信息量 / 作废 / 不可解释」四类里。

> [!WARNING]
> **勘误（2026-10-25）：上面第 ③ 条原写作「止损在本窗口削收益」——主体错误，已改为「自适应止损乘数」。**
>
> **原文（逐字）**：「③ `no_atrstop` 的**收益侧方向**（止损在本窗口削收益）」。
>
> **为什么错**：`no_atrstop` 的 override 只有 `blend_enable_adaptive_stop_atr: false`，`*_stop_mode` 仍是 `"atr_trailing"`、`*_stop_atr_mult` 仍是 `4.5`，止损照常由 `blend_base.py:875` 守卫、`:914` 触发（三份产物证据与机制见 §6.1 勘误）。「止损削收益」这个读法从未被任何一臂测到过。
>
> **对「只有三条」这一计数的影响：无。** ③ 仍是三条之一，只是主体从「止损」换成「自适应乘数」；跨三策略同向、幅度、「不跨模式反号」三条判据全部不变（ΔCAGR `+0.23 / +2.08 / +0.62pp`，standard 侧 `+2.65pp`）。**计数不改、臂不改、数字不改，改的是主语。**
>
> **反向证据**：真正的止损移除臂 `09_param_grid/atr/atr_none`（`cdhb_stop_mode="none"`）对 cdhb 的 ΔCAGR **−0.1590pp**、ΔMaxDD **+0.2137pp**（更差）——按本篇自己的判据，这条都不构成「止损削收益」。

---

## 7. DSR 在本网格不可用

### 7.1 为什么不可用（实现层面的硬约束）

`deflated_sharpe_ratio` 的实现（`common/metrics.py:198-215`）先算 **best-of-`n_trials` 的期望最大 Sharpe（即 deflation 门槛 `sr0`）**，再据此折算概率：

- `common/metrics.py:187-196` `expected_max_sharpe(n_trials, sharpe_std)` —— Bailey & López de Prado 的 Euler–Mascheroni 近似：
  `sr0 = sharpe_std × [(1−γ)·Φ⁻¹(1−1/n) + γ·Φ⁻¹(1−1/(n·e))]`，`γ = 0.5772156649`。
- `common/metrics.py:212` `sr0 = expected_max_sharpe(n_trials, sharpe_std)`；`:214` `z = (observed_sharpe − sr0) × √(n_obs−1) / denom`。

在 walkforward 路径下，调用方传的是 **`n_trials = n_valid_folds = 18`**、`sharpe_std = fold_sharpe_std`（折间 Sharpe 标准差）、`n_obs = 63`（每折 bar 数）。本网格的实测门槛（本 worker 用 `common/metrics.py` 同式复算）：

| 输入 | `fold_sharpe_std` | `sr0`（deflation 门槛） |
| :--- | ---: | ---: |
| cfsb `baseline` | 2.2836 | **4.2336** |
| crb `baseline` | 2.1844 | **4.0495** ← 即「≈ 4.05」 |
| cdhb `baseline` | 2.2141 | **4.1046** |
| 全网格（39 个 `walkforward_summary.json`） | **1.9153 … 2.3908** | **3.5507 … 4.4322** |

⇒ **门槛 ≈ 4.05（crb 基线），全网格 3.55–4.43**，而 39 次运行的 `mean_sharpe_ratio` 只有 `1.1221 … 1.7821`。**没有任何一臂的 Sharpe 达到门槛的一半。**

### 7.2 实测 DSR 读数

| 策略 | `deflated_sharpe_ratio` 全 13 臂区间 | 网格内最大值 |
| :--- | :--- | :--- |
| `cfsb` | `5.242e-79 … 4.647e-47` | `no_orthogon` 4.647e-47 |
| `crb` | `1.905e-68 … 1.377e-19` | `no_orthogon` **1.377e-19**（全网格最大） |
| `cdhb` | `5.401e-70 … 1.188e-32` | `no_thrust` 1.188e-32 |

⇒ **39/39 全部落在 `~1e-19` 或更低**，数值上就是 0。

- **禁止**用 DSR 给 13 臂排序；
- **禁止**把 `no_circuit` 的 `1.553e-54` 与 `bull_full` 的 `1.307e-69` 说成「前者显著优于后者」——两者都是「没通过门槛」，差别只是浮点指数；
- **禁止**把 `crb no_orthogon` 的 `1.377e-19` 说成「唯一接近显著的一臂」——它离门槛 4.05 仍差 2.27 个 Sharpe 单位。

根因不是某一臂的问题，而是**本折网格的噪声下限**：`fold_sharpe_std ≈ 1.92–2.39` 远大于折均值 Sharpe `1.12–1.78`。DSR 在这里度量的是「折间离散度有多大」，而不是「哪一臂更好」。

### 7.3 与 `03_r2b_oos` 的 0.5857 的关系

`FORENSIC_AUDIT.md` 引用的 cdhb **DSR = 0.5857** 来自 **`03_r2b_oos/chan_dual_hybrid_blend`**（本 worker 已核对其 `walkforward_summary.json`：`deflated_sharpe_ratio = 0.5856692509180732`、`n_folds = 6`、`fold_sharpe_std = 1.9299`、`requested_start = 2025-01-02`、`requested_end = 2026-10-22`；其 `run.log` 打印 `Deflated Sharpe Ratio: 0.586 (n_trials=6, fold Sharpe std=1.930)`）。

**这是一个不同的网格**：`n_trials = 6`（vs 本组 18）、6 折 × 约 63 bar 的更短窗口（2025-01→2026-10，vs 2022-01→2026-09）、现金 0%（vs 1.5%）。`sr0` 只随 `(n_trials, fold_sharpe_std)` 变化，两组根本不在同一条门槛下。

⇒ **本报告的 39 次运行既不重现、也不否证 `0.5857`。** 任何「用 08_ablate_wf 的 DSR 去支持或推翻 0.5857」的写法都是错的；反过来，「0.5857 在更长窗口上塌成 ≈0」这句话**也不能**由本组推出——因为本组换了 `n_trials`，两个读数不可比。

---

## 8. 结论：哪些结论取代了 `ABLATION_AND_ANATOMY.md`

`ABLATION_AND_ANATOMY.md` 的全部结论在 **standard（连续净值）** 口径下成立。本节的判据只有一条：**该主张在 walkforward 口径下是否仍然成立**。三档状态：

- **保留** = walkforward 下方向与量级都站得住，原句可直接沿用；
- **修正** = 方向站得住但量级/理由/适用范围必须改写；
- **作废** = walkforward 下反号、或实测为 ≡/不可判定，原句不得再引用。

| # | `ABLATION_AND_ANATOMY.md` 的主张（standard 口径原文摘要） | walkforward 状态 | 依据 | 引用时的正确写法 |
| :---: | :--- | :---: | :--- | :--- |
| 1 | 惯性/换手过滤（`no_inertia`）是真实且高价值的杠杆：调仓 462→893 / 480→947 / 503→917，CAGR 全降 −2.73/−3.70/−3.02pp，Sharpe 全降 −0.060/−0.105/−0.132 | **保留** | §6.1：ΔSharpe −0.1835 / −0.1992 / −0.1392（三策略同向、幅度最大）、调仓日 442→839 / 467→863 / 500→852 | 「惯性门同时是收益门与换手门」——**唯一一条跨三策略、跨两模式同向的结论**。加限定：折级胜折数 8–10/18 分裂 |
| 2 | CYQ 质量门（`no_cyq`）主要是回撤保护器：四态 +6.06pp、风险 +3.75pp 回撤，CAGR 只 −0.45/−0.67pp | **修正** | §6.1：ΔMaxDD 仅 +0.16 / +0.62 / +0.13pp（同号但**量级缩 10–24×**），ΔSharpe 全在噪声带内 | 方向保留（关掉 CYQ ⇒ 折内回撤变深，crb 13/18 折、cdhb 12/18 折），**量级必须改写成 pp 级而非「3.8–6.1pp 的回撤保护」** |
| 3 | 行业集中度上限（`no_sectorthrot`）净效果是压回撤：风险 +2.39pp、四态 +1.38pp | **作废** | §6.2：walkforward ΔMaxDD `−0.13 / +0.13 / +0.05pp`，三策略**符号不一致且全部落在噪声内**；ΔSharpe 0.0484–0.0710 | 只能写「本 20 票 universe 上行业上限约束力弱」（沿用 `chan_risk_managed_blend/_metrics.md` §7.2 的写法）；**不得**写「放开上限会加深回撤」 |
| 4 | ATR 跟踪止损（`no_atrstop`）在本窗口是净损耗：关闭后 CAGR +2.52 / +0.98 / +1.01pp | **重新判定（原句主题错位，2026-10-25）** | `no_atrstop` 只关 `blend_enable_adaptive_stop_atr`，`*_stop_mode` 仍为 `atr_trailing`（§6.1 勘误）；本口径 ΔCAGR `+0.23 / +2.08 / +0.62pp` 三策略同号为正，但 cfsb 从 +2.52 缩到 +0.23、crb 从 +0.98 涨到 +2.08；真实移除臂 `09_param_grid/atr/atr_none`（`cdhb_stop_mode="none"`）ΔCAGR **−0.1590pp**、ΔMaxDD **+0.2137pp** | 「本窗口把**自适应止损乘数**固定为 4.5×，三策略折均值 CAGR 都上升」✅（主体必须是「乘数」，不是「止损」）；「本窗口关掉 ATR 止损使 CAGR 上升」❌ **作废**（真实移除后不升）；「止损换来了更低回撤」❌（walkforward ΔMaxDD `−0.08 / +0.09 / −0.13pp`，无一策略同向）；「所以应当移除止损」**禁止**，且被 `atr_none` 加强 |
| 5 | 波动率目标（`no_voltarget`）是净损耗，但三策略全部落在噪声下限内 | **保留** | §6.2：ΔSharpe 0.0474 / 0.0927 / 0.0659，胜折 7/18、12/18、5/18 | 「没有可测到的正贡献」，**不得**升级为「应删除」 |
| 6 | 牛市满仓（`bull_full`）用回撤换收益；「抬高回撤」可信、「抬高收益」不可信 | **保留（且回撤侧证据强得多）** | §6.1：折内回撤在 **14/18、17/18、15/18** 折变深（46/54），ΔMaxDD +0.56 / +1.25 / +0.87pp；ΔCAGR −1.94 / +6.09 / −0.13pp（仍不同号） | 「0.8 的牛市敞口上限确实压住了折内回撤」——这是本网格**最稳的一条风险结论**；收益侧仍不可引用 |
| 7 | 正交化（`no_orthogon`）符号不稳定，不得声称有效/无效 | **保留（且更强）** | §5：crb `−0.1935 → +0.2452`、cdhb `+0.1230 → −0.0568`（Sharpe 反号）且 cdhb ΔMaxDD 也反号 | 「该模块的 OOS 价值未被证实」，**禁止**任何方向的断言 |
| 8 | 推力现金部署（`no_thrust`）符号不稳定；唯一稳健次结论是「去掉它会大幅降低换手」（Σ换手 −33%~−42%） | **保留（换手侧）/ 作废（收益侧）** | 换手：调仓日 442→377、467→373、500→396（由 §3 对齐表 `only_*` 推得：arm 总数 = 对齐 + `only_arm`）；ΔCAGR −5.94 / −1.07 / −4.74pp，但 standard 侧为 +0.15 / −7.54 / +2.14pp | **只能引用换手侧**（三策略同向降）。收益侧两侧都反号 ⇒ 按 §5 判「不可解释」 |
| 9 | `no_structregime` / crb, cdhb 作废：**结构性不可达**（代码级确证） | **保留** | §3.2 已由本 worker 独立重验源码：`:468` 在 `:465` 的 `regime_*` 分支内，`:445` 的 `rolling_ic_ir` 分支每条出口都 `return`；`:769` 只预计算 `median_d`，其唯一消费点仍是 `:468` | 「该臂对 crb/cdhb 零信息量，其『无影响』不得表述为『结构性状态机无效』」 |
| 10 | `no_hysteresis` / crb, cdhb 作废：**基线本已关闭**（`rs/config.py:794` 默认 `False`，params 无此键） | **保留** | §3.3 重验：`cfsb` dump 该键 `True`、crb/cdhb dump `<<ABSENT>>` | 同上；要评价 hysteresis 必须先在 crb/cdhb 造「基线开启」的对照臂 |
| 10b | （`ABLATION_AND_ANATOMY.md` §3.3 附带）cfsb 的 `no_structregime` **可达**且 `+5.30pp CAGR` 是正例 | **作废** | §5.3：standard `+5.57pp / ΔSharpe +0.2453` → walkforward **`+0.26pp / −0.0370`**，符号与量级双崩 | 只能保留「cfsb 上该机制**可达**」这一**可达性**事实（§3.2 已独立确证），**不得**保留「它贡献了 +5.3pp」 |
| 11 | `no_lowbeta` / cdhb 作废（逐位相同）；`/ cfsb` 作废（相对差 1.6e-4「实质惰性」） | **作废（理由）→ 修正** | §4.3 反称 #1：cdhb standard ≡(0.0) → walkforward 对齐 492 / **未对齐 15** / `max&#124;Δ&#124; 792.52`；cfsb standard ≈(47.94) → walkforward 440 对齐 / **20 个差异调仓日** / `2123.30` | 「低 beta 惯性在本策略/本窗口**没有可测到的净效果**」✅；「该模块什么也没做」❌——它在折内确实改变了成交（抵消 vs 惰性，见 §4.4） |
| 12 | `no_circuit` / cfsb, crb 作废（逐位相同）；`/ cdhb` 实质惰性（43.3 元）；§7.4「三档熔断器在这条 4.8 年路径上贡献恒为 0」 | **作废 → 修正** | §4.3 反称 #2/#3/#4：cfsb standard ≡ → walkforward **20 个差异调仓日、全在折 4、max 138.84**；crb standard ≡ → walkforward `only_arm = 1`、219.91；cdhb standard ≈(43.28) → walkforward **≡** | **「熔断在 18 折里只上场 1 折（cfsb 的折 4）」**✅；「贡献恒为 0」❌（标准模式看不到 ≠ 没发生）；「熔断无效」❌；「熔断有效」❌。任何「有效/无效」判断都**缺样本** |
| 13 | `04_r1_std_cb_compare` 信息量为零；两臂 **726 根**逐位相同（2022-01-04→2024-12-31）；`INDEX.md` 写的「730 bars」是笔误 | **保留** | 本 worker 重算：两臂 `backtest_equity.csv` 各 **726** 行、`comparison_report.json` 的 `overlap_bars = 726`、区间 `2022-01-04 → 2024-12-31`、对齐 726/726 `max&#124;Δ&#124; = 0.0` | 「04 阶段零信息量」成立；其 726 根窗口**不可**与 1,161 根阶段并列。`INDEX.md` 的 730 已在本次一并改为 726 |
| 14 | `no_sectorthrot` 暴露配置命名缺陷：`{cfsb,crb}_adaptive_sector_throttle` / `{cfsb,crb}_max_assets_per_sector` 被 `StrategyConfig.from_dict` 静默丢弃 | **保留（且 08 已验证修复生效）** | 08 的 39 个 `run.log` 中 `ignoring unknown key(s)` 命中 **0** 个文件（cfsb/crb 只发全局键 `blend_max_assets_per_sector`） | 「cfsb/crb 的该臂只测了『行业上限 2→20』一个杠杆」✅；与 cdhb 的复合杠杆**不得横向对比** |
| 15 | 噪声下限：&#124;ΔCAGR&#124; < 3pp 且 &#124;ΔSharpe&#124; < 0.15 的臂，符号与量级都不应被当作「因子的贡献」 | **修正（尺度）** | `ABLATION_AND_ANATOMY.md` §2.2 的 ±3pp/±0.15 来自 `01_r1_longwin` 的 4 个**高度重叠**的 2 年折；本网格 18 个不重叠季度折的实测离散度为 `fold_sharpe_std = 1.9153…2.3908`（39 次运行）、折级 CAGR 极差 `−22.70% … +182.65%` | walkforward 侧应采用各子文件的规则：**&#124;ΔSharpe&#124; ≤ ~0.07 且胜折数广泛分裂 ⇒ 不可判定**；跨模式反号 ⇒ 作废。两条同时满足才引用 |
| 16 | 审计的 DSR 证据（cdhb 0.5857）在本口径不可复现 | **保留（且给出充分条件）** | §7：本网格 `n_trials = 18`、门槛 `sr0 ≈ 3.55–4.43`、39/39 DSR ≤ `1.377e-19`；`0.5857` 来自 `03_r2b_oos` 的 `n_trials = 6` 网格 | 「两者网格不同，既不重现也不否证」——**双向**都不得越界 |
| 17 | （跨阶段）`01_r1_longwin` 的「均值」不是预期年化；`03_r2b_oos` 的一切绝对值不可引用 | **保留（未被本组触及）** | 本组未重跑这三个阶段，仅沿用其既有口径声明 | 不变 |

> [!WARNING]
> **勘误（2026-10-25）：本表第 4 行原判定为「保留（方向）/ 修正（量级）」，且原句主语是「ATR 跟踪止损」——主语错误，该行已改为「重新判定」。**
>
> **原文（逐字保留，以便对照）**：
>
> ```
> | 4 | ATR 跟踪止损（`no_atrstop`）在本窗口是净损耗：关闭后 CAGR +2.52 / +0.98 / +1.01pp | **保留（方向）/ 修正（量级）** | §6.1：CAGR `+0.23 / +2.08 / +0.62pp`——**三策略同号为正**，但 cfsb 从 +2.52 缩到 +0.23、crb 从 +0.98 涨到 +2.08 | 「本窗口关掉 ATR 止损，三策略折均值 CAGR 都上升」✅；「止损换来了更低回撤」❌（walkforward ΔMaxDD `−0.08 / +0.09 / −0.13pp`，无一策略同向）；「所以应当移除止损」**禁止** |
> ```
>
> **为什么不能「保留方向」**：本表这一行的状态栏是**对主语的整体裁定**。`no_atrstop` 的 override 只有 `blend_enable_adaptive_stop_atr: false`，`*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`（三份产物证据 + `blend_base.py:875 / :891 / :892 / :914` 的机制链条见 §6.1 勘误）。⇒ 「关掉止损 ⇒ CAGR 上升」这个方向**在本网格从未被测过**，谈不上「保留」；被测过的是「自适应乘数 ⇒ 常数 4.5×」，它的方向可以保留（同一行的允许写法已如此改写）。
>
> **反向证据**：真实移除臂 `09_param_grid/atr/atr_none`（`cdhb_stop_mode="none"`，18 折，`2022-01-04`→`2026-10-22`）对 cdhb 为 **ΔCAGR −0.1590pp、ΔMaxDD +0.2137pp（更差）**（`09_param_grid/atr/atr_none/chan_dual_hybrid_blend/walkforward_summary.json` 对 `08_ablate_wf/baseline/chan_dual_hybrid_blend/`）。即本行原本的「✅」句在真实移除下**不成立**。
>
> **仍成立的部分**：ΔCAGR `+0.23 / +2.08 / +0.62pp` 三策略同号为正（本表数字未改）；「止损换来了更低回撤」❌ 不变；「所以应当移除止损」**禁止** 不变、且被 `atr_none` 加强。
>
> **与 §8.1 的关系**：§8.1 的「有 5 处必须改写」是**原始报告当时的计数**，本勘误是 2026-10-25 追加的**清单之外**第 6 项更正——为不改写既有计数，它**未并入该清单、`5` 这个数字保持原样**；读者引用「5 处」时应同时知道本节还有这一条。

### 8.1 一句话结论

**`ABLATION_AND_ANATOMY.md` 的骨架（哪些臂作废、为什么作废、哪三条结论可引用）在 walkforward 复核后基本成立，但有 5 处必须改写：**

1. `no_circuit` 的「贡献恒为 0」→ **「18 折中只上场 1 折」**（样本不足，不是零贡献）；
2. `no_lowbeta` 的「实质惰性」→ **「抵消」**（cdhb standard ≡ 是抵消出来的，折内一重置就露 792.52 元差异）；
3. `no_cyq` 的回撤保护量级从 `3.8–6.1pp` 改为 **`0.13–0.62pp`**（方向不变）；
4. `no_sectorthrot` 的「压回撤」→ **作废**（三策略反号且全在噪声内）；
5. cfsb `no_structregime` 的 `+5.3pp 正贡献` → **作废**（walkforward 只剩 +0.26pp / ΔSharpe 反号），只保留「机制可达」这一事实。

**最根本的一条（§4）：`standard` 模式下的 `≡` 不是「模块什么都没做」的证据。** 4 个格子在换评估模式后改变判定类别，方向各不相同（3 个从 ≡ 变非 ≡，1 个从 ≈ 变 ≡），且其中 2 个（`no_lowbeta`/cdhb、`no_circuit`/cfsb）正是 prior 报告用来「作废」对应臂的直接依据。

---

## 9. 复现与溯源

### 9.1 本报告的每个数字的来源

| 数字 | 来源文件（只读） | 本 worker 的处理 |
| :--- | :--- | :--- |
| 39 × (`mean_sharpe_ratio`, `mean_cagr`, `mean_max_drawdown`, `mean_calmar_ratio`, `n_folds`, `deflated_sharpe_ratio`, `fold_sharpe_std`, 折边界, `actual_last_fold_end`) | `08_ablate_wf/<arm>/<strategy>/walkforward_summary.json` | 脚本抽取，未转述 |
| 39 × walkforward `≡` 判定（对齐数 / `only_arm` / `only_base` / `max&#124;Δportfolio_equity`） | `08_ablate_wf/<arm>/<strategy>/walkforward_rebalances.csv` | **独立重算**（`(fold,date)` 对齐 + 组内单值断言） |
| 调仓日数（442/467/500 → 839/863/852） | 同上 | 独立重算（去重 `(fold,date)` 计数） |
| 折级胜折数、折内回撤变深折数、折级 ΔSharpe 区间 | 同上 `walkforward_summary.json` 的 `rolling_window_performance`（18 条） | **独立重算**（逐折与 baseline 配对） |
| standard `≡` / `≈` 判定、`max&#124;Δeq&#124;`、权益重算 CAGR / 全期 MaxDD | `06_ablate/<arm>/<strategy>/backtest_equity.csv`、`comparison_report.json` | **独立重算**（1,161 行逐日对齐） |
| standard ΔSharpe | 同上 `comparison_report.json` 的 `strategy_sharpe_ratio` | 直接相减 |
| 1,161 bar / 27 根尾部 bar | `06_ablate/baseline/*/backtest_equity.csv`（行数与日期）+ `08_ablate_wf` 各 summary 的 `actual_last_fold_end` | **独立重算**（`date > 2026-09-07` 计数 = 27） |
| `sr0` 门槛 3.5507–4.4322 / 4.0495 | `common/metrics.py:187-196` 同式 + 各 summary 的 `fold_sharpe_std` | **用源码公式复算** |
| `03_r2b_oos` 的 0.5857 / `n_trials=6` | `03_r2b_oos/chan_dual_hybrid_blend/walkforward_summary.json` + `run.log` | 直接读取 |
| `≡` 的代码级根因 | `pipeline/research/blend/blend_base.py`（`:434` `:445` `:447` `:451` `:462` `:463` `:465` `:468` `:648` `:769` `:1038`）、`pipeline/research_strategy/rs/config.py:794` `:909` | **直接读源码**（非转述） |
| 命令行口径 | `/tmp/opencode/ablwf/run_cfsb.sh`、`run_crb.sh`、`run_cdhb.sh`；`08_ablate_wf/chan_risk_managed_blend/_provenance/run_crb.sh` | 逐字比对三份 |
| 13/13 knobs 落地 | `08_ablate_wf/<arm>/<strategy>/strategy_dump.json`、`run_manifest.json`、`run.log` | 见三个子文件的逐键表；本 worker 复验了 39/39 `status/mode`、0 unknown-key、0 Traceback |

### 9.2 目录布局（本次已规范化）

三个 worker 的 `--results-dir` 写法不一致：`cfsb` / `crb` 是 **arm 在前**（`08_ablate_wf/<arm>/<strategy>/`），`cdhb` 是 **strategy 在前**（`08_ablate_wf/chan_dual_hybrid_blend/<arm>/`）。本次已把 13 个 cdhb 目录整体 `mv` 到 `08_ablate_wf/<arm>/chan_dual_hybrid_blend/`，**未复制、未残留、未改动任何文件内容**。现状：

```
08_ablate_wf/
├── chan_four_state_blend/_metrics.md          ← 策略级汇总（cfsb）
├── chan_risk_managed_blend/_metrics.md        ← 策略级汇总（crb）
├── chan_risk_managed_blend/_provenance/       ← 6 个文件：4 份导出 + gen_metrics.py + run_crb.sh
├── chan_dual_hybrid_blend/_metrics.md         ← 策略级汇总（cdhb）
├── baseline/{chan_four_state_blend,chan_risk_managed_blend,chan_dual_hybrid_blend}/
├── bull_full/{…同构…}/
├── no_atrstop/ · no_circuit/ · no_cyq/ · no_hysteresis/ · no_inertia/ · no_lowbeta/
├── no_orthogon/ · no_sectorthrot/ · no_structregime/ · no_thrust/ · no_voltarget/
                                                    ← 共 13 个臂目录，每臂 3 个策略目录 × 8 产物
```

### 9.3 本 worker 未做的事（职责与约束）

- **未重跑任何回测**（只读产物 + 只读源码），因此所有数字都是「对已落盘结果的复核」，不是独立复现；`/tmp/opencode/ablwf/` 下的 runner 脚本属临时目录，**只有 `crb` 的那份被归档进 `_provenance/`**，`cfsb` / `cdhb` 的脚本未归档（复现性缺口，建议后续补归档，但**本报告按约束不修改任何产物，故未补**）。
- 未修改任何运行产物、未修改 `docs/`、未修改 `06_ablate/`、未修改 `07_r3_oos_bear/`、未修改既有的四份报告（`ABLATION_AND_ANATOMY.md` / `BEAR_WINDOW_OOS.md` / `FORENSIC_AUDIT.md` / `forensic_overfit_report.md`）。
- 目录规范化只做 `mv`（`backtests_research/` 整体未被 git 跟踪 ⇒ `git mv` 不可用，实测 `git ls-files backtests_research | wc -l = 0`）。
- 未提出任何标的剔除建议（本网格不含 Leave-One-Out 分析）。

### 9.4 无法验证 / 已知残留

1. **`cdhb` 13 个 `run_manifest.json` 的 `group` 字段仍是 `08_ablate_wf/chan_dual_hybrid_blend`**（即规范化之前的 strategy-first 路径），不随臂变化。这是**产物内写入的历史字符串**，与目录实际位置不符。按「不得修改运行产物」，原样保留。若下游以 `manifest.group` 反查目录，需注意这一点。
2. `cfsb` / `cdhb` 的 runner 脚本只存在于 `/tmp/opencode/ablwf/`（临时目录，重启即失），未归档。
3. `08_ablate_wf` 只覆盖 3 个策略（cfsb / crb / cdhb），`06_ablate` 同样只有这 3 个——`03_r2b_oos` / `07_r3_oos_bear` / `01_r1_longwin` 里的 `chan_four_state_shield_blend`、`chan_crisis_shield_blend`、`generic_blend` **没有对应的消融数据**，本报告对它们无话可说。
4. 本网格**无法**给出全期（1,161 bar）的 walkforward 回撤/收益——该量在逐折重置净值下不存在。

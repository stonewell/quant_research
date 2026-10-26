# chan_risk_managed_blend（前缀 `crb`）—— 走查（walkforward）13 臂模块归因

> 本文件只覆盖 **`chan_risk_managed_blend` 单一策略**的 13 臂 walkforward 重跑。
> `chan_four_state_blend` / `chan_dual_hybrid_blend` 由其他 worker 负责，不在本文件范围内。
> 全部数字由本 worker 自己产出的 `walkforward_summary.json` / `walkforward_report.csv` / `walkforward_rebalances.csv` / `strategy_dump.json` / `run_manifest.json` / `run.log` 读取；少数只读引用（`06_ablate`、`docs/`、`common/metrics.py`、`rs/config.py`、`blend_base.py`）在正文逐处标注。无人工估算。

## 0. 结论速览

- **13/13 臂**：`status=completed`、`mode=walkforward`、`n_folds=18`、`Traceback=0`、`ignoring unknown key(s)=0` ⇒ **无臂作废**（逐臂核验见 §4）。
- **≡（对 baseline 数值恒等、零信息）恰好 2 个：`no_structregime`、`no_hysteresis`** —— 与标准模式（`06_ablate`）的既有发现**一致**，且本次补上代码级归因（§3.1），证明其恒等是**结构性**的、与评估模式无关。
- **标准模式下同样恒等的 `no_circuit` 在走查下不再恒等**（对齐 467 个 bar，最大权益差 219.9148 元），但经济量级可忽略（ΔSharpe +0.001447）。这是**折内权益重置**暴露出来的唯一一条「标准模式看不见、走查看得见」的差异。
- **两条结论在 OOS 逐折评估下符号翻转**（标准为负、走查为正）：`no_orthogon`（ΔSharpe 标准 -0.1935 → 走查 +0.2452）、`no_thrust`（标准 -0.1270 → 走查 +0.1858） ⇒ 这两个模块的「贡献」是单路径产物，不构成 OOS 证据。
- **两种模式下同向、且确实在承担风控职责的模块**：`no_inertia`（去惯性门后 Sharpe -0.1992、MaxDD +0.52pp）与 `bull_full`（牛市敞口 0.8→1.0 后 CAGR +6.09pp 却 Sharpe -0.1816、MaxDD +1.25pp）。

## 1. 13 臂 walkforward 指标总表

口径见 §5。`Δ*` = 「该臂 − baseline」，**ΔMaxDD 为正 = 回撤更深（更差）**。

| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | actual_last_fold_end | ΔCAGR(pp) | ΔSharpe | ΔMaxDD(pp) |
|---|---|---|---|---|---|---|---|---|---|---|
| `baseline` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.1925e-41 | 2026-09-07 | +0.00 | +0.0000 | +0.00 |
| `no_voltarget` | 1.6297 | 31.41% | 4.38% | 10.8425 | 18 | 1.5088e-33 | 2026-09-07 | +3.53 | +0.0927 | +0.22 |
| `no_orthogon` | 1.7821 | 28.91% | 4.38% | 8.8928 | 18 | 1.3767e-19 | 2026-09-07 | +1.03 | +0.2452 | +0.23 |
| `no_structregime` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.1925e-41 | 2026-09-07 | +0.00 | +0.0000 | +0.00 |
| `no_cyq` | 1.5015 | 26.91% | 4.78% | 10.0938 | 18 | 3.9538e-45 | 2026-09-07 | -0.97 | -0.0355 | +0.62 |
| `no_atrstop` | 1.6615 | 29.96% | 4.24% | 11.5584 | 18 | 2.9930e-37 | 2026-09-07 | +2.08 | +0.1245 | +0.09 |
| `no_lowbeta` | 1.5637 | 28.10% | 4.11% | 10.8378 | 18 | 3.3526e-40 | 2026-09-07 | +0.22 | +0.0267 | -0.04 |
| `no_sectorthrot` | 1.5653 | 27.68% | 4.02% | 10.6597 | 18 | 2.3744e-37 | 2026-09-07 | -0.20 | +0.0284 | -0.13 |
| `no_hysteresis` | 1.5370 | 27.88% | 4.15% | 10.6352 | 18 | 3.1925e-41 | 2026-09-07 | +0.00 | +0.0000 | +0.00 |
| `no_thrust` | 1.7228 | 26.81% | 3.84% | 9.9178 | 18 | 3.3531e-20 | 2026-09-07 | -1.07 | +0.1858 | -0.31 |
| `no_inertia` | 1.3378 | 27.54% | 4.67% | 10.2711 | 18 | 1.9055e-68 | 2026-09-07 | -0.34 | -0.1992 | +0.52 |
| `bull_full` | 1.3554 | 33.97% | 5.40% | 9.6535 | 18 | 1.3976e-53 | 2026-09-07 | +6.09 | -0.1816 | +1.25 |
| `no_circuit` | 1.5384 | 27.94% | 4.15% | 10.6474 | 18 | 3.9356e-41 | 2026-09-07 | +0.06 | +0.0014 | -0.00 |

- baseline 绝对水平：Sharpe **1.5370**、CAGR **27.88%**、MaxDD **4.15%**、Calmar **10.6352**、`n_valid_folds=18`、`fold_sharpe_std=2.1844`。
- **截断披露（不是 gloss）**：请求 `2022-01-04 → 2026-10-22`，标准模式同窗口 `overlap_bars = 1161`（引自 `06_ablate/baseline/.../comparison_report.json`）。走查实际只能到 `actual_last_fold_end = 2026-09-07`：`window_bars = step_bars = 63`（`backtester/run_backtest.py:273-274`，`int(round(0.25*252))`）⇒ 覆盖 18 × 63 = 1134 根 bar，**末尾 27 根 bar（2026-09-08 → 2026-10-22）未进入任何折**（走查不生成不完整折）。13 臂的 `n_folds` 与 `actual_last_fold_end` 完全一致，臂间横比不受影响；但**绝对水平不等于标准模式全窗口水平，不得与 `06_ablate` 的绝对收益并列**。
- `deflated_sharpe_ratio` 在本折法下**不可用于排序**：其实现（`common/metrics.py:198-215`）取 `n_trials = n_valid_folds = 18`、`sharpe_std = 折间 Sharpe 标准差 = 2.1844` 计算 E[max]，按 `common/metrics.py:190-196` 同式复算门槛 sr₀ = **4.0495**，远高于任何臂的 mean Sharpe；13 臂 DSR 最大值仅 **1.3767e-19** ⇒ **只能读作「没有任何一臂通过 deflation 显著性门槛」，不能用来给臂排序。**

## 2. 逐折 OOS 一致性（走查相对标准模式的增量信息）

对每一折（共 18 折）取「臂 − baseline」的同折指标差，再统计方向一致率。

| 臂 | 逐折 ΔSharpe 均值 | Sharpe 胜折 | 中位 ΔSharpe | 逐折 ΔCAGR 均值 | CAGR 胜折 | 逐折 ΔMaxDD(pp) | 回撤更浅折数 |
|---|---|---|---|---|---|---|---|
| `baseline` | +0.0000 | — | +0.0000 | +0.00pp | — | +0.000 | — |
| `no_voltarget` | +0.0927 | 12/18 | +0.1903 | +3.53pp | 12/18 | +0.225 | 5/18 |
| `no_orthogon` | +0.2452 | 12/18 | +0.3394 | +1.03pp | 12/18 | +0.227 | 4/18 |
| `no_structregime` | +0.0000 | 0/18 | +0.0000 | +0.00pp | 0/18 | +0.000 | 0/18 |
| `no_cyq` | -0.0355 | 8/18 | -0.1046 | -0.97pp | 8/18 | +0.624 | 5/18 |
| `no_atrstop` | +0.1245 | 11/18 | +0.0701 | +2.08pp | 11/18 | +0.089 | 10/18 |
| `no_lowbeta` | +0.0267 | 3/18 | +0.0000 | +0.22pp | 3/18 | -0.044 | 3/18 |
| `no_sectorthrot` | +0.0284 | 6/18 | +0.0000 | -0.20pp | 6/18 | -0.134 | 10/18 |
| `no_hysteresis` | +0.0000 | 0/18 | +0.0000 | +0.00pp | 0/18 | +0.000 | 0/18 |
| `no_thrust` | +0.1858 | 11/18 | +0.1365 | -1.07pp | 9/18 | -0.310 | 11/18 |
| `no_inertia` | -0.1992 | 10/18 | +0.1138 | -0.34pp | 9/18 | +0.521 | 7/18 |
| `bull_full` | -0.1816 | 7/18 | -0.1429 | +6.09pp | 8/18 | +1.248 | 1/18 |
| `no_circuit` | +0.0014 | 1/18 | +0.0000 | +0.06pp | 1/18 | -0.000 | 1/18 |

读数前提：`mean_sharpe_ratio` / `mean_cagr` / `mean_max_drawdown` 本身就是**折均值**，所以本表的「逐折 Δ 均值」与 §1 的 `ΔSharpe` / `ΔCAGR` / `ΔMaxDD` **数学上恒等**（已逐臂核对一致）；本表**唯一新增的信息是胜折数**，即方向在 18 折里是否稳定。

读法要点：
- `no_inertia` 逐折 Sharpe 胜 10/18、中位 +0.1138 为正，但逐折均值 -0.1992 为负 ⇒ **少数折的巨幅恶化把多数折的小幅改善吃掉**：惯性门的作用是**削尾**——关掉它之后回撤反而加深 ΔMaxDD +0.52pp，这才是它的价值所在，而非抬均值 Sharpe。
- `bull_full` 逐折 ΔCAGR 均值 +6.09pp 但只有 8/18 折胜、回撤更浅仅 1/18 ⇒ **收益增量集中在少数折**，属典型「单折大赢 + 全期变脆」。
- `no_thrust` 逐折 ΔSharpe +0.1858（11/18 胜）而逐折 ΔCAGR -1.07pp（9/18 胜）⇒ 关掉牛市推力加仓是**牺牲收益换波动**，不是纯增益。
- `no_orthogon` 逐折 ΔSharpe +0.2452（12/18 胜、6/18 负）为 13 臂中逐折 Sharpe 均值最高者，但标准模式为 -0.1935 ⇒ **正交化在本策略上只是单路径看起来有用**。

## 3. ≡ 矩阵：静默空操作检测

方法：把每臂 `walkforward_rebalances.csv` 的 `portfolio_equity` 按 **(fold, date)** 对齐到 baseline（该列在同一 `(fold, date)` 内恒为单值 —— 对 13 个文件共 6419 个 `(fold, date)` 组逐组核验，非常量组 **0 个**），逐点取 `max|diff|`；`< 1e-9` 判为**数值恒等**。注意对齐 bar 数逐臂不同（330~467），因为一臂若在某日完全无成交就不会产生该行；`only_arm` / `only_base` 两列正是为此披露。

| 臂 | 对齐 bar 数 | 仅该臂有的行 | 仅 baseline 有的行 | max abs equity diff (元) | 判为 ≡ |
|---|---|---|---|---|---|
| `baseline` | 467 | 0 | 0 | 0.0000 | **≡** |
| `no_voltarget` | 433 | 36 | 34 | 7787.9820 | 否 |
| `no_orthogon` | 356 | 80 | 111 | 6583.6164 | 否 |
| `no_structregime` | 467 | 0 | 0 | 0.0000 | **≡** |
| `no_cyq` | 349 | 132 | 118 | 6709.2375 | 否 |
| `no_atrstop` | 414 | 65 | 53 | 4458.3607 | 否 |
| `no_lowbeta` | 464 | 2 | 3 | 762.8878 | 否 |
| `no_sectorthrot` | 438 | 21 | 29 | 2355.3186 | 否 |
| `no_hysteresis` | 467 | 0 | 0 | 0.0000 | **≡** |
| `no_thrust` | 330 | 43 | 137 | 6643.5222 | 否 |
| `no_inertia` | 415 | 448 | 52 | 7354.1697 | 否 |
| `bull_full` | 385 | 139 | 82 | 9591.1824 | 否 |
| `no_circuit` | 467 | 1 | 0 | 219.9148 | 否 |

**本策略 ≡ 集合 = {`no_structregime`, `no_hysteresis`}**，两臂均为 `aligned = 467`、`only_arm = 0`、`only_base = 0`、`max|diff| = 0.0`。

### 3.1 是否与标准模式既有发现一致 —— 一致，并给出代码级归因

1. **`blend_enable_structural_regime`**：`pipeline/research_strategy/rs/config.py:909` 默认为 `True`；该 flag 在 `pipeline/research/blend/blend_base.py` 只有三处出现 —— `:648` 读取，`:468` 在 `elif weighting_mode in ("regime_adaptive", "regime_ic_ir_hybrid")`（`:465`）分支内使用，`:769` 仅决定是否预算 `fractal_d_series`（其产物 `median_d` 同样只在该分支被消费）。本策略 dump 钉死 `blend_weighting_mode="rolling_ic_ir"`，而 `if weighting_mode == "rolling_ic_ir"`（`:445`）分支的每一条出口都是 `return`，`elif`（`:465`）**永不执行** ⇒ flag 恒不被读取 ⇒ **必然 ≡**。因此该臂的恒等是**结构性**的，与数据、期限、评估模式全部无关。
2. **`blend_enable_regime_hysteresis`**：`pipeline/research_strategy/rs/config.py:794` 默认即 `False`，而本策略 dump **不含**该键 ⇒ 运行时本来就是 `False` ⇒ 「设为 False」= **写回原值** ⇒ **必然 ≡**。（对照 `chan_four_state_blend` 在 `strategies_config.json` 把该键钉为 `True`，故同一臂在 cfsb 上并非空操作 —— 这也是两个策略的 ≡ 集合必然不同的原因。）

### 3.2 标准模式恒等、走查不恒等：`no_circuit`

`no_circuit` 在 `06_ablate` 标准模式下 `max|diff| = 0.000000`（同一 1161 bar 全等），走查下对齐 467 bar 的 `max|diff| = 219.9148` 元、ΔSharpe +0.001447、ΔCAGR +0.0585pp、ΔMaxDD -0.00016pp，且是全表唯一 `only_arm = 1` 的臂。

解释：三个 `crb_dd_*` 阈值抬到 `10.0`（按 dump 默认 `0.1/0.15/0.2` 的小数口径 = 1000% 回撤）后**在整条路径上永不触发**。标准模式是一条连续权益曲线 ⇒ 永不触发 = 完全恒等。走查把时间切成 18 折、**每折权益在折首重置**，熔断状态机与其回撤分母随之重置，于是个别折的折内回撤路径出现极小差别。**量级上仍是「模块失效」，但严格讲 walkforward 已把它与 baseline 区分开** ⇒ 本文件只记「非 ≡ 但经济上可忽略」，不写 ≡。

## 4. 配置落地核对清单

观测值全部来自该臂自己的 `strategy_dump.json` 的 `.params`；目标值逐字复制自 `docs/backtest_research.md` §8 `run_arm` 的 `case` 分支（`pfx=crb`）。

| 臂 | key | 目标值 | dump 观测值 | dump 默认值 | 落地 |
|---|---|---|---|---|---|
| `no_voltarget` | `crb_enable_vol_targeting` | `False` | `False` | `True` | ✅ |
| `no_orthogon` | `blend_enable_orthogonalization` | `False` | `False` | `True` | ✅ |
| `no_structregime` | `blend_enable_structural_regime` | `False` | `False` | `True` | ✅ |
| `no_cyq` | `blend_enable_cyq_quality_gate` | `False` | `False` | `True` | ✅ |
| `no_atrstop` | `blend_enable_adaptive_stop_atr` | `False` | `False` | `True` | ✅ |
| `no_lowbeta` | `blend_enable_low_beta_inertia` | `False` | `False` | `True` | ✅ |
| `no_sectorthrot` | `blend_max_assets_per_sector` | `20` | `20` | `2` | ✅ |
| `no_hysteresis` | `blend_enable_regime_hysteresis` | `False` | `False` | `<<dump 中无此键>>` | ✅ |
| `no_thrust` | `crb_dynamic_cash_deployment` | `False` | `False` | `True` | ✅ |
| `no_inertia` | `crb_adaptive_inertia` | `False` | `False` | `True` | ✅ |
| `no_inertia` | `crb_min_weight_change` | `0.0` | `0.0` | `0.05` | ✅ |
| `no_inertia` | `crb_min_holding_bars` | `0` | `0` | `2` | ✅ |
| `no_inertia` | `crb_entry_threshold` | `0.0` | `0.0` | `0.03` | ✅ |
| `bull_full` | `crb_target_bull_exposure` | `1.0` | `1.0` | `0.8` | ✅ |
| `no_circuit` | `crb_dd_reduce_thresh` | `10.0` | `10.0` | `0.1` | ✅ |
| `no_circuit` | `crb_dd_defensive_thresh` | `10.0` | `10.0` | `0.15` | ✅ |
| `no_circuit` | `crb_dd_stop_thresh` | `10.0` | `10.0` | `0.2` | ✅ |

- **17/17 个目标 key 落地**，0 个缺失、0 个仍停留在 dump 默认值 ⇒ **13 臂全部 VALID**。
- `grep -c "ignoring unknown key(s)"` 逐臂结果：baseline=0、no_voltarget=0、no_orthogon=0、no_structregime=0、no_cyq=0、no_atrstop=0、no_lowbeta=0、no_sectorthrot=0、no_hysteresis=0、no_thrust=0、no_inertia=0、bull_full=0、no_circuit=0 ⇒ **全为 0**。
- 逐臂 `jq '{status,mode}' run_manifest.json`：baseline=completed/walkforward、no_voltarget=completed/walkforward、no_orthogon=completed/walkforward、no_structregime=completed/walkforward、no_cyq=completed/walkforward、no_atrstop=completed/walkforward、no_lowbeta=completed/walkforward、no_sectorthrot=completed/walkforward、no_hysteresis=completed/walkforward、no_thrust=completed/walkforward、no_inertia=completed/walkforward、bull_full=completed/walkforward、no_circuit=completed/walkforward ⇒ **13/13 completed + walkforward**；逐臂 `grep -c Traceback run.log` 全为 0（0、0、0、0、0、0、0、0、0、0、0、0、0）。
- 特别核对：`no_inertia` 的 `crb_min_weight_change → 0.0`（默认 0.05）**且** `crb_min_holding_bars → 0`（默认 2）均已落地；`no_sectorthrot → blend_max_assets_per_sector = 20`（默认 2）；`no_circuit → crb_dd_reduce_thresh / crb_dd_defensive_thresh / crb_dd_stop_thresh` 三项均 `10.0`（默认 0.1 / 0.15 / 0.2）。
- 补充披露：`06_ablate` 归档里的 `no_sectorthrot` 是 **repair 之前**跑的——其 `run.log` 含 4 条 `ignoring unknown key(s)`（写了不存在的 `crb_max_assets_per_sector` / `crb_adaptive_sector_throttle`）。本次按修复后口径只写 `blend_max_assets_per_sector`，**0 条 warning**；两版有效 override 等价，故该臂两种模式仍可比。

## 5. 三个口径声明（硬性）

1. **周期 = `06_ablate` 的同窗口**：`--start 2022-01-04 --end 2026-10-22`。标准模式同窗口 `overlap_bars = 1161` ⇒ 本文件里「走查 vs 标准」的任何差异**只可能来自评估模式**，不来自区间选择。
2. **折规则 = `03_r2b_oos` / `07_r3_oos_bear` 的同规则**：`--window-years 0.25 --step-years 0.25` ⇒ `window_bars = step_bars = 63`，本策略实际 `n_folds = 18`。折数口径可比。
3. **现金扫单与 `06_ablate` 一致、与 `03_r2b_oos` 不一致**：`--cash-sweep-rate 0.015`，而 `03_r2b_oos` 是 **0%**（`docs/backtest_research.md:398` 口径披露三）。⇒ 任何跨 `03_r2b_oos` 的绝对数字比较都混入「闲置现金 1.5% 年化」这一项，**本文件的数字不得与 `03_r2b_oos` 或 `blend_17_baseline` / 归档 15 折并列**。

其他 13 臂完全一致的固定项：`--data-provider fuyao`（本地 DuckDB，全离线，零网络）、`--min-shares 100`、`--china-trading`、`--baseline-symbol 000300.SH`、`--n-jobs 1`、`--strategy-file pipeline/research_strategy/results/strategy_dumps/chan_risk_managed_blend_strategy.json`（`strategy_version=1.1.0`、`base_version=1.6.0`）。

## 6. 标准模式 vs 走查模式（唯一变量 = 评估模式）

标准模式数字引自 `06_ablate/<arm>/chan_risk_managed_blend/comparison_report.json` 的 `strategy_sharpe_ratio` / `strategy_cagr`（只读引用，未改动该目录）。

| 臂 | 标准 ΔSharpe | 走查 ΔSharpe | Sharpe 符号翻转 | 标准 ΔCAGR(pp) | 走查 ΔCAGR(pp) |
|---|---|---|---|---|---|
| `baseline` | +0.0000 | +0.0000 | — | +0.00 | +0.00 |
| `no_voltarget` | +0.0154 | +0.0927 | — | +1.39 | +3.53 |
| `no_orthogon` | -0.1935 | +0.2452 | **是** | -3.73 | +1.03 |
| `no_structregime` | +0.0000 | +0.0000 | — | +0.00 | +0.00 |
| `no_cyq` | -0.0894 | -0.0355 | — | -0.67 | -0.97 |
| `no_atrstop` | +0.0276 | +0.1245 | — | +0.98 | +2.08 |
| `no_lowbeta` | +0.0028 | +0.0267 | — | +0.10 | +0.22 |
| `no_sectorthrot` | +0.0162 | +0.0284 | — | +0.11 | -0.20 |
| `no_hysteresis` | +0.0000 | +0.0000 | — | +0.00 | +0.00 |
| `no_thrust` | -0.1270 | +0.1858 | **是** | -7.18 | -1.07 |
| `no_inertia` | -0.1054 | -0.1992 | — | -3.70 | -0.34 |
| `bull_full` | -0.2501 | -0.1816 | — | -2.52 | +6.09 |
| `no_circuit` | +0.0000 | +0.0014 | — | +0.00 | +0.06 |

- 符号翻转臂：**{`no_orthogon`, `no_thrust`}** ⇒ 它们在标准模式下的「正贡献」是单一路径的产物，**不可作为保留该模块的 OOS 证据**。
- 两种模式**同向**（可互相印证）的臂：`no_voltarget`（+0.0154 → +0.0927）、`no_cyq`（-0.0894 → -0.0355）、`no_atrstop`（+0.0276 → +0.1245）、`no_lowbeta`（+0.0028 → +0.0267）、`no_sectorthrot`（+0.0162 → +0.0284）、`no_inertia`（-0.1054 → -0.1992）、`bull_full`（-0.2501 → -0.1816）。

## 7. 可引用 / 作废清单

### 7.1 可引用（限定：本策略 + 本 20 只票 universe + 2022-01-04→2026-10-22 + 1.5% 现金扫单）

- **惯性/换手门是主力风控之一**：同时关掉 `crb_adaptive_inertia` 并把 `min_weight_change→0`、`min_holding_bars→0`、`entry_threshold→0` 后，走查 Sharpe 1.5370 → 1.3378（-0.1992）、MaxDD 4.15% → 4.67%（+0.52pp）；标准模式同向（-0.1054）⇒ **双重确认，可引用**。
- **牛市目标敞口上限 0.8 是有效的风险预算**：放到 1.0 后走查 CAGR 27.88% → 33.97%（+6.09pp），但 Sharpe 1.5370 → 1.3554（-0.1816）、MaxDD 4.15% → 5.40%（+1.25pp）、回撤更浅的折仅 1/18 ⇒ **可引用为「提高敞口是用风险换收益」，不可引用为「提高敞口更好」**。
- **CYQ 筹码质量门是真风控**：关掉后 Sharpe -0.0355、MaxDD +0.62pp、CAGR -0.97pp，标准模式同向（-0.0894）⇒ 可引用为「保留」。
- **正交化的正贡献不成立（但也不足以支持关闭）**：走查关掉后 Sharpe +0.2452（12/18 折更好）、MaxDD +0.23pp、CAGR +1.03pp；标准模式却是 -0.1935。⇒ **可引用为「该模块的 OOS 价值未被证实」**；**不可引用为「应当关闭它」**（ΔSharpe 为正的同时回撤更深，且 6/18 折更差，量级不足以改动已归档的活体配置）。
- **推力加仓（thrust）是「降波动不增收益」**：关掉后 Sharpe +0.1858、MaxDD -0.31pp，但 CAGR -1.07pp ⇒ 保留它的理由只能是风险而非收益（标准模式 -0.1270 / -7.18pp，同向）。

### 7.2 作废 / 零信息（不得据此下任何结论）

- **`no_structregime` ≡ baseline**（对齐 467 bar、`only_arm = only_base = 0`、`max|diff| = 0.0`）：本策略上为**结构性空操作**（§3.1 代码级归因）⇒ **零信息**。**既不构成「该模块无效」的证据，也不构成「该模块有效」的证据，更不得用于任何横向排名。**
- **`no_hysteresis` ≡ baseline**（对齐 467 bar、`only_arm = only_base = 0`、`max|diff| = 0.0`）：本策略上为**结构性空操作**（§3.1 代码级归因）⇒ **零信息**。**既不构成「该模块无效」的证据，也不构成「该模块有效」的证据，更不得用于任何横向排名。**
- **`no_circuit` 不可写成 ≡，也不可写成「熔断有效」**：走查已出现 219.91 元权益差，但 ΔSharpe 仅 +0.001447、ΔMaxDD -0.00016pp ⇒ 只能写「本窗口内熔断在标准模式下从未触发；走查下仅在折界重置处产生可忽略差异」。
- **`no_lowbeta` 处于噪声边缘**：`only_arm = 2`、`only_base = 3`，整条路径只有 5 个换手日不同，ΔSharpe +0.0267、ΔMaxDD -0.04pp ⇒ 在 20 只票 universe 上该模块几乎不咬合，**任何正负号都不该被当成结论**。
- **`no_sectorthrot` 的经济解释受限**：板块上限 2→20 只改变 438 个对齐 bar 中的少数路径（`only_arm = 21`、`only_base = 29`），ΔSharpe +0.0284（6/18 折更好）、ΔCAGR -0.20pp ⇒ 只能引用为「板块上限在本 20 票 universe 上约束力弱」，**不可引用为「可以取消板块上限」**。
- **`no_voltarget`**（ΔSharpe +0.0927、ΔMaxDD +0.22pp）与 **`no_atrstop`**（ΔSharpe +0.1245、ΔMaxDD +0.09pp）虽两模式同向为正，但 Sharpe 增量仅 0.0927~0.1245，而两臂的回撤都**更深** ⇒ **只可引用为「本窗口该模块未体现出风险调整后增益」，不可引用为「波动率目标 / ATR 止损应当移除」**。
- **禁止跨组比较**：本文件所有 Δ 只在 13 臂内部成立。

## 8. 溯源文件

| 路径 | 内容 |
|---|---|
| `backtests_research/20261025_125300/08_ablate_wf/<arm>/chan_risk_managed_blend/` | 13 臂各自的 `walkforward_summary.json` / `walkforward_report.csv` / `walkforward_rebalances.csv` / `strategy_dump.json` / `run_manifest.json` / `run.log` |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/crb_analysis.txt` | §1 / §4 的原始导出（未取整） |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/crb_table.json` | 逐臂全部指标 + 对齐诊断 + 配置落地布尔值（机器可读） |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/crb_foldstats.txt` | §2 逐折统计的原始导出 |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/crb_std_vs_wf.txt` | §6 标准 vs 走查对照的原始导出 |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/run_crb.sh` | 13 臂实际执行脚本（`extra` 逐字复制自 `docs/backtest_research.md` §8，`pfx=crb`） |
| `backtests_research/20261025_125300/08_ablate_wf/chan_risk_managed_blend/_provenance/gen_metrics.py` | 生成本文件表格的脚本（只读上述产物） |


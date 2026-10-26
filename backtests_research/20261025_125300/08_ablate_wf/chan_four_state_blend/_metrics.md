# `chan_four_state_blend`（`cfsb`）— 消融的 walkforward 复核（08_ablate_wf）

- **组**：`backtests_research/20261025_125300/08_ablate_wf/`
- **策略**：`chan_four_state_blend`（前缀 `cfsb`，dump 为 `pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json`）
- **臂**：13（`baseline` + 12 个单模块拆除臂），臂定义与 `extra` JSON **逐字**取自 `docs/backtest_research.md` §8 `run_arm`，仅把 `${pfx}` 代换为 `cfsb`
- **数据**：`--data-provider fuyao`（本地 `data/cache.duckdb` 只读，未联网）；股池 `universe_20_stocks.txt`（20 只 A 股）；`--min-shares 100 --china-trading --baseline-symbol 000300.SH`
- **驱动脚本**：`/tmp/opencode/ablwf/run_cfsb.sh`；臂日志 `/tmp/opencode/ablwf/<arm>.log`（已 `cp` 为各目录 `run.log`）；结构化抽取 `/tmp/opencode/ablwf/analyze.py` → `analysis.json`
- **本文所有数字均直接抄自各目录落盘产物**（`walkforward_summary.json` / `walkforward_rebalances.csv` / `strategy_dump.json` / `run_manifest.json` / `run.log`），无估算值。

---

## 1. 13 臂 walkforward 指标

口径：18 折算术均值；`mean_max_drawdown` 是**折内**最大回撤的 18 折均值（不是全期最大回撤）；Δ 一律为「该臂 − `baseline`」，CAGR / MaxDD 以 pp 计。

| 臂 | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | actual_last_fold_end | ΔCAGR (pp) | ΔSharpe | ΔMaxDD (pp) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |
| `baseline` | 1.433 | 30.15% | 4.69% | 10.914 | 18 | 1.987e-54 | 2026-09-07 | — | — | — |
| `no_voltarget` | 1.385 | 30.57% | 4.93% | 10.570 | 18 | 4.952e-49 | 2026-09-07 | +0.42 | −0.047 | +0.24 |
| `no_orthogon` | 1.537 | 29.94% | 4.50% | 10.534 | 18 | 4.647e-47 | 2026-09-07 | −0.22 | **+0.104** | −0.19 |
| `no_structregime` | 1.396 | 30.41% | 4.63% | 10.876 | 18 | 3.480e-53 | 2026-09-07 | +0.26 | −0.037 | −0.06 |
| `no_cyq` | 1.459 | 28.76% | 4.84% | 10.155 | 18 | 1.289e-51 | 2026-09-07 | −1.39 | +0.026 | +0.16 |
| `no_atrstop` | 1.425 | 30.38% | 4.61% | 10.621 | 18 | 3.936e-52 | 2026-09-07 | +0.23 | −0.008 | −0.08 |
| `no_lowbeta` | 1.364 | 29.62% | 4.69% | 10.702 | 18 | 2.373e-58 | 2026-09-07 | −0.53 | −0.069 | +0.00 |
| `no_sectorthrot` | 1.362 | 29.71% | 4.82% | 10.297 | 18 | 4.071e-58 | 2026-09-07 | −0.45 | −0.071 | +0.13 |
| `no_hysteresis` | 1.432 | 30.04% | 4.60% | 10.828 | 18 | 1.260e-50 | 2026-09-07 | −0.11 | −0.001 | −0.09 |
| `no_thrust` | 1.357 | 24.21% | 3.99% | 10.587 | 18 | 4.323e-61 | 2026-09-07 | **−5.94** | −0.076 | −0.70 |
| `no_inertia` | 1.249 | 28.20% | 4.86% | 10.182 | 18 | 5.242e-79 | 2026-09-07 | −1.95 | **−0.184** | +0.17 |
| `bull_full` | 1.127 | 28.21% | 5.24% | 9.189 | 18 | 1.307e-69 | 2026-09-07 | −1.94 | **−0.306** | **+0.56** |
| `no_circuit` | 1.432 | 30.14% | 4.69% | 10.910 | 18 | 1.553e-54 | 2026-09-07 | −0.01 | −0.001 | +0.01 |

**未截断折数、但有尾部 bar 不入折**：窗口 `2022-01-04 → 2026-10-22` 在本地缓存中共 **1,161 根** bar（只读实测 `ohlcv_bars` 表 `000300.SH` 区间计数 = 1161）；`0.25y/0.25y` ⇒ `63 bar/折 × 18 折 = 1,134 bar` 进入评分，**尾部 27 根（`2026-09-08 → 2026-10-22`）不进入任何折**（缓存实测该区间恰 27 个交易日）。13 臂全部 `n_folds = 18`、`n_valid_folds = 18`、`actual_first_fold_start = 2022-01-04`、`actual_last_fold_end = 2026-09-07`，与 `18 × 63 = 1134` 的预期完全一致，**没有折被丢弃、没有折内 bar 缺失**。

**DSR 一律读数为 0**：`deflated_sharpe_ratio` = 「扣掉 best-of-n_trials 选择偏差后真实 Sharpe > 0 的概率」，此处 `n_trials = n_valid_folds = 18`、`sharpe_std` = 折间 Sharpe 标准差（`baseline` 为 2.2836）。13 臂取值全部落在 `1.3e-47 ~ 5.2e-79`，**数值上都等于 0**：折间 Sharpe 离散度（2.15–2.39）远大于折均值 Sharpe（1.13–1.54），在 best-of-18 的 deflation 下没有任何一臂能过 DSR 关。这不是某一臂的问题，是本组的**噪声下限**——**禁止**用 DSR 给这 13 臂排序，也禁止把 `no_circuit` 的 1.553e-54 与 `bull_full` 的 1.307e-69 说成"前者显著优于后者"。

**折级符号稳定性**（可引用性的判据，逐折与 `baseline` 配对）：

| 臂 | Sharpe 优于 baseline 的折数 | 折级 ΔSharpe 中位数 | ΔSharpe 区间 | CAGR 优于 baseline 的折数 | 折内 MaxDD 更浅的折数 | ΔMaxDD 中位数 |
| :--- | :---: | ---: | :---: | :---: | :---: | ---: |
| `no_voltarget` | 7/18 | −0.024 | −1.157 … +1.124 | 7/18 | 4/18 | +0.17 pp |
| `no_orthogon` | 8/18 | −0.040 | −1.089 … +3.295 | 10/18 | 7/18 | +0.16 pp |
| `no_structregime` | 9/18 | −0.024 | −1.001 … +0.881 | 11/18 | 10/18 | −0.02 pp |
| `no_cyq` | 10/18 | +0.096 | −1.448 … +1.505 | 10/18 | 9/18 | +0.12 pp |
| `no_atrstop` | 5/18 | −0.052 | −1.097 … +1.170 | 7/18 | 8/18 | +0.00 pp |
| `no_lowbeta` | **0/18** | 0.000 | −1.241 … 0.000 | **0/18** | 0/18 | +0.00 pp |
| `no_sectorthrot` | 3/18 | −0.005 | −0.774 … +0.446 | 3/18 | 3/18 | +0.00 pp |
| `no_hysteresis` | 9/18 | +0.002 | −0.476 … +0.278 | 9/18 | 11/18 | −0.00 pp |
| `no_thrust` | 7/18 | −0.110 | −0.893 … +0.608 | 6/18 | **15/18** | **−0.37 pp** |
| `no_inertia` | 9/18 | +0.056 | −1.602 … +0.727 | 8/18 | 9/18 | +0.22 pp |
| `bull_full` | 6/18 | −0.105 | −2.761 … +0.493 | 8/18 | 4/18 | **+0.40 pp** |
| `no_circuit` | 0/18 | 0.000 | −0.018 … 0.000 | 0/18 | 0/18 | +0.00 pp |

读法：`no_lowbeta` / `no_circuit` 是**逐折方向一致**（0/18 折更好，从不反号）；`no_orthogon` / `no_cyq` / `no_hysteresis` / `no_inertia` / `bull_full` 的折级符号接近抛硬币（8–10 / 18），其折均值差异**不具备方向可复现性**。`no_thrust`（15/18 折回撤更浅、中位数 −0.37 pp）与 `bull_full`（14/18 折回撤更深、中位数 +0.40 pp）是仅有的两个**折内回撤符号逐折一致**的臂。

> 单折量级参照（`baseline` 逐折，抄自 `walkforward_summary.json`）：折 1 `2022-01-04→2022-04-11` Sharpe −1.417 / CAGR −19.21%，折 2 `2022-04-12→2022-07-13` −1.739 / −22.70%，折 7 `2023-07-27→2023-10-31` −2.194 / −20.80%，折 13 `2025-02-21→2025-05-26` −1.377 / −18.09%；另一端折 5 `2023-01-17→2023-04-21` +5.033 / +182.65%，折 9 `2024-01-30→2024-05-10` +5.320 / +181.29%。**折级 CAGR 的极差（−22.70% … +182.65%）比任何两臂之间的差异都大一个数量级**——这正是「折均值差值不得当显著性」的直接证据。

---

## 2. ≡ 静默无操作检测矩阵

判据：以 `baseline` 为参照，逐折对齐 `walkforward_rebalances.csv` 的 `(fold, date)` 键取 `portfolio_equity`（同一调仓日多行取值相同，取首行），若全部对齐点上 `max|Δ| < 1e-9` 判为 `≡`。

| 臂 | 对齐 (fold, date) 点数 | max 绝对 Δportfolio_equity | 相对最大差 | 有差异的调仓日 | 判定 |
| :--- | ---: | ---: | ---: | ---: | :---: |
| `no_voltarget` | 395 | 4,342.69 | 4.107e-02 | 340/395 | 非 ≡ |
| `no_orthogon` | 350 | 7,592.77 | 7.180e-02 | 332/350 | 非 ≡ |
| `no_structregime` | 377 | 5,136.80 | 5.127e-02 | 355/377 | 非 ≡ |
| `no_cyq` | 322 | 4,994.42 | 5.036e-02 | 304/322 | 非 ≡ |
| `no_atrstop` | 391 | 5,911.02 | 5.900e-02 | 314/391 | 非 ≡ |
| `no_lowbeta` | 440 | 2,123.30 | 2.032e-02 | 20/440 | 非 ≡ |
| `no_sectorthrot` | 423 | 4,987.15 | 4.978e-02 | 246/423 | 非 ≡ |
| `no_hysteresis` | 430 | 1,681.30 | 1.532e-02 | 339/430 | 非 ≡ |
| `no_thrust` | 317 | 7,631.65 | 5.737e-02 | 299/317 | 非 ≡ |
| `no_inertia` | 397 | 5,452.42 | 5.017e-02 | 379/397 | 非 ≡ |
| `bull_full` | 369 | 5,897.53 | 5.476e-02 | 351/369 | 非 ≡ |
| `no_circuit` | 442 | 138.84 | 1.447e-03 | 20/442 | 非 ≡（近空转，见下） |

- **本策略的 `≡` 集合为空**：`chan_four_state_blend` 的 12 个消融臂全部产生了可观测的净值分叉，**没有一臂退化为数值无操作**。（对齐点数 < 442 是因为各臂调仓日集合本身不同：例如 `no_inertia` 关掉惯性后调仓日增加到 839 个、`no_thrust` 减少到 377 个，交集自然变窄；`no_inertia` 的 839 个调仓日中只有 397 个与 `baseline` 的 442 个重合，这本身就是"惯性门确实在改调仓节奏"的直接证据。）
- **`no_circuit` 是最接近空转但未达 `≡` 的臂**：调仓日集合与 `baseline` **完全一致**（442 = 442，缺失 0、多出 0），仅 20 个调仓日出现差异，最大相对差 0.1447%（绝对 138.84 元，`baseline` 净值区间 91,516.67 – 133,755.01）。首个分叉日 = 折 4 `2022-10-26`（−53.95 元），最大差出现在折 4 `2022-12-22`（−138.84 元），末个分叉日 = 折 4 `2023-01-09`；**全部 20 个分叉点都落在折 4（`2022-10-19 → 2023-01-16`）这一个折内**。⇒ 18 折中只有 1 折的折内回撤路径真正触到了熔断三档阈值，其余 17 折熔断从未被触发。
- **模式对比（对 `≡` 结论的意义）**：同区间 **standard** 模式下（`06_ablate`，1,161 根 bar 全对齐）`no_circuit` 与 `baseline` 逐点 `max|Δ| = 0`、是真 `≡`；改成 walkforward 后不再逐点相同。⇒ **折内重置改变了回撤路径本身**，因此"某模块是否真的被触发"这件事在 standard 与 walkforward 两种模式下可以给出不同答案——这正是本次复核必须做 walkforward 版的直接理由（`06_ablate` 的 `no_circuit ≡ baseline` 只能读作"连续路径上熔断从未触发"，不能读作"熔断无效"）。

---

## 3. 配置落地检查清单（逐臂实测）

校验源：各目录 `strategy_dump.json` 的 `.params`（最终合并生效值）与 `run_manifest.json` 的 `.cli_overrides`；并辅以 `grep -c "ignoring unknown key(s)" run.log`、`grep -c Traceback run.log`、`jq '{status,mode}'`。

| 臂 | `strategy_dump.json` 中**实测到**的键与值 | 落地 | unknown-key 警告 | Traceback | manifest |
| :--- | :--- | :---: | ---: | ---: | :---: |
| `baseline` | （无覆盖，全部为 dump 默认：`cfsb_enable_vol_targeting=true`、`cfsb_dd_*`=0.1/0.15/0.2、`cfsb_min_weight_change=0.05`、`cfsb_min_holding_bars=2`、`cfsb_target_bull_exposure=0.8`、`blend_max_assets_per_sector=2` …） | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_voltarget` | `cfsb_enable_vol_targeting = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_orthogon` | `blend_enable_orthogonalization = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_structregime` | `blend_enable_structural_regime = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_cyq` | `blend_enable_cyq_quality_gate = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_atrstop` | `blend_enable_adaptive_stop_atr = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_lowbeta` | `blend_enable_low_beta_inertia = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_sectorthrot` | `blend_max_assets_per_sector = 20`（`cfsb` 无前缀孪生键，`cfsb_adaptive_sector_throttle` **本就不存在**，故未写） | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_hysteresis` | `blend_enable_regime_hysteresis = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_thrust` | `cfsb_dynamic_cash_deployment = false` | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_inertia` | `cfsb_adaptive_inertia=false`、`cfsb_min_weight_change=0.0`、`cfsb_min_holding_bars=0`、`cfsb_entry_threshold=0.0`（**四键齐全**，`min_holding_bars` 已确认从 2 → 0） | ✓ | 0 | 0 | `completed` / `walkforward` |
| `bull_full` | `cfsb_target_bull_exposure = 1.0`（dump 默认 0.8） | ✓ | 0 | 0 | `completed` / `walkforward` |
| `no_circuit` | `cfsb_dd_reduce_thresh=10.0`、`cfsb_dd_defensive_thresh=10.0`、`cfsb_dd_stop_thresh=10.0`（**三档全部抬到不可达哨兵**，dump 默认 0.1/0.15/0.2） | ✓ | 0 | 0 | `completed` / `walkforward` |

- **13/13 臂全部 `landing_ok = true`，无 INVALID 臂**；13 个 `run.log` 的 `ignoring unknown key(s)` 计数全为 **0**（`no_sectorthrot` 按 §8 修复后的写法只发全局键，未产生任何被静默丢弃的键）；13 个 `run.log` 的 `Traceback` 计数全为 **0**；13 个 `run_manifest.json` 全部 `status=completed`、`mode=walkforward`、`group=08_ablate_wf/<arm>`。
- `run_manifest.json` 的 `cli_overrides` 与 `strategy_dump.json` 的 `cli_strategy_params_override` 与上表逐键一致（例：`no_inertia` 的 `cli_overrides = {"cfsb_adaptive_inertia": false, "cfsb_min_weight_change": 0.0, "cfsb_min_holding_bars": 0, "cfsb_entry_threshold": 0.0}`）。
- **可达性矩阵的经验印证**：本组 `baseline` 落盘参数中 `blend_weighting_mode = "regime_ic_ir_hybrid"`（AGENTS.md 记录的**有意例外**，不得"规范化"回 `rolling_ic_ir`），而 `no_structregime` 臂在 377 个对齐调仓日中有 355 个产生净值差异、折均值 ΔSharpe −0.037 ⇒ **`docs/backtest_research.md` §8 可达性矩阵中 `cfsb` 的 `no_structregime` = ✓ 完全可达，在本组被独立证实**（该臂若像 `crb` / `cdhb` 那样走 `rolling_ic_ir` 提前 return，应表现为 ≡）。同理 `no_sectorthrot` 的 `blend_max_assets_per_sector = 20` 造成 246/423 个调仓日分叉，说明"只改全局行业上限"这一条在 `cfsb` 上确实生效（不是空操作）。

---

## 4. 三个口径披露（写进任何引用）

1. **窗口 `2022-01-04 → 2026-10-22` 与 `06_ablate` 的 standard 窗口完全一致（1,161 根 bar）** ⇒ 本文与 `06_ablate` 的任何差值**纯粹来自评估模式（walkforward 折内重置 vs 连续净值）**，不含区间差异。反过来：**本文数字不得与 `01_r1_longwin`（2022-01-04→2024-12-31）、`03_r2b_oos`（2025-01-02→2026-10-22）、`07_r3_oos_bear`（2021-06-01→2022-12-30）的折均值并列或合并**——区间不同即口径污染（同 `docs/backtest_research.md` §11 解读纪律第 2 条）。
2. **`--window-years 0.25 --step-years 0.25` 与 `03_r2b_oos` / `07_r3_oos_bear` 的折规则相同**（63 bar / 折，18 折；`03` / `07` 因区间更短分别为 6 折）⇒ **折的粒度与折内重置方式可比**，这是「本文的分叉日 / 折内回撤」与 `03` / `07` 的同类量能对齐讨论的唯一前提。
3. **`--cash-sweep-rate 0.015` 与 `06_ablate` 相同，而 `03_r2b_oos` 用的是 0% 现金收益** ⇒ 任何**跨本文与 `03_r2b_oos` 的绝对数字比较都混入了「闲置现金 1.5% 年化计息」这一项**（`07_r3_oos_bear` 同为 1.5%，与本组同侧）。方向上：本策略在各折普遍留现金（`--cash-sweep-rate` 抬高净值），故与 0% 口径的组横向相减时，**本文的数只会被抬高、不会被压低**。跨 `03` 的结论必须显式写出这句稀释说明。

> 附加披露（不是上述三条之一，但同样影响读数）：`deflated_sharpe_ratio` 全部塌缩到 ≈0（见 §1）；`mean_max_drawdown` 是**折内** MaxDD 的 18 折均值，**不是**全期（1,161 根）最大回撤——后者本组根本没有落盘，因为 walkforward 每折重置净值，全期回撤在此模式下不可观测。

---

## 5. 哪些结论可引用、哪些臂作废

### 5.1 作废 / 不得单独解释

| 项 | 状态 | 理由 |
| :--- | :--- | :--- |
| 任何 INVALID 臂 | **无** | 13/13 臂参数落地成功（§3） |
| 任何 `≡` 臂 | **无** | ≡ 集合为空（§2） |
| 任何 non-completed manifest | **无** | 13/13 `status=completed`、`mode=walkforward` |
| 用 DSR 给 13 臂排序 | **禁止** | 13 臂 DSR 全部 ≈0（1.3e-47 ~ 5.2e-79），差异无意义（§1） |
| 把 `no_circuit` 的「≈ 无差异」读成「熔断无效」 | **禁止** | 18 折中仅 1 折（折 4）触发过三档阈值，其余 17 折从未触发 ⇒ 该臂测的是「熔断缺席时行为」，样本上熔断几乎没上场（§2） |
| 把 `no_hysteresis`（ΔSharpe −0.001）读成「迟滞无效」 | **禁止** | 折级 9/18 且中位数 +0.002 ⇒ 在噪声内，只能写"不可判定" |
| 把 `no_sectorthrot` 与其他策略的同名臂横向对比 | **禁止** | `cfsb` 该臂只放开全局行业上限（2→20），没有节流开关可关，与 `cdhb` 不是同一实验（`docs/backtest_research.md` §8 可达性矩阵已披露） |

### 5.2 可引用（附限定条件）

- **`no_thrust` 是本策略唯一「折均值量级显著」的拆除损失**：ΔCAGR **−5.94 pp**、折级 ΔCAGR 中位数 −3.44 pp、ΔMaxDD −0.70 pp（折内回撤在 **15/18 折变浅**、ΔMaxDD 中位数 −0.37 pp，是 13 臂里唯一方向稳定为负的一侧）、ΔSharpe −0.076。可引用为：**动态现金部署（`cfsb_dynamic_cash_deployment`）是 `chan_four_state_blend` 在 18 折 walkforward 上贡献最大的单一模块，且其贡献主要是收益而非回撤保护**（拆掉后回撤更浅、收益更差；单折跌幅集中在折 9 `−54.05 pp`、折 5 `−22.12 pp`、折 10 `−12.38 pp`）。限定：Sharpe 只在 7/18 折变好，ΔSharpe 幅度仍在噪声带内 ⇒ 只可引用 CAGR 量级与"回撤变浅"方向，不可引用 Sharpe 排序。
- **`bull_full` 是本策略唯一「多指标同时变差」的拆除臂**：ΔSharpe **−0.306**（13 臂最差）、ΔCAGR −1.94 pp、ΔMaxDD **+0.56 pp**（13 臂最深，折内 MaxDD 4.69% → 5.24%；折内回撤在 **14/18 折变深**、中位数 +0.40 pp）、mean_calmar 10.914 → 9.189。可引用为：**把牛市目标敞口从 0.8 顶到 1.0 全仓，在 18 折上净负、且系统性放大折内回撤**（拆掉敞口上限 = 反证该上限有效）。限定：折级 8/18 CAGR 变好、ΔSharpe 区间 −2.761 … +0.493，"变差"集中在折 14 `2025-05-27→2025-08-22`（ΔSharpe −2.761、ΔCAGR −28.67 pp）与折 17 `2026-03-09→2026-06-09`（ΔSharpe −1.142、ΔCAGR −7.41 pp）。
- **`no_inertia` 是第二大拆除损失**：ΔSharpe **−0.184**、ΔCAGR −1.95 pp、ΔMaxDD +0.17 pp；且调仓日从 442 增至 839（≈1.9×）⇒ 可引用为：**惯性门在降换手的同时贡献了折均值 Sharpe（拆掉后收益下降且换手翻倍）**。限定：折级 9/18，符号不稳定。
- **`no_lowbeta` 方向最干净但量级最小**：**0/18 折** Sharpe 优于 baseline、0/18 折 CAGR 优于 baseline、折级 ΔSharpe 区间 −1.241 … 0.000（从不反号），而折均值仅 −0.069。可引用为：**低 beta 惯性豁免在本策略上方向一致地有益，但量级落在噪声带内**——只可作"方向一致"陈述，不可作"显著"陈述。
- **`no_orthogon` 是唯一「拆掉后折均值 Sharpe 变好」且方向尚可辨的臂**：ΔSharpe **+0.104**、ΔCAGR −0.22 pp、ΔMaxDD −0.19 pp。限定：折级 8/18、ΔSharpe 区间 −1.089 … +3.295（`+3.295` 来自折 15 `2025-08-25→2025-11-27`，该折 ΔCAGR +39.00 pp；反侧折 11 `2024-08-09→2024-11-14` −1.089、折 16 `2025-11-28→2026-03-06` −0.950）⇒ 只能写"**在本窗口该 universe 上，Löwdin 正交化对折均值 Sharpe 没有正贡献（可能为负），但证据不足以断言**"，不得写成"正交化有害"。
- **`no_circuit` 可引用为「在 18 折上熔断几乎不上场」**：调仓日集合与 baseline 完全一致（442 = 442），仅折 4（`2022-10-19 → 2023-01-16`）的 20 个调仓日有差异（最大相对差 0.1447%），折均值 ΔSharpe −0.001 / ΔCAGR −0.01 pp / ΔMaxDD +0.01 pp。这是本组最有价值的**负面/边界**结论之一：**该策略的熔断模块在 `2022-01-04 → 2026-09-07` 的 18 个季度折内，只有 1 个折真正触发过**——因此 `06_ablate` 与本文关于熔断的任何"有效/无效"判断都缺样本，必须回到 `docs/backtest_research.md` §11 解读纪律第 1、5 条（走离线合成压力带）。同一模式差异还说明：**standard 模式下该臂与 baseline 逐点全等（`max|Δ| = 0`），walkforward 下不全等**——折内重置改变了回撤路径，这是"同一模块在两种评估模式下可达性不同"的实证。
- **判定为「不可判定」（不得引用排序结论）的 6 个臂**：`no_voltarget`（ΔSharpe −0.047、7/18）、`no_structregime`（−0.037、9/18）、`no_atrstop`（−0.008、5/18）、`no_cyq`（+0.026、10/18）、`no_sectorthrot`（−0.071、3/18）、`no_hysteresis`（−0.001、9/18）。它们的 |ΔSharpe| ≤ 0.071 且折级符号在 3/18 – 10/18 之间摆动，**全部落在 `docs/backtest_research.md` §11 解读纪律第 3 条的噪声下限内**。唯一还能写的一句是 `no_sectorthrot` 的「行业上限 2 → 20 后折级 3/18 更好、中位 −0.005」——即"放宽行业上限没有带来折均值收益"，方向偏负但不显著。
- **整体层面**：13 臂折均值 Sharpe 极差 1.127 … 1.537（`bull_full` → `no_orthogon`），而**折级 CAGR 极差 −22.70% … +182.65%**、`fold_sharpe_std` 2.15–2.39 ⇒ 本组可用于判断**方向与量级**（如 `no_thrust` 的 −5.94 pp、`bull_full` 的 +0.56 pp 回撤放大），**不可用于判断 0.05–0.10 量级的 Sharpe 差异谁更优**（同 `docs/backtest_research.md` §11 解读纪律第 3 条：差异小于噪声下限即判"不可判定"）。

---

## 6. 产物与复现

- 每个臂目录：`backtests_research/20261025_125300/08_ablate_wf/<arm>/chan_four_state_blend/`，含 `strategy_dump.json`、`run_manifest.json`、`run.log`、`walkforward_summary.json`、`walkforward_report.csv`、`walkforward_weights.csv`（1,134 行 = 18 × 63 日度权重）、`walkforward_rebalances.csv`、`comparison_report.json`。
- 复现命令：`docs/backtest_research.md` §6.7（13 臂循环；`--mode walkforward --window-years 0.25 --step-years 0.25`，`--results-dir "$BT_ROOT/08_ablate_wf/$arm/$s"`）。
- 校验复跑：`python3 /tmp/opencode/ablwf/analyze.py`（只读上述产物，重算本表与 ≡ 矩阵）。

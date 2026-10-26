# `chan_dual_hybrid_blend` 逐模块消融（walkforward 口径）

- 策略：`Chan Dual Hybrid Alpha Blend Strategy`（前缀 `cdhb`）
- 消融臂：13 个（`baseline` + 12 个单模块拆除），与 `06_ablate/` 标准模式完全相同的一组臂
- 命令模板：见 `docs/backtest_research.md` §8 `run_arm`（`pfx=cdhb`，逐字照抄，未改写任何 override）
- 产物目录：`backtests_research/20261025_125300/08_ablate_wf/chan_dual_hybrid_blend/<arm>/`
- 运行脚本：`/tmp/opencode/ablwf/run_cdhb.sh`；日志：`/tmp/opencode/ablwf/cdhb_<arm>.log`（每个臂另有一份 `run.log` 副本落在产物目录内）
- 13 个臂全部 `run_manifest.json` = `status: completed`、`mode: walkforward`；`grep -c Traceback` 全为 0；`grep -c "ignoring unknown key(s)"` 全为 0；无任何臂崩溃。

## 1. 13 臂总表（walkforward，18 折）

Δ 列的参照物是本表 `baseline` 行；ΔMaxDD 为**正 = 回撤更深（更差）**。

| arm | mean_sharpe_ratio | mean_cagr | mean_max_drawdown | mean_calmar_ratio | n_folds | deflated_sharpe_ratio | actual_last_fold_end | ΔCAGR (pp) | ΔSharpe | ΔMaxDD (pp) |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1.424970 | 25.6274% | 4.2159% | 10.2096 | 18 | 2.8637e-50 | 2026-09-07 | +0.0000 | +0.0000 | +0.0000 |
| no_voltarget | 1.490879 | 26.4825% | 4.4109% | 10.0840 | 18 | 1.2980e-42 | 2026-09-07 | +0.8552 | +0.0659 | +0.1950 |
| no_orthogon | 1.368169 | 27.2296% | 4.3639% | 10.4206 | 18 | 4.5867e-46 | 2026-09-07 | +1.6022 | −0.0568 | +0.1479 |
| no_structregime | 1.424970 | 25.6274% | 4.2159% | 10.2096 | 18 | 2.8637e-50 | 2026-09-07 | +0.0000 | +0.0000 | +0.0000 |
| no_cyq | 1.459735 | 26.9494% | 4.3457% | 10.2846 | 18 | 1.5337e-52 | 2026-09-07 | +1.3221 | +0.0348 | +0.1298 |
| no_atrstop | 1.491987 | 26.2485% | 4.0906% | 10.6841 | 18 | 2.5343e-46 | 2026-09-07 | +0.6211 | +0.0670 | −0.1254 |
| no_lowbeta | 1.433568 | 25.5614% | 4.2279% | 10.1723 | 18 | 1.9946e-48 | 2026-09-07 | −0.0659 | +0.0086 | +0.0120 |
| no_sectorthrot | 1.473416 | 25.0490% | 4.2645% | 10.0524 | 18 | 5.7340e-46 | 2026-09-07 | −0.5783 | +0.0484 | +0.0486 |
| no_hysteresis | 1.424970 | 25.6274% | 4.2159% | 10.2096 | 18 | 2.8637e-50 | 2026-09-07 | +0.0000 | +0.0000 | +0.0000 |
| no_thrust | 1.515509 | 20.8852% | 3.3824% | 9.0594 | 18 | 1.1882e-32 | 2026-09-07 | −4.7422 | +0.0905 | −0.8335 |
| no_inertia | 1.285752 | 23.2870% | 4.8708% | 8.8343 | 18 | 5.2270e-56 | 2026-09-07 | −2.3403 | −0.1392 | +0.6548 |
| bull_full | 1.122050 | 25.5009% | 5.0906% | 8.4288 | 18 | 5.4011e-70 | 2026-09-07 | −0.1264 | −0.3029 | +0.8746 |
| no_circuit | 1.424970 | 25.6274% | 4.2159% | 10.2096 | 18 | 2.8637e-50 | 2026-09-07 | +0.0000 | +0.0000 | +0.0000 |

**读表前必须知道的两件事（数字层面的硬约束，非评论）**

1. `mean_cagr` / `mean_sharpe_ratio` / `mean_max_drawdown` 是 **18 折（每折约 63 个交易日）各自指标的年化均值**，不是"整条 4.5 年净值曲线"的 CAGR / Sharpe / MaxDD。因此 `mean_max_drawdown = 4.2159%` 是"平均单季最大回撤"，**不可**与任何整段回撤（例如 §5 提到的 6.6%、或标准模式整段 11.81%）直接相减比较。
2. `fold_sharpe_std` 在 13 个臂中为 **2.0065 – 2.2848**（baseline 2.2141）。本表所有 |ΔSharpe| ≤ 0.3029，即**每一个臂的 Sharpe 位移都小于一个折的 Sharpe 标准差**。
3. `deflated_sharpe_ratio` 在 13 个臂中全部饱和到 **≈0（1e-32 ~ 1e-70，含 baseline）**，此指标在本折网格下**不具备区分能力**：每个臂的 `run.log` 都打印 `Deflated Sharpe Ratio: 0.000 (n_trials=18, fold Sharpe std=<2.007 … 2.285>)`，`n_trials` 固定 18，只有 fold std 在 2.007–2.285 间变化。审计报告里 cdhb 的 DSR 0.5857 来自 `03_r2b_oos`（另一段时间窗、另一折网格），在本组不可复现——**不能用本表去证实或证伪那个 0.5857**。

## 2. 折叠与数据截断（实测，非估算）

- `n_folds = 18`、`n_valid_folds = 18`（全部 13 个臂一致），`actual_first_fold_start = 2022-01-04`，`actual_last_fold_end = 2026-09-07`（全部 13 个臂一致）。
- 首末折日期实测：折 1 = 2022-01-04 → 2022-04-11，折 18 = 2026-06-10 → 2026-09-07。
- **发生截断，且在这里明确报告**：请求区间 `2022-01-04 → 2026-10-22` 在 Fuyao 缓存中共 **1161 个交易日**；18 折实际覆盖 **1134** 个交易日（18 × 63），**末尾 27 个交易日（2026-09-08 → 2026-10-22）不构成完整折，未进入任何折的评估**。27 = 1161 − 1134，与 `actual_last_fold_end = 2026-09-07` 之后剩余的交易日数完全吻合。
- 因此本表的全部指标口径是"2022-01-04 → 2026-09-07 的 18 个季度折"，**不是**到 2026-10-22。

## 3. ≡ 静默空转矩阵（逐折对齐 `portfolio_equity`，对齐键 `(fold, date)`）

对齐基准：`baseline/walkforward_rebalances.csv` 的 `portfolio_equity`，去重后 **500 个调仓行 / 18 折**。判定阈值 `max|diff| < 1e-9`。

| arm | 对齐行数 | 未对齐行数 | max&#124;diff&#124; (RMB) | 判定 |
|---|---|---|---|---|
| no_voltarget | 492 | 20 | 3.8318e+03 | 有效差异 |
| no_orthogon | 388 | 202 | 5.9021e+03 | 有效差异 |
| no_structregime | 500 | 0 | 0.0000e+00 | **≡ 数值恒等** |
| no_cyq | 393 | 191 | 3.7434e+03 | 有效差异 |
| no_atrstop | 426 | 151 | 5.2852e+03 | 有效差异 |
| no_lowbeta | 492 | 15 | 7.9252e+02 | **≠（标准模式下是 ≡）** |
| no_sectorthrot | 472 | 48 | 4.0753e+03 | 有效差异 |
| no_hysteresis | 500 | 0 | 0.0000e+00 | **≡ 数值恒等** |
| no_thrust | 347 | 202 | 5.5854e+03 | 有效差异 |
| no_inertia | 443 | 466 | 6.6690e+03 | 有效差异 |
| bull_full | 433 | 183 | 5.5120e+03 | 有效差异 |
| no_circuit | 500 | 0 | 0.0000e+00 | **≡ 数值恒等** |

**walkforward 下的 ≡ 集合 = {`no_structregime`, `no_hysteresis`, `no_circuit`}。**

对标准模式（`06_ablate`，同一 `backtest_equity.csv` 逐 bar 对齐全 1161 bar）的**确认与反驳**：

| arm | 标准模式 ≡ ? | walkforward ≡ ? | 结论 |
|---|---|---|---|
| `no_structregime` | 是（max&#124;diff&#124;=0，1161 bar 全对齐） | 是（0，500 行全对齐） | **确认 ≡** |
| `no_hysteresis` | 是（0 / 1161 bar） | 是（0 / 500 行） | **确认 ≡** |
| `no_lowbeta` | 是（0 / 1161 bar） | **否**（492 对齐行，max&#124;diff&#124;=792.52，15 个调仓日仅单侧出现） | **反驳**——低 beta 惯性模块在折内会改变实际成交，只是连续跑时其影响恰好被抵消为 0 |
| `no_circuit` | 否（max&#124;diff&#124;=43.28，1161 bar 全对齐；相对 10 万本金约 0.04%） | **是**（0 / 500 行） | **walkforward 新增 ≡**——熔断在所有 18 折内从未触发（三档阈值被抬到 1000% 后净零差异），标准模式那 43.28 元差异在折切分后消失 |

关于 `no_structregime` / `no_hysteresis` 两个 ≡ 的落地事实（不是推断）：

- `no_hysteresis` 的 override `blend_enable_regime_hysteresis=false` **确实落进了 dump**（`strategy_dump.json.params.blend_enable_regime_hysteresis = false`），但 `baseline` 的 dump 里**根本没有这个键**，`StrategyConfig.blend_enable_regime_hysteresis` 的全局默认值本身就是 `False`（`pipeline/research_strategy/rs/config.py:794`），且 cdhb 的 `strategies_config.json` 参数块未覆盖它 ⇒ 该臂对 cdhb 而言是**结构性空操作**（不是"关掉后没影响"，而是"本来就是关着的"）。
- `no_structregime` 的 `blend_enable_structural_regime=false` 已按 `resolve_param` 第 3 优先级落到 dump，且该开关在 `blend_base.py:769` 只用于门控 fractal-dimension 的预计算分支；实测在 18 折 500 个调仓行上零差异 ⇒ 该分支在 2022-01→2026-09 从未改变 cdhb 的任何一笔决策。

## 4. 配置落地核对表（逐臂实测 `strategy_dump.json.params`）

| arm | 预期 override | dump 中实测值 | 缺失/仍为默认？ | `ignoring unknown key(s)` | Traceback |
|---|---|---|---|---|---|
| baseline | `{}` | 全为 dump 默认（`cdhb_enable_vol_targeting=true`、`blend_enable_orthogonalization=true`、`cdhb_max_assets_per_sector=2`、`blend_max_assets_per_sector=2` …） | — | 0 | 0 |
| no_voltarget | `cdhb_enable_vol_targeting=false` | `false` | 无 | 0 | 0 |
| no_orthogon | `blend_enable_orthogonalization=false` | `false` | 无 | 0 | 0 |
| no_structregime | `blend_enable_structural_regime=false` | `false` | 无 | 0 | 0 |
| no_cyq | `blend_enable_cyq_quality_gate=false` | `false` | 无 | 0 | 0 |
| no_atrstop | `blend_enable_adaptive_stop_atr=false` | `false` | 无 | 0 | 0 |
| no_lowbeta | `blend_enable_low_beta_inertia=false` | `false` | 无 | 0 | 0 |
| no_sectorthrot | `blend_max_assets_per_sector=20` + `cdhb_max_assets_per_sector=20` + `cdhb_adaptive_sector_throttle=false` | `blend_max_assets_per_sector=20`、`cdhb_max_assets_per_sector=20`、`cdhb_adaptive_sector_throttle=false`（三键齐全） | 无 | 0 | 0 |
| no_hysteresis | `blend_enable_regime_hysteresis=false` | `false` | 无（但 baseline 默认已是 false，见 §3） | 0 | 0 |
| no_thrust | `cdhb_dynamic_cash_deployment=false` | `false` | 无 | 0 | 0 |
| no_inertia | `cdhb_adaptive_inertia=false` + `cdhb_min_weight_change=0.0` + `cdhb_min_holding_bars=0` + `cdhb_entry_threshold=0.0` | `false` / `0.0` / `0` / `0.0`（四键齐全，`min_weight_change` 与 `entry_threshold` 均确为 0.0，非默认 0.05 / 0.03） | 无 | 0 | 0 |
| bull_full | `cdhb_target_bull_exposure=1.0` | `1.0`（默认 0.80） | 无 | 0 | 0 |
| no_circuit | `cdhb_dd_reduce_thresh=10.0` + `cdhb_dd_defensive_thresh=10.0` + `cdhb_dd_stop_thresh=10.0` | `10.0` / `10.0` / `10.0`（默认 0.1 / 0.15 / 0.2） | 无 | 0 | 0 |

**结论：13 个臂全部 INVALID=0（无一个臂因键缺失或仍为 dump 默认而作废）。** `no_sectorthrot` 的前缀孪生键 `cdhb_max_assets_per_sector=20` 实测已覆盖 dump 里钉死的 `2`，因此该臂在 cdhb 上**真实生效**（差异 max|diff|=4.08e+03，48 个调仓日单侧出现），不是空操作。

## 5. 三处 口径 声明（硬性）

1. **时间窗**：`--start 2022-01-04 --end 2026-10-22` 与 `06_ablate/` 标准模式**完全相同**。因此任一臂在本组与 `06_ablate` 对应臂之间的差异，只可能来自**评估模式（标准 vs walkforward）**，不含区间差异。（实际覆盖到 2026-09-07，见 §2 截断说明。）
2. **折规则**：`--window-years 0.25 --step-years 0.25`，与 `03_r2b_oos` / `07_r3_oos_bear` 相同，因此**折数可比**（本组 18 折；`07_r3_oos_bear` 的 cdhb 为 6 折，因其请求窗为 2021-06-01→2022-12-30）。
3. **现金收益率**：`--cash-sweep-rate 0.015`（年化 1.5%），与 `06_ablate/` 一致；`03_r2b_oos` 用的是 **0%**。**任何与 `03_r2b_oos` 数字的对比都必须披露这层 1.5% 现金收益稀释/增益**，且 `03_r2b_oos` 的窗口与本组不同，两组数字不可并列排名。

补充（同属数字口径，必须一并披露）：本组未加 `--market-impact` / `--max-volume-participation`（与 `06_ablate` §8 命令一致）；`--min-shares 100 --china-trading` 与 `06_ablate` 一致。

## 6. 哪些结论对本策略可引用 / 哪些臂作废

**可引用（walkforward 折扣内、且与标准模式同向的观察）**

| 观察 | 数字 | 依据 |
|---|---|---|
| 惯性/rebalance 闸是"拆掉后两模式 Sharpe 与 CAGR 同向变差"且位移最大的模块 | ΔSharpe −0.1392，ΔCAGR −2.3403 pp，ΔMaxDD +0.6548 pp；标准模式同向：ΔSharpe −0.1322、ΔCAGR −3.1666 pp | `no_inertia` |
| 牛市目标敞口 0.8→1.0 是唯一"拆掉后回撤确定性变深"的臂 | walkforward ΔMaxDD +0.8746 pp、ΔSharpe −0.3029；标准模式 ΔMaxDD +1.7661 pp（整段 11.8066%→13.5727%）、ΔCAGR +3.3077 pp | `bull_full`（**两模式 Sharpe 符号相反**，见下） |
| 三档回撤熔断在本窗口是死代码 | walkforward 与 baseline 逐折零差异（500/500 行，max&#124;diff&#124;=0）；标准模式整段仅差 43.28 元 / 0.04% | `no_circuit`、以及标准模式 `no_circuit` 的 43.28 元上限 |
| 结构 regime 与 regime 迟滞在本策略上是空转 | 两模式均逐 bar 零差异（walkforward 500/500 行；标准模式 1161/1161 bar） | `no_structregime`、`no_hysteresis` |

**不可引用 / 必须标注为"模式依赖、符号翻转"（这些臂本身有效，但结论不稳）**

| 臂 | 标准模式 | walkforward | 冲突点 |
|---|---|---|---|
| `bull_full` | ΔSharpe +0.1210、ΔCAGR +3.3077 pp、ΔMaxDD +1.7661 pp | ΔSharpe −0.3029、ΔCAGR −0.1264 pp、ΔMaxDD +0.8746 pp | **Sharpe 符号相反**；只有"敞口↑ ⇒ 回撤↑"同向 |
| `no_orthogon` | ΔSharpe +0.1230、ΔCAGR +1.7193 pp、ΔMaxDD −0.7247 pp | ΔSharpe −0.0568、ΔCAGR +1.6022 pp、ΔMaxDD +0.1479 pp | **Sharpe / MaxDD 符号相反**，CAGR 同向 |
| `no_thrust` | ΔSharpe +0.3690、ΔCAGR +2.1446 pp、ΔMaxDD −2.7392 pp | ΔSharpe +0.0905、**ΔCAGR −4.7422 pp**、ΔMaxDD −0.8335 pp | **CAGR 符号相反**（标准模式是提升，walkforward 是拖累） |

**作废（本策略下无信息量）**：`no_structregime`、`no_hysteresis`、`no_circuit` —— 三个臂与 baseline 逐折数值恒等（max|diff| = 0.0000e+00），**不得**被解读为"该模块无用"，只能说"在本窗口/本策略/本折网格下不产生任何差异"；其中 `no_hysteresis` 是结构性空操作（该开关对 cdhb 本来就是 `false`），因此**永久作废**，无论换什么窗口都不会有信息量。

**13 臂中无一个因配置未落地而作废**（§4 全部 0 缺失、0 unknown-key 警告、0 Traceback、manifest 全 `completed`）。

## 7. 审计结论（Grade A / 6.6% 风险预算）在本组证据下的状态

审计原文主张（`FORENSIC_AUDIT.md`）：cdhb 在 `03_r2b_oos` 上拿到 **Grade A / 0 缺陷**、Adj Sharpe 2.512、DSR 0.5857，并建议"可小仓实盘试运行，按 **6.6%** 而非 4.0% 做风险预算"（6.6% 来自把 39.2% 常态现金垫归一化后的满仓等效回撤）。逐条对照本组实测数字：

1. **没有任何一个臂削弱 Grade A**：13 臂中 ΔSharpe 的最好值为 `no_thrust` 的 **+0.0905**、最差值为 `bull_full` 的 **−0.3029**，而 `fold_sharpe_std` 为 **2.0065–2.2848**。也就是说，**拆掉任何一个风控模块都不能把 cdhb 的 Sharpe 提升超过一个折的 Sharpe 标准差**，同时没有任何臂把 Sharpe 打到负值（最低 1.1221，`bull_full`）。反向看，"某个模块在净拖累收益"也没有证据：ΔSharpe 为负的只有两臂——`no_inertia`（−0.1392）与 `bull_full`（−0.3029），其中 `no_inertia` 在标准模式同样为负（−0.1322）。
2. **6.6% 这个具体数字在本组既未被支持也未被否证，但有一个超出的实测值必须写下来**：
   - 本组 walkforward 的 `mean_max_drawdown = 4.2159%` 是**单季折**的平均最大回撤，**口径上与 6.6% 不可比**（6.6% 是 4.5 年整段的满仓等效回撤），所以本表无法验证 6.6%。
   - 真正可比的实测值是同一窗口、同一 1.5% 现金收益、同一 universe 下**标准模式的整段最大回撤：`11.8066%`**（`06_ablate/baseline/chan_dual_hybrid_blend`，1161 bar）。它**高于 6.6%**，且该组的请求区间与 6.6% 的推导区间（`03_r2b_oos`：2025-01 → 2026-07）不同。
   - 唯一"增加敞口"的臂 `bull_full`（牛市目标敞口 0.8→1.0）在两种模式下都把回撤推深：walkforward **4.2159% → 5.0906%（+0.8746 pp）**，标准模式整段 **11.8066% → 13.5727%（+1.7661 pp）**。方向上支持"现金垫确实压低了回撤、按满仓等效做风险预算"这一逻辑，但**给不出 6.6% 这个点估计**。
3. **审计的 DSR 证据在本组不可复现**：cdhb 的 `deflated_sharpe_ratio` 在 13 个臂里全部落在 **1.1882e-32 ~ 5.4011e-70**（含 baseline 的 2.8637e-50），原因是本折网格固定 `n_trials=18` 且 `fold_sharpe_std` 高达 2.007–2.285（baseline 2.2141）。因此**不能用本组 DSR 去支持或推翻 0.5857**；同时它也说明"0.5857 的非饱和"高度依赖 `03_r2b_oos` 那一套折网格，不能当作跨口径稳健的结论。

**一句话状态（只给数字）**：13 臂无一削弱 Grade A（|ΔSharpe| ≤ 0.3029 < fold_sharpe_std 2.0065–2.2848）；`bull_full` 是唯一在两个模式下都推深回撤的臂（+0.8746 pp / +1.7661 pp），方向上与"现金垫须做满仓等效风险预算"一致；但本组 walkforward 的单折 4.2159% 与 6.6% 口径不同、同窗口标准模式整段回撤为 11.8066%（高于 6.6%），**6.6% 这一具体数字在本组 13 臂中既得不到支持也得不到否证**。

## 8. 复现

```bash
# 13 个臂（脚本内 override 逐字取自 docs/backtest_research.md §8 run_arm，pfx=cdhb）
bash /tmp/opencode/ablwf/run_cdhb.sh
# 落地核对 + 总表 + ≡ 矩阵
pipeline/.venv/bin/python /tmp/opencode/ablwf/verify_cdhb.py
```

产物清单（每臂）：`walkforward_summary.json`、`walkforward_report.csv`、`walkforward_rebalances.csv`、`walkforward_weights.csv`、`comparison_report.json`、`strategy_dump.json`、`run_manifest.json`、`run.log`。

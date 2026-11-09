# LV23: cfsb 风格桶再归一化钳位（blend_bucket_renorm_max_delta）四臂实测与机制诊断报告

## 1. 实验背景与预注册方案

### 1.1 研究背景
在 LV20（补全 reversal 桶的收益性研究，负结果结案）实测中，独立观察到 `chan_four_state_blend`（cfsb）在风格分桶权重再归一化过程中存在显著的动态失真：
当前在 `pipeline/research_strategy/strategies_config.json:412` 中被钉住的参数 `blend_bucket_renorm_max_delta = 0.15`，导致实际应用的桶权重向量在多达 100–117/1134 bar 上显著滞后于瞬时再归一化目标值（单点 L1 偏差最高达 1.49，翻转后动量桶最多滞后 5 个 bar 仍持有防御桶权重）。
本实验（LV23）作为 LV20 的预注册后续任务，旨在系统性评估 `blend_bucket_renorm_max_delta` 钳位参数的有效性、行为影响及绩效敏感性，判定该钳位是提供了有效的调仓平滑防抖，还是带来了不必要的时序失真。

### 1.2 四臂设计与参数配置
本实验运行几何严格遵循 18 折滚动 walkforward 设定（2022-01-04 → 2026-10-22，window=0.25y, step=0.25y，现金计息 1.5%，MarketDB 本地 DuckDB，20 标的池）：
- **`a00_base_015`**：基线对照臂，`blend_bucket_renorm_max_delta = 0.15`（与 `strategies_config.json` 现行钉值一致，亦与 LV21 `02_cfsb_baseline` 完全可比）。
- **`a01_none`**：无钳位消融臂，`blend_bucket_renorm_max_delta = None`（彻底解除单日权重变动上限约束）。
- **`a02_030`**：放宽消融臂，`blend_bucket_renorm_max_delta = 0.30`（单日最大变动 30%）。
- **`a03_060`**：激进放宽消融臂，`blend_bucket_renorm_max_delta = 0.60`（单日最大变动 60%）。

### 1.3 预注册裁决门槛（Pre-registered Gate）
- **通过（PASS）**：`|ΔSharpe| ≥ 0.15` **且** `≥ 14/18` 折同号，同时满足留一折（Leave-One-Fold-Out, LOO）符号稳定性。
- **无效/无显著差异（NULL）**：未达到 PASS 门槛，且各臂与基线存在实际行为差异（非逐字节相同）。
- **证伪惰性（FALSIFIED-INERT）**：各臂与基线产物 md5 逐字节相同、差异为零（惰性轴）。

### 1.4 纪律红线
1. 严禁改动 `pipeline/research_strategy/strategies_config.json`（该文件已钉住 0.15，由 `test_blend_track_c.py:477` 严格断言）。
2. 严禁运行 `--dump-strategies`。
3. 严禁传递 `--weighting-mode`（权重模式必须保持 `regime_ic_ir_hybrid`，若在 `rolling_ic_ir` 下运行，代码将在 `blend_base.py:514-532` 提前返回而不执行钳位，导致实验无效）。
4. 仓库内零 `.py` 代码修改、零版本轴升级。若结果为 NULL，按预注册协议依规结案，不得在看到结果后临时追加参数网格。

---

## 2. 数据覆盖与环境披露

### 2.1 标的池与数据源
- 标的池文件：`backtests_research/20261108_101441_bucketrenorm/universe_20_stocks.txt`，包含实盘当前 20 只代表性 A 股标的。
- 数据源：`MarketDBDataProvider`（参数 `--data-provider marketdb --no-cache`），本地持久化 DuckDB。

### 2.2 数据覆盖披露（Coverage Disclosure）
在 Step 0 运行前完整性检查中确认：
- 标的 `600941.SH`（中国移动）在 A 股市场的上市首日为 **2022-01-05**，比本次回测要求的起始日期 **2022-01-04** 晚恰好 1 个交易日（标的 `601728.SH` 起始于 2021-08-20，其余 18 只均覆盖至 2016 年）。
- 经全量检查，全池 20 只股票在回测主区间及预热区间内数据均完整可用（单标的 1163–2462 bars，最新日期均为 2026-10-27），且 `600941.SH` 在 2022-01-04 当日因未上市自然不产生交易信号，对整体回测与折划分无系统性截断影响。特此披露。

---

## 3. 参数绑定与非惰性验证（Binding & Non-Inertness）

### 3.1 参数注入与执行日志留痕
四臂均顺利完成 18 折滚动回测，退出码全为 0（耗时记录引自 `elapsed_seconds.tsv`）：
- `a00_base_015`：耗时 237s，退出码 0。
- `a01_none`：耗时 239s，退出码 0。
- `a02_030`：耗时 236s，退出码 0。
- `a03_060`：耗时 237s，退出码 0。

各臂的参数绑定验证均从盘上归档严格解析：
1. **策略 Dump 校验**（`<ARM>/strategy_dump.json`）：
   - `a00_base_015`: `params.blend_bucket_renorm_max_delta == 0.15`
   - `a01_none`: `params.blend_bucket_renorm_max_delta == None`
   - `a02_030`: `params.blend_bucket_renorm_max_delta == 0.3`
   - `a03_060`: `params.blend_bucket_renorm_max_delta == 0.6`
   - 四臂中 `blend_weighting_mode` 均精确解析为 `"regime_ic_ir_hybrid"`。
2. **运行清单校验**（`<ARM>/run_manifest.json`）：
   - 各臂 `cli_overrides` 仅且仅包含 `blend_bucket_renorm_max_delta` 这一独立差异键。
   - 各臂 `n_folds == 18`，首折为 `2022-01-04..2022-04-11`，末折为 `2026-06-10..2026-09-07`。

### 3.2 产物散列与非惰性证明
为排除「轴生效但未引起决策变化」的假阴性（如 LV21 `crb@0.30` 出现的 md5 全同），提取关键调仓产物的 MD5 校验和：

| 实验臂 | `walkforward_weights.csv` MD5 | `walkforward_rebalances.csv` MD5 | 状态判定 |
| :--- | :--- | :--- | :--- |
| `a00_base_015` | `69c8caa98d778ddf07d9891b8847571f` | `e7a9189439b892d82392a67de582a833` | 基线基准 |
| `a01_none` | `84bfe54504e68987c088d8e04326b07f` | `2f5dca57f57e6d37cd7fd2a4c9c1f67a` | **非惰性**（完全相异） |
| `a02_030` | `3f7c872ca636ccdd38b45448b99f5413` | `c2c6e6fc4e060c6c6eeaeb772b81d99b` | **非惰性**（完全相异） |
| `a03_060` | `ec90ed47a31707f79f78bbb74ace68c2` | `abcad316c60daac4a7cb2a48ed1e009c` | **非惰性**（完全相异） |

四臂权重大表与调仓日志 MD5 均互不相同，确凿证明 `blend_bucket_renorm_max_delta` 并非惰性配置，而是深度渗透并改变了底层权重时序。

---

## 4. 机制诊断：翻转期与稳定期的滞后解耦（Flip-vs-Stable Diagnostic）

通过 `/tmp/opencode/run_bt_debug.py` 开启 `logging.DEBUG`，在标准全区间回测模式（Standard Mode，消除滚动折重复采样干扰）下对四臂进行逐日跟踪（回测区间 1161 bars，预热含回测共 1426 bars）。逐日对比 `blend_base.py:584` 的 `pre-renorm` 原始再归一化向量与 `:604` 的 `delta-clamped` 实际应用向量：

### 4.1 回测区间诊断结果（1161 bars, 2022-01-04 → 2026-10-22）

口径说明：regime 标签取同一条日志行 `budgets=` 三键（`momentum` / `reversal` / `defensive`）的 `argmax`，**不是** `pre-renorm` 的 `argmax` —— 因 reversal 风格桶恒为空（见 LV8 更正条），`pre-renorm` 每根 bar 只有 `momentum` / `defensive` 两键，误用会得出 1128 / 0 / 33 与仅 26 次翻转的错误口径（正确为 1064 / 64 / 33、81 次翻转）。"翻转滞后"按**严格定义**统计：滞后 bar 自身 `regime_t != regime_{t-1}` 计为翻转，其余滞后 bar（含翻转后 1–4 日的跟随日、以及窗口首两日 `2022-01-04` / `2022-01-05` 的预热残留）计为稳定。若改用"最近一次翻转在 k bar 内"的宽松定义，`a00` 的 61/39 会依次变为 k=1: 89/11、k=2: 94/6、k=3: 96/4、k=4: 98/2；"翻转段"定义（翻转段内滞后再收敛）则为 98/2。报告采用 k=0 的严格定义。

| 实验臂 | 钳位设定 | 发生滞后 bar 数（占比） | 最大 L1 距离（峰值日期） | 最大连续滞后 bar | 机制翻转次数 | 翻转点滞后 bar | 稳定期滞后 bar |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`a00_base_015`** | 0.15 | 100（8.61%） | 1.4712（2022-03-15） | 5 | 81 | 61 | 39 |
| **`a01_none`** | None | 0（0.00%） | 0.0000 | 0 | 81 | 0 | 0 |
| **`a02_030`** | 0.30 | 65（5.60%） | 1.1712 | 3 | 81 | 60 | 5 |
| **`a03_060`** | 0.60 | 12（1.03%） | 0.5712 | 1 | 81 | 12 | 0 |

- 四臂在 1161 bars 下的 regime 直方图严格一致：`momentum` 1064 bars / `reversal` 64 bars / `defensive` 33 bars，机制翻转次数恒为 81 次。
- 全量（含 265 bars 预热，共 1426 bars）观测：
  - `a00_base_015`：滞后 130 bars（9.12%），最大 L1 1.4712（发生于 2020-12-22），最长连续滞后 5 bars。
  - `a02_030`：滞后 80 bars（5.61%），最长连续滞后 3 bars。
  - `a03_060`：滞后 16 bars（1.12%），最长连续滞后 1 bar。

### 4.2 机制特征结论
1. **滞后高度聚焦于机制翻转时刻**：在基线 `0.15` 下，100 次滞后中有 61 次发生于 regime 翻转当日或紧邻翻转点；在 `0.30` 下，65 次滞后中有 60 次（92.3%）集中在翻转点；当放宽至 `0.60` 时，**全部 12 次滞后 100% 发生在翻转当日**，稳定期滞后彻底归零。
2. **独立证实 LV20 假说**：实测确凿证实了 LV20 §2 及 2026-11-07 测量结论——`blend_bucket_renorm_max_delta = 0.15` 的主要行为后果并非在平稳期平滑噪音，而是在 regime 翻转发生后对新配置产生 1–5 bars 的时滞阻尼。

---

## 5. Walkforward 绩效评估与门槛裁决

### 5.1 总体指标总览（引自各臂 `walkforward_summary.json`）

| 实验臂 | 钳位设定 | Mean Sharpe | Mean CAGR | Mean MaxDD | Sharpe Std | Deflated Sharpe Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`a00_base_015`** | 0.15（基线） | 1.1516 | 25.30% | 4.68% | 2.3166 | 2.24e-82 |
| **`a01_none`** | None | 1.2701 | 29.60% | 4.75% | 2.3456 | 5.29e-73 |
| **`a02_030`** | 0.30 | 1.2196 | 26.34% | 4.73% | 2.2444 | 3.66e-69 |
| **`a03_060`** | 0.60 | 1.3061 | 29.86% | 4.68% | 2.2957 | 1.41e-65 |

*注：`a00_base_015` 的 Mean Sharpe 1.1516 与 LV21 登记的 `02_cfsb_baseline`（1.1516）完全一致，确认基线完全复现。*

### 5.2 差分表现与预注册门槛裁决（对照 `a00_base_015`）

| 实验臂 | ΔSharpe | ΔCAGR | ΔMaxDD | 逐折同号 (+/-/0) | LOO 符号稳定性 | 门槛达成情况 | 最终裁决 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`a01_none`** | **+0.1184** | +4.30pp | +0.06pp | 11 / 7 / 0 | True [0.0613, 0.1356] | 幅度不足 0.15 且广度 11/18 < 14/18 | **NULL** |
| **`a02_030`** | **+0.0680** | +1.05pp | +0.05pp | 6 / 12 / 0 | True [0.0239, 0.0859] | 幅度与广度双未达标 | **NULL** |
| **`a03_060`** | **+0.1545** | +4.56pp | -0.00pp | 10 / 8 / 0 | True [0.0995, 0.1738] | 幅度微超 0.15，但广度 10/18 < 14/18 | **NULL** |

### 5.3 逐折表现解剖（引自 `walkforward_report.csv`）
深入分析逐折 Sharpe 数据可以明确增益与回撤的来源：
1. **主要收益集中于剧烈波动折**：
   - **Fold 16**（2025-11-28 → 2026-03-06）：基线 Sharpe 为 1.4189，而 `a01_none` 与 `a03_060` 均为 2.5079（**ΔSharpe +1.0890**）。
   - **Fold 07**（2023-07-27 → 2023-10-31）：基线 Sharpe 为 -2.2928，`a01_none` 收窄至 -1.9787，`a03_060` 收窄至 -1.4022（**ΔSharpe +0.8906**）。
   - **Fold 06**（2023-04-24 → 2023-07-26）：基线 Sharpe 为 0.1576，`a01_none` 与 `a03_060` 均为 0.6679（**ΔSharpe +0.5103**）。
   - **Fold 05**（2023-01-17 → 2023-04-21）：基线 Sharpe 为 4.5639，`a01_none` 与 `a03_060` 均为 5.0277（**ΔSharpe +0.4638**）。
2. **缺乏全样本广度支持**：
   - 虽然在上述大波动行情中，快速释放权重有利于捕捉动量或规避防御损失，但在其余 7–8 个折（如 Fold 02, 03, 08, 09, 10, 11, 12, 17）中，无钳位或大钳位臂与基线表现持平甚至略有劣后（例如 Fold 11 中 `a00` Sharpe 1.4582 vs `a01` 1.4079）。
   - `a03_060` 尽管均值 ΔSharpe 达到 +0.1545，但正向折数仅 10 折（10/18），未达到预注册的 14/18 稳健性门槛；`a01_none` 正向折数亦仅 11 折。

---

## 6. 裁定结论与处置建议

1. **统一裁定：全部为 NULL**。
   三个放宽/解除钳位的消融臂均未能满足预注册设定的稳健性门槛（`|ΔSharpe| ≥ 0.15` 且 `≥ 14/18` 折同号）。
2. **严守负结果纪律，不作推荐值改动**：
   按照《AGENTS.md》与项目实盘纪律，研究结果由少数折驱动且广度不足时不应作为策略默认推荐。因此：
   - **维持现行配置不变**：`strategies_config.json:412` 的 `blend_bucket_renorm_max_delta = 0.15` 保持原状。
   - **不修改任何代码，不升版本号，不运行 `--dump-strategies`**。
   - 确认本研究闭环结案，不再盲目扩展参数搜索空间。

---

## 7. 附注与诚实披露（Caveats & Technical Notes）

1. **Standard Mode 与 Walkforward 样本覆盖差异**：Step 4 诊断脚本采用单段 standard 模式回测（1161 bars），而 walkforward 18 折去重交易日为 1134 bars。二者在 regime 分布与翻转点特征上完全吻合，不影响机制分析有效性。
2. **预热期（Warmup）影响**：回测系统自 2020-12-02 开始预热（共 265 bars），预热期内的滞后特征（130 bars 滞后，最大 L1 1.4712）与正式回测期（100 bars 滞后）保持高度一致。
3. **DuckDBCache 路径伪警报**：`run_manifest.json` 中出现的 `db_path: data/cache.duckdb` 仅为缓存类默认属性字段，由于本次运行显式指定了 `--no-cache` 与 `--data-provider marketdb`，实际回测完全基于真实 MarketDB 运行（`db_used: False`）。
4. **DSR 指标有效性**：在本实验中，四臂的 Deflated Sharpe Ratio 均处于极小值量级（~1e-65 .. 1e-82），这是由于滚动 18 折测试样本架构导致的统计量稀释，在各臂间不具备实质性判别力，本次裁决依据以均值 Sharpe、逐折分布及 LOO 稳定性为主。
5. **四臂缺 `run.log`（流程缺口披露）**：`a00_base_015` / `a01_none` / `a02_030` / `a03_060` 四个目录内**均无** `run.log`，而同批兄弟批次（`backtests_research/20261107_210715_baseline6/`、`backtests_research/20261107_222523_stage12/`）每一臂都有；`run.log` 由外层 shell 的 `tee` 在回测进程退出后写入，且被 [`backtester/run_backtest.py`](file:///home/stone/Work/github/quant/backtester/run_backtest.py) `:1533` 的 `_artifact_sizes` **按设计排除**在 `run_manifest.json` 的 `artifacts` 列表之外，因此清单本身不可能列出它，本批的 `tee` 产物亦未留存。**后果**：本批的审计轨迹只能依赖 `run_manifest.json` 的 `cli_args.argv` + `strategy_dump.json` + `/tmp/opencode/` 下的四个 DEBUG 日志（后者为仓库外的临时文件，非归档产物，不可长期依赖）。此处如实披露为流程缺口，**不声称**本批存在运行日志。
6. **机制诊断日志与验证脚本持久化归档**：Step-4 机制诊断所使用的 4 个臂 DEBUG 运行日志（`diag_a00_base_015.log`、`diag_a01_none.log`、`diag_a02_030.log`、`diag_a03_060.log`）及分析验证脚本（`run_bt_debug.py`、`verify_a00_diag.py`、`verify_flip_stable.py`）已全部完整归档至本运行目录下的 `diagnostics/` 子目录，各文件 md5 校验码、行数与机制说明详见 `diagnostics/README.md`。核心机制证据现已在仓库内持久化留存，不再依赖外部临时目录 `/tmp/opencode/`。

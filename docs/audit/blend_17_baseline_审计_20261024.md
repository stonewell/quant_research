# blend_17_baseline Walkforward 深度审计报告（2026-10-24 重跑批次）

> 审计目录：`pipeline/results/blend_17_baseline/{1,2}`（严格限于该目录，未回退到 `pipeline/results`）
> 审计日期：2026-10-24　|　审计口径：walkforward-audit 六阶段脚本 + 独立重算 + provenance 核查
> 宇宙：20 只 A 股（仓外文件 `core_satellite_20_stocks.txt`，`sha256=ed48e922…`）｜基准：`000300.SH`
> 配套旧审计：[`docs/audit/blend_17_baseline_审计.md`](file:///home/stone/Work/github/quant/docs/audit/blend_17_baseline_审计.md)（2026-10-21，**不可逐位比对**，见 §8）

---

## 0. 结论速览

1. **本次产物可追溯，但 `git_commit=1db85fd` 不可单独复现**：运行窗口（batch1 `end=2025-12-31`、batch2 `end=2026-09-30`）取自当时**未提交**的 `pipeline/scripts/backtest_blend_strategies.sh` 与 `run_blend_cross_matrix.py`（原值 `2026-01-01` / `2026-10-01`）。该改动已于 2026-10-24 21:35:39 提交为 **`555dbdd`（当前 HEAD）**，复现必须以 `555dbdd` 为准，`1db85fd` 只是运行时 HEAD。
2. **报告的"业绩"数字口径错误，须打折**：`mean_cagr` 是**逐折年化收益率的算术平均**（`backtester/run_backtest.py:1421-1423`、`backtester/SCHEMAS.md:160-161`），不是复利收益。按折区间重算链式 CAGR：**22.9 / 22.2 / 22.0 / 21.1 / 20.2 / 12.9%**（报告值 30.2 / 29.9 / 33.0 / 28.0 / 26.5 / 18.0%），**虚高 5–11pp**。
3. **DSR≈1e-40 不是"没有 alpha"的证据**：`n_trials` 被误设为折数（15），`sharpe_std` 传入折间离散度（2.31），零假设因此预测"纯运气下 15 次试验的期望最大 Sharpe≈4.08"，观测均值 1.58 反而低 13 个标准误。批2 的 DSR≈0.9999 是 3 折样本过少的反向伪象。**两批 DSR 不可比、不可引用。**
4. **真实证据是折级一致性，不是 DSR**：15 折中 **10–11 折**同时跑赢基准 CAGR 与 Sharpe；折均值 Sharpe 的 t=**2.13–2.65**（p≈0.02–0.05，勉强显著）；但 6 策略折收益相关性 **0.80–0.98（均值 0.92）**，且 #1–#5 两两配对 t**≤1.04** → **名次不可信，应视为同一策略族**。
5. **regime 高度集中**：2023Q1（F5: 2023-01-17~04-21）与 2024Q1（F9: 2024-01-30~05-10）两折贡献对数增长的 **57–71%**（Generic 达 73%）；最差折六策略**同为 F7（2023-07-27~10-31）**。
6. **批2 是干净的 OOS 时间续接**（2025-11-28 起，与批1 末折 2025-11-27 **零重叠、零空隙**），但只有 3 折：名次整体翻转（风控型上位），Risk-Managed 52.4%、Four-State Risk-Managed 56.2%、Dual Hybrid 83.6% 的正收益来自单折；Dual Hybrid 剔除 `601872.SH` 净盈亏由 +1,096 翻为 **−3,839.61 RMB**（LOO CRITICAL）。
7. **质量门干净**：全策略 All-In=0、单票峰值 20.0%（内部 20% 上限）、F1 预热 +0 天、单次权益跳变 ≤9.7%、买入命率 44.9%–59.1%（无前视泄漏迹象）。**不干净的是资本效率与摩擦**：闲置现金批1 25.6–29.0%、批2 36.7–43.8%；批1 摩擦占净利 10.2–16.1%，批2 Dual Hybrid **125.8%**。
8. **判定：条件性可用（Grade B，风险指数 25/40/30；Dual Hybrid 为 Grade C/50）**。不可据 DSR 下结论、不可据#1–#5 名次选型、不可据批2 单票归因下注；实盘须限仓、限换手，并按**组合级连续权益**而非 fold-mean 口径跟踪。

---

## 1. 审计对象与运行口径

| 项 | batch 1 | batch 2 |
|---|---|---|
| 请求区间 | 2022-01-01 → 2025-12-31 | 2025-11-28 → 2026-09-30 |
| 实际覆盖 | 2022-01-04 → **2025-11-27**（尾部缺 34 日历日） | 2025-11-28 → **2026-09-07**（尾部缺 ~23 日历日） |
| 折数 / 窗口 | 15 折，每折 63 bar（0.25y） | 3 折，每折 63 bar |
| 宇宙 | 20 只，`universe_load_start=2020-11-29` | 同一 20 只，`universe_load_start=2024-10-26` |
| provider / 交易约束 | `fuyao` / `china_trading=true`、`min_shares=100` | 同左 |
| 基准 | `000300.SH`，`equal_weight`，**刻意无约束** | 同左 |
| 运行窗口（-07:00） | 21:08:09 → 21:11:33（203.5s） | 21:11:35 → 21:12:22（47.1s） |

补充：batch1 的 34 天尾部缺口是 `run_backtest.py:1436-1444` 允许的正常松弛（阈值 102 天），**不是数据缺失**；batch2 末折止于 2026-09-07，`2026-09-07 → 今天(2026-10-24)` 约 6 周数据未被使用。

---

## 2. 可追溯性核查（provenance）

| 字段 | 值 / 结论 | 裁定 |
|---|---|---|
| `git_commit` | `1db85fd066c243f369dfda0cb4b6a636803d037e`（2026-10-24 20:38:16） | **≠ 当前 HEAD `555dbdd`**；1db85fd 是 HEAD 的祖先 |
| `git_dirty` | `true`，脏路径 = `backtest_blend_strategies.sh`、`run_blend_cross_matrix.py` | 该 diff **改的是运行窗口**（`2026-01-01→2025-12-31`、`2026-10-01→2026-09-30`），**非装饰性** |
| 复现口径 | 必须用 `555dbdd`（21:35:39 提交，即本次运行后 23 分钟） | `1db85fd` 单独**不可复现**本次结果 |
| `db_sha256` | `86a8638e35c62d748715515b2254c811ab3148cdae7356cdc7c98518b97a0c4f`（324 MB） | 与 `docs/version.md` §16.15 记的三个历史指纹均不同 → 库在 2026-10-23 21:38 后又被写入过 |
| `strategy_dumps` md5 | 6 份全部落盘且与实际文件一致 | 内容可验 |
| dump mtime | 全部 `2026-10-24 21:11:34`（批1 结束后 1 秒、批2 开始前） | 是 `--dump-strategies` 的**字节级重 dump**（md5 未变），**不能**证明参数在 2025-11-28 前冻结 |
| 宇宙出处 | 仓外 `core_satellite_20_stocks.txt`（`sha256=ed48e922…`，20 只、顺序一致） | **PROVENANCE 未记该路径/哈希**（已知缺口，§16.15.8） |
| `seed` | `null`（链路无 RNG） | 同代码+同库确定性可复现 |

**结论**：§16.15.5「产物无法绑定库状态」的缺口**已部分关闭**（DB/git/dump 均可机械验证）；**仍未闭环**的是 ① 宇宙文件哈希未入库、② dump 的"创作时刻"不可考（dumps 被 gitignore）→ 批2 的 **OOS 纯度不能被证明**，只能说"时间上在批1 之后"，不能声称"参数冻结后才见的样本"。

---

## 3. 三个口径陷阱（本报告最重要的部分）

### 3.1 `mean_cagr` ≠ 复利 CAGR

折是**互不重叠且首尾相接**的（0.25y 窗口、0.25y 步长），因此可用折起止日把每折年化值还原为区间收益再连乘。总跨度 2022-01-04 → 2025-11-27 = **1,423 天**。

| 策略 | 报告 `mean_cagr` | **链式 CAGR** | 链式总收益 | 折级跨折回撤（下界） | 报告 `mean_maxdd` |
|---|---|---|---|---|---|
| Chan Single Crisis Shield | 30.2% | **22.9%** | +123.3% | 7.8%（F2） | 5.03% |
| Chan Four-State Risk-Managed | 33.0% | **22.0%** | +117.2% | 11.8%（F2） | 5.01% |
| Chan Risk-Managed | 29.9% | **22.2%** | +118.8% | 10.0%（F2） | 4.36% |
| Chan Four-State Shield | 28.0% | **21.1%** | +110.9% | 4.9%（F7） | 5.17% |
| Chan Dual Hybrid Alpha | 26.5% | **20.2%** | +104.7% | 7.2%（F2） | 4.43% |
| Generic Multi-Strategy | 18.0% | **12.9%** | +60.4% | 9.9%（F12） | 5.79% |
| **基准 000300.SH** | 6.3% | **−1.6%** | −6.1% | 32.1% | — |

要点：
- 报告的 `mean_cagr` 系统性**虚高 5.4–11.0pp**，因为 3 个月窗口的年化因子约 4×，把"+24% 的单季"放大成"+134% 的 CAGR"，算术平均再被这两个极值主导。
- `mean_max_drawdown` 是**折内回撤的均值**，天然忽略跨折回撤；折级链式回撤（4.9–11.8%）已是下界，**日频真实回撤必然更大**。引用"MaxDD 4.4%"作为风险预算是错误的。
- `mean_calmar_ratio`（10.5–12.0）同为 15 个折 Calmar 的算术平均，**没有组合含义**，不应出现在任何对外材料中。
- 相对基准的超额是真实的：基准同期链式 CAGR **−1.6%**，且基准折级回撤 32.1%；策略 12.9–22.9% 对应 **14–24pp/年** 的链式超额。

### 3.2 DSR 是"零假设误指定"的算术产物

实现：`common/metrics.py:198-215`（`deflated_sharpe_ratio`）、`common/metrics.py:187-195`（`expected_max_sharpe`）。walkforward 路径的实参：`backtester/run_backtest.py:1393-1400`（另一处 `:875-880`）

```
observed_sharpe = mean(15 折 Sharpe)          # 如 1.5798
n_trials        = n_valid_folds = 15          # ← 把"折"当成"独立试验"
n_obs           = round(0.25*252) = 63
sharpe_std      = 折 Sharpe 的 std(ddof=1) ex. 2.3091   # ← 折间离散度被当作"跨试验选择噪声"
skewness/kurtosis = 默认 0 / 3.0
sr0 = 2.3091 × 1.7668 ≈ 4.080
z   = (1.580 − 4.080) × √62 / √(1 + 0.5×1.58²) ≈ −13.1
DSR = Φ(−13.1) ≈ 1e-39                      # 落盘 5.96e-40
```

- 机制：折间 std 2.31 里**绝大部分是真实 regime 波动**（约为单折抽样 SE 0.126 的 18 倍），把它当作选择噪声，零假设就变成"运气最好的一次试验该有 Sharpe 4.08"，于是观测值必然"不显著"。
- 批2 反向：3 折、std≈0.97、`expected_max_sharpe(3, 0.97)≈0.82`，观测 1.537 → DSR≈0.9999。**同一指标在两批间从 1e-40 跳到 1.0，恰好证明它衡量的是折数与折间离散度的算术组合，而非 alpha。**
- 结论：**DSR 列既不能当否决票，也不能当通过票**。可用的显著性指标是折均值 Sharpe 的 t 检验（§4）。

### 3.3 基准比较不是同口径

`backtester/run_backtest.py:952-961`：基准被**刻意解除约束**——`min_shares=0`、`min_weight_change=0.0`、`china_trading=False`。策略侧则是 `min_shares=100`、`china_trading=True`、`min_weight_change=0.05`（`run_backtest.py:1094-1105`，6 份 dump 均为 0.05）。

方向上：基准**零摩擦、无 T+1/涨跌停/整手约束**，因此基准 CAGR 被系统性高估，策略的 `outperformance` 在成本轴上偏保守；但基准的 `min_weight_change=0` 又允许无成本频繁再平衡，会略微虚增基准。**净效应无法用现有产物量化**，故 `outperformance` 只能作方向性参考，不能作为"精确超额 xx pp"引用。

---

## 4. 折级统计、显著性与有效性

### 4.1 折 Sharpe 分布（batch 1，15 折）

| 策略 | 负折 | mean | median | std | 最好折 | 最差折 | 胜基准（CAGR & Sharpe） |
|---|---|---|---|---|---|---|---|
| Chan Single Crisis Shield | 5 | 1.580 | 1.363 | 2.309 | 5.283（F9 2024-01-30~05-10） | −1.562（F7） | 11 / 10 |
| Chan Risk-Managed | 4 | 1.525 | 1.150 | 2.358 | 4.998（F9） | −1.690（F7） | 11 / 11 |
| Chan Four-State Shield | 6 | 1.454 | 1.395 | 2.251 | 5.391（F9） | −1.907（F7） | 11 / 10 |
| Chan Dual Hybrid Alpha | 5 | 1.440 | 1.270 | 2.422 | 5.600（F14 2025-05-27~08-22） | −2.018（F7） | 11 / 10 |
| Chan Four-State Risk-Managed | 4 | 1.372 | 1.406 | 2.489 | 5.301（F9） | −2.238（F7） | 10 / 10 |
| Generic Multi-Strategy | 6 | 0.767 | 0.679 | 1.978 | 3.940（F8） | −2.214（F7） | 11 / 10 |
| 基准 000300.SH（各折） | 6 | −0.10 | — | 2.60 | 5.647（F14） | −3.317（F10） | — |

**最差折六策略全部落在 F7（2023-07-27~10-31）**，最好折集中在 F9/F14 → 同源 regime 暴露。

### 4.2 名次不可信

15 折配对 t 检验（同一折同一市场，配对消噪）：

| 对比 | 均值差 | t |
|---|---|---|
| Crisis Shield vs Risk-Managed | +0.05 | **0.35** |
| Crisis Shield vs Four-State Shield | +0.13 | 1.04 |
| Crisis Shield vs Dual Hybrid | +0.14 | 0.72 |
| Dual Hybrid vs Risk-Managed | −0.09 | −0.36 |
| Four-State Risk-Managed vs Risk-Managed | −0.15 | −0.61 |
| **任一 Chan blend vs Generic** | +0.60 ~ +0.81 | **1.35 ~ 2.14** |

折收益相关性：5 个 Chan blend 之间 **0.93–0.98**，与 Generic **0.80–0.86**，全体均值 **0.92**。

→ **#1–#5 是同一个策略族的五种参数化，名次差异在统计上不可分辨**；唯一可分辨的结论是"Chan 结构族显著优于纯 WQ/GTJA 微观 alpha 的 Generic"（且仅 t≈2，边际）。

### 4.3 统计功效：15 折只够"边际显著"

- 单折 63 bar，年化 Sharpe 的抽样 SE（Lo 2002）：SR=1.5 时 ≈ **0.18**，SR=5.6 时 ≈ **0.52**。
- 折间 std 2.31 ≈ 抽样 SE 的 **18 倍** → 折间波动几乎全是真实 regime 差异，故均值的正确标准误是 `2.31/√15 = 0.58–0.64`（不是 0.126/√15）。
- 由此 t = **2.13–2.65**（p≈0.019–0.049）：**显著但脆弱**——删掉最好折（F9, 5.28）后 Crisis Shield 均值从 1.58 掉到 1.31。
- 若要区分"真实 Sharpe 1.5"与"0.5"（Δ=1.0），需要 `n ≥ (2.31/0.51)² ≈ 21` 个季度折（≈5.3 年）。**当前 15 折（3.9 年）不足。**
- 收益集中度：F5+F9 两折贡献对数增长 **Crisis Shield 58% / Risk-Managed 60% / Four-State Risk-Managed 71% / Four-State Shield 61% / Dual Hybrid 58% / Generic 73%**。

---

## 5. 六阶段异常 / 过拟合检查矩阵

### 5.1 batch 1（top-3 详表，全部六只同族）

| 维度 | Crisis Shield | Risk-Managed | Four-State Shield |
|---|---|---|---|
| Safety Grade / 风险指数 | **B / 25** | **B / 25** | **B / 25** |
| All-In 事件（≥99%） | 0 | 0 | 0 |
| 单票峰值（上限 20%） | 20.0% | 20.0% | 20.0% |
| F1 预热延迟 | +0d | +0d | +0d |
| 单次权益跳变 | 7.4% | 5.3% | 7.0% |
| 买入命率（45–55% 正常） | 58.1% | 56.9% | 59.1% |
| 利润集中（单折占比 <40%） | 28.7%（F9） | 30.3%（F5） | 33.8%（F9） |
| Top3 资产占净利（<60%） | 60.2% ⚠ | 38.6% | 50.4% |
| 摩擦占净利（<15%） | 12.7%（5,721 RMB） | **16.1%**（7,123） | 10.2%（5,779） |
| 闲置现金 / 现金归一 MaxDD | 25.6% / 6.8% | 29.0% / 6.1% | 27.2% / 7.1% |
| LOO：剔除 Top1 / Top2 / Top3 | −24.8% / −46.4% / **−60.2%**（仍正） | −14.8% / −27.0% / −38.6% | −22.0% / −38.2% / −50.4% |
| 换手 / 折 | 3.1× ⚠ | 3.9× ⚠ | 3.1× ⚠ |

**批1 仅对 top-3 出具评级**（脚本 `--deep-top-n 3`），其余三只**未评级**，只能读折统计与排名表（§4）：Dual Hybrid 换手 **4.1×/折**、最大单次跳变 9%；Four-State Risk-Managed 换手 3.8×、CAGR 最高但折 std 2.49；Generic 换手 **8.0×/折**、6 个负折、折 std 1.98、折均值 Sharpe 0.77，是唯一在统计上明显落后的成员。**不得把"未评级"读成"安全"。**

**无一条 LOO 翻转负值**（最差为 Crisis Shield 剔 Top3 后仍 +17,882.79 RMB），故批1 的"明星资产依赖"是**程度问题**，不是存亡问题；但 Top3（`000938.SZ`、`300394.SZ`、`601872.SH`）在 6 只策略里反复出现，说明 alpha 来自**同一小簇动量票**。

### 5.2 batch 2（3 折，风控口径）

| 策略 | Grade / 风险 | DSR | Top3 占净利 | 单折集中 | 摩擦/净利 | 闲置现金 | LOO 剔除 Top1 |
|---|---|---|---|---|---|---|---|
| Chan Risk-Managed | **B / 40** | 0.9999 | 115.4% ⚠ | 52.4%（F2） | **35.4%** ⚠ | 37.1% | −59.0%（仍正） |
| Chan Four-State Risk-Managed | **B / 30** | 0.9839 | 94.3% ⚠ | 56.2%（F2） | 22.1% ⚠ | 36.7% | −42.4%（仍正） |
| Chan Dual Hybrid Alpha | **C / 50** | 0.9396 | 635.6% ⚠ | **83.6%（F1）** | **125.8%** ⚠ | 43.8% | **−3,839.61 RMB（翻负，CRITICAL）** |
| Chan Single Crisis Shield（未评级） | — | 0.259 | 未评级 | 未评级 | 未评级 | 未评级 | 未评级 |
| Generic（未评级） | — | ~0 | 未评级 | 未评级 | 未评级 | 未评级 | 未评级 |
| Chan Four-State Shield（未评级） | — | ~0 | 未评级 | 未评级 | 未评级 | 未评级 | 未评级 |

批2 未评级三只的排名表事实（仅排名，不含安全评级）：Crisis Shield 原始 Sharpe 1.010 / 调整 0.921、CAGR 16.20%、MaxDD 3.1%、胜 2/3 折、换手 2.7×、DSR 0.259；Generic 0.974 / 0.774、CAGR 32.15%、MaxDD 4.5%、胜 2/3、换手 6.0×、DSR ~0；Four-State Shield 0.493 / 0.364、CAGR 10.52%、MaxDD 3.3%、胜 2/3、换手 3.0×、DSR ~0。

批2 的 `Net PnL` 是**按资产的已实现交易盈亏**（脚本口径），不等于组合权益变动，故"1,096 RMB / 3,823 RMB"这类绝对额不能与"批1 +44,916 RMB"直接比规模。

---

## 6. 批2：真 OOS 续接，但只有 3 折

- **时间连续性成立**：batch1 末折止 **2025-11-27**，batch2 首折起 **2025-11-28**，日期交集 = 0（同一标的 `601872.SH`：批1 末笔 2025-11-24 SELL 300 股 → 批2 首笔 2025-11-28 BUY 600 股），**无重复计账**。
- **预热刚好够**：6 个模板 `warmup_bars` 全为 **252**（VAA 腿 252 / 三类型 120 / 四态 120 / WQ 250），batch2 `universe_load_start=2024-10-26` 到首折前一日实得 **256–266 bar**，**最坏只剩 4 根余量**——若未来缩短预热会直接截断早期折。
- **结论强度**：3 折 × 63 bar 只够回答"这 9.5 个月有没有崩"，**不足以判名次**（名次已从批1 的 Crisis Shield #1 翻为 Risk-Managed #1）。批2 相对基准仍有超额（链式 7.1–18.7% vs 基准 ≈3.2%），但 Dual Hybrid 的"超额"完全由单票 `601872.SH` + 单折 F1 支撑，且摩擦吃掉 125.8% 净利 → **该配置不具备实盘性**。
- **OOS 纯度未证**：6 份 dump 在 21:11:34 被重写（字节相同），dumps 又被 gitignore，无创作时间 → 无法排除"参数是在含 2026 数据的回溯调参中定稿"。**这是本报告唯一无法用产物闭环的阻塞项。**

---

## 7. 资产层亏损归因与"是否剪池"

### 7.1 脚本输出

| 批次 | 亏损资产 | 净盈亏合计 | 可省费用 | 建议保留数 |
|---|---|---|---|---|
| batch 1（3 只） | `600900.SH` −4,100.85 / `600584.SH` −3,650.11 / `601857.SH` −3,571.12 | −11,322.07 RMB | 3,022.01 | 17 / 20 |
| batch 2（10 只） | `601857.SH` −2,266 / `601225.SH` −2,211 / `601398.SH` −2,070 / `600028.SH` −1,985 / `601766.SH` −1,944 / `600584.SH` −1,301 / `600660.SH` −1,043 / `601288.SH` −172 / `601088.SH` −159 / `300394.SZ` −18 | −13,169.13 RMB | 2,173.47 | 10 / 20 |

### 7.2 不建议据此剪池（四条理由）

1. **基池口径不同**：脚本跑的时候**没有匹配的 20 只池文件**（仓内最接近的 `docs/universe/china/core_satellite_100_stocks.txt` 才是 20/20 覆盖），故用了"观测符号回退集"，两批输出 17/20 与 10/20 **互不兼容**，后者已把半数池子砍掉。
2. **与策略逻辑冲突**：批2 被剔的 `601398.SH` `601857.SH` `601225.SH` `600028.SH` 是高股息低波央企，正是 `low_beta_inertia` / `crisis_shield` 腿的防御来源；在 3 折窗口里它们"亏损"只是没赶上风格，剔除等于在 OOS 里做风格择时。
3. **归因口径**：亏损额是**已实现交易 PnL**，不含现金腿（`BIL`/`GC001`）与未平仓，不能等同组合拖累。
4. **流程约束**：按 `AGENTS.md` 规则，任何实际剔池必须先写入 [`docs/black_list.md`](file:///home/stone/Work/github/quant/docs/black_list.md) 并记触发类别与量化理由。

**仅建议做"记录级复核"**（不剪池）：`601857.SH` 与 `600584.SH` 在**两批、多策略**中一致亏损，符合"多策略失败"判据，可进入 `black_list.md` 的观察名单；`600900.SH` 仅批1 亏损、批2 反而被保留，不构成稳定证据。

---

## 8. 与既有文档的关系（引用纪律）

| 文档 | 裁定 |
|---|---|
| [`docs/audit/blend_17_baseline_审计.md`](file:///home/stone/Work/github/quant/docs/audit/blend_17_baseline_审计.md)（2026-10-21） | 其折级数字对应 **pre-`253189e` 旧配置**，产物已于 2026-10-23 21:36–21:38 被就地覆盖 → **不可逐位比对**（其四态盾 1.473 vs 本批 1.454）。其"DSR 是致命伤"的表述机制上正确，但须按 §3.2 重述为"零假设误指定的算术产物"。 |
| [`docs/audit/chan_dual_hybrid_deep_analysis_cn.md`](file:///home/stone/Work/github/quant/docs/audit/chan_dual_hybrid_deep_analysis_cn.md)（2026-10-09） | 审的是 `pipeline/results/blend/`、**22 只**池、**17 期**，与本目录（20 只、15+3 期）**不同源**；其"DSR 0.321 / A+ 91 分可上实盘"**不得**用于为本目录背书。 |
| `docs/version.md` §16.15.8 | 池出处（仓外文件、`sha256 ed48e922…`、目录名 ≠ `pruned_core_satellite_blend_17.txt`）**仍成立**。 |
| `docs/version.md` §16.15.9 / §16.15.13 | 换手三口径（双边 16.36× / 池化单边 8.18×）、摩擦 ≈2.02%/年、再平衡中位 2.0 交易日 → **引用本报告任何收益时必须并列**。 |
| `docs/version.md` §16.15.5 / §16.15.14 | "无法绑定库状态"**已被本次 PROVENANCE 部分取代**（db/git/dump 可验），但宇宙哈希仍缺。 |
| `docs/version.md` §16.14 | "无残余损伤"**已作废**（仅对 21:26–21:38 成立）。 |
| `AGENTS.md` `resolve_param` 陷阱 | 修复仍生效：`chan_risk_managed_blend.py:56-61` 用 `crb_wq_alpha_id`、`chan_four_state_blend.py:72-77` 用 `cfsb_wq_alpha_id`。`chan_crisis_shield_blend` 的 dump 里 `chan_composite → WorldQuantAlphaStrategy` 是**设计意图**（盾 = Alpha#12），但键名 `chan_composite` **具误导性**，建议后续改名。 |
| `AGENTS.md` 部署例外 | `chan_four_state_blend` 的 `blend_weighting_mode="regime_ic_ir_hybrid"` 是**部署模式**，**不得**"归一化"回 `rolling_ic_ir`；其余 5 只为 `rolling_ic_ir`。六只均开启 `orthogonalization / structural_regime / cyq_quality_gate / adaptive_stop_atr / low_beta_inertia`、`max_assets_per_sector=2`、`cash_proxy="BIL"`。 |

**六策略构成速查**（`base_version` 均 1.6.0）：
`crisis_shield`（three_type 45 / vaa 35 / WQ#12 20，v1.0.1）、`risk_managed`（three_type 45 / vaa 35 / composite 20，v1.1.0）、`four_state_shield`（35/25/four_state 20/WQ#12 20，v1.0.0）、`four_state`（four_state_exec 25 / three_type 45 / vaa 30，v1.2.1）、`dual_hybrid`（45/35/Alpha#53 10/#3 10/#41 5/#101 5，v1.0.0）、`generic`（wq6 25 / wq41 25 / gtja16 50，v1.0.0）。

---

## 9. 结论与下一步

### 9.1 可用性判定

**条件性可用（Grade B）**：折级 alpha 在 3.9 年里对基准有 14–24pp/年的链式超额，10–11/15 折稳定跑赢，未见前视/集中/预热类硬伤。但**统计上仅边际显著（t≈2.1–2.6）、regime 高度集中、族内不可分辨**，不足以支撑"选第一名上实盘"，只足以支撑"选一族、控风险、小仓位验证"。

### 9.2 五条硬约束（实盘前）

1. **报数纪律**：对外只报链式 CAGR 与**组合级连续权益回撤**；禁止再用 `mean_cagr` / `mean_maxdd` / `mean_calmar` / `DSR` 作业绩或显著性证据。
2. **选型纪律**：5 个 Chan blend 视作**同一策略族**（折相关 0.92），按 OOS + 摩擦保留 **1 只**（本批证据指向 Risk-Managed），其余作冗余/对照。
3. **风险纪律**：单票 20% 上限、行业 ≤2 只、闲置现金 26–44% 是收益构成的一部分，实盘须与 `target_vol 0.12–0.18` 对齐，不能把现金腿当"浪费"。
4. **摩擦纪律**：批1 换手 3.1–4.1×/折、摩擦占净利 10–16%，可接受；批2 Dual Hybrid 摩擦 125.8% + 单票依赖 → **该配置不得实盘**。
5. **归因纪律**：批2 的 3 折只作"未崩"证据，不作排名与单票归因证据。

### 9.3 必须先补的阻塞项

| 编号 | 事项 | 验收 |
|---|---|---|
| A1 | **provenance 记 `555dbdd`**（而非 1db85fd），并把 dirty diff 的最终形态固化；建议在 PROVENANCE 里同时记 `run_config_end_dates` | 重跑能字节级复现 `batch_summary.json` 的 `config.start/end` |
| A2 | **宇宙文件入 provenance**：记 `universe_file` + `universe_sha256`（`ed48e922…`） | 篡改池文件后 `verify_provenance` 失败 + 一条离线测试 |
| A3 | **dump 冻结时刻**：为 `--dump-strategies` 输出加 `generated_at` 字段（dumps 在 gitignore 里，只能靠自记时间） | 能证明 6 份参数早于 2025-11-28 |
| A4 | **连续权益口径**：给 walkforward 增 `--continuous-equity` 输出（跨折连乘/连续持仓），并同步 `SCHEMAS.md` | 能直接读出组合级 CAGR / MaxDD，不再依赖折均值 |
| A5 | **基准同口径**（已登记为 `development_plan.md` 的 **`L2.47`**）：对基准也施加 `min_shares/china_trading/min_weight_change` 的敏感性版本，给出 `outperformance` 区间 | 报告能写"超额 ∈ [x, y] pp/年" |

> 上述 A1–A5 属工程任务，须登记到 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md)（SSOT），本报告不擅自改该文件。

### 9.4 复现命令

```bash
# 审计（严格限定目录）
python3 pipeline/.agents/skills/walkforward-audit/scripts/run_audit.py \
  --results-dir pipeline/results/blend_17_baseline/1 --all --deep-top-n 3
python3 pipeline/.agents/skills/walkforward-audit/scripts/run_audit.py \
  --results-dir pipeline/results/blend_17_baseline/2 --all --deep-top-n 3

# 复跑两批（须在 555dbdd 上）
git checkout 555dbdd
bash pipeline/scripts/backtest_blend_strategies.sh   # 参数见 PROVENANCE.json
```

---

## 10. 附录：本次审计产物

- 脚本原始输出：`/tmp/opencode/blend_17_audit/`（`b1_*` / `b2_*`：summary、audit、rank、deep、overfit、losers、all，共 1,260 行）
- provenance 核查日志：`/tmp/opencode/blend_17_audit/verify.txt`
- 本报告不覆盖任何既有文件；2026-10-21 的旧审计保持原样（其数字不可与本批比对，原因见 §8）。

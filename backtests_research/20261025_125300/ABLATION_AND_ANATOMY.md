# 消融实验、standard 模式与全景解剖复核报告

**目标目录**：`/home/stone/Work/github/quant/backtests_research/20261025_125300/`
**审计范围**：`01_r1_longwin`、`02_r2a_std`、`03_r2b_oos`、`04_r1_std_cb_compare`、`05_anatomy`、`06_ablate`
**不包含**：`FORENSIC_AUDIT.md`（另一 worker 的 walkforward 取证审计）、过拟合评分、剔除标的建议
**可复现脚本与中间产物**：`/tmp/opencode/ablation_work/`
　— `analyze.py`（主脚本）、`xcheck.py`（交叉校验）、`integrity.py`（清单/哈希校验）
　— `ablation_metrics.csv`、`identical_arms.csv`、`config_landing.csv`、`standard_vs_baseline.csv`、`walkforward_phases.csv`、`manifests.csv`、`log_scan.csv`、`dump_versions.csv`、`run_output.txt`

---

## 1. 方法与目录清点

### 1.1 指标口径（全部照抄产码，未自定义）

| 指标 | 公式 | 出处 |
| :--- | :--- | :--- |
| Total Return | `eq[-1]/eq[0] - 1` | `common/allocation_backtester.py` |
| CAGR | `(1+TR)^(1/n_years) - 1`，其中 `n_years = (last_date - first_date).days / 365.25` | 同上（**日历天数**口径，非交易日数） |
| Sharpe | `mean(r_t) / std(r_t, ddof=1) * sqrt(252)` | 同上 |
| MaxDD | `max((cummax(eq) - eq) / cummax(eq))`（正数幅度） | 同上 |
| Calmar | `CAGR / MaxDD` | 同上 |
| Alpha | `(mean(r_s) - β·mean(r_b)) * 252`（CAPM 式，**不是** `mean(r_s - r_b)*252`） | `common/metrics.py::alpha_beta` |
| Beta | `Cov(r_s, r_b, ddof=1) / Var(r_b, ddof=1)` | 同上 |
| Tracking Error | `std(r_s - r_b, ddof=1) * sqrt(252)` | `common/metrics.py::tracking_error` |
| Information Ratio | `mean(r_s - r_b)/std(r_s - r_b, ddof=1) * sqrt(252)` | `common/metrics.py::information_ratio` |
| 单次换手 | (Σ abs(trade_value)) / portfolio_equity（`portfolio_equity` 为调仓前权益） | `common/allocation_backtester.py` 内 `total_turnover` 累加式 |

**无风险利率**：全流程未引入任何无风险利率来源，`risk_free` 取默认值 `0.0`（`sharpe_ratio`/`alpha_beta` 均为 `risk_free: float = 0.0`，`run_backtest.py` 调用时不传该参数）。本报告所有 Sharpe / Alpha 均按 **rf = 0** 计算。

> [!CAUTION]
> **口径陷阱（2026-10-25 补记）：`walkforward_summary.json` 逐折行里的 `baseline_*` 不是策略基线，禁止用它算 Δ。**
> 臂目录内 `rolling_window_performance[*]` 带 `baseline_cagr` / `baseline_sharpe_ratio` / `baseline_max_drawdown` 三个字段；它们描述的是 **`000300.SH` 的 `equal_weight` 基准**（`comparison_report.json` 的 `baseline_symbol` / `baseline_template` 可证），因而**在同一折上所有策略、所有臂取值完全相同**（实测 39 个叶 × 18 折均一致，首折 `baseline_sharpe_ratio = −3.0299`），**不是**该策略 `08_ablate_wf/baseline/<strategy>/` 的 `mean_cagr`。两者偏差：**逐折最大 147–192pp**（cfsb −192.1pp / crb −162.3pp / cdhb −147.5pp，均出现在 `2023-01-17` 折），**折均值口径 19.6–24.1pp**。⇒ **本报告的 Δ 一律以 `08_ablate_wf/baseline/<strategy>/` 为基准计算，绝不使用臂目录内的 `baseline_*` 列。**另注意与本文件 §1.2 校验项里 `comparison_report.json` 的 `baseline_cagr` 区分开：后者在 standard 模式下确为同策略 equal-weight 基准，用法无误。

### 1.2 自算与产码的一致性校验（可证伪点）

| 校验项 | 结果 |
| :--- | :--- |
| 14 个 `comparison_report.json` 的 `alpha/beta/tracking_error/information_ratio/overlap_bars` 与我从 equity CSV 的独立重算 | 最大绝对偏差 **1.4e-15**（浮点级） |
| `strategy_cagr` / `strategy_sharpe_ratio` 与重算 | 偏差 0 / 8.9e-15 |
| `outperformance_cagr` 与 `own_cagr - baseline_cagr` 重算 | 偏差 1.1e-16 |
| 39 个 06 臂的 `Total Turnover`（日志 2 位小数打印）与我从 `rebalance_report.csv` 重算 | 39/39 在打印精度内一致 |

结论：本报告的算术链与产出方代码同源，**没有一个数字来自估算或外推**。

### 1.3 实际目录清点（`find`/`ls` 实枚举，非按任务书推测）

| 阶段 | 叶目录数 | 策略 / 臂 |
| :--- | ---: | :--- |
| `01_r1_longwin` | 6 | `chan_crisis_shield_blend`、`chan_dual_hybrid_blend`、`chan_four_state_blend`、`chan_four_state_shield_blend`、`chan_risk_managed_blend`、`generic_blend`（walkforward） |
| `02_r2a_std` | 3 | `chan_four_state_blend`、`chan_risk_managed_blend`、`generic_blend`（standard） |
| `03_r2b_oos` | 6 | 与 01 同集合（walkforward OOS） |
| `04_r1_std_cb_compare` | 2 | 臂 `baseline` / `no_circuit`，**仅** `chan_four_state_blend`（standard） |
| `05_anatomy` | 6 | 与 01 同集合（standard） |
| `06_ablate` | 39 | 13 臂 × 3 策略（standard） |
| **合计** | **62** | **50 standard**（02:3 + 04:2 + 05:6 + 06:39）+ **12 walkforward**（01:6 + 03:6） |

`06_ablate` 的 13 个臂（实枚举）：`baseline`、`no_voltarget`、`no_orthogon`、`no_structregime`、`no_cyq`、`no_atrstop`、`no_lowbeta`、`no_sectorthrot`、`no_hysteresis`、`no_thrust`、`no_inertia`、`bull_full`、`no_circuit`，每个臂下恰有 3 个策略目录（`chan_four_state_blend` / cfsb、`chan_risk_managed_blend` / crb、`chan_dual_hybrid_blend` / cdhb），**13 × 3 = 39 个叶，无缺臂无缺策略**。

需注意的任务书与实况差异：`chan_four_state_shield_blend`、`chan_crisis_shield_blend`、`generic_blend` **只出现在 01/03/05**（各 6 个叶），**从未进入 06 消融**，因此本报告的消融结论仅覆盖上述 3 个策略，另 3 个策略的因子贡献在本批数据中 `缺失`（不可推断）。

`06_ablate` 的 runner（`scripts/run_group6_ablation_arms_8_13.py`）明确的统一条件：universe = `universe_20_stocks.txt` 的 20 只 A 股，`--start 2022-01-04 --end 2026-10-22 --mode standard --china-trading --min-shares 100 --cash-sweep-rate 0.015 --data-provider fuyao --n-jobs 1`；基准 = `000300.SH`（`equal_weight`，`rebalance_freq_days=5`）。6 个阶段的 `strategy_version/base_version` 完全一致（cfsb `1.2.1/1.6.0`、crb `1.1.0/1.6.0`、cdhb `1.0.0/1.6.0`）。

---

## 2. 消融矩阵（06_ablate，2022-01-04 → 2026-10-22，1,161 根）

每个单元格的 Δ 均相对**同策略的 `baseline` 臂**，同窗口、同成本模型、同 universe，因此差分是干净的一阶响应。

| 臂 | 策略 | Total Ret | CAGR | Sharpe | MaxDD | Calmar | 成交调仓次数 | Σ换手 | 均换手/次 | ΔCAGR | ΔSharpe | ΔMaxDD | 与 baseline 最大绝对差 |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 四态 | 188.98% | 24.76% | 1.615 | 10.70% | 2.31 | 462 | 62.44 | 13.52% | — | — | — | — |
| baseline | 风险 | 224.71% | 27.83% | 1.789 | 9.54% | 2.92 | 480 | 62.55 | 13.03% | — | — | — | — |
| baseline | 双重 | 116.41% | 17.46% | 1.257 | 11.81% | 1.48 | 503 | 70.31 | 13.98% | — | — | — | — |
| no_voltarget | 四态 | 206.14% | 26.27% | 1.671 | 11.62% | 2.26 | 456 | 63.68 | 13.96% | +1.51pp | +0.056 | +0.92pp | 20,356 |
| no_voltarget | 风险 | 241.94% | 29.22% | 1.804 | 10.20% | 2.86 | 477 | 63.49 | 13.31% | +1.39pp | +0.015 | +0.66pp | 22,504 |
| no_voltarget | 双重 | 126.70% | 18.60% | 1.349 | 11.83% | 1.57 | 497 | 71.09 | 14.30% | +1.14pp | +0.091 | +0.02pp | 12,042 |
| no_orthogon | 四态 | 199.73% | 25.71% | 1.713 | 10.27% | 2.50 | 446 | 60.40 | 13.54% | +0.95pp | +0.098 | −0.42pp | 18,103 |
| no_orthogon | 风险 | 181.72% | 24.10% | 1.595 | 10.88% | 2.22 | 427 | 56.43 | 13.21% | **−3.73pp** | **−0.193** | +1.34pp | 48,937 |
| no_orthogon | 双重 | 131.29% | 19.10% | 1.380 | 11.08% | 1.72 | 507 | 67.99 | 13.41% | +1.64pp | +0.123 | −0.72pp | 19,588 |
| no_structregime | 四态 | 252.80% | 30.06% | 1.860 | 9.59% | 3.13 | 455 | 62.88 | 13.82% | **+5.30pp** | **+0.245** | −1.11pp | 66,414 |
| no_structregime | 风险 | 224.71% | 27.83% | 1.789 | 9.54% | 2.92 | 480 | 62.55 | 13.03% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_structregime | 双重 | 116.41% | 17.46% | 1.257 | 11.81% | 1.48 | 503 | 70.31 | 13.98% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_cyq | 四态 | 183.99% | 24.31% | 1.515 | 16.76% | 1.45 | 443 | 58.64 | 13.24% | −0.45pp | −0.100 | **+6.06pp** | 27,663 |
| no_cyq | 风险 | 216.63% | 27.16% | 1.699 | 13.29% | 2.04 | 474 | 58.24 | 12.29% | −0.67pp | −0.089 | **+3.75pp** | 17,924 |
| no_cyq | 双重 | 130.50% | 19.02% | 1.307 | 11.33% | 1.68 | 499 | 64.74 | 12.97% | +1.56pp | +0.050 | −0.48pp | 18,772 |
| no_atrstop | 四态 | 218.06% | 27.28% | 1.768 | 9.36% | 2.92 | 468 | 63.27 | 13.52% | +2.52pp | +0.153 | −1.34pp | 32,945 |
| no_atrstop | 风险 | 236.83% | 28.81% | 1.816 | 9.65% | 2.99 | 483 | 62.79 | 13.00% | +0.98pp | +0.028 | +0.11pp | 29,165 |
| no_atrstop | 双重 | 125.51% | 18.47% | 1.369 | 10.87% | 1.70 | 493 | 68.38 | 13.87% | +1.01pp | +0.112 | −0.94pp | 15,338 |
| no_lowbeta | 四态 | 188.95% | 24.76% | 1.615 | 10.70% | 2.31 | 462 | 62.44 | 13.52% | −0.003pp | −0.000 | 0.00pp | 48（相对 1.6e-4）≈ |
| no_lowbeta | 风险 | 225.99% | 27.94% | 1.791 | 9.54% | 2.93 | 473 | 62.88 | 13.29% | +0.10pp | +0.003 | 0.00pp | 8,391 |
| no_lowbeta | 双重 | 116.41% | 17.46% | 1.257 | 11.81% | 1.48 | 503 | 70.31 | 13.98% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_sectorthrot | 四态 | 184.80% | 24.38% | 1.603 | 12.08% | 2.02 | 466 | 62.47 | 13.41% | −0.38pp | −0.012 | +1.38pp | 6,033 |
| no_sectorthrot | 风险 | 226.09% | 27.94% | 1.805 | 11.93% | 2.34 | 483 | 63.48 | 13.14% | +0.11pp | +0.016 | **+2.39pp** | 8,466 |
| no_sectorthrot | 双重 | 120.31% | 17.90% | 1.281 | 10.78% | 1.66 | 492 | 68.61 | 13.95% | +0.44pp | +0.024 | −1.03pp | 5,995 |
| no_hysteresis | 四态 | 190.99% | 24.94% | 1.624 | 10.52% | 2.37 | 460 | 62.96 | 13.69% | +0.18pp | +0.009 | −0.18pp | 2,156 |
| no_hysteresis | 风险 | 224.71% | 27.83% | 1.789 | 9.54% | 2.92 | 480 | 62.55 | 13.03% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_hysteresis | 双重 | 116.41% | 17.46% | 1.257 | 11.81% | 1.48 | 503 | 70.31 | 13.98% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_thrust | 四态 | 190.53% | 24.90% | 1.760 | 9.98% | 2.50 | 371 | 40.20 | 10.84% | +0.14pp | +0.145 | −0.72pp | 21,876 |
| no_thrust | 风险 | 146.06% | 20.65% | 1.662 | 7.23% | 2.85 | 365 | 35.97 | 9.86% | **−7.18pp** | −0.127 | −2.31pp | 82,947 |
| no_thrust | 双重 | 135.09% | 19.51% | 1.626 | 9.07% | 2.15 | 402 | 46.98 | 11.69% | +2.05pp | **+0.369** | −2.74pp | 18,691 |
| no_inertia | 四态 | 159.83% | 22.03% | 1.555 | 11.58% | 1.90 | 893 | 112.10 | 12.55% | −2.73pp | −0.060 | +0.89pp | 44,517 |
| no_inertia | 风险 | 182.02% | 24.13% | 1.683 | 11.21% | 2.15 | 947 | 118.90 | 12.56% | −3.70pp | −0.105 | +1.67pp | 74,297 |
| no_inertia | 双重 | 90.97% | 14.44% | 1.125 | 9.71% | 1.49 | 917 | 125.91 | 13.73% | −3.02pp | −0.132 | −2.09pp | 42,847 |
| bull_full | 四态 | 202.39% | 25.95% | 1.540 | 14.31% | 1.81 | 502 | 79.63 | 15.86% | +1.19pp | −0.075 | **+3.61pp** | 18,908 |
| bull_full | 风险 | 195.13% | 25.31% | 1.539 | 12.35% | 2.05 | 533 | 83.98 | 15.76% | −2.52pp | **−0.250** | +2.80pp | 49,466 |
| bull_full | 双重 | 145.74% | 20.62% | 1.378 | 13.57% | 1.52 | 575 | 89.65 | 15.59% | +3.15pp | +0.121 | +1.77pp | 38,759 |
| no_circuit | 四态 | 188.98% | 24.76% | 1.615 | 10.70% | 2.31 | 462 | 62.44 | 13.52% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_circuit | 风险 | 224.71% | 27.83% | 1.789 | 9.54% | 2.92 | 480 | 62.55 | 13.03% | ≡ 0 | ≡ 0 | ≡ 0 | **0** |
| no_circuit | 双重 | 116.44% | 17.47% | 1.258 | 11.80% | 1.48 | 503 | 70.30 | 13.98% | +0.004pp | +0.0003 | −0.008pp | 43（相对 2.6e-4）≈ |

> [!WARNING]
> **勘误（2026-10-25）：本表 `no_atrstop` 三行的归属主体是「自适应止损乘数」，不是「ATR 跟踪止损」。**
>
> 上表三行的**数字与 Δ 全部不变**（+2.52 / +0.98 / +1.01pp 等逐字保留），变的只是它们所测的对象：`no_atrstop` 臂的唯一 override 是 `{"blend_enable_adaptive_stop_atr": false}`，`*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`，**止损本身从未被移除**（机制与三份产物证据见 §3.4 勘误与 §7.1 勘误）。因此三行只能读作「把自适应乘数固定为常数 4.5× 后的响应」，**不得**读作「关掉 ATR 止损后的响应」。本臂**非 ≡** 基线（§3.2 的 ✓/✓/✓）这一判定不受影响，仍然成立——固定乘数确实改变了止损价格。

### 2.1 换手口径的一个重要区分（否则会误读上表"次数"）

`rebalance_report.csv` 里出现的 `rebalance_id` 个数 = **真正成交过的调仓次数**（上表列）；而 `run.log` 的 `Total Rebalances` = 策略**发出指令的**调仓次数，两者系统性不等：

| 臂/策略（摘录） | 成交次数 | 指令次数 | 空转指令 | 空转占比 |
| :--- | ---: | ---: | ---: | ---: |
| baseline / 四态 | 462 | 547 | 85 | 15.5% |
| baseline / 风险 | 480 | 546 | 66 | 12.1% |
| baseline / 双重 | 503 | 630 | 127 | 20.2% |
| no_inertia / 四态 | 893 | 982 | 89 | 9.1% |
| no_thrust / 风险 | 365 | 413 | 48 | 11.6% |

39 个臂的空转率均值 **15.6%**（区间 8.1%–22.2%），即约每 6–7 次调仓指令有 1 次因惯性/换手阈值/入场确认门槛而完全不动。这本身是惯性过滤有效工作的证据，但也意味着"调仓频率"若只看日志会高估真实交易频率 15%–22%。

### 2.2 噪声下限（用于判断 Δ 是否可解释）

`01_r1_longwin` 的 4 个折（2 年窗、0.25 年步长、跨 2022-01-04→2024-11-14，**窗口高度重叠、并非 4 个独立样本**）给出的同策略离散度：

| 策略 | 折 CAGR 区间 | 折 CAGR 极差 | 折 Sharpe 标准差 | Calmar 极差 |
| :--- | :--- | ---: | ---: | ---: |
| 四态 | 14.49% → 37.75% | 23.3pp | 0.568 | 1.36 → 3.97 |
| 风险 | 17.33% → 38.60% | 21.3pp | 0.467 | — |
| 双重 | 12.76% → 32.35% | 19.6pp | 0.479 | — |

**判读尺度**：同一策略仅换评估窗口就能产生 **±10pp 量级的 CAGR** 与 **±0.3–0.5 的 Sharpe** 漂移。因此 06 消融中 **|ΔCAGR| < 3pp 且 |ΔSharpe| < 0.15 的臂，其符号与量级都不应被当作"因子的贡献"**，只能当作"在该单一实现路径上不可分辨"。这是本报告后续所有"可信/不可信"判定的量化依据，而非主观判断。

---

## 3. 空操作 / 静默失效矩阵

### 3.1 判定方法与符号

对每个 (臂, 策略) 取 `backtest_equity.csv` 权益序列与同策略 `baseline` 臂序列，按索引**交集对齐**（39 个臂全部 1,161/1,161 完全对齐，无日期缺失），计算权益序列的**绝对差** `max |Δeq|` 与**相对差**：

- **≡ 逐位相同**：`max |Δeq| < 1e-9`（严格 0 到浮点不可分）
- **≈ 数值惰性**：`max |Δeq| > 1e-9` 但相对差 `< 1e-3`（有变化，但在 4 位有效数字内不可读）
- **✓ 有响应**：相对差 ≥ 1e-3

### 3.2 矩阵

| 臂 \ 策略 | 四态 (cfsb) | 风险 (crb) | 双重 (cdhb) |
| :--- | :---: | :---: | :---: |
| no_voltarget | ✓ | ✓ | ✓ |
| no_orthogon | ✓ | ✓（大幅：ΔCAGR −3.73pp） | ✓ |
| **no_structregime** | ✓（+5.30pp） | **≡** | **≡** |
| no_cyq | ✓ | ✓ | ✓ |
| no_atrstop | ✓ | ✓ | ✓ |
| **no_lowbeta** | **≈**（1.6e-4） | ✓（但仅 +0.10pp，实质惰性） | **≡** |
| no_sectorthrot | ✓ | ✓ | ✓ |
| **no_hysteresis** | ✓（+0.18pp 微弱） | **≡** | **≡** |
| no_thrust | ✓ | ✓（−7.18pp） | ✓ |
| no_inertia | ✓ | ✓ | ✓ |
| bull_full | ✓ | ✓ | ✓ |
| **no_circuit** | **≡** | **≡** | **≈**（2.6e-4） |

**≡ 全部清单（5/36 个非基线臂，占 13.9%）**：

1. `no_structregime / chan_risk_managed_blend`
2. `no_structregime / chan_dual_hybrid_blend`
3. `no_lowbeta / chan_dual_hybrid_blend`
4. `no_hysteresis / chan_risk_managed_blend`
5. `no_hysteresis / chan_dual_hybrid_blend`
6. `no_circuit / chan_four_state_blend`
7. `no_circuit / chan_risk_managed_blend`

（加 ≈ 的 2 个：`no_lowbeta/四态`、`no_circuit/双重`。）

### 3.3 每个静默臂的**根因**（区分"结构不可达"与"基线本已关闭"，二者含义完全不同）

| 静默臂 | 根因 | 证据等级 | 结论 |
| :--- | :--- | :--- | :--- |
| `no_structregime` / crb, cdhb | **结构性不可达**。`enable_structural_regime` 只被 `blend_base.py:468` 使用，而该行位于 `weighting_mode in ("regime_adaptive", "regime_ic_ir_hybrid")` 分支内（`blend_base.py:464`）。crb/cdhb 的 `blend_weighting_mode = "rolling_ic_ir"`，永不进入该分支；`blend_base.py:769` 的另一处只控制 fractal-dimension 预计算，其产物 `median_d` 同样只喂给该分支。 | **代码级确证** + 逐位相同 | 任务书预测**成立**。该臂对 crb/cdhb **零信息量**，其"无影响"绝不能表述为"结构性状态机无效"。 |
| `no_hysteresis` / crb, cdhb | **基线本已关闭，非不可达**。`blend_enable_regime_hysteresis` 的 `StrategyConfig` 默认值为 `False`（`rs/config.py:794`），crb/cdhb 的基线 params 里**根本没有这个键**（cfsb 有且为 `True`）。叠加 `blend_base.py:979` 的 `if enable_regime_hysteresis:` → False 走 legacy 单阈值路径。把 False 再设成 False 必然逐位相同。 | **代码级确证** | 任务书预测"结果成立、理由不成立"：机制本身对 crb/cdhb **可达**（不受 weighting_mode 限制），只是基线从未开启。**该臂不能用来评价 hysteresis**，正确表述是"crb/cdhb 在基线配置下本就没有启用 hysteresis"。 |
| `no_lowbeta` / cdhb | `enable_low_beta_inertia` 的唯一使用点在 `blend_base.py:1295`（选股/换仓分支内部），该分支在 cdhb 路径下要么未触发、要么其产生的 delta 被后续过滤吸收。 | 实测（**未定位到具体分支**） | 任务书未预测到这一条，属**新增发现**。 |
| `no_circuit` / cfsb, crb | 三档阈值被设为 `10.0` 的**不可达哨兵**，而基线结果逐位不变 → 三档回撤熔断器在该窗口**从未实际降杠杆**。 | 实测（且被 `04` 独立 A/B 复证，见 3.5） | 任务书预测方向成立。**但存在反直觉点**：基线报告的 MaxDD 已达 **10.70%**（四态）/ **9.54%**（风险），已越过 `dd_reduce_thresh = 0.10` 一档。对此的唯一诚实表述是"可观测到的回撤确实超过 10%，但熔断分支没有改变任何持仓"——可能机制是在 `dd_mag ≥ dd_reduce_thresh` 且 `fast_recovery`（`thrust_active or r_10d>0 or breadth≥0.60`）为真时，`blend_base.py:1081` 走 `raw_w = raw_blend; deleverage_scale = 1.0` 的**旁路**，也可能是 `dd_mag` 基于内部 `cum_nav` 与报告权益曲线的峰谷不同步。**本报告不宣称已定位根因**（需单独插桩才能定论），仅确证"该功能在本窗口贡献恒为 0"。 |
| `no_circuit` / cdhb | 同上，但差值 43.3（相对 2.6e-4）。cdhb 的 `elastic_drawdown_reset` 默认为 `True`（`blend_base.py:622` 的 `param_prefix == "cdhb"` 默认式），`blend_base.py:994` 的弹性重置会在 `fast_recovery` 时把 `peak_nav` 直接顶到当前 NAV，这与熔断器构成**互相覆盖**的两套机制。 | 实测 | 实质惰性。 |

### 3.4 配置落地校验（`strategy_dump.json` + `run.log` 双源）

对每个臂，把 `strategy_dump.json` 的 `params` 与 `run.log` 里 `Parameters: {...}` 行（运行期**生效**参数字典）分别与同策略 baseline 做键级 diff，并交叉校验两者是否一致。

**结果：39/39 个臂的 `dump.params` 与 `run.log` 的生效参数完全相等**（`dump_vs_log_agree = True`），无一处"dump 写了但没生效"。

| 臂 | 四态 (cfsb) 落地 diff | 风险 (crb) | 双重 (cdhb) | 落地判定 |
| :--- | :--- | :--- | :--- | :--- |
| no_voltarget | `cfsb_enable_vol_targeting: true→false` | `crb_enable_vol_targeting` | `cdhb_enable_vol_targeting` | ✅ 有效 |
| no_orthogon | `blend_enable_orthogonalization: true→false` | 同 | 同 | ✅ 有效 |
| no_structregime | `blend_enable_structural_regime: true→false` | 同（**但代码路径不可达**） | 同（**不可达**） | ⚠️ 配置落地但机制不可达 |
| no_cyq | `blend_enable_cyq_quality_gate: true→false` | 同 | 同 | ✅ 有效 |
| no_atrstop | `blend_enable_adaptive_stop_atr: true→false` | 同 | 同 | ✅ 落地（**仅切换乘数**：`eff_stop_mult` 由自适应裁剪值退回常数 4.5，`stop_mode` 仍为 `atr_trailing`，止损未被移除——见下方勘误） |
| no_lowbeta | `blend_enable_low_beta_inertia: true→false` | 同 | 同（**行为无变化**） | ✅ 落地 / cdhb 无效 |
| no_sectorthrot | `blend_max_assets_per_sector: 2→20`、`cfsb_max_assets_per_sector: null→20`、`cfsb_adaptive_sector_throttle: null→false` | 同（`crb_*`） | `blend_max_assets_per_sector: 2→20`、`cdhb_max_assets_per_sector: 2→20`、`cdhb_adaptive_sector_throttle: true→false` | ⚠️ cfsb/crb 部分无效，见下 |
| no_hysteresis | `blend_enable_regime_hysteresis: true→false` | `null→false`（**基线本就 False**） | `null→false`（**基线本就 False**） | ⚠️ crb/cdhb 无效 |
| no_thrust | `cfsb_dynamic_cash_deployment: true→false` | `crb_*` | `cdhb_*` | ✅ 有效 |
| no_inertia | `adaptive_inertia true→false` + `min_weight_change 0.05→0.0` + `min_holding_bars 2→0` + `entry_threshold 0.05→0.0` | `0.05→0.0` / `2→0` / `0.03→0.0` | 同 crb | ✅ 有效（**四键齐备**，符合任务书要求） |
| bull_full | `cfsb_target_bull_exposure: 0.8→1.0` | `crb_*` | `cdhb_*` | ✅ 有效 |
| no_circuit | `dd_reduce/defensive/stop_thresh: 0.1/0.15/0.2 → 10.0/10.0/10.0` | 同 | 同 | ✅ 落地（机制未触发） |

**`no_sectorthrot` 的关键缺陷（任务书重点检查项）**：该臂在 cfsb 与 crb 上被 `StrategyConfig` **静默丢弃了一半**：

```
pipeline/research_strategy/rs/config.py:1350: UserWarning: StrategyConfig.from_dict:
  ignoring unknown key(s) ['cfsb_adaptive_sector_throttle', 'cfsb_max_assets_per_sector']
pipeline/research_strategy/rs/config.py:1350: UserWarning: StrategyConfig.from_dict:
  ignoring unknown key(s) ['crb_adaptive_sector_throttle', 'crb_max_assets_per_sector']
```

即 `{cfsb,crb}_adaptive_sector_throttle` 与 `{cfsb,crb}_max_assets_per_sector` **不是** `StrategyConfig` 的已知字段（`cdhb_*` 才是，故双重策略无此警告）。真正生效的只有全局 `blend_max_assets_per_sector: 2→20`。此外 `adaptive_sector_throttle` 的 `StrategyConfig` 默认值本就是 `False`（`rs/config.py:781`），cfsb/crb 基线 params 里既无 prefixed 键也无 `blend_adaptive_sector_throttle`，所以**基线的 sector throttle 本来就是关的**，"关掉它"是第二次空操作。

→ **结论：`no_sectorthrot` 只能解释为"放开行业集中度上限（2→20）"这一个杠杆**；对 cdhb 才是"上限放开 + 自适应节流关闭"的复合杠杆。三者的 Δ 因此**不可横向比较**。

（全库仅此 4 行 `UserWarning`，其余 35 个臂的 override 键均被 `StrategyConfig` 正常识别。）

> [!WARNING]
> **勘误（2026-10-25）：`no_atrstop` 的落地判定「✅ 有效」原被读作「止损被关掉」——它只关掉了自适应乘数。**
>
> **原文（保留原样）**：上表该行原写作「`no_atrstop` | `blend_enable_adaptive_stop_atr: true→false` | 同 | 同 | **✅ 有效**」——**override 字符串本身是对的**（本表从未写错这个键），错的是判定栏的语义：它被后续章节（§2 表、§7.1）当成了「ATR 止损已移除」的依据。
>
> **为什么错**：`enable_adaptive_stop_atr` 在 `pipeline/research/blend/blend_base.py` 全文件只出现两处——`:612`（`resolve_param` 读取）与 `:892`（`if enable_adaptive_stop_atr:` 分支）。止损块本身由 `:875` 的 `if stop_mode in ("atr_trailing", "fixed_pct")` 守卫、由 `:914` 的 `if curr_p < trailing_stop_p:` 触发，与这个开关无关。开关为 `False` 时唯一的差别是 `:891` 的 `eff_stop_mult = stop_atr_mult`（= 4.5）不再被 `clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5)` 覆盖（`:892–896`），`:897` 的 `trailing_stop_p = max(asset_entry_price[s] * 0.92, peak_p - eff_stop_mult * cur_atr)` 照常计算。**该臂测的是「自适应乘数 → 常数 4.5×」。**
>
> **产物证据（6/6 叶：`06_ablate` 与 `08_ablate_wf` 各 3 策略）**：`run_manifest.json` → `cli_overrides` 只含 `{"blend_enable_adaptive_stop_atr": false}`；`strategy_dump.json` → `params` 中 `*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`；`run.log` 第 2 行 `Parameters: {...}` 运行期参数同值（本表前提 `dump_vs_log_agree = True` 已核）。
>
> **臂定义并非新发现**：`docs/backtest_research.md` §8 的 `no_atrstop` 行与其后「语义边界披露」早已写明「关闭后回退至固定 4.5x ATR 追踪止损，**并不是**彻底关闭底层 ATR 止损机制本身（彻底关闭需设置 `"${pfx}_stop_mode": "none"`）」。本报告 §1.3 声明臂定义逐字取自该来源，**却在解释结果时没有回读自己引用的定义**——这是本次误标的直接原因。
>
> **反向证据与仍成立的部分**：真正的「止损关闭」臂是 `09_param_grid/atr/atr_none`（`cdhb_stop_mode = "none"`，18 折、`2022-01-04`→`2026-10-22`），cdhb 上 ΔCAGR **−0.1590pp**、ΔMaxDD **+0.2137pp（更差）**——止损拿掉后收益没升、回撤反而更深。仍然成立的是「把自适应乘数固定为 4.5× 后三策略 CAGR 全部上升」这一**描述性事实**。完整勘误见 §7.1。

### 3.5 `04_r1_std_cb_compare` 专项：一对完全重复的对照组

| 目录 | 窗口 | bars | CAGR | Sharpe | MaxDD |
| :--- | :--- | ---: | ---: | ---: | ---: |
| `baseline/chan_four_state_blend` | 2022-01-04 → 2024-12-31 | 726 | 23.95% | 1.501 | 10.89% |
| `no_circuit/chan_four_state_blend` | 2022-01-04 → 2024-12-31 | 726 | 23.95% | 1.501 | 10.89% |

两条权益曲线 **726/726 根、`max |Δ| = 0.0`**（逐位相同），`comparison_report.json` 的 alpha/beta/TE/IR/outperformance 五位小数全部一致。该阶段**信息量为零**，与 06 中 `no_circuit/四态` 的 ≡ 结论互为独立复证。

附注：该阶段窗口是 **2022-01-04 → 2024-12-31（726 根）**，比 02/05/06 的全窗口（1,161 根）**短 435 根 / 37%**；`backtests_research/INDEX.md` 对该阶段写的是"730 bars"，**与实际 726 根不符**（INDEX 的笔误，非数据问题）。其 `baseline_equity.csv` 经核对 = 全窗口基准序列在 2024-12-31 处的**直接截断**（726/726 逐位相同，未重新归一），因此 04 与 02/05 的基准 CAGR（−7.19% vs −4.03%）差异仅由窗口不同造成，不是数据错误。

---

## 4. standard 模式 vs 基准（`000300.SH`）

### 4.1 基准的同一性（先确认"可比的基准"）

全部 50 条 `baseline_equity.csv` 逐一与 `02_r2a_std/chan_four_state_blend` 的基准做对齐比对：**48 条与全窗口基准逐位相同（`max |Δ| = 0.0`）**；仅 `04_r1_std_cb_compare` 的 2 条是前述的 2024-12-31 截断版（726 根，逐位等于截断序列）。即：基准没有随策略/阶段变化，`000300.SH` 买持在 2022-01-04→2026-10-22 为 **CAGR −4.03%、Sharpe −0.147、MaxDD 35.76%**（起始 99,900.10 → 终止 81,993.83）。

### 4.2 明细表

`overlap` 为我从两条权益 CSV 求索引交集后**实测**的重叠区间；`报告值` 取自 `comparison_report.json`；`重算值` 为独立复算。α/β/TE/IR 的重算与报告值偏差 ≤1.4e-15（见 §1.2），故下表只列一套数。

| 叶目录 | 实测重叠区间 | 重叠 bars | 策略 CAGR | 策略 Sharpe | 策略 MaxDD | 基准 CAGR | 基准 Sharpe | α（年化） | β | TE | IR | 超额 CAGR |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 02 / 四态 | 2022-01-04 → 2026-10-22 | 1,161 | 23.91% | 1.565 | 10.93% | −4.03% | −0.147 | 24.67% | 0.451 | 16.03% | 1.630 | +27.95pp |
| 02 / 风险 | 2022-01-04 → 2026-10-22 | 1,161 | 28.03% | 1.821 | 9.81% | −4.03% | −0.147 | 28.07% | 0.459 | 15.62% | 1.889 | +32.07pp |
| 02 / 通用 | 2022-01-04 → 2026-10-22 | 1,161 | 10.26% | 0.745 | 14.56% | −4.03% | −0.147 | 12.43% | 0.409 | 17.07% | 0.820 | +14.30pp |
| 04 / 四态（熔断开） | 2022-01-04 → **2024-12-31** | **726** | 23.95% | 1.501 | 10.89% | −7.19% | −0.319 | 26.05% | 0.418 | 17.43% | 1.695 | +31.14pp |
| 04 / 四态（熔断关） | 2022-01-04 → **2024-12-31** | **726** | 23.95% | 1.501 | 10.89% | −7.19% | −0.319 | 26.05% | 0.418 | 17.43% | 1.695 | +31.14pp |
| 05 / 危机盾 | 2022-01-04 → 2026-10-22 | 1,161 | 27.30% | 1.670 | 11.98% | −4.03% | −0.147 | 27.84% | 0.537 | 15.02% | 1.935 | +31.33pp |
| 05 / 双重 | 2022-01-04 → 2026-10-22 | 1,161 | 17.44% | 1.254 | 11.83% | −4.03% | −0.147 | 18.92% | 0.438 | 15.53% | 1.314 | +21.47pp |
| 05 / 四态 | 2022-01-04 → 2026-10-22 | 1,161 | 24.60% | 1.607 | 10.82% | −4.03% | −0.147 | 25.24% | 0.450 | 16.01% | 1.668 | +28.64pp |
| 05 / 四态盾 | 2022-01-04 → 2026-10-22 | 1,161 | 22.11% | 1.394 | 11.07% | −4.03% | −0.147 | 23.47% | 0.525 | 15.32% | 1.615 | +26.15pp |
| 05 / 风险 | 2022-01-04 → 2026-10-22 | 1,161 | 27.80% | 1.787 | 9.70% | −4.03% | −0.147 | 27.93% | 0.469 | 15.59% | 1.882 | +31.83pp |
| 05 / 通用 | 2022-01-04 → 2026-10-22 | 1,161 | 10.42% | 0.755 | 14.46% | −4.03% | −0.147 | 12.57% | 0.409 | 17.05% | 0.829 | +14.45pp |
| 06 基线 / 双重 | 2022-01-04 → 2026-10-22 | 1,161 | 17.46% | 1.257 | 11.81% | −4.03% | −0.147 | 18.93% | 0.438 | 15.51% | 1.317 | +21.50pp |
| 06 基线 / 四态 | 2022-01-04 → 2026-10-22 | 1,161 | 24.76% | 1.615 | 10.70% | −4.03% | −0.147 | 25.38% | 0.452 | 15.99% | 1.678 | +28.80pp |
| 06 基线 / 风险 | 2022-01-04 → 2026-10-22 | 1,161 | 27.83% | 1.789 | 9.54% | −4.03% | −0.147 | 27.96% | 0.470 | 15.58% | 1.885 | +31.87pp |

### 4.3 重叠区间是否"实质性缩短"？

- **02 / 05 / 06 全部 12 个叶：1,161 / 1,161 根，100% 覆盖名义窗口 2022-01-04 → 2026-10-22，无截断。** ✅
- **04 的两个叶：726 根，只覆盖到 2024-12-31（名义窗口的 62.5%），实质缩短 435 根。** ⚠️ 04 的任何 headline 数字都**不可**与 02/05/06 直接并列比较（其基准 CAGR −7.19% vs 全窗口 −4.03% 即为此所致）。
- 策略侧与基准侧的 bars 数逐叶相等（`own_bars == base_bars`），不存在"策略侧缺尾部、基准侧补齐"的隐性错位。

### 4.4 α/β 的读法

14 个叶的 β 全部落在 **0.409–0.537**，α 全部为正且 ≥12.4%/年。β≈0.45 说明策略在 000300 下跌 4.03% 的窗口里只承担约 45% 的指数暴露——这是 20 只 A 股 + 现金替代（`cash_proxy=BIL`）+ 目标波动 12% 的**结构性**结果，不是择时能力。**因此"α 24.67%"不能读作"相对于中国市场的纯选股超额"**：基准是单一指数买持，而策略既可持现金又可跨 20 只股票，两者的 β < 1 已内生于构造。TE 15%–17%、IR 1.3–1.9 才是更可比的"信息效率"读数，且同样只基于**一条 4.8 年路径**（见 §2.2 噪声下限）。

---

## 5. 跨阶段一致性

### 5.1 同一策略的四个来源对照

| 策略 | 01 长窗 WF 均值<br>(2022-01-04→2024-11-14) | 02 连续 standard<br>(1,161 根) | 03 OOS WF 均值<br>(2025-01-02→2026-07-27) | 05 全景解剖<br>(1,161 根) | 06 基线<br>(1,161 根) | 04 (726 根)<br>2022-01-04→2024-12-31 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| 四态 | 30.78% / 1.817 / 9.86% | 23.91% / 1.565 / 10.93% | 23.79% / 1.814 / 4.47% | 24.60% / 1.607 / 10.82% | 24.76% / 1.615 / 10.70% | 23.95% / 1.501 / 10.89% |
| 风险 | 31.39% / 1.855 / 9.29% | 28.03% / 1.821 / 9.81% | 34.96% / 2.296 / 4.40% | 27.80% / 1.787 / 9.70% | 27.83% / 1.789 / 9.54% | 缺失 |
| 双重 | 25.31% / 1.662 / 8.84% | **缺失** | 26.15% / 2.566 / 4.00% | 17.44% / 1.254 / 11.83% | 17.46% / 1.257 / 11.81% | 缺失 |
| 四态盾 | 30.34% / 1.833 / 10.69% | 缺失 | 15.73% / 1.558 / 4.74% | 22.11% / 1.394 / 11.07% | 缺失 | 缺失 |
| 危机盾 | 27.80% / 1.714 / 10.41% | 缺失 | 21.83% / 1.936 / 4.28% | 27.30% / 1.670 / 11.98% | 缺失 | 缺失 |
| 通用 | 13.87% / 0.955 / 14.83% | 10.26% / 0.745 / 14.56% | 0.16% / 0.238 / 6.04% | 10.42% / 0.755 / 14.46% | 缺失 | 缺失 |

（单元格 = CAGR / Sharpe / MaxDD。`缺失` = 该阶段无此策略目录。）

### 5.2 应当在哪儿一致，实际是否一致

**(a) 05 全景解剖 vs 06 消融基线 —— 应几乎相同，实测高度一致 ✅**
两者都是 standard 连续模式、同窗口、同 universe、同 `cash_proxy=BIL`。差异只能来自成本覆盖层：`05` 的 `run.log` 里有 `market impact: 380 (symbol, session) fill(s) ...` 行，`06` 与 `01/02/03` 全部为 **0 条**。实测：

| 策略 | 05 CAGR | 06 基线 CAGR | 差 | 05 Sharpe | 06 基线 Sharpe | 权益曲线最大相对差 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| 四态 | 24.60% | 24.76% | −0.16pp | 1.607 | 1.615 | 1.75e-2 |
| 风险 | 27.80% | 27.83% | −0.03pp | 1.787 | 1.789 | 2.93e-3 |
| 双重 | 17.44% | 17.46% | −0.02pp | 1.254 | 1.257 | 8.44e-3 |

**结论：05 与 06 基线是同一引擎、同一窗口、仅差平方根市场冲击覆盖**，量级（0.02–0.16pp CAGR）完全符合"5% 冲击系数"的预期。两者可互为对方的正确性校验，**不可**当作两个独立实验。

**(b) 02 连续 standard vs 06 基线 —— 差 0.03–0.85pp，方向一致 ✅**
两者的 `run.log` 除输出路径外**只有数字不同，`Parameters:` 行完全一致**（已逐行 diff 确认），即策略参数零差异，分歧只能来自 CLI 成本覆盖层。`06` 的日志与 `02` 一样**没有** `market impact` 行，因此 `06` 相对 `02` 的增益最可能就来自 `INDEX.md` 记的"1.5% 现金计息"（`02` 记为纯"连续 standard"）。实测：四态 23.91% vs 24.76%（−0.85pp）、风险 28.03% vs 27.83%（+0.20pp）。四态的 −0.85pp 大于风险的 +0.20pp，与"四态在该窗口持有更多现金"（其 MaxDD 更低、换手更低）方向一致。**同向、量级合理，不构成矛盾**；但"1.5% 现金计息"这一归因是**依据 INDEX 记录 + 参数零差异的推断**，CLI 未被 manifest 记录（`cli_overrides` 为空），无法从产物直接证实。

**(c) 01 长窗 walkforward vs 02/05/06 连续 standard —— 不应相等，实测差 1.0–7.1pp ⚠️（需解释，非错误）**
01 的"均值"是 **4 个高度重叠的 2 年窗口**（步长 0.25 年，跨 2.86 年）的算术平均，**不是 4 个独立样本**；且其**实际末折终点 2024-11-14**，比 `requested_end=2024-12-31` 提前 47 天。02/05/06 则是**一条覆盖到 2026-10-22 的连续路径**。两者回答的是不同问题，因此：

- 四态 30.78%（01）vs 23.91%（02）：**1.29×**，未触及任务书的 2× 红旗线；
- 风险 31.39%（01）vs 28.03%（02）：1.12×；
- 双重 25.31%（01）vs 17.44%（05）：**1.45×**，是本次最大的一组；
- 通用 13.87%（01）vs 10.26%（02）：1.35×。

01 系统性偏高有明确机制：其折窗口 2022-01→2024-11 恰好避开 2025 年（OOS 里连通用都只有 0.16%、双重 OOS Sharpe 虽高但 CAGR 分布极散）。**01 的均值不能当作"预期年化"，只能当作"2022–2024 区间的窗口敏感性探测"。**

**(d) 03 样本外 walkforward —— 6 个 0.25 年小折，噪声主导 ⚠️**
03 名义 `2025-01-02 → 2026-10-22`，**实际末折终点 2026-07-27（提前 87 天）**；每折约 60 根，折间离散度极端：

| 策略 | 03 均值 CAGR | 折 CAGR 区间 | 折 Sharpe 标准差 | 判读 |
| :--- | ---: | :--- | ---: | :--- |
| 风险 | 34.96% | **−13.95% → +121.10%** | 2.378 | 均值被单折 +121% 主导，**不可引用** |
| 四态 | 23.79% | −13.32% → +80.49% | 1.987 | 折极差 94pp，均值意义有限 |
| 双重 | 26.15% | −0.47% → +44.88% | 1.930 | 折极差 45pp |
| 四态盾 | 15.73% | −16.19% → +51.21% | 2.572 | 同上 |
| 危机盾 | 21.83% | −7.58% → +59.76% | 1.628 | 同上 |
| 通用 | 0.16% | −12.87% → +22.36% | 1.326 | 同上 |

03 的 `deflated_sharpe_ratio` 也直接暴露这一点：四态 9.8e-5、四态盾 1.7e-21、风险 5.1e-4、通用 4.2e-31（仅双重 0.586、危机盾 0.201）—— **DSR 远低于任何可信门槛**。03 的 headline 数字**只能用于排序方向，不能用于量化预期收益**。

### 5.3 我信哪个、为什么

1. **最可信：06 基线 / 05 全景解剖的连续 standard 全窗口数字**（四态 24.76%/1.615、风险 27.83%/1.789、双重 17.46%/1.257）。理由：单一路径无折拼接、1,161 根全覆盖、无窗口截断、成本与 universe 与消融各臂完全同源，且其 α/β/TE/IR 经独立复算逐位吻合。
2. **可信度次之：02 连续 standard 的 3 个策略**（同窗口，但缺 1.5% 现金利息覆盖，故系统性偏低）。
3. **只能作方向参考：01 长窗均值**。4 折重叠、非独立、末折提前 47 天截止。
4. **不可引用绝对值：03 OOS 均值**。6 个 60 根的折、折 CAGR 极差最大 135pp、DSR 低至 1.7e-21。
5. **完全不可用：04 阶段**。两个臂逐位相同，窗口还短 37%。

---

## 6. 数据完整性检查

### 6.1 `run_manifest.json`（62/62 全覆盖）

| 检查项 | 期望 | 实测 | 判定 |
| :--- | :--- | :--- | :--- |
| 每个叶目录都有 `run_manifest.json` | 是 | 62/62 | ✅ |
| `status == "completed"` | 是 | 62/62 `completed`，0 个失败/中断 | ✅ |
| `metrics` 在 standard 模式为 null | 是（产出方设计如此） | 50/50 个 standard 叶 `metrics` 全 null | ✅ 符合预期 |
| `metrics` 在 walkforward 模式已填充 | 是 | 12/12 个 walkforward 叶全部非 null（`n_folds` 4 或 6） | ✅ |
| `artifacts` 不含 `run.log` | 是 | 62/62 不含 | ✅ |
| `db_used == false` | 任务书预期 | **62/62 全部为 `true`** | ⚠️ **系统性偏离清单预期** |
| `strategy_dump.json` 的 sha256 == manifest 记录 | 是 | **62/62 完全吻合** | ✅ |
| `artifacts[].bytes` == 磁盘实际字节 | 是 | 62/62 叶的所有登记产物全部吻合，**无一缺件** | ✅ |
| `run_ts` 一致 | 是 | 62/62 = `20261025_125300` | ✅ |
| `git_commit` 一致且干净 | 是 | 62/62 = `8552c4b1`，但 **`git_dirty = true`（62/62）** | ⚠️ 见 6.4 |

**`db_used` 偏离说明**：这不是失败。这 62 个 run 全部以 `--data-provider fuyao` + `DuckDBCache`（`data/cache.duckdb`，324,022,272 字节）运行，DuckDB 是**唯一数据源**，"使用了本地 DB"是本次实验的既定设计。任务书写的"期望 false"来自另一套（纯 synthetic / CSV 缓存）检查清单，在此目录下**不适用**。**我把它登记为"与清单预期不符"，但不作为数据完整性缺陷。**

**唯一值得追的问题：DB 快照时间戳跨阶段不一致。**
- `01_r1_longwin` 全 6 个叶：`db_mtime_iso = 2026-10-25T03:13:40Z`
- `03/04/05/06` 全部 47 个叶：`2026-10-25T20:08:50Z`
- `02_r2a_std` 混合：四态 03:13（但 `completed_at` 为 20:08:50），风险/通用 20:08

两次时间戳记的 `db_size_bytes` **同为 324,022,272**，因此**无法从产物判断缓存内容是否在 17 小时间隔内被改写**（DuckDB 写入/WAL 检查点可以只改 mtime 不改 size）。**这是一个未解决项**：若 03:13 与 20:08 之间曾重灌或修正缓存，则 `01` 的 6 个 walkforward 与 `02–06` 的 standard **不是同一份行情数据**，跨阶段比较（§5）需再加一层保留。

### 6.2 `run.log` 关键词扫描（62 个文件，31,498 行）

| 关键词 | 01 (6) | 02 (3) | 03 (6) | 04 (2) | 05 (6) | 06 (39) | 合计 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `WARNING` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `UserWarning` | 0 | 0 | 0 | 0 | 0 | **4** | 4 |
| `baseline_calendar_mismatch` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `aligned date range` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `truncat` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `mismatch` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `_align_universe` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `Traceback` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `Error` | 0 | 3 | 0 | 2 | 6 | 39 | 50（**全部是 `Tracking Error:` 的正常打印行，非报错**） |
| `market impact` | 0 | 0 | 0 | 0 | **2** | 0 | 2 |
| `empty style bucket(s) ['reversal']` | 3,024 | 1,426 | 1,890 | 1,982 | 1,426 | 18,538 | **28,286**（占总行数 **89.8%**） |

要点：

- **零 `WARNING`、零 Traceback、零日历错配**。`baseline_calendar_mismatch` 在日志里从不打印，其权威值为 `comparison_report.json` 字段：**12/12 walkforward 叶全部 `false`** ✅。
- **"截断"信息不在日志里，只在 `walkforward_summary.json` 里**：01 的 `actual_last_fold_end = 2024-11-14`（请求 2024-12-31，提前 47 天）、03 的 `actual_last_fold_end = 2026-07-27`（请求 2026-10-22，提前 87 天）。日志扫描因此**不能**用来确认截断，必须读 summary——这是一个"看起来干净、实则漏检"的陷阱，本报告已在 §5.2(c)(d) 单独记录。
- **唯一真实告警是 4 行 `UserWarning`**，全部落在 `no_sectorthrot` 的 2 个 cfsb/crb 叶（各 2 行），内容见 §3.4——正是本次审计最关键的一处配置缺陷。
- **89.8% 的日志体积是同一句 `regime weighting: empty style bucket(s) ['reversal']` 的逐日重复**（单文件最多 3,024 行）。这是 cfsb 在 `regime_ic_ir_hybrid` 模式下把某个子策略判为 `reversal` 风格桶后，桶内无成员导致的每根 bar 复读；**不影响数值**，但使日志几乎不可读，也让"grep 日志找异常"这类检查严重低效。建议后续改为只打印首/末次并计数（不影响本次结论）。

### 6.3 产物完备性

每个 standard 叶都含 `backtest_equity.csv`、`backtest_weights.csv`、`baseline_equity.csv`、`comparison_report.json`、`equity_curve.png`、`rebalance_report.csv`、`strategy_dump.json`、`run.log`、`run_manifest.json`（9 件）；每个 walkforward 叶含 `walkforward_rebalances.csv`、`walkforward_report.csv`、`walkforward_summary.json`、`walkforward_weights.csv`、`comparison_report.json`、`strategy_dump.json`、`run.log`、`run_manifest.json`（8 件）。**62/62 无缺件。**

`strategy_dump.json` 的版本在 6 个阶段内**完全一致**（params 的 sha256 分组：四态 `d75f4034e8bf`、风险 `b7baa4321f3d`、双重 `89aa4004f55d`、通用 `8ca89ce30daa`、危机盾 `7635384db90d`、四态盾 `d36762f0517c`，各阶段同值），且与 `AGENTS.md` 记录的部署例外一致——**四态仍为 `blend_weighting_mode = "regime_ic_ir_hybrid"`（未被误"规范化"回 `rolling_ic_ir`）** ✅；`strategy_version/base_version` 全部为 `1.2.1/1.6.0`、`1.1.0/1.6.0`、`1.0.0/1.6.0` 等，与 `docs/version.md` 口径相符。

### 6.4 可复现性缺陷

`git_dirty = true` 出现在 **62/62** 个 manifest 中。这意味着 `8552c4b1` **不是**产生这批数字的真实代码状态：工作树在上游 commit 之上带有未提交修改（`AGENTS.md` 已记录本仓多个策略处于版本演进中）。**任何"从 commit 8552c4b1 重跑应得同数"的假设都不成立**——重跑只能复现"当时那个脏工作树"，而脏内容未被归档。这是本批结果最实质的复现性短板，建议后续在 manifest 里额外落一份 `git diff` 的 sha256。

---

## 7. 明确结论

### 7.1 哪些消融结论可信

判据：① 该臂的 override 真正在生效参数里落地；② 权益曲线非 ≡/≈ 基线；③ |Δ| 超过 §2.2 的噪声下限（±3pp CAGR / ±0.15 Sharpe）；④ 三个策略同向（更强的结论）或至少不反向矛盾。

| 消融结论 | 证据 | 可信度 |
| :--- | :--- | :--- |
| **惯性/换手过滤（`no_inertia`）是真实且高价值的杠杆**：关闭后调仓次数 462→893 / 480→947 / 503→917（**+82%~+97%**），Σ换手 62→112 / 63→119 / 70→126（**+80%~92%**），CAGR 全部下降 −2.73pp / −3.70pp / −3.02pp，Sharpe 全部下降 −0.060 / −0.105 / −0.132。**三策略同向、换手侧证据与收益侧证据互相印证**（不是单一路径噪声能造出的），这是本批消融中唯一"双向一致"的结论。 | ✅✅ 最强 | **可信** |
| **CYQ 质量门（`no_cyq`）主要是回撤保护器**：关闭后四态 MaxDD 10.70%→16.76%（**+6.06pp**）、风险 9.54%→13.29%（**+3.75pp**），而 CAGR 只 −0.45pp / −0.67pp。即"牺牲不到 0.7pp 收益换 3.8–6.1pp 回撤"。双重方向相反（+1.56pp CAGR、DD −0.48pp）。 | ✅ 四态/风险同向且幅度大 | **对四态、风险可信；不适用于双重** |
| **行业集中度上限（`no_sectorthrot`）的净效果是压回撤**：放开 2→20 后风险 MaxDD +2.39pp、四态 +1.38pp，CAGR 几乎不动（−0.38pp / +0.11pp）。双重反向（DD −1.03pp）。注意该臂在 cfsb/crb 上**只测了上限、没测 throttle**（§3.4）。 | ✅ 两策略同向 | **方向可信，"上限换回撤"这一半** |
| **自适应止损乘数（`no_atrstop`）在本窗口被固定为常数 4.5× 后收益上升**：该臂 override 只有 `blend_enable_adaptive_stop_atr: false`，`stop_mode` 仍为 `atr_trailing`、`stop_atr_mult` 仍为 4.5（§3.4），所以它测的是「自适应乘数 → 常数 4.5×」，**不是**「止损 → 无止损」。三策略 CAGR 全部上升（+2.52 / +0.98 / +1.01pp），MaxDD 两降一微升（−1.34 / −0.94 / +0.11pp），Σ换手几乎不变（62.4→63.3 等）⇒ 只能读作「`med_vol/s_vol` 自适应缩放在本窗口没有体现出正贡献」。 | ✅ 三策略同向，幅度略超噪声但 Sharpe 增益仅 +0.03~+0.15 | **方向可信，但主体是「自适应乘数」**；原「ATR 跟踪止损是净损耗」的表述**作废**（见下方勘误） |
| **波动率目标（`no_voltarget`）是净损耗**：关闭后三策略 CAGR +1.14~+1.51pp、Sharpe +0.015~+0.091，MaxDD 仅 +0.02~+0.92pp。三策略同向但**全部落在噪声下限内**（ΔSharpe ≤ 0.091 < 0.15）。 | ⚠️ 同向但幅度不足 | **只能判定"没有可测到的正贡献"，不能判定"应删除"** |
| **牛市满仓（`bull_full`）用回撤换收益**：三策略 MaxDD **全部恶化** +3.61 / +2.80 / +1.77pp，CAGR 两升一降，Sharpe 两降一升。 | ✅ MaxDD 侧三策略同向且幅度 >3pp 的组合 | **"抬高回撤"可信；"抬高收益"不可信** |
| **正交化（`no_orthogon`）符号不稳定**：风险 −3.73pp/−0.193、四态 +0.95pp/+0.098、双重 +1.64pp/+0.123。 | ❌ 反向矛盾 | **不可信，不得声称"正交化有效/无效"** |
| **推力现金部署（`no_thrust`）符号不稳定**：风险 **−7.18pp**（本次最大单臂跌幅）、双重 +2.05pp/+0.369、四态 +0.14pp。 | ❌ 反向矛盾（幅度差 9.2pp） | **不可信。唯一稳健的次结论是"去掉它会大幅降低换手"（Σ换手 62.44→40.20 / 62.55→35.97 / 70.31→46.98，即 −33%~−42%）** |

> [!WARNING]
> **勘误（2026-10-25）：`no_atrstop` 臂从未关闭 ATR 跟踪止损，上一版「ATR 跟踪止损在本窗口是净损耗」的结论作废。**
>
> **原文（逐字保留，以便对照）**：
>
> > 「**ATR 跟踪止损（`no_atrstop`）在本窗口是净损耗**：关闭后三策略 CAGR **全部上升**（+2.52 / +0.98 / +1.01pp），MaxDD 两降一微升（−1.34 / −0.94 / +0.11pp），Σ换手几乎不变（62.4→63.3 等）。即止损既没换来更低回撤、也没降低摩擦，只是削掉了收益。**三策略同向，且与"止损=保险"的直觉相反**。」（原判定栏：「✅ 同向，幅度略超噪声但 Sharpe 增益仅 +0.03~+0.15」「**方向可信，量级需保留**」）
>
> **为什么错（机制层）**：该臂的唯一 override 是 `{"blend_enable_adaptive_stop_atr": false}`，**它从未触碰 `stop_mode`**。`enable_adaptive_stop_atr` 在 `pipeline/research/blend/blend_base.py` 全文件只出现两处：`:612`（`resolve_param` 读取）与 `:892`（`if enable_adaptive_stop_atr:` 分支）。止损块由 `:875` 的 `if stop_mode in ("atr_trailing", "fixed_pct")` 守卫、由 `:914` 的 `if curr_p < trailing_stop_p:` 触发；开关为 `False` 时唯一差别是 `:891` 的 `eff_stop_mult = stop_atr_mult`（4.5）不再被 `:892–896` 的 `np.clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5)` 覆盖。⇒ **该臂测的是「自适应乘数 → 常数 4.5×」，不是「止损 → 无止损」。**
>
> **为什么错（流程层）**：`docs/backtest_research.md` §8 的 `no_atrstop` 行与其后「语义边界披露」早已写明——关闭后「回退至固定 4.5x ATR 追踪止损，**并不是**彻底关闭底层 ATR 止损机制本身（彻底关闭需设置 `"${pfx}_stop_mode": "none"`）」。本报告 §1.3 明言臂定义逐字取自该来源，却在解释结果时没有回读自己引用的定义。**这是一次「臂标签被当成语义」的误标，不是数字错误。**
>
> **三份产物证据（6/6 叶一致：`06_ablate` 与 `08_ablate_wf` × cfsb/crb/cdhb）**：
> 1. `run_manifest.json` → `cli_overrides` 只含 `{"blend_enable_adaptive_stop_atr": false}`（无 `*_stop_mode` 键）；
> 2. `strategy_dump.json` → `params` 中 `*_stop_mode` 仍为 `"atr_trailing"`、`*_stop_atr_mult` 仍为 `4.5`；
> 3. `run.log` 第 2 行 `Parameters: {...}` 的运行期生效参数同值（§3.4 已核 39/39 叶 `dump_vs_log_agree = True`）。
>
> **反向证据（真正的「止损关闭」臂，18 折 walkforward）**：`09_param_grid/atr/atr_none`（`cdhb_stop_mode = "none"`，`2022-01-04`→`2026-10-22`，0.25y/0.25y，`--cash-sweep-rate 0.015`）对 cdhb 给出 **ΔCAGR −0.1590pp、ΔMaxDD +0.2137pp（更差）**（`backtests_research/20261025_125300/09_param_grid/atr/atr_none/chan_dual_hybrid_blend/walkforward_summary.json` 对 `08_ablate_wf/baseline/chan_dual_hybrid_blend/`；该臂 `run.log` 的生效参数为 `blend_enable_adaptive_stop_atr: True` + `cdhb_stop_mode: 'none'`，与 `no_atrstop` 正好互补）。即**把止损真正拿掉，收益并未上升、回撤反而更深**，与「止损是净拖累」方向相反。
>
> **仍然成立的（主体已更换，不得丢）**：
> - 「把自适应乘数固定为 4.5× 后，三策略 CAGR 全部上升（+2.52 / +0.98 / +1.01pp）」这个**描述性事实**不变，可引用；主体必须写成「自适应乘数」。
> - `no_atrstop` 与基线**非 ≡**（§3.2 的 ✓/✓/✓、§3.1 的相对差判定）仍然成立且自洽——固定乘数确实改变了 `trailing_stop_p`，从而改变止损价格与成交。
> - 「**不得**移除 ATR 止损」这一禁令不但没被推翻，反而被 `atr_none` 加强（真实移除后 CAGR 侧不升、回撤侧更深）。
>
> **判废范围**：作废的只是**主体为「ATR 跟踪止损」的因果解释**与**其「方向可信」评级**；数字、序号、观测本身全部保留。下游若曾引用「6/6 格关掉止损 CAGR 升」（`06` standard × 3 + `08` walkforward × 3 共 6 格），该引用须一并改写为「关掉自适应乘数」。

### 7.2 哪些臂必须判定为无效（不得解释、不得引用）

| 臂 / 策略 | 无效原因 | 处置 |
| :--- | :--- | :--- |
| `no_structregime` / **风险**、**双重** | 逐位相同（逐位差为 0）；代码级确证该分支仅存在于 `regime_adaptive`/`regime_ic_ir_hybrid`，而两策略为 `rolling_ic_ir` | **作废**。不能读作"结构状态机无效"，只能读作"该权重模式下不存在此机制" |
| `no_hysteresis` / **风险**、**双重** | 逐位相同（逐位差为 0）；根因是**基线本已关闭**（`StrategyConfig` 默认 `False`，两策略 params 无此键），不是不可达 | **作废**。要评估 hysteresis 必须先造一个"基线开启"的对照臂 |
| `no_lowbeta` / **双重** | 逐位相同（逐位差为 0） | **作废** |
| `no_lowbeta` / **四态** | 相对差 1.6e-4，ΔCAGR −0.003pp | **作废**（同 7.3 的"实质惰性"） |
| `no_circuit` / **四态**、**风险** | 逐位相同（逐位差为 0），且被 `04` 的独立 A/B（726 根）再次复证 | **作废（作为"熔断贡献"的证据）**。但它作为一条**独立结论**极有价值：见 §7.4 |
| `no_circuit` / **双重** | 相对差 2.6e-4 | **实质惰性，作废** |
| `no_sectorthrot` / **四态**、**风险** 的 `*_adaptive_sector_throttle` 半个杠杆 | `StrategyConfig.from_dict` 明确 `UserWarning: ignoring unknown key(s)`（4 行日志），prefixed 键未被配置对象识别；且该 throttle 在 cfsb/crb 基线本就为 `False` | **该臂只能解释为 `blend_max_assets_per_sector: 2→20`**。不得把其 Δ 说成"行业节流的作用" |
| 整个 `04_r1_std_cb_compare` 阶段 | 两臂 726/726 根逐位相同、所有比较指标五位小数一致 | **该阶段信息量为零**，不得作为独立证据引用；其 726 根窗口亦不可与 1,161 根阶段并列 |

### 7.3 哪些数字不可引用

1. **`03_r2b_oos` 的一切绝对值**（CAGR / Sharpe / MaxDD / Calmar）。6 个约 60 根的小折，折 CAGR 极差最大 135pp（风险：−13.95% → +121.10%），DSR 低至 1.7e-21。**引用即为过度解读。**
2. **`01_r1_longwin` 的 "均值" 当作预期年化**。它是 4 个高度重叠 2 年窗（步长 0.25 年）的算术平均，非独立样本；且末折实际止于 2024-11-14 而非请求的 2024-12-31。只能读作"窗口敏感性探测"。
3. **任何 ΔCAGR < 3pp 或 ΔSharpe < 0.15 的消融差值的符号**。见 §2.2：同策略仅换评估窗就产生 ±10pp CAGR / ±0.3–0.5 Sharpe 漂移。具体地，`no_voltarget`、`no_lowbeta/风险`、`no_sectorthrot/风险`、`no_hysteresis/四态` 的符号**不可主张**。
4. **`INDEX.md` 里 04 阶段的"730 bars"**。实测 **726 根**（2022-01-04 → 2024-12-31）。INDEX 该行另有"熔断关闭对照臂"表述，实际与开启臂**逐位相同**。
5. **α 24.67% / 25.38% 等"年化 α"作为选股能力**。14 个叶 β 全部 0.409–0.537，基准为单一指数买持而策略可持现金、可跨 20 只标的，β<1 内生于构造。α 的分母（α 相对 000300）与"中国市场超额"不是同一概念。
6. **`run.log` 的 `Total Rebalances` 当作交易频率**。它是指令次数，真实成交次数只有其 78%–92%（§2.1），系统性高估 8%–22%。

### 7.4 两条应当独立成立、值得上报的观察（超出消融原意）

1. **三档回撤熔断器在这条 4.8 年路径上贡献恒为 0。** `no_circuit`（阈值置 10.0 不可达）在四态、风险上逐位复现基线；`04` 的独立 A/B 在 726 根上第二次逐位复现。而基线报告的 MaxDD 已达 **10.70%（四态）/ 10.89%（04 四态）**，**已越过 `dd_reduce_thresh = 0.10` 一档**。可确证的结论只有"熔断分支从未改变持仓"；**根因未定位**（候选：`fast_recovery` 旁路 `blend_base.py:1081`、或 `dd_mag` 基于内部 `cum_nav` 与报告权益峰谷不同步）。**建议单独插桩验证，不应在本报告内下结论。**
2. **`no_sectorthrot` 暴露出一个真实的配置命名缺陷**：`{cfsb,crb}_adaptive_sector_throttle` / `{cfsb,crb}_max_assets_per_sector` 会被 `StrategyConfig.from_dict` 静默丢弃并告警（`rs/config.py:1350`），而 `cdhb_*` 同名键是合法字段。也就是说：**同名概念在两个策略上可配、在另两个策略上不可配，且只在告警里可见**。这与 `AGENTS.md` 记录的 `resolve_param` 裸名前缀碰撞是同一类问题的两个侧面（那里是"意外读到全局值"，这里是"写了但被丢弃"）。

### 7.5 本 worker 未做的事项（职责边界）

- 未生成、未修改 `FORENSIC_AUDIT.md` / `forensic_overfit_report.md` / `pruned_universe.txt`，未运行 `run_audit.py`。
- 未做 Leave-One-Out 资产脆弱性、superstar winner 偏斜、过拟合风险评分、安全分级——属另一 worker 的取证审计范围。
- **未提出任何标的剔除建议**，未改动 `docs/` 下任何文件（含 `docs/black_list.md`、`docs/development_plan.md`、`docs/version.md`）。
- 未修改/删除任何既有产物；本次仅新增本文件 `ABLATION_AND_ANATOMY.md`。

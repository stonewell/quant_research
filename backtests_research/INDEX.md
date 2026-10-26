# 回测研究运行索引 (Backtest Research Index)

本文档由回测流程维护，记录每次正式回测运行（`backtests_research/<run_ts>/...`）的自审计元数据与复核索引。每一行对应一个叶子运行目录，取值以同目录下的 `run_manifest.json` 为单一事实来源（SSOT）。上表在册运行 + 「已登记但不再索引」一节的运行 = 磁盘实际叶子目录数；**索引不含不代表产物不存在**。

| 时间戳目录 | 实验组 (01–09) | 策略 | 关键指标 CAGR / Sharpe / MaxDD | 本次运行的变更点或消融臂 | 对应 [docs/development_plan.md](file:///home/stone/Work/github/quant/docs/development_plan.md) 任务条目 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `20261025_125300` | `01_r1_longwin` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.31% / Sharpe: 1.66 / MaxDD: 8.84% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `01_r1_longwin` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.78% / Sharpe: 1.82 / MaxDD: 9.86% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `01_r1_longwin` | Chan Four-State Shield Blend Strategy | CAGR: 30.34% / Sharpe: 1.83 / MaxDD: 10.69% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `01_r1_longwin` | Chan Risk-Managed Blend Strategy | CAGR: 31.39% / Sharpe: 1.86 / MaxDD: 9.29% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `01_r1_longwin` | Chan Single Crisis Shield Blend Strategy | CAGR: 27.80% / Sharpe: 1.71 / MaxDD: 10.41% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `01_r1_longwin` | Generic Multi-Strategy Blend (Adaptive) | CAGR: 13.87% / Sharpe: 0.96 / MaxDD: 14.83% | 长窗口基准 (4折 walkforward, 2.0y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 26.15% / Sharpe: 2.57 / MaxDD: 4.00% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 23.79% / Sharpe: 1.81 / MaxDD: 4.47% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Chan Four-State Shield Blend Strategy | CAGR: 15.73% / Sharpe: 1.56 / MaxDD: 4.74% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Chan Risk-Managed Blend Strategy | CAGR: 34.96% / Sharpe: 2.30 / MaxDD: 4.40% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Chan Single Crisis Shield Blend Strategy | CAGR: 21.83% / Sharpe: 1.94 / MaxDD: 4.28% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `03_r2b_oos` | Generic Multi-Strategy Blend (Adaptive) | CAGR: 0.16% / Sharpe: 0.24 / MaxDD: 6.04% | 样本外基准 (6折 walkforward, 0.25y window / 0.25y step) | L2.48, L2.50 |
| `20261025_125300` | `06_ablate/baseline` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.46% / Sharpe: 1.26 / MaxDD: 11.80% | 消融基线臂 (连续 standard + 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/baseline` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.76% / Sharpe: 1.62 / MaxDD: 10.70% | 消融基线臂 (连续 standard + 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/baseline` | Chan Risk-Managed Blend Strategy | CAGR: 27.83% / Sharpe: 1.79 / MaxDD: 9.50% | 消融基线臂 (连续 standard + 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/bull_full` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 20.62% / Sharpe: 1.38 / MaxDD: 13.60% | 消融臂: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/bull_full` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 25.95% / Sharpe: 1.54 / MaxDD: 14.30% | 消融臂: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/bull_full` | Chan Risk-Managed Blend Strategy | CAGR: 25.31% / Sharpe: 1.54 / MaxDD: 12.30% | 消融臂: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_atrstop` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 18.47% / Sharpe: 1.37 / MaxDD: 10.90% | 消融臂: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_atrstop` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 27.28% / Sharpe: 1.77 / MaxDD: 9.40% | 消融臂: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_atrstop` | Chan Risk-Managed Blend Strategy | CAGR: 28.81% / Sharpe: 1.82 / MaxDD: 9.60% | 消融臂: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_circuit` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.47% / Sharpe: 1.26 / MaxDD: 11.80% | 消融臂: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `06_ablate/no_circuit` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.76% / Sharpe: 1.62 / MaxDD: 10.70% | 消融臂: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `06_ablate/no_circuit` | Chan Risk-Managed Blend Strategy | CAGR: 27.83% / Sharpe: 1.79 / MaxDD: 9.50% | 消融臂: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `06_ablate/no_cyq` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 19.02% / Sharpe: 1.31 / MaxDD: 11.30% | 消融臂: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_cyq` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.31% / Sharpe: 1.51 / MaxDD: 16.80% | 消融臂: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_cyq` | Chan Risk-Managed Blend Strategy | CAGR: 27.16% / Sharpe: 1.70 / MaxDD: 13.30% | 消融臂: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_hysteresis` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.46% / Sharpe: 1.26 / MaxDD: 11.80% | 消融臂: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_hysteresis` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.94% / Sharpe: 1.62 / MaxDD: 10.50% | 消融臂: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_hysteresis` | Chan Risk-Managed Blend Strategy | CAGR: 27.83% / Sharpe: 1.79 / MaxDD: 9.50% | 消融臂: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_inertia` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 14.44% / Sharpe: 1.13 / MaxDD: 9.70% | 消融臂: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0) | L2.49 |
| `20261025_125300` | `06_ablate/no_inertia` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 22.03% / Sharpe: 1.55 / MaxDD: 11.60% | 消融臂: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0) | L2.49 |
| `20261025_125300` | `06_ablate/no_inertia` | Chan Risk-Managed Blend Strategy | CAGR: 24.13% / Sharpe: 1.68 / MaxDD: 11.20% | 消融臂: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0) | L2.49 |
| `20261025_125300` | `06_ablate/no_lowbeta` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.46% / Sharpe: 1.26 / MaxDD: 11.80% | 消融臂: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49 |
| `20261025_125300` | `06_ablate/no_lowbeta` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.76% / Sharpe: 1.61 / MaxDD: 10.70% | 消融臂: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49 |
| `20261025_125300` | `06_ablate/no_lowbeta` | Chan Risk-Managed Blend Strategy | CAGR: 27.94% / Sharpe: 1.79 / MaxDD: 9.50% | 消融臂: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49 |
| `20261025_125300` | `06_ablate/no_orthogon` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 19.10% / Sharpe: 1.38 / MaxDD: 11.10% | 消融臂: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_orthogon` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 25.71% / Sharpe: 1.71 / MaxDD: 10.30% | 消融臂: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_orthogon` | Chan Risk-Managed Blend Strategy | CAGR: 24.10% / Sharpe: 1.60 / MaxDD: 10.90% | 消融臂: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_sectorthrot` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.90% / Sharpe: 1.28 / MaxDD: 10.80% | 消融臂: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_sectorthrot` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.38% / Sharpe: 1.60 / MaxDD: 12.10% | 消融臂: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_sectorthrot` | Chan Risk-Managed Blend Strategy | CAGR: 27.94% / Sharpe: 1.80 / MaxDD: 11.90% | 消融臂: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_structregime` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 17.46% / Sharpe: 1.26 / MaxDD: 11.80% | 消融臂: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_structregime` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.06% / Sharpe: 1.86 / MaxDD: 9.60% | 消融臂: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_structregime` | Chan Risk-Managed Blend Strategy | CAGR: 27.83% / Sharpe: 1.79 / MaxDD: 9.50% | 消融臂: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_thrust` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 19.51% / Sharpe: 1.63 / MaxDD: 9.10% | 消融臂: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_thrust` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.90% / Sharpe: 1.76 / MaxDD: 10.00% | 消融臂: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_thrust` | Chan Risk-Managed Blend Strategy | CAGR: 20.65% / Sharpe: 1.66 / MaxDD: 7.20% | 消融臂: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_voltarget` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 18.60% / Sharpe: 1.35 / MaxDD: 11.80% | 消融臂: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_voltarget` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 26.27% / Sharpe: 1.67 / MaxDD: 11.60% | 消融臂: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `06_ablate/no_voltarget` | Chan Risk-Managed Blend Strategy | CAGR: 29.22% / Sharpe: 1.80 / MaxDD: 10.20% | 消融臂: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 7.84% / Sharpe: 0.40 / MaxDD: 5.48% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Chan Four-State Risk-Managed Blend Strategy | CAGR: -0.53% / Sharpe: -0.09 / MaxDD: 5.31% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Chan Four-State Shield Blend Strategy | CAGR: 5.30% / Sharpe: 0.34 / MaxDD: 4.71% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Chan Risk-Managed Blend Strategy | CAGR: 2.99% / Sharpe: 0.20 / MaxDD: 5.05% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Chan Single Crisis Shield Blend Strategy | CAGR: 7.81% / Sharpe: 0.52 / MaxDD: 4.80% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `07_r3_oos_bear` | Generic Multi-Strategy Blend (Adaptive) | CAGR: 17.30% / Sharpe: 0.92 / MaxDD: 6.47% | 熊市样本外 (6折 walkforward, 2021-06-01→2022-12-16, 0.25y/0.25y, 现金计息 1.5%) | L2.48, L2.50 |
| `20261025_125300` | `08_ablate_wf/baseline` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.63% / Sharpe: 1.42 / MaxDD: 4.22% | 消融基线臂 walkforward 复核 (18 折, 0.25y/0.25y, 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/baseline` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.15% / Sharpe: 1.43 / MaxDD: 4.69% | 消融基线臂 walkforward 复核 (18 折, 0.25y/0.25y, 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/baseline` | Chan Risk-Managed Blend Strategy | CAGR: 27.88% / Sharpe: 1.54 / MaxDD: 4.15% | 消融基线臂 walkforward 复核 (18 折, 0.25y/0.25y, 现金计息 1.5%) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/bull_full` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.50% / Sharpe: 1.12 / MaxDD: 5.09% | 消融臂 walkforward 复核: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/bull_full` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 28.21% / Sharpe: 1.13 / MaxDD: 5.24% | 消融臂 walkforward 复核: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/bull_full` | Chan Risk-Managed Blend Strategy | CAGR: 33.97% / Sharpe: 1.36 / MaxDD: 5.40% | 消融臂 walkforward 复核: 牛市满仓敞口 (target_bull_exposure=1.0) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_atrstop` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 26.25% / Sharpe: 1.49 / MaxDD: 4.09% | 消融臂 walkforward 复核: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_atrstop` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.38% / Sharpe: 1.42 / MaxDD: 4.61% | 消融臂 walkforward 复核: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_atrstop` | Chan Risk-Managed Blend Strategy | CAGR: 29.96% / Sharpe: 1.66 / MaxDD: 4.24% | 消融臂 walkforward 复核: 关闭自适应 ATR 跟踪止损 (blend_enable_adaptive_stop_atr=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_circuit` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.63% / Sharpe: 1.42 / MaxDD: 4.22% | 消融臂 walkforward 复核: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `08_ablate_wf/no_circuit` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.14% / Sharpe: 1.43 / MaxDD: 4.69% | 消融臂 walkforward 复核: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `08_ablate_wf/no_circuit` | Chan Risk-Managed Blend Strategy | CAGR: 27.94% / Sharpe: 1.54 / MaxDD: 4.15% | 消融臂 walkforward 复核: 关闭三档回撤熔断器 (dd_thresh=10.0 不可达哨兵) | L2.50 |
| `20261025_125300` | `08_ablate_wf/no_cyq` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 26.95% / Sharpe: 1.46 / MaxDD: 4.35% | 消融臂 walkforward 复核: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_cyq` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 28.76% / Sharpe: 1.46 / MaxDD: 4.84% | 消融臂 walkforward 复核: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_cyq` | Chan Risk-Managed Blend Strategy | CAGR: 26.91% / Sharpe: 1.50 / MaxDD: 4.78% | 消融臂 walkforward 复核: 关闭筹码质量门控 (blend_enable_cyq_quality_gate=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_hysteresis` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.63% / Sharpe: 1.42 / MaxDD: 4.22% | 消融臂 walkforward 复核: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_hysteresis` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.04% / Sharpe: 1.43 / MaxDD: 4.60% | 消融臂 walkforward 复核: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_hysteresis` | Chan Risk-Managed Blend Strategy | CAGR: 27.88% / Sharpe: 1.54 / MaxDD: 4.15% | 消融臂 walkforward 复核: 关闭 regime 状态机迟滞 (blend_enable_regime_hysteresis=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_inertia` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 23.29% / Sharpe: 1.29 / MaxDD: 4.87% | 消融臂 walkforward 复核: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0, min_holding_bars=0, entry_threshold=0.0) | L2.49 |
| `20261025_125300` | `08_ablate_wf/no_inertia` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 28.20% / Sharpe: 1.25 / MaxDD: 4.86% | 消融臂 walkforward 复核: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0, min_holding_bars=0, entry_threshold=0.0) | L2.49 |
| `20261025_125300` | `08_ablate_wf/no_inertia` | Chan Risk-Managed Blend Strategy | CAGR: 27.54% / Sharpe: 1.34 / MaxDD: 4.67% | 消融臂 walkforward 复核: 关闭调仓资产惯性过滤 (adaptive_inertia=false, min_weight_change=0.0, min_holding_bars=0, entry_threshold=0.0) | L2.49 |
| `20261025_125300` | `08_ablate_wf/no_lowbeta` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.56% / Sharpe: 1.43 / MaxDD: 4.23% | 消融臂 walkforward 复核: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_lowbeta` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 29.62% / Sharpe: 1.36 / MaxDD: 4.69% | 消融臂 walkforward 复核: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_lowbeta` | Chan Risk-Managed Blend Strategy | CAGR: 28.10% / Sharpe: 1.56 / MaxDD: 4.11% | 消融臂 walkforward 复核: 关闭低 beta 资产惯性 (blend_enable_low_beta_inertia=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_orthogon` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 27.23% / Sharpe: 1.37 / MaxDD: 4.36% | 消融臂 walkforward 复核: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_orthogon` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 29.94% / Sharpe: 1.54 / MaxDD: 4.50% | 消融臂 walkforward 复核: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_orthogon` | Chan Risk-Managed Blend Strategy | CAGR: 28.91% / Sharpe: 1.78 / MaxDD: 4.38% | 消融臂 walkforward 复核: 关闭 Löwdin 正交化 (blend_enable_orthogonalization=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_sectorthrot` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.05% / Sharpe: 1.47 / MaxDD: 4.26% | 消融臂 walkforward 复核: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_sectorthrot` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 29.71% / Sharpe: 1.36 / MaxDD: 4.82% | 消融臂 walkforward 复核: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_sectorthrot` | Chan Risk-Managed Blend Strategy | CAGR: 27.68% / Sharpe: 1.57 / MaxDD: 4.02% | 消融臂 walkforward 复核: 关闭行业板块上限约束 (max_assets_per_sector=20) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_structregime` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.63% / Sharpe: 1.42 / MaxDD: 4.22% | 消融臂 walkforward 复核: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_structregime` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.41% / Sharpe: 1.40 / MaxDD: 4.63% | 消融臂 walkforward 复核: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_structregime` | Chan Risk-Managed Blend Strategy | CAGR: 27.88% / Sharpe: 1.54 / MaxDD: 4.15% | 消融臂 walkforward 复核: 关闭结构 regime 识别 (blend_enable_structural_regime=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_thrust` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 20.89% / Sharpe: 1.52 / MaxDD: 3.38% | 消融臂 walkforward 复核: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_thrust` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 24.21% / Sharpe: 1.36 / MaxDD: 3.99% | 消融臂 walkforward 复核: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_thrust` | Chan Risk-Managed Blend Strategy | CAGR: 26.81% / Sharpe: 1.72 / MaxDD: 3.84% | 消融臂 walkforward 复核: 关闭广度推力现金部署 (dynamic_cash_deployment=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_voltarget` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 26.48% / Sharpe: 1.49 / MaxDD: 4.41% | 消融臂 walkforward 复核: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_voltarget` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.57% / Sharpe: 1.39 / MaxDD: 4.93% | 消融臂 walkforward 复核: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `08_ablate_wf/no_voltarget` | Chan Risk-Managed Blend Strategy | CAGR: 31.41% / Sharpe: 1.63 / MaxDD: 4.38% | 消融臂 walkforward 复核: 关闭 Garman-Klass 波动率目标 (enable_vol_targeting=false) | L2.49, L2.50 |
| `20261025_125300` | `09_param_grid/atr/atr_6_0` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 24.62% / Sharpe: 1.41 / MaxDD: 4.38% | 参数网格 walkforward: cdhb_stop_atr_mult=6.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_6_0` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.45% / Sharpe: 1.48 / MaxDD: 4.52% | 参数网格 walkforward: cfsb_stop_atr_mult=6.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_6_0` | Chan Risk-Managed Blend Strategy | CAGR: 27.37% / Sharpe: 1.47 / MaxDD: 4.42% | 参数网格 walkforward: crb_stop_atr_mult=6.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_8_0` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.23% / Sharpe: 1.45 / MaxDD: 4.22% | 参数网格 walkforward: cdhb_stop_atr_mult=8.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_8_0` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 30.29% / Sharpe: 1.46 / MaxDD: 4.68% | 参数网格 walkforward: cfsb_stop_atr_mult=8.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_8_0` | Chan Risk-Managed Blend Strategy | CAGR: 29.35% / Sharpe: 1.60 / MaxDD: 4.28% | 参数网格 walkforward: crb_stop_atr_mult=8.0（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_none` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 25.47% / Sharpe: 1.42 / MaxDD: 4.43% | 参数网格 walkforward: cdhb_stop_mode=none（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_none` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 33.54% / Sharpe: 1.63 / MaxDD: 4.61% | 参数网格 walkforward: cfsb_stop_mode=none（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/atr/atr_none` | Chan Risk-Managed Blend Strategy | CAGR: 29.85% / Sharpe: 1.65 / MaxDD: 4.37% | 参数网格 walkforward: crb_stop_mode=none（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_008_4` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 30.57% / Sharpe: 1.52 / MaxDD: 4.33% | 参数网格 walkforward: cdhb_min_weight_change=0.08 + cdhb_min_holding_bars=4（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_008_4` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 33.33% / Sharpe: 1.64 / MaxDD: 5.20% | 参数网格 walkforward: cfsb_min_weight_change=0.08 + cfsb_min_holding_bars=4（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_008_4` | Chan Risk-Managed Blend Strategy | CAGR: 31.73% / Sharpe: 1.54 / MaxDD: 4.92% | 参数网格 walkforward: crb_min_weight_change=0.08 + crb_min_holding_bars=4（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_010_5` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 27.52% / Sharpe: 1.49 / MaxDD: 4.45% | 参数网格 walkforward: cdhb_min_weight_change=0.1 + cdhb_min_holding_bars=5（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_010_5` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 35.90% / Sharpe: 1.76 / MaxDD: 5.41% | 参数网格 walkforward: cfsb_min_weight_change=0.1 + cfsb_min_holding_bars=5（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_010_5` | Chan Risk-Managed Blend Strategy | CAGR: 23.75% / Sharpe: 1.21 / MaxDD: 5.19% | 参数网格 walkforward: crb_min_weight_change=0.1 + crb_min_holding_bars=5（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_015_10` | Chan Dual Hybrid Alpha Blend Strategy | CAGR: 22.18% / Sharpe: 1.40 / MaxDD: 4.51% | 参数网格 walkforward: cdhb_min_weight_change=0.15 + cdhb_min_holding_bars=10（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_015_10` | Chan Four-State Risk-Managed Blend Strategy | CAGR: 28.88% / Sharpe: 1.44 / MaxDD: 5.28% | 参数网格 walkforward: cfsb_min_weight_change=0.15 + cfsb_min_holding_bars=10（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |
| `20261025_125300` | `09_param_grid/inertia/inertia_015_10` | Chan Risk-Managed Blend Strategy | CAGR: 22.07% / Sharpe: 1.42 / MaxDD: 4.86% | 参数网格 walkforward: crb_min_weight_change=0.15 + crb_min_holding_bars=10（18 折, 2022-01-04→2026-10-22, --cash-sweep-rate 0.015） | L2.49 |

> [!NOTE] 边界待裁决
> 下列两组的**去留尚未裁决**，因此暂时仍留在上表里。若日后判定它们同样不属于「值得重跑 / 需要复核」的对象，则按同一方式移入紧随其后的「已登记但不再索引的运行组」一节——叶子目录同样留在磁盘上不动，只是不再进入索引。
> - `01_r1_longwin`（6 行）—— 4 折、折重叠 87.5%，其唯一用途是 `FORENSIC_AUDIT.md` §7.4 评级表的负面教材（「折重叠 + DSR 饱和」案例）。
> - `06_ablate`（39 行）—— `ABLATION_AND_ANATOMY.md` 的 standard 口径 13 臂 ≡ 矩阵的唯一来源，且其 `no_atrstop` 臂正在被一份 2026-10-25 勘误引用。

## 已登记但不再索引的运行组（存档保留于磁盘，未删除）

> [!NOTE] 口径
> 下列叶子运行**完整保留在磁盘上**，只是不再作为「值得重跑 / 需要复核」的对象列入上表。移出索引的理由逐组写明。上表在册 114 行（01–08 保留 96 + 09 新增 18）+ 本节 11 行 = 125 = 磁盘 `run_manifest.json` 实数（01–08 共 107 + 09 共 18）。**索引不含不代表产物不存在** —— 任何引用这些数字的既有文档仍可按原路径直接复核。

| 组 | 叶子数 | 内容 | 不再索引的理由 |
| :--- | ---: | :--- | :--- |
| `02_r2a_std` | 3 | 连续 standard 口径基准（1161 bars） | 已被 `08_ablate_wf/baseline/` 完全覆盖——同策略、更长窗口、walkforward 口径 ⇒ 无独立重跑价值。 |
| `04_r1_std_cb_compare` | 2 | 熔断开关 A/B 对照（726 bars） | 实测差异 219.91 RMB，即无可测效应 ⇒ 结论已定，重跑不产生新信息。 |
| `05_anatomy` | 6 | 一次性全景解剖（含现金计息与冲击成本） | 全部结论已固化进 `ABLATION_AND_ANATOMY.md` ⇒ 属于一次性拆解，不是可重跑实验。 |

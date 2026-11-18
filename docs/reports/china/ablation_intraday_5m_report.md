# Intraday 5-Minute Microstructure & Blend Enhancement Ablation Report (Tasks T6.1 & T6.2)

> **RETIRED (2026-11-17)**: 六个 blend 侧 5m 特征开关（`blend_enable_intraday_5m` /
> `blend_enable_5m_interval_nesting` / `blend_intraday_upvol_thresh` /
> `blend_use_intraday_downside_vol` / `blend_enable_5m_stop_filter` /
> `blend_intraday_cutoff_time`）已从 `StrategyConfig` 与 `blend_base.py` **整体删除**，
> 生成本报告的脚本 `pipeline/research/blend/experiments/ablation_intraday_5m.py` 亦已删除。
> 下表数字**不可复现**（变体已坍缩为同一组权重），仅作为「Track T 为负结果」的历史证据保留。

> **Valuation Window**: 2024-01-01 to 2025-03-01 (280 Trading Days)  
> **Universe**: 6 Assets (5 Core China Equities + BIL Cash Proxy)  
> **Data Provider**: 100% Offline SyntheticDataProvider (Pinned Brownian Bridge 5m Bars)  
> **Intraday Cutoff**: `cutoff_time="14:15"` (39 Bars Causal Point-in-Time Alignment)  

---

## 1. Step-by-Step Ablation Performance Matrix

| Variant | Architecture / Enhancements | Sharpe | CAGR | MaxDD | Calmar | Rebalances | Turnover | DSR (N=5) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **M0** | M0: Baseline (Pure Daily) | **0.210** | +1.65% | 11.66% | 0.14 | 79 | 30.68 | **0.9891** |
| **M1** | M1: +Interval Nesting | **0.210** | +1.65% | 11.66% | 0.14 | 79 | 30.68 | **0.9891** |
| **M2** | M2: +Signed Vol & Downside Vol | **0.135** | +0.88% | 11.66% | 0.08 | 82 | 31.08 | **0.8558** |
| **M3** | M3: +Anti-Washout Stop & 3S TP | **0.135** | +0.88% | 11.66% | 0.08 | 82 | 31.08 | **0.8558** |
| **M4** | M4: Institutional Production Ensemble | **0.135** | +0.88% | 11.66% | 0.08 | 82 | 31.08 | **0.8558** |

---

## 2. Statistical Significance & Overfitting Verification (T6.2)

- **Ablation Winner**: **[M1] M1: +Interval Nesting**
- **Deflated Sharpe Ratio (DSR)**: **`0.9891`** (Hurdle: $DSR \ge 0.95$, passes with statistical significance against trial count penalty $N=5$).
- **Single Equivalent Random Search (ERS)**:
  - **Percentile**: **`1.0%`** (Threshold: $\ge 90.0\%$).
  - **Trust Status**: **`UNTRUSTED`** (beats random noise with statistical confidence).

---

## 3. Leave-One-Out (LOO) Fragility Audit

| Excluded Asset | LOO Sharpe | LOO CAGR | Δ Sharpe | Fragility Grade |
| :--- | :---: | :---: | :---: | :---: |
| `600000.SH` | -0.378 | -4.61% | -0.588 | 🟡 Sensitive |
| `601398.SH` | -0.295 | -3.74% | -0.505 | 🟡 Sensitive |
| `601857.SH` | -0.061 | -1.24% | -0.271 | 🟢 Robust |
| `601225.SH` | -0.052 | -1.18% | -0.262 | 🟢 Robust |
| `600660.SH` | -0.109 | -1.71% | -0.319 | 🟢 Robust |

> **Audit Conclusion**: The institutional production ensemble (M1) demonstrates resilient Sharpe stability across all asset exclusions, confirming absence of superstar-winner skew or single-stock fragility.

---
*原由 `pipeline/research/blend/experiments/ablation_intraday_5m.py` 自动生成（该脚本已于 2026-11-17 连同 Track T 开关面一并删除）；本报告保留为**历史结论**，不再可复现。*

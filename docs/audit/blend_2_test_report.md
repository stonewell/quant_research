# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_2`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 1  

---

## 1. Chan Dual Hybrid Alpha Blend Strategy
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `50 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **49.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 14.5%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 14.5%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **3.1% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 3.1%) |
| **Statistical Edge (DSR)** | **0.9924** | > 0.0500 | `EXCELLENT` | Deflated Sharpe confirms statistical alpha (DSR: 0.9924) |
| **Profit Concentration Skew** | **62.2% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 62.2% of positive returns |
| **Superstar Asset Fragility** | **276.9% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 276.9% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **31.9% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 31.9% of net profit (806.87 RMB) |
| **Capital Drag & Cash Flattery** | **37.5% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 37.5% | Norm MaxDD: 6.1% (vs 3.8% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **+2,529.54 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601872.SH)** | **-283.90 RMB** | `-111.2%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601872.SH, 600584.SH)** | **-2,448.62 RMB** | `-196.8%` | UNPROFITABLE |
| **Exclude Top 3 (601872.SH, 600584.SH, 601899.SH)** | **-4,473.78 RMB** | `-276.9%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **Superstar Fragility: Omitting 601872.SH flips Net PnL to negative (-283.90 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 62.2% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 31.9% of net profits**

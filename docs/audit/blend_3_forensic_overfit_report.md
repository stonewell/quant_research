# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_3`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 2  

---

## 1. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **8.8% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 8.8%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **30.9% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 30.9%) |
| **Superstar Asset Fragility** | **52.1% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 52.1% of net gain |
| **Friction Bleed Ratio** | **9.3% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 9.3% of net profit (4,032.42 RMB) |
| **Capital Drag & Cash Flattery** | **30.2% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 30.2% | Norm MaxDD: 6.5% (vs 4.5% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+43,334.56 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+30,648.89 RMB** | `-29.3%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601728.SH)** | **+25,100.85 RMB** | `-42.1%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601728.SH, 000938.SZ)** | **+20,773.65 RMB** | `-52.1%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 2. Chan Dual Hybrid Alpha Blend Strategy [Batch 2]
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `50 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **52.2% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 14.5%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 14.5%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **3.0% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 3.0%) |
| **Statistical Edge (DSR)** | **0.9971** | > 0.0500 | `EXCELLENT` | Deflated Sharpe confirms statistical alpha (DSR: 0.9971) |
| **Profit Concentration Skew** | **61.3% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 61.3% of positive returns |
| **Superstar Asset Fragility** | **999.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 999.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **100.0% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 100.0% of net profit (784.77 RMB) |
| **Capital Drag & Cash Flattery** | **38.5% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 38.5% | Norm MaxDD: 6.4% (vs 3.9% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **-115.37 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (600584.SH)** | **-2,280.09 RMB** | `+1876.4%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (600584.SH, 601899.SH)** | **-4,305.25 RMB** | `+3631.8%` | UNPROFITABLE |
| **Exclude Top 3 (600584.SH, 601899.SH, 600362.SH)** | **-5,146.60 RMB** | `+4361.1%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **Superstar Fragility: Omitting 600584.SH flips Net PnL to negative (-2,280.09 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 61.3% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 100.0% of net profits**

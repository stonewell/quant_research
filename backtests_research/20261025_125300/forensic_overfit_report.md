# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `/home/stone/Work/github/quant/backtests_research/20261025_125300`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 3  

---

## 1. Chan Dual Hybrid Alpha Blend Strategy [Batch 03_r2b_oos]
- **Safety Grade**: `Grade A (Institutional Grade - Highly Robust)`
- **Overfit Risk Index**: `0 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **52.8% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **3.4% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 3.4%) |
| **Statistical Edge (DSR)** | **0.5857** | > 0.0500 | `EXCELLENT` | Deflated Sharpe confirms statistical alpha (DSR: 0.5857) |
| **Profit Concentration Skew** | **28.5% from Fold 3** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 3: 28.5%) |
| **Superstar Asset Fragility** | **69.8% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 69.8% of net gain |
| **Friction Bleed Ratio** | **8.7% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 8.7% of net profit (2,892.59 RMB) |
| **Capital Drag & Cash Flattery** | **39.2% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 39.2% | Norm MaxDD: 6.6% (vs 4.0% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+33,428.77 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (600584.SH)** | **+23,511.40 RMB** | `-29.7%` | VIABLE |
| **Exclude Top 2 (600584.SH, 300394.SZ)** | **+14,327.32 RMB** | `-57.1%` | VIABLE |
| **Exclude Top 3 (600584.SH, 300394.SZ, 600362.SH)** | **+10,084.28 RMB** | `-69.8%` | VIABLE |

---

## 2. Chan Risk-Managed Blend Strategy [Batch 03_r2b_oos]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `40 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **51.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.3% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.3%) |
| **Statistical Edge (DSR)** | **0.0005** | > 0.0500 | `FAIL / MINED` | DSR 0.0005 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **54.1% from Fold 3** | < 40.0% | `WARNING` | Fold 3 generated 54.1% of positive returns |
| **Superstar Asset Fragility** | **51.6% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 51.6% of net gain |
| **Friction Bleed Ratio** | **6.2% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 6.2% of net profit (2,493.45 RMB) |
| **Capital Drag & Cash Flattery** | **30.5% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 30.5% | Norm MaxDD: 6.3% (vs 4.4% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+40,428.21 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (600362.SH)** | **+33,066.31 RMB** | `-18.2%` | VIABLE |
| **Exclude Top 2 (600362.SH, 300394.SZ)** | **+25,917.14 RMB** | `-35.9%` | VIABLE |
| **Exclude Top 3 (600362.SH, 300394.SZ, 601872.SH)** | **+19,586.84 RMB** | `-51.6%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0005 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 3 accounts for 54.1% of positive returns**

---

## 3. Chan Single Crisis Shield Blend Strategy [Batch 03_r2b_oos]
- **Safety Grade**: `Grade A (Institutional Grade - Highly Robust)`
- **Overfit Risk Index**: `0 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **47.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **3.7% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 3.7%) |
| **Statistical Edge (DSR)** | **0.2012** | > 0.0500 | `PASS` | Deflated Sharpe confirms statistical alpha (DSR: 0.2012) |
| **Profit Concentration Skew** | **43.1% from Fold 3** | < 40.0% | `WARNING` | Fold 3 generated 43.1% of positive returns |
| **Superstar Asset Fragility** | **57.7% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 57.7% of net gain |
| **Friction Bleed Ratio** | **7.3% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 7.3% of net profit (2,062.86 RMB) |
| **Capital Drag & Cash Flattery** | **30.7% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 30.7% | Norm MaxDD: 6.2% (vs 4.3% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+28,079.91 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601872.SH)** | **+21,954.24 RMB** | `-21.8%` | VIABLE |
| **Exclude Top 2 (601872.SH, 300394.SZ)** | **+15,988.59 RMB** | `-43.1%` | VIABLE |
| **Exclude Top 3 (601872.SH, 300394.SZ, 600362.SH)** | **+11,870.07 RMB** | `-57.7%` | VIABLE |

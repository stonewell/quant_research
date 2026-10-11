# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_5`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 8  

---

## 1. Chan Single Crisis Shield Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **58.2% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **12.7% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 12.7%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **29.1% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 29.1%) |
| **Superstar Asset Fragility** | **57.9% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 57.9% of net gain |
| **Friction Bleed Ratio** | **11.1% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 11.1% of net profit (4,388.60 RMB) |
| **Capital Drag & Cash Flattery** | **23.3% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 23.3% | Norm MaxDD: 7.3% (vs 5.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+39,451.82 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+29,942.96 RMB** | `-24.1%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 000938.SZ)** | **+23,177.14 RMB** | `-41.3%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 000938.SZ, 601872.SH)** | **+16,607.37 RMB** | `-57.9%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 2. Chan Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **54.4% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **9.3% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 9.3%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **24.3% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 24.3%) |
| **Superstar Asset Fragility** | **65.9% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 65.9% of net gain |
| **Friction Bleed Ratio** | **17.9% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 17.9% of net profit (4,552.02 RMB) |
| **Capital Drag & Cash Flattery** | **20.9% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 20.9% | Norm MaxDD: 7.4% (vs 5.9% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+25,415.19 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601288.SH)** | **+17,900.06 RMB** | `-29.6%` | VIABLE |
| **Exclude Top 2 (601288.SH, 601225.SH)** | **+13,276.72 RMB** | `-47.8%` | VIABLE |
| **Exclude Top 3 (601288.SH, 601225.SH, 601390.SH)** | **+8,661.19 RMB** | `-65.9%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 3. Chan Four-State Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **8.7% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 8.7%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **25.8% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 25.8%) |
| **Superstar Asset Fragility** | **54.0% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 54.0% of net gain |
| **Friction Bleed Ratio** | **15.9% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 15.9% of net profit (4,590.18 RMB) |
| **Capital Drag & Cash Flattery** | **21.3% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 21.3% | Norm MaxDD: 7.6% (vs 6.0% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+28,922.25 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601288.SH)** | **+21,646.99 RMB** | `-25.2%` | VIABLE |
| **Exclude Top 2 (601288.SH, 601390.SH)** | **+16,784.93 RMB** | `-42.0%` | VIABLE |
| **Exclude Top 3 (601288.SH, 601390.SH, 601728.SH)** | **+13,317.43 RMB** | `-54.0%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 4. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **51.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.7% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.7%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **35.3% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 35.3%) |
| **Superstar Asset Fragility** | **67.7% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 67.7% of net gain |
| **Friction Bleed Ratio** | **19.5% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 19.5% of net profit (5,070.62 RMB) |
| **Capital Drag & Cash Flattery** | **33.4% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 33.4% | Norm MaxDD: 7.6% (vs 5.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+26,033.54 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+18,816.71 RMB** | `-27.7%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601288.SH)** | **+13,584.63 RMB** | `-47.8%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601288.SH, 601728.SH)** | **+8,414.32 RMB** | `-67.7%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 5. Chan Single Crisis Shield Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **48.0% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 14.3%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 14.3%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **4.9% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 4.9%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **67.4% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 67.4% of positive returns |
| **Superstar Asset Fragility** | **212.8% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 212.8% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **26.5% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 26.5% of net profit (761.50 RMB) |
| **Capital Drag & Cash Flattery** | **37.2% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 37.2% | Norm MaxDD: 8.3% (vs 5.2% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **+2,870.63 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **-159.48 RMB** | `-105.6%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601899.SH, 000938.SZ)** | **-2,026.61 RMB** | `-170.6%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 000938.SZ, 600362.SH)** | **-3,238.26 RMB** | `-212.8%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601899.SH flips Net PnL to negative (-159.48 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 67.4% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 26.5% of net profits**

---

## 6. Chan Four-State Risk-Managed Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **49.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 15.9%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 15.9%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **3.5% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 3.5%) |
| **Statistical Edge (DSR)** | **0.0344** | > 0.0500 | `FAIL / MINED` | DSR 0.0344 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **70.8% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 70.8% of positive returns |
| **Superstar Asset Fragility** | **999.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 999.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **100.0% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 100.0% of net profit (938.13 RMB) |
| **Capital Drag & Cash Flattery** | **41.3% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 41.3% | Norm MaxDD: 7.0% (vs 4.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **-3,099.34 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (000157.SZ)** | **-4,152.15 RMB** | `+34.0%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (000157.SZ, 601728.SH)** | **-5,141.64 RMB** | `+65.9%` | UNPROFITABLE |
| **Exclude Top 3 (000157.SZ, 601728.SH, 600362.SH)** | **-6,117.35 RMB** | `+97.4%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0344 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 000157.SZ flips Net PnL to negative (-4,152.15 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 70.8% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 100.0% of net profits**

---

## 7. Chan Risk-Managed Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **49.4% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 14.6%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 14.6%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **5.8% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 5.8%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **70.6% from Fold 3** | < 40.0% | `HIGH REGIME RISK` | Fold 3 generated 70.6% of positive returns |
| **Superstar Asset Fragility** | **206.5% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 206.5% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **33.5% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 33.5% of net profit (908.31 RMB) |
| **Capital Drag & Cash Flattery** | **38.5% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 38.5% | Norm MaxDD: 7.4% (vs 4.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **+2,712.65 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (000938.SZ)** | **-78.46 RMB** | `-102.9%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (000938.SZ, 601872.SH)** | **-1,883.53 RMB** | `-169.4%` | UNPROFITABLE |
| **Exclude Top 3 (000938.SZ, 601872.SH, 601728.SH)** | **-2,889.30 RMB** | `-206.5%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 000938.SZ flips Net PnL to negative (-78.46 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 3 accounts for 70.6% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 33.5% of net profits**

---

## 8. Chan Dual Hybrid Alpha Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **44.7% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **4.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 4.2%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **100.0% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 100.0% of positive returns |
| **Superstar Asset Fragility** | **224620.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 224620.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **43887.9% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 43887.9% of net profit (1,009.25 RMB) |
| **Capital Drag & Cash Flattery** | **50.0% Idle Cash** | < 25.0% | `HIGH CASH CUSHION` | Avg idle cash: 50.0% | Norm MaxDD: 7.7% (vs 3.8% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+2.30 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **-2,406.05 RMB** | `-104729.0%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601899.SH, 601872.SH)** | **-3,839.69 RMB** | `-167072.2%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 601872.SH, 601728.SH)** | **-5,163.06 RMB** | `-224620.0%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601899.SH flips Net PnL to negative (-2,406.05 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 100.0% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 43887.9% of net profits**

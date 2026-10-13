# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_16`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 10  

---

## 1. Chan Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **55.8% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **11.1% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 11.1%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **26.9% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 26.9%) |
| **Superstar Asset Fragility** | **59.6% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 59.6% of net gain |
| **Friction Bleed Ratio** | **11.6% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 11.6% of net profit (5,034.91 RMB) |
| **Capital Drag & Cash Flattery** | **22.0% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 22.0% | Norm MaxDD: 7.2% (vs 5.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+43,570.06 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+32,846.43 RMB** | `-24.6%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601225.SH)** | **+23,990.97 RMB** | `-44.9%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601225.SH, 601288.SH)** | **+17,583.39 RMB** | `-59.6%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 2. Chan Single Crisis Shield Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.8% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **13.9% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 13.9%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **29.6% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 29.6%) |
| **Superstar Asset Fragility** | **61.1% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 61.1% of net gain |
| **Friction Bleed Ratio** | **11.6% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 11.6% of net profit (4,724.61 RMB) |
| **Capital Drag & Cash Flattery** | **21.7% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 21.7% | Norm MaxDD: 7.2% (vs 5.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+40,845.59 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+27,606.74 RMB** | `-32.4%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601872.SH)** | **+19,179.73 RMB** | `-53.0%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601872.SH, 601728.SH)** | **+15,887.39 RMB** | `-61.1%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 3. Chan Single Crisis Shield Blend Strategy [Batch 2]
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `55 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **42.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **9.0% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 9.0%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **74.1% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 74.1% of positive returns |
| **Superstar Asset Fragility** | **140.8% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 140.8% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **17.5% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 17.5% of net profit (816.01 RMB) |
| **Capital Drag & Cash Flattery** | **33.1% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 33.1% | Norm MaxDD: 6.9% (vs 4.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+4,662.26 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **+1,404.68 RMB** | `-69.9%` | VIABLE |
| **Exclude Top 2 (601899.SH, 000938.SZ)** | **-1,003.83 RMB** | `-121.5%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 000938.SZ, 601728.SH)** | **-1,902.18 RMB** | `-140.8%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **High Winner Concentration: Top 3 generate 140.8% of net gain**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 74.1% of positive returns**

---

## 4. Chan Four-State Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.7% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **8.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 8.2%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **31.4% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 31.4%) |
| **Superstar Asset Fragility** | **71.5% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 71.5% of net gain |
| **Friction Bleed Ratio** | **15.7% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 15.7% of net profit (5,070.31 RMB) |
| **Capital Drag & Cash Flattery** | **18.9% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 18.9% | Norm MaxDD: 7.6% (vs 6.2% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+32,206.62 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+20,794.66 RMB** | `-35.4%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601225.SH)** | **+13,250.07 RMB** | `-58.9%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601225.SH, 601288.SH)** | **+9,178.85 RMB** | `-71.5%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 5. Chan Four-State Shield Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.6% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **11.6% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 11.6%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **33.1% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 33.1%) |
| **Superstar Asset Fragility** | **62.3% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 62.3% of net gain |
| **Friction Bleed Ratio** | **18.0% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 18.0% of net profit (4,859.66 RMB) |
| **Capital Drag & Cash Flattery** | **25.1% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 25.1% | Norm MaxDD: 7.4% (vs 5.5% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+27,056.46 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+18,260.86 RMB** | `-32.5%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601872.SH)** | **+13,513.91 RMB** | `-50.1%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601872.SH, 601288.SH)** | **+10,211.67 RMB** | `-62.3%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 6. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `50 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **55.4% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **7.8% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 7.8%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **34.3% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 34.3%) |
| **Superstar Asset Fragility** | **84.7% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 84.7% of net gain |
| **Friction Bleed Ratio** | **33.8% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 33.8% of net profit (7,278.56 RMB) |
| **Capital Drag & Cash Flattery** | **32.8% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 32.8% | Norm MaxDD: 7.3% (vs 4.9% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+21,558.75 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+11,869.18 RMB** | `-44.9%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 600362.SH)** | **+7,264.52 RMB** | `-66.3%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 600362.SH, 601225.SH)** | **+3,299.12 RMB** | `-84.7%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **High Winner Concentration: Top 3 generate 84.7% of net gain**
- ⚠️ **Friction Bleed: Transaction friction consumes 33.8% of net profits**

---

## 7. Chan Dual Hybrid Alpha Blend Strategy [Batch 2]
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `65 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **45.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.2%) |
| **Statistical Edge (DSR)** | **0.0010** | > 0.0500 | `FAIL / MINED` | DSR 0.0010 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **97.3% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 97.3% of positive returns |
| **Superstar Asset Fragility** | **170.6% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 170.6% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **58.5% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 58.5% of net profit (1,872.68 RMB) |
| **Capital Drag & Cash Flattery** | **37.8% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 37.8% | Norm MaxDD: 7.5% (vs 4.7% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+3,200.26 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **+185.47 RMB** | `-94.2%` | VIABLE |
| **Exclude Top 2 (601899.SH, 601872.SH)** | **-1,163.38 RMB** | `-136.4%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 601872.SH, 600362.SH)** | **-2,260.78 RMB** | `-170.6%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0010 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **High Winner Concentration: Top 3 generate 170.6% of net gain**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 97.3% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 58.5% of net profits**

---

## 8. Generic Multi-Strategy Blend (Adaptive) [Batch 2]
- **Safety Grade**: `Grade C (High Overfit Risk - Fragile / Over-Parameterized)`
- **Overfit Risk Index**: `65 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **55.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **12.1% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 12.1%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **74.8% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 74.8% of positive returns |
| **Superstar Asset Fragility** | **137.4% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 137.4% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **13.7% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 13.7% of net profit (2,266.49 RMB) |
| **Capital Drag & Cash Flattery** | **30.2% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 30.2% | Norm MaxDD: 9.8% (vs 6.8% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+16,506.07 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601872.SH)** | **-385.88 RMB** | `-102.3%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601872.SH, 000938.SZ)** | **-4,264.53 RMB** | `-125.8%` | UNPROFITABLE |
| **Exclude Top 3 (601872.SH, 000938.SZ, 601601.SH)** | **-6,170.24 RMB** | `-137.4%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601872.SH flips Net PnL to negative (-385.88 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 74.8% of positive returns**

---

## 9. Generic Multi-Strategy Blend (Adaptive) [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `35 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **52.2% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.9% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.9%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **32.9% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 32.9%) |
| **Superstar Asset Fragility** | **66.4% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 66.4% of net gain |
| **Friction Bleed Ratio** | **58.6% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 58.6% of net profit (18,132.90 RMB) |
| **Capital Drag & Cash Flattery** | **20.0% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 20.0% | Norm MaxDD: 9.1% (vs 7.3% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 22 Assets)** | **+30,929.27 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601288.SH)** | **+23,093.95 RMB** | `-25.3%` | VIABLE |
| **Exclude Top 2 (601288.SH, 600362.SH)** | **+16,724.29 RMB** | `-45.9%` | VIABLE |
| **Exclude Top 3 (601288.SH, 600362.SH, 300394.SZ)** | **+10,380.06 RMB** | `-66.4%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Friction Bleed: Transaction friction consumes 58.6% of net profits**

---

## 10. Chan Four-State Shield Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **47.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **4.3% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 4.3%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **81.1% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 81.1% of positive returns |
| **Superstar Asset Fragility** | **213.6% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 213.6% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **27.2% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 27.2% of net profit (804.32 RMB) |
| **Capital Drag & Cash Flattery** | **34.6% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 34.6% | Norm MaxDD: 7.7% (vs 5.0% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **+2,960.48 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **-1,173.15 RMB** | `-139.6%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601899.SH, 000938.SZ)** | **-2,369.29 RMB** | `-180.0%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 000938.SZ, 601728.SH)** | **-3,361.86 RMB** | `-213.6%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601899.SH flips Net PnL to negative (-1,173.15 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 81.1% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 27.2% of net profits**

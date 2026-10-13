# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_15`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 5  

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

## 3. Chan Four-State Risk-Managed Blend Strategy [Batch 1]
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

## 4. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
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

## 5. Generic Multi-Strategy Blend (Adaptive) [Batch 1]
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

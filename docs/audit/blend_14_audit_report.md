# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend_14`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 5  

---

## 1. Chan Single Crisis Shield Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **59.0% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 19.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 19.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **15.4% Single Jump** | < 30.0% | `MODERATE` | Single rebalance jump of 15.4% |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **30.4% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 30.4%) |
| **Superstar Asset Fragility** | **52.5% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 52.5% of net gain |
| **Friction Bleed Ratio** | **9.5% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 9.5% of net profit (4,413.97 RMB) |
| **Capital Drag & Cash Flattery** | **23.5% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 23.5% | Norm MaxDD: 7.3% (vs 5.6% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+46,340.16 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+35,174.23 RMB** | `-24.1%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 000938.SZ)** | **+28,408.41 RMB** | `-38.7%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 000938.SZ, 601872.SH)** | **+22,009.12 RMB** | `-52.5%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 2. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **50.6% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.6% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.6%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **31.9% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 31.9%) |
| **Superstar Asset Fragility** | **73.1% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 73.1% of net gain |
| **Friction Bleed Ratio** | **19.9% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 19.9% of net profit (4,983.60 RMB) |
| **Capital Drag & Cash Flattery** | **32.4% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 32.4% | Norm MaxDD: 7.5% (vs 5.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+25,101.12 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+17,888.02 RMB** | `-28.7%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601288.SH)** | **+12,241.33 RMB** | `-51.2%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601288.SH, 000938.SZ)** | **+6,761.87 RMB** | `-73.1%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 3. Chan Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **54.2% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **9.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 9.2%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **29.5% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 29.5%) |
| **Superstar Asset Fragility** | **64.9% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 64.9% of net gain |
| **Friction Bleed Ratio** | **13.6% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 13.6% of net profit (4,542.61 RMB) |
| **Capital Drag & Cash Flattery** | **21.3% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 21.3% | Norm MaxDD: 7.7% (vs 6.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+33,318.87 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+23,486.64 RMB** | `-29.5%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601288.SH)** | **+16,871.87 RMB** | `-49.4%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601288.SH, 601225.SH)** | **+11,704.09 RMB** | `-64.9%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 4. Chan Four-State Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **56.2% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **9.0% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 9.0%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **29.4% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 29.4%) |
| **Superstar Asset Fragility** | **75.2% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 75.2% of net gain |
| **Friction Bleed Ratio** | **18.0% Fee / Net PnL** | < 15.0% | `ELEVATED` | Friction fees consumed 18.0% of net profit (4,586.85 RMB) |
| **Capital Drag & Cash Flattery** | **21.5% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 21.5% | Norm MaxDD: 8.0% (vs 6.3% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+25,510.46 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+16,760.92 RMB** | `-34.3%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601288.SH)** | **+11,357.01 RMB** | `-55.5%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601288.SH, 601390.SH)** | **+6,332.09 RMB** | `-75.2%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 5. Generic Multi-Strategy Blend (Adaptive) [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `35 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **52.1% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **10.4% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 10.4%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **32.6% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 32.6%) |
| **Superstar Asset Fragility** | **68.0% from Top 3** | < 60.0% | `WARNING` | Top 3 assets contributed 68.0% of net gain |
| **Friction Bleed Ratio** | **58.5% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 58.5% of net profit (16,762.97 RMB) |
| **Capital Drag & Cash Flattery** | **18.9% Idle Cash** | < 25.0% | `LOW` | Avg idle cash: 18.9% | Norm MaxDD: 8.7% (vs 7.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 22 Assets)** | **+28,677.34 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601288.SH)** | **+21,255.73 RMB** | `-25.9%` | VIABLE |
| **Exclude Top 2 (601288.SH, 601225.SH)** | **+15,061.80 RMB** | `-47.5%` | VIABLE |
| **Exclude Top 3 (601288.SH, 601225.SH, 601728.SH)** | **+9,167.69 RMB** | `-68.0%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Friction Bleed: Transaction friction consumes 58.5% of net profits**

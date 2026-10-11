# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `pipeline/results/blend`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: 8  

---

## 1. Chan Four-State Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **57.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **7.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 7.2%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **38.7% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 38.7%) |
| **Superstar Asset Fragility** | **46.6% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 46.6% of net gain |
| **Friction Bleed Ratio** | **11.5% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 11.5% of net profit (4,800.13 RMB) |
| **Capital Drag & Cash Flattery** | **43.1% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 43.1% | Norm MaxDD: 7.0% (vs 4.0% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+41,741.84 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+32,511.49 RMB** | `-22.1%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 000938.SZ)** | **+27,047.34 RMB** | `-35.2%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 000938.SZ, 601728.SH)** | **+22,295.43 RMB** | `-46.6%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 2. Chan Risk-Managed Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **57.0% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.5% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.5%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **28.2% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 28.2%) |
| **Superstar Asset Fragility** | **53.9% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 53.9% of net gain |
| **Friction Bleed Ratio** | **14.8% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 14.8% of net profit (4,789.67 RMB) |
| **Capital Drag & Cash Flattery** | **40.4% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 40.4% | Norm MaxDD: 6.9% (vs 4.1% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+32,438.22 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+23,209.60 RMB** | `-28.4%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601288.SH)** | **+18,834.29 RMB** | `-41.9%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601288.SH, 601728.SH)** | **+14,957.39 RMB** | `-53.9%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 3. Chan Dual Hybrid Alpha Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **58.5% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 19.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 19.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **11.8% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 11.8%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **32.2% from Fold 5** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 5: 32.2%) |
| **Superstar Asset Fragility** | **53.2% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 53.2% of net gain |
| **Friction Bleed Ratio** | **12.6% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 12.6% of net profit (3,538.00 RMB) |
| **Capital Drag & Cash Flattery** | **36.4% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 36.4% | Norm MaxDD: 6.7% (vs 4.3% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+28,145.69 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (300394.SZ)** | **+20,545.64 RMB** | `-27.0%` | VIABLE |
| **Exclude Top 2 (300394.SZ, 601728.SH)** | **+16,485.32 RMB** | `-41.4%` | VIABLE |
| **Exclude Top 3 (300394.SZ, 601728.SH, 601919.SH)** | **+13,185.29 RMB** | `-53.2%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 4. Chan Dual Hybrid Alpha Blend Strategy [Batch 2]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `30 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **49.3% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 17.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 17.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **5.0% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 5.0%) |
| **Statistical Edge (DSR)** | **0.1268** | > 0.0500 | `PASS` | Deflated Sharpe confirms statistical alpha (DSR: 0.1268) |
| **Profit Concentration Skew** | **59.9% from Fold 1** | < 40.0% | `WARNING` | Fold 1 generated 59.9% of positive returns |
| **Superstar Asset Fragility** | **121.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 121.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **13.1% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 13.1% of net profit (785.85 RMB) |
| **Capital Drag & Cash Flattery** | **45.4% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 45.4% | Norm MaxDD: 8.2% (vs 4.5% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **+5,988.09 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (000938.SZ)** | **+3,510.63 RMB** | `-41.4%` | VIABLE |
| **Exclude Top 2 (000938.SZ, 601899.SH)** | **+1,047.11 RMB** | `-82.5%` | VIABLE |
| **Exclude Top 3 (000938.SZ, 601899.SH, 601872.SH)** | **-1,257.56 RMB** | `-121.0%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **High Winner Concentration: Top 3 generate 121.0% of net gain**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 59.9% of positive returns**

---

## 5. Chan Single Crisis Shield Blend Strategy [Batch 1]
- **Safety Grade**: `Grade B (Moderate Caution - Viable with Sizing Rules)`
- **Overfit Risk Index**: `25 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **59.8% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 19.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 19.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **6.3% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 6.3%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **30.6% from Fold 9** | < 40.0% | `CLEAN` | Evenly distributed across folds (Fold 9: 30.6%) |
| **Superstar Asset Fragility** | **41.2% from Top 3** | < 60.0% | `ROBUST` | Top 3 assets contributed 41.2% of net gain |
| **Friction Bleed Ratio** | **11.0% Fee / Net PnL** | < 15.0% | `MINIMAL` | Friction fees consumed 11.0% of net profit (3,391.34 RMB) |
| **Capital Drag & Cash Flattery** | **44.2% Idle Cash** | < 25.0% | `MODERATE` | Avg idle cash: 44.2% | Norm MaxDD: 7.2% (vs 4.0% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 21 Assets)** | **+30,947.72 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601601.SH)** | **+26,039.10 RMB** | `-15.9%` | VIABLE |
| **Exclude Top 2 (601601.SH, 300394.SZ)** | **+21,474.32 RMB** | `-30.6%` | VIABLE |
| **Exclude Top 3 (601601.SH, 300394.SZ, 601288.SH)** | **+18,201.31 RMB** | `-41.2%` | VIABLE |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**

---

## 6. Chan Single Crisis Shield Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **50.8% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 12.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 12.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **4.2% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 4.2%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **80.5% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 80.5% of positive returns |
| **Superstar Asset Fragility** | **999.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 999.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **100.0% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 100.0% of net profit (654.58 RMB) |
| **Capital Drag & Cash Flattery** | **54.3% Idle Cash** | < 25.0% | `HIGH CASH CUSHION` | Avg idle cash: 54.3% | Norm MaxDD: 10.3% (vs 4.7% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **-1,369.91 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601899.SH)** | **-2,867.46 RMB** | `+109.3%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601899.SH, 601728.SH)** | **-3,761.82 RMB** | `+174.6%` | UNPROFITABLE |
| **Exclude Top 3 (601899.SH, 601728.SH, 601872.SH)** | **-4,493.40 RMB** | `+228.0%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601899.SH flips Net PnL to negative (-2,867.46 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 80.5% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 100.0% of net profits**

---

## 7. Chan Risk-Managed Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **43.0% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 19.8%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 19.8%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **2.6% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 2.6%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **100.0% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 100.0% of positive returns |
| **Superstar Asset Fragility** | **999.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 999.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **100.0% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 100.0% of net profit (1,013.47 RMB) |
| **Capital Drag & Cash Flattery** | **51.2% Idle Cash** | < 25.0% | `HIGH CASH CUSHION` | Avg idle cash: 51.2% | Norm MaxDD: 9.6% (vs 4.7% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 19 Assets)** | **-4,513.70 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601728.SH)** | **-5,881.87 RMB** | `+30.3%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601728.SH, 600000.SH)** | **-6,537.75 RMB** | `+44.8%` | UNPROFITABLE |
| **Exclude Top 3 (601728.SH, 600000.SH, 601872.SH)** | **-7,129.63 RMB** | `+58.0%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601728.SH flips Net PnL to negative (-5,881.87 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 100.0% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 100.0% of net profits**

---

## 8. Chan Four-State Risk-Managed Blend Strategy [Batch 2]
- **Safety Grade**: `Grade F (Rejected - Severe Overfit / Structural Dependency)`
- **Overfit Risk Index**: `75 / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
| **Look-Ahead Bias Check** | **47.9% Hit Rate** | 45.0% - 55.0% | `CLEAN` | Realistic trend hit rate; no forward leakage |
| **Single-Stock Concentration** | **Max Pos: 20.0%** | < 25.0% (Cap: 20%) | `CLEAN` | Strict position caps (Max pos: 20.0%) |
| **Fold 1 Warmup Padding** | **+0 Days** | < 60 Days | `CLEAN` | Immediate trade execution (+0d) |
| **Outlier Equity Jumps** | **2.7% Single Jump** | < 30.0% | `CLEAN` | Equity curve grew incrementally (Max: 2.7%) |
| **Statistical Edge (DSR)** | **0.0000** | > 0.0500 | `FAIL / MINED` | DSR 0.0000 indicates high data-mining / noise risk |
| **Profit Concentration Skew** | **100.0% from Fold 1** | < 40.0% | `HIGH REGIME RISK` | Fold 1 generated 100.0% of positive returns |
| **Superstar Asset Fragility** | **999.0% from Top 3** | < 60.0% | `HIGH FRAGILITY` | Top 3 contributed 999.0% (losers drained remaining gain) |
| **Friction Bleed Ratio** | **100.0% Fee / Net PnL** | < 15.0% | `SEVERE BLEED` | Friction fees consumed 100.0% of net profit (927.39 RMB) |
| **Capital Drag & Cash Flattery** | **50.3% Idle Cash** | < 25.0% | `HIGH CASH CUSHION` | Avg idle cash: 50.3% | Norm MaxDD: 8.9% (vs 4.4% raw) |

### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
| **Baseline (All 20 Assets)** | **-2,903.42 RMB** | `+0.0%` | Normal Operation |
| **Exclude Top 1 (601728.SH)** | **-4,105.47 RMB** | `+41.4%` | FLIPS UNPROFITABLE (CRITICAL) |
| **Exclude Top 2 (601728.SH, 600941.SH)** | **-4,647.86 RMB** | `+60.1%` | UNPROFITABLE |
| **Exclude Top 3 (601728.SH, 600941.SH, 601872.SH)** | **-5,017.30 RMB** | `+72.8%` | SEVERE LOSS DRAIN |

### Identified Overfitting & Fragility Alerts
- ⚠️ **DSR 0.0000 < 0.05 (High probability of data-mining / noise)**
- ⚠️ **Superstar Fragility: Omitting 601728.SH flips Net PnL to negative (-4,105.47 RMB)**
- ⚠️ **One-Hit Wonder Fold: Best Fold 1 accounts for 100.0% of positive returns**
- ⚠️ **Friction Bleed: Transaction friction consumes 100.0% of net profits**

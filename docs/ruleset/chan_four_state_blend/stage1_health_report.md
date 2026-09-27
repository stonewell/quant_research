# Stage 1 Live Pre-Trade Health & Macro Audit Report

> **Valuation Date**: `2026-09-27`  
> **Gate Directive**: **🟢 GO: STEADY BULL BREADTH REGIME**  
> **Report Timestamp**: Generated during 14:00 – 14:15 inspection window  

---

## 1. Portfolio Health & Drawdown Circuit Breakers

| Metric | Current Value | Threshold / Target | Status |
| :--- | :--- | :--- | :--- |
| **Current Portfolio NAV** | `100,000.00` | — | Tracking |
| **High-Water Mark (HWM)** | `100,000.00` | — | All-Time Peak |
| **Peak-to-Trough Drawdown** | `0.00%` | Tier 1: -10% / Tier 2: -15% / Tier 3: -20% | **🟢 NORMAL (Full Risk Budget)** |
| **Linear Equity Scale** | `100.0%` | Smooth linear damping | Continuous exposure factor |
| **Tier 1 Cooldown Counter** | `0 / 15` | 15 Bars | Monitoring |
| **Tier 3 Cooldown & Freeze** | `0 bars left` | 21 Bars lockout | Clear |
| **10-Day NAV Recovery** | `+0.00%` | > 0.0% | ⚪ None |

---

## 2. Universe Market Breadth & Momentum Thrust

| Indicator | Calculated | Benchmark Threshold | Regime Assessment |
| :--- | :--- | :--- | :--- |
| **50-Day Market Breadth** | `40.9%` | `30.0%` | 🟢 Bullish Breadth |
| **10-Day Momentum Thrust** | `45.5%` | `60.0%` | ⚪ Standard |
| **Target Equity Exposure** | `65%` | Standard: 50% / Bull: Up to 80% | Bullish Expansion |
| **Single-Stock Cap** | `20.0%` | Fixed hard limit | Enforced across all regimes |

### Top Momentum Leaders (10-Day ROC)
1. **`601919.SH`**: 10d ROC = `+6.05%` (Price: `151.38`, Above SMA50: `True`)
2. **`601899.SH`**: 10d ROC = `+4.38%` (Price: `162.66`, Above SMA50: `True`)
3. **`601398.SH`**: 10d ROC = `+3.46%` (Price: `119.54`, Above SMA50: `True`)
4. **`600660.SH`**: 10d ROC = `+2.62%` (Price: `123.96`, Above SMA50: `True`)
5. **`600362.SH`**: 10d ROC = `+2.07%` (Price: `117.24`, Above SMA50: `True`)

---

## 3. Realized Volatility Targeting

- **21-Day Realized Volatility**: `3.3%` (Target Vol: `12.0%`)
- **Barroso Volatility Scaling**: `1.00x`
- **Assessment**: 🟢 Realized market variance within standard bounds.

---

## 4. Mandatory Human Trader Actions (Stage 2 Execution)

1. 50-Day Market Breadth Active (40.9% >= 30%).
2. Target equity exposure dynamically deployed up to 65%.

---
*Report exported automatically by `scripts/check_live_portfolio_health.py`*

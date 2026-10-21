# Stage 1 Live Pre-Trade Health & Macro Audit Report

> **Valuation Date**: `2026-10-20`  
> **Strategy**: **Chan Four-State Risk-Managed Blend Strategy** (`chan_four_state_blend`)  
> **Strategy Version**: `v1.0.0 [base: v1.4.0]`  
> **Weighting & Ortho**: `rolling_ic_ir` | Orthogonalized: `True`  
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
| **50-Day Market Breadth** | `36.4%` | `30.0%` | 🟢 Bullish Breadth |
| **10-Day Momentum Thrust** | `36.4%` | `60.0%` | ⚪ Standard |
| **Target Equity Exposure** | `63%` | Standard: 50% / Bull: Up to 80% | Bullish Expansion |
| **Single-Stock Cap** | `20.0%` | Fixed hard limit | Enforced across all regimes |

### Top Momentum Leaders (10-Day ROC)
1. **`601899.SH`**: 10d ROC = `+7.64%` (Price: `166.08`, Above SMA50: `True`)
2. **`601919.SH`**: 10d ROC = `+4.60%` (Price: `151.79`, Above SMA50: `True`)
3. **`601601.SH`**: 10d ROC = `+2.19%` (Price: `192.45`, Above SMA50: `True`)
4. **`601398.SH`**: 10d ROC = `+2.15%` (Price: `120.12`, Above SMA50: `True`)
5. **`600660.SH`**: 10d ROC = `+1.78%` (Price: `123.13`, Above SMA50: `True`)

---

## 3. Realized Volatility Targeting

- **21-Day Realized Volatility**: `9.6%` (Target Vol: `12.0%`)
- **Barroso Volatility Scaling**: `1.00x`
- **Assessment**: 🟢 Realized market variance within standard bounds.

---

## 4. Mandatory Human Trader Actions (Stage 2 Execution)

1. 50-Day Market Breadth Active (36.4% >= 30%).
2. Target equity exposure dynamically deployed up to 63%.

---
*Report exported automatically by `scripts/check_live_portfolio_health.py`*

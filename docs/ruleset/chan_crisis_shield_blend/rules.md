# Chan Crisis Shield Blend Strategy: Live Trading Operating Manual & Rulebook (SOP)

> **Strategy Identifier**: `chan_crisis_shield_blend` ([`ChanCrisisShieldBlendStrategy`](file:///home/stone/Work/github/quant/pipeline/research/blend/chan_crisis_shield_blend.py#L20-L104))  
> **Live Readiness Score**: **80.5 / 100 (Grade B Tactical Aggressive Live Ready / High Return Potential)**  
> **Daily Execution Tool**: [`scripts/run_live_strategy.py`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py)  
> **Target Instruments**: High-liquidity Core-Satellite Equity Universe (22 curated assets) + Cash Equivalents (`511880.SH`, `511990.SH`, `GC001` / `R-001` repo sweep; US / synthetic backtests use `BIL`)  
> **Execution Routine**: Evaluated daily at **14:00**, staged execution between **14:20 – 14:45**, idle cash sweep between **14:55 – 15:00** (avoiding open auction volatility and market-on-close distortions).  
> **Underlying Factors**: `regime_trend_strength`, `absolute_momentum_trend`, `relative_momentum`, `volatility_targeting`, `mean_reversion`  
> **Walkforward Empirical Audit Summary**:
> - **Explosive Annualized Alpha**: Demonstrates top raw CAGR and Sharpe across multiple rolling windows: `blend_8/2` CAGR **+49.7%** (Sharpe 1.349, Calmar 11.94), `blend_12/2` CAGR **+41.9%** (Sharpe 1.295, Calmar 12.91), `blend_13/2` CAGR **+31.8%** (Sharpe 1.607, Calmar 9.07);
> - **Tight Tail Drawdown Defense**: Anchored by 35% Keller VAA canary cushion, capping mean maximum drawdowns to **4.3% ~ 4.7%**;
> - **Live Execution Caveat**: Returns are concentrated around high-volatility volume-spike reversals (Alpha#12); disciplined limit-order placement is mandatory to prevent execution slippage.

---

## 1. Strategy Identity & Architecture Overview

The **Chan Crisis Shield Blend Strategy** is an institutional multi-strategy portfolio engineered to capture high-beta recovery rebounds following panic sell-offs while maintaining strict downside preservation:
- **Chan Three-Type Strategy (45%)**: Structural pivot trend alpha capturing 1B, 2B, and 3B buy points ($B_1, B_2, B_3$);
- **Chan VAA Compound Strategy (35%)**: Keller 13612W canary defensive cushion protecting capital against broad macro downtrends;
- **WorldQuant Alpha#12 Volume Shock Reversal (20%)**:
  $$\text{Alpha\#12} = \text{sign}(\Delta \text{Volume}) \times (-\Delta \text{Close})$$
  Detects institutional capitulation where volume surges on down days, initiating mean-reversion entries on exhausted selling;
- **Unified Risk Controls**: Rolling 60d Bayesian IC-IR weighting ($t \ge 2.0$), Löwdin symmetric orthogonalization, Garman-Klass volatility targeting (12%), and multi-tier drawdown circuit breakers with 15-day auto-healing cooldowns.

```mermaid
flowchart TD
    Universe["Curated Universe (22 Core-Satellite Assets + Cash Proxy)"] --> SubAlphas["Sub-Strategy Signal Generators"]
    
    subgraph Ensemble ["Sub-Strategy Core Ensemble"]
        SubAlphas --> S1["Chan Three-Type Strategy (45%)\n(Structural 1B/2B/3B Trend Alpha)"]
        SubAlphas --> S2["Chan VAA Compound Strategy (35%)\n(Keller 13612W Canary Crash Cushion)"]
        SubAlphas --> S3["WQ Alpha#12 Volume Shock Reversal (20%)\nsign(Delta Vol) * (-Delta Close)"]
    end
    
    S1 & S2 & S3 --> Ortho["Löwdin Symmetric Orthogonalization\nW* = W(W^T W)^(-1/2)"]
    Ortho --> BayesianIC["Rolling 60d IC-IR Bayesian Adaptive Weighting\n(HLZ t >= 2.0 Significance Hurdle)"]
    BayesianIC --> RawBlend["Target Equity Allocation Vector"]
    
    RawBlend --> DDGate{"High-Water Mark (HWM) Drawdown Gate"}
    DDGate -- "DD >= 20% (Tier 3 Emergency Halt)" --> HardStop["Full Liquidation -> 100% Cash Proxy\nMandatory 15-Day Trading Freeze"]
    DDGate -- "10% <= DD < 20% (Tier 1 Linear Damping)" --> LinearDamp["Smooth Linear Exposure Reduction\n15-Day Auto-Healing Cooldown"]
    DDGate -- "DD < 10% (Normal Operations)" --> BreadthEngine{"Market Breadth & Thrust Engine\n(Breadth >= 30% or 10d Thrust >= 60%)"}
    
    BreadthEngine -- "Bull Breadth Active" --> BullExposure["Dynamic Exposure Expansion (65%~80% Equity Exposure)"]
    BreadthEngine -- "Neutral / Bearish" --> DefensiveExposure["Enforce Standard Hard Cap (Max 20% NAV per Stock)"]
    BullExposure --> SizingCap["Position Sizing Hard Caps (Max 20% per Stock)"]
    DefensiveExposure --> SizingCap
    LinearDamp --> SizingCap
    
    SizingCap --> VolTarget{"Garman-Klass Volatility Targeting (12% Target Vol)"}
    VolTarget --> FrictionFilter{"Adaptive Inertia Filter (|Delta W| >= 5%)"}
    
    FrictionFilter -- "Pass / Liquidation" --> ExecutionEngine["Order Execution Hierarchy:\n1. Phase 1: SELLS FIRST (free cash)\n2. Phase 2: BUYS SECOND (lot-rounded)\n3. Phase 3: POSITIONS HELD (inertia filtered)\n4. Phase 4: CASH SWEEP (overnight repo)"]
    FrictionFilter -- "Delta W < 5%" --> HoldPrior["Maintain Existing Shares (Zero Trade Friction)"]
    ExecutionEngine --> RepoSweep["14:55 Idle Cash Sweep into GC001 / 511880"]
```

---

## 2. Daily Live Trading Routine (Operational SOP Timetable)

Human traders must execute the following 6-step checklist sequentially every trading day:

1. **14:00 – 14:15: Stage 1 Portfolio Health & Macro Regime Inspection**
   Run the pre-trade health gate:
   ```bash
   uv run python scripts/check_live_portfolio_health.py --data-provider marketdb
   ```
   Inspect High-Water Mark (HWM) peak-to-trough drawdown, circuit breaker tier, and market breadth.
2. **14:15 – 14:25: Stage 2 Order Ticket Generation**
   If Stage 1 directive is **🟢 GO** or **🟡 CAUTION**, generate live order tickets:
   ```bash
   uv run python scripts/run_live_strategy.py \
     --strategy chan_crisis_shield_blend \
     --current-holdings-file docs/ruleset/chan_crisis_shield_blend/current_holdings.json \
     --portfolio-value <ACCOUNT_NAV> \
     --data-provider marketdb
   ```
3. **14:25 – 14:35: Phase 1 — Execute Sells First**
   Transmit all red `SELL` orders first to unlock cash purchasing power.
4. **14:35 – 14:45: Phase 2 — Execute Buys Second**
   Deploy freed cash into green `BUY` orders. Round share quantities down to board lot size (100 shares for A-shares). Skip positions displaying `⚠️ Below minimum board lot`.
   *Execution note for Alpha#12*: Place limit orders pegged to best bid/ask to avoid chasing volatility spikes.
5. **14:45 – 14:50: Phase 3 — Verify Inertia Holds**
   Confirm that all white `HOLD` positions (weight changes $< 5\%$) remain untouched.
6. **14:55 – 15:00: Phase 4 — Cash Proxy & Overnight Yield Sweep**
   Lend any remaining idle cash into Treasury repo (`GC001` / `R-001`) or currency ETFs (`511880.SH`).

---

## 3. Decision Logic & Rulebook

### Rule 1: Drawdown Circuit Breakers & Cooldown Recovery
$$\text{Drawdown} = \frac{\text{Current NAV} - \text{Peak NAV}}{\text{Peak NAV}}$$

* **Normal Regime ($\text{Drawdown} < 10\%$)**: Full risk budget active. Single-stock position cap = 20% NAV.
* **Tier 1 Smooth Damping ($10\% \le \text{Drawdown} < 15\%$)**: Linearly scale down target equity exposure:
  $$\text{Scale} = \max\left(0.50, 1.0 - 5.0 \times (\text{Drawdown} - 0.10)\right)$$
  Engage 15-day auto-healing cooldown tracker.
* **Tier 2 Tactical Defense ($15\% \le \text{Drawdown} < 20\%$)**: Cap total equity exposure at 30%; allocate 70% to Cash Proxy.
* **Tier 3 Emergency Halt ($\text{Drawdown} \ge 20\%$)**: Immediately liquidate 100% of all risky holdings into Cash Proxy. Enforce 15 trading days mandatory freeze.

### Rule 2: Alpha#12 Reversal Execution Safeguard
* **Entry Condition**: $\Delta \text{Close} < 0$ and $\Delta \text{Volume} > 0$, without breaking below the 3.0 ATR trailing channel.
* **Position Ceiling**: Hard cap at 20% NAV per stock to prevent single-asset tail-risk traps.
* **Order Style**: Limit orders strictly pegged to opposite-side bid/ask. No market orders.

### Rule 3: 5% Adaptive Inertia Filter ($|\Delta W| \ge 5\%$)
$$\Delta W = |W_{\text{target}} - W_{\text{current}}|$$

* **Standard Rebalance**: If $\Delta W < 5\%$, do NOT trade. Maintain existing position to eliminate unnecessary turnover friction.
* **Complete Exit Override**: If target weight is $0.0\%$, liquidate all shares immediately without applying the 5% threshold.

---

## 4. Human Trader Quick-Reference Card

| Phase | Metric / Trigger | Action / Order Instruction |
| :--- | :--- | :--- |
| **Stage 1: Halt** | $\text{Drawdown} \ge 20\%$ | 🔴 **Liquidate 100% of risk assets**; lock account for 15 days |
| **Stage 1: Damping**| $10\% \le \text{Drawdown} < 15\%$ | 🟡 **Smooth linear equity scaling**; engage 15-day cooldown counter |
| **Stage 1: Bull** | $\text{Breadth} \ge 30\%$ or $\text{Thrust} \ge 60\%$ | 🟢 **Bull exposure active**; deploy cash into top momentum leaders |
| **Stage 2: Sells** | Structural sell / ATR stop / target reduction | 🔴 **SELL FIRST** (14:20–14:35); execute at bid price to release cash |
| **Stage 2: Buys** | Alpha#12 reversal or Three-Type buy $\ge 5\%$ | 🟢 **BUY SECOND** (14:35–14:45); floor to 100-share integer lot |
| **Stage 2: Sub-lot** | Required shares $< 100$ | ⚪ **Skip order**; wait for larger allocation or keep prior shares |
| **Stage 2: Inertia** | Target change $|\Delta W| < 5\%$ | ⚪ **HOLD position**; zero trading friction |
| **Stage 2: Cash** | Idle cash after 14:55 | 💰 **Sweep into GC001 / R-001 repo**; earn overnight risk-free yield |

---

## 5. Standard CLI Invocation

```bash
uv run python scripts/run_live_strategy.py \
  --strategy chan_crisis_shield_blend \
  --universe-file docs/universe/china/core_satellite_22_stocks.txt \
  --output-dir docs/ruleset/chan_crisis_shield_blend \
  --portfolio-value 100000 \
  --data-provider marketdb
```

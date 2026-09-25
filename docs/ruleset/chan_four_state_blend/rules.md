# Chan Four-State Risk-Managed Blend Strategy: Live Trading Operating Manual & Rulebook

> **Strategy Identifier**: `chan_four_state_blend` ([`ChanFourStateBlendStrategy`](file:///home/stone/Work/github/quant/pipeline/research_strategy/rs/chan_advanced_strategies.py#L1768-L2078))  
> **Instrument Context**: Equity / ETF Universe (US Equities or China A-Shares) + Cash Proxy ([`BIL`](file:///home/stone/Work/github/quant/pipeline/research_strategy/strategies_config.json) / `511880` / `511990` / Overnight Repo)  
> **Trading Frequency**: Daily rebalance evaluation, executing primarily between **14:00 – 14:50** (to avoid open auction volatility and end-of-day market-on-close distortions).  
> **Underlying Factors**: `regime_trend_strength`, `absolute_momentum_trend`, `relative_momentum`, `volatility_targeting`

---

## 1. Strategy Identity & Architecture Overview

The **Chan Four-State Risk-Managed Blend Strategy** is an institutional multi-strategy portfolio based directly on the validated `ChanRiskManagedBlendStrategy` architecture, but replaces the multi-stage composite scaling sleeve with the industrial 4-state operational execution machine (`ChanFourStateExecutionStrategy`):

### Core Model Triad & Baseline Allocations
* **40% `ChanVaaCompoundStrategy`**: Dual-momentum VAA-G4 13612W macro regime crash protection buffer & defensive anchor.
* **40% `ChanThreeTypeStrategy`**: Standard segment-level pivot structural breakout and divergence engine.
* **20% `ChanFourStateExecutionStrategy`**: Industrial-grade 4-state operational execution machine (BUY_CANDIDATE, HOLD, HOLD_ALERT, SELL_EXIT, WAIT_OBSERVE) incorporating:
  - 5-bar gestation buffer (`chan_fse_min_hold_bars = 5`) to allow structural pivots to develop without Day 1-2 churn.
  - 1% breakout tolerance on ZG (`chan_fse_zg_tolerance_pct = 0.01`) to resist false breakdown noise.
  - Moving average entanglement filter (`chan_fse_use_ma_filter = True`) to reject whipsaw consolidations.
  - Lesson 16 zero-consolidation stagnation avoidance timeout (`chan_fse_cons_timeout_bars = 8`).

```mermaid
flowchart TD
    Universe["Universe Data (Risky + Cash Proxy)"] --> Sub1["ChanVaaCompound (40%)<br>Dual-momentum crash protection"]
    Universe --> Sub2["ChanThreeType (40%)<br>Segment pivot structural alpha"]
    Universe --> Sub3["ChanFourStateExecution (20%)<br>4-state FSM execution machine"]
    
    Sub1 & Sub2 & Sub3 --> Blend["Raw Weighted Target Weights"]
    
    Blend --> DDCheck{"Portfolio Drawdown from HWM"}
    
    DDCheck -- "DD < 10%" --> NormalMode["Normal Regime"]
    DDCheck -- "10% <= DD < 20%" --> SmoothDD["Smooth Drawdown Damping (10%~20%)<br>scale = 1.0 - (DD-10%)/10%"]
    DDCheck -- "DD >= 20%" --> Tier3["Tier 3: 100% Cash Stop (21-bar Cooldown)"]
    
    NormalMode --> BreadthCheck{"Market Breadth >= 30%?<br>(Close > SMA50)<br>OR 10d Thrust >= 60%"}
    BreadthCheck -- Yes --> DynamicCash["Dynamic Cash Deployment<br>Scale equity up to 80%<br>Strict Single-Stock Cap <= 20%<br>Thrust dispersed across >= 5 leaders"]
    BreadthCheck -- No --> BaseCap["Hard Single-Stock Cap (20%)"]
    
    DynamicCash & BaseCap & SmoothDD & Tier3 --> VolTarget["Volatility Targeting (12% Target)<br>scale = min(1.0, 12% / 21d realized vol)"]
    VolTarget --> InertiaFilter{"Asset Inertia Filter<br>|Delta W| >= 4%<br>(Emergency sells bypass at 0.1%)"}
    InertiaFilter -- "Pass" --> OrderExec["Execution: 1. Sells First -> 2. Buys Second"]
    InertiaFilter -- "Fail" --> HoldPrior["Hold Prior Positions (No Trade)"]
```

---

## 2. Daily Live Trading Routine (Operational Checklist)

A human trader must follow this 5-step daily routine in strict sequential order before placing any trades:

```mermaid
flowchart LR
    T1["Step 1: Drawdown & Smooth Damping"] --> T2["Step 2: Breadth & Volatility Targeting"]
    T2 --> T3["Step 3: Execute Sells FIRST"]
    T3 --> T4["Step 4: Size & Execute Buys"]
    T4 --> T5["Step 5: Apply 4% Friction Filter"]
```

---

## 3. Step-by-Step Decision Logic & Rules

### Step 1: Drawdown Circuit Breaker & Continuous Damping (Portfolio Level)
Calculate your current portfolio High-Water Mark (HWM) and peak-to-trough drawdown at 14:00:
$$\text{Drawdown} = \frac{\text{Current NAV} - \text{Peak NAV}}{\text{Peak NAV}}$$

* **IF Drawdown $< 10\%$ (Normal Regime)**:
  * Proceed to Step 2 with full risk budget. Standard single-stock cap = **$20\%$**.
* **IF $10\% \le \text{Drawdown} < 20\%$ (Continuous Smooth Damping, `cfsb_smooth_drawdown = True`)**:
  * **Action**: Replaces step-cliff liquidation with **continuous linear exposure damping**:
    $$\text{Scale}_{\text{dd}} = \max\left(0.0, 1.0 - \frac{\text{Drawdown} - 10\%}{20\% - 10\%}\right)$$
  * **Behavior**: At $10\%$ drawdown, exposure remains $100\%$; at $15\%$ drawdown, smoothly damped to $50\%$; at $18\%$ drawdown, damped to $20\%$, sweeping released equity into Cash Proxy (`BIL`).
  * **Fast Recovery**: If 10-day portfolio return turns positive or 10-day breadth thrust fires, drawdown penalty is immediately bypassed.
  * **Automatic HWM Healing**: If drawdown persists for 15 consecutive bars without fresh lows, the reference peak auto-heals to current NAV to prevent perpetual cash drag.
* **IF Drawdown $\ge 20\%$ (Tier 3: Hard Stop / Emergency Halt)**:
  * **Action**: **Liquidate $100\%$ of all risk assets into Cash Proxy immediately**.
  * **Lockout**: **Do not buy for 21 consecutive trading days**. On Day 22, reset HWM to current NAV to allow fresh cycle re-entry.

---

### Step 2: Market Breadth, 10-Day Thrust & Volatility Targeting (Capacity & Dispersion)
1. **Continuous Realized Volatility Targeting (`cfsb_enable_vol_targeting = True`, `cfsb_target_vol = 0.12`)**:
   * Grounded in Barroso & Santa-Clara (2015) volatility targeting, continuously tracking the rolling 21-day annualized market return volatility $\sigma_{21d}$.
   * When market turbulence elevates ($\sigma_{21d} > 12\%$), all target equity weights are scaled by $\min(1.0, 12\% / \sigma_{21d})$, preserving the rest in Cash Proxy, eliminating catastrophic tail drawdown.
2. **Market Breadth & Thrust Multi-Asset Dispersion**:
   * Calculate universe breadth: percentage of tracked stocks trading above their 50-day Simple Moving Average ($\text{Close} > \text{SMA}_{50}$, `cfsb_breadth_lookback = 50`) and short-term 10-day breadth thrust (percentage of stocks with positive 10-day return, $\text{ROC}_{10} > 0$, `cfsb_thrust_lookback = 10`):
   * **IF (Market Breadth $\ge 30\%$ OR 10-Day Breadth Thrust $\ge 60\%$) AND Drawdown $< 10\%$**:
     * **Dynamic Cash Deployment Triggered**: Total equity exposure allowed up to **$80\%$** (`cfsb_target_bull_exposure`).
     * **Max Single-Stock Cap Capped at $\le 20\%$** (`cfsb_bull_max_single_position = 0.20`): Strict limit prevents single-asset concentration blowups.
     * **Multi-Asset Thrust Dispersion**: When breadth thrust triggers, idle cash is evenly dispersed across **at least 5 distinct momentum leaders** (5%–8% each), strictly banning greedy all-in loading on a single stock.
   * **ELSE (Market Breadth $< 30\%$ AND Thrust $< 60\%$, OR Drawdown Active)**:
     * **Max Single-Stock Cap**: Hard limit of **$20\%$** per stock.
     * **Target Total Equity Exposure**: Standard unscaled exposure (typically $40\%–60\%$, balance in Cash Proxy `BIL`).

---

### Step 3: Asset-Level Sell Rules (Execute Sells FIRST)
Always execute sell orders before buy orders to guarantee purchasing power and avoid margin strain.

Liquidate an asset down to **$0.0\%$** if **ANY** of the following conditions trigger:

1. **Chan Structural Sell Points ($S_1 / S_2 / S_3$)**:
   * **$S_1$ (Top Divergence / 顶背驰)**: Stock makes a new high, but MACD histogram area or amplitude is clearly smaller than the previous upward stroke.
   * **$S_2$ (Second Sell Point / 二卖)**: Upward rebound fails to break the prior high and prints a lower-high pivot.
   * **$S_3$ (Third Sell Point / 三卖)**: Downward stroke breaks through the lower boundary of a consolidation pivot, and the subsequent pullback fails to re-enter the pivot zone.
2. **Four-State Machine Structural Invalidation Stops**:
   * **$B_1$ Entry Stop**: Close breaks below the lowest price of the $B_1$ bar low.
   * **$B_2$ Entry Stop**: Close breaks below the lowest price of the preceding consolidation pivot low ($DD$).
   * **$B_3$ Breakout Invalidation**: Close breaks below $ZG \times (1 - 0.01)$ (exceeds the 1% breakout tolerance band).
   * **Trailing Ratchet Stop**: As price trades above $ZG$, ratchet the exit stop up to $ZG$. Any close below $ZG$ exits the position.
3. **Lesson 16 Zero-Consolidation Stagnation Timeout**:
   * Position has lingered inside consolidation (`in_zs`) for $\ge 8$ consecutive bars (`chan_fse_cons_timeout_bars = 8`) without positive stroke momentum (`stroke_dir <= 0`) $\rightarrow$ **Sell immediately** to eliminate capital drag.
4. **Hard Stop-Loss**:
   * Asset drops **$\ge 8\%$** below entry price $\rightarrow$ **Market exit immediately** (`chan_fse_stop_loss_pct = 0.08`).
5. **Time Stop**:
   * Position held for **$\ge 90$ trading days** without new structural continuation $\rightarrow$ Close position and release capital (`chan_fse_max_holding_days = 90`).

---

### Step 4: Asset-Level Buy Rules (Position Sizing & Entry Filters)
Buy candidates qualify across the 3 sub-strategies:
1. **Four-State Execution Sleeve (20%)**:
   * **$B_1$ Bottom Fishing**: Requires price above MACD zero-axis reclaim or positive stroke divergence. Held with a **5-bar gestation buffer** (`min_hold_bars = 5`) to allow the pivot to develop.
   * **$B_2$ Pullback Buy**: Higher-low pivot formation after upward stroke.
   * **$B_3$ Breakout Buy**: Clear breakout above $ZG$. Gated by the **MA Entanglement Filter** (requires fast MA > slow MA and slope > 0 to avoid false range breakouts).
2. **Three-Type Sleeve (40%)**:
   * Allocates to confirmed $B_1, B_2, B_3$ setups across the universe with 20% single-stock ceiling.
3. **VAA Macro Compound Sleeve (40%)**:
   * Allocates to top 1-2 offensive assets with highest 13612W momentum score, or sweeps 70% to Cash Proxy when canary assets drop negative.
4. **Single-Stock Cap**: Never allocate more than **$20\%$** of total portfolio NAV to any single ticker (`cfsb_bull_max_single_position = 0.20`), eliminating excessive single-stock concentration during breadth thrusts.

---

### Step 5: Turnover & Minimum Trade Filter ($\Delta W \ge 4\%$)
Before entering an order into your broker terminal, calculate the weight change:
$$\Delta W = |W_{\text{target}} - W_{\text{current}}|$$

* **Normal Trading Days**:
  * **IF $\Delta W < 4\%$**: **DO NOT TRADE**. Hold prior quantity. (Prevents commission drag and slippage on micro-adjustments).
  * **IF $\Delta W \ge 4\%$**: Place order.
* **Circuit Breaker Days (Emergency Override)**:
  * If a circuit breaker triggered (Tier 1/2/3) or an asset hit an $8\%$ stop-loss:
    * **Emergency Sells**: Bypass the $4\%$ rule down to **$0.1\%$** ($|\Delta W| \ge 0.001$) and execute sales immediately.
    * **Emergency Buys**: Still require the full **$4\%$** threshold ($|\Delta W| \ge 0.04$) to prevent buying into a declining market.

---

## 4. Human Trader Quick-Reference Card

Keep this table handy during the market session:

| Check | Item | Condition | Human Trader Action |
| :--- | :--- | :--- | :--- |
| **Risk** | **HWM Drawdown** | $\ge 20\%$ | **STOP ALL TRADING (Tier 3)**: Liquidate $100\%$ to cash proxy. Freeze 21 days (HWM resets Day 22). |
| **Risk** | **HWM Drawdown** | $10\% - 19.9\%$ | **SMOOTH DAMPING**: Scale exposure continuously via $1.0 - (\text{DD}-10\%)/10\%$, sweeping released equity to cash. |
| **Risk** | **Market Volatility** | 21d realized vol $> 12\%$ | **VOL TARGETING**: Scale equity down continuously by $12\% / \sigma_{21d}$ to limit tail variance. |
| **Regime** | **Market Breadth** | $\ge 30\%$ above SMA50 or 10d Thrust $\ge 60\%$ | **BULL SCALING**: Total equity up to $80\%$; strict single-stock cap $\le 20\%$; thrust dispersed across $\ge 5$ leaders. |
| **Regime** | **Market Breadth** | $< 30\%$ above SMA50 and Thrust $< 60\%$ | **CONSERVATIVE**: Keep single-stock cap at $20\%$; standard cash buffer. |
| **Exit** | **Stop-Loss** | Loss $\ge 8\%$ from entry | **EXIT IMMEDIATELY**: Sell $100\%$ of position at market/limit. |
| **Exit** | **Time Stop** | Held $\ge 90$ days no progress| **EXIT**: Close position to release capital. |
| **Exit** | **Stagnation Timeout** | Held $\ge 8$ bars in consolidation | **EXIT**: Sell position to avoid Lesson 16 drag. |
| **Exit** | **Chan Sells / Invalidation**| $S_1, S_2, S_3$ or Breakout Stop | **EXIT**: Sell position to $0.0\%$. |
| **Entry** | **Four-State Machine**| Valid $B_1/B_2/B_3$ + MA Filter | **BUY**: Allocate with 5-bar gestation buffer ($\le 20\%$ cap). |
| **Execution**| **Friction Filter** | $|\Delta W| < 4\%$ | **SKIP**: Do not place order if change is under $4\%$ portfolio NAV. |
| **Execution**| **Sequence** | Multi-asset rebalance | **SELLS FIRST** (free up cash) $\rightarrow$ **BUYS SECOND**. |

---

## 5. Execution Nuances & Slippage Management

1. **Execution Window**: 
   * Evaluate data at **14:00**.
   * Transmit orders between **14:20 – 14:45**. 
   * Avoid trading in the first 30 minutes of the open (09:30–10:00) where bid-ask spreads are widest.
2. **Order Style**:
   * For highly liquid instruments (large-cap ETFs/stocks): Use **limit orders pegged to current bid/ask** or TWAP slices over 15 minutes.
   * For emergency stop-losses or Tier 3 circuit breakers: Use **market or immediate-or-cancel limit orders** to guarantee execution.
3. **Cash Proxy Management**:
   * Any capital not allocated to stocks should be parked in risk-free yield assets (e.g., [`BIL`](file:///home/stone/Work/github/quant/pipeline/research_strategy/strategies_config.json) / `SGOV` in US markets, or `511880` / `511990` / GC001 in China A-shares) rather than uninvested non-earning cash.

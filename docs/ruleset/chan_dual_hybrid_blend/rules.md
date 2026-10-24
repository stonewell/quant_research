# Chan Dual Hybrid Alpha Blend Strategy: Live Trading Operating Manual & Rulebook

> **Strategy Identifier**: `chan_dual_hybrid_blend` ([`ChanDualHybridBlendStrategy`](file:///home/stone/Work/github/quant/pipeline/research_strategy/rs/chan_advanced_strategies.py#L2663-L3333))  
> **Live Trading Readiness Score**: **87.5 / 100 (Grade B+, Institutional Live Ready)**  
> **Instrument Context**: China A-Shares / Liquid Equity Universe (21 Audited Core Assets) + Cash Management Equivalents (`511880.SH`, `511990.SH`, `GC001` / `R-001`, or `BIL` in US/Synthetic backtests)  
> **Trading Frequency**: Daily evaluation at **14:00**, execution window **14:20 – 14:45**, overnight cash sweep **14:55 – 15:00** (avoids open auction whipsaws and close auction distortions).  
> **Underlying Factors**: `regime_trend_strength`, `absolute_momentum_trend`, `relative_momentum`, `volatility_targeting`, `mean_reversion`  
> **Walkforward Audit Provenance**:
> - **4-Year Macro Cycle (15 Rolling Folds Full History, 2022-2025, `blend_4/1`)**: Mean Sharpe **1.223** (Adj: **1.052**), CAGR **24.64%**, MaxDD **5.08%**, Calmar **10.43**, **100% Passed Leave-One-Out (LOO) Fragility Test** (retained +8,699 RMB net profit even excluding all top 3 winners), 2.8x annualized turnover, strictly compliant position sizing (Max Pos 20.0%);
> - **Out-of-Sample Forward Stress Test (3 Rolling Folds, 2025.11-2026.09, `blend_4/2`)**: Ranked **#1 across all 4 blend strategies (Rank 1 🏆)**, CAGR **+3.23%** (vs CSI 300 benchmark **-1.50%**, Alpha **+4.73%**), MaxDD **4.61%**; **Fold 3 Summer 2026 Crash benchmark plunged -20.38%, whereas strategy dropped only -2.00%, generating +18.38% extreme crash alpha**.

---

## 1. Strategy Identity & Architecture Overview

The **Chan Dual Hybrid Alpha Blend Strategy** is an institutional multi-strategy quantitative allocation engine. It blends structural central consolidation buy points (Chan Three-Type), Keller Vigilant Asset Allocation (Chan VAA Compound), and an expanded 4-factor orthogonal alpha sleeve (Alpha#53, Alpha#3, Alpha#41, Alpha#101), reinforced by **Dynamic Bull/Bear Regime Weighting**, a **Garman-Klass Volatility Engine**, **ATR Trailing Dynamic Stops**, and **Adaptive Inertia Filtering** (asymmetric sell-bands exist in the backtest only — see Step 5).

```mermaid
flowchart TD
    Universe["Audited 21-Stock Universe + Cash Equivalents"] --> SubAlphas["Multi-Alpha Ensemble Engine"]
    
    subgraph Ensemble ["Dynamic Regime-Adaptive Multi-Strategy Allocation"]
        SubAlphas --> S1["Chan Three-Type Allocation\n(Fractal 1B/2B/3B Pivot Buy Points)\n[Bull: 50% | Chop: 45% | Bear: 45%]"]
        SubAlphas --> S2["Chan VAA Compound Defense\n(13612W Momentum Crash Buffer)\n[Bull: 20% | Chop: 25% | Bear: 30%]"]
        SubAlphas --> S3["Expanded 4-Alpha Sleeve\n(Alpha#53 Wick + Alpha#3 Divergence\n+ Alpha#41 VWAP + Alpha#101 Efficiency)\n[Bull: 30% | Chop: 30% | Bear: 25%]"]
    end
    
    S1 & S2 & S3 --> RawBlend["Raw Target Blend Weights"]
    
    RawBlend --> DDGate{"Portfolio Drawdown Gate\n(Peak NAV / HWM Check)"}
    DDGate -- "DD >= 20% (Tier 3 Hard Stop)" --> HardStop["Full Liquidation -> 100% Cash\nFreeze 21 Trading Days"]
    DDGate -- "10% <= DD < 20% (Tier 1/2 Damping)" --> LinearDamp["Smooth Linear Damping (1.0x -> 0.0x)\n15-Bar Auto-Heal & Momentum Recovery"]
    DDGate -- "DD < 10% (Normal)" --> BreadthEngine{"Breadth & Thrust Check\n(Breadth >= 30% or Thrust >= 60%)"}
    
    BreadthEngine -- "Thrust Active & Idle Cash" --> AntiFragileThrust["【Anti-Fragile Pre-Emptive Thrust】\n1. Quality Gate: 10d ROC >= 1% & P >= SMA50\n2. Cooldown Lock: Exclude 15-day ATR stopped stocks\n3. Sector Throttle: Max 1 per sector (2 for leaders)\n4. Inverse-Vol Parity: Weight ∝ Score_i / GK_Vol_i"]
    BreadthEngine -- "Normal / Neutral" --> SizingCap["Hard Position Cap: Max 20% NAV per Asset"]
    AntiFragileThrust --> SizingCap
    LinearDamp --> SizingCap
    
    SizingCap --> VolTarget{"Garman-Klass Volatility Targeting\n(Bull 24% | Chop 18% | Bear 12%)"}
    VolTarget --> FrictionFilter{"Trade Inertia Gates (LIVE)\nHeld position: |Delta W| >= 5%\nNew position: target >= 3%\nExit: 2-bar confirmation\nEmergency: full bypass\n(Backtest instead: adaptive 2.5%-8%, sells at 0.60x)"}
    
    FrictionFilter -- "Rebalance Triggered or Emergency" --> ExecutionEngine["Order Execution Sequence:\n1. 14:25 Sells First -> 2. 14:35 Buys Second\n100-Share Lot Downward Rounding"]
    FrictionFilter -- "Below gate" --> HoldPrior["Hold Prior Positions (Zero Friction)"]
    ExecutionEngine --> RepoSweep["14:55 Idle Cash Sweep to GC001 / R-001"]
```

### Core Mathematical Components

1. **Chan Three-Type Allocation**:
   - Decomposes price action into fractal strokes (Bi), segments, and central consolidation pivots ($ZG/ZD$).
   - **1st Buy ($B_1$)**: Trend-exhaustion bottom divergence (MACD area depletion).
   - **2nd Buy ($B_2$)**: Pullback re-test confirming higher low above $B_1$ bottom.
   - **3rd Buy ($B_3$)**: Breakout above pivot high $ZG$ with subsequent pullback bottom remaining strictly above $ZG$.
2. **Chan VAA Compound Defense**:
   - Computes 13612W Keller momentum scores on offensive and defensive baskets:
     $$\text{Score} = 12 \times r_{1M} + 4 \times r_{3M} + 2 \times r_{6M} + 1 \times r_{12M}$$
   - Switches systematically to cash equivalents if universe breadth flips negative, acting as the bedrock crash shield during bear regimes.
3. **Expanded 4-Alpha Sleeve**:
   - **WorldQuant Alpha#53 ($7.5\%$)**: Intraday Close Location Value (CLV) wick imbalance momentum:
     $$\text{Alpha\#53} = -1 \times \Delta_9\left(\frac{(\text{Close} - \text{Low}) - (\text{High} - \text{Close})}{\text{Close} - \text{Low} + 10^{-4}}\right)$$
   - **WorldQuant Alpha#3 ($7.5\%$)**: Volume-price rank delta accumulation:
     $$\text{Alpha\#3} = -1 \times \rho_{10}(\text{rank}(\text{Open}), \text{rank}(\text{Volume}))$$
   - **WorldQuant Alpha#41 ($7.5\%$)**: Geometric mean price support vs VWAP:
     $$\text{Alpha\#41} = \left(\text{High} \times \text{Low}\right)^{0.5} - \text{VWAP}$$
   - **WorldQuant Alpha#101 ($7.5\%$)**: Intraday directional efficiency factor:
     $$\text{Alpha\#101} = \frac{\text{Close} - \text{Open}}{(\text{High} - \text{Low}) + 10^{-4}}$$
4. **Dynamic Bull/Bear Regime Weighting**:
   - Bull Market ($\text{Breadth} \ge 0.60$ or $\text{Thrust} \ge 0.60$): Three-Type 50%, VAA 20%, Alpha Sleeve 30%;
   - Bear Market ($\text{Breadth} < 0.30$ and $\text{Thrust} < 0.40$): Three-Type 45%, VAA 30%, Alpha Sleeve 25%;
   - Neutral / Chop: Three-Type 45%, VAA 25%, Alpha Sleeve 30%.
5. **Garman-Klass Volatility Engine**:
   $$\sigma_{\text{GK}}^2 = 0.5 \times \left(\ln\frac{\text{High}}{\text{Low}}\right)^2 - (2\ln 2 - 1) \times \left(\ln\frac{\text{Close}}{\text{Open}}\right)^2$$
   Provides high-fidelity, extreme-low-lag intraday volatility estimates, detecting institutional distribution 3–5 days faster than close-to-close metrics.

---

## 2. Daily Live Trading Routine (5-Step Operational Checklist)

Human traders must execute the following sequential timeline strictly without deviation:

```mermaid
flowchart LR
    T1["14:00 - 14:15\nStep 1: NAV Drawdown & Auto-Heal Check"] --> T2["14:15 - 14:25\nStep 2: Breadth, Sector Throttle & Thrust"]
    T2 --> T3["14:25 - 14:35\nStep 3: 【EXECUTE SELLS & STOPS FIRST】"]
    T3 --> T4["14:35 - 14:45\nStep 4: 【EXECUTE BUYS WITH FREED CASH】"]
    T4 --> T5["14:45 - 14:50\nStep 5: Inertia Gates & 100-Share Lots"]
    T5 --> T6["14:55 - 15:00\nStep 6: GC001 Overnight Repo Sweep"]
```

* **14:00 – 14:15**: Pull live portfolio valuation, calculate High-Water Mark (HWM) drawdown, check circuit breaker tiers, and review cooldown counters.
* **14:15 – 14:25**: Measure percentage of stocks above 50d SMA (Breadth) and 10d positive return count (Thrust). Audit 15-day ATR cooldown locks and sector allocations to generate target weights.
* **14:25 – 14:35**: **EXECUTE SELLS FIRST**. For assets triggering structural sells, 3.0 ATR dynamic trailing stops, or drawdown reductions, submit limit orders at bid to release purchasing power. Tag stopped assets with a 15-day cooldown lock.
* **14:35 – 14:45**: **EXECUTE BUYS SECOND**. Utilizing verified freed cash and target weights, apply the live inertia gates (held position $\ge 5\%$, brand-new position $\ge 3\%$ — see Step 5) and 100-share downward lot rounding to place buy orders.
* **14:45 – 14:50**: Confirm execution fills, inspect pending orders, and log any partial executions.
* **14:55 – 15:00**: **OVERNIGHT CASH SWEEP**. Direct 100% of remaining idle cash into `GC001` or `R-001` reverse repo for risk-free overnight yield enhancement.

---

## 3. Step-by-Step Decision Logic & Rules

### Step 1: Drawdown Circuit Breakers & Auto-Heal Mechanism (Mandatory First Check)

At 14:00 daily, calculate portfolio drawdown against High-Water Mark:
$$\text{Drawdown} = \frac{\text{Current NAV} - \text{HWM}}{\text{HWM}}$$

1. **Normal Band ($\text{Drawdown} > -10\%$)**:
   - Risk multiplier = $1.0$. Standard single-stock cap = **20%**.
2. **Tier 1/2 Smooth Linear Damping ($-20\% < \text{Drawdown} \le -10\%$) — code + human (代码 + 人工)**:
   - **The damping factor itself is code-executed (代码执行)**. In the $15\%$–$20\%$ sub-band the health report *additionally* prints a rotation into a defensive sub-strategy (`scripts/check_live_portfolio_health.py:618-622`); that rotation is **human-executed (人工执行)** and the ticket generator does not perform it.
   - **Fast Recovery Channel**: If 10-day trailing return $R_{10d} > 0$ or breadth thrust $\ge 60\%$:
     - **Bypass damping**; maintain full allocation ($1.0$) to capture post-panic V-shaped recoveries.
   - **ELSE (Smooth Linear Damping)**:
     $$\text{scale}_{\text{DD}} = \max\left(0.0,\ 1.0 - \frac{|\text{Drawdown}| - 0.10}{0.20 - 0.10}\right)$$
     Scale all equity positions down progressively from **100% to 0%** (0% reduction at DD = 10%, exactly 50% at DD = 15%, 100% at DD = 20%), parking released capital into cash proxies. The discrete "× 0.5" branch exists in code (`pipeline/research/blend/blend_base.py:1075-1081`) but is **not** the deployed configuration: all six blends set `smooth_drawdown: true`; live applies the same continuous formula (`scripts/check_live_portfolio_health.py:380,384`).
   - **15-Day Auto-Heal**: If trapped in damping for **15 consecutive trading days**, reset HWM to current NAV to restore active alpha search.
3. **Tier 3 Hard Stop ($\text{Drawdown} \le -20\%$) — code-executed (代码执行, hard halt)**:
   - **Full liquidation**: Sell 100% of risky assets into cash equivalents immediately.
   - **Trading freeze**: Lock out new entries for **21 consecutive trading days**. Reset HWM on day 22.

> **Note — the Tier-2 rotation instruction is not executed by the ticket generator.** The "rotate into a defensive sub-strategy" text the health report prints at Tier 2 is an advisory action string for the human (`scripts/check_live_portfolio_health.py:618-622`); `scripts/run_live_strategy.py` contains no `TIER_1` / `TIER_2` branch at all, so on a Tier-2 day the ticket only applies the portfolio-level scaling (`current_targets *= live_scale` at `scripts/run_live_strategy.py:1766`, where `live_scale` is read from `account_state` / the health report's `linear_equity_scale`, `scripts/check_live_portfolio_health.py:380`). The rotation must be carried out by the human per the health report; therefore **on a Tier-2 day the position structure printed on the ticket may still be the Tier-1 damped shape**.

---

### Step 2: Regime Evaluation & Anti-Fragile Thrust Allocation

1. **Market Breadth Filter**:
   $$\text{Breadth} = \frac{\sum_{i=1}^{N} \mathbb{I}(P_{\text{Close}, i} > \text{SMA}_{50, i})}{N}$$
   - Bullish regime: $\text{Breadth} \ge 30\%$. Allows equity target expansion up to **80%**.
2. **Anti-Fragile Pre-Emptive Momentum Thrust**:
   - Triggered when positive 10d return count $\ge 5$ stocks (or $\ge 60\%$):
     - **Quality Gate**: $R_{10d, i} \ge 1.0\%$ AND $P_{\text{Close}, i} \ge \text{SMA}_{50, i}$;
     - **Cooldown Lock**: Asset must NOT be in a 15-day ATR stop cooldown;
     - **Accumulation Score**: $\text{Score}_i = R_{10d, i} \times (1.0 + 5.0 \times W_{\text{Alpha\#3}, i})$;
     - **Sector Throttle**: Max 1 stock per industry (up to 2 for leading sectors with top 2 stocks $> +5\%$);
     - **Garman-Klass Inverse-Vol Parity**: Weight proportional to $\text{Score}_i / \sigma_{\text{GK}, i}$, subject to 20% max position cap.

---

### Step 3: Exit & Stop-Loss Rules (SELLS FIRST)

Between **14:25 – 14:35**, evaluate positions. Liquidate immediately to **0.0%** if any condition triggers:
1. **Structural Sells**: Chan structural top divergence ($S_1$) or breakdown below central pivot ($S_3$).
2. **ATR Trailing Dynamic Stop & 15-Day Lock**:
   $$\text{Stop\_Price}_t = \max(\text{Stop\_Price}_{t-1}, \ P_{\text{High}, t} - 3.0 \times \text{ATR}_{14d, t})$$
   $$P_{\text{Current}} \le \text{Stop\_Price}_t \implies \text{Liquidate immediately at market/bid}$$
   - **Locks asset into a 15-trading-day cooldown**, barring re-entry.
3. **Time Stop**: 90 consecutive trading days without achieving a new higher high.
4. **Drawdown Damping**: Pro-rata reduction mandated by Step 1.

---

### Step 4: Entry & Volatility Targeting Rules (BUYS SECOND)

1. **Portfolio Sizing & Single-Asset Cap**:
   - Single-asset position strictly capped at **20% NAV** (`cdhb_max_single_position = 0.20`).
2. **Regime Volatility Targeting**:
   - Bull regime target: **24%**; Bear regime target: **12%**; Neutral target: **18%**.
   - If portfolio realized Garman-Klass volatility $\sigma_{\text{Port}} > \text{Target\_Vol}$:
     $$\text{Scalar} = \frac{\text{Target\_Vol}}{\sigma_{\text{Port}}}$$
     Scale all tentative buy weights downward by this scalar.

---

### Step 5: Trade Inertia Gates & Round-Lot Rounding

For every asset, compute the signed weight change at 14:00:
$$\Delta W = W_{\text{target}} - W_{\text{current}}$$

This strategy carries **one inertia parameter** (`cdhb_min_weight_change = 0.05`) and **a separate, looser entry gate** (`cdhb_entry_threshold = 0.03`). The "asymmetric 0.5x sell band" is a **backtest-only** feature of `blend_base.py` (and it is 0.60x there, not 0.5x) — **live sells do not get it**. Apply the **first** rule below that matches the asset's situation:

| # | Situation | Live rule — what you actually do | Backtest rule — what the model did (different by design) |
| :--- | :--- | :--- | :--- |
| **1** | You **already hold** the asset — target **increased** (add) | Require $\lvert\Delta W\rvert \ge \mathbf{5\%}$. Below that, **do not trade**; the ticket prints `HOLD` with reason "调仓未超惰性阈值 (< 5%)". | Buy threshold = the per-asset adaptive value **2.5% – 8%** ($\mathrm{clip}(\tfrac{\sigma^{21d}_{\text{asset}}}{\sigma^{21d}_{\text{universe}}} \times 5\%,\ 2.5\%,\ 8\%)$), loosened further for fresh entries to $\min(\text{adaptive},\ 3\%)$. |
| **2** | You **already hold** the asset — target **decreased** (trim / partial exit) | Require $\lvert\Delta W\rvert \ge \mathbf{5\%}$ — **the same 5% as an add**. There is no 2% or 2.5% "fast cash release" band in the live path. Below 5% the trim is refused (`HOLD`, "调仓未超惰性阈值 (< 5%)"). This is the one rule that runs **against** the backtest: the backtest trims **earlier**, the live runner trims **later**. | `asymmetric_sells` defaults **on** for this strategy, so the sell threshold is $0.60 \times$ the adaptive value, i.e. **1.5% – 4.8%** — deliberately more permissive than buys. |
| **3** | You hold **nothing** in the asset (0% → new position) | Require **target weight $\ge \mathbf{3\%}$** (`cdhb_entry_threshold = 0.03`). Below 3%, **do not open**; the ticket prints `HOLD_FILTERED` with reason "目标仓位未达最低建仓/调仓门槛 (… 防微量试错损耗)". | The backtester's own 5% gate **does not apply to new positions** (assets not already held are exempt); the template-level filter gates entries at $\min(\text{adaptive},\ 3\%)$, i.e. **2.5% – 3%**. |
| **4** | The model wants **0.0%** on an asset you hold (full exit) | Exempt from gates 1–3, but the exit must **persist 2 consecutive evaluation days** (`guard_latency_bars = 2`). Day 1 prints `PENDING_EXIT` / `HOLD_FILTERED` with reason "平仓信号进入确认期 (第 1/2 日待确认)". | Exits the same day the rebalance row carries 0.0% — no persistence requirement. |
| **5** | ATR trailing stop, structural sell, or an account circuit breaker | **Sell 100% immediately** (Step 3). All inertia gates are bypassed outright — no residual "0.1% floor". Buys are never granted this bypass. | The per-symbol `asset_emergency_df` mask bypasses inertia for that asset and bar. |

* **VERIFIED — the damping cut is itself gated, per asset, and is routinely refused.** The cut is applied **per asset**: every held name's weight is multiplied by the same `scale` factor, with the remainder swept into the cash proxy (`scripts/run_live_strategy.py:1766`; `pipeline/research/blend/blend_base.py:1069-1071`). Each name is therefore an **independent trim subject to its own threshold** — at DD = 15% the factor is 0.5, so a 6% position is cut to 3%, a 3pp change against the 5% gate, and is **refused that day**; the ticket shows `HOLD` with reason "调仓未超惰性阈值 (< 5%)". A 50% cut clears the gate only once the position is **≥ 10% of NAV**. This happens in **both paths**: live grants no risk-reduction exemption (the `is_stop` fast path only ever liquidates to 0.0 and only for stop-loss / `is_emergency_halt`, `common/position_lifecycle.py:363-420`; `is_emergency_halt` is Tier-3-only, `scripts/run_live_strategy.py:1927`), and the backtest's inertia filter swallows the same trim unless the asset-emergency bypass is on. Only the full-exit/stop paths in rules 4–5 liquidate regardless of size.
* **The two paths also diverge in DD ∈ (17%, 20%)**: the backtest opens its bypass at `scale_dd < 0.30`, i.e. DD > 17% (`pipeline/research/blend/blend_base.py:1072` → `is_global_emergency`, `common/allocation_templates.py:233-251`), while live only force-liquidates at DD ≥ 20% (Tier 3). Between 17% and 20% the backtest applies the cut and live keeps blocking it.
* **No override number is prescribed here.** On a Tier-1/2 day, reconcile the breaker tier against the reduction actually printed on the ticket before acting; whether to top a refused cut up manually is an account-owner policy decision, not a rule this manual can invent.

* **No relative trim hurdle for this strategy**: no `inertia_relative_ratio` is configured, so the live trim gate never scales with the size of the position.
* **No row-level gate in the backtest**: a rebalance row is emitted whenever *any* symbol's target moved, and it carries every symbol — per-symbol inertness within a live ticket has no backtest counterpart at row level.

> ⚠️ **The live path and the backtest path differ by design, as currently implemented.** The direction of the asymmetry is *reversed*: the backtest frees cash by trimming at 1.5% – 4.8%, while the live runner requires the full 5% for every trim, and it refuses brand-new positions below 3% that the backtest would take. Consequently **live holdings may legitimately contain fewer names, and hold larger positions, than the backtest for the same date.** Do not reconcile the gap by inventing a 2% live sell band, waiving the entry gate, or forcing the backtest's name count.

**A-Share 100-Share Round-Lot Rule** (applies after every gate above passes):
   $$\text{Shares}_i = \left\lfloor \frac{\text{NAV} \times W_{\text{Target}, i}}{P_{\text{Close}, i} \times 100} \right\rfloor \times 100$$

---

### Step 6: 14:55 Overnight Repo Sweep

At 14:55 daily:
- Sweep 100% of remaining unallocated cash into **`GC001` (204001.SH)** or **`R-001` (131810.SZ)**.
- Cash returns to usable status at 09:00 the following morning.

---

## 4. Audited 21-Stock Core Portfolio Universe

| Sector | Ticker | Name | Industry Tag | Strategic Role & Thesis | NAV Cap |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Optical Modules** | `300394.SZ` | Tianfu Telecom | `optics` | Global optical transceiver packaging leader; top alpha contributor across all cycles. | 20% |
| **ICT Infrastructure**| `000938.SZ` | Unisplendour | `tech` | AI computing networks and servers; primary breakout momentum pillar. | 20% |
| **Semiconductor Packaging**| `600584.SH` | JCET Group | `semiconductor`| Advanced packaging leader; core asset for Alpha#53 wick momentum. | 20% |
| **Telecom Infrastructure**| `601728.SH` | China Telecom | `telecom` | Digital compute foundation; resilient high-dividend anchor. | 20% |
| **Telecom Infrastructure**| `600941.SH` | China Mobile | `telecom` | SOE balance sheet battleship; ultra-low volatility defensive anchor. | 20% |
| **Metals Supercycle** | `601899.SH` | Zijin Mining | `metals` | Global gold and copper miner; inflation and macro resource hedge. | 20% |
| **Industrial Metals** | `600362.SH` | Jiangxi Copper | `metals` | Industrial copper smelting leader; top 3 winner in 2026 forward test (+1,484 RMB). | 20% |
| **Tanker Shipping** | `601872.SH` | CMES Shipping | `shipping` | Tanker freight upcycle; top alpha contributor in 2026 forward test (+2,193 RMB). | 20% |
| **Container Shipping**| `601919.SH` | COSCO SHIPPING | `shipping` | Container logistics; substantial dividend yield protection. | 20% |
| **High-End Manufacturing**| `600660.SH` | Fuyao Glass | `auto` | Global automotive glass monopoly; overseas resilience compounder. | 20% |
| **Machinery Export** | `000157.SZ` | Zoomlion | `machinery` | Construction equipment export; dividend support with cyclical upside. | 20% |
| **Energy & Refining** | `600028.SH` | Sinopec | `oil` | Downstream refining integration; throttled with PetroChina (max 1-2). | 20% |
| **Upstream Energy** | `601857.SH` | PetroChina | `oil` | Upstream exploration powerhouse; resource supercycle anchor. | 20% |
| **Defensive Insurance**| `601601.SH` | CPIC | `insurance` | Quality life and P&C carrier; equity portfolio beta recovery beneficiary. | 20% |
| **Low-Vol Banking** | `601398.SH` | ICBC | `bank` | World's largest bank; ultimate low-vol safe haven during market panics. | 20% |
| **Low-Vol Banking** | `601288.SH` | ABC | `bank` | Rural and commercial dividend play; top 3 winner across 4-year cycle. | 20% |
| **Retail Banking** | `600036.SH` | CMB | `bank` | Premier wealth management banking franchise. | 20% |
| **High-Dividend Coal**| `601088.SH` | China Shenhua | `coal` | Long-term thermal coal contracts; managed via ATR stops and sector throttle. | 20% |
| **High-Dividend Coal**| `601225.SH` | Shaanxi Coal | `coal` | Low-cost coal producer; throttled with Shenhua (max 1 asset). | 20% |
| **Transportation** | `601111.SH` | Air China | `transport` | Domestic and international carrier; strictly gated by 50d SMA filter. | 20% |
| **Commercial Banking**| `600000.SH` | SPDB | `bank` | Commercial banking liquidity proxy; throttled by inertia filter. | 20% |

---

## 5. Human Trader Quick-Reference Card (Cheat Sheet)

| Dimension | Rule Check | Trigger Condition | Mandatory Trader Action |
| :--- | :--- | :--- | :--- |
| **Risk** | **Tier 3 Hard Stop** — **code-executed (代码执行, hard halt)** | Drawdown $\ge 20\%$ | **【Full Liquidation】**: Sell 100% equities into cash proxies; **21-day freeze**. Reset on day 22. |
| **Risk** | **Tier 1/2 Damping** — **code + human (代码 + 人工)** | $10\% \le \text{Drawdown} < 20\%$ | **【Linear Reduction】**: Scale positions by $\text{scale}_{\text{DD}} = 1 - (\lvert DD\rvert - 10\%)/10\%$ (0% cut at 10%, 50% at 15%, 100% at 20%). 15-day auto-heal. The backend applies the scale; the Tier-2 rotation the health report prints is not executed by the ticket generator. |
| **Risk** | **Recovery Channel**| 10d return $> 0$ or thrust $\ge 60\%$ | **【Bypass Damping】**: Immediately restore 100% normal allocation. |
| **Risk** | **ATR Trailing Stop**| Price drops below 3.0 ATR line | **【Cut & Lock】**: Sell 100% of asset; **lock into 15-day trading freeze**. |
| **Regime** | **Momentum Thrust** | Positive 10d count $\ge 5$ stocks | **【Deploy Idle Cash】**: Allocate to qualified momentum leaders, max 20% per stock. |
| **Quality**| **Trend Filter** | Price $< \text{SMA}_{50}$ or $R_{10d} < 1\%$ | **【Block Entry】**: Do not allocate capital; prevents falling knives. |
| **Throttle**| **Sector Limit** | Sector already held | **【Sector Cap】**: Max 1 stock per sector (up to 2 for runaway leader sectors). |
| **Execution**| **Inertia (add gate)**| Held asset, add $\lvert\Delta W\rvert < 5\%$ | **【Do Not Trade】**: Hold exact prior shares. |
| **Execution**| **Inertia (trim gate)**| Held asset, trim $\lvert\Delta W\rvert < 5\%$ | **【Do Not Trade】**: No 2% "fast cash" band exists live; hold exact prior shares. |
| **Execution**| **Inertia (entry gate)**| New position target weight $< 3\%$ | **【Do Not Trade】**: Do not open a brand-new sub-3% line. |
| **Execution**| **Exit Confirmation**| Model target $= 0.0\%$ on a held asset | **【Wait 2 Days】**: Sell only after the signal persists 2 consecutive evaluations. |
| **Execution**| **Emergency Bypass**| Stop loss or circuit breaker | **【Zero Delay】**: Bypass all inertia gates completely; execute immediately. |
| **Execution**| **Trade Order** | Portfolio rebalance | **【Sells First, Buys Second】**: 14:25 sells $\implies$ 14:35 buys. |
| **Cash** | **Overnight Repo** | 14:55 unallocated cash | **【100% Sweep】**: Lend out via `GC001` or `R-001`. |

---

## 6. Execution Nuances & Slippage Management

1. **A-Share T+1 Settlement**: Stocks bought today cannot be sold until tomorrow. Limit entries to 20% max NAV per stock to prevent liquidity traps.
2. **Limit Up / Limit Down Rules**:
   - **Limit Down on Sells (-10% / -20%)**: If blocked by locked limit down, record anomaly, retain position overnight, and place immediate sell at open call auction (09:15–09:25) next morning.
   - **Limit Up on Buys (+10% / +20%)**: Never chase locked limit-up boards. Cancel order and preserve cash.
3. **Execution Routing**:
   - Orders $< 500,000$ RMB: Use limit orders pegged to opposite-side best bid/ask.
   - Orders $\ge 500,000$ RMB: Execute via 15-minute TWAP algorithm between 14:25 – 14:45.
4. **Corporate Actions & Dividends**: Cash dividends automatically credited to NAV, swept overnight into `GC001`, and reinvested at subsequent rebalances.

---

## 7. Production CLI Execution

```bash
uv run python scripts/run_live_strategy.py \
  --strategy chan_dual_hybrid_blend \
  --universe-file docs/universe/china/core_satellite_22_stocks.txt \
  --output-dir docs/ruleset/chan_dual_hybrid_blend \
  --portfolio-value 100000 \
  --data-provider marketdb
```

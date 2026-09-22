---
name: live-trading-rules
description: >-
  Generates production-grade, human-executable live trading operating manuals
  and buy/sell rulebooks (in both English and Chinese) when provided a quantitative
  strategy name. Inspects the strategy's class implementation, parameters, factor tags,
  signals, circuit breakers, and risk constraints across the codebase, and outputs
  standardized markdown manuals to docs/ruleset/<strategy_name>/rules.md and rules_cn.md.
  Use whenever the user asks to generate live trading rules, create a live trading SOP,
  document buy/sell rules for a strategy, or convert a backtested strategy into live execution rules.
---

# Live Trading Ruleset & Operating Manual Generator

This skill converts quantitative research and backtested strategies into production-grade, human-executable live trading operating manuals (SOPs) and buy/sell rulebooks in both English (`rules.md`) and Chinese (`rules_cn.md`).

---

## Workflow Overview

When invoked with a strategy name (e.g. `chan_risk_managed_blend`, `chan_composite`, `protective_asset_allocation`, `dual_momentum`, etc.):

```text
┌─────────────────────────────────────────────────────────────────┐
│ Phase 1: Strategy Discovery & Parameter Extraction              │
│ - Look up entry in pipeline/research_strategy/strategies_config.json│
│ - Inspect parameter defaults, factor tags, cash proxy           │
│ - Locate strategy class in pipeline/research_strategy/rs/       │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 2: Signal & Alpha Deconstruction                          │
│ - Extract buy triggers, entry tranches, indicator lookbacks     │
│ - Map exit triggers, structural sell points, stop-loss %, time  │
│ - Identify macro regime filters (VAA, breadth, volatility gate) │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 3: Risk Controls & Portfolio Sizing                       │
│ - Formalize multi-tier drawdown circuit breakers (10%, 15%, 20%)│
│ - Enforce single-stock NAV caps (e.g. 20%, bull expansion 30%)  │
│ - Enforce gross leverage <= 100% and cash proxy parking         │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 4: Execution & Friction Filtering                         │
│ - Establish asset-level inertia filter (|Delta W| >= 4%)        │
│ - Sequence trades: SELLS FIRST (free cash) -> BUYS SECOND       │
│ - Detail trading hours (14:00 - 14:50) and order types (limit/TWAP)
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 5: Export Standardized Dual-Language Manuals              │
│ - docs/ruleset/<strategy_name>/rules.md (English)               │
│ - docs/ruleset/<strategy_name>/rules_cn.md (Chinese)            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Detailed Step-by-Step Instructions

### Phase 1: Strategy Discovery & Parameter Extraction
1. **Search Configuration**:
   * Inspect [`pipeline/research_strategy/strategies_config.json`](file:///home/stone/Work/github/quant/pipeline/research_strategy/strategies_config.json).
   * Check if serialized dumps exist in [`pipeline/research_strategy/results/strategy_dumps/<strategy>_strategy.json`](file:///home/stone/Work/github/quant/pipeline/research_strategy/results/strategy_dumps).
2. **Locate Implementation Code**:
   * Inspect Python strategy classes in `pipeline/research_strategy/rs/` or `common/allocation_templates.py`.
   * Note the class name, inheritance, docstring, and helper methods.
3. **Extract Parameters**:
   * Identify all parameters (e.g., `stop_loss_pct`, `max_holding_days`, `crb_dd_reduce_thresh`, `cash_proxy`, `min_weight_change`).

### Phase 2: Signal & Alpha Deconstruction
1. **Entry Rules (Buys)**:
   * How are buy points identified? (e.g. MACD divergence, pivot breakout, momentum rank, moving average crossover).
   * Does the strategy scale in via tranches? (e.g., 30% on $B_1$, 40% on $B_2$, 30% on $B_3$).
2. **Exit Rules (Sells)**:
   * Structural sells (e.g. $S_1, S_2, S_3$, top divergence, lower-high failure).
   * Emergency structural brakes (e.g. Lessons 92–99 dangerous pivot violations).
   * Hard stop-loss threshold (e.g. $-5\%$ from entry price).
   * Time stops (e.g. 40 trading days without fresh continuation).
3. **Regime Filters**:
   * Market breadth gate (e.g. $\text{Breadth} \ge 50\%$ stocks above 50-day SMA).
   * Dual momentum regime (e.g. VAA 13612W score on offensive basket).

### Phase 3: Risk Controls & Portfolio Sizing
1. **Portfolio Drawdown Circuit Breakers**:
   * **Tier 1 (e.g. 10% DD)**: Halve equity allocation ($50\%$ to cash proxy).
   * **Tier 2 (e.g. 15% DD)**: Switch to defensive tactical mode ($70\%$ cash/defensive buffer).
   * **Tier 3 (e.g. 20% DD)**: Hard full stop (100% cash) + 21-day trading freeze cooldown.
2. **Concentration Constraints**:
   * Standard single-stock NAV cap (typically $\le 20\%$).
   * Bull-expanded single-stock cap (typically $\le 30\%$).
   * Maximum gross leverage: $100\%$ long-only.

### Phase 4: Order Execution & Turnover Filtering
1. **Friction / Inertia Threshold**:
   * Suppress rebalance orders if $|\Delta W| < 4\%$ (`crb_min_weight_change`).
   * Emergency bypass: During drawdown circuit breakers or $-5\%$ stop-loss, execute immediately.
2. **Execution Sequencing**:
   * **SELLS FIRST**: Close/downsize positions to release cash.
   * **BUYS SECOND**: Allocate new tranches using freed purchasing power.
3. **Operational Timetable**:
   * **14:00 – 14:15**: Data evaluation (HWM drawdown & market breadth).
   * **14:20 – 14:45**: Execution window. Avoid first 30 minutes of open (09:30–10:00).
   * **Order types**: Limit orders pegged to bid/ask, 15-minute TWAP, or IOC for emergency stops.

### Phase 5: Export Standardized Dual-Language Manuals
Generate and save two comprehensive markdown documents:
1. `docs/ruleset/<strategy_name>/rules.md` (English Operating Manual)
2. `docs/ruleset/<strategy_name>/rules_cn.md` (Chinese Operating Manual / 中文实盘操作手册)

Each document must contain:
1. **Strategy Identity & Architecture Overview** (with Mermaid flowcharts)
2. **Daily Live Trading Routine (5-Step Operational Checklist)**
3. **Step-by-Step Decision Logic & Rules** (`IF / AND / OR / THEN` conditions)
4. **Human Trader Quick-Reference Card (Cheat Sheet Table)**
5. **Execution Nuances & Slippage Management**

---

## Automated Helper Script

A CLI script is available to inspect strategies and generate baseline rulesets automatically:

```bash
# Run from repository root:
python pipeline/.agents/skills/live-trading-rules/scripts/generate_rules.py --strategy <strategy_name>

# To force overwrite existing files:
python pipeline/.agents/skills/live-trading-rules/scripts/generate_rules.py --strategy <strategy_name> --force
```

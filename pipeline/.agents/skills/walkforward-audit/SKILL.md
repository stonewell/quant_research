---
name: walkforward-audit
description: >-
  Audits walkforward backtest results, inspects fold-level trade records for
  quantitative anomalies (single-stock concentration, warmup delay, cash drag,
  equity outliers, turnover friction, look-ahead bias), computes anomaly-adjusted
  Sharpe rankings, deeply analyzes top 3 strategies for behavioral distortions,
  recommends losing assets to exclude from the universe, and conducts forensic
  anomaly and overfitting audits (Leave-One-Out asset fragility, superstar winner skew,
  friction bleed, cash-normalized drawdown, overfit risk scoring, and safety grading).
  Use whenever the user asks to analyze walkforward backtests, check trade records,
  find anomalies in top strategies, audit for abnormalities and overfitting,
  reorder strategy rankings, recommend losing assets to exclude, or build
  risk-managed live trading strategies.
---

# Walkforward Audit & Live Strategy Formulation

This skill implements an institutional 6-phase quantitative audit process to evaluate walkforward backtesting results, detect hidden backtest distortions in fold-level trade logs, compute realistic live-readiness rankings, conduct deep behavioral analysis on top-3 strategies, identify persistent losing assets to prune from trading universes, and conduct forensic anomaly and overfitting audits with Leave-One-Out fragility testing and live-safety grading.

---

## Workflow Overview

```text
┌─────────────────────────────────────────────────────────────────┐
│ Phase 1: Aggregate Walkforward Results                         │
│ - Scan walkforward_summary.json files across all strategies     │
│ - Parse canonical strategy name directly from summary JSON      │
│   ("strategy" or "strategy_name" fields)                        │
│ - Filter statistical significance (DSR > 0.05, winning folds)   │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 2: Audit Fold Trading Records                            │
│ - Parse walkforward_rebalances.csv logs                         │
│ - Detect key anomaly patterns (concentration, warmup, jumps)    │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 3: Anomaly-Adjusted Ranking                              │
│ - Apply penalties: concentration, cash drag, jumps, turnover    │
│ - Apply bonuses: DSR significance, fold consistency             │
│ - Format rankings with exact strategy names                     │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 4: Top 3 Strategy Deep Behavioral Analysis                │
│ - Analyze fold dynamics (best vs worst fold, return dispersion) │
│ - Quantify cash drag, turnover velocity & friction tax (RMB)    │
│ - Check single-fold profit concentration ("one-hit wonder")     │
│ - Trace asset-level alpha attribution (top winners vs losers)   │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 5: Asset-Level Loss Drag & Losing Asset Exclusion         │
│ - Aggregate stateful PnL, ROI, and transaction costs by ticker  │
│ - Classify chronic losers, negative ROI drift, friction bleed   │
│ - Recommend losing assets to exclude from trading universe      │
│ - Export pruned universe file & generate CLI rerun snippet      │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Phase 6: Forensic Anomaly & Overfitting Audit                   │
│ - Quantitative Anomaly & Overfit Inspection Matrix (9 metrics)  │
│ - Leave-One-Out (LOO) Asset Fragility Analysis (Top 1/2/3)      │
│ - Turnover Friction Bleed & Churn Asset Cluster Analysis        │
│ - Capital Efficiency & Cash-Normalized Drawdown (Norm MaxDD)    │
│ - Overfit Risk Index (0-100) & Safety Grade (Grade A/B/C/F)     │
│ - Export Markdown Audit Report (--export-overfit-report)        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Step-by-Step Instructions

> [!IMPORTANT]
> **Strict Directory Scoping & Canonical Strategy Name Resolution**:
> - Strictly audit **only the directory specified by the user** (pass `--results-dir <TARGET_DIR>`).
> - **Never hardcode or fall back to `pipeline/results`** or any other directory outside the user's target path.
> - **Canonical Strategy Name in Summary JSON**: Both `walkforward_summary.json` and `comparison_report.json` generated by `backtester/run_backtest.py` explicitly embed `"strategy"` and `"strategy_name"` fields (e.g. `"Chan Composite Multi-Stage Scaling"`, `"Chan Risk-Managed Blend Strategy"`, `"Active Dual Momentum GTAA"`).
> - Always read the real canonical strategy name directly from `walkforward_summary.json` (or `comparison_report.json`). **Never use JSON file names, folder stems, or script names** (e.g., never display `"chan_composite"`, `"dual_momentum_strategy"`, or `"multi_strategy_alpha_book.py:50"`).
> - If an older summary JSON lacks these fields, resolve the canonical name via `pipeline/research_strategy/strategies_config.json` or `results/strategy_dumps/`, never falling back to raw JSON file names.
> - If `<TARGET_DIR>` contains `walkforward_summary.json` directly (a single backtest run), audit that run dynamically using its embedded strategy name.
> - If `<TARGET_DIR>` contains subdirectories, audit each strategy run within those subdirectories using its respective summary JSON.

### Step 1: Run Cross-Strategy Walkforward Summary

Run the automated summary tool to aggregate performance strictly for the specified target directory:

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> --summary
```

**Key Metrics to Examine**:
1. **Deflated Sharpe Ratio (DSR)**: A strategy with Sharpe $>1.5$ but DSR $\approx 0$ indicates high fold variance or lucky sample timing. Only DSR $>0.05$ (ideally $>0.50$ or $1.0$) demonstrates statistically verifiable alpha.
2. **Fold Consistency**: Check winning folds (e.g. $6/6$ vs $3/6$). Consistent positive returns across all rolling periods trump high-variance explosive gains.
3. **Strategy Family Distribution**: Identify which family (e.g. Chan Structural, Momentum, Risk Parity, Mean Reversion) dominates.

---

### Step 2: Audit Fold Trading Records for Hidden Anomalies

Run the deep-dive audit tool against candidate strategies in the specified target directory:

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> --audit --top-n 10
```

Refer to the [Anomaly Patterns Reference](./references/anomaly_patterns.md) for detailed diagnostics:

1. **Concentration Risk (All-In Bets)**:
   - Check if the strategy takes 100% bets on single stocks (`target_weight >= 0.99`).
   - *Risk*: Catastrophic single-stock halt/gap-down risk in live trading.
2. **Cash Drag / Capital Under-Investment**:
   - Check if average stateful held weight sum $\sum w_i \ll 1.0$ (e.g. 0.30 in VAA compound).
   - *Critical*: Must track active positions statefully per fold; naively summing individual trade-event rows calculates marginal trade adjustments (which omit untouched holdings) and severely overstates idle cash.
   - *Risk*: Distorts raw MaxDD and CAGR comparisons; requires gross vs. net normalization.
3. **Fold 1 Warmup Delay**:
   - Check the days from Fold 1 start to first trade (e.g. $+113$ days).
   - *Risk*: Idle cash period produces zero drawdown, artificially boosting Fold 1 Sharpe and Calmar.
4. **Outlier Equity Jumps**:
   - Identify single-rebalance equity jumps $>30\%$ (or $>100\%$).
   - *Risk*: High CAGR driven by a single lucky fold rather than persistent edge.
5. **Turnover & Execution Friction**:
   - Check turnover ratio and rebalance count.
   - *Risk*: Near-daily micro-adjustments incur severe stamp duty and slippage in A-share trading.
6. **Look-Ahead Bias Check**:
   - Check Buy $\to$ Rise hit rate. Normal trend-following is $45\%-55\%$. A hit rate $>75\%$ indicates potential data leakage.

---

### Step 3: Compute Anomaly-Adjusted Rankings

Run the scoring engine to reorder candidates based on realistic live trading readiness:

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> --rank --top-n 10
```

**Scoring Methodology**:
$$\text{Adj Sharpe} = \text{Raw Sharpe} - \text{Penalties} + \text{Bonuses}$$

- **Penalties**:
  - Concentration: $-0.05$ per all-in trade (max $-0.40$).
  - Under-investment: $-(1.0 - \bar{w}_{\text{sum}}) \times 0.50$.
  - Warmup bias: $-0.05$ if Fold 1 idle $>60$ days.
  - Outlier equity jump: $-(\text{jump} - 0.30) \times 0.30$ for jumps $>30\%$.
  - Turnover friction: $-(\text{turnover} \times 0.002 \times 2 \times 0.50)$ (max $-0.30$).
  - Tradability friction: $-0.10$ if rebalances $>50$ per fold.
- **Bonuses**:
  - DSR statistical confidence: $+0.15$ if DSR $>0.95$, $+0.10$ if DSR $>0.50$.
  - Fold consistency: $+0.10$ for $6/6$ winning folds, $+0.05$ if Sharpe std $<1.0$.

---

### Step 4: Deep Behavioral & Anomaly Analysis of Top 3 Strategies

Run an in-depth diagnosis across the top-ranked strategies (defaults to top 3):

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> --deep-analyze --deep-top-n 3
```

**Key Behavioral Checks for Each Top Strategy**:
1. **Regime & Fold Fragility**:
   - Check best fold vs worst fold CAGR and MaxDD.
   - Check **Profit Concentration**: If a single fold generates $>40\%$ of all positive returns, flag "one-hit wonder" regime sensitivity.
2. **Capital Efficiency vs Cash Drag**:
   - Examine average active weight sum $\bar{w}_{\text{sum}}$. If idle cash $>50\%$, verify whether low drawdown is simply due to holding cash.
3. **Turnover & Friction Tax**:
   - Quantify total friction costs (commissions, slippage, stamp duty) in RMB and as a percentage of gross alpha.
4. **Asset-Level Alpha Attribution**:
   - Identify which specific stocks drove the alpha (Top Alpha Drivers) vs which stocks caused major drawdowns (Severe Loss Drags).

---

### Step 5: Asset-Level Loss Drag & Losing Asset Exclusion

Consolidate asset PnL across the top evaluated strategies, identify chronic losers dragging down overall returns, and recommend a pruned universe:

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> \
  --deep-analyze \
  --exclude-losers \
  --universe-file docs/universe/china/original_universe.txt \
  --export-pruned-universe docs/universe/china/pruned_universe.txt
```

**Exclusion Diagnosis Criteria**:
- **Severe Loss Drag**: Cumulative net PnL $< -1,000$ RMB or ROI $< -3.0\%$.
- **Friction Bleed**: Transaction costs exceed gross gains (churn eating alpha).
- **Multi-Strategy Failure**: Asset produced negative PnL across $\ge 2$ different top strategies.
- **Quantified Output**:
  - Reports exact eliminated loss drag in RMB and friction fees saved.
  - Automatically exports the cleaned universe file (`--export-pruned-universe`).
  - Outputs the exact CLI command to re-run the backtester or pipeline with the pruned universe.

---

### Step 6: Forensic Anomaly & Overfitting Audit

Run the deep forensic anomaly and overfitting audit engine across candidate strategies (or the top N strategies):

```bash
# Terminal output with inspection matrix & LOO table
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> \
  --abnormal-overfit --deep-top-n 3

# Or export an audit report in GitHub-flavored Markdown
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> \
  --abnormal-overfit --deep-top-n 3 \
  --export-overfit-report docs/audit/forensic_overfit_report.md
```

**Quantitative Anomaly & Overfit Inspection Matrix**:
1. **Look-Ahead Bias Check**:
   - Buy hit rate: $45\% - 55\%$ is standard. If $>70\%$, flag as `DATA LEAKAGE RISK`.
2. **Single-Stock Concentration**:
   - Checks if any trade had $|w_i| \ge 80\%$ or $\ge 99\%$, and audits maximum single position against the $20\%$ cap.
3. **Fold 1 Warmup Padding**:
   - Checks if first trade in Fold 1 took $>60$ days to execute, artificially suppressing drawdown.
4. **Outlier Equity Jumps**:
   - Single-rebalance equity jump $>30\%$ indicates single-trade dependency or multi-bagger lucky timing.
5. **Statistical Edge (DSR)**:
   - Deflated Sharpe Ratio must be $>0.05$ (ideal $>0.50$). If $DSR \approx 0$, backtest alpha is likely data-mined noise.
6. **Profit Concentration Skew ("One-Hit Wonder")**:
   - If the single best fold generates $>40\%$ (or $>60\%$) of positive cumulative CAGR, flag `HIGH REGIME RISK`.
7. **Superstar Asset Fragility & Leave-One-Out (LOO) Analysis**:
   - Computes net profit share of Top 1, Top 2, Top 3 winning assets.
   - **LOO Sensitivity Test**: Automatically tests removing Top 1, Top 2, and Top 3 winners. If excluding the #1 winner flips net PnL from positive to **negative**, the strategy has **CRITICAL FRAGILITY / SUPERSTAR OVERFIT**.
8. **Friction Bleed Ratio**:
   - Quantifies transaction friction (commissions, slippage, stamp duty) as a percentage of net gains:
     $$\text{Friction Bleed} = \frac{\text{Total Friction Costs}}{\text{Net PnL}} \times 100\%$$
   - $>15\%$ is elevated; $>30\%$ indicates severe churn eroding true alpha.
9. **Capital Drag & Cash Flattery**:
   - Computes average active risky exposure $\bar{w}_{\text{active}}$ and idle cash $\%$.
   - Calculates **Cash-Normalized Maximum Drawdown**:
     $$\text{MaxDD}_{\text{normalized}} = \frac{\text{MaxDD}_{\text{raw}}}{\max(\bar{w}_{\text{active}}, 0.10)}$$
     Exposes whether low drawdowns are real alpha or simply cash flattery.

**Overfit Risk Index (0-100) & Safety Grades**:
- **Grade A (Institutional Grade, Risk $\le 20$)**: Distributed cross-sectional alpha, passes LOO test, disciplined friction, statistically significant.
- **Grade B (Moderate Caution, Risk $21 - 40$)**: Viable edge with live risk constraints (position limits, turnover throttles).
- **Grade C (High Overfit Risk, Risk $41 - 65$)**: Fragile; collapses if top winner or top fold is removed. Requires structural refactoring (anti-fragility engine, sector throttle).
- **Grade F (Rejected, Risk $> 65$)**: Severe overfit, data leakage, or unviable live execution.

---

### Full Pipeline Run

To execute the entire 6-phase audit (including loss drag exclusion and forensic overfit audit) in a single command:

```bash
python3 .agents/skills/walkforward-audit/scripts/run_audit.py --results-dir <TARGET_DIR> --all --deep-top-n 3
```

---

## Quick Reference Scripts

- **Complete Audit Runner**: [run_audit.py](./scripts/run_audit.py)
  - Options:
    - `--all`: Execute all 6 audit phases end-to-end.
    - `--summary`: Cross-strategy performance & DSR aggregation.
    - `--audit`: Fold trading record anomaly detection.
    - `--rank`: Anomaly-adjusted Sharpe ranking.
    - `--deep-analyze` / `--deep`: Behavioral fold & alpha driver attribution.
    - `--exclude-losers`: Chronic loser classification & universe pruning.
    - `--abnormal-overfit` / `--overfit`: Forensic anomaly & overfit audit (LOO fragility, inspection matrix, safety grade).
    - `--export-overfit-report PATH`: Export forensic report in GitHub markdown.
    - `--universe-file PATH` / `--export-pruned-universe PATH`: Cleaned universe export.
    - `--results-dir PATH`: Target results directory to audit.
- **Detailed Anomaly Reference**: [anomaly_patterns.md](./references/anomaly_patterns.md)
- **Live Risk Management Guide**: [live_risk_rules.md](./references/live_risk_rules.md)


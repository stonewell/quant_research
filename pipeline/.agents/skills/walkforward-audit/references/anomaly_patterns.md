# Walkforward Trading Record Anomaly Patterns

This guide documents the 7 recurring quantitative anomalies identified when auditing fold-level trading records (`walkforward_rebalances.csv` and `walkforward_summary.json`).

---

## 1. Single-Stock Concentration & "All-In" Bets

### Definition
A strategy places $\ge 80\%$ (or $100\%$) of portfolio NAV into a single equity ticker within a rebalance row.

### Why It Distorts Backtests
- In historical simulations, hitting a stock during a momentum rally (e.g. 300124.SZ or 688041.SH) yields massive CAGR without showing proportional drawdown if no sudden gap-down occurred.
- In live trading, holding $100\%$ in one stock exposes the fund to single-stock halts, limit-down locks, or fraud/governance shocks.

### Detection Rule
```python
all_in_trades = [t for t in trades if abs(t["target_weight"]) >= 0.99]
```

### Remediation
Enforce a hard single-stock cap (e.g., `crb_max_single_position = 0.20`). If fewer assets trigger entry, leave the remainder in `cash_proxy`.

---

## 2. Chronic Capital Under-Investment & Cash Cushion Drag

### Definition
Total sum of weights across all assets (including cash proxy) is significantly less than 1.0 (e.g., $\sum w_i \le 0.30$).

### Why It Distorts Backtests
- A strategy investing only 30% capital in risky assets will report artificially low Max Drawdown (e.g. 2.7%) and low CAGR (12.9%).
- On a per-invested-dollar basis, the strategy might have 40%+ CAGR and 9% MaxDD. Comparing its raw Sharpe or Calmar directly against 100% invested strategies creates a misleading comparison.

### Detection Rule (Stateful Holdings Tracking)
> [!WARNING]
> `rebalance_report.csv` / `walkforward_rebalances.csv` only emits rows for assets whose positions were **adjusted** (`|weight_change| > 1e-7`). For asynchronous or asset-level inertia strategies, untouched held assets do not emit rows. Naively summing `target_weight` inside trade-event rows calculates **marginal rebalance flow**, not **portfolio holdings**, falsely reporting active portfolios as 80-90% idle cash!
> 
> You MUST track cumulative active holdings statefully per fold:

```python
# Stateful position tracking across rebalance events per fold:
held_weight_sums = []
for fold in sorted(set(t["fold"] for t in trades)):
    ft = [t for t in trades if t["fold"] == fold]
    events = sorted(set((t["rebalance_id"], t["date"]) for t in ft))
    event_trades = defaultdict(list)
    for t in ft:
        event_trades[(t["rebalance_id"], t["date"])].append(t)
    
    held = {}
    for ev in events:
        for t in event_trades[ev]:
            if t["target_weight"] > 1e-7:
                held[t["symbol"]] = t["target_weight"]
            else:
                held.pop(t["symbol"], None)
        held_weight_sums.append(sum(held.values()))

avg_weight_sum = statistics.mean(held_weight_sums) if held_weight_sums else 1.0
if avg_weight_sum < 0.80:
    # Strategy has genuine unallocated capital drag across active periods
```

### Remediation
Report both gross NAV metrics and capital-deployed normalized metrics. For live deployment, blend with an active core engine to utilize idle capital.

---

## 3. Fold 1 Warmup Delay & Idle Period Bias

### Definition
The strategy has zero trades for the first 60–120+ days of Fold 1 while indicators accumulate historical bars.

### Why It Distorts Backtests
- In rolling walkforward folds, if Fold 1 starts on 2022-08-22 but trades do not execute until 2022-12-13 (+113 days), the portfolio sits in cash with 0.0 drawdown.
- Sharpe and Calmar ratios calculated over the full window are distorted: the idle period inflates the denominator (time) or artificially suppresses volatility.

### Detection Rule
```python
first_trade_date = min(t["date"] for t in fold1_trades)
warmup_gap_days = (first_trade_date - fold1_start_date).days
if warmup_gap_days > 60:
    # Fold 1 Sharpe is biased by extended idle cash period
```

### Remediation
Evaluate performance both across all folds and excluding Fold 1 (`ex-F1`), ensuring strategies are tested when indicators are already hot.

---

## 4. Single-Rebalance Outlier Equity Jumps

### Definition
Portfolio equity increases by $>30\%$ (or up to $100\%+$) between two consecutive rebalance dates.

### Why It Distorts Backtests
- High mean CAGR is often driven by a single lucky fold (e.g., 450% annualized in Fold 2) catching a multi-bagger move.
- The remaining folds may have modest returns (10–20%), masking regime fragility.

### Detection Rule
```python
equity_jumps = [(eq[i] - eq[i-1]) / eq[i-1] for i in range(1, len(eq))]
max_jump = max(equity_jumps)
if max_jump > 0.30:
    # High-impact outlier trade detected
```

### Remediation
Apply an outlier jump penalty when calculating adjusted Sharpe. Verify whether the strategy wins in folds without the outlier jump.

---

## 5. Micro-Rebalance Turnover & Transaction Friction

### Definition
Frequent, low-magnitude weight shifts (e.g. daily adjustments of $<2\%$ weight per symbol) across dozens of stocks.

### Why It Distorts Backtests
- Flat fee backtests (e.g. 5bps commission + 5bps slippage) understate market impact, stamp duties (10bps in A-shares), and bid-ask spread friction.
- Annualized turnover of $15\times - 30\times$ can erase 3–6% of annual alpha in live execution.

### Detection Rule
```python
avg_rebal_dates_per_fold = total_rebalances / num_folds
if avg_rebal_dates_per_fold > 40:
    # High execution friction risk
```

### Remediation
Implement a turnover filter: suppress rebalances where $\max_i |\Delta w_i| < 0.02$ (2%).

---

## 6. Dead / Missing Fold Activity

### Definition
A strategy executes 0 trades across an entire 6-month fold (e.g., Fold 1 has 0 trades and 0.0 return).

### Why It Distorts Backtests
- Counting a 0.0 return fold as an active trial drags down the mean Sharpe and artificially alters the Deflated Sharpe Ratio calculation.
- Suggests either excessive parameter stiffness or universe mismatch (assets never satisfied entry rules).

### Detection Rule
```python
active_folds = [f for f in folds if f["trades_count"] > 0]
if len(active_folds) < len(folds):
    # Strategy was inactive during certain market regimes
```

---

## 7. Look-Ahead Bias & Directional Timing Anomalies

### Definition
Trade execution systematically precedes large price moves with suspiciously high accuracy ($>75\%$ buy hit rates).

### Diagnostic Rule
Check if price consistently rises after BUY across symbols:
```python
buy_hit_rate = sum(1 for t in buys if next_price > entry_price) / len(buys)
# Genuine trend-following strategies typically have 45% - 55% hit rates.
# An entry hit rate > 75% on large sample size indicates potential look-ahead leakage.
```

---

## 8. Chronic Asset-Level Loss Drag & Friction Bleed

### Definition
Certain individual assets in the universe systematically generate negative realized PnL across multiple rolling folds, where transaction fees (stamp duties, commissions, slippage) exceed gross price returns or structural downtrends repeatedly trigger stop-outs.

### Why It Distorts Backtests
- In walkforward backtests with broad universes (e.g. 30–50 symbols), a handful of chronic underperformers can shave 5–10% off cumulative CAGR.
- High turnover on non-trending assets incurs significant friction without generating alpha, diluting winning stock contributions.

### Detection Rule
```python
# Track stateful PnL per symbol across all folds:
# Net PnL = Sells + Terminal Value - Buys - Costs
net_pnl = st["sells"] + st["end_val"] - st["buys"] - st["costs"]
if net_pnl < 0 and (roi < -3.0 or st["costs"] > abs(net_pnl) * 0.5):
    # Flag as candidate for universe exclusion
```

### Remediation
Exclude identified chronic losers from the universe definition file (e.g., pruning from 47 to 31 or 20 assets) and re-run rolling evaluations.

---

## 9. Single-Fold Profit Concentration ("One-Hit Wonder" Risk)

### Definition
A strategy produces attractive mean CAGR (e.g. 15–20%), but $>40\%$ to $60\%+$ of total positive returns originate from a single exceptional fold (e.g., Fold 5 catching an explosive beta rally), while remaining folds are flat or negative.

### Why It Distorts Backtests
- The strategy lacks all-weather robustness: it appears profitable on average only because one favorable macro window compensated for multiple losing quarters.
- In live trading, deploying such a strategy during normal or hostile regimes exposes capital to extended drawdown before the favorable regime recurs.

### Detection Rule
```python
pos_cagr_sum = sum(c for c in fold_cagrs if c > 0)
max_fold_share = max(fold_cagrs) / pos_cagr_sum if pos_cagr_sum > 0 else 0.0
if max_fold_share > 0.40 and len(fold_cagrs) > 2:
    # Flag as regime-fragile "one-hit wonder"
```

### Remediation
Apply an anomaly penalty in adjusted Sharpe rankings and verify fold win rate consistency ($\ge 60\%$ winning folds).

---

## 10. Superstar Asset Fragility & Leave-One-Out (LOO) Sensitivity

### Definition
A quantitative strategy generates positive net portfolio PnL across rolling walkforward windows, but the gains are heavily concentrated in 1 to 3 "superstar" winning assets (e.g. contributing $>100\%$ to $250\%+$ of total net profit while other assets generate net negative returns).

### Why It Distorts Backtests
- The strategy does not possess systemic cross-sectional alpha. Instead, it accidentally caught a single idiosyncratic macro breakout (e.g. shipping cycle or AI rally).
- If that single asset is omitted, or in live trading does not replicate its historic multi-bagger move, the remaining portfolio constituents suffer negative drift and fee drag.

### Detection Rule (Leave-One-Out Test)
```python
# Compute stateful net PnL across all assets:
total_net_pnl = sum(a["net_pnl"] for a in assets)
winners = sorted([a for a in assets if a["net_pnl"] > 0], key=lambda x: x["net_pnl"], reverse=True)

# LOO Step 1: Omit Top 1 winner
pnl_ex_top1 = total_net_pnl - winners[0]["net_pnl"]
if pnl_ex_top1 <= 0:
    # CRITICAL FRAGILITY: Omitting single top asset flips strategy to net unprofitable!
```

### Remediation
1. Enforce sector concentration throttles (max 1 stock per industry in momentum slots).
2. Apply inverse-volatility risk-balanced sizing (`w_i \propto score_i / vol_i`) to prevent high-beta stocks from monopolizing portfolio variance.
3. Require multi-asset dispersion floors ($\ge 5$ distinct assets participating in allocations).

---

## 11. Transaction Friction Bleed Ratio

### Definition
Transaction costs (commissions, slippage, and stamp taxes) consume an outsized portion of gross trading profits ($\ge 15\%$ elevated, $\ge 30\%$ severe bleed) due to high-frequency micro-rebalancing or churning on non-trending assets.

### Why It Distorts Backtests
- Academic models assuming zero or minimal flat fees mask the compounding drag of turnover friction.
- Non-trending assets generate repeated small losses and high commissions, eroding the alpha generated by winning assets.

### Detection Rule
```python
friction_bleed_ratio = (total_costs / total_net_pnl) * 100.0
if friction_bleed_ratio > 30.0:
    # SEVERE FRICTION BLEED: >30% of alpha is paid to brokers/exchanges
```

### Remediation
1. Suppress micro-rebalancing: implement minimum weight change threshold ($|\Delta w| \ge 5\%$).
2. Inherit friction suppression directly in backtester loops to eliminate passive price-drift trades.
3. Add momentum hurdle thresholds ($10\text{d ROC} \ge 1.0\%$) and post-stop cooldowns (15 bars) to avoid whipsaw churn.

---

## 12. Capital Drag & Cash-Normalized Drawdown Flattery

### Definition
A strategy holds significant uninvested idle cash (e.g. $30\% - 50\%$ average cash reserve) through risk gates or cash buffers, resulting in an artificially depressed maximum drawdown (e.g., $3.8\%$).

### Why It Distorts Backtests
- The headline drawdown ($3.8\%$) is mathematically dampened by cash cushioning.
- When evaluating live equity risk, an investor deploying $100\%$ capital to equities will experience substantially higher volatility.

### Detection Rule
```python
avg_active_exposure = statistics.mean(held_active_weights)
idle_cash_pct = max(0.0, 1.0 - avg_active_exposure) * 100.0

cash_normalized_maxdd = raw_maxdd / max(avg_active_exposure, 0.10)
# Example: 3.84% raw MaxDD with 62% average exposure -> 6.19% normalized MaxDD
```

### Remediation
Always report both raw headline metrics and capital-normalized drawdown/CAGR metrics to ensure realistic apples-to-apples comparisons.

---

## 13. Quantitative Overfitting Risk Score & Live Trading Safety Grading

### Definition
A composite forensic index (0 to 100) aggregating multiple structural overfit and fragility vectors into an institutional live-trading safety grade:

| Overfit Vector | Penalty Weight | Diagnostic Threshold |
| :--- | :--- | :--- |
| **Deflated Sharpe (DSR)** | +25 pts | $DSR < 0.05$ (High sample-mining noise probability) |
| **Superstar Fragility (LOO)**| +25 pts | Omitting Top 1 winner flips net PnL negative |
| **Winner Concentration** | +15 pts | Top 3 winners account for $>80\%$ of net PnL |
| **One-Hit Wonder Fold** | +15 pts | Best single fold generates $>50\%$ of positive CAGR |
| **Friction Bleed** | +10 pts | Transaction fees consume $>25\%$ of net PnL |
| **Extreme Concentration** | +15 pts | Single asset position reaches $\ge 80\%$ |
| **Outlier Equity Jump** | +10 pts | Single rebalance jump $>30\%$ |
| **Look-Ahead Leakage** | +25 pts | Buy hit rate $>70\%$ |
| **Warmup Delay Padding** | +10 pts | First trade delayed $>60$ days in Fold 1 |

### Institutional Safety Grades
- **Grade A (Institutional Grade, Risk $\le 20$)**: Distributed cross-sectional alpha, passes LOO test, disciplined friction, statistically validated.
- **Grade B (Moderate Caution, Risk $21 - 40$)**: Genuine edge with live risk constraints (position caps, turnover filters).
- **Grade C (High Overfit Risk, Risk $41 - 65$)**: Fragile; collapses if top winner or favorable fold is removed. Requires structural refactoring.
- **Grade F (Rejected, Risk $> 65$)**: Severe overfit, data leakage, or unviable execution.


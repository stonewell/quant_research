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

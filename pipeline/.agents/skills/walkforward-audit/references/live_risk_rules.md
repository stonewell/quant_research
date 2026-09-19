# Institutional Live Trading Risk Rules

This reference outlines production-grade risk controls to convert backtested quant strategies into institutional live trading implementations.

---

## 1. Position Limits & Diversification Floor

| Constraint | Limit | Purpose |
|------------|-------|---------|
| **Single-Stock NAV Cap** | $\le 20\%$ | Prevents idiosyncratic black swan events (halts, fraud, earnings gaps). |
| **Minimum Position Floor** | $\ge 5$ stocks | Guarantees diversification; single-stock signals leave remainder in cash. |
| **Gross Leverage Ceiling** | $\le 100\%$ | Long-only execution without margin interest friction. |
| **Sector Allocation Cap** | $\le 40\%$ | Prevents over-concentration in cyclical or policy-sensitive industries. |

---

## 2. Multi-Level Drawdown Circuit Breakers

Circuit breakers measure peak-to-trough decline from High-Water Mark (HWM) on a daily close basis:

```
                  Normal Regime (0% - 10% Drawdown)
                                 │
                   Drawdown >= 10% from HWM
                                 ▼
         Tier 1: Risk Reduction (Halve Equity Allocation)
            - 50% capital derouted to cash_proxy
            - Retains remaining 50% to capture rebounds
                                 │
                   Drawdown >= 15% from HWM
                                 ▼
         Tier 2: Tactical Defense (Shift to VAA / Defense)
            - 100% allocation routed to defensive buffer
            - 70% cash / short-term debt preservation
                                 │
                   Drawdown >= 20% from HWM
                                 ▼
         Tier 3: Hard Circuit Breaker (100% Cash Stop)
            - Full exit to cash_proxy
            - Automatic trading halt pending quantitative review
```

---

## 3. Turnover Filter & Minimum Trade Threshold

Frequent micro-adjustments generate unnecessary slippage and brokerage fees.
- **Rule**: A rebalance instruction is emitted **only** if at least one asset changes by $\ge 2\%$ ($\Delta w \ge 0.02$).
- **Sparse Execution**: When $\Delta w < 0.02$ across all holdings, the previous day's targets are maintained, and the sparse weights row remains `NaN`.

---

## 4. Execution Constraints & Slippage Budgets

| Parameter | Institutional Standard | Rationale |
|-----------|------------------------|-----------|
| **Trading Window** | 10:00 – 14:30 | Avoid opening call auction volatility and closing imbalances. |
| **Order Type** | VWAP / TWAP | Minimize market impact across liquid constituents. |
| **Participation Rate** | $\le 5\%$ of ADV | Keep orders below the radar of adverse selection algorithms. |
| **Cost Model** | Stamp duty: 10bps (sell)<br>Commission: 3-5bps<br>Slippage: 10-20bps | Total round-trip budget: ~35–50bps. |
| **Turnover Budget** | $\le 25\times$ annualized | Hard cap to protect net alpha from fee erosion. |

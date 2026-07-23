# Researched Quantitative Trading Strategies (`research_strategy`)

A dedicated side project implementing and evaluating three top-tier quantitative tactical asset allocation (TAA) strategies synthesized from academic literature and practitioner research (*Journal of Finance*, *Journal of Portfolio Management*, SSRN, AllocateSmartly).

---

## 1. Overview & Strategy Catalog

```
+-----------------------------------------------------------------------------------+
|                        research_strategy Side Project                             |
+-----------------------------------------------------------------------------------+
                                          |
          +-------------------------------+-------------------------------+
          |                               |                               |
          v                               v                               v
+-------------------+           +-------------------+           +-------------------+
|  Strategy 1:      |           |  Strategy 2:      |           |  Strategy 3:      |
|  Active Dual      |           |  Bold Asset       |           |  Volatility-      |
|  Momentum GTAA    |           |  Allocation (BAA) |           |  Managed          |
|  (Antonacci /     |           |  (Wouter Keller   |           |  Portfolios       |
|  Faber)           |           |  2022 SSRN)       |           |  (Moreira & Muir  |
|                   |           |                   |           |  2017 J. Finance) |
+-------------------+           +-------------------+           +-------------------+
```

### Strategy 1: Active Dual Momentum GTAA + Risk Parity
* **Academic Grounding**: Gary Antonacci (2014, *Journal of Portfolio Management*, "Risk-Adjusted Momentum Strategies"); Meb Faber (2007, *Journal of Wealth Management*, "A Quantitative Approach to Tactical Asset Allocation").
* **Mathematical Mechanics**:
  1. **Absolute Momentum Gate**: Asset $i$ must satisfy $Close_i(t) > SMA_{200, i}(t)$ and $ROC_{126, i}(t) > 0$. Assets failing either condition are disqualified to avoid severe drawdowns.
  2. **Multi-Horizon Relative Momentum Ranking**: Qualifying assets are scored via $Score_i(t) = 0.5 \cdot ROC_{63, i}(t) + 0.5 \cdot ROC_{126, i}(t)$. Select top $K=3$ assets.
  3. **Inverse Volatility Risk Parity Weighting**:
     $$w_i = \frac{1/\sigma_{60, i}}{\sum_{j \in \text{Selected}} 1/\sigma_{60, j}} \cdot \left(\frac{M}{K}\right)$$
     where $M$ is the number of passing assets ($\le K$).
  4. **Defensive Cash Overlay**: Unallocated weight $(1 - \sum w_i)$ steps into cash proxy (`BIL`).

### Strategy 2: Wouter Keller's Bold Asset Allocation (BAA-G12)
* **Academic Grounding**: Wouter J. Keller (2022, SSRN "Relative and Absolute Momentum in Times of Rising/Low Yields: Bold Asset Allocation").
* **Mathematical Mechanics**:
  1. **Canary Universe Turbulence Detector**: `["SPY", "EEM", "EFA", "AGG"]`.
  2. **Canary Trigger**: If **any** canary asset has negative 12-month / 13-week momentum ($Close < SMA_{200}$ or $ROC_{126} < 0$), market state is flagged as **Turbulent**. Otherwise, **Calm**.
  3. **Universe Switching**:
     * **Calm State**: Trade **Offensive Universe** (`SPY`, `QQQ`, `IWM`, `EFA`, `EEM`, `TLT`, `LQD`, `DBC`). Equal-weight top $K=3$ assets by 126-day $ROC$.
     * **Turbulent State**: Trade **Defensive Universe** (`TIP`, `IEF`, `TLT`, `BIL`, `AGG`, `DBC`). Equal-weight top $K=3$ defensive assets with positive 126-day $ROC$. Any unallocated slots move to `BIL` cash proxy.

### Strategy 3: Moreira & Muir Volatility-Managed Portfolios (VolTiming)
* **Academic Grounding**: Alan Moreira & Tyler Muir (2017, *Journal of Finance* 72(4):1611–1644, "Volatility-Managed Portfolios").
* **Mathematical Mechanics**:
  1. Scales baseline portfolio exposure $f(t)$ dynamically by the inverse of recent 20-day realized volatility $\hat{\sigma}_{20, t-1}$:
     $$f_{\text{managed}}(t) = \min\left(1.0, \frac{\text{Target Volatility}}{\hat{\sigma}_{20, t-1}}\right) \cdot f(t)$$
  2. Unallocated weight $(1 - f_{\text{managed}}(t))$ is held in cash proxy (`BIL`).
  3. Eliminates momentum crash tail risk (Barroso & Santa-Clara 2015) by rapidly de-leveraging during market volatility spikes.

---

## 2. Strictly Offline Testing Policy

**NO REAL MARKET DATA IS FETCHED OR REQUIRED.**
To ensure fast, reproducible execution across any environment without internet access or API dependencies, all CLI runs and unit tests execute strictly against **synthetic multi-asset OHLCV data** generated via correlated geometric Brownian motion and factor drift models (`common/testing.py`).

---

## 3. Directory Structure

```
apps/quant/research_strategy/
├── rs/
│   ├── __init__.py
│   ├── config.py              # StrategyConfig (universes, lookback periods, risk parameters)
│   └── strategy.py            # Dual Momentum, BAA-G12, and Volatility-Managed implementations
├── run_research_strategy.py   # CLI runner with synthetic data generator & backtester
├── dashboard.py               # Terminal ASCII report viewer
├── tests/
│   └── test_strategy.py       # Offline unit tests for all strategy mechanics
└── README.md                  # Strategy formulations, citations, and guide
```

---

## 4. Usage Guide

### Running Unit Tests
Execute unit tests using the workspace virtual environment:
```powershell
strategy_generator\.venv\Scripts\python.exe -m pytest research_strategy/tests -v
```

### Running CLI Backtests
Simulate portfolio backtests on synthetic multi-asset data across all strategies:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/run_research_strategy.py --strategy all
```
Options for `--strategy`: `dual_momentum`, `baa_keller`, `volatility_managed`, `all`.

### Viewing Terminal Dashboard
Launch the terminal dashboard to view side-by-side performance summaries and recent target allocations:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/dashboard.py
```

---

## 5. Key Metrics Reported

* **Sharpe Ratio**: Annualized return per unit of total risk ($R_p / \sigma_p$).
* **CAGR**: Compound Annual Growth Rate over the simulated period.
* **Max Drawdown**: Maximum peak-to-trough decline.
* **Calmar Ratio**: Annualized return divided by maximum drawdown ($\text{CAGR} / |\text{Max DD}|$).
* **Win Rate & Profit Factor**: Percentage of positive daily return periods and ratio of gross gains to gross losses.
* **Turnover & Rebalances**: Accumulated portfolio rebalance turnover and rebalance count.

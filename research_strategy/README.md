# Researched Quantitative Trading Strategies (`research_strategy`)

A dedicated side project implementing and evaluating nine quantitative trading strategies: five tactical asset allocation (TAA) strategies synthesized from academic literature and practitioner research (*Journal of Finance*, *Journal of Portfolio Management*, SSRN, AllocateSmartly), plus four single-asset timing strategies consolidated into this project from this workspace's former standalone `rsi_strategy`, `swing_trend_strategy`, `grid_trading`, and `ensemble_strategy` side projects.

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

### Strategy 4: Accelerating Dual Momentum (ADM)
* **Academic/Practitioner Grounding**: Chris Ludlow & Steve Hanly (2018, EngineeredPortfolio.com), independently tracked by AllocateSmartly (ranked 5th most popular TAA strategy by member allocation as of this research).
* **Universe**: exactly 4 ETFs -- `SPY` (US large-cap), `SCZ` (international small-cap equities), and a defensive pair `TLT` (20+yr Treasuries) / `TIP` (TIPS).
* **Mathematical Mechanics**:
  1. **Momentum Score**: for `SPY` and `SCZ`, $Score_i(t) = \frac{1}{3}\left(ROC_{21,i}(t) + ROC_{63,i}(t) + ROC_{126,i}(t)\right)$ -- the simple average of trailing 1/3/6-month total returns.
  2. **Binary Switch**: if $Score_{SPY} > Score_{SCZ}$ and $Score_{SPY} > 0$, hold 100% `SPY`. Elif $Score_{SCZ} > Score_{SPY}$ and $Score_{SCZ} > 0$, hold 100% `SCZ`.
  3. **Defensive Fallback**: if neither equity sleeve has positive relative+absolute momentum, hold 100% of whichever of `TLT`/`TIP` has the higher trailing 1-month ($ROC_{21}$) return.
  4. Fully concentrated, single-asset holding -- no partial or cash allocation is part of the published rule.
* **Caveat**: AllocateSmartly explicitly characterizes ADM as an especially aggressive strategy with the highest annualized volatility of any strategy they track -- a known characteristic of this fully-concentrated design, not a defect.

### Strategy 5: Vigilant Asset Allocation (VAA-G4)
* **Academic Grounding**: Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624, "Breadth Momentum and Vigilant Asset Allocation (VAA): Winning More by Losing Less"). This project implements the aggressive, fully-concentrated **G4 (T=1/B=1)** variant the authors recommend, not the diversified Balanced/G12 variant.
* **Mathematical Mechanics**:
  1. **13612W Momentum Score**: $Score_i(t) = 12\cdot(p_0/p_1 - 1) + 4\cdot(p_0/p_3 - 1) + 2\cdot(p_0/p_6 - 1) + 1\cdot(p_0/p_{12} - 1)$, a 12/4/2/1-weighted blend of 1/3/6/12-month returns (implemented as $ROC_{21}$/$ROC_{63}$/$ROC_{126}$/$ROC_{252}$ trading days).
  2. **Binary Switch**: if **every** offensive-universe asset has a positive score, hold 100% of the single highest-scoring offensive asset. Otherwise (any offensive asset's score is non-positive), hold 100% of the single highest-scoring defensive asset.
  3. Fully concentrated, single-asset holding, no diversification within the chosen sleeve -- this is what distinguishes G4 from the Balanced/G12 variant.
* **CAVEAT -- disputed universe**: Keller & Keuning's own published offensive/defensive ticker list could **not** be confirmed with high confidence. Two different candidate universes attributed to the paper by secondary sources were each independently checked and refuted during research for this project. `StrategyConfig.vaa_offensive_universe`/`vaa_defensive_universe` (default: offensive `SPY, QQQ, EFA, EEM`; defensive `IEF, BIL`) are **illustrative defaults only**, not a verified reproduction of the original paper's universe -- substitute your own before treating results as a paper replication. The 13612W formula and binary switching logic above are independently well-verified and unaffected by this caveat.

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
│   ├── nl_parser.py           # Plain-English strategy description -> ParsedStrategySpec
│   └── strategy.py            # NaturalLanguageStrategy engine + all 5 strategy implementations
├── run_research_strategy.py   # CLI runner with synthetic data generator & backtester
├── dashboard.py               # Terminal ASCII report viewer
├── tests/
│   ├── test_nl_parser.py      # Offline unit tests for the plain-English parser
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
Options for `--strategy`: `dual_momentum`, `baa_keller`, `volatility_managed`, `accelerating_dual_momentum`, `vigilant_asset_allocation`, `all`.

You can also evaluate a strategy written in plain English instead of a preset, via the `NaturalLanguageStrategy` engine (`--description`/`--description-file`; see `rs/nl_parser.py`). This path currently covers the equal-weight/inverse-volatility/volatility-managed/canary-switch mechanics behind Strategies 1-3; ADM and VAA (Strategies 4-5) are standalone classes not yet expressible through the parser.

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

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
* **Academic/Practitioner Grounding**: Chris Ludlow & Steve Hanly (2018, EngineeredPortfolio.com), independently tracked by AllocateSmartly.
* **Universe**: 4 ETFs -- `SPY`, `SCZ`, and defensive pair `TLT` / `TIP`.

### Strategy 5: Vigilant Asset Allocation (VAA-G4)
* **Academic Grounding**: Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624).
* **Mechanics**: 13612W momentum score with binary switching between offensive and defensive universes.

### Strategy 6–9: Consolidated Timing Strategies
* **RSI(2) Mean-Reversion**: Connors-style short-term RSI mean-reversion timing across active symbols.
* **Trend-Pullback Swing**: Trend-following pullback strategy buying dips in confirmed uptrends.
* **ATR-Adaptive Grid**: Volatility-scaled grid trading strategy with trend filters and drawdown stop.
* **Regime-Switching Ensemble**: ADX regime-switching ensemble combining trend-following and RSI mean-reversion.

### Strategy 10–11: Turtle Channel Breakout Strategies (S1 & S2)
* **Historical & Academic Grounding**: Richard Dennis & William Eckhardt (1983 "Turtle Traders"), Richard Donchian (1960 "High-Low Channel Breakout"), Robert Carver (2023 "Systematic Trading").
* **Mathematical Mechanics**:
  1. **Donchian Breakout Entry**:
     * **System 1 (S1 - 20-day)**: Long entry when $Close_i(t) > \max(High_i(t-20 \dots t-1))$.
     * **System 2 (S2 - 55-day)**: Long entry when $Close_i(t) > \max(High_i(t-55 \dots t-1))$.
  2. **Trend Filter**: Optional $Close_i(t) > SMA_{200, i}(t)$ gate to prevent buying breakouts in secular bear markets.
  3. **Donchian & $2N$ ATR Exits**:
     * **Donchian Low Exit**: Exits when $Close_i(t) < \min(Low_i(t-N_{\text{exit}} \dots t-1))$ (10-day for S1, 20-day for S2).
     * **$2N$ ATR Trailing Stop**: Exits when price drops $2 \times \text{ATR}_{20}$ below the peak price achieved since entry.
  4. **Inverse-ATR Volatility Sizing**: Normalizes risk exposure across active breakout symbols proportional to $1 / (\text{ATR}_{20} / Close)$. Unallocated capital defaults to cash proxy (`BIL`).

---

## 2. JSON Strategy Configuration (`strategies_config.json`)

All strategy parameters, descriptions, and plain English definitions are defined in `research_strategy/strategies_config.json` instead of being hardcoded.

### Schema
The configuration supports two strategy entry types:
1. **Plain English Strategies (`"type": "natural_language"`)**:
   Uses `"plain_english_description"` parsed dynamically by the Natural Language Strategy Engine (`nl_parser.py`).
2. **Class-based Strategies (`"type": "class"`)**:
   Specifies `"class_name"` mapped to python strategy implementations (`rs/strategy.py`).

Example structure:
```json
{
  "dual_momentum": {
    "name": "Active Dual Momentum GTAA",
    "type": "natural_language",
    "plain_english_description": "Rebalance monthly. Risky assets: SPY, QQQ, IWM, EFA, EEM, GLD, TLT, VNQ. Apply absolute trend gate: Close > 200d SMA and 126d ROC > 0. Rank passing assets by 63d and 126d momentum, select top 3 assets, and allocate using 60d inverse volatility risk parity weighting. Assign unallocated capital to cash proxy BIL.",
    "parameters": {
      "rebalance_freq_days": 21,
      "top_k": 3
    }
  },
  "rsi_mean_reversion": {
    "name": "RSI(2) Mean-Reversion",
    "type": "class",
    "class_name": "RSIMeanReversionStrategy",
    "description": "Connors-style RSI(2) long-only mean-reversion timing strategy.",
    "parameters": {
      "rsi_symbol": "SPY",
      "rsi_period": 2,
      "rsi_oversold_threshold": 10.0,
      "rsi_exit_rsi_threshold": 70.0
    }
  }
}
```

---

## 3. Strictly Offline Testing Policy

**NO REAL MARKET DATA IS FETCHED OR REQUIRED.**
All CLI runs and unit tests execute strictly against **synthetic multi-asset OHLCV data** generated via correlated geometric Brownian motion and factor drift models (`common/testing.py`).

---

## 4. Directory Structure

```
apps/quant/research_strategy/
├── rs/
│   ├── __init__.py
│   ├── config.py              # StrategyConfig & load_strategies_config()
│   ├── nl_parser.py           # Plain-English strategy description -> ParsedStrategySpec
│   └── strategy.py            # NaturalLanguageStrategy engine + strategy implementations
├── strategies_config.json     # Central JSON configuration for all strategies & parameters
├── run_research_strategy.py   # CLI runner loading strategy configs dynamically
├── dashboard.py               # Terminal ASCII report viewer
├── tests/
│   ├── test_nl_parser.py      # Offline unit tests for the plain-English parser
│   └── test_strategy.py       # Offline unit tests for all strategies & config loading
└── README.md                  # Strategy formulations, citations, and guide
```

---

## 5. Usage Guide

### Running Unit Tests
Execute offline unit tests using the workspace virtual environment:
```powershell
strategy_generator\.venv\Scripts\python.exe -m pytest research_strategy/tests -v
```

### Running CLI Backtests
Simulate portfolio backtests on synthetic multi-asset data across all strategies loaded from JSON config:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/run_research_strategy.py --strategy all
```

Pass a custom JSON configuration file:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/run_research_strategy.py --config custom_config.json --strategy all
```

Evaluate custom plain English strategies via CLI text:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/run_research_strategy.py --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility."
```

### Viewing Terminal Dashboard
Launch the terminal dashboard to view side-by-side performance summaries and recent target allocations:
```powershell
strategy_generator\.venv\Scripts\python.exe research_strategy/dashboard.py
```

---

## 6. Key Metrics Reported

* **Sharpe Ratio**: Annualized return per unit of total risk ($R_p / \sigma_p$).
* **CAGR**: Compound Annual Growth Rate over the simulated period.
* **Max Drawdown**: Maximum peak-to-trough decline.
* **Calmar Ratio**: Annualized return divided by maximum drawdown ($\text{CAGR} / |\text{Max DD}|$).
* **Win Rate & Profit Factor**: Percentage of positive daily return periods and ratio of gross gains to gross losses.
* **Turnover & Rebalances**: Accumulated portfolio rebalance turnover and rebalance count.

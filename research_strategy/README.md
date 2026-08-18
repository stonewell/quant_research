# Researched Quantitative Trading Strategies (`research_strategy`)

A dedicated side project implementing and evaluating seventeen quantitative trading strategies: five tactical asset allocation (TAA) strategies synthesized from academic literature and practitioner research (*Journal of Finance*, *Journal of Portfolio Management*, SSRN, AllocateSmartly), four single-asset timing strategies consolidated into this project from this workspace's former standalone `rsi_strategy`, `swing_trend_strategy`, `grid_trading`, and `ensemble_strategy` side projects, two Donchian channel breakout systems, four modern, actively-followed static/fixed-weight portfolios popular with retail and practitioner communities today (Permanent Portfolio, Golden Butterfly, All Weather, HFEA), and two modern systematic TAA extensions added in a follow-up deep-research pass on "modern, popular, effective" strategies (Protective Asset Allocation, Adaptive Asset Allocation) -- see "Strategy 12-17" below for that pass's findings and disclosed simplifications.

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

### Strategy 12–15: Modern Popular Static Portfolios (added in a follow-up "modern, popular, effective strategies" deep-research pass)

A dedicated research pass specifically looked for strategies that are genuinely modern (still actively discussed today, not just historically notable), popular (real retail/practitioner followings, not obscure), and effective (backed by a sourced, real performance statistic) — deliberately excluding anything mechanically similar to Strategies 1-11 above. Every claim below cites a source found during that pass; where a headline number's provenance was weaker (promotional material vs. an independently-computed backtest), that's stated explicitly rather than smoothed over. All four are implemented via one shared `StaticAllocationStrategy` engine (`rs/strategy.py`) — fixed weights, no momentum, no trend gate, periodically rebalanced back to the same targets — since a single, well-tested mechanism correctly serves all four rather than four near-identical bespoke classes.

* **Permanent Portfolio** (Harry Browne, 1980s; still actively covered today — dividendes.ch, April 2026; optimizedportfolio.com's "(2026)"-dated guide): 25% each broad US stocks / long-term Treasuries / cash / gold, annual rebalance. Sourced 10-year performance (lazyportfolioetf.com): Sharpe ≈0.59, max drawdown ≈30.6% (inflation-adjusted) — meaningfully milder than broad equities, consistent with its stability-over-growth design, not a return-maximizing one.
* **Golden Butterfly** (Tyler / Portfolio Charts; actively covered — optimizedportfolio.com "(2026)"; bestfolio.app): 20% each total-market stocks / small-cap / long bonds / short bonds / gold — adds a small-cap tilt and splits fixed income by duration versus Permanent Portfolio. Sourced performance (portfoliocharts.com, portfoliodb.com): CAGR ≈8.1-8.3%, max drawdown ≈18-20%, Sharpe ≈0.47 — roughly 93% of the S&P 500's CAGR at about a third of its drawdown.
* **All Weather / "All Seasons"** (the specific 30/40/15/7.5/7.5 stocks/long bonds/intermediate bonds/gold/commodities breakdown traces to Tony Robbins' *Money: Master the Game*, an interview with Ray Dalio — not a Bridgewater publication; Portfolio Charts uses the identical allocation as "All Seasons"): a **fixed-weight approximation** of risk parity, not genuine risk-budgeted risk parity (which would risk-budget each sleeve to equal volatility contribution, typically requiring bond-sleeve leverage this version skips entirely). Cited performance (Robbins' own promotional material: profitable 86% of years 1984-2013) is weaker-sourced than Portfolio Charts' own backtest and is disclosed as such.
* **HFEA — "Hedgefundie's Excellent Adventure"** (Bogleheads forum, 2019; the "Part II" continuation thread has run 250+ pages through 2024-2026 — sustained, active community following): 55% UPRO (3x daily S&P 500) / 45% TMF (3x daily 20+yr Treasuries), quarterly rebalance. Sourced performance (optimizedportfolio.com aggregating PortfolioVisualizer-style backtests): ≈24.6% CAGR vs. SPY's ≈14.8% since May 2009 — a window dominated by a falling-rate, positive-correlation regime that favors the strategy's core bet — alongside a ≈70-71% max drawdown bottoming in late 2023, when 2022's rising-rate shock broke the strategy's central stock-bond-decorrelation assumption and forced TMF's 2022 1-for-10 reverse split. **This project's SyntheticDataProvider does not simulate genuine 3x daily-reset leverage or volatility decay** — running HFEA against synthetic data (this project's default, per its offline testing policy below) exercises only the rebalancing mechanics, not the real strategy's leverage/correlation-breakdown risk; use `--data-provider yfinance` against real UPRO/TMF data to see genuine behavior.

### Strategy 16–17: Modern Systematic TAA Extensions (same research pass)

* **Protective Asset Allocation — PAA** (Wouter J. Keller & Jan Willem Keuning, 2016, SSRN #2759734): a direct successor to Strategy 5 (VAA-G4) by the same authors, still actively tracked as a live strategy on AllocateSmartly. Scores an 11-asset risky universe (`SPY, QQQ, IWM, EFA, EEM, VNQ, DBC, GLD, HYG, LQD, TLT` — this project's own consolidation of the original paper's 12-asset universe, folding VGK+EWJ into the existing EFA holding) by a smoothed absolute-momentum signal, then sends a **continuously-scaled** (not binary) fraction of capital to a protection asset (`IEF`) based on the breadth of assets in positive momentum; the remainder splits equally across the top 6 highest-momentum assets by rank regardless of individual sign. **This project could not independently verify the paper's exact bond-fraction formula constants against the primary SSRN source this session** — the implementation captures the documented breadth-based mechanism with a disclosed, reconstructed formula (see `ProtectiveAssetAllocation`'s docstring in `rs/strategy.py`), not a verified reproduction of the paper's precise numbers.
* **Adaptive Asset Allocation — AAA** (Butler, Philbrick, Gordillo & Varadi, 2012, SSRN #2328254, "Adaptive Asset Allocation: A Primer"; GestaltU/ReSolve Asset Management, which still actively references this framework today): the one strategy in this project requiring genuinely new portfolio-construction machinery — a two-stage momentum-filter-then-minimum-variance-optimization pipeline (`scipy.optimize.minimize`, SLSQP, long-only). Ranks an 8-asset universe (this project's disclosed simplification of the original 10-asset universe — EZU+EWJ folded into EFA, the RWX international-REIT sleeve dropped for lack of a proxy elsewhere in this project) by 6-month momentum, keeps the top half, then solves for minimum-variance weights using a covariance matrix built from 126-day correlation combined with a more-responsive 20-day volatility (the paper's own "hybrid" construction). Cited performance ranges widely by backtest vintage (16.9% CAGR / Sharpe 2.15 since 1989 in the original primer vs. ≈12.1-14.8%/yr in secondary aggregators over different windows) — these are disclosed as non-cross-verified figures from different sources, not one consistent track record.

### What was researched but NOT implemented in this pass

* **Generalized Protective Momentum (GPM)**, a documented PAA successor (Keller/Keuning, tracked on AllocateSmartly) that replaces PAA's raw momentum ranking with a composite score penalizing assets correlated to the rest of the universe. Excluded because this pass could not independently confirm GPM's exact SSRN citation/year (the closest verified hit was a related-but-different 2015 Keller/Butler/Kipnis paper) — per this project's own evidentiary standard, an unconfirmed citation isn't published as a hard reference. A future pass that locates and reads the primary source could add it as a straightforward extension of `ProtectiveAssetAllocation`.

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
    },
    "factors": ["absolute_momentum_trend", "relative_momentum", "volatility_targeting"]
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
    },
    "factors": ["mean_reversion", "absolute_momentum_trend"]
  }
}
```

---

## 2b. Factor Tagging & `factor_summary.json` (the `strategy_generator` hand-off)

Every entry in `strategies_config.json` carries an optional `"factors"` list, tagging which
quantitative factor category (or categories) that strategy conditions on. The tag vocabulary is
shared workspace-wide via `common/factor_taxonomy.py`'s `FACTOR_CATEGORIES` — the SAME vocabulary
`common/allocation_templates.py`'s templates use for their own `factor_tags` field, so a tag means
the same thing in both projects:

| Tag | Meaning |
|---|---|
| `absolute_momentum_trend` | An asset's own trailing trend/return sign (SMA gate, ROC > 0 gate) |
| `relative_momentum` | Cross-sectional ranking of assets by trailing return |
| `volatility_targeting` | Realized volatility used to size/scale exposure |
| `mean_reversion` | Short-term oscillator (RSI) signaling overbought/oversold reversal |
| `breadth` | Aggregate count/fraction of a basket in positive momentum, as a market-wide risk-on/off signal |
| `correlation_diversification` | Covariance/correlation structure used for portfolio construction |
| `regime_trend_strength` | Trend-strength/regime classification (ADX, Hurst) gating trend-following |
| `static_fixed_weight` | Fixed weights with no adaptive signal |

`load_strategies_config()` warns (doesn't raise) on an unrecognized tag, matching this project's
existing "warn on unknown key" convention.

After every `run_research_strategy.py` run, `results/factor_summary.json` aggregates each ran
strategy's backtest performance (Sharpe/CAGR/max drawdown/Calmar) by these tags — e.g. "every
strategy tagged `breadth` averaged Sharpe X on this run." This is a real, consumable artifact:
`strategy_generator`'s `--factor-report` flag loads it and uses it to (conservatively) tie-break
its own template selection when the primary backtested-Sharpe signal is ambiguous — see
`strategy_generator/README.md` for the full mechanism and why it's deliberately bounded to
tie-breaking only.

**Read the `caveat` field in `factor_summary.json` before trusting anything in it.** On this
project's default `--data-provider synthetic`, the underlying GBM data has no real
momentum/mean-reversion/volatility-clustering structure by construction — a factor "winning" on a
synthetic run reflects mechanism/plumbing, not a validated edge. Re-run with
`--data-provider yfinance` against real prices for a factor comparison that actually means
something. Ad-hoc `--description`/`--description-file` runs have no config entry and are omitted
from the summary rather than force-tagged.

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

This project shares a single `uv`-managed environment with the rest of the
workspace. From the repo root (one level up), run `uv sync` once, then:

### Running Unit Tests
```powershell
uv run pytest research_strategy/tests -v
```

### Running CLI Backtests
Simulate portfolio backtests on synthetic multi-asset data across all strategies loaded from JSON config:
```powershell
uv run python research_strategy/run_research_strategy.py --strategy all
```

Pass a custom JSON configuration file:
```powershell
uv run python research_strategy/run_research_strategy.py --config custom_config.json --strategy all
```

Evaluate custom plain English strategies via CLI text:
```powershell
uv run python research_strategy/run_research_strategy.py --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility."
```

### Viewing Terminal Dashboard
Launch the terminal dashboard to view side-by-side performance summaries and recent target allocations:
```powershell
uv run python research_strategy/dashboard.py
```

---

## 6. Key Metrics Reported

* **Sharpe Ratio**: Annualized return per unit of total risk ($R_p / \sigma_p$).
* **CAGR**: Compound Annual Growth Rate over the simulated period.
* **Max Drawdown**: Maximum peak-to-trough decline.
* **Calmar Ratio**: Annualized return divided by maximum drawdown ($\text{CAGR} / |\text{Max DD}|$).
* **Win Rate & Profit Factor**: Percentage of positive daily return periods and ratio of gross gains to gross losses.
* **Turnover & Rebalances**: Accumulated portfolio rebalance turnover and rebalance count.

Outputs land in `results/`: `research_strategy_report.json` (per-strategy metrics), one
`<strategy>_weights.csv` per strategy, and `factor_summary.json` (per-factor-tag aggregated
performance across the run — see "Factor Tagging" above).

[ English | [简体中文](README_ZH.md) ]

# Researched Quantitative Trading Strategies (`research_strategy`)

A dedicated side project implementing and evaluating twenty-one quantitative trading strategies: five tactical asset allocation (TAA) strategies synthesized from academic literature and practitioner research (*Journal of Finance*, *Journal of Portfolio Management*, SSRN, AllocateSmartly), four single-asset timing strategies consolidated into this project from this workspace's former standalone `rsi_strategy`, `swing_trend_strategy`, `grid_trading`, and `ensemble_strategy` side projects, two Donchian channel breakout systems, four modern, actively-followed static/fixed-weight portfolios popular with retail and practitioner communities today (Permanent Portfolio, Golden Butterfly, All Weather, HFEA), two modern systematic TAA extensions added in a follow-up deep-research pass on "modern, popular, effective" strategies (Protective Asset Allocation, Adaptive Asset Allocation) -- see "Strategy 12-17" below for that pass's findings and disclosed simplifications -- and three original, from-scratch structural readings of 缠中说禅 ("Chan theory") price structure, the second and third each an additive extension of the first (see "Strategy 18-19, 21").

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

### Strategy 18: Chan Pivot Shift (original structural reading, not ported from any external library)

* **Chan Pivot Shift** (`ChanPivotShiftStrategy`, `chan_pivot_shift`): a from-scratch reading of 缠中说禅 ("Chan theory") price structure, written natively for this project in `rs/chan_structure.py` — it does **not** import, port, or reuse any code, formula, or default from the third-party `czsc` Rust/Python library (prior art on the same theory this workspace is aware of but does not depend on). Pipeline: collapse K-line inclusion relationships into merged bars, detect top/bottom fractals (3-bar extrema), alternate them into strokes (enforcing a minimum-bar independence gap between a stroke's fractals), then group ≥3 consecutive overlapping strokes into pivots (consolidation ranges). Trades a **pivot shift**: goes long once a new pivot's band steps wholly above the prior one's and a confirming pullback low forms; exits on a symmetric downward shift, a simple stroke-over-stroke momentum-divergence proxy (not `czsc`'s SNR/rsq), a stop-loss, or a max-holding-days safety net. This is an original interpretation of the theory's "trend = pivots stepping up/down" idea, not a reproduction of any formal 买卖点 (buy/sell point) taxonomy, any published paper, or any other codebase's algorithm — see `rs/chan_structure.py`'s docstrings for the specific simplifications disclosed at each stage.

### Strategy 19: Chan Pivot Shift (MACD) (additive copy of Strategy 18 using real MACD divergence)

* **Chan Pivot Shift (MACD)** (`ChanPivotShiftMACDStrategy`, `chan_pivot_shift_macd`): a near-literal copy of Strategy 18 above, not a modification of it — `ChanPivotShiftStrategy`/`chan_structure.py` are left exactly as they are. Keeps the exact same stroke-based pivot-band-shift buy/sell rule (deliberately NOT rebuilt on segments, unlike Strategy 21 below), but replaces the disclosed stroke-slope/length "momentum divergence proxy" with real MACD-histogram-area divergence (背驰, via `common.indicators.macd`). One deliberate difference beyond the proxy swap: made **symmetric** — a top-divergence sell (a new high on weaker MACD momentum) AND a bottom-divergence buy (a new low on weaker MACD momentum) — where the original proxy only ever produced a sell signal, checked only on up-strokes. See `rs/chan_signals.py`'s `compute_chan_pivot_macd_signals` for the implementation.

### Strategy 20: Compounder Margin of Safety (price-only proxy of a conservative value-investing framework)

* **Compounder Margin of Safety** (`CompounderMarginOfSafetyStrategy`, `compounder_margin_of_safety`): adapts a conservative value-investing community's valuation framework (see `docs/snowball_strategy.txt` at the repo root) — the original method only holds durable, moat-protected, high-ROE, dividend-paying compounders whose expected 5-year return clears a risk premium over a broad-index benchmark (~12% normal hurdle vs. the index's own ~6-8%), and sells the moment that edge decays away. **DISCLOSED SIMPLIFICATION:** this workspace has no dividend/ROE/earnings/valuation data anywhere (OHLCV price history only, verified via grep across every provider in `common/data.py`), so this is a **price-only proxy**: a long-term uptrend + contained-volatility "stability" gate stands in for the moat/high-ROE quality screen, and a trailing annualized-return proxy (momentum persistence, not a real forecast) stands in for the document's earnings-growth-driven expected return; the sell rule is a direct translation (exit once that trailing-return proxy decays below the benchmark's own trailing return). Candidate universe defaults to an illustrative, unverified blue-chip basket (KO, PG, JNJ, MSFT, COST, WMT, MCD, PEP) rather than this project's usual broad-ETF universe, since "moat"/"quality" are single-company traits. A **real-fundamentals version** (actual ROE/dividend yield/earnings growth from yfinance) lives in the separate `fundamental_screener` project instead, since it needs real network data and can't be offline/synthetic-tested like everything here.

### Strategy 21: Chan Three-Type Buy/Sell Points (additive extension of Strategy 18)

* **Chan Three-Type Buy/Sell Points** (`ChanThreeTypeStrategy`, `chan_three_type`): an ADDITIVE extension of Strategy 18 above, not a modification of it — `ChanPivotShiftStrategy`/`chan_structure.py` are left exactly as they are, and this strategy coexists alongside it as its own independent reading of 缠中说禅 ("Chan theory"), one level closer to the formal published taxonomy. Adds two structural layers on top of `chan_structure.py`'s strokes: segments (线段, a disclosed price-only proxy for the real characteristic-sequence termination rule) and segment-level pivots (built by reusing `chan_structure.build_pivots` verbatim on segments instead of strokes). Replaces the Strategy 18 divergence proxy with real MACD-histogram-area divergence (背驰, via `common.indicators.macd`, previously unused by any strategy in this project) and implements the formal first/second/third-type buy/sell point taxonomy (一/二/三类买卖点): a first-type point is a pivot breakdown/breakout confirmed by MACD divergence between the entering and leaving move; a second-type point is a failed follow-through after a first-type point (a retest that doesn't make a new extreme); a third-type point is a breakout retest that holds the pivot's own band edge, with no divergence check. See `rs/chan_signals.py` for the full disclosed simplifications at each new stage.

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

`rs/strategy.py`'s `instantiate_strategy_from_config_entry()` — the single source of truth for
building a strategy instance from an entry, reused directly by `strategy_generator` and
`backtester` — validates each entry and raises a clear `ValueError` (naming the entry key) rather
than silently misbehaving: a `"natural_language"` entry with a missing or blank (including
whitespace-only) `"plain_english_description"` is rejected instead of silently falling through to
a generic default strategy, a `"class"` entry with a missing/empty `"class_name"` is rejected, and
an `entry_data` that isn't a dict (e.g. `None`, a list) is rejected up front instead of raising a
confusing `AttributeError` deep inside the function.

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
│   ├── chan_structure.py      # Independent Chan-theory structure detector (fractals/strokes/pivots)
│   ├── chan_signals.py        # Additive extension: segments, real MACD divergence, 一/二/三类买卖点
│   ├── timing_aspects.py      # Entry x exit/risk aspect decomposition for single-asset timing templates
│   └── strategy.py            # NaturalLanguageStrategy engine + strategy implementations
├── strategies_config.json     # Central JSON configuration for all strategies & parameters
├── run_research_strategy.py   # CLI runner loading strategy configs dynamically
├── dashboard.py               # Terminal ASCII report viewer
├── tests/
│   ├── test_nl_parser.py      # Offline unit tests for the plain-English parser
│   ├── test_chan_structure.py # Offline unit tests for the Chan structure detector
│   ├── test_chan_signals.py   # Offline unit tests for segments/MACD divergence/三类买卖点
│   ├── test_timing_aspects.py # Offline unit tests for entry x exit aspect composition
│   └── test_strategy.py       # Offline unit tests for all strategies & config loading
└── README.md                  # Strategy formulations, citations, and guide
```

---

## 5. Usage Guide

This project shares a single `uv`-managed environment with the rest of the
`pipeline/` group. From `pipeline/` (one level up), run `uv sync` once, then:

### Running Unit Tests
```powershell
uv run pytest research_strategy/tests -v
```

### Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the other 3 projects — see `common/README.md`'s
cross-reference index; `resolve_universe_from_args` picks the first one supplied, in that order,
falling back to this project's own 18-symbol `DEFAULT_UNIVERSE_SYMBOLS` (SPY, QQQ, IWM, EFA, EEM,
GLD, TLT, VNQ, AGG, TIP, IEF, LQD, DBC, BIL, SCZ, HYG, UPRO, TMF) if none are given.

| Flag | Type / default | Meaning |
|---|---|---|
| `--universe` / `-u` | space-separated tickers, default: none | Explicit ticker list (falls back to `DEFAULT_UNIVERSE_SYMBOLS`) |
| `--universe-file` | path, default: none | Load tickers from a file instead |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--strategy` | str, default `"all"` | Which `strategies_config.json` entry to run (its key), or `"all"` to run every configured strategy |
| `--config` | path, default: none | Alternate JSON config file (same schema as `strategies_config.json`, §2) instead of the built-in one |
| `--dump-strategies` | flag, default off | Dump every strategy in the loaded config as its own backtester-compatible `<key>_strategy.json` under `results/strategy_dumps/` (usable directly with `backtester/run_backtest.py --strategy-file` or `pipeline/live_signal`), then exit without loading any universe/market data or running a backtest |
| `--description` | str, default: none | One-off plain-English strategy text, parsed via `rs/nl_parser.py` instead of reading `strategies_config.json`/`--config` |
| `--description-file` | path, default: none | Same as `--description`, but read the text from a file |
| `--n-days` | int, default `1200` | Number of synthetic bars to generate (only used with `--data-provider synthetic`) |
| `--seed` | int, default `42` | Random seed for synthetic data generation (only used with `--data-provider synthetic`) |
| `--top-n` | int, default `5` | Number of top-ranked strategies (by Sharpe ratio, CAGR tie-break) written to `top_strategies_summary.json` (§7) |
| `--data-provider` | str, default `"synthetic"` | `synthetic` (default — see §3's offline policy), `yfinance`, `csv`, or a custom registered/module-specifier provider |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data (only relevant to non-synthetic providers) |
| `--cache-ttl-days` | float, default: none | Maximum age (in days) of a cached OHLCV file before it's treated as stale and re-fetched; `None` (default) never expires a cache entry on age alone |

Note: unlike the other 3 projects, this CLI's `--data-provider` **default is `synthetic`**, per
§3's offline-testing policy. Real market data is fully supported (the CLI's own low-Sharpe warning
literally suggests `--data-provider yfinance` to cross-check a synthetic finding) — it is simply
not the default, and `--n-days`/`--seed` are silently ignored once you opt into a real provider.

`DATA_DIR` now resolves to the shared, workspace-wide OHLCV cache directory
(`<repo_root>/data/`) rather than a project-local folder — see `common/README.md`'s
"Shared OHLCV cache directory" section (§7) for details.

### Running CLI Backtests
Simulate portfolio backtests on synthetic multi-asset data across all strategies loaded from JSON config:
```powershell
uv run python research_strategy/run_research_strategy.py --strategy all
```

Run a single named strategy instead of all of them:
```powershell
uv run python research_strategy/run_research_strategy.py --strategy dual_momentum
```

Pass a custom JSON configuration file:
```powershell
uv run python research_strategy/run_research_strategy.py --config custom_config.json --strategy all
```

Dump every configured strategy as its own backtester-ready `strategy.json` (no market data needed):
```powershell
uv run python research_strategy/run_research_strategy.py --dump-strategies
```

Evaluate custom plain English strategies via CLI text:
```powershell
uv run python research_strategy/run_research_strategy.py --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility."
```

Same, but reading the description from a file:
```powershell
uv run python research_strategy/run_research_strategy.py --description-file my_strategy.txt
```

Control the synthetic data itself (more bars, different seed):
```powershell
uv run python research_strategy/run_research_strategy.py --strategy all --n-days 2500 --seed 7
```

#### Real market data

```powershell
# Cross-check a synthetic finding against real prices for the default universe, all strategies
uv run python research_strategy/run_research_strategy.py --strategy all --data-provider yfinance --no-cache

# A specific strategy, an explicit real-ticker universe
uv run python research_strategy/run_research_strategy.py --strategy dual_momentum \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT --data-provider yfinance

# Universe loaded from a file (e.g. a basket produced by instrument_selection), real data
uv run python research_strategy/run_research_strategy.py --strategy all \
  --universe-file instrument_selection/results/basket.json --data-provider yfinance

# Plain-English description evaluated against real data instead of synthetic
uv run python research_strategy/run_research_strategy.py \
  --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility." \
  --data-provider yfinance

# CSV-folder provider (offline real data you already downloaded)
uv run python research_strategy/run_research_strategy.py --strategy all \
  --universe SPY QQQ TLT GLD --data-provider csv --data-dir /path/to/ohlcv_csvs
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
`<strategy>_weights.csv` per strategy, `factor_summary.json` (per-factor-tag aggregated
performance across the run — see "Factor Tagging" above), and `top_strategies_summary.json` (the
top `--top-n` strategies from this run, ranked by Sharpe ratio with CAGR as a tie-break — see §7).
Unlike `factor_summary.json`, this ranks individual strategies against each other rather than
aggregating by factor tag, and it isn't currently consumed by any other project — it's a
human-facing leaderboard, also echoed to the console at the end of the run.

## 7. Data Shapes & Schemas

This project consumes the shared **OHLCV DataFrame**, **universe dict**, **target weights
DataFrame**, and **portfolio backtest result dict** shapes documented in `../../common/README.md`
(§1–4) — see that file first. `factor_summary.json`'s schema is documented in full in section 2b
above, not repeated here. Everything below is unique to this project.

### `results/research_strategy_report.json`

A JSON object keyed by strategy key (e.g. `"dual_momentum"`, or `"custom_plain_english"` for an
ad-hoc `--description` run). Each entry:

| Field | Type | Notes |
|---|---|---|
| `strategy_name` | str | From the parsed spec, or the class name for a class-based strategy with no `spec` attribute |
| `raw_description` | str | The plain-English text (natural-language strategies) or the class's own docstring |
| `parsed_summary` | str | `explain_weights()`'s full text |
| `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio`, `win_rate` | float | From the shared backtest result dict (`../../common/README.md` §4) |
| `profit_factor` | float or `null` | `null` when not finite (e.g. no losing days) |
| `total_turnover` | float | |
| `total_rebalances` | int | |

### `results/top_strategies_summary.json`

Ranks the strategies actually evaluated in this run by backtested Sharpe ratio (CAGR as a
tie-break) — see §5/§6. Distinct from `factor_summary.json` (§2b), which aggregates by factor
TAG rather than ranking individual strategies:

| Field | Type | Notes |
|---|---|---|
| `run_context` | object | Same shape as `factor_summary.json`'s `run_context` (§2b) |
| `ranking_metric` | str | Always `"sharpe_ratio (cagr tie-break)"` |
| `n_strategies_evaluated` | int | Total strategies with a valid backtest result this run (before truncating to `--top-n`) |
| `top_strategies` | array | Up to `--top-n` entries, `rank` 1 = highest Sharpe (see below) |
| `caveat` | str | Same synthetic-data-caveat convention as `factor_summary.json` — **read this before trusting the ranking** |

Each `top_strategies[i]` entry:

| Field | Type | Notes |
|---|---|---|
| `rank` | int | 1-indexed |
| `strategy_key` | str | The `strategies_config.json` key (or `"custom_plain_english"`) |
| `strategy_name` | str | Prefers `strategies_config.json`'s own `"name"`; falls back to `research_strategy_report.json`'s `strategy_name` for an ad-hoc `--description` run with no config entry |
| `description` | str | Prefers `strategies_config.json`'s `"description"`/`"plain_english_description"`; same fallback as above |
| `factor_tags` | array of str | This strategy's own tags from `strategies_config.json` (empty for an ad-hoc run) |
| `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio`, `win_rate` | float | Same values as `research_strategy_report.json` |
| `profit_factor` | float or `null` | `null` when not finite |
| `total_turnover` | float | |
| `total_rebalances` | int | |

### `results/<strategy>_weights.csv`

The DENSE (forward-filled) form of the target weights DataFrame (`../../common/README.md` §3) —
`target_weights.ffill().fillna(0.0)` — one column per universe symbol, one row per trading day.

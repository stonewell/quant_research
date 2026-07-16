# Automated Strategy Generator (Universe-Wide, Data-Driven)

A tool that GENERATES a concrete, parameterized trading strategy for a whole
UNIVERSE of instruments at once, pooling their historical data — not a
separate strategy per symbol, and not a fixed, manually-designed strategy
applied everywhere. It classifies the universe's statistical regime
(trending / mean-reverting / random-walk-like) by pooling every symbol's own
evidence, routes the whole universe to a matching single-symbol strategy
template, searches that template's small parameter space by POOLED
performance across every instrument, sanity-checks the result against a
random-search baseline, and validates the whole pipeline with proper
walk-forward testing.

**Not single-instrument-only.** Alongside that single-symbol search, the
generator also searches a pairs-trading candidate across every pair in the
universe (distance/rolling-z-score pairs trading, `stratgen/pairs_search.py`)
and returns whichever of the two candidate FAMILIES is better-supported by
the evidence — a trusted (ERS-passed, enough trades) candidate beats an
untrusted one regardless of raw score; between two trusted (or two
untrusted) candidates, the higher-scoring one wins. This removes an earlier
version's architecture limit, where a pairs-trading strategy (inherently a
bet on the relationship between two instruments, not on either one's own
trend/mean-reversion character) could only ever be invoked manually — never
something the generator itself could discover and output. Both candidates'
full detail are always returned (`GeneratedStrategySpec.single_symbol_result`
/ `.pairs_result`), regardless of which one wins, so the runner-up is never
silently discarded. See "Additional sub-3-month templates" below for the
research grounding, and `stratgen/pairs_search.py`'s own docstring for how
the same anti-overfitting defenses (Equivalent Random Search, tracked trial
counts) are applied to the pairs search specifically — searching every pair
in an N-symbol universe is itself a combinatorial multiple-comparisons
problem, and is treated with exactly the same skepticism as everything else
in this project.

**Why pool across the universe instead of generating one strategy per
symbol** (this was a deliberate revision from an earlier per-symbol design):
running an independent generator on each of N instruments and reporting
whichever one back-tested best is itself an uncorrected multiple-comparisons
problem — effectively N trials, without the correction that implies, at
exactly the level the Deflated Sharpe Ratio exists to catch (see below).
Selecting parameters by how well they generalize across many different
instruments simultaneously is a materially stronger anti-overfitting
property than selecting by best fit to one instrument's history — at the
honest cost of producing one strategy that may not be the single best fit
for any individual name (see "Known limitations" for the flip side of that
tradeoff, and note that this hasn't been validated against real data to
confirm the tradeoff is worth it in practice — it's a sound methodological
argument, not an empirically demonstrated improvement).

**Testing note, stated up front per this project's original instruction: this
project was validated with synthetic data only — the 52-test suite exercises
every module's mechanics and statistical properties, but the pipeline was NOT
run end-to-end against real market data this session.** `run_strategygen.py`
and `stratgen/data.py` are complete and usable against real data (via
yfinance) whenever you're ready to run them yourself; they just weren't
exercised here.

## Why this design, and what the research changed about it

Research surfaced one heavily-replicated, sobering result that shaped
everything else: **Allen & Karjalainen's classic genetic-algorithm study
(1999, Journal of Financial Economics) — a careful, methodologically rigorous
search with a built-in validation step — still largely failed to beat
buy-and-hold net of realistic transaction costs, with negative average excess
returns in 9 of 10 out-of-sample test periods.** That result, from one of the
most careful automated-strategy-discovery studies ever published, is the
reason this project does NOT implement full genetic programming over an
open-ended function set. Instead, every design choice below is a documented
mitigation against the same overfitting risk that sank that approach.

### 1. Regime-conditioned template routing (not free-form rule search)

Each symbol in the universe is independently classified via the **Hurst
exponent** — trending (persistent, H significantly > 0.5), mean-reverting
(anti-persistent, H significantly < 0.5), or random-walk-like (statistically
indistinguishable from noise). This routing logic itself is documented in
Chang, Lizardi & Shah (2022, arXiv:2205.11122).

**Pooling across the universe** (`regime.aggregate_regime`): each symbol's
Hurst is standardized into a z-score against a null calibrated to *that
symbol's own* window length (so instruments with different history lengths
stay comparable), and the whole universe's regime is decided by the **median**
z-score across all symbols — not a vote on already-discretized per-symbol
labels (which throws away how strongly each symbol leans), and not the mean
(which one outlier instrument, e.g. a commodity ETF with an unusually strong
idiosyncratic trend, could single-handedly flip). If the universe's regime
call is "no trade," the generator returns a spec with empty parameters for
the whole universe immediately, before running any parameter search at all.

**The critical methodological detail** (Noppakaew et al., 2025, *Asia
Pacific Journal of Mathematics*): the R/S Hurst estimator has a well-known
finite-sample bias — it does NOT center on exactly 0.5 for a true random
walk at practical sample sizes. Using a naive "H > 0.5 = trending" cutoff is
therefore itself a source of false positives. `regime.calibrate_null_distribution()`
instead simulates hundreds of pure random walks **of the same window length**
and computes this module's own Hurst estimator on each, giving the
estimator's true mean/std under "no memory at all" for that specific data
length — then classifies against that calibrated null, not a textbook
constant. A dedicated test (`test_calibrate_null_distribution_centers_above_naive_half_for_short_windows`)
confirms this bias is real and the calibration corrects for it.

The significance threshold (`k`, in standard deviations from the calibrated
null) is a deliberate design choice, not the cited paper's own number: the
source study used k=0.5 for a descriptive three-way *tertile* split of trend
strength, which is far too permissive for gating a *binary* decision to
deploy an entirely different strategy template — at k=0.5, pure noise gets
misclassified roughly a third of the time by chance alone. This project
defaults to **k=1.5** (~13% two-sided false-positive rate) instead, documented
in `regime.classify_regime`'s docstring, with a dedicated test verifying the
empirical false-positive rate on synthetic noise stays in a sane range
(the test asserting a single fixed seed "never" misfires would itself have
been statistically fragile — see the git history if curious why that test
was rewritten).

### 2. A small, constrained parameter search — not genetic programming

The field's own documented mitigation for the overfitting risk of flexible
rule search is to restrict the primitive/parameter set to a small number of
long-established, simple constructs rather than an arbitrary function set
(Allen & Karjalainen, explicitly citing data-snooping as the risk). Each
template here (`templates.py`) exposes exactly **2 free parameters**:

- `MomentumTemplate`: fast/slow SMA crossover (state-based: long whenever
  fast > slow), 3×3 = 9-combination grid.
- `MeanReversionTemplate`: RSI entry/exit thresholds (Connors-style, same
  convention as this workspace's `rsi_strategy` project), 3×3 = 9-combination grid.
- `NoTradeTemplate`: no parameters, no trades — the deliberate "don't force
  an edge that isn't there" option for random-walk-like instruments.

Stop-loss distance is auto-scaled to each instrument's own ATR% (not a fixed
percentage), per the research's emphasis on volatility-relative rather than
universal risk parameters — and is a template-level constant, not searched,
to keep the effective number of trials small and auditable.

**Pooled selection**: each of the 9 candidate parameter combinations is
backtested INDEPENDENTLY on every symbol in the universe, and the combination
selected is the one with the best POOLED (median, by default; `mean` is also
available via `GeneratorConfig.aggregation`) Sharpe ratio across all of them
— not the one that best fits any single instrument. `GeneratedStrategySpec.per_symbol_sharpe`
and `.per_symbol_num_trades` expose the per-instrument breakdown for the
*winning* parameters, specifically so you can see how consistent the
"universal" choice actually is (a strategy that pools well because it's
mediocre-everywhere looks very different from one that's strong on most
names and pooled-down by one laggard, and the summary number alone can't
tell you which).

### 3. Equivalent Random Search (ERS) — a mandatory sanity pretest

Concrete, quantifiable pretest (Chen & Navet, ICONIP 2006): before trusting
any search result, compare it against a size-matched pool of **randomly
generated** candidate parameterizations evaluated on the *same* (pooled,
universe-wide) objective. Beating that pool is necessary but explicitly **not
sufficient** for concluding a generated strategy is genuinely good — it only
clears a minimum bar (default: the grid-search winner must beat the 90th
percentile of 200 random candidates, each itself scored by its own pooled
performance across the universe). Failing this check is treated as a hard
signal that the search machinery found nothing better than chance would have.

### 4. Walk-forward validation — the standard, concretely-specified defense

A three-way chronological split, repeated across rolling folds, applied to
the whole universe at once (every symbol must share the same trading
calendar/number of bars — `run_walkforward` validates this and raises a
clear error otherwise, rather than silently misaligning dates):

- **Train window** (default 4 years): classify the universe's pooled regime
  and search candidate parameterizations, each scored by pooled performance
  across every symbol.
- **Validation window** (default 2 years), immediately after: SELECT the
  winning candidate by *pooled validation* performance, not training
  performance — this is what actually defends against overfitting; picking
  the training-set winner is just curve-fitting with an extra step.
- **Embargo gap** (default 30 days), then **test window** (default 1 year,
  also the fold step size): a genuinely untouched holdout, read only once
  per fold (pooled across the universe), after selection.

The **generalization ratio** (mean test-window performance ÷ mean
validation-window performance) measures how much the strategy degrades
out-of-sample — near 1.0 means it generalizes; near 0 (or negative) means
the in-sample edge was mostly noise. A live sanity run during development
(synthetic AR(1)-trending data, 9 folds) showed a generalization ratio of
~0.33 with substantial fold-to-fold noise — a realistic, honest illustration
of exactly the degradation this methodology exists to detect, not a
guarantee the tool will always find something that "works."

### The Deflated Sharpe Ratio — accounting for the search itself

The single best-corroborated statistical safeguard found for an automated
**generation** pipeline specifically (Bailey & Lopez de Prado, 2014, *J.
Portfolio Management*): it corrects an observed Sharpe ratio for (a) having
picked the best of N independent trials — modeling the expected maximum
Sharpe achievable by pure luck across that many trials — and (b) non-Normal
returns (skewness/kurtosis). This matters here specifically because a
generator that tries several templates × parameter grids × random-search
candidates (now evaluated by pooled performance across a whole universe) is
exactly the kind of multi-trial search process DSR was built to correct for;
report DSR against the *actual* number of
trials your generator ran (`walkforward.run_walkforward()` tracks and uses
this automatically), not a raw single-split Sharpe ratio. The implementation
was validated against the formula's own internal consistency properties
(DSR=0.5 exactly when observed Sharpe equals the expected-by-luck maximum;
reduces to the classic Probabilistic Sharpe Ratio at N=1 trial; strictly
decreasing as trials increase) rather than a secondhand numerical example
that couldn't be verified against the primary source in this session.

## What did NOT survive research verification

- A claim that this exact regime-routed strategy outperformed buy-and-hold
  specifically in high-Hurst regimes (tested across five East Asian indices)
  was explicitly refuted (0-3 vote) during adversarial fact-checking. The
  routing *methodology* is well-documented; a claim that it's *profitable*
  is not — treat this tool as a way to generate a hypothesis worth testing
  on your own data, not a verified source of edge.
- A specific minimum-trade-count threshold (n≈50) and a walk-forward
  window/step/embargo scheme (4yr/2yr/1yr/30-day) both come from a single,
  very recent (2026), modest-outlet study with no independent replication —
  used here as reasonable, disclosed defaults (`GeneratorConfig.min_trades_for_trust`,
  `WalkForwardConfig`'s window years), not field consensus. Recalibrate for
  your own instrument's trade frequency and history length.

## Additional sub-3-month templates (added after a follow-up deep-research pass)

A later research pass specifically looked for MORE templates suited to
holding periods under ~3 months with sourced evidence of beating buy-and-hold
on drawdown as well as return — deliberately excluding this project's
already-implemented approaches (momentum crossover, RSI mean-reversion, this
workspace's separate grid-trading and regime-switching-ensemble projects).
Every claim below was adversarially verified 3-way (≥2/3 votes to refute
kills a claim) before being trusted; where a headline number didn't survive
that check, that's stated explicitly rather than omitted. **Not validated
against real market data this session, same as the rest of this project** —
synthetic-data tests only (see `tests/test_templates.py`,
`tests/test_backtester.py`, `tests/test_pairs.py`).

### `TurnOfMonthTemplate` — calendar effect
Buy near month-end, sell a few trading days into the next month
(`entry_days_before_month_end`, `exit_trading_day_of_month`: 2 free params).
**Confidence: high** — multiply-corroborated across independent academic
sources (Lakonishok & Smidt 1988; McConnell & Xu; Carchano & Tornero) across
30+ country equity indexes, not a single-source claim. A cited illustrative
backtest (1926–2005) reported 7.2% annualized return at 6.9% volatility
(Sharpe 1.04, max drawdown −20.79%) while invested only ~4 of ~20 trading
days/month. Honest weak point (disclosed by the source itself, not found by
this project's own testing): no accepted risk-based explanation exists, only
unproven cash-flow/rebalancing hypotheses, and calendar effects are
documented to weaken or drift to different days over time.

### `VolGatedMomentumTemplate` — conditional volatility-targeting, simplified
Trend-following (fixed 100-day SMA filter), de-risked out of the market when
realized volatility spikes above its own trailing-252-day percentile
(`vol_lookback`, `vol_percentile`: 2 free params). **Confidence: high** for
the underlying mechanism — Bongaerts, Kang & van Dijk (2020, *Financial
Analysts Journal*, peer-reviewed) found *conditional* (extreme-state-only)
volatility targeting cuts average max drawdown ~6.6 percentage points across
equity markets, and 54.1%→20.1% for momentum-factor portfolios specifically,
while *continuous* (always-on) vol-scaling underperformed in 4 of 10 markets
tested. **Disclosed simplification**: this workspace's backtester is
single-position binary exposure (0%/100%), not continuous position sizing,
so this template implements the paper's overlay as a hard entry-block/
forced-exit gate rather than continuous scaling — it captures the
drawdown-reducing mechanism (de-risk during vol spikes) but not the paper's
more nuanced sizing, and the paper's low-vol-state exposure *increase* has no
long-only-unlevered analogue here and isn't attempted.

### `stratgen/pairs.py` + `stratgen/pairs_backtester.py` + `stratgen/pairs_search.py` — distance/rolling-z-score pairs trading
Gatev, Goetzmann & Rouwenhorst (2006, *Review of Financial Studies* — the
seminal, peer-reviewed "distance method" pairs-trading study). **Confidence:
high** for the historical result — 1963–2002, the top-20-pairs portfolio
delivered ~11% annualized excess return (~2x the S&P 500's) at 1/2–1/3 the
volatility, a Sharpe ratio 4–6x the market's, with a materially smoother
equity curve (worst monthly loss 8.2% vs. much rougher market swings) that
performed especially well during the real 1969–1980 bear market. Several
disclosed, deliberate departures from the original design, all driven by
this project's constraints:

1. **Not a `Template` subclass — but IS a first-class generator output.**
   Every single-symbol template here signals off one instrument's own OHLCV,
   routed by that instrument's own Hurst regime, and executes through a
   single-position long-only backtester; pairs trading is inherently a
   two-instrument, market-neutral long-short strategy, so forcing it into
   that same interface would misrepresent the mechanism the evidence is
   about. Instead, `pairs_search.search_pairs_candidates` runs as an
   independent candidate family inside `StrategyGenerator.generate()`
   (see the top of this README and `generator.py`'s docstring) — searched
   across every pair in the universe, evaluated with its own Equivalent
   Random Search check, and compared against the single-symbol winner on
   equal (trust-gated) footing. This is the change that removed the
   generator's original single-instrument-per-symbol architecture limit.
2. **Rolling window instead of GGR's discrete formation/trading blocks.**
   The original design re-picks pairs and re-estimates the spread's std at
   the boundary of non-overlapping 12-month formation / 6-month trading
   blocks; this module uses a single continuously-rolling lookback for both
   instead — simpler and directly walk-forward-safe, and how most
   practitioner implementations of the same idea are actually built, but a
   real simplification worth naming.
3. **Hard `max_holding_days` cap (default 63 trading days ≈ 3 months).**
   GGR's own reported average holding period for an open position was
   3.75–4 months — they explicitly call it "medium-term" — which exceeds
   this project's <3-month target. The cap forces an exit the original
   design didn't have; `test_pairs_backtest_max_holding_days_forces_exit_
   when_spread_never_converges` verifies it actually fires when the spread
   never reverts on its own.
4. **Inherently long-short** (short the "rich" leg, long the "cheap" leg) —
   cannot be made long-only without destroying the market-neutral mechanism
   the evidence is about, unlike every other template in this project.
5. **No margin/borrow-cost modeling.** Short-sale proceeds are credited to
   cash immediately and the liability is marked to market each bar (the
   standard simplified convention), but real short selling also incurs a
   stock-borrow fee and margin requirements this doesn't model.
6. Documented decay, same caveat pattern as every other finding in this
   workspace: post-1988, the top-20 portfolio's raw monthly excess return
   fell from 118bp to ~38bp as the strategy became more widely known/
   competed away — today's edge is likely thinner than the 40-year average.

### What did NOT make the cut from this follow-up pass, and why
- **Cointegration-based ETF pairs trading** (Chen & Alexiou 2025): mechanics
  confirmed, but the "better Sharpe AND lower drawdown than buy-and-hold"
  framing was refuted on adversarial check — the paper's own baseline result
  is Sharpe 0.28–0.45 with near-zero total return (0.8–1.0% over 24 years)
  and up to an 11-year drawdown-recovery time; tiny drawdown mostly because
  it barely trades, not because it's a good strategy.
- **Donchian/Turtle breakout**: rule mechanics confirmed, but the only
  evidence found is a single 6-month backtest on one Chinese commodity
  futures contract (7 trades) — far too thin to say anything about
  equities/ETFs, and a cited (unverified, second-hand) regime note says it
  has "no obvious advantage" in bull markets.
- **Post-Earnings-Announcement Drift (PEAD)**: well-documented academically
  but decaying (spread fell from ~5% in the 1980s/90s to ≤3% by the late
  2010s), concentrated in small-caps, no Sharpe/drawdown-vs-buy-and-hold
  evidence found anywhere, a 3-month hold sits right at this project's
  ceiling, and it's fundamentally cross-sectional (rank many stocks at once)
  rather than single-symbol — a poor fit for every backtester in this
  workspace.
- **Low-volatility factor**: Quantpedia's own indicative backtest shows a
  −45.9% max drawdown — not clearly better than buy-and-hold on the axis
  this pass was actually looking for.
- **VIX term-structure strategies / Opening Range Breakout**: excluded by
  this project's own equities/long-only/daily-bar constraints — VIX
  strategies need futures/ETNs and one source explicitly says they carry
  large, hard-to-manage drawdowns (not lower); ORB needs intraday data and
  shorting, and the only backtest found used a 3x-leveraged ETF with no
  buy-and-hold comparison at all.

## Project layout

Shared code (the yfinance loader, standard indicators, the base Hurst
estimator, and standard performance metrics) lives one level up in
`../common/` and is used by every project in this workspace. Each module
here re-exports the shared functions it needs and keeps only project-specific
logic local — `regime.py`'s Monte-Carlo calibration and universe-pooling
methodology in particular is unique to this project and unaffected; only its
base `hurst_exponent` math is now shared.

```
strategy_generator/
  stratgen/
    indicators.py     rsi/sma/atr/atr_pct/realized_vol re-exported from ../common/indicators.py (the deliberately small primitive set)
    regime.py          hurst_exponent re-exported from ../common/hurst.py; Monte-Carlo calibration + universe-wide median-pooled classification (local, unique to this project)
    templates.py       MomentumTemplate, MeanReversionTemplate, NoTradeTemplate, TurnOfMonthTemplate, VolGatedMomentumTemplate (2 free params each)
    backtester.py      Template-agnostic event loop: next-bar-open entries, intrabar ATR-stop, mark-to-market (single-instrument, long-only)
    pairs.py            Distance/rolling-z-score pairs-trading signals (two-instrument, long-short -- not a Template subclass, see above)
    pairs_backtester.py  Dollar-neutral long-short execution matching pairs.py's signals
    pairs_search.py       Pairs-candidate search across every pair in a universe + its own Equivalent Random Search check -- the pairs analogue of the single-symbol grid search below
    metrics.py          summarize() (local) + base metrics + Deflated Sharpe Ratio re-exported from ../common/metrics.py
    generator.py        Universe-pooled regime routing + constrained grid search + ERS for single-symbol templates, PLUS the pairs-candidate search, compared and reconciled into one GeneratedStrategySpec
    walkforward.py       Three-way split (train/validation/test) across rolling folds, applied to the pooled universe (single-symbol templates only -- see Known limitations)
    data.py               Thin wrapper over ../common/data.py (present, usable, not exercised this session)
  run_strategygen.py       CLI: "generate" (fast, single-window) or "walkforward" (full validation) for the WHOLE universe
  tests/                    pytest, synthetic data only -- includes test_pairs.py and test_pairs_search.py for the new pairs capability
  data/, results/           gitignored
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r ../requirements.txt
```

## Usage (against real data, when you're ready)

```bash
# Fast: generate ONE strategy for the whole universe from all available history
python run_strategygen.py --universe SPY QQQ AAPL --mode generate

# Slower, honest: full walk-forward validation with generalization ratio + DSR
python run_strategygen.py --universe SPY QQQ AAPL --mode walkforward --start 2010-01-01
```

Key options (see `python run_strategygen.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--mode` | `generate` (one-shot, all history) or `walkforward` (proper rolling validation) |
| `--aggregation` | `median` (default; resists one outlier instrument) or `mean`, for pooling per-symbol Sharpe ratios |
| `--n-random-search` | Size of the Equivalent Random Search pool, for BOTH the single-symbol and pairs candidate searches (default 200) |
| `--ers-percentile-threshold` | How far above the random pool a candidate must rank to be trusted (default 0.90) |
| `--min-trades-for-trust` | Minimum trade count (total across the universe for single-symbol; round-trips for the winning pair) before a result is trusted (default 10) |
| `--no-search-pairs` | Disable the pairs-trading candidate search -- single-symbol templates only, restoring the pre-pairs behavior |
| `--max-pairs-to-search` | Cap on distinct pairs backtested for large universes; C(N,2) grows quadratically (default 50) |
| `--pairs-max-holding-days` | Hard cap forcing pairs trades to close under this many trading days (default 63 ≈ 3 months) |
| `--train-years` / `--validation-years` / `--test-years` / `--embargo-days` | Walk-forward window sizes (single-symbol templates only -- see Known limitations) |

`--mode walkforward` inner-joins every symbol's dates before running (see
`_align_universe` in `run_strategygen.py`) since fold boundaries are
bar-position-based and require a shared trading calendar across the universe.

## Testing

```bash
python -m pytest tests/ -v
```

74 tests, synthetic data only (per this project's instruction), covering:
indicator correctness; the Hurst estimator's known finite-sample bias and
its Monte-Carlo calibration; universe-wide regime pooling (median resists a
single outlier instrument, a universe of pure noise routes to no-trade, an
empty universe degrades gracefully); each template's signal logic matching a
manual calculation; the backtester's no-lookahead execution, stop-loss
triggering (via a deterministic constructed spike, not a data-dependent race
with the signal-based exit), and cash-never-negative invariant; the Deflated
Sharpe Ratio's internal mathematical consistency (reduces to the classic PSR
at N=1, exactly 0.5 at the luck threshold, monotonically decreasing in trial
count); end-to-end universe-pooled regime routing to the correct template
on strongly-trending/mean-reverting/random-walk synthetic universes,
including that a universal strategy still exposes a per-symbol performance
breakdown; the walk-forward harness's fold geometry (chronological
ordering, no overlap, correct step size), its hard rejection of a
misaligned (differently-lengthed) universe, and a full end-to-end run
producing a valid generalization ratio and DSR; the pairs-trading module
(rolling z-score matching a manual calculation, correct long/short direction
assignment and P&L sign on a deterministic divergence-then-convergence
price series, the hard `max_holding_days` cap actually firing when a spread
never converges, equity never crossing zero); and the pairs-candidate
search's integration into `generate()` (it finds a deliberately-cointegrated
pair among uncorrelated filler symbols, respects `max_pairs_to_search`,
reports honest ERS/trust fields, and does NOT win over a trusted
single-symbol candidate when the pairs candidate itself isn't trusted).

A real bug the test suite caught during this revision, worth noting for
anyone extending the synthetic-data helpers: the original additive
random-walk price construction (`100 + cumsum(...)`) could wander negative
over long/volatile paths (confirmed: phi=0.75, seed=10 went to -16.7),
producing `log(negative)` `RuntimeWarning`s and silently-corrupted log
returns. Fixed by switching to a geometric (multiplicative) random walk,
which is standard practice for synthetic price simulation and inherently
guarantees positive prices — the test suite now runs warning-free even
under `-W error::RuntimeWarning`.

## Known limitations

- Not validated against real market data in this session (see above) —
  synthetic AR(1)/random-walk processes are a reasonable mechanism test but
  are not real market microstructure, regime persistence, or event risk.
- The parameter search is a small grid, not genetic programming or Bayesian
  optimization — deliberate, per the research above, but it does mean the
  generator can only find what's inside each template's 9-combination grid.
- **The flip side of universe-pooling**: a strategy selected for the best
  pooled performance across many instruments may not be a strong fit for
  ANY single one of them — pooling reduces the risk of overfitting to one
  instrument's idiosyncrasies, but it can also produce a "compromise"
  strategy that's mediocre everywhere. `per_symbol_sharpe`/`per_symbol_num_trades`
  are exposed specifically so you can check this rather than trust the
  pooled number blindly; a highly heterogeneous universe (e.g., mixing
  equities, bonds, and commodities) is more likely to produce this failure
  mode than a universe of similar instruments (e.g., US sector ETFs).
- Pooling across the universe does NOT eliminate multiple-testing risk, it
  relocates it: the DSR correction here accounts for the parameter-grid and
  random-search trials within one generation run, but not for having tried
  this on several different candidate universes and reported the best one
  (an analogous, still-open concern research flagged for the per-instrument
  design this replaced).
- Walk-forward window defaults (4yr/2yr/1yr/30-day embargo) were calibrated
  in the source literature for liquid, long-history S&P 500 stocks; shorter-
  history instruments will need smaller windows (all configurable) or won't
  produce any folds at all (`generate_folds` returns an empty list rather
  than guessing).
- Walk-forward requires every symbol in the universe to share the same
  number of bars; instruments with materially different listing histories
  need to be aligned (e.g., inner join on dates, as the CLI does) or dropped
  before calling `run_walkforward` directly.
- **The pairs-candidate search is only wired into `generate()`'s one-shot
  path, not into `walkforward.py`.** `run_walkforward` still only searches
  single-symbol templates per fold — a pairs strategy discovered by
  `--mode generate` has NOT been walk-forward-validated the way a
  single-symbol template's DSR/generalization-ratio numbers have. Treat any
  `strategy_family="pairs"` result from `--mode generate` as a one-shot,
  in-sample-search-only hypothesis until walk-forward support for pairs is
  added.
- **Searching every pair in an N-symbol universe is itself a combinatorial
  multiple-comparisons problem**, on top of the ones this project already
  corrects for at the parameter level. `pairs_search.py` applies its own
  Equivalent Random Search check and reports its actual trial count
  (pairs × param grid + the random pool), but the SAME caveat as everywhere
  else in this project applies: beating that pool is necessary, not
  sufficient, for concluding a discovered pair is genuinely tradeable rather
  than the best-looking one of many by chance. `max_pairs_to_search` caps
  runtime on large universes by sampling a subset of pairs rather than
  covering all of C(N,2) — `n_pairs_searched`/`n_pairs_total` on the result
  make a capped run visible rather than silently partial.
- The 2-way choice between the single-symbol winner and the pairs winner is
  itself a (small) additional selection decision, not folded into either
  candidate's own reported `n_trials`/DSR — negligible next to the
  within-family trial counts, but worth naming rather than ignoring.

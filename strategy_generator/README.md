# Automated Strategy Generator (Universe-Wide, Data-Driven)

A tool that GENERATES a concrete, parameterized trading strategy for a whole
UNIVERSE of instruments at once, pooling their historical data — not a
separate strategy per symbol, and not a fixed, manually-designed strategy
applied everywhere. It classifies the universe's statistical regime
(trending / mean-reverting / random-walk-like) by pooling every symbol's own
evidence, routes the whole universe to a matching strategy template,
searches that template's small parameter space by POOLED performance across
every instrument, sanity-checks the result against a random-search baseline,
and validates the whole pipeline with proper walk-forward testing.

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
    indicators.py     rsi/sma/atr/atr_pct re-exported from ../common/indicators.py (the deliberately small primitive set)
    regime.py          hurst_exponent re-exported from ../common/hurst.py; Monte-Carlo calibration + universe-wide median-pooled classification (local, unique to this project)
    templates.py       MomentumTemplate, MeanReversionTemplate, NoTradeTemplate (2 free params each)
    backtester.py      Template-agnostic event loop: next-bar-open entries, intrabar ATR-stop, mark-to-market
    metrics.py          summarize() (local) + base metrics + Deflated Sharpe Ratio re-exported from ../common/metrics.py
    generator.py        Universe-pooled regime routing + constrained grid search + Equivalent Random Search check
    walkforward.py       Three-way split (train/validation/test) across rolling folds, applied to the pooled universe
    data.py               Thin wrapper over ../common/data.py (present, usable, not exercised this session)
  run_strategygen.py       CLI: "generate" (fast, single-window) or "walkforward" (full validation) for the WHOLE universe
  tests/                    52 pytest tests, synthetic data only
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
| `--n-random-search` | Size of the Equivalent Random Search pool (default 200) |
| `--ers-percentile-threshold` | How far above the random pool the search winner must rank to be trusted (default 0.90) |
| `--min-trades-for-trust` | Minimum TOTAL trade count across the universe before a result is trusted (default 10) |
| `--train-years` / `--validation-years` / `--test-years` / `--embargo-days` | Walk-forward window sizes |

`--mode walkforward` inner-joins every symbol's dates before running (see
`_align_universe` in `run_strategygen.py`) since fold boundaries are
bar-position-based and require a shared trading calendar across the universe.

## Testing

```bash
python -m pytest tests/ -v
```

52 tests, synthetic data only (per this project's instruction), covering:
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
breakdown; and the walk-forward harness's fold geometry (chronological
ordering, no overlap, correct step size), its hard rejection of a
misaligned (differently-lengthed) universe, and a full end-to-end run
producing a valid generalization ratio and DSR.

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

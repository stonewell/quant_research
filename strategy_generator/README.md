# Automated Strategy Generator (Per-Instrument, Data-Driven)

A tool that GENERATES a concrete, parameterized trading strategy for an
instrument directly from its own historical price data — rather than a
fixed, manually-designed strategy applied everywhere. It classifies the
instrument's statistical regime (trending / mean-reverting / random-walk-like),
routes it to a matching strategy template, searches that template's small
parameter space, sanity-checks the result against a random-search baseline,
and validates the whole pipeline with proper walk-forward testing.

**Testing note, stated up front per this round's explicit instruction: this
project was validated with synthetic data only — the 41-test suite exercises
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

Each instrument is classified via the **Hurst exponent** — trending
(persistent, H significantly > 0.5), mean-reverting (anti-persistent, H
significantly < 0.5), or random-walk-like (statistically indistinguishable
from noise, in which case the generator deliberately produces a "no trade"
strategy rather than forcing a template to fit). This routing logic itself
is documented in Chang, Lizardi & Shah (2022, arXiv:2205.11122).

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

Stop-loss distance is auto-scaled to the instrument's own ATR% (not a fixed
percentage), per the research's emphasis on volatility-relative rather than
universal risk parameters — and is a template-level constant, not searched,
to keep the effective number of trials small and auditable.

### 3. Equivalent Random Search (ERS) — a mandatory sanity pretest

Concrete, quantifiable pretest (Chen & Navet, ICONIP 2006): before trusting
any search result, compare it against a size-matched pool of **randomly
generated** candidate parameterizations evaluated on the *same* data. Beating
that pool is necessary but explicitly **not sufficient** for concluding a
generated strategy is genuinely good — it only clears a minimum bar (default:
the grid-search winner must beat the 90th percentile of 200 random
candidates). Failing this check is treated as a hard signal that the search
machinery found nothing better than chance would have.

### 4. Walk-forward validation — the standard, concretely-specified defense

A three-way chronological split, repeated across rolling folds:

- **Train window** (default 4 years): fit/search candidate parameterizations.
- **Validation window** (default 2 years), immediately after: SELECT the
  winning candidate by *validation* performance, not training performance —
  this is what actually defends against overfitting; picking the training-set
  winner is just curve-fitting with an extra step.
- **Embargo gap** (default 30 days), then **test window** (default 1 year,
  also the fold step size): a genuinely untouched holdout, read only once
  per fold, after selection.

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
per-instrument generator that tries several templates × parameter grids ×
random-search candidates is exactly the kind of multi-trial search process
DSR was built to correct for; report DSR against the *actual* number of
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

```
strategy_generator/
  stratgen/
    indicators.py     RSI (Wilder), SMA, ATR/ATR% -- the deliberately small primitive set
    regime.py          Hurst exponent (R/S) + Monte-Carlo-calibrated regime classification
    templates.py       MomentumTemplate, MeanReversionTemplate, NoTradeTemplate (2 free params each)
    backtester.py      Template-agnostic event loop: next-bar-open entries, intrabar ATR-stop, mark-to-market
    metrics.py          Standard metrics + Deflated Sharpe Ratio (Bailey & Lopez de Prado)
    generator.py        Regime routing + constrained grid search + Equivalent Random Search check
    walkforward.py       Three-way split (train/validation/test) across rolling folds, generalization ratio, DSR
    data.py               yfinance loader (present, usable, not exercised this session)
  run_strategygen.py       CLI: "generate" (fast, single-window) or "walkforward" (full validation) per instrument
  tests/                    41 pytest tests, synthetic data only
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
# Fast: generate a strategy spec per instrument from all available history
python run_strategygen.py --universe SPY QQQ AAPL --mode generate

# Slower, honest: full walk-forward validation with generalization ratio + DSR
python run_strategygen.py --universe SPY QQQ AAPL --mode walkforward --start 2010-01-01
```

Key options (see `python run_strategygen.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--mode` | `generate` (one-shot, all history) or `walkforward` (proper rolling validation) |
| `--n-random-search` | Size of the Equivalent Random Search pool (default 200) |
| `--ers-percentile-threshold` | How far above the random pool the search winner must rank to be trusted (default 0.90) |
| `--min-trades-for-trust` | Minimum trade count before a result is trusted (default 10 — see the honesty note above) |
| `--train-years` / `--validation-years` / `--test-years` / `--embargo-days` | Walk-forward window sizes |

## Testing

```bash
python -m pytest tests/ -v
```

41 tests, synthetic data only (per this round's instruction), covering:
indicator correctness; the Hurst estimator's known finite-sample bias and
its Monte-Carlo calibration; regime classification boundaries and empirical
false-positive rate on pure noise; each template's signal logic matching a
manual calculation; the backtester's no-lookahead execution, stop-loss
triggering (via a deterministic constructed spike, not a data-dependent
race with the signal-based exit), and cash-never-negative invariant; the
Deflated Sharpe Ratio's internal mathematical consistency (reduces to the
classic PSR at N=1, exactly 0.5 at the luck threshold, monotonically
decreasing in trial count); end-to-end regime routing to the correct
template on strongly-trending/mean-reverting/random-walk synthetic series;
and the walk-forward harness's fold geometry (chronological ordering, no
overlap, correct step size) plus a full end-to-end run producing a valid
generalization ratio and DSR.

## Known limitations

- Not validated against real market data in this session (see above) —
  synthetic AR(1)/random-walk processes are a reasonable mechanism test but
  are not real market microstructure, regime persistence, or event risk.
- The parameter search is a small grid, not genetic programming or Bayesian
  optimization — deliberate, per the research above, but it does mean the
  generator can only find what's inside each template's 9-combination grid.
- Regime classification and the parameter search both happen per-instrument,
  independently. Research flagged (as an open question, not something with
  an established answer) that running this across a large universe is
  itself a multiple-comparisons problem across instruments, not just across
  parameter trials within one instrument — the DSR correction here accounts
  for trials within a fold/instrument, not for having screened many
  instruments and reporting only the best one.
- Walk-forward window defaults (4yr/2yr/1yr/30-day embargo) were calibrated
  in the source literature for liquid, long-history S&P 500 stocks; shorter-
  history instruments will need smaller windows (all configurable) or won't
  produce any folds at all (`generate_folds` returns an empty list rather
  than guessing).

# Automated Portfolio Strategy Generator (Basket Asset Allocation)

A tool that GENERATES a concrete, parameterized asset allocation trading strategy for a whole
BASKET of instruments simultaneously (e.g. exported from `instrument_selection`).

**This README was rewritten to match the current code.** An earlier revision of this project
had a single-symbol/pairs-trading/Hurst-regime-routed/walk-forward architecture; that code
(`stratgen/templates.py`, `backtester.py`, `portfolio_backtester.py`, `pairs*.py`,
`walkforward.py`, and their tests) was deleted in a later commit and replaced with the
architecture described below. If you find a reference anywhere else in this workspace to those
deleted modules, it's stale — this file is the accurate one.

## What it actually does today

It searches across 9 portfolio allocation templates — all defined in the SHARED
`../common/allocation_templates.py` (also used directly by `backtester` and indirectly by
`research_strategy`), each grounded in peer-reviewed quantitative finance literature:

1. `EqualWeightAllocation` (1/N naive baseline)
2. `InverseVolatilityAllocation` (1/vol risk parity)
3. `CrossSectionalMomentumAllocation` (Jegadeesh & Titman 1993; Moskowitz et al. 2012)
4. `HierarchicalRiskParityAllocation` (Marcos López de Prado 2016, *Journal of Portfolio Management*)
5. `DualMomentumAllocation` (Gary Antonacci 2014, *JPM* / Faber 2007)
6. `MaxDiversificationAllocation` (Choueifaty & Coignard 2008, *JPM*)
7. `MeanReversionAllocation` (Connors-style short-term RSI mean-reversion, cross-sectional)
8. `MinimumVarianceAllocation` (Markowitz 1952 — genuine constrained quadratic optimization, distinct from HRP's heuristic recursive-bisection substitute for one)
9. `BreadthGatedMomentumAllocation` (generalizes `research_strategy`'s `ProtectiveAssetAllocation`/Keller & Keuning 2016 breadth-based crash-protection mechanism to an arbitrary basket)

Templates 7-9 were added in a follow-up pass that inventoried the quantitative factors used
across `research_strategy`'s 17 strategies and ported the three factor categories (mean-reversion,
genuine minimum-variance, and market-breadth) that were proven valuable there but entirely absent
from this project's searchable template set. See each class's docstring in
`common/allocation_templates.py` for its full citation and disclosed caveats.

`stratgen/generator.py` grid-searches every template's small parameter set (`param_grid`),
scores each combination with the shared portfolio backtester (`common/allocation_backtester.py`,
which accounts for daily weight drift, partial cash holdings, and rebalancing transaction costs),
and picks the highest-Sharpe combination across ALL templates. The Equivalent Random Search (ERS)
check then validates that this winner beats a size-matched pool of random allocation portfolios —
necessary, not sufficient, evidence the result isn't just noise.

**Every template operates cross-sectionally on the whole universe already** — this is not a
per-symbol independent search; `HierarchicalRiskParityAllocation` and `MinimumVarianceAllocation`
both consume the full covariance matrix, `CrossSectionalMomentumAllocation`/`DualMomentumAllocation`/
`MeanReversionAllocation`/`BreadthGatedMomentumAllocation` rank/gate across the whole basket, and
`MaxDiversificationAllocation` uses average pairwise correlation. There is no separate
per-instrument search path to reconcile against a portfolio-level one.

## Optional: consuming a `research_strategy` factor report

`--factor-report <path>` accepts a `factor_summary.json` produced by
`research_strategy/run_research_strategy.py` (aggregated backtest performance grouped by the same
factor-category tags each template in `common/allocation_templates.py` declares via its
`factor_tags` field — see `common/factor_taxonomy.py` for the shared vocabulary). This is a real
data hand-off between the two projects, not just parallel, independently-designed template
libraries — but it is deliberately bounded:

- The grid search's own per-template best-Sharpe selection is **unchanged**.
- The factor report is used ONLY to break a tie when two or more templates' backtested Sharpe
  ratios land within `--factor-tiebreak-epsilon` (default 5%) of each other — i.e. the primary,
  ERS-validated signal is already ambiguous. A factor score can **never** override a clearly
  better-performing template.
- The output (`GeneratedStrategySpec.factor_context`/`.factor_tiebreak_used`, also written into
  `strategy.json`) always shows every considered template's factor score and whether the tie-break
  actually fired, regardless of whether it changed anything.

**Why so conservative:** `research_strategy`'s default data provider is synthetic GBM, which has
no real momentum/mean-reversion/volatility-clustering structure by construction — a factor
"winning" on that data reflects mechanism/plumbing, not a validated edge (this caveat is embedded
directly in `factor_summary.json`'s own `caveat` field). Letting a synthetic-run factor score
freely override real backtested performance here would be dishonest; tie-breaking on a genuinely
ambiguous primary signal is a defensible, bounded use, and becomes more informative once
`research_strategy` is run against real data (`--data-provider yfinance`).

```bash
# 1. Generate a factor summary from research_strategy (synthetic data, its default)
uv run python research_strategy/run_research_strategy.py --strategy all

# 2. Feed it into strategy_generator
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ TLT GLD \
  --mode generate --factor-report research_strategy/results/factor_summary.json
```

## Why this design (still accurate, unchanged by the architecture rewrite)

Research surfaced one heavily-replicated, sobering result that shaped everything here: **Allen &
Karjalainen's classic genetic-algorithm study (1999, Journal of Financial Economics)** — a careful,
methodologically rigorous search with a built-in validation step — still largely failed to beat
buy-and-hold net of realistic transaction costs, with negative average excess returns in 9 of 10
out-of-sample test periods. That result is why this project does NOT implement free-form genetic
programming over an open-ended function set. Instead:

### A small, constrained parameter search — not genetic programming

Each template exposes only 2-4 free parameters (see each class in `common/allocation_templates.py`)
rather than an arbitrary rule space, per the field's own documented mitigation against data-snooping
(Allen & Karjalainen). `stratgen/generator.py`'s `grid_combinations()` does a plain
`itertools.product` over each template's grid — no per-template special-casing, so a new template
class needs zero changes to `generator.py` beyond being added to `common/allocation_templates.py`'s
`ALLOCATION_TEMPLATES` list.

### Equivalent Random Search (ERS) — a mandatory sanity pretest

Concrete, quantifiable pretest (Chen & Navet, ICONIP 2006): before trusting any search result,
compare it against a size-matched pool of **randomly generated** candidate weight allocations,
evaluated on the same portfolio backtest. Beating that pool is necessary but explicitly **not
sufficient** for concluding a generated strategy is genuinely good (default: the grid-search winner
must beat the 90th percentile of 200 random candidates). Failing this check is a hard signal the
search found nothing better than chance.

## Known, disclosed gaps (not silently missing — deliberately out of scope for the current pass)

- **`stratgen/regime.py`'s Hurst-based regime classification (trending/mean-reverting/random-walk,
  with proper finite-sample-bias calibration against a simulated null) still exists and is still
  unit-tested in isolation (`tests/test_regime.py`), but `generator.py` does not call it.** It is
  dead code from the live generation pipeline's perspective — a leftover from the deleted
  single-symbol architecture. Reviving it (e.g. as a pre-filter routing which templates are even
  eligible for a given basket's regime) was explicitly considered during the factor-hand-off pass
  and deliberately deferred as separate, larger architectural work, not silently dropped.
- **Walk-forward validation and the Deflated Sharpe Ratio are currently unimplemented for this
  architecture.** `--mode` only supports `generate` (a single-window search); the three-way
  train/validation/test fold methodology and DSR correction that the deleted `walkforward.py`
  implemented for the old single-symbol templates were not ported to the allocation-template
  architecture. `common/metrics.py`'s `deflated_sharpe_ratio` is still available and correct, just
  not wired into this project's CLI.
- **Pairs trading was removed, not just disabled.** The earlier distance/rolling-z-score
  pairs-trading candidate search (`pairs.py`/`pairs_backtester.py`/`pairs_search.py`) no longer
  exists anywhere in this project.

## Project layout

```
strategy_generator/
  stratgen/
    generator.py       Grid search across common/allocation_templates.py's 9 templates, scored via
                        common/allocation_backtester.py, + Equivalent Random Search + the optional
                        factor-report tie-break (_apply_factor_tiebreak)
    regime.py           Hurst-based regime classification -- still present, unit-tested, but
                        DISCONNECTED from generator.py (see "Known, disclosed gaps" above)
    indicators.py       Thin re-exports from ../common/indicators.py
    metrics.py          Thin re-exports from ../common/metrics.py (including deflated_sharpe_ratio,
                        currently unused by this project's own CLI -- see above)
    data.py             Thin wrapper over ../common/data.py
  run_strategygen.py     CLI: "generate" mode (--factor-report is optional)
  tests/                 pytest, synthetic data only
  data/, results/        gitignored
```

The actual template implementations, and the shared `factor_tags`/factor-taxonomy machinery, live
in `../common/allocation_templates.py` and `../common/factor_taxonomy.py` — this project imports
and searches them, it doesn't define them.

## Setup

This project shares a single `uv`-managed environment with the rest of the workspace. From the
repo root (one level up):

```bash
uv sync
```

## Usage

```bash
# Generate ONE strategy for the whole universe from all available history (run from the repo root)
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate

# With a research_strategy factor report as an optional tie-break input
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate \
  --factor-report research_strategy/results/factor_summary.json

# Offline/synthetic data only (no network calls) -- this workspace's standing testing convention
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate \
  --data-provider synthetic
```

Key options (see `uv run python strategy_generator/run_strategygen.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--mode` | Only `generate` is currently supported (see "Known, disclosed gaps") |
| `--n-random-search` | Size of the Equivalent Random Search pool (default 200) |
| `--ers-percentile-threshold` | How far above the random pool a candidate must rank to be trusted (default 0.90) |
| `--min-rebalances-for-trust` | Minimum rebalance count before a result is trusted (default 4) |
| `--factor-report` | Optional path to a `research_strategy` `factor_summary.json` (see above) |
| `--factor-tiebreak-epsilon` | How close two templates' Sharpe ratios must be before `--factor-report` can break the tie (default 0.05) |
| `--data-provider` | `yfinance` (default), `csv`, `synthetic`, or a custom module specifier |
| `--data-dir` | Folder path for the CSV data provider |
| `--no-cache` | Disable local CSV caching of fetched data |

## Testing

```bash
# from the repo root
uv run pytest strategy_generator/tests -v
```

Synthetic data only, covering: each of the 9 templates' signal logic and edge cases (mirrored in
`common/allocation_templates.py`'s own docstrings), the grid search + ERS mechanism, the
`_apply_factor_tiebreak` tie-break logic (fires only within epsilon, never overrides a clear
winner, no-op when no report is supplied — a dedicated regression test pins that omitting
`--factor-report` reproduces byte-for-byte the same winner as before this feature existed), and
`--factor-report` file validation (a malformed file raises a clear, named error rather than a raw
`KeyError` surfacing deep inside `generator.py`).

## Known limitations

- Not validated against real market data this session — synthetic data only, per this workspace's
  standing testing convention.
- The parameter search is a small grid, not genetic programming or Bayesian optimization —
  deliberate, per the research above, but it does mean the generator can only find what's inside
  each template's own grid.
- The factor-report hand-off's tie-break-only design (see above) means it will rarely change the
  outcome in practice when one template is genuinely, clearly better on a given basket — this is
  intentional, not a bug; a factor report that "won" on synthetic data is not evidence strong
  enough to override a real backtested Sharpe difference.
- See `research_strategy/README.md` for the full caveat on why a `factor_summary.json` computed
  on synthetic data reflects mechanism, not a validated factor edge.

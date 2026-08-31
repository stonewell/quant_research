[ English | [简体中文](README_ZH.md) ]

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
`../../common/allocation_templates.py` (also used directly by `backtester` and indirectly by
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
and picks the highest-Sharpe combination across ALL templates — plus, by default, hybrid
recombinations of DIFFERENT templates' own aspects (see "Optional: aspect composition" below;
`--no-compose-aspects` to disable). The Equivalent Random Search (ERS) check then validates that
this winner beats a size-matched pool of random allocation portfolios — necessary, not sufficient,
evidence the result isn't just noise.

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

## Optional: consuming a `pattern_mining` report (`--pattern-report`)

Turning-point indicator pattern mining is its own pipeline stage now — see `pattern_mining/README.md`
for the full methodology (zigzag turning-point detection, the indicator menu, the Bonferroni-corrected
shuffle-null significance test, and its disclosed lag/hindsight caveats). Run it once
(`pattern_mining/run_pattern_mining.py`) to produce a durable `pattern_mining/results/pattern_report.json`,
then pass that report here with `--pattern-report`: its significant findings are turned into
`PatternBasedAllocationTemplate` (`common/allocation_templates.py`) candidates (up to
`--pattern-max-templates`), folded into the SAME grid-search + Equivalent Random Search pipeline as
every static template via the generic `extra_templates` mechanism — they must still clear the same
ERS bar to be trusted; the mining significance test alone is never sufficient.

```bash
# 1. Mine patterns once (its own stage, durable output)
uv run python pattern_mining/run_pattern_mining.py --universe SPY QQQ TLT GLD --data-provider synthetic

# 2. Feed the report into strategy_generator (can be reused across multiple runs)
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ TLT GLD \
  --mode generate --data-provider synthetic \
  --pattern-report pattern_mining/results/pattern_report.json
```

## Optional: including `research_strategy` strategies as candidates (`--research-strategy`)

`--research-strategy KEY [KEY ...]` includes one or more of `research_strategy`'s 20 strategy
implementations as additional candidate templates, alongside the 9 static allocation templates and
any `--pattern-report` findings. Each `KEY` is one of `research_strategy/strategies_config.json`'s
short keys (e.g. `baa_keller`, `adaptive_grid`, `rsi_mean_reversion` — see `research_strategy/README.md`
for the full list of 20); run
`PYTHONPATH=.. uv run python -c "from research_strategy.rs.config import load_strategies_config; print(sorted(load_strategies_config().keys()))"`
from inside `pipeline/` to see them directly (the bare form without `PYTHONPATH=..` fails with
`ModuleNotFoundError: No module named 'common'` — unlike every `run_*.py` entry point, this
one-liner bypasses the repo-root `sys.path` injection those scripts do themselves; on PowerShell use
`$env:PYTHONPATH=".."; uv run python -c "..."` instead). Each named strategy is instantiated exactly as
`research_strategy`'s own CLI would build it — including any `strategies_config.json` parameter
overrides — via `research_strategy.rs.strategy.instantiate_strategy_from_config_entry`, then folded
into the SAME grid-search + Equivalent Random Search pipeline as every static template via the
generic `extra_templates` mechanism (same plumbing `--pattern-report` uses). An unrecognized key
raises a clear error listing every valid key rather than failing deep inside the search.

```bash
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ TLT GLD --mode generate \
  --data-provider synthetic --research-strategy baa_keller adaptive_grid \
  --factor-report research_strategy/results/factor_summary.json
```

## Optional: aspect composition — hybrid templates across DIFFERENT sources (`--no-compose-aspects`)

**ON by default.** Each of the 9 static templates fuses two orthogonal decisions into one
`generate_weights` — SELECTION (which symbols are eligible) + WEIGHTING (how the invested fraction
splits across them) for basket templates; ENTRY SIGNAL + EXIT/RISK/SIZING for the 4 decomposable
single-asset timing templates (`RSIMeanReversionStrategy`, `SwingTrendPullbackStrategy`,
`ChanPivotShiftStrategy`, `TurtleBreakoutStrategy`, only reachable via `--research-strategy`).
Aspect composition decomposes each side into a standalone, independently composable piece
(`common/strategy_aspects.py` for basket templates' `SelectionAspect`/`WeightingAspect`;
`research_strategy/rs/timing_aspects.py` for timing templates' `EntrySignalAspect`/`ExitRiskAspect`)
and searches HYBRID pairings across different source templates too — e.g. momentum's stock-picking
paired with inverse-volatility's position sizing, a combination that doesn't exist as any single
static template. A winning hybrid is folded into the SAME grid-search + Equivalent Random Search
pipeline as every static/mined/`--research-strategy` candidate; it must still clear the same ERS bar
to be trusted.

A winning hybrid's `results/strategy.json` carries a `composite_spec` block (see "Data Shapes &
Schemas" below) so `backtester` can reconstruct the exact `CompositeAllocationTemplate`/
`CompositeTimingTemplate` instance. Pass `--no-compose-aspects` to disable this and restrict the
search to only the templates explicitly named/loaded (the 9 static templates, plus any
`--pattern-report`/`--research-strategy` candidates) — useful for a faster run, or to reproduce
this project's pre-aspect-composition search behavior exactly.

```bash
# Default: aspect composition ON -- may return a hybrid composite_spec winner
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ IWM EFA EEM GLD TLT \
  --mode generate --data-provider synthetic

# Disable aspect composition -- only whole static/mined/--research-strategy templates compete
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ IWM EFA EEM GLD TLT \
  --mode generate --data-provider synthetic --no-compose-aspects
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
    generator.py       Grid search across common/allocation_templates.py's 9 templates PLUS any
                        extra_templates (mined patterns), scored via common/allocation_backtester.py,
                        + Equivalent Random Search + the optional factor-report tie-break
                        (_apply_factor_tiebreak). The grid-search/ERS mechanics themselves
                        (grid_combinations, RandomAllocationTemplate, grid_search_template,
                        run_ers_validation) now live in ../../common/allocation_search.py, shared with
                        backtester's --optimize feature -- generator.py just supplies the
                        _portfolio_score callback and its own multi-template reduction/tiebreak on top;
                        no CLI flags, strategy.json schema, or user-visible behavior changed.
    regime.py           Hurst-based regime classification -- still present, unit-tested, but
                        DISCONNECTED from generator.py (see "Known, disclosed gaps" above)
    indicators.py       Re-exports from ../../common/indicators.py: the small, restricted set for the
                        9 static templates, PLUS the broader popular-indicators menu the (now
                        separate) pattern_mining stage mines against (see indicators.py's own
                        docstring for why that's a disclosed exception, not a contradiction, of
                        the "restricted set" rule)
    metrics.py          Thin re-exports from ../../common/metrics.py (including deflated_sharpe_ratio,
                        currently unused by this project's own CLI -- see above)
    data.py             Thin wrapper over ../../common/data.py
  run_strategygen.py     CLI: "generate" mode (--factor-report and --pattern-report are both optional)
  tests/                 pytest, synthetic data only
  data/, results/        gitignored
```

Turning-point pattern mining (`turning_points.py`, `pattern_mining.py`) moved to its own
`pattern_mining/` project — see `pattern_mining/README.md`.

The actual template implementations (including `PatternBasedAllocationTemplate` and
`build_aggregate_curve`), and the shared `factor_tags`/factor-taxonomy and indicator-feature
machinery, live in `../../common/allocation_templates.py`, `../../common/factor_taxonomy.py`, and
`../../common/indicator_features.py` — this project imports and searches them, it doesn't define
them (`common/` stays project-agnostic; the `pattern_mining` project's mining orchestration is a
separate stage this project consumes a durable report from, not code it owns).

## Setup

This project shares a single `uv`-managed environment with the rest of the `pipeline/` group. From
`pipeline/` (one level up):

```bash
uv sync
```

## Usage

### Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the other 3 projects — see `common/README.md`'s
cross-reference index; `resolve_universe_from_args` picks the first one supplied, in that order,
falling back to this project's own default universe (`["SPY", "QQQ"]`) if none are given.

| Flag | Type / default | Meaning |
|---|---|---|
| `--universe` / `-u` | space-separated tickers, default: none | Explicit ticker list (falls back to `["SPY", "QQQ"]`) |
| `--universe-file` | path, default: none | Load tickers from a file instead |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--start` | `YYYY-MM-DD`, default `"2015-01-01"` | History start date |
| `--end` | `YYYY-MM-DD`, default `"2024-12-31"` | History end date |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider |
| `--mode` | `generate`, default `"generate"` | Only `generate` is currently supported (see "Known, disclosed gaps") |
| `--n-random-search` | int, default `200` | Size of the Equivalent Random Search pool |
| `--ers-percentile-threshold` | float, default `0.90` | How far above the random pool a candidate must rank to be trusted |
| `--min-rebalances-for-trust` | int, default `4` | Minimum rebalance count before a result is trusted |
| `--factor-report` | path, default: none | Optional path to a `research_strategy` `factor_summary.json` (see above) |
| `--factor-tiebreak-epsilon` | float, default `0.05` | How close two templates' Sharpe ratios must be before `--factor-report` can break the tie |
| `--pattern-report` | path, default: none | Optional path to a `pattern_mining` stage `pattern_report.json` (see above and `pattern_mining/README.md`) |
| `--pattern-max-templates` | int, default `5` | Cap on how many significant findings (from `--pattern-report`) become candidate templates |
| `--research-strategy` | space-separated `strategies_config.json` keys, default: none | Include one or more `research_strategy` strategies (e.g. `baa_keller`, `adaptive_grid`) as additional candidate templates (see above) |
| `--data-provider` | str, default `"yfinance"` | `yfinance`, `csv`, `synthetic`, or a custom module specifier |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data |
| `--cache-ttl-days` | float, default: none | Maximum age (in days) of a cached OHLCV file before it's treated as stale and re-fetched; `None` (default) never expires a cache entry on age alone |
| `--no-plots` | flag, default off (charts on) | Skip writing the winning strategy's equity-curve chart (`results/equity_curve.png`) |
| `--no-compose-aspects` | flag, default off (aspect composition ON) | Disable hybrid search across DIFFERENT templates' selection/weighting or entry/exit aspects (see "Optional: aspect composition" below); restricts the search to only the templates explicitly named/loaded |

The local OHLCV cache directory now resolves to the shared, workspace-wide location
(`<repo_root>/data/`) rather than a project-local folder — see `common/README.md`'s "Shared OHLCV
cache directory" section (§7) for details.

### Sample commands (real market data)

```bash
# Generate ONE strategy for the whole universe from all available history (run from inside pipeline/)
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate

# Explicit date range and bar interval
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL MSFT NVDA \
  --start 2018-01-01 --end 2024-12-31 --interval 1d

# Universe loaded from a file (e.g. a basket produced by instrument_selection)
uv run python strategy_generator/run_strategygen.py \
  --universe-file instrument_selection/results/basket.json --mode generate

# Wider Equivalent Random Search pool and a stricter trust bar
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL GLD TLT \
  --n-random-search 500 --ers-percentile-threshold 0.95 --min-rebalances-for-trust 8

# With a research_strategy factor report as an optional tie-break input
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate \
  --factor-report research_strategy/results/factor_summary.json

# Factor report with a wider tie-break tolerance
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL TLT GLD \
  --factor-report research_strategy/results/factor_summary.json --factor-tiebreak-epsilon 0.10

# With a pattern_mining report as an additional candidate source (run the pattern_mining
# stage first -- see pattern_mining/README.md -- to produce this file)
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate \
  --pattern-report pattern_mining/results/pattern_report.json

# Pattern report with a lower candidate-template cap
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL MSFT NVDA GLD TLT \
  --pattern-report pattern_mining/results/pattern_report.json --pattern-max-templates 3

# Factor report AND a pattern report together (both optional candidate sources at once)
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL MSFT NVDA GLD TLT IEF \
  --factor-report research_strategy/results/factor_summary.json \
  --pattern-report pattern_mining/results/pattern_report.json

# Offline/synthetic data only (no network calls) -- this workspace's standing testing convention
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ AAPL --mode generate \
  --data-provider synthetic

# CSV-folder provider (offline real data you already downloaded), no local caching
uv run python strategy_generator/run_strategygen.py --universe SPY QQQ TLT GLD \
  --data-provider csv --data-dir /path/to/ohlcv_csvs --no-cache
```

## Data Shapes & Schemas

This project consumes the shared **OHLCV DataFrame**, **universe dict**, **target weights
DataFrame**, **portfolio backtest result dict**, **factor taxonomy vocabulary**, and **indicator
feature menu** shapes documented in `../../common/README.md` (§1–6) — see that file first.
`factor_summary.json` (consumed via `--factor-report`) is documented in `research_strategy/README.md`
(the project that produces it), not repeated here. Everything below is unique to this project.

### `results/strategy.json` — this project's own output (schema OWNED here; `backtester` consumes it)

| Field | Type | Notes |
|---|---|---|
| `template_name` | str | One of the 9 static template names, `pattern_<feature>_<lookback>_<peak\|trough>` for a mined winner, or `<selection_key>__<weighting_key>` / `<entry_key>__<exit_key>` for an aspect-composed hybrid winner (see `composite_spec` below) |
| `params` | dict | The winning grid-search combination (keys depend on `template_name` — see each template's `param_grid` in `../../common/allocation_templates.py`) |
| `explanation` | str | `explain_weights()`'s full text |
| `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio`, `win_rate` | float | From the shared backtest result dict |
| `profit_factor` | float or `null` | `null` when not finite |
| `trusted` | bool | `ers_passed AND total_rebalances >= min_rebalances_for_trust` |
| `ers_passed` | bool | Whether the winner beat `--ers-percentile-threshold` of the random-portfolio pool |
| `ers_percentile` | float | The winner's actual percentile against that pool |
| `factor_context` | dict or `null` | `{template_name: factor_score}` for every candidate, only when `--factor-report` was supplied (`null`/absent-equivalent otherwise) |
| `factor_tiebreak_used` | bool | Whether `--factor-report` actually changed the winner (see "consuming a factor report" above) |
| `pattern_spec` | dict or `null` | **Only non-null when `template_name` starts with `pattern_`** — the fields needed to reconstruct the exact `PatternBasedAllocationTemplate` instance: `feature_name` (str), `feature_lookback` (int or 3-int list, e.g. `[12, 26, 9]` for `macd_hist`), `threshold` (float), `comparison` (`"below"`/`"above"`), `event_type` (`"trough"`/`"peak"`), `mined_p_value` (float), `mined_n_events` (int). `backtester/run_backtest.py`'s `_get_template` reads this block to reconstruct the template when present — a hand-edited `strategy.json` naming a `pattern_*` template WITHOUT this block cannot be re-run. |
| `research_strategy_spec` | dict or `null` | **Only non-null when the winning template came from `--research-strategy`** (never both this and `pattern_spec` at once — a winning template only ever came from one source) — the fields needed to reconstruct the exact `research_strategy` instance: `strategy_key` (str, the `strategies_config.json` key, e.g. `"baa_keller"`) and `entry_data` (dict, that key's full `strategies_config.json` entry, unmodified). Reconstruct via `research_strategy.rs.strategy.instantiate_strategy_from_config_entry(strategy_key, entry_data)` — the same function `run_strategygen.py` itself calls to build the candidate in the first place. |
| `composite_spec` | dict or `null` | **Only non-null when the winner came from aspect composition** (see "Optional: aspect composition" above; mutually exclusive with every other `*_spec` field) — always has `track` (`"allocation"` or `"timing"`) plus exactly 2 more fields: `selection_key`/`weighting_key` (str, e.g. `"momentum_topn"`/`"inverse_vol"`, looked up in `common/strategy_aspects.py`'s `SELECTION_ASPECTS`/`WEIGHTING_ASPECTS`) for `track: "allocation"`, or `entry_key`/`exit_key` (str, e.g. `"rsi_oversold_entry"`/`"rsi_cross_exit"`, looked up in `research_strategy/rs/timing_aspects.py`'s `ENTRY_SIGNAL_ASPECTS`/`EXIT_RISK_ASPECTS`) for `track: "timing"`. `backtester/run_backtest.py`'s `_get_template` reads this block to reconstruct the exact `CompositeAllocationTemplate`/`CompositeTimingTemplate` instance, using this file's own top-level `params` as the reconstructed composite's `default_params`. |

### `GeneratedStrategySpec` (in-memory dataclass, `stratgen/generator.py`)

The object `StrategyGenerator.generate()` returns — a superset of `strategy.json` above, plus
`n_symbols` (int), `total_turnover`/`total_rebalances` (duplicated from the backtest result dict for
convenience), `n_trials` (int, total grid + random-search trials run), `target_weights` (the
full sparse target weights DataFrame, §3 above — `strategy.json` does NOT persist this; only
`results/strategygen_allocation_weights.csv` does, in dense/ffill'd form), and `equity_curve` (the
winning candidate's daily portfolio-value DataFrame from `common/allocation_backtester.py`'s result
dict — also not persisted in `strategy.json`; it's what `run_strategygen.py` charts, see "Outputs"
below).

### Outputs

Besides `results/strategy.json` and `results/strategygen_allocation_weights.csv` above,
`run_strategygen.py` also writes `results/equity_curve.png` by default — the winning strategy's
own IN-SAMPLE equity curve (the same data it was searched/validated on, unlike `backtester`'s
out-of-sample chart) via the shared `common/plotting.py::plot_equity_curve` helper. Pass
`--no-plots` to skip it.

### `pattern_report.json`

Produced by the separate `pattern_mining` stage and consumed here via `--pattern-report` — its
schema (`run_context`/`status`/`findings`) and the underlying mining DataFrames
(`find_turning_points`, `mine_indicator_patterns`, `build_pattern_templates`) are documented in
`pattern_mining/README.md`, not repeated here.

## Testing

```bash
# from inside pipeline/
uv run pytest strategy_generator/tests -v
```

Synthetic data only, covering: each of the 9 templates' signal logic and edge cases (mirrored in
`common/allocation_templates.py`'s own docstrings), the grid search + ERS mechanism, the
`_apply_factor_tiebreak` tie-break logic (fires only within epsilon, never overrides a clear
winner, no-op when no report is supplied — a dedicated regression test pins that omitting
`--factor-report` reproduces byte-for-byte the same winner as before this feature existed),
`--factor-report` file validation (a malformed file raises a clear, named error rather than a raw
`KeyError` surfacing deep inside `generator.py`), `--pattern-report` file validation (same style),
and the 6 new popular indicators (Bollinger Bands, Stochastic, CCI, Williams %R, EMA, OBV). The
zigzag turning-point detector and `pattern_mining.py`'s significance test (positive/negative
control tests) now live in, and are tested by, the separate `pattern_mining` project
(`uv run pytest pattern_mining/tests -v`) — see `pattern_mining/README.md`.

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
- The `pattern_mining` stage's significance test has known, disclosed limitations (a measured
  residual false-positive rate even after its lag adjustment, one fixed lag tested per run, not a
  range) — see `pattern_mining/README.md`. Treat a `--pattern-report` candidate's significance as a
  filter that earns it a shot at the ERS/backtest gate here, not as evidence on its own.
- A `PatternBasedAllocationTemplate` that wins is NOT in the static `ALLOCATION_TEMPLATES` registry
  (it's universe-specific); `run_strategygen.py` embeds a `pattern_spec` block in `strategy.json` so
  `backtester/run_backtest.py` can reconstruct the exact same instance (see its own `_get_template`)
  — a strategy.json missing that block for a `pattern_`-named template (e.g. hand-edited) cannot be
  re-run.

[ English | [简体中文](README_ZH.md) ]

# `common` — Shared Code & Data Schemas

Shared code used by every project in this workspace (`backtester`, `instrument_selection`,
`research_strategy`, `strategy_generator`): market data loading (`data.py`), universe resolution
(`universe.py`), technical indicators (`indicators.py`, `indicator_features.py`), the Hurst
exponent (`hurst.py`), performance metrics (`metrics.py`), the portfolio allocation backtester
(`allocation_backtester.py`) and its templates (`allocation_templates.py`), shared grid-search +
Equivalent Random Search validation (`allocation_search.py`), the shared factor taxonomy
(`factor_taxonomy.py`), rebalance scheduling (`scheduling.py`), synthetic-data test generators
(`testing.py`), shared CLI scaffolding for every `run_*.py` entrypoint (`cli_utils.py`), shared
output-writing conventions (`reporting.py`), shared shuffle/placebo-null significance testing
primitives (`significance.py`), and shared chart generation (`plotting.py`).

**This file is the single source of truth for every DataFrame/dataset shape used by 2+ projects in
this workspace.** Each project's own README documents only the schemas that are genuinely unique to
it, and links back here for anything shared — see the bottom of this file for the cross-reference
index. Don't duplicate a schema definition in a project README if it's already documented below.

---

## 1. OHLCV DataFrame

The universal price-data shape. Produced by every `BaseDataProvider` in `data.py`
(`YFinanceDataProvider`, `CSVFolderDataProvider`, `SyntheticDataProvider`, `CachedDataProvider`,
`MarketDBDataProvider`, `FuyaoDataProvider`) and by `common/testing.py`'s synthetic generators;
consumed by every indicator function in `indicators.py`/`indicator_features.py` and every
`AllocationTemplate`.

| | |
|---|---|
| **Index** | `pd.DatetimeIndex`, ascending, one row per trading day |
| **Columns** | `Open`, `High`, `Low`, `Close` (all `float`) — `Volume` (`float`) is present from every real `BaseDataProvider` and from `SyntheticDataProvider`, but **absent** from `common/testing.py`'s bare helpers (`make_ohlcv_from_closes`, `make_random_walk_df`, `make_oscillating_df`, `make_trending_pullback_df`, `make_ar1_ohlcv`) — code that needs `Volume` (e.g. `common.indicators.obv`) must use `SyntheticDataProvider`-backed data in tests, not the bare helpers |
| **Invariant** | `High >= Close >= Low` and `High >= Open >= Low` per row (not separately enforced by every synthetic generator, but true of real data and of `SyntheticDataProvider`) |

## 2. Universe dict

`Dict[str, pd.DataFrame]` — ticker symbol → that symbol's own OHLCV DataFrame (§1). The standard
shape passed to every `AllocationTemplate.generate_weights(universe, params)`,
`run_allocation_backtest(universe, ...)`, and every project's own per-symbol metric functions.
Produced by `common.data.load_universe`/`load_ohlcv` (looped) and by
`common.universe.resolve_universe_from_args` (symbols only — resolving to prices is a separate
step via `load_universe`). Symbols across a universe are NOT guaranteed to share an identical
`DatetimeIndex` (different listing histories) unless a caller explicitly aligns them (e.g.
`backtester/run_backtest.py`'s `_align_universe`, an inner join).

## 3. Target weights DataFrame (sparse)

The output of every `AllocationTemplate.generate_weights(universe, params)` — see
`allocation_templates.py`'s module docstring for the full rationale, summarized here as the data
contract:

| | |
|---|---|
| **Index** | `pd.DatetimeIndex` — the same calendar as the universe's own OHLCV data (or an inner-joined subset of it, e.g. `common.allocation_templates.build_aggregate_curve`'s output calendar) |
| **Columns** | one per symbol in the universe |
| **Values** | target portfolio weight (0.0–1.0); a row's values should sum to `<= 1.0` (unallocated weight is idle cash, earning 0% — see `allocation_backtester.py`) |
| **Sparsity (load-bearing, not optional)** | a row is `NaN` on every date EXCEPT an actual rebalance date, where it holds the new target — **even if that target is numerically identical to the previous rebalance's** (e.g. equal-weight recomputing the same 1/N every period). The backtester (§4) tells "rebalanced to the same weight" apart from "no rebalance happened" by row PRESENCE, not by value-equality. Templates must never forward-fill their own output; only the backtester does that internally. |

Two coexisting, both-correct conventions for how a template represents "insufficient data to compute a target yet" on a given rebalance date (see `common/allocation_templates.py` for concrete examples of each):
- **Rank-based templates** (momentum, mean-reversion, breadth-gated) initialize the row to all-`0.0` (an explicit, deliberate all-cash rebalance instruction).
- **Covariance-based templates** (HRP, minimum-variance, max-diversification) leave the row as `NaN` (not a rebalance at all — the backtester drifts the previous weights forward).

CSV exports of this shape (e.g. `pipeline/research_strategy/results/<strategy>_weights.csv`,
`pipeline/strategy_generator/results/strategygen_allocation_weights.csv`,
`backtester/results/backtest_weights.csv`) apply `.ffill().fillna(0.0)` before writing — i.e. the
saved CSV is the DENSE, forward-filled daily weight series, not the sparse in-memory contract above.

## 4. Portfolio backtest result dict

Returned by `common.allocation_backtester.run_allocation_backtest(universe, target_weights, ...)` —
the single shared backtesting engine used by `backtester`, `research_strategy`, and
`strategy_generator`.

| Key | Type | Meaning |
|---|---|---|
| `equity_curve` | `pd.DataFrame` (1 column: `equity`), `DatetimeIndex` | Daily portfolio equity, starting at `initial_capital` |
| `actual_weights` | `pd.DataFrame`, `DatetimeIndex`, columns = symbols | DENSE daily weights actually held (post-drift, post-rebalance) — not sparse |
| `total_turnover` | `float` | Sum of absolute weight changes across every rebalance |
| `total_rebalances` | `int` | Count of dates with an actual rebalance instruction |
| `total_return`, `cagr`, `max_drawdown`, `sharpe_ratio`, `calmar_ratio`, `win_rate`, `profit_factor` | `float` | Standard performance metrics. `max_drawdown` is a **positive magnitude** (e.g. `0.18` for an 18% drawdown), matching `common/metrics.py`'s own convention. `win_rate`/`profit_factor` here are computed from the DAILY RETURN SERIES (`common.metrics.win_rate_from_returns`/`profit_factor_from_returns`) — a DIFFERENT convention from `common.metrics.win_rate`/`profit_factor`, which take a trades DataFrame (`side`/`pnl` columns); the two same-named pairs are NOT interchangeable, see `common/metrics.py`'s own docstrings |

On an empty/degenerate input, only `{"equity_curve": pd.DataFrame(), "turnover": 0.0}` is returned
(note: `"turnover"`, not `"total_turnover"`, in this specific empty-input short-circuit — callers
should check `result["equity_curve"].empty` before relying on the other keys).

## 5. Factor taxonomy tag vocabulary

`common/factor_taxonomy.py`'s `FACTOR_CATEGORIES: Dict[str, str]` — the one shared vocabulary both
`research_strategy` (`strategies_config.json`'s `"factors"` key) and `common.allocation_templates`
(`AllocationTemplate.factor_tags`, a `List[str]` field) use to tag which quantitative factor(s) a
strategy/template conditions on. Valid tags: `absolute_momentum_trend`, `relative_momentum`,
`volatility_targeting`, `mean_reversion`, `breadth`, `correlation_diversification`,
`regime_trend_strength`, `static_fixed_weight`. See `pipeline/research_strategy/README.md`'s "Factor
Tagging" section and `pipeline/strategy_generator/README.md`'s "consuming a research_strategy factor report"
section for how this vocabulary is actually used (the mechanism, not the schema, lives there).

## 6. Indicator feature menu (`name`, `lookback`) pairs

`common/indicator_features.py`'s `DEFAULT_FEATURE_MENU: List[Tuple[str, int | Tuple[int,int,int]]]`
— e.g. `("rsi", 14)`, `("macd_hist", (12, 26, 9))`. `feature_label(name, lookback) -> str` turns a
pair into a column-safe label (e.g. `"rsi_14"`, `"macd_hist_12_26_9"`); `compute_feature(curve,
name, lookback) -> pd.Series` computes it. Used by `pipeline/pattern_mining/pmine/pattern_mining.py`'s
feature table (see that project's README) and by `common.allocation_templates.PatternBasedAllocationTemplate`
— the SAME dispatch backs both, deliberately, so a mined threshold is tested and later traded
against an identical computation.

## 7. Grid-search + Equivalent Random Search (ERS) validation (`allocation_search.py`)

Shared template-agnostic search/validation primitives, extracted from
`pipeline/strategy_generator/stratgen/generator.py` so `backtester`'s `--optimize` flag can tune a SINGLE,
already-chosen template's parameters using the exact same validated mechanism instead of a second,
independently-maintained copy. Used by both `strategy_generator`'s multi-template search (one
`grid_search_template` call per candidate template, `run_ers_validation` once on the overall
winner) and `backtester --optimize`'s single-template tuning (`optimize_template`, which composes
both for you).

Every function takes a `ScoreFn = Callable[[object, dict], dict]` callback —
`score_fn(template, params) -> dict` — instead of hardcoding how to backtest a candidate; the
returned dict must have at least a `"sharpe_ratio"` key (see §4's result dict). `strategy_generator`
supplies a single-shot `run_allocation_backtest`-based scorer; `backtester` supplies
`run_standard`/`run_walkforward`-based ones. Any exception `score_fn` raises is caught internally
and degraded to a `-inf`-Sharpe fallback trial plus a `RuntimeWarning` naming the exception, so a
bad candidate can never crash the whole search; if EVERY trial (or every ERS random draw) fails
this way, a second, more prominent aggregate `RuntimeWarning` fires too, since that pattern usually
means `score_fn` itself is broken rather than every candidate being genuinely bad.

Public members:

| Member | Purpose |
|---|---|
| `grid_combinations(param_grid: dict) -> list` | Cartesian product of a `{param_name: [values]}` dict into a list of param dicts; an empty/falsy `param_grid` returns `[{}]` (one degenerate trial), never `[]` |
| `random_weights(universe, rebalance_freq_days, rng) -> pd.DataFrame` | Generates a random valid sparse weight matrix (§3's shape) for the ERS null comparison |
| `RandomAllocationTemplate(rng)` | A dummy `AllocationTemplate` subclass (see §3) used purely for the ERS check — `name="random_allocation"`, empty `param_grid`/`factor_tags`, `generate_weights` backed by `random_weights` |
| `grid_search_template(template, score_fn) -> list` | Evaluates `score_fn` over every combination in `template.param_grid`; returns `[{"params", "result", "score"}, ...]`, one entry per trial |
| `run_ers_validation(params, best_score, best_result, score_fn, *, n_random_search=200, ers_percentile_threshold=0.90, min_rebalances_for_trust=4, seed=None) -> dict` | Draws `n_random_search` random-weight portfolios, scores each, and returns `{"ers_percentile", "ers_passed", "trusted"}` — `ers_percentile` fail-safes to `0.0` (never a trivial `1.0`) when every random trial is non-finite |
| `optimize_template(universe, template, score_fn, **ers_kwargs) -> dict` | Convenience wrapper: `grid_search_template` then `run_ers_validation` on its winner, for the single-template case |

## 8. Shared chart generation (`plotting.py`)

`plot_equity_curve(equity: pd.Series, results_dir: str, *, baseline: Optional[pd.Series] = None,
strategy_label="Strategy", baseline_label="Baseline", title="Equity Curve",
filename="equity_curve.png") -> str` — single-line equity chart (or two lines if `baseline` is
given, e.g. a buy-and-hold benchmark plotted alongside the strategy). `equity`/`baseline` are plain
`pd.Series` (date index -> portfolio value) — pass `result["equity_curve"]["equity"]` (§4), not the
raw `run_allocation_backtest()` dict. Saves under `results_dir` (created if missing) and returns the
saved absolute path. Used by `backtester/run_backtest.py` and `pipeline/strategy_generator/run_strategygen.py`
to produce the equity-curve chart that accompanies each run's report, so chart styling/behavior
stays identical across both instead of being reimplemented per project.

## 9. Shared OHLCV cache directory

Every project's `run_*.py` resolves its `--cache-dir`/`DATA_DIR` default via
`common.cli_utils.shared_data_dir()`, which always resolves to a single `<repo_root>/data/`
directory regardless of which project calls it — a symbol/interval/date-range fetched by one
project is reused by all the others instead of being downloaded/cached separately per project.

`common.data.CachedDataProvider` names each cache file `{ProviderClassName}_{symbol}_{interval}_
{start}_{end}.csv` (e.g. `YFinanceDataProvider_SPY_1d_2015-01-01_2024-12-31.csv`). The provider-class
prefix is load-bearing, not cosmetic: `research_strategy` defaults to `--data-provider synthetic`
while the other 3 projects default to `--data-provider yfinance`, so without it, two projects
requesting the "same" symbol/interval/date-range from different providers against this shared
directory would silently read back each other's (wrong-provider) cached data.

A cache entry never expires by default (`cache_max_age_days=None`, `CachedDataProvider`'s
constructor default) — every `run_*.py`'s `--cache-ttl-days` flag (added via
`add_data_provider_cli_args`, so its name/behavior is identical across all 4 projects and
`run_pipeline.py`'s passthrough) opts into re-fetching a cached file older than N days instead.

---

## Cross-reference index

| Project | Uses from this file | Documents locally (see that project's own README) |
|---|---|---|
| `backtester` | §1–4, 7–8 | `strategy.json` consumption (schema owned by `strategy_generator`), `backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv` (see `backtester/SCHEMAS.md`) |
| `instrument_selection` | §1–2 (universe/OHLCV in) | `screening_report.csv`, `correlation_matrix.csv`, `screened_out.csv`, `basket.json` |
| `research_strategy` | §1–5 | `research_strategy_report.json`, `factor_summary.json`, `strategies_config.json` entry schema |
| `pattern_mining` | §1–2, 6 | `pattern_report.json` (schema OWNED here), turning-points/feature-table/findings DataFrames |
| `strategy_generator` | §1–8 | `strategy.json` (schema OWNED here), `GeneratedStrategySpec` |

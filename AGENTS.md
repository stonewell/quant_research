# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, etc.) working in this repository.
This file is deliberately shorter than `README.md` and the per-project `README.md`/`SCHEMAS.md`
files -- it captures conventions and gotchas an agent needs to not break things, not the full CLI
reference. Read `README.md` first for the end-to-end pipeline picture and data-flow diagram.

## What this repo is

A modular, six-component quantitative trading research/backtesting workspace: `research_strategy`
(factor research) -> `instrument_selection` (universe screening) -> `pattern_mining` (optional
turning-point mining) -> `strategy_generator` (allocation strategy search) -> `backtester`
(standalone evaluation), all built on shared infrastructure in `common/`. `run_pipeline.py` chains
all 5 stages via subprocess. See `README.md`'s "Workspace Architecture & Data Flow" section for the
diagram and each stage's I/O schema.

## Setup & running things

```bash
uv sync                                    # one shared env for the whole workspace
uv run python <project>/run_*.py --help    # every project's CLI
uv run python run_pipeline.py --data-provider synthetic --universe SPY QQQ ...
```

On Windows, the venv's interpreter is at `.venv/Scripts/python.exe` if `uv run` isn't available in
the current shell.

## Testing policy -- read before running or writing tests

- **Never use real market data or network access in tests or example commands.** Every test suite
  runs 100% offline via `SyntheticDataProvider`/`common/testing.py`'s synthetic OHLCV generators.
  Default any CLI example you write to `--data-provider synthetic`; only use `yfinance` when a human
  explicitly asks for a real-data run.
- Each project has its own `tests/` directory (`common/tests`, `research_strategy/tests`,
  `instrument_selection/tests`, `pattern_mining/tests`, `strategy_generator/tests`,
  `backtester/tests`, root `tests/`). None has an `__init__.py`, and several projects share
  identically-named test files (`test_allocation_templates.py`, `test_indicators.py`, ...), so a
  bare `pytest` from the repo root -- or `pytest` given more than one of these directories at
  once -- fails with `import file mismatch`. Two ways around this:
  - Run one directory at a time (what the per-project READMEs show): `pytest common/tests -q`.
  - Or pass `--import-mode=importlib` to run several/all directories together in one invocation:
    `pytest common/tests strategy_generator/tests backtester ... -q --import-mode=importlib`.

## Core domain model -- must-know before touching template/strategy code

- Every strategy/allocation template is an `AllocationTemplate` subclass (`common/allocation_templates.py`)
  implementing `generate_weights(universe, params) -> DataFrame`, `explain_weights(params) -> str`,
  and `warmup_bars(params) -> int`. Two families implement this interface: the 9 static, zero-arg
  templates in `common/allocation_templates.py`, and the 18 richer, `StrategyConfig`-driven
  templates (basket presets + single-asset timing strategies) in `research_strategy/rs/strategy.py`.

- **Sparse weights contract (critical):** `generate_weights` returns a DataFrame indexed by date
  where a row is `NaN` on every day EXCEPT an actual rebalance date, where it holds the real target
  weight. Templates must **never forward-fill their own output** -- the backtester
  (`common/allocation_backtester.py`) tells "a rebalance was instructed" apart from "nothing
  happened today" by whether the row is present at all, not by whether its value changed from the
  prior row (a template can legitimately recompute an identical target on consecutive rebalances).

- **Cell-level NaN vs. 0.0 (critical -- caused two real bugs in this session's aspect-composition
  work, both fixed):** `run_allocation_backtest` does `sparse_weights.ffill().fillna(0.0)`
  **column-wise across the whole frame**. Inside an actual rebalance row, a `NaN` cell for one
  symbol does **not** mean "zero" -- it carries forward that symbol's last non-NaN value from a
  *previous* rebalance. If a template's own logic decides to exclude or de-risk a symbol it
  previously held, it **must write an explicit `0.0`** for that symbol at that rebalance date, never
  leave it `NaN`, or the backtester will silently keep holding the stale prior position.

- **Equivalent Random Search (ERS)** (`common/allocation_search.py`): the winning template/params is
  validated by comparing its Sharpe against N random-weight portfolios; it's only `trusted` if it
  beats a percentile threshold *and* has enough rebalances. Run ERS **once per search**, on the
  already-chosen winner -- never once per candidate template (that would burn the random-draw budget
  N times over and change what "beating random chance" means).

- **Factor taxonomy** (`common/factor_taxonomy.py`): tags like `relative_momentum`,
  `mean_reversion`, `volatility_targeting` on templates/strategies feed `strategy_generator`'s
  optional factor-report tie-break -- it can only resolve a near-tie between top candidates, never
  override a template that clearly won on backtested Sharpe.

- **Aspect composition** (added recently -- `common/strategy_aspects.py` for basket templates,
  `research_strategy/rs/timing_aspects.py` for single-asset timing templates): `strategy_generator`
  no longer just picks the single best whole template. It decomposes several templates into
  reusable pieces -- *selection* (which symbols/how much to invest) + *weighting* (how to size
  across them) for the 9 static templates; *entry signal* + *exit/risk/sizing* for 4 of the 18
  timing templates -- and searches hybrid pairings across DIFFERENT source templates too (e.g.
  momentum's stock-picking + inverse-volatility's sizing), on by default via
  `GeneratorConfig.enable_aspect_composition` / CLI `--no-compose-aspects`. A winning hybrid embeds a
  `composite_spec` block in `strategy.json` so `backtester` can reconstruct the exact instance. Read
  both modules' docstrings before extending this -- the NaN-vs-0.0 rule above is exactly what bit
  the first version of this feature.

## Documentation & coding style conventions

This codebase is unusually disclosure-heavy -- match it, don't strip it out:

- Non-trivial functions/classes that port a known strategy or technique cite their academic
  grounding (author, year, venue) and explicitly disclose any simplification versus the original
  (e.g. "HONEST CAVEAT", "DISCLOSED APPROXIMATION" comments).
- Comments explain *why*, often naming a specific prior bug the current code shape fixes -- don't
  delete these as "obvious" when touching nearby code; they're load-bearing history, not clutter.
- Prefer reusing existing shared primitives (`common/indicators.py`, `common/allocation_search.py`,
  `common/covariance.py`, `common/scheduling.py`, `common/testing.py`) over reimplementing math that
  already exists there.
- Git commit messages in this repo are terse, lowercase, present-tense summaries (e.g. "add chan
  pivot", "add denoise", "pattern mining merge with current strategy gen instead of separated
  pipeline").

## Known pre-existing issues (not yet fixed -- don't be alarmed)

- `HierarchicalRiskParityAllocation` can raise `IndexError` on some small/degenerate synthetic
  universes. It's already caught and degraded to `-inf` Sharpe by `_portfolio_score`, so it doesn't
  break a run -- that candidate just loses -- but you may see the warning in output.

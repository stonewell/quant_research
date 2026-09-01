[ English | [简体中文](README_ZH.md) ]

# Point-in-Time Buy/Sell Signal (`live_signal`)

A dedicated `pipeline/` project that answers the operational question none of the other pipeline
stages do: given a universe and an already-generated, fixed strategy (`strategy.json`, the same file
`strategy_generator` produces and `backtester` consumes), **what should I actually do today** (or as
of any given date)? `backtester` evaluates a fixed strategy over a historical date *range*; this
project evaluates it at a single point in time and turns the result into a concrete, actionable buy/
sell signal and rebalance instruction.

This project deliberately does no strategy search of its own — it only reconstructs and re-runs an
already-generated strategy, exactly like `backtester` does, via the same reconstruction function.

---

## 1. What it does

1. Truncates each universe symbol's OHLCV history to `<= --as-of-date` (default: today) — the one
   guarantee that makes the output point-in-time correct: data after that date is never consulted,
   regardless of what a data provider happens to return.
2. Reconstructs the strategy from `--strategy-file` via `backtester.run_backtest`'s own
   `_get_template`/`_load_strategy_file` (the single place in this repo that handles all 6
   `strategy.json` variants — plain static, `pattern_spec`, `research_strategy_spec`,
   `composite_spec`, `fundamental_spec`, `bnn_spec` — uniformly). **Coupling caveat**: these are
   `_`-prefixed private functions in a CLI script, not a published API; if `backtester/run_backtest.py`
   is ever refactored without updating this project, this import breaks silently. Deliberately reused
   anyway rather than duplicating ~110 lines of 6-way reconstruction logic.
3. Runs `template.generate_weights(universe, params)` on the truncated data and extracts the strategy's
   **current target weights** — its most recent real rebalance row (the sparse-weights contract, see
   `common/README.md` §3) at or before `--as-of-date`.
4. Compares that target against a **reference**:
   - `--current-holdings`/`--current-holdings-file` (a `{symbol: weight_fraction}` JSON of your
     ACTUAL current portfolio), if given — produces an exact trade list from your real holdings to
     the strategy's target.
   - Otherwise, the strategy's own previous rebalance — a self-consistent signal with zero extra
     input, but it only tells you what CHANGED in the strategy's own recommendation, not what to
     actually trade if your real portfolio has drifted from it.
5. Classifies every symbol as `buy` (target > reference), `sell` (target < reference), or `hold`
   (unchanged, still held) — a symbol at ~0 on both sides is dropped as noise. Reports the top
   `--top-n` buy candidates ranked by target weight, the full buy/sell lists, and the full rebalance
   instruction table.

## 2. Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/`--universe-kwargs`)
and the data-provider trio (`--data-provider`/`--data-dir`/`--no-cache`/`--cache-ttl-days`) are shared
with every other project — see `common/README.md`'s cross-reference index.

| Flag | Type / default | Meaning |
|---|---|---|
| `--strategy-file` | path, **required** | Path to the `strategy.json` exported by `strategy_generator` (or `backtester`-compatible equivalent from `research_strategy`/`fundamental_screener`/`bnn_forecaster`) |
| `--as-of-date` | `YYYY-MM-DD`, default: today | Point-in-time date to evaluate the strategy at. Any past date works too (deterministic testing/debugging) — data after this date is never used. |
| `--lookback-days` | int, default `800` | Calendar days of history to load before `--as-of-date` (~2.2 years — comfortably covers every existing template's `warmup_bars`). Raise this if you see the short-history warning. |
| `--current-holdings` | JSON str, default: none | `{symbol: weight_fraction}` of your actual current portfolio. Omit (with `--current-holdings-file` too) to compare against the strategy's own previous rebalance instead. |
| `--current-holdings-file` | path, default: none | Same shape as `--current-holdings`, loaded from a file. Mutually exclusive with `--current-holdings`. |
| `--top-n` | int, default `5` | How many top buy candidates to highlight, ranked by target weight. |
| `--action-threshold` | float, default `1e-6` | Minimum \|weight delta\| to count as a buy/sell rather than a hold. |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider. |
| `--results-dir` | path, default: none | Override the output directory (default: `live_signal/results/`). |
| `--cache-dir` | path, default: none | Override the shared, workspace-wide OHLCV cache directory. |

Note: unlike `research_strategy` (`--data-provider` default `synthetic`), this project's default is
`yfinance` — matching `backtester`'s own default, since this tool's entire purpose is a live,
actionable signal against real prices. Every test and doc example below still explicitly passes
`--data-provider synthetic` per this workspace's offline testing policy — the default only affects an
interactive human run.

## 3. Sample commands

```bash
# from inside pipeline/ (offline/synthetic -- this workspace's standing testing convention)

# 0. Need a strategy.json first (skip if you already have one from strategy_generator/
#    research_strategy/fundamental_screener/bnn_forecaster -- any of them work unchanged):
uv run python strategy_generator/run_strategygen.py \
  --universe SPY QQQ BIL --data-provider synthetic --mode generate

# No holdings given -- compares against the strategy's own previous rebalance
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ BIL --data-provider synthetic --as-of-date 2024-06-01

# Holdings-aware -- an exact trade list from your real portfolio to the strategy's target
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ BIL --data-provider synthetic --as-of-date 2024-06-01 \
  --current-holdings '{"BIL": 1.0}'

# Universe loaded from a file (e.g. a basket produced by instrument_selection)
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe-file instrument_selection/results/basket.json --data-provider synthetic
```

```bash
# Real market data -- "what should I do today" against actual prices
uv run python live_signal/run_live_signal.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT --data-provider yfinance \
  --current-holdings-file my_portfolio.json
```

A `bnn_spec`-sourced `strategy.json` requires `bnn_forecaster`'s own isolated venv (same caveat as
`backtester`): `ml/bnn_forecaster/.venv/Scripts/python.exe live_signal/run_live_signal.py ...`.

## 4. Output

Console: run context (`as-of` date requested vs. the actual signal date used, reference source), the
strategy's `explain_weights()` narrative, top-N buy candidates, the full buy/sell lists, and every
symbol's weight delta.

`results/live_signal_report.json`:

| Field | Type | Notes |
|---|---|---|
| `status` | str | `"ok"` or `"no_signal"` (no rebalance occurred at/before `--as-of-date` — insufficient warmup/history, not an error) |
| `run_context` | object | `as_of_date`, `signal_date` (the actual rebalance date used), `template_name`, `universe`, `reference_source` |
| `current_target_weights` | object | The strategy's current target weight per symbol |
| `reference_weights` | object | The comparison point (holdings or the strategy's previous rebalance) |
| `buy_signal` / `sell_signal` | array | Rows of the rebalance instruction table (see below) with `action` `"buy"`/`"sell"` |
| `top_n_buys` | array | The `--top-n` buy rows with the largest `target_weight` |
| `rebalance_instruction` | array | Every non-noise symbol: `symbol`, `target_weight`, `reference_weight`, `delta`, `is_new_position`, `action` |

`results/live_signal_instruction.csv` — the same `rebalance_instruction` table, for spreadsheet/trade-desk use.

## 5. Directory structure

```
pipeline/live_signal/
├── lsig/
│   ├── __init__.py
│   └── signal.py            # pure logic: as-of truncation, rebalance-instruction/delta computation, top-N buys
├── run_live_signal.py       # CLI: args, universe/data load, strategy reconstruction, orchestration, output
├── tests/
│   └── test_signal.py       # offline unit tests (pure logic + synthetic-data CLI tests)
└── README.md
```

## 6. Testing

100% offline, per this workspace's standing policy — every CLI-level test passes `--data-provider
synthetic` explicitly.

```bash
uv run pytest live_signal/tests -v
```

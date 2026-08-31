[ English | [简体中文](README_ZH.md) ]

# `backtester`

Standalone CLI that evaluates a fixed, already-generated allocation strategy (a `strategy.json`
exported by `strategy_generator`) against a basket of assets — either once, over the full date
range (`--mode standard`), or across rolling time windows to check consistency with no
re-optimization (`--mode walkforward`). This project deliberately does no strategy search of its
own; it only re-runs the shared `common.allocation_backtester.run_allocation_backtest` engine that
`strategy_generator` and `research_strategy` also use.

Data shapes/schemas (the `strategy.json` input, the 3 CSV outputs, and the shared OHLCV/universe/
target-weights/result-dict shapes this project consumes) are documented in `SCHEMAS.md`, not
repeated here.

## Setup

This project has no `pyproject.toml`/`uv` environment of its own — run it with whichever group's
venv fits the strategy you're evaluating (see the root `README.md`'s "Setup & Environment"
section). For the pipeline group:

```bash
cd pipeline && uv sync
```

Then invoke this project's CLI from the repo root via that venv's interpreter directly (as every
sample command below does), e.g. `pipeline/.venv/Scripts/python.exe backtester/run_backtest.py
...` — a bare `uv run python backtester/run_backtest.py ...` from the repo root fails
(`ModuleNotFoundError: No module named 'numpy'`) since there's no `uv`-managed environment at the
repo root for `uv run` to fall back to.

## Usage

### Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the other 3 projects — see `common/README.md`'s
cross-reference index. Unlike every other project's CLI, this one passes **no default universe**
to `resolve_universe_from_args` — one of the 3 universe flags is effectively required; omitting
all of them raises `ValueError("No universe symbols provided or resolved...")`.

| Flag | Type / default | Meaning |
|---|---|---|
| `--strategy-file` | path, **required** | Path to the `strategy.json` exported by `strategy_generator` (schema in `SCHEMAS.md`) |
| `--universe` / `-u` | space-separated tickers, default: none | Explicit ticker list to backtest the strategy against (**no fallback default** — see above) |
| `--universe-file` | path, default: none | Load tickers from a file instead |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--start` | `YYYY-MM-DD`, default `"2015-01-01"` | History start date |
| `--end` | `YYYY-MM-DD`, default `"2024-12-31"` | History end date |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider |
| `--mode` | `standard` \| `walkforward`, default `"standard"` | `standard` evaluates the full date range once; `walkforward` re-evaluates the SAME fixed params across rolling folds, no re-optimization |
| `--window-years` | float, default `1.0` | Walkforward fold length, in years (only used with `--mode walkforward`) |
| `--step-years` | float, default `0.5` | Walkforward fold step size, in years (only used with `--mode walkforward`) |
| `--initial-capital` | float, default `100000.0` | Starting portfolio equity |
| `--commission-pct` | float, default `0.0005` | Per-trade commission, as a fraction of traded notional |
| `--slippage-pct` | float, default `0.0005` | Per-trade slippage, as a fraction of traded notional |
| `--baseline-symbol` | str, default: none | Optional single reference symbol (e.g. `SPY`) to compare the strategy against. Off by default — none of the comparison code runs unless this is set |
| `--baseline-template` | str, default `"equal_weight"` | Static allocation template (one of the 9 in `ALLOCATION_TEMPLATES` — no `pattern_*` templates) used to turn `--baseline-symbol` into a baseline equity curve |
| `--baseline-params` | JSON str, default: none | Params for `--baseline-template` as a JSON object string (default: that template's first `param_grid` combination) |
| `--optimize` | flag, default off | Grid-search the loaded strategy's `template.param_grid` on THIS universe (scored via the same `--mode` you selected) and Equivalent-Random-Search-validate the winner (via the shared `common/allocation_search.py`) before running the final backtest. If the winner fails ERS validation, falls back to the strategy file's ORIGINAL params — never silently produces no output. Always writes `results/optimize_report.json` (see `SCHEMAS.md`) |
| `--n-random-search` | int, default `200` | Size of the Equivalent Random Search pool used to validate `--optimize`'s winning combination |
| `--ers-percentile-threshold` | float, default `0.90` | How far above the random-portfolio pool the winning combination must rank to be trusted |
| `--min-rebalances-for-trust` | int, default `4` | Minimum `total_rebalances` the winning combination must have before it's trusted, even if it clears the ERS percentile |
| `--data-provider` | str, default `"yfinance"` | `yfinance`, `csv`, `synthetic`, or a custom module specifier |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data |
| `--results-dir` | path, default: none | Override where `backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv` are written (defaults to `backtester/results/`) |
| `--cache-dir` | path, default: none | Override the OHLCV CSV cache directory (defaults to the shared, workspace-wide `<repo_root>/data/` — see `common/README.md`'s "Shared OHLCV cache directory" section) |
| `--cache-ttl-days` | float days, default: none | Re-fetch a cached OHLCV file older than N days instead of trusting it forever |
| `--no-plots` | flag, default off (charts on) | Skip the `equity_curve.png` chart normally produced in `--mode standard` |

### Sample commands

Run from the repo root, via the pipeline venv's interpreter directly (see "Setup" above) — `uv
run` doesn't work here since this project has no environment of its own.

#### Real market data

```bash
# Standard mode: full-history evaluation of a generated strategy on a new basket
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --mode standard

# Explicit date range and bar interval
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA --start 2018-01-01 --end 2024-12-31 --interval 1d

# Universe loaded from a file (e.g. a basket produced by instrument_selection)
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe-file pipeline/instrument_selection/results/basket.json --mode standard

# Walkforward mode: rolling-fold consistency check with the default 1y window / 0.5y step
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward

# Walkforward mode with custom window/step sizes
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward --window-years 2 --step-years 1

# Custom trading-cost assumptions and starting capital
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --initial-capital 250000 --commission-pct 0.001 --slippage-pct 0.001

# Re-running a mined pattern-based strategy (strategy.json with a pattern_spec block) on a new basket
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT IEF --mode standard

# Re-running a research_strategy-sourced strategy (strategy.json with a research_strategy_spec
# block -- a research_strategy strategy that won strategy_generator's search) on a new basket;
# works identically in both --mode standard and --mode walkforward
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY TLT BIL GLD --mode standard
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY TLT BIL GLD --mode walkforward

# Re-running an aspect-composed hybrid strategy (strategy.json with a composite_spec block --
# a winning pairing of one template's selection/entry aspect with a DIFFERENT template's own
# weighting/exit aspect; see strategy_generator's --no-compose-aspects) on a new basket
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ IWM EFA EEM GLD TLT --mode standard

# Re-running a fundamental_screener-sourced strategy (strategy.json with a fundamental_spec
# block) -- see pipeline/fundamental_screener/README.md for how fundamental_strategy.json is produced
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/fundamental_screener/results/fundamental_strategy.json \
  --universe KO PG SPY BIL --mode standard

# Re-running a bnn_forecaster-sourced strategy (strategy.json with a bnn_spec block) -- MUST use
# bnn_forecaster's own isolated venv, not pipeline's (see ml/bnn_forecaster/README.md)
ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file ml/bnn_forecaster/results/bnn_strategy.json \
  --universe KO PG SPY BIL --mode standard

# Custom results/cache directories, no local caching of the fetched data
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --no-cache \
  --results-dir /tmp/backtest_results --cache-dir /tmp/backtest_cache

# CSV-folder provider (offline real data you already downloaded)
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --data-provider csv --data-dir /path/to/ohlcv_csvs
```

#### Offline / synthetic data

```bash
# Offline/synthetic data only (no network calls) -- this workspace's standing testing convention
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic

# Standard mode with a baseline symbol comparison (synthetic data -- no network calls)
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic \
  --baseline-symbol SPY --baseline-template equal_weight

# Walkforward mode with a baseline comparison and custom baseline params (synthetic data)
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode walkforward \
  --baseline-symbol SPY --baseline-params '{"rebalance_freq_days": 21}'

# --optimize: re-tune the loaded strategy's params on this universe/mode and
# ERS-validate the winner before running the final backtest (synthetic data)
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode standard \
  --optimize --n-random-search 200 --ers-percentile-threshold 0.90

# --optimize under --mode walkforward: each candidate is scored by its MEAN
# fold Sharpe across the same rolling windows walkforward would report
pipeline/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file pipeline/strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode walkforward \
  --optimize --n-random-search 100 --ers-percentile-threshold 0.90 --min-rebalances-for-trust 4
```

Outputs land in `results/` (or `--results-dir` if given): `backtest_equity.csv` and
`backtest_weights.csv` (`--mode standard`), or `walkforward_report.csv` (`--mode walkforward`) —
see `SCHEMAS.md` for column-level detail. `--mode walkforward` also always writes
`walkforward_summary.json` (mean fold metrics plus the Deflated Sharpe Ratio). When
`--baseline-symbol` is set: `baseline_equity.csv` and `comparison_report.json` are written in both
modes, and `walkforward_report.csv` gains 5 extra `baseline_*`/`outperformance` columns. In
`--mode standard`, unless `--no-plots` is given, `equity_curve.png` is also written (a two-line
chart, strategy vs. baseline, if `--baseline-symbol` is set). When `--optimize` is set,
`results/optimize_report.json` is always written (success or failure) — see `SCHEMAS.md` — and, on
a trusted win, the FINAL backtest (and therefore every other output above) reflects the tuned
`best_params` instead of the strategy file's original params.

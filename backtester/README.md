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

This project shares a single `uv`-managed environment with the rest of the workspace. From the
repo root (one level up):

```bash
uv sync
```

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
| `--data-provider` | str, default `"yfinance"` | `yfinance`, `csv`, `synthetic`, or a custom module specifier |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data |
| `--results-dir` | path, default: none | Override where `backtest_equity.csv`/`backtest_weights.csv`/`walkforward_report.csv` are written (defaults to `backtester/results/`) |
| `--cache-dir` | path, default: none | Override the OHLCV CSV cache directory (defaults to the shared, workspace-wide `<repo_root>/data/` — see `common/README.md`'s "Shared OHLCV cache directory" section) |
| `--cache-ttl-days` | float days, default: none | Re-fetch a cached OHLCV file older than N days instead of trusting it forever |
| `--no-plots` | flag, default off (charts on) | Skip the `equity_curve.png` chart normally produced in `--mode standard` |

### Sample commands (real market data)

```bash
# Standard mode: full-history evaluation of a generated strategy on a new basket (run from the repo root)
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --mode standard

# Explicit date range and bar interval
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA --start 2018-01-01 --end 2024-12-31 --interval 1d

# Universe loaded from a file (e.g. a basket produced by instrument_selection)
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe-file instrument_selection/results/basket.json --mode standard

# Walkforward mode: rolling-fold consistency check with the default 1y window / 0.5y step
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward

# Walkforward mode with custom window/step sizes
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL GLD TLT --mode walkforward --window-years 2 --step-years 1

# Custom trading-cost assumptions and starting capital
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL --initial-capital 250000 --commission-pct 0.001 --slippage-pct 0.001

# Re-running a mined pattern-based strategy (strategy.json with a pattern_spec block) on a new basket
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT IEF --mode standard

# Custom results/cache directories, no local caching of the fetched data
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --no-cache \
  --results-dir /tmp/backtest_results --cache-dir /tmp/backtest_cache

# CSV-folder provider (offline real data you already downloaded)
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe SPY QQQ TLT GLD --data-provider csv --data-dir /path/to/ohlcv_csvs

# Offline/synthetic data only (no network calls) -- this workspace's standing testing convention
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic

# Standard mode with a baseline symbol comparison (synthetic data -- no network calls)
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic \
  --baseline-symbol SPY --baseline-template equal_weight

# Walkforward mode with a baseline comparison and custom baseline params (synthetic data)
uv run python backtester/run_backtest.py \
  --strategy-file strategy_generator/results/strategy.json \
  --universe A B C --data-provider synthetic --mode walkforward \
  --baseline-symbol SPY --baseline-params '{"rebalance_freq_days": 21}'
```

Outputs land in `results/` (or `--results-dir` if given): `backtest_equity.csv` and
`backtest_weights.csv` (`--mode standard`), or `walkforward_report.csv` (`--mode walkforward`) —
see `SCHEMAS.md` for column-level detail. `--mode walkforward` also always writes
`walkforward_summary.json` (mean fold metrics plus the Deflated Sharpe Ratio). When
`--baseline-symbol` is set: `baseline_equity.csv` and `comparison_report.json` are written in both
modes, and `walkforward_report.csv` gains 5 extra `baseline_*`/`outperformance` columns. In
`--mode standard`, unless `--no-plots` is given, `equity_curve.png` is also written (a two-line
chart, strategy vs. baseline, if `--baseline-symbol` is set).

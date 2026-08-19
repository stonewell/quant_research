# `backtester` — Data Shapes & Schemas

Standalone schema doc, kept separate from `README.md` (which covers setup/usage/CLI arguments) so
schema reference doesn't get lost in the middle of usage instructions. This project consumes the
shared **OHLCV DataFrame**, **universe dict**, **target weights DataFrame**, and **portfolio
backtest result dict** shapes documented in `../common/README.md` (§1–4) — see that file first.

## Input: `--strategy-file` (a `strategy.json`)

Schema owned and documented by `strategy_generator` — see `../strategy_generator/README.md`'s "Data
Shapes & Schemas" section for the full field list, including the `pattern_spec` block this
project's `_get_template()` reads to reconstruct a mined `PatternBasedAllocationTemplate` when
`template_name` starts with `pattern_`. Only `template_name` and `params` are strictly required;
everything else is read via `.get()`.

## Outputs

### `results/backtest_equity.csv` (`--mode standard`)

The shared backtest result dict's `equity_curve` (`../common/README.md` §4), written as-is: one
`equity` column, `DatetimeIndex`.

### `results/backtest_weights.csv` (`--mode standard`)

The shared backtest result dict's `actual_weights` (`../common/README.md` §4) — the DENSE daily
weights actually held (post-drift, post-rebalance), one column per universe symbol.

### `results/walkforward_report.csv` (`--mode walkforward`)

One row per rolling fold. Columns: `start_date`, `end_date` (str, `YYYY-MM-DD`), `sharpe_ratio`,
`cagr`, `max_drawdown`, `calmar_ratio`, `win_rate`, `profit_factor` (all `float`, `NaN` for a fold
where the template produced empty weights or an empty equity curve), `total_turnover` (`float`,
`0.0` on a `NaN` fold), `total_rebalances` (`int`, `0` on a `NaN` fold).

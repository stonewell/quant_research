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

When `--baseline-symbol` is set, 5 extra columns are appended: `baseline_sharpe_ratio`,
`baseline_cagr`, `baseline_max_drawdown`, `baseline_calmar_ratio` (the baseline run's per-fold
metrics of the same name) and `outperformance` (`cagr - baseline_cagr`). These are joined onto the
strategy's fold rows by **`(start_date, end_date)`, not row position** — the strategy and baseline
fold lists come from independently loaded calendars and bar-position arithmetic, so they are not
guaranteed to line up row-for-row. A strategy fold whose `(start_date, end_date)` has no matching
baseline fold gets `NaN` in all 5 columns rather than being dropped.

### `results/baseline_equity.csv` (`--mode standard`, only when `--baseline-symbol` is set)

The baseline run's equity curve (same shape as `backtest_equity.csv` above) — `run_standard`'s
`equity_curve` for the `--baseline-symbol`/`--baseline-template`/`--baseline-params` run. Only
written in `--mode standard`; in `--mode walkforward` the baseline is instead reflected purely via
the `baseline_*`/`outperformance` fold columns in `walkforward_report.csv` and the
`mean_baseline_*` fields in `comparison_report.json` below (there is no single baseline equity
curve to save — the baseline is itself computed fold-by-fold).

### `results/comparison_report.json` (both modes, only when `--baseline-symbol` is set)

Field set differs by mode:

**`--mode standard`:** `baseline_symbol` (str), `baseline_template` (str), `baseline_params`
(dict), `baseline_sharpe_ratio`, `baseline_cagr`, `baseline_max_drawdown`, `baseline_calmar_ratio`,
`strategy_sharpe_ratio`, `strategy_cagr` (all `float`), plus the comparison fields: `overlap_bars`
(`int`, number of overlapping bars between the strategy and baseline equity curves), `alpha`
(`float`, annualized), `beta` (`float`), `tracking_error` (`float`, annualized), `information_ratio`
(`float`), `outperformance_cagr` (`float`, `strategy_cagr - baseline_cagr`). When `overlap_bars < 2`,
`alpha`/`beta`/`tracking_error`/`information_ratio`/`outperformance_cagr` are `null`.

**`--mode walkforward`:** `baseline_symbol` (str), `baseline_template` (str), `baseline_params`
(dict), `mean_baseline_sharpe_ratio`, `mean_baseline_cagr` (means of the merged
`walkforward_report.csv` baseline columns), `mean_outperformance_cagr` (mean of the merged
`outperformance` column).

### `results/walkforward_summary.json` (`--mode walkforward`, always written)

Always written, independent of `--baseline-symbol`. Fields: `mean_sharpe_ratio`, `mean_cagr`,
`mean_max_drawdown`, `mean_calmar_ratio` (means across all folds, `float`), `n_folds` (`int`, total
fold count), `n_valid_folds` (`int`, folds with a non-`NaN` `sharpe_ratio`), `fold_sharpe_std`
(`float`, sample std (`ddof=1`) of the valid folds' Sharpe ratios), `deflated_sharpe_ratio`
(`float`, Bailey & Lopez de Prado's Deflated Sharpe Ratio treating each fold as one of
`n_valid_folds` independent trials). When `n_valid_folds < 2`, `fold_sharpe_std` and
`deflated_sharpe_ratio` are both `null` (a std/DSR isn't computable from fewer than 2 folds).

### `results/equity_curve.png` (`--mode standard`, unless `--no-plots`)

A PNG line chart of the strategy's equity curve (see `common/plotting.py`'s `plot_equity_curve`).
When `--baseline-symbol` is set, a second dashed line for the baseline equity curve is overlaid on
the same chart. Not produced in `--mode walkforward` under any flag combination.

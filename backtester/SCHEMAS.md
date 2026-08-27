# `backtester` — Data Shapes & Schemas

Standalone schema doc, kept separate from `README.md` (which covers setup/usage/CLI arguments) so
schema reference doesn't get lost in the middle of usage instructions. This project consumes the
shared **OHLCV DataFrame**, **universe dict**, **target weights DataFrame**, and **portfolio
backtest result dict** shapes documented in `../common/README.md` (§1–4) — see that file first.

## Input: `--strategy-file` (a `strategy.json`)

Schema owned and documented by `strategy_generator` — see `../pipeline/strategy_generator/README.md`'s "Data
Shapes & Schemas" section for the full field list, including the `pattern_spec` block this
project's `_get_template()` reads to reconstruct a mined `PatternBasedAllocationTemplate` when
`template_name` starts with `pattern_`. Only `template_name` and `params` are strictly required;
everything else is read via `.get()`.

Also supported: a `research_strategy_spec` block (dict or `null`, default `null`), present when a
`research_strategy` strategy (one of the 17 implementations in `../pipeline/research_strategy/rs/strategy.py`)
won `strategy_generator`'s search instead of a static/mined template. Exactly 2 fields: `strategy_key`
(str, a key from `pipeline/research_strategy/strategies_config.json`, e.g. `"permanent_portfolio"`) and
`entry_data` (dict, the exact raw `strategies_config.json[strategy_key]` entry). `_get_template()`
reads this block, when present, to reconstruct the exact strategy instance via
`research_strategy.rs.strategy.instantiate_strategy_from_config_entry(strategy_key, entry_data)` —
this works uniformly for both `type: "class"` and `type: "natural_language"` entries, and for
BOTH `--mode standard` and `--mode walkforward` (the reconstructed instance's `warmup_bars()` is
honored during walk-forward fold buffering exactly like any other template's).

Also supported: a `composite_spec` block (dict or `null`, default `null`), present when
`strategy_generator`'s aspect composition (`--no-compose-aspects` to disable; ON by default — see
`../pipeline/strategy_generator/README.md`) won with a HYBRID pairing across two different source
templates instead of a single whole one. Always has a `track` field, `"allocation"` or `"timing"`,
plus exactly 2 more fields depending on which:
- `track: "allocation"` (basket templates, `common/strategy_aspects.py`): `selection_key` (str,
  e.g. `"momentum_topn"`) + `weighting_key` (str, e.g. `"inverse_vol"`) — looked up in
  `SELECTION_ASPECTS`/`WEIGHTING_ASPECTS` to build a `CompositeAllocationTemplate`.
- `track: "timing"` (single-asset templates, `../pipeline/research_strategy/rs/timing_aspects.py`):
  `entry_key` (str, e.g. `"rsi_oversold_entry"`) + `exit_key` (str, e.g. `"rsi_cross_exit"`) —
  looked up in `ENTRY_SIGNAL_ASPECTS`/`EXIT_RISK_ASPECTS` to build a `CompositeTimingTemplate`.

`_get_template()` also passes the strategy.json's own top-level `params` through as the
reconstructed composite's `default_params`, so `--optimize`'s fresh grid search falls back to the
actually-tuned values instead of each aspect's own generic hardcoded defaults. `_load_strategy_file()`
validates `track` is one of the two allowed values and that the matching pair of keys is present,
raising a `ValueError` naming the missing key(s) otherwise.

Also supported: a `fundamental_spec` block (dict or `null`, default `null`) — a trivial marker,
always exactly `{"source": "fundamental_screener"}`, present when the strategy file was produced by
the separate `fundamental_screener` project (see `../pipeline/fundamental_screener/README.md`)
instead of `strategy_generator`. `_get_template()` uses its mere presence to reconstruct a
zero-arg `FundamentalMarginOfSafetyStrategy`, with all actual behavior coming from the strategy
file's own top-level `params` (already loaded regardless) — the marker only identifies the origin
and triggers the right import.

Also supported: a `bnn_spec` block (dict or `null`, default `null`) — same trivial-marker shape as
`fundamental_spec`, always exactly `{"source": "bnn_forecaster"}`, present when the strategy file
was produced by the separate `bnn_forecaster` project (see `../ml/bnn_forecaster/README.md`).
Reconstructing a `bnn_spec` strategy REQUIRES running `backtester` with `bnn_forecaster`'s own
isolated `uv` environment (its `autobnn`/`jax` dependency chain is not installed in `pipeline`'s
venv) — e.g. `ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py --strategy-file
ml/bnn_forecaster/results/bnn_strategy.json ...` from the repo root.

`pattern_spec`, `research_strategy_spec`, `composite_spec`, `fundamental_spec`, and `bnn_spec` are
ALL mutually exclusive — a winning strategy.json only ever carries one of the five (or none, for a
plain static template). `_load_strategy_file()` enforces this: a `strategy.json` with more than one
of these blocks non-`null` raises a `ValueError` naming the conflict, rather than silently letting
`_get_template()`'s fixed check order quietly ignore all but one.

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
`outperformance` column), `baseline_calendar_mismatch` (bool). The strategy's and baseline's fold
lists are joined on `(start_date, end_date)`, not row position (see `walkforward_report.csv` above);
if BOTH fold lists are non-empty but the join matched zero rows (e.g. because one of the main
`--universe` symbols has a shorter history than `--baseline-symbol`, shifting every fold's aligned
start/end date), every `baseline_*`/`outperformance` value is silently `NaN` end-to-end unless you
know to look — `baseline_calendar_mismatch` is `true` in exactly that degenerate case (and a
console `WARNING:` is also printed), so the report is self-diagnosing instead of just full of
unexplained nulls. `false` in the normal case, including when only *some* folds fail to match.

### `results/walkforward_summary.json` (`--mode walkforward`, always written)

Always written, independent of `--baseline-symbol`. Fields: `mean_sharpe_ratio`, `mean_cagr`,
`mean_max_drawdown`, `mean_calmar_ratio` (means across all folds, `float`), `n_folds` (`int`, total
fold count), `n_valid_folds` (`int`, folds with a non-`NaN` `sharpe_ratio`), `fold_sharpe_std`
(`float`, sample std (`ddof=1`) of the valid folds' Sharpe ratios), `deflated_sharpe_ratio`
(`float`, Bailey & Lopez de Prado's Deflated Sharpe Ratio treating each fold as one of
`n_valid_folds` independent trials). When `n_valid_folds < 2`, `fold_sharpe_std` and
`deflated_sharpe_ratio` are both `null` (a std/DSR isn't computable from fewer than 2 folds).

### `results/optimize_report.json` (only when `--optimize` is set, ALWAYS written -- success or failure)

Written by the shared `common/allocation_search.py` grid-search + Equivalent Random Search (ERS)
mechanism (`optimize_template()`), the same one `strategy_generator` uses internally, applied here
to re-tune the ALREADY-CHOSEN template's `param_grid` on THIS run's universe/mode, instead of
silently trusting the strategy file's original params. Fields:

| Field | Type | Meaning |
|---|---|---|
| `status` | `"success"` \| `"failed"` | `"success"` iff the winning grid-search combination passed ERS validation (`trusted`); `"failed"` otherwise |
| `reason` | str or `null` | `null` on success. On failure: either an ERS-percentile message (`ers_passed` was `False`) or a `total_rebalances`-vs-`--min-rebalances-for-trust` message (`ers_passed` was `True` but the winner didn't rebalance often enough to be trusted) |
| `original_params` | dict | The strategy file's original `params`, unmodified |
| `original_result` | dict | The score_fn result of scoring `original_params` once, for comparison. Shape differs by mode -- see below |
| `best_params` | dict | The winning grid-search combination's params (identical to `original_params` when `template.param_grid` is empty, e.g. a `research_strategy_spec`-sourced template) |
| `best_result` | dict | The score_fn result of scoring `best_params`. Same per-mode shape as `original_result` |
| `ers_percentile` | float | `best_params`'s Sharpe percentile rank against the Equivalent Random Search pool (`0.0`-`1.0`) |
| `ers_passed` | bool | Whether `ers_percentile >= --ers-percentile-threshold` |
| `trusted` | bool | `ers_passed AND best_result["total_rebalances"] >= --min-rebalances-for-trust` -- this is what actually gates whether the final backtest uses `best_params` or falls back to `original_params` |
| `n_trials` | int | Total number of (template, params) combinations scored: grid combinations + `--n-random-search` |
| `improvement` | dict | `{"sharpe_ratio": best - original, "cagr": best - original}` (the `sharpe_ratio`/`cagr` keys of `best_result`/`original_result`) |

`original_result`/`best_result`'s shape differs by mode:

- **`--mode standard`:** the flat `run_standard()` result dict (`common/README.md` §4 shape) --
  `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio`, `win_rate`, `profit_factor`,
  `total_turnover`, `total_rebalances`, plus `equity_curve`/`actual_weights` (DataFrames, rendered
  as their `str()` form in the JSON since a DataFrame isn't natively JSON-serializable -- for the
  actual equity curve/weights use `backtest_equity.csv`/`backtest_weights.csv` instead, which
  always reflect whichever params (`best_params` or the `original_params` fallback) the final run
  actually used).
- **`--mode walkforward`:** `sharpe_ratio`, `cagr`, `max_drawdown`, `calmar_ratio` (each the mean
  across that metric's finite-valued folds, `float`; `sharpe_ratio` is `-inf` if every fold was
  non-finite, the other three are `NaN` in that case), `total_rebalances`/`total_turnover` (summed
  across folds), and `folds` (the full per-fold list `run_walkforward()` returns, same shape as
  `walkforward_report.csv`'s rows). `improvement.cagr` (mean-fold `cagr` of `best_result` minus
  `original_result`) is therefore a real number in `--mode walkforward` too, not always `null`.

Regardless of `status`, the backtest reflected in `backtest_equity.csv`/`backtest_weights.csv`/
`walkforward_report.csv`/everything else this file documents corresponds to `best_params` on
success or `original_params` on failure -- `--optimize` never produces no output, even when tuning
doesn't pass validation. That result is `best_result`/`original_result` above reused directly
(`score_fn(template, params)` already computed it during the grid search/original scoring), not a
second, redundant `run_standard`/`run_walkforward` call.

### `results/equity_curve.png` (`--mode standard`, unless `--no-plots`)

A PNG line chart of the strategy's equity curve (see `common/plotting.py`'s `plot_equity_curve`).
When `--baseline-symbol` is set, a second dashed line for the baseline equity curve is overlaid on
the same chart. Not produced in `--mode walkforward` under any flag combination.

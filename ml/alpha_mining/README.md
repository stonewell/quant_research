# alpha_mining

Two-stage **Genetic Programming (GP) + LightGBM** cross-sectional alpha mining
sub-project (`ML1`), built as a standalone, isolated `uv` project under `ml/`.

- **Stage 1 (GP Formulaic Discovery)**: Strongly-typed expression trees
  (`panel` vs `window ∈ {3, 5, 10, 20, 40, 60}`) reusing operators directly from
  `common/alpha101_operators.py` (plus `sigmoid` and `signed_log`), optimized on
  a multi-objective fitness (`|IC| * sqrt(|IR_daily|)` net of turnover, library
  correlation, and parsimony penalties).
- **Stage 2 (Native LightGBM Combiner)**: Combines discovered factors with
  `lightgbm.train` (`lambdarank` or Gaussian-rank `regression`, fixed boost
  rounds, zero `scikit-learn` dependency) under strict **Purged Group TimeSeries
  CV + Embargo**.
- **Production Guardrail**: **Not wired into `run_pipeline.py`,
  `strategies_config.json`, `common/strategy_spec.py`, `run_screener.py`, or
  `scripts/run_live_strategy.py`.**

## Isolated `uv` Environment

`ml/alpha_mining/` maintains its own `pyproject.toml`, `uv.lock`, and `.venv`
so `lightgbm>=4.3` is **never** installed into `pipeline/.venv`. Shared repo
modules (`common/*`) are reached via the standard repo-root `sys.path`
injection (`common.cli_utils.bootstrap_project_paths`), identical to
`ml/bnn_forecaster/`.

```bash
cd ml/alpha_mining
uv sync                          # one-time setup, creates ml/alpha_mining/.venv
```

Core dependencies: `pandas>=2.0`, `numpy>=1.24`, `scipy>=1.10`, `lightgbm>=4.3`,
`duckdb>=1.1`, `yfinance>=0.2` (dev: `pytest>=7.0`). No `scikit-learn`, `DEAP`,
or `gplearn`.

## Temporal Isolation & 3-Segment Split

To eliminate GP-to-combiner selection leakage, the timeline is strictly
partitioned into three non-overlapping segments separated by `gap = h + embargo`
trading bars (`h = holding_period + exec_lag`, default `5 + 1 = 6`,
`embargo = 5`):

```text
[GP Discovery] -> gap(h + e) -> [Combiner: Purged WF CV -> OOF] -> gap(h + e) -> [Holdout]
```

- **Purged CV (`amine/purged_cv.py`)**: Pure `numpy`/`pandas` (zero ML-library
  imports) with exact closed-interval symmetry:
  - Sample `i` spans closed label window `[i, i + h]`.
  - For contiguous test block `[a, b]`, pre-test training indices
    `i ∈ [a - h, a - 1]` are purged (max allowed pre-test index `a - h - 1`).
  - When `train_side="both"`, post-test indices `i ∈ [b + 1, b + h + e]` are
    purged/embargoed (min allowed post-test index `b + h + e + 1`, strictly
    disjoint from `b + h` even at `e = 0`).
  - Default `train_side="past"` (strictly causal expanding walk-forward).

## Operator Reuse & Module Layout

All 12 core time-series/cross-sectional operators (`ts_sum`, `ts_std`,
`ts_corr`, `ts_cov`, `ts_rank`, `ts_argmax`, `ts_argmin`, `ts_decay_linear`,
`cs_rank`, `cs_demean`, `cs_zscore`, `signed_power`) plus `ts_delay`,
`ts_delta`, `ts_mean`, `ts_min`, `ts_max`, `cs_scale`, `cs_indneutralize`,
`sigmoid`, and `signed_log` are imported directly from
`common/alpha101_operators.py` without reimplementation. Every GP node
immediately maps `±inf -> NaN` and re-applies the tradability availability mask
(`Close` finite) so untradable assets never receive fabricated values.

| File | Responsibility |
| :--- | :--- |
| `amine/config.py` | Validated dataclasses (`AlphaMiningConfig`, `GPConfig`, `PurgedCVConfig`, `CombinerConfig`, `MonitoringConfig`) + `small_data()` profile |
| `amine/labels.py` | Execution-lagged `raw_return` (default, `exec_lag=1`), opt-in `market_residual` / `specific_residual`, optional `sector_neutral` |
| `amine/purged_cv.py` | Pure `numpy`/`pandas` `PurgedGroupTimeSeriesCV`, `assert_no_leakage`, and `split_timeline_three_way` |
| `amine/orthogonalize.py` | Signed cross-sectional Löwdin orthogonalization (`n_valid < m + 2` fallback, `max(λ)*1e-6` floor, `NaN` preservation) |
| `amine/gp.py` | Strongly-typed GP expression trees (`GPNode`, `GPFactorMiner`), deterministic `default_rng`, bloat caps (`depth<=6`, `nodes<=30`) |
| `amine/fitness.py` | Vectorized `fast_rank_ic` (parity with `compute_rank_ic` to `1e-10`), daily IR fitness, turnover/correlation penalties, greedy top-K |
| `amine/combiner.py` | Native `lightgbm.train` (`lambdarank` / `regression`), fixed boost rounds, `run_purged_cv_oof`, `fit_predict_holdout` |
| `amine/monitoring.py` | Report-only v1 diagnostics: Mann-Whitney adversarial validation (`AUC > 0.70`), importance decay, 20d turnover breaker (`> 0.30/day`) |
| `amine/strategy.py` | Standalone `AlphaMiningStrategy(AllocationTemplate)` (`version = "1.0.0"`), **zero `lightgbm` imports**, strict sparse weights |
| `amine/benchmark.py` | 5-arm holdout comparison via `run_allocation_backtest` + `factor_ic_summary` and Rule-9 empirical gate |
| `run_alpha_mining.py` | Offline-default CLI writing `alpha_mining_report.json` and `oof_scores.csv` |

## Standalone `AlphaMiningStrategy` (`version = "1.0.0"`)

`amine/strategy.py` exposes `AlphaMiningStrategy(AllocationTemplate)` with
`version = "1.0.0"` and **zero `lightgbm` imports** (even transitively, thanks
to lazy PEP 562 `__getattr__` in `amine/__init__.py`). It consumes a precomputed
OOF/holdout score panel without in-backtest training, rebalances every `k`
trading bars to equal-weight top-N (`1/M`, sum `<= 1.0`), writes **explicit
`0.0`** for excluded symbols on rebalance rows, and leaves non-rebalance rows
strictly `NaN` (no `ffill`).

## CLI Usage (100% Offline Synthetic Default)

The CLI defaults to `--data-provider synthetic` (zero network access):

```bash
cd ml/alpha_mining

# Fast synthetic smoke run (writes results/alpha_mining_report.json & oof_scores.csv)
uv run python run_alpha_mining.py --data-provider synthetic --small-data

# Custom GP / CV budget and output directory
uv run python run_alpha_mining.py \
  --data-provider synthetic \
  --gp-population 30 \
  --gp-generations 3 \
  --n-splits 3 \
  --output-dir /tmp/am
```

Outputs written to `--output-dir`:
- `alpha_mining_report.json`: timeline boundaries, Stage-1 GP factors & IC
  summaries, Stage-2 OOF RankIC, 5-arm holdout comparison (`lgbm_top_n`,
  `equal_weight_universe`, `best_single_seed`, `equal_weight_gp_combo`,
  `lgbm_lowdin_on`), report-only monitoring diagnostics, and Rule-9 gate status.
- `oof_scores.csv`: date × symbol out-of-fold combiner score panel.

## Offline Tests

All unit tests run 100% offline on synthetic data (no network, no `tests/__init__.py`):

```bash
cd ml/alpha_mining
uv run pytest tests -v -m "not slow"
```

Verify that `lightgbm` remains strictly absent from `pipeline/.venv`:

```bash
cd pipeline
uv run python -c "import importlib.util as u, sys; sys.exit(u.find_spec('lightgbm') is not None)"
```

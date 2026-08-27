# bnn_forecaster

Probabilistic price forecasting via [AutoBNN](https://research.google/blog/autobnn-probabilistic-time-series-forecasting-with-compositional-bayesian-neural-networks/)
(Google Research) -- a compositional Bayesian Neural Network that produces a
calibrated median forecast plus a confidence interval, built from
interpretable components (trend, periodic, changepoint), instead of a
black-box net.

This is the **third** of this workspace's "beat the benchmark" strategy
family, alongside `research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy`
(price-only technical proxy) and `fundamental_screener` (real ROE/dividend/
earnings-growth). All three hold a candidate only while its own expected
return clears both a confidence/quality gate and the benchmark's own
expected return, and sell the moment that edge decays -- here, the "expected
return" is a genuine probabilistic forecast instead of a proxy or a
fundamental-data formula.

**Not wired into `run_pipeline.py`.**

## Why this project has its own isolated `uv` environment

AutoBNN needs JAX + TensorFlow Probability (+ flax/optax/chex/bayeux-ml
transitively) -- a first-of-its-kind, heavy dependency for a workspace whose
`pipeline/pyproject.toml` is otherwise just pandas/numpy/scipy/yfinance/
matplotlib. Worse, `tensorflow-probability==0.25.0` (the newest version
resolvable on this workspace's package index) has NOT kept pace with jax's
and numpy's own fast-moving internal APIs, so it needs both pinned well
below their own latest releases:

```toml
"numpy>=1.24,<2.1"   # TFP's bundled bijector code still calls np.reshape(..., newshape=...), removed in newer numpy
"jax<0.4.30"         # TFP's JAX substrate calls jax.interpreters.xla.pytype_aval_mappings, removed in newer jax
"jaxlib<0.4.30"      # jaxlib must track jax's own version closely or jax refuses to import
```

Forcing the ROOT workspace to accept these constraints would risk breaking
every other project's own numpy/scipy versions. Instead, `bnn_forecaster` is
its own **standalone `uv` project** (`bnn_forecaster/pyproject.toml`, its own
`.venv`, its own `uv.lock`) -- `uv sync` from *within* this directory creates
an isolated environment with both the pinned AutoBNN stack and a working copy
of this workspace's common runtime deps (pandas/numpy/scipy/yfinance/
matplotlib), so `common/*` (reached the same way every project reaches it --
plain `sys.path` injection, not an installed package) and `backtester/*` both
import and run correctly from inside it -- verified directly: `backtester`'s
own full test suite passes when run with `bnn_forecaster/.venv`'s python.

```bash
cd ml/bnn_forecaster
uv sync                          # one-time setup, creates ml/bnn_forecaster/.venv
uv run python run_bnn_forecaster.py --data-provider synthetic
```

To backtest a winning `bnn_strategy.json` (see below), run `backtester`
**using this project's own venv**, not `pipeline/`'s:

```bash
# from the repo root
ml/bnn_forecaster/.venv/Scripts/python.exe backtester/run_backtest.py \
  --strategy-file ml/bnn_forecaster/results/bnn_strategy.json --universe KO PG SPY BIL
```

## What it does

Given a universe, fits a BNN per candidate symbol (and the benchmark) on
trailing OHLCV price history, and ranks:
- **Top-N buy candidates**: the forecast clears a required-return hurdle
  AND the confidence interval is narrow enough to trust.
- **Top-N sell candidates**: the forecast has decayed below the benchmark's
  own forecast, OR confidence has degraded.

**Overlap resolution**: a symbol can never appear on both lists -- sell
always takes precedence over buy (same rule, same rationale, as
`fundamental_screener`'s `evaluate_buy_sell`: a capital-preservation trigger
always outranks a return signal). See `bnnf/rules.py`.

Unlike `fundamental_screener`, this project needs **no network access** for
its actual signal -- AutoBNN fits on OHLCV price history, which
`--data-provider synthetic` already supplies offline. The cost here is CPU
time (a BNN fit per symbol), not a live API call.

## ⚠️ Calibration is NOT verified -- read before trusting any output

This project ships the **mechanism** (fit → forecast → buy/sell rules →
backtester integration) working end-to-end, verified directly. It does
**not** ship a forecast verified to be well-calibrated for financial return
series. Two things were discovered empirically while building this:

1. **Standardization matters.** AutoBNN's default priors assume roughly
   unit-variance training data (like most GP/BNN libraries). Raw log-price
   (e.g. ~4.5-5.0, with a tiny variance) is badly scale-mismatched to that
   assumption and produces confidence intervals off by orders of magnitude
   without standardizing the training target first. `bnnf/forecasting.py`
   does this (standardize before fit, invert after predict) -- a real,
   worthwhile fix, kept.
2. **Even after standardizing, `ci_width` came out routinely in the
   hundreds-to-thousands-of-percent (annualized) range** with the default
   `sum_of_stumps` model structure and `normal_likelihood_logistic_noise`
   likelihood, using both this project's own reduced default settings
   (`width=10, num_iters=1000`) AND AutoBNN's own full recommended defaults
   (`width=50, num_iters=5000`) -- nowhere near usable for a `--max-ci-width`
   gate expressed in normal return-magnitude terms (e.g. `0.30` = 30%)
   without further empirical tuning: trying other `model_or_name`/
   `likelihood_model` combinations, more particles, or MCMC/VI instead of
   MAP. That tuning is a genuine research task, not something this session
   could respons­ibly guess default numbers for.

**Before trusting `--required-return`/`--max-ci-width` for anything**,
inspect a real run's actual `ci_width` output (`bnn_forecast_report.json`)
and set thresholds that match what you actually observe.

## Usage

### Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the pipeline projects — see `common/README.md`'s
cross-reference index; `resolve_universe_from_args` picks the first one supplied, in that order,
falling back to this project's own 8-symbol `DEFAULT_CANDIDATE_UNIVERSE` (KO, PG, JNJ, MSFT, COST,
WMT, MCD, PEP) if none are given.

| Flag | Type / default | Meaning |
|---|---|---|
| `--universe` / `-u` | space-separated tickers, default: none | Explicit ticker list (falls back to `DEFAULT_CANDIDATE_UNIVERSE`) |
| `--universe-file` | path, default: none | Load tickers from a file instead |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--benchmark` | str, default `"SPY"` | Broad-index benchmark symbol for the sell-trigger comparator |
| `--top-n` | int, default `5` | How many top buy/sell candidates to report |
| `--horizon-days` | int, default `21` (~1 month) | Forecast horizon in trading days, also the annualization base |
| `--lookback-days` | int, default `756` (~3 years) | Trading days of trailing history to fit each symbol's BNN on |
| `--estimator` | `map` \| `mcmc` \| `vi`, default `"map"` | AutoBNN estimator type — `map` (Maximum A Posteriori) is by far the cheapest; `mcmc`/`vi` are much more expensive |
| `--width` | int, default `10` | AutoBNN ensemble width (reduced from AutoBNN's own default of 50 for practical runtime) |
| `--num-iters` | int, default `1000` | MAP optimization iterations per symbol (reduced from AutoBNN's own default of 5000 — see the calibration trade-off above) |
| `--num-particles` | int, default `4` | AutoBNN particle count |
| `--required-return` | float, default `0.10` | Annualized median-forecast buy hurdle |
| `--max-ci-width` | float, default `0.30` | 97.5/2.5 quantile spread ceiling (annualized) to count a forecast as "confident" — see the calibration caveat above before trusting this default |
| `--seed` | int, default `42` | Random seed |
| `--start` | `YYYY-MM-DD`, default `"2015-01-01"` | History start date |
| `--end` | `YYYY-MM-DD`, default `"2024-12-31"` | History end date |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider |
| `--data-provider` | str, default `"synthetic"` | `synthetic`, `yfinance`, `csv`, or a custom module specifier |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of OHLCV history |
| `--cache-ttl-days` | float, default: none | Maximum age (in days) of a cached OHLCV file before it's treated as stale and re-fetched |

### Sample commands

```bash
uv run python run_bnn_forecaster.py --data-provider synthetic
uv run python run_bnn_forecaster.py --universe KO PG JNJ MSFT COST WMT MCD PEP --data-provider yfinance
uv run python run_bnn_forecaster.py --num-iters 200 --width 6   # faster, lower-quality fit for quick iteration

# Shorter forecast horizon and trailing lookback
uv run python run_bnn_forecaster.py --horizon-days 5 --lookback-days 252 --data-provider synthetic

# More expensive estimator + more particles (slower, potentially better-calibrated -- unverified)
uv run python run_bnn_forecaster.py --estimator vi --num-particles 8 --data-provider synthetic

# Universe loaded from a file (e.g. a basket produced by instrument_selection)
uv run python run_bnn_forecaster.py --universe-file ../../pipeline/instrument_selection/results/basket.json \
  --data-provider yfinance

# Tune the buy/sell thresholds after inspecting a real run's actual ci_width output (see caveat above)
uv run python run_bnn_forecaster.py --required-return 0.15 --max-ci-width 1.5 --data-provider yfinance

# Re-fetch a stale cached OHLCV file after 1 day instead of trusting it forever
uv run python run_bnn_forecaster.py --data-provider yfinance --cache-ttl-days 1
```

### Outputs (`results/`)

- **`bnn_forecast_report.json`** -- `run_context`, `n_universe_evaluated`,
  `top_buy`/`top_sell` (symbol, expected_return, ci_width, confident,
  buy_flag/sell_flag), `caveat`.
- **`bnn_strategy.json`** -- a `strategy.json`-compatible artifact
  (`template_name: "bnn_forecast"`, `params` = resolved `ForecasterConfig`,
  `bnn_spec: {"source": "bnn_forecaster"}`) for manual
  `backtester --strategy-file` use (see above for the venv caveat).

## Testing

```bash
uv run pytest tests -v              # fast (mocked fit_forecast), ~2s
uv run pytest tests -v -m slow      # includes one real (unmocked) AutoBNN fit, ~15s
```

Most tests mock `fit_forecast` with controlled `forecast_return`/`ci_width`
values -- a real fit costs several seconds even at the smallest practical
settings (JAX JIT compilation dominates), and calibration quality is the
separate, disclosed-as-unverified concern above, not something unit tests
for the buy/sell/aggregation *mechanics* should depend on. One `@pytest.mark.slow`
test exercises the real fit/predict path end-to-end, checking only
shape/no-crash.

## Layout

```
bnn_forecaster/
├── pyproject.toml       # own isolated uv project -- see "Why this project has its own..." above
├── bnnf/
│   ├── config.py        # ForecasterConfig
│   ├── forecasting.py   # fit_forecast() -- wraps autobnn's estimators, standardizes/un-standardizes
│   ├── rules.py         # pure buy/sell rule evaluation (current-snapshot shape, mirrors
│   │                      fundamental_screener/fscreen/rules.py)
│   └── strategy.py      # BnnForecastStrategy(AllocationTemplate) -- fit-once-cache-on-instance,
│                          then predict across the whole date range for a time-varying daily signal
├── run_bnn_forecaster.py
├── tests/
└── README.md
```

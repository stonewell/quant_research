# Turning-Point Indicator Pattern Mining (`pattern_mining`)

A dedicated pipeline stage that mines a universe's aggregate portfolio price history for
statistically significant technical-indicator patterns preceding major turning points, and writes a
durable, standalone `pattern_report.json` for `strategy_generator`'s `--pattern-report` flag to
consume. Extracted from `strategy_generator`'s former in-process `--mine-patterns` flag so mining
results are durable and reusable across multiple `strategy_generator` runs/parameter sweeps without
re-running this (Bonferroni-corrected, shuffle-null) mining pass every time, and so EVERY significant
finding is reported — not just whichever ones happened to be turned into templates and happened to
win a single generation run.

---

## 1. What it does

Given a universe (e.g. exported from `instrument_selection`), it:

1. Builds an equal-weight aggregate portfolio curve (`common.allocation_templates.build_aggregate_curve`).
2. Detects the curve's major peaks/troughs with a percentage-based zigzag filter (`pmine/turning_points.py`).
3. Tests whether any indicator in a broad "popular technical indicators" menu
   (`common/indicator_features.py`: RSI, SMA-relative position, ROC, ATR%, ADX, Bollinger %B,
   Stochastic %K, MACD histogram, CCI, Williams %R) reads significantly differently
   `--pattern-lag-bars` trading days (default 20) BEFORE those turning points than before a random
   date (`pmine/pattern_mining.py`), via a Bonferroni-corrected shuffle-null significance test.
4. Writes every tested (feature, event_type) combination's result — significant or not — to
   `results/pattern_report.json`.

`strategy_generator/run_strategygen.py --pattern-report <path>` loads that file, turns the
significant findings into `PatternBasedAllocationTemplate` (`common/allocation_templates.py`)
candidates (up to `--pattern-max-templates`), and folds them into the SAME grid-search + Equivalent
Random Search pipeline as every static template — a mined candidate must still clear the same ERS
bar to be trusted; the mining significance test alone is never sufficient.

## 2. Why the lag matters — the single most important methodological decision here

Reading an indicator EXACTLY AT a zigzag-confirmed turning point is nearly tautological, not a
discovery. A zigzag peak is, by construction, a local price maximum reached via a recent run-up; a
momentum-style indicator computed AT that exact bar is measuring THE SAME run-up the label is built
from. An early version of this feature tested at lag=0 and found ~80% of the whole indicator menu
"significant" on a **pure random-walk universe with zero real structure** — proof the lag=0 question
is nearly definitionally true regardless of the data, not a real finding. Reading the indicator
`--pattern-lag-bars` days BEFORE the turning point asks a genuinely different, actionable question
instead: "did this indicator already look unusual before the reversal happened, in a way you could
have observed and acted on in real time."

**Honest residual limitation (measured, not assumed)**: even at the default 20-bar lag, repeated
pure-random-walk negative-control runs still occasionally flag a handful of the menu "significant" —
far fewer than at lag=0-10 (which flagged roughly half the menu), but not a clean, reliable zero.
Some mechanical/tautological correlation between momentum-style indicators and momentum-defined
turning points survives the lag adjustment. This is exactly why the mining significance test
(Bonferroni-corrected across the whole menu, since it tests dozens of indicator/lookback
combinations) is treated as a candidate-generation FILTER, not proof of a real edge: every mined
candidate must ALSO clear `strategy_generator`'s Equivalent Random Search bar, which tests actual
backtested performance against random portfolios, not just "does this statistic look unusual."

**Confirmation-lag / hindsight caveat (a separate issue from the tautology above)**: labeling a
historical date a "turning point" at all requires a few bars of hindsight past the turning point
itself — legitimate for this research/mining pass (the same hindsight `common/metrics.py`'s
`max_drawdown` already uses elsewhere in this workspace), but the resulting **live trading template
has no such lag**: it only ever compares a live, already-known indicator reading against the mined
threshold, never trying to detect a turning point in real time.

**Expected outcome on synthetic data**: per this workspace's own repeated finding elsewhere
(`instrument_selection`'s Hurst/momentum/candlestick significance tests, `strategy_generator`'s own
ERS checks), most series show no significant structure. Finding 0 significant patterns is the
common, correct result on synthetic GBM-like data, not a bug.

## 3. Usage

```bash
uv run python pattern_mining/run_pattern_mining.py \
  --universe-file instrument_selection/results/basket.json --data-provider synthetic
```

### Argument reference

Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the other projects — see `common/README.md`'s cross-reference
index; `resolve_universe_from_args` picks the first one supplied, in that order, falling back to
this project's own default universe (`["SPY", "QQQ"]`) if none are given.

| Flag | Type / default | Meaning |
|---|---|---|
| `--universe` / `-u` | space-separated tickers, default: none | Explicit ticker list (falls back to `["SPY", "QQQ"]`) |
| `--universe-file` | path, default: none | Load tickers from a file instead (e.g. `instrument_selection/results/basket.json`) |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--start` | `YYYY-MM-DD`, default `"2015-01-01"` | History start date |
| `--end` | `YYYY-MM-DD`, default `"2024-12-31"` | History end date |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider |
| `--pattern-min-swing-pct` | float, default `0.05` | Minimum zigzag swing size to confirm a turning point (0.05 = 5%) |
| `--pattern-lag-bars` | int, default `20` | How many trading days before each turning point to read indicators at (see §2) |
| `--data-provider` | str, default `"yfinance"` | `yfinance`, `csv`, `synthetic`, or a custom module specifier |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data |
| `--cache-ttl-days` | float, default: none | Maximum age (in days) of a cached OHLCV file before it's treated as stale and re-fetched |

The local OHLCV cache directory resolves to the shared, workspace-wide location (`<repo_root>/data/`)
— see `common/README.md`'s "Shared OHLCV cache directory" section.

## 4. Output: `results/pattern_report.json`

| Field | Type | Notes |
|---|---|---|
| `run_context` | dict | `data_provider`, `universe` (resolved ticker list), `start`, `end`, `min_swing_pct`, `lag_bars` |
| `status` | str | `"ok"`, `"insufficient_data"` (fewer than ~200 aligned bars), or `"insufficient_turning_points"` (fewer than ~20 confirmed peaks+troughs) |
| `findings` | list of dicts | One entry per (feature, event_type) tested (empty in the two degenerate `status` cases, and commonly empty even when `status="ok"` — see §2's "expected outcome"); see schema below |

Each `findings` entry:

| Field | Type | Notes |
|---|---|---|
| `feature` | str | Indicator name, e.g. `"rsi"`, `"adx"` |
| `lookback` | int or 3-int list | e.g. `14`, or `[12, 26, 9]` for `macd_hist` |
| `event_type` | str | `"peak"` or `"trough"` |
| `observed_stat` | float | Mean indicator reading `lag_bars` before real turning points |
| `null_mean` | float | Mean indicator reading before random (non-turning-point) dates |
| `p_value` | float | From the Bonferroni-corrected shuffle-null test |
| `adjusted_alpha` | float | `0.05 / n_tests` |
| `significant` | bool | `p_value < adjusted_alpha` |
| `comparison` | str | `"below"`/`"above"` — whether `observed_stat` sits below or above `null_mean` |
| `threshold` | float | Median observed value — becomes a `PatternBasedAllocationTemplate`'s mined threshold |
| `n_events` | int | Number of turning points contributing to `observed_stat` |

`strategy_generator/run_strategygen.py`'s `--pattern-report` loads this file, reconstructs the
`findings` list back into a DataFrame (`pd.DataFrame(data["findings"])`), and calls
`pmine.pattern_mining.build_pattern_templates(findings, max_templates=...)` — the exact same
function a live mining pass would call, so the JSON hand-off is lossless (see
`tests/test_run_pattern_mining.py`'s round-trip test).

## 5. Project layout

```
pattern_mining/
  pmine/
    __init__.py         Bootstrap: makes ../../common/ importable
    turning_points.py    Percentage-based zigzag peak/trough detector
    pattern_mining.py    Aggregate-curve turning-point detection -> indicator feature menu ->
                          Bonferroni-corrected shuffle-null significance test ->
                          PatternBasedAllocationTemplate candidates
  run_pattern_mining.py  CLI entry point, writes results/pattern_report.json
  tests/                 pytest, synthetic data only
  results/               gitignored
```

The actual `PatternBasedAllocationTemplate` implementation and `build_aggregate_curve` (and the
shared `factor_tags`/factor-taxonomy/indicator-feature machinery this stage mines against) live in
`../../common/allocation_templates.py`, `../../common/factor_taxonomy.py`, and
`../../common/indicator_features.py` — this project imports and mines them, it doesn't define them.

## 6. Testing

```bash
# from the repo root
uv run pytest pattern_mining/tests -v
```

Synthetic data only, covering: the zigzag turning-point detector (known peak/trough locations,
small-wiggle filtering, end-of-series repainting exclusion), `pattern_mining.py`'s significance test
with BOTH a positive control (a deliberately planted indicator/turning-point relationship must be
flagged significant) and a negative control (a pure random-walk universe must show meaningfully
fewer significant findings after the lag adjustment than before it — a comparative check, not a
fragile exact-zero assertion), the CLI's `pattern_report.json` output shape, and a round-trip test
proving `build_pattern_templates` reconstructs byte-for-byte-equivalent templates whether fed a live
mining result or one reloaded from JSON.

## 7. Known limitations

- Not validated against real market data this session — synthetic data only, per this workspace's
  standing testing convention.
- The significance test has a measured residual false-positive rate even after the lag adjustment
  (see §2) — treat a finding's significance as a filter that earns it a shot at
  `strategy_generator`'s ERS/backtest gate, not as evidence on its own.
- Only ONE fixed lag (`--pattern-lag-bars`) is tested per run, not a range — a deliberate choice to
  avoid adding another dimension to the already-corrected multiple-comparisons menu, at the cost of
  not knowing whether a different lag would have found something this run didn't.
- `pattern_report.json` reports ALL tested findings (not capped) — `--pattern-max-templates` (on
  `strategy_generator`'s side) decides how many of the significant ones actually compete as
  candidate templates for a given generation run.

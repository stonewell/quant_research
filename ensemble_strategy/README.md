# Regime-Switching Ensemble Strategy (Broad ETFs)

A long-only backtester that combines trend-following, tactical RSI(2)
mean-reversion, and a cash regime into a single regime-switching system for
broad market ETFs (SPY, QQQ) — and, critically, decomposes the combination
into its standalone parts so the combination can be judged on evidence
rather than assumed to help. Backtesting only — no order placement.

**Headline finding, stated up front:** the specific combination method built
here (ADX-gated routing between a trend-following sleeve and a tactical
RSI(2) sleeve) **does not beat simply using the trend-following sleeve
alone** across every window tested. This is the honest result of the
experiment, not a caveat buried at the end — see "What the backtest actually
found" below before using any part of this code for real decisions.

## Strategy summary

Three regimes, each computed from data available before the bar being
traded (no lookahead — see `regime.py`):

- **Trend** (price above a rising 200-day SMA AND ADX(14) >= 25): the
  trend-following sleeve takes over — stay fully invested, no tactical
  trading. This exists specifically to fix a weakness found in this
  workspace's separate `swing_trend_strategy` project: any strategy with a
  capped holding period cannot capture a long secular bull run.
- **Range** (price above the 200-day SMA but ADX(14) <= 20): the tactical
  RSI(2) mean-reversion sleeve takes over — same entry/exit thresholds as
  this workspace's `rsi_strategy` project (buy RSI(2)<10, sell RSI(2)>70).
  Between the two ADX thresholds, the previous sub-regime is carried forward
  (hysteresis) specifically to reduce whipsaw at ambiguous trend-strength readings.
- **Downtrend** (price below the 200-day SMA): cash, regardless of ADX.

`run_backtest.py` always runs three variants side by side — `ensemble`
(all three regimes), `trend_only` (trend-following sleeve alone, ignoring
ADX/RSI entirely), and `meanrev_only` (the original RSI(2) strategy, active
whenever price is above the 200-day SMA) — plus buy-and-hold, so the
combination is directly compared against each of its parts.

## Research grounding

A multi-source, adversarially-verified research pass produced a clear and
somewhat sobering meta-finding: **the rationale for combining trend-following
and mean-reversion is well-supported qualitatively, but no verified
quantitative backtest of such a combination on SPY/QQQ survived fact-checking.**
Every specific blog-reported CAGR/Sharpe/drawdown number for a combined
system was explicitly refuted during adversarial verification. This means
the "build the combination and see what happens" step of this project isn't
reproducing a validated result — it's generating the missing evidence.

**What is well-supported:**
- Trend-following and mean-reversion are documented as complementary because
  markets exhibit both short-term momentum and medium-term mean reversion,
  each style tending to dominate in different regimes.
- Trend-following has a convex, "option-straddle"-like payoff — its biggest
  gains come during extreme moves in either direction. This showed up
  concretely in 2022: as stock-bond correlation broke down, a trend-following
  benchmark (SG Trend Index) gained ~+27% while the S&P 500 and 10-year
  Treasuries both fell and a mean-reversion/short-vol proxy (Cboe PutWrite)
  lost ~-7.7%. (This evidence is from multi-asset futures trend-following,
  not an equity-ETF-only study — it supports the *rationale*, not a specific
  equity-only Sharpe number.)
- Concrete regime-detection rules are documented in practice: a 200-day SMA
  trend gate, ADX >= ~25 signaling a trending regime, ADX <= ~20 signaling a
  ranging regime. Their *definitions* are well-documented; their *efficacy*
  is not — the one source proposing this exact binary AND-gate made an
  unverified "+100% performance" claim that failed adversarial verification.
- The critical methodological pitfall for any regime-switching backtest is
  look-ahead bias — fitting/deciding a regime using data not yet available
  at that point in time inflates apparent performance. This implementation
  shifts every regime/indicator value by one bar (see `regime.py`) so a
  bar's decision only ever uses data through the prior bar's close.

## What the backtest actually found

Four SPY windows, each showing total return / CAGR / Sharpe / max drawdown
for all three modes plus buy-and-hold:

| Period | Buy&Hold return | Ensemble | Trend-only | RSI(2)-only |
|---|---|---|---|---|
| 2000-2024 | +534.6% | +180.3% (Sharpe 0.54, DD 28.5%) | **+328.2%** (Sharpe 0.60, DD 24.9%) | +53.0% (Sharpe 0.34, DD 19.3%) |
| 2010-2024 | +583.8% | +119.5% (Sharpe 0.65, DD 28.6%) | **+261.2%** (Sharpe 0.83, DD 20.4%) | +24.6% (Sharpe 0.28, DD 19.3%) |
| 2015-2024 | +240.8% | +45.0% (Sharpe 0.47, DD 28.6%) | **+120.1%** (Sharpe 0.78, DD 20.4%) | +5.5% (Sharpe 0.12, DD 19.3%) |
| 2005-2015 | +113.9% | +60.2% (Sharpe **0.58**, DD **17.8%**) | +64.2% (Sharpe 0.49, DD 24.9%) | +33.5% (Sharpe 0.52, DD 11.2%) |

**The trend-following sleeve alone wins on total return in every single
window, usually by a wide margin, and wins on Sharpe in 3 of 4 windows.**
The ensemble is worse than trend-only on almost every metric except in the
2005-2015 window, where it trades some return for a better Sharpe and a
much shallower drawdown (17.8% vs 24.9%).

**Why the combination underperforms, diagnosed from the data (not assumed):**
during "range" regime bars (ADX <= 20), the trend-only sleeve is invested
essentially all the time, while the standalone RSI(2) sleeve is only
invested ~19% of those same bars (it only buys brief oversold dips). ADX <=
20 does not mean "the market is flat" — it usually just means low
directional *strength*, and a large fraction of those bars are still quietly
drifting upward. Routing capital away from a buy-and-hold-style exposure
during "range" regime and replacing it with a tactical dip-buying strategy
throws away most of that drift, and the occasional-oversold-dip entries
don't make up the difference. This is a concrete, diagnosed failure mode of
ADX-based regime routing for long-only equity index exposure — not a general
statement that trend+mean-reversion combination never works (the futures
evidence above suggests it can, in a different context), just that *this*
specific combination method, on *this* asset class, does not.

**Practical takeaway:** if forced to choose one of the three approaches
tested here for a broad ETF, the simple trend-following sleeve (invested
whenever price is above a rising 200-day SMA, cash otherwise — no RSI, no
ADX) was the best performer on both absolute and (mostly) risk-adjusted
terms in this research. The ensemble's only real selling point is a
smoother ride in some windows at a real cost to total return — a legitimate
choice only if you weight drawdown smoothness far above absolute return, not
a strict improvement.

## Project layout

Shared code (the yfinance loader, standard indicators, and standard
performance metrics) lives one level up in `../common/` and is used by every
project in this workspace. Each module here re-exports the shared functions
it needs and keeps only project-specific logic local, so the public API
(`ensemblebot.data.load_ohlcv`, `ensemblebot.metrics.sharpe_ratio`, etc.) is
unchanged for callers.

```
ensemble_strategy/
  ensemblebot/
    config.py        EnsembleConfig — mode selects ensemble/trend_only/meanrev_only
    data.py           Thin wrapper over ../common/data.py, pinned to this project's data/ dir
    indicators.py     rsi/sma/adx re-exported from ../common/indicators.py
    regime.py         Regime classification with hysteresis and no-lookahead shifting
    backtester.py     Event loop: binary exposure model, mode-dependent desired-exposure logic
    metrics.py        summarize() (local) + base metrics re-exported from ../common/metrics.py
    plotting.py       Price with regime shading, equity-curve comparison across all modes
  run_backtest.py      CLI — always runs all 3 modes + buy-and-hold side by side
  tests/                pytest unit + integration tests
  data/                 cached price CSVs (gitignored)
  results/              trade logs, equity curves, charts (gitignored)
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r ../requirements.txt
```

## Usage

```bash
python run_backtest.py --symbol SPY --start 2000-01-01 --end 2024-12-31
```

Key options (see `python run_backtest.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--trend-ma-period` | Long-term trend gate (default 200-day SMA) |
| `--adx-period`, `--adx-trend-threshold`, `--adx-range-threshold` | ADX sub-regime classification and hysteresis band |
| `--rsi-period`, `--entry-rsi-threshold`, `--exit-rsi-threshold` | Tactical RSI(2) mean-reversion sleeve parameters |

Outputs land in `results/`: per-mode trade logs and equity curves,
`price_and_regime.png` (regime shading over price), `equity_curve.png`
(all three modes vs buy-and-hold).

## Testing

```bash
python -m pytest tests/ -v
```

Covers: RSI/SMA/ADX correctness (ADX reads low in choppy/flat markets, high
in strong sustained trends), the hysteresis mechanism in isolation, the
long-term downtrend gate overriding ADX even when the decline itself reads
as "trending," a dedicated no-lookahead regression test (regime values up to
a cutoff point are proven identical whether or not future data exists), and
integration tests across all three modes (cash never negative, downtrend
regime forces a flat position, `trend_only` stays invested through the
"range" window while `meanrev_only` doesn't, `meanrev_only` trades more
often than `trend_only`).

## Known limitations

- Daily bars only; no intraday data or costs modeled beyond commission/slippage.
- Single-asset, single-position, binary (0%/100%) exposure — no blended or
  volatility-targeted position sizing across the sub-strategies, which
  research flagged as an alternative combination method with no more
  verified evidence than the regime-routing approach tested here.
- This tested one specific, well-motivated regime-routing design. It does
  not prove trend+mean-reversion combination can never work for equity
  ETFs — only that this implementation, on SPY, across four windows, did not
  beat its simpler trend-following component. A different combination
  method (e.g., an additive risk overlay on top of a trend-following
  default, rather than full regime routing) might perform differently and
  was not tested here, per the project's time budget.

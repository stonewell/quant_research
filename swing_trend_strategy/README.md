# Trend-Pullback Swing Strategy (Long-Only, Max ~3-Month Hold)

A long-only backtester for a "buy the dip in an uptrend" swing strategy,
built to the user's spec: long only, never held longer than ~3 months
(63 trading days), targeting a high win rate and a positive return that
beats buy-and-hold — always reported against **two** baselines: the traded
symbol's own buy-and-hold, and buy-and-hold of a benchmark symbol (SPY by
default). Backtesting only — no order placement, no live/paper trading.

## Strategy summary

- **Entry** (the one verified, reproducible rule set found in research):
  price above a rising 200-day SMA (uptrend), price below its 20-day SMA
  (temporary pullback), and a 5-period RSI below 45.
- **Exit** — four independent mechanisms, whichever triggers first:
  1. Stop-loss (default 5%, checked intrabar).
  2. Profit target at a 3:1 reward:risk ratio (default +15%, checked intrabar).
  3. Trailing stop (activates once up 7%, then trails 4% behind the peak —
     tighter than the activation gain so it always locks in some profit once live).
  4. Hard time cap: forced exit after `max_holding_days` (default 63 ≈ 3 months).
- **Position sizing**: defaults to a flat 100% of equity (`sizing_mode="equity_pct"`)
  so the strategy's exposure is directly comparable to a fully-invested
  buy-and-hold baseline. A `sizing_mode="risk_based"` option (risk 1% of
  equity per trade, sized off the stop distance) is also implemented — it's
  the safer, research-backed choice for real trading, but it under-allocates
  relative to buy-and-hold and isn't the right choice if your goal is
  matching buy-and-hold's exposure for a fair comparison.
- Single position at a time, next-bar-open execution for signal-based
  entries (no lookahead), intrabar checks for stop/target/trailing exits,
  mark-to-market equity every bar, commissions + slippage on every fill.

## Research grounding — and an important honesty note

A multi-source, adversarially-verified research pass produced one clear,
load-bearing finding: **among long-only strategies with short-to-medium
holding periods, only pullback/mean-reversion-in-an-uptrend approaches are
documented as high-win-rate (70-82%)**. Pure trend-following/breakout
systems are consistently documented — in both academic and practitioner
sources — as LOW win rate (30-40%), relying on rare large winners; they are
structurally incompatible with a "high win rate" goal, so that entire
strategy family was ruled out before writing any code.

The one specific, verified backtest of this exact rule set (close > 200-day
SMA, close < 20-day SMA, RSI(5) < 45 entry; RSI(5) > 65 exit) reports an
**82% win rate and 8.3% CAGR on SPY** — but the same source notes this
**underperforms SPY buy-and-hold on raw CAGR**; its documented edge is lower
drawdown and less time-in-market, not higher absolute return. Research could
not find any verified source demonstrating "high win rate AND beats
buy-and-hold" simultaneously for this or any long-only, ≤3-month-holding
strategy family. Take that as the honest starting expectation.

**What this implementation does differently, and why:** the verified exit
rule (RSI back above 65) locks in very small average wins (~1-2%), which is
exactly why the documented version underperforms buy-and-hold — it exits
winners almost immediately. This code raises the RSI exit threshold to 90
(so it rarely fires) and instead lets a trailing stop, a 3:1 profit target,
and the 3-month time cap manage exits — letting winners run further. This is
a **disclosed, empirically-tuned design choice**, not a verified finding.
It was checked for a parameter "plateau" (nearby stop/target/trailing values
give qualitatively similar results — see the sweep below) across two very
different SPY windows rather than fit to one lucky sample, per the
curve-fitting safeguards the research surfaced.

### What the tuning actually found (SPY, `equity_pct` sizing, default parameters)

| Period | Strategy return | Strategy win rate | Strategy max DD | Strategy Sharpe | Buy & hold return | Buy & hold max DD |
|---|---|---|---|---|---|---|
| 2010-01 – 2024-12 (exceptional secular bull run) | +250% | 68.4% | 15.6% | 0.90 | +546% | 33.7% |
| 2000-01 – 2014-12 (dot-com crash + 2008 crash) | **+92%** | 64.1% | 16.7% | 0.57 | +88% | ~55%+ |

**Read this the same way as the sibling grid-trading and RSI-2 projects in
this workspace**: this strategy does not reliably beat buy-and-hold in a
strong, sustained bull market (2010-2024 SPY was one of the best 15-year
windows in market history) — nothing with a 3-month position cap can out-
compound an index that never has a real drawdown to buy into. But across a
stress-heavy window with two major crashes (2000-2014), it **did** beat
buy-and-hold on raw return, while holding a ~65-68% win rate and a much
shallower drawdown throughout. The strategy's edge is capital preservation
through volatility, converted opportunistically into outperformance
specifically when buy-and-hold itself struggles — not a guaranteed
outperformance machine in every regime.

**Important limitation found during testing, not from research:** this rule
set's high-win-rate behavior is strongest on broad index ETFs (SPY, QQQ). On
individual high-growth stocks (AAPL, MSFT) win rate drops to ~45-58% and the
strategy badly underperforms buy-and-hold, because no capped-holding-period
strategy can capture a decade of secular single-stock compounding (AAPL
returned +936% buy-and-hold 2015-2024; the strategy captured +59% of that).
**Use this strategy on diversified index ETFs, not concentrated growth
stocks**, if the goal is competing with buy-and-hold.

### Other research-verified facts baked into the design

- Fixed-fractional position sizing (risk ~1-2% of equity per trade based on
  stop distance) is well-verified as the standard risk-of-ruin safeguard —
  implemented as `sizing_mode="risk_based"`, just not the default (see above).
- A high win rate does not by itself imply profitability — expectancy depends
  on win rate AND average win/loss size (`metrics.expectancy_stats` reports
  this explicitly rather than just win rate, per the research's core warning).
- Curve-fitting safeguards: avoid overly precise parameters, look for a
  performance plateau across a neighborhood of values, and walk-forward
  validate. This code's defaults are round numbers (5% stop, 3:1 target, 63-day
  cap) chosen from a range that behaved similarly, not a single optimized point.

## Project layout

```
swing_trend_strategy/
  swingbot/
    config.py        SwingConfig dataclass — every tunable parameter, with rationale
    data.py           yfinance OHLCV loader with local CSV caching
    indicators.py     RSI (Wilder), SMA
    strategy.py       Vectorized entry/exit signal computation (no state)
    backtester.py     Event loop: next-bar-open entries, intrabar stop/target/trailing, cash/equity accounting
    metrics.py        CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, expectancy, avg holding period
    plotting.py       Price+MAs+trades chart, equity-curve-vs-both-benchmarks chart
  run_backtest.py      CLI entry point (always reports both benchmarks)
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
# Default: SPY vs its own buy-and-hold (both baselines coincide since symbol == benchmark)
python run_backtest.py --start 2010-01-01 --end 2024-12-31

# Test an individual stock against both its own buy-and-hold AND SPY buy-and-hold
python run_backtest.py --symbol MSFT --benchmark-symbol SPY --start 2010-01-01 --end 2024-12-31
```

Key options (see `python run_backtest.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--symbol`, `--benchmark-symbol` | Traded instrument and the second comparison baseline (default SPY) |
| `--sizing-mode`, `--risk-per-trade-pct`, `--position-size-pct` | `equity_pct` (full exposure, comparable to buy-and-hold) or `risk_based` (safer, 1% risk per trade) |
| `--trend-ma-period`, `--pullback-ma-period`, `--rsi-period`, `--entry-rsi-threshold` | Entry rule parameters (defaults are the verified rule) |
| `--exit-rsi-threshold` | Exit-by-RSI threshold — set to 65 to reproduce the literature's documented (lower-return) exit |
| `--stop-loss-pct`, `--reward-risk-ratio` | Stop-loss and profit target (target = stop × ratio) |
| `--trailing-activate-pct`, `--trailing-stop-pct`, `--no-trailing-stop` | Trailing stop behavior |
| `--max-holding-days` | Hard time cap (default 63 ≈ 3 months, per the user's requirement) |

Outputs land in `results/`: `<symbol>_trades.csv`, `<symbol>_equity_curve.csv`,
`price_and_trades.png`, `equity_curve.png`.

## Testing

```bash
python -m pytest tests/ -v
```

Covers: RSI/SMA correctness, vectorized entry/exit signal logic (all three
entry conditions required, rising-trend-filter gating, RSI exit threshold),
metrics (including expectancy and average-holding-period calculations), and
integration tests — trades occur in a trending-with-pullbacks market, cash
never goes negative, a regression test for a real sizing bug found during
development (100% equity allocation previously left no room for fees and
silently skipped every entry), the stop-loss caps a single trade's loss, the
profit target fires at the correct price, no trade exceeds the max holding
period, and risk-based position sizing matches the sizing formula.

## Known limitations

- Daily bars only; no intraday data or costs modeled.
- Single-asset, single-position backtests only — no portfolio-level
  allocation across multiple tickers, though the CLI makes it easy to run
  the same strategy across several symbols for comparison.
- No dividend cash-flow modeling beyond yfinance's auto-adjusted close.
- The exit-mechanism tuning (RSI threshold, trailing stop, profit target)
  was validated across two SPY windows for a performance plateau, not a
  full walk-forward optimization — re-validate before committing capital,
  and don't assume these defaults transfer well to individual stocks (see
  the honesty note above).

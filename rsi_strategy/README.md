# RSI-2 Long-Only Mean-Reversion Backtester (Stocks/ETFs)

A long-only backtester for the short-period RSI ("RSI-2", Larry Connors style)
mean-reversion strategy: buy oversold dips in an uptrend, sell the bounce.
Backtesting only — no order placement, no live/paper trading.

## Strategy summary

- **Entry**: price above its 200-day SMA (trend filter) AND a short-period
  RSI (default period 2) drops below an oversold threshold (default 10).
  A "cumulative RSI(2)" variant (sum of RSI(2) over 2 days) is also supported.
- **Exit**: configurable — RSI crossing back above a threshold (default 70),
  price closing above a short SMA (default 5-day), or either. See the
  research caveat below: unlike the entry rule, the exit rule is *not*
  settled by verified sources, so it's exposed as a parameter to test rather
  than hard-coded as "the" correct exit.
- **Risk controls**: optional intrabar stop-loss (checked against the bar's
  Low, off by default) and an optional max-holding-period time stop (on by
  default, 10 bars) — both configurable so they can be backtested with and
  without, since research found no verified consensus on either.
- **Execution model**: signals are computed from a bar's CLOSE, then acted on
  at the NEXT bar's OPEN, to avoid lookahead (you can't know a bar's own
  closing RSI value until it closes). The one exception is the stop-loss,
  which is checked intrabar and executes the same bar, since a protective
  stop is meant to react immediately.
- Long-only, single position at a time, mark-to-market equity every bar,
  commissions + slippage on every fill.

## Research grounding

Built after a multi-source, adversarially-verified research pass. What
survived verification vs. what didn't matters here — RSI-2 is a strategy
with a lot of confidently-stated folklore online, much of which did not
hold up:

**Well-verified (4+ independent, cross-checked sources):**
1. Entry rule: price > 200-day SMA AND RSI(2) < 10 (or < 5 for a more
   aggressive variant with historically higher — and riskier — returns).
   Mirror-image short rule exists but is out of scope here (long-only).
2. The 200-day trend filter measurably reduces max drawdown vs. an
   unfiltered version — it exists specifically to avoid buying oversold dips
   in a market that's just going to keep falling ("catching a falling knife").
3. RSI formula itself: `RSI = 100 - 100/(1+RS)`. Classic Wilder RSI smooths
   average gain/loss with recursive exponential smoothing (weight 1/n on the
   newest bar); "Cutler's"/plain RSI uses a simple moving average instead,
   specifically to avoid the result depending on how far back the calculation
   window starts. Both are implemented here (`--rsi-method wilder|cutler`)
   since which one a backtest uses is a real, reproducibility-relevant choice.

**Explicitly NOT verified — treat as open design choices, not settled facts:**
4. **The exit rule.** The commonly repeated convention (RSI crossing back
   above 50/70/90, or price closing above a 5-day SMA) is nearly universal in
   secondary/blog literature, but three independent attempts to verify the
   *specific* documented exit rule from primary-looking sources were all
   refuted during adversarial fact-checking. This code implements multiple
   exit modes (`--exit-mode rsi_cross|ma_cross|either`) so you can test which
   one actually works on your data instead of trusting a citation that didn't
   hold up.
5. **Position sizing, stop-loss policy, holding-period limits.** No verified
   claims addressed any of these. A frequently-repeated claim ("Connors
   tested stops across hundreds of thousands of trades and found they hurt
   performance") showed up in search results but was never independently
   confirmed, so it is *not* asserted as fact here — `stop_loss_pct` and
   `max_holding_days` are both implemented as configurable, independently
   testable knobs rather than baked-in "best practices."
6. **Most concrete performance statistics circulating online** (specific win
   rates, CAGR, Sharpe ratios like "75% win rate, Sharpe 2.85" or "9% CAGR at
   28% time invested") were explicitly refuted during verification and
   appear to be unsourced or fabricated in the blogs that state them. Do not
   treat any specific number you find in an RSI-2 blog post as verified.

**The one source with reproducible, cited backtest numbers** (Price Action
Lab / Michael Harris, SPY 1993–2018) found the strategy's results are driven
far more by the *exit* parameter than the entry parameter, and that even with
the 200-day filter, RSI-2 (CAGR 8.48%, max DD 19.0%, Sharpe 0.73) underperformed
a naive 50/200-day moving-average crossover (CAGR 9.76%, max DD 19.4%, Sharpe
0.76) on both absolute and risk-adjusted terms — a useful, honest expectation-
setter: this is a real, testable short-term edge, not a strategy that reliably
beats simpler trend-following, and small parameter changes swing results a lot
(changing only the exit from RSI>70 to RSI>95 nearly doubled max drawdown in
that sample, from -23.75% to -40.55%).

## Project layout

Shared code (the yfinance loader, standard indicators, and standard
performance metrics) lives one level up in `../common/` and is used by every
project in this workspace. Each module here re-exports the shared functions
it needs and keeps only project-specific logic local, so the public API
(`rsibot.data.load_ohlcv`, `rsibot.metrics.sharpe_ratio`, etc.) is unchanged
for callers.

```
rsi_strategy/
  rsibot/
    config.py        RSIConfig dataclass — every tunable parameter
    data.py           Thin wrapper over ../common/data.py, pinned to this project's data/ dir
    indicators.py     rsi(method=...) dispatcher (local) + rsi_wilder/rsi_cutler/cumulative_rsi/sma re-exported from ../common/indicators.py
    strategy.py       Vectorized entry/exit signal computation (no state)
    backtester.py     Event loop: next-bar-open execution, stops, cash/equity accounting
    metrics.py        summarize() (local) + base metrics re-exported from ../common/metrics.py
    plotting.py       Price+RSI+trades chart, equity-curve-vs-benchmark chart
  run_backtest.py      CLI entry point
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
python run_backtest.py --symbol SPY --start 2015-01-01 --end 2024-12-31
```

Key options (see `python run_backtest.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--rsi-period`, `--rsi-method` | RSI lookback (default 2) and smoothing method (`wilder`/`cutler`) |
| `--entry-mode`, `--oversold-threshold` | `single` (RSI < threshold) or `cumulative` (rolling sum of RSI < threshold) |
| `--no-trend-filter`, `--trend-ma-period` | Disable/configure the 200-day trend filter |
| `--exit-mode`, `--exit-rsi-threshold`, `--exit-ma-period` | Exit rule — the parameter research flagged as unsettled; test all three modes |
| `--stop-loss-pct` | Optional intrabar hard stop (default off/`None`) |
| `--max-holding-days` | Optional time-based exit safety net (default 10 bars) |
| `--commission-per-trade`, `--commission-pct`, `--slippage-pct` | Trading costs |

Outputs land in `results/`: `<symbol>_trades.csv`, `<symbol>_equity_curve.csv`,
`price_and_rsi.png`, `equity_curve.png`.

## Example results (SPY, default parameters: RSI(2)<10, 200-day filter, exit RSI>70)

| Period | Strategy return | Strategy max DD | Strategy Sharpe | Win rate | Profit factor | Buy & hold return | Buy & hold max DD |
|---|---|---|---|---|---|---|---|
| 2015-01 – 2024-12 | +25.9% | 12.6% | 0.41 | 72.9% | 1.69 | +227.0% | 33.7% |
| 2000-01 – 2012-12 | +31.7% | 15.5% | 0.40 | 74.0% | 1.83 | +23.6% | 55.2% |

Unlike the grid-trading strategy in the sibling project, this one shows a
genuine positive edge per trade (profit factor > 1, ~70%+ win rate) while
being in the market only ~11-14% of the time — but it still gives up most of
buy-and-hold's absolute return in a strong bull run (2015-2024), which matches
the research: this is a short-term mean-reversion edge layered on top of a
trend filter, not a replacement for staying invested. Changing just the exit
mode measurably moves results (see the `--exit-mode ma_cross` vs default
`rsi_cross` comparison you can reproduce yourself) — confirming the research
finding that the exit parameter, not the entry threshold, is what most drives
this strategy's behavior. Don't take the shipped defaults as tuned/optimal;
they're a documented, defensible starting point, not a fitted result.

## Testing

```bash
python -m pytest tests/ -v
```

Covers: RSI formula correctness (Wilder vs Cutler divergence, boundary cases),
cumulative-RSI and SMA helpers, vectorized signal logic (entry/exit
conditions, trend-filter gating), metrics formulas, and integration tests
(trades occur in oscillating markets, cash never goes negative, the stop-loss
caps a single trade's loss, and the max-holding-period safety net actually
forces an exit).

## Known limitations

- Daily bars only by default; no intraday data or costs modeled.
- Single-asset, single-position backtests only — no portfolio-level
  allocation across multiple tickers.
- No dividend cash-flow modeling beyond yfinance's auto-adjusted close.
- As documented above, the exit rule and all risk-management parameters are
  open design choices this code lets you test, not settled, source-verified
  best practices — don't treat the defaults as optimized.

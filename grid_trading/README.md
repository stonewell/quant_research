# ATR-Adaptive Grid Trading Backtester (Equities/ETFs)

A long-only grid trading backtester for stocks/ETFs where grid spacing and
recentering are driven by volatility (ATR) instead of fixed price levels.
Backtesting only — no order placement, no live/paper trading.

## Strategy summary

- **Grid mechanics**: buy-low/sell-high. A set of price levels is built around
  a center price; each pair of adjacent levels is a "slot". A flat slot buys
  when price touches its lower level; once long, it sells when price rises to
  the slot's upper level, banking the spacing as profit.
- **Dynamic spacing**: `spacing_pct = clip(ATR% * atr_multiplier, min_spacing_pct, max_spacing_pct)`.
  Wider ATR (more volatility) widens the grid; the floor/ceiling stop the grid
  from becoming degenerate (too tight → fee churn; too wide → misses
  oscillations).
- **Recentering**: the grid rebuilds around the current price whenever it's
  flat (no open risk), and force-liquidates + rebuilds if price breaks out of
  the outer band (`regrid_breakout_mult`) even while holding inventory.
- **Trend filter**: a long-term SMA band classifies each bar as `up` /
  `down` / `range`. New buys are blocked in a `down` regime, since grid bots
  lose money buying every dip in a sustained downtrend. Sells (closing
  existing longs) are never blocked.
- **Risk controls**: per-slot position sizing (% of equity), a cap on the
  number of simultaneously open slots, a capital reserve (never deploys 100%
  of equity into open grid positions), and a portfolio-level equity drawdown
  circuit breaker that liquidates everything and pauses new entries for a
  cooldown period.
- **Equities-specific**: long-only (no shorting), commissions + slippage on
  every fill, mark-to-market equity every bar (not just realized P&L on
  closed trades, which was flagged in research as a common source of
  backtest-performance illusion).

## Research grounding

This was built after a multi-source research pass (see findings below); the
main implementable takeaways that shaped the design:

1. **ATR-based dynamic spacing** is a documented practitioner pattern:
   `spacing = clip(ATR% * multiplier, floor, ceiling)`, typically recomputed
   on a rolling basis (hourly/daily depending on bar size) rather than fixed
   once at strategy start.
2. **Grid trading is fundamentally a range-bound/mean-reversion strategy.**
   Multiple independent sources agree it performs well in choppy, sideways
   markets and **loses money in sustained trends**, because a long-only grid
   keeps buying dips in a downtrend without a corresponding exit, and a
   trend-following escape (breakout regrid, trend filter, drawdown stop) is
   necessary to cap the damage — it will not eliminate it.
3. **Position sizing**: fix a small percentage of equity per grid order
   (1–2% cited) and cap the number of simultaneously open orders — otherwise
   a violent directional move over-leverages the account, since grid systems
   are Martingale-like (they keep committing capital as price moves against
   the average entry).
4. **Equity-based circuit breakers** (close everything if floating drawdown
   exceeds ~10% of account equity) are the practitioner-recommended backstop,
   distinct from a per-trade stop-loss, because grid strategies defer/avoid
   realizing per-trade losses by design.
5. **Backtest honestly**: mark unrealized inventory to market every bar. A
   grid backtest that only reports closed-trade P&L can look highly
   profitable intraday purely because winners close and losers stay open.
6. **Evaluate with standard risk-adjusted metrics** (Sharpe, Sortino, CAGR,
   max drawdown, win rate, profit factor) — a very high win rate is not
   sufficient on its own; profit factor (a much smaller number of larger
   trend-driven losses can outweigh many small grid wins) is the metric that
   most directly exposes this.

Confidence caveat: several numeric folk-heuristics circulating online (e.g.
"allocate exactly 30-50% of capital to active grids") did not survive
adversarial source-checking — they trace to single uncited marketing blogs
that contradict each other. Where research didn't converge on a hard number,
this code exposes the choice as a configurable parameter (see
`gridbot/config.py`) rather than hard-coding an unverified constant, and the
docstrings say so explicitly.

## Project layout

```
grid_trading/
  gridbot/
    config.py        GridConfig dataclass — every tunable parameter
    data.py           yfinance OHLCV loader with local CSV caching
    indicators.py     ATR (Wilder), SMA, trend-regime classifier
    grid_engine.py    Grid geometry + slot fill state machine (no money logic)
    backtester.py     Event loop: cash/equity accounting, costs, risk controls
    metrics.py        CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor
    plotting.py       Price+grid+trades chart, equity-curve-vs-benchmark chart
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
python run_backtest.py --symbol SPY --start 2018-01-01 --end 2024-12-31
```

Key options (see `python run_backtest.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--atr-period`, `--atr-multiplier` | ATR lookback and spacing scale factor |
| `--min-spacing-pct`, `--max-spacing-pct` | Spacing floor/ceiling |
| `--grid-levels-per-side` | Number of buy/sell levels each side of center |
| `--regrid-breakout-mult` | How far price must exit the band before a forced regrid/liquidation (see note below — this is the most sensitive parameter) |
| `--position-size-pct`, `--max-open-slots` | Per-slot sizing and exposure cap |
| `--trend-ma-period`, `--trend-band-pct` | Trend filter that gates new buys |
| `--drawdown-stop-pct`, `--cooldown-bars-after-stop` | Portfolio circuit breaker |
| `--commission-per-trade`, `--commission-pct`, `--slippage-pct` | Trading costs |

Outputs land in `results/`: `<symbol>_trades.csv`, `<symbol>_equity_curve.csv`,
`price_and_grid.png`, `equity_curve.png`.

## Example results (SPY, default parameters)

| Period | Market character | Grid total return | Grid max DD | Buy & hold return | Buy & hold max DD |
|---|---|---|---|---|---|
| 2018-01 – 2024-12 | strong sustained bull run | -1.1% | 3.0% | +140.5% | 33.7% |
| 2000-01 – 2012-12 | two crashes, no net progress ("lost decade") | -6.6% | 7.5% | +20.8% | 55.2% |

**Read this the right way**: the grid strategy underperforms buy-and-hold on
total return in both windows — it is *not* a free lunch, and it does not beat
a strong trend. What it consistently does is cut drawdown by roughly 10x
relative to buy-and-hold, at the cost of giving up most of the upside. That
tradeoff is the honest, expected behavior of a mean-reversion strategy per the
research above, not a bug — SPY spent both tested windows trending rather
than chopping sideways, which is the regime this strategy is weakest in.
`regrid_breakout_mult` controls that tradeoff directly: smaller values regrid
(and force-liquidate) more eagerly and rack up whipsaw losses; larger values
let the drawdown-stop do more of the defensive work and ride out more noise.
Don't treat the shipped default as tuned/optimal — walk-forward validate it
(rolling train/test windows, per the backtesting-methodology sources) on the
instrument and period you actually care about before drawing conclusions.

## Testing

```bash
python -m pytest tests/ -v
```

Covers: indicator correctness (ATR/SMA/trend regime), grid engine mechanics
(spacing clipping, level/slot construction, fill triggers, breakout
detection, liquidation), metrics formulas, and integration tests on synthetic
oscillating/crashing price series (verifies trades occur in range-bound
markets, cash never goes negative, and the drawdown stop actually caps
losses in a crash).

## Known limitations

- Daily bars only by default; intraday grid trading (the regime it's most
  associated with) would need sub-daily data and materially different cost
  assumptions.
- No dividend cash flow modeling beyond yfinance's auto-adjusted close
  (`auto_adjust=True`), which approximates but doesn't exactly replicate
  total-return accounting.
- Single-asset backtests only; no portfolio-level or multi-instrument grid
  allocation.
- The slot-fill model assumes a resting limit order at each grid level fills
  whenever the bar's high/low range covers it — a standard backtesting
  simplification, not a simulation of order-book depth or partial fills.

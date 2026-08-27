# fundamental_screener

Real-fundamentals buy/sell screening adapted from a conservative
value-investing community's valuation framework (see
`../../docs/snowball_strategy.txt` at the repo root): only hold durable,
moat-protected, high-ROE, dividend-paying compounders whose expected return
clears a risk premium over a broad-index benchmark, and sell the moment
that edge decays away.

This is the **real-fundamentals sibling** of
`research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy` (a
price-only proxy, since the rest of this workspace has no
dividend/ROE/earnings data and must stay offline/synthetic-testable). This
project instead fetches real ROE, dividend yield, earnings growth, and
debt-to-equity from yfinance -- so, unlike every other project in this
workspace, **it always touches the real network**, regardless of what
`--data-provider` you pass (that flag only controls the benchmark's own
OHLCV history, used for the sell-trigger comparator).

**Not wired into `run_pipeline.py`** -- its live-network dependency is a
deliberately different operating mode than every other pipeline stage's
offline-by-default convention.

## What it does

Given a universe, ranks:
- **Top-N buy candidates**: pass the quality gate (ROE/dividend/leverage/
  earnings-growth thresholds) AND their expected return
  (`earnings_growth + dividend_yield`, the source document's own "Model 2"
  formula) clears a required hurdle (default 12%).
- **Top-N sell candidates**: fail the quality gate, OR their expected
  return has decayed below the benchmark's own trailing return.

**Overlap resolution:** a symbol can never appear on both lists. Sell
always takes precedence over buy -- see `fscreen/rules.py`'s
`evaluate_buy_sell` docstring for why (a capital-preservation trigger
always outranks a return signal, matching the source document's own
conservatism).

## Usage

```bash
# Screen the default illustrative blue-chip basket (KO, PG, JNJ, MSFT, COST, WMT, MCD, PEP)
python run_fundamental_screener.py --data-provider synthetic

# A custom universe, real benchmark price history
python run_fundamental_screener.py --universe KO PG JNJ MSFT COST WMT MCD PEP \
  --benchmark SPY --data-provider yfinance

# Tune the quality/return thresholds
python run_fundamental_screener.py --required-return 0.10 --min-roe 0.20 \
  --max-debt-to-equity 100 --top-n 3
```

`--data-provider` (default `synthetic`, matching this workspace's offline
convention) only affects the benchmark's OHLCV history used for the
sell-trigger comparator -- **fundamentals always come from real yfinance**,
printed as an explicit warning when run with `--data-provider synthetic`
(a synthetic benchmark comparator is meaningless noise; use
`--data-provider yfinance` for a comparator that means anything).

### Outputs (`results/`)

- **`fundamental_screen_report.json`** -- the primary human-facing output:
  `run_context`, `n_universe_evaluated`, `top_buy` (list of
  `{symbol, expected_return, roe, dividend_yield, earnings_growth,
  debt_to_equity, quality_ok, buy_flag, sell_flag}`), `top_sell` (same
  shape), and a `caveat` field -- read it before trusting anything here.
- **`fundamental_strategy.json`** -- a `strategy.json`-compatible artifact
  (same shape `strategy_generator`/`research_strategy` produce) so
  `backtester/run_backtest.py --strategy-file results/fundamental_strategy.json`
  can run a real backtest against this project's own
  `FundamentalMarginOfSafetyStrategy`, via a `fundamental_spec` marker
  block (mutually exclusive with `pattern_spec`/`research_strategy_spec`/
  `composite_spec`, matching that same hand-off convention). This hand-off
  is deliberately manual, not auto-wired into `run_pipeline.py`.

## Disclosed limitations

- **Current, not historical, fundamentals.** `FundamentalMarginOfSafetyStrategy`
  (the backtester-facing strategy) fetches each candidate's fundamentals
  ONCE and treats them as a CONSTANT signal across the whole backtest
  window -- yfinance's free API has no historical point-in-time
  fundamentals. A backtest here answers "would this symbol pass TODAY's
  screen, applied retroactively," not "would it have passed the screen at
  each historical date."
- **No caching.** Each run re-fetches fundamentals for every candidate
  symbol from yfinance -- there's no caching layer for fundamentals (unlike
  this workspace's shared OHLCV cache at `../../data/`). Accepted for a first
  version; revisit if this becomes a real cost/rate-limit concern.
- **Illustrative candidate universe.** The default 8-symbol blue-chip
  basket is a hand-picked, unverified illustration, not a curated
  reproduction of the source document's own selection criteria.
- **Unit conventions.** `roe`/`dividend_yield`/`earnings_growth`/
  `debt_to_equity` are read as-is from yfinance's own `.info` fields with
  no unit normalization -- see `common/data.py`'s `YFinanceDataProvider.fetch_metadata`
  docstring for the exact source fields and their known inconsistencies
  across yfinance versions.

## Testing

Every automated test mocks `fetch_fund_metadata` -- no real network access
in this project's own test suite, matching this workspace's testing
conventions even though the CLI itself is real-data-only by design:

```bash
uv run pytest fundamental_screener/tests -v
```

## Layout

```
fundamental_screener/
├── fscreen/
│   ├── config.py        # ScreenerConfig
│   ├── fundamentals.py  # fetch_fundamentals_frame() -- the one real-network call site
│   ├── rules.py         # pure buy/sell rule evaluation (expected_return, quality_ok,
│   │                     evaluate_buy_sell, rank_buy_sell) -- shared by the CLI report
│   │                     and the backtester-facing strategy
│   └── strategy.py      # FundamentalMarginOfSafetyStrategy(AllocationTemplate)
├── run_fundamental_screener.py
├── tests/
└── README.md
```

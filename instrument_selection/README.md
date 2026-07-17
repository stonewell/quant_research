# Instrument Selection / Screening Tool for Quant Strategies

A screening tool that computes the documented, quantitative criteria for
judging whether a stock or ETF is a good, tradable, diversifying candidate
for systematic strategies — **deliberately strategy-agnostic**. An earlier
version of this tool scored instruments for "fit" against specific strategy
families (grid trading, trend-following, mean-reversion), but that requires
assuming which strategy you'll run before you've even picked the instrument,
and no verified formula for that combination survived this project's
research anyway. This version instead scores what matters for *any*
systematic strategy: tradability, adequate volatility, genuine statistical
structure, real diversification value, and enough trustworthy history. This
is a screening/research tool, not a backtester: it doesn't simulate trades,
it characterizes instruments.

## The five key aspects, and how each affects selection

### 1. Liquidity — can you actually trade it at the backtested price?

**Why it matters:** a backtested edge is worthless if it can't be executed.
Illiquid instruments suffer larger market impact from position-sized orders,
and bid-ask spread is a real, often-underestimated transaction cost that
erodes an edge on every round trip. Slippage is a function of volatility,
latency, and *strategy type* — momentum/trend strategies suffer more
slippage on average than mean-reversion strategies because they buy into
instruments that are already moving, chasing a price that keeps running away.

**What's implemented:** average dollar volume (`Close × Volume`, well-
established simple proxy), and the **Corwin & Schultz (2012, *Journal of
Finance*) high-low spread estimator** — a real academic method for
estimating the bid-ask spread from daily OHLC data alone, without needing
tick/quote data. It works by exploiting the fact that a single day's
high-low range reflects both true volatility and the bid-ask bounce, while a
2-day range reflects volatility over roughly twice the time but the *same*
bounce component — letting the two be mathematically disentangled.

**Honest caveat:** no single verified numeric liquidity cutoff (e.g. "$5M/day
minimum") survived this project's research pass — requirements are strategy-
and position-size-dependent. `min_avg_dollar_volume` is an adjustable
screening floor you should tune to your own capital and strategy, not a
scientifically-derived threshold. `liquidity_score` instead ranks
instruments by percentile *within your chosen universe*, a more defensible
relative measure.

### 2. Volatility — is there a tradable edge, and what kind?

**Why it matters:** grid trading needs volatility, but specifically
*range-bound* volatility — enough price oscillation to repeatedly hit grid
levels, without a sustained directional move that leaves the grid
one-sidedly exposed. Trend-following needs *sustained directional*
volatility. Very low-volatility instruments may not generate enough edge to
cover round-trip costs, regardless of strategy. Volatility-of-volatility
("vol clustering") matters too: an instrument whose own volatility regime is
itself unstable is harder for any strategy to size risk against consistently.

**What's implemented:** realized volatility (annualized rolling std of
returns), ATR% (ATR as a fraction of price — the same building block used in
this workspace's grid-trading project's dynamic spacing), vol-of-vol
(rolling std of the realized-vol series), and an ATR *regime-change* ratio
(short-window ATR% vs. its own longer-window average — research's documented
illustrative trigger is a 20-day vs. 60-day ATR ratio ≥ 1.30 signaling "cut
size, widen stops"). ADX (Wilder's trend-strength indicator) is included
too, since it's the standard practitioner convention for distinguishing
trending (ADX ≳ 25) from ranging (ADX ≲ 20) regimes — the same threshold
convention used in this workspace's ensemble-strategy project.

### 3. Predictability — does the series show genuine statistical structure at all?

**Why it matters:** this tool deliberately does NOT use this to decide
"trend-following vs. mean-reversion" (that's a strategy choice, made
separately, after you already know which instruments are worth trading).
What it answers instead is more fundamental: does this instrument's return
series show *any* statistically significant departure from a random walk —
in either direction — that a systematic strategy could in principle exploit?
An instrument indistinguishable from noise offers nothing for a systematic
strategy to grip onto, regardless of which family you'd choose. The
direction of any detected structure (trending vs. mean-reverting) is still
reported in `regime_label`, purely as descriptive information for whatever
strategy-selection decision you make separately.

The **Hurst exponent** (H) is the formal tool for this: H < 0.5 is
anti-persistent (mean-reverting — an up move tends to be followed by a down
move), H = 0.5 is a random walk (no exploitable memory), H > 0.5 is
persistent (trending — an up move tends to be followed by more up moves).

**What's implemented:** classical rescaled-range (R/S) analysis — split the
return series into chunks of varying size, compute the range of cumulative
mean-adjusted deviations divided by standard deviation for each chunk,
average across same-sized chunks, and take the slope of log(chunk size) vs.
log(average R/S). This slope is H.

**The single most important, well-documented pitfall this tool specifically
addresses:** a naive Hurst estimate computed directly on raw returns is
frequently above 0.5 (often ~0.6) purely from short-term autocorrelation
*artifacts*, not genuine long-range memory. A rigorous 1995 study (Cheung,
*Applied Economics Letters*) found this exact effect on a decade of daily FX
return data — and confirmed it by testing against Fourier-phase-randomized
surrogate data, finding the elevated H values were **not statistically
significant** versus random noise for nearly every series tested. Reporting
a raw H value with no significance test is exactly the kind of unverified
claim that failed adversarial fact-checking repeatedly during this project's
research. `hurst_significance()` builds an empirical null distribution by
computing H on many randomly-shuffled copies of the same series (which by
construction have no memory at all), and reports how extreme the observed H
is relative to that null — every reported `regime_label` in this tool's
output is gated on this significance test, not the raw H value alone.
(This shuffle-based test is simpler than, and a coarser instrument than, the
academic literature's Fourier-phase-randomization surrogates, since a full
shuffle destroys short-range as well as long-range dependence — documented
in the code so you know its limits.)

Autocorrelation (lag-1) and a simplified variance-ratio statistic (Lo &
MacKinlay's point estimate, not the full heteroskedasticity-robust test) are
included as complementary, easier-to-interpret cross-checks.

### 4. Correlation and diversification — building a basket, not a bet on one name

**Why it matters:** if you're running a strategy across several instruments
simultaneously, redundant, highly-correlated candidates don't diversify your
risk — you're effectively making one concentrated bet through several
tickers. **Critical, well-documented caveat this tool checks empirically
rather than assumes:** a peer-reviewed 2015 study (Cotter & Suurlaht, via
arXiv) found that correlation-cluster-based diversification is **not
uniform across regimes** — during a market crash, correlation-cluster
selection often produced the *largest* return variance among the methods
tested (worse, not better, than plain random selection), because pairwise
correlations tend toward 1 exactly when a crash hits and diversification is
needed most.

**What's implemented:** a pairwise correlation matrix, beta to a benchmark
(covariance-based, the standard definition), hierarchical clustering using a
proper correlation-distance metric (`d = sqrt(2×(1-ρ))`, the same metric used
in the correlation-clustering academic literature) to flag redundant
candidates, and — critically — `correlation_regime_shift()`, which
*empirically checks* whether average pairwise correlation on your specific
universe is actually higher during high-volatility periods than calm ones,
rather than assuming the documented phenomenon applies without verifying it
on your own data. Each symbol's own average correlation to the rest of the
universe now also feeds directly into the selection score's
`diversification_score` component (lower average correlation to peers scores
higher) — this is the concrete way correlation, not just liquidity and
volatility, shapes the ranking, per the request to weigh it explicitly.

### 5. History length and fund quality — can you trust the numbers, and will the fund still exist?

**Why it matters:** every statistic above is only as reliable as the sample
it's computed on — the Hurst exponent specifically becomes unreliable with
fewer than roughly 100-200 observations. Separately, for ETFs specifically, a
peer-reviewed hazard-model study (2020, *Journal of Financial Markets*) found
that the first three years of an ETF's life are its highest-risk window for
closure, and recommends individual investors favor ETFs at least 3-4 years
old; the same study found expense ratio and fund/family AUM are also real,
measurable determinants of closure risk. A separate peer-reviewed study
(Ben-David et al. 2020, *Journal of Financial Economics*) found illiquid ETFs
have larger tracking error and can have *higher return variance than their
own underlying NAV* — the ETF wrapper itself can add risk beyond the
underlying basket.

**What's implemented:** `history_years` (actual calendar span of available
price data — always computable, and a legitimate proxy for fund age when the
requested date range predates the fund's inception) drives
`history_adequacy_score`, capped at full credit once a fund clears
`min_history_years_for_full_credit` (default 4, matching the "3-4 years"
research threshold). Expense ratio and AUM are fetched best-effort via
yfinance metadata (`fetch_fund_metadata()`) and scored (`etf_expense_score`,
`etf_aum_score`) when available. Critically, these are **not weighted the
same as the verified research metrics above** — they're a data-availability
convenience, not themselves a research finding, and individual stocks simply
don't have them. The overall score handles this gracefully: when
expense-ratio/AUM data is missing for a symbol (e.g., it's a stock, or the
lookup failed), that component's weight is dropped and the remaining
weights are renormalized for that row — a stock is never penalized for
lacking a fund-only attribute.

## The composite score

`overall_selection_score` is a weighted average of five always-present
components (liquidity, volatility adequacy, predictability, diversification,
history adequacy) plus two optional ETF-metadata components, with weights:

| Component | Weight | Always available? |
|---|---|---|
| `liquidity_score` | 0.30 | yes |
| `vol_adequacy_score` | 0.20 | yes |
| `predictability_score` | 0.20 | yes |
| `diversification_score` | 0.15 | yes (needs ≥2 symbols) |
| `history_adequacy_score` | 0.10 | yes |
| `etf_expense_score` | 0.025 | best-effort |
| `etf_aum_score` | 0.025 | best-effort |

As with every other number in this tool: **no verified universal formula for
combining these into one score survived research** — these weights are a
transparent, documented, adjustable default (pass your own `weights` dict to
`score_universe()`), not a scientifically optimal combination. Use the
resulting rank as a shortlist to investigate further, not a proof of
anything.

## From scores to a chosen basket: the discrete selection step

Everything above ranks instruments individually. It doesn't answer the
actual question a basket-builder needs answered: **which K instruments do I
actually trade?** Sorting by `overall_selection_score` and taking the top K
is the obvious first idea, but it can happily fill a basket with mutually-
redundant names that all scored well for the same reason (the README's own
worked example above shows exactly this: QQQ and XLK, both tech-heavy, are
97.5% correlated — a naive top-K basket could contain both and call it
diversified). `correlation.redundancy_flags()` only WARNS about this; it
never resolves it into a final list. A follow-up deep-research pass looked
specifically for verified methods to close that gap — deliberately
excluding portfolio WEIGHT optimization (Markowitz, HRP, risk parity, which
allocate capital across an already-chosen set) since that's a different,
later problem. Three methods survived with real evidentiary backing and are
now implemented in `selectorbot/selection.py`:

### `select_cluster_representatives` — cluster, then pick one per cluster
Reuses this project's own `correlation.hierarchical_clusters`, then picks
ONE representative per cluster. **Confidence: high**, but the rule matters:
the one peer-reviewed, theoretically-grounded method for exactly this — ACC,
"Asset Clustering through Correlation" (Tang, Xu & Zhou, *Expert Systems
with Applications*, 2022) — proves a NARROWER rule than "pick the cluster's
best individual scorer." Their Theorem 2 shows that among portfolios formed
by picking one asset per correlation-cluster, choosing the **lowest-
variance** asset in each cluster minimizes portfolio variance — a claim
generalizing this into "lowest-volatility is the universally best
representative-selection rule" was separately checked during this research
and does **not** hold; the guarantee is conditional on that specific
downstream objective (minimizing portfolio variance) and on the correlation-
blockmodel clustering assumption. Backed by a real 19-year S&P 500 backtest
(Feb 2001–Jan 2020): ACC-selected baskets beat both plain SPY and sector-ETF
baskets on Sharpe/Sortino/Calmar under three separate allocation schemes
(e.g. Sharpe 0.79–0.86 vs. SPY's 0.36; max drawdown 32.45% vs. SPY's 55.25%
under a minimum-variance allocation). This module's `representative_rule=
"lowest_volatility"` (the default whenever a volatility Series is supplied)
implements the proven rule; `"highest_score"` is offered as a disclosed,
**unproven fallback** for when volatility data isn't available, or when
minimizing portfolio variance specifically isn't your actual goal.

### `select_diversified_greedy` — Max-Sum Diversification for a fixed K
Borodin, Lee & Ye (PODS 2012 / *ACM Transactions on Algorithms*) formalize
exactly this problem — select a fixed-size subset maximizing a quality score
plus the sum of pairwise diversity (distance) among selected members,
subject to a cardinality constraint — and explicitly name "portfolio
management" as an application domain alongside web search and facility
location. **Confidence: high** for the algorithm/theory — they prove the
problem is NP-hard even under a metric distance, and give a greedy
construction (repeatedly add the candidate with the highest marginal gain:
its own score plus `diversity_weight` times its total distance to everyone
already chosen) with a proven constant-factor approximation guarantee for a
monotone submodular quality function (a plain per-instrument score
qualifies trivially, as the modular special case). **No finance-specific
backtest exists** in the surviving research for this exact algorithm — the
guarantee is "provably not much worse than the best possible greedy-style
selection," not validated financial performance. Use `diversity_weight=0`
to recover naive top-K exactly (useful for side-by-side comparison).

### `select_diversified_threshold_greedy` — the simplest, threshold-gated variant
Walks candidates in descending score order and keeps one only if its
correlation to every already-selected instrument stays below
`max_correlation` — literally enforcing this project's existing
`max_cluster_correlation` config (previously used only to flag pairs, never
to act on them) during selection itself. Unlike the other two methods, it
determines the basket size FROM THE DATA rather than requiring K up front —
echoing a peer-reviewed finding (Yang, Rea & Rea, *Journal of Investment
Strategies*, 2016) that the number of instruments needed for adequate
diversification isn't a fixed constant: their PCA-based selection method
(a related but separately-researched, NOT-implemented-here approach — see
below) showed the "right" count shrinks when correlations rise and grows
when they fall, using as few as ~15 of 200 ASX-listed stocks to closely
replicate the full index depending on the prevailing correlation regime.

### What was researched but deliberately NOT implemented this pass
- **The Generalized MaxMean Dispersion Problem** (Prokopyev et al., 2009):
  folds the choice of K itself into a single ratio-maximization objective
  (maximize total pairwise diversity divided by total selected weight,
  subject only to a minimum-size constraint) — elegant, but a genuine
  fractional-programming problem, more involved to solve correctly than
  either greedy method above. Left undone to keep the implemented surface
  matched to what's actually tested.
- **PCA-based backward-elimination variable selection** (Yang, Rea & Rea
  2016, cited above): iteratively removes the instrument contributing least
  to diversification, using PCA loadings on the returns matrix. Needs an
  eigendecomposition this project doesn't otherwise compute — a reasonable
  future addition, not done here.

### The failure mode none of these methods resolve
Research explicitly checked whether the selection-step literature addresses
this project's own prior finding (Cotter & Suurlaht 2015, in
`correlation.py`'s docstring) that correlation-cluster-based diversification
can back the WORST outcome during a crash. It corroborated and sharpened the
concern rather than resolving it: **left-tail (crash) correlations run
systematically higher than calm-period correlations across asset classes**
— a persistent, well-documented regularity (Page & Panariello, *Financial
Analysts Journal*, 2018, corroborating earlier foundational work: Longin &
Solnik 2001; Ang & Bekaert 2002), not a one-off episode. All three methods
above consume a single, unconditional correlation matrix computed over the
whole sample (`correlation.correlation_matrix`) — none of them, nor the
wider selection-specific literature searched for this revision, resolves
the fact that a basket selected to look diversified in normal times can
become far more correlated than the matrix suggests exactly when a crash
makes diversification matter most. A claim that cluster structure becomes
MORE rigid (rather than reshuffling) during a crisis was itself checked and
did not hold up — so it remains a genuinely open question, not a
merely-under-researched one, how these selected baskets actually behave
when correlations spike.

## Other documented pitfalls (described, not coded)

- **Survivorship and look-ahead bias in backtesting:** using today's index
  constituents to backtest history inflates measured returns — one study
  found correcting for ~8% of missing (delisted) tickers in a momentum
  strategy eliminated ~40% of its apparent alpha; another found a
  low-volatility factor's well-documented outperformance actually *reverses*
  (high-vol stocks appear to outperform low-vol by 16x) when using a
  survivorship-biased "stocks currently in the index" universe instead of
  the correct point-in-time membership. This tool inherits the same risk if
  you feed it today's constituent list and backtest strategies over history
  — it does not correct for point-in-time universe membership.
- **Selection-bias / backtest overfitting from the selection process
  itself:** picking instruments because a strategy already performed well on
  them is a form of data mining. Bailey & López de Prado's Deflated Sharpe
  Ratio (DSR) formally corrects an observed Sharpe ratio for the number of
  independent trials searched, and shows a strategy with an annualized
  Sharpe of 2.5 can be statistically insignificant once you account for
  having searched 100 configurations. Their related finding is worth taking
  seriously: because most financial return series have some memory/serial
  dependence, backtest overfitting doesn't just wash out to zero
  out-of-sample — it can produce *negative* expected out-of-sample returns.
  If you use this tool's ranking to pick instruments and then backtest a
  strategy on exactly those instruments, be aware you've performed a search
  step that should be accounted for before trusting the backtest.

## What running this on a real broad-ETF universe actually found

Running the default universe (SPY, QQQ, IWM, DIA, EFA, EEM, GLD, SLV, USO,
TLT, IEF, and 5 sector ETFs) over 2015-2024 produced a genuinely informative,
self-validating result:

- **15 of 16 instruments showed a Hurst exponent statistically
  indistinguishable from a random walk** (`hurst_significant = False`) — only
  USO (oil) showed a significant trending signal (H=0.61). This is a strong,
  independent confirmation of a well-documented academic finding cited in
  research: applying R/S analysis to the S&P 500 itself over 1950-2012 gives
  H≈0.49, indicating the broad market index behaves close to a random walk
  over long samples. **This directly explains a pattern seen across this
  workspace's other projects**: mean-reversion and trend-following strategies
  applied to SPY/QQQ over long, mixed-regime windows don't have a strong,
  persistent statistical edge on the whole-period series — any edge they
  show tends to be regime-specific (e.g., real during 2000-2002/2008-style
  stress, absent during a long, mostly-random-walk-like bull grind).
- **The correlation-spike-in-stress phenomenon was empirically confirmed on
  this data**: average pairwise correlation was 0.266 in calm periods vs.
  0.389 in high-volatility periods (a 1.46x spike) — matching the documented
  warning that diversification benefits shrink exactly when most needed.
- **Redundant pairs were correctly identified**: QQQ↔XLK (ρ=0.975, both
  tech-heavy), TLT↔IEF (ρ=0.918, both long-duration Treasuries), SPY↔DIA
  (ρ=0.954) — sensible, real-world-plausible groupings that a basket
  constructor should deduplicate before allocating capital across "16
  diversified ETFs" that are actually closer to 8-10 independent bets.
- **With strategy-fit removed, the top-ranked instruments by
  `overall_selection_score` were EEM (56.1), TLT (54.9), QQQ (54.6), and SPY
  (54.5)** — all four combine strong liquidity with either useful
  diversification (TLT's negative-ish beta to SPY, EEM's lower correlation to
  the rest of the universe) or high liquidity/history-adequacy. USO, despite
  being the *one* instrument with statistically significant predictability
  (H=0.61), ranked near the bottom (34.2) because that single strength was
  outweighed by the composite's other components: worst-in-universe
  volatility adequacy (its realized vol was the highest of all 16, and the
  scoring penalizes extremes relative to peers, not just "more vol"), weak
  liquidity, and the worst expense-ratio/AUM scores in the universe — a
  concrete illustration of why a composite score matters: one strong factor
  doesn't make a good overall candidate if every other factor is weak.

## Project layout

Shared code (the yfinance loader and standard indicators) lives one level up
in `../common/` and is used by every project in this workspace. Each module
here re-exports the shared functions it needs and keeps only project-specific
logic local, so the public API (`selectorbot.data.load_ohlcv`,
`selectorbot.volatility.atr`, `selectorbot.persistence.hurst_exponent`, etc.)
is unchanged for callers. `liquidity.py`, `correlation.py`, and `scoring.py`
are unique to this project and unaffected.

```
instrument_selection/
  selectorbot/
    config.py        SelectionConfig — universe, benchmark, all thresholds
    data.py           Thin wrapper over ../common/data.py (load_ohlcv/load_universe/fetch_fund_metadata), pinned to this project's data/ dir
    liquidity.py      Avg dollar volume + Corwin-Schultz spread estimator
    volatility.py     volatility_summary() (local) + atr/atr_pct/adx/realized_vol/vol_of_vol/atr_regime_ratio re-exported from ../common/indicators.py
    persistence.py    hurst_significance()/persistence_summary() (local, shuffle-based significance test) + hurst_exponent/autocorrelation/variance_ratio re-exported from ../common/hurst.py
    correlation.py    Correlation matrix, beta, hierarchical clustering, redundancy flags, regime-shift check
    scoring.py        Strategy-agnostic composite score: liquidity, volatility adequacy, predictability, diversification, history/fund-quality
    selection.py       Turns scores + correlation into an actual chosen basket: cluster-representative, Max-Sum-Diversification greedy, and threshold-gated greedy (see "From scores to a chosen basket" above)
    plotting.py       Correlation heatmap, dendrogram, Hurst-vs-volatility scatter (descriptive, not strategy-specific)
  run_screener.py      CLI — full report across all metrics for a universe, plus a chosen basket via --select-method
  tests/                pytest unit tests (43 tests covering every module)
  data/                 cached price CSVs (gitignored)
  results/              screening report, correlation matrix, charts (gitignored)
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r ../requirements.txt
```

## Usage

```bash
# Default broad-ETF universe
python run_screener.py --start 2015-01-01 --end 2024-12-31

# Your own universe
python run_screener.py --universe SPY QQQ AAPL MSFT NVDA GLD TLT --benchmark SPY
```

Key options (see `python run_screener.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--universe` | Space-separated list of tickers to screen (benchmark is auto-added) |
| `--benchmark` | Symbol used for beta and the correlation regime-shift check |
| `--min-avg-dollar-volume` | Liquidity floor (adjustable, not a verified universal number) |
| `--max-cluster-correlation` | Threshold above which a pair is flagged as redundant |
| `--no-fund-metadata` | Skip the best-effort expense-ratio/AUM lookup (faster, no ETF-quality component) |
| `--top-n` | How many top-ranked instruments to print by overall selection score |
| `--select-method` | `top_k` (naive baseline), `cluster` (ACC-style representative-per-cluster), `greedy` (Max-Sum Diversification, needs `--select-k`), or `threshold` (default -- gated by `--max-cluster-correlation`, sizes itself) |
| `--select-k` | Basket size for `top_k`/`greedy` (required for `greedy`) |
| `--select-max-k` | Optional cap on basket size for `threshold` (which otherwise sizes itself from the data) |

Outputs land in `results/`: `screening_report.csv` (every metric and score
per symbol), `correlation_matrix.csv`, and three charts. The chosen basket
(per `--select-method`) prints to stdout — run all four methods side by
side on your own universe to compare, since no head-to-head comparison of
them survived this project's research (see "From scores to a chosen
basket" above).

## Testing

```bash
python -m pytest tests/ -v
```

43 tests covering: the Corwin-Schultz spread estimator (non-negative,
increases with wider high-low noise), realized vol/ATR%/vol-of-vol/ADX
correctness on synthetic series with known properties, the Hurst estimator
correctly ordering trending > random-walk > mean-reverting synthetic AR(1)
series and the significance test correctly flagging strong signals while
NOT flagging pure noise, correlation/beta/clustering/redundancy detection on
constructed correlated and independent series, the regime-shift check
detecting a synthetic stress-correlation spike, and the composite scoring
formulas — including that predictability is direction-agnostic, insignificant
Hurst readings score near zero, diversification favors low correlation to
the rest of the universe, history adequacy caps at the configured threshold,
and — the most important behavioral test — that a symbol missing ETF-only
metadata (e.g., a plain stock) still gets a valid, non-NaN overall score
with its weights correctly renormalized rather than being penalized;
`selection.py`'s three methods (`test_selection.py`) on a constructed
"near-identical pair + one independent symbol" universe: cluster
representatives correctly collapse the redundant pair to one member per the
`highest_score` and `lowest_volatility` rules (including that
`lowest_volatility` overrides a higher raw score, and that requesting it
without a volatility Series raises), the Max-Sum-Diversification greedy
prefers the independent symbol over a near-duplicate once `diversity_weight`
is nonzero but degenerates to exact naive top-K at `diversity_weight=0`
(a direct check that the "improvement over naive" claim is real, not just
asserted), and the threshold-gated greedy both skips the correlated
lower-priority candidate and lets basket size emerge from the data rather
than a fixed K.

## Known limitations

- Does not correct for survivorship bias or point-in-time universe
  membership — feeding it today's index constituents and backtesting
  strategies over history inherits that well-documented risk (see above).
- The Hurst significance test uses full-shuffle surrogates (simpler, coarser
  than the academic literature's phase-randomization method) — a
  significant result means "some temporal dependence beyond chance," not
  proof of long-range memory specifically.
- `scoring.py`'s composite formula is a transparent, documented way of
  combining the individual verified metrics — not a validated predictive
  model. No such model survived this project's research pass. Use the
  ranking as a shortlist to investigate further, not a guarantee.
- Expense ratio and AUM come from yfinance's `.info` metadata, which is
  inconsistent across instrument types and frequently missing — this is a
  data-availability limitation, not a research finding, and the composite
  score is designed to degrade gracefully (not penalize) when it's absent.
- `history_years` is calendar span of the data actually downloaded, which
  only equals true fund age when your requested start date predates the
  fund's inception; if you request a shorter window than the fund's real
  history, this will understate its true age.
- **`selection.py`'s three methods are not validated against real market
  data this session** (per this pass's own instruction) — only synthetic,
  constructed-correlation test universes. `select_cluster_representatives`
  and `select_diversified_greedy` both carry real backtested/theoretical
  evidence from their respective sources (see above), but this project's
  own implementation of them hasn't been run against real prices yet.
- All three selection methods consume ONE static, unconditional correlation
  matrix — none of them adapts to the well-documented fact that
  correlations spike specifically during crashes (see "The failure mode
  none of these methods resolve," above). A basket that looks well-
  diversified on `correlation.correlation_matrix`'s full-sample numbers can
  still be far more correlated than that exactly when it matters most.
- No head-to-head comparison of the three methods against EACH OTHER
  survived research (each is validated against a naive baseline in its own
  source, not against the others) — running more than one on your own
  universe and comparing is this project's own exploration, not a
  reproduction of a verified ranking.

[ English | [简体中文](README_ZH.md) ]

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

## Screen first, score second: hard gates before soft scores

Liquidity and history length used to be soft, rank-based inputs into the
one composite score everything downstream (including the diversification-
selection methods below) consumed — `min_avg_dollar_volume` existed as a
config field but was never actually enforced anywhere; a genuinely illiquid
instrument could still rank well by scoring strongly on unrelated
dimensions (predictability, diversification, history) and end up selected
anyway. A follow-up deep-research pass specifically checked whether that's
defensible, and found real, citable evidence it isn't:

- **Index-provider methodology** (MSCI's GIMI and Factor Index families,
  verified against primary-source PDFs, stable across ~2013-2024 document
  vintages): a set of enumerated, **binary pass/fail** investability screens
  — minimum size, minimum liquidity, minimum length of trading, financial
  reporting — defines an eligible universe strictly BEFORE any
  factor-tilting, weighting, or optimization step runs on it. MSCI's own
  factor "Alpha score" formula deliberately **excludes liquidity as a
  component** — it's resolved entirely by universe construction (which
  Parent Index the optimizer is even allowed to draw from), never blended
  into the score the optimizer selects on.
- **The composite-indicator/MCDA literature** (OECD/JRC *Handbook on
  Constructing Composite Indicators*; Cinelli, Kadziński, Gonzalez &
  Słowiński, *Omega*, peer-reviewed) formally names this failure mode **"full
  compensability"**: an additive/weighted score lets a unit offset a
  deficiency on one dimension with strength on another — the Handbook's own
  worked example shows (21,1,1,1) and (6,6,6,6) can score identically
  despite representing very different underlying conditions. Its
  prescription for a genuinely non-negotiable requirement (a tradability
  floor is exactly that) is to remove it from the composite via a **prior
  hard gate**, not to keep tuning weights.
- **This project's own `hurst_min_obs` floor already did this correctly**
  for the Hurst significance test specifically (see `persistence.py`) —
  below that many observations, the result is `NaN`/`"insufficient_data"`,
  not a low-confidence score. Independent peer-reviewed evidence (Weron 2002,
  *Physica A*) supports treating short samples this way: DFA/GPH
  Hurst-estimator error degrades by roughly an order of magnitude between
  L=256 and L=65536 observations, and the original study excluded its own
  shortest sample size from part of its analysis rather than including it
  with a caveat. `screening.py` (new) extends the SAME pattern to liquidity
  and overall history length, which previously had no equivalent floor.

`run_screener.py` now runs `screening.screen_universe()` immediately after
computing raw metrics and BEFORE building the correlation matrix or scoring
anything — an excluded instrument is dropped from the correlation matrix,
`overall_selection_score`, and every `selection.py` method entirely, not
just soft-scored down. Two hard gates are implemented: `min_avg_dollar_volume`
(existing field, now actually enforced) and a new, deliberately low/
permissive `min_history_years` (distinct from the SOFT
`min_history_years_for_full_credit` used by `history_adequacy_score` — the
hard floor just asks "is there enough data to compute anything meaningful
at all," the soft threshold asks "has this ETF cleared its highest-closure-
risk window"). The benchmark symbol is exempt from both gates (beta and the
correlation regime-shift check both require it to be present), and every
excluded symbol's reason is reported (`screened_out.csv`, printed to stdout)
rather than having it silently vanish.

**What this pass could NOT resolve, and neither could the wider literature
searched for it:** whether screening BEFORE vs. AFTER correlation/clustering
changes which instruments end up as cluster representatives or Max-Sum
greedy picks. This project screens before (matching the index-provider
precedent's ordering), but that specific ordering choice is not
independently verified to matter for these particular selection algorithms
— it's a reasonable default, not a proven-optimal one.

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
scientifically-derived threshold. `liquidity_score` ranks instruments by
percentile *within the universe that already cleared that floor* (see
"Screen first, score second," below, for why this is now a hard gate applied
in `screening.py`, not just a soft, rank-based input the way it used to be).

### 2. Volatility and Downside Risk — is there a tradable edge, and what kind?

**Why it matters:** grid trading needs volatility, but specifically
*range-bound* volatility — enough price oscillation to repeatedly hit grid
levels, without a sustained directional move that leaves the grid
one-sidedly exposed. Trend-following needs *sustained directional*
volatility. Very low-volatility instruments may not generate enough edge to
cover round-trip costs, regardless of strategy.

Standard volatility measures treat upward rallies as "risk", penalizing assets
that have strong upward price gains. **Downside realized volatility** (semi-deviation,
Estrada 2000; Ang, Chen & Xing 2006, *Journal of Finance* "Downside Risk") isolates
loss volatility relative to 0. Assets with low **downside volatility ratio**
(downside vol / total realized vol) exhibit favorable right-skewed gain dynamics
and lower crash propensity.

**What's implemented:** realized volatility (annualized rolling std of
returns), downside realized volatility (annualized semi-deviation of negative returns),
downside volatility ratio, ATR% (ATR as a fraction of price — the same building block used in
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

### 3b. Candlestick predictability — does the OHLC structure carry information too?

**Why it's here, and why it's a SEPARATE, deliberately small component from
the Hurst predictability above:** the Hurst test asks whether *close-to-close
returns* have exploitable long-memory structure. Candlestick analysis makes a
different, older claim: that the *within-bar and across-bar open/high/low/close
relationships* — hammers, engulfings, stars — carry short-horizon reversal
information that close-only statistics miss. This component asks the same
strategy-agnostic question as §3 ("is there genuine, statistically significant
structure at all?"), but on that OHLC-geometry channel instead of long memory,
and reports the direction only as a descriptive label (`candlestick_label`).

**The honest, adversarially-checked research picture (this is a contested
area, and the component is weighted to reflect that):**

- **The notable study IN FAVOUR — and the source of the test this component
  implements:** Caginalp & Laurent (1998, *Applied Mathematical Finance*
  5:181-206, "The Predictive Power of Price Patterns") ran a non-parametric
  test on daily OHLC of all S&P 500 stocks 1992-1996 and found three-day
  candlestick reversal patterns predicted a reversal out-of-sample at ~36
  standard deviations from the null (~1% over a two-day hold). Their core
  statistic — does the *conditional* probability of a reversal given a pattern
  exceed the *unconditional* base-rate probability — is exactly the
  direction-agnostic information-content measure `candlestick.py` computes.
- **The more rigorous study AGAINST — and the source of the significance test:**
  Marshall, Young & Rose (2006, *Journal of Banking & Finance* 30(8):2303-2323)
  built an extension of the Efron (1979) bootstrap that resamples random OHLC
  series and found candlestick strategies have **no value** on DJIA stocks
  1992-2002 — "further evidence that this market is informationally efficient."
  Their bootstrap-null comparison (real edge vs. the edge you'd get by chance)
  is the discipline `candlestick_significance()` follows, via a coarser
  random-date placebo (documented as coarser, the same honesty caveat the
  Hurst test carries about its own shuffle surrogates).
- **Corroboration across markets:** a Swedish OMXS30 study (2007-2015) found
  poor predictive power / weak-form efficiency; an intraday DJIA 5-minute
  study (Etienne et al.) found ~a third of rules beat buy-and-hold at the
  Bonferroni level but **none** survived transaction costs plus the SSPA
  data-snooping correction.

**What's implemented:** `common/indicators.py` detects the canonical
single-/two-/three-bar reversal patterns (hammer, hanging man, inverted
hammer, shooting star, bullish/bearish engulfing, piercing line, dark-cloud
cover, bullish/bearish harami, morning/evening star, three white soldiers /
three black crows, plus a neutral doji), each gated on a preceding-trend
context exactly as Caginalp & Laurent used a short MA (a hammer in a downtrend
is bullish; the identical shape in an uptrend is the bearish hanging man).
`candlestick.py` then measures the pattern-conditional forward return net of
the base-rate drift, tests it against a placebo null, and — like every
`regime_label` in this tool — gates the reported edge on that significance
test. `candlestick_score` ranks the absolute (direction-agnostic) edge across
the surviving universe and down-weights insignificant readings to near-zero,
just as `predictability_score` does for an insignificant Hurst value.

**What to expect (and why a near-zero result is the CORRECT outcome, not a
bug):** given the weight of rigorous evidence, `candlestick_significant`
should be **False for most liquid instruments** — directly analogous to this
project's own Hurst finding, where 15 of 16 broad ETFs came back
random-walk-like. A non-zero, significant candlestick edge is a flag that an
instrument is unusual and worth investigating further, *not* a validated
trading edge. That is why the composite gives this component the smallest
weight of the five always-present scores (0.03, the smallest share of the
"exploitable structure" family) and treats an insignificant reading as ~zero.

### 3c. Momentum predictability — does the instrument's own past return predict its future?

**Why it's here, and how it differs from §3 and §3b:** momentum is the single
most-replicated form of the "is there exploitable structure?" question, so it
gets its own channel. §3 (Hurst) measures long-range memory via variance
scaling; §3b (candlestick) measures OHLC-geometry reversals; this component
measures the **serial correlation between an instrument's past `lookback`-day
return and its subsequent `horizon`-day return** — the literal statistical
core of the momentum anomaly. As always, direction is reported descriptively
(`momentum_label`: `momentum`/trending vs. `reversal`/mean-reverting) and the
*score* is the direction-agnostic magnitude, gated on significance. This is a
strategy-agnostic property of the instrument ("does momentum work on it over
the sample?"), NOT a "buy it because it's up right now" timing call — the
trailing return is reported only as a descriptive snapshot
(`momentum_lookback_return`), never scored.

**The honest, adversarially-checked research picture:**

- **The evidence FOR (two of the most-cited anomalies in finance):**
  Jegadeesh & Titman (1993, *Journal of Finance* 48(1):65-91) documented
  cross-sectional momentum (past 3-12-month winners keep beating losers by
  ~1%/month over the next 3-12 months, NYSE/AMEX 1965-1989, not explained by
  systematic risk — though it partially reverses over the following two
  years). Moskowitz, Ooi & Pedersen (2012, *Journal of Financial Economics*
  104(2):228-250) documented **time-series** momentum: a security's own past
  12-month excess return positively predicts its next-month return for **every
  one of 58 liquid futures** across equities, currencies, commodities and
  bonds. The time-series form is the one relevant to picking a single
  instrument, and is exactly what `momentum_edge` estimates.
- **The critique AGAINST — and the reason it's still bootstrap-tested
  per-instrument rather than trusted outright:** Huang, Li, Wang & Zhou (2020,
  *JFE* 135(3):774-794, "Time series momentum: Is it there?") showed the
  headline TSM result rests on a **pooled** regression whose large t-stat
  (~4.3) is *not* statistically reliable — it over-rejects no-predictability
  because of cross-asset mean differences, a persistent predictor, and
  volatility scaling. Their bootstrap-corrected, **asset-by-asset** tests find
  little TSM in- or out-of-sample, and the strategy performs about the same as
  one based on the historical mean that needs no predictability at all. Their
  prescription — test each instrument on its own against a proper bootstrap
  null — is *exactly* what this module does, so the critique is baked into the
  method rather than argued around.
- **The technical-indicator angle (RSI/MACD/ROC) specifically:** Park & Irwin
  (2007, *Journal of Economic Surveys*) reviewed 95 modern technical-analysis
  studies (56 positive / 20 negative / 19 mixed) and found most suffer
  data-snooping, ex-post rule selection, and transaction-cost problems; their
  futures reality-check found that after White's Bootstrap Reality Check and
  Hansen's SPA data-snooping corrections, popular rules including RSI and MACD
  were significant in only 2 of 17 contracts and didn't persist out-of-sample.
  So the classic momentum *indicators* (RSI, MACD, ROC) are computed and
  reported descriptively, but the **scored** quantity is the bootstrap-tested
  return-predictability edge, not a raw indicator signal.
- **The cross-cutting caveat even when momentum IS real:** Daniel & Moskowitz
  (2016, *JFE* 122(2):221-247, "Momentum crashes") show momentum returns are
  strongly negatively skewed (the winner-minus-loser portfolio's monthly skew
  is −4.70) with infrequent but severe, persistent crashes concentrated in
  panic states — after market declines, when volatility is high,
  contemporaneous with rebounds (Mar-May 2009: past losers +163% vs. past
  winners +8%). A high momentum edge is **not** a free lunch; it carries
  left-tail crash risk this single number can't capture — the same spirit as
  the unresolved correlation-spikes-in-a-crash caveat in §4.

**What's implemented:** `common/indicators.py` adds `roc` (rate of change /
raw momentum, the J&T/MOP 12-month signal) and `macd`, alongside the existing
`rsi` and `adx`. `momentum.py`'s `momentum_efficacy()` computes the past-vs-
future serial correlation (using log prices, so the Pearson correlation
demeans out the per-asset drift that biased Huang et al.'s pooled test) and
tests it against a shuffle null that destroys serial dependence while
preserving the sliding windows' mechanical overlap. `momentum_score` ranks the
absolute edge across the surviving universe and down-weights insignificant
readings to near-zero, exactly like the Hurst and candlestick channels.

**What to expect:** as with Hurst and candlesticks — and consistent with both
this project's own random-walk-like Hurst finding and Huang et al.'s weak
per-asset result — `momentum_significant` should be **False for many broad,
liquid instruments** over long, mixed-regime windows. A significant momentum
edge flags an instrument whose own-return predictability is unusually strong
on this sample: a lead to investigate, weighted small (0.10) and crash-
caveated, not a validated trading edge.

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
requested date range predates the fund's inception) is now checked TWICE:
a hard floor (`min_history_years`, default 1) excludes an instrument
entirely in `screening.py` before anything else runs, and — only for
instruments that clear it — drives `history_adequacy_score`, capped at full
credit once a fund clears `min_history_years_for_full_credit` (default 4,
matching the "3-4 years" research threshold). See "Screen first, score
second," above, for why these are two separate thresholds with two separate
jobs, not one number doing double duty. Expense ratio and AUM are fetched best-effort via
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

`overall_selection_score` is a weighted average of seven always-present
components (liquidity, volatility adequacy, predictability, momentum,
candlestick predictability, diversification, history adequacy) plus two
optional ETF-metadata components, with weights:

| Component | Weight | Always available? |
|---|---|---|
| `liquidity_score` | 0.30 | yes |
| `vol_adequacy_score` | 0.20 | yes |
| `momentum_score` | 0.10 | yes |
| `predictability_score` | 0.07 | yes |
| `candlestick_score` | 0.03 | yes |
| `diversification_score` | 0.15 | yes (needs ≥2 symbols) |
| `history_adequacy_score` | 0.10 | yes |
| `etf_expense_score` | 0.025 | best-effort |
| `etf_aum_score` | 0.025 | best-effort |

The "does this series show exploitable structure at all?" family keeps its
original combined weight of **0.20**, now split across three independent,
bootstrap-null-gated tests **by strength of evidence**: `momentum_score`
(time-series-momentum serial correlation) gets the largest share at 0.10 — two
of the most-replicated anomalies in finance (§3c) — `predictability_score`
(long-memory Hurst) 0.07, and `candlestick_score` (OHLC reversal edge) the
smallest at 0.03 (weakest evidence, §3b). Momentum is capped at 0.10 rather
than higher because its per-asset significance is contested (Huang et al.
2020) and it carries left-tail crash risk (Daniel & Moskowitz 2016). Nothing
outside the family changed, so the weights still sum to 1.0, and — like every
other component — a missing or statistically insignificant reading in any of
the three degrades gracefully to near-zero rather than penalizing.

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

### `select_max_diversification_ratio` — Maximum Diversification Ratio selection
Greedily selects a subset of K assets that maximizes Choueifaty & Coignard (2008,
*Journal of Portfolio Management*, "Toward Maximum Diversification")'s landmark
**Diversification Ratio**:
$$DR(w) = \frac{w^T \sigma}{\sqrt{w^T \Sigma w}}$$
DR measures the ratio of weighted average asset volatilities to total portfolio
volatility. Selecting assets that maximize DR ensures the basket exhibits strong
correlation-risk reduction properties.

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
- **The candlestick component came back with NO significant edge for every
  liquid ETF tested** — a direct real-data re-confirmation of the dominant
  academic finding (Marshall, Young & Rose 2006) and a near-exact parallel to
  the Hurst result above. On a 2015-2024 run, SPY/QQQ/TLT/GLD each detected
  800-930 reversal patterns yet produced a base-rate-adjusted conditional edge
  within ~±0.1% and a placebo p-value nowhere near significance (0.12-0.59),
  so all four scored near-zero on `candlestick_score`. As documented in §3b,
  this "no edge" outcome is the *expected* result for liquid instruments, not
  a defect — the component earns its keep by flagging the rare instrument that
  deviates from it, exactly as the Hurst test flagged only USO.
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
in `../../common/` and is used by every project in this workspace. Each module
here re-exports the shared functions it needs and keeps only project-specific
logic local, so the public API (`selectorbot.data.load_ohlcv`,
`selectorbot.volatility.atr`, `selectorbot.persistence.hurst_exponent`, etc.)
is unchanged for callers. `liquidity.py`, `correlation.py`, and `scoring.py`
are unique to this project and unaffected.

```
instrument_selection/
  selectorbot/
    config.py        SelectionConfig — universe, benchmark, all thresholds
    data.py           Thin wrapper over ../../common/data.py (load_ohlcv/load_universe/fetch_fund_metadata), pinned to this project's data/ dir
    liquidity.py      Avg dollar volume + Corwin-Schultz spread estimator
    volatility.py     volatility_summary() (local) + atr/atr_pct/adx/realized_vol/vol_of_vol/atr_regime_ratio re-exported from ../../common/indicators.py
    persistence.py    hurst_significance()/persistence_summary() (local, shuffle-based significance test) + hurst_exponent/autocorrelation/variance_ratio re-exported from ../../common/hurst.py
    candlestick.py    candlestick_significance()/candlestick_summary() (local, placebo-null test on OHLC reversal patterns) + pattern detectors re-exported from ../../common/indicators.py
    momentum.py       momentum_efficacy()/momentum_summary() (local, shuffle-null test on past-vs-future return serial correlation) + roc/macd/rsi re-exported from ../../common/indicators.py
    correlation.py    Correlation matrix, beta, hierarchical clustering, redundancy flags, regime-shift check
    screening.py       HARD gates (liquidity floor, min history) applied BEFORE scoring/correlation/selection -- see "Screen first, score second" above
    scoring.py        Strategy-agnostic composite score for instruments that already cleared screening.py's gates: volatility adequacy, predictability, momentum, candlestick predictability, diversification, history/fund-quality
    selection.py       Turns scores + correlation into an actual chosen basket: cluster-representative, Max-Sum-Diversification greedy, and threshold-gated greedy (see "From scores to a chosen basket" above)
    plotting.py       Correlation heatmap, dendrogram, Hurst-vs-volatility scatter (descriptive, not strategy-specific)
  run_screener.py      CLI — full report across all metrics for a universe, plus a chosen basket via --select-method
  tests/                pytest unit tests (72 tests covering every module)
  data/                 cached price CSVs (gitignored)
  results/              screening report, correlation matrix, screened-out report, charts (gitignored)
```

## Setup

This project shares a single `uv`-managed environment with the rest of the
`pipeline/` group. From `pipeline/` (one level up):

```bash
uv sync
```

## Usage

### Argument reference

All flags are optional except `--select-k` (required only when `--select-method greedy` is
chosen). Universe-resolution flags (`--universe`/`--universe-file`/`--universe-provider`/
`--universe-kwargs`) are shared with the other 3 projects — see
`common/README.md`'s cross-reference index; `resolve_universe_from_args` picks the first one
supplied, in that order, falling back to this project's own 16-symbol `DEFAULT_UNIVERSE` (SPY,
QQQ, IWM, DIA, EFA, EEM, GLD, SLV, USO, TLT, IEF, XLE, XLF, XLK, XLV, XLU) if none are given.

| Flag | Type / default | Meaning |
|---|---|---|
| `--universe` / `-u` | space-separated tickers, default: none (falls back to `DEFAULT_UNIVERSE`) | Explicit ticker list to screen (benchmark is auto-added if missing) |
| `--universe-file` | path, default: none | Load tickers from a file instead (e.g. one symbol per line, or a prior `results/basket.json`) |
| `--universe-provider` | str, default: none | Resolve the universe from a registered provider (e.g. an index-membership provider) instead of a static list |
| `--universe-kwargs` | JSON str, default: none | Extra kwargs (as a JSON object string) passed to `--universe-provider` |
| `--benchmark` | str, default `"SPY"` | Symbol used for beta and the correlation regime-shift check |
| `--start` | `YYYY-MM-DD`, default `"2015-01-01"` | History start date |
| `--end` | `YYYY-MM-DD`, default `"2024-12-31"` | History end date |
| `--interval` | str, default `"1d"` | Bar interval passed to the data provider |
| `--min-avg-dollar-volume` | float, default `5,000,000.0` | HARD liquidity gate — excluded before scoring/selection, not just soft-scored |
| `--min-history-years` | float, default `1.0` | HARD history-length gate, distinct from `min_history_years_for_full_credit` (a soft scoring threshold, config-only, not exposed as its own flag) |
| `--max-cluster-correlation` | float, default `0.85` | Threshold above which a pair is flagged as redundant; also the sizing gate for `--select-method threshold` |
| `--no-fund-metadata` | flag, default off (metadata fetched) | Skip the best-effort expense-ratio/AUM lookup (faster, no ETF-quality component) |
| `--top-n` | int, default `8` | How many top-ranked instruments to print by overall selection score |
| `--select-method` | `top_k` \| `cluster` \| `greedy` \| `threshold` \| `max_diversification`, default `"threshold"` | `top_k` (naive baseline, needs `--select-k`), `cluster` (ACC-style representative-per-cluster), `greedy` (Max-Sum Diversification, **requires** `--select-k`), `threshold` (gated by `--max-cluster-correlation`, sizes itself), `max_diversification` (optimizer-based) |
| `--select-k` | int, default: none | Basket size for `top_k`/`greedy` (required for `greedy`) |
| `--select-max-k` | int, default: none | Optional cap on basket size for `threshold`/`max_diversification` (which otherwise size themselves from the data) |
| `--data-provider` | str, default `"yfinance"` | `yfinance`, `csv`, `synthetic`, or a custom registered/module-specifier provider |
| `--data-dir` | path, default: none | Folder path for the `csv` data provider |
| `--no-cache` | flag, default off (cached) | Disable local CSV caching of fetched data |
| `--cache-ttl-days` | float, default: none | Max age, in days, of a cached OHLCV CSV file before it's treated as stale and re-fetched (default: never expire). Cache directory is shared workspace-wide (`<repo_root>/data/`) -- see [`common/README.md`'s "Shared OHLCV cache directory"](../../common/README.md#7-shared-ohlcv-cache-directory) |
| `--no-plots` | flag, default off (plots written) | Skip writing the 3 chart files to `results/` |

### Sample commands (real market data)

```bash
# Default broad-ETF universe, default date range (run from inside pipeline/)
uv run python instrument_selection/run_screener.py

# Explicit universe + benchmark + custom date range
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ AAPL MSFT NVDA GLD TLT --benchmark SPY \
  --start 2018-01-01 --end 2024-12-31

# Universe loaded from a file (e.g. a basket saved by a prior run)
uv run python instrument_selection/run_screener.py \
  --universe-file instrument_selection/results/basket.json --benchmark SPY

# Tighter liquidity/history gates, stricter redundancy threshold
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM EFA EEM GLD SLV USO TLT IEF XLE XLF XLK XLV XLU \
  --min-avg-dollar-volume 20000000 --min-history-years 3 --max-cluster-correlation 0.75

# --select-method top_k (naive baseline, needs --select-k)
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV --select-method top_k --select-k 5

# --select-method cluster (ACC-style representative-per-cluster)
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV USO TLT --select-method cluster

# --select-method greedy (Max-Sum Diversification, --select-k required)
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV USO TLT --select-method greedy --select-k 6

# --select-method threshold (default), with an explicit basket-size cap
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV USO TLT XLE XLF XLK XLV XLU \
  --select-method threshold --select-max-k 10

# --select-method max_diversification
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ IWM DIA EFA EEM GLD SLV USO TLT --select-method max_diversification --select-max-k 8

# Faster run: skip fund-metadata lookup, skip charts, skip the local cache
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ AAPL MSFT NVDA --no-fund-metadata --no-plots --no-cache

# Weekly bars instead of daily, with a data-dir cache override
uv run python instrument_selection/run_screener.py \
  --universe SPY QQQ TLT GLD --interval 1wk --data-provider yfinance
```

Outputs land in `results/`: `screening_report.csv` (every metric and score
per symbol that cleared the hard gates), `correlation_matrix.csv`,
`screened_out.csv` (excluded symbols and why, only written if any were
excluded), and three charts. The chosen basket (per `--select-method`)
prints to stdout — run all four methods side by side on your own universe
to compare, since no head-to-head comparison of them survived this
project's research (see "From scores to a chosen basket" above).

## Data Shapes & Schemas

This project consumes the shared **OHLCV DataFrame** and **universe dict** shapes documented in
`../../common/README.md` (§1–2) — see that file first if you need those. Everything below is unique
to this project.

### `results/screening_report.csv` — one row per symbol that cleared the hard screen

Index: ticker symbol. Columns, grouped by which module computes them:

| Source | Columns |
|---|---|
| `liquidity.py` | `avg_dollar_volume`, `median_dollar_volume`, `median_spread_pct`, `spread_pct_p90` |
| `volatility.py` | `realized_vol_annualized_pct`, `downside_vol_annualized_pct`, `downside_vol_ratio`, `atr_pct_mean`, `vol_of_vol`, `pct_days_vol_regime_change`, `adx_mean`, `pct_days_trending_adx`, `pct_days_ranging_adx` |
| `persistence.py` | `hurst`, `hurst_significant` (bool), `hurst_p_value`, `autocorr_lag1`, `variance_ratio_q5`, `regime_label` (str: `"trending"`/`"mean_reverting"`/`"random_walk_like"`/`"insufficient_data"`) |
| `candlestick.py` | `candlestick_edge`, `candlestick_significant` (bool), `candlestick_p_value`, `candlestick_n_signals`, `candlestick_signal_rate`, `candlestick_label` (str: `"bullish_edge"`/`"bearish_edge"`/`"no_edge"`/`"insufficient_signals"`/`"insufficient_data"`) |
| `momentum.py` | `momentum_edge`, `momentum_significant` (bool), `momentum_p_value`, `momentum_n_windows`, `momentum_lookback_return`, `pct_days_above_trend_ma`, `momentum_label` (str: `"momentum"`/`"reversal"`/`"no_momentum"`/`"insufficient_data"`) |
| `run_screener.py` (computed directly) | `history_years` |
| `run_screener.py` (merged from `correlation.py`) | `avg_correlation_to_universe` |
| `data.py`/`fetch_fund_metadata` (optional, best-effort) | `expense_ratio`, `total_assets` — `NaN` for plain stocks or when metadata lookup fails, never force-filled |
| `scoring.py` (the composite) | `liquidity_score`, `vol_adequacy_score`, `predictability_score`, `momentum_score`, `candlestick_score`, `diversification_score`, `history_adequacy_score`, `etf_expense_score`/`etf_aum_score` (optional, only present when expense/AUM data was fetched), `overall_selection_score` — all `0.0`–`100.0` except the last, which is the documented weighted average (see "The composite score" above) |

### `results/correlation_matrix.csv`

Square matrix: index and columns are both the screened universe's ticker symbols, values are
pairwise Pearson correlation of daily returns (`correlation.py`'s `correlation_matrix()`).

### `results/screened_out.csv` (only written if non-empty)

**Same columns as `screening_report.csv`'s raw metrics ONLY** (`liquidity.py` through
`history_years` in the table above) **plus `screen_fail_reason`** (str, semicolon-joined if more
than one gate failed, e.g. `"liquidity;history"`) — screening runs BEFORE scoring
(`screening.screen_universe()`), so a screened-out row never has the `avg_correlation_to_universe`,
`expense_ratio`/`total_assets`, or any `*_score` column; those are computed only for symbols that
passed.

### `results/basket.json`

```json
{"basket": ["SPY", "QQQ", "..."], "method": "threshold", "date_generated": "2026-01-01T00:00:00Z"}
```

`"method"` is whichever `--select-method` produced this basket. This file is also the shared
"universe hand-off" format `common/universe.py`'s `FileUniverseProvider` reads from any of the
other three projects via `--universe-file` (it accepts a bare list, or a dict with a `"basket"`,
`"symbols"`, `"universe"`, or `"tickers"` key — this project's own writer always uses `"basket"`).

## Testing

```bash
# from inside pipeline/
uv run pytest instrument_selection/tests -v
```

72 tests covering: the Corwin-Schultz spread estimator (non-negative,
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
than a fixed K; `screening.py`'s hard gates (`test_screening.py`): an
illiquid instrument and a too-short-history instrument are each correctly
excluded with the right reason recorded, both reasons are reported together
when an instrument fails on both axes, missing liquidity/history data is
treated as a failure rather than silently passing, a good instrument is
never excluded, the benchmark is exempt from both gates when named (and
correctly NOT exempt when it isn't), and passed/screened-out always
partition the input exactly (nothing is dropped or double-counted);
`candlestick.py` (`test_candlestick.py`): the pattern detectors correctly
find a bullish/bearish engulfing in the appropriate trend and — the key
behavioral check — flag the identical hammer shape as bullish in a downtrend
but bearish (hanging man) in an uptrend, the directional-edge math nets out
the base-rate drift to zero when patterns carry no information, the
significance test flags a deterministically-engineered reversal edge but
does NOT flag a random walk (the exact mirror of the Hurst noise test),
`candlestick_summary` reports `insufficient_data` for too-short series, and
`candlestick_score` gates on significance (an insignificant edge scores below
an identical-magnitude significant one) while a symbol with no candlestick
data still gets a valid, renormalized overall score rather than a penalty;
`momentum.py` (`test_momentum.py`): `roc`/`macd` correctness (ROC equals the
trailing return; MACD is positive in an uptrend and ~0 when flat), the
efficacy test flags a strongly positively-autocorrelated AR(1) series as
`momentum` and a negatively-autocorrelated one as `reversal` (correct sign)
but does NOT flag an iid random walk (the same noise discipline as the Hurst
and candlestick tests), `momentum_summary` reports `insufficient_data` for a
too-short series, and `momentum_score` gates on significance while a symbol
with no momentum data still gets a valid, renormalized overall score.

## Known limitations

- Does not correct for survivorship bias or point-in-time universe
  membership — feeding it today's index constituents and backtesting
  strategies over history inherits that well-documented risk (see above).
- The Hurst significance test uses full-shuffle surrogates (simpler, coarser
  than the academic literature's phase-randomization method) — a
  significant result means "some temporal dependence beyond chance," not
  proof of long-range memory specifically.
- **The candlestick component is intentionally small and expected to be
  near-zero for most liquid instruments** — the weight of rigorous evidence
  (Marshall, Young & Rose 2006; corroborated across markets) is that
  candlestick patterns carry little-to-no exploitable information after
  correcting for data snooping and base-rate drift; only Caginalp & Laurent
  (1998) found strong positive evidence. `candlestick_significance()` uses a
  random-date *placebo* null, not Marshall-Young-Rose's full OHLC bootstrap or
  a data-snooping (SSPA/Reality-Check) correction across patterns, and does
  NOT account for transaction costs — so a significant reading means "this
  instrument's OHLC geometry shows a reversal edge beyond random signal
  placement," a flag to investigate, not a tradable, cost-adjusted edge. The
  pattern detectors also use a single fixed geometry/trend-window convention;
  candlestick definitions vary across authors and the specific thresholds here
  (shadow ratio, small-body fraction, MA trend window) are documented,
  adjustable conventions, not verified-optimal numbers.
- **The momentum component measures a real, heavily-replicated anomaly but is
  deliberately conservative about it.** `momentum_edge` is a single
  past-vs-future serial correlation at one fixed (`lookback`, `horizon`) pair;
  Huang, Li, Wang & Zhou (2020) showed the pooled evidence for time-series
  momentum is statistically fragile and asset-by-asset evidence is weak, so a
  significant reading here means "this instrument's own returns predicted its
  future returns beyond an iid shuffle on this sample," not a validated,
  cost-adjusted, out-of-sample edge. It uses a shuffle null (not a
  data-snooping-robust SSPA/Reality-Check across many lookback/horizon
  choices), ignores transaction costs, and — critically — does not capture
  momentum's well-documented left-tail crash risk (Daniel & Moskowitz 2016:
  momentum returns are severely negatively skewed and crash in post-decline,
  high-volatility panic states). The momentum and Hurst channels also overlap
  conceptually (both detect trending persistence via different estimators);
  this is disclosed and reflected in their shared, capped family weight, not
  hidden. `momentum_lookback_return` is a point-in-time descriptor only and is
  never part of the score. Like `selection.py` and `screening.py`, the
  momentum component was **not run against real market data this session** —
  it is validated only on synthetic AR(1) series with known serial structure
  (the same construction the Hurst tests use).
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
- **`screening.py`'s hard gates are not validated against real market data
  this session either** — same as `selection.py`, synthetic constructed
  universes only.
- Whether screening BEFORE correlation/clustering (this project's choice)
  vs. AFTER changes which instruments end up as cluster representatives or
  greedy diversification picks is an explicitly open question — no source
  found in either research pass addresses it directly for these algorithms.
  The current ordering matches index-provider precedent, not a
  finance-specific proof that order doesn't matter here.
- `min_history_years` (the new hard floor, default 1 year) is a deliberately
  low, permissive default chosen by analogy to the Hurst precedent, not
  itself a number with direct literature support the way
  `min_history_years_for_full_credit` (4 years, ETF-closure-risk research)
  has — tune it for your own data-quality tolerance.

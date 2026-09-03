"""Configuration for the instrument screening/selection tool.

Research grounding (see README.md for full citations/caveats). This tool
does not "prove" any instrument will work with a given strategy -- it
computes the specific, documented metrics practitioners and academics use to
reason about strategy-instrument fit, and is explicit about which numeric
thresholds are well-verified conventions versus which are illustrative
defaults you should tune.
"""

from dataclasses import dataclass, field
from typing import List


DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA",           # broad US equity index ETFs
    "EFA", "EEM",                          # international / emerging markets
    "GLD", "SLV", "USO",                   # commodities
    "TLT", "IEF",                          # bonds
    "XLE", "XLF", "XLK", "XLV", "XLU",     # US sector ETFs
]


@dataclass
class SelectionConfig:
    universe: List[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    benchmark: str = "SPY"
    start: str = "2015-01-01"
    end: str = "2024-12-31"
    interval: str = "1d"

    # --- liquidity ---
    # No single verified numeric $-volume cutoff survived research (liquidity
    # requirements are strategy- and size-dependent) -- this is an adjustable
    # screening floor, not a scientifically "correct" number. But research
    # DID verify that it should act as a HARD GATE, not a soft-scored input:
    # index-provider methodology (MSCI GIMI/Factor Index families) applies
    # liquidity as a binary pass/fail investability screen BEFORE any
    # weighting/optimization step, and the composite-indicator literature
    # (OECD/JRC Handbook) documents "full compensability" as the named risk
    # of folding a non-negotiable tradability requirement into an additive
    # score -- a genuinely illiquid instrument can "buy back" a good
    # composite score with strength on an unrelated dimension. See
    # `screening.screen_universe()`, applied before scoring/selection.
    # Rank-based `liquidity_score` (percentile within the SURVIVING universe)
    # is still used alongside this as a more defensible relative measure for
    # ranking among instruments that already cleared the hard floor.
    min_avg_dollar_volume: float = 5_000_000.0
    liquidity_window: int = 60

    # --- volatility ---
    realized_vol_window: int = 20
    atr_period: int = 14
    # Verified concept: compare a short ATR window to a longer one; a large
    # deviation signals a volatility-regime change (source used 20d vs 60d,
    # 30% deviation as an illustrative trigger for cutting size).
    atr_short_window: int = 20
    atr_long_window: int = 60
    atr_regime_change_threshold: float = 0.30

    # --- trend-persistence / mean-reversion (Hurst exponent) ---
    hurst_min_obs: int = 200            # research: <100-200 obs gives unreliable estimates
    hurst_max_lag_fraction: float = 0.5  # R/S analysis uses chunk sizes up to this fraction of the series
    hurst_neutral_band: float = 0.05     # |H - 0.5| <= this is "not economically meaningful" (research: 0.45-0.55 band)
    hurst_n_surrogates: int = 200        # bootstrap surrogates to test whether H is genuinely different from a random walk

    # --- candlestick-pattern predictability ---
    # A deliberately SMALL, significance-gated component: the weight of
    # rigorous evidence (Marshall, Young & Rose 2006; corroborated across
    # markets) is that candlestick patterns carry little-to-no exploitable
    # information in liquid markets once you correct for data snooping and
    # base-rate drift -- Caginalp & Laurent (1998) is the notable in-favour
    # study whose conditional-probability test this component adapts. So this
    # is scored like Hurst: gated on a placebo/bootstrap significance test,
    # near-zero for most instruments, and a non-zero result flags an unusual
    # instrument to investigate rather than a validated trading edge. See
    # `candlestick.py` for the full research picture and caveats.
    candlestick_horizon: int = 5         # forward-return holding window in bars (research uses 2-10 days)
    candlestick_trend_window: int = 5    # short-MA window for the preceding-trend gate (Caginalp & Laurent used 3-5 days)
    candlestick_min_obs: int = 200       # below this, don't test candlestick edge at all (same discipline as hurst_min_obs)
    candlestick_min_signals: int = 20    # too few detected patterns -> the edge estimate is untrustworthy, score ~0
    candlestick_n_surrogates: int = 200  # placebo/bootstrap draws for the significance null

    # --- time-series-momentum predictability ---
    # A separate "is there exploitable structure?" channel from Hurst: it
    # measures the serial correlation between an instrument's past-return and
    # its future return -- the statistical core of the cross-sectional
    # (Jegadeesh & Titman 1993) and time-series (Moskowitz, Ooi & Pedersen
    # 2012) momentum anomalies, two of the most-replicated findings in finance.
    # It is STILL tested per-instrument against a bootstrap null rather than
    # trusted outright, precisely because Huang, Li, Wang & Zhou (2020) showed
    # the headline pooled-regression t-stat is not statistically reliable and
    # asset-by-asset evidence is weak. So it is scored like Hurst/candlestick:
    # gated on significance, near-zero for many liquid instruments, and always
    # crash-caveated (Daniel & Moskowitz 2016). See `momentum.py`.
    momentum_lookback: int = 252   # trailing-return window (~12 months, the MOP/J&T horizon)
    momentum_horizon: int = 21     # forward-return window the past return is tested against (~1 month, MOP)
    momentum_trend_ma: int = 200   # MA for the descriptive pct-days-above-trend snapshot (classic 200-day filter)
    momentum_min_obs: int = 400    # below this there isn't enough history for a stable lookback/horizon correlation
    momentum_n_surrogates: int = 200  # shuffle-null draws for the significance test

    # --- correlation / diversification ---
    max_cluster_correlation: float = 0.85  # candidates this correlated get flagged as redundant

    # --- ADX-based trend-strength convention (illustrative thresholds, see README) ---
    adx_period: int = 14
    adx_trend_threshold: float = 25.0
    adx_range_threshold: float = 20.0

    # --- history length / fund-metadata (ETF closure-risk research) ---
    # Two distinct thresholds, per the same hard-gate-vs-soft-score research
    # finding as liquidity above: `min_history_years` is a HARD floor --
    # below it, every statistic this tool computes is too unreliable to
    # trust at all (an instrument with a few weeks of data shouldn't be
    # ranked on volatility/correlation/predictability, it should be
    # excluded), applied in `screening.screen_universe()`. Kept deliberately
    # low/permissive (unlike Hurst's own much stricter `hurst_min_obs`
    # floor) since liquidity/volatility are still roughly estimable with far
    # less history than a Hurst significance test needs.
    min_history_years: float = 1.0
    # `min_history_years_for_full_credit` stays a SOFT scoring threshold --
    # a peer-reviewed hazard-model study found ETF closure risk is
    # concentrated in a fund's first three years and recommends individual
    # investors favor ETFs at least 3-4 years old; full credit in
    # `history_adequacy_score` is given at or above this many years, but an
    # instrument between the two thresholds is scored down, not excluded.
    min_history_years_for_full_credit: float = 4.0
    # Best-effort enrichment (expense ratio, AUM) via yfinance metadata --
    # NOT from verified research (that's a data-availability question, not a
    # research claim) and often missing/unreliable, especially for
    # individual stocks. Scoring degrades gracefully when unavailable.
    fetch_fund_metadata: bool = True

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
    # requirements are strategy- and size-dependent) -- these are adjustable
    # screening floors, not scientifically "correct" numbers. Rank-based
    # scoring (percentile within the universe) is used alongside these as a
    # more defensible relative measure.
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

    # --- correlation / diversification ---
    correlation_window: int = 252
    max_cluster_correlation: float = 0.85  # candidates this correlated get flagged as redundant

    # --- ADX-based trend-strength convention (illustrative thresholds, see README) ---
    adx_period: int = 14
    adx_trend_threshold: float = 25.0
    adx_range_threshold: float = 20.0

    # --- history length / fund-metadata (ETF closure-risk research) ---
    # A peer-reviewed hazard-model study found ETF closure risk is
    # concentrated in a fund's first three years and recommends individual
    # investors favor ETFs at least 3-4 years old; longer history also makes
    # every statistic above (Hurst especially) more reliable. Full credit is
    # given at or above this many years of available price history.
    min_history_years_for_full_credit: float = 4.0
    # Best-effort enrichment (expense ratio, AUM) via yfinance metadata --
    # NOT from verified research (that's a data-availability question, not a
    # research claim) and often missing/unreliable, especially for
    # individual stocks. Scoring degrades gracefully when unavailable.
    fetch_fund_metadata: bool = True

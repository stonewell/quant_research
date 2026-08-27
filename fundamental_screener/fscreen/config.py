"""Configuration for the fundamental screener.

Grounding: a conservative value-investing community's valuation framework
(see `docs/snowball_strategy.txt` at the repo root) -- only hold durable,
moat-protected, high-ROE, dividend-paying compounders whose expected return
clears a risk premium over a broad-index benchmark, and sell the moment
that edge decays away. This project applies the document's own criteria
against REAL fundamentals (ROE, dividend yield, earnings growth, debt/
equity from yfinance); the price-only proxy version (for this workspace's
offline/synthetic testing policy) lives instead in
`research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy`.
"""

from dataclasses import dataclass, field
from typing import List

# Illustrative, unverified blue-chip basket -- "moat"/"ROE"/"dividend
# policy" are single-company traits, so this defaults to individual stocks
# rather than this workspace's usual broad-ETF universes. Not a curated
# reproduction of the source document's own selection criteria.
DEFAULT_CANDIDATE_UNIVERSE = ["KO", "PG", "JNJ", "MSFT", "COST", "WMT", "MCD", "PEP"]
DEFAULT_BENCHMARK_SYMBOL = "SPY"


@dataclass
class ScreenerConfig:
    universe: List[str] = field(default_factory=lambda: list(DEFAULT_CANDIDATE_UNIVERSE))
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL
    top_n: int = 5

    # Doc's own Model 2 formula: expected_return = earnings_growth + dividend_yield.
    # required_return is the doc's normal-environment buy hurdle (~12%).
    required_return: float = 0.12

    # Quality gate (doc's moat/high-ROE/dividend/growth screen, applied to real fundamentals).
    min_roe: float = 0.15
    min_dividend_yield: float = 0.0        # must be > 0 (i.e. must actually pay a dividend)
    max_debt_to_equity: float = 150.0      # yfinance's own debtToEquity scale -- see common/data.py's caveat
    min_earnings_growth: float = 0.05      # doc's explicit >=5% 5yr-CAGR requirement (yfinance growth field is a proxy)

    # Benchmark comparator (sell trigger) is price-based -- an index's own
    # "expected return" is inherently a price/total-return concept, and
    # needs real OHLCV history, not fundamentals.
    lookback_days: int = 1260              # ~5 years, mirrors the doc's own 5-year framing

    def __post_init__(self):
        if self.top_n <= 0:
            raise ValueError(f"ScreenerConfig.top_n must be > 0, got {self.top_n}")
        if not self.benchmark_symbol:
            raise ValueError("ScreenerConfig.benchmark_symbol must be a non-empty string")
        if not self.universe:
            raise ValueError("ScreenerConfig.universe must be a non-empty list")

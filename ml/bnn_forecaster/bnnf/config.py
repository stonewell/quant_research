"""Configuration for the AutoBNN-based forecaster.

Grounding: AutoBNN (Google Research, https://research.google/blog/autobnn-probabilistic-time-series-forecasting-with-compositional-bayesian-neural-networks/)
fits a compositional Bayesian Neural Network per time series and produces a
calibrated median + confidence-interval forecast, built from interpretable
components (trend, periodic, changepoint) rather than a black-box net. This
project applies the SAME "beat the benchmark" philosophy already used by
`research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy` (price-only
technical proxy) and `fundamental_screener` (real ROE/dividend/earnings-growth),
but with a genuine probabilistic price forecast as the expected-return signal
instead of a momentum proxy or real fundamentals.
"""

from dataclasses import dataclass, field
from typing import List

# Illustrative, unverified blue-chip basket -- same as research_strategy's
# CompounderMarginOfSafetyStrategy and fundamental_screener's ScreenerConfig,
# for consistency across this workspace's three "beat the benchmark" strategies.
DEFAULT_CANDIDATE_UNIVERSE = ["KO", "PG", "JNJ", "MSFT", "COST", "WMT", "MCD", "PEP"]
DEFAULT_BENCHMARK_SYMBOL = "SPY"


@dataclass
class ForecasterConfig:
    universe: List[str] = field(default_factory=lambda: list(DEFAULT_CANDIDATE_UNIVERSE))
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL
    top_n: int = 5

    horizon_days: int = 21          # forecast horizon (~1 trading month), also the annualization base
    lookback_days: int = 756        # ~3 years of history to fit each symbol's BNN on

    # AutoBNN estimator knobs -- "map" (Maximum A Posteriori) is the cheapest
    # of AutoBNN's 3 estimator types (mcmc/vi are much more expensive); width
    # and num_iters are reduced from AutoBNN's own defaults (width=50,
    # num_iters=5000) for practical runtime in a workspace with no GPU/TPU
    # assumption -- disclosed as a speed/quality trade-off, not a hidden one.
    estimator: str = "map"
    width: int = 10
    num_iters: int = 1000
    num_particles: int = 4
    seed: int = 42

    # Buy/sell thresholds -- same shape as fundamental_screener.ScreenerConfig:
    # a confidence gate (is the forecast interval narrow enough to trust) plus
    # an expected-return-vs-benchmark hurdle.
    #
    # PLACEHOLDER, NOT EMPIRICALLY CALIBRATED: see bnnf/forecasting.py's own
    # docstring -- this module's own experimentation found real ci_width
    # output routinely in the hundreds-to-thousands-of-percent range with
    # the default model structure/likelihood, i.e. this default threshold
    # may never actually gate anything as "confident" without further
    # tuning. Inspect a real run's ci_width values before trusting this.
    required_return: float = 0.10   # annualized median-forecast hurdle to buy
    max_ci_width: float = 0.30      # 97.5/2.5 quantile spread ceiling (annualized) to count as "confident"

    def __post_init__(self):
        if self.top_n <= 0:
            raise ValueError(f"ForecasterConfig.top_n must be > 0, got {self.top_n}")
        if not self.benchmark_symbol:
            raise ValueError("ForecasterConfig.benchmark_symbol must be a non-empty string")
        if not self.universe:
            raise ValueError("ForecasterConfig.universe must be a non-empty list")
        if self.horizon_days <= 0:
            raise ValueError(f"ForecasterConfig.horizon_days must be > 0, got {self.horizon_days}")
        if self.lookback_days <= self.horizon_days:
            raise ValueError(
                f"ForecasterConfig.lookback_days ({self.lookback_days}) must exceed horizon_days "
                f"({self.horizon_days}) -- there must be more training history than the forecast reaches."
            )

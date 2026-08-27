"""BNN Forecaster Package -- probabilistic price forecasting via AutoBNN
(compositional Bayesian Neural Networks), adapted to this workspace's
"beat the benchmark" strategy philosophy (see docs/snowball_strategy.txt
and its two other siblings: research_strategy's CompounderMarginOfSafetyStrategy
and fundamental_screener's FundamentalMarginOfSafetyStrategy)."""

from .config import ForecasterConfig

__all__ = ["ForecasterConfig"]

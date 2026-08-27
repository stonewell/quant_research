"""Shared factor-category vocabulary used to tag strategies/templates across
this workspace's `research_strategy` and `strategy_generator` projects.

This is the ONE canonical taxonomy both projects reference when tagging
their own strategies/templates (`pipeline/research_strategy/strategies_config.json`'s
`"factors"` key; `common/allocation_templates.py`'s `factor_tags` field) --
having a single shared vocabulary is what makes `research_strategy`'s
factor-summary output and `strategy_generator`'s `--factor-report` consumer
(see `pipeline/strategy_generator/stratgen/generator.py`) mean the same thing by a
given tag, rather than drifting into inconsistent spellings independently.
"""

FACTOR_CATEGORIES = {
    "absolute_momentum_trend": "An asset's own trailing trend/return sign (SMA gate, ROC > 0 gate).",
    "relative_momentum": "Cross-sectional ranking of assets by trailing return.",
    "volatility_targeting": "Realized volatility used to size/scale exposure (inverse-vol weighting, vol targeting, ATR sizing).",
    "mean_reversion": "Short-term oscillator (RSI) signaling overbought/oversold reversal.",
    "breadth": "Aggregate count/fraction of a basket in positive momentum, used as a market-wide risk-on/off signal.",
    "correlation_diversification": "Covariance/correlation structure used for portfolio construction (HRP, minimum-variance, max diversification).",
    "regime_trend_strength": "Trend-strength/regime classification (ADX, Hurst) gating whether trend-following applies.",
    "static_fixed_weight": "Fixed weights with no adaptive signal.",
}

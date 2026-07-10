"""Configuration for the regime-switching ensemble strategy.

Research grounding (see README.md for full citations/caveats):
- The *rationale* for combining trend-following and mean-reversion is
  well-supported: they're documented as complementary because each style
  dominates in different regimes (trend in sustained directional moves,
  mean-reversion in calm/range-bound conditions), and trend-following has a
  convex, option-straddle-like payoff that showed up concretely in 2022 when
  stock-bond correlation broke down.
- CRITICAL HONESTY FLAG: no verified backtest of a combined trend+mean-
  reversion system on SPY/QQQ survived adversarial fact-checking during
  research -- every specific blog-reported CAGR/Sharpe/drawdown number for
  such a combination was refuted. This implementation exists specifically to
  generate that missing out-of-sample evidence ourselves (see README for the
  actual results), not to reproduce a validated published result.
- Regime-detection rules used here ARE individually documented (though their
  *efficacy*, not just their definition, is unverified): a 200-day SMA trend
  gate, ADX >= 25 signaling a trending regime, ADX <= 20 signaling a ranging
  regime, and a hysteresis band (20 < ADX < 25 carries the prior regime
  forward) specifically to reduce the whipsaw/false-switching risk that
  research flagged as a major pitfall of regime-switching systems.
- Look-ahead discipline (verified as "the cardinal sin" of regime-switching
  backtests): every regime/indicator value used to decide a bar's action is
  computed from data available BEFORE that bar (shifted by 1), and execution
  happens at the following bar's open.
"""

from dataclasses import dataclass


@dataclass
class EnsembleConfig:
    # --- instrument / data ---
    symbol: str = "SPY"
    start: str = "2000-01-01"
    end: str = "2024-12-31"
    interval: str = "1d"

    # --- capital ---
    initial_capital: float = 100_000.0

    # --- mode: which sleeve(s) are active (lets the CLI decompose the ensemble
    #     into its standalone components for a fair combination-vs-parts test) ---
    # "ensemble"    - full 3-regime system (trend-following + tactical mean-reversion + cash)
    # "trend_only"  - the trend-following sleeve alone (invested iff price > rising 200-day SMA, cash otherwise)
    # "meanrev_only"- the RSI(2) mean-reversion sleeve alone (same trend gate, but RSI(2) entries/exits instead of buy-and-hold)
    mode: str = "ensemble"

    # --- long-term trend gate (shared by all sleeves; downtrend always means cash) ---
    trend_ma_period: int = 200

    # --- ADX-based trend-strength sub-regime (only relevant to "ensemble" mode) ---
    adx_period: int = 14
    adx_trend_threshold: float = 25.0   # ADX >= this: trend-following sleeve takes over
    adx_range_threshold: float = 20.0   # ADX <= this: mean-reversion sleeve takes over
    # between the two thresholds: carry the previous sub-regime forward (hysteresis, reduces whipsaw)

    # --- tactical mean-reversion sleeve (RSI(2), Connors-style; same thresholds as the rsi_strategy project) ---
    rsi_period: int = 2
    entry_rsi_threshold: float = 10.0
    exit_rsi_threshold: float = 70.0

    # --- costs ---
    commission_per_trade: float = 1.0
    commission_pct: float = 0.0005
    slippage_pct: float = 0.0005

    # --- backtest mechanics ---
    warmup_bars: int = 210

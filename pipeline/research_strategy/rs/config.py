"""Configuration for Researched Quantitative Trading Strategies.

Grounding:
1. Dual Momentum GTAA + Risk Parity: Antonacci (2014, JPM), Faber (2007, JWM).
2. Bold Asset Allocation (BAA-G12): Wouter J. Keller (2022 SSRN).
3. Volatility-Managed Portfolios: Moreira & Muir (2017, Journal of Finance).
4. Accelerating Dual Momentum: Ludlow & Hanly (2018, EngineeredPortfolio.com).
5. Vigilant Asset Allocation (VAA-G4): Keller & Keuning (2017 SSRN #3002624).
6. RSI(2) mean-reversion, trend-pullback swing, ATR-adaptive grid, and
   regime-switching ensemble: ported from this workspace's former
   `rsi_strategy`, `swing_trend_strategy`, `grid_trading`, and
   `ensemble_strategy` side projects (see `rs/strategy.py`
   for the full research grounding each one carried, and the close-based
   approximation each port discloses relative to its original event-driven,
   intrabar-aware backtester).
7. Chan Pivot Shift: an original, from-scratch reading of 缠中说禅 (Chan
   theory) written natively for this project -- NOT a port of, or
   dependency on, the third-party `czsc` Rust/Python library. See
   `rs/chan_structure.py` for the structure detector and its disclosed
   simplifications.
"""

import json
import os
import warnings
from dataclasses import dataclass, field, fields
from typing import List, Optional

from common.factor_taxonomy import FACTOR_CATEGORIES

DEFAULT_RISKY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"]
DEFAULT_CASH_PROXY = "BIL"

# Vigilant Asset Allocation (VAA-G4) universes. NOTE: Keller & Keuning's own
# published offensive/defensive ticker list could NOT be confirmed with high
# confidence -- two different candidate universes attributed to the paper by
# secondary sources were independently checked and refuted. These defaults
# are illustrative only, not a verified reproduction of the original paper's
# universe; substitute your own before treating results as a paper replication.
DEFAULT_VAA_OFFENSIVE = ["SPY", "QQQ", "EFA", "EEM"]
DEFAULT_VAA_DEFENSIVE = ["IEF", "BIL"]

# Protective Asset Allocation (PAA, Keller & Keuning 2016) universe. The
# original paper's 12-asset universe includes VGK (Europe) and EWJ (Japan)
# separately; this project consolidates both into the existing EFA
# (developed ex-US broad) holding already used by every other strategy here,
# trading one region-level split of granularity for consistency with the
# rest of this project's default universe -- a disclosed simplification, not
# a verified reproduction of the original 12-asset list. HYG (high-yield
# credit) has no substitute among the other strategies' universes and is
# kept as-is.
DEFAULT_PAA_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "HYG", "LQD", "TLT"]
DEFAULT_PAA_PROTECTION_SYMBOL = "IEF"

# Adaptive Asset Allocation (AAA, Butler/Philbrick/Gordillo/Varadi 2012)
# universe. The original 10-asset universe splits developed ex-US equity
# into EZU (Eurozone) + EWJ (Japan) and includes a dedicated international
# REIT sleeve (RWX); this project consolidates the former into EFA (same
# simplification as PAA above) and drops the latter (no international-REIT
# proxy exists elsewhere in this project's default universe, and reusing
# VNQ -- US REITs -- for both slots would misrepresent it as two distinct
# asset classes). The resulting 8-asset universe is a disclosed, reduced
# version of the original -- pass a custom `aaa_universe` (e.g. via
# --universe-kwargs or a custom strategies_config.json) for a faithful
# 10-asset reproduction against real market data.
DEFAULT_AAA_UNIVERSE = ["SPY", "EFA", "EEM", "VNQ", "IEF", "TLT", "DBC", "GLD"]

# Hybrid Asset Allocation (HAA, Keller & Keuning 2023) universes.
DEFAULT_HAA_OFFENSIVE = ["SPY", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT"]
DEFAULT_HAA_DEFENSIVE = ["IEF", "BIL"]

# Defensive Asset Allocation (DAA, Keller & Keuning 2018) universes.
DEFAULT_DAA_CANARY = ["VWO", "BND"]
DEFAULT_DAA_RISKY = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]
DEFAULT_DAA_DEFENSIVE = ["IEF", "LQD", "BIL"]


@dataclass
class StrategyConfig:
    rebalance_freq_days: int = 21          # Monthly trading cadence (~21 trading days)

    # Universes
    risky_universe: List[str] = field(default_factory=lambda: list(DEFAULT_RISKY_UNIVERSE))
    cash_proxy: str = DEFAULT_CASH_PROXY

    # Dual Momentum & Risk Parity parameters
    trend_sma_period: int = 200            # 200-day SMA trend gate
    mom_short_lookback: int = 63           # ~3 months (63 trading days)
    mom_long_lookback: int = 126           # ~6 months (126 trading days)
    vol_lookback: int = 60                 # ~3 months rolling window for realized volatility
    top_k: int = 3                         # Select top K assets by momentum score

    # Accelerating Dual Momentum (Ludlow & Hanly 2018) parameters. Fixed
    # 4-ETF universe per the published rule: SPY vs. SCZ on relative +
    # absolute momentum, falling back to whichever of TLT/TIP has the
    # better trailing 1-month return when neither equity sleeve qualifies.
    adm_equity_a: str = "SPY"
    adm_equity_b: str = "SCZ"
    adm_bond_a: str = "TLT"
    adm_bond_b: str = "TIP"

    # Vigilant Asset Allocation (VAA-G4) parameters -- see
    # DEFAULT_VAA_OFFENSIVE/DEFAULT_VAA_DEFENSIVE above for the universe
    # caveat. The 13612W momentum formula and T=1/B=1 binary switch
    # (Keller & Keuning 2017) are well-verified and NOT affected by that
    # caveat -- only the specific tickers are illustrative.
    vaa_offensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_VAA_OFFENSIVE))
    vaa_defensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_VAA_DEFENSIVE))

    # --- RSI(2) mean-reversion (ported from `rsi_strategy`) ---
    # Entry: price > 200d SMA (trend filter) and RSI(2) < oversold_threshold
    # (or cumulative RSI(2) over `rsi_cumulative_lookback` bars < threshold).
    # Exit rule has NO verified consensus per the original project's
    # research (only the entry + trend filter are well-verified) -- treat
    # exit_mode/exit_rsi_threshold as an open, empirically-tuned parameter.
    rsi_symbol: str = "SPY"
    rsi_period: int = 2
    rsi_method: str = "wilder"            # "wilder" or "cutler"
    rsi_entry_mode: str = "single"        # "single" or "cumulative"
    rsi_oversold_threshold: float = 10.0
    rsi_cumulative_lookback: int = 2
    rsi_cumulative_threshold: float = 10.0
    rsi_require_trend_filter: bool = True
    rsi_trend_ma_period: int = 200
    rsi_exit_mode: str = "rsi_cross"      # "rsi_cross", "ma_cross", or "either"
    rsi_exit_rsi_threshold: float = 70.0
    rsi_exit_ma_period: int = 5
    rsi_stop_loss_pct: Optional[float] = None   # None disables
    rsi_max_holding_days: Optional[int] = 10    # None disables
    rsi_position_size_pct: float = 1.0

    # --- Trend-pullback swing (ported from `swing_trend_strategy`) ---
    # Entry: close > rising 200d SMA (uptrend), close < 20d SMA (pullback),
    # 5-period RSI < 45. Only the flat "equity_pct" position-sizing mode is
    # ported -- the original's "risk_based" per-trade sizing (stop-distance
    # -> position size) doesn't fit a flat target-weight schedule.
    swing_symbol: str = "SPY"
    swing_trend_ma_period: int = 200
    swing_require_rising_trend_ma: bool = True
    swing_trend_slope_lookback: int = 20
    swing_pullback_ma_period: int = 20
    swing_rsi_period: int = 5
    swing_entry_rsi_threshold: float = 45.0
    swing_exit_rsi_threshold: float = 90.0    # high on purpose -- see original project's research notes:
    # set above the verified 65 so the trailing stop / profit target / time
    # cap do most of the exit work instead of an early RSI-cross exit.
    swing_stop_loss_pct: float = 0.05
    swing_reward_risk_ratio: float = 3.0      # profit_target_pct = stop_loss_pct * reward_risk_ratio
    swing_use_trailing_stop: bool = True
    swing_trailing_activate_pct: float = 0.07
    swing_trailing_stop_pct: float = 0.04
    swing_max_holding_days: Optional[int] = 63
    swing_position_size_pct: float = 1.0

    # --- ATR-adaptive grid trading (ported from `grid_trading`) ---
    # Long-only buy-low/sell-high grid, spacing scaled by ATR% (clipped to a
    # floor/ceiling), gated by a trend filter and an equity drawdown circuit
    # breaker. See `AdaptiveGridStrategy`'s docstring for the CLOSE-based
    # (vs. the original's intrabar High/Low) approximation this port makes.
    grid_symbol: str = "SPY"
    grid_atr_period: int = 14
    grid_atr_multiplier: float = 1.0
    grid_min_spacing_pct: float = 0.01
    grid_max_spacing_pct: float = 0.06
    grid_levels_per_side: int = 6
    grid_regrid_breakout_mult: float = 2.0
    grid_regrid_on_profit_cycle: bool = True
    grid_position_size_pct: float = 0.015     # equity fraction risked per grid slot
    grid_max_open_slots: int = 5
    grid_capital_reserve_pct: float = 0.4     # fraction of capital kept out of active grids
    grid_trend_ma_period: int = 100
    grid_trend_band_pct: float = 0.03
    grid_drawdown_stop_pct: float = 0.10
    grid_cooldown_bars_after_stop: int = 10

    # --- Regime-switching ensemble (ported from `ensemble_strategy`) ---
    # Trend-following (buy-and-hold) when ADX signals a trending regime
    # above a rising 200d SMA, tactical RSI(2) mean-reversion when
    # range-bound, cash below the 200d SMA. `ensemble_mode` selects which
    # sleeve(s) are active, matching the original project's
    # component-decomposition CLI ("ensemble", "trend_only", "meanrev_only").
    ensemble_symbol: str = "SPY"
    ensemble_mode: str = "ensemble"
    ensemble_trend_ma_period: int = 200
    ensemble_adx_period: int = 14
    ensemble_adx_trend_threshold: float = 25.0
    ensemble_adx_range_threshold: float = 20.0
    ensemble_rsi_period: int = 2
    ensemble_entry_rsi_threshold: float = 10.0
    ensemble_exit_rsi_threshold: float = 70.0
    ensemble_min_weight_change: float = 0.02

    # --- Protective Asset Allocation (Keller & Keuning 2016, SSRN #2759734) ---
    # Breadth-based, continuously-scaled crash protection: the fraction of
    # capital sent to `paa_protection_symbol` grows smoothly from 0% (all N
    # risky assets in positive momentum) to 100% (at or below `n1` assets in
    # positive momentum, where n1 = paa_protection_factor * N / 4). The
    # remainder splits equally across the paa_top_k highest-momentum risky
    # assets (ranked, not gated -- selected regardless of individual sign).
    # NOTE: this project could not independently verify the original paper's
    # exact bond-fraction formula/constants against the primary SSRN source
    # this session -- see rs/strategy.py's ProtectiveAssetAllocation
    # docstring for the honest caveat on what's verified vs. reconstructed.
    paa_universe: List[str] = field(default_factory=lambda: list(DEFAULT_PAA_UNIVERSE))
    paa_protection_symbol: str = DEFAULT_PAA_PROTECTION_SYMBOL
    paa_momentum_lookback: int = 252   # ~12 months of daily bars, adapting the paper's monthly 13-point SMA
    paa_top_k: int = 6
    paa_protection_factor: int = 1     # a in {0, 1, 2}; a=1 matches AllocateSmartly's published PAA variant

    # --- Adaptive Asset Allocation (Butler/Philbrick/Gordillo/Varadi 2012, SSRN #2328254) ---
    # Two-stage: (1) momentum filter keeps the top aaa_top_k of the universe
    # by aaa_momentum_lookback-day return; (2) minimum-variance optimization
    # (long-only, weights sum to 1) on the survivors, using a covariance
    # matrix built from aaa_corr_lookback-day correlation combined with
    # aaa_vol_lookback-day (shorter, more responsive) volatility -- the
    # paper's own "hybrid" covariance construction. Positions below
    # aaa_min_weight_pct are dropped and the remainder renormalized.
    aaa_universe: List[str] = field(default_factory=lambda: list(DEFAULT_AAA_UNIVERSE))
    aaa_momentum_lookback: int = 126   # 6 months
    aaa_top_k: int = 4                 # half of an 8-asset universe, preserving the paper's "keep half" rule
    aaa_vol_lookback: int = 20
    aaa_corr_lookback: int = 126
    aaa_min_weight_pct: float = 0.02

    # --- Turtle Channel Breakout Strategy (Dennis & Eckhardt / Donchian) ---
    turtle_symbol: str = "SPY"
    turtle_entry_breakout_days: int = 20
    turtle_exit_breakout_days: int = 10
    turtle_atr_period: int = 20
    turtle_atr_stop_mult: float = 2.0
    turtle_require_trend_filter: bool = True
    turtle_trend_ma_period: int = 200
    turtle_position_sizing_mode: str = "inverse_atr"  # "inverse_atr" or "equal_weight"
    turtle_min_weight_change: float = 0.02

    # --- Chan Pivot Shift (original, from-scratch reading of 缠中说禅/Chan
    # theory -- see rs/chan_structure.py; NOT ported from the `czsc`
    # library). Entry: the price pivot (consolidation range) steps up to a
    # wholly higher band and a confirming pullback low forms. Exit: a
    # symmetric downward pivot shift, a stroke-over-stroke momentum-
    # divergence proxy, a stop-loss, or a max-holding-days timeout. ---
    chan_symbol: str = "SPY"
    chan_min_gap_bars: int = 4       # min merged-bar gap between a stroke's fractals (independence rule)
    chan_min_strokes: int = 3        # min consecutive overlapping strokes to seed a pivot (theory minimum: 3)
    chan_stop_loss_pct: Optional[float] = 0.08   # None disables
    chan_max_holding_days: Optional[int] = 90    # None disables
    chan_position_size_pct: float = 1.0

    # --- Chan Three-Type Buy/Sell Points (additive extension of Chan Pivot
    # Shift above -- see rs/chan_signals.py; NOT a modification of
    # ChanPivotShiftStrategy or chan_structure.py, which are left untouched).
    # Builds segments (线段) on top of strokes, segment-level pivots (中枢),
    # real MACD-histogram-area divergence (背驰, common.indicators.macd), and
    # the formal 一/二/三类买卖点 taxonomy. Entry/exit is an unconditional OR
    # of all three buy/sell-point types respectively (no per-type toggle in
    # this first version). ---
    chan3_symbol: str = "SPY"
    chan3_min_gap_bars: int = 4
    chan3_min_strokes: int = 3
    chan3_macd_fast: int = 12
    chan3_macd_slow: int = 26
    chan3_macd_signal: int = 9
    chan3_stop_loss_pct: Optional[float] = 0.08   # None disables
    chan3_max_holding_days: Optional[int] = 90    # None disables
    chan3_position_size_pct: float = 1.0
    chan3_include_stroke_signals: bool = True
    chan3_max_single_position: float = 0.20        # Hard maximum weight per individual stock (default: 20%)
    chan3_min_weight_change: float = 0.02          # Asset inertia filter threshold (default: 2%)

    # --- Chan Pivot Shift (MACD) (additive copy of Chan Pivot Shift above --
    # see rs/chan_signals.py's compute_chan_pivot_macd_signals; NOT a
    # modification of ChanPivotShiftStrategy). Same stroke-based
    # pivot-band-shift buy/sell rule, but with the disclosed stroke-slope
    # "momentum divergence proxy" replaced by real MACD-histogram-area
    # divergence (common.indicators.macd), made symmetric: a top-divergence
    # sell AND a bottom-divergence buy (the original proxy was sell-only). ---
    chanm_symbol: str = "SPY"
    chanm_min_gap_bars: int = 4
    chanm_min_strokes: int = 3
    chanm_macd_fast: int = 12
    chanm_macd_slow: int = 26
    chanm_macd_signal: int = 9
    chanm_stop_loss_pct: Optional[float] = 0.08   # None disables
    chanm_max_holding_days: Optional[int] = 90    # None disables
    chanm_position_size_pct: float = 1.0

    # --- Chan Multi-Timeframe Trend (Chan MTF) ---
    chan_mtf_trend_ma_period: int = 200
    chan_mtf_min_gap_bars: int = 4
    chan_mtf_min_strokes: int = 3
    chan_mtf_stop_loss_pct: Optional[float] = 0.08
    chan_mtf_max_holding_days: Optional[int] = 90
    chan_mtf_position_size_pct: float = 1.0
    chan_mtf_pivot_osc_size_pct: float = 0.5   # reduced size when Lesson 107's precise-trend gate hasn't confirmed
    chan_mtf_require_weekly_regime: bool = False

    # --- Chan Trend Third Buy (B3 Breakout Retest) ---
    chan_b3_min_gap_bars: int = 4
    chan_b3_min_strokes: int = 3
    chan_b3_macd_fast: int = 12
    chan_b3_macd_slow: int = 26
    chan_b3_macd_signal: int = 9
    chan_b3_stop_loss_pct: Optional[float] = 0.08
    chan_b3_max_holding_days: Optional[int] = 90
    chan_b3_position_size_pct: float = 1.0

    # --- Chan Mean-Reversion Divergence (B1 + 防狼术 Risk Controls) ---
    chan_mrd_min_gap_bars: int = 4
    chan_mrd_min_strokes: int = 3
    chan_mrd_macd_fast: int = 12
    chan_mrd_macd_slow: int = 26
    chan_mrd_macd_signal: int = 9
    chan_mrd_entry_mode: str = "raw_b1"   # "raw_b1", "zero_axis", "failed_retest", "combined"
    chan_mrd_confirm_window_bars: int = 20
    chan_mrd_require_trend_filter: bool = True
    chan_mrd_trend_ma_period: int = 200
    chan_mrd_stop_loss_pct: Optional[float] = 0.05
    chan_mrd_profit_target_pct: Optional[float] = 0.15
    chan_mrd_trailing_stop_pct: Optional[float] = 0.04
    chan_mrd_trailing_activate_pct: Optional[float] = 0.08
    chan_mrd_max_holding_days: Optional[int] = 30
    chan_mrd_position_size_pct: float = 1.0

    # --- Chan Composite (Multi-Stage Position Scaling B1/B2/B3) ---
    chan_comp_min_gap_bars: int = 4
    chan_comp_min_strokes: int = 3
    chan_comp_macd_fast: int = 12
    chan_comp_macd_slow: int = 26
    chan_comp_macd_signal: int = 9
    chan_comp_b1_weight: float = 0.30
    chan_comp_b2_weight: float = 0.40
    chan_comp_b3_weight: float = 0.30
    chan_comp_stop_loss_pct: Optional[float] = 0.08
    chan_comp_max_holding_days: Optional[int] = 90
    chan_comp_allow_flat_b2_b3: bool = True
    chan_comp_max_single_position: float = 0.20    # Hard maximum weight per individual stock (default: 20%)
    chan_comp_min_weight_change: float = 0.02      # Asset inertia filter threshold (default: 2%)
    chan_comp_use_structural_stops: bool = True   # When True, enforces deterministic structural invalidation stops (Table 3)

    # --- Chan Four-State Execution Strategy (chan_four_state_execution) ---
    # Industrial-grade Chan execution strategy implementing the 4-state operational machine
    # (BUY_CANDIDATE, HOLD, HOLD_ALERT, SELL_EXIT, WAIT_OBSERVE) with deterministic structural
    # invalidation stops (1B bar low, 2B dd low, 3B zg pivot high), ratcheting trailing stop to ZG,
    # moving average entanglement filter, and Lesson 16 zero-consolidation drag.
    chan_fse_min_gap_bars: int = 4
    chan_fse_min_strokes: int = 3
    chan_fse_macd_fast: int = 12
    chan_fse_macd_slow: int = 26
    chan_fse_macd_signal: int = 9
    chan_fse_stop_loss_pct: Optional[float] = 0.08
    chan_fse_max_holding_days: Optional[int] = 90
    chan_fse_exit_on_consolidation: bool = True
    chan_fse_trail_stop_to_zg: bool = True
    chan_fse_use_ma_filter: bool = True
    chan_fse_max_single_position: float = 0.20
    chan_fse_min_weight_change: float = 0.04
    chan_fse_min_hold_bars: int = 5
    chan_fse_zg_tolerance_pct: float = 0.025
    chan_fse_cons_timeout_bars: int = 8
    chan_fse_stop_evaluation_mode: str = "close"
    chan_fse_b1_buffer_pct: float = 0.03
    chan_fse_cooldown_bars: int = 4
    chan_fse_two_stage_entry: bool = True
    chan_fse_use_breadth_filter: bool = True
    chan_fse_breadth_bull_thresh: float = 0.30
    chan_fse_adx_filter: bool = True
    chan_fse_adx_threshold: float = 20.0
    chan_fse_adx_period: int = 14

    # --- Chan Best Selector Meta-Strategy ---
    chan_best_lookback_days: int = 63
    chan_best_metric: str = "sharpe"  # "sharpe" or "cagr"
    chan_best_rebalance_freq_days: int = 21
    chan_best_min_weight_change: float = 0.02

    # --- Chan VAA Compound Strategy ---
    # Macro regime crash protection compounder: dynamically shifts allocation
    # between Chan structural trend following (bullish alpha) and VAA-G4 dual
    # momentum (crash defense with crash protection threshold).
    chan_vaa_chan_weight: float = 0.60
    chan_vaa_mode: str = "regime_adaptive"   # "regime_adaptive" or "fixed_blend"
    chan_vaa_defensive_boost: bool = True
    chan_vaa_gate_chan_in_defensive: bool = False
    chan_vaa_rebalance_freq_days: int = 21
    chan_vaa_min_weight_change: float = 0.02
    chan_vaa_offensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_VAA_OFFENSIVE))
    chan_vaa_defensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_VAA_DEFENSIVE))

    # --- Chan Pivot Shift (MACD) Advanced (ChanPivotShiftMACDAdvStrategy):
    # an enhanced SIBLING of Chan Pivot Shift (MACD) above -- that strategy/
    # its config (chanm_*) are left completely untouched. Adds (Lessons
    # 92-99/103/024/027/033/061): a MACD zero-axis entry gate, weekly-level
    # 区间套 re-confirmation, a dangerous pivot-relation exit overlay, a
    # 盘整背驰-vs-背驰 divergence-strength filter, an opt-in volume
    # confirmation, and coincidence-based position sizing. See
    # rs/chan_lesson_strategies.py. ---
    chanm_adv_min_gap_bars: int = 4
    chanm_adv_min_strokes: int = 3
    chanm_adv_macd_fast: int = 12
    chanm_adv_macd_slow: int = 26
    chanm_adv_macd_signal: int = 9
    chanm_adv_stop_loss_pct: Optional[float] = 0.08
    chanm_adv_max_holding_days: Optional[int] = 90
    chanm_adv_position_size_pct: float = 1.0
    chanm_adv_require_cross_pivot_divergence: bool = True
    chanm_adv_require_volume_confirmation: bool = False
    chanm_adv_weak_signal_position_size_pct: float = 0.5
    chanm_adv_use_trend_gate: bool = True
    chanm_adv_trend_ma_period: int = 200
    chanm_adv_suppress_top_div_in_uptrend: bool = True
    chanm_adv_trailing_activate_pct: Optional[float] = 0.08
    chanm_adv_trailing_stop_pct: Optional[float] = 0.04
    chanm_adv_require_weekly_regime: bool = False

    # --- Chan Pivot-Oscillation Monitor (Zn, Lesson 92, 0844-...-092.md):
    # tracks each sub-swing's midpoint (Zn) inside a confirmed stroke-level
    # pivot vs. the pivot's own center (Z) to detect a strengthening/
    # weakening bias, and flags a breakout beyond the pivot band that
    # immediately reverts as a wedge bull-/bear-trap (disclosed
    # simplification of the lesson's fuller 3rd-buy/sell check) -- see
    # rs/chan_lesson_strategies.py. ---
    pivot_osc_min_gap_bars: int = 4
    pivot_osc_min_strokes: int = 3
    pivot_osc_trap_confirm_bars: int = 6
    pivot_osc_stop_loss_pct: Optional[float] = 0.08
    pivot_osc_max_holding_days: Optional[int] = 90
    pivot_osc_position_size_pct: float = 1.0

    # --- Chan Fibonacci MA Sector-Strength Rotation (Lesson 106,
    # 1053-...-106.md): ranks every symbol into a tier (0-8) by how many
    # Fibonacci-period SMAs (5/13/21/34/55/89/144/233) it currently trades
    # above, then rotates capital into the top-tier symbols -- a
    # cross-sectional selection/rotation strategy, not single-symbol timing.
    # See rs/chan_lesson_strategies.py. ---
    fibo_top_k: int = 3
    fibo_min_tier: int = 3
    fibo_rebalance_freq_days: int = 21

    # --- Compounder Margin-of-Safety (price-proxy adaptation of a
    # conservative value-investing community's valuation framework, see
    # docs/snowball_strategy.txt) -- holds a candidate "quality compounder"
    # only while it's in a confirmed uptrend with contained volatility (a
    # price-only proxy for the doc's moat/high-ROE screen -- this project has
    # no real ROE/dividend/earnings data anywhere, see rs/strategy.py's own
    # docstring for the full disclosed-simplification list) AND its own
    # trailing annualized return clears a required hurdle; exits once that
    # trailing return decays below the benchmark's own trailing return (the
    # doc's own sell-trigger rule, translated directly). Candidate universe
    # is illustrative -- a hand-picked, unverified blue-chip basket, not a
    # curated reproduction of the source document's own selection criteria. ---
    cms_candidate_universe: List[str] = field(
        default_factory=lambda: ["KO", "PG", "JNJ", "MSFT", "COST", "WMT", "MCD", "PEP"]
    )
    cms_benchmark_symbol: str = "SPY"
    cms_lookback_days: int = 1260           # ~5 years, mirrors the doc's own 5-year framing
    cms_trend_ma_period: int = 200
    cms_vol_lookback: int = 60
    cms_max_volatility: float = 0.30
    cms_required_return: float = 0.12       # doc's normal-environment buy hurdle

    # --- Bollinger Bands Trading Strategy (John Bollinger Methods I & III) ---
    bb_period: int = 20
    bb_num_std: float = 2.0
    bb_mode: str = "breakout"                # "breakout" (Method I Squeeze) or "mean_reversion" (%B + RSI)
    bb_squeeze_lookback: int = 126           # ~6 months for rolling BandWidth quantile
    bb_squeeze_quantile: float = 0.20        # Squeeze defined as bottom 20% BandWidth
    bb_require_trend_filter: bool = True     # 200-day SMA macro trend gate
    bb_trend_ma_period: int = 200
    bb_rsi_filter: bool = True               # RSI confirmation for mean reversion
    bb_rsi_period: int = 14
    bb_rsi_oversold: float = 35.0
    bb_exit_mode: str = "mid_band"           # "mid_band" (20 SMA) or "opposite_band"
    bb_stop_loss_pct: Optional[float] = 0.06
    bb_trailing_stop_pct: Optional[float] = 0.04
    bb_trailing_activate_pct: Optional[float] = 0.06
    bb_max_holding_days: Optional[int] = 63
    bb_position_size_pct: float = 1.0
    bb_min_weight_change: float = 0.02

    # --- Hybrid Asset Allocation (HAA, Keller & Keuning 2023, SSRN #4346906) ---
    haa_canary_symbol: str = "TIP"
    haa_top_k: int = 4
    haa_offensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_HAA_OFFENSIVE))
    haa_defensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_HAA_DEFENSIVE))
    haa_rebalance_freq_days: int = 21

    # --- Defensive Asset Allocation (DAA, Keller & Keuning 2018, SSRN #3212862) ---
    daa_canary_universe: List[str] = field(default_factory=lambda: list(DEFAULT_DAA_CANARY))
    daa_top_k: int = 6
    daa_risky_universe: List[str] = field(default_factory=lambda: list(DEFAULT_DAA_RISKY))
    daa_defensive_universe: List[str] = field(default_factory=lambda: list(DEFAULT_DAA_DEFENSIVE))
    daa_rebalance_freq_days: int = 21

    # --- Residual Momentum Strategy (Blitz, Huij & Martens 2011; Barroso & Santa-Clara 2015) ---
    resmom_lookback_days: int = 252          # ~12 months regression & standardization window
    resmom_skip_days: int = 21               # Skip most recent 1 month to prevent short-term reversal
    resmom_benchmark_symbol: str = "SPY"
    resmom_top_k: int = 3
    resmom_target_vol: float = 0.12          # 12% annualized target volatility
    resmom_rebalance_freq_days: int = 21
    resmom_require_trend_filter: bool = True
    resmom_trend_ma_period: int = 200

    # --- Macro Regime Factor Compound Strategy ---
    regime_compound_rebalance_freq_days: int = 21
    regime_compound_lookback_days: int = 63
    regime_compound_breadth_bull_thresh: float = 0.60
    regime_compound_breadth_bear_thresh: float = 0.35
    regime_compound_breadth_mom_thresh: float = 0.65
    regime_compound_vol_zscore_thresh: float = 1.0
    regime_compound_hurst_trend_thresh: float = 0.52
    regime_compound_hurst_meanrev_thresh: float = 0.48
    regime_compound_mode: str = "discrete_winner"  # "discrete_winner", "smooth_blend", or "risk_budgeted"
    regime_compound_enable_vol_targeting: bool = True
    regime_compound_target_vol: float = 0.12

    # --- Adaptive Fast Expansion Strategy ---
    afe_fast_roc_days: int = 15
    afe_slow_roc_days: int = 126
    afe_target_vol: float = 0.12
    afe_top_k: int = 3
    afe_breadth_thrust_thresh: float = 0.65
    afe_rebalance_freq_days: int = 10

    # --- Multi-Strategy Alpha Book Strategy ---
    ms_pod_preset: str = "core_satellite"   # 'core_satellite', 'alpha_leaders', 'all_regime'
    ms_execution_mode: str = "pod_native_sparse"  # 'pod_native_sparse', 'periodic_sync'
    ms_rebalance_freq_days: int = 21
    ms_lookback_days: int = 63
    ms_pod_max_drawdown_limit: float = 0.06
    ms_drawdown_recovery_days: int = 10
    ms_max_pod_budget: float = 0.35
    ms_min_pod_budget: float = 0.15
    ms_budget_smoothing_alpha: float = 0.50
    ms_canary_breadth_thresh: float = 0.50
    ms_min_weight_change: float = 0.02

    # --- Chan Risk-Managed Blend Strategy (walkforward-validated ensemble) ---
    # Derived from walkforward anomaly analysis: blends the top 3 adjusted-
    # Sharpe Chan strategies with position limits, drawdown circuit breakers,
    # and a minimum-weight-change threshold to curb excessive turnover.
    crb_composite_weight: float = 0.15       # allocation to chan_composite
    crb_three_type_weight: float = 0.35      # allocation to chan_three_type
    crb_vaa_weight: float = 0.50             # allocation to chan_vaa_compound
    crb_max_single_position: float = 0.20    # hard cap per stock (prevents 100% concentration)
    crb_min_weight_change: float = 0.04      # skip rebalance trades below 4% change
    crb_dd_reduce_thresh: float = 0.10       # drawdown level to halve position sizes
    crb_dd_defensive_thresh: float = 0.15    # drawdown level to switch to VAA-only
    crb_dd_stop_thresh: float = 0.20         # drawdown level to exit to 100% cash
    crb_tier1_cooldown_bars: int = 15        # bars in Tier 1/2 drawdown before auto-healing HWM to current NAV
    crb_dynamic_cash_deployment: bool = True # dynamically deploy idle cash to active risky assets in bull breadth
    crb_breadth_lookback: int = 50           # lookback window for breadth SMA
    crb_breadth_bull_thresh: float = 0.30    # breadth threshold for deploying idle cash (lowered to 0.30)
    crb_thrust_lookback: int = 10            # lookback window for short-term breadth thrust (Zweig 10-day thrust)
    crb_thrust_thresh: float = 0.60          # breadth thrust threshold (fraction of assets with 10d ROC > 0)
    crb_target_bull_exposure: float = 0.80   # target equity exposure in bull breadth regime
    crb_bull_max_single_position: float = 0.30 # dynamically expanded single-stock cap in bull breadth (defaults to 0.30)

    # --- Chan Four-State Risk-Managed Blend Strategy (chan_four_state_blend) ---
    # Enhanced institutional ensemble blending ChanFourStateExecutionStrategy (15%),
    # ChanThreeTypeStrategy (35%), and ChanVaaCompoundStrategy (50%) with institutional risk controls.
    cfsb_four_state_weight: float = 0.15
    cfsb_three_type_weight: float = 0.35
    cfsb_vaa_weight: float = 0.50
    cfsb_max_single_position: float = 0.20
    cfsb_min_weight_change: float = 0.04
    cfsb_dd_reduce_thresh: float = 0.10
    cfsb_dd_defensive_thresh: float = 0.15
    cfsb_dd_stop_thresh: float = 0.20
    cfsb_tier1_cooldown_bars: int = 15       # bars in Tier 1/2 drawdown before auto-healing HWM to current NAV
    cfsb_dynamic_cash_deployment: bool = True
    cfsb_breadth_lookback: int = 50
    cfsb_breadth_bull_thresh: float = 0.30
    cfsb_thrust_lookback: int = 10
    cfsb_thrust_thresh: float = 0.60
    cfsb_target_bull_exposure: float = 0.80
    cfsb_bull_max_single_position: float = 0.30

    # --- Price Action Breakout & Retest Strategy (docs/chan_similar_trading.md Strategy 1) ---
    pabr_box_window: int = 20                # consolidation box length (~4 weeks)
    pabr_max_box_range_pct: float = 0.15     # max box range (high - low) / low
    pabr_vol_contraction_ratio: float = 0.90 # box volume / baseline volume ratio
    pabr_breakout_vol_mult: float = 1.25     # volume expansion threshold on breakout
    pabr_retest_max_bars: int = 10           # max bars allowed for retest
    pabr_retest_tolerance: float = 0.03      # retest proximity tolerance to box high
    pabr_stop_loss_pct: float = 0.06         # max protective stop loss
    pabr_take_profit_mult: float = 1.5       # profit target multiple of box height
    pabr_max_holding_days: int = 60          # time stop
    pabr_position_size_pct: float = 1.0      # single position allocation size

    # --- Volume Profile POC Migration Strategy (docs/chan_similar_trading.md Strategy 2) ---
    vp_lookback: int = 60                    # rolling volume profile window (~3 months)
    vp_n_bins: int = 30                      # number of price bins for profile
    vp_value_area_pct: float = 0.70          # standard value area coverage
    vp_poc_step_lookback: int = 20           # step lookback for staircase check (~1 month)
    vp_min_step_pct: float = 0.015           # minimum upward step in POC
    vp_dip_tolerance: float = 0.03           # tolerance for dip to POC / VAL
    vp_max_divergence_pct: float = 0.18      # profit taking divergence from POC
    vp_poc_collapse_pct: float = 0.04        # exit trigger if POC drops
    vp_stop_loss_pct: float = 0.06           # stop loss below entry/VAL
    vp_max_holding_days: int = 60            # time stop
    vp_position_size_pct: float = 1.0        # single position allocation size

    # --- Modified Elliott Wave 3 Fibonacci Strategy (docs/chan_similar_trading.md Strategy 3) ---
    w3_pivot_window: int = 5                 # local pivot extrema search window
    w3_min_wave1_pct: float = 0.08           # minimum Wave 1 impulse gain
    w3_fibo_min_retrace: float = 0.45        # Wave 2 golden pocket min retracement
    w3_fibo_max_retrace: float = 0.65        # Wave 2 golden pocket max retracement
    w3_vol_contraction_ratio: float = 0.70   # Wave 2 volume dry-up ratio vs Wave 1
    w3_fibo_extension: float = 1.618         # Wave 3 profit target extension
    w3_stop_buffer_pct: float = 0.02         # buffer below Wave 2 trough
    w3_stop_loss_pct: float = 0.06           # maximum protective stop loss
    w3_max_holding_days: int = 65            # time stop
    w3_position_size_pct: float = 1.0        # single position allocation size

    # Backtester execution defaults
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0005          # 5 bps
    slippage_pct: float = 0.0005            # 5 bps
    min_shares: int = 1
    china_trading: bool = False              # Apply China A-share trading rules (T+1, price limits, stamp duty, 100-share lot)
    us_trading: bool = False                 # Apply US market rules (1-share lot, SEC sell fee, T+0 margin)
    hk_trading: bool = False                 # Apply HK market rules (board lot, 0.1085% dual-sided stamp/levies, T+0)
    chan_causal_signals: bool = True         # Strictly causal point-in-time signal generation for Chan strategies (default ON)

    def __post_init__(self):
        """Validates only the handful of fields used downstream as divisors,
        loop bounds, or array indices (`strategy.py`'s generate_weights
        methods) -- a bad value there currently produces a confusing
        ZeroDivisionError/IndexError deep in strategy execution rather than a
        clear error at config-construction time. Deliberately NOT exhaustive
        over all ~60 fields; enum-like string fields (e.g. `ensemble_mode`,
        `rsi_method`) are left unvalidated since their strategies already
        handle an unrecognized value with an explicit fallback/error."""
        if sum([bool(self.china_trading), bool(self.us_trading), bool(self.hk_trading)]) > 1:
            raise ValueError("Only one of china_trading, us_trading, hk_trading may be enabled.")
        if self.rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.rebalance_freq_days must be > 0, got {self.rebalance_freq_days}")
        if self.top_k <= 0:
            raise ValueError(f"StrategyConfig.top_k must be > 0, got {self.top_k}")
        if self.commission_pct < 0:
            raise ValueError(f"StrategyConfig.commission_pct must be >= 0, got {self.commission_pct}")
        if self.slippage_pct < 0:
            raise ValueError(f"StrategyConfig.slippage_pct must be >= 0, got {self.slippage_pct}")
        if self.initial_capital <= 0:
            raise ValueError(f"StrategyConfig.initial_capital must be > 0, got {self.initial_capital}")
        if not isinstance(self.min_shares, int) or self.min_shares < 1:
            raise ValueError(f"StrategyConfig.min_shares must be an integer >= 1, got {self.min_shares!r}")
        if not isinstance(self.china_trading, bool):
            raise ValueError(f"StrategyConfig.china_trading must be a boolean, got {self.china_trading!r}")
        if not isinstance(self.us_trading, bool):
            raise ValueError(f"StrategyConfig.us_trading must be a boolean, got {self.us_trading!r}")
        if not isinstance(self.hk_trading, bool):
            raise ValueError(f"StrategyConfig.hk_trading must be a boolean, got {self.hk_trading!r}")
        if not isinstance(self.chan_causal_signals, bool):
            raise ValueError(f"StrategyConfig.chan_causal_signals must be a boolean, got {self.chan_causal_signals!r}")
        if not self.cash_proxy or not isinstance(self.cash_proxy, str):
            raise ValueError(f"StrategyConfig.cash_proxy must be a non-empty string, got {self.cash_proxy!r}")
        if not isinstance(self.risky_universe, list):
            raise ValueError(f"StrategyConfig.risky_universe must be a list, got {type(self.risky_universe).__name__}")
        if self.chan_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_min_gap_bars must be > 0, got {self.chan_min_gap_bars}")
        if self.chan_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_min_strokes must be >= 3, got {self.chan_min_strokes}")
        if self.chan3_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan3_min_gap_bars must be > 0, got {self.chan3_min_gap_bars}")
        if self.chan3_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan3_min_strokes must be >= 3, got {self.chan3_min_strokes}")
        if self.chanm_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chanm_min_gap_bars must be > 0, got {self.chanm_min_gap_bars}")
        if self.chanm_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chanm_min_strokes must be >= 3, got {self.chanm_min_strokes}")
        if self.chan_mtf_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_mtf_min_gap_bars must be > 0, got {self.chan_mtf_min_gap_bars}")
        if self.chan_mtf_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_mtf_min_strokes must be >= 3, got {self.chan_mtf_min_strokes}")
        if self.chan_b3_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_b3_min_gap_bars must be > 0, got {self.chan_b3_min_gap_bars}")
        if self.chan_b3_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_b3_min_strokes must be >= 3, got {self.chan_b3_min_strokes}")
        if self.chan_mrd_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_mrd_min_gap_bars must be > 0, got {self.chan_mrd_min_gap_bars}")
        if self.chan_mrd_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_mrd_min_strokes must be >= 3, got {self.chan_mrd_min_strokes}")
        if self.chan_comp_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_comp_min_gap_bars must be > 0, got {self.chan_comp_min_gap_bars}")
        if self.chan_comp_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_comp_min_strokes must be >= 3, got {self.chan_comp_min_strokes}")
        if self.pivot_osc_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.pivot_osc_min_gap_bars must be > 0, got {self.pivot_osc_min_gap_bars}")
        if self.pivot_osc_min_strokes < 3:
            raise ValueError(f"StrategyConfig.pivot_osc_min_strokes must be >= 3, got {self.pivot_osc_min_strokes}")
        if self.chan_mrd_confirm_window_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_mrd_confirm_window_bars must be > 0, got {self.chan_mrd_confirm_window_bars}")
        if self.chan_mrd_trend_ma_period <= 0:
            raise ValueError(f"StrategyConfig.chan_mrd_trend_ma_period must be > 0, got {self.chan_mrd_trend_ma_period}")
        if self.fibo_top_k <= 0:
            raise ValueError(f"StrategyConfig.fibo_top_k must be > 0, got {self.fibo_top_k}")
        if not (0 <= self.fibo_min_tier <= 8):
            raise ValueError(f"StrategyConfig.fibo_min_tier must be in [0, 8], got {self.fibo_min_tier}")
        if self.fibo_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.fibo_rebalance_freq_days must be > 0, got {self.fibo_rebalance_freq_days}")
        if self.chanm_adv_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chanm_adv_min_gap_bars must be > 0, got {self.chanm_adv_min_gap_bars}")
        if self.chanm_adv_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chanm_adv_min_strokes must be >= 3, got {self.chanm_adv_min_strokes}")
        if self.chan_fse_min_gap_bars <= 0:
            raise ValueError(f"StrategyConfig.chan_fse_min_gap_bars must be > 0, got {self.chan_fse_min_gap_bars}")
        if self.chan_fse_min_strokes < 3:
            raise ValueError(f"StrategyConfig.chan_fse_min_strokes must be >= 3, got {self.chan_fse_min_strokes}")
        if self.chan_fse_min_hold_bars < 0:
            raise ValueError(f"StrategyConfig.chan_fse_min_hold_bars must be >= 0, got {self.chan_fse_min_hold_bars}")
        if self.chan_fse_zg_tolerance_pct < 0.0:
            raise ValueError(f"StrategyConfig.chan_fse_zg_tolerance_pct must be >= 0, got {self.chan_fse_zg_tolerance_pct}")
        if self.chan_fse_cons_timeout_bars < 0:
            raise ValueError(f"StrategyConfig.chan_fse_cons_timeout_bars must be >= 0, got {self.chan_fse_cons_timeout_bars}")
        if self.chan_fse_stop_evaluation_mode not in ("close", "low"):
            raise ValueError(f"StrategyConfig.chan_fse_stop_evaluation_mode must be 'close' or 'low', got {self.chan_fse_stop_evaluation_mode}")
        if self.chan_fse_b1_buffer_pct < 0.0:
            raise ValueError(f"StrategyConfig.chan_fse_b1_buffer_pct must be >= 0, got {self.chan_fse_b1_buffer_pct}")
        if self.chan_fse_cooldown_bars < 0:
            raise ValueError(f"StrategyConfig.chan_fse_cooldown_bars must be >= 0, got {self.chan_fse_cooldown_bars}")
        if not (0.0 <= self.chan_fse_breadth_bull_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.chan_fse_breadth_bull_thresh must be between 0 and 1, got {self.chan_fse_breadth_bull_thresh}")
        if self.chan_vaa_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.chan_vaa_rebalance_freq_days must be > 0, got {self.chan_vaa_rebalance_freq_days}")
        if self.bb_period <= 0:
            raise ValueError(f"StrategyConfig.bb_period must be > 0, got {self.bb_period}")
        if self.bb_squeeze_lookback <= 0:
            raise ValueError(f"StrategyConfig.bb_squeeze_lookback must be > 0, got {self.bb_squeeze_lookback}")
        if not (0.0 <= self.bb_squeeze_quantile <= 1.0):
            raise ValueError(f"StrategyConfig.bb_squeeze_quantile must be between 0 and 1, got {self.bb_squeeze_quantile}")
        if self.bb_trend_ma_period <= 0:
            raise ValueError(f"StrategyConfig.bb_trend_ma_period must be > 0, got {self.bb_trend_ma_period}")
        if self.bb_rsi_period <= 0:
            raise ValueError(f"StrategyConfig.bb_rsi_period must be > 0, got {self.bb_rsi_period}")
        if self.haa_top_k <= 0:
            raise ValueError(f"StrategyConfig.haa_top_k must be > 0, got {self.haa_top_k}")
        if self.haa_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.haa_rebalance_freq_days must be > 0, got {self.haa_rebalance_freq_days}")
        if self.daa_top_k <= 0:
            raise ValueError(f"StrategyConfig.daa_top_k must be > 0, got {self.daa_top_k}")
        if self.daa_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.daa_rebalance_freq_days must be > 0, got {self.daa_rebalance_freq_days}")
        if self.resmom_lookback_days <= 0:
            raise ValueError(f"StrategyConfig.resmom_lookback_days must be > 0, got {self.resmom_lookback_days}")
        if self.resmom_skip_days < 0:
            raise ValueError(f"StrategyConfig.resmom_skip_days must be >= 0, got {self.resmom_skip_days}")
        if self.resmom_lookback_days <= self.resmom_skip_days:
            raise ValueError(f"StrategyConfig.resmom_lookback_days ({self.resmom_lookback_days}) must be > resmom_skip_days ({self.resmom_skip_days})")
        if self.resmom_top_k <= 0:
            raise ValueError(f"StrategyConfig.resmom_top_k must be > 0, got {self.resmom_top_k}")
        if self.resmom_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.resmom_rebalance_freq_days must be > 0, got {self.resmom_rebalance_freq_days}")
        if self.regime_compound_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.regime_compound_rebalance_freq_days must be > 0, got {self.regime_compound_rebalance_freq_days}")
        if self.regime_compound_lookback_days <= 0:
            raise ValueError(f"StrategyConfig.regime_compound_lookback_days must be > 0, got {self.regime_compound_lookback_days}")
        if not (0.0 <= self.regime_compound_breadth_bear_thresh <= self.regime_compound_breadth_bull_thresh <= self.regime_compound_breadth_mom_thresh <= 1.0):
            raise ValueError(
                f"StrategyConfig: breadth thresholds must satisfy 0 <= bear ({self.regime_compound_breadth_bear_thresh}) "
                f"<= bull ({self.regime_compound_breadth_bull_thresh}) <= mom ({self.regime_compound_breadth_mom_thresh}) <= 1"
            )
        if self.regime_compound_mode not in ("discrete_winner", "smooth_blend", "risk_budgeted"):
            raise ValueError(f"StrategyConfig.regime_compound_mode must be 'discrete_winner', 'smooth_blend', or 'risk_budgeted', got {self.regime_compound_mode}")
        if self.afe_fast_roc_days <= 0:
            raise ValueError(f"StrategyConfig.afe_fast_roc_days must be > 0, got {self.afe_fast_roc_days}")
        if self.afe_slow_roc_days <= 0:
            raise ValueError(f"StrategyConfig.afe_slow_roc_days must be > 0, got {self.afe_slow_roc_days}")
        if self.afe_fast_roc_days >= self.afe_slow_roc_days:
            raise ValueError(f"StrategyConfig: afe_fast_roc_days ({self.afe_fast_roc_days}) must be < afe_slow_roc_days ({self.afe_slow_roc_days})")
        if self.afe_top_k <= 0:
            raise ValueError(f"StrategyConfig.afe_top_k must be > 0, got {self.afe_top_k}")
        if self.afe_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.afe_rebalance_freq_days must be > 0, got {self.afe_rebalance_freq_days}")
        if not (0.0 < self.afe_breadth_thrust_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.afe_breadth_thrust_thresh must be between 0 and 1, got {self.afe_breadth_thrust_thresh}")
        if self.afe_target_vol <= 0:
            raise ValueError(f"StrategyConfig.afe_target_vol must be > 0, got {self.afe_target_vol}")
        if self.regime_compound_target_vol <= 0:
            raise ValueError(f"StrategyConfig.regime_compound_target_vol must be > 0, got {self.regime_compound_target_vol}")
        if self.ms_rebalance_freq_days <= 0:
            raise ValueError(f"StrategyConfig.ms_rebalance_freq_days must be > 0, got {self.ms_rebalance_freq_days}")
        if self.ms_lookback_days <= 0:
            raise ValueError(f"StrategyConfig.ms_lookback_days must be > 0, got {self.ms_lookback_days}")
        valid_presets = {"core_satellite", "alpha_leaders", "all_regime"}
        if self.ms_pod_preset not in valid_presets:
            raise ValueError(f"StrategyConfig.ms_pod_preset must be one of {sorted(valid_presets)}, got {self.ms_pod_preset!r}")
        valid_exec_modes = {"pod_native_sparse", "periodic_sync"}
        if self.ms_execution_mode not in valid_exec_modes:
            raise ValueError(f"StrategyConfig.ms_execution_mode must be one of {sorted(valid_exec_modes)}, got {self.ms_execution_mode!r}")
        if self.ms_drawdown_recovery_days <= 0:
            raise ValueError(f"StrategyConfig.ms_drawdown_recovery_days must be > 0, got {self.ms_drawdown_recovery_days}")
        if not (0.0 < self.ms_pod_max_drawdown_limit < 1.0):
            raise ValueError(f"StrategyConfig.ms_pod_max_drawdown_limit must be between 0 and 1, got {self.ms_pod_max_drawdown_limit}")
        if not (0.0 < self.ms_min_pod_budget <= self.ms_max_pod_budget <= 1.0):
            raise ValueError(f"StrategyConfig: ms_min_pod_budget ({self.ms_min_pod_budget}) must be <= ms_max_pod_budget ({self.ms_max_pod_budget}) and > 0")
        if not (0.0 < self.ms_budget_smoothing_alpha <= 1.0):
            raise ValueError(f"StrategyConfig.ms_budget_smoothing_alpha must be between 0 and 1, got {self.ms_budget_smoothing_alpha}")
        if not (0.0 < self.ms_canary_breadth_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.ms_canary_breadth_thresh must be between 0 and 1, got {self.ms_canary_breadth_thresh}")
        if not (0.0 < self.crb_min_weight_change <= 1.0):
            raise ValueError(f"StrategyConfig.crb_min_weight_change must be between 0 and 1, got {self.crb_min_weight_change}")
        if not (0.0 < self.crb_target_bull_exposure <= 1.0):
            raise ValueError(f"StrategyConfig.crb_target_bull_exposure must be between 0 and 1, got {self.crb_target_bull_exposure}")
        if not (0.0 < self.crb_breadth_bull_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.crb_breadth_bull_thresh must be between 0 and 1, got {self.crb_breadth_bull_thresh}")
        if self.crb_breadth_lookback <= 0:
            raise ValueError(f"StrategyConfig.crb_breadth_lookback must be > 0, got {self.crb_breadth_lookback}")
        if self.crb_thrust_lookback <= 0:
            raise ValueError(f"StrategyConfig.crb_thrust_lookback must be > 0, got {self.crb_thrust_lookback}")
        if not (0.0 < self.crb_thrust_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.crb_thrust_thresh must be between 0 and 1, got {self.crb_thrust_thresh}")
        if not (0.0 < self.crb_bull_max_single_position <= 1.0):
            raise ValueError(f"StrategyConfig.crb_bull_max_single_position must be between 0 and 1, got {self.crb_bull_max_single_position}")
        if not (0.0 < self.cfsb_min_weight_change <= 1.0):
            raise ValueError(f"StrategyConfig.cfsb_min_weight_change must be between 0 and 1, got {self.cfsb_min_weight_change}")
        if not (0.0 < self.cfsb_target_bull_exposure <= 1.0):
            raise ValueError(f"StrategyConfig.cfsb_target_bull_exposure must be between 0 and 1, got {self.cfsb_target_bull_exposure}")
        if not (0.0 < self.cfsb_breadth_bull_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.cfsb_breadth_bull_thresh must be between 0 and 1, got {self.cfsb_breadth_bull_thresh}")
        if self.cfsb_breadth_lookback <= 0:
            raise ValueError(f"StrategyConfig.cfsb_breadth_lookback must be > 0, got {self.cfsb_breadth_lookback}")
        if self.cfsb_thrust_lookback <= 0:
            raise ValueError(f"StrategyConfig.cfsb_thrust_lookback must be > 0, got {self.cfsb_thrust_lookback}")
        if not (0.0 < self.cfsb_thrust_thresh <= 1.0):
            raise ValueError(f"StrategyConfig.cfsb_thrust_thresh must be between 0 and 1, got {self.cfsb_thrust_thresh}")
        if not (0.0 < self.cfsb_bull_max_single_position <= 1.0):
            raise ValueError(f"StrategyConfig.cfsb_bull_max_single_position must be between 0 and 1, got {self.cfsb_bull_max_single_position}")
        if self.crb_tier1_cooldown_bars <= 0:
            raise ValueError(f"StrategyConfig.crb_tier1_cooldown_bars must be > 0, got {self.crb_tier1_cooldown_bars}")
        if self.cfsb_tier1_cooldown_bars <= 0:
            raise ValueError(f"StrategyConfig.cfsb_tier1_cooldown_bars must be > 0, got {self.cfsb_tier1_cooldown_bars}")
        if self.chan_fse_adx_threshold <= 0:
            raise ValueError(f"StrategyConfig.chan_fse_adx_threshold must be > 0, got {self.chan_fse_adx_threshold}")
        if self.chan_fse_adx_period <= 0:
            raise ValueError(f"StrategyConfig.chan_fse_adx_period must be > 0, got {self.chan_fse_adx_period}")

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyConfig":
        """Instantiates StrategyConfig from a dictionary, keeping only valid dataclass fields.
        Unknown keys are dropped (for forward-compatibility with newer config
        files) but a warning names them, rather than the previous total silence."""
        valid_fields = {f.name for f in fields(cls)}
        unknown_keys = set(data.keys()) - valid_fields
        if unknown_keys:
            warnings.warn(f"StrategyConfig.from_dict: ignoring unknown key(s) {sorted(unknown_keys)}")
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


def load_strategies_config(json_path: Optional[str] = None) -> dict:
    """Loads the strategy configuration JSON file.
    Defaults to research_strategy/strategies_config.json if json_path is not specified.
    """
    if json_path is None:
        json_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies_config.json")
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Strategy config file not found at '{json_path}'.")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"Strategy config file '{json_path}' must be a JSON object mapping strategy "
            f"keys to entry objects, got {type(data).__name__}."
        )
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(
                f"strategies_config.json entry '{key}' must be a JSON object, got {type(value).__name__}."
            )
        unknown_factors = set(value.get("factors", [])) - set(FACTOR_CATEGORIES)
        if unknown_factors:
            warnings.warn(
                f"strategies_config.json entry '{key}': unrecognized factor tag(s) {sorted(unknown_factors)} "
                f"not in common.factor_taxonomy.FACTOR_CATEGORIES: {sorted(FACTOR_CATEGORIES)}"
            )

    return data


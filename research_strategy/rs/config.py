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
"""

import json
import os
import warnings
from dataclasses import dataclass, field, fields
from typing import List, Optional

DEFAULT_RISKY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"]
DEFAULT_CASH_PROXY = "BIL"

# Keller's Bold Asset Allocation (BAA-G12) Universes
DEFAULT_BAA_CANARY = ["SPY", "EEM", "EFA", "AGG"]
DEFAULT_BAA_OFFENSIVE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "LQD", "DBC"]
DEFAULT_BAA_DEFENSIVE = ["TIP", "IEF", "TLT", "BIL", "AGG", "DBC"]

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


@dataclass
class StrategyConfig:
    strategy_type: str = "dual_momentum"  # Options: "dual_momentum", "baa_keller", "volatility_managed"
    rebalance_freq_days: int = 21          # Monthly trading cadence (~21 trading days)

    # Universes
    risky_universe: List[str] = field(default_factory=lambda: list(DEFAULT_RISKY_UNIVERSE))
    cash_proxy: str = DEFAULT_CASH_PROXY

    # BAA-specific universes
    baa_canary: List[str] = field(default_factory=lambda: list(DEFAULT_BAA_CANARY))
    baa_offensive: List[str] = field(default_factory=lambda: list(DEFAULT_BAA_OFFENSIVE))
    baa_defensive: List[str] = field(default_factory=lambda: list(DEFAULT_BAA_DEFENSIVE))

    # Dual Momentum & Risk Parity parameters
    trend_sma_period: int = 200            # 200-day SMA trend gate
    mom_short_lookback: int = 63           # ~3 months (63 trading days)
    mom_long_lookback: int = 126           # ~6 months (126 trading days)
    vol_lookback: int = 60                 # ~3 months rolling window for realized volatility
    top_k: int = 3                         # Select top K assets by momentum score

    # Volatility-Managed Portfolios parameters
    vol_managed_target_vol: float = 0.15   # Target annualized volatility (15%)
    vol_managed_var_lookback: int = 20     # 20-day realized variance estimate
    vol_managed_max_leverage: float = 1.0  # Max exposure (1.0 = no leverage, unlevered long-only)

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

    # Backtester execution defaults
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0005          # 5 bps
    slippage_pct: float = 0.0005            # 5 bps

    def __post_init__(self):
        """Validates only the handful of fields used downstream as divisors,
        loop bounds, or array indices (`strategy.py`'s generate_weights
        methods) -- a bad value there currently produces a confusing
        ZeroDivisionError/IndexError deep in strategy execution rather than a
        clear error at config-construction time. Deliberately NOT exhaustive
        over all ~60 fields; enum-like string fields (e.g. `ensemble_mode`,
        `rsi_method`) are left unvalidated since their strategies already
        handle an unrecognized value with an explicit fallback/error."""
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
        if not self.cash_proxy or not isinstance(self.cash_proxy, str):
            raise ValueError(f"StrategyConfig.cash_proxy must be a non-empty string, got {self.cash_proxy!r}")
        if not isinstance(self.risky_universe, list):
            raise ValueError(f"StrategyConfig.risky_universe must be a list, got {type(self.risky_universe).__name__}")

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

    return data


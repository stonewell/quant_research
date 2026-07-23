"""Configuration for Researched Quantitative Trading Strategies.

Grounding:
1. Dual Momentum GTAA + Risk Parity: Antonacci (2014, JPM), Faber (2007, JWM).
2. Bold Asset Allocation (BAA-G12): Wouter J. Keller (2022 SSRN).
3. Volatility-Managed Portfolios: Moreira & Muir (2017, Journal of Finance).
"""

from dataclasses import dataclass, field
from typing import List

DEFAULT_RISKY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"]
DEFAULT_CASH_PROXY = "BIL"

# Keller's Bold Asset Allocation (BAA-G12) Universes
DEFAULT_BAA_CANARY = ["SPY", "EEM", "EFA", "AGG"]
DEFAULT_BAA_OFFENSIVE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "LQD", "DBC"]
DEFAULT_BAA_DEFENSIVE = ["TIP", "IEF", "TLT", "BIL", "AGG", "DBC"]


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

    # Backtester execution defaults
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0005          # 5 bps
    slippage_pct: float = 0.0005            # 5 bps

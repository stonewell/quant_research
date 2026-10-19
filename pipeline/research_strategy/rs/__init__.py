"""Research Strategy Package - Researched Quantitative Trading Strategies."""

from .config import StrategyConfig, load_strategies_config

def __getattr__(name: str):
    if name == "ChanVolumeProfileHybridStrategy":
        from .chan_vp_hybrid_strategy import ChanVolumeProfileHybridStrategy
        return ChanVolumeProfileHybridStrategy
    if name == "WyckoffChanSpringStrategy":
        from .wyckoff_chan_strategy import WyckoffChanSpringStrategy
        return WyckoffChanSpringStrategy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "StrategyConfig",
    "load_strategies_config",
    "ChanVolumeProfileHybridStrategy",
    "WyckoffChanSpringStrategy",
]

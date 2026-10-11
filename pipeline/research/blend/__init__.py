"""Blend strategies package.

Provides modular institutional multi-strategy blend portfolios built on
`BaseBlendAllocationStrategy`.
"""

from .blend_base import (
    CHINA_EQUITY_SECTORS,
    BaseBlendAllocationStrategy,
    _aligned_master_index_helper,
    _compute_garman_klass_vol,
    _get_risky_symbols_helper,
    _get_symbol_sector,
)
from .chan_risk_managed_blend import ChanRiskManagedBlendStrategy
from .chan_four_state_blend import ChanFourStateBlendStrategy
from .chan_crisis_shield_blend import ChanCrisisShieldBlendStrategy
from .chan_dual_hybrid_blend import ChanDualHybridBlendStrategy
from .generic_blend import GenericBlendStrategy

__all__ = [
    "BaseBlendAllocationStrategy",
    "ChanRiskManagedBlendStrategy",
    "ChanFourStateBlendStrategy",
    "ChanCrisisShieldBlendStrategy",
    "ChanDualHybridBlendStrategy",
    "GenericBlendStrategy",
    "CHINA_EQUITY_SECTORS",
    "_get_symbol_sector",
    "_compute_garman_klass_vol",
    "_get_risky_symbols_helper",
    "_aligned_master_index_helper",
]

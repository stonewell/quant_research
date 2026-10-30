"""Chan Crisis Shield Blend Strategy.

Walkforward-validated institutional multi-strategy portfolio based on
Chan Risk-Managed Blend, replacing Chan Composite with WorldQuant Alpha#12
(Volume Shock Reversal):
- 35% ChanVaaCompoundStrategy (Keller VAA regime crash buffer & defensive anchor)
- 45% ChanThreeTypeStrategy (segment-level pivot structural alpha)
- 20% WorldQuantAlphaStrategy (Alpha#12: Volume Shock Reversal sign(delta(Vol, 1)) * (-delta(Close, 1)))
"""

import copy
from typing import Dict, Optional
import pandas as pd

from common.allocation_templates import AllocationTemplate
from pipeline.research_strategy.rs.config import StrategyConfig
from .blend_base import BaseBlendAllocationStrategy


class ChanCrisisShieldBlendStrategy(BaseBlendAllocationStrategy):
    """Chan Crisis Shield Blend Strategy (缠论危机盾牌风控配置策略):

    Walkforward-validated institutional multi-strategy portfolio based on
    `ChanRiskManagedBlendStrategy`, replacing `ChanCompositeStrategy` with
    WorldQuant Alpha#12 (Volume Shock Reversal).
    """

    version: str = "1.0.2"

    def __init__(self, config: Optional[StrategyConfig] = None, version: Optional[str] = None):
        cfg = config or StrategyConfig()
        super().__init__(
            name="chan_crisis_shield_blend",
            param_prefix="ccsb",
            alt_param_prefixes=["crb"],
            config=cfg,
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
                "mean_reversion",
            ],
            version=version,
        )

    def get_sub_strategies(
        self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None
    ) -> Dict[str, AllocationTemplate]:
        from pipeline.research_strategy.rs.chan_advanced_strategies import ChanVaaCompoundStrategy
        from pipeline.research_strategy.rs.strategy import ChanThreeTypeStrategy
        from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantAlphaStrategy

        cfg = self.config
        p = params or {}
        # Note: resolves the shield WorldQuant alpha id directly. The previous
        # nested bare-name "wq_alpha_id" fallback was dead code (ccsb_shield_alpha_id
        # always short-circuits) AND a latent footgun -- the bare "wq_alpha_id" would
        # have leaked the global StrategyConfig.wq_alpha_id=101 if ccsb_shield_alpha_id
        # were ever None. See docs/version.md.
        wq_id = int(self.resolve_param(p, "shield_alpha_id", getattr(cfg, "ccsb_shield_alpha_id", 12), int))
        req_trend = bool(self.resolve_param(p, "shield_require_trend", getattr(cfg, "ccsb_shield_require_trend", False), bool))

        cfg_wq = copy.deepcopy(cfg)
        cfg_wq.wq_alpha_id = wq_id
        cfg_wq.wq_require_trend_filter = req_trend
        shield_strat = WorldQuantAlphaStrategy(cfg_wq)

        return {
            "chan_composite": shield_strat,
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
        }

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        shield_w = float(self.resolve_param(p, "shield_weight", self.resolve_param(p, "composite_weight", getattr(cfg, "ccsb_shield_weight", 0.20), float), float))
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "ccsb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "ccsb_vaa_weight", 0.35), float))
        return {
            "chan_composite": shield_w,
            "chan_three_type": three_w,
            "chan_vaa_compound": vaa_w,
        }

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        shield_id = int(self.resolve_param(p, "shield_alpha_id", getattr(cfg, "ccsb_shield_alpha_id", 12), int))
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "ccsb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "ccsb_vaa_weight", 0.35), float))
        shield_w = float(self.resolve_param(p, "shield_weight", self.resolve_param(p, "composite_weight", getattr(cfg, "ccsb_shield_weight", 0.20), float), float))
        max_pos = float(self.resolve_param(p, "max_single_position", getattr(cfg, "ccsb_max_single_position", 0.20), float))
        min_chg = float(self.resolve_param(p, "min_weight_change", getattr(cfg, "ccsb_min_weight_change", 0.05), float))
        vol_m = str(self.resolve_param(p, "vol_model", getattr(cfg, "ccsb_vol_model", "garman_klass"), str))
        stop_m = str(self.resolve_param(p, "stop_mode", getattr(cfg, "ccsb_stop_mode", "atr_trailing"), str))
        min_entry = int(self.resolve_param(p, "min_entry_bars", getattr(cfg, "ccsb_min_entry_bars", 2), int))

        return (
            f"Chan Crisis Shield Blend Strategy (chan_crisis_shield_blend): "
            f"institutional ensemble blending chan_vaa_compound ({vaa_w:.0%}), "
            f"chan_three_type ({three_w:.0%}), and WorldQuant Alpha#{shield_id} Volume Shock Reversal ({shield_w:.0%}) "
            f"with hard position cap ({max_pos:.0%} max per stock), "
            f"drawdown circuit breakers (smooth damping from 10%, defensive VAA at 15%, stop at 20% with fast recovery & 15d auto-heal), "
            f"turnover filter (min trade change {min_chg:.0%}), "
            f"{f'{min_entry}d entry confirmation gate, ' if min_entry > 1 else ''}"
            f"volatility targeting (12% target vol via {vol_m}), "
            f"{'stop mode (' + stop_m + '), ' if stop_m != 'none' else ''}"
            f"and dynamic cash deployment in bull breadth (>=30% or 10d thrust)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252

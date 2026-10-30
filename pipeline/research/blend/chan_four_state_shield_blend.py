"""Chan Four-State Shield Blend Strategy.

Institutional multi-strategy portfolio fusing the Chan Four-State Execution Machine
(中小资金拒绝盘整, strict pivot breakout validation) with WorldQuant Alpha#12
(Volume Shock Reversal from Chan Crisis Shield) alongside structural pivot trend alpha
and Keller VAA defensive canary anchor:
- 35% ChanThreeTypeStrategy (segment-level pivot structural alpha)
- 25% ChanVaaCompoundStrategy (Keller VAA regime crash buffer & defensive anchor)
- 20% ChanFourStateExecutionStrategy (4-state operational machine with gestation buffer)
- 20% WorldQuantAlphaStrategy (Alpha#12: Volume Shock Reversal sign(delta(Vol, 1)) * (-delta(Close, 1)))
"""

import copy
from typing import Any, Dict, Optional
import pandas as pd

from common.allocation_templates import AllocationTemplate
from pipeline.research_strategy.rs.config import StrategyConfig
from .blend_base import BaseBlendAllocationStrategy


class ChanFourStateShieldBlendStrategy(BaseBlendAllocationStrategy):
    """Chan Four-State Shield Blend Strategy (缠论四象危机盾风控配置策略):

    Walkforward-validated institutional multi-strategy portfolio fusing:
    1. ChanFourStateExecutionStrategy (20%): 4-state operational machine rejecting consolidation
    2. WorldQuant Alpha#12 (20%): Volume Shock Reversal capturing crisis rebounds
    3. ChanThreeTypeStrategy (35%): Structural pivot trend following
    4. ChanVaaCompoundStrategy (25%): Keller VAA crash canary defensive anchor
    """

    version: str = "1.0.1"

    def __init__(self, config: Optional[StrategyConfig] = None, version: Optional[str] = None):
        cfg = config or StrategyConfig()
        super().__init__(
            name="chan_four_state_shield_blend",
            param_prefix="cfssb",
            alt_param_prefixes=["cfsb", "ccsb", "crb"],
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
        from pipeline.research_strategy.rs.chan_advanced_strategies import (
            ChanFourStateExecutionStrategy,
            ChanVaaCompoundStrategy,
        )
        from pipeline.research_strategy.rs.strategy import ChanThreeTypeStrategy
        from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantAlphaStrategy

        cfg = self.config
        p = params or {}
        wq_id = int(self.resolve_param(p, "shield_alpha_id", getattr(cfg, "cfssb_shield_alpha_id", 12), int))
        req_trend = bool(self.resolve_param(p, "shield_require_trend", getattr(cfg, "cfssb_shield_require_trend", False), bool))

        cfg_wq = copy.deepcopy(cfg)
        cfg_wq.wq_alpha_id = wq_id
        cfg_wq.wq_require_trend_filter = req_trend
        shield_strat = WorldQuantAlphaStrategy(cfg_wq)

        return {
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
            "chan_four_state": ChanFourStateExecutionStrategy(cfg),
            "wq_alpha12": shield_strat,
        }

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cfssb_three_type_weight", 0.35), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cfssb_vaa_weight", 0.25), float))
        fse_w = float(self.resolve_param(p, "four_state_weight", getattr(cfg, "cfssb_four_state_weight", 0.20), float))
        shield_w = float(self.resolve_param(p, "shield_weight", getattr(cfg, "cfssb_shield_weight", 0.20), float))
        return {
            "chan_three_type": three_w,
            "chan_vaa_compound": vaa_w,
            "chan_four_state": fse_w,
            "wq_alpha12": shield_w,
        }

    def compute_regime_weights(
        self,
        base_weights: Dict[str, float],
        breadth: float,
        thrust: float,
        date: Any,
        params: Optional[dict] = None,
    ) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        dynamic_regime = bool(self.resolve_param(p, "dynamic_regime_weights", getattr(cfg, "cfssb_dynamic_regime_weights", True), bool))
        if not dynamic_regime:
            return base_weights

        # Bull: 40% Three-Type / 25% Four-State / 15% VAA / 20% Alpha#12 (Aggressive trend capture)
        # Bear: 25% Three-Type / 15% Four-State / 35% VAA / 25% Alpha#12 (Crash defense + Volume shock reversal bounce)
        # Chop: 35% Three-Type / 20% Four-State / 25% VAA / 20% Alpha#12 (Balanced rotational baseline)
        if breadth >= 0.60 or thrust >= 0.60:
            return {
                "chan_three_type": 0.40,
                "chan_vaa_compound": 0.15,
                "chan_four_state": 0.25,
                "wq_alpha12": 0.20,
            }
        elif breadth < 0.30 and thrust < 0.40:
            return {
                "chan_three_type": 0.25,
                "chan_vaa_compound": 0.35,
                "chan_four_state": 0.15,
                "wq_alpha12": 0.25,
            }
        else:
            return {
                "chan_three_type": 0.35,
                "chan_vaa_compound": 0.25,
                "chan_four_state": 0.20,
                "wq_alpha12": 0.20,
            }

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cfssb_three_type_weight", 0.35), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cfssb_vaa_weight", 0.25), float))
        fse_w = float(self.resolve_param(p, "four_state_weight", getattr(cfg, "cfssb_four_state_weight", 0.20), float))
        shield_w = float(self.resolve_param(p, "shield_weight", getattr(cfg, "cfssb_shield_weight", 0.20), float))
        shield_id = int(self.resolve_param(p, "shield_alpha_id", getattr(cfg, "cfssb_shield_alpha_id", 12), int))
        max_pos = float(self.resolve_param(p, "max_single_position", getattr(cfg, "cfssb_max_single_position", 0.20), float))
        min_chg = float(self.resolve_param(p, "min_weight_change", getattr(cfg, "cfssb_min_weight_change", 0.05), float))
        vol_m = str(self.resolve_param(p, "vol_model", getattr(cfg, "cfssb_vol_model", "garman_klass"), str))
        stop_m = str(self.resolve_param(p, "stop_mode", getattr(cfg, "cfssb_stop_mode", "atr_trailing"), str))
        min_entry = int(self.resolve_param(p, "min_entry_bars", getattr(cfg, "cfssb_min_entry_bars", 2), int))

        return (
            f"Chan Four-State Shield Blend Strategy (chan_four_state_shield_blend): "
            f"institutional ensemble fusing chan_three_type ({three_w:.0%}), "
            f"chan_vaa_compound ({vaa_w:.0%}), chan_four_state_execution ({fse_w:.0%}), "
            f"and WorldQuant Alpha#{shield_id} Volume Shock Reversal ({shield_w:.0%}) "
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

"""Generic Configurable Blend Allocation Strategy.

Allows arbitrary quantitative strategies from the workspace to be combined into an
institutional blend portfolio with full risk controls, volatility targeting,
and turnover filtering out of the box.
"""

import copy
from typing import Dict, Optional
import pandas as pd

from common.allocation_templates import AllocationTemplate
from pipeline.research_strategy.rs.config import StrategyConfig
from .blend_base import BaseBlendAllocationStrategy


class GenericBlendStrategy(BaseBlendAllocationStrategy):
    """Generic Configurable Blend Allocation Strategy (通用混合配置策略):

    Enables institutional blending of arbitrary sub-strategies specified via
    `blend_sub_strategies = {"strategy_name": weight}` without needing to write
    new strategy classes.
    """

    version: str = "1.0.0"

    def __init__(self, config: Optional[StrategyConfig] = None, version: Optional[str] = None):
        cfg = config or StrategyConfig()
        super().__init__(
            name="generic_blend",
            param_prefix="blend",
            config=cfg,
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
            ],
            version=version,
        )

    def _resolve_sub_strategies_spec(self, params: Optional[dict] = None) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        spec = p.get("blend_sub_strategies", getattr(cfg, "blend_sub_strategies", None))
        if isinstance(spec, dict) and spec:
            return spec
        # Default fallback: 50% Three-Type, 50% VAA Compound
        return {"chan_three_type": 0.50, "chan_vaa_compound": 0.50}

    def get_sub_strategies(
        self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None
    ) -> Dict[str, AllocationTemplate]:
        from pipeline.research_strategy.rs.strategy import STRATEGY_CLASS_MAP

        cfg = self.config
        spec = self._resolve_sub_strategies_spec(params)
        sub_strats = {}

        # Name mapping normalization
        alias_map = {
            "chan_three_type": "ChanThreeTypeStrategy",
            "chan_vaa_compound": "ChanVaaCompoundStrategy",
            "chan_composite": "ChanCompositeStrategy",
            "chan_four_state": "ChanFourStateExecutionStrategy",
            "accelerating_dual_momentum": "AcceleratingDualMomentum",
            "turtle_breakout": "TurtleBreakoutStrategy",
            "vigilant_asset_allocation": "VigilantAssetAllocation",
        }

        for name in spec.keys():
            cls_name = alias_map.get(name, name)
            if cls_name in STRATEGY_CLASS_MAP:
                cls_obj = STRATEGY_CLASS_MAP[cls_name]
                sub_strats[name] = cls_obj(cfg)
            elif name.startswith("wq_alpha") or name.startswith("alpha_") or name.startswith("wq_"):
                import re
                match = re.search(r"\d+", name)
                alpha_id = int(match.group()) if match else 101
                from pipeline.research_strategy.rs.strategy import WorldQuantAlphaStrategy
                cfg_alpha = copy.deepcopy(cfg)
                cfg_alpha.wq_alpha_id = alpha_id
                sub_strats[name] = WorldQuantAlphaStrategy(cfg_alpha)
            elif name.startswith("gtja_alpha") or name.startswith("gtja_"):
                import re
                match = re.search(r"\d+", name)
                alpha_id = int(match.group()) if match else 16
                from pipeline.research_strategy.rs.gtja191_factors import GTJAAlphaStrategy
                cfg_alpha = copy.deepcopy(cfg)
                cfg_alpha.gtja_alpha_id = alpha_id
                sub_strats[name] = GTJAAlphaStrategy(cfg_alpha)
            else:
                # Try finding case-insensitive match in STRATEGY_CLASS_MAP
                found = False
                for k, v in STRATEGY_CLASS_MAP.items():
                    if k.lower() == cls_name.lower():
                        sub_strats[name] = v(cfg)
                        found = True
                        break
                if not found:
                    raise ValueError(f"GenericBlendStrategy: Unrecognized sub-strategy '{name}'")

        return sub_strats

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        spec = self._resolve_sub_strategies_spec(params)
        return {k: float(v) for k, v in spec.items()}

    def explain_weights(self, params: dict = None) -> str:
        weights = self.get_sub_strategy_weights(params)
        weights_desc = ", ".join(f"{k} ({v:.0%})" for k, v in weights.items())
        return f"Generic Blend Strategy (generic_blend): multi-strategy ensemble blending {weights_desc} with institutional risk controls."

    def warmup_bars(self, params: dict = None) -> int:
        return 252

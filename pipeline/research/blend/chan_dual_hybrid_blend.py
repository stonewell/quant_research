"""Chan Dual Hybrid Alpha Blend Strategy.

Anti-fragile institutional multi-strategy portfolio combining structural trend
alpha, defensive crash buffer, and dual WorldQuant formulaic alphas:
- 35% ChanVaaCompoundStrategy (Keller VAA regime crash buffer & defensive anchor)
- 45% ChanThreeTypeStrategy (segment-level pivot structural alpha)
- 10% WorldQuant Alpha#53 (Candle Wick Imbalance CLV shift)
- 10% WorldQuant Alpha#3 (Volume-Price Rank Correlation Delta)
Optional expanded sleeve adds Alpha#41 and Alpha#101.
"""

import copy
from typing import Any, Dict, Optional
import pandas as pd

from common.allocation_templates import AllocationTemplate
from pipeline.research_strategy.rs.config import StrategyConfig
from .blend_base import BaseBlendAllocationStrategy


class ChanDualHybridBlendStrategy(BaseBlendAllocationStrategy):
    """Chan Dual Hybrid Alpha Blend Strategy (缠论双因子混合Alpha配置策略):

    Walkforward-champion institutional multi-strategy portfolio based on
    `BaseBlendAllocationStrategy`, featuring dynamic regime-adaptive weighting,
    Garman-Klass extreme-value volatility targeting, Chandelier ATR trailing stops,
    and adaptive inertia rebalancing.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        cfg = config or StrategyConfig()
        super().__init__(
            name="chan_dual_hybrid_blend",
            param_prefix="cdhb",
            config=cfg,
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
            ],
        )

    def get_sub_strategies(
        self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None
    ) -> Dict[str, AllocationTemplate]:
        from pipeline.research_strategy.rs.chan_advanced_strategies import ChanVaaCompoundStrategy
        from pipeline.research_strategy.rs.strategy import ChanThreeTypeStrategy
        from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantAlphaStrategy

        cfg = self.config
        p = params or {}
        a1_id = int(self.resolve_param(p, "alpha1_id", getattr(cfg, "cdhb_alpha1_id", 53), int))
        a2_id = int(self.resolve_param(p, "alpha2_id", getattr(cfg, "cdhb_alpha2_id", 3), int))
        a1_trend = bool(self.resolve_param(p, "alpha1_require_trend", getattr(cfg, "cdhb_alpha1_require_trend", False), bool))
        a2_trend = bool(self.resolve_param(p, "alpha2_require_trend", getattr(cfg, "cdhb_alpha2_require_trend", False), bool))
        expand_alphas = bool(self.resolve_param(p, "expand_alpha_sleeve", getattr(cfg, "cdhb_expand_alpha_sleeve", True), bool))
        a3_id = int(self.resolve_param(p, "alpha3_id", getattr(cfg, "cdhb_alpha3_id", 41), int))
        a4_id = int(self.resolve_param(p, "alpha4_id", getattr(cfg, "cdhb_alpha4_id", 101), int))

        cfg_a1 = copy.deepcopy(cfg)
        cfg_a1.wq_alpha_id = a1_id
        cfg_a1.wq_require_trend_filter = a1_trend
        strat_a1 = WorldQuantAlphaStrategy(cfg_a1)

        cfg_a2 = copy.deepcopy(cfg)
        cfg_a2.wq_alpha_id = a2_id
        cfg_a2.wq_require_trend_filter = a2_trend
        strat_a2 = WorldQuantAlphaStrategy(cfg_a2)

        sub_map = {
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "wq_alpha1": strat_a1,
            "wq_alpha2": strat_a2,
        }

        if expand_alphas:
            cfg_a3 = copy.deepcopy(cfg)
            cfg_a3.wq_alpha_id = a3_id
            cfg_a3.wq_require_trend_filter = False
            sub_map["wq_alpha3"] = WorldQuantAlphaStrategy(cfg_a3)

            cfg_a4 = copy.deepcopy(cfg)
            cfg_a4.wq_alpha_id = a4_id
            cfg_a4.wq_require_trend_filter = False
            sub_map["wq_alpha4"] = WorldQuantAlphaStrategy(cfg_a4)

        return sub_map

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cdhb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cdhb_vaa_weight", 0.35), float))
        a1_w = float(self.resolve_param(p, "alpha1_weight", getattr(cfg, "cdhb_alpha1_weight", 0.10), float))
        a2_w = float(self.resolve_param(p, "alpha2_weight", getattr(cfg, "cdhb_alpha2_weight", 0.10), float))
        expand_alphas = bool(self.resolve_param(p, "expand_alpha_sleeve", getattr(cfg, "cdhb_expand_alpha_sleeve", True), bool))

        if expand_alphas:
            alpha_share = (a1_w + a2_w) / 4.0
            return {
                "chan_three_type": three_w,
                "chan_vaa_compound": vaa_w,
                "wq_alpha1": alpha_share,
                "wq_alpha2": alpha_share,
                "wq_alpha3": alpha_share,
                "wq_alpha4": alpha_share,
            }
        else:
            return {
                "chan_three_type": three_w,
                "chan_vaa_compound": vaa_w,
                "wq_alpha1": a1_w,
                "wq_alpha2": a2_w,
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
        dynamic_regime = bool(self.resolve_param(p, "dynamic_regime_weights", getattr(cfg, "cdhb_dynamic_regime_weights", True), bool))

        # Check for explicit custom static weights
        has_custom = (
            ("cdhb_three_type_weight" in p or "cdhb_vaa_weight" in p or "cdhb_alpha1_weight" in p or "cdhb_alpha2_weight" in p)
            or (base_weights.get("chan_three_type", 0) == 0.0 and base_weights.get("chan_vaa_compound", 0) == 0.0)
        )
        if has_custom and "cdhb_dynamic_regime_weights" not in p:
            dynamic_regime = False

        if not dynamic_regime:
            return base_weights

        # Bull: 50% Three-Type / 20% VAA / 30% Alphas (aggressive trend participation)
        # Bear: 45% Three-Type / 30% VAA / 25% Alphas (firepower + defensive anchor)
        # Chop: 45% Three-Type / 25% VAA / 30% Alphas (rotational balance)
        if breadth >= 0.60 or thrust >= 0.60:
            cur_three_w, cur_vaa_w, cur_alpha_tot = 0.50, 0.20, 0.30
        elif breadth < 0.30 and thrust < 0.40:
            cur_three_w, cur_vaa_w, cur_alpha_tot = 0.45, 0.30, 0.25
        else:
            cur_three_w, cur_vaa_w, cur_alpha_tot = 0.45, 0.25, 0.30

        expand_alphas = "wq_alpha3" in base_weights and "wq_alpha4" in base_weights
        if expand_alphas:
            alpha_share = cur_alpha_tot / 4.0
            return {
                "chan_three_type": cur_three_w,
                "chan_vaa_compound": cur_vaa_w,
                "wq_alpha1": alpha_share,
                "wq_alpha2": alpha_share,
                "wq_alpha3": alpha_share,
                "wq_alpha4": alpha_share,
            }
        else:
            alpha_share = cur_alpha_tot / 2.0
            return {
                "chan_three_type": cur_three_w,
                "chan_vaa_compound": cur_vaa_w,
                "wq_alpha1": alpha_share,
                "wq_alpha2": alpha_share,
            }

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        a1_id = int(self.resolve_param(p, "alpha1_id", getattr(cfg, "cdhb_alpha1_id", 53), int))
        a2_id = int(self.resolve_param(p, "alpha2_id", getattr(cfg, "cdhb_alpha2_id", 3), int))
        a3_id = int(self.resolve_param(p, "alpha3_id", getattr(cfg, "cdhb_alpha3_id", 41), int))
        a4_id = int(self.resolve_param(p, "alpha4_id", getattr(cfg, "cdhb_alpha4_id", 101), int))
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cdhb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cdhb_vaa_weight", 0.35), float))
        a1_w = float(self.resolve_param(p, "alpha1_weight", getattr(cfg, "cdhb_alpha1_weight", 0.10), float))
        a2_w = float(self.resolve_param(p, "alpha2_weight", getattr(cfg, "cdhb_alpha2_weight", 0.10), float))
        max_pos = float(self.resolve_param(p, "max_single_position", getattr(cfg, "cdhb_max_single_position", 0.20), float))
        min_chg = float(self.resolve_param(p, "min_weight_change", getattr(cfg, "cdhb_min_weight_change", 0.05), float))
        vol_m = str(self.resolve_param(p, "vol_model", getattr(cfg, "cdhb_vol_model", "garman_klass"), str))
        stop_m = str(self.resolve_param(p, "stop_mode", getattr(cfg, "cdhb_stop_mode", "atr_trailing"), str))
        stop_mult = float(self.resolve_param(p, "stop_atr_mult", getattr(cfg, "cdhb_stop_atr_mult", 4.5), float))
        expand_alphas = bool(self.resolve_param(p, "expand_alpha_sleeve", getattr(cfg, "cdhb_expand_alpha_sleeve", True), bool))
        t_vol = float(self.resolve_param(p, "target_vol", getattr(cfg, "cdhb_target_vol", 0.18), float))

        sleeve_desc = f"Alpha#{a1_id} ({a1_w:.0%}), Alpha#{a2_id} ({a2_w:.0%})"
        if expand_alphas:
            sleeve_desc += f", Alpha#{a3_id} (5%), Alpha#{a4_id} (5%)"

        vol_name = "Garman-Klass" if vol_m == "garman_klass" else vol_m.replace('_', ' ').title()
        min_entry = int(self.resolve_param(p, "min_entry_bars", getattr(cfg, "cdhb_min_entry_bars", 2), int))

        return (
            f"Chan Dual Hybrid Alpha Blend Strategy (chan_dual_hybrid_blend): "
            f"walkforward-champion institutional ensemble blending chan_vaa_compound ({vaa_w:.0%}), "
            f"chan_three_type ({three_w:.0%}), and {sleeve_desc} "
            f"with hard position cap ({max_pos:.0%} max per stock), "
            f"drawdown circuit breakers (smooth damping from 10%, defensive VAA at 15%, stop at 20% with fast recovery & 15d auto-heal), "
            f"turnover filter (min trade change {min_chg:.0%}), "
            f"{f'{min_entry}d entry confirmation gate, ' if min_entry > 1 else ''}"
            f"volatility targeting ({t_vol:.0%} target vol via {vol_name}), "
            f"and {stop_m} stop ({stop_mult:.1f}x ATR)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252

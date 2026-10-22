"""Chan Four-State Risk-Managed Blend Strategy.

Institutional multi-strategy portfolio combining Chan Four-State Execution Machine
with structural pivot trend alpha and Keller VAA defensive canary anchor:
- 30% ChanVaaCompoundStrategy (defensive anchor & canary crash buffer)
- 45% ChanThreeTypeStrategy (structural pivot trend alpha)
- 25% ChanFourStateExecutionStrategy (4-state operational machine with gestation buffer)
"""

import copy
from typing import Dict, Optional
import pandas as pd

from common.allocation_templates import AllocationTemplate
from pipeline.research_strategy.rs.config import StrategyConfig
from .blend_base import BaseBlendAllocationStrategy


class ChanFourStateBlendStrategy(BaseBlendAllocationStrategy):
    """Chan Four-State Risk-Managed Blend Strategy (缠论四态风控混合配置策略):

    Institutional multi-strategy portfolio based on `ChanRiskManagedBlendStrategy`,
    replacing `ChanCompositeStrategy` with the 4-state operational execution machine
    (`ChanFourStateExecutionStrategy`) and applying unified institutional risk controls.
    """

    version: str = "1.1.0"

    def __init__(self, config: Optional[StrategyConfig] = None, version: Optional[str] = None):
        cfg = config or StrategyConfig()
        super().__init__(
            name="chan_four_state_blend",
            param_prefix="cfsb",
            config=cfg,
            factor_tags=[
                "regime_trend_strength",
                "absolute_momentum_trend",
                "relative_momentum",
                "volatility_targeting",
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

        cfg = self.config
        p = params or {}
        strat_type = str(self.resolve_param(p, "third_strategy_type", getattr(cfg, "cfsb_third_strategy_type", "four_state"), str)).lower()
        # BUGFIX (2026-10-21): resolve only the strategy-prefixed WorldQuant alpha
        # id. The bare "wq_alpha_id" name previously let resolve_param's final
        # fallback grab the global StrategyConfig.wq_alpha_id=101 (the standalone
        # WorldQuantAlphaStrategy default), silently replacing
        # ChanFourStateExecutionStrategy with WorldQuantAlphaStrategy #101. See
        # docs/version.md for the audit record.
        wq_id = p.get("cfsb_wq_alpha_id", getattr(cfg, "cfsb_wq_alpha_id", None))

        if strat_type in ("worldquant_alpha", "wq_alpha") or wq_id is not None:
            from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantAlphaStrategy
            cfg_wq = copy.deepcopy(cfg)
            if wq_id is not None:
                cfg_wq.wq_alpha_id = int(wq_id)
            third_strat = WorldQuantAlphaStrategy(cfg_wq)
        elif strat_type in ("worldquant_mega_alpha", "mega_alpha"):
            from pipeline.research_strategy.rs.worldquant_alpha_strategy import WorldQuantMegaAlphaStrategy
            third_strat = WorldQuantMegaAlphaStrategy(cfg)
        else:
            third_strat = ChanFourStateExecutionStrategy(cfg)

        return {
            "chan_four_state": third_strat,
            "chan_three_type": ChanThreeTypeStrategy(cfg),
            "chan_vaa_compound": ChanVaaCompoundStrategy(cfg),
        }

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        cfg = self.config
        p = params or {}
        fse_w = float(self.resolve_param(p, "four_state_weight", getattr(cfg, "cfsb_four_state_weight", 0.25), float))
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cfsb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cfsb_vaa_weight", 0.30), float))
        return {
            "chan_four_state": fse_w,
            "chan_three_type": three_w,
            "chan_vaa_compound": vaa_w,
        }

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        fse_w = float(self.resolve_param(p, "four_state_weight", getattr(cfg, "cfsb_four_state_weight", 0.25), float))
        three_w = float(self.resolve_param(p, "three_type_weight", getattr(cfg, "cfsb_three_type_weight", 0.45), float))
        vaa_w = float(self.resolve_param(p, "vaa_weight", getattr(cfg, "cfsb_vaa_weight", 0.30), float))
        max_pos = float(self.resolve_param(p, "max_single_position", getattr(cfg, "cfsb_max_single_position", 0.20), float))
        dd_red = float(self.resolve_param(p, "dd_reduce_thresh", getattr(cfg, "cfsb_dd_reduce_thresh", 0.10), float))
        dd_def = float(self.resolve_param(p, "dd_defensive_thresh", getattr(cfg, "cfsb_dd_defensive_thresh", 0.15), float))
        dd_stop = float(self.resolve_param(p, "dd_stop_thresh", getattr(cfg, "cfsb_dd_stop_thresh", 0.20), float))
        tier1_cd = int(self.resolve_param(p, "tier1_cooldown_bars", getattr(cfg, "cfsb_tier1_cooldown_bars", 15), int))
        min_chg = float(self.resolve_param(p, "min_weight_change", getattr(cfg, "cfsb_min_weight_change", 0.05), float))
        dyn_cash = bool(self.resolve_param(p, "dynamic_cash_deployment", getattr(cfg, "cfsb_dynamic_cash_deployment", True), bool))
        b_thresh = float(self.resolve_param(p, "breadth_bull_thresh", getattr(cfg, "cfsb_breadth_bull_thresh", 0.30), float))
        t_lookback = int(self.resolve_param(p, "thrust_lookback", getattr(cfg, "cfsb_thrust_lookback", 10), int))
        vol_tgt = bool(self.resolve_param(p, "enable_vol_targeting", getattr(cfg, "cfsb_enable_vol_targeting", True), bool))
        t_vol = float(self.resolve_param(p, "target_vol", getattr(cfg, "cfsb_target_vol", 0.12), float))
        smooth_dd = bool(self.resolve_param(p, "smooth_drawdown", getattr(cfg, "cfsb_smooth_drawdown", True), bool))
        vol_m = str(self.resolve_param(p, "vol_model", getattr(cfg, "cfsb_vol_model", "garman_klass"), str))
        stop_m = str(self.resolve_param(p, "stop_mode", getattr(cfg, "cfsb_stop_mode", "atr_trailing"), str))
        min_entry = int(self.resolve_param(p, "min_entry_bars", getattr(cfg, "cfsb_min_entry_bars", 2), int))

        return (
            f"Chan Four-State Risk-Managed Blend Strategy (chan_four_state_blend): "
            f"institutional ensemble blending chan_vaa_compound ({vaa_w:.0%}), "
            f"chan_three_type ({three_w:.0%}), and chan_four_state_execution ({fse_w:.0%}) "
            f"with hard position cap ({max_pos:.0%} max per stock), "
            f"drawdown circuit breakers ({'smooth damping' if smooth_dd else 'halve equity'} from {dd_red:.0%}, defensive VAA at {dd_def:.0%}, stop at {dd_stop:.0%} with fast recovery & {tier1_cd}d auto-heal), "
            f"turnover filter (min trade change {min_chg:.0%}), "
            f"{f'{min_entry}d entry confirmation gate, ' if min_entry > 1 else ''}"
            f"{'volatility targeting (' + f'{t_vol:.0%}' + f' target vol via {vol_m}), ' if vol_tgt else ''}"
            f"{'stop mode (' + stop_m + '), ' if stop_m != 'none' else ''}"
            f"and {'dynamic cash deployment in bull breadth (>=' + f'{b_thresh:.0%}' + f' or {t_lookback}d thrust)' if dyn_cash else 'static cash buffer'}."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 252

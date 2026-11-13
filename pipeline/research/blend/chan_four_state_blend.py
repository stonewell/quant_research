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

    # 1.1.0 -> 1.2.0 (2026-10-22, first fix round): partial-trim tenure lock,
    #   bucket-renorm clamp (Fix 3), regime hysteresis (Fix 4), relative trim
    #   inertia (Fix 7), broker_weight snapshot (Fix 6), advisory labelling (Fix 5).
    # 1.2.0 -> 1.2.1 (2026-10-22, correction round): behavioural corrections found
    #   while verifying the first round against the live incident --
    #   (a) SAFETY: the min-holding tenure lock no longer gates the stop-loss /
    #       emergency-halt liquidation path (a 2-bar anti-whipsaw lock must never
    #       convert a bounded stop into an unbounded tail loss);
    #   (b) Fix 7's `cfsb_inertia_relative_ratio` raised 0.25 -> 0.30 because
    #       0.25 * 0.20 (position cap) == 0.05 == the absolute floor, i.e. a
    #       mathematical no-op;
    #   (c) `blend_weighting_mode` restored to `regime_ic_ir_hybrid` in
    #       strategies_config.json (the mode the 2026-10-22 live run executed
    #       under; the first round's re-dump had silently reverted it to
    #       `rolling_ic_ir`, which made Fix 3/4 inert). See docs/version.md §8.
    # 1.2.2 -> 1.3.0 (2026-11-12, LV18 wiring): Stage-1's Barroso realized-volatility factor
    #   (`check_live_portfolio_health.py::evaluate_realized_volatility`) is now a CONSUMER
    #   inside the live ticket build, as a CEILING on the equity budget:
    #       equity_budget = min(model_equity_sum, vol_scaling_factor)
    #   Deliberately NOT a multiplication: `blend_base.py` already applies its own
    #   Garman-Klass target-vol scaling (`cfsb_target_vol = 0.12`), so multiplying would
    #   de-risk the same volatility observation twice (archived 2026-11-11/205137:
    #   model equity 0.463 x factor 0.3703 = 0.171 instead of 0.370).
    #   Gate: `cfsb_portfolio_vol_overlay_enabled` (default true) +
    #   `cfsb_portfolio_vol_overlay_mode` (`cap`|`off`) in strategies_config.json, wired in
    #   `scripts/run_live_strategy.py` (strategy allowlist = this strategy only), so no other
    #   strategy's ticket changes. A degraded Stage-1 factor is `applied=false` /
    #   `reason=vol_unavailable` and NEVER defaults to a 1.0x multiplier. No `blend_base.py`
    #   vol-targeting logic was touched, so `BASE_VERSION` stays `2.0.0`. See docs/version.md.
    # 1.3.0 -> 1.3.1 (2026-11-12, PLAN C): the Stage-1 ceiling is now ENFORCED, not merely
    #   reported. Measured on the 2026-11-09 replay: `binding=true`, `capped_equity_sum=0.3995`,
    #   but `post_lifecycle_equity_sum=0.5661` because `000157.SZ` sat in `EXIT_CONFIRMING` at
    #   16.66%, so the ticket delivered 0.5166 -- 11.72pp OVER the ceiling with only
    #   `ceiling_exceeded_by` to show for it. Now: when the cap binds and the post-lifecycle
    #   book is >1pp over, ONLY positions already in `PENDING_EXIT` / `EXIT_CONFIRMING` are
    #   flattened (largest contribution first) until the book is inside the ceiling; China T+1,
    #   stale/absent prices and every `HELD_ACTIVE` name are hard-blocked and recorded in
    #   `force_exit_skipped`. Switch `cfsb_portfolio_vol_ceiling_force_exits` (default ON).
    #   Non-breaking: `blend_base.py` vol targeting / `evaluate_realized_volatility` untouched,
    #   so `BASE_VERSION` stays `2.0.0`. See docs/version.md §34.
    version: str = "1.3.1"

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

"""Base Blend Allocation Strategy Engine.

Unified, institutional-grade risk management, market breadth/thrust detection,
volatility targeting, drawdown circuit breakers, and turnover filters for
multi-strategy blend portfolios.

Features provided across all blend strategies:
- Calendar & universe synchronization complying with the workspace sparse weights NaN-vs-0.0 contract
- Universe market breadth (fraction > SMA50) & 10-day Zweig breadth thrust
- Volatility engines: Garman-Klass (1980) OHLC extreme-value vol (8x efficiency) or close-to-close rolling std
- Regime-adaptive target volatility & portfolio Markowitz diversification tracking
- Multi-tier drawdown circuit breakers with auto-healing cooldown & fast-recovery override
- Elastic flash reset of drawdown brake on confirmed thrust/breadth surge
- Dynamic bull cash deployment & dynamic single-stock position cap expansion
- Pre-emptive multi-asset breadth thrust cash deployment across momentum leaders
- Multi-signal conviction confluence sizing & volume surge confirmation
- Universal equity sector concentration throttling with adaptive expansion for leading sectors
- Inverse-volatility risk parity sizing
- Asset-level protective stop (-8%) & Chandelier ATR trailing stop with post-stop cooldown
- Quality & relative strength gate suppressing entries into persistent downtrend assets
- Volatility-scaled adaptive asymmetric inertia rebalancing with entry bypass
"""

from collections import defaultdict
import copy
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    apply_asset_inertia,
    _fill_out_columns,
    _sparse_from_daily,
    _step_asset_inertia,
)
from common.indicators import atr, sma
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.research_strategy.rs.config import StrategyConfig


_LOGGER = logging.getLogger(__name__)


CHINA_EQUITY_SECTORS = {
    "601225.SH": "coal", "601088.SH": "coal",
    "601857.SH": "oil_petrochem", "600028.SH": "oil_petrochem",
    "601872.SH": "shipping", "601919.SH": "shipping",
    "601111.SH": "aviation",
    "601728.SH": "telecom", "600941.SH": "telecom",
    "601288.SH": "banking", "601398.SH": "banking", "600000.SH": "banking", "601166.SH": "banking",
    "601601.SH": "insurance",
    "601899.SH": "metals", "600362.SH": "metals",
    "000157.SZ": "machinery",
    "600584.SH": "semiconductor", "002371.SZ": "semiconductor",
    "000938.SZ": "computer_ict",
    "300394.SZ": "telecom_cpo",
    "600660.SH": "auto_parts",
    "601390.SH": "construction",
}


def _get_symbol_sector(sym: str) -> str:
    """Classifies symbol into broad industrial sector for concentration throttling."""
    if sym in CHINA_EQUITY_SECTORS:
        return CHINA_EQUITY_SECTORS[sym]
    raw = sym.split(".")[0]
    return f"sec_{raw[:2]}" if len(raw) >= 2 else "sec_other"


def _compute_garman_klass_vol(df: pd.DataFrame, window: int = 21, default_vol: float = 0.14) -> pd.Series:
    """Computes Garman-Klass (1980) extreme-value volatility using OHLC bars.
    Provides ~8x statistical efficiency over close-to-close returns.
    Falls back gracefully to close-to-close returns std if OHLC unavailable or constant.
    """
    cols = df.columns
    if not all(k in cols for k in ["High", "Low", "Close", "Open"]):
        ret = df["Close"].pct_change().fillna(0.0)
        return (ret.rolling(window, min_periods=10).std() * np.sqrt(252)).fillna(default_vol)

    h = df["High"].replace(0, np.nan)
    l = df["Low"].replace(0, np.nan)
    c = df["Close"].replace(0, np.nan)
    o = df["Open"].replace(0, np.nan)

    if (h == l).all():
        ret = df["Close"].pct_change().fillna(0.0)
        return (ret.rolling(window, min_periods=10).std() * np.sqrt(252)).fillna(default_vol)

    log_hl = np.log((h / l).clip(lower=1e-6)) ** 2
    log_co = np.log((c / o).clip(lower=1e-6)) ** 2
    gk_var = 0.5 * log_hl - (2.0 * np.log(2.0) - 1.0) * log_co
    gk_var = gk_var.clip(lower=0.0)
    gk_vol = np.sqrt(gk_var.rolling(window, min_periods=10).mean() * 252.0)
    return gk_vol.fillna(default_vol)


def _get_risky_symbols_helper(
    universe: Dict[str, pd.DataFrame],
    params: dict,
    cfg_symbol: Optional[str] = None,
    cfg_risky_universe: Optional[List[str]] = None,
    cash_proxy: str = "BIL",
) -> List[str]:
    p = params or {}
    if "symbol" in p and p["symbol"]:
        sym = p["symbol"]
        return [sym] if sym in universe else []
    if "symbols" in p and p["symbols"]:
        return [s for s in p["symbols"] if s in universe and s != cash_proxy]
    if "risky_universe" in p and p["risky_universe"]:
        return [s for s in p["risky_universe"] if s in universe and s != cash_proxy]

    if cfg_symbol:
        return [cfg_symbol] if cfg_symbol in universe else []

    candidates = [s for s in (cfg_risky_universe or []) if s in universe and s != cash_proxy]
    if not candidates:
        candidates = [s for s in universe if s != cash_proxy]
    return candidates


def _aligned_master_index_helper(universe: Dict[str, pd.DataFrame], risky_symbols: List[str]) -> pd.DatetimeIndex:
    non_empty = [s for s in risky_symbols if s in universe and not universe[s].empty]
    if not non_empty:
        return pd.DatetimeIndex([])
    if len(non_empty) == 1:
        return universe[non_empty[0]].index
    return pd.DatetimeIndex(sorted(set().union(*(universe[s].index for s in non_empty))))


def _apply_portfolio_deleverage(
    desired_row: pd.Series,
    risky_symbols: List[str],
    cash_proxy: Optional[str],
    scale: float,
) -> pd.Series:
    """Scales risky assets by portfolio-level deleveraging scale and sweeps residual to cash proxy."""
    out = desired_row.copy()
    if scale < 1.0:
        s_clamped = max(0.0, float(scale))
        out[risky_symbols] = out[risky_symbols] * s_clamped
        if cash_proxy and cash_proxy in out.index:
            out[cash_proxy] = max(0.0, 1.0 - float(out[risky_symbols].sum()))
    return out


class BaseBlendAllocationStrategy(AllocationTemplate):
    """Institutional Multi-Strategy Base Blend Allocation Engine.

    Encapsulates risk management, breadth detection, drawdown circuit breakers,
    Garman-Klass volatility targeting, ATR chandelier trailing stops, sector throttling,
    and adaptive turnover filters so any blend strategy can inherit them cleanly.
    """

    BASE_VERSION: str = "1.7.0"
    version: str = "1.0.0"

    def __init__(
        self,
        name: str = "base_blend",
        param_prefix: str = "blend",
        alt_param_prefixes: Optional[List[str]] = None,
        config: Optional["StrategyConfig"] = None,
        factor_tags: Optional[List[str]] = None,
        version: Optional[str] = None,
        base_version: Optional[str] = None,
    ):
        if config is None:
            from pipeline.research_strategy.rs.config import StrategyConfig
            self.config = StrategyConfig()
        else:
            self.config = config
        self.param_prefix = param_prefix
        self.alt_param_prefixes = alt_param_prefixes or []
        self.base_version: str = base_version or getattr(self.__class__, "BASE_VERSION", "1.7.0")
        strat_version: str = version or getattr(self.__class__, "version", "1.0.0")
        tags = factor_tags or [
            "regime_trend_strength",
            "absolute_momentum_trend",
            "relative_momentum",
            "volatility_targeting",
        ]
        super().__init__(name=name, param_grid={}, factor_tags=tags, version=strat_version)

    def get_version_info(self, params: Optional[dict] = None) -> Dict[str, Any]:
        sub_strats = {}
        try:
            sub_strats = self.get_sub_strategies(universe={}, params=params)
        except Exception:
            sub_strats = {}
        return {
            "version": str(self.version),
            "base_version": str(self.base_version),
            "composite_version": f"{self.version} [base: {self.base_version}]",
            "template_name": self.name,
            "strategy_class": self.__class__.__name__,
            "sub_strategies": {
                k: {
                    "class": inst.__class__.__name__,
                    "version": getattr(inst, "version", "1.0.0"),
                }
                for k, inst in (sub_strats or {}).items()
            },
        }

    @property
    def version_string(self) -> str:
        return f"v{self.version} [base: v{self.base_version}]"

    def resolve_param(
        self,
        p: dict,
        name: str,
        default: Any = None,
        param_type: Optional[type] = None,
    ) -> Any:
        """Resolves configuration parameter across runtime params and StrategyConfig.

        Resolution precedence:
        1. p[f"{param_prefix}_{name}"]
        2. p[f"{alt_prefix}_{name}"] (e.g. ccsb fallback to crb)
        3. p[f"blend_{name}"]
        4. p[name]
        5. getattr(cfg, f"{param_prefix}_{name}")
        6. getattr(cfg, f"{alt_prefix}_{name}")
        7. getattr(cfg, f"blend_{name}")
        8. getattr(cfg, name)
        9. default
        """
        cfg = self.config
        prefixes = [self.param_prefix] + self.alt_param_prefixes

        # 1. Runtime parameter overrides
        for prefix in prefixes:
            if prefix:
                full_key = f"{prefix}_{name}"
                if full_key in p:
                    val = p[full_key]
                    return param_type(val) if param_type is not None and val is not None else val

        if f"blend_{name}" in p:
            val = p[f"blend_{name}"]
            return param_type(val) if param_type is not None and val is not None else val

        if name in p:
            val = p[name]
            return param_type(val) if param_type is not None and val is not None else val

        # 2. Config object attributes
        for prefix in prefixes:
            if prefix:
                full_key = f"{prefix}_{name}"
                if hasattr(cfg, full_key):
                    val = getattr(cfg, full_key)
                    if val is not None:
                        return param_type(val) if param_type is not None else val

        if hasattr(cfg, f"blend_{name}"):
            val = getattr(cfg, f"blend_{name}")
            if val is not None:
                return param_type(val) if param_type is not None else val

        if hasattr(cfg, name):
            val = getattr(cfg, name)
            if val is not None:
                return param_type(val) if param_type is not None else val

        return default

    # -- Task C6.2: the two DISTINCT "confluence" sizing switches ----------------
    #
    # There are two unrelated switches whose names both contain "confluence":
    #
    #   * LEGACY_CONFLUENCE_SIZING_KEY (`confluence_sizing`, blend-prefixed
    #     `blend_confluence_sizing`, cdhb-prefixed `cdhb_confluence_sizing`)
    #     -> the long-standing `c_mult` confluence/resonance sizing block that
    #        multiplies the pre-emptive thrust score by 0.6 / 1.1 / 1.6. Default
    #        True for the `cdhb` prefix; `chan_dual_hybrid_blend` pins it True in
    #        strategies_config.json.
    #   * TRACK_C_ANCHOR3_CONFLUENCE_SIZING_KEY (`enable_vp_confluence_sizing`,
    #     blend-prefixed `blend_enable_vp_confluence_sizing`) -> Track C Anchor 3,
    #     the VP/VAH + Wyckoff effort-result *modifier* that nudges `conf_count`
    #     and applies a 0.6 damping. Default False and absent from every blend
    #     block in strategies_config.json, i.e. Anchor 3 is OFF everywhere today.
    #
    # They are NOT synonyms and neither implies the other. Anchor 3 is a modifier
    # of the legacy block, so its code is nested inside `if legacy_confluence_sizing:`
    # -- that nesting is a documented precondition, not a shared switch. Auditing
    # Track C activation by grepping for the substring "confluence" is therefore
    # WRONG; use `confluence_sizing_gates()` below.
    LEGACY_CONFLUENCE_SIZING_KEY: str = "confluence_sizing"
    TRACK_C_ANCHOR3_CONFLUENCE_SIZING_KEY: str = "enable_vp_confluence_sizing"

    def confluence_sizing_gates(self, params: Optional[dict] = None) -> Dict[str, bool]:
        """Resolve the two independent confluence switches, each from ITS OWN key.

        Returns
        -------
        dict with two keys:
          * ``legacy_confluence_sizing`` -- the legacy `c_mult` confluence/resonance
            sizing (``confluence_sizing`` / ``<prefix>_confluence_sizing``).
          * ``anchor3_vp_confluence_sizing`` -- Track C Anchor 3
            (``enable_vp_confluence_sizing`` / ``<prefix>_enable_vp_confluence_sizing``).

        This is the single authoritative attribution point: `generate_weights`
        reads both flags from here, and the audit/regression tests assert against
        it, so `cdhb_confluence_sizing=true` can never again be read as
        "Anchor 3 is enabled".

        NOTE (deliberate): the two key strings are passed to `resolve_param` as
        LITERALS, not through the class constants above. The workspace's config-wiring
        analyzer (`pipeline/tests/test_config_wiring.py`) proves a `StrategyConfig` field
        is wired by matching a literal string at `resolve_param`'s name argument, so a
        constant would make all three of `cdhb_confluence_sizing`,
        `blend_enable_vp_confluence_sizing` and `enable_vp_confluence_sizing` look like
        zombies. `test_c62_anchor3_is_nested_in_legacy_block_and_keys_stay_distinct`
        pins the constants to these literals so the two cannot drift apart.
        """
        p = params or {}
        return {
            "legacy_confluence_sizing": self.resolve_param(
                p, "confluence_sizing", (self.param_prefix == "cdhb"), bool
            ),
            "anchor3_vp_confluence_sizing": self.resolve_param(
                p, "enable_vp_confluence_sizing", False, bool
            ),
        }

    def get_sub_strategies(
        self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None
    ) -> Dict[str, AllocationTemplate]:
        """Subclasses must implement this to return {strategy_key: template_instance}."""
        raise NotImplementedError("Subclasses must implement get_sub_strategies")

    def get_sub_strategy_weights(self, params: Optional[dict] = None) -> Dict[str, float]:
        """Subclasses must implement this to return {strategy_key: base_weight}."""
        raise NotImplementedError("Subclasses must implement get_sub_strategy_weights")

    def compute_regime_weights(
        self,
        base_weights: Dict[str, float],
        breadth: float,
        thrust: float,
        date: Any,
        params: Optional[dict] = None,
    ) -> Dict[str, float]:
        """Hook for dynamic regime-adaptive weighting (Bull / Bear / Chop).
        Default returns base_weights unchanged.
        """
        return base_weights

    def _classify_sub_strategy_styles(
        self, sub_strats: Dict[str, AllocationTemplate], params: Optional[dict] = None
    ) -> Dict[str, str]:
        """Classifies sub-strategies into institutional style buckets:
        - 'momentum': Trend following, multi-scale momentum, breakout (e.g. chan_three_type, wq_alpha41)
        - 'reversal': Mean reversion, microstructure, shadow exhaustion (e.g. wq_alpha12, wq_alpha53, gtja_alpha16)
        - 'defensive': Crash protection, carry, cash preservation (e.g. chan_vaa_compound, permanent, BIL)
        """
        classification = {}
        for sname, strat in sub_strats.items():
            name_lower = sname.lower()
            tags = getattr(strat, "factor_tags", []) or []
            tags_lower = [str(t).lower() for t in tags]

            # Priority 1: Defensive / Carry
            if (
                any(k in name_lower for k in ("vaa", "defensive", "permanent", "bil", "hfea", "cash"))
                or any(k in tags_lower for k in ("defensive", "carry", "cash"))
            ):
                classification[sname] = "defensive"
            # Priority 2: Reversal / Microstructure
            elif (
                any(k in name_lower for k in ("reversal", "mean_reversion", "rsi", "gtja", "alpha12", "alpha53", "alpha101", "shadow", "microstructure", "spring", "wyckoff"))
                or any(k in tags_lower for k in ("mean_reversion", "reversal", "microstructure", "order_flow"))
            ):
                classification[sname] = "reversal"
            # Priority 3: Momentum / Trend
            elif (
                any(k in name_lower for k in ("momentum", "trend", "breakout", "three_type", "turtle", "alpha41", "alpha6", "alpha38", "chan_vp"))
                or any(k in tags_lower for k in ("relative_momentum", "absolute_momentum_trend", "regime_trend_strength", "breakout"))
            ):
                classification[sname] = "momentum"
            else:
                if "vaa" in name_lower:
                    classification[sname] = "defensive"
                else:
                    classification[sname] = "momentum"
        return classification

    def _compute_rolling_sub_strategy_performance(
        self, sub_returns_slice: pd.DataFrame, min_tstat: float = 2.0
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Computes rolling annualized Information Ratio and Harvey-Liu-Zhu t-statistic.
        Applies Harvey, Liu & Zhu (2016) significance hurdle: zero out IR if t-stat < min_tstat or mean <= 0.
        """
        ir_map = {}
        tstat_map = {}
        if sub_returns_slice.empty or len(sub_returns_slice) < 5:
            return ir_map, tstat_map

        n_bars = len(sub_returns_slice)
        sqrt_n = np.sqrt(n_bars)
        for col in sub_returns_slice.columns:
            series = sub_returns_slice[col]
            mu = float(series.mean())
            sigma = float(series.std(ddof=1))
            if sigma > 1e-8 and mu > 0.0:
                t_stat = (mu / sigma) * sqrt_n
                ir_ann = (mu / sigma) * np.sqrt(252.0)
            else:
                t_stat = 0.0
                ir_ann = 0.0

            tstat_map[col] = t_stat
            if t_stat >= min_tstat:
                ir_map[col] = ir_ann
            else:
                ir_map[col] = 0.0

        return ir_map, tstat_map

    def _apply_lowdin_orthogonalization(self, sub_weights_matrix: pd.DataFrame) -> pd.DataFrame:
        """Applies Löwdin symmetric orthogonalization: W* = W (W^T W)^{-1/2}.
        Removes collinearity while minimizing Frobenius distance to original weights.
        """
        W = sub_weights_matrix.values.astype(float)
        n_assets, n_strats = W.shape
        if n_strats <= 1 or np.all(np.abs(W) < 1e-6):
            return sub_weights_matrix.copy()

        col_sums = W.sum(axis=0)
        active_cols = col_sums > 1e-6
        if np.sum(active_cols) <= 1:
            return sub_weights_matrix.copy()

        S = W.T @ W
        try:
            eigvals, eigvecs = np.linalg.eigh(S)
            eigvals_clipped = np.maximum(eigvals, 1e-6)
            S_inv_sqrt = eigvecs @ np.diag(1.0 / np.sqrt(eigvals_clipped)) @ eigvecs.T
            W_star = W @ S_inv_sqrt
            W_star = np.maximum(0.0, W_star)
            for j in range(n_strats):
                csum = W_star[:, j].sum()
                if csum > 1e-6 and col_sums[j] > 1e-6:
                    W_star[:, j] = W_star[:, j] * (col_sums[j] / csum)
            return pd.DataFrame(W_star, index=sub_weights_matrix.index, columns=sub_weights_matrix.columns)
        except Exception:
            return sub_weights_matrix.copy()

    def _compute_adaptive_weights(
        self,
        sub_strats: Dict[str, AllocationTemplate],
        base_weights: Dict[str, float],
        sub_returns_slice: pd.DataFrame,
        weighting_mode: str,
        breadth_val: float,
        thrust: float,
        vol_z: float,
        persistence: float,
        adaptive_min_tstat: float,
        adaptive_shrinkage: float,
        date: Any,
        params: Optional[dict] = None,
        median_d: Optional[float] = None,
        # (2026-10-22 incident) Optional hooks threaded from generate_weights:
        # - thrust_active: hysteresis-adjusted macro thrust flag. When None the
        #   legacy `thrust >= thrust_thresh` comparison is used unchanged.
        # - prev_bucket_weights: previous bar's normalized style-bucket weights
        #   used to delta-clamp renormalization (Fix 3).
        # - bucket_renorm_max_delta: max per-bar style-bucket weight change;
        #   None disables clamping (unchanged legacy behavior).
        # - bucket_weights_out: mutable dict mutated in place to record this
        #   bar's normalized bucket weights for the next iteration.
        thrust_active: Optional[bool] = None,
        prev_bucket_weights: Optional[Dict[str, float]] = None,
        bucket_renorm_max_delta: Optional[float] = None,
        bucket_weights_out: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Dynamically computes sub-strategy blend weights under selected weighting mode:
        - 'rolling_ic_ir': Bayesian shrinkage on rolling annualized IR with HLZ t-stat filter.
        - 'regime_adaptive': Gu-Kelly-Xiu macro style routing across Momentum / Reversal / Defensive.
        - 'regime_ic_ir_hybrid': Two-layer hybrid combining macro style budget routing and intra-bucket rolling IC-IR.
        """
        p = params or {}
        thrust_thresh = self.resolve_param(p, "thrust_thresh", 0.60, float)
        breadth_bull_thresh = self.resolve_param(p, "breadth_bull_thresh", 0.30, float)
        enable_structural_regime = self.resolve_param(p, "enable_structural_regime", True, bool)
        if bucket_renorm_max_delta is None:
            bucket_renorm_max_delta = self.resolve_param(p, "bucket_renorm_max_delta", None, float)
        # Hysteresis-adjusted thrust gate: use the threaded flag when present,
        # otherwise fall back to the exact legacy comparison.
        thrust_on = (thrust >= thrust_thresh) if thrust_active is None else bool(thrust_active)

        # If all sub-strategies have zero base weights (e.g. explicitly disabled by caller), return as-is
        if sum(base_weights.values()) <= 1e-6:
            return base_weights

        if weighting_mode == "rolling_ic_ir":
            if sub_returns_slice.empty or len(sub_returns_slice) < 5:
                return base_weights
            ir_map, _ = self._compute_rolling_sub_strategy_performance(sub_returns_slice, adaptive_min_tstat)
            active_keys = [k for k, bw in base_weights.items() if bw > 1e-6]
            if not active_keys:
                return base_weights
            tot_ir = sum(max(0.0, ir_map.get(k, 0.0)) for k in active_keys)
            if tot_ir > 1e-6:
                weights = {}
                for k, bw in base_weights.items():
                    if k in active_keys:
                        raw_w = max(0.0, ir_map.get(k, 0.0)) / tot_ir
                        weights[k] = (1.0 - adaptive_shrinkage) * raw_w + adaptive_shrinkage * bw
                    else:
                        weights[k] = 0.0
                tot_w = sum(weights.values())
                return {k: v / tot_w for k, v in weights.items()} if tot_w > 0 else base_weights
            return base_weights

        elif weighting_mode in ("regime_adaptive", "regime_ic_ir_hybrid"):
            style_map = self._classify_sub_strategy_styles(sub_strats, p)
            # Track C Anchor 1: Macro State Machine Budgeting with Fractal Dimension D_t
            if enable_structural_regime and median_d is not None and not np.isnan(median_d):
                if (breadth_val >= 0.60) or thrust_on or (median_d < 1.45 and vol_z < 1.0):
                    budgets = {"momentum": 0.80, "reversal": 0.15, "defensive": 0.05}
                elif (breadth_val >= breadth_bull_thresh and vol_z <= 2.0) or (median_d > 1.55):
                    budgets = {"momentum": 0.15, "reversal": 0.75, "defensive": 0.10}
                else:
                    budgets = {"momentum": 0.05, "reversal": 0.10, "defensive": 0.85}
            else:
                # Gu, Kelly & Xiu (2020) macro regime budget assignment
                if (breadth_val >= 0.60) or thrust_on or (persistence >= 0.65 and vol_z < 1.0):
                    budgets = {"momentum": 0.75, "reversal": 0.20, "defensive": 0.05}
                elif (breadth_val >= breadth_bull_thresh and vol_z <= 2.0):
                    budgets = {"momentum": 0.20, "reversal": 0.65, "defensive": 0.15}
                else:
                    budgets = {"momentum": 0.05, "reversal": 0.10, "defensive": 0.85}

            bucket_members = {"momentum": [], "reversal": [], "defensive": []}
            for k, bw in base_weights.items():
                if bw > 1e-6:
                    b_name = style_map.get(k, "momentum")
                    bucket_members[b_name].append(k)

            # (2026-10-22 incident) Empty style buckets previously went unnoticed:
            # a fully-renormalized budget that silently omitted a style bucket
            # concentrated the portfolio (e.g. defensive bucket jump). Warn so a
            # missing classification is visible instead of silently renormalizing.
            empty_buckets = [b for b in ("momentum", "reversal", "defensive") if not bucket_members[b]]
            if empty_buckets:
                _LOGGER.warning(
                    "regime weighting: empty style bucket(s) %s at date=%s — sub-strategy style classification may be incomplete",
                    empty_buckets,
                    date,
                )
            _LOGGER.debug(
                "regime weighting bar=%s breadth=%.3f thrust=%.3f budgets=%s members=%s",
                date, breadth_val, thrust, budgets, {b: len(v) for b, v in bucket_members.items()},
            )

            active_budgets = {b: budgets[b] for b in budgets if bucket_members[b]}
            tot_b = sum(active_budgets.values())
            if tot_b > 0:
                norm_budgets = {b: v / tot_b for b, v in active_budgets.items()}
            else:
                if bucket_weights_out is not None:
                    bucket_weights_out.clear()
                return base_weights

            _LOGGER.debug("regime weighting bar=%s pre-renorm=%s", date, norm_budgets)

            # (2026-10-22 incident) Delta-clamp bucket renormalization toward the
            # previous bar's bucket weights to prevent a single-bar regime flip
            # from 4x-ing a style bucket (defensive 0.10 -> 0.40). The new bucket
            # allocation may move at most `bucket_renorm_max_delta` from its prior
            # value, then renormalizes. None leaves legacy behavior unchanged.
            if bucket_renorm_max_delta is not None and prev_bucket_weights:
                clamped = {}
                for b, alloc in norm_budgets.items():
                    prev = float(prev_bucket_weights.get(b, 0.0))
                    if alloc > prev + bucket_renorm_max_delta:
                        clamped[b] = prev + bucket_renorm_max_delta
                    elif alloc < prev - bucket_renorm_max_delta:
                        clamped[b] = max(0.0, prev - bucket_renorm_max_delta)
                    else:
                        clamped[b] = alloc
                tot_clamped = sum(max(0.0, v) for v in clamped.values())
                if tot_clamped > 0:
                    norm_budgets = {b: max(0.0, v) / tot_clamped for b, v in clamped.items()}
                    _LOGGER.debug("regime weighting bar=%s delta-clamped=%s", date, norm_budgets)

            # Record this bar's normalized bucket weights for next iteration.
            if bucket_weights_out is not None:
                bucket_weights_out.clear()
                bucket_weights_out.update(norm_budgets)

            weights = {}
            if weighting_mode == "regime_adaptive" or sub_returns_slice.empty or len(sub_returns_slice) < 5:
                for b, b_alloc in norm_budgets.items():
                    members = bucket_members[b]
                    sum_base = sum(base_weights[m] for m in members)
                    for m in members:
                        rel_bw = (base_weights[m] / sum_base) if sum_base > 0 else (1.0 / len(members))
                        weights[m] = b_alloc * rel_bw
            else:
                # Two-layer hybrid: macro budget + intra-bucket rolling IC-IR
                ir_map, _ = self._compute_rolling_sub_strategy_performance(sub_returns_slice, adaptive_min_tstat)
                for b, b_alloc in norm_budgets.items():
                    members = bucket_members[b]
                    sum_base = sum(base_weights[m] for m in members)
                    tot_ir_b = sum(max(0.0, ir_map.get(m, 0.0)) for m in members)
                    for m in members:
                        rel_bw = (base_weights[m] / sum_base) if sum_base > 0 else (1.0 / len(members))
                        if tot_ir_b > 1e-6:
                            raw_ir_w = max(0.0, ir_map.get(m, 0.0)) / tot_ir_b
                            intra_w = (1.0 - adaptive_shrinkage) * raw_ir_w + adaptive_shrinkage * rel_bw
                        else:
                            intra_w = rel_bw
                        weights[m] = b_alloc * intra_w

            tot_w = sum(weights.values())
            return {k: v / tot_w for k, v in weights.items()} if tot_w > 0 else base_weights

        # Fallback to static compute_regime_weights hook
        return self.compute_regime_weights(base_weights, breadth_val, thrust, date, p)

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}

        # Core parameters with institutional defaults
        cash_proxy = self.resolve_param(p, "cash_proxy", getattr(cfg, "cash_proxy", "BIL"), str)
        max_single_pos = self.resolve_param(p, "max_single_position", 0.20, float)
        bull_max_pos = self.resolve_param(p, "bull_max_single_position", 0.20, float)
        min_weight_change = self.resolve_param(p, "min_weight_change", 0.05, float)
        entry_thresh = self.resolve_param(p, "entry_threshold", min_weight_change, float)
        min_holding_bars = self.resolve_param(p, "min_holding_bars", 2, int)
        min_entry_bars = self.resolve_param(p, "min_entry_bars", self.resolve_param(p, "guard_latency_bars", 2, int), int)
        thrust_incumbent_bonus = self.resolve_param(p, "thrust_incumbent_bonus", 1.20, float)
        dd_reduce_thresh = self.resolve_param(p, "dd_reduce_thresh", 0.10, float)
        dd_defensive_thresh = self.resolve_param(p, "dd_defensive_thresh", 0.15, float)
        dd_stop_thresh = self.resolve_param(p, "dd_stop_thresh", 0.20, float)
        tier1_cooldown_bars = self.resolve_param(p, "tier1_cooldown_bars", 15, int)
        stop_cooldown_bars = self.resolve_param(p, "stop_cooldown_bars", 21, int)
        dynamic_cash = self.resolve_param(p, "dynamic_cash_deployment", True, bool)
        breadth_lookback = self.resolve_param(p, "breadth_lookback", 50, int)
        breadth_bull_thresh = self.resolve_param(p, "breadth_bull_thresh", 0.30, float)
        thrust_lookback = self.resolve_param(p, "thrust_lookback", 10, int)
        thrust_thresh = self.resolve_param(p, "thrust_thresh", 0.60, float)
        target_bull_exposure = self.resolve_param(p, "target_bull_exposure", 0.80, float)
        enable_vol_targeting = self.resolve_param(p, "enable_vol_targeting", True, bool)
        target_vol = self.resolve_param(p, "target_vol", 0.12, float)
        target_bull_vol = self.resolve_param(p, "target_bull_vol", 0.24, float)
        target_bear_vol = self.resolve_param(p, "target_bear_vol", 0.12, float)
        smooth_drawdown = self.resolve_param(p, "smooth_drawdown", True, bool)

        # Institutional feature switches (defaulting to enabled per workspace instructions)
        vol_model = self.resolve_param(p, "vol_model", "garman_klass", str)
        stop_mode = self.resolve_param(p, "stop_mode", "atr_trailing", str)
        stop_atr_mult = self.resolve_param(p, "stop_atr_mult", 4.5, float)
        asset_stop_cooldown_bars = self.resolve_param(p, "asset_stop_cooldown_bars", 5, int)
        normal_cooldown_bars = self.resolve_param(p, "normal_cooldown_bars", self.resolve_param(p, "reentry_cooldown_bars", 1, int), int)
        adaptive_inertia = self.resolve_param(p, "adaptive_inertia", True, bool)
        asymmetric_sells = self.resolve_param(p, "asymmetric_sells", (self.param_prefix == "cdhb"), bool)
        preemptive_thrust = self.resolve_param(p, "preemptive_thrust_deployment", True, bool)
        min_thrust_assets = self.resolve_param(p, "min_thrust_assets", 5, int)
        enable_adaptive_stop_atr = self.resolve_param(p, "enable_adaptive_stop_atr", True, bool)
        enable_low_beta_inertia = self.resolve_param(p, "enable_low_beta_inertia", True, bool)
        max_assets_per_sector = self.resolve_param(p, "max_assets_per_sector", 2, int)
        adaptive_sector_throttle = self.resolve_param(p, "adaptive_sector_throttle", False, bool)
        sector_incumbent_hurdle = self.resolve_param(p, "sector_incumbent_hurdle", 0.03, float)
        thrust_min_roc = self.resolve_param(p, "thrust_min_roc", 0.01, float)
        thrust_sizing_mode = self.resolve_param(p, "thrust_sizing_mode", "vol_adjusted", str)
        require_asset_trend = self.resolve_param(p, "require_asset_trend", (self.param_prefix == "cdhb"), bool)
        require_rs_rank = self.resolve_param(p, "require_rs_rank", (self.param_prefix == "cdhb"), bool)
        # Task C6.2 (attribution): this is the LEGACY `c_mult` confluence/resonance
        # sizing, NOT Track C Anchor 3. Both confluence switches are resolved together
        # through `confluence_sizing_gates()` so their keys can never be swapped again
        # -- see the docstring there. `cdhb_confluence_sizing: true` in
        # strategies_config.json means "legacy resonance sizing ON", never "Anchor 3 ON".
        _confluence_gates = self.confluence_sizing_gates(p)
        legacy_confluence_sizing = _confluence_gates["legacy_confluence_sizing"]
        elastic_dd_reset = self.resolve_param(p, "elastic_drawdown_reset", (self.param_prefix == "cdhb"), bool)

        # Blend Base adaptive and weighting mode configuration
        weighting_mode = self.resolve_param(p, "weighting_mode", "rolling_ic_ir", str)
        adaptive_lookback_days = self.resolve_param(p, "adaptive_lookback_days", 60, int)
        adaptive_min_tstat = self.resolve_param(p, "adaptive_min_tstat", 2.0, float)
        adaptive_shrinkage = self.resolve_param(p, "adaptive_shrinkage", 0.20, float)
        enable_orthogonalization = self.resolve_param(p, "enable_orthogonalization", True, bool)

        # (2026-10-22 incident) Regime hysteresis (Fix 4) + bucket renormalization
        # delta-clamp (Fix 3). All default to no-op so other strategies' backtest
        # numbers are byte-for-byte unchanged.
        enable_regime_hysteresis = self.resolve_param(p, "enable_regime_hysteresis", False, bool)
        thrust_enter_thresh = self.resolve_param(p, "thrust_enter_thresh", 0.62, float)
        thrust_exit_thresh = self.resolve_param(p, "thrust_exit_thresh", 0.58, float)
        bucket_renorm_max_delta = self.resolve_param(p, "bucket_renorm_max_delta", None, float)

        # 5-Minute Intraday Microstructure & Interval Nesting Parameters
        enable_intraday_5m = self.resolve_param(p, "enable_intraday_5m", False, bool)
        enable_5m_interval_nesting = self.resolve_param(p, "enable_5m_interval_nesting", False, bool)
        intraday_upvol_thresh = self.resolve_param(p, "intraday_upvol_thresh", 0.58, float)
        use_intraday_downside_vol = self.resolve_param(p, "use_intraday_downside_vol", False, bool)
        enable_5m_stop_filter = self.resolve_param(p, "enable_5m_stop_filter", False, bool)
        intraday_cutoff_time = self.resolve_param(p, "intraday_cutoff_time", "14:15", str)

        # Track C Multi-Theory Quantitative Synthesis Parameters (Anchors 1-5)
        enable_structural_regime = self.resolve_param(p, "enable_structural_regime", True, bool)
        enable_cyq_quality_gate = self.resolve_param(p, "enable_cyq_quality_gate", True, bool)
        min_cyq_winner_ratio = self.resolve_param(p, "min_cyq_winner_ratio", 0.20, float)
        # Task C6.1: Anchor 2's consolidation-density veto threshold. This MUST come
        # from the shared `chan_vp_min_consolidation_density` field (StrategyConfig
        # default 0.45, also 0.45 in strategies_config.json) -- the same field the
        # `chan_vp_hybrid` path already honours. Before this fix the veto was a bare
        # `0.40` float literal, so the configured value was inert on this path and the
        # live threshold silently disagreed with the declared one.
        min_consolidation_density = self.resolve_param(p, "chan_vp_min_consolidation_density", 0.45, float)
        # Task C6.2: Anchor 3's own switch (NOT the legacy `c_mult` sizing above).
        enable_vp_confluence_sizing = _confluence_gates["anchor3_vp_confluence_sizing"]
        enable_spring_fast_entry = self.resolve_param(p, "enable_spring_fast_entry", False, bool)
        enable_structural_chandelier_stop = self.resolve_param(p, "enable_structural_chandelier_stop", False, bool)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        risky_symbols = _get_risky_symbols_helper(universe, p, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        # Fallback on-demand structural feature enrichment if not pre-enriched
        if (
            enable_cyq_quality_gate
            or enable_vp_confluence_sizing
            or enable_spring_fast_entry
            or enable_structural_chandelier_stop
        ):
            needs_enrich = any(
                sym in universe
                and not universe[sym].empty
                and "cyq_winner_ratio" not in universe[sym].columns
                for sym in risky_symbols
            )
            if needs_enrich:
                from common.structural_factors import enrich_universe_with_structural_features
                universe = enrich_universe_with_structural_features(universe)

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        # Step 1: Precompute market breadth, 10d thrust, ROC, 50d trend, 20d slope, volume surge, and ATR
        breadth_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        thrust_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        roc_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        roc_60_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        trend_50_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        ma20_slope_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        vol_surge_matrix = pd.DataFrame(index=master_index, columns=risky_symbols, dtype=float)
        vol_breakout_matrix = pd.DataFrame(0.0, index=master_index, columns=risky_symbols, dtype=float)
        asset_returns = pd.DataFrame(0.0, index=master_index, columns=risky_symbols)
        asset_vol_21d = pd.DataFrame(0.0, index=master_index, columns=risky_symbols)
        atr_14_matrix = pd.DataFrame(0.0, index=master_index, columns=risky_symbols)

        for sym in risky_symbols:
            if sym in universe and not universe[sym].empty:
                df_s = universe[sym]
                c = df_s["Close"].reindex(master_index).ffill()
                ma_b = sma(c, breadth_lookback)
                breadth_matrix[sym] = (c > ma_b).astype(float)
                ma50 = sma(c, 50)
                ma20 = sma(c, 20)
                ma20_prev = ma20.shift(5)
                trend_50_matrix[sym] = (c >= ma50).astype(float)
                ma20_slope_matrix[sym] = (ma20 >= ma20_prev).astype(float)
                c_prev = c.shift(thrust_lookback)
                roc_thrust = (c / c_prev - 1.0)
                roc_matrix[sym] = roc_thrust
                roc_60_matrix[sym] = (c / c.shift(60) - 1.0).fillna(0.0)
                thrust_matrix[sym] = (roc_thrust > 0.0).astype(float)
                if "Volume" in df_s.columns:
                    v = df_s["Volume"].reindex(master_index).ffill()
                    v_ma20 = sma(v, 20)
                    base_surge = (v >= 1.2 * v_ma20)
                    if enable_intraday_5m and "UpVol_Ratio_5m" in df_s.columns:
                        up_vol_s = df_s["UpVol_Ratio_5m"].reindex(master_index)
                        if "VWAP_Prem_5m" in df_s.columns:
                            vwap_prem_s = df_s["VWAP_Prem_5m"].reindex(master_index)
                            vwap_ok = vwap_prem_s.isna() | (vwap_prem_s >= 0.0)
                        elif "VWAP" in df_s.columns:
                            vwap_s = df_s["VWAP"].reindex(master_index)
                            vwap_ok = vwap_s.isna() | (c >= vwap_s)
                        else:
                            vwap_ok = True
                        up_vol_ok = up_vol_s.isna() | (up_vol_s >= intraday_upvol_thresh)
                        vol_surge_matrix[sym] = (base_surge & up_vol_ok & vwap_ok).astype(float)
                    else:
                        vol_surge_matrix[sym] = base_surge.astype(float)
                    vol_breakout_matrix[sym] = ((v >= 1.5 * v_ma20) & (v_ma20 > 0)).astype(float).fillna(0.0)
                else:
                    vol_surge_matrix[sym] = 1.0
                    vol_breakout_matrix[sym] = 0.0

                asset_returns[sym] = c.pct_change().fillna(0.0)
                if use_intraday_downside_vol and "RS_Down_5m" in df_s.columns:
                    rs_down = df_s["RS_Down_5m"].reindex(master_index)
                    # 21-day rolling mean of (2 * RS_Down_5m), annualized by sqrt(252)
                    df_sym = df_s.reindex(master_index).ffill()
                    gk_fallback = _compute_garman_klass_vol(df_sym, 21, target_vol)
                    downside_vol = np.sqrt(252.0 * (2.0 * rs_down).rolling(21, min_periods=5).mean())
                    asset_vol_21d[sym] = downside_vol.fillna(gk_fallback).fillna(target_vol)
                elif vol_model == "garman_klass":
                    df_sym = df_s.reindex(master_index).ffill()
                    asset_vol_21d[sym] = _compute_garman_klass_vol(df_sym, 21, target_vol)
                else:
                    asset_vol_21d[sym] = (asset_returns[sym].rolling(21, min_periods=10).std() * np.sqrt(252)).fillna(target_vol)

                if stop_mode == "atr_trailing":
                    df_sym = df_s.reindex(master_index).ffill()
                    atr_14_matrix[sym] = atr(df_sym, 14).fillna(c * 0.02)
            else:
                asset_vol_21d[sym] = target_vol

        daily_breadth = breadth_matrix.mean(axis=1).fillna(0.50)
        daily_thrust = thrust_matrix.mean(axis=1).fillna(0.50)

        # Precompute diversified basket return volatility and macro state indicators
        basket_ret = asset_returns.mean(axis=1).fillna(0.0)
        basket_vol_21d = (basket_ret.rolling(21, min_periods=10).std() * np.sqrt(252)).fillna(target_vol)
        vol_mean_252 = basket_vol_21d.rolling(252, min_periods=20).mean().fillna(target_vol)
        vol_std_252 = basket_vol_21d.rolling(252, min_periods=20).std().replace(0, np.nan).fillna(0.04)
        vol_zscore_series = ((basket_vol_21d - vol_mean_252) / vol_std_252).fillna(0.0)
        persistence_series = (basket_ret > 0).astype(float).rolling(20, min_periods=5).mean().fillna(0.50)

        # Precompute cross-sectional median fractal dimension D_t (Track C Anchor 1)
        fractal_d_series = None
        if enable_structural_regime:
            fd_cols = [
                universe[s]["fractal_dimension"].reindex(master_index)
                for s in risky_symbols
                if s in universe and not universe[s].empty and "fractal_dimension" in universe[s].columns
            ]
            if fd_cols:
                fractal_d_series = pd.concat(fd_cols, axis=1).median(axis=1).ffill().bfill()
            else:
                try:
                    from common.structural_factors import fractal_dimension
                    basket_price = (1.0 + basket_ret).cumprod()
                    fractal_d_series = fractal_dimension(basket_price, window=60).reindex(master_index).ffill().bfill()
                except Exception:
                    fractal_d_series = None

        # Step 2: Execute sub-strategies and get daily forward-filled weights
        sub_strats = self.get_sub_strategies(universe, p)
        sub_daily = {}
        for sname, strat in sub_strats.items():
            w_sp = strat.generate_weights(universe, p)
            if not w_sp.empty:
                w_d = w_sp.reindex(master_index).ffill().fillna(0.0)
            else:
                w_d = pd.DataFrame(0.0, index=master_index, columns=symbols)
            sub_daily[sname] = _fill_out_columns(w_d, symbols)

        # Base sub-strategy weights
        raw_sub_weights = self.get_sub_strategy_weights(p)
        tot_sw = sum(raw_sub_weights.values())
        base_weights = {k: v / tot_sw for k, v in raw_sub_weights.items()} if tot_sw > 0 else raw_sub_weights

        # Precompute sub-strategy daily portfolio returns history for causal rolling performance tracking
        sub_returns_history = pd.DataFrame(0.0, index=master_index, columns=list(sub_strats.keys()))
        for sname in sub_strats.keys():
            if sname in sub_daily:
                shifted_w = sub_daily[sname].shift(1)[risky_symbols].fillna(0.0)
                sub_returns_history[sname] = (shifted_w * asset_returns[risky_symbols]).sum(axis=1).fillna(0.0)

        # Find defensive sub-strategy key if available (typically ChanVaaCompoundStrategy)
        defensive_key = None
        for k in sub_strats.keys():
            if "vaa" in k.lower():
                defensive_key = k
                break
        if defensive_key is None and sub_strats:
            for k in sub_strats.keys():
                if "defensive" in k.lower() or "permanent" in k.lower():
                    defensive_key = k
                    break

        # Step 3: Simulation loop tracking portfolio NAV, drawdown, stops, and dynamic cash
        daily_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        desired_weights = pd.DataFrame(0.0, index=master_index, columns=symbols)
        cum_nav = 1.0
        peak_nav = 1.0
        nav_history = []
        port_ret_history = []
        tier1_counter = 0
        stop_counter = 0
        emergency_flags = []
        asset_emergency_df = pd.DataFrame(False, index=master_index, columns=risky_symbols)

        asset_cooldown_until = {s: 0 for s in risky_symbols}
        asset_entry_price = {s: None for s in risky_symbols}
        asset_peak_price = {s: None for s in risky_symbols}
        entry_signal_bars = {s: 0 for s in risky_symbols}
        expansion_signal_bars = {s: 0 for s in risky_symbols}

        use_inertia = (min_weight_change > 0.0 or min_holding_bars > 0)
        inertia_thresh_df = None
        static_inertia_thresh = pd.Series(float(min_weight_change), index=risky_symbols)
        if use_inertia and adaptive_inertia and risky_symbols:
            clip_min = max(0.02, 0.5 * min_weight_change)
            clip_max = max(clip_min, min(0.08, 2.0 * min_weight_change))
            expanding_asset_vol = asset_vol_21d[risky_symbols].expanding(min_periods=1).mean().fillna(target_vol)
            expanding_mkt_vol = expanding_asset_vol.mean(axis=1).clip(lower=0.01)
            inertia_thresh_df = (
                expanding_asset_vol.div(expanding_mkt_vol, axis=0) * min_weight_change
            ).clip(lower=clip_min, upper=clip_max)
        holding_age = {s: 0 for s in risky_symbols}
        last_buy_bar = {s: -999 for s in risky_symbols}

        # (2026-10-22 incident) Per-bar regime state threaded across iterations:
        # `prev_state["thrust_active"]` feeds regime hysteresis (Fix 4) and
        # `prev_bucket_weights` feeds bucket renormalization delta-clamp (Fix 3).
        prev_state: Dict[str, Any] = {"thrust_active": False}
        prev_bucket_weights: Dict[str, float] = {}

        for t in range(len(master_index)):
            date = master_index[t]

            # Update NAV based on positions held from previous day
            if t > 0:
                prev_date = master_index[t - 1]
                held_risky = daily_weights.loc[prev_date, risky_symbols]
                port_ret = float((held_risky * asset_returns.loc[date]).sum())
                cum_nav *= (1.0 + port_ret)
                port_ret_history.append(port_ret)
                if cum_nav > peak_nav:
                    peak_nav = cum_nav
            else:
                port_ret_history.append(0.0)
            nav_history.append(cum_nav)

            # Asset-level stop loss and ATR trailing Chandelier stop tracking
            if stop_mode in ("atr_trailing", "fixed_pct"):
                for s in risky_symbols:
                    prev_w = float(daily_weights.loc[prev_date, s]) if t > 0 else 0.0
                    curr_p = float(universe[s]["Close"].reindex(master_index).ffill().iloc[t]) if s in universe and not universe[s].empty else 0.0
                    if prev_w > 1e-4 and curr_p > 0:
                        if asset_entry_price[s] is None or asset_entry_price[s] <= 0:
                            asset_entry_price[s] = curr_p
                            asset_peak_price[s] = curr_p
                        else:
                            if curr_p > (asset_peak_price[s] or curr_p):
                                asset_peak_price[s] = curr_p
                            is_stopped = False
                            if stop_mode == "atr_trailing":
                                cur_atr = float(atr_14_matrix[s].iloc[t]) if s in atr_14_matrix.columns else (curr_p * 0.02)
                                peak_p = asset_peak_price[s] or curr_p
                                if peak_p >= asset_entry_price[s] * 1.05:
                                    eff_stop_mult = stop_atr_mult
                                    if enable_adaptive_stop_atr:
                                        s_vol = float(asset_vol_21d.iloc[t][s]) if (s in asset_vol_21d.columns and pd.notna(asset_vol_21d.iloc[t][s])) else target_vol
                                        med_vol = float(asset_vol_21d.iloc[t].median()) if not asset_vol_21d.iloc[t].empty else target_vol
                                        if s_vol > 0.05 and med_vol > 0.05:
                                            eff_stop_mult = float(np.clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5))
                                    trailing_stop_p = max(asset_entry_price[s] * 0.92, peak_p - eff_stop_mult * cur_atr)
                                    # Track C Anchor 5: Structural ZG/VAL Chandelier Stop floor
                                    if enable_structural_chandelier_stop and s in universe and not universe[s].empty:
                                        df_u = universe[s]
                                        if date in df_u.index:
                                            cols_u = df_u.columns
                                            zg = df_u.loc[date, "ZG"] if "ZG" in cols_u else (df_u.loc[date, "zg"] if "zg" in cols_u else np.nan)
                                            val = df_u.loc[date, "VAL"] if "VAL" in cols_u else (df_u.loc[date, "val"] if "val" in cols_u else np.nan)
                                            if pd.notna(zg) and pd.notna(val):
                                                struct_floor = min(float(zg), float(val)) * 0.985
                                                trailing_stop_p = max(trailing_stop_p, struct_floor)
                                            elif pd.notna(zg):
                                                trailing_stop_p = max(trailing_stop_p, float(zg) * 0.985)
                                            elif pd.notna(val):
                                                trailing_stop_p = max(trailing_stop_p, float(val) * 0.985)
                                else:
                                    trailing_stop_p = asset_entry_price[s] * 0.92
                                if curr_p < trailing_stop_p:
                                    is_stopped = True
                                    # Anti-washout stop loss exemption (morning dip recovery filter)
                                    if enable_5m_stop_filter and curr_p >= trailing_stop_p * 0.97:
                                        if s in universe and not universe[s].empty:
                                            df_u = universe[s]
                                            if "Afternoon_Low_5m" in df_u.columns and date in df_u.index:
                                                aft_low = df_u.loc[date, "Afternoon_Low_5m"]
                                                vwap_prem = df_u.loc[date, "VWAP_Prem_5m"] if "VWAP_Prem_5m" in df_u.columns else np.nan
                                                upvol = df_u.loc[date, "UpVol_Ratio_5m"] if "UpVol_Ratio_5m" in df_u.columns else np.nan
                                                if pd.notna(aft_low) and aft_low >= trailing_stop_p and (pd.isna(vwap_prem) or vwap_prem >= 0.0) and (pd.isna(upvol) or upvol >= 0.60):
                                                    is_stopped = False

                                # Track C Anchor 5: Naked POC profit taking protection
                                if not is_stopped and enable_structural_chandelier_stop and asset_entry_price[s] and curr_p >= asset_entry_price[s] * 1.10:
                                    if s in universe and not universe[s].empty:
                                        df_u = universe[s]
                                        if date in df_u.index:
                                            cols_u = df_u.columns
                                            npoc_col = "NPOC" if "NPOC" in cols_u else ("npoc" if "npoc" in cols_u else None)
                                            if npoc_col and "standardized_divergence" in cols_u:
                                                npoc_val = df_u.loc[date, npoc_col]
                                                div_val = df_u.loc[date, "standardized_divergence"]
                                                if pd.notna(npoc_val) and pd.notna(div_val):
                                                    if curr_p >= float(npoc_val) and float(div_val) < -1.0:
                                                        is_stopped = True

                                # Sub-level 3S take-profit lock:
                                # When holding asset with floating profit >= 8% from entry, and pullback from peak >= 2 * ATR
                                if not is_stopped and asset_entry_price[s] and (curr_p >= asset_entry_price[s] * 1.08 or peak_p >= asset_entry_price[s] * 1.08):
                                    if curr_p <= peak_p - 2.0 * cur_atr:
                                        if (enable_intraday_5m or enable_5m_stop_filter) and s in universe and not universe[s].empty:
                                            df_u = universe[s]
                                            if "Chan_5m_Sell_Trigger" in df_u.columns and date in df_u.index:
                                                if df_u.loc[date, "Chan_5m_Sell_Trigger"] == 1:
                                                    is_stopped = True
                            elif stop_mode == "fixed_pct":
                                if curr_p < asset_entry_price[s] * 0.92:
                                    is_stopped = True

                            if is_stopped:
                                asset_emergency_df.loc[date, s] = True
                                asset_cooldown_until[s] = t + asset_stop_cooldown_bars
                                asset_entry_price[s] = None
                                asset_peak_price[s] = None
                                entry_signal_bars[s] = 0
                                expansion_signal_bars[s] = 0
                    elif prev_w <= 1e-4:
                        if t >= asset_cooldown_until[s]:
                            asset_entry_price[s] = None
                            asset_peak_price[s] = None

            # Fast-recovery override detection
            lookback_idx = max(0, len(nav_history) - 1 - 10)
            r_10d = (cum_nav / nav_history[lookback_idx] - 1.0) if len(nav_history) > 10 else 0.0
            thrust = float(daily_thrust.iloc[t])
            breadth_val = float(daily_breadth.iloc[t])

            # (2026-10-22 incident, Fix 4) Regime hysteresis on the Zweig thrust
            # gate: a single bar flipping 0.60 -> 0.50 used to slam the regime
            # state machine from the momentum budget into the defensive budget.
            # With hysteresis the thrust flag only ENTERS when thrust rises to
            # `thrust_enter_thresh` (0.62) and only EXITS when it falls below
            # `thrust_exit_thresh` (0.58), so noise around 0.60 no longer flips
            # the portfolio. Disabled by default (legacy single-threshold path).
            if enable_regime_hysteresis:
                prev_thrust_active = bool(prev_state.get("thrust_active", False))
                if prev_thrust_active:
                    thrust_active = thrust >= thrust_exit_thresh
                else:
                    thrust_active = thrust >= thrust_enter_thresh
            else:
                thrust_active = thrust >= thrust_thresh

            fast_recovery = thrust_active or (r_10d > 0.0) or (breadth_val >= 0.60)

            current_dd = (cum_nav - peak_nav) / peak_nav if peak_nav > 0 else 0.0
            dd_mag = abs(current_dd)

            # Elastic flash reset of drawdown brake on confirmed market thrust / breadth surge
            if elastic_dd_reset and fast_recovery and (dd_mag >= dd_reduce_thresh):
                peak_nav = cum_nav
                tier1_counter = 0
                stop_counter = 0
                dd_mag = 0.0

            # Auto-healing cooldown
            if dd_mag >= dd_stop_thresh:
                stop_counter += 1
                tier1_counter = 0
                if stop_counter >= stop_cooldown_bars:
                    peak_nav = cum_nav
                    stop_counter = 0
                    dd_mag = 0.0
            elif dd_mag >= dd_reduce_thresh:
                tier1_counter += 1
                stop_counter = 0
                if tier1_counter >= tier1_cooldown_bars:
                    peak_nav = cum_nav
                    tier1_counter = 0
                    dd_mag = 0.0
            else:
                stop_counter = 0
                tier1_counter = 0

            # Compute raw sub-strategy blended weights based on weighting_mode
            if weighting_mode == "static":
                cur_weights = self.compute_regime_weights(base_weights, breadth_val, thrust, date, p)
            else:
                sub_ret_slice = sub_returns_history.iloc[max(0, t - adaptive_lookback_days) : t] if t >= 10 else pd.DataFrame()
                bucket_weights_out: Dict[str, float] = {}
                cur_weights = self._compute_adaptive_weights(
                    sub_strats=sub_strats,
                    base_weights=base_weights,
                    sub_returns_slice=sub_ret_slice,
                    weighting_mode=weighting_mode,
                    breadth_val=breadth_val,
                    thrust=thrust,
                    vol_z=float(vol_zscore_series.iloc[t]),
                    persistence=float(persistence_series.iloc[t]),
                    adaptive_min_tstat=adaptive_min_tstat,
                    adaptive_shrinkage=adaptive_shrinkage,
                    date=date,
                    params=p,
                    median_d=(float(fractal_d_series.iloc[t]) if (fractal_d_series is not None and pd.notna(fractal_d_series.iloc[t])) else None),
                    # (2026-10-22 incident) Hysteresis flag threaded only when
                    # enabled (None preserves the legacy internal comparison),
                    # plus bucket delta-clamp state for Fix 3.
                    thrust_active=(thrust_active if enable_regime_hysteresis else None),
                    prev_bucket_weights=(prev_bucket_weights or None),
                    bucket_renorm_max_delta=bucket_renorm_max_delta,
                    bucket_weights_out=bucket_weights_out,
                )
                if bucket_weights_out:
                    prev_bucket_weights = dict(bucket_weights_out)

            # Optional Löwdin symmetric orthogonalization
            if enable_orthogonalization and len(sub_strats) > 1:
                mat = pd.DataFrame(
                    {sname: sub_daily[sname].loc[date, risky_symbols] for sname in sub_strats if sname in sub_daily},
                    index=risky_symbols,
                )
                mat_ortho = self._apply_lowdin_orthogonalization(mat)
                raw_blend = pd.Series(0.0, index=symbols)
                for sname, sw in cur_weights.items():
                    if sname in mat_ortho.columns:
                        raw_blend[risky_symbols] += sw * mat_ortho[sname]
                        if cash_proxy in symbols and cash_proxy in sub_daily[sname].columns:
                            raw_blend[cash_proxy] += sw * float(sub_daily[sname].loc[date, cash_proxy])
            else:
                raw_blend = pd.Series(0.0, index=symbols)
                for sname, sw in cur_weights.items():
                    if sname in sub_daily:
                        raw_blend += sw * sub_daily[sname].loc[date]

            # Circuit breaker logic
            deleverage_scale = 1.0
            if dd_mag >= dd_stop_thresh:
                if fast_recovery and defensive_key is not None:
                    raw_w = sub_daily[defensive_key].loc[date].copy()
                    is_emergency = not thrust_active
                    deleverage_scale = 1.0
                else:
                    raw_w = pd.Series(0.0, index=symbols)
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = 1.0
                    is_emergency = True
                    deleverage_scale = 0.0
            elif dd_mag >= dd_reduce_thresh:
                if fast_recovery:
                    raw_w = raw_blend.copy()
                    is_emergency = False
                    deleverage_scale = 1.0
                elif smooth_drawdown:
                    scale_dd = max(0.0, 1.0 - (dd_mag - dd_reduce_thresh) / max(0.01, dd_stop_thresh - dd_reduce_thresh))
                    raw_w = raw_blend.copy()
                    deleverage_scale = scale_dd
                    is_emergency = (scale_dd < 0.30)
                elif dd_mag >= dd_defensive_thresh and defensive_key is not None:
                    raw_w = sub_daily[defensive_key].loc[date].copy()
                    is_emergency = True
                    deleverage_scale = 1.0
                else:
                    raw_w = raw_blend.copy()
                    deleverage_scale = 0.50
                    is_emergency = True
            else:
                raw_w = raw_blend.copy()
                is_emergency = False
                deleverage_scale = 1.0

            emergency_flags.append(is_emergency)
            if is_emergency:
                asset_emergency_df.loc[date, :] = True

            # Quality & Relative Strength Gate
            if t > 0:
                prev_date = master_index[t - 1]
                if require_asset_trend:
                    row_roc60 = roc_60_matrix.iloc[t]
                    roc60_median = float(row_roc60.median()) if not row_roc60.empty else 0.0
                    row_slope = ma20_slope_matrix.iloc[t]
                    row_trend50 = trend_50_matrix.iloc[t]
                    for s in risky_symbols:
                        prev_w_s = float(desired_weights.loc[prev_date, s])
                        if prev_w_s <= 1e-4 and float(raw_w[s]) > 0.01:
                            if float(row_trend50[s]) < 0.5 and float(row_slope[s]) < 0.5 and float(row_roc60[s]) < roc60_median:
                                raw_w[s] = 0.0

                # Track C Anchor 2: CYQ Quality Gate & Consolidation Density Gate
                if enable_cyq_quality_gate:
                    for s in risky_symbols:
                        prev_w_s = float(desired_weights.loc[prev_date, s])
                        if prev_w_s <= 1e-4 and float(raw_w[s]) > 0.01 and s in universe and not universe[s].empty:
                            df_u = universe[s]
                            cols = df_u.columns
                            if date in df_u.index:
                                if "cyq_winner_ratio" in cols:
                                    c_wr = df_u.loc[date, "cyq_winner_ratio"]
                                    if pd.notna(c_wr) and float(c_wr) < min_cyq_winner_ratio:
                                        # Wyckoff spring exemption: if spring >= 1.25 and curvature >= 1.5, allow pass
                                        # PROTECTIVE NOTE (Task C5.1): the 1.5 curvature leg is
                                        # DELIBERATELY left at 1.5 even though it is structurally
                                        # unreachable -- z-scored window=5 fits obey
                                        # |kappa| <= 2*sqrt(5/14) ~= 1.1952 (see
                                        # common.structural_factors.CURVATURE_KAPPA_BOUND_WINDOW5).
                                        # This is a fail-CLOSED union exemption: keeping it
                                        # unreachable preserves current behaviour. Lowering it to
                                        # 0.8 would ADD a brand-new exemption pass path with zero
                                        # walkforward evidence -- do NOT "fix" it without that
                                        # evidence.
                                        is_exempt = False
                                        if "wyckoff_spring" in cols and "extremum_curvature" in cols:
                                            w_sp = df_u.loc[date, "wyckoff_spring"]
                                            e_curv = df_u.loc[date, "extremum_curvature"]
                                            if pd.notna(w_sp) and pd.notna(e_curv) and float(w_sp) >= 1.25 and float(e_curv) >= 1.5:
                                                is_exempt = True
                                        if not is_exempt:
                                            raw_w[s] = 0.0

                                if float(raw_w[s]) > 0.01 and "consolidation_density" in cols:
                                    c_dens = df_u.loc[date, "consolidation_density"]
                                    # Task C6.1: threshold comes from
                                    # `chan_vp_min_consolidation_density` (default 0.45),
                                    # never a float literal. A bare `0.40` here made the
                                    # configured value inert on this, the only blend-side
                                    # veto consumer, and silently disagreed with the
                                    # declared 0.45.
                                    if pd.notna(c_dens) and float(c_dens) < min_consolidation_density:
                                        raw_w[s] = 0.0

            # Dynamic cash deployment & pre-emptive thrust cash deployment
            effective_cap = max_single_pos
            if not is_emergency and dynamic_cash:
                bull_active = (breadth_val >= breadth_bull_thresh) or thrust_active
                if bull_active:
                    breadth_factor = 1.0 if thrust_active else np.clip((breadth_val - breadth_bull_thresh) / max(0.01, 0.75 - breadth_bull_thresh), 0.0, 1.0)
                    target_exp = min(target_bull_exposure, 0.60 + breadth_factor * (target_bull_exposure - 0.60))
                    effective_cap = min(bull_max_pos, max_single_pos * (1.0 + 0.50 * breadth_factor)) if bull_max_pos > max_single_pos else max_single_pos

                    active_risky = [s for s in risky_symbols if raw_w[s] > 1e-6]
                    if active_risky:
                        tot_active = float(raw_w[active_risky].sum())
                        if tot_active > 0 and target_exp > tot_active:
                            scale = target_exp / tot_active
                            for s in active_risky:
                                raw_w[s] = min(effective_cap, raw_w[s] * scale)

                    # Pre-Emptive Breadth Thrust Deployment
                    if thrust_active and preemptive_thrust:
                        tot_active = float(raw_w[risky_symbols].sum())
                        if tot_active < target_exp:
                            unallocated = target_exp - tot_active
                            row_roc = roc_matrix.iloc[t]
                            row_roc60 = roc_60_matrix.iloc[t]
                            roc60_median = float(row_roc60.median()) if not row_roc60.empty else 0.0
                            row_trend50 = trend_50_matrix.iloc[t]
                            row_slope = ma20_slope_matrix.iloc[t]

                            cand_scores = {}
                            cand_vols = {}
                            confluence_mults = {}

                            for s in risky_symbols:
                                if t < asset_cooldown_until[s]:
                                    continue
                                r_val = float(row_roc[s]) if pd.notna(row_roc[s]) else 0.0
                                if r_val >= thrust_min_roc:
                                    if require_asset_trend and (float(row_trend50[s]) < 0.5 or float(row_slope[s]) < 0.5):
                                        continue
                                    if require_rs_rank and float(row_roc60[s]) < roc60_median:
                                        continue

                                    c_mult = 1.0
                                    # Task C6.2: this is the LEGACY `c_mult` confluence/
                                    # resonance sizing gate (`confluence_sizing`, i.e. the
                                    # `cdhb_confluence_sizing` key). It is NOT Track C
                                    # Anchor 3 -- see `confluence_sizing_gates()`.
                                    if legacy_confluence_sizing:
                                        conf_count = 1
                                        for sname, s_df in sub_daily.items():
                                            if s in s_df.columns and float(s_df.loc[date, s]) > 0.01:
                                                conf_count += 1
                                        if float(vol_surge_matrix.iloc[t][s]) > 0.5:
                                            conf_count += 1
                                        damping_5m = 1.0
                                        if enable_intraday_5m and s in universe and not universe[s].empty:
                                            df_u = universe[s]
                                            if date in df_u.index:
                                                q_val = float(df_u.loc[date, "SmartMoney_Q_5m"]) if "SmartMoney_Q_5m" in df_u.columns and pd.notna(df_u.loc[date, "SmartMoney_Q_5m"]) else 0.0
                                                up_val = float(df_u.loc[date, "UpVol_Ratio_5m"]) if "UpVol_Ratio_5m" in df_u.columns and pd.notna(df_u.loc[date, "UpVol_Ratio_5m"]) else 0.5
                                                if q_val < -0.002 and up_val >= 0.55:
                                                    conf_count += 1
                                                elif up_val < 0.42:
                                                    damping_5m = 0.5

                                        # Track C Anchor 3: VP Confluence & Wyckoff Effort-Result Sizing
                                        #
                                        # ATTRIBUTION (Task C6.2): Anchor 3's own switch is
                                        # `enable_vp_confluence_sizing` (blend key
                                        # `blend_enable_vp_confluence_sizing`, default False,
                                        # absent from every blend block in
                                        # strategies_config.json => OFF in all 6 blends).
                                        # The two keys above are NOT synonyms and neither
                                        # implies the other.
                                        #
                                        # STRUCTURAL PRECONDITION (documented, not a bug):
                                        # Anchor 3 only *modifies* the legacy `c_mult` score
                                        # computed in the enclosing `if
                                        # legacy_confluence_sizing:` block (it bumps
                                        # `conf_count` / applies `damping_vp`), so its code is
                                        # nested inside that block by construction. Turning on
                                        # `enable_vp_confluence_sizing` alone therefore has NO
                                        # effect -- the legacy block must also be on. Do not
                                        # infer Anchor 3 activation from the string
                                        # "confluence" anywhere; use
                                        # `confluence_sizing_gates()`.
                                        damping_vp = 1.0
                                        if enable_vp_confluence_sizing and s in universe and not universe[s].empty:
                                            df_u = universe[s]
                                            if date in df_u.index:
                                                cols_u = df_u.columns
                                                curr_close_s = float(df_u.loc[date, "Close"]) if "Close" in cols_u else 0.0
                                                if "VAH" in cols_u and "cyq_cost_concentration" in cols_u:
                                                    vah_val = df_u.loc[date, "VAH"]
                                                    conc_val = df_u.loc[date, "cyq_cost_concentration"]
                                                    if pd.notna(vah_val) and pd.notna(conc_val) and curr_close_s >= float(vah_val) and float(conc_val) >= 0.85:
                                                        conf_count += 1
                                                if "wyckoff_effort_result" in cols_u and "High" in cols_u and "Low" in cols_u:
                                                    er_val = df_u.loc[date, "wyckoff_effort_result"]
                                                    h_val = df_u.loc[date, "High"]
                                                    l_val = df_u.loc[date, "Low"]
                                                    if pd.notna(er_val) and pd.notna(h_val) and pd.notna(l_val):
                                                        mid_bar = (float(h_val) + float(l_val)) / 2.0
                                                        if float(er_val) < 0.35 and curr_close_s < mid_bar:
                                                            damping_vp = 0.6

                                        if conf_count >= 3:
                                            c_mult = 1.6
                                        elif conf_count >= 2:
                                            c_mult = 1.1
                                        else:
                                            c_mult = 0.6
                                        c_mult *= damping_5m * damping_vp
                                    confluence_mults[s] = c_mult

                                    # Alpha#3 / sub-strategy boost if available
                                    a2_bonus = 0.0
                                    if "wq_alpha2" in sub_daily and s in sub_daily["wq_alpha2"].columns:
                                        a2_bonus = max(0.0, float(sub_daily["wq_alpha2"].loc[date, s]))

                                    base_score = r_val * (1.0 + 5.0 * a2_bonus) * c_mult
                                    if enable_intraday_5m and s in universe and not universe[s].empty:
                                        df_u = universe[s]
                                        if "SmartMoney_Q_5m" in df_u.columns and date in df_u.index:
                                            q_sm = float(df_u.loc[date, "SmartMoney_Q_5m"]) if pd.notna(df_u.loc[date, "SmartMoney_Q_5m"]) else 0.0
                                            base_score *= (1.0 + 3.0 * max(0.0, -q_sm))
                                    if t > 0:
                                        prev_date = master_index[t - 1]
                                        if float(desired_weights.loc[prev_date, s]) > 1e-4:
                                            base_score *= thrust_incumbent_bonus
                                    cand_scores[s] = base_score
                                    v = float(asset_vol_21d.iloc[t][s]) if pd.notna(asset_vol_21d.iloc[t][s]) else target_vol
                                    cand_vols[s] = max(0.10, v)

                            # Sector concentration throttle
                            sec_rocs = defaultdict(list)
                            for s in risky_symbols:
                                r_val = float(row_roc[s]) if pd.notna(row_roc[s]) else 0.0
                                sec = _get_symbol_sector(s)
                                sec_rocs[sec].append(r_val)
                            sec_mean_roc = {sec: float(np.mean(vals)) for sec, vals in sec_rocs.items() if vals}
                            top_sec_thresh = float(pd.Series(sec_mean_roc).quantile(0.80)) if sec_mean_roc else 0.03

                            # Track held and locked incumbents from the prior day
                            held_incumbents = set()
                            locked_incumbents = set()
                            if t > 0:
                                prev_date = master_index[t - 1]
                                for s in risky_symbols:
                                    if float(desired_weights.loc[prev_date, s]) > 1e-4:
                                        has_stopped = bool(asset_emergency_df.loc[date, s]) or (t < asset_cooldown_until[s])
                                        if not has_stopped and not is_emergency:
                                            held_incumbents.add(s)
                                        if min_holding_bars > 0:
                                            k = t - 1
                                            age = 0
                                            while k >= 0 and float(desired_weights.iloc[k][s]) > 1e-4:
                                                age += 1
                                                k -= 1
                                            eff_min_holding = min_holding_bars
                                            if enable_low_beta_inertia:
                                                s_vol = float(asset_vol_21d.iloc[t][s]) if (s in asset_vol_21d.columns and pd.notna(asset_vol_21d.iloc[t][s])) else target_vol
                                                if s_vol < 0.22:
                                                    eff_min_holding = max(eff_min_holding, 10)
                                            if age < eff_min_holding and not has_stopped and not is_emergency:
                                                locked_incumbents.add(s)

                            all_sorted = sorted(cand_scores.keys(), key=lambda s: (-cand_scores[s], s))
                            sorted_cands = []
                            sector_counts = defaultdict(int)
                            for s in risky_symbols:
                                if float(raw_w[s]) > 0.01:
                                    sec = _get_symbol_sector(s)
                                    sector_counts[sec] += 1

                            for cand in all_sorted:
                                if cand in sorted_cands:
                                    continue
                                sec = _get_symbol_sector(cand)
                                allowed_sec_cap = max_assets_per_sector
                                if adaptive_sector_throttle and sec_mean_roc.get(sec, 0.0) >= max(0.02, top_sec_thresh):
                                    allowed_sec_cap = max(allowed_sec_cap, 2)

                                # Sector incumbent protection: outside challenger must beat held incumbent's ROC by at least sector_incumbent_hurdle
                                sec_incumbents = [
                                    s for s in held_incumbents
                                    if _get_symbol_sector(s) == sec and s not in sorted_cands and float(raw_w[s]) <= 0.01
                                ]
                                if cand not in held_incumbents and sec_incumbents:
                                    best_inc = max(sec_incumbents, key=lambda s: float(row_roc[s]) if pd.notna(row_roc[s]) else 0.0)
                                    inc_roc = float(row_roc[best_inc]) if pd.notna(row_roc[best_inc]) else 0.0
                                    cand_roc = float(row_roc[cand]) if pd.notna(row_roc[cand]) else 0.0
                                    if cand_roc < inc_roc + sector_incumbent_hurdle:
                                        # Challenger fails hurdle; retain existing incumbent to fill sector quota
                                        effective_occ = sector_counts[sec] + sum(
                                            1 for s in locked_incumbents if s != best_inc and _get_symbol_sector(s) == sec
                                        )
                                        if effective_occ < allowed_sec_cap:
                                            if best_inc not in cand_scores:
                                                cand_scores[best_inc] = max(0.001, inc_roc) * thrust_incumbent_bonus
                                            if best_inc not in cand_vols:
                                                v = float(asset_vol_21d.iloc[t][best_inc]) if pd.notna(asset_vol_21d.iloc[t][best_inc]) else target_vol
                                                cand_vols[best_inc] = max(0.10, v)
                                            sorted_cands.append(best_inc)
                                            sector_counts[sec] += 1
                                        if len(sorted_cands) >= min_thrust_assets:
                                            break
                                        continue

                                # An outside challenger cannot displace a locked incumbent in the same sector
                                effective_occ = sector_counts[sec] + sum(1 for s in locked_incumbents if s != cand and _get_symbol_sector(s) == sec)
                                if effective_occ < allowed_sec_cap:
                                    sorted_cands.append(cand)
                                    sector_counts[sec] += 1
                                if len(sorted_cands) >= min_thrust_assets:
                                    break

                            n_cands = len(sorted_cands)
                            if n_cands > 0:
                                if thrust_sizing_mode == "vol_adjusted":
                                    inv_vols = {c: (cand_scores[c] / cand_vols[c]) for c in sorted_cands}
                                    tot_inv = sum(inv_vols.values()) or 1.0
                                    target_shares = {c: inv_vols[c] / tot_inv for c in sorted_cands}
                                else:
                                    target_shares = {c: 1.0 / n_cands for c in sorted_cands}

                                thrust_slot_cap = min(effective_cap, max(0.04, unallocated / n_cands))

                                # Pass 1: Proportional risk-balanced allocation
                                for cand in sorted_cands:
                                    if unallocated <= 1e-6:
                                        break
                                    current_w = float(raw_w[cand])
                                    space = max(0.0, effective_cap - current_w)
                                    if space > 0.01:
                                        alloc_desired = unallocated * target_shares[cand]
                                        cand_cap = min(effective_cap, thrust_slot_cap * confluence_mults.get(cand, 1.0))
                                        alloc = min(space, min(cand_cap, alloc_desired))
                                        raw_w[cand] = current_w + alloc
                                        unallocated -= alloc

                                # Pass 2: Fill remaining space
                                if unallocated > 1e-6:
                                    for cand in sorted_cands:
                                        if unallocated <= 1e-6:
                                            break
                                        current_w = float(raw_w[cand])
                                        space = max(0.0, effective_cap - current_w)
                                        if space > 0.01:
                                            alloc = min(space, unallocated)
                                            raw_w[cand] = current_w + alloc
                                            unallocated -= alloc

                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = max(0.0, 1.0 - float(raw_w[risky_symbols].sum()))

            # Volatility targeting overlay
            if enable_vol_targeting and not is_emergency:
                if breadth_val >= 0.60 or thrust_active:
                    curr_target_vol = max(target_vol, target_bull_vol)
                elif breadth_val < 0.30 and thrust < 0.40:
                    curr_target_vol = min(target_vol, target_bear_vol)
                else:
                    curr_target_vol = target_vol

                if len(port_ret_history) >= 21:
                    port_realized_vol = float(np.std(port_ret_history[-21:], ddof=1) * np.sqrt(252))
                    cur_meas_vol = max(0.05, port_realized_vol)
                else:
                    cur_meas_vol = float(basket_vol_21d.iloc[t])

                if cur_meas_vol > curr_target_vol:
                    vol_scalar = curr_target_vol / cur_meas_vol
                    raw_w[risky_symbols] *= vol_scalar
                    if cash_proxy in symbols:
                        raw_w[cash_proxy] = max(0.0, 1.0 - float(raw_w[risky_symbols].sum()))

            # Enforce cooldown and single-position caps
            for s in risky_symbols:
                if t < asset_cooldown_until[s]:
                    raw_w[s] = 0.0
                elif raw_w[s] > effective_cap:
                    raw_w[s] = effective_cap

            # Pre-emptive Entry Confirmation Filter ("avoid 1-day trading by not entering")
            if min_entry_bars > 1:
                prev_date_w = master_index[t - 1] if t > 0 else None
                for s in risky_symbols:
                    prev_w = float(desired_weights.loc[prev_date_w, s]) if prev_date_w is not None else 0.0
                    target_w = float(raw_w[s])

                    # 1. New position entry filter (prev_w <= 1e-4):
                    # Requires signal to persist for at least min_entry_bars consecutive trading days
                    # before opening the position. Single-day transient spikes are suppressed to 0.0.
                    # UPLSM Parity: Strict confluence of Volume breakout (>= 1.5x MA20) AND market breadth >= 60%
                    # allows Day 1 fast-path bypass.
                    if prev_w <= 1e-4:
                        if target_w >= entry_thresh:
                            entry_signal_bars[s] += 1
                            is_vol = bool(vol_breakout_matrix.iloc[t][s] > 0.5) if s in vol_breakout_matrix.columns else False
                            is_breadth = (breadth_val >= 0.60)
                            if entry_signal_bars[s] < min_entry_bars:
                                if is_vol and is_breadth:
                                    entry_signal_bars[s] = min_entry_bars  # Fast-path bypass Day 1 confirmation (strict confluence)
                                elif (enable_intraday_5m or enable_5m_interval_nesting) and s in universe and not universe[s].empty:
                                    df_u = universe[s]
                                    if "Chan_5m_Buy_Confluence" in df_u.columns and date in df_u.index:
                                        c_conf = df_u.loc[date, "Chan_5m_Buy_Confluence"]
                                        upvol = df_u.loc[date, "UpVol_Ratio_5m"] if "UpVol_Ratio_5m" in df_u.columns else np.nan
                                        vwap_prem = df_u.loc[date, "VWAP_Prem_5m"] if "VWAP_Prem_5m" in df_u.columns else np.nan
                                        if c_conf == 2 and (pd.isna(upvol) or upvol >= intraday_upvol_thresh) and (pd.isna(vwap_prem) or vwap_prem >= 0.0):
                                            entry_signal_bars[s] = min_entry_bars  # Fast-track entry Day 1 bypass
                                elif enable_spring_fast_entry and entry_signal_bars[s] == 1 and s in universe and not universe[s].empty:
                                    df_u = universe[s]
                                    if date in df_u.index and "wyckoff_spring" in df_u.columns and "extremum_curvature" in df_u.columns:
                                        w_sp = df_u.loc[date, "wyckoff_spring"]
                                        e_curv = df_u.loc[date, "extremum_curvature"]
                                        # PROTECTIVE NOTE (Task C5.1): same unreachable window=5
                                        # curvature bound as the cyq exemption above
                                        # (|kappa| <= 2*sqrt(5/14) ~= 1.1952). The 1.5 leg is
                                        # left at 1.5 on purpose: this is a fail-closed entry
                                        # fast-track, so an unreachable leg merely preserves the
                                        # existing gating. Make it reachable (e.g. 0.8) only with
                                        # walkforward evidence that the extra entries are worth it.
                                        if pd.notna(w_sp) and pd.notna(e_curv) and float(w_sp) >= 1.25 and float(e_curv) >= 1.5:
                                            entry_signal_bars[s] = min_entry_bars  # Track C Anchor 4: Wyckoff Spring Day 1 fast-track entry bypass
                                if entry_signal_bars[s] < min_entry_bars:
                                    raw_w[s] = 0.0  # Pending confirmation on Day 1; do NOT enter!

                            # Track C Anchor 4: Extreme kinetic top divergence veto
                            if enable_spring_fast_entry and float(raw_w[s]) > 0.0 and s in universe and not universe[s].empty:
                                df_u = universe[s]
                                if date in df_u.index and "standardized_divergence" in df_u.columns and "POC" in df_u.columns:
                                    s_div = df_u.loc[date, "standardized_divergence"]
                                    poc_val = df_u.loc[date, "POC"]
                                    curr_p_s = float(df_u.loc[date, "Close"]) if "Close" in df_u.columns else 0.0
                                    if pd.notna(s_div) and pd.notna(poc_val) and float(s_div) < -1.2 and curr_p_s < float(poc_val):
                                        raw_w[s] = 0.0
                                        entry_signal_bars[s] = 0
                        else:
                            entry_signal_bars[s] = 0
                            raw_w[s] = 0.0

                    # 2. Incremental expansion filter on existing positions:
                    # If target demands significant expansion (>= min_weight_change), require persistence
                    # so 1-day thrust spikes don't pump and dump existing holdings.
                    # UPLSM Parity: Volume breakout (>= 1.5x MA20) allows immediate expansion bypass.
                    else:
                        entry_signal_bars[s] = min_entry_bars  # Incumbent position has confirmed entry
                        if target_w - prev_w >= min_weight_change:
                            expansion_signal_bars[s] += 1
                            is_vol = bool(vol_breakout_matrix.iloc[t][s] > 0.5) if s in vol_breakout_matrix.columns else False
                            if expansion_signal_bars[s] < min_entry_bars:
                                if is_vol:
                                    expansion_signal_bars[s] = min_entry_bars  # Fast-path bypass for expansions
                                else:
                                    raw_w[s] = prev_w  # Suppress expansion until confirmed for min_entry_bars
                        else:
                            expansion_signal_bars[s] = 0

            # Sub-level veto for new entries (Task T3.1):
            if (enable_intraday_5m or enable_5m_interval_nesting):
                prev_date_w = master_index[t - 1] if t > 0 else None
                for s in risky_symbols:
                    prev_w = float(desired_weights.loc[prev_date_w, s]) if prev_date_w is not None else 0.0
                    target_w = float(raw_w[s])
                    if prev_w <= 1e-4 and target_w >= entry_thresh:
                        if s in universe and not universe[s].empty:
                            df_u = universe[s]
                            if "Chan_5m_Sell_Trigger" in df_u.columns and date in df_u.index:
                                if df_u.loc[date, "Chan_5m_Sell_Trigger"] == 1:
                                    aft_ret = df_u.loc[date, "Afternoon_Ret_5m"] if "Afternoon_Ret_5m" in df_u.columns else 0.0
                                    if pd.notna(aft_ret) and aft_ret < -0.015:
                                        raw_w[s] = 0.0  # Sub-level veto on entry

            # Track C Anchor 4: Extreme kinetic top divergence veto for new entries when min_entry_bars <= 1
            if min_entry_bars <= 1 and enable_spring_fast_entry:
                prev_date_w = master_index[t - 1] if t > 0 else None
                for s in risky_symbols:
                    prev_w = float(desired_weights.loc[prev_date_w, s]) if prev_date_w is not None else 0.0
                    if prev_w <= 1e-4 and float(raw_w[s]) >= entry_thresh and s in universe and not universe[s].empty:
                        df_u = universe[s]
                        if date in df_u.index and "standardized_divergence" in df_u.columns and "POC" in df_u.columns:
                            s_div = df_u.loc[date, "standardized_divergence"]
                            poc_val = df_u.loc[date, "POC"]
                            curr_p_s = float(df_u.loc[date, "Close"]) if "Close" in df_u.columns else 0.0
                            if pd.notna(s_div) and pd.notna(poc_val) and float(s_div) < -1.2 and curr_p_s < float(poc_val):
                                raw_w[s] = 0.0

            tot_risky = float(raw_w[risky_symbols].sum())
            if tot_risky > 1.0:
                raw_w[risky_symbols] /= tot_risky
                if cash_proxy in symbols:
                    raw_w[cash_proxy] = 0.0
            elif cash_proxy in symbols:
                raw_w[cash_proxy] = max(0.0, 1.0 - tot_risky)

            # Inline causal asset-level inertia filtering so daily_weights[date]
            # reflects actual post-inertia positions for subsequent NAV/stop/tenure tracking
            if use_inertia:
                if t == 0:
                    current_w = raw_w.copy()
                    for s in risky_symbols:
                        last_buy_bar[s] = 0 if current_w[s] > 1e-4 else -999
                else:
                    prev_w_series = desired_weights.iloc[t - 1]
                    thresh_t = inertia_thresh_df.iloc[t] if inertia_thresh_df is not None else static_inertia_thresh
                    current_w = _step_asset_inertia(
                        t=t,
                        ideal_w=raw_w,
                        current_w=prev_w_series,
                        risky_symbols=risky_symbols,
                        symbols=symbols,
                        thresh_t=thresh_t,
                        holding_age=holding_age,
                        last_buy_bar=last_buy_bar,
                        cash_proxy=cash_proxy,
                        em_row=asset_emergency_df.iloc[t],
                        asymmetric_sells=asymmetric_sells,
                        entry_threshold=entry_thresh,
                        min_holding_bars=min_holding_bars,
                    )
                desired_weights.loc[date] = current_w
            else:
                desired_weights.loc[date] = raw_w

            daily_weights.loc[date] = _apply_portfolio_deleverage(
                desired_weights.loc[date], risky_symbols, cash_proxy, deleverage_scale
            )

            # Cooldown Parity: Track normal strategic exits (when target drops to 0.0)
            # and enforce normal_cooldown_bars to prevent immediate 1-bar whipsaw re-entry.
            if t > 0:
                prev_date_w = master_index[t - 1]
                for s in risky_symbols:
                    prev_w_val = float(desired_weights.loc[prev_date_w, s])
                    curr_w_val = float(desired_weights.loc[date, s])
                    if prev_w_val > 1e-4 and curr_w_val <= 1e-4:
                        if not bool(asset_emergency_df.loc[date, s]):
                            if normal_cooldown_bars > 0:
                                asset_cooldown_until[s] = max(
                                    asset_cooldown_until[s],
                                    t + 1 + normal_cooldown_bars,
                                )
                        entry_signal_bars[s] = 0
                        expansion_signal_bars[s] = 0
                        asset_entry_price[s] = None
                        asset_peak_price[s] = None

            # (2026-10-22 incident, Fix 4) Persist this bar's hysteresis-adjusted
            # thrust flag for the next iteration's enter/exit boundary decision.
            prev_state["thrust_active"] = thrust_active

        daily_weights = _fill_out_columns(daily_weights, symbols)
        return _sparse_from_daily(daily_weights)

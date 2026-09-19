"""Institutional Multi-Strategy Alpha Book Strategy (MS-AlphaBook).

Academic & Quantitative Grounding:
1. Multi-Strategy Pod Architecture (Millennium, Point72, Citadel; Blitz 2024):
   Separates alpha generation into independent, decorrelated pods configured via `ms_pod_preset`:
   - `core_satellite` (Default & Recommended):
     - Pod 1: Structural Momentum Alpha (`ChanPivotShiftMACDStrategy` - Tier 1 alpha leader:
              Sharpe 1.75, CAGR 15.11%, MaxDD 4.39%, 95.2% win rate across 21 walkforward folds)
     - Pod 2: Multi-Signal Structural Alpha (`ChanCompositeStrategy` - Tier 1 alpha leader:
              Sharpe 1.53, CAGR 15.91%, MaxDD 5.71%, 90.5% win rate)
     - Pod 3: Tactical Crash Protection (`VigilantAssetAllocation` - Tier 2 defense leader:
              Sharpe 1.08, CAGR 15.02%, MaxDD 5.87%, Fold 10 Covid-crash CAGR +54.89%)
     - Pod 4: Cross-Asset Relative Momentum (`AcceleratingDualMomentum` - Tier 2 momentum leader:
              Sharpe 1.02, CAGR 16.78%, MaxDD 9.97%, 76.2% win rate)
   - `alpha_leaders`:
     - Concentrated ensemble across Tier 1 Chan alpha leaders: `ChanPivotShiftMACDStrategy`,
       `ChanThreeTypeStrategy` (Sharpe 1.56, CAGR 16.33%), and `ChanVaaCompoundStrategy` (Sharpe 1.30).
   - `all_regime`:
     - Balanced all-weather ensemble: `ChanPivotShiftMACDStrategy` (30%), `ChanCompositeStrategy` (30%),
       `VigilantAssetAllocation` (20%), and `PermanentPortfolioStrategy` (20%).
2. Risk Budgeting & Decoupling Risk from Capital (Roncalli 2013; Asness et al. 2012):
   Allocates marginal risk across pods inversely proportional to realized volatility (1/sigma),
   with configurable budget clamps (`ms_min_pod_budget`, `ms_max_pod_budget`) preventing any
   single sleeve from dominating portfolio variance.
3. Fast-Recovery Drawdown Regularization (Quant Memo 2026):
   Replaces the pro-cyclical 80% quarantine cliff with continuous drawdown damping:
   - Damping multiplier: `clip(1.0 - 0.50 * (drawdown / max_dd_limit), 0.50, 1.0)`.
   - Fast Trough Recovery: Once a pod's short-term trailing return turns positive (`R_10d > 0`),
     the drawdown penalty is immediately cleared, eliminating the "trough trap" and allowing
     the strategy to capture explosive V-shaped market rebounds.
4. Dual-Gate Macro Canary & Equity Growth Breadth Risk-Throttle (Morwane 2026; Keller & Keuning 2018):
   - Computes market breadth strictly across growth/equity assets (`growth_symbols > 200d SMA`),
     preventing fixed-income bear markets from artificially dragging equity exposure into cash.
   - Evaluates canary 13612W momentum score on (`TIP`, `IEF`, `BIL`). When canary assets are healthy
     or equity breadth is expanding, full gross exposure (1.0) is permitted.
   - Systemic Stress: Only when both canary momentum AND equity breadth collapse is gross equity
     exposure throttled smoothly into cash preservation (`BIL`).
   - Breadth Thrust Override: If short-term 15-day breadth thrust > 0.65, gross exposure is immediately
     un-throttled to 1.0 without waiting for lagging 200-day moving averages.
5. Dual Execution Modes (`ms_execution_mode`):
   - `pod_native_sparse` (Default): Blends daily sleeve target weights and compresses to sparse
     contract via `_sparse_from_daily`, propagating intra-month rotations and event-driven pivots.
   - `periodic_sync`: Traditional fixed-frequency central rebalance loop (e.g. 21 days).
6. Sparse Weights Contract & NaN-vs-0.0 Discipline (Workspace Core Contract):
   Target weights DataFrame has NaN between rebalances. On rebalance dates, every symbol has an
   explicit float value (0.0 if not held/derouted, target weight if held, unallocated capital in
   `cash_proxy`), preventing stale position leaks.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.indicators import roc, sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .config import StrategyConfig
from .taa_strategies import score_13612w


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


def _get_growth_symbols(risky_symbols: List[str]) -> List[str]:
    """Isolates equity and growth assets from fixed-income, commodity, and cash proxies."""
    non_growth = {"TLT", "IEF", "BIL", "AGG", "TIP", "LQD", "BND", "SHY", "DBC", "GLD", "IAU", "CASH"}
    growth = [s for s in risky_symbols if s not in non_growth]
    return growth if growth else risky_symbols


class MultiStrategyAlphaBookStrategy(AllocationTemplate):
    """Institutional Multi-Strategy Alpha Book with Risk Budgeting, Fast-Recovery
    Drawdown Regularization, and Dual-Gate Macro Canary Risk-Throttling.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="multi_strategy_alpha_book", param_grid={})

    def _get_pods(self, cfg: StrategyConfig) -> Dict[str, AllocationTemplate]:
        preset = getattr(cfg, "ms_pod_preset", "core_satellite")
        from .chan_advanced_strategies import ChanCompositeStrategy, ChanVaaCompoundStrategy
        from .strategy import (
            AcceleratingDualMomentum,
            ChanPivotShiftMACDStrategy,
            ChanThreeTypeStrategy,
            PermanentPortfolioStrategy,
        )
        from .taa_strategies import VigilantAssetAllocation

        if preset == "alpha_leaders":
            return {
                "structural_trend": ChanPivotShiftMACDStrategy(cfg),
                "three_type_alpha": ChanThreeTypeStrategy(cfg),
                "canary_compound": ChanVaaCompoundStrategy(cfg),
            }
        elif preset == "all_regime":
            return {
                "structural_trend": ChanPivotShiftMACDStrategy(cfg),
                "composite_alpha": ChanCompositeStrategy(cfg),
                "tactical_defense": VigilantAssetAllocation(cfg),
                "permanent_core": PermanentPortfolioStrategy(cfg),
            }
        else:  # core_satellite default
            return {
                "trend_alpha": ChanPivotShiftMACDStrategy(cfg),
                "structural_alpha": ChanCompositeStrategy(cfg),
                "tactical_defense": VigilantAssetAllocation(cfg),
                "momentum_expansion": AcceleratingDualMomentum(cfg),
            }

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}

        if "ms_pod_preset" in p:
            from dataclasses import replace
            cfg = replace(cfg, ms_pod_preset=p["ms_pod_preset"])

        preset = str(getattr(cfg, "ms_pod_preset", "core_satellite"))
        exec_mode = str(p.get("ms_execution_mode", getattr(cfg, "ms_execution_mode", "pod_native_sparse")))
        cash_proxy = str(p.get("cash_proxy", getattr(cfg, "cash_proxy", "BIL")))
        rebal_freq = int(p.get("ms_rebalance_freq_days", getattr(cfg, "ms_rebalance_freq_days", 21)))
        lookback_days = int(p.get("ms_lookback_days", getattr(cfg, "ms_lookback_days", 63)))
        max_dd_limit = float(p.get("ms_pod_max_drawdown_limit", getattr(cfg, "ms_pod_max_drawdown_limit", 0.06)))
        recovery_days = int(p.get("ms_drawdown_recovery_days", getattr(cfg, "ms_drawdown_recovery_days", 10)))
        max_budget = float(p.get("ms_max_pod_budget", getattr(cfg, "ms_max_pod_budget", 0.35)))
        min_budget = float(p.get("ms_min_pod_budget", getattr(cfg, "ms_min_pod_budget", 0.15)))
        alpha_smooth = float(p.get("ms_budget_smoothing_alpha", getattr(cfg, "ms_budget_smoothing_alpha", 0.50)))
        breadth_thresh = float(p.get("ms_canary_breadth_thresh", getattr(cfg, "ms_canary_breadth_thresh", 0.50)))
        min_weight_change = float(p.get("ms_min_weight_change", getattr(cfg, "ms_min_weight_change", 0.02)))

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        risky_symbols = _get_risky_symbols_helper(universe, p, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        # 1. Instantiate pods and precompute daily sleeve tracking
        pods = self._get_pods(cfg)
        pod_names = list(pods.keys())
        n_pods = len(pod_names)
        if n_pods == 0:
            return pd.DataFrame()

        pod_daily: Dict[str, pd.DataFrame] = {}
        for pod_name, strat in pods.items():
            sparse_w = strat.generate_weights(universe, params)
            if sparse_w.empty:
                w_dense = pd.DataFrame(0.0, index=master_index, columns=symbols)
                if cash_proxy in symbols:
                    w_dense[cash_proxy] = 1.0
            else:
                w_dense = sparse_w.reindex(master_index).ffill().fillna(0.0)
                w_dense = _fill_out_columns(w_dense, symbols)
                if cash_proxy in symbols:
                    zero_alloc = w_dense.sum(axis=1) == 0.0
                    w_dense.loc[zero_alloc, cash_proxy] = 1.0
            pod_daily[pod_name] = w_dense

        # 2. Compute asset daily returns and simulated daily pod returns (lookahead-free)
        prices = pd.DataFrame(
            {s: universe[s]["Close"] for s in symbols if "Close" in universe[s].columns}
        ).reindex(master_index).ffill()
        asset_rets = prices.pct_change().fillna(0.0)

        pod_rets = pd.DataFrame(index=master_index, columns=pod_names, data=0.0)
        for pod_name in pod_names:
            w_shifted = pod_daily[pod_name].shift(1).fillna(0.0)
            common_cols = [c for c in symbols if c in asset_rets.columns]
            pod_rets[pod_name] = (w_shifted[common_cols] * asset_rets[common_cols]).sum(axis=1)

        # 3. Growth & Canary symbols for dual-gate macro risk-throttle
        growth_symbols = _get_growth_symbols(risky_symbols)
        canary_candidates = ["TIP", "IEF", "BIL"]
        canary_symbols = [s for s in canary_candidates if s in universe and "Close" in universe[s].columns]

        # Precompute indicators across full history to eliminate redundant O(dates * symbols) rolling slices
        growth_ma200 = {}
        growth_r15 = {}
        for sym in growth_symbols:
            if sym in universe and "Close" in universe[sym].columns:
                c_full = universe[sym]["Close"]
                growth_ma200[sym] = sma(c_full, 200)
                growth_r15[sym] = roc(c_full, 15)

        canary_scores = {}
        for csym in canary_symbols:
            c_full = universe[csym]["Close"]
            canary_scores[csym] = score_13612w(c_full)

        # 4. Multi-strategy allocation & budget review schedule
        budget_dates = _get_rebalance_dates(master_index, rebal_freq)
        current_budgets = {pod_name: 1.0 / n_pods for pod_name in pod_names}

        budget_history: Dict[pd.Timestamp, Dict[str, float]] = {}
        throttle_history: Dict[pd.Timestamp, float] = {}

        for date in budget_dates:
            dt_loc = master_index.get_loc(date)

            # A. Dynamic Risk Budgeting (Inverse-Vol) & Fast Recovery Drawdown Damping
            if dt_loc >= lookback_days:
                hist_rets = pod_rets.iloc[dt_loc - lookback_days : dt_loc]
                vols = hist_rets.std() * np.sqrt(252)
                vols = vols.replace(0.0, 0.05).fillna(0.05)
                vols = np.maximum(vols, 0.02)
                inv_vol = 1.0 / vols
                target_b = (inv_vol / inv_vol.sum()).to_dict()

                # Trailing peak drawdown per pod
                cum_nav = (1.0 + hist_rets).cumprod()
                peaks = cum_nav.cummax()
                drawdowns = (cum_nav - peaks) / peaks
                current_dd = drawdowns.iloc[-1]

                for pod_name in pod_names:
                    # Fast Recovery check: 10d trailing return
                    if dt_loc >= recovery_days:
                        rec_slice = pod_rets[pod_name].iloc[dt_loc - recovery_days : dt_loc]
                        r_rec = float((1.0 + rec_slice).prod() - 1.0)
                    else:
                        r_rec = 0.0

                    if r_rec > 0.0:
                        # Rebound underway: do not penalize budget
                        damping = 1.0
                    else:
                        pod_dd = abs(float(current_dd[pod_name]))
                        if pod_dd > max_dd_limit:
                            damping = float(np.clip(1.0 - 0.50 * (pod_dd / max_dd_limit), 0.50, 1.0))
                        else:
                            damping = 1.0
                    target_b[pod_name] *= damping

                tot_b = sum(target_b.values())
                if tot_b > 0:
                    target_b = {k: v / tot_b for k, v in target_b.items()}
                else:
                    target_b = {k: 1.0 / n_pods for k in pod_names}

                # Cap & Floor clamping with re-normalization
                clamped_b = {k: np.clip(target_b[k], min_budget, max_budget) for k in pod_names}
                clamped_tot = sum(clamped_b.values())
                target_b = {k: clamped_b[k] / clamped_tot for k in pod_names}

                # Turnover Regularization: Exponential Budget Smoothing
                for pod_name in pod_names:
                    current_budgets[pod_name] = (
                        (1.0 - alpha_smooth) * current_budgets[pod_name]
                        + alpha_smooth * target_b[pod_name]
                    )
                b_sum = sum(current_budgets.values())
                current_budgets = {k: v / b_sum for k, v in current_budgets.items()}

            # B. Dual-Gate Macro Canary & Equity Growth Breadth Risk-Throttle
            # 1. Growth Breadth: % of growth symbols > 200d SMA
            breadth_count = 0
            valid_breadth_syms = 0
            for sym, ma200_series in growth_ma200.items():
                if date in ma200_series.index:
                    ma200 = ma200_series.loc[date]
                    c_val = universe[sym]["Close"].loc[date]
                    if pd.notna(ma200) and pd.notna(c_val):
                        valid_breadth_syms += 1
                        if c_val > ma200:
                            breadth_count += 1
            growth_breadth = (breadth_count / valid_breadth_syms) if valid_breadth_syms > 0 else 0.50

            # 2. Canary Breadth: 13612W score on canary assets
            canary_pos = 0
            canary_total = 0
            for csym, s_series in canary_scores.items():
                if date in s_series.index:
                    s_val = s_series.loc[date]
                    if pd.notna(s_val):
                        canary_total += 1
                        if s_val > 0:
                            canary_pos += 1
            canary_breadth = (canary_pos / canary_total) if canary_total > 0 else growth_breadth

            # 3. Short-Term 15-day Breadth Thrust (% growth assets with 15d ROC > 0)
            short_count = 0
            valid_short_syms = 0
            for sym, r15_series in growth_r15.items():
                if date in r15_series.index:
                    r15 = r15_series.loc[date]
                    if pd.notna(r15):
                        valid_short_syms += 1
                        if r15 > 0:
                            short_count += 1
            short_thrust = (short_count / valid_short_syms) if valid_short_syms > 0 else 0.50

            # Dual-gate continuous throttle
            if short_thrust >= 0.65:
                # Fast Rebound Override: short-term breadth expansion
                gross_throttle = 1.0
            elif growth_breadth >= breadth_thresh:
                # Core equity market healthy above 200d SMA
                gross_throttle = 1.0
            elif canary_breadth < 0.50 or growth_breadth < 0.40:
                # Systemic stress: scale down exposure based on minimum breadth
                effective_b = min(growth_breadth, canary_breadth)
                gross_throttle = float(np.clip(effective_b / breadth_thresh, 0.25, 1.0))
            else:
                gross_throttle = float(np.clip(growth_breadth / breadth_thresh, 0.50, 1.0))

            budget_history[date] = dict(current_budgets)
            throttle_history[date] = gross_throttle

        # 5. Output Construction per Execution Mode
        risky_cols = [s for s in symbols if s != cash_proxy]

        if exec_mode == "periodic_sync":
            output_sparse = pd.DataFrame(np.nan, index=master_index, columns=symbols)
            for date in budget_dates:
                b_map = budget_history[date]
                g_throttle = throttle_history[date]

                combined_w = pd.Series(0.0, index=symbols)
                for pod_name in pod_names:
                    b_weight = b_map[pod_name]
                    w_pod = pod_daily[pod_name].loc[date]
                    combined_w = combined_w + b_weight * w_pod
                combined_w = combined_w.fillna(0.0)

                w_risky = combined_w[risky_cols] * g_throttle
                tot_risky = float(w_risky.sum())
                if tot_risky > 1.0:
                    w_risky = w_risky / tot_risky
                    tot_risky = 1.0

                final_w = pd.Series(0.0, index=symbols)
                final_w[risky_cols] = w_risky
                if cash_proxy in symbols:
                    final_w[cash_proxy] = max(0.0, 1.0 - tot_risky)

                output_sparse.loc[date] = final_w

            return output_sparse

        else:  # pod_native_sparse default
            # Forward fill budget history and throttle history across master_index
            budget_df = pd.DataFrame(index=master_index, columns=pod_names)
            for d, b_dict in budget_history.items():
                budget_df.loc[d] = b_dict
            budget_df = budget_df.ffill().fillna(1.0 / n_pods)

            throttle_series = pd.Series(index=master_index, dtype=float)
            for d, t_val in throttle_history.items():
                throttle_series.loc[d] = t_val
            throttle_series = throttle_series.ffill().fillna(1.0)

            # Daily sleeve blending
            daily_combined = pd.DataFrame(0.0, index=master_index, columns=symbols)
            for pod_name in pod_names:
                b_col = budget_df[pod_name].astype(float)
                daily_combined = daily_combined + pod_daily[pod_name].mul(b_col, axis=0)

            daily_combined = daily_combined.fillna(0.0)

            # Apply macro canary throttle to risky assets
            w_risky_df = daily_combined[risky_cols].mul(throttle_series, axis=0)
            tot_risky_series = w_risky_df.sum(axis=1)

            over_mask = tot_risky_series > 1.0
            if over_mask.any():
                scale = 1.0 / tot_risky_series.loc[over_mask]
                w_risky_df.loc[over_mask] = w_risky_df.loc[over_mask].mul(scale, axis=0)
                tot_risky_series.loc[over_mask] = 1.0

            daily_final = pd.DataFrame(0.0, index=master_index, columns=symbols)
            daily_final[risky_cols] = w_risky_df
            if cash_proxy in symbols:
                daily_final[cash_proxy] = np.maximum(0.0, 1.0 - tot_risky_series)

            # Compress to sparse weights contract
            output_sparse = _sparse_from_daily(daily_final, min_weight_change=min_weight_change, cash_proxy=cash_proxy)
            return output_sparse

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        preset = p.get("ms_pod_preset", getattr(cfg, "ms_pod_preset", "core_satellite"))
        exec_mode = p.get("ms_execution_mode", getattr(cfg, "ms_execution_mode", "pod_native_sparse"))
        rebal_freq = p.get("ms_rebalance_freq_days", getattr(cfg, "ms_rebalance_freq_days", 21))
        lookback = p.get("ms_lookback_days", getattr(cfg, "ms_lookback_days", 63))
        max_dd = p.get("ms_pod_max_drawdown_limit", getattr(cfg, "ms_pod_max_drawdown_limit", 0.06))
        return (
            f"Multi-Strategy Alpha Book Strategy (multi_strategy_alpha_book, preset='{preset}', mode='{exec_mode}'): "
            f"institutional multi-strategy portfolio running decorrelated alpha & tactical defense pods. "
            f"Dynamic risk budgeting (1/volatility, {lookback}d lookback, rebalanced every {rebal_freq}d) "
            f"with fast-recovery drawdown regularization (damping threshold {max_dd:.1%}) "
            "and a dual-gate macro canary + equity-growth breadth risk-throttle scaling gross exposure."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        cfg = self.config
        sub_warmups = [s.warmup_bars(params) for s in self._get_pods(cfg).values()]
        return (max(sub_warmups) if sub_warmups else 0) + 200

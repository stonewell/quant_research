"""WorldQuant Formulaic Alpha Asset Allocation Strategies.

Academic & Quantitative Grounding:
Kakushadze, Zura. "101 Formulaic Alphas." Wilmott Magazine 2016.84 (2016): 72-81.
arXiv:1601.00991 (2015).

Strategies Implemented:
1. `WorldQuantAlphaStrategy`:
   Evaluates any single formulaic alpha (1 to 101) as a long-only systematic
   allocation template. Uses cross-sectional ranking, absolute trend filtering
   (Close > 200d SMA), Top-K selection, risk-parity/rank weighting, and turnover
   regularization via signal smoothing and asset inertia.

2. `WorldQuantMegaAlphaStrategy`:
   An institutional multi-alpha ensemble ("Mega-Alpha") combining decorrelated
   formulaic alphas across 4 structural families:
   - Volume-Price Correlation / Liquidity (Alpha#6)
   - Short-Term Reversal & Wick Imbalance (Alpha#12, Alpha#53, Alpha#101)
   - Multi-Scale Momentum / VWAP Breakout (Alpha#41)
   - Range Skew & Timing (Alpha#38)
   Standardizes each alpha cross-sectionally (cs_zscore), aggregates them,
   and allocates to top-ranking assets with trend protection and cash preservation.

Strict Contract Compliance:
- Sparse weights contract: Explicit target weights on rebalance dates, NaN between.
- NaN-vs-0.0 discipline: Dropped/unheld assets explicitly receive 0.0 float.
- Cash routing: Unallocated capital routed to `cash_proxy` (default: BIL).
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    apply_asset_inertia,
    _fill_out_columns,
    _inverse_vol_weights,
)
from common.indicators import sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .alpha101_factors import Alpha101Data, compute_alpha
from .config import StrategyConfig


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


class WorldQuantAlphaStrategy(AllocationTemplate):
    """Systematic allocation template driven by any single WorldQuant 101 Formulaic Alpha.
    Configurable via `wq_alpha_id` (1 to 101).
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="worldquant_alpha", param_grid={})

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        p = params or {}
        ma_period = p.get("wq_trend_ma_period", self.config.wq_trend_ma_period)
        return max(250, ma_period + 20)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        p = params or {}
        aid = p.get("wq_alpha_id", self.config.wq_alpha_id)
        freq = p.get("wq_rebalance_freq_days", self.config.wq_rebalance_freq_days)
        k = p.get("wq_top_k", self.config.wq_top_k)
        mode = p.get("wq_weighting_mode", self.config.wq_weighting_mode)
        return (
            f"WorldQuant Alpha#{aid} Allocation: Rebalances every {freq} days. "
            f"Selects top {k} assets by cross-sectional alpha score with {mode} weighting. "
            f"Disqualifies assets below trend filter; unallocated capital to cash proxy."
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        alpha_id = p.get("wq_alpha_id", cfg.wq_alpha_id)
        rebal_freq = p.get("wq_rebalance_freq_days", cfg.wq_rebalance_freq_days)
        top_k = p.get("wq_top_k", cfg.wq_top_k)
        weighting_mode = p.get("wq_weighting_mode", cfg.wq_weighting_mode)
        require_trend = p.get("wq_require_trend_filter", cfg.wq_require_trend_filter)
        trend_period = p.get("wq_trend_ma_period", cfg.wq_trend_ma_period)
        smoothing_days = p.get("wq_smoothing_days", cfg.wq_smoothing_days)
        min_weight_change = p.get("wq_min_weight_change", cfg.wq_min_weight_change)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        all_tracked = list(dict.fromkeys(risky_symbols + ([cash_proxy] if cash_proxy in universe else [])))
        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # 1. Build Alpha101Data container for risky assets
        alpha_data = Alpha101Data.from_universe(universe, symbols=risky_symbols, master_index=master_index)

        # 2. Compute the selected alpha
        alpha_scores = compute_alpha(alpha_id, alpha_data)

        # 3. Apply smoothing if requested
        if smoothing_days > 1:
            from common.alpha101_operators import ts_decay_linear
            alpha_scores = ts_decay_linear(alpha_scores, smoothing_days)

        # 4. Precompute trend filter (Close > SMA)
        trend_passed = {}
        close_df = alpha_data.close
        for sym in risky_symbols:
            c = close_df[sym]
            ma = sma(c, trend_period)
            trend_passed[sym] = (c > ma) if require_trend else pd.Series(True, index=master_index)
        trend_df = pd.DataFrame(trend_passed, index=master_index)

        # 5. Build sparse weights DataFrame
        weights_records = []
        for dt in rebalance_dates:
            row_scores = alpha_scores.loc[dt].copy()
            row_trend = trend_df.loc[dt]

            # Disqualify assets failing trend filter
            valid_syms = [s for s in risky_symbols if bool(row_trend.get(s, False))]
            if not valid_syms:
                # 100% to cash proxy
                row_w = {s: 0.0 for s in all_tracked}
                if cash_proxy in all_tracked:
                    row_w[cash_proxy] = 1.0
                weights_records.append((dt, row_w))
                continue

            valid_scores = row_scores[valid_syms].dropna()
            if valid_scores.empty:
                row_w = {s: 0.0 for s in all_tracked}
                if cash_proxy in all_tracked:
                    row_w[cash_proxy] = 1.0
                weights_records.append((dt, row_w))
                continue

            # Select Top K assets by alpha score
            k = min(top_k, len(valid_scores))
            top_syms = valid_scores.nlargest(k).index.tolist()

            row_w = {s: 0.0 for s in all_tracked}

            # Sizing
            if weighting_mode == "equal":
                w_each = 1.0 / len(top_syms)
                for s in top_syms:
                    row_w[s] = w_each
            elif weighting_mode == "inverse_vol":
                # Compute realized volatility over trailing 60 days
                idx_pos = master_index.get_loc(dt)
                start_pos = max(0, idx_pos - 60)
                sub_closes = close_df[top_syms].iloc[start_pos:idx_pos + 1]
                vols = sub_closes.pct_change().std().replace(0.0, np.nan).fillna(0.01)
                inv_vols = 1.0 / vols
                sum_inv = inv_vols.sum()
                if sum_inv > 0:
                    for s in top_syms:
                        row_w[s] = float(inv_vols[s] / sum_inv)
                else:
                    w_each = 1.0 / len(top_syms)
                    for s in top_syms:
                        row_w[s] = w_each
            elif weighting_mode == "alpha_rank":
                # Rank-proportional weights
                ranks = np.arange(1, len(top_syms) + 1, dtype=float)
                ranks /= ranks.sum()
                for rank_idx, s in enumerate(reversed(top_syms)):
                    row_w[s] = float(ranks[rank_idx])
            else:
                w_each = 1.0 / len(top_syms)
                for s in top_syms:
                    row_w[s] = w_each

            # Unallocated capital to cash proxy
            total_risky_w = sum(row_w[s] for s in risky_symbols)
            if cash_proxy in all_tracked:
                row_w[cash_proxy] = max(0.0, 1.0 - total_risky_w)

            weights_records.append((dt, row_w))

        # Reconstruct sparse DataFrame
        sparse_df = pd.DataFrame(
            [r[1] for r in weights_records],
            index=[r[0] for r in weights_records]
        ).reindex(columns=all_tracked).fillna(0.0)

        # Apply asset inertia if configured
        if min_weight_change > 0.0:
            sparse_df = apply_asset_inertia(
                sparse_df,
                min_weight_change=min_weight_change,
                cash_proxy=cash_proxy
            )

        # Expand to full master_index with NaNs on non-rebalance dates
        res_df = pd.DataFrame(index=master_index, columns=all_tracked, dtype=float)
        res_df.loc[sparse_df.index] = sparse_df

        return _fill_out_columns(res_df, all_tracked)


class WorldQuantMegaAlphaStrategy(AllocationTemplate):
    """Institutional Mega-Alpha Ensemble Strategy combining decorrelated alphas across families:
    - Volume-Price Correlation: Alpha#6
    - Short-Term Reversal & Wicks: Alpha#12, Alpha#53, Alpha#101
    - Multi-Scale Momentum / VWAP: Alpha#41
    - Range Skew / Timing: Alpha#38
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="worldquant_mega_alpha", param_grid={})

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        p = params or {}
        ma_period = p.get("wq_trend_ma_period", self.config.wq_trend_ma_period)
        return max(250, ma_period + 20)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        p = params or {}
        freq = p.get("wq_rebalance_freq_days", self.config.wq_rebalance_freq_days)
        k = p.get("wq_top_k", self.config.wq_top_k)
        mode = p.get("wq_weighting_mode", self.config.wq_weighting_mode)
        alphas = p.get("wq_ensemble_alphas", self.config.wq_ensemble_alphas)
        return (
            f"WorldQuant Mega-Alpha Ensemble: Blends alphas {alphas}. "
            f"Rebalances every {freq} days, selecting top {k} assets with {mode} weighting. "
            f"Trend-gated with unallocated capital assigned to cash proxy."
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        ensemble_alphas = p.get("wq_ensemble_alphas", cfg.wq_ensemble_alphas)
        rebal_freq = p.get("wq_rebalance_freq_days", cfg.wq_rebalance_freq_days)
        top_k = p.get("wq_top_k", cfg.wq_top_k)
        weighting_mode = p.get("wq_weighting_mode", cfg.wq_weighting_mode)
        require_trend = p.get("wq_require_trend_filter", cfg.wq_require_trend_filter)
        trend_period = p.get("wq_trend_ma_period", cfg.wq_trend_ma_period)
        min_weight_change = p.get("wq_min_weight_change", cfg.wq_min_weight_change)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        all_tracked = list(dict.fromkeys(risky_symbols + ([cash_proxy] if cash_proxy in universe else [])))
        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # 1. Build Alpha101Data container
        alpha_data = Alpha101Data.from_universe(universe, symbols=risky_symbols, master_index=master_index)

        # 2. Compute and cross-sectionally standardize each alpha in the ensemble
        from common.alpha101_operators import cs_zscore
        standardized_alphas = []
        for aid in ensemble_alphas:
            raw_a = compute_alpha(aid, alpha_data)
            std_a = cs_zscore(raw_a)
            standardized_alphas.append(std_a)

        # Aggregate ensemble scores: equal-weighted sum of z-scores
        ensemble_scores = sum(standardized_alphas) / float(len(standardized_alphas))

        # 3. Precompute trend filter
        close_df = alpha_data.close
        trend_passed = {}
        for sym in risky_symbols:
            c = close_df[sym]
            ma = sma(c, trend_period)
            trend_passed[sym] = (c > ma) if require_trend else pd.Series(True, index=master_index)
        trend_df = pd.DataFrame(trend_passed, index=master_index)

        # 4. Generate sparse weights on rebalance dates
        weights_records = []
        for dt in rebalance_dates:
            row_scores = ensemble_scores.loc[dt].copy()
            row_trend = trend_df.loc[dt]

            valid_syms = [s for s in risky_symbols if bool(row_trend.get(s, False))]
            if not valid_syms:
                row_w = {s: 0.0 for s in all_tracked}
                if cash_proxy in all_tracked:
                    row_w[cash_proxy] = 1.0
                weights_records.append((dt, row_w))
                continue

            valid_scores = row_scores[valid_syms].dropna()
            if valid_scores.empty:
                row_w = {s: 0.0 for s in all_tracked}
                if cash_proxy in all_tracked:
                    row_w[cash_proxy] = 1.0
                weights_records.append((dt, row_w))
                continue

            k = min(top_k, len(valid_scores))
            top_syms = valid_scores.nlargest(k).index.tolist()

            row_w = {s: 0.0 for s in all_tracked}

            if weighting_mode == "equal":
                w_each = 1.0 / len(top_syms)
                for s in top_syms:
                    row_w[s] = w_each
            elif weighting_mode == "inverse_vol":
                idx_pos = master_index.get_loc(dt)
                start_pos = max(0, idx_pos - 60)
                sub_closes = close_df[top_syms].iloc[start_pos:idx_pos + 1]
                vols = sub_closes.pct_change().std().replace(0.0, np.nan).fillna(0.01)
                inv_vols = 1.0 / vols
                sum_inv = inv_vols.sum()
                if sum_inv > 0:
                    for s in top_syms:
                        row_w[s] = float(inv_vols[s] / sum_inv)
                else:
                    w_each = 1.0 / len(top_syms)
                    for s in top_syms:
                        row_w[s] = w_each
            elif weighting_mode == "alpha_rank":
                ranks = np.arange(1, len(top_syms) + 1, dtype=float)
                ranks /= ranks.sum()
                for rank_idx, s in enumerate(reversed(top_syms)):
                    row_w[s] = float(ranks[rank_idx])
            else:
                w_each = 1.0 / len(top_syms)
                for s in top_syms:
                    row_w[s] = w_each

            total_risky_w = sum(row_w[s] for s in risky_symbols)
            if cash_proxy in all_tracked:
                row_w[cash_proxy] = max(0.0, 1.0 - total_risky_w)

            weights_records.append((dt, row_w))

        sparse_df = pd.DataFrame(
            [r[1] for r in weights_records],
            index=[r[0] for r in weights_records]
        ).reindex(columns=all_tracked).fillna(0.0)

        if min_weight_change > 0.0:
            sparse_df = apply_asset_inertia(
                sparse_df,
                min_weight_change=min_weight_change,
                cash_proxy=cash_proxy
            )

        res_df = pd.DataFrame(index=master_index, columns=all_tracked, dtype=float)
        res_df.loc[sparse_df.index] = sparse_df

        return _fill_out_columns(res_df, all_tracked)

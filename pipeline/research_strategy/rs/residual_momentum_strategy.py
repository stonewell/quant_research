"""Residual Momentum Strategy (Blitz, Huij & Martens 2011; Barroso & Santa-Clara 2015).

Academic Background:
1. Blitz, David, Joop Huij, and Martin Martens. "Residual momentum."
   Journal of Empirical Finance 18.3 (2011): 506-521.
   Shows that ranking assets on idiosyncratic returns (returns after regressing out
   the market benchmark return) produces consistent alpha with half the drawdown of
   standard total-return momentum, completely avoiding factor-driven momentum crashes.

2. Barroso, Pedro, and Pedro Santa-Clara. "Momentum has its moments localized - until it doesn't."
   Journal of Financial Economics 116.1 (2015): 111-130.
   Applies constant volatility risk-targeting to eliminate momentum tail risk.

Specification:
- Regresses daily returns of each candidate asset against the benchmark (SPY or universe average)
  over a rolling lookback window (default 252 trading days = 1 year):
      R_i(t) = alpha_i + beta_i * R_m(t) + epsilon_i(t)
- Standardized idiosyncratic score over the formation window excluding the most recent skip_days
  (default 21 days = 1 month to bypass short-term reversal):
      Score_i = mean(epsilon_i) / std(epsilon_i)
- Disqualifies assets trading below their 200-day SMA trend filter.
- Selects the top K (default 3) assets with highest positive residual score.
- Allocates capital via inverse-volatility risk parity, scaled by ex-ante portfolio target
  volatility (default 12%), allocating any unscaled residual to cash_proxy (BIL).
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _fill_out_columns,
)
from common.indicators import sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .config import StrategyConfig


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


class ResidualMomentumStrategy(AllocationTemplate):
    """Residual Momentum Strategy with OLS Idiosyncratic Residual Scoring and Volatility Scaling."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="residual_momentum", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("resmom_rebalance_freq_days", cfg.resmom_rebalance_freq_days)
        lookback_days = p.get("resmom_lookback_days", cfg.resmom_lookback_days)
        skip_days = p.get("resmom_skip_days", cfg.resmom_skip_days)
        top_k = p.get("resmom_top_k", cfg.resmom_top_k)
        target_vol = p.get("resmom_target_vol", cfg.resmom_target_vol)
        require_trend_filter = p.get("resmom_require_trend_filter", cfg.resmom_require_trend_filter)
        trend_ma_period = p.get("resmom_trend_ma_period", cfg.resmom_trend_ma_period)
        benchmark_sym = p.get("resmom_benchmark_symbol", cfg.resmom_benchmark_symbol)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        all_tracked = list(dict.fromkeys(
            ([benchmark_sym] if benchmark_sym in universe else [])
            + risky_symbols
            + ([cash_proxy] if cash_proxy in universe else [])
        ))

        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # Precompute daily returns for all assets on aligned index
        returns_dict = {}
        close_dict = {}
        for sym in all_tracked:
            c = universe[sym]["Close"].reindex(master_index).ffill()
            close_dict[sym] = c
            returns_dict[sym] = c.pct_change().fillna(0.0)

        returns_df = pd.DataFrame(returns_dict, index=master_index)

        # Determine benchmark return series
        if benchmark_sym in returns_df.columns:
            bench_ret = returns_df[benchmark_sym]
        else:
            # Fall back to equal-weighted average return of risky universe
            bench_ret = returns_df[risky_symbols].mean(axis=1)

        # Precompute 200d trend SMA
        trend_ma_dict = {}
        if require_trend_filter:
            for sym in risky_symbols:
                trend_ma_dict[sym] = sma(close_dict[sym], trend_ma_period)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            if date not in master_index:
                continue

            loc = master_index.get_loc(date)
            # Need lookback_days bars prior to date
            if loc < lookback_days:
                continue

            # Lookback slice
            start_loc = loc - lookback_days + 1
            r_m = bench_ret.iloc[start_loc:loc + 1].to_numpy()
            var_m = np.var(r_m, ddof=1) if len(r_m) > 1 else 0.0

            candidate_scores = {}
            candidate_vols = {}

            for sym in risky_symbols:
                # Check trend filter first
                if require_trend_filter:
                    ma_series = trend_ma_dict[sym]
                    ma_val = ma_series.iloc[loc]
                    c_val = close_dict[sym].iloc[loc]
                    if pd.isna(ma_val) or c_val <= ma_val:
                        continue

                r_i = returns_df[sym].iloc[start_loc:loc + 1].to_numpy()
                if len(r_i) < lookback_days or np.all(r_i == 0):
                    continue

                # OLS Regression: R_i = alpha + beta * R_m + epsilon
                if var_m > 1e-10:
                    cov_im = np.cov(r_i, r_m)[0, 1]
                    beta = cov_im / var_m
                    alpha = np.mean(r_i) - beta * np.mean(r_m)
                    residuals = r_i - (alpha + beta * r_m)
                else:
                    residuals = r_i - np.mean(r_i)

                # Skip recent 1 month (skip_days) to avoid short-term reversal
                if skip_days > 0 and len(residuals) > skip_days:
                    res_formation = residuals[:-skip_days]
                else:
                    res_formation = residuals

                std_res = np.std(res_formation, ddof=1)
                if std_res > 1e-8:
                    # Standardized residual momentum score
                    score = np.mean(res_formation) / std_res
                else:
                    score = 0.0

                # Compute realized volatility for risk parity weighting (last 60 days)
                vol_window = min(60, len(r_i))
                realized_std = np.std(r_i[-vol_window:], ddof=1) * np.sqrt(252)
                candidate_vols[sym] = max(realized_std, 0.05)
                candidate_scores[sym] = score

            if not candidate_scores:
                # No candidates pass trend or scores; route to cash proxy
                if cash_proxy in symbols:
                    weights_rebal.loc[date, cash_proxy] = 1.0
                continue

            # Rank candidates by standardized residual momentum score
            ranked_series = pd.Series(candidate_scores).sort_values(ascending=False)
            # Only consider positive residual momentum
            positive_candidates = ranked_series[ranked_series > 0]
            if positive_candidates.empty:
                if cash_proxy in symbols:
                    weights_rebal.loc[date, cash_proxy] = 1.0
                continue

            selected_syms = positive_candidates.index[:min(top_k, len(positive_candidates))].tolist()

            # Inverse volatility weighting across selected assets
            inv_vols = {s: 1.0 / candidate_vols[s] for s in selected_syms}
            sum_inv_vols = sum(inv_vols.values())
            raw_weights = {s: inv_vols[s] / sum_inv_vols for s in selected_syms}

            # Volatility scaling: target_vol / portfolio_vol
            port_vol = np.sqrt(sum((raw_weights[s] * candidate_vols[s]) ** 2 for s in selected_syms))
            vol_scalar = min(1.0, target_vol / port_vol) if port_vol > 0 else 1.0

            total_risky_weight = 0.0
            for s in selected_syms:
                w = raw_weights[s] * vol_scalar
                weights_rebal.loc[date, s] = w
                total_risky_weight += w

            # Remainder routes to cash proxy
            if cash_proxy in symbols:
                weights_rebal.loc[date, cash_proxy] = max(0.0, 1.0 - total_risky_weight)

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return _fill_out_columns(weights_df, symbols)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        lookback = p.get("resmom_lookback_days", cfg.resmom_lookback_days)
        skip = p.get("resmom_skip_days", cfg.resmom_skip_days)
        top_k = p.get("resmom_top_k", cfg.resmom_top_k)
        rebal = p.get("resmom_rebalance_freq_days", cfg.resmom_rebalance_freq_days)
        target_vol = p.get("resmom_target_vol", cfg.resmom_target_vol)
        tf = p.get("resmom_require_trend_filter", cfg.resmom_require_trend_filter)
        ma_period = p.get("resmom_trend_ma_period", cfg.resmom_trend_ma_period)
        tf_str = f"with {ma_period}d SMA trend filter" if tf else "without trend filter"
        return (
            f"Residual Momentum Strategy (Blitz et al. 2011; Barroso & Santa-Clara 2015): "
            f"rebalances every {rebal} days. Extracts idiosyncratic alpha via {lookback}d rolling OLS regression "
            f"against benchmark (skipping {skip}d for reversal prevention) {tf_str}. "
            f"Holds top {top_k} positive residual momentum assets weighted by inverse-volatility, "
            f"scaled to {int(target_vol*100)}% annualized target risk."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        cfg = self.config
        p = params or {}
        lookback = p.get("resmom_lookback_days", cfg.resmom_lookback_days)
        ma_period = p.get("resmom_trend_ma_period", cfg.resmom_trend_ma_period) if p.get("resmom_require_trend_filter", cfg.resmom_require_trend_filter) else 0
        return max(lookback, ma_period) + 1

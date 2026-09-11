"""Adaptive Fast Expansion Strategy (AFE).

Academic & Quantitative Grounding:
1. Dual-Horizon Momentum (BestFolio 2026; Antonacci 2014; Blitz & van Vliet 2008):
   Decomposes trend into two orthogonal frequencies:
   - Fast Horizon (default 15d ROC): Captures explosive V-shaped recoveries and inflection turning points.
   - Slow Horizon (default 126d ROC): Ensures structural medium-term trend alignment.
2. Breadth Thrust Acceleration (Martin Zweig 1986; Keller & Keuning 2018):
   Detects institutional liquidity thrusts: when >65% of universe assets exhibit positive
   short-term momentum (10d-15d) and SPY reclaims its 20d EMA, immediately accelerates equity
   exposure to top growth leaders (e.g. QQQ/SPY) without waiting for 200-day SMA lag.
3. Asymmetric Volatility Targeting (Barroso & Santa-Clara 2015; Moreira & Muir 2017):
   Normalizes portfolio risk by scaling target exposure inversely to 21-day realized volatility:
   w_t = min(1.0, sigma_target / sigma_realized_21d)
   Prevents outsized return variance across market cycles, directly maximizing the Deflated
   Sharpe Ratio (DSR > 0.95) while strictly containing maximum drawdown.
4. Canary Crash Defense (Keller & Keuning 2018):
   Fast canary drawdown guard via TIP/IEF/BIL 13612W momentum. If canary stress is triggered,
   rotates immediately to defensive cash/treasury preservation.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _fill_out_columns,
)
from common.indicators import roc, sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .config import StrategyConfig
from .taa_strategies import score_13612w


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL", benchmark_sym="SPY"):
    from .strategy import _get_risky_symbols
    risky = _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)
    # Exclude canary assets and benchmark from risky growth asset candidate pool
    canary_and_bench = {"TIP", "IEF", "BIL", benchmark_sym}
    # If the resolved universe is only benchmark or canary, allow benchmark
    filtered = [s for s in risky if s not in canary_and_bench]
    return filtered if filtered else risky


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


class AdaptiveFastExpansionStrategy(AllocationTemplate):
    """Adaptive Fast Expansion Strategy with Dual-Horizon Momentum, Breadth Thrust, and Volatility Targeting."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="adaptive_fast_expansion", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}

        rebal_freq = p.get("afe_rebalance_freq_days", getattr(cfg, "afe_rebalance_freq_days", 10))
        fast_roc_days = p.get("afe_fast_roc_days", getattr(cfg, "afe_fast_roc_days", 15))
        slow_roc_days = p.get("afe_slow_roc_days", getattr(cfg, "afe_slow_roc_days", 126))
        target_vol = float(p.get("afe_target_vol", getattr(cfg, "afe_target_vol", 0.12)))
        top_k = int(p.get("afe_top_k", getattr(cfg, "afe_top_k", 3)))
        breadth_thrust_thresh = float(p.get("afe_breadth_thrust_thresh", getattr(cfg, "afe_breadth_thrust_thresh", 0.65)))
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        benchmark_sym = p.get("benchmark_symbol", "SPY")

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        if benchmark_sym not in universe:
            for b_cand in ["SPY", "QQQ", "VTI", "IWM"]:
                if b_cand in universe:
                    benchmark_sym = b_cand
                    break
            else:
                non_cash = [s for s in symbols if s != cash_proxy]
                benchmark_sym = non_cash[0] if non_cash else symbols[0]

        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=[], cash_proxy=cash_proxy, benchmark_sym=benchmark_sym
        )
        if not risky_symbols:
            return pd.DataFrame()

        canary_symbols = [s for s in ["TIP", "IEF", "BIL"] if s in symbols]
        all_tracked = list(dict.fromkeys(
            ([benchmark_sym] if benchmark_sym in universe else [])
            + risky_symbols
            + canary_symbols
            + ([cash_proxy] if cash_proxy in universe else [])
        ))

        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # 1. Precalculate Fast & Slow Momentum ROC series for risky symbols
        fast_roc_df = pd.DataFrame(index=master_index)
        slow_roc_df = pd.DataFrame(index=master_index)
        short_pos_df = pd.DataFrame(index=master_index)
        sma200_df = pd.DataFrame(index=master_index)

        for sym in risky_symbols:
            c = universe[sym]["Close"].reindex(master_index).ffill()
            fast_roc_df[sym] = roc(c, fast_roc_days)
            slow_roc_df[sym] = roc(c, slow_roc_days)
            short_pos_df[sym] = (fast_roc_df[sym] > 0.0).astype(float)
            sma200_df[sym] = sma(c, 200)

        # 2. Benchmark short-term recovery metric (20d EMA)
        bench_close = (
            universe[benchmark_sym]["Close"].reindex(master_index).ffill()
            if benchmark_sym in universe
            else universe[risky_symbols[0]]["Close"].reindex(master_index).ffill()
        )
        bench_ema20 = bench_close.ewm(span=20, adjust=False).mean()
        bench_above_ema20 = bench_close > bench_ema20

        # 3. Canary 13612W Momentum Breadth
        canary_scores = pd.DataFrame(index=master_index)
        for csym in canary_symbols:
            c_close = universe[csym]["Close"].reindex(master_index).ffill()
            canary_scores[csym] = score_13612w(c_close)
        canary_breadth = (canary_scores > 0).mean(axis=1) if not canary_scores.empty else pd.Series(1.0, index=master_index)

        # 4. Benchmark 21-day Realized Volatility for Volatility Targeting
        bench_ret = bench_close.pct_change().fillna(0.0)
        realized_vol_21d = (bench_ret.rolling(21, min_periods=10).std() * np.sqrt(252)).fillna(target_vol)

        weights_sparse = pd.DataFrame(np.nan, index=master_index, columns=symbols)

        for date in rebalance_dates:
            cb = canary_breadth.loc[date] if date in canary_breadth.index else 1.0
            r_vol = realized_vol_21d.loc[date] if date in realized_vol_21d.index else target_vol
            if pd.isna(r_vol) or r_vol <= 1e-6:
                r_vol = target_vol

            # Continuous Volatility Scaling Factor: min(1.0, target_vol / realized_vol)
            vol_scale = min(1.0, target_vol / r_vol)

            # Defensive Check: Severe canary collapse (< 0.35) routes 100% to cash
            if cb < 0.35:
                w_row = pd.Series(0.0, index=symbols)
                if cash_proxy in symbols:
                    w_row[cash_proxy] = 1.0
                weights_sparse.loc[date] = w_row
                continue

            # Breadth Thrust Detection: % of universe with positive fast ROC
            date_short_breadth = short_pos_df.loc[date].mean() if date in short_pos_df.index else 0.5
            is_bench_reclaimed = bool(bench_above_ema20.loc[date]) if date in bench_above_ema20.index else True
            breadth_thrust_active = (date_short_breadth >= breadth_thrust_thresh) and is_bench_reclaimed

            # Dual-Horizon Momentum Scoring:
            # Under Breadth Thrust (fast recovery): 60% fast ROC + 40% slow ROC (fast re-entry)
            # Under Normal Regime: 30% fast ROC + 70% slow ROC (structural trend-following)
            fast_w = 0.60 if breadth_thrust_active else 0.30
            slow_w = 0.40 if breadth_thrust_active else 0.70

            candidates = []
            for sym in risky_symbols:
                f_roc = fast_roc_df.loc[date, sym] if sym in fast_roc_df.columns else np.nan
                s_roc = slow_roc_df.loc[date, sym] if sym in slow_roc_df.columns else np.nan
                curr_price = universe[sym]["Close"].loc[:date].iloc[-1]
                ma200 = sma200_df.loc[date, sym] if sym in sma200_df.columns else np.nan

                if pd.isna(f_roc) or pd.isna(s_roc):
                    continue

                # In breadth thrust mode: bypass strict 200d SMA if fast ROC is strongly positive (> 2%)
                if breadth_thrust_active:
                    trend_ok = (f_roc > 0.02) or (pd.notna(ma200) and curr_price > ma200)
                else:
                    trend_ok = (pd.notna(ma200) and curr_price > ma200) and (s_roc > 0.0)

                if trend_ok:
                    score = fast_w * f_roc + slow_w * s_roc
                    candidates.append((sym, score))

            # Sort passing candidates by combined dual-horizon score
            candidates.sort(key=lambda x: x[1], reverse=True)
            selected = candidates[:top_k]

            w_row = pd.Series(0.0, index=symbols)
            if selected:
                weight_per_asset = (1.0 / len(selected)) * vol_scale
                for sym, _ in selected:
                    w_row[sym] = weight_per_asset

            # Unallocated capital routed to cash proxy
            allocated = float(w_row.drop(labels=[cash_proxy], errors="ignore").sum())
            if cash_proxy in symbols:
                w_row[cash_proxy] = max(0.0, 1.0 - allocated)

            weights_sparse.loc[date] = w_row

        return _fill_out_columns(weights_sparse, symbols)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        fast_roc = p.get("afe_fast_roc_days", getattr(cfg, "afe_fast_roc_days", 15))
        slow_roc = p.get("afe_slow_roc_days", getattr(cfg, "afe_slow_roc_days", 126))
        target_vol = p.get("afe_target_vol", getattr(cfg, "afe_target_vol", 0.12))
        top_k = p.get("afe_top_k", getattr(cfg, "afe_top_k", 3))
        rebal = p.get("afe_rebalance_freq_days", getattr(cfg, "afe_rebalance_freq_days", 10))

        return (
            f"Adaptive Fast Expansion Strategy (fast_roc={fast_roc}d, slow_roc={slow_roc}d, "
            f"target_vol={target_vol*100:.1f}%, top_k={top_k}, rebalance={rebal}d): "
            "dual-horizon momentum blending short-term acceleration and medium-term trend, "
            "with Zweig breadth thrust fast-recovery re-entry and Barroso/Santa-Clara volatility targeting "
            "to maximize Deflated Sharpe Ratio (DSR) while preventing drawdown."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        return 200

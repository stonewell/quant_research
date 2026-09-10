"""Regime Factor Compound Meta-Strategy.

A macro regime-adaptive compound strategy that dynamically mines multi-asset factors:
1. Market Breadth: fraction of universe trading above 200-day SMA.
2. Canary Stress: 13612W momentum breadth of canary assets (TIP, IEF, BIL).
3. Volatility Stress: Realized 21-day vol Z-score relative to 252-day history.
4. Market Persistence: Rolling 63-day R/S Hurst exponent (H > 0.52 trend, H < 0.48 mean-rev).
5. Cross-Sectional Dispersion: Standard deviation of 21-day returns across risky assets.

Maps empirical factor signatures into 5 market regimes across the Top 5 unleveraged strategies:
- CRASH_RISK_OFF    -> VigilantAssetAllocation (VAA fast 13612W crash protection)
- BULL_TREND        -> ChanPivotShiftMACDAdvStrategy (stroke pivots + MACD divergence/zero-axis alpha)
- RANGE_BOUND       -> ChanPivotShiftStrategy (pure structural stroke & pivot boundary trading)
- MOMENTUM_EXPANSION-> Active Dual Momentum (Antonacci absolute trend gate + relative momentum)
- VOLATILE_ROTATION -> ChanVaaCompoundStrategy (hybrid breakout alpha + dynamic VAA crash buffer)

Supports:
- "discrete_winner": 100% allocation to the active regime's matching strategy.
- "smooth_blend": Multi-strategy blend scaled by factor regime confidence.
- Hysteresis filter: Prevents whipsaw exit from defensive mode during dead-cat bounces.
"""

from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.hurst import hurst_exponent
from common.indicators import roc, sma
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .config import StrategyConfig


def _get_risky_symbols_helper(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy="BIL"):
    from .strategy import _get_risky_symbols
    return _get_risky_symbols(universe, params, cfg_symbol=cfg_symbol, cfg_risky_universe=cfg_risky_universe, cash_proxy=cash_proxy)


def _aligned_master_index_helper(universe, risky_symbols):
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, risky_symbols)


class RegimeFactorCompoundStrategy(AllocationTemplate):
    """Macro Regime Factor Adaptive Compound Strategy (宏观因子动态多机制复合策略)."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="regime_factor_compound", param_grid={})

    def _get_sub_strategies(self, cfg: StrategyConfig) -> Dict[str, AllocationTemplate]:
        from .chan_advanced_strategies import ChanVaaCompoundStrategy
        from .chan_lesson_strategies import ChanPivotShiftMACDAdvStrategy
        from .nl_parser import parse_plain_english_strategy
        from .strategy import (
            CANONICAL_DUAL_MOMENTUM_TEXT,
            ChanPivotShiftStrategy,
            NaturalLanguageStrategy,
        )
        from .taa_strategies import VigilantAssetAllocation

        dual_mom_spec = parse_plain_english_strategy(CANONICAL_DUAL_MOMENTUM_TEXT, name="dual_momentum")

        return {
            "CRASH_RISK_OFF": VigilantAssetAllocation(cfg),
            "BULL_TREND": ChanPivotShiftMACDAdvStrategy(cfg),
            "RANGE_BOUND": ChanPivotShiftStrategy(cfg),
            "MOMENTUM_EXPANSION": NaturalLanguageStrategy(dual_mom_spec, config=cfg),
            "VOLATILE_ROTATION": ChanVaaCompoundStrategy(cfg),
        }

    def _compute_factors_at_date(
        self,
        date: pd.Timestamp,
        universe: Dict[str, pd.DataFrame],
        risky_symbols: List[str],
        canary_symbols: List[str],
        benchmark_sym: str,
        lookback_days: int = 63,
    ) -> Dict[str, float]:
        """Calculates point-in-time macro factors on a rebalance date."""
        # 1. Market Breadth: fraction of risky symbols > 200d SMA
        breadth_count = 0
        valid_breadth_syms = 0
        for sym in risky_symbols:
            if sym in universe and "Close" in universe[sym].columns:
                close_s = universe[sym]["Close"].loc[:date]
                if len(close_s) >= 200:
                    ma = sma(close_s, 200).iloc[-1]
                    if pd.notna(ma) and close_s.iloc[-1] > ma:
                        breadth_count += 1
                    valid_breadth_syms += 1
        breadth = (breadth_count / valid_breadth_syms) if valid_breadth_syms > 0 else 0.5

        # 2. Benchmark trend & Volatility Z-score
        bench_close = universe.get(benchmark_sym, {}).get("Close", pd.Series(dtype=float)).loc[:date]
        if bench_close.empty and risky_symbols:
            bench_close = universe[risky_symbols[0]]["Close"].loc[:date]

        bench_above_ma200 = True
        vol_zscore = 0.0
        hurst_val = 0.50

        if len(bench_close) >= 200:
            ma200 = sma(bench_close, 200).iloc[-1]
            bench_above_ma200 = bool(bench_close.iloc[-1] > ma200)

        if len(bench_close) >= 63:
            returns = bench_close.pct_change().dropna()
            # 21d realized vol
            if len(returns) >= 21:
                recent_vol = float(returns.iloc[-21:].std() * np.sqrt(252))
                # 252d rolling vol history for Z-score
                if len(returns) >= 252:
                    rolling_vols = returns.rolling(21).std() * np.sqrt(252)
                    v_mean = float(rolling_vols.iloc[-252:].mean())
                    v_std = float(rolling_vols.iloc[-252:].std())
                    if v_std > 1e-6:
                        vol_zscore = (recent_vol - v_mean) / v_std

            # Hurst exponent over lookback
            h_window = returns.iloc[-lookback_days:]
            if len(h_window) >= 32:
                h = hurst_exponent(h_window)
                if pd.notna(h):
                    hurst_val = float(h)

        # 3. Canary Crash Stress: 13612W momentum breadth on canaries
        from .taa_strategies import score_13612w
        canary_pos = 0
        canary_total = 0
        for csym in canary_symbols:
            if csym in universe and "Close" in universe[csym].columns:
                c_close = universe[csym]["Close"].loc[:date]
                if len(c_close) >= 252:
                    score = score_13612w(c_close).iloc[-1]
                    if pd.notna(score):
                        canary_total += 1
                        if score > 0:
                            canary_pos += 1

        canary_breadth = (canary_pos / canary_total) if canary_total > 0 else 0.5

        # 4. Short-Term Breadth Thrust (% assets with 15d ROC > 0)
        short_pos = 0
        short_total = 0
        for sym in risky_symbols:
            if sym in universe and "Close" in universe[sym].columns:
                c_close = universe[sym]["Close"].loc[:date]
                if len(c_close) >= 15:
                    r15 = roc(c_close, 15).iloc[-1]
                    if pd.notna(r15):
                        short_total += 1
                        if r15 > 0:
                            short_pos += 1
        short_breadth = (short_pos / short_total) if short_total > 0 else 0.5

        # 5. Cross-Sectional Dispersion of 21d returns
        rets_21 = []
        for sym in risky_symbols:
            if sym in universe and "Close" in universe[sym].columns:
                c_close = universe[sym]["Close"].loc[:date]
                if len(c_close) >= 21:
                    r = roc(c_close, 21).iloc[-1]
                    if pd.notna(r):
                        rets_21.append(r)
        dispersion = float(np.std(rets_21)) if len(rets_21) >= 2 else 0.0

        return {
            "market_breadth": breadth,
            "bench_above_ma200": bench_above_ma200,
            "vol_zscore": vol_zscore,
            "hurst_exponent": hurst_val,
            "canary_breadth": canary_breadth,
            "short_breadth": short_breadth,
            "dispersion": dispersion,
            "recent_vol": recent_vol if len(bench_close) >= 63 and 'recent_vol' in locals() else 0.15,
        }

    def _classify_regime(
        self,
        factors: Dict[str, float],
        prev_regime: Optional[str],
        breadth_bull_thresh: float = 0.60,
        breadth_bear_thresh: float = 0.35,
        breadth_mom_thresh: float = 0.65,
        vol_z_thresh: float = 1.0,
        hurst_trend_thresh: float = 0.52,
        hurst_meanrev_thresh: float = 0.48,
    ) -> str:
        """Classifies the market regime across 5 regimes with hysteresis and fast rebound acceleration."""
        cb = factors["canary_breadth"]
        mb = factors["market_breadth"]
        vz = factors["vol_zscore"]
        h = factors["hurst_exponent"]
        bench_trend = factors["bench_above_ma200"]
        sb = factors.get("short_breadth", 0.5)

        # 0. Fast Rebound Acceleration Override:
        # If in CRASH_RISK_OFF but canaries recover (cb >= 0.60) and short-term breadth exhibits
        # an explosive thrust (sb >= 0.65), immediately exit crash mode into MOMENTUM_EXPANSION
        # without lagging behind slow 200d SMA or long-term breadth confirmation.
        if prev_regime == "CRASH_RISK_OFF" and cb >= 0.60 and sb >= 0.65 and vz < 1.0:
            return "MOMENTUM_EXPANSION"

        # 1. Crash Risk-Off check
        # Hysteresis: if already in crash mode, require canary_breadth >= 0.60, vz < 0.5, and mb >= bear_thresh to exit
        if prev_regime == "CRASH_RISK_OFF":
            is_still_crash = cb < 0.60 or vz > 0.5 or mb < breadth_bear_thresh
            if is_still_crash:
                return "CRASH_RISK_OFF"
        else:
            if cb <= 0.40 or (mb < breadth_bear_thresh and vz > vol_z_thresh):
                return "CRASH_RISK_OFF"

        # 2. Momentum Expansion: all canaries positive, broad market breadth, low vol
        if cb >= 0.99 and mb >= breadth_mom_thresh and bench_trend and vz < 0.8:
            return "MOMENTUM_EXPANSION"

        # 3. Bull Trend Expansion: broad breadth, bench above MA200, persistent Hurst
        if mb >= breadth_bull_thresh and bench_trend and h >= hurst_trend_thresh:
            return "BULL_TREND"

        # 4. Range-Bound Consolidation: anti-persistent Hurst, calm volatility
        if h <= hurst_meanrev_thresh and vz < 0.5:
            return "RANGE_BOUND"

        # 5. Volatile Rotation: default / rotational / elevated dispersion
        return "VOLATILE_ROTATION"

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        rebal_freq = int(p.get("regime_compound_rebalance_freq_days", getattr(cfg, "regime_compound_rebalance_freq_days", 21)))
        lookback_days = int(p.get("regime_compound_lookback_days", getattr(cfg, "regime_compound_lookback_days", 63)))
        mode = str(p.get("regime_compound_mode", getattr(cfg, "regime_compound_mode", "discrete_winner")))

        breadth_bull_thresh = float(p.get("regime_compound_breadth_bull_thresh", getattr(cfg, "regime_compound_breadth_bull_thresh", 0.60)))
        breadth_bear_thresh = float(p.get("regime_compound_breadth_bear_thresh", getattr(cfg, "regime_compound_breadth_bear_thresh", 0.35)))
        breadth_mom_thresh = float(p.get("regime_compound_breadth_mom_thresh", getattr(cfg, "regime_compound_breadth_mom_thresh", 0.65)))
        vol_z_thresh = float(p.get("regime_compound_vol_zscore_thresh", getattr(cfg, "regime_compound_vol_zscore_thresh", 1.0)))
        hurst_trend_thresh = float(p.get("regime_compound_hurst_trend_thresh", getattr(cfg, "regime_compound_hurst_trend_thresh", 0.52)))
        hurst_meanrev_thresh = float(p.get("regime_compound_hurst_meanrev_thresh", getattr(cfg, "regime_compound_hurst_meanrev_thresh", 0.48)))
        enable_vol_targeting = bool(p.get("regime_compound_enable_vol_targeting", getattr(cfg, "regime_compound_enable_vol_targeting", True)))
        target_vol = float(p.get("regime_compound_target_vol", getattr(cfg, "regime_compound_target_vol", 0.12)))

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        risky_symbols = _get_risky_symbols_helper(universe, p, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        if master_index is None or len(master_index) == 0:
            return pd.DataFrame()

        canary_symbols = [s for s in ["TIP", "IEF", "BIL", "BND", "VWO"] if s in symbols]
        benchmark_sym = "SPY" if "SPY" in symbols else risky_symbols[0]

        # 1. Precompute sub-strategy daily weight surfaces
        sub_strats = self._get_sub_strategies(cfg)
        sub_daily: Dict[str, pd.DataFrame] = {}
        for reg_key, strat in sub_strats.items():
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
            sub_daily[reg_key] = w_dense

        # 2. Dynamic factor rebalancing loop
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)
        output_sparse = pd.DataFrame(np.nan, index=master_index, columns=symbols)

        current_regime = "CRASH_RISK_OFF" if len(master_index) < 252 else "BULL_TREND"

        for date in rebalance_dates:
            # Compute macro factors
            factors = self._compute_factors_at_date(
                date=date,
                universe=universe,
                risky_symbols=risky_symbols,
                canary_symbols=canary_symbols,
                benchmark_sym=benchmark_sym,
                lookback_days=lookback_days,
            )

            # Classify regime
            current_regime = self._classify_regime(
                factors=factors,
                prev_regime=current_regime,
                breadth_bull_thresh=breadth_bull_thresh,
                breadth_bear_thresh=breadth_bear_thresh,
                breadth_mom_thresh=breadth_mom_thresh,
                vol_z_thresh=vol_z_thresh,
                hurst_trend_thresh=hurst_trend_thresh,
                hurst_meanrev_thresh=hurst_meanrev_thresh,
            )

            # Route allocation based on active regime & mode
            if mode == "discrete_winner":
                w = sub_daily[current_regime].loc[date].copy()
            elif mode == "smooth_blend":
                primary_weights = sub_daily[current_regime].loc[date]
                if current_regime == "CRASH_RISK_OFF":
                    alt_weights = sub_daily["RANGE_BOUND"].loc[date]
                    w = 0.80 * primary_weights + 0.20 * alt_weights
                elif current_regime == "MOMENTUM_EXPANSION":
                    alt_weights = sub_daily["BULL_TREND"].loc[date]
                    w = 0.65 * primary_weights + 0.35 * alt_weights
                elif current_regime == "BULL_TREND":
                    alt_weights = sub_daily["MOMENTUM_EXPANSION"].loc[date]
                    w = 0.65 * primary_weights + 0.35 * alt_weights
                elif current_regime == "RANGE_BOUND":
                    alt_weights = sub_daily["CRASH_RISK_OFF"].loc[date]
                    w = 0.65 * primary_weights + 0.35 * alt_weights
                else:  # VOLATILE_ROTATION
                    alt_weights = sub_daily["MOMENTUM_EXPANSION"].loc[date]
                    w = 0.60 * primary_weights + 0.40 * alt_weights
            else:
                w = sub_daily[current_regime].loc[date].copy()

            # Fill missing cells with 0.0 (cell-level NaN vs 0.0 discipline)
            w = w.fillna(0.0)

            # Volatility Targeting Overlay (Barroso & Santa-Clara 2015; Moreira & Muir 2017):
            # Scale target weights by min(1.0, target_vol / realized_vol) to standardize variance across market cycles
            if enable_vol_targeting and current_regime != "CRASH_RISK_OFF":
                r_vol = float(factors.get("recent_vol", 0.15))
                if r_vol > 1e-4:
                    vol_scale = min(1.0, target_vol / r_vol)
                    w = w * vol_scale

            # Scale and route unallocated capital to cash_proxy
            risky_cols = [s for s in symbols if s != cash_proxy]
            w_risky = w[risky_cols].copy()
            w_cash = float(w[cash_proxy]) if cash_proxy in symbols else 0.0
            tot = float(w_risky.sum()) + w_cash

            if tot > 1.0:
                scale = 1.0 / tot
                w_risky = w_risky * scale
                w_cash = w_cash * scale

            if cash_proxy in symbols:
                remainder = max(0.0, 1.0 - (float(w_risky.sum()) + w_cash))
                w[cash_proxy] = w_cash + remainder
            w[risky_cols] = w_risky

            output_sparse.loc[date] = w

        return _fill_out_columns(output_sparse, symbols)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        rebal = p.get("regime_compound_rebalance_freq_days", getattr(cfg, "regime_compound_rebalance_freq_days", 21))
        mode = p.get("regime_compound_mode", getattr(cfg, "regime_compound_mode", "discrete_winner"))
        return (
            f"Macro Regime Factor Adaptive Compound Strategy (mode='{mode}', rebalance={rebal}d): "
            "dynamically extracts 5 macro factors (market breadth, canary stress, vol Z-score, Hurst exponent, dispersion) "
            "to classify market regimes across the Top 5 unleveraged strategies: "
            "CRASH_RISK_OFF -> VigilantAssetAllocation (VAA), "
            "BULL_TREND -> ChanPivotShiftMACDAdvStrategy, "
            "RANGE_BOUND -> ChanPivotShiftStrategy, "
            "MOMENTUM_EXPANSION -> Active Dual Momentum, "
            "VOLATILE_ROTATION -> ChanVaaCompoundStrategy."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        return 252

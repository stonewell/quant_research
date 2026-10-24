"""Wyckoff-Chan Spring Strategy (威科夫因果弹簧与中枢反转策略).

Academic Grounding:
- Richard D. Wyckoff (1931): Method of Trading and Investing in Stocks (Spring / Shakeout)
- 缠中说禅 (Chan 2006-2008): Lessons on Pivot Construction (中枢) and Support Bands (ZD)
- Tianfeng Securities Financial Engineering (2019): Turning Point Curvature Modeling

Quantitative Mechanics:
1. Causal Support S_{t-1} = ZD_{t-1} & Wyckoff Spring:
   - Identifies causal support from confirmed Chan pivot lower bound ZD (with fallback
     to 20-day rolling lowest price before pivots form).
   - Detects causal Spring: previous bar undercuts support (penetration between min_penetration
     and max_penetration), current bar reclaims back above support on expanding volume.
   - Requires scale-free turning point extremum curvature kappa >= min_curvature, whose default
     is now DEFAULT_REACHABLE_MIN_CURVATURE (0.8). The former 1.5 default was structurally
     unreachable: on a full window=5 fit |kappa| <= 2*sqrt(5/14) ~= 1.1952 because the window
     is z-scored before the quadratic is fitted (Task C5.1), so the strategy could never open a
     position. 0.8 is an INTERIM reachability default, not a walkforward-optimised threshold.
2. Stop-Loss & Two-Stage Profit Targets:
   - Protective stop-loss is anchored strictly at 0.5% below spring low (Low_{t-1}).
   - Target 1 (ZG): when price reaches pivot upper bound ZG, scale out 50% of position
     and raise stop-loss to breakeven (entry price).
   - Target 2 (ZG + 1.2 * (ZG - ZD)): full exit of remaining 50% position, writing explicit 0.0.
   - Enforces sparse weights contract: NaN on non-rebalance days, explicit 0.0 on exit.
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _sparse_from_daily,
)
from common.structural_factors import (
    DEFAULT_REACHABLE_MIN_CURVATURE,
    extremum_curvature,
    warn_unreachable_curvature_threshold,
    wyckoff_spring,
)
from .chan_advanced_strategies import (
    _extract_pivot_and_stroke_series,
    _get_risky_symbols_helper,
    _aligned_master_index_helper,
)
from .config import StrategyConfig


def run_wyckoff_spring_position_loop(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    entry_signals: np.ndarray,
    zd: np.ndarray,
    zg: np.ndarray,
    stop_buffer_pct: float = 0.005,
    max_holding_days: int = 30,
    position_size_pct: float = 1.0,
) -> np.ndarray:
    """Stateful execution loop for Wyckoff-Chan Spring strategy.

    Implements:
    - Tight stop loss below spring low
    - Target 1 (ZG): 50% scale-out and raise stop to breakeven
    - Target 2 (ZG + 1.2*(ZG-ZD)): 100% exit (0.0)
    - Timeout exit (0.0)
    """
    n = len(close)
    raw = np.zeros(n, dtype=float)

    in_position = False
    scaled_out = False
    entry_idx = 0
    entry_price = 0.0
    stop_price = 0.0
    zg_target = np.nan
    target_2 = np.nan
    current_weight = 0.0

    for i in range(n):
        p_close = close[i]
        p_high = high[i]

        if in_position:
            held = i - entry_idx

            # 1. Check stop loss & timeout
            stopped = p_close <= stop_price
            timed_out = held >= max_holding_days

            # 2. Check Target 2 exit
            target2_hit = (not np.isnan(target_2)) and (p_close >= target_2 or p_high >= target_2)

            if stopped or timed_out or target2_hit:
                in_position = False
                scaled_out = False
                current_weight = 0.0
                raw[i] = 0.0
                continue

            # 3. Check Target 1 (ZG) scale-out
            if (not scaled_out) and (not np.isnan(zg_target)) and (p_close >= zg_target):
                scaled_out = True
                current_weight = position_size_pct * 0.5
                stop_price = max(stop_price, entry_price)  # Raise stop loss to breakeven

            raw[i] = current_weight
        else:
            if entry_signals[i]:
                in_position = True
                scaled_out = False
                entry_idx = i
                entry_price = p_close
                spring_low = low[i - 1] if i > 0 else low[i]
                stop_price = spring_low * (1.0 - stop_buffer_pct)

                curr_zg = zg[i]
                curr_zd = zd[i]
                if not np.isnan(curr_zg) and curr_zg > entry_price:
                    zg_target = curr_zg
                else:
                    zg_target = entry_price * 1.05

                if not np.isnan(curr_zg) and not np.isnan(curr_zd) and curr_zg > curr_zd:
                    target_2 = curr_zg + 1.2 * (curr_zg - curr_zd)
                else:
                    target_2 = zg_target * 1.10

                current_weight = position_size_pct
                raw[i] = current_weight
            else:
                raw[i] = 0.0

    return raw


class WyckoffChanSpringStrategy(AllocationTemplate):
    """Wyckoff-Chan Spring Strategy (威科夫因果弹簧与中枢反转策略):

    Synthesizes causal support ZD with Wyckoff Spring undercut-reclaim mechanics,
    high positive curvature V-bottom confirmation, and institutional two-stage
    profit taking.
    """

    # Task C5.1: 1.0.0 -> 1.1.0 -- default min-curvature gate lowered from the
    # structurally unreachable 1.5 to DEFAULT_REACHABLE_MIN_CURVATURE (0.8) and
    # the dead-gate warning added at threshold-resolution time.
    version: str = "1.1.0"

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="wyckoff_chan_spring",
            factor_tags=["mean_reversion", "regime_trend_strength"],
            param_grid={},
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        min_gap_bars = int(p.get("chan_vp_min_gap_bars", getattr(cfg, "chan_vp_min_gap_bars", 4)))
        min_strokes = int(p.get("chan_vp_min_strokes", getattr(cfg, "chan_vp_min_strokes", 3)))
        max_penetration = float(p.get("wyckoff_spring_max_penetration", getattr(cfg, "wyckoff_spring_max_penetration", 0.04)))
        min_penetration = float(p.get("wyckoff_spring_min_penetration", getattr(cfg, "wyckoff_spring_min_penetration", 0.005)))
        min_curvature = float(p.get("wyckoff_spring_min_curvature", getattr(cfg, "wyckoff_spring_min_curvature", DEFAULT_REACHABLE_MIN_CURVATURE)))
        # Task C5.1: the analytic full-window ceiling for window=5 is ~1.1952, so a
        # resolved threshold at/above it (e.g. a legacy 1.5 config override) is a dead
        # gate. Warn rather than raise so old configs still run -- but visibly.
        warn_unreachable_curvature_threshold(
            min_curvature, window=5, context="wyckoff_chan_spring.wyckoff_spring_min_curvature"
        )
        min_rel_vol = float(p.get("wyckoff_spring_min_rel_vol", getattr(cfg, "wyckoff_spring_min_rel_vol", 1.0)))
        stop_buffer_pct = float(p.get("wyckoff_spring_stop_buffer_pct", getattr(cfg, "wyckoff_spring_stop_buffer_pct", 0.005)))
        max_holding_days = int(p.get("wyckoff_spring_max_holding_days", getattr(cfg, "wyckoff_spring_max_holding_days", 30)))
        position_size_pct = float(p.get("wyckoff_spring_position_size_pct", getattr(cfg, "wyckoff_spring_position_size_pct", 1.0)))

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols_helper(
            universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy
        )
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index_helper(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            n_bars = len(bars)
            if n_bars < 20:
                raw_weights[sym] = pd.Series(0.0, index=master_index)
                continue

            # 1. Causal Pivot extraction with fallback to rolling 20d min
            piv_df = _extract_pivot_and_stroke_series(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
            zd_s = piv_df["zd"]
            zg_s = piv_df["zg"]

            low_s = bars["Low"] if "Low" in bars.columns else bars["Close"]
            fallback_support = low_s.rolling(20, min_periods=5).min()
            support_s = zd_s.fillna(fallback_support)

            # 2. Wyckoff Spring relative volume & curvature computation
            spring_score = wyckoff_spring(
                bars,
                support_series=support_s,
                max_penetration=max_penetration,
                min_penetration=min_penetration,
                volume_lookback=20,
            )
            curv = extremum_curvature(bars["Close"], window=5, min_periods=3)

            # 3. Entry condition: Spring confirmed with sufficient volume & positive curvature
            entry_cond = (spring_score >= min_rel_vol) & (curv >= min_curvature)

            # Align arrays
            close_aligned = bars["Close"].reindex(master_index).to_numpy(dtype=float)
            high_aligned = (
                bars["High"].reindex(master_index).to_numpy(dtype=float)
                if "High" in bars.columns
                else close_aligned
            )
            low_aligned = low_s.reindex(master_index).to_numpy(dtype=float)
            entry_aligned = entry_cond.reindex(master_index).fillna(False).to_numpy(dtype=bool)
            zd_aligned = zd_s.reindex(master_index).to_numpy(dtype=float)
            zg_aligned = zg_s.reindex(master_index).to_numpy(dtype=float)

            raw_w = run_wyckoff_spring_position_loop(
                close=close_aligned,
                high=high_aligned,
                low=low_aligned,
                entry_signals=entry_aligned,
                zd=zd_aligned,
                zg=zg_aligned,
                stop_buffer_pct=stop_buffer_pct,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
            )
            raw_weights[sym] = raw_w

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        min_curvature = float(
            p.get(
                "wyckoff_spring_min_curvature",
                getattr(cfg, "wyckoff_spring_min_curvature", DEFAULT_REACHABLE_MIN_CURVATURE),
            )
        )
        return (
            "Wyckoff-Chan Spring Strategy (威科夫因果弹簧与中枢反转): "
            "enters on causal support undercut and reclaim (Wyckoff Spring) confirmed by "
            f"scale-free extremum curvature kappa >= {min_curvature:g} "
            "(Task C5.1: default 0.8 is an interim reachability value; the former 1.5 was "
            "above the analytic window-5 ceiling of ~1.1952 and could never trigger); "
            "stops out below spring low; "
            "scales out 50% at Zhongshu upper bound ZG and moves stop to breakeven; "
            "fully exits at Target 2 (ZG + 1.2*(ZG-ZD)) or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = int(p.get("chan_vp_min_gap_bars", getattr(cfg, "chan_vp_min_gap_bars", 4)))
        min_strokes = int(p.get("chan_vp_min_strokes", getattr(cfg, "chan_vp_min_strokes", 3)))
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(structural, 40)

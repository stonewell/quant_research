"""Chan Volume Profile Hybrid Strategy (缠论筹码HVN中枢与三买真空遍历策略).

Academic Grounding:
- 缠中说禅 (Chan 2006-2008): Lessons on Pivot Construction (中枢) and Third Buy Points (三买)
- Steidlmayer (1986), Dalrymple (2004): Market Profile & Volume Profile (POC, Value Area VAH/VAL)
- Wyckoff (1931): Law of Effort vs. Result traversing liquidity vacuum (LVN)

Quantitative Mechanics:
1. HVN Pivot Validation:
   - Identifies causal Chan pivots [ZD, ZG] via stroke/fractal extraction (used for the
     structural breakout/retest and stop levels).
   - Computes rolling Volume Profile (POC, VAL, VAH) and consolidation volume density.
   - Confirms an HVN only when the SAME-WINDOW value area density >= min_density
     (default 0.45) and POC resides within [VAL, VAH], filtering out hollow false pivots.
     Task C5.2: this used to test POC against the already-confirmed (lagged) pivot
     [ZD, ZG]; the 60d POC and stale ZD/ZG had zero overlap on the synthetic universe,
     so hvn_valid was always False and the strategy never traded.
2. SanMai (Third Buy) Value Acceptance & Vacuum Traverse:
   - Gated on Third Buy (B3) breakout retest holding above ZG and VAH.
   - Confirms high push efficiency via Wyckoff Effort-vs-Result (effort_result >= 0.80).
3. Dynamic Naked POC (NPOC) Take-Profit & Divergence Exit:
   - Monitors overhead historically un-tested POC levels (NPOC) across the liquidity vacuum.
   - Takes profit when price reaches overhead NPOC with momentum divergence (div < -0.5),
     or when standardized top divergence div < -1.0 while in profit.
   - Enforces structural stop-loss below ZD and percentage stop-loss below entry.
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
from common.indicators import macd
from common.structural_factors import (
    compute_rolling_poc_series,
    consolidation_density,
    wyckoff_effort_result,
    standardized_divergence,
)
from .chan_advanced_strategies import (
    _extract_pivot_and_stroke_series,
    _get_risky_symbols_helper,
    _aligned_master_index_helper,
)
from .chan_signals import compute_chan3_signals
from .config import StrategyConfig


def run_chan_vp_position_loop(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    poc: np.ndarray,
    zd: np.ndarray,
    zg: np.ndarray,
    entry_signals: np.ndarray,
    sell_signals: np.ndarray,
    std_div: np.ndarray,
    stop_loss_pct: float = 0.06,
    max_holding_days: int = 60,
    position_size_pct: float = 1.0,
    vp_lookback: int = 60,
) -> np.ndarray:
    """Stateful execution loop for Chan-Volume Profile Hybrid strategy.

    Tracks entry state, overhead un-tested POCs (NPOC), divergence exits,
    stop losses, and timeout exits.
    """
    n = len(close)
    raw = np.zeros(n, dtype=float)
    in_position = False
    entry_idx = 0
    entry_price = 0.0
    npoc_target = np.nan

    for i in range(n):
        p_close = close[i]
        p_high = high[i]
        curr_zd = zd[i]

        if in_position:
            held = i - entry_idx
            ret = (p_close / entry_price - 1.0) if entry_price > 0 else 0.0
            high_ret = (p_high / entry_price - 1.0) if entry_price > 0 else 0.0

            # 1. Stop loss check
            stopped_pct = ret <= -stop_loss_pct
            stopped_zd = (not np.isnan(curr_zd)) and (p_close < curr_zd * 0.98)
            stopped = stopped_pct or stopped_zd

            # 2. Timeout check
            timed_out = held >= max_holding_days

            # 3. Chan sell point check
            chan_sell = bool(sell_signals[i])

            # 4. NPOC target hit + divergence or standardized divergence exit
            div_val = std_div[i]
            npoc_hit = (
                not np.isnan(npoc_target)
                and p_high >= npoc_target
                and high_ret > 0.02
                and (not np.isnan(div_val))
                and div_val < -0.5
            )
            div_exit = (
                ret > 0.0
                and (not np.isnan(div_val))
                and div_val < -1.0
            )

            if stopped or timed_out or chan_sell or npoc_hit or div_exit:
                in_position = False
                raw[i] = 0.0
                npoc_target = np.nan
            else:
                raw[i] = position_size_pct
        else:
            if entry_signals[i]:
                in_position = True
                entry_idx = i
                entry_price = p_close
                raw[i] = position_size_pct

                # Identify overhead Naked POC (NPOC)
                # Search historical POCs from [i - vp_lookback, i) that formed above entry_price * 1.02
                # and have not yet been touched by subsequent High prices up to bar i
                npoc_cand = []
                lookback_start = max(0, i - vp_lookback)
                for k in range(lookback_start, i):
                    pk = poc[k]
                    if not np.isnan(pk) and pk > entry_price * 1.02:
                        touched = False
                        if k + 1 < i:
                            if np.max(high[k + 1 : i]) >= pk:
                                touched = True
                        if not touched:
                            npoc_cand.append(pk)
                if npoc_cand:
                    npoc_target = float(np.min(npoc_cand))
                else:
                    npoc_target = np.nan
            else:
                raw[i] = 0.0

    return raw


class ChanVolumeProfileHybridStrategy(AllocationTemplate):
    """Chan Volume Profile Hybrid Strategy (缠论筹码HVN中枢与三买真空遍历策略):

    Combines causal Chan structure (strokes, pivots, B3 signals) with Volume Profile
    distribution metrics and Wyckoff effort-vs-result to achieve high-conviction
    breakout retest entries across liquidity vacuums.
    """

    # Task C5.2: 1.0.0 -> 1.1.0 -- HVN validation re-based from the stale
    # confirmed pivot [ZD, ZG] onto the same-window value area [VAL, VAH].
    version: str = "1.1.0"

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(
            name="chan_vp_hybrid",
            factor_tags=["regime_trend_strength", "relative_momentum", "absolute_momentum_trend"],
            param_grid={},
        )

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        vp_lookback = int(p.get("chan_vp_lookback", getattr(cfg, "chan_vp_lookback", 60)))
        n_bins = int(p.get("chan_vp_n_bins", getattr(cfg, "chan_vp_n_bins", 30)))
        val_pct = float(p.get("chan_vp_val_pct", getattr(cfg, "chan_vp_val_pct", 0.70)))
        min_density = float(p.get("chan_vp_min_consolidation_density", getattr(cfg, "chan_vp_min_consolidation_density", 0.45)))
        min_gap_bars = int(p.get("chan_vp_min_gap_bars", getattr(cfg, "chan_vp_min_gap_bars", 4)))
        min_strokes = int(p.get("chan_vp_min_strokes", getattr(cfg, "chan_vp_min_strokes", 3)))
        stop_loss_pct = float(p.get("chan_vp_stop_loss_pct", getattr(cfg, "chan_vp_stop_loss_pct", 0.06)))
        max_holding_days = int(p.get("chan_vp_max_holding_days", getattr(cfg, "chan_vp_max_holding_days", 60)))
        position_size_pct = float(p.get("chan_vp_position_size_pct", getattr(cfg, "chan_vp_position_size_pct", 1.0)))

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
            if n_bars < min(vp_lookback, 20):
                raw_weights[sym] = pd.Series(0.0, index=master_index)
                continue

            # 1. Causal Chan Pivot & Stroke extraction
            piv_df = _extract_pivot_and_stroke_series(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
            zd_s = piv_df["zd"]
            zg_s = piv_df["zg"]

            # 2. Rolling Volume Profile (POC, VAL, VAH)
            poc_arr, val_arr, vah_arr = compute_rolling_poc_series(
                bars, lookback=vp_lookback, n_bins=n_bins, value_area_pct=val_pct
            )
            poc_s = pd.Series(poc_arr, index=bars.index)
            val_s = pd.Series(val_arr, index=bars.index)
            vah_s = pd.Series(vah_arr, index=bars.index)

            # 3. Consolidation Density & HVN validation
            # Task C5.2: measure density against the SAME-WINDOW value area
            # [VAL, VAH] that the rolling profile just produced. Passing the
            # confirmed pivot [ZD, ZG] here was the bug: a Chan pivot is only
            # known a stroke/segment late, so on synthetic data the 60d POC had
            # exactly zero overlap with the stale band, hvn_valid was always
            # False, and the strategy could never open a position. [ZD, ZG]
            # remain structural inputs further below (breakout/retest vs ZG,
            # stop vs ZD, hvn_recent freshness vs ZG) and are untouched.
            cd_s = consolidation_density(bars, zd=val_s, zg=vah_s, window=20)
            # NOTE: POC in [VAL, VAH] holds BY CONSTRUCTION of
            # compute_rolling_poc_series (the value area is grown outward from
            # the POC bin), so the POC clause is a cheap sanity/integrity check,
            # not an independent filter -- the effective gate here is the
            # same-window value-area density >= min_density.
            hvn_instant = (cd_s >= min_density) & (poc_s >= val_s) & (poc_s <= vah_s) & (vah_s > val_s)
            hvn_recent = hvn_instant.rolling(vp_lookback, min_periods=1).max().astype(bool)
            hvn_valid = hvn_instant | (hvn_recent & (bars["Close"] >= zg_s * 0.99))

            # 4. Third Buy & Effort-vs-Result Breakout Retest
            sig = compute_chan3_signals(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes, causal=True)
            third_buy = sig["third_buy"].fillna(False)
            sell_sig = sig["sell_signal"].fillna(False)

            er_s = wyckoff_effort_result(bars, lag=5, volume_lookback=20)
            breakout_retest = (
                (bars["Close"] >= zg_s * 0.99)
                & (bars["Close"] > poc_s)
                & (er_s >= 0.80)
                & ((bars["Close"] >= vah_s * 0.98) | vah_s.isna())
            )
            entry_cond = (third_buy | breakout_retest) & hvn_valid

            # Standardized MACD Divergence for profit taking / momentum exhaustion
            macd_df = macd(bars["Close"], 12, 26, 9)
            macd_hist = macd_df["hist"]
            std_div = standardized_divergence(bars["Close"], macd_hist, lag=5, vol_lookback=20)

            # Align series to master_index
            close_aligned = bars["Close"].reindex(master_index).to_numpy(dtype=float)
            high_aligned = (
                bars["High"].reindex(master_index).to_numpy(dtype=float)
                if "High" in bars.columns
                else close_aligned
            )
            low_aligned = (
                bars["Low"].reindex(master_index).to_numpy(dtype=float)
                if "Low" in bars.columns
                else close_aligned
            )
            poc_aligned = poc_s.reindex(master_index).to_numpy(dtype=float)
            zd_aligned = zd_s.reindex(master_index).to_numpy(dtype=float)
            zg_aligned = zg_s.reindex(master_index).to_numpy(dtype=float)
            entry_aligned = entry_cond.reindex(master_index).fillna(False).to_numpy(dtype=bool)
            sell_aligned = sell_sig.reindex(master_index).fillna(False).to_numpy(dtype=bool)
            std_div_aligned = std_div.reindex(master_index).to_numpy(dtype=float)

            raw_w = run_chan_vp_position_loop(
                close=close_aligned,
                high=high_aligned,
                low=low_aligned,
                poc=poc_aligned,
                zd=zd_aligned,
                zg=zg_aligned,
                entry_signals=entry_aligned,
                sell_signals=sell_aligned,
                std_div=std_div_aligned,
                stop_loss_pct=stop_loss_pct,
                max_holding_days=max_holding_days,
                position_size_pct=position_size_pct,
                vp_lookback=vp_lookback,
            )
            raw_weights[sym] = raw_w

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)
        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        return (
            "Chan Volume Profile Hybrid Strategy (缠论筹码HVN中枢与三买真空遍历): "
            "identifies high-volume-node (HVN) validated Chan pivots using rolling Volume Profile "
            "and consolidation density; enters on confirmed Third Buy (B3) breakout retests traversing "
            "the liquidity vacuum; takes profit at overhead Naked POC (NPOC) levels or momentum divergence, "
            "with structural stop-loss and time-based exits."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        lookback = p.get("chan_vp_lookback", getattr(cfg, "chan_vp_lookback", 60))
        min_gap_bars = p.get("chan_vp_min_gap_bars", getattr(cfg, "chan_vp_min_gap_bars", 4))
        min_strokes = p.get("chan_vp_min_strokes", getattr(cfg, "chan_vp_min_strokes", 3))
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        return max(lookback, structural, 60)

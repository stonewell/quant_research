"""Tactical Asset Allocation (TAA) Strategies: HAA and DAA.

1. Hybrid Asset Allocation (HAA):
   Wouter J. Keller & Jan Willem Keuning (2023, SSRN #4346906).
   A streamlined dual-momentum model with a single canary asset (TIP).
   Switches between 100% offensive allocation (top 4 of 8 global assets)
   and 100% defensive allocation (IEF vs BIL) based on TIP's 13612W momentum.
   Includes Keller's dual-momentum crash diversion for offensive assets with negative momentum.

2. Defensive Asset Allocation (DAA):
   Wouter J. Keller & Jan Willem Keuning (2018, SSRN #3212862).
   A multi-tier crash protection model using dual canary assets (VWO and BND).
   Dynamically scales Cash Fraction (0%, 50%, 100%) based on canary momentum breadth,
   allocating across 12 risky assets and 3 defensive assets (IEF, LQD, BIL).
"""

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _fill_out_columns,
)
from common.indicators import roc
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .config import (
    DEFAULT_CASH_PROXY,
    DEFAULT_DAA_CANARY,
    DEFAULT_DAA_DEFENSIVE,
    DEFAULT_DAA_RISKY,
    DEFAULT_HAA_DEFENSIVE,
    DEFAULT_HAA_OFFENSIVE,
    StrategyConfig,
)


def _aligned_master_index_helper(universe: Dict[str, pd.DataFrame], symbols: List[str]) -> pd.DatetimeIndex:
    from .strategy import _aligned_master_index
    return _aligned_master_index(universe, symbols)


def score_13612w(close: pd.Series) -> pd.Series:
    """Computes Keller & Keuning's 13612W momentum score:
    12 * r_1m + 4 * r_3m + 2 * r_6m + 1 * r_12m
    using 21, 63, 126, and 252 trading day returns.
    """
    return 12 * roc(close, 21) + 4 * roc(close, 63) + 2 * roc(close, 126) + roc(close, 252)


class HybridAssetAllocationStrategy(AllocationTemplate):
    """Hybrid Asset Allocation (HAA, Keller & Keuning 2023).

    Uses TIP (TIPS bond ETF) as a single canary asset to toggle between offensive and defensive regimes:
    - If TIP 13612W momentum > 0 (Offensive Mode):
      Ranks offensive assets by 13612W score and selects the top K (default 4).
      Each slot receives 1/K (25%). If an individual selected asset has 13612W score <= 0,
      its 25% allocation is diverted to the best defensive asset (IEF vs BIL).
    - If TIP 13612W momentum <= 0 (Defensive Mode):
      100% is allocated to the best defensive asset (IEF vs BIL).
      If both defensive assets have momentum <= 0, capital is placed in cash_proxy (BIL).
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="hybrid_asset_allocation", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("haa_rebalance_freq_days", cfg.haa_rebalance_freq_days)
        canary_sym = p.get("haa_canary_symbol", cfg.haa_canary_symbol)
        top_k = p.get("haa_top_k", cfg.haa_top_k)
        off_univ = p.get("haa_offensive_universe", cfg.haa_offensive_universe)
        def_univ = p.get("haa_defensive_universe", cfg.haa_defensive_universe)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        offensive_symbols = [s for s in off_univ if s in symbols]
        defensive_symbols = [s for s in def_univ if s in symbols]
        all_tracked = list(dict.fromkeys(
            ([canary_sym] if canary_sym in symbols else [])
            + offensive_symbols
            + defensive_symbols
            + ([cash_proxy] if cash_proxy in symbols else [])
        ))

        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # Precompute 13612W scores for all tracked assets
        scores_dict = {}
        for sym in all_tracked:
            scores_dict[sym] = score_13612w(universe[sym]["Close"])
        scores_df = pd.DataFrame(scores_dict, index=master_index)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            if date not in scores_df.index:
                continue

            date_scores = scores_df.loc[date]

            # Best defensive asset on this date
            best_def = cash_proxy if cash_proxy in symbols else (defensive_symbols[0] if defensive_symbols else None)
            valid_def_scores = date_scores[defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
            if not valid_def_scores.empty:
                candidate_def = valid_def_scores.idxmax()
                # Keller rule: only hold defensive asset if its score > 0, else cash_proxy
                if valid_def_scores[candidate_def] > 0:
                    best_def = candidate_def
                elif cash_proxy in symbols:
                    best_def = cash_proxy
                else:
                    best_def = candidate_def

            # Check canary status
            canary_score = date_scores.get(canary_sym, np.nan)
            canary_bullish = pd.notna(canary_score) and canary_score > 0

            # If canary is not in universe or NaN, evaluate based on offensive universe breadth
            if pd.isna(canary_score) and offensive_symbols:
                valid_off = date_scores[offensive_symbols].dropna()
                canary_bullish = len(valid_off) > 0 and (valid_off > 0).mean() >= 0.5

            if canary_bullish and offensive_symbols:
                # Offensive mode: pick top K offensive assets
                valid_off = date_scores[offensive_symbols].dropna()
                if not valid_off.empty:
                    ranked_off = valid_off.sort_values(ascending=False)
                    k = min(top_k, len(ranked_off))
                    selected_off = ranked_off.iloc[:k]
                    slot_weight = 1.0 / k

                    for sym, sc in selected_off.items():
                        if sc > 0:
                            weights_rebal.loc[date, sym] += slot_weight
                        else:
                            # Dual-momentum crash diversion: divert to best defensive asset
                            if best_def and best_def in symbols:
                                weights_rebal.loc[date, best_def] += slot_weight
                else:
                    if best_def and best_def in symbols:
                        weights_rebal.loc[date, best_def] = 1.0
            else:
                # Defensive mode: 100% to best defensive asset
                if best_def and best_def in symbols:
                    weights_rebal.loc[date, best_def] = 1.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return _fill_out_columns(weights_df, symbols)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        canary = p.get("haa_canary_symbol", cfg.haa_canary_symbol)
        top_k = p.get("haa_top_k", cfg.haa_top_k)
        rebal = p.get("haa_rebalance_freq_days", cfg.haa_rebalance_freq_days)
        return (
            f"Hybrid Asset Allocation (HAA, Keller & Keuning 2023): rebalances every {rebal} days. "
            f"Uses canary asset {canary} 13612W momentum to toggle regimes. "
            f"In bull mode ({canary} > 0), holds top {top_k} offensive assets (diverting any with score <= 0 to IEF/BIL). "
            f"In bear mode ({canary} <= 0), shifts 100% to leading defensive asset (IEF vs BIL)."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        return 252


class DefensiveAssetAllocationStrategy(AllocationTemplate):
    """Defensive Asset Allocation (DAA, Keller & Keuning 2018).

    Uses dual canary assets (VWO and BND) to implement graduated crash protection:
    - Counts bad canary assets (13612W score <= 0):
      - 0 bad canaries -> Cash Fraction = 0%: 100% in top 6 risky assets (16.7% each).
      - 1 bad canary   -> Cash Fraction = 50%: 50% in top 3 risky assets, 50% in best defensive asset.
      - 2 bad canaries -> Cash Fraction = 100%: 100% in best defensive asset.
    - If any selected risky asset has 13612W score <= 0, its allocation diverts to the best defensive asset.
    - Defensive universe: IEF, LQD, BIL. If all defensive scores <= 0, routes to cash_proxy (BIL).
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name="defensive_asset_allocation", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("daa_rebalance_freq_days", cfg.daa_rebalance_freq_days)
        canary_univ = p.get("daa_canary_universe", cfg.daa_canary_universe)
        top_k = p.get("daa_top_k", cfg.daa_top_k)
        risky_univ = p.get("daa_risky_universe", cfg.daa_risky_universe)
        def_univ = p.get("daa_defensive_universe", cfg.daa_defensive_universe)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        canary_symbols = [s for s in canary_univ if s in symbols]
        risky_symbols = [s for s in risky_univ if s in symbols]
        defensive_symbols = [s for s in def_univ if s in symbols]
        all_tracked = list(dict.fromkeys(
            canary_symbols
            + risky_symbols
            + defensive_symbols
            + ([cash_proxy] if cash_proxy in symbols else [])
        ))

        master_index = _aligned_master_index_helper(universe, all_tracked if all_tracked else symbols)
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # Precompute 13612W scores
        scores_dict = {}
        for sym in all_tracked:
            scores_dict[sym] = score_13612w(universe[sym]["Close"])
        scores_df = pd.DataFrame(scores_dict, index=master_index)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            if date not in scores_df.index:
                continue

            date_scores = scores_df.loc[date]

            # Best defensive asset
            best_def = cash_proxy if cash_proxy in symbols else (defensive_symbols[0] if defensive_symbols else None)
            valid_def_scores = date_scores[defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
            if not valid_def_scores.empty:
                candidate_def = valid_def_scores.idxmax()
                if valid_def_scores[candidate_def] > 0:
                    best_def = candidate_def
                elif cash_proxy in symbols:
                    best_def = cash_proxy
                else:
                    best_def = candidate_def

            # Count canary signals with momentum <= 0
            if canary_symbols:
                canary_scores = date_scores[canary_symbols].dropna()
                bad_canaries = int((canary_scores <= 0).sum())
                # If some canaries are missing due to NaN, scale bad count proportionally
                if len(canary_scores) < len(canary_symbols) and len(canary_scores) > 0:
                    bad_ratio = bad_canaries / len(canary_scores)
                    bad_canaries = int(round(bad_ratio * len(canary_symbols)))
            else:
                # If no canaries are in universe, use risky breadth
                valid_risky = date_scores[risky_symbols].dropna() if risky_symbols else pd.Series(dtype=float)
                if not valid_risky.empty:
                    pos_pct = (valid_risky > 0).mean()
                    if pos_pct >= 0.7:
                        bad_canaries = 0
                    elif pos_pct >= 0.4:
                        bad_canaries = 1
                    else:
                        bad_canaries = 2
                else:
                    bad_canaries = 2

            # Determine Cash Fraction (CF)
            if bad_canaries == 0:
                cf = 0.0
                risky_share = 1.0
                k = min(top_k, len(risky_symbols)) if risky_symbols else 0
            elif bad_canaries == 1:
                cf = 0.5
                risky_share = 0.5
                k = min(max(1, top_k // 2), len(risky_symbols)) if risky_symbols else 0
            else:  # bad_canaries >= 2
                cf = 1.0
                risky_share = 0.0
                k = 0

            # Allocate cash fraction to best defensive asset
            if cf > 0 and best_def and best_def in symbols:
                weights_rebal.loc[date, best_def] += cf

            # Allocate risky share across top K risky assets
            if risky_share > 0 and k > 0 and risky_symbols:
                valid_risky = date_scores[risky_symbols].dropna()
                if not valid_risky.empty:
                    ranked_risky = valid_risky.sort_values(ascending=False)
                    selected_risky = ranked_risky.iloc[:k]
                    slot_weight = risky_share / k

                    for sym, sc in selected_risky.items():
                        if sc > 0:
                            weights_rebal.loc[date, sym] += slot_weight
                        else:
                            # Negative momentum risky asset diverts to best defensive asset
                            if best_def and best_def in symbols:
                                weights_rebal.loc[date, best_def] += slot_weight
                else:
                    if best_def and best_def in symbols:
                        weights_rebal.loc[date, best_def] += risky_share
            elif risky_share > 0:
                if best_def and best_def in symbols:
                    weights_rebal.loc[date, best_def] += risky_share

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return _fill_out_columns(weights_df, symbols)

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        canaries = p.get("daa_canary_universe", cfg.daa_canary_universe)
        top_k = p.get("daa_top_k", cfg.daa_top_k)
        rebal = p.get("daa_rebalance_freq_days", cfg.daa_rebalance_freq_days)
        canary_str = ", ".join(canaries)
        return (
            f"Defensive Asset Allocation (DAA, Keller & Keuning 2018): rebalances every {rebal} days. "
            f"Monitors dual canaries ({canary_str}) via 13612W momentum for graduated crash defense. "
            f"0 bad canaries -> 100% in top {top_k} risky assets. "
            f"1 bad canary -> 50% in top {max(1, top_k // 2)} risky / 50% defensive. "
            f"2 bad canaries -> 100% defensive (IEF, LQD, BIL)."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        return 252

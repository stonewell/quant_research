"""Tactical Asset Allocation (TAA) Strategies: VAA, DAA, and HAA.

Unified Keller & Keuning TAA family sharing the 13612W momentum and canary defense engine:
1. Vigilant Asset Allocation (VAA-G4):
   Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624).
   Uses offensive universe breadth (all 4 assets > 0) to rotate into top offensive asset,
   otherwise 100% into top defensive asset.

2. Defensive Asset Allocation (DAA):
   Wouter J. Keller & Jan Willem Keuning (2018, SSRN #3212862).
   A multi-tier crash protection model using dual canary assets (VWO and BND).
   Dynamically scales Cash Fraction (0%, 50%, 100%) based on canary momentum breadth,
   allocating across 12 risky assets and 3 defensive assets (IEF, LQD, BIL).

3. Hybrid Asset Allocation (HAA):
   Wouter J. Keller & Jan Willem Keuning (2023, SSRN #4346906).
   A streamlined dual-momentum model with a single canary asset (TIP).
   Switches between 100% offensive allocation (top 4 of 8 global assets)
   and 100% defensive allocation (IEF vs BIL) based on TIP's 13612W momentum.
   Includes Keller's dual-momentum crash diversion for offensive assets with negative momentum.
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


class CanaryAssetAllocationBase(AllocationTemplate):
    """Base class for Keller & Keuning's Tactical Asset Allocation (TAA) strategies:
    VAA (2017), DAA (2018), and HAA (2023).

    Shared mechanisms:
    1. 13612W momentum scoring (12*r_1m + 4*r_3m + 2*r_6m + 1*r_12m).
    2. Synchronized master date indexing and periodic rebalance scheduling.
    3. Defensive asset selection (picking the highest scoring positive defensive asset,
       or defaulting to cash proxy).
    4. Dual-momentum crash diversion: diverting slots with non-positive scores to safe assets.
    """

    def __init__(self, name: str, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        super().__init__(name=name, param_grid={})

    def _compute_13612w_scores(
        self,
        universe: Dict[str, pd.DataFrame],
        tracked_symbols: List[str],
        master_index: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        scores_dict = {}
        for sym in tracked_symbols:
            if sym in universe and "Close" in universe[sym].columns:
                scores_dict[sym] = score_13612w(universe[sym]["Close"]).reindex(master_index)
        return pd.DataFrame(scores_dict, index=master_index)

    def _select_best_defensive(
        self,
        date_scores: pd.Series,
        defensive_symbols: List[str],
        cash_proxy: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        require_positive: bool = True,
    ) -> Optional[str]:
        all_syms = symbols if symbols is not None else list(date_scores.index)
        best_def = cash_proxy if cash_proxy and cash_proxy in all_syms else (defensive_symbols[0] if defensive_symbols else None)
        valid_def = date_scores[defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
        if not valid_def.empty:
            candidate = max(valid_def.index, key=lambda s: (valid_def[s], s))
            if not require_positive or valid_def[candidate] > 0:
                best_def = candidate
            elif cash_proxy and cash_proxy in all_syms:
                best_def = cash_proxy
            else:
                best_def = candidate
        return best_def

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        return 252


class VigilantAssetAllocation(CanaryAssetAllocationBase):
    """Vigilant Asset Allocation (VAA-G4).

    Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624).
    Uses offensive universe breadth (all assets in offensive universe must have 13612W > 0)
    to hold 100% in the single highest-scoring offensive asset; otherwise rotates 100%
    into the single highest-scoring defensive asset.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        super().__init__(name="vigilant_asset_allocation", config=config)

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: Optional[dict] = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        offensive_universe = p.get("vaa_offensive_universe", cfg.vaa_offensive_universe)
        defensive_universe = p.get("vaa_defensive_universe", cfg.vaa_defensive_universe)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        offensive_symbols = [s for s in offensive_universe if s in symbols]
        defensive_symbols = [s for s in defensive_universe if s in symbols]
        all_tracked = list(dict.fromkeys(offensive_symbols + defensive_symbols))

        scores = self._compute_13612w_scores(universe, all_tracked, master_index)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            off_scores = scores.loc[date, offensive_symbols].dropna() if offensive_symbols else pd.Series(dtype=float)
            if len(off_scores) < len(offensive_symbols):
                continue

            if not off_scores.empty and (off_scores > 0).all():
                best_off = max(off_scores.index, key=lambda s: (off_scores[s], s))
                weights_rebal.loc[date, best_off] = 1.0
            else:
                def_scores = scores.loc[date, defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
                if not def_scores.empty:
                    best_def = max(def_scores.index, key=lambda s: (def_scores[s], s))
                    weights_rebal.loc[date, best_def] = 1.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: Optional[dict] = None) -> str:
        cfg = self.config
        p = params or {}
        offensive_universe = p.get("vaa_offensive_universe", cfg.vaa_offensive_universe)
        defensive_universe = p.get("vaa_defensive_universe", cfg.vaa_defensive_universe)
        return (
            f"Vigilant Asset Allocation -- VAA-G4 (Keller & Keuning 2017): Rebalances every "
            f"{p.get('rebalance_freq_days', cfg.rebalance_freq_days)} days. "
            f"Reasoning: Scores each asset via the 13612W formula (a 12/4/2/1-weighted blend of "
            f"1/3/6/12-month returns). If every offensive asset ({', '.join(offensive_universe)}) scores "
            f"positive, holds 100% of the single highest-scoring one. Otherwise rotates fully into the "
            f"single highest-scoring defensive asset ({', '.join(defensive_universe)}). Fully concentrated, "
            f"no diversification within the chosen sleeve. NOTE: offensive/defensive tickers here are "
            f"illustrative, not a verified reproduction of the original paper's universe (see class docstring)."
        )


class HybridAssetAllocationStrategy(CanaryAssetAllocationBase):
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
        super().__init__(name="hybrid_asset_allocation", config=config)

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

        scores_df = self._compute_13612w_scores(universe, all_tracked, master_index)
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            if date not in scores_df.index:
                continue

            date_scores = scores_df.loc[date]
            best_def = self._select_best_defensive(
                date_scores, defensive_symbols, cash_proxy=cash_proxy, symbols=symbols, require_positive=True
            )

            # Check canary status
            canary_score = date_scores.get(canary_sym, np.nan)
            canary_bullish = pd.notna(canary_score) and canary_score > 0

            # If canary is not in universe or NaN, evaluate based on offensive universe breadth
            if pd.isna(canary_score) and offensive_symbols:
                valid_off = date_scores[offensive_symbols].dropna()
                canary_bullish = len(valid_off) > 0 and (valid_off > 0).mean() >= 0.5

            if canary_bullish and offensive_symbols:
                valid_off = date_scores[offensive_symbols].dropna()
                if not valid_off.empty:
                    ranked_off = sorted(valid_off.items(), key=lambda x: (-x[1], x[0]))
                    k = min(top_k, len(ranked_off))
                    selected_off = ranked_off[:k]
                    slot_weight = 1.0 / k

                    for sym, sc in selected_off:
                        if sc > 0:
                            weights_rebal.loc[date, sym] += slot_weight
                        else:
                            # Dual-momentum crash diversion
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


class DefensiveAssetAllocationStrategy(CanaryAssetAllocationBase):
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
        super().__init__(name="defensive_asset_allocation", config=config)

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

        scores_df = self._compute_13612w_scores(universe, all_tracked, master_index)
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            if date not in scores_df.index:
                continue

            date_scores = scores_df.loc[date]
            best_def = self._select_best_defensive(
                date_scores, defensive_symbols, cash_proxy=cash_proxy, symbols=symbols, require_positive=True
            )

            # Count canary signals with momentum <= 0
            if canary_symbols:
                canary_scores = date_scores[canary_symbols].dropna()
                bad_canaries = int((canary_scores <= 0).sum())
                if len(canary_scores) < len(canary_symbols) and len(canary_scores) > 0:
                    bad_ratio = bad_canaries / len(canary_scores)
                    bad_canaries = int(round(bad_ratio * len(canary_symbols)))
            else:
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
                    ranked_risky = sorted(valid_risky.items(), key=lambda x: (-x[1], x[0]))
                    selected_risky = ranked_risky[:k]
                    slot_weight = risky_share / k

                    for sym, sc in selected_risky:
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
            f"0 bad canaries -> 100% across top {top_k} risky assets; 1 bad canary -> 50% cash fraction; "
            f"2 bad canaries -> 100% cash fraction into leading defensive asset (IEF/LQD/BIL)."
        )

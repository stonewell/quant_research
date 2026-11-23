"""Microstructure & Short-Horizon Multi-Factor Composite Strategy (Track MS, MS7).

Chains the Track MS canonical factor & portfolio construction primitives:
1. Factor Families:
   - MS2 Chip & Structural: ``cyq_cost_deviation``, ``cyq_quantile_spread_90_10``
   - MS3 Dynamic Peer Network: ``compute_peer_momentum_spillover``,
     ``compute_peer_chip_interaction_alpha``
   - MS4 Microstructure & Limit-Up: ``overnight_return``, ``intraday_return``,
     ``limit_up_magnet_and_break``, daily-proxy ``iceberg_accumulation_score`` &
     ``smart_money_flow_divergence`` (recording degradation traces via MS9)
   - MS15/MS16 Quality & Anti-Lottery: ``roc`` minus ``max_return``
2. MS1 Cross-Sectional Purification:
   - ``cs_mad_winsorize`` -> optional ``cs_neutralize_ols`` ->
     ``cs_rank_gaussian_or_uniform(..., method="gaussian")`` ->
     optional ``cs_lowdin_orthogonalize``
3. M1.2 Hysteresis Selection:
   - Reuses ``common.selection_band.RankBandSelection`` directly (zero duplication).
4. MS3 + MS6 Convex Optimization:
   - ``ledoit_wolf_shrinkage`` + ``compute_almgren_chriss_impact_coefficients`` +
     ``_alpha_impact_penalized_weights`` (SLSQP with L1 + L1.5 impact penalties).

Sparse Weights & Explicit 0.0 Contract (AGENTS Rule 3)
------------------------------------------------------
- Non-rebalance dates are strictly all-``NaN`` (never forward-filled).
- Active rebalance rows contain finite floats with explicit ``0.0`` written for
  every unselected, exited, or empty symbol; rows sum to ``1.0`` when
  ``cash_proxy`` is present in ``universe`` (and ``<= 1.0`` with unallocated
  cash residual otherwise).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _alpha_impact_penalized_weights,
    compute_almgren_chriss_impact_coefficients,
)
from common.covariance import ledoit_wolf_shrinkage
from common.cross_sectional import (
    cs_lowdin_orthogonalize,
    cs_mad_winsorize,
    cs_neutralize_ols,
    cs_rank_gaussian_or_uniform,
)
from common.daily_factors import (
    intraday_return,
    limit_up_magnet_and_break,
    max_return,
    overnight_return,
)
from common.indicators import roc
from common.intraday_features import (
    iceberg_accumulation_score,
    smart_money_flow_divergence,
)
from common.peer_network import (
    compute_peer_chip_interaction_alpha,
    compute_peer_momentum_spillover,
)
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from common.selection_band import RankBandSelection
from common.structural_factors import (
    cyq_cost_deviation,
    cyq_quantile_spread_90_10,
)
from .config import StrategyConfig


class MicrostructureAlphaCompositeStrategy(AllocationTemplate):
    """Microstructure & Short-Horizon Multi-Factor Composite Strategy (Track MS, MS7)."""

    version: str = "1.1.0"

    def __init__(
        self,
        config: Optional[StrategyConfig] = None,
        version: Optional[str] = None,
    ) -> None:
        self.config = config or StrategyConfig()
        strat_version = version or getattr(self.__class__, "version", "1.1.0")
        self._last_ms7_diagnostics: Dict[str, Any] = {
            "real_turn_bars": 0,
            "proxy_turn_bars": 0,
            "n_symbols_with_real_turn": 0,
            "n_symbols_with_5m": 0,
            "has_any_5m": False,
            "iceberg_5m_bars": 0,
            "smart_money_non_nan_cells": 0,
            "iceberg_non_nan_cells": 0,
        }
        super().__init__(
            name="microstructure_alpha_composite",
            param_grid={},
            factor_tags=[
                "relative_momentum",
                "mean_reversion",
                "volatility_targeting",
            ],
            version=strat_version,
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        p = params or {}
        cfg = self.config
        lookback = int(p.get("ms7_lookback_days", cfg.ms7_lookback_days))
        return max(20, lookback) + 5

    def explain_weights(self, params: Optional[dict] = None) -> str:
        p = params or {}
        cfg = self.config
        freq = int(p.get("ms7_rebalance_freq_days", cfg.ms7_rebalance_freq_days))
        lb = int(p.get("ms7_lookback_days", cfg.ms7_lookback_days))
        buy_b = float(p.get("ms7_buy_band_pct", cfg.ms7_buy_band_pct))
        sell_b = float(p.get("ms7_sell_band_pct", cfg.ms7_sell_band_pct))
        l15 = float(p.get("ms7_impact_penalty_l15", cfg.ms7_impact_penalty_l15))
        return (
            f"MicrostructureAlphaCompositeStrategy (MS7 v{self.version}): "
            f"rebalances every {freq}d with {lb}d lookback, RankBandSelection "
            f"[{buy_b:.0%}, {sell_b:.0%}], Ledoit-Wolf covariance, and L1.5 impact "
            f"penalty ({l15:.3f})."
        )

    @staticmethod
    def _sparse_reindex_columns(
        weights_df: pd.DataFrame, all_universe_syms: List[str]
    ) -> pd.DataFrame:
        """Reindex columns to ``all_universe_syms`` preserving Rule 3 sparse contract.

        Missing/empty universe columns receive explicit ``0.0`` ONLY on active
        rebalance rows (``weights_df.notna().any(axis=1)``); non-rebalance rows
        stay strictly all-``NaN``.
        """
        out = weights_df.reindex(columns=all_universe_syms)
        active_mask = weights_df.notna().any(axis=1)
        if active_mask.any():
            missing_cols = [c for c in all_universe_syms if c not in weights_df.columns]
            if missing_cols:
                out.loc[active_mask, missing_cols] = 0.0
        return out

    def _compute_composite_scores_internal(
        self,
        universe: Dict[str, pd.DataFrame],
        master_index: pd.DatetimeIndex,
        risky_symbols: List[str],
        params: Optional[dict] = None,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Compute aligned OHLCV-derived returns, close, volume, and MS7 composite scores."""
        p = params or {}
        cfg = self.config

        lookback_days = int(p.get("ms7_lookback_days", cfg.ms7_lookback_days))
        mad_n = float(p.get("ms7_mad_n", cfg.ms7_mad_n))
        enable_lowdin_ortho = bool(p.get("ms7_enable_lowdin_ortho", cfg.ms7_enable_lowdin_ortho))
        enable_ols_neutralize = bool(
            p.get("ms7_enable_ols_neutralize", cfg.ms7_enable_ols_neutralize)
        )
        diag_sink = p.get("diagnostics")
        intraday_5m_map = p.get("intraday_5m_universe") or {}

        close_dict: Dict[str, pd.Series] = {}
        open_dict: Dict[str, pd.Series] = {}
        high_dict: Dict[str, pd.Series] = {}
        low_dict: Dict[str, pd.Series] = {}
        vol_dict: Dict[str, pd.Series] = {}

        for s in risky_symbols:
            df_s = universe[s]
            c_s = df_s["Close"].astype(float).reindex(master_index)
            close_dict[s] = c_s
            open_dict[s] = (
                df_s["Open"].astype(float).reindex(master_index)
                if "Open" in df_s.columns
                else c_s
            )
            high_dict[s] = (
                df_s["High"].astype(float).reindex(master_index)
                if "High" in df_s.columns
                else c_s
            )
            low_dict[s] = (
                df_s["Low"].astype(float).reindex(master_index)
                if "Low" in df_s.columns
                else c_s
            )
            vol_dict[s] = (
                df_s["Volume"].astype(float).reindex(master_index)
                if "Volume" in df_s.columns
                else pd.Series(1.0, index=master_index)
            )

        close_df = pd.DataFrame(close_dict, index=master_index)
        open_df = pd.DataFrame(open_dict, index=master_index)
        high_df = pd.DataFrame(high_dict, index=master_index)
        low_df = pd.DataFrame(low_dict, index=master_index)
        volume_df = pd.DataFrame(vol_dict, index=master_index)
        returns_df = close_df.pct_change(fill_method=None)

        def _norm_comp(panel: pd.DataFrame) -> pd.DataFrame:
            clipped = cs_mad_winsorize(panel, n_mad=mad_n)
            # Treat zero-dispersion rows (e.g. all-zero magnet_break on normal days) as NaN
            # so constant components do not dilute active signals.
            row_std = clipped.std(axis=1, ddof=1)
            clipped = clipped.where(row_std.gt(1e-12), axis=0)
            return cs_rank_gaussian_or_uniform(clipped, method="gaussian")

        def _weighted_nanmean(
            components: List[Tuple[float, pd.DataFrame]],
        ) -> pd.DataFrame:
            num = np.zeros((len(master_index), len(risky_symbols)), dtype=np.float64)
            den = np.zeros_like(num)
            for w_c, df_c in components:
                if w_c <= 0.0:
                    continue
                arr = df_c.reindex(index=master_index, columns=risky_symbols).to_numpy(
                    dtype=np.float64
                )
                ok = np.isfinite(arr)
                num += np.where(ok, w_c * arr, 0.0)
                den += np.where(ok, w_c, 0.0)
            out = np.where(den > 1e-12, num / np.where(den > 1e-12, den, 1.0), np.nan)
            return pd.DataFrame(out, index=master_index, columns=risky_symbols, dtype=np.float64)

        # 2. Multi-Factor Panel Extraction (MS2, MS3, MS4, MS16)
        # Pre-slice intraday 5m bars causally to <= master_index[-1] so walkforward
        # fold evaluation can never peek past the fold's last session.
        cutoff_day = pd.Timestamp(master_index[-1]).normalize() if len(master_index) > 0 else None
        sliced_5m_map: Dict[str, Optional[pd.DataFrame]] = {}
        n_symbols_with_5m = 0
        iceberg_5m_bars_total = 0
        for s in risky_symbols:
            raw_5m = intraday_5m_map.get(s) if isinstance(intraday_5m_map, dict) else None
            if raw_5m is not None and not raw_5m.empty:
                df_5m = raw_5m
                if cutoff_day is not None and isinstance(df_5m.index, pd.DatetimeIndex):
                    idx_5m = df_5m.index.tz_localize(None) if df_5m.index.tz is not None else df_5m.index
                    df_5m = df_5m.loc[idx_5m.normalize() <= cutoff_day]
                if not df_5m.empty:
                    sliced_5m_map[s] = df_5m
                    n_symbols_with_5m += 1
                    if isinstance(df_5m.index, pd.DatetimeIndex):
                        idx_norm = (
                            df_5m.index.tz_localize(None).normalize()
                            if df_5m.index.tz is not None
                            else df_5m.index.normalize()
                        )
                        iceberg_5m_bars_total += int((df_5m.groupby(idx_norm).size() >= 8).sum())
                else:
                    sliced_5m_map[s] = None
            else:
                sliced_5m_map[s] = None

        # Family 1: Chip & Structural (MS2) — positive Capital Gains Overhang (Grinblatt & Han 2005)
        # paired with tight chip peak (-chip_spread_panel).
        cost_dev_cols: Dict[str, pd.Series] = {}
        chip_spread_cols: Dict[str, pd.Series] = {}
        real_turn_bars_total = 0
        proxy_turn_bars_total = 0
        n_symbols_with_real_turn = 0
        for s in risky_symbols:
            df_s = universe[s]
            df_5m = sliced_5m_map.get(s)
            if len(df_s) >= 5:
                turn_stats: Dict[str, Any] = {}
                cd = cyq_cost_deviation(
                    df_s,
                    n_bins=50,
                    intraday_5m_bars=df_5m,
                    turn_stats_out=turn_stats,
                ).reindex(master_index)
                cs = cyq_quantile_spread_90_10(
                    df_s,
                    n_bins=50,
                    intraday_5m_bars=df_5m,
                ).reindex(master_index)
                real_turn_bars_total += int(turn_stats.get("real_turn_bars", 0))
                proxy_turn_bars_total += int(turn_stats.get("proxy_turn_bars", 0))
                if turn_stats.get("used_real_turn"):
                    n_symbols_with_real_turn += 1
                cost_dev_cols[s] = cd
                chip_spread_cols[s] = cs
            else:
                cost_dev_cols[s] = pd.Series(np.nan, index=master_index)
                chip_spread_cols[s] = pd.Series(np.nan, index=master_index)
        cost_dev_panel = pd.DataFrame(cost_dev_cols, index=master_index)
        chip_spread_panel = pd.DataFrame(chip_spread_cols, index=master_index)
        f_chip = _weighted_nanmean([
            (0.65, _norm_comp(cost_dev_panel)),
            (0.35, _norm_comp(-chip_spread_panel)),
        ])

        # Family 2: Peer Network Spillover (MS3) — 5d smoothed peer momentum interacted with
        # chip tightening (avoids raw 1d anti-drift subtraction).
        peer_win = max(15, lookback_days)
        peer_k = min(5, max(1, len(risky_symbols) - 1))
        if len(risky_symbols) >= 2:
            peer_mom_df = compute_peer_momentum_spillover(
                returns_df,
                window=peer_win,
                top_k=peer_k,
                adjacency_method="partial_corr",
                min_obs=min(10, peer_win),
            )
            peer_mom_smooth = peer_mom_df.rolling(5, min_periods=2).mean()
            peer_chip_df = compute_peer_chip_interaction_alpha(
                peer_mom_smooth, chip_spread_panel, delta_window=5
            )
            f_peer = peer_chip_df.combine_first(peer_mom_smooth)
        else:
            f_peer = pd.DataFrame(np.nan, index=master_index, columns=risky_symbols)

        # Family 3: Overnight/Intraday Split & Microstructure (MS4) — cross-sectionally
        # standardized before blending; smart_money_flow_divergence gated on real 5m bars
        # so its contrarian daily fallback never cancels intra_minus_on / iceberg.
        r_on = overnight_return(open_df, close_df)
        r_id = intraday_return(open_df, close_df)
        intra_minus_on = (r_id - r_on).rolling(
            lookback_days, min_periods=max(3, lookback_days // 4)
        ).mean()
        magnet_break = limit_up_magnet_and_break(open_df, high_df, low_df, close_df, volume_df)
        magnet_smooth = magnet_break.rolling(5, min_periods=1).mean()

        has_any_5m = bool(n_symbols_with_5m > 0)
        ice_cols: Dict[str, pd.Series] = {}
        sm_cols: Dict[str, pd.Series] = {}
        for s in risky_symbols:
            df_s = universe[s]
            df_5m = sliced_5m_map.get(s)
            ice_s = iceberg_accumulation_score(
                df_5m, daily_df=df_s, symbol=s, degradation_trace=diag_sink
            ).reindex(master_index)
            ice_cols[s] = ice_s.rolling(lookback_days, min_periods=3).mean()
            if has_any_5m or diag_sink is not None:
                sm_s = smart_money_flow_divergence(
                    df_5m, daily_df=df_s, symbol=s, degradation_trace=diag_sink
                ).reindex(master_index)
                sm_cols[s] = sm_s.rolling(lookback_days, min_periods=3).mean()

        ice_df = pd.DataFrame(ice_cols, index=master_index)
        sm_df = pd.DataFrame(sm_cols, index=master_index) if sm_cols else pd.DataFrame(index=master_index)
        self._last_ms7_diagnostics = {
            "real_turn_bars": int(real_turn_bars_total),
            "proxy_turn_bars": int(proxy_turn_bars_total),
            "n_symbols_with_real_turn": int(n_symbols_with_real_turn),
            "n_symbols_with_5m": int(n_symbols_with_5m),
            "has_any_5m": bool(has_any_5m),
            "iceberg_5m_bars": int(iceberg_5m_bars_total),
            "smart_money_non_nan_cells": int(sm_df.notna().sum().sum()) if not sm_df.empty else 0,
            "iceberg_non_nan_cells": int(ice_df.notna().sum().sum()) if not ice_df.empty else 0,
        }

        micro_components: List[Tuple[float, pd.DataFrame]] = [
            (0.55 if not has_any_5m else 0.45, _norm_comp(intra_minus_on)),
            (0.35 if not has_any_5m else 0.30, _norm_comp(ice_df)),
            (0.10, _norm_comp(magnet_smooth)),
        ]
        if has_any_5m:
            micro_components.append(
                (0.15, _norm_comp(sm_df))
            )
        f_micro = _weighted_nanmean(micro_components)

        # Family 4: Volatility-Adjusted Medium-Term Momentum + Anti-Lottery MAX Penalty (MS16)
        short_vol_exp = returns_df.rolling(lookback_days, min_periods=5).std()
        mom_panel = pd.DataFrame(
            {s: roc(close_df[s], lookback_days) for s in risky_symbols},
            index=master_index,
        )
        mom_sharpe = mom_panel / (short_vol_exp.replace(0.0, np.nan) * np.sqrt(float(lookback_days)))
        max_ret_panel = max_return(close_df, lookback=lookback_days, k=min(2, lookback_days))
        f_mom_quality = _weighted_nanmean([
            (0.75, _norm_comp(mom_sharpe)),
            (0.25, _norm_comp(-max_ret_panel)),
        ])

        # 3. MS1 Cross-Sectional Purification, Price-Level-Invariant Neutralization & Löwdin Ortho
        vol_sma_60 = volume_df.where(volume_df > 0.0).rolling(60, min_periods=5).mean()
        rel_vol_exp = np.log((volume_df.where(volume_df > 0.0) / vol_sma_60).clip(lower=1e-4))
        exposures = {"rel_vol": rel_vol_exp, "short_vol": short_vol_exp}

        raw_families = {
            "chip": f_chip,
            "peer": f_peer,
            "micro": f_micro,
            "mom_quality": f_mom_quality,
        }
        purified: Dict[str, pd.DataFrame] = {}
        for fname, fpanel in raw_families.items():
            w_p = cs_mad_winsorize(fpanel, n_mad=mad_n)
            if enable_ols_neutralize:
                w_p = cs_neutralize_ols(w_p, exposures=exposures, sectors=None)
            purified[fname] = cs_rank_gaussian_or_uniform(w_p, method="gaussian")

        if enable_lowdin_ortho and len(risky_symbols) >= 5:
            ortho = cs_lowdin_orthogonalize(
                purified, demean=True, scale="unit_variance"
            )
            # Fall back to purified panel on dates where listwise Lowdin is NaN
            combined_panels = {
                k: ortho[k].combine_first(purified[k]) for k in purified
            }
        else:
            combined_panels = purified

        # Economically weighted combination across orthogonalized families
        composite_scores = _weighted_nanmean([
            (0.35, combined_panels["mom_quality"]),
            (0.30, combined_panels["chip"]),
            (0.25, combined_panels["micro"]),
            (0.10, combined_panels["peer"]),
        ]).where(close_df.notna())

        return composite_scores, close_df, volume_df, returns_df

    def compute_composite_scores(
        self,
        universe: Dict[str, pd.DataFrame],
        params: Optional[dict] = None,
    ) -> pd.DataFrame:
        """Compute the MS7 purified composite alpha score panel for risky symbols."""
        p = params or {}
        cfg = self.config
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        risky_symbols = [
            s for s in universe.keys()
            if s != cash_proxy and not universe[s].empty
        ]
        if not risky_symbols:
            return pd.DataFrame()
        non_empty_indices = [universe[s].index for s in risky_symbols]
        master_index = pd.DatetimeIndex(sorted(set().union(*non_empty_indices)))
        if len(master_index) == 0:
            return pd.DataFrame(columns=risky_symbols, dtype=float)
        scores, _, _, _ = self._compute_composite_scores_internal(
            universe, master_index, risky_symbols, params=p
        )
        return scores

    def generate_weights(
        self,
        universe: Dict[str, pd.DataFrame],
        params: Optional[dict] = None,
    ) -> pd.DataFrame:
        p = params or {}
        cfg = self.config

        rebal_freq = int(p.get("ms7_rebalance_freq_days", cfg.ms7_rebalance_freq_days))
        lookback_days = int(p.get("ms7_lookback_days", cfg.ms7_lookback_days))
        buy_band_pct = float(p.get("ms7_buy_band_pct", cfg.ms7_buy_band_pct))
        sell_band_pct = float(p.get("ms7_sell_band_pct", cfg.ms7_sell_band_pct))
        delta_score_sigma = float(p.get("ms7_delta_score_sigma", cfg.ms7_delta_score_sigma))
        risk_aversion = float(p.get("ms7_risk_aversion", cfg.ms7_risk_aversion))
        turnover_penalty_l1 = float(p.get("ms7_turnover_penalty_l1", cfg.ms7_turnover_penalty_l1))
        impact_penalty_l15 = float(p.get("ms7_impact_penalty_l15", cfg.ms7_impact_penalty_l15))
        single_stock_cap = float(p.get("ms7_single_stock_cap", cfg.ms7_single_stock_cap))
        cash_allowance = float(p.get("ms7_cash_allowance", cfg.ms7_cash_allowance))
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        all_universe_syms = list(universe.keys())
        if not all_universe_syms:
            return pd.DataFrame()

        risky_symbols = [
            s for s in all_universe_syms
            if s != cash_proxy and not universe[s].empty
        ]
        all_tracked = list(
            dict.fromkeys(
                risky_symbols
                + ([cash_proxy] if (cash_proxy in universe and not universe[cash_proxy].empty) else [])
            )
        )
        if not all_tracked:
            return pd.DataFrame()

        # Build aligned master trading calendar
        non_empty_indices = [universe[s].index for s in all_tracked if not universe[s].empty]
        master_index = pd.DatetimeIndex(sorted(set().union(*non_empty_indices)))
        if len(master_index) == 0:
            return pd.DataFrame(columns=all_universe_syms, dtype=float)

        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)
        if not risky_symbols:
            weights_df = pd.DataFrame(np.nan, index=master_index, columns=all_tracked, dtype=float)
            if len(rebalance_dates) > 0 and cash_proxy in all_tracked:
                weights_df.loc[rebalance_dates[0], cash_proxy] = 1.0
            return self._sparse_reindex_columns(weights_df, all_universe_syms)

        # 1-3. Multi-factor extraction, purification, neutralization, and Löwdin composite
        composite_scores, close_df, volume_df, returns_df = (
            self._compute_composite_scores_internal(
                universe, master_index, risky_symbols, params=p
            )
        )

        # 4. M1.2 Hysteresis Band Selection via RankBandSelection (Mandatory Reuse)
        scores_rebal = composite_scores.reindex(rebalance_dates)
        band_selector = RankBandSelection(
            buy_band_pct=buy_band_pct,
            sell_band_pct=sell_band_pct,
            delta_score_sigma=delta_score_sigma,
        )
        band_weights_sparse = band_selector.select(scores_rebal)
        band_active_rebal = band_weights_sparse.ffill().fillna(0.0)

        # 5. Convex L1.5 Impact-Penalized Weight Optimization on Selected Survivors
        weights_df = pd.DataFrame(np.nan, index=master_index, columns=all_tracked, dtype=np.float64)
        prev_full_w = np.zeros(len(risky_symbols), dtype=np.float64)
        sym_to_idx = {s: idx for idx, s in enumerate(risky_symbols)}
        cov_win_len = max(20, lookback_days)

        for t in rebalance_dates:
            if t not in band_active_rebal.index:
                continue
            survivors = [s for s in risky_symbols if float(band_active_rebal.loc[t, s]) > 0.0]
            # Explicit 0.0 for all unselected / liquidated symbols on this rebalance row
            row_w: Dict[str, float] = {s: 0.0 for s in all_tracked}

            if not survivors:
                if cash_proxy in all_tracked:
                    row_w[cash_proxy] = 1.0
                prev_full_w[:] = 0.0
                for s_col, val in row_w.items():
                    weights_df.loc[t, s_col] = val
                continue

            if len(survivors) == 1:
                s0 = survivors[0]
                # Enforce hard single_stock_cap (never breach cap; residual goes to cash)
                w_s0 = min(1.0, single_stock_cap)
                row_w[s0] = float(w_s0)
            else:
                ret_win = returns_df.loc[:t, survivors].tail(cov_win_len).dropna(how="any")
                if len(ret_win) >= 5:
                    cov_lw, _ = ledoit_wolf_shrinkage(ret_win.to_numpy(dtype=np.float64))
                    cov_ann = np.asarray(cov_lw, dtype=np.float64) * 252.0
                else:
                    cov_ann = np.eye(len(survivors), dtype=np.float64) * (0.20 ** 2)

                # Grinold-Kahn annualized expected-return scaling: commensurate with cov_ann = 252 * cov_lw.
                # Uses median annualized vol across survivors so high-vol names aren't artificially boosted.
                diag_vol_ann = np.sqrt(np.maximum(np.diag(cov_ann), 1e-6))
                med_vol_ann = float(np.median(diag_vol_ann))
                alpha_z = (
                    scores_rebal.loc[t, survivors].fillna(0.0).to_numpy(dtype=np.float64)
                )
                alpha_ann = 1.0 * med_vol_ann * alpha_z

                w_prev_surv = np.array(
                    [prev_full_w[sym_to_idx[s]] for s in survivors], dtype=np.float64
                )

                # Compute 1D survivor volatility and ADV for Almgren-Chriss impact coefficients
                vol_surv = (
                    returns_df.loc[:t, survivors]
                    .tail(cov_win_len)
                    .std(ddof=1)
                    .to_numpy(dtype=np.float64)
                )
                adv_surv = (
                    (close_df.loc[:t, survivors] * volume_df.loc[:t, survivors])
                    .tail(cov_win_len)
                    .mean()
                    .to_numpy(dtype=np.float64)
                )
                kappas = compute_almgren_chriss_impact_coefficients(vol_surv, adv_surv)

                w_opt = _alpha_impact_penalized_weights(
                    cov_ann,
                    w_prev_surv,
                    alpha=alpha_ann,
                    risk_aversion=risk_aversion,
                    turnover_penalty=turnover_penalty_l1,
                    impact_penalty=impact_penalty_l15,
                    impact_kappas=kappas,
                    single_cap=single_stock_cap,
                    cash_allowance=cash_allowance,
                )
                for idx_s, s_name in enumerate(survivors):
                    val = float(w_opt[idx_s])
                    row_w[s_name] = 0.0 if val < 1e-6 else val

            risky_sum = sum(row_w[s] for s in risky_symbols)
            if risky_sum > 1.0 + 1e-9:
                for s in risky_symbols:
                    row_w[s] /= risky_sum
                risky_sum = 1.0
            if cash_proxy in all_tracked:
                row_w[cash_proxy] = max(0.0, 1.0 - risky_sum)

            for s in risky_symbols:
                prev_full_w[sym_to_idx[s]] = row_w[s]
            for s_col, val in row_w.items():
                weights_df.loc[t, s_col] = val

        return self._sparse_reindex_columns(weights_df, all_universe_syms)

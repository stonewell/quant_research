"""Researched Quantitative Asset Allocation Strategy Implementations.

1. ActiveDualMomentumRiskParity:
   Dual Momentum GTAA (Antonacci 2014, Faber 2007) with Absolute Trend Gate,
   Multi-Horizon Relative Momentum, Inverse Volatility Sizing, and Cash Overlay.

2. BoldAssetAllocation:
   Wouter J. Keller (2022, SSRN) Bold Asset Allocation (BAA-G12). Uses a Canary
   universe to detect market turbulence and dynamically switch between
   Offensive and Defensive asset pools.

3. VolatilityManagedStrategy:
   Alan Moreira & Tyler Muir (2017, Journal of Finance) inverse-variance return
   scaling strategy to mitigate momentum crash risk and stabilize portfolio volatility.
"""

from typing import Dict, List

import numpy as np
import pandas as pd

from common.indicators import realized_vol, roc, sma
from .config import StrategyConfig


def _get_rebalance_dates(index: pd.DatetimeIndex, freq_days: int) -> pd.DatetimeIndex:
    """Returns rebalance dates every freq_days."""
    return index[::freq_days]


class ActiveDualMomentumRiskParity:
    """Active Dual Momentum GTAA + Inverse Volatility Risk Parity Strategy.

    1. Trend Gate: Close > 200d SMA and 126d ROC > 0.
    2. Momentum Ranking: 0.5 * ROC(63) + 0.5 * ROC(126).
    3. Inverse Volatility Sizing: Weight_i ~ 1 / Vol_60(i).
    4. Cash Overlay: Unallocated weight assigned to cash proxy (BIL).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        trend_period = p.get("trend_sma_period", cfg.trend_sma_period)
        short_mom = p.get("mom_short_lookback", cfg.mom_short_lookback)
        long_mom = p.get("mom_long_lookback", cfg.mom_long_lookback)
        vol_lookback = p.get("vol_lookback", cfg.vol_lookback)
        top_k = p.get("top_k", cfg.top_k)
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        # Precompute indicators for all symbols
        closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})
        smas = pd.DataFrame({sym: sma(df["Close"], trend_period) for sym, df in universe.items()})
        rocs_short = pd.DataFrame({sym: roc(df["Close"], short_mom) for sym, df in universe.items()})
        rocs_long = pd.DataFrame({sym: roc(df["Close"], long_mom) for sym, df in universe.items()})
        vols = pd.DataFrame({sym: realized_vol(df["Close"], vol_lookback) for sym, df in universe.items()})

        composite_mom = 0.5 * rocs_short + 0.5 * rocs_long

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        risky_symbols = [s for s in cfg.risky_universe if s in symbols]

        for date in rebalance_dates:
            # 1. Absolute Momentum Gate: Close > SMA(200) and ROC(126) > 0
            passing_symbols = []
            for sym in risky_symbols:
                c = closes.loc[date, sym]
                s = smas.loc[date, sym]
                rl = rocs_long.loc[date, sym]
                if pd.notna(c) and pd.notna(s) and pd.notna(rl) and c > s and rl > 0:
                    passing_symbols.append(sym)

            m = len(passing_symbols)
            if m > 0:
                # 2. Relative Momentum Ranking
                sym_scores = composite_mom.loc[date, passing_symbols].dropna()
                selected = sym_scores.nlargest(min(m, top_k)).index.tolist()

                # 3. Inverse Volatility Risk Parity Sizing
                selected_vols = vols.loc[date, selected].replace(0, np.nan)
                inv_v = 1.0 / selected_vols
                sum_inv_v = inv_v.sum()

                if sum_inv_v > 0 and pd.notna(sum_inv_v):
                    raw_weights = inv_v / sum_inv_v
                    # Scale down total risky exposure if fewer than K assets pass
                    scale_factor = len(selected) / float(top_k)
                    final_risky_weights = raw_weights * scale_factor

                    for sym in selected:
                        weights_rebal.loc[date, sym] = final_risky_weights[sym]

            # 4. Cash Overlay
            total_risky_weight = weights_rebal.loc[date, risky_symbols].sum()
            cash_weight = max(0.0, 1.0 - total_risky_weight)
            if cash_proxy in symbols:
                weights_rebal.loc[date, cash_proxy] = cash_weight

        # Sparse target weights DataFrame
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        p = params or {}
        return (
            f"Active Dual Momentum GTAA: Rebalances every {p.get('rebalance_freq_days', self.config.rebalance_freq_days)} days. "
            f"Reasoning: Applies an absolute momentum trend filter (200d SMA & 126d ROC > 0) to eliminate declining assets. "
            f"Ranks qualifying assets by dual-horizon momentum (63d + 126d ROC) and allocates to the top {p.get('top_k', self.config.top_k)} "
            f"using 60-day inverse volatility weighting. Unallocated capital is held in defensive cash proxy ({self.config.cash_proxy})."
        )

    def warmup_bars(self, params: dict = None) -> int:
        p = params or {}
        return max(p.get("trend_sma_period", self.config.trend_sma_period), p.get("mom_long_lookback", self.config.mom_long_lookback))


class BoldAssetAllocation:
    """Wouter Keller's Bold Asset Allocation (BAA-G12) Strategy.

    1. Canary Universe Turbulence Detector: SPY, EEM, EFA, AGG.
    2. If ANY canary asset has negative momentum (Close < 12m SMA or 126d ROC < 0), state = Turbulent.
    3. Calm State: Select top 3 assets from Offensive Universe by 126d ROC.
    4. Turbulent State: Select top 3 assets from Defensive Universe with ROC > 0; remainder to BIL cash.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        top_k = p.get("top_k", cfg.top_k)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})
        smas = pd.DataFrame({sym: sma(df["Close"], 200) for sym, df in universe.items()})
        rocs = pd.DataFrame({sym: roc(df["Close"], 126) for sym, df in universe.items()})

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        canary_symbols = [s for s in cfg.baa_canary if s in symbols]
        offensive_symbols = [s for s in cfg.baa_offensive if s in symbols]
        defensive_symbols = [s for s in cfg.baa_defensive if s in symbols]

        for date in rebalance_dates:
            # Check Canary Universe
            turbulent = False
            for cs in canary_symbols:
                c = closes.loc[date, cs]
                s = smas.loc[date, cs]
                r = rocs.loc[date, cs]
                if pd.isna(c) or pd.isna(s) or pd.isna(r) or c <= s or r <= 0:
                    turbulent = True
                    break

            if not turbulent and offensive_symbols:
                # Calm Market -> Trade Offensive Universe
                scores = rocs.loc[date, offensive_symbols].dropna()
                if not scores.empty:
                    top_assets = scores.nlargest(min(len(scores), top_k)).index.tolist()
                    w_each = 1.0 / len(top_assets) if top_assets else 0.0
                    for ta in top_assets:
                        weights_rebal.loc[date, ta] = w_each
            else:
                # Turbulent Market -> Trade Defensive Universe
                scores = rocs.loc[date, defensive_symbols].dropna()
                positive_defensive = scores[scores > 0].nlargest(min(len(scores), top_k)).index.tolist()

                if positive_defensive:
                    w_each = 1.0 / top_k
                    for da in positive_defensive:
                        weights_rebal.loc[date, da] = w_each
                    # Assign remaining unallocated slots to cash
                    rem_weight = 1.0 - (len(positive_defensive) * w_each)
                    if cash_proxy in symbols and rem_weight > 0:
                        weights_rebal.loc[date, cash_proxy] += rem_weight
                else:
                    # 100% Defensive Cash Proxy
                    if cash_proxy in symbols:
                        weights_rebal.loc[date, cash_proxy] = 1.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        return (
            f"Bold Asset Allocation (BAA-G12, Wouter Keller 2022): Rebalances every {self.config.rebalance_freq_days} days. "
            f"Reasoning: Monitors Canary universe ({', '.join(self.config.baa_canary)}) for market turbulence. "
            f"In calm markets, allocates equal weight to top 3 Offensive assets. "
            f"In turbulent markets, rotates to Defensive universe assets with positive momentum, placing remainder in cash ({self.config.cash_proxy})."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 200


class VolatilityManagedStrategy:
    """Alan Moreira & Tyler Muir (2017, Journal of Finance) Volatility-Managed Strategy.

    Scales baseline portfolio return / weight inversely by its recent 20-day realized variance:
    Weight = Target_Vol / Realized_Vol_20, capped at max_leverage (e.g. 1.0).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        target_vol = p.get("vol_managed_target_vol", cfg.vol_managed_target_vol)
        var_lookback = p.get("vol_managed_var_lookback", cfg.vol_managed_var_lookback)
        max_lev = p.get("vol_managed_max_leverage", cfg.vol_managed_max_leverage)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        risky_symbols = [s for s in cfg.risky_universe if s in symbols]
        if not risky_symbols:
            risky_symbols = [s for s in symbols if s != cash_proxy]

        closes = pd.DataFrame({sym: universe[sym]["Close"] for sym in risky_symbols})

        # Calculate equal-weight baseline portfolio daily return
        returns_df = closes.pct_change()
        portfolio_return = returns_df.mean(axis=1)

        # 20-day annualized realized volatility of the baseline risky portfolio
        port_vol = portfolio_return.rolling(var_lookback, min_periods=var_lookback).std() * np.sqrt(252)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            # Only equal-weight across risky symbols that actually have a
            # price on this date -- a symbol that hasn't started trading yet
            # (NaN Close) must be excluded, not silently handed real capital
            # that would then earn a fabricated 0% return every day.
            valid_risky = [s for s in risky_symbols if pd.notna(closes.loc[date, s])]
            n_valid = len(valid_risky)
            if n_valid == 0:
                continue

            v = port_vol.loc[date]
            if pd.notna(v) and v > 0:
                # Volatility timing scale factor: Target_Vol / Realized_Vol
                scale = min(max_lev, target_vol / v)
            else:
                scale = 1.0

            risky_w = scale / n_valid
            for sym in valid_risky:
                weights_rebal.loc[date, sym] = risky_w

            total_risky_w = weights_rebal.loc[date, risky_symbols].sum()
            cash_w = max(0.0, 1.0 - total_risky_w)
            if cash_proxy in symbols:
                weights_rebal.loc[date, cash_proxy] = cash_w

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        return (
            f"Volatility-Managed Portfolio (Moreira & Muir 2017, J. Finance): Rebalances every {self.config.rebalance_freq_days} days. "
            f"Reasoning: Dynamically scales portfolio risk exposure inversely by the 20-day realized volatility of the basket. "
            f"De-leverages into cash ({self.config.cash_proxy}) when volatility spikes, eliminating momentum crash tail risk."
        )

    def warmup_bars(self, params: dict = None) -> int:
        p = params or {}
        return p.get("vol_managed_var_lookback", self.config.vol_managed_var_lookback)

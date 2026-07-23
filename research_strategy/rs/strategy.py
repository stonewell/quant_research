"""Researched Quantitative Asset Allocation Strategy Implementations.

Powered by NaturalLanguageStrategy: A plain English execution engine that interprets
structured strategy rules (ParsedStrategySpec) and dynamically executes backtests.

Canonical Preset Strategies:
1. ActiveDualMomentumRiskParity (Antonacci 2014, Faber 2007)
2. BoldAssetAllocation (Wouter Keller 2022 BAA-G12)
3. VolatilityManagedStrategy (Moreira & Muir 2017)
"""

from typing import Dict, List, Union

import numpy as np
import pandas as pd

from common.indicators import realized_vol, roc, sma
from .config import StrategyConfig
from .nl_parser import ParsedStrategySpec, parse_plain_english_strategy

CANONICAL_DUAL_MOMENTUM_TEXT = (
    "Rebalance monthly. Risky assets: SPY, QQQ, IWM, EFA, EEM, GLD, TLT, VNQ. "
    "Apply absolute trend gate: Close > 200d SMA and 126d ROC > 0. "
    "Rank passing assets by 63d and 126d momentum, select top 3 assets, "
    "and allocate using 60d inverse volatility risk parity weighting. "
    "Assign unallocated capital to cash proxy BIL."
)

CANONICAL_BAA_KELLER_TEXT = (
    "Rebalance monthly. Use canary assets: SPY, EEM, EFA, AGG. "
    "If any canary asset has Close below 200d SMA or 126d ROC <= 0, market is turbulent. "
    "In calm markets, select top 3 offensive assets from SPY, QQQ, IWM, EFA, EEM, TLT, LQD, DBC by 126d ROC with equal weighting. "
    "In turbulent markets, select top 3 defensive assets from TIP, IEF, TLT, BIL, AGG, DBC with positive 126d ROC, "
    "and assign remainder to cash proxy BIL."
)

CANONICAL_VOLATILITY_MANAGED_TEXT = (
    "Rebalance monthly. Risky assets: SPY, QQQ, IWM, EFA, EEM, GLD, TLT, VNQ. "
    "Dynamically scale portfolio risk exposure using 20-day volatility-managed inverse variance scaling "
    "targeting 15% annual volatility, de-leveraging into cash proxy BIL when volatility spikes."
)


def _get_rebalance_dates(index: pd.DatetimeIndex, freq_days: int) -> pd.DatetimeIndex:
    """Returns rebalance dates every freq_days."""
    return index[::freq_days]


class NaturalLanguageStrategy:
    """Execution engine for Plain English Quantitative Asset Allocation Strategies."""

    def __init__(self, spec_or_text: Union[ParsedStrategySpec, str], config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        if isinstance(spec_or_text, ParsedStrategySpec):
            self.spec = spec_or_text
        else:
            self.spec = parse_plain_english_strategy(spec_or_text)

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        spec = self.spec
        p = params or {}

        rebal_freq = p.get("rebalance_freq_days", spec.rebalance_freq_days)
        cash_proxy = p.get("cash_proxy", spec.cash_proxy)
        top_k = p.get("top_k", spec.top_k)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})

        # Precompute indicators needed
        smas = {}
        if spec.trend_sma_period > 0 or spec.use_canary_logic:
            period = spec.trend_sma_period if spec.trend_sma_period > 0 else 200
            smas = pd.DataFrame({sym: sma(df["Close"], period) for sym, df in universe.items()})

        rocs_short = {}
        if spec.mom_short_lookback > 0:
            rocs_short = pd.DataFrame({sym: roc(df["Close"], spec.mom_short_lookback) for sym, df in universe.items()})

        rocs_long = {}
        long_lookback = spec.trend_roc_lookback if spec.trend_roc_lookback > 0 else spec.mom_long_lookback
        if long_lookback > 0:
            rocs_long = pd.DataFrame({sym: roc(df["Close"], long_lookback) for sym, df in universe.items()})

        vols = {}
        if spec.allocation_scheme == "inverse_volatility":
            vols = pd.DataFrame({sym: realized_vol(df["Close"], spec.vol_lookback) for sym, df in universe.items()})

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        # ---------------------------------------------------------
        # MODE 1: BAA Canary Turbulence Logic
        # ---------------------------------------------------------
        if spec.use_canary_logic:
            canary_symbols = [s for s in spec.canary_universe if s in symbols]
            offensive_symbols = [s for s in spec.offensive_universe if s in symbols]
            defensive_symbols = [s for s in spec.defensive_universe if s in symbols]

            for date in rebalance_dates:
                turbulent = False
                for cs in canary_symbols:
                    c = closes.loc[date, cs]
                    s = smas.loc[date, cs] if isinstance(smas, pd.DataFrame) and cs in smas.columns else np.nan
                    r = rocs_long.loc[date, cs] if isinstance(rocs_long, pd.DataFrame) and cs in rocs_long.columns else np.nan
                    if pd.isna(c) or pd.isna(s) or pd.isna(r) or c <= s or r <= 0:
                        turbulent = True
                        break

                if not turbulent and offensive_symbols:
                    scores = rocs_long.loc[date, offensive_symbols].dropna()
                    if not scores.empty:
                        top_assets = scores.nlargest(min(len(scores), top_k)).index.tolist()
                        w_each = 1.0 / len(top_assets) if top_assets else 0.0
                        for ta in top_assets:
                            weights_rebal.loc[date, ta] = w_each
                else:
                    scores = rocs_long.loc[date, defensive_symbols].dropna()
                    positive_defensive = scores[scores > 0].nlargest(min(len(scores), top_k)).index.tolist()
                    if positive_defensive:
                        w_each = 1.0 / top_k
                        for da in positive_defensive:
                            weights_rebal.loc[date, da] = w_each
                        rem_w = 1.0 - (len(positive_defensive) * w_each)
                        if cash_proxy in symbols and rem_w > 0:
                            weights_rebal.loc[date, cash_proxy] += rem_w
                    else:
                        if cash_proxy in symbols:
                            weights_rebal.loc[date, cash_proxy] = 1.0

        # ---------------------------------------------------------
        # MODE 2: Volatility-Managed Inverse Variance Scaling
        # ---------------------------------------------------------
        elif spec.allocation_scheme == "volatility_managed":
            risky_symbols = [s for s in spec.risky_universe if s in symbols]
            if not risky_symbols:
                risky_symbols = [s for s in symbols if s != cash_proxy]

            returns_df = closes[risky_symbols].pct_change()
            portfolio_return = returns_df.mean(axis=1)
            port_vol = portfolio_return.rolling(spec.var_lookback, min_periods=spec.var_lookback).std() * np.sqrt(252)

            for date in rebalance_dates:
                valid_risky = [s for s in risky_symbols if pd.notna(closes.loc[date, s])]
                n_valid = len(valid_risky)
                if n_valid == 0:
                    continue

                v = port_vol.loc[date]
                scale = min(spec.max_leverage, spec.target_vol / v) if pd.notna(v) and v > 0 else 1.0
                risky_w = scale / n_valid
                for sym in valid_risky:
                    weights_rebal.loc[date, sym] = risky_w

                total_risky_w = weights_rebal.loc[date, risky_symbols].sum()
                cash_w = max(0.0, 1.0 - total_risky_w)
                if cash_proxy in symbols:
                    weights_rebal.loc[date, cash_proxy] = cash_w

        # ---------------------------------------------------------
        # MODE 3: Dual Momentum / Standard Portfolio Allocation
        # ---------------------------------------------------------
        else:
            risky_symbols = [s for s in spec.risky_universe if s in symbols]
            if not risky_symbols:
                risky_symbols = [s for s in symbols if s != cash_proxy]

            composite_mom = pd.DataFrame(index=closes.index, columns=risky_symbols, data=0.0)
            if isinstance(rocs_short, pd.DataFrame) and isinstance(rocs_long, pd.DataFrame):
                composite_mom = 0.5 * rocs_short[risky_symbols] + 0.5 * rocs_long[risky_symbols]

            for date in rebalance_dates:
                passing_symbols = []
                for sym in risky_symbols:
                    c = closes.loc[date, sym]
                    s = smas.loc[date, sym] if isinstance(smas, pd.DataFrame) and sym in smas.columns else np.nan
                    rl = rocs_long.loc[date, sym] if isinstance(rocs_long, pd.DataFrame) and sym in rocs_long.columns else np.nan

                    pass_sma = (c > s) if spec.trend_sma_period > 0 and pd.notna(c) and pd.notna(s) else True
                    pass_roc = (rl > 0) if spec.trend_roc_lookback > 0 and pd.notna(rl) else True

                    if pass_sma and pass_roc and pd.notna(c):
                        passing_symbols.append(sym)

                m = len(passing_symbols)
                if m > 0:
                    sym_scores = composite_mom.loc[date, passing_symbols].dropna()
                    selected = sym_scores.nlargest(min(m, top_k)).index.tolist()

                    if spec.allocation_scheme == "equal_weight":
                        w_each = (1.0 / top_k)
                        for sym in selected:
                            weights_rebal.loc[date, sym] = w_each
                    else:  # "inverse_volatility"
                        selected_vols = vols.loc[date, selected].replace(0, np.nan)
                        inv_v = 1.0 / selected_vols
                        sum_inv_v = inv_v.sum()
                        if sum_inv_v > 0 and pd.notna(sum_inv_v):
                            raw_weights = inv_v / sum_inv_v
                            scale_factor = len(selected) / float(top_k)
                            final_weights = raw_weights * scale_factor
                            for sym in selected:
                                weights_rebal.loc[date, sym] = final_weights[sym]

                total_risky_w = weights_rebal.loc[date, risky_symbols].sum()
                cash_w = max(0.0, 1.0 - total_risky_w)
                if cash_proxy in symbols:
                    weights_rebal.loc[date, cash_proxy] = cash_w

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        return self.spec.format_summary()

    def warmup_bars(self, params: dict = None) -> int:
        return max(200, self.spec.trend_sma_period, self.spec.mom_long_lookback)


# Preset Strategy Wrappers
class ActiveDualMomentumRiskParity(NaturalLanguageStrategy):
    """Preset Active Dual Momentum GTAA + Inverse Volatility Risk Parity Strategy."""
    def __init__(self, config: StrategyConfig = None):
        spec = parse_plain_english_strategy(CANONICAL_DUAL_MOMENTUM_TEXT, name="Active Dual Momentum GTAA")
        super().__init__(spec, config=config)


class BoldAssetAllocation(NaturalLanguageStrategy):
    """Preset Wouter Keller's Bold Asset Allocation (BAA-G12) Strategy."""
    def __init__(self, config: StrategyConfig = None):
        spec = parse_plain_english_strategy(CANONICAL_BAA_KELLER_TEXT, name="Bold Asset Allocation (BAA-G12)")
        super().__init__(spec, config=config)


class VolatilityManagedStrategy(NaturalLanguageStrategy):
    """Preset Moreira & Muir Volatility-Managed Strategy."""
    def __init__(self, config: StrategyConfig = None):
        spec = parse_plain_english_strategy(CANONICAL_VOLATILITY_MANAGED_TEXT, name="Volatility-Managed Portfolio")
        super().__init__(spec, config=config)

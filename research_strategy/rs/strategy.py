"""Researched Quantitative Asset Allocation & Timing Strategy Implementations.

Powered by NaturalLanguageStrategy: A plain English execution engine that interprets
structured strategy rules (ParsedStrategySpec) and dynamically executes backtests.

Canonical Preset Strategies:
1. ActiveDualMomentumRiskParity (Antonacci 2014, Faber 2007)
2. BoldAssetAllocation (Wouter Keller 2022 BAA-G12)
3. VolatilityManagedStrategy (Moreira & Muir 2017)

Standalone Research Strategies:
4. AcceleratingDualMomentum (Ludlow & Hanly 2018, EngineeredPortfolio.com)
5. VigilantAssetAllocation (Keller & Keuning 2017, SSRN #3002624, VAA-G4)

Timing Strategies (ported from workspace side projects & extended for multi-asset):
6. RSIMeanReversionStrategy
7. SwingTrendPullbackStrategy
8. AdaptiveGridStrategy
9. EnsembleRegimeSwitchingStrategy
"""

from typing import Dict, List, Union

import numpy as np
import pandas as pd

from common.indicators import (
    adx,
    atr,
    cumulative_rsi,
    realized_vol,
    roc,
    rsi,
    rsi_cutler,
    rsi_wilder,
    sma,
)
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


def _sparse_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Compress a dense daily target-weight DataFrame to the sparse contract:
    NaN except on a day the target actually differs from the previous day's."""
    changed = (daily != daily.shift(1)).any(axis=1)
    changed.iloc[0] = True
    return daily.where(changed)


def _fill_out_columns(daily: pd.DataFrame, symbols: list) -> pd.DataFrame:
    for s in symbols:
        if s not in daily.columns:
            daily[s] = 0.0
    return daily[symbols]


def _get_risky_symbols(
    universe: Dict[str, pd.DataFrame],
    params: dict,
    cfg_symbol: str,
    cfg_risky_universe: List[str],
    cash_proxy: str,
) -> List[str]:
    """Helper to determine the list of risky symbols to evaluate for timing strategies.

    Precedence: an explicit per-call override via `params` ("symbol" for a
    single ticker, "symbols"/"risky_universe" for a list) always wins.
    Absent any override, the strategy's own configured `<x>_symbol` is
    authoritative for single-symbol trading -- e.g. `RSIMeanReversionStrategy()`
    with zero params trades just "SPY" (matching the original single-asset
    project this was ported from), because `cfg_symbol` is honored whenever
    it's set, NOT treated as a "still at its default" sentinel just because
    it happens to equal the field's own default value. (A prior version
    special-cased the literal string "SPY" as meaning "unset" -- which broke
    every config that named SPY, including the shipped defaults, since a
    string can't distinguish "left at default" from "explicitly chosen".)
    Only when `cfg_symbol` itself is empty/None does this fall back to the
    shared multi-asset `risky_universe` for genuine multi-symbol evaluation."""
    p = params or {}
    if "symbol" in p and p["symbol"]:
        sym = p["symbol"]
        return [sym] if sym in universe else []
    if "symbols" in p and p["symbols"]:
        return [s for s in p["symbols"] if s in universe and s != cash_proxy]
    if "risky_universe" in p and p["risky_universe"]:
        return [s for s in p["risky_universe"] if s in universe and s != cash_proxy]

    if cfg_symbol:
        return [cfg_symbol] if cfg_symbol in universe else []

    candidates = [s for s in cfg_risky_universe if s in universe and s != cash_proxy]
    if not candidates:
        candidates = [s for s in universe if s != cash_proxy]
    return candidates


def _rsi_signal(rsi_value: float, in_position: bool, entry_threshold: float, exit_threshold: float) -> int:
    if pd.isna(rsi_value):
        return 1 if in_position else 0
    if in_position:
        return 0 if rsi_value > exit_threshold else 1
    return 1 if rsi_value < entry_threshold else 0


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

        # MODE 1: BAA Canary Turbulence Logic
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

        # MODE 2: Volatility-Managed Inverse Variance Scaling
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

        # MODE 3: Dual Momentum / Standard Portfolio Allocation
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


class AcceleratingDualMomentum:
    """Accelerating Dual Momentum (ADM).

    Chris Ludlow & Steve Hanly, EngineeredPortfolio.com (2018), popularized
    and independently tracked by AllocateSmartly.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        equity_a = p.get("adm_equity_a", cfg.adm_equity_a)
        equity_b = p.get("adm_equity_b", cfg.adm_equity_b)
        bond_a = p.get("adm_bond_a", cfg.adm_bond_a)
        bond_b = p.get("adm_bond_b", cfg.adm_bond_b)

        symbols = list(universe.keys())
        if equity_a not in symbols or equity_b not in symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        def avg_momentum(sym: str) -> pd.Series:
            close = universe[sym]["Close"]
            return (roc(close, 21) + roc(close, 63) + roc(close, 126)) / 3.0

        mom_a = avg_momentum(equity_a)
        mom_b = avg_momentum(equity_b)
        bond_symbols = [s for s in (bond_a, bond_b) if s in symbols]
        bond_1m = {s: roc(universe[s]["Close"], 21) for s in bond_symbols}

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            a, b = mom_a.loc[date], mom_b.loc[date]
            if pd.notna(a) and pd.notna(b) and a > b and a > 0:
                weights_rebal.loc[date, equity_a] = 1.0
                continue
            if pd.notna(a) and pd.notna(b) and b > a and b > 0:
                weights_rebal.loc[date, equity_b] = 1.0
                continue

            if pd.isna(a) or pd.isna(b):
                continue
            candidates = {s: bond_1m[s].loc[date] for s in bond_symbols if pd.notna(bond_1m[s].loc[date])}
            if candidates:
                best_bond = max(candidates, key=candidates.get)
                weights_rebal.loc[date, best_bond] = 1.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            f"Accelerating Dual Momentum (Ludlow & Hanly 2018): Rebalances every "
            f"{p.get('rebalance_freq_days', cfg.rebalance_freq_days)} days. "
            f"Reasoning: Scores {p.get('adm_equity_a', cfg.adm_equity_a)} and {p.get('adm_equity_b', cfg.adm_equity_b)} "
            f"by the average of their trailing 1/3/6-month returns; holds 100% of whichever has the higher score "
            f"if that score is positive. If neither qualifies, holds 100% of whichever of "
            f"{p.get('adm_bond_a', cfg.adm_bond_a)}/{p.get('adm_bond_b', cfg.adm_bond_b)} has the better trailing "
            f"1-month return. Fully concentrated, single-asset holding -- no partial or cash allocation."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 126


class VigilantAssetAllocation:
    """Vigilant Asset Allocation (VAA-G4).

    Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
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

        def score_13612w(sym: str) -> pd.Series:
            close = universe[sym]["Close"]
            return 12 * roc(close, 21) + 4 * roc(close, 63) + 2 * roc(close, 126) + roc(close, 252)

        scores = pd.DataFrame({sym: score_13612w(sym) for sym in all_tracked}) if all_tracked else pd.DataFrame()

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        for date in rebalance_dates:
            off_scores = scores.loc[date, offensive_symbols].dropna() if offensive_symbols else pd.Series(dtype=float)
            if len(off_scores) < len(offensive_symbols):
                continue

            if not off_scores.empty and (off_scores > 0).all():
                weights_rebal.loc[date, off_scores.idxmax()] = 1.0
            else:
                def_scores = scores.loc[date, defensive_symbols].dropna() if defensive_symbols else pd.Series(dtype=float)
                if not def_scores.empty:
                    weights_rebal.loc[date, def_scores.idxmax()] = 1.0

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
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

    def warmup_bars(self, params: dict = None) -> int:
        return 252


class RSIMeanReversionStrategy:
    """Connors-style RSI(2) long-only mean-reversion strategy (ported from
    the standalone `rsi_strategy` project, extended for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        rsi_period = p.get("rsi_period", cfg.rsi_period)
        rsi_method = p.get("rsi_method", cfg.rsi_method)
        entry_mode = p.get("rsi_entry_mode", cfg.rsi_entry_mode)
        oversold_threshold = p.get("rsi_oversold_threshold", cfg.rsi_oversold_threshold)
        cumulative_lookback = p.get("rsi_cumulative_lookback", cfg.rsi_cumulative_lookback)
        cumulative_threshold = p.get("rsi_cumulative_threshold", cfg.rsi_cumulative_threshold)
        require_trend_filter = p.get("rsi_require_trend_filter", cfg.rsi_require_trend_filter)
        trend_ma_period = p.get("rsi_trend_ma_period", cfg.rsi_trend_ma_period)
        exit_mode = p.get("rsi_exit_mode", cfg.rsi_exit_mode)
        exit_rsi_threshold = p.get("rsi_exit_rsi_threshold", cfg.rsi_exit_rsi_threshold)
        exit_ma_period = p.get("rsi_exit_ma_period", cfg.rsi_exit_ma_period)
        stop_loss_pct = p.get("rsi_stop_loss_pct", cfg.rsi_stop_loss_pct)
        max_holding_days = p.get("rsi_max_holding_days", cfg.rsi_max_holding_days)
        position_size_pct = p.get("rsi_position_size_pct", cfg.rsi_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg.rsi_symbol, cfg.risky_universe, cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = universe[risky_symbols[0]].index
        n_bars = len(master_index)
        raw_weights = {sym: np.zeros(n_bars) for sym in risky_symbols}

        for sym in risky_symbols:
            close = universe[sym]["Close"]
            rsi_series = rsi_wilder(close, rsi_period) if rsi_method == "wilder" else rsi_cutler(close, rsi_period)
            trend_ma = sma(close, trend_ma_period)
            exit_ma = sma(close, exit_ma_period)

            if entry_mode == "cumulative":
                entry_trigger = cumulative_rsi(rsi_series, cumulative_lookback) < cumulative_threshold
            elif entry_mode == "single":
                entry_trigger = rsi_series < oversold_threshold
            else:
                raise ValueError(f"Unknown rsi_entry_mode: {entry_mode!r} (expected 'single' or 'cumulative')")

            trend_ok = (close > trend_ma) if require_trend_filter else pd.Series(True, index=master_index)
            entry_signal = (entry_trigger & trend_ok).fillna(False)

            exit_rsi_ok = rsi_series > exit_rsi_threshold
            exit_ma_ok = close > exit_ma
            if exit_mode == "rsi_cross":
                exit_signal = exit_rsi_ok
            elif exit_mode == "ma_cross":
                exit_signal = exit_ma_ok
            elif exit_mode == "either":
                exit_signal = exit_rsi_ok | exit_ma_ok
            else:
                raise ValueError(f"Unknown rsi_exit_mode: {exit_mode!r} (expected 'rsi_cross', 'ma_cross', or 'either')")
            exit_signal = exit_signal.fillna(False)

            in_position = False
            entry_idx = 0
            for i in range(n_bars):
                if in_position:
                    held = i - entry_idx
                    stopped = stop_loss_pct is not None and (close.iloc[i] / close.iloc[entry_idx] - 1) <= -stop_loss_pct
                    timed_out = max_holding_days is not None and held >= max_holding_days
                    if exit_signal.iloc[i] or stopped or timed_out:
                        in_position = False
                        raw_weights[sym][i] = 0.0
                    else:
                        raw_weights[sym][i] = position_size_pct
                elif entry_signal.iloc[i]:
                    in_position = True
                    entry_idx = i
                    raw_weights[sym][i] = position_size_pct

        daily = pd.DataFrame(raw_weights, index=master_index)
        total_risky_raw = daily.sum(axis=1)
        scale = np.where(total_risky_raw > 1.0, 1.0 / total_risky_raw, 1.0)
        daily = daily.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily.sum(axis=1))

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            f"RSI(2) Mean-Reversion (Connors-style, multi-asset timing): "
            f"long active symbols when RSI({p.get('rsi_period', cfg.rsi_period)}) < "
            f"{p.get('rsi_oversold_threshold', cfg.rsi_oversold_threshold)}"
            f"{' and price > trend SMA' if p.get('rsi_require_trend_filter', cfg.rsi_require_trend_filter) else ''}. "
            f"Exits on RSI cross, stop-loss, or max-holding-days safety net."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("rsi_trend_ma_period", cfg.rsi_trend_ma_period),
            p.get("rsi_exit_ma_period", cfg.rsi_exit_ma_period),
            p.get("rsi_cumulative_lookback", cfg.rsi_cumulative_lookback),
            p.get("rsi_period", cfg.rsi_period),
        )


class SwingTrendPullbackStrategy:
    """Long-only trend-pullback swing strategy (ported from `swing_trend_strategy`,
    extended for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        trend_ma_period = p.get("swing_trend_ma_period", cfg.swing_trend_ma_period)
        require_rising_trend_ma = p.get("swing_require_rising_trend_ma", cfg.swing_require_rising_trend_ma)
        trend_slope_lookback = p.get("swing_trend_slope_lookback", cfg.swing_trend_slope_lookback)
        pullback_ma_period = p.get("swing_pullback_ma_period", cfg.swing_pullback_ma_period)
        rsi_period = p.get("swing_rsi_period", cfg.swing_rsi_period)
        entry_rsi_threshold = p.get("swing_entry_rsi_threshold", cfg.swing_entry_rsi_threshold)
        exit_rsi_threshold = p.get("swing_exit_rsi_threshold", cfg.swing_exit_rsi_threshold)
        stop_loss_pct = p.get("swing_stop_loss_pct", cfg.swing_stop_loss_pct)
        reward_risk_ratio = p.get("swing_reward_risk_ratio", cfg.swing_reward_risk_ratio)
        use_trailing_stop = p.get("swing_use_trailing_stop", cfg.swing_use_trailing_stop)
        trailing_activate_pct = p.get("swing_trailing_activate_pct", cfg.swing_trailing_activate_pct)
        trailing_stop_pct = p.get("swing_trailing_stop_pct", cfg.swing_trailing_stop_pct)
        max_holding_days = p.get("swing_max_holding_days", cfg.swing_max_holding_days)
        position_size_pct = p.get("swing_position_size_pct", cfg.swing_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg.swing_symbol, cfg.risky_universe, cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = universe[risky_symbols[0]].index
        n_bars = len(master_index)
        profit_target_pct = stop_loss_pct * reward_risk_ratio
        raw_weights = {sym: np.zeros(n_bars) for sym in risky_symbols}

        for sym in risky_symbols:
            close = universe[sym]["Close"]
            trend_ma = sma(close, trend_ma_period)
            pullback_ma = sma(close, pullback_ma_period)
            rsi_series = rsi(close, rsi_period)

            trend_ok = close > trend_ma
            if require_rising_trend_ma:
                trend_ok = trend_ok & (trend_ma > trend_ma.shift(trend_slope_lookback))
            pullback_ok = close < pullback_ma
            rsi_ok = rsi_series < entry_rsi_threshold
            entry_signal = (trend_ok & pullback_ok & rsi_ok).fillna(False)
            exit_signal = (rsi_series > exit_rsi_threshold).fillna(False)

            in_position = False
            entry_idx = 0
            peak_price = 0.0
            for i in range(n_bars):
                c = close.iloc[i]
                if in_position:
                    entry_price = close.iloc[entry_idx]
                    peak_price = max(peak_price, c)
                    held = i - entry_idx
                    stopped = c <= entry_price * (1 - stop_loss_pct)
                    targeted = c >= entry_price * (1 + profit_target_pct)
                    trailed = (use_trailing_stop and (peak_price / entry_price - 1) >= trailing_activate_pct
                               and c <= peak_price * (1 - trailing_stop_pct))
                    timed_out = max_holding_days is not None and held >= max_holding_days
                    if stopped or targeted or trailed or exit_signal.iloc[i] or timed_out:
                        in_position = False
                        raw_weights[sym][i] = 0.0
                    else:
                        raw_weights[sym][i] = position_size_pct
                elif entry_signal.iloc[i]:
                    in_position = True
                    entry_idx = i
                    peak_price = c
                    raw_weights[sym][i] = position_size_pct

        daily = pd.DataFrame(raw_weights, index=master_index)
        total_risky_raw = daily.sum(axis=1)
        scale = np.where(total_risky_raw > 1.0, 1.0 / total_risky_raw, 1.0)
        daily = daily.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily.sum(axis=1))

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            f"Trend-Pullback Swing (multi-asset timing): buy dips (close < "
            f"{p.get('swing_pullback_ma_period', cfg.swing_pullback_ma_period)}-day SMA, RSI < "
            f"{p.get('swing_entry_rsi_threshold', cfg.swing_entry_rsi_threshold)}) within confirmed uptrend "
            f"(close > rising {p.get('swing_trend_ma_period', cfg.swing_trend_ma_period)}-day SMA). "
            f"Exits on stop-loss, profit target, trailing stop, or max holding period."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("swing_trend_ma_period", cfg.swing_trend_ma_period),
            p.get("swing_trend_slope_lookback", cfg.swing_trend_slope_lookback),
        )


class AdaptiveGridStrategy:
    """ATR-adaptive grid trading strategy (ported from `grid_trading`, extended
    for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        levels_per_side = p.get("grid_levels_per_side", cfg.grid_levels_per_side)
        atr_period = p.get("grid_atr_period", cfg.grid_atr_period)
        atr_multiplier = p.get("grid_atr_multiplier", cfg.grid_atr_multiplier)
        min_spacing_pct = p.get("grid_min_spacing_pct", cfg.grid_min_spacing_pct)
        max_spacing_pct = p.get("grid_max_spacing_pct", cfg.grid_max_spacing_pct)
        position_size_pct = p.get("grid_position_size_pct", cfg.grid_position_size_pct)
        capital_reserve_pct = p.get("grid_capital_reserve_pct", cfg.grid_capital_reserve_pct)
        max_open_slots = p.get("grid_max_open_slots", cfg.grid_max_open_slots)
        trend_ma_period = p.get("grid_trend_ma_period", cfg.grid_trend_ma_period)
        trend_band_pct = p.get("grid_trend_band_pct", cfg.grid_trend_band_pct)
        regrid_breakout_mult = p.get("grid_regrid_breakout_mult", cfg.grid_regrid_breakout_mult)
        regrid_on_profit_cycle = p.get("grid_regrid_on_profit_cycle", cfg.grid_regrid_on_profit_cycle)
        drawdown_stop_pct = p.get("grid_drawdown_stop_pct", cfg.grid_drawdown_stop_pct)
        cooldown_bars_after_stop = p.get("grid_cooldown_bars_after_stop", cfg.grid_cooldown_bars_after_stop)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg.grid_symbol, cfg.risky_universe, cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = universe[risky_symbols[0]].index
        n_bars = len(master_index)
        max_deployed = 1.0 - capital_reserve_pct
        raw_weights = {sym: np.zeros(n_bars) for sym in risky_symbols}

        def build_grid(center, spacing_abs):
            lv = [center + i * spacing_abs for i in range(-levels_per_side, levels_per_side + 1)]
            return lv, [False] * (len(lv) - 1)

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            atr_series = atr(df, atr_period)
            trend_ma = sma(close, trend_ma_period)

            levels = None
            slot_state = None
            notional_equity = 1.0
            peak_equity = 1.0
            cooldown_until = -1

            for i in range(n_bars):
                c = close.iloc[i]
                a = atr_series.iloc[i]
                ma = trend_ma.iloc[i]

                if pd.isna(a) or pd.isna(ma) or pd.isna(c) or c <= 0:
                    continue

                spacing_pct = max(min_spacing_pct, min(max_spacing_pct, (a / c) * atr_multiplier))
                spacing_abs = spacing_pct * c
                n_open = sum(slot_state) if slot_state else 0

                if levels is None or (n_open == 0 and regrid_on_profit_cycle):
                    levels, slot_state = build_grid(c, spacing_abs)
                else:
                    center = (levels[0] + levels[-1]) / 2.0
                    span = (levels[-1] - levels[0]) / 2.0
                    if c > center + span * regrid_breakout_mult or c < center - span * regrid_breakout_mult:
                        slot_state = [False] * len(slot_state)
                        levels, slot_state = build_grid(c, spacing_abs)

                in_cooldown = i <= cooldown_until
                if c > ma * (1 + trend_band_pct):
                    trend = "up"
                elif c < ma * (1 - trend_band_pct):
                    trend = "down"
                else:
                    trend = "range"
                allow_new_entries = (not in_cooldown) and (trend != "down")

                for j in range(len(slot_state)):
                    if slot_state[j] and c >= levels[j + 1]:
                        slot_state[j] = False

                if allow_new_entries:
                    center = (levels[0] + levels[-1]) / 2.0
                    n_open = sum(slot_state)
                    candidates = sorted(
                        (j for j in range(len(slot_state)) if not slot_state[j] and c <= levels[j]),
                        key=lambda j: abs(levels[j] - center),
                    )
                    for j in candidates:
                        if n_open >= max_open_slots or (n_open + 1) * position_size_pct > max_deployed:
                            break
                        slot_state[j] = True
                        n_open += 1

                raw_weights[sym][i] = min(1.0, sum(slot_state) * position_size_pct)

                if i > 0 and pd.notna(close.iloc[i - 1]) and close.iloc[i - 1] > 0:
                    day_ret = c / close.iloc[i - 1] - 1.0
                    notional_equity *= (1.0 + raw_weights[sym][i - 1] * day_ret)
                peak_equity = max(peak_equity, notional_equity)
                drawdown = (peak_equity - notional_equity) / peak_equity if peak_equity > 0 else 0.0

                if drawdown >= drawdown_stop_pct and not in_cooldown:
                    slot_state = [False] * len(slot_state)
                    raw_weights[sym][i] = 0.0
                    cooldown_until = i + cooldown_bars_after_stop

        daily = pd.DataFrame(raw_weights, index=master_index)
        total_risky_raw = daily.sum(axis=1)
        scale = np.where(total_risky_raw > max_deployed, max_deployed / total_risky_raw, 1.0)
        daily = daily.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily.sum(axis=1))

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            f"ATR-Adaptive Grid (multi-asset timing): builds buy-low/sell-high ATR grids "
            f"across active risky symbols. Gated by trend filter and drawdown circuit breaker per symbol."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("grid_atr_period", cfg.grid_atr_period),
            p.get("grid_trend_ma_period", cfg.grid_trend_ma_period),
        )


class EnsembleRegimeSwitchingStrategy:
    """Regime-switching ensemble (ported from `ensemble_strategy`, extended for
    multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        mode = p.get("ensemble_mode", cfg.ensemble_mode)
        trend_ma_period = p.get("ensemble_trend_ma_period", cfg.ensemble_trend_ma_period)
        adx_period = p.get("ensemble_adx_period", cfg.ensemble_adx_period)
        adx_trend_threshold = p.get("ensemble_adx_trend_threshold", cfg.ensemble_adx_trend_threshold)
        adx_range_threshold = p.get("ensemble_adx_range_threshold", cfg.ensemble_adx_range_threshold)
        rsi_period = p.get("ensemble_rsi_period", cfg.ensemble_rsi_period)
        entry_rsi_threshold = p.get("ensemble_entry_rsi_threshold", cfg.ensemble_entry_rsi_threshold)
        exit_rsi_threshold = p.get("ensemble_exit_rsi_threshold", cfg.ensemble_exit_rsi_threshold)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg.ensemble_symbol, cfg.risky_universe, cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = universe[risky_symbols[0]].index
        n_bars = len(master_index)
        raw_weights = {sym: np.zeros(n_bars) for sym in risky_symbols}

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            trend_ma = sma(close, trend_ma_period)
            adx_series = adx(df, adx_period)
            rsi_series = rsi(close, rsi_period)

            long_term_uptrend = close > trend_ma
            raw_sub = pd.Series(np.where(
                adx_series >= adx_trend_threshold, "trend",
                np.where(adx_series <= adx_range_threshold, "range", None)
            ), index=master_index, dtype=object).ffill().fillna("range")
            regime = pd.Series(np.where(long_term_uptrend, raw_sub, "downtrend"), index=master_index)

            regime = regime.shift(1)
            rsi_shifted = rsi_series.shift(1)

            in_position = False
            for i in range(n_bars):
                r = regime.iloc[i]
                if pd.isna(r):
                    in_position = False
                    continue
                rv = rsi_shifted.iloc[i]
                if mode == "trend_only":
                    desired = 1 if r != "downtrend" else 0
                elif mode == "meanrev_only":
                    desired = 0 if r == "downtrend" else _rsi_signal(rv, in_position, entry_rsi_threshold, exit_rsi_threshold)
                elif mode == "ensemble":
                    if r == "downtrend":
                        desired = 0
                    elif r == "trend":
                        desired = 1
                    else:
                        desired = _rsi_signal(rv, in_position, entry_rsi_threshold, exit_rsi_threshold)
                else:
                    raise ValueError(f"Unknown ensemble_mode: {mode!r} (expected 'ensemble', 'trend_only', or 'meanrev_only')")
                raw_weights[sym][i] = float(desired)
                in_position = desired == 1

        daily = pd.DataFrame(raw_weights, index=master_index)
        n_active = daily.sum(axis=1)
        scale = np.where(n_active > 0, 1.0 / np.maximum(1.0, n_active), 0.0)
        daily = daily.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily.sum(axis=1))

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        mode = p.get("ensemble_mode", cfg.ensemble_mode)
        return (
            f"Regime-Switching Ensemble (multi-asset timing, mode={mode}): "
            f"equal-weights active signals across risky symbols based on ADX regime classification."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("ensemble_trend_ma_period", cfg.ensemble_trend_ma_period),
            p.get("ensemble_adx_period", cfg.ensemble_adx_period),
            p.get("ensemble_rsi_period", cfg.ensemble_rsi_period),
        ) + 1

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
10. TurtleBreakoutStrategy (Dennis & Eckhardt / Donchian)

Modern Popular Portfolios (static, fixed-weight, no momentum/trend logic):
11. PermanentPortfolioStrategy (Harry Browne)
12. GoldenButterflyStrategy (Portfolio Charts)
13. AllWeatherStrategy (Dalio-style retail risk-parity approximation)
14. HFEAStrategy ("Hedgefundie's Excellent Adventure", leveraged UPRO/TMF)

Modern Systematic TAA Extensions:
15. ProtectiveAssetAllocation (Keller & Keuning 2016, SSRN #2759734, PAA)
16. AdaptiveAssetAllocation (Butler/Philbrick/Gordillo/Varadi 2012, SSRN #2328254, AAA)

Original Structural Strategies (independent from-scratch implementations,
not ports of any workspace side project or third-party library):
17. ChanPivotShiftStrategy (缠中说禅/Chan-theory pivot-shift reading, see `chan_structure.py`)
18. ChanThreeTypeStrategy (additive extension of #17: segments, real MACD
    divergence, formal 一/二/三类买卖点 taxonomy -- see `chan_signals.py`)
19. ChanPivotShiftMACDStrategy (additive copy of #17: same pivot-shift buy/sell
    rule, but its disclosed stroke-slope divergence proxy replaced by real
    MACD divergence, made symmetric -- see `chan_signals.py`)

Value-Investing-Adapted Strategies:
20. CompounderMarginOfSafetyStrategy (price-only proxy of a conservative valuation
    framework, see `docs/snowball_strategy.txt`; the real-fundamentals version lives
    in the separate `fundamental_screener` project)
"""

from typing import Dict, List, Union
import warnings

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
from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _fill_out_columns,
    _inverse_vol_weights,
    _min_variance_weights,
    _sparse_from_daily,
)
from common.position_exits import run_stop_timeout_exit
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from .chan_structure import compute_chan_signals
from .chan_signals import compute_chan3_signals, compute_chan_pivot_macd_signals
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
    Absent any override, `cfg_symbol` (when non-empty) is honored for
    single-symbol trading. Passing `cfg_symbol=None, cfg_risky_universe=None`
    deliberately skips both hardcoded-symbol branches and lands on the final
    fallback below -- every symbol actually present in the passed `universe`,
    minus `cash_proxy` -- which is what the 8 single-instrument atomic timing
    strategies (RSI/Swing/Grid/Ensemble/Turtle/Chan x3) now pass, since each
    already independently recomputes its own entry/exit signal per-symbol in
    its own `generate_weights` loop, making a whole-universe basket coherent.
    `CompositeTimingTemplate` (rs/timing_aspects.py) must NOT do this -- its
    entry/exit aspect functions compute ONE signal off a single resolved `df`,
    so resolving more than one symbol there would nonsensically broadcast
    that one signal across unrelated tickers (see
    test_composite_timing_only_trades_entry_aspects_own_symbol_not_full_risky_universe);
    it always passes its real `cfg_symbol`/`risky_universe` values."""
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

    candidates = [s for s in (cfg_risky_universe or []) if s in universe and s != cash_proxy]
    if not candidates:
        candidates = [s for s in universe if s != cash_proxy]
    return candidates


def _aligned_master_index(universe: Dict[str, pd.DataFrame], risky_symbols: List[str]) -> pd.DatetimeIndex:
    """Real (and especially multi-symbol) market data isn't guaranteed to
    share one exact trading calendar -- different listing dates, holiday
    calendars (e.g. international ETFs), or provider gaps can leave one
    symbol's index a few bars shorter/longer than another's even within the
    same loaded universe. Now that `_get_risky_symbols` can return more than
    one symbol by default (the basket behavior), every per-symbol raw-weight
    array must be built and then aligned onto ONE common index before they
    can be combined into a single DataFrame -- use the intersection of every
    risky symbol's own index, the same alignment `backtester/run_backtest.py`'s
    `_align_universe` already applies for walk-forward. For the (still common)
    single-symbol case this is exactly that symbol's own index, unchanged."""
    common_index = universe[risky_symbols[0]].index
    for sym in risky_symbols[1:]:
        common_index = common_index.intersection(universe[sym].index)
    return common_index


def _rsi_signal(rsi_value: float, in_position: bool, entry_threshold: float, exit_threshold: float) -> int:
    if pd.isna(rsi_value):
        return 1 if in_position else 0
    if in_position:
        return 0 if rsi_value > exit_threshold else 1
    return 1 if rsi_value < entry_threshold else 0


class NaturalLanguageStrategy(AllocationTemplate):
    """Execution engine for Plain English Quantitative Asset Allocation Strategies."""

    def __init__(self, spec_or_text: Union[ParsedStrategySpec, str], config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        if isinstance(spec_or_text, ParsedStrategySpec):
            self.spec = spec_or_text
        else:
            self.spec = parse_plain_english_strategy(spec_or_text)
        super().__init__(name=self.spec.strategy_name, param_grid={})

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

                # Three distinct cases, kept separate so that "no eligible
                # offensive candidates" (a universe/config narrowing issue)
                # is never mischaracterized as "market is turbulent" (a
                # canary-signal outcome) -- they used to share the same
                # `else` branch below, which silently routed a CALM market
                # with an empty `offensive_symbols` list (e.g. a custom
                # config that narrows the offensive list independently of
                # the canary list) into the defensive-allocation logic as if
                # the canary had actually signalled turbulence.
                if turbulent:
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
                elif offensive_symbols:
                    scores = rocs_long.loc[date, offensive_symbols].dropna()
                    if not scores.empty:
                        top_assets = scores.nlargest(min(len(scores), top_k)).index.tolist()
                        w_each = 1.0 / len(top_assets) if top_assets else 0.0
                        for ta in top_assets:
                            weights_rebal.loc[date, ta] = w_each
                    elif cash_proxy in symbols:
                        # Market is calm, but none of the configured
                        # offensive assets has a usable score this
                        # rebalance (e.g. still warming up) -- a
                        # data-availability fallback, not a turbulent call.
                        weights_rebal.loc[date, cash_proxy] = 1.0
                else:
                    # Market is calm (not turbulent) but there are no
                    # offensive candidates configured/eligible at all.
                    # Fall back to cash without claiming turbulence.
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
                        scale_factor = len(selected) / float(top_k)
                        final_weights = _inverse_vol_weights(vols.loc[date, selected], scale=scale_factor, on_invalid="zero")
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


class AcceleratingDualMomentum(AllocationTemplate):
    """Accelerating Dual Momentum (ADM).

    Chris Ludlow & Steve Hanly, EngineeredPortfolio.com (2018), popularized
    and independently tracked by AllocateSmartly.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="accelerating_dual_momentum", param_grid={})

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


class VigilantAssetAllocation(AllocationTemplate):
    """Vigilant Asset Allocation (VAA-G4).

    Wouter J. Keller & Jan Willem Keuning (2017, SSRN #3002624).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="vigilant_asset_allocation", param_grid={})

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


class RSIMeanReversionStrategy(AllocationTemplate):
    """Connors-style RSI(2) long-only mean-reversion strategy (ported from
    the standalone `rsi_strategy` project, extended for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="rsi_mean_reversion", param_grid={})

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
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        raw_weights = {}

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

            trend_ok = (close > trend_ma) if require_trend_filter else pd.Series(True, index=close.index)
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

            raw = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )
            raw_weights[sym] = pd.Series(raw, index=close.index).reindex(master_index).fillna(0.0).to_numpy()

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

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


class SwingTrendPullbackStrategy(AllocationTemplate):
    """Long-only trend-pullback swing strategy (ported from `swing_trend_strategy`,
    extended for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="swing_trend_pullback", param_grid={})

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
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        profit_target_pct = stop_loss_pct * reward_risk_ratio
        raw_weights = {}

        for sym in risky_symbols:
            close = universe[sym]["Close"]
            n_bars = len(close)
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

            raw = np.zeros(n_bars)
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
                        raw[i] = 0.0
                    else:
                        raw[i] = position_size_pct
                elif entry_signal.iloc[i]:
                    in_position = True
                    entry_idx = i
                    peak_price = c
                    raw[i] = position_size_pct

            raw_weights[sym] = pd.Series(raw, index=close.index).reindex(master_index).fillna(0.0).to_numpy()

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

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


class ChanPivotShiftStrategy(AllocationTemplate):
    """Long-only timing strategy reading 缠中说禅 ("Chan theory") price
    structure -- inclusion-merged bars -> fractals -> strokes -> pivots (see
    `rs/chan_structure.py`) -- and trading a "pivot shift" view of trend:
    enter once the price's consolidation range (pivot) steps up to a wholly
    higher band and a confirming pullback low forms; exit on a symmetric
    downward pivot shift, a stroke-over-stroke momentum-divergence proxy, a
    stop-loss, or a max-holding-days safety net.

    This is an original, from-scratch reading of Chan theory written
    natively for this project -- it does NOT port, or reuse any code,
    formula, or default from the `czsc` Rust/Python library (third-party
    prior art on the same theory this workspace is aware of but does not
    depend on); its "divergence" signal is a simple stroke slope/length
    ratio, not that library's SNR/rsq metrics. See `chan_structure.py` for
    the disclosed simplifications in the underlying structure detector.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_pivot_shift", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan_min_gap_bars", cfg.chan_min_gap_bars)
        min_strokes = p.get("chan_min_strokes", cfg.chan_min_strokes)
        stop_loss_pct = p.get("chan_stop_loss_pct", cfg.chan_stop_loss_pct)
        max_holding_days = p.get("chan_max_holding_days", cfg.chan_max_holding_days)
        position_size_pct = p.get("chan_position_size_pct", cfg.chan_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan_signals(bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes)
            entry_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan-theory pivot shift (original structural reading, not a czsc port): "
            "long active symbols once their price pivot (consolidation range) steps up to a "
            f"wholly higher band (min {p.get('chan_min_strokes', cfg.chan_min_strokes)} strokes/pivot, "
            f"{p.get('chan_min_gap_bars', cfg.chan_min_gap_bars)}-bar fractal independence gap) and a "
            "confirming pullback low forms. Exits on a symmetric downward pivot shift, a stroke-over-stroke "
            "momentum-divergence proxy, a stop-loss, or a max-holding-days safety net."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan_min_gap_bars", cfg.chan_min_gap_bars)
        min_strokes = p.get("chan_min_strokes", cfg.chan_min_strokes)
        return min_strokes * 2 * (min_gap_bars + 2)


class ChanThreeTypeStrategy(AllocationTemplate):
    """Long-only timing strategy trading the formal 一/二/三类买卖点 (first/
    second/third-type buy/sell point) taxonomy from 缠中说禅 ("Chan theory"),
    built on segments (线段) and segment-level pivots (中枢) with real
    MACD-histogram-area divergence (背驰) -- see `rs/chan_signals.py` for the
    full structure detector and its disclosed simplifications.

    This is an ADDITIVE extension, not a modification, of
    `ChanPivotShiftStrategy`/`chan_structure.py`: it is its own independent
    reading of the theory, one level closer to the formal taxonomy (real
    divergence via `common.indicators.macd`, rather than that strategy's
    disclosed stroke-slope/length "momentum divergence proxy"), coexisting
    with it rather than replacing it. Entry fires on any of the three
    buy-point types (an unconditional OR, no per-type toggle in this first
    version); exit is the symmetric OR of the three sell-point types, a
    stop-loss, or a max-holding-days safety net.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_three_type", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chan3_min_gap_bars", cfg.chan3_min_gap_bars)
        min_strokes = p.get("chan3_min_strokes", cfg.chan3_min_strokes)
        macd_fast = p.get("chan3_macd_fast", cfg.chan3_macd_fast)
        macd_slow = p.get("chan3_macd_slow", cfg.chan3_macd_slow)
        macd_signal = p.get("chan3_macd_signal", cfg.chan3_macd_signal)
        stop_loss_pct = p.get("chan3_stop_loss_pct", cfg.chan3_stop_loss_pct)
        max_holding_days = p.get("chan3_max_holding_days", cfg.chan3_max_holding_days)
        position_size_pct = p.get("chan3_position_size_pct", cfg.chan3_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan3_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            )
            entry_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan three-type buy/sell points (segments + segment-level pivots + real MACD divergence, "
            "an additive extension of chan_pivot_shift -- not a czsc port): long active symbols on any "
            "first-type (pivot breakdown/breakout confirmed by MACD-histogram-area divergence), "
            "second-type (a failed follow-through after a first-type point), or third-type (a breakout "
            f"retest that holds the pivot's own band edge) buy point (min {p.get('chan3_min_strokes', cfg.chan3_min_strokes)} "
            f"strokes/segment/pivot, {p.get('chan3_min_gap_bars', cfg.chan3_min_gap_bars)}-bar fractal independence gap). "
            "Exits on the symmetric sell-point types, a stop-loss, or a max-holding-days safety net."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chan3_min_gap_bars", cfg.chan3_min_gap_bars)
        min_strokes = p.get("chan3_min_strokes", cfg.chan3_min_strokes)
        macd_slow = p.get("chan3_macd_slow", cfg.chan3_macd_slow)
        macd_signal = p.get("chan3_macd_signal", cfg.chan3_macd_signal)
        # Segments recurse one extra level deeper than chan_pivot_shift's own
        # stroke-level pivots (a pivot now needs min_strokes SEGMENTS, each
        # needing min_strokes STROKES), plus a small buffer for the
        # entering/leaving legs a first/third-type point also needs beyond
        # the pivot's own window; must also clear MACD's own EMA warm-up.
        structural = (min_strokes**2) * 2 * (min_gap_bars + 2) + 2 * (min_gap_bars + 2)
        macd_floor = macd_slow + macd_signal + 10
        return max(structural, macd_floor)


class ChanPivotShiftMACDStrategy(AllocationTemplate):
    """Long-only timing strategy that is a near-literal copy of
    `ChanPivotShiftStrategy`'s pivot-band-shift buy/sell rule (stroke-based
    pivots -- deliberately NOT segments, unlike `ChanThreeTypeStrategy`), with
    its disclosed stroke-slope/length "momentum divergence proxy" replaced by
    real MACD-histogram-area divergence (`common.indicators.macd`, via
    `rs/chan_signals.py`'s `compute_chan_pivot_macd_signals`).

    ADDITIVE, not a modification: `ChanPivotShiftStrategy`/`chan_structure.py`
    are left exactly as they are. One deliberate difference beyond the proxy
    swap: this strategy is SYMMETRIC (a top-divergence sell AND a
    bottom-divergence buy), where the original proxy only ever produced a
    sell signal (checked only on up-strokes) -- real divergence makes both
    directions equally cheap to compute, so both are wired in here.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="chan_pivot_shift_macd", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        min_gap_bars = p.get("chanm_min_gap_bars", cfg.chanm_min_gap_bars)
        min_strokes = p.get("chanm_min_strokes", cfg.chanm_min_strokes)
        macd_fast = p.get("chanm_macd_fast", cfg.chanm_macd_fast)
        macd_slow = p.get("chanm_macd_slow", cfg.chanm_macd_slow)
        macd_signal = p.get("chanm_macd_signal", cfg.chanm_macd_signal)
        stop_loss_pct = p.get("chanm_stop_loss_pct", cfg.chanm_stop_loss_pct)
        max_holding_days = p.get("chanm_max_holding_days", cfg.chanm_max_holding_days)
        position_size_pct = p.get("chanm_position_size_pct", cfg.chanm_position_size_pct)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            bars = universe[sym]
            sig = compute_chan_pivot_macd_signals(
                bars, min_gap_bars=min_gap_bars, min_strokes=min_strokes,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            )
            entry_signal = sig["buy_signal"].reindex(master_index).fillna(False)
            exit_signal = sig["sell_signal"].reindex(master_index).fillna(False)
            close = bars["Close"].reindex(master_index)

            raw_weights[sym] = run_stop_timeout_exit(
                close, entry_signal, exit_signal, stop_loss_pct, max_holding_days, position_size_pct
            )

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        return (
            "Chan-theory pivot shift with real MACD divergence (a copy of chan_pivot_shift with its "
            "disclosed stroke-slope divergence proxy replaced by real MACD-histogram-area divergence): "
            "long active symbols once their price pivot steps up to a wholly higher band "
            f"(min {p.get('chanm_min_strokes', cfg.chanm_min_strokes)} strokes/pivot, "
            f"{p.get('chanm_min_gap_bars', cfg.chanm_min_gap_bars)}-bar fractal independence gap) and a "
            "confirming pullback low forms, OR a bottom-divergence buy point (a new low on weaker MACD "
            "momentum). Exits on a symmetric downward pivot shift, a top-divergence sell point, a "
            "stop-loss, or a max-holding-days safety net."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        min_gap_bars = p.get("chanm_min_gap_bars", cfg.chanm_min_gap_bars)
        min_strokes = p.get("chanm_min_strokes", cfg.chanm_min_strokes)
        macd_slow = p.get("chanm_macd_slow", cfg.chanm_macd_slow)
        macd_signal = p.get("chanm_macd_signal", cfg.chanm_macd_signal)
        structural = min_strokes * 2 * (min_gap_bars + 2)
        macd_floor = macd_slow + macd_signal + 10
        return max(structural, macd_floor)


class AdaptiveGridStrategy(AllocationTemplate):
    """ATR-adaptive grid trading strategy (ported from `grid_trading`, extended
    for multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="adaptive_grid", param_grid={})

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
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        max_deployed = 1.0 - capital_reserve_pct
        raw_weights = {}

        def build_grid(center, spacing_abs):
            lv = [center + i * spacing_abs for i in range(-levels_per_side, levels_per_side + 1)]
            return lv, [False] * (len(lv) - 1)

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            n_bars = len(close)
            atr_series = atr(df, atr_period)
            trend_ma = sma(close, trend_ma_period)

            raw = np.zeros(n_bars)
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

                raw[i] = min(1.0, sum(slot_state) * position_size_pct)

                if i > 0 and pd.notna(close.iloc[i - 1]) and close.iloc[i - 1] > 0:
                    day_ret = c / close.iloc[i - 1] - 1.0
                    notional_equity *= (1.0 + raw[i - 1] * day_ret)
                peak_equity = max(peak_equity, notional_equity)
                drawdown = (peak_equity - notional_equity) / peak_equity if peak_equity > 0 else 0.0

                if drawdown >= drawdown_stop_pct and not in_cooldown:
                    slot_state = [False] * len(slot_state)
                    raw[i] = 0.0
                    cooldown_until = i + cooldown_bars_after_stop

            raw_weights[sym] = pd.Series(raw, index=close.index).reindex(master_index).fillna(0.0).to_numpy()

        daily = pd.DataFrame(raw_weights, index=master_index)
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy, cap=max_deployed, cash_target=1.0)

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


class EnsembleRegimeSwitchingStrategy(AllocationTemplate):
    """Regime-switching ensemble (ported from `ensemble_strategy`, extended for
    multi-asset evaluation).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="ensemble_regime_switching", param_grid={})

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
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)
        raw_weights = {}

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            n_bars = len(close)
            trend_ma = sma(close, trend_ma_period)
            adx_series = adx(df, adx_period)
            rsi_series = rsi(close, rsi_period)

            long_term_uptrend = close > trend_ma
            raw_sub = pd.Series(np.where(
                adx_series >= adx_trend_threshold, "trend",
                np.where(adx_series <= adx_range_threshold, "range", None)
            ), index=close.index, dtype=object).ffill().fillna("range")
            regime = pd.Series(np.where(long_term_uptrend, raw_sub, "downtrend"), index=close.index)

            regime = regime.shift(1)
            rsi_shifted = rsi_series.shift(1)

            raw = np.zeros(n_bars)
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
                raw[i] = float(desired)
                in_position = desired == 1

            raw_weights[sym] = pd.Series(raw, index=close.index).reindex(master_index).fillna(0.0).to_numpy()

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


class TurtleBreakoutStrategy(AllocationTemplate):
    """Classic Turtle Channel Breakout Strategy (Dennis & Eckhardt / Donchian).

    Supports System 1 (20-day entry / 10-day exit) and System 2 (55-day entry / 20-day exit)
    with 2N ATR trailing stop, optional 200d SMA trend filter, and inverse ATR risk weighting.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="turtle_breakout", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        entry_breakout_days = p.get("turtle_entry_breakout_days", cfg.turtle_entry_breakout_days)
        exit_breakout_days = p.get("turtle_exit_breakout_days", cfg.turtle_exit_breakout_days)
        atr_period = p.get("turtle_atr_period", cfg.turtle_atr_period)
        atr_stop_mult = p.get("turtle_atr_stop_mult", cfg.turtle_atr_stop_mult)
        require_trend_filter = p.get("turtle_require_trend_filter", cfg.turtle_require_trend_filter)
        trend_ma_period = p.get("turtle_trend_ma_period", cfg.turtle_trend_ma_period)
        position_sizing_mode = p.get("turtle_position_sizing_mode", cfg.turtle_position_sizing_mode)

        symbols = list(universe.keys())
        risky_symbols = _get_risky_symbols(universe, params, cfg_symbol=None, cfg_risky_universe=None, cash_proxy=cash_proxy)
        if not risky_symbols:
            return pd.DataFrame()

        master_index = _aligned_master_index(universe, risky_symbols)

        active_mask = {}
        vol_weights = {}

        for sym in risky_symbols:
            df = universe[sym]
            close = df["Close"]
            high = df["High"]
            low = df["Low"]
            n_bars = len(close)

            atr_series = atr(df, atr_period)
            trend_ma = sma(close, trend_ma_period) if require_trend_filter else None

            donchian_high = high.shift(1).rolling(entry_breakout_days).max()
            donchian_low = low.shift(1).rolling(exit_breakout_days).min()

            in_position = False
            peak_price = 0.0
            active = np.zeros(n_bars, dtype=bool)
            vol = np.zeros(n_bars, dtype=float)

            for i in range(n_bars):
                c = close.iloc[i]
                a = atr_series.iloc[i]

                if pd.isna(c) or c <= 0 or pd.isna(a) or a <= 0:
                    in_position = False
                    continue

                ma = trend_ma.iloc[i] if require_trend_filter else None
                if require_trend_filter and pd.isna(ma):
                    in_position = False
                    continue

                # donchian_high/donchian_low are already shift(1)'d before the
                # rolling window (built from bars before i, never bar i
                # itself), so comparing them against bar i's OWN close here
                # is the full and correct amount of lag -- matching the
                # README's documented formula (Close(t) vs. the channel
                # through t-1) and every sibling strategy in this file
                # (RSI/Swing/Grid/Ensemble all decide day i's state from day
                # i's own already-lagged indicators). Using [i-1] values here
                # on top of that would double the lag, delaying entries,
                # exits, and stops by a full extra trading day.
                dh = donchian_high.iloc[i]
                dl = donchian_low.iloc[i]

                if in_position:
                    peak_price = max(peak_price, high.iloc[i])
                    stop_price = peak_price - atr_stop_mult * a
                    donchian_exit = pd.notna(dl) and c < dl
                    atr_exit = c < stop_price

                    if donchian_exit or atr_exit:
                        in_position = False
                else:
                    entry_breakout = pd.notna(dh) and c > dh
                    trend_ok = (not require_trend_filter) or (pd.notna(ma) and c > ma)
                    if entry_breakout and trend_ok:
                        in_position = True
                        peak_price = high.iloc[i]

                active[i] = in_position
                vol[i] = (c / a) if (in_position and a > 0) else 0.0

            active_mask[sym] = pd.Series(active, index=close.index).reindex(master_index).fillna(False).to_numpy()
            vol_weights[sym] = pd.Series(vol, index=close.index).reindex(master_index).fillna(0.0).to_numpy()

        daily_weights = pd.DataFrame(index=master_index)

        if position_sizing_mode == "inverse_atr":
            vol_df = pd.DataFrame(vol_weights, index=master_index)
            sum_vol = vol_df.sum(axis=1)
            scale = np.where(sum_vol > 0, 1.0 / np.maximum(1.0, sum_vol), 0.0)
            daily_weights = vol_df.mul(scale, axis=0)
        else:
            mask_df = pd.DataFrame(active_mask, index=master_index).astype(float)
            sum_active = mask_df.sum(axis=1)
            scale = np.where(sum_active > 0, 1.0 / np.maximum(1.0, sum_active), 0.0)
            daily_weights = mask_df.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily_weights[cash_proxy] = np.maximum(0.0, 1.0 - daily_weights.sum(axis=1))

        daily_weights = _fill_out_columns(daily_weights, symbols)
        return _sparse_from_daily(daily_weights)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        entry_days = p.get("turtle_entry_breakout_days", cfg.turtle_entry_breakout_days)
        exit_days = p.get("turtle_exit_breakout_days", cfg.turtle_exit_breakout_days)
        stop_mult = p.get("turtle_atr_stop_mult", cfg.turtle_atr_stop_mult)
        tf = p.get("turtle_require_trend_filter", cfg.turtle_require_trend_filter)
        ma_period = p.get("turtle_trend_ma_period", cfg.turtle_trend_ma_period)
        tf_str = f"with {ma_period}d SMA trend filter" if tf else "without trend filter"
        return (
            f"Turtle Channel Breakout (multi-asset timing, {entry_days}d entry / {exit_days}d exit): "
            f"buys Donchian high breakouts {tf_str}. Exits on {exit_days}d Donchian low or "
            f"{stop_mult}N ATR trailing stop. Positions sized by inverse volatility."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        entry_days = p.get("turtle_entry_breakout_days", cfg.turtle_entry_breakout_days)
        exit_days = p.get("turtle_exit_breakout_days", cfg.turtle_exit_breakout_days)
        atr_period = p.get("turtle_atr_period", cfg.turtle_atr_period)
        ma_period = p.get("turtle_trend_ma_period", cfg.turtle_trend_ma_period) if p.get("turtle_require_trend_filter", cfg.turtle_require_trend_filter) else 0
        return max(entry_days, exit_days, atr_period, ma_period) + 1


class CompounderMarginOfSafetyStrategy(AllocationTemplate):
    """Price-only proxy adaptation of a conservative value-investing
    community's valuation framework (see `docs/snowball_strategy.txt`): the
    original method only holds durable, moat-protected, high-ROE,
    dividend-paying compounders whose expected 5-year return clears a risk
    premium over a broad-index benchmark, and sells the moment that edge
    decays away.

    DISCLOSED SIMPLIFICATION: the original framework is fundamentals-driven
    (5-year earnings forecast, ROE, dividend policy, free-cash-flow quality,
    P/E-based terminal valuation) -- no dividend/ROE/earnings/valuation data
    exists anywhere in this workspace (OHLCV price history only). This class
    is a PRICE-ONLY PROXY: a long-term-uptrend + contained-volatility
    "stability" gate stands in for the moat/high-ROE quality screen, and a
    trailing annualized-return proxy (momentum persistence, not a real
    forecast) stands in for the document's earnings-growth-driven expected
    return. A real-fundamentals version (actual ROE/dividend yield/earnings
    growth from yfinance) lives in the separate `fundamental_screener`
    project instead, since it needs real network data and can't be
    offline/synthetic-tested like every other strategy here.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="compounder_margin_of_safety", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        candidate_universe = p.get("cms_candidate_universe", cfg.cms_candidate_universe)
        benchmark_symbol = p.get("cms_benchmark_symbol", cfg.cms_benchmark_symbol)
        lookback_days = p.get("cms_lookback_days", cfg.cms_lookback_days)
        trend_ma_period = p.get("cms_trend_ma_period", cfg.cms_trend_ma_period)
        vol_lookback = p.get("cms_vol_lookback", cfg.cms_vol_lookback)
        max_volatility = p.get("cms_max_volatility", cfg.cms_max_volatility)
        required_return = p.get("cms_required_return", cfg.cms_required_return)

        symbols = list(universe.keys())
        candidate_symbols = [s for s in candidate_universe if s in universe and s != benchmark_symbol]
        if not candidate_symbols or benchmark_symbol not in universe:
            return pd.DataFrame()

        master_index = universe[candidate_symbols[0]].index
        n_bars = len(master_index)

        benchmark_close = universe[benchmark_symbol]["Close"]
        # Annualized trailing return over lookback_days -- the benchmark's
        # own compounded return, used as the sell-trigger comparator. An
        # index's own "expected return" is inherently a price/total-return
        # concept, so this comparator is price-based even though the
        # candidate side's own signal would be real-fundamentals-based in
        # the fundamental_screener project's sibling strategy.
        benchmark_trailing_return = (
            (benchmark_close / benchmark_close.shift(lookback_days)) ** (252.0 / lookback_days) - 1.0
        )

        raw_weights = {}
        for sym in candidate_symbols:
            close = universe[sym]["Close"]
            trend_ma = sma(close, trend_ma_period)
            vol = realized_vol(close, vol_lookback)
            trailing_return = (close / close.shift(lookback_days)) ** (252.0 / lookback_days) - 1.0

            quality_ok = (close > trend_ma) & (vol <= max_volatility)
            entry_signal = (quality_ok & (trailing_return >= required_return)).fillna(False)
            exit_signal = (~quality_ok | (trailing_return < benchmark_trailing_return)).fillna(True)

            close_arr = close.to_numpy()
            entry_arr = entry_signal.to_numpy()
            exit_arr = exit_signal.to_numpy()
            raw = np.zeros(n_bars)
            in_position = False
            for i in range(n_bars):
                if in_position:
                    if exit_arr[i]:
                        in_position = False
                        raw[i] = 0.0
                    else:
                        raw[i] = 1.0
                elif entry_arr[i]:
                    in_position = True
                    raw[i] = 1.0
            raw_weights[sym] = raw

        position_size_pct = 1.0 / len(candidate_symbols)
        daily = pd.DataFrame(raw_weights, index=master_index) * position_size_pct
        daily = _cap_and_deroute_to_cash(daily, symbols, cash_proxy)

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        trend_ma_period = p.get("cms_trend_ma_period", cfg.cms_trend_ma_period)
        max_volatility = p.get("cms_max_volatility", cfg.cms_max_volatility)
        lookback_days = p.get("cms_lookback_days", cfg.cms_lookback_days)
        required_return = p.get("cms_required_return", cfg.cms_required_return)
        benchmark_symbol = p.get("cms_benchmark_symbol", cfg.cms_benchmark_symbol)
        return (
            f"Compounder Margin-of-Safety (price-proxy adaptation of docs/snowball_strategy.txt's "
            f"conservative valuation framework): holds a candidate symbol only while it is in a "
            f"confirmed uptrend (close > {trend_ma_period}-day SMA) with realized volatility <= "
            f"{max_volatility * 100:.0f}% (a quality/stability proxy for the doc's moat/high-ROE "
            f"screen) AND its own {lookback_days}-bar annualized trailing return is >= "
            f"{required_return * 100:.0f}% (a momentum-persistence proxy for the doc's "
            f"earnings-growth-driven expected return, not a real forecast). Exits the moment that "
            f"trailing-return proxy decays below {benchmark_symbol}'s own trailing return -- a direct "
            f"translation of the document's own sell-trigger rule (hold only while priced to beat the "
            f"benchmark). DISCLOSED SIMPLIFICATION: no real fundamentals (ROE/dividend yield/earnings "
            f"growth) are used here -- see the separate fundamental_screener project for a "
            f"real-data version of this same philosophy."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("cms_trend_ma_period", cfg.cms_trend_ma_period),
            p.get("cms_vol_lookback", cfg.cms_vol_lookback),
            p.get("cms_lookback_days", cfg.cms_lookback_days),
        )


# --- Modern Popular Static Portfolios -------------------------------------
# Fixed-weight, periodically-rebalanced allocations -- no momentum, no trend
# gate, no cross-sectional ranking. Each preset substitutes a ticker already
# in this project's default universe for a canonical holding where a close,
# disclosed equivalent exists (see each class's own docstring for specifics),
# to avoid introducing new symbols purely for cosmetic ticker fidelity.

PERMANENT_PORTFOLIO_WEIGHTS = {"SPY": 0.25, "TLT": 0.25, "BIL": 0.25, "GLD": 0.25}

GOLDEN_BUTTERFLY_WEIGHTS = {
    "SPY": 0.20,  # total-market stock sleeve (canonical: VTI)
    "IWM": 0.20,  # small-cap sleeve (canonical: VBR, small-cap VALUE -- this project has no
                  # small-cap-value data source, so the value tilt is lost; disclosed)
    "TLT": 0.20,  # long-term treasuries
    "BIL": 0.20,  # short-term safe asset (canonical: SHY, 1-3yr treasuries; BIL substitutes
                  # T-bills, a close but not identical duration)
    "GLD": 0.20,  # gold
}

ALL_WEATHER_WEIGHTS = {"SPY": 0.30, "TLT": 0.40, "IEF": 0.15, "GLD": 0.075, "DBC": 0.075}

HFEA_WEIGHTS = {"UPRO": 0.55, "TMF": 0.45}


class StaticAllocationStrategy(AllocationTemplate):
    """Generic engine for fixed-weight, periodically-rebalanced portfolios --
    no momentum, no trend gate, no ranking. Rebalances back to the SAME
    target weights every `rebalance_freq_days`, which matters for the
    backtester's sparse-weights contract exactly as it does for
    EqualWeightAllocation (see common/allocation_templates.py's module
    docstring): recomputing an identical target on every rebalance date is
    still a real instruction, not a no-op.

    Any configured symbol absent from the universe passed to
    generate_weights() is silently dropped -- its weight is simply left
    unallocated (idle cash, per common/allocation_backtester.py's documented
    convention for weights that don't sum to 1.0) rather than being
    redistributed across the remaining symbols or raising. A strategy like
    HFEA that names a leveraged ETF should degrade gracefully if that symbol
    isn't in the requested universe, not crash the whole run -- but a
    warning is emitted so the gap isn't silently invisible either.
    """

    def __init__(self, weights: Dict[str, float], rebalance_freq_days: int, name: str, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        self.weights = dict(weights)
        self.default_rebalance_freq_days = rebalance_freq_days
        super().__init__(name=name, param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", self.default_rebalance_freq_days)
        weights = p.get("weights", self.weights)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        active_weights = {s: w for s, w in weights.items() if s in symbols}
        missing = sorted(set(weights) - set(active_weights))
        if missing:
            warnings.warn(
                f"{self.name}: configured symbol(s) {missing} not present in universe; "
                f"their weight is left unallocated (idle cash), not redistributed."
            )

        row = {s: active_weights.get(s, 0.0) for s in symbols}
        weights_rebal = pd.DataFrame([row] * len(rebalance_dates), index=rebalance_dates, columns=symbols)

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        p = params or {}
        weights = p.get("weights", self.weights)
        rebal_freq = p.get("rebalance_freq_days", self.default_rebalance_freq_days)
        weight_str = ", ".join(f"{s} {w * 100:.1f}%" for s, w in weights.items())
        return (
            f"{self.name}: fixed-weight allocation ({weight_str}), rebalanced every "
            f"{rebal_freq} trading days back to these exact targets. No momentum, "
            f"trend gate, or ranking -- purely a static, periodically-rebalanced mix."
        )

    def warmup_bars(self, params: dict = None) -> int:
        return 0


class PermanentPortfolioStrategy(StaticAllocationStrategy):
    """Permanent Portfolio (Harry Browne). Still actively covered today
    (e.g. dividendes.ch, April 2026; optimizedportfolio.com's "(2026)"-dated
    guide), decades after its 1980s origin.

    Canonical allocation: 25% broad US stocks / 25% long-term Treasuries /
    25% cash (T-bills) / 25% gold (VTI/TLT/BIL/GLD). This project substitutes
    SPY for VTI (both broad-US-market proxies, ~99% correlated) to reuse a
    symbol already in this project's default universe instead of introducing
    a new one for an equivalent holding.

    Annual rebalance is the canonical convention (not a volatility-band
    trigger, which some modern implementations use instead). Sourced
    performance (lazyportfolioetf.com, 10yr window): Sharpe ~0.59, ~30.6%
    max drawdown (inflation-adjusted) -- meaningfully milder than broad
    equities, consistent with the portfolio's stated stability-over-growth
    design goal, not a return-maximizing one.
    """

    def __init__(self, config: StrategyConfig = None):
        super().__init__(
            weights=PERMANENT_PORTFOLIO_WEIGHTS,
            rebalance_freq_days=252,
            name="permanent_portfolio",
            config=config,
        )


class GoldenButterflyStrategy(StaticAllocationStrategy):
    """Golden Butterfly (Tyler / Portfolio Charts). Actively covered today
    (optimizedportfolio.com "(2026)"; bestfolio.app blog).

    Canonical allocation: 20% each of total-market stocks, small-cap value,
    long-term bonds, short-term bonds, and gold -- adds a small-cap tilt and
    splits fixed income by duration versus the Permanent Portfolio's single
    long-bond+cash split, trading some stability for a more equity-like
    return. See GOLDEN_BUTTERFLY_WEIGHTS above for this project's disclosed
    ticker substitutions (IWM for VBR loses the value tilt; BIL for SHY is a
    close but not identical duration).

    Sourced performance (portfoliocharts.com / portfoliodb.com, varies by
    window): CAGR ~8.1-8.3%, max drawdown ~-18% to -20%, Sharpe ~0.47 --
    roughly 93% of the S&P 500's CAGR at about a third of its max drawdown
    over the cited window.
    """

    def __init__(self, config: StrategyConfig = None):
        super().__init__(
            weights=GOLDEN_BUTTERFLY_WEIGHTS,
            rebalance_freq_days=252,
            name="golden_butterfly",
            config=config,
        )


class AllWeatherStrategy(StaticAllocationStrategy):
    """All Weather / "All Seasons" retail risk-parity approximation. The
    specific 30/40/15/7.5/7.5 breakdown traces to Tony Robbins' "Money:
    Master the Game" (interview with Ray Dalio) -- Portfolio Charts uses the
    same allocation under the name "All Seasons Portfolio".

    IMPORTANT: this is a FIXED-WEIGHT approximation, not genuine risk parity.
    Real risk parity risk-BUDGETS each sleeve to contribute equal volatility
    (typically requiring leverage on the bond sleeve to make bonds'
    contribution match equities'); this retail version skips that entirely
    and just holds fixed percentages -- a documented simplification, not a
    reproduction of Bridgewater's actual methodology. Cited performance
    figures (Robbins' own promotional material: profitable 86% of years
    1984-2013) are weaker-sourced than Portfolio Charts' own independently
    computed backtest and should be treated as illustrative, not verified.

    Annual rebalance, standard practice for the retail version.
    """

    def __init__(self, config: StrategyConfig = None):
        super().__init__(
            weights=ALL_WEATHER_WEIGHTS,
            rebalance_freq_days=252,
            name="all_weather",
            config=config,
        )


class HFEAStrategy(StaticAllocationStrategy):
    """"Hedgefundie's Excellent Adventure" (HFEA): 55% UPRO (3x daily S&P
    500) / 45% TMF (3x daily 20+yr Treasuries), quarterly rebalance.
    Originated on the Bogleheads forum in 2019 (revised from an original
    40/60 split); the "Part II" continuation thread has run 250+ pages
    through 2024-2026, indicating sustained active community following.

    Sourced performance (optimizedportfolio.com, aggregating
    PortfolioVisualizer-style backtests): ~24.6% CAGR vs. SPY's ~14.8% since
    May 2009 -- but that window is dominated by a multi-year falling-rate,
    positive stock-bond-correlation regime that directly favors this
    strategy's core bet, so this figure alone overstates what an investor
    starting today should expect. The same aggregate history includes a
    ~70-71% max drawdown bottoming in late 2023: 2022's rising-rate shock
    broke the strategy's central assumption (negative/uncorrelated stock-bond
    returns) when both UPRO and TMF fell together, and TMF underwent a
    1-for-10 reverse split in 2022 from AUM/price decline. Proponents
    themselves explicitly caution against allocating an entire portfolio to
    this strategy.

    IMPORTANT LIMITATION: this project's SyntheticDataProvider generates an
    independent random walk per symbol -- it does NOT simulate genuine 3x
    daily-reset leverage or the resulting volatility decay (the actual
    mechanism behind leveraged ETFs' well-documented long-run divergence from
    a naive "3x the index return" assumption). Running this strategy against
    synthetic data exercises the REBALANCING MECHANICS only; it demonstrates
    nothing about the real strategy's leverage-decay or correlation-breakdown
    risk. Use real UPRO/TMF price data (--data-provider yfinance) to observe
    genuine behavior.
    """

    def __init__(self, config: StrategyConfig = None):
        super().__init__(
            weights=HFEA_WEIGHTS,
            rebalance_freq_days=63,  # quarterly
            name="hfea",
            config=config,
        )


class ProtectiveAssetAllocation(AllocationTemplate):
    """Protective Asset Allocation (PAA). Wouter J. Keller & Jan Willem
    Keuning (2016, SSRN #2759734, "Protective Asset Allocation (PAA): A
    Simple Momentum-Based Alternative for Term Deposits") -- a direct
    successor to this project's own VigilantAssetAllocation (VAA-G4) by the
    same authors, still actively tracked as a live strategy on
    AllocateSmartly today.

    Mechanics: each of N risky assets is scored by a smoothed absolute-
    momentum signal (close / trailing SMA - 1); this project adapts the
    paper's 13-point MONTHLY SMA to a `paa_momentum_lookback`-day (default
    252, ~12 months) DAILY SMA -- a disclosed daily-bar adaptation. Count
    `n` = the number of the N risky assets with positive momentum. The
    protection-asset (default IEF) fraction scales continuously with
    breadth: 100% once n falls to or below n1 = paa_protection_factor * N /
    4, scaling down toward 0% as n rises to N. The remainder splits EQUALLY
    across the paa_top_k highest-momentum risky assets, selected by rank
    regardless of individual sign -- even a fully turbulent reading still
    sends the non-protection remainder to the best-ranked assets, not cash.

    HONEST CAVEAT: this project could not independently verify the exact
    published bond-fraction formula/constants (the precise n1 breakpoint and
    scaling denominator) against the primary SSRN paper this session -- the
    implementation captures the documented MECHANISM (a continuous,
    breadth-based crash-protection fraction, parameterized by a disclosed,
    configurable `paa_protection_factor`) but the precise numeric breakpoints
    are a reasonable, disclosed reconstruction, not a verified reproduction
    of the paper's exact formula. See config.py's DEFAULT_PAA_UNIVERSE
    docstring for the further disclosed universe simplification (VGK+EWJ
    consolidated into EFA).
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="protective_asset_allocation", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        risky_universe = p.get("paa_universe", cfg.paa_universe)
        protection_symbol = p.get("paa_protection_symbol", cfg.paa_protection_symbol)
        lookback = p.get("paa_momentum_lookback", cfg.paa_momentum_lookback)
        top_k = p.get("paa_top_k", cfg.paa_top_k)
        protection_factor = p.get("paa_protection_factor", cfg.paa_protection_factor)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        has_protection = protection_symbol in symbols
        risky_symbols = [s for s in risky_universe if s in symbols and s != protection_symbol]
        n_assets = len(risky_symbols)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        if n_assets == 0:
            weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
            weights_df.loc[rebalance_dates] = weights_rebal
            return weights_df

        mom = pd.DataFrame({
            sym: universe[sym]["Close"] / sma(universe[sym]["Close"], lookback) - 1.0
            for sym in risky_symbols
        })

        n1 = protection_factor * n_assets / 4.0
        k = min(top_k, n_assets)

        for date in rebalance_dates:
            row = mom.loc[date].dropna()
            if len(row) < n_assets:
                continue  # still warming up -- skip until every asset has a valid momentum reading

            n_positive = int((row > 0).sum())
            denom = n_assets - n1
            if n_positive <= n1:
                protection_fraction = 1.0
            elif denom > 0:
                protection_fraction = max(0.0, (n_assets - n_positive) / denom)
            else:
                protection_fraction = 0.0

            top_symbols = row.sort_values(ascending=False).index[:k]
            risky_fraction = 1.0 - protection_fraction
            if len(top_symbols) > 0:
                weights_rebal.loc[date, top_symbols] = risky_fraction / len(top_symbols)
            if has_protection:
                weights_rebal.loc[date, protection_symbol] += protection_fraction

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        risky_universe = p.get("paa_universe", cfg.paa_universe)
        protection_symbol = p.get("paa_protection_symbol", cfg.paa_protection_symbol)
        top_k = p.get("paa_top_k", cfg.paa_top_k)
        return (
            f"Protective Asset Allocation -- PAA (Keller & Keuning 2016): rebalances every "
            f"{p.get('rebalance_freq_days', cfg.rebalance_freq_days)} days. Reasoning: scores "
            f"{', '.join(risky_universe)} by smoothed absolute momentum; the fraction allocated to "
            f"the protection asset ({protection_symbol}) scales continuously with the number of "
            f"assets in positive momentum (100% protection when breadth is weak, 0% when all "
            f"assets are positive). The remainder splits equally across the top {top_k} "
            f"highest-momentum assets by rank. NOTE: bond-fraction formula constants are a "
            f"disclosed reconstruction of the documented mechanism, not a verified reproduction "
            f"of the original paper's exact formula (see class docstring)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return p.get("paa_momentum_lookback", cfg.paa_momentum_lookback)


class AdaptiveAssetAllocation(AllocationTemplate):
    """Adaptive Asset Allocation (AAA). Butler, Philbrick, Gordillo & Varadi
    (2012, SSRN #2328254, "Adaptive Asset Allocation: A Primer";
    GestaltU/ReSolve Asset Management, which still actively references this
    framework today).

    Two-stage mechanism, using the shared `_min_variance_weights` SLSQP
    solver (`common/allocation_templates.py` -- also used there by
    `MinimumVarianceAllocation`, a genuine constrained optimization distinct
    from `HierarchicalRiskParityAllocation`'s heuristic recursive-bisection
    substitute for one):
      1. Momentum filter: rank the universe by aaa_momentum_lookback-day
         (default 126, ~6mo) return; keep the top aaa_top_k (default 4 of 8,
         preserving the paper's "keep half" rule).
      2. Minimum-variance optimization on the survivors: build a covariance
         matrix from aaa_corr_lookback-day (default 126) correlation
         combined with aaa_vol_lookback-day (default 20, more responsive)
         volatility -- the paper's own "hybrid" construction -- then solve
         for the long-only, minimum-variance weights. Positions below
         aaa_min_weight_pct are dropped and the remainder renormalized.

    Sourced performance across cited backtests ranges widely by vintage/
    window (16.9% CAGR / Sharpe 2.15 since 1989 in the original primer;
    ~12.1%/yr since 1989 and a separate ~14.8%/yr over a more recent 10-year
    window from secondary aggregators) -- these figures come from different,
    not cross-verified backtest windows and should be treated as
    illustrative of the strategy's real-world following, not a single
    consistent, independently-verified track record.

    HONEST CAVEAT on the universe: see config.py's DEFAULT_AAA_UNIVERSE
    docstring for the disclosed simplification (EZU+EWJ consolidated into
    EFA; RWX international-REIT sleeve dropped entirely for lack of a proxy
    elsewhere in this project's default universe) -- an 8-asset, not the
    original 10-asset, reproduction.
    """

    def __init__(self, config: StrategyConfig = None):
        self.config = config or StrategyConfig()
        super().__init__(name="adaptive_asset_allocation", param_grid={})

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        cfg = self.config
        p = params or {}
        rebal_freq = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        aaa_universe = p.get("aaa_universe", cfg.aaa_universe)
        mom_lookback = p.get("aaa_momentum_lookback", cfg.aaa_momentum_lookback)
        top_k = p.get("aaa_top_k", cfg.aaa_top_k)
        vol_lookback = p.get("aaa_vol_lookback", cfg.aaa_vol_lookback)
        corr_lookback = p.get("aaa_corr_lookback", cfg.aaa_corr_lookback)
        min_weight_pct = p.get("aaa_min_weight_pct", cfg.aaa_min_weight_pct)

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        universe_symbols = [s for s in aaa_universe if s in symbols]
        n_universe = len(universe_symbols)
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, rebal_freq)

        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)

        if n_universe == 0:
            weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
            weights_df.loc[rebalance_dates] = weights_rebal
            return weights_df

        closes = pd.DataFrame({sym: universe[sym]["Close"] for sym in universe_symbols})
        mom = roc(closes, mom_lookback)
        rets = closes.pct_change()
        k = min(top_k, n_universe)

        for date in rebalance_dates:
            mom_row = mom.loc[date].dropna()
            if len(mom_row) < n_universe:
                continue  # still warming up

            survivors = mom_row.sort_values(ascending=False).index[:k].tolist()

            hist = rets.loc[:date, survivors]
            corr_hist = hist.tail(corr_lookback)
            vol_hist = hist.tail(vol_lookback)
            if len(corr_hist) < corr_lookback or len(vol_hist) < vol_lookback:
                continue  # still warming up

            corr = corr_hist.corr().to_numpy()
            vol = vol_hist.std().to_numpy()
            if np.any(~np.isfinite(corr)) or np.any(~np.isfinite(vol)):
                continue  # degenerate window (e.g. zero-variance survivor) -- skip this rebalance

            cov = corr * np.outer(vol, vol)

            w = _min_variance_weights(cov)
            w = np.where(w < min_weight_pct, 0.0, w)
            total = w.sum()
            if total <= 0:
                continue
            w = w / total

            weights_rebal.loc[date, survivors] = w

        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal
        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        aaa_universe = p.get("aaa_universe", cfg.aaa_universe)
        top_k = p.get("aaa_top_k", cfg.aaa_top_k)
        return (
            f"Adaptive Asset Allocation -- AAA (Butler/Philbrick/Gordillo/Varadi 2012): rebalances "
            f"every {p.get('rebalance_freq_days', cfg.rebalance_freq_days)} days. Reasoning: ranks "
            f"{', '.join(aaa_universe)} by {p.get('aaa_momentum_lookback', cfg.aaa_momentum_lookback)}-day "
            f"momentum, keeps the top {top_k}, then solves a long-only minimum-variance optimization "
            f"on the survivors using a correlation x volatility hybrid covariance matrix. NOTE: this "
            f"project's universe is a disclosed, reduced 8-asset simplification of the original "
            f"10-asset paper (see class docstring)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return max(
            p.get("aaa_momentum_lookback", cfg.aaa_momentum_lookback),
            p.get("aaa_corr_lookback", cfg.aaa_corr_lookback),
        ) + 1


# Name -> class registry for every "class"-type entry in strategies_config.json,
# and the strategies_config.json -> instance builder. Both live here (not in
# run_research_strategy.py, where they originally lived) so another project can
# import them directly -- e.g. `strategy_generator`'s `--research-strategy` flag
# and `backtester`'s research_strategy_spec reconstruction both need to build
# the exact same instance a `research_strategy` CLI run would, including
# natural-language-parsed strategies (dual_momentum, baa_keller,
# volatility_managed), not just the class-based ones. See
# `common/README.md`'s cross-project import convention note for how a consumer
# reaches this module.
STRATEGY_CLASS_MAP = {
    "AcceleratingDualMomentum": AcceleratingDualMomentum,
    "ActiveDualMomentumRiskParity": ActiveDualMomentumRiskParity,
    "AdaptiveAssetAllocation": AdaptiveAssetAllocation,
    "AdaptiveGridStrategy": AdaptiveGridStrategy,
    "AllWeatherStrategy": AllWeatherStrategy,
    "BoldAssetAllocation": BoldAssetAllocation,
    "ChanPivotShiftMACDStrategy": ChanPivotShiftMACDStrategy,
    "ChanPivotShiftStrategy": ChanPivotShiftStrategy,
    "ChanThreeTypeStrategy": ChanThreeTypeStrategy,
    "CompounderMarginOfSafetyStrategy": CompounderMarginOfSafetyStrategy,
    "EnsembleRegimeSwitchingStrategy": EnsembleRegimeSwitchingStrategy,
    "GoldenButterflyStrategy": GoldenButterflyStrategy,
    "HFEAStrategy": HFEAStrategy,
    "NaturalLanguageStrategy": NaturalLanguageStrategy,
    "PermanentPortfolioStrategy": PermanentPortfolioStrategy,
    "ProtectiveAssetAllocation": ProtectiveAssetAllocation,
    "RSIMeanReversionStrategy": RSIMeanReversionStrategy,
    "SwingTrendPullbackStrategy": SwingTrendPullbackStrategy,
    "TurtleBreakoutStrategy": TurtleBreakoutStrategy,
    "VigilantAssetAllocation": VigilantAssetAllocation,
    "VolatilityManagedStrategy": VolatilityManagedStrategy,
}


def instantiate_strategy_from_config_entry(entry_key: str, entry_data: dict):
    """Builds the strategy instance a `strategies_config.json` entry describes
    -- either a `type: "natural_language"` entry (parsed via
    `parse_plain_english_strategy`) or a `type: "class"` entry (looked up in
    `STRATEGY_CLASS_MAP` by `class_name`). This is the single source of truth
    for "given a strategies_config.json key, build the exact instance
    research_strategy's own CLI would" -- reused as-is by any external
    consumer instead of reimplementing per-type reconstruction."""
    if not isinstance(entry_data, dict):
        raise ValueError(
            f"Strategy '{entry_key}': entry_data must be a dict, got {type(entry_data).__name__}"
        )
    strat_type = entry_data.get("type", "class")
    params = entry_data.get("parameters", {})
    try:
        cfg = StrategyConfig.from_dict(params)
    except ValueError as exc:
        raise ValueError(f"Invalid config for strategy '{entry_key}': {exc}") from exc

    if strat_type == "natural_language":
        plain_english = entry_data.get("plain_english_description", "")
        if not plain_english or not plain_english.strip():
            raise ValueError(
                f"Strategy '{entry_key}' has type 'natural_language' but no 'plain_english_description' key"
            )
        name = entry_data.get("name", entry_key)
        spec = parse_plain_english_strategy(plain_english, name=name)
        return NaturalLanguageStrategy(spec, config=cfg)
    elif strat_type == "class":
        cls_name = entry_data.get("class_name", "")
        if not cls_name:
            raise ValueError(f"Strategy '{entry_key}' has type 'class' but no 'class_name' key")
        cls_obj = STRATEGY_CLASS_MAP.get(cls_name)
        if not cls_obj:
            raise ValueError(f"Unrecognized strategy class_name '{cls_name}' for strategy key '{entry_key}'")
        return cls_obj(config=cfg)
    else:
        raise ValueError(f"Unknown strategy type '{strat_type}' for strategy key '{entry_key}'")


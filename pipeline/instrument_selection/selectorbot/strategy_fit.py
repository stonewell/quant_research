"""Strategy-Fit Evaluation & Resolution for Instrument Selection.

Given a strategy (by key, class name, template name, strategy.json file, or style preset),
this module evaluates candidate universe instruments across two complementary pillars:

1. Pillar A: Factor Profile Fit
   Evaluates how closely each instrument's statistical characteristics (Hurst exponent,
   ADX trend strength, momentum serial correlation, ATR oscillation, volatility stability,
   diversification to universe) align with the strategy's required quantitative factors.

2. Pillar B: Empirical Simulation Fit
   Executes a single-asset backtest for each instrument using the strategy's logic,
   evaluating standalone Sharpe ratio, CAGR, max drawdown, and trade activity. Assets
   where the strategy's conditions never fire are heavily penalized.

The two pillars combine into a 0-100 `strategy_fit_score`, which feeds downstream
into the overall selection score and discrete basket selection.
"""

from dataclasses import dataclass, field
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
import warnings

import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_pipeline_dir = os.path.join(_REPO_ROOT, "pipeline")
if _pipeline_dir not in sys.path:
    sys.path.insert(0, _pipeline_dir)

from common.allocation_backtester import run_allocation_backtest
from common.allocation_templates import ALLOCATION_TEMPLATES, AllocationTemplate
from common.strategy_spec import get_template, load_strategy_file
from .scoring import _pct_rank


STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "trend": {
        "name": "Trend-Following / Breakout Style",
        "factors": ["absolute_momentum_trend", "regime_trend_strength"],
        "description": "Prefers assets with strong directional persistence (Hurst > 0.5), high ADX, upward trend alignment, and positive momentum.",
    },
    "mean_reversion": {
        "name": "Mean-Reversion / Oscillator Style",
        "factors": ["mean_reversion"],
        "description": "Prefers assets with mean-reverting price dynamics (Hurst < 0.5), high ATR volatility oscillation, and low directional drift.",
    },
    "momentum": {
        "name": "Cross-Sectional Momentum Style",
        "factors": ["relative_momentum", "absolute_momentum_trend"],
        "description": "Prefers assets with high trailing momentum returns, positive momentum serial correlation, and upward moving average support.",
    },
    "grid": {
        "name": "Grid Trading / Volatility Range Style",
        "factors": ["mean_reversion", "volatility_targeting"],
        "description": "Prefers range-bound instruments with high intraday/daily volatility oscillation and contained tail risk.",
    },
    "multi_asset": {
        "name": "Multi-Asset / Defensive Style",
        "factors": ["correlation_diversification", "breadth"],
        "description": "Prefers diversifying assets with low correlation to equities, low downside volatility ratio, and solid historical longevity.",
    },
    "volatility": {
        "name": "Volatility-Targeted / Risk Parity Style",
        "factors": ["volatility_targeting"],
        "description": "Prefers assets with predictable volatility, contained vol-of-vol, and stable ATR regime behavior.",
    },
}

STYLE_ALIASES: Dict[str, str] = {
    "trend_following": "trend",
    "breakout": "trend",
    "turtle": "trend",
    "chan": "trend",
    "meanrev": "mean_reversion",
    "mean_rev": "mean_reversion",
    "oscillator": "mean_reversion",
    "rsi": "mean_reversion",
    "cross_sectional_momentum": "momentum",
    "grid_trading": "grid",
    "asset_allocation": "multi_asset",
    "risk_parity": "multi_asset",
    "defensive": "multi_asset",
    "vaa": "multi_asset",
    "paa": "multi_asset",
    "volatility_targeting": "volatility",
    "volatility_managed": "volatility",
}


@dataclass
class StrategyTarget:
    name: str
    key: str
    description: str
    factor_tags: List[str]
    style_label: str
    strategy_instance: Optional[Any] = None
    params: dict = field(default_factory=dict)
    is_style_preset: bool = False


def _infer_style_label(factor_tags: List[str]) -> str:
    """Infers high-level style label from factor tags."""
    if not factor_tags:
        return "multi_asset"
    factors = set(factor_tags)
    if "mean_reversion" in factors:
        if "volatility_targeting" in factors:
            return "grid"
        return "mean_reversion"
    if "regime_trend_strength" in factors or "absolute_momentum_trend" in factors:
        if "relative_momentum" in factors and "regime_trend_strength" not in factors:
            return "momentum"
        return "trend"
    if "relative_momentum" in factors:
        return "momentum"
    if "volatility_targeting" in factors:
        return "volatility"
    if "correlation_diversification" in factors or "breadth" in factors:
        return "multi_asset"
    return "trend"


def resolve_strategy_target(
    strategy_name_or_key: Optional[str] = None,
    strategy_file: Optional[str] = None,
) -> StrategyTarget:
    """Resolves a strategy target from either a strategy file or a strategy name/key/style.

    Supports:
    1. Strategy JSON file (from strategy_dumps/ or results/strategy.json).
    2. Style presets ('trend', 'mean_reversion', 'momentum', 'grid', 'multi_asset', 'volatility').
    3. `strategies_config.json` entry keys (e.g. 'chan_pivot_shift_macd', 'turtle_breakout_s1', 'dual_momentum').
    4. `ALLOCATION_TEMPLATES` static template names (e.g. 'equal_weight', 'inverse_variance', 'momentum').
    5. Class names registered in `STRATEGY_CLASS_MAP`.
    """
    if not strategy_name_or_key and not strategy_file:
        raise ValueError("Either strategy_name_or_key or strategy_file must be provided.")

    if strategy_file:
        strategy_def = load_strategy_file(strategy_file)
        params = strategy_def.get("params", {})
        template_name = strategy_def.get("template_name", "custom_strategy")
        strategy_instance = get_template(
            template_name,
            pattern_spec=strategy_def.get("pattern_spec"),
            research_strategy_spec=strategy_def.get("research_strategy_spec"),
            composite_spec=strategy_def.get("composite_spec"),
            params=params,
            fundamental_spec=strategy_def.get("fundamental_spec"),
            bnn_spec=strategy_def.get("bnn_spec"),
        )
        factor_tags = list(getattr(strategy_instance, "factor_tags", []))
        if not factor_tags and "research_strategy_spec" in strategy_def:
            entry_data = strategy_def["research_strategy_spec"].get("entry_data", {})
            factor_tags = list(entry_data.get("factors", []))
        style_label = _infer_style_label(factor_tags)
        return StrategyTarget(
            name=strategy_def.get("name", template_name),
            key=os.path.basename(strategy_file),
            description=strategy_def.get("description", f"Loaded strategy from {strategy_file}"),
            factor_tags=factor_tags,
            style_label=style_label,
            strategy_instance=strategy_instance,
            params=params,
            is_style_preset=False,
        )

    raw_key = strategy_name_or_key.strip()
    s = raw_key.lower()

    # 1. Check style presets
    if s.startswith("style:"):
        s = s.split(":", 1)[1]
    if s in STYLE_ALIASES:
        s = STYLE_ALIASES[s]
    if s in STYLE_PRESETS:
        preset = STYLE_PRESETS[s]
        return StrategyTarget(
            name=preset["name"],
            key=f"style:{s}",
            description=preset["description"],
            factor_tags=list(preset["factors"]),
            style_label=s,
            strategy_instance=None,
            params={},
            is_style_preset=True,
        )

    # 2. Check research_strategy strategies_config.json
    try:
        from research_strategy.rs.config import load_strategies_config
        from research_strategy.rs.strategy import instantiate_strategy_from_config_entry
        config_dict = load_strategies_config()
        matched_key = None
        for k in config_dict:
            if k.lower() == s or config_dict[k].get("name", "").lower() == s:
                matched_key = k
                break
        if matched_key:
            entry = config_dict[matched_key]
            factors = list(entry.get("factors", []))
            params = entry.get("parameters", {})
            instance = instantiate_strategy_from_config_entry(matched_key, entry)
            return StrategyTarget(
                name=entry.get("name", matched_key),
                key=matched_key,
                description=entry.get("description", ""),
                factor_tags=factors,
                style_label=_infer_style_label(factors),
                strategy_instance=instance,
                params=params,
                is_style_preset=False,
            )
    except Exception:
        pass

    # 3. Check ALLOCATION_TEMPLATES
    for t_cls in ALLOCATION_TEMPLATES:
        if t_cls.name.lower() == s or t_cls.__name__.lower() == s:
            instance = t_cls()
            factors = list(getattr(instance, "factor_tags", []))
            return StrategyTarget(
                name=instance.name,
                key=instance.name,
                description=f"Static allocation template: {instance.name}",
                factor_tags=factors,
                style_label=_infer_style_label(factors),
                strategy_instance=instance,
                params={},
                is_style_preset=False,
            )

    # 4. Check STRATEGY_CLASS_MAP
    try:
        from research_strategy.rs.strategy import STRATEGY_CLASS_MAP
        from research_strategy.rs.config import StrategyConfig
        for cls_name, cls_obj in STRATEGY_CLASS_MAP.items():
            if cls_name.lower() == s:
                instance = cls_obj(StrategyConfig())
                factors = list(getattr(instance, "factor_tags", []))
                return StrategyTarget(
                    name=cls_name,
                    key=cls_name,
                    description=f"Research strategy class: {cls_name}",
                    factor_tags=factors,
                    style_label=_infer_style_label(factors),
                    strategy_instance=instance,
                    params={},
                    is_style_preset=False,
                )
    except Exception:
        pass

    raise ValueError(
        f"Unrecognized strategy '{strategy_name_or_key}'. "
        f"Expected a style preset ({sorted(STYLE_PRESETS.keys())}), "
        f"a strategies_config.json key, or a strategy class name."
    )


def compute_strategy_profile_fit(
    metrics: pd.DataFrame,
    factor_tags: List[str],
    style_label: Optional[str] = None,
) -> pd.Series:
    """Computes Pillar A: factor profile fit score (0.0 to 100.0) for each instrument.

    Evaluates statistical alignment against the strategy's factor requirements:
    - Trend / Momentum: Hurst > 0.5 significance, ADX strength, % days above MA, positive momentum.
    - Mean-Reversion: Hurst < 0.5 significance, ATR oscillation, low trend drift, tail risk containment.
    - Relative Momentum: Trailing return magnitude, serial momentum edge, diversification.
    - Defensive / Multi-Asset: Low correlation to universe, downside vol containment, history.
    - Volatility Targeting: Vol-of-vol stability, ATR regime predictability.
    """
    if metrics.empty:
        return pd.Series(dtype=float)

    symbols = metrics.index
    component_scores: List[pd.Series] = []

    factors = set(factor_tags) if factor_tags else set()
    if style_label == "trend" or "absolute_momentum_trend" in factors or "regime_trend_strength" in factors:
        # Trend profile
        hurst_val = metrics.get("hurst", pd.Series(0.5, index=symbols))
        hurst_sig = metrics.get("hurst_significant", pd.Series(False, index=symbols)).fillna(False)
        hurst_trend = (hurst_val - 0.5).clip(lower=0.0) / 0.5
        hurst_trend_score = hurst_trend * np.where(hurst_sig, 1.0, 0.2)

        adx_rank = _pct_rank(metrics.get("adx_mean", pd.Series(0.5, index=symbols)))
        trend_days_rank = _pct_rank(metrics.get("pct_days_above_trend_ma", pd.Series(0.5, index=symbols)))

        mom_edge = metrics.get("momentum_edge", pd.Series(0.0, index=symbols)).clip(lower=0.0)
        mom_sig = metrics.get("momentum_significant", pd.Series(False, index=symbols)).fillna(False)
        mom_rank = _pct_rank(mom_edge) * np.where(mom_sig, 1.0, 0.3)

        vol_rank = _pct_rank(metrics.get("realized_vol_annualized_pct", pd.Series(0.5, index=symbols)))

        trend_score = (
            0.30 * hurst_trend_score * 100.0
            + 0.25 * (adx_rank * 100.0)
            + 0.20 * (trend_days_rank * 100.0)
            + 0.15 * (mom_rank * 100.0)
            + 0.10 * (vol_rank * 100.0)
        )
        component_scores.append(trend_score)

    if style_label == "mean_reversion" or "mean_reversion" in factors:
        # Mean-reversion profile
        hurst_val = metrics.get("hurst", pd.Series(0.5, index=symbols))
        hurst_sig = metrics.get("hurst_significant", pd.Series(False, index=symbols)).fillna(False)
        hurst_meanrev = (0.5 - hurst_val).clip(lower=0.0) / 0.5
        hurst_meanrev_score = hurst_meanrev * np.where(hurst_sig, 1.0, 0.2)

        atr_osc_rank = _pct_rank(metrics.get("atr_pct_mean", pd.Series(0.5, index=symbols)))
        adx_mean = metrics.get("adx_mean", pd.Series(0.5, index=symbols))
        low_trend_rank = 1.0 - _pct_rank(adx_mean)

        cnd_edge = metrics.get("candlestick_edge", pd.Series(0.0, index=symbols)).abs()
        cnd_sig = metrics.get("candlestick_significant", pd.Series(False, index=symbols)).fillna(False)
        cnd_rank = _pct_rank(cnd_edge) * np.where(cnd_sig, 1.0, 0.3)

        downside_vol = metrics.get("downside_vol_ratio", pd.Series(0.5, index=symbols))
        tail_risk_rank = 1.0 - _pct_rank(downside_vol)

        meanrev_score = (
            0.30 * hurst_meanrev_score * 100.0
            + 0.25 * (atr_osc_rank * 100.0)
            + 0.20 * (low_trend_rank * 100.0)
            + 0.15 * (cnd_rank * 100.0)
            + 0.10 * (tail_risk_rank * 100.0)
        )
        component_scores.append(meanrev_score)

    if style_label == "momentum" or "relative_momentum" in factors:
        # Cross-sectional relative momentum profile
        lookback_ret = _pct_rank(metrics.get("momentum_lookback_return", pd.Series(0.5, index=symbols)))
        mom_edge = metrics.get("momentum_edge", pd.Series(0.0, index=symbols)).clip(lower=0.0)
        mom_sig = metrics.get("momentum_significant", pd.Series(False, index=symbols)).fillna(False)
        mom_rank = _pct_rank(mom_edge) * np.where(mom_sig, 1.0, 0.3)

        trend_days = _pct_rank(metrics.get("pct_days_above_trend_ma", pd.Series(0.5, index=symbols)))
        corr_to_u = metrics.get("avg_correlation_to_universe", pd.Series(0.5, index=symbols))
        div_rank = 1.0 - _pct_rank(corr_to_u)

        momentum_score = (
            0.35 * (lookback_ret * 100.0)
            + 0.30 * (mom_rank * 100.0)
            + 0.20 * (trend_days * 100.0)
            + 0.15 * (div_rank * 100.0)
        )
        component_scores.append(momentum_score)

    if style_label == "volatility" or "volatility_targeting" in factors:
        # Volatility predictability profile
        vol_of_vol = metrics.get("vol_of_vol", pd.Series(0.5, index=symbols))
        stable_vol = 1.0 - _pct_rank(vol_of_vol)

        atr_regime = metrics.get("atr_regime_ratio", pd.Series(0.0, index=symbols)).abs()
        stable_regime = 1.0 - _pct_rank(atr_regime)

        downside_vol = metrics.get("downside_vol_ratio", pd.Series(0.5, index=symbols))
        tail_risk_rank = 1.0 - _pct_rank(downside_vol)

        vol_score = (
            0.40 * (stable_vol * 100.0)
            + 0.30 * (stable_regime * 100.0)
            + 0.30 * (tail_risk_rank * 100.0)
        )
        component_scores.append(vol_score)

    if style_label == "multi_asset" or "correlation_diversification" in factors or "breadth" in factors or "static_fixed_weight" in factors:
        # Defensive / multi-asset diversification profile
        corr_to_u = metrics.get("avg_correlation_to_universe", pd.Series(0.5, index=symbols))
        low_corr = 1.0 - _pct_rank(corr_to_u)

        downside_vol = metrics.get("downside_vol_ratio", pd.Series(0.5, index=symbols))
        tail_risk_rank = 1.0 - _pct_rank(downside_vol)

        history_years = metrics.get("history_years", pd.Series(0.5, index=symbols))
        history_rank = _pct_rank(history_years)

        multi_score = (
            0.45 * (low_corr * 100.0)
            + 0.35 * (tail_risk_rank * 100.0)
            + 0.20 * (history_rank * 100.0)
        )
        component_scores.append(multi_score)

    if not component_scores:
        # Default balanced profile if no specific factors matched
        liquidity_rank = _pct_rank(metrics.get("avg_dollar_volume", pd.Series(0.5, index=symbols)))
        vol_rank = _pct_rank(metrics.get("realized_vol_annualized_pct", pd.Series(0.5, index=symbols)))
        div_rank = 1.0 - _pct_rank(metrics.get("avg_correlation_to_universe", pd.Series(0.5, index=symbols)))
        default_score = 0.40 * (liquidity_rank * 100.0) + 0.30 * (vol_rank * 100.0) + 0.30 * (div_rank * 100.0)
        component_scores.append(default_score)

    # Average all applicable factor scores
    score_df = pd.DataFrame(component_scores).T
    final_score = score_df.mean(axis=1).clip(lower=0.0, upper=100.0)
    final_score.name = "strategy_profile_fit_score"
    return final_score


def compute_strategy_simulation_fit(
    data: Dict[str, pd.DataFrame],
    strategy_instance: Any,
    params: dict = None,
    cash_proxy: str = "BIL",
) -> pd.DataFrame:
    """Computes Pillar B: empirical single-asset backtest simulation fit.

    Runs strategy simulation on each asset individually, extracting Sharpe ratio,
    CAGR, max drawdown, and rebalance counts. Penalizes instruments where the strategy
    never fires.
    """
    if not data or strategy_instance is None:
        return pd.DataFrame()

    params = params or {}
    results = {}

    for sym, df in data.items():
        if df.empty or len(df) < 30:
            results[sym] = {
                "sim_sharpe": 0.0,
                "sim_cagr": 0.0,
                "sim_max_dd": 0.0,
                "sim_calmar": 0.0,
                "sim_profit_factor": 0.0,
                "sim_win_rate": 0.0,
                "sim_rebalances": 0,
                "sim_turnover": 0.0,
                "sim_traded": False,
            }
            continue

        # Build mini single-asset universe with cash proxy if available
        if cash_proxy in data and sym != cash_proxy:
            mini_u = {sym: df, cash_proxy: data[cash_proxy]}
        else:
            mini_u = {sym: df}

        try:
            weights = strategy_instance.generate_weights(mini_u, params)
            if weights is None or weights.empty:
                results[sym] = {
                    "sim_sharpe": 0.0,
                    "sim_cagr": 0.0,
                    "sim_max_dd": 0.0,
                    "sim_calmar": 0.0,
                    "sim_profit_factor": 0.0,
                    "sim_win_rate": 0.0,
                    "sim_rebalances": 0,
                    "sim_turnover": 0.0,
                    "sim_traded": False,
                }
                continue

            backtest_res = run_allocation_backtest(mini_u, weights)
            rebal_count = int(backtest_res.get("total_rebalances", 0))
            turnover = float(backtest_res.get("total_turnover", 0.0))
            cagr_val = float(backtest_res.get("cagr", 0.0))
            sharpe_val = float(backtest_res.get("sharpe_ratio", 0.0))
            max_dd = float(backtest_res.get("max_drawdown", 0.0))
            calmar = float(backtest_res.get("calmar_ratio", 0.0))
            profit_factor = float(backtest_res.get("profit_factor", 0.0))
            win_rate = float(backtest_res.get("win_rate", 0.0))

            # Strategy is considered traded if the target asset itself was held at any point (> 1% weight)
            actual_w = backtest_res.get("actual_weights", pd.DataFrame())
            if isinstance(actual_w, pd.DataFrame) and sym in actual_w.columns:
                traded = bool((actual_w[sym] > 0.01).any())
            else:
                traded = (rebal_count > 0) and (turnover > 0.01)

            results[sym] = {
                "sim_sharpe": sharpe_val,
                "sim_cagr": cagr_val,
                "sim_max_dd": max_dd,
                "sim_calmar": calmar,
                "sim_profit_factor": profit_factor,
                "sim_win_rate": win_rate,
                "sim_rebalances": rebal_count,
                "sim_turnover": turnover,
                "sim_traded": traded,
            }
        except Exception as exc:
            warnings.warn(f"Simulation fit failed for symbol {sym}: {exc}")
            results[sym] = {
                "sim_sharpe": 0.0,
                "sim_cagr": 0.0,
                "sim_max_dd": 0.0,
                "sim_calmar": 0.0,
                "sim_profit_factor": 0.0,
                "sim_win_rate": 0.0,
                "sim_rebalances": 0,
                "sim_turnover": 0.0,
                "sim_traded": False,
            }

    sim_df = pd.DataFrame(results).T
    if sim_df.empty:
        return sim_df

    sharpe_rank = _pct_rank(sim_df["sim_sharpe"])
    cagr_rank = _pct_rank(sim_df["sim_cagr"])
    dd_rank = 1.0 - _pct_rank(sim_df["sim_max_dd"])

    sim_score = (0.50 * sharpe_rank + 0.30 * cagr_rank + 0.20 * dd_rank) * 100.0
    sim_df["strategy_sim_fit_score"] = np.where(sim_df["sim_traded"], sim_score, 0.0)
    return sim_df


def compute_combined_strategy_fit(
    profile_scores: pd.Series,
    sim_df: Optional[pd.DataFrame] = None,
    profile_weight: float = 0.40,
    sim_weight: float = 0.60,
) -> pd.DataFrame:
    """Combines Pillar A profile fit and Pillar B simulation fit into a unified fit DataFrame."""
    if sim_df is None or sim_df.empty or "strategy_sim_fit_score" not in sim_df:
        combined = pd.DataFrame({
            "strategy_profile_fit_score": profile_scores,
            "strategy_fit_score": profile_scores,
        }, index=profile_scores.index)
        return combined

    sim_scores = sim_df["strategy_sim_fit_score"].reindex(profile_scores.index).fillna(0.0)
    has_sim_trading = sim_df["sim_traded"].any() if "sim_traded" in sim_df else False

    if has_sim_trading:
        fit_score = (profile_weight * profile_scores + sim_weight * sim_scores).clip(0.0, 100.0)
    else:
        # If no simulation trades fired across all assets, fall back to profile score
        fit_score = profile_scores

    combined = sim_df.copy()
    combined["strategy_profile_fit_score"] = profile_scores
    combined["strategy_fit_score"] = fit_score
    return combined

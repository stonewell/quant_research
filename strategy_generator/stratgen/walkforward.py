"""Walk-forward validation: the standard, concretely-specified defense
against overfitting when auto-tuning strategy parameters to historical data.

Research grounding: a three-way chronological split -- (a) a TRAIN window
used to fit/search candidate parameterizations, (b) a VALIDATION window
immediately after, used to SELECT among those candidates by out-of-sample
performance (not by training performance -- this is what actually defends
against overfitting; selecting by training performance alone is just curve
fitting), and (c) a disjoint TEST window, never touched during search, for
the final unbiased read -- repeated across multiple rolling folds. An
embargo gap before the test window guards against information leakage from
indicator lookback windows straddling the validation/test boundary. The
"generalization ratio" (mean out-of-sample performance / mean in-sample
performance) measures how much the strategy degrades out of sample; a ratio
near 1.0 means it generalizes well, a ratio near 0 (or negative) means the
in-sample edge was mostly noise.

Regime classification (Hurst) and the parameter grid search are both fit
using ONLY the train window of each fold; the winning parameterization is
chosen by VALIDATION-window performance, and test-window performance is
computed only once, after selection, per fold.
"""

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtester import run_backtest
from .generator import GeneratorConfig, grid_combinations
from .metrics import deflated_sharpe_ratio, sharpe_ratio
from .regime import classify_series
from .templates import TEMPLATES_BY_REGIME, NoTradeTemplate

TRADING_DAYS_PER_YEAR = 252


@dataclass
class WalkForwardConfig:
    train_years: float = 4.0
    validation_years: float = 2.0
    test_years: float = 1.0       # also used as the step size between folds, per research's rolling-window convention
    embargo_days: int = 30
    warmup_buffer_days: int = 250  # extra bars prepended per fold so indicators (e.g. SMA-150) are warmed up by train_start
    generator_config: GeneratorConfig = field(default_factory=GeneratorConfig)


def _years_to_bars(years: float) -> int:
    return int(round(years * TRADING_DAYS_PER_YEAR))


def generate_folds(n_bars: int, config: WalkForwardConfig) -> list:
    """Bar-index (not date) fold boundaries -- simpler and avoids calendar
    edge cases; each fold is [buffer_start, test_end) with train/validation/
    test sub-ranges inside it."""
    train_bars = _years_to_bars(config.train_years)
    validation_bars = _years_to_bars(config.validation_years)
    test_bars = _years_to_bars(config.test_years)
    step_bars = test_bars

    folds = []
    buffer_start = 0
    while True:
        train_start = buffer_start + config.warmup_buffer_days
        train_end = train_start + train_bars
        validation_start = train_end
        validation_end = validation_start + validation_bars
        test_start = validation_end + config.embargo_days
        test_end = test_start + test_bars
        if test_end > n_bars:
            break
        folds.append({
            "buffer_start": buffer_start, "train_start": train_start, "train_end": train_end,
            "validation_start": validation_start, "validation_end": validation_end,
            "test_start": test_start, "test_end": test_end,
        })
        buffer_start += step_bars
    return folds


def _slice_sharpe(equity_curve: pd.DataFrame, start_date, end_date) -> tuple:
    window = equity_curve.loc[(equity_curve.index >= start_date) & (equity_curve.index < end_date)]
    if len(window) < 5:
        return float("-inf"), 0
    returns = window["equity"].pct_change().dropna()
    return sharpe_ratio(returns), len(window)


def _evaluate_fold(df: pd.DataFrame, fold: dict, config: GeneratorConfig) -> dict:
    fold_df = df.iloc[fold["buffer_start"]:fold["test_end"]]
    train_df = df.iloc[fold["train_start"]:fold["train_end"]]

    log_returns = np.log(train_df["Close"] / train_df["Close"].shift(1)).dropna()
    regime = classify_series(log_returns, n_simulations=config.hurst_n_simulations, k=config.hurst_k, seed=config.hurst_seed)
    template_cls = TEMPLATES_BY_REGIME[regime["regime_label"]]
    template = template_cls()

    train_start_date = df.index[fold["train_start"]]
    validation_start_date = df.index[fold["validation_start"]]
    validation_end_date = df.index[fold["validation_end"]]
    test_start_date = df.index[fold["test_start"]]
    test_end_date = df.index[min(fold["test_end"], len(df) - 1)]

    if isinstance(template, NoTradeTemplate):
        return {"regime_label": regime["regime_label"], "hurst": regime["hurst"], "template_name": template.name,
                "params": {}, "validation_sharpe": 0.0, "test_sharpe": 0.0, "test_num_trades": 0,
                "n_trials": 0, "trusted": True}

    combos = grid_combinations(template.param_grid)
    scored = []
    for params in combos:
        try:
            result = run_backtest(fold_df, template, params, initial_capital=config.initial_capital,
                                   commission_per_trade=config.commission_per_trade, commission_pct=config.commission_pct,
                                   slippage_pct=config.slippage_pct, atr_period=config.atr_period)
        except Exception:
            scored.append((params, float("-inf"), None))
            continue
        eq = result["equity_curve"]
        if eq.empty:
            scored.append((params, float("-inf"), None))
            continue
        validation_sharpe, _ = _slice_sharpe(eq, validation_start_date, validation_end_date)
        scored.append((params, validation_sharpe, eq))

    best_params, best_validation_sharpe, best_eq = max(scored, key=lambda r: r[1])

    if best_eq is None:
        test_sharpe, test_n = float("-inf"), 0
    else:
        test_sharpe, test_n = _slice_sharpe(best_eq, test_start_date, test_end_date)

    trusted = best_validation_sharpe > 0 and test_n >= config.min_trades_for_trust
    return {
        "regime_label": regime["regime_label"], "hurst": regime["hurst"], "template_name": template.name,
        "params": best_params, "validation_sharpe": best_validation_sharpe, "test_sharpe": test_sharpe,
        "test_num_trades": test_n, "n_trials": len(combos), "trusted": trusted,
    }


def run_walkforward(df: pd.DataFrame, config: WalkForwardConfig = None) -> dict:
    config = config or WalkForwardConfig()
    folds = generate_folds(len(df), config)
    if not folds:
        raise ValueError("Not enough bars to build even one walk-forward fold with the configured window lengths")

    fold_results = [_evaluate_fold(df, fold, config.generator_config) for fold in folds]

    validation_sharpes = np.array([r["validation_sharpe"] for r in fold_results if np.isfinite(r["validation_sharpe"])])
    test_sharpes = np.array([r["test_sharpe"] for r in fold_results if np.isfinite(r["test_sharpe"])])

    mean_is = validation_sharpes.mean() if len(validation_sharpes) else 0.0
    mean_oos = test_sharpes.mean() if len(test_sharpes) else 0.0
    generalization_ratio = (mean_oos / mean_is) if mean_is > 0 else float("nan")

    total_trials = sum(r["n_trials"] for r in fold_results)
    total_test_obs = sum(r["test_num_trades"] for r in fold_results)
    dsr = None
    if len(test_sharpes) and total_trials > 1:
        dsr = deflated_sharpe_ratio(float(np.median(test_sharpes)), n_trials=max(total_trials, len(fold_results)),
                                      n_obs=max(total_test_obs, 30))

    return {
        "folds": fold_results,
        "mean_validation_sharpe": mean_is,
        "mean_test_sharpe": mean_oos,
        "generalization_ratio": generalization_ratio,
        "deflated_sharpe_ratio": dsr,
        "n_folds": len(fold_results),
    }

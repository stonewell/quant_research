"""Walk-forward validation for a whole UNIVERSE: the standard,
concretely-specified defense against overfitting when auto-tuning strategy
parameters to historical data, applied to a single pooled-across-the-universe
strategy rather than one strategy per symbol.

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
performance) measures how much the strategy degrades out of sample.

Regime classification (Hurst) is fit using ONLY the train window of each
fold, POOLED (median) across every symbol in the universe -- that part is
about which single-symbol TEMPLATE FAMILY to route to, and is unaffected by
the change below. The parameter grid search, however, now matches
`generator.py`'s single-shot `generate()` methodology: each candidate is
scored by ONE multi-asset PORTFOLIO backtest across the whole fold universe
(`portfolio_backtester.py`) rather than N independent per-symbol backtests
pooled by median after the fact. The winning parameterization is chosen by
that one combined equity curve's VALIDATION-window Sharpe, and its
TEST-window Sharpe/trade-count are read from the SAME combined run, once,
after selection -- not pooled from N separate per-symbol test-window slices.
This was a deliberate revision (walk-forward previously used the older
per-symbol-pooled methodology after `generate()` had already moved on from
it -- see strategy_generator/README.md's "Known limitations" history).

Because fold boundaries are defined by bar POSITION (not date), every symbol
in the universe must share the same number of bars / trading calendar --
`run_walkforward` validates this and raises a clear error otherwise, rather
than silently misaligning dates across instruments.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .generator import GeneratorConfig, grid_combinations
from .metrics import deflated_sharpe_ratio, sharpe_ratio
from .portfolio_backtester import run_portfolio_backtest
from .regime import aggregate_regime
from .templates import TEMPLATES_BY_REGIME, NoTradeTemplate

TRADING_DAYS_PER_YEAR = 252


@dataclass
class WalkForwardConfig:
    train_years: float = 4.0
    validation_years: float = 2.0
    test_years: float = 1.0       # also used as the step size between folds
    embargo_days: int = 30
    warmup_buffer_days: int = 250  # extra bars prepended per fold so indicators are warmed up by train_start
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


def _validate_aligned_universe(universe: dict) -> int:
    lengths = {symbol: len(df) for symbol, df in universe.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(
            f"Walk-forward fold boundaries are bar-position-based and require every symbol in the "
            f"universe to share the same number of bars (i.e. the same trading calendar); got {lengths}"
        )
    return next(iter(lengths.values()))


def _evaluate_fold(universe: dict, fold: dict, config: GeneratorConfig) -> dict:
    fold_universe = {s: df.iloc[fold["buffer_start"]:fold["test_end"]] for s, df in universe.items()}
    train_returns = {}
    for s, df in universe.items():
        train_df = df.iloc[fold["train_start"]:fold["train_end"]]
        train_returns[s] = np.log(train_df["Close"] / train_df["Close"].shift(1)).dropna()

    regime = aggregate_regime(train_returns, n_simulations=config.hurst_n_simulations, k=config.hurst_k, seed=config.hurst_seed)
    template_cls = TEMPLATES_BY_REGIME[regime["regime_label"]]
    template = template_cls()

    any_df = next(iter(universe.values()))
    validation_start_date = any_df.index[fold["validation_start"]]
    validation_end_date = any_df.index[fold["validation_end"]]
    test_start_date = any_df.index[fold["test_start"]]
    test_end_date = any_df.index[min(fold["test_end"], len(any_df) - 1)]

    if isinstance(template, NoTradeTemplate):
        return {"regime_label": regime["regime_label"], "pooled_hurst_z": regime["pooled_z"], "template_name": template.name,
                "params": {}, "validation_sharpe": 0.0, "test_sharpe": 0.0, "test_num_trades": 0, "test_num_bars": 0,
                "n_trials": 0, "trusted": True}

    combos = grid_combinations(template.param_grid)
    scored = []
    for params in combos:
        try:
            result = run_portfolio_backtest(
                fold_universe, template, params, max_concurrent_positions=config.max_concurrent_positions,
                max_holding_days=config.single_symbol_max_holding_days, initial_capital=config.initial_capital,
                commission_per_trade=config.commission_per_trade, commission_pct=config.commission_pct,
                slippage_pct=config.slippage_pct, atr_period=config.atr_period,
            )
        except Exception:
            scored.append((params, float("-inf"), None))
            continue
        eq = result["equity_curve"]
        if eq.empty:
            scored.append((params, float("-inf"), None))
            continue
        v_sharpe, _ = _slice_sharpe(eq, validation_start_date, validation_end_date)
        scored.append((params, v_sharpe, result))

    best_params, best_validation_sharpe, best_result = max(scored, key=lambda r: r[1])

    if best_result is None:
        test_sharpe, test_num_bars, test_num_trades = float("-inf"), 0, 0
    else:
        test_sharpe, test_num_bars = _slice_sharpe(best_result["equity_curve"], test_start_date, test_end_date)
        trades = best_result["trades"]
        if trades.empty:
            test_num_trades = 0
        else:
            in_test_window = (trades["date"] >= test_start_date) & (trades["date"] < test_end_date)
            # Actual closed-trade count in the test window, used for the `min_trades_for_trust` gate.
            # The per-symbol-pooled design this replaced conflated this with `test_num_bars` below (a
            # pre-existing naming mismatch that silently inflated trust, since bars-in-window is always
            # >> trades-in-window) -- kept as two separate fields now: `test_num_bars` still feeds DSR's
            # `n_obs` (the return-series sample size backing the Sharpe estimate, which IS bar count, not
            # trade count), while `test_num_trades` is the actual trade count the trust gate needs.
            test_num_trades = int((in_test_window & (trades["side"] == "sell")).sum())

    trusted = best_validation_sharpe > 0 and test_num_trades >= config.min_trades_for_trust
    return {
        "regime_label": regime["regime_label"], "pooled_hurst_z": regime["pooled_z"], "template_name": template.name,
        "params": best_params, "validation_sharpe": best_validation_sharpe, "test_sharpe": test_sharpe,
        "test_num_trades": test_num_trades, "test_num_bars": test_num_bars, "n_trials": len(combos), "trusted": trusted,
    }


def run_walkforward(universe: dict, config: WalkForwardConfig = None) -> dict:
    """`universe`: {symbol: OHLCV DataFrame}. Every symbol must share the
    same number of bars (see `_validate_aligned_universe`); align/trim your
    data (e.g., an inner join on dates) before calling this."""
    config = config or WalkForwardConfig()
    if not universe:
        raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")
    n_bars = _validate_aligned_universe(universe)

    folds = generate_folds(n_bars, config)
    if not folds:
        raise ValueError("Not enough bars to build even one walk-forward fold with the configured window lengths")

    fold_results = [_evaluate_fold(universe, fold, config.generator_config) for fold in folds]

    validation_sharpes = np.array([r["validation_sharpe"] for r in fold_results if np.isfinite(r["validation_sharpe"])])
    test_sharpes = np.array([r["test_sharpe"] for r in fold_results if np.isfinite(r["test_sharpe"])])

    mean_is = validation_sharpes.mean() if len(validation_sharpes) else 0.0
    mean_oos = test_sharpes.mean() if len(test_sharpes) else 0.0
    generalization_ratio = (mean_oos / mean_is) if mean_is > 0 else float("nan")

    total_trials = sum(r["n_trials"] for r in fold_results)
    total_test_obs = sum(r["test_num_bars"] for r in fold_results)  # DSR's n_obs = return-series sample size, not trade count
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
        "n_symbols": len(universe),
    }

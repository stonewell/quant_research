"""Generate a strategy for a single instrument from its own historical data:
classify its regime (Hurst, Monte-Carlo-calibrated), route to the matching
template, search its small parameter grid, and sanity-check the result
against an "equivalent random search" (ERS) benchmark before trusting it.

Research grounding for the ERS check (Chen & Navet, ICONIP 2006): a
concrete, quantifiable pretest for whether a parameter search is doing
anything better than chance is to compare it against a size-matched pool of
RANDOMLY generated candidates on the same data/objective. Beating that pool
is necessary but explicitly NOT sufficient for concluding the result is
genuinely profitable -- it only clears a minimum bar. This generator treats
failing the ERS check as a hard signal to fall back to no-trade rather than
deploy a plausible-looking but likely-lucky parameter combination.
"""

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtester import run_backtest
from .metrics import sharpe_ratio
from .regime import classify_series
from .templates import TEMPLATES_BY_REGIME, NoTradeTemplate


@dataclass
class GeneratorConfig:
    hurst_n_simulations: int = 300
    hurst_k: float = 1.5  # see regime.classify_regime's docstring for why this is stricter than the cited paper's 0.5
    hurst_seed: int = None
    atr_period: int = 14
    n_random_search: int = 200
    ers_percentile_threshold: float = 0.90
    min_trades_for_trust: int = 10
    initial_capital: float = 100_000.0
    commission_per_trade: float = 1.0
    commission_pct: float = 0.0005
    slippage_pct: float = 0.0005
    warmup: int = None


@dataclass
class GeneratedStrategySpec:
    regime_label: str
    hurst: float
    template_name: str
    params: dict
    train_sharpe: float
    train_num_trades: int
    ers_passed: bool
    ers_percentile: float
    n_trials: int
    trusted: bool  # ers_passed AND enough trades to be statistically meaningful (see GeneratorConfig.min_trades_for_trust)


def _backtest_sharpe(df: pd.DataFrame, template, params: dict, config: GeneratorConfig) -> tuple:
    """Returns (annualized Sharpe, num_trades). A run that raises (e.g. not
    enough bars) or produces an empty equity curve scores -inf so it's never
    selected, but the attempt still counts toward `n_trials`."""
    try:
        result = run_backtest(
            df, template, params, initial_capital=config.initial_capital,
            commission_per_trade=config.commission_per_trade, commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct, atr_period=config.atr_period, warmup=config.warmup,
        )
    except Exception:
        return float("-inf"), 0
    eq = result["equity_curve"]
    if eq.empty:
        return float("-inf"), 0
    returns = eq["equity"].pct_change().dropna()
    sr = sharpe_ratio(returns)
    num_trades = (result["trades"]["side"] == "sell").sum() if not result["trades"].empty else 0
    return sr, num_trades


def grid_combinations(param_grid: dict) -> list:
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]


def _random_params(param_grid: dict, rng: np.random.Generator) -> dict:
    params = {}
    for name, values in param_grid.items():
        lo, hi = min(values), max(values)
        params[name] = int(rng.integers(lo, hi + 1))
    return params


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, df: pd.DataFrame) -> GeneratedStrategySpec:
        cfg = self.config
        log_returns = np.log(df["Close"] / df["Close"].shift(1)).dropna()
        regime = classify_series(log_returns, n_simulations=cfg.hurst_n_simulations, k=cfg.hurst_k, seed=cfg.hurst_seed)

        template_cls = TEMPLATES_BY_REGIME[regime["regime_label"]]
        template = template_cls()

        if isinstance(template, NoTradeTemplate):
            return GeneratedStrategySpec(
                regime_label=regime["regime_label"], hurst=regime["hurst"], template_name=template.name,
                params={}, train_sharpe=0.0, train_num_trades=0, ers_passed=True, ers_percentile=1.0,
                n_trials=0, trusted=True,
            )

        # --- constrained grid search over the template's small, fixed parameter set ---
        combos = grid_combinations(template.param_grid)
        grid_results = [(params, *_backtest_sharpe(df, template, params, cfg)) for params in combos]
        best_params, best_sharpe, best_trades = max(grid_results, key=lambda r: r[1])

        # --- equivalent random search (ERS) sanity check ---
        rng = np.random.default_rng(cfg.hurst_seed)
        random_sharpes = []
        for _ in range(cfg.n_random_search):
            random_params = _random_params(template.param_grid, rng)
            sr, _ = _backtest_sharpe(df, template, random_params, cfg)
            if sr != float("-inf"):
                random_sharpes.append(sr)

        if random_sharpes:
            ers_percentile = float((np.array(random_sharpes) < best_sharpe).mean())
        else:
            ers_percentile = 1.0  # no valid random baseline to compare against
        ers_passed = ers_percentile >= cfg.ers_percentile_threshold

        trusted = ers_passed and best_trades >= cfg.min_trades_for_trust
        return GeneratedStrategySpec(
            regime_label=regime["regime_label"], hurst=regime["hurst"], template_name=template.name,
            params=best_params, train_sharpe=best_sharpe, train_num_trades=int(best_trades),
            ers_passed=ers_passed, ers_percentile=ers_percentile,
            n_trials=len(combos) + len(random_sharpes), trusted=trusted,
        )

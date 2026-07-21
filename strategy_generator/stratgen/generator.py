"""Generate ONE allocation strategy for a whole UNIVERSE of instruments.

Searches across a set of portfolio allocation templates (e.g., Equal Weight,
Inverse Volatility, Cross-Sectional Momentum) to find the best-performing
rebalance schedule and parameter set for the given basket of assets.

The Equivalent Random Search (ERS) check compares the winning strategy against
a pool of random-weight portfolios (where weights sum to 1.0 at each rebalance)
to ensure the result is genuinely better than chance.
"""

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.allocation_templates import ALLOCATION_TEMPLATES
from .metrics import sharpe_ratio


@dataclass
class GeneratorConfig:
    n_random_search: int = 200
    ers_percentile_threshold: float = 0.90
    min_rebalances_for_trust: int = 4
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0005
    slippage_pct: float = 0.0005
    seed: int = None


@dataclass
class GeneratedStrategySpec:
    n_symbols: int
    template_name: str
    params: dict
    universe_sharpe: float
    total_turnover: float
    total_rebalances: int
    ers_passed: bool
    ers_percentile: float
    n_trials: int
    trusted: bool
    explanation: str
    target_weights: pd.DataFrame  # sparse: NaN except on actual rebalance dates (see allocation_templates.py)


def _portfolio_score(universe: dict, template, params: dict, config: GeneratorConfig) -> tuple:
    """Backtest `params` using the allocation backtester."""
    try:
        target_weights = template.generate_weights(universe, params)
        if target_weights.empty:
            return float("-inf"), 0, 0.0
            
        result = run_allocation_backtest(
            universe, target_weights,
            initial_capital=config.initial_capital,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct
        )
    except Exception:
        return float("-inf"), 0, 0.0
        
    eq = result["equity_curve"]
    if eq.empty:
        return float("-inf"), 0, 0.0
        
    returns = eq["equity"].pct_change().dropna()
    sr = sharpe_ratio(returns)
    
    return sr, result["total_rebalances"], result["total_turnover"]


def grid_combinations(param_grid: dict) -> list:
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*param_grid.values())]


def _random_weights(universe: dict, rebalance_freq_days: int, rng: np.random.Generator) -> pd.DataFrame:
    """Generates a random valid weight matrix (sums to 1.0 on rebalance days).
    Sparse (NaN off rebalance dates), matching the contract every
    AllocationTemplate.generate_weights must follow -- see allocation_templates.py."""
    symbols = list(universe.keys())
    if not symbols:
        return pd.DataFrame()

    master_index = universe[symbols[0]].index
    rebalance_dates = master_index[::rebalance_freq_days]

    n_dates = len(rebalance_dates)
    n_symbols = len(symbols)

    # Generate random weights (Dirichlet distribution equivalent)
    raw_w = rng.exponential(scale=1.0, size=(n_dates, n_symbols))
    norm_w = raw_w / raw_w.sum(axis=1, keepdims=True)

    weights_rebal = pd.DataFrame(norm_w, index=rebalance_dates, columns=symbols)

    weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
    weights_df.loc[rebalance_dates] = weights_rebal

    return weights_df


class RandomAllocationTemplate:
    """A dummy template used purely for the ERS check."""
    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        
    def generate_weights(self, universe: dict, params: dict) -> pd.DataFrame:
        return _random_weights(universe, params["rebalance_freq_days"], self.rng)


def _search_allocation(universe: dict, cfg: GeneratorConfig) -> dict:
    """Grid search across all allocation templates."""
    
    all_results = []
    total_grid_trials = 0
    
    # 1. Grid Search across all templates
    for template_cls in ALLOCATION_TEMPLATES:
        template = template_cls()
        combos = grid_combinations(template.param_grid)
        total_grid_trials += len(combos)
        
        for params in combos:
            score, rebalances, turnover = _portfolio_score(universe, template, params, cfg)
            all_results.append({
                "template": template,
                "params": params,
                "score": score,
                "rebalances": rebalances,
                "turnover": turnover
            })
            
    # Find the best template + params
    best_result = max(all_results, key=lambda r: r["score"])
    best_score = best_result["score"]
    
    # 2. Equivalent Random Search (ERS)
    rng = np.random.default_rng(cfg.seed)
    random_scores = []
    random_template = RandomAllocationTemplate(rng)
    
    # Use the winning rebalance frequency for a fair comparison
    winning_freq = best_result["params"].get("rebalance_freq_days", 21)
    
    for _ in range(cfg.n_random_search):
        score, _, _ = _portfolio_score(universe, random_template, {"rebalance_freq_days": winning_freq}, cfg)
        if np.isfinite(score):
            random_scores.append(score)

    ers_percentile = float((np.array(random_scores) < best_score).mean()) if random_scores else 1.0
    ers_passed = ers_percentile >= cfg.ers_percentile_threshold
    trusted = ers_passed and best_result["rebalances"] >= cfg.min_rebalances_for_trust

    return {
        "template": best_result["template"],
        "params": best_result["params"],
        "score": best_score,
        "total_rebalances": best_result["rebalances"],
        "total_turnover": best_result["turnover"],
        "ers_passed": ers_passed,
        "ers_percentile": ers_percentile,
        "n_trials": total_grid_trials + len(random_scores),
        "trusted": trusted,
    }


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, universe: dict) -> GeneratedStrategySpec:
        cfg = self.config
        if not universe:
            raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

        result = _search_allocation(universe, cfg)
        
        template = result["template"]
        params = result["params"]
        
        # Generate the final weights for output
        target_weights = template.generate_weights(universe, params)
        explanation = template.explain_weights(params)

        return GeneratedStrategySpec(
            n_symbols=len(universe),
            template_name=template.name,
            params=params,
            universe_sharpe=result["score"],
            total_turnover=result["total_turnover"],
            total_rebalances=result["total_rebalances"],
            ers_passed=result["ers_passed"],
            ers_percentile=result["ers_percentile"],
            n_trials=result["n_trials"],
            trusted=result["trusted"],
            explanation=explanation,
            target_weights=target_weights
        )

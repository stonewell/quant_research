"""Generate ONE strategy for a whole UNIVERSE of instruments, from their
pooled historical data -- not a separate strategy per symbol.

Why pool across the universe rather than generate per-symbol: research
flagged, as an open and unresolved question, that running a per-instrument
generator across a large universe is itself a multiple-comparisons problem
-- generating N independent strategies (one per symbol) and reporting
whichever backtested best is effectively N trials without the correction
that implies (exactly the failure mode the Deflated Sharpe Ratio exists to
catch, just at the instrument level instead of the parameter level).
Classifying the universe's regime AND selecting parameters by their POOLED
performance across every symbol treats "generalizes across many different
instruments" as the actual search objective -- a materially stronger
anti-overfitting property than "fits this one instrument's history best,"
at the cost of producing one strategy that may not be the single best fit
for any individual name.

Research grounding for the ERS check (Chen & Navet, ICONIP 2006): a
concrete, quantifiable pretest for whether a parameter search is doing
anything better than chance is to compare it against a size-matched pool of
RANDOMLY generated candidates on the same data/objective -- here, the same
POOLED-across-the-universe objective. Beating that pool is necessary but
explicitly NOT sufficient for concluding the result is genuinely profitable.
"""

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtester import run_backtest
from .metrics import sharpe_ratio
from .regime import aggregate_regime
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
    aggregation: str = "median"  # "median" or "mean" -- how per-symbol Sharpe ratios are pooled into one score


@dataclass
class GeneratedStrategySpec:
    regime_label: str
    pooled_hurst_z: float
    n_symbols: int
    template_name: str
    params: dict
    universe_sharpe: float          # the pooled (median/mean) Sharpe across every symbol in the universe
    total_num_trades: int
    per_symbol_sharpe: dict          # transparency: how consistent is this "universal" strategy across instruments?
    per_symbol_num_trades: dict
    ers_passed: bool
    ers_percentile: float
    n_trials: int
    trusted: bool  # ers_passed AND enough total trades to be statistically meaningful


def _backtest_sharpe(df: pd.DataFrame, template, params: dict, config: GeneratorConfig) -> tuple:
    """Returns (annualized Sharpe, num_trades) for one symbol. A run that
    raises (e.g. not enough bars) or produces an empty equity curve scores
    -inf so it's never selected, but the attempt still counts toward `n_trials`."""
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


def _pool(values: list, method: str) -> float:
    values = np.array(values)
    if len(values) == 0:
        return float("-inf")
    return float(np.median(values)) if method == "median" else float(values.mean())


def _aggregate_backtest_score(universe: dict, template, params: dict, config: GeneratorConfig) -> tuple:
    """Backtest `params` independently on every symbol in the universe, and
    pool the resulting Sharpe ratios into one universe-level score. Returns
    (pooled_score, per_symbol_sharpe, per_symbol_trades)."""
    per_symbol_sharpe, per_symbol_trades = {}, {}
    for symbol, df in universe.items():
        sr, trades = _backtest_sharpe(df, template, params, config)
        per_symbol_sharpe[symbol] = sr
        per_symbol_trades[symbol] = trades
    valid = [v for v in per_symbol_sharpe.values() if np.isfinite(v)]
    pooled = _pool(valid, config.aggregation)
    return pooled, per_symbol_sharpe, per_symbol_trades


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, universe: dict) -> GeneratedStrategySpec:
        """`universe`: {symbol: OHLCV DataFrame}. Symbols do NOT need to
        share a common date range or length here -- unlike walk-forward
        (which needs aligned fold boundaries), a one-shot generation just
        backtests each symbol independently and pools the results."""
        cfg = self.config
        if not universe:
            raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

        returns_by_symbol = {symbol: np.log(df["Close"] / df["Close"].shift(1)).dropna()
                              for symbol, df in universe.items()}
        regime = aggregate_regime(returns_by_symbol, n_simulations=cfg.hurst_n_simulations, k=cfg.hurst_k, seed=cfg.hurst_seed)

        template_cls = TEMPLATES_BY_REGIME[regime["regime_label"]]
        template = template_cls()

        if isinstance(template, NoTradeTemplate):
            return GeneratedStrategySpec(
                regime_label=regime["regime_label"], pooled_hurst_z=regime["pooled_z"], n_symbols=regime["n_symbols"],
                template_name=template.name, params={}, universe_sharpe=0.0, total_num_trades=0,
                per_symbol_sharpe={s: 0.0 for s in universe}, per_symbol_num_trades={s: 0 for s in universe},
                ers_passed=True, ers_percentile=1.0, n_trials=0, trusted=True,
            )

        # --- constrained grid search, selected by POOLED performance across the whole universe ---
        combos = grid_combinations(template.param_grid)
        grid_results = [(params, *_aggregate_backtest_score(universe, template, params, cfg)) for params in combos]
        best_params, best_pooled, best_per_symbol_sharpe, best_per_symbol_trades = max(grid_results, key=lambda r: r[1])

        # --- equivalent random search (ERS), also pooled across the universe ---
        rng = np.random.default_rng(cfg.hurst_seed)
        random_pooled_scores = []
        for _ in range(cfg.n_random_search):
            random_params = _random_params(template.param_grid, rng)
            pooled, _, _ = _aggregate_backtest_score(universe, template, random_params, cfg)
            if np.isfinite(pooled):
                random_pooled_scores.append(pooled)

        if random_pooled_scores:
            ers_percentile = float((np.array(random_pooled_scores) < best_pooled).mean())
        else:
            ers_percentile = 1.0
        ers_passed = ers_percentile >= cfg.ers_percentile_threshold

        total_trades = int(sum(best_per_symbol_trades.values()))
        trusted = ers_passed and total_trades >= cfg.min_trades_for_trust

        return GeneratedStrategySpec(
            regime_label=regime["regime_label"], pooled_hurst_z=regime["pooled_z"], n_symbols=regime["n_symbols"],
            template_name=template.name, params=best_params, universe_sharpe=best_pooled, total_num_trades=total_trades,
            per_symbol_sharpe=best_per_symbol_sharpe, per_symbol_num_trades=best_per_symbol_trades,
            ers_passed=ers_passed, ers_percentile=ers_percentile,
            n_trials=len(combos) + len(random_pooled_scores), trusted=trusted,
        )

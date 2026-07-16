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

Not single-instrument-only: alongside the pooled single-symbol search below,
`generate()` also runs `pairs_search.search_pairs_candidates` over every pair
in the universe and compares its (ERS-checked, trust-gated) winner against
the single-symbol winner, reporting whichever is better-supported --
removing the earlier architecture limit where a pairs-trading (inherently
two-instrument, long-short) strategy could never be a generator OUTPUT, only
a manually-invoked module. See pairs_search.py's own docstring for how the
same multiple-comparisons defenses are applied to that combinatorial search.
"""

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtester import run_backtest
from .metrics import sharpe_ratio
from .pairs_search import PairsSearchConfig, search_pairs_candidates
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
    search_pairs: bool = True     # also search pairs-trading candidates across the universe (see module docstring)
    pairs_max_holding_days: int = 63   # fixed, not searched -- keeps pairs holds under this project's 3-month target
    max_pairs_to_search: int = 50       # combinatorial cap on distinct pairs backtested for large universes


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
    # Everything below is new, defaulted for backward compatibility with existing callers that only
    # read the fields above. `strategy_family` is the one new field worth always checking: the winning
    # candidate can now be "pairs", not just "single_symbol"/"no_trade" -- when it is, the fields above
    # describe the WINNING PAIR (template_name="distance_pairs", params=lookback/entry_zscore, etc.),
    # and `pair_symbols`/`pairs_result` carry the pairs-specific detail. `single_symbol_result` and
    # `pairs_result` are BOTH populated (when applicable) regardless of which family won, so you can see
    # what the runner-up looked like -- the same "expose the losing candidates" transparency this
    # project already uses for `per_symbol_sharpe`.
    strategy_family: str = "single_symbol"  # "single_symbol" | "pairs" | "no_trade"
    pair_symbols: tuple = None
    single_symbol_result: dict = None
    pairs_result: object = None  # pairs_search.PairsSearchResult, or None if not searched/no pairs available


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


def _search_single_symbol(universe: dict, template, cfg: GeneratorConfig) -> dict:
    """The original constrained grid search, selected by POOLED performance
    across the whole universe, plus its Equivalent Random Search check.
    Returns a plain dict (rather than a dataclass) since it's an internal
    intermediate the two candidates get compared through, not part of the
    public return shape."""
    combos = grid_combinations(template.param_grid)
    grid_results = [(params, *_aggregate_backtest_score(universe, template, params, cfg)) for params in combos]
    best_params, best_pooled, best_per_symbol_sharpe, best_per_symbol_trades = max(grid_results, key=lambda r: r[1])

    rng = np.random.default_rng(cfg.hurst_seed)
    random_pooled_scores = []
    for _ in range(cfg.n_random_search):
        random_params = _random_params(template.param_grid, rng)
        pooled, _, _ = _aggregate_backtest_score(universe, template, random_params, cfg)
        if np.isfinite(pooled):
            random_pooled_scores.append(pooled)

    ers_percentile = float((np.array(random_pooled_scores) < best_pooled).mean()) if random_pooled_scores else 1.0
    ers_passed = ers_percentile >= cfg.ers_percentile_threshold
    total_trades = int(sum(best_per_symbol_trades.values()))
    trusted = ers_passed and total_trades >= cfg.min_trades_for_trust

    return {
        "template_name": template.name, "params": best_params, "score": best_pooled,
        "total_trades": total_trades, "per_symbol_sharpe": best_per_symbol_sharpe,
        "per_symbol_num_trades": best_per_symbol_trades, "ers_passed": ers_passed,
        "ers_percentile": ers_percentile, "n_trials": len(combos) + len(random_pooled_scores),
        "trusted": trusted,
    }


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, universe: dict) -> GeneratedStrategySpec:
        """`universe`: {symbol: OHLCV DataFrame}. Symbols do NOT need to
        share a common date range or length here for the single-symbol
        search -- unlike walk-forward (which needs aligned fold boundaries),
        a one-shot generation just backtests each symbol independently and
        pools the results. Pairs candidates align each pair's own dates
        internally (see pairs_search.py).

        Searches TWO independent candidate families -- single-symbol (routed
        by the universe's pooled Hurst regime, as before) and pairs-trading
        (searched across every pair in the universe, regardless of that
        regime, since pairs trade the RELATIONSHIP between two instruments,
        not either one's own trend/mean-reversion character) -- and returns
        whichever is better-supported by the evidence: a TRUSTED candidate
        (ERS-passed and enough trades) beats an untrusted one regardless of
        raw score; among two trusted (or two untrusted) candidates, the
        higher-scoring one wins. Both candidates' full detail are returned
        regardless of which wins (`single_symbol_result`/`pairs_result`), so
        the runner-up is never silently discarded.
        """
        cfg = self.config
        if not universe:
            raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

        returns_by_symbol = {symbol: np.log(df["Close"] / df["Close"].shift(1)).dropna()
                              for symbol, df in universe.items()}
        regime = aggregate_regime(returns_by_symbol, n_simulations=cfg.hurst_n_simulations, k=cfg.hurst_k, seed=cfg.hurst_seed)

        template_cls = TEMPLATES_BY_REGIME[regime["regime_label"]]
        template = template_cls()

        single_result = None
        if not isinstance(template, NoTradeTemplate):
            single_result = _search_single_symbol(universe, template, cfg)

        pairs_result = None
        if cfg.search_pairs and len(universe) >= 2:
            pairs_result = search_pairs_candidates(universe, cfg, PairsSearchConfig(
                max_holding_days=cfg.pairs_max_holding_days, max_pairs_to_search=cfg.max_pairs_to_search,
                seed=cfg.hurst_seed,
            ))

        candidates = []
        if single_result is not None:
            candidates.append(("single_symbol", single_result["score"], single_result["trusted"]))
        if pairs_result is not None:
            candidates.append(("pairs", pairs_result.sharpe, pairs_result.trusted))

        trusted_candidates = [c for c in candidates if c[2]]
        if trusted_candidates:
            winner = max(trusted_candidates, key=lambda c: c[1])[0]
        elif candidates:
            # Nothing cleared the ERS/min-trades bar -- report the higher-scoring candidate anyway,
            # marked untrusted, same transparency the single-symbol-only path always had.
            winner = max(candidates, key=lambda c: c[1])[0]
        else:
            winner = "no_trade"

        common = dict(regime_label=regime["regime_label"], pooled_hurst_z=regime["pooled_z"],
                      n_symbols=regime["n_symbols"], single_symbol_result=single_result, pairs_result=pairs_result)

        if winner == "no_trade":
            return GeneratedStrategySpec(
                **common, template_name=NoTradeTemplate().name, params={}, universe_sharpe=0.0, total_num_trades=0,
                per_symbol_sharpe={s: 0.0 for s in universe}, per_symbol_num_trades={s: 0 for s in universe},
                ers_passed=True, ers_percentile=1.0, n_trials=0, trusted=True, strategy_family="no_trade",
            )
        elif winner == "pairs":
            return GeneratedStrategySpec(
                **common, template_name="distance_pairs", params=pairs_result.params, universe_sharpe=pairs_result.sharpe,
                total_num_trades=pairs_result.num_trades,
                per_symbol_sharpe={pairs_result.symbol_a: pairs_result.sharpe, pairs_result.symbol_b: pairs_result.sharpe},
                per_symbol_num_trades={pairs_result.symbol_a: pairs_result.num_trades, pairs_result.symbol_b: pairs_result.num_trades},
                ers_passed=pairs_result.ers_passed, ers_percentile=pairs_result.ers_percentile,
                n_trials=pairs_result.n_trials, trusted=pairs_result.trusted, strategy_family="pairs",
                pair_symbols=(pairs_result.symbol_a, pairs_result.symbol_b),
            )
        else:
            return GeneratedStrategySpec(
                **common, template_name=single_result["template_name"], params=single_result["params"],
                universe_sharpe=single_result["score"], total_num_trades=single_result["total_trades"],
                per_symbol_sharpe=single_result["per_symbol_sharpe"], per_symbol_num_trades=single_result["per_symbol_num_trades"],
                ers_passed=single_result["ers_passed"], ers_percentile=single_result["ers_percentile"],
                n_trials=single_result["n_trials"], trusted=single_result["trusted"], strategy_family="single_symbol",
            )

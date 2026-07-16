"""Generate ONE strategy for a whole UNIVERSE of instruments, from their
combined historical data -- not a separate strategy per symbol.

Single-symbol candidates are scored by ONE multi-asset PORTFOLIO backtest
across the whole universe (`portfolio_backtester.py`): every signaled symbol
is traded concurrently, sharing one cash pool and one combined equity curve,
capped at `max_concurrent_positions` open slots and `single_symbol_
max_holding_days` per position (keeping this project's sub-3-month holding
target enforced even for a template whose own exit signal has no such
guarantee, e.g. a momentum crossover riding a long trend). This replaced an
earlier design that instead ran N fully independent single-asset backtests
(each with its own 100%-of-capital, one-symbol-at-a-time position) and
pooled the resulting Sharpe ratios by median/mean after the fact --
approximating "trade the universe" rather than actually doing it. Scoring by
one combined equity curve is both more realistic (shared capital means a
strategy that would try to hold too many positions at once gets sized down,
exactly like a real account) and, as a side effect, exactly what "evaluate
the multiple assets trading result as a whole" means: one number from one
run, not many numbers pooled together.

Research grounding for the ERS check (Chen & Navet, ICONIP 2006): a
concrete, quantifiable pretest for whether a parameter search is doing
anything better than chance is to compare it against a size-matched pool of
RANDOMLY generated candidates on the same data/objective -- here, the same
combined-portfolio objective. Beating that pool is necessary but explicitly
NOT sufficient for concluding the result is genuinely profitable.

Not single-instrument-only: alongside the single-symbol portfolio search
below, `generate()` also runs `pairs_search.search_pairs_candidates` over
every pair in the universe and compares its (ERS-checked, trust-gated)
winner against the single-symbol winner, reporting whichever is
better-supported -- removing the earlier architecture limit where a
pairs-trading (inherently two-instrument, long-short) strategy could never
be a generator OUTPUT, only a manually-invoked module. See pairs_search.py's
own docstring for how the same multiple-comparisons defenses are applied to
that combinatorial search.
"""

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import sharpe_ratio
from .pairs_search import PairsSearchConfig, search_pairs_candidates
from .portfolio_backtester import run_portfolio_backtest
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
    max_concurrent_positions: int = 10   # equal-weight slot cap for the single-symbol portfolio backtest
    single_symbol_max_holding_days: int = 63  # fixed, not searched -- keeps holds under this project's 3-month target
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
    universe_sharpe: float          # the WINNING candidate's own Sharpe -- one combined-portfolio backtest, not a pooled median/mean of separate ones
    total_num_trades: int
    per_symbol_pnl: dict             # transparency: realized P&L contributed by each symbol within the winning (shared-portfolio, for single-symbol; per-leg, for pairs) run
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
    # project already uses for `per_symbol_pnl`.
    strategy_family: str = "single_symbol"  # "single_symbol" | "pairs" | "no_trade"
    pair_symbols: tuple = None
    single_symbol_result: dict = None
    pairs_result: object = None  # pairs_search.PairsSearchResult, or None if not searched/no pairs available


def _portfolio_score(universe: dict, template, params: dict, config: GeneratorConfig) -> tuple:
    """Backtest `params` as ONE multi-asset portfolio across the whole
    universe (see portfolio_backtester.py) and score it from that single
    combined equity curve -- not by pooling N independently-computed
    per-symbol Sharpe ratios. A run that raises (e.g. not enough aligned
    bars) or produces an empty equity curve scores -inf so it's never
    selected, but the attempt still counts toward `n_trials`. Returns
    (sharpe, total_num_trades, per_symbol_num_trades, per_symbol_pnl)."""
    try:
        result = run_portfolio_backtest(
            universe, template, params, max_concurrent_positions=config.max_concurrent_positions,
            max_holding_days=config.single_symbol_max_holding_days, initial_capital=config.initial_capital,
            commission_per_trade=config.commission_per_trade, commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct, atr_period=config.atr_period, warmup=config.warmup,
        )
    except Exception:
        return float("-inf"), 0, {s: 0 for s in universe}, {s: 0.0 for s in universe}
    eq = result["equity_curve"]
    if eq.empty:
        return float("-inf"), 0, {s: 0 for s in universe}, {s: 0.0 for s in universe}
    returns = eq["equity"].pct_change().dropna()
    sr = sharpe_ratio(returns)
    trades = result["trades"]
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades
    per_symbol_num_trades = {s: int((sells["symbol"] == s).sum()) for s in universe}
    per_symbol_pnl = {s: float(sells.loc[sells["symbol"] == s, "pnl"].sum()) if not sells.empty else 0.0 for s in universe}
    return sr, int(len(sells)), per_symbol_num_trades, per_symbol_pnl


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


def _search_single_symbol(universe: dict, template, cfg: GeneratorConfig) -> dict:
    """Constrained grid search, each candidate scored by ONE multi-asset
    portfolio backtest across the whole universe (see `_portfolio_score`),
    plus its Equivalent Random Search check. Returns a plain dict (rather
    than a dataclass) since it's an internal intermediate the two candidate
    families get compared through, not part of the public return shape."""
    combos = grid_combinations(template.param_grid)
    grid_results = [(params, *_portfolio_score(universe, template, params, cfg)) for params in combos]
    best_params, best_score, best_total_trades, best_per_symbol_trades, best_per_symbol_pnl = max(
        grid_results, key=lambda r: r[1])

    rng = np.random.default_rng(cfg.hurst_seed)
    random_scores = []
    for _ in range(cfg.n_random_search):
        random_params = _random_params(template.param_grid, rng)
        score, _, _, _ = _portfolio_score(universe, template, random_params, cfg)
        if np.isfinite(score):
            random_scores.append(score)

    ers_percentile = float((np.array(random_scores) < best_score).mean()) if random_scores else 1.0
    ers_passed = ers_percentile >= cfg.ers_percentile_threshold
    trusted = ers_passed and best_total_trades >= cfg.min_trades_for_trust

    return {
        "template_name": template.name, "params": best_params, "score": best_score,
        "total_trades": best_total_trades, "per_symbol_num_trades": best_per_symbol_trades,
        "per_symbol_pnl": best_per_symbol_pnl, "ers_passed": ers_passed,
        "ers_percentile": ers_percentile, "n_trials": len(combos) + len(random_scores),
        "trusted": trusted,
    }


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, universe: dict) -> GeneratedStrategySpec:
        """`universe`: {symbol: OHLCV DataFrame}. Both candidate families
        now require a shared trading calendar across the universe -- the
        single-symbol search runs ONE portfolio backtest holding concurrent
        positions across every symbol (`portfolio_backtester.py` inner-joins
        the universe's dates internally), and pairs candidates align each
        pair's own dates internally (see pairs_search.py). Unlike the design
        this replaced, symbols with materially different listing histories
        will have their non-overlapping bars silently excluded from
        scoring, not backtested independently on their own full history.

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
                per_symbol_pnl={s: 0.0 for s in universe}, per_symbol_num_trades={s: 0 for s in universe},
                ers_passed=True, ers_percentile=1.0, n_trials=0, trusted=True, strategy_family="no_trade",
            )
        elif winner == "pairs":
            return GeneratedStrategySpec(
                **common, template_name="distance_pairs", params=pairs_result.params, universe_sharpe=pairs_result.sharpe,
                total_num_trades=pairs_result.num_trades, per_symbol_pnl=pairs_result.per_symbol_pnl,
                per_symbol_num_trades={pairs_result.symbol_a: pairs_result.num_trades, pairs_result.symbol_b: pairs_result.num_trades},
                ers_passed=pairs_result.ers_passed, ers_percentile=pairs_result.ers_percentile,
                n_trials=pairs_result.n_trials, trusted=pairs_result.trusted, strategy_family="pairs",
                pair_symbols=(pairs_result.symbol_a, pairs_result.symbol_b),
            )
        else:
            return GeneratedStrategySpec(
                **common, template_name=single_result["template_name"], params=single_result["params"],
                universe_sharpe=single_result["score"], total_num_trades=single_result["total_trades"],
                per_symbol_pnl=single_result["per_symbol_pnl"], per_symbol_num_trades=single_result["per_symbol_num_trades"],
                ers_passed=single_result["ers_passed"], ers_percentile=single_result["ers_percentile"],
                n_trials=single_result["n_trials"], trusted=single_result["trusted"], strategy_family="single_symbol",
            )

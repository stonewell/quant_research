"""Pairs-candidate search over a universe -- the pairs-trading analogue of
generator.py's constrained single-symbol grid search, removing the
generator's original single-instrument-per-symbol architecture limit: pairs
trading is a bet on the RELATIONSHIP between two instruments, not on any one
instrument's own trend/mean-reversion regime, so it's searched as an
independent candidate family alongside (not gated by) the universe's Hurst
regime classification -- see `generator.StrategyGenerator.generate`, which
now compares this family's winner against the single-symbol winner and
reports whichever is better-supported by the evidence.

Multiple-comparisons risk, made explicit rather than ignored: searching
every pair in a universe of N symbols is itself a combinatorial number of
trials (up to C(N,2) pairs x this module's own small param grid) -- exactly
the kind of search this whole project's design otherwise guards against for
single-symbol templates (Allen & Karjalainen 1999's data-snooping finding is
the reason the rest of this project restricts to small, fixed template/
parameter sets rather than open-ended search). The SAME two defenses used
for the single-symbol path are applied here:

1. Equivalent Random Search (Chen & Navet, ICONIP 2006): the winning
   pair+params combination must beat a size-matched pool of RANDOMLY chosen
   pair+params combinations, evaluated the same way. Beating that pool is
   necessary but not sufficient for concluding the result is genuinely good.
2. The true trial count (pairs searched x param grid, plus the random pool)
   is tracked and returned so the caller can feed it into the Deflated
   Sharpe Ratio, exactly as the single-symbol path already does.

For a large universe, C(N,2) grows quadratically -- `max_pairs_to_search`
caps how many distinct pairs are actually backtested (a uniform random
sample without replacement, seeded for reproducibility), and the result
reports both `n_pairs_searched` and `n_pairs_total` so a capped run is
never mistaken for having covered every possible pair.
"""

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import sharpe_ratio
from .pairs import PairsConfig
from .pairs_backtester import run_pairs_backtest

PAIRS_PARAM_GRID = {"lookback": [30, 60, 90], "entry_zscore": [1.5, 2.0, 2.5]}


@dataclass
class PairsSearchConfig:
    max_holding_days: int = 63          # fixed, not searched -- keeps holds under this project's 3-month target
    max_pairs_to_search: int = 50       # combinatorial cap for large universes -- reported, never silent
    seed: int = None


@dataclass
class PairsSearchResult:
    symbol_a: str
    symbol_b: str
    params: dict
    sharpe: float
    num_trades: int
    ers_passed: bool
    ers_percentile: float
    n_trials: int
    trusted: bool
    n_pairs_searched: int
    n_pairs_total: int


def _align(df_a: pd.DataFrame, df_b: pd.DataFrame) -> tuple:
    common = df_a.index.intersection(df_b.index)
    return df_a.loc[common], df_b.loc[common]


def _pair_score(df_a: pd.DataFrame, df_b: pd.DataFrame, params: dict,
                 pairs_config: "PairsSearchConfig", gen_config) -> tuple:
    """Returns (annualized Sharpe, num_round_trips) for one pair+params
    combo. A run that raises (e.g. not enough aligned bars for the
    configured lookback) or produces an empty equity curve scores -inf so
    it's never selected, but the attempt still counts toward `n_trials`."""
    try:
        pc = PairsConfig(lookback=params["lookback"], entry_zscore=params["entry_zscore"],
                         max_holding_days=pairs_config.max_holding_days)
        result = run_pairs_backtest(
            df_a, df_b, pc, initial_capital=gen_config.initial_capital,
            commission_per_trade=gen_config.commission_per_trade, commission_pct=gen_config.commission_pct,
            slippage_pct=gen_config.slippage_pct, warmup=gen_config.warmup,
        )
    except Exception:
        return float("-inf"), 0
    eq = result["equity_curve"]
    if eq.empty:
        return float("-inf"), 0
    returns = eq["equity"].pct_change().dropna()
    sr = sharpe_ratio(returns)
    num_round_trips = int((result["trades"]["event"] != "entry").sum() // 2) if not result["trades"].empty else 0
    return sr, num_round_trips


def search_pairs_candidates(universe: dict, gen_config, pairs_config: PairsSearchConfig = None):
    """`universe`: {symbol: OHLCV DataFrame}, same shape as
    `generator.StrategyGenerator.generate`'s input. Returns `None` if there
    are fewer than 2 symbols to pair, otherwise a `PairsSearchResult` for the
    single best pair+params combination found (by raw Sharpe -- `trusted`
    reports whether it cleared the ERS/min-trades bar, same convention as
    the single-symbol search)."""
    pairs_config = pairs_config or PairsSearchConfig()
    symbols = list(universe.keys())
    all_pairs = list(itertools.combinations(symbols, 2))
    if not all_pairs:
        return None

    rng = np.random.default_rng(pairs_config.seed)
    if len(all_pairs) > pairs_config.max_pairs_to_search:
        chosen_idx = rng.choice(len(all_pairs), size=pairs_config.max_pairs_to_search, replace=False)
        searched_pairs = [all_pairs[i] for i in chosen_idx]
    else:
        searched_pairs = all_pairs

    combos = [dict(zip(PAIRS_PARAM_GRID.keys(), c)) for c in itertools.product(*PAIRS_PARAM_GRID.values())]
    aligned = {pair: _align(universe[pair[0]], universe[pair[1]]) for pair in searched_pairs}

    grid_results = []
    for pair in searched_pairs:
        df_a, df_b = aligned[pair]
        for params in combos:
            sr, trades = _pair_score(df_a, df_b, params, pairs_config, gen_config)
            grid_results.append((pair, params, sr, trades))

    best_pair, best_params, best_sharpe, best_trades = max(grid_results, key=lambda r: r[2])

    random_scores = []
    for _ in range(gen_config.n_random_search):
        pair = searched_pairs[rng.integers(len(searched_pairs))]
        params = {"lookback": int(rng.integers(20, 121)), "entry_zscore": float(rng.uniform(1.0, 3.0))}
        df_a, df_b = aligned[pair]
        sr, _ = _pair_score(df_a, df_b, params, pairs_config, gen_config)
        if np.isfinite(sr):
            random_scores.append(sr)

    ers_percentile = float((np.array(random_scores) < best_sharpe).mean()) if random_scores else 1.0
    ers_passed = ers_percentile >= gen_config.ers_percentile_threshold
    trusted = ers_passed and best_trades >= gen_config.min_trades_for_trust

    return PairsSearchResult(
        symbol_a=best_pair[0], symbol_b=best_pair[1], params=best_params, sharpe=best_sharpe,
        num_trades=best_trades, ers_passed=ers_passed, ers_percentile=ers_percentile,
        n_trials=len(searched_pairs) * len(combos) + len(random_scores), trusted=trusted,
        n_pairs_searched=len(searched_pairs), n_pairs_total=len(all_pairs),
    )

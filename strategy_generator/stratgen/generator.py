"""Generate ONE allocation strategy for a whole UNIVERSE of instruments.

Searches across a set of portfolio allocation templates (e.g., Equal Weight,
Inverse Volatility, Cross-Sectional Momentum) to find the best-performing
rebalance schedule and parameter set for the given basket of assets.

The Equivalent Random Search (ERS) check compares the winning strategy against
a pool of random-weight portfolios (where weights sum to 1.0 at each rebalance)
to ensure the result is genuinely better than chance.
"""

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.allocation_search import (
    RandomAllocationTemplate,
    grid_combinations,
    grid_search_template,
    run_ers_validation,
)
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
    # How close (as a fraction of the leading score, with `factor_tiebreak_epsilon`
    # itself also used as an absolute floor so near-zero Sharpe scores don't
    # collapse the window to nothing) two templates' backtested Sharpe ratios
    # must be before an optional factor_report (see generate()) is allowed to
    # break the tie. A factor score NEVER overrides a clearly-better-performing
    # template -- see _search_allocation's docstring.
    factor_tiebreak_epsilon: float = 0.05


@dataclass
class GeneratedStrategySpec:
    n_symbols: int
    template_name: str
    params: dict
    universe_sharpe: float
    cagr: float
    max_drawdown: float
    calmar_ratio: float
    win_rate: float
    profit_factor: float
    total_turnover: float
    total_rebalances: int
    ers_passed: bool
    ers_percentile: float
    n_trials: int
    trusted: bool
    explanation: str
    target_weights: pd.DataFrame  # sparse: NaN except on actual rebalance dates (see allocation_templates.py)
    # Populated only when a factor_report was supplied to generate(): each
    # considered template's factor_score (mean historical Sharpe of its own
    # factor_tags, per the report), for transparency into whether/how it
    # could have influenced selection -- see _search_allocation.
    factor_context: dict = None
    factor_tiebreak_used: bool = False
    equity_curve: pd.DataFrame = None


def _portfolio_score(universe: dict, template, params: dict, config: GeneratorConfig) -> dict:
    """Backtest `params` using the allocation backtester."""
    try:
        target_weights = template.generate_weights(universe, params)
        if target_weights.empty:
            return {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}

        result = run_allocation_backtest(
            universe, target_weights,
            initial_capital=config.initial_capital,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct
        )
        return result
    except Exception as exc:
        warnings.warn(
            f"_portfolio_score: {template!r} raised {type(exc).__name__}: {exc} -- "
            f"treating this candidate/trial as -inf Sharpe and continuing.",
            category=RuntimeWarning,
        )
        return {"sharpe_ratio": float("-inf"), "total_rebalances": 0, "total_turnover": 0.0}


def _factor_score(template, factor_report: dict):
    """Mean historical Sharpe (from factor_report['factor_performance']) over
    `template`'s own factor_tags. Returns None if no report was supplied or
    none of the template's tags appear in the report (e.g. a tag that no
    strategy in that research_strategy run carried)."""
    if not factor_report:
        return None
    factor_performance = factor_report.get("factor_performance", {})
    sharpes = [
        factor_performance[tag]["mean_sharpe_ratio"]
        for tag in getattr(template, "factor_tags", [])
        if tag in factor_performance and "mean_sharpe_ratio" in factor_performance[tag]
    ]
    return sum(sharpes) / len(sharpes) if sharpes else None


def _apply_factor_tiebreak(best_per_template: dict, factor_report: dict, epsilon: float):
    """Given each template's own best (template, params, res, score) result,
    picks the overall winner: the highest-score template, UNLESS an optional
    factor_report is supplied and at least one OTHER template is within
    `epsilon` of the leading score (see GeneratorConfig.factor_tiebreak_epsilon)
    AND has a computable factor_score that beats the leader's -- in which case
    that template wins instead. Returns (winning_result, factor_context,
    factor_tiebreak_used). Pure function of its inputs -- no backtesting --
    so this is unit-testable without constructing real universes/templates.
    """
    best_result = max(best_per_template.values(), key=lambda r: r["score"])
    best_score = best_result["score"]

    factor_context = None
    if factor_report is not None:
        factor_context = {}
        for r in best_per_template.values():
            factor_context[r["template"].name] = _factor_score(r["template"], factor_report)

    factor_tiebreak_used = False
    if factor_report is not None and np.isfinite(best_score):
        tolerance = max(abs(best_score) * epsilon, epsilon)
        tied = [r for r in best_per_template.values() if abs(best_score - r["score"]) <= tolerance]
        tied_with_factor_score = [r for r in tied if factor_context.get(r["template"].name) is not None]
        if len(tied) > 1 and tied_with_factor_score:
            factor_winner = max(tied_with_factor_score, key=lambda r: factor_context[r["template"].name])
            if factor_winner["template"].name != best_result["template"].name:
                best_result = factor_winner
                factor_tiebreak_used = True

    return best_result, factor_context, factor_tiebreak_used


def _search_allocation(universe: dict, cfg: GeneratorConfig, factor_report: dict = None,
                        extra_templates: list = None) -> dict:
    """Grid search across all allocation templates.

    If `factor_report` is supplied (see run_strategygen.py's --factor-report
    and research_strategy/run_research_strategy.py's factor_summary.json
    output), it is used ONLY to break a tie among templates whose backtested
    Sharpe ratios are already statistically ambiguous (within
    cfg.factor_tiebreak_epsilon of each other) -- it can never override a
    template that clearly outperformed on this universe's own backtest. This
    keeps the primary, ERS-validated Sharpe signal authoritative; the factor
    report only nudges genuinely close calls, and is documented as such in
    the returned factor_context/factor_tiebreak_used fields regardless of
    whether it actually fired.

    `extra_templates`, if supplied, is a list of ALREADY-INSTANTIATED
    AllocationTemplate objects (e.g. PatternBasedAllocationTemplate
    instances built by stratgen/pattern_mining.py from a universe-specific
    mined pattern) folded into the SAME candidate pool as the 9 static,
    zero-arg-constructible classes in ALLOCATION_TEMPLATES -- they compete
    through the identical grid-search + ERS + factor-tiebreak pipeline below,
    with no special-casing. Omitting it (default None/[]) is exactly
    today's behavior.
    """

    all_results = []
    total_grid_trials = 0

    # 1. Grid Search across all templates (static + any pre-instantiated extras)
    templates = [template_cls() for template_cls in ALLOCATION_TEMPLATES] + list(extra_templates or [])

    score_fn = lambda template, params: _portfolio_score(universe, template, params, cfg)

    names_seen = set()
    for template in templates:
        if template.name in names_seen:
            raise ValueError(
                f"Duplicate allocation template name '{template.name}' -- template names must be "
                f"unique within a single generate() call (this can happen if extra_templates collides "
                f"with a static template name, or two mined templates share the same name)."
            )
        names_seen.add(template.name)

        trials = grid_search_template(template, score_fn)
        total_grid_trials += len(trials)

        for t in trials:
            all_results.append({
                "template": template,
                "params": t["params"],
                "res": t["result"],
                "score": t["score"],
            })

    # Find the best (template, params) combo per DISTINCT template -- needed
    # so tie-breaking compares templates against each other, not individual
    # param combos (a single template's own grid can otherwise fill every
    # top slot and hide that a different template is a close second).
    best_per_template = {}
    for r in all_results:
        name = r["template"].name
        if name not in best_per_template or r["score"] > best_per_template[name]["score"]:
            best_per_template[name] = r

    best_result, factor_context, factor_tiebreak_used = _apply_factor_tiebreak(
        best_per_template, factor_report, cfg.factor_tiebreak_epsilon
    )
    best_score = best_result["score"]
    best_res = best_result["res"]

    # 2. Equivalent Random Search (ERS)
    ers = run_ers_validation(
        best_result["params"], best_score, best_res, score_fn,
        n_random_search=cfg.n_random_search, ers_percentile_threshold=cfg.ers_percentile_threshold,
        min_rebalances_for_trust=cfg.min_rebalances_for_trust, seed=cfg.seed,
    )
    ers_percentile, ers_passed, trusted = ers["ers_percentile"], ers["ers_passed"], ers["trusted"]

    return {
        "template": best_result["template"],
        "params": best_result["params"],
        "res": best_res,
        "score": best_score,
        "total_rebalances": best_res.get("total_rebalances", 0),
        "total_turnover": best_res.get("total_turnover", 0.0),
        "ers_passed": ers_passed,
        "ers_percentile": ers_percentile,
        "n_trials": total_grid_trials + cfg.n_random_search,
        "trusted": trusted,
        "factor_context": factor_context,
        "factor_tiebreak_used": factor_tiebreak_used,
    }


class StrategyGenerator:
    def __init__(self, config: GeneratorConfig = None):
        self.config = config or GeneratorConfig()

    def generate(self, universe: dict, factor_report: dict = None, extra_templates: list = None) -> GeneratedStrategySpec:
        """`factor_report` is the optional, parsed contents of a
        research_strategy factor_summary.json (see run_strategygen.py's
        --factor-report flag) -- omit it (default) for today's unchanged
        behavior. `extra_templates` is an optional list of pre-instantiated
        AllocationTemplate objects (e.g. from stratgen/pattern_mining.py) to
        fold into the search alongside the 9 static templates -- omit it
        (default) for today's unchanged behavior. See _search_allocation's
        docstring for exactly how/when either can influence the winner."""
        cfg = self.config
        if not universe:
            raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")

        result = _search_allocation(universe, cfg, factor_report=factor_report, extra_templates=extra_templates)

        template = result["template"]
        params = result["params"]
        res = result["res"]

        # Generate the final weights for output
        target_weights = template.generate_weights(universe, params)
        explanation = template.explain_weights(params)

        return GeneratedStrategySpec(
            n_symbols=len(universe),
            template_name=template.name,
            params=params,
            universe_sharpe=result["score"],
            cagr=res.get("cagr", 0.0),
            max_drawdown=res.get("max_drawdown", 0.0),
            calmar_ratio=res.get("calmar_ratio", 0.0),
            win_rate=res.get("win_rate", 0.0),
            profit_factor=res.get("profit_factor", 0.0),
            total_turnover=result["total_turnover"],
            total_rebalances=result["total_rebalances"],
            ers_passed=result["ers_passed"],
            ers_percentile=result["ers_percentile"],
            n_trials=result["n_trials"],
            trusted=result["trusted"],
            explanation=explanation,
            target_weights=target_weights,
            factor_context=result["factor_context"],
            factor_tiebreak_used=result["factor_tiebreak_used"],
            equity_curve=res.get("equity_curve"),
        )

#!/usr/bin/env python
"""CLI entry point: generate a portfolio allocation strategy for a basket of assets.

Unlike the previous version which focused on single-asset timing signals,
this tool searches for the optimal portfolio allocation method (e.g., Inverse Volatility,
Cross-Sectional Momentum, Equal Weight) across the entire basket simultaneously.

Example:
    python run_strategygen.py --universe SPY QQQ AAPL --mode generate
"""

import argparse
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd

from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common import plotting
from common.reporting import format_weights_pct, write_dense_weights_csv, write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args
from stratgen.generator import GeneratorConfig, StrategyGenerator


def _load_factor_report(path: str) -> dict:
    """Loads and validates a research_strategy factor_summary.json file (see
    research_strategy/run_research_strategy.py's build_and_write_factor_summary).
    Raises a clear error naming what's missing rather than a raw KeyError
    surfacing later inside generator.py."""
    with open(path, "r") as f:
        report = json.load(f)

    if not isinstance(report, dict) or "factor_performance" not in report:
        raise ValueError(
            f"Malformed factor report '{path}': expected a JSON object with a "
            f"'factor_performance' key (see research_strategy's factor_summary.json output)."
        )
    if not isinstance(report["factor_performance"], dict):
        raise ValueError(
            f"Malformed factor report '{path}': 'factor_performance' must be a JSON object, "
            f"got {type(report['factor_performance']).__name__}."
        )

    return report


def _load_pattern_report(path: str) -> dict:
    """Loads and validates a `pattern_mining` stage `pattern_report.json`
    (see `pattern_mining/run_pattern_mining.py`). Raises a clear error
    naming what's missing rather than a raw KeyError surfacing later."""
    with open(path, "r") as f:
        report = json.load(f)

    if not isinstance(report, dict) or "findings" not in report:
        raise ValueError(
            f"Malformed pattern report '{path}': expected a JSON object with a "
            f"'findings' key (see pattern_mining/run_pattern_mining.py's output)."
        )
    if not isinstance(report["findings"], list):
        raise ValueError(
            f"Malformed pattern report '{path}': 'findings' must be a JSON array, "
            f"got {type(report['findings']).__name__}."
        )

    return report

RESULTS_DIR = default_results_dir(__file__)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Basket Asset Allocation Strategy Generator")
    add_universe_cli_args(p, default_universe=["SPY", "QQQ"])
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--mode", choices=["generate"], default="generate", help="Currently only 'generate' is supported for allocation.")
    p.add_argument("--n-random-search", type=int, default=200)
    p.add_argument("--ers-percentile-threshold", type=float, default=0.90)
    p.add_argument("--min-rebalances-for-trust", type=int, default=4)
    p.add_argument("--factor-report", type=str, default=None,
                   help="Path to a research_strategy factor_summary.json (see "
                        "research_strategy/run_research_strategy.py) -- when given, its per-factor "
                        "performance is used ONLY to break ties among templates whose backtested "
                        "Sharpe ratios are already statistically ambiguous (see --factor-tiebreak-epsilon); "
                        "it can never override a clearly-better-performing template. Omit for today's "
                        "unchanged, factor-report-free selection.")
    p.add_argument("--factor-tiebreak-epsilon", type=float, default=0.05,
                   help="How close (as a fraction of the leading Sharpe, also used as an absolute floor) "
                        "two templates' scores must be before --factor-report is allowed to break the tie "
                        "(default: 0.05 = 5%%). Ignored if --factor-report is not given.")
    p.add_argument("--pattern-report", type=str, default=None,
                   help="Path to a pattern_mining stage pattern_report.json (see "
                        "pattern_mining/run_pattern_mining.py) -- when given, its significant turning-point "
                        "indicator findings are turned into candidate PatternBasedAllocationTemplate "
                        "instances (up to --pattern-max-templates), which compete through the same "
                        "grid-search + Equivalent Random Search validation as every static template. Omit "
                        "to search only the 9 static templates (plus any --research-strategy templates).")
    p.add_argument("--pattern-max-templates", type=int, default=5,
                   help="Cap on how many significant mined patterns (from --pattern-report) become "
                        "candidate templates (default 5).")
    p.add_argument("--research-strategy", nargs="+", default=None, metavar="STRATEGY_KEY",
                   help="Include one or more research_strategy strategies (by their strategies_config.json key, "
                        "e.g. 'baa_keller', 'adaptive_grid' -- see research_strategy/README.md for the full list) "
                        "as additional candidate templates, alongside the 9 static allocation templates and any "
                        "--pattern-report findings. Each is instantiated exactly as research_strategy's own CLI "
                        "would build it (including any strategies_config.json parameter overrides).")
    p.add_argument("--no-plots", action="store_true",
                   help="Skip the equity-curve chart normally produced for the winning strategy (charts are ON by default).")
    p.add_argument("--no-compose-aspects", action="store_true",
                   help="Disable aspect composition (ON by default): pairing one decomposable template's own "
                        "selection/entry aspect with a DIFFERENT decomposable template's own weighting/exit "
                        "aspect (e.g. momentum's stock-picking with inverse-volatility's position sizing) to "
                        "search hybrid strategies that aren't any single static or --research-strategy template. "
                        "See common/strategy_aspects.py and research_strategy/rs/timing_aspects.py. Pass this "
                        "flag to restrict the search to only the templates explicitly named/loaded.")
    add_data_provider_cli_args(p)
    return p


def main():
    args = build_arg_parser().parse_args()
    gen_config = GeneratorConfig(
        n_random_search=args.n_random_search,
        ers_percentile_threshold=args.ers_percentile_threshold,
        min_rebalances_for_trust=args.min_rebalances_for_trust,
        factor_tiebreak_epsilon=args.factor_tiebreak_epsilon,
        enable_aspect_composition=not args.no_compose_aspects,
    )

    factor_report = None
    if args.factor_report:
        factor_report = _load_factor_report(args.factor_report)
        print(f"Loaded factor report from {args.factor_report} "
              f"({len(factor_report['factor_performance'])} factor(s) with performance data)")

    universe_symbols = resolve_universe_from_args(args, default_symbols=["SPY", "QQQ"])
    if not universe_symbols:
        raise ValueError("Resolved universe symbol list is empty.")

    data_kwargs = build_data_kwargs(args)

    universe = load_universe_with_banner(
        universe_symbols, args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=shared_data_dir(),
        data_kwargs=data_kwargs, require_nonempty=True,
        cache_max_age_days=args.cache_ttl_days,
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)

    extra_templates = None
    # Kept separate from extra_templates itself (which grows below to also
    # include any --research-strategy templates): the pattern_spec
    # reconstruction block further down assumes every candidate it inspects
    # is a PatternBasedAllocationTemplate (it reads feature_name/threshold/
    # etc.), so it must only ever search the mined templates, never the
    # combined pool.
    mined_templates = None
    if args.pattern_report:
        from pattern_mining.pmine.pattern_mining import build_pattern_templates

        pattern_report = _load_pattern_report(args.pattern_report)
        findings = pd.DataFrame(pattern_report["findings"])
        mined_templates = build_pattern_templates(findings, max_templates=args.pattern_max_templates)
        extra_templates = mined_templates
        n_significant = int(findings["significant"].sum()) if not findings.empty else 0
        # 0 significant patterns is an expected, valid outcome (especially
        # on synthetic data) -- not an error. See pmine/pattern_mining.py.
        print(f"Loaded pattern report from {args.pattern_report} (status={pattern_report.get('status')}, "
              f"{n_significant} significant of {len(findings)} finding(s)); "
              f"{len(extra_templates)} added as candidate template(s).")

    # research_strategy_entries/templates are kept as separate dicts (keyed by
    # strategies_config.json key) rather than folded anonymously into
    # extra_templates, so the strategy.json reconstruction block below can
    # look a winning template back up by its ORIGINAL config key + entry_data
    # -- mirroring, but not reusing, the --pattern-report/pattern_spec pattern
    # above (a mined template reconstructs from its own fields; a
    # research_strategy template reconstructs via research_strategy's own
    # instantiate_strategy_from_config_entry, given back its key + entry).
    research_strategy_entries = {}  # key -> entry_data dict (for strategy.json reconstruction)
    research_strategy_templates = {}  # key -> instantiated template
    if args.research_strategy:
        from research_strategy.rs.config import load_strategies_config
        from research_strategy.rs.strategy import instantiate_strategy_from_config_entry

        loaded_config = load_strategies_config()
        unknown = set(args.research_strategy) - set(loaded_config.keys())
        if unknown:
            raise ValueError(
                f"Unknown --research-strategy key(s) {sorted(unknown)}; valid keys: {sorted(loaded_config.keys())}"
            )
        for key in args.research_strategy:
            entry_data = loaded_config[key]
            research_strategy_entries[key] = entry_data
            research_strategy_templates[key] = instantiate_strategy_from_config_entry(key, entry_data)

        extra_templates = (extra_templates or []) + list(research_strategy_templates.values())
        print(f"Added {len(research_strategy_templates)} research_strategy candidate template(s): "
              f"{sorted(research_strategy_templates.keys())}")

    if args.mode == "generate":
        spec = StrategyGenerator(gen_config).generate(universe, factor_report=factor_report, extra_templates=extra_templates)
        print(f"\n=== Generated Allocation Strategy for basket of {spec.n_symbols} assets ===")
        print(f"  Template: {spec.template_name}")
        print(f"  Optimal Parameters: {spec.params}")
        print(f"  Portfolio Sharpe Ratio: {spec.universe_sharpe:.2f} | CAGR: {spec.cagr * 100:.2f}% | Max Drawdown: {spec.max_drawdown * 100:.2f}%")
        print(f"  Calmar Ratio: {spec.calmar_ratio:.2f} | Win Rate: {spec.win_rate * 100:.1f}% | Profit Factor: {spec.profit_factor:.2f}")
        print(f"  Total Rebalances: {spec.total_rebalances} | Total Turnover: {spec.total_turnover:.2f}")
        print(f"  ERS Percentile: {spec.ers_percentile:.2f} | Trusted: {spec.trusted}")
        if factor_report is not None:
            print(f"  Factor Context (mean historical Sharpe by candidate template's factor tags): {spec.factor_context}")
            print(f"  Factor Tie-Break Used: {spec.factor_tiebreak_used}")
        if spec.is_composite:
            print(f"  Composite Strategy (track={spec.composite_track}): assembled from aspects "
                  f"{spec.component_templates} -- not one of this workspace's original templates.")

        print("\n=== Strategy Logic & Execution Schedule ===")
        print(f"  {spec.explanation}")

        print("\n=== Recent Target Weights (Last 5 Rebalance Dates) ===")
        # spec.target_weights is sparse -- NaN except on an actual rebalance
        # date, so those dates are just the non-NaN rows (no diffing needed,
        # and no risk of missing a rebalance that recomputed the same weight).
        weights = spec.target_weights

        # Convert to percentages for readability
        recent_pct = format_weights_pct(weights, 5)
        print(recent_pct)

        out_path = os.path.join(RESULTS_DIR, "strategygen_allocation_weights.csv")
        write_dense_weights_csv(weights, out_path)
        print(f"\nSaved full daily target weights to {out_path}")

        # If a mined PatternBasedAllocationTemplate won, it's NOT in the
        # static ALLOCATION_TEMPLATES registry (it's universe-specific, not
        # zero-arg constructible) -- embed its reconstruction fields
        # directly so backtester/run_backtest.py can rebuild the exact same
        # instance from strategy.json alone (see its own _get_template).
        pattern_spec = None
        for t in (mined_templates or []):
            if t.name == spec.template_name:
                pattern_spec = {
                    "feature_name": t.feature_name,
                    "feature_lookback": t.feature_lookback,
                    "threshold": t.threshold,
                    "comparison": t.comparison,
                    "event_type": t.event_type,
                    "mined_p_value": t.mined_p_value,
                    "mined_n_events": t.mined_n_events,
                }
                break

        # Same idea as pattern_spec above, but for a winning research_strategy
        # template: it's also not in the static ALLOCATION_TEMPLATES registry,
        # so embed enough to rebuild the exact same instance via
        # research_strategy.rs.strategy.instantiate_strategy_from_config_entry
        # (strategy_key + entry_data is everything that function needs).
        # pattern_spec and research_strategy_spec can never both be non-null:
        # each loop only matches against its own disjoint template list, and a
        # winning template can only ever have come from one of them.
        research_strategy_spec = None
        for key, t in research_strategy_templates.items():
            if t.name == spec.template_name:
                research_strategy_spec = {
                    "strategy_key": key,
                    "entry_data": research_strategy_entries[key],
                }
                break

        # Populated only when a hybrid assembled by aspect composition won
        # (see GeneratorConfig.enable_aspect_composition) -- mutually
        # exclusive with pattern_spec/research_strategy_spec above, same
        # reconstruction convention: enough for backtester/run_backtest.py's
        # _get_template to rebuild the exact same CompositeAllocationTemplate/
        # CompositeTimingTemplate instance from common.strategy_aspects/
        # research_strategy.rs.timing_aspects's registries.
        composite_spec = None
        if spec.is_composite:
            key_names = (
                ("selection_key", "weighting_key") if spec.composite_track == "allocation"
                else ("entry_key", "exit_key")
            )
            composite_spec = {
                "track": spec.composite_track,
                key_names[0]: spec.component_templates[0],
                key_names[1]: spec.component_templates[1],
            }

        strategy_json_path = os.path.join(RESULTS_DIR, "strategy.json")
        write_json_report({
            "template_name": spec.template_name,
            "params": spec.params,
            "explanation": spec.explanation,
            "sharpe_ratio": spec.universe_sharpe,
            "cagr": spec.cagr,
            "max_drawdown": spec.max_drawdown,
            "calmar_ratio": spec.calmar_ratio,
            "win_rate": spec.win_rate,
            "profit_factor": spec.profit_factor,
            "trusted": spec.trusted,
            "ers_passed": spec.ers_passed,
            "ers_percentile": spec.ers_percentile,
            "factor_context": spec.factor_context,
            "factor_tiebreak_used": spec.factor_tiebreak_used,
            "pattern_spec": pattern_spec,
            "research_strategy_spec": research_strategy_spec,
            "composite_spec": composite_spec,
        }, strategy_json_path)
        print(f"Saved strategy definition to {strategy_json_path}")

        if not args.no_plots and spec.equity_curve is not None and not spec.equity_curve.empty:
            chart_path = plotting.plot_equity_curve(
                spec.equity_curve["equity"], RESULTS_DIR, strategy_label=spec.template_name,
                title=f"{spec.template_name} Equity Curve (in-sample search result)",
            )
            print(f"Saved equity curve chart to {chart_path}")
    else:
        print("Walkforward mode is currently disabled for the new allocation architecture.")


if __name__ == "__main__":
    main()

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

import numpy as np
import pandas as pd

from common.universe import add_universe_cli_args, resolve_universe_from_args
from stratgen.data import load_ohlcv
from stratgen.generator import GeneratorConfig, StrategyGenerator
from stratgen.pattern_mining import build_pattern_templates, mine_indicator_patterns


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

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


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
    p.add_argument("--mine-patterns", action="store_true",
                   help="Detect this universe's aggregate portfolio turning points (peaks/troughs), mine "
                        "a menu of popular technical indicators for a statistically significant pattern "
                        "preceding them (Bonferroni-corrected shuffle-null test, see "
                        "stratgen/pattern_mining.py), and fold any significant finding into the search as "
                        "an additional candidate template -- it still must clear the same Equivalent "
                        "Random Search bar as every static template. Finding 0 significant patterns is a "
                        "common, valid outcome (especially on synthetic data), not an error.")
    p.add_argument("--pattern-min-swing-pct", type=float, default=0.05,
                   help="Minimum zigzag swing size (fraction) to confirm a turning point (default 0.05 = 5%%).")
    p.add_argument("--pattern-lag-bars", type=int, default=20,
                   help="How many trading days BEFORE each turning point to read indicators at, to avoid "
                        "the near-tautological result of reading them AT the turning point itself (default 20; "
                        "see pattern_mining.py's module docstring for why this matters).")
    p.add_argument("--pattern-max-templates", type=int, default=5,
                   help="Cap on how many significant mined patterns become candidate templates (default 5).")
    p.add_argument("--data-provider", default="yfinance",
                   help="Market data source provider ('yfinance', 'csv', 'synthetic', or custom module specifier string e.g. 'script.py:CustomProvider')")
    p.add_argument("--data-dir", type=str, default=None,
                   help="Folder path for CSV data provider")
    p.add_argument("--no-cache", action="store_true")
    return p


def main():
    args = build_arg_parser().parse_args()
    gen_config = GeneratorConfig(
        n_random_search=args.n_random_search,
        ers_percentile_threshold=args.ers_percentile_threshold,
        min_rebalances_for_trust=args.min_rebalances_for_trust,
        factor_tiebreak_epsilon=args.factor_tiebreak_epsilon,
    )

    factor_report = None
    if args.factor_report:
        factor_report = _load_factor_report(args.factor_report)
        print(f"Loaded factor report from {args.factor_report} "
              f"({len(factor_report['factor_performance'])} factor(s) with performance data)")

    universe_symbols = resolve_universe_from_args(args, default_symbols=["SPY", "QQQ"])
    if not universe_symbols:
        raise ValueError("Resolved universe symbol list is empty.")

    data_kwargs = {"provider": args.data_provider}
    if args.data_dir:
        data_kwargs["folder_path"] = args.data_dir

    universe = {}
    for symbol in universe_symbols:
        print(f"Loading {symbol} ...")
        universe[symbol] = load_ohlcv(symbol, args.start, args.end, args.interval, use_cache=not args.no_cache, **data_kwargs)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    extra_templates = None
    if args.mine_patterns:
        findings, mining_status = mine_indicator_patterns(
            universe,
            min_swing_pct=args.pattern_min_swing_pct,
            lag_bars=args.pattern_lag_bars,
        )
        if mining_status != "ok":
            print(f"Pattern mining: {mining_status} -- skipping, proceeding with the 9 standard templates only.")
        else:
            extra_templates = build_pattern_templates(findings, max_templates=args.pattern_max_templates)
            n_significant = int(findings["significant"].sum()) if not findings.empty else 0
            # 0 significant patterns is an expected, valid outcome (especially
            # on synthetic data) -- not an error. See pattern_mining.py.
            print(f"Pattern mining found {n_significant} statistically significant indicator pattern(s) "
                  f"out of {len(findings)} tested (Bonferroni-corrected, lag={args.pattern_lag_bars} bars); "
                  f"{len(extra_templates)} added as candidate template(s).")

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

        print("\n=== Strategy Logic & Execution Schedule ===")
        print(f"  {spec.explanation}")

        print("\n=== Recent Target Weights (Last 5 Rebalance Dates) ===")
        # spec.target_weights is sparse -- NaN except on an actual rebalance
        # date, so those dates are just the non-NaN rows (no diffing needed,
        # and no risk of missing a rebalance that recomputed the same weight).
        weights = spec.target_weights
        recent_rebalances = weights.dropna(how="all").tail(5)

        # Convert to percentages for readability
        recent_pct = (recent_rebalances * 100).round(1).astype(str) + "%"
        print(recent_pct)

        out_path = os.path.join(RESULTS_DIR, "strategygen_allocation_weights.csv")
        weights.ffill().fillna(0.0).to_csv(out_path)
        print(f"\nSaved full daily target weights to {out_path}")

        # If a mined PatternBasedAllocationTemplate won, it's NOT in the
        # static ALLOCATION_TEMPLATES registry (it's universe-specific, not
        # zero-arg constructible) -- embed its reconstruction fields
        # directly so backtester/run_backtest.py can rebuild the exact same
        # instance from strategy.json alone (see its own _get_template).
        pattern_spec = None
        for t in (extra_templates or []):
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

        strategy_json_path = os.path.join(RESULTS_DIR, "strategy.json")
        with open(strategy_json_path, "w") as f:
            json.dump({
                "template_name": spec.template_name,
                "params": spec.params,
                "explanation": spec.explanation,
                "sharpe_ratio": spec.universe_sharpe,
                "cagr": spec.cagr,
                "max_drawdown": spec.max_drawdown,
                "calmar_ratio": spec.calmar_ratio,
                "win_rate": spec.win_rate,
                "profit_factor": spec.profit_factor if np.isfinite(spec.profit_factor) else None,
                "trusted": spec.trusted,
                "ers_passed": spec.ers_passed,
                "ers_percentile": spec.ers_percentile,
                "factor_context": spec.factor_context,
                "factor_tiebreak_used": spec.factor_tiebreak_used,
                "pattern_spec": pattern_spec,
            }, f, indent=2)
        print(f"Saved strategy definition to {strategy_json_path}")
    else:
        print("Walkforward mode is currently disabled for the new allocation architecture.")


if __name__ == "__main__":
    main()

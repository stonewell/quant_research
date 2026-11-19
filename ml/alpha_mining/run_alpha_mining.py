#!/usr/bin/env python
"""CLI entry point for the ML1 two-stage GP + LightGBM Alpha Mining pipeline.

Runs the 3-segment temporally isolated workflow:
1. ``[GP discovery]``: typed Genetic Programming factor mining on discovery dates.
2. ``gap(h + embargo)`` -> ``[Combiner]``: native LightGBM under Purged Walk-Forward CV.
3. ``gap(h + embargo)`` -> ``[Holdout]``: 5-arm comparison through
   ``common.allocation_backtester.run_allocation_backtest`` plus report-only
   monitoring diagnostics and Rule-9 empirical gate evaluation.

**Not wired into ``run_pipeline.py``.** Requires this project's own isolated
``uv`` environment (``ml/alpha_mining/pyproject.toml`` / ``.venv``) so
``lightgbm`` never pollutes ``pipeline/.venv``.

Example (100% offline default):
    uv run python run_alpha_mining.py --data-provider synthetic --small-data
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure the repo root is in sys.path to allow importing from common
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.cli_utils import (
    add_data_provider_cli_args,
    bootstrap_project_paths,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.reporting import utc_timestamp, write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args

bootstrap_project_paths(_REPO_ROOT, __file__)

from amine.benchmark import run_alpha_mining_benchmark
from amine.config import (
    AlphaMiningConfig,
    CombinerConfig,
    GPConfig,
    PurgedCVConfig,
    VALID_LABEL_MODES,
    VALID_OBJECTIVES,
)

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    "JPM", "BAC", "XOM", "CVX", "JNJ", "PG",
]

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ML1 Two-Stage Genetic Programming + LightGBM Alpha Mining Pipeline"
    )
    add_universe_cli_args(p, default_universe=DEFAULT_UNIVERSE)
    add_data_provider_cli_args(
        p,
        default_provider="synthetic",
        no_cache_help="Disable local DuckDB caching of OHLCV history",
    )
    p.add_argument("--start", default="2018-01-01", help="History start date (YYYY-MM-DD)")
    p.add_argument("--end", default="2024-12-31", help="History end date (YYYY-MM-DD)")
    p.add_argument("--interval", default="1d", help="Bar interval (default: 1d)")
    p.add_argument("--k", "--holding-period", dest="holding_period", type=int, default=5,
                   help="Forward label holding period k in trading bars (default: 5)")
    p.add_argument("--exec-lag", type=int, default=1,
                   help="Execution delay in trading bars (default: 1, next-close entry)")
    p.add_argument("--embargo-days", type=int, default=5,
                   help="Post-test embargo in trading bars (default: 5)")
    p.add_argument("--label-mode", choices=list(VALID_LABEL_MODES), default="raw_return",
                   help="Label construction mode (default: raw_return)")
    p.add_argument("--sector-neutral", action="store_true",
                   help="Cross-sectionally demean labels within sectors via cs_indneutralize")
    p.add_argument("--objective", choices=list(VALID_OBJECTIVES), default="lambdarank",
                   help="LightGBM combiner objective (default: lambdarank)")
    p.add_argument("--gp-population", type=int, default=None,
                   help="GP population size (default: 200, or 30 with --small-data)")
    p.add_argument("--gp-generations", type=int, default=None,
                   help="GP number of generations (default: 20, or 3 with --small-data)")
    p.add_argument("--top-k-factors", type=int, default=None,
                   help="Max non-redundant GP factors selected (default: 10, or 5 with --small-data)")
    p.add_argument("--n-splits", type=int, default=None,
                   help="Number of Purged WF CV splits (default: 5, or 3 with --small-data)")
    p.add_argument("--top-n", type=int, default=5,
                   help="Top-N assets held equal-weight in backtest book (default: 5)")
    p.add_argument("--lowdin-combiner", action="store_true",
                   help="Enable signed Löwdin orthogonalization before LightGBM combiner")
    p.add_argument("--small-data", action="store_true",
                   help="Use compact GP/LightGBM/CV profile for fast synthetic runs")
    p.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42)")
    p.add_argument("--output-dir", default=RESULTS_DIR,
                   help=f"Output directory for alpha_mining_report.json and oof_scores.csv (default: {RESULTS_DIR})")
    return p


def build_config_from_args(args: argparse.Namespace) -> AlphaMiningConfig:
    if args.small_data:
        base = AlphaMiningConfig.small_data(seed=args.seed)
    else:
        base = AlphaMiningConfig(seed=args.seed)

    gp_cfg = GPConfig(
        population_size=args.gp_population if args.gp_population is not None else base.gp.population_size,
        n_generations=args.gp_generations if args.gp_generations is not None else base.gp.n_generations,
        tournament_size=min(
            base.gp.tournament_size,
            args.gp_population if args.gp_population is not None else base.gp.population_size,
        ),
        top_k=args.top_k_factors if args.top_k_factors is not None else base.gp.top_k,
        seed=args.seed,
    )
    cv_cfg = PurgedCVConfig(
        n_splits=args.n_splits if args.n_splits is not None else base.cv.n_splits,
        embargo_days=args.embargo_days,
        train_side=base.cv.train_side,
        min_train_dates=base.cv.min_train_dates,
        min_test_dates=base.cv.min_test_dates,
    )
    comb_cfg = CombinerConfig(
        objective=args.objective,
        max_depth=base.combiner.max_depth,
        num_leaves=base.combiner.num_leaves,
        learning_rate=base.combiner.learning_rate,
        num_boost_round=base.combiner.num_boost_round,
        min_child_samples=base.combiner.min_child_samples,
        seed=args.seed,
    )
    return AlphaMiningConfig(
        holding_period=args.holding_period,
        exec_lag=args.exec_lag,
        label_mode=args.label_mode,
        sector_neutral=bool(args.sector_neutral),
        residual_min_obs=base.residual_min_obs,
        lowdin_for_gp_library=base.lowdin_for_gp_library,
        lowdin_for_combiner=bool(args.lowdin_combiner),
        top_n=args.top_n,
        seed=args.seed,
        gp=gp_cfg,
        cv=cv_cfg,
        combiner=comb_cfg,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = build_config_from_args(args)

    universe_symbols = resolve_universe_from_args(args, default_symbols=DEFAULT_UNIVERSE)
    data_kwargs = build_data_kwargs(args)
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed

    price_universe = load_universe_with_banner(
        universe_symbols,
        args.start,
        args.end,
        args.interval,
        use_cache=not args.no_cache,
        cache_dir=DATA_DIR,
        data_kwargs=data_kwargs,
        require_nonempty=True,
        loading_msg=(
            f"Loading {len(universe_symbols)} symbols via provider '{args.data_provider}' "
            f"({args.start} to {args.end}) ..."
        ),
    )

    report, oof_scores = run_alpha_mining_benchmark(price_universe, cfg)
    report["run_context"] = {
        "data_provider": args.data_provider,
        "start": args.start,
        "end": args.end,
        "interval": args.interval,
        "n_symbols": len(price_universe),
        "symbols": list(price_universe.keys()),
        "generated_at": utc_timestamp(),
    }

    os.makedirs(args.output_dir, exist_ok=True)
    report_path = os.path.join(args.output_dir, "alpha_mining_report.json")
    oof_path = os.path.join(args.output_dir, "oof_scores.csv")
    write_json_report(report, report_path)
    oof_scores.to_csv(oof_path)

    oof_ic = report["stage2_combiner_oof"]["ic_summary"]
    print(f"\nSaved alpha mining report to {report_path}")
    print(f"Saved OOF score panel to     {oof_path}")
    print(
        f"\nStage 1 GP: selected {report['stage1_gp']['n_selected']} factors "
        f"(fallback_to_seeds={report['stage1_gp']['fallback_to_seeds']})"
    )
    print(
        f"Stage 2 OOF: mean_ic={oof_ic['mean_ic']:.4f}, t_stat={oof_ic['t_stat']:.2f}, "
        f"ic_ir={oof_ic['ic_ir']:.2f} (n_obs={oof_ic['n_obs']})"
    )
    print("\n=== Holdout 5-Arm Comparison ===")
    for arm_key, arm in report["holdout_arms"].items():
        ic_s = arm["ic_summary"]
        print(
            f"  {arm_key:24s} | IC={ic_s['mean_ic']:+.4f} (t={ic_s['t_stat']:+.2f}) "
            f"| Sharpe={arm['sharpe_ratio']:+.2f} | CAGR={arm['cagr']*100:+.1f}% "
            f"| Rebal={arm['total_rebalances']} Trades={arm['total_trades']}"
        )

    mon = report["monitoring"]
    print(
        f"\nMonitoring (report-only): adv_auc={mon['adversarial_validation']['auc']} "
        f"(alarm={mon['adversarial_validation']['alarm']}), "
        f"imp_decay_alarm={mon['importance_decay']['alarm']}, "
        f"turnover_alarm={mon['turnover_breaker']['alarm']} "
        f"(action={mon['turnover_breaker']['recommended_action']})"
    )
    print(f"Rule-9 Gate: {'PASS' if report['rule9_gate']['passed'] else 'FAIL'} {report['rule9_gate']['checks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

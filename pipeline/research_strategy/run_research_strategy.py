#!/usr/bin/env python
"""CLI Entry Point: Researched Quantitative Trading Strategies Evaluation.

Evaluates strategies written in plain English or predefined canonical research strategies:
1. Active Dual Momentum GTAA + Risk Parity (Antonacci 2014, Faber 2007)
2. Wouter Keller's Bold Asset Allocation BAA-G12 (Keller 2022)
3. Moreira & Muir Volatility-Managed Portfolios (Moreira & Muir 2017)
4. Accelerating Dual Momentum (Ludlow & Hanly 2018)
5. Vigilant Asset Allocation VAA-G4 (Keller & Keuning 2017)
6. RSI(2) Mean-Reversion (ported from the former rsi_strategy project)
7. Trend-Pullback Swing (ported from the former swing_trend_strategy project)
8. ATR-Adaptive Grid (ported from the former grid_trading project)
9. Regime-Switching Ensemble (ported from the former ensemble_strategy project)
10. Custom Plain English Strategy descriptions (--description or --description-file)
11. Compounder Margin of Safety (price-only proxy of docs/snowball_strategy.txt's conservative
    valuation framework; see rs/strategy.py's module docstring for the full strategy list)

STRICT TEST POLICY: Runs on synthetic multi-asset price histories (no real market data calls).

Examples:
    python run_research_strategy.py --strategy all
    python run_research_strategy.py --config custom_config.json --strategy all
    python run_research_strategy.py --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility."
    python run_research_strategy.py --description-file strategy.txt
    python run_research_strategy.py --dump-strategies
"""

import argparse
import os
import statistics
import sys

# Ensure the repo root is in sys.path to allow importing from common
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.cli_utils import (
    add_data_provider_cli_args,
    bootstrap_project_paths,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.factor_taxonomy import FACTOR_CATEGORIES
from common.reporting import format_weights_pct, write_dense_weights_csv, write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args

# Also add this project's own directory (for bare `from rs...` imports) and
# pipeline/ (for research_strategy as a sibling group member).
bootstrap_project_paths(_REPO_ROOT, __file__)
from rs.config import StrategyConfig, load_strategies_config
from rs.nl_parser import parse_plain_english_strategy
from rs.strategy import (
    NaturalLanguageStrategy,
    STRATEGY_CLASS_MAP,
    instantiate_strategy_from_config_entry,
)

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


DEFAULT_UNIVERSE_SYMBOLS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ",
    "AGG", "TIP", "IEF", "LQD", "DBC", "BIL", "SCZ",
    # Added for the modern popular-strategy pass: HYG (high-yield credit, no
    # substitute among the symbols above) for ProtectiveAssetAllocation;
    # UPRO/TMF (leveraged ETFs, leverage IS the strategy -- no substitute
    # possible) for HFEAStrategy.
    "HYG", "UPRO", "TMF",
]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Researched Quantitative Trading Strategies CLI Runner")
    add_universe_cli_args(p, default_universe=DEFAULT_UNIVERSE_SYMBOLS)
    p.add_argument("--strategy",
                   default="all",
                   help="Strategy key from JSON config to evaluate, or 'all' (default: 'all')")
    p.add_argument("--config", type=str, default=None, help="Path to custom strategies JSON config file")
    p.add_argument("--dump-strategies", action="store_true",
                   help="Dump every strategy in the loaded config as its own backtester-compatible "
                        "<key>_strategy.json file under results/strategy_dumps/ (usable directly "
                        "with backtester/run_backtest.py --strategy-file), then exit without "
                        "loading any universe/market data or running a backtest evaluation")
    p.add_argument("--vaa-offensive-universe", nargs="+", default=None, metavar="TICKER",
                   help="vigilant_asset_allocation only: explicit offensive-pool tickers. "
                        "strategies_config.json no longer hardcodes this -- both "
                        "--vaa-offensive-universe and --vaa-defensive-universe are required to "
                        "run this strategy (their tickers are automatically included in the "
                        "loaded universe even if not also passed to --universe)")
    p.add_argument("--vaa-defensive-universe", nargs="+", default=None, metavar="TICKER",
                   help="vigilant_asset_allocation only: explicit defensive-pool tickers (see "
                        "--vaa-offensive-universe)")
    p.add_argument("--description", type=str, help="Plain English strategy description text")
    p.add_argument("--description-file", type=str, help="Path to plain English strategy description text file")
    p.add_argument("--n-days", type=int, default=1200,
                   help="Number of business days of history to request (all providers)")
    p.add_argument("--seed", type=int, default=42, help="Random seed (only used with --data-provider synthetic)")
    p.add_argument("--top-n", type=int, default=5,
                   help="Number of top-ranked strategies (by Sharpe ratio, CAGR tie-break) to include "
                        "in top_strategies_summary.json (default: 5)")
    add_data_provider_cli_args(p, default_provider="synthetic", no_cache_help="Disable local CSV caching of fetched data")
    return p


def build_and_write_factor_summary(report_data, strategy_factor_tags, args, start, end, results_dir):
    """Aggregates per-strategy backtest results by factor tag and writes
    results/factor_summary.json -- the real hand-off artifact
    `strategy_generator`'s optional `--factor-report` flag consumes (see
    `strategy_generator/stratgen/generator.py`) to contextualize/tie-break
    its own template selection. Deliberately conservative: this never claims
    more than what a single run on `--data-provider synthetic` (the
    default) can actually support -- see the `caveat` field below.
    """
    metrics = ("sharpe_ratio", "cagr", "max_drawdown", "calmar_ratio")

    per_tag_values = {tag: {m: [] for m in metrics} for tag in FACTOR_CATEGORIES}
    for strat_name, tags in strategy_factor_tags.items():
        if strat_name not in report_data:
            continue
        for tag in tags:
            if tag not in per_tag_values:
                continue  # unrecognized tag already warned about at config-load time
            for m in metrics:
                value = report_data[strat_name].get(m)
                if value is not None:
                    per_tag_values[tag][m].append(value)

    factor_performance = {}
    for tag, values_by_metric in per_tag_values.items():
        n = len(values_by_metric["sharpe_ratio"])
        if n == 0:
            continue
        entry = {"n_strategies": n}
        for m, values in values_by_metric.items():
            if values:
                entry[f"mean_{m}"] = statistics.mean(values)
                entry[f"median_{m}"] = statistics.median(values)
        factor_performance[tag] = entry

    is_synthetic = args.data_provider == "synthetic"
    caveat = (
        f"Computed on provider='{args.data_provider}'"
        f"{f', seed={args.seed}' if is_synthetic else ''}, n_days={args.n_days}, {start} to {end}. "
    )
    if is_synthetic:
        caveat += (
            "Synthetic GBM data has NO real momentum/mean-reversion/volatility-clustering structure "
            "by construction, so this summary reflects MECHANISM/plumbing on this specific run, not a "
            "validated factor edge -- re-run with --data-provider yfinance against real prices for a "
            "meaningful factor comparison."
        )
    else:
        caveat += (
            "Computed from a single backtest window; treat as one data point, not a statistically "
            "robust factor study -- re-run across multiple periods/universes before treating any "
            "factor's ranking here as durable."
        )

    summary = {
        "run_context": {
            "data_provider": args.data_provider,
            "seed": args.seed if is_synthetic else None,
            "n_days": args.n_days,
            "start": start,
            "end": end,
        },
        "factor_performance": factor_performance,
        "strategy_factor_tags": strategy_factor_tags,
        "caveat": caveat,
    }

    path = os.path.join(results_dir, "factor_summary.json")
    write_json_report(summary, path)
    return path


def build_and_write_top_strategies_summary(report_data, strategy_factor_tags, loaded_config, args,
                                            start, end, results_dir, top_n=5):
    """Ranks this run's strategies by backtested Sharpe ratio (CAGR as a tie-break) and writes
    results/top_strategies_summary.json -- a human-scannable leaderboard, distinct from
    `build_and_write_factor_summary`'s per-FACTOR-TAG aggregation (this ranks individual
    strategies against each other, not tags). Same synthetic-data-caveat convention as that
    function, tailored to per-strategy ranking noise instead of factor aggregation. Returns
    `(path, top_strategies)` -- the list is returned alongside the path so callers can print a
    console leaderboard without re-reading the JSON file just written."""
    ranked = sorted(
        report_data.items(),
        key=lambda kv: (kv[1]["sharpe_ratio"], kv[1]["cagr"]),
        reverse=True,
    )[:top_n]

    top_strategies = []
    for rank, (strat_name, metrics) in enumerate(ranked, start=1):
        # Prefer strategies_config.json's own human-readable name/description
        # (what a user would recognize this strategy by) -- report_data's own
        # strategy_name/raw_description is the fallback for an ad-hoc
        # --description/--description-file run with no config entry at all.
        config_entry = loaded_config.get(strat_name, {})
        display_name = config_entry.get("name", metrics["strategy_name"])
        description = (
            config_entry.get("description")
            or config_entry.get("plain_english_description")
            or metrics["raw_description"]
        )
        top_strategies.append({
            "rank": rank,
            "strategy_key": strat_name,
            "strategy_name": display_name,
            "description": description,
            "factor_tags": strategy_factor_tags.get(strat_name, []),
            "sharpe_ratio": metrics["sharpe_ratio"],
            "cagr": metrics["cagr"],
            "max_drawdown": metrics["max_drawdown"],
            "calmar_ratio": metrics["calmar_ratio"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "total_turnover": metrics["total_turnover"],
            "total_rebalances": metrics["total_rebalances"],
        })

    is_synthetic = args.data_provider == "synthetic"
    caveat = (
        f"Computed on provider='{args.data_provider}'"
        f"{f', seed={args.seed}' if is_synthetic else ''}, n_days={args.n_days}, {start} to {end}. "
        "Ranking reflects a SINGLE backtest window with no Equivalent Random Search or significance "
        "testing (unlike strategy_generator) -- treat rank order as descriptive of this one run, not "
        "a validated quality signal."
    )
    if is_synthetic:
        caveat += (
            " Synthetic GBM data has NO real momentum/mean-reversion/volatility-clustering structure "
            "by construction, so this ranking reflects MECHANISM/plumbing on this specific run, not "
            "genuine strategy skill -- re-run with --data-provider yfinance against real prices for a "
            "meaningful leaderboard."
        )

    summary = {
        "run_context": {
            "data_provider": args.data_provider,
            "seed": args.seed if is_synthetic else None,
            "n_days": args.n_days,
            "start": start,
            "end": end,
        },
        "ranking_metric": "sharpe_ratio (cagr tie-break)",
        "n_strategies_evaluated": len(report_data),
        "top_strategies": top_strategies,
        "caveat": caveat,
    }

    path = os.path.join(results_dir, "top_strategies_summary.json")
    write_json_report(summary, path)
    return path, top_strategies


def build_and_dump_strategies_as_backtester_files(loaded_config, results_dir):
    """Writes every strategies_config.json entry as its own backtester-compatible
    <key>_strategy.json (usable directly with `backtester/run_backtest.py --strategy-file` or
    `pipeline/live_signal`), via the same `research_strategy_spec` shape
    `common/strategy_spec.py`'s `get_template()` already knows how to reconstruct
    (`instantiate_strategy_from_config_entry(strategy_key, entry_data)`). Returns the list of
    written paths. A malformed entry (e.g. an unrecognized class_name) is skipped with a printed
    warning rather than aborting the rest of the dump."""
    dump_dir = os.path.join(results_dir, "strategy_dumps")
    os.makedirs(dump_dir, exist_ok=True)
    written = []
    for entry_key, entry_data in loaded_config.items():
        try:
            strat_obj = instantiate_strategy_from_config_entry(entry_key, entry_data)
        except ValueError as exc:
            print(f"  WARNING: Skipping dump for strategy '{entry_key}' -- {exc}")
            continue
        strategy_def = {
            "template_name": entry_key,
            "params": entry_data.get("parameters", {}),
            "explanation": strat_obj.explain_weights(),
            "research_strategy_spec": {"strategy_key": entry_key, "entry_data": entry_data},
        }
        path = os.path.join(dump_dir, f"{entry_key}_strategy.json")
        write_json_report(strategy_def, path)
        written.append(path)
    return written


def _resolve_vaa_entry_data(entry_data: dict, args) -> dict:
    """VAA-G4 no longer hardcodes its offensive/defensive candidate pools in
    strategies_config.json -- they're structurally two DIFFERENT-natured pools (offensive/risky
    vs. defensive/safe-haven), so unlike every other strategy fixed this pass, they can't be
    soundly auto-derived from one flat universe. Both --vaa-offensive-universe and
    --vaa-defensive-universe must be supplied on the command line instead. Returns a copy of
    entry_data with them injected into its "parameters" block (CLI always wins over whatever the
    JSON might still contain), or None if either flag is missing -- the caller decides whether
    that's a hard error (a single explicit --strategy request) or a skip-with-warning (--strategy
    all, so one strategy's new requirement doesn't block every other strategy in the run)."""
    if not args.vaa_offensive_universe or not args.vaa_defensive_universe:
        return None
    entry_data = dict(entry_data)
    entry_data["parameters"] = {
        **entry_data.get("parameters", {}),
        "vaa_offensive_universe": args.vaa_offensive_universe,
        "vaa_defensive_universe": args.vaa_defensive_universe,
    }
    return entry_data


_VAA_MISSING_POOLS_MESSAGE = (
    "'vigilant_asset_allocation' no longer has a hardcoded default universe -- both "
    "--vaa-offensive-universe and --vaa-defensive-universe must be supplied."
)


def main():
    args = build_arg_parser().parse_args()
    cfg = StrategyConfig()

    loaded_config = load_strategies_config(args.config)

    if args.dump_strategies:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        written = build_and_dump_strategies_as_backtester_files(loaded_config, RESULTS_DIR)
        print(f"Dumped {len(written)} strategy definition(s) to "
              f"{os.path.join(RESULTS_DIR, 'strategy_dumps')}:")
        for path in written:
            print(f"  {path}")
        return

    # Every provider (including "synthetic") now goes through the same
    # common.data.load_universe path -- SyntheticDataProvider gives the same
    # reproducible-per-seed data whether it's this CLI or any other caller
    # asking for provider="synthetic", instead of a second, one-off
    # synthetic generator living only here.
    start = "2020-01-01"
    end = str(pd.bdate_range(start, periods=args.n_days)[-1].date())

    data_kwargs = build_data_kwargs(args)
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed

    universe_symbols = resolve_universe_from_args(args, default_symbols=DEFAULT_UNIVERSE_SYMBOLS)
    # Make sure vigilant_asset_allocation's explicit pools actually get their OHLCV data loaded,
    # even if the caller didn't also list them in --universe -- otherwise a pool would resolve
    # non-empty at the argument level but empty once intersected against the loaded universe.
    vaa_pool_symbols = list(dict.fromkeys((args.vaa_offensive_universe or []) + (args.vaa_defensive_universe or [])))
    if vaa_pool_symbols:
        universe_symbols = list(dict.fromkeys(universe_symbols + vaa_pool_symbols))

    universe = load_universe_with_banner(
        universe_symbols, start, end,
        use_cache=not args.no_cache, cache_dir=DATA_DIR,
        data_kwargs=data_kwargs, require_nonempty=False,
        cache_max_age_days=args.cache_ttl_days,
        loading_msg=f"Loading market data for {len(universe_symbols)} symbols via provider "
                    f"'{args.data_provider}' ({start} to {end}) ...",
    )
    print()

    strategies_to_run = {}

    if args.description:
        spec = parse_plain_english_strategy(args.description)
        strategies_to_run["custom_plain_english"] = NaturalLanguageStrategy(spec, cfg)
    elif args.description_file:
        if not os.path.exists(args.description_file):
            print(f"Error: Description file '{args.description_file}' not found.")
            sys.exit(1)
        with open(args.description_file, "r") as f:
            desc_text = f.read()
        spec = parse_plain_english_strategy(desc_text)
        strategies_to_run["custom_plain_english"] = NaturalLanguageStrategy(spec, cfg)
    else:
        if args.strategy == "all":
            for entry_key, entry_data in loaded_config.items():
                if entry_key == "vigilant_asset_allocation":
                    resolved = _resolve_vaa_entry_data(entry_data, args)
                    if resolved is None:
                        print(f"  WARNING: Skipping strategy '{entry_key}' -- {_VAA_MISSING_POOLS_MESSAGE}\n")
                        continue
                    entry_data = resolved
                strategies_to_run[entry_key] = instantiate_strategy_from_config_entry(entry_key, entry_data)
        elif args.strategy in loaded_config:
            entry_data = loaded_config[args.strategy]
            if args.strategy == "vigilant_asset_allocation":
                resolved = _resolve_vaa_entry_data(entry_data, args)
                if resolved is None:
                    print(f"Error: {_VAA_MISSING_POOLS_MESSAGE}")
                    sys.exit(1)
                entry_data = resolved
            strategies_to_run[args.strategy] = instantiate_strategy_from_config_entry(args.strategy, entry_data)
        else:
            print(f"Error: Unknown strategy '{args.strategy}'. Available options in config: {', '.join(loaded_config.keys())}, or 'all'.")
            sys.exit(1)

    # entry_key -> "factors" tags, only for strategies actually run FROM the
    # JSON config (an ad-hoc --description/--description-file run has no
    # config entry and is deliberately left untagged, not force-tagged).
    strategy_factor_tags = {
        strat_name: loaded_config[strat_name].get("factors", [])
        for strat_name in strategies_to_run
        if strat_name in loaded_config
    }

    report_data = {}
    weights_summary = {}

    for strat_name, strat_obj in strategies_to_run.items():
        spec_summary = strat_obj.explain_weights()
        print("=" * 80)
        print("SECTION 1: PARSED STRATEGY SPECIFICATION")
        print(spec_summary)
        print("\nSECTION 2: BACKTEST RESULTS & TARGET WEIGHTS")
        print("=" * 80)

        target_weights = strat_obj.generate_weights(universe)

        backtest_res = run_allocation_backtest(
            universe,
            target_weights,
            initial_capital=cfg.initial_capital,
            commission_pct=cfg.commission_pct,
            slippage_pct=cfg.slippage_pct
        )

        # `run_allocation_backtest` deliberately short-circuits to
        # {"equity_curve": pd.DataFrame(), "turnover": 0.0} (no metrics keys
        # at all) whenever `target_weights` came back empty -- which is
        # itself the CORRECT, documented behavior of several strategies
        # (e.g. AcceleratingDualMomentum, RSIMeanReversionStrategy,
        # SwingTrendPullbackStrategy, AdaptiveGridStrategy,
        # EnsembleRegimeSwitchingStrategy, VigilantAssetAllocation) when a
        # required ticker is missing from the loaded universe. Without this
        # guard, a single strategy hitting that legitimate path used to
        # crash the ENTIRE `--strategy all` run via an unhandled KeyError on
        # backtest_res['sharpe_ratio'] below, producing zero output (no
        # report/factor-summary/weights CSVs) for every strategy, not just
        # the affected one. Skip just this strategy and keep going instead.
        if "sharpe_ratio" not in backtest_res or backtest_res.get("equity_curve", pd.DataFrame()).empty:
            print(f"  WARNING: Skipping strategy '{strat_name}' -- backtest produced no result "
                  f"(generate_weights() returned an empty target-weights DataFrame, most likely "
                  f"because a ticker required by this strategy is missing from the loaded "
                  f"universe). This strategy is excluded from the JSON report, weights CSVs, "
                  f"and factor summary for this run; other strategies continue normally.\n")
            continue

        print(f"  Sharpe Ratio:    {backtest_res['sharpe_ratio']:.2f}")
        print(f"  CAGR:            {backtest_res['cagr'] * 100:.2f}%")
        print(f"  Max Drawdown:    {backtest_res['max_drawdown'] * 100:.2f}%")
        print(f"  Calmar Ratio:    {backtest_res['calmar_ratio']:.2f}")
        print(f"  Win Rate:        {backtest_res['win_rate'] * 100:.1f}%")
        print(f"  Profit Factor:   {backtest_res['profit_factor']:.2f}" if np.isfinite(backtest_res['profit_factor']) else "  Profit Factor:   N/A")
        print(f"  Total Turnover:  {backtest_res['total_turnover']:.2f}")
        print(f"  Total Rebal:     {backtest_res['total_rebalances']}\n")

        print("  Recent Target Weights (Last 3 Rebalances):")
        print(format_weights_pct(target_weights, 3, suffix="%\n"))

        spec = getattr(strat_obj, "spec", None)
        report_data[strat_name] = {
            "strategy_name": getattr(spec, "strategy_name", type(strat_obj).__name__),
            "raw_description": getattr(spec, "raw_description", (type(strat_obj).__doc__ or "").strip()),
            "parsed_summary": spec_summary,
            "sharpe_ratio": float(backtest_res["sharpe_ratio"]),
            "cagr": float(backtest_res["cagr"]),
            "max_drawdown": float(backtest_res["max_drawdown"]),
            "calmar_ratio": float(backtest_res["calmar_ratio"]),
            "win_rate": float(backtest_res["win_rate"]),
            "profit_factor": float(backtest_res["profit_factor"]) if np.isfinite(backtest_res["profit_factor"]) else None,
            "total_turnover": float(backtest_res["total_turnover"]),
            "total_rebalances": int(backtest_res["total_rebalances"]),
        }
        weights_summary[strat_name] = target_weights

    os.makedirs(RESULTS_DIR, exist_ok=True)
    json_path = os.path.join(RESULTS_DIR, "research_strategy_report.json")
    write_json_report(report_data, json_path)
    print(f"Saved JSON report to {json_path}")

    for strat_name, tw_df in weights_summary.items():
        csv_path = os.path.join(RESULTS_DIR, f"{strat_name}_weights.csv")
        write_dense_weights_csv(tw_df, csv_path)
        print(f"Saved full daily weights for {strat_name} to {csv_path}")

    factor_summary_path = build_and_write_factor_summary(
        report_data, strategy_factor_tags, args, start, end, RESULTS_DIR
    )
    print(f"Saved factor summary to {factor_summary_path}")

    top_strategies_path, top_strategies = build_and_write_top_strategies_summary(
        report_data, strategy_factor_tags, loaded_config, args, start, end, RESULTS_DIR, top_n=args.top_n
    )
    print(f"Saved top strategies summary to {top_strategies_path}")

    print(f"\n=== Top {len(top_strategies)} Strategies (Sharpe ratio, CAGR tie-break) ===")
    for entry in top_strategies:
        print(f"  #{entry['rank']} {entry['strategy_name']} ({entry['strategy_key']}): "
              f"Sharpe {entry['sharpe_ratio']:.2f} | CAGR {entry['cagr'] * 100:.2f}% | "
              f"MaxDD {entry['max_drawdown'] * 100:.2f}% | Calmar {entry['calmar_ratio']:.2f}")


if __name__ == "__main__":
    main()

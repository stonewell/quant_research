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

STRICT TEST POLICY: Runs on synthetic multi-asset price histories (no real market data calls).

Examples:
    python run_research_strategy.py --strategy all
    python run_research_strategy.py --config custom_config.json --strategy all
    python run_research_strategy.py --description "Rebalance monthly. Select top 3 assets from SPY, QQQ, EEM, GLD, TLT with Close > 200d SMA. Rank by 126d return and allocate using 60d inverse volatility."
    python run_research_strategy.py --description-file strategy.txt
"""

import argparse
import json
import os
import sys

# Ensure project root and research_strategy directory are in sys.path
_RS_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_RS_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _RS_ROOT not in sys.path:
    sys.path.insert(0, _RS_ROOT)

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.data import load_universe
from common.universe import add_universe_cli_args, resolve_universe_from_args
from rs.config import StrategyConfig, load_strategies_config
from rs.nl_parser import parse_plain_english_strategy
from rs.strategy import (
    AcceleratingDualMomentum,
    ActiveDualMomentumRiskParity,
    AdaptiveGridStrategy,
    BoldAssetAllocation,
    EnsembleRegimeSwitchingStrategy,
    NaturalLanguageStrategy,
    RSIMeanReversionStrategy,
    SwingTrendPullbackStrategy,
    TurtleBreakoutStrategy,
    VigilantAssetAllocation,
    VolatilityManagedStrategy,
)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

STRATEGY_CLASS_MAP = {
    "AcceleratingDualMomentum": AcceleratingDualMomentum,
    "ActiveDualMomentumRiskParity": ActiveDualMomentumRiskParity,
    "AdaptiveGridStrategy": AdaptiveGridStrategy,
    "BoldAssetAllocation": BoldAssetAllocation,
    "EnsembleRegimeSwitchingStrategy": EnsembleRegimeSwitchingStrategy,
    "NaturalLanguageStrategy": NaturalLanguageStrategy,
    "RSIMeanReversionStrategy": RSIMeanReversionStrategy,
    "SwingTrendPullbackStrategy": SwingTrendPullbackStrategy,
    "TurtleBreakoutStrategy": TurtleBreakoutStrategy,
    "VigilantAssetAllocation": VigilantAssetAllocation,
    "VolatilityManagedStrategy": VolatilityManagedStrategy,
}


def instantiate_strategy_from_config_entry(entry_key: str, entry_data: dict):
    strat_type = entry_data.get("type", "class")
    params = entry_data.get("parameters", {})
    try:
        cfg = StrategyConfig.from_dict(params)
    except ValueError as exc:
        raise ValueError(f"Invalid config for strategy '{entry_key}': {exc}") from exc

    if strat_type == "natural_language":
        plain_english = entry_data.get("plain_english_description", "")
        name = entry_data.get("name", entry_key)
        spec = parse_plain_english_strategy(plain_english, name=name)
        return NaturalLanguageStrategy(spec, config=cfg)
    elif strat_type == "class":
        cls_name = entry_data.get("class_name", "")
        if not cls_name:
            raise ValueError(f"Strategy '{entry_key}' has type 'class' but no 'class_name' key")
        cls_obj = STRATEGY_CLASS_MAP.get(cls_name)
        if not cls_obj:
            raise ValueError(f"Unrecognized strategy class_name '{cls_name}' for strategy key '{entry_key}'")
        return cls_obj(config=cfg)
    else:
        raise ValueError(f"Unknown strategy type '{strat_type}' for strategy key '{entry_key}'")


DEFAULT_UNIVERSE_SYMBOLS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ",
    "AGG", "TIP", "IEF", "LQD", "DBC", "BIL", "SCZ"
]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Researched Quantitative Trading Strategies CLI Runner")
    add_universe_cli_args(p, default_universe=DEFAULT_UNIVERSE_SYMBOLS)
    p.add_argument("--strategy",
                   default="all",
                   help="Strategy key from JSON config to evaluate, or 'all' (default: 'all')")
    p.add_argument("--config", type=str, default=None, help="Path to custom strategies JSON config file")
    p.add_argument("--description", type=str, help="Plain English strategy description text")
    p.add_argument("--description-file", type=str, help="Path to plain English strategy description text file")
    p.add_argument("--n-days", type=int, default=1200,
                   help="Number of business days of history to request (all providers)")
    p.add_argument("--seed", type=int, default=42, help="Random seed (only used with --data-provider synthetic)")
    p.add_argument("--data-provider", default="synthetic",
                   help="Market data source provider ('synthetic', 'yfinance', 'csv', or custom module specifier string e.g. 'script.py:CustomProvider')")
    p.add_argument("--data-dir", type=str, default=None,
                   help="Folder path for CSV data provider")
    p.add_argument("--no-cache", action="store_true", help="Disable local CSV caching of fetched data")
    return p


def main():
    args = build_arg_parser().parse_args()
    cfg = StrategyConfig()

    # Every provider (including "synthetic") now goes through the same
    # common.data.load_universe path -- SyntheticDataProvider gives the same
    # reproducible-per-seed data whether it's this CLI or any other caller
    # asking for provider="synthetic", instead of a second, one-off
    # synthetic generator living only here.
    start = "2020-01-01"
    end = str(pd.bdate_range(start, periods=args.n_days)[-1].date())

    data_kwargs = {"provider": args.data_provider}
    if args.data_provider == "synthetic":
        data_kwargs["seed"] = args.seed
    if args.data_dir:
        data_kwargs["folder_path"] = args.data_dir

    universe_symbols = resolve_universe_from_args(args, default_symbols=DEFAULT_UNIVERSE_SYMBOLS)

    print(f"Loading market data for {len(universe_symbols)} symbols via provider "
          f"'{args.data_provider}' ({start} to {end}) ...")
    universe = load_universe(
        universe_symbols, start=start, end=end,
        use_cache=not args.no_cache, cache_dir=DATA_DIR, **data_kwargs,
    )
    print(f"Loaded {len(universe)} symbols: {', '.join(universe.keys())}\n")

    loaded_config = load_strategies_config(args.config)
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
                strategies_to_run[entry_key] = instantiate_strategy_from_config_entry(entry_key, entry_data)
        elif args.strategy in loaded_config:
            entry_data = loaded_config[args.strategy]
            strategies_to_run[args.strategy] = instantiate_strategy_from_config_entry(args.strategy, entry_data)
        else:
            print(f"Error: Unknown strategy '{args.strategy}'. Available options in config: {', '.join(loaded_config.keys())}, or 'all'.")
            sys.exit(1)

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

        print(f"  Sharpe Ratio:    {backtest_res['sharpe_ratio']:.2f}")
        print(f"  CAGR:            {backtest_res['cagr'] * 100:.2f}%")
        print(f"  Max Drawdown:    {backtest_res['max_drawdown'] * 100:.2f}%")
        print(f"  Calmar Ratio:    {backtest_res['calmar_ratio']:.2f}")
        print(f"  Win Rate:        {backtest_res['win_rate'] * 100:.1f}%")
        print(f"  Profit Factor:   {backtest_res['profit_factor']:.2f}" if np.isfinite(backtest_res['profit_factor']) else "  Profit Factor:   N/A")
        print(f"  Total Turnover:  {backtest_res['total_turnover']:.2f}")
        print(f"  Total Rebal:     {backtest_res['total_rebalances']}\n")

        recent_rebal = target_weights.dropna(how="all").tail(3)
        print("  Recent Target Weights (Last 3 Rebalances):")
        print((recent_rebal * 100).round(1).astype(str) + "%\n")

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
        weights_summary[strat_name] = target_weights.ffill().fillna(0.0)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    json_path = os.path.join(RESULTS_DIR, "research_strategy_report.json")
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"Saved JSON report to {json_path}")

    for strat_name, tw_df in weights_summary.items():
        csv_path = os.path.join(RESULTS_DIR, f"{strat_name}_weights.csv")
        tw_df.to_csv(csv_path)
        print(f"Saved full daily weights for {strat_name} to {csv_path}")


if __name__ == "__main__":
    main()

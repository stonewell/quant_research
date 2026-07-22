#!/usr/bin/env python
"""CLI Entry Point: Researched Quantitative Trading Strategies Evaluation.

Runs backtests for:
1. Active Dual Momentum GTAA + Risk Parity (Antonacci 2014, Faber 2007)
2. Wouter Keller's Bold Asset Allocation BAA-G12 (Keller 2022)
3. Moreira & Muir Volatility-Managed Portfolios (Moreira & Muir 2017)

STRICT TEST POLICY: Runs on synthetic multi-asset price histories (no real market data calls).

Example:
    python run_research_strategy.py --strategy all
    python run_research_strategy.py --strategy dual_momentum
"""

import argparse
import json
import os
import sys
from typing import Dict

# Ensure project root is in sys.path for common and rs imports
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.testing import make_ohlcv_from_closes
from rs.config import StrategyConfig
from rs.strategy import (
    ActiveDualMomentumRiskParity,
    BoldAssetAllocation,
    VolatilityManagedStrategy,
)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def generate_synthetic_universe(n_days: int = 1200, seed: int = 42, start: str = "2020-01-01") -> Dict[str, pd.DataFrame]:
    """Generates a realistic synthetic multi-asset universe with correlated factor drift,
    market regimes, and volatility clustering. Guaranteed offline & network-free.
    """
    rng = np.random.default_rng(seed)

    # Market factor returns
    market_drift = 0.0003  # ~7.5% annual drift
    market_vol = 0.01      # ~16% annual vol
    market_returns = rng.normal(market_drift, market_vol, n_days)

    # Add a stress/crash regime in the middle (e.g. days 400-500)
    market_returns[400:500] = rng.normal(-0.002, 0.025, 100)

    symbols = [
        "SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ",
        "AGG", "TIP", "IEF", "LQD", "DBC", "BIL"
    ]

    universe = {}
    for i, sym in enumerate(symbols):
        if sym == "BIL":
            # Cash proxy: steady small positive return, ultra-low vol
            ret = rng.normal(0.0001, 0.0002, n_days)
        elif sym in ("TLT", "IEF", "AGG", "TIP"):
            # Treasuries/Bonds: slightly negative correlation to equities during stress
            ret = -0.3 * market_returns + rng.normal(0.0001, 0.005, n_days)
        elif sym == "GLD":
            # Gold: independent commodity driver
            ret = rng.normal(0.0002, 0.008, n_days)
        else:
            # Equities / Real Estate: beta * market + idiosyncratic noise
            beta = 0.8 + 0.1 * (i % 4)
            ret = beta * market_returns + rng.normal(0, 0.006, n_days)

        close = 100.0 * np.exp(np.cumsum(ret))
        universe[sym] = make_ohlcv_from_closes(close, spread=0.2, start=start)

    return universe


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Researched Quantitative Trading Strategies CLI Runner")
    p.add_argument("--strategy", choices=["dual_momentum", "baa_keller", "volatility_managed", "all"],
                   default="all", help="Strategy implementation to evaluate")
    p.add_argument("--n-days", type=int, default=1200, help="Number of synthetic trading days to simulate")
    p.add_argument("--seed", type=int, default=42, help="Random seed for synthetic data generation")
    return p


def main():
    args = build_arg_parser().parse_args()
    cfg = StrategyConfig()

    print(f"Generating synthetic multi-asset universe ({args.n_days} days, seed={args.seed}) ...")
    universe = generate_synthetic_universe(n_days=args.n_days, seed=args.seed)
    print(f"Generated synthetic data for {len(universe)} symbols: {', '.join(universe.keys())}\n")

    strategies_to_run = {}
    if args.strategy in ("dual_momentum", "all"):
        strategies_to_run["dual_momentum"] = ActiveDualMomentumRiskParity(cfg)
    if args.strategy in ("baa_keller", "all"):
        strategies_to_run["baa_keller"] = BoldAssetAllocation(cfg)
    if args.strategy in ("volatility_managed", "all"):
        strategies_to_run["volatility_managed"] = VolatilityManagedStrategy(cfg)

    report_data = {}
    weights_summary = {}

    for strat_name, strat_obj in strategies_to_run.items():
        print(f"=== Running Strategy: {strat_name} ===")
        target_weights = strat_obj.generate_weights(universe)

        backtest_res = run_allocation_backtest(
            universe,
            target_weights,
            initial_capital=cfg.initial_capital,
            commission_pct=cfg.commission_pct,
            slippage_pct=cfg.slippage_pct
        )

        explanation = strat_obj.explain_weights()
        print(f"  Logic: {explanation}")
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

        report_data[strat_name] = {
            "explanation": explanation,
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

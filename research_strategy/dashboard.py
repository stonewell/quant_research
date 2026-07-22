#!/usr/bin/env python
"""Terminal Dashboard Viewer for Researched Quantitative Trading Strategies.

Displays side-by-side performance comparisons, drawdown metrics, and asset
allocation breakdowns from saved backtest reports.
"""

import json
import os
import sys

import pandas as pd

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def display_dashboard():
    json_path = os.path.join(RESULTS_DIR, "research_strategy_report.json")
    if not os.path.exists(json_path):
        print(f"Report file not found at {json_path}.")
        print("Please run `python run_research_strategy.py` first to generate backtest results.")
        return

    with open(json_path, "r") as f:
        data = json.load(f)

    print("\n" + "=" * 80)
    print("      RESEARCHED QUANTITATIVE TRADING STRATEGIES DASHBOARD")
    print("=" * 80 + "\n")

    # Performance Comparison Table
    metrics_list = []
    for strat, metrics in data.items():
        metrics_list.append({
            "Strategy": strat,
            "Sharpe Ratio": f"{metrics['sharpe_ratio']:.2f}",
            "CAGR": f"{metrics['cagr'] * 100:.2f}%",
            "Max Drawdown": f"{metrics['max_drawdown'] * 100:.2f}%",
            "Calmar Ratio": f"{metrics['calmar_ratio']:.2f}",
            "Win Rate": f"{metrics['win_rate'] * 100:.1f}%",
            "Profit Factor": f"{metrics['profit_factor']:.2f}" if metrics['profit_factor'] is not None else "N/A",
            "Turnover": f"{metrics['total_turnover']:.2f}",
            "Rebalances": metrics['total_rebalances'],
        })

    df_metrics = pd.DataFrame(metrics_list).set_index("Strategy")
    print("=== STRATEGY PERFORMANCE SUMMARY ===")
    print(df_metrics.to_string())
    print("-" * 80 + "\n")

    # Strategy Explanations & Citations
    print("=== STRATEGY LOGIC & CITATIONS ===")
    for strat, metrics in data.items():
        print(f"* [{strat}]: {metrics['explanation']}")
    print("-" * 80 + "\n")

    # Current Target Weights Breakdown
    print("=== LATEST TARGET WEIGHT ALLOCATIONS (%) ===")
    for strat in data.keys():
        csv_path = os.path.join(RESULTS_DIR, f"{strat}_weights.csv")
        if os.path.exists(csv_path):
            weights_df = pd.read_csv(csv_path, index_col=0)
            if not weights_df.empty:
                latest_weights = (weights_df.iloc[-1] * 100).round(1)
                active_weights = latest_weights[latest_weights > 0]
                active_str = ", ".join([f"{sym}: {w:.1f}%" for sym, w in active_weights.items()])
                print(f"* [{strat}] ({weights_df.index[-1]}): {active_str or '100% Cash / Flat'}")
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    display_dashboard()

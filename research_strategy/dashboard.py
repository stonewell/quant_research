#!/usr/bin/env python
"""Terminal Dashboard Viewer for Researched Quantitative Trading Strategies.

Displays parsed strategy specifications, side-by-side performance comparisons,
drawdown metrics, and current target weight allocations from saved backtest reports.
"""

import json
import os

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

    # Top-N Leaderboard (Sharpe ratio, CAGR tie-break) -- written by
    # run_research_strategy.py's build_and_write_top_strategies_summary().
    # Kept as a standalone JSON read (not an import of run_research_strategy.py)
    # so this dashboard stays a lightweight, independent viewer.
    top_strategies_path = os.path.join(RESULTS_DIR, "top_strategies_summary.json")
    if os.path.exists(top_strategies_path):
        with open(top_strategies_path, "r") as f:
            top_summary = json.load(f)
        top_strategies = top_summary.get("top_strategies", [])
        print(f"=== TOP {len(top_strategies)} STRATEGIES (Sharpe ratio, CAGR tie-break) ===")
        for entry in top_strategies:
            profit_factor = f"{entry['profit_factor']:.2f}" if entry.get("profit_factor") is not None else "N/A"
            print(f"  #{entry['rank']} {entry['strategy_name']} ({entry['strategy_key']}): "
                  f"Sharpe {entry['sharpe_ratio']:.2f} | CAGR {entry['cagr'] * 100:.2f}% | "
                  f"MaxDD {entry['max_drawdown'] * 100:.2f}% | Calmar {entry['calmar_ratio']:.2f} | "
                  f"Profit Factor {profit_factor}")
        print(f"(of {top_summary.get('n_strategies_evaluated', len(data))} strategies evaluated this run)")
        print("-" * 80 + "\n")
    else:
        print("(No top_strategies_summary.json found -- re-run run_research_strategy.py to generate it.)\n")

    # Performance Comparison Table
    metrics_list = []
    for strat, details in data.items():
        metrics_list.append({
            "Strategy Key": strat,
            "Strategy Name": details.get("strategy_name", strat),
            "Sharpe": f"{details['sharpe_ratio']:.2f}",
            "CAGR": f"{details['cagr'] * 100:.2f}%",
            "Max DD": f"{details['max_drawdown'] * 100:.2f}%",
            "Calmar": f"{details['calmar_ratio']:.2f}",
            "Win Rate": f"{details['win_rate'] * 100:.1f}%",
            "Profit Factor": f"{details['profit_factor']:.2f}" if details['profit_factor'] is not None else "N/A",
            "Turnover": f"{details['total_turnover']:.2f}",
            "Rebalances": details['total_rebalances'],
        })

    df_metrics = pd.DataFrame(metrics_list).set_index("Strategy Key")
    print("=== STRATEGY PERFORMANCE SUMMARY ===")
    print(df_metrics.to_string())
    print("-" * 80 + "\n")

    # Parsed Strategy Specifications
    print("=== INTERPRETED PLAIN ENGLISH STRATEGY SPECIFICATIONS ===")
    for strat, details in data.items():
        summary = details.get("parsed_summary")
        if summary:
            print(summary)
            print()
        else:
            print(f"* [{strat}]: {details.get('raw_description', 'N/A')}\n")
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

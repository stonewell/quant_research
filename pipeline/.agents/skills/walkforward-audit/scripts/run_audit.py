#!/usr/bin/env python3
"""Walkforward Audit & Trading Record Anomaly Analyzer.

Automates the 3-stage quantitative evaluation process:
1. Stage 1: Cross-strategy walkforward performance aggregation & DSR calculation
2. Stage 2: Fold-level trading record anomaly detection (concentration, warmup, turnover, jumps, look-ahead)
3. Stage 3: Anomaly-adjusted Sharpe ranking and live trading risk scoring

Usage:
  python run_audit.py [--all] [--summary] [--audit] [--rank] [--top-n 10] [--results-dir PATH]
"""

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def find_results_dir(explicit_path=None) -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if p.is_dir():
            return p
        raise FileNotFoundError(f"Specified results directory not found: {explicit_path}")

    # Only check ./results in the current working directory if no explicit path given
    cwd_results = Path.cwd() / "results"
    if cwd_results.is_dir():
        return cwd_results

    raise FileNotFoundError("No results directory specified. Pass --results-dir explicitly.")


def _lookup_canonical_name_by_key(key: str, search_dir: Path = None) -> str:
    """Resolves human-readable strategy name from strategies_config.json or strategy_dumps."""
    clean_key = key.replace("_strategy", "").strip()
    cur = (search_dir or Path.cwd()).resolve()
    for p in [cur, cur.parent, cur.parent.parent, Path.cwd(), Path.cwd().parent]:
        for candidate_path in [
            p / "pipeline/research_strategy/strategies_config.json",
            p / "research_strategy/strategies_config.json",
            p / "strategies_config.json",
        ]:
            if candidate_path.is_file():
                try:
                    with open(candidate_path) as f:
                        cfg = json.load(f)
                    if clean_key in cfg and "name" in cfg[clean_key]:
                        return str(cfg[clean_key]["name"])
                except Exception:
                    pass

        for dump_dir in [
            p / "pipeline/research_strategy/results/strategy_dumps",
            p / "research_strategy/results/strategy_dumps",
        ]:
            dump_file = dump_dir / f"{clean_key}_strategy.json"
            if dump_file.is_file():
                try:
                    with open(dump_file) as f:
                        sdata = json.load(f)
                    name = (
                        sdata.get("strategy_name")
                        or sdata.get("name")
                        or sdata.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
                    )
                    if name:
                        return str(name)
                except Exception:
                    pass
    return None


def _detect_strategy_name(results_dir: Path, strategy_name: str = None) -> str:
    if strategy_name:
        return strategy_name

    # 1. Check if walkforward_summary.json or comparison_report.json directly specifies the strategy name
    for sum_name in ["walkforward_summary.json", "comparison_report.json"]:
        sum_file = results_dir / sum_name
        if sum_file.is_file():
            try:
                with open(sum_file) as f:
                    data = json.load(f)
                name = data.get("strategy_name") or data.get("strategy")
                if name:
                    return str(name)
            except Exception:
                pass

    # 2. Check if strategy.json exists in results_dir or its parent
    for candidate in [results_dir / "strategy.json", results_dir.parent / "strategy.json"]:
        if candidate.is_file():
            try:
                with open(candidate) as f:
                    sdata = json.load(f)
                name = (
                    sdata.get("strategy_name")
                    or sdata.get("name")
                    or sdata.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
                )
                if name:
                    return str(name)
            except Exception:
                pass

    # 3. Look up key in strategies_config.json or strategy_dumps
    canonical = _lookup_canonical_name_by_key(results_dir.name, results_dir)
    if canonical:
        return canonical

    # 4. Humanize directory name as last resort (never raw snake_case or filename)
    clean_name = results_dir.name.replace("_strategy", "").replace("_", " ").title()
    if clean_name and clean_name.lower() not in ("results", "output", "backtest"):
        return clean_name
    return "Walkforward Strategy"


def load_all_summaries(results_dir: Path, strategy_name: str = None):
    strategies = []
    direct_summary = results_dir / "walkforward_summary.json"
    if direct_summary.is_file():
        try:
            with open(direct_summary) as f:
                data = json.load(f)
            strat = (
                data.get("strategy_name")
                or data.get("strategy")
                or _detect_strategy_name(results_dir, strategy_name)
            )
            data["dir_name"] = ""
            data["strategy"] = strat
            strategies.append(data)
            return strategies
        except Exception as e:
            print(f"Warning: Failed to parse {direct_summary}: {e}", file=sys.stderr)

    for entry in sorted(os.listdir(results_dir)):
        sub = results_dir / entry
        if not sub.is_dir():
            continue
        summary_path = sub / "walkforward_summary.json"
        if not summary_path.is_file():
            continue
        try:
            with open(summary_path) as f:
                data = json.load(f)
            strat = (
                data.get("strategy_name")
                or data.get("strategy")
                or _detect_strategy_name(sub, strategy_name)
            )
            data["dir_name"] = entry
            data["strategy"] = strat
            strategies.append(data)
        except Exception as e:
            print(f"Warning: Failed to parse {summary_path}: {e}", file=sys.stderr)
    return strategies


def run_summary(strategies):
    print("=" * 135)
    print(f"{'Strategy':<38} {'Sharpe':>8} {'CAGR%':>8} {'MaxDD%':>8} {'Calmar':>8} {'DSR':>10} {'Folds':>6} {'Period':>25}")
    print("=" * 135)

    strategies_sorted = sorted(strategies, key=lambda s: s["mean_sharpe_ratio"], reverse=True)

    for s in strategies_sorted:
        dsr = s.get("deflated_sharpe_ratio")
        dsr_str = f"{dsr:.4f}" if dsr is not None and abs(dsr) > 1e-10 else "~0"
        period = f"{s.get('actual_first_fold_start', '?')} → {s.get('actual_last_fold_end', '?')}"
        print(f"{s['strategy']:<38} {s['mean_sharpe_ratio']:>8.3f} {s['mean_cagr']*100:>8.2f} "
              f"{s['mean_max_drawdown']*100:>8.1f} {s['mean_calmar_ratio']:>8.3f} "
              f"{dsr_str:>10} {s['n_folds']:>6} {period:>25}")

    print("=" * 135)
    print(f"Total strategies: {len(strategies)} | "
          f"Positive Sharpe: {sum(1 for s in strategies if s['mean_sharpe_ratio'] > 0)} | "
          f"Profitable (CAGR > 0): {sum(1 for s in strategies if s['mean_cagr'] > 0)} | "
          f"DSR > 0.05: {sum(1 for s in strategies if (s.get('deflated_sharpe_ratio') or 0) > 0.05)}")
    return strategies_sorted


def audit_strategy_trades(results_dir: Path, dir_name: str, summary_data: dict):
    path = (results_dir / dir_name / "walkforward_rebalances.csv") if dir_name else (results_dir / "walkforward_rebalances.csv")
    if not path.is_file():
        return None

    trades = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row["fold"] = int(row["fold"])
                row["rebalance_id"] = int(row["rebalance_id"])
                for k in ["price", "prior_weight", "target_weight", "weight_change",
                           "trade_value", "shares", "prior_shares", "target_shares",
                           "commission", "slippage", "total_cost", "portfolio_equity"]:
                    row[k] = float(row.get(k, 0.0) or 0.0)
                trades.append(row)
            except Exception:
                continue

    if not trades:
        return None

    folds = sorted(set(t["fold"] for t in trades))
    fold_perf = summary_data.get("rolling_window_performance", [])

    # Anomaly checks:
    # 1. Concentration >= 80%
    concentrated_trades = [t for t in trades if abs(t["target_weight"]) >= 0.80]
    all_in_trades = [t for t in trades if abs(t["target_weight"]) >= 0.99]

    # 2. Weight sum per rebalance
    rebal_groups = defaultdict(dict)
    for t in trades:
        rebal_groups[(t["fold"], t["rebalance_id"], t["date"])][t["symbol"]] = t["target_weight"]
    weight_sums = [sum(ws.values()) for ws in rebal_groups.values()]
    avg_weight_sum = statistics.mean(weight_sums) if weight_sums else 1.0
    underinvested_rebals = sum(1 for ws in weight_sums if ws < 0.80)

    # 3. Warmup delay in Fold 1
    fold1_trades = [t for t in trades if t["fold"] == 1]
    warmup_days = 0
    if fold1_trades and fold_perf:
        try:
            d_start = datetime.strptime(fold_perf[0].get("start_date", ""), "%Y-%m-%d")
            d_trade = datetime.strptime(min(t["date"] for t in fold1_trades), "%Y-%m-%d")
            warmup_days = (d_trade - d_start).days
        except Exception:
            warmup_days = 0

    # 4. Single-rebalance equity jumps
    fold_equities = defaultdict(dict)
    for t in trades:
        fold_equities[t["fold"]][(t["rebalance_id"], t["date"])] = t["portfolio_equity"]
    max_jump = 0.0
    for fold_num, rebal_eq in fold_equities.items():
        equities = [v for k, v in sorted(rebal_eq.items())]
        for i in range(1, len(equities)):
            if equities[i-1] > 0:
                jump = (equities[i] - equities[i-1]) / equities[i-1]
                max_jump = max(max_jump, jump)

    # 5. Turnover ratio per fold
    fold_turnovers = []
    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        tot_val = sum(abs(t["trade_value"]) for t in ft)
        avg_eq = statistics.mean(t["portfolio_equity"] for t in ft) if ft else 1.0
        fold_turnovers.append(tot_val / avg_eq if avg_eq > 0 else 0.0)
    avg_turnover = statistics.mean(fold_turnovers) if fold_turnovers else 0.0

    # 6. Directional hit rate (look-ahead check)
    sym_trades = defaultdict(list)
    for t in trades:
        sym_trades[t["symbol"]].append(t)
    buys_up = 0
    tot_buys = 0
    for sym, slist in sym_trades.items():
        for i in range(len(slist) - 1):
            if slist[i]["action"] == "BUY" and slist[i]["price"] > 0:
                tot_buys += 1
                if slist[i+1]["price"] > slist[i]["price"]:
                    buys_up += 1
    buy_hit_rate = buys_up / tot_buys if tot_buys > 0 else 0.50

    return {
        "dir_name": dir_name,
        "strategy": (
            summary_data.get("strategy")
            or summary_data.get("strategy_name")
            or _detect_strategy_name(results_dir / dir_name if dir_name else results_dir)
        ),
        "total_trades": len(trades),
        "total_rebals": len(rebal_groups),
        "unique_symbols": len(set(t["symbol"] for t in trades)),
        "concentrated_trades": len(concentrated_trades),
        "all_in_trades": len(all_in_trades),
        "avg_weight_sum": avg_weight_sum,
        "underinvested_rebals": underinvested_rebals,
        "warmup_days_fold1": warmup_days,
        "max_equity_jump": max_jump,
        "avg_turnover": avg_turnover,
        "buy_hit_rate": buy_hit_rate,
    }


def run_ranking(strategies, results_dir: Path, top_n: int = 10):
    sorted_strats = sorted(strategies, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:top_n]
    results = []

    for s in sorted_strats:
        audit = audit_strategy_trades(results_dir, s["dir_name"], s)
        raw_sharpe = s["mean_sharpe_ratio"]
        dsr = s.get("deflated_sharpe_ratio") or 0.0
        folds = s.get("rolling_window_performance", [])
        sharpe_std = s.get("fold_sharpe_std", 1.0)
        winning_folds = sum(1 for f in folds if f.get("sharpe_ratio", 0) > 0)

        if not audit:
            results.append({
                "strategy": s["strategy"],
                "raw_sharpe": raw_sharpe,
                "adj_sharpe": raw_sharpe,
                "cagr": s["mean_cagr"],
                "maxdd": s["mean_max_drawdown"],
                "dsr": dsr,
                "win": f"{winning_folds}/{len(folds)}",
                "notes": "No rebalances.csv found",
            })
            continue

        # Penalties:
        # 1. Concentration penalty: 0.05 per all-in trade, capped at 0.40
        p_conc = min(audit["all_in_trades"] * 0.05, 0.40)

        # 2. Under-investment penalty
        p_under = max(0.0, (1.0 - audit["avg_weight_sum"]) * 0.50)

        # 3. Warmup bias penalty (idle > 60 days in fold 1)
        p_warm = 0.05 if audit["warmup_days_fold1"] > 60 else 0.0

        # 4. Outlier equity jump penalty (>30% jump)
        p_jump = max(0.0, (audit["max_equity_jump"] - 0.30) * 0.30) if audit["max_equity_jump"] > 0.30 else 0.0

        # 5. Turnover cost penalty (20bps extra per turnover unit annualized)
        p_tover = min(audit["avg_turnover"] * 0.002 * 2 * 0.50, 0.30)

        # 6. Tradability friction (near-daily rebalancing >50 dates per fold)
        avg_dates = audit["total_rebals"] / max(len(folds), 1)
        p_trade = 0.10 if avg_dates > 50 else (0.05 if avg_dates > 30 else 0.0)

        # Bonuses:
        b_dsr = 0.15 if dsr > 0.95 else (0.10 if dsr > 0.50 else (0.05 if dsr > 0.05 else 0.0))
        b_cons = (0.10 if winning_folds == len(folds) else (0.05 if winning_folds >= len(folds) - 1 else 0.0))
        if sharpe_std < 1.0:
            b_cons += 0.05

        adj_sharpe = raw_sharpe - (p_conc + p_under + p_warm + p_jump + p_tover + p_trade) + (b_dsr + b_cons)

        results.append({
            "strategy": s["strategy"],
            "raw_sharpe": raw_sharpe,
            "adj_sharpe": adj_sharpe,
            "cagr": s["mean_cagr"],
            "maxdd": s["mean_max_drawdown"],
            "dsr": dsr,
            "win": f"{winning_folds}/{len(folds)}",
            "p_conc": p_conc,
            "p_under": p_under,
            "p_warm": p_warm,
            "p_jump": p_jump,
            "p_tover": p_tover,
            "p_trade": p_trade,
            "b_dsr": b_dsr,
            "b_cons": b_cons,
            "all_in": audit["all_in_trades"],
            "avg_wsum": audit["avg_weight_sum"],
            "max_jump": audit["max_equity_jump"],
            "turnover": audit["avg_turnover"],
        })

    results.sort(key=lambda r: r["adj_sharpe"], reverse=True)

    print("=" * 140)
    print("ANOMALY-ADJUSTED STRATEGY RANKINGS (for live trading readiness)")
    print("=" * 140)
    print(f"{'Rank':>4} {'Strategy':<35} {'Raw':>7} {'Adj':>7} {'CAGR%':>7} {'MaxDD%':>7} {'DSR':>7} "
          f"{'Win':>5} {'AllIn':>6} {'WSum':>5} {'Tover':>6} {'Jump%':>6}")
    print("-" * 140)

    for i, r in enumerate(results, 1):
        dsr_str = f"{r['dsr']:.3f}" if r.get("dsr", 0) > 1e-5 else "~0"
        allin = r.get("all_in", "-")
        wsum = f"{r.get('avg_wsum', 1.0):.2f}"
        tover = f"{r.get('turnover', 0.0):.1f}"
        jump = f"{r.get('max_jump', 0.0)*100:.0f}%"
        print(f"{i:>4} {r['strategy']:<35} {r['raw_sharpe']:>7.3f} {r['adj_sharpe']:>7.3f} "
              f"{r['cagr']*100:>7.2f} {r['maxdd']*100:>7.1f} {dsr_str:>7} "
              f"{r['win']:>5} {allin:>6} {wsum:>5} {tover:>6} {jump:>6}")

    print("=" * 140)
    return results


def main():
    parser = argparse.ArgumentParser(description="Walkforward Audit & Trading Record Anomaly Analyzer")
    parser.add_argument("--results-dir", type=str, default=None, help="Path to results directory")
    parser.add_argument("--all", action="store_true", help="Run full pipeline: summary, trade audit, and adjusted ranking")
    parser.add_argument("--summary", action="store_true", help="Run cross-strategy performance aggregation")
    parser.add_argument("--audit", action="store_true", help="Run deep-dive trading record anomaly audit")
    parser.add_argument("--rank", action="store_true", help="Compute anomaly-adjusted rankings")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top strategies to evaluate")
    parser.add_argument("--strategy", type=str, default=None, help="Analyze single strategy by name")

    args = parser.parse_args()
    results_dir = find_results_dir(args.results_dir)
    print(f"Using results directory: {results_dir}\n")

    summaries = load_all_summaries(results_dir, strategy_name=args.strategy)
    if not summaries:
        print("No walkforward_summary.json files found.", file=sys.stderr)
        sys.exit(1)

    if args.all or (not args.summary and not args.audit and not args.rank):
        # Default: run all
        run_summary(summaries)
        print("\n" + "#" * 100)
        print("# STAGE 2: TRADING RECORD ANOMALY AUDIT")
        print("#" * 100)
        sorted_s = sorted(summaries, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:args.top_n]
        for s in sorted_s:
            audit = audit_strategy_trades(results_dir, s["dir_name"], s)
            if audit:
                print(f"\n[{audit['strategy']}] Trades: {audit['total_trades']}, "
                      f"All-In Events: {audit['all_in_trades']}, "
                      f"Avg Weight Sum: {audit['avg_weight_sum']:.2f}, "
                      f"Fold 1 Warmup: +{audit['warmup_days_fold1']}d, "
                      f"Max Equity Jump: {audit['max_equity_jump']*100:.1f}%, "
                      f"Turnover: {audit['avg_turnover']:.1f}x, "
                      f"Buy Hit Rate: {audit['buy_hit_rate']*100:.1f}%")
        print("\n")
        run_ranking(summaries, results_dir, top_n=args.top_n)
    else:
        if args.summary:
            run_summary(summaries)
        if args.audit:
            sorted_s = sorted(summaries, key=lambda s: s["mean_sharpe_ratio"], reverse=True)[:args.top_n]
            for s in sorted_s:
                audit = audit_strategy_trades(results_dir, s["dir_name"], s)
                if audit:
                    print(f"\n[{audit['strategy']}] Trades: {audit['total_trades']}, "
                          f"All-In Events: {audit['all_in_trades']}, "
                          f"Avg Weight Sum: {audit['avg_weight_sum']:.2f}, "
                          f"Fold 1 Warmup: +{audit['warmup_days_fold1']}d, "
                          f"Max Equity Jump: {audit['max_equity_jump']*100:.1f}%, "
                          f"Turnover: {audit['avg_turnover']:.1f}x, "
                          f"Buy Hit Rate: {audit['buy_hit_rate']*100:.1f}%")
        if args.rank:
            run_ranking(summaries, results_dir, top_n=args.top_n)


if __name__ == "__main__":
    main()

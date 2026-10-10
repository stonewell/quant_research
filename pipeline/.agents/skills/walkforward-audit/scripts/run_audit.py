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

    if not strategies:
        for entry in sorted(os.listdir(results_dir)):
            sub = results_dir / entry
            if not sub.is_dir():
                continue
            for sub_entry in sorted(os.listdir(sub)):
                sub_sub = sub / sub_entry
                if not sub_sub.is_dir():
                    continue
                summary_path = sub_sub / "walkforward_summary.json"
                if not summary_path.is_file():
                    continue
                try:
                    with open(summary_path) as f:
                        data = json.load(f)
                    strat = (
                        data.get("strategy_name")
                        or data.get("strategy")
                        or _detect_strategy_name(sub_sub, strategy_name)
                    )
                    data["dir_name"] = f"{entry}/{sub_entry}"
                    data["strategy"] = f"{strat} [Batch {entry}]"
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

    # 2. Stateful tracking of actual held portfolio weights per fold
    # NOTE: walkforward_rebalances.csv only records assets adjusted during a rebalance event
    # (|weight_change| > 1e-7). In asynchronous or asset-level inertia strategies, untouched
    # assets from prior rebalances do not emit rows. Naively summing target_weight in trade rows
    # severely distorts the average invested weight (e.g. reporting 0.12 instead of 0.65).
    # We maintain a stateful tracker of active positions across rebalance events per fold.
    rebal_groups = defaultdict(dict)
    held_weight_sums = []
    underinvested_rebals = 0

    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        event_dict = defaultdict(list)
        events = []
        for t in ft:
            rebal_key = (t["fold"], t["rebalance_id"], t["date"])
            rebal_groups[rebal_key][t["symbol"]] = t["target_weight"]

            event_key = (t["rebalance_id"], t["date"])
            if event_key not in event_dict:
                events.append(event_key)
            event_dict[event_key].append(t)

        held_pos = {}
        for ev in events:
            for t in event_dict[ev]:
                sym = t["symbol"]
                tw = t["target_weight"]
                if tw > 1e-7:
                    held_pos[sym] = tw
                else:
                    held_pos.pop(sym, None)
            total_invested = sum(held_pos.values())
            held_weight_sums.append(total_invested)
            if total_invested < 0.80:
                underinvested_rebals += 1

    avg_weight_sum = statistics.mean(held_weight_sums) if held_weight_sums else 1.0

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
        winning_folds = sum(1 for f in folds if (f.get("sharpe_ratio") or 0) > 0)

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
            "dir_name": s.get("dir_name", ""),
            "summary": s,
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


def audit_strategy_assets(results_dir: Path, dir_name: str):
    """Performs stateful asset-level trade analysis and PnL attribution from walkforward_rebalances.csv."""
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
    symbol_stats = defaultdict(lambda: {
        "buys": 0.0, "sells": 0.0, "costs": 0.0, "end_val": 0.0,
        "trades": 0, "buy_count": 0, "sell_count": 0, "folds_present": set(),
    })

    for f_num in folds:
        ft = [t for t in trades if t["fold"] == f_num]
        events = sorted(set((t["rebalance_id"], t["date"]) for t in ft))
        event_dict = defaultdict(list)
        for t in ft:
            event_dict[(t["rebalance_id"], t["date"])].append(t)

        held = {}
        for ev in events:
            for t in event_dict[ev]:
                sym = t["symbol"]
                val = t["trade_value"]
                cost = t["total_cost"]
                act = t["action"]
                st = symbol_stats[sym]
                st["costs"] += cost
                st["trades"] += 1
                st["folds_present"].add(f_num)
                if act == "BUY":
                    st["buys"] += val
                    st["buy_count"] += 1
                elif act == "SELL":
                    st["sells"] += val
                    st["sell_count"] += 1

                if t["target_shares"] > 1e-4:
                    held[sym] = (t["target_shares"], t["price"])
                else:
                    held.pop(sym, None)

        for sym, (sh, pr) in held.items():
            symbol_stats[sym]["end_val"] += sh * pr

    assets = []
    for sym, st in symbol_stats.items():
        net_pnl = st["sells"] + st["end_val"] - st["buys"] - st["costs"]
        roi = (net_pnl / st["buys"] * 100.0) if st["buys"] > 0 else 0.0
        assets.append({
            "symbol": sym,
            "net_pnl": net_pnl,
            "buys": st["buys"],
            "sells": st["sells"],
            "costs": st["costs"],
            "end_val": st["end_val"],
            "trades": st["trades"],
            "buy_count": st["buy_count"],
            "sell_count": st["sell_count"],
            "folds": len(st["folds_present"]),
            "roi": roi,
        })

    assets.sort(key=lambda a: a["net_pnl"], reverse=True)
    winners = [a for a in assets if a["net_pnl"] > 0]
    losers = [a for a in assets if a["net_pnl"] < 0]

    return {
        "trades": trades,
        "assets": assets,
        "winners": winners,
        "losers": losers,
        "total_cost": sum(a["costs"] for a in assets),
        "total_bought": sum(a["buys"] for a in assets),
        "total_sold": sum(a["sells"] for a in assets),
        "total_net_pnl": sum(a["net_pnl"] for a in assets),
    }


def deep_analyze_top_strategies(
    ranked_results: list,
    results_dir: Path,
    top_n: int = 3,
    universe_file: str = None,
    export_pruned_path: str = None,
):
    """Performs deep behavioral, regime, and asset-attribution audit on top N strategies and recommends losing asset exclusions."""
    if not ranked_results:
        print("No ranked strategies available for deep analysis.", file=sys.stderr)
        return

    top_strats = ranked_results[:top_n]
    print("\n" + "=" * 140)
    print(f"STAGE 4: TOP {len(top_strats)} STRATEGY DEEP BEHAVIORAL & ANOMALY ANALYSIS")
    print("=" * 140)

    agg_symbol_stats = defaultdict(lambda: {
        "net_pnl": 0.0, "buys": 0.0, "sells": 0.0, "costs": 0.0,
        "trades": 0, "win_strats": 0, "loss_strats": 0, "strats": set(),
    })
    all_observed_symbols = set()

    for rank_idx, r in enumerate(top_strats, 1):
        strat_name = r["strategy"]
        dir_name = r.get("dir_name", "")
        summary = r.get("summary", {})
        folds_perf = summary.get("rolling_window_performance", [])

        asset_data = audit_strategy_assets(results_dir, dir_name)
        trade_audit = audit_strategy_trades(results_dir, dir_name, summary)

        # Behavioral & Fold Metrics
        cagrs = [(f.get("cagr") or 0.0) for f in folds_perf]
        win_folds = sum(1 for c in cagrs if c > 0)

        best_fold_idx = cagrs.index(max(cagrs)) if cagrs else 0
        worst_fold_idx = cagrs.index(min(cagrs)) if cagrs else 0
        pos_cagr_sum = sum(c for c in cagrs if c > 0)
        max_fold_share = (max(cagrs) / pos_cagr_sum) if pos_cagr_sum > 0 else 0.0

        # Capital & Friction
        total_costs = asset_data["total_cost"] if asset_data else 0.0
        total_pnl = asset_data["total_net_pnl"] if asset_data else 0.0
        active_wsum = r.get("avg_wsum", 1.0)
        idle_cash = max(0.0, 1.0 - active_wsum)

        # Anomaly Diagnostics
        anomalies = []
        if r.get("all_in", 0) > 0:
            anomalies.append(f"CONCENTRATION: {r['all_in']} all-in (>=99%) single-stock bets detected")
        if idle_cash > 0.50:
            anomalies.append(f"CASH DRAG: {idle_cash*100:.1f}% average uninvested idle capital cushion")
        if r.get("turnover", 0.0) > 3.0:
            anomalies.append(f"HIGH TURNOVER: {r['turnover']:.1f}x turnover per fold incurs severe execution friction")
        if max_fold_share > 0.40 and len(cagrs) > 2:
            anomalies.append(f"PROFIT CONCENTRATION: Best Fold {best_fold_idx+1} accounts for {max_fold_share*100:.1f}% of positive CAGR")
        if r.get("p_warm", 0.0) > 0:
            anomalies.append("WARMUP DELAY: Fold 1 delayed first trade > 60 days")
        if r.get("max_jump", 0.0) > 0.30:
            anomalies.append(f"OUTLIER EQUITY JUMP: Single rebalance equity jumped {r['max_jump']*100:.1f}%")
        if trade_audit and trade_audit.get("buy_hit_rate", 0.5) > 0.70:
            anomalies.append(f"LOOK-AHEAD RISK: Buy hit rate is {trade_audit['buy_hit_rate']*100:.1f}% (suspiciously high for equities)")

        print(f"\n[{rank_idx}] {strat_name}")
        print("-" * 140)
        dsr_val = r.get("dsr", 0.0)
        dsr_str = f"{dsr_val:.3f}" if dsr_val > 1e-5 else "~0"
        print(f"  Overall: Adj Sharpe: {r.get('adj_sharpe', 0.0):.3f} | Raw Sharpe: {r.get('raw_sharpe', 0.0):.3f} | "
              f"CAGR: {r.get('cagr', 0.0)*100:.2f}% | MaxDD: {r.get('maxdd', 0.0)*100:.1f}% | DSR: {dsr_str}")
        if folds_perf:
            best_f = folds_perf[best_fold_idx]
            worst_f = folds_perf[worst_fold_idx]
            best_cagr = (best_f.get('cagr') or 0.0) * 100
            best_mdd = (best_f.get('max_drawdown') or 0.0) * 100
            worst_cagr = (worst_f.get('cagr') or 0.0) * 100
            worst_mdd = (worst_f.get('max_drawdown') or 0.0) * 100
            print(f"  Fold Dynamics: {win_folds}/{len(folds_perf)} Winning Folds | "
                  f"Best: Fold {best_fold_idx+1} ({best_f.get('start_date')} to {best_f.get('end_date')}, CAGR: {best_cagr:.1f}%, MaxDD: {best_mdd:.1f}%) | "
                  f"Worst: Fold {worst_fold_idx+1} ({worst_f.get('start_date')} to {worst_f.get('end_date')}, CAGR: {worst_cagr:.1f}%, MaxDD: {worst_mdd:.1f}%)")
        print(f"  Capital & Friction: Active Weight Sum: {active_wsum:.2f} (Idle Cash: {idle_cash*100:.0f}%) | "
              f"Turnover: {r.get('turnover', 0.0):.1f}x | Total Friction Cost: {total_costs:,.2f} RMB | Net PnL: {total_pnl:,.2f} RMB")

        if anomalies:
            print("  Identified Anomalies & Friction Risks:")
            for a in anomalies:
                print(f"    - ⚠️  {a}")
        else:
            print("  Identified Anomalies: None (Clean institutional execution profile)")

        if asset_data and asset_data["assets"]:
            top_winners = asset_data["winners"][:3]
            top_losers = asset_data["losers"][-3:]
            w_str = ", ".join(f"{w['symbol']} (+{w['net_pnl']:,.0f} RMB, {w['roi']:+.1f}%)" for w in top_winners) if top_winners else "None"
            l_str = ", ".join(f"{l['symbol']} ({l['net_pnl']:,.0f} RMB, {l['roi']:+.1f}%)" for l in top_losers) if top_losers else "None"
            print(f"  Top Alpha Drivers : {w_str}")
            print(f"  Severe Loss Drags : {l_str}")

            for a in asset_data["assets"]:
                sym = a["symbol"]
                all_observed_symbols.add(sym)
                st = agg_symbol_stats[sym]
                st["net_pnl"] += a["net_pnl"]
                st["buys"] += a["buys"]
                st["sells"] += a["sells"]
                st["costs"] += a["costs"]
                st["trades"] += a["trades"]
                st["strats"].add(strat_name)
                if a["net_pnl"] < -1e-4:
                    st["loss_strats"] += 1
                elif a["net_pnl"] > 1e-4:
                    st["win_strats"] += 1

    # Stage 5: Consolidated Losing Assets & Exclusion Recommendations
    print("\n" + "=" * 140)
    print("STAGE 5: ASSET-LEVEL LOSS DRAG ATTRIBUTION & UNIVERSE EXCLUSION RECOMMENDATIONS")
    print("=" * 140)

    consolidated = []
    for sym, st in agg_symbol_stats.items():
        net = st["net_pnl"]
        roi = (net / st["buys"] * 100.0) if st["buys"] > 0 else 0.0
        consolidated.append({
            "symbol": sym,
            "net_pnl": net,
            "buys": st["buys"],
            "sells": st["sells"],
            "costs": st["costs"],
            "trades": st["trades"],
            "roi": roi,
            "loss_strats": st["loss_strats"],
            "win_strats": st["win_strats"],
            "n_strats": len(st["strats"]),
        })

    consolidated.sort(key=lambda x: x["net_pnl"])
    recommended_losers = [c for c in consolidated if c["net_pnl"] < -1e-4]

    if recommended_losers:
        print(f"{'Symbol':<12} {'Net PnL (RMB)':>15} {'ROI%':>8} {'Trades':>8} {'Costs (RMB)':>14} {'Loss/Traded Strats':>20} {'Exclusion Diagnosis':<45}")
        print("-" * 140)
        for l in recommended_losers:
            diag = []
            if l["net_pnl"] < -2000:
                diag.append("Heavy capital loss")
            if l["roi"] < -3.0:
                diag.append("Negative ROI drift")
            if l["costs"] > abs(l["net_pnl"]) * 0.5:
                diag.append("High turnover friction")
            if l["loss_strats"] >= 2:
                diag.append(f"Multi-strategy failure ({l['loss_strats']}/{l['n_strats']})")
            diag_str = "; ".join(diag) if diag else "Net negative alpha contribution"

            strat_ratio = f"{l['loss_strats']}/{l['n_strats']}"
            print(f"{l['symbol']:<12} {l['net_pnl']:>15,.2f} {l['roi']:>7.1f}% {l['trades']:>8} {l['costs']:>14,.2f} {strat_ratio:>20} {diag_str:<45}")

        total_loss_drag = sum(l["net_pnl"] for l in recommended_losers)
        total_costs_saved = sum(l["costs"] for l in recommended_losers)
        print("-" * 140)
        print(f"SUMMARY: Identified {len(recommended_losers)} losing assets dragging down portfolio performance.")
        print(f"Total Loss Drag Eliminated : {total_loss_drag:>15,.2f} RMB")
        print(f"Total Friction Fees Saved  : {total_costs_saved:>15,.2f} RMB")
    else:
        print("No severe losing assets detected across the evaluated top strategies.")

    # Determine base universe
    base_universe = []
    if universe_file and os.path.isfile(universe_file):
        with open(universe_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    for sym in line.replace(",", " ").replace('"', '').replace("'", "").split():
                        if sym:
                            base_universe.append(sym)
    else:
        base_universe = sorted(all_observed_symbols)

    losing_syms = set(l["symbol"] for l in recommended_losers)
    pruned_universe = [s for s in base_universe if s not in losing_syms]

    print("\n" + "-" * 140)
    print(f"RECOMMENDED PRUNED UNIVERSE ({len(pruned_universe)} of {len(base_universe)} assets retained):")
    print("-" * 140)
    print(" ".join(pruned_universe))

    if export_pruned_path:
        out_p = Path(export_pruned_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w") as f:
            for sym in pruned_universe:
                f.write(f"{sym}\n")
        print(f"\nSuccessfully exported pruned universe to: {out_p.resolve()}")

    print("\nRecommended CLI Execution Snippet:")
    if export_pruned_path:
        print(f"  --universe-file {export_pruned_path}")
    else:
        print(f"  --universe {' '.join(pruned_universe[:10])} ...")
    print("=" * 140)


def audit_abnormal_and_overfit(
    ranked_results: list,
    results_dir: Path,
    top_n: int = 3,
    export_report_path: str = None,
):
    """Conducts a forensic quantitative anomaly & overfitting audit on top N strategies.

    Inspects:
    1. Look-Ahead Bias & Directional Timing Anomalies (buy hit rate > 70%)
    2. Single-Stock All-In Concentration (bets >= 80% or 99%)
    3. Fold 1 Warmup Padding (first trade delayed > 60d)
    4. Outlier Equity Jumps (single-rebalance jump > 30%)
    5. Statistical Alpha Deflation (DSR < 0.05 vs > 0.50)
    6. Profit Concentration / Regime Fragility ("One-Hit Wonder" fold share > 40%)
    7. Superstar Asset Fragility & Leave-One-Out (LOO) Sensitivity Test
    8. Turnover Friction Bleed (Transaction fees as % of gross/net PnL)
    9. Capital Efficiency vs Cash Flattery (Cash-normalized MaxDD)
    10. Overfitting Risk Score (0-100) & Live-Trading Safety Grade (A / B / C / F)
    """
    if not ranked_results:
        print("No ranked strategies available for anomaly & overfit audit.", file=sys.stderr)
        return

    top_strats = ranked_results[:top_n]
    print("\n" + "=" * 140)
    print(f"STAGE 6: FORENSIC ANOMALY & OVERFITTING AUDIT (TOP {len(top_strats)} STRATEGIES)")
    print("=" * 140)

    report_markdown_blocks = []

    for idx, r in enumerate(top_strats, 1):
        strat_name = r["strategy"]
        dir_name = r.get("dir_name", "")
        summary = r.get("summary", {})
        folds_perf = summary.get("rolling_window_performance", [])

        asset_data = audit_strategy_assets(results_dir, dir_name)
        trade_audit = audit_strategy_trades(results_dir, dir_name, summary)

        # 1. Look-ahead bias
        hit_rate = trade_audit.get("buy_hit_rate", 0.50) if trade_audit else 0.50
        hit_rate_pct = hit_rate * 100.0
        lookahead_status = "CLEAN" if hit_rate <= 0.65 else ("WARNING" if hit_rate <= 0.75 else "DATA LEAKAGE RISK")
        lookahead_diag = "Realistic trend hit rate; no forward leakage" if hit_rate <= 0.65 else ("Elevated timing hit rate" if hit_rate <= 0.75 else "Suspiciously high directional accuracy")

        # 2. All-in bets & concentration
        all_in_cnt = r.get("all_in", 0)
        trades = asset_data.get("trades", []) if asset_data else []
        max_pos = max((abs(t["target_weight"]) for t in trades), default=0.0) * 100.0
        allin_status = "CLEAN" if (all_in_cnt == 0 and max_pos <= 25.0) else ("WARNING" if max_pos <= 50.0 else "HIGH CONCENTRATION")
        allin_diag = f"Strict position caps (Max pos: {max_pos:.1f}%)" if (all_in_cnt == 0 and max_pos <= 25.0) else f"{all_in_cnt} all-in trades; Max pos: {max_pos:.1f}%"

        # 3. Warmup padding
        warmup_days = trade_audit.get("warmup_days_fold1", 0) if trade_audit else 0
        warmup_status = "CLEAN" if warmup_days <= 30 else ("WARNING" if warmup_days <= 60 else "PADDED")
        warmup_diag = f"Immediate trade execution (+{warmup_days}d)" if warmup_days <= 30 else f"Delayed trading by +{warmup_days} days in Fold 1"

        # 4. Outlier equity jump
        max_jump = (r.get("max_jump", 0.0) or 0.0) * 100.0
        jump_status = "CLEAN" if max_jump <= 15.0 else ("MODERATE" if max_jump <= 30.0 else "OUTLIER DRIVEN")
        jump_diag = f"Equity curve grew incrementally (Max: {max_jump:.1f}%)" if max_jump <= 15.0 else f"Single rebalance jump of {max_jump:.1f}%"

        # 5. DSR statistical edge
        dsr = r.get("dsr", 0.0) or 0.0
        dsr_status = "EXCELLENT" if dsr >= 0.50 else ("PASS" if dsr >= 0.05 else "FAIL / MINED")
        dsr_diag = f"Deflated Sharpe confirms statistical alpha (DSR: {dsr:.4f})" if dsr >= 0.05 else f"DSR {dsr:.4f} indicates high data-mining / noise risk"

        # 6. Profit concentration ("One-Hit Wonder")
        cagrs = [(f.get("cagr") or 0.0) for f in folds_perf]
        pos_cagr_sum = sum(c for c in cagrs if c > 0)
        top_fold_share = (max(cagrs) / pos_cagr_sum * 100.0) if pos_cagr_sum > 0 else 0.0
        best_fold_idx = (cagrs.index(max(cagrs)) + 1) if cagrs else 1
        worst_fold_idx = (cagrs.index(min(cagrs)) + 1) if cagrs else 1
        fold_status = "CLEAN" if top_fold_share <= 40.0 else ("WARNING" if top_fold_share <= 60.0 else "HIGH REGIME RISK")
        fold_diag = f"Evenly distributed across folds (Fold {best_fold_idx}: {top_fold_share:.1f}%)" if top_fold_share <= 40.0 else f"Fold {best_fold_idx} generated {top_fold_share:.1f}% of positive returns"

        # 7. Superstar asset fragility & Leave-One-Out (LOO) test
        total_net_pnl = asset_data.get("total_net_pnl", 0.0) if asset_data else 0.0
        winners = asset_data.get("winners", []) if asset_data else []
        losers = asset_data.get("losers", []) if asset_data else []
        assets_list = asset_data.get("assets", []) if asset_data else []

        top1_pnl = winners[0]["net_pnl"] if len(winners) > 0 else 0.0
        top2_pnl = (winners[0]["net_pnl"] + winners[1]["net_pnl"]) if len(winners) > 1 else top1_pnl
        top3_pnl = sum(w["net_pnl"] for w in winners[:3]) if len(winners) > 0 else 0.0

        top3_share = (top3_pnl / total_net_pnl * 100.0) if total_net_pnl > 1e-4 else (999.0 if top3_pnl > 0 else 0.0)
        fragility_status = "ROBUST" if top3_share <= 60.0 else ("WARNING" if top3_share <= 100.0 else "HIGH FRAGILITY")
        fragility_diag = f"Top 3 assets contributed {top3_share:.1f}% of net gain" if top3_share <= 100.0 else f"Top 3 contributed {top3_share:.1f}% (losers drained remaining gain)"

        # LOO Calculations
        loo_results = []
        loo_results.append({
            "scenario": f"Baseline (All {len(assets_list)} Assets)",
            "pnl": total_net_pnl,
            "drop_pct": 0.0,
            "status": "Normal Operation",
        })
        if len(winners) >= 1:
            pnl_ex1 = total_net_pnl - top1_pnl
            drop_ex1 = (top1_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex1 = "VIABLE" if pnl_ex1 > 0 else "FLIPS UNPROFITABLE (CRITICAL)"
            loo_results.append({
                "scenario": f"Exclude Top 1 ({winners[0]['symbol']})",
                "pnl": pnl_ex1,
                "drop_pct": -drop_ex1,
                "status": stat_ex1,
            })
        if len(winners) >= 2:
            pnl_ex2 = total_net_pnl - top2_pnl
            drop_ex2 = (top2_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex2 = "VIABLE" if pnl_ex2 > 0 else "UNPROFITABLE"
            loo_results.append({
                "scenario": f"Exclude Top 2 ({winners[0]['symbol']}, {winners[1]['symbol']})",
                "pnl": pnl_ex2,
                "drop_pct": -drop_ex2,
                "status": stat_ex2,
            })
        if len(winners) >= 3:
            pnl_ex3 = total_net_pnl - top3_pnl
            drop_ex3 = (top3_pnl / total_net_pnl * 100.0) if total_net_pnl != 0 else 0.0
            stat_ex3 = "VIABLE" if pnl_ex3 > 0 else "SEVERE LOSS DRAIN"
            loo_results.append({
                "scenario": f"Exclude Top 3 ({winners[0]['symbol']}, {winners[1]['symbol']}, {winners[2]['symbol']})",
                "pnl": pnl_ex3,
                "drop_pct": -drop_ex3,
                "status": stat_ex3,
            })

        # 8. Friction drag ratio
        total_costs = asset_data.get("total_cost", 0.0) if asset_data else 0.0
        friction_net_drag = (total_costs / total_net_pnl * 100.0) if total_net_pnl > 1e-4 else (100.0 if total_costs > 0 else 0.0)
        friction_status = "MINIMAL" if friction_net_drag <= 15.0 else ("ELEVATED" if friction_net_drag <= 30.0 else "SEVERE BLEED")
        friction_diag = f"Friction fees consumed {friction_net_drag:.1f}% of net profit ({total_costs:,.2f} RMB)"

        # 9. Capital drag & Cash-normalized MaxDD
        active_wsum = r.get("avg_wsum", 1.0)
        idle_cash = max(0.0, 1.0 - active_wsum) * 100.0
        raw_maxdd = (r.get("maxdd", 0.0) or 0.0) * 100.0
        raw_cagr = (r.get("cagr", 0.0) or 0.0) * 100.0
        norm_maxdd = raw_maxdd / max(active_wsum, 0.10)
        norm_cagr = raw_cagr / max(active_wsum, 0.10)
        drag_status = "LOW" if idle_cash <= 25.0 else ("MODERATE" if idle_cash <= 50.0 else "HIGH CASH CUSHION")
        drag_diag = f"Avg idle cash: {idle_cash:.1f}% | Norm MaxDD: {norm_maxdd:.1f}% (vs {raw_maxdd:.1f}% raw)"

        # 10. Quantitative Overfitting Risk Score & Grade
        overfit_score = 0
        overfit_flags = []
        if dsr < 0.05:
            overfit_score += 25
            overfit_flags.append(f"DSR {dsr:.4f} < 0.05 (High probability of data-mining / noise)")
        if len(loo_results) > 1 and loo_results[1]["pnl"] <= 0:
            overfit_score += 25
            overfit_flags.append(f"Superstar Fragility: Omitting {winners[0]['symbol']} flips Net PnL to negative ({loo_results[1]['pnl']:,.2f} RMB)")
        elif top3_share > 80.0:
            overfit_score += 15
            overfit_flags.append(f"High Winner Concentration: Top 3 generate {top3_share:.1f}% of net gain")
        if top_fold_share > 50.0 and len(cagrs) > 2:
            overfit_score += 15
            overfit_flags.append(f"One-Hit Wonder Fold: Best Fold {best_fold_idx} accounts for {top_fold_share:.1f}% of positive returns")
        if friction_net_drag > 25.0:
            overfit_score += 10
            overfit_flags.append(f"Friction Bleed: Transaction friction consumes {friction_net_drag:.1f}% of net profits")
        if max_pos >= 80.0:
            overfit_score += 15
            overfit_flags.append(f"Extreme Concentration: Max single position reached {max_pos:.1f}%")
        if max_jump > 30.0:
            overfit_score += 10
            overfit_flags.append(f"Outlier Equity Jump: Single rebalance equity jumped {max_jump:.1f}%")
        if hit_rate > 0.70:
            overfit_score += 25
            overfit_flags.append(f"Look-Ahead Leakage Risk: Buy hit rate is {hit_rate_pct:.1f}%")
        if warmup_days > 60:
            overfit_score += 10
            overfit_flags.append(f"Warmup Padding: Fold 1 delayed first trade by {warmup_days} days")

        overfit_score = min(100, overfit_score)
        if overfit_score <= 20:
            grade_label = "Grade A (Institutional Grade - Highly Robust)"
        elif overfit_score <= 40:
            grade_label = "Grade B (Moderate Caution - Viable with Sizing Rules)"
        elif overfit_score <= 65:
            grade_label = "Grade C (High Overfit Risk - Fragile / Over-Parameterized)"
        else:
            grade_label = "Grade F (Rejected - Severe Overfit / Structural Dependency)"

        # Console Output
        print(f"\n[{idx}] {strat_name}")
        print("-" * 140)
        print(f"  Safety Grade: {grade_label} | Overfit Risk Index: {overfit_score} / 100")
        if overfit_flags:
            print("  Overfitting & Fragility Alerts:")
            for flag in overfit_flags:
                print(f"    - ⚠️  {flag}")
        else:
            print("  Overfitting Alerts: None detected (Institutional-grade robustness)")

        print("\n  QUANTITATIVE ANOMALY & OVERFIT INSPECTION MATRIX:")
        print("  " + "-" * 136)
        print(f"  {'Inspection Dimension':<30} {'Audited Metric':<24} {'Safety Benchmark':<24} {'Status':<12} {'Forensic Diagnostic':<44}")
        print("  " + "-" * 136)
        matrix_rows = [
            ("Look-Ahead Bias Check", f"{hit_rate_pct:.1f}% Hit Rate", "45.0% - 55.0%", lookahead_status, lookahead_diag),
            ("Single-Stock Concentration", f"Max Pos: {max_pos:.1f}%", "< 25.0% (Cap: 20%)", allin_status, allin_diag),
            ("Fold 1 Warmup Padding", f"+{warmup_days} Days", "< 60 Days", warmup_status, warmup_diag),
            ("Outlier Equity Jumps", f"{max_jump:.1f}% Single Jump", "< 30.0%", jump_status, jump_diag),
            ("Statistical Edge (DSR)", f"{dsr:.4f}", "> 0.0500", dsr_status, dsr_diag),
            ("Profit Concentration Skew", f"{top_fold_share:.1f}% from Fold {best_fold_idx}", "< 40.0%", fold_status, fold_diag),
            ("Superstar Asset Fragility", f"{top3_share:.1f}% from Top 3", "< 60.0%", fragility_status, fragility_diag),
            ("Friction Bleed Ratio", f"{friction_net_drag:.1f}% Fee / Net PnL", "< 15.0%", friction_status, friction_diag),
            ("Capital Drag & Cash Flattery", f"{idle_cash:.1f}% Idle Cash", "< 25.0%", drag_status, drag_diag),
        ]
        for dim, met, bench, stat, diag in matrix_rows:
            print(f"  {dim:<30} {met:<24} {bench:<24} {stat:<12} {diag:<44}")
        print("  " + "-" * 136)

        print("\n  LEAVE-ONE-OUT (LOO) ASSET FRAGILITY ANALYSIS:")
        print("  " + "-" * 136)
        print(f"  {'Exclusion Scenario':<55} {'Resulting Net PnL':>22} {'PnL Drop %':>15} {'Strategy Viability Assessment':<40}")
        print("  " + "-" * 136)
        for loo in loo_results:
            pnl_str = f"{loo['pnl']:>+18,.2f} RMB"
            drop_str = f"{loo['drop_pct']:>14.1f}%"
            print(f"  {loo['scenario']:<55} {pnl_str} {drop_str} {loo['status']:<40}")
        print("  " + "-" * 136)

        # Build Markdown section
        md_block = f"""## {idx}. {strat_name}
- **Safety Grade**: `{grade_label}`
- **Overfit Risk Index**: `{overfit_score} / 100`

### Quantitative Anomaly & Overfit Inspection Matrix

| Inspection Dimension | Audited Metric | Safety Benchmark | Status | Forensic Diagnostic |
| :--- | :--- | :--- | :--- | :--- |
"""
        for dim, met, bench, stat, diag in matrix_rows:
            md_block += f"| **{dim}** | **{met}** | {bench} | `{stat}` | {diag} |\n"

        md_block += f"""
### Leave-One-Out (LOO) Fragility Analysis

| Exclusion Scenario | Resulting Net PnL | PnL Drop % | Strategy Viability Assessment |
| :--- | :--- | :--- | :--- |
"""
        for loo in loo_results:
            md_block += f"| **{loo['scenario']}** | **{loo['pnl']:+,.2f} RMB** | `{loo['drop_pct']:+.1f}%` | {loo['status']} |\n"

        if overfit_flags:
            md_block += "\n### Identified Overfitting & Fragility Alerts\n"
            for flag in overfit_flags:
                md_block += f"- ⚠️ **{flag}**\n"

        report_markdown_blocks.append(md_block)

    if export_report_path:
        out_p = Path(export_report_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        full_md = f"""# Walkforward Anomaly & Overfit Forensic Audit Report

> **Audited Directory**: `{results_dir}`  
> **Evaluation Window**: Walkforward Rolling Folds  
> **Evaluated Top Strategies**: {len(top_strats)}  

---

""" + "\n---\n\n".join(report_markdown_blocks)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(full_md)
        print(f"\nSuccessfully exported forensic anomaly & overfit audit report to: {out_p.resolve()}")

    print("=" * 140)


def main():
    parser = argparse.ArgumentParser(description="Walkforward Audit & Trading Record Anomaly Analyzer")
    parser.add_argument("--results-dir", type=str, default=None, help="Path to results directory")
    parser.add_argument("--all", action="store_true", help="Run full pipeline: summary, trade audit, adjusted ranking, deep top-3 analysis, losing asset exclusions, and forensic anomaly/overfit audit")
    parser.add_argument("--summary", action="store_true", help="Run cross-strategy performance aggregation")
    parser.add_argument("--audit", action="store_true", help="Run deep-dive trading record anomaly audit")
    parser.add_argument("--rank", action="store_true", help="Compute anomaly-adjusted rankings")
    parser.add_argument("--deep-analyze", "--deep", action="store_true", help="Run deep behavioral & asset attribution audit on top strategies")
    parser.add_argument("--deep-top-n", type=int, default=3, help="Number of top strategies for deep behavioral & asset analysis (default: 3)")
    parser.add_argument("--exclude-losers", action="store_true", help="Identify and recommend losing assets to exclude from trading universe")
    parser.add_argument("--abnormal-overfit", "--overfit", "--audit-overfit", action="store_true", help="Run forensic quantitative anomaly & overfit audit (LOO fragility, superstar concentration, friction bleed, cash flattery)")
    parser.add_argument("--export-overfit-report", type=str, default=None, help="Export detailed anomaly & overfitting markdown report to file")
    parser.add_argument("--universe-file", type=str, default=None, help="Path to original universe file to prune")
    parser.add_argument("--export-pruned-universe", type=str, default=None, help="Path to export the pruned universe file")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top strategies to evaluate in ranking")
    parser.add_argument("--strategy", type=str, default=None, help="Analyze single strategy by name")

    args = parser.parse_args()
    results_dir = find_results_dir(args.results_dir)
    print(f"Using results directory: {results_dir}\n")

    summaries = load_all_summaries(results_dir, strategy_name=args.strategy)
    if not summaries:
        print("No walkforward_summary.json files found.", file=sys.stderr)
        sys.exit(1)

    has_specific_action = (
        args.summary or args.audit or args.rank or args.deep_analyze or args.exclude_losers or args.abnormal_overfit
    )

    if args.all or not has_specific_action:
        # Full institutional audit pipeline
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
        ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
        deep_analyze_top_strategies(
            ranked,
            results_dir,
            top_n=args.deep_top_n,
            universe_file=args.universe_file,
            export_pruned_path=args.export_pruned_universe,
        )
        audit_abnormal_and_overfit(
            ranked,
            results_dir,
            top_n=args.deep_top_n,
            export_report_path=args.export_overfit_report,
        )
    else:
        ranked = None
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
        if args.rank or args.deep_analyze or args.exclude_losers or args.abnormal_overfit:
            ranked = run_ranking(summaries, results_dir, top_n=args.top_n)

        if args.deep_analyze or args.exclude_losers:
            if ranked is None:
                ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
            deep_analyze_top_strategies(
                ranked,
                results_dir,
                top_n=args.deep_top_n,
                universe_file=args.universe_file,
                export_pruned_path=args.export_pruned_universe,
            )

        if args.abnormal_overfit:
            if ranked is None:
                ranked = run_ranking(summaries, results_dir, top_n=args.top_n)
            audit_abnormal_and_overfit(
                ranked,
                results_dir,
                top_n=args.deep_top_n,
                export_report_path=args.export_overfit_report,
            )


if __name__ == "__main__":
    main()


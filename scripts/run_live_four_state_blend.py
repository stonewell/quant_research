#!/usr/bin/env python3
"""Operational Daily Live Execution Runner for Chan Four-State Risk-Managed Blend Strategy.

Loads the validated `chan_four_state_blend` strategy configuration, fetches point-in-time
market data up to `--as-of-date`, evaluates the 4-state FSM + 3-type + VAA composite logic,
applies the drawdown circuit breakers, 30% breadth / 10d thrust cash deployment, and 4% inertia filter,
and outputs a complete, actionable trading ticket (SELLS FIRST -> BUYS SECOND) with 100-share lot rounding.

Usage:
    # 1. Run live check for today using the pruned 11-stock universe:
    uv run python scripts/run_live_four_state_blend.py --data-provider synthetic

    # 2. Run with real market data (MarketDB or yfinance) as of a specific date:
    uv run python scripts/run_live_four_state_blend.py --data-provider marketdb --as-of-date 2025-08-22

    # 3. Rebalance against an actual live brokerage portfolio (with 100,000 RMB equity):
    uv run python scripts/run_live_four_state_blend.py \\
        --current-holdings '{"300394.SZ": 0.15, "000938.SZ": 0.10, "BIL": 0.75}' \\
        --portfolio-value 100000
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd

# Add repo root to sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    load_universe_with_banner,
    shared_data_dir,
)
from common.strategy_spec import get_template, load_strategy_file
from common.universe import resolve_universe_from_args
from pipeline.live_signal.lsig.signal import as_of_universe, latest_rebalance_rows

DEFAULT_STRATEGY_FILE = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps", "chan_four_state_blend_strategy.json"
)
DEFAULT_UNIVERSE_FILE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "14_stocks_pruned.txt")
DEFAULT_OUTPUT_DIR = os.path.join(_REPO_ROOT, "docs", "ruleset", "chan_four_state_blend")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Daily Live Trading Ticket Generator for Chan Four-State Risk-Managed Blend"
    )
    parser.add_argument(
        "--strategy-file",
        default=DEFAULT_STRATEGY_FILE,
        help="Path to strategy.json (default: chan_four_state_blend_strategy.json)",
    )
    parser.add_argument(
        "--universe-file",
        default=DEFAULT_UNIVERSE_FILE,
        help="Path to universe symbols file (default: 14_stocks_pruned.txt)",
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="Point-in-time valuation date (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=800,
        help="Lookback calendar days for indicator warmup (default: 800 days)",
    )
    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=100000.0,
        help="Total portfolio NAV in account currency / RMB (default: 100,000.0)",
    )
    parser.add_argument(
        "--current-holdings",
        type=str,
        default=None,
        help="JSON string of current portfolio holdings: '{\"SYMBOL\": weight, ...}'",
    )
    parser.add_argument(
        "--current-holdings-file",
        type=str,
        default=None,
        help="Path to JSON file containing current portfolio holdings",
    )
    parser.add_argument(
        "--lot-size",
        type=int,
        default=100,
        help="Trading lot size for buy order rounding (default: 100 shares for A-shares)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save generated trading orders CSV",
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    return parser.parse_args()


def load_holdings(args) -> Optional[Dict[str, float]]:
    if args.current_holdings and args.current_holdings_file:
        raise ValueError("Specify either --current-holdings or --current-holdings-file, not both.")
    if args.current_holdings:
        return json.loads(args.current_holdings)
    if args.current_holdings_file:
        with open(args.current_holdings_file, "r") as f:
            return json.load(f)
    return None


def main():
    args = parse_args()
    as_of_date = args.as_of_date or date.today().isoformat()
    start_date = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    print("=" * 110)
    print("CHAN FOUR-STATE RISK-MANAGED BLEND STRATEGY - DAILY LIVE EXECUTION RUNNER")
    print("=" * 110)
    print(f"Strategy Config   : {args.strategy_file}")
    print(f"Universe Source   : {args.universe_file}")
    print(f"Valuation As-Of   : {as_of_date} (History window: {start_date} -> {as_of_date})")
    print(f"Account NAV       : {args.portfolio_value:,.2f} RMB")
    print(f"Data Provider     : {args.data_provider}")

    # 1. Load Strategy Template
    if not os.path.exists(args.strategy_file):
        raise FileNotFoundError(f"Strategy file not found at: {args.strategy_file}")
    strategy_def = load_strategy_file(args.strategy_file)
    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    template = get_template(
        template_name,
        strategy_def.get("pattern_spec"),
        strategy_def.get("research_strategy_spec"),
        strategy_def.get("composite_spec"),
        params,
    )

    # 2. Load Universe
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")

    universe = load_universe_with_banner(
        universe_symbols,
        start_date,
        as_of_date,
        interval="1d",
        use_cache=not args.no_cache,
        cache_dir=shared_data_dir(),
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, as_of_date)

    # 3. Generate Weights & Extract Latest Rebalance
    sparse_weights = template.generate_weights(universe, params)
    rebalances = latest_rebalance_rows(sparse_weights)
    if rebalances.empty:
        print("\n[ERROR] No rebalance rows generated at or before this date. Ensure sufficient lookback history.")
        return

    latest_rebal_date = rebalances.index[-1].strftime("%Y-%m-%d")
    current_targets = rebalances.iloc[-1].fillna(0.0)

    # 4. Resolve Reference Portfolio (Actual Holdings vs Strategy Last Rebalance)
    user_holdings = load_holdings(args)
    if user_holdings is not None:
        reference = pd.Series(user_holdings, dtype=float)
        reference_source = "User-Supplied Actual Live Brokerage Holdings"
    elif len(rebalances) >= 2:
        reference = rebalances.iloc[-2].fillna(0.0)
        reference_source = f"Strategy Prior Rebalance ({rebalances.index[-2].strftime('%Y-%m-%d')})"
    else:
        reference = pd.Series(0.0, index=current_targets.index)
        reference_source = "Initial Fresh Launch (100% Cash Base)"

    all_symbols = sorted(list(set(current_targets.index).union(set(reference.index))))
    target_series = current_targets.reindex(all_symbols, fill_value=0.0)
    current_series = reference.reindex(all_symbols, fill_value=0.0)

    # 5. Build Execution Order Plan
    min_trade_thresh = float(params.get("cfsb_min_weight_change", 0.05))
    cash_proxy = params.get("cash_proxy", "BIL")

    rows = []
    for sym in all_symbols:
        tgt_w = float(target_series[sym])
        cur_w = float(current_series[sym])
        delta_w = tgt_w - cur_w
        abs_delta = abs(delta_w)

        # Get latest close price
        if sym in universe and not universe[sym].empty:
            price = float(universe[sym]["Close"].iloc[-1])
        else:
            price = 1.0  # Cash proxy default

        # Determine action
        if sym == cash_proxy:
            action = "SWEEP_CASH" if delta_w > 0 else "RELEASE_CASH"
        elif abs_delta < min_trade_thresh:
            action = "HOLD_FILTERED"
        elif delta_w <= -min_trade_thresh:
            action = "SELL"
        elif delta_w >= min_trade_thresh:
            action = "BUY"
        else:
            action = "HOLD"

        target_val = tgt_w * args.portfolio_value
        current_val = cur_w * args.portfolio_value
        trade_val = delta_w * args.portfolio_value

        # Calculate lot-rounded shares for equities
        if sym != cash_proxy and price > 0:
            target_shares = int(np.floor(target_val / price / args.lot_size) * args.lot_size) if action == "BUY" else int(np.round(target_val / price))
            current_shares = int(np.round(current_val / price))
            delta_shares = target_shares - current_shares
        else:
            target_shares = 0
            current_shares = 0
            delta_shares = 0

        rows.append({
            "symbol": sym,
            "action": action,
            "price": price,
            "current_weight": cur_w,
            "target_weight": tgt_w,
            "delta_weight": delta_w,
            "current_shares": current_shares,
            "target_shares": target_shares,
            "delta_shares": delta_shares,
            "trade_value_rmb": trade_val,
        })

    order_df = pd.DataFrame(rows)

    # Sort: SELLS FIRST -> BUYS SECOND -> HOLDS
    def sort_priority(act):
        if act == "SELL":
            return 1
        if act == "BUY":
            return 2
        if act.startswith("SWEEP") or act.startswith("RELEASE"):
            return 3
        return 4

    order_df["priority"] = order_df["action"].map(sort_priority)
    order_df = order_df.sort_values(by=["priority", "delta_weight"]).drop(columns=["priority"])

    print(f"\nRebalance Valuation Date: {latest_rebal_date}")
    print(f"Reference Portfolio Basis: {reference_source}")
    print("-" * 110)

    # Display SELLS FIRST
    sells = order_df[order_df["action"] == "SELL"]
    buys = order_df[order_df["action"] == "BUY"]
    holds = order_df[order_df["action"].str.startswith("HOLD")]
    cash_rows = order_df[order_df["action"].str.contains("CASH")]

    print("\n[PHASE 1: EXECUTE SELLS FIRST (Release Purchasing Power)]")
    if sells.empty:
        print("  No sell orders required today.")
    else:
        for _, r in sells.iterrows():
            print(f"  🔴 SELL {r['symbol']:<10} | Price: {r['price']:>7.2f} | Current: {r['current_weight']:>6.1%} -> Target: {r['target_weight']:>6.1%} ({r['delta_weight']:>+6.1%}) | Sell Qty: {abs(r['delta_shares']):>6} shares (~{abs(r['trade_value_rmb']):>8,.2f} RMB)")

    print("\n[PHASE 2: EXECUTE BUYS SECOND (Allocate Available Capital)]")
    if buys.empty:
        print("  No new buy orders required today.")
    else:
        for _, r in buys.iterrows():
            print(f"  🟢 BUY  {r['symbol']:<10} | Price: {r['price']:>7.2f} | Current: {r['current_weight']:>6.1%} -> Target: {r['target_weight']:>6.1%} ({r['delta_weight']:>+6.1%}) | Buy Qty: {r['delta_shares']:>6} shares (~{r['trade_value_rmb']:>8,.2f} RMB)")

    print("\n[PHASE 3: POSITIONS HELD (Inertia Filtered < 4% or Unchanged)]")
    for _, r in holds.iterrows():
        reason = "Filtered (< 4% change)" if r["action"] == "HOLD_FILTERED" else "Optimal target maintained"
        print(f"  ⚪ HOLD {r['symbol']:<10} | Weight: {r['target_weight']:>6.1%} | Held Value: ~{r['target_weight']*args.portfolio_value:>8,.2f} RMB | ({reason})")

    if not cash_rows.empty:
        print("\n[PHASE 4: CASH PROXY & LIQUIDITY MANAGEMENT]")
        for _, r in cash_rows.iterrows():
            print(f"  💰 CASH {r['symbol']:<10} | Balance: {r['target_weight']:>6.1%} (~{r['target_weight']*args.portfolio_value:>8,.2f} RMB) | Park in {r['symbol']} / 511880 / 国债逆回购")

    # Save to CSV
    os.makedirs(args.output_dir, exist_ok=True)
    out_csv = os.path.join(args.output_dir, "live_trading_ticket.csv")
    order_df.to_csv(out_csv, index=False)
    print("\n" + "=" * 110)
    print(f"[SUCCESS] Trading ticket exported to: {out_csv}")
    print("=" * 110)


if __name__ == "__main__":
    main()

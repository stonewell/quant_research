#!/usr/bin/env python3
"""Multi-Strategy Operational Live Order Netting Runner.

Executes a portfolio of quantitative strategies in a single master brokerage account,
performing automated internal crossing (内部对冲), order netting (撮合净额化),
friction tax savings audit (税费磨损节约量化), and sub-strategy virtual ledger tracking.

Execution Hierarchy:
  Phase 1: NET SELLS FIRST (releases master purchasing power)
  Phase 2: NET BUYS SECOND (lot-rounded entries in 100-share lots)
  Phase 3: NET POSITIONS HELD (inertia & tenure protected holds)
  Phase 4: GLOBAL CASH SWEEP (idle cash into BIL / 511880 / GC001)

Usage:
  # 1. Tri-Core Blend Netting Execution with synthetic data:
  python scripts/run_live_multi_strategy.py \
    --strategies chan_crisis_shield_blend:0.40 chan_risk_managed_blend:0.35 chan_four_state_shield_blend:0.25 \
    --universe-file docs/universe/china/pruned_core_satellite_blend_16.txt \
    --portfolio-value 100000 \
    --data-provider synthetic

  # 2. Netting with actual current holdings:
  python scripts/run_live_multi_strategy.py \
    --strategies chan_crisis_shield_blend:0.40 chan_risk_managed_blend:0.35 chan_four_state_shield_blend:0.25 \
    --current-holdings '{"300394.SZ": 500, "601872.SH": 1000, "BIL": 80000}' \
    --portfolio-value 100000 \
    --data-provider synthetic
"""

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

def _find_repo_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(d, "AGENTS.md")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("Cannot locate repo root (AGENTS.md not found)")


_REPO_ROOT = _find_repo_root()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_REPO_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

from common.cli_utils import (
    add_data_provider_cli_args,
    add_market_clock_cli_args,
    build_data_kwargs,
    load_live_universe,
    persist_run_metadata,
    resolve_live_clock_context,
    shared_data_dir,
)
from common.freshness import assert_fresh_universe, latest_close
from common.order_netting import (
    FrictionSavings,
    MarketType,
    NettingResult,
    OrderNettingEngine,
    SubStrategyIntent,
    get_market_currency,
    get_market_label,
)
from common.strategy_spec import get_template, load_strategy_file
from common.universe import (
    get_stock_name,
    is_placeholder_stock_name,
    resolve_universe_from_args,
)
from pipeline.live_signal.lsig.signal import as_of_universe, latest_rebalance_rows
from scripts.live_paths import MULTI_STRATEGY_KEY, default_live_output_dir
from scripts.live_preflight import (
    LiveDumpPreflightError,
    verify_live_strategy_dump_preflight,
)
from scripts.run_live_strategy import (
    announce_run_mode,
    load_holdings,
    load_symbol_names,
    pad_east_asian,
    resolve_holding_bars,
    resolve_holdings_weights_and_shares,
    resolve_lot_size,
    resolve_run_mode,
    resolve_strategy_definition,
)

DEFAULT_UNIVERSE_FILE = os.path.join(
    _REPO_ROOT, "docs", "universe", "china", "pruned_core_satellite_blend_16.txt"
)
# Default netted-ticket output dir: `<repo_root>/scripts/results/live/multi_strategy_netted`.
# Owned by `scripts/live_paths.py` -- NOT `docs/ruleset/multi_strategy_netted` any more.
# That tree holds hand-authored operating manuals; a netting run must not write its
# generated ticket / sub-ledgers into it, and nothing may CREATE it by default. The
# web app reads this directory back when a dated archive run does not exist yet
# (`web/livetrading/storage.py::StorageManager.get_multi_netting_run`), so the path and
# the `multi_strategy_netted` key are derived here, never re-spelled at either end.
DEFAULT_OUTPUT_DIR = default_live_output_dir(MULTI_STRATEGY_KEY)


def resolve_prior_sub_ledgers(
    val_date: str,
    output_dir: Optional[str],
    sub_ledgers_file_arg: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolves prior sub-ledgers strictly before val_date to ensure re-run idempotency."""
    out_file = os.path.join(output_dir, "sub_ledgers.json") if output_dir else ""
    if sub_ledgers_file_arg and os.path.exists(sub_ledgers_file_arg):
        # Only use if it is not today's own output file
        if not out_file or os.path.abspath(sub_ledgers_file_arg) != os.path.abspath(out_file):
            try:
                with open(sub_ledgers_file_arg, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    # Search earlier date runs strictly before val_date
    cand_parents = []
    if output_dir:
        cand_parents.append(os.path.dirname(os.path.abspath(output_dir)))
        cand_parents.append(os.path.dirname(os.path.dirname(os.path.abspath(output_dir))))

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for parent in cand_parents:
        if not os.path.isdir(parent):
            continue
        sub_dates = sorted([d for d in os.listdir(parent) if date_re.match(d) and d < val_date], reverse=True)
        for d in sub_dates:
            d_path = os.path.join(parent, d)
            cands = [
                os.path.join(d_path, "sub_ledgers.json"),
            ]
            if output_dir:
                cands.append(os.path.join(d_path, os.path.basename(output_dir), "sub_ledgers.json"))
            if os.path.isdir(d_path):
                for sub in os.listdir(d_path):
                    if sub.startswith("multi_netted_"):
                        cands.append(os.path.join(d_path, sub, "sub_ledgers.json"))
            for cand in cands:
                if os.path.exists(cand):
                    try:
                        with open(cand, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data and isinstance(data, dict):
                                return data
                    except Exception:
                        pass
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Multi-Strategy Operational Live Order Netting Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=[
            "chan_crisis_shield_blend:0.40",
            "chan_risk_managed_blend:0.35",
            "chan_four_state_shield_blend:0.25",
        ],
        help="List of strategy_key:capital_weight pairs (e.g. chan_crisis_shield_blend:0.40)",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="Path to JSON configuration file specifying multi-strategy setup",
    )
    parser.add_argument(
        "--universe-file",
        default=DEFAULT_UNIVERSE_FILE,
        help="Default universe symbols file",
    )
    parser.add_argument(
        "--as-of-date",
        default=None,
        help=(
            "Valuation as-of-date (YYYY-MM-DD). Default: the market clock's as_of_trading_date "
            "(close + publication grace, market-local), never the host's local date. An explicit "
            "value must be a --market trading day and may not be later than the latest published session. "
            "WARN: a PAST trading day is a deliberate history replay -- the run-window gate is skipped by "
            "design (盘中手工回放仍能出工单) and the run is marked run_mode=replay in summary.json; do not "
            "archive that netted ticket as a same-day live one or use it for attribution."
        ),
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Lookback start date for historical market data loading (YYYY-MM-DD, default: as_of_date - lookback_days)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=800,
        help="Lookback calendar days for indicator calculation (default: 800)",
    )
    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=100000.0,
        help="Total portfolio NAV in RMB (default: 100,000.0)",
    )
    parser.add_argument(
        "--current-holdings",
        default=None,
        help="JSON string of master account holdings: '{\"SYMBOL\": shares, ...}'",
    )
    parser.add_argument(
        "--current-holdings-file",
        default=None,
        help="Path to JSON file containing master account holdings",
    )
    parser.add_argument(
        "--account-state-file",
        default=None,
        help="Path to account state file for NAV or holdings recovery",
    )
    parser.add_argument(
        "--sub-ledgers-file",
        default=None,
        help="Path to persistent virtual sub-ledgers JSON file",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Output directory to save netted trading ticket and reports (default: "
            "scripts/results/live/multi_strategy_netted, an ordinary workspace artifact "
            "directory -- never docs/ruleset/)"
        ),
    )
    parser.add_argument(
        "--min-holding-bars",
        type=int,
        default=2,
        help="Minimum tenure holding bars before liquidation is permitted (default: 2)",
    )
    parser.add_argument(
        "--inertia-threshold",
        type=float,
        default=0.05,
        help="Global weight inertia threshold below which rebalances are filtered (default: 0.05)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Market data cache directory",
    )
    parser.add_argument(
        "--allow-weighting-mode-override",
        action="store_true",
        help=(
            "EXPLORATION ONLY -- bypasses two pre-flight refusals: (1) a strategy dump whose "
            "blend_weighting_mode differs from strategies_config.json, and (2) a strategy key "
            "that is not present in strategies_config.json at all (this path prints a loud "
            "warning: the pinned mode was never verified). It does NOT bypass a malformed dump, "
            "an unidentifiable dump, or a registered strategy whose blend_weighting_mode is "
            "missing entirely. Do NOT archive the resulting run as a live ticket."
        ),
    )

    add_data_provider_cli_args(parser)
    # (L2.3) as-of / market / run-window gate / strict freshness / probe retry limit.
    add_market_clock_cli_args(parser, stage="stage2")
    return parser.parse_args()


def parse_strategy_specs(strategies_arg: List[str], config_file: Optional[str]) -> List[Dict[str, Any]]:
    """Parse strategy keys, weights, and optional custom universes."""
    specs = []
    if config_file and os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                specs = data
            elif isinstance(data, dict) and "strategies" in data:
                specs = data["strategies"]

    if not specs:
        for item in strategies_arg:
            parts = item.split(":")
            s_key = parts[0].strip()
            w = float(parts[1]) if len(parts) > 1 else 1.0
            specs.append({
                "strategy_key": s_key,
                "capital_weight": w,
            })

    # Normalize weights
    total_w = sum(s.get("capital_weight", 1.0) for s in specs)
    for s in specs:
        s["capital_weight"] = s.get("capital_weight", 1.0) / total_w
    return specs


def main():
    args = parse_args()
    # (L2.3) Netted live deployment: as-of + run-window gate resolved from the market
    # clock exactly as in Stage 2, before any data load.
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe from {args.universe_file}")
    clock_ctx = resolve_live_clock_context(args, universe_symbols, stage="stage2")
    val_date = clock_ctx.as_of_date
    # (L2.30) This entrypoint resolves the clock EXACTLY as Stage 2 does
    # (`add_market_clock_cli_args` + `resolve_live_clock_context`) and declares its own
    # `--as-of-date`, so a pinned past session bypasses the run-window gate here too and a
    # full netted ticket still comes out. Without this the archived summary would look like a
    # same-day real run -- the precise failure mode L2.30 exists to prevent. Both helpers are
    # imported from `scripts.run_live_strategy` on purpose: there must be exactly ONE replay
    # predicate in the codebase, not a second copy that can drift.
    run_mode = resolve_run_mode(clock_ctx)
    announce_run_mode(clock_ctx, run_mode)
    if getattr(args, "start_date", None):
        start_date = args.start_date
    else:
        start_date = (pd.Timestamp(val_date) - timedelta(days=args.lookback_days)).date().isoformat()
    os.makedirs(args.output_dir, exist_ok=True)

    strategy_specs = parse_strategy_specs(args.strategies, args.config_file)
    print("=" * 115)
    print("MULTI-STRATEGY ORDER NETTING & VIRTUAL LEDGER RUNNER (多策略撮合净额化与内部对冲引擎)")
    print(f"调仓基准日期 (As-of-Date)   : {val_date}")
    print(f"数据回溯周期 (Lookback)     : {start_date} -> {val_date}")
    print(f"主账户总资产 (Portfolio NAV): ¥{args.portfolio_value:,.2f} RMB")
    print(f"股票池路径   (Universe)     : {args.universe_file}")
    print(f"数据源提供者 (Provider)     : {args.data_provider}")
    print(f"协同策略数量 (Strategies)   : {len(strategy_specs)} 个")
    for s in strategy_specs:
        print(f"  • {s['strategy_key']:<35} : 资金分配权重 {s['capital_weight']:>6.1%}")
    print("=" * 115)

    # 1. Load Universe symbols
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe from {args.universe_file}")
    symbol_names = load_symbol_names(args.universe_file)

    # 2. Load Raw Master Holdings
    raw_user_holdings = load_holdings(args)
    held_symbols = [
        s.strip() for s in (raw_user_holdings.keys() if raw_user_holdings else [])
        if s.strip() not in ("BIL", "CASH")
    ]
    all_symbols_to_load = sorted(list(set(universe_symbols).union(set(held_symbols))))

    cache_dir = args.cache_dir or shared_data_dir()
    universe, probe_attempts = load_live_universe(
        args,
        all_symbols_to_load,
        start_date,
        val_date,
        clock_ctx=clock_ctx,
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, val_date)
    freshness_summary = assert_fresh_universe(
        universe,
        val_date,
        strict=clock_ctx.freshness_strict,
        expected_symbols=all_symbols_to_load,
        exclude=("BIL", "CASH"),
        stage="multi_strategy_netted",
    )
    persist_run_metadata(
        args.output_dir,
        clock_ctx,
        freshness_summary=freshness_summary,
        probe_attempts=probe_attempts,
        extra={"date": val_date, "stage": "multi_strategy_netted", "run_mode": run_mode},
    )

    # (2026-10-22 incident, Fix 2 / L2.3) Netting prices come from the SAME single
    # freshness owner as Stage 2 (`common.freshness.latest_close`), not `.iloc[-1]`.
    latest_prices = {}
    for sym in all_symbols_to_load:
        if sym in universe and not universe[sym].empty:
            latest_prices[sym] = latest_close(
                universe, sym, val_date, strict=clock_ctx.freshness_strict
            )
        else:
            latest_prices[sym] = 1.0

    # 3. Evaluate each strategy to extract its rebalance targets
    intents: List[SubStrategyIntent] = []
    print("\n[STEP 1: 评估各子策略独立调仓意向 (Evaluating Sub-Strategy Targets)]")
    for s_spec in strategy_specs:
        s_key = s_spec["strategy_key"]
        w_strat = s_spec["capital_weight"]
        strat_def, actual_key, resolved_desc, template = resolve_strategy_definition(s_key, None)
        params = strat_def.get("params", {})

        # (P1.8 / P1.9) Pre-flight consistency check between dump and strategies_config.json.
        # The static-template exemption is owned by the verifier (`is_static_template_exempt`);
        # gating on the bare name here would mis-exempt a research dump keyed `dual_momentum`.
        verify_live_strategy_dump_preflight(
            strategy_def=strat_def,
            strategy_key=actual_key,
            strategy_file=resolved_desc,
            allow_override=getattr(args, "allow_weighting_mode_override", False),
        )
        strat_name = (
            strat_def.get("strategy_name")
            or strat_def.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
            or s_key.replace("_", " ").title()
        )

        sparse_weights = template.generate_weights(universe, params)
        rebalances = latest_rebalance_rows(sparse_weights)
        if rebalances.empty:
            print(f"  [WARN] Strategy {s_key} returned no rebalances. Defaulting to 0.")
            current_targets = pd.Series(0.0, index=all_symbols_to_load)
        else:
            current_targets = rebalances.iloc[-1].fillna(0.0)

        intent = SubStrategyIntent(
            strategy_key=s_key,
            strategy_name=strat_name,
            capital_weight=w_strat,
            allocated_capital=args.portfolio_value * w_strat,
            target_weights=current_targets.to_dict(),
        )
        intents.append(intent)
        print(f"  ✓ {strat_name} (权重: {w_strat:.1%}, 分配资金: ¥{intent.allocated_capital:,.2f}) 信号提取完毕")

    # 4. Resolve Master Account Holdings and Holding Bars
    master_holdings_dict: Dict[str, Any] = {}
    if raw_user_holdings:
        master_holdings_dict = dict(raw_user_holdings)
    else:
        master_holdings_dict = {"BIL": args.portfolio_value}

    prior_sub_ledgers = resolve_prior_sub_ledgers(
        val_date=val_date,
        output_dir=args.output_dir,
        sub_ledgers_file_arg=args.sub_ledgers_file,
    )

    holding_bars = resolve_holding_bars(
        raw_user_holdings or {},
        account_state=None,
        output_dir=args.output_dir,
        current_holdings_file=args.current_holdings_file,
        as_of_date=val_date,
        default_bars=args.min_holding_bars,
    )

    # 5. Run Netting Engine
    print("\n[STEP 2: 执行多策略撮合净额化与内部对冲 (Executing Order Netting)]")
    engine = OrderNettingEngine(
        lot_size_default=100,
        stamp_duty=0.0005,
        commission=0.0003,
        slippage=0.0005,
        cash_proxy="BIL",
    )

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=master_holdings_dict,
        portfolio_value=args.portfolio_value,
        latest_prices=latest_prices,
        as_of_date=val_date,
        holding_bars=holding_bars,
        min_holding_bars=args.min_holding_bars,
        inertia_threshold=args.inertia_threshold,
        symbol_names=symbol_names,
        prior_sub_ledgers=prior_sub_ledgers,
    )

    def curr_sym(m_or_c: str) -> str:
        if m_or_c in ("HONGKONG", "HKD"):
            return "HK$"
        if m_or_c in ("US", "USD"):
            return "$"
        return "¥"

    # 6. Display Internal Crossing Audit & Friction Savings
    fs = res.friction_savings
    print("\n" + "=" * 115)
    print("内部对冲与摩擦损耗节约审计报告 (INTERNAL CROSSING & FRICTION SAVINGS AUDIT)")
    print("=" * 115)
    print(f"多策略毛买卖股数 (Gross Shares) : {fs.total_gross_shares:>10,d} 股 (毛交易流水: ¥{fs.total_gross_turnover:>10,.2f})")
    print(f"主账户净买卖股数 (Net Shares)   : {fs.total_net_shares:>10,d} 股 (净交易流水: ¥{fs.total_net_turnover:>10,.2f})")
    print(f"内部对冲化解股数 (Crossed Shares: {fs.total_crossed_shares:>10,d} 股")
    print(f"无效换手削减比例 (Churn Reduction: {fs.churn_reduction_ratio:>10.2%}")
    print("-" * 115)
    print(f"  • 节约印花税及规费 (A股卖出0.05% / 港股双边0.1085%) : ¥{fs.saved_stamp_duty:>10,.2f} RMB")
    if fs.saved_sec_fee > 0:
        print(f"  • 节约美股监管规费 (SEC Section 31 0.00278%)        : ¥{fs.saved_sec_fee:>10,.2f} RMB")
    print(f"  • 节约双边券商佣金 (0.03% 券商佣金)                 : ¥{fs.saved_commission:>10,.2f} RMB")
    print(f"  • 节约双边冲击成本 (0.05% 买卖滑点)                 : ¥{fs.saved_slippage:>10,.2f} RMB")
    if fs.saved_by_market:
        m_parts = [f"{get_market_label(m)}: ¥{val:,.2f}" for m, val in fs.saved_by_market.items() if val > 0]
        if m_parts:
            print(f"  • 各市场节约分布   ({', '.join(m_parts)})")
    print(f"  ★ 合计为账户挽回摩擦损耗                           : ¥{fs.total_friction_saved:>10,.2f} RMB")
    print("=" * 115)

    # Display Active Crossing Symbols
    active_crossed = [c for c in res.crossed_details if c.crossed_shares > 0]
    if active_crossed:
        print("\n[高对冲标的明细表 (Internally Crossed Assets Breakdown)]")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'毛买股数':>9} {'毛卖股数':>9} {'对冲股数':>9} {'净执行股数':>10} {'节约金额':>10}")
        print("-" * 105)
        for c in active_crossed:
            cs = curr_sym(c.market)
            print(f"{c.symbol:<11} {get_market_label(c.market):<4} {c.lot_size:>5d} {pad_east_asian(c.name, 10)} {c.price:>8.2f} "
                  f"{c.gross_buy_shares:>9,d} {c.gross_sell_shares:>9,d} {c.crossed_shares:>9,d} {c.net_shares:>10,d} {cs}{c.saved_friction:>9,.2f}")
        print("-" * 105)

    # 7. Display Standard 4-Phase Netted Execution Ticket
    print("\n" + "=" * 115)
    print("主账户全局四阶段净额执行工单 (GLOBAL 4-PHASE NET EXECUTION TICKET)")
    print("=" * 115)

    df = res.net_orders_df
    sells = df[df["action"] == "SELL"]
    buys = df[df["action"] == "BUY"]
    holds = df[df["action"].str.startswith("HOLD")]
    cash = df[df["symbol"] == "BIL"]

    if not sells.empty:
        print("\n【阶段一：净卖出优先执行 (PHASE 1: NET SELLS FIRST - 释放购买力)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'当前股数':>9} {'目标股数':>9} {'净卖股数':>9} {'释放资金':>12} {'说明理由':<25}")
        print("-" * 115)
        for _, r in sells.iterrows():
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['current_shares']:>9,d} {r['target_shares']:>9,d} {abs(r['delta_shares']):>9,d} {cs}{r['trade_value']:>11,.2f} {r['reason']}")
    else:
        print("\n【阶段一：净卖出优先执行 (PHASE 1: NET SELLS FIRST)】: 今日无卖出指令 (No Sell Orders)")

    if not buys.empty:
        print("\n【阶段二：净买入规范执行 (PHASE 2: NET BUYS SECOND - 整手建仓)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'当前股数':>9} {'目标股数':>9} {'净买股数':>9} {'所需资金':>12} {'说明理由':<25}")
        print("-" * 115)
        for _, r in buys.iterrows():
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['current_shares']:>9,d} {r['target_shares']:>9,d} {r['delta_shares']:>9,d} {cs}{r['trade_value']:>11,.2f} {r['reason']}")
    else:
        print("\n【阶段二：净买入规范执行 (PHASE 2: NET BUYS SECOND)】: 今日无买入指令 (No Buy Orders)")

    if not holds.empty:
        print(f"\n【阶段三：持仓维持与锁仓 (PHASE 3: NET POSITIONS HELD - {len(holds)} 只标的)】")
        print(f"{'代码 (Symbol)':<11} {'市场':<4} {'一手':>5} {'名称 (Name)':<10} {'现价':>8} {'持有股数':>9} {'持仓市值':>12} {'当前权重':>8} {'目标权重':>8} {'说明状态':<25}")
        print("-" * 115)
        for _, r in holds.iterrows():
            mval = r['target_shares'] * r['price']
            cs = curr_sym(r.get("market", "CHINA"))
            print(f"{r['symbol']:<11} {get_market_label(r.get('market', 'CHINA')):<4} {r.get('lot_size', 100):>5d} {pad_east_asian(r['name'], 10)} {r['price']:>8.2f} "
                  f"{r['target_shares']:>9,d} {cs}{mval:>11,.2f} {r['current_weight']:>7.1%} {r['target_weight']:>7.1%} {r['reason']}")

    if not cash.empty:
        print("\n【阶段四：闲置资金现金管理 (PHASE 4: GLOBAL CASH SWEEP)】")
        r_cash = cash.iloc[0]
        print(f"  现金管理工具代码 : {r_cash['symbol']} (银华日利 / 国债逆回购 GC001 / 美元国债 BIL)")
        print(f"  执行后可用现金   : ¥{r_cash['target_shares']:>10,.2f} ({r_cash['target_weight']:>5.1%})")
        print(f"  调仓净发生变化   : ¥{r_cash['delta_shares']:>10,.2f}")
        print(f"  执行动作状态     : {r_cash['action']} ({r_cash['reason']})")
    print("=" * 115)

    # 8. Save Artifacts & Reports
    ticket_csv = os.path.join(args.output_dir, "netted_trading_ticket.csv")
    ticket_json = os.path.join(args.output_dir, "netted_trading_ticket.json")
    savings_json = os.path.join(args.output_dir, "friction_savings_report.json")
    ledgers_json = os.path.join(args.output_dir, "sub_ledgers.json")

    df.to_csv(ticket_csv, index=False, encoding="utf-8-sig")
    with open(ticket_json, "w", encoding="utf-8") as f:
        json.dump(res.net_orders, f, indent=2, ensure_ascii=False)
    with open(savings_json, "w", encoding="utf-8") as f:
        json.dump(res.to_dict()["friction_savings"], f, indent=2, ensure_ascii=False)
    with open(ledgers_json, "w", encoding="utf-8") as f:
        json.dump(res.sub_ledgers, f, indent=2, ensure_ascii=False)

    print(f"\n[✓] 净额化调仓票据已成功导出至:")
    print(f"    工单表格 (CSV)  : {ticket_csv}")
    print(f"    工单数据 (JSON) : {ticket_json}")
    print(f"    节约报告 (JSON) : {savings_json}")
    print(f"    虚拟账本 (JSON) : {ledgers_json}")


if __name__ == "__main__":
    main()

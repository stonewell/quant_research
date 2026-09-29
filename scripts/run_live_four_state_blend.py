#!/usr/bin/env python3
"""Operational Daily Live Execution Runner for Chan Four-State Risk-Managed Blend Strategy.

Loads the validated `chan_four_state_blend` strategy configuration, fetches point-in-time
market data up to `--as-of-date`, evaluates the 4-state FSM + 3-type + VAA composite logic,
applies the drawdown circuit breakers, 30% breadth / 10d thrust cash deployment, and 5% inertia filter,
and outputs a complete, actionable trading ticket (SELLS FIRST -> BUYS SECOND) with per-market lot rounding.

Usage:
    # 1. Run live check for today using the default Core-Satellite 22-stock universe:
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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import re
import unicodedata

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

def _resolve_path(p: Optional[str]) -> Optional[str]:
    """Resolve path relative to repo root if it does not exist relative to CWD."""
    if not p:
        return p
    if os.path.isabs(p) or os.path.exists(p):
        return os.path.abspath(p)
    candidate = os.path.join(_REPO_ROOT, p)
    if os.path.exists(candidate):
        return os.path.abspath(candidate)
    return p

from common.allocation_backtester import _get_hk_board_lot
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
DEFAULT_UNIVERSE_FILE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")
DEFAULT_OUTPUT_DIR = os.path.join(_REPO_ROOT, "docs", "ruleset", "chan_four_state_blend")


_FALLBACK_STOCK_NAMES: Dict[str, str] = {
    # 核心底仓 Core (A股央国企高股息红利蓝筹)
    "601872.SH": "招商轮船", "601728.SH": "中国电信", "601288.SH": "农业银行",
    "601225.SH": "陕西煤业", "601919.SH": "中远海控", "601111.SH": "中国国航",
    "601857.SH": "中国石油", "601601.SH": "中国太保", "600941.SH": "中国移动",
    "600028.SH": "中国石化", "601088.SH": "中国神华", "601398.SH": "工商银行",
    "600362.SH": "江西铜业", "600000.SH": "浦发银行", "000157.SZ": "中联重科",
    "601166.SH": "兴业银行", "601390.SH": "中国中铁",
    # 卫星增强 Satellite (A股科技/成长/产业Alpha领军)
    "300394.SZ": "天孚通信", "601899.SH": "紫金矿业", "600584.SH": "长电科技",
    "600660.SH": "福耀玻璃", "000938.SZ": "紫光股份", "002371.SZ": "北方华创",
    "688008.SH": "澜起科技", "603501.SH": "韦尔股份", "688012.SH": "中微公司",
    "688041.SH": "海光信息", "688981.SH": "中芯国际", "002156.SZ": "通富微电",
    "688072.SH": "拓荆科技", "688120.SH": "华海清科",
    # 港股核心/卫星 (HK Stocks)
    "0005.HK": "汇丰控股", "0386.HK": "中国石油化工", "0388.HK": "香港交易所",
    "0700.HK": "腾讯控股", "0857.HK": "中国石油股份", "0883.HK": "中国海洋石油",
    "0939.HK": "建设银行", "0941.HK": "中国移动", "0992.HK": "联想集团",
    "1299.HK": "友邦保险", "1398.HK": "工商银行", "1810.HK": "小米集团-W",
    "2318.HK": "中国平安", "2388.HK": "中银香港", "2800.HK": "盈富基金",
    "3690.HK": "美团-W", "3988.HK": "中国银行", "9988.HK": "阿里巴巴-W",
    # 美股核心/卫星 (US Stocks)
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌",
    "AMZN": "亚马逊", "META": "Meta", "TSLA": "特斯拉", "SPY": "标普500ETF",
    "QQQ": "纳指100ETF", "JNJ": "强生", "PG": "宝洁", "KO": "可口可乐",
    "JPM": "摩根大通", "XOM": "埃克森美孚", "CVX": "雪佛龙",
    # 现金与固收管理
    "BIL": "流动性现金", "SHV": "短期美债ETF", "511880.SH": "银华日利ETF", "511990.SH": "华宝添益ETF",
}


def pad_east_asian(s: str, width: int) -> str:
    """Pad string considering East Asian wide characters (each counting as 2 display columns)."""
    display_w = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
    padding = max(0, width - display_w)
    return s + " " * padding


def load_symbol_names(universe_file: Optional[str] = None) -> Dict[str, str]:
    """Extract symbol -> stock name mapping from universe file comments and fallback dict."""
    names = dict(_FALLBACK_STOCK_NAMES)
    if universe_file and os.path.exists(universe_file):
        try:
            last_comment = None
            with open(universe_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        if not line.startswith("# =") and not line.startswith("# -"):
                            m = re.match(r"^#\s*([^\s(（]+)", line)
                            if m:
                                last_comment = m.group(1).strip()
                    else:
                        sym = line.replace(",", " ").split()[0].upper()
                        if last_comment and len(last_comment) <= 12:
                            names[sym] = last_comment
                            last_comment = None
        except Exception:
            pass
    return names


def resolve_lot_size(sym: str, override_lot: Optional[int] = None) -> int:
    """Resolve trading lot size by symbol context (100 for A-shares, 1 for US, board lot for HK)."""
    if override_lot is not None:
        return override_lot
    clean = sym.strip()
    if clean.endswith(".HK") or (clean.isdigit() and len(clean) in (4, 5)):
        return _get_hk_board_lot(clean, default_lot=100)
    elif clean.endswith(".SH") or clean.endswith(".SZ"):
        return 100
    else:
        return 1


def parse_args(args: Optional[List[str]] = None):
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
        help="Path to universe symbols file (default: core_satellite_22_stocks.txt)",
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
        default=None,
        help="Total portfolio NAV in account currency / RMB (default: loaded from account-state-file or 100,000.0)",
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
        default=None,
        help="Trading lot size for buy order rounding (default: auto-detect 100 for A-shares, 1 for US, board lots for HK)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save generated trading orders CSV",
    )
    parser.add_argument(
        "--account-state-file",
        default=None,
        help="Path to persistent account state JSON file (default: account_state.json inside --output-dir)",
    )
    parser.add_argument(
        "--health-report-file",
        default=None,
        help="Path to Stage 1 health report JSON file (default: stage1_health_report.json inside --output-dir)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Folder path for DuckDB / parquet market data cache (default: data/ in repo root)",
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    parsed = parser.parse_args(args)

    parsed.output_dir = _resolve_path(parsed.output_dir) or parsed.output_dir
    if parsed.account_state_file is None:
        parsed.account_state_file = os.path.join(parsed.output_dir, "account_state.json")
    else:
        parsed.account_state_file = _resolve_path(parsed.account_state_file)
    if parsed.health_report_file is None:
        parsed.health_report_file = os.path.join(parsed.output_dir, "stage1_health_report.json")
    else:
        parsed.health_report_file = _resolve_path(parsed.health_report_file)
    parsed.strategy_file = _resolve_path(parsed.strategy_file)
    parsed.universe_file = _resolve_path(parsed.universe_file)
    if parsed.current_holdings_file:
        if not os.path.exists(parsed.current_holdings_file) and os.path.exists(os.path.join(parsed.output_dir, parsed.current_holdings_file)):
            parsed.current_holdings_file = os.path.join(parsed.output_dir, parsed.current_holdings_file)
        else:
            parsed.current_holdings_file = _resolve_path(parsed.current_holdings_file)
    if parsed.cache_dir:
        parsed.cache_dir = _resolve_path(parsed.cache_dir)
    return parsed


def load_holdings(args) -> Optional[Dict[str, Any]]:
    if args.current_holdings and args.current_holdings_file:
        raise ValueError("Specify either --current-holdings or --current-holdings-file, not both.")
    if args.current_holdings:
        return json.loads(args.current_holdings)
    if args.current_holdings_file and os.path.exists(args.current_holdings_file):
        with open(args.current_holdings_file, "r", encoding="utf-8") as f:
            return json.load(f)
    # Check default current_holdings.json in output_dir
    default_holdings = os.path.join(args.output_dir, "current_holdings.json")
    if os.path.exists(default_holdings):
        print(f"[INFO] 自动加载持仓配置文件 (Auto-loaded holdings from): {default_holdings}")
        with open(default_holdings, "r", encoding="utf-8") as f:
            return json.load(f)
    # Check if account_state_file exists and contains a holdings dictionary
    if args.account_state_file and os.path.exists(args.account_state_file):
        try:
            with open(args.account_state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
                if "holdings" in state_data and isinstance(state_data["holdings"], dict):
                    print(f"[INFO] 自动从账户状态加载持仓 (Auto-loaded holdings from state): {args.account_state_file}")
                    return state_data["holdings"]
        except Exception:
            pass
    return None


def resolve_holdings_weights_and_shares(
    raw_holdings: Optional[Dict[str, Any]],
    universe: Dict[str, pd.DataFrame],
    portfolio_value: float,
    cash_proxy: str = "BIL",
) -> Tuple[Dict[str, float], Dict[str, int], float, float]:
    """Resolve user-supplied holdings (either share quantities or fractional weights)
    into normalized portfolio weights and exact held share counts.

    Returns:
        (weights_dict, shares_dict, total_equity_val, cash_val)
    """
    if not raw_holdings:
        return {}, {}, 0.0, portfolio_value

    # Filter out 0 or null values
    cleaned = {k.strip(): float(v) for k, v in raw_holdings.items() if v is not None and float(v) > 0}
    if not cleaned:
        return {}, {}, 0.0, portfolio_value

    # Non-cash held assets
    non_cash_items = {k: v for k, v in cleaned.items() if k not in (cash_proxy, "BIL", "CASH")}

    # Detect weight mode: all non-cash values <= 1.0, sum <= 1.05, and has non-integer float
    is_weight_mode = False
    if non_cash_items:
        all_le_1 = all(v <= 1.0 for v in non_cash_items.values())
        sum_le_105 = sum(non_cash_items.values()) <= 1.05
        has_fraction = any(not float(v).is_integer() for v in non_cash_items.values())
        if all_le_1 and sum_le_105 and has_fraction:
            is_weight_mode = True

    weights_dict: Dict[str, float] = {}
    shares_dict: Dict[str, int] = {}
    total_equity_val = 0.0

    if is_weight_mode:
        # Legacy weight mode: user supplied fractional weights
        for sym, w in non_cash_items.items():
            weights_dict[sym] = float(w)
            price = float(universe[sym]["Close"].iloc[-1]) if (sym in universe and not universe[sym].empty) else 0.0
            eq_val = w * portfolio_value
            total_equity_val += eq_val
            shares_dict[sym] = int(np.round(eq_val / price)) if price > 0 else 0
        cash_val = max(0.0, portfolio_value - total_equity_val)
        weights_dict[cash_proxy] = max(0.0, cash_val / portfolio_value) if portfolio_value > 0 else 0.0
    else:
        # Quantity / Shares mode: user supplied share counts
        for sym, qty in non_cash_items.items():
            int_qty = int(np.round(qty))
            shares_dict[sym] = int_qty
            if sym in universe and not universe[sym].empty:
                price = float(universe[sym]["Close"].iloc[-1])
            else:
                price = 0.0
                print(f"[WARN] 无法获取标的 {sym} 的最新收盘价，持仓市值暂计为 0。")
            eq_val = int_qty * price
            total_equity_val += eq_val
            weights_dict[sym] = (eq_val / portfolio_value) if portfolio_value > 0 else 0.0

        cash_val = portfolio_value - total_equity_val
        if cash_val < -1e-4:
            print(f"[WARN] 股票总持仓市值 (¥{total_equity_val:,.2f}) 超过账户总资产 (¥{portfolio_value:,.2f})，推算现金比例置为 0。")
            cash_val = 0.0
            cash_w = 0.0
        else:
            cash_w = cash_val / portfolio_value if portfolio_value > 0 else 0.0

        weights_dict[cash_proxy] = cash_w

    return weights_dict, shares_dict, total_equity_val, cash_val


def main():
    args = parse_args()
    as_of_date = args.as_of_date or date.today().isoformat()
    start_date = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    # Load account state (if available) for NAV or circuit breakers
    account_state = {}
    if args.account_state_file and os.path.exists(args.account_state_file):
        try:
            with open(args.account_state_file, "r", encoding="utf-8") as f:
                account_state = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to read account state from {args.account_state_file}: {e}")

    # Resolve portfolio NAV: priority CLI > account_state.json current_nav > default 100,000
    if args.portfolio_value is None:
        if account_state.get("current_nav") is not None:
            args.portfolio_value = float(account_state["current_nav"])
            print(f"[INFO] 自动读取账户净值 (Loaded current NAV from state): ¥{args.portfolio_value:,.2f}")
        else:
            args.portfolio_value = 100000.0

    print("=" * 110)
    print("CHAN FOUR-STATE RISK-MANAGED BLEND STRATEGY - DAILY LIVE EXECUTION RUNNER")
    print("禅论四状态风险管理混合策略 - 每日实盘调仓指令生成器")
    print("=" * 110)
    print(f"策略配置 (Config)   : {args.strategy_file}")
    print(f"标的资产 (Universe) : {args.universe_file}")
    print(f"数据周期 (Window)   : {start_date} -> {as_of_date} (最新估值日: {as_of_date})")
    print(f"账户资金 (NAV)      : {args.portfolio_value:,.2f} RMB")
    print(f"行情数据 (Provider) : {args.data_provider}")

    # 1. Load Strategy Template
    if not os.path.exists(args.strategy_file):
        raise FileNotFoundError(f"Strategy file not found at: {args.strategy_file}")
    strategy_def = load_strategy_file(args.strategy_file)
    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    cash_proxy = params.get("cash_proxy", "BIL")
    template = get_template(
        template_name,
        strategy_def.get("pattern_spec"),
        strategy_def.get("research_strategy_spec"),
        strategy_def.get("composite_spec"),
        params,
    )

    # 2. Load Universe (including any held symbols outside the universe list)
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")
    symbol_names = load_symbol_names(args.universe_file)

    raw_user_holdings = load_holdings(args)
    held_symbols = [s.strip() for s in (raw_user_holdings.keys() if raw_user_holdings else []) if s.strip() not in (cash_proxy, "BIL", "CASH")]
    symbols_to_load = sorted(list(set(universe_symbols).union(set(held_symbols))))

    cache_dir = args.cache_dir or shared_data_dir()
    universe = load_universe_with_banner(
        symbols_to_load,
        start_date,
        as_of_date,
        interval="1d",
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
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

    # Check circuit breaker from account_state
    circuit_tier = account_state.get("circuit_breaker_tier")
    freeze_rem = int(account_state.get("freeze_remaining_bars", 0))
    if circuit_tier == "TIER_3_HALT" or freeze_rem > 0:
        print("\n" + "!" * 110)
        print(f"[CIRCUIT BREAKER ENGAGED / 熔断器生效] 账户处于第三级熔断停牌期 (Tier 3 Emergency Halt, 剩余冷冻: {freeze_rem} 交易日)")
        print("根据风控守则，所有风险标的持仓清零，全额切换为现金停泊 (100% Cash Sweep).")
        print("!" * 110 + "\n")
        current_targets = pd.Series(0.0, index=current_targets.index)
    elif account_state.get("linear_equity_scale") is not None:
        live_scale = float(account_state["linear_equity_scale"])
        if live_scale < 0.999:
            print(f"[RISK DAMPING / 风险阻尼] 根据账户实盘回撤，线性压缩股票仓位比例至: {live_scale:.1%}")
            current_targets = current_targets * live_scale

    # 4. Resolve Reference Portfolio (Actual Holdings vs Strategy Last Rebalance)
    current_shares_dict: Dict[str, int] = {}
    if raw_user_holdings is not None:
        reference_weights, current_shares_dict, total_eq_val, cash_bal_val = resolve_holdings_weights_and_shares(
            raw_user_holdings, universe, args.portfolio_value, cash_proxy
        )
        reference = pd.Series(reference_weights)
        reference_source = "User-Supplied Actual Live Brokerage Holdings"
        print("\n" + "-" * 110)
        print("[持仓资产与闲置资金对账 (Holdings & Cash Reconciliation)]")
        print(f"  当前股票持仓市值 : ¥{total_eq_val:>10,.2f} ({total_eq_val/args.portfolio_value:>5.1%})")
        print(f"  推算账户闲置现金 : ¥{cash_bal_val:>10,.2f} ({cash_bal_val/args.portfolio_value:>5.1%})")
        print(f"  账户总资产估值   : ¥{args.portfolio_value:>10,.2f}")
        print("-" * 110)
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

    # Compute implied cash weight: strategy only outputs equity weights, remainder is cash
    equity_weight_sum = sum(float(target_series[s]) for s in target_series.index if s != cash_proxy)
    implied_cash_weight = max(0.0, 1.0 - equity_weight_sum)
    if cash_proxy not in target_series.index:
        target_series = pd.concat([target_series, pd.Series({cash_proxy: implied_cash_weight})])
        current_series = pd.concat([current_series, pd.Series({cash_proxy: current_series.get(cash_proxy, 0.0)})])
        all_symbols = sorted(list(set(target_series.index).union(set(current_series.index))))
    else:
        target_series[cash_proxy] = implied_cash_weight

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
        elif cur_w == 0.0 and tgt_w == 0.0:
            action = "NO_POSITION"   # not held, not targeted — skip entirely
        elif cur_w == 0.0 and tgt_w > 0.0:
            action = "BUY"           # fresh entry from zero — always a BUY, no inertia filter
        elif abs_delta < min_trade_thresh:
            action = "HOLD_FILTERED"  # user holds and change is below inertia threshold
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
        sym_lot = resolve_lot_size(sym, args.lot_size)
        current_shares = current_shares_dict.get(sym, int(np.round(current_val / price))) if (price > 0 and current_val > 0) else current_shares_dict.get(sym, 0)
        if sym != cash_proxy and price > 0:
            if action == "BUY":
                # Compute buy qty from trade delta value, round down to lot size
                raw_buy_shares = trade_val / price
                lot_buy_shares = int(np.floor(raw_buy_shares / sym_lot) * sym_lot)
                delta_shares = max(0, lot_buy_shares)
                target_shares = current_shares + delta_shares
                actual_trade_val = delta_shares * price
            elif action == "SELL":
                if tgt_w <= 1e-6:
                    # Full liquidation: sell 100% of currently held shares
                    delta_shares = -current_shares
                    target_shares = 0
                    actual_trade_val = delta_shares * price
                else:
                    # Partial reduction: round up to lot size, capped at current_shares
                    raw_sell_shares = abs(trade_val) / price
                    lot_sell_shares = int(np.ceil(raw_sell_shares / sym_lot) * sym_lot)
                    lot_sell_shares = min(lot_sell_shares, current_shares)
                    delta_shares = -lot_sell_shares
                    target_shares = current_shares + delta_shares
                    actual_trade_val = delta_shares * price
            else:
                target_shares = current_shares
                delta_shares = 0
                actual_trade_val = 0.0
        else:
            target_shares = 0
            current_shares = 0
            delta_shares = 0
            actual_trade_val = 0.0

        sym_name = symbol_names.get(sym, sym)
        rows.append({
            "symbol": sym,
            "name": sym_name,
            "action": action,
            "price": price,
            "current_weight": cur_w,
            "target_weight": tgt_w,
            "delta_weight": delta_w,
            "current_shares": current_shares,
            "target_shares": target_shares,
            "delta_shares": delta_shares,
            "trade_value": actual_trade_val,
            "trade_value_rmb": actual_trade_val,
        })

    order_df = pd.DataFrame(rows)

    # Reconcile cash proxy: actual post-trade cash = initial cash - net equity purchases
    # This accounts for lot rounding residuals and any undeployable (0-share) allocations.
    cash_mask = order_df["symbol"] == cash_proxy
    if cash_mask.any():
        equity_trades_val = order_df.loc[~cash_mask, "trade_value"].sum()
        initial_cash_val = current_series.get(cash_proxy, 0.0) * args.portfolio_value
        actual_post_trade_cash = initial_cash_val - equity_trades_val
        actual_cash_weight = max(0.0, actual_post_trade_cash / args.portfolio_value)
        order_df.loc[cash_mask, "target_weight"] = actual_cash_weight
        order_df.loc[cash_mask, "delta_weight"] = actual_cash_weight - current_series.get(cash_proxy, 0.0)
        order_df.loc[cash_mask, "trade_value"] = -equity_trades_val
        order_df.loc[cash_mask, "trade_value_rmb"] = -equity_trades_val

    # Sort: SELLS FIRST -> BUYS SECOND -> HOLDS; drop NO_POSITION rows (0% on both sides)
    order_df = order_df[order_df["action"] != "NO_POSITION"]

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

    print(f"\n调仓基准日期 (Rebalance Date): {latest_rebal_date}")
    print(f"持仓对照基准 (Reference Basis): {reference_source}")
    print("-" * 110)

    # Display SELLS FIRST
    sells = order_df[order_df["action"] == "SELL"]
    buys = order_df[order_df["action"] == "BUY"]
    holds = order_df[order_df["action"].str.startswith("HOLD")]
    cash_rows = order_df[order_df["action"].str.contains("CASH")]

    print("\n[PHASE 1: EXECUTE SELLS FIRST (第一阶段: 卖出平仓/减仓释放资金)]")
    if sells.empty:
        print("  今日无需执行任何卖出指令 (No sell orders required today).")
    else:
        for _, r in sells.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            print(f"  🔴 SELL {sym_col} | 现价: {r['price']:>7.2f} | 仓位: {r['current_weight']:>5.1%} -> 目标: {r['target_weight']:>5.1%} ({r['delta_weight']:>+5.1%}) | 卖出: {abs(r['delta_shares']):>6} 股 (~¥{abs(r['trade_value']):>9,.2f})")

    print("\n[PHASE 2: EXECUTE BUYS SECOND (第二阶段: 买入建仓/加仓资金配置)]")
    if buys.empty:
        print("  今日无需执行任何买入指令 (No buy orders required today).")
    else:
        for _, r in buys.iterrows():
            qty_str = f"{r['delta_shares']:>6}"
            suffix = ""
            if r['delta_shares'] == 0:
                sym_lot = resolve_lot_size(r['symbol'], args.lot_size)
                raw_shares = (r['delta_weight'] * args.portfolio_value) / r['price'] if r['price'] > 0 else 0
                qty_str = f"{'0':>6}"
                suffix = f" ⚠️  低于单手门槛 (不足{sym_lot}股) — 目标需{raw_shares:.0f}股, 1手需¥{sym_lot * r['price']:,.0f}"
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            print(f"  🟢 BUY  {sym_col} | 现价: {r['price']:>7.2f} | 仓位: {r['current_weight']:>5.1%} -> 目标: {r['target_weight']:>5.1%} ({r['delta_weight']:>+5.1%}) | 买入: {qty_str} 股 (~¥{r['trade_value']:>9,.2f}){suffix}")

    # Phase 3: only show positions the user actually holds (cur_w > 0) or that have a real target weight
    actual_holds = holds[(holds["current_weight"] > 0) | (holds["target_weight"] > 0)]
    print(f"\n[PHASE 3: POSITIONS HELD (第三阶段: 维持既有持仓/惰性过滤 < {min_trade_thresh:.0%})]")
    if actual_holds.empty:
        print("  今日无既有持仓需要维持。")
    else:
        for _, r in actual_holds.iterrows():
            reason = f"调仓未超惰性阈值 (< {min_trade_thresh:.0%})" if r["action"] == "HOLD_FILTERED" else "最优目标权重维持"
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            shares_info = f"({int(r['current_shares'])} 股)" if r['current_shares'] > 0 else ""
            print(f"  ⚪ HOLD {sym_col} | 维持仓位: {r['target_weight']:>5.1%} {shares_info:<10} | 持仓市值: ~¥{r['target_weight']*args.portfolio_value:>9,.2f} | ({reason})")

    if not cash_rows.empty:
        print("\n[PHASE 4: CASH PROXY & LIQUIDITY MANAGEMENT (第四阶段: 闲置资金对账)]")
        for _, r in cash_rows.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            print(f"  💰 CASH {sym_col} | 闲置现金: {r['target_weight']:>5.1%} (~¥{r['target_weight']*args.portfolio_value:>9,.2f}) | 停泊于 {r['symbol']} / 511880 / 国债逆回购")

    # Save to CSV
    os.makedirs(args.output_dir, exist_ok=True)
    out_csv = os.path.join(args.output_dir, "live_trading_ticket.csv")
    order_df.to_csv(out_csv, index=False)
    print("\n" + "=" * 110)
    print(f"[SUCCESS] 交易指令单已导出至: {out_csv}")
    print("=" * 110)


if __name__ == "__main__":
    main()

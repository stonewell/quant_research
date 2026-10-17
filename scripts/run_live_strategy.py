#!/usr/bin/env python3
"""Universal Operational Daily Live Execution Runner for Quantitative Trading Strategies.

Supports all strategies in the quantitative workspace:
  - 55+ research strategies (Chan blend models, Chan structural timing, Turtle, Bollinger, WorldQuant, etc.)
  - 9 static allocation templates (equal_weight, hierarchical_risk_parity, momentum, etc.)
  - Composite aspect hybrids and mined pattern models
  - Custom strategy configuration JSON files

Loads the strategy specification, fetches point-in-time market data up to `--as-of-date`,
evaluates strategy logic and portfolio weights, checks account state / circuit breakers,
applies turnover inertia filtering and liquidity constraints, and outputs a complete,
actionable 4-phase trading ticket:
  Phase 1: SELLS FIRST (releases purchasing power)
  Phase 2: BUYS SECOND (lot-rounded entries with minimum lot alerts)
  Phase 3: POSITIONS HELD (inertia-filtered holds)
  Phase 4: CASH PROXY & LIQUIDITY MANAGEMENT (idle cash sweep into BIL / 511880 / GC001)

Usage:
    # 1. Run live check with default strategy (chan_four_state_blend) and synthetic data:
    uv run python scripts/run_live_strategy.py --data-provider synthetic

    # 2. Run with any research strategy by name / key:
    uv run python scripts/run_live_strategy.py --strategy turtle_breakout --data-provider synthetic
    uv run python scripts/run_live_strategy.py --strategy chan_dual_hybrid_blend --data-provider synthetic

    # 3. Run with a static allocation template:
    uv run python scripts/run_live_strategy.py --strategy equal_weight --data-provider synthetic
    uv run python scripts/run_live_strategy.py --strategy hierarchical_risk_parity --data-provider synthetic

    # 4. Run with a specific strategy JSON file:
    uv run python scripts/run_live_strategy.py \
        --strategy-file pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json \
        --data-provider marketdb --as-of-date 2025-08-22

    # 5. Rebalance against actual live brokerage holdings with 100,000 RMB equity:
    uv run python scripts/run_live_strategy.py \
        --strategy chan_four_state_blend \
        --current-holdings '{"300394.SZ": 0.15, "000938.SZ": 0.10, "BIL": 0.75}' \
        --portfolio-value 100000
"""

import argparse
import json
import os
import re
import sys
import unicodedata
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
from common.strategy_spec import _STATIC_TEMPLATES, get_template, load_strategy_file
from common.universe import (
    FALLBACK_STOCK_NAMES,
    get_stock_name,
    is_placeholder_stock_name,
    resolve_universe_from_args,
)
from pipeline.live_signal.lsig.signal import as_of_universe, latest_rebalance_rows

DEFAULT_STRATEGY_KEY = "chan_four_state_blend"
DEFAULT_STRATEGY_FILE = os.path.join(
    _REPO_ROOT,
    "pipeline",
    "research_strategy",
    "results",
    "strategy_dumps",
    "chan_four_state_blend_strategy.json",
)
DEFAULT_UNIVERSE_FILE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")
DEFAULT_OUTPUT_DIR = os.path.join(_REPO_ROOT, "docs", "ruleset", DEFAULT_STRATEGY_KEY)

_FALLBACK_STOCK_NAMES: Dict[str, str] = dict(FALLBACK_STOCK_NAMES)

_INVALID_STOCK_NAME_WORDS = {
    "custom", "universe", "generated", "portfolio", "strategy", "benchmark",
    "index", "note", "notes", "warning", "attention", "header", "date",
    "time", "stocks", "stock", "tickers", "ticker", "asset", "assets",
    "selection", "selected", "source", "core", "satellite", "total", "temp",
    "run", "runs", "phase", "config", "none", "unknown", "null", "undefined",
    "china", "us", "hk", "hongkong", "america", "global", "etf", "etfs",
    "架构定位", "定量来源", "沪深", "优质", "股票池", "精选", "核心底仓", "卫星增强",
}


def _clean_stock_name_candidate(cand: str) -> Optional[str]:
    """Validate and clean a candidate stock name string, returning None if invalid."""
    if not cand:
        return None
    cand = cand.strip().strip(":,;：，；")
    if not cand or len(cand) > 12:
        return None
    cand_lower = cand.lower()
    if cand_lower in _INVALID_STOCK_NAME_WORDS:
        return None
    for bad_w in ("generated", "universe", "copyright", "license", "table", "strategy", "portfolio", "custom"):
        if bad_w in cand_lower:
            return None
    return cand


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
                        last_comment = None
                        continue
                    if line.startswith("#"):
                        after_hash = line.lstrip("#").strip()
                        if not after_hash or after_hash[0] in "=*-~#/+_[]{}:!>@$%^&":
                            last_comment = None
                            continue
                        if "(" in after_hash or "（" in after_hash:
                            raw_cand = after_hash.split("(")[0].split("（")[0].strip()
                        else:
                            words = after_hash.split()
                            raw_cand = words[0] if words else None
                        if raw_cand:
                            cleaned = _clean_stock_name_candidate(raw_cand)
                            last_comment = cleaned
                        else:
                            last_comment = None
                    else:
                        inline_name = None
                        if "#" in line:
                            parts = line.split("#", 1)
                            sym = parts[0].replace(",", " ").split()[0].upper() if parts[0].strip() else ""
                            raw_inline = parts[1].strip().split("(")[0].split("（")[0].strip()
                            inline_name = _clean_stock_name_candidate(raw_inline)
                        else:
                            sym = line.replace(",", " ").split()[0].upper()

                        cand_name = inline_name or last_comment
                        if cand_name and sym:
                            existing = _FALLBACK_STOCK_NAMES.get(sym)
                            if not existing or any(ord(c) > 127 for c in cand_name):
                                names[sym] = cand_name
                        last_comment = None
        except Exception:
            pass
    # Final sanity pass: ensure no symbol gets placeholder or invalid names
    for s, n in list(names.items()):
        if is_placeholder_stock_name(n, s):
            names[s] = _FALLBACK_STOCK_NAMES.get(s, s)
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


def resolve_blend_param(
    params: Dict[str, Any],
    name: str,
    default: Any = None,
    param_type: Optional[type] = None,
) -> Any:
    """Resolve configuration parameter across blend strategy prefix variants:
    cfsb_*, crb_*, ccsb_*, cfssb_*, cdhb_*, blend_*, or direct un-prefixed name.
    """
    for prefix in ("cfsb", "crb", "ccsb", "cfssb", "cdhb", "blend"):
        k = f"{prefix}_{name}"
        if k in params:
            val = params[k]
            return param_type(val) if param_type is not None and val is not None else val
    if name in params:
        val = params[name]
        return param_type(val) if param_type is not None and val is not None else val
    return default


def resolve_trade_inertia_threshold(params: Dict[str, Any], default: float = 0.05) -> float:
    """Universal trade inertia / turnover threshold resolver across all 55+ strategies.

    Checks:
    1. Direct keys: 'min_weight_change', 'inertia_threshold'
    2. Strategy-specific prefixes ending with '_min_weight_change' or '_min_weight_pct'
       (e.g., cfsb_min_weight_change, turtle_min_weight_change, chan3_min_weight_change,
       wq_min_weight_change, aaa_min_weight_pct, bb_min_weight_change, ms_min_weight_change)
    3. Fallback default (0.05).
    """
    if "min_weight_change" in params and params["min_weight_change"] is not None:
        return float(params["min_weight_change"])
    if "inertia_threshold" in params and params["inertia_threshold"] is not None:
        return float(params["inertia_threshold"])
    for k, v in params.items():
        if (k.endswith("_min_weight_change") or k.endswith("_min_weight_pct")) and v is not None:
            return float(v)
    return float(default)


def resolve_min_entry_bars(
    params: Dict[str, Any],
    cli_override: Optional[int] = None,
    default: int = 2,
) -> int:
    """Resolve minimum entry confirmation bars threshold across all strategies.

    Precedence:
    1. Direct CLI override (--min-entry-bars)
    2. Strategy parameter via resolve_blend_param (cfsb_min_entry_bars, cfssb_min_entry_bars, etc., or min_entry_bars)
    3. Strategy parameter matching ending with '_min_entry_bars'
    4. Fallback default (2).
    """
    if cli_override is not None:
        return max(1, int(cli_override))
    val = resolve_blend_param(params, "min_entry_bars", None, int)
    if val is not None:
        return max(1, int(val))
    for k, v in params.items():
        if k.endswith("_min_entry_bars") and v is not None:
            return max(1, int(v))
    return int(default)


def resolve_min_holding_bars(
    params: Dict[str, Any],
    cli_override: Optional[int] = None,
    default: int = 2,
) -> int:
    """Resolve minimum continuous holding bars/tenure threshold across all strategies.

    Precedence:
    1. Direct CLI override (--min-holding-bars)
    2. Strategy parameter via resolve_blend_param (cfsb_min_holding_bars, crb_min_holding_bars, etc., or min_holding_bars)
    3. Strategy parameter matching ending with '_min_holding_bars'
    4. Fallback default (2).
    """
    if cli_override is not None:
        return max(0, int(cli_override))
    val = resolve_blend_param(params, "min_holding_bars", None, int)
    if val is not None:
        return max(0, int(val))
    for k, v in params.items():
        if k.endswith("_min_holding_bars") and v is not None:
            return max(0, int(v))
    return int(default)


def resolve_holding_bars(
    current_holdings: Dict[str, Any],
    account_state: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    current_holdings_file: Optional[str] = None,
    as_of_date: Optional[str] = None,
    default_bars: int = 1,
) -> Dict[str, int]:
    """Resolves continuous holding bars/tenure for currently held assets.

    Resolution sources (in priority order):
    1. account_state.json's 'holding_bars' dictionary (if available).
    2. Historical run folders in the parent directory of output_dir / current_holdings_file:
       Walks backwards across previous date folders (YYYY-MM-DD), inspecting
       live_trading_ticket.csv, post_holdings.json, or holdings.json to count
       consecutive holding periods and identify the entry trade date.
    3. Fallback: default_bars (defaults to 1).
    """
    holding_bars: Dict[str, int] = {}
    cand_parents = []
    if output_dir:
        cand_parents.append(os.path.dirname(os.path.abspath(output_dir)))
        cand_parents.append(os.path.abspath(output_dir))
    if current_holdings_file:
        cand_parents.append(os.path.dirname(os.path.abspath(current_holdings_file)))
        cand_parents.append(os.path.dirname(os.path.dirname(os.path.abspath(current_holdings_file))))

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cur_date = as_of_date or (
        os.path.basename(output_dir)
        if output_dir and date_re.match(os.path.basename(output_dir))
        else "9999-99-99"
    )

    parent_dir = None
    prior_dates: List[str] = []
    for p in cand_parents:
        if os.path.isdir(p):
            sub_dates = sorted([d for d in os.listdir(p) if date_re.match(d) and d < cur_date], reverse=True)
            if sub_dates:
                parent_dir = p
                prior_dates = sub_dates
                break

    for sym in current_holdings:
        if current_holdings[sym] is None:
            continue
        try:
            qty_or_w = float(current_holdings[sym])
        except (ValueError, TypeError):
            continue
        if qty_or_w <= 0:
            continue
        clean_sym = str(sym).strip()

        age = 0
        found_in_history = False
        if parent_dir and prior_dates:
            for d in prior_dates:
                d_dir = os.path.join(parent_dir, d)
                d_ticket = os.path.join(d_dir, "live_trading_ticket.csv")
                d_post = os.path.join(d_dir, "post_holdings.json")
                d_hold = os.path.join(d_dir, "holdings.json")

                held_on_d = False
                is_entry_on_d = False
                if os.path.exists(d_ticket):
                    try:
                        df = pd.read_csv(d_ticket)
                        row = df[df["symbol"] == clean_sym]
                        if not row.empty:
                            r = row.iloc[0]
                            tgt_sh = float(r.get("target_shares", 0))
                            tgt_w = float(r.get("target_weight", 0))
                            cur_sh = float(r.get("current_shares", 0))
                            act = str(r.get("action", ""))
                            if tgt_sh > 0 or tgt_w > 0:
                                held_on_d = True
                                if act == "BUY" and cur_sh == 0:
                                    is_entry_on_d = True
                    except Exception:
                        pass
                elif os.path.exists(d_post):
                    try:
                        with open(d_post, "r", encoding="utf-8") as pf:
                            ph = json.load(pf)
                            held_on_d = float(ph.get(clean_sym, 0)) > 0
                    except Exception:
                        pass
                elif os.path.exists(d_hold):
                    try:
                        with open(d_hold, "r", encoding="utf-8") as hf:
                            hh = json.load(hf)
                            held_on_d = float(hh.get(clean_sym, 0)) > 0
                    except Exception:
                        pass

                if held_on_d:
                    age += 1
                    found_in_history = True
                    if is_entry_on_d:
                        break  # entered on date d, stop counting backwards
                else:
                    break  # continuity broken

        acc_bars = None
        if account_state and isinstance(account_state, dict):
            # If account_state was already updated today, its top-level holding_bars
            # contains today's post-trade targets (with fresh BUYs reset to 0).
            # We must resolve against the pre-trade baseline entering today (prior_state).
            if account_state.get("as_of_date") == cur_date and isinstance(account_state.get("prior_state"), dict):
                prior_hb = account_state["prior_state"].get("holding_bars")
                if isinstance(prior_hb, dict):
                    acc_bars = prior_hb
            elif account_state.get("as_of_date") != cur_date:
                hb = account_state.get("holding_bars")
                if isinstance(hb, dict):
                    acc_bars = hb

        if found_in_history:
            holding_bars[clean_sym] = age
        elif acc_bars is not None and clean_sym in acc_bars:
            try:
                holding_bars[clean_sym] = int(acc_bars[clean_sym])
            except (ValueError, TypeError):
                holding_bars[clean_sym] = default_bars
        else:
            holding_bars[clean_sym] = default_bars

    return holding_bars


_STATIC_NAME_ALIASES = {
    "risk_parity": "hierarchical_risk_parity",
    "hrp": "hierarchical_risk_parity",
    "momentum": "cross_sectional_momentum",
    "csm": "cross_sectional_momentum",
    "ew": "equal_weight",
    "inv_vol": "inverse_volatility",
    "min_var": "minimum_variance",
    "max_div": "max_diversification",
}


def resolve_strategy_definition(
    strategy_arg: Optional[str] = None,
    strategy_file_arg: Optional[str] = None,
) -> Tuple[Dict[str, Any], str, str, Any]:
    """Universal resolver for quantitative strategy specifications.

    Accepts:
    1. Direct file path to strategy.json
    2. Strategy key from strategy_dumps/ (e.g. chan_four_state_blend, turtle_breakout)
    3. Strategy key from strategies_config.json (all 55 research strategies)
    4. Static allocation template name or alias (equal_weight, hierarchical_risk_parity, etc.)
    5. Fallback default (chan_four_state_blend)

    Returns:
        (strategy_def_dict, strategy_key, resolved_description, template_instance)
    """
    target = strategy_file_arg or strategy_arg or DEFAULT_STRATEGY_FILE
    dumps_dir = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps")

    resolved_path = _resolve_path(target)
    strategy_def: Optional[Dict[str, Any]] = None
    strategy_key: str = ""
    resolved_desc: str = ""

    # Case 1: Target is an existing file on disk
    if resolved_path and os.path.isfile(resolved_path):
        strategy_def = load_strategy_file(resolved_path)
        strategy_key = (
            strategy_def.get("research_strategy_spec", {}).get("strategy_key")
            or strategy_def.get("template_name", "custom_strategy")
        )
        resolved_desc = os.path.relpath(resolved_path, _REPO_ROOT) if resolved_path.startswith(_REPO_ROOT) else resolved_path

    # Case 2: Target is a strategy dump in pipeline/research_strategy/results/strategy_dumps/
    if strategy_def is None and os.path.isdir(dumps_dir):
        clean_target = target.strip()
        candidates = [
            clean_target,
            f"{clean_target}.json",
            f"{clean_target}_strategy.json",
        ]
        for cand in candidates:
            cand_path = os.path.join(dumps_dir, cand)
            if os.path.isfile(cand_path):
                strategy_def = load_strategy_file(cand_path)
                strategy_key = (
                    strategy_def.get("research_strategy_spec", {}).get("strategy_key")
                    or strategy_def.get("template_name")
                    or clean_target
                )
                resolved_desc = os.path.relpath(cand_path, _REPO_ROOT)
                break

    # Case 3: Target is in pipeline/research_strategy/strategies_config.json
    if strategy_def is None:
        config_file = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json")
        if os.path.isfile(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                clean_key = target.strip()
                if clean_key in cfg_data:
                    entry = cfg_data[clean_key]
                    strategy_key = clean_key
                    strategy_def = {
                        "template_name": clean_key,
                        "strategy_name": entry.get("name", clean_key.replace("_", " ").title()),
                        "params": entry.get("parameters", {}),
                        "explanation": entry.get("description", ""),
                        "research_strategy_spec": {
                            "strategy_key": clean_key,
                            "entry_data": entry,
                        },
                    }
                    resolved_desc = f"strategies_config.json:{clean_key}"
            except Exception:
                pass

    # Case 4: Target is a static allocation template (or alias)
    if strategy_def is None:
        clean_target = target.strip().lower()
        static_name = _STATIC_NAME_ALIASES.get(clean_target, clean_target)
        if static_name in _STATIC_TEMPLATES:
            strategy_key = static_name
            strategy_def = {
                "template_name": static_name,
                "strategy_name": static_name.replace("_", " ").title(),
                "params": {},
                "explanation": f"Static basket allocation template: {static_name}",
            }
            resolved_desc = f"static_template:{static_name}"

    if strategy_def is None:
        raise ValueError(
            f"Could not resolve strategy '{target}'. Please specify an existing strategy.json path, "
            f"a research strategy key (e.g. chan_four_state_blend, turtle_breakout), "
            f"or a static template name (e.g. equal_weight, hierarchical_risk_parity)."
        )

    template_name = strategy_def["template_name"]
    params = strategy_def.get("params", {})

    # Reconstruct the template instance
    template = get_template(
        template_name=template_name,
        pattern_spec=strategy_def.get("pattern_spec"),
        research_strategy_spec=strategy_def.get("research_strategy_spec"),
        composite_spec=strategy_def.get("composite_spec"),
        params=params,
        fundamental_spec=strategy_def.get("fundamental_spec"),
        bnn_spec=strategy_def.get("bnn_spec"),
    )

    # For static templates or templates with a param_grid, supply default params if missing
    if hasattr(template, "param_grid") and isinstance(template.param_grid, dict):
        for k, v in template.param_grid.items():
            if k not in params and isinstance(v, (list, tuple)) and len(v) > 0:
                params[k] = v[0]
        strategy_def["params"] = params

    return strategy_def, strategy_key, resolved_desc, template


def parse_args(args: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        description="Universal Operational Live Trading Execution Runner (All Strategies)"
    )
    parser.add_argument(
        "--strategy",
        "-s",
        default=None,
        help="Strategy key, name, or path to strategy.json (e.g. chan_four_state_blend, turtle_breakout, equal_weight)",
    )
    parser.add_argument(
        "--strategy-file",
        default=None,
        help="Path to strategy.json (alternative to --strategy)",
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
        help="JSON string of current portfolio holdings: '{\"SYMBOL\": weight_or_shares, ...}'",
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
        "--min-entry-bars",
        type=int,
        default=None,
        help="Minimum continuous signal confirmation bars before initial entry (default: resolved from strategy config or 2)",
    )
    parser.add_argument(
        "--min-holding-bars",
        type=int,
        default=None,
        help="Minimum continuous holding bars before exit/liquidation is permitted (default: resolved from strategy config or 2)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to save generated trading orders CSV (default: docs/ruleset/<strategy_key>)",
    )
    parser.add_argument(
        "--account-state-file",
        default=None,
        help="Path to persistent account state JSON file (default: account_state.json inside output-dir)",
    )
    parser.add_argument(
        "--health-report-file",
        default=None,
        help="Path to Stage 1 health report JSON file (default: stage1_health_report.json inside output-dir)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Folder path for DuckDB / parquet market data cache (default: data/ in repo root)",
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    parsed = parser.parse_args(args)

    # 1. Resolve strategy definition to determine strategy key and output directory
    strategy_input = parsed.strategy_file or parsed.strategy
    strategy_def, strategy_key, resolved_desc, template = resolve_strategy_definition(
        strategy_arg=parsed.strategy,
        strategy_file_arg=parsed.strategy_file,
    )
    parsed._strategy_def = strategy_def
    parsed._strategy_key = strategy_key
    parsed._resolved_desc = resolved_desc
    parsed._template = template

    # 2. Dynamically bind output directory to strategy key if not explicitly given
    if parsed.output_dir is None:
        parsed.output_dir = os.path.join(_REPO_ROOT, "docs", "ruleset", strategy_key)
    else:
        parsed.output_dir = _resolve_path(parsed.output_dir) or parsed.output_dir

    if parsed.account_state_file is None:
        parsed.account_state_file = os.path.join(parsed.output_dir, "account_state.json")
    else:
        parsed.account_state_file = _resolve_path(parsed.account_state_file)

    if parsed.health_report_file is None:
        parsed.health_report_file = os.path.join(parsed.output_dir, "stage1_health_report.json")
    else:
        parsed.health_report_file = _resolve_path(parsed.health_report_file)

    parsed.universe_file = _resolve_path(parsed.universe_file)
    if parsed.strategy_file:
        parsed.strategy_file = _resolve_path(parsed.strategy_file)
    else:
        parsed.strategy_file = resolved_desc

    if parsed.current_holdings_file:
        if not os.path.exists(parsed.current_holdings_file) and os.path.exists(
            os.path.join(parsed.output_dir, parsed.current_holdings_file)
        ):
            parsed.current_holdings_file = os.path.join(parsed.output_dir, parsed.current_holdings_file)
        else:
            parsed.current_holdings_file = _resolve_path(parsed.current_holdings_file)

    if parsed.cache_dir:
        parsed.cache_dir = _resolve_path(parsed.cache_dir)

    return parsed


def load_holdings(args) -> Optional[Dict[str, Any]]:
    """Load current user holdings from CLI argument, file, or output directory."""
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


def apply_intraday_distribution_gate(
    target_series: pd.Series,
    current_series: pd.Series,
    cash_proxy: str,
    health_report: Optional[Dict[str, Any]] = None,
    account_state: Optional[Dict[str, Any]] = None,
) -> pd.Series:
    """Applies Stage 1 intraday distribution gating to target weights.

    When gate_code is 'CAUTION_INTRADAY_DISTRIBUTION', all Phase 1 sell orders remain
    100% untouched, but buy increments (target > current) for non-cash assets are scaled down
    by intraday_buy_scale (default 0.50). The unallocated weight automatically sweeps into cash.
    """
    health_report = health_report or {}
    account_state = account_state or {}
    gate_code = health_report.get("gate_code") or account_state.get("gate_code")
    if gate_code != "CAUTION_INTRADAY_DISTRIBUTION":
        return target_series

    intraday_buy_scale = float(
        health_report.get("intraday_buy_scale", account_state.get("intraday_buy_scale", 0.50))
    )
    print("\n" + "!" * 110)
    print(f"[INTRADAY DISTRIBUTION ALERT / 日内冲高派发预警] 检测到 Stage 1 门禁指令: CAUTION_INTRADAY_DISTRIBUTION")
    print(f"根据实盘风控守则，所有 Phase 1 卖单正常执行，新开/加仓风险资产的目标买入增量压缩至: {intraday_buy_scale:.1%}")
    print("!" * 110 + "\n")

    scaled_target = target_series.copy()
    for s in scaled_target.index:
        if s != cash_proxy and scaled_target[s] > current_series.get(s, 0.0):
            cur_w = float(current_series.get(s, 0.0))
            delta_w = float(scaled_target[s]) - cur_w
            scaled_target[s] = cur_w + delta_w * intraday_buy_scale

    return scaled_target


def main():
    args = parse_args()
    as_of_date = args.as_of_date or date.today().isoformat()
    start_date = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    strategy_def = args._strategy_def
    strategy_key = args._strategy_key
    resolved_desc = args._resolved_desc
    template = args._template

    template_name = strategy_def["template_name"]
    params = strategy_def.get("params", {})
    strat_name = (
        strategy_def.get("strategy_name")
        or strategy_def.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
        or template_name.replace("_", " ").title()
    )
    cash_proxy = params.get("cash_proxy", "BIL")

    # Load account state (if available) for NAV or circuit breakers
    account_state = {}
    if args.account_state_file and os.path.exists(args.account_state_file):
        try:
            with open(args.account_state_file, "r", encoding="utf-8") as f:
                account_state = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to read account state from {args.account_state_file}: {e}")

    health_report = {}
    if args.health_report_file and os.path.exists(args.health_report_file):
        try:
            with open(args.health_report_file, "r", encoding="utf-8") as f:
                health_report = json.load(f)
            print(f"[INFO] 成功读取 Stage 1 门禁报告: {args.health_report_file}")
        except Exception as e:
            print(f"[WARN] Failed to read health report from {args.health_report_file}: {e}")

    # Resolve portfolio NAV: priority CLI > account_state.json current_nav > default 100,000
    if args.portfolio_value is None:
        if account_state.get("current_nav") is not None:
            args.portfolio_value = float(account_state["current_nav"])
            print(f"[INFO] 自动读取账户净值 (Loaded current NAV from state): ¥{args.portfolio_value:,.2f}")
        else:
            args.portfolio_value = 100000.0

    # Universal inertia / turnover threshold and holding tenure constraints
    min_trade_thresh = resolve_trade_inertia_threshold(params, default=0.05)
    min_holding_bars = resolve_min_holding_bars(params, args.min_holding_bars, default=2)
    min_entry_bars = resolve_min_entry_bars(params, args.min_entry_bars, default=2)
    if args.min_entry_bars is not None:
        params["min_entry_bars"] = min_entry_bars

    # Build descriptive engine banner information
    engine_parts = []
    if "blend_weighting_mode" in params or "blend" in template_name.lower():
        engine_parts.append(f"加权={params.get('blend_weighting_mode', 'rolling_ic_ir')}")
        engine_parts.append(f"正交化={params.get('blend_enable_orthogonalization', True)}")
    if "vol_model" in params or any(k.endswith("_vol_model") for k in params):
        vm = resolve_blend_param(params, "vol_model", "garman_klass", str)
        engine_parts.append(f"波动率模型={vm}")
    if "rebalance_freq_days" in params:
        engine_parts.append(f"调仓周期={params['rebalance_freq_days']}日")
    engine_parts.append(f"买入确认={min_entry_bars}日")
    engine_parts.append(f"最低持仓={min_holding_bars}日")
    engine_parts.append(f"惰性过滤={min_trade_thresh:.1%}")
    engine_info = " | ".join(engine_parts)

    print("=" * 110)
    print("UNIVERSAL QUANTITATIVE LIVE TRADING EXECUTION RUNNER (实盘量化调仓指令生成器)")
    print(f"策略方案 (Strategy) : {strat_name} ({template_name} / {strategy_key})")
    print(f"配置模型 (Engine)   : {engine_info}")
    print(f"策略来源 (Config)   : {resolved_desc}")
    print(f"标的资产 (Universe) : {args.universe_file}")
    print(f"数据周期 (Window)   : {start_date} -> {as_of_date} (最新估值日: {as_of_date})")
    print(f"账户资金 (NAV)      : {args.portfolio_value:,.2f} RMB")
    print(f"行情数据 (Provider) : {args.data_provider}")
    print("=" * 110)

    # 2. Load Universe (including any held symbols outside the universe list)
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")
    symbol_names = load_symbol_names(args.universe_file)

    raw_user_holdings = load_holdings(args)
    held_symbols = [
        s.strip()
        for s in (raw_user_holdings.keys() if raw_user_holdings else [])
        if s.strip() not in (cash_proxy, "BIL", "CASH")
    ]
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

    # Resolve actual continuous holding tenure across previous runs / account state
    holding_bars = resolve_holding_bars(
        raw_user_holdings or {},
        account_state=account_state,
        output_dir=args.output_dir,
        current_holdings_file=args.current_holdings_file,
        as_of_date=as_of_date,
        default_bars=1,
    )

    # 3. Generate Weights & Extract Latest Rebalance
    sparse_weights = template.generate_weights(universe, params)
    rebalances = latest_rebalance_rows(sparse_weights)
    if rebalances.empty:
        print("\n[ERROR] No rebalance rows generated at or before this date. Ensure sufficient lookback history.")
        return

    latest_rebal_date = rebalances.index[-1].strftime("%Y-%m-%d")
    current_targets = rebalances.iloc[-1].fillna(0.0)

    is_today_rebal = (pd.Timestamp(as_of_date) in sparse_weights.index) and (
        not sparse_weights.loc[pd.Timestamp(as_of_date)].dropna().empty
    )
    if not is_today_rebal:
        print(f"\n[INFO] 今日 ({as_of_date}) 非模型既定调仓日 (最近调仓信号日: {latest_rebal_date})。")
        print(f"       将严格执行最低持仓锁定期 (min_holding_bars={min_holding_bars})，锁定未满期持仓，避免日内/隔日频繁反向开平仓。")

    # Check circuit breaker from account_state or health_report
    circuit_tier = account_state.get("circuit_breaker_tier") or health_report.get("circuit_breaker_tier")
    freeze_rem = int(account_state.get("freeze_remaining_bars", health_report.get("freeze_remaining_bars", 0)))
    if circuit_tier == "TIER_3_HALT" or freeze_rem > 0:
        print("\n" + "!" * 110)
        print(f"[CIRCUIT BREAKER ENGAGED / 熔断器生效] 账户处于第三级熔断停牌期 (Tier 3 Emergency Halt, 剩余冷冻: {freeze_rem} 交易日)")
        print("根据风控守则，所有风险标的持仓清零，全额切换为现金停泊 (100% Cash Sweep).")
        print("!" * 110 + "\n")
        current_targets = pd.Series(0.0, index=current_targets.index)
    elif account_state.get("linear_equity_scale") is not None or health_report.get("linear_equity_scale") is not None:
        live_scale = float(
            account_state.get("linear_equity_scale")
            if account_state.get("linear_equity_scale") is not None
            else health_report.get("linear_equity_scale")
        )
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

    # Apply Stage 1 intraday distribution gating (CAUTION_INTRADAY_DISTRIBUTION)
    target_series = apply_intraday_distribution_gate(
        target_series,
        current_series,
        cash_proxy,
        health_report=health_report,
        account_state=account_state,
    )

    # 5. Build Execution Order Plan
    # Compute implied cash weight: strategy outputs equity weights, remainder is cash
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

        # Check tenure lock (prohibit premature selling before min_holding_bars)
        held_age = holding_bars.get(sym, min_holding_bars)
        is_circuit_halt = (circuit_tier == "TIER_3_HALT" or freeze_rem > 0)
        is_tenure_locked = (held_age < min_holding_bars) and not is_circuit_halt

        reason = ""
        # Determine action
        if sym == cash_proxy:
            action = "SWEEP_CASH" if delta_w > 0 else "RELEASE_CASH"
            reason = "流动性闲置资金归集/释放"
        elif cur_w == 0.0 and tgt_w == 0.0:
            action = "NO_POSITION"   # not held, not targeted — skip entirely
        elif cur_w == 0.0 and tgt_w > 0.0:
            action = "BUY"           # fresh entry from zero — always a BUY, no inertia filter
            reason = "策略模型新开仓信号"
        elif tgt_w <= 1e-6 and cur_w > 0.0:
            if is_tenure_locked:
                action = "HOLD_FILTERED"
                tgt_w = cur_w
                delta_w = 0.0
                abs_delta = 0.0
                reason = f"未满最低持仓周期 (持仓 {held_age} 日 < 最低 {min_holding_bars} 日，锁定防洗盘)"
            else:
                action = "SELL"          # full liquidation exit — always SELL, never hold remnant
                reason = "策略模型平仓信号 (已满最低持仓周期)"
        elif abs_delta < min_trade_thresh:
            action = "HOLD_FILTERED"  # user holds and change is below inertia threshold
            reason = f"调仓未超惰性阈值 (< {min_trade_thresh:.0%})"
        elif delta_w <= -min_trade_thresh:
            if is_tenure_locked:
                action = "HOLD_FILTERED"
                tgt_w = cur_w
                delta_w = 0.0
                abs_delta = 0.0
                reason = f"未满最低持仓周期 (持仓 {held_age} 日 < 最低 {min_holding_bars} 日，锁定防洗盘)"
            else:
                action = "SELL"
                reason = "策略模型减仓调仓"
        elif delta_w >= min_trade_thresh:
            action = "BUY"
            reason = "策略模型加仓调仓"
        else:
            action = "HOLD"
            reason = "最优目标权重维持"

        current_val = cur_w * args.portfolio_value
        trade_val = delta_w * args.portfolio_value

        # Calculate lot-rounded shares for equities
        sym_lot = resolve_lot_size(sym, args.lot_size)
        current_shares = (
            current_shares_dict.get(sym, int(np.round(current_val / price)))
            if (price > 0 and current_val > 0)
            else current_shares_dict.get(sym, 0)
        )
        if sym != cash_proxy and price > 0:
            if action in ("HOLD_FILTERED", "HOLD"):
                target_shares = current_shares
                delta_shares = 0
                actual_trade_val = 0.0
            elif action == "BUY":
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
        if is_placeholder_stock_name(sym_name, sym):
            sym_name = _FALLBACK_STOCK_NAMES.get(sym, sym)
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
            "holding_bars": held_age if cur_w > 0 else 0,
            "reason": reason,
        })

    # Purchasing power constraint: total buys cannot exceed available cash + sales proceeds
    initial_cash_val = current_series.get(cash_proxy, 0.0) * args.portfolio_value
    total_sales_proceeds = abs(sum(r["trade_value"] for r in rows if r["action"] == "SELL" and r["symbol"] != cash_proxy))
    available_purchasing_power = max(0.0, initial_cash_val + total_sales_proceeds)

    total_desired_buys = sum(r["trade_value"] for r in rows if r["action"] == "BUY" and r["symbol"] != cash_proxy)
    if total_desired_buys > available_purchasing_power and total_desired_buys > 0:
        buy_scale = available_purchasing_power / total_desired_buys
        print(f"\n[RISK ALERT / 购买力约束] 计划买入总额 (¥{total_desired_buys:,.2f}) 超过可用资金 (¥{available_purchasing_power:,.2f})。")
        print(f"                       自动按购买力系数 {buy_scale:.2%} 调整买入股数，防止可用资金击穿。")
        for r in rows:
            if r["action"] == "BUY" and r["symbol"] != cash_proxy and r["price"] > 0:
                sym_lot = resolve_lot_size(r["symbol"], args.lot_size)
                scaled_val = r["trade_value"] * buy_scale
                scaled_shares = int(np.floor(scaled_val / r["price"] / sym_lot) * sym_lot)
                r["delta_shares"] = max(0, scaled_shares)
                r["target_shares"] = r["current_shares"] + r["delta_shares"]
                r["trade_value"] = r["delta_shares"] * r["price"]
                r["trade_value_rmb"] = r["trade_value"]
                r["target_weight"] = (r["target_shares"] * r["price"]) / args.portfolio_value
                r["delta_weight"] = r["target_weight"] - r["current_weight"]

    order_df = pd.DataFrame(rows)

    # Reconcile cash proxy: actual post-trade cash = initial cash - net equity purchases
    cash_mask = order_df["symbol"] == cash_proxy
    if cash_mask.any():
        equity_trades_val = order_df.loc[~cash_mask, "trade_value"].sum()
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
            reason = r.get("reason") or (f"调仓未超惰性阈值 (< {min_trade_thresh:.0%})" if r["action"] == "HOLD_FILTERED" else "最优目标权重维持")
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            shares_info = f"({int(r['current_shares'])} 股)" if r['current_shares'] > 0 else ""
            h_bar = int(r.get("holding_bars", 0))
            tenure_str = f" [持仓{h_bar}日]" if h_bar > 0 else ""
            print(f"  ⚪ HOLD {sym_col} | 维持仓位: {r['target_weight']:>5.1%} {shares_info:<10}{tenure_str} | 持仓市值: ~¥{r['target_weight']*args.portfolio_value:>9,.2f} | ({reason})")

    if not cash_rows.empty:
        print("\n[PHASE 4: CASH PROXY & LIQUIDITY MANAGEMENT (第四阶段: 闲置资金对账)]")
        for _, r in cash_rows.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            print(f"  💰 CASH {sym_col} | 闲置现金: {r['target_weight']:>5.1%} (~¥{r['target_weight']*args.portfolio_value:>9,.2f}) | 停泊于 {r['symbol']} / 511880 / 国债逆回购")

    # Update post-trade holdings and holding_bars
    updated_holding_bars = {}
    post_holdings = {}
    for _, r in order_df.iterrows():
        sym = r["symbol"]
        if sym == cash_proxy:
            continue
        tgt_sh = int(r["target_shares"])
        if tgt_sh > 0:
            post_holdings[sym] = tgt_sh
            if r["action"] == "BUY" and int(r["current_shares"]) == 0:
                updated_holding_bars[sym] = 0
            else:
                updated_holding_bars[sym] = int(r.get("holding_bars", 1))

    # Save to CSV and JSON
    os.makedirs(args.output_dir, exist_ok=True)
    out_csv = os.path.join(args.output_dir, "live_trading_ticket.csv")
    order_df.to_csv(out_csv, index=False)
    out_json = os.path.join(args.output_dir, "live_trading_ticket.json")
    try:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(order_df.to_dict(orient="records"), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Failed to write live_trading_ticket.json: {e}")
    print("\n" + "=" * 110)
    print(f"[SUCCESS] 交易指令单已导出至: {out_csv} 及 {out_json}")

    # Save post_holdings.json if output_dir is given
    post_holdings_path = os.path.join(args.output_dir, "post_holdings.json")
    try:
        with open(post_holdings_path, "w", encoding="utf-8") as f:
            json.dump(post_holdings, f, indent=2, ensure_ascii=False)
        print(f"[SUCCESS] 预测持仓已导出至: {post_holdings_path}")
    except Exception as e:
        print(f"[WARN] Failed to write post_holdings.json: {e}")

    # Persist updated holding_bars into account_state.json if available
    if args.account_state_file:
        if isinstance(account_state.get("prior_state"), dict) and "holding_bars" not in account_state["prior_state"]:
            account_state["prior_state"]["holding_bars"] = dict(holding_bars)
        account_state["holding_bars"] = updated_holding_bars
        try:
            with open(args.account_state_file, "w", encoding="utf-8") as f:
                json.dump(account_state, f, indent=2, ensure_ascii=False)
            print(f"[SUCCESS] 账户状态持仓周期已更新至: {args.account_state_file}")
        except Exception as e:
            print(f"[WARN] Failed to update account state file: {e}")
    print("=" * 110)


if __name__ == "__main__":
    main()

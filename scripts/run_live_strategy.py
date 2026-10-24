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


from common.data import get_data_provider
from common.allocation_backtester import _get_hk_board_lot
from common.cli_utils import (
    add_data_provider_cli_args,
    add_market_clock_cli_args,
    build_data_kwargs,
    load_live_universe,
    persist_run_metadata,
    resolve_live_clock_context,
    shared_data_dir,
)
from common.financial_api import check_missing_trading_days, get_trading_days_calendar
from common.market_clock import (
    MARKET_CHINA,
    MixedMarketUniverseError,
    UnknownSymbolMarketError,
    as_of_trading_date,
    infer_market_from_symbols,
)
from common.position_lifecycle import (
    AssetLifecycleState,
    BlockingReason,
    ExitType,
    LifecycleDecision,
    PositionLifecycleConfig,
    PositionState,
    blocking_reason_label,
    coerce_blocking_reason,
    is_liquidation_event,
    step_position_lifecycle,
)
from common.strategy_spec import _STATIC_TEMPLATES, get_template, load_strategy_file
from common.universe import (
    FALLBACK_STOCK_NAMES,
    get_stock_name,
    is_placeholder_stock_name,
    resolve_universe_from_args,
)
from pipeline.live_signal.lsig.signal import as_of_universe, latest_rebalance_rows

# Default *output directory* owner -- shared with Stage 1
# (`scripts/check_live_portfolio_health.py`), which writes the very
# `stage1_health_report.json` / `account_state.json` this script reads back out of
# its own `--output-dir`. Moving the default in one file and not the other would
# silently split "tickets written here, health check reads there", so neither file
# may re-spell the path. See `scripts/live_paths.py` for the layout and its evidence.
from scripts.live_paths import DEFAULT_STRATEGY_KEY, default_live_output_dir


# (2026-10-22 incident, Fix 2) Freshness policy -- bar-date-vs-expected-session
# comparison and the strict/allow-stale decision -- has exactly ONE owner:
# `common/freshness.py`. Do NOT re-implement `df.index[-1].date() != as_of_date`
# anywhere in this file; two drifting freshness checks is the same class of bug
# the incident was. The re-export below keeps `scripts.run_live_strategy.StalePriceError`
# working for callers/tests that already reference it here.
from common.freshness import (
    StalePriceError,
    assert_fresh_universe,
    is_stale_bar,
    last_bar_session,
    latest_close,
)


try:
    from pipeline.research.blend.blend_base import _get_symbol_sector
except ImportError:
    try:
        from research.blend.blend_base import _get_symbol_sector
    except ImportError:
        def _get_symbol_sector(sym: str) -> str:
            raw = sym.split(".")[0]
            return f"sec_{raw[:2]}" if len(raw) >= 2 else "sec_other"


DEFAULT_STRATEGY_FILE = os.path.join(
    _REPO_ROOT,
    "pipeline",
    "research_strategy",
    "results",
    "strategy_dumps",
    "chan_four_state_blend_strategy.json",
)
DEFAULT_UNIVERSE_FILE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")
# Default output dir: `<repo_root>/scripts/results/live/<strategy_key>`.
# Owned by `scripts/live_paths.py` and shared with Stage 1 -- NOT `docs/ruleset/`
# any more (that tree is hand-authored manuals; a live run must not write tickets
# into it, and nothing may create it by default). The effective default is bound to
# the *resolved* strategy key in `parse_args`; this constant only documents the
# unparameterised case, so it must be derived, never re-spelled.
DEFAULT_OUTPUT_DIR = default_live_output_dir(DEFAULT_STRATEGY_KEY)

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


def resolve_run_mode(clock_ctx: Any) -> str:
    """`replay` or `live` for this run -- the machine-readable marker (`L2.30`).

    Semantics, and they are NOT invented here: `common/cli_utils.resolve_live_clock_context`
    already decides this when it resolves the run-window gate. An explicit `--as-of-date`
    pinned to a session EARLIER than the clock's latest published session is a deliberate
    historical replay (`window_gate == "not_applicable_replay"`): the window gate is
    skipped *by design* so 复盘/重放 works, but such a run still emits a full order ticket.
    Without a marker that ticket is indistinguishable from a same-day real one, so it could
    be archived under today's key or used in post-hoc attribution.

    Every other shape is `live`:
      * no `--as-of-date` (as-of = the clock's published session),
      * `--as-of-date` == the clock's published session (an explicit same-day run),
      * a future `--as-of-date`, which is rejected upstream by the clock (never reaches here).
    Same-day and future-day behaviour is therefore unchanged bit-for-bit; only the marker
    and its banner are new.
    """
    gate = str(getattr(clock_ctx, "window_gate", "") or "")
    if gate == "not_applicable_replay":
        return "replay"
    # Defensive second spelling of the same predicate, so the marker cannot silently read
    # "live" if the gate label is ever renamed: as-of strictly before the clock's session.
    as_of = str(getattr(clock_ctx, "as_of_date", "") or "")
    clock_as_of = str(getattr(clock_ctx, "clock_as_of", "") or "")
    if (
        str(getattr(clock_ctx, "as_of_source", "")) == "explicit"
        and as_of
        and clock_as_of
        and as_of < clock_as_of
    ):
        return "replay"
    return "live"


def announce_run_mode(clock_ctx: Any, run_mode: str) -> None:
    """Loud operator-facing banner when a replay ticket is about to be produced (`L2.30`).

    A replay bypasses the run-window gate (by design). The banner exists so nobody mistakes
    the resulting ticket for a same-day real instruction: intraday manual replay still
    produces orders, and that is exactly the sentence the operator must see.
    """
    if run_mode != "replay":
        return
    print("\n" + "!" * 110)
    print(
        "[WARN] REPLAY MODE / 历史回放模式 (run_mode=replay): "
        f"--as-of-date={clock_ctx.as_of_date} 早于市场时钟最近已收盘交易日 "
        f"{clock_ctx.clock_as_of}。"
    )
    print(
        "       运行窗口门禁按设计不适用于 replay（盘中手工回放仍能出工单），"
        "因此本工单不是当前时点的实盘指令。"
    )
    print(
        "       禁止把本工单当作当日真实工单归档，或用于事后归因/业绩归因；"
        "summary.json 已写入 run_mode=replay 可机读标记。"
    )
    print("!" * 110 + "\n")


def resolve_market_as_of_date(
    symbols: Optional[List[str]] = None,
    now: Optional[Any] = None,
) -> date:
    """The **market** as-of trading day, used whenever a caller passes no `as_of_date`.

    (development_plan.md `L2.31`) This replaces a bare `date.today()` fallback. OWNER of
    "what day is it in the market" is `common/market_clock.py` (`L2.1`/`L2.5`); a host-side
    calendar day is a different day on any host whose timezone is not the market's -- e.g.
    a UTC-7 host at 2026-10-23T00:30Z reads 10-22 while Shanghai is already mid 10-23, so
    the old fallback silently aged every holding/cooldown verdict by one session. That is
    the same root cause as the `L2.4` archive-key incident, one layer down (script layer).

    Market selection: inferred from `symbols` via the clock's own
    `infer_market_from_symbols`. When the list is empty, unrecognisable (`NEW_SYM`,
    synthetic fixtures) or spans markets, the calendar cannot be picked from it, so we
    fall back to `MARKET_CHINA` -- this script's operating market (every default universe
    under `docs/universe/china/` is A-share) -- rather than raising: an exception here
    would be swallowed by callers' `except Exception: default_bars` paths and would look
    exactly like "no anchor at all", which is worse than a documented default.

    `now` is an injection seam for tests/replays (tz-aware UTC); production passes None,
    so the one wall-clock read stays inside `market_clock.market_now()`.
    """
    market: Optional[str] = None
    try:
        market = infer_market_from_symbols(list(symbols or []))
    except (UnknownSymbolMarketError, MixedMarketUniverseError):
        market = None
    return as_of_trading_date(now=now, market=market or MARKET_CHINA)


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


def resolve_exit_cooldown(
    symbols: List[str],
    account_state: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    as_of_date: Optional[str] = None,
    default_bars: int = 999,
    now: Optional[Any] = None,
) -> Dict[str, int]:
    """Calculates elapsed trading days since the last full exit for each symbol.

    Reads last_exit_date from account_state (or historical run folders), and uses
    check_missing_trading_days from common.financial_api to determine elapsed trading days
    in the open-closed interval (last_exit_date, as_of_date].

    Archive-scan anchoring caveat (narrowed 2026-10-23, development_plan.md L2.21):
    when reconstructing last_exit_date from historical live_trading_ticket.csv
    files, a row anchors an exit ONLY if action == "SELL".

    (L2.24) The verdict is taken from `common.position_lifecycle.is_liquidation_event`
    -- the single predicate for "did this row liquidate the position?" -- and NOT
    from `target_weight`, and NOT from `blocking_reason`. Archived tickets written
    before `L2.24` simply have no `blocking_reason` column; the read goes through
    `coerce_blocking_reason` (missing -> `NONE`) so legacy archives keep working
    unchanged instead of raising `KeyError`. A `blocking_reason` can only ever
    explain why a row traded nothing; it can never promote a non-`SELL` row into
    an anchor.

    History / why the old `target_weight == 0.0` leg was removed: the 2026-10-22
    hotfix already excluded CANCEL_PENDING (a cancelled PENDING_ENTRY for a symbol
    that was never held), but kept `tgt_w == 0.0` as an anchor for every OTHER
    action. That leg was phantoms-only -- `target_weight == 0.0` is written by
    three code paths that never executed a single share:
      1. minimum-entry / rebalance threshold filtering
         (common/position_lifecycle.py:458 -> HOLD_FILTERED, tgt_w 0.0);
      2. the PENDING_ENTRY confirmation window
         (common/position_lifecycle.py:543 -> HOLD_FILTERED, tgt_w 0.0);
      3. the market-closed freeze (common/position_lifecycle.py:200-209 ->
         HOLD_FILTERED with target_weight = state.current_weight, which is 0.0
         whenever the symbol is flat -- an ordinary "market shut" note became an
         exit).
    A forensic scan of the 25 archived tickets found 34 such phantom-anchoring
    rows. Because the scan runs newest-first, a phantom anchor frequently sits
    only ONE day before as_of -- enough to arm the 2-trading-day re-entry cooldown
    (`--reentry-cooldown-bars` default 2), so the symbol got pinned to COOLDOWN ->
    HOLD_FILTERED and flattened right before Phase 2 could buy it again. Every
    real liquidation path in this script emits action == "SELL", so narrowing to
    SELL loses no genuine anchor; CANCEL_PENDING needs no special case any more.

    Deliberately unchanged:
      - the account_state / prior_state `last_exit_date` path above never consults
        the archive, so state-file-anchored cooldowns behave exactly as before;
      - `default_bars` (999) still means "no known exit".
    (L2.22, decided 2026-10-23) Partial de-risking is NOT an exit. Both anchor
    paths enforce that now:
      - the ticket-writing site uses `should_anchor_reentry_cooldown` -- a real
        `SELL` AND a post-trade position of zero AND not a closed-session freeze;
      - THIS archive scan additionally requires the archived row's post-fill
        quantity to be zero (`_archived_shares_after`), because an archived partial
        trim is a `SELL` row and the L2.21 SELL-only verdict would have re-armed
        the cooldown on the next run, defeating the ticket-site fix.
    Only a full liquidation restarts the exit clock.
    The companion reader resolve_holding_bars classifies by holdings/entry columns
    and is NOT affected by this heuristic.
    """
    as_of_d = (
        pd.Timestamp(as_of_date).date()
        if as_of_date
        # (L2.31) No explicit valuation date -> ask the MARKET clock, never the host's
        # `date.today()`. This date decides "how many bars held" and therefore every
        # cooldown/tenure verdict, so a host-vs-market off-by-one silently shortens the
        # re-entry cooldown. Owner: `common/market_clock.as_of_trading_date`.
        else resolve_market_as_of_date(symbols, now=now)
    )
    account_state = account_state or {}
    last_exit_dates: Dict[str, str] = {}
    if isinstance(account_state.get("last_exit_date"), dict):
        last_exit_dates.update(account_state["last_exit_date"])

    if isinstance(account_state.get("prior_state"), dict) and isinstance(account_state["prior_state"].get("last_exit_date"), dict):
        for k, v in account_state["prior_state"]["last_exit_date"].items():
            if k not in last_exit_dates:
                last_exit_dates[k] = v

    unresolved = [s for s in symbols if s not in last_exit_dates]
    if unresolved and output_dir:
        cand_parents = [os.path.dirname(os.path.abspath(output_dir)), os.path.abspath(output_dir)]
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        cur_date_str = as_of_d.isoformat()
        prior_dates = []
        parent_dir = None
        for p in cand_parents:
            if os.path.isdir(p):
                sub_dates = sorted([d for d in os.listdir(p) if date_re.match(d) and d < cur_date_str], reverse=True)
                if sub_dates:
                    parent_dir = p
                    prior_dates = sub_dates
                    break
        if parent_dir and prior_dates:
            for sym in unresolved:
                for d in prior_dates:
                    d_ticket = os.path.join(parent_dir, d, "live_trading_ticket.csv")
                    if os.path.exists(d_ticket):
                        try:
                            df = pd.read_csv(d_ticket)
                            row = df[df["symbol"] == sym]
                            if not row.empty:
                                first = row.iloc[0]
                                act = str(first.get("action", ""))
                                # (L2.24) `blocking_reason` is read only for the
                                # legacy-tolerance contract: pre-L2.24 archives have
                                # no such column (`Series.get` -> None -> NONE), so
                                # nothing may raise and nothing may change. The
                                # anchoring verdict below comes from `action` (see
                                # is_liquidation_event()) PLUS the L2.22 post-fill
                                # share check -- never from `target_weight`.
                                # (`target_weight` is intentionally not read any more -- see the
                                # narrowing rationale below.)
                                # (2026-10-23 narrowing, development_plan.md L2.21) ONLY a real
                                # `SELL` anchors. The old `tgt_w == 0.0` leg (with CANCEL_PENDING
                                # excluded by the 2026-10-22 hotfix) was pure phantom-anchor
                                # machinery: HOLD_FILTERED + tgt_w 0.0 is emitted by the min-entry
                                # threshold filter, by the PENDING_ENTRY confirmation window, and by
                                # the market-closed freeze while flat -- none of which traded
                                # anything. 34 such rows across the 25 archived tickets, often one
                                # day before as_of, armed the 2-day cooldown and flattened the
                                # symbol before Phase 2 buys. `act != "CANCEL_PENDING"` is now
                                # redundant: a CANCEL_PENDING row is never `SELL`, so it cannot
                                # shadow a genuine day-1 SELL anchor any more.
                                # (L2.22, decided 2026-10-23) A `SELL` alone is NOT enough
                                # to anchor from an ARCHIVE either: the ruling is "only a
                                # position going to zero is an exit; a partial trim never
                                # resets the cooldown". An archived partial trim carries
                                # `action == "SELL"` with `target_shares > 0`, so the
                                # SELL-only verdict below (L2.21) re-armed the cooldown on
                                # the NEXT run and silently defeated the ticket-site fix.
                                # The post-fill quantity recorded in the archive row
                                # (`target_shares`) is the second, independent fact: the
                                # position must be gone. Same predicate family as
                                # `should_anchor_reentry_cooldown` (which adds the
                                # `blocking_reason` leg; an ARCHIVED freeze row can never be
                                # a SELL, so it needs no special case here).
                                block_label = coerce_blocking_reason(first.get("blocking_reason"))
                                if (
                                    is_liquidation_event(act, block_label)
                                    and _archived_shares_after(first) == 0
                                ):
                                    last_exit_dates[sym] = d
                                    break
                        except Exception:
                            pass

    exit_bars: Dict[str, int] = {}
    for sym in symbols:
        exit_date_str = last_exit_dates.get(sym)
        if not exit_date_str:
            exit_bars[sym] = default_bars
            continue
        try:
            exit_d = pd.Timestamp(exit_date_str).date()
            if as_of_d <= exit_d:
                exit_bars[sym] = 0
            else:
                missed = check_missing_trading_days(exit_d, as_of_d)
                exit_bars[sym] = len(missed)
        except Exception:
            exit_bars[sym] = default_bars

    return exit_bars


def _archived_shares_after(row: Any) -> int:
    """Post-fill held quantity recorded by an archived `live_trading_ticket.csv` row.

    `docs/development_plan.md` **L2.22**. Returns the row's `target_shares`, i.e. the
    quantity the account holds once that historical ticket's orders have filled --
    the archive-side counterpart of the live runner's post-trade share count. Only
    a zero here (a position that went to ZERO) may anchor a re-entry cooldown.

    Legacy tolerance is deliberate and directional: a pre-column archive (no
    `target_shares` key -- the column predates the archive heuristic, so this is a
    tolerance path rather than a normal one) or an unparseable value returns 0,
    i.e. it keeps the old "a `SELL` anchors" verdict. Refusing to anchor on a row
    whose fill quantity cannot be read would silently DISABLE a genuine cooldown,
    which is the riskier failure. Never invert this default.
    """
    try:
        raw = row.get("target_shares")
    except AttributeError:
        return 0
    if raw is None:
        return 0
    try:
        if pd.isna(raw):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def should_anchor_reentry_cooldown(
    action: Any,
    blocking_reason: Any,
    shares_before: int,
    shares_after: int,
) -> bool:
    """Whether a ticket row may re-arm (anchor) an asset's re-entry cooldown.

    `docs/development_plan.md` **L2.22**, decided 2026-10-23. The product ruling is
    deliberately NOT "any de-risking anchors": **only a position actually going to
    zero counts as an exit, and a partial trim never resets the cooldown clock.**

    Three independent facts must all hold:

    1. a real exit EXECUTED -- `common.position_lifecycle.is_liquidation_event`
       (i.e. `action == "SELL"`). A gated / holding / record row is never an exit
       no matter what weight it carries;
    2. the position is GONE afterwards -- `shares_after == 0` on a position that was
       non-empty before (`shares_before > 0`). This is SHARE arithmetic on the
       post-trade book, the one honest "did we exit?" datum; a partial trim leaves
       shares behind and therefore anchors nothing;
    3. the row was EVALUATED at all -- `blocking_reason != MARKET_CLOSED_FREEZE`.
       A closed-session row freezes the whole state machine and trades nothing; it
       documents that nothing ran, not that we exited.

    Why this is a named predicate rather than an inline condition: the previous
    inline form (`cur_sh > 0 and (act == "SELL" or tgt_sh == 0)`) anchored on the
    `SELL` leg for PARTIAL trims too, so a de-risking trim re-armed the cooldown and
    pinned the asset out of Phase 2 for the next N sessions. It also still carried a
    `tgt_sh == 0` inference -- exactly the zero-target liquidation inference that
    `L2.21`/`L2.24`/`L2.25` removed everywhere else. NEVER re-derive an exit from
    `target_weight == 0.0` (or a zero target SHARE count) alone: only the pair
    (real `SELL`, zero shares after) is a liquidation.
    """
    if coerce_blocking_reason(blocking_reason) is BlockingReason.MARKET_CLOSED_FREEZE:
        # Nothing was evaluated: the state machine is frozen, no order exists.
        return False
    if not is_liquidation_event(action, blocking_reason):
        # Not a SELL -> not an exit (a blocking label can never promote a row).
        return False
    # Post-trade share truth: the position must be fully gone.
    return shares_after == 0 and shares_before > 0


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

    # Extract version metadata
    strat_version = getattr(template, "version", None) or strategy_def.get("strategy_version") or "1.0.0"
    base_version = getattr(template, "base_version", None) or strategy_def.get("base_version")
    if hasattr(template, "get_version_info"):
        try:
            strat_version_info = template.get_version_info(params=params)
        except Exception:
            try:
                strat_version_info = template.get_version_info()
            except Exception:
                strat_version_info = {
                    "version": str(strat_version),
                    "template_name": template_name,
                }
    else:
        strat_version_info = strategy_def.get("version_info") or {
            "version": str(strat_version),
            "template_name": template_name,
        }

    strategy_def["strategy_version"] = str(strat_version)
    strategy_def["base_version"] = str(base_version) if base_version else None
    strategy_def["strategy_version_info"] = strat_version_info

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
        help=(
            "Point-in-time valuation date (YYYY-MM-DD). Default: resolved from the market clock's "
            "as_of_trading_date (close + publication grace, in the market's own timezone) -- never the "
            "host's local date. An explicit value is still validated against the clock: it must be a "
            "trading day of --market and may not be later than the latest published session. "
            "WARN: passing a PAST trading day is a deliberate history replay -- the run-window gate is "
            "skipped by design (盘中手工回放仍能出工单 / intraday manual replay still produces an order "
            "ticket) and the run is marked run_mode=replay in summary.json; do not archive that ticket "
            "as a same-day live one or use it for attribution."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Lookback start date for historical market data loading (YYYY-MM-DD, default: as_of_date - lookback_days)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=800,
        help="Lookback calendar days for indicator warmup (default: 800 days)",
    )
    parser.add_argument(
        "--guard-latency-bars",
        type=int,
        default=2,
        help="Unified entry and exit confirmation latency bars (UPLSM master parameter, default: 2)",
    )
    parser.add_argument(
        "--entry-confirm-bars",
        type=int,
        default=2,
        help="Consecutive bars required to confirm a new BUY signal (default: 2)",
    )
    parser.add_argument(
        "--exit-confirm-bars",
        type=int,
        default=2,
        help="Consecutive bars required to confirm liquidation for small positions below inertia threshold (default: 2)",
    )
    parser.add_argument(
        "--reentry-cooldown-bars",
        type=int,
        default=2,
        help="Trading days cooldown after full exit before permitting re-entry (default: 2)",
    )
    parser.add_argument(
        "--min-entry-threshold",
        type=float,
        default=None,
        help="Minimum target weight threshold for fresh entry (default: inherits trade inertia threshold, e.g. 0.05 / 5%%)",
    )
    parser.add_argument(
        "--volume-breakout-mult",
        type=float,
        default=1.5,
        help="Volume multiplier over 20-day MA volume to qualify as breakout exempting confirmation (default: 1.5)",
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
        help=(
            "Directory to save generated trading orders CSV (default: "
            "scripts/results/live/<strategy_key>, an ordinary workspace artifact "
            "directory -- never docs/ruleset/)"
        ),
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
    parser.add_argument(
        "--enable-intraday-5m",
        action="store_true",
        help="Enable 5-minute intraday execution price guidance and microstructure checks",
    )
    parser.add_argument(
        "--cutoff-time",
        default="14:15",
        help=(
            "Intraday cutoff time for Stage 2 order generation, expressed in the MARKET's local wall "
            "clock (not the host's; default: 14:15, 39 5m bars). Used only for 5m EXECUTION-price "
            "guidance -- daily-bar valuation always uses the as-of session from the market clock."
        ),
    )
    parser.add_argument(
        "--allow-stale-prices",
        action="store_true",
        help=(
            "DANGEROUS EMERGENCY OVERRIDE (equivalent to --no-strict-freshness). By default Stage 2 "
            "REFUSES to run (raises StalePriceError) when any mark-to-market close is not the --as-of-date "
            "bar, because a stale close silently corrupts NAV, weights and share quantities. With this flag "
            "the mismatch is downgraded to a loud warning, the run continues, and every affected ticket row "
            "carries price_stale=true plus a [陈旧价格] note in its reason. Intended only for non-trading-day "
            "dry runs / known provider gaps."
        ),
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    # (L2.3) --market / --allow-intraday / --strict-market-clock / --strict-freshness /
    # --publication-grace-minutes / --max-probe-attempts: the as-of + run-window gate
    # flags from design §8. Declared through the shared helper so Stage 1, Stage 2 and
    # the multi-strategy netting runner cannot drift apart.
    add_market_clock_cli_args(parser, stage="stage2")
    parsed = parser.parse_args(args)

    # Reconcile guard_latency_bars with backward compatibility overrides
    raw_args = args if args is not None else sys.argv[1:]
    has_guard = any(a == "--guard-latency-bars" or a.startswith("--guard-latency-bars=") for a in raw_args)
    has_entry = any(a == "--entry-confirm-bars" or a.startswith("--entry-confirm-bars=") for a in raw_args)
    has_exit = any(a == "--exit-confirm-bars" or a.startswith("--exit-confirm-bars=") for a in raw_args)
    has_min_entry = (parsed.min_entry_bars is not None)

    if not has_guard:
        if has_min_entry:
            parsed.guard_latency_bars = parsed.min_entry_bars
        elif has_entry:
            parsed.guard_latency_bars = parsed.entry_confirm_bars
        elif has_exit:
            parsed.guard_latency_bars = parsed.exit_confirm_bars

    if not has_entry:
        parsed.entry_confirm_bars = parsed.guard_latency_bars
    if not has_exit:
        parsed.exit_confirm_bars = parsed.guard_latency_bars

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

    # 2. Dynamically bind output directory to strategy key if not explicitly given.
    #    The path comes from `scripts.live_paths.default_live_output_dir` -- the same
    #    resolver Stage 1 (`scripts/check_live_portfolio_health.py`) uses for its own
    #    `--output-dir` default. Stage 1 writes `stage1_health_report.json` and
    #    `account_state.json` there and this script reads them back, so the two
    #    defaults MUST resolve to one directory; they do so by sharing a function,
    #    not by two literals that happen to match.
    if parsed.output_dir is None:
        parsed.output_dir = default_live_output_dir(strategy_key)
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
    as_of_date: Optional[str] = None,
    allow_stale_prices: bool = False,
) -> Tuple[Dict[str, float], Dict[str, int], float, float]:
    """Resolve user-supplied holdings (either share quantities or fractional weights)
    into normalized portfolio weights and exact held share counts.

    Returns:
        (weights_dict, shares_dict, total_equity_val, cash_val)

    (2026-10-22 incident, Fix 2) This is a mark-to-market path: `total_equity_val`
    / `cash_val` / each holding's weight are all derived from the symbol's last
    close. A stale last bar therefore silently rewrites the whole reference book,
    not just one number. Every price here is resolved through
    `common.freshness.latest_close()`, the single owner of the bar-date-vs-
    expected-session comparison, which raises `StalePriceError` on a mismatch
    unless `allow_stale_prices=True` -- the call site passes
    `not clock_ctx.freshness_strict`, i.e. the single boolean that both
    `--no-strict-freshness` and `--allow-stale-prices` fold into.
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
            if sym in universe and not universe[sym].empty:
                # Same mark-to-market concept as the order loop's price site:
                # routed through the single freshness owner (common/freshness.py).
                price = latest_close(
                    universe,
                    sym,
                    as_of_date,
                    strict=not allow_stale_prices,
                )
            else:
                price = 0.0
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
                # Same mark-to-market concept as the order loop's price site:
                # routed through the single freshness owner (common/freshness.py).
                price = latest_close(
                    universe,
                    sym,
                    as_of_date,
                    strict=not allow_stale_prices,
                )
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


def main(cli_args: Optional[List[str]] = None):
    args = parse_args(cli_args)

    # (L2.3) as-of and the run-window gate come from `common/market_clock.py`, NOT
    # from the host's `date.today()`. The universe is resolved first only because the
    # authoritative market is inferred from its symbol suffixes; the gate then runs
    # BEFORE any data load, so an out-of-window invocation costs nothing and cannot
    # half-produce a ticket.
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")
    clock_ctx = resolve_live_clock_context(args, universe_symbols, stage="stage2")
    as_of_date = clock_ctx.as_of_date
    expected_last_session = clock_ctx.expected_last_session
    # (L2.30) A pinned past session is a REPLAY: the run-window gate is skipped by design,
    # so the run must announce itself and carry a machine-readable marker into summary.json.
    # Every other shape (no --as-of-date, same-day, future-day) resolves to "live" and its
    # behaviour is unchanged.
    run_mode = resolve_run_mode(clock_ctx)
    announce_run_mode(clock_ctx, run_mode)
    if getattr(args, "start_date", None):
        start_date = args.start_date
    else:
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

    # Track streak counters, historical entry prices, and UPLSM lifecycle states from account_state
    entry_signal_streak: Dict[str, int] = dict(account_state.get("entry_signal_streak", {}))
    zero_target_streak: Dict[str, int] = dict(account_state.get("zero_target_streak", {}))
    entry_prices: Dict[str, float] = dict(account_state.get("entry_prices", {}))
    last_exit_date: Dict[str, str] = dict(account_state.get("last_exit_date", {}))
    lifecycle_states_raw: Dict[str, Any] = dict(account_state.get("lifecycle_states", {}))

    if account_state.get("as_of_date") == as_of_date and isinstance(account_state.get("prior_state"), dict):
        ps = account_state["prior_state"]
        if isinstance(ps.get("entry_signal_streak"), dict):
            entry_signal_streak = dict(ps["entry_signal_streak"])
        if isinstance(ps.get("zero_target_streak"), dict):
            zero_target_streak = dict(ps["zero_target_streak"])
        if isinstance(ps.get("entry_prices"), dict):
            entry_prices = dict(ps["entry_prices"])
        if isinstance(ps.get("last_exit_date"), dict):
            last_exit_date = dict(ps["last_exit_date"])
        if isinstance(ps.get("lifecycle_states"), dict):
            lifecycle_states_raw = dict(ps["lifecycle_states"])

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
    min_entry_thresh = getattr(args, "min_entry_threshold", None) or resolve_blend_param(params, "entry_threshold", None, float) or min_trade_thresh
    min_holding_bars = resolve_min_holding_bars(params, args.min_holding_bars, default=2)
    guard_latency_bars = getattr(args, "guard_latency_bars", 2)
    if args.min_entry_bars is not None:
        guard_latency_bars = resolve_min_entry_bars(params, args.min_entry_bars, default=guard_latency_bars)
    elif "guard_latency_bars" in params:
        guard_latency_bars = int(params["guard_latency_bars"])
    elif "min_entry_bars" in params or "cfsb_min_entry_bars" in params or "cfssb_min_entry_bars" in params:
        guard_latency_bars = resolve_min_entry_bars(params, default=guard_latency_bars)

    entry_confirm_bars = guard_latency_bars
    exit_confirm_bars = guard_latency_bars
    min_entry_bars = guard_latency_bars
    params["min_entry_bars"] = min_entry_bars
    params["guard_latency_bars"] = guard_latency_bars

    lifecycle_cfg = PositionLifecycleConfig(
        guard_latency_bars=guard_latency_bars,
        min_holding_bars=min_holding_bars,
        normal_cooldown_bars=getattr(args, "reentry_cooldown_bars", 2),
        stop_cooldown_bars=max(5, getattr(args, "reentry_cooldown_bars", 2)),
        inertia_threshold=min_trade_thresh,
        # (2026-10-22 incident) Relative trim-side inertia hurdle (Fix 7):
        # SELL trims must clear max(inertia_threshold, ratio * current_weight).
        # Resolved via cfsb_inertia_relative_ratio for chan_four_state_blend;
        # 0.0 (disabled) everywhere else so other strategies are unaffected.
        inertia_relative_ratio=resolve_blend_param(params, "inertia_relative_ratio", 0.0, float),
        min_entry_threshold=min_entry_thresh,
        hard_stop_loss_pct=0.08,
        atr_multiplier=4.5,
        volume_surge_multiplier=getattr(args, "volume_breakout_mult", 1.5),
        breadth_thrust_threshold=0.60,
        enable_entry_fast_path=True,
        enable_exit_fast_path=True,
    )

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
    engine_parts.append(f"UPLSM确认={guard_latency_bars}日")
    engine_parts.append(f"买入确认={min_entry_bars}日")
    engine_parts.append(f"最低持仓={min_holding_bars}日")
    engine_parts.append(f"惰性过滤={min_trade_thresh:.1%}")
    engine_parts.append(f"最低建仓={min_entry_thresh:.1%}")
    engine_info = " | ".join(engine_parts)

    strat_ver = strategy_def.get("strategy_version", "1.0.0")
    base_ver = strategy_def.get("base_version")
    strategy_version_info = strategy_def.get("version_info")
    if strategy_version_info is None and hasattr(template, "get_version_info"):
        try:
            strategy_version_info = template.get_version_info()
        except Exception:
            pass
    if strategy_version_info is None:
        strategy_version_info = {"version": strat_ver}
        if base_ver:
            strategy_version_info["base_version"] = base_ver
    ver_str = f"v{strat_ver}" + (f" [base: v{base_ver}]" if base_ver else "")

    print("=" * 110)
    print("UNIVERSAL QUANTITATIVE LIVE TRADING EXECUTION RUNNER (实盘量化调仓指令生成器)")
    print(f"策略方案 (Strategy) : {strat_name} ({template_name} / {strategy_key})")
    print(f"策略版本 (Version)  : {ver_str}")
    print(f"配置模型 (Engine)   : {engine_info}")
    print(f"策略来源 (Config)   : {resolved_desc}")
    print(f"标的资产 (Universe) : {args.universe_file}")
    print(f"数据周期 (Window)   : {start_date} -> {as_of_date} (最新估值日: {as_of_date})")
    print(f"账户资金 (NAV)      : {args.portfolio_value:,.2f} RMB")
    print(f"行情数据 (Provider) : {args.data_provider}")
    # (2026-10-22 incident) Stage-1 `target_exposure` is an advisory macro
    # "should" level, NOT an executable equity weight. The executable equity /
    # cash split is the model's generate_weights output printed below after
    # computation. This note prevents the Stage-1 advisory (e.g. 71.1% equity)
    # being misread as the literal Stage-2 cash-proxy target (e.g. 44.4% BIL).
    print("提示 (Note)         : Stage-1 target_exposure 为咨询值；实际权益/现金仓位以下方模型权重输出为准")
    print("=" * 110)

    # 2. Load Universe (including any held symbols outside the universe list)
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
    # (L2.3) `expected_last_session=as_of` is what turns the cache gate from "we synced
    # today" into "this session's bar is on disk" (design §6/§7). The bounded re-probe
    # lives in cli_utils.load_live_universe; freshness JUDGEMENT stays in
    # common/freshness.py (assert_fresh_universe below).
    universe, probe_attempts = load_live_universe(
        args,
        symbols_to_load,
        start_date,
        as_of_date,
        clock_ctx=clock_ctx,
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, as_of_date)
    # (L2.3) Hard freshness assertion (design §8 --strict-freshness): a symbol whose
    # latest bar is not the as-of session aborts the run before ANY price, weight or
    # share count is computed. `exclude={cash_proxy}` keeps "no frame at all" (a
    # legitimate cash-proxy fallback) out of the staleness verdict.
    freshness_summary = assert_fresh_universe(
        universe,
        as_of_date,
        strict=clock_ctx.freshness_strict,
        expected_symbols=symbols_to_load,
        exclude=(cash_proxy,),
        stage="stage2",
    )
    persist_run_metadata(
        args.output_dir,
        clock_ctx,
        freshness_summary=freshness_summary,
        probe_attempts=probe_attempts,
        extra={"date": as_of_date, "stage": "stage2", "run_mode": run_mode},
    )

    enable_intraday_5m = getattr(args, "enable_intraday_5m", False) or bool(
        resolve_blend_param(params, "enable_intraday_5m", False, bool)
    )
    if enable_intraday_5m:
        try:
            from common.intraday_features import (
                IntradayFeatureConfig,
                load_intraday_5m_universe,
                enrich_universe_with_intraday_features,
            )
            provider = get_data_provider(args.data_provider, **build_data_kwargs(args))
            cutoff_t = getattr(args, "cutoff_time", "14:15") or "14:15"
            universe_5m = load_intraday_5m_universe(
                symbols_to_load,
                start=start_date,
                end=as_of_date,
                data_provider=provider,
                cutoff_time=cutoff_t,
            )
            universe = enrich_universe_with_intraday_features(
                universe, universe_5m, config=IntradayFeatureConfig(cutoff_time=cutoff_t), synthesize_intraday_bar=True
            )
            # (2026-10-22 incident, Fix 1) This line used to print
            # "成功加载并聚合 5m 日内特征 ... 标的数: {len(universe)}" -- a SUCCESS
            # message whose count was the DAILY symbol count. On 2026-10-22 the
            # configured provider (FuyaoDataProvider) raises on any
            # interval != "1d", so EVERY 5m fetch failed, the 5m universe was
            # empty, enrich_universe_with_intraday_features early-returned an
            # un-enriched frame, and downstream VWAP/Chan-5m fields were all
            # None -- while this line still claimed 20 symbols of intraday
            # microstructure had been aggregated. Report what actually arrived:
            # count symbols with a NON-EMPTY 5m frame and the real last 5m
            # timestamp, and say WARN + the reason when there are none.
            n_5m_ok = sum(
                1
                for _s, _df in universe_5m.items()
                if _df is not None and not _df.empty
            )
            if n_5m_ok == 0:
                print(
                    f"[WARN] 5m 日内特征不可用：{len(symbols_to_load)} 个标的的 5m 数据帧全部为空 "
                    f"(provider={args.data_provider}, 截断时点={cutoff_t})。"
                    "常见原因：provider does not support 5m (该数据源仅支持日线 interval='1d')。"
                    "后续 intraday_vwap / vwap_premium_pct / chan_5m_state 等日内字段将为空值，"
                    "Stage-2 仅按日线信号生成工单；请勿把本行理解为日内特征已生效。"
                )
            else:
                _last_5m_ts = max(
                    _df.index.max().to_pydatetime()
                    for _df in universe_5m.values()
                    if _df is not None and not _df.empty
                )
                print(
                    f"[INFO] 5m 日内特征已聚合: 有效标的数 {n_5m_ok}/{len(symbols_to_load)} "
                    f"(截断时点: {cutoff_t}, 最新 5m K 线: {_last_5m_ts})"
                )
                if n_5m_ok < len(symbols_to_load):
                    _missing_5m = sorted(
                        _s for _s in symbols_to_load
                        if _s not in universe_5m or universe_5m[_s] is None or universe_5m[_s].empty
                    )
                    print(
                        f"[WARN] 以下 {len(_missing_5m)} 个标的缺少 5m 数据，日内字段为空: "
                        f"{', '.join(_missing_5m[:20])}{' ...' if len(_missing_5m) > 20 else ''}"
                    )
        except Exception as exc:
            print(f"[WARN] Failed to load 5m intraday features in Stage 2 runner: {exc}")

    # Check if as_of_date is an open trading day
    as_of_dt = pd.Timestamp(as_of_date)
    as_of_d = as_of_dt.date()
    is_weekend = as_of_dt.weekday() in (5, 6)
    official_cal = get_trading_days_calendar() if getattr(args, "data_provider", "") != "synthetic" else None
    is_calendar_holiday = bool(official_cal and (max(official_cal) >= as_of_d) and (as_of_d not in official_cal))

    has_market_bars = False
    for df_sym in universe.values():
        if df_sym is not None and not df_sym.empty:
            last_dt = df_sym.index[-1]
            last_d = last_dt.date() if hasattr(last_dt, "date") else pd.Timestamp(last_dt).date()
            if last_d == as_of_d:
                has_market_bars = True
                break

    is_market_open = (not is_weekend) and (not is_calendar_holiday) and has_market_bars
    if not is_market_open:
        closed_reasons = []
        if is_weekend:
            closed_reasons.append("周末休市 (Weekend)")
        if is_calendar_holiday:
            closed_reasons.append("法定节假日 (Exchange Holiday)")
        if not has_market_bars:
            closed_reasons.append("当日无交易行情K线 (No Market Bar)")
        closed_desc = " / ".join(closed_reasons) if closed_reasons else "非交易日"
        print("\n" + "=" * 110)
        print(f"[MARKET CLOSED / 休市提示] 今日 ({as_of_date}) 为非交易日/休市日 ({closed_desc})。")
        print("                      UPLSM 仓位状态机与确认门禁计数器将全额冻结维持，不推进确认期、锁定期与冷却期。")
        print("=" * 110 + "\n")

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

    # (2026-10-22 incident) Surface the MODEL's executable equity/cash split so
    # it is clearly distinguishable from the Stage-1 advisory target_exposure.
    # The model's equity weight is the sum of risky-symbol targets; the cash
    # weight is the cash proxy target (plus any residual to 100%).
    model_equity_target = float(current_targets.drop(cash_proxy, errors="ignore").sum())
    model_cash_target = float(current_targets.get(cash_proxy, 0.0))
    model_residual = max(0.0, 1.0 - model_equity_target - model_cash_target)
    print(f"\n[INFO] 模型目标仓位 (Model Targets): 权益 {model_equity_target:.1%} | 现金停泊 {model_cash_target:.1%} | 未分配 {model_residual:.1%}")
    print(f"       (Stage-1 target_exposure 仅为咨询值；实际执行以本行模型权益/现金拆分为准)")

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
            raw_user_holdings,
            universe,
            args.portfolio_value,
            cash_proxy,
            as_of_date=as_of_date,
            # One switch, two spellings: `clock_ctx.freshness_strict` already folds
            # `--no-strict-freshness` and `--allow-stale-prices` into one boolean, so the
            # mark-to-market path cannot honour one flag while `assert_fresh_universe`
            # honours the other.
            allow_stale_prices=not clock_ctx.freshness_strict,
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

    # Resolve exit cooldown bars since last full exit for each symbol
    exit_cooldown = resolve_exit_cooldown(
        all_symbols,
        account_state=account_state,
        output_dir=args.output_dir,
        as_of_date=as_of_date,
    )

    # Detect macro thrust & volume breakout
    macro_dict = health_report.get("market_macro", {})
    thrust_active = (
        bool(macro_dict.get("thrust_active", False))
        or bool(health_report.get("thrust_active", False))
        or float(macro_dict.get("thrust", 0.0) or 0.0) >= 0.60
        or float(macro_dict.get("breadth_thrust_10d", 0.0) or 0.0) >= 0.60
        or float(health_report.get("breadth_thrust_10d", 0.0) or 0.0) >= 0.60
    )

    # Precompute sub-strategy signals for blend strategies
    sub_strategy_weights_by_symbol: Dict[str, Dict[str, float]] = {}
    if hasattr(template, "get_sub_strategies"):
        try:
            sub_strats = template.get_sub_strategies(universe, params)
            for sname, s_strat in sub_strats.items():
                w_sp = s_strat.generate_weights(universe, params)
                if not w_sp.empty:
                    rebal_sub = latest_rebalance_rows(w_sp)
                    if not rebal_sub.empty:
                        last_w = rebal_sub.iloc[-1]
                        for sym_k, val_k in last_w.items():
                            if pd.notna(val_k):
                                if sym_k not in sub_strategy_weights_by_symbol:
                                    sub_strategy_weights_by_symbol[sym_k] = {}
                                sub_strategy_weights_by_symbol[sym_k][sname] = round(float(val_k), 4)
        except Exception:
            pass

    # Sector holding counts & throttle limit
    sector_counts: Dict[str, int] = {}
    for sym_c, w_c in current_series.items():
        if sym_c != cash_proxy and w_c > 1e-4:
            sec_c = _get_symbol_sector(sym_c)
            sector_counts[sec_c] = sector_counts.get(sec_c, 0) + 1
    max_assets_per_sector = int(params.get("max_assets_per_sector", 3))

    breadth_val = float(macro_dict.get("breadth", health_report.get("breadth", 0.50)) or 0.50)
    thrust_val = float(macro_dict.get("thrust", health_report.get("thrust", 0.50)) or 0.50)
    macro_passed = not (circuit_tier == "TIER_3_HALT" or freeze_rem > 0)

    updated_lifecycle_states: Dict[str, Any] = {}
    rows = []
    stale_price_symbols: List[str] = []
    for sym in all_symbols:
        tgt_w = float(target_series[sym])
        # (2026-10-22 incident, Fix 6) FREEZE the raw strategy target here, before
        # step_position_lifecycle() overwrites `tgt_w` with
        # `decision.target_weight`. Thrust attribution used to subtract the
        # sub-strategy total from the POST-lifecycle target, so a symbol whose
        # allocation came from the macro breadth-thrust bucket but whose
        # lifecycle decision later flattened it (PENDING_ENTRY confirmation
        # pending, inertia filter, tenure lock) showed a residual of exactly 0.0
        # and `macro_breadth_thrust` was silently dropped from
        # `detail_reason.dimension_signals` -- the ticket then claimed the symbol
        # had no thrust allocation at all. This is AUDIT metadata only: every
        # downstream share/trade computation keeps using the lifecycle-adjusted
        # `tgt_w` exactly as before.
        raw_tgt_w = tgt_w
        cur_w = float(current_series[sym])
        delta_w = tgt_w - cur_w
        abs_delta = abs(delta_w)

        # Get latest close price -- and never silently accept a bar that is not
        # the as_of_date bar (2026-10-22 incident, Fix 2: five held symbols were
        # priced at the 2026-10-21 close with no warning). All freshness policy
        # lives in common/freshness.py; the "no data at all" fallback stays here
        # because a missing frame and a stale frame are different conditions.
        price_stale = False
        price_bar_date: Optional[str] = None
        if sym in universe and not universe[sym].empty:
            price = latest_close(universe, sym, as_of_date, strict=clock_ctx.freshness_strict)
            price_bar_date = last_bar_session(universe[sym])
            price_stale = is_stale_bar(universe[sym], as_of_date)
            if price_stale and sym not in stale_price_symbols:
                stale_price_symbols.append(sym)
        else:
            price = 1.0  # Cash proxy default
            # A non-cash symbol with no frame at all is priced at a placeholder
            # 1.0 -- just as fabricated as a stale close, so it is flagged too
            # (but not raised: the loader already warned / skipped it, and the
            # cash proxy legitimately has no frame when absent from the universe).
            if sym != cash_proxy:
                price_stale = True
                if sym not in stale_price_symbols:
                    stale_price_symbols.append(sym)
                print(f"[WARN] 标的 {sym} 无可用行情数据，价格以占位值 1.0 计; 工单该行标记 price_stale=True。")

        # Calculate lot-rounded shares for equities
        sym_lot = resolve_lot_size(sym, args.lot_size)
        current_val = cur_w * args.portfolio_value
        current_shares = (
            current_shares_dict.get(sym, int(np.round(current_val / price)))
            if (price > 0 and current_val > 0)
            else current_shares_dict.get(sym, 0)
        )

        is_circuit_halt = (circuit_tier == "TIER_3_HALT" or freeze_rem > 0)
        bars_since_exit = exit_cooldown.get(sym, account_state.get("exit_bars_ago", {}).get(sym, 999))
        reentry_cooldown_bars = getattr(args, "reentry_cooldown_bars", 2)

        if sym == cash_proxy:
            action = "SWEEP_CASH" if delta_w > 0 else "RELEASE_CASH"
            reason = "流动性闲置资金归集/释放"
            fast_path_badge = None
            decision_detail = {}
            held_age = 0
            is_tenure_locked = False
            # Cash sweeping is not a gated equity decision.
            blocking_reason = BlockingReason.NONE
        else:
            # Check volume breakout ratio and ATR
            cur_v = 0.0
            ma_v = 0.0
            vol_ratio = 1.0
            sym_atr = None
            if sym in universe and not universe[sym].empty:
                df_sym = universe[sym]
                if "Volume" in df_sym.columns:
                    v_series = df_sym["Volume"].dropna()
                    if len(v_series) > 0:
                        cur_v = float(v_series.iloc[-1])
                        ma_v = float(v_series.iloc[-20:].mean()) if len(v_series) >= 1 else cur_v
                        if ma_v > 0:
                            vol_ratio = cur_v / ma_v
                if all(col in df_sym.columns for col in ("High", "Low", "Close")) and len(df_sym) >= 14:
                    try:
                        from common.indicators import atr
                        sym_atr = float(atr(df_sym, 14).iloc[-1])
                    except Exception:
                        sym_atr = None

            # Determine market
            if sym.endswith(".SH") or sym.endswith(".SZ") or sym.endswith(".BJ"):
                asset_market = "china"
            elif sym.endswith(".HK") or (sym.isdigit() and len(sym) in (4, 5)):
                asset_market = "hk"
            else:
                asset_market = "china" if getattr(args, "market", None) == "china" else "us"

            # Hydrate or restore AssetLifecycleState
            if sym in lifecycle_states_raw:
                state = AssetLifecycleState.from_dict(lifecycle_states_raw[sym])
                state.current_weight = cur_w
                # (2026-10-22 incident) Capture the actual brokerage weight as
                # a pre-trade snapshot BEFORE step_position_lifecycle mutates
                # `current_weight` to the post-rebalance target. Without this,
                # account_state.json persists the TARGET as if it were the
                # broker weight (see docs/version.md §8).
                state.broker_weight = cur_w
                if entry_prices.get(sym, 0.0) > 0 and state.entry_price <= 0:
                    state.entry_price = float(entry_prices.get(sym, 0.0))
                if state.entry_price > 0:
                    state.peak_price = max(state.peak_price, state.entry_price, price if current_shares > 0 else 0.0)
            else:
                cur_sh = current_shares
                is_held = (cur_sh > 0) or (cur_w > 1e-6)
                held_b = holding_bars.get(sym, 1 if is_held else 0)
                if is_held:
                    state_enum = PositionState.HELD_GESTATION if held_b < 1 else PositionState.HELD_ACTIVE
                elif bars_since_exit < reentry_cooldown_bars:
                    state_enum = PositionState.COOLDOWN
                elif entry_signal_streak.get(sym, 0) > 0:
                    state_enum = PositionState.PENDING_ENTRY
                else:
                    state_enum = PositionState.FLAT

                cooldown_rem = max(0, reentry_cooldown_bars - bars_since_exit) if state_enum == PositionState.COOLDOWN else 0
                entry_p = float(entry_prices.get(sym, 0.0) or 0.0)
                peak_p = max(entry_p, price) if is_held and price > 0 else entry_p
                state = AssetLifecycleState(
                    symbol=sym,
                    state=state_enum,
                    held_bars=held_b,
                    entry_streak=int(entry_signal_streak.get(sym, 0)),
                    exit_streak=int(zero_target_streak.get(sym, 0)),
                    cooldown_remaining=cooldown_rem,
                    entry_price=entry_p,
                    peak_price=peak_p,
                    current_weight=cur_w,
                    # (2026-10-22 incident) New-state creation must also capture
                    # the actual brokerage weight snapshot (see hydration branch).
                    broker_weight=cur_w,
                )

            # Pre-execution brokerage reconciliation:
            # If the brokerage account has zero shares/weight but UPLSM thinks it is holding,
            # reconcile to FLAT or COOLDOWN to prevent phantom position locks.
            if (current_shares == 0 or cur_w <= 1e-6) and state.is_holding():
                state.state = PositionState.COOLDOWN if (state.cooldown_remaining > 0 or bars_since_exit < reentry_cooldown_bars) else PositionState.FLAT
                if state.state == PositionState.COOLDOWN and state.cooldown_remaining <= 0:
                    state.cooldown_remaining = max(1, reentry_cooldown_bars - bars_since_exit)
                state.held_bars = 0
                state.current_weight = 0.0
                state.entry_price = 0.0
                state.peak_price = 0.0
                state.entry_streak = 0
                state.exit_streak = 0

            decision = step_position_lifecycle(
                state=state,
                raw_target_weight=tgt_w,
                current_price=price,
                cfg=lifecycle_cfg,
                market=asset_market,
                current_volume=cur_v if cur_v > 0 else None,
                ma20_volume=ma_v if ma_v > 0 else None,
                market_breadth=thrust_val if thrust_active else breadth_val,
                atr=sym_atr,
                is_emergency_halt=is_circuit_halt,
                is_market_open=is_market_open,
            )

            action = decision.action
            tgt_w = decision.target_weight
            delta_w = tgt_w - cur_w
            abs_delta = abs(delta_w)
            reason = decision.reason
            fast_path_badge = None
            decision_detail = decision.detail_reason
            # (L2.24) The lifecycle machine's own structured verdict. Every
            # override below that changes `action` MUST also restate this, so a
            # row can never claim one gate while having been decided by another.
            blocking_reason = decision.blocking_reason

            # (L2.23) NOT evaluated on a closed market: the script-level
            # min-entry relabel below is a SIGNAL-filter story ("目标未达门槛"),
            # which would read as "a signal was evaluated and rejected" on a day
            # nothing was evaluated at all. The economics are identical either way
            # (both write tgt_w = 0.0 for a flat symbol), so skipping it on a
            # freeze day is a pure semantics fix.
            is_market_closed_freeze = (
                coerce_blocking_reason(decision.blocking_reason)
                is BlockingReason.MARKET_CLOSED_FREEZE
            )

            if (
                not is_market_closed_freeze
                and cur_w == 0.0
                and 0.0 < target_series[sym] < min_entry_thresh
            ):
                action = "HOLD_FILTERED"
                tgt_w = 0.0
                delta_w = 0.0
                abs_delta = 0.0
                blocking_reason = BlockingReason.MIN_ENTRY_THRESHOLD
                reason = f"目标仓位未达最低建仓/调仓门槛 (目标 {target_series[sym]:.1%} < 门槛 {min_entry_thresh:.0%}，防微量试错损耗)"

            if decision.fast_path_triggered:
                fast_path_badge = f"⚡[{decision.fast_path_triggered}]"
                if fast_path_badge not in reason:
                    reason = f"{reason} {fast_path_badge}"

            if action == "COOLDOWN_BLOCKED":
                # `action` is widened to the shared HOLD_FILTERED label, but the
                # reason stays distinguishable through `blocking_reason`
                # (L2.24) -- that is the whole point of the enum.
                action = "HOLD_FILTERED"
                blocking_reason = BlockingReason.COOLDOWN_BLOCKED

            # (2026-10-22 incident, Fix 4a) Explicit allow-list, NOT an
            # implicit "everything that is not HOLD". CANCEL_PENDING must never
            # collapse into NO_POSITION: NO_POSITION rows are dropped from the
            # ticket a few lines below, which is precisely how 601857.SH became
            # invisible. Only a genuinely flat, never-signalled HOLD is a
            # no-position row.
            if (
                cur_w == 0.0
                and tgt_w == 0.0
                and action in ("HOLD", "COOLDOWN_BLOCKED")
                and not state.is_holding()
            ):
                action = "NO_POSITION"

            held_age = state.held_bars if state.is_holding() else 0
            is_tenure_locked = bool(state.held_bars < min_holding_bars if state.is_holding() else False)

            # (2026-10-22 incident, belt-and-suspenders) Fix 1 already gates
            # partial-trim SELLs on `can_sell` inside position_lifecycle.py.
            # This is a SECOND defensive layer for any residual code path that
            # still emits a SELL while the asset is tenure-locked: a positive
            # target weight (partial trim) is downgraded to HOLD_FILTERED and
            # the weight is pinned to the current holding so no SELL order is
            # ever generated during the minimum-holding lock.
            #
            # Full exits (tgt_w <= 0) are intentionally NOT re-blocked here:
            #  - Stop-loss / emergency-halt SELLs (tgt_w == 0) are emitted by
            #    position_lifecycle.py REGARDLESS of the tenure lock (the lock
            #    is explicitly EXEMPTED for risk exits — see
            #    common/position_lifecycle.py), so they must flow through
            #    untouched and liquidate today.
            #  - Signal-driven full exits on a locked position are already
            #    downgraded to HOLD_FILTERED by position_lifecycle.py's
            #    `can_sell` gate, so they never reach this layer as a SELL.
            #
            # (2026-10-22 incident) The tenure lock is a China T+1 gestation
            # concept; `can_sell` inside position_lifecycle.py is
            # `market != "china" or held_bars >= min_holding_bars`. The
            # `is_tenure_locked` flag above is held_bars-only, so it must be
            # re-scoped to `asset_market == "china"` here to avoid over-blocking
            # partial-trim SELLs for non-China (US/HK) strategies where the
            # lock does not apply.
            if is_tenure_locked and asset_market == "china" and action == "SELL" and tgt_w > 0:
                action = "HOLD_FILTERED"
                tgt_w = cur_w
                delta_w = 0.0
                abs_delta = 0.0
                # (L2.24) The SELL was vetoed by the tenure lock, not by inertia,
                # not by a closed market.
                blocking_reason = BlockingReason.TENURE_LOCKED
                # Keep the persisted lifecycle weight consistent with the
                # downgraded no-trade decision (no SELL order is emitted, so
                # current_weight must remain the broker weight, not the
                # unexecuted trim target that step_position_lifecycle may have
                # already staged into state.current_weight).
                state.current_weight = cur_w
                lock_reason = f"未满最低持仓周期 (持仓 {state.held_bars} 日 < 最低 {min_holding_bars} 日，锁定防洗盘)"
                reason = f"{lock_reason} | 原信号: {reason}" if reason and reason != lock_reason else lock_reason

            updated_lifecycle_states[sym] = state.to_dict()

        # Determine board price limits and 5m microstructure indicators (R10 & R11)
        sym_name = symbol_names.get(sym, sym)
        if is_placeholder_stock_name(sym_name, sym):
            sym_name = _FALLBACK_STOCK_NAMES.get(sym, sym)
        is_st = ("ST" in sym_name.upper()) if sym_name else False
        is_etf = (sym.startswith("51") or sym.startswith("15") or sym.startswith("56") or sym.startswith("58") or sym.startswith("16"))
        decimals = 3 if is_etf else 2
        tick_tol = 0.001 if is_etf else 0.01

        if is_st:
            limit_pct = 0.05
        elif sym.endswith(".BJ") or sym.startswith("43") or sym.startswith("83") or sym.startswith("87") or sym.startswith("92"):
            limit_pct = 0.30
        elif sym.startswith("688") or sym.startswith("300") or sym.startswith("301"):
            limit_pct = 0.20
        else:
            limit_pct = 0.10

        df_sym_all = universe.get(sym, pd.DataFrame())
        is_new_stock = (len(df_sym_all) <= 5)
        if is_new_stock:
            limit_pct = 1.0  # exempt

        prev_close = price
        if len(df_sym_all) >= 2:
            prev_close = float(df_sym_all["Close"].iloc[-2])

        p_limit_up = round(prev_close * (1.0 + limit_pct), decimals) if limit_pct < 1.0 else price * 2.0
        p_limit_down = round(prev_close * (1.0 - limit_pct), decimals) if limit_pct < 1.0 else 0.01

        last_locked = 0
        intraday_vwap = None
        vwap_prem_pct = None
        chan_5m_state = "N/A"
        chan_5m_zg = 0.0

        if not df_sym_all.empty:
            last_row = df_sym_all.iloc[-1]
            if "Last_3Bars_Locked_Limit" in last_row and pd.notna(last_row["Last_3Bars_Locked_Limit"]):
                last_locked = int(last_row["Last_3Bars_Locked_Limit"])
            if "VWAP" in last_row and pd.notna(last_row["VWAP"]):
                intraday_vwap = float(last_row["VWAP"])
            if "VWAP_Prem_5m" in last_row and pd.notna(last_row["VWAP_Prem_5m"]):
                vwap_prem_pct = float(last_row["VWAP_Prem_5m"])
            if "Chan_5m_State" in last_row and pd.notna(last_row["Chan_5m_State"]):
                chan_5m_state = str(last_row["Chan_5m_State"])
            if "Chan_5m_ZG" in last_row and pd.notna(last_row["Chan_5m_ZG"]):
                chan_5m_zg = float(last_row["Chan_5m_ZG"])

        execution_warning = None

        trade_val = delta_w * args.portfolio_value
        raw_shares = 0.0
        if sym != cash_proxy and price > 0:
            # (2026-10-22 incident, Fix 4b) CANCEL_PENDING is a RECORD row, not an
            # order: it keeps the existing share count and trades nothing. Listing
            # it explicitly (rather than letting it fall through to the trailing
            # `else`) keeps the "no tradable instruction" guarantee self-evident.
            if action in ("HOLD_FILTERED", "HOLD", "COOLDOWN_BLOCKED", "NO_POSITION", "CANCEL_PENDING"):
                target_shares = current_shares
                delta_shares = 0
                actual_trade_val = 0.0
                raw_shares = 0.0
            elif action == "BUY":
                # Check LIMIT_UP_LOCKED (R10)
                if limit_pct < 1.0 and (price >= p_limit_up - tick_tol or last_locked == 1):
                    action = "HOLD_FILTERED"
                    target_shares = current_shares
                    delta_shares = 0
                    actual_trade_val = 0.0
                    raw_shares = 0.0
                    tgt_w = cur_w
                    delta_w = 0.0
                    abs_delta = 0.0
                    reason = "涨停封板无法买入 (LIMIT_UP_LOCKED)"
                    execution_warning = "LIMIT_UP_LOCKED"
                    # (L2.24) A BUY that was vetoed at the exchange board, not at
                    # the lifecycle machine.
                    blocking_reason = BlockingReason.LIMIT_UP_LOCKED
                else:
                    raw_buy_shares = trade_val / price
                    raw_shares = raw_buy_shares
                    lot_buy_shares = int(np.floor(raw_buy_shares / sym_lot) * sym_lot)
                    delta_shares = max(0, lot_buy_shares)

                    # Check INTRADAY_OVEREXTENDED_TRIM
                    if vwap_prem_pct is not None and vwap_prem_pct > 0.035 and not df_sym_all.empty and df_sym_all.iloc[-1].get("Chan_5m_Sell_Trigger") == 1:
                        trimmed_shares = int(np.floor((delta_shares * 0.5) / sym_lot) * sym_lot)
                        if trimmed_shares < delta_shares:
                            delta_shares = max(0, trimmed_shares)
                            reason = f"{reason} [日内透支追高降档 INTRADAY_OVEREXTENDED_TRIM]"

                    target_shares = current_shares + delta_shares
                    actual_trade_val = delta_shares * price
            elif action == "SELL":
                # Check LIMIT_DOWN_ILLIQUID_RISK (R10 & R14)
                if limit_pct < 1.0 and (price <= p_limit_down + tick_tol or last_locked == -1 or cur_v == 0.0):
                    execution_warning = "LIMIT_DOWN_ILLIQUID_RISK"
                    reason = f"{reason} [跌停封死/流动性枯竭警示 LIMIT_DOWN_ILLIQUID_RISK]"

                if tgt_w <= 1e-6:
                    raw_shares = -float(current_shares)
                    delta_shares = -current_shares
                    target_shares = 0
                    actual_trade_val = delta_shares * price
                else:
                    raw_sell_shares = abs(trade_val) / price
                    raw_shares = -raw_sell_shares
                    lot_sell_shares = int(np.ceil(raw_sell_shares / sym_lot) * sym_lot)
                    lot_sell_shares = min(lot_sell_shares, current_shares)
                    delta_shares = -lot_sell_shares
                    target_shares = current_shares + delta_shares
                    actual_trade_val = delta_shares * price
            else:
                target_shares = current_shares
                delta_shares = 0
                actual_trade_val = 0.0
                raw_shares = 0.0
        else:
            target_shares = 0
            current_shares = 0
            delta_shares = 0
            actual_trade_val = 0.0
            raw_shares = 0.0

        # Suggested limit prices (R11)
        suggested_limit_price = price
        passive_support_limit_price = price
        if sym != cash_proxy and price > 0:
            if action == "BUY":
                suggested_limit_price = min(p_limit_up, round(price * 1.002, decimals))
                if intraday_vwap and intraday_vwap > 0:
                    support_anchor = max(intraday_vwap, chan_5m_zg) if chan_5m_zg > 0 else intraday_vwap
                    passive_support_limit_price = min(
                        round(price * 1.002, decimals),
                        round(support_anchor * 1.005, decimals),
                    )
                else:
                    passive_support_limit_price = suggested_limit_price
            elif action == "SELL":
                if "止损" in reason or "HALT" in reason or "EMERGENCY" in reason:
                    suggested_limit_price = max(p_limit_down, round(price * 0.997, decimals))
                else:
                    suggested_limit_price = max(p_limit_down, round(price * 0.998, decimals))
                passive_support_limit_price = suggested_limit_price

        # Post-calculation lot-rounding zero shares liquidation:
        # When current_shares > 0 and after lot rounding target_shares == 0
        # (whether raw tgt_w <= 1e-6 or because lot rounding rounded it down to 0),
        # the asset has been fully liquidated. Reconcile state to COOLDOWN.
        if sym != cash_proxy and current_shares > 0 and target_shares == 0:
            state.state = PositionState.COOLDOWN
            state.cooldown_remaining = reentry_cooldown_bars
            state.held_bars = 0
            state.current_weight = 0.0
            state.entry_price = 0.0
            state.peak_price = 0.0
            state.last_exit_type = ExitType.NORMAL.value
            state.entry_streak = 0
            state.exit_streak = 0
            updated_lifecycle_states[sym] = state.to_dict()
            held_age = 0
            is_tenure_locked = False
            action = "SELL"
            tgt_w = 0.0
            delta_w = -cur_w
            abs_delta = abs(delta_w)
            # (L2.24) A real liquidation is never labelled as blocked.
            blocking_reason = BlockingReason.NONE
            if not reason or reason == "最优权重微调":
                reason = "目标仓位不足单手或整手归零清仓"
        elif sym != cash_proxy and current_shares == 0 and target_shares == 0 and state.is_holding():
            # If a BUY was attempted from FLAT but lot rounding rounded down to 0 shares,
            # revert lifecycle state back to FLAT to avoid phantom gestation.
            state.state = PositionState.FLAT
            state.held_bars = 0
            state.current_weight = 0.0
            state.entry_price = 0.0
            state.peak_price = 0.0
            state.entry_streak = 0
            state.exit_streak = 0
            updated_lifecycle_states[sym] = state.to_dict()
            held_age = 0
            is_tenure_locked = False

        # Sector throttle and dimension signals
        sec = _get_symbol_sector(sym) if sym != cash_proxy else "cash"
        cur_sec_holdings = sector_counts.get(sec, 0)
        if sym in sub_strategy_weights_by_symbol:
            dim_signals = dict(sub_strategy_weights_by_symbol[sym])
        else:
            dim_signals = {strategy_key: round(tgt_w, 4)}
        sub_total_w = sum(dim_signals.values()) if sym in sub_strategy_weights_by_symbol else 0.0
        # (2026-10-22 incident, Fix 6) Residual thrust attribution must be measured
        # against the PRE-lifecycle target `raw_tgt_w`, not the post-lifecycle
        # `tgt_w`. When the lifecycle machine flattens an allocation (PENDING_ENTRY
        # still confirming, inertia filter, tenure lock) `tgt_w` becomes 0.0, so
        # `max(0.0, tgt_w - sub_total_w)` was always 0.0 for exactly the symbols
        # whose thrust allocation had been suppressed -- the audit block claimed
        # `macro_breadth_thrust` did not exist, hiding the reason the symbol was
        # silent. `raw_tgt_w` is AUDIT-ONLY: it never enters trade_val /
        # delta_shares / any order field (grep: the variable is read solely here
        # and in the `thrust_attribution` audit block below).
        # Disclosed limitation: for symbols with no sub-strategy decomposition we
        # cannot split the residual further, so the whole raw target is attributed
        # to the thrust bucket (pre-existing behaviour, unchanged).
        macro_thrust_w = max(0.0, raw_tgt_w - sub_total_w)
        if macro_thrust_w > 0.001 and thrust_active:
            dim_signals["macro_breadth_thrust"] = round(macro_thrust_w, 4)

        # (2026-10-22 incident, Fix 2) A stale close must be visible in the row's
        # own text, not only in the machine-readable `price_stale` column: the
        # printed/CSV ticket is what a human actually executes from.
        if price_stale:
            _stale_tag = (
                f" [陈旧价格 PRICE_STALE: 价格K线={price_bar_date or '无行情'} != as_of={as_of_date}]"
            )
            if "PRICE_STALE" not in reason:
                reason = f"{reason}{_stale_tag}"

        # Formulate human-readable summary reason
        summary_reason = reason
        if not summary_reason:
            if action == "BUY":
                summary_reason = f"目标仓位增至 {tgt_w:.1%} (变动 {delta_w:+.1%})，突破惯性阈值并满足入场准则，执行买入"
            elif action == "SELL":
                summary_reason = f"目标仓位减至 {tgt_w:.1%} (变动 {delta_w:+.1%})，满足最小持仓锁定，执行卖出"
            elif action == "HOLD_FILTERED":
                summary_reason = f"调仓未超惰性阈值 (< {min_trade_thresh:.0%})"
            else:
                summary_reason = "最优目标权重维持"

        if action == "BUY" and dim_signals.get("macro_breadth_thrust", 0.0) >= min_trade_thresh:
            if "[宏观动量冲力预部署]" not in summary_reason:
                summary_reason = f"{summary_reason} [宏观动量冲力预部署]"

        # Structured multi-dimensional detail reason
        detail_reason = {
            "summary_reason": summary_reason,
            # (L2.24) Same verdict as the ticket column, repeated inside the audit
            # block so a reader that only keeps `detail_reason` is not forced back
            # to guessing from `target_weight`.
            "blocking_reason": coerce_blocking_reason(blocking_reason).value,
            # (L2.25) Explicit audit verdict for "did this row liquidate the
            # position?". Derived by the single shared predicate
            # (`common.position_lifecycle.is_liquidation_event`, i.e. `action ==
            # "SELL"`), NOT by comparing `target_weight` with 0.0: at least three
            # gates write a 0.0 target while trading absolutely nothing
            # (min-entry filter / PENDING_ENTRY confirmation / market-closed
            # freeze), and `CANCEL_PENDING` is a never-held record. Audit,
            # attribution and UI consumers read this key (or the pair
            # `action` + `blocking_reason`) instead of re-deriving it.
            "is_liquidation": bool(is_liquidation_event(action, blocking_reason)),
            "dimension_signals": dim_signals,
            "lifecycle_state": state.to_dict() if sym != cash_proxy else {},
            "lifecycle_decision": decision_detail,
            "inertia_check": {
                "delta_weight": round(delta_w, 4),
                "threshold": float(min_trade_thresh),
                "triggered": bool(abs_delta >= min_trade_thresh),
            },
            "holding_lock": {
                "bars_held": int(held_age),
                "min_holding_bars": int(min_holding_bars),
                "locked": bool(is_tenure_locked),
            },
            "cooldown_status": {
                "bars_since_exit": int(bars_since_exit),
                "cooldown_remaining": int(state.cooldown_remaining if sym != cash_proxy else 0),
                "min_cooldown_bars": int(reentry_cooldown_bars),
                "in_cooldown": bool(sym != cash_proxy and state.state == PositionState.COOLDOWN),
            },
            "sector_throttle": {
                "sector": sec,
                "max_allowed": max_assets_per_sector,
                "current_sector_holdings": cur_sec_holdings,
                "throttled": False,
            },
            "macro_gate": {
                "passed": bool(macro_passed),
                "breadth": round(breadth_val, 4),
                "thrust": round(thrust_val, 4),
            },
            "lot_rounding": {
                "target_shares_raw": round(float(raw_shares), 2),
                "shares": int(abs(delta_shares)),
                "lot_size": int(sym_lot),
            },
        }
        if fast_path_badge:
            detail_reason["fast_path_badge"] = fast_path_badge
            detail_reason["fast_path_triggered"] = decision.fast_path_triggered

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
            "shares": int(abs(delta_shares)),
            "trade_value": actual_trade_val,
            "trade_value_rmb": actual_trade_val,
            "holding_bars": held_age,
            "reason": reason,
            "summary_reason": summary_reason,
            "detail_reason": detail_reason,
            "strategy_version": strat_ver,
            "base_version": base_ver,
            "execution_warning": execution_warning,
            # (2026-10-22 incident, Fix 2) Explicit per-row freshness mark so a
            # stale close can never hide inside a well-formed ticket row. True
            # either because the bar is not the as_of_date session (only possible
            # with --allow-stale-prices, otherwise StalePriceError aborted the
            # run) or because the symbol had no frame at all and was priced at a
            # placeholder 1.0.
            "price_stale": bool(price_stale),
            "price_bar_date": price_bar_date,
            "intraday_vwap": intraday_vwap,
            "vwap_premium_pct": vwap_prem_pct,
            "chan_5m_state": chan_5m_state,
            "suggested_limit_price": suggested_limit_price,
            "passive_support_limit_price": passive_support_limit_price,
            # (L2.24) Appended LAST on purpose: the CSV gains one TRAILING column
            # and the JSON gains one key, so no existing column changes position
            # or meaning and no schema version bump is needed (a reader that does
            # not know this column ignores it; a pre-L2.24 archive simply lacks it
            # and reads back as `NONE` via `coerce_blocking_reason`). This is the
            # structured "why was nothing traded" verdict that replaces guessing
            # intent from `target_weight == 0.0`.
            "blocking_reason": coerce_blocking_reason(blocking_reason).value,
        })

    # Purchasing power constraint: total buys cannot exceed available cash + sales proceeds
    initial_cash_val = current_series.get(cash_proxy, 0.0) * args.portfolio_value
    # Double freeze R14: exclude unexecutable LIMIT_DOWN_ILLIQUID_RISK sell orders from purchasing power
    total_sales_proceeds = abs(sum(
        r["trade_value"] for r in rows
        if r["action"] == "SELL" and r["symbol"] != cash_proxy and r.get("execution_warning") != "LIMIT_DOWN_ILLIQUID_RISK"
    ))
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
                r["shares"] = int(r["delta_shares"])
                r["target_shares"] = r["current_shares"] + r["delta_shares"]
                r["trade_value"] = r["delta_shares"] * r["price"]
                r["trade_value_rmb"] = r["trade_value"]
                r["target_weight"] = (r["target_shares"] * r["price"]) / args.portfolio_value
                r["delta_weight"] = r["target_weight"] - r["current_weight"]
                if "detail_reason" in r and isinstance(r["detail_reason"], dict):
                    if "lot_rounding" in r["detail_reason"]:
                        r["detail_reason"]["lot_rounding"]["shares"] = int(r["delta_shares"])
                        r["detail_reason"]["lot_rounding"]["target_shares_raw"] = round(scaled_val / r["price"], 2)
                    if "inertia_check" in r["detail_reason"]:
                        r["detail_reason"]["inertia_check"]["delta_weight"] = round(r["delta_weight"], 4)

    order_df = pd.DataFrame(rows)

    # Reconcile cash proxy: actual post-trade cash = initial cash - net equity purchases
    cash_mask = order_df["symbol"] == cash_proxy
    if cash_mask.any():
        # Double freeze R14: exclude LIMIT_DOWN_ILLIQUID_RISK sell orders from equity_trades_val so idle cash sweep does not overdraft
        valid_equity_mask = (~cash_mask) & (order_df.get("execution_warning") != "LIMIT_DOWN_ILLIQUID_RISK")
        equity_trades_val = order_df.loc[valid_equity_mask, "trade_value"].sum()
        actual_post_trade_cash = initial_cash_val - equity_trades_val
        actual_cash_weight = max(0.0, actual_post_trade_cash / args.portfolio_value)
        order_df.loc[cash_mask, "target_weight"] = actual_cash_weight
        order_df.loc[cash_mask, "delta_weight"] = actual_cash_weight - current_series.get(cash_proxy, 0.0)
        order_df.loc[cash_mask, "trade_value"] = -equity_trades_val
        order_df.loc[cash_mask, "trade_value_rmb"] = -equity_trades_val

    # Sort: SELLS FIRST -> BUYS SECOND -> HOLDS; drop NO_POSITION rows (0% on both sides)
    # (2026-10-22 incident, Fix 4) CANCEL_PENDING rows are RECORDS, not orders,
    # and must survive this filter: a symbol whose entry confirmation was
    # cancelled is exactly the case an operator needs to see. Dropping it (as the
    # NO_POSITION reclassification used to) is how 601857.SH vanished from the
    # ticket entirely.
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
            warn_tag = " ⚠️ [LIMIT_DOWN_ILLIQUID_RISK / 资金已冻结]" if r.get("execution_warning") == "LIMIT_DOWN_ILLIQUID_RISK" else ""
            limit_p = r.get("suggested_limit_price", r["price"])
            print(f"  🔴 SELL {sym_col} | 现价: {r['price']:>7.2f} (建议限价: {limit_p:.2f}) | 仓位: {r['current_weight']:>5.1%} -> 目标: {r['target_weight']:>5.1%} ({r['delta_weight']:>+5.1%}) | 卖出: {abs(r['delta_shares']):>6} 股 (~¥{abs(r['trade_value']):>9,.2f}){warn_tag}")

    print("\n[PHASE 2: EXECUTE BUYS SECOND (第二阶段: 买入建仓/加仓资金配置)]")
    if buys.empty:
        print("  今日无需执行任何买入指令 (No buy orders required today).")
    else:
        for _, r in buys.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            price_info = f"建议限价: {r.get('suggested_limit_price', r['price']):.2f}"
            if r.get("passive_support_limit_price") and r.get("passive_support_limit_price") != r.get("suggested_limit_price"):
                price_info += f" (被动支撑: {r['passive_support_limit_price']:.2f})"
            print(f"  🟢 BUY  {sym_col} | 现价: {r['price']:>7.2f} | 仓位: {r['current_weight']:>5.1%} -> 目标: {r['target_weight']:>5.1%} ({r['delta_weight']:>+5.1%}) | 买入: {r['shares']:>6} 股 (~¥{r['trade_value']:>9,.2f}) | {price_info}")
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
    # (L2.23) EXCEPT the market-closed freeze rows, which are pulled out into
    # their own block below: a frozen row means "not evaluated", so rendering it
    # under "维持既有持仓/惰性过滤" as "⚪ HOLD 维持仓位" told the operator the
    # opposite of what happened (and, for a flat symbol, printed a 0.0% "held"
    # position). Splitting is done on the structured `blocking_reason` column
    # (L2.24), never on `target_weight == 0.0`.
    _blocking_col = (
        order_df["blocking_reason"]
        if "blocking_reason" in order_df.columns
        else pd.Series(BlockingReason.NONE.value, index=order_df.index)
    )
    # NOTE (load-bearing): do NOT pre-coerce the whole column into a Series first.
    # `BlockingReason` subclasses `str`, so `Series.map(coerce_blocking_reason)`
    # returns a *plain-string* Series -- pandas drops the enum identity -- and a
    # subsequent `is BlockingReason.X` comparison would then be silently False for
    # every row (this exact refactor briefly made Phase 3a render nothing at all).
    # Coercing element-by-element in a list comprehension keeps real members.
    _blocking_values = [coerce_blocking_reason(v) for v in _blocking_col.tolist()]
    _is_freeze_row = pd.Series(
        [b is BlockingReason.MARKET_CLOSED_FREEZE for b in _blocking_values],
        index=order_df.index,
        dtype=bool,
    )
    frozen_holds = holds[holds.index.isin(_is_freeze_row[_is_freeze_row].index)]
    active_holds = holds[~holds.index.isin(frozen_holds.index)]
    # (L2.25) Which rows belong in Phase 3 is decided from the STRUCTURED verdict,
    # never by asking whether `target_weight == 0.0` "means we exited". A gated
    # row (blocking_reason != NONE) is always shown, even at a 0.0 target, because
    # it is a real reason the operator requested; `target_weight > 0` remains ONLY
    # an inclusion rule ("the model still wants this symbol"), never a liquidation
    # verdict; and `action == "HOLD_FILTERED"` keeps pre-L2.24 rows (no
    # blocking_reason column) visible, since for those the action is the only
    # signal available.
    _is_gated_row = pd.Series(
        [b is not BlockingReason.NONE for b in _blocking_values],
        index=order_df.index,
        dtype=bool,
    )
    actual_holds = active_holds[
        (active_holds["current_weight"] > 0)
        | (active_holds["target_weight"] > 0)
        | _is_gated_row.reindex(active_holds.index, fill_value=False)
        | (active_holds["action"] == "HOLD_FILTERED")
    ]
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
            gated = coerce_blocking_reason(r.get("blocking_reason"))
            if gated is not BlockingReason.NONE:
                # (L2.25) An EVALUATED-BUT-GATED row: a signal existed and a gate
                # stopped it. It is NOT "维持仓位" (nothing was re-affirmed) and, for
                # a flat symbol, it is emphatically not a "0.0% held position" --
                # that wording was the display half of the phantom-exit bug class.
                position_txt = (
                    f"持仓不变: {r['current_weight']:>5.1%} -> 目标 {r['target_weight']:>5.1%}"
                    if r['current_shares'] > 0
                    else f"未持仓: 0.0% -> 目标 {r['target_weight']:>5.1%}"
                )
                print(
                    f"  🟡 {blocking_reason_label(gated)} {sym_col} | {position_txt} {shares_info:<10}{tenure_str} | "
                    f"无交易指令 | ({reason})"
                )
            else:
                print(f"  ⚪ HOLD {sym_col} | 维持仓位: {r['target_weight']:>5.1%} {shares_info:<10}{tenure_str} | 持仓市值: ~¥{r['target_weight']*args.portfolio_value:>9,.2f} | ({reason})")

    # (L2.23) Market-closed freeze rows get their OWN semantics: neither "held"
    # nor "filtered". Nothing was evaluated -- no signal, no gate, no counter
    # advanced -- so the operator must not read this as "the model decided to
    # keep the position" (which is what "⚪ HOLD 维持仓位 0.0%" used to say for a
    # flat symbol). The row is still emitted and archived: it is the evidence
    # that the run happened on a non-trading day.
    if not frozen_holds.empty:
        print("\n[PHASE 3a: MARKET CLOSED — NOT EVALUATED (第三阶段附: 休市冻结，未评估任何信号 — 无交易指令)]")
        for _, r in frozen_holds.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            shares_info = f"({int(r['current_shares'])} 股)" if r['current_shares'] > 0 else ""
            print(
                f"  🕒 {blocking_reason_label(r.get('blocking_reason'))} {sym_col} | "
                f"仓位保持不变: {r['target_weight']:>5.1%} {shares_info:<10}| "
                f"持仓市值: ~¥{r['target_weight']*args.portfolio_value:>9,.2f} | "
                f"(休市冻结，未评估任何信号；状态机/确认期/锁定期/冷却期计数器全部冻结，本行不含任何交易指令)"
            )

    # (2026-10-22 incident, Fix 4) PENDING_ENTRY cancellations are printed in
    # their own block so an operator can see that a confirmation period was
    # aborted (and how far it had progressed) WITHOUT mistaking it for an order.
    # Deliberately outside Phase 1/2/3/4: it trades nothing.
    cancel_pending_rows = order_df[order_df["action"] == "CANCEL_PENDING"]
    if not cancel_pending_rows.empty:
        print("\n[PHASE 3b: PENDING ENTRY CANCELLED (第三阶段附: 待确认开仓信号消失 — 仅记录, 非交易指令)]")
        for _, r in cancel_pending_rows.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            lost_streak = (r.get("detail_reason") or {}).get("lifecycle_decision", {}).get("cancelled_entry_streak", 0) \
                if isinstance(r.get("detail_reason"), dict) else 0
            print(
                f"  ⛔ CANCEL_PENDING {sym_col} | 目标仓位已归零 (0.0%), 取消的确认进度: {lost_streak} 日 | "
                f"无买卖指令, 仅登记状态变化 | ({r.get('reason', '')})"
            )

    # (2026-10-22 incident, Fix 2) End-of-ticket staleness banner: impossible to
    # miss even for an operator who only reads the last screen of output.
    if stale_price_symbols:
        print("\n" + "!" * 110)
        print(
            f"[STALE PRICE WARNING / 陈旧价格警告] 以下 {len(stale_price_symbols)} 个标的的价格不是 as_of_date="
            f"{as_of_date} 的收盘价（工单行已标记 price_stale=true）:"
        )
        print(f"    {', '.join(stale_price_symbols)}")
        print("  下单前必须人工核对这些标的最新价；NAV / 权重 / 股数均基于该过期价格计算。")
        print("!" * 110)

    if not cash_rows.empty:
        print("\n[PHASE 4: CASH PROXY & LIQUIDITY MANAGEMENT (第四阶段: 闲置资金对账)]")
        for _, r in cash_rows.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            print(f"  💰 CASH {sym_col} | 闲置现金: {r['target_weight']:>5.1%} (~¥{r['target_weight']*args.portfolio_value:>9,.2f}) | 停泊于 {r['symbol']} / 511880 / 国债逆回购")

    # Update post-trade holdings and state tracking
    post_holdings = {}
    dual_holding_bars = {}
    dual_entry_signal_streak = {}
    dual_zero_target_streak = {}
    dual_entry_prices = {}
    updated_last_exit_date = dict(last_exit_date)

    for s, st_dict in updated_lifecycle_states.items():
        st_obj = AssetLifecycleState.from_dict(st_dict)
        if st_obj.is_holding() or st_obj.held_bars > 0:
            dual_holding_bars[s] = st_obj.held_bars
        if st_obj.entry_streak > 0:
            dual_entry_signal_streak[s] = st_obj.entry_streak
        if st_obj.exit_streak > 0:
            dual_zero_target_streak[s] = st_obj.exit_streak
        if st_obj.entry_price > 0:
            dual_entry_prices[s] = st_obj.entry_price

    # (2026-10-22 incident, Fix 5) A streak RESET used to be invisible: the dict
    # above only ever writes entries whose streak is > 0, so a symbol that fell
    # from 2 back to 0 simply LOST its key. `account_state.json` then read as if
    # that symbol had never been queued at all (601857.SH's 1/2 confirmation was
    # unreadable the next day), and no log line recorded the loss. We now (a)
    # persist an EXPLICIT 0 for any symbol that was queued on entry and is no
    # longer, and (b) log the transition with the value it lost -- so a cancelled
    # or abandoned confirmation is permanently auditable.
    # Backward compatibility: the on-disk schema is unchanged (a symbol -> int
    # dict). Legacy sparse files keep working, because every reader uses
    # `.get(sym, 0) > 0` semantics; nothing interprets a present key as "queued".
    prior_entry_streaks = entry_signal_streak or {}
    for s_prev, v_prev in prior_entry_streaks.items():
        try:
            prev_streak = int(v_prev or 0)
        except (TypeError, ValueError):
            continue
        if prev_streak <= 0:
            continue
        st_now = updated_lifecycle_states.get(s_prev)
        if st_now is None:
            continue
        try:
            now_streak = int(st_now.get("entry_streak", 0) or 0)
        except (TypeError, ValueError):
            now_streak = 0
        if now_streak > 0:
            continue
        dual_entry_signal_streak[s_prev] = 0
        print(f"[INFO] entry_signal_streak reset: {s_prev} (was {prev_streak})")

    for _, r in order_df.iterrows():
        sym = r["symbol"]
        if sym == cash_proxy:
            continue
        tgt_sh = int(r["target_shares"])
        act = r["action"]
        cur_sh = int(r["current_shares"])
        p = float(r["price"])

        if tgt_sh > 0:
            post_holdings[sym] = tgt_sh
            if act == "BUY":
                dual_zero_target_streak.pop(sym, None)
                if sym in updated_lifecycle_states:
                    updated_lifecycle_states[sym]["exit_streak"] = 0
                if cur_sh == 0:
                    dual_holding_bars[sym] = 0
                    if p > 0:
                        dual_entry_prices[sym] = p
                    updated_last_exit_date.pop(sym, None)
                    if sym in updated_lifecycle_states:
                        updated_lifecycle_states[sym]["held_bars"] = 0
                        if p > 0:
                            updated_lifecycle_states[sym]["entry_price"] = p
                            updated_lifecycle_states[sym]["peak_price"] = p
            else:
                if sym not in dual_holding_bars:
                    dual_holding_bars[sym] = int(r.get("holding_bars", 1))
                if sym not in dual_entry_prices and p > 0:
                    dual_entry_prices[sym] = p
        else:
            if act == "BUY":
                dual_zero_target_streak.pop(sym, None)
                if sym in updated_lifecycle_states:
                    updated_lifecycle_states[sym]["exit_streak"] = 0
            # (L2.22, decided 2026-10-23) Cooldown anchoring is now "did a REAL real
            # exit happen AND is the position actually gone?" -- delegated to
            # `should_anchor_reentry_cooldown`. Consequences, all intentional:
            #   - a PARTIAL trim (`act == "SELL"`, `tgt_sh > 0`) no longer anchors:
            #     we still hold the name, so its exit clock must not restart
            #     (previously the `act == "SELL"` leg re-armed the cooldown and
            #     locked the still-held asset out of the next N sessions);
            #   - a closed-session freeze row never anchors (nothing was evaluated);
            #   - the old `tgt_sh == 0` zero-target leg is GONE: no exit may ever be
            #     reverse-inferred from a zero target weight/share count. Only the
            #     pair (action == "SELL", zero shares held afterwards) is an exit --
            #     see `common/position_lifecycle.is_liquidation_event` (L2.24/L2.25).
            if should_anchor_reentry_cooldown(
                act, r.get("blocking_reason"), cur_sh, tgt_sh
            ):
                updated_last_exit_date[sym] = as_of_date
                dual_entry_prices.pop(sym, None)
                dual_holding_bars.pop(sym, None)
                dual_zero_target_streak.pop(sym, None)
                dual_entry_signal_streak.pop(sym, None)
                if sym in updated_lifecycle_states:
                    updated_lifecycle_states[sym]["state"] = PositionState.COOLDOWN.value
                    updated_lifecycle_states[sym]["cooldown_remaining"] = reentry_cooldown_bars
                    updated_lifecycle_states[sym]["held_bars"] = 0
                    updated_lifecycle_states[sym]["entry_price"] = 0.0
                    updated_lifecycle_states[sym]["peak_price"] = 0.0
                    updated_lifecycle_states[sym]["current_weight"] = 0.0
                    updated_lifecycle_states[sym]["exit_streak"] = 0
                    updated_lifecycle_states[sym]["entry_streak"] = 0
                    if not updated_lifecycle_states[sym].get("last_exit_type"):
                        updated_lifecycle_states[sym]["last_exit_type"] = ExitType.NORMAL.value
            elif cur_sh == 0 and tgt_sh == 0:
                dual_holding_bars.pop(sym, None)
                dual_entry_prices.pop(sym, None)

    updated_exit_bars_ago = resolve_exit_cooldown(
        list(updated_last_exit_date.keys()),
        {"last_exit_date": updated_last_exit_date},
        output_dir=args.output_dir,
        as_of_date=as_of_date,
    )

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

    # Persist updated state into account_state.json if available
    if args.account_state_file:
        if isinstance(account_state.get("prior_state"), dict):
            if "lifecycle_states" not in account_state["prior_state"]:
                account_state["prior_state"]["lifecycle_states"] = dict(account_state.get("lifecycle_states", {}))
            if "holding_bars" not in account_state["prior_state"]:
                account_state["prior_state"]["holding_bars"] = dict(holding_bars)
            if "entry_signal_streak" not in account_state["prior_state"]:
                account_state["prior_state"]["entry_signal_streak"] = dict(account_state.get("entry_signal_streak", {}))
            if "zero_target_streak" not in account_state["prior_state"]:
                account_state["prior_state"]["zero_target_streak"] = dict(account_state.get("zero_target_streak", {}))
            if "last_exit_date" not in account_state["prior_state"]:
                account_state["prior_state"]["last_exit_date"] = dict(account_state.get("last_exit_date", {}))
            if "entry_prices" not in account_state["prior_state"]:
                account_state["prior_state"]["entry_prices"] = dict(account_state.get("entry_prices", {}))
        account_state["lifecycle_states"] = updated_lifecycle_states
        account_state["holding_bars"] = dual_holding_bars
        account_state["entry_prices"] = dual_entry_prices
        account_state["last_exit_date"] = updated_last_exit_date
        account_state["exit_bars_ago"] = updated_exit_bars_ago
        account_state["entry_signal_streak"] = dual_entry_signal_streak
        account_state["zero_target_streak"] = dual_zero_target_streak
        account_state["strategy_version"] = strat_ver
        if base_ver:
            account_state["base_version"] = base_ver
        if strategy_version_info:
            account_state["strategy_version_info"] = strategy_version_info
        try:
            with open(args.account_state_file, "w", encoding="utf-8") as f:
                json.dump(account_state, f, indent=2, ensure_ascii=False)
            print(f"[SUCCESS] 账户状态持仓周期与防抖跟踪及UPLSM状态已更新至: {args.account_state_file}")
        except Exception as e:
            print(f"[WARN] Failed to update account state file: {e}")
    print("=" * 110)
    return order_df


if __name__ == "__main__":
    main()

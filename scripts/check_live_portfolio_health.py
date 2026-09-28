#!/usr/bin/env python3
"""Stage 1 Operational Live Deployment Runner: Portfolio Health & Macro Regime Inspection.

Evaluates the mandatory 14:00 – 14:15 pre-trade health gate before Stage 2 order generation:
1. Calculates portfolio High-Water Mark (HWM) peak-to-trough drawdown and circuit breaker tiers:
   - Normal (DD < 10%): Full risk budget, proceed to Stage 2.
   - Tier 1 (10% <= DD < 15%): Smooth linear damping towards cash (15-day auto-healing).
   - Tier 2 (15% <= DD < 20%): Tactical defense rotation (70% cash proxy).
   - Tier 3 (DD >= 20%): Emergency halt (100% cash sweep, 21-day trading freeze).
2. Evaluates Fast-Recovery Override (10d NAV recovery > 0 or 10d breadth thrust >= 60%).
3. Evaluates 50-day market breadth (Close > SMA50 >= 30%) and 10-day momentum thrust (ROC10 > 0 >= 60%).
4. Calculates Barroso 21-day realized volatility targeting scaling factor (target vol = 12%).
5. Outputs an actionable pre-trade directive (GO / CAUTION / NO-GO) and saves structured reports.

Usage:
    # 1. Run live check for today using the default China Core-Satellite universe:
    uv run python scripts/check_live_portfolio_health.py --data-provider synthetic

    # 2. Run with current brokerage NAV and peak NAV:
    uv run python scripts/check_live_portfolio_health.py \\
        --portfolio-value 98000 \\
        --peak-nav 105000 \\
        --data-provider marketdb

    # 3. Run for US Equities Core-Satellite universe:
    uv run python scripts/check_live_portfolio_health.py \\
        --universe-file docs/universe/us/core_satellite_22_stocks.txt \\
        --portfolio-value 100000 \\
        --data-provider yfinance
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add repo root to sys.path -- robust for both scripts/ and docs/ruleset/…/ (symlink) invocations
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

from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    load_universe_with_banner,
    shared_data_dir,
)
from common.indicators import sma
from common.strategy_spec import load_strategy_file
from common.universe import resolve_universe_from_args
from pipeline.live_signal.lsig.signal import as_of_universe

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


DEFAULT_STRATEGY_FILE = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps", "chan_four_state_blend_strategy.json"
)
DEFAULT_UNIVERSE_FILE = os.path.join(_REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")
DEFAULT_OUTPUT_DIR = os.path.join(_REPO_ROOT, "docs", "ruleset", "chan_four_state_blend")


def parse_args(args: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        description="Stage 1 Live Deployment Runner: Portfolio Health & Macro Regime Inspection"
    )
    parser.add_argument(
        "--portfolio-value",
        "--nav",
        type=float,
        default=100000.0,
        help="Current total portfolio NAV in account currency (default: 100,000.0)",
    )
    parser.add_argument(
        "--peak-nav",
        "--hwm",
        type=float,
        default=None,
        help="Portfolio High-Water Mark peak NAV (default: loaded from state file or set to --portfolio-value)",
    )
    parser.add_argument(
        "--account-state-file",
        default=None,
        help="Path to persistent account state JSON file (default: account_state.json in --output-dir)",
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
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save generated health reports (default: docs/ruleset/chan_four_state_blend)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Folder path for DuckDB / parquet market data cache (default: data/ in repo root)",
    )
    parser.add_argument(
        "--no-save-state",
        action="store_true",
        help="Do not persist updated state back to account-state-file",
    )
    parser.add_argument(
        "--auto-run-stage2",
        action="store_true",
        help="If set and gate directive is GO/CAUTION, automatically run run_live_four_state_blend.py",
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    parsed = parser.parse_args(args)

    # Dynamically resolve output_dir and default account_state_file
    parsed.output_dir = _resolve_path(parsed.output_dir) or parsed.output_dir
    if parsed.account_state_file is None:
        parsed.account_state_file = os.path.join(parsed.output_dir, "account_state.json")
    else:
        parsed.account_state_file = _resolve_path(parsed.account_state_file)
    parsed.strategy_file = _resolve_path(parsed.strategy_file)
    parsed.universe_file = _resolve_path(parsed.universe_file)
    if parsed.cache_dir:
        parsed.cache_dir = _resolve_path(parsed.cache_dir)
    return parsed


def to_json_serializable(obj: Any) -> Any:
    """Recursively convert NumPy and pandas types into standard JSON-serializable types."""
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        val = float(obj)
        return None if np.isnan(val) else val
    if isinstance(obj, (np.ndarray, pd.Series)):
        return [to_json_serializable(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): to_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_serializable(x) for x in obj]
    return obj


def load_account_state(state_file: str) -> Dict[str, Any]:
    """Load persistent account state or initialize a fresh state dictionary."""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to read account state file '{state_file}': {e}. Using fresh state.")
    return {
        "as_of_date": None,
        "current_nav": None,
        "peak_nav": None,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [],
    }


def save_account_state(state_file: str, state: Dict[str, Any]) -> None:
    """Save updated account state to JSON file."""
    os.makedirs(os.path.dirname(os.path.abspath(state_file)), exist_ok=True)
    serializable = to_json_serializable(state)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False, default=str)


def inspect_portfolio_drawdown(
    current_nav: float,
    peak_nav: Optional[float],
    state: Dict[str, Any],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute peak-to-trough drawdown, circuit breaker tiers, auto-healing, and fast recovery."""
    dd_reduce_thresh = float(params.get("cfsb_dd_reduce_thresh", 0.10))
    dd_defensive_thresh = float(params.get("cfsb_dd_defensive_thresh", 0.15))
    dd_stop_thresh = float(params.get("cfsb_dd_stop_thresh", 0.20))
    tier1_cooldown_bars = int(params.get("cfsb_tier1_cooldown_bars", 15))
    stop_cooldown_bars = 21

    # Resolve HWM peak NAV
    saved_peak = state.get("peak_nav")
    if peak_nav is not None:
        effective_peak = float(peak_nav)
    elif saved_peak is not None and saved_peak > 0:
        effective_peak = max(float(saved_peak), current_nav)
    else:
        effective_peak = current_nav

    current_dd = (current_nav - effective_peak) / effective_peak if effective_peak > 0 else 0.0
    dd_mag = abs(current_dd)

    tier1_counter = int(state.get("tier1_consecutive_bars", 0))
    tier3_counter = int(state.get("tier3_consecutive_bars", 0))
    freeze_remaining = int(state.get("freeze_remaining_bars", 0))

    auto_healed = False
    heal_reason = ""

    # Evaluate Cooldown & Auto-Healing
    if dd_mag >= dd_stop_thresh:
        tier3_counter += 1
        tier1_counter = 0
        if freeze_remaining == 0:
            freeze_remaining = stop_cooldown_bars
        else:
            freeze_remaining = max(0, freeze_remaining - 1)

        if tier3_counter >= stop_cooldown_bars:
            effective_peak = current_nav
            tier3_counter = 0
            freeze_remaining = 0
            current_dd = 0.0
            dd_mag = 0.0
            auto_healed = True
            heal_reason = f"Tier 3 Full Stop sustained {stop_cooldown_bars} bars: Auto-healed HWM reset to current NAV."
    elif dd_mag >= dd_reduce_thresh:
        tier1_counter += 1
        tier3_counter = 0
        freeze_remaining = max(0, freeze_remaining - 1) if freeze_remaining > 0 else 0

        if tier1_counter >= tier1_cooldown_bars:
            effective_peak = current_nav
            tier1_counter = 0
            current_dd = 0.0
            dd_mag = 0.0
            auto_healed = True
            heal_reason = f"Tier 1/2 Risk Damping sustained {tier1_cooldown_bars} bars: Auto-healed HWM reset to current NAV."
    else:
        tier1_counter = 0
        tier3_counter = 0
        freeze_remaining = max(0, freeze_remaining - 1) if freeze_remaining > 0 else 0
        if current_nav > effective_peak:
            effective_peak = current_nav

    # Fast-Recovery Override (NAV 10-day momentum > 0)
    nav_hist = state.get("nav_history_10d", [])
    r_10d = (current_nav / nav_hist[0] - 1.0) if len(nav_hist) >= 10 and nav_hist[0] > 0 else 0.0
    fast_recovery_nav = r_10d > 0.0

    # Classify Tier Status
    if dd_mag >= dd_stop_thresh or freeze_remaining > 0:
        tier = "TIER_3_HALT"
        tier_label = "🔴 TIER 3 (Emergency Halt / 100% Cash)"
        linear_scale = 0.0
    elif dd_mag >= dd_defensive_thresh:
        tier = "TIER_2_DEFENSIVE"
        tier_label = "🟠 TIER 2 (Tactical Defense Mode / 70% Cash)"
        linear_scale = max(0.0, 1.0 - (dd_mag - dd_reduce_thresh) / max(0.01, dd_stop_thresh - dd_reduce_thresh))
    elif dd_mag >= dd_reduce_thresh:
        tier = "TIER_1_DAMPING"
        tier_label = "🟡 TIER 1 (Smooth Linear Damping)"
        linear_scale = max(0.0, 1.0 - (dd_mag - dd_reduce_thresh) / max(0.01, dd_stop_thresh - dd_reduce_thresh))
    else:
        tier = "NORMAL"
        tier_label = "🟢 NORMAL (Full Risk Budget)"
        linear_scale = 1.0

    return {
        "current_nav": current_nav,
        "peak_nav": effective_peak,
        "drawdown_pct": current_dd,
        "drawdown_mag": dd_mag,
        "tier": tier,
        "tier_label": tier_label,
        "linear_equity_scale": linear_scale,
        "tier1_counter": tier1_counter,
        "tier1_max": tier1_cooldown_bars,
        "tier3_counter": tier3_counter,
        "tier3_max": stop_cooldown_bars,
        "freeze_remaining_bars": freeze_remaining,
        "auto_healed": auto_healed,
        "heal_reason": heal_reason,
        "nav_10d_return": r_10d,
        "fast_recovery_nav": fast_recovery_nav,
    }


def evaluate_market_breadth_and_thrust(
    universe: Dict[str, pd.DataFrame],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute universe 50-day market breadth, 10-day momentum thrust, and top leaders."""
    breadth_lookback = int(params.get("cfsb_breadth_lookback", 50))
    breadth_bull_thresh = float(params.get("cfsb_breadth_bull_thresh", 0.30))
    thrust_lookback = int(params.get("cfsb_thrust_lookback", 10))
    thrust_thresh = float(params.get("cfsb_thrust_thresh", 0.60))
    target_bull_exposure = float(params.get("cfsb_target_bull_exposure", 0.80))
    cash_proxy = params.get("cash_proxy", "BIL")

    symbols = [s for s in universe.keys() if s != cash_proxy and not universe[s].empty]
    if not symbols:
        return {
            "breadth": 0.50,
            "thrust": 0.50,
            "breadth_active": True,
            "thrust_active": False,
            "bull_active": False,
            "breadth_factor": 0.0,
            "target_exposure": 0.50,
            "leaders": [],
        }

    above_sma_count = 0
    positive_roc_count = 0
    roc_records = []

    for sym in symbols:
        df = universe[sym]
        close = df["Close"].dropna()
        if len(close) < 2:
            continue

        # 50-day SMA check
        sma_val = close.rolling(min(breadth_lookback, len(close))).mean().iloc[-1]
        last_price = float(close.iloc[-1])
        is_above_sma = last_price > sma_val
        if is_above_sma:
            above_sma_count += 1

        # 10-day ROC check
        shift_idx = max(0, len(close) - 1 - thrust_lookback)
        prev_price = float(close.iloc[shift_idx])
        roc_10d = (last_price / prev_price - 1.0) if prev_price > 0 else 0.0
        if roc_10d > 0.0:
            positive_roc_count += 1

        roc_records.append({
            "symbol": sym,
            "last_price": last_price,
            "sma_50": sma_val,
            "above_sma": is_above_sma,
            "roc_10d": roc_10d,
        })

    tot = len(symbols)
    breadth = above_sma_count / tot if tot > 0 else 0.50
    thrust = positive_roc_count / tot if tot > 0 else 0.50

    breadth_active = breadth >= breadth_bull_thresh
    thrust_active = thrust >= thrust_thresh
    bull_active = breadth_active or thrust_active

    if thrust_active:
        breadth_factor = 1.0
    elif breadth_active:
        breadth_factor = float(np.clip((breadth - breadth_bull_thresh) / max(0.01, 0.75 - breadth_bull_thresh), 0.0, 1.0))
    else:
        breadth_factor = 0.0

    target_exp = min(target_bull_exposure, 0.60 + breadth_factor * (target_bull_exposure - 0.60)) if bull_active else 0.50

    # Sort leaders by 10-day ROC descending
    roc_records.sort(key=lambda x: x["roc_10d"], reverse=True)
    top_leaders = roc_records[:5]

    return {
        "breadth": breadth,
        "breadth_thresh": breadth_bull_thresh,
        "breadth_active": breadth_active,
        "thrust": thrust,
        "thrust_thresh": thrust_thresh,
        "thrust_active": thrust_active,
        "bull_active": bull_active,
        "breadth_factor": breadth_factor,
        "target_exposure": target_exp,
        "tracked_symbols_count": tot,
        "leaders": top_leaders,
    }


def evaluate_realized_volatility(
    universe: Dict[str, pd.DataFrame],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute 21-day realized market volatility and Barroso volatility targeting scaling factor."""
    target_vol = float(params.get("cfsb_target_vol", 0.12))
    cash_proxy = params.get("cash_proxy", "BIL")

    returns_list = []
    for sym, df in universe.items():
        if sym == cash_proxy or df.empty or "Close" not in df:
            continue
        c = df["Close"].dropna()
        if len(c) > 22:
            returns_list.append(c.pct_change().dropna())

    if not returns_list:
        return {
            "realized_vol_21d": target_vol,
            "target_vol": target_vol,
            "vol_scaling_factor": 1.0,
            "vol_elevated": False,
        }

    combined_ret = pd.concat(returns_list, axis=1).mean(axis=1).dropna()
    if len(combined_ret) >= 10:
        vol_21d = float(combined_ret.iloc[-21:].std() * np.sqrt(252))
        if np.isnan(vol_21d) or vol_21d <= 0:
            vol_21d = target_vol
    else:
        vol_21d = target_vol

    scale = min(1.0, target_vol / vol_21d) if vol_21d > 0 else 1.0
    return {
        "realized_vol_21d": vol_21d,
        "target_vol": target_vol,
        "vol_scaling_factor": scale,
        "vol_elevated": vol_21d > target_vol,
    }


def resolve_gate_directive(
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
) -> Tuple[str, str, List[str]]:
    """Synthesize health checks into an actionable trading gate directive."""
    actions = []
    tier = dd_info["tier"]
    fast_rec = dd_info["fast_recovery_nav"] or macro_info["thrust_active"]
    freeze = dd_info["freeze_remaining_bars"]

    if freeze > 0 or (tier == "TIER_3_HALT" and not fast_rec):
        code = "NO_GO_EMERGENCY"
        headline = "🔴 NO-GO: EMERGENCY HALT & 100% CASH LIQUIDATION"
        actions.append("HALT ALL RISK ASSET PURCHASES: Do NOT place any buy orders today.")
        actions.append(f"Execute 100% liquidation of all risky assets into Cash Proxy ({freeze} freeze days remaining).")
        actions.append("Park proceeds in yield-bearing cash proxy (511880 / GC001 / BIL).")
    elif tier == "TIER_3_HALT" and fast_rec:
        code = "CAUTION_RECOVERING"
        headline = "🟡 CAUTION: TIER 3 DOWNGRADED VIA FAST RECOVERY OVERRIDE"
        actions.append("Fast Recovery Active: 10d NAV recovery > 0 or Breadth Thrust >= 60%.")
        actions.append("Downgrade from 100% Cash Stop to Tier 2 Defensive Mode (ChanVaaCompound with 70% cash).")
        actions.append("Prioritize structural exits before opening limited defensive positions.")
    elif tier == "TIER_2_DEFENSIVE":
        code = "CAUTION_DEFENSIVE"
        headline = "🟠 CAUTION: TIER 2 TACTICAL DEFENSE MODE ACTIVE"
        actions.append("Rotate 100% of allocation into ChanVaaCompound defensive sub-strategy.")
        actions.append("Enforce 70% cash proxy defensive reserve; restrict risk exposure to <= 30%.")
        actions.append("Execute SELLS FIRST to release capital and eliminate lagging assets.")
    elif tier == "TIER_1_DAMPING":
        code = "CAUTION_DAMPING"
        headline = "🟡 CAUTION: TIER 1 SMOOTH LINEAR DAMPING ACTIVE"
        actions.append(f"Scale equity exposure to {dd_info['linear_equity_scale']:.1%} of standard model weights.")
        actions.append("Sweep released capital into Cash Proxy; execute SELLS FIRST.")
    else:
        # Normal
        code = "GO"
        if macro_info["thrust_active"]:
            headline = "🟢 GO: RAPID REBOUND / BREADTH THRUST MODE ACTIVE"
            actions.append(f"10-Day Momentum Thrust Active ({macro_info['thrust']:.1%} >= {macro_info['thrust_thresh']:.0%}).")
            actions.append(f"Bull exposure scaled up to {macro_info['target_exposure']:.0%}, dispersed across >= 5 leaders.")
            actions.append("Strictly maintain 20% single-stock ceiling across all assets.")
        elif macro_info["breadth_active"]:
            headline = "🟢 GO: STEADY BULL BREADTH REGIME"
            actions.append(f"50-Day Market Breadth Active ({macro_info['breadth']:.1%} >= {macro_info['breadth_thresh']:.0%}).")
            actions.append(f"Target equity exposure dynamically deployed up to {macro_info['target_exposure']:.0%}.")
        else:
            headline = "🟢 GO: CONSERVATIVE / SELECTIVE MARKET REGIME"
            actions.append("Standard unscaled allocation (40%–60% equity, balance in Cash Proxy).")
            actions.append("Standard 20% single-stock cap strictly applied.")

    if vol_info["vol_elevated"]:
        actions.append(
            f"Volatility Alert: 21d realized vol ({vol_info['realized_vol_21d']:.1%}) > target ({vol_info['target_vol']:.1%}); "
            f"Barroso scaling scales equity by {vol_info['vol_scaling_factor']:.2f}x."
        )

    return code, headline, actions


def print_dashboard(
    as_of_date: str,
    universe_path: str,
    strategy_path: str,
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    gate_code: str,
    gate_headline: str,
    actions: List[str],
) -> None:
    """Print an executive-level pre-trade terminal dashboard."""
    print("=" * 110)
    print("STAGE 1 PRE-TRADE GATE: PORTFOLIO HEALTH & MACRO REGIME INSPECTION (14:00 – 14:15)")
    print("=" * 110)
    print(f"Valuation As-Of Date : {as_of_date}")
    print(f"Strategy Config      : {strategy_path}")
    print(f"Universe Source      : {universe_path} ({macro_info.get('tracked_symbols_count', 0)} assets)")
    print("-" * 110)

    # 1. Portfolio Health
    print("[1. PORTFOLIO HEALTH & DRAWDOWN CIRCUIT BREAKERS]")
    print(f"  Current Portfolio NAV : {dd_info['current_nav']:>12,.2f}")
    print(f"  High-Water Mark (HWM) : {dd_info['peak_nav']:>12,.2f}")
    print(f"  Peak-to-Trough DD     : {dd_info['drawdown_pct']:>11.2%}")
    print(f"  Circuit Breaker Tier  : {dd_info['tier_label']}")
    print(f"  Linear Equity Scale   : {dd_info['linear_equity_scale']:>11.1%}")

    if dd_info["tier1_counter"] > 0:
        print(f"  Tier 1/2 Cooldown     : {dd_info['tier1_counter']} / {dd_info['tier1_max']} bars until HWM auto-heal")
    if dd_info["tier3_counter"] > 0 or dd_info["freeze_remaining_bars"] > 0:
        print(f"  Tier 3 Trading Freeze : {dd_info['freeze_remaining_bars']} bars remaining lockout (Counter: {dd_info['tier3_counter']}/{dd_info['tier3_max']})")
    if dd_info["auto_healed"]:
        print(f"  ✨ AUTO-HEAL RESET     : {dd_info['heal_reason']}")
    if dd_info["fast_recovery_nav"]:
        print(f"  🚀 FAST-RECOVERY NAV   : 10-day NAV recovery is positive ({dd_info['nav_10d_return']:>+.2%})")

    # 2. Market Breadth & Thrust
    print("\n[2. UNIVERSE MARKET BREADTH & MOMENTUM THRUST]")
    b_status = "🟢 Bullish" if macro_info["breadth_active"] else "⚪ Neutral/Defensive"
    t_status = "🚀 Thrust Active" if macro_info["thrust_active"] else "⚪ Inactive"
    print(f"  50-Day Market Breadth : {macro_info['breadth']:>6.1%} (Threshold: {macro_info['breadth_thresh']:>5.1%}) | {b_status}")
    print(f"  10-Day Momentum Thrust: {macro_info['thrust']:>6.1%} (Threshold: {macro_info['thrust_thresh']:>5.1%}) | {t_status}")
    print(f"  Target Total Equity   : {macro_info['target_exposure']:>6.1%} (Max Single-Stock Cap: 20.0%)")

    if macro_info.get("leaders"):
        print("  Top Momentum Leaders  : " + ", ".join([f"{x['symbol']} ({x['roc_10d']:>+.1%})" for x in macro_info["leaders"]]))

    # 3. Volatility Targeting
    print("\n[3. REALIZED VOLATILITY TARGETING (BARROSO & SANTA-CLARA 2015)]")
    v_status = "⚠️ Scaled Down" if vol_info["vol_elevated"] else "🟢 Stable"
    print(f"  21-Day Realized Vol   : {vol_info['realized_vol_21d']:>6.1%} (Target Vol: {vol_info['target_vol']:>5.1%}) | {v_status}")
    print(f"  Volatility Scaling    : {vol_info['vol_scaling_factor']:>6.2f}x")

    # 4. Directive & Trader Instructions
    print("-" * 110)
    print(f"FINAL PRE-TRADE DIRECTIVE : {gate_headline}")
    print("-" * 110)
    print("MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (14:15 – 14:50):")
    for i, act in enumerate(actions, start=1):
        print(f"  {i}. {act}")
    print("=" * 110)


def generate_reports(
    output_dir: str,
    as_of_date: str,
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    gate_code: str,
    gate_headline: str,
    actions: List[str],
) -> Tuple[str, str]:
    """Generate and write structured JSON and Markdown summary reports."""
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "stage1_health_report.json")
    md_path = os.path.join(output_dir, "stage1_health_report.md")

    report_dict = {
        "as_of_date": as_of_date,
        "gate_code": gate_code,
        "gate_headline": gate_headline,
        "portfolio_health": dd_info,
        "market_macro": macro_info,
        "volatility_targeting": vol_info,
        "trader_actions": actions,
    }

    serializable_report = to_json_serializable(report_dict)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(serializable_report, f, indent=2, ensure_ascii=False, default=str)

    md_content = f"""# Stage 1 Live Pre-Trade Health & Macro Audit Report

> **Valuation Date**: `{as_of_date}`  
> **Gate Directive**: **{gate_headline}**  
> **Report Timestamp**: Generated during 14:00 – 14:15 inspection window  

---

## 1. Portfolio Health & Drawdown Circuit Breakers

| Metric | Current Value | Threshold / Target | Status |
| :--- | :--- | :--- | :--- |
| **Current Portfolio NAV** | `{dd_info['current_nav']:,.2f}` | — | Tracking |
| **High-Water Mark (HWM)** | `{dd_info['peak_nav']:,.2f}` | — | All-Time Peak |
| **Peak-to-Trough Drawdown** | `{dd_info['drawdown_pct']:.2%}` | Tier 1: -10% / Tier 2: -15% / Tier 3: -20% | **{dd_info['tier_label']}** |
| **Linear Equity Scale** | `{dd_info['linear_equity_scale']:.1%}` | Smooth linear damping | Continuous exposure factor |
| **Tier 1 Cooldown Counter** | `{dd_info['tier1_counter']} / {dd_info['tier1_max']}` | 15 Bars | {"Auto-Heal Imminent" if dd_info['tier1_counter'] >= 10 else "Monitoring"} |
| **Tier 3 Cooldown & Freeze** | `{dd_info['freeze_remaining_bars']} bars left` | 21 Bars lockout | {"Active Lockout" if dd_info['freeze_remaining_bars'] > 0 else "Clear"} |
| **10-Day NAV Recovery** | `{dd_info['nav_10d_return']:+.2%}` | > 0.0% | {"🚀 Active" if dd_info['fast_recovery_nav'] else "⚪ None"} |

---

## 2. Universe Market Breadth & Momentum Thrust

| Indicator | Calculated | Benchmark Threshold | Regime Assessment |
| :--- | :--- | :--- | :--- |
| **50-Day Market Breadth** | `{macro_info['breadth']:.1%}` | `30.0%` | {"🟢 Bullish Breadth" if macro_info['breadth_active'] else "⚪ Conservative"} |
| **10-Day Momentum Thrust** | `{macro_info['thrust']:.1%}` | `60.0%` | {"🚀 Rapid Rebound Thrust" if macro_info['thrust_active'] else "⚪ Standard"} |
| **Target Equity Exposure** | `{macro_info['target_exposure']:.0%}` | Standard: 50% / Bull: Up to 80% | {"Bullish Expansion" if macro_info['target_exposure'] > 0.50 else "Standard Exposure"} |
| **Single-Stock Cap** | `20.0%` | Fixed hard limit | Enforced across all regimes |

### Top Momentum Leaders (10-Day ROC)
"""
    if macro_info.get("leaders"):
        for i, lead in enumerate(macro_info["leaders"], start=1):
            md_content += f"{i}. **`{lead['symbol']}`**: 10d ROC = `{lead['roc_10d']:+.2%}` (Price: `{lead['last_price']:.2f}`, Above SMA50: `{lead['above_sma']}`)\n"
    else:
        md_content += "No active leaders found.\n"

    md_content += f"""
---

## 3. Realized Volatility Targeting

- **21-Day Realized Volatility**: `{vol_info['realized_vol_21d']:.1%}` (Target Vol: `{vol_info['target_vol']:.1%}`)
- **Barroso Volatility Scaling**: `{vol_info['vol_scaling_factor']:.2f}x`
- **Assessment**: {"⚠️ Elevated volatility: Rescaling portfolio equity exposure downwards." if vol_info['vol_elevated'] else "🟢 Realized market variance within standard bounds."}

---

## 4. Mandatory Human Trader Actions (Stage 2 Execution)

"""
    for i, act in enumerate(actions, start=1):
        md_content += f"{i}. {act}\n"

    md_content += "\n---\n*Report exported automatically by `scripts/check_live_portfolio_health.py`*\n"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return json_path, md_path


def main():
    args = parse_args()
    as_of_date = args.as_of_date or date.today().isoformat()
    start_date = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    # 1. Load Strategy Definition
    if not os.path.exists(args.strategy_file):
        raise FileNotFoundError(f"Strategy file not found at: {args.strategy_file}")
    strategy_def = load_strategy_file(args.strategy_file)
    params = strategy_def.get("params", {})

    # 2. Load Universe Data
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")

    cache_dir = args.cache_dir or shared_data_dir()
    universe = load_universe_with_banner(
        universe_symbols,
        start_date,
        as_of_date,
        interval="1d",
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, as_of_date)

    # 3. Load Account State & Evaluate Portfolio Drawdown
    state = load_account_state(args.account_state_file)
    dd_info = inspect_portfolio_drawdown(
        current_nav=args.portfolio_value,
        peak_nav=args.peak_nav,
        state=state,
        params=params,
    )

    # 4. Evaluate Universe Breadth, Thrust & Volatility
    macro_info = evaluate_market_breadth_and_thrust(universe, params)
    vol_info = evaluate_realized_volatility(universe, params)

    # 5. Synthesize Gate Directive
    gate_code, gate_headline, actions = resolve_gate_directive(dd_info, macro_info, vol_info)

    # 6. Update Account State
    if not args.no_save_state:
        nav_hist = state.get("nav_history_10d", [])
        nav_hist.append(args.portfolio_value)
        if len(nav_hist) > 10:
            nav_hist.pop(0)

        updated_state = dict(state)
        updated_state.update({
            "as_of_date": as_of_date,
            "current_nav": dd_info["current_nav"],
            "peak_nav": dd_info["peak_nav"],
            "drawdown_pct": dd_info["drawdown_pct"],
            "circuit_breaker_tier": dd_info["tier"],
            "linear_equity_scale": dd_info["linear_equity_scale"],
            "tier1_consecutive_bars": dd_info["tier1_counter"],
            "tier3_consecutive_bars": dd_info["tier3_counter"],
            "freeze_remaining_bars": dd_info["freeze_remaining_bars"],
            "nav_history_10d": nav_hist,
        })
        save_account_state(args.account_state_file, updated_state)

    # 7. Print Dashboard & Export Reports
    print_dashboard(
        as_of_date=as_of_date,
        universe_path=args.universe_file,
        strategy_path=args.strategy_file,
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code=gate_code,
        gate_headline=gate_headline,
        actions=actions,
    )

    json_rep, md_rep = generate_reports(
        output_dir=args.output_dir,
        as_of_date=as_of_date,
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code=gate_code,
        gate_headline=gate_headline,
        actions=actions,
    )
    print(f"\n[SUCCESS] Reports exported:")
    print(f"  Markdown: {md_rep}")
    print(f"  JSON    : {json_rep}")
    if not args.no_save_state:
        print(f"  State   : {args.account_state_file}")

    # 8. Optional Auto-Run Stage 2
    if args.auto_run_stage2:
        if gate_code == "NO_GO_EMERGENCY":
            print("\n[STOP] Auto-run Stage 2 aborted due to 🔴 NO-GO / EMERGENCY HALT.")
            sys.exit(1)
        else:
            print("\n[AUTO-EXECUTE] Launching Stage 2: run_live_four_state_blend.py ...")
            stage2_script = os.path.join(_REPO_ROOT, "scripts", "run_live_four_state_blend.py")
            cmd = [
                sys.executable,
                stage2_script,
                "--portfolio-value",
                str(args.portfolio_value),
                "--universe-file",
                args.universe_file,
                "--strategy-file",
                args.strategy_file,
                "--data-provider",
                args.data_provider,
                "--output-dir",
                args.output_dir,
                "--account-state-file",
                args.account_state_file,
            ]
            if args.as_of_date:
                cmd.extend(["--as-of-date", args.as_of_date])
            if getattr(args, "cache_dir", None):
                cmd.extend(["--cache-dir", args.cache_dir])
            if getattr(args, "data_dir", None):
                cmd.extend(["--data-dir", args.data_dir])
            if getattr(args, "no_cache", False):
                cmd.append("--no-cache")
            import subprocess
            subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()

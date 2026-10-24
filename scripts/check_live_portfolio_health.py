#!/usr/bin/env python3
"""Stage 1 Operational Live Deployment Runner: Portfolio Health & Macro Regime Inspection.

Evaluates the mandatory pre-trade health gate -- design §8 window: (13:55, 14:20) in the
MARKET's own local time (China/HK/US), not the host's clock -- before Stage 2 order generation:
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
import copy
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

from common.data import get_data_provider
from common.cli_utils import (
    add_data_provider_cli_args,
    add_market_clock_cli_args,
    build_data_kwargs,
    load_live_universe,
    persist_run_metadata,
    resolve_live_clock_context,
    shared_data_dir,
)
from common.freshness import assert_fresh_universe
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


def _fmt_metric(value: Any, spec: str, na: str = "N/A") -> str:
    """Format a possibly-None metric for the dashboard, rendering N/A when unmeasured.

    (2026-10-22 incident, Fix 1) Unavailable intraday features must render as
    "N/A", never as a number a human could mistake for a measurement.
    """
    if value is None:
        return na
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return na


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
        help=(
            "Point-in-time valuation date (YYYY-MM-DD). Default: the market clock's as_of_trading_date "
            "(close + publication grace, in the market's own timezone), never the host's local date. "
            "An explicit value must be a --market trading day and may not be later than the latest "
            "published session."
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
        help="If set and gate directive is GO/CAUTION, automatically run Stage 2 execution runner (scripts/run_live_strategy.py)",
    )
    parser.add_argument(
        "--enable-intraday-5m",
        action="store_true",
        help="Enable 5-minute intraday mark-to-market drawdown, VWAP breadth, and afternoon distribution gates",
    )
    parser.add_argument(
        "--cutoff-time",
        default="14:00",
        help=(
            "Intraday cutoff time for Stage 1, expressed in the MARKET's local wall clock (not the "
            "host's; default: 14:00, 36 5m bars). Used only for 5m microstructure/drawdown monitoring; "
            "daily-bar valuation always uses the as-of session from the market clock."
        ),
    )
    parser.add_argument(
        "--current-holdings-file",
        default=None,
        help="Path to current holdings JSON file for mark-to-market intraday NAV tracking",
    )
    parser.add_argument(
        "--allow-stale-prices",
        action="store_true",
        help=(
            "DANGEROUS EMERGENCY OVERRIDE (equivalent to --no-strict-freshness). Stage 1 refuses to run "
            "when any universe symbol's latest bar predates the as-of session, because breadth/thrust/vol "
            "would then be computed on yesterday's closes. This downgrades that hard failure to a loud "
            "warning and records freshness_summary=FAIL in the report."
        ),
    )
    add_data_provider_cli_args(parser, default_provider="synthetic")
    # (L2.3) as-of / market / run-window gate / strict freshness / probe retry limit.
    add_market_clock_cli_args(parser, stage="stage1")
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


def inspect_portfolio_drawdown(
    current_nav: float,
    peak_nav: Optional[float],
    state: Dict[str, Any],
    params: Dict[str, Any],
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute peak-to-trough drawdown, circuit breaker tiers, auto-healing, and fast recovery."""
    dd_reduce_thresh = float(resolve_blend_param(params, "dd_reduce_thresh", 0.10, float))
    dd_defensive_thresh = float(resolve_blend_param(params, "dd_defensive_thresh", 0.15, float))
    dd_stop_thresh = float(resolve_blend_param(params, "dd_stop_thresh", 0.20, float))
    tier1_cooldown_bars = int(resolve_blend_param(params, "tier1_cooldown_bars", 15, int))
    stop_cooldown_bars = int(resolve_blend_param(params, "stop_cooldown_bars", 21, int))

    # If state was already updated for as_of_date and has prior_state, use pre-day baseline
    if as_of_date and state.get("as_of_date") == as_of_date and isinstance(state.get("prior_state"), dict):
        state = state["prior_state"]

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
    breadth_lookback = int(resolve_blend_param(params, "breadth_lookback", 50, int))
    breadth_bull_thresh = float(resolve_blend_param(params, "breadth_bull_thresh", 0.30, float))
    thrust_lookback = int(resolve_blend_param(params, "thrust_lookback", 10, int))
    thrust_thresh = float(resolve_blend_param(params, "thrust_thresh", 0.60, float))
    target_bull_exposure = float(resolve_blend_param(params, "target_bull_exposure", 0.80, float))
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
        # (2026-10-22 incident) `target_exposure` is a Stage-1 REGIME advisory
        # (a macro "should" level), NOT an executable order. Stage-2
        # (`run_live_strategy.py`) derives actual equity/cash weights from the
        # model's generate_weights output. Persisting this flag prevents the
        # 0.7111 advisory being misread as the literal 0.444087 BIL target.
        "target_exposure_is_advisory": True,
        "tracked_symbols_count": tot,
        "leaders": top_leaders,
    }


def evaluate_realized_volatility(
    universe: Dict[str, pd.DataFrame],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute 21-day realized market volatility and Barroso volatility targeting scaling factor."""
    target_vol = float(resolve_blend_param(params, "target_vol", 0.12, float))
    vol_model = str(resolve_blend_param(params, "vol_model", "garman_klass", str)).lower()
    cash_proxy = params.get("cash_proxy", "BIL")

    vol_estimates = []
    gk_count = 0
    for sym, df in universe.items():
        if sym == cash_proxy or df.empty or "Close" not in df:
            continue
        # Extreme-value Garman-Klass (1980) volatility using OHLC bars
        if vol_model == "garman_klass" and all(c in df.columns for c in ("High", "Low", "Close", "Open")):
            h = df["High"].replace(0, np.nan)
            l = df["Low"].replace(0, np.nan)
            c = df["Close"].replace(0, np.nan)
            o = df["Open"].replace(0, np.nan)
            if not (h == l).all() and len(c.dropna()) >= 10:
                log_hl = np.log((h / l).clip(lower=1e-6)) ** 2
                log_co = np.log((c / o).clip(lower=1e-6)) ** 2
                gk_var = 0.5 * log_hl - (2.0 * np.log(2.0) - 1.0) * log_co
                gk_var = gk_var.clip(lower=0.0)
                gk_vol = float(np.sqrt(gk_var.iloc[-21:].mean() * 252.0))
                if not np.isnan(gk_vol) and gk_vol > 0:
                    vol_estimates.append(gk_vol)
                    gk_count += 1
                    continue

        # Close-to-close fallback
        c = df["Close"].dropna()
        if len(c) > 22:
            ret = c.pct_change().dropna()
            cc_vol = float(ret.iloc[-21:].std() * np.sqrt(252))
            if not np.isnan(cc_vol) and cc_vol > 0:
                vol_estimates.append(cc_vol)

    if vol_estimates:
        vol_21d = float(np.median(vol_estimates))
    else:
        vol_21d = target_vol

    scale = min(1.0, target_vol / vol_21d) if vol_21d > 0 else 1.0
    active_model = "Garman-Klass (1980)" if (gk_count > 0 and vol_model == "garman_klass") else "Close-to-Close"

    # Downside semivariance & jump check
    downside_estimates = []
    signed_jumps = []
    for sym, df in universe.items():
        if sym == cash_proxy or df.empty:
            continue
        if "RS_Down_5m" in df.columns:
            rs_down_s = df["RS_Down_5m"].dropna()
            if len(rs_down_s) >= 5:
                down_vol = float(np.sqrt(252.0 * (2.0 * rs_down_s).iloc[-21:].mean()))
                if not np.isnan(down_vol) and down_vol > 0:
                    downside_estimates.append(down_vol)
        if "Signed_Jump_5m" in df.columns:
            sj = df["Signed_Jump_5m"].dropna()
            if len(sj) > 0:
                signed_jumps.append(float(sj.iloc[-1]))

    if downside_estimates:
        downside_vol_21d = float(np.median(downside_estimates))
        med_jump = float(np.median(signed_jumps)) if signed_jumps else 0.0
        if med_jump >= 0.0:
            eff_vol = downside_vol_21d
            active_model = "5m Downside Semivariance (Patton & Sheppard 2015)"
        else:
            eff_vol = max(vol_21d, downside_vol_21d)
            active_model = "Garman-Klass / Downside Vol Max"
        scale = min(1.0, target_vol / eff_vol) if eff_vol > 0 else 1.0
        vol_21d = eff_vol

    return {
        "realized_vol_21d": vol_21d,
        "target_vol": target_vol,
        "vol_scaling_factor": scale,
        "vol_elevated": vol_21d > target_vol,
        "vol_model": active_model,
    }


def resolve_gate_directive(
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    intraday_5m_info: Optional[Dict[str, Any]] = None,
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
        # (2026-10-22 incident, Fix 1) An intraday gate that could NOT be
        # evaluated must say so out loud. Previously the health script fed
        # NaN-substituted defaults (breadth 1.0) into this branch, which looked
        # like a comfortably-passing gate. Neither the alert branch nor a "pass"
        # claim is emitted now: the gate is explicitly SKIPPED/UNKNOWN.
        if intraday_5m_info and intraday_5m_info.get("intraday_features_available") is False:
            actions.append(
                "⚠️ Intraday distribution gate SKIPPED / UNKNOWN — 5m 日内特征不可用"
                f"（{intraday_5m_info.get('unavailable_reason', 'no measured intraday features')}）。"
                "该门禁既未通过也未触发；未测得数据不会被当作 PASS，也不会缩放买入预算。"
            )
        elif intraday_5m_info and intraday_5m_info.get("distribution_gate_status") == "UNKNOWN":
            # Partially-measured case (2026-10-22 live-path hardening): some
            # symbols carry a VWAP premium but too few carry every component for
            # the aggregate to be judged. Silence here would read as "no
            # distribution detected", so state the uncertainty explicitly.
            actions.append(
                "⚠️ Intraday distribution gate UNKNOWN — 日内特征仅部分测得，"
                f"聚合指标无法判定（已测得标的 {intraday_5m_info.get('symbols_measured', 0)} 个，"
                f"缺失 {len(intraday_5m_info.get('symbols_missing_features') or [])} 个，"
                f"截断时点 {intraday_5m_info.get('cutoff_time', 'N/A')}）。"
                "该门禁未通过也未触发，不会被当作 PASS，也不会缩放买入预算。"
            )
        # Evaluate intraday afternoon distribution and trough alerts
        if intraday_5m_info and intraday_5m_info.get("distribution_alert"):
            code = "CAUTION_INTRADAY_DISTRIBUTION"
            headline = "🟡 CAUTION: INTRADAY AFTERNOON DISTRIBUTION DETECTED"
            actions.append("Intraday Afternoon Distribution: Morning breakout failed with afternoon volume dump.")
            actions.append("Stage 2 BUY orders will be scaled down to 50% (intraday_buy_scale = 0.50). SELLS execute normally.")
        elif intraday_5m_info and intraday_5m_info.get("trough_alert"):
            code = "CAUTION_INTRADAY_TROUGH"
            headline = "🟡 CAUTION: INTRADAY NAV FLASH DRAWDOWN DETECTED"
            dd_tr = intraday_5m_info.get("dd_trough") or 0.0
            actions.append(f"Intraday mark-to-market trough drawdown reached {abs(dd_tr):.1%}, exceeding tactical defense line.")
            actions.append("Diagnostic warning only (R5 backtest parity): monitoring intraday volatility without forced tier escalation.")
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
    strategy_info: Dict[str, Any],
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    gate_code: str,
    gate_headline: str,
    actions: List[str],
    intraday_5m_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Print an executive-level pre-trade terminal dashboard."""
    print("=" * 110)
    print("STAGE 1 PRE-TRADE GATE: PORTFOLIO HEALTH & MACRO REGIME INSPECTION (13:55 – 14:20 market-local)")
    print("=" * 110)
    print(f"Valuation As-Of Date : {as_of_date}")
    strat_disp = f"{strategy_info.get('strategy_name', 'N/A')} ({strategy_info.get('template_name', '')})"
    print(f"Strategy Deployment  : {strat_disp}")
    print(f"Weighting & Ortho    : Mode={strategy_info.get('weighting_mode', 'rolling_ic_ir')} | Orthogonalization={strategy_info.get('orthogonalization', True)}")
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
    print(f"  Target Total Equity   : {macro_info['target_exposure']:>6.1%} (Max Single-Stock Cap: 20.0%) [Stage-1 advisory, ignored by Stage-2 orders]")

    if macro_info.get("leaders"):
        print("  Top Momentum Leaders  : " + ", ".join([f"{x['symbol']} ({x['roc_10d']:>+.1%})" for x in macro_info["leaders"]]))

    # 3. Volatility Targeting
    print("\n[3. REALIZED VOLATILITY TARGETING (BARROSO & SANTA-CLARA 2015)]")
    v_status = "⚠️ Scaled Down" if vol_info["vol_elevated"] else "🟢 Stable"
    vol_mod_lbl = f", Model: {vol_info.get('vol_model', 'N/A')}" if "vol_model" in vol_info else ""
    print(f"  21-Day Realized Vol   : {vol_info['realized_vol_21d']:>6.1%} (Target Vol: {vol_info['target_vol']:>5.1%}{vol_mod_lbl}) | {v_status}")
    print(f"  Volatility Scaling    : {vol_info['vol_scaling_factor']:>6.2f}x")

    if intraday_5m_info and intraday_5m_info.get("enabled"):
        print("\n[4. 5-MINUTE INTRADAY MICROSTRUCTURE & MARK-TO-MARKET (14:00)]")
        # (2026-10-22 incident, Fix 1) Render UNMEASURED metrics as N/A. The old
        # dashboard printed fabricate-looking numbers (breadth 100.0%) computed
        # from NaN-substituted constants on a run whose 5m frame was empty.
        if intraday_5m_info.get("intraday_features_available") is False:
            print(f"  ⚠️ 状态               : SKIPPED / UNKNOWN — 5m 日内特征不可用 (不影响日线门禁)")
            print(f"  原因 (Reason)         : {intraday_5m_info.get('unavailable_reason', 'N/A')}")
        print(
            f"  Intraday VWAP Breadth : {_fmt_metric(intraday_5m_info.get('intraday_vwap_breadth'), '>6.1%'):>6} "
            "(Threshold: 25.0%)"
        )
        print(
            f"  Afternoon Return Mean : {_fmt_metric(intraday_5m_info.get('afternoon_return_mean'), '>+6.2%'):>6} "
            f"(Breakdown Ratio: {_fmt_metric(intraday_5m_info.get('afternoon_vwap_breakdown_ratio'), '.1%')})"
        )
        print(
            f"  Intraday Trough DD    : {_fmt_metric(intraday_5m_info.get('dd_trough'), '>11.2%'):>11} "
            f"(Intraday Peak: {_fmt_metric(intraday_5m_info.get('hwm_intra'), ',.2f')})"
        )
        print(f"  Distribution Gate     : {intraday_5m_info.get('distribution_gate_status', 'EVALUATED')}")
        if intraday_5m_info.get("distribution_alert"):
            print("  ⚠️ ALERT              : Afternoon Distribution detected! Stage 2 BUY budget halved (intraday_buy_scale=0.5).")
        if intraday_5m_info.get("trough_alert"):
            print("  ⚠️ ALERT              : Intraday Flash Trough Drawdown breached tactical defense line.")

    # 4. Directive & Trader Instructions
    print("-" * 110)
    print(f"FINAL PRE-TRADE DIRECTIVE : {gate_headline}")
    print("-" * 110)
    print("MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (14:15 – 14:50):")
    for i, act in enumerate(actions, start=1):
        print(f"  {i}. {act}")
    print("=" * 110)


def coarse_directive(gate_code: Optional[str]) -> str:
    """Map the granular Stage-1 `gate_code` onto the 3-value operator directive.

    (2026-10-22 live-path hardening, FIX 1 reconciliation) Stage 1 has ALWAYS
    emitted granular gate codes -- `GO`, `CAUTION_DAMPING`, `CAUTION_DEFENSIVE`,
    `CAUTION_RECOVERING`, `CAUTION_INTRADAY_DISTRIBUTION`,
    `CAUTION_INTRADAY_TROUGH`, `NO_GO_EMERGENCY` -- while every downstream
    consumer (`web/livetrading/execution_service.py`, `storage.py`,
    `static/js/app.js`, and the web tests) speaks a strict 3-value enum
    `GO` / `CAUTION` / `NO_GO`. The two only ever agreed because, before this
    fix, the intraday branch was unreachable: NaN-substituted defaults made
    `intraday_vwap_breadth` come out as 1.0, so no intraday caution could fire.
    Restoring honest intraday measurement made `CAUTION_INTRADAY_DISTRIBUTION`
    reachable for the first time and the mismatch surfaced as a hard contract
    break (the web test rejected the value, and `app.js` would have rendered it
    as the RED "NO-GO / 严禁开仓" badge -- the most alarming possible display for
    a 50%-buy-scale caution).

    Resolution: the producer now OWNS the coarse field instead of making the
    consumer guess. `directive` is the 3-value enum; `gate_code` keeps full
    granularity, so an operator can still tell "genuinely CAUTION because
    measured afternoon distribution" from "CAUTION because the drawdown tier is
    damping". Unavailability never reaches a CAUTION code at all: FIX 1 sets
    `distribution_alert=None` and emits a separate SKIPPED/UNKNOWN action line,
    so `intraday_features_available` / `distribution_gate_status` remain the
    authoritative "did this gate actually run?" signals.

    Unknown codes fail SAFE toward `CAUTION` (never `GO`, never a fabricated
    `NO_GO`): a code this mapping does not recognise is not evidence that risk
    is normal.
    """
    code = (gate_code or "").strip().upper()
    if code.startswith("NO_GO"):
        return "NO_GO"
    if code.startswith("CAUTION"):
        return "CAUTION"
    if code.startswith("GO"):
        return "GO"
    return "CAUTION"


def generate_reports(
    output_dir: str,
    as_of_date: str,
    strategy_info: Dict[str, Any],
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    gate_code: str,
    gate_headline: str,
    actions: List[str],
    intraday_5m_info: Optional[Dict[str, Any]] = None,
    clock_ctx: Any = None,
    freshness_summary: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Generate and write structured JSON and Markdown summary reports."""
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "stage1_health_report.json")
    md_path = os.path.join(output_dir, "stage1_health_report.md")

    report_dict = {
        "as_of_date": as_of_date,
        # (L2.3, design §9) One clock snapshot + one per-symbol freshness verdict, so
        # every report can answer "which instant, which session, which bars?" without
        # cross-referencing the console log.
        "clock": (clock_ctx.stamp_fields() if clock_ctx is not None else None),
        "freshness_summary": freshness_summary,
        "strategy_info": strategy_info,
        "strategy_version": strategy_info.get("strategy_version", "1.0.0"),
        "base_version": strategy_info.get("base_version"),
        "version_info": strategy_info.get("version_info"),
        # (2026-10-22 live-path hardening, FIX 1 reconciliation) `directive` is
        # the coarse 3-value enum every web/API consumer and the operator UI
        # speaks; `gate_code` retains the granular cause. Do NOT collapse these
        # into one field: the coarse enum drives the banner colour, the granular
        # code drives the explanation (see `coarse_directive` docstring).
        "directive": coarse_directive(gate_code),
        "gate_code": gate_code,
        "gate_headline": gate_headline,
        "portfolio_health": dd_info,
        "market_macro": macro_info,
        "volatility_targeting": vol_info,
        # (2026-10-22 incident, Fix 1) SCHEMA CHANGE, disclosed: when the intraday
        # block is enabled, `intraday_5m_metrics` now carries
        # `intraday_features_available` (bool) and `distribution_gate_status`
        # ("EVALUATED" | "UNKNOWN" | "SKIPPED"), and every unmeasured metric is
        # JSON `null` instead of a fabricated 1.0/0.0. Consumers must treat null
        # as UNKNOWN, never as a passing value. Top-level mirror added for
        # consumers that do not read the nested block.
        "intraday_features_available": (
            intraday_5m_info.get("intraday_features_available") if intraday_5m_info else None
        ),
        "intraday_5m_metrics": intraday_5m_info,
        "trader_actions": actions,
    }

    serializable_report = to_json_serializable(report_dict)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(serializable_report, f, indent=2, ensure_ascii=False, default=str)

    strat_ver_display = f"v{strategy_info.get('strategy_version', '1.0.0')}" + (f" [base: v{strategy_info.get('base_version')}]" if strategy_info.get('base_version') else "")
    md_content = f"""# Stage 1 Live Pre-Trade Health & Macro Audit Report

> **Valuation Date**: `{as_of_date}`  
> **Strategy**: **{strategy_info.get('strategy_name', 'N/A')}** (`{strategy_info.get('template_name', '')}`)  
> **Strategy Version**: `{strat_ver_display}`  
> **Weighting & Ortho**: `{strategy_info.get('weighting_mode', 'rolling_ic_ir')}` | Orthogonalized: `{strategy_info.get('orthogonalization', True)}`  
> **Gate Directive**: **{gate_headline}**  
> **Report Timestamp**: Generated during the market-local 13:55 – 14:20 inspection window  

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
| **Target Equity Exposure** | `{macro_info['target_exposure']:.0%}` | Standard: 50% / Bull: Up to 80% | {"Bullish Expansion" if macro_info['target_exposure'] > 0.50 else "Standard Exposure"} (Stage-1 advisory; Stage-2 model weights govern orders) |
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
"""
    # (2026-10-22 incident, Fix 1) Intraday metrics are rendered only when they
    # were actually measured; otherwise the section states UNAVAILABLE and every
    # number is `N/A`. A missing measurement must never be printed as a value.
    if intraday_5m_info and intraday_5m_info.get("enabled"):
        _id_avail = intraday_5m_info.get("intraday_features_available")
        md_content += "\n## 4. 5-Minute Intraday Microstructure\n\n"
        if _id_avail is False:
            md_content += (
                "> **UNAVAILABLE — gate status `SKIPPED` / `UNKNOWN` (NOT passed).**\n"
                f"> {intraday_5m_info.get('unavailable_reason', 'no measured intraday features')}\n"
                f"> {intraday_5m_info.get('note', '')}\n\n"
            )
        md_content += (
            "| Metric | Value | Note |\n"
            "| :--- | :--- | :--- |\n"
            f"| **Intraday VWAP Breadth** | `{_fmt_metric(intraday_5m_info.get('intraday_vwap_breadth'), '.1%')}` | Threshold: 25.0% |\n"
            f"| **Afternoon Return Mean** | `{_fmt_metric(intraday_5m_info.get('afternoon_return_mean'), '+.2%')}` | Since 11:30 |\n"
            f"| **Afternoon VWAP Breakdown Ratio** | `{_fmt_metric(intraday_5m_info.get('afternoon_vwap_breakdown_ratio'), '.1%')}` | |\n"
            f"| **Intraday Trough Drawdown** | `{_fmt_metric(intraday_5m_info.get('dd_trough'), '.2%')}` | Diagnostic only (R5) |\n"
            f"| **Distribution Gate Status** | `{intraday_5m_info.get('distribution_gate_status', 'EVALUATED')}` | `SKIPPED`/`UNKNOWN` never means PASS |\n"
            f"| **Intraday Buy Scale** | `{intraday_5m_info.get('intraday_buy_scale', 1.0):.2f}` | 0.50 only on a positive detection |\n\n"
        )
        md_content += "\n---\n"

    md_content += (
        "\n## "
        + ("5" if (intraday_5m_info and intraday_5m_info.get("enabled")) else "4")
        + ". Mandatory Human Trader Actions (Stage 2 Execution)\n\n"
    )
    for i, act in enumerate(actions, start=1):
        md_content += f"{i}. {act}\n"

    md_content += "\n---\n*Report exported automatically by `scripts/check_live_portfolio_health.py`*\n"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return json_path, md_path


# Filename `generate_reports` writes (and Stage 2 must read back).
STAGE1_HEALTH_REPORT_JSON = "stage1_health_report.json"


def _absolutize_child_arg(path: Optional[str]) -> Optional[str]:
    """Anchors a path destined for the Stage-2 child process to an explicit base.

    (P1.3 -- six-defect audit, Defect 6 follow-up) The child process must never
    re-resolve a path against ITS CWD, because the only thing that guarantees the
    child inherits the same CWD as Stage 1 is that we happen to launch it with
    `subprocess.run` from the same process. Handing it an already-absolute path
    removes that implicit dependency entirely.

    Anchor policy == `parse_args`'s own `_resolve_path` fallback, made explicit:
    by the time this runs, `_resolve_path` has already turned every path that
    existed relative to the CWD into an absolute path, so anything still relative
    was resolved against `_REPO_ROOT` (or not found anywhere). Anchoring those to
    `_REPO_ROOT` here -- instead of `os.path.abspath`, which is CWD-dependent -- is
    both order-independent and CWD-independent. `_REPO_ROOT` is preferred over the
    CWD deliberately: the repo is the documented base for every relative path this
    script accepts (`docs/universe/...`, `data/`, `docs/ruleset/...`).
    """
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(_REPO_ROOT, path))


def build_stage2_command(args: argparse.Namespace, enable_intraday_5m: bool = False) -> List[str]:
    """Builds the argv for the auto-launched Stage 2 runner (`--auto-run-stage2`).

    (P1.3) `--health-report-file` is passed EXPLICITLY instead of relying on
    Stage 2's implicit `os.path.join(output_dir, "stage1_health_report.json")`
    default. That default is reconstructed inside the child from whatever
    `--output-dir` it was handed; if that value were relative, the child would
    resolve it against its own CWD and silently read a DIFFERENT (or missing)
    gate report -- exactly the class of defect that made `--health-report-file`
    a dangling argument before T0.3. Both flags now carry the same absolute
    path, and that path is the one `generate_reports` actually wrote.

    Every path-valued flag is absolutized (see `_absolutize_child_arg`), so the
    resulting argv is identical no matter which directory the operator ran
    Stage 1 from.
    """
    for required in ("output_dir", "universe_file", "strategy_file", "account_state_file"):
        if not getattr(args, required, None):
            raise ValueError(
                f"build_stage2_command: --{required.replace('_', '-')} must be resolved "
                "before Stage 2 can be auto-launched"
            )

    stage2_script = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    output_dir = _absolutize_child_arg(args.output_dir)
    # Same filename `generate_reports` wrote above (see STAGE1_HEALTH_REPORT_JSON).
    health_report_file = os.path.join(output_dir, STAGE1_HEALTH_REPORT_JSON)

    cmd = [
        sys.executable,
        stage2_script,
        "--portfolio-value",
        str(args.portfolio_value),
        "--universe-file",
        _absolutize_child_arg(args.universe_file),
        "--strategy-file",
        _absolutize_child_arg(args.strategy_file),
        "--data-provider",
        args.data_provider,
        "--output-dir",
        output_dir,
        "--account-state-file",
        _absolutize_child_arg(args.account_state_file),
        # Explicit, absolute, and identical to the file this process just wrote.
        "--health-report-file",
        health_report_file,
    ]
    if args.as_of_date:
        cmd.extend(["--as-of-date", args.as_of_date])
    if getattr(args, "start_date", None):
        cmd.extend(["--start-date", args.start_date])
    if getattr(args, "cache_dir", None):
        cmd.extend(["--cache-dir", _absolutize_child_arg(args.cache_dir)])
    if getattr(args, "data_dir", None):
        cmd.extend(["--data-dir", _absolutize_child_arg(args.data_dir)])
    if getattr(args, "no_cache", False):
        cmd.append("--no-cache")
    if enable_intraday_5m:
        cmd.append("--enable-intraday-5m")
    # (L2.3) Forward the market-clock policy the operator chose for Stage 1 so the
    # auto-launched Stage 2 resolves the SAME market/as-of/grace and applies the same
    # gate strictness. Without this, Stage 1 could run under `--market hk` (or an
    # explicitly downgraded gate) and Stage 2 would silently re-infer both.
    if getattr(args, "market", None):
        cmd.extend(["--market", args.market])
    if getattr(args, "allow_intraday", False):
        cmd.append("--allow-intraday")
    if not getattr(args, "strict_market_clock", True):
        cmd.append("--no-strict-market-clock")
    if not getattr(args, "strict_freshness", True) or getattr(args, "allow_stale_prices", False):
        cmd.append("--no-strict-freshness")
    if getattr(args, "publication_grace_minutes", None) is not None:
        cmd.extend(["--publication-grace-minutes", str(args.publication_grace_minutes)])
    if getattr(args, "max_probe_attempts", None) is not None:
        cmd.extend(["--max-probe-attempts", str(args.max_probe_attempts)])
    return cmd


def main(cli_args: Optional[List[str]] = None):
    args = parse_args(cli_args)

    # (L2.3) The as-of date is the MARKET's latest published session, resolved by
    # `common/market_clock.py`; `date.today()` on a UTC-7 host is not an acceptable
    # substitute (design §2/§5). The run-window gate runs before any data load, so a
    # 10:05-style invocation (2026-10-22) is refused before it can produce a report.
    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")
    clock_ctx = resolve_live_clock_context(args, universe_symbols, stage="stage1")
    as_of_date = clock_ctx.as_of_date
    if getattr(args, "start_date", None):
        start_date = args.start_date
    else:
        start_date = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    # 1. Load Strategy Definition
    if not os.path.exists(args.strategy_file):
        raise FileNotFoundError(f"Strategy file not found at: {args.strategy_file}")
    strategy_def = load_strategy_file(args.strategy_file)
    params = strategy_def.get("params", {})
    strat_name = (
        strategy_def.get("strategy_name")
        or strategy_def.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
        or strategy_def.get("template_name", "").replace("_", " ").title()
    )
    strat_ver = strategy_def.get("strategy_version", "1.0.0")
    base_ver = strategy_def.get("base_version")
    ver_info = strategy_def.get("version_info")
    strategy_info = {
        "strategy_name": strat_name,
        "template_name": strategy_def.get("template_name", ""),
        "strategy_version": str(strat_ver),
        "base_version": str(base_ver) if base_ver else None,
        "version_info": ver_info,
        "weighting_mode": params.get("blend_weighting_mode", "rolling_ic_ir"),
        "orthogonalization": params.get("blend_enable_orthogonalization", True),
    }

    # 2. Load Universe Data
    if not universe_symbols:
        raise ValueError(f"Could not resolve universe symbols from {args.universe_file}")

    cache_dir = args.cache_dir or shared_data_dir()
    universe, probe_attempts = load_live_universe(
        args,
        universe_symbols,
        start_date,
        as_of_date,
        clock_ctx=clock_ctx,
        use_cache=not args.no_cache,
        cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args),
        require_nonempty=True,
    )
    universe = as_of_universe(universe, as_of_date)
    # (L2.3) A stale bar here means breadth/thrust/vol would be computed on yesterday's
    # closes -- the same silent substitution the incident was made of, one stage earlier.
    freshness_summary = assert_fresh_universe(
        universe,
        as_of_date,
        strict=clock_ctx.freshness_strict,
        expected_symbols=universe_symbols,
        exclude=(params.get("cash_proxy", "BIL"),),
        stage="stage1",
    )

    # 3. Load Account State & Evaluate Portfolio Drawdown
    state = load_account_state(args.account_state_file)

    # Establish baseline state for evaluation:
    # If state was already updated for this exact as_of_date (same-day re-run),
    # roll back to prior_state (state entering today) to ensure complete idempotency.
    is_same_day = (state.get("as_of_date") == as_of_date)
    if is_same_day and isinstance(state.get("prior_state"), dict):
        baseline_state = copy.deepcopy(state["prior_state"])
        prior_state_to_preserve = copy.deepcopy(state["prior_state"])
    elif is_same_day and "prior_state" not in state:
        # Fallback for states created before prior_state tracking was added:
        # Prevent double-incrementing counters and duplicate nav_hist entries on same-day re-runs
        baseline_state = copy.deepcopy(state)
        if baseline_state.get("tier1_consecutive_bars", 0) > 0:
            baseline_state["tier1_consecutive_bars"] -= 1
        if baseline_state.get("tier3_consecutive_bars", 0) > 0:
            baseline_state["tier3_consecutive_bars"] -= 1
        if baseline_state.get("nav_history_10d"):
            baseline_state["nav_history_10d"] = baseline_state["nav_history_10d"][:-1]
        prior_state_to_preserve = copy.deepcopy(baseline_state)
    else:
        # First run on a new date: baseline is the current state
        baseline_state = copy.deepcopy(state)
        prior_state_to_preserve = copy.deepcopy(state)
        prior_state_to_preserve.pop("prior_state", None)

    dd_info = inspect_portfolio_drawdown(
        current_nav=args.portfolio_value,
        peak_nav=args.peak_nav,
        state=baseline_state,
        params=params,
        as_of_date=as_of_date,
    )

    # 4. Evaluate Universe Breadth, Thrust & Volatility (and optional 5m Intraday Enrichment)
    enable_intraday_5m = getattr(args, "enable_intraday_5m", False) or bool(
        resolve_blend_param(params, "enable_intraday_5m", False, bool)
    )
    intraday_5m_info = None
    if enable_intraday_5m:
        try:
            from common.intraday_features import (
                IntradayFeatureConfig,
                load_intraday_5m_universe,
                enrich_universe_with_intraday_features,
            )
            provider = get_data_provider(args.data_provider, **build_data_kwargs(args))
            cutoff_t = getattr(args, "cutoff_time", "14:00") or "14:00"
            universe_5m = load_intraday_5m_universe(
                universe_symbols,
                start=start_date,
                end=as_of_date,
                data_provider=provider,
                cutoff_time=cutoff_t,
            )
            universe = enrich_universe_with_intraday_features(
                universe, universe_5m, config=IntradayFeatureConfig(cutoff_time=cutoff_t), synthesize_intraday_bar=True
            )

            # (2026-10-22 incident, Fix 1) HONESTY GATE. This block used to
            # substitute NaN -> defaults for every microstructure field
            # (`VWAP_Prem_5m` -> 0.0, `UpVol_Ratio_5m` -> 0.5, `Afternoon_Ret_5m`
            # -> 0.0) and then divide, producing `intraday_vwap_breadth = 1.0`,
            # `afternoon_return_mean = 0.0`, `afternoon_vwap_breakdown_ratio =
            # 0.0` on a run where the 5m frame was EMPTY (the configured provider
            # raises on interval != "1d"). A breadth of 1.0 is a PASSING value for
            # the distribution gate, so the gate reported success on data that
            # did not exist. The defaults are now GONE: we first determine how
            # many symbols actually carry a measured feature, and when none do we
            # emit None + `intraday_features_available: false` +
            # `distribution_gate_status: "SKIPPED"` with a logged reason.
            cash_proxy = params.get("cash_proxy", "BIL")
            n_5m_ok = sum(
                1
                for _s in universe_symbols
                if _s in universe_5m and universe_5m[_s] is not None and not universe_5m[_s].empty
            )
            measured_syms: List[str] = []
            for sym in universe_symbols:
                if sym == cash_proxy or sym not in universe:
                    continue
                df_sym = universe[sym]
                if df_sym.empty or "VWAP_Prem_5m" not in df_sym.columns:
                    continue
                if pd.isna(df_sym["VWAP_Prem_5m"].iloc[-1]):
                    continue
                measured_syms.append(sym)

            if not measured_syms:
                _unavail_reason = (
                    f"5m 日内特征缺失: {len(universe_symbols)} 个标的中 5m 数据帧非空的仅 {n_5m_ok} 个，"
                    "且无任何标的具备已测得的 VWAP_Prem_5m 特征 "
                    f"(provider={args.data_provider})。常见原因: provider does not support 5m。"
                )
                print(f"[WARN] {_unavail_reason}")
                print("[WARN] 日内分布门禁状态 = SKIPPED (UNKNOWN)：未测得数据不得视为 PASS，本次不会因日内跳水下调买入预算。")
                intraday_5m_info = {
                    "enabled": True,
                    "available": False,
                    "intraday_features_available": False,
                    "cutoff_time": cutoff_t,
                    "unavailable_reason": _unavail_reason,
                    "note": (
                        "5m 日内指标未测得，全部以 null 表达；distribution gate = SKIPPED (UNKNOWN), "
                        "不是 PASS。intraday_buy_scale 保持 1.0 仅表示未施加任何缩放。"
                    ),
                    "symbols_5m_available": n_5m_ok,
                    "symbols_measured": 0,
                    "symbols_vwap_measured": 0,
                    # Everything that should have carried a feature is, by
                    # definition, missing one here -- named rather than silently
                    # counted, so the operator can see the blast radius.
                    "symbols_missing_features": [
                        _s for _s in universe_symbols if _s != cash_proxy
                    ],
                    # Measured metrics: genuinely unknown -> null (never a
                    # fabricated 1.0/0.0 that could read as "gate passed").
                    "intraday_vwap_breadth": None,
                    "afternoon_return_mean": None,
                    "afternoon_vwap_breakdown_ratio": None,
                    "distribution_alert": None,
                    "distribution_gate_status": "SKIPPED",
                    "trough_alert": None,
                    "hwm_intra": None,
                    "dd_trough": None,
                    "intraday_buy_scale": 1.0,
                }
            else:

                # Evaluate Afternoon Distribution & VWAP breadth (measured symbols only)
                vwap_ok_count = 0
                risky_count = 0
                afternoon_rets = []
                breakdown_count = 0
                missing_feature_syms: List[str] = []
                for sym in universe_symbols:
                    if sym == cash_proxy or sym not in universe:
                        continue
                    df_sym = universe[sym]
                    if df_sym.empty:
                        continue
                    row = df_sym.iloc[-1]
                    if sym not in measured_syms:
                        # Never fabricate: a symbol without a measured feature is
                        # excluded from the breadth denominator and named in the report.
                        missing_feature_syms.append(sym)
                        continue
                    has_upvol = "UpVol_Ratio_5m" in row and pd.notna(row["UpVol_Ratio_5m"])
                    has_aft = "Afternoon_Ret_5m" in row and pd.notna(row["Afternoon_Ret_5m"])
                    if not has_upvol:
                        # (2026-10-22 live-path hardening, FIX 1) The breadth
                        # NUMERATOR requires a measured up-volume ratio, so a
                        # symbol without one can never count as a hit. Keeping it
                        # in the DENOMINATOR would then manufacture a breadth of
                        # 0.0 -- which is below the 25% alert line, i.e. missing
                        # data would fabricate a genuine-looking
                        # "afternoon distribution detected" CAUTION. Missing
                        # data must never masquerade as a risk assessment in
                        # EITHER direction, so such symbols leave the denominator
                        # too and are named in `symbols_missing_features`.
                        missing_feature_syms.append(sym)
                        continue
                    risky_count += 1
                    vwap_prem = float(row["VWAP_Prem_5m"])
                    upvol = float(row["UpVol_Ratio_5m"])
                    aft_ret = float(row["Afternoon_Ret_5m"]) if has_aft else None

                    # A breadth hit requires BOTH measured conditions; a missing
                    # up-volume ratio can no longer be counted as a favourable 0.5.
                    if vwap_prem >= 0.0 and upvol >= 0.50:
                        vwap_ok_count += 1
                    if aft_ret is not None:
                        afternoon_rets.append(aft_ret)
                    if vwap_prem < 0.0 and aft_ret is not None and aft_ret < -0.01:
                        breakdown_count += 1

                intraday_vwap_breadth = vwap_ok_count / risky_count if risky_count > 0 else None
                afternoon_ret_mean = float(np.mean(afternoon_rets)) if afternoon_rets else None
                breakdown_ratio = (breakdown_count / risky_count) if risky_count > 0 else None
                # The alert can only be evaluated from measured values; an absent
                # component makes the aggregate unknown rather than "no alert".
                if intraday_vwap_breadth is None:
                    distribution_alert = None
                elif intraday_vwap_breadth < 0.25:
                    distribution_alert = True
                elif afternoon_ret_mean is not None and breakdown_ratio is not None:
                    distribution_alert = bool(afternoon_ret_mean < -0.012 and breakdown_ratio >= 0.50)
                else:
                    distribution_alert = False

                # Intraday NAV trough evaluation (R5: Diagnostic only, do NOT overwrite peak_nav)
                hwm_intra = float(dd_info["peak_nav"])
                dd_trough = float(dd_info["drawdown_pct"])
                trough_alert = False

                # Load holdings to calculate mark-to-market NAV
                holdings = {}
                if getattr(args, "current_holdings_file", None) and os.path.exists(args.current_holdings_file):
                    try:
                        with open(args.current_holdings_file, "r", encoding="utf-8") as f:
                            holdings_data = json.load(f)
                        if isinstance(holdings_data, dict):
                            holdings = holdings_data.get("holdings", holdings_data)
                    except Exception as e:
                        print(f"[WARN] Failed to read current_holdings_file: {e}")
                elif state.get("current_holdings"):
                    holdings = state["current_holdings"]
                elif state.get("positions"):
                    holdings = state["positions"]

                if holdings and universe_5m:
                    ref_ts = None
                    for sym, df_5 in universe_5m.items():
                        if not df_5.empty and sym in holdings:
                            ts_sub = [idx for idx in df_5.index if str(idx.date()) == as_of_date and idx.time() <= pd.Timestamp(cutoff_t).time()]
                            if len(ts_sub) > 0:
                                ref_ts = ts_sub
                                break

                    if ref_ts and len(ref_ts) > 0:
                        nav_series = []
                        for ts_bar in ref_ts:
                            bar_nav = 0.0
                            total_weight = 0.0
                            for sym, pos_val in holdings.items():
                                shares = 0
                                weight = 0.0
                                if isinstance(pos_val, (int, float)):
                                    if pos_val > 1.0:
                                        shares = pos_val
                                    else:
                                        weight = pos_val
                                elif isinstance(pos_val, dict):
                                    shares = pos_val.get("shares", 0)
                                    weight = pos_val.get("weight", 0.0)

                                if sym in universe_5m and ts_bar in universe_5m[sym].index:
                                    p_close = float(universe_5m[sym].loc[ts_bar, "Close"])
                                    if shares > 0:
                                        bar_nav += shares * p_close
                                    elif weight > 0:
                                        bar_nav += weight * args.portfolio_value
                                        total_weight += weight
                                elif sym == cash_proxy:
                                    if weight > 0:
                                        bar_nav += weight * args.portfolio_value
                                        total_weight += weight

                            if bar_nav > 0:
                                if total_weight > 0 and total_weight < 1.0:
                                    bar_nav += (1.0 - total_weight) * args.portfolio_value
                                nav_series.append(bar_nav)

                        if nav_series:
                            intra_peak = max(nav_series)
                            intra_min = min(nav_series)
                            hwm_intra = max(float(dd_info["peak_nav"]), intra_peak)
                            dd_trough = (intra_min - hwm_intra) / hwm_intra if hwm_intra > 0 else 0.0
                            dd_defensive = float(resolve_blend_param(params, "dd_defensive_thresh", 0.15, float))
                            if abs(dd_trough) >= dd_defensive and abs(dd_info["drawdown_pct"]) < dd_defensive:
                                trough_alert = True

                intraday_5m_info = {
                    "enabled": True,
                    "available": True,
                    "intraday_features_available": True,
                    "cutoff_time": cutoff_t,
                    "symbols_5m_available": n_5m_ok,
                    # `symbols_measured` = symbols that actually fed the breadth
                    # aggregate (VWAP premium AND up-volume both measured);
                    # `symbols_vwap_measured` = symbols with a VWAP premium at
                    # all, which is what makes the gate "available". They differ
                    # exactly when a symbol has one feature but not the other.
                    "symbols_measured": risky_count,
                    "symbols_vwap_measured": len(measured_syms),
                    "symbols_missing_features": missing_feature_syms,
                    # An UNKNOWN alert (None) is preserved as None: it means the
                    # aggregate could not be evaluated, which is NOT the same as
                    # "no distribution detected".
                    "intraday_vwap_breadth": intraday_vwap_breadth,
                    "afternoon_return_mean": afternoon_ret_mean,
                    "afternoon_vwap_breakdown_ratio": breakdown_ratio,
                    "distribution_alert": distribution_alert,
                    "distribution_gate_status": (
                        "UNKNOWN" if distribution_alert is None else "EVALUATED"
                    ),
                    "trough_alert": trough_alert,
                    "hwm_intra": hwm_intra,
                    "dd_trough": dd_trough,
                    # Scaling is only ever applied on a POSITIVE detection; an
                    # unavailable/unknown gate never silently scales and never
                    # claims to have passed.
                    "intraday_buy_scale": 0.50 if distribution_alert else 1.0,
                }
        except Exception as exc:
            print(f"[WARN] Failed to evaluate 5m intraday features: {exc}")

    macro_info = evaluate_market_breadth_and_thrust(universe, params)
    vol_info = evaluate_realized_volatility(universe, params)

    # 5. Synthesize Gate Directive
    gate_code, gate_headline, actions = resolve_gate_directive(dd_info, macro_info, vol_info, intraday_5m_info)

    # 6. Update Account State
    if not args.no_save_state:
        nav_hist = list(baseline_state.get("nav_history_10d", []))
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
            "gate_code": gate_code,
            "intraday_buy_scale": intraday_5m_info.get("intraday_buy_scale", 1.0) if intraday_5m_info else 1.0,
            "nav_history_10d": nav_hist,
            "prior_state": prior_state_to_preserve,
        })
        save_account_state(args.account_state_file, updated_state)

    # 7. Print Dashboard & Export Reports
    print_dashboard(
        as_of_date=as_of_date,
        universe_path=args.universe_file,
        strategy_path=args.strategy_file,
        strategy_info=strategy_info,
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code=gate_code,
        gate_headline=gate_headline,
        actions=actions,
        intraday_5m_info=intraday_5m_info,
    )

    json_rep, md_rep = generate_reports(
        output_dir=args.output_dir,
        as_of_date=as_of_date,
        strategy_info=strategy_info,
        dd_info=dd_info,
        macro_info=macro_info,
        vol_info=vol_info,
        gate_code=gate_code,
        gate_headline=gate_headline,
        actions=actions,
        intraday_5m_info=intraday_5m_info,
        clock_ctx=clock_ctx,
        freshness_summary=freshness_summary,
    )
    # (L2.3, design §9) `summary.json` is the shared per-run archive key/value store the
    # web layer merges into; the clock + freshness blocks land there too.
    persist_run_metadata(
        args.output_dir,
        clock_ctx,
        freshness_summary=freshness_summary,
        probe_attempts=probe_attempts,
        extra={"date": as_of_date, "stage": "stage1", "gate_code": gate_code},
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
            # (P1.3) argv construction lives in `build_stage2_command` so the offline
            # suite can assert the exact flag set (notably the explicit absolute
            # `--health-report-file`) without launching a child process.
            cmd = build_stage2_command(args, enable_intraday_5m=enable_intraday_5m)
            print(f"\n[AUTO-EXECUTE] Launching Stage 2: {os.path.basename(cmd[1])} ...")
            import subprocess
            subprocess.run(cmd, check=True)

    return {
        "gate_code": gate_code,
        "gate_headline": gate_headline,
        "actions": actions,
        "dd_info": dd_info,
        "macro_info": macro_info,
        "vol_info": vol_info,
        "intraday_5m_info": intraday_5m_info,
    }


if __name__ == "__main__":
    main()

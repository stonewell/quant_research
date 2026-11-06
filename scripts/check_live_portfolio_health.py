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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    CASH_PROXY_SYMBOLS,
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
from common.live_execution_timing import (
    merge_timing_into_summary,
    resolve_execution_timing,
)
from common.strategy_spec import load_strategy_file
from common.universe import resolve_universe_from_args
from pipeline.live_signal.lsig.signal import as_of_universe

# Default output-directory owner (Stage 1 and Stage 2 must resolve ONE directory:
# Stage 1 writes `stage1_health_report.json` / reads `account_state.json` there, and
# Stage 2 reads/writes the same two files). See `scripts/live_paths.py`.
from scripts.live_paths import DEFAULT_STRATEGY_KEY, default_live_output_dir
from scripts.live_preflight import (
    LiveDumpPreflightError,
    verify_live_strategy_dump_preflight,
)

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
# Default *output directory* owner -- shared with Stage 2
# (`scripts/run_live_strategy.py`), which reads back the `stage1_health_report.json`
# this script writes there (and rewrites the `account_state.json` this script reads).
# Both sides resolve it through `scripts/live_paths.py` so "health check writes here,
# tickets read there" cannot split; neither file may re-spell the path or the key.
DEFAULT_OUTPUT_DIR = default_live_output_dir(DEFAULT_STRATEGY_KEY)

#: Filename Stage 1 writes and Stage 2 reads back. One owner: `generate_reports`
#: writes it, `build_stage2_command` passes it, and Stage 2's own
#: `--health-report-file` default reconstructs the identical name. Never re-spell
#: this literal at either end.
STAGE1_HEALTH_REPORT_JSON = "stage1_health_report.json"

#: Bars required before the 10-day NAV recovery leg of the fast-recovery override
#: is considered MEASURED. Below this the leg is `INSUFFICIENT_HISTORY` (return
#: `None`), never a fabricated 0.00%. One owner: `inspect_portfolio_drawdown`
#: gates on it, the dashboard/Markdown renderers quote it, and
#: `append_nav_history_point` caps the persisted series at exactly this length.
NAV_10D_RECOVERY_WINDOW = 10

#: Status values for the 10-day NAV recovery leg. `INSUFFICIENT_HISTORY` means
#: "not measured" -- it is NOT a flat 0.00% observation, and it never switches
#: the override ON (see `inspect_portfolio_drawdown`).
NAV_10D_STATUS_OK = "OK"
NAV_10D_STATUS_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

#: ---------------------------------------------------------------------------
#: (LV1, 2026-11-03) FROZEN CONTRACT -- the Stage-1 gate NAV source.
#:
#: Written down BEFORE the code, per the LV1 acceptance criteria
#: (`docs/development_plan.md`, LV1 item 1). Do not amend silently.
#:
#: (a) SOURCE OF TRUTH. `inspect_portfolio_drawdown`'s drawdown / circuit-breaker /
#:     tier math is fed the MARKED-TO-MARKET (MTM) NAV, read from
#:     `nav_breakdown.json::nav_effective`, whose sole producer is the valuation
#:     layer (`web/livetrading/execution_service.py::_write_nav_breakdown`).
#:     The operator form value (`--portfolio-value`) is NOT the gate NAV any more.
#:     Fact base (two archived runs, 10-30 and 11-02): `summary.json.portfolio_value`
#:     was frozen at 199935.46 on BOTH days while `nav_effective` moved
#:     191630.06 -> 192096.06 -> drawdown was recorded as -1.53% while the true MTM
#:     drawdown against the 203049.96 peak was -5.62% / -5.39% (understated by
#:     4.09 / 3.86 percentage points), and `tier1_consecutive_bars` stayed pinned at
#:     0 because the counter ran on the understated number.
#:
#: (b) DISAGREEMENT. When MTM and the form value disagree, `tier`,
#:     `drawdown_pct` and `tier1_consecutive_bars` are recomputed FROM MTM. The form
#:     value survives ONLY as an input-sanity cross-check: it is persisted
#:     (`nav_form`), diffed (`nav_deviation_pct` / `nav_deviation_exceeded`) and
#:     rendered in both the Markdown row and the JSON report. It never re-enters the
#:     gate arithmetic.
#:
#: (c) MISSING MTM. An absent / unreadable / non-positive `nav_effective` is an
#:     EXPLICIT DEGRADATION, never a silent fallback: `nav_source` becomes
#:     `form_fallback_refused`, `nav_gate_status` becomes `DEGRADED_NAV_UNAVAILABLE`,
#:     `tier_status` becomes `UNKNOWN`, `nav_deviation_pct` /
#:     `nav_deviation_exceeded` stay `None` (NOT `False` -- "not measurable" must
#:     stay distinguishable from "inside threshold"), a loud `[WARN]` is printed, and
#:     the Markdown row renders UNKNOWN. The breaker is then NEVER reported as
#:     "not triggered" (missing != not-triggered; same rule as the 10-day NAV
#:     recovery leg and §2.19 D11 item 4).
#:
#: INVALIDATION CONDITION: this contract is void the moment either (i) the valuation
#:     layer stops writing `nav_effective` beside `account_state.json`, or (ii) any
#:     code path feeds the form value back in as the gate NAV. On either event the
#:     LV1 row in `docs/development_plan.md` must be reopened before code moves.
#: ---------------------------------------------------------------------------
NAV_BREAKDOWN_FILENAME = "nav_breakdown.json"  # written by the valuation layer; Stage 1 reads it
NAV_BREAKDOWN_EFFECTIVE_KEY = "nav_effective"

#: FROZEN deviation threshold (LV1 item 3): |MTM - form| / |form| above this ratio is
#: an explicit, persisted, rendered deviation event -- never a log line only.
NAV_DEVIATION_WARN_PCT = 0.01

#: Why the MTM NAV was (or was not) available. `OK` is the only value that lets
#: `resolve_gate_nav` return an MTM NAV.
NAV_BREAKDOWN_STATUS_OK = "OK"
#: (LV11) `FILE_MISSING` is kept for the "no path supplied at all" case only. An ABSENT file
#: at a path the caller did supply is no longer collapsed into it: "not staged yet" and
#: "should be there but isn't" are different operational facts and used to be
#: indistinguishable here, which is exactly why every fresh run directory looked like a
#: permanent degradation.
NAV_BREAKDOWN_STATUS_FILE_MISSING = "FILE_MISSING"
#: The valuation layer has not written this run's snapshot yet (a TIMING gap -- Stage 1 was
#: launched before the book was marked). Normal for a first run of a session before LV11's
#: pre-staging; never evidence that the snapshot was lost.
NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN = "SNAPSHOT_NOT_YET_WRITTEN"
#: This run directory already carries evidence of a completed valuation round
#: (`summary.json` / `holdings.json` / a Stage-1 report), yet the snapshot is absent => the
#: snapshot was lost, not deferred. Operationally different from the line above.
NAV_BREAKDOWN_STATUS_SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
NAV_BREAKDOWN_STATUS_UNREADABLE = "UNREADABLE"
NAV_BREAKDOWN_STATUS_EFFECTIVE_MISSING = "NAV_EFFECTIVE_MISSING"
NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID = "NAV_EFFECTIVE_INVALID"

#: Files whose presence proves this run directory already ran a round. Used ONLY to split
#: `SNAPSHOT_NOT_YET_WRITTEN` from `SNAPSHOT_MISSING`; `account_state.json` is deliberately
#: excluded because the web layer seeds it BEFORE launching Stage 1 (so it would make every
#: first run look like a lost snapshot).
_RUN_DIR_ROUND_MARKERS = (
    "summary.json",
    "holdings.json",
    "post_holdings.json",
    "live_trading_ticket.json",
    "stage1_health_report.json",
)

#: Which NAV the gate used. `mtm` == measured; `form_fallback_refused` == degraded.
NAV_SOURCE_MTM = "mtm"
NAV_SOURCE_FORM_FALLBACK_REFUSED = "form_fallback_refused"

#: Gate verdict availability. `DEGRADED_NAV_UNAVAILABLE` means the tier below is an
#: UNEVALUATED placeholder, not a passing verdict.
NAV_GATE_STATUS_EVALUATED = "EVALUATED"
NAV_GATE_STATUS_DEGRADED = "DEGRADED_NAV_UNAVAILABLE"


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
        "--nav-breakdown-file",
        default=None,
        help=(
            "(LV1) Path to the marked-to-market valuation snapshot the GATE NAV is read from "
            "(default: nav_breakdown.json in --output-dir, i.e. the file the web valuation layer "
            "writes next to account_state.json). This file -- not --portfolio-value -- supplies "
            "the drawdown / circuit-breaker NAV."
        ),
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
        help=(
            "Directory to save generated health reports (default: "
            "scripts/results/live/chan_four_state_blend -- the same directory Stage 2 "
            "defaults to, never docs/ruleset/)"
        ),
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
    parser.add_argument(
        "--refresh-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Pre-flight refresh/patch of market data cache (indices 1d, universe 1d, and 5m when "
            "--enable-intraday-5m is active) up to as_of_date before Stage 1 starts work "
            "(default: True; automatically skipped when --data-provider is 'synthetic' or --no-cache)."
        ),
    )
    parser.add_argument(
        "--allow-create-cache",
        action="store_true",
        default=False,
        help=(
            "仅在你确实要重建缓存库时使用: 默认情况下，缓存库（<cache-dir>/cache.duckdb）缺失会被"
            "直接拒绝 (status=refused, 退出码非 0)，因为 Stage-1 的职责是刷新行情，而不是重建缓存。"
            "缓存被误删时应先恢复备份；确需从零重建再显式加本开关。"
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
    # (LV1) Default the MTM valuation snapshot beside the state file -- same run
    # directory the web layer launches Stage 1 with (`--output-dir run_dir`), so no
    # new flag is required in the deployed path; `--nav-breakdown-file` only exists
    # for ad-hoc / test runs.
    # (LV11) Remember whether the operator PINNED the snapshot path before defaulting it.
    # A pinned path must exist (fails closed); the defaulted path may legitimately be absent
    # on a run that has not been valued yet, and is reported as `SNAPSHOT_NOT_YET_WRITTEN`.
    parsed.nav_breakdown_file_explicit = parsed.nav_breakdown_file is not None
    if parsed.nav_breakdown_file is None:
        parsed.nav_breakdown_file = os.path.join(parsed.output_dir, NAV_BREAKDOWN_FILENAME)
    else:
        parsed.nav_breakdown_file = _resolve_path(parsed.nav_breakdown_file)
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


class NavSnapshotError(RuntimeError):
    """(LV11) An explicitly pinned valuation snapshot is missing or unparsable.

    Deliberately NOT degraded-and-continue: `--nav-breakdown-file` means "the gate NAV comes
    from THIS file", so a run that cannot read it must abort instead of publishing a report
    whose breaker verdict is an unevaluated placeholder. (Distinct from the *default* path:
    when nobody pinned a file, an absent snapshot is a documented timing gap -- see
    `NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN`.)
    """


def _snapshot_absence_status(path: str) -> str:
    """Split an absent snapshot into "not staged yet" vs "lost" (LV11).

    A run directory that already holds a round artifact has demonstrably been valued at some
    point, so an absent snapshot there is a LOSS; an otherwise-empty directory is simply a
    Stage 1 that ran before anything was marked to market.
    """
    try:
        run_dir = os.path.dirname(os.path.abspath(path))
    except Exception:
        return NAV_BREAKDOWN_STATUS_SNAPSHOT_MISSING
    if not os.path.isdir(run_dir):
        return NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN
    for marker in _RUN_DIR_ROUND_MARKERS:
        if os.path.exists(os.path.join(run_dir, marker)):
            return NAV_BREAKDOWN_STATUS_SNAPSHOT_MISSING
    return NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN


def load_nav_breakdown(
    path: Optional[str], *, explicit: bool = False
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read `nav_breakdown.json` (written by the valuation layer). I/O only.

    Returns `(payload_or_None, status)`. Every failure path returns a *status code*
    instead of raising or substituting a number, so `resolve_gate_nav` can turn it
    into an explicit degradation rather than a silent fallback.

    (LV11) Two behaviours changed:

    * an ABSENT file is classified as `SNAPSHOT_NOT_YET_WRITTEN` (timing gap) or
      `SNAPSHOT_MISSING` (the run dir proves a round already happened) instead of both
      collapsing into `FILE_MISSING`;
    * `explicit=True` (the operator pinned `--nav-breakdown-file`) turns a missing or
      unparsable snapshot into `NavSnapshotError` -- fail closed, no degraded report.
    """
    if not path:
        return None, NAV_BREAKDOWN_STATUS_FILE_MISSING
    if not os.path.exists(path):
        if explicit:
            raise NavSnapshotError(
                f"(LV11) --nav-breakdown-file={path} 不存在：显式指定的估值快照必须可读，"
                "拒绝降级后继续（缺失 ≠ 未触发）。"
            )
        return None, _snapshot_absence_status(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        if explicit:
            raise NavSnapshotError(
                f"(LV11) --nav-breakdown-file={path} 无法解析: {exc}；显式指定的估值快照"
                "必须可读，拒绝降级后继续。"
            ) from exc
        return None, NAV_BREAKDOWN_STATUS_UNREADABLE
    if not isinstance(payload, dict):
        if explicit:
            raise NavSnapshotError(
                f"(LV11) --nav-breakdown-file={path} 顶层不是 JSON 对象；拒绝降级后继续。"
            )
        return None, NAV_BREAKDOWN_STATUS_UNREADABLE
    return payload, NAV_BREAKDOWN_STATUS_OK


def resolve_gate_nav(
    nav_breakdown: Optional[Dict[str, Any]],
    form_nav: Optional[float],
    breakdown_status: str = NAV_BREAKDOWN_STATUS_OK,
    deviation_threshold: float = NAV_DEVIATION_WARN_PCT,
) -> Dict[str, Any]:
    """PURE resolver for the Stage-1 gate NAV (LV1). No I/O, no globals mutated.

    Chosen deliberately as a new helper rather than an existing one: a grep of
    `common/`, `scripts/` and `web/livetrading/` finds `nav_effective` PRODUCERS
    (`web/livetrading/execution_service.py::_write_nav_breakdown`) and archival
    consumers (`scripts/repair_live_run_snapshots.py`), but no shared resolver for
    "which NAV should the gate use" -- Stage 1 had zero reads of
    `nav_breakdown.json` before this change. Inventing a second reader elsewhere
    would have split the ownership of the decision, so the decision lives here, next
    to its only consumer.

    Contract (frozen -- see the `LV1` block above `NAV_BREAKDOWN_FILENAME`):
      * MTM available  -> `nav_source = "mtm"`, `gate_nav` = MTM, deviation fields
        measured (`nav_deviation_exceeded` is a real bool).
      * MTM missing    -> `nav_source = "form_fallback_refused"`,
        `nav_gate_status = "DEGRADED_NAV_UNAVAILABLE"`, `gate_nav = None`, and the
        deviation fields are `None` (NOT `False`) so "not measurable" can never be
        read as "inside threshold".

    Deviation is signed and measured against the form value:
    `(mtm - form) / |form|`. A negative value means the form overstates NAV -- the
    exact direction of the 10-30 / 11-02 incident.
    """
    form_val: Optional[float]
    try:
        form_val = float(form_nav) if form_nav is not None else None
    except (TypeError, ValueError):
        form_val = None

    status = breakdown_status or NAV_BREAKDOWN_STATUS_OK
    mtm: Optional[float] = None
    if status == NAV_BREAKDOWN_STATUS_OK:
        raw = nav_breakdown.get(NAV_BREAKDOWN_EFFECTIVE_KEY) if isinstance(nav_breakdown, dict) else None
        if raw is None:
            status = NAV_BREAKDOWN_STATUS_EFFECTIVE_MISSING
        else:
            try:
                candidate = float(raw)
            except (TypeError, ValueError):
                status = NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID
            else:
                # A non-finite / non-positive MTM is not a usable NAV: `0.0` would
                # make the drawdown math divide by a fake zero, and `NaN` would
                # poison every downstream comparison into False ("not triggered").
                if not np.isfinite(candidate) or candidate <= 0.0:
                    status = NAV_BREAKDOWN_STATUS_EFFECTIVE_INVALID
                else:
                    mtm = candidate

    as_of = nav_breakdown.get("as_of") if isinstance(nav_breakdown, dict) else None
    if not isinstance(as_of, str):
        as_of = None

    if mtm is None:
        return {
            "nav_source": NAV_SOURCE_FORM_FALLBACK_REFUSED,
            "nav_mtm": None,
            "nav_form": form_val,
            "nav_deviation_pct": None,
            "nav_deviation_exceeded": None,
            "nav_deviation_threshold_pct": float(deviation_threshold),
            "nav_breakdown_status": status,
            "nav_breakdown_as_of": as_of,
            "nav_gate_status": NAV_GATE_STATUS_DEGRADED,
            "nav_gate_nav": None,
            "nav_degradation_reason": (
                f"nav_breakdown.json 未提供可用的 {NAV_BREAKDOWN_EFFECTIVE_KEY} "
                f"(status={status})：门禁净值不可判定，表单值 {form_val} 仅作占位显示，"
                "不构成盯市净值，熔断状态按 UNKNOWN 处理（缺失 ≠ 未触发）"
            ),
        }

    deviation_pct: Optional[float] = None
    if form_val is not None and abs(form_val) > 0.0:
        deviation_pct = (mtm - form_val) / abs(form_val)
    return {
        "nav_source": NAV_SOURCE_MTM,
        "nav_mtm": mtm,
        "nav_form": form_val,
        "nav_deviation_pct": deviation_pct,
        # `None` (unmeasurable) is preserved, never coerced to `False`.
        "nav_deviation_exceeded": (
            None if deviation_pct is None else bool(abs(deviation_pct) > float(deviation_threshold))
        ),
        "nav_deviation_threshold_pct": float(deviation_threshold),
        "nav_breakdown_status": status,
        "nav_breakdown_as_of": as_of,
        "nav_gate_status": NAV_GATE_STATUS_EVALUATED,
        "nav_gate_nav": mtm,
        "nav_degradation_reason": None,
    }


def resolve_gate_nav_context(args) -> Dict[str, Any]:
    """Read `nav_breakdown.json` and resolve the gate NAV for one Stage-1 run (LV1).

    The ONLY I/O in the LV1 path, and it is isolated here so the decision itself
    (`resolve_gate_nav`) stays a pure function. Emits the two loud warnings the
    frozen contract requires: deviation above threshold, and MTM unavailable.

    On a degraded run `nav_gate_nav` stays `None` -- the caller must decide what to
    display, and `attach_nav_provenance` will relabel the verdict UNKNOWN. The form
    value is exposed separately as `nav_gate_nav_used` so the substitution is
    explicit in the returned dict (and thus in the report) rather than implicit.
    """
    payload, status = load_nav_breakdown(
        getattr(args, "nav_breakdown_file", None),
        explicit=bool(getattr(args, "nav_breakdown_file_explicit", False)),
    )
    resolution = resolve_gate_nav(
        payload,
        getattr(args, "portfolio_value", None),
        breakdown_status=status,
    )
    if resolution["nav_gate_nav"] is None:
        # LOUD, not silent.
        print(
            "[WARN] (LV1) 门禁净值降级: 取不到盯市净值 "
            f"({resolution['nav_degradation_reason']})。"
            "本次回撤/熔断判定为 UNKNOWN，**不是**「未触发」；"
            "请恢复 nav_breakdown.json 后重跑，勿用表单值替代盯市净值。"
        )
        # (LV11) Say WHICH kind of absence this is. A timing gap is recoverable by staging
        # the snapshot (the web layer now does that before launching Stage 1); a lost
        # snapshot means this run directory already had one and it disappeared.
        if status == NAV_BREAKDOWN_STATUS_SNAPSHOT_NOT_YET_WRITTEN:
            print(
                "[WARN] (LV11) 分类: SNAPSHOT_NOT_YET_WRITTEN —— 本次运行目录尚无估值快照，"
                "属**时序缺口**（Stage 1 早于估值写盘启动）。web 层启动器应在拉起 Stage 1 前"
                "预置快照；直接命令行调用需先产出快照。"
            )
        elif status == NAV_BREAKDOWN_STATUS_SNAPSHOT_MISSING:
            print(
                "[WARN] (LV11) 分类: SNAPSHOT_MISSING —— 该运行目录已存在本轮产物，估值快照"
                "却缺失，属**真缺失**（未被删除/未写成功），不要当成「还没写」。"
            )
        try:
            resolution["nav_gate_nav_used"] = float(resolution["nav_form"])
        except (TypeError, ValueError):
            resolution["nav_gate_nav_used"] = None
    elif resolution["nav_deviation_exceeded"]:
        print(
            "[WARN] (LV1) 表单值与盯市净值偏离超阈值: "
            f"MTM {resolution['nav_mtm']:,.2f} vs 表单 {resolution['nav_form']:,.2f}"
            f"（偏离 {resolution['nav_deviation_pct']:+.2%}，阈值 "
            f"{resolution['nav_deviation_threshold_pct']:.2%}）。"
            "回撤/层级已按 MTM 重算，表单值仅留痕。"
        )
    return resolution


def attach_nav_provenance(dd_info: Dict[str, Any], nav_resolution: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the LV1 NAV-source provenance into `dd_info` (the dict that gets published).

    `dd_info` lands verbatim in `stage1_health_report.json::portfolio_health` and its
    numeric fields are copied into `account_state.json`, so the provenance has to
    travel with it -- a log line alone would be invisible to every consumer.

    On a degraded run the tier ENUM is deliberately left untouched (Stage 2 compares
    `circuit_breaker_tier == "TIER_3_HALT"`, and inventing a new enum value there would
    silently change order behaviour), but `tier_status` becomes `UNKNOWN` and the
    human label stops claiming a passing verdict: missing NAV must never be readable
    as "breaker not triggered".
    """
    for key in (
        "nav_source",
        "nav_mtm",
        "nav_form",
        "nav_deviation_pct",
        "nav_deviation_exceeded",
        "nav_deviation_threshold_pct",
        "nav_breakdown_status",
        "nav_breakdown_as_of",
        "nav_gate_status",
        "nav_degradation_reason",
    ):
        dd_info[key] = nav_resolution.get(key)
    if nav_resolution.get("nav_gate_nav") is None:
        dd_info["tier_status"] = "UNKNOWN"
        dd_info["tier_label"] = (
            "⚪ UNKNOWN (NAV DEGRADED — 盯市净值不可用，熔断层级未判定；缺失 ≠ 未触发)"
        )
        dd_info["nav_breaker_evaluated"] = False
    else:
        dd_info["tier_status"] = "EVALUATED"
        dd_info["nav_breaker_evaluated"] = True
    return dd_info


def build_account_state_update(
    state: Dict[str, Any],
    baseline_state: Dict[str, Any],
    dd_info: Dict[str, Any],
    *,
    as_of_date: str,
    gate_code: str,
    intraday_5m_info: Optional[Dict[str, Any]] = None,
    prior_state: Any = None,
) -> Dict[str, Any]:
    """The `account_state.json` delta one Stage-1 run persists (LV11 item 6).

    Extracted from `main()` ON PURPOSE. The 2026-11-03 post-commit audit found that the
    fail-closed behaviour of this block was guarded only by a source-string grep, because the
    block sat inline in `main()` and every offline test bypassed `main()`. As a pure function
    it is directly testable -- and the two lies it used to tell are both asserted against:

    * **degraded run** (`nav_gate_status == DEGRADED_NAV_UNAVAILABLE`): persists
      `circuit_breaker_tier="UNKNOWN"` instead of the placeholder's "NORMAL". Stage 2 compares
      that enum (`scripts/run_live_strategy.py`), so a passing-looking value here would let
      "report UNKNOWN / archive NORMAL / orders still issued" hold simultaneously.
      It also leaves `nav_history_10d` UNTOUCHED: appending the form placeholder would poison
      the 10-day recovery metric with a number nobody valued. `current_nav` / `peak_nav` /
      `drawdown_pct` / counters / scale are likewise NOT overwritten, because writing the
      placeholder back pushes a form value into the HWM chain (the LV1 / LV7 defect).
    * **measured run**: byte-identical to the pre-LV11 behaviour (append the effective NAV,
      persist the tier the math actually produced).
    """
    updated_state = dict(state)
    nav_degraded = dd_info.get("nav_gate_status") == NAV_GATE_STATUS_DEGRADED
    buy_scale = intraday_5m_info.get("intraday_buy_scale", 1.0) if intraday_5m_info else 1.0

    if nav_degraded:
        print(
            "[WARN] (LV11) 门禁净值降级：本次**不**写入 nav_history_10d、**不**覆盖 "
            "current_nav/peak_nav/drawdown_pct，且 circuit_breaker_tier 落 `UNKNOWN`。"
            "Stage 2 将拒绝出票（缺失 ≠ 未触发）。"
        )
        updated_state.update(
            {
                "as_of_date": as_of_date,
                "circuit_breaker_tier": "UNKNOWN",
                "gate_code": gate_code,
                # The intraday hint comes from 5m bars, not from NAV, so it stays; the
                # drawdown-derived scale does not, because there is no measured drawdown.
                "intraday_buy_scale": buy_scale,
                "nav_history_10d": list(baseline_state.get("nav_history_10d", []) or []),
                "prior_state": prior_state,
                "nav_degraded_as_of": as_of_date,
            }
        )
    else:
        # (2026-10-30 incident) Persist the EFFECTIVE NAV the tier math just used
        # (`dd_info["current_nav"]`) rather than the raw operator form value
        # (`args.portfolio_value`), so the series the fast-recovery gate reads back is by
        # construction the same number the circuit breaker acted on. A repeated point is kept
        # (flat NAV is legitimate) but warned about -- see `append_nav_history_point`.
        nav_hist, nav_frozen = append_nav_history_point(
            baseline_state.get("nav_history_10d", []) or [],
            dd_info["current_nav"],
        )
        if nav_frozen:
            print(
                "[WARN] nav_history_10d 出现重复点: 本次 effective NAV "
                f"{float(dd_info['current_nav']):,.2f} 与上一交易日完全相同。"
                "该点已写入（真实盘整亦可能如此），但请确认 Stage 2 表单/账户的 portfolio_value "
                "并非冻结值：冻结值会让 10 日 NAV 恢复门禁看起来像一条平坦序列。"
            )
        updated_state.update(
            {
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
                "intraday_buy_scale": buy_scale,
                "nav_history_10d": nav_hist,
                "prior_state": prior_state,
            }
        )

    # (LV1) NAV-source provenance, persisted in BOTH branches so the archive says WHY: on a
    # degraded run this records `nav_source="form_fallback_refused"`,
    # `nav_gate_status="DEGRADED_NAV_UNAVAILABLE"`, `tier_status="UNKNOWN"` and deviation
    # `None` -- the degradation itself, never a passing verdict.
    updated_state.update(
        {
            "nav_source": dd_info.get("nav_source"),
            "nav_mtm": dd_info.get("nav_mtm"),
            "nav_form": dd_info.get("nav_form"),
            "nav_deviation_pct": dd_info.get("nav_deviation_pct"),
            "nav_deviation_exceeded": dd_info.get("nav_deviation_exceeded"),
            "nav_gate_status": dd_info.get("nav_gate_status"),
            "tier_status": dd_info.get("tier_status"),
        }
    )
    return updated_state


def refresh_stage1_data(
    symbols: Sequence[str],
    as_of_date: str,
    data_provider: str,
    cache_dir: Optional[str],
    market: str = "china",
    enable_intraday_5m: bool = False,
    no_cache: bool = False,
    allow_create_cache: bool = False,
) -> Dict[str, Any]:
    """Pre-flight refresh/patch of market data cache up to as_of_date before Stage 1 starts.

    100% Offline Rule: If no_cache is True or data_provider is in ('synthetic', 'csv'), immediately return
    {"status": "skipped", "reason": "synthetic_or_no_cache"} without touching any network or external DB.

    `allow_create_cache=False` (the default, 2026-10-27) makes a MISSING cache file a REFUSAL:
    this function's job is to REFRESH the cache, not to rebuild it. `DuckDBCache(eff_cache_dir)`
    is a writable open that `makedirs` + `_init_tables`s, so before the gate existed a deleted
    `data/cache.duckdb` was silently recreated (empty) by the very first Stage-1 run -- and then
    Stage 2 traded against it. Refusing returns
    `{"status": "refused", "reason": "cache_missing_needs_allow_create", ...}`; `main()` treats
    that as fatal (it cannot continue without market data) unless `--allow-create-cache` was
    passed, which is the deliberate "yes, I really am rebuilding from scratch" switch.

    `summary["cache_created"]` is True only when this call actually had to create the file
    (`False` when it already existed, and `False` when the run refused -- nothing was created).
    """
    if no_cache or str(data_provider).strip().lower() in ("synthetic", "csv"):
        return {"status": "skipped", "reason": "synthetic_or_no_cache"}

    print(
        f"[STAGE 1 DATA REFRESH] 正在执行前置行情数据刷新 "
        f"(market={market}, as_of={as_of_date}, provider={data_provider})..."
    )
    eff_cache_dir = cache_dir or shared_data_dir()

    # Resolve the FINAL database file the same way `DuckDBCache.__init__` does, so the gate
    # below judges the exact path that is about to be opened (a `--cache-dir` may itself be
    # the `.duckdb` file rather than a directory).
    cache_is_file = eff_cache_dir.endswith(".duckdb") or eff_cache_dir.endswith(".db")
    if os.path.isdir(eff_cache_dir) or not cache_is_file:
        db_file = os.path.join(eff_cache_dir, "cache.duckdb")
    else:
        db_file = eff_cache_dir
    existed = os.path.exists(db_file)

    summary: Dict[str, Any] = {
        "status": "completed",
        "market": market,
        "as_of_date": as_of_date,
        "cache_dir": eff_cache_dir,
        "cache_file": db_file,
        "steps": {},
    }

    if not existed and not allow_create_cache:
        # REFUSED. No `DuckDBCache(...)` call happens on this branch, so the missing file is
        # NOT recreated as a side effect of the refusal itself.
        summary["status"] = "refused"
        summary["reason"] = "cache_missing_needs_allow_create"
        summary["cache_created"] = False
        print(
            f"[STAGE 1 DATA REFRESH REFUSED] 缓存库不存在: {db_file}\n"
            f"  （可能被删除或从未创建。）Stage-1 的职责是刷新缓存，不是重建缓存，因此这里拒绝继续。\n"
            f"  - 确需从零重建缓存库，请显式加 `--allow-create-cache` 后重跑；\n"
            f"  - 否则请先恢复备份（例如 data/cache.backup-*.duckdb → data/cache.duckdb）再运行。\n"
            f"  本次未创建任何文件。"
        )
        return summary

    summary["cache_created"] = not existed

    try:
        from common.duckdb_cache import DuckDBCache
        cache = DuckDBCache(eff_cache_dir)
    except Exception as exc:
        print(f"[STAGE 1 DATA REFRESH WARN] 无法打开 DuckDB 缓存 ({eff_cache_dir}): {exc}")
        summary["status"] = "failed"
        summary["error"] = str(exc)
        return summary

    if market == "china":
        try:
            from scripts.sync_china_5m_and_indices import (
                MAJOR_INDICES,
                sync_indices_daily,
                patch_equity_daily_bars,
                sync_5m_intraday,
            )
        except ImportError as exc:
            print(f"[STAGE 1 DATA REFRESH WARN] 导入行情同步模块失败: {exc}")
            summary["steps"]["import"] = f"failed: {exc}"
            return summary

        equity_syms = [s for s in symbols if str(s).strip().upper() not in CASH_PROXY_SYMBOLS]

        # Per-symbol fetch failures (raised, i.e. NOT "0 new bars"). The sync functions
        # isolate a failing symbol themselves and return `symbol -> "Provider: Type: msg"`;
        # this block carries that forward into `fetch_errors` / `freshness_summary` so the
        # persisted run says WHY a symbol is missing instead of only that it is missing
        # (2026-10-27: a GBK error page on 601288.SH looked exactly like an empty symbol).
        fetch_errors: Dict[str, str] = {}

        def _absorb(produced: Any) -> None:
            if isinstance(produced, dict):
                for sym, reason in produced.items():
                    fetch_errors.setdefault(str(sym), str(reason))

        # 1. Major indices daily bars
        try:
            start_1d = (pd.Timestamp(as_of_date) - timedelta(days=35)).strftime("%Y-%m-%d")
            _absorb(sync_indices_daily(cache, MAJOR_INDICES, start=start_1d, end=as_of_date))
            summary["steps"]["sync_indices_daily"] = "ok"
        except Exception as exc:
            print(f"[STAGE 1 DATA REFRESH WARN] 大盘指数日线同步异常 (容错忽略): {exc}")
            summary["steps"]["sync_indices_daily"] = f"warn: {exc}"

        # 2. Patch equity daily bars
        if equity_syms:
            try:
                _absorb(patch_equity_daily_bars(cache, equity_syms, end=as_of_date))
                summary["steps"]["patch_equity_daily_bars"] = "ok"
            except Exception as exc:
                print(f"[STAGE 1 DATA REFRESH WARN] 个股日线补充拉取异常 (容错忽略): {exc}")
                summary["steps"]["patch_equity_daily_bars"] = f"warn: {exc}"

        # 3. 5m intraday sync if enabled
        if enable_intraday_5m:
            try:
                start_5m = (pd.Timestamp(as_of_date) - timedelta(days=10)).strftime("%Y-%m-%d")
                _absorb(
                    sync_5m_intraday(cache, equity_syms, MAJOR_INDICES, start=start_5m, end=as_of_date)
                )
                summary["steps"]["sync_5m_intraday"] = "ok"
            except Exception as exc:
                print(f"[STAGE 1 DATA REFRESH WARN] 5分钟分时行情同步异常 (容错忽略): {exc}")
                summary["steps"]["sync_5m_intraday"] = f"warn: {exc}"

        if fetch_errors:
            # Same key/shape as `freshness_summary["fetch_errors"]` so the two can be
            # compared directly; also counted in `steps` for a one-glance summary.
            summary["fetch_errors"] = fetch_errors
            summary["steps"]["fetch_errors"] = len(fetch_errors)
            print(
                f"[STAGE 1 DATA REFRESH WARN] {len(fetch_errors)} 个标的抓取报错"
                f"（不是「无数据」）: {', '.join(sorted(fetch_errors))}"
            )

    return summary


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


def append_nav_history_point(
    nav_history: Sequence[float],
    effective_nav: float,
) -> Tuple[List[float], bool]:
    """Append one NAV point to the rolling 10-day recovery series.

    Returns `(new_history, is_frozen_duplicate)`.

    (2026-10-30 incident) Two hardening rules live here, both driven by the same
    real failure: an archived `account_state.json` carried FIVE identical
    `nav_history_10d` values (197,351.17) because the operator form's
    `portfolio_value` was frozen, and the recovery gate then "measured" a flat
    series. An archive repair collapsed it to 4 real entries.

    * The point appended is the caller's EFFECTIVE NAV -- the number the tier
      math actually acted on (`dd_info["current_nav"]`) -- never a raw form
      value that might differ from it.
    * A point identical to the previous one is still appended (chronology must
      not be rewritten / de-duplicated -- a genuinely flat NAV overnight is
      legitimate), but it is REPORTED via the returned flag so the caller can
      warn that the series looks frozen. Silence is what let a frozen form value
      masquerade as a flat NAV history.

    The list is capped at `NAV_10D_RECOVERY_WINDOW` by dropping the OLDEST
    entries, so ordering stays strictly chronological.
    """
    history = [h for h in (nav_history or [])]
    new_nav = float(effective_nav)

    is_frozen_duplicate = False
    if history:
        try:
            prev_nav = float(history[-1])
        except (TypeError, ValueError):
            prev_nav = None
        is_frozen_duplicate = prev_nav is not None and prev_nav == new_nav

    history.append(new_nav)
    if len(history) > NAV_10D_RECOVERY_WINDOW:
        history = history[-NAV_10D_RECOVERY_WINDOW:]
    return history, is_frozen_duplicate


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
    #
    # (2026-10-30 incident) A series shorter than the window is NOT a measured
    # 0.00% return. This legacy expression fell through to `r_10d = 0.0` whenever
    # the archive held fewer than `NAV_10D_RECOVERY_WINDOW` entries, so a run
    # whose history had been repaired down to 4 real points still printed
    # "10-Day NAV Recovery +0.00% / ⚪ None" -- an unmeasured metric dressed up as
    # a measurement, the exact anti-pattern the 5m intraday gate fixed with
    # `SKIPPED`/`UNKNOWN` ("SKIPPED never means PASS").
    #
    # Now the return is `None` + `nav_10d_status = INSUFFICIENT_HISTORY`, and the
    # override stays strictly OFF: an override may only ever be switched ON by a
    # POSITIVE MEASURED recovery, never by missing data. The tier /
    # circuit-breaker / linear-scale math below is untouched -- `r_10d` only ever
    # fed `fast_recovery_nav`.
    nav_hist = state.get("nav_history_10d", []) or []
    nav_hist_len = len(nav_hist)
    base_nav: Optional[float] = None
    if nav_hist_len >= NAV_10D_RECOVERY_WINDOW:
        try:
            base_nav = float(nav_hist[0])
        except (TypeError, ValueError):
            base_nav = None

    if base_nav is not None and base_nav > 0.0:
        r_10d: Optional[float] = current_nav / base_nav - 1.0
        nav_10d_status = NAV_10D_STATUS_OK
        nav_10d_unavailable_reason = None
    else:
        # Short history, or a non-positive / unparseable base (a corrupt series is
        # equally unmeasurable). Either way: no number, and no override.
        r_10d = None
        nav_10d_status = NAV_10D_STATUS_INSUFFICIENT_HISTORY
        if nav_hist_len < NAV_10D_RECOVERY_WINDOW:
            nav_10d_unavailable_reason = (
                f"nav_history_10d 仅有 {nav_hist_len}/{NAV_10D_RECOVERY_WINDOW} 个交易日记录，"
                "不足以测量 10 日 NAV 恢复幅度"
            )
        else:
            nav_10d_unavailable_reason = (
                f"nav_history_10d 起点 NAV 非正数或不可解析 ({nav_hist[0]!r})，10 日恢复幅度不可测"
            )
    # CONSERVATIVE BY CONSTRUCTION: `None` (unmeasured) can never compare > 0.0,
    # so `fast_recovery_nav` is False here. Never flip an override ON from
    # missing data.
    fast_recovery_nav = bool(r_10d is not None and r_10d > 0.0)

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
        # (2026-10-30) Additive schema fields: `OK` means the return above is a
        # real measurement; `INSUFFICIENT_HISTORY` means it is `None` (JSON null)
        # and the fast-recovery override is UNAVAILABLE -- not "not triggered".
        "nav_10d_status": nav_10d_status,
        "nav_10d_history_len": nav_hist_len,
        "nav_10d_required_len": NAV_10D_RECOVERY_WINDOW,
        "nav_10d_unavailable_reason": nav_10d_unavailable_reason,
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


# ---------------------------------------------------------------------------
# DELIVERED SINGLE-STOCK CAP -- MEASURED & DISCLOSED, CLAMPED ONLY ON DEMAND.
#
# (2026-11-04 post-run audit) The 20% single-name ceiling is applied to the
# DELIVERED quantity by Stage 2 (`apply_market_value_cap` in
# `scripts/run_live_strategy.py`): that function ALWAYS measures
# `target_shares * price / portfolio_value` per leg and records the verdict in
# `detail_reason["mv_cap"]` = {market_value_weight, cap, breached, enforced,
# clamped}; it clamps a BUY leg ONLY when `--enforce-mv-cap` is passed AND the
# `LIVETRADING_DISABLE_MV_CAP` kill-switch is unset.
#
# The archived `as_of = 2026-11-04` run delivered 601919.SH at 21.5077% of NAV
# with `breached: true, enforced: false, clamped: false` -- correctly disclosed in
# the ticket -- while THIS report asserted "Enforced across all regimes" and
# "Strictly maintain 20% single-stock ceiling across all assets." Both sentences
# were false in that configuration; two absolute claims cannot co-exist with a
# disclosed, unenforced breach.
#
# The clamp itself stays OFF by design (a registered, user-adjudicated decision:
# `LV3` in docs/development_plan.md §2.21) -- only the WORDING changes. The text is
# DERIVED from the same predicate and cap constant Stage 2 applies
# (`mv_cap_enforcement_enabled`, `DEFAULT_MAX_SINGLE_POSITION`), never restated by
# hand, so switching the Stage-2 flag on flips the report to its enforcing form by
# itself and the prose cannot drift from behaviour again. Nothing here feeds the
# gate: no NAV, breadth, thrust, drawdown, `breached`/`enforced`/`clamped` value is
# computed, read or altered by this block -- it is pure string formatting.
# ---------------------------------------------------------------------------
SINGLE_STOCK_CAP_ENFORCE_FLAG = "--enforce-mv-cap"
SINGLE_STOCK_CAP_KILL_SWITCH_ENV = "LIVETRADING_DISABLE_MV_CAP"


def single_stock_cap_state(enforce_requested: bool = False) -> Dict[str, Any]:
    """Read the delivered-cap enforcement state from the module that APPLIES it.

    SSOT by import: the cap value and the effective `enforced` verdict (flag AND
    kill-switch, resolved by Stage 2's own `mv_cap_enforcement_enabled`) come from
    `scripts/run_live_strategy.py`. Re-spelling either here is exactly how the
    report claimed blanket enforcement while the flag was off.

    `enforce_requested` mirrors Stage 2's `--enforce-mv-cap`; Stage 1 defaults to
    False because it never clamps anything itself (it runs BEFORE any ticket
    exists), so `enforced=True` can only arrive via an explicit opt-in that the
    caller (or the operator, once Stage 1 grows the flag) supplies.
    """
    from scripts import run_live_strategy as _stage2

    cap = float(getattr(_stage2, "DEFAULT_MAX_SINGLE_POSITION", 0.20))
    requested = bool(enforce_requested)
    enforced = bool(_stage2.mv_cap_enforcement_enabled(requested))
    return {
        "cap": cap,
        "enforce_requested": requested,
        "enforced": enforced,
        # Requested but refused => the env kill-switch outranked the flag. Distinguishing
        # this from "flag never passed" keeps the disclosure accurate in both cases.
        "overridden_by_kill_switch": bool(requested and not enforced),
        "enforce_flag": SINGLE_STOCK_CAP_ENFORCE_FLAG,
        "kill_switch_env": SINGLE_STOCK_CAP_KILL_SWITCH_ENV,
    }


def single_stock_cap_status_text(state: Dict[str, Any]) -> str:
    """One-line status cell for the report table, derived from `state` only."""
    cap_pct = f"{float(state.get('cap', 0.20)):.1%}"
    flag = state.get("enforce_flag", SINGLE_STOCK_CAP_ENFORCE_FLAG)
    kill_env = state.get("kill_switch_env", SINGLE_STOCK_CAP_KILL_SWITCH_ENV)
    if state.get("enforced"):
        return f"🔒 Enforced — a BUY leg above {cap_pct} is clamped to whole lots (`{flag}` on)"
    if state.get("overridden_by_kill_switch"):
        return (
            f"⚠️ Measured & disclosed, **NOT clamped** — `{flag}` was requested but "
            f"`{kill_env}` overrides it"
        )
    return (
        f"⚠️ Measured & disclosed, **NOT clamped** — `{flag}` off (kill-switch `{kill_env}`); "
        f"per-leg weight + `breached`/`enforced`/`clamped` land in the Stage-2 ticket "
        f"as `detail_reason.mv_cap`"
    )


def single_stock_cap_action_text(state: Dict[str, Any]) -> str:
    """Operator action for the ceiling, derived from `state` only (no absolutes)."""
    cap_pct = f"{float(state.get('cap', 0.20)):.0%}"
    flag = state.get("enforce_flag", SINGLE_STOCK_CAP_ENFORCE_FLAG)
    kill_env = state.get("kill_switch_env", SINGLE_STOCK_CAP_KILL_SWITCH_ENV)
    if state.get("enforced"):
        return (
            f"Honor the {cap_pct} single-stock ceiling; Stage 2 clamps any BUY leg whose "
            f"delivered market-value weight exceeds it."
        )
    if state.get("overridden_by_kill_switch"):
        return (
            f"{cap_pct} single-stock ceiling is MEASURED but NOT clamped: `{flag}` was requested "
            f"but `{kill_env}` overrides it — keep every position at or below {cap_pct} by hand."
        )
    return (
        f"{cap_pct} single-stock ceiling is MEASURED but NOT clamped (`{flag}` off): keep every "
        f"position at or below {cap_pct} by hand — Stage 2 only flags a breach as `MV_CAP` in "
        f"the ticket."
    )


def resolve_gate_directive(
    dd_info: Dict[str, Any],
    macro_info: Dict[str, Any],
    vol_info: Dict[str, Any],
    intraday_5m_info: Optional[Dict[str, Any]] = None,
    single_stock_cap: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, List[str]]:
    """Synthesize health checks into an actionable trading gate directive."""
    actions = []
    # Derived, not asserted: see the SINGLE_STOCK_CAP_* block above.
    cap_state = single_stock_cap if isinstance(single_stock_cap, dict) else single_stock_cap_state()
    tier = dd_info["tier"]
    fast_rec = dd_info["fast_recovery_nav"] or macro_info["thrust_active"]
    freeze = dd_info["freeze_remaining_bars"]

    if (tier == "TIER_3_HALT" or freeze > 0) and fast_rec:
        code = "CAUTION_RECOVERING"
        headline = "🟡 CAUTION: TIER 3 DOWNGRADED VIA FAST RECOVERY OVERRIDE"
        # L2.42: Synchronize dd_info fields so persisted account_state.json and
        # stage1_health_report.json emit consistent Tier-2 defensive scaling (0.30)
        dd_info["tier"] = "TIER_2_DEFENSIVE"
        dd_info["tier_label"] = "🟡 TIER 2 (Defensive Mode / Fast Recovery Override)"
        dd_info["linear_equity_scale"] = 0.30
        actions.append("Fast Recovery Active: 10d NAV recovery > 0 or Breadth Thrust >= 60%.")
        actions.append("Downgrade from 100% Cash Stop to Tier 2 Defensive Mode (ChanVaaCompound with 70% cash).")
        actions.append("Prioritize structural exits before opening limited defensive positions.")
    elif freeze > 0 or tier == "TIER_3_HALT":
        code = "NO_GO_EMERGENCY"
        headline = "🔴 NO-GO: EMERGENCY HALT & 100% CASH LIQUIDATION"
        actions.append("HALT ALL RISK ASSET PURCHASES: Do NOT place any buy orders today.")
        actions.append(f"Execute 100% liquidation of all risky assets into Cash Proxy ({freeze} freeze days remaining).")
        actions.append("Park proceeds in yield-bearing cash proxy (511880 / GC001 / BIL).")
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
                actions.append(single_stock_cap_action_text(cap_state))
            elif macro_info["breadth_active"]:
                headline = "🟢 GO: STEADY BULL BREADTH REGIME"
                actions.append(f"50-Day Market Breadth Active ({macro_info['breadth']:.1%} >= {macro_info['breadth_thresh']:.0%}).")
                actions.append(f"Target equity exposure dynamically deployed up to {macro_info['target_exposure']:.0%}.")
            else:
                headline = "🟢 GO: CONSERVATIVE / SELECTIVE MARKET REGIME"
                actions.append("Standard unscaled allocation (40%–60% equity, balance in Cash Proxy).")
                actions.append(single_stock_cap_action_text(cap_state))

    # (2026-10-30 incident) An override that could NOT be evaluated must say so
    # out loud, in the same disclosure tone as the 5m `SKIPPED` / `UNKNOWN` lines
    # above. Silence would read as "10-day recovery was measured and did not
    # trigger", which is what the pre-fix 0.00% fallback effectively claimed.
    # Appending an action never changes `code` / `headline`: the tier and
    # circuit-breaker verdicts are unchanged, only their disclosure is.
    if dd_info.get("nav_10d_status") == NAV_10D_STATUS_INSUFFICIENT_HISTORY:
        _nav_unavail_reason = dd_info.get("nav_10d_unavailable_reason") or (
            f"nav_history_10d 仅有 {dd_info.get('nav_10d_history_len', 0)}"
            f"/{NAV_10D_RECOVERY_WINDOW} 个交易日记录，数据不足"
        )
        actions.append(
            "⚠️ Fast-Recovery NAV override UNAVAILABLE — 10 日 NAV 恢复幅度未测得"
            f"（{_nav_unavail_reason}）。该覆盖既未通过也未触发；"
            "缺失数据不会被当作「未触发」，更不会被当作 PASS，也不会放宽任何熔断层级。"
        )

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
    timing: Optional[Dict[str, Any]] = None,
    single_stock_cap: Optional[Dict[str, Any]] = None,
) -> None:
    """Print an executive-level pre-trade terminal dashboard."""
    print("=" * 110)
    # (LV2) The header used to CLAIM the 13:55 – 14:20 inspection window unconditionally --
    # including on the two archived runs that were executed pre-open (02:02 / 05:53
    # market-local). It is now generated from the run's ACTUAL provenance, and the window
    # string appears only when the run really was inside it.
    print("STAGE 1 PRE-TRADE GATE: PORTFOLIO HEALTH & MACRO REGIME INSPECTION")
    if isinstance(timing, dict) and timing.get("header_text"):
        print(f"[EXECUTION TIMING] {timing['header_text']}")
        print(f"[EXECUTION TIMING] {timing.get('overnight_gap_hint', '')}")
    print("=" * 110)
    print(f"Valuation As-Of Date : {as_of_date}")
    strat_disp = f"{strategy_info.get('strategy_name', 'N/A')} ({strategy_info.get('template_name', '')})"
    print(f"Strategy Deployment  : {strat_disp}")
    print(f"Weighting & Ortho    : Mode={_weighting_mode_display(strategy_info)} | Orthogonalization={strategy_info.get('orthogonalization', True)}")
    print(f"Strategy Config      : {strategy_path}")
    print(f"Universe Source      : {universe_path} ({macro_info.get('tracked_symbols_count', 0)} assets)")
    print("-" * 110)

    # 1. Portfolio Health
    print("[1. PORTFOLIO HEALTH & DRAWDOWN CIRCUIT BREAKERS]")
    print(f"  Current Portfolio NAV : {dd_info['current_nav']:>12,.2f}")
    # (LV1) The gate NAV's provenance, right beside the number it produced. On a
    # degraded run the tier line below already reads UNKNOWN; this line says WHY.
    if dd_info.get("nav_source") == NAV_SOURCE_MTM:
        print(
            f"  NAV Source            : mtm ({NAV_BREAKDOWN_EFFECTIVE_KEY})"
            f" | form cross-check {_fmt_metric(dd_info.get('nav_form'), ',.2f')}"
            f" | dev {_fmt_metric(dd_info.get('nav_deviation_pct'), '+.2%')}"
            f"{' ⚠️ ABOVE THRESHOLD' if dd_info.get('nav_deviation_exceeded') else ''}"
        )
    else:
        print(
            "  NAV Source            : ⚠️ DEGRADED — 盯市净值不可用，熔断状态 UNKNOWN"
            f" (status={dd_info.get('nav_breakdown_status')})，缺失 ≠ 未触发"
        )
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
    if dd_info.get("nav_10d_status") == NAV_10D_STATUS_INSUFFICIENT_HISTORY:
        # (2026-10-30) Unmeasured -> say UNMEASURED. Printing "not positive" here
        # would claim a measurement the run never had.
        print(
            f"  ⚠️ FAST-RECOVERY NAV   : UNAVAILABLE — 10 日 NAV 恢复幅度未测得"
            f"（{dd_info.get('nav_10d_history_len', 0)}/{NAV_10D_RECOVERY_WINDOW} 个交易日, 数据不足）"
            "，覆盖保持关闭（未测得 ≠ 未触发）"
        )
    elif dd_info["fast_recovery_nav"]:
        print(f"  🚀 FAST-RECOVERY NAV   : 10-day NAV recovery is positive ({dd_info['nav_10d_return']:>+.2%})")

    # 2. Market Breadth & Thrust
    print("\n[2. UNIVERSE MARKET BREADTH & MOMENTUM THRUST]")
    b_status = "🟢 Bullish" if macro_info["breadth_active"] else "⚪ Neutral/Defensive"
    t_status = "🚀 Thrust Active" if macro_info["thrust_active"] else "⚪ Inactive"
    print(f"  50-Day Market Breadth : {macro_info['breadth']:>6.1%} (Threshold: {macro_info['breadth_thresh']:>5.1%}) | {b_status}")
    print(f"  10-Day Momentum Thrust: {macro_info['thrust']:>6.1%} (Threshold: {macro_info['thrust_thresh']:>5.1%}) | {t_status}")
    # (2026-11-04 audit) The ceiling is named together with its ACTUAL enforcement state
    # (`single_stock_cap_state()` -> Stage 2's predicate), never as a bare absolute.
    _cap_state = single_stock_cap if isinstance(single_stock_cap, dict) else single_stock_cap_state()
    _cap_pct = f"{float(_cap_state.get('cap', 0.20)):.1%}"
    _cap_word = "enforced" if _cap_state.get("enforced") else "measured, NOT clamped"
    print(f"  Target Total Equity   : {macro_info['target_exposure']:>6.1%} (Max Single-Stock Cap: {_cap_pct} — {_cap_word}) [Stage-1 advisory, ignored by Stage-2 orders]")

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
    # (LV2) Only a run genuinely INSIDE the design window may name a clock window. This line
    # used to print "(14:15 – 14:50)" unconditionally -- including on the two archived PRE-OPEN
    # rounds that motivated LV2, i.e. it told an operator to "act between 14:15 and 14:50" on a
    # report produced at 02:02 market-local. Outside the window the actions still apply; only
    # the clock promise is withdrawn (the real timing lives in the report header).
    if timing and timing.get("in_window"):
        print("MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (14:15 – 14:50):")
    else:
        print("MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (窗口外 / 回放运行 — 实际时点见表头 Execution Timing):")
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


def _weighting_mode_display(strategy_info: Dict[str, Any]) -> str:
    """Human-readable weighting mode for the console / Markdown report.

    `stage1_health_report.json` carries three distinct states and the human surfaces must
    not collapse them back into one (owner review 2026-10-24, Step 0):

    * verified mode -> the mode itself;
    * registered pin REJECTED + override -> the dump's real mode, explicitly flagged as
      not the configured one (`weighting_mode_mismatch_overridden`), because printing a
      bare `rolling_ic_ir` next to a successful run reads as "the pin approved this";
    * unregistered + override -> `weighting_mode = None`, rendered as UNVERIFIED rather
      than as a plausible default.

    The `'rolling_ic_ir'` fallback survives only for legacy reports written before the
    `weighting_mode_verified` flag existed.
    """
    mode = strategy_info.get("weighting_mode")
    if strategy_info.get("weighting_mode_mismatch_overridden") is True:
        return f"{mode} [UNVERIFIED: pinned-mode mismatch overridden]"
    if mode is not None:
        return str(mode)
    if strategy_info.get("weighting_mode_verified") is False:
        return "UNVERIFIED (unregistered strategy)"
    return "rolling_ic_ir"


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
    refresh_summary: Optional[Dict[str, Any]] = None,
    timing: Optional[Dict[str, Any]] = None,
    single_stock_cap: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Generate and write structured JSON and Markdown summary reports."""
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, STAGE1_HEALTH_REPORT_JSON)
    md_path = os.path.join(output_dir, "stage1_health_report.md")

    # (LV2) Provenance text for the header. With no resolved clock we say NOTHING about a
    # window: the pre-LV2 code hard-coded "Generated during the market-local 13:55 – 14:20
    # inspection window" and that sentence was false on both archived runs.
    if isinstance(timing, dict) and timing.get("header_text"):
        _timing_header = str(timing["header_text"])
        _timing_gap_hint = str(timing.get("overnight_gap_hint") or "")
        _timing_session = str(timing.get("execution_session") or "")
        _timing_price_ref = str(timing.get("price_reference") or "")
    else:
        _timing_header = "Generated without a resolved market-clock context (no window claim)"
        _timing_gap_hint = ""
        _timing_session = ""
        _timing_price_ref = ""

    report_dict = {
        "as_of_date": as_of_date,
        # (L2.3, design §9) One clock snapshot + one per-symbol freshness verdict, so
        # every report can answer "which instant, which session, which bars?" without
        # cross-referencing the console log.
        "clock": (clock_ctx.stamp_fields() if clock_ctx is not None else None),
        # (LV2) The execution-timing block, so a reader never has to infer "which session do
        # these prices fill in" from the ordinal position of the report in the archive.
        "execution_timing": (dict(timing) if isinstance(timing, dict) else None),
        "freshness_summary": freshness_summary,
        # (2026-10-27) The pre-flight data-refresh outcome, including
        # `cache_created` (True only when THIS run had to create a missing
        # cache.duckdb, i.e. only with --allow-create-cache). Persisted so a
        # human reading stage1_health_report.json can tell "the cache was
        # already there and got refreshed" from "this run built it from
        # nothing", which is otherwise invisible post-hoc.
        "data_refresh": refresh_summary,
        "cache_created": (
            refresh_summary.get("cache_created") if isinstance(refresh_summary, dict) else None
        ),
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

    # (2026-10-30 incident) The 10-day NAV recovery leg gets the same honesty
    # treatment as the 5m intraday gate: an unmeasured return renders as
    # `N/A (n/10 个交易日, 数据不足)` + an explicit "override unavailable" verdict,
    # never as `+0.00%` / "⚪ None" (which reads as a measured flat series).
    _nav10d_insufficient = (
        dd_info.get("nav_10d_status") == NAV_10D_STATUS_INSUFFICIENT_HISTORY
    )
    if _nav10d_insufficient:
        _nav10d_value = (
            f"N/A ({dd_info.get('nav_10d_history_len', 0)}/{NAV_10D_RECOVERY_WINDOW} 个交易日, 数据不足)"
        )
        _nav10d_verdict = "⚪ N/A (override unavailable)"
    else:
        _nav10d_value = _fmt_metric(dd_info.get("nav_10d_return"), "+.2%")
        _nav10d_verdict = "🚀 Active" if dd_info.get("fast_recovery_nav") else "⚪ None"

    _nav10d_disclosure = (
        "> **10-Day NAV Recovery UNAVAILABLE — 未测得（不是「未触发」）。**\n"
        f"> {dd_info.get('nav_10d_unavailable_reason') or 'nav_history_10d 数据不足'}。\n"
        "> 快速恢复覆盖（Fast-Recovery Override）因此保持关闭：未测得数据不会被当作 PASS，"
        "也不会放宽任何熔断层级或缩放系数。\n\n"
        if _nav10d_insufficient
        else ""
    )

    # (LV1) NAV provenance, rendered in the report -- not only logged. See the frozen
    # contract block above `NAV_BREAKDOWN_FILENAME`.
    _nav_threshold_display = _fmt_metric(dd_info.get("nav_deviation_threshold_pct"), ".2%")
    _nav_deviation_display = _fmt_metric(dd_info.get("nav_deviation_pct"), "+.2%")
    _nav_breakdown_status = dd_info.get("nav_breakdown_status")
    if dd_info.get("nav_source") == NAV_SOURCE_MTM:
        _nav_source_value = f"`{dd_info.get('nav_mtm'):,.2f}` (盯市 nav_effective)"
        _nav_form_display = _fmt_metric(dd_info.get("nav_form"), ",.2f")
        if dd_info.get("nav_deviation_exceeded"):
            _nav_source_status = f"⚠️ 偏离超阈值 (> {_nav_threshold_display})"
            _nav_disclosure = (
                "> **NAV SOURCE DEVIATION — 表单值与盯市净值不一致（LV1）。**\n"
                f"> 盯市净值 `nav_effective = {dd_info.get('nav_mtm'):,.2f}`，"
                f"操作员表单值 `--portfolio-value = {_nav_form_display}`，"
                f"偏离 `{_nav_deviation_display}`（冻结阈值 `{_nav_threshold_display}`）。\n"
                "> 回撤 `drawdown_pct`、熔断层级 `tier` 与 `tier1_consecutive_bars` 均按**盯市净值**重算；"
                "表单值仅作输入合理性校验并在此留痕，不参与门禁运算。\n\n"
            )
        else:
            _nav_source_status = f"Form cross-check only (≤ {_nav_threshold_display})"
            _nav_disclosure = ""
    else:
        _nav_source_value = f"N/A — 盯市净值不可用 (`{_nav_breakdown_status}`)"
        _nav_form_display = (
            f"{_fmt_metric(dd_info.get('nav_form'), ',.2f')} (未经验证的表单占位值，不是净值)"
        )
        _nav_source_status = "⚪ UNKNOWN (NAV degraded; 缺失 ≠ 未触发)"
        _nav_disclosure = (
            "> **GATE NAV DEGRADED — 盯市净值不可用（LV1）。**\n"
            f"> {dd_info.get('nav_degradation_reason') or 'nav_breakdown.json 缺少 nav_effective'}。\n"
            "> 因此本次**没有**可判定的回撤 / 熔断层级：`tier_status = UNKNOWN`。"
            "未测得**不等于**「未触发」，不得据此放宽任何层级或缩放系数；"
            "表内数字只是表单值的占位显示，请恢复 `nav_breakdown.json` 后重跑。\n\n"
        )

    strat_ver_display = f"v{strategy_info.get('strategy_version', '1.0.0')}" + (f" [base: v{strategy_info.get('base_version')}]" if strategy_info.get('base_version') else "")
    # (2026-11-04 audit) Derived from Stage 2's own enforcement predicate -- see the
    # SINGLE_STOCK_CAP_* block. This row used to read "Enforced across all regimes"
    # unconditionally, which contradicted the same run's disclosed 21.5077% breach.
    _cap_state = single_stock_cap if isinstance(single_stock_cap, dict) else single_stock_cap_state()
    _cap_display = f"{float(_cap_state.get('cap', 0.20)):.1%}"
    _cap_status_cell = single_stock_cap_status_text(_cap_state)
    md_content = f"""# Stage 1 Live Pre-Trade Health & Macro Audit Report

> **Valuation Date**: `{as_of_date}`  
> **Strategy**: **{strategy_info.get('strategy_name', 'N/A')}** (`{strategy_info.get('template_name', '')}`)  
> **Strategy Version**: `{strat_ver_display}`  
> **Weighting & Ortho**: `{_weighting_mode_display(strategy_info)}` | Orthogonalized: `{strategy_info.get('orthogonalization', True)}`  
> **Gate Directive**: **{gate_headline}**  
> **Report Timestamp**: {_timing_header}  
> **Execution Timing**: `execution_session={_timing_session or 'N/A'}` · `price_reference={_timing_price_ref or 'N/A'}`  
> **Overnight Gap (ATT)**: {_timing_gap_hint or 'n/a'}  

{_nav_disclosure}---

## 1. Portfolio Health & Drawdown Circuit Breakers

| Metric | Current Value | Threshold / Target | Status |
| :--- | :--- | :--- | :--- |
| **Current Portfolio NAV** | `{dd_info['current_nav']:,.2f}` | — | {"Tracking" if dd_info.get('nav_source') == NAV_SOURCE_MTM else "⚠️ UNVERIFIED (表单占位值，不是盯市净值)"} |
| **Gate NAV Source (LV1)** | {_nav_source_value} | Form cross-check `{_nav_form_display}` · dev `{_nav_deviation_display}` (frozen Δ ≤ {_nav_threshold_display}) | {_nav_source_status} |
| **High-Water Mark (HWM)** | `{dd_info['peak_nav']:,.2f}` | — | All-Time Peak |
| **Peak-to-Trough Drawdown** | `{dd_info['drawdown_pct']:.2%}` | Tier 1: -10% / Tier 2: -15% / Tier 3: -20% | **{dd_info['tier_label']}** |
| **Linear Equity Scale** | `{dd_info['linear_equity_scale']:.1%}` | Smooth linear damping | Continuous exposure factor |
| **Tier 1 Cooldown Counter** | `{dd_info['tier1_counter']} / {dd_info['tier1_max']}` | 15 Bars | {"Auto-Heal Imminent" if dd_info['tier1_counter'] >= 10 else "Monitoring"} |
| **Tier 3 Cooldown & Freeze** | `{dd_info['freeze_remaining_bars']} bars left` | 21 Bars lockout | {"Active Lockout" if dd_info['freeze_remaining_bars'] > 0 else "Clear"} |
| **10-Day NAV Recovery** | `{_nav10d_value}` | > 0.0% | {_nav10d_verdict} |

{_nav10d_disclosure}---

## 2. Universe Market Breadth & Momentum Thrust

| Indicator | Calculated | Benchmark Threshold | Regime Assessment |
| :--- | :--- | :--- | :--- |
| **50-Day Market Breadth** | `{macro_info['breadth']:.1%}` | `30.0%` | {"🟢 Bullish Breadth" if macro_info['breadth_active'] else "⚪ Conservative"} |
| **10-Day Momentum Thrust** | `{macro_info['thrust']:.1%}` | `60.0%` | {"🚀 Rapid Rebound Thrust" if macro_info['thrust_active'] else "⚪ Standard"} |
| **Target Equity Exposure** | `{macro_info['target_exposure']:.0%}` | Standard: 50% / Bull: Up to 80% | {"Bullish Expansion" if macro_info['target_exposure'] > 0.50 else "Standard Exposure"} (Stage-1 advisory; Stage-2 model weights govern orders) |
| **Single-Stock Cap** | `{_cap_display}` | Delivered market-value cap (measured per leg in Stage 2) | {_cap_status_cell} |

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


# Filename `generate_reports` writes (and Stage 2 must read back) is owned by
# `STAGE1_HEALTH_REPORT_JSON`, declared with the module constants above.


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
    script accepts (`docs/universe/...`, `data/`, `scripts/results/live/...`).
    """
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(_REPO_ROOT, path))


def build_stage2_command(
    args: argparse.Namespace,
    enable_intraday_5m: bool = False,
    portfolio_value: Optional[float] = None,
) -> List[str]:
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

    (LV12, 2026-11-05) `portfolio_value` is the NAV Stage 2 is launched with. It is
    threaded EXPLICITLY (`gate_nav`, i.e. the marked-to-market `nav_effective`) so the
    child that sizes orders inherits the SAME NAV the parent gated on -- a Stage-1 gate
    verdict computed from MTM while Stage 2 sizes lots against the frozen form value was
    the second half of the LV12 divergence. The default is `args.portfolio_value` so
    every other caller keeps its historical argv byte-for-byte (and `str(...)` of the
    untouched object is preserved verbatim, not re-coerced to float).
    """
    for required in ("output_dir", "universe_file", "strategy_file", "account_state_file"):
        if not getattr(args, required, None):
            raise ValueError(
                f"build_stage2_command: --{required.replace('_', '-')} must be resolved "
                "before Stage 2 can be auto-launched"
            )

    stage2_nav = args.portfolio_value if portfolio_value is None else portfolio_value
    stage2_script = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    output_dir = _absolutize_child_arg(args.output_dir)
    # Same filename `generate_reports` wrote above (see STAGE1_HEALTH_REPORT_JSON).
    health_report_file = os.path.join(output_dir, STAGE1_HEALTH_REPORT_JSON)

    cmd = [
        sys.executable,
        stage2_script,
        "--portfolio-value",
        str(stage2_nav),
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
    if getattr(args, "allow_weighting_mode_override", False):
        cmd.append("--allow-weighting-mode-override")
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
    # (LV2) Resolve the execution-timing block ONCE per run from the same clock context the
    # gate used, so the dashboard, the Markdown header, the JSON report and `summary.json`
    # cannot disagree about when this run happened. Values are frozen (`as_of+1` /
    # `next_open`) per the 2026-11-03 user ruling -- see `common/live_execution_timing.py`.
    timing = resolve_execution_timing(clock_ctx, as_of=as_of_date, run_mode="live")
    print(f"[EXECUTION TIMING] {timing['header_text']}")
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
    template_name = strategy_def.get("template_name", "")
    strategy_key = (
        strategy_def.get("research_strategy_spec", {}).get("strategy_key")
        or template_name
    )
    # (P1.8 / owner decision A follow-up, extended 2026-10-24 Step 0)
    # `verify_live_strategy_dump_preflight` distinguishes THREE outcomes; a bool could not:
    #   * verified           -> real mode, `weighting_mode_verified = true`
    #   * unregistered + override -> nothing was compared; write `weighting_mode = None`
    #   * registered mismatch + override -> the dump's real mode is kept, but the pin
    #     explicitly REJECTED it, so `verified` must be false and the override flagged.
    # Every verified path keeps the previous behaviour byte-identical.
    # (P1.9) The static-template exemption is owned by the verifier (`is_static_template_exempt`)
    # and returns `verified=True` for a genuine static template -- exactly the hardcoded
    # default it replaced -- so the name-only gate that used to wrap this call is gone.
    weighting_mode_verified = True
    weighting_mode_mismatch_overridden = False
    _preflight = verify_live_strategy_dump_preflight(
        strategy_def=strategy_def,
        strategy_key=strategy_key,
        strategy_file=args.strategy_file,
        allow_override=getattr(args, "allow_weighting_mode_override", False),
    )
    weighting_mode_verified = _preflight.verified
    weighting_mode_mismatch_overridden = _preflight.mismatch_overridden

    # (P1.8) Strict resolution for blend strategies: blend_weighting_mode is mandatory
    if not weighting_mode_verified and not weighting_mode_mismatch_overridden:
        # Unregistered + override: record the absence of a verified value, not a guess.
        weighting_mode = None
    elif "blend" in template_name.lower() or "blend" in str(strategy_key).lower():
        if "blend_weighting_mode" not in params and not getattr(args, "allow_weighting_mode_override", False):
            raise LiveDumpPreflightError(
                f"Blend strategy '{strategy_key}' in '{args.strategy_file}' is missing required "
                f"'blend_weighting_mode' in params."
            )
        weighting_mode = params.get("blend_weighting_mode", "rolling_ic_ir")
    else:
        weighting_mode = params.get("blend_weighting_mode", "rolling_ic_ir")

    strategy_info = {
        "strategy_name": strat_name,
        "template_name": template_name,
        "strategy_version": str(strat_ver),
        "base_version": str(base_ver) if base_ver else None,
        "version_info": ver_info,
        "weighting_mode": weighting_mode,
        # Honest provenance: False means "nothing in this run confirmed the mode above
        # against the pinned configuration" -- either no config entry existed, or the pin
        # disagreed and was overridden (see the next field for which).
        "weighting_mode_verified": weighting_mode_verified,
        # True only when a registered pin was REJECTED and the override let it through:
        # `weighting_mode` is then the dump's real value, not the authorised one.
        "weighting_mode_mismatch_overridden": weighting_mode_mismatch_overridden,
        "orthogonalization": params.get("blend_enable_orthogonalization", True),
    }

    enable_intraday_5m = getattr(args, "enable_intraday_5m", False) or bool(
        resolve_blend_param(params, "enable_intraday_5m", False, bool)
    )

    if getattr(args, "refresh_data", True):
        refresh_summary = refresh_stage1_data(
            symbols=universe_symbols,
            as_of_date=as_of_date,
            data_provider=args.data_provider,
            cache_dir=args.cache_dir or shared_data_dir(),
            market=clock_ctx.market,
            enable_intraday_5m=enable_intraday_5m,
            no_cache=getattr(args, "no_cache", False),
            allow_create_cache=getattr(args, "allow_create_cache", False),
        )
    else:
        refresh_summary = {}

    # 1b. (2026-10-27) A REFUSED refresh is FATAL, not a warning. Without the cache the very
    # next step (`load_live_universe` -> `load_universe` -> writable `CachedDataProvider`) would
    # happily CREATE the empty DuckDB file this run just refused to create, i.e. the operator's
    # deliberate deletion would be undone one line later. Exit non-zero with the operator-facing
    # remedy (restore a backup, or re-run with --allow-create-cache).
    if isinstance(refresh_summary, dict) and refresh_summary.get("status") == "refused":
        print(
            f"\n[STAGE 1 ABORT] 缓存库缺失，已拒绝继续: "
            f"{refresh_summary.get('cache_file', '(unknown)')}\n"
            f"  Stage-1 无法在没有行情缓存的情况下计算 breadth/thrust/drawdown，"
            f"且继续执行会重新创建一个空缓存库。\n"
            f"  - 先恢复备份（例如 data/cache.backup-*.duckdb → data/cache.duckdb）后重跑；\n"
            f"  - 确需从零重建缓存库，请加 `--allow-create-cache` 后重跑。"
        )
        sys.exit(2)

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
        # Distinguishes "the fetch raised" from "the symbol genuinely has no bars",
        # using the same `symbol -> "Provider: Type: msg"` mapping the refresh produced.
        fetch_errors=(refresh_summary.get("fetch_errors") if isinstance(refresh_summary, dict) else None),
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

    # (LV1, 2026-11-03) GATE NAV SOURCE. The drawdown / circuit-breaker math below is
    # fed the MARKED-TO-MARKET NAV, not the operator form value. See the frozen
    # contract block above `NAV_BREAKDOWN_FILENAME` for the full rule set; in short:
    # MTM wins, the form value is only an input-sanity cross-check, and a missing MTM
    # is an explicit degradation (never a silent fallback, never "not triggered").
    nav_resolution = resolve_gate_nav_context(args)
    gate_nav = nav_resolution["nav_gate_nav"]
    if gate_nav is None:
        # Degradation branch: the form value is used purely as a DISPLAY PLACEHOLDER so
        # the report keeps its shape. `attach_nav_provenance` relabels the verdict
        # UNKNOWN, so this can never read as a measured NAV.
        gate_nav = nav_resolution["nav_gate_nav_used"]

    dd_info = inspect_portfolio_drawdown(
        current_nav=gate_nav,
        peak_nav=args.peak_nav,
        state=baseline_state,
        params=params,
        as_of_date=as_of_date,
    )
    # Attach the NAV-source provenance to the SAME dict that lands in the report JSON
    # (`"portfolio_health": dd_info`) and in `account_state.json`.
    attach_nav_provenance(dd_info, nav_resolution)

    # 4. Evaluate Universe Breadth, Thrust & Volatility (and optional 5m Intraday Enrichment)
    enable_intraday_5m = getattr(args, "enable_intraday_5m", False) or bool(
        resolve_blend_param(params, "enable_intraday_5m", False, bool)
    )

    # (LV12, 2026-11-05) ONE NAV FOR BOTH STAGES. The 5m intraday trough math below IS a NAV
    # computation, so it must run on the SAME NAV the daily gate ran on -- the marked-to-market
    # `nav_effective` -- and never on the operator form value. Before this, `hwm_intra` /
    # `dd_trough` were the only NAV figures in the whole report still scaled by
    # `--portfolio-value`: on the archived 10-30 / 11-02 pair the form value was frozen at
    # 199935.46 while the book marked 191630.06, so the intraday trough was measured against a
    # NAV that did not exist. `gate_nav` already carries the LV1 degraded fallback (the form
    # placeholder when `nav_effective` is unusable) and that fallback is PRESERVED here on
    # purpose: a degraded run must keep producing today's numbers rather than crash.
    intraday_nav_base = gate_nav

    # The form-vs-MTM difference is RECORDED, never silently absorbed (LV12 deliverable c).
    # Emitted only when both NAVs were measured AND they disagree beyond the frozen LV1
    # tolerance -- so a `form == MTM` run adds no key at all and its 5m block stays
    # byte-identical. A DEGRADED run emits nothing here either: LV1 has already warned, loudly,
    # that the whole gate NAV fell back to the form placeholder; there is no silent difference
    # to disclose in that case, only an admitted absence.
    intraday_nav_base_record: Optional[Dict[str, Any]] = None
    if nav_resolution.get("nav_deviation_exceeded") is True:
        _base_form = nav_resolution.get("nav_form")
        _base_mtm = nav_resolution.get("nav_mtm")
        intraday_nav_base_record = {
            "basis": "mark_to_market",
            # Both raw inputs, the signed delta and the LV1 deviation, so an auditor can
            # recompute `hwm_intra` / `dd_trough` without re-deriving which NAV was used.
            "form_value": _base_form,
            "mtm_value": _base_mtm,
            "delta": float(_base_mtm) - float(_base_form),
            "deviation_pct": nav_resolution.get("nav_deviation_pct"),
            "threshold_pct": nav_resolution.get("nav_deviation_threshold_pct"),
            "base_used": float(intraday_nav_base),
            "note": (
                "5m 日内 HWM/回撤底座 = 盯市 nav_effective；操作员表单值仅留痕，"
                "不参与 intraday 净值计算（LV12）"
            ),
        }
        print(
            "[WARN] (LV12) 5m 日内 NAV 底座与表单值不一致: "
            f"MTM {float(_base_mtm):,.2f} vs 表单 {float(_base_form):,.2f}"
            f"（差 {float(_base_mtm) - float(_base_form):+,.2f}，偏离 "
            f"{nav_resolution.get('nav_deviation_pct'):+.2%}）；"
            "hwm_intra/dd_trough 已按盯市净值计算，表单值仅留痕。"
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
                                        bar_nav += weight * intraday_nav_base
                                        total_weight += weight
                                elif sym == cash_proxy:
                                    if weight > 0:
                                        bar_nav += weight * intraday_nav_base
                                        total_weight += weight

                            if bar_nav > 0:
                                if total_weight > 0 and total_weight < 1.0:
                                    bar_nav += (1.0 - total_weight) * intraday_nav_base
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

    # (LV12) Attach the explicit form-vs-MTM record to the SAME dict that lands in
    # `stage1_health_report.json::intraday_5m_metrics` -- a log line alone would be invisible to
    # every consumer of the report. Omitted entirely when the two NAVs agree (or when the run was
    # degraded), so the `form == MTM` outputs stay byte-identical.
    if intraday_5m_info is not None and intraday_nav_base_record is not None:
        intraday_5m_info["nav_base"] = intraday_nav_base_record

    macro_info = evaluate_market_breadth_and_thrust(universe, params)
    vol_info = evaluate_realized_volatility(universe, params)

    # 5. Synthesize Gate Directive
    # (2026-11-04 audit) The single-stock cap state is resolved ONCE from Stage 2's own
    # enforcement predicate and shared by the actions list, the dashboard and the report
    # table, so all three cannot disagree. It feeds TEXT ONLY: nothing below reads it into
    # a gate, NAV, breadth, thrust, drawdown or `breached`/`enforced`/`clamped` value.
    # `getattr(args, "enforce_mv_cap", False)` mirrors Stage 2's flag name; Stage-1's parser
    # does not define it today, hence the derived default (False = warn-only).
    single_stock_cap = single_stock_cap_state(getattr(args, "enforce_mv_cap", False))
    gate_code, gate_headline, actions = resolve_gate_directive(
        dd_info, macro_info, vol_info, intraday_5m_info, single_stock_cap=single_stock_cap
    )

    # 6. Update Account State
    if not args.no_save_state:
        # (LV11 item 6, 2026-11-03 post-commit audit) Fail closed in the machine-readable
        # layer: the decision itself lives in `build_account_state_update`, which is a pure,
        # directly-tested function precisely because this block used to sit inline here and
        # every offline test bypassed `main()` (see its docstring for the two lies it fixed).
        updated_state = build_account_state_update(
            state,
            baseline_state,
            dd_info,
            as_of_date=as_of_date,
            gate_code=gate_code,
            intraday_5m_info=intraday_5m_info,
            prior_state=prior_state_to_preserve,
        )
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
        timing=timing,
        single_stock_cap=single_stock_cap,
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
        refresh_summary=refresh_summary,
        timing=timing,
        single_stock_cap=single_stock_cap,
    )
    # (L2.3, design §9) `summary.json` is the shared per-run archive key/value store the
    # web layer merges into; the clock + freshness blocks land there too.
    persist_run_metadata(
        args.output_dir,
        clock_ctx,
        freshness_summary=freshness_summary,
        probe_attempts=probe_attempts,
        extra={"date": as_of_date, "stage": "stage1", "gate_code": gate_code, **merge_timing_into_summary(timing)},
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
            # (LV12) ... and the NAV threaded in is the SAME resolved gate NAV (`gate_nav`),
            # i.e. MTM when measurable and the LV1 form placeholder only when it is not.
            cmd = build_stage2_command(
                args, enable_intraday_5m=enable_intraday_5m, portfolio_value=gate_nav
            )
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

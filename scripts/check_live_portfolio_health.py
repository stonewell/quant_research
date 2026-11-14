#!/usr/bin/env python3
"""Stage 1 Operational Live Deployment Runner: Portfolio Health & Macro Regime Inspection.

Evaluates the mandatory pre-trade health gate inside the live run window --
`as_of` close + daily publication grace -> the NEXT session's open, in the MARKET's own
local time (China/HK/US), not the host's clock (see
`common.market_clock.post_close_to_open_window`, the single owner) -- before Stage 2 order
generation:
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
import re
import sys
from datetime import date, datetime, timedelta, timezone
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

from common.advisory_report import ADVISORY_PHASE_P1, build_advisory_block
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
from common.duckdb_cache import format_adjust_factor_freshness_warning
from common.indicators import sma
from common.intraday_features import INTRADAY_5M_PUBLICATION_AFTER_LOCAL
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
    detect_state_version_clobber,
    stamp_account_state_version,
    verify_live_strategy_dump_preflight,
    version_triple_from_strategy_def,
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

#: Which NAV the gate used. `mtm` == measured; `form_fallback_refused` == degraded;
#: `timebase_mismatch_refused` == a measured number that was REFUSED because its equity
#: and cash legs do not share one instant (see the `LV1-TIMEBASE` block below).
NAV_SOURCE_MTM = "mtm"
NAV_SOURCE_FORM_FALLBACK_REFUSED = "form_fallback_refused"
NAV_SOURCE_TIMEBASE_REFUSED = "timebase_mismatch_refused"

#: Gate verdict availability. `DEGRADED_NAV_UNAVAILABLE` means the tier below is an
#: UNEVALUATED placeholder, not a passing verdict.
NAV_GATE_STATUS_EVALUATED = "EVALUATED"
NAV_GATE_STATUS_DEGRADED = "DEGRADED_NAV_UNAVAILABLE"

#: ---------------------------------------------------------------------------
#: (LV1-TIMEBASE, 2026-11-11) `nav_effective` must be ONE instant's NAV.
#:
#: THE DEFECT. `nav_effective` is `equity + cash` as the valuation layer computed it.
#: The two legs are sourced at DIFFERENT times in the live path: the equity leg is
#: marked at the snapshot's `as_of` (today's close), while the cash leg can be the
#: PREVIOUS session's settled balance --
#:
#:   (a) intraday / pre-trade Stage 1: the LV11 pre-staged snapshot
#:       (`execution_service.py::run_health_check` -> `_archived_cash_for` ->
#:       `_resolve_nav(cash=<previous round's summary.json cash>)` ->
#:       `_write_nav_breakdown`) marks TODAY's book but reuses the LAST ROUND's cash
#:       residual, and records that provenance as a `cash_from_archive:` warning
#:       inside `nav_breakdown.json::nav_warnings`.
#:   (b) post-close / next-day recompute: cash == that day's settled
#:       `nav_breakdown.json::cash`, and `equity + cash` is genuinely one instant.
#:
#: ARCHIVE FACT (2026-11-10/202644, 11-11/205137 -- verified read-only): before
#: 2026-11-11 the SAME `nav_breakdown.json` path was written TWICE per round --
#: (a) before the gate child launches, then (b) by Stage 2
#: (`run_live_deploy` -> `_write_nav_breakdown`), which OVERWRITES it ~0.4 s after
#: Stage 1 finished. That double-write is now history: since 2026-11-11 the
#: `nav_breakdown.json` this file reads (`--nav-breakdown-file`) is always the Stage-1
#: gate snapshot, and the Stage-2 valuation goes to
#: `nav_breakdown.stage2.json` instead. The archive therefore keeps (b) -- a
#: perfectly consistent same-instant file -- while `stage1_health_report.json` still
#: records the `nav_mtm` the gate actually used (208,067.60 / 101,093.32). Replaying the
#: archived breakdown alone can never reproduce the defect; the pre-staged payload has to
#: be reconstructed from `equity` + the PREVIOUS session's `summary.json::cash`.
#:
#: Mixing them is a silent, enormous NAV error whenever the previous session traded:
#: 2026-11-10 -> `nav_effective` 208,067.60 = equity 182,471.00 + cash 25,596.60
#: (11-09's cash; the 11-10 same-instant value was 196,852.32) => +5.14% phantom
#: gain; 2026-11-11 -> 101,093.32 = 86,712.00 + 14,381.32 (11-10's cash; true
#: 196,397.11) => -51.59% phantom drawdown => TIER_3_HALT + full liquidation, while
#: the book had moved -0.23%. A reverse-signed cash move masks real risk just as
#: easily, so BOTH directions must be refused, never averaged away.
#:
#: THE RULE. The gate accepts a measured NAV only when its legs are provably one
#: instant. Detectors, all from the snapshot itself (no new I/O):
#:   * `cash_as_of != as_of` (STRUCTURED, written by the valuation layer) => the cash
#:     leg belongs to another session => mismatch. This is the authoritative signal;
#:   * `cash_from_archive:` in `nav_warnings` (older snapshots that predate
#:     `cash_as_of`) => same conclusion, with the archived round's date read from the
#:     provenance text. A round of the SAME session is not a mismatch;
#:   * `|nav_effective - (equity + cash)| > NAV_TIMEBASE_LEGS_TOLERANCE` => the file
#:     contradicts itself => whichever leg is stale, the number is unusable.
#:
#: THE PRODUCER now refuses to publish such a NAV at all: a cross-timebase staged
#: snapshot carries `nav_effective=None` + `cash_as_of` + `nav_degradation_reason`, so the
#: gate degrades on `NAV_EFFECTIVE_MISSING` before any number reaches the breaker. The
#: checks below are the second line of defence -- they cover archives written by older
#: builders and any future writer that reintroduces the mixed sum.
#:
#: On a mismatch the number is REFUSED: `nav_source = "timebase_mismatch_refused"`,
#: `nav_gate_status = "DEGRADED_NAV_UNAVAILABLE"`, `nav_gate_nav = None` => tier
#: UNKNOWN => Stage 2 refuses to issue a ticket. Deliberately NOT a fallback to
#: `nav_form`: that is the frozen operator value LV1 already banned from the gate
#: arithmetic (invalidation condition (ii)), and it is stale in exactly the same
#: direction. Deliberately NOT "repair from the legs" either: when the cash leg is
#: the previous session's settled balance, the same-instant cash is simply NOT IN THE
#: SNAPSHOT, so any repair would be an invention. Refusing is the conservative
#: choice -- it costs a re-staged snapshot, never an unwanted liquidation.
#:
#: `--nav-cash-leg` makes the (a)/(b) distinction explicit for callers that know:
#:   * `auto` (default) -- infer from the snapshot (the two detectors above);
#:   * `same_instant` -- the caller asserts cash and equity are one instant; still
#:     enforces the legs tolerance, but ignores the archive marker;
#:   * `prior_close` -- the caller asserts the cash leg is a settled prior-close
#:     balance (semantic (a)); the mixed NAV can NEVER be a same-instant NAV, so it
#:     is refused outright.
#: ---------------------------------------------------------------------------
NAV_TIMEBASE_STATUS_OK = "OK"
#: Could not be measured (no `equity`/`cash` legs, `cash_source="missing"`, or an
#: explicit NAV override): no claim is made, and the pre-existing behaviour stands.
NAV_TIMEBASE_STATUS_UNVERIFIABLE = "UNVERIFIABLE"
NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND = "CASH_LEG_PRIOR_ROUND"
NAV_TIMEBASE_STATUS_LEGS_MISMATCH = "LEGS_MISMATCH"
#: Fixed, grep-able prefix required by the LV1-TIMEBASE contract.
NAV_TIMEBASE_MISMATCH_PREFIX = "[NAV TIMEBASE MISMATCH]"
#: Money tolerance for `nav_effective == equity + cash` (one currency unit).
NAV_TIMEBASE_LEGS_TOLERANCE = 1.0
#: The producer's marker for "cash came from a PREVIOUS round's summary.json".
NAV_TIMEBASE_CASH_FROM_ARCHIVE_MARKER = "cash_from_archive:"
NAV_CASH_LEG_AUTO = "auto"
NAV_CASH_LEG_SAME_INSTANT = "same_instant"
NAV_CASH_LEG_PRIOR_CLOSE = "prior_close"
NAV_CASH_LEG_CHOICES = (NAV_CASH_LEG_AUTO, NAV_CASH_LEG_SAME_INSTANT, NAV_CASH_LEG_PRIOR_CLOSE)


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
        "--nav-cash-leg",
        choices=list(NAV_CASH_LEG_CHOICES),
        default=NAV_CASH_LEG_AUTO,
        help=(
            "(LV1-TIMEBASE) Which timebase the snapshot's CASH leg belongs to. "
            "`auto` (default): infer from nav_breakdown.json -- a `cash_from_archive:` "
            "provenance line means the cash leg is a PREVIOUS round's settled residual while "
            "the equity leg is marked at this as_of, and the mixed NAV is refused (tier "
            "UNKNOWN). `same_instant`: the caller asserts equity and cash are one instant; "
            "only the equity+cash tolerance is enforced. `prior_close`: the caller asserts "
            "the cash leg is the previous close's settled balance (semantic (a)), so the "
            "mixed NAV can never be a same-instant NAV and is refused outright."
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
        "--intraday-publish-after",
        default=INTRADAY_5M_PUBLICATION_AFTER_LOCAL,
        help=(
            "Market-local wall-clock time after which the as-of session's 5m bars are assumed "
            f"published (default {INTRADAY_5M_PUBLICATION_AFTER_LOCAL}). BaoStock 5m data often only "
            "appears after ~20:00 -- this is a live observation, not a protocol guarantee, and it is "
            "independent of --publication-grace-minutes (the DAILY bar rule): it never moves the run "
            "window, it only decides whether a missing 5m layer is reported as "
            "'pending_publication' (wait) or 'missing' (sync defect)."
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


def _finite_float(value: Any) -> Optional[float]:
    """`float(value)` when it is a finite real number, else `None` (never a guess)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _cash_from_archive_warning(nav_breakdown: Any) -> Optional[str]:
    """The producer's `cash_from_archive:` provenance line, if present (LV1-TIMEBASE).

    `execution_service.py` appends this to `nav_warnings` precisely when the LV11
    pre-staged snapshot reused a PREVIOUS round's `summary.json::cash`. Absent => no such
    claim, and the legs tolerance is the only detector left.
    """
    warnings = nav_breakdown.get("nav_warnings") if isinstance(nav_breakdown, dict) else None
    if not isinstance(warnings, (list, tuple)):
        return None
    for entry in warnings:
        if isinstance(entry, str) and entry.startswith(NAV_TIMEBASE_CASH_FROM_ARCHIVE_MARKER):
            return entry
    return None


def _archive_cash_is_same_session(archive_warning: str, nav_breakdown: Dict[str, Any]) -> bool:
    """Is the archived cash from THIS snapshot's session (=> same instant, no mismatch)?

    "Previous ROUND" is not automatically "previous SESSION": on a same-day re-run the
    pre-staged cash can come from an earlier round of the SAME session (`runs/.../<date>/`),
    where it is the day's settled balance and the legs do share one instant. Refusing that
    would be a false positive, so the archive date is read from the provenance text (the
    producer embeds the `summary.json` path, which is keyed by date -- see
    `execution_service._archived_cash_for`).

    Unparseable provenance is treated as a DIFFERENT session (the conservative reading: the
    warning literally says 上一轮, and `--nav-cash-leg same_instant` is the documented
    operator override).
    """
    as_of = nav_breakdown.get("as_of") if isinstance(nav_breakdown, dict) else None
    if not isinstance(as_of, str) or not as_of:
        return False
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", archive_warning)
    if not dates:
        return False
    # The path is appended last; the LAST date is the archived round's own directory date.
    return dates[-1] >= as_of


def assess_nav_timebase(
    nav_breakdown: Optional[Dict[str, Any]],
    nav_effective: Optional[float],
    cash_leg_basis: str = NAV_CASH_LEG_AUTO,
) -> Dict[str, Any]:
    """PURE check that `nav_effective`'s equity and cash legs share ONE instant.

    See the `LV1-TIMEBASE` block above `NAV_TIMEBASE_STATUS_OK` for the defect this
    closes and for why a mismatch is REFUSED rather than repaired or replaced by the
    operator form value.

    Never raises, never invents a number: every unmeasurable case returns
    `NAV_TIMEBASE_STATUS_UNVERIFIABLE`, which leaves the pre-existing behaviour
    untouched (a snapshot that does not carry legs is not thereby accused).
    """
    basis = cash_leg_basis if cash_leg_basis in NAV_CASH_LEG_CHOICES else NAV_CASH_LEG_AUTO
    payload = nav_breakdown if isinstance(nav_breakdown, dict) else {}
    equity = _finite_float(payload.get("equity"))
    cash = _finite_float(payload.get("cash"))
    cash_source_raw = payload.get("cash_source")
    cash_source = cash_source_raw if isinstance(cash_source_raw, str) else None
    nav_source_raw = payload.get("nav_source")
    nav_source = nav_source_raw if isinstance(nav_source_raw, str) else None
    # `cash_source == "missing"` means the engine had NO resolvable cash and wrote 0.0
    # as an explicit UNDERSTATEMENT; `equity + 0.0` is not a same-instant NAV, so the
    # legs cannot be used as evidence either way.
    legs_nav: Optional[float] = None
    if equity is not None and cash is not None and cash_source != "missing":
        legs_nav = float(equity + cash)
    mtm_value = _finite_float(nav_effective)
    legs_delta: Optional[float] = None
    if legs_nav is not None and mtm_value is not None:
        legs_delta = float(mtm_value - legs_nav)
    archive_warning = _cash_from_archive_warning(payload)
    # (LV1-TIMEBASE) STRUCTURED timebase first: `cash_as_of` is written by the valuation
    # layer itself (`_write_nav_breakdown`), so no date is ever parsed out of a log line.
    cash_as_of_raw = payload.get("cash_as_of")
    cash_as_of = cash_as_of_raw if isinstance(cash_as_of_raw, str) else None
    snapshot_as_of = payload.get("as_of") if isinstance(payload.get("as_of"), str) else None

    status = NAV_TIMEBASE_STATUS_UNVERIFIABLE
    detail: Optional[str] = None
    if nav_source == "override":
        # An explicit override IS the operator's signed assertion; the engine's legs are
        # a reference for the diff line only and are EXPECTED to differ. Refusing here
        # would break the one documented acknowledged-NAV path.
        detail = "nav_source=override：人工覆盖净值不参与腿间校验（记账为 override）"
    elif basis == NAV_CASH_LEG_PRIOR_CLOSE:
        status = NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
        detail = (
            f"调用方显式声明 --nav-cash-leg={NAV_CASH_LEG_PRIOR_CLOSE}：现金腿是上一收盘的"
            "已结算余额，而权益腿是本快照 as_of 的盯市价——两腿不同时点，拒绝用于门禁"
        )
    elif (
        basis == NAV_CASH_LEG_AUTO
        and cash_as_of is not None
        and snapshot_as_of is not None
        and cash_as_of != snapshot_as_of
    ):
        status = NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
        detail = (
            "现金腿时间戳与权益腿不同：cash_as_of="
            f"{cash_as_of} != as_of={snapshot_as_of}（快照自带结构化字段 `cash_as_of`）"
            f"；nav_effective={mtm_value}（equity={equity} + cash={cash}）不可作为门禁净值"
        )
    elif (
        archive_warning is not None
        and basis == NAV_CASH_LEG_AUTO
        and not _archive_cash_is_same_session(archive_warning, payload)
    ):
        status = NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
        detail = (
            "现金腿取自上一轮归档（快照自带 provenance："
            f"`{archive_warning}`），权益腿却是本 as_of 的盯市价 → 两腿不同时点；"
            f"nav_effective={mtm_value}（equity={equity} + cash={cash}）不可作为门禁净值"
        )
    elif legs_nav is None:
        detail = "快照未同时给出可用的 equity / cash 腿（或 cash_source=missing），无法判定时点"
    elif legs_delta is not None and abs(legs_delta) > NAV_TIMEBASE_LEGS_TOLERANCE:
        status = NAV_TIMEBASE_STATUS_LEGS_MISMATCH
        detail = (
            f"快照自相矛盾：nav_effective={mtm_value:,.2f} 与 equity+cash="
            f"{legs_nav:,.2f} 相差 {legs_delta:+,.2f}（容差 ±{NAV_TIMEBASE_LEGS_TOLERANCE:,.2f}）"
            f"→ 至少一条腿的时点/来源与本轮不一致"
            + (f"（equity={equity:,.2f}, cash={cash:,.2f}, cash_source={cash_source}）")
        )
    else:
        status = NAV_TIMEBASE_STATUS_OK
        detail = None

    return {
        "status": status,
        "detail": detail,
        "equity": equity,
        "cash": cash,
        "cash_source": cash_source,
        "cash_as_of": cash_as_of,
        "legs_nav": legs_nav,
        "legs_delta": legs_delta,
        "cash_leg_basis": (
            NAV_CASH_LEG_PRIOR_CLOSE
            if status == NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND
            else NAV_CASH_LEG_SAME_INSTANT
        )
        if status != NAV_TIMEBASE_STATUS_UNVERIFIABLE
        else basis,
    }


def resolve_gate_nav(
    nav_breakdown: Optional[Dict[str, Any]],
    form_nav: Optional[float],
    breakdown_status: str = NAV_BREAKDOWN_STATUS_OK,
    deviation_threshold: float = NAV_DEVIATION_WARN_PCT,
    cash_leg_basis: str = NAV_CASH_LEG_AUTO,
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
      * MTM present but its legs span two instants (LV1-TIMEBASE) ->
        `nav_source = "timebase_mismatch_refused"`, same degraded gate status and
        `None` deviation fields, `nav_mtm = None` (the refused number survives as
        `nav_rejected_nav_effective` for audit only). See `assess_nav_timebase`.

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
                # (LV1-TIMEBASE) The producer's own reason, when it withheld `nav_effective`
                # on purpose (e.g. a cross-timebase staged snapshot), so the archive says
                # WHY instead of only "missing".
                + (
                    f"；生产端声明: {nav_breakdown.get('nav_degradation_reason')}"
                    if isinstance(nav_breakdown, dict)
                    and isinstance(nav_breakdown.get("nav_degradation_reason"), str)
                    else ""
                )
            ),
        }

    # (LV1-TIMEBASE) A measured number is only usable when BOTH legs belong to the same
    # instant. A silent acceptance here is exactly how the 11-10 / 11-11 phantom
    # +5.14% / -51.59% NAVs reached the circuit breaker.
    timebase = assess_nav_timebase(nav_breakdown, mtm, cash_leg_basis)
    if timebase["status"] in (
        NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND,
        NAV_TIMEBASE_STATUS_LEGS_MISMATCH,
    ):
        return {
            "nav_source": NAV_SOURCE_TIMEBASE_REFUSED,
            "nav_mtm": None,
            "nav_form": form_val,
            "nav_deviation_pct": None,
            "nav_deviation_exceeded": None,
            "nav_deviation_threshold_pct": float(deviation_threshold),
            "nav_breakdown_status": status,
            "nav_breakdown_as_of": as_of,
            "nav_gate_status": NAV_GATE_STATUS_DEGRADED,
            "nav_gate_nav": None,
            "nav_degradation_reason": f"{NAV_TIMEBASE_MISMATCH_PREFIX} {timebase['detail']}",
            # Audit trail: which snapshot number was refused, and on what evidence.
            "nav_timebase_status": timebase["status"],
            "nav_timebase_detail": timebase["detail"],
            "nav_cash_leg_basis": timebase["cash_leg_basis"],
            "nav_rejected_nav_effective": mtm,
            "nav_equity": timebase["equity"],
            "nav_cash": timebase["cash"],
            "nav_cash_source": timebase["cash_source"],
            "nav_cash_as_of": timebase["cash_as_of"],
            "nav_legs_nav": timebase["legs_nav"],
            "nav_legs_delta": timebase["legs_delta"],
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
        # Additive provenance: `OK` / `UNVERIFIABLE` are the only values reachable on
        # this path (a mismatch returned above). Kept OUT of `attach_nav_provenance`'s
        # unconditional key list so a clean run's published key set does not change.
        "nav_timebase_status": timebase["status"],
        "nav_timebase_detail": timebase["detail"],
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
        cash_leg_basis=getattr(args, "nav_cash_leg", NAV_CASH_LEG_AUTO),
    )
    _timebase_refused = resolution.get("nav_timebase_status") in (
        NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND,
        NAV_TIMEBASE_STATUS_LEGS_MISMATCH,
    )
    if resolution["nav_gate_nav"] is None:
        # LOUD, not silent. A TIMEBASE refusal is reported by its own (more precise) line
        # below: there the snapshot DID yield an MTM, it was rejected as mixed-instant --
        # saying "取不到盯市净值" about it would be wrong.
        if not _timebase_refused:
            print(
                "[WARN] (LV1) 门禁净值降级: 取不到盯市净值 "
                f"({resolution['nav_degradation_reason']})。"
                "本次回撤/熔断判定为 UNKNOWN，**不是**「未触发」；"
                "请恢复 nav_breakdown.json 后重跑，勿用表单值替代盯市净值。"
            )
        # (LV1-TIMEBASE) A REFUSED number needs its own, grep-able line: "the snapshot
        # exists but its legs span two instants" is a different operational fact from
        # "no snapshot", and only this one is repaired by re-marking the book.
        if _timebase_refused:
            print(
                f"{NAV_TIMEBASE_MISMATCH_PREFIX} (LV1-TIMEBASE) 拒绝用该 nav_effective "
                f"驱动熔断判定: status={resolution.get('nav_timebase_status')}; "
                f"rejected nav_effective={resolution.get('nav_rejected_nav_effective')!r}; "
                f"equity={resolution.get('nav_equity')!r}, cash={resolution.get('nav_cash')!r}, "
                f"cash_source={resolution.get('nav_cash_source')!r}, "
                f"equity+cash={resolution.get('nav_legs_nav')!r}。\n"
                f"  理由: {resolution.get('nav_timebase_detail')}\n"
                "  权益腿与现金腿必须属于同一时点；本次按保守档处理（tier = UNKNOWN，"
                "Stage 2 将拒绝出票）。请用同一时点的持仓 + 已结算现金重新产出 "
                "nav_breakdown.json 后重跑 Stage 1；若确实要按上一收盘已结算现金计价，"
                "显式加 `--nav-cash-leg prior_close` 让口径落档。"
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

    (2026-11-12 defect A) On a degraded run the tier enum is now ALSO written as
    `UNKNOWN`. Previously it kept the placeholder's `"NORMAL"` while
    `account_state.circuit_breaker_tier` became `"UNKNOWN"`, so the two archives
    contradicted each other and Stage 2's top-level `circuit_breaker_tier` read (or the
    placeholder `"NORMAL"`, or nothing) looked like an evaluated, passing breaker.
    `tier_status` / `nav_breaker_evaluated` carry the same verdict, and the human label
    keeps its "UNKNOWN (NAV DEGRADED ...)" wording: missing NAV must never be readable as
    "breaker not triggered". The measured branch is untouched.
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
    # (LV1-TIMEBASE) Persisted ONLY when a mismatch was measured, so a clean run's
    # published key set stays byte-identical (the LV1 key-set contract). A refused
    # number must be auditable from the archive: which snapshot value was thrown out,
    # and the legs that made it unusable.
    if nav_resolution.get("nav_timebase_status") in (
        NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND,
        NAV_TIMEBASE_STATUS_LEGS_MISMATCH,
    ):
        for key in (
            "nav_timebase_status",
            "nav_timebase_detail",
            "nav_cash_leg_basis",
            "nav_rejected_nav_effective",
            "nav_equity",
            "nav_cash",
            "nav_cash_source",
            "nav_cash_as_of",
            "nav_legs_nav",
            "nav_legs_delta",
        ):
            dd_info[key] = nav_resolution.get(key)
    if nav_resolution.get("nav_gate_nav") is None:
        dd_info["tier_status"] = "UNKNOWN"
        # (2026-11-12 defect A) The tier enum must agree with `tier_status` and with
        # `account_state.circuit_breaker_tier` ("UNKNOWN" on a degraded run). Leaving the
        # placeholder "NORMAL" here made "report NORMAL / archive UNKNOWN" simultaneously
        # true, and Stage 2's top-level read saw a passing-looking tier.
        dd_info["tier"] = "UNKNOWN"
        dd_info["tier_label"] = (
            "⚪ UNKNOWN (NAV DEGRADED — 盯市净值不可用，熔断层级未判定；缺失 ≠ 未触发)"
        )
        dd_info["nav_breaker_evaluated"] = False
    else:
        dd_info["tier_status"] = "EVALUATED"
        dd_info["nav_breaker_evaluated"] = True
    return dd_info


def resolve_stage1_run_mode(clock_ctx: Any) -> str:
    """`replay` or `live` for THIS Stage-1 run -- delegated, never re-derived (`L2.30`).

    Stage 1's report writes `execution_timing` (the block a post-hoc audit reads to learn
    "which session do these prices fill in, and was this a real run?"). That call used to
    assert `run_mode="live"`, so a deliberate replay -- `--as-of-date` pinned to a session
    EARLIER than the market clock's latest published one, which
    `common/cli_utils.resolve_live_clock_context` labels
    `window_gate="not_applicable_replay"` -- still published a report claiming to be live.

    The predicate lives in `scripts.run_live_strategy.resolve_run_mode` and is imported
    on purpose: Stage 2 and the netted runner already share that ONE definition, and a second
    copy here could drift into disagreeing with them about the same invocation. Lazy import
    keeps this module's import graph free of the whole Stage-2 runner (the backtester + signal
    stack) while still being the same object at run time.

    Every non-replay shape is `live`: no `--as-of-date` (as-of = the clock's session), an
    explicit `--as-of-date` equal to the clock's session (what the web layer sends), and a
    future as-of (rejected by the clock upstream). A genuine live run's report is therefore
    unchanged bit-for-bit.
    """
    from scripts.run_live_strategy import resolve_run_mode

    return resolve_run_mode(clock_ctx)


def _round_version_triple(strategy_def: Any) -> Dict[str, Any]:
    """This round's `strategy_version` / `base_version` / `strategy_version_info`.

    (2026-11-12 seeding race) Stage 1 deliberately does NOT instantiate the live class
    (`strategy_version_verified` stays False; see `strategy_info`), so the dump it already
    loaded is the only engine version this stage legitimately knows. That is enough here:
    the state this stage writes is TODAY's, and it must at least record THIS round's dump --
    never the triple copied out of yesterday's snapshot. Cross-checking the dump against the
    live class remains Stage 2's fail-closed job, before any ticket exists.
    """
    return version_triple_from_strategy_def(strategy_def)


def build_account_state_update(
    state: Dict[str, Any],
    baseline_state: Dict[str, Any],
    dd_info: Dict[str, Any],
    *,
    as_of_date: str,
    gate_code: str,
    intraday_5m_info: Optional[Dict[str, Any]] = None,
    prior_state: Any = None,
    prior_state_frozen_suspect: Optional[str] = None,
    version_triple: Optional[Dict[str, Any]] = None,
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

    (2026-11-12 seeding race) `version_triple` is the CURRENT round's triple
    (`_round_version_triple(strategy_def)`); when supplied it is stamped onto the returned
    state (top level only -- `prior_state` keeps the snapshot's own fields). Omitting it
    keeps the previous output byte-identical, which is what every direct-call test relies on.

    (LV7, `docs/lv7_prior_state_contract.md` §2) `prior_state_frozen_suspect` is the
    REASON string returned by `detect_frozen_prior_state` (or `None`). A truthy value
    stamps `prior_state_frozen_suspect: true` + `prior_state_frozen_reason` in **both**
    branches below, so a frozen carried state cannot hide behind a green report. A clean
    run emits **neither key** -- never `false` -- so old consumers see no new field at
    all. Annotation only: no NAV number is ever rewritten.
    """
    updated_state = dict(state)
    # Emitted in BOTH branches (degraded + measured): see the docstring. Absent, not False,
    # when clean.
    frozen_annotation: Dict[str, Any] = {}
    if prior_state_frozen_suspect:
        print(
            "[WARN] (LV7) prior_state 疑似冻结: "
            f"{prior_state_frozen_suspect}。已落 `prior_state_frozen_suspect: true`，"
            "本次运行不改写任何 NAV；请核对上游账户状态是否在复制当日净值。"
        )
        frozen_annotation = {
            "prior_state_frozen_suspect": True,
            "prior_state_frozen_reason": str(prior_state_frozen_suspect),
        }
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
                **frozen_annotation,
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
                **frozen_annotation,
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
    # (LV1-TIMEBASE) Only on a measured mismatch: the archive must say which snapshot
    # value was refused and which legs made it unusable. A clean run writes neither key.
    if dd_info.get("nav_timebase_status") in (
        NAV_TIMEBASE_STATUS_CASH_LEG_PRIOR_ROUND,
        NAV_TIMEBASE_STATUS_LEGS_MISMATCH,
    ):
        updated_state["nav_timebase_status"] = dd_info.get("nav_timebase_status")
        updated_state["nav_timebase_detail"] = dd_info.get("nav_timebase_detail")
        updated_state["nav_cash_leg_basis"] = dd_info.get("nav_cash_leg_basis")
        updated_state["nav_rejected_nav_effective"] = dd_info.get("nav_rejected_nav_effective")

    # (2026-11-12 seeding race) THE ROUND'S OWN VERSION TRIPLE, stamped LAST and always.
    #
    # This function builds the state FOR `as_of_date`, and it starts from `dict(state)` --
    # which, on the first run of a session, is the SEED copied out of the previous session's
    # snapshot. Copying it also copied its `strategy_version` (2026-11-12: `1.2.2` while the
    # live class was `1.3.0`), so the round's state was published claiming the OLD engine and
    # Stage 2's parity gate refused the round -- a coin flip decided by whether the polluted
    # file happened to be corrected between two same-day instances.
    #
    # `version_triple` therefore comes from THIS round's strategy dump (`strategy_info`), never
    # from the carried state. `prior_state` keeps its own triple untouched: that block is the
    # snapshot, and its version fields are evidence of which engine wrote it
    # (`docs/lv7_prior_state_contract.md` §5.4). `None` keeps the legacy callers' output
    # byte-identical (every direct-call test passes no triple).
    if version_triple is not None:
        stamp_account_state_version(updated_state, version_triple)
    return updated_state


def resolve_cache_db_file(cache_dir: str) -> str:
    """The exact `.duckdb` file `DuckDBCache(cache_dir)` would open.

    ONE owner for this rule (2026-11-13): `refresh_stage1_data` judged its "refuse to create a
    missing cache" gate on it, and the adjust-factor freshness census below now reads the same
    file, so a `--cache-dir` that is itself a `.duckdb`/`.db` file cannot make the two disagree.
    """
    cache_is_file = cache_dir.endswith(".duckdb") or cache_dir.endswith(".db")
    if os.path.isdir(cache_dir) or not cache_is_file:
        return os.path.join(cache_dir, "cache.duckdb")
    return cache_dir


def inspect_stage1_adjust_factor_freshness(
    *,
    refresh_summary: Optional[Dict[str, Any]],
    cache_dir: str,
) -> Dict[str, Any]:
    """Stage-1 census of "do the adjustment factors reach the bars?" -- OBSERVATION ONLY.

    Deliberately thin: it resolves the file the run just refreshed and delegates to
    `common.duckdb_cache.inspect_adjust_factor_freshness`, which never raises. Nothing here
    feeds `gate_code`, breadth/thrust/vol, target exposure, weights or orders -- the block is
    written to `stage1_health_report.json::adjust_factor_freshness` and printed as one `[WARN]`
    line when it is `lagging`/`missing`. Stage 2 is untouched by design: a stale factor tail is
    currently PRICE-NEUTRAL (the qfq anchor is the latest stored factor, so `qfq == raw` until a
    corporate action lands in the gap), and turning an observation into a gate is a separate,
    explicit decision.

    A skipped/failed refresh contributes no cache file: `refresh_summary["cache_file"]` is used
    when present (it is the path that was actually opened), otherwise the path is resolved from
    `cache_dir` with the same rule the gate used.
    """
    from common.duckdb_cache import inspect_adjust_factor_freshness

    db_file = None
    if isinstance(refresh_summary, dict):
        db_file = refresh_summary.get("cache_file")
    if not db_file:
        db_file = resolve_cache_db_file(cache_dir)
    return inspect_adjust_factor_freshness(db_file)


def summarise_namespace_trace(namespace_trace: Optional[Dict[str, Any]]) -> Optional[str]:
    """One operator line for the LV24 per-namespace patch trace -- or `None` when nothing is due.

    LV24 (2026-11-13): the Stage-1 full-universe refresh printed an AGGREGATE `fetch_errors`
    only, so `fetch_errors={}` / `steps.patch_equity_daily_bars = ok` read exactly like "every
    namespace is current" while a namespace could have been skipped as fresh **by a tolerance**
    (the 2026-11-05 shape: the namespace the valuation read demanded an exact `as_of` bar from).
    `patch_equity_daily_bars` already published the three-state map on the cache for the holdings
    path; this surfaces the same trace for the Stage-1 child.

    OBSERVATION ONLY (`enforcement: observation_only`): the line reports, it never gates. It is
    printed when the trace contains a `failed` namespace OR a namespace that was skipped under
    the non-strict TOLERANCE (i.e. "we did not refresh it, and not because it was current") --
    those are the two facts the old aggregate could not express. A trace where every namespace
    was skipped strictly-fresh or patched prints nothing.

    Returns the line (also printed by the caller) so tests can assert on the text without
    scraping stdout. `None` when there is no trace at all.
    """
    if not isinstance(namespace_trace, dict) or not namespace_trace:
        return None
    states = namespace_trace.get("namespace_trace") or {}
    detail = namespace_trace.get("namespace_detail") or {}
    if not states:
        return None

    def _describe(prov: str) -> str:
        state = states.get(prov)
        reason = (detail.get(prov) or {}).get("reason")
        rule = (detail.get(prov) or {}).get("rule")
        text = f"{prov}={state}"
        if reason:
            text += f"({reason})"
        if rule:
            text += f"[{rule}]"
        return text

    failed = sorted(prov for prov, state in states.items() if state == "failed")
    tolerance_skipped = sorted(
        prov
        for prov, state in states.items()
        if state == "skipped" and (detail.get(prov) or {}).get("reason") == "fresh_tolerance"
    )
    if not failed and not tolerance_skipped:
        return None

    traced = " ".join(_describe(prov) for prov in sorted(states))
    why = []
    if failed:
        why.append(f"{len(failed)} 个命名空间失败（{', '.join(failed)}）")
    if tolerance_skipped:
        why.append(
            f"{len(tolerance_skipped)} 个命名空间仅因容忍度未刷新"
            f"（{', '.join(tolerance_skipped)}）"
        )
    return (
        f"[STAGE 1 NAMESPACE TRACE] {'; '.join(why)} -- {traced}"
        " （逐命名空间留痕，仅观测、不影响选股/权重/下单；"
        "容忍度跳过不等于新鲜，估值可能读到陈旧 bar）"
    )


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

    `summary["equity_daily_patch"]` (LV24, 2026-11-13) is the per-namespace trace of the
    daily-bar patch pass, `{"namespace_trace": {provider -> skipped|patched|failed},
    "namespace_detail": {provider -> {state, reason, symbol[, rule]}}}`. It is OBSERVATION ONLY
    and additive: it exists because `fetch_errors` (frozen shape/meaning, freeze doc
    `docs/lv19_freshness_contract.md` §4.4) can only say "a symbol failed", never "this namespace
    was skipped by a tolerance rather than refreshed". The key is absent when the pass never got
    to evaluate a namespace (skipped/refused refresh, empty universe), never an empty map for
    "nothing to report" -- same convention as `fetch_errors`.
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
    db_file = resolve_cache_db_file(eff_cache_dir)
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
            # LV24: per-namespace trace for THIS refresh, filled in place by the patch pass even
            # when it ends in a refusal (the primitive fills `trace_out` before re-raising).
            namespace_trace: Dict[str, Any] = {}
            try:
                _absorb(
                    patch_equity_daily_bars(
                        cache,
                        equity_syms,
                        end=as_of_date,
                        trace_out=namespace_trace,
                    )
                )
                summary["steps"]["patch_equity_daily_bars"] = "ok"
            except Exception as exc:
                print(f"[STAGE 1 DATA REFRESH WARN] 个股日线补充拉取异常 (容错忽略): {exc}")
                summary["steps"]["patch_equity_daily_bars"] = f"warn: {exc}"
            # LV24 (2026-11-13): persist `{provider -> skipped|patched|failed}` PLUS the reason
            # behind each state under `stage1_health_report.json::data_refresh.equity_daily_patch`.
            # Observation only: nothing below reads it back into the gate, the weights or the
            # orders, and the block is absent when the pass never evaluated a namespace. The
            # `fetch_errors` key beside it keeps its frozen shape/meaning (freeze doc §4.4).
            if namespace_trace:
                summary["equity_daily_patch"] = namespace_trace
                line = summarise_namespace_trace(namespace_trace)
                if line:
                    print(line)

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


def _coerce_nav_float(value: Any) -> Optional[float]:
    """`float(value)` or `None` when unparseable / non-finite / absent.

    Deliberately NARROW: `None`, `""`, `"n/a"`, NaN and infinities all become
    `None` ("we cannot say") instead of a number. A sentinel number would be
    indistinguishable from a real NAV and would silently enter comparisons.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def detect_frozen_prior_state(
    prior_state: Any,
    today_nav: Any,
) -> Optional[str]:
    """Return a human-readable reason when the carried `prior_state` NAV is FROZEN, else `None`.

    (LV7, frozen contract `docs/lv7_prior_state_contract.md` §2)

    Semantics: `prior_state` is supposed to be the PREVIOUS trading day's CONFIRMED
    snapshot. `latest_account_state_at_or_before` already picks the right instance
    (订正 ①), so this function does NOT re-select anything -- it only ANNOTATES. The
    remaining defect is that a stale/frozen carrier is indistinguishable from "nothing
    changed today": the 2026-11-02 archive has `prior_state.as_of_date = 2026-10-29`
    with `prior_state.current_nav == 199935.46 ==` the top-level value, i.e. the dates
    advance while the NAV is copied. Once the upstream form value freezes, the whole
    chain SELF-CONFIRMS.

    Rules (annotation only -- never mutates a NAV, never feeds the tier arithmetic):

    * `prior_state.current_nav == today_nav` by EXACT float equality -> reason string.
      Exact, not `approx`: a frozen value is a bit-for-bit copy of the same decimal
      literal, while a tolerance would flag genuine small moves and turn the marker
      into noise that operators learn to ignore.
    * Anything unmeasurable on either side (`None` / missing / unparseable) -> `None`.
      "Cannot tell" is not "clean", but it is also not a provable freeze; callers must
      pass the MEASURED gate NAV, and a DEGRADED run has none.
    """
    if not isinstance(prior_state, dict):
        return None
    prior_nav = _coerce_nav_float(prior_state.get("current_nav"))
    today = _coerce_nav_float(today_nav)
    if prior_nav is None or today is None or prior_nav != today:
        return None
    prior_as_of = prior_state.get("as_of_date") or "未知"
    return (
        f"prior_state.current_nav 与当日门禁 NAV 完全相同（{today:,.2f}；"
        f"prior_state.as_of_date={prior_as_of}）—— 前推的很可能是当日净值的复制，"
        "而非上一交易日的已确认结算；该标记仅为注释，不改写任何 NAV / 熔断算术"
    )


def count_distinct_nav_points(nav_history: Any) -> int:
    """Count ADJACENTLY DISTINCT, parseable NAV points (a consecutive duplicate counts once).

    (LV7, frozen contract `docs/lv7_prior_state_contract.md` §3)

    The 10-day recovery statistic needs >= 10 DISTINCT observations. Raw length is the
    wrong denominator: the 2026-11-02 archive held FIVE raw points whose last two were
    both 199935.46, and a frozen series would otherwise "reach" the threshold by copying
    itself ten times -- nobody has to pad it, the freeze pads itself.

    Rules:
    * unparseable points are SKIPPED (they neither count nor crash the caller);
    * a point equal to the immediately preceding counted value counts ONCE;
    * non-adjacent repeats (A, B, A) count TWICE -- de-duplication targets consecutive
      copies, not genuine observations.

    Counting only: the series itself is NEVER rewritten here (`append_nav_history_point`
    still keeps flat points and warns, because a genuinely flat NAV overnight is real).
    """
    count = 0
    previous: Optional[float] = None
    for raw in nav_history or []:
        value = _coerce_nav_float(raw)
        if value is None:
            continue
        if count == 0 or value != previous:
            count += 1
        previous = value
    return count


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



def nav10d_display_len(dd_info: Dict[str, Any]) -> int:
    """Count shown in the human-readable `n/10 个交易日` renders.

    (LV7) The DISTINCT count is what the threshold is judged on, so it is also the number
    an operator should read: a 5-record series with a duplicated tail has 4 usable
    sessions, not 5. Falls back to the raw length for hand-built `dd_info` dicts that
    predate the field (JSON still carries both, so audit never depends on rendering).
    """
    distinct = dd_info.get("nav_10d_distinct_len")
    if distinct is not None:
        return int(distinct)
    return int(dd_info.get("nav_10d_history_len", 0) or 0)


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
    # (LV7, `docs/lv7_prior_state_contract.md` §3) The threshold is the count of
    # ADJACENTLY DISTINCT observations, not `len(nav_history_10d)`. Raw length let a
    # frozen series reach "10 points" by copying itself; the 2026-11-02 archive held 5
    # raw points whose last two were both 199935.46 (4 distinct). A consecutive
    # duplicate counts once, and the series itself is never rewritten here.
    nav_hist = state.get("nav_history_10d", []) or []
    nav_hist_len = len(nav_hist)
    nav_10d_distinct_len = count_distinct_nav_points(nav_hist)
    base_nav: Optional[float] = None
    if nav_10d_distinct_len >= NAV_10D_RECOVERY_WINDOW:
        try:
            # Base = the observation exactly `NAV_10D_RECOVERY_WINDOW` sessions back, NOT
            # `nav_hist[0]` (LV7 nit B.4). `append_nav_history_point` caps the list at that
            # window today, so `[0]` happens to be the 10th-back entry -- but if the cap
            # ever grows, `[0]` would silently measure a >10-session return under a 10-day
            # label. The index is safe: `distinct_len >= WINDOW` implies `len >= WINDOW`.
            base_nav = float(nav_hist[-NAV_10D_RECOVERY_WINDOW])
        except (TypeError, ValueError):
            base_nav = None

    if base_nav is not None and base_nav > 0.0:
        r_10d: Optional[float] = current_nav / base_nav - 1.0
        nav_10d_status = NAV_10D_STATUS_OK
        nav_10d_unavailable_reason = None
    else:
        # Short history, a duplicate-heavy history that never actually reached ten
        # distinct sessions, or a non-positive / unparseable base (a corrupt series is
        # equally unmeasurable). Either way: no number, and no override.
        r_10d = None
        nav_10d_status = NAV_10D_STATUS_INSUFFICIENT_HISTORY
        if nav_10d_distinct_len < NAV_10D_RECOVERY_WINDOW:
            if nav_hist_len < NAV_10D_RECOVERY_WINDOW:
                nav_10d_unavailable_reason = (
                    f"nav_history_10d 仅有 {nav_hist_len}/{NAV_10D_RECOVERY_WINDOW} 个交易日记录"
                    f"（去重后仅 {nav_10d_distinct_len} 个不同值），"
                    "不足以测量 10 日 NAV 恢复幅度"
                )
            else:
                nav_10d_unavailable_reason = (
                    f"nav_history_10d 有 {nav_hist_len} 条记录，但去重后仅 "
                    f"{nav_10d_distinct_len}/{NAV_10D_RECOVERY_WINDOW} 个不同交易日"
                    "（相邻重复点不重复计数），不足以测量 10 日 NAV 恢复幅度"
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
        # `history_len` stays the RAW record count (old readers are byte-compatible);
        # `distinct_len` is the count the threshold is actually judged on (LV7).
        "nav_10d_history_len": nav_hist_len,
        "nav_10d_distinct_len": nav_10d_distinct_len,
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


#: Minimum number of daily observations a volatility estimate may be built from.
#: (D7, 2026-11-11) A 21-day realized volatility computed from FEWER than 21 bars is
#: not a 21-day realized volatility. The pre-D7 code accepted `>= 10` bars for the
#: Garman-Klass branch (and silently used whatever `iloc[-21:]` happened to contain),
#: i.e. a short history produced a number that LOOKED like a full-window estimate.
#: Short history is now an explicit degradation (null + reason), never a number.
VOL_MIN_OBS = 21

#: Reason code emitted when no symbol has enough history to estimate volatility.
VOL_DEGRADED_INSUFFICIENT_HISTORY = "insufficient_history"

# ---------------------------------------------------------------------------
# (LV18 LABEL FIX, 2026-11-13) THE FACTOR'S SCOPE IS PART OF THE FACTOR
#
# `LV18` was filed because this script COMPUTED `vol_scaling_factor` and PRESENTED it as
# an applied risk action while nothing downstream consumed it ("presented as an applied
# action, actually inert"). The wiring (`scripts/run_live_strategy.py::apply_portfolio_
# vol_overlay_for_strategy`, cap semantics) fixed the global case, but it is STRATEGY
# GATED: it is consumed as a `min()` CEILING on total equity ONLY for the vol-ceiling
# ALLOWLIST (`PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS`, currently `chan_four_state_blend`).
# For every other strategy the number is INERT, and the pre-fix strings here claimed for
# all of them, unconditionally:
#   * "Barroso scaling scales equity by 0.42x"                    (gate actions)
#   * "Volatility Scaling    :   0.42x"                           (terminal dashboard)
#   * "Barroso Volatility Scaling: `0.42x`"                       (Markdown report)
#   * "Elevated volatility: Rescaling portfolio equity exposure downwards."  (assessment)
# That is the LV18 lie one level down -- per strategy instead of globally.
#
# WHY A NEUTRAL, TWO-PART QUALIFIER AT EVERY SITE (and not a per-strategy branch):
# none of the four render sites knows its strategy's ALLOWLIST MEMBERSHIP.
# `resolve_gate_directive` receives no strategy identity at all; `print_dashboard` /
# `generate_reports` receive `strategy_info`, which carries `strategy_name` /
# `template_name` but NOT the `strategy_key` the allowlist is expressed in, and neither
# receives a resolved membership flag. Branching on `template_name` would be an invented
# lookup that silently rots the day a strategy is added to (or removed from) the Stage-2
# allowlist, and importing the whole Stage-2 runner here to read its constant would put a
# live deployment's Stage 1 behind that module's import graph for a display string.
# So the qualifier is written to be TRUE IN BOTH CASES, like its neighbour
# `[Stage-1 advisory, ignored by Stage-2 orders]` on `target_exposure`:
#   * "applied as a cap only for ... allowlist strategies" -- never claims application for
#     an off-allowlist strategy (the inert case), and
#   * "cap ... no effect on other strategies" -- never implies the factor MULTIPLIES or
#     raises exposure for an allowlisted one (it is a ceiling: `min(model_equity_sum, f)`).
# One owner: every user-visible presentation of the factor interpolates THIS string, and
# `pipeline/tests/test_vol_scaling_labeling.py` statically asserts the renderers keep
# doing so, so a future edit cannot drop the scope silently.
# ---------------------------------------------------------------------------
VOL_CAP_SCOPE_QUALIFIER = (
    "[applied as a cap only for vol-ceiling allowlist strategies "
    "(currently chan_four_state_blend); no effect on other strategies' orders]"
)


def evaluate_realized_volatility(
    universe: Dict[str, pd.DataFrame],
    params: Dict[str, Any],
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute 21-day realized market volatility and Barroso volatility targeting scaling factor.

    (D7, 2026-11-11) TWO DEFECTS FIXED HERE -- both of the "stale risk input / silent
    degradation" family, and both visible in the archived `chan_four_state_blend`
    snapshots for 2026-11-09/10/11, where `realized_vol_21d` was identical to 16
    decimal places (0.3240854096084951) and `vol_scaling_factor` frozen at 0.370273
    while `breadth`/`thrust` changed every day.

    1. STALE INTRADAY WINDOW OVERRIDING A FRESH DAILY ESTIMATE. The 5m downside
       semivariance branch below reads the `RS_Down_5m` column joined onto the DAILY
       frame, then `.dropna()` -> `.iloc[-21:]`. `dropna()` makes the window END AT
       THE LAST NON-NaN DATE, not at `as_of`: when the intraday cache stops on
       2026-11-09, the 11-10 and 11-11 windows silently re-use the SAME 21
       observations ending 11-09 and overwrite the (correctly recomputed, as-of)
       Garman-Klass value. The window endpoint was invisible in the report, so
       "frozen volatility" had no discoverable cause. Now: `as_of` is passed in, the
       intraday window end is MEASURED, and a window that does not reach `as_of` is
       REJECTED (`vol_intraday_rejected_reason`, `vol_intraday_input_stale`) instead
       of being used. The daily branches are truncated to `as_of` defensively too.

    2. INSUFFICIENT HISTORY SILENTLY FABRICATED A NUMBER. With no estimable symbol
       the old code set `vol_21d = target_vol` (i.e. scale = 1.0) while still
       reporting a `vol_model` and `vol_elevated: false` -- indistinguishable from a
       measured calm market. Now: `realized_vol_21d = None`, an explicit
       `vol_degraded`/`vol_degraded_reason_code`/`vol_degraded_reason` triple, and
       `vol_scaling_factor = 1.0` labelled `vol_scaling_fallback:
       "insufficient_history"` so the neutral scaling is never read as a measurement.
       The SAME rule now applies to the 5m branch: it used to accept 5 observations
       while still calling the result "21d" (the archived 0.3240854096084951 was a
       16-session semivariance -- the BaoStock 5m cache starts 2026-10-19), so
       eligibility there also requires VOL_MIN_OBS.

    Every window endpoint actually used is persisted (`vol_window_start` /
    `vol_window_end` / `vol_n_obs` / `vol_input_bars_last_date`), so a stale input is
    visible in the report instead of having to be reverse-engineered from a repeated
    float.
    """
    target_vol = float(resolve_blend_param(params, "target_vol", 0.12, float))
    vol_model = str(resolve_blend_param(params, "vol_model", "garman_klass", str)).lower()
    cash_proxy = params.get("cash_proxy", "BIL")

    as_of_ts: Optional[pd.Timestamp] = None
    if as_of is not None:
        try:
            as_of_ts = pd.Timestamp(as_of).normalize()
        except (TypeError, ValueError):
            as_of_ts = None

    def _truncate(df: pd.DataFrame) -> pd.DataFrame:
        """Point-in-time truncation. The caller already truncates, but the window end
        of THIS function is what the report now certifies, so it is enforced here."""
        if as_of_ts is not None and isinstance(df.index, pd.DatetimeIndex):
            return df.loc[:as_of_ts]
        return df

    vol_estimates: List[float] = []
    gk_count = 0
    #: per-symbol last bar date of the window that actually fed its estimate
    daily_window_ends: List[pd.Timestamp] = []
    daily_window_starts: List[pd.Timestamp] = []
    daily_obs: List[int] = []
    skipped_symbols: List[str] = []

    for sym, df in universe.items():
        if sym == cash_proxy or df.empty or "Close" not in df:
            continue
        df = _truncate(df)
        if df.empty:
            skipped_symbols.append(sym)
            continue

        # Extreme-value Garman-Klass (1980) volatility using OHLC bars
        if vol_model == "garman_klass" and all(c in df.columns for c in ("High", "Low", "Close", "Open")):
            h = df["High"].replace(0, np.nan)
            l = df["Low"].replace(0, np.nan)
            c = df["Close"].replace(0, np.nan)
            o = df["Open"].replace(0, np.nan)
            log_hl = np.log((h / l).clip(lower=1e-6)) ** 2
            log_co = np.log((c / o).clip(lower=1e-6)) ** 2
            gk_var = (0.5 * log_hl - (2.0 * np.log(2.0) - 1.0) * log_co).clip(lower=0.0)
            gk_var_valid = gk_var.dropna()
            if not (h == l).all() and len(gk_var_valid) >= VOL_MIN_OBS:
                window = gk_var_valid.iloc[-VOL_MIN_OBS:]
                gk_vol = float(np.sqrt(window.mean() * 252.0))
                if not np.isnan(gk_vol) and gk_vol > 0:
                    vol_estimates.append(gk_vol)
                    gk_count += 1
                    daily_window_ends.append(pd.Timestamp(window.index[-1]))
                    daily_window_starts.append(pd.Timestamp(window.index[0]))
                    daily_obs.append(int(len(window)))
                    continue

        # Close-to-close fallback (needs VOL_MIN_OBS RETURNS, hence VOL_MIN_OBS+1 prices)
        c = df["Close"].dropna()
        if len(c) >= VOL_MIN_OBS + 1:
            ret = c.pct_change().dropna()
            window = ret.iloc[-VOL_MIN_OBS:]
            cc_vol = float(window.std() * np.sqrt(252))
            if not np.isnan(cc_vol) and cc_vol > 0:
                vol_estimates.append(cc_vol)
                daily_window_ends.append(pd.Timestamp(window.index[-1]))
                daily_window_starts.append(pd.Timestamp(window.index[0]))
                daily_obs.append(int(len(window)))
                continue
        skipped_symbols.append(sym)

    vol_21d: Optional[float] = float(np.median(vol_estimates)) if vol_estimates else None
    input_source = (
        "unavailable"
        if vol_21d is None
        else ("garman_klass_ohlc" if (gk_count > 0 and vol_model == "garman_klass") else "close_to_close")
    )
    active_model = {
        "garman_klass_ohlc": "Garman-Klass (1980)",
        "close_to_close": "Close-to-Close",
        "unavailable": "Unavailable (insufficient history)",
    }[input_source]
    scale = min(1.0, target_vol / vol_21d) if (vol_21d is not None and vol_21d > 0) else 1.0

    # Downside semivariance & jump check
    #
    # (D7 second finding) This branch used to accept `len(rs_down_s) >= 5` while still
    # calling the result a "21-day" volatility: on the archived cache the BaoStock 5m
    # history only starts 2026-10-19, i.e. 16 sessions, so 0.3240854096084951 was a
    # 16-observation semivariance wearing a 21-day label. Eligibility now requires
    # VOL_MIN_OBS, exactly like the daily branch; the observed window end/length is
    # still MEASURED and reported for every symbol that has any 5m feature, so a stale
    # or short 5m cache remains visible even when it cannot be used.
    downside_estimates: List[float] = []
    signed_jumps: List[float] = []
    intraday_window_ends: List[pd.Timestamp] = []
    intraday_window_starts: List[pd.Timestamp] = []
    intraday_obs: List[int] = []
    intraday_obs_all: List[int] = []
    for sym, df in universe.items():
        if sym == cash_proxy or df.empty:
            continue
        df = _truncate(df)
        if df.empty:
            continue
        if "RS_Down_5m" in df.columns:
            rs_down_s = df["RS_Down_5m"].dropna()
            if len(rs_down_s) > 0:
                intraday_window_ends.append(pd.Timestamp(rs_down_s.index[-1]))
                intraday_obs_all.append(int(len(rs_down_s)))
            if len(rs_down_s) >= VOL_MIN_OBS:
                window = rs_down_s.iloc[-VOL_MIN_OBS:]
                down_vol = float(np.sqrt(252.0 * (2.0 * window).mean()))
                if not np.isnan(down_vol) and down_vol > 0:
                    downside_estimates.append(down_vol)
                    intraday_window_starts.append(pd.Timestamp(window.index[0]))
                    intraday_obs.append(int(len(window)))
        if "Signed_Jump_5m" in df.columns:
            sj = df["Signed_Jump_5m"].dropna()
            if len(sj) > 0:
                signed_jumps.append(float(sj.iloc[-1]))

    intraday_used = False
    intraday_rejected_reason: Optional[str] = None
    intraday_window_end: Optional[pd.Timestamp] = max(intraday_window_ends) if intraday_window_ends else None
    intraday_available_obs: Optional[int] = min(intraday_obs_all) if intraday_obs_all else None
    intraday_stale = bool(
        intraday_window_end is not None
        and as_of_ts is not None
        and intraday_window_end.normalize() < as_of_ts
    )
    intraday_eligible = bool(downside_estimates)

    if intraday_window_end is not None and intraday_stale:
        # THE D7 DEFECT, refused. The 5m feature window stops BEFORE `as_of`, so this
        # estimate is not an as-of measurement: it is the previous session's number
        # wearing today's date. Overwriting the fresh daily estimate with it is what
        # froze `realized_vol_21d` across three sessions (11-10/11-11 re-used the
        # 11-09 window). Recorded, never silently applied.
        intraday_rejected_reason = (
            f"5m downside-semivariance window ends {intraday_window_end.date()} < as_of "
            f"{as_of_ts.date()} (intraday feature inputs are stale); rejected so a stale "
            "window cannot override the as-of daily estimate"
        )
    elif intraday_window_end is not None and not intraday_eligible:
        intraday_rejected_reason = (
            f"5m downside semivariance has only {intraday_available_obs} usable sessions "
            f"(< {VOL_MIN_OBS}); a shorter window is not a 21-day estimate"
        )

    if downside_estimates and not intraday_stale:
        downside_vol_21d = float(np.median(downside_estimates))
        med_jump = float(np.median(signed_jumps)) if signed_jumps else 0.0
        if med_jump >= 0.0:
            eff_vol = downside_vol_21d
            active_model = "5m Downside Semivariance (Patton & Sheppard 2015)"
        else:
            eff_vol = max(vol_21d, downside_vol_21d) if vol_21d is not None else downside_vol_21d
            active_model = "Garman-Klass / Downside Vol Max"
        if eff_vol > 0:
            scale = min(1.0, target_vol / eff_vol)
            vol_21d = eff_vol
            intraday_used = True
            input_source = "downside_semivariance_5m"

    # --- Explicit degradation (never a silent `target_vol` substitute) -----------
    vol_degraded = vol_21d is None
    vol_degraded_reason_code: Optional[str] = None
    vol_degraded_reason: Optional[str] = None
    if vol_degraded:
        vol_degraded_reason_code = VOL_DEGRADED_INSUFFICIENT_HISTORY
        vol_degraded_reason = (
            f"no symbol in the universe has >= {VOL_MIN_OBS} usable "
            f"{'Garman-Klass (OHLC)' if vol_model == 'garman_klass' else 'close-to-close'} "
            f"observations up to as_of={as_of_ts.date() if as_of_ts is not None else 'N/A'}; "
            f"realized_vol_21d is NULL (not a measurement) and Barroso scaling falls back "
            f"to the neutral 1.0x"
        )
        scale = 1.0

    # --- Window provenance -------------------------------------------------------
    def _iso(ts: Optional[pd.Timestamp]) -> Optional[str]:
        return None if ts is None else pd.Timestamp(ts).date().isoformat()

    daily_last: Optional[pd.Timestamp] = max(daily_window_ends) if daily_window_ends else None
    as_of_lag_days: Optional[int] = None
    if daily_last is not None and as_of_ts is not None:
        as_of_lag_days = int((as_of_ts - daily_last.normalize()).days)
    intraday_lag_days: Optional[int] = None
    if intraday_window_end is not None and as_of_ts is not None:
        intraday_lag_days = int((as_of_ts - intraday_window_end.normalize()).days)

    # The reported window MUST describe the input that produced the reported number:
    # the 5m downside window when it was used, otherwise the daily window.
    if intraday_used:
        used_start: Optional[pd.Timestamp] = min(intraday_window_starts) if intraday_window_starts else None
        used_end: Optional[pd.Timestamp] = intraday_window_end
        used_obs: Optional[int] = min(intraday_obs) if intraday_obs else None
        used_lag_days = intraday_lag_days
    else:
        used_start = min(daily_window_starts) if daily_window_starts else None
        used_end = daily_last
        used_obs = min(daily_obs) if daily_obs else None
        used_lag_days = as_of_lag_days

    return {
        # --- pre-existing keys (unchanged semantics, except realized_vol_21d may be None)
        "realized_vol_21d": vol_21d,
        "target_vol": target_vol,
        "vol_scaling_factor": scale,
        "vol_elevated": bool(vol_21d is not None and vol_21d > target_vol),
        "vol_model": active_model,
        # --- D7 provenance: WHICH bars, WHICH window, HOW fresh
        "vol_as_of": _iso(as_of_ts),
        "vol_input_source": input_source,
        #: Last bar date of the window that actually fed the reported estimate.
        "vol_input_bars_last_date": _iso(used_end),
        #: Days between as_of and that last input bar (0 == the window reaches today).
        "vol_input_lag_days": used_lag_days,
        #: True when the input window does not reach as_of (should be impossible behind
        #: the freshness gate -- recorded so it can never be assumed).
        "vol_input_stale": bool(
            used_end is not None and as_of_ts is not None and used_end.normalize() < as_of_ts
        ),
        "vol_window_start": _iso(used_start),
        "vol_window_end": _iso(used_end),
        #: Shortest contributing window -- the estimate is only as long as this.
        "vol_n_obs": used_obs,
        #: Daily-branch window, kept beside the reported one so an intraday-sourced
        #: estimate cannot hide the daily bars that are also on hand.
        "vol_daily_bars_last_date": _iso(daily_last),
        "vol_daily_lag_days": as_of_lag_days,
        "vol_symbols_used": len(vol_estimates),
        "vol_symbols_skipped": sorted(skipped_symbols),
        # --- 5m downside-semivariance provenance
        "vol_intraday_used": intraday_used,
        "vol_intraday_window_end": _iso(intraday_window_end),
        "vol_intraday_n_obs": int(min(intraday_obs)) if intraday_obs else None,
        #: Usable sessions actually on hand (21 required for eligibility), reported even
        #: when the branch could not be used, so a short 5m history stays visible.
        "vol_intraday_available_obs": intraday_available_obs,
        "vol_intraday_eligible": intraday_eligible,
        "vol_intraday_lag_days": intraday_lag_days,
        "vol_intraday_input_stale": bool(intraday_stale),
        "vol_intraday_rejected_reason": intraday_rejected_reason,
        # --- explicit degradation
        "vol_degraded": vol_degraded,
        "vol_degraded_reason_code": vol_degraded_reason_code,
        "vol_degraded_reason": vol_degraded_reason,
        #: Only set when a neutral 1.0x stands in for an UNMEASURED factor.
        "vol_scaling_fallback": VOL_DEGRADED_INSUFFICIENT_HISTORY if vol_degraded else None,
    }


# ---------------------------------------------------------------------------
# (D7, 2026-11-11) 5m INTRADAY COVERAGE -- MEASURED, NOT ASSUMED.
#
# `observability.intraday_5m` on the archived 2026-11-10/11-11 rounds reported
# `symbols_measured=0` with reason "5m 日内特征缺失: ... 常见原因: provider does not
# support 5m", next to `enabled=true` and `symbols_5m_available=20`. That reads as
# "the provider cannot do 5m", which is FALSE: a read-only query of
# `data/cache.duckdb::intraday_ohlcv_bars` shows 48 5m rows for each of the 20
# universe symbols on 2026-11-09 (max ts 2026-11-09 15:00, BaoStock), and the
# 2026-11-09 round measured all 20. The 5m cache simply stopped being refreshed
# after 11-09, so on 11-10/11-11 the last daily row's VWAP_Prem_5m is NaN and no
# symbol is "measured" -- a STALE-CACHE degradation, not a capability gap.
# (`load_intraday_5m_universe`'s DuckDB fallback also tries BaoStock first
# regardless of the configured provider, so a `fuyao` run is silently served
# BaoStock rows; that is the upstream cause and is NOT fixed here -- this script
# only stops MISREPORTING it.)
#
# The status fields below make the distinction machine-readable:
#   * `provider_unsupported`          -- zero non-empty 5m frames for the universe;
#   * `degraded_stale_intraday_cache` -- frames exist but their last bar predates as_of;
#   * `degraded_unmeasured_features`  -- frames reach as_of yet no feature was measured;
#   * `evaluated`                     -- at least one symbol carried a measured feature.
# `enabled` keeps the web layer's meaning (real rows on the as-of session) -- it is
# False on a stale cache even though `requested` stays True, so "requested" can never
# be read as "measured".
# ---------------------------------------------------------------------------
INTRADAY_5M_STATUS_EVALUATED = "evaluated"
INTRADAY_5M_STATUS_STALE = "degraded_stale_intraday_cache"
INTRADAY_5M_STATUS_UNSUPPORTED = "provider_unsupported"
INTRADAY_5M_STATUS_UNMEASURED = "degraded_unmeasured_features"
#: (2026-11-12) Same bars-missing fact as STALE/UNMEASURED, but the CAUSE is measured: the
#: clock has not reached the 5m publication time yet (`--intraday-publish-after`, 20:00 for
#: BaoStock). Distinct on purpose -- "wait" and "fix the sync" need different operator
#: actions, and auditing a run must be able to tell them apart months later.
INTRADAY_5M_STATUS_PENDING = "pending_publication"

INTRADAY_5M_CODE_STALE = "intraday_5m_stale_window"
INTRADAY_5M_CODE_UNAVAILABLE = "intraday_5m_unavailable"
INTRADAY_5M_CODE_UNMEASURED = "intraday_5m_features_unmeasured"
#: Mirrors `common.intraday_features.INTRADAY_5M_PUBLICATION_CODE_PENDING` (one spelling is
#: asserted equal in the offline tests; kept local so this script keeps its lazy imports).
INTRADAY_5M_CODE_PENDING = "intraday_5m_pending_publication"

#: (D7 follow-up, 2026-11-11) Injectable wall clock for the self-report stamp.
#: `None` -> real UTC now (production behaviour unchanged); tests/replays pin it so two
#: Stage-1 runs of the SAME inputs produce a BYTE-IDENTICAL `intraday_5m_metrics` block
#: (the `generated_at` stamp used to leak 0.5s of wall-clock drift into
#: `test_nav_source_parity.py::test_equal_navs_leave_the_five_minute_block_bit_identical`).
INTRADAY_5M_NOW_OVERRIDE: Optional[datetime] = None


def _intraday_5m_now(now: Optional[datetime] = None) -> datetime:
    """The instant stamped into `intraday_5m_metrics.generated_at`.

    Precedence: explicit `now` argument > module override > real UTC now. The module
    override exists so a test can freeze the clock for a whole `main()` invocation
    without threading a parameter through the CLI.
    """
    if now is not None:
        return now
    if INTRADAY_5M_NOW_OVERRIDE is not None:
        return INTRADAY_5M_NOW_OVERRIDE
    return datetime.now(timezone.utc)


def summarize_intraday_5m_provenance(
    universe_5m: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate the loader's per-frame provenance attrs (`common.intraday_features`).

    Every served frame carries `intraday_5m_provider` / `intraday_5m_source` /
    `intraday_5m_latest_ts`, and a frame that had to fall back additionally carries
    `fallback_from` / `fallback_to` / `fallback_reason`. The report must say WHICH
    provider actually served the bars and WHICH fallback happened -- otherwise a
    `fuyao` run served by BaoStock's frozen cache is indistinguishable from a fuyao
    delivery, which is exactly how the 2026-11-09..11 freeze stayed invisible.

    Aggregation rule: a field is a scalar when every contributing frame agrees, and a
    sorted list of the distinct values when they disagree (nothing is hidden by a
    "first symbol wins" pick). `provider`/`source` are the majority value -- ties broken
    by sorted order -- because "which provider dominates this universe" is the useful
    scalar, with `provider_by_symbol` kept for the full picture.
    """
    providers: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    fallbacks: Dict[str, Dict[str, Any]] = {}
    latest: Optional[str] = None
    latest_ts_obj: Optional[pd.Timestamp] = None

    for sym, frame in (universe_5m or {}).items():
        name = str(sym)
        attrs = getattr(frame, "attrs", {}) or {}
        prov = attrs.get("intraday_5m_provider")
        src = attrs.get("intraday_5m_source")
        if prov:
            providers[name] = str(prov)
        if src:
            sources[name] = str(src)
        if attrs.get("fallback_to") is not None:
            fallbacks[name] = {
                "from": attrs.get("fallback_from"),
                "to": attrs.get("fallback_to"),
                "reason": attrs.get("fallback_reason"),
                "detail": attrs.get("fallback_reason_detail"),
            }
        ts_raw = attrs.get("intraday_5m_latest_ts")
        if ts_raw:
            try:
                ts = pd.Timestamp(ts_raw)
            except (TypeError, ValueError):
                continue
            if latest_ts_obj is None or ts > latest_ts_obj:
                latest_ts_obj, latest = ts, ts.isoformat()

    def _majority(values: Dict[str, str]) -> Optional[str]:
        if not values:
            return None
        counts: Dict[str, int] = {}
        for value in values.values():
            counts[value] = counts.get(value, 0) + 1
        best = max(counts.values())
        return sorted(v for v, c in counts.items() if c == best)[0]

    def _collapse(key: str) -> Any:
        vals = sorted({str(fb[key]) for fb in fallbacks.values() if fb.get(key) is not None})
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else vals

    return {
        "provider": _majority(providers),
        "source": _majority(sources),
        "latest_ts": latest,
        "provider_by_symbol": dict(sorted(providers.items())),
        "n_symbols_with_provenance": len(providers),
        # Fallback chain: null unless at least one symbol had to switch provider.
        "fallback_from": _collapse("from"),
        "fallback_to": _collapse("to"),
        "fallback_reason": _collapse("reason"),
        "n_fallback_symbols": len(fallbacks),
        "fallback_by_symbol": {k: v for k, v in sorted(fallbacks.items())},
    }


def probe_intraday_5m_cache_coverage(
    universe_5m: Optional[Dict[str, Any]],
    as_of_date: Optional[str],
) -> Dict[str, Any]:
    """Measure what the 5m layer ACTUALLY returned for `as_of_date`.

    Counts 5m ROWS on the as-of session (never symbols, never daily bars -- the
    2026-10-22 defect) and reports the last bar date of the whole 5m universe, so a
    stale cache is visible instead of being blamed on provider capability.
    """
    session: Optional[pd.Timestamp] = None
    if as_of_date:
        try:
            session = pd.Timestamp(as_of_date).normalize()
        except (TypeError, ValueError):
            session = None

    rows_5m = 0
    with_rows: List[str] = []
    unavailable: List[str] = []
    last_bar: Optional[pd.Timestamp] = None

    for sym, df in (universe_5m or {}).items():
        name = str(sym)
        if df is None or bool(getattr(df, "empty", True)):
            unavailable.append(name)
            continue
        idx = pd.DatetimeIndex(df.index)
        if len(idx) > 0:
            sym_last = pd.Timestamp(idx.max())
            if last_bar is None or sym_last > last_bar:
                last_bar = sym_last
        n_on_session = int((idx.normalize() == session).sum()) if session is not None else int(len(idx))
        rows_5m += n_on_session
        (with_rows if n_on_session > 0 else unavailable).append(name)

    lag_days: Optional[int] = None
    if last_bar is not None and session is not None:
        lag_days = int((session - last_bar.normalize()).days)

    return {
        "rows_5m": int(rows_5m),
        "as_of_date": as_of_date,
        "symbols_with_rows": sorted(with_rows),
        "symbols_unavailable": sorted(unavailable),
        "intraday_data_last_bar_date": None if last_bar is None else last_bar.date().isoformat(),
        "intraday_lag_calendar_days": lag_days,
        "intraday_data_stale": bool(lag_days is not None and lag_days > 0),
    }


def classify_intraday_5m_status(
    coverage: Dict[str, Any],
    *,
    n_5m_symbols_nonempty: int,
    features_measured: int,
    requested: bool = True,
    freshness: Optional[Dict[str, Any]] = None,
    publication: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Derive `status` / `degraded` / `degraded_reason_code` from MEASURED facts.

    `freshness` is the output of `common.intraday_features.assess_intraday_5m_freshness`
    (worst-symbol aggregation). When supplied it is authoritative: a cache that holds the
    as-of session but not its closing bar is NOT a frozen cache, so `status` must not claim
    one. Only when it is omitted does the coarser max-bar-date heuristic of `coverage` apply
    (kept for callers/tests that only measured coverage).

    `publication` is the output of
    `common.intraday_features.assess_intraday_5m_publication_state` -- the SAME staleness
    fact plus the "has the 5m publication hour arrived yet" axis. When it says
    `pending_publication` and no feature was measured, the cause is known
    (`intraday_5m_pending_publication`) and it OUTRANKS the generic
    `degraded_unmeasured_features` label: the operator action is "rerun after 20:00", not
    "debug the feature path". It never upgrades a degraded state to `evaluated`.

    The emitted `degraded_reason_code` never uses `provider_unsupported_5m`: that token is a
    PROVIDER-LEVEL fact owned by the loader's `fallback_reason` (e.g. "fuyao has no 5m rows
    for 601872.SH"), not a layer-level diagnosis. The layer-level codes are:
    `intraday_5m_unavailable` (zero 5m frames at all), `intraday_5m_stale_window` (frames
    exist but end on an earlier session), `intraday_5m_features_unmeasured` (frames reach
    the session yet no feature was measured), `intraday_5m_pending_publication` (bars short
    of the session AND the publication hour has not arrived).
    """
    rows_5m = int(coverage.get("rows_5m") or 0)
    try:  # lazy: keep this script's module-level imports unchanged
        from common.intraday_features import (
            INTRADAY_5M_FRESHNESS_OK,
            INTRADAY_5M_FRESHNESS_STALE,
        )
    except ImportError:  # pragma: no cover - only if the module is unavailable
        INTRADAY_5M_FRESHNESS_OK, INTRADAY_5M_FRESHNESS_STALE = "ok", "stale_last_bar"

    if freshness is not None:
        stale = bool(freshness.get("stale"))
        lag = freshness.get("lag_calendar_days")
        freshness_reason = freshness.get("reason")
    else:
        stale = bool(coverage.get("intraday_data_stale"))
        lag = coverage.get("intraday_lag_calendar_days")
        freshness_reason = INTRADAY_5M_FRESHNESS_STALE if stale else INTRADAY_5M_FRESHNESS_OK

    # `lag >= 1` == the newest bar belongs to an EARLIER session (a frozen cache). A lag of
    # 0 with `stale=True` only means "the session's closing bar is not in yet", which is
    # normal for an intraday run and is reported through `freshness_reason`, not as a freeze.
    frozen_on_earlier_session = bool(stale and lag is not None and int(lag) >= 1)
    has_any_frame = int(n_5m_symbols_nonempty) > 0
    has_any_bar = coverage.get("intraday_data_last_bar_date") is not None or has_any_frame

    if int(features_measured) > 0:
        status, code = INTRADAY_5M_STATUS_EVALUATED, None
    elif not has_any_frame and not has_any_bar:
        status, code = INTRADAY_5M_STATUS_UNSUPPORTED, INTRADAY_5M_CODE_UNAVAILABLE
    elif frozen_on_earlier_session:
        status, code = INTRADAY_5M_STATUS_STALE, INTRADAY_5M_CODE_STALE
    else:
        status, code = INTRADAY_5M_STATUS_UNMEASURED, INTRADAY_5M_CODE_UNMEASURED

    # Known cause outranks the generic label, but only while nothing was measured: a
    # measured feature set is `evaluated` no matter what the publication clock says.
    if publication is not None and int(features_measured) <= 0:
        if publication.get("status") == INTRADAY_5M_STATUS_PENDING:
            status = INTRADAY_5M_STATUS_PENDING
            code = publication.get("degraded_reason_code") or INTRADAY_5M_CODE_PENDING

    degraded = status != INTRADAY_5M_STATUS_EVALUATED
    result = {
        "requested": bool(requested),
        # Web-layer contract: `enabled` == REAL rows on the as-of session.
        "enabled": bool(requested) and rows_5m > 0,
        "status": status,
        "degraded": degraded,
        "degraded_reason_code": code,
        "freshness_reason": freshness_reason,
        # ISO-8601 UTC, so the web layer's freshness check can consume this block.
        "generated_at": _intraday_5m_now(now).isoformat(),
    }
    if publication is not None:
        # Only the publication-axis facts; the freshness fields are merged once, by the
        # caller, straight from the owner (`assess_intraday_5m_freshness`).
        result.update(
            {
                "expected_after": publication.get("expected_after"),
                "expected_publication_ts": publication.get("expected_publication_ts"),
                "publication_state": publication.get("status"),
            }
        )
    return result


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
            # (2026-11-12) The cause is KNOWN and temporary (BaoStock 5m publishes ~20:00).
            # Disclosed as an operator action, never as a refusal: the DAILY-bar decision is
            # complete at close+grace, so a book that does not need the 5m gate is unaffected;
            # only a strategy that DOES need it should rerun later. Tickets are still emitted.
            if intraday_5m_info.get("status") == INTRADAY_5M_STATUS_PENDING:
                actions.append(
                    "⏳ 5m 当日数据尚未发布（BaoStock 通常 "
                    f"{intraday_5m_info.get('expected_after') or INTRADAY_5M_PUBLICATION_AFTER_LOCAL} "
                    "后可用）：依赖 5m 日内门禁（如 intraday_buy_scale / 日内分布门禁）的策略，"
                    "建议 20:00 后重跑本阶段；本次仅按日线口径出票，未使用 5m 特征。"
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
            f"nav_history_10d 仅有 {nav10d_display_len(dd_info)}"
            f"/{NAV_10D_RECOVERY_WINDOW} 个交易日记录，数据不足"
        )
        actions.append(
            "⚠️ Fast-Recovery NAV override UNAVAILABLE — 10 日 NAV 恢复幅度未测得"
            f"（{_nav_unavail_reason}）。该覆盖既未通过也未触发；"
            "缺失数据不会被当作「未触发」，更不会被当作 PASS，也不会放宽任何熔断层级。"
        )

    if vol_info["vol_elevated"]:
        # (LV18 label fix, 2026-11-13) "scales equity by X" asserted an APPLIED rescale for
        # whichever strategy this gate serves; the factor is a Stage-2 CEILING consumed only
        # by the vol-ceiling allowlist, so the scope travels with the number.
        actions.append(
            f"Volatility Alert: 21d realized vol ({vol_info['realized_vol_21d']:.1%}) > target ({vol_info['target_vol']:.1%}); "
            f"Barroso factor {vol_info['vol_scaling_factor']:.2f}x is a CEILING on total equity "
            f"(never a multiplier, and it can only LOWER exposure) {VOL_CAP_SCOPE_QUALIFIER}."
        )
    # (D7) An UNMEASURED volatility factor must say so out loud: `vol_scaling_factor: 1.0`
    # is the neutral stand-in, not evidence of a calm market.
    if vol_info.get("vol_degraded"):
        actions.append(
            "⚠️ Volatility Targeting UNAVAILABLE — 21 日实现波动率未测得"
            f"（reason_code={vol_info.get('vol_degraded_reason_code')}: "
            f"{vol_info.get('vol_degraded_reason', 'insufficient history')}）。"
            "Barroso 缩放回退为中性 1.00x（不是「波动率低所以不缩放」），"
            "该因子既未通过也未触发。"
        )
    elif vol_info.get("vol_intraday_rejected_reason"):
        actions.append(
            "⚠️ 5m 下行半方差窗口未采用 — "
            f"{vol_info.get('vol_intraday_rejected_reason')}；"
            f"本次 realized_vol_21d 由 {vol_info.get('vol_input_source')} 按 as_of "
            f"{vol_info.get('vol_as_of')} 计算"
            f"（窗口 {vol_info.get('vol_window_start')} → {vol_info.get('vol_window_end')}，"
            f"n_obs={vol_info.get('vol_n_obs')}）。"
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
            f"（{nav10d_display_len(dd_info)}/{NAV_10D_RECOVERY_WINDOW} 个交易日, 数据不足）"
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
    # (LV18 label fix, 2026-11-13) "⚠️ Scaled Down" claimed an APPLIED rescale for every
    # strategy, but this line reports a MEASUREMENT (realized vol above target); whether
    # anything downstream is capped is stated, with its scope, on the factor line below.
    v_status = "⚠️ Vol Above Target" if vol_info["vol_elevated"] else "🟢 Stable"
    if vol_info.get("vol_degraded"):
        v_status = "⚠️ UNAVAILABLE (neutral 1.00x fallback)"
    vol_mod_lbl = f", Model: {vol_info.get('vol_model', 'N/A')}" if "vol_model" in vol_info else ""
    print(f"  21-Day Realized Vol   : {_fmt_metric(vol_info['realized_vol_21d'], '>6.1%')} (Target Vol: {vol_info['target_vol']:>5.1%}{vol_mod_lbl}) | {v_status}")
    # (LV18 label fix) The value alone read as an applied rescale for EVERY strategy; the
    # factor is a Stage-2 ceiling on the allowlist only, so the scope is printed with it.
    print(f"  Volatility Scaling    : {vol_info['vol_scaling_factor']:>6.2f}x {VOL_CAP_SCOPE_QUALIFIER}")
    # (D7) Window provenance: a repeated float is diagnosable only if the input window
    # is on screen. Printed for every run, stale or not.
    print(
        f"  Vol Input Window      : {(vol_info.get('vol_window_start') or 'N/A')} → "
        f"{(vol_info.get('vol_window_end') or 'N/A')} (n_obs={_fmt_metric(vol_info.get('vol_n_obs'), 'd')}, "
        f"last_bar={vol_info.get('vol_input_bars_last_date') or 'N/A'}, "
        f"lag={_fmt_metric(vol_info.get('vol_input_lag_days'), 'd')}d, "
        f"source={vol_info.get('vol_input_source')})"
    )
    if vol_info.get("vol_intraday_input_stale"):
        print(
            "  ⚠️ 5m Downside Window  : STALE — ends "
            f"{vol_info.get('vol_intraday_window_end')} (lag {vol_info.get('vol_intraday_lag_days')}d), "
            "REJECTED (not used to override the as-of daily estimate)"
        )
    elif vol_info.get("vol_intraday_rejected_reason"):
        print(
            "  ⚠️ 5m Downside Window  : NOT USED — "
            f"{vol_info.get('vol_intraday_rejected_reason')}"
            f" (available sessions={vol_info.get('vol_intraday_available_obs')})"
        )

    if intraday_5m_info and (intraday_5m_info.get("requested") or intraday_5m_info.get("enabled")):
        print("\n[4. 5-MINUTE INTRADAY MICROSTRUCTURE & MARK-TO-MARKET (截断时点见下)]")
        # (2026-10-22 incident, Fix 1) Render UNMEASURED metrics as N/A. The old
        # dashboard printed fabricate-looking numbers (breadth 100.0%) computed
        # from NaN-substituted constants on a run whose 5m frame was empty.
        if intraday_5m_info.get("degraded"):
            # (D7) `requested=true` must never be read as "measured". Name the status, the
            # measured cause (stale cache vs. no 5m rows) and the provider/fallback chain.
            _fb_reason = intraday_5m_info.get("fallback_reason")
            _fb_from = intraday_5m_info.get("fallback_from")
            _fb_to = intraday_5m_info.get("fallback_to")
            _fb_chain = (
                f"{_fb_from}→{_fb_to}" + (f" ({_fb_reason})" if _fb_reason else "")
                if (_fb_from or _fb_to)
                else "none"
            )
            print(
                f"  ⚠️ Status             : DEGRADED — status={intraday_5m_info.get('status')} "
                f"(requested=true but NOT evaluated; degraded_reason_code="
                f"{intraday_5m_info.get('degraded_reason_code')}, "
                f"freshness_reason={intraday_5m_info.get('freshness_reason')})"
            )
            print(
                f"  5m Coverage           : rows_5m(as_of)={intraday_5m_info.get('rows_5m')} | "
                f"last 5m bar={intraday_5m_info.get('latest_available_ts')} | "
                f"stale={intraday_5m_info.get('stale')} lag={intraday_5m_info.get('lag_calendar_days')}d | "
                f"provider={intraday_5m_info.get('provider')}/{intraday_5m_info.get('source')} | "
                f"fallback={_fb_chain}"
            )
        if intraday_5m_info.get("intraday_features_available") is False:
            if intraday_5m_info.get("status") == INTRADAY_5M_STATUS_PENDING:
                print(
                    "  ⏳ 状态               : PENDING PUBLICATION — 5m 当日数据尚未发布"
                    f"（BaoStock 通常 {intraday_5m_info.get('expected_after') or INTRADAY_5M_PUBLICATION_AFTER_LOCAL} 后可用）"
                )
            else:
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
    # (LV2) Only a run genuinely INSIDE the run window may name a clock promise. This line
    # used to print "(14:15 – 14:50)" unconditionally -- including on the two archived PRE-OPEN
    # rounds that motivated LV2, i.e. it told an operator to "act between 14:15 and 14:50" on a
    # report produced at 02:02 market-local. Under the post-close window the promise is the
    # NEXT OPEN (the ticket's price is an as_of close), so that is what gets printed.
    if timing and timing.get("in_window"):
        _exec_date = timing.get("execution_date") or "次一交易日"
        print(
            f"MANDATORY HUMAN TRADER ACTIONS FOR STAGE 2 (成交时点: {_exec_date} 开盘；"
            "工单在本窗口内生成):"
        )
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
    *,
    advisory: Optional[Dict[str, Any]] = None,
    adjust_factor_freshness: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Generate and write structured JSON and Markdown summary reports.

    `advisory` (`AT7` P1) is the optional advisory block from
    `common.advisory_report.build_advisory_block`. It is written verbatim and
    consumed by nothing here: default `None` keeps every existing caller and
    byte-for-byte report unchanged.

    `adjust_factor_freshness` (2026-11-13) is the READ-ONLY census from
    `inspect_stage1_adjust_factor_freshness`: per-symbol `max(adjust_factors.date)` vs
    `max(ohlcv_bars.date)`, its aggregate `status`, and `gates_orders: False`. It is persisted
    verbatim under the same key and read by NO gate -- `None` (every pre-2026-11-13 caller, and
    any run whose cache could not be inspected) simply omits it.
    """
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
        # (2026-11-13, observation only) "Do the adjustment factors reach the bars?" -- see
        # `inspect_stage1_adjust_factor_freshness`. `gates_orders: False` travels inside the
        # block: it is written for audit and consumed by NO gate, weight or order path.
        "adjust_factor_freshness": adjust_factor_freshness,
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
        # (LV7 nit) Copied alongside the value it describes: False == "these strings are the
        # dump's own record, nothing in this report checked them against the live class".
        "strategy_version_verified": strategy_info.get("strategy_version_verified", False),
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
        # (2026-11-12 defect A) TOP-LEVEL mirror of the breaker enum. Stage 2 reads
        # `circuit_breaker_tier` at the report's top level (it also digs into
        # `portfolio_health` since the same fix), and this key used to be absent entirely --
        # so an older reader fell back to its own placeholder, or to a polluted
        # `account_state.json`, and could see "NORMAL" while the gate verdict was UNKNOWN.
        # Degraded runs now publish "UNKNOWN" here (see `attach_nav_provenance`); measured
        # runs publish exactly the tier the drawdown math produced.
        "circuit_breaker_tier": dd_info.get("tier"),
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
        # (`AT7` P1) Advisory-only: the AT5 matrix / AT4 labels / R1 verdict,
        # written for audit and read by NO gate. `advisory=True` +
        # `gates_orders=False` travel inside the block. `None` when the caller
        # did not supply one (existing behaviour preserved).
        "advisory": advisory,
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
            f"N/A ({nav10d_display_len(dd_info)}/{NAV_10D_RECOVERY_WINDOW} 个交易日, 数据不足)"
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
    # (LV7 nit) Stage 1 never compares the dump's recorded versions against the live class
    # (Stage 2 owns that gate and fails closed before any ticket). Say so next to the number
    # instead of letting a stale dump's version print as established fact.
    if not strategy_info.get("strategy_version_verified", False):
        strat_ver_display += " ⚠️ UNVERIFIED (dump 记录值，未与今日活类校验)"
    # (2026-11-04 audit) Derived from Stage 2's own enforcement predicate -- see the
    # SINGLE_STOCK_CAP_* block. This row used to read "Enforced across all regimes"
    # unconditionally, which contradicted the same run's disclosed 21.5077% breach.
    _cap_state = single_stock_cap if isinstance(single_stock_cap, dict) else single_stock_cap_state()
    _cap_display = f"{float(_cap_state.get('cap', 0.20)):.1%}"
    _cap_status_cell = single_stock_cap_status_text(_cap_state)
    # (LV18 label fix, 2026-11-13) "Rescaling portfolio equity exposure downwards" was an
    # unconditional APPLIED-action claim; the factor is a Stage-2 ceiling for the vol-ceiling
    # allowlist only, and it can only LOWER exposure. Same qualifier as the factor line above.
    _vol_cap_assessment = (
        (
            "⚠️ Elevated volatility: total equity exposure is CAPPED, never raised "
            + VOL_CAP_SCOPE_QUALIFIER
        )
        if vol_info["vol_elevated"]
        else "🟢 Realized market variance within standard bounds."
    )
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

- **21-Day Realized Volatility**: `{_fmt_metric(vol_info['realized_vol_21d'], '.1%')}` (Target Vol: `{vol_info['target_vol']:.1%}`)
- **Barroso Volatility Scaling**: `{vol_info['vol_scaling_factor']:.2f}x` {VOL_CAP_SCOPE_QUALIFIER}
- **Input Window**: `{vol_info.get('vol_window_start') or 'N/A'}` → `{vol_info.get('vol_window_end') or 'N/A'}` (n_obs `{_fmt_metric(vol_info.get('vol_n_obs'), 'd')}`, last bar `{vol_info.get('vol_input_bars_last_date') or 'N/A'}`, lag `{_fmt_metric(vol_info.get('vol_input_lag_days'), 'd')}`d, source `{vol_info.get('vol_input_source')}`)
- **Assessment**: {_vol_cap_assessment}
"""
    # (D7) A degraded / stale-window volatility input gets its own loud line. Silence
    # here is what let a frozen 0.3240854096084951 read as a measured market.
    if vol_info.get("vol_degraded"):
        md_content += (
            "\n> **⚠️ VOLATILITY FACTOR UNAVAILABLE (DEGRADED, NOT PASS).** "
            f"reason_code=`{vol_info.get('vol_degraded_reason_code')}`: "
            f"{vol_info.get('vol_degraded_reason')}  "
            "The `1.00x` Barroso scaling above is the neutral fallback "
            "(`vol_scaling_fallback=insufficient_history`), NOT a measured calm market.\n"
        )
    if vol_info.get("vol_intraday_input_stale"):
        md_content += (
            "\n> **⚠️ 5m DOWNSIDE-SEMIVARIANCE WINDOW STALE — REJECTED.** Window ends "
            f"`{vol_info.get('vol_intraday_window_end')}` (lag `{vol_info.get('vol_intraday_lag_days')}`d) "
            f"vs as_of `{vol_info.get('vol_as_of')}`; it was NOT used, so the estimate above "
            "comes from the as-of daily window.\n"
        )
    elif vol_info.get("vol_intraday_rejected_reason"):
        md_content += (
            "\n> **⚠️ 5m DOWNSIDE-SEMIVARIANCE WINDOW NOT USED.** "
            f"{vol_info.get('vol_intraday_rejected_reason')} "
            f"(available sessions `{vol_info.get('vol_intraday_available_obs')}`).\n"
        )
    md_content += "\n---\n"
    # (2026-10-22 incident, Fix 1) Intraday metrics are rendered only when they
    # were actually measured; otherwise the section states UNAVAILABLE and every
    # number is `N/A`. A missing measurement must never be printed as a value.
    if intraday_5m_info and (intraday_5m_info.get("requested") or intraday_5m_info.get("enabled")):
        _id_avail = intraday_5m_info.get("intraday_features_available")
        md_content += "\n## 4. 5-Minute Intraday Microstructure\n\n"
        if intraday_5m_info.get("degraded"):
            # (D7) One loud, grep-able line: `requested=true` is NOT "measured". It now names
            # the lag (in days) and the ACTUAL provider/fallback chain, because "provider does
            # not support 5m" was a misdiagnosis of a frozen cache served by a fallback.
            _fb_reason = intraday_5m_info.get("fallback_reason")
            _fb_from = intraday_5m_info.get("fallback_from")
            _fb_to = intraday_5m_info.get("fallback_to")
            if _fb_from or _fb_to:
                _fb_text = f"`{_fb_from}→{_fb_to}" + (
                    f" ({_fb_reason})`" if _fb_reason else "`"
                )
            else:
                _fb_text = "无回退（`provider={}`）".format(
                    intraday_5m_info.get("provider")
                )
            md_content += (
                f"> **⚠️ DEGRADED — `status={intraday_5m_info.get('status')}`, "
                f"`degraded=true`, `degraded_reason_code={intraday_5m_info.get('degraded_reason_code')}`, "
                f"`freshness_reason={intraday_5m_info.get('freshness_reason')}`.** "
                "`requested=true` 只表示已请求 5m 层，**不表示已测得**。"
                f"as_of 当日 5m 行数 `{intraday_5m_info.get('rows_5m')}`，"
                f"5m 数据最后一根 bar `{intraday_5m_info.get('latest_available_ts') or intraday_5m_info.get('intraday_data_last_bar_date')}`"
                f"，`stale={intraday_5m_info.get('stale')}`，滞后 "
                f"`{intraday_5m_info.get('lag_calendar_days')}` 天；"
                f"供数来源 `{intraday_5m_info.get('provider')}` / `{intraday_5m_info.get('source')}`，"
                f"回退链 {_fb_text}"
                f"（{intraday_5m_info.get('n_fallback_symbols')}/{intraday_5m_info.get('n_symbols')} 个标的）。\n\n"
            )
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
        + ("5" if (intraday_5m_info and (intraday_5m_info.get("requested") or intraday_5m_info.get("enabled"))) else "4")
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
    intraday_5m_provider: Optional[str] = None,
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
    # (2026-11-13) Forward the PINNED 5m provider so the child loads its 5m layer from the
    # same source this process gated on. `None` (unpinned) appends nothing, so every
    # existing argv stays byte-for-byte identical.
    if intraday_5m_provider:
        cmd.extend(["--intraday-5m-provider", str(intraday_5m_provider)])
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
    #
    # (L2.30 Stage-1 half, 2026-11-13) `run_mode` is DERIVED, never asserted. This call used
    # to hardcode `run_mode="live"`, so a `--as-of-date` pinned to an EARLIER session (a
    # deliberate replay/backfill) still produced a Stage-1 report claiming `run_mode="live"`
    # in `execution_timing` -- a report lying about its own mode, in the very block a post-hoc
    # audit reads. The predicate is NOT re-implemented here: `resolve_stage1_run_mode`
    # delegates to `scripts.run_live_strategy.resolve_run_mode`, the one replay predicate both
    # Stage 2 and the netted runner already use (same-day / clock-derived runs stay `live`
    # bit-for-bit, so a genuine live run's report is unchanged).
    run_mode = resolve_stage1_run_mode(clock_ctx)
    timing = resolve_execution_timing(clock_ctx, as_of=as_of_date, run_mode=run_mode)
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
        # (LV7 nit, 2026-11-06) PROVENANCE, not verification. Stage 1 reads these strings
        # straight out of the dump and runs NO dump-vs-live-class parity check -- that gate
        # lives in Stage 2's `resolve_strategy_definition` (and re-instantiating the template
        # here purely to compare versions would duplicate its entire construction, plus add a
        # new failure mode to a report-only stage). Always False, so no consumer can read the
        # numbers above as "confirmed against the live engine"; Stage 2 re-checks them and
        # fails closed before any ticket exists.
        "strategy_version_verified": False,
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

    # (2026-11-13, OBSERVATION ONLY) The bar freshness above is about `ohlcv_bars`; nothing so
    # far compared it with `adjust_factors`, which is written by a DIFFERENT path and stopped 12
    # sessions earlier. `get_latest_factor_hfq` is non-None for such a symbol, so
    # `MissingAdjustFactorsError` never fires and the qfq anchor silently comes from the stale
    # row. The census names that state (JSON + one [WARN] line); it feeds NO gate and NO order
    # path -- see `inspect_stage1_adjust_factor_freshness`.
    adjust_factor_freshness = inspect_stage1_adjust_factor_freshness(
        refresh_summary=refresh_summary,
        cache_dir=args.cache_dir or shared_data_dir(),
    )
    _af_freshness_warning = format_adjust_factor_freshness_warning(adjust_factor_freshness)
    if _af_freshness_warning:
        print(_af_freshness_warning)

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

    # (LV7, `docs/lv7_prior_state_contract.md` §2) The carried `prior_state` must be the
    # PREVIOUS session's confirmed snapshot. `latest_account_state_at_or_before` already
    # picks the right instance (订正 ①) -- this is VALIDATION ONLY, it re-selects nothing.
    # A frozen carrier (prior NAV == today's NAV) is annotated, never "corrected".
    # The MEASURED gate NAV is the right comparison value: on a degraded run
    # `nav_gate_nav` is None (the form placeholder is display-only), so no marker is
    # claimed from a number nobody measured.
    prior_state_frozen_reason = detect_frozen_prior_state(
        prior_state_to_preserve,
        nav_resolution["nav_gate_nav"],
    )

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
    # (2026-11-13) WHICH provider serves the 5m BARS, resolved through the SAME chain as
    # the switch above (`cfsb_intraday_5m_provider` -> `blend_intraday_5m_provider` ->
    # `intraday_5m_provider`). `None` = today's behaviour: use the run-level
    # `--data-provider` and let `load_intraday_5m_universe`'s fallback chain decide per
    # symbol. A pin is NOT a silent preference: see `StrategyConfig.intraday_5m_provider`
    # for the measured split (equities' 5m only under BaoStock, indices' only under
    # Fuyao/EastMoney) that makes the run-level provider unable to serve both halves.
    #
    # Resolved HERE (outside the loader's `try`) so the value survives a loader failure and
    # can always be forwarded to Stage 2 below -- Stage 2 must load the 5m layer from the
    # SAME provider Stage 1 gated on. Store it under the flags' naming so the two stages
    # cannot drift (`args.intraday_5m_provider` is reserved for the forwarded value; the
    # param-derived one keeps the `_resolved` suffix).
    _intraday_5m_provider_resolved = resolve_blend_param(
        params, "intraday_5m_provider", None, str
    )
    if _intraday_5m_provider_resolved is not None:
        _intraday_5m_provider_resolved = str(_intraday_5m_provider_resolved).strip() or None

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
    _5m_coverage: Optional[Dict[str, Any]] = None
    _5m_provenance: Optional[Dict[str, Any]] = None
    _5m_freshness: Optional[Dict[str, Any]] = None
    _5m_publication: Optional[Dict[str, Any]] = None
    if enable_intraday_5m:
        try:
            from common.intraday_features import (
                IntradayFeatureConfig,
                assess_intraday_5m_freshness,
                load_intraday_5m_universe,
                enrich_universe_with_intraday_features,
            )
            # (2026-11-13) The 5m BARS may come from a DIFFERENT provider than every daily
            # bar: `provider_5m` is a SECOND instance, used ONLY for
            # `load_intraday_5m_universe`. The daily provider (loaded earlier from
            # `args.data_provider`), the Stage-1 cache refresh that ran before it, and every
            # other fetch are untouched. Unpinned (`None`) => the run-level provider, i.e.
            # exactly the object/behaviour this block had before the pin existed.
            _5m_provider_name = _intraday_5m_provider_resolved or args.data_provider
            provider_5m = get_data_provider(_5m_provider_name, **build_data_kwargs(args))
            if _intraday_5m_provider_resolved:
                print(
                    f"[INTRADAY 5M] 5m 行情提供方已按策略参数固定为 "
                    f"{_intraday_5m_provider_resolved}（日线仍用 {args.data_provider}）"
                )
            cutoff_t = getattr(args, "cutoff_time", "14:00") or "14:00"
            universe_5m = load_intraday_5m_universe(
                universe_symbols,
                start=start_date,
                end=as_of_date,
                data_provider=provider_5m,
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
            # (D7) MEASURE the 5m coverage before deciding anything. `symbols_5m_available`
            # alone cannot tell "provider has no 5m" apart from "the 5m cache stopped on a
            # previous session"; the last-bar date and the as-of row count can.
            _5m_coverage = probe_intraday_5m_cache_coverage(universe_5m, as_of_date)
            # (D7 follow-up) WHICH provider served the bars, and whether a fallback happened.
            # A `fuyao` run served by BaoStock's frozen cache now says so instead of looking
            # like a fuyao delivery.
            _5m_provenance = summarize_intraday_5m_provenance(universe_5m)
            # (D7 follow-up) Session-level staleness, worst-symbol aggregation, delegated to
            # the ONE owner (`common.intraday_features.assess_intraday_5m_freshness`): the
            # max-bar-date heuristic cannot tell "closing bar not in yet" from "frozen cache".
            # Guarded: a calendar error must not take the whole 5m block down with it.
            try:
                _5m_freshness = assess_intraday_5m_freshness(
                    universe_5m,
                    expected_last_session=as_of_date,
                    market=getattr(clock_ctx, "market", "china") or "china",
                )
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                print(f"[WARN] 5m 新鲜度评估失败（按未评估处理）: {exc}")
                _5m_freshness = None
            # (2026-11-12) SECOND, INDEPENDENT AXIS: is the as-of session's 5m layer simply
            # not published yet? BaoStock usually refreshes the day's minute bars after
            # ~20:00, while the DAILY bar is ready at close+grace (15:30). Without this,
            # a 16:00 run reported a stale/frozen cache when nothing was wrong. It is built
            # ON TOP of `assess_intraday_5m_freshness` (same `stale` truth, no second
            # deadline arithmetic) and it never relaxes the rule that a previous session's
            # 5m bars are unusable -- `available` is defined as `not stale`.
            _5m_publication: Optional[Dict[str, Any]] = None
            try:
                from common.intraday_features import assess_intraday_5m_publication_state
                _5m_publication = assess_intraday_5m_publication_state(
                    universe_5m,
                    as_of=as_of_date,
                    market=getattr(clock_ctx, "market", "china") or "china",
                    now=_intraday_5m_now(),
                    publish_after=getattr(
                        args, "intraday_publish_after", INTRADAY_5M_PUBLICATION_AFTER_LOCAL
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                print(f"[WARN] 5m 发布状态评估失败（按「未知/已过期」处理）: {exc}")
                _5m_publication = None
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
                # (D7) The old text blamed the provider unconditionally. Now the reason
                # states WHICH cause was MEASURED: no 5m rows at all vs. a 5m cache whose
                # last bar predates as_of (the 2026-11-10/11-11 case: 20 frames present,
                # last bar 2026-11-09, `provider=fuyao` merely served stale rows), and WHICH
                # provider/fallback chain actually delivered them.
                _last_5m = _5m_coverage.get("intraday_data_last_bar_date")
                _lag_5m = _5m_freshness.get("lag_calendar_days")
                _stale_5m = bool(_5m_freshness.get("stale"))
                _served_by = _5m_provenance.get("provider") or "unknown"
                _fb_from = _5m_provenance.get("fallback_from")
                _fb_to = _5m_provenance.get("fallback_to")
                _fb_reason = _5m_provenance.get("fallback_reason")
                _fb_chain = (
                    f"{_fb_from}→{_fb_to} ({_fb_reason})"
                    if _fb_from or _fb_to
                    else "无回退"
                )
                _5m_pending = bool(
                    _5m_publication is not None
                    and _5m_publication.get("status") == INTRADAY_5M_STATUS_PENDING
                )
                _5m_publication_missing = bool(
                    _5m_publication is not None
                    and not _5m_publication.get("available")
                    and not _5m_pending
                )
                if _5m_pending:
                    # (2026-11-12) Named cause, not a cache defect: BaoStock's 5m layer for the
                    # as-of session is usually published after ~20:00. Greppable on purpose.
                    print(
                        "[WARN] 5m 当日数据尚未发布（BaoStock 通常 20:00 后可用）；本次运行将不使用 5m 特征"
                    )
                    _unavail_reason = (
                        f"5m 当日数据尚未发布（BaoStock 通常 {_5m_publication.get('expected_after')} "
                        f"后可用，当前 {_5m_publication.get('now_local')}）；本次运行将不使用 5m 特征，"
                        "且不会退化为使用前一交易日的 5m 数据"
                        f"（缓存末根={_5m_publication.get('latest_available_ts')}，"
                        f"expected_last_session={_5m_publication.get('expected_last_session')}）。"
                        "如需 5m 日内门禁结论，请在该时点之后重跑。"
                    )
                elif n_5m_ok <= 0:
                    _unavail_reason = (
                        f"5m 日内特征缺失: {len(universe_symbols)} 个标的中 5m 数据帧非空的 0 个"
                        f"(provider={_5m_provider_name}, 5m 行数={_5m_coverage.get('rows_5m')})。"
                        "这是没有任何 5m 数据（缓存与 provider 均为空），不是「当日特征算不出」。"
                    )
                elif _stale_5m:
                    _unavail_reason = (
                        f"5m 日内特征缺失（5m 数据陈旧，NOT 该 provider 不支持 5m）: "
                        f"{len(universe_symbols)} 个标的中 5m 数据帧非空 {n_5m_ok} 个，"
                        f"但这些帧的最后一根 bar 是 {_last_5m}"
                        f"（expected_last_session={_5m_freshness.get('expected_last_session')}，"
                        f"滞后 {_lag_5m} 天，freshness_reason={_5m_freshness.get('reason')}，"
                        f"as_of 当日 5m 行数={_5m_coverage.get('rows_5m')}），"
                        f"因此当日没有任何 VWAP_Prem_5m 特征。实际供数 provider={_served_by}，"
                        f"回退链={_fb_chain}。需先补齐 5m 缓存，不是换 provider。"
                        + (
                            # (2026-11-12) The publication hour has already passed, so
                            # "not published yet" is ruled out -- say what that means.
                            f"已过 {_5m_publication.get('expected_after')} 仍缺当日 5m，需检查同步。"
                            if _5m_publication_missing
                            else ""
                        )
                    )
                else:
                    _unavail_reason = (
                        f"5m 日内特征缺失: {len(universe_symbols)} 个标的中 5m 数据帧非空 {n_5m_ok} 个，"
                        f"as_of {as_of_date} 当日 5m 行数={_5m_coverage.get('rows_5m')}，"
                        "仍无任何标的具备已测得的 VWAP_Prem_5m 特征 "
                        f"(实际供数 provider={_served_by}，回退链={_fb_chain})。"
                        "这是特征计算/读取路径问题，不是 provider 能力缺失。"
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

    # (D7) Merge the MEASURED coverage / provenance / freshness record into whichever branch
    # ran, and correct `enabled` to the web layer's contract (real rows on the as-of session).
    # Without this, `enabled=true` claimed "已启用" on a stale 5m cache.
    if intraday_5m_info is not None:
        _5m_coverage_rec = _5m_coverage or probe_intraday_5m_cache_coverage({}, as_of_date)
        _5m_prov_rec = _5m_provenance or summarize_intraday_5m_provenance({})
        _5m_status = classify_intraday_5m_status(
            _5m_coverage_rec,
            n_5m_symbols_nonempty=int(intraday_5m_info.get("symbols_5m_available") or 0),
            features_measured=int(intraday_5m_info.get("symbols_vwap_measured") or 0),
            requested=True,
            freshness=_5m_freshness,
            publication=_5m_publication,
        )
        intraday_5m_info.update(_5m_coverage_rec)
        intraday_5m_info.update(_5m_prov_rec)
        intraday_5m_info.update(_5m_status)
        if _5m_freshness is not None:
            # Session-level staleness: worst-symbol aggregation straight from the OWNER
            # (`assess_intraday_5m_freshness`), never re-derived here.
            intraday_5m_info.update(
                {
                    "expected_last_session": _5m_freshness.get("expected_last_session"),
                    "expected_deadline_ts": _5m_freshness.get("expected_deadline_ts"),
                    "latest_available_ts": _5m_freshness.get("latest_available_ts"),
                    "stale": bool(_5m_freshness.get("stale")),
                    "lag_calendar_days": _5m_freshness.get("lag_calendar_days"),
                    "freshness_reason": _5m_freshness.get("reason"),
                    "stale_symbols": list(_5m_freshness.get("stale_symbols") or []),
                    "fresh_symbols": list(_5m_freshness.get("fresh_symbols") or []),
                    "n_symbols": _5m_freshness.get("n_symbols"),
                }
            )

    # (LV12) Attach the explicit form-vs-MTM record to the SAME dict that lands in
    # `stage1_health_report.json::intraday_5m_metrics` -- a log line alone would be invisible to
    # every consumer of the report. Omitted entirely when the two NAVs agree (or when the run was
    # degraded), so the `form == MTM` outputs stay byte-identical.
    if intraday_5m_info is not None and intraday_nav_base_record is not None:
        intraday_5m_info["nav_base"] = intraday_nav_base_record

    macro_info = evaluate_market_breadth_and_thrust(universe, params)
    # (D7) `as_of` is passed so the 5m downside-semivariance window is MEASURED against
    # today's session and rejected when stale; without it a frozen 5m cache silently
    # froze `realized_vol_21d` across sessions (2026-11-09/10/11 archive).
    vol_info = evaluate_realized_volatility(universe, params, as_of=as_of_date)

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
        # (2026-11-12 seeding race) The triple for the state THIS round creates for
        # `as_of_date`, taken from the strategy dump this run already loaded -- never
        # inherited from `state`, which on the first run of a session is yesterday's copy.
        _round_triple = _round_version_triple(strategy_def)
        updated_state = build_account_state_update(
            state,
            baseline_state,
            dd_info,
            as_of_date=as_of_date,
            gate_code=gate_code,
            intraday_5m_info=intraday_5m_info,
            prior_state=prior_state_to_preserve,
            prior_state_frozen_suspect=prior_state_frozen_reason,
            version_triple=_round_triple,
        )
        save_account_state(args.account_state_file, updated_state)
        # (2026-11-12 seeding race, requirement b) ASSERT WHAT LANDED. Stamping in memory is
        # not proof of the file: a later write into the same path (a second seeding pass, a
        # restarted stage, an operator edit) is exactly how the two 11-12 instances diverged.
        # Re-read and refuse -- with the grep-able marker -- when the on-disk state claims THIS
        # session with another engine's triple. Read-only: nothing is re-stamped here.
        _landed = load_account_state(args.account_state_file)
        _landed_clobber = detect_state_version_clobber(
            _landed if isinstance(_landed, dict) else {},
            _round_triple.get("strategy_version"),
            _round_triple.get("base_version"),
            as_of_date=as_of_date,
        )
        if _landed_clobber:
            raise LiveDumpPreflightError(f"{_landed_clobber} `{args.account_state_file}`")

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

    # (`AT7` P1) One advisory block for this run, built from HONEST EMPTY INPUTS:
    # no cross-sectional score source exists in the live chain yet
    # (`common/signal_snapshot.py:12-38`) and there is no executed-notional
    # ledger, so the matrix renders INSUFFICIENT and both capacity labels
    # render MISSING. Nothing is fabricated to make the report look measured.
    advisory_block = build_advisory_block(
        phase=ADVISORY_PHASE_P1,
        as_of=as_of_date,
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
        advisory=advisory_block,
        adjust_factor_freshness=adjust_factor_freshness,
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
                args,
                enable_intraday_5m=enable_intraday_5m,
                portfolio_value=gate_nav,
                # (2026-11-13) Same pin this Stage used for its own 5m load.
                intraday_5m_provider=_intraday_5m_provider_resolved,
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

#!/usr/bin/env python3
"""Universal Operational Daily Live Execution Runner for Quantitative Trading Strategies.

Supports all strategies in the quantitative workspace:
  - 55+ research strategies (Chan blend models, Chan structural timing, Turtle, Bollinger, WorldQuant, etc.)
  - static allocation templates (equal_weight, hierarchical_risk_parity, momentum, sleeve_allocator, etc.)
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
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


from common.advisory_report import (
    ADVISORY_PHASE_P3,
    _json_number,
    build_advisory_block,
    r1_decision_to_dict,
)
from common.data import get_data_provider
from common.effective_params import (
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    SOURCE_DUMP,
    SOURCE_OVERRIDE,
    build_effective_params,
    param_entry,
)
from common.allocation_backtester import CASH_PROXY_SYMBOLS, _get_hk_board_lot
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
from common.intraday_drawdown_derisk import evaluate_intraday_drawdown
from common.r1_derisk_report import apply_r1_intraday_derisk
from common.live_execution_timing import (
    MissedExecutionOpenError,
    annotate_executability,
    build_ticket_payload,
    executability_log_line,
    merge_timing_into_summary,
    resolve_execution_timing,
    ticket_order_rows,
    write_execution_timing,
)
from common.market_clock import (
    MARKET_CHINA,
    MixedMarketUniverseError,
    UnknownSymbolMarketError,
    as_of_trading_date,
    infer_market_from_symbol,
    infer_market_from_symbols,
)
from common.overnight_derisk import (
    DECISION_SPEC_VERSION as OVERNIGHT_DERISK_SPEC_VERSION,
    DEFAULT_DOWNSCALE_RATIO as OVERNIGHT_DERISK_DEFAULT_RATIO,
    OvernightDeriskConfig,
    OvernightDeriskStatus,
    apply_overnight_downscale,
    compute_gap_series,
    evaluate_overnight_derisk,
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
    cold_start_lifecycle_state,
    derive_lifecycle_context_from_frame,
    is_liquidation_event,
    lifecycle_context_at,
    resolve_position_policy_config,
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
from scripts.live_preflight import (
    LiveDumpPreflightError,
    apply_prior_state_selection,
    assert_account_state_version_parity,
    assert_dump_version_parity,
    assert_state_version_not_clobbered,
    derive_session_root,
    select_prior_state,
    verify_live_strategy_dump_preflight,
)


class LiveGateDegradedError(RuntimeError):
    """(LV11 item 6) Stage 1's gate NAV was unavailable, so NO ticket may be produced.

    The circuit breaker is what authorises risk on this account, and it needs a marked-to-market
    NAV. When Stage 1 could not measure one it records `tier_status="UNKNOWN"` /
    `nav_gate_status="DEGRADED_NAV_UNAVAILABLE"` (and, since LV11, persists
    `circuit_breaker_tier="UNKNOWN"` rather than the placeholder's "NORMAL"). Until now Stage 2
    read ONLY `circuit_breaker_tier` and would have carried on ordering: "report UNKNOWN /
    archive NORMAL / orders issued" could all be true at once.

    Missing is NOT "not triggered" (same rule as the 10-day NAV recovery leg and §2.19 D11
    item 4), so this refuses instead of assuming the breaker passed. The operator's remedy is
    to restore/stage `nav_breakdown.json` and re-run Stage 1 -- never to widen the gate.
    """


#: (LV11) The ONE documented way to proceed on a degraded gate: an explicit operator
#: acknowledgement, never a default and never inferred.
#:
#: WHY IT HAS TO EXIST. Stage 1 takes no holdings/cash input, so on the very first round of a
#: brand-new account there is nothing in the archive to mark (`resolve_seed_holdings` ->
#: `("none")`) and the gate legitimately cannot be evaluated. Without an escape that state
#: would be a bootstrap deadlock: no gate -> refuse to order -> the archive never gains the
#: snapshot the next gate would need. The web layer sets this variable ONLY for the run whose
#: operator checked "人工覆盖净值" (`nav_override=True`, i.e. "I assert the NAV by hand")
#: -- the same assertion that already authorises an unmarkable book in `_resolve_nav`.
#:
#: It does NOT make the degradation invisible: the archive still carries
#: `circuit_breaker_tier="UNKNOWN"` / `tier_status="UNKNOWN"`, and this run additionally
#: records `gate_degraded_acknowledged=True` in `summary.json`, so "orders were issued on an
#: unevaluated breaker, knowingly" is machine-readable rather than silent.
ALLOW_DEGRADED_GATE_ENV = "LIVETRADING_ALLOW_DEGRADED_GATE"


def degraded_gate_acknowledged() -> bool:
    """True only when the operator explicitly acknowledged an unevaluated breaker."""
    return str(os.environ.get(ALLOW_DEGRADED_GATE_ENV, "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


#: (2026-11-12 defect A) `stage1_health_report.json` publishes the gate verdict INSIDE
#: `portfolio_health` (`tier_status`, `nav_gate_status`, `nav_breaker_evaluated`,
#: `nav_source`, ...), but `README`-era vintages only mirrored a subset at the top level and
#: that top level could carry the placeholder `circuit_breaker_tier="NORMAL"`. The pre-fix
#: reader looked ONLY at the top level, so `tier_status`/`nav_gate_status` were always
#: `None` and the fail-closed branch leaned entirely on `account_state.json` -- which is
#: itself carried over from the previous round (`web/livetrading/execution_service.py`),
#: i.e. exactly the archive that can be stale-but-green.
HEALTH_GATE_UNKNOWN_STATUS = "UNKNOWN"
HEALTH_GATE_DEGRADED_NAV_STATUS = "DEGRADED_NAV_UNAVAILABLE"
#: "More defensive" ordering used when the two archives disagree about the tier. A
#: lower-severity tier may never override a higher-severity one; `"UNKNOWN"` outranks
#: everything because an unevaluated breaker is not a passing breaker.
HEALTH_GATE_TIER_SEVERITY: Dict[str, int] = {
    "UNKNOWN": 100,
    "TIER_3_HALT": 40,
    "TIER_2_DEFENSIVE": 30,
    "TIER_1_DAMPING": 20,
    "CAUTION_RECOVERING": 10,
    "NORMAL": 0,
}


def _gate_text(value: Any) -> Optional[str]:
    """`value` as a stripped string, or `None` when absent/blank (never `"None"`)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _gate_first_present(*values: Any) -> Any:
    """First value that is not `None`. Unlike `a or b` this keeps a legitimate `0.0`."""
    for value in values:
        if value is not None:
            return value
    return None


def _gate_keyed_provenance(gate: Dict[str, Any], key: str, *fallbacks: Any) -> Any:
    """THIS round's gate value when the key exists (even as `None`), else the fallbacks.

    A degraded report explicitly stores `nav_mtm: null`; falling back to a carried-over
    numeric NAV in that case would print a marked-to-market value the gate refused to use.
    """
    if key in gate:
        return gate.get(key)
    return _gate_first_present(*fallbacks)


def resolve_stage1_health_gate(
    account_state: Optional[Dict[str, Any]],
    health_report: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve the Stage-1 gate verdict from BOTH archives, keeping the WORSE answer.

    WHY BOTH: `stage1_health_report.json` is this round's measurement;
    `account_state.json` is what the previous round persisted. Either can be the stale one,
    and a green value in the stale one must not launder a degraded value in the other (the
    2026-11-12 incident: the report said `tier_status=UNKNOWN` nested while the carried
    account state said `EVALUATED`, and Stage 2 ordered anyway).

    Resolution rules (deliberately additive -- nothing previously refused becomes allowed):
      * `tier_status`     -- `UNKNOWN` if EITHER source has it; else `EVALUATED` if either does;
      * `nav_gate_status` -- `DEGRADED_NAV_UNAVAILABLE` if either has it;
      * `circuit_breaker_tier` -- max SEVERITY across `account_state`, the nested
        `portfolio_health` block and the report's top-level mirror. A top-level placeholder
        `"NORMAL"` therefore cannot outvote a nested `TIER_2_DEFENSIVE`, and when the gate is
        known to be unevaluated the resolved tier is forced to `"UNKNOWN"` instead of the
        placeholder "NORMAL";
      * `nav_breaker_evaluated` -- `False` wins over `True`; a string-independent guard for
        the same condition (a rewritten/renamed status string cannot hide it).

    Pure dict-in/dict-out (no I/O) so the fail-closed decision is unit-testable offline.
    """
    acct = account_state if isinstance(account_state, dict) else {}
    report = health_report if isinstance(health_report, dict) else {}
    # The nested block is where Stage 1 actually writes the verdict; older/flat reports put
    # it at the top level. Read both -- the top level is a fallback, never the only source.
    _health_gate = report.get("portfolio_health")
    if not isinstance(_health_gate, dict):
        _health_gate = report
    sources = (acct, _health_gate, report)

    tier_statuses = [_gate_text(s.get("tier_status")) for s in sources]
    nav_gate_statuses = [_gate_text(s.get("nav_gate_status")) for s in sources]

    def _worse_status(values: List[Optional[str]], degraded: str) -> Optional[str]:
        present = [v for v in values if v]
        if any(v.upper() == degraded for v in present):
            return degraded
        return present[0] if present else None

    tier_status = _worse_status(tier_statuses, HEALTH_GATE_UNKNOWN_STATUS)
    nav_gate_status = _worse_status(nav_gate_statuses, HEALTH_GATE_DEGRADED_NAV_STATUS)

    breaker_flags = [s.get("nav_breaker_evaluated") for s in sources]
    if any(flag is False for flag in breaker_flags):
        nav_breaker_evaluated: Optional[bool] = False
    elif any(flag is True for flag in breaker_flags):
        nav_breaker_evaluated = True
    else:
        nav_breaker_evaluated = None

    tier_candidates = [
        _gate_text(acct.get("circuit_breaker_tier")),
        _gate_text(_health_gate.get("circuit_breaker_tier")),
        _gate_text(_health_gate.get("tier")),
        _gate_text(report.get("circuit_breaker_tier")),
    ]
    _known_tiers = [c for c in tier_candidates if c]
    if (
        tier_status == HEALTH_GATE_UNKNOWN_STATUS
        or nav_breaker_evaluated is False
        or any(c.upper() == HEALTH_GATE_UNKNOWN_STATUS for c in _known_tiers)
    ):
        # An unevaluated breaker is not a passing breaker -- never let the placeholder
        # "NORMAL" (or a carried-over green tier) speak for it.
        circuit_tier: Optional[str] = HEALTH_GATE_UNKNOWN_STATUS
    elif _known_tiers:
        circuit_tier = max(
            _known_tiers,
            key=lambda c: HEALTH_GATE_TIER_SEVERITY.get(c.upper(), 15),
        )
    else:
        circuit_tier = None

    degraded_reasons: List[str] = []
    if tier_status == HEALTH_GATE_UNKNOWN_STATUS:
        degraded_reasons.append("tier_status=UNKNOWN")
    if nav_gate_status == HEALTH_GATE_DEGRADED_NAV_STATUS:
        degraded_reasons.append("nav_gate_status=DEGRADED_NAV_UNAVAILABLE")
    if nav_breaker_evaluated is False:
        degraded_reasons.append("nav_breaker_evaluated=false")
    if circuit_tier == HEALTH_GATE_UNKNOWN_STATUS:
        degraded_reasons.append("circuit_breaker_tier=UNKNOWN")

    return {
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "tier_status": tier_status,
        "nav_gate_status": nav_gate_status,
        "circuit_breaker_tier": circuit_tier,
        "nav_breaker_evaluated": nav_breaker_evaluated,
        # Provenance is read from THIS round's gate block first (it names the degradation),
        # then the carried-over account_state (which may hold a persisted timebase verdict),
        # then the report's flat mirror.
        "nav_source": _gate_keyed_provenance(
            _health_gate, "nav_source", acct.get("nav_source"), report.get("nav_source")
        ),
        "nav_mtm": _gate_keyed_provenance(
            _health_gate, "nav_mtm", acct.get("nav_mtm"), report.get("nav_mtm")
        ),
        "nav_timebase_status": _gate_keyed_provenance(
            _health_gate,
            "nav_timebase_status",
            acct.get("nav_timebase_status"),
            report.get("nav_timebase_status"),
        ),
        "gate": _health_gate,
    }


def format_stage1_gate_degraded_detail(info: Dict[str, Any]) -> str:
    """One-line, copy-pasteable provenance for a refused/acknowledged degraded gate."""
    return (
        f"tier_status={info.get('tier_status')!r} "
        f"nav_gate_status={info.get('nav_gate_status')!r} "
        f"circuit_breaker_tier={info.get('circuit_breaker_tier')!r} "
        f"nav_breaker_evaluated={info.get('nav_breaker_evaluated')!r} "
        f"nav_source={info.get('nav_source')!r} "
        f"nav_mtm={info.get('nav_mtm')!r} "
        # (LV1-TIMEBASE) Which KIND of degradation this is: the snapshot existed, but its
        # equity and cash legs spanned two instants.
        f"nav_timebase_status={info.get('nav_timebase_status')!r} "
        f"reasons={info.get('degraded_reasons')}"
    )


def assert_stage1_gate_usable(
    account_state: Optional[Dict[str, Any]],
    health_report: Optional[Dict[str, Any]],
    *,
    acknowledged: Optional[bool] = None,
) -> Dict[str, Any]:
    """Refuse BEFORE any order math when Stage 1's gate NAV was not usable.

    Returns the resolved gate info (including `circuit_breaker_tier`, which `main()` still
    needs for the Tier-2/Tier-3 paths). Raises `LiveGateDegradedError` unless the operator
    explicitly acknowledged the degradation (`LIVETRADING_ALLOW_DEGRADED_GATE=1`); the
    acknowledgement is logged loudly and is NOT silent.
    """
    info = resolve_stage1_health_gate(account_state, health_report)
    if not info["degraded"]:
        return info
    _degraded_detail = format_stage1_gate_degraded_detail(info)
    if acknowledged is None:
        acknowledged = degraded_gate_acknowledged()
    if not acknowledged:
        raise LiveGateDegradedError(
            "\n" + "!" * 110
            + "\n[REFUSED / 拒绝出票] Stage 1 门禁净值不可用：本次回撤/熔断判定为 UNKNOWN，"
            "\n**不是**「未触发」。缺失 ≠ 未触发 —— 在未取得盯市净值前不得生成任何工单。"
            f"\n  {_degraded_detail}"
            "\n处置: 恢复/预置 nav_breakdown.json 后重跑 Stage 1（web 启动器会在拉起 Stage 1 前"
            "预置估值快照），切勿放宽门禁或用表单值替代盯市净值。"
            "\n唯一例外: 操作员已人工裁定净值（表单勾选「人工覆盖净值」）时，"
            f"{ALLOW_DEGRADED_GATE_ENV}=1 会记录该次知情承担并继续。"
            + "\n" + "!" * 110
        )
    # Acknowledged: proceed, but NEVER quietly -- the archive records the acknowledgement
    # (`gate_degraded_acknowledged` in `summary.json`) on top of Stage 1's own
    # `circuit_breaker_tier="UNKNOWN"`, so an auditor can see the breaker was unevaluated.
    print("\n" + "!" * 110)
    print(
        "[WARN] (LV11) 操作员已显式确认在**门禁净值不可用**的前提下继续出票 "
        f"({ALLOW_DEGRADED_GATE_ENV}=1)。熔断档位仍为 UNKNOWN —— 这不是「已评估且未触发」。"
    )
    print(f"       {_degraded_detail}")
    print("!" * 110 + "\n")
    return info


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
    for prefix in _BLEND_PARAM_PREFIXES:
        k = f"{prefix}_{name}"
        if k in params:
            val = params[k]
            return param_type(val) if param_type is not None and val is not None else val
    if name in params:
        val = params[name]
        return param_type(val) if param_type is not None and val is not None else val
    return default


#: Blend prefix spellings, in resolution order. One tuple so the resolver and the
#: AT8 provenance layer can never disagree about which key won.
_BLEND_PARAM_PREFIXES: Tuple[str, ...] = ("cfsb", "crb", "ccsb", "cfssb", "cdhb", "blend")

#: Literal marker for "this run has no strategy-dump artifact".
MISSING_DUMP_SHA256 = "MISSING"


def effective_params_dump_sha256(
    config_source: Optional[str],
    repo_root: Optional[str] = None,
) -> str:
    """sha256 of the strategy-dump artifact this run resolved, else ``"MISSING"``.

    `config_source` is `resolve_strategy_definition`'s `resolved_desc`: a repo-relative
    path for a strategy/dump file, or ``strategies_config.json:<key>`` /
    ``static_template:<name>`` for the two non-dump origins.

    Only a REAL file gets a hash. A static template, a `strategies_config.json:` entry
    (that file is not the run's SSOT dump) and a nonexistent path all report the
    literal ``"MISSING"`` -- never a fabricated 64-hex value that would look like
    provenance. Read-only: the file is opened ``rb`` and never written.
    """
    if not config_source:
        return MISSING_DUMP_SHA256
    desc = str(config_source)
    if desc.startswith("strategies_config.json:") or desc.startswith("static_template:"):
        return MISSING_DUMP_SHA256
    path = desc if os.path.isabs(desc) else os.path.join(repo_root or _REPO_ROOT, desc)
    if not os.path.isfile(path):
        return MISSING_DUMP_SHA256
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return MISSING_DUMP_SHA256


def _effective_param_source(
    declared_params: Dict[str, Any],
    keys: Iterable[str],
    *,
    cli_override: bool = False,
    config_origin: str = SOURCE_DUMP,
) -> Tuple[Any, str]:
    """Return ``(declared, source)`` for the first key present in `declared_params`.

    Precedence mirrors the resolver: the caller passes `keys` in RESOLUTION order
    (blend prefixes first, then the bare name, then suffix aliases). A CLI override
    outranks everything (that is what the runner's own resolvers do).
    """
    declared = None
    found = False
    for key in keys:
        if key in declared_params:
            declared = declared_params[key]
            found = True
            break
    if cli_override:
        return declared, SOURCE_OVERRIDE
    if not found:
        return None, SOURCE_DEFAULT
    return declared, config_origin


def _record_effective_param(
    entries: Dict[str, Dict[str, Any]],
    declared_params: Dict[str, Any],
    name: str,
    resolved: Any,
    *,
    keys: Optional[Iterable[str]] = None,
    cli_override: bool = False,
    config_origin: str = SOURCE_DUMP,
    aliases: Iterable[str] = (),
) -> None:
    """Record one resolved parameter's provenance triple into `entries`."""
    if keys is None:
        keys = [f"{prefix}_{name}" for prefix in _BLEND_PARAM_PREFIXES] + [name]
    declared_keys = list(keys) + [k for k in aliases if k not in keys]
    declared, source = _effective_param_source(
        declared_params, declared_keys, cli_override=cli_override, config_origin=config_origin
    )
    entries[name] = param_entry(declared, resolved, source)


def _effective_params_config_origin(resolved_desc: Optional[str]) -> str:
    """Classify where a declared param came from: dump vs `strategies_config.json`.

    A static template's injected `param_grid` defaults are DEFAULTS, not a config
    artifact, so they report `default`.
    """
    desc = str(resolved_desc or "")
    if desc.startswith("strategies_config.json:"):
        return SOURCE_CONFIG
    if desc.startswith("static_template:"):
        return SOURCE_DEFAULT
    return SOURCE_DUMP


def _write_effective_params(
    output_dir: str,
    payload: Dict[str, Any],
) -> Optional[str]:
    """Write ``<output_dir>/effective_params.json`` atomically; never raise into the run.

    AT8 is an OBSERVATION layer: a capture/write failure must print loudly and let
    the order path continue unchanged. Returns the path, or ``None`` on failure.

    ATOMIC (same pattern as `scripts/repair_live_run_snapshots.write_bytes_atomic`):
    the payload is serialised into ``effective_params.json.tmp`` in the SAME directory,
    flushed/closed, then `os.replace`d onto the final name. A crash mid-write therefore
    leaves at most a ``.tmp`` -- never a truncated ``effective_params.json`` that a
    reader would parse as a complete-but-empty provenance record. A failed replace
    deletes the leftover ``.tmp`` before re-raising for the caller's loud warning.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "effective_params.json")
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
            os.replace(tmp_path, path)
        except BaseException:
            # `KeyboardInterrupt`/`SystemExit` must keep propagating, but a half-written
            # temp file is never a useful artifact -- remove it on the way out.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        print(f"[EFFECTIVE PARAMS] 参数溯源已写入: {path}")
        return path
    except Exception as exc:
        print(
            f"[EFFECTIVE PARAMS] warning: 写入失败，运行继续（参数溯源缺失）: {exc}",
            file=sys.stderr,
        )
        return None


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
    3. Strategy parameter via resolve_blend_param with min_hold_bars (chan_fse_min_hold_bars, etc.)
    4. Strategy parameter matching ending with '_min_holding_bars' or '_min_hold_bars'
    5. Fallback default (2).
    """
    if cli_override is not None:
        return max(0, int(cli_override))
    val = resolve_blend_param(params, "min_holding_bars", None, int)
    if val is not None:
        return max(0, int(val))
    val_short = resolve_blend_param(params, "min_hold_bars", None, int)
    if val_short is not None:
        return max(0, int(val_short))
    for k, v in params.items():
        if (k.endswith("_min_holding_bars") or k.endswith("_min_hold_bars")) and v is not None:
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


def enforce_execution_executability(
    timing: Dict[str, Any],
    clock_ctx: Any,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """(`D3`) Refuse to write a ticket AFTER the open it declares as its fill instant.

    THE DEFECT. `execution_session=as_of+1` / `price_reference=next_open` declare that the
    orders fill at the NEXT session's open. Nothing compared that declared instant with the
    instant the ticket was actually written: the archived runs of 2026-11-09 / 11-10 / 11-11
    were all generated around noon of their own `execution_date`
    (`local_now=2026-11-10T12:50:14+08:00` for `execution_date=2026-11-10`, etc.), i.e. the
    declared next-open reference price had ALREADY expired when the file appeared. The run
    still succeeded, so the archive said "next_open" about an order that could only be read
    as stale.

    DISPOSITION = (A) HARD REFUSE, fail-closed. Deferring instead ((B)) would carry
    `execution_date` to a later open while `price_reference` stayed `next_open` -- a false
    statement about which close the prices came from, and `common/live_execution_timing.py`'s
    frozen vocabulary has no third spelling for "close t-1, fill t+2". So: no ticket, a
    `MissedExecutionOpenError`, and the operator re-runs (which re-prices on the then-latest
    closed session).

    `--allow-intraday` is deliberately NOT consulted here: it only relaxes the designed
    inspection WINDOW (`common/cli_utils.assert_run_window`). Missing the open is a different
    fact and no flag may launder it.

    Returns the annotated timing block (the caller keeps using it); raises when violated.
    The structured audit block is written to `execution_timing.json` BEFORE raising, so the
    refusal itself is auditable in the archive (`executable=false` + `timing_violation` +
    `lateness_minutes` + `disposition=refused`).
    """
    annotated = annotate_executability(timing, getattr(clock_ctx, "clock", None))
    generated_at: Optional[str] = None
    try:
        generated_at = clock_ctx.clock.local_now().isoformat()
    except Exception:
        generated_at = None
    line = executability_log_line(annotated, generated_at)
    print(line)
    violation = annotated.get("timing_violation")
    if not violation:
        return annotated
    # Keep the persisted `timing_log` identical to the printed verdict (the clock's own
    # instant when it is readable, `annotate_executability`'s fallback otherwise).
    annotated["timing_log"] = line
    if output_dir:
        try:
            write_execution_timing(output_dir, annotated)
        except Exception as exc:  # pragma: no cover - audit write must not mask the refusal
            print(f"[WARN] 未能写入 execution_timing.json 审计块: {exc}")
    raise MissedExecutionOpenError(
        f"{line}\n"
        f"票据声明在 {annotated.get('execution_date')} 开盘成交"
        f"（execution_session=as_of+1, price_reference=next_open），但本次运行发生在其后 "
        f"（violation={violation}, lateness_minutes={annotated.get('lateness_minutes')}）。"
        "因此该票的参考价已失效：本轮不出票（fail-closed），请重跑。"
        "`--allow-intraday` 只放宽运行窗口，不放行「已错过开盘」，不能作为绕过。"
    )


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
    #
    # (LV7, frozen contract `docs/lv7_prior_state_contract.md` §5.2) COMPARE BEFORE TAKING.
    # The two expressions below give the LIVE CLASS precedence and then the result is
    # stamped back over `strategy_def["strategy_version"]`, so a dump/class disagreement
    # used to be unobservable (it got overwritten) and the ticket printed a number no
    # persisted artifact agreed with -- exactly how the 2026-11-02 archive ended up with
    # two coexisting version pairs. The parity check runs first and fails closed.
    #
    # LAYERING (LV7 defect B.5, decided 2026-11-06 -- shape A, "keep the check here"):
    # this call therefore fails closed at the PARSING layer, because `parse_args` calls
    # `resolve_strategy_definition` to learn `strategy_key` (the output directory depends
    # on it, so that disk read is unavoidable). The P1.10 rule ("argument parsing stays
    # free of disk-backed fail-fast validation") applies ONLY to
    # `verify_live_strategy_dump_preflight`, which is still called from `main()`; it does
    # NOT apply to the version axis. Do NOT relocate this call into `main()` beside that
    # verifier: by then the two re-stamps four lines below have already overwritten the
    # recorded values, so the check would compare live-vs-live and could never fail.
    # The check MUST stay above those re-stamps. It is also the ONLY version gate on the
    # netted/matrix path (`scripts/run_live_multi_strategy.py` calls this same resolver;
    # its own `verify_live_strategy_dump_preflight` has no version axis).
    assert_dump_version_parity(
        strategy_def,
        getattr(template, "version", None),
        getattr(template, "base_version", None),
        dump_display=resolved_desc,
    )
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


def _cli_flag_given(raw_args: Iterable[str], flag: str) -> bool:
    """True when ``flag`` was actually typed on the command line.

    Recognises both ``--flag value`` and ``--flag=value`` spellings. A parsed value
    cannot answer this: argparse defaults make "typed 2" and "default 2"
    indistinguishable, and the latency flags are aliases of one collapsed value.
    """
    return any(a == flag or a.startswith(flag + "=") for a in raw_args)


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
        "--live-use-policy-kernel",
        action="store_true",
        default=False,
        help=(
            "(M4.3) INERT LEGACY KEY. The `common.position_policy` kernel is the ONLY engine for "
            "the per-asset lifecycle step since M4.4 D3 (`step_position_lifecycle` delegates to "
            "`AssetPolicy.step`; the legacy 6-state body was deleted in BASE_VERSION 2.0.0). "
            "Resolution is still CLI -> strategy params (`live_use_policy_kernel`) -> True, but a "
            "pinned `false` now only emits a one-time removal warning and the kernel runs anyway. "
            "Kept for back-compat with persisted configs. Hydrated symbols with no persisted "
            "lifecycle state are cold-started by the kernel rule (counters zeroed, state "
            "restricted to FLAT/HELD_ACTIVE/COOLDOWN) instead of fabricating "
            "PENDING_ENTRY/HELD_GESTATION."
        ),
    )
    parser.add_argument(
        "--min-entry-threshold",
        type=float,
        default=None,
        help="Minimum target weight threshold for fresh entry (default: inherits trade inertia threshold, e.g. 0.05 / 5%%)",
    )
    parser.add_argument(
        "--liquidate-stale-input-lots",
        action="store_true",
        help=(
            "(D6, 2026-10-29 incident) ENABLE the residual-lot liquidation order. When the model "
            "target for a symbol is 0 and its lifecycle state is NOT a holding state "
            "(FLAT/COOLDOWN/PENDING_ENTRY) while the operator's input book still carries shares, "
            "the row is a STALE INPUT LOT (600584.SH / 600362.SH kept 100-share residuals after "
            "being sold out of the model, and the inertia filter -- |drift| 2.49%% < 5%% -- emitted "
            "HOLD shares:0 forever, so the lot could never be cleared). DEFAULT OFF = warn-only: "
            "the row is annotated + a loud warning is printed, but NO order is emitted, so this "
            "flag cannot change the next live run unless somebody asks. "
            "Kill-switch: LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION=1 disables even when set."
        ),
    )
    parser.add_argument(
        "--max-single-position",
        type=float,
        default=0.20,
        help=(
            "(D7, 2026-10-29 incident) Maximum DELIVERED market-value weight per symbol "
            "(target_shares * price / portfolio_value), default 0.20. The model clamps its raw "
            "weight, but lot rounding can push the delivered value over the cap (observed 20.28%% "
            "and 20.01%%). This value alone is WARN-ONLY: rows over the cap are annotated with "
            "MV_CAP and nothing is changed. Pair it with --enforce-mv-cap to clamp to whole lots. "
            "Kill-switch: LIVETRADING_DISABLE_MV_CAP=1."
        ),
    )
    parser.add_argument(
        "--enforce-mv-cap",
        action="store_true",
        help=(
            "(D7) ENABLE the clamp: after lot rounding, a row whose delivered market-value weight "
            "exceeds --max-single-position is reduced to whole lots at or below the cap "
            "(delta_shares / trade_value / target_weight recomputed, reason annotated MV_CAP). "
            "DEFAULT OFF = warn-only, so this cannot change an order unless explicitly asked."
        ),
    )
    parser.add_argument(
        "--enforce-r1-derisk",
        action="store_true",
        help=(
            "(AT7 P3) ENABLE the R1 intraday de-risk on the sizing path: buy increments are "
            "multiplied by the R1 multiplier (real intraday_buy_scale x R1 multiplier, e.g. "
            "0.50 x 0.25 = 0.125), sells and the cash leg untouched. "
            "DEFAULT OFF = the shadow line only, so this cannot change an order unless "
            "explicitly asked. Kill-switch: LIVETRADING_DISABLE_R1_DERISK=1."
        ),
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
        "--intraday-publish-after",
        default="20:00",
        help=(
            "Market-local wall-clock time after which the as-of session's 5m bars are assumed "
            "published (default 20:00). BaoStock 5m data often only appears after ~20:00 -- a live "
            "observation, not a protocol guarantee. Independent of --publication-grace-minutes (the "
            "DAILY bar rule): it never moves the run window, it only labels a missing 5m layer as "
            "'pending_publication' (wait and rerun) vs 'missing' (sync defect). See "
            "common.intraday_features.INTRADAY_5M_PUBLICATION_AFTER_LOCAL."
        ),
    )
    parser.add_argument(
        "--enable-overnight-derisk",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "`R6` (rule r6.3) overnight de-risking, DEFAULT ON (owner ruling 2026-11-07). At the "
            "derived instant D = session_close_time(market, day) - 45min (half-day aware, market-"
            "local -- never a hard-coded 14:15), decide whether the cross-sectional overnight gap "
            "volatility has spiked above 2x its trailing 252-session median and, if so, multiply "
            "every NON-cash-proxy target weight by --overnight-derisk-ratio (the difference stays "
            "in cash; no renormalisation). The sigma cross-section is the LOADED UNIVERSE for the "
            "market minus the cash proxies (r6.3), never the target book. Insufficient data does "
            "NOT de-risk and does NOT refuse the ticket: it warns loudly and lands an explicit "
            "field on every affected row. See docs/overnight_derisk_judgement_freeze.md."
        ),
    )
    parser.add_argument(
        "--overnight-derisk-ratio",
        type=float,
        default=OVERNIGHT_DERISK_DEFAULT_RATIO,
        help=(
            "Downscale applied to non-cash-proxy target weights when the overnight-de-risk "
            "judgement TRIGGERS (default: 0.5). Must satisfy 0 < ratio < 1: 0.0 would mean "
            "liquidation and >= 1.0 would mean leverage, both outside this rule's semantics."
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
    add_data_provider_cli_args(parser, default_provider="synthetic")
    # (L2.3) --market / --allow-intraday / --strict-market-clock / --strict-freshness /
    # --publication-grace-minutes / --max-probe-attempts: the as-of + run-window gate
    # flags from design §8. Declared through the shared helper so Stage 1, Stage 2 and
    # the multi-strategy netting runner cannot drift apart.
    add_market_clock_cli_args(parser, stage="stage2")
    parsed = parser.parse_args(args)

    # Reconcile guard_latency_bars with backward compatibility overrides
    raw_args = args if args is not None else sys.argv[1:]
    has_guard = _cli_flag_given(raw_args, "--guard-latency-bars")
    has_entry = _cli_flag_given(raw_args, "--entry-confirm-bars")
    has_exit = _cli_flag_given(raw_args, "--exit-confirm-bars")
    has_min_entry = _cli_flag_given(raw_args, "--min-entry-bars")
    # AT8: which latency/tenure flags the operator ACTUALLY typed. Provenance derives
    # `source=override` from this set, never from a parsed value -- an argparse default
    # (e.g. `guard_latency_bars`'s 2) is indistinguishable from a typed 2, and the four
    # latency spellings below all move the ONE collapsed value the runner keeps.
    parsed._explicit_cli_flags = frozenset(
        flag
        for flag, given in (
            ("--guard-latency-bars", has_guard),
            ("--entry-confirm-bars", has_entry),
            ("--exit-confirm-bars", has_exit),
            ("--min-entry-bars", has_min_entry),
            ("--min-holding-bars", _cli_flag_given(raw_args, "--min-holding-bars")),
        )
        if given
    )

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

    # (`R6`, rule r6.3) Validate the overnight-de-risk budget HERE, before any data load or
    # order math: 0.0 would mean liquidation and >= 1.0 leverage, both outside the frozen
    # semantics of a *downscale*. `enabled=False` still validates the ratio, because a budget
    # that cannot be honoured must not sit silently in a config waiting for the next flag flip.
    overnight_derisk_config = OvernightDeriskConfig(
        downscale_ratio=float(parsed.overnight_derisk_ratio),
        enabled=bool(parsed.enable_overnight_derisk),
    )
    parsed._overnight_derisk_config = overnight_derisk_config

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


def resolve_cash_proxy_target_symbols(
    target_symbols: Iterable[str],
    cash_proxy: str,
) -> set[str]:
    """Target keys that count as **cash** for the overnight-de-risk multiplier (`R6`).

    Two *existing* authorities, no new ticker literals invented here:

    1. ``cash_proxy`` -- the run's cash-sweep symbol (``params["cash_proxy"]``, default ``BIL``),
       i.e. exactly the symbol ``model_cash_target`` / the Tier-3 "100% cash sweep" / Phase 4 use;
    2. ``common.allocation_backtester.CASH_PROXY_SYMBOLS`` -- the repo's canonical set of "a symbol
       whose own price already carries the risk-free rate" (T-bill ETF / money-market fund / 1-day
       repo). A China book holding both ``511880.SH`` and ``GC001`` must not have either of them
       halved as if it were risky equity.

    Matching uses the **bare code** (exchange suffix stripped, case folded), because the canonical
    set stores ``511880`` while universes carry ``511880.SH``. The return value is a subset of
    `target_symbols` itself, so it can be passed straight to
    `common.overnight_derisk.apply_overnight_downscale`, whose cash test is a deliberate exact-key
    match (no fuzzy matching that could silently exempt a genuinely risky ticker).
    """
    bare_codes: set[str] = set()
    for sym in [cash_proxy] + sorted(CASH_PROXY_SYMBOLS):
        text = str(sym or "").strip().upper()
        if not text:
            continue
        bare_codes.add(text)
        if "." in text:
            bare_codes.add(text.split(".", 1)[0])
    return {
        sym
        for sym in target_symbols
        if str(sym).strip().upper() in bare_codes
        or str(sym).strip().upper().split(".", 1)[0] in bare_codes
    }


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


def compute_r1_advisory(health_report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """(`AT7` **P2** shadow) The `R1` multiplier *as a diary entry* -- never a sizing input.

    Reads the Stage-1 `intraday_5m_metrics` pair (`hwm_intra` / `dd_trough`) that
    `scripts/check_live_portfolio_health.py` already writes, hands it to
    `common.intraday_drawdown_derisk.evaluate_intraday_drawdown` (the SOLE owner of
    the `-2 / -3 / -4 %` ladder and of the `1.0 / 0.75 / 0.50 / 0.25` multipliers --
    none is re-declared here), and flattens the verdict with
    `common.advisory_report.r1_decision_to_dict`.

    Returns `None` when either reading is absent. `None` is the honest answer, not a
    failure: `R1` itself returns `INSUFFICIENT_DATA` with `drawdown=None` for an
    unmeasurable session, and this wrapper keeps that distinction one level up -- an
    absent shadow line says "there was nothing to shadow", while a fabricated
    multiplier of `1.0` (or a drawdown of `0.0`) would claim "measured, and it was
    fine", which is a far stronger and false statement. Stage-1 writes explicit
    `null`s on its SKIPPED path (`check_live_portfolio_health.py:2737-2738`), so this
    branch is reachable in production, not just in tests.

    `reference_price` is deliberately NOT passed: the self-consistency anchor is the
    previous close / carried prior peak, and Stage-1 does not surface that anchor as
    its own field (`hwm_intra` is already `max(carried peak, session peak)`), so the
    guard cannot be evaluated from the report alone. Guessing an anchor would
    manufacture a verdict; omitting it just leaves that one guard unevaluated.

    Pure: no I/O, no mutation of the report, no module state -- callable twice with the
    same dict and bit-identically repeatable (pinned by
    `pipeline/tests/test_advisory_wiring.py`).
    """
    metrics = (health_report or {}).get("intraday_5m_metrics") or {}
    hwm, dd = metrics.get("hwm_intra"), metrics.get("dd_trough")
    if hwm is None or dd is None:
        return None            # unmeasurable -> no shadow line; never fabricate 0/1
    return r1_decision_to_dict(evaluate_intraday_drawdown(hwm_intra=hwm, dd_trough=dd))


# --------------------------------------------------------------------------- #
# (D6/D7, 2026-10-29 live incident) Delivery-time guards on the generated ticket
# --------------------------------------------------------------------------- #
# FORENSIC CONTEXT (why these live here, not in the strategy/backtest layer):
#   Both defects are about what the ORDER TICKET actually delivers, i.e. they are
#   invisible to the model and to `blend_base.py`:
#
#   D6 -- RESIDUAL LOTS.  600584.SH and 600362.SH were sold out of the model, but the
#   operator's input book kept 100-share residuals.  The lifecycle machine was already
#   FLAT, and the inertia filter compares |target - current| against 5%: a 2.49%-weight
#   residual is below it EVERY day, so the row was emitted as `HOLD` with `shares: 0`
#   forever.  A position the model no longer wants could never be liquidated -- a
#   phantom holding that every downstream reader (NAV, reconciliations, the holdings
#   table) then treated as real.
#
#   D7 -- DELIVERED MARKET-VALUE CAP.  The 20% single-name cap was enforced on the
#   model's RAW weight (`blend_base.py`), but lot rounding happens AFTER that, so the
#   delivered `target_shares * price / portfolio_value` reached 20.28% and 20.01% -- a
#   silent cap breach that no strategy-side check can see.
#
#   PREDICTABILITY RULE FOR BOTH: detection + annotation is unconditional (the condition
#   can never be silent), but the ORDER-changing half is opt-in and OFF by default, and
#   an env kill-switch overrides even that.  Nothing here can change the next live run
#   unless the operator passes the flag.
STALE_LOT_LIQUIDATION_BADGE = "STALE_LOT_LIQUIDATION"
MV_CAP_BADGE = "MV_CAP"
_DISABLE_STALE_LOT_ENV = "LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION"
_DISABLE_MV_CAP_ENV = "LIVETRADING_DISABLE_MV_CAP"
#: (`AT7` P3) Kill-switch for the R1 intraday de-risk enforcement.
_DISABLE_R1_DERISK_ENV = "LIVETRADING_DISABLE_R1_DERISK"
#: Reason prefix for an emitted residual-lot sell (kept verbatim: operators grep it).
STALE_LOT_REASON = "残仓强制清出（已离场标的仍有持股）"
#: Default single-name delivery cap (mirrors the 20% model-side cap).
DEFAULT_MAX_SINGLE_POSITION = 0.20


def _env_truthy(name: str) -> bool:
    """True for the repo's usual truthy spellings (`1/true/yes/on/y/t`)."""
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def stale_lot_liquidation_enabled(requested: Optional[bool] = None) -> bool:
    """Whether a detected stale input lot may produce a SELL (default OFF).

    Two independent gates must both say yes:
      * `--liquidate-stale-input-lots` (the operator's explicit request), and
      * `LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION` NOT set (the kill-switch wins).
    """
    if _env_truthy(_DISABLE_STALE_LOT_ENV):
        return False
    return bool(requested)


def mv_cap_enforcement_enabled(requested: Optional[bool] = None) -> bool:
    """Whether the delivered market-value cap may clamp an order (default OFF).

    Same two-gate shape as `stale_lot_liquidation_enabled`:
    `--enforce-mv-cap` AND not `LIVETRADING_DISABLE_MV_CAP=1`.
    """
    if _env_truthy(_DISABLE_MV_CAP_ENV):
        return False
    return bool(requested)


def r1_derisk_enforcement_enabled(requested: Optional[bool] = None) -> bool:
    """Whether the `R1` intraday de-risk may scale BUY increments (default OFF).

    Third instance of the same two-gate shape (`AT7` P3, user-approved
    2026-11-09), and the precedence is: **env kill-switch > CLI flag > OFF**.

      * `LIVETRADING_DISABLE_R1_DERISK=1` wins over everything -- even over an
        explicit `--enforce-r1-derisk`, so an operator can neutralise a persisted
        invocation without editing it;
      * otherwise the CLI request decides, and `None`/`False` both mean OFF.

    Nothing else in the run reads this: the caller passes the boolean down to the
    one sizing call site (`apply_r1_intraday_derisk`), there is no module state.
    """
    if _env_truthy(_DISABLE_R1_DERISK_ENV):
        return False
    return bool(requested)


def resolve_r1_derisk_target(
    target_series: pd.Series,
    current_series: pd.Series,
    cash_proxy: str,
    *,
    enforced: bool,
    r1_multiplier: Optional[float],
) -> Tuple[pd.Series, bool]:
    """Return `(target_series_after, applied)` for the `AT7` P3 de-risk gate. Pure; no I/O.

    The ONE place the enforcement predicate lives, so a wiring regression (wrong
    flag, wrong series, dropped block) is testable instead of hidden inside
    `main()`. `applied` is `False` -- and the caller's OWN object is returned
    unchanged, not a copy -- unless enforcement was granted AND there is a real
    de-risking multiplier (`< 1.0`); `>= 1.0`, `None` and a non-finite value are
    all no-ops, exactly as in `common.r1_derisk_report.apply_r1_intraday_derisk`.
    Multiplying by a missing multiplier is never "assume 1.0".
    """
    if not enforced or r1_multiplier is None or not float(r1_multiplier) < 1.0:
        return target_series, False
    return (
        apply_r1_intraday_derisk(target_series, current_series, cash_proxy, r1_multiplier),
        True,
    )


def assess_stale_input_lot(
    *,
    current_shares: float,
    model_target: float,
    is_holding: bool,
    lifecycle_action: str,
    enforce: bool = False,
    price: float = 0.0,
) -> Dict[str, Any]:
    """Classify one row as a stale input lot and decide what (if anything) to do.

    INVARIANT (D6): ``current_shares > 0 ∧ model_target == 0 ∧ not is_holding``
    => ``detected`` is True.  Note `model_target` is the RAW strategy target captured
    before the lifecycle machine runs: on the inertia path the machine rewrites the
    target to `state.current_weight` (the residual's own weight), which is precisely why
    the pre-lifecycle value is the only honest witness of "the model wants none of this".

    SCOPE INVARIANT (review finding 1, required): this helper may ONLY be asked about
    rows the lifecycle did NOT already decide to liquidate.  The phantom-lot shape is
    always *"state FLAT/COOLDOWN + a HOLD row"* -- it is never a lifecycle-emitted
    SELL.  The risk-exit paths in ``common/position_lifecycle.py`` (hard stop / ATR
    trailing stop / Tier-3 emergency halt / normal exit confirmation) all set
    ``state = COOLDOWN`` *before* returning the ``SELL``, so after the call
    ``state.is_holding()`` is False and ``current_shares > 0`` -- shape-identical to a
    residual lot.  Feeding such a row in here used to append the stale-lot sentence
    ("惰性阈值会永久挡住清出，未启用 --liquidate-stale-input-lots 故本行不下单") to a row that
    *does* place an order, and under ``--liquidate-stale-input-lots`` it prefixed the
    exit reason with `STALE_LOT_REASON`, so anyone grepping the badge to reconcile
    residual-lot cleanups also matched stop-loss / circuit-breaker exits and real risk
    events became unsearchable.  ``lifecycle_action`` is therefore REQUIRED and a
    liquidation event is rejected loudly instead of being silently mislabelled.

    Returns a dict that is BOTH the audit annotation and (when enforced) the order
    instruction, so the caller cannot annotate one thing and trade another::

        {"detected": bool, "enforced": bool, "badge": "STALE_LOT_LIQUIDATION",
         "shares": int, "model_target": float,
         "action": "SELL"|None, "target_shares": int|None, "delta_shares": int|None,
         "trade_value": float|None, "reason": str|None}

    Detection alone never changes an order: `enforced` is False and `action` is None.
    """
    if is_liquidation_event(lifecycle_action):
        raise ValueError(
            "assess_stale_input_lot() must never be applied to a row the lifecycle already "
            f"decided to liquidate (lifecycle_action={lifecycle_action!r}). A lifecycle SELL "
            "is a real risk event (stop-loss / trailing stop / circuit breaker / confirmed "
            "exit) and must keep its own reason verbatim; the phantom-lot shape is always a "
            "HOLD row in a non-holding state. Gate the call site with "
            "common.position_lifecycle.is_liquidation_event() first."
        )
    shares = int(round(float(current_shares or 0.0)))
    target = float(model_target or 0.0)
    detected = bool(shares > 0 and not is_holding and target <= 1e-6)
    enforced = bool(detected and enforce)
    if not detected:
        return {
            "detected": False,
            "enforced": False,
            "badge": STALE_LOT_LIQUIDATION_BADGE,
            "shares": max(0, shares),
            "model_target": target,
            "action": None,
            "target_shares": None,
            "delta_shares": None,
            "trade_value": None,
            "reason": None,
        }
    px = float(price or 0.0)
    if enforced:
        reason = (
            f"{STALE_LOT_REASON} [{STALE_LOT_LIQUIDATION_BADGE}: 卖出 {shares} 股"
            f"（模型目标 0，生命周期非持仓）]"
        )
    else:
        reason = (
            f"[残仓告警 {STALE_LOT_LIQUIDATION_BADGE}: 输入账本仍有 {shares} 股，"
            f"而模型目标为 0 且生命周期非持仓；惰性阈值会永久挡住清出，"
            f"未启用 --liquidate-stale-input-lots 故本行不下单]"
        )
    return {
        "detected": True,
        "enforced": enforced,
        "badge": STALE_LOT_LIQUIDATION_BADGE,
        "shares": shares,
        "model_target": target,
        "action": "SELL" if enforced else None,
        "target_shares": 0 if enforced else None,
        "delta_shares": -shares if enforced else None,
        "trade_value": (-shares * px) if enforced else None,
        "reason": reason,
    }


def market_value_weight(quantity: float, price: float, portfolio_value: float) -> float:
    """`quantity * price / portfolio_value`, 0.0 when either denominator is unusable."""
    try:
        q = float(quantity or 0.0)
        p = float(price or 0.0)
        pv = float(portfolio_value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if pv <= 0 or p <= 0:
        return 0.0
    return (q * p) / pv


def apply_market_value_cap(
    row: Dict[str, Any],
    *,
    portfolio_value: float,
    max_weight: float = DEFAULT_MAX_SINGLE_POSITION,
    lot_size: int = 100,
    enforce: bool = False,
    tolerance: float = 1e-6,
) -> Dict[str, Any]:
    """Flag (and, when enforced, clamp) one ticket row against the DELIVERED MV cap.

    D7 (2026-10-29 incident): `blend_base.py` clamps the model's raw weight, and lot
    rounding afterwards pushes the delivered `target_shares * price / portfolio_value`
    over the cap (observed 20.28% / 20.01%).  This is the only place that sees the
    post-rounding quantity, so the check must live here.

    Behaviour:
      * ALWAYS (warn-only mode included): compute `mv_weight`, store it in
        `detail_reason["mv_cap"]`, and append a `MV_CAP` badge to `reason` /
        `summary_reason` when the cap is breached -- a breach can never be silent;
      * ONLY with `enforce=True`: for a row that still BUYS (`delta_shares > 0`), reduce
        the delta to whole lots so the delivered weight is at or below the cap, then
        recompute `target_shares` / `trade_value` / `target_weight` / `delta_weight`.
        A row that is already above the cap without trading (a HOLD of legacy drift) is
        annotated but never sold down here -- this guard is not an exit rule.

    Disclosed limitation: clamping to WHOLE lots means the post-clamp weight is usually
    slightly BELOW the cap, not exactly at it (e.g. 20.28% -> 17.75% for a 100-share lot
    at 25.35).  Trading a fractional lot to hit 20.00% exactly is not possible.

    The row is mutated in place AND returned (callers use the return value).
    """
    price = float(row.get("price") or 0.0)
    pv = float(portfolio_value or 0.0)
    cap = float(max_weight or 0.0)
    if price <= 0 or pv <= 0 or cap <= 0:
        return row

    target_shares = float(row.get("target_shares") or 0)
    current_shares = float(row.get("current_shares") or 0)
    delta_shares = float(row.get("delta_shares") or 0)
    mv_before = market_value_weight(target_shares, price, pv)
    breached = bool(mv_before > cap + tolerance)
    block: Dict[str, Any] = {
        "market_value_weight": round(mv_before, 6),
        "cap": cap,
        "breached": breached,
        "enforced": False,
        "clamped": False,
    }
    detail = row.get("detail_reason")
    if isinstance(detail, dict):
        detail["mv_cap"] = block

    if not breached:
        return row

    clamped = False
    if enforce and delta_shares > 0:
        lot = max(1, int(lot_size or 1))
        # Whole-lot ceiling for the TOTAL target quantity at the cap.
        cap_shares = np.floor((cap * pv) / price / lot) * lot
        allowed_delta = np.floor((cap_shares - current_shares) / lot) * lot
        new_delta = int(max(0, allowed_delta))
        if new_delta < int(delta_shares):
            row["delta_shares"] = new_delta
            row["shares"] = abs(new_delta)
            row["target_shares"] = int(round(current_shares + new_delta))
            row["trade_value"] = new_delta * price
            row["trade_value_rmb"] = row["trade_value"]
            row["target_weight"] = market_value_weight(row["target_shares"], price, pv)
            row["delta_weight"] = row["target_weight"] - float(row.get("current_weight") or 0.0)
            clamped = True
            block["pre_clamp_market_value_weight"] = round(mv_before, 6)
            block["post_clamp_market_value_weight"] = round(row["target_weight"], 6)
            block["clamped_delta_shares"] = new_delta
            if isinstance(detail, dict):
                if isinstance(detail.get("lot_rounding"), dict):
                    detail["lot_rounding"]["shares"] = new_delta
                if isinstance(detail.get("inertia_check"), dict):
                    detail["inertia_check"]["delta_weight"] = round(row["delta_weight"], 4)

    block["clamped"] = clamped
    block["enforced"] = bool(enforce)
    if clamped:
        _cap_tail = f"，已按整手回收至 {block['post_clamp_market_value_weight']:.2%}"
    elif enforce:
        _cap_tail = "，本行无买入增量可回收（非加仓），仅标注"
    else:
        _cap_tail = "，未启用 --enforce-mv-cap 故不改单"
    badge_note = (
        f"[市值权重超限 {MV_CAP_BADGE}: 交付市值权重 {mv_before:.2%} > 上限 {cap:.2%}{_cap_tail}]"
    )
    for key in ("reason", "summary_reason"):
        text = str(row.get(key) or "")
        if MV_CAP_BADGE not in text:
            row[key] = f"{text} {badge_note}".strip()
    return row


# ---------------------------------------------------------------------------
# (D4, evidence `runs/2026-11-09/204956`) Delivered-weight accounting
# ---------------------------------------------------------------------------
#
# THE DEFECT THIS CLOSES.  A ticket row states a MODEL target weight (`target_weight`)
# and a WHOLE-LOT quantity (`target_shares`) that cannot express it, and nothing bridged
# the two:
#
#   * 600660.SH: `target_weight = 0.2`, `detail_reason.lot_rounding.target_shares_raw =
#     299.04`, `shares = 200` -> the row delivers `200 * 56.62 / NAV = 17.17%`, but the
#     ticket still printed "目标: 20.0%" and `account_state.lifecycle_states` persisted
#     `current_weight = 0.2`.  The next round's inertia filter / min-entry filter /
#     single-name cap therefore all compared against a weight the book can never hold.
#   * the whole ticket summed to `Σ target_weight = 1.028337` (BIL row included) while
#     `Σ current_weight = 1.000000` -- i.e. the generated book claimed 102.8% of NAV.
#   * 601919.SH breached the 20% delivered cap (`market_value_weight = 0.211865`) and the
#     breach existed ONLY inside `detail_reason.mv_cap`; the operator had to open the JSON
#     to find it.
#
# THE CONTRACT (three additive fields, no existing meaning changed):
#   * `target_weight_model`    -- the row's target coordinate AFTER the Stage-1 intraday
#                                 gate and the vol cap, AFTER `step_position_lifecycle` /
#                                 the min-entry filter, and BEFORE lot rounding (audit
#                                 only; divide by `equity_scale_factor` to recover the
#                                 pre-cap value).
#   * `target_weight_effective`-- `target_shares * price / NAV`, i.e. what the ticket
#                                 actually delivers; the cash-proxy row carries the NAV
#                                 RESIDUAL (`1 - Σ equity effective`), which is the only
#                                 honest weight a placeholder row with no share quantity
#                                 can have.
#   * ticket top level: `weights_normalized` / `weight_normalization_factor` /
#     `target_weight_sum` / `target_weight_sum_model` / `rounding_rebalanced` /
#     `mv_cap_breaches`.
#     NOTE (2026-11-12 defect B): `target_weight_sum_model` is `normalize_model_target_weights`'
#     Σ over the legs it may rewrite (BUY rows + cash proxy) at the pre-normalization,
#     pre-lot-rounding instant -- NOT the sum of the whole model book.
# `target_weight` itself keeps its pre-D4 meaning (the model/clamped target that sizing
# used), because archived readers and the live UI consume it.
WEIGHT_BUDGET_TOLERANCE = 1e-9
TARGET_WEIGHT_EFFECTIVE_DECIMALS = 8

# ---------------------------------------------------------------------------------------
# (LV18 wiring, 2026-11-12) STAGE-1 BARROSO VOLATILITY SCALING -> CAP ON THE EQUITY BUDGET
#
# `scripts/check_live_portfolio_health.py::evaluate_realized_volatility` has always computed
# `vol_scaling_factor = clip(target_vol / realized_vol_21d)` (Barroso & Santa-Clara 2015,
# target_vol = 0.12), printed it three times and archived it -- but `docs/development_plan.md`
# LV18 registered that it had NO downstream consumer: the number was presented as an applied
# risk action while nothing in Stage 2 was sized by it.
#
# WHY `cap` AND NOT A SECOND MULTIPLICATION (the load-bearing decision):
#   `BaseBlendAllocationStrategy` already applies its OWN Garman-Klass target-vol scaling
#   (`blend_enable_vol_targeting` / `blend_target_vol = 0.12`) to the portfolio, which is why
#   the model's own equity target on 2026-11-11 was 46.3% instead of a fully invested book.
#   Multiplying the two independent estimates of "how much risk to take" (archived 11-11:
#   model equity 0.463, Stage-1 factor 0.370) would ask the book to hold only
#   0.463 * 0.370 = 0.171 of NAV -- i.e. cut risk twice for the same observation of elevated
#   volatility. Stage-1's factor is therefore wired as a CEILING the book may not exceed:
#       equity_budget = min(model_equity_sum, vol_scaling_factor)
#   The strategy's own vol targeting stays the primary sizing engine; Stage-1 can only make
#   the book LESS aggressive, never more. See docs/version.md (2026-11-12 entry, §LV18).
PORTFOLIO_VOL_OVERLAY_MODE_CAP = "cap"
PORTFOLIO_VOL_OVERLAY_MODE_OFF = "off"
PORTFOLIO_VOL_OVERLAY_MODES: Tuple[str, ...] = (
    PORTFOLIO_VOL_OVERLAY_MODE_CAP,
    PORTFOLIO_VOL_OVERLAY_MODE_OFF,
)
#: Allowlist (deliberate): the overlay is wired for `chan_four_state_blend` ONLY, declared in
#: `pipeline/research_strategy/strategies_config.json` under the prefixed keys
#: `cfsb_portfolio_vol_overlay_enabled` / `cfsb_portfolio_vol_overlay_mode`. AGENTS.md is
#: explicit that blend sub-strategy/state resolution must use the strategy-PREFIXED key and
#: never a bare name that can collide with a global `StrategyConfig` field (the 2026-10-21
#: `wq_alpha_id` incident), so the resolver below only reads prefixed spellings.
PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS: Tuple[str, ...] = ("chan_four_state_blend",)
#: Reason code for "Stage-1 worked but produced no MEASURED factor" (short history / stale
#: intraday window). A missing factor must NEVER be read as 1.0x -- that would silently
#: re-enable full risk exactly when the risk input is unknown.
VOL_OVERLAY_UNAVAILABLE_REASON = "vol_unavailable"
#: (LV18 item 2, 2026-11-12) DELIVERY-SHORTFALL GUARDRAIL. The ceiling is expressed in
#: continuous weight space, but the ticket can only deliver whole LOTS. A tiny ceiling
#: (e.g. factor 0.037 on a ~200k account) can round down to almost nothing: the measured
#: replay delivered 0.48% of NAV against a 3.70% ceiling -- 87% of the intended de-risking
#: silently failed to reach the market. Anything below this delivered/capped ratio is
#: reported (`delivery_shortfall`, `vol_overlay_delivery_ok=false`) and printed as a WARN.
#: The guard NEVER enlarges weights and NEVER skips lot rounding to close the gap: the fix
#: is for an operator to see the shortfall, not for the runner to invent exposure.
VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO = 0.5
#: (2026-11-12 defect B) LIFECYCLE-ATTRITION GUARDRAIL. The ceiling is measured on the
#: target VECTOR, but the ticket only carries rows that SURVIVED `step_position_lifecycle`
#: (PENDING_ENTRY confirmation, exit-confirming, inertia/tenure blocks) plus the 5% /
#: minimum-entry filter. On the archived `2026-11-12/162857` ticket that pass removed
#: 12.85pp of a 38.94pp cap (33%) before lot rounding even started, yet the old
#: `delivery_shortfall_ratio` divided by the CAP (`0.2507/0.3894 = 0.64`) and stayed quiet:
#: a third of the de-risking could vanish without a single WARN. This ratio is
#: `post_lifecycle / capped` and WARNS below the threshold (default 0.8, i.e. more than
#: one fifth of the ceiling lost before lots). Report-only, exactly like the delivery leg.
VOL_OVERLAY_LIFECYCLE_SHORTFALL_RATIO = 0.8
#: Reason code stamped into the audit block when a Stage-1 circuit breaker has ALREADY
#: zeroed the risk book before the ceiling is evaluated. Without it the audit reads
#: "capped_equity_sum=0, binding=false" and could be mistaken for the vol cap clearing the
#: book (it did not -- it had nothing left to cap).
VOL_OVERLAY_SUPERSEDED_BY_CIRCUIT_BREAKER = "circuit_breaker_tier3"

#: (D5) Sidecar that makes "an EMPTY book" distinguishable from "no book". Written next to
#: `post_holdings.json` by this runner; read by `web/livetrading/storage.py`
#: (`post_holdings_meta_status`), which keeps the same filename constant.
POST_HOLDINGS_META_FILENAME = "post_holdings.meta.json"


def _row_weight(row: Dict[str, Any], key: str = "target_weight") -> float:
    """`row[key]` as a finite float, 0.0 when absent/unusable (never NaN into JSON)."""
    try:
        value = float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if np.isfinite(value) else 0.0


def _detail_block(row: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """`row["detail_reason"][name]` as a mutable dict, created on demand; None if unusable."""
    detail = row.get("detail_reason")
    if not isinstance(detail, dict):
        return None
    block = detail.get(name)
    if not isinstance(block, dict):
        block = {}
        detail[name] = block
    return block


def normalize_model_target_weights(
    rows: List[Dict[str, Any]],
    *,
    portfolio_value: float,
    cash_proxy: Optional[str] = None,
    lot_size_fn: Optional[Any] = None,
    tolerance: float = WEIGHT_BUDGET_TOLERANCE,
) -> Dict[str, Any]:
    """Scale the model's target weights down when the buy side sums above 100% (D4).

    Runs BEFORE the purchasing-power rescale and the delivered-cap pass, so every later
    stage (lot rounding, cash reconciliation, printing) sees a book that already fits
    inside one NAV.  Proportional: `factor = 1 / Σ`, where `Σ` counts ONLY the weights this
    function is allowed to rewrite: the BUY legs plus the cash-proxy placeholder.

    SCOPE (deliberate, load-bearing): a SELL / HOLD / CANCEL_PENDING row's TARGET is NEVER
    rescaled.  A ticket may not silently turn "sell 300" into "sell 280" (a different
    order), and a freeze/HOLD row's target is the position being carried -- rewriting it
    would break the "market closed => nothing was evaluated" invariant.  Those rows also
    cannot be rescaled in practice: with a stale price the carried weight can legitimately
    exceed 100% (`current_weight = 4.75` in the L2.23 freeze test), and normalizing it would
    manufacture a trade the model never asked for.  If the non-BUY rows keep the book above
    100%, `reconcile_delivered_weight_budget` reports `budget_ok = False` and WARNs instead
    of touching them.

    Pure arithmetic on plain dicts (no I/O, no globals) so it is unit-testable offline.
    Returns the audit summary that ends up at the ticket's top level.
    """
    lot_size_fn = lot_size_fn or resolve_lot_size
    pv = float(portfolio_value or 0.0)
    summary: Dict[str, Any] = {
        "weights_normalized": False,
        "weight_normalization_factor": 1.0,
        "target_weight_sum_model": 0.0,
        "weight_normalization_partial": False,
    }
    if pv <= 0 or not rows:
        return summary

    def _scalable(row: Dict[str, Any]) -> bool:
        if cash_proxy is not None and row.get("symbol") == cash_proxy:
            return True
        return str(row.get("action") or "").strip().upper() == "BUY"

    total = 0.0
    for r in rows:
        if _scalable(r):
            total += _row_weight(r)
    summary["target_weight_sum_model"] = round(total, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
    if not np.isfinite(total) or total <= 1.0 + tolerance:
        return summary

    factor = 1.0 / total
    summary["weights_normalized"] = True
    summary["weight_normalization_factor"] = round(factor, 10)

    for r in rows:
        if not _scalable(r):
            continue
        before = _row_weight(r)
        if before <= 0.0:
            continue
        r["target_weight"] = before * factor
        r["delta_weight"] = r["target_weight"] - _row_weight(r, "current_weight")
        block = _detail_block(r, "weight_budget")
        if block is not None:
            block["normalization_factor"] = summary["weight_normalization_factor"]
            block["target_weight_model"] = round(_row_weight(r, "target_weight_model") or before, 10)
        if cash_proxy is not None and r.get("symbol") == cash_proxy:
            continue
        price = _row_weight(r, "price")
        if price <= 0.0:
            continue
        lot = max(1, int(lot_size_fn(str(r.get("symbol") or "")) or 1))
        target_f = (r["target_weight"] * pv) / price
        new_target = int(np.floor(target_f / lot) * lot)
        new_delta = new_target - int(r.get("current_shares") or 0)
        if new_delta < 0:
            # The scaled target fell below what is already held.  Emitting a SELL was not
            # asked for by the model, so the row becomes a zero-quantity record instead of
            # an order in the opposite direction.
            new_delta = 0
            new_target = int(r.get("current_shares") or 0)
            summary["weight_normalization_partial"] = True
        r["target_shares"] = new_target
        r["delta_shares"] = new_delta
        r["shares"] = int(abs(new_delta))
        r["trade_value"] = new_delta * price
        r["trade_value_rmb"] = r["trade_value"]
        detail = r.get("detail_reason")
        if isinstance(detail, dict):
            if isinstance(detail.get("lot_rounding"), dict):
                detail["lot_rounding"]["shares"] = int(new_delta)
                detail["lot_rounding"]["target_shares_raw"] = round(target_f, 2)
            if isinstance(detail.get("inertia_check"), dict):
                detail["inertia_check"]["delta_weight"] = round(r["delta_weight"], 4)
    return summary


def _as_config_bool(value: Any, default: bool) -> bool:
    """Coerce a JSON/CLI-ish config value to bool, keeping string spellings honest.

    `bool("false")` is `True`, which would make a persisted `"cfsb_portfolio_vol_overlay_enabled":
    "false"` silently ENABLE a risk overlay. Config values here are JSON booleans in practice,
    but the coercion must not depend on that.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def resolve_portfolio_vol_overlay_params(
    strategy_key: Optional[str],
    params: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Resolve `(enabled, mode)` for the Stage-1 volatility overlay.

    Gating order is deliberate:
      1. STRATEGY ALLOWLIST FIRST. Only `chan_four_state_blend` may carry the overlay; every
         other strategy resolves `(False, "off")` regardless of what its params say, so the
         50+ other strategies keep byte-identical behaviour (AGENTS.md: do not change the
         default behaviour of the rest of the fleet).
      2. Then the strategy-PREFIXED keys (`cfsb_portfolio_vol_overlay_enabled` / `_mode`),
         never a bare name -- `resolve_blend_param`'s final fallback is a bare
         `getattr(cfg, name)`, which is exactly the collision class the `wq_alpha_id` incident
         came from. Here the prefixed spelling is spelled out explicitly for this strategy.
      3. Default ENABLED (user requirement, 2026-11-12): a dump that predates the key must not
         silently revert the wiring to inert again.
    """
    if strategy_key not in PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS:
        return False, PORTFOLIO_VOL_OVERLAY_MODE_OFF
    p = params or {}
    enabled = _as_config_bool(p.get("cfsb_portfolio_vol_overlay_enabled"), True)
    mode = str(p.get("cfsb_portfolio_vol_overlay_mode") or PORTFOLIO_VOL_OVERLAY_MODE_CAP).strip().lower()
    if mode not in PORTFOLIO_VOL_OVERLAY_MODES:
        # Unknown spelling: fall back to the conservative-but-active `cap` (a ceiling can
        # only de-risk); never silently switch to "multiply" or "off".
        mode = PORTFOLIO_VOL_OVERLAY_MODE_CAP
    if mode == PORTFOLIO_VOL_OVERLAY_MODE_OFF:
        enabled = False
    return bool(enabled), mode


#: (2026-11-12 AT8 audit) The two config keys this overlay resolves. They were resolved here
#: and CONSUMED by `apply_portfolio_vol_overlay_for_strategy`, but never registered in
#: `effective_param_entries` -- so a ticket carrying `vol_overlay.applied = true` could not be
#: reproduced from its own `effective_params.json`: the snapshot held no overlay key at all,
#: and "why was a cap enabled on this round?" was answerable only by opening the dump.
VOL_OVERLAY_ENABLED_PARAM = "cfsb_portfolio_vol_overlay_enabled"
VOL_OVERLAY_MODE_PARAM = "cfsb_portfolio_vol_overlay_mode"


def resolve_portfolio_vol_overlay_provenance(
    strategy_key: Optional[str],
    params: Optional[Dict[str, Any]],
    *,
    declared_params: Optional[Dict[str, Any]] = None,
    config_origin: str = SOURCE_DUMP,
    cli_override: bool = False,
) -> Dict[str, Any]:
    """`resolve_portfolio_vol_overlay_params` + WHERE each resolved value came from (AT8).

    Resolution itself is NOT re-implemented: the `(enabled, mode)` pair is taken from
    `resolve_portfolio_vol_overlay_params`, so the snapshot can never disagree with the run
    (allowlist-first, prefixed-only keys, unknown spelling -> `cap`, `off` -> disabled).

    `params` is what the resolver reads; `declared_params` (the caller's pre-mutation freeze)
    is what gets reported as `declared`. They coincide for these two keys -- nothing in this
    runner writes them back -- but keeping the two inputs separate makes that an explicit fact
    rather than an assumption.

    SOURCE LABELS use `common/effective_params.py`'s CLOSED vocabulary
    (`default` / `config` / `dump` / `override`), which has no `"allowlist"` member. A strategy
    OFF the allowlist therefore reports `default` for both keys: the effective value does not
    come from that strategy's params at all, it is the gate's own built-in default
    (`enabled=False`, `mode="off"`). `allowlisted=False` plus the echoed `declared_*` values
    make the "why" readable -- a dump that declares `enabled=True` still shows
    `declared=True / resolved=False / source=default`, i.e. declared-but-gated, never
    "the config turned it off".
    """
    declared_src: Dict[str, Any] = declared_params if declared_params is not None else (params or {})
    enabled, mode = resolve_portfolio_vol_overlay_params(strategy_key, params)
    allowlisted = strategy_key in PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS

    def _source(param_name: str) -> str:
        if not allowlisted:
            return SOURCE_DEFAULT
        if cli_override:
            return SOURCE_OVERRIDE
        return config_origin if param_name in declared_src else SOURCE_DEFAULT

    return {
        "enabled": enabled,
        "mode": mode,
        "allowlisted": allowlisted,
        "declared_enabled": declared_src.get(VOL_OVERLAY_ENABLED_PARAM),
        "declared_mode": declared_src.get(VOL_OVERLAY_MODE_PARAM),
        "enabled_source": _source(VOL_OVERLAY_ENABLED_PARAM),
        "mode_source": _source(VOL_OVERLAY_MODE_PARAM),
    }


def record_portfolio_vol_overlay_effective_params(
    entries: Dict[str, Dict[str, Any]],
    declared_params: Dict[str, Any],
    strategy_key: Optional[str],
    *,
    config_origin: str = SOURCE_DUMP,
    cli_override: bool = False,
) -> Dict[str, Any]:
    """Register the two overlay keys as provenance triples; returns the resolution.

    `entries` is the `{param_name: {declared, resolved, source}}` map that
    `build_effective_params` projects into `effective_params.json` -- no key cap and no
    de-duplication beyond plain dict assignment, so these two names are written exactly once
    (a later write for the same name would simply overwrite it, as for every other param).
    """
    provenance = resolve_portfolio_vol_overlay_provenance(
        strategy_key,
        declared_params,
        declared_params=declared_params,
        config_origin=config_origin,
        cli_override=cli_override,
    )
    entries[VOL_OVERLAY_ENABLED_PARAM] = param_entry(
        provenance["declared_enabled"],
        provenance["enabled"],
        provenance["enabled_source"],
    )
    entries[VOL_OVERLAY_MODE_PARAM] = param_entry(
        provenance["declared_mode"], provenance["mode"], provenance["mode_source"]
    )
    return provenance


def resolve_stage1_vol_inputs(health_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read Stage-1's realized volatility / Barroso factor out of the health report.

    Accepts both shapes the report has carried: the nested `volatility_targeting` block and
    the duplicated top-level keys. A factor is only `available` when it is a FINITE POSITIVE
    number measured by Stage 1 -- `vol_degraded` / `vol_scaling_fallback` (the 2026-11-11 D7
    degradation path, where `realized_vol_21d` is NULL and the factor is a neutral 1.0x label
    rather than a measurement) is reported as `vol_unavailable`, never as "1.0x, nothing to
    see". The caller keeps the model weights untouched in that case.
    """
    report = health_report or {}
    nested = report.get("volatility_targeting")
    vt: Dict[str, Any] = nested if isinstance(nested, dict) else {}

    def _get(key: str) -> Any:
        if key in vt and vt[key] is not None:
            return vt[key]
        return report.get(key)

    realized = _get("realized_vol_21d")
    raw_scale = _get("vol_scaling_factor")
    degraded = bool(_get("vol_degraded") or False)
    fallback = _get("vol_scaling_fallback")

    info: Dict[str, Any] = {
        "realized_vol_21d": None,
        "vol_scaling_factor": None,
        "available": False,
        "reason": None,
    }
    try:
        if realized is not None:
            realized_f = float(realized)
            info["realized_vol_21d"] = realized_f if np.isfinite(realized_f) else None
    except (TypeError, ValueError):
        info["realized_vol_21d"] = None

    if degraded or fallback:
        info["reason"] = VOL_OVERLAY_UNAVAILABLE_REASON
        return info
    try:
        scale = float(raw_scale)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        info["reason"] = VOL_OVERLAY_UNAVAILABLE_REASON
        return info
    if not np.isfinite(scale) or scale <= 0.0:
        info["reason"] = VOL_OVERLAY_UNAVAILABLE_REASON
        return info
    info["vol_scaling_factor"] = scale
    info["available"] = True
    return info


def apply_portfolio_vol_ceiling(
    target_series: pd.Series,
    *,
    cash_proxy: Optional[str],
    vol_scaling_factor: Optional[float],
    realized_vol_21d: Optional[float] = None,
    tolerance: float = WEIGHT_BUDGET_TOLERANCE,
    superseded_by: Optional[str] = None,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Apply Stage-1's Barroso factor as a CEILING on the equity weight vector.

    CONTRACT (`mode="cap"`):
        equity_budget = min(Σ equity target weights, vol_scaling_factor)

    The factor is a CAP, never a multiplier. `BaseBlendAllocationStrategy` already applies its
    own Garman-Klass target-vol scaling (`blend_target_vol = 0.12`), which is why the model's
    own equity target on the archived 2026-11-11 round was 0.463 instead of a fully invested
    book; multiplying the two would ask for `0.463 * 0.370 = 0.171` of NAV, i.e. de-risk the
    same elevated-volatility observation twice.

    WHY THIS RUNS ON THE WEIGHT VECTOR AND NOT ON THE BUILT ROWS (the load-bearing decision,
    measured 2026-11-12): a pass over already-built ticket rows cannot de-risk a book that is
    ALREADY invested. `normalize_model_target_weights`' contract forbids inventing a SELL when
    a scaled target falls below the held quantity, and the same rule applied here left the
    delivered book at 78.6% while the row claimed a 3.7% target (`partial=true`) -- i.e. the
    exact "presented as applied, actually inert" shape LV18 exists to kill. Silently emitting
    SELLs from the row pass is worse: it would bypass `step_position_lifecycle`'s `can_sell`
    (China T+1 gestation), the tenure lock and the inertia/normalization gates. Scaling the
    TARGET VECTOR instead is the same place `apply_r1_intraday_derisk` and the Stage-1 linear
    damping / Tier-2 defense already operate, so every downstream gate (lifecycle, tenure,
    lot rounding, purchasing power, delivered MV cap) still applies, and a trim is emitted by
    the machinery that is allowed to emit it. Callers must ALSO set
    `is_portfolio_deleverage=True` when this binds so the tenure lock permits the trim (L2.40).

    Cash is deliberately untouched here: the caller recomputes the implied cash residual
    immediately after (`1 - Σ equity`), so a capped book parks the difference in the cash
    proxy instead of leaving it unallocated.

    A missing / degraded factor is a NO-OP (`applied=False`, `reason="vol_unavailable"`) --
    never a 1.0x multiplier, because an unmeasured volatility is not a calm market.

    ORDERING vs. THE STAGE-1 DEFENSES (Tier-2 recovery damping / linear drawdown damping) --
    the rule is STRICTER WINS, NEVER THE PRODUCT: this function consumes the target vector
    AFTER those defenses have already scaled it, so it can only tighten an already-damped
    book, never re-scale it. A 0.50 damping followed by a 0.37 ceiling yields 0.37 (not
    0.50 x 0.37 = 0.185); a 0.30 damping against a 0.37 ceiling is a no-op (`binding=False`),
    also not a product. If a Tier-3 halt (`TIER_3_HALT`) has already zeroed the book, the
    caller passes `superseded_by="circuit_breaker_tier3"` so the audit says "nothing left to
    cap" instead of crediting the ceiling with clearing the book (`binding` stays `False`,
    `equity_scale_factor` stays `None`).

    Pure function: returns a NEW series plus the audit block (no I/O, no globals).
    """
    summary: Dict[str, Any] = {
        "applied": False,
        "mode": PORTFOLIO_VOL_OVERLAY_MODE_CAP,
        "realized_vol_21d": realized_vol_21d,
        "vol_scaling_factor": vol_scaling_factor,
        # (2026-11-12 defect B) `model_equity_sum` is the Σ equity target of the vector this
        # function RECEIVED -- i.e. AFTER the intraday distribution gate and any Stage-1
        # damping, and BEFORE this cap. It was never "the model's original target"; Stage 2
        # had already applied the intraday gate upstream. `pre_cap_equity_sum` is the same
        # number under an unambiguous name; both are kept so archived readers keep working.
        "model_equity_sum": None,
        "pre_cap_equity_sum": None,
        "capped_equity_sum": None,
        "equity_scale_factor": None,
        "binding": False,
        "reason": None,
        "superseded_by_circuit_breaker": False,
    }
    if target_series is None or len(target_series) == 0:
        summary["reason"] = "no_targets"
        return target_series, summary

    risky_idx = [s for s in target_series.index if cash_proxy is None or str(s) != str(cash_proxy)]
    model_sum = float(
        sum(max(0.0, float(target_series[s])) for s in risky_idx)
    )
    # Recorded even when the factor is unusable: the ticket must still say what the book
    # asked for, so an operator can see that nothing was capped and why. SEMANTICS
    # (2026-11-12 defect B): this is the POST-intraday-gate, POST-Stage-1-damping, PRE-cap
    # equity sum -- NOT the model's original book (see `pre_cap_equity_sum`, the same value
    # under an unambiguous name).
    summary["model_equity_sum"] = round(model_sum, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
    summary["pre_cap_equity_sum"] = summary["model_equity_sum"]
    summary["capped_equity_sum"] = summary["model_equity_sum"]

    if superseded_by:
        # A Stage-1 breaker already emptied the book. There is nothing to cap, and the audit
        # must NOT be readable as "the vol ceiling cleared the book" (`binding=False`,
        # `equity_scale_factor=None`). Deliberately does not touch `is_portfolio_deleverage`.
        summary["superseded_by_circuit_breaker"] = True
        summary["reason"] = str(superseded_by)
        return target_series.copy(), summary

    if vol_scaling_factor is None or not np.isfinite(float(vol_scaling_factor)) or float(vol_scaling_factor) <= 0.0:
        summary["reason"] = VOL_OVERLAY_UNAVAILABLE_REASON
        return target_series.copy(), summary
    summary["applied"] = True
    if model_sum <= 0.0:
        summary["reason"] = "empty_equity_book"
        return target_series.copy(), summary

    budget = min(model_sum, float(vol_scaling_factor))
    summary["capped_equity_sum"] = round(budget, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
    summary["binding"] = bool(budget < model_sum - tolerance)
    if not summary["binding"]:
        # The strategy's own book is already at or below the Stage-1 ceiling.
        return target_series.copy(), summary

    factor = budget / model_sum
    summary["equity_scale_factor"] = round(factor, 10)
    capped = target_series.copy()
    for sym in risky_idx:
        capped[sym] = float(target_series[sym]) * factor
    return capped, summary


def apply_portfolio_vol_overlay_for_strategy(
    target_series: pd.Series,
    *,
    strategy_key: Optional[str],
    params: Optional[Dict[str, Any]],
    health_report: Optional[Dict[str, Any]],
    cash_proxy: Optional[str] = None,
    superseded_by: Optional[str] = None,
) -> Tuple[pd.Series, Optional[Dict[str, Any]]]:
    """Strategy-gated entry point used by `main()`.

    Returns the caller's series UNCHANGED and `None` as the audit block for every
    non-allowlisted strategy AND for a disabled overlay, so their tickets simply do not
    carry a `vol_overlay` key -- that is what keeps the other 50+ strategies byte-identical
    to before this change.
    """
    enabled, mode = resolve_portfolio_vol_overlay_params(strategy_key, params)
    if not enabled:
        return target_series, None
    vol = resolve_stage1_vol_inputs(health_report)
    capped, summary = apply_portfolio_vol_ceiling(
        target_series,
        cash_proxy=cash_proxy,
        vol_scaling_factor=vol["vol_scaling_factor"],
        realized_vol_21d=vol["realized_vol_21d"],
        superseded_by=superseded_by,
    )
    summary["mode"] = mode
    return capped, summary


def evaluate_vol_overlay_delivery(
    capped_equity_sum: Optional[float],
    delivered_equity_sum: Optional[float],
    *,
    post_lifecycle_equity_sum: Optional[float] = None,
    binding: bool = False,
    shortfall_ratio: float = VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO,
    lifecycle_shortfall_threshold: float = VOL_OVERLAY_LIFECYCLE_SHORTFALL_RATIO,
) -> Dict[str, Any]:
    """Compare the CEILING against what the ticket can actually deliver, in TWO legs.

    (2026-11-12 defect B) The original single-leg version divided the delivered equity by
    the CAP and called the result "lot-rounding loss". That was wrong whenever the lifecycle
    machine (or the minimum-entry filter) removed part of the book FIRST: on the archived
    `2026-11-12/162857` ticket the cap was 0.3894, the rows that survived lifecycle summed to
    0.2610, and whole lots delivered 0.2507 -- the old ratio `0.2507/0.3894 = 0.64` cleared
    its 0.5 threshold even though 12.85pp (33% of the ceiling) had already disappeared
    before rounding. This function therefore reports BOTH legs:

      * LIFECYCLE leg -- `lifecycle_shortfall_ratio = post_lifecycle / capped`, flagged
        (`lifecycle_shortfall`) below `lifecycle_shortfall_threshold` (0.8): the ceiling was
        mostly eaten by lifecycle / minimum-entry filtering, not by lots.
      * DELIVERY leg -- `delivery_shortfall_ratio = delivered / post_lifecycle` (the true
        whole-lot loss), flagged below `shortfall_ratio` (0.5). When the caller does not pass
        a lifecycle sum the denominator falls back to the cap, preserving the legacy meaning
        for old callers/tests.

    Contract (unchanged in spirit):
      * not binding, or a zero/absent ceiling -> nothing to measure, all flags False;
      * `delivered > denominator` (lots rounded UP) -> ratio > 1 -> no shortfall (drift is
        already reported by `ceiling_exceeded_by`);
      * `post_lifecycle == 0` with a positive cap -> lifecycle leg flags the total loss and
        the delivery leg is NOT measured (there is no denominator left).

    Pure arithmetic on floats (no I/O) so it is unit-testable offline. Report-only: it never
    rounds up, enlarges a weight, or skips lot rounding to close the gap.
    """
    result: Dict[str, Any] = {
        "delivery_shortfall": False,
        "delivery_shortfall_ratio": None,
        "lifecycle_shortfall": False,
        "lifecycle_shortfall_ratio": None,
        "delivery_ok": True,
        "lifecycle_ok": True,
    }
    try:
        capped = float(capped_equity_sum)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return result
    if not np.isfinite(capped) or capped <= 0.0 or not binding:
        return result

    post_lifecycle: Optional[float]
    try:
        post_lifecycle = (
            float(post_lifecycle_equity_sum)
            if post_lifecycle_equity_sum is not None
            else None
        )
    except (TypeError, ValueError):
        post_lifecycle = None
    if post_lifecycle is not None and not np.isfinite(post_lifecycle):
        post_lifecycle = None

    denominator = capped
    if post_lifecycle is not None:
        lifecycle_ratio = max(0.0, post_lifecycle) / capped
        result["lifecycle_shortfall_ratio"] = round(lifecycle_ratio, 4)
        result["lifecycle_shortfall"] = bool(
            lifecycle_ratio < float(lifecycle_shortfall_threshold)
        )
        result["lifecycle_ok"] = not result["lifecycle_shortfall"]
        if post_lifecycle <= 0.0:
            # Nothing survived the lifecycle pass: there is no delivery to measure, but the
            # lifecycle leg above already reports the full loss.
            return result
        denominator = post_lifecycle

    try:
        delivered = float(delivered_equity_sum)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return result
    if not np.isfinite(delivered):
        return result
    ratio = max(0.0, delivered) / denominator
    result["delivery_shortfall_ratio"] = round(ratio, 4)
    result["delivery_shortfall"] = bool(ratio < float(shortfall_ratio))
    result["delivery_ok"] = not result["delivery_shortfall"]
    return result


def format_vol_overlay_lifecycle_warning(
    capped_equity_sum: float,
    post_lifecycle_equity_sum: float,
    reasons: Optional[Dict[str, Any]] = None,
) -> str:
    """The greppable one-liner for a ceiling mostly eaten by the lifecycle pass."""
    capped_pct = float(capped_equity_sum) * 100.0
    post_pct = float(post_lifecycle_equity_sum) * 100.0
    gap_pp = capped_pct - post_pct
    reason_txt = ""
    if isinstance(reasons, dict) and reasons:
        parts = []
        for code in sorted(reasons):
            block = reasons.get(code) or {}
            try:
                weight = float(block.get("equity_sum") or 0.0) * 100.0
                count = int(block.get("symbols") or 0)
            except (TypeError, ValueError):
                continue
            parts.append(f"{code}: {count} 只 / {weight:.2f}pp")
        if parts:
            reason_txt = "；归因(" + ", ".join(parts) + ")"
    return (
        f"[WARN/VOL OVERLAY] 上限 {capped_pct:.2f}% 但有 {gap_pp:.2f}pp 被生命周期/最低建仓"
        f"过滤掉（过滤后仅剩 {post_pct:.2f}%）{reason_txt} —— 已如实记录，"
        "未放大权重也未放宽任何门槛。"
    )


def format_vol_overlay_delivery_warning(
    capped_equity_sum: float,
    delivered_equity_sum: float,
    post_lifecycle_equity_sum: Optional[float] = None,
) -> str:
    """The greppable one-liner for a capped-but-undeliverable book (`[WARN/VOL OVERLAY]`).

    (2026-11-12 defect B) When `post_lifecycle_equity_sum` is supplied the message names BOTH
    brackets, so the operator can tell a whole-lot loss from lifecycle attrition: the ceiling
    percentage alone used to be read as "lots ate 33%" when lots had only eaten 3pp.
    """
    capped_pct = float(capped_equity_sum) * 100.0
    delivered_pct = float(delivered_equity_sum) * 100.0
    if post_lifecycle_equity_sum is not None:
        post_pct = float(post_lifecycle_equity_sum) * 100.0
        gap_pp = post_pct - delivered_pct
        return (
            f"[WARN/VOL OVERLAY] 上限 {capped_pct:.2f}%（生命周期后 {post_pct:.2f}%）但整手后仅能交付 "
            f"{delivered_pct:.2f}%（整手缺口 {gap_pp:.2f}pp）—— 整手取整吃掉了大部分可交付幅度，"
            "已如实记录，未放大权重也未跳过整手取整。"
        )
    gap_pp = capped_pct - delivered_pct
    return (
        f"[WARN/VOL OVERLAY] 上限 {capped_pct:.2f}% 但整手后仅能交付 "
        f"{delivered_pct:.2f}%（缺口 {gap_pp:.2f}pp）—— 整手取整吃掉了大部分降风险幅度，"
        "已如实记录，未放大权重也未跳过整手取整。"
    )


def reconcile_delivered_weight_budget(
    rows: List[Dict[str, Any]],
    *,
    portfolio_value: float,
    cash_proxy: Optional[str] = None,
    lot_size_fn: Optional[Any] = None,
    tolerance: float = WEIGHT_BUDGET_TOLERANCE,
) -> Dict[str, Any]:
    """Stamp `target_weight_effective` and keep `Σ effective <= 1` (D4).

    Always: every row gets `target_weight_effective = target_shares * price / NAV` (cash
    proxy: the NAV residual).  That is the number the book can actually deliver AFTER
    whole-lot rounding, and the one `account_state.lifecycle_states.current_weight` now
    persists.

    When `Σ effective` still exceeds 1 because a lot rounded UP above its target, whole
    lots are shaved off the SMALLEST delivered positions first (`action == "BUY"` rows with
    a real delta only -- a ticket may not invent a SELL), and `rounding_rebalanced` is set.
    The function is idempotent, so the caller runs it once before the delivered-cap pass
    (where shaving is still allowed) and once after it (to refresh the stamp on rows whose
    quantity the cap just clamped).

    Pure arithmetic on plain dicts (no I/O) so it is unit-testable offline.
    """
    lot_size_fn = lot_size_fn or resolve_lot_size
    pv = float(portfolio_value or 0.0)
    summary: Dict[str, Any] = {
        "rounding_rebalanced": False,
        "target_weight_sum": 0.0,
        "equity_weight_sum": 0.0,
        "cash_weight_effective": None,
        "budget_ok": True,
        "shaved_rows": [],
    }
    if pv <= 0 or not rows:
        return summary

    equity = [r for r in rows if not (cash_proxy is not None and r.get("symbol") == cash_proxy)]
    cash_rows = [r for r in rows if cash_proxy is not None and r.get("symbol") == cash_proxy]

    def eff(row: Dict[str, Any]) -> float:
        return market_value_weight(
            row.get("target_shares"), row.get("price"), pv
        )

    total = float(sum(eff(r) for r in equity))
    guard = 0
    while total > 1.0 + tolerance and guard < 100000:
        guard += 1
        candidates: List[Tuple[float, Dict[str, Any], int]] = []
        for r in equity:
            if str(r.get("action") or "").strip().upper() != "BUY":
                continue
            lot = max(1, int(lot_size_fn(str(r.get("symbol") or "")) or 1))
            if int(r.get("delta_shares") or 0) < lot:
                continue
            candidates.append((eff(r), r, lot))
        if not candidates:
            break
        candidates.sort(key=lambda item: item[0])
        _w, row, lot = candidates[0]
        price = _row_weight(row, "price")
        if price <= 0.0:
            break
        excess_value = (total - 1.0) * pv
        # `- 1e-9` on the lot count, not on the weights: `total - 1.0` is 0.02000000000000002
        # for a 1.02 book, and a bare `ceil` would then shave ONE LOT TOO MANY.
        lots = int(np.ceil(excess_value / (lot * price) - 1e-9))
        lots = max(1, min(lots, int(row.get("delta_shares") or 0) // lot))
        shaved = lots * lot
        row["target_shares"] = int(row.get("target_shares") or 0) - shaved
        row["delta_shares"] = int(row.get("delta_shares") or 0) - shaved
        row["shares"] = int(abs(row["delta_shares"]))
        row["trade_value"] = float(row["delta_shares"]) * price
        row["trade_value_rmb"] = row["trade_value"]
        block = _detail_block(row, "weight_budget")
        if block is not None:
            block["shaved_shares"] = int(block.get("shaved_shares") or 0) + shaved
        detail = row.get("detail_reason")
        if isinstance(detail, dict) and isinstance(detail.get("lot_rounding"), dict):
            detail["lot_rounding"]["shares"] = int(row["delta_shares"])
        summary["rounding_rebalanced"] = True
        summary["shaved_rows"].append(row.get("symbol"))
        total = float(sum(eff(r) for r in equity))

    for r in equity:
        r["target_weight_effective"] = round(eff(r), TARGET_WEIGHT_EFFECTIVE_DECIMALS)
    summary["equity_weight_sum"] = round(total, TARGET_WEIGHT_EFFECTIVE_DECIMALS)

    total_effective = total
    if cash_rows:
        # A cash-proxy row carries no share quantity (`target_shares` is 0 by construction),
        # so the only truthful "delivered weight" it can state is the NAV left over.
        cash_eff = max(0.0, 1.0 - total)
        for r in cash_rows:
            r["target_weight_effective"] = round(cash_eff, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
        total_effective = total + cash_eff
        summary["cash_weight_effective"] = round(cash_eff, TARGET_WEIGHT_EFFECTIVE_DECIMALS)

    summary["target_weight_sum"] = round(total_effective, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
    summary["budget_ok"] = bool(-tolerance <= total_effective <= 1.0 + tolerance)
    return summary


def merge_weight_budget_summaries(
    base: Dict[str, Any], update: Dict[str, Any]
) -> Dict[str, Any]:
    """Fold a second `reconcile_delivered_weight_budget` pass into the first (D4).

    The refresh pass (after the delivered-cap clamp) must not lose the "we had to shave
    lots / normalize weights" verdict the first pass recorded.
    """
    merged = dict(base or {})
    for key in ("rounding_rebalanced",):
        merged[key] = bool((base or {}).get(key)) or bool((update or {}).get(key))
    for key in ("weights_normalized", "weight_normalization_partial"):
        merged[key] = bool((base or {}).get(key)) or bool((update or {}).get(key))
    for key in ("target_weight_sum", "equity_weight_sum", "cash_weight_effective", "budget_ok"):
        if key in (update or {}):
            merged[key] = (update or {}).get(key)
    shaved: List[Any] = []
    for src in (base or {}, update or {}):
        for sym in src.get("shaved_rows") or []:
            if sym not in shaved:
                shaved.append(sym)
    merged["shaved_rows"] = shaved
    return merged


def collect_mv_cap_breaches(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ticket-level summary of every delivered market-value cap breach (D4).

    `apply_market_value_cap` records the verdict inside `detail_reason.mv_cap`; a breach
    that only lives there is invisible to an operator reading the ticket header (the
    2026-11-09 601919.SH case: 21.19% delivered against a 20% cap, undiscovered).
    """
    breaches: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        block = (r.get("detail_reason") or {}).get("mv_cap") or {}
        if not block.get("breached"):
            continue
        breaches.append({
            "symbol": r.get("symbol"),
            "market_value_weight": block.get("market_value_weight"),
            "cap": block.get("cap"),
            "enforced": bool(block.get("enforced")),
            "clamped": bool(block.get("clamped")),
        })
    return breaches


#: Grep-able markers for the machine check below (`D6`).
TICKET_BUY_STDOUT_MARKER = "🟢 BUY "
TICKET_SELL_STDOUT_MARKER = "🔴 SELL "


#: Below this gap the delivered weight and the stated target are the same number with
#: rounding noise (`20.0%` vs `19.9996%`), so no annotation is printed.
DELIVERED_WEIGHT_NOTE_TOLERANCE = 0.001


def _delivered_weight_note(row: Any) -> str:
    """` | 可交付: 17.17% (整手取整后)` when the ticket cannot deliver its stated target.

    (D4) The 2026-11-09 600660.SH row said `目标: 20.0%` while its 200-share lot delivered
    17.17%; the operator had no way to see the gap without doing the arithmetic by hand.
    """
    try:
        eff = row.get("target_weight_effective")
        tgt = float(row.get("target_weight") or 0.0)
        eff_f = float(eff)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(eff_f):
        return ""
    if abs(eff_f - tgt) <= DELIVERED_WEIGHT_NOTE_TOLERANCE:
        return ""
    return f" | 可交付: {eff_f:>5.2%} (整手取整后)"


def print_ticket_order_line(line: str, sink: Optional[List[str]] = None) -> None:
    """`print` one rendered ticket line, keeping a copy for the parity check (D6)."""
    print(line)
    if sink is not None:
        sink.append(line)


def verify_ticket_stdout_order_parity(
    orders: Iterable[Any],
    stdout_text: Optional[str],
    *,
    action: str = "BUY",
    marker: str = TICKET_BUY_STDOUT_MARKER,
) -> Dict[str, Any]:
    """Machine check: one printed order line per ticket order of `action` (D6).

    2026-11-09/204956 evidence: PHASE 2 printed `🟢 BUY 600660.SH ... 200 股` TWICE while
    the ticket carried one order -- an operator copying the stdout would have doubled the
    position.  The rendering loop had two `print()` calls per row (the second one existed
    only to attach the sub-lot warning), so the invariant "orders == printed lines" is now
    asserted instead of assumed.

    Pure text/count comparison (no I/O), so it can be run against any archived
    `summary.json::deploy_stdout` as well as against a live run's captured lines.
    """
    wanted = str(action or "").strip().upper()
    n_orders = 0
    for o in orders or []:
        getter = getattr(o, "get", None)
        if not callable(getter):
            continue
        if str(getter("action") or "").strip().upper() == wanted:
            n_orders += 1
    n_lines = sum(1 for line in str(stdout_text or "").splitlines() if marker in line)
    result = {
        "action": wanted,
        "marker": marker,
        "orders": n_orders,
        "printed_lines": n_lines,
        "parity": n_orders == n_lines,
    }
    if not result["parity"]:
        print(
            f"[WARN/TICKET STDOUT PARITY] {wanted}: 票据 {n_orders} 条 vs 标准输出 "
            f"{n_lines} 行 (marker={marker!r}) -- 照 stdout 抄单可能重复或漏单。"
        )
    return result


def summarize_intraday_5m_loading(
    universe_5m: Optional[Dict[str, Any]],
    symbols: Iterable[str],
    *,
    provider_label: Optional[str],
    cutoff_time: str,
    expected_last_session: Optional[Any] = None,
    market: str = "china",
    now: Optional[Any] = None,
    publish_after: str = "20:00",
) -> List[str]:
    """Report what the 5m layer ACTUALLY delivered, with the attribution it can prove.

    (2026-11-11, `六` item 6) The zero-row branch used to print
    ``常见原因：provider does not support 5m (该数据源仅支持日线 interval='1d')``. That
    diagnosis is FALSE: the live run used a 5m-capable provider and still saw zero rows,
    because the symbols had no 5m coverage in the provider OR in the DuckDB fallback (and,
    in the sibling failure, because the cache stopped a session earlier than the daily
    data). The misleading sentence is replaced by the two causes that are actually
    distinguishable from the frames themselves:

      * ``该 provider 无该标的 5m 行`` -- the caller's provider offered no rows and the
        fallback chain (other providers in the DuckDB cache / parquet) had none either.
        When a fallback DID serve some symbols its ``fallback_from -> fallback_to``
        (and ``fallback_reason``) is printed, so "who served this" is never inferred
        from timestamps;
      * ``缓存陈旧 (lag=N 天)`` -- rows exist but the newest one predates the expected
        session, measured by `common.intraday_features.assess_intraday_5m_freshness`
        (never re-derived here).

    The success line additionally carries `provider` / `source` / `fallback_reason` /
    `stale` / `lag_calendar_days`, which is what makes a silently frozen 5m layer visible
    where the old line only printed a timestamp.

    Pure formatting over `df.attrs`: no I/O, no fetch, no calendar guessing.
    """
    symbol_list = list(symbols)
    total = len(symbol_list)
    frames = universe_5m or {}
    ok = [
        df
        for df in frames.values()
        if df is not None and not bool(getattr(df, "empty", True))
    ]
    n_ok = len(ok)

    providers: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    fallback_reasons: Dict[str, int] = {}
    chains: Dict[str, int] = {}
    n_stale = 0
    lags: List[int] = []
    stale_evidence = False
    for df in ok:
        attrs = getattr(df, "attrs", None) or {}
        if attrs.get("intraday_5m_provider"):
            key = str(attrs["intraday_5m_provider"])
            providers[key] = providers.get(key, 0) + 1
        if attrs.get("intraday_5m_source"):
            key = str(attrs["intraday_5m_source"])
            sources[key] = sources.get(key, 0) + 1
        if attrs.get("fallback_reason"):
            key = str(attrs["fallback_reason"])
            fallback_reasons[key] = fallback_reasons.get(key, 0) + 1
        if attrs.get("fallback_from") or attrs.get("fallback_to"):
            key = f"{attrs.get('fallback_from') or '?'}->{attrs.get('fallback_to') or '?'}"
            chains[key] = chains.get(key, 0) + 1
        if attrs.get("intraday_5m_stale") is not None:
            stale_evidence = True
            if attrs.get("intraday_5m_stale") is True:
                n_stale += 1
        lag = attrs.get("intraday_5m_lag_calendar_days")
        if isinstance(lag, int) and not isinstance(lag, bool):
            lags.append(int(lag))

    def _joined(counts: Dict[str, int]) -> str:
        if not counts:
            return "n/a"
        return ", ".join(f"{k}x{v}" for k, v in sorted(counts.items()))

    if n_ok == 0:
        fallback_text = (
            f"回退链 {provider_label or '<no provider>'} -> DuckDB 缓存（其它 provider） -> parquet"
            if provider_label
            else "回退链 DuckDB 缓存（其它 provider） -> parquet"
        )
        return [
            f"[WARN] 5m 日内特征不可用：{total} 个标的的 5m 数据帧全部为空 "
            f"(provider={provider_label or 'n/a'}, 截断时点={cutoff_time})。"
            f"归因：该 provider 无这批标的的 5m 行（{fallback_text} 亦无该标的的行）。"
            "（旧文案把零行归因为『该数据源不支持 5m，仅支持日线 interval=1d』，此判断已被证伪："
            "支持 5m 的 provider 同样会零行；逐标的的回退/缓存告警见上方 5m 加载日志。）"
            "后续 intraday_vwap / vwap_premium_pct / chan_5m_state 等日内字段将为空值，"
            "Stage-2 仅按日线信号生成工单；请勿把本行理解为日内特征已生效。"
        ]

    try:
        last_5m_ts = max(
            pd.Timestamp(df.index.max()) for df in ok
        )
    except (TypeError, ValueError):
        last_5m_ts = None

    stale_flag: Optional[bool] = (n_stale > 0) if stale_evidence else None
    lag_value: Optional[int] = max(lags) if lags else None
    expected_session = None
    if expected_last_session is not None:
        try:
            expected_session = pd.Timestamp(expected_last_session).date().isoformat()
        except (TypeError, ValueError):
            expected_session = None
        # Frames loaded WITHOUT `expected_last_session` carry no stale/lag attrs; measure
        # the aggregate then, through the shared owner, instead of guessing "fresh".
        if not stale_evidence and not lags:
            try:
                from common.intraday_features import assess_intraday_5m_freshness

                freshness = assess_intraday_5m_freshness(
                    {s: d for s, d in frames.items() if d is not None},
                    expected_last_session=expected_last_session,
                    market=market,
                )
                stale_flag = bool(freshness.get("stale"))
                lag_value = freshness.get("lag_calendar_days")
            except Exception:  # diagnostics must never break the run
                pass

    lines = [
        f"[INFO] 5m 日内特征已聚合: 有效标的数 {n_ok}/{total} "
        f"(截断时点: {cutoff_time}, 最新 5m K 线: {last_5m_ts}, "
        f"provider={_joined(providers)}, source={_joined(sources)}, "
        f"fallback_reason={_joined(fallback_reasons)}, "
        f"stale={stale_flag}, lag_calendar_days={lag_value})"
    ]
    if chains:
        lines.append(
            f"[WARN] 5m 数据来源回退：{_joined(chains)}（reason={_joined(fallback_reasons)}）-- "
            "这些标的的日内特征由回退 provider 提供，不等于所配置 provider 已交付 5m 数据。"
        )
    if stale_flag:
        # (2026-11-12) Distinguish "the provider has not published the day's 5m bars yet"
        # (BaoStock: usually after ~20:00) from "the cache is frozen". Both mean the 5m
        # features are unusable for this run, so neither is relaxed -- only the operator
        # action differs: wait and rerun vs. fix the sync.
        pending_after: Optional[str] = None
        publication_missing = False
        try:
            from common.intraday_features import assess_intraday_5m_publication_state

            _pub = assess_intraday_5m_publication_state(
                {s: d for s, d in frames.items() if d is not None},
                as_of=expected_session or expected_last_session,
                market=market,
                now=now,
                publish_after=publish_after,
            )
            if _pub.get("status") == "pending_publication":
                pending_after = str(_pub.get("expected_after") or publish_after)
            elif _pub.get("status") == "missing":
                publication_missing = True
        except Exception:  # diagnostics must never break the run
            pending_after = None
        if pending_after:
            lines.append(
                f"[WARN] 5m 当日数据尚未发布（BaoStock 通常 {pending_after} 后可用）；本次运行将不使用 5m 特征"
                f"（最新 5m K 线未达期望会话 {expected_session or expected_last_session}，"
                f"lag_calendar_days={lag_value}）。5m 依赖型门禁建议 {pending_after} 后重跑；"
                "本次仅按日线信号生成工单。"
            )
        else:
            lines.append(
                f"[WARN] 5m 缓存陈旧：最新 5m K 线未达期望会话 {expected_session or expected_last_session} "
                f"(stale=True, lag_calendar_days={lag_value})。"
                "日内特征不代表当日，请勿据此判断日内状态；需刷新 5m 缓存后重跑。"
                + (
                    f"已过 {publish_after} 仍缺当日 5m，需检查同步。"
                    if publication_missing
                    else ""
                )
            )
    if n_ok < total:
        missing = sorted(
            str(s)
            for s in symbol_list
            if s not in frames or frames.get(s) is None or bool(getattr(frames.get(s), "empty", True))
        )
        lines.append(
            f"[WARN] 以下 {len(missing)} 个标的缺少 5m 数据，日内字段为空: "
            f"{', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}"
        )
    return lines


def main(cli_args: Optional[List[str]] = None):
    args = parse_args(cli_args)
    # (`R6`, rule r6.3) The overnight-de-risk budget, already validated inside `parse_args`
    # (0 < ratio < 1). Read it here as one object so the integration site cannot re-derive a
    # different default than the CLI documents.
    overnight_derisk_config: OvernightDeriskConfig = args._overnight_derisk_config

    # (P1.10 / 2026-10-24) Pre-flight consistency check between the resolved strategy dump and
    # strategies_config.json. MOVED here from `parse_args()` so argument parsing stays free of
    # this side effect (Stage 1 and the multi-strategy runner already gate in `main()`), and it
    # now runs exactly once, at the start of the run.
    # SCOPE (LV7 defect B.5, 2026-11-06): "argument parsing stays free of disk-backed
    # fail-fast validation" holds for THIS gate (the weighting-mode axis) only. The VERSION
    # axis is deliberately a parsing-layer gate: `parse_args` calls
    # `resolve_strategy_definition` (it must -- `output_dir` is derived from the resolved
    # `strategy_key`), and that function compares the dump's recorded version fields against
    # the live class BEFORE re-stamping them. Moving that comparison here would be vacuous
    # (it would read the already-re-stamped dict) and would drop the only version guard on
    # the netted/matrix runner. See the layering note at the comparison site.
    # INVARIANT: this must stay ABOVE every write in this function -- the run has to fail
    # BEFORE any ticket artifact exists -- so do NOT move it below `os.makedirs(...)` or the
    # ticket construction; a "fail-before-ticket" regression is what P1.10 exists to prevent.
    # The exemption for genuine static templates lives INSIDE the verifier
    # (`is_static_template_exempt`); there is deliberately no name-only gate here any more --
    # the bare name test skipped `dual_momentum`, a static template name that is ALSO a
    # `strategies_config.json` key (P1.9).
    verify_live_strategy_dump_preflight(
        strategy_def=args._strategy_def,
        strategy_key=args._strategy_key,
        strategy_file=args.strategy_file or args._resolved_desc,
        allow_override=getattr(args, "allow_weighting_mode_override", False),
    )

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
    # (LV2) Resolve the execution-timing block from the SAME clock context the window gate
    # used. The two enums are frozen (`as_of+1` / `next_open`, 2026-11-03 user ruling): an
    # in-window intraday run still values on the last CLOSED session, so it is never a
    # same-session execution. What varies is only the header provenance / gap hint.
    timing = resolve_execution_timing(clock_ctx, as_of=clock_ctx.as_of_date, run_mode=run_mode)
    print(f"[EXECUTION TIMING] {timing['header_text']}")
    print(f"[EXECUTION TIMING] {timing['overnight_gap_hint']}")
    # (D3) Compare the DECLARED fill instant (`execution_date` open) with the real generation
    # instant. A run that is already past that open (or past the close) cannot produce a
    # truthful `as_of+1` / `next_open` ticket, so it fails closed HERE -- before any data
    # load, any order math and any ticket write. Replay runs are exempt inside
    # `annotate_executability` (there is no live fill to be late for).
    timing = enforce_execution_executability(timing, clock_ctx, args.output_dir)
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
    # ---- AT8: save the DECLARED params before anything mutates them in place ----
    # `params` IS `strategy_def["params"]`, and the resolution block below writes
    # `params["min_entry_bars"]` / `params["guard_latency_bars"]` back into it. Reading
    # "declared" after that would report the RESOLVED value as declared. Freeze a shallow
    # copy NOW; this is the only mandatory ordering constraint of the provenance layer.
    declared_params = dict(params)
    effective_param_entries: Dict[str, Dict[str, Any]] = {}
    _effective_origin = _effective_params_config_origin(resolved_desc)
    _effective_dump_sha = effective_params_dump_sha256(resolved_desc)
    # No run id is minted anywhere in this runner, so derive a deterministic one from the
    # SAME clock context the run stamps into summary.json (`<as_of>_<local HHMMSS>`).
    try:
        _run_hhmmss = clock_ctx.clock.local_now().strftime("%H%M%S")
    except Exception:
        _run_hhmmss = clock_ctx.clock.stamp().local_now.strftime("%H%M%S")
    effective_run_id = f"{as_of_date}_{_run_hhmmss}"
    strat_name = (
        strategy_def.get("strategy_name")
        or strategy_def.get("research_strategy_spec", {}).get("entry_data", {}).get("name")
        or template_name.replace("_", " ").title()
    )
    cash_proxy = params.get("cash_proxy", "BIL")
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "cash_proxy",
        cash_proxy,
        keys=("cash_proxy",),
        config_origin=_effective_origin,
    )

    # Load account state (if available) for NAV or circuit breakers
    account_state = {}
    if args.account_state_file and os.path.exists(args.account_state_file):
        try:
            with open(args.account_state_file, "r", encoding="utf-8") as f:
                account_state = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to read account state from {args.account_state_file}: {e}")

    # ---- (D2) `prior_state` must be the PREVIOUS session's CONFIRMED snapshot ----------
    # The 2026-11-09 archive carried `prior_state.as_of_date = 2026-11-05` while a SUCCESSFUL
    # 2026-11-06 snapshot (`2026-11-06/185016/`) existed, so the lifecycle/streak/entry-price
    # baseline resumed from a two-session-old book (a FAILED 11-06 deploy and an orphan 11-08
    # copy of the 11-05 ledger were the only other candidates). Resolve the snapshot HERE from
    # the archive itself instead of trusting whatever block the carried state happens to hold:
    # newest date, newest SUCCESSFUL instance within it, snapshot's own `as_of_date` equal to
    # its run date, and `base_version` equal to today's live engine. The decision (and every
    # skipped candidate with its reason) lands in `account_state["prior_state_source"]`, and
    # the line is printed for `summary.json`'s `deploy_stdout` / `prior_state_log`.
    prior_state_log: Optional[str] = None
    try:
        _prior_selection = select_prior_state(
            derive_session_root(args.output_dir),
            as_of_date,
            expected_base_version=(
                getattr(template, "base_version", None) or strategy_def.get("base_version")
            ),
        )
        prior_state_log = apply_prior_state_selection(account_state, _prior_selection)
        if prior_state_log:
            print(prior_state_log)
    except Exception as exc:  # a resolver failure must not eat the carried state
        print(f"[WARN] prior_state 解析失败（保留既有 prior_state）: {exc}")

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

    # ---- AT8 provenance: the values just re-resolved, against the frozen declared dict ----
    # `source` keys off the flags the operator ACTUALLY typed (`parse_args` records them),
    # never off a parsed value: `--guard-latency-bars` / `--entry-confirm-bars` /
    # `--exit-confirm-bars` / `--min-entry-bars` all collapse into the same guard latency
    # (the resolution chain just above), so any of them that was typed can override both keys.
    _explicit_cli_flags = getattr(args, "_explicit_cli_flags", frozenset())
    _latency_flag_typed = any(
        flag in _explicit_cli_flags
        for flag in (
            "--guard-latency-bars",
            "--entry-confirm-bars",
            "--exit-confirm-bars",
            "--min-entry-bars",
        )
    )
    # A typed latency flag is only an OVERRIDE when its value is the one that survived:
    # `parse_args` folds `--entry-confirm-bars`/`--exit-confirm-bars` into
    # `args.guard_latency_bars`, but the chain just above prefers `args.min_entry_bars` when
    # it is set, and lets a dump `guard_latency_bars` / `*_min_entry_bars` pin outrank a
    # typed `--guard-latency-bars` / `--entry-confirm-bars` / `--exit-confirm-bars`.
    # Reporting `override` there would assert an override that never applied
    # (declared == resolved). The floor mirrors `resolve_min_entry_bars`'s `max(1, ...)`.
    if "--min-entry-bars" in _explicit_cli_flags:
        _cli_latency = max(1, int(args.min_entry_bars))
    elif _latency_flag_typed:
        _cli_latency = max(1, int(getattr(args, "guard_latency_bars", 2)))
    else:
        _cli_latency = None
    _latency_cli_override = (
        _cli_latency is not None and int(guard_latency_bars) == _cli_latency
    )
    # `--min-holding-bars` has no sibling spelling and its resolver checks the CLI FIRST,
    # so a typed flag always wins there -- presence is the honest test.
    _min_holding_cli_override = "--min-holding-bars" in _explicit_cli_flags
    # `min_holding_bars` resolution order (see `resolve_min_holding_bars`): prefixed
    # `*_min_holding_bars` -> bare -> prefixed/`*_min_hold_bars` suffix scan -> default.
    _min_holding_keys: List[str] = [
        f"{prefix}_min_holding_bars" for prefix in _BLEND_PARAM_PREFIXES
    ] + ["min_holding_bars"]
    _min_holding_keys += [
        f"{prefix}_min_hold_bars" for prefix in _BLEND_PARAM_PREFIXES
    ] + ["min_hold_bars"]
    _min_holding_keys += [
        k
        for k in declared_params
        if (k.endswith("_min_holding_bars") or k.endswith("_min_hold_bars"))
        and k not in _min_holding_keys
    ]
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "min_holding_bars",
        min_holding_bars,
        keys=_min_holding_keys,
        cli_override=_min_holding_cli_override,
        config_origin=_effective_origin,
    )
    # `guard_latency_bars` / `min_entry_bars` collapse to ONE resolved value (the chain
    # above) but are recorded under both names an operator may search for. Declared is the
    # first of the two spellings present in the frozen dict; `_latency_cli_override` is true
    # only when a typed latency flag supplied the value that survived the chain.
    _entry_keys: List[str] = [
        f"{prefix}_min_entry_bars" for prefix in _BLEND_PARAM_PREFIXES
    ] + ["min_entry_bars"]
    _entry_keys += [
        k
        for k in declared_params
        if k.endswith("_min_entry_bars") and k not in _entry_keys
    ]
    _entry_cli_override = _latency_cli_override
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "guard_latency_bars",
        guard_latency_bars,
        keys=("guard_latency_bars",) + tuple(_entry_keys),
        cli_override=_entry_cli_override,
        config_origin=_effective_origin,
    )
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "min_entry_bars",
        min_entry_bars,
        keys=("min_entry_bars", "guard_latency_bars") + tuple(_entry_keys),
        cli_override=_entry_cli_override,
        config_origin=_effective_origin,
    )

    # ---- M4.3: inert policy-kernel key + M1.1 opt-in knobs -------------------
    # The key is still RESOLVED (1. `--live-use-policy-kernel` -> args, 2. strategy
    # params `live_use_policy_kernel` / blend-prefixed spellings, 3. True) and
    # still carried on the config, but since M4.4 stage D3 it selects NOTHING: the
    # legacy state machine it used to opt out of was deleted in BASE_VERSION 2.0.0.
    # A persisted strategy param pinning `live_use_policy_kernel=false` therefore
    # only makes `step_position_lifecycle` log its one-time removal warning; the
    # kernel runs either way (failing the run closed over a dead key would be a
    # bigger behaviour change than the removal).
    live_use_policy_kernel = bool(getattr(args, "live_use_policy_kernel", False)) or bool(
        resolve_blend_param(params, "live_use_policy_kernel", True, bool)
    )
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "live_use_policy_kernel",
        live_use_policy_kernel,
        keys=[f"{prefix}_live_use_policy_kernel" for prefix in _BLEND_PARAM_PREFIXES]
        + ["live_use_policy_kernel", "lifecycle_live_use_policy_kernel"],
        cli_override=bool(getattr(args, "live_use_policy_kernel", False)),
        config_origin=_effective_origin,
    )
    # The six M1.1 knobs come from the SHARED resolver so the live driver and the
    # kernel can never disagree about them (one mapping, `position_policy_
    # config_from_lifecycle`). Only these six are taken: the runner keeps its own
    # resolution for every legacy field (notably `min_entry_threshold`, which the
    # backtest-side resolver deliberately leaves `None` while live derives 0.05).
    _policy_cfg = resolve_position_policy_config(params, args)
    # AT8: per-knob provenance for the six M1.1 opt-in gates. `lifecycle_<field>` outranks
    # the bare field in the shared resolver, so `declared` reads the same two spellings and
    # a `--lifecycle-*` CLI value is the only override.
    for _knob_name in (
        "two_day_breakdown_rule",
        "breakdown_days",
        "breakdown_vol_shrink_pct",
        "price_reentry_gate",
        "price_reentry_pct",
        "reentry_vwap_lookback",
    ):
        _record_effective_param(
            effective_param_entries,
            declared_params,
            _knob_name,
            getattr(_policy_cfg, _knob_name),
            keys=(f"lifecycle_{_knob_name}", _knob_name),
            cli_override=getattr(args, f"lifecycle_{_knob_name}", None) is not None,
            config_origin=_effective_origin,
        )

    # AT8 (incident-2 anchor): `declared` is the BARE key -- usually absent -- while the
    # resolver picks the blend-prefixed spelling (`cfsb_inertia_relative_ratio`) out of the
    # dump. Recording the prefixed value as "declared" would hide exactly the divergence
    # this artifact exists to expose (declared null -> resolved 0.30).
    _inertia_relative_ratio = resolve_blend_param(params, "inertia_relative_ratio", 0.0, float)
    _inertia_present = "inertia_relative_ratio" in declared_params or any(
        f"{prefix}_inertia_relative_ratio" in declared_params for prefix in _BLEND_PARAM_PREFIXES
    )
    effective_param_entries["inertia_relative_ratio"] = param_entry(
        declared_params.get("inertia_relative_ratio"),
        _inertia_relative_ratio,
        _effective_origin if _inertia_present else SOURCE_DEFAULT,
    )

    # ---- LV18 / AT8 (2026-11-12 defect): the vol-overlay gate must be reproducible ------
    # A ticket with `vol_overlay.applied = true` was un-explainable from its own snapshot:
    # `cfsb_portfolio_vol_overlay_enabled` / `_mode` were resolved (and consumed, see
    # `apply_portfolio_vol_overlay_for_strategy` below) but never registered here, so
    # `effective_params.json` carried neither key. Recorded through the SAME resolver the run
    # executes (no re-derivation), and for a strategy OFF the allowlist the keys are recorded
    # too -- as `enabled=False` / `mode="off"` with `source="default"` -- so the snapshot can
    # also answer "why does THIS strategy carry no overlay?".
    record_portfolio_vol_overlay_effective_params(
        effective_param_entries,
        declared_params,
        strategy_key,
        config_origin=_effective_origin,
    )

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
        inertia_relative_ratio=_inertia_relative_ratio,
        min_entry_threshold=min_entry_thresh,
        hard_stop_loss_pct=0.08,
        atr_multiplier=4.5,
        volume_surge_multiplier=getattr(args, "volume_breakout_mult", 1.5),
        breadth_thrust_threshold=0.60,
        enable_entry_fast_path=True,
        enable_exit_fast_path=True,
        # M4.3: the inert kernel key + the six M1.1 opt-in gates. Since M4.4 D3 the
        # key selects nothing (`False` only triggers the adapter's removal warning);
        # it is carried so a persisted config keeps round-tripping.
        live_use_policy_kernel=live_use_policy_kernel,
        two_day_breakdown_rule=_policy_cfg.two_day_breakdown_rule,
        breakdown_days=_policy_cfg.breakdown_days,
        breakdown_vol_shrink_pct=_policy_cfg.breakdown_vol_shrink_pct,
        price_reentry_gate=_policy_cfg.price_reentry_gate,
        price_reentry_pct=_policy_cfg.price_reentry_pct,
        reentry_vwap_lookback=_policy_cfg.reentry_vwap_lookback,
    )

    # Build descriptive engine banner information
    engine_parts = []
    if "blend_weighting_mode" in params or "blend" in template_name.lower():
        engine_parts.append(f"加权={params.get('blend_weighting_mode', 'rolling_ic_ir')}")
        engine_parts.append(f"正交化={params.get('blend_enable_orthogonalization', True)}")
    if "vol_model" in params or any(k.endswith("_vol_model") for k in params):
        vm = resolve_blend_param(params, "vol_model", "garman_klass", str)
        engine_parts.append(f"波动率模型={vm}")
        _record_effective_param(
            effective_param_entries,
            declared_params,
            "vol_model",
            vm,
            config_origin=_effective_origin,
        )
    if "rebalance_freq_days" in params:
        engine_parts.append(f"调仓周期={params['rebalance_freq_days']}日")
    engine_parts.append(f"UPLSM确认={guard_latency_bars}日")
    engine_parts.append(f"买入确认={min_entry_bars}日")
    engine_parts.append(f"最低持仓={min_holding_bars}日")
    engine_parts.append(f"惰性过滤={min_trade_thresh:.1%}")
    engine_parts.append(f"最低建仓={min_entry_thresh:.1%}")
    # M4.4 D3: the kernel is the ONLY engine, so the banner always says so. (The
    # inert `live_use_policy_kernel` key is not consulted -- announcing the engine
    # conditionally would imply a choice that no longer exists.)
    engine_parts.append("策略内核=position_policy")
    engine_info = " | ".join(engine_parts)

    strat_ver = strategy_def.get("strategy_version", "1.0.0")
    base_ver = strategy_def.get("base_version")
    # (LV7, `docs/lv7_prior_state_contract.md` §5.3) The carried-forward account state must
    # have been written by TODAY's engine version. Checked HERE (before any order math and
    # before any ticket artifact exists), and again immediately before the state is
    # persisted -- the state dict is heavily mutated in between.
    # Fail closed: a stale `base_version` (the 2026-11-02 archive carries 1.7.0 while the
    # live class is 1.8.0) must STOP the run, never be silently re-stamped. The remedy is
    # an explicit operator action (§5.4), not an automatic overwrite.
    _live_strategy_version = getattr(template, "version", None) or strat_ver
    _live_base_version = getattr(template, "base_version", None) or base_ver
    # (2026-11-12 seeding race) BEFORE the parity gate, label the specific case it cannot
    # distinguish: this state ALREADY claims THIS session but carries another engine's
    # version, i.e. the round's own `account_state.json` was overwritten by the seeding of a
    # previous session's snapshot. Parity alone would send the operator to "regenerate the
    # dump" -- the wrong remedy, because the dump check already passed above. Fail-closed
    # either way; this only re-labels the diagnosis.
    assert_state_version_not_clobbered(
        account_state,
        _live_strategy_version,
        _live_base_version,
        as_of_date=as_of_date,
        state_display=args.account_state_file,
    )
    assert_account_state_version_parity(
        account_state,
        _live_strategy_version,
        _live_base_version,
        state_display=args.account_state_file,
    )
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
        extra={
            "date": as_of_date,
            "stage": "stage2",
            "run_mode": run_mode,
            **merge_timing_into_summary(timing),
            # (D2) Grep-able `[PRIOR STATE] ...` conclusion (same line the console printed),
            # so "which snapshot did this ticket resume from?" is answerable from the archive
            # alone. Absent when the resolver had nothing to say (no archive context).
            **({"prior_state_log": prior_state_log} if prior_state_log else {}),
            # (LV11) Only present when the operator explicitly acknowledged an unevaluated
            # breaker; recorded here so the permission is auditable next to the ticket rather
            # than living only in the server's console scrollback.
            **(
                {"gate_degraded_acknowledged": True, "gate_degraded_ack_env": ALLOW_DEGRADED_GATE_ENV}
                if degraded_gate_acknowledged()
                else {}
            ),
        },
    )

    enable_intraday_5m = getattr(args, "enable_intraday_5m", False) or bool(
        resolve_blend_param(params, "enable_intraday_5m", False, bool)
    )
    _record_effective_param(
        effective_param_entries,
        declared_params,
        "enable_intraday_5m",
        enable_intraday_5m,
        cli_override=bool(getattr(args, "enable_intraday_5m", False)),
        config_origin=_effective_origin,
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
            _5m_market = getattr(clock_ctx, "market", None) or "china"
            universe_5m = load_intraday_5m_universe(
                symbols_to_load,
                start=start_date,
                end=as_of_date,
                data_provider=provider,
                cutoff_time=cutoff_t,
                # (2026-11-11, `六` item 6) Stamp `intraday_5m_stale` /
                # `intraday_5m_lag_calendar_days` on each frame so a FROZEN cache is
                # distinguishable from "no 5m support" at the loader, not inferred from
                # timestamps by every downstream reader.
                expected_last_session=expected_last_session,
                market=_5m_market,
            )
            # (2026-10-22 incident, Fix 1) This line used to print
            # "成功加载并聚合 5m 日内特征 ... 标的数: {len(universe)}" -- a SUCCESS
            # message whose count was the DAILY symbol count. On 2026-10-22 the
            # configured provider (FuyaoDataProvider) raises on any
            # interval != "1d", so EVERY 5m fetch failed, the 5m universe was
            # empty, enrich_universe_with_intraday_features early-returned an
            # un-enriched frame, and downstream VWAP/Chan-5m fields were all
            # None -- while this line still claimed 20 symbols of intraday
            # microstructure had been aggregated.
            # (2026-11-11, `六` item 6) The zero-row WARN then blamed "provider does not
            # support 5m", which is DISPROVEN: the live provider delivered 5m fine, its
            # per-symbol coverage was missing / its cache stopped one session early. The
            # reporting now says WHICH cause it can prove (`provider=`, `source=`,
            # `fallback_from->to`, `stale`, `lag_calendar_days`) instead of guessing.
            #
            # Reported BEFORE the enrichment on purpose: the enrichment can raise (a
            # malformed/degenerate 5m frame does), and a report that only runs on the
            # success path would once again hide the state of the data it describes.
            for _line in summarize_intraday_5m_loading(
                universe_5m,
                symbols_to_load,
                provider_label=str(getattr(args, "data_provider", "") or ""),
                cutoff_time=str(cutoff_t),
                expected_last_session=expected_last_session,
                market=_5m_market,
                publish_after=str(
                    getattr(args, "intraday_publish_after", None) or "20:00"
                ),
            ):
                print(_line)
            universe = enrich_universe_with_intraday_features(
                universe, universe_5m, config=IntradayFeatureConfig(cutoff_time=cutoff_t), synthesize_intraday_bar=True
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

    # ------------------------------------------------------------------ #
    # (`R6`, rule r6.3) OVERNIGHT DE-RISKING -- decided here, applied here
    # ------------------------------------------------------------------ #
    # Positioning (frozen judgement: `docs/overnight_derisk_judgement_freeze.md`): this is
    # "how much exposure do we carry through tonight's gap", NOT intraday timing and NOT
    # per-symbol selection. It sits exactly where the model's target weights become final --
    # before `model_equity_target` and before every downstream gate -- so the circuit breakers
    # / recovery scale / Stage-1 distribution gate all consume the DE-RISKED book instead of
    # silently re-scaling the un-de-risked one.
    #
    # Applied PER MARKET: `D = session_close_time(market, day) - 45min` is market-local and
    # half-day aware, so lending one market's clock to another market's book is precisely the
    # look-ahead the freeze document §2.2 forbids (a fixed 14:15 is after the close on a US/HK
    # half-day). `resolve_live_clock_context` already refuses a cross-market universe unless
    # `--market` was passed explicitly, so the normal run collapses to exactly one group.
    #
    # (`r6.3`, freeze doc `## Revision 2026-11-07b`) TWO DIFFERENT SETS, never confused:
    #   * the SIGMA cross-section is the LOADED UNIVERSE for the market minus the cash proxies
    #     (`cross_section_markets` below). It must not be the target book: a 2-name book yields a
    #     2-name sigma, the trigger then flips on strategy rotation rather than on risk, and
    #     recomputing the trailing 252-sigma baseline from TODAY's book retroactively rewrites
    #     history (a mild look-ahead). A market whose cross-section is empty yields
    #     INSUFFICIENT_DATA -- there is deliberately NO fallback to the book.
    #   * the APPLICATION surface is still the target book (`market_groups`): a market only gets
    #     a multiplier where the book actually holds a risky weight.
    overnight_derisk_by_market: Dict[str, Dict[str, Any]] = {}
    overnight_derisk_row_market: Dict[str, str] = {}
    overnight_derisk_summary: Optional[Dict[str, Any]] = None
    if overnight_derisk_config.enabled:
        session_date_for_derisk = pd.Timestamp(as_of_date).date()
        cash_proxy_keys = resolve_cash_proxy_target_symbols(current_targets.index, cash_proxy)
        risky_keys = [s for s in current_targets.index if s not in cash_proxy_keys]

        # (r6.3) Sigma cross-section: loaded universe, grouped by market, cash proxies removed.
        universe_cash_proxy_keys = resolve_cash_proxy_target_symbols(list(universe.keys()), cash_proxy)
        universe_market_of: Dict[str, str] = {}
        cross_section_markets: Dict[str, List[str]] = {}
        universe_market_fallback: List[str] = []
        for sym in universe:
            try:
                sym_market = infer_market_from_symbol(sym)
            except UnknownSymbolMarketError:
                # The gate already resolved this run's authoritative market; a symbol it could
                # not identify does not open a new one, and guessing a calendar is what the
                # market-clock design (§14 row 3) explicitly refuses. It is NOT silent any more:
                # the fallback is printed below so an unverified market attribution is visible.
                sym_market = clock_ctx.market
                universe_market_fallback.append(str(sym))
            universe_market_of[str(sym)] = sym_market
            if sym in universe_cash_proxy_keys:
                # Cash proxies carry the risk-free rate, not overnight equity gap risk: they must
                # never enter N_t (they would mechanically damp sigma toward zero).
                continue
            cross_section_markets.setdefault(sym_market, []).append(sym)

        # Application surface: the book, grouped by the same markets.
        market_groups: Dict[str, List[str]] = {}
        book_market_fallback: List[str] = []
        for sym in risky_keys:
            sym_market = universe_market_of.get(str(sym))
            if sym_market is None:
                try:
                    sym_market = infer_market_from_symbol(sym)
                except UnknownSymbolMarketError:
                    sym_market = clock_ctx.market
                    book_market_fallback.append(str(sym))
            market_groups.setdefault(sym_market, []).append(sym)

        if universe_market_fallback:
            print(
                f"[INFO] (R6) universe 中 {len(universe_market_fallback)} 个标的无法从符号推断市场，"
                f"已回退到本次运行市场 {clock_ctx.market}: {sorted(universe_market_fallback)}"
                "（市场归属未经符号验证，判定时点与横截面均按该市场计算）"
            )
        if book_market_fallback:
            print(
                f"[INFO] (R6) 目标簿中无法从符号推断市场的标的已回退到本次运行市场 "
                f"{clock_ctx.market}: {sorted(book_market_fallback)}"
            )

        for sym_market in sorted(market_groups):
            group_symbols = market_groups[sym_market]
            cross_section_symbols = cross_section_markets.get(sym_market, [])
            group_universe = {s: universe[s] for s in cross_section_symbols}
            gap, symbols_used_series = compute_gap_series(group_universe, as_of_date)
            decision = evaluate_overnight_derisk(
                gap,
                symbols_used_series,
                sym_market,
                session_date_for_derisk,
                overnight_derisk_config,
            )
            scaled = apply_overnight_downscale(
                {s: float(current_targets[s]) for s in group_symbols},
                decision,
                cash_proxy_keys,
            )
            for sym, weight in scaled.items():
                current_targets[sym] = weight
            block = decision.ticket_block(overnight_derisk_config)
            overnight_derisk_by_market[sym_market] = block
            for sym in group_symbols:
                overnight_derisk_row_market[sym] = sym_market

            if decision.status is OvernightDeriskStatus.INSUFFICIENT_DATA:
                # (Owner ruling 2026-11-07) insufficient data only WARNS: it does not de-risk,
                # and it does not refuse the ticket -- but it must NEVER look like "not
                # triggered", so the state, the evidence and the reason all print AND land on
                # every affected ticket row.
                print("\n" + "!" * 110)
                print(
                    f"[WARN] (R6) 隔夜暴露下调判据数据不足 (market={sym_market}) —— "
                    "**不降暴露**，且这**不是**「未触发」。"
                )
                print(f"       {decision.reason}")
                print(
                    f"       decision_instant={block['decision_instant']} "
                    f"sessions_used={decision.sessions_used} "
                    f"symbols_used={decision.symbols_used} ratio_applied={block['ratio_applied']}"
                )
                print("!" * 110 + "\n")
            else:
                print(
                    f"\n[OVERNIGHT DERISK / 隔夜暴露] market={sym_market} "
                    f"status={decision.status.value} D={block['decision_instant']} "
                    f"σ_gap={block['sigma_gap']} baseline={block['baseline_median']} "
                    f"ratio={block['ratio_applied']}"
                )

        if not market_groups:
            print(
                "\n[INFO] (R6) 隔夜暴露下调判据已启用，但目标权重中没有任何风险标的"
                "（全部为现金代理），无需判定。"
            )

        overnight_derisk_summary = {
            "spec_version": OVERNIGHT_DERISK_SPEC_VERSION,
            "config": {
                "minutes_before_close": overnight_derisk_config.minutes_before_close,
                "gap_window": overnight_derisk_config.gap_window,
                "baseline_window": overnight_derisk_config.baseline_window,
                "sigma_multiplier": overnight_derisk_config.sigma_multiplier,
                "downscale_ratio": overnight_derisk_config.downscale_ratio,
            },
            "cash_proxy_symbols": sorted(str(s) for s in cash_proxy_keys),
            "status_by_market": {
                market: block["status"] for market, block in overnight_derisk_by_market.items()
            },
            "markets": overnight_derisk_by_market,
        }
        if len(overnight_derisk_by_market) == 1:
            # Normal case: flatten so `summary.json.overnight_derisk.status` is directly
            # readable. A mixed book keeps the per-market map only -- there is no single status,
            # and inventing a fourth word (e.g. "MIXED") would break the closed tri-state.
            only = next(iter(overnight_derisk_by_market.values()))
            overnight_derisk_summary.update(
                {
                    key: only[key]
                    for key in (
                        "status",
                        "market",
                        "decision_instant",
                        "sigma_gap",
                        "baseline_median",
                        "sigma_multiplier",
                        "ratio_applied",
                        "sessions_used",
                        "symbols_used",
                        "reason",
                    )
                }
            )
        # A MERGE into the same summary.json (`persist_run_metadata` is a merge, not an
        # overwrite), so the clock/freshness block written before the data load survives and
        # this run gains one machine-readable decision block. It repeats the "审计字段已写入"
        # banner -- cosmetic, and far cheaper than a second writer that could clobber a key.
        persist_run_metadata(
            args.output_dir,
            clock_ctx,
            freshness_summary=freshness_summary,
            probe_attempts=probe_attempts,
            extra={"overnight_derisk": overnight_derisk_summary},
        )

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
    # (2026-11-12 defect A) The verdict lives INSIDE the report's `portfolio_health` block;
    # the top-level mirror is a fallback (and may be a placeholder). `resolve_stage1_health_gate`
    # reads BOTH archives and keeps the worse answer, and `assert_stage1_gate_usable` refuses
    # BEFORE any order math when either says the breaker was never evaluated.
    _gate_info = assert_stage1_gate_usable(account_state, health_report)
    circuit_tier = _gate_info["circuit_breaker_tier"]
    freeze_rem = int(account_state.get("freeze_remaining_bars", health_report.get("freeze_remaining_bars", 0)))
    gate_code = account_state.get("gate_code") or health_report.get("gate_code")
    is_recovering = (gate_code == "CAUTION_RECOVERING" or circuit_tier == "CAUTION_RECOVERING")
    is_portfolio_deleverage = False
    # (LV18 item 3) Set ONLY by the Tier-3 halt below. Threaded into the vol-ceiling audit so
    # an emptied book is attributed to the breaker rather than to Stage-1's vol cap. It is
    # deliberately NOT `is_portfolio_deleverage`: the halt path keeps its existing semantics.
    circuit_breaker_superseded = None
    if (circuit_tier == "TIER_3_HALT" or freeze_rem > 0) and not is_recovering:
        print("\n" + "!" * 110)
        print(f"[CIRCUIT BREAKER ENGAGED / 熔断器生效] 账户处于第三级熔断停牌期 (Tier 3 Emergency Halt, 剩余冷冻: {freeze_rem} 交易日)")
        print("根据风控守则，所有风险标的持仓清零，全额切换为现金停泊 (100% Cash Sweep).")
        print("!" * 110 + "\n")
        current_targets = pd.Series(0.0, index=current_targets.index)
        if cash_proxy in current_targets.index:
            current_targets[cash_proxy] = 1.0
        circuit_breaker_superseded = VOL_OVERLAY_SUPERSEDED_BY_CIRCUIT_BREAKER
    elif is_recovering:
        raw_scale = (
            account_state.get("linear_equity_scale")
            if account_state.get("linear_equity_scale") is not None
            else health_report.get("linear_equity_scale")
        )
        rec_scale = float(raw_scale) if raw_scale is not None else 0.30
        if rec_scale <= 0.0 or rec_scale >= 0.999:
            rec_scale = 0.30
        print(f"[FAST RECOVERY OVERRIDE / 快速恢复] 账户处于快速恢复期 (CAUTION_RECOVERING)，降级为 Tier 2 防御模式，股票仓位比例控制在: {rec_scale:.1%}")
        is_portfolio_deleverage = True
        risky_syms_in_targets = [s for s in current_targets.index if s != cash_proxy]
        current_targets[risky_syms_in_targets] = current_targets[risky_syms_in_targets] * rec_scale
        if cash_proxy in current_targets.index:
            tot_risky = float(current_targets[risky_syms_in_targets].sum())
            current_targets[cash_proxy] = max(0.0, 1.0 - tot_risky)
    elif account_state.get("linear_equity_scale") is not None or health_report.get("linear_equity_scale") is not None:
        live_scale = float(
            account_state.get("linear_equity_scale")
            if account_state.get("linear_equity_scale") is not None
            else health_report.get("linear_equity_scale")
        )
        if live_scale < 0.999:
            print(f"[RISK DAMPING / 风险阻尼] 根据账户实盘回撤，线性压缩股票仓位比例至: {live_scale:.1%}")
            is_portfolio_deleverage = True
            live_scale_clamped = max(0.0, live_scale)
            risky_syms_in_targets = [s for s in current_targets.index if s != cash_proxy]
            current_targets[risky_syms_in_targets] = current_targets[risky_syms_in_targets] * live_scale_clamped
            if cash_proxy in current_targets.index:
                tot_risky = float(current_targets[risky_syms_in_targets].sum())
                current_targets[cash_proxy] = max(0.0, 1.0 - tot_risky)

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
    # (2026-11-12 defect B) Capture the equity sum BEFORE the gate so the vol-overlay audit
    # can attribute the whole chain instead of collapsing "gate x cap x lifecycle x lot
    # rounding" into one opaque number. The gate's own scaling logic is NOT touched.
    _pre_distribution_gate_equity_sum = float(
        sum(
            max(0.0, float(target_series[s]))
            for s in target_series.index
            if cash_proxy is None or str(s) != str(cash_proxy)
        )
    )
    target_series = apply_intraday_distribution_gate(
        target_series,
        current_series,
        cash_proxy,
        health_report=health_report,
        account_state=account_state,
    )

    # (`AT7` P2) SHADOW line, computed NEXT TO the real gate and read by it never.
    # `r1_adv` is `None` when Stage-1 could not measure the intraday drawdown; in
    # that case no shadow number is invented (see `compute_r1_advisory`).
    r1_adv = compute_r1_advisory(health_report)
    # The REAL buy-side scale: the SAME fallback chain `apply_intraday_distribution_gate`
    # resolves inline (`health_report` -> `account_state` -> 0.50 default). Re-resolved
    # here rather than refactored out of the gate ON PURPOSE -- P2 is write-only, so the
    # sizing function above must stay byte-identical, and one extra read of two dicts
    # cannot change an order. (Stage-1 only ever writes 0.50 on a positive distribution
    # detection and mirrors 1.0 otherwise, so this value equals the scale the gate
    # actually applies whenever the gate fires.)
    real_intraday_buy_scale = float(
        health_report.get("intraday_buy_scale", account_state.get("intraday_buy_scale", 0.50))
    )
    # (`AT7` P3) SCOPE OF `intraday_buy_scale`: it is Stage-1's *intraday
    # distribution* brake and NOTHING else. `R4` (cross-sectional dispersion /
    # market-level de-risk) is explicitly OUT OF SCOPE for this row -- there is no
    # `R4` import here, no market-level de-risk is computed, and this scale must
    # not be stretched to cover one. `R1` below is the ONLY thing that composes
    # with it, and only on buy increments.
    #
    # (`AT7` P3) ENFORCEMENT of the frozen composition:
    #     effective_buy_scale = real_buy_scale x r1.multiplier   (0.50 x 0.25 = 0.125)
    # Two gates must both say yes (`r1_derisk_enforcement_enabled`: env kill-switch
    # > `--enforce-r1-derisk` > OFF), so the default path is bit-identical to P2.
    # `apply_r1_intraday_derisk` scales the float weights HERE -- upstream of lot
    # rounding, of the purchasing-power rescale and of the delivered market-value
    # cap -- so a de-risked target still flows through every existing rounding and
    # validation gate instead of bypassing them. It runs AFTER
    # `apply_intraday_distribution_gate`, which is what makes the composition
    # multiplicative rather than a replacement.
    r1_enforced = r1_derisk_enforcement_enabled(getattr(args, "enforce_r1_derisk", False))
    r1_multiplier: Optional[float] = None
    if r1_adv is not None and r1_adv.get("multiplier") is not None:
        r1_multiplier = float(r1_adv["multiplier"])
    # The whole predicate (flag AND real multiplier) lives in this pure helper; on
    # the default OFF path it hands back the caller's own object, so nothing below
    # can tell P3 from P2.
    target_series, r1_applied = resolve_r1_derisk_target(
        target_series,
        current_series,
        cash_proxy,
        enforced=r1_enforced,
        r1_multiplier=r1_multiplier,
    )
    if r1_applied:
        effective_buy_scale = real_intraday_buy_scale * r1_multiplier
        print("\n" + "!" * 110)
        print(
            f"[R1 INTRADAY DERISK / 日内回撤降险] status={r1_adv['status']} "
            f"multiplier={r1_multiplier:.2f}; 买入增量按 实盘系数 {real_intraday_buy_scale:.2%} "
            f"× R1 {r1_multiplier:.2f} = {effective_buy_scale:.2%} 压缩；卖单与现金不动。"
        )
        print("!" * 110 + "\n")

    # ---------------------------------------------------------------------------------
    # (LV18, 2026-11-12) STAGE-1 BARROSO CEILING -> EQUITY BUDGET CAP (not a multiplier)
    #
    # `check_live_portfolio_health.py::evaluate_realized_volatility` always COMPUTED
    # `vol_scaling_factor = clip(target_vol / realized_vol_21d)` and archived it, but
    # nothing consumed it (docs/development_plan.md LV18: "presented as an applied risk
    # action, actually inert"). It is wired HERE, on the TARGET WEIGHT VECTOR, for exactly
    # the reason `apply_r1_intraday_derisk` right above is: a later pass over built rows
    # cannot de-risk an already-invested book without inventing SELLs that would bypass
    # `step_position_lifecycle`'s `can_sell` (China T+1 gestation), the tenure lock and the
    # inertia gate -- measured on 2026-11-11 synthetic replay, the row pass left the
    # delivered book at 78.6% while the row claimed 3.7% (`partial=true`), i.e. inert again.
    #
    # SEMANTICS (`model_equity_sum` = Σ equity targets of the model book):
    #     equity_budget = min(model_equity_sum, vol_scaling_factor)
    # NOT a product: `blend_base.py` already applies its own Garman-Klass target-vol
    # scaling (`blend_target_vol = 0.12`), so multiplying would de-risk the same elevated
    # volatility twice (archived 2026-11-11/205137: 0.463 x 0.370 = 0.171, not 0.370).
    #
    # The implied cash residual is recomputed immediately below (`1 - Σ equity`), so a
    # capped book parks the difference in the cash proxy. `is_portfolio_deleverage=True`
    # is set when the cap binds so the tenure lock permits the trim (L2.40), exactly like
    # the Tier-2 / linear-damping Stage-1 defenses above.
    #
    # ORDERING vs. THOSE DEFENSES (Tier-2 recovery damping / linear drawdown damping):
    # STRICTER WINS, NEVER THE PRODUCT. This pass reads the vector AFTER they scaled it, so
    # it can only tighten an already-damped book: 0.50 damping + 0.37 ceiling -> 0.37 (not
    # 0.185); 0.30 damping + 0.37 ceiling -> binding=False, series untouched (also not
    # 0.111). A Tier-3 halt has already zeroed the book before this point, so the breaker is
    # passed in as `superseded_by` and the audit attributes the empty book to the breaker
    # (`superseded_by_circuit_breaker=true`) instead of crediting the vol cap with it.
    # Strategy-gated (`chan_four_state_blend` ONLY): every other strategy gets its series
    # back untouched and `vol_overlay=None`, keeping its ticket byte-identical.
    # ---------------------------------------------------------------------------------
    target_series, vol_overlay = apply_portfolio_vol_overlay_for_strategy(
        target_series,
        strategy_key=strategy_key,
        params=params,
        health_report=health_report,
        cash_proxy=cash_proxy,
        superseded_by=circuit_breaker_superseded,
    )
    if vol_overlay is not None:
        if vol_overlay.get("reason") == VOL_OVERLAY_UNAVAILABLE_REASON:
            print(
                "[INFO/VOL OVERLAY] Stage-1 波动率因子不可用（未实测/已降级），"
                "保留模型原始仓位，不做任何缩放（默认 1.0x 会把未测得当成平静市况）。"
            )
        elif vol_overlay.get("superseded_by_circuit_breaker"):
            print(
                "[INFO/VOL OVERLAY] 三级熔断已先行清零风险仓位 "
                f"(superseded_by={vol_overlay.get('reason')})；上限没有参与本次降风险，"
                "不重复缩放，也不把清仓记在上限名下。"
            )
        elif vol_overlay.get("binding"):
            _rv = vol_overlay.get("realized_vol_21d")
            _rv_txt = f"{_rv:.1%}" if isinstance(_rv, (int, float)) else "N/A"
            print("\n" + "!" * 110)
            print(
                f"[RISK ALERT / VOL OVERLAY] Stage-1 Barroso 上限生效: "
                f"模型权益 {vol_overlay['model_equity_sum']:.2%} -> 上限 "
                f"{vol_overlay['capped_equity_sum']:.2%} "
                f"(21d 实测波动 {_rv_txt}, 因子 {vol_overlay['vol_scaling_factor']:.4f}x；"
                f"语义=取上限(min)，非相乘); 超出部分转为现金停泊。"
            )
            print("!" * 110 + "\n")
            is_portfolio_deleverage = True
        else:
            print(
                f"[INFO/VOL OVERLAY] Stage-1 上限未触发（模型权益 "
                f"{vol_overlay['model_equity_sum']:.2%} <= 因子 "
                f"{vol_overlay['vol_scaling_factor']:.4f}x），目标仓位保持不变。"
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
    _sub_strategy_classes: Optional[Dict[str, str]] = None
    _effective_weighting_mode: Optional[str] = None
    if hasattr(template, "get_sub_strategies"):
        try:
            sub_strats = template.get_sub_strategies(universe, params)
            # ---- AT8 run-level facts, captured ONCE per run ------------------------
            # Not per symbol and never inside `detail_reason`: these describe the RUN,
            # so they belong beside `params`, not in every ticket row.
            _sub_strategy_classes = {
                sname: type(s_strat).__name__ for sname, s_strat in sub_strats.items()
            }
            if hasattr(template, "resolve_param"):
                # Deliberately the SAME resolver, the SAME `params` and the SAME default
                # the engine's own call uses inside `blend_base.generate_weights` (which
                # this observation layer must not touch). `resolve_param` is pure, so a
                # second call cannot disagree with the first -- but this IS a duplicate
                # call site, disclosed here rather than silently re-deriving the mode.
                _effective_weighting_mode = template.resolve_param(
                    params, "weighting_mode", "rolling_ic_ir", str
                )
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

    # ---- AT8: persist parameter provenance. OBSERVATION ONLY -------------------------
    # Nothing below reads this file back, so a failure here must never alter sizing or the
    # order path: both the pure build and the disk write are wrapped, and the only effect
    # of a failure is one loud stderr warning.
    try:
        _effective_payload = build_effective_params(
            strategy_id=strategy_key,
            strategy_version=strategy_def.get("strategy_version"),
            base_version=strategy_def.get("base_version"),
            config_source=resolved_desc,
            strategy_dump_sha256=_effective_dump_sha,
            as_of=as_of_date,
            run_id=effective_run_id,
            params=effective_param_entries,
            sub_strategy_classes=_sub_strategy_classes,
            effective_weighting_mode=_effective_weighting_mode,
        )
        _write_effective_params(args.output_dir, _effective_payload)
    except Exception as exc:
        print(
            f"[EFFECTIVE PARAMS] warning: 参数溯源构建失败，运行继续: {exc}",
            file=sys.stderr,
        )

    # Sector holding counts & throttle limit
    sector_counts: Dict[str, int] = {}
    for sym_c, w_c in current_series.items():
        if sym_c != cash_proxy and w_c > 1e-4:
            sec_c = _get_symbol_sector(sym_c)
            sector_counts[sec_c] = sector_counts.get(sec_c, 0) + 1
    max_assets_per_sector = int(params.get("max_assets_per_sector", 3))

    breadth_val = float(macro_dict.get("breadth", health_report.get("breadth", 0.50)) or 0.50)
    thrust_val = float(macro_dict.get("thrust", health_report.get("thrust", 0.50)) or 0.50)
    macro_passed = not ((circuit_tier == "TIER_3_HALT" or freeze_rem > 0) and not is_recovering)

    updated_lifecycle_states: Dict[str, Any] = {}
    rows = []
    stale_price_symbols: List[str] = []
    # (`AT7` P3) ONE portfolio-level advisory block per run, built OUTSIDE the
    # per-symbol loop so every row carries the same object shape. This run is
    # labelled `P3` and REPLACES the `P2` block -- one run is one phase, never
    # two blocks. Honest empty inputs for the two dimensions that still have no
    # live source: no cross-sectional score source exists in the live chain yet
    # (`common/signal_snapshot.py:12-38`) and there is no executed-notional
    # ledger, so the matrix renders INSUFFICIENT and both capacity labels render
    # MISSING. `r1` / `real_buy_scale` / `shadow_buy_scale` keep the same shape as
    # P2; `r1_effective` is flipped to `True` ONLY when the two-gate switch let
    # `apply_r1_intraday_derisk` actually shrink a buy increment, and
    # `effective_buy_scale` is added exactly then.
    #
    # `r1_effective` is (re)assigned here rather than passed to the builder: the
    # builder is the frozen shared shape (`common/advisory_report.py`, owned by
    # P1/P2) and it always renders the conservative `False`; only this caller
    # knows whether an order was really resized.
    advisory_block = build_advisory_block(
        phase=ADVISORY_PHASE_P3,
        leg_reports=(),
        turnover=None,
        participation=None,
        as_of=as_of_date,
        r1=r1_adv,
        real_buy_scale=real_intraday_buy_scale,
    )
    advisory_block["r1_effective"] = r1_applied
    if r1_applied:
        # `_json_number` guard: the ticket writer has no `allow_nan=False` crutch, so
        # a non-finite composed scale must be ABSENT, never `NaN`.
        effective_buy_scale_json = _json_number(
            real_intraday_buy_scale * r1_multiplier
        )
        if effective_buy_scale_json is not None:
            advisory_block["effective_buy_scale"] = effective_buy_scale_json
        # `gates_orders` on the R1 sub-block means "THIS run's orders were actually
        # resized by R1" -- the top-level flag stays `False` because the advisory
        # LAYER never gates (it is a report, not an input to any sizing function).
        advisory_block["r1"]["gates_orders"] = True
    # NOTE on the TOP-LEVEL `gates_orders`: it stays `False`, and that is not in
    # tension with `r1_effective=True`. The flag is the ADVISORY-LAYER fence ("no
    # other gate or sizing function consumes this block" -- still exactly true, the
    # multiplier is applied by the caller from `r1_adv`), while `r1_effective`
    # records that the R1 multiplier itself was applied to buy increments. Flipping
    # the top-level `gates_orders` here would contradict the frozen builder's
    # contract; `advisory_block["r1"]["gates_orders"]` (set above) is the field that
    # does describe this run. The two fields answer two different questions.
    # Exactly ONE R1 log line per run, and only when there was a verdict to
    # report. When enforcement fired the line says so (the banner above already
    # stated the composed scale); otherwise it states the P2 fence in the line
    # itself so a pasted log cannot be mistaken for an executed de-risk.
    if r1_adv is not None and advisory_block.get("shadow_buy_scale") is not None:
        if r1_applied:
            print(
                "[ADVISORY R1 DERISK] real intraday_buy_scale=%.2f | "
                "R1 multiplier=%.2f (status=%s) | "
                "effective_buy_scale=%.2f | APPLIED to buy increments only"
                % (
                    advisory_block["real_buy_scale"],
                    advisory_block["r1"]["multiplier"],
                    advisory_block["r1"]["status"],
                    advisory_block["effective_buy_scale"],
                )
            )
        else:
            print(
                "[ADVISORY SHADOW] real intraday_buy_scale=%.2f | "
                "R1 shadow multiplier=%.2f (status=%s) | "
                "composed shadow scale=%.2f | NOT applied to orders"
                % (
                    advisory_block["real_buy_scale"],
                    advisory_block["r1"]["multiplier"],
                    advisory_block["r1"]["status"],
                    advisory_block["shadow_buy_scale"],
                )
            )
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
        # On an enforced `R1` (`--enforce-r1-derisk`) run this target is already
        # POST-de-risk; stale-lot detection stays correct because a buy increment
        # can only shrink, never invert, so `raw_tgt_w > current >= 0` still holds.
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

        is_circuit_halt = ((circuit_tier == "TIER_3_HALT" or freeze_rem > 0) and not is_recovering)
        bars_since_exit = exit_cooldown.get(sym, account_state.get("exit_bars_ago", {}).get(sym, 999))
        reentry_cooldown_bars = getattr(args, "reentry_cooldown_bars", 2)
        #: (D6) Stale-input-lot verdict for THIS row (None for the cash leg / a clean row).
        stale_input_lot: Optional[Dict[str, Any]] = None

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

            # (M1.1 / Stage C) The two OPT-IN policy gates need bar anchors this
            # loop never carried: a 20-bar MA of close and a 20-bar VWAP (gate 2b's
            # structural floor / gate 3b's reclaim leg), plus a VAL level ONLY if
            # the loaded frame actually has one. They are derived by the SAME
            # helper the backtest simulator uses (`derive_lifecycle_context`), from
            # the SAME frame this loop just priced -- so the ticket and the
            # backtest cannot disagree on a trigger. Warmup / missing data decode
            # to `None` (both gates then no-op); nothing is ever fabricated, and
            # `None` is never read as 0.0.
            #
            # The four values are computed even while both gates are OFF (their
            # defaults): the derivation is a pure trailing window, so it cannot
            # move a decision by itself, and deriving them here means the ON path
            # has no separate code path to rot.
            #
            # `bar=None` (the frame's LAST row) is the as-of session, not a
            # look-ahead: `as_of_universe(universe, as_of_date)` has already
            # truncated every frame to `index <= as_of_date`, and `latest_close`
            # above prices the very same row.
            anchor_ctx = lifecycle_context_at(
                derive_lifecycle_context_from_frame(
                    universe.get(sym),
                    vwap_lookback=int(getattr(lifecycle_cfg, "reentry_vwap_lookback", 20) or 20),
                )
            )

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
                    # (M4.3) A real recorded cost basis supersedes a cold-start
                    # approximation; carrying the flag afterwards would keep
                    # claiming a stop was anchored on a guess.
                    state.entry_price_approx = False
                if state.entry_price > 0:
                    state.peak_price = max(state.peak_price, state.entry_price, price if current_shares > 0 else 0.0)
            else:
                cur_sh = current_shares
                is_held = (cur_sh > 0) or (cur_w > 1e-6)
                held_b = holding_bars.get(sym, 1 if is_held else 0)
                # ---- M4.3 COLD START (the ONLY cold start since M4.4 D3) -----
                # There is NO persisted lifecycle state for this symbol, so there is
                # no history to resume. The deleted legacy hydration branch
                # FABRICATED one: `HELD_GESTATION` from a broker holding-bar guess
                # and `PENDING_ENTRY` from a model signal streak -- states the
                # machine was never actually observed in, which the kernel's first
                # step would then silently credit tenure/confirmation for.
                # The rule itself lives in `cold_start_lifecycle_state` (one
                # implementation, unit-tested): zeroed counters, state restricted to
                # FLAT / HELD_ACTIVE / COOLDOWN, `entry_price` falling back to the
                # current price with `entry_price_approx=True` when no cost basis
                # exists.
                state = cold_start_lifecycle_state(
                    sym,
                    is_held=is_held,
                    held_bars=held_b,
                    bars_since_exit=bars_since_exit,
                    reentry_cooldown_bars=reentry_cooldown_bars,
                    entry_price=float(entry_prices.get(sym, 0.0) or 0.0),
                    price=price,
                    current_weight=cur_w,
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
                # (M4.3) The reconciliation is not a real exit, so it must NOT
                # fabricate a gate-3b anchor (`last_exit_price`), but it does end
                # any structural ladder the phantom holding had accrued.
                state.breakdown_streak = 0
                state.expansion_streak = 0

            # (D6, 2026-10-29 incident) The MIRROR of the reconciliation above, and the
            # one that was missing: the lifecycle machine says the asset is gone (FLAT)
            # while the broker/input book still shows a positive weight.  That is exactly
            # the 600584.SH / 600362.SH residual lot, and it used to pass in silence --
            # the row then looked like a legitimate `HOLD` and no reconciliation, NAV or
            # ticket reader had a reason to complain.  Warn loudly, unconditionally.
            if cur_w > 1e-6 and state.state == PositionState.FLAT:
                print(
                    f"[WARN/账户不一致] {sym}: 生命周期状态=FLAT 但券商权重仍为正 "
                    f"(broker_weight={cur_w:.4%}, current_shares={current_shares}); "
                    f"疑似离场后残仓，请核对账本。"
                )

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
                is_portfolio_deleverage=is_portfolio_deleverage,
                ma20_close=anchor_ctx["ma20_close"],
                vwap20=anchor_ctx["vwap20"],
                vol_ma20=anchor_ctx["vol_ma20"],
                val=anchor_ctx["val"],
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
            is_china_t1_gestation = (asset_market == "china" and state.held_bars < 1)
            should_block_tenure = is_china_t1_gestation or (is_tenure_locked and not is_portfolio_deleverage)

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
            # (L2.40) When portfolio deleveraging is active (`is_portfolio_deleverage`),
            # partial trims are permitted provided China T+1 gestation (`held_bars >= 1`)
            # is satisfied.
            if should_block_tenure and asset_market == "china" and action == "SELL" and tgt_w > 0:
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

            # (D6, 2026-10-29 incident) STALE-INPUT-LOT guard.  600584.SH / 600362.SH were
            # sold out of the model but kept 100-share residuals in the operator's input
            # book.  The lifecycle machine was already FLAT, and the inertia filter compares
            # the target against the residual's OWN weight (2.49% < 5%), so the row was
            # emitted as `HOLD` with `shares: 0` every single day and the lot could never be
            # liquidated: a position the model no longer wants, permanently stuck in the
            # book.  Detection + annotation is unconditional; the SELL itself needs
            # `--liquidate-stale-input-lots` (and survives the
            # `LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION=1` kill-switch), so the default
            # behaviour cannot change a single order.
            #
            # `raw_tgt_w` (the pre-lifecycle model target) is the witness, NOT `tgt_w`: on
            # the inertia path the lifecycle machine rewrote `tgt_w` to `state.current_weight`,
            # which is the residual's own weight and therefore hides the fact that the model
            # asked for zero.
            #
            # (2026-10-29 review, finding 1) SCOPE GATE: only rows the lifecycle did NOT
            # already decide to liquidate may be assessed. The risk-exit paths in
            # `common/position_lifecycle.py` (hard stop / ATR trailing stop / Tier-3
            # emergency halt / confirmed normal exit) all move the state to COOLDOWN
            # BEFORE returning the SELL, i.e. `state.is_holding()` is False and
            # `current_shares > 0` after the call -- shape-identical to a residual lot.
            # Without this gate the guard appended "本行不下单" to a row that DOES place an
            # order, and under `--liquidate-stale-input-lots` it rewrote the exit reason to
            # `STALE_LOT_REASON | 原信号: <stop-loss/circuit-breaker>`, so grepping the
            # badge for residual-lot cleanups also matched real risk events.
            # Both the machine's own verdict (`decision.action`) and the effective row
            # action are consulted: any later gate (tenure lock) that downgrades a SELL
            # cannot resurrect the annotation either.
            lifecycle_liquidates = is_liquidation_event(
                decision.action
            ) or is_liquidation_event(action)
            if not lifecycle_liquidates:
                stale_input_lot = assess_stale_input_lot(
                    current_shares=current_shares,
                    model_target=raw_tgt_w,
                    is_holding=state.is_holding(),
                    lifecycle_action=action,
                    enforce=stale_lot_liquidation_enabled(
                        getattr(args, "liquidate_stale_input_lots", False)
                    ),
                    price=price,
                )
            if stale_input_lot and stale_input_lot["detected"]:
                mode = "已启用强制清出" if stale_input_lot["enforced"] else "告警模式（未启用 --liquidate-stale-input-lots，本行不下单）"
                print(
                    f"[WARN/残仓] {sym}: 模型目标为 0 且生命周期状态={state.state.value}，"
                    f"但输入账本仍有 {current_shares} 股残仓；惰性阈值不会清出它。{mode}"
                )
                if stale_input_lot["enforced"]:
                    # A real liquidation is never labelled as blocked (L2.24): the row's
                    # structured verdict must be NONE, exactly like the lot-rounding
                    # liquidation path below.
                    action = "SELL"
                    tgt_w = 0.0
                    delta_w = -cur_w
                    abs_delta = abs(delta_w)
                    reason = (
                        f"{stale_input_lot['reason']} | 原信号: {reason}"
                        if reason and reason != stale_input_lot["reason"]
                        else stale_input_lot["reason"]
                    )
                    blocking_reason = BlockingReason.NONE
                else:
                    # Warn-only: keep the row exactly as the gates decided it, but make the
                    # condition impossible to miss in the printed/CSV ticket.
                    reason = f"{reason} {stale_input_lot['reason']}".strip() if reason else stale_input_lot["reason"]

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
        # (`AT7` P1/P2/P3) Advisory-only audit key: same shape on every symbol, never
        # read back by any gate or sizing function (P2 carries the shadow `r1` /
        # `shadow_buy_scale` line; P3 flips `r1_effective` and adds
        # `effective_buy_scale` when --enforce-r1-derisk actually applied).
        # Strictly JSON-native (no numpy/pandas scalars) because the ticket writer
        # uses `json.dump(..., ensure_ascii=False)` without `default=str` /
        # `allow_nan=False`.
        detail_reason["advisory"] = advisory_block
        if fast_path_badge:
            detail_reason["fast_path_badge"] = fast_path_badge
            detail_reason["fast_path_triggered"] = decision.fast_path_triggered
        # (`R6`, rule r6.3) The overnight-de-risk verdict travels WITH the row, so a reader that
        # only keeps `detail_reason` can still tell TRIGGERED / NOT_TRIGGERED / INSUFFICIENT_DATA
        # apart (the last one carries `ratio_applied == 1.0` too -- `status` + `reason` are the
        # discriminators, exactly as the freeze document §9 requires).
        _overnight_market = overnight_derisk_row_market.get(sym)
        if _overnight_market is not None:
            detail_reason["overnight_derisk"] = overnight_derisk_by_market[_overnight_market]
        # (D6, 2026-10-29 incident) The structured half of the residual-lot annotation: a
        # reader that only keeps `detail_reason` (audit tooling, the UI's row inspector)
        # must still be able to tell "warned but not enforced" from "sold it".
        if stale_input_lot and stale_input_lot.get("detected"):
            detail_reason["stale_input_lot"] = {
                "detected": True,
                "enforced": bool(stale_input_lot.get("enforced")),
                "shares": stale_input_lot.get("shares"),
                "model_target_weight": round(float(stale_input_lot.get("model_target") or 0.0), 6),
                "lifecycle_state": state.state.value if sym != cash_proxy else None,
                "badge": STALE_LOT_LIQUIDATION_BADGE,
            }

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
            # (D4, 2026-11-09 evidence) Two additive columns, deliberately placed HERE and
            # NOT appended at the end: `blocking_reason` must stay the TRAILING column of
            # the CSV (pinned by the L2.24 regression test), so the new audit fields are
            # inserted next to the weight they describe.
            #   * `target_weight_model` -- this row's target coordinate as the ticket froze it:
            #     it has ALREADY passed the Stage-1 intraday gate, the Stage-1 vol ceiling
            #     (`equity_scale_factor`) and `step_position_lifecycle` / the minimum-entry
            #     filter, and has NOT yet been lot-rounded / delivered-capped. (2026-11-12
            #     defect B: the pre-fix comment called it "the model's ORIGINAL target ...
            #     frozen before ... the delivered cap", which was wrong on both ends -- it is
            #     computed AFTER the cap, so dividing by `equity_scale_factor` is what
            #     recovers the cap-front model value, and AFTER the lifecycle pass.)
            #   * `target_weight_effective` -- what the ticket actually DELIVERS
            #     (`target_shares * price / NAV`), stamped by
            #     `reconcile_delivered_weight_budget` below.
            # `target_weight` itself keeps its pre-D4 meaning.
            "target_weight_model": float(tgt_w),
            "target_weight_effective": None,
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

    # (D4, evidence 2026-11-09/204956) FIRST: the buy side of the model's book must fit
    # inside one NAV before anything downstream is sized from it.  The archive's ticket
    # summed to `Σ target_weight = 1.028337` -- a book claiming 102.8% of the account.
    # Proportional normalization happens HERE (before the purchasing-power rescale and the
    # delivered-cap pass), so every later stage works on a book that already fits and the
    # ticket records `weights_normalized` + the factor it used instead of shipping 102.8%.
    # SELL/HOLD/CANCEL_PENDING targets are deliberately NOT rescaled -- see the function's
    # docstring: that would rewrite an order (or a freeze's carried weight) the model never
    # asked to change.
    weight_budget = normalize_model_target_weights(
        rows,
        portfolio_value=args.portfolio_value,
        cash_proxy=cash_proxy,
        lot_size_fn=lambda s: resolve_lot_size(s, args.lot_size),
    )
    if weight_budget["weights_normalized"]:
        print(
            f"[WARN/WEIGHT BUDGET] 模型目标权重合计 (买入侧+现金) {weight_budget['target_weight_sum_model']:.6f} > 1，"
            f"已按系数 {weight_budget['weight_normalization_factor']:.6f} 等比归一"
            + (
                "（部分买入行归一后已跌破当前持仓，按 0 股登记而不是反向卖出）"
                if weight_budget["weight_normalization_partial"]
                else ""
            )
        )

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

    # (D4) SECOND: stamp the DELIVERED weight of every row and make the book fit one NAV.
    # Whole-lot rounding rounds UP as often as down (299.04 raw shares -> 200 delivered,
    # i.e. 20.00% stated vs 17.17% delivered), and it can also push the total over 100%.
    # Shaving is only ever applied to rows that would BUY MORE (never to a HOLD whose drift
    # is already held, and never by inventing a SELL) and always starts with the SMALLEST
    # delivered position, which is where lot-rounding amplification comes from.
    _lot_fn = lambda s: resolve_lot_size(s, args.lot_size)
    weight_budget = merge_weight_budget_summaries(
        weight_budget,
        reconcile_delivered_weight_budget(
            rows,
            portfolio_value=args.portfolio_value,
            cash_proxy=cash_proxy,
            lot_size_fn=_lot_fn,
        ),
    )
    if weight_budget["rounding_rebalanced"]:
        print(
            f"[WARN/WEIGHT BUDGET] 整手取整后合计权重超 100%，已从最小权重标的削减整手数: "
            f"{', '.join(str(s) for s in weight_budget['shaved_rows'])}"
        )

    # (D7, 2026-10-29 incident) DELIVERED market-value cap, evaluated AFTER lot rounding
    # (including the purchasing-power rescale above, which re-rounds every BUY).  The
    # model's 20% clamp in `blend_base.py` works on the RAW weight, so the quantity that
    # actually reaches the ticket could still deliver 20.28% / 20.01% of NAV.  Warn-only
    # by default: every breach is annotated (MV_CAP) and printed, but an order is only
    # changed under `--enforce-mv-cap` (and never when
    # `LIVETRADING_DISABLE_MV_CAP=1`).  `--max-single-position` is the cap value.
    mv_cap = float(getattr(args, "max_single_position", DEFAULT_MAX_SINGLE_POSITION) or DEFAULT_MAX_SINGLE_POSITION)
    mv_cap_enforced = mv_cap_enforcement_enabled(getattr(args, "enforce_mv_cap", False))
    for r in rows:
        if r["symbol"] == cash_proxy:
            continue
        apply_market_value_cap(
            r,
            portfolio_value=args.portfolio_value,
            max_weight=mv_cap,
            lot_size=resolve_lot_size(r["symbol"], args.lot_size),
            enforce=mv_cap_enforced,
        )
        _mv_block = (r.get("detail_reason") or {}).get("mv_cap") or {}
        if _mv_block.get("breached"):
            if _mv_block.get("clamped"):
                _tail = f"，已按整手回收至 {_mv_block.get('post_clamp_market_value_weight'):.2%}"
            else:
                _tail = "，未启用 --enforce-mv-cap 故不改单"
            print(
                f"[WARN/市值上限] {r['symbol']}: 交付市值权重 "
                f"{_mv_block.get('market_value_weight'):.2%} > 上限 {mv_cap:.2%}{_tail}"
            )
            # (D4) The same verdict in a grep-able, ASCII-tagged line: a breach that only
            # lives inside `detail_reason` was how 2026-11-09's 601919.SH (21.19% vs 20%)
            # stayed invisible to everyone reading the ticket header.
            print(
                f"[WARN/MV CAP] {r['symbol']}: delivered market-value weight "
                f"{_mv_block.get('market_value_weight'):.2%} > cap {mv_cap:.2%} "
                f"(enforced={bool(_mv_block.get('enforced'))}){_tail}"
            )

    # (D4) The cap may have clamped a BUY down (or the purchasing-power rescale re-rounded
    # it), so refresh the delivered stamp on the final quantities.  Idempotent: a book that
    # already fits is only re-stamped, never shaved twice.
    weight_budget = merge_weight_budget_summaries(
        weight_budget,
        reconcile_delivered_weight_budget(
            rows,
            portfolio_value=args.portfolio_value,
            cash_proxy=cash_proxy,
            lot_size_fn=_lot_fn,
        ),
    )
    mv_cap_breaches = collect_mv_cap_breaches(rows)
    if not weight_budget["budget_ok"]:
        print(
            f"[WARN/WEIGHT BUDGET] 票据目标权重合计 {weight_budget['target_weight_sum']:.6f} 仍超出 1.0，"
            f"无买入整手可削减（持仓漂移或无现金代理行），请人工核对。"
        )

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
    # (D6, evidence 2026-11-09/204956) Every rendered ORDER line is tee'd into this buffer
    # and the BUY/SELL count is asserted against the ticket AFTER it is written:
    # `verify_ticket_stdout_order_parity`.  That check exists because PHASE 2 used to print
    # one `🟢 BUY` line per row TWICE (a second `print()` existed only to attach the
    # sub-lot warning), and an operator copying the stdout would have doubled the order.
    ticket_stdout_lines: List[str] = []
    if sells.empty:
        print("  今日无需执行任何卖出指令 (No sell orders required today).")
    else:
        for _, r in sells.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            warn_tag = " ⚠️ [LIMIT_DOWN_ILLIQUID_RISK / 资金已冻结]" if r.get("execution_warning") == "LIMIT_DOWN_ILLIQUID_RISK" else ""
            limit_p = r.get("suggested_limit_price", r["price"])
            print_ticket_order_line(
                f"  🔴 SELL {sym_col} | 现价: {r['price']:>7.2f} (建议限价: {limit_p:.2f}) | "
                f"仓位: {r['current_weight']:>5.1%} -> 目标: {r['target_weight']:>5.1%} "
                f"({r['delta_weight']:>+5.1%}) | 卖出: {abs(r['delta_shares']):>6} 股 "
                f"(~¥{abs(r['trade_value']):>9,.2f}){_delivered_weight_note(r)}{warn_tag}",
                ticket_stdout_lines,
            )

    print("\n[PHASE 2: EXECUTE BUYS SECOND (第二阶段: 买入建仓/加仓资金配置)]")
    if buys.empty:
        print("  今日无需执行任何买入指令 (No buy orders required today).")
    else:
        for _, r in buys.iterrows():
            sym_col = pad_east_asian(f"{r['symbol']} ({r['name']})", 24)
            price_info = f"建议限价: {r.get('suggested_limit_price', r['price']):.2f}"
            if r.get("passive_support_limit_price") and r.get("passive_support_limit_price") != r.get("suggested_limit_price"):
                price_info += f" (被动支撑: {r['passive_support_limit_price']:.2f})"
            # (D6) EXACTLY ONE line per BUY row.  The sub-lot warning and the delivered
            # weight annotation are folded into THIS line -- they used to live in a second
            # `print()` of the same order, which is how the duplicated `🟢 BUY` appeared.
            qty_str = f"{r['delta_shares']:>6}"
            suffix = ""
            if r['delta_shares'] == 0:
                sym_lot = resolve_lot_size(r['symbol'], args.lot_size)
                raw_shares = (r['delta_weight'] * args.portfolio_value) / r['price'] if r['price'] > 0 else 0
                qty_str = f"{'0':>6}"
                suffix = f" ⚠️  低于单手门槛 (不足{sym_lot}股) — 目标需{raw_shares:.0f}股, 1手需¥{sym_lot * r['price']:,.0f}"
            eff_note = _delivered_weight_note(r)
            print_ticket_order_line(
                f"  🟢 BUY  {sym_col} | 现价: {r['price']:>7.2f} | 仓位: {r['current_weight']:>5.1%} "
                f"-> 目标: {r['target_weight']:>5.1%} ({r['delta_weight']:>+5.1%}) | "
                f"买入: {qty_str} 股 (~¥{r['trade_value']:>9,.2f}) | {price_info}"
                f"{eff_note}{suffix}",
                ticket_stdout_lines,
            )

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
                        # (M4.3) A fresh entry invalidates any breakdown ladder
                        # accrued while flat; the kernel's gate 2b counts only
                        # consecutive held bars. Inert for the legacy engine.
                        updated_lifecycle_states[sym]["breakdown_streak"] = 0
                        updated_lifecycle_states[sym]["expansion_streak"] = 0
                        if p > 0:
                            updated_lifecycle_states[sym]["entry_price"] = p
                            updated_lifecycle_states[sym]["peak_price"] = p
                            # A real fill replaces any cold-start approximation.
                            updated_lifecycle_states[sym]["entry_price_approx"] = False
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
                    # (M4.3) A real, closed exit ends any in-progress structural
                    # breakdown/expansion ladder, and its close becomes the
                    # anchor `price_reentry_gate` (gate 3b) tests against. These
                    # keys are inert for the legacy engine (which reads neither),
                    # so writing them cannot change an OFF-path decision.
                    updated_lifecycle_states[sym]["breakdown_streak"] = 0
                    updated_lifecycle_states[sym]["expansion_streak"] = 0
                    if p > 0:
                        updated_lifecycle_states[sym]["last_exit_price"] = float(p)
                    if not updated_lifecycle_states[sym].get("last_exit_type"):
                        updated_lifecycle_states[sym]["last_exit_type"] = ExitType.NORMAL.value
            elif cur_sh == 0 and tgt_sh == 0:
                dual_holding_bars.pop(sym, None)
                dual_entry_prices.pop(sym, None)

    # (D4, evidence `2026-11-09/204956`) Persist the DELIVERED weight, not the model's wish.
    # The archive's `account_state.lifecycle_states["600660.SH"]` carried
    # `current_weight = 0.2` (the model target) while the ticket could only deliver
    # `0.171663` (200 shares x 56.62 / NAV); nothing recorded the 17.17% that the book would
    # actually hold, so the next round's inertia / min-entry / single-name-cap gates all
    # compared against a weight that could never exist. `target_weight_model` keeps the
    # original number for audit, `current_weight` becomes what is deliverable.
    # Runs AFTER the cooldown/exit reconciliation above on purpose: a real liquidation has
    # already zeroed `current_weight` there, and its delivered weight IS 0.0, so this loop
    # cannot resurrect a phantom position.
    if "target_weight_effective" in order_df.columns:
        for _rec in order_df.to_dict(orient="records"):
            _sym = _rec.get("symbol")
            if _sym == cash_proxy:
                continue
            _st = updated_lifecycle_states.get(_sym)
            if not isinstance(_st, dict):
                continue
            _eff = _rec.get("target_weight_effective")
            if _eff is None:
                continue
            try:
                _eff_f = float(_eff)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(_eff_f):
                continue
            _st["current_weight"] = round(_eff_f, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
            _model = _rec.get("target_weight_model")
            if _model is not None:
                try:
                    _st["target_weight_model"] = round(float(_model), TARGET_WEIGHT_EFFECTIVE_DECIMALS)
                except (TypeError, ValueError):
                    pass

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
    # (LV2) The ticket is written as the versioned ENVELOPE carrying `execution_session` /
    # `price_reference` next to the order rows. Built OUTSIDE the try/except on purpose: a
    # timing block that is missing or out of domain must ABORT generation (no defaulting),
    # whereas an I/O failure stays the pre-existing warning. Readers must use
    # `common.live_execution_timing.ticket_order_rows` -- every archived ticket is still a
    # bare list, and a reader that assumes one shape silently sees "no ticket" for the other.
    ticket_payload = build_ticket_payload(order_df.to_dict(orient="records"), timing)
    # (D4, evidence `2026-11-09/204956`) BOOK-LEVEL verdicts at the ticket's TOP level, so a
    # reader never has to re-derive them from the rows and never has to open the per-row
    # `detail_reason` block:
    #   * `weights_normalized` (+ factor) -- the model's raw targets summed above 100% and
    #     were scaled proportionally instead of the ticket claiming 102.8% of NAV;
    #   * `target_weight_sum` / `target_weight_sum_model` -- delivered (post lot-rounding,
    #     cash-proxy residual included) vs `normalize_model_target_weights`' own sum, which
    #     counts ONLY the legs it is allowed to rewrite (BUY rows + the cash proxy) at the
    #     moment BEFORE normalization and BEFORE lot rounding -- NOT the model's whole book
    #     (SELL/HOLD rows are excluded by contract).  (2026-11-12 defect B corrected this
    #     comment: it used to call that partial sum "the model's own sum".)  The delivered one
    #     never exceeds `1 + 1e-9`;
    #   * `rounding_rebalanced` -- whole lots had to be shaved off the smallest positions to
    #     keep the delivered book inside one NAV;
    #   * `mv_cap_breaches` -- EVERY delivered single-name cap breach, so a 21.19%-against-
    #     20% breach can no longer hide inside `detail_reason.mv_cap`.
    ticket_payload["weights_normalized"] = bool(weight_budget.get("weights_normalized"))
    ticket_payload["weight_normalization_factor"] = float(
        weight_budget.get("weight_normalization_factor") or 1.0
    )
    ticket_payload["target_weight_sum"] = weight_budget.get("target_weight_sum")
    ticket_payload["target_weight_sum_model"] = weight_budget.get("target_weight_sum_model")
    #: `False` only when the book could NOT be brought inside 100% without inventing an
    #: order (e.g. held drift already above NAV): the WARN line was printed, and both the
    #: ticket and the archive say so instead of quietly claiming a fitting book.
    ticket_payload["weight_budget_ok"] = bool(weight_budget.get("budget_ok", True))
    ticket_payload["rounding_rebalanced"] = bool(weight_budget.get("rounding_rebalanced"))
    ticket_payload["mv_cap_breaches"] = mv_cap_breaches
    # (LV18, 2026-11-12) Stage-1 Barroso ceiling, archived next to the other book-level
    # verdicts. Present ONLY for the allowlisted strategy with the overlay enabled -- every
    # other strategy's ticket is byte-identical to before this change.
    #   {"applied", "mode", "realized_vol_21d", "vol_scaling_factor",
    #    "model_equity_sum", "pre_cap_equity_sum", "pre_gate_equity_sum",
    #    "capped_equity_sum", "equity_scale_factor", "binding",
    #    "reason", "superseded_by_circuit_breaker", "post_lifecycle_equity_sum",
    #    "lifecycle_filtered_equity_sum", "lifecycle_filtered_reasons",
    #    "lifecycle_attribution_complete", "delivered_equity_sum",
    #    "ceiling_exceeded_by", "delivery_shortfall", "delivery_shortfall_ratio",
    #    "lifecycle_shortfall", "lifecycle_shortfall_ratio"}
    # `binding=True` means the cap actually reduced the equity targets (the difference is
    # parked in the cash proxy); `applied=True` with `reason="vol_unavailable"` means Stage-1
    # produced no MEASURED factor and the book was deliberately left untouched (never
    # defaulted to a 1.0x multiplier); `superseded_by_circuit_breaker=True` means a Stage-1
    # breaker (Tier-3 halt) had ALREADY emptied the book, so the ceiling is not the reason it
    # is flat -- never read `capped_equity_sum=0` as "the vol cap cleared the book".
    # (2026-11-12 defect B) The equity chain, in order: `pre_gate_equity_sum` (before the
    # intraday distribution gate) -> `model_equity_sum` == `pre_cap_equity_sum` (post-gate,
    # pre-cap) -> `capped_equity_sum` -> `post_lifecycle_equity_sum` (after
    # `step_position_lifecycle` + the min-entry filter) -> `delivered_equity_sum` (post
    # lot-rounding, `Σ target_weight_effective`). `lifecycle_filtered_equity_sum` =
    # `capped - post_lifecycle`, attributed per reason code in `lifecycle_filtered_reasons`
    # ({code: {"symbols", "equity_sum"}}). `ceiling_exceeded_by` reports how far whole lots
    # pushed delivery past the ceiling; `delivery_shortfall` flags lot rounding delivering
    # only a sliver of the POST-LIFECYCLE book, while `lifecycle_shortfall` flags a ceiling
    # mostly eaten by the lifecycle pass instead -- they are distinct findings and the two
    # ticket flags (`vol_overlay_delivery_ok` / `vol_overlay_lifecycle_ok`) mirror them
    # separately, leaving `weight_budget_ok` (a 100%-budget verdict) untouched.
    if vol_overlay is not None:
        _delivered_equity = sum(
            _row_weight(r, "target_weight_effective")
            for r in rows
            if str(r.get("symbol") or "") != str(cash_proxy)
        )
        vol_overlay["delivered_equity_sum"] = round(_delivered_equity, TARGET_WEIGHT_EFFECTIVE_DECIMALS)
        if vol_overlay.get("capped_equity_sum") is not None:
            vol_overlay["ceiling_exceeded_by"] = round(
                max(0.0, _delivered_equity - float(vol_overlay["capped_equity_sum"])),
                TARGET_WEIGHT_EFFECTIVE_DECIMALS,
            )
        else:
            vol_overlay["ceiling_exceeded_by"] = None
        # (2026-11-12 defect B) ATTRIBUTION: the chain is
        #   pre_gate  -> (intraday distribution gate) -> model/pre_cap
        #             -> (Stage-1 vol ceiling)           -> capped
        #             -> (lifecycle + min-entry filter)  -> post_lifecycle
        #             -> (whole-lot rounding)            -> delivered.
        # Only `post_lifecycle` to `delivered` is genuine lot-rounding loss; the old
        # `delivery_shortfall_ratio` divided by `capped` and therefore stayed quiet while a
        # third of the ceiling was eaten by the lifecycle pass (archived 2026-11-12/162857:
        # capped 0.3894, post-lifecycle 0.2610, delivered 0.2507 -> old ratio 0.64 "OK").
        # `rows` here is the FULL pre-drop list (NO_POSITION rows included), so the equity sum
        # below is the model's post-lifecycle target, not the ticket's surviving subset.
        _post_lifecycle_equity = 0.0
        _lifecycle_filtered_gap = 0.0
        _lifecycle_reasons: Dict[str, Any] = {}
        for _r in rows:
            _sym = str(_r.get("symbol") or "")
            if _sym == str(cash_proxy):
                continue
            _row_target = max(0.0, _row_weight(_r, "target_weight"))
            _post_lifecycle_equity += _row_target
            try:
                _pre_lifecycle = max(0.0, float(target_series.get(_sym, 0.0) or 0.0))
            except (TypeError, ValueError):
                _pre_lifecycle = 0.0
            _gap = _pre_lifecycle - _row_target
            if _gap <= 0.0:
                continue
            _lifecycle_filtered_gap += _gap
            _action = str(_r.get("action") or "").strip().upper()
            if _action == "NO_POSITION":
                _code = "no_position"
            else:
                _typed = coerce_blocking_reason(_r.get("blocking_reason"))
                _code = (
                    str(_typed.value).lower()
                    if _typed is not BlockingReason.NONE
                    else f"action_{_action.lower() or 'unknown'}"
                )
            _bucket = _lifecycle_reasons.setdefault(
                _code, {"symbols": 0, "equity_sum": 0.0}
            )
            _bucket["symbols"] += 1
            _bucket["equity_sum"] = round(
                _bucket["equity_sum"] + _gap, TARGET_WEIGHT_EFFECTIVE_DECIMALS
            )
        vol_overlay["pre_gate_equity_sum"] = round(
            _pre_distribution_gate_equity_sum, TARGET_WEIGHT_EFFECTIVE_DECIMALS
        )
        vol_overlay["post_lifecycle_equity_sum"] = round(
            _post_lifecycle_equity, TARGET_WEIGHT_EFFECTIVE_DECIMALS
        )
        if vol_overlay.get("capped_equity_sum") is not None:
            vol_overlay["lifecycle_filtered_equity_sum"] = round(
                max(
                    0.0,
                    float(vol_overlay["capped_equity_sum"]) - _post_lifecycle_equity,
                ),
                TARGET_WEIGHT_EFFECTIVE_DECIMALS,
            )
        else:
            vol_overlay["lifecycle_filtered_equity_sum"] = None
        vol_overlay["lifecycle_filtered_reasons"] = _lifecycle_reasons
        # Cross-check: independently measured per-row gaps should equal capped - post. A
        # mismatch means a symbol in the target vector never produced a row (the caps are
        # still the authoritative headline number, this flag is just honest about it).
        vol_overlay["lifecycle_attribution_complete"] = bool(
            vol_overlay.get("lifecycle_filtered_equity_sum") is not None
            and abs(
                float(vol_overlay["lifecycle_filtered_equity_sum"]) - _lifecycle_filtered_gap
            )
            <= 1e-6
        )
        # (LV18 item 2 + 2026-11-12 defect B) Delivery-shortfall guardrail now splits into
        # two legs: lifecycle attrition (denominator = capped) and genuine whole-lot loss
        # (denominator = post_lifecycle). Report-only: weights, lot rounding and
        # `weight_budget_ok` are all left alone -- an operator sees the gap, the runner never
        # invents exposure to close it.
        _delivery = evaluate_vol_overlay_delivery(
            vol_overlay.get("capped_equity_sum"),
            _delivered_equity,
            post_lifecycle_equity_sum=vol_overlay.get("post_lifecycle_equity_sum"),
            binding=bool(vol_overlay.get("binding")),
        )
        vol_overlay["delivery_shortfall"] = _delivery["delivery_shortfall"]
        vol_overlay["delivery_shortfall_ratio"] = _delivery["delivery_shortfall_ratio"]
        vol_overlay["lifecycle_shortfall"] = _delivery["lifecycle_shortfall"]
        vol_overlay["lifecycle_shortfall_ratio"] = _delivery["lifecycle_shortfall_ratio"]
        ticket_payload["vol_overlay_delivery_ok"] = _delivery["delivery_ok"]
        ticket_payload["vol_overlay_lifecycle_ok"] = _delivery["lifecycle_ok"]
        if _delivery["lifecycle_shortfall"]:
            print(
                format_vol_overlay_lifecycle_warning(
                    float(vol_overlay["capped_equity_sum"]),
                    float(vol_overlay["post_lifecycle_equity_sum"]),
                    vol_overlay.get("lifecycle_filtered_reasons"),
                )
            )
        if _delivery["delivery_shortfall"]:
            print(
                format_vol_overlay_delivery_warning(
                    float(vol_overlay["capped_equity_sum"]),
                    _delivered_equity,
                    vol_overlay.get("post_lifecycle_equity_sum"),
                )
            )
        ticket_payload["vol_overlay"] = vol_overlay
    # (D6, evidence `2026-11-09/204956`) MACHINE CHECK, archived with the ticket: one printed
    # order line per order. PHASE 2 used to render the same BUY twice (one `orders` entry),
    # i.e. an operator copying the stdout would have doubled the position.
    ticket_payload["ticket_stdout_parity"] = {
        action: verify_ticket_stdout_order_parity(
            ticket_order_rows(ticket_payload), "\n".join(ticket_stdout_lines),
            action=action, marker=marker,
        )
        for action, marker in (
            ("BUY", TICKET_BUY_STDOUT_MARKER),
            ("SELL", TICKET_SELL_STDOUT_MARKER),
        )
    }
    try:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(ticket_payload, f, indent=2, ensure_ascii=False)
        # Beside the ticket and merged into summary.json, so a consumer that only knows the
        # pre-LV2 list shape still has a machine-readable place to read the timing from.
        write_execution_timing(args.output_dir, timing)
    except Exception as e:
        print(f"[WARN] Failed to write live_trading_ticket.json: {e}")
    print("\n" + "=" * 110)
    print(f"[SUCCESS] 交易指令单已导出至: {out_csv} 及 {out_json}")

    # Save post_holdings.json if output_dir is given
    # (D5, evidence `2026-11-11/205137`) `post_holdings.json` alone cannot tell "the book is
    # EMPTY" apart from "the file was never produced": the archive above holds `{}` because
    # the day fully liquidated, which is the CORRECT result, yet every reader saw the same
    # thing as a missing file. The shape of `post_holdings.json` is therefore unchanged
    # (`{symbol: shares}`, `{}` = flat) and a sidecar `post_holdings.meta.json` now states
    # completeness explicitly. Its schema:
    #   {"as_of": "YYYY-MM-DD", "complete": bool, "n_symbols": int,
    #    "generated_at": ISO-8601 local, "source": "ticket"|"replay"}
    # `complete: true` + `{}` = a confirmed all-cash book (a legal seed); a missing file or a
    # missing/false `complete` = UNUSABLE (readers must return None, never "flat").
    post_holdings_path = os.path.join(args.output_dir, "post_holdings.json")
    post_holdings_written = False
    try:
        with open(post_holdings_path, "w", encoding="utf-8") as f:
            json.dump(post_holdings, f, indent=2, ensure_ascii=False)
        post_holdings_written = True
        print(f"[SUCCESS] 预测持仓已导出至: {post_holdings_path}")
    except Exception as e:
        print(f"[WARN] Failed to write post_holdings.json: {e}")
    post_holdings_meta_path = os.path.join(args.output_dir, POST_HOLDINGS_META_FILENAME)
    try:
        post_holdings_meta = {
            "as_of": str(as_of_date),
            "complete": bool(post_holdings_written),
            "n_symbols": len([
                s for s, q in (post_holdings or {}).items() if int(q or 0) > 0
            ]),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            # A pinned past session is a REPLAY (`resolve_run_mode`); its book is a
            # reconstruction, not a fill plan, and the sidecar says so.
            "source": "replay" if str(run_mode) == "replay" else "ticket",
        }
        with open(post_holdings_meta_path, "w", encoding="utf-8") as f:
            json.dump(post_holdings_meta, f, indent=2, ensure_ascii=False)
        print(
            f"[SUCCESS] 预测持仓元数据已导出至: {post_holdings_meta_path} "
            f"(complete={post_holdings_meta['complete']}, n_symbols={post_holdings_meta['n_symbols']})"
        )
    except Exception as e:
        print(f"[WARN] Failed to write {POST_HOLDINGS_META_FILENAME}: {e}")

    # Persist updated state into account_state.json if available
    if args.account_state_file:
        # (LV7, `docs/lv7_prior_state_contract.md` §5.3) Re-assert the version parity
        # IMMEDIATELY before the write: the state object is mutated throughout this
        # function, so the early check is not sufficient proof of what would land on disk.
        # Same read-only predicate, no auto-heal -- and note this block never rewrites a
        # MISMATCHED version, it only re-stamps today's (already-verified) values.
        #
        # (2026-11-12 seeding race) The clobber guard runs FIRST for the same reason as the
        # early call: a state dated today with an older triple is the seeding defect, not a
        # stale dump, and that distinction must reach the operator.
        assert_state_version_not_clobbered(
            account_state,
            _live_strategy_version,
            _live_base_version,
            as_of_date=as_of_date,
            state_display=args.account_state_file,
        )
        assert_account_state_version_parity(
            account_state,
            _live_strategy_version,
            _live_base_version,
            state_display=args.account_state_file,
        )
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

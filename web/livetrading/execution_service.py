"""Execution service for invoking Stage 1 Health Check and Stage 2 Live Deployment.

Runs scripts via isolated Python subprocesses with live output capture and automatic archival.

**Valuation date ownership (L2.3 / C6 fix).** The web layer no longer pushes
``--as-of-date=today`` at the child scripts.  ``date.today()`` is the *host's*
calendar day, which has no relationship to the market's session: it is a
non-trading weekend day on Saturdays, and during the session it names a bar that
does not exist yet.  ``common/cli_utils.py::resolve_live_clock_context`` (the single
owner of the as-of decision, `L2.3`) now resolves the valuation session itself from
``common/market_clock.py`` whenever ``--as-of-date`` is omitted, so the web omits it
and keeps only its *own* need for a date -- naming the run directory -- fed from the
same clock (``_market_clock_val_date``).  An explicitly requested as-of (a replay of
a specific session) is still forwarded verbatim, and validated by that same gate in
the child process, never here: duplicating the trading-day/close-publication rules in
the web layer is exactly how the two sides drift apart.

**Actionable failures (the other half of C6).** The child runner lets
``RunWindowError`` / ``StalePriceError`` / as-of ``ValueError`` propagate as a
traceback, so the only copy of "why" used to live in raw ``stderr`` while the UI
showed ``Execution failed with code N``.  ``summarize_execution_failure`` now
classifies ``stderr`` into an operator-facing ``error_kind`` / ``error_headline`` /
``error_hint`` and folds them into the human-readable ``message`` the UI renders,
while the raw ``stderr`` is preserved untouched in both the response and
``summary.json``.

**Observability + 0-row intraday (`L2.4`).** ``summary.json`` carries design §9's
clock block (tz-aware UTC, market-local, session, as-of), the stage scripts'
``freshness_summary`` and this layer's own ``observability.intraday_5m`` verdict --
merged, never overwritten.  A 5m universe with ZERO real rows on the as-of session
yields ``enabled=False`` plus a WARNING (case 11), and the affected symbols are
named inside ``freshness_summary.intraday_5m`` so "no intraday data" can never be
mistaken for "the intraday gates passed".

**One archive date key (`L2.4` / `L2.5`).** ``StorageManager.market_clock_date`` is
the only date-key resolver in the web trio; ``_assert_archive_keyed_by`` enforces
that the run directory really is keyed by the valuation session it writes into
``summary.json``.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import pandas as pd

try:
    from common.universe import get_stock_name, is_placeholder_stock_name
except ImportError:
    try:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        from common.universe import get_stock_name, is_placeholder_stock_name
    except Exception:
        def get_stock_name(sym: str, default: Optional[str] = None) -> str:
            return default if default is not None else sym
        def is_placeholder_stock_name(name: object, symbol: object = None) -> bool:
            n = "" if name is None else str(name).strip()
            return n.lower() in ("custom", "universe", "none", "unknown", "nan", "") or (
                symbol is not None and n == str(symbol).strip()
            )

try:
    from .storage import (
        PHANTOM_REFERENCE_ENTRY,
        HoldingsInputMismatchError,
        StorageManager,
        diff_holdings_input_vs_prev_post,
        diff_reference_book_vs_lifecycle,
        extract_lifecycle_states,
        format_holdings_input_mismatch,
        format_phantom_reference_entries,
        is_cash_symbol,
        is_instance_dir_name,
        parse_ticket_csv,
        post_holdings_from_ticket,
        strict_holdings_input_enabled,
    )
except (ImportError, ValueError):
    from storage import (
        PHANTOM_REFERENCE_ENTRY,
        HoldingsInputMismatchError,
        StorageManager,
        diff_holdings_input_vs_prev_post,
        diff_reference_book_vs_lifecycle,
        extract_lifecycle_states,
        format_holdings_input_mismatch,
        format_phantom_reference_entries,
        is_cash_symbol,
        is_instance_dir_name,
        parse_ticket_csv,
        post_holdings_from_ticket,
        strict_holdings_input_enabled,
    )

try:
    from .valuation import MissingCashError, StalePriceError, compute_nav
except (ImportError, ValueError):
    # The web app imports this module by bare name after putting `web/livetrading` on
    # sys.path (`server.py` / `app.py`), so the package-relative form is not always valid.
    from valuation import MissingCashError, StalePriceError, compute_nav


# --------------------------------------------------------------------------- #
# (C6) Actionable failure classification for the child runner's stderr
# --------------------------------------------------------------------------- #
#: ``kind`` -> stdout-visible headline -> operator hint, in *match priority* order.
#:
#: The markers are the literal strings `common/cli_utils.py` (run-window gate /
#: as-of gate) and `common/freshness.py` (stale price) put in the exception message,
#: i.e. the last line of the child's traceback. They are matched as plain
#: substrings (never parsed as data) so a reworded message degrades to
#: ``error_kind="unknown"`` -- still showing the raw traceback -- instead of
#: mislabelling the failure.
_FAILURE_RULES: Tuple[Tuple[str, Tuple[str, ...], str, str], ...] = (
    (
        "run_window_gate",
        ("RunWindowError", "运行窗口门禁拒绝", "RUN WINDOW GATE REFUSED"),
        "运行窗口门禁拒绝：当前不在允许的实盘运行窗口内（盘中 / 开盘前默认拒绝生成工单）",
        "等到市场本地收盘后的运行窗口再跑（A 股 14:10 之后，收盘后门禁不再适用）；"
        "确需盘中放行，请确认后在被调脚本上显式加 --allow-intraday（或 --no-strict-market-clock "
        "降级为警告）——盘中工单只能使用上一已收盘交易日的价格。",
    ),
    (
        "stale_prices",
        ("StalePriceError", "陈旧价格拒绝执行", "(STALE PRICE)"),
        "行情陈旧被拒：缓存里用于估值的最新 K 线不是目标估值日，拒绝在旧价上交易",
        "先把行情缓存同步到该交易日的收盘 bar（或换用正确的 --data-provider）再重跑；"
        "若确属数据源已知缺口的人工演练，可在被调脚本上显式加 --allow-stale-prices"
        "（工单对应行会被标记 price_stale=True）。",
    ),
    (
        "as_of_not_honourable",
        (
            "晚于市场时钟公布的最近已收盘交易日",
            "估值日必须是交易日",
            "的交易日 (calendar=",
            "--as-of-date 必须是 YYYY-MM-DD",
        ),
        "估值日被拒：该日期不是此市场的交易日，或其收盘价尚未收盘/尚未发布",
        "留空 as-of（由脚本按市场时钟解析最近已收盘交易日），或改传前一交易日；"
        "若是当日收盘价尚未发布，请等收盘 + 发布宽限之后再跑。",
    ),
    (
        "market_resolution",
        (
            "MixedMarketUniverseError",
            "UnknownSymbolMarketError",
            "无法从标的推断市场",
            "只包含现金/流动性代理标的",
        ),
        "市场无法确定：universe 跨市场或无法推断，实盘无法选择时区/节假日历",
        "把跨市场标的拆成两个组合，或在被调脚本上显式指定 --market {china,hk,us}。",
    ),
    (
        "holiday_calendar_unknown",
        ("HolidayCalendarCoverageError", "holiday calendar looks corrupt"),
        "节假日历不覆盖该年份/该市场：无法判断是否交易日（“日历未知”不等于“开市”）",
        "在 common/market_holidays.py 补齐该市场该年份的休市表后重跑。",
    ),
)

#: Trailing line of a Python traceback: ``pkg.module.SomeError: message``.
_ERROR_LINE_RE = re.compile(
    r"^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*(?:Error|Exception))\s*:\s*(.*)$"
)

# NOTE (L2.4/L2.5): the cash-proxy exclusion that keeps ``BIL`` from voting on the
# market -- and therefore on the run-directory date key -- now lives in exactly one
# place, `storage.is_clock_cash_proxy`, used by `StorageManager.market_clock_markets`.
# Do not re-derive a second copy here: two drifting market inferences are how the
# run directory, the summary date and the quotes basis come apart again.


def _extract_error_line(stderr: str) -> str:
    """Last exception line of a traceback, verbatim (``""`` when there is none)."""
    lines = [ln.rstrip() for ln in (stderr or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if _ERROR_LINE_RE.match(line.strip()):
            return line.strip()
    return lines[-1].strip() if lines else ""


def summarize_execution_failure(
    stderr: str,
    returncode: Optional[int] = None,
    label: str = "Execution",
) -> Dict[str, Any]:
    """Turn a child runner's ``stderr`` into an operator-actionable failure block.

    Returns a dict with ``error_kind`` / ``error_headline`` / ``error_hint`` /
    ``error_detail`` (the exception line, verbatim) / ``error_excerpt`` (last 8
    stderr lines) / ``error_message`` (the single human sentence the UI renders).

    Classification reads **only** ``stderr`` -- the runner prints the market-clock
    banner and the ``[STALE PRICE WARNING]`` list to stdout, and matching those would
    label a successful run as a refusal.  Nothing is swallowed: ``error_detail`` and
    ``error_excerpt`` are always the untouched original text, and callers still
    return the full ``stderr``.
    """
    stderr = stderr or ""
    detail = _extract_error_line(stderr)

    kind = "unknown"
    headline = ""
    hint = ""
    for rule_kind, markers, rule_headline, rule_hint in _FAILURE_RULES:
        if any(marker in stderr for marker in markers) or (
            detail and any(marker in detail for marker in markers)
        ):
            kind, headline, hint = rule_kind, rule_headline, rule_hint
            break

    if kind == "unknown":
        headline = "脚本执行失败，未识别出已知的门禁 / 行情原因"
        hint = (
            "展开 STDERR 查看完整回溯；门禁类拒绝通常会带 RunWindowError / "
            "StalePriceError / --as-of-date 字样。"
        )

    excerpt = "\n".join(
        [ln.rstrip() for ln in stderr.splitlines() if ln.strip()][-8:]
    )
    message = f"{label} 执行失败 (returncode={returncode})：{headline}"
    if detail:
        message += f"\n原因原文：{detail}"
    if hint:
        message += f"\n处理建议：{hint}"

    return {
        "error_kind": kind,
        "error_headline": headline,
        "error_hint": hint,
        "error_detail": detail,
        "error_excerpt": excerpt,
        "error_message": message,
    }


# --------------------------------------------------------------------------- #
# (L2.4, design §9 / §12 case 11) A 0-row 5m frame must never read as success
# --------------------------------------------------------------------------- #
#: The child runners' honest WARN marker (§9). Stage 2 prints it when the 5m
#: universe came back with zero real rows; Stage 1 additionally persists a
#: structured `intraday_5m_metrics` block inside `stage1_health_report.json`.
INTRADAY_5M_WARN_MARKER = "5m 日内特征不可用"


def _market_clock_module():
    """`common.market_clock`, with a repo-root sys.path retry (standalone web launch)."""
    try:
        import common.market_clock as market_clock
    except ImportError:  # pragma: no cover - repo root not importable yet
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
        import common.market_clock as market_clock
    return market_clock


def summarize_intraday_5m_availability(
    universe_5m: Optional[Mapping[str, Any]] = None,
    as_of_date: Optional[str] = None,
    *,
    enabled_requested: bool = True,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """`enabled` for the 5m intraday layer: True only on REAL rows for `as_of_date`.

    `common/intraday_features.py` marks a frame it could not fill with
    ``df.attrs["intraday_5m_available"] = False``.  That marker is CONSUMED here --
    never re-derived by attempting another fetch -- and a frame carrying it stays
    "unavailable" even if a caller accidentally hands over non-empty rows.

    The count is of 5m ROWS on the as-of session: not symbols, not daily bars.
    That distinction is the 2026-10-22 defect -- Stage 2 printed
    "成功加载并聚合 5m 日内特征 (标的数: 20)" where the 20 was the DAILY symbol count
    while the 5m universe was empty, so every intraday gate silently passed on data
    that did not exist.  Zero rows therefore means `enabled=False` + a WARNING, and
    the returned block is what gets persisted for downstream readers.
    """
    rows = 0
    with_rows: List[str] = []
    unavailable: List[str] = []
    session = None
    if as_of_date:
        try:
            session = pd.Timestamp(as_of_date).normalize()
        except (TypeError, ValueError):
            session = None

    for sym, df in (universe_5m or {}).items():
        name = str(sym)
        if (
            df is None
            or bool(getattr(df, "empty", True))
            or getattr(df, "attrs", {}).get("intraday_5m_available") is False
        ):
            unavailable.append(name)
            continue
        if session is not None:
            try:
                n_on_session = int((pd.DatetimeIndex(df.index).normalize() == session).sum())
            except (TypeError, ValueError):
                n_on_session = 0
        else:
            n_on_session = int(len(df))
        rows += n_on_session
        (with_rows if n_on_session > 0 else unavailable).append(name)

    enabled = bool(enabled_requested) and rows > 0
    block: Dict[str, Any] = {
        "requested": bool(enabled_requested),
        "enabled": enabled,
        "rows_5m": rows,
        "as_of_date": as_of_date,
        "symbols_with_rows": sorted(with_rows),
        "symbols_unavailable": sorted(unavailable),
        "reason": reason,
    }
    if block["requested"] and not enabled:
        message = (
            f"[WARNING] 5m 日内特征不可用 (enabled=False)：as_of={as_of_date} 当日真实 5m 行数为 0"
            "（统计的是 5m 行数，不是标的数，也不是日线 bar 数）。"
        )
        if reason:
            message += f"原因：{reason}。"
        message += " 下游必须把日内 VWAP / Chan-5m 门槛视为 UNKNOWN/SKIPPED，绝不静默 PASS。"
        print(message)
        block["warning"] = message
    return block


def reconcile_intraday_5m_observability(
    health_report: Optional[Mapping[str, Any]] = None,
    *,
    stdout: str = "",
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Persistable 5m-availability verdict for a run, derived from its artifacts.

    Priority (most specific first):
      1. THIS stage's own stdout marker -- the child's honest WARN;
      2. Stage 1's structured ``intraday_5m_metrics`` block.

    A report that says "requested, but nothing measured" is reconciled IN PLACE:
    ``enabled`` is forced to ``False`` and ``intraday_5m_available`` to ``False``,
    so a downstream reader can never see ``enabled=True`` on zero measured rows.
    """
    metrics: Optional[Mapping[str, Any]] = None
    if isinstance(health_report, Mapping):
        candidate = health_report.get("intraday_5m_metrics")
        if isinstance(candidate, Mapping):
            metrics = candidate

    if INTRADAY_5M_WARN_MARKER in (stdout or ""):
        block = summarize_intraday_5m_availability(
            None,
            as_of_date,
            enabled_requested=True,
            reason="子进程 stdout 报告 5m 日内特征不可用（当日 5m 行数为 0）",
        )
        block["source"] = "stdout"
    elif metrics is not None:
        requested = bool(metrics.get("enabled"))
        unavailable = (
            metrics.get("intraday_features_available") is False
            or metrics.get("available") is False
            or metrics.get("symbols_5m_available") == 0
            or metrics.get("symbols_measured") == 0
        )
        if requested and unavailable:
            block = summarize_intraday_5m_availability(
                None,
                as_of_date,
                enabled_requested=True,
                reason=str(metrics.get("unavailable_reason") or "stage1 报告显示无任何已测得的 5m 特征"),
            )
            block["source"] = "stage1_health_report.intraday_5m_metrics"
        else:
            block = {
                "requested": requested,
                "enabled": bool(requested and not unavailable),
                "rows_5m": None,  # not measured at this layer: the report counts symbols
                "as_of_date": as_of_date,
                "symbols_with_rows": [],
                "symbols_unavailable": [],
                "symbols_measured": metrics.get("symbols_measured"),
                "symbols_5m_available": metrics.get("symbols_5m_available"),
                "reason": metrics.get("unavailable_reason"),
                "source": "stage1_health_report.intraday_5m_metrics",
            }
    else:
        return {
            "requested": False,
            "enabled": False,
            "rows_5m": 0,
            "as_of_date": as_of_date,
            "symbols_with_rows": [],
            "symbols_unavailable": [],
            "reason": "本次运行未请求 / 未产出 5m 日内特征",
            "source": "none",
        }

    missing = metrics.get("symbols_missing_features") if metrics is not None else None
    if isinstance(missing, (list, tuple)):
        block["symbols_unavailable"] = sorted(str(s) for s in missing)

    if isinstance(metrics, dict) and not block["enabled"]:
        # Readers of stage1_health_report.json must not see enabled=True next to
        # "nothing measured": the raw block is normalized, not just annotated.
        metrics["enabled"] = False
        metrics["intraday_5m_available"] = False
        metrics["rows_5m"] = block["rows_5m"]

    return block


# --------------------------------------------------------------------------- #
# Dividend-aware NAV valuation (web/livetrading/valuation.py) into Stage 2
# --------------------------------------------------------------------------- #
# WHY THIS BLOCK EXISTS
# ---------------------
# Stage 2 used to be handed `--portfolio-value <the operator's form field>` verbatim, i.e.
# a NAV nobody had checked against the book.  `valuation.compute_nav` now marks the
# submitted shares dividend-inclusively (`shares x raw_close x factor_ratio`) and THAT
# number is what the child receives.  Three consequences are load-bearing:
#
#   1. `--portfolio-value` in the child's argv is no longer the form value.  The subprocess
#      contract is unchanged (same flag), only its producer moved.
#   2. A failure is a REFUSAL by default.  Silently falling back to the form value would
#      reintroduce exactly the defect this engine removes (a NAV that ignores dividends /
#      holds a stale price), so falling back needs an explicit `LIVETRADING_ALLOW_NAV_FALLBACK`
#      opt-in that logs an ERROR and is recorded as `nav_source="fallback_input"`.
#   3. `cache_uninitialized` and `stale_price` are DIFFERENT failures with different fixes.
#      An empty/uninitialised cache (the operator deleted `data/cache.duckdb`) otherwise
#      surfaces as "no price at all for X" -- which reads like a stale-market problem and
#      sends the operator to look at the wrong thing.  `cache_structure_reason` probes the
#      tables/row counts so the message names the actual cure (a Stage-1 data refresh).
NAV_FALLBACK_ENV = "LIVETRADING_ALLOW_NAV_FALLBACK"

#: Row-level mark, one entry per symbol.  NEVER folded into `summary.json` (which stays a
#: top-level digest): a 20-symbol book would otherwise bloat every summary by 20 blocks.
NAV_BREAKDOWN_FILENAME = "nav_breakdown.json"

#: Machine-readable failure classes.  They are written into `nav_warnings` (prefixed as
#: `reason=<class>: ...`) and into the refusal text, so the two failure families below can
#: never be conflated by a reader or by a future log-grep.
NAV_REASON_CACHE_UNINITIALIZED = "cache_uninitialized"
NAV_REASON_STALE_PRICE = "stale_price"
NAV_REASON_MISSING_CASH = "missing_cash"
NAV_REASON_ADJUST_BASIS = "adjust_basis"
NAV_REASON_INPUT = "input_error"
NAV_REASON_OTHER = "valuation_error"

#: (headline, operator hint) per failure class.  The cache class deliberately does NOT
#: mention staleness: the cure is a data refresh, not "wait for the close".
_NAV_REASON_TEXT: Dict[str, Tuple[str, str]] = {
    NAV_REASON_CACHE_UNINITIALIZED: (
        "缓存库为空或未初始化（不是行情过期）",
        "请先执行 Stage-1 数据刷新，把 ohlcv_bars / adjust_factors 补齐后重跑："
        "scripts/check_live_portfolio_health.py（其前置行情刷新）或 "
        "scripts/sync_china_5m_and_indices.py；若 adjust_factors 表缺失，还需要因子写入/重建脚本。"
        "本次确需按人工净值部署时，在表单勾选「人工覆盖净值」，"
        f"或设置 {NAV_FALLBACK_ENV}=1 显式回退（会记录一条 ERROR 日志）。",
    ),
    NAV_REASON_STALE_PRICE: (
        "行情陈旧或该标的缺当日 bar",
        "先把行情缓存同步到本次估值日（或换用正确的 --data-provider）再重跑；"
        f"确属数据源已知缺口的人工演练，可设置 {NAV_FALLBACK_ENV}=1 回退表单净值"
        "（会记录一条 ERROR），或在表单勾选「人工覆盖净值」。",
    ),
    NAV_REASON_MISSING_CASH: (
        "现金余额无法确定",
        "在持仓表单填写「现金余额」（留空才回退 BIL / 511880 的市值）；"
        "账户里没有任何停放现金标的时必须显式填写，否则 NAV 只是一个残差猜测。",
    ),
    NAV_REASON_ADJUST_BASIS: (
        "缓存价格口径（basis）未声明或无法派生",
        "运行 scripts/migrate_adjust_basis.py 为缓存声明价格口径后重跑；"
        "按错误的口径估值是百分比级的价格错误，不能降级为警告。",
    ),
    NAV_REASON_INPUT: (
        "持仓输入无法解析",
        "检查持仓股数（必须是整数、非负）与现金金额（不得为负）；"
        "表单里的权重列不会被当作股数。",
    ),
    NAV_REASON_OTHER: (
        "净值估值失败",
        "展开 STDERR / 服务端日志查看完整回溯后重试。",
    ),
}

_NAV_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})


# --------------------------------------------------------------------------- #
# (L2.62) Same-day run instances: `summary.json` must not accumulate across rounds
# --------------------------------------------------------------------------- #
#: Cross-round summary inheritance whitelist -- INTENTIONALLY EMPTY.
#:
#: Forensic origin: `2026-10-27/summary.json` simultaneously carried
#: `deploy_status="SUCCESS"` AND `error_detail="KeyboardInterrupt"`, with a
#: `deploy_stdout` (UTC 03:06) older than the `health_stdout` (UTC 04:31). Two rounds had
#: merged into one file, so "did the 04:31 run deploy?" had no single answer. Every field
#: named in that audit -- `deploy_status` / `deploy_stdout` / `health_stdout` / `nav_*` /
#: `trades_count` / `error_*` -- must therefore NEVER survive into another round's summary.
#:
#: The layout fix (one directory per round) makes inheritance structurally impossible for
#: a genuinely new instance. `de_merge_run_summary` below is the second line of defence for
#: the one case where a directory IS reused (same-second idempotent allocation, or
#: `LIVETRADING_RUN_INSTANCE_SPLIT=0`). If inheritance is ever needed, add the field here
#: AND record `inherited_from` + `inherited_fields` in the summary.
SUMMARY_INHERITANCE_WHITELIST: Tuple[str, ...] = ()


def de_merge_run_summary(
    pre_summary: Optional[Mapping[str, Any]],
    post_summary: Optional[Mapping[str, Any]],
    owned_keys: Optional[Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Drop fields that leaked in from a PREVIOUS round's `summary.json`.

    ``pre_summary`` is what the directory contained BEFORE this round started, so any key
    in it that this round did not re-derive (``owned_keys``) and that is absent from
    :data:`SUMMARY_INHERITANCE_WHITELIST` is a cross-round leftover.  Returns
    ``(cleaned_summary, dropped_keys)``; pure, so it is unit-testable offline.

    A genuinely new instance directory has an empty ``pre_summary``, making this a no-op --
    exactly the "starts from an empty dict" contract, enforced rather than assumed.
    """
    post: Dict[str, Any] = dict(post_summary or {})
    pre = dict(pre_summary or {})
    if not pre:
        return post, []
    owned = set(owned_keys or ())
    dropped: List[str] = []
    for key in list(post):
        if key in owned or key in SUMMARY_INHERITANCE_WHITELIST:
            continue
        if key in pre:
            dropped.append(key)
            post.pop(key)
    return post, sorted(dropped)


#: (L2.62) Stage-1's gate artifact, and the fields inside it a Stage-2 ticket obeys.
#:
#: A Stage-2 round that has to inherit a report from an EARLIER instance of the same
#: session records `inherited_fields` = this tuple minus what the report lacks. It is
#: derived from the report file that was ACTUALLY handed to the child -- never copied out
#: of a previous round's `summary.json`, which is exactly the merge the de-merge defends
#: against.
STAGE1_HEALTH_REPORT_FILENAME = "stage1_health_report.json"
STAGE1_GATE_FIELDS: Tuple[str, ...] = (
    "directive",
    "gate_code",
    "linear_equity_scale",
    "intraday_buy_scale",
    "circuit_breaker_tier",
    "freeze_remaining_bars",
    "market_macro",
    "breadth",
    "thrust",
    "thrust_active",
    "breadth_thrust_10d",
    "portfolio_health",
)
#: (L2.62) Summary keys proving a directory's round really RAN Stage 1. Used to decide
#: whether a missing gate report is a hard precondition failure (Stage 1 ran, its report
#: is unreachable -> refuse) or merely "Stage 1 was never invoked for this session"
#: (pre-L2.62 behaviour, warned about rather than silently upgraded to a new default).
STAGE1_EVIDENCE_SUMMARY_KEYS: Tuple[str, ...] = (
    "health_status",
    "health_returncode",
    "health_report_source",
)


class Stage1HealthReportMissingError(RuntimeError):
    """Refusal to deploy because this session's Stage-1 gate report cannot be found.

    Subclasses ``RuntimeError`` for the same reason as ``NavValuationError``: ``server.py``
    maps it to HTTP 500 with the whole message as ``detail``, so the operator reads the
    refusal plus the one command that fixes it instead of receiving a ticket that was
    produced with the macro gate / circuit breaker silently switched off.
    """


def _env_truthy(name: str) -> bool:
    """Same truthy set as `storage._truthy_env`, so every `LIVETRADING_*` knob reads alike."""
    return str(os.environ.get(name, "")).strip().lower() in _NAV_TRUE_VALUES


def nav_fallback_allowed() -> bool:
    """Opt-in degradation: on valuation failure, deploy anyway with the form-provided NAV.

    Off by default.  When on, the failure is logged at ERROR level and the result is tagged
    ``nav_source="fallback_input"`` so a later reader cannot mistake it for a computed mark.
    """
    return _env_truthy(NAV_FALLBACK_ENV)


class NavValuationError(RuntimeError):
    """Refusal to deploy because the book could not be marked honestly.

    Subclasses ``RuntimeError`` because that is what `server.py` maps to HTTP 500 +
    ``detail`` -- the same convention as ``storage.HoldingsInputMismatchError`` -- so the
    operator receives the WHOLE refusal (class + cause + the command that fixes it)
    instead of a bare ``Execution failed with code N``.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        detail: str = "",
        nav_warnings: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.nav_warnings = list(nav_warnings or [])


def _shares_book(holdings: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    """``symbol -> integer shares`` for the valuation engine.

    The web payload is polymorphic (JSON numbers, numeric strings, ``{shares: n}`` row
    dicts, weight-only rows).  ``compute_nav`` accepts ONLY ints and raises on anything
    else, so coercion happens here -- and weight-only rows (no share count) are dropped
    rather than valued as if the weight were a share count.
    """
    book: Dict[str, int] = {}
    for raw_symbol, raw_value in (holdings or {}).items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        if isinstance(raw_value, Mapping):
            raw_value = raw_value.get("shares", raw_value.get("share", raw_value.get("quantity")))
        try:
            shares_f = float(raw_value)
        except (TypeError, ValueError):
            continue
        if shares_f != shares_f or shares_f in (float("inf"), float("-inf")):
            continue
        if shares_f == 0:
            continue
        book[symbol] = int(shares_f)
    return book


def open_live_valuation_cache(cache_dir: Optional[str]):
    """Read-ONLY ``DuckDBCache`` over the live OHLCV cache directory.

    PATH RESOLUTION -- deliberately not re-implemented here:
      * the DIRECTORY is `StorageManager.cache_dir` (env ``LIVETRADING_CACHE_DIR`` >
        ``web/livetrading/data/settings.local.json`` key ``cache_dir`` > dynamic default
        ``<repo_root>/data``), i.e. the exact value already handed to the child as
        ``--cache-dir`` (and the same default as `common/cli_utils.shared_data_dir`);
      * the FILE NAME is resolved by `DuckDBCache.__init__` itself
        (``common/duckdb_cache.py:1104-1108``: a directory becomes
        ``<dir>/cache.duckdb``).  Nothing here hardcodes ``cache.duckdb``.

    WHY READ-ONLY: a writable `DuckDBCache.__init__` CREATES the database and its tables
    (`_init_tables`, ``duckdb_cache.py:1159``).  After the live cache was deleted, a
    writable open would silently leave a fresh EMPTY cache behind and then report "no
    price at all" for every holding -- an uninitialised cache masquerading as stale
    prices, plus a brand-new file the operator never asked for.  Read-only raises
    ``FileNotFoundError`` instead (``duckdb_cache.py:1116``), which
    `ExecutionService._valuation_cache` classifies as ``cache_uninitialized``.
    """
    if not cache_dir:
        raise FileNotFoundError("cache_dir is not configured")
    from common.duckdb_cache import DuckDBCache

    return DuckDBCache(db_path=str(cache_dir), read_only=True)


def cache_structure_reason(cache: Any) -> Optional[str]:
    """``None`` when the cache looks initialised, else a machine-readable reason.

    ROW-COUNT PROBE, not exception sniffing: `query_bars`/`query_adjust_factors` return an
    EMPTY frame for an unknown symbol on a healthy cache and raise a duckdb
    ``CatalogException`` on a cache whose tables were never created, so the failure cannot
    be told apart from "this symbol has no data" at that layer.  Counting rows in
    ``ohlcv_bars`` / ``adjust_factors`` distinguishes the two:
      * table missing  -> ``<table>_table_missing`` (the live cache predates the table, or
                          is a brand-new empty database);
      * table empty    -> ``<table>_empty`` (initialised schema, zero history).
    Both mean "Stage-1 data refresh needed", never "stale price".

    ``adjust_factors`` is required because the default ``dividend_mode="total_return"``
    cannot be honoured without it: the engine would degrade to RAW marking (dividends
    excluded) one symbol at a time, which is the very defect `valuation.py` exists to fix.

    A duck-typed cache without ``_get_connection`` (fakes used by `test_portfolio_valuation`)
    is treated as healthy -- there is nothing structural to probe.
    """
    get_con = getattr(cache, "_get_connection", None)
    if get_con is None:
        return None
    # No public row-count API exists on DuckDBCache; this read-only handle is the same one
    # its own query methods use, and it never writes.
    con = get_con(read_only=True)
    try:
        for table in ("ohlcv_bars", "adjust_factors"):
            try:
                row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
            except Exception:
                return f"{table}_table_missing"
            count = int(row[0]) if row and row[0] is not None else 0
            if count <= 0:
                return f"{table}_empty"
    finally:
        try:
            if not getattr(cache, "is_memory", False):
                con.close()
        except Exception:
            pass
    return None


class ExecutionService:
    """Orchestrates health check and live deployment runs."""

    #: (L2.62) How long a Stage-2 request may reuse the directory of an immediately
    #: preceding Stage-2 request for the same `(strategy, universe, date)`.  A
    #: double-click must not fragment the archive into two instances of one round.
    PENDING_STAGE2_REUSE_SECONDS = 1.0

    def __init__(self, storage: StorageManager):
        self.storage = storage
        #: (L2.62) `(strategy, universe, date) -> (run_dir, owned_keys)` allocated by Stage 1
        #: and not yet consumed by Stage 2.  Both stages of one deploy MUST share one
        #: directory: Stage 2 receives `--health-report-file` from it and is the only writer
        #: of the ticket.  `owned_keys` is the set of summary fields THIS round's Stage 1
        #: wrote -- Stage 2 must re-own them, otherwise the de-merge would delete the health
        #: half of the very round it is completing.
        #:
        #: **LIMITATION -- in-process only.** This is a plain dict, so the hand-off exists
        #: only inside ONE Python process: a web restart between Stage 1 and Stage 2 (or a
        #: deploy served by a different uvicorn worker) finds no entry.  Stage 2 must never
        #: treat that as "no macro gate needed" -- `_resolve_stage2_health_gate` then
        #: re-discovers the newest `stage1_health_report.json` of the same session on disk,
        #: hands it to the child, and records `health_report_source` / `inherited_from` /
        #: `inherited_fields` in the new round's summary.  If the session shows Stage-1
        #: evidence (see `STAGE1_EVIDENCE_SUMMARY_KEYS`) but no report is reachable, the
        #: deploy is REFUSED (`Stage1HealthReportMissingError`) rather than run ungated.
        self._pending_stage1: Dict[Tuple[str, str, str], Tuple[str, FrozenSet[str]]] = {}
        #: `(strategy, universe, date) -> (monotonic_ts, run_dir, inherited_owned_keys)` of the
        #: last Stage-2 dir, consulted only when no Stage-1 allocation is pending (a
        #: Stage-2-only deploy or a double-click).
        #:
        #: **LIMITATION -- in-process only**, exactly like `_pending_stage1`: after a
        #: restart a double-click is deduped only by the second-level
        #: `storage.allocate_run_instance_dir` idempotency (one archive entry per second),
        #: never by this window.  Losing the window costs an extra instance dir, never a
        #: gate: the health report is re-resolved from disk (see `_resolve_stage2_health_gate`).
        self._pending_stage2: Dict[Tuple[str, str, str], Tuple[float, str, FrozenSet[str]]] = {}

    # -- (L2.62) same-day run instances: allocation + hand-off -- #
    @staticmethod
    def _instance_key(strategy_key: Optional[str], universe_key: Optional[str], val_date: str) -> Tuple[str, str, str]:
        return (str(strategy_key or ""), str(universe_key or ""), str(val_date or ""))

    def _allocate_stage1_run_dir(self, base_dir: str, key: Tuple[str, str, str]) -> str:
        """Stage 1 owns the allocation; Stage 2 reuses it. Returns the run dir to use.

        The flat (first-of-session) directory is handed off too, not just instances: both
        stages of ONE deploy must write into the SAME directory, otherwise Stage 2's
        `--health-report-file` would point at a Stage-1 report that lives somewhere else
        and the session would be split across two directories by accident.
        """
        run_dir = self.storage.allocate_run_instance_dir(base_dir)
        if len(self._pending_stage1) >= 64:
            self._pending_stage1.pop(next(iter(self._pending_stage1)), None)
        self._pending_stage1[key] = (run_dir, frozenset())
        return run_dir

    def _record_stage1_owned_keys(
        self, key: Tuple[str, str, str], run_dir: str, owned_keys: Any
    ) -> None:
        """Publish the fields Stage 1 wrote for THIS round, for Stage 2 to re-own."""
        pending = self._pending_stage1.get(key)
        if pending and pending[0] == run_dir:
            self._pending_stage1[key] = (run_dir, frozenset(owned_keys or ()))

    def _resolve_stage2_run_dir(
        self, base_dir: str, key: Tuple[str, str, str]
    ) -> Tuple[str, FrozenSet[str], str]:
        """`(run directory, Stage-1-owned keys, hand-off source)` for this Stage-2 round.

        The third element says WHERE the directory came from, which decides how the
        Stage-1 gate report is resolved:

        * ``"stage1"``     -- this process still holds the Stage-1 allocation: the report
                              belongs to this very round (same directory, same round);
        * ``"stage2_reuse"`` -- the double-click window (`PENDING_STAGE2_REUSE_SECONDS`);
        * ``"new"``        -- neither is known, i.e. a Stage-2-only deploy, a RESTART, or a
                              request served by another uvicorn worker.  This is the case
                              that must not silently deploy without a macro gate; see
                              `_resolve_stage2_health_gate`.
        """
        pending = self._pending_stage1.pop(key, None)
        if pending and os.path.isdir(pending[0]):
            run_dir, inherited = pending
            self._remember_stage2_dir(key, run_dir, inherited)
            return run_dir, inherited, "stage1"
        previous = self._pending_stage2.get(key)
        if previous and (time.monotonic() - previous[0]) <= self.PENDING_STAGE2_REUSE_SECONDS:
            if os.path.isdir(previous[1]):
                return previous[1], previous[2], "stage2_reuse"
        run_dir = self.storage.allocate_run_instance_dir(base_dir)
        self._remember_stage2_dir(key, run_dir, frozenset())
        return run_dir, frozenset(), "new"

    def _remember_stage2_dir(
        self, key: Tuple[str, str, str], run_dir: str, inherited: FrozenSet[str]
    ) -> None:
        if len(self._pending_stage2) >= 64:
            self._pending_stage2.pop(next(iter(self._pending_stage2)), None)
        self._pending_stage2[key] = (time.monotonic(), run_dir, inherited)

    # -- (L2.62) Stage-1 gate report: restart / multi-worker tolerant resolution -- #
    def _read_health_report(self, path: str) -> Optional[Dict[str, Any]]:
        """Top-level dict of a gate report, or `None` when it is absent/unreadable."""
        report = self.storage._load_json(path, default=None)
        return report if isinstance(report, dict) else None

    def _health_gate_summary_fields(
        self,
        report_file: Optional[str],
        report: Optional[Dict[str, Any]],
        source_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Summary provenance for the gate report THIS round used.

        Everything here is derived from the report file that was actually handed to the
        child (or from its absence) -- never from a previous round's `summary.json`, so
        re-owning these keys cannot resurrect another round's health/nav/deploy fields.
        `inherited_*` is only populated when the report came from a DIFFERENT directory
        than this round's own.
        """
        fields: Dict[str, Any] = {
            "health_gate_applied": bool(report_file),
            "health_report_file": report_file,
            "health_gate_code": (report or {}).get("gate_code"),
            "health_gate_directive": (report or {}).get("directive"),
        }
        if source_dir is not None:
            fields["health_report_source"] = os.path.basename(os.path.normpath(source_dir))
            try:
                fields["inherited_from"] = os.path.relpath(source_dir, self.storage.archive_dir)
            except ValueError:  # different drive on Windows -- keep the absolute path
                fields["inherited_from"] = source_dir
            fields["inherited_fields"] = sorted(f for f in STAGE1_GATE_FIELDS if f in (report or {}))
        return fields

    def _find_inheritable_stage1_report(
        self, base_dir: str, run_dir: str
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """`(run dir, report)` of the newest USABLE Stage-1 report of this session.

        Same ordering rule as every other L2.62 reader -- lexicographic instance names,
        newest first, never mtime -- so a gate report restored from a backup cannot be
        mistaken for a newer one.  `run_dir` itself is skipped: the caller already knows it
        has no report (otherwise it would not be searching).
        """
        own = os.path.normpath(run_dir)
        candidates: List[str] = []
        latest = self.storage.resolve_latest_instance_dir(base_dir)
        if latest:
            candidates.append(latest)
        candidates.extend(reversed(self.storage.list_instances_for_date(base_dir)))
        seen = set()
        for candidate in candidates:
            norm = os.path.normpath(candidate)
            if norm in seen or norm == own:
                continue
            seen.add(norm)
            report_file = os.path.join(norm, STAGE1_HEALTH_REPORT_FILENAME)
            if not os.path.exists(report_file):
                continue
            report = self._read_health_report(report_file)
            if report is None:
                logging.warning(
                    "[L2.62] 忽略不可读的 Stage-1 门禁报告（继续向前回溯）: %s", report_file
                )
                continue
            return norm, report
        return None, None

    def _stage1_evidence_dirs(self, base_dir: str) -> List[str]:
        """Run dirs of this session whose `summary.json` proves their round ran Stage 1.

        This is the boundary between "a macro gate existed and is now unreachable" (a hard
        refusal -- the regression this method exists for) and "Stage 1 was never invoked
        for this session at all" (the pre-L2.62 behaviour, kept but warned about).
        """
        evidence: List[str] = []
        for candidate in self.storage.list_instances_for_date(base_dir):
            summary = self.storage._load_json(
                os.path.join(candidate, "summary.json"), default=None
            )
            if isinstance(summary, dict) and any(k in summary for k in STAGE1_EVIDENCE_SUMMARY_KEYS):
                evidence.append(candidate)
        return evidence

    def _resolve_stage2_health_gate(
        self, base_dir: str, run_dir: str, handoff: str
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """`(path to hand the child as --health-report-file, summary provenance)`.

        An IN-PROCESS Stage-1 hand-off whose own child wrote no report keeps the historic
        behaviour (no flag, loud warning): nothing older can be more authoritative than
        the Stage-1 round that just ran, so inheriting there would be misattribution.

        Everywhere else the report is re-resolved from the filesystem, which is what makes
        the Stage 1 -> Stage 2 hand-off survive a restart or a request landing on another
        uvicorn worker:

        1. this round's own directory (a reused instance dir may already hold one);
        2. `resolve_latest_instance_dir(base_dir)`, then `list_instances_for_date` newest
           -> oldest, for a directory containing `stage1_health_report.json`;
        3. `handoff == "new"` (restart / another uvicorn worker / Stage-2-only) and nothing
           was found while the session shows Stage-1 evidence -> raise
           `Stage1HealthReportMissingError`.  Deploying here would hand the engine a ticket
           with the macro gate / circuit breaker silently switched off, which is exactly
           the L2.62 regression;
        4. nothing found and no Stage-1 evidence -> keep the pre-L2.62 semantics (flag
           omitted) but make it explicit in both the log and the summary
           (`health_gate_applied=False`), never a silent default.

        Step 3 is deliberately scoped to `"new"`: a `"stage2_reuse"` round is the same
        in-process round as the one that just ran (a double-click), so there is no new
        gate decision to refuse -- it logs and proceeds exactly like its predecessor.
        """
        own_report = os.path.join(run_dir, STAGE1_HEALTH_REPORT_FILENAME)
        if os.path.exists(own_report):
            report = self._read_health_report(own_report)
            if report is not None:
                return own_report, self._health_gate_summary_fields(own_report, report)

        if handoff == "stage1":
            logging.warning(
                "[L2.62] Stage-1 本轮目录缺少可用门禁报告（%s）；按既有语义不带 --health-report-file 部署，"
                "本轮 summary 记录 health_gate_applied=False。若需门禁生效，请重跑 Stage 1 健康检查。",
                own_report,
            )
            return None, self._health_gate_summary_fields(None, None)

        source_dir, report = self._find_inheritable_stage1_report(base_dir, run_dir)
        if source_dir is not None and report is not None:
            report_file = os.path.join(source_dir, STAGE1_HEALTH_REPORT_FILENAME)
            logging.warning(
                "[L2.62] 未找到本进程内的 Stage-1 交接（进程重启 / 多 worker / 仅跑 Stage 2），"
                "已回溯并继承同一会话的门禁报告: %s（%s）",
                report_file,
                handoff,
            )
            return report_file, self._health_gate_summary_fields(report_file, report, source_dir)

        evidence = self._stage1_evidence_dirs(base_dir) if handoff == "new" else []
        if evidence:
            raise Stage1HealthReportMissingError(
                "[L2.62] 拒绝部署：本次会话（%s）已有 Stage-1 健康检查记录（%s），但目录中没有可用的 "
                "stage1_health_report.json，且本进程没有 Stage-1 -> Stage-2 交接（进程重启 / 多 worker）。"
                "继续部署会让宏观门禁与熔断指令被静默跳过。"
                "处理：重新执行一次 Stage 1 健康检查（/api/run-health）后再部署；"
                "或确认该会话的门禁报告未被删除/移出归档目录。"
                % (
                    os.path.basename(os.path.normpath(base_dir)) or base_dir,
                    ", ".join(os.path.basename(os.path.normpath(p)) or p for p in evidence[-3:]),
                )
            )

        logging.warning(
            "[L2.62] 本次会话没有任何 Stage-1 门禁报告（%s），按既有语义（L2.62 之前同为不带 "
            "--health-report-file）部署；本轮 summary 记录 health_gate_applied=False。"
            "需要宏观门禁/熔断生效时，请先执行 Stage 1 健康检查。",
            os.path.basename(os.path.normpath(run_dir)) or run_dir,
        )
        return None, self._health_gate_summary_fields(None, None)

    # -- (C6) valuation-date resolution: market clock, never `date.today()` -- #
    def _locate_universe_file(self, universe_file: Optional[str]) -> Optional[str]:
        """Path-resolution half of `_resolve_universe`, with no run-dir side effects.

        Split out so the *valuation date* can be resolved before the run directory
        exists: the run directory is named after that date, so its path cannot be an
        input to computing it.  Same precedence as `_resolve_universe` (absolute ->
        repo root -> universe dir -> cwd-relative), returning `None` when nothing is
        found or a custom (materialized) universe was requested.
        """
        if not universe_file or universe_file == "__CUSTOM__":
            return None
        if os.path.isabs(universe_file) and os.path.exists(universe_file):
            return universe_file
        candidate = os.path.join(self.storage.repo_root, universe_file)
        if os.path.exists(candidate):
            return candidate
        candidate_u = os.path.join(self.storage.universe_dir, universe_file)
        if os.path.exists(candidate_u):
            return candidate_u
        if os.path.exists(universe_file):
            return universe_file
        return None

    @staticmethod
    def _read_universe_symbols(path: str) -> List[str]:
        """Symbols of a universe file (one per line, `#` comments allowed)."""
        symbols: List[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    token = line.split("#", 1)[0].strip()
                    if token:
                        # Universe files are one symbol per line; be tolerant of
                        # whitespace/comma separated rows anyway.
                        for tok in token.replace(",", " ").split():
                            symbols.append(tok.upper())
        except OSError:
            return []
        return symbols

    def _symbols_for_valuation_clock(
        self,
        universe_file: Optional[str],
        custom_symbols: Optional[List[str]],
    ) -> List[str]:
        """The symbol set used only to infer the market (timezone + holiday calendar).

        Custom symbols win (they are exactly what will be traded); otherwise the
        selected preset universe file is read; otherwise the same China
        core-satellite default `_resolve_universe` falls back to.
        """
        if custom_symbols:
            return [str(s).strip().upper() for s in custom_symbols if s and str(s).strip()]
        preset = self._locate_universe_file(universe_file)
        if preset:
            symbols = self._read_universe_symbols(preset)
            if symbols:
                return symbols
        default_u = os.path.join(
            self.storage.repo_root, "docs", "universe", "china", "core_satellite_22_stocks.txt"
        )
        return self._read_universe_symbols(default_u)

    def _market_clock_val_date(
        self,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> str:
        """ISO valuation date = the market clock's latest *published* session.

        (L2.4 / L2.5) The date MATH lives in exactly one place --
        `StorageManager.market_clock_date` -> `common.market_clock.expected_last_session`
        -- so the run-directory name, `summary.json['date']`, the multi-netting
        archive key and the `/api/*/latest` lookups are all the same session by
        construction.  This method only chooses *which* symbols vote on the market
        (the set that will actually be traded, cash proxies excluded).

        Replaces `date.today()`: that is the host's calendar day, which is a
        non-trading weekend on Saturdays and, intraday, a bar that does not exist.
        """
        return self.storage.market_clock_date(
            symbols=self._symbols_for_valuation_clock(universe_file, custom_symbols),
            now=now,
        )

    # -- (L2.4) archive key + §9 observability -- #
    @staticmethod
    def _assert_archive_keyed_by(run_dir: str, valuation_date: str) -> None:
        """The run directory MUST be keyed by the valuation session, not by `today`.

        `L2.4`'s acceptance is "run_dir naming and `summary.json['as_of']` agree".
        Both are written from the same `val_date`, and this path-component assertion
        is what keeps a future "just use `date.today()`" shortcut from silently
        splitting them apart.
        """
        parts = [p for p in str(run_dir).replace("\\", "/").split("/") if p]
        if valuation_date not in parts:
            raise ValueError(
                f"Archive directory {run_dir!r} is not keyed by the valuation session "
                f"{valuation_date!r}: run_dir and summary.json['date'] would disagree."
            )

    def _clock_observability(
        self,
        valuation_date: str,
        universe_file: Optional[str],
        custom_symbols: Optional[List[str]],
        as_of_source: str,
    ) -> Dict[str, Any]:
        """Design §9 fields this layer owns: tz-aware UTC + market-local + session + as_of.

        The live stage scripts already persist a richer `clock` block through
        `common/cli_utils.persist_run_metadata`; this block is the COMPLETION that
        guarantees the fields exist even when the child failed before writing them
        (a refusal happens after the banner but before the report in some paths).
        Callers merge with `setdefault`, so the child's authoritative values win.
        """
        market_clock = _market_clock_module()
        symbols = self._symbols_for_valuation_clock(universe_file, custom_symbols)
        markets = self.storage.market_clock_markets(symbols=symbols)
        primary = markets[0] if markets else "china"
        block = market_clock.market_now(primary).stamp().to_dict()
        # `as_of_trading_date` must describe THIS run's valuation (a replay pins an
        # older session), while the clock's own answer is kept for audit.
        block["clock_as_of_trading_date"] = block.get("as_of_trading_date")
        block.update(
            {
                "as_of_trading_date": valuation_date,
                "as_of_source": as_of_source,
                "expected_last_session": valuation_date,
                "markets": markets,
                "source": "web/livetrading",
            }
        )
        return block

    @staticmethod
    def _not_checked_freshness(valuation_date: str, stage: str) -> Dict[str, Any]:
        """Honest placeholder when the child persisted no `freshness_summary`.

        `NOT_CHECKED` is deliberately NOT `PASS`: a missing audit block must not read
        as "the bars were verified".
        """
        return {
            "stage": stage,
            "status": "NOT_CHECKED",
            "expected_session": valuation_date,
            "reason": "子进程未持久化 freshness_summary（可能在其之前失败）；NOT_CHECKED 不是 PASS",
            "source": "web/livetrading",
        }

    def _merge_observability(
        self,
        summary: Dict[str, Any],
        *,
        valuation_date: str,
        universe_file: Optional[str],
        custom_symbols: Optional[List[str]],
        as_of_source: str,
        stage: str,
    ) -> Dict[str, Any]:
        """Fill in §9 fields the child did not write, then log them for the operator.

        A MERGE: the child's `clock` / `freshness_summary` (written by
        `persist_run_metadata`) survive verbatim; only missing keys are added.
        """
        block = self._clock_observability(valuation_date, universe_file, custom_symbols, as_of_source)
        existing_clock = summary.get("clock")
        if isinstance(existing_clock, dict):
            for key, value in block.items():
                existing_clock.setdefault(key, value)
            clock_block = existing_clock
        else:
            summary["clock"] = block
            clock_block = block

        if not isinstance(summary.get("freshness_summary"), dict):
            summary["freshness_summary"] = self._not_checked_freshness(valuation_date, stage)

        print(
            f"[OBSERVABILITY] stage={stage} utc={clock_block.get('utc_now')} "
            f"local={clock_block.get('local_now')} session={clock_block.get('session')} "
            f"as_of={valuation_date} ({as_of_source}) "
            f"freshness={summary['freshness_summary'].get('status')}"
        )
        return clock_block

    def _apply_intraday_observability(
        self,
        summary: Dict[str, Any],
        block: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist the 5m verdict into `summary.json` and its `freshness_summary`.

        Case 11 (§12) requires the affected symbols to be visible in the freshness
        audit, so a 0-row run names them there instead of leaving a blank that reads
        like "nothing to report".
        """
        observability = summary.get("observability")
        if not isinstance(observability, dict):
            observability = {}
            summary["observability"] = observability
        observability["intraday_5m"] = block

        fresh = summary.get("freshness_summary")
        if isinstance(fresh, dict) and block.get("requested") and not block.get("enabled"):
            fresh["intraday_5m"] = {
                "status": "UNAVAILABLE",
                "enabled": False,
                "rows_5m": block.get("rows_5m"),
                "symbols_unavailable": list(block.get("symbols_unavailable") or []),
                "reason": block.get("reason"),
            }
        return block

    @staticmethod
    def _as_of_args(explicit_as_of: Optional[str]) -> List[str]:
        """`--as-of-date` argv fragment: **only** for an explicitly requested session.

        Omitting it (the normal live case) is what makes the child runner resolve the
        session from the market clock, since `resolve_live_clock_context` only takes
        the `explicit` branch when the flag is present.  A user-supplied date is
        forwarded verbatim -- it is a deliberate replay of that session, and the
        child's own gate validates it (trading day, close already published).
        """
        return ["--as-of-date", explicit_as_of] if explicit_as_of else []

    def _resolve_universe(
        self,
        universe_file: Optional[str],
        custom_symbols: Optional[List[str]],
        run_dir: str,
    ) -> str:
        """Resolve universe file path or materialize custom symbols to a universe file."""
        # 1. If an actual preset universe file was selected, resolve it
        located = self._locate_universe_file(universe_file)
        if located:
            return located

        # 2. If custom symbols are provided (or custom universe selected), materialize them cleanly
        if custom_symbols and len(custom_symbols) > 0:
            custom_path = os.path.join(run_dir, "resolved_universe.txt")
            with open(custom_path, "w", encoding="utf-8") as f:
                f.write("# ==============================================================================\n")
                f.write(f"# Auto-generated custom universe: {datetime.now(timezone.utc).isoformat()}\n")
                f.write("# ==============================================================================\n")
                for s in custom_symbols:
                    if s and s.strip():
                        clean_sym = s.strip().upper()
                        stock_name = get_stock_name(clean_sym)
                        if stock_name and not is_placeholder_stock_name(stock_name, clean_sym):
                            f.write(f"# {stock_name}\n")
                        f.write(f"{clean_sym}\n")
            return custom_path

        # Default fallback: China Core-Satellite 22
        default_u = os.path.join(self.storage.repo_root, "docs", "universe", "china", "core_satellite_22_stocks.txt")
        return default_u

    def _resolve_strategy(self, strategy_file: str) -> str:
        """Resolve strategy file path relative to strategy_dir or repo_root."""
        if os.path.isabs(strategy_file) and os.path.exists(strategy_file):
            return strategy_file
        candidate = os.path.join(self.storage.strategy_dir, strategy_file)
        if os.path.exists(candidate):
            return candidate
        candidate_repo = os.path.join(self.storage.repo_root, strategy_file)
        if os.path.exists(candidate_repo):
            return candidate_repo
        return strategy_file

    def _get_subprocess_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        # Ensure repo root is on PYTHONPATH for common/ and pipeline/ imports
        existing_pythonpath = env.get("PYTHONPATH", "")
        paths = [self.storage.repo_root]
        if existing_pythonpath:
            paths.append(existing_pythonpath)
        env["PYTHONPATH"] = os.path.pathsep.join(paths)
        return env

    # --------------------------------------------------------------------------
    # (NAV) Dividend-aware valuation, delegated to `valuation.compute_nav`
    # --------------------------------------------------------------------------
    def _nav_refusal(
        self,
        reason: str,
        detail: str,
        *,
        exc: Optional[BaseException] = None,
        book: Optional[Mapping[str, Any]] = None,
        as_of: Optional[str] = None,
    ) -> NavValuationError:
        """Build the operator-facing refusal for one failure class.

        The class-specific headline/hint pair comes from `_NAV_REASON_TEXT`, so a missing
        CACHE can never be reported with the stale-price wording (and vice versa).  The
        machine-readable class also lands in `nav_warnings` as ``reason=<class>: ...``.
        """
        headline, hint = _NAV_REASON_TEXT.get(reason, _NAV_REASON_TEXT[NAV_REASON_OTHER])
        message = f"实盘调仓已中止（净值估值失败）：{headline}。\n原因：{detail}\n处理建议：{hint}"
        logging.error(
            "[nav] 拒绝部署: reason=%s as_of=%s symbols=%s detail=%s",
            reason,
            as_of,
            sorted(book or {}),
            detail,
            exc_info=exc if exc is not None else False,
        )
        return NavValuationError(
            message,
            reason=reason,
            detail=detail,
            nav_warnings=[f"reason={reason}: {detail}"],
        )

    def _valuation_cache(self, *, book=None, as_of=None):
        """Read-only live cache handle; a missing file is `cache_uninitialized`, not stale."""
        cache_dir = getattr(self.storage, "cache_dir", None)
        try:
            return open_live_valuation_cache(cache_dir)
        except FileNotFoundError as exc:
            raise self._nav_refusal(
                NAV_REASON_CACHE_UNINITIALIZED,
                f"cache_db_missing: {cache_dir} (<cache_dir>/cache.duckdb 不存在；"
                f"DuckDBCache 以只读打开，因此不会自动建库)",
                exc=exc,
                book=book,
                as_of=as_of,
            ) from exc
        except Exception as exc:
            raise self._nav_refusal(
                NAV_REASON_CACHE_UNINITIALIZED,
                f"cache_open_failed: {cache_dir}: {type(exc).__name__}: {exc}",
                exc=exc,
                book=book,
                as_of=as_of,
            ) from exc

    @staticmethod
    def _classify_nav_exception(exc: BaseException) -> Tuple[str, str]:
        """``(reason, detail)`` for any exception raised while marking the book.

        Ordered most-specific first.  `StalePriceError` is a ``ValueError`` subclass, so it
        must be tested before the generic ``ValueError`` branch -- otherwise a stale bar
        would be reported as bad operator input.
        """
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, MissingCashError):
            return NAV_REASON_MISSING_CASH, detail
        if isinstance(exc, StalePriceError):
            return NAV_REASON_STALE_PRICE, detail
        text = str(exc)
        if type(exc).__name__ == "UnknownAdjustBasisError" or "stored data has basis" in text:
            return NAV_REASON_ADJUST_BASIS, detail
        if "Catalog Error" in text or "does not exist" in text:
            # A table vanished between the structural probe and the read (or the probe was
            # skipped for a duck-typed cache): same cure, same class.
            return NAV_REASON_CACHE_UNINITIALIZED, detail
        if isinstance(exc, ValueError):
            return NAV_REASON_INPUT, detail
        return NAV_REASON_OTHER, detail

    def _compute_nav_result(
        self,
        book: Mapping[str, int],
        *,
        as_of: str,
        cash: Optional[float],
        provider: str,
        nav_override: Optional[float] = None,
        anchors: Optional[Mapping[str, str]] = None,
    ):
        """``NavResult`` for ``book``; any failure becomes a classified `NavValuationError`."""
        cache = self._valuation_cache(book=book, as_of=as_of)
        try:
            structure = cache_structure_reason(cache)
        except Exception as exc:  # corrupt file / unreadable table: same cure as "empty"
            raise self._nav_refusal(
                NAV_REASON_CACHE_UNINITIALIZED,
                f"cache_probe_failed: {type(exc).__name__}: {exc}",
                exc=exc,
                book=book,
                as_of=as_of,
            ) from exc
        if structure:
            raise self._nav_refusal(
                NAV_REASON_CACHE_UNINITIALIZED,
                f"cache_structure={structure} (ohlcv_bars/adjust_factors 缺失或零行)",
                book=book,
                as_of=as_of,
            )
        try:
            return compute_nav(
                dict(book),
                as_of=as_of,
                cache=cache,
                cash=cash,
                anchors=dict(anchors or {}),
                nav_override=nav_override,
                provider=provider,
            )
        except Exception as exc:
            reason, detail = self._classify_nav_exception(exc)
            raise self._nav_refusal(reason, detail, exc=exc, book=book, as_of=as_of) from exc

    def _derive_anchors(
        self,
        book: Mapping[str, int],
        *,
        as_of: str,
        strategy_key: Optional[str],
        universe_key: Optional[str],
        exclude_dir: Optional[str] = None,
    ) -> Tuple[Dict[str, str], List[str]]:
        """``(anchors, missing)`` -- first ARCHIVED session each held symbol was > 0.

        WHY THIS EXISTS: the engine's total-return mark is
        ``shares x raw_close x (factor_mark / factor_anchor)``, and ``factor_hfq`` is
        monotone increasing, so the anchor MUST be the day the position was opened.
        Without one the engine now degrades to raw (a lower bound, dividends excluded).
        A lower bound under-sizes every order slightly, which is far safer than the
        alternative, but it is still an avoidable error -- and this station already
        archives every session's book, so the open date is recoverable.

        Resolution is deliberately reuse-only (no new path logic): `StorageManager`
        already owns the archive layout via `list_runs`, `_load_snapshot_holdings`
        (that session's pre-trade `holdings.json`) and `extract_post_run_holdings`
        (fallback for pre-`holdings.json` archives).  Scanned oldest -> newest, so the
        first hit IS the open date; every symbol is resolved independently (a book
        built over several rounds gets per-name anchors).

        Best effort by design: an unreadable/empty archive yields ``({}, missing)`` and
        the engine reports ``anchor_unavailable`` -- never a guessed date.
        """
        wanted = set(book)
        if not wanted or not strategy_key or not universe_key:
            return {}, sorted(wanted)
        try:
            runs = self.storage.list_runs(
                strategy_key=strategy_key, universe_key=universe_key, require_both=True
            )
        except Exception as exc:  # corrupt archive must not block a valuation
            logging.warning("[nav] 建仓日回溯失败（忽略，按裸价下限估值）：%s", exc)
            return {}, sorted(wanted)

        # Oldest first: the FIRST observation of a symbol is its open date.
        ordered = sorted(
            (r for r in runs if r.get("path") and r.get("date")),
            key=lambda r: str(r.get("date")),
        )
        exclude = os.path.abspath(exclude_dir) if exclude_dir else None
        anchors: Dict[str, str] = {}
        for run in ordered:
            run_path = str(run.get("path"))
            if exclude and os.path.abspath(run_path) == exclude:
                continue  # the run being written right now is not history
            date_val = str(run.get("date"))
            if date_val >= as_of:
                # `>=`, not `>` (L2.62): same-day runs are now *sibling instance dirs*
                # (`<date>/<HHMMSS>/`), not the single flat dir.  A symbol first seen in
                # one of them may well have been opened during today's own session, so
                # today's date is NOT a usable anchor date -- anchoring to it would make
                # the total-return ratio identically 1.0 while *claiming* `total_return`
                # in `dividend_basis`, i.e. silently reporting a raw valuation as if it
                # were dividend-inclusive.  Excluding the whole day keeps that case on
                # the honest `anchor_unavailable` -> raw path (a lower bound).
                break
            snapshot = self.storage._load_snapshot_holdings(run_path)
            if snapshot is None:
                snapshot = self.storage.extract_post_run_holdings(run_path)
            if not snapshot:
                continue
            held = _shares_book(snapshot)
            for symbol in wanted - set(anchors):
                if held.get(symbol, 0) > 0:
                    anchors[symbol] = date_val
            if len(anchors) == len(wanted):
                break
        return anchors, sorted(wanted - set(anchors))

    @staticmethod
    def _nav_payload(
        *,
        nav: float,
        nav_source: str,
        nav_computed: Optional[float],
        equity: Optional[float],
        cash: Optional[float],
        cash_source: Optional[str],
        as_of: str,
        per_symbol: Optional[Mapping[str, Any]] = None,
        stale_symbols: Optional[List[str]] = None,
        dividend_alarms: Optional[List[dict]] = None,
        nav_warnings: Optional[List[str]] = None,
        anchors: Optional[Mapping[str, str]] = None,
        anchor_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The single shape every NAV path returns, so three code paths cannot drift."""
        return {
            #: value handed to the child as `--portfolio-value`
            "nav_effective": float(nav),
            #: the ENGINE's own mark (`equity + cash`); on an override this is "Y" in the
            #: diff log line, and on a fallback it is None (nothing could be computed)
            "nav_computed": None if nav_computed is None else float(nav_computed),
            "nav_source": nav_source,
            "equity": None if equity is None else float(equity),
            "cash": None if cash is None else float(cash),
            "cash_source": cash_source,
            "as_of": as_of,
            #: ``symbol -> position-open date`` actually handed to the engine, and where
            #: those dates came from: ``"run_archive"`` (derived from archived books) or
            #: ``None`` when nothing could be derived (then those symbols are marked at
            #: RAW -- a lower bound -- and say so via `anchor_unavailable`).
            "anchors": dict(anchors or {}),
            "anchor_source": anchor_source,
            "per_symbol": dict(per_symbol or {}),
            "stale_symbols": list(stale_symbols or []),
            "dividend_alarms": list(dividend_alarms or []),
            "nav_warnings": list(nav_warnings or []),
        }

    def _resolve_nav(
        self,
        holdings: Optional[Mapping[str, Any]],
        *,
        as_of: str,
        cash: Optional[float],
        portfolio_value: float,
        nav_override: bool,
        provider: str,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        exclude_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark the book to market and decide what `--portfolio-value` will be.

        Three paths, and only three:
          * ``computed``       -- the engine's mark (the normal live path);
          * ``override``       -- the operator explicitly pinned a NAV (`nav_override=True`);
                                  the engine is still run for the diff line, but a failed
                                  computation does NOT block the deploy the operator asked for;
          * ``fallback_input`` -- the engine failed AND ``LIVETRADING_ALLOW_NAV_FALLBACK`` is
                                  on: deploy with the form value, ERROR-logged.

        Position-open dates are derived from the run archive (`_derive_anchors`) and fed to
        the engine as ``anchors``.  Without them the engine marks at RAW -- a LOWER BOUND --
        and says so; it must never substitute the start of the factor history, which would
        capitalise pre-entry dividends into today's NAV (see `valuation.py`).

        Without the opt-in a failure raises `NavValuationError` (abort, no child process).
        """
        book = _shares_book(holdings)
        anchors, anchors_missing = self._derive_anchors(
            book,
            as_of=as_of,
            strategy_key=strategy_key,
            universe_key=universe_key,
            exclude_dir=exclude_dir,
        )
        anchor_source = "run_archive" if anchors else None
        if anchors_missing:
            logging.warning(
                "[nav] %d/%d 个标的找不到建仓日（%s），这些标的将按裸价下限估值（不含建仓以来股息）",
                len(anchors_missing),
                len(book),
                ",".join(anchors_missing[:5]) + ("..." if len(anchors_missing) > 5 else ""),
            )

        if nav_override:
            return self._resolve_nav_override(
                book,
                as_of=as_of,
                cash=cash,
                override_value=float(portfolio_value),
                provider=provider,
                anchors=anchors,
                anchor_source=anchor_source,
            )

        try:
            res = self._compute_nav_result(
                book, as_of=as_of, cash=cash, provider=provider, anchors=anchors
            )
        except NavValuationError as exc:
            if not nav_fallback_allowed():
                raise
            logging.error(
                "[nav] 估值失败，按 %s=1 回退为人工入参净值 %.2f（reason=%s, as_of=%s）：%s",
                NAV_FALLBACK_ENV,
                float(portfolio_value),
                exc.reason,
                as_of,
                exc.detail,
            )
            return self._nav_payload(
                nav=float(portfolio_value),
                nav_source="fallback_input",
                nav_computed=None,
                equity=None,
                cash=float(cash) if cash is not None else None,
                cash_source="explicit" if cash is not None else None,
                as_of=as_of,
                anchors=anchors,
                anchor_source=anchor_source,
                nav_warnings=list(exc.nav_warnings)
                + [
                    f"nav_fallback_applied: {NAV_FALLBACK_ENV}=1，估值失败后回退表单净值 "
                    f"{float(portfolio_value):.2f}；本次 NAV 未经引擎校验"
                ],
            )

        return self._nav_payload(
            nav=res.nav,
            nav_source=res.nav_source,
            nav_computed=res.equity + res.cash,
            equity=res.equity,
            cash=res.cash,
            cash_source=res.cash_source,
            as_of=res.as_of,
            anchors=anchors,
            anchor_source=anchor_source,
            per_symbol=res.per_symbol,
            stale_symbols=res.stale_symbols,
            dividend_alarms=res.dividend_alarms,
            nav_warnings=res.warnings,
        )

    def _resolve_nav_override(
        self,
        book: Mapping[str, int],
        *,
        as_of: str,
        cash: Optional[float],
        override_value: float,
        provider: str,
        anchors: Optional[Mapping[str, str]] = None,
        anchor_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Explicit human override: deploy with `override_value`, but say what was skipped.

        The engine is still run so the operator learns how far the override drifted from the
        computed mark (``本次使用了人工覆盖净值 X，计算值为 Y，差异 Z%``, at WARNING level).
        An override exists precisely for the case where the engine CANNOT run (deleted
        cache, suspended name), so a failed computation is logged rather than raised -- the
        operator has already taken responsibility for the number by ticking the box.
        """
        computed = None
        equity = None
        cash_value = float(cash) if cash is not None else None
        cash_source = "explicit" if cash is not None else None
        warnings: List[str] = []
        per_symbol: Mapping[str, Any] = {}
        stale_symbols: List[str] = []
        dividend_alarms: List[dict] = []

        try:
            res = self._compute_nav_result(
                book,
                as_of=as_of,
                cash=cash,
                provider=provider,
                nav_override=override_value,
                anchors=anchors,
            )
            computed = float(res.equity + res.cash)
            equity = res.equity
            cash_value = res.cash
            cash_source = res.cash_source
            per_symbol = res.per_symbol
            stale_symbols = list(res.stale_symbols)
            dividend_alarms = list(res.dividend_alarms)
            warnings = list(res.warnings)
        except NavValuationError as exc:
            warnings = list(exc.nav_warnings)
            logging.warning(
                "[nav] 人工覆盖净值路径：引擎无法计算参考净值（reason=%s, as_of=%s）：%s；"
                "本次直接使用人工值 %.2f，差异无法计算",
                exc.reason,
                as_of,
                exc.detail,
                override_value,
            )
            warnings.append(
                f"nav_override_no_reference: 覆盖路径下引擎无法计算参考净值（reason={exc.reason}），"
                f"人工值 {override_value:.2f} 的偏差不可量化"
            )

        if computed is not None:
            diff = override_value - computed
            diff_pct = (diff / computed * 100.0) if computed else None
            pct_text = f"{diff_pct:+.2f}%" if diff_pct is not None else "n/a（计算值为 0）"
            message = (
                f"本次使用了人工覆盖净值 {override_value:,.2f}，计算值为 {computed:,.2f}，"
                f"差异 {diff:+,.2f}（{pct_text}）"
            )
            logging.warning("[nav] %s", message)
            warnings.append("nav_override: " + message)

        return self._nav_payload(
            nav=override_value,
            nav_source="override",
            nav_computed=computed,
            equity=equity,
            cash=cash_value,
            cash_source=cash_source,
            as_of=as_of,
            anchors=anchors,
            anchor_source=anchor_source,
            per_symbol=per_symbol,
            stale_symbols=stale_symbols,
            dividend_alarms=dividend_alarms,
            nav_warnings=warnings,
        )

    def _write_nav_breakdown(self, run_dir: str, nav_info: Mapping[str, Any]) -> str:
        """Persist the per-symbol mark to its own file, never into `summary.json`.

        A 20-symbol book would add ~20 blocks to every summary; the digest keeps scalars
        plus a pointer (`nav_breakdown_file` / `nav_symbol_count`) to this file instead.
        """
        path = os.path.join(run_dir, NAV_BREAKDOWN_FILENAME)
        payload = {
            "as_of": nav_info.get("as_of"),
            "nav_effective": nav_info.get("nav_effective"),
            "nav_computed": nav_info.get("nav_computed"),
            "nav_source": nav_info.get("nav_source"),
            "equity": nav_info.get("equity"),
            "cash": nav_info.get("cash"),
            "cash_source": nav_info.get("cash_source"),
            #: Position-open dates used for the total-return ratio, and their provenance.
            #: ``None`` means every symbol inside `per_symbol` was marked at RAW (a lower
            #: bound) -- see the `anchor_unavailable` entries in `nav_warnings`.
            "anchor_source": nav_info.get("anchor_source"),
            "anchors": dict(nav_info.get("anchors") or {}),
            "raw_valuation_total": sum(
                float(e.get("value_raw") or 0.0)
                for e in (nav_info.get("per_symbol") or {}).values()
                if isinstance(e, Mapping) and e.get("counted_as") == "equity"
            ),
            "stale_symbols": list(nav_info.get("stale_symbols") or []),
            "dividend_alarms": list(nav_info.get("dividend_alarms") or []),
            "nav_warnings": list(nav_info.get("nav_warnings") or []),
            "per_symbol": dict(nav_info.get("per_symbol") or {}),
        }
        self.storage._save_json(path, payload)
        return path

    def run_health_check(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        as_of_date: Optional[str] = None,
        start_date: Optional[str] = None,
        portfolio_value: float = 100000.0,
        peak_nav: Optional[float] = None,
        data_provider: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        strategy_name: Optional[str] = None,
        universe_name: Optional[str] = None,
        allow_intraday: bool = True,
        refresh_data: bool = True,
    ) -> Dict[str, Any]:
        """Execute Stage 1 Health Check."""
        provider = data_provider or self.storage.default_data_provider or "marketdb"
        strat_key, univ_key, strat_name, univ_name = self.storage.resolve_keys(
            strategy_file=strategy_file,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            strategy_key=strategy_key,
            universe_key=universe_key,
        )
        if strategy_name:
            strat_name = strategy_name
        if universe_name:
            univ_name = universe_name

        val_date = as_of_date or self._market_clock_val_date(universe_file, custom_symbols)
        explicit_as_of = as_of_date or None
        as_of_source = "explicit" if explicit_as_of else "market_clock"
        base_dir = self.storage.get_run_dir(strat_key, univ_key, val_date, prefer_existing=True)
        # (L2.4) The archive KEY is the valuation session; asserting it here keeps
        # run_dir and summary.json['date'] from ever drifting apart.
        self._assert_archive_keyed_by(base_dir, val_date)
        # (L2.62) The FIRST run of a session keeps the flat directory; every later run gets
        # its own `<date>/<HHMMSS>/`. Stage 2 reuses whatever this stage allocated.
        pending_key = self._instance_key(strat_key, univ_key, val_date)
        run_dir = self._allocate_stage1_run_dir(base_dir, pending_key)
        self._assert_archive_keyed_by(run_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)
        # (L2.62) What this directory held BEFORE this round writes anything: empty for a
        # freshly allocated instance, so `de_merge_run_summary` cannot let an earlier
        # round's `health_*` / `error_*` / `deploy_*` fields survive into this one.
        summary_file = os.path.join(run_dir, "summary.json")
        pre_summary = self.storage._load_json(summary_file, default={}) or {}

        strat_path = self._resolve_strategy(strategy_file)
        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)
        account_state_file = os.path.join(run_dir, "account_state.json")

        # Ensure account_state_file in daily run_dir has proper pre-trade baseline
        if os.path.exists(account_state_file):
            try:
                existing_st = self.storage._load_json(account_state_file)
                if existing_st and existing_st.get("as_of_date") == val_date and "prior_state" not in existing_st:
                    carried = self.storage._carry_forward_account_state(val_date, strat_key, univ_key)
                    if carried:
                        existing_st["prior_state"] = carried
                        self.storage._save_json(account_state_file, existing_st)
            except Exception:
                pass
        else:
            try:
                # (L2.62) A fresh instance dir starts empty; seed it with the newest state
                # at-or-before this session -- which on a rerun is THIS session's earlier
                # instance (what the flat layout used to have in the same file), and on a
                # first run is the previous session's state, exactly as before.
                state_data = self.storage.latest_account_state_at_or_before(
                    val_date, strat_key, univ_key
                )
                if not state_data:
                    state_data = self.storage.get_account_state(date_str=val_date, strategy_key=strat_key, universe_key=univ_key)
                if state_data:
                    if state_data.get("as_of_date") == val_date and "prior_state" not in state_data:
                        carried = self.storage._carry_forward_account_state(val_date, strat_key, univ_key)
                        if carried:
                            state_data = {**state_data, "prior_state": carried}
                    self.storage._save_json(account_state_file, state_data)
            except Exception:
                pass

        cmd = [
            self.storage.python_executable,
            self.storage.stage1_script,
            "--strategy-file", strat_path,
            "--universe-file", univ_path,
            "--portfolio-value", str(portfolio_value),
            "--output-dir", run_dir,
            "--account-state-file", account_state_file,
            "--data-provider", provider,
        ]
        # (C6) `--as-of-date` is passed ONLY for an explicitly requested session: a
        # live run must let `common/cli_utils.py` resolve it from the market clock
        # (`date.today()` here used to force a non-existent intraday / weekend bar).
        cmd.extend(self._as_of_args(explicit_as_of))

        start_d = start_date or getattr(self.storage, "default_start_date", None)
        if start_d:
            cmd.extend(["--start-date", start_d])
        if peak_nav is not None and peak_nav > 0:
            cmd.extend(["--peak-nav", str(peak_nav)])
        if self.storage.cache_dir:
            cmd.extend(["--cache-dir", self.storage.cache_dir])
        cmd.append("--enable-intraday-5m")
        if allow_intraday:
            cmd.append("--allow-intraday")
        else:
            cmd.append("--no-allow-intraday")
        if not refresh_data:
            cmd.append("--no-refresh-data")

        print(f"[ExecutionService] Running Stage 1 Health Check: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=self.storage.repo_root,
            env=self._get_subprocess_env(),
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout
        stderr = proc.stderr
        success = proc.returncode == 0

        # Load reports
        health_rep_file = os.path.join(run_dir, STAGE1_HEALTH_REPORT_FILENAME)
        health_data = self.storage._load_json(health_rep_file, default=None)
        # (L2.4, §9/§12-11) A 0-row 5m frame must not survive as "enabled". Reconcile
        # from THIS stage's stdout marker (most specific) or Stage 1's structured
        # block, normalizing the persisted report in place when nothing was measured.
        intraday_obs = reconcile_intraday_5m_observability(
            health_data, stdout=stdout, as_of_date=val_date
        )
        if health_data and isinstance(health_data, dict):
            # Normalize fields for UI and API compatibility
            if "directive" not in health_data:
                health_data["directive"] = health_data.get("gate_code", "UNKNOWN")
            if "directive_label" not in health_data:
                health_data["directive_label"] = health_data.get("gate_headline", "")
            if "portfolio_health" in health_data:
                ph = health_data["portfolio_health"]
                health_data.setdefault("current_nav", ph.get("current_nav"))
                health_data.setdefault("peak_nav", ph.get("peak_nav"))
                health_data.setdefault("drawdown", ph.get("drawdown_pct"))
                health_data.setdefault("circuit_breaker_tier", ph.get("tier"))
                health_data.setdefault("linear_equity_scale", ph.get("linear_equity_scale"))
            if "market_macro" in health_data:
                mm = health_data["market_macro"]
                health_data.setdefault("market_breadth_50d", mm.get("breadth"))
                health_data.setdefault("breadth_thrust_10d", mm.get("thrust"))
                health_data.setdefault("target_exposure", mm.get("target_exposure"))
            if "volatility_targeting" in health_data:
                vt = health_data["volatility_targeting"]
                health_data.setdefault("realized_vol_21d", vt.get("realized_vol_21d"))
                health_data.setdefault("vol_scaling_factor", vt.get("vol_scaling_factor"))
            if "trader_actions" in health_data and "directive_summary" not in health_data:
                health_data["directive_summary"] = " \n".join(health_data["trader_actions"][:2])
            health_data["intraday_5m"] = intraday_obs
            # Re-save normalized version
            self.storage._save_json(health_rep_file, health_data)

        state_data = self.storage._load_json(account_state_file, default=None)

        # Update global account state
        if state_data:
            self.storage.save_account_state(
                state_data,
                date_str=val_date,
                strategy_key=strat_key,
                universe_key=univ_key,
                run_dir=run_dir,
            )

        # (C6) A gate refusal (`RunWindowError` / as-of `ValueError` / `StalePriceError`)
        # reaches us as a child traceback in `stderr`. Classify it so the UI shows the
        # reason and the next action instead of only `code N`; raw `stderr` is kept.
        failure = summarize_execution_failure(
            stderr, returncode=proc.returncode, label="Stage 1 Health Check"
        ) if not success else {}

        # Write summary.json
        summary = self.storage._load_json(summary_file, default={})
        round_fields = {
            # (L2.4, §9) tz-aware UTC anchor; the market-local anchor lives in `clock`.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "date": val_date,
            # (L2.4) Literal alias of `date` == the archive key: `_assert_archive_keyed_by`
            # guarantees this equals the run-dir component, so "dir names one session,
            # summary says another" cannot happen.
            "as_of": val_date,
            "strategy_key": strat_key,
            "strategy_name": strat_name,
            "strategy_file": strat_path,
            "universe_key": univ_key,
            "universe_name": univ_name,
            "universe_file": univ_path,
            "portfolio_value": portfolio_value,
            "data_provider": provider,
            "as_of_source": as_of_source,
            "health_status": "SUCCESS" if success else "FAILED",
            "health_returncode": proc.returncode,
            "health_stdout": stdout,
            "health_stderr": stderr,
        }
        summary.update(round_fields)
        owned_keys = set(round_fields)
        if failure:
            failure_fields = {k: failure[k] for k in ("error_kind", "error_headline", "error_hint", "error_detail")}
            summary.update(failure_fields)
            owned_keys |= set(failure_fields)
        clock_block = self._merge_observability(
            summary,
            valuation_date=val_date,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            as_of_source=as_of_source,
            stage="stage1",
        )
        self._apply_intraday_observability(summary, intraday_obs)
        owned_keys |= {"clock", "freshness_summary", "observability", "intraday_5m"}
        # (L2.62) De-merge: only this round's own fields survive; the inheritance whitelist
        # is empty, so nothing from an earlier same-day round can leak through.
        summary, dropped_fields = de_merge_run_summary(pre_summary, summary, owned_keys)
        if dropped_fields:
            logging.warning(
                "[summary-de-merge] %s: dropped cross-round fields %s (L2.62 继承白名单为空)",
                run_dir,
                dropped_fields,
            )
        self.storage._save_json(summary_file, summary)
        # Hand THIS round's field set to Stage 2 (same directory, same round): without it
        # the de-merge there would delete the health half of the summary it completes.
        self._record_stage1_owned_keys(pending_key, run_dir, owned_keys)

        return {
            "status": "success" if success else "error",
            "message": "Stage 1 Health Check executed successfully" if success else failure["error_message"],
            "date": val_date,
            "as_of_source": as_of_source,
            "clock": clock_block,
            "freshness_summary": summary.get("freshness_summary"),
            "intraday_5m": intraday_obs,
            "strategy_key": strat_key,
            "strategy_name": strat_name,
            "universe_key": univ_key,
            "universe_name": univ_name,
            "stdout": stdout,
            "stderr": stderr,
            "health_report": health_data,
            "account_state": state_data,
            **failure,
        }

    # --------------------------------------------------------------------------
    # Holdings-input reconciliation (no fill receipt exists -- this is advisory)
    # --------------------------------------------------------------------------
    def _check_holdings_input(
        self,
        current_holdings: Optional[Dict[str, Any]],
        val_date: str,
        strategy_key: Optional[str],
        universe_key: Optional[str],
        matrix_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare the operator's INPUT book against the previous round's PLANNED book.

        Returns the block embedded in the response as ``holdings_input_check`` and in
        ``summary.json`` under the same key, so a stale-table deploy stays forensically
        traceable after the fact. Cash proxies (BIL/CASH/...) are dropped from the INPUT
        side only: `post_holdings_from_ticket` never records cash (cash is the implied NAV
        residual), so leaving them in would flag every single deploy forever.

        ``matrix_id`` switches the REFERENCE to the previous multi-strategy netting run
        (`get_previous_multi_netting_post_holdings`): matrix runs live in
        `<archive>/<date>/multi_netted_<matrix>` and are deliberately not part of the
        `(strategy, universe)` carry-forward chain, so the per-strategy walk would report
        "no reference" on every matrix deploy and the advisory check would be inert on
        exactly the path that produced the 2026-10-27 stale-book incident.

        Strict mode (`LIVETRADING_STRICT_HOLDINGS_INPUT=1`) raises
        `HoldingsInputMismatchError` BEFORE `save_holdings()` is called, so a refused
        deploy writes no `holdings.json` -- the archive keeps the last operator-confirmed
        book instead of recording a book we just said we do not believe.

        D5 (2026-10-29 incident): the differ above cannot see a defect that sits in the
        REFERENCE itself.  That day's report said `n_mismatched: 0` -- a clean pass --
        while the input carried 600584.SH and 600362.SH, two names the model had already
        exited, because those phantom 100-share lots had been written into the planned book
        by earlier rounds.  The independent witness is the previous run's
        `account_state.json` lifecycle machine: `prev_post[sym] > 0` for a symbol whose
        recorded state is FLAT / COOLDOWN / PENDING_ENTRY means the reference is stale.
        Those symbols are flagged as `PHANTOM_REFERENCE_ENTRY` (advisory) and `clean_pass`
        becomes False, so "no mismatch" can never again be reported as a clean pass.  Under
        strict mode a flagged reference refuses the deploy, same as a mismatch.
        """
        shares_input = {
            sym: value
            for sym, value in (current_holdings or {}).items()
            if not is_cash_symbol(sym)
        }
        if matrix_id:
            prev_post, prev_run_dir = self.storage.get_previous_multi_netting_post_holdings(
                date_str=val_date, matrix_id=matrix_id
            )
        else:
            prev_post, prev_run_dir = self.storage.get_previous_post_holdings(
                date_str=val_date, strategy_key=strategy_key, universe_key=universe_key
            )

        if prev_post is None:
            # No earlier plan to compare against (first ever run, or every earlier run
            # lacked both post_holdings.json and a ticket with target_shares).
            return {
                "mismatched": {},
                "n_mismatched": 0,
                "n_symbols": len(shares_input),
                "prev_post_available": False,
                "prev_post_run_dir": None,
                "strict": strict_holdings_input_enabled(),
                "phantom_reference": {},
                "n_phantom_reference": 0,
                "phantom_reference_warning_code": PHANTOM_REFERENCE_ENTRY,
                "reference_symbols_without_lifecycle": [],
                "prev_post_lifecycle_available": False,
                "clean_pass": True,
            }

        check = diff_holdings_input_vs_prev_post(shares_input, prev_post)
        check["prev_post_available"] = True
        check["prev_post_run_dir"] = prev_run_dir
        check["strict"] = strict_holdings_input_enabled()

        # (D5) Cross-check the REFERENCE book against the previous run's lifecycle state.
        # `None` prev_run_dir (a legacy reference we could not attribute to a directory)
        # yields an empty lifecycle map -> nothing flagged, which is the correct
        # "no evidence" answer rather than a guessed one.
        prev_state: Dict[str, Any] = {}
        if prev_run_dir:
            try:
                prev_state = self.storage._load_json(
                    os.path.join(prev_run_dir, "account_state.json"), default={}
                ) or {}
            except Exception as exc:  # a broken baseline must not block the deploy
                logging.warning("[holdings-input-check] 上一轮 account_state 读取失败（忽略）: %s", exc)
                prev_state = {}
        prev_lifecycle = extract_lifecycle_states(prev_state)
        check.update(diff_reference_book_vs_lifecycle(prev_post, prev_lifecycle))
        check["prev_post_lifecycle_available"] = bool(prev_lifecycle)
        #: "Nothing to see here" needs BOTH halves: no diff AND no phantom reference.
        check["clean_pass"] = not (check["n_mismatched"] or check["n_phantom_reference"])

        if check["n_phantom_reference"]:
            phantom_line = format_phantom_reference_entries(check)
            logging.warning(
                "[holdings-input-check] %s (prev planned book: %s)",
                phantom_line,
                os.path.basename(os.path.normpath(prev_run_dir)) if prev_run_dir else "?",
            )
            if check["strict"]:
                raise HoldingsInputMismatchError(
                    "上一轮票面计划持仓本身已失效（生命周期状态显示标的不在持仓中），"
                    "已按严格模式（LIVETRADING_STRICT_HOLDINGS_INPUT=1）拒绝部署。"
                    + phantom_line
                )

        if check["n_mismatched"]:
            summary_line = format_holdings_input_mismatch(check)
            logging.warning(
                "[holdings-input-check] %s (prev planned book: %s)",
                summary_line,
                os.path.basename(os.path.normpath(prev_run_dir)) if prev_run_dir else "?",
            )
            if check["strict"]:
                raise HoldingsInputMismatchError(
                    "持仓输入与上一轮票面计划持仓不一致，已按严格模式"
                    "（LIVETRADING_STRICT_HOLDINGS_INPUT=1）拒绝部署。" + summary_line
                )
        return check

    def run_live_deploy(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        as_of_date: Optional[str] = None,
        start_date: Optional[str] = None,
        portfolio_value: float = 100000.0,
        current_holdings: Optional[Dict[str, Any]] = None,
        data_provider: Optional[str] = None,
        lot_size: Optional[int] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        strategy_name: Optional[str] = None,
        universe_name: Optional[str] = None,
        guard_latency_bars: int = 2,
        allow_intraday: bool = True,
        cash: Optional[float] = None,
        nav_override: bool = False,
        max_single_position: Optional[float] = None,
        enforce_mv_cap: bool = False,
        liquidate_stale_input_lots: bool = False,
    ) -> Dict[str, Any]:
        """Execute Stage 2 Live Deployment.

        ``cash``/``nav_override`` feed the dividend-aware NAV engine (`valuation.compute_nav`):
        ``cash=None`` means "use the parked-cash holding (BIL / 511880)", and
        ``nav_override=True`` means the operator pinned ``portfolio_value`` by hand.
        Both the archived summary and the child's ``--portfolio-value`` are derived from that
        mark, never from the raw form value (see `_resolve_nav`).

        ``max_single_position`` / ``enforce_mv_cap`` / ``liquidate_stale_input_lots`` are the
        D6/D7 delivery-time guards (2026-10-29 incident).  All three default to the
        no-order-change behaviour: `None`/`False` means the flags are NOT passed to the
        child at all, so the next live run cannot change unless somebody asks.  Passing
        ``max_single_position=0.20`` only sets the CAP the child annotates against; the
        order-changing part is ``enforce_mv_cap=True`` (CLI `--enforce-mv-cap`).  The
        child-side env kill-switches (`LIVETRADING_DISABLE_MV_CAP=1`,
        `LIVETRADING_DISABLE_STALE_LOT_LIQUIDATION=1`) win over these flags.
        """
        provider = data_provider or self.storage.default_data_provider or "marketdb"
        strat_key, univ_key, strat_name, univ_name = self.storage.resolve_keys(
            strategy_file=strategy_file,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            strategy_key=strategy_key,
            universe_key=universe_key,
        )
        if strategy_name:
            strat_name = strategy_name
        if universe_name:
            univ_name = universe_name

        val_date = as_of_date or self._market_clock_val_date(universe_file, custom_symbols)
        explicit_as_of = as_of_date or None
        as_of_source = "explicit" if explicit_as_of else "market_clock"
        base_dir = self.storage.get_run_dir(strat_key, univ_key, val_date, prefer_existing=True)
        # (L2.4) Archive key == valuation session (never the host's `date.today()`).
        self._assert_archive_keyed_by(base_dir, val_date)
        # (L2.62) Reuse the directory Stage 1 allocated for THIS round (both stages of one
        # deploy share it); otherwise allocate our own, deduping a double-click within
        # `PENDING_STAGE2_REUSE_SECONDS`.
        pending_key = self._instance_key(strat_key, univ_key, val_date)
        run_dir, inherited_owned, handoff = self._resolve_stage2_run_dir(base_dir, pending_key)
        self._assert_archive_keyed_by(run_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)
        summary_file = os.path.join(run_dir, "summary.json")
        pre_summary = self.storage._load_json(summary_file, default={}) or {}

        strat_path = self._resolve_strategy(strategy_file)
        # (L2.62) The in-process Stage-1 hand-off is the fast path; a restart / another
        # uvicorn worker has no entry, so the gate report is re-resolved from this
        # session's archive instead of being silently dropped (see the method's docstring).
        # `health_rep_file` may therefore point at an EARLIER instance's report, and the
        # summary records which one (`health_report_source` / `inherited_from`).  Resolved
        # BEFORE any further write so a refused (ungated) round leaves no half-written dir.
        health_rep_file, health_gate_fields = self._resolve_stage2_health_gate(
            base_dir, run_dir, handoff
        )
        #: This round's own copy of the gate report. It is where the post-run 5m
        #: normalization writes when the report was INHERITED, so a Stage-2-only round can
        #: never mutate a previous instance's archive (de-merge applies to files too).
        health_rep_local_file = os.path.join(run_dir, STAGE1_HEALTH_REPORT_FILENAME)

        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)
        account_state_file = os.path.join(run_dir, "account_state.json")

        # (L2.62) A fresh instance dir starts WITHOUT `account_state.json`; Stage 2 reads
        # forward-only state (lifecycle / entry-price / streak) from it, so seed it from the
        # newest run at-or-before this session -- on a rerun that is THIS session's earlier
        # instance (what the flat layout kept in the same file, and the reason the
        # carry-forward chain must not be allowed to break).
        if not os.path.exists(account_state_file):
            try:
                seed_state = self.storage.latest_account_state_at_or_before(
                    val_date, strat_key, univ_key
                )
                if seed_state:
                    self.storage._save_json(account_state_file, seed_state)
            except Exception as exc:  # a missing baseline must not block the deploy
                logging.warning("[L2.62] account_state 预置失败（忽略）: %s", exc)

        # Resolve holdings
        if current_holdings is not None:
            effective_holdings = current_holdings
        else:
            effective_holdings = self.storage.get_holdings(date_str=val_date, strategy_key=strat_key, universe_key=univ_key)

        # (NAV) Mark the book BEFORE any side effect or argv assembly.  Fails closed: a book
        # that cannot be valued is a refusal by default (see `_resolve_nav`), so the run
        # directory never gains a holdings.json for a deploy that will not happen.
        nav_info = self._resolve_nav(
            effective_holdings,
            as_of=val_date,
            cash=cash,
            portfolio_value=portfolio_value,
            nav_override=nav_override,
            provider=provider,
            strategy_key=strat_key,
            universe_key=univ_key,
            exclude_dir=run_dir,
        )
        #: The value the child receives (`--portfolio-value`).  For the computed path this is
        #: the engine's dividend-inclusive mark -- deliberately NOT `portfolio_value`.
        effective_portfolio_value = nav_info["nav_effective"]
        self._write_nav_breakdown(run_dir, nav_info)

        # Advisory reconciliation of the INPUT book against the previous round's PLANNED
        # post-trade book (see `diff_holdings_input_vs_prev_post`). This system has no
        # broker fill receipt, so a mismatch means "the table may still be holding the
        # pre-trade book" -- NOT "the trade failed". Default: warn + report; only
        # `LIVETRADING_STRICT_HOLDINGS_INPUT=1` turns it into a refusal.
        holdings_input_check = self._check_holdings_input(
            effective_holdings,
            val_date=val_date,
            strategy_key=strat_key,
            universe_key=univ_key,
        )

        # Save holdings snapshot in run_dir and globally. (L2.62) `run_dir` pins the archive
        # write to THIS round's instance directory -- "latest" would resolve the session's
        # base dir and break the carry-forward chain.
        holdings_file = os.path.join(run_dir, "holdings.json")
        self.storage.save_holdings(
            effective_holdings,
            date_str=val_date,
            strategy_key=strat_key,
            universe_key=univ_key,
            run_dir=run_dir,
        )

        cmd = [
            self.storage.python_executable,
            self.storage.stage2_script,
            "--strategy-file", strat_path,
            "--universe-file", univ_path,
            "--portfolio-value", str(effective_portfolio_value),
            "--current-holdings-file", holdings_file,
            "--output-dir", run_dir,
            "--account-state-file", account_state_file,
            "--data-provider", provider,
        ]
        # (C6) No `--as-of-date` unless the operator pinned a session explicitly.
        cmd.extend(self._as_of_args(explicit_as_of))

        start_d = start_date or getattr(self.storage, "default_start_date", None)
        if start_d:
            cmd.extend(["--start-date", start_d])
        if health_rep_file and os.path.exists(health_rep_file):
            cmd.extend(["--health-report-file", health_rep_file])
        if lot_size is not None and lot_size > 0:
            cmd.extend(["--lot-size", str(lot_size)])
        if self.storage.cache_dir:
            cmd.extend(["--cache-dir", self.storage.cache_dir])
        cmd.append("--enable-intraday-5m")
        if guard_latency_bars is not None:
            cmd.extend(["--guard-latency-bars", str(guard_latency_bars)])
        if allow_intraday:
            cmd.append("--allow-intraday")
        else:
            cmd.append("--no-allow-intraday")
        # (D7, 2026-10-29 incident) Delivery-time market-value cap. The child clamps ONLY
        # when `--enforce-mv-cap` is present; the cap VALUE alone is warn-only, so passing
        # it here cannot change an order by itself.
        if max_single_position is not None and max_single_position > 0:
            cmd.extend(["--max-single-position", str(max_single_position)])
        if enforce_mv_cap:
            cmd.append("--enforce-mv-cap")
        # (D6, 2026-10-29 incident) Residual-lot liquidation is opt-in: without this flag
        # the child only annotates + warns on a stale input lot.
        if liquidate_stale_input_lots:
            cmd.append("--liquidate-stale-input-lots")

        print(f"[ExecutionService] Running Stage 2 Live Deployment: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=self.storage.repo_root,
            env=self._get_subprocess_env(),
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout
        stderr = proc.stderr
        success = proc.returncode == 0

        # Load generated trading ticket CSV/JSON
        csv_file = os.path.join(run_dir, "live_trading_ticket.csv")
        ticket_json_file = os.path.join(run_dir, "live_trading_ticket.json")
        ticket_records = []
        #: PLANNED post-ticket book ("if every row filled as instructed"), None = unknown.
        #: Surfaced to the UI as `holdings_plan` so the holdings table can be refreshed
        #: with what the book SHOULD be -- never presented as a fill receipt.
        post_holdings: Optional[Dict[str, int]] = None
        try:
            if os.path.exists(ticket_json_file) and os.path.exists(csv_file):
                if os.path.getmtime(csv_file) > os.path.getmtime(ticket_json_file):
                    ticket_records = parse_ticket_csv(csv_file)
                    self.storage._save_json(ticket_json_file, ticket_records)
                else:
                    ticket_records = self.storage._load_json(ticket_json_file, default=[])
            elif os.path.exists(ticket_json_file):
                ticket_records = self.storage._load_json(ticket_json_file, default=[])
            elif os.path.exists(csv_file):
                ticket_records = parse_ticket_csv(csv_file)
                self.storage._save_json(ticket_json_file, ticket_records)

            if ticket_records and isinstance(ticket_records, list):
                for r in ticket_records:
                    if isinstance(r, dict) and is_placeholder_stock_name(r.get("name"), r.get("symbol")):
                        r["name"] = get_stock_name(r.get("symbol"), r.get("symbol"))
                # Save post_holdings.json for subsequent day carry-over
                post_holdings = post_holdings_from_ticket(ticket_records)
                if post_holdings is not None:
                    self.storage._save_json(os.path.join(run_dir, "post_holdings.json"), post_holdings)
        except Exception as e:
            print(f"[ExecutionService] Error loading or converting ticket: {e}")

        state_data = self.storage._load_json(account_state_file, default=None)
        # (L2.62) `health_rep_file` is `None` only when this session has no gate report at
        # all (a Stage-2-only deploy); `_read_health_report` owns the unreadable case.
        health_data = self._read_health_report(health_rep_file) if health_rep_file else None
        # (L2.4, §9/§12-11) Stage 2 fetches its own 5m series, so THIS stage's stdout
        # marker is consulted first; the report is normalized in place when zero rows.
        intraday_obs = reconcile_intraday_5m_observability(
            health_data, stdout=stdout, as_of_date=val_date
        )
        if isinstance(health_data, dict) and health_data:
            health_data["intraday_5m"] = intraday_obs
            # (L2.62) Always write THIS round's own copy. When the report was inherited
            # from an earlier instance, `health_rep_file` points at THAT archive, and
            # writing there would mutate a previous round's directory -- the file-level
            # form of the cross-round merge the de-merge prevents.
            self.storage._save_json(health_rep_local_file, health_data)

        if state_data:
            self.storage.save_account_state(
                state_data,
                date_str=val_date,
                strategy_key=strat_key,
                universe_key=univ_key,
                run_dir=run_dir,
            )

        # (C6) Classify a gate/staleness refusal out of the child's traceback so the UI
        # can say why (and what to do) instead of only `code N`; `stderr` stays raw.
        failure = summarize_execution_failure(
            stderr, returncode=proc.returncode, label="Stage 2 Live Deployment"
        ) if not success else {}

        # Update summary.json
        summary = self.storage._load_json(summary_file, default={})
        round_fields = {
            # (L2.4, §9) tz-aware UTC anchor; market-local/session live in `clock`.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "date": val_date,
            # (L2.4) Literal alias of `date` == the archive key: `_assert_archive_keyed_by`
            # guarantees this equals the run-dir component, so "dir names one session,
            # summary says another" cannot happen.
            "as_of": val_date,
            "strategy_key": strat_key,
            "strategy_name": strat_name,
            "strategy_file": strat_path,
            "universe_key": univ_key,
            "universe_name": univ_name,
            "universe_file": univ_path,
            # `portfolio_value` stays the OPERATOR'S input (audit: what was asked for);
            # `nav_effective` is what the child actually received on the command line.
            "portfolio_value": portfolio_value,
            "nav_effective": nav_info["nav_effective"],
            # (NAV) Dividend-aware mark. Scalars only -- the per-symbol detail lives in
            # `<run_dir>/nav_breakdown.json` (`nav_breakdown_file` below) so a 20-symbol
            # book cannot bloat the digest.
            "nav_computed": nav_info["nav_computed"],
            "nav_source": nav_info["nav_source"],
            "nav_equity": nav_info["equity"],
            "cash": nav_info["cash"],
            "cash_source": nav_info["cash_source"],
            "nav_stale_symbols": nav_info["stale_symbols"],
            "nav_warnings": nav_info["nav_warnings"],
            "nav_symbol_count": len(nav_info["per_symbol"]),
            #: Where the position-open dates used for the total-return ratio came from.
            #: ``"run_archive"`` = derived from archived books; ``None`` = no anchor was
            #: available and those symbols were marked at RAW (a LOWER BOUND).
            "anchor_source": nav_info["anchor_source"],
            "anchor_count": len(nav_info["anchors"]),
            "nav_breakdown_file": NAV_BREAKDOWN_FILENAME,
            "data_provider": provider,
            "as_of_source": as_of_source,
            # (L2.62) Which Stage-1 gate report this ticket obeyed -- derived from the
            # report file actually passed to the child, so a restart-inherited gate is
            # auditable (`health_report_source` / `inherited_from` / `inherited_fields`)
            # and a deploy that ran WITHOUT a gate says so (`health_gate_applied=False`)
            # instead of looking identical to a gated one.
            **health_gate_fields,
            "deploy_status": "SUCCESS" if success else "FAILED",
            "deploy_returncode": proc.returncode,
            "deploy_stdout": stdout,
            "deploy_stderr": stderr,
            "trades_count": len([r for r in ticket_records if r.get("action") in ("BUY", "SELL")]),
            # Advisory (never blocking by default): the INPUT book vs the previous round's
            # PLANNED post-ticket book, so a stale-table deploy stays reconstructable.
            "holdings_input_check": holdings_input_check,
        }
        summary.update(round_fields)
        owned_keys = set(round_fields)
        if failure:
            failure_fields = {k: failure[k] for k in ("error_kind", "error_headline", "error_hint", "error_detail")}
            summary.update(failure_fields)
            owned_keys |= set(failure_fields)
        clock_block = self._merge_observability(
            summary,
            valuation_date=val_date,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            as_of_source=as_of_source,
            stage="stage2",
        )
        self._apply_intraday_observability(summary, intraday_obs)
        owned_keys |= {"clock", "freshness_summary", "observability", "intraday_5m"}
        # (L2.62) De-merge: a same-day instance's summary holds THIS round's fields only.
        # `inherited_owned` are the fields this round's OWN Stage 1 wrote into this same
        # directory -- re-owned here so the summary keeps both halves of one round, while
        # anything from an EARLIER round is still dropped.
        summary, dropped_fields = de_merge_run_summary(
            pre_summary, summary, owned_keys | set(inherited_owned)
        )
        if dropped_fields:
            logging.warning(
                "[summary-de-merge] %s: dropped cross-round fields %s (L2.62 继承白名单为空)",
                run_dir,
                dropped_fields,
            )
        self.storage._save_json(summary_file, summary)

        return {
            "status": "success" if success else "error",
            "message": "Stage 2 Live Deployment executed successfully" if success else failure["error_message"],
            "date": val_date,
            "as_of_source": as_of_source,
            "clock": clock_block,
            "freshness_summary": summary.get("freshness_summary"),
            "intraday_5m": intraday_obs,
            "strategy_key": strat_key,
            "strategy_name": strat_name,
            "universe_key": univ_key,
            "universe_name": univ_name,
            "stdout": stdout,
            "stderr": stderr,
            "health_report": health_data,
            "trading_ticket": ticket_records,
            "account_state": state_data,
            "holdings": effective_holdings,
            # (NAV) What the book was marked at, and where that number came from. The UI
            # highlights `nav_source == "override"` and shows the cash provenance so a
            # BIL-derived cash balance is never mistaken for an operator-entered one.
            "nav_computed": nav_info["nav_computed"],
            "nav_effective": nav_info["nav_effective"],
            "nav_source": nav_info["nav_source"],
            "cash": nav_info["cash"],
            "cash_source": nav_info["cash_source"],
            "nav_warnings": nav_info["nav_warnings"],
            "anchor_source": nav_info["anchor_source"],
            # Planned post-ticket book (from `target_shares`) -- what the holdings table should
            # show after this deploy. `holdings` above is the PRE-trade input book.
            "holdings_plan": post_holdings,
            # Advisory reconciliation of the INPUT book vs the previous round's PLANNED
            # post-ticket book. See `diff_holdings_input_vs_prev_post`.
            "holdings_input_check": holdings_input_check,
            **failure,
        }

    def run_multi_strategy_netted_deploy(
        self,
        matrix_id: str = "tri_core_blend",
        strategies: Optional[List[Dict[str, Any]]] = None,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        as_of_date: Optional[str] = None,
        start_date: Optional[str] = None,
        portfolio_value: float = 100000.0,
        current_holdings: Optional[Dict[str, Any]] = None,
        data_provider: Optional[str] = None,
        min_holding_bars: int = 2,
        inertia_threshold: float = 0.05,
        allow_intraday: bool = True,
    ) -> Dict[str, Any]:
        """Execute Multi-Strategy Order Netting & Live Deployment."""
        provider = data_provider or self.storage.default_data_provider or "synthetic"
        # (C6) Same rule as Stage 1/2: the run directory is named after the market
        # clock's published session unless the operator pinned one.
        val_date = as_of_date or self._market_clock_val_date(universe_file, custom_symbols)
        explicit_as_of = as_of_date or None
        as_of_source = "explicit" if explicit_as_of else "market_clock"
        run_dir = self.storage.get_multi_netting_run_dir(matrix_id, val_date)
        # (L2.4/L2.5) Same archive-key invariant as Stage 1/2; the matrix run dir is
        # `<archive>/<as_of>/multi_netted_<matrix>`.
        self._assert_archive_keyed_by(run_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)

        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)

        # Resolve strategy pairs
        strat_args = []
        if strategies and isinstance(strategies, list):
            for s in strategies:
                k = s.get("strategy_key") or s.get("key")
                w = s.get("capital_weight", 1.0)
                if k:
                    strat_args.append(f"{k}:{w}")
        else:
            # Fall back to configured matrix
            cfgs = self.storage.get_multi_strategy_configs()
            target_cfg = next((c for c in cfgs if c.get("matrix_id") == matrix_id), cfgs[0] if cfgs else None)
            if target_cfg and "strategies" in target_cfg:
                for s in target_cfg["strategies"]:
                    strat_args.append(f"{s['strategy_key']}:{s.get('capital_weight', 1.0)}")
            else:
                strat_args = [
                    "chan_crisis_shield_blend:0.40",
                    "chan_risk_managed_blend:0.35",
                    "chan_four_state_shield_blend:0.25",
                ]

        # Resolve holdings
        if current_holdings is not None:
            effective_holdings = current_holdings
        else:
            effective_holdings = self.storage.get_holdings(date_str=val_date)

        holdings_file = os.path.join(run_dir, "holdings.json")

        # Advisory reconciliation of the INPUT book against the previous MATRIX round's
        # PLANNED post-trade book (L2.53 closure). Matrix runs do not call
        # `save_holdings`/`get_previous_post_holdings` (their directory is not part of the
        # per-(strategy, universe) carry-forward chain), so without this call the stale-book
        # check was inert on the netted path -- the very path the 2026-10-27 incident ran.
        # Same contract as Stage 2: warn + report by default, refuse only when
        # `LIVETRADING_STRICT_HOLDINGS_INPUT=1`. Raised BEFORE `holdings.json` is written.
        holdings_input_check = self._check_holdings_input(
            effective_holdings,
            val_date=val_date,
            strategy_key=None,
            universe_key=None,
            matrix_id=matrix_id,
        )

        self.storage._save_json(holdings_file, effective_holdings or {"BIL": portfolio_value})

        cmd = [
            self.storage.python_executable,
            self.storage.multi_netting_script,
            "--strategies", *strat_args,
            "--universe-file", univ_path,
            "--portfolio-value", str(portfolio_value),
            "--current-holdings-file", holdings_file,
            "--output-dir", run_dir,
            "--data-provider", provider,
            "--min-holding-bars", str(min_holding_bars),
            "--inertia-threshold", str(inertia_threshold),
        ]
        # (C6) Let the runner resolve as-of from the market clock; forward a session
        # only when the operator explicitly pinned one.
        cmd.extend(self._as_of_args(explicit_as_of))
        start_d = start_date or getattr(self.storage, "default_start_date", None)
        if start_d:
            cmd.extend(["--start-date", start_d])
        if self.storage.cache_dir:
            cmd.extend(["--cache-dir", self.storage.cache_dir])
        cmd.append("--enable-intraday-5m")
        if allow_intraday:
            cmd.append("--allow-intraday")
        else:
            cmd.append("--no-allow-intraday")

        print(f"[ExecutionService] Running Multi-Strategy Netting Deploy: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=self.storage.repo_root,
            env=self._get_subprocess_env(),
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout
        stderr = proc.stderr
        success = proc.returncode == 0

        # Load generated netting artifacts
        ticket_json_file = os.path.join(run_dir, "netted_trading_ticket.json")
        savings_json_file = os.path.join(run_dir, "friction_savings_report.json")
        ledgers_json_file = os.path.join(run_dir, "sub_ledgers.json")

        ticket_records = self.storage._load_json(ticket_json_file, default=[])
        savings_data = self.storage._load_json(savings_json_file, default={})
        ledgers_data = self.storage._load_json(ledgers_json_file, default={})

        if ticket_records and isinstance(ticket_records, list):
            for r in ticket_records:
                if isinstance(r, dict) and is_placeholder_stock_name(r.get("name"), r.get("symbol")):
                    r["name"] = get_stock_name(r.get("symbol"), r.get("symbol"))

        # Update summary.json
        # (C6) Same actionable-failure classification as Stage 1/2; `stderr` stayed raw.
        failure = summarize_execution_failure(
            stderr, returncode=proc.returncode, label="Multi-Strategy Netting Deploy"
        ) if not success else {}
        intraday_obs = reconcile_intraday_5m_observability(None, stdout=stdout, as_of_date=val_date)
        summary_file = os.path.join(run_dir, "summary.json")
        # (L2.4) MERGE, never overwrite: `run_live_multi_strategy.py` already persisted
        # the §9 `clock` / `freshness_summary` block here; replacing the file with this
        # layer's keys used to delete exactly the audit trail we are adding.
        summary = self.storage._load_json(summary_file, default={})
        summary.update({
            # (L2.4, §9) tz-aware UTC anchor; market-local/session live in `clock`.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "date": val_date,
            # (L2.4) Literal alias of `date` == the archive key: `_assert_archive_keyed_by`
            # guarantees this equals the run-dir component, so "dir names one session,
            # summary says another" cannot happen.
            "as_of": val_date,
            "matrix_id": matrix_id,
            "universe_file": univ_path,
            "portfolio_value": portfolio_value,
            "data_provider": provider,
            "as_of_source": as_of_source,
            "deploy_status": "SUCCESS" if success else "FAILED",
            "deploy_returncode": proc.returncode,
            "deploy_stdout": stdout,
            "deploy_stderr": stderr,
            "friction_savings": savings_data,
            "strategies": strat_args,
            "trades_count": len([r for r in ticket_records if r.get("action") in ("BUY", "SELL")]),
            # Advisory (never blocking by default): the INPUT book vs the previous MATRIX
            # round's planned post-ticket book, so a stale-table netted deploy stays
            # reconstructable from the archive.
            "holdings_input_check": holdings_input_check,
        })
        if failure:
            summary.update({k: failure[k] for k in ("error_kind", "error_headline", "error_hint", "error_detail")})
        clock_block = self._merge_observability(
            summary,
            valuation_date=val_date,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            as_of_source=as_of_source,
            stage="multi_netted",
        )
        self._apply_intraday_observability(summary, intraday_obs)
        self.storage._save_json(summary_file, summary)

        return {
            "status": "success" if success else "error",
            "message": "Multi-Strategy Netting Deploy executed successfully" if success else failure["error_message"],
            "date": val_date,
            "as_of_source": as_of_source,
            "clock": clock_block,
            "freshness_summary": summary.get("freshness_summary"),
            "intraday_5m": intraday_obs,
            "matrix_id": matrix_id,
            "stdout": stdout,
            "stderr": stderr,
            "trading_ticket": ticket_records,
            "friction_savings": savings_data,
            "sub_ledgers": ledgers_data,
            "summary": summary,
            "holdings": effective_holdings,
            # No `post_holdings.json` is written for matrix runs (their directory is not part
            # of the per-(strategy, universe) carry-forward chain), so the planned book is
            # derived on the fly purely for the UI's holdings-table refresh.
            "holdings_plan": post_holdings_from_ticket(ticket_records),
            # Advisory reconciliation of the INPUT book vs the previous MATRIX round's
            # PLANNED post-ticket book (`get_previous_multi_netting_post_holdings`).
            "holdings_input_check": holdings_input_check,
            **failure,
        }


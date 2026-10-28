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
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
        HoldingsInputMismatchError,
        StorageManager,
        diff_holdings_input_vs_prev_post,
        format_holdings_input_mismatch,
        is_cash_symbol,
        parse_ticket_csv,
        post_holdings_from_ticket,
        strict_holdings_input_enabled,
    )
except (ImportError, ValueError):
    from storage import (
        HoldingsInputMismatchError,
        StorageManager,
        diff_holdings_input_vs_prev_post,
        format_holdings_input_mismatch,
        is_cash_symbol,
        parse_ticket_csv,
        post_holdings_from_ticket,
        strict_holdings_input_enabled,
    )


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


class ExecutionService:
    """Orchestrates health check and live deployment runs."""

    def __init__(self, storage: StorageManager):
        self.storage = storage

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
        run_dir = self.storage.get_run_dir(strat_key, univ_key, val_date, prefer_existing=True)
        # (L2.4) The archive KEY is the valuation session; asserting it here keeps
        # run_dir and summary.json['date'] from ever drifting apart.
        self._assert_archive_keyed_by(run_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)

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
                state_data = self.storage._carry_forward_account_state(val_date, strat_key, univ_key)
                if not state_data:
                    state_data = self.storage.get_account_state(date_str=val_date, strategy_key=strat_key, universe_key=univ_key)
                if state_data:
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
        health_rep_file = os.path.join(run_dir, "stage1_health_report.json")
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
            self.storage.save_account_state(state_data, date_str=val_date, strategy_key=strat_key, universe_key=univ_key)

        # (C6) A gate refusal (`RunWindowError` / as-of `ValueError` / `StalePriceError`)
        # reaches us as a child traceback in `stderr`. Classify it so the UI shows the
        # reason and the next action instead of only `code N`; raw `stderr` is kept.
        failure = summarize_execution_failure(
            stderr, returncode=proc.returncode, label="Stage 1 Health Check"
        ) if not success else {}

        # Write summary.json
        summary_file = os.path.join(run_dir, "summary.json")
        summary = self.storage._load_json(summary_file, default={})
        summary.update({
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
        })
        if failure:
            summary.update({k: failure[k] for k in ("error_kind", "error_headline", "error_hint", "error_detail")})
        clock_block = self._merge_observability(
            summary,
            valuation_date=val_date,
            universe_file=universe_file,
            custom_symbols=custom_symbols,
            as_of_source=as_of_source,
            stage="stage1",
        )
        self._apply_intraday_observability(summary, intraday_obs)
        self.storage._save_json(summary_file, summary)

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
    ) -> Dict[str, Any]:
        """Compare the operator's INPUT book against the previous round's PLANNED book.

        Returns the block embedded in the response as ``holdings_input_check`` and in
        ``summary.json`` under the same key, so a stale-table deploy stays forensically
        traceable after the fact. Cash proxies (BIL/CASH/...) are dropped from the INPUT
        side only: `post_holdings_from_ticket` never records cash (cash is the implied NAV
        residual), so leaving them in would flag every single deploy forever.

        Strict mode (`LIVETRADING_STRICT_HOLDINGS_INPUT=1`) raises
        `HoldingsInputMismatchError` BEFORE `save_holdings()` is called, so a refused
        deploy writes no `holdings.json` -- the archive keeps the last operator-confirmed
        book instead of recording a book we just said we do not believe.
        """
        shares_input = {
            sym: value
            for sym, value in (current_holdings or {}).items()
            if not is_cash_symbol(sym)
        }
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
            }

        check = diff_holdings_input_vs_prev_post(shares_input, prev_post)
        check["prev_post_available"] = True
        check["prev_post_run_dir"] = prev_run_dir
        check["strict"] = strict_holdings_input_enabled()

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
    ) -> Dict[str, Any]:
        """Execute Stage 2 Live Deployment."""
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
        run_dir = self.storage.get_run_dir(strat_key, univ_key, val_date, prefer_existing=True)
        # (L2.4) Archive key == valuation session (never the host's `date.today()`).
        self._assert_archive_keyed_by(run_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)

        strat_path = self._resolve_strategy(strategy_file)
        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)
        account_state_file = os.path.join(run_dir, "account_state.json")
        health_rep_file = os.path.join(run_dir, "stage1_health_report.json")

        # Resolve holdings
        if current_holdings is not None:
            effective_holdings = current_holdings
        else:
            effective_holdings = self.storage.get_holdings(date_str=val_date, strategy_key=strat_key, universe_key=univ_key)

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

        # Save holdings snapshot in run_dir and globally
        holdings_file = os.path.join(run_dir, "holdings.json")
        self.storage.save_holdings(effective_holdings, date_str=val_date, strategy_key=strat_key, universe_key=univ_key)

        cmd = [
            self.storage.python_executable,
            self.storage.stage2_script,
            "--strategy-file", strat_path,
            "--universe-file", univ_path,
            "--portfolio-value", str(portfolio_value),
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
        if os.path.exists(health_rep_file):
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
        health_data = self.storage._load_json(health_rep_file, default=None)
        # (L2.4, §9/§12-11) Stage 2 fetches its own 5m series, so THIS stage's stdout
        # marker is consulted first; the report is normalized in place when zero rows.
        intraday_obs = reconcile_intraday_5m_observability(
            health_data, stdout=stdout, as_of_date=val_date
        )
        if isinstance(health_data, dict) and health_data:
            health_data["intraday_5m"] = intraday_obs
            self.storage._save_json(health_rep_file, health_data)

        if state_data:
            self.storage.save_account_state(state_data, date_str=val_date, strategy_key=strat_key, universe_key=univ_key)

        # (C6) Classify a gate/staleness refusal out of the child's traceback so the UI
        # can say why (and what to do) instead of only `code N`; `stderr` stays raw.
        failure = summarize_execution_failure(
            stderr, returncode=proc.returncode, label="Stage 2 Live Deployment"
        ) if not success else {}

        # Update summary.json
        summary_file = os.path.join(run_dir, "summary.json")
        summary = self.storage._load_json(summary_file, default={})
        summary.update({
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
            "portfolio_value": portfolio_value,
            "data_provider": provider,
            "as_of_source": as_of_source,
            "deploy_status": "SUCCESS" if success else "FAILED",
            "deploy_returncode": proc.returncode,
            "deploy_stdout": stdout,
            "deploy_stderr": stderr,
            "trades_count": len([r for r in ticket_records if r.get("action") in ("BUY", "SELL")]),
            # Advisory (never blocking by default): the INPUT book vs the previous round's
            # PLANNED post-ticket book, so a stale-table deploy stays reconstructable.
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
            stage="stage2",
        )
        self._apply_intraday_observability(summary, intraday_obs)
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
            **failure,
        }


"""Single owner of bar-freshness (session alignment) policy for mark-to-market prices.

**Why this module exists (2026-10-22 live incident, disclosed in full).**
Stage 2 (`scripts/run_live_strategy.py`) used to take
`float(universe[sym]["Close"].iloc[-1])` unconditionally -- "whatever the last
available bar is". On 2026-10-22 six symbols' cached series ended on 2026-10-21
while `as_of_date` was 2026-10-22. Five held positions were therefore marked at
the PREVIOUS session's close, the NAV was understated by ~0.70%, and every
share-quantity decision in the ticket was computed against a price that was not
today's price. Nothing warned; the ticket looked perfectly well-formed. That is
the worst failure mode available to a live executor: silent, well-formatted,
wrong.

**Invariant this module enforces.** A mark-to-market price may only be consumed
after its bar date has been compared against the *expected trading session*.
`expected_session=None` means "no expectation is known" (backtests, historical
replays, research callers) and is a deliberate never-raise legacy path -- this
module must never introduce a wall-clock dependency (`date.today()`), because
that would make backtests non-deterministic and live/backtest parity
unprovable. The market-clock phase will pass a real session string only on live
paths, so that phase is a body substitution here rather than a rewrite of every
call site.

**Strict/allow-stale policy is owned here too.** `strict=True` raises
`StalePriceError`; `strict=False` (the operator's explicit
`--allow-stale-prices` opt-out) logs loudly and returns the stale close, so the
caller can tag the ticket row instead of stopping the run. Two independent
freshness checks drifting apart is exactly the class of bug this module exists
to prevent, so everything that needs to know "is this bar stale?" must come
through `is_stale_bar()` / `latest_close()` -- never a local re-implementation
of `df.index[-1].date() != expected`.

**Dependency-light on purpose.** pandas + stdlib + logging only: no provider, no
`duckdb_cache`, no I/O. This module takes an already-loaded frame and a session
string; it does not know or care how either was obtained.

"Missing data" and "stale data" are DIFFERENT conditions and are deliberately
NOT conflated here: an absent/empty frame is the caller's fallback (e.g. the
cash-proxy `price = 1.0` default), while this module only judges frames that
actually have bars.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd


_LOGGER = logging.getLogger(__name__)


class StalePriceError(ValueError):
    """A mark-to-market close does not belong to the expected trading session.

    Subclasses `ValueError` so generic "bad input" handlers catch it, while a
    caller that specifically wants to distinguish "stale market data" from
    "malformed config" can catch this narrower type.
    """


def _bar_date(value: Any) -> Optional[date]:
    """Best-effort calendar date for a DatetimeIndex entry / Timestamp / date."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        # datetime.datetime is a date subclass too; .date() normalizes both.
        return value.date() if hasattr(value, "date") else value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def last_bar_session(df: Optional[pd.DataFrame]) -> Optional[str]:
    """ISO date of a frame's last bar, or None when it has no bars.

    None (not an exception) for an absent/empty frame: "no data" is the
    caller's problem to handle, see the module docstring.
    """
    if df is None or df.empty or len(df.index) == 0:
        return None
    bar_d = _bar_date(df.index[-1])
    return bar_d.isoformat() if bar_d is not None else None


def is_stale_bar(
    df: Optional[pd.DataFrame],
    expected_session: Optional[str],
) -> bool:
    """True when the frame's last bar is NOT the `expected_session` bar.

    The single implementation of the comparison the incident was missing.
    `expected_session=None` always returns False (no expectation => nothing can
    be stale); an absent/empty frame also returns False, because "no data" is
    handled by the caller's fallback rather than mislabelled as staleness.
    """
    if expected_session is None:
        return False
    bar = last_bar_session(df)
    if bar is None:
        return False
    try:
        expected = pd.Timestamp(expected_session).date().isoformat()
    except (TypeError, ValueError):
        # An unparseable expectation is a caller bug, not a market-data bug:
        # never silently declare fresh OR stale on garbage input.
        _LOGGER.warning(
            "is_stale_bar: unparseable expected_session=%r; treating bar %s as not stale",
            expected_session,
            bar,
        )
        return False
    # Deliberately ONE-SIDED: only a bar OLDER than the expected session is stale.
    # A bar NEWER than `expected_session` is silently accepted, because both live
    # callers truncate their frame to `as_of_date` upstream (via `as_of_universe`).
    # A future caller that does NOT truncate must not assume the future bar is
    # rejected here -- it is accepted.
    return bar < expected


def latest_close(
    universe: Dict[str, pd.DataFrame],
    sym: str,
    expected_session: Optional[str],
    strict: bool,
) -> float:
    """Return `universe[sym]`'s last close, enforcing session freshness.

    Parameters
    ----------
    universe:
        Already-loaded `symbol -> OHLCV DataFrame` mapping. The caller must have
        handled the symbol being absent / the frame being empty (this function is
        only reached when a frame with bars exists, and raises ValueError
        otherwise, because that is a caller-contract violation, not market data).
    sym:
        Symbol key. Named in every error/warning message so an operator can act.
    expected_session:
        ISO "YYYY-MM-DD" of the session the price must belong to. `None` is the
        documented legacy never-raise path (backtests / research callers).
    strict:
        `True` => raise `StalePriceError` on a stale bar (live default).
        `False` => log a loud warning and return the stale close, so the caller
        can tag the affected ticket row (operator's --allow-stale-prices).

    Returns
    -------
    float
        The last close. Always a plain float -- no tuple/dict, so call sites stay
        readable and the market-clock phase can swap the body alone.

    Raises
    ------
    ValueError
        If `sym` is missing from `universe` or its frame is empty -- the caller
        must keep the "no data at all" fallback at the call site.
    StalePriceError
        When `strict` and the last bar predates `expected_session`.
    """
    df = universe.get(sym)
    if df is None or df.empty:
        raise ValueError(
            f"latest_close({sym}): empty/absent frame passed to the freshness gate. "
            "'No data at all' must be handled by the CALLER (fallback price), not here."
        )
    price = float(df["Close"].iloc[-1])

    if expected_session is None:
        # Legacy / research path: no session expectation, never raise.
        return price

    if not is_stale_bar(df, expected_session):
        return price

    bar = last_bar_session(df)
    expect_iso = pd.Timestamp(expected_session).date().isoformat()
    msg = (
        f"陈旧价格拒绝执行 (STALE PRICE): 标的 {sym} 的最新收盘价 {price:.4f} 来自 "
        f"{bar} 的 K 线，但本次估值/交易会话为 {expect_iso}。"
        f"该价格并非 {expect_iso} 的收盘价，盯市 NAV、权重与下单股数都会基于过期价格被静默算出。"
        " 修复方式：重新拉取该标的至 " + expect_iso + " 的行情；"
        "若确属非交易日/数据源已知缺口的人工演练，可显式传入 --allow-stale-prices "
        "将其降级为警告（此时工单对应行会被标记 price_stale=True）。"
    )
    if strict:
        raise StalePriceError(msg)

    print(f"[WARN] {msg} [--allow-stale-prices 已显式开启：降级为警告，请人工复核后再下单]")
    _LOGGER.warning("stale price allowed for %s: bar=%s expected=%s", sym, bar, expect_iso)
    return price


# --------------------------------------------------------------------------- #
# Universe-level freshness summary + hard assertion (L2.3, design §9)
# --------------------------------------------------------------------------- #
def build_freshness_summary(
    universe: Dict[str, pd.DataFrame],
    expected_session: Optional[str],
    *,
    expected_symbols: Optional[Iterable[str]] = None,
    exclude: Iterable[str] = (),
    stage: Optional[str] = None,
    strict: Optional[bool] = None,
    fetch_errors: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Per-symbol bar-date audit of a loaded universe (design §9 `freshness_summary`).

    THE single implementation of "which symbols' bars are not the expected session".
    Both live stages persist this block into `stage1_health_report.json` /
    `summary.json`; do not compute a parallel version in a script (design §7: two
    drifting checks are precisely the defect being removed).

    Parameters
    ----------
    universe:
        `symbol -> OHLCV frame`, already truncated to the as-of date by the caller
        (`pipeline.live_signal.lsig.signal.as_of_universe`). A symbol whose frame is
        missing is only reported as `missing` when `expected_symbols` is supplied --
        "absent" and "stale" stay distinct conditions, but neither is allowed to
        masquerade as fresh.
    expected_session:
        ISO date of the session every bar must belong to. `None` => `NOT_CHECKED`
        (backtest/research path), never a silent PASS.
    expected_symbols / exclude:
        Symbols that must be present, minus ones that legitimately have no bar
        (e.g. a cash proxy that is not in the traded universe).
    fetch_errors:
        Optional `symbol -> "Provider: TypeName: message"` map produced by the
        Stage-1 refresh (`scripts/sync_china_5m_and_indices.py`). It does NOT change
        `status` (a symbol with no usable frame is already `missing`, hence FAIL);
        it only records WHY a symbol is missing. "Fetched zero bars" and "the fetch
        raised" both land in `missing_symbols`, but only the latter also appears here
        -- without it the two conditions are indistinguishable after the fact
        (2026-10-27: a GBK error page from 601288.SH and a genuinely empty symbol
        looked identical).

    Returns
    -------
    dict with `status` in {PASS, FAIL, NOT_CHECKED}, per-symbol `price_bar_dates`,
    `stale` (bar < expected) and `missing_symbols` lists, plus `fetch_errors`.
    """
    expected_iso = None
    if expected_session is not None:
        try:
            expected_iso = pd.Timestamp(expected_session).date().isoformat()
        except (TypeError, ValueError):
            expected_iso = str(expected_session)

    excluded = {str(s) for s in exclude}
    bar_dates: Dict[str, Optional[str]] = {}
    stale: List[Dict[str, str]] = []
    for sym, df in (universe or {}).items():
        if str(sym) in excluded:
            continue
        bar_dates[str(sym)] = last_bar_session(df)
        if expected_iso is not None and is_stale_bar(df, expected_iso):
            stale.append({"symbol": str(sym), "bar_session": last_bar_session(df)})

    missing: List[str] = []
    if expected_symbols is not None:
        for sym in expected_symbols:
            if str(sym) in excluded:
                continue
            if sym not in (universe or {}):
                missing.append(str(sym))
                bar_dates.setdefault(str(sym), None)

    if expected_iso is None:
        status = "NOT_CHECKED"
    elif stale or missing:
        status = "FAIL"
    else:
        status = "PASS"

    return {
        "stage": stage,
        "expected_session": expected_iso,
        "strict": (None if strict is None else bool(strict)),
        "status": status,
        "n_expected": len(bar_dates),
        "n_fresh": len([s for s, d in bar_dates.items() if d == expected_iso]),
        "n_stale": len(stale),
        "n_missing": len(missing),
        "stale": stale,
        "missing_symbols": missing,
        "price_bar_dates": bar_dates,
        # Additive, never status-changing: see the `fetch_errors` docstring above.
        "fetch_errors": {str(k): str(v) for k, v in dict(fetch_errors or {}).items()},
    }


def assert_fresh_universe(
    universe: Dict[str, pd.DataFrame],
    expected_session: Optional[str],
    *,
    strict: bool,
    expected_symbols: Optional[Iterable[str]] = None,
    exclude: Iterable[str] = (),
    stage: Optional[str] = None,
    fetch_errors: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """`build_freshness_summary` + the hard assertion (design §8 `--strict-freshness`).

    `strict=True` raises `StalePriceError` listing every offending symbol (the bar it
    would have been valued at vs the session that was expected). `strict=False` is
    the operator's explicit downgrade and only prints/logs the same list. Either way
    the returned summary is what the caller persists, so a downgraded run still
    carries its own evidence.

    `fetch_errors` (optional) annotates WHY a symbol is missing: see
    `build_freshness_summary`. It never changes the verdict -- a missing frame is a
    FAIL either way -- it only stops "the provider answered with an error page" from
    being reported as "this symbol has no bars".
    """
    summary = build_freshness_summary(
        universe,
        expected_session,
        expected_symbols=expected_symbols,
        exclude=exclude,
        stage=stage,
        strict=strict,
        fetch_errors=fetch_errors,
    )
    if summary["status"] == "PASS" or summary["status"] == "NOT_CHECKED":
        if summary["status"] == "PASS":
            print(
                f"[FRESHNESS PASS] as_of={summary['expected_session']} "
                f"({summary['n_fresh']}/{summary['n_expected']} 标的的收盘 bar 属于该交易日)"
            )
        return summary

    errors = summary.get("fetch_errors") or {}
    lines = [
        f"标的 {row['symbol']}: 最新 bar = {row['bar_session']} (< as_of {summary['expected_session']})"
        for row in summary["stale"]
    ] + [
        (
            f"标的 {sym}: 抓取报错（不是「无数据」）: {errors[sym]}"
            if sym in errors
            else f"标的 {sym}: 完全没有可用的日线 frame (as_of {summary['expected_session']})"
        )
        for sym in summary["missing_symbols"]
    ]
    detail = "\n  - ".join(lines)
    msg = (
        f"[FRESHNESS FAIL] {stage or 'live'} 数据新鲜度断言失败: as_of="
        f"{summary['expected_session']}，{len(summary['stale'])} 个标的的 bar 早于该交易日、"
        f"{len(summary['missing_symbols'])} 个标的无行情。逐条:\n  - {detail}\n"
        "陈旧收盘价会静默污染盯市 NAV、目标权重与下单股数（2026-10-22 事故：6 只标的价格停在 10-21）。"
        "修复方式：重新同步这些标的至 as_of 的行情；若确属休市/数据源已知缺口的人工演练，"
        "可显式 --no-strict-freshness（或 --allow-stale-prices）降级为警告。"
    )
    if strict:
        raise StalePriceError(msg)
    print(f"[WARN] {msg} [已显式降级为警告，请人工复核后再下单]")
    _LOGGER.warning("freshness downgraded to warning: %s", summary["stale"])
    return summary

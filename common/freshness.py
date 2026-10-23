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
from typing import Any, Dict, Optional

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

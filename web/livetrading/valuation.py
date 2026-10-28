"""Dividend-aware portfolio NAV engine for the Live Trading station.

WHY THIS MODULE EXISTS
----------------------
The live station used to mark a portfolio with **raw** closes alone
(``shares x close``).  For a dividend-paying holding that silently drops every
cash distribution out of the equity curve: the share count does not move on the
ex-dividend day, the raw price does, so the account appears to lose exactly the
dividend -- forever, permanently, at every payment date.  A backtest that sizes
positions off such a NAV will under-invest and a circuit breaker reading such a
NAV will fire on a *non-event*.

TOTAL-RETURN MARKING, AND WHY IT IS A RATIO (NOT AN ENTRY PRICE)
----------------------------------------------------------------
BaoStock defines the backward adjustment family as::

    fore  = back / back[last]        # forward (qfq) factor
    hfq   = raw  x back              # backward (total-return) price

so ``back`` is the cumulative total-return multiplier.  A mark that is
dividend-inclusive is therefore::

    value_i = shares_i x raw_close_i,as_of x (factor_hfq_i,as_of / factor_hfq_i,anchor)

Deliberately a **ratio between two factor readings**, never an absolute
``hfq`` price: dividing by the anchor factor makes the valuation invariant to
the table's normalisation (BaoStock re-normalises ``back`` so the *latest* row
is 1.0; a stored table can be re-normalised at any sync without any economic
event).  An absolute ``hfq`` price would silently rescale the whole portfolio
on such a re-normalisation.  The ratio only cares about *relative* change since
the anchor.

DOUBLE-COUNTING BAN (the load-bearing invariant)
------------------------------------------------
Bonus issues (送股), stock splits (拆股) and rights issues (配股) are ALREADY
inside ``factor_hfq``: on the ex-date the raw price divides by exactly the
factor step, so ``raw x factor`` is continuous *with an unchanged share
count*.  This engine therefore **must never change ``shares``** for a corporate
action.  Multiplying the share count AND applying the factor step would credit
the split twice -- a 2:1 split would double the position twice over.  The
``holdings`` argument is the *shares actually held today*, and the sole
correction applied is the factor ratio.  ``test_split_never_double_counts``
pins this: raw 10 -> 5 with factor 1.0 -> 2.0 must leave the mark untouched.

CASH
----
Cash is an explicit input.  When it is absent this engine falls back to the
market value of a parked-cash holding (``BIL`` / ``511880`` money-market
vehicles), because in this station's operations unallocated cash is usually
swept into one of those.  The fallback symbol is valued at its **raw** close
(its distributions are already inside its own NAV) and is moved out of
``equity`` into ``cash`` so the same money is never counted in both legs.
When neither an explicit amount nor a parked-cash holding exists the result is
NOT a silent zero: ``cash_source="missing"`` and, with ``fail_closed=True``, a
raised error.

STALENESS
---------
A symbol whose last bar is older than ``as_of`` is "stale".  ``fail_closed=True``
raises :class:`common.freshness.StalePriceError` (the repo's existing type, a
``ValueError`` subclass); ``fail_closed=False`` marks it with the last available
close, records it in ``NavResult.stale_symbols`` and emits a warning.  Because
the staleness rule is one-sided, ``as_of`` must be the **last trading session**
the caller intends to mark: a Saturday ``as_of`` makes every symbol stale and
will (correctly) fail closed.

DIVIDEND ALARMS
---------------
``dividend_alarms`` reverse-engineers suspected ex-dividend events out of
factor steps: with ``ratio = back[t-1] / back[t] < 1 - 1e-4`` the implied cash
amount is ``D = raw_close[t-1] x (1 - ratio)``.  Alarms are **observability
only**: they never touch cash, never touch share count, never change the mark.

DATA ACCESS
-----------
Prices and factors are read through a *duck-typed* cache (``DuckDBCache``
compatible), so tests inject fakes and this module imports nothing heavy::

    query_bars(provider, symbol, start=..., end=..., interval="1d", adjust="raw")
    query_adjust_factors(symbol, start=None, end=as_of)
    get_latest_factor_hfq(symbol)

``adjust="raw"`` is passed EXPLICITLY: the real ``DuckDBCache.query_bars``
defaults to ``adjust="qfq"``, and a qfq price multiplied by a back factor would
double-apply the adjustment.  A cache that cannot honour ``adjust=`` is not
adapted to -- silently marking qfq prices is worse than failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

# `common.freshness` is pandas+logging only (no fastapi, no duckdb), so this
# top-level import keeps `import web.livetrading.valuation` cheap.
from common.freshness import StalePriceError, is_stale_bar

__all__ = [
    "NavResult",
    "compute_nav",
    "MissingCashError",
    "StalePriceError",
    "DIVIDEND_MODES",
]

#: Marking modes.  `raw` exists for comparison / last-resort fallback only.
DIVIDEND_MODES = ("total_return", "raw")

#: Parked-cash vehicles, in priority order.  Both are treated as cash
#: equivalents, so their own distributions stay inside their (raw) price.
CASH_FALLBACK_SYMBOLS = ("BIL", "511880")

#: A backward-factor step larger than this is (almost certainly) a corporate
#: action rather than float noise.  1e-4 == 1bp, far above re-sync noise
#: (~1e-16) and far below any real dividend (~10bp-500bp of price).
_DIVIDEND_STEP_RTOL = 1e-4

#: Extra calendar days fetched around the dividend-scan window so the "previous
#: trading close" of an ex-date near the window edge is still available.
_SCAN_PAD_DAYS = 14

_DEFAULT_PROVIDER = "marketdb"


class MissingCashError(ValueError):
    """No cash amount could be resolved from any of the three sources.

    Subclasses ``ValueError`` so generic input-validation handlers still catch
    it.  Raised only when ``fail_closed=True``; otherwise the result reports
    ``cash_source="missing"`` with ``cash=0.0`` and a loud warning.
    """


@dataclass(frozen=True)
class NavResult:
    """A marked portfolio. All monetary values are in the account currency.

    ``nav`` is ``equity + cash`` unless ``nav_source == "override"``, in which
    case ``nav`` is the caller-supplied figure while ``equity``/``cash``/
    ``per_symbol`` remain the engine's own computation (kept so an operator can
    see how far the override drifted).
    """

    nav: float
    equity: float
    cash: float
    cash_source: str  # "explicit" | "bil_fallback" | "missing"
    nav_source: str  # "computed" | "override"
    as_of: str
    per_symbol: dict
    stale_symbols: list
    dividend_alarms: list
    warnings: list


# ---------------------------------------------------------------------------
# small as-of helpers (factor tables are step functions, never interpolated)
# ---------------------------------------------------------------------------
def _to_ts(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.normalize()


def _asof_value(series: Optional[pd.Series], ts: pd.Timestamp) -> Optional[float]:
    """Last value at or before ``ts`` (no interpolation, no look-ahead)."""
    if series is None or series.empty:
        return None
    idx = series.index
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.DatetimeIndex(pd.to_datetime(idx))
        series = pd.Series(series.to_numpy(), index=idx).sort_index()
    pos = idx.searchsorted(ts, side="right") - 1
    if pos < 0:
        return None
    val = series.iloc[pos]
    if pd.isna(val):
        return None
    return float(val)


def _extract_close(bars: pd.DataFrame) -> Optional[pd.Series]:
    """Raw close series, tolerating both the cache's ``Close`` and a fake's ``close``."""
    if bars is None or getattr(bars, "empty", True):
        return None
    for col in ("Close", "close"):
        if col in bars.columns:
            s = pd.to_numeric(bars[col], errors="coerce").astype(float)
            s = s.dropna()
            if s.empty:
                return None
            if not isinstance(s.index, pd.DatetimeIndex):
                s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
            return s.sort_index()
    return None


def _factor_series(factors: Any) -> Optional[pd.Series]:
    """``factor_hfq`` series from a ``query_adjust_factors`` frame."""
    if factors is None or getattr(factors, "empty", True):
        return None
    if "factor_hfq" not in factors.columns:
        return None
    s = pd.to_numeric(factors["factor_hfq"], errors="coerce").astype(float).dropna()
    if s.empty:
        return None
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
    return s.sort_index()


def _fetch_bars(
    cache,
    provider: str,
    symbol: str,
    start: Optional[pd.Timestamp],
    end: pd.Timestamp,
    warnings_list: List[str],
) -> Optional[pd.Series]:
    """Raw closes in ``[start, end]``; widens to full history when the window is empty.

    The widening matters for long suspensions: a name halted for longer than the
    dividend-scan window has no bar in ``[start, end]`` at all, yet its last
    close before the halt is exactly the mark ``fail_closed=False`` wants.
    """
    kwargs = dict(interval="1d", adjust="raw")
    bars = cache.query_bars(
        provider,
        symbol,
        start=None if start is None else start.date().isoformat(),
        end=end.date().isoformat(),
        **kwargs,
    )
    closes = _extract_close(bars)
    if closes is None and start is not None:
        warnings_list.append(
            f"no_bar_in_scan_window: {symbol} has no bar in "
            f"[{start.date().isoformat()}, {end.date().isoformat()}]; "
            f"widening the read to the full stored history"
        )
        bars = cache.query_bars(
            provider,
            symbol,
            start=None,
            end=end.date().isoformat(),
            **kwargs,
        )
        closes = _extract_close(bars)
    if closes is not None:
        # Never read past the mark date, whatever a fake cache returns.
        closes = closes[closes.index <= end]
        if closes.empty:
            return None
    return closes


def _query_factors(cache, symbol: str, as_of: pd.Timestamp) -> Optional[pd.Series]:
    """Factor history strictly up to ``as_of``.

    The real ``DuckDBCache.query_adjust_factors(symbol, start, end)`` documents
    that ``start`` is NOT applied, so passing ``start=None`` is both correct and
    necessary: the earliest row of the returned frame is the start of the stored
    factor history (used only to DETECT the ``anchor_before_factor_history`` case
    and for the diagnostic ``full_history_ratio`` -- never as a valuation anchor).
    """
    factors = cache.query_adjust_factors(
        symbol, start=None, end=as_of.date().isoformat()
    )
    return _factor_series(factors)


def _scan_dividend_alarms(
    symbol: str,
    factors: Optional[pd.Series],
    closes: Optional[pd.Series],
    window_start: pd.Timestamp,
    as_of: pd.Timestamp,
    warnings_list: List[str],
) -> List[dict]:
    """Reverse-engineer suspected ex-dividend events from backward-factor steps."""
    if factors is None or len(factors) < 2:
        return []
    # Seed the slice ONE row before `window_start` so a step landing exactly on the
    # window's first session is still detectable (it needs a predecessor to compare to).
    start_pos = max(int(factors.index.searchsorted(window_start, side="left")) - 1, 0)
    window = factors.iloc[start_pos:]
    window = window[window.index <= as_of]
    if len(window) < 2:
        return []

    alarms: List[dict] = []
    prev_date = None
    prev_back = None
    for ts, back in window.items():
        if prev_back is not None and prev_back > 0 and back > 0:
            ratio = float(prev_back) / float(back)
            if ratio < 1.0 - _DIVIDEND_STEP_RTOL:
                raw_prev = _asof_value(closes, prev_date)
                if raw_prev is None:
                    warnings_list.append(
                        f"dividend_alarm_skipped_no_prev_close: {symbol} "
                        f"ex_date={pd.Timestamp(ts).date().isoformat()}"
                    )
                else:
                    alarms.append(
                        {
                            "symbol": symbol,
                            "ex_date": pd.Timestamp(ts).date().isoformat(),
                            "implied_dividend_per_share": float(raw_prev * (1.0 - ratio)),
                            "raw_prev_close": float(raw_prev),
                            "ratio": ratio,
                        }
                    )
        prev_date = pd.Timestamp(ts)
        prev_back = float(back)
    return alarms


def compute_nav(
    holdings: Mapping[str, int],
    *,
    as_of: str,
    cache,
    cash: Optional[float] = None,
    anchors: Optional[Mapping[str, str]] = None,
    nav_override: Optional[float] = None,
    dividend_mode: str = "total_return",
    fail_closed: bool = True,
    dividend_scan_days: int = 30,
    provider: str = _DEFAULT_PROVIDER,
) -> NavResult:
    """Mark ``holdings`` to market as of ``as_of`` -- dividend-inclusive by default.

    Parameters
    ----------
    holdings:
        ``symbol -> shares``, the shares actually held **today**.  Corporate
        actions are already inside ``factor_hfq``, so shares are never adjusted
        here (see the module docstring's DOUBLE-COUNTING BAN).
    as_of:
        Mark date, ``YYYY-MM-DD``.  Must be a trading session for every holding
        when ``fail_closed=True``.
    cache:
        Duck-typed cache exposing ``query_bars``, ``query_adjust_factors`` and
        (optionally) ``get_latest_factor_hfq``.
    cash:
        Explicit cash balance.  When ``None``, the value of a parked-cash
        holding (``BIL`` then ``511880``) is used and moved out of ``equity``.
    anchors:
        ``symbol -> position-open date``.  The factor ratio is measured from
        here.  **A missing anchor degrades that symbol to RAW valuation** (ratio
        ``1.0``, ``anchor_unavailable`` warning) -- a LOWER BOUND that excludes
        every dividend paid since entry.  Falling back to the **earliest
        available factor date** is FORBIDDEN: ``factor_hfq`` is monotone
        increasing, so that ratio capitalises *pre-entry* dividends into today's
        value and systemically OVERSTATES NAV (a 2016 anchor on a 2026 mark is
        typically 1.6x+, i.e. the whole book is sized ~60% too large, because
        ``--portfolio-value`` is the denominator of every order's size).  An
        anchor that is later than ``as_of``, or that precedes the stored factor
        history, degrades the same way for the same reason.
        The per-symbol entry exposes ``value_raw`` (``shares x raw_close``,
        the no-anchor floor), ``anchor_inflation_ratio`` (``factor_mark /
        factor_anchor``, exactly ``1.0`` when the symbol was marked at raw) and
        ``full_history_ratio`` (a DIAGNOSTIC: what the rejected
        earliest-date fallback would have applied, so the size of the error is
        measurable instead of asserted).
    nav_override:
        Explicit NAV override.  ``nav`` becomes this value while
        ``equity``/``cash``/``per_symbol`` stay computed, plus a
        ``nav_override_applied`` warning.
    dividend_mode:
        ``"total_return"`` (default, dividend-inclusive) or ``"raw"``
        (comparison/fallback: ``shares x raw_close`` only).
    fail_closed:
        Raise on stale prices / unresolvable cash instead of degrading.
    dividend_scan_days:
        Natural-day window scanned for factor steps (alarm only).
    provider:
        Provider key for ``query_bars`` (the wire-in caller supplies it; default
        ``"marketdb"`` matches the station's default chain).

    Raises
    ------
    StalePriceError
        (``ValueError`` subclass) when a holding has no bar on ``as_of`` and
        ``fail_closed=True``; also when a holding has no price at all.
    MissingCashError
        (``ValueError`` subclass) when cash is unresolvable and
        ``fail_closed=True``.
    ValueError
        On bad ``dividend_mode`` / negative shares / unparseable ``as_of``.
        Adjustment-basis errors from the cache (e.g. ``UnknownAdjustBasisError``)
        propagate unchanged: a wrong-basis read is a data-integrity failure, not
        a staleness one, and must never be degraded into a price.
    """
    as_of_ts = _to_ts(as_of)
    if as_of_ts is None:
        raise ValueError(f"compute_nav: unparseable as_of={as_of!r}")
    as_of_iso = as_of_ts.date().isoformat()

    if dividend_mode not in DIVIDEND_MODES:
        raise ValueError(
            f"compute_nav: dividend_mode must be one of {DIVIDEND_MODES}, got {dividend_mode!r}"
        )
    try:
        scan_days = int(dividend_scan_days)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"compute_nav: dividend_scan_days must be an int, got {dividend_scan_days!r}"
        ) from exc
    if scan_days < 0:
        raise ValueError(f"compute_nav: dividend_scan_days must be >= 0, got {scan_days}")

    anchors = dict(anchors or {})
    warnings_list: List[str] = []
    stale_symbols: List[str] = []
    dividend_alarms: List[dict] = []
    per_symbol: Dict[str, dict] = {}

    scan_start = as_of_ts - pd.Timedelta(days=scan_days + _SCAN_PAD_DAYS)

    for symbol in sorted(holdings, key=str):
        raw_shares = holdings[symbol]
        try:
            shares = int(raw_shares)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"compute_nav: shares for {symbol} must be an int, got {raw_shares!r}"
            ) from exc
        if shares < 0:
            raise ValueError(f"compute_nav: negative share count for {symbol}: {shares}")
        if shares == 0:
            warnings_list.append(f"zero_shares_ignored: {symbol}")
            continue

        closes = _fetch_bars(cache, provider, symbol, scan_start, as_of_ts, warnings_list)
        if closes is None or closes.empty:
            if fail_closed:
                raise StalePriceError(
                    f"compute_nav: no price at all for {symbol} on or before {as_of_iso} "
                    f"(raw basis, provider={provider})"
                )
            warnings_list.append(
                f"no_price_available: {symbol} has no bar on or before {as_of_iso}; "
                f"marked at 0.0 with its shares still listed"
            )
            stale_symbols.append(symbol)
            per_symbol[symbol] = {
                "shares": shares,
                "raw_close": None,
                "factor_hfq": None,
                "anchor_date": None,
                "value": 0.0,
                "value_raw": 0.0,
                "anchor_inflation_ratio": 1.0,
                "full_history_ratio": None,
                "dividend_basis": "raw",
                "stale": True,
                "mark_date": None,
                "factor_anchor_hfq": None,
                "counted_as": "equity",
            }
            continue

        mark_ts = pd.Timestamp(closes.index[-1]).normalize()
        stale = is_stale_bar(closes.to_frame("Close"), expected_session=as_of_iso)
        if stale:
            if fail_closed:
                raise StalePriceError(
                    f"compute_nav: {symbol} has no bar on {as_of_iso}; last stored bar is "
                    f"{mark_ts.date().isoformat()} (provider={provider}). Pass the last "
                    f"trading session as as_of, or fail_closed=False to mark stale."
                )
            stale_symbols.append(symbol)
            warnings_list.append(
                f"stale_price: {symbol} has no bar on {as_of_iso}; using the last available "
                f"close from {mark_ts.date().isoformat()}"
            )

        raw_close = float(closes.iloc[-1])
        mark_iso = mark_ts.date().isoformat()

        factors = _query_factors(cache, symbol, as_of_ts)
        dividend_alarms.extend(
            _scan_dividend_alarms(
                symbol,
                factors,
                closes,
                as_of_ts - pd.Timedelta(days=scan_days),
                as_of_ts,
                warnings_list,
            )
        )

        factor_mark: Optional[float] = None
        factor_anchor: Optional[float] = None
        anchor_iso: Optional[str] = None
        used_total_return = dividend_mode == "total_return"
        #: DIAGNOSTIC (never used in valuation): the ratio the REJECTED
        #: "earliest factor date" fallback would have applied to this symbol.
        full_history_ratio: Optional[float] = None

        if used_total_return:
            if factors is None or factors.empty:
                used_total_return = False
                warnings_list.append(
                    f"factor_missing: {symbol} has no factor_hfq history up to {as_of_iso}; "
                    f"falling back to RAW valuation (dividends excluded)"
                )
            else:
                factor_mark = _asof_value(factors, mark_ts)
                if factor_mark is None or factor_mark <= 0:
                    used_total_return = False
                    warnings_list.append(
                        f"factor_missing: {symbol} has no usable factor_hfq at {mark_iso}; "
                        f"falling back to RAW valuation (dividends excluded)"
                    )
                else:
                    first_ts = pd.Timestamp(factors.index[0]).normalize()
                    first_factor = float(factors.iloc[0])
                    if first_factor > 0:
                        full_history_ratio = float(factor_mark / first_factor)

                    anchor_ts = _to_ts(anchors.get(symbol))
                    if anchor_ts is None:
                        # (P0, 2026-10-27) The old code anchored on the earliest factor
                        # date here.  `factor_hfq` is monotone increasing (601398.SH:
                        # 1.6447 on 2026-10-19, ~1.0 at its 2016 start), so that ratio
                        # RE-CAPITALISED every pre-entry dividend into today's value and
                        # overstated NAV by the whole dividend history -- and NAV is the
                        # denominator of `target_weight x NAV / price`, so every ticket
                        # was over-sized in the same proportion.  A missing anchor is
                        # now a LOWER BOUND (raw), never a silent upgrade.
                        used_total_return = False
                        warnings_list.append(
                            f"anchor_unavailable: no position-open date for {symbol}; marking "
                            f"at RAW (ex-dividend close) -- a LOWER BOUND that EXCLUDES all "
                            f"dividends paid since entry. The earliest factor date "
                            f"{first_ts.date().isoformat()} was NOT used as the anchor: it "
                            f"capitalises pre-entry dividends into today's value"
                            + (
                                f" (that would have marked value x{full_history_ratio:.4f})"
                                if full_history_ratio is not None
                                else ""
                            )
                        )
                    elif anchor_ts > as_of_ts:
                        used_total_return = False
                        warnings_list.append(
                            f"anchor_after_as_of: position-open date "
                            f"{anchor_ts.date().isoformat()} for {symbol} is later than as_of "
                            f"{as_of_iso}; marking at RAW (LOWER BOUND, dividends since entry "
                            f"excluded) instead of anchoring on another date"
                        )
                    else:
                        factor_anchor = _asof_value(factors, anchor_ts)
                        if factor_anchor is None or factor_anchor <= 0:
                            # Anchor precedes the stored factor history.  Measuring from
                            # the earliest row (the old behaviour) would silently swap in
                            # an anchor the operator never supplied -- the same pre-entry
                            # dividend capitalisation, just triggered by a different path.
                            used_total_return = False
                            warnings_list.append(
                                f"anchor_before_factor_history: {symbol} position-open date "
                                f"{anchor_ts.date().isoformat()} has no factor row (history "
                                f"starts {first_ts.date().isoformat()}); marking at RAW "
                                f"(LOWER BOUND, dividends since entry excluded) rather than "
                                f"measuring total return from "
                                f"{first_ts.date().isoformat()}"
                                + (
                                    f" (that would have marked value x{full_history_ratio:.4f})"
                                    if full_history_ratio is not None
                                    else ""
                                )
                            )
                        else:
                            anchor_iso = anchor_ts.date().isoformat()

        if used_total_return:
            value = shares * raw_close * (factor_mark / factor_anchor)
        else:
            factor_mark = None
            factor_anchor = None
            if dividend_mode == "total_return":
                # Factor history / anchor was unusable: do not claim an anchor we never used.
                anchor_iso = None
            else:
                # Raw mode never reads factors for valuation; a caller-supplied anchor
                # is still echoed for audit symmetry.
                supplied_anchor = _to_ts(anchors.get(symbol))
                anchor_iso = (
                    supplied_anchor.date().isoformat() if supplied_anchor is not None else None
                )
            value = shares * raw_close

        per_symbol[symbol] = {
            "shares": shares,
            "raw_close": raw_close,
            "factor_hfq": factor_mark,
            "anchor_date": anchor_iso,
            "value": float(value),
            #: ``shares x raw_close``: the floor reported when no trusted anchor exists.
            "value_raw": float(shares * raw_close),
            #: ``factor_mark / factor_anchor``; exactly 1.0 whenever the symbol was
            #: marked at raw (so 1.0 must NOT be read as "this name pays no dividend" --
            #: see ``dividend_basis``).
            "anchor_inflation_ratio": (
                float(factor_mark / factor_anchor) if used_total_return else 1.0
            ),
            #: DIAGNOSTIC ONLY -- what the rejected earliest-factor-date fallback would
            #: have applied. Never enters `value`/`equity`/`nav`.
            "full_history_ratio": full_history_ratio,
            "dividend_basis": "total_return" if used_total_return else "raw",
            "stale": bool(stale),
            "mark_date": mark_iso,
            "factor_anchor_hfq": factor_anchor,
            "counted_as": "equity",
        }

    # ---------------------------- cash resolution ----------------------------
    cash_source = "explicit"
    resolved_cash: Optional[float] = None
    if cash is not None:
        try:
            resolved_cash = float(cash)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"compute_nav: cash must be numeric, got {cash!r}") from exc
        if resolved_cash < 0:
            raise ValueError(f"compute_nav: negative cash balance: {resolved_cash}")
    else:
        for candidate in CASH_FALLBACK_SYMBOLS:
            entry = per_symbol.get(candidate)
            if entry is not None:
                # Market value, deliberately RAW: a parked-cash vehicle's own
                # distributions are already inside its price, and the amount an
                # operator can actually withdraw is shares x price.
                if entry["raw_close"] is not None:
                    resolved_cash = float(entry["shares"] * entry["raw_close"])
                else:
                    resolved_cash = float(entry["value"])
                cash_source = "bil_fallback"
                entry["counted_as"] = "cash"
                warnings_list.append(
                    f"cash_fallback: no explicit cash given; {candidate} holding "
                    f"({entry['shares']} shares, raw mark) is treated as cash and moved out of "
                    f"equity so it is not counted twice"
                )
                break
        if resolved_cash is None:
            cash_source = "missing"

    if cash_source == "missing":
        message = (
            f"compute_nav: no cash amount resolvable as of {as_of_iso}: cash=None and no "
            f"parked-cash holding among {CASH_FALLBACK_SYMBOLS}. Refusing to mark a portfolio "
            f"with unknown cash (fail_closed=True)."
        )
        if fail_closed:
            raise MissingCashError(message)
        resolved_cash = 0.0
        warnings_list.append(
            "cash_missing: " + message + " Marking cash=0.0 -- NAV is an UNDERSTATEMENT."
        )

    equity = float(
        sum(e["value"] for e in per_symbol.values() if e["counted_as"] == "equity")
    )
    computed_nav = equity + float(resolved_cash)

    if nav_override is not None:
        try:
            nav = float(nav_override)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"compute_nav: nav_override must be numeric, got {nav_override!r}"
            ) from exc
        nav_source = "override"
        warnings_list.append(
            f"nav_override_applied: nav={nav:.6f} overrides the computed {computed_nav:.6f} "
            f"(diff={nav - computed_nav:+.6f}); equity/cash/per_symbol remain computed"
        )
    else:
        nav = computed_nav
        nav_source = "computed"

    return NavResult(
        nav=float(nav),
        equity=equity,
        cash=float(resolved_cash),
        cash_source=cash_source,
        nav_source=nav_source,
        as_of=as_of_iso,
        per_symbol=per_symbol,
        stale_symbols=stale_symbols,
        dividend_alarms=sorted(dividend_alarms, key=lambda a: (a["symbol"], a["ex_date"])),
        warnings=warnings_list,
    )

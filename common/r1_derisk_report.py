"""`AT7` **P3**: the frozen `R1` de-risk composition, and its offline before/after report.

WHY THIS MODULE OWNS THE RULE
=============================
`AT7` (`docs/development_plan.md` §2.24) is a three-pass approval gate: **P1**
report fields, **P2** shadow, **P3** enforcement. `P3` was approved with the
composition semantics *frozen* by the user:

    effective_buy_scale = real_buy_scale x r1.multiplier
    (e.g. 0.50 x 0.25 = 0.125)

applied to **buy increments only**; sells and the cash leg are untouched. That
sentence must exist in exactly ONE place, or the sizing path and the report can
drift apart and the report would no longer describe what was traded. So the
scaling rule lives here, `scripts/run_live_strategy.py` imports it for the live
path, and `build_r1_derisk_report` uses the very same function for the
before/after comparison. No second implementation, no re-derived threshold: the
`R1` ladder (`-2% / -3% / -4%` -> `0.75 / 0.50 / 0.25`) stays owned by
`common.intraday_drawdown_derisk` and is never restated here.

SCOPE -- WHAT IS *NOT* HERE
---------------------------
`R4` (cross-sectional dispersion / market-level de-risk) is explicitly OUT OF SCOPE
for this row: nothing here imports it, evaluates it, or de-risks on it, and
`intraday_buy_scale` must never be stretched to mean a market-level brake. `R1`
is an intraday, portfolio-level *buy-side* brake and that is all it is.

SPARSE-WEIGHT / NaN CONTRACT
----------------------------
`apply_r1_intraday_derisk` is a pure float-series transform. It never
forward-fills, never rounds to lots, never writes 0.0 to de-risk a name by
deletion, and returns the *caller's own object* unchanged when the multiplier
does not de-risk (`>= 1.0`, or unmeasurable `None`). Lot rounding happens later,
in the live path, on the scaled weight -- so a de-risked target still flows
through the existing rounding/validation gates instead of bypassing them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd

__all__ = [
    "apply_r1_intraday_derisk",
    "scale_buy_increment",
    "build_r1_derisk_report",
    "main",
]


def _finite(value: Any) -> Optional[float]:
    """`float` iff finite, else `None` -- a strict-JSON report never carries NaN/Infinity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def scale_buy_increment(
    current_weight: float, target_weight: float, multiplier: float
) -> float:
    """One symbol's post-de-risk target weight (the frozen P3 rule, single symbol).

    Only a BUY increment (`target > current`) is shrunk; a sell, a hold and the
    cash leg return the target verbatim. `multiplier >= 1.0` is a no-op.
    """
    if multiplier is None or multiplier >= 1.0:
        return target_weight
    current = float(current_weight)
    target = float(target_weight)
    if target <= current:
        return target_weight
    return current + (target - current) * float(multiplier)


def apply_r1_intraday_derisk(
    target_series: pd.Series,
    current_series: pd.Series,
    cash_proxy: str,
    r1_multiplier: Optional[float],
) -> pd.Series:
    """Scale BUY increments of `target_series` by `r1_multiplier`; everything else verbatim.

    Multiplicative by construction: called after
    `apply_intraday_distribution_gate` (which already multiplied the increment by
    `intraday_buy_scale`), the surviving increment is
    `(target - current) * real_buy_scale * r1_multiplier`, i.e. exactly the
    approved `real x multiplier` composition. Nothing is rounded here -- lot
    rounding stays in the live path, downstream of this call.

    Returns the input object itself (not a copy) when there is nothing to do, so
    the default/off path is bit-identical to the pre-P3 behaviour.
    """
    if r1_multiplier is None or r1_multiplier >= 1.0:
        return target_series
    scaled = target_series.copy()
    for s in scaled.index:
        if s == cash_proxy:
            continue
        scaled[s] = scale_buy_increment(
            current_series.get(s, 0.0), float(scaled[s]), r1_multiplier
        )
    return scaled


def _strip_suffix(symbol: str) -> str:
    """`511880.SH` -> `511880` (board suffix only; the cash-proxy list has no suffix)."""
    return symbol.split(".")[0]


def _cash_symbol_set(cash_proxy: Any) -> frozenset:
    """Normalize the cash-leg selector: `None` (absent), one symbol, or many."""
    if cash_proxy is None:
        return frozenset()
    if isinstance(cash_proxy, str):
        return frozenset({cash_proxy})
    return frozenset(str(s) for s in cash_proxy)


def build_r1_derisk_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: Any,
    run_id: Any,
    strategy_id: Any,
    strategy_version: Any,
    base_version: Any,
    config_hash: Any,
    r1: Optional[Mapping[str, Any]],
    real_buy_scale: Any,
    portfolio_value: Any,
    rounder: Callable[[str, float], float],
    effective_buy_scale: Any = None,
    cash_proxy: Any = None,
) -> Dict[str, Any]:
    """Pure, deterministic "what would P3 have done to this ticket?" comparison.

    Input rows are the PRE-ROUNDING weights a dry/archived run produces
    (`symbol`, `price`, `target_weight`, `current_weight`). `rounder(symbol,
    raw_shares) -> shares` MUST be the live path's own rounding callable (or a
    direct wrapper of it) -- lot rules are never re-implemented here.

    `qty_*` / `notional_*` describe the DELIVERED target position, not the trade
    delta: a buy row's `target_weight_after` is the de-risked weight and
    `delta_notional` is the exposure the de-risk removes. A sell row's before and
    after are identical by construction (sells are out of scope) and contribute a
    zero delta, which is the point -- it proves the fence numerically.

    Unknown inputs never become plausible numbers: an unusable `price` /
    `portfolio_value` yields `qty = 0` and `0.0` notional for that row, and every
    non-finite value is written as `None` (strict-JSON: `json.dumps(..., allow_nan=False)`).

    Deterministic: rows are visited in input order, no clock is read, no dict
    ordering is incidental, and every float is rounded to a fixed number of
    decimals before it is emitted -- identical inputs give identical bytes
    (`json.dumps(report, allow_nan=False, sort_keys=True)` twice).
    """
    pv = _finite(portfolio_value) or 0.0
    cash_symbols = _cash_symbol_set(cash_proxy)
    real_scale = _finite(real_buy_scale)
    r1_block = {
        "multiplier": _finite((r1 or {}).get("multiplier")),
        "status": (r1 or {}).get("status"),
        "drawdown": _finite((r1 or {}).get("drawdown")),
    }
    if effective_buy_scale is None:
        if real_scale is not None and r1_block["multiplier"] is not None:
            effective_buy_scale = real_scale * r1_block["multiplier"]
        else:
            effective_buy_scale = real_scale
    effective_scale = _finite(effective_buy_scale)
    multiplier = r1_block["multiplier"] if r1_block["multiplier"] is not None else 1.0

    symbols: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol"))
        if symbol in cash_symbols:
            # The cash leg is out of scope for R1 and its "quantity" is not
            # shares, so it is not compared here at all (never silently 0.0).
            continue
        price = _finite(row.get("price")) or 0.0
        target_before = _finite(row.get("target_weight"))
        current = _finite(row.get("current_weight")) or 0.0
        if target_before is None:
            continue
        target_after = scale_buy_increment(current, target_before, multiplier)

        def _qty(weight: float) -> int:
            if pv <= 0 or price <= 0 or weight <= 0:
                return 0
            raw = float(weight) * pv / price
            return int(rounder(symbol, raw))

        qty_before = _qty(target_before)
        qty_after = _qty(target_after)
        notional_before = round(qty_before * price, 2)
        notional_after = round(qty_after * price, 2)
        symbols.append(
            {
                "symbol": symbol,
                "target_weight_before": round(target_before, 6),
                "target_weight_after": round(target_after, 6),
                "qty_before": qty_before,
                "qty_after": qty_after,
                "delta_qty": qty_after - qty_before,
                "notional_before": notional_before,
                "notional_after": notional_after,
                "delta_notional": round(notional_after - notional_before, 2),
            }
        )

    total_before = round(sum(s["notional_before"] for s in symbols), 2)
    total_delta = round(sum(s["delta_notional"] for s in symbols), 2)
    # Derived, not re-summed: both identities the contract promises hold exactly
    # (`total_notional_delta == sum(delta_notional)` and
    # `total_notional_after == total_notional_before + total_notional_delta`) in
    # whole cents, with no float-tie ambiguity between the two sums.
    total_after = round(total_before + total_delta, 2)

    return {
        "as_of": None if as_of is None else str(as_of),
        "run_id": None if run_id is None else str(run_id),
        "strategy_id": None if strategy_id is None else str(strategy_id),
        "strategy_version": None if strategy_version is None else str(strategy_version),
        "base_version": None if base_version is None else str(base_version),
        "config_hash": None if config_hash is None else str(config_hash),
        "r1": r1_block,
        "real_buy_scale": real_scale,
        "effective_buy_scale": effective_scale,
        "symbols": symbols,
        "total_notional_before": total_before,
        "total_notional_after": total_after,
        "total_notional_delta": total_delta,
    }


# --------------------------------------------------------------------------- #
# Thin operator entry point: compare an ALREADY-ARCHIVED ticket (no network, no
# account read). Lot sizes come from the ticket itself
# (`detail_reason.lot_rounding.lot_size`, written by the live path), so this
# entry point reuses the live rounding instead of re-deriving board lots.
# --------------------------------------------------------------------------- #
def _ticket_rows(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("orders", "rows", "ticket"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("ticket JSON must be a list of rows or carry an 'orders'/'rows' list")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline R1 (AT7 P3) before/after order-quantity report from an archived ticket."
    )
    parser.add_argument("--ticket", required=True, help="Archived Stage-2 ticket JSON (pre-enforcement)")
    parser.add_argument("--r1-multiplier", type=float, required=True, help="R1 multiplier (0.25/0.50/0.75/1.0)")
    parser.add_argument("--r1-status", default=None, help="R1 status string (for the report header)")
    parser.add_argument("--r1-drawdown", type=float, default=None, help="Measured intraday drawdown (fraction)")
    parser.add_argument("--real-buy-scale", type=float, default=None, help="intraday_buy_scale actually used")
    parser.add_argument("--effective-buy-scale", type=float, default=None, help="default: real x multiplier")
    parser.add_argument("--portfolio-value", type=float, default=None, help="default: from the ticket")
    parser.add_argument("--default-lot-size", type=int, default=100, help="fallback lot when a row has none recorded")
    parser.add_argument(
        "--cash-proxy",
        default=None,
        help=(
            "Cash-leg symbol(s) to exclude, comma-separated. Default: auto-detect via "
            "common.allocation_backtester.CASH_PROXY_SYMBOLS (the same list the engine uses)."
        ),
    )
    parser.add_argument("--output", default=None, help="Write the report here (default: stdout)")
    args = parser.parse_args(argv)

    with open(args.ticket, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = _ticket_rows(payload)
    meta = payload if isinstance(payload, Mapping) else {}

    def _meta(*keys: str) -> Any:
        for key in keys:
            if key in meta:
                return meta[key]
        return None

    pv = args.portfolio_value
    if pv is None:
        pv = _meta("portfolio_value", "nav")
    lots: Dict[str, int] = {}
    for row in rows:
        lot = ((row.get("detail_reason") or {}).get("lot_rounding") or {}).get("lot_size")
        try:
            lots[str(row.get("symbol"))] = max(1, int(lot))
        except (TypeError, ValueError):
            # Fallback, not a crash: this is an operator reporting tool. But an
            # assumed lot is SILENTLY wrong for HK board lots (500/1000) and for
            # US names (1), so say it out loud per affected symbol.
            assumed_lot = max(1, int(args.default_lot_size))
            lots[str(row.get("symbol"))] = assumed_lot
            print(
                f"[R1 REPORT] warning: {row.get('symbol')} carries no "
                f"detail_reason.lot_rounding.lot_size; assuming lot size "
                f"{assumed_lot} -- quantities for this symbol may be wrong.",
                file=sys.stderr,
            )

    def rounder(symbol: str, raw_shares: float) -> int:
        lot = lots.get(symbol, max(1, int(args.default_lot_size)))
        return int(math.floor(raw_shares / lot) * lot)

    if args.cash_proxy:
        cash_proxy: Any = [s.strip() for s in args.cash_proxy.split(",") if s.strip()]
    else:
        # Reuse the engine's own cash-proxy list instead of hard-coding symbols.
        from common.allocation_backtester import CASH_PROXY_SYMBOLS  # noqa: PLC0415

        cash_proxy = [
            str(row.get("symbol"))
            for row in rows
            if _strip_suffix(str(row.get("symbol"))).upper() in CASH_PROXY_SYMBOLS
        ]

    report = build_r1_derisk_report(
        rows,
        as_of=_meta("as_of", "as_of_date"),
        run_id=_meta("run_id"),
        strategy_id=_meta("strategy_id", "strategy_key", "strategy"),
        strategy_version=_meta("strategy_version"),
        base_version=_meta("base_version"),
        config_hash=_meta("config_hash"),
        r1={
            "multiplier": args.r1_multiplier,
            "status": args.r1_status,
            "drawdown": args.r1_drawdown,
        },
        real_buy_scale=args.real_buy_scale,
        effective_buy_scale=args.effective_buy_scale,
        portfolio_value=pv,
        rounder=rounder,
        cash_proxy=cash_proxy,
    )
    text = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover -- operator entry point
    sys.exit(main())

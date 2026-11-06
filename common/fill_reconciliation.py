"""Fill-receipt <-> cash-ledger **reconciliation** (`LV4`), read-only by construction.

Implements the frozen contract ``docs/fill_reconciliation_schema.md`` (``fill_receipt/v1`` /
``cash_movement/v1``, frozen 2026-11-05); the doc is the authority, this module only consumes it.

* **Integer money only** (§2.1, §6.2).  Shares are ``int``, money is ``int`` 分 (``*_cents``),
  prices are ``int`` ticks of 1e-4 元 (``*_ticks``).  Conversion happens once at the boundary and
  every closure comparison is pure ``int`` arithmetic.  ``notional = qty * ticks / 100`` must
  divide exactly; a remainder is reported as ``price_rounding`` (§6.3), never silently rounded.
* **Two orthogonal axes** (§5.1): ``quantity_outcome`` (what traded, incl. ``None`` = "evidence
  insufficient, no quantity verdict") and ``recon_status``.  ``unreconciled`` never degrades into
  a ``filled``/``unfilled`` default (§5.2); P8 demotes the status **preserving** the quantity read.
* **Per-term residuals** (§6.2): one ``difference_cents`` per identity-(I) currency, per
  identity-(II) ticket and per candidate — never one collapsed global scalar.  ``None`` means
  "not verifiable", which is *not* zero (真空闭合不算闭合, §6.4 A2).
* **No adjudication** (§9.4): ``adjudicated`` is always ``False``, ``selected_candidate`` always
  ``None``; the result never asserts that a ticket did or did not fill.
* **Late arrivals** (§7.3) are carried separately for the next round and never folded in.
* Tolerance is exactly 0 分 (§6.2); the only permitted non-residual note is ``price_rounding``
  with ``|note| <= 1`` 分 per fill (§6.3).

# DISCLOSED AMBIGUITY (§4.1): the doc derives the ticket key with ``row_ordinal:04d`` and then
# calls ``row_ordinal`` "0-based 从 1 计行序" — "0-based" and "counted from 1" contradict.  Used
# the plain 0-based index, the only reading that keeps the key stable for an immutable archive.
#
# DISCLOSED AMBIGUITY (§5.2 vs §5.3): §5.2's "六值定义" table lists ``unreconciled`` among the six
# values while §5.1 puts it on the *other* axis and gives ``quantity_outcome`` the domain
# filled/partial/unfilled/rejected/cancelled/null.  Followed §5.1 + P8: ``unreconciled`` lives
# only in ``recon_status``.
#
# DISCLOSED AMBIGUITY (§5.3): P3-P7 do not cover ``Σfilled > ordered`` (over-fill) and §5.2's
# reason-code set is closed, so no honest code exists.  Reported as ``quantity_outcome=None`` +
# ``unreconciled`` + an explicit note; no invented reason code is emitted.
#
# DISCLOSED AMBIGUITY (§6.1, §8.1): identity (I) needs an observed closing balance *and* an
# opening one.  A ledger without supplied opening cash yields ``difference_cents=None`` /
# ``classification="not_verifiable"`` rather than a fake ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = ["CASH_PROXY_SYMBOLS", "CandidateScenario", "DifferenceTerm", "LateArrival",
           "ReconcileError", "ReconcileResult", "TicketReconciliation", "reconcile_fills"]

PRICE_TICKS_PER_YUAN = 10_000
CASH_PROXY_SYMBOLS = frozenset({"BIL", "511880", "511880.SH", "GC001", "511990", "511990.SH"})

# §5.2 closed reason-code set — do not extend without a §11 revision of the frozen doc.
R_NO_RECEIPT, R_MISSING = "NO_FILL_RECEIPT", "MISSING_REQUIRED_FIELD"
R_NO_LEDGER, R_CASH_NOT_MOVED = "NO_LEDGER", "CASH_NOT_MOVED"
R_CASH_RESIDUAL, R_AMBIGUOUS = "CASH_RESIDUAL", "AMBIGUOUS_TICKET_KEY"
R_DIRECTION, R_LATE = "DIRECTION_MISMATCH", "LATE_ARRIVAL"
REASON_ORDER = (R_MISSING, R_AMBIGUOUS, R_DIRECTION, R_NO_RECEIPT, R_LATE, R_NO_LEDGER,
                R_CASH_NOT_MOVED, R_CASH_RESIDUAL)

ZERO, ROUNDING, GENUINE, UNVERIFIABLE = ("zero", "price_rounding", "genuine_discrepancy",
                                         "not_verifiable")
ZERO_OR = lambda v: UNVERIFIABLE if v is None else (ZERO if v == 0 else GENUINE)  # noqa: E731

_FILL_REQUIRED = ("fill_id", "run_id", "symbol", "side", "ordered_quantity", "filled_quantity",
                  "fill_price_ticks", "currency", "fill_ts", "trade_date", "status")
_MOVEMENT_REQUIRED = ("cash_movement_id", "run_id", "currency", "amount_cents",
                      "balance_after_cents", "movement_ts", "value_date", "source")
_LEDGER_SOURCES = frozenset({"trade_settlement", "cash_sweep", "fee"})


class ReconcileError(ValueError):
    """Genuinely malformed input (e.g. non-integer 分); ordinary gaps never raise (§8.3)."""


@dataclass(frozen=True)
class DifferenceTerm:
    """One closure residual: identity ``I`` (per currency), ``II`` (per ticket) or ``candidate``."""

    identity: str
    key: str
    difference_cents: Optional[int]
    classification: str = ZERO
    currency: Optional[str] = None
    rounding_note_cents: int = 0
    detail: str = ""

    @property
    def is_residual(self) -> bool:
        return self.difference_cents not in (None, 0)


@dataclass(frozen=True)
class LateArrival:
    """A fill past the round's ``as_of``; carried to the next round, never folded in (§7.3)."""

    fill_id: str
    ticket_id: Optional[str]
    symbol: str
    trade_date: str
    fill_ts: str
    carry_age_rounds: int = 1
    reason: str = R_LATE


@dataclass(frozen=True)
class CandidateScenario:
    """A symmetrically listed, **unranked** fill combination (§9.4).  Never selected."""

    candidate_id: str
    assumed_filled_ticket_ids: Tuple[str, ...]
    implied_cash_impact_cents: int
    expected_closing_cash_cents: Optional[int]
    difference_cents: Optional[int]
    recon_status: str = "unreconciled"
    reason_codes: Tuple[str, ...] = ()
    detail: str = "reported, not adjudicated"


@dataclass(frozen=True)
class TicketReconciliation:
    """One derived ticket: both verdict axes, its own residual term, its evidence trail."""

    ticket_id: str
    row_ordinal: int
    symbol: str
    action: str
    side: Optional[str]
    ordered_quantity: Optional[int]
    filled_quantity: Optional[int]
    claimed_cash_impact_cents: Optional[int]
    observed_cash_impact_cents: Optional[int]
    quantity_outcome: Optional[str]
    recon_status: str
    outcome: str
    reason_codes: Tuple[str, ...] = ()
    missing_fields: Tuple[str, ...] = ()
    fill_ids: Tuple[str, ...] = ()
    ambiguous_ticket_key: bool = False
    difference_term: Optional[DifferenceTerm] = None

    @property
    def difference_cents(self) -> Optional[int]:
        """This ticket's identity-(II) residual — a per-ticket term, never a global one."""
        return None if self.difference_term is None else self.difference_term.difference_cents

    @property
    def adjudicated(self) -> bool:
        return False


@dataclass(frozen=True)
class ReconcileResult:
    """Read-only reconciliation report; asserting that a fill happened is impossible.

    ``frozen=True`` is load-bearing, not stylistic: §9.4 says the candidate set may only be
    collapsed by *new evidence*.  ``init=False`` alone would hide ``adjudicated`` from
    ``__init__`` while leaving ``result.adjudicated = True`` legal, i.e. the non-adjudication
    guarantee would be a naming convention.  Freezing makes the write raise
    ``FrozenInstanceError``.
    """

    as_of: Optional[str]
    currency: str
    opening_cash_cents: Dict[str, int]
    closing_cash_cents: Dict[str, Optional[int]]
    tickets: Tuple[TicketReconciliation, ...]
    currency_terms: Tuple[DifferenceTerm, ...]
    ticket_terms: Tuple[DifferenceTerm, ...]
    candidate_terms: Tuple[DifferenceTerm, ...]
    candidates: Tuple[CandidateScenario, ...]
    late_arrivals: Tuple[LateArrival, ...] = ()
    unattributed_fill_ids: Tuple[str, ...] = ()
    excluded_movement_ids: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    adjudicated: bool = field(default=False, init=False)      # never settable (§9.4)
    selected_candidate: None = field(default=None, init=False)

    @property
    def ledger_present(self) -> bool:
        return bool(self.currency_terms) and all(
            t.classification != UNVERIFIABLE for t in self.currency_terms)

    @property
    def residuals(self) -> Dict[str, Optional[int]]:
        """Every term keyed ``identity:key`` — the doc's per-term residual surface (§6.2)."""
        return {f"{t.identity}:{t.key}": t.difference_cents
                for t in self.currency_terms + self.ticket_terms + self.candidate_terms}

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in vars(self).items()}
        # `init=False` fields are never written to the instance `__dict__`, so `vars()` alone
        # would silently DROP the two fields that carry the §9.4 guarantee from the serialized
        # report (§9.5 prints both).  Absent is not "false".
        d["adjudicated"] = self.adjudicated
        d["selected_candidate"] = self.selected_candidate
        for k in ("tickets", "currency_terms", "ticket_terms", "candidate_terms", "candidates",
                  "late_arrivals"):
            d[k] = [vars(x) for x in d[k]]
        for k in ("unattributed_fill_ids", "excluded_movement_ids", "notes"):
            d[k] = list(d[k])
        return d


# --- boundary conversion: the only place a str/float may appear (§2.1, §8.3) -----------------
def _records(obj: Any, what: str) -> List[Dict[str, Any]]:
    if obj is None:
        return []
    if hasattr(obj, "columns") and hasattr(obj, "to_dict"):  # pandas.DataFrame
        return [dict(r) for r in obj.to_dict("records")]
    if isinstance(obj, Mapping):
        return [dict(obj)]
    if isinstance(obj, (str, bytes)):
        raise ReconcileError(f"{what}: expected records, got {type(obj).__name__}")
    try:
        rows = list(obj)
    except TypeError as exc:
        raise ReconcileError(f"{what}: not an iterable of records ({exc})") from exc
    out = []
    for i, row in enumerate(rows):
        if isinstance(row, Mapping):
            out.append(dict(row))
        elif hasattr(row, "__dict__"):
            out.append(dict(vars(row)))
        else:
            raise ReconcileError(f"{what}[{i}]: expected a mapping, got {type(row).__name__}")
    return out


def _text(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and value != value):
        return None
    return str(value).strip() or None


def _cents(value: Any, field_name: str, owner: str) -> Optional[int]:
    """Integer 分 or ``None``; non-integer money is malformed and raises (§2.1)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ReconcileError(f"{owner}.{field_name}: bool is not integer 分")
    if isinstance(value, int):
        return value
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ReconcileError(f"{owner}.{field_name}: not integer 分 ({value!r})") from exc
    if dec.is_nan() or dec.is_infinite():
        return None
    if dec != dec.to_integral_value():
        raise ReconcileError(f"{owner}.{field_name}: non-integer 分 ({value!r})")
    return int(dec)


def _yuan_to_cents(value: Any, field_name: str, owner: str) -> Optional[int]:
    """Ticket prices are 元 — a *derived* claim, never closure authority (§6.3)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ReconcileError(f"{owner}.{field_name}: not a 元 amount ({value!r})") from exc
    return None if dec.is_nan() or dec.is_infinite() else int((dec * 100).to_integral_value())


def _qty(value: Any) -> Optional[int]:
    """Shares must be integral; fractional shares become a missing field, not an exception."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if dec.is_nan() or dec.is_infinite() or dec != dec.to_integral_value():
        return None
    return int(dec)


def _ts(value: Any) -> Tuple[Optional[datetime], Optional[str]]:
    """``(aware datetime, error)``; a bare timestamp is a format error (§7.2)."""
    s = _text(value)
    if s is None:
        return None, "missing"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, "unparseable"
    return (None, "offset required") if dt.tzinfo is None else (dt, None)


def _as_of(value: Any) -> Tuple[Optional[str], Optional[datetime]]:
    s = _text(value)
    if s is None:
        return None, None
    dt, _ = _ts(s)
    if dt is not None:
        return dt.date().isoformat(), dt
    try:
        return date.fromisoformat(s[:10]).isoformat(), None
    except ValueError:
        return None, None


def _side(action: Optional[str]) -> Optional[str]:
    a = (action or "").upper()
    return "BUY" if a in ("BUY", "SWEEP_CASH") else "SELL" if a in ("SELL", "RELEASE_CASH") else None


def reconcile_fills(tickets: Any, fills: Any, cash_movements: Any, opening_cash: Any, *,
                    as_of: Any = None, currency: str = "CNY") -> ReconcileResult:
    """Reconcile one round's tickets ↔ fill receipts ↔ cash ledger, per the frozen doc.

    ``tickets`` / ``fills`` / ``cash_movements`` accept a ``DataFrame``, one mapping, a list of
    mappings, or ``None``.  ``opening_cash`` is integer 分 for ``currency``, or a
    ``{currency: 分}`` mapping.  Empty inputs, missing optional fields and unattributable rows are
    *reported*; only malformed money raises (see :class:`ReconcileError`).
    """
    notes: List[str] = []
    ticket_rows = _records(tickets, "tickets")
    fill_rows = _records(fills, "fills")
    movement_rows = _records(cash_movements, "cash_movements")

    as_of_date, as_of_dt = _as_of(as_of)
    if as_of_date is None and ticket_rows:
        as_of_date, as_of_dt = _as_of(ticket_rows[0].get("as_of")
                                      or ticket_rows[0].get("execution_date"))
    if as_of_date is None:
        notes.append("AS_OF_MISSING: no cut-off; late-arrival filter disabled (§7.1).")

    opening: Dict[str, int] = {}
    if isinstance(opening_cash, Mapping):
        for cur, val in opening_cash.items():
            cents = _cents(val, "opening_cash", str(cur))
            if cents is not None:
                opening[str(cur)] = cents
    else:
        cents = _cents(opening_cash, "opening_cash", "opening_cash")
        if cents is not None:
            opening[currency] = cents

    # §4.1 derived key; a collision makes *both* rows unreconciled (no "take the first").
    keyed: List[Tuple[str, Dict[str, Any], int]] = []
    for ordinal, row in enumerate(ticket_rows):
        date_part = (_text(row.get("as_of")) or _text(row.get("execution_date"))
                     or as_of_date or "")
        keyed.append((f"{date_part}:{ordinal:04d}:{_text(row.get('symbol')) or ''}:"
                      f"{_text(row.get('action')) or ''}", row, ordinal))
    key_counts: Dict[str, int] = {}
    for key, _, _ in keyed:
        key_counts[key] = key_counts.get(key, 0) + 1
    broker_index: Dict[str, List[str]] = {}
    for key, row, _ in keyed:
        if _text(row.get("broker_order_id")):
            broker_index.setdefault(_text(row["broker_order_id"]), []).append(key)

    # --- receipts: validate, resolve the ticket, split late arrivals (§7.3) -------------------
    by_ticket: Dict[str, List[Dict[str, Any]]] = {}
    receipt_count: Dict[str, int] = {}
    late: List[LateArrival] = []
    unattributed: List[str] = []
    for idx, row in enumerate(fill_rows):
        fid = _text(row.get("fill_id")) or f"<row {idx}>"
        missing = [f for f in _FILL_REQUIRED
                   if (_qty(row.get(f)) if f in ("ordered_quantity", "filled_quantity",
                                                 "fill_price_ticks") else _text(row.get(f))) is None]
        fill_dt, ts_err = _ts(row.get("fill_ts"))
        if ts_err:
            missing.append("fill_ts")
        if _text(row.get("total_fees_cents")) is None and (
                _text(row.get("commission_cents")) is None
                and _text(row.get("other_fees_cents")) is None):
            missing.append("total_fees_cents")   # §8.2: fees may come as components only
        row["_missing"], row["_fill_id"] = tuple(sorted(set(missing))), fid

        ticket_id = _text(row.get("ticket_id"))
        if not ticket_id:
            matches = broker_index.get(_text(row.get("broker_order_id")) or "", [])
            if len(matches) > 1:
                unattributed.append(fid)
                notes.append(f"AMBIGUOUS_TICKET_KEY: broker_order_id "
                             f"{row.get('broker_order_id')!r} maps to {len(matches)} tickets; "
                             f"fill {fid} not attributed (§4.1).")
                continue
            ticket_id = matches[0] if matches else None
        if not ticket_id or ticket_id not in key_counts:
            unattributed.append(fid)
            continue
        receipt_count[ticket_id] = receipt_count.get(ticket_id, 0) + 1  # P1 counts late ones

        trade_date = _text(row.get("trade_date")) or ""
        if ((as_of_date and trade_date and trade_date > as_of_date)
                or (as_of_dt and fill_dt and fill_dt > as_of_dt)):
            late.append(LateArrival(
                fill_id=fid, ticket_id=ticket_id, symbol=_text(row.get("symbol")) or "",
                trade_date=trade_date, fill_ts=_text(row.get("fill_ts")) or "",
                carry_age_rounds=max(1, _qty(row.get("carry_age_rounds")) or 1)))
            continue
        by_ticket.setdefault(ticket_id, []).append(row)

    # --- ledger: validate, direction, zero-amount, cut-off (§3.1, §7.1) ----------------------
    movements: List[Dict[str, Any]] = []
    excluded: List[str] = []
    for idx, row in enumerate(movement_rows):
        mid = _text(row.get("cash_movement_id")) or f"<row {idx}>"
        amount = _cents(row.get("amount_cents"), "amount_cents", mid)
        balance = _cents(row.get("balance_after_cents"), "balance_after_cents", mid)
        mv_dt, ts_err = _ts(row.get("movement_ts"))
        missing = [f for f in _MOVEMENT_REQUIRED if row.get(f) is None]
        if amount is None:
            missing.append("amount_cents")
        if balance is None:
            missing.append("balance_after_cents")
        if ts_err:
            missing.append("movement_ts")
        if (_text(row.get("source")) or "") in _LEDGER_SOURCES and not _text(row.get("ticket_id")):
            missing.append("ticket_id")
        if missing:
            excluded.append(f"{mid}:{R_MISSING}({','.join(sorted(set(missing)))})")
            continue
        if amount == 0:                                   # §3.1: not a legal ledger row
            excluded.append(f"{mid}:ZERO_AMOUNT")
            continue
        direction = (_text(row.get("direction")) or "").upper()
        if (direction == "IN" and amount < 0) or (direction == "OUT" and amount > 0):
            excluded.append(f"{mid}:{R_DIRECTION}")
            continue
        if (as_of_dt and mv_dt and mv_dt > as_of_dt) or (
                as_of_date and not as_of_dt and str(row.get("movement_ts"))[:10] > as_of_date):
            excluded.append(f"{mid}:AFTER_AS_OF")
            continue
        row.update(_mid=mid, _amount=amount, _balance=balance, _dt=mv_dt)
        movements.append(row)

    # --- identity (I): per-currency account closure (§6.1) -----------------------------------
    currencies = sorted(set(opening) | {currency}
                        | {_text(m.get("currency")) or currency for m in movements})
    closing: Dict[str, Optional[int]] = {}
    ledger_ok: Dict[str, bool] = {}
    currency_terms: List[DifferenceTerm] = []
    for cur in currencies:
        book = sorted([m for m in movements if (_text(m.get("currency")) or currency) == cur],
                      key=lambda m: (m["_dt"].timestamp() if m["_dt"] else 0.0, m["_mid"]))
        if not book:                                     # vacuum closure is not closure (§6.4 A2)
            closing[cur], ledger_ok[cur] = None, False
            currency_terms.append(DifferenceTerm("I", cur, None, UNVERIFIABLE, cur,
                                                 detail=R_NO_LEDGER))
            continue
        chain_ok = all(book[i]["_balance"] == book[i - 1]["_balance"] + book[i]["_amount"]
                       for i in range(1, len(book)))     # §6.4 A7
        if not chain_ok:
            notes.append(f"{cur}: balance_after_cents chain breaks (§6.4 A7) -> CASH_RESIDUAL.")
        observed = book[-1]["_balance"]
        closing[cur], ledger_ok[cur] = observed, True
        if cur not in opening:
            currency_terms.append(DifferenceTerm(
                "I", cur, None, UNVERIFIABLE, cur,
                detail="opening_cash unavailable; residual unknowable"))
            continue
        residual = observed - (opening[cur] + sum(m["_amount"] for m in book))
        currency_terms.append(DifferenceTerm(
            "I", cur, residual, ZERO if residual == 0 else GENUINE, cur,
            detail="balance chain break" if not chain_ok else ""))

    # --- per-ticket verdict (P0-P8) and identity (II) ---------------------------------------
    ticket_records: List[TicketReconciliation] = []
    ticket_terms: List[DifferenceTerm] = []
    for key, row, ordinal in keyed:
        symbol, action = _text(row.get("symbol")) or "", _text(row.get("action")) or ""
        side = _side(action)
        if key_counts[key] > 1:                          # P0: key ambiguity beats everything
            term = DifferenceTerm("II", key, None, UNVERIFIABLE, detail=R_AMBIGUOUS)
            ticket_terms.append(term)
            ticket_records.append(TicketReconciliation(
                key, ordinal, symbol, action, side, None, None, None, None, None, "unreconciled",
                "unreconciled", (R_AMBIGUOUS,), ambiguous_ticket_key=True, difference_term=term))
            continue

        codes: List[str] = []
        ordered = _qty(row.get("shares"))
        if ordered is None:
            ordered = _qty(row.get("delta_shares"))
        if ordered is None:
            cur_sh, tgt_sh = _qty(row.get("current_shares")), _qty(row.get("target_shares"))
            ordered = None if (cur_sh is None or tgt_sh is None) else tgt_sh - cur_sh
        ordered = abs(ordered) if ordered is not None else None

        missing = [f for f in ("symbol", "action") if row.get(f) is None]
        if side is None and action:
            missing.append("side")
        if ordered is None:
            missing.append("ordered_quantity")
        receipts = by_ticket.get(key, [])
        extra_missing = sorted({f for r in receipts for f in r["_missing"]})
        usable = [r for r in receipts if not r["_missing"]]
        if missing or extra_missing:
            codes.append(R_MISSING)                      # P0
        if not receipt_count.get(key):
            codes.append(R_NO_RECEIPT)                   # P1 — NOT "unfilled"
        if any(a.ticket_id == key for a in late):
            codes.append(R_LATE)                         # P2

        filled = sum(_qty(r.get("filled_quantity")) or 0 for r in usable)
        statuses = [(_text(r.get("status")) or "").upper() for r in usable]
        quantity: Optional[str] = None                   # None = no quantity verdict (§5.2)
        if usable:
            if statuses and all(s == "REJECTED" for s in statuses):
                quantity = "rejected"                                        # P3
            elif ordered is not None and filled > ordered:
                notes.append(f"{key}: Σfilled={filled} > ordered={ordered} is outside P3-P7 "
                             "(§5.3); no quantity verdict issued.")
            elif ordered is not None and filled == 0 and "CANCELLED" in statuses:
                quantity = "cancelled"                                       # P4
            elif ordered is not None and filled == 0:
                quantity = "unfilled"                                        # P5
            elif ordered is not None and filled < ordered:
                quantity = "partial"                                         # P6
            elif ordered is not None and filled == ordered:
                quantity = "filled"                                          # P7

        claim = _cents(row.get("cash_impact_cents"), "cash_impact_cents", key)
        if claim is None:
            tv = _yuan_to_cents(row.get("trade_value"), "trade_value", key)
            if tv is not None:
                # `trade_value` is signed as a *trade* for equity rows (BUY -> +), so the
                # account-side cash impact is its negation.  The cash-proxy leg is the
                # exception: `run_live_strategy.py:3174` writes it as the cash delta itself
                # (`-Σ equity trade_value`), so its cash impact keeps the stored sign --
                # exactly the doc's §9.2 mapping (-10,836.00 => -1,083,600) without which
                # §9.4's C2 (+2,167,200) cannot be reproduced.
                claim = tv if symbol in CASH_PROXY_SYMBOLS else -tv
        fill_currencies = {(_text(r.get("currency")) or currency) for r in receipts}
        if len(fill_currencies) > 1:                     # §2.2: currencies never sum
            notes.append(f"{key}: receipts span currencies {sorted(fill_currencies)}; the ticket "
                         "cannot be closed as one term (§2.2).")
        cur = (_text(row.get("currency")) or (next(iter(fill_currencies))
                                              if len(fill_currencies) == 1 else currency))
        booked = [m for m in movements if _text(m.get("ticket_id")) == key
                  and (_text(m.get("currency")) or currency) == cur]
        observed = (sum(m["_amount"] for m in booked) if booked
                    else (0 if ledger_ok.get(cur, False) else None))
        expected, rounding_hit = 0, False
        for r in usable:
            q, p = _qty(r.get("filled_quantity")) or 0, _qty(r.get("fill_price_ticks")) or 0
            derived, remainder = divmod(q * p, 100)      # 1/100 分 -> 分
            rounding_hit = rounding_hit or bool(remainder)
            given = _cents(r.get("notional_cents"), "notional_cents", r["_fill_id"])
            fees = _cents(r.get("total_fees_cents"), "total_fees_cents", r["_fill_id"])
            if fees is None:
                fees = (_cents(r.get("commission_cents"), "commission_cents", r["_fill_id"]) or 0)
                fees += (_cents(r.get("other_fees_cents"), "other_fees_cents",
                                r["_fill_id"]) or 0)
            sign = 1 if (_text(r.get("side")) or side or "").upper() == "BUY" else -1
            expected += (-sign * (given if given is not None else derived)) - fees  # §2.3

        residual = None if (observed is None or quantity is None) else observed - expected
        rounding = bool(rounding_hit and residual not in (None, 0) and abs(residual) <= 1)
        term = DifferenceTerm(
            "II", key, residual,
            UNVERIFIABLE if residual is None else ZERO if residual == 0 else
            ROUNDING if rounding else GENUINE,
            cur, residual if rounding else 0,
            "" if residual is not None else
            "no quantity read; §6.1 (II) covers only tickets that have one")
        ticket_terms.append(term)

        status = "reconciled"                            # P8 demotes, never overwrites (§5.3)
        if quantity is None:
            status = "unreconciled"
        if not ledger_ok.get(cur, False):
            codes.append(R_NO_LEDGER)
        if term.classification == GENUINE:
            codes.append(R_CASH_RESIDUAL)
        # §5.2 / §6.4 A6: "a ticket that should have moved cash, but the ledger shows none".
        # The cash is legitimately absent where the evidence says nothing traded (P3 rejected /
        # P4 cancelled / P5 unfilled -- A5 states a rejected order produces no settlement row at
        # all) or where the fill is carried to the next round (P2 / §7.3).  Flagging those would
        # assert a discrepancy no observation supports.
        if (claim and not observed and quantity in (None, "filled", "partial")
                and R_LATE not in codes):
            codes.append(R_CASH_NOT_MOVED)                # §6.4 A6 / §9.5
        if codes:
            status = "unreconciled"                       # any P0-P2/P8 trigger is loud
        ticket_records.append(TicketReconciliation(
            key, ordinal, symbol, action, side, ordered, filled if usable else None, claim,
            observed, quantity, status,
            "unreconciled" if status == "unreconciled" else (quantity or "unreconciled"),
            tuple(c for c in REASON_ORDER if c in set(codes)),
            tuple(extra_missing or missing), tuple(r["_fill_id"] for r in receipts),
            False, term))

    # --- candidate set: symmetric, unranked, never selected (§9.4) --------------------------
    legs = [t for t in ticket_records if t.claimed_cash_impact_cents not in (None, 0)]
    observed_closing = closing.get(currency)
    if observed_closing is None and currency in opening:
        # DISCLOSED AMBIGUITY (§9.4/§9.6): the doc's candidate arithmetic and its "unattributable
        # cash move" example both need an *independent* observed closing balance (there taken from
        # ``nav_breakdown.json::cash``), which this frozen signature does not accept.  With no
        # ledger, ``opening_cash`` is the only observation available, so it stands in here — every
        # candidate stays ``unreconciled``/``NO_LEDGER``, so no closure is ever claimed.
        observed_closing = opening[currency]
        notes.append(f"CANDIDATE_CLOSING_FROM_OPENING: no ledger for {currency}; candidate deltas "
                     "use opening_cash as the closing observation (§9.4 uses nav cash instead).")
    if len(legs) > 8:
        subsets = [()] + [(i,) for i in range(len(legs))] + [tuple(range(len(legs)))]
        notes.append(f"CANDIDATE_SET_CAPPED: {len(legs)} legs > 8; enumerated none/singletons/"
                     "all instead of the full power set (§9.4).")
    else:
        subsets = sorted((tuple(i for i in range(len(legs)) if mask >> i & 1)
                          for mask in range(1 << len(legs))), key=lambda s: (len(s), s))

    candidates: List[CandidateScenario] = []
    candidate_terms: List[DifferenceTerm] = []
    for n, subset in enumerate(subsets, start=1):
        cid = f"C{n}"
        implied = sum(legs[i].claimed_cash_impact_cents or 0 for i in subset)
        expected = None if observed_closing is None else observed_closing + implied
        diff = None if expected is None else observed_closing - expected
        codes_c = ((R_NO_LEDGER,) if observed_closing is None or not diff
                   else (R_CASH_NOT_MOVED,))   # a 0 from "nothing happened" is not evidence (A2)
        candidates.append(CandidateScenario(
            cid, tuple(legs[i].ticket_id for i in subset), implied, expected, diff,
            reason_codes=codes_c))
        candidate_terms.append(DifferenceTerm("candidate", cid, diff, ZERO_OR(diff), currency))

    return ReconcileResult(
        as_of=as_of_date, currency=currency, opening_cash_cents=dict(opening),
        closing_cash_cents=dict(closing), tickets=tuple(ticket_records),
        currency_terms=tuple(currency_terms), ticket_terms=tuple(ticket_terms),
        candidate_terms=tuple(candidate_terms), candidates=tuple(candidates),
        late_arrivals=tuple(late), unattributed_fill_ids=tuple(unattributed),
        excluded_movement_ids=tuple(excluded), notes=tuple(notes))

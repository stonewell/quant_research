"""Offline contract tests for `common/fill_reconciliation.py` (`LV4`, frozen 2026-11-05).

The authority is the frozen doc `docs/fill_reconciliation_schema.md`; the module only consumes
it.  Every case below is pure-function: plain dicts in, dataclasses out -- no network, no cache,
no subprocess, no archived run directory.  Fixtures are the doc's own §9 shape (T1 000157.SZ
BUY 1800 @ 6.0200 = 1,083,600 分, T2 BIL SWEEP_CASH -10,836.00).

THE FIVE DISCLOSED AMBIGUITIES (module header) AND WHETHER THE CHOSEN READING DEFENDS THE DOC
=============================================================================================
A1. §4.1 `row_ordinal` -- the doc writes `row_ordinal:04d` and then calls it "0-based 从 1 计".
    Module uses the plain 0-based enumerate index.  **Defensible**: "0-based" is the only reading
    consistent with `:04d` and with an immutable, re-derivable archive key; the "从 1 计" clause
    contradicts the formula it decorates (see `test_ambiguous_ticket_key_*` for the consequence:
    two rows can never collide on this key, so the doc's §4.1 constraint 1 cannot fire).
A2. §5.2 vs §5.1 -- `unreconciled` listed among the "六值" while §5.1 puts it on the other axis.
    Module keeps `unreconciled` only in `recon_status`.  **Defensible**: §5.1 fixes both domains
    explicitly and P8 says the quantity verdict is *preserved*, which is impossible if
    `unreconciled` is a `quantity_outcome` value (`test_p8_demotes_partial_but_keeps_quantity_read`).
A3. §5.3 over-fill (`Σfilled > ordered`) has no reason code in the closed §5.2 set.  Module
    reports `quantity_outcome=None` + `unreconciled` + a note.  **Defensible**: inventing a code
    would violate §5.2's closure and §11; claiming `filled` would contradict P7's literal test.
A4. §6.1/§8.1 identity (I) needs an opening balance.  Module reports `None` /
    `not_verifiable` instead of a fake 0.  **Defensible**: §6.2 says a residual is per-identity
    and §6.4 A2 says vacuum closure is not closure (`test_missing_opening_cash_is_not_verifiable`).
A5. §9.4/§9.6 candidate arithmetic needs an *independent* observed closing cash, which the frozen
    signature does not accept.  Module substitutes `opening_cash` and says so in a note.  This is
    the **weakest** of the five: it reproduces the doc's §9.4 table exactly only because the
    measured ledger did not move.  It never claims closure (every candidate stays `unreconciled`
    with `NO_LEDGER`/`CASH_NOT_MOVED`), so it is defensible *as long as* the
    `CANDIDATE_CLOSING_FROM_OPENING` note is honoured by consumers -- asserted in
    `test_doc_case_9_4_candidate_table` rather than treated as a closure.

Bug fixes that these tests forced (see also the module comments, each cites its clause):
  * `ReconcileResult` was a mutable dataclass, so `result.adjudicated = True` *succeeded* --
    §9.4's "no adjudication" was a naming convention.  It is now `frozen=True`.
  * `trade_value` was negated for every row, but `run_live_strategy.py:3174` writes the cash-proxy
    leg as the cash delta itself, so doc §9.2's -10,836.00 -> -1,083,600 and §9.4's C2
    (+2,167,200) were both sign-flipped.
  * `CASH_NOT_MOVED` fired on tickets whose receipts prove nothing traded (P3 rejected / P4
    cancelled / P5 unfilled) and on fills carried by §7.3, asserting a discrepancy no observation
    supports (and contradicting A5, which says a rejected order produces no settlement row).
"""

import os
import sys
from dataclasses import FrozenInstanceError

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.fill_reconciliation import (  # noqa: E402
    CASH_PROXY_SYMBOLS,
    CandidateScenario,
    DifferenceTerm,
    LateArrival,
    ReconcileError,
    ReconcileResult,
    TicketReconciliation,
    reconcile_fills,
)

AS_OF = "2026-11-02"
RUN = AS_OF
OPENING = 4_780_006                      # §9.1: 47,800.06 元
KEY_T1 = f"{AS_OF}:0000:000157.SZ:BUY"   # §4.1 derived key, 0-based ordinal (ambiguity A1)
KEY_T2 = f"{AS_OF}:0001:BIL:SWEEP_CASH"

# FrozenInstanceError is what a frozen dataclass raises; AttributeError keeps the assertion
# honest if the guarantee is ever re-implemented with a property / __setattr__ instead.
FROZEN_ERRORS = (FrozenInstanceError, AttributeError)


def ticket(symbol="000157.SZ", action="BUY", shares=1800, trade_value=10836.00, **extra):
    """One `live_trading_ticket.csv` row (only the columns the reconciler reads)."""
    row = {"as_of": AS_OF, "symbol": symbol, "action": action, "shares": shares,
           "current_shares": 0, "target_shares": shares, "trade_value": trade_value}
    row.update(extra)
    return row


TICKET_000157 = ticket()
TICKET_BIL = ticket(symbol="BIL", action="SWEEP_CASH", shares=0, trade_value=-10836.00)


def fill(**over):
    """One `fill_receipt/v1` row: full fill of T1 at 6.0200 元 (60,200 ticks)."""
    row = {"fill_id": "F1", "run_id": RUN, "ticket_id": KEY_T1, "symbol": "000157.SZ",
           "side": "BUY", "ordered_quantity": 1800, "filled_quantity": 1800,
           "fill_price_ticks": 60200, "currency": "CNY",
           "fill_ts": "2026-11-02T10:14:33.512+08:00", "trade_date": AS_OF,
           "status": "FILLED", "commission_cents": 0, "other_fees_cents": 0,
           "total_fees_cents": 0}
    row.update(over)
    return row


def movement(amount_cents, balance_after_cents, *, movement_id="M1", ticket_id=KEY_T1,
             source="trade_settlement", **over):
    """One `cash_movement/v1` row; `direction` derived from the sign, as §3.1 requires."""
    numeric = isinstance(amount_cents, (int, float)) and not isinstance(amount_cents, bool)
    row = {"cash_movement_id": movement_id, "run_id": RUN, "currency": "CNY",
           "amount_cents": amount_cents, "balance_after_cents": balance_after_cents,
           "movement_ts": "2026-11-02T11:00:00+08:00", "value_date": "2026-11-03",
           "source": source, "direction": "IN" if (not numeric or amount_cents >= 0) else "OUT"}
    if ticket_id is not None:
        row["ticket_id"] = ticket_id
    row.update(over)
    return row


def dividend(amount=100, balance=OPENING + 100):
    """A ticket-less row that only serves to make the ledger *exist* (never closure evidence)."""
    return movement(amount, balance, movement_id="M-DIV", ticket_id=None, source="dividend",
                    value_date=AS_OF, movement_ts="2026-11-02T12:00:00+08:00")


# --- 1 ---------------------------------------------------------------------------------------
def test_no_fills_is_no_receipt_not_unfilled():
    """§6.4 A1 / P1: zero receipts is `unreconciled` + NO_FILL_RECEIPT, never `unfilled`."""
    result = reconcile_fills([TICKET_000157], [], [], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.ticket_id == KEY_T1
    assert t.ordered_quantity == 1800
    assert t.claimed_cash_impact_cents == -1_083_600      # 1800 x 60,200 ticks / 100
    assert t.quantity_outcome is None                     # no quantity verdict at all
    assert t.recon_status == "unreconciled"
    assert t.outcome == "unreconciled"
    assert set(t.reason_codes) == {"NO_FILL_RECEIPT", "NO_LEDGER", "CASH_NOT_MOVED"}
    assert result.closing_cash_cents == {"CNY": None}
    assert result.ledger_present is False                 # §6.4 A2: vacuum is not closure


# --- 2 ---------------------------------------------------------------------------------------
def test_full_fill_closes_every_identity_to_the_cent():
    """§6.4 A3: filled + identity (I) and (II) residuals are exactly 0 分."""
    result = reconcile_fills([TICKET_000157], [fill()],
                             [movement(-1_083_600, OPENING - 1_083_600)], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.quantity_outcome == "filled"
    assert t.difference_cents == 0                        # closure first: this is the contract
    assert result.currency_terms[0].difference_cents == 0
    assert t.recon_status == "reconciled"
    assert result.closing_cash_cents["CNY"] == OPENING - 1_083_600
    assert result.ledger_present is True
    assert t.reason_codes == ()


# --- 3 ---------------------------------------------------------------------------------------
def test_partial_fill_debits_only_the_filled_shares():
    """§6.4 A4: 900 of 1800 is `partial`, the aggregate settles exactly, remainder stays explicit."""
    result = reconcile_fills([TICKET_000157],
                             [fill(filled_quantity=900, status="PARTIAL_FILL")],
                             [movement(-541_800, OPENING - 541_800)], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.quantity_outcome == "partial"
    assert t.recon_status == "reconciled"
    assert (t.ordered_quantity, t.filled_quantity) == (1800, 900)
    assert t.difference_cents == 0                        # 900 x 60,200 / 100 == 541,800
    assert result.closing_cash_cents["CNY"] == OPENING - 541_800
    assert result.currency_terms[0].difference_cents == 0
# --- 4 ---------------------------------------------------------------------------------------
def test_rejected_ticket_leaves_cash_untouched():
    """§6.4 A5: `rejected`, no settlement row, and no invented cash discrepancy."""
    result = reconcile_fills([TICKET_000157],
                             [fill(filled_quantity=0, fill_price_ticks=0, status="REJECTED")],
                             [dividend()], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.quantity_outcome == "rejected"
    assert t.recon_status == "reconciled"
    assert t.fill_ids == ("F1",)                          # the rejection itself is on record
    assert t.difference_cents == 0
    assert t.reason_codes == ()
    # Cash is unchanged by the ticket: only the unrelated dividend moved it.
    assert result.closing_cash_cents["CNY"] == OPENING + 100
    assert result.currency_terms[0].difference_cents == 0


# --- 5 ---------------------------------------------------------------------------------------
def test_ledger_unmoved_reports_per_ticket_differences_and_never_adjudicates():
    """§6.2/§9.4: one difference per ticket, no global scalar, no way to select a candidate."""
    sell = ticket(symbol="600519.SH", action="SELL", shares=100, trade_value=-5000.00)
    key_sell = f"{AS_OF}:0001:600519.SH:SELL"
    result = reconcile_fills(
        [TICKET_000157, sell],
        [fill(), fill(fill_id="F2", ticket_id=key_sell, symbol="600519.SH", side="SELL",
                      ordered_quantity=100, filled_quantity=100, fill_price_ticks=500000)],
        [dividend()], OPENING, as_of=AS_OF)
    by_key = {t.ticket_id: t for t in result.tickets}
    buy_term, sell_term = by_key[KEY_T1], by_key[key_sell]
    assert isinstance(buy_term, TicketReconciliation)

    # Both receipts say the shares traded; the ledger moved for neither.  Each delta is its own
    # identity-(II) term, computed from *that ticket's* claim -- not one collapsed number.
    assert buy_term.quantity_outcome == "filled"
    assert buy_term.difference_cents == 1_083_600
    assert sell_term.quantity_outcome == "filled"
    assert sell_term.difference_cents == -500_000
    assert buy_term.difference_cents != sell_term.difference_cents
    assert result.residuals[f"II:{KEY_T1}"] == 1_083_600
    assert result.residuals[f"II:{key_sell}"] == -500_000
    assert not hasattr(result, "difference_cents")          # no global scalar to hide behind
    assert {"CASH_NOT_MOVED", "CASH_RESIDUAL"} <= set(buy_term.reason_codes)
    assert buy_term.recon_status == sell_term.recon_status == "unreconciled"

    # §9.4: the candidate set may only be collapsed by new evidence -- structurally, not by
    # convention.  `init=False` alone would leave both writes legal.
    assert result.adjudicated is False
    assert result.selected_candidate is None
    assert all(c.recon_status == "unreconciled" for c in result.candidates)
    with pytest.raises(FROZEN_ERRORS):
        result.adjudicated = True
    with pytest.raises(FROZEN_ERRORS):
        result.selected_candidate = "C1"
    with pytest.raises(FROZEN_ERRORS):
        buy_term.recon_status = "reconciled"


# --- 6 ---------------------------------------------------------------------------------------
def test_late_arrival_is_a_carry_and_does_not_touch_the_closed_round():
    """§7.3 / P2: `trade_date > as_of` is carried, never folded into this round's figures."""
    result = reconcile_fills([TICKET_000157],
                             [fill(trade_date="2026-11-03")], [dividend()], OPENING, as_of=AS_OF)
    (late,) = result.late_arrivals
    assert isinstance(late, LateArrival)
    assert (late.fill_id, late.ticket_id) == ("F1", KEY_T1)
    assert late.trade_date == "2026-11-03"
    assert late.carry_age_rounds == 1
    assert late.reason == "LATE_ARRIVAL"

    (t,) = result.tickets
    assert "LATE_ARRIVAL" in t.reason_codes
    assert t.quantity_outcome is None                     # the fill did not count this round
    assert t.recon_status == "unreconciled"
    # No cash was claimed for the carry -- "CASH_NOT_MOVED" here would assert a discrepancy the
    # carry rule itself creates (the money settles next round).
    assert "CASH_NOT_MOVED" not in t.reason_codes
    assert result.closing_cash_cents["CNY"] == OPENING + 100
    assert result.currency_terms[0].difference_cents == 0
    # Stronger: the candidate arithmetic of this round is bit-identical to the same round with
    # the late receipt removed -- a carry alters nothing that was already closed.
    without = reconcile_fills([TICKET_000157], [], [dividend()], OPENING, as_of=AS_OF)
    assert ([c.implied_cash_impact_cents for c in result.candidates]
            == [c.implied_cash_impact_cents for c in without.candidates])
    assert ([c.difference_cents for c in result.candidates]
            == [c.difference_cents for c in without.candidates])


# --- 7 ---------------------------------------------------------------------------------------
def test_ambiguous_ticket_key_is_reported_never_silently_resolved():
    """§4.1: an ambiguous reverse lookup is loud.  Not "take the first match"."""
    first = ticket(broker_order_id="B-1")
    second = ticket(symbol="600519.SH", trade_value=5000.00, broker_order_id="B-1")
    result = reconcile_fills([first, second],
                             [fill(ticket_id=None, broker_order_id="B-1")],
                             [dividend()], OPENING, as_of=AS_OF)
    assert result.unattributed_fill_ids == ("F1",)
    assert any(n.startswith("AMBIGUOUS_TICKET_KEY") for n in result.notes)
    assert len(result.tickets) == 2
    assert all(t.recon_status == "unreconciled" for t in result.tickets)
    assert all("NO_FILL_RECEIPT" in t.reason_codes for t in result.tickets)
    # Neither ticket may acquire a quantity verdict by being the first row.
    assert all(t.quantity_outcome is None for t in result.tickets)

    # The literal duplicate (as_of, row_ordinal, symbol, action) tuple cannot occur: the ordinal
    # *is* the row index, so the key is unique by construction.  Two identical rows therefore stay
    # two independently reported tickets -- no overwrite, no silent drop -- and the reachable
    # AMBIGUOUS_TICKET_KEY path is the broker_order_id reverse lookup above.  The doc's §4.1
    # constraint 1 ("同 symbol 同 action 同 as_of 即派生冲突") contradicts its own key formula.
    dup = reconcile_fills([TICKET_000157, dict(TICKET_000157)], [], [], OPENING, as_of=AS_OF)
    assert [t.row_ordinal for t in dup.tickets] == [0, 1]
    assert len({t.ticket_id for t in dup.tickets}) == 2
    assert all(t.recon_status == "unreconciled" for t in dup.tickets)
    assert [t.ambiguous_ticket_key for t in dup.tickets] == [False, False]
# --- 8 ---------------------------------------------------------------------------------------
def test_integer_cents_close_exactly_where_floats_drift():
    """§2.1/§6.2: ten 0.29 元 fills must sum to 290 分 exactly -- the 元->分 float path loses one."""
    ticks = 2900                                          # 0.2900 元
    assert int(ticks / 10_000 * 100) == 28                # float round-trip loses a 分
    assert int(0.29 * 100) == 28
    assert ticks % 100 == 0                               # the integer path is exact
    ten_fills = [fill(fill_id=f"P{i}", ordered_quantity=10, filled_quantity=1,
                      fill_price_ticks=ticks, status="PARTIAL_FILL") for i in range(10)]
    result = reconcile_fills([ticket(shares=10)], ten_fills,
                             [movement(-290, OPENING - 290)], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.filled_quantity == 10
    assert t.quantity_outcome == "filled"
    assert t.difference_cents == 0                        # 10 x 29 分, never 10 x 28
    assert t.recon_status == "reconciled"
    assert result.currency_terms[0].difference_cents == 0
    assert result.closing_cash_cents["CNY"] == OPENING - 290


# --- 9 ---------------------------------------------------------------------------------------
def test_p8_demotes_partial_but_keeps_the_quantity_read():
    """§5.3 P8: a residual demotes `recon_status`; `quantity_outcome` stays readable at `partial`."""
    result = reconcile_fills([TICKET_000157],
                             [fill(filled_quantity=900, status="PARTIAL_FILL")],
                             [movement(-1_083_600, OPENING - 1_083_600)], OPENING, as_of=AS_OF)
    (t,) = result.tickets
    assert t.quantity_outcome == "partial"                # NOT overwritten with "unreconciled"
    assert t.recon_status == "unreconciled"
    assert t.outcome == "unreconciled"
    assert t.difference_cents == -541_800                 # 0 observed - (-541,800) expected
    assert "CASH_RESIDUAL" in t.reason_codes


# --- 10 --------------------------------------------------------------------------------------
def test_non_integer_cents_is_malformed_and_raises():
    """§2.1: a fractional 分 is malformed input, not a rounding problem."""
    with pytest.raises(ReconcileError, match="non-integer 分"):
        reconcile_fills([TICKET_000157], [],
                        [movement(12.5, OPENING - 12, movement_id="M-BAD")], OPENING, as_of=AS_OF)
    with pytest.raises(ReconcileError, match="non-integer 分"):
        reconcile_fills([TICKET_000157], [],
                        [movement("1083599.5", OPENING, movement_id="M-STR")], OPENING,
                        as_of=AS_OF)
    with pytest.raises(ReconcileError, match="bool is not integer 分"):
        reconcile_fills([TICKET_000157], [],
                        [movement(True, OPENING, movement_id="M-BOOL")], OPENING, as_of=AS_OF)


# --- 11 --------------------------------------------------------------------------------------
def test_empty_inputs_return_a_non_adjudicated_report():
    """§8.3: absence of data is reported, never raised, and never resolved."""
    result = reconcile_fills([], [], [], None)
    assert isinstance(result, ReconcileResult)
    assert result.adjudicated is False
    assert result.selected_candidate is None
    assert result.tickets == ()
    # An empty round still lists the trivially-empty combination, and never closes it.
    assert all(not c.assumed_filled_ticket_ids for c in result.candidates)
    assert all(c.difference_cents is None for c in result.candidates)
    assert result.late_arrivals == ()
    assert result.as_of is None
    assert any(n.startswith("AS_OF_MISSING") for n in result.notes)
    payload = result.to_dict()
    assert payload["adjudicated"] is False and payload["selected_candidate"] is None
    assert payload["tickets"] == [] and isinstance(payload["opening_cash_cents"], dict)


# --- 12 --------------------------------------------------------------------------------------
def test_doc_case_9_4_candidate_table_is_reproduced_verbatim():
    """§9.4/§10③: C1 +1,083,600, C2 +2,167,200, C3 0 from the two §9.2 ticket rows."""
    result = reconcile_fills([TICKET_000157, TICKET_BIL], [], [], OPENING, as_of=AS_OF)
    assert "BIL" in CASH_PROXY_SYMBOLS
    # Ticket-side amounts are preserved: the equity leg's cash impact is -trade_value, the
    # cash-proxy leg is written *as* the cash delta (run_live_strategy.py:3174).
    assert [t.claimed_cash_impact_cents for t in result.tickets] == [-1_083_600, -1_083_600]

    by_legs = {frozenset(c.assumed_filled_ticket_ids): c for c in result.candidates}
    assert all(isinstance(c, CandidateScenario) for c in result.candidates)
    only_t1 = by_legs[frozenset({KEY_T1})]
    both = by_legs[frozenset({KEY_T1, KEY_T2})]
    none = by_legs[frozenset()]
    assert only_t1.expected_closing_cash_cents == 3_696_406
    assert only_t1.difference_cents == 1_083_600
    assert both.expected_closing_cash_cents == 2_612_806
    assert both.difference_cents == 2_167_200
    assert none.expected_closing_cash_cents == OPENING
    assert none.difference_cents == 0                     # "not falsified" != "confirmed"
    assert {c.recon_status for c in result.candidates} == {"unreconciled"}
    assert result.adjudicated is False
    assert result.selected_candidate is None
    assert all(isinstance(t, DifferenceTerm) for t in result.candidate_terms)
    assert sorted(t.difference_cents for t in result.candidate_terms) == [0, 1_083_600,
                                                                        1_083_600, 2_167_200]
    assert any(n.startswith("CANDIDATE_CLOSING_FROM_OPENING") for n in result.notes)


# --- 13 --------------------------------------------------------------------------------------
def test_missing_opening_cash_is_not_verifiable_not_zero():
    """§6.4 A2 / §6.1: without an opening balance identity (I) has no residual -- and not 0."""
    result = reconcile_fills([TICKET_000157], [fill()],
                             [movement(-1_083_600, OPENING - 1_083_600)], None, as_of=AS_OF)
    (term,) = result.currency_terms
    assert term.classification == "not_verifiable"
    assert term.difference_cents is None
    assert result.ledger_present is False
    assert result.closing_cash_cents["CNY"] == OPENING - 1_083_600
    (t,) = result.tickets
    assert t.quantity_outcome == "filled"
    assert t.recon_status == "reconciled"                 # (II) is still verifiable
    assert t.difference_cents == 0


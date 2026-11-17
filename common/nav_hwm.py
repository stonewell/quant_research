"""Pure NAV / high-water-mark helpers for the live-trading archive.

WHY THIS MODULE EXISTS (the incident it is written against)
==========================================================
The live archive
``202609/runs/chan_four_state_blend/custom_astock_202609_20_new/`` stores, per round,
an ``account_state.json`` (``current_nav`` / ``peak_nav`` / ``drawdown_pct`` /
``nav_history_10d``) next to a ``nav_breakdown.json`` that carries the two legs of the
NAV separately: ``equity`` and ``cash``.

On **2026-11-05** the two files disagreed:

* ``nav_breakdown.json`` (same instant, one round): ``equity = 186218.00``,
  ``cash = 12592.57`` -> NAV **198810.57**;
* ``account_state.json``: ``current_nav = 208830.78`` = ``186218.00`` (that day's
  equity leg) + ``22612.78`` (**the PREVIOUS round's cash leg**, 2026-11-04).

So the account NAV was a *mixed-timebase* sum: today's securities were added to
yesterday's cash, because the cash figure is not read out of the same snapshot as the
equity mark. The error is 208830.78 - 198810.57 = **10020.21**, i.e. 5.0% of NAV.
**2026-11-06** repeats it verbatim: 186476.57 = 173884.00 + 12592.57 (true 199480.90).

Two things then turn a one-day arithmetic slip into a permanent scar:

1. ``peak_nav`` ratchets **up only** (``peak = max(peak, nav)``). The phantom
  208830.78 was accepted as a new high-water mark on 11-05 and never came down.
2. ``drawdown_pct`` is derived from that peak, so every later round reports a deeper
   drawdown than the account really had. The real all-time high is **203049.96**
   (2026-09-29); the archive reports **-5.77%** where the truth is **-3.08%**
   (196789.34 / 203049.96 - 1, 2026-11-16) -- and a 5.77% drawdown is what feeds the
   multi-tier circuit breaker, i.e. the phantom peak trades *real* risk decisions.

The rule this module encodes
============================
A NAV may feed the high-water mark only when **both legs are same-instant**. The order of
evidence is fixed, and it is the *precedence* that matters:

1. **Structured field wins, both ways.** ``cash_as_of != as_of`` (both present) =>
   INELIGIBLE, hard. ``cash_as_of == as_of`` => **ELIGIBLE**, and the decision is made by
   that field *alone*: warning strings found alongside it are recorded as
   ``corroborating_warnings`` and must NOT flip the verdict. (Review finding, MEDIUM: the
   producer appends ``cash_from_archive:`` on ANY archive-sourced cash -- including a
   same-session re-run, where the gate's own ``_archive_cash_is_same_session`` proves the
   legs share one instant. Barring those would silently freeze the HWM on a new-high day,
   so the breaker would UNDER-trip -- over-barring is worse than the bug it fixes.)
2. **Unconditional bars.** ``stale_symbols`` non-empty (at least one mark carried
   forward) and ``nav_source == "form_fallback"`` (a human number, not a snapshot) are
   not timebase claims, so they bar the NAV whatever ``cash_as_of`` says.
3. **Warnings are the fallback, only when the structured field is absent.** With no
   ``cash_as_of``/``as_of`` pair, the free-text evidence is all there is: scan BOTH
   ``nav_warnings`` **and** ``cash_as_of_warning`` for ``cash_from_archive`` /
   ``identical_to_previous_round_residual``, matched by **prefix** (mirroring
   ``scripts/check_live_portfolio_health.py::_cash_from_archive_warning``), never by loose
   substring.

Two corrections this precedence encodes (the earlier guard got both wrong):
``identical_to_previous_round_residual`` is written by the real producer to
``cash_as_of_warning`` (``execution_service._flag_cash_identical_to_previous_round``),
never to ``nav_warnings`` -- so a scan of ``nav_warnings`` alone misses it on real
payloads; and the 2026-11-05 defect has NO warning at all (its ``nav_warnings`` carry only
``provider_key_resolved``), so it is caught by the legs-vs-account comparison in
``scripts/repair_live_nav_hwm.py``, not by any warning scan.

PEAK PROVENANCE -- AND WHAT IT DOES *NOT* FIX (``NV13``, 2026-11-16)
===================================================================
Everything above repairs DATA. Nothing above stops the ENGINE from inheriting a stale
peak: a new round copies the previous round's state block, so ``prior_state.peak_nav``
re-enters the next instance unconditionally. That is not hypothetical -- ``2026-11-16/
142959`` landed *during* the second repair pass and inherited the phantom ``208830.78``
again; it reads ``203049.96`` today only because the source files had been repaired
first. "Today is clean" is therefore a fact about the data, not a guarantee of the
engine.

:func:`peak_provenance` answers the narrower, answerable question: *is the peak we are
about to divide by backed by anything in the run tree?* It returns one of five labels
(:data:`PEAK_PROVENANCE_LEG_VERIFIED` / :data:`PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL` /
:data:`PEAK_PROVENANCE_UNVERIFIED_INHERITED` / :data:`PEAK_PROVENANCE_UNVERIFIABLE` /
:data:`PEAK_PROVENANCE_MISSING`) plus the numbers behind the verdict.

**``missing`` vs. ``unverifiable`` (review finding, HIGH).** These were one label, and
that conflation made the opt-in ``--require-provable-peak`` gate fail OPEN exactly where
it was supposed to fail closed: a peak that IS present but has no leg-verified series to
compare against (non-archive ``--output-dir``, scan failure, empty tree) reported
``missing``, which the gate treats as "not assessable, proceed". ``missing`` now means
*no usable peak at all* -- nothing is being divided by, so there is no unprovable peak --
while ``unverifiable`` means *the peak is real and in use, but this run could not check
it*. The gate refuses both ``unverified_inherited`` and ``unverifiable``; only
``missing`` still passes, because a run with no peak at all cannot be carrying a phantom.

**The boundary, stated honestly.** A label is not a proof. Making an untrustworthy peak
*visible* (and optionally fatal, via the ``--require-provable-peak`` CLI flag) is the
whole deliverable. A peak labelled ``leg_verified`` is one whose value happens to sit
inside the tolerance of a NAV the archive can re-add from two legs -- it is not a signed
record, and the label does not make the number authoritative. The real fix needs the
producer to persist a signed ATH record (value + the round that produced it + a
fingerprint of the leg snapshot it was derived from) and to refuse to carry an unsigned
peak forward; see the NV13 row in ``docs/development_plan.md`` and the operator-facing
report of this change.

**Why a naive rule is wrong.** The true ATH ``203049.96`` (2026-09-29) is NOT present in
the run tree's leg NAVs -- the highest leg NAV there is ``202483.94`` (2026-11-03),
because the ATH predates the tree. So "the peak may not exceed any leg-verified NAV"
would reject a *legitimate* peak by 0.28%, while the phantom exceeds the same maximum by
3.13%. :data:`PEAK_PROVENANCE_TOL` (0.5%) separates the two *today*; it is a reporting
separation device, explicitly not a principle, and it is quoted verbatim in both the
warning and the fail-closed message so a reader can judge it.

Everything here is pure: mappings in, primitives out, no I/O, no module-level mutable
state, no clock. ``scripts/repair_live_nav_hwm.py`` is the only caller that touches disk;
``pipeline/tests/test_live_nav_hwm.py`` pins the truth table offline.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "CASH_FROM_ARCHIVE_MARKER",
    "RESIDUAL_MARKER",
    "HWM_TIME_BASED_WARNING_FIELDS",
    "INELIGIBLE_NAV_WARNINGS",
    "FORM_FALLBACK_NAV_SOURCES",
    "leg_nav",
    "hwm_eligibility",
    "hwm_eligibility_detail",
    "restate_peak",
    "dedupe_adjacent",
    "drawdown",
    "PEAK_PROVENANCE_LEG_VERIFIED",
    "PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL",
    "PEAK_PROVENANCE_UNVERIFIED_INHERITED",
    "PEAK_PROVENANCE_UNVERIFIABLE",
    "PEAK_PROVENANCE_MISSING",
    "PEAK_PROVENANCE_LABELS",
    "PEAK_PROVENANCE_TOL",
    "PEAK_NAV_OPERATOR_ATH_KEY",
    "max_leg_verified_nav",
    "peak_provenance",
]

#: The producer's provenance marker for archive-sourced cash
#: (``execution_service.py``: ``f"cash_from_archive: cash={...} 取自上一轮 summary.json
#: ({origin})"``). Matched as a PREFIX: a warning that merely *mentions* the phrase later
#: in its text is not the producer's claim.
CASH_FROM_ARCHIVE_MARKER = "cash_from_archive"

#: The producer's marker for "this round's cash numerically equals the previous round's
#: residual" (``execution_service.CASH_AS_OF_WARNING_IDENTICAL``). Written to
#: ``cash_as_of_warning`` -- NOT to ``nav_warnings``.
RESIDUAL_MARKER = "identical_to_previous_round_residual"

#: Fields the producer may write a timebase warning into. ``cash_as_of_warning`` is the
#: one that carries the residual marker on real payloads (2026-11-16/124508 is the
#: regression case), so both are scanned.
HWM_TIME_BASED_WARNING_FIELDS: tuple[str, ...] = ("nav_warnings", "cash_as_of_warning")

#: Warning markers that disqualify a NAV when (and only when) no structured
#: ``cash_as_of``/``as_of`` pair exists to outrank them.
#:
#: * ``cash_from_archive`` -- the writer reused an archived cash figure.
#: * ``identical_to_previous_round_residual`` -- the cash leg equals the *previous*
#:   round's leftover, which is the shape of the 2026-11-05 defect (22612.78 belonged
#:   to 11-04).
INELIGIBLE_NAV_WARNINGS: tuple[str, ...] = (
    CASH_FROM_ARCHIVE_MARKER,
    RESIDUAL_MARKER,
)

#: ``nav_source`` values that mean the NAV came from the operator form, not from marks.
FORM_FALLBACK_NAV_SOURCES: tuple[str, ...] = ("form_fallback",)

#: Absolute tolerance used when telling "same NAV" from "different NAV" in reports.
#: One cent, because every snapshot in the archive is quantised to 0.01 RMB.
NAV_ABS_TOL = 0.01

#: ---------------------------------------------------------------------------
#: (NV13, 2026-11-16) PEAK PROVENANCE -- a LABEL, never a gate.
#:
#: Five labels. Exactly TWO of them are accusations, and the fifth is the one that used
#: to be indistinguishable from ``missing``:
#:
#: * ``leg_verified`` -- the reported peak is equal to, or within ``tol`` of, the
#:   highest NAV the archive can re-add from two same-instant legs.
#: * ``attributable_external`` -- the peak is ABOVE every leg-verified NAV by more than
#:   ``tol``, but an operator explicitly declared it (``--peak-nav`` or a recorded ATH).
#:   Attributable is not verified: it is a named human assertion, on the record.
#: * ``unverified_inherited`` -- the peak is above every leg-verified NAV by more than
#:   ``tol`` and NOTHING declares it externally: it came in with the copied
#:   ``prior_state`` block. This is the 2026-11-05 phantom's label.
#: * ``unverifiable`` -- a USABLE peak IS in play, but no leg-verified NAV series was
#:   supplied, so the run can neither verify nor refute it. (Review finding, HIGH: this
#:   used to collapse into ``missing``, which made ``--require-provable-peak`` fail OPEN
#:   in precisely the deployments -- non-archive layout, failed scan, empty tree -- where
#:   it is the only available guard.)
#: * ``missing`` -- no usable peak at all (absent / non-finite / non-positive). Nothing is
#:   being divided by, so there is no unprovable peak to refuse: always silent, never
#:   fatal. Absence of evidence is reported, never read as proof.
#: ---------------------------------------------------------------------------
PEAK_PROVENANCE_LEG_VERIFIED = "leg_verified"
PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL = "attributable_external"
PEAK_PROVENANCE_UNVERIFIED_INHERITED = "unverified_inherited"
PEAK_PROVENANCE_UNVERIFIABLE = "unverifiable"
PEAK_PROVENANCE_MISSING = "missing"

PEAK_PROVENANCE_LABELS: tuple[str, ...] = (
    PEAK_PROVENANCE_LEG_VERIFIED,
    PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL,
    PEAK_PROVENANCE_UNVERIFIED_INHERITED,
    PEAK_PROVENANCE_UNVERIFIABLE,
    PEAK_PROVENANCE_MISSING,
)

#: Relative tolerance for "the peak is backed by a leg-verified NAV" (0.5%).
#:
#: MEASURED, not chosen for comfort: the legitimate ATH 203049.96 sits 0.2798% above the
#: highest leg-verified NAV in the tree (202483.94), while the phantom 208830.78 sits
#: 3.13% above it. Anything between those two numbers separates today's cases; 0.5%
#: leaves the legitimate peak ~1.8x inside the band and the phantom ~6.3x outside it.
#: DISCLOSED LIMITATION: this is a separation device, NOT a principle -- a phantom only
#: 0.4% above the true ATH would pass as ``leg_verified``. Quoted verbatim in the
#: warning and in the fail-closed message so nobody has to reverse-engineer it.
PEAK_PROVENANCE_TOL = 0.005

#: Optional ``account_state`` key an operator (or a future writer) can set to declare
#: that ``peak_nav`` is an EXTERNAL all-time high taken from a round this archive cannot
#: see. No producer writes it today: it exists so the label has a persisted, reviewable
#: form instead of "trust the inherited number". It only ever changes the LABEL.
PEAK_NAV_OPERATOR_ATH_KEY = "peak_nav_operator_ath"



def _finite(value: object) -> Optional[float]:
    """``value`` as a finite float, or ``None`` (``bool``/blank/nan/inf all fail)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def leg_nav(nav_breakdown: Mapping) -> Optional[float]:
    """``equity + cash`` from a ``nav_breakdown`` snapshot; ``None`` when unusable.

    WHY the two legs and not ``nav_effective``: ``nav_effective`` is the number the
    engine *believed* -- on 2026-11-05 it is not even written into this file (the 208830.78
    lives only in ``account_state.json``). The legs are the only pair that can be
    re-added and compared against the account state, which is what makes the
    mixed-timebase defect detectable at all.

    Rejected (``None``) when either leg is missing/non-finite, or when ``cash < 0``:
    a negative cash leg cannot be a real balance -- it is a subtracted-form artifact
    (``cash = portfolio_value - equity``) and adding it back would double-count.
    """
    if not isinstance(nav_breakdown, Mapping):
        return None
    equity = _finite(nav_breakdown.get("equity"))
    cash = _finite(nav_breakdown.get("cash"))
    if equity is None or cash is None:
        return None
    if cash < 0.0:
        return None
    return equity + cash


def _as_str_list(value: Any) -> List[str]:
    """A warning field as a list of non-empty strings (a bare string = one entry)."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = (value,)
    try:
        items = list(value)
    except TypeError:
        return [str(value)]
    return [str(item) for item in items if str(item).strip()]


def _match_markers(nav_breakdown: Mapping) -> List[Dict[str, str]]:
    """Every producer marker present in the two warning fields, with its provenance.

    Prefix matching only, mirroring
    ``scripts/check_live_portfolio_health.py::_cash_from_archive_warning``: the producer
    writes ``"<marker>: <detail>"``, so a warning that merely *mentions* the phrase is not
    the producer's claim and must not be read as one.
    """
    matched: List[Dict[str, str]] = []
    for field in HWM_TIME_BASED_WARNING_FIELDS:
        for warning in _as_str_list(nav_breakdown.get(field)):
            for marker in INELIGIBLE_NAV_WARNINGS:
                if warning.strip().startswith(marker):
                    matched.append({"field": field, "marker": marker, "warning": warning})
                    break
    return matched


def hwm_eligibility_detail(nav_breakdown: Mapping) -> Dict[str, Any]:
    """``hwm_eligibility`` plus the evidence that decided it (same precedence).

    Returns ``{"eligible", "reason", "deciding_evidence", "structured_timebase",
    "corroborating_warnings"}``. The extra fields exist because the verdict alone cannot
    be audited: on a same-instant re-run the snapshot carries ``cash_from_archive`` and
    the NAV is STILL eligible, so "which evidence won" is the only thing that explains
    why. ``corroborating_warnings`` is recorded and never flips the verdict.
    """
    detail: Dict[str, Any] = {
        "eligible": False,
        "reason": "",
        "deciding_evidence": "",
        "structured_timebase": None,
        "corroborating_warnings": [],
    }
    if not isinstance(nav_breakdown, Mapping):
        detail["reason"] = detail["deciding_evidence"] = "nav_breakdown is not a mapping"
        return detail

    matched = _match_markers(nav_breakdown)
    detail["corroborating_warnings"] = matched

    # 1. STRUCTURED TIMEBASE FIRST -- and it wins in both directions.
    as_of = nav_breakdown.get("as_of")
    cash_as_of = nav_breakdown.get("cash_as_of")
    structured_equal: Optional[bool] = None
    if as_of is not None and cash_as_of is not None:
        structured_equal = str(cash_as_of) == str(as_of)
        detail["structured_timebase"] = structured_equal
        detail["deciding_evidence"] = (
            f"structured: cash_as_of={cash_as_of!r} == as_of={as_of!r} "
            "(same-instant legs; warning text cannot override this)"
            if structured_equal
            else f"structured: cash_as_of={cash_as_of!r} != as_of={as_of!r}"
        )
        if not structured_equal:
            detail["reason"] = (
                f"cash_as_of={cash_as_of!r} != as_of={as_of!r}: the cash leg was struck on a "
                "different day than the equity leg (mixed-timebase NAV)"
            )
            return detail
    else:
        detail["deciding_evidence"] = (
            "no structured cash_as_of/as_of pair in this snapshot: the producer's warning "
            "markers are the only timebase evidence available"
        )

    # 2. UNCONDITIONAL BARS -- not timebase claims, so `cash_as_of` cannot outrank them.
    stale_list = _as_str_list(nav_breakdown.get("stale_symbols"))
    if stale_list:
        detail["reason"] = f"stale_symbols non-empty: {stale_list}"
        detail["deciding_evidence"] = "unconditional bar: stale_symbols"
        return detail

    source = nav_breakdown.get("nav_source")
    if source is not None and str(source).strip().lower() in FORM_FALLBACK_NAV_SOURCES:
        detail["reason"] = (
            f"nav_source={source!r}: NAV came from the operator form, not from marks"
        )
        detail["deciding_evidence"] = "unconditional bar: nav_source=form_fallback"
        return detail

    # 3. WARNING MARKERS -- only when there is no structured field to outrank them.
    if structured_equal is None and matched:
        first = matched[0]
        detail["reason"] = (
            f"{first['field']} starts with {first['marker']!r}: {first['warning']} "
            "(no structured cash_as_of/as_of pair, so the warning is the only timebase "
            "evidence)"
        )
        detail["deciding_evidence"] = (
            f"warning marker in {first['field']} (no structured timebase field present)"
        )
        return detail

    detail["eligible"] = True
    return detail


def hwm_eligibility(nav_breakdown: Mapping) -> tuple[bool, str]:
    """May this NAV feed the high-water mark? ``(True, "")`` or ``(False, reason)``.

    Precedence (the rule, in order -- see the module docstring for WHY):

    1. ``cash_as_of`` and ``as_of`` both present:
       * unequal => INELIGIBLE, hard. The file itself says the cash was struck on a
         different day than the equity; no prose can rescue that.
       * equal => **ELIGIBLE, decided by this field alone.** Producer warnings found in
         ``nav_warnings`` / ``cash_as_of_warning`` are corroborating evidence and do NOT
         flip the verdict. ``cash_from_archive`` is appended on ANY archive-sourced cash,
         including a same-session re-run (``_archive_cash_is_same_session``) where the legs
         do share one instant; barring those would freeze the HWM on a new-high day and
         make the breaker UNDER-trip.
    2. ``stale_symbols`` non-empty => INELIGIBLE (a carried-forward mark is not this
       instant's equity). Unconditional.
    3. ``nav_source == "form_fallback"`` => INELIGIBLE (a human number, not a snapshot).
       Unconditional.
    4. Structured pair ABSENT => scan ``nav_warnings`` **and** ``cash_as_of_warning`` for
       ``cash_from_archive`` / ``identical_to_previous_round_residual``, matched by prefix
       (the producer writes ``"<marker>: <detail>"``). Any hit => INELIGIBLE.

    A missing field is not a rejection by itself (older snapshots predate it), which is
    why the JSON scan also compares the leg NAV against ``account_state.current_nav``:
    absence of evidence is reported, never silently treated as proof.

    Use :func:`hwm_eligibility_detail` when the *evidence* is needed, not just the verdict.
    """
    detail = hwm_eligibility_detail(nav_breakdown)
    return bool(detail["eligible"]), str(detail["reason"])


def restate_peak(candidates: Iterable[tuple[str, float, bool]]) -> float:
    """Highest NAV among *eligible* candidates only.

    ``candidates`` is an iterable of ``(label, nav, eligible)``. Ineligible rows are the
    whole point: the phantom 208830.78 of 2026-11-05 must not be able to set the mark,
    or restating the peak would reproduce the incident instead of undoing it.

    A row whose ``nav`` is non-finite is skipped even when flagged eligible (the flag is
    the caller's opinion; a nan cannot be a maximum).

    Raises:
        ValueError: when no usable candidate exists. Returning ``0.0`` instead would
            satisfy the caller's "never below the highest eligible leg NAV" floor for the
            wrong reason and let a peak of zero be written.
    """
    best: Optional[float] = None
    best_label: Optional[str] = None
    seen = 0
    for label, nav, eligible in candidates:
        seen += 1
        if not eligible:
            continue
        value = _finite(nav)
        if value is None:
            continue
        if best is None or value > best:
            best, best_label = value, str(label)
    if best is None:
        raise ValueError(
            f"restate_peak: no eligible NAV among {seen} candidate(s); refusing to invent a peak"
        )
    assert best_label is not None  # for type checkers; best is not None implies a label
    return best


def dedupe_adjacent(series: Sequence[float]) -> list[float]:
    """Drop a value equal to its immediate predecessor.

    The archive repeats a NAV on consecutive rounds (199935.46 twice in
    ``nav_history_10d`` for 10-29 -> 10-30 -> 11-02) because a day with no fill simply
    re-posts the previous mark. Those repeats are not observations: they occupy slots in
    a ten-slot window and push real history out, and they make ``nav_history_10d``
    disagree with the number of distinct NAVs the account actually had.

    Exact equality on the float is intentional -- the snapshots are quantised to 0.01
    and re-posted, so a genuine one-cent move must survive.
    """
    out: list[float] = []
    for raw in series:
        value = _finite(raw)
        if value is None:
            continue
        if out and value == out[-1]:
            continue
        out.append(value)
    return out


def drawdown(nav: float, peak: float) -> float:
    """``nav / peak - 1`` (negative = below the high-water mark), as a fraction.

    Same convention as the engine (``scripts/repair_live_run_snapshots.py`` writes
    ``(nav - peak) / peak``), so a restated value is directly comparable with
    ``drawdown_pct`` in ``account_state.json`` and with the circuit-breaker tiers.

    Raises:
        ValueError: when ``peak`` is not a positive finite number, or ``nav`` is not
            finite. ``inf``/``nan`` written into ``account_state.json`` would silently
            disable the breaker; a loud refusal is the safer failure.
    """
    nav_f = _finite(nav)
    peak_f = _finite(peak)
    if peak_f is None or peak_f <= 0.0:
        raise ValueError(f"drawdown: peak must be a positive finite number, got {peak!r}")
    if nav_f is None:
        raise ValueError(f"drawdown: nav must be a finite number, got {nav!r}")
    return nav_f / peak_f - 1.0


def max_leg_verified_nav(leg_navs: Optional[Iterable[Any]]) -> Optional[float]:
    """Highest usable NAV in a leg-verified series, or ``None`` when there is none.

    "Usable" = finite and strictly positive, matching :func:`leg_nav`'s own refusals: a
    non-positive NAV is a broken row, not a low one, and letting it into a maximum is
    harmless only by accident.
    """
    best: Optional[float] = None
    for raw in leg_navs or ():
        value = _finite(raw)
        if value is None or value <= 0.0:
            continue
        if best is None or value > best:
            best = value
    return best


def peak_provenance(
    peak_nav: Any,
    leg_navs: Optional[Iterable[Any]],
    *,
    tol: float = PEAK_PROVENANCE_TOL,
    declared_external: bool = False,
) -> tuple[str, str]:
    """``(label, detail)`` for the high-water mark we are about to divide by.

    ``label`` is one of :data:`PEAK_PROVENANCE_LABELS`; ``detail`` is the arithmetic
    behind it (the peak, the highest leg-verified NAV, the excess and the tolerance), so
    nothing has to be re-derived by the reader.

    THE RULE, in order:

    1. unusable ``peak_nav`` (absent / non-finite / non-positive) -> ``missing``;
       nothing is being divided by, so there is no unprovable peak;
    2. usable peak but no leg-verified NAV at all -> ``unverifiable``. Still not an
       accusation -- "this run could not check the peak" is a different statement from
       "the peak is wrong" -- but it is NOT ``missing``: a peak that is present and in
       use, and that nothing here can check, is exactly the case the opt-in
       ``--require-provable-peak`` gate must refuse rather than wave through (finding,
       HIGH: the two absences used to share one label, so the flag failed OPEN wherever
       the archive was unreachable);
    3. ``peak <= max_leg * (1 + tol)`` -> ``leg_verified``;
    4. above that band, with ``declared_external`` -> ``attributable_external``;
    5. above that band, undeclared -> ``unverified_inherited``.

    ``declared_external`` is the operator's assertion (``--peak-nav``, or a recorded ATH
    in ``account_state[PEAK_NAV_OPERATOR_ATH_KEY]``). It outranks "above every leg NAV"
    because the true ATH of 2026-09-29 (203049.96) predates the run tree and therefore
    can never appear in its leg series -- exactly the case a purely archive-internal rule
    would misclassify. It is consulted AFTER rule 2, so a declaration cannot lift a peak
    out of ``unverifiable``: with no leg series there is nothing to attribute against, and
    a name on the record is not a measurement.

    Raises:
        ValueError: when ``tol`` is not a finite, non-negative number. A silently
            ignored tolerance would put the label and the printed threshold out of sync
            in the one message an operator is supposed to act on.
    """
    tol_f = _finite(tol)
    if tol_f is None or tol_f < 0.0:
        raise ValueError(f"peak_provenance: tol must be a finite non-negative number, got {tol!r}")

    peak = _finite(peak_nav)
    if peak is None or peak <= 0.0:
        return (
            PEAK_PROVENANCE_MISSING,
            f"no usable peak_nav ({peak_nav!r}): the high-water mark is absent, so there is "
            "nothing to attribute",
        )

    max_leg = max_leg_verified_nav(leg_navs)
    if max_leg is None:
        return (
            PEAK_PROVENANCE_UNVERIFIABLE,
            f"peak {peak:,.2f} is present and in use, but no leg-verified NAV was supplied, "
            "so this run cannot verify OR refute it (unverifiable -- the run could NOT "
            "check the peak; that is not a claim that the peak is wrong)",
        )

    excess = peak / max_leg - 1.0
    if excess <= tol_f:
        return (
            PEAK_PROVENANCE_LEG_VERIFIED,
            f"peak {peak:,.2f} is within {tol_f:.2%} of the highest leg-verified NAV "
            f"{max_leg:,.2f} (excess {excess:+.2%})",
        )

    if declared_external:
        return (
            PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL,
            f"peak {peak:,.2f} exceeds the highest leg-verified NAV {max_leg:,.2f} by "
            f"{excess:.2%} (> tol {tol_f:.2%}), but it was declared externally "
            "(--peak-nav / a recorded operator ATH): attributable, not archive-verified",
        )

    return (
        PEAK_PROVENANCE_UNVERIFIED_INHERITED,
        f"peak {peak:,.2f} exceeds EVERY leg-verified NAV in the run tree (highest "
        f"{max_leg:,.2f}) by {excess:.2%} > tol {tol_f:.2%}, and nothing declares it "
        "externally: it was inherited from prior_state",
    )

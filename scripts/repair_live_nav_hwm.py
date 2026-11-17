#!/usr/bin/env python3
"""Restate the live archive's high-water mark from same-instant NAV legs (dry run by default).

WHY THIS SCRIPT EXISTS -- the 2026-11-05 mixed-timebase incident
===============================================================
Archive root (git-ignored runtime state):
``202609/runs/chan_four_state_blend/custom_astock_202609_20_new/``
laid out as ``<root>/<YYYY-MM-DD>/<HHMMSS>/{account_state.json,nav_breakdown.json,...}``.

``nav_breakdown.json`` stores the NAV as two legs; ``account_state.json`` stores the
single number the engine carried forward. On **2026-11-05** the two disagreed:

* ``nav_breakdown.json``: ``equity = 186218.00``, ``cash = 12592.57`` -> **198810.57**
  (both legs struck at the same instant, that round);
* ``account_state.json``: ``current_nav = 208830.78`` = ``186218.00`` (today's equity)
  + ``22612.78`` (**the 2026-11-04 cash leg**).

The cash leg was carried over from the previous round while the securities were
re-marked, so the account NAV was a *mixed-timebase* sum: +10020.21 (+5.04%) of pure
phantom equity. **2026-11-06** repeats it: 186476.57 = 173884.00 + 12592.57, true
199480.90 -- note the second 11-06 instance (``185016``) is consistent at 199480.90, so
the defect is per-round, not per-day.

Why one bad number became permanent
-----------------------------------
``peak_nav`` ratchets up only (``peak = max(peak, nav)``). 208830.78 was accepted as a
new all-time high on 11-05 and never came back down, so ``drawdown_pct`` -- and with it
the multi-tier circuit breaker -- was computed against a high-water mark that never
existed. The real ATH is **203049.96** (2026-09-29, see
``202609/20260929/account_state.json``). Reported vs true drawdown at 2026-11-16:
**-5.77%** (196789.34 / 208830.78 - 1) vs **-3.08%** (196789.34 / 203049.96 - 1).

What this script does -- and what it refuses to do
==================================================
1. Scans every ``<root>/<date>/<HHMMSS>/`` instance (auxiliary directories such as
   ``204956.pre-repair-...Z`` are reported and skipped, never repaired: they are the
   evidence copies).
2. Per round: ``leg_nav`` + ``hwm_eligibility`` from ``common/nav_hwm.py``, and the
   account state's ``current_nav`` compared against ``leg_nav``. A disagreement larger
   than one cent is ``MIXED_TIMEBASE`` and both numbers plus the delta are reported.
3. Proposes a restored ``peak_nav`` (``--peak`` if given, else the highest *eligible*
   leg NAV), a ``drawdown_pct`` derived from the round's own leg NAV, and a
   ``nav_history_10d`` that is the adjacent-deduped leg-NAV series ending at that round.
4. ``--apply`` rewrites **only** ``peak_nav`` / ``drawdown_pct`` / ``nav_history_10d``
   in ``account_state.json``. ``current_nav`` is never recomputed here (the archive's
   own ``nav_breakdown.json`` already states it, and a restatement that edits the NAV
   would erase the evidence of the defect), and ``nav_breakdown.json`` /
   ``summary.json`` / ``live_trading_ticket.json`` / ``holdings.json`` are never opened
   for writing at all.

Safety rails
============
* Default is a **dry run**; nothing is written without ``--apply``.
* First rewrite of a file leaves ``<name>.orig``; if that exists already (an earlier
  repair, or a pre-existing backup), the pre-repair bytes go to ``<name>.orig.navhwm``.
  An existing backup is never overwritten.
* All writes are atomic (same-directory ``.tmp-navhwm`` + ``os.replace``).
* Idempotent: the proposed content is a pure function of the ``nav_breakdown.json``
  files and the run list, so a second ``--apply`` finds the file already correct and
  writes nothing -- byte-identical, not "equivalent".
* **Concurrent-edit abort (implemented, not just documented):** before the FIRST write,
  ``verify_appraisal_still_holds`` re-reads every target and checks that each field about
  to change still holds the value the appraisal saw (``changes[field]["before"]``). All
  mismatches are collected first, then the run aborts with exit 2 having written **0**
  files -- somebody else edited the archive, and overwriting that edit would destroy the
  only record of what the engine actually read.
* **Append-only ledger (1.2.0).** ``nav_hwm_manifest.<date>.json`` is a day's ledger, and
  a repair is routinely run more than once per day (here: a top-level pass, then a
  ``prior_state`` pass after the nested gap was found). 1.1.0 wrote the file
  last-write-wins, so pass 2 replaced the record of pass 1: the ``.orig`` backups still
  preserved the bytes, but the ledger no longer said what the first pass changed. The
  manifest now carries a top-level ``passes`` array and each run APPENDS its pass
  (``{recorded_at, script_version, file_count, nested_rewritten, files}``) instead of
  replacing the file. The newest pass's fields are still mirrored at the top level, so a
  single-pass reader -- and every manifest written before 1.2.0 -- keeps working
  unchanged. Merging only happens when something was actually rewritten, so an idempotent
  second ``--apply`` still leaves the manifest byte-identical.
* Peak floor and peak band. The **auto-derived** peak must be at least the highest
  HWM-**eligible** leg NAV, so a cross-timebase leg sum can never pull the restored mark
  UP toward the phantom (the floor is about *not proposing something below the best
  evidence*). An explicit ``--peak`` is exempt from that floor -- it is the operator's
  assertion, usually from a round this archive cannot see -- but it is **bounded above**:
  an override more than ``PEAK_PROVENANCE_TOL`` above ``max_leg_nav_all`` gets a loud
  ``[WARN]`` naming both numbers and the tolerance. That upper band is what separates a
  legitimate external ATH (203049.96, 0.28% above the archive's best leg NAV) from a
  phantom (208830.78, 3.13% above it) -- without it, ``--peak 208830.78`` re-injects the
  exact number this tool exists to remove, silently. It is a warning, never a hard abort:
  an operator-declared external ATH is legitimate, it must simply never be silent.

Exit codes: ``0`` clean / nothing to change (or changes applied), ``1`` changes
proposed but not applied, ``2`` error (bad root, no runs, invariant failure, concurrent
edit, or a write loop that aborted partway -- the manifest then records the partial pass
with ``incomplete: true``).

Usage
=====
    python scripts/repair_live_nav_hwm.py --runs-root <root>
    python scripts/repair_live_nav_hwm.py --runs-root <root> --peak 203049.96
    python scripts/repair_live_nav_hwm.py --runs-root <root> --peak 203049.96 --apply
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.nav_hwm import (  # noqa: E402
    NAV_ABS_TOL,
    PEAK_PROVENANCE_TOL,
    dedupe_adjacent,
    drawdown,
    hwm_eligibility_detail,
    leg_nav,
    restate_peak,
)

SCRIPT_VERSION = "1.3.0"

#: 1.1.0 (review pass): `hwm_eligibility` now honours the structured `cash_as_of` pair
#: over warning text (a same-session re-run is eligible), `resolve_peak` floors on
#: ELIGIBLE legs only and exempts an explicit `--peak`, and the documented
#: concurrent-edit abort is now actually implemented (`verify_appraisal_still_holds`).

#: 1.2.0: the manifest became an append-only ledger (`passes` array). The 1.1.0 shape
#: overwrote the day's earlier pass whenever a second repair ran -- and a second repair
#: is the normal case here (the first pass only restated the top level, the second
#: restated the nested `prior_state` copies), so the 2026-11-16 ledger lost the record of
#: what pass 1 changed while its `.orig` backups survived. `merge_manifest` now appends,
#: and a legacy single-pass manifest is upgraded in place (its top level becomes pass 1).

#: 1.3.0 (review pass): three defects in the WRITE path, all of which could hide a
#: partial repair:
#:   1. an explicit `--peak` had no UPPER bound, so `--peak 208830.78` re-injected the
#:      phantom verbatim and silently (only a below-floor override warned). It now emits a
#:      loud `peak_warnings` entry above `max_leg_nav_all * (1 + PEAK_PROVENANCE_TOL)`;
#:   2. `apply_repairs` was not transactional on a generic `OSError` -- earlier files were
#:      rewritten, the failing file kept an orphaned `.orig`, a `.tmp-navhwm` could be left
#:      behind and NO manifest was written, so the partial pass was invisible. It now
#:      reports exactly what was rewritten and what failed, unlinks its own temp file, and
#:      still writes a manifest pass marked `incomplete: true`;
#:   3. the byte-level write scope was asserted, not checked: a serializer that touched an
#:      untouched field while `fields_changed` reported only the repaired ones would pass
#:      silently. A post-serialization diff now aborts that file with a named error;
#: plus `merge_manifest` now refuses a malformed `passes` entry (`ManifestError`) instead
#: of dropping it.

#: Instance directories are ``<HHMMSS>``; anything else (``*.pre-repair-*`` backups,
#: ``holdings.json``-only aux dirs) is evidence and is reported, never repaired.
INSTANCE_DIR_RE = re.compile(r"^\d{6}$")

#: The only three fields ``--apply`` may touch -- at the TOP LEVEL...
REPAIRED_FIELDS: Tuple[str, ...] = ("peak_nav", "drawdown_pct", "nav_history_10d")

#: ...and inside the nested ``prior_state`` copy (same three, never anything else).
#:
#: WHY THE NESTED BLOCK MATTERS (production gap found after the 2026-11-16 repair):
#: every ``account_state.json`` also carries ``prior_state`` -- a copy of the previous
#: round's state. The first pass repaired only the top level, so the copies still hold the
#: phantom peak 208830.78, while the snapshots they were copied FROM now say 203049.96.
#: ``inspect_portfolio_drawdown`` swaps to ``prior_state`` when
#: ``as_of_date == state["as_of_date"]`` -- i.e. on exactly the SAME-DAY RE-RUN the
#: operator is about to take -- and would then read the phantom peak and a
#: duplicate-laden history. Same three fields, no counters, no tier, and
#: ``prior_state.current_nav`` is NEVER touched (it is the record of what that round read).
NESTED_CONTAINER = "prior_state"
NESTED_PREFIX = NESTED_CONTAINER + "."
NESTED_REPAIRED_FIELDS: Tuple[str, ...] = tuple(
    NESTED_PREFIX + field for field in REPAIRED_FIELDS
)

#: Files/fields the repair may never write. Printed, written into the manifest, and
#: asserted by the offline test.
NEVER_TOUCHED: Tuple[str, ...] = (
    "account_state.current_nav",
    "account_state.prior_state.current_nav",
    "account_state.prior_state.tier1_consecutive_bars",
    "account_state.prior_state.tier3_consecutive_bars",
    "account_state.prior_state.circuit_breaker_tier",
    "nav_breakdown.json",
    "summary.json",
    "live_trading_ticket.json",
    "holdings.json",
)

NAV_HISTORY_LEN = 10

#: ``drawdown_pct`` is compared with this absolute tolerance (1e-9 of a fraction, i.e.
#: 0.0000001%). Anything below it is float noise from a different summation order, not
#: a different conclusion -- and rewriting on noise would break idempotency.
DRAWDOWN_ABS_TOL = 1e-9


# ---------------------------------------------------------------------------
# bytes / atomics
# ---------------------------------------------------------------------------
def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def json_bytes(obj: Any) -> bytes:
    """Exactly the archive's writer style: ``indent=2``, UTF-8, no trailing newline.

    Verified byte-for-byte against the real ``account_state.json`` files, so a rewrite
    changes the three repaired fields and nothing else -- not the indentation, not the
    key order, not the encoded Chinese in ``lifecycle_states``.
    """
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Same-directory ``.tmp-navhwm`` + ``os.replace``, with NO temp left behind on failure.

    (review finding, MEDIUM) The temp file used to survive a failed write -- a disk-full or
    permission error on the *replace* left a ``*.tmp-navhwm`` beside the archive, which
    looks like a half-written repair to every later reader. The temp is unlinked here, so
    a failure leaves the directory exactly as it found it (plus whatever the caller wrote
    before this call, which `apply_repairs` reports).
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-navhwm")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def orig_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + ".orig")


def alt_orig_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + ".orig.navhwm")


def ensure_backup(path: Path, pre_repair: bytes) -> Dict[str, Any]:
    """Write ``<name>.orig`` (or ``<name>.orig.navhwm`` when ``.orig`` exists).

    An existing backup is never overwritten: it is the only byte-exact copy of the
    original, and after the first apply the live file no longer contains it.
    """
    info: Dict[str, Any] = {"created": False, "path": None, "md5": None}
    orig = orig_path(path)
    if not orig.exists():
        write_bytes_atomic(orig, pre_repair)
        info.update(created=True, path=str(orig), md5=md5_bytes(orig.read_bytes()))
        return info
    info.update(path=str(orig), md5=md5_bytes(orig.read_bytes()))
    alt = alt_orig_path(path)
    if not alt.exists():
        write_bytes_atomic(alt, pre_repair)
        info.update(created=True, path=str(alt), md5=md5_bytes(alt.read_bytes()))
        return info
    info["note"] = (
        "both .orig and .orig.navhwm already exist; left untouched (never overwrite a "
        "backup -- it may be the only byte-exact copy of the original)"
    )
    return info


# ---------------------------------------------------------------------------
# discovery + per-round facts
# ---------------------------------------------------------------------------
class RunFact:
    """One round on disk: what the archive claims vs what its own legs say."""

    __slots__ = (
        "date",
        "instance",
        "path",
        "breakdown",
        "state",
        "leg_nav",
        "eligible",
        "ineligible_reason",
        "eligibility",
        "current_nav",
        "peak_nav",
        "drawdown_pct",
        "nav_history",
        "prior_state",
        "prior_as_of",
        "prior_peak_nav",
        "prior_drawdown_pct",
        "prior_nav_history",
    )

    def __init__(self, date: str, instance: str, path: Path,
                 breakdown: Optional[Mapping[str, Any]],
                 state: Optional[Mapping[str, Any]]) -> None:
        self.date = date
        self.instance = instance
        self.path = path
        self.breakdown = breakdown
        self.state = state
        self.leg_nav = leg_nav(breakdown) if breakdown is not None else None
        if breakdown is None:
            self.eligible, self.ineligible_reason = False, "no nav_breakdown.json"
            self.eligibility = {
                "eligible": False,
                "reason": self.ineligible_reason,
                "deciding_evidence": "no nav_breakdown.json in this instance directory",
                "structured_timebase": None,
                "corroborating_warnings": [],
            }
        else:
            # The DETAIL (not just the verdict) is kept: on a same-instant re-run the
            # snapshot carries `cash_from_archive` and is still eligible, and the report
            # has to be able to say which evidence won.
            self.eligibility = hwm_eligibility_detail(breakdown)
            self.eligible = bool(self.eligibility["eligible"])
            self.ineligible_reason = str(self.eligibility["reason"])
        self.current_nav = _num(state.get("current_nav")) if state else None
        self.peak_nav = _num(state.get("peak_nav")) if state else None
        self.drawdown_pct = _num(state.get("drawdown_pct")) if state else None
        hist = state.get("nav_history_10d") if state else None
        self.nav_history = list(hist) if isinstance(hist, list) else None
        # The nested copy of the PREVIOUS round's state (see NESTED_REPAIRED_FIELDS).
        prior = state.get("prior_state") if state else None
        self.prior_state = prior if isinstance(prior, dict) else None
        self.prior_as_of = (
            str(self.prior_state.get("as_of_date"))
            if self.prior_state is not None and self.prior_state.get("as_of_date")
            else None
        )
        self.prior_peak_nav = (
            _num(self.prior_state.get("peak_nav")) if self.prior_state is not None else None
        )
        self.prior_drawdown_pct = (
            _num(self.prior_state.get("drawdown_pct")) if self.prior_state is not None else None
        )
        phist = self.prior_state.get("nav_history_10d") if self.prior_state is not None else None
        self.prior_nav_history = list(phist) if isinstance(phist, list) else None

    @property
    def rel(self) -> str:
        return f"{self.date}/{self.instance}"

    @property
    def state_path(self) -> Path:
        return self.path / "account_state.json"

    @property
    def mixed_delta(self) -> Optional[float]:
        if self.current_nav is None or self.leg_nav is None:
            return None
        return self.current_nav - self.leg_nav


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _load_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except Exception:
        return None


def collect_facts(runs_root: Path) -> Tuple[List["RunFact"], List[Dict[str, Any]]]:
    """Every ``<date>/<HHMMSS>/`` round in run order, plus the skipped directories.

    Sorted by ``(date, instance)`` so "the series ending at that round" is well defined:
    the archive only ever appends, and an instance name is its start time.
    """
    facts: List[RunFact] = []
    skipped: List[Dict[str, Any]] = []
    for date_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for inst_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
            if not INSTANCE_DIR_RE.match(inst_dir.name):
                skipped.append(
                    {
                        "dir": f"{date_dir.name}/{inst_dir.name}",
                        "reason": (
                            "not an <HHMMSS> instance directory (pre-repair backup or "
                            "auxiliary run dir): evidence, never repaired"
                        ),
                    }
                )
                continue
            breakdown = _load_json(inst_dir / "nav_breakdown.json")
            state = _load_json(inst_dir / "account_state.json")
            facts.append(RunFact(date_dir.name, inst_dir.name, inst_dir, breakdown, state))
    return facts, skipped


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------
def eligible_series(facts: Sequence[RunFact]) -> List[float]:
    """Same-instant leg NAVs in run order, adjacent duplicates removed.

    Only ``eligible`` rounds contribute: an ineligible round's legs are, by definition,
    not both struck at one instant, so its sum is exactly the phantom the repair is
    undoing.
    """
    return dedupe_adjacent([f.leg_nav for f in facts if f.eligible and f.leg_nav is not None])


def resolve_peak(facts: Sequence[RunFact], peak_override: Optional[float]) -> Tuple[float, str, float]:
    """``(restored_peak, source, max_eligible_leg_nav, max_leg_nav_all)``.

    TWO GUARD RAILS, one exemption, and one UPPER BAND (review findings, LOW + MEDIUM):

    * The floor is computed from **HWM-eligible** rounds only. An ineligible round's legs
      are, by definition, not same-instant, so their SUM is exactly the phantom the repair
      removes -- a cross-timebase breakdown whose legs add up above the all-time high must
      never be able to pull the restored peak UP toward itself.
    * The floor applies only to the **auto-derived** peak. ``restate_peak`` over the
      eligible set already satisfies it by construction; the check stays as a cheap
      invariant against future edits.
    * An explicit ``--peak`` is **exempt from the floor**: it is the operator's assertion,
      usually taken from a round the archive cannot see (here: 203049.96 from 2026-09-29).
      A below-eligible-maximum override is reported as a warning (``peak_warnings``),
      never refused.
    * ... but an explicit ``--peak`` is **not unbounded**. The floor is a LOWER bound, so
      it cannot possibly stop ``--peak 208830.78`` -- the phantom is ABOVE it. The band
      that separates a legitimate external ATH (0.28% above the archive's best leg NAV)
      from the phantom (3.13% above it) is an UPPER one, and it is checked in ``appraise``
      against ``max_leg_nav_all * (1 + PEAK_PROVENANCE_TOL)``: loud warning, never a hard
      abort. (The 1.2.0 docstring justified the floor as protection against
      ``--peak <phantom>``; that was backwards and is corrected here.)

    ``max_leg_nav_all`` (every round, eligible or not) is still returned for the report:
    readers need to see how far the phantom's leg sum sits from the restored mark -- and
    it is the UPPER-bound reference, because "above every leg NAV in the archive" is the
    shape of both the legitimate external ATH and the phantom.
    """
    leg_navs = [f.leg_nav for f in facts if f.leg_nav is not None]
    if not leg_navs:
        raise ValueError("no leg NAV could be computed from any nav_breakdown.json")
    max_leg_all = max(leg_navs)
    eligible_legs = [f.leg_nav for f in facts if f.eligible and f.leg_nav is not None]
    max_leg = max(eligible_legs) if eligible_legs else max_leg_all

    if peak_override is not None:
        restored, source = float(peak_override), "explicit --peak"
    else:
        restored = restate_peak(
            (f.rel, f.leg_nav, f.eligible) for f in facts if f.leg_nav is not None
        )
        source = "max eligible leg_nav"

    if not (restored == restored) or restored in (float("inf"), float("-inf")):
        raise ValueError(f"restored peak is not finite: {restored!r}")
    if peak_override is None and restored < max_leg - 0.005:
        raise ValueError(
            f"auto-derived peak {restored:.2f} < max eligible leg_nav {max_leg:.2f}: "
            "restate_peak must return the highest eligible NAV, so this is a bug, not a "
            "data condition -- refusing to write"
        )
    return restored, source, max_leg, max_leg_all


def _money_same(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) < 0.005


def _history_same(a: Optional[Sequence[Any]], b: Optional[Sequence[Any]]) -> bool:
    if a is None or b is None:
        return a is b
    if len(a) != len(b):
        return False
    return all(_money_same(_num(x), _num(y)) for x, y in zip(a, b))


def propose_for(
    fact: RunFact,
    *,
    prefix_series: Sequence[float],
    restored_peak: float,
) -> Optional[Dict[str, Any]]:
    """The three restated fields for one round (``None`` when nothing can be proposed).

    A round without a same-instant ``leg_nav`` gets **no** proposal: without the legs
    there is no evidence of what that round's NAV was, and writing a peak/history into
    such a directory would be restating a round this tool cannot read. It is reported
    (``NO_NAV_BREAKDOWN`` / ``LEG_NAV_UNUSABLE``) and left alone.
    """
    if fact.state is None or fact.leg_nav is None:
        return None
    return {
        "peak_nav": restored_peak,
        "drawdown_pct": drawdown(fact.leg_nav, restored_peak),
        "nav_history_10d": list(prefix_series[-NAV_HISTORY_LEN:]),
    }


def stored_value(fact: RunFact, field: str) -> Any:
    """The currently-recorded value for a repair key (top-level or ``prior_state.*``)."""
    if field.startswith(NESTED_PREFIX):
        base = field[len(NESTED_PREFIX):]
        if base == "nav_history_10d":
            return fact.prior_nav_history
        return getattr(fact, "prior_" + base, None)
    if field == "nav_history_10d":
        return fact.nav_history
    return getattr(fact, field, None)


def propose_prior_state(
    fact: RunFact,
    *,
    prefix_series: Optional[Sequence[float]],
    restored_peak: float,
) -> Optional[Dict[str, Any]]:
    """The three restated fields for the NESTED ``prior_state`` copy.

    ``prefix_series`` is the leg-derived series ending at the run whose date equals
    ``prior_state.as_of_date`` (``None`` when that date has no ``<HHMMSS>`` round under the
    root, e.g. a holiday, or a copy pointing outside this archive).

    WHY the peak is the same ``restored_peak`` for every round: the first pass wrote that
    one verified mark into every top-level ``peak_nav``, so the nested copy must agree with
    the snapshot it was copied from -- otherwise the same-day swap reads a different peak
    than the file next door. ``drawdown_pct`` is derived from the copy's OWN
    ``current_nav`` (never rewritten here), which is why a nested dd can legitimately come
    out positive: it records a NAV above the verified mark.
    """
    if fact.prior_state is None or fact.prior_as_of is None or prefix_series is None:
        return None
    proposed: Dict[str, Any] = {
        "peak_nav": restored_peak,
        "nav_history_10d": list(prefix_series[-NAV_HISTORY_LEN:]),
    }
    prior_nav = _num(fact.prior_state.get("current_nav"))
    if prior_nav is not None:
        proposed["drawdown_pct"] = drawdown(prior_nav, restored_peak)
    return proposed


def changes_for(
    fact: RunFact,
    proposed: Optional[Mapping[str, Any]],
    prior_proposed: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Field-level diff ``{field: {before, after}}`` against what the file says now.

    Keys are flat: ``peak_nav`` ... for the top level and ``prior_state.peak_nav`` ... for
    the nested copy, so apply/verify/manifest share one code path.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for field in REPAIRED_FIELDS:
        if proposed is None or field not in proposed:
            continue
        before = stored_value(fact, field)
        after = proposed[field]
        if not _field_same(field, before, after):
            out[field] = {"before": before, "after": after}
    for field in REPAIRED_FIELDS:
        if prior_proposed is None or field not in prior_proposed:
            continue
        key = NESTED_PREFIX + field
        before = stored_value(fact, key)
        after = prior_proposed[field]
        if not _field_same(field, before, after):
            out[key] = {"before": before, "after": after}
    return out


def history_is_stale(history: Optional[Sequence[Any]], ceiling: float) -> bool:
    """Does a stored ``nav_history_10d`` still carry a phantom value or an adjacent dup?

    The two shapes the first pass left behind in the nested copies:
    * a value ABOVE the verified mark (208830.78 > 203049.96) -- a NAV that never existed;
    * an adjacent duplicate (199935.46 twice) -- a re-posted mark that occupies a slot in
      a ten-slot window and makes the recovery measurement count a session twice.

    ``ceiling`` is the verified high-water mark: nothing recorded above it can be real.
    """
    if not history:
        return False
    previous: Optional[float] = None
    for raw in history:
        value = _num(raw)
        if value is None:
            previous = None
            continue
        if value > ceiling + 0.005:
            return True
        if previous is not None and abs(value - previous) < 0.005:
            return True
        previous = value
    return False


def classify(fact: RunFact, proposed: Optional[Mapping[str, Any]],
             restored_peak: float, changes: Mapping[str, Any]) -> List[str]:
    """Diagnostic codes for the report; the *changes* are what ``--apply`` acts on."""
    codes: List[str] = []
    delta = fact.mixed_delta
    if delta is not None and abs(delta) > NAV_ABS_TOL:
        codes.append("MIXED_TIMEBASE")
    if fact.state is None:
        codes.append("NO_ACCOUNT_STATE")
    if fact.breakdown is None:
        codes.append("NO_NAV_BREAKDOWN")
    elif not fact.eligible:
        codes.append("HWM_INELIGIBLE")
    elif fact.leg_nav is None:
        codes.append("LEG_NAV_UNUSABLE")
    if fact.peak_nav is not None and fact.peak_nav > restored_peak + 0.005:
        codes.append("PEAK_ABOVE_RESTORED")
    if fact.peak_nav is not None and fact.leg_nav is not None:
        try:
            expected = drawdown(fact.leg_nav, fact.peak_nav)
        except ValueError:
            expected = None
        if expected is not None and fact.drawdown_pct is not None:
            if abs(fact.drawdown_pct - expected) > DRAWDOWN_ABS_TOL:
                codes.append("DRAWDOWN_INCONSISTENT")
    if "nav_history_10d" in changes:
        codes.append("NAV_HISTORY_RESTATED")
    if any(c in changes for c in ("peak_nav", "drawdown_pct")):
        codes.append("PEAK_RESTATED")

    # --- nested `prior_state` findings (reported even when no write is needed) --------
    if fact.prior_state is not None:
        if fact.prior_peak_nav is not None and (
            abs(fact.prior_peak_nav - restored_peak) > 0.005
        ):
            codes.append("NESTED_PRIOR_STATE_STALE")
        if history_is_stale(fact.prior_nav_history, restored_peak):
            codes.append("NESTED_PRIOR_STATE_HISTORY_STALE")
        if NESTED_PREFIX + "peak_nav" in changes or NESTED_PREFIX + "drawdown_pct" in changes:
            codes.append("NESTED_PRIOR_STATE_RESTATED")
    return codes


def appraise(runs_root: Path, peak_override: Optional[float]) -> Dict[str, Any]:
    """Full read-only appraisal: facts, per-round proposals, exit-relevant counts."""
    facts, skipped = collect_facts(runs_root)
    if not facts:
        raise ValueError(f"no <date>/<HHMMSS>/ run directories under {runs_root}")

    restored_peak, peak_source, max_leg, max_leg_all = resolve_peak(facts, peak_override)
    peak_warnings: List[str] = []
    if peak_override is not None and restored_peak < max_leg - 0.005:
        # Informational only: an explicit `--peak` is the operator's assertion and wins
        # (see `resolve_peak`). Printed and recorded so a reader can see the gap.
        peak_warnings.append(
            f"explicit --peak {restored_peak:.2f} is below the highest HWM-eligible leg NAV "
            f"{max_leg:.2f}; the override was honoured (the operator's assertion outranks the "
            "archive), but a peak below a measured eligible NAV will report a phantom drawdown"
        )
    if peak_override is not None and max_leg_all > 0 and (
        restored_peak > max_leg_all * (1.0 + PEAK_PROVENANCE_TOL)
    ):
        # (review finding, MEDIUM) The floor above is a LOWER bound, so it cannot stop
        # `--peak 208830.78`: the phantom sits ABOVE every leg NAV, not below it. This is
        # the band that actually separates the two cases -- the legitimate external ATH is
        # 0.28% above the archive's best leg NAV, the phantom 3.13% above it. A warning,
        # never a hard abort: an ATH taken from a round this archive cannot see is
        # legitimate. It must simply never be silent, because the same edit that fixes the
        # archive can also re-inject the phantom verbatim.
        excess = restored_peak / max_leg_all - 1.0
        peak_warnings.append(
            f"explicit --peak {restored_peak:.2f} is ABOVE every leg NAV in this archive "
            f"(highest {max_leg_all:.2f}) by {excess:.2%} > PEAK_PROVENANCE_TOL "
            f"{PEAK_PROVENANCE_TOL:.2%}; an operator-declared external ATH legitimately looks "
            "like this, but so does the 2026-11-05 phantom (208830.78, 3.13% above the same "
            "maximum) -- verify the override against a statement you can cite before --apply"
        )

    # ``prior_state`` copies are sourced by DATE, not by position: the running state that
    # a round copied is the FINAL round of that date (the archive only appends, so the last
    # ``<HHMMSS>`` instance of the day is what the next day resumed from).
    last_index_by_date: Dict[str, int] = {}
    for idx, fact in enumerate(facts):
        last_index_by_date[fact.date] = idx

    # Each round's proposed history is the eligible leg-NAV series *ending at that
    # round* -- that is the whole point of restating an archive in place.
    entries: List[Dict[str, Any]] = []
    for idx, fact in enumerate(facts):
        prefix = eligible_series(facts[: idx + 1])
        proposed = propose_for(fact, prefix_series=prefix, restored_peak=restored_peak)
        prior_prefix: Optional[List[float]] = None
        if fact.prior_as_of is not None:
            source_idx = last_index_by_date.get(fact.prior_as_of)
            if source_idx is not None:
                prior_prefix = eligible_series(facts[: source_idx + 1])
        prior_proposed = propose_prior_state(
            fact, prefix_series=prior_prefix, restored_peak=restored_peak
        )
        changes = changes_for(fact, proposed, prior_proposed)
        entries.append(
            {
                "date": fact.date,
                "instance": fact.instance,
                "dir": fact.rel,
                "leg_nav": fact.leg_nav,
                "current_nav": fact.current_nav,
                "mixed_timebase_delta": fact.mixed_delta,
                "eligible": fact.eligible,
                "ineligible_reason": fact.ineligible_reason,
                # Which evidence decided eligibility (structured field vs warning marker)
                # and any marker that was recorded without flipping the verdict.
                "eligibility": fact.eligibility,
                "stored": {
                    "peak_nav": fact.peak_nav,
                    "drawdown_pct": fact.drawdown_pct,
                    "nav_history_10d": fact.nav_history,
                },
                "prior_state": {
                    "present": fact.prior_state is not None,
                    "as_of_date": fact.prior_as_of,
                    "source_dir": (
                        facts[last_index_by_date[fact.prior_as_of]].rel
                        if fact.prior_as_of in last_index_by_date
                        else None
                    ),
                    "stored": {
                        "peak_nav": fact.prior_peak_nav,
                        "drawdown_pct": fact.prior_drawdown_pct,
                        "nav_history_10d": fact.prior_nav_history,
                        "current_nav": (
                            _num(fact.prior_state.get("current_nav"))
                            if fact.prior_state is not None
                            else None
                        ),
                    },
                    "proposed": prior_proposed,
                },
                "proposed": proposed,
                "changes": changes,
                "codes": classify(fact, proposed, restored_peak, changes),
            }
        )

    # --- cross-file consistency: a copy must agree with the snapshot it came from -----
    # Reported only (never auto-resolved by writing): if the nested peak and the source's
    # top-level peak disagree AFTER this repair, one of them was edited by something other
    # than this tool, and picking a winner here would bury that fact.
    peak_after: List[Optional[float]] = []
    for entry in entries:
        change = entry["changes"].get("peak_nav")
        peak_after.append(
            change["after"] if change is not None else entry["stored"]["peak_nav"]
        )
    mismatches: List[Dict[str, Any]] = []
    for entry in entries:
        prior = entry["prior_state"]
        if not prior["present"] or prior["as_of_date"] is None:
            continue
        source_idx = last_index_by_date.get(prior["as_of_date"])
        if source_idx is None:
            # Nothing under this root for the copied date (a flat `<date>/` with no
            # `<HHMMSS>/` round): there is no snapshot to compare against, so this is
            # "unverifiable", never a mismatch -- and no nested write is proposed either
            # (`propose_prior_state` needs a leg-derived series).
            if "PRIOR_STATE_SOURCE_MISSING" not in entry["codes"]:
                entry["codes"].append("PRIOR_STATE_SOURCE_MISSING")
            continue
        nested_after = (
            entry["changes"][NESTED_PREFIX + "peak_nav"]["after"]
            if NESTED_PREFIX + "peak_nav" in entry["changes"]
            else prior["stored"]["peak_nav"]
        )
        source_peak = peak_after[source_idx]
        if nested_after is None or source_peak is None or not _money_same(nested_after, source_peak):
            if "PRIOR_STATE_SOURCE_MISMATCH" not in entry["codes"]:
                entry["codes"].append("PRIOR_STATE_SOURCE_MISMATCH")
            mismatches.append(
                {
                    "dir": entry["dir"],
                    "prior_as_of": prior["as_of_date"],
                    "prior_peak_after": nested_after,
                    "source_peak_after": source_peak,
                }
            )

    return {
        "script_version": SCRIPT_VERSION,
        "runs_root": str(runs_root),
        "restored_peak": restored_peak,
        "peak_source": peak_source,
        #: Floor used by the auto-derived path: HWM-ELIGIBLE rounds only (review finding).
        "max_eligible_leg_nav": max_leg,
        #: Raw maximum over EVERY round (eligible or not) -- how far a cross-timebase
        #: leg sum sits from the restored mark. Reported, never used as a floor.
        "max_leg_nav_all": max_leg_all,
        "peak_floor_applied": peak_override is None,
        "peak_warnings": peak_warnings,
        "repaired_fields": list(REPAIRED_FIELDS),
        "never_touched": list(NEVER_TOUCHED),
        "scanned": len(entries),
        "flagged": sum(1 for e in entries if e["codes"]),
        "runs": entries,
        "skipped": skipped,
        "targets": [e["dir"] for e in entries if e["changes"]],
        "nested_targets": [
            e["dir"] for e in entries if any(k.startswith(NESTED_PREFIX) for k in e["changes"])
        ],
        "prior_state_source_mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
class ConcurrentEditError(RuntimeError):
    """A target moved between appraisal and apply: the whole run is refused, nothing written."""

    def __init__(self, mismatches: Sequence[str]) -> None:
        self.mismatches = list(mismatches)
        super().__init__(
            "concurrent edit detected -- refusing to write anything (0 files touched):\n  "
            + "\n  ".join(self.mismatches)
        )


class ManifestError(RuntimeError):
    """The day's manifest exists but cannot be parsed: refuse to overwrite the ledger.

    Same reasoning as the concurrent-edit abort: the manifest is the only record of what
    an earlier pass changed, and ``.orig`` md5s cannot be re-derived once the byte copy is
    gone. Silently replacing an unreadable ledger would destroy evidence, so the run stops
    before the first write (checked in ``main`` BEFORE ``apply_repairs``).
    """


class ApplyAbortedError(RuntimeError):
    """The write loop stopped PARTWAY through; ``applied`` is what it already rewrote.

    (review finding, MEDIUM) ``ConcurrentEditError`` is a clean abort -- it is raised
    before the first write, so 0 files are touched. This is the other shape: a generic I/O
    failure (disk full, permissions on a later file) or a serializer that moved an
    out-of-scope field, AFTER earlier files were rewritten. It carries the rewritten list
    so ``main`` can report the blast radius and still write a manifest pass marked
    ``incomplete: true`` -- otherwise the partial pass would be invisible in the ledger.
    """

    def __init__(
        self,
        message: str,
        *,
        applied: Sequence[Mapping[str, Any]] = (),
        failed_files: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.applied = [dict(entry) for entry in applied]
        self.failed_files = [dict(entry) for entry in failed_files]
        super().__init__(message)


class RepairWriteError(ApplyAbortedError):
    """An ``OSError`` during the write loop: name the file, keep the already-rewritten list.

    The failing file is UNCHANGED on disk (``write_bytes_atomic`` replaces atomically),
    but ``ensure_backup`` may already have produced its ``.orig`` -- reported as the
    entry's ``backup`` so an operator can see the extra file this abort created.
    """


class RepairScopeError(ApplyAbortedError):
    """A serialized rewrite changed a field OUTSIDE the repair scope: write nothing more.

    (review finding, LOW) ``fields_changed`` is what ``apply_repairs`` *intended* to
    change. Nothing checked that the BYTES it wrote agreed: a serializer that dropped,
    reordered into different values, or re-encoded an untouched field would rewrite the
    file while the manifest still reported three (or six) repaired fields. This error
    names every leaked path and leaves that file untouched.
    """


def _same_json_value(a: Any, b: Any) -> bool:
    """Deep equality for loaded JSON values, with ``NaN == NaN``.

    WHY not plain ``!=``: ``json`` accepts ``NaN``/``Infinity`` on both read and write, and
    ``float('nan') != float('nan')``. A ``NaN`` sitting in an UNTOUCHED field (a broken
    mark inside ``lifecycle_states``, say) would then be reported as "the serializer moved
    this field", aborting a perfectly good repair. Recursive because ``entry_prices`` /
    ``lifecycle_states`` are nested containers.
    """
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same_json_value(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same_json_value(x, y) for x, y in zip(a, b))
    return a == b


def unexpected_field_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> List[str]:
    """Field paths that differ between two loaded states, EXCLUDING the repaired ones.

    Only ``REPAIRED_FIELDS`` (top level) and the same three inside ``prior_state`` may
    ever differ -- that is the whole write contract (
    ``NEVER_TOUCHED`` says the same thing from the other side). A non-empty return means
    the serialized bytes moved something nobody asked them to move, and the file must not
    be written.

    ``prior_state`` is compared one level deep on purpose: its ``current_nav``,
    ``tier1_consecutive_bars``, ``circuit_breaker_tier`` and ``entry_prices`` are the ones
    the manifest promises never to touch, and a whole-block comparison would mask exactly
    those edits.
    """
    out: List[str] = []
    for key in sorted(set(before) | set(after)):
        if key in REPAIRED_FIELDS:
            continue
        if key not in before or key not in after:
            # a field that appeared or vanished is out of scope whatever it is
            out.append(str(key))
            continue
        old, new = before[key], after[key]
        if key == NESTED_CONTAINER:
            if not isinstance(old, dict) or not isinstance(new, dict):
                if not _same_json_value(old, new):
                    out.append(str(key))
                continue
            for sub in sorted(set(old) | set(new)):
                if sub in REPAIRED_FIELDS:
                    continue
                if sub not in old or sub not in new or not _same_json_value(old[sub], new[sub]):
                    out.append(f"{NESTED_PREFIX}{sub}")
            continue
        if not _same_json_value(old, new):
            out.append(str(key))
    return out


def _field_same(field: str, a: Any, b: Any) -> bool:
    """Field-aware equality used by BOTH the diff and the pre-write re-check.

    One comparison, two callers: if the diff and the verification ever disagreed about
    "same", a file could be reported as changed and then rejected as moved (or silently
    overwritten). Keep them the same function.
    """
    if field == "nav_history_10d" or field == "prior_state.nav_history_10d":
        return _history_same(a, b)
    base = field[len(NESTED_PREFIX):] if field.startswith(NESTED_PREFIX) else field
    if base == "drawdown_pct":
        return (
            a is not None
            and b is not None
            and abs(float(a) - float(b)) <= DRAWDOWN_ABS_TOL
        )
    return _money_same(a, b)


def verify_appraisal_still_holds(
    report: Mapping[str, Any], runs_root: Path
) -> None:
    """Re-read every target and assert it still holds the values we appraised.

    WHY this has to exist as its own step: a phantom peak is the one thing the archive
    cannot re-derive, so a repair that silently overwrites a number somebody else just
    edited destroys the only evidence of what the engine read that day. Every mismatch is
    collected (not just the first) so one report names the whole blast radius, and the
    check runs over ALL targets BEFORE the first write -- an abort must leave 0 files
    touched, not "some of them".
    """
    mismatches: List[str] = []
    for entry in report["runs"]:
        if not entry["changes"]:
            continue
        rel = f"{entry['dir']}/account_state.json"
        path = Path(runs_root) / entry["dir"] / "account_state.json"
        if not path.exists():
            mismatches.append(f"{rel}: file disappeared after appraisal")
            continue
        try:
            obj = json.loads(path.read_bytes().decode("utf-8"))
        except Exception as exc:
            mismatches.append(f"{rel}: unreadable after appraisal ({exc})")
            continue
        for field, change in entry["changes"].items():
            on_disk = read_field(obj, field)
            if not _field_same(field, on_disk, change["before"]):
                mismatches.append(
                    f"{rel}:{field} appraised={change['before']!r} but on disk={on_disk!r}"
                )
    if mismatches:
        raise ConcurrentEditError(mismatches)


def read_field(obj: Mapping[str, Any], field: str) -> Any:
    """Read a repair key from a loaded ``account_state`` (top-level or nested)."""
    if field.startswith(NESTED_PREFIX):
        container = obj.get(NESTED_CONTAINER)
        if not isinstance(container, dict):
            return None
        return container.get(field[len(NESTED_PREFIX):])
    return obj.get(field)


def write_field(obj: Dict[str, Any], field: str, value: Any) -> None:
    """Write a repair key into a loaded ``account_state`` (top-level or nested).

    The nested container is only ever *updated in place*: if ``prior_state`` is not a
    mapping the write is refused rather than created, so a malformed block is never
    replaced by a half-built one.
    """
    if field.startswith(NESTED_PREFIX):
        container = obj.get(NESTED_CONTAINER)
        if not isinstance(container, dict):
            raise ValueError(
                f"cannot write {field}: {NESTED_CONTAINER} is not an object on disk"
            )
        container[field[len(NESTED_PREFIX):]] = value
        return
    obj[field] = value


def apply_repairs(report: Mapping[str, Any], runs_root: Path) -> List[Dict[str, Any]]:
    """Rewrite the restated fields (top level + nested ``prior_state``) in every target.

    Returns one record per file that actually changed. A file whose on-disk bytes
    already equal the proposed bytes is *not* rewritten -- that is what makes the second
    ``--apply`` byte-identical rather than merely equivalent.

    THREE rails, in the order they matter (review findings, MEDIUM + LOW):

    1. ``verify_appraisal_still_holds`` runs over ALL targets before the first write, so a
       concurrent edit aborts with 0 files touched (``ConcurrentEditError``);
    2. the serialized bytes are RE-PARSED and diffed against the object as it was read,
       and any changed path outside ``REPAIRED_FIELDS`` / ``NESTED_REPAIRED_FIELDS``
       aborts that file (``RepairScopeError``) -- ``fields_changed`` says what the tool
       meant to write, this says what it actually wrote;
    3. an ``OSError`` mid-loop is not allowed to escape ``main`` as a traceback: it is
       wrapped in ``RepairWriteError`` carrying the files already rewritten, because a
       partial pass that nobody recorded is worse than no pass at all.

    Raises:
        ConcurrentEditError: when any target no longer matches the appraisal. Raised
            before the first write, so the archive is untouched.
        RepairScopeError: when a rewrite would touch a field outside the repair scope.
            That file is left untouched.
        RepairWriteError: when an I/O error stopped the loop partway; ``applied`` is what
            was already rewritten.
    """
    verify_appraisal_still_holds(report, runs_root)

    applied: List[Dict[str, Any]] = []
    for entry in report["runs"]:
        if not entry["changes"]:
            continue
        rel = f"{entry['dir']}/account_state.json"
        path = Path(runs_root) / entry["dir"] / "account_state.json"
        backup: Optional[Dict[str, Any]] = None
        try:
            raw_before = path.read_bytes()
            obj = json.loads(raw_before.decode("utf-8"))
            # An untouched parse, for the post-serialization scope check below: `obj` is
            # mutated in place by `write_field`, so it cannot serve as "before".
            original = copy.deepcopy(obj)
            for field, change in entry["changes"].items():
                write_field(obj, field, change["after"])
            raw_after = json_bytes(obj)
            if raw_after == raw_before:
                continue
            leaked = unexpected_field_changes(
                original, json.loads(raw_after.decode("utf-8"))
            )
            if leaked:
                raise RepairScopeError(
                    f"{rel}: the serialized rewrite changed field(s) outside the repair "
                    f"scope {sorted(REPAIRED_FIELDS)}: {', '.join(leaked)} -- refusing to "
                    "write this file (the bytes and `fields_changed` disagree; that is a "
                    "serializer bug, not a data condition)",
                    applied=applied,
                    failed_files=[
                        {
                            "path": rel,
                            "error": "out-of-scope field change: " + ", ".join(leaked),
                        }
                    ],
                )
            backup = ensure_backup(path, raw_before)
            write_bytes_atomic(path, raw_after)
        except OSError as exc:
            raise RepairWriteError(
                f"{rel}: write loop failed ({type(exc).__name__}: {exc}); "
                f"{len(applied)} file(s) were already rewritten before this abort",
                applied=applied,
                failed_files=[
                    {
                        "path": rel,
                        "error": f"{type(exc).__name__}: {exc}",
                        # `.orig` is written BEFORE the live file, so a failed live write
                        # leaves it behind. Reported, never deleted: it is a byte-exact
                        # copy of the file that is now unchanged on disk.
                        "backup": (
                            _rel_to(backup["path"], runs_root)
                            if backup and backup.get("created")
                            else None
                        ),
                    }
                ],
            ) from exc
        applied.append(
            {
                "path": rel,
                "md5_before": md5_bytes(raw_before),
                # md5 of the bytes just written, not of a re-read: `os.replace` puts
                # exactly these bytes at `path`, and a second disk read is one more
                # `OSError` that would otherwise look like a failed write.
                "md5_after": md5_bytes(raw_after),
                "backup": _rel_to(backup["path"], runs_root) if backup else None,
                "backup_created": bool(backup and backup["created"]),
                "nested_fields_changed": [
                    f for f in entry["changes"] if f.startswith(NESTED_PREFIX)
                ],
                "fields_changed": {
                    field: {"before": change["before"], "after": change["after"]}
                    for field, change in entry["changes"].items()
                },
            }
        )
    return applied


def _rel_to(path: Optional[str], runs_root: Path) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(Path(runs_root).resolve()).as_posix()
    except Exception:
        return str(path)


def manifest_path(runs_root: Path, when: Optional[datetime] = None) -> Path:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return Path(runs_root) / f"nav_hwm_manifest.{stamp}.json"


def build_manifest(
    report: Mapping[str, Any],
    applied: Sequence[Mapping[str, Any]],
    runs_root: Path,
    *,
    incomplete: bool = False,
    failed_files: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Ledger of what was rewritten: per-file before/after md5 + changed fields.

    Written only when at least one file was actually rewritten, so an idempotent rerun
    leaves the previous manifest (and therefore its md5s) exactly as they were.

    ``incomplete=True`` is the PARTIAL-pass shape (review finding, MEDIUM): the write loop
    aborted after some files were already rewritten, so ``files`` is "what really
    happened, as far as it got" and ``failed_files`` names the path that stopped it. A
    partial pass with no ledger would be invisible; an incomplete pass that says so is
    recoverable.

    The entry is a single PASS (see ``merge_manifest``): ``files``/``rewritten`` describe
    this one run, and an older ledger's passes are preserved beside it, never dropped.
    """
    return {
        "generator": "scripts/repair_live_nav_hwm.py",
        "script_version": report["script_version"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs_root": str(runs_root),
        "restored_peak": report["restored_peak"],
        "peak_source": report["peak_source"],
        "max_eligible_leg_nav": report["max_eligible_leg_nav"],
        "max_leg_nav_all": report["max_leg_nav_all"],
        "peak_floor_applied": report["peak_floor_applied"],
        "peak_warnings": report["peak_warnings"],
        "repaired_fields": list(REPAIRED_FIELDS),
        "never_touched": list(NEVER_TOUCHED),
        "scanned": report["scanned"],
        "flagged": report["flagged"],
        "rewritten": len(applied),
        #: True when the write loop aborted partway (see `ApplyAbortedError`): the files
        #: listed below WERE rewritten, and `failed_files` says where it stopped.
        "incomplete": bool(incomplete),
        #: Non-empty only on an aborted pass: `{path, error, backup?}`.
        "failed_files": [dict(entry) for entry in failed_files],
        #: How many rewritten files also had their nested `prior_state` copy restated.
        "nested_rewritten": sum(1 for f in applied if f.get("nested_fields_changed")),
        "prior_state_source_mismatches": report["prior_state_source_mismatches"],
        "incident": {
            "date": "2026-11-05",
            "mixed_timebase_nav": 208830.78,
            "same_instant_leg_nav": 198810.57,
            "phantom_equity": 10020.21,
            "real_all_time_high": 203049.96,
            "real_all_time_high_date": "2026-09-29",
            "repeated_on": ["2026-11-06/184826"],
        },
        "files": list(applied),
        "flagged_but_unchanged": [
            {"dir": e["dir"], "codes": e["codes"]}
            for e in report["runs"]
            if e["codes"] and not e["changes"]
        ],
        "skipped_dirs": report["skipped"],
    }


def pass_from_ledger(ledger: Mapping[str, Any]) -> Dict[str, Any]:
    """One ``passes`` entry from a whole manifest (legacy itself, if pre-1.2.0).

    WHY the keys are copied rather than referenced: a legacy manifest's top level IS the
    first pass (that is the only shape 1.0.x/1.1.x could write), so upgrading one is a
    pure re-shaping -- ``recorded_at`` from its ``generated_at_utc``, ``file_count`` from
    its ``rewritten`` -- with no field invented and no md5 recomputed.
    """
    return {
        "recorded_at": ledger["generated_at_utc"],
        "script_version": ledger["script_version"],
        "file_count": ledger["rewritten"],
        "nested_rewritten": ledger.get("nested_rewritten", 0),
        # (review finding, MEDIUM) A pass that aborted partway says so, in the ledger, for
        # ever: `file_count` alone cannot distinguish "repaired 2 of 3 targets" from
        # "there were only 2 targets".
        "incomplete": bool(ledger.get("incomplete", False)),
        "failed_files": [dict(entry) for entry in ledger.get("failed_files") or []],
        "files": list(ledger.get("files") or []),
    }


def validated_passes(
    ledger: Mapping[str, Any], *, source: str
) -> Optional[List[Dict[str, Any]]]:
    """The ledger's recorded ``passes``, VALIDATED; ``None`` when it predates 1.2.0.

    (review finding, LOW) 1.2.0 dropped malformed entries while still rewriting the file
    (``[dict(e) for e in recorded if isinstance(e, Mapping)]``), so a ledger could claim a
    SHORTER history than it was given -- the same class of evidence loss the append-only
    ledger exists to prevent. A pass entry that cannot be read is a ``ManifestError``, and
    every caller of this function is on the abort-BEFORE-write path: ``load_manifest``
    validates the day's ledger before the first write, and ``merge_manifest`` re-validates
    what it is about to merge into.
    """
    if "passes" not in ledger:
        return None
    recorded = ledger.get("passes")
    if not isinstance(recorded, list):
        raise ManifestError(
            f"{source}: 'passes' is {type(recorded).__name__}, not a list; refusing to "
            "merge -- an earlier pass record cannot be read, so it must not be replaced"
        )
    passes: List[Dict[str, Any]] = []
    for index, entry in enumerate(recorded):
        if not isinstance(entry, Mapping):
            raise ManifestError(
                f"{source}: passes[{index}] is {type(entry).__name__}, not an object; "
                "refusing to merge -- rewriting this ledger would drop that entry, and an "
                "unreadable pass record is evidence of a pass that happened"
            )
        passes.append(dict(entry))
    return passes


def load_manifest(path: Path) -> Optional[Dict[str, Any]]:
    """Read today's manifest, or ``None`` when there is none.

    Raises ``ManifestError`` for a file that exists but is not a JSON object: the caller
    must abort BEFORE writing, because a ledger that cannot be read cannot be merged and
    must not be replaced. The message names the FULL path (not just
    ``nav_hwm_manifest.<date>.json``): the operator has to be able to act on it, and the
    bare name is ambiguous once more than one run root exists under the workspace.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_bytes().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- any read/parse failure is the same verdict
        raise ManifestError(
            # The FULL path, not just the file name: this error is what an operator (and
            # the offline test) has to act on, and `nav_hwm_manifest.<date>.json` alone is
            # ambiguous once more than one run root exists under the workspace.
            f"{path} exists but cannot be parsed ({exc}); refusing to overwrite a "
            "ledger whose earlier passes would be lost -- inspect or move it aside first"
        ) from exc
    if not isinstance(obj, dict):
        raise ManifestError(
            f"{path} exists but is not a JSON object ({type(obj).__name__}); "
            "refusing to overwrite it"
        )
    # (review finding, LOW) Validate the `passes` array BEFORE the first write: a
    # malformed entry used to be silently dropped by `merge_manifest` while the file was
    # rewritten, so the ledger could lose the record of a pass that really happened.
    validated_passes(obj, source=str(path))
    return obj


def merge_manifest(
    existing: Optional[Mapping[str, Any]],
    ledger: Mapping[str, Any],
) -> Dict[str, Any]:
    """Append this run's pass to ``existing``'s ``passes`` (newest last, mirrored on top).

    A repair is routinely run twice a day -- here a top-level pass, then a ``prior_state``
    pass once the nested gap was found -- so last-write-wins erased the first pass's
    record even though its ``.orig`` backups survived. This keeps every pass.

    ``existing`` may be a multi-pass manifest (``passes`` already present) or a legacy
    single-pass one (its top level becomes pass 1 exactly; see ``pass_from_ledger``).
    ``None`` means "first pass of the day".

    A ``passes`` entry that is not a mapping raises ``ManifestError`` (review finding,
    LOW): the 1.2.0 version dropped such entries and still overwrote the file, which is
    the one thing an append-only ledger must never do.

    The returned document also mirrors the NEWEST pass at the top level
    (``files``/``rewritten``/``nested_rewritten``/``generated_at_utc``/``script_version``,
    taken from ``ledger`` unchanged), so every reader written against the 1.0-1.1 shape
    keeps seeing what it saw -- just never at the cost of the older passes.
    """
    passes: List[Dict[str, Any]] = []
    if isinstance(existing, Mapping):
        recorded_passes = validated_passes(existing, source="existing manifest")
        if recorded_passes is not None:
            passes = recorded_passes
        else:
            # Legacy single-pass manifest: its top level is pass 1, verbatim.
            passes = [pass_from_ledger(existing)]
    passes.append(pass_from_ledger(ledger))

    merged = dict(ledger)
    merged["pass_count"] = len(passes)
    merged["passes"] = passes
    return merged


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], width: int = 11, digits: int = 2) -> str:
    if value is None:
        return f"{'-':>{width}}"
    return f"{value:>{width}.{digits}f}"


def print_table(report: Mapping[str, Any]) -> None:
    """Short, scannable table: only the rounds that carry a finding."""
    print(
        f"[scan] root={report['runs_root']} scanned={report['scanned']} "
        f"flagged={report['flagged']} skipped_dirs={len(report['skipped'])}"
    )
    print(
        f"[peak] restored_peak={report['restored_peak']:.2f} "
        f"({report['peak_source']}) ; max_eligible_leg_nav="
        f"{report['max_eligible_leg_nav']:.2f} ; max_leg_nav_all="
        f"{report['max_leg_nav_all']:.2f}"
        + ("" if report["peak_floor_applied"] else " ; floor: n/a (explicit --peak)")
    )
    for warning in report["peak_warnings"]:
        print(f"[WARN] {warning}")
    header = (
        f"{'date':<11} {'instance':<8} {'leg_nav':>12} {'current_nav':>12} {'delta':>11} "
        f"{'peak: now -> restored':>26}  codes"
    )
    print(header)
    print("-" * len(header))
    for entry in report["runs"]:
        if not entry["codes"]:
            continue
        stored_peak = entry["stored"]["peak_nav"]
        after_peak = entry["changes"].get("peak_nav", {}).get("after", stored_peak)
        now_s = "-" if stored_peak is None else f"{round(float(stored_peak), 2)}"
        next_s = "-" if after_peak is None else f"{round(float(after_peak), 2)}"
        print(
            f"{entry['date']:<11} {entry['instance']:<8} {_fmt(entry['leg_nav'], 12)} "
            f"{_fmt(entry['current_nav'], 12)} {_fmt(entry['mixed_timebase_delta'], 11)} "
            f"{now_s + ' -> ' + next_s:>26}  {','.join(entry['codes'])}"
        )
    if report["targets"]:
        print(f"[proposed] {len(report['targets'])} account_state.json in the repair set: "
              f"{', '.join(report['targets'])}")
        if report.get("nested_targets"):
            print(f"[proposed] {len(report['nested_targets'])} of them also have a stale "
                  f"nested prior_state: {', '.join(report['nested_targets'])}")
    else:
        print("[proposed] none -- archive already agrees with its own NAV legs")
    for mismatch in report.get("prior_state_source_mismatches") or []:
        print(f"[WARN] PRIOR_STATE_SOURCE_MISMATCH {mismatch['dir']}: prior_state "
              f"(as_of {mismatch['prior_as_of']}) peak={mismatch['prior_peak_after']} "
              f"!= source top-level peak={mismatch['source_peak_after']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repair_live_nav_hwm.py",
        description=(
            "Restate peak_nav / drawdown_pct / nav_history_10d in the live archive from "
            "same-instant NAV legs. Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--runs-root",
        required=True,
        help="archive root laid out as <root>/<YYYY-MM-DD>/<HHMMSS>/...",
    )
    parser.add_argument(
        "--peak",
        type=float,
        default=None,
        help=(
            "restored high-water mark (e.g. the real ATH 203049.96 from 2026-09-29); "
            "defaults to the highest eligible leg NAV found under the root. An override "
            "more than PEAK_PROVENANCE_TOL above every leg NAV in the archive is HONOURED "
            "but loudly warned: that is where a legitimate external ATH and the 2026-11-05 "
            "phantom (208830.78) look alike"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write account_state.json (peak_nav/drawdown_pct/nav_history_10d only); "
             "without it nothing is modified",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="also write the full machine-readable report to this path",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    runs_root = Path(args.runs_root).expanduser()
    if not runs_root.is_dir():
        print(f"[ERROR] runs root not found: {runs_root}", file=sys.stderr)
        return 2

    try:
        report = appraise(runs_root, args.peak)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print_table(report)

    applied: List[Dict[str, Any]] = []
    abort: Optional[ApplyAbortedError] = None
    if report["targets"] and args.apply:
        target = manifest_path(runs_root)
        # Read (and validate) the ledger BEFORE the first write: an unreadable manifest
        # means an earlier pass's record cannot be merged, so the run must stop with the
        # archive untouched instead of replacing that record after the fact. This now also
        # rejects a malformed `passes` entry (review finding, LOW), for the same reason.
        try:
            existing_ledger = load_manifest(target)
        except ManifestError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        try:
            applied = apply_repairs(report, runs_root)
        except ConcurrentEditError as exc:
            # Abort THE WHOLE RUN: `apply_repairs` verifies every target before the first
            # write, so this exit means 0 files were touched.
            print(f"[ERROR] {exc}", file=sys.stderr)
            print("[aborted] 0 files written; re-run the dry run and re-appraise", file=sys.stderr)
            return 2
        except ApplyAbortedError as exc:
            # (review finding, MEDIUM/LOW) The write loop stopped PARTWAY: earlier files are
            # already rewritten, the failing one is unchanged (an `.orig` may have been
            # created), and there is no ledger entry yet. A partial pass nobody recorded is
            # the worst of the three outcomes, so the manifest is written anyway, marked
            # `incomplete: true`, and the run exits 2 (distinct from a clean 0).
            abort = exc
            applied = exc.applied
            ledger = build_manifest(
                report,
                applied,
                runs_root,
                incomplete=True,
                failed_files=exc.failed_files,
            )
            try:
                merged = merge_manifest(existing_ledger, ledger)
                write_bytes_atomic(target, json_bytes(merged))
                ledger_note = f"manifest -> {target.name} (pass {merged['pass_count']} marked incomplete)"
            except (ManifestError, OSError) as ledger_exc:
                ledger_note = f"MANIFEST NOT WRITTEN ({ledger_exc})"
            print(f"[ERROR] {exc}", file=sys.stderr)
            if applied:
                print(
                    f"[partial] {len(applied)} file(s) rewritten BEFORE the abort and left in "
                    "place; each has its .orig backup holding the pre-repair bytes: "
                    + ", ".join(str(f.get("path")) for f in applied),
                    file=sys.stderr,
                )
            else:
                print("[partial] 0 files were rewritten before the abort", file=sys.stderr)
            for failed in exc.failed_files:
                print(
                    f"[partial] failed: {failed.get('path')} -- {failed.get('error')}"
                    + (
                        f" (orphaned backup: {failed['backup']})"
                        if failed.get("backup")
                        else ""
                    ),
                    file=sys.stderr,
                )
            print(
                f"[partial] {ledger_note}; fix the failing path, re-run the dry run, then "
                "--apply again",
                file=sys.stderr,
            )
        else:
            ledger = build_manifest(report, applied, runs_root)
            if applied:
                merged = merge_manifest(existing_ledger, ledger)
                write_bytes_atomic(target, json_bytes(merged))
                prior_passes = merged["pass_count"] - 1
                print(f"[applied] {len(applied)} file(s) rewritten; manifest -> {target.name} "
                      f"(pass {merged['pass_count']} of the day"
                      + (f", {prior_passes} earlier pass(es) kept" if prior_passes else "") + ")")
            else:
                print("[applied] nothing written: every target already holds the proposed values")
    elif report["targets"]:
        print("[dry-run] nothing written; re-run with --apply to write the three fields")

    if args.json_out:
        out = Path(args.json_out)
        payload = dict(report)
        payload["mode"] = "apply" if args.apply else "dry-run"
        payload["applied"] = applied
        if abort is not None:
            payload["incomplete"] = True
            payload["failed_files"] = [dict(entry) for entry in abort.failed_files]
        write_bytes_atomic(out, json_bytes(payload))

    if abort is not None:
        return 2
    if not report["targets"]:
        print("[clean] 0 = nothing to change")
        return 0
    if not args.apply:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


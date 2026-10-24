#!/usr/bin/env python3
"""QFQ alignment repair: dry-run by default, guarded `--apply` (docs/version.md §14).

WHAT THIS REPAIRS (and why it is not a "re-fetch")
--------------------------------------------------
`docs/version.md` §13 proved that the 2026-10-22 sync applied a *local* scalar
re-alignment to already-stored rows over **bounded** date windows, and that the two
symbols ended up on **opposite sides** of the damage:

``600660.SH`` — **corruption.** 2024-01-02 → 2024-07-19 (132 bars) was multiplied by
    0.9814126394, i.e. it now sits **1.8587 % too low**. Before that rewrite the
    Fuyao namespace agreed *bit-for-bit* with ``MarketDBDataProvider`` across
    2020-01-02 → 2026-10-16, so the reference namespace still holds the exact
    pre-incident values. Repair = **restore** those 132 rows from the reference.
``600362.SH`` — **incomplete repair.** The same rewrite moved 2024-01-02 →
    2025-12-31 onto the reference basis (it cancelled a pre-existing +0.337 %
    Fuyao↔reference gap), but it did **not** extend the correction to the 1690 bars
    before 2024-01-02, leaving a **-0.3402 pp** seam at 2024-01-02. Repair =
    **rebase the prefix** 2017-01-18 → 2023-12-29 by the same constant.

WHY A PREFIX REBASE AND NOT A WINDOW REALIGNMENT
------------------------------------------------
A genuine forward-adjusted (QFQ) re-basing driven by an ex-dividend event multiplies
**every** bar before the ex-date: its footprint is open-ended towards the past. A
*closed* interval re-alignment is mechanically indistinguishable from the incident
itself, so the writer here accepts exactly two shapes, and refuses everything else:

* ``rebase`` — must start at the symbol's **first** bar and end on the session just
  before the seam (``assert_single_basis_prefix``);
* ``restore`` — must be a window whose value is **bit-recoverable** from an
  independent namespace that agrees with the target everywhere *outside* the window
  (``assert_restore_is_reference_exact``).

Both shapes are verified *after* the write, inside the same transaction.

SCOPE — the default plan IS the census damage set
-------------------------------------------------
The repo already learned this lesson once: §14 documented *two* damaged symbols
because a backtest had surfaced exactly two, and the §15 census found five rewritten
symbols of which four were damage. Planning only the documented pair therefore left
**three** damaged blocks unfixed while a second dry-run happily reported "nothing to
repair" — a half-repair that looks complete. So the scope rule is now explicit:

* the **default** scope is **every symbol of ``--provider``** — the same universe the
  §15 census scans. ``--all-symbols`` is kept only as an explicit restatement of that
  default (backward compatible, no semantic change);
* narrowing is possible (``--symbol``, repeatable), but a narrowed run **must** report
  what it did *not* fix: the residual damage list is printed at the end, carried in
  ``--json`` as ``residual_damage``, and the exit code is non-zero while it is non-empty;
* the residual is always measured on the **whole** provider universe, never on the
  narrowed slice — measuring it on the narrowed slice would make every narrowing look
  clean, which is precisely the bug this section exists to prevent;
* ``nothing to repair`` is only printed when the census damage set (whole universe) is
  empty. "The plan's ops are done but damage remains" prints
  ``plan completed, but N damage block(s) remain`` instead.

DRY-RUN / APPLY SEMANTICS
-------------------------
* default is ``--dry-run``: it only ever opens ``read_only=True`` and prints what it
  *would* do (rows, factor, first/last rows, seam amplitude before → after);
* ``--apply`` is the only mode that writes, and it must, in this order:
  1. be refused if a pytest process targets ``<repo>/data`` (L2.13 guard, reused);
  2. take a timestamped byte-copy backup of the whole database file;
  3. apply every op inside **one transaction** (``BEGIN`` … ``COMMIT``);
  4. self-verify inside that transaction (row counts, ``volume`` bit-identical,
     per-row OHLC equal to the planned values, traded-value ratio == factor);
  5. ``ROLLBACK`` on any failure — the database must be left byte-identical;
  6. reopen read-only, re-run the census, and exit non-zero if any damage block is
     still visible (a narrowed ``--apply`` is therefore never reported as success).
  The backup is **never** deleted by this script, and a failed apply keeps it too.

EXIT CODES
----------
``0``  dry-run: the census damage set is empty (nothing to repair) / apply: applied and
       re-censused with zero residual damage
``1``  dry-run: actionable plan / plan completed with residual damage / apply: residual
       damage remains, or the census could not be certified (no ``--baseline``) /
       refused by a guard (nothing written)
``2``  usage / data error, or apply failed and rolled back

USAGE
-----
    # dry-run, the recipe quoted in docs/version.md §14 (scope = every symbol of the
    # provider, i.e. exactly the §15 census damage set)
    uv run --offline --project pipeline python scripts/repair_qfq_alignment.py \\
        --baseline data/cache.duckdb.bak \\
        --additional-baseline data/cache.duckdb.bak.20261022-213155

    # deliberately narrow (prints the residual damage list and exits 1 while any
    # damage block outside the named symbols is still there)
    uv run --offline --project pipeline python scripts/repair_qfq_alignment.py \\
        --baseline data/cache.duckdb.bak --symbol 600660.SH

    # apply onto a COPY (never the production cache)
    uv run --offline --project pipeline python scripts/repair_qfq_alignment.py \\
        --db /tmp/opencode/cache_copy.duckdb --apply
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.audit_qfq_alignment import (  # noqa: E402  (path bootstrap must run first)
    _PRICE_COLS,
    _iso,
    census,
    connect_read_only,
    detect_blocks,
    detect_cross_provider_seams,
    judge_rewrite_direction,
    load_bars,
    seam_windows,
)

_DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
_DEFAULT_PROVIDER = "FuyaoDataProvider"
_DEFAULT_REFERENCE_PROVIDER = "MarketDBDataProvider"
#: The two symbols of the original §14 recipe. **Not** the default scope any more: the
#: default is every symbol of ``--provider`` (the §15 census universe), because planning
#: only these two left three damaged blocks unfixed while the follow-up dry-run said
#: "nothing to repair" (docs/version.md §14/§15). Kept as the documented narrow example
#: and as the "old scope" the regression test contrasts against.
_DEFAULT_SYMBOLS = ("600362.SH", "600660.SH")
#: Every symbol the whole-universe census (docs/version.md §15) found rewritten. The
#: CLI default stays the two §14 objects so the documented recipe is unchanged; pass
#: ``--all-symbols`` (or these names explicitly) for the full set.
_CENSUS_SYMBOLS = ("600660.SH", "600362.SH", "601225.SH", "601288.SH", "601398.SH")
#: Snapshot filenames next to the cache, oldest first. They are **not** interchangeable:
#: ``.bak`` (2026-10-21 15:12) is the last snapshot *before* the bank re-basing, while
#: ``.bak.20261022-213155`` already contains it — using only the latter silently misses
#: three of the five damaged symbols.
_DEFAULT_BASELINES = (
    os.path.join(_REPO_ROOT, "data", "cache.duckdb.bak"),
    os.path.join(_REPO_ROOT, "data", "cache.duckdb.bak.20261022-213155"),
)
_TABLE = "ohlcv_bars"

EXIT_OK = 0
EXIT_ACTIONABLE = 1
EXIT_ERROR = 2
#: Same value as ``EXIT_ACTIONABLE``, different meaning: the run did what the plan said,
#: but the whole-universe census still sees damage. Named separately so a caller reads
#: "not fully repaired" instead of "there is a plan to run".
EXIT_RESIDUAL = 1

#: Tolerance for "the value was not touched": float64 ULP-level rewrites pass.
ULP_TOL = 1e-12
#: Tolerance for "the written value equals the planned value".
WRITE_TOL = 1e-12


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class RepairOp:
    """One atomic repair operation on one (provider, symbol) series."""

    symbol: str
    provider: str
    kind: str  # "restore" | "rebase"
    start: datetime.date
    end: datetime.date
    rows: int
    factor: float = 1.0
    source_provider: Optional[str] = None
    #: ``"baseline"`` (bit-exact pre-incident snapshot of the same provider) or
    #: ``"reference"`` (the independent namespace). Baseline wins when it covers the
    #: window, because it is the only source that restores the original bits.
    source_kind: str = "reference"
    #: The session right after the repaired range — the seam the op is closing.
    seam_date: Optional[datetime.date] = None
    seam_before_pp: float = 0.0
    seam_after_pp: float = 0.0
    boundary_return_before_pp: Optional[float] = None
    boundary_return_after_pp: Optional[float] = None
    note: str = ""
    #: Sum of ``close * volume`` the repaired range must have after the write. Pinned
    #: at plan time because a repair legitimately changes it: mixing the post-write
    #: value with a factor measured elsewhere is how a re-alignment goes unnoticed.
    expected_traded_value: Optional[float] = None
    #: Expected post-write values, keyed by date. In-memory only, used by the
    #: in-transaction self-check.
    expected: Dict[datetime.date, Tuple[float, float, float, float]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "kind": self.kind,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "rows": self.rows,
            "factor": round(self.factor, 12),
            "source_provider": self.source_provider,
            "source_kind": self.source_kind,
            "seam_date": _iso(self.seam_date),
            "seam_before_pp": round(self.seam_before_pp, 6),
            "seam_after_pp": round(self.seam_after_pp, 6),
            "boundary_return_before_pp": (
                None if self.boundary_return_before_pp is None
                else round(self.boundary_return_before_pp, 6)
            ),
            "boundary_return_after_pp": (
                None if self.boundary_return_after_pp is None
                else round(self.boundary_return_after_pp, 6)
            ),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Scope model — "what the census says is damaged" vs. "what this plan covers"
# ---------------------------------------------------------------------------


@dataclass
class DamageBlock:
    """One damage block as the §15 census judges it — the authority for the scope.

    Deliberately *not* the same object as `RepairOp`: a block says "this range is off
    the reference", an op says "this is the write that fixes it". Keeping them apart is
    what lets a narrowed plan prove — rather than assume — that it left something
    behind, and lets a `rebase` op (which fixes a stale prefix no block describes) and
    a `restore` op (one per block) coexist without pretending they are 1:1.
    """

    symbol: str
    start: datetime.date
    end: datetime.date
    rows: int
    kind: str
    baseline: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "rows": self.rows,
            "kind": self.kind,
            "baseline": self.baseline,
        }


def list_symbols(con: duckdb.DuckDBPyConnection, provider: str = _DEFAULT_PROVIDER) -> List[str]:
    """Every symbol stored for ``provider`` — the default repair scope, sorted."""
    return [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT symbol FROM {_TABLE} WHERE provider = ? ORDER BY 1",
            [provider],
        ).fetchall()
    ]


def find_damage_blocks(
    con: duckdb.DuckDBPyConnection,
    baselines: Sequence[Tuple[str, duckdb.DuckDBPyConnection]],
    provider: str = _DEFAULT_PROVIDER,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    symbols: Optional[Sequence[str]] = None,
) -> List[DamageBlock]:
    """The whole-universe damage set, judged exactly as ``audit --census`` judges it.

    Every supplied snapshot is scanned and the first sighting of a
    ``(symbol, start, end)`` block wins — the same rule the census CLI uses, because the
    oldest snapshot is the one that proves what was changed when. Passing no baseline
    yields an empty list, which is *not* "clean": callers must treat that as
    "uncertifiable" (see `run`), never as "nothing to repair".
    """
    out: List[DamageBlock] = []
    seen: set = set()
    for label, con_base in baselines:
        for r in census(
            con,
            con_base,
            provider=provider,
            reference_provider=reference_provider,
            symbols=None if symbols is None else list(symbols),
            baseline_label=label,
        ):
            if r.direction != "damage":
                continue
            key = (r.symbol, r.start, r.end)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                DamageBlock(
                    symbol=r.symbol,
                    start=r.start,
                    end=r.end,
                    rows=r.rows,
                    kind=r.kind,
                    baseline=label,
                )
            )
    return out


def residual_damage(
    ops: Sequence[RepairOp], damage: Sequence[DamageBlock]
) -> List[DamageBlock]:
    """Damage blocks the plan does not cover — the honest half of a narrowed run.

    A block counts as covered only when an op on the **same symbol** spans it entirely:
    a partially covered block is exactly the "repaired half of it" state that reads as
    success while the series still has a seam in the middle.
    """
    return [
        b
        for b in damage
        if not any(
            op.symbol == b.symbol and op.start <= b.start and op.end >= b.end for op in ops
        )
    ]


# ---------------------------------------------------------------------------
# Guards -- the "no windowed re-basing" rule as executable checks
# ---------------------------------------------------------------------------


class RepairRefused(RuntimeError):
    """A repair was refused by a hard guard; nothing was written."""


def assert_append_only(write_start: datetime.date, existing_max: datetime.date) -> None:
    """Refuse any write whose first row is at or before the symbol's last bar.

    This is the ordinary **sync** contract: appending may only add sessions strictly
    after what is already stored (the shape `patch_equity_daily_bars` must obey).
    A repair that intentionally rewrites history has to go through
    `assert_single_basis_prefix` / `assert_restore_is_reference_exact` instead — those
    accept only the two shapes that cannot be produced by an accidental windowed
    realignment.
    """
    if write_start <= existing_max:
        raise RepairRefused(
            f"append-only contract violated: first row {write_start} <= existing "
            f"MAX(date) {existing_max}. A historical rewrite must be declared as a "
            f"'rebase' (prefix) or 'restore' (verified reference window) op."
        )


def assert_single_basis_prefix(
    op: RepairOp,
    series_min: datetime.date,
    series_max: datetime.date,
    seam_date: datetime.date,
) -> None:
    """A `rebase` op must be the whole prefix, on one basis, up to the seam."""
    if op.kind != "rebase":
        raise RepairRefused(f"{op.symbol}: expected a rebase op, got {op.kind!r}")
    if op.start != series_min:
        raise RepairRefused(
            f"{op.symbol}: rebase must start at the symbol's first bar {series_min}, "
            f"got {op.start} — a rebase that starts mid-history is a windowed "
            f"re-alignment (the exact defect being repaired)"
        )
    if op.end >= seam_date:
        raise RepairRefused(
            f"{op.symbol}: rebase must end before the seam {seam_date}, got {op.end}"
        )
    if not (0.5 < op.factor < 1.5) or op.factor == 1.0:
        raise RepairRefused(f"{op.symbol}: implausible rebase factor {op.factor!r}")
    if op.end < series_min:
        raise RepairRefused(f"{op.symbol}: empty rebase range")


def assert_restore_is_reference_exact(
    op: RepairOp,
    target: pd.DataFrame,
    reference: pd.DataFrame,
    window: Tuple[datetime.date, datetime.date],
) -> None:
    """A `restore` op must be a window whose values come verbatim from the reference.

    Three conditions, all necessary for this to be a *restoration* rather than a
    fresh re-alignment: the reference must cover the whole window; the two series
    must agree (bit-level) everywhere outside it; and they must disagree inside it
    (otherwise there is nothing to repair).
    """
    if op.kind != "restore":
        raise RepairRefused(f"{op.symbol}: expected a restore op, got {op.kind!r}")
    if op.start != window[0] or op.end != window[1]:
        raise RepairRefused(
            f"{op.symbol}: restore window {op.start}..{op.end} does not match the "
            f"detected seam-bounded window {window[0]}..{window[1]}"
        )
    if reference.empty:
        raise RepairRefused(f"{op.symbol}: no reference rows, cannot restore")
    ref_dates = set(reference["date"])
    missing = [d for d in op.expected if d not in ref_dates]
    if missing:
        raise RepairRefused(
            f"{op.symbol}: reference namespace does not cover {len(missing)} window "
            f"row(s) (first: {_iso(missing[0])})"
        )
    outside = target.merge(reference, on="date", suffixes=("_t", "_r"))
    outside = outside[(outside["date"] < op.start) | (outside["date"] > op.end)]
    if not outside.empty:
        for col in _PRICE_COLS:
            rel = (outside[f"{col}_t"] - outside[f"{col}_r"]).abs() / outside[f"{col}_r"].abs()
            if float(rel.max()) > ULP_TOL:
                raise RepairRefused(
                    f"{op.symbol}: target and reference disagree by "
                    f"{float(rel.max()):.3e} OUTSIDE the restore window — the reference "
                    f"is not on the same basis, refusing to overwrite"
                )
    inside = target.merge(reference, on="date", suffixes=("_t", "_r"))
    inside = inside[(inside["date"] >= op.start) & (inside["date"] <= op.end)]
    if inside.empty or float(
        ((inside["close_t"] - inside["close_r"]).abs() / inside["close_r"].abs()).max()
    ) <= ULP_TOL:
        raise RepairRefused(f"{op.symbol}: target already equals the reference inside the window")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _first_and_last_session(df: pd.DataFrame) -> Tuple[datetime.date, datetime.date]:
    return df["date"].iloc[0], df["date"].iloc[-1]


def _max_rel_close_diff(a: pd.DataFrame, b: pd.DataFrame) -> float:
    """Largest relative close difference between two series on their shared sessions."""
    if a.empty or b.empty:
        return float("inf")
    m = a.merge(b, on="date", suffixes=("_a", "_b"))
    if m.empty:
        return float("inf")
    denom = m["close_b"].abs().where(m["close_b"].abs() > 0)
    return float(((m["close_a"] - m["close_b"]).abs() / denom).max())


def build_plan(
    con: duckdb.DuckDBPyConnection,
    con_baseline: Optional[duckdb.DuckDBPyConnection],
    symbols: Sequence[str],
    provider: str = _DEFAULT_PROVIDER,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    extra_baselines: Optional[Sequence[Tuple[str, duckdb.DuckDBPyConnection]]] = None,
    notes: Optional[List[str]] = None,
    baseline_label: str = "baseline",
) -> List[RepairOp]:
    """Derive the minimal repair plan for each symbol (read-only).

    Exactly **one** op per symbol: the census (§15) showed that a symbol whose history
    was rewritten twice shows up as a single non-constant block, and repairing it is
    still one verbatim restore from the oldest snapshot that still holds the bits.
    ``notes`` (when given) collects why a *considered* symbol was left alone, so a
    whole-universe dry-run can show its work instead of a suspiciously short list.
    """
    ops: List[RepairOp] = []
    for sym in symbols:
        target = load_bars(con, provider, sym)
        if target.empty:
            continue
        reference = load_bars(con, reference_provider, sym)
        # Ordered, labelled snapshots: ``baselines[0]`` drives the prefix-rebase path
        # (it needs *one* unambiguous written block), every entry is searched for a
        # bit-exact restore source.
        baselines: List[Tuple[str, pd.DataFrame]] = []
        for label, c in [(baseline_label, con_baseline)] + list(extra_baselines or []):
            if c is None:
                continue
            df = load_bars(c, provider, sym)
            if not df.empty:
                baselines.append((label, df))
        baseline = baselines[0][1] if baselines else pd.DataFrame()

        # 1) Restoration candidate: a bounded window where the target deviates from an
        #    independent namespace that agrees with it everywhere outside.
        seams = detect_cross_provider_seams(target, reference, sym, provider, reference_provider)
        windows = seam_windows(seams)
        if len(windows) > 1:
            # Repairing several disjoint windows on one symbol needs a per-window plan
            # and a per-window self-check; refusing is honest, guessing is not.
            raise RepairRefused(
                f"{sym}: {len(windows)} bounded windows detected "
                f"({', '.join(f'{_iso(a)}..{_iso(b)}' for a, b, _ in windows)}); "
                f"this writer repairs one window per symbol — narrow it with --symbol "
                f"after a manual review"
            )
        if len(windows) == 1 and len(seams) == 2:
            w_start, w_end, offset_pp = windows[0]
            window_rows = reference[(reference["date"] >= w_start) & (reference["date"] <= w_end)]
            if not window_rows.empty:
                # Prefer the pre-incident snapshot of the *same* provider when it covers
                # the window: it is the only source that restores the original bits
                # (the independent namespace is 1-2 ULP away, which is irrelevant for a
                # 1.86 % bias but means "restored" would be a slight overstatement).
                source_rows, source_kind, source_label = window_rows, "reference", reference_provider
                for label, base_df in baselines:
                    base_window = base_df[
                        (base_df["date"] >= w_start) & (base_df["date"] <= w_end)
                    ]
                    if len(base_window) != len(window_rows):
                        continue
                    if _max_rel_close_diff(base_window, target) <= ULP_TOL:
                        # The snapshot *equals the live series* inside the window, so this
                        # window was never rewritten. The Fuyao↔reference step is a
                        # long-standing basis difference (proved on 300394.SZ, which shows
                        # a 0.186 % closed window in both snapshots), not damage — writing
                        # the reference values over it would invent a repair.
                        if notes is not None:
                            notes.append(
                                f"{sym}: cross-provider window {_iso(w_start)}..{_iso(w_end)} "
                                f"({offset_pp:+.4f} pp) is identical in {label} — a "
                                f"pre-existing basis gap, not a rewrite; skipped"
                            )
                        window_rows = pd.DataFrame()
                        break
                    source_rows, source_kind = base_window, "baseline"
                    source_label = f"{label} (bit-exact)"
                    break
                if window_rows.empty:
                    continue
                expected = {
                    r["date"]: (r["open"], r["high"], r["low"], r["close"])
                    for _, r in source_rows.iterrows()
                }
                op = RepairOp(
                    symbol=sym,
                    provider=provider,
                    kind="restore",
                    start=w_start,
                    end=w_end,
                    rows=len(expected),
                    factor=float(1.0 / (1.0 + offset_pp / 100.0)),
                    source_provider=source_label,
                    source_kind=source_kind,
                    seam_date=w_end + datetime.timedelta(days=1),
                    seam_before_pp=abs(offset_pp),
                    seam_after_pp=0.0,
                    note=(
                        "window sits "
                        f"{offset_pp:+.4f} pp off the reference level; values restored "
                        f"verbatim from {source_label}"
                    ),
                    expected=expected,
                    expected_traded_value=float(
                        (source_rows["close"] * source_rows["volume"]).sum()
                    ),
                )
                assert_restore_is_reference_exact(op, target, source_rows, (w_start, w_end))
                ops.append(op)
                continue

        # 2) Prefix-rebase candidate: an earlier scalar realignment left the prefix on a
        #    stale basis. The factor is *measured* from the baseline snapshot, never
        #    hardcoded and never taken from an upstream table we cannot verify offline.
        #    The direction is the precondition, not an afterthought: rebasing the prefix
        #    is only correct when the *block* is the side that matches the reference
        #    (600362.SH). If the reference says the block is the deviating side, or says
        #    nothing at all, the prefix must not be touched.
        if not baseline.empty:
            baseline_label = baselines[0][0]
            blocks = detect_blocks(target, baseline, sym, provider)
            material = [b for b in blocks if b.kind == "material_rescale"]
            direction = "undetermined"
            if len(material) == 1 and material[0].bounded:
                direction, n_ref, _, _, _ = judge_rewrite_direction(
                    target, baseline, reference, material[0].start, material[0].end
                )
            if len(material) == 1 and material[0].bounded and direction == "fix":
                block = material[0]
                factor = float(block.ratios["close"])
                series_min, series_max = _first_and_last_session(target)
                prefix = target[target["date"] < block.start]
                if not prefix.empty:
                    corrected = prefix.copy()
                    corrected[list(_PRICE_COLS)] = corrected[list(_PRICE_COLS)].values * factor
                    expected = {
                        r["date"]: (r["open"], r["high"], r["low"], r["close"])
                        for _, r in corrected.iterrows()
                    }
                    boundary_before, boundary_after = _boundary_returns(
                        target, baseline, block.start
                    )
                    # The injected step IS the difference between the contaminated
                    # boundary return and the basis-free one measured on the baseline.
                    injected_pp = abs(boundary_after - boundary_before)
                    op = RepairOp(
                        symbol=sym,
                        provider=provider,
                        kind="rebase",
                        start=series_min,
                        end=prefix["date"].iloc[-1],
                        rows=len(expected),
                        factor=factor,
                        source_provider=f"{baseline_label} (measured factor)",
                        source_kind="baseline",
                        seam_date=block.start,
                        seam_before_pp=injected_pp,
                        seam_after_pp=0.0,
                        boundary_return_before_pp=boundary_before,
                        boundary_return_after_pp=boundary_after,
                        note=(
                            f"prefix still on the stale basis: ×{factor:.10f} measured from "
                            f"the baseline snapshot, restoring the basis-free boundary "
                            f"return {boundary_before:+.4f} pp -> {boundary_after:+.4f} pp"
                        ),
                        expected=expected,
                        expected_traded_value=float(
                            (corrected["close"] * corrected["volume"]).sum()
                        ),
                    )
                    assert_single_basis_prefix(op, series_min, series_max, block.start)
                    ops.append(op)
                    continue

        # 3) Damage-block restore (census shape, §15): the same write also rescaled
        #    *open-ended* stretches — 2024-01-02 up to the newest stored bar — where no
        #    seam test can bound the window because the reference namespace starts later
        #    (MarketDB begins 2024-07-22). The direction is judged on the overlap against
        #    that reference: a write that moved a reference-matching series *off* the
        #    reference is undone by restoring the pre-incident bits verbatim; a write
        #    that moved it *onto* the reference is the fix and is left alone.
        for label, base_df in baselines:
            fixed = False
            for b in detect_blocks(target, base_df, sym, provider):
                if b.kind not in ("material_rescale", "rewrite"):
                    continue
                direction, n_ref, r_old, r_new, reason = judge_rewrite_direction(
                    target, base_df, reference, b.start, b.end
                )
                if direction != "damage":
                    if notes is not None:
                        notes.append(
                            f"{sym}: {b.kind} block {_iso(b.start)}..{_iso(b.end)} vs "
                            f"{label} judged {direction!r}, not repaired ({reason})"
                        )
                    continue
                base_window = base_df[
                    (base_df["date"] >= b.start) & (base_df["date"] <= b.end)
                ]
                if base_window.empty or len(base_window) != b.rows:
                    raise RepairRefused(
                        f"{sym}: {label} covers only {len(base_window)} of the {b.rows} "
                        f"rewritten bar(s) {_iso(b.start)}..{_iso(b.end)}; a partial "
                        f"restore would leave a seam inside the block"
                    )
                expected = {
                    r["date"]: (r["open"], r["high"], r["low"], r["close"])
                    for _, r in base_window.iterrows()
                }
                op = RepairOp(
                    symbol=sym,
                    provider=provider,
                    kind="restore",
                    start=b.start,
                    end=b.end,
                    rows=len(expected),
                    factor=float(1.0 / b.ratios["close"]),
                    source_provider=f"{label} (bit-exact)",
                    source_kind="baseline",
                    seam_date=b.start,
                    seam_before_pp=abs(
                        b.boundary_distortion_pp.get(
                            "left_pp", abs(1.0 / b.ratios["close"] - 1.0) * 100.0
                        )
                    ),
                    seam_after_pp=0.0,
                    note=(
                        f"{b.kind} block {_iso(b.start)}..{_iso(b.end)} is {abs(r_new - 1.0) * 100:.4f} % "
                        f"off the reference while the pre-incident level was "
                        f"{abs(r_old - 1.0) * 100:.4f} % off it ({n_ref} overlapping "
                        f"session(s)); values restored verbatim from {label}"
                    ),
                    expected=expected,
                    expected_traded_value=float(
                        (base_window["close"] * base_window["volume"]).sum()
                    ),
                )
                assert_restore_is_reference_exact(op, target, base_window, (b.start, b.end))
                ops.append(op)
                fixed = True
                break
            if fixed:
                break
    return ops


def _boundary_returns(
    target: pd.DataFrame, baseline: pd.DataFrame, seam: datetime.date
) -> Tuple[float, float]:
    """Return at the seam before (as cached) and after (as it was pre-incident).

    The bar before the seam and the seam bar are on *different* bases in the cached
    series, so their ratio is contaminated. In the baseline both were on the same
    (stale) basis, so their ratio is basis-free — that is the value the repair must
    restore, and it is measured, not assumed.
    """
    def _ret(df: pd.DataFrame) -> Optional[float]:
        idx = df.index[df["date"] == seam]
        if len(idx) == 0 or int(idx[0]) == 0:
            return None
        i = int(idx[0])
        return (df["close"].iloc[i] / df["close"].iloc[i - 1] - 1.0) * 100.0

    before = _ret(target)
    after = _ret(baseline)
    return (0.0 if before is None else before), (0.0 if after is None else after)


# ---------------------------------------------------------------------------
# Repair preview (what --apply would do)
# ---------------------------------------------------------------------------


def apply_expected(target: pd.DataFrame, expected: Dict[datetime.date, Tuple[float, ...]]) -> pd.DataFrame:
    """Return ``target`` with the planned OHLC substituted, vectorised."""
    exp = pd.DataFrame.from_dict(expected, orient="index", columns=list(_PRICE_COLS))
    aligned = exp.reindex(pd.Index(target["date"]))
    mask = aligned["close"].notna().to_numpy()
    fixed = target.copy()
    for col in _PRICE_COLS:
        fixed.loc[mask, col] = aligned[col].to_numpy()[mask]
    return fixed


def _edge_returns(df: pd.DataFrame, date: datetime.date) -> Optional[float]:
    """Close-to-close return (pp) ending on ``date``; ``None`` at the series' first bar."""
    idx = df.index[df["date"] == date]
    if len(idx) == 0 or int(idx[0]) == 0:
        return None
    i = int(idx[0])
    return float(df["close"].iloc[i] / df["close"].iloc[i - 1] - 1.0) * 100.0


def edge_residuals(
    target: pd.DataFrame,
    fixed: pd.DataFrame,
    source: pd.DataFrame,
    start: datetime.date,
    end: datetime.date,
) -> Dict[str, Any]:
    """The internal seam test: does the repaired bar look like the pre-incident one?

    A cross-provider seam can only be measured where the reference namespace has
    coverage, which is precisely *not* the case for the open-ended blocks of §15 (the
    reference starts 2024-07-22, the block starts 2024-01-02). So the repaired block's
    two edges are checked **against the source snapshot's own returns**: those are
    basis-free, and a correct repair must reproduce them to rounding (≤1e-9 pp).
    """
    left = {"before": _edge_returns(target, start), "after": _edge_returns(fixed, start),
            "source": _edge_returns(source, start)}
    after_end = target.index[target["date"] == end]
    right: Dict[str, Any] = {"before": None, "after": None, "source": None}
    if len(after_end):
        i = int(after_end[0]) + 1
        for label, df in (("before", target), ("after", fixed)):
            if i < len(df):
                right[label] = float(df["close"].iloc[i] / df["close"].iloc[i - 1] - 1.0) * 100.0
        j = source.index[source["date"] == end]
        if len(j) and int(j[0]) + 1 < len(source):
            k = int(j[0]) + 1
            right["source"] = float(
                source["close"].iloc[k] / source["close"].iloc[k - 1] - 1.0
            ) * 100.0
    out: Dict[str, Any] = {}
    for side, vals in (("left", left), ("right", right)):
        residual = (
            None if vals["after"] is None or vals["source"] is None
            else abs(vals["after"] - vals["source"])
        )
        out[f"{side}_return_before_pp"] = vals["before"]
        out[f"{side}_return_after_pp"] = vals["after"]
        out[f"{side}_return_source_pp"] = vals["source"]
        out[f"{side}_residual_pp"] = residual
    return out


def _conn_list(con_baseline: Any) -> List[duckdb.DuckDBPyConnection]:
    """Normalise the single-connection and multi-snapshot call shapes."""
    if con_baseline is None:
        return []
    if isinstance(con_baseline, (list, tuple)):
        return [c for c in con_baseline if c is not None]
    return [con_baseline]


def _expected_matches(df: pd.DataFrame, expected: Dict[datetime.date, Tuple[float, ...]]) -> bool:
    """True when ``df`` holds the planned OHLC bits on every planned date."""
    if df.empty or not expected:
        return False
    have = {
        r["date"]: (r["open"], r["high"], r["low"], r["close"]) for _, r in df.iterrows()
    }
    for d, want in expected.items():
        got = have.get(d)
        if got is None or tuple(float(v) for v in got) != tuple(float(v) for v in want):
            return False
    return True


def source_for_baseline_op(
    cons: Any, op: RepairOp
) -> Optional[pd.DataFrame]:
    """The supplied snapshot that actually holds the planned bits (or ``None``).

    Matching on values rather than on the filename is deliberate: a restore is only
    admissible if some snapshot *reproduces the plan*, and a stale ``--baseline`` must
    fail loudly instead of silently becoming the source of a different basis.
    """
    for con in _conn_list(cons):
        df = load_bars(con, op.provider, op.symbol)
        if _expected_matches(df, op.expected):
            return df
    return None


def repair_preview(
    con: duckdb.DuckDBPyConnection,
    ops: Sequence[RepairOp],
    provider: str = _DEFAULT_PROVIDER,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    baselines: Sequence[Tuple[str, duckdb.DuckDBPyConnection]] = (),
) -> List[Dict[str, Any]]:
    """Apply the plan *in memory* and report what changes (no writes)."""
    out: List[Dict[str, Any]] = []
    for op in ops:
        target = load_bars(con, provider, op.symbol)
        reference = load_bars(con, reference_provider, op.symbol)
        fixed = apply_expected(target, op.expected)
        rows = target[(target["date"] >= op.start) & (target["date"] <= op.end)]
        fixed_rows = fixed[(fixed["date"] >= op.start) & (fixed["date"] <= op.end)]
        ratio = float((fixed_rows["close"] / rows["close"]).median())
        seams_after = detect_cross_provider_seams(
            fixed, reference, op.symbol, provider, reference_provider
        )
        windows_after = seam_windows(seams_after)
        # The basis-free series the repaired edges must reproduce: the snapshot the
        # values came from, or the reference namespace for a reference restore.
        if op.source_kind == "baseline":
            source = source_for_baseline_op([c for _, c in baselines], op)
        else:
            source = reference
        edges = (
            edge_residuals(target, fixed, source, op.start, op.end)
            if source is not None and not source.empty
            else {}
        )
        # A rebase's seam sits *outside* its rewritten range (the op ends on the bar
        # before the seam), so its residual is measured directly against the basis-free
        # return the plan measured on the snapshot.
        if op.kind == "rebase" and op.seam_date is not None and op.boundary_return_after_pp is not None:
            seam_now = _edge_returns(fixed, op.seam_date)
            edges = dict(edges)
            edges["seam_return_after_pp"] = seam_now
            edges["seam_residual_pp"] = (
                None if seam_now is None
                else abs(seam_now - op.boundary_return_after_pp)
            )
        out.append(
            {
                "op": op,
                "rows": len(rows),
                "factor_observed": ratio,
                "max_rel_change": float(
                    ((fixed_rows["close"] - rows["close"]).abs() / rows["close"]).max()
                ),
                "volume_unchanged": bool(
                    (fixed_rows["volume"].values == rows["volume"].values).all()
                ),
                "seams_after": len(seams_after),
                "bounded_windows_after": len(windows_after),
                "seam_after_pp": (
                    0.0 if not seams_after
                    else max(abs(s.step_pp) for s in seams_after)
                ),
                "seam_after_dates": [
                    f"{_iso(s.date_before)}→{_iso(s.date_after)} {s.step_pp:+.4f}"
                    for s in seams_after
                ],
                "edges": edges,
                "head": _rows_table(rows, fixed_rows, head=True),
                "tail": _rows_table(rows, fixed_rows, head=False),
            }
        )
    return out


def _rows_table(before: pd.DataFrame, after: pd.DataFrame, head: bool, n: int = 3) -> List[Dict[str, Any]]:
    b = before.head(n) if head else before.tail(n)
    a = after.head(n) if head else after.tail(n)
    rows = []
    for (_, rb), (_, ra) in zip(b.iterrows(), a.iterrows()):
        rows.append(
            {
                "date": _iso(rb["date"]),
                "close_before": float(rb["close"]),
                "close_after": float(ra["close"]),
                "ratio": float(ra["close"] / rb["close"]) if rb["close"] else float("nan"),
                "volume_same": bool(rb["volume"] == ra["volume"]),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Apply (guarded, transactional, self-verified)
# ---------------------------------------------------------------------------


def _snapshot(con: duckdb.DuckDBPyConnection, ops: Sequence[RepairOp]) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    """Per-series coverage + per-repair-range turnover, measured on disk.

    Two scopes on purpose: the whole-series shape (`count`/`min`/`max`) must be
    invariant, while `close * volume` has to be compared *inside* the repaired range —
    a series-wide sum would be dominated by the untouched rows and is not a check.
    """
    snap: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for op in ops:
        key = (op.provider, op.symbol, op.start, op.end)
        row = con.execute(
            f"SELECT count(*), min(date), max(date) FROM {_TABLE} "
            f"WHERE provider = ? AND symbol = ?",
            [op.provider, op.symbol],
        ).fetchone()
        rng = con.execute(
            f"SELECT count(*), sum(volume), sum(close * volume) FROM {_TABLE} "
            f"WHERE provider = ? AND symbol = ? AND date BETWEEN ? AND ?",
            [op.provider, op.symbol, op.start, op.end],
        ).fetchone()
        snap[key] = {
            "count": int(row[0]),
            "min_date": row[1],
            "max_date": row[2],
            "range_count": int(rng[0]),
            "range_volume": float(rng[1]) if rng[1] is not None else 0.0,
            "range_traded_value": float(rng[2]) if rng[2] is not None else 0.0,
        }
    return snap


def _self_check(
    con: duckdb.DuckDBPyConnection,
    ops: Sequence[RepairOp],
    before: Dict[Tuple[Any, ...], Dict[str, Any]],
) -> None:
    """In-transaction verification. Any failure must abort the transaction.

    Module-level on purpose: the rollback test monkeypatches it to raise.
    """
    after = _snapshot(con, ops)
    for op in ops:
        key = (op.provider, op.symbol, op.start, op.end)
        b, a = before[key], after[key]
        if a["count"] != b["count"]:
            raise RuntimeError(f"{op.symbol}: row count changed {b['count']} -> {a['count']}")
        if (a["min_date"], a["max_date"]) != (b["min_date"], b["max_date"]):
            raise RuntimeError(f"{op.symbol}: coverage changed, refusing")
        if a["range_count"] != op.rows:
            raise RuntimeError(
                f"{op.symbol}: repaired range holds {a['range_count']} rows, planned {op.rows}"
            )
        if a["range_volume"] != b["range_volume"]:
            raise RuntimeError(
                f"{op.symbol}: volume changed inside the range "
                f"({b['range_volume']} -> {a['range_volume']}) — "
                f"a re-alignment must never touch turnover"
            )
        if op.expected_traded_value is not None:
            got = a["range_traded_value"]
            want = op.expected_traded_value
            denom = max(abs(want), 1e-12)
            if abs(got - want) / denom > 1e-9:
                raise RuntimeError(
                    f"{op.symbol}: repaired traded value {got!r} != planned {want!r} "
                    f"(relative error {abs(got - want) / denom:.3e})"
                )
        got = con.execute(
            f"SELECT date, open, high, low, close FROM {_TABLE} "
            f"WHERE provider = ? AND symbol = ? AND date BETWEEN ? AND ?",
            [op.provider, op.symbol, op.start, op.end],
        ).fetchall()
        if len(got) != op.rows:
            raise RuntimeError(f"{op.symbol}: wrote {len(got)} rows, planned {op.rows}")
        for d, o, h, low, c in got:
            want = op.expected.get(d if isinstance(d, datetime.date) else d.date())
            if want is None:
                raise RuntimeError(f"{op.symbol}: unexpected row {d} in the written range")
            for got_v, want_v in zip((o, h, low, c), want):
                if abs(got_v - want_v) > max(WRITE_TOL, abs(want_v) * 1e-12):
                    raise RuntimeError(
                        f"{op.symbol} {d}: wrote {got_v!r}, planned {want_v!r}"
                    )


def _refuse_production_write(db_path: str) -> None:
    """Reuse the L2.13 gate: no pytest process may write into `<repo>/data`."""
    try:
        from common.duckdb_cache import refuse_production_cache_path
    except Exception:  # pragma: no cover - repository layout changed
        return
    refuse_production_cache_path(db_path, read_only=False)


def is_production_cache(db_path: str) -> bool:
    """True when ``db_path`` resolves inside this checkout's ``<repo>/data``."""
    data_dir = os.path.realpath(os.path.join(_REPO_ROOT, "data"))
    resolved = os.path.realpath(os.path.abspath(db_path))
    return resolved == data_dir or resolved.startswith(data_dir + os.sep)


def refuse_production_apply(db_path: str, allow_production: bool) -> None:
    """Refuse ``--apply`` on the live cache unless the operator says so explicitly.

    This is deliberately *not* the pytest gate: it holds in every process, so the
    only way to write the live cache is to ask for it by name. The dry-run default
    plus this refusal means an unattended invocation can never mutate production.
    """
    if is_production_cache(db_path) and not allow_production:
        raise RepairRefused(
            f"{db_path} is the production market cache. --apply refuses it by default; "
            f"repair a copy first (--db /tmp/opencode/cache_copy.duckdb), and only then "
            f"re-run with --i-know-this-is-production during the maintenance window."
        )


def admit_write(
    con: duckdb.DuckDBPyConnection,
    op: RepairOp,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    con_baseline: Any = None,
) -> None:
    """The write gate: re-validate an op against the *live* series before touching it.

    Two hard rules, both measured against what is already on disk rather than
    against what the plan claims:

    1. **No append-first write.** A write whose first row is strictly after
       ``MAX(date)`` is an append and belongs to the sync path; this repair writer
       refuses it (see `assert_append_only`). Every op admitted here therefore
       *is* a declared historical rewrite.
    2. **Declared shape only.** A rewrite must be either the symbol's complete
       prefix up to (but excluding) a real seam session, or a window that is
       bit-recoverable from the reference namespace. A closed interval that
       happens to sit mid-history — the incident's own footprint — is refused.
    """
    if op.kind not in ("rebase", "restore"):
        raise RepairRefused(f"{op.symbol}: unknown op kind {op.kind!r}")

    bounds = con.execute(
        f"SELECT min(date), max(date) FROM {_TABLE} WHERE provider = ? AND symbol = ?",
        [op.provider, op.symbol],
    ).fetchone()
    if bounds is None or bounds[0] is None:
        raise RepairRefused(f"{op.symbol}: no rows for provider {op.provider!r}")
    series_min, series_max = bounds
    if not isinstance(series_min, datetime.date):
        series_min, series_max = series_min.date(), series_max.date()

    if op.start > series_max:
        # Documents the sync contract and fails loudly: an append is never a repair.
        assert_append_only(op.start, series_max)
        raise RepairRefused(
            f"{op.symbol}: write starts after MAX(date) {series_max}; appends belong to "
            f"scripts/sync_china_5m_and_indices.py, not to this repair writer"
        )

    if op.kind == "rebase":
        if op.seam_date is None:
            raise RepairRefused(f"{op.symbol}: rebase has no seam date, refusing blind rewrite")
        assert_single_basis_prefix(op, series_min, series_max, op.seam_date)
        gap = con.execute(
            f"SELECT count(*) FROM {_TABLE} WHERE provider = ? AND symbol = ? "
            f"AND date > ? AND date < ?",
            [op.provider, op.symbol, op.end, op.seam_date],
        ).fetchone()[0]
        if gap:
            raise RepairRefused(
                f"{op.symbol}: {gap} bar(s) between the rebase end {op.end} and the seam "
                f"{op.seam_date} — the prefix must be gap-free up to the seam"
            )
        if not con.execute(
            f"SELECT count(*) FROM {_TABLE} WHERE provider = ? AND symbol = ? AND date >= ?",
            [op.provider, op.symbol, op.seam_date],
        ).fetchone()[0]:
            raise RepairRefused(f"{op.symbol}: seam {op.seam_date} is not a stored session")
        return

    # restore
    target = load_bars(con, op.provider, op.symbol)
    if op.source_kind == "baseline":
        cons = _conn_list(con_baseline)
        if not cons:
            raise RepairRefused(
                f"{op.symbol}: restore is sourced from the pre-incident snapshot, but no "
                f"baseline was supplied — refusing to substitute a different namespace"
            )
        source = source_for_baseline_op(cons, op)
        if source is None:
            raise RepairRefused(
                f"{op.symbol}: none of the {len(cons)} supplied snapshot(s) reproduces the "
                f"planned values — refusing to write a basis this writer cannot re-derive"
            )
        # Direction gate (census rule, docs/version.md §15): a verbatim restore from an
        # older snapshot is admissible only while the *live* series is the one that moved
        # off the reference. If the snapshot is the deviating side, the write under it was
        # a genuine alignment and undoing it would re-create the defect.
        reference = load_bars(con, reference_provider, op.symbol)
        direction, n_ref, r_old, r_new, reason = judge_rewrite_direction(
            target, source, reference, op.start, op.end
        )
        if direction != "damage":
            raise RepairRefused(
                f"{op.symbol}: direction is {direction!r}, not 'damage' — {reason}; "
                f"refusing to undo a write that did not corrupt the level"
            )
    else:
        if op.source_provider is None:
            raise RepairRefused(f"{op.symbol}: restore without a source namespace")
        source = load_bars(con, reference_provider, op.symbol)
    assert_restore_is_reference_exact(op, target, source, (op.start, op.end))


def apply_plan(
    db_path: str,
    ops: Sequence[RepairOp],
    backup_dir: Optional[str] = None,
    stamp: Optional[str] = None,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    allow_production: bool = False,
    baseline_path: Optional[str] = None,
    baseline_paths: Optional[Sequence[str]] = None,
) -> str:
    """Apply the plan: backup, one transaction, self-check, commit or rollback.

    Returns the backup path. Raises ``RepairRefused`` before touching anything when a
    guard rejects the plan, and ``RuntimeError`` after a rollback.
    """
    if not ops:
        raise RepairRefused("empty plan")
    refuse_production_apply(db_path, allow_production)
    _refuse_production_write(db_path)

    paths = list(baseline_paths or [])
    if baseline_path:
        paths.append(baseline_path)

    # Guards run *before* any file operation so a rejected plan cannot even leave a
    # backup behind, and again against the live series (not the plan's own claims).
    con_guard = connect_read_only(db_path)
    cons_base = [connect_read_only(p) for p in paths]
    try:
        for op in ops:
            admit_write(con_guard, op, reference_provider, cons_base)
    finally:
        con_guard.close()
        for c in cons_base:
            c.close()

    backup = os.path.join(
        backup_dir or os.path.dirname(os.path.abspath(db_path)),
        f"{os.path.basename(db_path)}.bak.{stamp or datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}",
    )
    os.makedirs(os.path.dirname(backup) or ".", exist_ok=True)
    try:
        shutil.copy2(db_path, backup)
    except OSError as exc:
        raise RepairRefused(
            f"could not create the backup {backup}: {exc}; nothing was written"
        ) from exc

    con = duckdb.connect(db_path, read_only=False)
    try:
        before = _snapshot(con, ops)
        con.execute("BEGIN TRANSACTION")
        try:
            _write_ops(con, ops)
            _self_check(con, ops, before)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    # Post-commit verification on a fresh read-only connection.
    con_ro = connect_read_only(db_path)
    try:
        for op in ops:
            key = (op.provider, op.symbol, op.start, op.end)
            after = _snapshot(con_ro, [op])[key]
            if after["count"] != before[key]["count"]:
                raise RuntimeError(f"{op.symbol}: post-commit row count mismatch")
    finally:
        con_ro.close()
    return backup


def _write_ops(con: duckdb.DuckDBPyConnection, ops: Sequence[RepairOp]) -> None:
    """One batch per op, all inside the caller's transaction."""
    for op in ops:
        if op.kind == "rebase":
            con.execute(
                f"UPDATE {_TABLE} SET open = open * ?, high = high * ?, low = low * ?, "
                f"close = close * ? WHERE provider = ? AND symbol = ? AND date BETWEEN ? AND ?",
                [op.factor, op.factor, op.factor, op.factor, op.provider, op.symbol, op.start, op.end],
            )
        else:
            con.execute(
                "CREATE OR REPLACE TEMP TABLE _repair_restore("
                "provider VARCHAR, symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, "
                "low DOUBLE, close DOUBLE)"
            )
            con.executemany(
                "INSERT INTO _repair_restore VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    [op.provider, op.symbol, d, *vals]
                    for d, vals in sorted(op.expected.items())
                ],
            )
            con.execute(
                f"UPDATE {_TABLE} SET open = r.open, high = r.high, low = r.low, close = r.close "
                f"FROM _repair_restore r "
                f"WHERE {_TABLE}.provider = r.provider AND {_TABLE}.symbol = r.symbol "
                f"AND {_TABLE}.date = r.date"
            )
            con.execute("DROP TABLE _repair_restore")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_num(v: Optional[float], spec: str) -> str:
    return "-" if v is None else format(v, spec)


def render_residual(blocks: Sequence[DamageBlock], ops: Sequence[RepairOp]) -> str:
    """The "what this plan does NOT fix" table (empty string when there is none)."""
    if not blocks:
        return ""
    planned = ", ".join(sorted({op.symbol for op in ops})) or "-"
    lines = [
        "",
        f"residual damage — 未修复的剩余致损块 ({len(blocks)}):",
        "",
        "| Symbol | Block | Start | End | Rows | Baseline |",
        "| :--- | :--- | :--- | :--- | ---: | :--- |",
    ]
    for b in blocks:
        lines.append(
            f"| `{b.symbol}` | {b.kind} | {_iso(b.start)} | {_iso(b.end)} | {b.rows} "
            f"| {b.baseline or '-'} |"
        )
    lines += [
        "",
        f"the plan only covered: {planned}. Re-run without --symbol (the default scope "
        f"is every symbol of --provider) to repair these too.",
    ]
    return "\n".join(lines)


def render_plan(ops: Sequence[RepairOp], previews: Sequence[Dict[str, Any]], db: str) -> str:
    lines = ["## QFQ repair plan (dry-run — nothing was written)", "", f"target: `{db}`", ""]
    if not ops:
        lines.append("plan: no repair op was derived for the requested scope.")
        return "\n".join(lines)
    lines.append(
        "| Symbol | Op | Start | End | Rows | Factor observed | Source | Evidence "
        "| Seam before | Seam after | Edge residual | Volume unchanged |"
    )
    lines.append(
        "| :--- | :--- | :--- | :--- | ---: | ---: | :--- | :--- | ---: | ---: | ---: | :---: |"
    )
    for p in previews:
        op: RepairOp = p["op"]
        edges = p.get("edges") or {}
        residual = max(
            [
                v
                for v in (
                    edges.get("left_residual_pp"),
                    edges.get("right_residual_pp"),
                    edges.get("seam_residual_pp"),
                )
                if v is not None
            ]
            or [float("nan")]
        )
        evidence = (
            "bit-exact" if op.source_kind == "baseline" and op.kind == "restore"
            else "measured factor" if op.kind == "rebase"
            else "reference namespace"
        )
        lines.append(
            "| `{}` | **{}** | {} | {} | {} | {:.10f} | {} | {} | {:.4f} pp | {:.4f} pp "
            "| {} | {} |".format(
                op.symbol,
                op.kind,
                _iso(op.start),
                _iso(op.end),
                op.rows,
                p["factor_observed"],
                op.source_provider or "-",
                evidence,
                op.seam_before_pp,
                p["seam_after_pp"],
                _fmt_num(residual, ".2e"),
                "yes" if p["volume_unchanged"] else "**NO**",
            )
        )
    for p in previews:
        op: RepairOp = p["op"]
        lines.append("")
        lines.append(f"### `{op.symbol}` {op.kind}: {_iso(op.start)} → {_iso(op.end)}")
        lines.append(f"- {op.note}")
        lines.append(
            f"- rows={p['rows']} max_rel_change={p['max_rel_change']:.3e} "
            f"seams after repair: {p['seams_after']} (bounded windows "
            f"{p['bounded_windows_after']})"
            + (
                " — " + "; ".join(p["seam_after_dates"])
                if p.get("seam_after_dates")
                else ""
            )
        )
        edges = p.get("edges") or {}
        if edges:
            lines.append(
                "- edge check (repaired return vs the source snapshot's own return): "
                f"left {_fmt_num(edges.get('left_return_before_pp'), '+.4f')} → "
                f"{_fmt_num(edges.get('left_return_after_pp'), '+.4f')} "
                f"(source {_fmt_num(edges.get('left_return_source_pp'), '+.4f')}, "
                f"residual {_fmt_num(edges.get('left_residual_pp'), '.2e')} pp); "
                f"right {_fmt_num(edges.get('right_return_before_pp'), '+.4f')} → "
                f"{_fmt_num(edges.get('right_return_after_pp'), '+.4f')} "
                f"(source {_fmt_num(edges.get('right_return_source_pp'), '+.4f')}, "
                f"residual {_fmt_num(edges.get('right_residual_pp'), '.2e')} pp)"
            )
            if "seam_residual_pp" in edges:
                lines.append(
                    "- seam check (the bar the rebase is closing): repaired return "
                    f"{_fmt_num(edges.get('seam_return_after_pp'), '+.4f')} pp vs the "
                    f"basis-free {_fmt_num(op.boundary_return_after_pp, '+.4f')} pp → "
                    f"residual {_fmt_num(edges.get('seam_residual_pp'), '.2e')} pp"
                )
        lines.append("")
        lines.append("| date | close before | close after | ratio | volume same |")
        lines.append("| :--- | ---: | ---: | ---: | :---: |")
        for label, rows in (("first 3", p["head"]), ("last 3", p["tail"])):
            for r in rows:
                lines.append(
                    f"| {r['date']} | {r['close_before']:.6f} | {r['close_after']:.6f} "
                    f"| {r['ratio']:.12f} | {'yes' if r['volume_same'] else '**no**'} |"
                )
            if label == "first 3":
                lines.append("| … | | | | |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Dry-run (default) and guarded apply for the QFQ alignment repair "
            "(docs/version.md §14). Dry-run never writes; --apply backs up first, "
            "writes in one transaction, self-verifies, and rolls back on failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=_DEFAULT_DB, help="cache to repair (dry-run: read-only)")
    p.add_argument("--baseline", default=None,
                   help=(
                       "pre-incident snapshot used to derive the rebase factor and to "
                       "source verbatim restores. Repeat with --additional-baseline for "
                       "snapshots of different ages: the banks' pre-incident state is only "
                       "in the older file " + os.path.basename(_DEFAULT_BASELINES[0])
                   ))
    p.add_argument("--additional-baseline", action="append", dest="additional_baselines",
                   default=None, metavar="PATH",
                   help="further snapshot, searched after --baseline (repeatable)")
    p.add_argument("--symbol", action="append", dest="symbols", default=None,
                   help=(
                       "NARROW the scope to these symbols (repeatable). This is the only "
                       "narrowing switch: the run then prints the damage blocks it left "
                       "behind, reports them as `residual_damage` in --json, and exits "
                       "non-zero until they are repaired too"
                   ))
    p.add_argument("--all-symbols", action="store_true",
                   help=(
                       "explicit restatement of the DEFAULT scope (kept for backward "
                       "compatibility): plan every symbol of --provider, i.e. the §15 "
                       "census universe, so the plan equals the census damage set"
                   ))
    p.add_argument("--provider", default=_DEFAULT_PROVIDER)
    p.add_argument("--reference-provider", default=_DEFAULT_REFERENCE_PROVIDER)
    p.add_argument("--apply", action="store_true",
                   help="actually write (requires --backup-dir unless the DB is a copy)")
    p.add_argument("--i-know-this-is-production", action="store_true",
                   help="required for --apply on <repo>/data/cache.duckdb")
    p.add_argument("--backup-dir", default=None, help="where the timestamped backup lands")
    p.add_argument("--stamp", default=None, help="fixed backup timestamp (tests only)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    return p.parse_args(argv)


def _baseline_paths(args: argparse.Namespace) -> List[str]:
    return [p for p in [args.baseline, *(args.additional_baselines or [])] if p]


def _open_baselines(paths: Sequence[str]) -> List[Tuple[str, duckdb.DuckDBPyConnection]]:
    return [(os.path.basename(p), connect_read_only(p)) for p in paths]


def _close_baselines(cons: Sequence[Tuple[str, duckdb.DuckDBPyConnection]]) -> None:
    for _, c in cons:
        c.close()


def _residual_verdict(n: int) -> str:
    """The sentence that replaces "nothing to repair" when the plan left damage behind."""
    return (
        f"plan completed, but {n} damage block(s) remain — "
        f"计划完成，但仍有 {n} 块残差"
    )


def _uncertified_notice(provider: str) -> str:
    return (
        "warning: no --baseline was supplied, so the whole-universe census cannot certify "
        f"`{provider}` as clean. This run is UNVERIFIED — not a clean result — and the "
        "exit code stays non-zero."
    )


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if args.symbols and args.all_symbols:
        print(
            "error: --symbol narrows the scope while --all-symbols is already the default; "
            "pass one or the other.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    narrowed = args.symbols is not None
    base_paths = _baseline_paths(args)

    con = None
    cons_base: List[Tuple[str, duckdb.DuckDBPyConnection]] = []
    notes: List[str] = []
    ops: List[RepairOp] = []
    damage: List[DamageBlock] = []
    residual: List[DamageBlock] = []
    symbols: List[str] = []
    certified = False
    try:
        con = connect_read_only(args.db)
        # Default scope = the census universe. `--symbol` is the only narrowing switch.
        symbols = list(args.symbols) if narrowed else list_symbols(con, args.provider)
        cons_base = _open_baselines(base_paths)
        certified = bool(cons_base)
        ops = build_plan(
            con,
            cons_base[0][1] if cons_base else None,
            symbols,
            args.provider,
            args.reference_provider,
            extra_baselines=cons_base[1:],
            notes=notes,
            baseline_label=cons_base[0][0] if cons_base else "baseline",
        )
        # The residual is measured on the WHOLE provider universe even for a narrowed
        # plan: measuring it on the narrowed slice would report every narrowing as clean,
        # which is the exact "half-repaired looks finished" failure this scope rule fixes.
        damage = find_damage_blocks(
            con, cons_base, args.provider, args.reference_provider
        )
        residual = residual_damage(ops, damage)
        previews = (
            repair_preview(
                con, ops, args.provider, args.reference_provider, baselines=cons_base
            )
            if ops
            else []
        )
    except (FileNotFoundError, LookupError, duckdb.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except RepairRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_ACTIONABLE
    finally:
        _close_baselines(cons_base)
        if con is not None:
            con.close()

    if not args.apply:
        if args.json:
            print(json.dumps(
                {
                    "mode": "dry-run",
                    "db": args.db,
                    "scope": "narrowed" if narrowed else "all-symbols",
                    "symbols": symbols,
                    "damage_census_certified": certified,
                    "damage_blocks": [b.as_dict() for b in damage],
                    "residual_damage": [b.as_dict() for b in residual],
                    "residual_damage_count": len(residual),
                    "ops": [
                        {**p["op"].as_dict(), "seam_after_pp": round(p["seam_after_pp"], 6),
                         "volume_unchanged": p["volume_unchanged"],
                         "max_rel_change": float(f"{p['max_rel_change']:.3e}"),
                         "seams_after": p["seams_after"],
                         "seam_after_dates": p.get("seam_after_dates", []),
                         "edges": p.get("edges", {}),
                         "head": p["head"], "tail": p["tail"]}
                        for p in previews
                    ],
                    "notes": list(dict.fromkeys(notes)),
                },
                indent=2, ensure_ascii=False,
            ))
        else:
            print(render_plan(ops, previews, args.db))
            if notes:
                print("\nconsidered but not repaired (nothing was written for these):")
                for note in dict.fromkeys(notes):
                    print(f"- {note}")
            if residual:
                print(render_residual(residual, ops))
                print("\n" + _residual_verdict(len(residual)))
            if not certified:
                print("\n" + _uncertified_notice(args.provider))
            if ops:
                print(
                    f"\n{len(ops)} op(s) would touch "
                    f"{sum(o.rows for o in ops)} row(s). Nothing was written "
                    f"(--dry-run is the default)."
                )
                if not residual and certified:
                    print(
                        "residual damage: none — the plan covers every damage block the "
                        f"census sees in `{args.provider}`."
                    )
                if residual:
                    print(
                        "exit: 1 (RESIDUAL + ACTIONABLE) — --apply would fix only the ops "
                        "above and leave the residual blocks unfixed."
                    )
                else:
                    print("exit: 1 (ACTIONABLE) — run again with --apply to write.")
            elif residual:
                print(
                    "\nexit: 1 (RESIDUAL) — the plan is empty for this scope, yet the "
                    "census still sees damaged blocks (listed above)."
                )
            elif certified:
                print(
                    "\nnothing to repair: the census found no damage block in any symbol "
                    f"of `{args.provider}`."
                )
                print("exit: 0 (clean)")
            else:
                print("\nexit: 1 (UNCERTIFIED) — supply --baseline to run the census.")
        if not certified or residual:
            return EXIT_RESIDUAL
        return EXIT_ACTIONABLE if ops else EXIT_OK

    # ---- apply ----
    if not ops:
        if not certified:
            print(
                "nothing to do: no repair op could be derived, and without --baseline the "
                "damage census cannot certify the cache is clean — UNVERIFIED, so this is "
                "not reported as a clean result."
            )
            return EXIT_RESIDUAL
        if residual:
            print(render_residual(residual, ops))
            print("\n" + _residual_verdict(len(residual)))
            print("no repair op could be derived for the requested scope; nothing was written.")
            return EXIT_RESIDUAL
        print(
            "nothing to repair: the census found no damage block in any symbol of "
            f"`{args.provider}`."
        )
        return EXIT_OK
    try:
        backup = apply_plan(
            args.db,
            ops,
            backup_dir=args.backup_dir,
            stamp=args.stamp,
            reference_provider=args.reference_provider,
            allow_production=args.i_know_this_is_production,
            baseline_paths=base_paths,
        )
    except RepairRefused as exc:
        print(f"refused: {exc}\nnothing was written.", file=sys.stderr)
        return EXIT_ACTIONABLE
    except Exception as exc:
        print(f"apply FAILED and was rolled back: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # Post-apply: re-run the whole-universe census on the committed file. A narrowed
    # apply is *not* a success just because its own ops landed.
    con_after = None
    cons_after: List[Tuple[str, duckdb.DuckDBPyConnection]] = []
    try:
        con_after = connect_read_only(args.db)
        cons_after = _open_baselines(base_paths)
        residual_after = residual_damage(
            ops,
            find_damage_blocks(con_after, cons_after, args.provider, args.reference_provider),
        )
    finally:
        _close_baselines(cons_after)
        if con_after is not None:
            con_after.close()

    if args.json:
        print(json.dumps(
            {
                "mode": "apply",
                "db": args.db,
                "scope": "narrowed" if narrowed else "all-symbols",
                "backup": backup,
                "applied": [op.as_dict() for op in ops],
                "residual_damage": [b.as_dict() for b in residual_after],
                "residual_damage_count": len(residual_after),
                "damage_census_certified": certified,
            },
            indent=2, ensure_ascii=False,
        ))
    else:
        print(f"applied {len(ops)} op(s) to {args.db}")
        print(f"backup: {backup}")
        for op in ops:
            print(f"  {op.symbol} {op.kind} {_iso(op.start)}..{_iso(op.end)} rows={op.rows}")
        if residual_after:
            print(render_residual(residual_after, ops))
            print("\n" + _residual_verdict(len(residual_after)))
        elif not certified:
            print("\n" + _uncertified_notice(args.provider))
        else:
            print(
                "\npost-apply census: nothing to repair — no damage block remains in any "
                f"symbol of `{args.provider}`."
            )
    return EXIT_RESIDUAL if residual_after or not certified else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

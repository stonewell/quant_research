#!/usr/bin/env python3
"""Cross-source `adjust_factors` repair (D9): dry-run by default, guarded ``--apply``.

WHAT THIS REPAIRS (and why it is not a "re-fetch")
--------------------------------------------------
``adjust_factors`` carries a ``source`` column but its primary key is ``(symbol, date)``, and
``common/duckdb_cache.py::store_adjust_factors`` writes with ``INSERT OR REPLACE``. So a writer
whose factors are normalised on a DIFFERENT basis silently destroys the other source's row on
every shared date. The D9 incident: ``scripts/sync_china_5m_and_indices.py`` stored BaoStock's
sparse event rows (cumulative from the IPO -- ``600660.SH`` reaches **81.588562** by 2026-09-24)
into symbols whose dense daily rows come from Fuyao (2024-anchored, 1.2452-1.3895). The surviving
row is 58.7x its neighbours; applying it rescaled that session's qfq close from 52.47 to 3080.96,
moved ``sma_50`` from 54.21 to 114.75, and polluted breadth / target-exposure / dividend alerts.

The write side now refuses such batches (``store_adjust_factors``, D9) and the read side keeps a
plausibility band (``ImplausibleAdjustFactorStep``). Rows polluted BEFORE that gate existed must
still be cleaned, which is what this script does -- it deletes, it does not "re-pull".

WHAT IS CLASSIFIED AS FOREIGN (and why the scope is the dense span)
-------------------------------------------------------------------
Per symbol, the **dense source** is the one holding the most rows (ties: widest date span, then
name). Every other source whose row count is at most ``SPARSE_MAX_SHARE`` of the dense count is
**sparse-foreign** -- the two bases are not competitors by volume, so one of them is a partial
transcription of the other.

Only the sparse-foreign rows that fall INSIDE the dense source's covered interval are removed.
That is exactly the set that could have overwritten a dense row (the PK collision the bug needs)
and the set that interleaves a second basis into one as-of sequence. Rows outside the interval
are reported but kept: they are the only factor rows for bars before the dense series starts,
and deleting them would leave those bars on the 1.0 default (a different, larger error). They
are flagged in the report as ``foreign-basis prefix`` for a follow-up decision.

DRY-RUN / APPLY SEMANTICS (same contract as ``scripts/repair_qfq_alignment.py``)
-------------------------------------------------------------------------------
* default is ``--dry-run``: it opens ``read_only=True`` and writes NOTHING -- not even a
  directory under ``data/repair_backups``;
* ``--apply`` is the only mode that writes, and it must, in this order:
  1. be refused if a pytest process targets ``<repo>/data`` (the L2.13 guard, reused);
  2. be refused on ``<repo>/data/cache.duckdb`` unless ``--i-know-this-is-production``;
  3. export every row it is about to delete to
     ``data/repair_backups/<timestamp>/adjust_factors_removed.json`` (original rows verbatim,
     ``last_sync_time`` included) and take a timestamped byte-copy of the database file;
  4. write ``repair_manifest.json`` (md5 of the backup JSON and of the DB copy before/after,
     removed row count, execution time, script version);
  5. delete inside ONE transaction and self-verify the resulting row counts;
  6. reopen read-only and re-run the census, exiting non-zero if a foreign row inside a dense
     span is still visible.
  Backups are never deleted by this script, and a failed apply keeps them.

The original rows are never overwritten in place: the repair is a DELETE, and the deleted values
survive only in the JSON backup (which is why the backup is written before the transaction).

EXIT CODES
----------
``0``  nothing to remove (or apply: removed everything the census saw)
``1``  dry-run: an actionable plan / apply: residual rows remain or a guard refused (nothing
       written)
``2``  usage / data error, or apply failed and rolled back

USAGE
-----
    # dry-run (default) against the production cache -- read-only, safe
    uv run --offline --project pipeline python scripts/repair_cross_source_adjust_factors.py

    # apply onto a COPY (never the production cache)
    uv run --offline --project pipeline python scripts/repair_cross_source_adjust_factors.py \\
        --db /tmp/opencode/cache_copy.duckdb --apply
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
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

from scripts.audit_qfq_alignment import connect_read_only  # noqa: E402  (bootstrap first)
from common.duckdb_cache import (  # noqa: E402
    IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO,
    IMPLAUSIBLE_ADJUST_FACTOR_RATIO,
)
from scripts.repair_qfq_alignment import (  # noqa: E402
    RepairRefused,
    refuse_production_apply,
)

#: Version string recorded in ``repair_manifest.json``. Bump when the classification rules (not
#: the formatting) change, so a manifest always says which rules produced its row set.
SCRIPT_VERSION = "1.0.0"

_DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
_DEFAULT_BACKUP_ROOT = os.path.join(_REPO_ROOT, "data", "repair_backups")
_TABLE = "adjust_factors"

#: A source holding at most this share of the dense source's rows is "sparse": it is a partial
#: transcription, not a competing full series. Half is deliberately generous -- the observed
#: foreign sources hold 5-48 rows against 670-680 dense rows (< 8%), and a symbol with two
#: genuinely comparable sources must be reviewed by a human, not silently deleted from.
SPARSE_MAX_SHARE = 0.5

EXIT_OK = 0
EXIT_ACTIONABLE = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class SourceStat:
    """One (symbol, source) group as the census sees it."""

    source: str
    rows: int
    first: datetime.date
    last: datetime.date

    @property
    def span_days(self) -> int:
        return (self.last - self.first).days


@dataclass
class PlannedRow:
    """One row this script would delete, with the context needed to judge it."""

    symbol: str
    date: datetime.date
    factor_hfq: float
    source: str
    last_sync_time: Optional[str] = None
    #: The dense source's as-of factor at (or before) this date, i.e. what the read path will
    #: fall back to once the row is gone.
    dense_asof_date: Optional[datetime.date] = None
    dense_asof_factor: Optional[float] = None

    @property
    def ratio(self) -> Optional[float]:
        if not self.dense_asof_factor:
            return None
        return self.factor_hfq / self.dense_asof_factor

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": self.date.isoformat(),
            "factor_hfq": self.factor_hfq,
            "source": self.source,
            "last_sync_time": self.last_sync_time,
            "dense_asof_date": (
                None if self.dense_asof_date is None else self.dense_asof_date.isoformat()
            ),
            "dense_asof_factor": self.dense_asof_factor,
            "ratio_to_dense": None if self.ratio is None else round(self.ratio, 6),
        }


@dataclass
class SymbolPlan:
    """Everything the census decided for one symbol."""

    symbol: str
    dense: SourceStat
    sparse: SourceStat
    rows: List[PlannedRow] = field(default_factory=list)
    #: Sparse-foreign rows OUTSIDE the dense span: kept, but reported (see module docstring).
    kept_outside: int = 0
    kept_outside_first: Optional[datetime.date] = None
    kept_outside_last: Optional[datetime.date] = None
    #: Extreme kept-prefix factor as a multiple of the dense median. When this leaves the read
    #: guard's band, the symbol's full-history qfq/hfq read fails closed *after* this repair --
    #: the operator needs to know that here, not by hitting the exception later.
    kept_outside_max_ratio: Optional[float] = None

    @property
    def sparse_share(self) -> float:
        return self.sparse.rows / self.dense.rows if self.dense.rows else 1.0


@dataclass
class SkippedSymbol:
    """A multi-source symbol this script deliberately does NOT touch."""

    symbol: str
    reason: str
    stats: List[SourceStat] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Census / plan
# ---------------------------------------------------------------------------


def _source_stats(
    con: duckdb.DuckDBPyConnection, symbol: str
) -> List[SourceStat]:
    rows = con.execute(
        f"SELECT coalesce(source, '<NULL>') AS src, count(*), min(date), max(date) "
        f"FROM {_TABLE} WHERE symbol = ? GROUP BY 1",
        [symbol],
    ).fetchall()
    out: List[SourceStat] = []
    for src, n, lo, hi in rows:
        lo = lo if isinstance(lo, datetime.date) else lo.date()
        hi = hi if isinstance(hi, datetime.date) else hi.date()
        out.append(SourceStat(source=src, rows=int(n), first=lo, last=hi))
    return sorted(out, key=lambda s: (-s.rows, -s.span_days, s.source))


def list_symbols(con: duckdb.DuckDBPyConnection) -> List[str]:
    return [
        r[0]
        for r in con.execute(f"SELECT DISTINCT symbol FROM {_TABLE} ORDER BY symbol").fetchall()
    ]


def multi_source_symbols(con: duckdb.DuckDBPyConnection) -> List[str]:
    return [
        r[0]
        for r in con.execute(
            f"SELECT symbol FROM {_TABLE} GROUP BY symbol "
            f"HAVING count(DISTINCT coalesce(source, '<NULL>')) > 1 ORDER BY symbol"
        ).fetchall()
    ]


def _outside_extreme_ratio(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    source: str,
    dense: SourceStat,
    dense_factors: Sequence[float],
) -> Optional[float]:
    """Extreme kept-prefix factor as a multiple of the dense median (``None`` when N/A).

    This is the same quantity the READ guard compares against
    (`common/duckdb_cache.py::IMPLAUSIBLE_ADJUST_FACTOR_RATIO`): reporting it here says, up
    front, whether the symbol will still fail closed after this repair.
    """
    positive = [f for f in dense_factors if f > 0]
    if not positive:
        return None
    median = float(pd.Series(positive).median())
    if not median:
        return None
    row = con.execute(
        f"SELECT max(factor_hfq), min(factor_hfq) FROM {_TABLE} "
        f"WHERE symbol = ? AND coalesce(source, '<NULL>') = ? AND (date < ? OR date > ?)",
        [symbol, source, dense.first, dense.last],
    ).fetchone()
    if not row or row[0] is None:
        return None
    return max(abs(float(row[0]) / median), abs(float(row[1]) / median))


def _outside_ratio_is_implausible(ratio: Optional[float]) -> bool:
    return ratio is not None and (
        ratio > IMPLAUSIBLE_ADJUST_FACTOR_RATIO or ratio < IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO
    )


def build_plan(
    con: duckdb.DuckDBPyConnection,
    symbols: Optional[Sequence[str]] = None,
) -> Tuple[List[SymbolPlan], List[SkippedSymbol]]:
    """Classify every symbol and list the rows to delete.

    Read-only by construction: the only statements issued here are ``SELECT``s.
    """
    plans: List[SymbolPlan] = []
    skipped: List[SkippedSymbol] = []
    universe = list(symbols) if symbols else multi_source_symbols(con)

    for symbol in universe:
        stats = _source_stats(con, symbol)
        if len(stats) < 2:
            skipped.append(SkippedSymbol(symbol, "single source -- nothing to reconcile", stats))
            continue
        dense = stats[0]
        others = stats[1:]
        for other in others:
            if other.rows > dense.rows * SPARSE_MAX_SHARE:
                skipped.append(
                    SkippedSymbol(
                        symbol,
                        f"{other.source!r} holds {other.rows} row(s) vs {dense.rows} dense -- "
                        f"two comparable sources, needs a human decision "
                        f"(delete neither automatically)",
                        stats,
                    )
                )
                continue
            inside = con.execute(
                f"SELECT date, factor_hfq, last_sync_time FROM {_TABLE} "
                f"WHERE symbol = ? AND coalesce(source, '<NULL>') = ? AND date BETWEEN ? AND ? "
                f"ORDER BY date",
                [symbol, other.source, dense.first, dense.last],
            ).fetchall()
            dense_rows = con.execute(
                f"SELECT date, factor_hfq FROM {_TABLE} "
                f"WHERE symbol = ? AND coalesce(source, '<NULL>') = ? ORDER BY date",
                [symbol, dense.source],
            ).fetchall()
            dense_dates = [
                d if isinstance(d, datetime.date) else d.date() for d, _ in dense_rows
            ]
            dense_factors = [float(v) for _, v in dense_rows]

            plan = SymbolPlan(symbol=symbol, dense=dense, sparse=other)
            for date, factor, last_sync in inside:
                date = date if isinstance(date, datetime.date) else date.date()
                pos = _last_at_or_before(dense_dates, date)
                plan.rows.append(
                    PlannedRow(
                        symbol=symbol,
                        date=date,
                        factor_hfq=float(factor),
                        source=other.source,
                        last_sync_time=None if last_sync is None else str(last_sync),
                        dense_asof_date=None if pos < 0 else dense_dates[pos],
                        dense_asof_factor=None if pos < 0 else dense_factors[pos],
                    )
                )
            outside = other.rows - len(plan.rows)
            plan.kept_outside = outside
            if outside:
                lo, hi = con.execute(
                    f"SELECT min(date), max(date) FROM {_TABLE} "
                    f"WHERE symbol = ? AND coalesce(source, '<NULL>') = ? "
                    f"AND (date < ? OR date > ?)",
                    [symbol, other.source, dense.first, dense.last],
                ).fetchone()
                plan.kept_outside_first = None if lo is None else _as_date(lo)
                plan.kept_outside_last = None if hi is None else _as_date(hi)
                plan.kept_outside_max_ratio = _outside_extreme_ratio(
                    con, symbol, other.source, dense, dense_factors
                )
            if plan.rows:
                plans.append(plan)
            else:
                reason = (
                    f"{other.source!r} holds no row inside {dense.source!r} "
                    f"{dense.first}..{dense.last} -- no overwrite possible"
                )
                ratio = _outside_extreme_ratio(
                    con, symbol, other.source, dense, dense_factors
                )
                if _outside_ratio_is_implausible(ratio):
                    reason += (
                        f"; [WARN] its kept prefix is {ratio:.2f}x the dense median (guard band "
                        f"{IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO}-{IMPLAUSIBLE_ADJUST_FACTOR_RATIO}x) "
                        f"-> full-history qfq/hfq reads of {symbol} fail closed "
                        f"(ImplausibleAdjustFactorStep) until that pre-{dense.first} history "
                        f"is re-pulled from a single source"
                    )
                skipped.append(SkippedSymbol(symbol, reason, stats))
    return plans, skipped


def _as_date(value: Any) -> datetime.date:
    return value if isinstance(value, datetime.date) else value.date()


def _last_at_or_before(dates: Sequence[datetime.date], day: datetime.date) -> int:
    """Index of the last dense date <= ``day`` (-1 when none)."""
    lo, hi, ans = 0, len(dates) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= day:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan(
    plans: Sequence[SymbolPlan],
    skipped: Sequence[SkippedSymbol],
    db: str,
    *,
    head: int = 6,
) -> str:
    lines: List[str] = []
    rows_total = sum(len(p.rows) for p in plans)
    lines.append(f"cross-source adjust-factor census (READ-ONLY): {db}")
    lines.append(
        f"  table={_TABLE}  symbols with >1 source={len(plans) + len(skipped)}  "
        f"actionable symbols={len(plans)}  rows to remove={rows_total}"
    )
    if not plans:
        lines.append("")
        lines.append("nothing to repair: no foreign-source row sits inside another source's span.")
    for plan in plans:
        lines.append("")
        lines.append(
            f"{plan.symbol}  dense={plan.dense.source} rows={plan.dense.rows} "
            f"{plan.dense.first}..{plan.dense.last}"
        )
        lines.append(
            f"  sparse={plan.sparse.source} rows={plan.sparse.rows} "
            f"{plan.sparse.first}..{plan.sparse.last} "
            f"({plan.sparse_share:.1%} of dense) -> DELETE {len(plan.rows)} inside-row(s)"
            + (
                f", KEEP {plan.kept_outside} outside-row(s) "
                f"({plan.kept_outside_first}..{plan.kept_outside_last}, "
                f"foreign-basis prefix)"
                if plan.kept_outside
                else ""
            )
        )
        lines.append(
            "    date        removed factor   dense factor (as-of)      ratio    source"
        )
        for row in plan.rows[:head]:
            ratio = "n/a" if row.ratio is None else f"{row.ratio:.2f}x"
            dense_txt = (
                "none stored"
                if row.dense_asof_factor is None
                else f"{row.dense_asof_factor:.6f} ({row.dense_asof_date})"
            )
            lines.append(
                f"    {row.date}  {row.factor_hfq:>14.6f}   {dense_txt:>24}   "
                f"{ratio:>7}   {row.source}"
            )
        if len(plan.rows) > head:
            lines.append(f"    ... {len(plan.rows) - head} more row(s)")
        if _outside_ratio_is_implausible(plan.kept_outside_max_ratio):
            lines.append(
                f"    [WARN] the kept prefix sits on the OTHER basis: its extreme factor is "
                f"{plan.kept_outside_max_ratio:.2f}x the dense median (guard band "
                f"{IMPLAUSIBLE_ADJUST_FACTOR_MIN_RATIO}-{IMPLAUSIBLE_ADJUST_FACTOR_RATIO}x)"
                f" -> after this repair, full-history qfq/hfq reads of {plan.symbol} still "
                f"fail closed (ImplausibleAdjustFactorStep) until the "
                f"pre-{plan.dense.first} history is re-pulled from a single source."
            )
    if skipped:
        lines.append("")
        lines.append("multi-source symbols left untouched (reported, nothing deleted):")
        for item in skipped:
            detail = ", ".join(f"{s.source}={s.rows}" for s in item.stats)
            lines.append(f"  {item.symbol}: {item.reason}" + (f"  [{detail}]" if detail else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _md5_file(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refuse_pytest_write(db_path: str) -> None:
    """Reuse the L2.13 gate: no pytest process may write into `<repo>/data`."""
    try:
        from common.duckdb_cache import refuse_production_cache_path
    except Exception:  # pragma: no cover - repository layout changed
        return
    refuse_production_cache_path(db_path, read_only=False)


def apply_plan(
    db_path: str,
    plans: Sequence[SymbolPlan],
    *,
    backup_dir: Optional[str] = None,
    stamp: Optional[str] = None,
    allow_production: bool = False,
) -> str:
    """Backup, then delete in one transaction. Returns the backup directory."""
    if not plans or not any(p.rows for p in plans):
        raise RepairRefused("empty plan")
    refuse_production_apply(db_path, allow_production)
    _refuse_pytest_write(db_path)

    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(backup_dir or _DEFAULT_BACKUP_ROOT, stamp)
    os.makedirs(out_dir, exist_ok=True)

    planned_rows = [row.as_dict() for plan in plans for row in plan.rows]
    json_path = os.path.join(out_dir, "adjust_factors_removed.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "script": os.path.basename(__file__),
                "script_version": SCRIPT_VERSION,
                "removed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "db": db_path,
                "rows": planned_rows,
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )

    db_copy = None
    if os.path.isfile(db_path):
        db_copy = os.path.join(out_dir, f"{os.path.basename(db_path)}.bak.{stamp}")
        shutil.copy2(db_path, db_copy)

    db_md5_before = _md5_file(db_path)
    con = duckdb.connect(db_path, read_only=False)
    try:
        before_total = con.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0]
        con.execute("BEGIN TRANSACTION")
        try:
            for plan in plans:
                con.execute(
                    f"DELETE FROM {_TABLE} WHERE symbol = ? "
                    f"AND coalesce(source, '<NULL>') = ? AND date BETWEEN ? AND ?",
                    [plan.symbol, plan.sparse.source, plan.dense.first, plan.dense.last],
                )
            after_total = con.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0]
            expected = before_total - len(planned_rows)
            if after_total != expected:
                raise RuntimeError(
                    f"row count after delete is {after_total}, expected {expected} "
                    f"({before_total} before, {len(planned_rows)} planned removals)"
                )
            for row in planned_rows:
                still = con.execute(
                    f"SELECT count(*) FROM {_TABLE} WHERE symbol = ? AND date = ? AND source = ?",
                    [row["symbol"], row["date"], row["source"]],
                ).fetchone()[0]
                if still:
                    raise RuntimeError(
                        f"{row['symbol']} {row['date']}: row survived the delete "
                        f"(another source holds the same (symbol, date))"
                    )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    manifest = {
        "script": os.path.basename(__file__),
        "script_version": SCRIPT_VERSION,
        "mode": "apply",
        "executed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "db": db_path,
        "removed_rows": len(planned_rows),
        "removed_json": json_path,
        "removed_json_md5": _md5_file(json_path),
        "db_copy": db_copy,
        "db_copy_md5": _md5_file(db_copy) if db_copy else None,
        "db_md5_before": db_md5_before,
        "db_md5_after": _md5_file(db_path),
        "symbols": [
            {
                "symbol": plan.symbol,
                "dense_source": plan.dense.source,
                "dense_rows": plan.dense.rows,
                "dense_span": [plan.dense.first.isoformat(), plan.dense.last.isoformat()],
                "sparse_source": plan.sparse.source,
                "sparse_rows": plan.sparse.rows,
                "removed": len(plan.rows),
                "kept_outside_span": plan.kept_outside,
            }
            for plan in plans
        ],
    }
    with open(os.path.join(out_dir, "repair_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return out_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Dry-run (default) and guarded apply for the D9 cross-source adjust-factor "
            "overwrite. Dry-run never writes; --apply exports the removed rows to JSON, "
            "copies the database, deletes in one transaction and self-verifies."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=_DEFAULT_DB, help="cache to inspect (dry-run: read-only)")
    p.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        default=None,
        help="NARROW the scope to these symbols (repeatable)",
    )
    p.add_argument("--apply", action="store_true", help="actually delete (writes a backup first)")
    p.add_argument(
        "--i-know-this-is-production",
        action="store_true",
        help="required for --apply on <repo>/data/cache.duckdb",
    )
    p.add_argument(
        "--backup-dir",
        default=None,
        help=f"parent of the timestamped backup directory (default: {_DEFAULT_BACKUP_ROOT})",
    )
    p.add_argument("--stamp", default=None, help="fixed backup timestamp (tests only)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument(
        "--head",
        type=int,
        default=6,
        help="rows printed per symbol in the table (default: 6; the JSON/backup is complete)",
    )
    return p.parse_args(argv)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    con = None
    try:
        con = connect_read_only(args.db)
        plans, skipped = build_plan(con, args.symbols)
    except (FileNotFoundError, LookupError, duckdb.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if con is not None:
            con.close()

    total_rows = sum(len(p.rows) for p in plans)

    if not args.apply:
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "db": args.db,
                        "script_version": SCRIPT_VERSION,
                        "symbols": [p.symbol for p in plans],
                        "rows_to_remove": total_rows,
                        "plan": [
                            {
                                "symbol": p.symbol,
                                "dense_source": p.dense.source,
                                "sparse_source": p.sparse.source,
                                "rows": [r.as_dict() for r in p.rows],
                                "kept_outside_span": p.kept_outside,
                                "kept_outside_max_ratio": (
                                    None
                                    if p.kept_outside_max_ratio is None
                                    else round(p.kept_outside_max_ratio, 6)
                                ),
                            }
                            for p in plans
                        ],
                        "skipped": [
                            {"symbol": s.symbol, "reason": s.reason} for s in skipped
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(render_plan(plans, skipped, args.db, head=args.head))
            if plans:
                print(
                    f"\n{total_rows} row(s) would be deleted from {len(plans)} symbol(s). "
                    f"Nothing was written (--dry-run is the default)."
                )
                print("exit: 1 (ACTIONABLE) -- run again with --apply to write.")
            else:
                print("\nnothing to repair. exit: 0 (clean)")
        return EXIT_ACTIONABLE if plans else EXIT_OK

    if not plans:
        print("nothing to repair: no foreign-source row sits inside another source's span.")
        return EXIT_OK
    try:
        out_dir = apply_plan(
            args.db,
            plans,
            backup_dir=args.backup_dir,
            stamp=args.stamp,
            allow_production=args.i_know_this_is_production,
        )
    except RepairRefused as exc:
        print(f"refused: {exc}\nnothing was written.", file=sys.stderr)
        return EXIT_ACTIONABLE
    except Exception as exc:
        print(f"apply FAILED and was rolled back: {exc}\nbackups kept.", file=sys.stderr)
        return EXIT_ERROR

    # Re-census on a fresh read-only connection: the repair is only done when the census is.
    con_after = None
    try:
        con_after = connect_read_only(args.db)
        residual, residual_skipped = build_plan(con_after, args.symbols)
    finally:
        if con_after is not None:
            con_after.close()

    if args.json:
        print(
            json.dumps(
                {
                    "mode": "apply",
                    "db": args.db,
                    "backup_dir": out_dir,
                    "removed_rows": total_rows,
                    "removed": [p.symbol for p in plans],
                    "residual_rows": sum(len(p.rows) for p in residual),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"applied {total_rows} deletion(s) across {len(plans)} symbol(s) to {args.db}")
        print(f"backup: {out_dir}")
        for plan in plans:
            print(f"  {plan.symbol}: -{len(plan.rows)} {plan.sparse.source} row(s)")
        if residual:
            print(render_plan(residual, residual_skipped, args.db, head=args.head))
            print(f"\n{sum(len(p.rows) for p in residual)} row(s) still look foreign.")
        else:
            print("\npost-apply census: no foreign-source row inside another source's span.")
    return EXIT_ACTIONABLE if residual else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

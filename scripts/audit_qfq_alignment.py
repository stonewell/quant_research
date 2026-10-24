#!/usr/bin/env python3
"""Read-only audit of QFQ (forward-adjusted) price realignment artefacts in the cache.

WHY THIS EXISTS (``docs/development_plan.md`` ``L2.8``)
------------------------------------------------------
``docs/version.md`` §9.2 recorded that the 2026-10-22 Fuyao sync rewrote historical
OHLC prices over *bounded* date windows:

* ``600362.SH`` × 0.99663988 over 2024-01-02 → 2025-12-31 (485 rows);
* ``600660.SH`` × 0.98141264 over 2024-01-02 → 2024-07-19 (132 rows);
* ``600660.SH`` × 1.0 (i.e. a rewrite with no price change) over 2026-03-09 → 2026-09-11;
* ``600900.SH`` a short window whose only difference is float ULP noise.

A *bounded* rescale is the interesting shape: a genuine forward-adjustment (QFQ)
re-basing driven by an ex-dividend event multiplies **every** bar before the ex-date,
so its footprint is open-ended towards the past, never a closed interval. The audit
below therefore separates three mechanically different footprints:

``material_rescale``
    A contiguous run of rows whose OHLC were multiplied by one constant scalar that
    is *not* 1. This is the arithmetic signature of a local "align to the overlap
    date" rescale applied to already-stored floats (``new == old * k`` to within a
    float64 ULP), **not** of an upstream re-fetch (an upstream QFQ re-fetch returns
    freshly rounded values, so the per-row ratio would jitter in the 1e-4 range).
``ulp_noise``
    A contiguous run of rows rewritten with a relative change <= ``--tol-rel``
    (default 1e-12). Numerically a no-op: it proves a write happened, not that any
    price changed.
``rewrite``
    A contiguous run of material changes whose per-row ratio is *not* constant.

And it reports what a bounded rescale breaks: the **single-bar returns straddling
each edge**, which are the only values a constant multiplicative offset corrupts
(returns strictly inside the block, and strictly outside it, are unaffected).

MODES
-----
``diff`` (needs ``--baseline``)
    Compare the audited DB against an earlier snapshot (e.g.
    ``data/cache.duckdb.bak.20261022-213155``) row by row. This is the reproduction
    mode: it tells you exactly which rows changed and by how much.
``cross-provider`` (default when no ``--baseline`` is given)
    Compare two provider namespaces *inside one DB* (default
    ``FuyaoDataProvider`` vs ``MarketDBDataProvider``). The two namespaces are
    maintained from the same upstream chain and agree bit-for-bit outside a
    rewritten window, so a step in their ratio is the same artefact observed without
    needing a backup file.

READ-ONLY GUARANTEE
-------------------
The production cache is opened with ``duckdb.connect(..., read_only=True)`` and this
module deliberately does **not** import ``common.duckdb_cache`` — there is no write
path to reach at all. Nothing here mutates a database, and the script touches no
network.

EXIT CODES
----------
``0``  no material anomaly (only ULP-noise rewrites, at most)
``1``  at least one material anomaly (bounded non-unit rescale, or a material
       rewrite) — i.e. "this cache contains a QFQ alignment artefact"
``2``  usage / data error (missing file, missing table, unknown symbol, bad range)

The exit code is the contract used by the offline tests; the human-readable report
goes to stdout.

USAGE
-----
    uv run --offline --project pipeline python scripts/audit_qfq_alignment.py \\
        --baseline data/cache.duckdb.bak.20261022-213155

    # only one symbol, only 2024, machine-readable
    uv run --offline --project pipeline python scripts/audit_qfq_alignment.py \\
        --baseline data/cache.duckdb.bak.20261022-213155 \\
        --symbol 600660.SH --start 2024-01-01 --end 2024-12-31 --json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "cache.duckdb")
_DEFAULT_PROVIDER = "FuyaoDataProvider"
_DEFAULT_REFERENCE_PROVIDER = "MarketDBDataProvider"
_DEFAULT_SYMBOLS = ("600362.SH", "600660.SH")
_PRICE_COLS = ("open", "high", "low", "close")

EXIT_OK = 0
EXIT_ANOMALY = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """One contiguous run of rewritten rows, with the evidence needed to classify it."""

    symbol: str
    provider: str
    start: datetime.date
    end: datetime.date
    rows: int
    kind: str  # "material_rescale" | "ulp_noise" | "rewrite"
    ratios: Dict[str, float] = field(default_factory=dict)
    ratio_spread: Dict[str, float] = field(default_factory=dict)
    max_rel_change: float = 0.0
    left_anchor: Optional[datetime.date] = None
    right_anchor: Optional[datetime.date] = None
    boundary_distortion_pp: Dict[str, float] = field(default_factory=dict)
    bounded: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "rows": self.rows,
            "kind": self.kind,
            "ratios": {k: round(v, 12) for k, v in self.ratios.items()},
            "ratio_spread": {k: float(f"{v:.3e}") for k, v in self.ratio_spread.items()},
            "max_rel_change": float(f"{self.max_rel_change:.3e}"),
            "left_anchor": _iso(self.left_anchor),
            "right_anchor": _iso(self.right_anchor),
            "boundary_distortion_pp": {
                k: round(v, 6) for k, v in self.boundary_distortion_pp.items()
            },
            "bounded": self.bounded,
        }


@dataclass
class Seam:
    """A step in the ``provider / reference_provider`` price ratio.

    ``date_before``/``date_after`` are the two sessions the step sits between, i.e.
    the interval edge; ``step_pp`` is the size of the injected level error.
    """

    symbol: str
    provider: str
    date_before: datetime.date
    date_after: datetime.date
    ratio_before: float
    ratio_after: float
    step_pp: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "date_before": _iso(self.date_before),
            "date_after": _iso(self.date_after),
            "ratio_before": round(self.ratio_before, 12),
            "ratio_after": round(self.ratio_after, 12),
            "step_pp": round(self.step_pp, 6),
        }


@dataclass
class RewriteRecord:
    """One material rewrite, with the cross-namespace evidence that fixes its direction.

    ``direction`` is the only judgement the census makes, and it is deliberately
    one-sided-information-free: it compares the *pre-incident* and *current* level of
    ``provider / reference`` **on the same sessions**, so it never needs reference
    coverage outside the rewritten window.
    """

    symbol: str
    provider: str
    start: datetime.date
    end: datetime.date
    rows: int
    kind: str
    factor: float
    spread: float
    volume_unchanged: bool
    bounded: bool
    left_pp: Optional[float]
    right_pp: Optional[float]
    ref_rows: int
    ratio_old: Optional[float]
    ratio_new: Optional[float]
    direction: str  # "damage" | "fix" | "undetermined"
    reason: str
    baseline: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "baseline": self.baseline,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "rows": self.rows,
            "kind": self.kind,
            "factor": round(self.factor, 10),
            "spread": float(f"{self.spread:.3e}"),
            "volume_unchanged": self.volume_unchanged,
            "bounded": self.bounded,
            "left_pp": None if self.left_pp is None else round(self.left_pp, 6),
            "right_pp": None if self.right_pp is None else round(self.right_pp, 6),
            "ref_rows": self.ref_rows,
            "ratio_old": None if self.ratio_old is None else round(self.ratio_old, 10),
            "ratio_new": None if self.ratio_new is None else round(self.ratio_new, 10),
            "direction": self.direction,
            "reason": self.reason,
        }


def _iso(d: Optional[datetime.date]) -> Optional[str]:
    return d.isoformat() if d is not None else None


# ---------------------------------------------------------------------------
# Loading (read-only)
# ---------------------------------------------------------------------------


def connect_read_only(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open a cache file strictly read-only.

    Raises ``FileNotFoundError`` when the path does not exist so the caller can map
    it to ``EXIT_ERROR``.
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"cache file not found: {db_path}")
    return duckdb.connect(db_path, read_only=True)


def load_bars(
    con: duckdb.DuckDBPyConnection,
    provider: str,
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    table: str = "ohlcv_bars",
) -> pd.DataFrame:
    """Load and validate one (provider, symbol) daily series, ascending by date."""
    if not _table_exists(con, table):
        raise LookupError(f"table {table!r} not found in the database")
    cols = ", ".join(_PRICE_COLS + ("volume",))
    sql = f"SELECT date, {cols} FROM {table} WHERE provider = ? AND symbol = ?"
    params: List[Any] = [provider, symbol]
    if start:
        sql += " AND CAST(date AS DATE) >= CAST(? AS DATE)"
        params.append(start)
    if end:
        sql += " AND CAST(date AS DATE) <= CAST(? AS DATE)"
        params.append(end)
    sql += " ORDER BY date"
    df = con.execute(sql, params).df()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in _PRICE_COLS + ("volume",):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    # NaN prices would poison every ratio; drop them explicitly instead of silently
    # producing a "no change" verdict.
    df = df.dropna(subset=list(_PRICE_COLS)).reset_index(drop=True)
    return df


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """True when ``table`` resolves, including an ``ATTACH``-ed ``catalog.table`` form.

    The census pass compares the live cache against a snapshot attached as e.g.
    ``bak``, so the qualified spelling must not be mistaken for a missing table —
    that error would silently abort a whole-cache scan.
    """
    parts = table.split(".")
    name = parts[-1]
    if len(parts) == 1:
        rows = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    elif len(parts) == 2:
        rows = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = ? AND table_catalog = ?",
            [name, parts[0]],
        ).fetchone()
    else:
        rows = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = ? AND table_catalog = ? AND table_schema = ?",
            [name, parts[-3], parts[-2]],
        ).fetchone()
    return bool(rows and rows[0])


# ---------------------------------------------------------------------------
# Block detection
# ---------------------------------------------------------------------------


def _relative_change(new: pd.Series, old: pd.Series) -> pd.Series:
    denom = old.abs().where(old.abs() > 0)
    return (new - old).abs() / denom


def detect_blocks(
    new_df: pd.DataFrame,
    old_df: pd.DataFrame,
    symbol: str,
    provider: str,
    tol_rel: float = 1e-12,
    ratio_tolerance: float = 1e-9,
    max_gap_days: int = 20,
    max_skipped_rows: int = 5,
    block_col: str = "close",
) -> List[Block]:
    """Find contiguous runs of rewritten rows and classify each one.

    ``block_col`` is the column used to decide *what* changed (close is the least
    ambiguous: an intraday high/low can legitimately repeat to a different ULP).
    Ratios are still reported for every price column.
    """
    if new_df.empty or old_df.empty:
        return []
    merged = new_df.merge(old_df, on="date", suffixes=("_new", "_old"))
    if merged.empty:
        return []
    merged = merged.sort_values("date").reset_index(drop=True)

    rel = _relative_change(merged[f"{block_col}_new"], merged[f"{block_col}_old"])
    changed = rel > 0  # any bit-level difference at all
    material = rel > tol_rel

    blocks: List[Block] = []
    for rows in _runs(merged, changed, max_gap_days, max_skipped_rows):
        idx = merged.index[rows]
        # A run may mix ULP-only and material rows (e.g. a rescale that also
        # re-rounded a few untouched bars). Classify by the dominant population.
        mat_rows = [i for i in idx if material.iloc[i]]
        kind = "material_rescale" if mat_rows else "ulp_noise"
        if not mat_rows:
            sel = idx
        else:
            sel = pd.Index(mat_rows)
        blocks.append(
            _describe_block(
                merged,
                sel,
                idx,
                symbol,
                provider,
                kind,
                tol_rel,
                ratio_tolerance,
            )
        )
    return blocks


def _runs(
    merged: pd.DataFrame,
    mask: pd.Series,
    max_gap_days: int,
    max_skipped_rows: int = 0,
) -> List[List[int]]:
    """Yield the positions of each contiguous True run.

    Two tolerances keep a real rewrite from being shredded into dozens of blocks:

    * ``max_gap_days`` — calendar distance that splits a block (Spring Festival /
      National Day breaks are ~9-12 days, so the default is 20);
    * ``max_skipped_rows`` — number of *unchanged* rows allowed between two changed
      rows before the block is considered closed. Sporadic ULP-only re-rounding
      leaves a few bars untouched and would otherwise fragment one write into many.
    """
    out: List[List[int]] = []
    cur: List[int] = []
    skipped = 0
    prev: Optional[datetime.date] = None
    for pos, flag in enumerate(mask.tolist()):
        if not flag:
            if cur:
                skipped += 1
                if skipped > max_skipped_rows:
                    out.append(cur)
                    cur = []
                    skipped = 0
                    prev = None
            continue
        d = merged.at[pos, "date"]
        if cur and prev is not None and (d - prev).days > max_gap_days:
            out.append(cur)
            cur = []
        cur.append(pos)
        prev = d
        skipped = 0
    if cur:
        out.append(cur)
    return out


def _describe_block(
    merged: pd.DataFrame,
    sel: pd.Index,
    run: pd.Index,
    symbol: str,
    provider: str,
    kind: str,
    tol_rel: float,
    ratio_tolerance: float,
) -> Block:
    ratios: Dict[str, float] = {}
    spread: Dict[str, float] = {}
    max_rel = 0.0
    for col in _PRICE_COLS:
        new = merged.loc[sel, f"{col}_new"]
        old = merged.loc[sel, f"{col}_old"]
        r = (new / old.where(old != 0)).dropna()
        if r.empty:
            continue
        med = float(r.median())
        ratios[col] = med
        spread[col] = float((r / med - 1.0).abs().max()) if med else float("inf")
    for col in _PRICE_COLS:
        rel = _relative_change(merged.loc[sel, f"{col}_new"], merged.loc[sel, f"{col}_old"])
        if not rel.empty:
            max_rel = max(max_rel, float(rel.max()))

    close_ratio = ratios.get("close", 1.0)
    close_spread = spread.get("close", 0.0)
    if kind != "ulp_noise":
        if abs(close_ratio - 1.0) > ratio_tolerance and close_spread <= ratio_tolerance:
            kind = "material_rescale"
        elif close_spread > ratio_tolerance:
            kind = "rewrite"

    first_pos, last_pos = int(sel.min()), int(sel.max())
    left = merged.at[first_pos - 1, "date"] if first_pos - 1 >= 0 else None
    right = merged.at[last_pos + 1, "date"] if last_pos + 1 < len(merged) else None

    distortion: Dict[str, float] = {}
    if left is not None:
        distortion["left_pp"] = _return_distortion(merged, first_pos - 1, first_pos)
    if right is not None:
        distortion["right_pp"] = _return_distortion(merged, last_pos, last_pos + 1)

    return Block(
        symbol=symbol,
        provider=provider,
        start=merged.at[int(run.min()), "date"],
        end=merged.at[int(run.max()), "date"],
        rows=len(sel),
        kind=kind,
        ratios=ratios,
        ratio_spread=spread,
        max_rel_change=max_rel,
        left_anchor=left,
        right_anchor=right,
        boundary_distortion_pp=distortion,
        bounded=left is not None and right is not None,
    )


def _return_distortion(merged: pd.DataFrame, i_prev: int, i_next: int) -> float:
    """Return change (in percentage points) injected at one bar by the rewrite."""
    new_ret = merged.at[i_next, "close_new"] / merged.at[i_prev, "close_new"] - 1.0
    old_ret = merged.at[i_next, "close_old"] / merged.at[i_prev, "close_old"] - 1.0
    return (new_ret - old_ret) * 100.0


def detect_cross_provider_seams(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    symbol: str,
    provider: str,
    reference_provider: str,
    step_tolerance: float = 1e-6,
    max_gap_days: int = 20,
) -> List[Seam]:
    """Find *seams* where two provider namespaces disagree on price level.

    The two namespaces are fed by the same upstream chain and normally agree to the
    last bit, so their ratio ``r = close_a / close_b`` is flat. A QFQ re-basing that
    was applied to only part of one namespace shows up as a **step** in ``r``, and
    the step dates are exactly the interval edges the audit is looking for.

    Deliberately step-based rather than "deviation from the modal ratio": when a
    series is split roughly in half by one seam, the majority level is arbitrary and
    a deviation test would report the wrong side as the anomaly.
    """
    if df_a.empty or df_b.empty:
        return []
    merged = df_a.merge(df_b, on="date", suffixes=("_a", "_b")).sort_values("date")
    if len(merged) < 3:
        return []
    merged = merged.reset_index(drop=True)
    denom = merged["close_b"].where(merged["close_b"] > 0)
    merged["ratio"] = merged["close_a"] / denom
    if merged["ratio"].notna().sum() < 3:
        return []

    seams: List[Seam] = []
    step_idx: List[int] = []
    for i in range(1, len(merged)):
        r0, r1 = merged.at[i - 1, "ratio"], merged.at[i, "ratio"]
        if pd.isna(r0) or pd.isna(r1) or r0 == 0:
            continue
        if (merged.at[i, "date"] - merged.at[i - 1, "date"]).days > max_gap_days:
            continue  # do not chain a basis step across a long gap
        if abs(r1 / r0 - 1.0) <= step_tolerance:
            continue
        step_idx.append(i)

    for i in step_idx:
        r_before = _flat_level(merged, i - 1, -1, step_tolerance, max_gap_days)
        r_after = _flat_level(merged, i, +1, step_tolerance, max_gap_days)
        seams.append(
            Seam(
                symbol=symbol,
                provider=f"{provider}|{reference_provider}",
                date_before=merged.at[i - 1, "date"],
                date_after=merged.at[i, "date"],
                ratio_before=r_before,
                ratio_after=r_after,
                step_pp=(r_after / r_before - 1.0) * 100.0 if r_before else float("nan"),
            )
        )
    return seams


def _flat_level(
    merged: pd.DataFrame, start_pos: int, direction: int, tol: float, max_gap_days: int
) -> float:
    """Median ratio of the constant-level run that starts at ``start_pos``."""
    vals: List[float] = []
    base = merged.at[start_pos, "ratio"]
    if pd.isna(base):
        return float("nan")
    vals.append(float(base))
    i = start_pos + direction
    while 0 <= i < len(merged):
        prev_i = i - direction
        if (merged.at[i, "date"] - merged.at[prev_i, "date"]).days > max_gap_days:
            break
        r = merged.at[i, "ratio"]
        if pd.isna(r) or abs(r / base - 1.0) > tol:
            break
        vals.append(float(r))
        i += direction
    return float(pd.Series(vals).median())


def seam_windows(seams: Sequence[Seam]) -> List[Tuple[datetime.date, datetime.date, float]]:
    """Pair consecutive seams into the interval whose price level is off.

    Returns ``(start, end, level_offset_pp)`` for each *bounded* window; a single
    seam yields one open-ended window (needs external reference data to bound).
    """
    out: List[Tuple[datetime.date, datetime.date, float]] = []
    for a, b in zip(seams, seams[1:]):
        if a.symbol != b.symbol:
            continue
        if b.date_after <= a.date_after:
            continue
        out.append((a.date_after, b.date_before, a.step_pp))
    return out


# ---------------------------------------------------------------------------
# Direction judgement + whole-universe census
# ---------------------------------------------------------------------------

#: Below this the namespace is considered to agree with the reference level.
_REF_AGREE_TOL = 1e-6
#: A rewrite is only called damaged/fixed when one side is this many times better.
_DIRECTION_MARGIN = 3.0
#: Minimum reference sessions inside a block before a direction may be claimed.
_MIN_REF_ROWS = 20


def _ratio_level(df_a: pd.DataFrame, df_b: pd.DataFrame, start, end) -> Tuple[int, Optional[float], float]:
    """Median ``a/b`` close ratio over ``[start, end]``, plus its row count and spread.

    Measured on **the same sessions** for both the pre-incident and the current
    series, which is what makes the verdict robust: no reference coverage is needed
    outside the rewritten window.
    """
    if df_a.empty or df_b.empty:
        return 0, None, float("inf")
    merged = df_a.merge(df_b, on="date", suffixes=("_a", "_b"))
    merged = merged[(merged["date"] >= start) & (merged["date"] <= end)]
    if merged.empty:
        return 0, None, float("inf")
    denom = merged["close_b"].where(merged["close_b"] > 0)
    ratio = (merged["close_a"] / denom).dropna()
    if ratio.empty:
        return 0, None, float("inf")
    med = float(ratio.median())
    spread = float((ratio / med - 1.0).abs().max()) if med else float("inf")
    return len(ratio), med, spread


def judge_rewrite_direction(
    current: pd.DataFrame,
    baseline: pd.DataFrame,
    reference: pd.DataFrame,
    start: datetime.date,
    end: datetime.date,
    tol_ref: float = _REF_AGREE_TOL,
    margin: float = _DIRECTION_MARGIN,
    min_ref_rows: int = _MIN_REF_ROWS,
) -> Tuple[str, int, Optional[float], Optional[float], str]:
    """Decide whether a rewrite repaired or corrupted the series.

    The reference namespace is fed from the same upstream chain and normally agrees
    with ``provider`` to the last bit. So:

    * if the **current** series agrees with it and the pre-incident one does not,
      the write aligned the series → ``"fix"``;
    * if the **pre-incident** series agrees and the current one does not, the write
      moved a correct series off the reference → ``"damage"``;
    * otherwise → ``"undetermined"`` (and the reason says what is missing).
    """
    n, r_new, _ = _ratio_level(current, reference, start, end)
    _, r_old, _ = _ratio_level(baseline, reference, start, end)
    if n < min_ref_rows:
        return "undetermined", n, r_old, r_new, (
            f"reference namespace covers only {n} session(s) of this window "
            f"(need >= {min_ref_rows}); direction cannot be judged"
        )
    a_old = abs((r_old or 0.0) - 1.0)
    a_new = abs((r_new or 0.0) - 1.0)
    if a_new <= tol_ref:
        return "fix", n, r_old, r_new, (
            f"current level {r_new:.10f} matches the reference; the write cancelled a "
            f"{a_old * 100:.4f} % pre-existing offset"
        )
    if a_old <= tol_ref:
        return "damage", n, r_old, r_new, (
            f"pre-incident level was {r_old:.10f} (exactly on the reference); the write "
            f"left it at {r_new:.10f} ({a_new * 100:.4f} % off)"
        )
    if a_old * margin < a_new:
        return "damage", n, r_old, r_new, (
            f"pre-incident level {r_old:.10f} ({a_old * 100:.4f} %) was "
            f"{a_new / max(a_old, 1e-12):.1f}x closer to the reference than the current "
            f"{r_new:.10f} ({a_new * 100:.4f} %)"
        )
    if a_new * margin < a_old:
        return "fix", n, r_old, r_new, (
            f"current level {r_new:.10f} is {a_old / max(a_new, 1e-12):.1f}x closer to the "
            f"reference than the pre-incident {r_old:.10f}"
        )
    return "undetermined", n, r_old, r_new, (
        f"neither level agrees with the reference (old {r_old:.10f}, new {r_new:.10f}) "
        f"and neither dominates by {margin}x"
    )


def census(
    con_current: duckdb.DuckDBPyConnection,
    con_baseline: duckdb.DuckDBPyConnection,
    provider: str = _DEFAULT_PROVIDER,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    symbols: Optional[Sequence[str]] = None,
    table: str = "ohlcv_bars",
    baseline_table: Optional[str] = None,
    baseline_label: str = "",
    **kwargs: Any,
) -> List[RewriteRecord]:
    """Scan **every** symbol of ``provider`` for rewrites and judge each one's direction.

    No sampling: the symbol list comes from the database unless ``symbols`` is given.
    Rewritten rows, constant scalar, ULP spread, untouched volume and the
    reference-level comparison are all recorded per block so the table is auditable
    without re-running anything.
    """
    if symbols is None:
        symbols = [
            r[0]
            for r in con_current.execute(
                f"SELECT DISTINCT symbol FROM {table} WHERE provider = ? ORDER BY 1",
                [provider],
            ).fetchall()
        ]
    base_table = baseline_table or table
    records: List[RewriteRecord] = []
    for sym in symbols:
        current = load_bars(con_current, provider, sym)
        baseline = load_bars(con_baseline, provider, sym, table=base_table)
        if current.empty or baseline.empty:
            continue
        reference = load_bars(con_current, reference_provider, sym)
        # Volume is compared on the intersection: the sync may also append rows, and
        # a row-count difference is not a turnover change.
        vol = current.merge(baseline, on="date", suffixes=("_c", "_b"))
        volume_same = bool((vol["volume_c"].values == vol["volume_b"].values).all())
        for b in detect_blocks(current, baseline, sym, provider, **kwargs):
            if b.kind not in ("material_rescale", "rewrite"):
                continue
            direction, n, r_old, r_new, reason = judge_rewrite_direction(
                current, baseline, reference, b.start, b.end
            )
            records.append(
                RewriteRecord(
                    symbol=sym,
                    provider=provider,
                    start=b.start,
                    end=b.end,
                    rows=b.rows,
                    kind=b.kind,
                    factor=float(b.ratios.get("close", float("nan"))),
                    spread=float(b.ratio_spread.get("close", float("nan"))),
                    volume_unchanged=volume_same,
                    bounded=b.bounded,
                    left_pp=b.boundary_distortion_pp.get("left_pp"),
                    right_pp=b.boundary_distortion_pp.get("right_pp"),
                    ref_rows=n,
                    ratio_old=r_old,
                    ratio_new=r_new,
                    direction=direction,
                    reason=reason,
                    baseline=baseline_label,
                )
            )
    return records


def census_coverage(
    con: duckdb.DuckDBPyConnection,
    provider: str = _DEFAULT_PROVIDER,
    reference_provider: str = _DEFAULT_REFERENCE_PROVIDER,
    table: str = "ohlcv_bars",
    min_ref_rows: int = _MIN_REF_ROWS,
) -> Dict[str, Any]:
    """How much of ``provider``'s universe the reference namespace can actually judge.

    A symbol with fewer than ``min_ref_rows`` overlapping reference sessions cannot be
    direction-judged at all. That is not a defect of the census — it is the boundary of
    what the evidence can support, and it is reported instead of being assumed away.
    """
    rows = con.execute(
        f"SELECT o.symbol, count(r.date) FROM "
        f"(SELECT DISTINCT symbol FROM {table} WHERE provider = ?) o "
        f"LEFT JOIN {table} r ON r.symbol = o.symbol AND r.provider = ? "
        f"GROUP BY 1 ORDER BY 1",
        [provider, reference_provider],
    ).fetchall()
    symbols = [r[0] for r in rows]
    no_ref = [r[0] for r in rows if not r[1]]
    thin = [r[0] for r in rows if r[1] and r[1] < min_ref_rows]
    return {
        "symbols": len(symbols),
        "with_reference": len(symbols) - len(no_ref),
        "no_reference": no_ref,
        "thin_reference": thin,
        "judgeable": len(symbols) - len(no_ref) - len(thin),
    }


def render_census(records: Sequence[RewriteRecord], db: str, baseline: str) -> str:
    lines = [
        "## QFQ whole-universe census",
        "",
        f"current: `{db}` — baseline: `{baseline}`",
        "",
    ]
    if not records:
        lines.append("no material rewrite found in any symbol.")
        return "\n".join(lines)
    lines.append(
        "| Symbol | Baseline | Start | End | Rows | Kind | Factor | Spread | Volume same "
        "| Left pp | Right pp | Ref rows | Ratio old | Ratio new | Direction |"
    )
    lines.append(
        "| :--- | :--- | :--- | :--- | ---: | :--- | ---: | ---: | :---: | ---: | ---: "
        "| ---: | ---: | ---: | :--- |"
    )
    for r in records:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {:.10f} | {:.2e} | {} | {} | {} | {} | {} "
            "| {} | **{}** |".format(
                r.symbol,
                r.baseline or "-",
                _iso(r.start),
                _iso(r.end),
                r.rows,
                r.kind,
                r.factor,
                r.spread,
                "yes" if r.volume_unchanged else "**NO**",
                _fmt_pp(r.left_pp),
                _fmt_pp(r.right_pp),
                r.ref_rows,
                "-" if r.ratio_old is None else f"{r.ratio_old:.10f}",
                "-" if r.ratio_new is None else f"{r.ratio_new:.10f}",
                r.direction,
            )
        )
    counts = {"damage": 0, "fix": 0, "undetermined": 0}
    for r in records:
        counts[r.direction] = counts.get(r.direction, 0) + 1
    lines.append("")
    lines.append(
        "summary: {} rewritten block(s) over {} symbol(s) — **{} damage**, **{} fix**, "
        "**{} undetermined**; {} row(s) rewritten.".format(
            len(records),
            len({r.symbol for r in records}),
            counts["damage"],
            counts["fix"],
            counts["undetermined"],
            sum(r.rows for r in records),
        )
    )
    lines.append("")
    lines.append("### direction evidence")
    for r in records:
        lines.append(f"- `{r.symbol}` **{r.direction}**: {r.reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def is_anomaly(finding: Any) -> bool:
    """Only material (non-ULP) findings count as an anomaly."""
    if isinstance(finding, Seam):
        return abs(finding.step_pp) > 0.0
    return finding.kind in {"material_rescale", "rewrite"}


def summarise(findings: Sequence[Any]) -> Dict[str, Any]:
    anomalies = [f for f in findings if is_anomaly(f)]
    if findings and isinstance(findings[0], Seam):
        # Seam mode reports intervals, not rows: `rows_affected` is not knowable
        # without the reference series' coverage, so it stays None instead of lying.
        bounded = len(seam_windows(anomalies))
        rows: Optional[int] = None
    else:
        rows = sum(f.rows for f in anomalies)
        bounded = len([f for f in anomalies if f.bounded])
    return {
        "findings": len(findings),
        "anomalies": len(anomalies),
        "bounded_anomalies": bounded,
        "rows_affected": rows,
        "symbols_affected": sorted({f.symbol for f in anomalies}),
    }


def render_markdown(blocks: Sequence[Block], db: Optional[str], title: str) -> str:
    lines = [f"## {title}", ""]
    if db:
        lines.append(f"cache: `{db}`")
        lines.append("")
    if not blocks:
        lines.append("no rewritten rows found in the audited range.")
        return "\n".join(lines)
    lines.append(
        "| Symbol | Provider | Start | End | Rows | Kind | Close ratio | Ratio spread "
        "| Max rel change | Bounded | Left edge pp | Right edge pp |"
    )
    lines.append(
        "| :--- | :--- | :--- | :--- | ---: | :--- | ---: | ---: | ---: | :---: | ---: | ---: |"
    )
    for b in blocks:
        lines.append(
            "| `{}` | `{}` | {} | {} | {} | **{}** | {} | {:.2e} | {:.2e} | {} | {} | {} |".format(
                b.symbol,
                b.provider,
                _iso(b.start),
                _iso(b.end),
                b.rows,
                b.kind,
                "-" if "close" not in b.ratios else f"{b.ratios['close']:.10f}",
                b.ratio_spread.get("close", 0.0),
                b.max_rel_change,
                "yes" if b.bounded else "no",
                _fmt_pp(b.boundary_distortion_pp.get("left_pp")),
                _fmt_pp(b.boundary_distortion_pp.get("right_pp")),
            )
        )
    total = summarise(blocks)
    lines.append("")
    lines.append(
        "summary: {} block(s), {} material anomal(ies), {} of them bounded, {} row(s), symbols: {}".format(
            total["findings"],
            total["anomalies"],
            total["bounded_anomalies"],
            total["rows_affected"],
            ", ".join(total["symbols_affected"]) or "-",
        )
    )
    return "\n".join(lines)


def render_seams(seams: Sequence[Seam], db: Optional[str], title: str) -> str:
    lines = [f"## {title}", ""]
    if db:
        lines.append(f"cache: `{db}`")
        lines.append("")
    if not seams:
        lines.append(
            "no ratio seams found: the audited namespace and the reference namespace agree "
            "at every overlapping session."
        )
        return "\n".join(lines)
    lines.append("| Symbol | Namespaces | Seam (before → after) | Ratio before | Ratio after | Step |")
    lines.append("| :--- | :--- | :--- | ---: | ---: | ---: |")
    for s in seams:
        lines.append(
            "| `{}` | `{}` | {} → {} | {:.10f} | {:.10f} | {} |".format(
                s.symbol,
                s.provider,
                _iso(s.date_before),
                _iso(s.date_after),
                s.ratio_before,
                s.ratio_after,
                _fmt_pp(s.step_pp),
            )
        )
    for sym in sorted({s.symbol for s in seams}):
        wins = seam_windows([s for s in seams if s.symbol == sym])
        for w_start, w_end, offset in wins:
            lines.append("")
            lines.append(
                f"bounded window `{sym}`: {_iso(w_start)} → {_iso(w_end)} sits "
                f"{offset:+.4f} pp off the reference level."
            )
    total = summarise(seams)
    lines.append("")
    lines.append(
        "summary: {} seam(s) across {} symbol(s); {} bounded window(s) carry a level "
        "error.".format(total["findings"], len(total["symbols_affected"]), total["bounded_anomalies"])
    )
    return "\n".join(lines)


def _fmt_pp(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:+.4f}"


def render_verdict(findings: Sequence[Any]) -> List[str]:
    """Interpretation notes, so the report is not just numbers."""
    notes: List[str] = []
    for b in findings:
        if isinstance(b, Seam):
            notes.append(
                f"`{b.symbol}` seam at {_iso(b.date_after)}: ratio {b.ratio_before:.10f} → "
                f"{b.ratio_after:.10f} ({b.step_pp:+.4f} pp). Both namespaces are fed from "
                f"the same upstream chain, so a step is a realignment, not a market move."
            )
            continue
        if b.kind == "ulp_noise":
            notes.append(
                f"`{b.symbol}` {_iso(b.start)}→{_iso(b.end)}: ULP-only rewrite "
                f"(max rel {b.max_rel_change:.2e}) — a write happened, no price moved."
            )
        elif b.kind == "material_rescale":
            notes.append(
                f"`{b.symbol}` {_iso(b.start)}→{_iso(b.end)}: constant ×{b.ratios.get('close', 0):.10f} "
                f"over {b.rows} rows, spread {b.ratio_spread.get('close', 0):.2e}; "
                f"{'bounded' if b.bounded else 'open-ended'} → local rescale of stored floats "
                f"(an upstream QFQ re-fetch would jitter per row)."
            )
        else:
            notes.append(
                f"`{b.symbol}` {_iso(b.start)}→{_iso(b.end)}: non-constant rewrite "
                f"(ratio spread {b.ratio_spread.get('close', 0):.2e}) over {b.rows} rows."
            )
    if any(
        isinstance(b, Block) and b.bounded and is_anomaly(b) for b in findings
    ):
        notes.append(
            "A *bounded* non-unit rescale is not a corporate-action footprint: a genuine "
            "ex-dividend re-basing applies to every bar before the ex-date. The bars that "
            "carry the injected error are the two returns straddling each edge "
            "(left/right pp above); returns inside and outside are unaffected."
        )
    if any(isinstance(b, Seam) for b in findings):
        notes.append(
            "Seam-based check: the two namespaces are fed by the same upstream chain, so a "
            "flat ratio is the healthy state. A pair of seams bounds the interval whose "
            "price level was realigned; only that interval (and the two returns straddling "
            "each seam) is affected — outside it both namespaces still agree."
        )
    return notes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Read-only audit of QFQ realignment artefacts in a DuckDB price cache "
            "(development_plan L2.8). Never writes, never goes online."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=_DEFAULT_DB, help="cache to audit (opened read_only)")
    p.add_argument(
        "--baseline",
        default=None,
        help=(
            "earlier snapshot to diff against (opened read_only). Omit to run "
            "cross-provider mode against --reference-provider."
        ),
    )
    p.add_argument("--symbol", action="append", dest="symbols", default=None,
                   help=f"symbol to audit, repeatable (default: {' '.join(_DEFAULT_SYMBOLS)})")
    p.add_argument("--provider", default=_DEFAULT_PROVIDER, help="provider namespace to audit")
    p.add_argument("--reference-provider", default=_DEFAULT_REFERENCE_PROVIDER,
                   help="second namespace used by cross-provider mode")
    p.add_argument("--start", default=None, help="inclusive start date (YYYY-MM-DD)")
    p.add_argument("--end", default=None, help="inclusive end date (YYYY-MM-DD)")
    p.add_argument("--tol-rel", type=float, default=1e-12,
                   help="relative price change below this counts as ULP noise (default 1e-12)")
    p.add_argument("--ratio-tolerance", type=float, default=1e-9,
                   help="max spread for a block to count as a single constant scalar (default 1e-9)")
    p.add_argument("--max-gap-days", type=int, default=20,
                   help="calendar gap that splits a block, e.g. holidays (default 20)")
    p.add_argument("--max-skipped-rows", type=int, default=5,
                   help="unchanged rows tolerated inside one block (default 5)")
    p.add_argument("--step-tolerance", type=float, default=1e-6,
                   help="cross-provider mode: relative ratio step that counts as a seam (default 1e-6)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a markdown report")
    p.add_argument("--census", action="store_true",
                   help=(
                       "whole-universe mode: diff EVERY symbol of --provider against "
                       "--baseline and judge each rewrite's direction (damage/fix/"
                       "undetermined). Requires --baseline."
                   ))
    p.add_argument("--additional-baseline", action="append", dest="additional_baselines",
                   default=None, metavar="PATH",
                   help=(
                       "further snapshot, scanned after --baseline (repeatable). Needed "
                       "for --census: the newest snapshot can already contain the write, "
                       "so only the oldest one proves what was changed when"
                   ))
    p.add_argument("--all-symbols", action="store_true",
                   help="expand the symbol list from the database instead of the defaults")
    return p.parse_args(argv)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    symbols = args.symbols or list(_DEFAULT_SYMBOLS)

    if args.census:
        if not args.baseline:
            print("error: --census requires --baseline (a pre-incident snapshot)", file=sys.stderr)
            return EXIT_ERROR
        baseline_paths = [args.baseline, *(args.additional_baselines or [])]
        try:
            con_cur = connect_read_only(args.db)
        except (FileNotFoundError, duckdb.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        records: List[RewriteRecord] = []
        coverage: Dict[str, Any] = {}
        per_baseline: Dict[str, int] = {}
        try:
            coverage = census_coverage(con_cur, args.provider, args.reference_provider)
            seen: set = set()
            for path in baseline_paths:
                label = os.path.basename(path)
                con_base = connect_read_only(path)
                try:
                    found = census(
                        con_cur,
                        con_base,
                        args.provider,
                        args.reference_provider,
                        None if args.all_symbols or not args.symbols else symbols,
                        baseline_label=label,
                        tol_rel=args.tol_rel,
                        ratio_tolerance=args.ratio_tolerance,
                        max_gap_days=args.max_gap_days,
                        max_skipped_rows=args.max_skipped_rows,
                    )
                finally:
                    con_base.close()
                per_baseline[label] = len(found)
                for r in found:
                    # The same block is visible from every snapshot older than the write;
                    # the oldest snapshot is the informative one (it proves the block was
                    # *not* there before), so the first sighting wins.
                    key = (r.symbol, r.start, r.end)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(r)
        except (LookupError, duckdb.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        finally:
            con_cur.close()
        if args.json:
            counts: Dict[str, int] = {}
            for r in records:
                counts[r.direction] = counts.get(r.direction, 0) + 1
            print(
                json.dumps(
                    {
                        "mode": "census",
                        "db": args.db,
                        "baselines": baseline_paths,
                        "per_baseline_blocks": per_baseline,
                        "provider": args.provider,
                        "reference_provider": args.reference_provider,
                        "coverage": coverage,
                        "counts": counts,
                        "records": [r.as_dict() for r in records],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(render_census(records, args.db, ", ".join(baseline_paths)))
            print("")
            print("### coverage of the direction judgement")
            print(
                "- symbols of `{}`: **{}** — with reference overlap: {:,} → judgeable: "
                "**{}** (needs >= {} overlapping sessions); no reference at all: {}; "
                "too few sessions: {}".format(
                    args.provider,
                    coverage["symbols"],
                    coverage["with_reference"],
                    coverage["judgeable"],
                    _MIN_REF_ROWS,
                    len(coverage["no_reference"]),
                    len(coverage["thin_reference"]),
                )
            )
            if coverage["no_reference"]:
                print(
                    "- no-reference symbols (a rewrite there would be *undetermined*, "
                    "nothing to compare against): "
                    + ", ".join(f"`{s}`" for s in coverage["no_reference"])
                )
            print(
                "- blocks seen per baseline: "
                + ", ".join(f"`{k}`: {v}" for k, v in per_baseline.items())
            )
            undet = [r for r in records if r.direction == "undetermined"]
            missing = [
                r for r in records
                if r.ref_rows < _MIN_REF_ROWS
            ]
            print(
                "- **undetermined blocks: {}**{}".format(
                    len(undet),
                    "" if not undet else " — " + "; ".join(
                        f"`{r.symbol}` ({r.ref_rows} ref sessions: {r.reason})"
                        for r in undet
                    ),
                )
            )
            if not missing and not undet:
                print(
                    "- no block was left unjudged, i.e. every rewritten block had "
                    "enough reference overlap for a level comparison."
                )
        return EXIT_ANOMALY if any(r.direction == "damage" for r in records) else EXIT_OK

    con_new = con_base = None
    findings: List[Any] = []
    try:
        con_new = connect_read_only(args.db)
        if args.all_symbols:
            symbols = [
                r[0]
                for r in con_new.execute(
                    "SELECT DISTINCT symbol FROM ohlcv_bars WHERE provider = ? ORDER BY 1",
                    [args.provider],
                ).fetchall()
            ]
        if args.baseline:
            con_base = connect_read_only(args.baseline)
            for sym in symbols:
                new_df = load_bars(con_new, args.provider, sym, args.start, args.end)
                old_df = load_bars(con_base, args.provider, sym, args.start, args.end)
                if new_df.empty and old_df.empty:
                    raise LookupError(f"no bars for {args.provider}/{sym} in either file")
                findings.extend(
                    detect_blocks(
                        new_df,
                        old_df,
                        sym,
                        args.provider,
                        tol_rel=args.tol_rel,
                        ratio_tolerance=args.ratio_tolerance,
                        max_gap_days=args.max_gap_days,
                        max_skipped_rows=args.max_skipped_rows,
                    )
                )
            mode = "diff"
            title = f"QFQ diff audit ({args.provider})"
            db_desc = f"{args.db} vs baseline {args.baseline}"
        else:
            for sym in symbols:
                a = load_bars(con_new, args.provider, sym, args.start, args.end)
                b = load_bars(con_new, args.reference_provider, sym, args.start, args.end)
                if a.empty or b.empty:
                    continue  # no comparable pair for this symbol
                findings.extend(
                    detect_cross_provider_seams(
                        a,
                        b,
                        sym,
                        args.provider,
                        args.reference_provider,
                        step_tolerance=args.step_tolerance,
                        max_gap_days=args.max_gap_days,
                    )
                )
            mode = "cross-provider"
            title = f"QFQ cross-provider audit ({args.provider} vs {args.reference_provider})"
            db_desc = args.db
    except (FileNotFoundError, LookupError, duckdb.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        for con in (con_base, con_new):
            if con is not None:
                con.close()

    total = summarise(findings)
    if args.json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "symbols": symbols,
                    "summary": total,
                    "findings": [f.as_dict() for f in findings],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if mode == "diff":
            print(render_markdown(findings, db_desc, title))
        else:
            print(render_seams(findings, db_desc, title))
        notes = render_verdict(findings)
        if notes:
            print("\n### verdict")
            for n in notes:
                print(f"- {n}")
        print(
            f"\nexit: {'1 (ANOMALY)' if total['anomalies'] else '0 (clean)'}"
            f" — {total['anomalies']} material finding(s)"
        )
    return EXIT_ANOMALY if total["anomalies"] else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

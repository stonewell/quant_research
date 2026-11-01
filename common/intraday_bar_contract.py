"""M3.3 -- the 5m calibration bar contract: the six assertions, as pure functions.

Why this module exists (history, load-bearing): the M3.3 gate was originally written inside
`backtests_research/2026-10-26_window_vwap/probe_5m.py`. That directory is a *research artifact*
and is gitignored on purpose (see `.gitignore`, `/backtests_research/**`), so the gate's logic --
the only thing that decides whether a full 5m calibration may start -- was untracked while the
test suite that claimed to cover it was tracked. A clean clone therefore could not run the gate
or its tests. This module is the tracked home of every assertion; the probe script is now a thin
CLI shim over it (`python backtests_research/2026-10-26_window_vwap/probe_5m.py --help`).

CONTRACT (what a caller must know):

  * **DataFrames in, DataFrames in.** Nothing here opens a database, imports `duckdb`,
    `common.duckdb_cache` or `common.financial_api`, touches the network, or writes the
    production cache. The caller does the SQL and hands over two plain frames:
      - `bars`  : `symbol, ts, open, high, low, close, volume, amount` (`ts` datetime-like;
                  `adjustflag` optional and unused).
      - `daily` : `symbol, date, open, high, low, close, volume, amount` (`date` a python
                  `datetime.date`). `amount` may be entirely NaN -- A5 then takes its explicitly
                  disclosed degraded path.
    Both are expected numeric-coerced by the loader; the checks coerce defensively anyway.
  * **The only file this module writes is the provenance pin** (`provenance.json` in `out_dir`,
    requirement 4). It is never rewritten once a value is present; a conflict fails the
    assertion instead. No other function writes anything.
  * **`amount` is never derived.** `amount` must come from the DB column; `volume * close` is
    the forbidden substitute (requirement 7) and `_guard_amount_not_reconstructed` additionally
    trips on the fingerprint of a column that *was* rebuilt that way.
  * **Failure is data, not an exception.** Each `check_*` returns `(block, violations_frame)`
    where `block["passed"]` is a bool. Only un-runnable inputs (no `amount` column, a corrupt
    pin, a bad whitelist file) raise `ProbeGateError`; the CLI turns those into a fail-closed
    report so the gate still refuses to open.
  * `finalize_report` is the A6 aggregate: it decides `all_passed` / `exit_code` and fails
    closed, including when an assertion block is missing because of an earlier hard error.

The seven `M3.3` requirements map 1:1 onto the block names used in `probe_report.json`:
`A1_amount_coverage`, `A2_implied_vwap_range`, `A3_shape`, `A4_timestamp_convention`,
`A5_amount_reconciliation`, `A6_gate`, plus requirement 7 enforced by
`_guard_amount_not_reconstructed`.

HONEST CAVEATS (disclosed rather than buried):
  * `suspended` vs `missing` relies on the daily table's `volume` for that symbol/date. With no
    daily row at all the day is `unknown_no_daily_bar` and counts as a FAILURE, because
    "we cannot tell" is not the same as "the stock was halted".
  * The whitelist is matched on (date, HH:MM window, symbol set). Its `expected_rows` is reported
    next to the actual match count; a mismatch is disclosed but is not itself a gate failure --
    re-downloads can legitimately change the row count of a known bad window by a few bars, and
    turning that into a red light would train people to widen the whitelist.
  * A4's magnitude cross-check is coarse (default 5%) on purpose: it is a *units/scale* smoke
    test (are we comparing CNY-per-share with CNY-per-share?), not a second reconciliation. The
    strict 1% / 2% reconciliation is A5.
  * Shards are contiguous slices of the symbol's own sorted trading dates, so a shard is a time
    window rather than a fixed calendar period (universe start dates differ).
  * A4 fails a START-labelled DB instead of pinning it: the pin records the contract the study
    calibrated on (END), so recording a shifted grid would legitimise a different basis.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

import pandas as pd

# Version of the *contract* (constants + assertion semantics), bumped whenever a threshold, a
# classification, or the report shape changes in a way a reader of probe_report.json could notice.
CONTRACT_VERSION = "m3.3.1"

REPORT_NAME = "probe_report.json"
VIOLATIONS_NAME = "probe_violations.csv"
PROVENANCE_NAME = "provenance.json"
PROVENANCE_KEY = "bar_timestamp_convention"
PROVENANCE_NOTE_KEY = "bar_timestamp_convention_note"

INTRADAY_TABLE = "intraday_5m"
DAILY_TABLE = "daily_1d"

EXPECTED_BARS_PER_DAY = 48
EXPECTED_CONVENTION = "END"          # A4: label t covers (t-5m, t] -- the calibration contract

AMOUNT_COVERAGE_MIN = 0.995          # A1
VWAP_RANGE_REL_TOL = 1e-6            # A2: float slack only, not a fudge factor
AMOUNT_RECON_MEDIAN_MAX = 0.01       # A5 primary path
VWAP_PROXY_MEDIAN_MAX = 0.02         # A5 degraded path
MAGNITUDE_MEDIAN_MAX = 0.05          # A4 coarse cross-check
DEFAULT_SHARDS = 4

LUNCH_START = "11:30"
LUNCH_END = "13:00"
SESSION_CLOSE = "15:00"

# `amount` provenance string, recorded in the report so a reader can prove where it came from.
AMOUNT_SOURCE = "db:intraday_5m.amount"

# Every assertion block the gate refuses to open without. Order is the report order.
REQUIRED_ASSERTIONS = (
    "A1_amount_coverage",
    "A2_implied_vwap_range",
    "A3_shape",
    "A4_timestamp_convention",
    "A5_amount_reconciliation",
)

# Violation-CSV schema, shared by all three row-producing checks so frames can be concatenated
# without a dtype surprise.
VIOLATION_COLUMNS = (
    "check", "symbol", "ts", "date", "time", "shard", "reason", "detail",
    "amount", "volume", "whitelisted", "whitelist_id",
)

# Known-and-accepted violations. Everything here is *reported* (match counts per entry), never
# silently skipped: the point of the whitelist is to keep a known data-source event from turning
# the gate permanently red, while still printing that it happened.
DEFAULT_WHITELIST: list[dict] = [
    {
        "id": "wl_2024-09-27_1030_1120_zero_volume",
        "date": "2024-09-27",
        "time_from": "10:30",
        "time_to": "11:20",
        "symbols": ["*"],
        "expected_rows": 155,
        "reason": (
            "Known 155 rows with volume<=0 (and amount=0) on 2024-09-27 10:30-11:20 across 18 "
            "symbols: the upstream BaoStock 5m feed returned zero volume/amount for that window. "
            "It is a real upstream return, not a local null-fill, so it is whitelisted by "
            "(date, HH:MM, symbol) instead of being ignored -- probe_report.json prints the "
            "match count for this entry and probe_violations.csv keeps every row."
        ),
    },
]


class ProbeGateError(RuntimeError):
    """Raised when the assertions cannot even be evaluated (missing `amount`, bad pin/whitelist)."""


def empty_violations() -> pd.DataFrame:
    """An empty frame with the violation-CSV schema (stable column order, no dtype guessing)."""
    return pd.DataFrame(columns=list(VIOLATION_COLUMNS))


def build_report(*, out_dir: str, db_path: str | None = None,
                 shards: int = DEFAULT_SHARDS,
                 symbols_filter: list[str] | None = None,
                 probe_name: str = "probe_5m.py") -> dict:
    """The report skeleton (`probe_report.json` before any assertion runs).

    Kept here, not in the CLI shim, so the artifact shape is part of the tracked contract: a
    consumer of `probe_report.json` can rely on these keys existing even on a fail-closed run.
    """
    return {
        "schema_version": 1,
        "probe": probe_name,
        "probe_version": CONTRACT_VERSION,
        "task": "M3.3 (5m calibration probe hardened into a gate)",
        "generated_at_local": pd.Timestamp.now().isoformat(timespec="seconds"),
        "db_path": os.path.abspath(db_path) if db_path else None,
        "out_dir": os.path.abspath(out_dir),
        "shards": int(shards),
        "symbols_filter": list(symbols_filter) if symbols_filter else None,
        "assertions": {},
        "all_passed": False,
    }


# ---------------------------------------------------------------------------------------------
# session grid
# ---------------------------------------------------------------------------------------------
def _grid_labels(end_labelled: bool) -> list[str]:
    """48 A-share 5m bar labels, END- or START-labelled.

    END (BaoStock's convention): 09:35..11:30 and 13:05..15:00 -- label t covers (t-5m, t].
    START would be 09:30..11:25 and 13:00..14:55.
    """
    labels: list[str] = []
    for open_, close in (("09:30", "11:30"), ("13:00", "15:00")):
        t = pd.Timestamp(f"2000-01-01 {open_}")
        end = pd.Timestamp(f"2000-01-01 {close}")
        while t < end:
            labels.append((t + pd.Timedelta(minutes=5) if end_labelled else t).strftime("%H:%M"))
            t += pd.Timedelta(minutes=5)
    return labels


END_LABELS = _grid_labels(True)
START_LABELS = _grid_labels(False)
assert len(END_LABELS) == len(START_LABELS) == EXPECTED_BARS_PER_DAY, "session grid must be 48"


# ---------------------------------------------------------------------------------------------
# requirement 7 -- `amount` must come from the DB, never from volume x close
# ---------------------------------------------------------------------------------------------
def _guard_amount_not_reconstructed(bars: pd.DataFrame) -> None:
    """Requirement 7 guard: `amount` must come from the DB column, never from volume x close.

    Why this is a hard rule: `volume * close` reproduces a *self-consistent* turnover series that
    reconciles perfectly against itself and against any VWAP proxy, so every downstream assertion
    would pass while the number is simply wrong (it also poisons the series with adjusted prices
    when the basis is qfq/hfq). This module therefore never derives `amount`, and additionally
    trips on the fingerprint of a reconstructed column: if every usable row satisfies
    `amount == volume * close` to 1e-12 relative, the column was rebuilt, not downloaded.
    """
    if "amount" not in bars.columns:
        raise ProbeGateError(
            "intraday_5m has no `amount` column: true VWAP is not computable and this probe "
            "REFUSES to substitute volume*close (that is the forbidden path in M3.3 item 7). "
            "Re-download into the isolated calibration DB (see download_5m.py)."
        )
    if bars.empty:
        return
    amt = pd.to_numeric(bars["amount"], errors="coerce")
    vol = pd.to_numeric(bars["volume"], errors="coerce")
    close = pd.to_numeric(bars["close"], errors="coerce")
    usable = amt.notna() & vol.notna() & close.notna() & (vol > 0) & (amt > 0)
    if int(usable.sum()) < 20:  # too few rows to call it a fingerprint either way
        return
    recon = (amt[usable] - vol[usable] * close[usable]).abs() <= (1e-12 + 1e-12 * amt[usable].abs())
    if bool(recon.all()):
        raise ProbeGateError(
            "amount looks reconstructed as volume*close on every sampled row (max rel diff <= "
            "1e-12). That is the explicitly forbidden path (M3.3 item 7): re-download the "
            "isolated calibration DB with a real `amount` column instead."
        )


# ---------------------------------------------------------------------------------------------
# whitelist
# ---------------------------------------------------------------------------------------------
def load_whitelist(path: str | None, *, default_path: str | None = None) -> tuple[list[dict], str]:
    """Built-in default entries, optionally extended by a JSON file. Returns (entries, source).

    A *missing* default path is tolerated (the built-in entry is the baseline); a missing
    explicitly-requested path raises. `default_path` lets the caller name its own default location
    without this module knowing about research directories.

    Never writes anything.
    """
    entries = [dict(e) for e in DEFAULT_WHITELIST]
    source = "builtin"
    if not path:
        return entries, source
    if not os.path.exists(path):
        if default_path is not None and os.path.abspath(path) == os.path.abspath(default_path):
            return entries, source  # default location simply not present
        raise ProbeGateError(f"--whitelist file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        extra = json.load(fh)
    if isinstance(extra, dict):
        extra = extra.get("entries", [])
    if not isinstance(extra, list):
        raise ProbeGateError(f"--whitelist must be a JSON list (or {{'entries': [...]}}): {path}")
    for i, e in enumerate(extra):
        if "id" not in e or "date" not in e:
            raise ProbeGateError(f"whitelist entry #{i} needs at least `id` and `date`: {e!r}")
        entries.append(dict(e))
    return entries, f"builtin+{os.path.abspath(path)}"


def _entry_mask(df: pd.DataFrame, entry: dict) -> pd.Series:
    mask = df["date"].astype(str) == str(entry["date"])
    if entry.get("time_from"):
        mask &= df["time"] >= str(entry["time_from"])
    if entry.get("time_to"):
        mask &= df["time"] <= str(entry["time_to"])
    syms = entry.get("symbols") or ["*"]
    if "*" not in syms:
        mask &= df["symbol"].isin(syms)
    return mask


def _whitelist_assign(df: pd.DataFrame, entries: list[dict]) -> tuple[pd.Series, dict]:
    """Return (first-matching-entry-id per row, {entry_id: matched rows})."""
    hit = pd.Series([None] * len(df), index=df.index, dtype=object)
    counts: dict[str, int] = {}
    for entry in entries:
        mask = _entry_mask(df, entry)
        counts[entry["id"]] = int(mask.sum())
        hit = hit.mask(mask & hit.isna(), entry["id"])
    return hit, counts


# ---------------------------------------------------------------------------------------------
# sharding
# ---------------------------------------------------------------------------------------------
def _shard_edges(dates: Iterable, n_shards: int) -> list[tuple]:
    """Contiguous slices of the symbol's own sorted trading dates (see HONEST CAVEATS)."""
    ds = sorted(set(dates))
    if not ds:
        return []
    n = max(1, min(int(n_shards), len(ds)))
    base, extra = divmod(len(ds), n)
    edges, start = [], 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        if size <= 0:
            continue
        chunk = ds[start:start + size]
        edges.append((chunk[0], chunk[-1]))
        start += size
    return edges


def _assign_shard(date_series: pd.Series, edges: list[tuple]) -> pd.Series:
    out = pd.Series([-1] * len(date_series), index=date_series.index, dtype=int)
    for i, (lo, hi) in enumerate(edges):
        out = out.mask((date_series >= lo) & (date_series <= hi), i)
    return out


# ---------------------------------------------------------------------------------------------
# A1 -- amount non-null rate, per symbol x shard
# ---------------------------------------------------------------------------------------------
def check_amount_coverage(bars: pd.DataFrame, *, n_shards: int, whitelist: list[dict],
                          threshold: float = AMOUNT_COVERAGE_MIN) -> tuple[dict, pd.DataFrame]:
    block: dict = {
        "name": "A1_amount_coverage",
        "threshold": threshold,
        "scope": "per symbol x shard",
        "violations_csv": VIOLATIONS_NAME,
    }
    empty = empty_violations()
    if bars.empty:
        block.update({"passed": False, "reason": "no 5m bars in the calibration DB"})
        return block, empty

    d = bars[["symbol", "ts", "amount", "volume"]].copy()
    d["date"] = d["ts"].dt.date
    d["time"] = d["ts"].dt.strftime("%H:%M")
    amt = pd.to_numeric(d["amount"], errors="coerce")
    vol = pd.to_numeric(d["volume"], errors="coerce")
    d["_ok"] = amt > 0
    d["shard"] = _assign_shard(d["date"], _shard_edges(d["date"], n_shards))

    viol = d.loc[~d["_ok"]].copy()
    viol["amount"] = amt.loc[viol.index]
    viol["volume"] = vol.loc[viol.index]
    viol["reason"] = "amount_nonpositive"
    viol.loc[viol["amount"].isna(), "reason"] = "amount_missing"
    viol["detail"] = ""
    viol.loc[pd.to_numeric(viol["volume"], errors="coerce") <= 0, "detail"] = "volume_nonpositive"

    if len(viol):
        hit, wl_counts = _whitelist_assign(viol, whitelist)
    else:
        hit, wl_counts = pd.Series([None] * 0, index=viol.index, dtype=object), {
            e["id"]: 0 for e in whitelist
        }
    viol["whitelist_id"] = hit
    viol["whitelisted"] = hit.notna()
    d.loc[viol.index, "whitelist_id"] = hit

    per_shard: list[dict] = []
    for (sym, shard), g in d.groupby(["symbol", "shard"], sort=True):
        n = int(len(g))
        if n == 0:
            continue
        viol_n = int((~g["_ok"]).sum())
        wl_n = int(g["whitelist_id"].notna().sum())
        effective = max(0, viol_n - wl_n)
        raw_share = 1.0 - viol_n / n
        eff_share = 1.0 - effective / n
        per_shard.append({
            "symbol": sym,
            "shard": int(shard),
            "bars": n,
            "violations_raw": viol_n,
            "whitelisted": wl_n,
            "violations_effective": effective,
            "share_raw": round(raw_share, 8),
            "share_effective": round(eff_share, 8),
            "passed": bool(eff_share >= threshold - 1e-12),
        })

    failed = [r for r in per_shard if not r["passed"]]
    block.update({
        "passed": bool(per_shard) and not failed,
        "counter": "bars with amount>0 / bars",
        "per_symbol_shard": per_shard,
        "failed_symbol_shards": len(failed),
        "bars_checked": int(len(d)),
        "violations_raw": int(len(viol)),
        "violations_whitelisted": int(viol["whitelisted"].sum()),
        "violations_effective": int((~viol["whitelisted"]).sum()),
        "min_share_raw": round(min((r["share_raw"] for r in per_shard), default=float("nan")), 8),
        "min_share_effective": round(min((r["share_effective"] for r in per_shard), default=float("nan")), 8),
        "whitelist_match_counts": wl_counts,
        "whitelist_match_scope": "counts are VIOLATING rows matched per entry, not all rows in the window",
        "whitelist_entries": [
            {
                "id": e["id"],
                "date": e.get("date"),
                "time_from": e.get("time_from"),
                "time_to": e.get("time_to"),
                "symbols": e.get("symbols", ["*"]),
                "expected_rows": e.get("expected_rows"),
                "matched_rows": wl_counts.get(e["id"], 0),
                "reason": e.get("reason", ""),
            }
            for e in whitelist
        ],
    })
    wl_mismatch = [
        {"id": e["id"], "expected_rows": e.get("expected_rows"), "matched_rows": wl_counts.get(e["id"], 0)}
        for e in whitelist
        if e.get("expected_rows") is not None and int(e["expected_rows"]) != int(wl_counts.get(e["id"], 0))
    ]
    if wl_mismatch:
        block["whitelist_expected_vs_matched"] = wl_mismatch
        block["whitelist_note"] = (
            "matched != expected for at least one entry -- disclosed, not a gate failure "
            "(re-downloads can change a known-bad window by a few bars)."
        )

    rows = viol[["symbol", "ts", "date", "time", "shard", "reason", "detail", "amount", "volume",
                 "whitelisted", "whitelist_id"]].copy()
    rows.insert(0, "check", "A1_amount_coverage")
    return block, rows


# ---------------------------------------------------------------------------------------------
# A2 -- implied VWAP inside [low, high]
# ---------------------------------------------------------------------------------------------
def check_implied_vwap_range(bars: pd.DataFrame, *, tol: float = VWAP_RANGE_REL_TOL) -> tuple[dict, pd.DataFrame]:
    block: dict = {"name": "A2_implied_vwap_range", "threshold": f"vwap_implied in [low, high] (rel tol {tol:g})",
                   "scope": "per bar, plus per (symbol, day)"}
    empty = empty_violations()
    cols = ["symbol", "ts", "high", "low", "volume", "amount"]
    d = bars[cols].copy()
    d = d[d["high"].notna() & d["low"].notna() & d["volume"].notna() & d["amount"].notna()]
    d = d[(d["volume"] > 0) & (d["amount"] > 0) & (d["high"] >= d["low"])]
    if d.empty:
        block.update({"passed": False, "reason": "no rows with usable amount/volume/high/low"})
        return block, empty

    d["date"] = d["ts"].dt.date
    d["time"] = d["ts"].dt.strftime("%H:%M")
    d["vwap_implied"] = d["amount"] / d["volume"]
    lo = d["low"] * (1.0 - tol)
    hi = d["high"] * (1.0 + tol)
    d["_bar_bad"] = (d["vwap_implied"] < lo) | (d["vwap_implied"] > hi)

    day = d.groupby(["symbol", "date"]).agg(day_low=("low", "min"), day_high=("high", "max")).reset_index()
    d = d.merge(day, on=["symbol", "date"], how="left")
    d["_day_bad"] = (d["vwap_implied"] < d["day_low"] * (1.0 - tol)) | (
        d["vwap_implied"] > d["day_high"] * (1.0 + tol)
    )
    d["_bad"] = d["_bar_bad"] | d["_day_bad"]

    bad = d.loc[d["_bad"]].copy()
    excursion = pd.Series(0.0, index=d.index)
    excursion = excursion.mask(d["vwap_implied"] > d["high"], (d["vwap_implied"] / d["high"]) - 1.0)
    excursion = excursion.mask(d["vwap_implied"] < d["low"], 1.0 - (d["vwap_implied"] / d["low"]))
    d["excursion"] = excursion

    block.update({
        "passed": bool(len(bad) == 0),
        "rows_checked": int(len(d)),
        "violations": int(len(bad)),
        "violations_bar_level": int(d["_bar_bad"].sum()),
        "violations_day_level": int(d["_day_bad"].sum()),
        "worst_relative_excursion": float(d["excursion"].abs().max()) if len(d) else 0.0,
        "example_violations": bad.head(10)[
            ["symbol", "ts", "vwap_implied", "low", "high"]
        ].to_dict("records") if len(bad) else [],
    })
    if len(bad):
        rows = pd.DataFrame({
            "check": "A2_implied_vwap_range",
            "symbol": bad["symbol"].values,
            "ts": bad["ts"].values,
            "date": bad["date"].values,
            "time": bad["time"].values,
            "shard": -1,
            "reason": "vwap_implied_outside_low_high",
            "detail": [f"vwap={v:.6f} low={l:.6f} high={h:.6f}" for v, l, h in
                       zip(bad["vwap_implied"], bad["low"], bad["high"])],
            "amount": bad["amount"].values,
            "volume": bad["volume"].values,
            "whitelisted": False,
            "whitelist_id": None,
        })
        return block, rows
    return block, empty


# ---------------------------------------------------------------------------------------------
# A4a -- timestamp convention inference (needed by A3 to pick the allowed grid)
# ---------------------------------------------------------------------------------------------
def infer_timestamp_convention(labels: set[str]) -> tuple[str | None, dict]:
    """Decide END vs START from the observed clock labels. Returns (convention, evidence)."""
    evidence = {
        "first_label": min(labels) if labels else None,
        "last_label": max(labels) if labels else None,
        "has_09:30": "09:30" in labels,
        "has_09:35": "09:35" in labels,
        "has_11:25": "11:25" in labels,
        "has_11:30": "11:30" in labels,
        "has_13:00": "13:00" in labels,
        "has_13:05": "13:05" in labels,
        "has_14:55": "14:55" in labels,
        "has_15:00": "15:00" in labels,
    }
    # Only four labels differ between the two grids: 09:30 (START-only), 11:30 (END-only),
    # 13:00 (START-only), 15:00 (END-only). 11:25 and 14:55 exist in BOTH grids (11:25 is the
    # END label of 11:20-11:25, 14:55 the END label of 14:50-14:55), so they carry no vote.
    end_votes = sum([
        evidence["has_09:35"] and not evidence["has_09:30"],
        evidence["has_11:30"],
        evidence["has_15:00"],
        not evidence["has_13:00"],
    ])
    start_votes = sum([
        evidence["has_09:30"] and not evidence["has_09:35"],
        evidence["has_13:00"],
        not evidence["has_11:30"],
        not evidence["has_15:00"],
    ])
    evidence["end_votes"] = int(end_votes)
    evidence["start_votes"] = int(start_votes)
    if end_votes >= 3 and start_votes == 0:
        return "END", evidence
    if start_votes >= 3 and end_votes == 0:
        return "START", evidence
    return None, evidence


def _read_pinned_convention(path: str) -> tuple[str | None, dict | None]:
    """Return the already-recorded convention (if any) and the parsed provenance document."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:  # a corrupt pin must not be "fixed" silently
        raise ProbeGateError(f"cannot read {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProbeGateError(f"{path} is not a JSON object")
    raw = doc.get(PROVENANCE_KEY)
    if raw is None:
        return None, doc
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None:
        return None, doc
    return str(raw).strip().upper(), doc


def _pin_convention(path: str, convention: str, doc: dict | None) -> str:
    """Add the convention field WITHOUT touching any existing value. Returns the action taken."""
    if doc is not None and PROVENANCE_KEY in doc:
        _read_pinned_convention(path)  # already validated; nothing to write
        return "unchanged"
    new_doc = dict(doc or {})
    new_doc[PROVENANCE_KEY] = convention
    new_doc[PROVENANCE_NOTE_KEY] = (
        "END-labelled: 48 daily labels are 09:35,09:40,...,11:30 / 13:05,...,15:00; label t covers "
        "(t-5m, t]. Pinned by common/intraday_bar_contract.py (M3.3) -- a later probe that "
        "measures a different convention FAILS instead of rewriting this field."
    )
    existed = doc is not None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(new_doc, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    return "field_added" if existed else "file_created"


# ---------------------------------------------------------------------------------------------
# A3 -- shape
# ---------------------------------------------------------------------------------------------
def check_shape(bars: pd.DataFrame, daily: pd.DataFrame, *, convention: str | None,
                expected_bars: int = EXPECTED_BARS_PER_DAY) -> tuple[dict, pd.DataFrame]:
    allowed_labels = END_LABELS if (convention or "END") == "END" else START_LABELS
    allowed = set(allowed_labels)
    session_first = allowed_labels[0]
    block: dict = {
        "name": "A3_shape",
        "threshold": f"{expected_bars} bars/day, no lunch bars, no pre-09:30/post-15:00 bars, no dupes",
        "scope": "per symbol x trading day",
        "expected_bars_per_day": expected_bars,
        "allowed_labels_count": len(allowed_labels),
        "allowed_labels_assumed_convention": (convention or "END (assumed; convention undetermined)"),
    }
    empty = empty_violations()
    if bars.empty:
        block.update({"passed": False, "reason": "no 5m bars in the calibration DB"})
        return block, empty

    d = bars[["symbol", "ts"]].copy()
    d["date"] = d["ts"].dt.date
    d["time"] = d["ts"].dt.strftime("%H:%M")
    d["on_grid"] = d["time"].isin(allowed)
    d["lunch"] = (d["time"] > LUNCH_START) & (d["time"] < LUNCH_END)
    d["pre_session"] = d["time"] < session_first
    d["post_session"] = d["time"] > SESSION_CLOSE
    d["off_grid_other"] = ~d["on_grid"] & ~d["lunch"] & ~d["pre_session"] & ~d["post_session"]

    agg = d.groupby(["symbol", "date"], sort=True).agg(
        n_bars=("ts", "size"),
        n_distinct=("ts", "nunique"),
        lunch=("lunch", "sum"),
        pre_session=("pre_session", "sum"),
        post_session=("post_session", "sum"),
        off_grid=("off_grid_other", "sum"),
    ).reset_index()

    # calendar = days the daily table says exist UNION days the 5m table has
    daily_cal = daily[["symbol", "date"]].drop_duplicates() if not daily.empty else \
        pd.DataFrame(columns=["symbol", "date"])
    calendar = pd.concat([agg[["symbol", "date"]], daily_cal], ignore_index=True).drop_duplicates()

    if not daily.empty:
        dv = daily[["symbol", "date", "volume"]].copy()
        dv["daily_volume"] = pd.to_numeric(dv["volume"], errors="coerce")
        dv["has_daily_bar"] = True
        dv = dv.drop(columns=["volume"]).drop_duplicates(subset=["symbol", "date"])
    else:
        dv = pd.DataFrame(columns=["symbol", "date", "daily_volume", "has_daily_bar"])

    m = calendar.merge(dv, on=["symbol", "date"], how="left")
    m = m.merge(agg, on=["symbol", "date"], how="left")
    for col in ("n_bars", "n_distinct", "lunch", "pre_session", "post_session", "off_grid"):
        m[col] = m[col].fillna(0).astype(int)
    m["has_daily_bar"] = m["has_daily_bar"].fillna(False).astype(bool)

    def _classify(row) -> str:
        if row["n_bars"] == 0:
            if row["has_daily_bar"]:
                vol = row["daily_volume"]
                if pd.isna(vol) or float(vol) == 0.0:
                    return "suspended"      # 停牌: the daily bar exists but no volume was traded
            return "unknown_no_daily_bar"   # cannot prove a halt -> counts as a failure
        if row["n_bars"] != expected_bars or row["n_distinct"] != row["n_bars"]:
            return "missing"                # 缺失: the day traded but bars are short/duplicated
        if row["lunch"] or row["pre_session"] or row["post_session"] or row["off_grid"]:
            return "off_grid"
        return "ok"

    m["classification"] = m.apply(_classify, axis=1)
    counts = m["classification"].value_counts().to_dict()

    bad = m[m["classification"].isin(["missing", "off_grid", "unknown_no_daily_bar"])].copy()
    suspended = m[m["classification"] == "suspended"]

    block.update({
        "passed": bool(len(bad) == 0),
        "days_checked": int(len(m)),
        "days_ok": int(counts.get("ok", 0)),
        "days_missing": int(counts.get("missing", 0)),
        "days_off_grid": int(counts.get("off_grid", 0)),
        "days_suspended": int(counts.get("suspended", 0)),
        "days_unclassified": int(counts.get("unknown_no_daily_bar", 0)),
        "lunch_bars": int(m["lunch"].sum()),
        "pre_session_bars": int(m["pre_session"].sum()),
        "post_session_bars": int(m["post_session"].sum()),
        "off_grid_bars_other": int(m["off_grid"].sum()),
        "duplicate_bars": int((m["n_bars"] - m["n_distinct"]).sum()),
        "suspension_rule": "no 5m bars AND (no daily volume traded: NULL or 0) -- classified "
                           "'suspended', never merged into 'missing'",
        "missing_rule": "daily bar shows trading but 5m bar count != 48 (or duplicate timestamps)",
        "example_bad_days": bad.head(20)[
            ["symbol", "date", "n_bars", "classification"]
        ].assign(date=lambda x: x["date"].astype(str)).to_dict("records"),
        "example_suspended_days": suspended.head(10)[["symbol", "date"]].assign(
            date=lambda x: x["date"].astype(str)
        ).to_dict("records"),
    })

    if len(bad):
        rows = pd.DataFrame({
            "check": "A3_shape",
            "symbol": bad["symbol"].values,
            "ts": pd.NaT,
            "date": bad["date"].values,
            "time": "",
            "shard": -1,
            "reason": bad["classification"].values,
            "detail": [f"n_bars={n} lunch={l} pre={p} post={q} off_grid={o}"
                       for n, l, p, q, o in zip(bad["n_bars"], bad["lunch"], bad["pre_session"],
                                                bad["post_session"], bad["off_grid"])],
            "amount": float("nan"),
            "volume": bad["daily_volume"].values,
            "whitelisted": False,
            "whitelist_id": None,
        })
        return block, rows
    return block, empty


# ---------------------------------------------------------------------------------------------
# A4b -- convention + magnitude cross-check + provenance pin
# ---------------------------------------------------------------------------------------------
def check_timestamp_convention(bars: pd.DataFrame, daily: pd.DataFrame, *,
                               convention: str | None, evidence: dict, out_dir: str,
                               magnitude_max: float = MAGNITUDE_MEDIAN_MAX) -> dict:
    path = os.path.join(out_dir, PROVENANCE_NAME)
    block: dict = {
        "name": "A4_timestamp_convention",
        "threshold": f"END-labelled AND median |sum(amount)/sum(volume) vs day average - 1| < {magnitude_max:g}",
        "scope": "all bars; provenance pin",
        "detected": convention,
        "evidence": evidence,
        "provenance_path": os.path.abspath(path),
        "convention_definition": "END-labelled: 48 labels 09:35..11:30 / 13:05..15:00; label t covers (t-5m, t]",
    }

    # magnitude cross-check: are 5m CNY/volume and the daily price on the same scale/basis?
    mag = {"days": 0, "median_abs_rel_dev": None, "max_abs_rel_dev": None}
    if not bars.empty and not daily.empty:
        d = bars[["symbol", "ts", "amount", "volume"]].copy()
        d["date"] = d["ts"].dt.date
        g = d.groupby(["symbol", "date"]).agg(amt5=("amount", "sum"), vol5=("volume", "sum"))
        dref = daily[["symbol", "date", "high", "low", "close", "volume", "amount"]].copy()
        dref["vwap_daily"] = pd.to_numeric(dref["amount"], errors="coerce") / pd.to_numeric(
            dref["volume"], errors="coerce")
        typical = (pd.to_numeric(dref["high"], errors="coerce")
                   + pd.to_numeric(dref["low"], errors="coerce")
                   + pd.to_numeric(dref["close"], errors="coerce")) / 3.0
        dref["reference_price"] = dref["vwap_daily"].where(dref["vwap_daily"] > 0, typical)
        j = g.reset_index().merge(dref[["symbol", "date", "reference_price"]], on=["symbol", "date"], how="inner")
        j = j[(j["vol5"] > 0) & j["reference_price"].notna() & (j["reference_price"] > 0)]
        if len(j):
            dev = ((j["amt5"] / j["vol5"]) / j["reference_price"] - 1.0).abs()
            mag = {
                "days": int(len(j)),
                "median_abs_rel_dev": float(dev.median()),
                "max_abs_rel_dev": float(dev.max()),
            }
    block["magnitude_check"] = mag
    magnitude_ok = bool(mag["days"] > 0 and mag["median_abs_rel_dev"] is not None
                        and mag["median_abs_rel_dev"] < magnitude_max)

    recorded, doc = _read_pinned_convention(path) if os.path.isdir(out_dir) else (None, None)
    block["recorded"] = recorded

    if convention is None:
        block.update({
            "passed": False,
            "provenance_action": "not_written",
            "reason": "cannot determine START vs END labelling from the observed clock labels "
                      f"(evidence={evidence}); refusing to guess and refusing to pin",
        })
        return block
    if convention != EXPECTED_CONVENTION:
        # The calibration contract asks for END labels (M3.3 item 4). A START-labelled DB is a
        # different, incompatible contract, so it fails -- and the pin is NOT written, because
        # recording "START" would legitimise a basis nobody calibrated on.
        block.update({
            "passed": False,
            "provenance_action": "not_written",
            "expected": EXPECTED_CONVENTION,
            "reason": f"bar labels are {convention}-labelled but the calibration contract requires "
                      f"{EXPECTED_CONVENTION} (09:35 = 09:30-09:35). Re-derive bar labels or "
                      f"re-download; do not calibrate on a shifted label grid.",
        })
        return block
    if recorded is not None and recorded != convention:
        block.update({
            "passed": False,
            "provenance_action": "conflict_not_rewritten",
            "reason": f"provenance.json pins {PROVENANCE_KEY}={recorded!r} but this DB measures "
                      f"{convention!r}. The pin is never silently overwritten: either the DB or the "
                      f"pin is wrong, and a human must decide.",
        })
        return block

    action = _pin_convention(path, convention, doc) if os.path.isdir(out_dir) else "out_dir_missing"
    block["provenance_action"] = action
    block["passed"] = bool(magnitude_ok)
    if not block["passed"]:
        block["reason"] = (
            "convention label is consistent but the magnitude cross-check failed "
            f"(median |dev|={mag['median_abs_rel_dev']} vs limit {magnitude_max:g}, "
            f"days={mag['days']}) -- units/basis mismatch, not a labelling issue"
        )
    return block


# ---------------------------------------------------------------------------------------------
# A5 -- amount reconciliation
# ---------------------------------------------------------------------------------------------
def _ok_days(bars: pd.DataFrame, daily: pd.DataFrame, *, convention: str | None,
             expected_bars: int) -> set:
    """(symbol, date) pairs that A3 classified as clean 48-bar sessions (A5's sample)."""
    allowed = set(END_LABELS if (convention or "END") == "END" else START_LABELS)
    d = bars[["symbol", "ts"]].copy()
    d["date"] = d["ts"].dt.date
    d["time"] = d["ts"].dt.strftime("%H:%M")
    d["bad_time"] = ~d["time"].isin(allowed)
    agg = d.groupby(["symbol", "date"]).agg(n=("ts", "size"), n_distinct=("ts", "nunique"),
                                            bad=("bad_time", "sum"))
    clean = agg[(agg["n"] == expected_bars) & (agg["n_distinct"] == agg["n"]) & (agg["bad"] == 0)]
    return set(clean.index.tolist())


def check_amount_reconciliation(bars: pd.DataFrame, daily: pd.DataFrame, *,
                                valid_days: set | None = None,
                                amount_max: float = AMOUNT_RECON_MEDIAN_MAX,
                                proxy_max: float = VWAP_PROXY_MEDIAN_MAX) -> dict:
    block: dict = {
        "name": "A5_amount_reconciliation",
        "scope": "per (symbol, day) with a clean 48-bar session",
        "threshold_primary": amount_max,
        "threshold_degraded": proxy_max,
        "degraded": False,
        "degraded_reason": None,
    }
    if bars.empty or daily.empty:
        block.update({"passed": False, "reason": "need both 5m bars and daily bars to reconcile"})
        return block

    d = bars[["symbol", "ts", "amount", "volume"]].copy()
    d["date"] = d["ts"].dt.date
    g = d.groupby(["symbol", "date"], sort=True).agg(amt5=("amount", "sum"), vol5=("volume", "sum"),
                                                     n=("ts", "size")).reset_index()
    if valid_days is not None:
        keys = pd.MultiIndex.from_frame(g[["symbol", "date"]])
        g = g[[k in valid_days for k in keys]].copy()
    if g.empty:
        block.update({"passed": False, "reason": "no clean (symbol, day) session to reconcile "
                                                 "(A3 shape assertion found none)"})
        return block

    dref = daily[["symbol", "date", "high", "low", "close", "volume", "amount"]].copy()
    for col in ("high", "low", "close", "volume", "amount"):
        dref[col] = pd.to_numeric(dref[col], errors="coerce")
    j = g.merge(dref, on=["symbol", "date"], how="inner")
    block["days_matched"] = int(len(j))
    if j.empty:
        block.update({"passed": False, "reason": "no (symbol, day) present in both tables"})
        return block

    amount_usable = j["amount"].notna() & (j["amount"] > 0)
    vol_usable = j["volume"].notna() & (j["volume"] > 0)
    primary = j[amount_usable & vol_usable].copy()
    block["days_with_daily_amount"] = int(len(primary))
    block["days_without_daily_amount"] = int(len(j) - len(primary))

    if len(primary) > 0:
        rel = (primary["amt5"] / primary["amount"]) - 1.0
        median_abs = float(rel.abs().median())
        block.update({
            "path": "primary: sum(5m amount) vs daily amount",
            "measured": "median |sum(5m amount)/daily amount - 1|",
            "median_abs_rel_err": median_abs,
            "p90_abs_rel_err": float(rel.abs().quantile(0.90)),
            "max_abs_rel_err": float(rel.abs().max()),
            "days_checked": int(len(primary)),
            "passed": bool(median_abs < amount_max),
        })
        if block["days_without_daily_amount"] > 0:
            block["partial_coverage_note"] = (
                f"{block['days_without_daily_amount']} of {block['days_matched']} days have no usable "
                "daily amount and were excluded from the primary statistic (disclosed, not hidden)."
            )
        return block

    proxy = j[vol_usable].copy()
    if proxy.empty:
        block.update({"passed": False, "reason": "daily table has neither amount nor volume"})
        return block
    typical = (proxy["high"] + proxy["low"] + proxy["close"]) / 3.0
    ok = typical.notna() & (typical > 0) & (proxy["vol5"] > 0)
    proxy = proxy[ok].copy()
    if proxy.empty:
        block.update({"passed": False, "reason": "degraded path has no usable (H+L+C)/3 reference"})
        return block
    dev = ((proxy["amt5"] / proxy["vol5"]) / typical[ok]) - 1.0
    median_abs = float(dev.abs().median())
    block.update({
        "degraded": True,
        "degraded_reason": (
            "daily_1d carries no usable `amount` column, so the primary 1% amount reconciliation is "
            "IMPOSSIBLE. Degraded (disclosed) substitute: median |(sum5m amount / sum5m volume) / "
            "((high+low+close)/3) - 1| < 2%. This validates the price level only -- it cannot prove "
            "the turnover magnitude the way the primary check does."
        ),
        "path": "degraded: sum(5m amount)/sum(5m volume) vs (high+low+close)/3",
        "measured": "median |5m VWAP / (H+L+C)/3 - 1|",
        "median_abs_rel_err": median_abs,
        "p90_abs_rel_err": float(dev.abs().quantile(0.90)),
        "max_abs_rel_err": float(dev.abs().max()),
        "days_checked": int(len(proxy)),
        "passed": bool(median_abs < proxy_max),
    })
    return block


# ---------------------------------------------------------------------------------------------
# orchestration -- A1..A5 is one call; A6 is the aggregate
# ---------------------------------------------------------------------------------------------
def evaluate_assertions(bars: pd.DataFrame, daily: pd.DataFrame, *, out_dir: str,
                        whitelist: list[dict] | None = None,
                        shards: int = DEFAULT_SHARDS,
                        amount_coverage_min: float = AMOUNT_COVERAGE_MIN,
                        vwap_tol: float = VWAP_RANGE_REL_TOL,
                        amount_recon_tol: float = AMOUNT_RECON_MEDIAN_MAX,
                        proxy_recon_tol: float = VWAP_PROXY_MEDIAN_MAX,
                        magnitude_tol: float = MAGNITUDE_MEDIAN_MAX,
                        expected_bars: int = EXPECTED_BARS_PER_DAY,
                        ) -> tuple[dict[str, dict], list[pd.DataFrame]]:
    """Run A1..A5 and return `(assertions, violation_frames)`.

    `assertions` is keyed by the block names in `REQUIRED_ASSERTIONS` and is exactly what belongs
    under `probe_report.json -> assertions`. `violation_frames` are the per-check frames in the
    `VIOLATION_COLUMNS` schema; the caller concatenates and writes them (this module does not).

    Only the provenance pin (`out_dir/provenance.json`) is ever written.
    """
    entries = list(whitelist if whitelist is not None else DEFAULT_WHITELIST)

    labels = set(bars["ts"].dt.strftime("%H:%M"))
    convention, evidence = infer_timestamp_convention(labels)

    block_a1, rows_a1 = check_amount_coverage(
        bars, n_shards=shards, whitelist=entries, threshold=amount_coverage_min)
    block_a2, rows_a2 = check_implied_vwap_range(bars, tol=vwap_tol)
    block_a3, rows_a3 = check_shape(bars, daily, convention=convention, expected_bars=expected_bars)
    block_a4 = check_timestamp_convention(
        bars, daily, convention=convention, evidence=evidence, out_dir=out_dir,
        magnitude_max=magnitude_tol)
    # A5's sample is A3's clean sessions: reconciling a day that is missing bars would mix a
    # shape defect into a turnover defect, and only one of them is A5's business.
    ok_days: set = set()
    if block_a3.get("days_ok"):
        ok_days = _ok_days(bars, daily, convention=convention, expected_bars=expected_bars)
    block_a5 = check_amount_reconciliation(bars, daily, valid_days=ok_days,
                                          amount_max=amount_recon_tol, proxy_max=proxy_recon_tol)

    assertions = {
        "A1_amount_coverage": block_a1,
        "A2_implied_vwap_range": block_a2,
        "A3_shape": block_a3,
        "A4_timestamp_convention": block_a4,
        "A5_amount_reconciliation": block_a5,
    }
    return assertions, [rows_a1, rows_a2, rows_a3]


def finalize_report(report: dict) -> dict:
    """A6: attach `degraded`, `A6_gate`, `all_passed`, `exit_code` to an in-progress report.

    Fails CLOSED: a missing required block (because an earlier hard error aborted the run) or a
    recorded `error` closes the gate even if the blocks that did run passed. There is deliberately
    no override -- see `no_downgrade`.
    """
    blocks = report.get("assertions") or {}
    block_a5 = blocks.get("A5_amount_reconciliation") or {}
    report["degraded"] = bool(block_a5.get("degraded", False))
    report["degraded_reasons"] = [block_a5["degraded_reason"]] if report["degraded"] else []

    failed = [n for n, b in blocks.items() if not b.get("passed")]
    missing_blocks = [n for n in REQUIRED_ASSERTIONS if n not in blocks]
    all_passed = bool(not failed and not missing_blocks and not report.get("error"))
    report["A6_gate"] = {
        "name": "A6_gate",
        "passed": all_passed,
        "failed_assertions": failed + [f"{n}:not_evaluated" for n in missing_blocks],
        "downstream_allowed": all_passed,
        "message": ("all six assertions passed -- full calibration may proceed" if all_passed else
                    "GATE CLOSED: a full calibration run MUST NOT start on this DB"),
        "no_downgrade": "this probe has no --force/--continue flag by design (M3.3 item 6)",
    }
    report["all_passed"] = all_passed
    report["exit_code"] = 0 if all_passed else 1
    return report


def write_artifacts(report: dict, violation_frames: Iterable[pd.DataFrame], out_dir: str) -> pd.DataFrame:
    """Write `probe_violations.csv` + `probe_report.json`; returns the concatenated violations.

    Runs unconditionally, including on a fail-closed report: a closed gate that left no artifact
    behind would be indistinguishable from a crash.
    """
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, REPORT_NAME)
    violations_path = os.path.join(out_dir, VIOLATIONS_NAME)

    non_empty = [f for f in violation_frames if len(f)]
    if non_empty:
        vs = pd.concat(non_empty, ignore_index=True)
    else:
        vs = empty_violations()
    vs.to_csv(violations_path, index=False)
    report["artifacts"] = {"report": report_path, "violations_csv": violations_path,
                           "violations_rows": int(len(vs))}
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    return vs


def run_gate(bars: pd.DataFrame | None, daily: pd.DataFrame | None, *, out_dir: str,
             report: dict | None = None, error: str | None = None,
             whitelist: list[dict] | None = None,
             shards: int = DEFAULT_SHARDS,
             amount_coverage_min: float = AMOUNT_COVERAGE_MIN,
             vwap_tol: float = VWAP_RANGE_REL_TOL,
             amount_recon_tol: float = AMOUNT_RECON_MEDIAN_MAX,
             proxy_recon_tol: float = VWAP_PROXY_MEDIAN_MAX,
             magnitude_tol: float = MAGNITUDE_MEDIAN_MAX,
             expected_bars: int = EXPECTED_BARS_PER_DAY,
             ) -> tuple[dict, pd.DataFrame]:
    """Evaluate, aggregate (A6), write the artifacts, and return `(report, violations)`.

    This is the whole gate for a caller that already holds the two frames -- the CLI shim's only
    remaining job is the SQL and the printing. `error` / `bars=None` model the fail-closed path
    (the loader could not produce frames at all): the report is still written, `exit_code` is 1.

    `report` is an optional seed from `build_report` (so a caller can add `input` metadata before
    the assertions run); a fresh one is built when omitted. Never raises for data problems: a
    `ProbeGateError` from requirement 7's guard is recorded as `report["error"]` and closes the
    gate. Genuine programming errors are left to propagate.
    """
    out_dir = os.path.abspath(out_dir)
    report = dict(report) if report is not None else build_report(out_dir=out_dir, shards=shards)

    violation_frames: list[pd.DataFrame] = []
    if error is not None:
        report["error"] = str(error)
        report["assertions"] = report.get("assertions") or {}
    elif bars is None or daily is None:
        report["error"] = "no input frames supplied to the gate"
        report["assertions"] = report.get("assertions") or {}
    else:
        try:
            _guard_amount_not_reconstructed(bars)
            assertions, violation_frames = evaluate_assertions(
                bars, daily, out_dir=out_dir, whitelist=whitelist, shards=shards,
                amount_coverage_min=amount_coverage_min, vwap_tol=vwap_tol,
                amount_recon_tol=amount_recon_tol, proxy_recon_tol=proxy_recon_tol,
                magnitude_tol=magnitude_tol, expected_bars=expected_bars,
            )
            report["assertions"] = assertions
        except ProbeGateError as exc:
            report["error"] = str(exc)
            report["assertions"] = report.get("assertions") or {}

    finalize_report(report)
    violations = write_artifacts(report, violation_frames, out_dir)
    return report, violations

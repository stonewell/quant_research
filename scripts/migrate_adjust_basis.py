#!/usr/bin/env python3
"""Audit and DECLARE the adjustment basis of already-cached bars (M3.6 review F1/F2).

Why this script exists
----------------------
`asset_sync_metadata.adjustflag` and `intraday_sync_metadata.adjustflag` were added to the
DuckDB market cache by an in-place `ALTER TABLE ADD COLUMN`, which backfills nothing. Every row
that predates that migration therefore carries SQL NULL, i.e. "these cached bytes have no
recorded price basis". A cache in that state can serve QFQ prices to a `raw` request (or return
QFQ unscaled to a `qfq` request whose window ends before the factor anchor) with no warning at
all -- the exact failure class of the 2026-10-23 incident.

Since M3.6 that state is FAIL-CLOSED: `DuckDBCache.query_bars` / `query_intraday_bars` raise
`UnknownAdjustBasisError` (and `store_bars` refuses to stamp a new label onto it --
`AdjustBasisTransitionRefused`). This script is the sanctioned way out of it.

What it does -- and does NOT do
-------------------------------
* READ-ONLY by default (`--dry-run`, or no arguments at all): it lists every
  (provider, symbol[, interval]) whose metadata basis is unknown (SQL NULL, or the explicit
  `"unknown"` sentinel written by an undeclared modern sync) while the bar table already holds
  rows for it, with the row count.
* `--set-basis {raw,qfq,hfq} --provider P --symbol S` writes ONE declared label into the sync
  metadata row. Nothing else.
* Two claims are refused unless `--force` is given (M3.6 review P2), because both reinterpret
  every stored price of that symbol: declaring `raw` for a symbol with no `adjust_factors` rows
  (no `qfq`/`hfq` read could ever be derived, so they all fail closed), and OVERWRITING an
  already-declared basis (the sanctioned fix for a wrong label is a re-fetch).
* It NEVER rewrites, rescales or deletes a single price/volume/amount. It NEVER infers a basis
  from prices (a shift factor and a corporate action are indistinguishable from the price series
  alone -- see `docs/version.md` §13.8). It performs NO network access of any kind: declaring a
  basis is a statement about the bytes already on disk, which only a human can make.

Typical use
-----------
    # 1. see what needs a decision (read-only, safe against the live cache)
    python scripts/migrate_adjust_basis.py --dry-run

    # 2. declare, one symbol at a time, after checking how that symbol was originally synced
    python scripts/migrate_adjust_basis.py --set-basis qfq \
        --provider BaoStockDataProvider --symbol 600519.SH

After the declaration the symbol reads normally again (and `store_bars` may append to it). If you
later find the declaration was wrong, that is a price-basis problem: re-fetch the symbol
(`scripts/repair_qfq_alignment.py`), do not flip the label by hand.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.duckdb_cache import (  # noqa: E402
    DuckDBCache,
    _UNDECLARED_BASIS,
    canonicalize_adjust_mode,
    is_legacy_basis_unknown,
    parse_stored_basis,
)

#: Tables this script understands, with the extra key column each one carries.
#: `meta_table`, `bars_table`, `extra_key` (None for daily, 'interval' for intraday).
_SCOPES = {
    "daily": {
        "meta_table": "asset_sync_metadata",
        "bars_table": "ohlcv_bars",
        "extra_key": None,
        "time_col": "date",
    },
    "intraday": {
        "meta_table": "intraday_sync_metadata",
        "bars_table": "intraday_ohlcv_bars",
        "extra_key": "interval",
        "time_col": "ts",
    },
}


def scan_unknown_basis(cache: DuckDBCache, scopes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Read-only scan: metadata rows with an UNDECLARED basis that already hold bars.

    Returns one dict per finding, ordered by (scope, provider, symbol, interval):

        {"scope", "provider", "symbol", "interval", "state", "row_count", "first_bar", "last_bar"}

    `state` is `"legacy_null"` (pre-M3.6 row: the column itself was added on top of it) or
    `"undeclared"` (a modern write recorded the `_UNDECLARED_BASIS` sentinel). Both are refused
    by the read path; neither can be inferred here.

    The join to the bar table is deliberate: a metadata row with ZERO bars is harmless (nothing
    can be read from it, and the next store_bars rebuilds it), so it is not reported as something
    that needs a decision.
    """
    wanted = [s for s in (scopes or list(_SCOPES)) if s in _SCOPES]
    findings: List[Dict[str, Any]] = []

    con = cache._get_connection(read_only=True)
    try:
        for scope in wanted:
            spec = _SCOPES[scope]
            meta_table = spec["meta_table"]
            bars_table = spec["bars_table"]
            extra = spec["extra_key"]
            time_col = spec["time_col"]

            select_extra = f"m.{extra}," if extra else "CAST(NULL AS VARCHAR) AS interval,"
            group_extra = f"m.{extra}, " if extra else ""
            join_extra = f" AND b.{extra} = m.{extra}" if extra else ""

            sql = f"""
                SELECT
                    m.provider,
                    m.symbol,
                    {select_extra}
                    m.adjustflag,
                    count(*) AS row_count,
                    MIN(b.{time_col}) AS first_bar,
                    MAX(b.{time_col}) AS last_bar
                FROM {meta_table} m
                JOIN {bars_table} b
                  ON b.provider = m.provider AND b.symbol = m.symbol{join_extra}
                GROUP BY m.provider, m.symbol, {group_extra}m.adjustflag
                ORDER BY m.provider, m.symbol{", " + extra if extra else ""}
            """
            rows = con.execute(sql).fetchall()
            for provider, symbol, interval, adjustflag, row_count, first_bar, last_bar in rows:
                if not is_legacy_basis_unknown(adjustflag) and (
                    str(adjustflag).strip().lower() != _UNDECLARED_BASIS
                ):
                    continue
                findings.append(
                    {
                        "scope": scope,
                        "provider": provider,
                        "symbol": symbol,
                        "interval": interval,
                        "state": "legacy_null" if is_legacy_basis_unknown(adjustflag) else "undeclared",
                        "row_count": int(row_count),
                        "first_bar": str(first_bar) if first_bar is not None else "",
                        "last_bar": str(last_bar) if last_bar is not None else "",
                    }
                )
    finally:
        if not cache.is_memory:
            con.close()

    findings.sort(key=lambda f: (f["scope"], f["provider"], f["symbol"], f["interval"] or ""))
    return findings


def format_scan_report(findings: List[Dict[str, Any]]) -> str:
    """Human-readable render of `scan_unknown_basis` -- the `--dry-run` output body."""
    lines: List[str] = []
    if not findings:
        lines.append("No cached symbol has an undeclared adjustment basis. Nothing to migrate.")
        return "\n".join(lines)

    total_rows = sum(f["row_count"] for f in findings)
    lines.append(
        f"{len(findings)} undeclared-basis entr(ies) covering {total_rows} stored bar(s). "
        f"Each one READ-FAILS until its basis is declared:"
    )
    lines.append("")
    header = f"{'scope':<9} {'provider':<24} {'symbol':<14} {'interval':<8} {'state':<12} {'bars':>10}  span"
    lines.append(header)
    lines.append("-" * len(header))
    for f in findings:
        lines.append(
            f"{f['scope']:<9} {f['provider']:<24} {f['symbol']:<14} "
            f"{(f['interval'] or '-'):<8} {f['state']:<12} {f['row_count']:>10}  "
            f"{f['first_bar']} .. {f['last_bar']}"
        )
    lines.append("")
    lines.append(
        "Declare one entry with:\n"
        "  python scripts/migrate_adjust_basis.py --set-basis {raw|qfq|hfq} "
        "--provider <provider> --symbol <symbol> [--interval <interval>]"
    )
    lines.append(
        "This tool never rewrites prices and never guesses: the basis is a statement about the "
        "bytes already on disk."
    )
    return "\n".join(lines)


def declare_basis(
    cache: DuckDBCache,
    provider: str,
    symbol: str,
    basis: str,
    interval: Optional[str] = None,
    scope: Optional[str] = None,
    force: bool = False,
) -> str:
    """Writes ONE declared basis into a metadata row. Returns the previous raw value (repr'd).

    Deliberately narrow:
      * only `raw` / `qfq` / `hfq` are accepted -- `"unknown"` is not a declaration, and a FREE
        FORM string would reintroduce the ambiguity this whole mechanism removes;
      * the row must EXIST: this script never creates metadata (a missing row is
        `store_bars`' job, and inventing one would fabricate a boundary claim as well);
      * no price, volume or amount column is touched, ever.

    Guards (M3.6 review P2, item 5) -- a declaration is a claim about bytes, so the two claims
    that are cheap to make and expensive to get wrong are refused unless the operator insists
    with `force=True` (`--force` on the CLI):

      1. `--set-basis raw` for a symbol the `adjust_factors` table has never seen. Raw bytes with
         no factors are only READABLE as raw; any `qfq`/`hfq` request then fails closed with
         `MissingAdjustFactorsError` instead of being derived. That is a legitimate final state
         (e.g. a provider with no factor source), but it is almost always a mistake when the
         operator is in the middle of a migration, so it must be deliberate.
      2. Overwriting an ALREADY-DECLARED basis (`raw`/`qfq`/`hfq` -> a different one). This is
         how a row silently changes meaning under every stored price; the sanctioned fix for a
         wrong label is a re-fetch (`scripts/repair_qfq_alignment.py`), not a flip. Re-declaring
         the SAME value is a no-op and needs no flag.
    """
    declared = canonicalize_adjust_mode(basis)
    scope = scope or ("intraday" if interval else "daily")
    spec = _SCOPES[scope]
    meta_table = spec["meta_table"]
    extra = spec["extra_key"]

    params: List[Any] = [declared, provider, symbol]
    where = "provider = ? AND symbol = ?"
    where_params: List[Any] = [provider, symbol]
    if extra:
        if not interval:
            raise ValueError(
                f"{meta_table} is keyed by {extra}: --interval is required for an intraday "
                f"declaration."
            )
        where += f" AND {extra} = ?"
        params.append(interval)
        where_params.append(interval)

    con = cache._get_connection(read_only=False)
    try:
        row = con.execute(
            f"SELECT adjustflag FROM {meta_table} WHERE {where}",
            where_params,
        ).fetchone()
        if row is None:
            raise KeyError(
                f"No {meta_table} row for provider={provider!r}, symbol={symbol!r}"
                + (f", interval={interval!r}" if extra else "")
                + ". This script only LABELS an existing row; it never creates one "
                "(re-run the sync for this symbol instead)."
            )
        previous = row[0]
        previous_declared = parse_stored_basis(previous)

        if previous_declared is not None:
            if previous_declared == declared:
                # Idempotent: the row already says exactly this. Nothing to write, nothing to
                # force -- re-running the "typical use" command must not start demanding --force.
                return repr(previous)
            if not force:
                raise RuntimeError(
                    f"Refusing to overwrite the ALREADY-DECLARED basis of {symbol!r} "
                    f"({provider}"
                    + (f", interval={interval!r}" if extra else "")
                    + f"): stored metadata says {previous_declared!r}, you asked for "
                    f"{declared!r}. Every stored price is interpreted through that label, so a "
                    f"flip silently changes the meaning of the whole history. If the label really "
                    f"is wrong, the sanctioned fix is a re-fetch "
                    f"(scripts/repair_qfq_alignment.py). Pass --force only if you are certain the "
                    f"stored bytes already are '{declared}' and only the label was wrong."
                )

        if declared == "raw" and not force:
            has_factors = con.execute(
                "SELECT 1 FROM adjust_factors WHERE symbol = ? LIMIT 1", [symbol]
            ).fetchone()
            if has_factors is None:
                raise RuntimeError(
                    f"Refusing to declare basis 'raw' for {symbol!r} ({provider}): the "
                    f"`adjust_factors` table holds NO factor rows for this symbol, so no qfq/hfq "
                    f"read could ever be derived from these bytes -- every adjusted request would "
                    f"fail closed with MissingAdjustFactorsError. That is a legitimate end state "
                    f"only if this symbol has no factor source at all; if you are mid-migration, "
                    f"fetch its factors first. Re-run with --force to declare 'raw' anyway."
                )

        con.execute(f"UPDATE {meta_table} SET adjustflag = ? WHERE {where}", params)
        verify = con.execute(
            f"SELECT adjustflag FROM {meta_table} WHERE {where}", where_params
        ).fetchone()
    finally:
        if not cache.is_memory:
            con.close()

    if not verify or verify[0] != declared:
        raise RuntimeError(
            f"Declaring basis for {symbol!r} ({provider}) did not persist (expected "
            f"{declared!r}, got {verify[0] if verify else None!r})."
        )
    return repr(previous)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit + explicit declaration of the adjustment basis of already-cached "
            "bars. Never rewrites prices, never infers a basis, never touches the network."
        )
    )
    parser.add_argument(
        "--db",
        default=os.path.join(_REPO_ROOT, "data", "cache.duckdb"),
        help="Path to the DuckDB market cache (default: <repo>/data/cache.duckdb).",
    )
    parser.add_argument(
        "--dry-run",
        "--scan",
        dest="dry_run",
        action="store_true",
        help="List undeclared-basis entries only (the default when no --set-basis is given).",
    )
    parser.add_argument(
        "--set-basis",
        choices=["raw", "qfq", "hfq"],
        default=None,
        help="Declare the basis of ONE entry (requires --provider and --symbol).",
    )
    parser.add_argument("--provider", default=None, help="Provider name, e.g. BaoStockDataProvider.")
    parser.add_argument("--symbol", default=None, help="Symbol, e.g. 600519.SH.")
    parser.add_argument(
        "--interval",
        default=None,
        help="Intraday interval (5m, ...). Required to declare a 5m entry; selects the intraday tables.",
    )
    parser.add_argument(
        "--scope",
        choices=["all", "daily", "intraday"],
        default="all",
        help="Which tables to scan (default: all).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Override the two declaration guards: declaring basis 'raw' for a symbol with no "
            "`adjust_factors` rows, and overwriting an already-declared basis. Both are normally "
            "refused because they change the meaning of every stored price for that symbol."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.set_basis:
        if not args.provider or not args.symbol:
            parser.error("--set-basis requires --provider and --symbol")
        if args.force:
            # Deliberately loud and BEFORE the write: the two guards this bypasses exist because
            # both actions reinterpret every stored price of the symbol.
            print("!" * 78)
            print("WARNING: --force bypasses the basis-declaration guards.")
            if args.set_basis == "raw":
                print(
                    "         Declaring 'raw' for a symbol with no adjust_factors rows makes "
                    "every\n         qfq/hfq read of it FAIL CLOSED (MissingAdjustFactorsError) "
                    "until factors\n         are fetched. That is only correct if the symbol has "
                    "no factor source at all."
                )
            print(
                "         If the stored basis is ALREADY declared, this overwrites that label, "
                "which\n         silently changes the interpretation of the stored history.\n"
                "         The sanctioned fix for a wrong label is a re-fetch, not a flip."
            )
            print("!" * 78)
        cache = DuckDBCache(db_path=args.db, read_only=False)
        previous = declare_basis(
            cache,
            args.provider,
            args.symbol,
            args.set_basis,
            interval=args.interval,
            force=args.force,
        )
        target = f"{args.provider}/{args.symbol}" + (f" [{args.interval}]" if args.interval else "")
        print(f"Declared basis {args.set_basis!r} for {target} (was {previous}). No prices changed.")
        return 0

    # Default = dry run. Read-only open: this path cannot write even by accident.
    cache = DuckDBCache(db_path=args.db, read_only=True)
    scopes = None if args.scope == "all" else [args.scope]
    findings = scan_unknown_basis(cache, scopes)
    print("=" * 78)
    print("ADJUSTMENT-BASIS AUDIT (READ-ONLY) -- no prices are read for inference, no network")
    print(f"Cache: {cache.db_path}")
    print("=" * 78)
    print(format_scan_report(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())

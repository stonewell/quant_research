#!/usr/bin/env python3
"""Read-only report of the `511880.SH` 5m coverage verdict (`docs/development_plan.md` `M3.11`).

WHAT IT DOES
------------
Recomputes the three `M3.11` acceptance questions straight from `data/cache.duckdb` --
coverage against the requested target window, per-session completeness, and cross
comparability against a peer 5m series -- and prints one compact verdict table.

    PASS / FAIL / INSUFFICIENT   (see `common/intraday_coverage.py` for the contracts)

Exit code is `0` **only** when all three checks pass; `1` when any check fails or cannot be
evaluated; `2` for usage / IO problems (missing DB, unknown symbol).

READ-ONLY GUARANTEES
--------------------
* `duckdb.connect(..., read_only=True)` -- the only connection this script ever opens.
* No `ATTACH`, no `CREATE`, no `CHECKPOINT`/`EXPORT`/`VACUUM`, no temp file next to the DB.
* A missing DB file is reported, never created (`DuckDBCache` would `makedirs` + `_init_tables`).
* No network, no downloader, no provider object -- just `SELECT`s.

WHY THE DEFAULT WINDOW IS 2026-10-19 .. 2026-11-05
-------------------------------------------------
The `2026-11-08` user ruling re-baselined the 5m table to the post-`M3.8` caliber (see
`docs/intraday_5m_coverage.md`): current coverage is `34,944` rows / 28 symbols / from
`2026-09-16`, and `511880.SH` 5m spans `2026-10-19 09:35 ~ 2026-11-05 15:00` (14 sessions,
672 bars, single BaoStock provider). Defaulting to that span means the bare command answers
the only question that matters *under the current caliber*: does the cache support the window
we now claim? -- rather than a self-fulfilling "does the cache cover itself", and rather than
re-failing forever against the retired `2026-01-05` window.

The OLD caliber is still reproducible on demand -- `--start 2026-01-05 --end 2026-11-05`
reports `coverage FAIL` (14/219 weekdays = 6.4%) and exits `1`, which is the historical
verdict preserved in the doc. Nothing about the checks themselves changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import datetime as dt

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import pandas as pd

from common.intraday_coverage import (
    BARS_PER_SESSION_5M,
    STATUS_PASS,
    check_coverage,
    check_cross_comparability,
    check_daily_completeness,
    start_date_spread,
)

_READ_COLUMNS = "provider, symbol, interval, ts, open, high, low, close, volume, amount"

#: New caliber (post-`M3.8`) target window, fixed by the 2026-11-08 user ruling: the 5m table
#: was re-baselined to `34,944 行 / 28 只 / 2026-09-16 起` and `511880.SH` now spans
#: `2026-10-19 09:35 ~ 2026-11-05 15:00` (14 sessions / 672 bars). See
#: `docs/intraday_5m_coverage.md` and `docs/development_plan.md` `M3.11`.
#: The retired window (`2026-01-05 ~ 2026-11-05`, the `M3.4` snapshot) stays reproducible via
#: `--start 2026-01-05`; it is deliberately NOT the default any more.
DEFAULT_START = "2026-10-19"
DEFAULT_END = "2026-11-05"
DEFAULT_SYMBOL = "511880.SH"
DEFAULT_PEER = "000001.SH"

#: Exit-code contract: `0` only when every verdict is `pass`; `1` when any is
#: `fail`/`insufficient`; `2` is reserved for usage / IO problems (missing DB, unknown symbol).
EXIT_ALL_PASS = 0
EXIT_NOT_ALL_PASS = 1
EXIT_USAGE_OR_IO = 2


def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"{db_path} does not exist -- refusing to create it "
            "(this script is read-only by contract)"
        )
    return duckdb.connect(db_path, read_only=True)


def _providers(con, symbol: str, interval: str):
    rows = con.execute(
        "SELECT provider, COUNT(*) FROM intraday_ohlcv_bars "
        "WHERE symbol = ? AND interval = ? GROUP BY 1 ORDER BY 2 DESC, 1",
        [symbol, interval],
    ).fetchall()
    return [(str(p), int(n)) for p, n in rows]


def _frame(con, symbol: str, interval: str, provider: str) -> pd.DataFrame:
    """One provider's series for one symbol -- never a mixed-provider stack.

    The cache legally holds the same symbol under several providers (the 7 index symbols
    currently do: Fuyao *and* EastMoney, 1,488 rows each). Stacking them would double every
    timestamp and make the completeness check report duplicates that are not defects of the
    series, so the caller picks one provider explicitly.
    """
    return con.execute(
        f"SELECT {_READ_COLUMNS} FROM intraday_ohlcv_bars "
        "WHERE symbol = ? AND interval = ? AND provider = ? ORDER BY ts",
        [symbol, interval, provider],
    ).df()


def _series_starts(con, interval: str):
    rows = con.execute(
        "SELECT symbol, MIN(ts) FROM intraday_ohlcv_bars WHERE interval = ? "
        "GROUP BY 1 ORDER BY 2, 1",
        [interval],
    ).fetchall()
    return {str(s): ts for s, ts in rows}


def _fmt_ts(value) -> str:
    return "-" if value is None else str(pd.Timestamp(value))[:16]


def _verdict(report) -> str:
    return {"pass": "PASS", "fail": "FAIL"}.get(report.status, "INSUFFICIENT")


def build_arg_parser() -> argparse.ArgumentParser:
    """The CLI surface, split out so tests can pin defaults without touching DuckDB.

    Side-effect free by construction: building the parser opens no connection, stats no path
    and reads no environment variable.
    """
    ap = argparse.ArgumentParser(
        description="Read-only 5m coverage / completeness / comparability report (M3.11)."
    )
    ap.add_argument("--db", default=os.path.join(_REPO_ROOT, "data", "cache.duckdb"),
                    help="cache path (opened read_only; never created)")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL, help="symbol under test")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--start", default=DEFAULT_START, help="target window start")
    ap.add_argument("--end", default=DEFAULT_END, help="target window end")
    ap.add_argument("--peer", default=DEFAULT_PEER,
                    help="peer symbol for cross comparability ('' to skip)")
    ap.add_argument("--tolerance", type=float, default=0.99,
                    help="minimum shared-bar ratio over the overlap")
    ap.add_argument("--bars-per-session", type=int, default=BARS_PER_SESSION_5M)
    ap.add_argument("--json", action="store_true", help="print the three reports as JSON")
    return ap


def exit_code_for_statuses(statuses) -> int:
    """Pure mapping from the check verdicts to the process exit code.

    `0` only when *every* verdict is `pass`; `1` as soon as one is `fail`/`insufficient`.
    `2` is never produced here -- execution errors return it directly in `main()`.
    """
    return EXIT_ALL_PASS if all(s == STATUS_PASS for s in statuses) else EXIT_NOT_ALL_PASS


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    start = pd.to_datetime(args.start).date()
    end = pd.to_datetime(args.end).date()

    try:
        con = _connect(args.db)
    except (FileNotFoundError, OSError, duckdb.Error) as exc:
        print(f"[ERROR] cannot open cache read-only: {exc}", file=sys.stderr)
        return EXIT_USAGE_OR_IO

    try:
        providers = _providers(con, args.symbol, args.interval)
        if not providers:
            print(f"[ERROR] no {args.interval} rows for {args.symbol} in {args.db}",
                  file=sys.stderr)
            return EXIT_USAGE_OR_IO
        used_provider, used_rows = providers[0]
        frame = _frame(con, args.symbol, args.interval, used_provider)

        coverage = check_coverage(
            frame, args.symbol, start, end, args.interval,
            min_coverage_ratio=1.0,
        )
        completeness = check_daily_completeness(
            frame, args.symbol, bars_per_session=args.bars_per_session
        )

        peer_frame = None
        comparability = None
        peer_provider = None
        if args.peer and args.peer != args.symbol:
            peer_providers = _providers(con, args.peer, args.interval)
            if peer_providers:
                peer_provider, _ = peer_providers[0]
                peer_frame = _frame(con, args.peer, args.interval, peer_provider)
                comparability = check_cross_comparability(
                    frame, peer_frame, args.tolerance,
                    label_a=args.symbol, label_b=args.peer,
                )

        starts = _series_starts(con, args.interval)
        spread = start_date_spread(args.symbol, starts)
    finally:
        con.close()

    if args.json:
        payload = {
            "coverage": coverage.as_dict(),
            "completeness": completeness.as_dict(),
            "comparability": None if comparability is None else comparability.as_dict(),
            "start_spread": spread.as_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"cache        : {args.db}  (read_only=True)")
        print(f"symbol       : {args.symbol} @ {args.interval}   "
              f"provider={used_provider} rows={used_rows}"
              + (f"  (other providers present: "
                 f"{', '.join(f'{p}:{n}' for p, n in providers[1:])})" if len(providers) > 1 else ""))
        print(f"target window: {start} .. {end}   bars/session={args.bars_per_session}")
        print()
        print(f"{'检查':<10} {'结论':<13} 关键数字")
        print("-" * 96)
        print(f"{'覆盖':<10} {_verdict(coverage):<13} "
              f"实际 {_fmt_ts(coverage.actual_start)} ~ {_fmt_ts(coverage.actual_end)} | "
              f"{coverage.bar_count} 根 / {len(coverage.observed_sessions)} 交易日 | "
              f"应覆盖 {len(coverage.expected_sessions)} 日({coverage.calendar_basis}) | "
              f"比值 {coverage.coverage_ratio:.3f} >= {coverage.min_coverage_ratio:.2f} | "
              f"缺口 {len(coverage.unexplained_missing)} 日 | {coverage.reason}")
        print(f"{'逐日完整性':<8} {_verdict(completeness):<13} "
              f"{len(completeness.sessions)} 交易日 | 短日 {len(completeness.short_sessions)} | "
              f"超量日 {len(completeness.overfull_sessions)} | "
              f"重复时间戳 {completeness.duplicate_ts_count} | "
              f"午休(11:30-13:00) bar {completeness.lunch_break_violations} | "
              f"{completeness.reason}")
        if comparability is None:
            print(f"{'横向可比':<8} {'SKIPPED':<13} 未指定 --peer")
        else:
            print(f"{'横向可比':<8} {_verdict(comparability):<13} "
                  f"vs {comparability.label_b}({peer_provider}) | "
                  f"重叠 {_fmt_ts(comparability.overlap_start)} ~ "
                  f"{_fmt_ts(comparability.overlap_end)} | "
                  f"共享 {comparability.shared_count} | "
                  f"比值 {comparability.bar_ratio:.4f} >= {comparability.tolerance:.2f} | "
                  f"{comparability.reason}")
        print("-" * 96)
        gap = spread.symbol_sessions_behind_earliest
        print(f"起点横向 : 本标的 {spread.starts.get(args.symbol)} | "
              f"全表最早 {spread.earliest_symbol} {spread.earliest_start} | "
              f"起点中位数 {spread.median_start} | "
              f"落后最早 {gap if gap is not None else 'n/a'} 根会话")
        shorter = ", ".join(spread.peers_shorter_than_symbol[:8]) or "无"
        print(f"更短的对  : {len(spread.peers_shorter_than_symbol)} 只（{shorter}）")

    statuses = [coverage.status, completeness.status]
    if comparability is not None:
        statuses.append(comparability.status)
    return exit_code_for_statuses(statuses)


if __name__ == "__main__":
    sys.exit(main())

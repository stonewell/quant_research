#!/usr/bin/env python
"""CLI entry point: mine this universe's aggregate portfolio turning-point
history for statistically significant indicator patterns, and write a
standalone, durable `pattern_report.json`.

Extracted from `strategy_generator`'s former in-process `--mine-patterns`
flag (see root README's pipeline docs) so mining results are durable and
reusable across multiple `strategy_generator` runs/parameter sweeps without
re-running this (Bonferroni-corrected, shuffle-null) mining pass every
time, and so EVERY significant finding is reported -- not just whichever
ones happened to be turned into templates and happened to win a single
generation run. `strategy_generator/run_strategygen.py`'s `--pattern-report`
flag consumes this stage's output.

Example:
    python run_pattern_mining.py --universe-file ../instrument_selection/results/basket.json
"""

import argparse
import os
import sys

_PM_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PM_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _PM_ROOT not in sys.path:
    sys.path.insert(0, _PM_ROOT)

from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.reporting import write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args
from pmine.pattern_mining import mine_indicator_patterns

RESULTS_DIR = default_results_dir(__file__)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Turning-Point Indicator Pattern Mining CLI Runner")
    add_universe_cli_args(p, default_universe=["SPY", "QQQ"])
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--interval", default="1d")
    p.add_argument("--pattern-min-swing-pct", type=float, default=0.05,
                   help="Minimum zigzag swing size (fraction) to confirm a turning point (default 0.05 = 5%%).")
    p.add_argument("--pattern-lag-bars", type=int, default=20,
                   help="How many trading days BEFORE each turning point to read indicators at, to avoid "
                        "the near-tautological result of reading them AT the turning point itself (default 20; "
                        "see pmine/pattern_mining.py's module docstring for why this matters).")
    add_data_provider_cli_args(p)
    return p


def main():
    args = build_arg_parser().parse_args()

    universe_symbols = resolve_universe_from_args(args, default_symbols=["SPY", "QQQ"])
    if not universe_symbols:
        raise ValueError("Resolved universe symbol list is empty.")

    data_kwargs = build_data_kwargs(args)
    universe = load_universe_with_banner(
        universe_symbols, args.start, args.end, args.interval,
        use_cache=not args.no_cache, cache_dir=shared_data_dir(),
        data_kwargs=data_kwargs, require_nonempty=True,
        cache_max_age_days=args.cache_ttl_days,
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)

    findings, status = mine_indicator_patterns(
        universe, min_swing_pct=args.pattern_min_swing_pct, lag_bars=args.pattern_lag_bars,
    )
    n_significant = int(findings["significant"].sum()) if not findings.empty else 0
    # 0 significant patterns (or status != "ok") is an expected, valid
    # outcome (especially on synthetic data) -- not an error. See
    # pmine/pattern_mining.py's module docstring.
    print(f"Pattern mining: status={status}, {n_significant} statistically significant indicator "
          f"pattern(s) out of {len(findings)} tested (Bonferroni-corrected, lag={args.pattern_lag_bars} bars).")

    report_path = os.path.join(RESULTS_DIR, "pattern_report.json")
    write_json_report({
        "run_context": {
            "data_provider": args.data_provider,
            "universe": universe_symbols,
            "start": args.start,
            "end": args.end,
            "min_swing_pct": args.pattern_min_swing_pct,
            "lag_bars": args.pattern_lag_bars,
        },
        "status": status,
        "findings": findings.to_dict(orient="records"),
    }, report_path)
    print(f"Saved pattern report to {report_path}")


if __name__ == "__main__":
    main()

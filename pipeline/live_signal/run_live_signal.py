#!/usr/bin/env python
"""CLI entry point: point-in-time buy/sell signal + rebalance instruction.

Given a universe and a `strategy.json` (the same file `strategy_generator`
produces and `backtester` consumes), reconstructs the strategy and runs it
with data truncated at a given "as of" date (default: today) -- no
lookahead -- then reports:
- the strategy's current target weights (its most recent rebalance at or
  before the as-of date)
- a buy/sell/hold classification and a concrete rebalance instruction,
  either against the user's actual current holdings (`--current-holdings`)
  or, if none are given, against the strategy's own previous rebalance
- the top-N symbols to buy, ranked by target weight

This project deliberately does no strategy search of its own -- like
`backtester`, it only reconstructs and re-runs an already-generated,
fixed strategy. Unlike `backtester`, it evaluates a single point in time
instead of a historical date range.

Example:
    python run_live_signal.py --strategy-file ../strategy_generator/results/strategy.json \\
        --universe SPY QQQ BIL --data-provider synthetic --as-of-date 2024-06-01
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

import pandas as pd

# Ensure this project's own directory (for bare `from lsig...` imports),
# pipeline/ (for research_strategy/fundamental_screener), and the repo root
# (for common) are all in sys.path.
_LIVE_SIGNAL_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_LIVE_SIGNAL_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
for _extra in (_LIVE_SIGNAL_ROOT, _PROJECT_ROOT, _REPO_ROOT):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_results_dir,
    load_universe_with_banner,
    shared_data_dir,
)
from common.reporting import write_json_report
from common.universe import add_universe_cli_args, resolve_universe_from_args
from common.strategy_spec import get_template, load_strategy_file

from lsig.signal import as_of_universe, compute_rebalance_instruction, latest_rebalance_rows, top_n_buys

RESULTS_DIR = default_results_dir(__file__)
DATA_DIR = shared_data_dir()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Point-in-time buy/sell signal + rebalance instruction")
    p.add_argument("--strategy-file", required=True, help="Path to strategy.json file exported by strategy_generator")
    add_universe_cli_args(p)
    p.add_argument("--as-of-date", type=str, default=None,
                   help="YYYY-MM-DD point-in-time date to evaluate the strategy at (default: today). "
                        "Works for any past date too -- data after this date is never consulted.")
    p.add_argument("--lookback-days", type=int, default=800,
                   help="Calendar days of history to load before --as-of-date (default: 800, ~2.2 years -- "
                        "comfortably covers every existing template's warmup_bars requirement).")
    p.add_argument("--current-holdings", type=str, default=None,
                   help="JSON object string of {symbol: weight_fraction} for your ACTUAL current portfolio "
                        "(e.g. '{\"SPY\": 0.6, \"BIL\": 0.4}'). Omit (with --current-holdings-file too) to "
                        "compare against the strategy's own previous rebalance instead.")
    p.add_argument("--current-holdings-file", type=str, default=None,
                   help="Path to a JSON file with the same {symbol: weight_fraction} shape as --current-holdings.")
    p.add_argument("--top-n", type=int, default=5, help="How many top buy candidates to highlight (ranked by target weight)")
    p.add_argument("--action-threshold", type=float, default=1e-6,
                   help="Minimum |weight delta| to count as a buy/sell rather than a hold (default: 1e-6)")
    p.add_argument("--interval", default="1d")
    add_data_provider_cli_args(p, default_provider="yfinance")
    p.add_argument("--results-dir", type=str, default=None,
                   help=f"Override the output directory for the JSON report/CSV instruction (default: {RESULTS_DIR})")
    p.add_argument("--cache-dir", type=str, default=None,
                   help=f"Override the shared, workspace-wide OHLCV CSV cache directory (default: {DATA_DIR})")
    return p


def _load_current_holdings(args) -> dict:
    if args.current_holdings and args.current_holdings_file:
        raise ValueError("Pass at most one of --current-holdings / --current-holdings-file, not both.")
    if args.current_holdings:
        return json.loads(args.current_holdings)
    if args.current_holdings_file:
        with open(args.current_holdings_file, "r") as f:
            return json.load(f)
    return None


def main():
    args = build_arg_parser().parse_args()
    results_dir = args.results_dir or RESULTS_DIR
    cache_dir = args.cache_dir or DATA_DIR
    os.makedirs(results_dir, exist_ok=True)

    as_of_date = args.as_of_date or date.today().isoformat()
    start = (pd.Timestamp(as_of_date) - timedelta(days=args.lookback_days)).date().isoformat()

    strategy_def = load_strategy_file(args.strategy_file)
    template_name = strategy_def["template_name"]
    params = strategy_def["params"]
    template = get_template(
        template_name, strategy_def.get("pattern_spec"), strategy_def.get("research_strategy_spec"),
        strategy_def.get("composite_spec"), params, strategy_def.get("fundamental_spec"),
        strategy_def.get("bnn_spec"),
    )

    print(f"Loaded strategy: {template_name}")
    print(f"Parameters: {params}")
    print(f"Logic: {strategy_def.get('explanation', '')}")
    print(f"As-of date: {as_of_date} (loading {start}..{as_of_date})")
    print()

    universe_symbols = resolve_universe_from_args(args)
    if not universe_symbols:
        raise ValueError("No universe symbols provided or resolved. Pass --universe, --universe-file, or --universe-provider.")

    universe = load_universe_with_banner(
        universe_symbols, start, as_of_date, args.interval,
        use_cache=not args.no_cache, cache_dir=cache_dir,
        data_kwargs=build_data_kwargs(args), require_nonempty=True,
        cache_max_age_days=args.cache_ttl_days,
    )
    # Belt-and-suspenders: guarantee no lookahead regardless of what the
    # provider actually returned for `end`.
    universe = as_of_universe(universe, as_of_date)

    warmup_bars = template.warmup_bars(params)
    short_symbols = [sym for sym, df in universe.items() if len(df) < warmup_bars]
    if short_symbols:
        print(f"WARNING: {short_symbols} have fewer than {warmup_bars} bars of history before "
              f"{as_of_date} (this template's warmup_bars requirement) -- their signal may be "
              f"unreliable or absent. Consider raising --lookback-days.")

    sparse_weights = template.generate_weights(universe, params)
    rebalances = latest_rebalance_rows(sparse_weights)

    if rebalances.empty:
        report = {
            "status": "no_signal",
            "run_context": {"as_of_date": as_of_date, "template_name": template_name, "universe": universe_symbols},
            "message": f"No rebalance occurred at or before {as_of_date} -- insufficient warmup/history "
                       f"for this template, or the as-of date is too early. Try raising --lookback-days "
                       f"or an earlier --as-of-date.",
        }
        print(report["message"])
        write_json_report(report, os.path.join(results_dir, "live_signal_report.json"))
        return

    current_row = rebalances.iloc[-1]
    current_date = rebalances.index[-1]

    holdings = _load_current_holdings(args)
    if holdings is not None:
        # Intentionally NOT reindexed to current_row's own index here --
        # a symbol the user actually holds that the strategy's universe
        # doesn't cover must still show up as a full sell (compute_rebalance_
        # instruction's own union-of-symbols alignment handles that).
        reference = pd.Series(holdings, dtype=float)
        reference_source = "user-supplied current holdings"
    elif len(rebalances) >= 2:
        reference = rebalances.iloc[-2].fillna(0.0)
        reference_source = "strategy's own previous rebalance"
    else:
        reference = pd.Series(0.0, index=current_row.index)
        reference_source = "no prior rebalance (first signal) -- treated as starting from cash"

    instruction = compute_rebalance_instruction(current_row, reference, args.action_threshold)
    buys = instruction[instruction["action"] == "buy"]
    sells = instruction[instruction["action"] == "sell"]
    top_buys = top_n_buys(instruction, args.top_n)

    print(f"Signal date (most recent rebalance at/before {as_of_date}): {current_date.date()}")
    print(f"Reference: {reference_source}")
    print()
    print(f"=== Top {args.top_n} Buy Candidates ===")
    if top_buys.empty:
        print("  (none)")
    else:
        for sym, row in top_buys.iterrows():
            tag = " [NEW]" if row["is_new_position"] else ""
            print(f"  {sym}: target {row['target_weight']*100:.1f}% (delta +{row['delta']*100:.1f}%){tag}")
    print()
    print(f"=== Full Buy Signal ({len(buys)}) ===")
    for sym, row in buys.iterrows():
        print(f"  BUY  {sym}: {row['reference_weight']*100:.1f}% -> {row['target_weight']*100:.1f}% (+{row['delta']*100:.1f}%)")
    print(f"=== Full Sell Signal ({len(sells)}) ===")
    for sym, row in sells.iterrows():
        print(f"  SELL {sym}: {row['reference_weight']*100:.1f}% -> {row['target_weight']*100:.1f}% ({row['delta']*100:.1f}%)")

    report = {
        "status": "ok",
        "run_context": {
            "as_of_date": as_of_date, "signal_date": str(current_date.date()),
            "template_name": template_name, "universe": universe_symbols,
            "reference_source": reference_source,
        },
        "current_target_weights": current_row.dropna().to_dict(),
        "reference_weights": reference.to_dict(),
        "buy_signal": buys.reset_index(names="symbol").to_dict(orient="records"),
        "top_n_buys": top_buys.reset_index(names="symbol").to_dict(orient="records"),
        "sell_signal": sells.reset_index(names="symbol").to_dict(orient="records"),
        "rebalance_instruction": instruction.reset_index(names="symbol").to_dict(orient="records"),
    }
    write_json_report(report, os.path.join(results_dir, "live_signal_report.json"))
    instruction.reset_index(names="symbol").to_csv(os.path.join(results_dir, "live_signal_instruction.csv"), index=False)
    print()
    print(f"Saved JSON report to {os.path.join(results_dir, 'live_signal_report.json')}")
    print(f"Saved rebalance instruction to {os.path.join(results_dir, 'live_signal_instruction.csv')}")


if __name__ == "__main__":
    main()

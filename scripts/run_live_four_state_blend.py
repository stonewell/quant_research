#!/usr/bin/env python3
"""Operational Daily Live Execution Runner for Chan Four-State Risk-Managed Blend Strategy.

NOTE: This script is maintained for backwards compatibility.
The universal runner supporting all strategies is now available at:
    `scripts/run_live_strategy.py`

Usage of this wrapper forwards directly to `scripts/run_live_strategy.py`
with `--strategy chan_four_state_blend` as default.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_REPO_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

# Re-export all functions, classes, and constants from the universal live strategy runner
from scripts.run_live_strategy import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_STRATEGY_FILE,
    DEFAULT_STRATEGY_KEY,
    DEFAULT_UNIVERSE_FILE,
    _FALLBACK_STOCK_NAMES,
    _INVALID_STOCK_NAME_WORDS,
    _clean_stock_name_candidate,
    load_holdings,
    load_symbol_names,
    main,
    pad_east_asian,
    parse_args,
    resolve_blend_param,
    resolve_holdings_weights_and_shares,
    resolve_lot_size,
    resolve_strategy_definition,
    resolve_trade_inertia_threshold,
)

if __name__ == "__main__":
    main()

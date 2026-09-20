#!/usr/bin/bash
set -x

find research_strategy/results/strategy_dumps/ -type f -name "*.json" -print0 | while IFS= read -r -d '' file; do
    # Get just the filename (e.g., "document.txt")
    filename=$(basename "$file")

    # Strip the extension (e.g., "document")
    name_only="${filename%.*}"

    echo "$name_only"

	uv run python ../backtester/run_backtest.py \
	   --strategy-file $file \
	   --mode walkforward \
	   --window-years 0.5 \
	   --step-years 0.5 \
	   --universe-file ../docs/universe/china/50_stock.txt \
	   --end  2026-01-01 \
	   --start 2022-01-01 \
	   --data-provider fuyao \
	   --min-shares=100 \
	   --results-dir results/$name_only \
	   --baseline-symbol 000300.SH
done

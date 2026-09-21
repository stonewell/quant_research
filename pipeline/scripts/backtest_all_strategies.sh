#!/usr/bin/env bash
set -x
cd "$(dirname "$0")/.."

UNIVERSE=${1:-"../docs/universe/china/50_stock.txt"}
RESULTS=${2:-"results"}
START=${3:-"2022-01-01"}
END=${4:-"2026-01-01"}

uv run python research_strategy/run_research_strategy.py --dump-strategies

find research_strategy/results/strategy_dumps/ -type f -name "*.json" -print0 | while IFS= read -r -d '' file; do
    # Get just the filename (e.g., "document.txt")
    filename=$(basename "$file")

    # Strip the extension (e.g., "document")
    name_only="${filename%.*}"

    echo "$name_only"

	uv run python ../backtester/run_backtest.py \
	   --strategy-file "$file" \
	   --mode walkforward \
	   --window-years 0.5 \
	   --step-years 0.5 \
	   --universe-file "${UNIVERSE}" \
	   --end ${END} \
	   --start ${START} \
	   --data-provider fuyao \
	   --min-shares=100 \
	   --results-dir "${RESULTS}/${name_only}" \
	   --china-trading \
	   --baseline-symbol 000300.SH
done

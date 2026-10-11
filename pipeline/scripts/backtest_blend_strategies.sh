#!/usr/bin/env bash
set -e
set -x

cd "$(dirname "$0")/.."

# Default parameter configurations
UNIVERSE="../docs/universe/china/50_stock.txt"
RESULTS="results"
START="2022-01-01"
END="2026-01-01"
WEIGHTING_MODE="${WEIGHTING_MODE:-"static"}"
ENABLE_ORTHOGONALIZATION="${ENABLE_ORTHOGONALIZATION:-"false"}"

# Parse named flags and/or positional arguments
positional_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --weighting-mode|--weighting_mode)
            WEIGHTING_MODE="$2"
            shift 2
            ;;
        --enable-orthogonalization|--enable_orthogonalization)
            if [[ -n "$2" && "$2" != --* ]]; then
                ENABLE_ORTHOGONALIZATION="$2"
                shift 2
            else
                ENABLE_ORTHOGONALIZATION="true"
                shift 1
            fi
            ;;
        --universe)
            UNIVERSE="$2"
            shift 2
            ;;
        --results)
            RESULTS="$2"
            shift 2
            ;;
        --start)
            START="$2"
            shift 2
            ;;
        --end)
            END="$2"
            shift 2
            ;;
        *)
            positional_args+=("$1")
            shift
            ;;
    esac
done

# Map positional arguments in order if supplied
if [ ${#positional_args[@]} -ge 1 ]; then UNIVERSE="${positional_args[0]}"; fi
if [ ${#positional_args[@]} -ge 2 ]; then RESULTS="${positional_args[1]}"; fi
if [ ${#positional_args[@]} -ge 3 ]; then START="${positional_args[2]}"; fi
if [ ${#positional_args[@]} -ge 4 ]; then END="${positional_args[3]}"; fi
if [ ${#positional_args[@]} -ge 5 ]; then WEIGHTING_MODE="${positional_args[4]}"; fi
if [ ${#positional_args[@]} -ge 6 ]; then ENABLE_ORTHOGONALIZATION="${positional_args[5]}"; fi

# Normalize boolean for enable_orthogonalization
ORTHO_BOOL="false"
if [[ "$ENABLE_ORTHOGONALIZATION" =~ ^(true|True|TRUE|1|yes|Yes|y|Y)$ ]]; then
    ORTHO_BOOL="true"
fi

echo "=== Blend Strategies Backtest Configuration ==="
echo "Universe:                  ${UNIVERSE}"
echo "Results Dir:               ${RESULTS}"
echo "Start Date:                ${START}"
echo "End Date:                  ${END}"
echo "Weighting Mode:            ${WEIGHTING_MODE}"
echo "Enable Orthogonalization:  ${ORTHO_BOOL}"
echo "=============================================="

# Step 1: Dump baseline strategy definition files
uv run python research_strategy/run_research_strategy.py --dump-strategies

# Step 2: Inject weighting_mode and enable_orthogonalization into dumped blend strategies
uv run python -c "
import json, glob, sys
mode = sys.argv[1]
ortho = sys.argv[2].lower() in ('true', '1', 'yes')
for p in glob.glob('research_strategy/results/strategy_dumps/*blend*.json'):
    with open(p, 'r') as f:
        d = json.load(f)
    d.setdefault('params', {})['blend_weighting_mode'] = mode
    d['params']['blend_enable_orthogonalization'] = ortho
    d['params']['weighting_mode'] = mode
    d['params']['enable_orthogonalization'] = ortho
    if 'research_strategy_spec' in d and 'entry_data' in d['research_strategy_spec']:
        ep = d['research_strategy_spec']['entry_data'].setdefault('parameters', {})
        ep['blend_weighting_mode'] = mode
        ep['blend_enable_orthogonalization'] = ortho
        ep['weighting_mode'] = mode
        ep['enable_orthogonalization'] = ortho
    with open(p, 'w') as f:
        json.dump(d, f, indent=2)
" "${WEIGHTING_MODE}" "${ORTHO_BOOL}"

STRAT_PARAMS="{\"blend_weighting_mode\": \"${WEIGHTING_MODE}\", \"blend_enable_orthogonalization\": ${ORTHO_BOOL}, \"weighting_mode\": \"${WEIGHTING_MODE}\", \"enable_orthogonalization\": ${ORTHO_BOOL}}"

# Step 3: Run walk-forward backtests across all blend strategies
find research_strategy/results/strategy_dumps/ -type f -name "*blend*.json" -print0 | while IFS= read -r -d '' file; do
    filename=$(basename "$file")
    name_only="${filename%.*}"
    echo "Running backtest for: $name_only"

    uv run python ../backtester/run_backtest.py \
       --strategy-file "$file" \
       --strategy-params "${STRAT_PARAMS}" \
       --mode walkforward \
       --window-years 0.25 \
       --step-years 0.25 \
       --universe-file "${UNIVERSE}" \
       --end ${END} \
       --start ${START} \
       --data-provider fuyao \
       --min-shares=100 \
       --results-dir "${RESULTS}/${name_only}" \
       --china-trading \
       --baseline-symbol 000300.SH
done

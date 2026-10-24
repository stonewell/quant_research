#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Blend strategy batch backtest -- THIN WRAPPER around
# scripts/run_batch_backtest.py (`B2.3`).
#
# This used to be a `find ... | while read` loop launching `run_backtest.py`
# once per dumped blend strategy: 55 interpreter cold starts plus 55 full
# DuckDB re-queries of the same 50-symbol universe. The loop is gone; the batch
# entry point loads the universe ONCE in a single process and fans the
# strategies out to a process pool (fork/COW -- workers inherit the loaded
# frames instead of re-reading the cache).
#
# Old usage -> new usage (unchanged for callers):
#   ./backtest_blend_strategies.sh [UNIVERSE] [RESULTS] [START] [END] \
#                                  [WEIGHTING_MODE] [ENABLE_ORTHOGONALIZATION] [EXTRA_PARAMS]
#   ./backtest_blend_strategies.sh --universe X --results R --start S --end E \
#                                  --weighting-mode M --enable-orthogonalization [true|false] \
#                                  --extra-params '{"k": v}'
#
# Per-flag mapping:
#   UNIVERSE / --universe        -> --universe-file
#   RESULTS  / --results         -> --output-dir   (per-strategy artifacts still land in
#                                   ${RESULTS}/<dump file stem>/, exactly as before)
#   START/END                    -> --start / --end
#   WEIGHTING_MODE               -> part of --strategy-params (blend_weighting_mode / weighting_mode)
#   ENABLE_ORTHOGONALIZATION     -> part of --strategy-params (blend_enable_orthogonalization /
#                                   enable_orthogonalization)
#   EXTRA_PARAMS                 -> merged into --strategy-params
#   (hardcoded fuyao)            -> --data-provider "${DATA_PROVIDER:-fuyao}"  (new override)
#   (55 serial processes)        -> --n-jobs "${BATCH_N_JOBS}"  (unset = auto)
#   (nothing)                    -> ${RESULTS}/batch_ranking.csv|batch_summary.json|batch_failed.csv
#
# Measured (2026-10-23, offline/synthetic, 6 blend dumps, 4 folds each, median
# of 3): old serial loop 52.9s, --n-jobs 1 102.7s, --n-jobs 2 56.1s,
# --n-jobs 6 26.0s, auto 26.8s. So AUTO (unset BATCH_N_JOBS) is the default and
# `BATCH_N_JOBS` should not be set to 1 to "be safe": a strategy worker keeps its
# folds serial (INNER_N_JOBS=1), so `-j 1` gives up BOTH pools. INNER_N_JOBS=auto
# reproduces the old loop's fold fan-out (52.9s) if you ever have fewer
# strategies than cores, but plain AUTO beats it.
#
# Steps 1-2 (dump the strategies, then bake the weighting mode / orthogonalization
# / extra params into the dumped JSONs) are KEPT verbatim: they produce artifacts
# other tooling reads, and they are data preparation, not the serial loop. The
# same params are additionally passed through `--strategy-params`, which is how
# `run_backtest.py` itself merges them (the old `--strategy-params "$STRAT_PARAMS"`
# behavior).
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

# Default parameter configurations
UNIVERSE="../docs/universe/china/50_stock.txt"
RESULTS="results"
START="2022-01-01"
END="2026-01-01"
WEIGHTING_MODE="${WEIGHTING_MODE:-"rolling_ic_ir"}"
ENABLE_ORTHOGONALIZATION="${ENABLE_ORTHOGONALIZATION:-"true"}"
EXTRA_PARAMS="${EXTRA_PARAMS:-"{}"}"
# New (optional) knobs; defaults reproduce the old script's behavior.
DATA_PROVIDER="${DATA_PROVIDER:-"fuyao"}"
BATCH_N_JOBS="${BATCH_N_JOBS:-""}"          # empty = auto: min(cpu_count, n_strategies)
INNER_N_JOBS="${INNER_N_JOBS:-"1"}"         # fold workers inside a strategy; 1 = never nest pools
WINDOW_YEARS="${WINDOW_YEARS:-"0.25"}"
STEP_YEARS="${STEP_YEARS:-"0.25"}"
BASELINE_SYMBOL="${BASELINE_SYMBOL:-"000300.SH"}"   # empty string disables the benchmark

# Parse named flags and/or positional arguments
positional_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --weighting-mode|--weighting_mode)
            WEIGHTING_MODE="$2"
            shift 2
            ;;
        --enable-orthogonalization|--enable_orthogonalization)
            if [[ -n "${2:-}" && "$2" != --* ]]; then
                ENABLE_ORTHOGONALIZATION="$2"
                shift 2
            else
                ENABLE_ORTHOGONALIZATION="true"
                shift 1
            fi
            ;;
        --extra-params|--strategy-params)
            EXTRA_PARAMS="$2"
            shift 2
            ;;
        --universe)
            UNIVERSE="$2"
            shift 2
            ;;
        --results|--output-dir)
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
        --data-provider)
            DATA_PROVIDER="$2"
            shift 2
            ;;
        --n-jobs|-j)
            BATCH_N_JOBS="$2"
            shift 2
            ;;
        --inner-n-jobs)
            INNER_N_JOBS="$2"
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
if [ ${#positional_args[@]} -ge 7 ]; then EXTRA_PARAMS="${positional_args[6]}"; fi

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
echo "Extra Params:              ${EXTRA_PARAMS}"
echo "Data Provider:             ${DATA_PROVIDER}"
echo "Batch Workers:             ${BATCH_N_JOBS:-"auto"}"
echo "Inner (fold) Workers:      ${INNER_N_JOBS}"
echo "=============================================="

# Step 1: Dump baseline strategy definition files
uv run python research_strategy/run_research_strategy.py --dump-strategies

# Step 2: Inject weighting_mode, enable_orthogonalization, and extra_params into dumped blend strategies
uv run python -c "
import json, glob, sys
mode = sys.argv[1]
ortho = sys.argv[2].lower() in ('true', '1', 'yes')
extra_raw = sys.argv[3] if len(sys.argv) > 3 else '{}'
try:
    extra_params = json.loads(extra_raw) if extra_raw.strip() else {}
except Exception as e:
    print(f'Warning: Failed to parse extra_params JSON: {e}', file=sys.stderr)
    extra_params = {}

for p in glob.glob('research_strategy/results/strategy_dumps/*blend*.json'):
    with open(p, 'r') as f:
        d = json.load(f)
    params = d.setdefault('params', {})
    params['blend_weighting_mode'] = mode
    params['blend_enable_orthogonalization'] = ortho
    params['weighting_mode'] = mode
    params['enable_orthogonalization'] = ortho
    params.update(extra_params)
    if 'research_strategy_spec' in d and 'entry_data' in d['research_strategy_spec']:
        ep = d['research_strategy_spec']['entry_data'].setdefault('parameters', {})
        ep['blend_weighting_mode'] = mode
        ep['blend_enable_orthogonalization'] = ortho
        ep['weighting_mode'] = mode
        ep['enable_orthogonalization'] = ortho
        ep.update(extra_params)
    with open(p, 'w') as f:
        json.dump(d, f, indent=2)
" "${WEIGHTING_MODE}" "${ORTHO_BOOL}" "${EXTRA_PARAMS}"

STRAT_PARAMS=$(uv run python -c "
import json, sys
base = {
    'blend_weighting_mode': sys.argv[1],
    'blend_enable_orthogonalization': sys.argv[2].lower() in ('true', '1', 'yes'),
    'weighting_mode': sys.argv[1],
    'enable_orthogonalization': sys.argv[2].lower() in ('true', '1', 'yes')
}
extra_raw = sys.argv[3] if len(sys.argv) > 3 else '{}'
try:
    extra = json.loads(extra_raw) if extra_raw.strip() else {}
except Exception:
    extra = {}
base.update(extra)
print(json.dumps(base))
" "${WEIGHTING_MODE}" "${ORTHO_BOOL}" "${EXTRA_PARAMS}")

# Step 3: ONE batch invocation for every dumped blend strategy (was: one
# `uv run python ../backtester/run_backtest.py` per file).
BATCH_ARGS=(
    ../scripts/run_batch_backtest.py
    --strategies-glob "research_strategy/results/strategy_dumps/*blend*.json"
    --strategy-params "${STRAT_PARAMS}"
    --universe-file "${UNIVERSE}"
    --start "${START}"
    --end "${END}"
    --window-years "${WINDOW_YEARS}"
    --step-years "${STEP_YEARS}"
    --data-provider "${DATA_PROVIDER}"
    --min-shares 100
    --china-trading
    --output-dir "${RESULTS}"
    --results-subdir stem
    --inner-n-jobs "${INNER_N_JOBS}"
)
if [ -n "${BASELINE_SYMBOL}" ]; then
    BATCH_ARGS+=(--baseline-symbol "${BASELINE_SYMBOL}")
fi
if [ -n "${BATCH_N_JOBS}" ]; then
    BATCH_ARGS+=(--n-jobs "${BATCH_N_JOBS}")
fi

uv run python "${BATCH_ARGS[@]}"

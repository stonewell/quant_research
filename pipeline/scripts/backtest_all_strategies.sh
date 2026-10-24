#!/usr/bin/env bash
set -x
# Repo root from THIS file's location (never `$PWD`), so the in-repo default pool
# below resolves from any cwd: `./backtest_all_strategies.sh`,
# `bash pipeline/scripts/backtest_all_strategies.sh`, or an absolute-path call.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
INVOKE_DIR="$(pwd -P)"
cd "${REPO_ROOT}/pipeline"

# Default pool: 22 core-satellite names (17 high-dividend SOE cores + 5 momentum
# satellites) -- the same in-repo default `run_blend_cross_matrix.py` and the live
# scripts resolve. `P1.6`-class regression this replaces: the old default pointed at
# `50_stock.txt` sitting directly under `docs/universe/china/`; commit `89f8388`
# moved that pool into the Chinese-named subdirectory
# `50只A股高流动性优质股票精选/`, so the bare path stopped resolving and every
# invocation died inside run_backtest.py instead of failing here.
DEFAULT_UNIVERSE="${REPO_ROOT}/docs/universe/china/core_satellite_22_stocks.txt"

usage() {
    local _status="MISSING"
    if [[ -f "${DEFAULT_UNIVERSE}" ]]; then _status="exists"; fi
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [UNIVERSE] [RESULTS] [START] [END]
       $(basename "${BASH_SOURCE[0]}") --universe X --results R [--start S] [--end E] [--help]

Serial (one process per dumped strategy) walkforward backtest of EVERY strategy
in research_strategy/results/strategy_dumps/. Kept for reference/back-compat:
the batch entry point (\`scripts/run_batch_backtest.py\`, wrapped by
\`backtest_blend_strategies.sh\`) loads the universe once and is much faster.

Universe paths may be absolute, relative to the caller's cwd, or relative to the
repo root (e.g. docs/universe/china/core_satellite_22_stocks.txt).
RESULTS is resolved the same way and is always reported as an ABSOLUTE path
before any work, so a run can never land in an unexpected directory silently:
--results pipeline/results/blend_16 (from the repo root) and --results
results/blend_16 (from pipeline/) both mean <repo>/pipeline/results/blend_16.
Omitting RESULTS keeps the historical default: <repo>/pipeline/results.
--help/-h is honoured in ANY argument position.
Set BLEND_BATCH_DRY_RUN=1 to resolve/validate the universe and stop before any work.

Default universe: ${DEFAULT_UNIVERSE}
                  (${_status})
EOF
}

# `--help`/`-h` must win from ANY position, not only argv[1]. This pre-scan runs
# BEFORE the positional mapping below, because `--help` sitting in a positional
# slot (argv[2], argv[4], ...) used to be silently consumed as RESULTS -- a full
# serial pass would then start writing into a directory literally named `--help`.
# A flag whose value is the literal string `--help` is pathological enough to ignore.
for _arg in "$@"; do
    case "${_arg}" in
        -h|--help)
            usage
            exit 0
            ;;
    esac
done
unset _arg

UNIVERSE="${DEFAULT_UNIVERSE}"
RESULTS="results"
# Same flag as in backtest_blend_strategies.sh: only an EXPLICIT results location
# goes through caller-cwd-first resolution. The default must keep landing in
# `pipeline/results` exactly as before.
RESULTS_EXPLICIT=0
START="2022-01-01"
END="2026-01-01"

# The legacy form is positional ([UNIVERSE] [RESULTS] [START] [END]); the named
# flags are accepted too so the doc-style invocation works here as well and so a
# `--results` can never be swallowed as a positional value. Whatever is left over
# is mapped positionally, in order, exactly as before.
positional_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --universe)
            UNIVERSE="$2"
            shift 2
            ;;
        --results|--output-dir)
            RESULTS="$2"
            RESULTS_EXPLICIT=1
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
        -*)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 2
            ;;
        *)
            positional_args+=("$1")
            shift
            ;;
    esac
done

if [ ${#positional_args[@]} -ge 1 ]; then UNIVERSE="${positional_args[0]}"; fi
if [ ${#positional_args[@]} -ge 2 ]; then RESULTS="${positional_args[1]}"; RESULTS_EXPLICIT=1; fi
if [ ${#positional_args[@]} -ge 3 ]; then START="${positional_args[2]}"; fi
if [ ${#positional_args[@]} -ge 4 ]; then END="${positional_args[3]}"; fi

# Same resolution order as backtest_blend_strategies.sh: absolute, then caller's
# cwd, then repo root (how every doc snippet writes it), then the pipeline/
# workdir (historical). Unresolvable paths are passed through verbatim so the
# guard names what the operator actually typed.
resolve_universe_path() {
    local candidate="$1"
    if [[ "${candidate}" = /* ]]; then
        printf '%s\n' "${candidate}"
        return 0
    fi
    local base
    for base in "${INVOKE_DIR}" "${REPO_ROOT}" "${PWD}"; do
        if [[ -f "${base}/${candidate}" ]]; then
            printf '%s\n' "${base}/${candidate}"
            return 0
        fi
    done
    printf '%s\n' "${candidate}"
}

# Same result as backtest_blend_strategies.sh's resolve_output_path -- keep the two
# textually in sync. Absolute; else the candidate is anchored at the first base
# (caller's cwd, then repo root, then the pipeline/ workdir) that already contains
# its FIRST path component. A results directory usually does not exist yet, so the
# anchor is the leading component rather than the whole path.
#
# The bug: the script `cd`s into pipeline/ before parsing, so the doc-style
# `--results pipeline/results/blend_16` from the repo root used to write to
# <repo>/pipeline/pipeline/results/blend_16 -- created silently, run "succeeds".
resolve_output_path() {
    local candidate="$1"
    while [[ "${candidate}" == ./* ]]; do candidate="${candidate#./}"; done
    while [[ "${#candidate}" -gt 1 && "${candidate}" == */ ]]; do candidate="${candidate%/}"; done
    if [[ "${candidate}" = /* ]]; then
        printf '%s\n' "${candidate}"
        return 0
    fi
    local anchor="${candidate%%/*}"
    local base
    for base in "${INVOKE_DIR}" "${REPO_ROOT}" "${PWD}"; do
        if [[ -n "${anchor}" && -e "${base}/${anchor}" ]]; then
            printf '%s\n' "${base}/${candidate}"
            return 0
        fi
    done
    printf '%s\n' "${PWD}/${candidate}"
}

UNIVERSE="$(resolve_universe_path "${UNIVERSE}")"
if [[ ! -f "${UNIVERSE}" ]]; then
    echo "Error: universe file not found at '${UNIVERSE}'" >&2
    echo "       Pass an existing pool as the first argument (absolute, or relative to the" >&2
    echo "       caller's cwd / repo root). Built-in default: ${DEFAULT_UNIVERSE}" >&2
    exit 1
fi

# `[[ -f ]]` alone is NOT a sufficient guard, and this repo has the concrete case
# that proves it. `docs/universe/china/core_satellite_22_stocks.txt` and 4 of its
# siblings are tracked mode-`120000` SYMLINKS with RELATIVE targets (their target
# files are tracked `100644`, so they resolve fine in a clean Linux/macOS clone).
# But a Windows checkout with `core.symlinks=false` -- git's default without
# Developer Mode -- materialises such a link as a small PLAIN TEXT FILE whose
# whole content is the link target string. `[[ -f ]]` then PASSES and the ticker
# parser reads `核心卫星组合精选(...)/core_satellite_22_stocks.txt` as the
# universe: the batch silently runs on a 1-token garbage pool instead of erroring.
# So: resolve the PATH first (missing -> the pinned
# `Error: universe file not found at '<path>'`), then validate the CONTENT
# (too few well-formed tickers -> the degraded-symlink message).
#
# Threshold >= 5: every real pool in `docs/universe/china/` clears it by a wide
# margin, while a link-target string (1 token) and a comments/blanks-only file
# (0 tokens) always trip it. The Python side (`run_blend_cross_matrix.py`'s
# load_universe_symbols) rejects both cases too -- `_MIN_UNIVERSE_SYMBOLS = 2`
# plus a strict `^\d{6}\.(SH|SZ|BJ)$` check -- but that runs INSIDE the
# orchestrator; the shell check is deliberately the earlier, identical-wording
# fail-fast. Keep the two scripts' guards textually in sync.
UNIVERSE_MIN_SYMBOLS=5
count_valid_universe_symbols() {
    # One ticker per line; blank lines and `#` comments skipped; commas count as
    # separators; case-insensitive; tolerant of CRLF (Windows) checkouts.
    awk '
        /^[[:space:]]*#/ { next }
        {
            ntok = split($0, tok, /[[:space:],]+/)
            for (i = 1; i <= ntok; i++) {
                if (toupper(tok[i]) ~ /^[0-9][0-9][0-9][0-9][0-9][0-9]\.(SH|SZ|BJ)$/) count++
            }
        }
        END { print count + 0 }
    ' "$1"
}

UNIVERSE_SYMBOL_COUNT="$(count_valid_universe_symbols "${UNIVERSE}")"
if [[ "${UNIVERSE_SYMBOL_COUNT}" -lt "${UNIVERSE_MIN_SYMBOLS}" ]]; then
    echo "Error: universe file '${UNIVERSE}' parsed to only ${UNIVERSE_SYMBOL_COUNT} valid China A-share ticker(s); at least ${UNIVERSE_MIN_SYMBOLS} 'NNNNNN.SH|SZ|BJ' tickers are required (one per line, blank lines and '#' comments ignored)." >&2
    echo "       Hint: if this is a git symlink materialised as a text file, the checkout has core.symlinks=false" >&2
    echo "       (git's Windows default without Developer Mode): such a file's content is the LINK TARGET," >&2
    echo "       not a pool. Check 'git ls-files -s <path>' (mode 120000) and 'cat <path>'; fix with" >&2
    echo "       'git -c core.symlinks=true checkout -- <path>' or by enabling Developer Mode." >&2
    echo "       Built-in default: ${DEFAULT_UNIVERSE}" >&2
    exit 1
fi

# Resolve the output directory BEFORE any work and report it as an ABSOLUTE path:
# the old script echoed nothing, so a mis-landing was invisible. The default
# (nothing supplied) stays pipeline/results, exactly as before.
if [[ "${RESULTS_EXPLICIT}" == "1" ]]; then
    RESULTS="$(resolve_output_path "${RESULTS}")"
else
    RESULTS="${PWD}/results"
fi

echo "=== Serial Per-Strategy Walkforward Batch ==="
echo "Universe:     ${UNIVERSE}"
echo "Results Dir:  ${RESULTS}"
echo "Start:        ${START}"
echo "End:          ${END}"
echo "============================================="

# `BLEND_BATCH_DRY_RUN=1` stops here: universe resolved and content-validated,
# nothing written, no strategy dump and no backtest. Used by
# `pipeline/tests/test_script_default_paths.py` to prove the default resolves
# from any cwd without touching the cache/network.
#
# Nothing in this wrapper creates the results directory itself -- that happens
# inside `run_backtest.py` -- so the dry-run stop is already earlier than any
# write, and the results dir (resolved above) does not exist afterwards.
#
# The variable is deliberately NAMESPACED rather than a bare `DRY_RUN`: `DRY_RUN`
# is a common exported variable (CI images, docker envs, unrelated tools), so a
# stray `DRY_RUN=1` in the operator's environment would otherwise SILENTLY skip a
# real multi-hour batch run. `DRY_RUN` is not honoured here at all -- not even as
# an alias -- and the message below is unmistakable so a dry run can never be
# mistaken for a completed one. Same name as the sibling blend wrapper.
if [[ "${BLEND_BATCH_DRY_RUN:-0}" == "1" ]]; then
    echo "DRY RUN (BLEND_BATCH_DRY_RUN=1): resolved ${UNIVERSE} (${UNIVERSE_SYMBOL_COUNT} valid symbols); stopping before the strategy dump (results would go to ${RESULTS})."
    exit 0
fi

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

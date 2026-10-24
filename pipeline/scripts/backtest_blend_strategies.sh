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
#                                   Resolved to an ABSOLUTE path before use and printed
#                                   as such in the banner: absolute -> as-is, else
#                                   ${INVOKE_DIR} -> repo root -> pipeline/ (see
#                                   resolve_output_path). Both documented spellings land
#                                   in the same place: `--results pipeline/results/blend_16`
#                                   from the repo root and `--results results/blend_16`
#                                   from `pipeline/` are both
#                                   <repo>/pipeline/results/blend_16. The DEFAULT (no
#                                   --results at all) is unchanged: `pipeline/results`.
#   START/END                    -> --start / --end
#   WEIGHTING_MODE               -> run-local dump COPY (blend_weighting_mode) + --strategy-params
#                                   (blend_weighting_mode / weighting_mode). UNSET BY DEFAULT:
#                                   see "Step 2" below.
#   ENABLE_ORTHOGONALIZATION     -> run-local dump COPY (blend_enable_orthogonalization) +
#                                   --strategy-params. UNSET BY DEFAULT.
#   EXTRA_PARAMS                 -> merged into --strategy-params (and into the override copy,
#                                   when one is materialised)
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
# Steps 1-2 (dump the strategies, then optionally bake the weighting mode /
# orthogonalization / extra params into a RUN-LOCAL COPY of those dumps) are
# kept in spirit: they produce artifacts other tooling reads, and they are data
# preparation, not the serial loop. Step 2 no longer writes the dumps themselves
# -- see the incident note right below. The same params are additionally passed
# through `--strategy-params`, which is how `run_backtest.py` itself merges them
# (the old `--strategy-params "$STRAT_PARAMS"` behavior).
#
# --- 2026-10-24 live-trading incident: Step 2 must never write the live dumps ---
# Step 2 used to stamp ONE mode into all six
# `research_strategy/results/strategy_dumps/*blend*.json` IN PLACE:
#     params['blend_weighting_mode'] = ${WEIGHTING_MODE:-rolling_ic_ir}  (+ 3 aliases)
# Those dumps are not scratch: `scripts/run_live_strategy.py`'s
# DEFAULT_STRATEGY_FILE *is* `chan_four_state_blend_strategy.json`, and nothing on
# that path re-reads `strategies_config.json`, so the last blend backtest's
# leftovers size live order tickets. `chan_four_state_blend` is pinned to
# `regime_ic_ir_hybrid` (strategies_config.json, AGENTS.md section 8 -- the
# documented exception), so a plain run silently swapped the DEPLOYED engine to
# `rolling_ic_ir`: blend_base.py:445 then returns before the regime_* branch at
# :465, making the bucket_renorm_max_delta clamp (:522-529) and the thrust
# hysteresis inert, while `check_live_portfolio_health.py:1144-1152` mislabelled
# the mode. The structural flaw is that ONE global mode cannot be correct for six
# blends when one of them legitimately pins a different one.
#
# Now:
#   * DEFAULT = NO INJECTION. No mode/ortho value is invented here; each dump keeps
#     its own configured mode (so cfsb backtests as `regime_ic_ir_hybrid`), and
#     `--strategy-params` is not passed at all unless something was requested.
#   * An EXPLICIT --weighting-mode / --enable-orthogonalization / --no-orthogonalization
#     (or a positional / exported env value) materialises an overridden COPY under
#     `${RESULTS}/_strategy_dumps_override/` via `scripts/blend_dump_overrides.py`,
#     and Step 3's `--strategies-glob` points AT THAT COPY. The live dumps directory
#     is opened read-only: no snapshot, no mutate, no restore -- there is nothing to
#     restore, so no `trap` is needed and a mid-run failure cannot strand it.
#   * A request that departs from `chan_four_state_blend`'s pinned
#     `regime_ic_ir_hybrid` does NOT abort (an ablation may explore), but the helper
#     prints a prominent warning naming the exception, the pinned value and the
#     hidden risk, and lists every strategy it overrode.
#   * Reproducing a pre-fix `rolling_ic_ir` sweep therefore needs the flag spelled
#     out: `--weighting-mode rolling_ic_ir`.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- Path resolution --------------------------------------------------------
# The repo root is derived from THIS file's location, never from `$PWD`: every
# default below is repo-relative, so the script must behave identically when
# invoked as `./backtest_blend_strategies.sh`, as
# `bash pipeline/scripts/backtest_blend_strategies.sh` from the repo root, or by
# absolute path from an unrelated cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
# The operator's cwd, captured BEFORE we cd into pipeline/: `--universe pool.txt`
# must mean "pool.txt next to me", not "pool.txt next to pipeline/".
INVOKE_DIR="$(pwd -P)"

cd "${REPO_ROOT}/pipeline"

# Default parameter configurations
#
# `P1.6`-class regression this replaces: the old default pointed at `50_stock.txt`
# sitting directly under `docs/universe/china/`; commit `89f8388` moved that pool
# into the Chinese-named subdirectory `50只A股高流动性优质股票精选/`, so the bare
# path stopped resolving.
# A bare invocation therefore died with a FileNotFoundError deep inside the
# batch worker pool instead of at argument-parse time.
#
# The replacement is the pool every blend walkforward report in this repo is
# actually run on -- 22 core-satellite names (17 high-dividend SOE cores + 5
# momentum satellites) -- and the same in-repo default that
# `run_blend_cross_matrix.py`, `run_live_strategy.py` and
# `check_live_portfolio_health.py` already resolve.
DEFAULT_UNIVERSE="${REPO_ROOT}/docs/universe/china/core_satellite_22_stocks.txt"
UNIVERSE="${DEFAULT_UNIVERSE}"
RESULTS="results"
# Whether the operator actually supplied a results location. The DEFAULT must keep
# landing in `pipeline/results` (unchanged by this fix), so it is resolved against
# the pipeline/ workdir directly and never put through the caller-cwd-first
# resolution order below -- otherwise a stray `results/` directory next to the
# caller would silently re-home every default run.
RESULTS_EXPLICIT=0
START="2022-01-01"
END="2026-01-01"
# --- NO INJECTION BY DEFAULT (2026-10-24 incident fix) ----------------------
# These two used to default to `rolling_ic_ir` / `true`, which is what made Step 2
# rewrite every live blend dump on every plain run. An EMPTY value now means "the
# caller did not ask", and the only way to get an override is to ask explicitly
# (flag, positional, or an exported env value -- see *_EXPLICIT below).
WEIGHTING_MODE="${WEIGHTING_MODE:-}"
ENABLE_ORTHOGONALIZATION="${ENABLE_ORTHOGONALIZATION:-}"
# Explicit-request tracking. This is deliberately separate from "the variable has a
# value": the mode has to reach `--strategy-params` and the override copy ONLY when
# someone asked for it, and an explicit `--enable-orthogonalization false` must stay
# distinguishable from "nobody said anything" (both would otherwise read as unset).
WEIGHTING_MODE_EXPLICIT=0
ORTHO_EXPLICIT=0
if [[ -n "${WEIGHTING_MODE}" ]]; then WEIGHTING_MODE_EXPLICIT=1; fi
if [[ -n "${ENABLE_ORTHOGONALIZATION}" ]]; then ORTHO_EXPLICIT=1; fi
EXTRA_PARAMS="${EXTRA_PARAMS:-"{}"}"
# Directory Step 1 dumps into and Step 2/3 read. Defaults to the historical
# pipeline/-relative location; overridable so tests can point the whole flow at a
# throwaway directory and never touch the repo's (gitignored but LIVE for
# `run_live_strategy.py`) dumps.
STRATEGY_DUMPS_DIR="${STRATEGY_DUMPS_DIR:-}"
# Name of the run-local override copy, created inside ${RESULTS} (absolute by the
# time Step 2 runs). Kept in sync with blend_dump_overrides.OVERRIDE_DIR_NAME.
OVERRIDE_DIRNAME="_strategy_dumps_override"
BLEND_GLOB="*blend*.json"
# New (optional) knobs; defaults reproduce the old script's behavior.
DATA_PROVIDER="${DATA_PROVIDER:-"fuyao"}"
BATCH_N_JOBS="${BATCH_N_JOBS:-""}"          # empty = auto: min(cpu_count, n_strategies)
INNER_N_JOBS="${INNER_N_JOBS:-"1"}"         # fold workers inside a strategy; 1 = never nest pools
WINDOW_YEARS="${WINDOW_YEARS:-"0.25"}"
STEP_YEARS="${STEP_YEARS:-"0.25"}"
BASELINE_SYMBOL="${BASELINE_SYMBOL:-"000300.SH"}"   # empty string disables the benchmark

# `--help` is answered without touching the network, the DuckDB cache, the
# strategy-dump step, or a single backtest -- and it reports whether the built-in
# default pool actually resolves, which is the check that `P1.6` regressed.
usage() {
    local status="MISSING"
    if [[ -f "${DEFAULT_UNIVERSE}" ]]; then status="exists"; fi
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [UNIVERSE] [RESULTS] [START] [END] [WEIGHTING_MODE] \\
       [ENABLE_ORTHOGONALIZATION] [EXTRA_PARAMS]
       $(basename "${BASH_SOURCE[0]}") --universe X --results R --start S --end E \\
       [--weighting-mode M] [--enable-orthogonalization [true|false]] [--no-orthogonalization] \\
       [--extra-params '{"k": v}'] [--data-provider P] [--n-jobs N] [--help]

Thin wrapper around scripts/run_batch_backtest.py: dumps the strategies, then runs
ONE batch backtest over every dumped *blend*.json (see the header comment for the
full flag mapping and the measured speedups).

DEFAULT = NO INJECTION: each dumped strategy runs with its own configured
blend_weighting_mode / blend_enable_orthogonalization (so chan_four_state_blend
runs its pinned 'regime_ic_ir_hybrid'). The live dumps directory
(pipeline/research_strategy/results/strategy_dumps/) is READ ONLY for this script.
An explicit --weighting-mode / --enable-orthogonalization / --no-orthogonalization
writes an overridden COPY under <results>/_strategy_dumps_override/ and points the
batch at that copy; a request that departs from chan_four_state_blend's pinned
mode still runs, but prints a prominent warning (AGENTS.md section 8 exception).

Universe paths may be absolute, relative to the caller's cwd, or relative to the
repo root (e.g. --universe docs/universe/china/core_satellite_22_stocks.txt).
--results is resolved the same way and is always reported as an ABSOLUTE path in
the configuration banner, so a run can never land in an unexpected directory
silently: --results pipeline/results/blend_16 (from the repo root) and
--results results/blend_16 (from pipeline/) both mean <repo>/pipeline/results/blend_16.
Omitting --results keeps the historical default: <repo>/pipeline/results.
--help/-h is honoured in ANY argument position.
Set BLEND_BATCH_DRY_RUN=1 to resolve/validate the universe and stop before any work.

Default universe: ${DEFAULT_UNIVERSE}
                  (${status})
EOF
}

# `--help`/`-h` must win from ANY position, not only argv[1]. This pre-scan runs
# BEFORE the flag/positional mapping below: otherwise `--help` sitting in a
# positional slot (e.g. argv[2], or argv[4] after `--universe pool --start 2022-01-01`)
# is swallowed as a value and a real multi-hour batch starts. A flag whose value is
# the literal string `--help` is pathological enough to ignore.
for _arg in "$@"; do
    case "${_arg}" in
        -h|--help)
            usage
            exit 0
            ;;
    esac
done
unset _arg

# Parse named flags and/or positional arguments
positional_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --weighting-mode|--weighting_mode)
            WEIGHTING_MODE="$2"
            WEIGHTING_MODE_EXPLICIT=1
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
            ORTHO_EXPLICIT=1
            ;;
        # Explicit "off" must be expressible WITHOUT a value: `--enable-orthogonalization
        # false` works, but `--no-orthogonalization` is the spelling nobody gets wrong.
        --no-orthogonalization|--no_enable_orthogonalization|--disable-orthogonalization)
            ENABLE_ORTHOGONALIZATION="false"
            ORTHO_EXPLICIT=1
            shift 1
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
if [ ${#positional_args[@]} -ge 2 ]; then RESULTS="${positional_args[1]}"; RESULTS_EXPLICIT=1; fi
if [ ${#positional_args[@]} -ge 3 ]; then START="${positional_args[2]}"; fi
if [ ${#positional_args[@]} -ge 4 ]; then END="${positional_args[3]}"; fi
if [ ${#positional_args[@]} -ge 5 ]; then WEIGHTING_MODE="${positional_args[4]}"; WEIGHTING_MODE_EXPLICIT=1; fi
if [ ${#positional_args[@]} -ge 6 ]; then ENABLE_ORTHOGONALIZATION="${positional_args[5]}"; ORTHO_EXPLICIT=1; fi
if [ ${#positional_args[@]} -ge 7 ]; then EXTRA_PARAMS="${positional_args[6]}"; fi

# --- Universe resolution + fail-fast guard ----------------------------------
# Resolved BEFORE any work (before the strategy dump and before the batch): a
# bad pool must fail here with the offending path named, not inside a worker
# pool after 50 strategies have already been dumped.
#
# Accepted forms, in order: absolute; relative to the caller's cwd
# (`--universe pool.txt`); relative to the repo root (what every doc/README
# snippet writes -- `--universe docs/universe/...` -- even though we have since
# cd'd into pipeline/); relative to the pipeline/ workdir (the historical
# behaviour). Anything else is handed back verbatim so the guard below can name
# exactly what the operator typed.
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

# --- Output-path resolution (--results) -------------------------------------
# Same accepted forms and the same base order as `resolve_universe_path` above --
# absolute; relative to the caller's cwd; relative to the repo root; relative to
# the pipeline/ workdir (historical) -- but with one unavoidable difference: a
# results directory usually does NOT exist yet, so `[[ -f ]]` is not available as
# the discriminator. Instead the candidate's FIRST path component is used as the
# anchor: if `<base>/<first component>` exists, the candidate is interpreted
# relative to `<base>`. That is enough to disambiguate every documented spelling:
#
#   from <repo>       --results pipeline/results/blend_16
#                     -> `<repo>/pipeline` exists, anchor at INVOKE_DIR
#                     -> <repo>/pipeline/results/blend_16   (NOT pipeline/pipeline/...)
#   from <repo>/pipeline --results results/blend_16
#                     -> `<repo>/pipeline/results` exists (matrix runner's cwd)
#                     -> <repo>/pipeline/results/blend_16
#   from <repo>/pipeline --results pipeline/results/blend_16   (doc snippet pasted
#                     after a `cd pipeline/`) -> `<repo>/pipeline/pipeline` does NOT
#                     exist, falls through to the repo root -> the same right place.
#
# The concrete bug this fixes (silent wrong output): both wrappers `cd` into
# pipeline/ BEFORE parsing, so the doc-style invocation printed verbatim in
# `docs/audit/blend_15_walkforward_deep_analysis.md` and
# `docs/audit/blend_16_walkforward_deep_analysis.md`
# (`--results pipeline/results/blend_16`, run from the repo root) used to land in
# `<repo>/pipeline/pipeline/results/blend_16` -- a nested directory created
# silently, with the run reporting success. The banner now prints the resolved
# ABSOLUTE results dir, so a wrong landing is visible instead of silent.
resolve_output_path() {
    local candidate="$1"
    # `./pipeline/results/x` must anchor like `pipeline/results/x`; trailing
    # slashes are banner noise (keep a bare `/`).
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
    # Nothing anywhere anchors the first component: keep the historical
    # pipeline/-relative meaning (what every pre-fix invocation effectively got).
    printf '%s\n' "${PWD}/${candidate}"
}

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
# orchestrator, after this wrapper has already dumped 50+ strategies; the shell
# check is deliberately the earlier, identical-wording fail-fast.
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

UNIVERSE="$(resolve_universe_path "${UNIVERSE}")"
if [[ ! -f "${UNIVERSE}" ]]; then
    echo "Error: universe file not found at '${UNIVERSE}'" >&2
    echo "       Pass an existing pool with --universe <path> (absolute, or relative to the" >&2
    echo "       caller's cwd / repo root). Built-in default: ${DEFAULT_UNIVERSE}" >&2
    exit 1
fi

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

# Resolve the output directory to an ABSOLUTE path before it is echoed or used.
# Done AFTER the universe guards so a bad pool still fails with the same message
# and exit code as before (path resolution order does not change the guard), and
# BEFORE the banner so the operator sees where the run will actually write.
if [[ "${RESULTS_EXPLICIT}" == "1" ]]; then
    RESULTS="$(resolve_output_path "${RESULTS}")"
else
    # Default: unchanged from every previous release -- pipeline/results, i.e. the
    # pipeline/ workdir we already cd'd into, whatever the caller's cwd was.
    RESULTS="${PWD}/results"
fi

# Dumps directory resolution (Step 1 writes it, Steps 2-3 read it). Only an
# explicit STRATEGY_DUMPS_DIR changes it; the default is the historical
# pipeline/-relative location, so every existing caller lands where it always did.
# The directory is deliberately NOT created or validated here: Step 1 creates it,
# and a `BLEND_BATCH_DRY_RUN=1` must leave the filesystem untouched even in a clean
# clone where it does not exist yet.
resolve_dumps_dir() {
    local candidate="$1"
    if [[ "${candidate}" = /* ]]; then
        printf '%s\n' "${candidate}"
        return 0
    fi
    local base
    for base in "${INVOKE_DIR}" "${REPO_ROOT}" "${PWD}"; do
        if [[ -d "${base}/${candidate}" ]]; then
            printf '%s\n' "${base}/${candidate}"
            return 0
        fi
    done
    printf '%s\n' "${PWD}/${candidate}"
}

if [[ -n "${STRATEGY_DUMPS_DIR}" ]]; then
    DUMPS_DIR="$(resolve_dumps_dir "${STRATEGY_DUMPS_DIR}")"
else
    DUMPS_DIR="${PWD}/research_strategy/results/strategy_dumps"
fi

# Normalize boolean for enable_orthogonalization. Empty = NOT REQUESTED, which is
# different from `false`: an unrequested ortho setting must not be written anywhere
# (see the 2026-10-24 incident note in the header).
ORTHO_BOOL=""
if [[ "${ORTHO_EXPLICIT}" == "1" ]]; then
    ORTHO_BOOL="false"
    if [[ "$ENABLE_ORTHOGONALIZATION" =~ ^(true|True|TRUE|1|yes|Yes|y|Y)$ ]]; then
        ORTHO_BOOL="true"
    fi
fi
OVERRIDE_REQUESTED=0
if [[ "${WEIGHTING_MODE_EXPLICIT}" == "1" || "${ORTHO_EXPLICIT}" == "1" ]]; then
    OVERRIDE_REQUESTED=1
fi

echo "=== Blend Strategies Backtest Configuration ==="
echo "Universe:                  ${UNIVERSE}"
# Absolute on purpose: the old banner echoed the raw argument, so the
# `pipeline/pipeline/results/...` mis-landing looked exactly like a success.
echo "Results Dir:               ${RESULTS}"
echo "Start Date:                ${START}"
echo "End Date:                  ${END}"
# These three lines are what an operator checks after a run that "looked fine", so
# they report the ABSENCE of an override explicitly instead of printing a default
# that is then silently injected.
if [[ "${OVERRIDE_REQUESTED}" == "1" ]]; then
    echo "Weighting Mode:            ${WEIGHTING_MODE:-(not overridden)}"
    echo "Enable Orthogonalization:  ${ORTHO_BOOL:-(not overridden)}"
else
    echo "Weighting Mode:            (none - each dump keeps its configured blend_weighting_mode)"
    echo "Enable Orthogonalization:  (none - each dump keeps its configured value)"
fi
echo "Strategy Dumps:            ${DUMPS_DIR}"
echo "Extra Params:              ${EXTRA_PARAMS}"
echo "Data Provider:             ${DATA_PROVIDER}"
echo "Batch Workers:             ${BATCH_N_JOBS:-"auto"}"
echo "Inner (fold) Workers:      ${INNER_N_JOBS}"
echo "=============================================="

# `BLEND_BATCH_DRY_RUN=1` stops here: the universe has been resolved and
# content-validated, nothing has been written, no strategy dump and no backtest
# ran. Used by `pipeline/tests/test_script_default_paths.py` to prove the default
# pool resolves from any cwd without touching the cache/network.
#
# Nothing in this wrapper creates the (resolved, absolute) results directory
# either: that happens inside `run_batch_backtest.py`, which the guard below never
# reaches. So a dry run leaves the filesystem untouched while still reporting the
# one location a real run would write to.
#
# The variable is deliberately NAMESPACED rather than a bare `DRY_RUN`: `DRY_RUN`
# is a common exported variable (CI images, docker envs, unrelated tools), so a
# stray `DRY_RUN=1` in the operator's environment would otherwise SILENTLY skip a
# real multi-hour batch run. `DRY_RUN` is not honoured here at all -- not even as
# an alias -- and the message below is unmistakable so a dry run can never be
# mistaken for a completed one.
if [[ "${BLEND_BATCH_DRY_RUN:-0}" == "1" ]]; then
    echo "DRY RUN (BLEND_BATCH_DRY_RUN=1): resolved ${UNIVERSE} (${UNIVERSE_SYMBOL_COUNT} valid symbols); stopping before the strategy dump."
    exit 0
fi

# Step 1: Dump baseline strategy definition files
#
# `--dump-strategies` is the ONLY process allowed to write the dumps, and it
# writes them straight out of `strategies_config.json` (so `chan_four_state_blend`
# carries its pinned `regime_ic_ir_hybrid`, and no unprefixed alias keys appear).
# Everything after this point treats that directory as READ ONLY.
uv run python research_strategy/run_research_strategy.py --dump-strategies

# Step 2: OPTIONAL run-local override copy of the blend dumps (default: nothing).
#
# The pre-fix Step 2 stamped one global mode into the live dumps in place, which
# is how a backtest run silently reconfigured the LIVE strategy file (see the
# 2026-10-24 incident note at the top of this file). It now materialises an
# overridden COPY instead, and Step 3's --strategies-glob points at that copy.
#
# Consequence worth stating: the copies live under ${RESULTS}, so they are part of
# the run's artifacts and `PROVENANCE.json` records their paths+hashes -- an
# ablation remains fully reproducible. The LIVE dumps directory is only ever read,
# so "restore after the run" does not exist as a concept and needs no `trap`.
BATCH_DUMPS_DIR="${DUMPS_DIR}"
if [[ "${OVERRIDE_REQUESTED}" == "1" ]]; then
    OVERRIDE_DIR="${RESULTS}/${OVERRIDE_DIRNAME}"
    OVERRIDE_ARGS=(
        "${SCRIPT_DIR}/blend_dump_overrides.py"
        --source-dir "${DUMPS_DIR}"
        --dest-dir "${OVERRIDE_DIR}"
        --pattern "${BLEND_GLOB}"
        --extra-params "${EXTRA_PARAMS}"
    )
    if [[ "${WEIGHTING_MODE_EXPLICIT}" == "1" ]]; then
        OVERRIDE_ARGS+=(--weighting-mode "${WEIGHTING_MODE}")
    fi
    if [[ "${ORTHO_EXPLICIT}" == "1" ]]; then
        OVERRIDE_ARGS+=(--enable-orthogonalization "${ORTHO_BOOL}")
    fi
    # Same invocation mechanism Step 2 always used (`uv run python <script> args`).
    # The helper reads strategies_config.json itself to learn each strategy's pinned
    # mode, prints the override summary + any pinned-exception warning, and exits
    # non-zero (before renaming anything into place) if it cannot build the copy.
    uv run python "${OVERRIDE_ARGS[@]}"
    BATCH_DUMPS_DIR="${OVERRIDE_DIR}"
else
    echo "No weighting-mode/orthogonalization override requested: Step 3 reads the LIVE dumps"
    echo "in place (${DUMPS_DIR}); nothing is copied and nothing is rewritten."
    echo "Pass --weighting-mode / --enable-orthogonalization (or --no-orthogonalization) to"
    echo "explore a mode: that writes a run-local COPY under ${RESULTS}/${OVERRIDE_DIRNAME}/."
fi

# --strategy-params: built ONLY from what was actually requested. An empty result
# means the flag is not passed at all, so the batch runs each dump exactly as
# configured -- which is the whole point of the fix (the pre-fix version always
# forced `rolling_ic_ir`, masking `chan_four_state_blend`'s pinned regime mode even
# inside the backtest).
STRAT_PARAMS=""
EXTRA_PARAMS_TRIMMED="$(printf '%s' "${EXTRA_PARAMS}" | tr -d '[:space:]')"
if [[ "${OVERRIDE_REQUESTED}" == "1" || "${EXTRA_PARAMS_TRIMMED}" != "{}" ]]; then
    STRAT_PARAMS=$(uv run python -c "
import json, sys
mode = sys.argv[1]
ortho_raw = sys.argv[2]
base = {}
# An empty value means 'not requested': emitting the key with an empty/default
# value is exactly the bug this replaces.
if mode:
    base['blend_weighting_mode'] = mode
    base['weighting_mode'] = mode
if ortho_raw:
    ortho = ortho_raw.lower() in ('true', '1', 'yes')
    base['blend_enable_orthogonalization'] = ortho
    base['enable_orthogonalization'] = ortho
extra_raw = sys.argv[3] if len(sys.argv) > 3 else '{}'
try:
    extra = json.loads(extra_raw) if extra_raw.strip() else {}
except Exception:
    extra = {}
base.update(extra)
print(json.dumps(base))
" "${WEIGHTING_MODE}" "${ORTHO_BOOL}" "${EXTRA_PARAMS}")
fi

# Step 3: ONE batch invocation for every dumped blend strategy (was: one
# `uv run python ../backtester/run_backtest.py` per file).
#
# `--strategies-glob` is the ONLY input the batch reads strategies from, so it is
# what decides live-vs-copy: the live dumps dir when nothing was overridden, the
# run-local copy dir otherwise. `--results-subdir stem` is unaffected (the copies
# keep the source filename stems), so per-strategy artifacts land in the same
# `results/<stem>/` directories either way.
BATCH_ARGS=(
    ../scripts/run_batch_backtest.py
    --strategies-glob "${BATCH_DUMPS_DIR}/${BLEND_GLOB}"
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
if [[ -n "${STRAT_PARAMS}" && "${STRAT_PARAMS}" != "{}" ]]; then
    BATCH_ARGS+=(--strategy-params "${STRAT_PARAMS}")
fi
if [ -n "${BASELINE_SYMBOL}" ]; then
    BATCH_ARGS+=(--baseline-symbol "${BASELINE_SYMBOL}")
fi
if [ -n "${BATCH_N_JOBS}" ]; then
    BATCH_ARGS+=(--n-jobs "${BATCH_N_JOBS}")
fi

uv run python "${BATCH_ARGS[@]}"

"""`P1.6`-class regression guard -- committed script defaults must RESOLVE.

Why this file exists
--------------------
`P1.6` promised "no path default that cannot resolve", and then introduced two of
them:

1. `pipeline/scripts/run_blend_cross_matrix.py`'s `--universe` default was pointed
   at `docs/universe/china/core_satellite_20_stocks.txt`, a path that only ever
   existed as a *proposal* in `docs/version.md` §16.15.8.
2. `pipeline/scripts/backtest_blend_strategies.sh` (and its older sibling
   `backtest_all_strategies.sh`) defaulted to
   `../docs/universe/china/50_stock.txt`, which stopped existing in commit
   `89f8388` when that pool moved into
   `docs/universe/china/50只A股高流动性优质股票精选/`.

Both failed the same way: the operator saw a clean CLI, and the lack of a file
only surfaced deep inside a batch worker pool (or, for the shell scripts, as a
`FileNotFoundError` from `run_backtest.py` *after* 50 strategies had already been
dumped). Static greps for labels cannot catch that; the only honest check is to
mechanically resolve the default and look at the filesystem.

What is asserted here
---------------------
1. Both committed shell scripts pass `bash -n`.
2. `BLEND_BATCH_DRY_RUN=1` resolves the default universe and stops BEFORE the
   strategy dump: exit 0, no `uv` invoked (a poisoned `uv` shim on PATH proves
   it), nothing written into the repo -- from the repo root, from the script's own
   directory, and from an unrelated cwd (`tmp_path`). The variable is namespaced on
   purpose: a bare `DRY_RUN` is common in CI/docker environments, and honouring it
   would silently turn a real batch run into a no-op. `DRY_RUN` must have NO effect
   (asserted below).
3. The resolved default is an in-repo file that parses to a plausible China
   A-share pool (reuses the orchestrator's own parser, so the shell default and
   the Python default are held to one standard).
4. `--help` exits 0, names that default, and reports it as existing.
5. A missing universe fails fast with `Error: universe file not found at '<path>'`
   + exit 1, with the operator's own string quoted back -- never a worker crash.
6. A universe file that EXISTS but holds no usable pool -- a comments/blanks-only
   file, or the shape a `core.symlinks=false` Windows checkout produces (the whole
   content is one relative path string) -- must fail fast too, with a message that
   names the path and the degraded-symlink cause. `[[ -f ]]` alone cannot catch it.
7. The doc-style override (`--universe docs/universe/china/...`, i.e. repo-root
   relative, written exactly as `docs/audit/*.md` writes it) still resolves even
   though the script `cd`s into `pipeline/` first.
8. No `*_UNIVERSE=` assignment in either script still names the deleted pool, and
   the `run_batch_backtest.py` copy-paste epilog points at an existing file.
9. No developer-machine path (`/home/stone/Work/...`) in shell scripts -- that check
   lives in `test_config_wiring.py::test_no_hardcoded_personal_paths_in_python_scripts`,
   which now scans `.sh` too.
10. `--results` resolves to the SAME place from both documented spellings -- the
    doc-style `--results pipeline/results/blend_16` run from the repo root (what
    `docs/audit/blend_15_walkforward_deep_analysis.md` /
    `blend_16_walkforward_deep_analysis.md` print) and the matrix-style
    `--results results/blend_16` that `run_blend_cross_matrix.py` passes with
    `cwd=pipeline/`. The pre-fix behaviour silently wrote the first into
    `pipeline/pipeline/results/blend_16`; that nested path must never appear, in
    the banner or on disk. The DEFAULT (no results argument) must still be
    `pipeline/results`.
11. `--help`/`-h` is honoured in ANY argv position (argv[2], argv[4]), in both
    wrappers -- before positional mapping, so it can never be swallowed as the
    RESULTS positional (in `backtest_all_strategies.sh` that used to start a real
    serial pass writing into a directory literally named `--help`).
12. An explicit results directory that does not exist yet is reported (and would be
    created) only under the resolved location: `BLEND_BATCH_DRY_RUN=1` stops before
    the results directory is created at all, and no `pipeline/pipeline/...` sibling
    ever appears.

Everything is offline and cheap: `bash -n`, argv resolution, file parsing. The
`BLEND_BATCH_DRY_RUN` path exits before `uv run`, so no venv, cache, or network is
touched, and no subprocess ever writes inside the repo.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "pipeline", "scripts")
_PIPELINE_DIR = os.path.join(_REPO_ROOT, "pipeline")
_BLEND_SH = os.path.join(_SCRIPTS_DIR, "backtest_blend_strategies.sh")
_ALL_SH = os.path.join(_SCRIPTS_DIR, "backtest_all_strategies.sh")
_BATCH_PY = os.path.join(_REPO_ROOT, "scripts", "run_batch_backtest.py")
_MATRIX_PY = os.path.join(_SCRIPTS_DIR, "run_blend_cross_matrix.py")

# The pool the shell scripts must default to: the 22-name core-satellite book every
# blend walkforward report in `docs/reports/china/` and `docs/audit/` was produced
# with, and the same in-repo default the Python/live entry points already resolve.
_EXPECTED_DEFAULT = os.path.join(
    _REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt"
)

# Deleted in commit 89f8388; a default naming it can never resolve.
_REMOVED_POOL = "docs/universe/china/50_stock.txt"

_DRY_RUN_TIMEOUT = 120  # generous: cold `bash` + coreutils only, no interpreter

# Deliberately namespaced (NOT a bare `DRY_RUN`): `DRY_RUN` is a common exported
# variable in CI images / docker envs / unrelated tooling, so honouring it here
# would silently no-op a real batch run. See test_legacy_dry_run_var_has_no_effect.
_DRY_RUN_ENV = "BLEND_BATCH_DRY_RUN"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the wrapper scripts"
)


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("_matrix_module_for_script_paths", _MATRIX_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def matrix_module():
    return _load_matrix_module()


@pytest.fixture
def uv_shim(tmp_path):
    """A poisoned `uv` on PATH: proves the wrapper stopped BEFORE any real work.

    Two jobs, both load-bearing:

    * `--help` and `BLEND_BATCH_DRY_RUN=1` must not reach the strategy-dump step.
      Asserting on stdout alone is fragile (`--help` legitimately says
      "strategy_dumps"), so the proof is behavioural: if `uv` had been invoked, it
      would leave a marker.
    * Defence in depth for the repo. If a future edit moves the guard below the dump
      step, this shim makes the runaway `uv run ... --dump-strategies` fail (exit 1,
      no interpreter spawned) instead of rewriting `strategy_dumps/*.json` from a
      unit test.
    """
    bin_dir = tmp_path / "uvshim"
    bin_dir.mkdir()
    marker = tmp_path / "uv_was_invoked"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    uv.chmod(0o755)
    return bin_dir, marker


def _run(args, cwd, extra_env=None, path_prefix=None):
    env = os.environ.copy()
    env.pop("UNIVERSE", None)  # a stray exported UNIVERSE must not leak into defaults
    # A stray exported dry-run switch must not leak in either: the whole point of
    # the namespacing test is that only BLEND_BATCH_DRY_RUN is honoured, and every
    # other test wants the *real* (guarded) code path.
    env.pop(_DRY_RUN_ENV, None)
    env.pop("DRY_RUN", None)
    env.update(extra_env or {})
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_DRY_RUN_TIMEOUT,
        check=False,
    )


def _dry_run(script, cwd, args=(), path_prefix=None):
    """Resolve the universe without touching the dump step or the backtester."""
    return _run(
        ["bash", script, *args], cwd=cwd, extra_env={_DRY_RUN_ENV: "1"}, path_prefix=path_prefix
    )


def _resolved_universe(stdout: str) -> str:
    match = re.search(
        r"DRY RUN \(" + re.escape(_DRY_RUN_ENV) + r"=1\): resolved (.+?) \(\d+ valid symbols\);", stdout
    )
    assert match, f"the dry run did not report a resolved universe; stdout was:\n{stdout}"
    return match.group(1).strip()


def _resolved_results(stdout: str) -> str:
    """The ABSOLUTE results dir the banner reports (both wrappers print it)."""
    match = re.search(r"^Results Dir:\s+(\S+)\s*$", stdout, re.MULTILINE)
    assert match, f"the script did not report a resolved results dir; stdout was:\n{stdout}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# 1. Both wrappers are syntactically valid
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_shell_script_parses(script):
    assert os.path.isfile(script), f"script missing: {script!r}"
    result = _run(["bash", "-n", script], cwd=_REPO_ROOT)
    assert result.returncode == 0, f"bash -n failed for {script!r}:\n{result.stderr}"


# --------------------------------------------------------------------------- #
# 2-3. The default resolves from any cwd, and is a real China pool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
@pytest.mark.parametrize("cwd_kind", ["repo_root", "script_dir", "unrelated_tmp"])
def test_default_resolves_from_any_cwd(script, cwd_kind, tmp_path, uv_shim):
    """`BASH_SOURCE`-relative resolution: the default must not depend on `$PWD`.

    `unrelated_tmp` is a pytest tmp dir (outside the repo) on purpose -- it is the
    cwd a `bash /abs/path/script.sh` invocation actually has.
    """
    shim_dir, uv_marker = uv_shim
    cwd = {
        "repo_root": _REPO_ROOT,
        "script_dir": _SCRIPTS_DIR,
        "unrelated_tmp": str(tmp_path),
    }[cwd_kind]

    result = _dry_run(script, cwd, path_prefix=shim_dir)
    assert result.returncode == 0, (
        f"{_DRY_RUN_ENV}=1 from cwd={cwd!r} failed with exit {result.returncode}:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    resolved = _resolved_universe(result.stdout)
    assert os.path.isfile(resolved), f"resolved default does not exist: {resolved!r}"
    assert resolved == _EXPECTED_DEFAULT, (
        f"default universe changed to {resolved!r}; the blend family is documented against "
        f"{_EXPECTED_DEFAULT!r} (see docs/reports/china/chan_*_blend/). If the pool really "
        f"moved, update this guard and the prose in both shell scripts together."
    )
    # The DEFAULT results dir must keep landing under pipeline/results regardless of
    # the caller's cwd -- the `--results` resolution fix must not re-home it.
    assert _resolved_results(result.stdout) == os.path.join(_PIPELINE_DIR, "results")
    assert not uv_marker.exists(), (
        f"{_DRY_RUN_ENV}=1 leaked past the guard into the strategy-dump step (uv was invoked)"
    )


def test_default_pool_is_inside_repo_and_parses(matrix_module):
    symbols = matrix_module.load_universe_symbols(_EXPECTED_DEFAULT)
    assert len(symbols) >= 10, f"default pool parsed to only {len(symbols)} symbol(s)"
    assert len(symbols) == len(set(symbols)), "duplicate symbols in the default pool"
    assert os.path.commonpath([_REPO_ROOT, os.path.abspath(_EXPECTED_DEFAULT)]) == _REPO_ROOT
    assert "core_satellite" in os.path.basename(_EXPECTED_DEFAULT)


# --------------------------------------------------------------------------- #
# 4-6. `--help`, fail-fast guard, and the doc-style repo-root-relative override
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_help_reports_the_default_and_that_it_exists(script, tmp_path, uv_shim):
    shim_dir, uv_marker = uv_shim
    result = _run(["bash", script, "--help"], cwd=str(tmp_path), path_prefix=shim_dir)
    assert result.returncode == 0, f"--help exited {result.returncode}:\n{result.stderr}"
    assert _EXPECTED_DEFAULT in result.stdout, "help must name the built-in default pool"
    assert "(exists)" in result.stdout, "help must report the default as existing"
    assert not uv_marker.exists(), "--help must not run anything"


def test_missing_universe_fails_fast_with_the_path_quoted(tmp_path, uv_shim):
    shim_dir, uv_marker = uv_shim
    missing = "definitely_not_a_pool.txt"
    result = _dry_run(_BLEND_SH, str(tmp_path), ["--universe", missing], path_prefix=shim_dir)

    assert result.returncode == 1, (
        f"a missing universe must exit 1 before any work, got {result.returncode}\n"
        f"stdout:\n{result.stdout}"
    )
    assert f"Error: universe file not found at '{missing}'" in result.stderr, (
        f"guard must quote the operator's own path; stderr was:\n{result.stderr}"
    )
    assert not uv_marker.exists()


def test_all_strategy_script_missing_universe_fails_fast(tmp_path, uv_shim):
    """The older positional-argument wrapper needs the same guard."""
    shim_dir, uv_marker = uv_shim
    missing = "definitely_not_a_pool.txt"
    result = _dry_run(_ALL_SH, str(tmp_path), [missing], path_prefix=shim_dir)

    assert result.returncode == 1, (
        f"a missing universe must exit 1 before any work, got {result.returncode}\n"
        f"stdout:\n{result.stdout}"
    )
    assert f"Error: universe file not found at '{missing}'" in result.stderr
    assert not uv_marker.exists()


# --------------------------------------------------------------------------- #
# 6. `[[ -f ]]` is not enough: a file that EXISTS but is not a pool must fail too
# --------------------------------------------------------------------------- #
_DEGRADED_SYMLINK_HINT = "if this is a git symlink materialised as a text file, the checkout has core.symlinks=false"


def _universe_args(script, path):
    """`--universe PATH` for the flag-parsing wrapper, a bare positional for the old one."""
    return ["--universe", str(path)] if script == _BLEND_SH else [str(path)]


def _assert_not_a_pool(result, path, uv_marker):
    assert result.returncode == 1, (
        f"a file that exists but holds no pool must exit 1 before any work, got "
        f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert f"Error: universe file '{path}'" in result.stderr, (
        f"the content guard must name the offending path; stderr was:\n{result.stderr}"
    )
    assert _DEGRADED_SYMLINK_HINT in result.stderr.lower(), (
        f"the message must spell out the degraded-symlink cause; stderr was:\n{result.stderr}"
    )
    assert "core.symlinks=false" in result.stderr
    assert not uv_marker.exists(), "the content guard must fire before any uv invocation"


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_degraded_symlink_universe_fails_fast(script, tmp_path, uv_shim):
    """A `core.symlinks=false` checkout turns the pool symlink into a text file.

    Windows git (no Developer Mode) materialises a mode-`120000` entry as a small
    plain file whose whole content is the link target string. `[[ -f ]]` passes, so
    without a content guard the batch runs on a 1-token garbage pool. This fixture
    reproduces exactly that shape, using the real relative target of the default
    pool, and must be rejected before anything is dumped.
    """
    shim_dir, uv_marker = uv_shim
    link_target = os.readlink(_EXPECTED_DEFAULT)
    assert not os.path.isabs(link_target), (
        f"{_EXPECTED_DEFAULT!r} is no longer a relative symlink -- this fixture premise "
        f"(and the cross-platform safety it documents) needs re-checking"
    )
    degraded = tmp_path / "core_satellite_22_stocks.txt"
    degraded.write_text(link_target + "\n", encoding="utf-8")
    assert degraded.read_text(encoding="utf-8").strip() == link_target

    result = _dry_run(script, str(tmp_path), _universe_args(script, degraded), path_prefix=shim_dir)
    _assert_not_a_pool(result, str(degraded), uv_marker)


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_comments_only_universe_fails_fast(script, tmp_path, uv_shim):
    """Comments + blank lines parse to zero tickers: also not a pool."""
    shim_dir, uv_marker = uv_shim
    empty = tmp_path / "comments_only.txt"
    empty.write_text("# Core-Satellite Universe: 22 Stocks\n\n# core\n\n   # indented comment\n", encoding="utf-8")

    result = _dry_run(script, str(tmp_path), _universe_args(script, empty), path_prefix=shim_dir)
    _assert_not_a_pool(result, str(empty), uv_marker)


# --------------------------------------------------------------------------- #
# 6b. The dry-run switch is NAMESPACED: a bare `DRY_RUN` must never no-op a run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_legacy_dry_run_var_has_no_effect(script, uv_shim):
    """`DRY_RUN=1` (a common exported var in CI/docker) must NOT skip the batch.

    Behavioural proof: with `DRY_RUN=1` but no `BLEND_BATCH_DRY_RUN`, the script
    walks straight past the guard into real work -- the poisoned `uv` shim proves
    that (it leaves a marker and aborts the dumps, so nothing is ever written).
    """
    shim_dir, uv_marker = uv_shim
    result = _run(["bash", script], cwd=_REPO_ROOT, extra_env={"DRY_RUN": "1"}, path_prefix=shim_dir)

    assert "DRY RUN (" not in result.stdout, (
        f"a bare DRY_RUN=1 was honoured as a dry run -- it must not be an alias:\n{result.stdout}"
    )
    assert uv_marker.exists(), (
        "a bare DRY_RUN=1 stopped the script before real work; the namespaced "
        f"{_DRY_RUN_ENV} must be the only switch"
    )


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_dry_run_switch_is_namespaced_in_code(script):
    """Static backstop: no executable line may reference an un-namespaced `DRY_RUN`.

    Prose (comments) may name the generic variable to explain why it is ignored;
    executable code may only use `BLEND_BATCH_DRY_RUN`.
    """
    offenders = []
    for lineno, line in enumerate(open(script, encoding="utf-8").read().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "DRY_RUN" not in stripped:
            continue
        if re.search(r"(?<![A-Z_])DRY_RUN", stripped):
            offenders.append(f"{os.path.basename(script)}:{lineno}: {stripped}")
    assert not offenders, (
        f"un-namespaced DRY_RUN in executable code (use {_DRY_RUN_ENV}):\n  " + "\n  ".join(offenders)
    )


def test_doc_style_repo_root_relative_override_still_resolves(uv_shim):
    """`docs/audit/blend_15_*.md` invokes `--universe docs/universe/china/...`.

    The script `cd`s into `pipeline/` before resolving, so a naive pass-through would
    look for `pipeline/docs/...`. Repo-root-relative resolution has to keep working.
    """
    shim_dir, uv_marker = uv_shim
    doc_pool = "docs/universe/china/pruned_core_satellite_blend_16.txt"
    assert os.path.isfile(os.path.join(_REPO_ROOT, doc_pool)), (
        f"the pool the audit docs cite no longer exists: {doc_pool!r}"
    )
    result = _dry_run(_BLEND_SH, _REPO_ROOT, ["--universe", doc_pool], path_prefix=shim_dir)
    assert result.returncode == 0, f"override failed:\n{result.stderr}"
    assert _resolved_universe(result.stdout) == os.path.join(_REPO_ROOT, doc_pool)

    # Same pool as the wrapper's first positional argument.
    positional = _dry_run(_ALL_SH, _REPO_ROOT, [doc_pool], path_prefix=shim_dir)
    assert positional.returncode == 0, f"positional override failed:\n{positional.stderr}"
    assert _resolved_universe(positional.stdout) == os.path.join(_REPO_ROOT, doc_pool)
    assert not uv_marker.exists()


def test_absolute_override_works_from_unrelated_cwd(tmp_path):
    result = _dry_run(_BLEND_SH, str(tmp_path), ["--universe", _EXPECTED_DEFAULT])
    assert result.returncode == 0, f"absolute override failed:\n{result.stderr}"
    assert _resolved_universe(result.stdout) == _EXPECTED_DEFAULT


# --------------------------------------------------------------------------- #
# 7-8. No stale pool literals are left in defaults / copy-paste help text
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_no_universe_default_still_points_at_the_deleted_pool(script):
    """No `*UNIVERSE=` assignment may name the pool that commit 89f8388 moved away.

    Checking assignments (not the whole file) keeps the historical note in each
    script's header comment legal: the disclosure of *what* regressed is worth
    keeping, the dead default is not.
    """
    stale_markers = (os.path.basename(_REMOVED_POOL), "core_satellite_20")
    offenders = []
    for lineno, line in enumerate(open(script, encoding="utf-8").read().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if "UNIVERSE" not in name:
            continue
        if any(marker in value for marker in stale_markers):
            offenders.append(f"{os.path.basename(script)}:{lineno}: {stripped}")
    assert not offenders, (
        f"universe default assignment still names a pool that does not exist "
        f"(stale markers {stale_markers}):\n  " + "\n  ".join(offenders)
    )


def test_batch_runner_help_example_points_at_an_existing_universe():
    """A copy-pasted `--universe-file docs/universe/...` must actually work."""
    result = _run([sys.executable, _BATCH_PY, "--help"], cwd=_REPO_ROOT)
    assert result.returncode == 0, f"--help failed:\n{result.stderr}"

    referenced = re.findall(r"--universe-file\s+(\S+\.txt)", result.stdout)
    assert referenced, "expected a --universe-file example in the batch runner epilog"

    for rel in referenced:
        resolved = rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)
        assert os.path.isfile(resolved), (
            f"the help epilog advertises --universe-file {rel!r}, which does not exist. "
            f"This is the P1.6 class: a copy-pasteable path that cannot resolve."
        )


# --------------------------------------------------------------------------- #
# 10. `--results` must not be re-rooted under pipeline/ into pipeline/pipeline/
#
# Both wrappers `cd` into pipeline/ BEFORE parsing, so a raw pass-through turned
# the doc-style `--results pipeline/results/blend_16` (printed verbatim in
# docs/audit/blend_15_walkforward_deep_analysis.md and blend_16_...) into
# `pipeline/pipeline/results/blend_16`: a nested directory created silently while
# the run reported success. These tests pin the resolved banner path for BOTH
# documented spellings -- and the matrix runner's spelling, which passes
# `--results results/<folder>/<round>` with `cwd=pipeline/`
# (run_blend_cross_matrix.py) and must keep landing in pipeline/results/....
# --------------------------------------------------------------------------- #
def _results_args(script, value):
    """Both wrappers accept the documented `--results` flag form."""
    return ["--results", value]


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_doc_style_results_dir_is_not_double_nested(script, uv_shim):
    """`--results pipeline/results/blend_16` from the repo root."""
    shim_dir, uv_marker = uv_shim
    expected = os.path.join(_PIPELINE_DIR, "results", "blend_16")

    result = _dry_run(
        script, _REPO_ROOT, _results_args(script, "pipeline/results/blend_16"), path_prefix=shim_dir
    )
    assert result.returncode == 0, f"doc-style invocation failed:\n{result.stderr}"
    assert _resolved_results(result.stdout) == expected, (
        f"doc-style --results must resolve to {expected!r}; the audit docs print exactly "
        f"`--results pipeline/results/blend_16` run from the repo root.\nstdout:\n{result.stdout}"
    )
    assert "pipeline/pipeline" not in result.stdout, (
        f"the banner still shows the nested wrong landing:\n{result.stdout}"
    )
    assert not os.path.exists(os.path.join(_PIPELINE_DIR, "pipeline")), (
        "pipeline/pipeline/ was created -- the doc-style results path is being re-rooted"
    )
    assert not uv_marker.exists()


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
def test_matrix_style_results_dir_resolves_under_pipeline(script, uv_shim):
    """`--results results/blend_16` with cwd=pipeline/ (run_blend_cross_matrix.py)."""
    shim_dir, uv_marker = uv_shim
    expected = os.path.join(_PIPELINE_DIR, "results", "blend_16")

    result = _dry_run(
        script, _PIPELINE_DIR, _results_args(script, "results/blend_16"), path_prefix=shim_dir
    )
    assert result.returncode == 0, f"matrix-style invocation failed:\n{result.stderr}"
    assert _resolved_results(result.stdout) == expected, (
        f"the matrix runner passes `--results results/<folder>/<round>` with cwd=pipeline/ "
        f"and must land in {expected!r}.\nstdout:\n{result.stdout}"
    )
    assert not uv_marker.exists()


_PROBE_RESULTS_BASENAME = "__never_created_by_a_dry_run__"


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
@pytest.mark.parametrize(
    "cwd,requested",
    [
        (_REPO_ROOT, f"pipeline/results/{_PROBE_RESULTS_BASENAME}"),
        (_PIPELINE_DIR, f"results/{_PROBE_RESULTS_BASENAME}"),
    ],
    ids=["from_repo_root", "from_pipeline"],
)
def test_explicit_results_dir_is_reported_but_never_created(script, cwd, requested, uv_shim):
    """A results dir that does not exist yet is created ONLY at the resolved location.

    `BLEND_BATCH_DRY_RUN=1` stops before the strategy dump, and neither wrapper
    creates the results directory itself at all (that happens inside
    `run_backtest.py`), so the dry run must leave the filesystem untouched -- while
    still naming the one location a real run would write to. The nested
    `pipeline/pipeline/...` sibling must never be named or created.
    """
    shim_dir, uv_marker = uv_shim
    expected = os.path.join(_PIPELINE_DIR, "results", _PROBE_RESULTS_BASENAME)
    nested = os.path.join(_PIPELINE_DIR, "pipeline", "results", _PROBE_RESULTS_BASENAME)

    result = _dry_run(script, cwd, _results_args(script, requested), path_prefix=shim_dir)
    assert result.returncode == 0, f"dry run failed:\n{result.stderr}"
    assert _resolved_results(result.stdout) == expected, (
        f"requested {requested!r} from {cwd!r}; expected the resolved path {expected!r}.\n"
        f"stdout:\n{result.stdout}"
    )
    assert not os.path.exists(expected), (
        f"the dry run created the results directory {expected!r}; it must stop before any write"
    )
    assert not os.path.exists(nested), f"the wrongly nested {nested!r} was created"
    assert not uv_marker.exists()


# --------------------------------------------------------------------------- #
# 11. `--help`/`-h` must win from ANY argv position, never become a positional
#
# `backtest_all_strategies.sh` used to check `argv[1]` only, so a later `--help`
# was silently consumed as the RESULTS positional and a full serial pass started,
# writing into a directory literally named `--help`. Both wrappers now pre-scan
# the whole argument list before any positional mapping.
# --------------------------------------------------------------------------- #
def _help_argv(script, flag, position):
    """argv with `flag` at exactly `position` (1-based, script name excluded)."""
    pool = "docs/universe/china/core_satellite_22_stocks.txt"
    if script == _BLEND_SH:
        # argv[1] = --results (its value slot is exactly where a later --help used
        # to be swallowed), or --universe/--results/--help at positions 1-4.
        return ["--results", flag] if position == 2 else ["--universe", pool, "--results", flag]
    # The legacy-positional wrapper: a pool in argv[1], then the flag in argv[2]
    # (the RESULTS slot) or argv[4].
    return [pool, flag] if position == 2 else [pool, "results", "2022-01-01", flag]


@pytest.mark.parametrize("script", [_BLEND_SH, _ALL_SH], ids=["blend", "all"])
@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("position", [2, 4])
def test_help_flag_is_honoured_in_any_argument_position(script, flag, position, uv_shim):
    shim_dir, uv_marker = uv_shim
    args = _help_argv(script, flag, position)
    assert args[position - 1] == flag, f"test bug: {flag!r} is not argv[{position}] of {args!r}"

    result = _run(["bash", script, *args], cwd=_REPO_ROOT, extra_env={_DRY_RUN_ENV: "1"}, path_prefix=shim_dir)
    assert result.returncode == 0, (
        f"{flag} at argv[{position}] of {os.path.basename(script)} exited "
        f"{result.returncode} instead of printing usage:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Usage:" in result.stdout, f"{flag} at argv[{position}] did not print usage:\n{result.stdout}"
    assert _EXPECTED_DEFAULT in result.stdout
    # The pre-fix failure mode: `--help` used as a directory name.
    assert not os.path.exists(os.path.join(_PIPELINE_DIR, "--help"))
    assert not os.path.exists(os.path.join(_PIPELINE_DIR, "-h"))
    assert not uv_marker.exists(), f"{flag} at argv[{position}] ran real work (uv was invoked)"


"""`backtest_blend_strategies.sh` may never rewrite the LIVE blend strategy dumps.

Why this file exists (2026-10-24 live-trading incident)
------------------------------------------------------
The wrapper's Step 2 used to run, unconditionally and IN PLACE:

    params['blend_weighting_mode'] = ${WEIGHTING_MODE:-rolling_ic_ir}   # + 3 aliases

over `pipeline/research_strategy/results/strategy_dumps/*blend*.json`. Those dumps
are the LIVE strategy definitions: `scripts/run_live_strategy.py`'s
`DEFAULT_STRATEGY_FILE` is `chan_four_state_blend_strategy.json` and nothing on
that path re-reads `strategies_config.json`. `chan_four_state_blend` is pinned to
`blend_weighting_mode="regime_ic_ir_hybrid"` (documented exception, AGENTS.md
section 8), so a plain wrapper run silently re-pointed the DEPLOYED engine at
`rolling_ic_ir`: `blend_base.py:445` returns before the `regime_*` branch at
`:465`, and the `bucket_renorm_max_delta` clamp (`:522-529`) plus the thrust
hysteresis go inert.

The replacement design (what is asserted here)
----------------------------------------------
`pipeline/scripts/blend_dump_overrides.py` materialises an overridden COPY of the
blend dumps in a run-local directory and the batch's `--strategies-glob` is
pointed at that copy. The live dumps directory is opened READ ONLY -- there is no
snapshot/mutate/restore window and therefore no `trap` to get wrong.

Assertions:
1.  `mode=None` -> the destination is BYTE-IDENTICAL to the source (copy2, mtime
    included) and no key is added.
2.  `mode="rolling_ic_ir"` -> every copied blend dump carries
    `blend_weighting_mode` in `params` AND in
    `research_strategy_spec.entry_data.parameters`, while every SOURCE file's
    sha256 and mtime are unchanged (the live dumps are read-only by construction).
3.  A request that departs from a strategy's pinned mode is REPORTED separately
    (`report.pinned_deviations`) yet still applied (an ablation may explore);
    a request equal to the pin is not flagged. The `chan_four_state_blend`
    warning text names the exception, the pinned value and the inert-overlay risk.
4.  Atomicity: a failure mid-write leaves neither a partial destination nor a
    damaged previous one -- staging directory + rename.
5.  Staging guard rails: `dest == source` / `dest` inside `source` are refused
    outright, so no future caller can reintroduce the in-place write through the
    helper.
6.  Shell level, with a poisoned `uv` that (a) no-ops `--dump-strategies` and
    (b) RECORDS the batch argv instead of running it: the live dumps directory
    hash is identical before/after a full run, on the default path AND on the
    `--weighting-mode` path -- and the batch argv points at the copy only in the
    latter. The same probe runs against the repo's real dumps when they exist.
7.  Static: the wrapper no longer contains an inline dump write; the only writer
    it may call is the helper.

Everything is offline (`tmp_path` fixtures, no market data, no network) and the
batch is never executed -- the shim records its argv and exits 0.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_HELPER_PATH = os.path.join(_REPO_ROOT, "pipeline", "scripts", "blend_dump_overrides.py")
_BLEND_SH = os.path.join(_REPO_ROOT, "pipeline", "scripts", "backtest_blend_strategies.sh")
_LIVE_DUMPS_DIR = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps"
)
_EXPECTED_DEFAULT_UNIVERSE = os.path.join(
    _REPO_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt"
)

_CFSB = "chan_four_state_blend"
_CFSB_PINNED = "regime_ic_ir_hybrid"
_ALIAS_KEYS = ("weighting_mode", "enable_orthogonalization")
_DRY_RUN_TIMEOUT = 120


def _load_helper():
    spec = importlib.util.spec_from_file_location("_blend_dump_overrides_under_test", _HELPER_PATH)
    assert spec is not None and spec.loader is not None, f"cannot import {_HELPER_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper():
    return _load_helper()


# --------------------------------------------------------------------------- #
# Fixtures: a synthetic dumps directory shaped exactly like a real one
# --------------------------------------------------------------------------- #
def _dump_doc(key: str, mode: str, ortho: bool = True) -> dict:
    """Minimal but structurally faithful dump: `params` mirrored in the spec."""
    params = {
        "blend_weighting_mode": mode,
        "blend_enable_orthogonalization": ortho,
        "blend_adaptive_inertia": True,
        "cash_proxy": "511880.SH",
    }
    return {
        "template_name": key,
        "strategy_version": "1.0.0",
        "base_version": "1.0.0",
        "version_info": {"version": "1.0.0", "base_version": "1.0.0", "template_name": key},
        "params": dict(params),
        "explanation": "synthetic",
        "research_strategy_spec": {
            "strategy_key": key,
            "entry_data": {"name": key, "parameters": dict(params)},
        },
    }


@pytest.fixture
def source_dir(tmp_path):
    dumps = tmp_path / "strategy_dumps"
    dumps.mkdir()
    for key, mode in ((_CFSB, _CFSB_PINNED), ("generic_blend", "rolling_ic_ir")):
        (dumps / f"{key}_strategy.json").write_text(
            json.dumps(_dump_doc(key, mode), indent=2), encoding="utf-8"
        )
    # A non-blend dump: the pattern must leave it alone (the batch only ever reads
    # `*blend*.json`, and copying unrelated files would widen the blast radius).
    (dumps / "turtle_breakout_strategy.json").write_text(
        json.dumps(_dump_doc("turtle_breakout", "rolling_ic_ir"), indent=2), encoding="utf-8"
    )
    return dumps


def _dir_fingerprint(directory) -> dict:
    """`{filename: (sha256, mtime_ns)}` for every entry -- the "nothing changed" proof."""
    out = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(str(directory), name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            out[name] = (hashlib.sha256(handle.read()).hexdigest(), os.stat(path).st_mtime_ns)
    return out


def _aggregate_hash(directory) -> str:
    """One sha256 over the whole directory's (name, bytes) pairs."""
    digest = hashlib.sha256()
    for name, (file_hash, _mtime) in sorted(_dir_fingerprint(directory).items()):
        digest.update(name.encode())
        digest.update(file_hash.encode())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 1. No request -> byte-identical copies, no key added
# --------------------------------------------------------------------------- #
def test_no_override_copies_are_byte_identical(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    before = _dir_fingerprint(source_dir)

    report = helper.materialise_dump_overrides(str(source_dir), str(dest))

    assert report.overridden == ()
    assert set(report.unchanged) == {_CFSB, "generic_blend"}
    assert set(report.written) == {_CFSB, "generic_blend"}, (
        "only *blend*.json may be copied -- the non-blend dump must be left alone"
    )
    assert report.pinned_deviations == ()

    copied = sorted(os.listdir(dest))
    assert copied == [f"{_CFSB}_strategy.json", "generic_blend_strategy.json"]
    for name in copied:
        with open(source_dir / name, "rb") as handle:
            src_bytes = handle.read()
        with open(dest / name, "rb") as handle:
            dst_bytes = handle.read()
        assert src_bytes == dst_bytes, f"{name} is not byte-identical after a no-op copy"
        assert (dest / name).stat().st_mtime_ns == (source_dir / name).stat().st_mtime_ns, (
            "copy2 must preserve mtime -- a timestamp change would make a 'no injection' run "
            "look like an injection in any mtime-based audit"
        )

    params = json.loads((dest / f"{_CFSB}_strategy.json").read_text(encoding="utf-8"))["params"]
    assert params["blend_weighting_mode"] == _CFSB_PINNED
    assert _dir_fingerprint(source_dir) == before


# --------------------------------------------------------------------------- #
# 2. A requested mode reaches the COPIES only; the source is untouched
# --------------------------------------------------------------------------- #
def test_override_stamps_both_containers_and_never_writes_source(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    before = _dir_fingerprint(source_dir)

    report = helper.materialise_dump_overrides(
        str(source_dir), str(dest), weighting_mode="rolling_ic_ir"
    )

    assert set(report.overridden) == {_CFSB, "generic_blend"}
    assert report.unchanged == ()

    for key in (_CFSB, "generic_blend"):
        doc = json.loads((dest / f"{key}_strategy.json").read_text(encoding="utf-8"))
        assert doc["params"]["blend_weighting_mode"] == "rolling_ic_ir"
        mirror = doc["research_strategy_spec"]["entry_data"]["parameters"]
        assert mirror == doc["params"], (
            "the spec mirror must stay in lockstep with params (a clean dump keeps them equal; "
            "every consumer may read either container)"
        )
        # The 2026-10-24 aliases must NOT come back: `resolve_param` precedence 3 is
        # `blend_<name>`, so the bare spelling is a second source of truth that can only lie.
        for alias in _ALIAS_KEYS:
            assert alias not in doc["params"], f"unprefixed alias {alias!r} reintroduced"
            assert alias not in mirror, f"unprefixed alias {alias!r} reintroduced in the mirror"

    # THE invariant: the live dumps are read-only by construction.
    assert _dir_fingerprint(source_dir) == before, (
        "materialise_dump_overrides modified the SOURCE dumps -- that is the 2026-10-24 incident"
    )


# --------------------------------------------------------------------------- #
# 3. Pinned exceptions are reported, not silently averaged away
# --------------------------------------------------------------------------- #
def test_pinned_deviation_is_reported_and_still_applied(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    report = helper.materialise_dump_overrides(
        str(source_dir),
        str(dest),
        weighting_mode="rolling_ic_ir",
        pinned_modes={_CFSB: _CFSB_PINNED, "generic_blend": "rolling_ic_ir"},
    )

    assert report.has_pinned_deviation
    assert [d.strategy for d in report.pinned_deviations] == [_CFSB], (
        "generic_blend's pin equals the request, so only the cfsb exception may be flagged"
    )
    deviation = report.pinned_deviations[0]
    assert deviation.pinned_mode == _CFSB_PINNED
    assert deviation.requested_mode == "rolling_ic_ir"
    assert deviation.is_documented_exception
    # ... and the structure also says it WAS applied (not skipped): an ablation may
    # legitimately explore another mode, it just may not do so silently.
    assert _CFSB in report.overridden

    text = helper.format_report(report)
    assert "PINNED WEIGHTING-MODE EXCEPTION OVERRIDDEN" in text
    assert _CFSB in text and _CFSB_PINNED in text and "rolling_ic_ir" in text
    assert "bucket_renorm_max_delta" in text and "hysteresis" in text, (
        "the warning must name the risk that actually materialised, not just the value change"
    )
    assert "NOT modified" in text


def test_pinned_equal_request_is_not_flagged(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    report = helper.materialise_dump_overrides(
        str(source_dir),
        str(dest),
        weighting_mode=_CFSB_PINNED,
        pinned_modes={_CFSB: _CFSB_PINNED},
    )
    assert report.pinned_deviations == ()
    assert _CFSB in report.overridden
    assert "EXCEPTION OVERRIDDEN" not in helper.format_report(report)


def test_ortho_only_override_keeps_the_configured_weighting_mode(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    report = helper.materialise_dump_overrides(
        str(source_dir),
        str(dest),
        enable_orthogonalization=False,
        pinned_modes={_CFSB: _CFSB_PINNED},
    )
    assert report.pinned_deviations == (), "an ortho-only request does not touch the mode"
    doc = json.loads((dest / f"{_CFSB}_strategy.json").read_text(encoding="utf-8"))
    assert doc["params"]["blend_enable_orthogonalization"] is False
    assert doc["params"]["blend_weighting_mode"] == _CFSB_PINNED


# --------------------------------------------------------------------------- #
# 4. Atomicity + guard rails
# --------------------------------------------------------------------------- #
def test_failed_write_leaves_no_partial_destination(helper, source_dir, tmp_path):
    dest = tmp_path / "override"
    dest.mkdir()
    (dest / "previous.txt").write_text("previous run", encoding="utf-8")
    (source_dir / "broken_blend_strategy.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        helper.materialise_dump_overrides(str(source_dir), str(dest), weighting_mode="regime_adaptive")

    assert sorted(os.listdir(dest)) == ["previous.txt"], (
        "a mid-write failure damaged the previous override directory (and would have left a "
        "half-written blend family for the batch to consume)"
    )
    assert "previous.txt" in os.listdir(dest)
    staging = [n for n in os.listdir(tmp_path) if ".staging." in n]
    assert staging == [], f"staging directory leaked: {staging}"


def test_dest_equal_to_or_inside_source_is_refused(helper, source_dir):
    with pytest.raises(ValueError, match="must differ from source_dir"):
        helper.materialise_dump_overrides(str(source_dir), str(source_dir))
    with pytest.raises(ValueError, match="inside source_dir"):
        helper.materialise_dump_overrides(str(source_dir), os.path.join(str(source_dir), "copy"))


def test_empty_source_dir_is_refused(helper, tmp_path):
    """Pointing the batch at an empty override dir must fail HERE, not as an empty glob."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no strategy dump matched"):
        helper.materialise_dump_overrides(str(empty), str(tmp_path / "override"))


# --------------------------------------------------------------------------- #
# 5. Pinned modes come from the real config (single source of truth)
# --------------------------------------------------------------------------- #
def test_load_pinned_modes_reads_the_repo_config(helper):
    pinned = helper.load_pinned_modes()
    assert pinned.get(_CFSB) == _CFSB_PINNED, (
        f"{_CFSB} must resolve to its documented pin {_CFSB_PINNED!r}; the override warning is "
        "silent without it"
    )
    assert pinned.get("generic_blend") == "rolling_ic_ir"
    # A missing config must degrade to "no pins", never crash the run.
    assert helper.load_pinned_modes(str(_HELPER_PATH)) == {}


# --------------------------------------------------------------------------- #
# 6. Static: the wrapper has no inline dump write left
# --------------------------------------------------------------------------- #
def test_wrapper_has_no_inline_dump_stamp():
    text = open(_BLEND_SH, encoding="utf-8").read()
    # Comments may (and must) quote the pre-fix statement: that disclosure is load
    # bearing history. Only EXECUTABLE lines are constrained.
    code_lines = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # The exact pre-fix statements, both containers. (The `-c` STRAT_PARAMS builder
    # legitimately writes `base[...]`, the CLI override dict; that is not a dump.)
    for offender in (
        "params['blend_weighting_mode']",
        "ep['blend_weighting_mode']",
        "params['weighting_mode']",
        "ep['weighting_mode']",
        "params.update(extra_params)",
    ):
        assert offender not in code, (
            f"{offender!r} is back in the wrapper's executable code -- the global in-place stamp "
            "that reconfigured the LIVE dumps on 2026-10-24"
        )

    # No in-place JSON writer at all: the wrapper must not open a dump for writing.
    assert not any("json.dump(" in line for line in code_lines), (
        "the wrapper must not serialise JSON itself; the override copy is "
        "pipeline/scripts/blend_dump_overrides.py's job"
    )

    # The one permitted writer must be wired, and the batch must read from the
    # directory Step 2 decided on (live OR copy) -- not a hardcoded live path.
    assert "blend_dump_overrides.py" in code
    assert '--strategies-glob "${BATCH_DUMPS_DIR}/${BLEND_GLOB}"' in code, (
        "the batch glob must follow BATCH_DUMPS_DIR, otherwise an override copy is never consumed"
    )


# --------------------------------------------------------------------------- #
# 7. Shell level: a full run leaves the dumps byte-identical
# --------------------------------------------------------------------------- #
@pytest.fixture
def uv_shim(tmp_path):
    """A poisoned `uv` that never does real work and RECORDS its argv.

    * `run python research_strategy/run_research_strategy.py --dump-strategies`
      -> exit 0 without touching anything (a real dump would overwrite the very
      files this test is about).
    * `run python ../scripts/run_batch_backtest.py ...` -> exit 0, argv recorded
      (never a real backtest: it needs a data provider and minutes of CPU).
    * everything else (the override helper, `-c` JSON builders) -> real python,
      because those are the units under test and they are pure local JSON.

    Each invocation is recorded as one line, args joined by `|`.
    """
    bin_dir = tmp_path / "_uvshim"
    bin_dir.mkdir()
    record = tmp_path / "uv_record.txt"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "INVOKE %s\\n" "$(printf \'%s|\' "$@")" >> "{record}"\n'
        'shift  # run\n'
        'shift  # python\n'
        'case "$1" in\n'
        "  *run_research_strategy.py) exit 0 ;;\n"
        "  *run_batch_backtest.py) exit 0 ;;\n"
        f'  *) exec "{sys.executable}" "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir


def _run_wrapper(shim_bin, workdir, dumps_dir, extra_args=()):
    """Run the wrapper with the poisoned `uv`; returns (CompletedProcess, invocations, results)."""
    record = os.path.join(os.path.dirname(str(shim_bin)), "uv_record.txt")
    env = os.environ.copy()
    for leaked in ("WEIGHTING_MODE", "ENABLE_ORTHOGONALIZATION", "EXTRA_PARAMS", "BLEND_BATCH_DRY_RUN"):
        env.pop(leaked, None)
    env["STRATEGY_DUMPS_DIR"] = str(dumps_dir)
    env["PATH"] = f"{shim_bin}{os.pathsep}{env.get('PATH', '')}"
    results = workdir / "results"
    argv = [
        "bash", _BLEND_SH,
        "--universe", _EXPECTED_DEFAULT_UNIVERSE,
        "--results", str(results),
        *extra_args,
    ]
    proc = subprocess.run(
        argv, cwd=_REPO_ROOT, env=env, capture_output=True, text=True,
        timeout=_DRY_RUN_TIMEOUT, check=False,
    )
    invocations = []
    if os.path.isfile(record):
        for line in open(record, encoding="utf-8").read().splitlines():
            if line.startswith("INVOKE "):
                invocations.append([a for a in line[len("INVOKE "):].split("|") if a])
    return proc, invocations, results


def test_default_run_never_stamps_the_dumps_and_reads_them_in_place(source_dir, tmp_path, uv_shim):
    before = _aggregate_hash(source_dir)

    proc, invocations, results = _run_wrapper(uv_shim, tmp_path, source_dir)

    assert proc.returncode == 0, f"wrapper failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert _aggregate_hash(source_dir) == before, (
        "a DEFAULT run changed the live dumps -- this is exactly the 2026-10-24 regression"
    )
    assert "No weighting-mode/orthogonalization override requested" in proc.stdout

    batch = [args for args in invocations if any("run_batch_backtest.py" in a for a in args)]
    assert len(batch) == 1, f"expected exactly one batch invocation, got {invocations}"
    glob_arg = next(a for a in batch[0] if a.endswith("*blend*.json"))
    assert glob_arg == os.path.join(str(source_dir), "*blend*.json"), (
        f"the default path must read the dumps in place, got {glob_arg!r}"
    )
    assert "--strategy-params" not in batch[0], (
        "no mode/ortho/extra param was requested, so --strategy-params must not be passed at all "
        f"(it would re-introduce the global injection); argv was {batch[0]}"
    )
    assert not (results / "_strategy_dumps_override").exists(), (
        "a default run must not materialise an override directory"
    )


def test_override_run_never_stamps_the_dumps_and_points_the_batch_at_the_copy(
    source_dir, tmp_path, uv_shim
):
    before = _dir_fingerprint(source_dir)

    proc, invocations, results = _run_wrapper(
        uv_shim, tmp_path, source_dir, extra_args=("--weighting-mode", "rolling_ic_ir")
    )

    assert proc.returncode == 0, f"wrapper failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert _dir_fingerprint(source_dir) == before, (
        "the override path wrote the LIVE dumps instead of a copy"
    )

    helper_calls = [args for args in invocations if any("blend_dump_overrides.py" in a for a in args)]
    assert len(helper_calls) == 1, f"expected one helper invocation, got {invocations}"
    helper_argv = helper_calls[0]
    override_dir = results / "_strategy_dumps_override"
    assert "--source-dir" in helper_argv and str(source_dir) in helper_argv
    assert str(override_dir) in helper_argv

    batch = [args for args in invocations if any("run_batch_backtest.py" in a for a in args)]
    assert len(batch) == 1
    glob_arg = next(a for a in batch[0] if a.endswith("*blend*.json"))
    assert glob_arg == os.path.join(str(override_dir), "*blend*.json"), (
        f"the batch must consume the COPY, got {glob_arg!r}"
    )
    # The copy itself carries the override, and the pinned exception is called out.
    copied = json.loads((override_dir / f"{_CFSB}_strategy.json").read_text(encoding="utf-8"))
    assert copied["params"]["blend_weighting_mode"] == "rolling_ic_ir"
    assert "PINNED WEIGHTING-MODE EXCEPTION OVERRIDDEN" in proc.stdout
    assert f"Overridden (2): {_CFSB}, generic_blend" in proc.stdout, (
        f"the run must list which strategies it overrode; stdout was:\n{proc.stdout}"
    )
    # Nothing is executed for real: no backtest ran, only its argv was recorded.
    assert "batch_ranking.csv" not in proc.stdout


def test_repo_live_dumps_are_untouched_across_an_override_run(tmp_path, uv_shim):
    """Same probe, against the repo's REAL dumps directory (skipped when absent).

    The synthetic fixture proves the mechanism; this proves the actual files that
    `run_live_strategy.py` reads are unaffected -- `chan_four_state_blend_strategy.json`
    must still say `regime_ic_ir_hybrid` afterwards.
    """
    blend_dumps = sorted(
        os.path.join(_LIVE_DUMPS_DIR, n)
        for n in os.listdir(_LIVE_DUMPS_DIR)
        if n.endswith(".json") and "blend" in n
    ) if os.path.isdir(_LIVE_DUMPS_DIR) else []
    if not blend_dumps:
        pytest.skip(f"no blend dumps under {_LIVE_DUMPS_DIR} (clean clone); synthetic probe covers the mechanism")

    before = _aggregate_hash(_LIVE_DUMPS_DIR)

    proc, _invocations, _results = _run_wrapper(
        uv_shim, tmp_path, _LIVE_DUMPS_DIR, extra_args=("--weighting-mode", "rolling_ic_ir")
    )
    assert proc.returncode == 0, f"wrapper failed:\n{proc.stdout}\n{proc.stderr}"
    assert _aggregate_hash(_LIVE_DUMPS_DIR) == before, (
        "the repo's live strategy dumps changed during an override run"
    )

    cfsb = json.loads(
        open(os.path.join(_LIVE_DUMPS_DIR, f"{_CFSB}_strategy.json"), encoding="utf-8").read()
    )
    assert cfsb["params"]["blend_weighting_mode"] == _CFSB_PINNED, (
        "the LIVE chan_four_state_blend dump (run_live_strategy.py's DEFAULT_STRATEGY_FILE) no "
        "longer carries its pinned regime mode"
    )


# --------------------------------------------------------------------------- #
# 8. Task B6.3: De-fragilize hardcoded line numbers & verify structural AST references
# --------------------------------------------------------------------------- #
def test_no_brittle_line_numbers_in_docstring_or_warning_text(helper):
    """Docstring and warning formatter must reference structural symbols, not fragile line numbers."""
    # 1. Module docstring check
    doc = helper.__doc__ or ""
    assert not re.search(r"blend_base\.py:\d+", doc), "found brittle blend_base.py:<line> in docstring"
    assert not re.search(r":\d{3,}", doc), "found brittle :<line> number in docstring"
    assert not re.search(r":\d+-\d+", doc), "found brittle :<start>-<end> line range in docstring"

    # 2. Formatted warning text check
    deviation = helper.PinnedDeviation(
        strategy=_CFSB,
        pinned_mode=_CFSB_PINNED,
        requested_mode="rolling_ic_ir",
    )
    warning_text = helper.format_pinned_exception_warning(deviation, "/tmp/override")
    assert not re.search(r"blend_base\.py:\d+", warning_text), "found brittle blend_base.py:<line> in warning"
    assert not re.search(r":\d{3,}", warning_text), "found brittle :<line> number in warning"
    assert not re.search(r":\d+-\d+", warning_text), "found brittle :<start>-<end> line range in warning"


def test_warning_text_structural_symbols_exist_in_blend_base(helper):
    """Every class, method, branch name and parameter in the warning must exist in blend_base.py AST."""
    import ast

    blend_base_path = os.path.join(
        _REPO_ROOT, "pipeline", "research", "blend", "blend_base.py"
    )
    assert os.path.isfile(blend_base_path)

    with open(blend_base_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="blend_base.py")

    # Verify BaseBlendAllocationStrategy class exists
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "BaseBlendAllocationStrategy" in classes
    cls_node = classes["BaseBlendAllocationStrategy"]

    # Verify _compute_adaptive_weights method exists in class
    methods = {n.name: n for n in cls_node.body if isinstance(n, ast.FunctionDef)}
    assert "_compute_adaptive_weights" in methods
    fn_node = methods["_compute_adaptive_weights"]

    # Verify bucket_renorm_max_delta is a parameter/identifier in _compute_adaptive_weights
    fn_args = [a.arg for a in fn_node.args.args + fn_node.args.kwonlyargs]
    assert "bucket_renorm_max_delta" in fn_args

    # Verify weighting modes are string literals evaluated in _compute_adaptive_weights
    strings = {
        n.value for n in ast.walk(fn_node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    for mode in ("rolling_ic_ir", "regime_adaptive", "regime_ic_ir_hybrid"):
        assert mode in strings, f"{mode!r} not found in _compute_adaptive_weights string literals"

    # Verify the warning text references these exact symbols
    deviation = helper.PinnedDeviation(
        strategy=_CFSB,
        pinned_mode=_CFSB_PINNED,
        requested_mode="rolling_ic_ir",
    )
    warning_text = helper.format_pinned_exception_warning(deviation, "/tmp/override")
    assert "BaseBlendAllocationStrategy" in warning_text
    assert "_compute_adaptive_weights" in warning_text
    assert "rolling_ic_ir" in warning_text
    assert "regime_adaptive" in warning_text
    assert "regime_ic_ir_hybrid" in warning_text
    assert "bucket_renorm_max_delta" in warning_text
    assert "thrust" in warning_text and "hysteresis" in warning_text


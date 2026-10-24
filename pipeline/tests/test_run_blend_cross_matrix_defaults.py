"""`P1.6` regression guard -- `run_blend_cross_matrix.py` path defaults must exist.

Why this file exists
--------------------
P1.6 "removed a hardcoded personal path" from
`pipeline/scripts/run_blend_cross_matrix.py` by pointing `--universe` at
`docs/universe/china/core_satellite_20_stocks.txt` ... a file that was only ever a
*proposal* in `docs/version.md` §16.15.8 and was never committed. The fix that was
meant to repair the default therefore broke the default outright: a bare invocation
died with `FileNotFoundError`, and -- because the path is forwarded to
`backtest_blend_strategies.sh` -> `--universe-file` -> `FileUniverseProvider` -- the
failure could only ever be discovered deep inside a batch worker pool.

Static label greps cannot catch that. These tests are the mechanical version:

1. `--universe`'s default is a real file inside the repo (no path escaping the repo).
2. It parses to a non-trivial China symbol list of the shape the batch actually
   resolves (`<6 digits>.<SH|SZ>`); a comment-only or wrong-market file fails.
3. Every other path-like default/reference the module builds off `_REPO_ROOT` exists.
4. A missing / malformed universe raises an error that NAMES the file (never a bare
   `FileNotFoundError` from somewhere deep in the stack), and `main()` turns that into
   `exit(1)` before any subprocess is spawned.

100% offline and fast: file parsing plus argv patching, no backtest, no network.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MODULE_PATH = os.path.join(_REPO_ROOT, "pipeline", "scripts", "run_blend_cross_matrix.py")

# The batch resolves China A-share tickers; `FileUniverseProvider` tolerates bare
# strings, so a shape check here is the only thing that catches "pointed at a README".
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def _load_module():
    """Import the orchestrator by path (it lives under `pipeline/scripts`, not a package)."""
    spec = importlib.util.spec_from_file_location("_run_blend_cross_matrix_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def matrix_module():
    return _load_module()


# --------------------------------------------------------------------------- #
# 1-3. Defaults exist, stay inside the repo, and parse to a real China universe
# --------------------------------------------------------------------------- #
def test_module_file_is_the_one_we_think_it_is(matrix_module):
    assert os.path.abspath(matrix_module.__file__) == os.path.abspath(_MODULE_PATH)


def test_default_universe_exists_and_is_inside_repo(matrix_module):
    default = matrix_module._DEFAULT_UNIVERSE
    assert os.path.isfile(default), (
        f"_DEFAULT_UNIVERSE does not exist: {default!r}. This is the exact P1.6 "
        f"regression: a `--universe` default pointing at an uncommitted path makes a "
        f"bare `python pipeline/scripts/run_blend_cross_matrix.py` die."
    )
    # P1.6's whole point: no default may escape into a developer's home directory.
    repo_root = os.path.abspath(matrix_module._REPO_ROOT)
    assert os.path.commonpath([repo_root, os.path.abspath(default)]) == repo_root, (
        f"_DEFAULT_UNIVERSE escapes the repo ({default!r} not under {repo_root!r}); "
        f"P1.6 removed exactly this kind of personal absolute path."
    )


def test_default_universe_parses_to_a_real_china_core_satellite_pool(matrix_module):
    symbols = matrix_module.load_universe_symbols(matrix_module._DEFAULT_UNIVERSE)

    assert len(symbols) == len(set(symbols)), "duplicate symbols in the default universe"
    assert len(symbols) >= 10, (
        f"default universe parsed to {len(symbols)} symbol(s); a 6-cell x 2-round "
        f"cross-ablation matrix needs a real pool, not a stub"
    )

    malformed = [sym for sym in symbols if not _SYMBOL_RE.match(sym)]
    assert not malformed, (
        f"default universe holds symbols the China blend batch cannot resolve: "
        f"{malformed[:5]}"
    )

    # Semantic check, not just syntax: this is the core-satellite pool, so the file
    # name must say so. Guards against silently re-pointing at a random universe.
    assert "core_satellite" in os.path.basename(matrix_module._DEFAULT_UNIVERSE)


def test_path_like_defaults_all_exist(matrix_module):
    """Every path-like default the module builds off `_REPO_ROOT` must be real.

    `_DEFAULT_UNIVERSE` is the only module-level path constant today; the loop keeps
    the next one from regressing silently. `os.path.join(_REPO_ROOT, ...)` references
    are checked the same way.
    """
    checked = 0
    for name in dir(matrix_module):
        if not (name.endswith("_PATH") or "_DEFAULT_" in name or name.startswith("_DEFAULT_")):
            continue
        value = getattr(matrix_module, name)
        if isinstance(value, str) and os.path.isabs(value):
            assert os.path.exists(value), f"module default {name} does not exist: {value!r}"
            checked += 1
    assert checked >= 1, "no path-like module defaults were found -- has _DEFAULT_UNIVERSE been renamed?"

    # The batch entry point the orchestrator shells out to (built in `main()`, so not
    # a module attribute) must exist too, otherwise every cell fails at subprocess time.
    backtest_script = os.path.join(
        matrix_module._REPO_ROOT, "pipeline", "scripts", "backtest_blend_strategies.sh"
    )
    assert os.path.isfile(backtest_script), f"backtest script missing: {backtest_script!r}"


def test_argparse_universe_default_is_the_module_constant(matrix_module, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_blend_cross_matrix.py"])
    args = matrix_module.parse_args()
    assert args.universe == matrix_module._DEFAULT_UNIVERSE
    assert os.path.isfile(args.universe)


# --------------------------------------------------------------------------- #
# 4. Bad universes fail loudly, by name, before any subprocess
# --------------------------------------------------------------------------- #
def test_missing_universe_raises_informative_error(matrix_module, tmp_path):
    missing = tmp_path / "no_such_universe.txt"
    with pytest.raises(FileNotFoundError) as excinfo:
        matrix_module.load_universe_symbols(str(missing))

    message = str(excinfo.value)
    assert str(missing) in message, f"error must name the missing path, got: {message}"
    assert "--universe" in message, f"error must tell the operator how to fix it, got: {message}"


def test_comment_only_universe_is_rejected(matrix_module, tmp_path):
    pool = tmp_path / "comments_only.txt"
    pool.write_text("# all commentary, no symbols\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 2"):
        matrix_module.load_universe_symbols(str(pool))


def test_wrong_market_universe_is_rejected(matrix_module, tmp_path):
    pool = tmp_path / "us_pool.txt"
    pool.write_text("AAPL\nMSFT\nSPY\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        matrix_module.load_universe_symbols(str(pool))
    assert str(pool) in str(excinfo.value)
    assert "AAPL" in str(excinfo.value)


def test_main_exits_cleanly_on_missing_universe(matrix_module, monkeypatch, tmp_path, capsys):
    """`main()` must stop with exit(1) + a readable stderr line, never a subprocess."""
    missing = tmp_path / "absent.txt"
    monkeypatch.setattr(
        sys, "argv", ["run_blend_cross_matrix.py", "--universe", str(missing), "--dry-run"]
    )
    with pytest.raises(SystemExit) as excinfo:
        matrix_module.main()

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert str(missing) in stderr
    assert "Universe file not found" in stderr

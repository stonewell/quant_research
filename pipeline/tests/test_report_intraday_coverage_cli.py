"""Offline contract tests for the `scripts/report_intraday_coverage.py` CLI surface.

WHAT IS BEING PINNED, AND WHY IT NEEDS A TEST
---------------------------------------------
`M3.11` was closed on the **new caliber** ruling of 2026-11-08: the 5m table shrank to
`34,944 行 / 28 只 / 2026-09-16 起` and `511880.SH` now spans `2026-10-19 09:35 ~
2026-11-05 15:00`. The script's default target window was still the retired `M3.4` snapshot
window (`2026-01-05 ~ 2026-11-05`), so a bare run re-reported `coverage FAIL 6.4%` and
`exit 1` forever -- a correct answer about a window nobody claims any more, and exactly the
kind of stale default that silently outlives its ruling.

Three things are therefore pinned here, and nothing else:

  * the default window IS the new caliber (and the old window stays reachable as an override);
  * `--start` / `--end` overrides still win over the defaults, unchanged;
  * the exit-code contract -- `0` only when every verdict is `pass`, `1` when any is
    `fail`/`insufficient`, `2` reserved for usage / IO errors.

100% offline: the script is loaded through `importlib` (it lives in `scripts/`, not in an
importable package) and only `build_arg_parser()` / `exit_code_for_statuses()` are exercised.
**No DuckDB connection is opened, no `data/` path is stat'd, no network is touched** -- the
module-level `import duckdb` is a library import only, and `main()` (the sole place that
connects) is never called.
"""

import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common.intraday_coverage import (
    BARS_PER_SESSION_5M,
    STATUS_FAIL,
    STATUS_INSUFFICIENT,
    STATUS_PASS,
)


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "report_intraday_coverage_under_test",
    os.path.join("scripts", "report_intraday_coverage.py"),
)

#: The post-`M3.8` caliber (2026-11-08 ruling). Written as literals on purpose: if someone
#: re-baselines the constants, this test must fail loudly rather than follow the constant.
NEW_CALIBER_START = "2026-10-19"
NEW_CALIBER_END = "2026-11-05"
#: The retired `M3.4` snapshot window -- still reachable via `--start`, never the default.
OLD_CALIBER_START = "2026-01-05"


# ---------------------------------------------------------------------------
# 1. Defaults ARE the new caliber
# ---------------------------------------------------------------------------
def test_default_window_constants_are_the_new_caliber():
    assert _mod.DEFAULT_START == NEW_CALIBER_START
    assert _mod.DEFAULT_END == NEW_CALIBER_END


def test_parser_defaults_are_the_new_caliber():
    args = _mod.build_arg_parser().parse_args([])
    assert args.start == NEW_CALIBER_START
    assert args.end == NEW_CALIBER_END
    assert (args.start, args.end) == (_mod.DEFAULT_START, _mod.DEFAULT_END)


def test_old_caliber_start_is_not_the_default_any_more():
    """Guards the exact regression that motivated this file: a bare run must not ask the
    retired window's question."""
    args = _mod.build_arg_parser().parse_args([])
    assert args.start != OLD_CALIBER_START


# ---------------------------------------------------------------------------
# 2. Overrides still win (unchanged behaviour)
# ---------------------------------------------------------------------------
def test_start_and_end_overrides_win_over_defaults():
    args = _mod.build_arg_parser().parse_args(
        ["--start", OLD_CALIBER_START, "--end", "2026-11-05"]
    )
    assert args.start == OLD_CALIBER_START
    assert args.end == "2026-11-05"


def test_overrides_are_independent_of_each_other():
    """`--start` alone must keep the default end (that is how the old caliber is reproduced)."""
    args = _mod.build_arg_parser().parse_args(["--start", OLD_CALIBER_START])
    assert args.start == OLD_CALIBER_START
    assert args.end == NEW_CALIBER_END


# ---------------------------------------------------------------------------
# 3. The rest of the CLI surface is untouched by the re-baseline
# ---------------------------------------------------------------------------
def test_other_cli_defaults_unchanged():
    args = _mod.build_arg_parser().parse_args([])
    assert args.symbol == "511880.SH"
    assert args.peer == "000001.SH"
    assert args.interval == "5m"
    assert args.bars_per_session == BARS_PER_SESSION_5M
    assert args.tolerance == pytest.approx(0.99)
    assert args.json is False
    # The DB path default is the repo cache, but merely *naming* it must not touch the disk.
    assert args.db.endswith(os.path.join("data", "cache.duckdb"))


def test_json_flag_still_parses():
    assert _mod.build_arg_parser().parse_args(["--json"]).json is True


# ---------------------------------------------------------------------------
# 4. Exit-code contract (pure helper -- never touches the DB)
# ---------------------------------------------------------------------------
def test_exit_zero_only_when_all_verdicts_pass():
    assert _mod.exit_code_for_statuses([STATUS_PASS, STATUS_PASS, STATUS_PASS]) == 0
    assert _mod.exit_code_for_statuses([STATUS_PASS, STATUS_PASS]) == 0


@pytest.mark.parametrize(
    "statuses",
    [
        [STATUS_FAIL, STATUS_PASS, STATUS_PASS],
        [STATUS_PASS, STATUS_FAIL, STATUS_PASS],
        [STATUS_PASS, STATUS_PASS, STATUS_FAIL],
        [STATUS_INSUFFICIENT, STATUS_PASS, STATUS_PASS],
        [STATUS_PASS, STATUS_INSUFFICIENT, STATUS_PASS],
        [STATUS_FAIL, STATUS_FAIL, STATUS_FAIL],
    ],
)
def test_exit_one_when_any_verdict_fails_or_is_insufficient(statuses):
    assert _mod.exit_code_for_statuses(statuses) == 1


def test_exit_two_is_reserved_and_never_produced_by_the_helper():
    assert _mod.EXIT_ALL_PASS == 0
    assert _mod.EXIT_NOT_ALL_PASS == 1
    assert _mod.EXIT_USAGE_OR_IO == 2
    # The helper only ever returns 0 or 1 -- `2` is main()'s usage/IO escape hatch.
    produced = {
        _mod.exit_code_for_statuses([STATUS_PASS, STATUS_PASS]),
        _mod.exit_code_for_statuses([STATUS_FAIL, STATUS_PASS]),
        _mod.exit_code_for_statuses([STATUS_INSUFFICIENT]),
    }
    assert produced <= {0, 1}
    assert 2 not in produced

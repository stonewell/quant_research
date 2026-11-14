"""Offline contract tests for the `scripts/report_intraday_coverage.py` CLI surface.

WHAT IS BEING PINNED, AND WHY IT NEEDS A TEST
---------------------------------------------
`M3.11` was closed on the **new caliber** ruling of 2026-11-08, and the script's default target
window has since been changed twice: first from the retired `M3.4` snapshot
(`2026-01-05 ~ 2026-11-05`) to the `M3.11` snapshot (`2026-10-19 ~ 2026-11-05`), then from that
second frozen literal to a **derived** window. Each fixed pair was a correct answer about a
window nobody claims any more the moment the cache grew past it, which is exactly the kind of
stale default that silently outlives its ruling.

Four things are therefore pinned here, and nothing else:

  * the window is DERIVED from the cache (`end` = latest bar date, `start` = `end` minus
    `--sessions` sessions) and no fixed `DEFAULT_START` / `DEFAULT_END` literal comes back;
  * `--start` / `--end` overrides still win over the derivation, independently;
  * the weekday look-back arithmetic itself (`window_start_for`) and the resolution
    precedence (`resolve_window`);
  * the exit-code contract -- `0` only when every verdict is `pass`, `1` when any is
    `fail`/`insufficient`, `2` reserved for usage / IO errors.

100% offline: the script is loaded through `importlib` (it lives in `scripts/`, not in an
importable package) and only `build_arg_parser()` / `window_start_for()` / `resolve_window()` /
`exit_code_for_statuses()` are exercised. **No DuckDB connection is opened, no `data/` path is
stat'd, no network is touched** -- the module-level `import duckdb` is a library import only,
and `main()` (the sole place that connects) is never called.
"""

import datetime as dt
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

#: The retired `M3.4` snapshot window -- still reachable via `--start`, never the default.
OLD_CALIBER_START = "2026-01-05"
#: The `M3.11` snapshot's end date -- the second literal that used to be a default.
SECOND_LITERAL_END = "2026-11-05"


# ---------------------------------------------------------------------------
# 1. The default window is DERIVED, not a fixed pair
# ---------------------------------------------------------------------------
def test_no_fixed_default_window_literal_survives():
    """The whole point of the change: there is no `DEFAULT_START`/`DEFAULT_END` to go stale.

    Written as an explicit absence check on purpose: re-introducing either constant (even
    with an updated date) would resurrect the defect this test exists to prevent.
    """
    assert not hasattr(_mod, "DEFAULT_START")
    assert not hasattr(_mod, "DEFAULT_END")
    assert _mod.DEFAULT_SESSIONS == 20


def test_parser_defaults_are_derived():
    args = _mod.build_arg_parser().parse_args([])
    assert args.start is None
    assert args.end is None
    assert args.sessions == _mod.DEFAULT_SESSIONS


def test_old_caliber_start_is_not_the_default_any_more():
    """Guards the exact regression that motivated this file: a bare run must not ask the
    retired window's question."""
    args = _mod.build_arg_parser().parse_args([])
    assert args.start != OLD_CALIBER_START
    assert args.end != SECOND_LITERAL_END


@pytest.mark.parametrize(
    "end, sessions, expected_start",
    [
        # 20 sessions back from a Friday: 4 full weeks + the Friday itself.
        (dt.date(2026, 11, 13), 20, dt.date(2026, 10, 19)),
        (dt.date(2026, 11, 13), 1, dt.date(2026, 11, 13)),
        (dt.date(2026, 11, 13), 5, dt.date(2026, 11, 9)),
        # A weekend end is not a session: the count starts at the preceding Friday.
        (dt.date(2026, 11, 14), 1, dt.date(2026, 11, 13)),
        (dt.date(2026, 11, 15), 2, dt.date(2026, 11, 12)),
        # Multi-week span, no weekend counted twice.
        (dt.date(2026, 12, 1), 10, dt.date(2026, 11, 18)),
    ],
)
def test_window_start_for_counts_weekdays_backwards(end, sessions, expected_start):
    assert _mod.window_start_for(end, sessions) == expected_start


@pytest.mark.parametrize("sessions", [0, -1, _mod.MAX_SESSIONS + 1])
def test_window_start_for_rejects_absurd_session_counts(sessions):
    with pytest.raises(ValueError):
        _mod.window_start_for(dt.date(2026, 11, 13), sessions)


def test_derived_window_uses_the_latest_bar_and_reports_its_sources():
    latest = dt.date(2026, 11, 13)
    start, end, start_source, end_source = _mod.resolve_window(None, None, 20, latest)
    assert (start, end) == (_mod.window_start_for(latest, 20), latest)
    assert (start_source, end_source) == ("derived", "latest_bar")


def test_resolution_requires_a_window_end_when_the_cache_has_no_bars():
    with pytest.raises(ValueError):
        _mod.resolve_window(None, None, 20, None)


# ---------------------------------------------------------------------------
# 2. Overrides still win (unchanged precedence)
# ---------------------------------------------------------------------------
def test_start_and_end_overrides_win_over_the_derivation():
    args = _mod.build_arg_parser().parse_args(
        ["--start", OLD_CALIBER_START, "--end", SECOND_LITERAL_END]
    )
    assert args.start == OLD_CALIBER_START
    assert args.end == SECOND_LITERAL_END
    start, end, start_source, end_source = _mod.resolve_window(
        args.start, args.end, 20, dt.date(2026, 11, 13)
    )
    assert (start.isoformat(), end.isoformat()) == (OLD_CALIBER_START, SECOND_LITERAL_END)
    assert (start_source, end_source) == ("--start", "--end")


def test_overrides_are_independent_of_each_other():
    """`--start` alone must keep the derived end (that is how the old caliber is reproduced)."""
    args = _mod.build_arg_parser().parse_args(["--start", OLD_CALIBER_START])
    assert args.start == OLD_CALIBER_START
    assert args.end is None
    start, end, start_source, end_source = _mod.resolve_window(
        args.start, args.end, 20, dt.date(2026, 11, 13)
    )
    assert start.isoformat() == OLD_CALIBER_START
    assert end == dt.date(2026, 11, 13)
    assert (start_source, end_source) == ("--start", "latest_bar")


def test_sessions_override_changes_the_derived_start_only():
    args = _mod.build_arg_parser().parse_args(["--sessions", "5"])
    assert args.sessions == 5
    start, end, _, _ = _mod.resolve_window(
        args.start, args.end, args.sessions, dt.date(2026, 11, 13)
    )
    assert (start, end) == (dt.date(2026, 11, 9), dt.date(2026, 11, 13))


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

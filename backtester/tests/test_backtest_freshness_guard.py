"""`L2.3`/`L2.5` (design §11) -- the BACKTEST side of the freshness contract.

The market clock exists so LIVE runs can demand a specific session from the cache. A
backtest must never do that: if its `expected_last_session` depended on the wall clock,
the same command would take a different cache-coverage decision (and possibly re-fetch
different data) depending on the day it was run. There is exactly one legal value on
this side of the boundary, `None`, and `run_backtest.py` centralises it in
`backtest_expected_last_session()` so a future edit cannot silently thread a real
session through.

`L2.5` strengthens that from "a date is rejected" to "**any** non-None value is
rejected" (identity comparison, so `0` / `False` / `""` cannot slip through), asserts
that every call site passes the marker with *no* argument, and adds a runtime smoke
test that actually runs a backtest and records what reached the data layer.
"""

import ast
import datetime as dt
import json
import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_PROJECT_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

import backtester.run_backtest as run_backtest
from backtester.run_backtest import (
    BacktestSessionLeakError,
    backtest_expected_last_session,
)

_BACKTESTER_DIR = os.path.join(_PROJECT_ROOT, "backtester")
_RUN_BACKTEST = os.path.join(_BACKTESTER_DIR, "run_backtest.py")

#: Every call shape in that file that reaches the cache/data layer.
_LOAD_CALL_NAMES = frozenset(
    {"load_universe", "load_universe_with_banner", "load_ohlcv", "fetch_ohlcv", "fetch_universe"}
)


def _parse(path):
    with open(path, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def test_backtest_guard_is_none_only():
    assert backtest_expected_last_session() is None
    assert backtest_expected_last_session(None) is None


@pytest.mark.parametrize(
    "leaked",
    [
        dt.date(2026, 10, 22),
        dt.datetime(2026, 10, 22, 15, 30),
        pd.Timestamp("2026-10-22"),
        "2026-10-22",
        # Falsey-but-not-None: the exact shapes a truthiness check would let through.
        0,
        "",
        False,
    ],
)
def test_any_non_none_session_is_rejected(leaked):
    """GUARD (L2.5): rejection is on IDENTITY with `None`, not on truthiness/type."""
    with pytest.raises(BacktestSessionLeakError) as excinfo:
        backtest_expected_last_session(leaked)
    # Still catchable as the historical RuntimeError type (no caller breakage).
    assert isinstance(excinfo.value, RuntimeError)
    assert "expected_last_session=None" in str(excinfo.value)


def test_every_backtest_load_call_pins_the_none_contract():
    """No load call in `run_backtest.py` may omit the session -- that is the escape hatch.

    Omitting the kwarg *happens* to mean `None` today, so the assertion is deliberately
    about the call SHAPE: it requires the explicit `backtest_expected_last_session()`
    marker, which is what makes the next edit (someone "helpfully" passing an
    `as_of_date`) raise instead of quietly making the backtest clock-dependent.
    """
    tree = _parse(_RUN_BACKTEST)

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _LOAD_CALL_NAMES:
            continue
        checked += 1
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        assert "expected_last_session" in kwargs, (
            f"backtester/run_backtest.py:{node.lineno} {name}(...) lost its explicit "
            "expected_last_session=backtest_expected_last_session() marker"
        )
        value = kwargs["expected_last_session"]
        assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == (
            "backtest_expected_last_session"
        ), (
            f"backtester/run_backtest.py:{node.lineno} {name}(...) must pass "
            f"backtest_expected_last_session(), got {ast.dump(value)[:120]}"
        )
        # (L2.5) ...and it must be called with NO argument. `backtest_expected_last_session(None)`
        # would re-open the door: the next edit only has to swap the argument.
        assert not value.args and not value.keywords, (
            f"backtester/run_backtest.py:{node.lineno} {name}(...) calls "
            "backtest_expected_last_session with an argument; the only legal call is "
            "the zero-argument marker"
        )

    assert checked >= 2, "fixture drifted: expected at least the baseline + main universe loads"


def test_marker_is_never_called_with_an_argument_anywhere():
    """Every `backtest_expected_last_session(...)` call site in `run_backtest.py` is zero-arg."""
    tree = _parse(_RUN_BACKTEST)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "backtest_expected_last_session"
    ]
    assert calls, "fixture drifted: the marker is no longer called in run_backtest.py"
    for node in calls:
        assert not node.args and not node.keywords, (
            f"backtester/run_backtest.py:{node.lineno} passes an argument to "
            "backtest_expected_last_session(); use the zero-argument marker"
        )


def test_no_other_backtester_module_threads_a_session():
    """No module in `backtester/` may hand a session value to a data load itself.

    `run_backtest.py` is the only place allowed to mention `expected_last_session`, and
    there only as `backtest_expected_last_session()`. Anything else would be a second,
    drifting definition of the boundary.
    """
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(_BACKTESTER_DIR):
        if "__pycache__" in dirpath or os.path.basename(dirpath) == "tests":
            continue
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            if path == _RUN_BACKTEST:
                continue
            tree = _parse(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "expected_last_session":
                    offenders.append(f"{os.path.relpath(path, _PROJECT_ROOT)}:{node.lineno}")
    assert not offenders, (
        "only backtester/run_backtest.py may thread expected_last_session (always as "
        f"backtest_expected_last_session()); found: {offenders}"
    )


def test_smoke_backtest_only_touches_the_none_path(tmp_path, monkeypatch):
    """SMOKE (L2.5): run a real backtest and record what reached the loader.

    The AST tests pin the call SHAPE; this pins the RUNTIME trace. Every
    `load_universe_with_banner` invocation during a full `main()` run must carry
    `expected_last_session=None`, and at least one must actually happen (otherwise the
    smoke proves nothing). Everything is offline: the synthetic provider, `--no-cache`,
    and a `tmp_path` results directory.
    """
    observed = []
    real_loader = run_backtest.load_universe_with_banner

    def _spy(*args, **kwargs):
        observed.append(kwargs.get("expected_last_session", "<omitted>"))
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(run_backtest, "load_universe_with_banner", _spy)

    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(
        json.dumps({"template_name": "equal_weight", "params": {"rebalance_freq_days": 10}}),
        encoding="utf-8",
    )
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA", "BBB",
        "--data-provider", "synthetic",
        "--no-cache",
        "--start", "2020-01-01",
        "--end", "2021-12-31",
        "--results-dir", str(tmp_path / "results"),
        "--cache-dir", str(tmp_path / "cache"),
        "--no-plots",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    run_backtest.main()

    assert observed, "smoke did not exercise any data load -- it proves nothing"
    assert all(v is None for v in observed), (
        f"a non-None expected_last_session reached the backtest loader: {observed}"
    )

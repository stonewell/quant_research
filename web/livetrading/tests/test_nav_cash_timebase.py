"""`LV1-TIMEBASE` -- the pre-staged valuation snapshot must be marked at ONE instant.

FORENSIC CONTEXT (2026-11-10 / 11-11 live rounds, reproduced offline here)
--------------------------------------------------------------------------
Before 2026-11-11, `nav_breakdown.json` was written TWICE per round, to the SAME path:

1. **before** the Stage-1 gate is launched -- `ExecutionService.run_health_check` ->
   `_archived_cash_for` -> `_resolve_nav(cash=<last archived round's summary.json::cash>)`
   -> `_write_nav_breakdown` (the "LV11 pre-stage");
2. **after** it, by Stage 2 -- `run_live_deploy` -> `_resolve_nav(cash=<operator form>)` ->
   `_write_nav_breakdown`, which OVERWRITES (1).

That double-write is now history (split on 2026-11-11): `nav_breakdown.json` is the
STAGE-1 gate snapshot (pre-staged by `run_health_check` before the gate child and read
exactly by the gate via `--nav-breakdown-file`, so the archived file IS what the breaker
decided on), and Stage 2's own deploy-time mark goes to `nav_breakdown.stage2.json`
(its `summary.json` fields live under the `*_stage2` suffix next to the bare-name gate
fields). In the 2026-11-10/11-11 archives the gate read (1) and the archive kept (2),
so the archived `nav_breakdown.json` could be a perfectly consistent file that Stage 1
never saw. Real numbers:

    11-10: gate saw 208,067.60 = equity 182,471.00 + cash 25,596.60  (11-09's
           `summary.json::cash`)  -> +5.14% PHANTOM GAIN vs the same-instant 196,852.32
    11-11: gate saw 101,093.32 = equity  86,712.00 + cash 14,381.32  (11-10's cash)
           -> -51.59% PHANTOM DRAWDOWN -> TIER_3_HALT / freeze 21 bars / full liquidation,
           while the book had moved -0.23%

Cause: the equity leg had just been refreshed to `as_of` (`_refresh_held_book_daily_bars`),
while the cash leg belonged to the PREVIOUS session. The fix stamps the cash leg's session
(`cash_as_of`) and, when it is not the session being marked, WITHHOLDS `nav_effective`
(`cash_as_of` + a reason on disk) instead of publishing `today's equity + yesterday's cash`.
The operator's own cash (web form 现金余额 -> `RunHealthRequest.cash` -> `run_health_check`)
is the same-instant source and restores the normal path.

100% OFFLINE: throwaway `StorageManager` + a real file-backed DuckDB cache under the OS temp
root, `subprocess.run` always monkeypatched. No path under `data/` is opened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pandas as pd
import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import storage  # noqa: E402  (bare name, exactly how `server.py` imports it)
import execution_service  # noqa: E402
from common.duckdb_cache import DuckDBCache  # noqa: E402

PROVIDER = "marketdb"  # NOT "synthetic": the engine reads the cache for its mark
PREV_SESSION = "2026-01-05"
AS_OF = "2026-01-06"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, on_launch=None):
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        if on_launch is not None:
            on_launch(list(cmd))
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _frame(rows, column="Close"):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    series = pd.Series([float(v) for _, v in items], index=idx)
    return pd.DataFrame({column: series}, index=idx)


def _seed_book(cache, symbol="AAA", price=10.0):
    """`AAA`: 10.00 on both sessions, factor 1.0 -> 100 shares mark to exactly 1,000."""
    cache.store_bars(
        PROVIDER,
        symbol,
        _frame({PREV_SESSION: price, AS_OF: price}).assign(
            Open=price, High=price, Low=price, Volume=1_000_000.0
        )[["Open", "High", "Low", "Close", "Volume"]],
        adjustflag="raw",
    )
    cache.store_adjust_factors(symbol, _frame({PREV_SESSION: 1.0, AS_OF: 1.0}, "factor_hfq"))


@pytest.fixture
def workspace(tmp_path):
    dirs = {}
    for name in ("data", "runs", "cache", "settings"):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        dirs[name] = d
    mgr = storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(dirs["data"]),
        archive_dir=str(dirs["runs"]),
        cache_dir=str(dirs["cache"]),
        settings_dir=str(dirs["settings"]),
    )
    return {"storage": mgr, "dirs": dirs, "tmp_path": tmp_path}


def _strategy_file(workspace):
    path = os.path.join(str(workspace["tmp_path"]), "strat.json")
    workspace["storage"]._save_json(path, {"template_name": "dummy", "params": {}})
    return path


def _archive_round_with_cash(workspace, date_str, cash, cash_source="explicit"):
    """Write a REAL earlier round whose `summary.json` records a measured cash residual."""
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )
    return workspace["storage"].save_daily_run(
        date_str=date_str,
        holdings={"AAA": 100},
        summary={"date": date_str, "cash": cash, "cash_source": cash_source},
        strategy_key=strat_key,
        universe_key=univ_key,
    )


def _stage(workspace, monkeypatch, *, cash=None, seed_book=None):
    """Run ONE `run_health_check` and return the snapshot the gate would have read."""
    _seed_book(DuckDBCache(db_path=str(workspace["dirs"]["cache"])))
    seen = {}

    def _inspect(cmd):
        run_dir = _flag(cmd, "--output-dir")
        with open(
            os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), encoding="utf-8"
        ) as f:
            seen["payload"] = json.load(f)
        seen["pinned"] = _flag(cmd, "--nav-breakdown-file")

    captured = _patch_subprocess(monkeypatch, on_launch=_inspect)
    monkeypatch.setattr(
        workspace["storage"],
        "resolve_seed_holdings",
        lambda *a, **k: (dict(seed_book or {"AAA": 100}), "previous_post_holdings"),
    )
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_health_check(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        data_provider=PROVIDER,
        refresh_data=False,
        cash=cash,
    )
    assert len(captured) == 1, captured
    assert seen["pinned"] == os.path.join(
        _flag(captured[0], "--output-dir"), execution_service.NAV_BREAKDOWN_FILENAME
    ), "the gate must read the file that was just staged"
    return seen["payload"]


# --------------------------------------------------------------------------- #
# 1. The defect: previous session's cash + today's equity
# --------------------------------------------------------------------------- #
def test_previous_sessions_cash_never_yields_a_mixed_nav_effective(workspace, monkeypatch, capsys):
    """DISCRIMINATING: the 11-10 / 11-11 shape must not produce a usable NAV.

    4 x 10.00 == equity 1,000 (0 shares? no: 100 shares) and the archive says cash 250 from
    `2026-01-05`. Pre-fix the staged file carried `nav_effective = 1,250` -- a number that
    belongs to no single instant -- and the gate gated on it. Post-fix the number is WITHHELD
    and the snapshot says why, on disk.
    """
    _archive_round_with_cash(workspace, PREV_SESSION, cash=250.0)

    payload = _stage(workspace, monkeypatch)  # no operator cash -> archive only
    printed = capsys.readouterr().out

    # (1) no mixed NAV reaches the gate
    assert payload["nav_effective"] is None
    assert payload["nav_computed"] is None
    assert payload["nav_source"] == "staged_cash_timebase_mismatch"
    # (2) both legs and BOTH timestamps stay on disk -- the audit trail the 202644 archive lost
    assert payload["as_of"] == AS_OF
    assert payload["cash_as_of"] == PREV_SESSION
    assert payload["equity"] == pytest.approx(1000.0)
    assert payload["cash"] == pytest.approx(250.0)
    assert payload["cash_source"] == "explicit"
    # (3) the reason is explicit, grep-able, and names both sessions
    assert "(LV1-TIMEBASE)" in payload["nav_degradation_reason"]
    assert PREV_SESSION in payload["nav_degradation_reason"]
    assert AS_OF in payload["nav_degradation_reason"]
    warnings = " ".join(payload["nav_warnings"])
    assert "cash_timebase_mismatch" in warnings
    assert "cash_from_archive" in warnings
    assert "[LV1-TIMEBASE]" in printed

    # (4) The gate's own reader therefore degrades instead of gating: reproduce exactly what
    # Stage 1 does with this payload (no import of the Stage-1 script needed -- the contract
    # is `nav_effective` absent => `NAV_EFFECTIVE_MISSING` => degraded/UNKNOWN).
    assert payload["nav_effective"] is None, "Stage 1 sees no gate NAV in this snapshot"


def test_same_instant_operator_cash_keeps_the_normal_path(workspace, monkeypatch):
    """The operator's OWN cash (web 现金余额) restores a single-instant NAV.

    Same archived round as the failing test above; only `cash=` differs. The snapshot must be
    a normal engine mark: `nav_effective == equity + cash`, `cash_as_of == as_of`, source
    `computed`, `cash_source == "explicit"` -- i.e. every pre-existing field keeps its meaning
    (backward compatibility for the explicit-cash path).
    """
    _archive_round_with_cash(workspace, PREV_SESSION, cash=250.0)

    payload = _stage(workspace, monkeypatch, cash=42.5)

    assert payload["nav_effective"] == pytest.approx(1042.5)
    assert payload["nav_computed"] == pytest.approx(1042.5)
    assert payload["nav_effective"] == pytest.approx(payload["equity"] + payload["cash"])
    assert payload["nav_source"] == "computed"
    assert payload["cash_source"] == "explicit"
    assert payload["cash_as_of"] == AS_OF
    assert not any("cash_timebase_mismatch" in w for w in payload["nav_warnings"])
    assert not any("cash_from_archive" in w for w in payload["nav_warnings"])


def test_same_session_archived_cash_is_not_a_mismatch(workspace, monkeypatch):
    """A round archived on `as_of` ITSELF is same-instant -- no degradation.

    "Previous ROUND" is not automatically "previous SESSION": an intraday re-run of the same
    session stages cash measured earlier today, which is the right timebase. Refusing that
    would block every re-run.

    The archive is stubbed at `_archived_cash_for` on purpose: a *flat* same-date round is
    hidden by the pre-existing D8 listing rule the moment the staging allocates an instance
    subdir under that date, which is unrelated to the timebase decision under test.
    """
    origin = os.path.join(str(workspace["dirs"]["runs"]), "x", "summary.json")
    real = execution_service.ExecutionService._archived_cash_for

    def _same_session(self_, date_str, strategy_key, universe_key, exclude_dir=None):
        return 250.0, AS_OF, origin

    monkeypatch.setattr(execution_service.ExecutionService, "_archived_cash_for", _same_session)
    try:
        payload = _stage(workspace, monkeypatch)
    finally:
        monkeypatch.setattr(execution_service.ExecutionService, "_archived_cash_for", real)

    assert payload["cash_as_of"] == AS_OF
    assert payload["nav_effective"] == pytest.approx(1250.0)
    assert payload["nav_source"] == "computed"


# --------------------------------------------------------------------------- #
# 2. The structured timestamp itself
# --------------------------------------------------------------------------- #
def test_archived_cash_carries_the_rounds_session_date(workspace):
    """`_archived_cash_for` must return `(cash, cash_as_of, path)` -- no text parsing.

    Consumers compare `cash_as_of` with the session they are marking. Inferring it from the
    `summary.json` path (what the first, rejected version of this fix did) makes the guard
    depend on a log string.
    """
    _archive_round_with_cash(workspace, PREV_SESSION, cash=250.0)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=["AAA"]
    )

    cash, cash_as_of, origin = svc._archived_cash_for(AS_OF, strat_key, univ_key)

    assert cash == pytest.approx(250.0)
    assert cash_as_of == PREV_SESSION
    assert origin and origin.endswith("summary.json")

    # Nothing usable -> a 3-tuple of Nones (never a fabricated 0.0 / date).
    assert svc._archived_cash_for(AS_OF, "no-such-key", "no-such-universe") == (None, None, None)


def test_stage2_mark_stamps_the_sessions_own_timebase(workspace, monkeypatch):
    """The deploy path stamps `cash_as_of = val_date`: the form cash IS a same-day assertion.

    Guards the other half of the contract -- a same-instant marker must not be a staging-only
    concept, or the gate could not tell a healthy Stage-2 file from a pre-staged one.
    """
    _seed_book(DuckDBCache(db_path=str(workspace["dirs"]["cache"])))
    seen = {}

    def _run(cmd, *args, **kwargs):
        run_dir = _flag(list(cmd), "--output-dir")
        with open(
            os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), encoding="utf-8"
        ) as f:
            seen["payload"] = json.load(f)
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run)
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    svc.run_live_deploy(
        strategy_file=_strategy_file(workspace),
        custom_symbols=["AAA"],
        as_of_date=AS_OF,
        portfolio_value=100000.0,
        current_holdings={"AAA": 100},
        data_provider=PROVIDER,
        cash=42.5,
        edited=True,
    )

    payload = seen["payload"]
    assert payload["nav_effective"] == pytest.approx(1042.5)
    assert payload["cash_as_of"] == AS_OF
    assert payload["cash_source"] == "explicit"

"""Stage 2 must deploy the DIVIDEND-AWARE NAV, never the raw form field.

FORENSIC CONTEXT
----------------
`web/livetrading/valuation.py` (19 offline tests in
``web/livetrading/tests/test_portfolio_valuation.py``) marks a book
dividend-inclusively as ``shares x raw_close x (factor_hfq[as_of] / factor_hfq[anchor])``.
Until now nothing in the deployment chain used it: ``run_live_deploy`` pushed
``--portfolio-value <the operator's form field>`` straight at
``scripts/run_live_strategy.py``.  Every downstream number -- target weights, whole-lot
rounding, the cash sweep, the ticket's ``target_shares`` -- is sized off that value, so an
unmarked NAV silently shrinks every position by the dividends paid since entry.

This suite pins the wire-in (``ExecutionService._resolve_nav``):

1. the child's ``--portfolio-value`` is the ENGINE's mark, not the form value;
2. an explicit human override is honoured, logged at WARNING level and tagged
   ``nav_source="override"``;
3. a valuation failure ABORTS by default (no child process) and only degrades to the form
   value under ``LIVETRADING_ALLOW_NAV_FALLBACK=1`` (``nav_source="fallback_input"``);
4. the per-symbol mark lands in ``<run_dir>/nav_breakdown.json`` while ``summary.json``
   keeps scalars only (a 20-symbol book must not bloat the digest);
5. a missing / uninitialised cache is reported as ``cache_uninitialized`` -- the cure is a
   Stage-1 data refresh, NOT "wait for the close" -- and never as ``stale_price``.

WHY THIS FILE LIVES IN ``pipeline/tests/``
------------------------------------------
Same reason as ``test_live_holdings_input_check.py``: ``web/livetrading/tests`` needs
``fastapi`` + ``pydantic``, which the shared ``pipeline`` venv does not have, so that
package does not collect there.  ``execution_service.py``/``storage.py`` import only
stdlib + pandas + ``common.*``, and the engine is reached through `compute_nav` itself.

100% offline: every case uses ``tmp_path`` + a throwaway DuckDB file (or a deliberately
uninitialised one), ``subprocess.run`` is always monkeypatched, and no path under the real
``data/`` tree is ever opened.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import subprocess
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WEB_DIR = os.path.join(_REPO_ROOT, "web", "livetrading")
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.duckdb_cache import DuckDBCache  # noqa: E402

#: `execution_service` imports `storage`/`valuation` by BARE name after putting
#: `web/livetrading` on sys.path (exactly how `server.py` does it), so these modules are
#: imported the same way here -- loading them under importlib aliases would create a
#: second `storage` module whose `StorageManager` is not the one the service sees.
import storage  # noqa: E402


def _load_execution_service():
    """Load ``execution_service.py`` without importing it under its bare name.

    Unique module name so this suite cannot collide with another test file that loads a
    same-named module; its OWN bare imports (`storage`, `valuation`) still resolve through
    `sys.path`, which is what we want.
    """
    path = os.path.join(_WEB_DIR, "execution_service.py")
    spec = importlib.util.spec_from_file_location("web_livetrading_execution_service_nav", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


execution_service = _load_execution_service()

PROVIDER = "marketdb"  # not "synthetic": the engine reads the cache, synthetic has no rows
AS_OF = "2026-01-06"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch):
    """Capture argv and never launch a real process; returns the capture list."""
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _bars(rows):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    close = pd.Series([float(c) for _, c in items], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1_000_000.0},
        index=idx,
    )


def _factors(rows):
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    return pd.DataFrame({"factor_hfq": pd.Series([float(v) for _, v in items], index=idx)}, index=idx)


def _seed_cache(cache, symbol, bars, factors=None):
    cache.store_bars(PROVIDER, symbol, _bars(bars), adjustflag="raw")
    if factors is not None:
        cache.store_adjust_factors(symbol, _factors(factors))


@pytest.fixture
def workspace(tmp_path):
    """Hermetic StorageManager + a REAL file-backed cache under ``tmp_path``.

    A file (not ``:memory:``) is required: the service resolves the cache from
    ``StorageManager.cache_dir`` and opens it READ-ONLY, exactly as production does.
    """
    dirs = {}
    for name in ("data", "runs", "cache", "settings"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d
    mgr = storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(dirs["data"]),
        archive_dir=str(dirs["runs"]),
        cache_dir=str(dirs["cache"]),
        settings_dir=str(dirs["settings"]),
    )
    return {"storage": mgr, "dirs": dirs, "tmp_path": tmp_path}


def _seed_two_bar_book(workspace, holdings=None, cash=1000.0):
    """``AAA``: 10.00 -> 10.00, factor 1.0 -> 1.0, anchored on the first bar."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(
        cache,
        "AAA",
        {"2026-01-05": 10.0, AS_OF: 10.0},
        {"2026-01-05": 1.0, AS_OF: 1.0},
    )
    return cache


def _strategy_file(workspace):
    path = os.path.join(str(workspace["tmp_path"]), "strat.json")
    workspace["storage"]._save_json(path, {"template_name": "dummy", "params": {}})
    return path


def _archive_run(workspace, date_str, holdings, custom_symbols=None):
    """Write a REAL archived run directory (via `StorageManager.save_daily_run`).

    This is what `_derive_anchors` reads: the archive is the only record of when a
    position was opened.  Using the production writer (not a hand-rolled directory)
    keeps the test honest about the archive layout it depends on.  `custom_symbols` must
    match the deploy's, because it is what the universe key (hence the archive subtree)
    is derived from.
    """
    strat_key, univ_key, _, _ = workspace["storage"].resolve_keys(
        strategy_file=_strategy_file(workspace), custom_symbols=list(custom_symbols or ["AAA"])
    )
    return workspace["storage"].save_daily_run(
        date_str=date_str,
        holdings=holdings,
        summary={"date": date_str},
        strategy_key=strat_key,
        universe_key=univ_key,
    )


def _run(workspace, monkeypatch, **kwargs):
    svc = execution_service.ExecutionService(storage=workspace["storage"])
    kwargs.setdefault("strategy_file", _strategy_file(workspace))
    kwargs.setdefault("custom_symbols", ["AAA"])
    kwargs.setdefault("as_of_date", AS_OF)
    kwargs.setdefault("data_provider", PROVIDER)
    return svc.run_live_deploy(**kwargs)


# ---------------------------------------------------------------------------
# 1. computed path -- the ENGINE's mark is what the child receives
# ---------------------------------------------------------------------------
def test_portfolio_value_passed_to_child_is_the_computed_nav(workspace, monkeypatch):
    """DISCRIMINATING: the form says 100,000; the book is worth 100 x 10 + 1,000 cash = 2,000.

    The pre-wire-in implementation forwarded ``str(portfolio_value)`` verbatim, so this
    assertion fails on it (the argv would read ``--portfolio-value 100000.0``).
    """
    _seed_two_bar_book(workspace)
    captured = _patch_subprocess(monkeypatch)

    calls = []
    real_compute_nav = execution_service.compute_nav

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_compute_nav(*args, **kwargs)

    monkeypatch.setattr(execution_service, "compute_nav", _spy)

    res = _run(
        workspace,
        monkeypatch,
        current_holdings={"AAA": 100},
        cash=1000.0,
        portfolio_value=100000.0,
    )

    assert len(calls) == 1, calls  # the engine really ran, once
    assert len(captured) == 1, captured
    cmd = captured[0]
    # 100 shares x 10.00 raw x (1.0 / 1.0) + 1,000 cash
    assert _flag(cmd, "--portfolio-value") == "2000.0"
    assert _flag(cmd, "--portfolio-value") != "100000.0"
    assert res["nav_effective"] == pytest.approx(2000.0)
    assert res["nav_computed"] == pytest.approx(2000.0)
    assert res["nav_source"] == "computed"
    assert res["cash"] == pytest.approx(1000.0)
    assert res["cash_source"] == "explicit"


def test_cash_blank_falls_back_to_the_parked_cash_holding(workspace, monkeypatch):
    """``cash=None`` + a BIL row -> the engine values BIL at its raw close and moves it OUT
    of equity, so the same money is never counted in both legs (``bil_fallback``)."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.0})
    _seed_cache(cache, "BIL", {"2026-01-05": 100.0, AS_OF: 100.0}, {"2026-01-05": 1.0, AS_OF: 1.0})
    captured = _patch_subprocess(monkeypatch)

    res = _run(
        workspace,
        monkeypatch,
        current_holdings={"AAA": 100, "BIL": 5},
        custom_symbols=["AAA", "BIL"],
        cash=None,
        portfolio_value=100000.0,
    )

    assert res["cash_source"] == "bil_fallback"
    # 100 x 10 (equity) + 5 x 100 (cash) = 1,500
    assert _flag(captured[0], "--portfolio-value") == "1500.0"
    assert res["nav_computed"] == pytest.approx(1500.0)


def test_dividend_factor_ratio_reaches_the_child_argv(workspace, monkeypatch):
    """The mark is dividend-INCLUSIVE **once an open date is known**: factor 1.0 -> 1.05
    lifts 100 shares by 5%.

    Raw close is FLAT (10.0 -> 10.0) on purpose, so a raw-only mark would report 1,000 and
    this test would fail -- proving the factor ratio reaches the argv.  The open date comes
    from the run archive (`_derive_anchors`), which is the ONLY thing that licenses the
    total-return ratio: without it the engine would (correctly) mark at raw, because
    anchoring on the start of the factor history capitalises pre-entry dividends.
    """
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.05})
    _archive_run(workspace, "2026-01-05", {"AAA": 100})
    captured = _patch_subprocess(monkeypatch)

    res = _run(
        workspace,
        monkeypatch,
        current_holdings={"AAA": 100},
        cash=0.0,
        portfolio_value=100000.0,
    )

    assert res["nav_computed"] == pytest.approx(1050.0)
    assert _flag(captured[0], "--portfolio-value") == "1050.0"
    assert res["anchor_source"] == "run_archive"


def test_no_archived_open_date_marks_at_raw_and_says_so(workspace, monkeypatch):
    """HARD REGRESSION at the wire-in layer: no archive -> NO anchor -> raw, not 1.05x.

    The factor series is 1.0 (2026-01-05) -> 1.05 (as_of), i.e. the ratio an
    earliest-factor-date anchor would have applied.  The child must receive
    100 x 10.00 = 1000, and the 5% must NOT be capitalised.
    """
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.05})
    captured = _patch_subprocess(monkeypatch)

    res = _run(
        workspace,
        monkeypatch,
        current_holdings={"AAA": 100},
        cash=0.0,
        portfolio_value=100000.0,
    )

    assert res["nav_computed"] == pytest.approx(1000.0)
    assert _flag(captured[0], "--portfolio-value") == "1000.0"
    assert _flag(captured[0], "--portfolio-value") != "1050.0"
    assert res["anchor_source"] is None
    assert any(w.startswith("anchor_unavailable") for w in res["nav_warnings"]), res["nav_warnings"]

    run_dir = _flag(captured[0], "--output-dir")
    with open(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), encoding="utf-8") as f:
        breakdown = json.load(f)
    entry = breakdown["per_symbol"]["AAA"]
    assert entry["value_raw"] == pytest.approx(1000.0)
    assert entry["value"] == pytest.approx(1000.0)          # not 1050
    assert entry["anchor_inflation_ratio"] == pytest.approx(1.0)
    assert entry["dividend_basis"] == "raw"
    assert entry["full_history_ratio"] == pytest.approx(1.05)  # the rejected ratio, measured
    assert breakdown["anchor_source"] is None
    assert breakdown["anchors"] == {}


def test_anchors_come_from_the_first_archived_session_holding_the_symbol(workspace, monkeypatch):
    """Anchor = FIRST archived session where shares > 0, per symbol, scanned oldest->newest.

    ``AAA`` is held from 2026-01-02; ``BBB`` only appears from 2026-01-05, so the two
    symbols must get DIFFERENT open dates (a single book-wide date would mis-anchor one).
    """
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-02": 10.0, AS_OF: 10.0}, {"2026-01-02": 1.0, AS_OF: 1.2})
    _seed_cache(cache, "BBB", {"2026-01-05": 20.0, AS_OF: 20.0}, {"2026-01-05": 1.1, AS_OF: 1.32})
    _archive_run(workspace, "2026-01-02", {"AAA": 100}, custom_symbols=["AAA", "BBB"])
    _archive_run(workspace, "2026-01-05", {"AAA": 100, "BBB": 50}, custom_symbols=["AAA", "BBB"])
    captured = _patch_subprocess(monkeypatch)

    res = _run(
        workspace,
        monkeypatch,
        current_holdings={"AAA": 100, "BBB": 50},
        custom_symbols=["AAA", "BBB"],
        cash=0.0,
        portfolio_value=100000.0,
    )

    run_dir = _flag(captured[0], "--output-dir")
    with open(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), encoding="utf-8") as f:
        breakdown = json.load(f)

    assert breakdown["anchor_source"] == "run_archive"
    assert breakdown["anchors"] == {"AAA": "2026-01-02", "BBB": "2026-01-05"}
    # AAA: 100 x 10 x (1.2 / 1.0); BBB: 50 x 20 x (1.32 / 1.1)
    aaa = breakdown["per_symbol"]["AAA"]
    bbb = breakdown["per_symbol"]["BBB"]
    assert aaa["anchor_date"] == "2026-01-02"
    assert aaa["value"] == pytest.approx(100 * 10.0 * 1.2)
    assert aaa["anchor_inflation_ratio"] == pytest.approx(1.2)
    assert bbb["anchor_date"] == "2026-01-05"
    assert bbb["value"] == pytest.approx(50 * 20.0 * (1.32 / 1.1))
    assert res["anchor_source"] == "run_archive"


# ---------------------------------------------------------------------------
# 2. override path -- an explicit human NAV wins, loudly
# ---------------------------------------------------------------------------
def test_explicit_override_uses_the_form_value_and_warns(workspace, monkeypatch, caplog):
    _seed_two_bar_book(workspace)
    captured = _patch_subprocess(monkeypatch)

    with caplog.at_level(logging.WARNING):
        res = _run(
            workspace,
            monkeypatch,
            current_holdings={"AAA": 100},
            cash=1000.0,
            portfolio_value=12345.0,
            nav_override=True,
        )

    assert res["nav_source"] == "override"
    assert _flag(captured[0], "--portfolio-value") == "12345.0"
    # The engine's own mark is still recorded, so the drift is auditable.
    assert res["nav_computed"] == pytest.approx(2000.0)
    assert any(w.startswith("nav_override") for w in res["nav_warnings"]), res["nav_warnings"]

    message = " ".join(rec.getMessage() for rec in caplog.records)
    assert "本次使用了人工覆盖净值" in message
    assert "12,345.00" in message and "2,000.00" in message
    assert "%" in message  # the percentage drift is spelled out, per the spec


def test_override_still_deploys_when_the_engine_cannot_compute(workspace, monkeypatch, caplog):
    """An override exists for the case where the engine CANNOT run (deleted cache).

    It must not be blocked by the very failure it is meant to bypass -- but it must say so.
    """
    # No cache.duckdb at all: the read-only open raises FileNotFoundError.
    captured = _patch_subprocess(monkeypatch)

    with caplog.at_level(logging.WARNING):
        res = _run(
            workspace,
            monkeypatch,
            current_holdings={"AAA": 100},
            cash=1000.0,
            portfolio_value=54321.0,
            nav_override=True,
        )

    assert res["nav_source"] == "override"
    assert _flag(captured[0], "--portfolio-value") == "54321.0"
    assert res["nav_computed"] is None
    assert any(
        w.startswith("nav_override_no_reference") or w.startswith("reason=cache_uninitialized")
        for w in res["nav_warnings"]
    ), res["nav_warnings"]
    assert "人工覆盖" in " ".join(rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# 3. failure semantics -- abort by default, degrade only on explicit opt-in
# ---------------------------------------------------------------------------
def test_missing_cash_aborts_before_any_subprocess(workspace, monkeypatch):
    """No cash and no parked-cash holding -> MissingCashError -> refusal, no child spawn."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.0})
    captured = _patch_subprocess(monkeypatch)
    monkeypatch.delenv(execution_service.NAV_FALLBACK_ENV, raising=False)

    with pytest.raises(execution_service.NavValuationError) as excinfo:
        _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=None, portfolio_value=100000.0)

    assert captured == []  # no order ticket was produced
    err = excinfo.value
    assert err.reason == "missing_cash"
    assert "现金余额无法确定" in str(err)
    assert "现金" in str(err)  # the hint names the field to fill in
    assert any(w.startswith("reason=missing_cash") for w in err.nav_warnings)


def test_missing_cash_falls_back_only_with_the_explicit_switch(workspace, monkeypatch, caplog):
    """``LIVETRADING_ALLOW_NAV_FALLBACK=1`` -> deploy with the form value, tagged + ERROR."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.0})
    captured = _patch_subprocess(monkeypatch)
    monkeypatch.setenv(execution_service.NAV_FALLBACK_ENV, "1")

    with caplog.at_level(logging.ERROR):
        res = _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=None, portfolio_value=100000.0)

    assert len(captured) == 1
    assert _flag(captured[0], "--portfolio-value") == "100000.0"
    assert res["nav_source"] == "fallback_input"
    assert res["nav_computed"] is None  # nothing was computed -- say so, do not fake it
    assert any(w.startswith("nav_fallback_applied") for w in res["nav_warnings"])
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_stale_price_aborts_with_the_stale_class(workspace, monkeypatch):
    """A book marked on a day the cache has no bar for -> ``stale_price`` (not uninitialised)."""
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    _seed_cache(cache, "AAA", {"2026-01-05": 10.0}, {"2026-01-05": 1.0})  # no AS_OF bar
    captured = _patch_subprocess(monkeypatch)
    monkeypatch.delenv(execution_service.NAV_FALLBACK_ENV, raising=False)

    with pytest.raises(execution_service.NavValuationError) as excinfo:
        _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=0.0, portfolio_value=100000.0)

    assert captured == []
    assert excinfo.value.reason == "stale_price"
    assert "行情陈旧" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3b. cache_uninitialized vs stale_price -- the two failure FAMILIES
# ---------------------------------------------------------------------------
def test_missing_cache_file_is_reported_as_uninitialised_not_stale(workspace, monkeypatch):
    """The live cache was DELETED. A read-only open must not create it, and the message must
    name the Stage-1 refresh, never the stale-price story."""
    captured = _patch_subprocess(monkeypatch)
    monkeypatch.delenv(execution_service.NAV_FALLBACK_ENV, raising=False)
    db_file = os.path.join(str(workspace["dirs"]["cache"]), "cache.duckdb")

    with pytest.raises(execution_service.NavValuationError) as excinfo:
        _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=1000.0, portfolio_value=100000.0)

    assert captured == []
    err = excinfo.value
    assert err.reason == "cache_uninitialized"
    text = str(err)
    assert "缓存库为空或未初始化" in text
    assert "Stage-1 数据刷新" in text
    # Never the stale-price wording: the cure is a data refresh, not waiting for a close.
    assert "行情陈旧" not in text
    assert any(w.startswith("reason=cache_uninitialized") for w in err.nav_warnings)
    # ...and nothing was created on disk by the failed deploy (read-only open, by design).
    assert not os.path.exists(db_file), f"a refused deploy must not create {db_file}"


def test_cache_with_zero_bars_is_uninitialised(workspace, monkeypatch):
    """An auto-created (``_init_tables``) cache has the tables but ZERO rows.

    Row count -- not exception sniffing -- is what distinguishes "nobody ever synced" from
    "this symbol has no data": `query_bars` answers both with an empty frame.
    """
    DuckDBCache(db_path=str(workspace["dirs"]["cache"]))  # creates the file + empty tables
    captured = _patch_subprocess(monkeypatch)
    monkeypatch.delenv(execution_service.NAV_FALLBACK_ENV, raising=False)

    with pytest.raises(execution_service.NavValuationError) as excinfo:
        _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=1000.0, portfolio_value=100000.0)

    assert captured == []
    assert excinfo.value.reason == "cache_uninitialized"
    assert "ohlcv_bars_empty" in str(excinfo.value)


def test_cache_without_adjust_factors_table_is_uninitialised(workspace, monkeypatch):
    """A cache that predates ``adjust_factors`` (the real production shape, 2026-10) must NOT
    silently degrade to RAW marking -- that is the dividend-dropping defect itself."""
    import duckdb

    db_file = os.path.join(str(workspace["dirs"]["cache"]), "cache.duckdb")
    con = duckdb.connect(db_file)
    con.execute(
        "CREATE TABLE ohlcv_bars (provider VARCHAR NOT NULL, symbol VARCHAR NOT NULL, "
        "date DATE NOT NULL, open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, "
        "close DOUBLE NOT NULL, volume DOUBLE NOT NULL, amount DOUBLE, "
        "PRIMARY KEY (provider, symbol, date))"
    )
    con.execute(
        "INSERT INTO ohlcv_bars VALUES (?, 'AAA', '2026-01-06', 10, 10, 10, 10, 1000, NULL)",
        [PROVIDER],
    )
    con.close()

    captured = _patch_subprocess(monkeypatch)
    monkeypatch.delenv(execution_service.NAV_FALLBACK_ENV, raising=False)

    with pytest.raises(execution_service.NavValuationError) as excinfo:
        _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=0.0, portfolio_value=100000.0)

    assert captured == []
    assert excinfo.value.reason == "cache_uninitialized"
    assert "adjust_factors_table_missing" in str(excinfo.value)
    assert "缓存库为空或未初始化" in str(excinfo.value)

    # With the opt-in it degrades -- but it is LABELLED as a fallback, never reported as a
    # successful computed mark.
    monkeypatch.setenv(execution_service.NAV_FALLBACK_ENV, "1")
    captured_ok = _patch_subprocess(monkeypatch)
    res = _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=0.0, portfolio_value=100000.0)
    assert len(captured_ok) == 1
    assert res["nav_source"] == "fallback_input"
    assert res["nav_computed"] is None


# ---------------------------------------------------------------------------
# 4. artifacts -- per-symbol detail in its own file, scalars in summary.json
# ---------------------------------------------------------------------------
def test_nav_breakdown_file_holds_the_per_symbol_mark(workspace, monkeypatch):
    _seed_two_bar_book(workspace)
    captured = _patch_subprocess(monkeypatch)

    _run(workspace, monkeypatch, current_holdings={"AAA": 100}, cash=1000.0, portfolio_value=100000.0)

    run_dir = _flag(captured[0], "--output-dir")
    path = os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME)
    assert os.path.exists(path)

    with open(path, "r", encoding="utf-8") as f:
        breakdown = json.load(f)

    entry = breakdown["per_symbol"]["AAA"]
    for key in ("shares", "raw_close", "factor_hfq", "anchor_date", "value", "stale",
                "value_raw", "anchor_inflation_ratio", "dividend_basis"):
        assert key in entry, (key, entry)
    assert entry["shares"] == 100
    assert entry["raw_close"] == pytest.approx(10.0)
    # No archived open date in this workspace => RAW (a lower bound), never the earliest
    # factor row.  The stored factor is 1.0 on both bars, so the mark is 1,000 either way;
    # what matters is that NO anchor is claimed and the basis says "raw".
    assert entry["factor_hfq"] is None
    assert entry["anchor_date"] is None
    assert entry["value"] == pytest.approx(1000.0)
    assert entry["value_raw"] == pytest.approx(1000.0)
    assert entry["anchor_inflation_ratio"] == pytest.approx(1.0)
    assert entry["dividend_basis"] == "raw"
    assert entry["stale"] is False
    assert breakdown["anchor_source"] is None
    assert breakdown["anchors"] == {}
    assert breakdown["nav_computed"] == pytest.approx(2000.0)
    assert breakdown["cash"] == pytest.approx(1000.0)


def test_summary_json_carries_scalars_but_never_the_20_symbol_detail(workspace, monkeypatch):
    """A 20-symbol book: summary.json stays a digest (pointer + count), detail lives elsewhere."""
    symbols = [f"T{i:02d}" for i in range(20)]
    cache = DuckDBCache(db_path=str(workspace["dirs"]["cache"]))
    for sym in symbols:
        _seed_cache(cache, sym, {"2026-01-05": 10.0, AS_OF: 10.0}, {"2026-01-05": 1.0, AS_OF: 1.0})
    captured = _patch_subprocess(monkeypatch)

    _run(
        workspace,
        monkeypatch,
        current_holdings={sym: 100 for sym in symbols},
        custom_symbols=symbols,
        cash=0.0,
        portfolio_value=100000.0,
    )

    run_dir = _flag(captured[0], "--output-dir")
    with open(os.path.join(run_dir, "summary.json"), "r", encoding="utf-8") as f:
        summary = json.load(f)

    for key in ("nav_computed", "nav_source", "cash", "cash_source", "nav_warnings"):
        assert key in summary, (key, sorted(summary))
    assert summary["nav_source"] == "computed"
    assert summary["nav_computed"] == pytest.approx(20000.0)
    assert summary["cash"] == pytest.approx(0.0)
    assert summary["cash_source"] == "explicit"
    assert summary["nav_symbol_count"] == 20
    assert summary["nav_breakdown_file"] == execution_service.NAV_BREAKDOWN_FILENAME
    # The digest must NOT carry the per-symbol blocks...
    assert "per_symbol" not in summary
    assert not any(sym in summary for sym in symbols), "summary.json leaked per-symbol detail"

    # ...while the breakdown file does, one entry per holding.
    with open(os.path.join(run_dir, execution_service.NAV_BREAKDOWN_FILENAME), "r", encoding="utf-8") as f:
        breakdown = json.load(f)
    assert set(breakdown["per_symbol"]) == set(symbols)


# ---------------------------------------------------------------------------
# 5. web models declare the new fields (`pydantic` is absent from this venv)
# ---------------------------------------------------------------------------
def test_models_declare_cash_and_nav_fields():
    """Checked on the AST, not by importing: `models.py` needs pydantic, which the shared
    `pipeline` venv does not have. FastAPI's `response_model` FILTERS undeclared keys, so a
    missing declaration would silently drop the provenance badge from the UI."""
    path = os.path.join(_WEB_DIR, "models.py")
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    annotations = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            annotations[node.name] = {
                t.target.id
                for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
            }
    assert "cash" in annotations["HoldingsUpdateModel"]
    assert {"cash", "nav_override"} <= annotations["RunDeployRequest"]
    assert {"nav_computed", "nav_source", "cash_source", "nav_warnings"} <= annotations["RunResponse"]

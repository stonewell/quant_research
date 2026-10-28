"""C6 regression tests: the web layer must not invent the valuation date, and a
gate refusal must be reported in words an operator can act on.

FORENSIC CONTEXT (L2.3 review, defect C6)
-----------------------------------------
``execution_service`` used to push ``--as-of-date=<date.today()>`` at Stage 1/2 and
the multi-strategy netter. ``date.today()`` is the *host's* calendar day, so:

* during the session it named a daily bar that does not exist yet, and the new
  market-clock gate (``common/cli_utils.py::resolve_live_clock_context``) rejected it
  with ``--as-of-date ... 晚于市场时钟公布的最近已收盘交易日 ...``;
* on a weekend / holiday it named a non-trading day and the gate rejected it with
  ``--as-of-date ... 不是 china 的交易日``;
* while the UI only ever showed ``Execution failed with code 1``, because the real
  reason lived in the child's ``stderr`` traceback.

The tests below pin all four halves of the fix (omit-by-default, forward-an-explicit
replay verbatim, run-directory date from the clock, actionable + non-swallowing
failure text). Discrimination was measured, not assumed: swapping the pre-fix
``date.today()``/``--as-of-date`` logic back in makes 11 of these tests fail
(only the pure-helper classification test and the success-path preservation guard
still pass). ``--as-of-date today`` appears in the argv and ``message`` degenerates
to ``Execution failed with code N`` under the old implementation.

Everything is offline: ``subprocess.run`` is always monkeypatched (the real live
runners are never launched) and the ``temp_workspace`` fixture keeps every writable
path under the OS temp root (``conftest.py``'s autouse hermetic fixture).
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from execution_service import ExecutionService, summarize_execution_failure
from storage import StorageManager


@pytest.fixture
def temp_workspace():
    """Hermetic workspace: every writable path under the OS temp root.

    ``cache_dir`` is pinned explicitly as well, so the production
    ``<repo_root>/data/cache.duckdb`` is unreachable even when this module is run on
    its own (the package autouse fixture in ``conftest.py`` is the second layer).
    """
    temp_dir = tempfile.mkdtemp()
    data_dir = os.path.join(temp_dir, "data")
    archive_dir = os.path.join(temp_dir, "runs")
    cache_dir = os.path.join(temp_dir, "duckdb_cache")
    settings_dir = os.path.join(temp_dir, "settings")
    for d in (data_dir, archive_dir, cache_dir, settings_dir):
        os.makedirs(d, exist_ok=True)

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=data_dir,
        archive_dir=archive_dir,
        cache_dir=cache_dir,
        settings_dir=settings_dir,
    )
    yield storage, temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, returncode=0, stdout="", stderr=""):
    """Capture argv and never launch a real process."""
    captured = []

    def _run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _MockProc(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _patch_subprocess_writing(monkeypatch, artifacts=None, returncode=0, stdout="", stderr=""):
    """Capture argv and materialise the child's artifacts under its `--output-dir`.

    The real runners write `stage1_health_report.json` / `summary.json` themselves; the
    mocked child must do the same for the post-processing under test (L2.4's
    observability merge / 5m reconciliation) to have anything to read.
    """
    captured = []

    def _run(cmd, *args, **kwargs):
        cmd_list = list(cmd)
        captured.append(cmd_list)
        if "--output-dir" in cmd_list:
            run_dir = cmd_list[cmd_list.index("--output-dir") + 1]
            os.makedirs(run_dir, exist_ok=True)
            for name, payload in (artifacts or {}).items():
                with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
                    json.dump(payload, f)
        return _MockProc(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _run)
    return captured


def _arg_value(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


#: Verbatim shape of the child's traceback tail for the run-window gate.
_RUN_WINDOW_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "/repo/scripts/run_live_strategy.py", line 1172, in main\n'
    '    clock_ctx = resolve_live_clock_context(args, universe_symbols, stage="stage2")\n'
    '  File "/repo/common/cli_utils.py", line 557, in resolve_live_clock_context\n'
    "    verdict = assert_run_window(\n"
    "common.cli_utils.RunWindowError: [stage2] 运行窗口门禁拒绝 (RUN WINDOW GATE REFUSED): "
    "stage=stage2 market=china local_now=2026-10-23T10:05:00+08:00 session=open "
    "expected_window=14:10-15:00 (市场本地时间) as_of=2026-10-22。窗口外运行会把「此刻」当成实时。"
    "处理：在市场本地窗口内运行；或显式 --allow-intraday 放行盘中运行；"
    "或 --no-strict-market-clock 降级为警告。\n"
)

#: Verbatim shape of the child's traceback tail for the stale-price gate.
_STALE_PRICE_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "/repo/scripts/run_live_strategy.py", line 1665, in main\n'
    "    price = latest_close(universe, sym, as_of_date, strict=clock_ctx.freshness_strict)\n"
    "common.freshness.StalePriceError: 陈旧价格拒绝执行 (STALE PRICE): 标的 601872.SH 的最新收盘价 "
    "7.1234 来自 2026-10-21 的 K 线，但本次估值/交易会话为 2026-10-22。"
    " 修复方式：重新拉取该标的至 2026-10-22 的行情；若确属非交易日/数据源已知缺口的人工演练，"
    "可显式传入 --allow-stale-prices 将其降级为警告。\n"
)

#: The market-clock gate's as-of refusal (an explicit date the clock cannot honour).
_AS_OF_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "/repo/common/cli_utils.py", line 522, in resolve_live_clock_context\n'
    "    raise ValueError(\n"
    "ValueError: --as-of-date 2026-10-23 晚于市场时钟公布的最近已收盘交易日 2026-10-22 "
    "(market=china local=2026-10-23T10:05:00+08:00 session=open)。"
    "该交易日的收盘价尚未收盘/尚未发布，拒绝用未完成的日线 bar 估值。\n"
)


@pytest.fixture
def service(temp_workspace):
    storage, temp_dir = temp_workspace
    return ExecutionService(storage=storage), storage, temp_dir


# --------------------------------------------------------------------------- #
# 1. argv: no `--as-of-date` unless the operator pinned a session
# --------------------------------------------------------------------------- #
def test_all_three_commands_omit_as_of_date_by_default(service, monkeypatch):
    """GUARD (C6): live runs must let the child resolve as-of from the market clock.

    Old behaviour: ``--as-of-date today`` / ``--as-of-date 2026-10-23`` (the host's
    ``date.today()``) was always appended, which the new gate rejects intraday and on
    non-trading days.
    """
    svc, storage, temp_dir = service
    captured = _patch_subprocess(monkeypatch)

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    svc.run_health_check(strategy_file=strat_file, custom_symbols=["600519.SH"], portfolio_value=100000.0)
    # `nav_override=True`: this test pins the as-of-date plumbing, not the mark -- there is
    # no valuation cache in this workspace, so the engine path would (correctly) refuse.
    svc.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
        nav_override=True,
    )
    svc.run_multi_strategy_netted_deploy(
        strategies=[{"strategy_key": "chan_risk_managed_blend", "capital_weight": 1.0}],
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
    )

    assert len(captured) == 3, captured
    for cmd in captured:
        assert "--as-of-date" not in cmd, f"as-of must be clock-resolved by the child: {cmd}"
        assert not any(str(a).startswith("--as-of-date=") for a in cmd), cmd
        assert "today" not in cmd, cmd


def test_explicit_replay_as_of_is_forwarded_verbatim(service, monkeypatch):
    """A pinned session (historical replay) is still passed through, unchanged.

    Requirement 4 (the success path must not move) lives here: the child's own gate
    validates an explicit date, so the web forwards exactly what the operator asked
    for rather than substituting ``date.today()``.
    """
    svc, storage, temp_dir = service
    captured = _patch_subprocess(monkeypatch)

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    res = svc.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2024-05-10",
        portfolio_value=100000.0,
        # Marks the as-of-date plumbing, not the NAV (no valuation cache here).
        nav_override=True,
    )

    assert len(captured) == 1
    cmd = captured[0]
    assert _arg_value(cmd, "--as-of-date") == "2024-05-10"
    assert res["date"] == "2024-05-10"
    assert res["as_of_source"] == "explicit"
    assert _arg_value(cmd, "--output-dir").endswith(os.path.join("2024-05-10"))


# --------------------------------------------------------------------------- #
# 2. run-directory / archived date comes from the market clock
# --------------------------------------------------------------------------- #
def test_run_dir_date_comes_from_the_market_clock_not_the_host(service, monkeypatch):
    """GUARD (C6): `val_date` is the clock's published session, not `date.today()`.

    The clock is frozen to a fixed Thursday (2024-05-09) that can never coincide with
    the host's real "today", so the old ``date.today()`` implementation fails here.
    """
    import common.market_clock as market_clock_module

    svc, storage, _temp_dir = service
    captured = _patch_subprocess(monkeypatch)

    real_expected = market_clock_module.expected_last_session
    calls = []

    def _frozen(market="china", now=None, **kwargs):
        calls.append(market)
        return real_expected(
            market,
            now=dt.datetime(2024, 5, 10, 1, 0, tzinfo=dt.timezone.utc),  # Beijing 09:00 pre-open
            **kwargs,
        )

    monkeypatch.setattr(market_clock_module, "expected_last_session", _frozen)

    strat_file = os.path.join(_temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    res = svc.run_health_check(strategy_file=strat_file, custom_symbols=["600519.SH"], portfolio_value=100000.0)

    assert calls == ["china"], f"market inferred from the traded symbols: {calls}"
    assert res["date"] == "2024-05-09", res["date"]
    assert res["as_of_source"] == "market_clock"
    cmd = captured[0]
    out_dir = _arg_value(cmd, "--output-dir")
    assert out_dir.endswith(os.path.join("2024-05-09"))
    assert os.path.isdir(out_dir), out_dir
    assert _arg_value(cmd, "--account-state-file").startswith(out_dir)
    # The archive really was written under the clock's session, not "today".
    assert out_dir.startswith(_canonical(storage.archive_dir))

    # (L2.4) `summary.json` names the SAME session as the directory it sits in.
    with open(os.path.join(out_dir, "summary.json"), "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["as_of"] == "2024-05-09"
    assert summary["as_of"] == summary["date"] == os.path.basename(out_dir)
    assert summary["clock"]["as_of_trading_date"] == "2024-05-09"
    assert summary["clock"]["as_of_source"] == "market_clock"


def _canonical(path):
    return os.path.realpath(os.path.abspath(os.fspath(path)))


@pytest.mark.parametrize(
    "market, now_utc, expected",
    [
        # Beijing 10:05 Friday, mid-session => today's bar is unfinished.
        ("china", dt.datetime(2026, 10, 23, 2, 5, tzinfo=dt.timezone.utc), "2026-10-22"),
        # Beijing 15:35 Friday, close+30min publication grace elapsed => today.
        ("china", dt.datetime(2026, 10, 23, 7, 35, tzinfo=dt.timezone.utc), "2026-10-23"),
        # Beijing 15:29 Friday, inside the close..grace gap => still yesterday.
        ("china", dt.datetime(2026, 10, 23, 7, 29, tzinfo=dt.timezone.utc), "2026-10-22"),
        # Saturday => previous Friday.
        ("china", dt.datetime(2026, 10, 24, 4, 0, tzinfo=dt.timezone.utc), "2026-10-23"),
        # US 12:00 New York (16:00Z, EDT) mid-session Friday => Thursday 10-22.
        ("us", dt.datetime(2026, 10, 23, 16, 0, tzinfo=dt.timezone.utc), "2026-10-22"),
    ],
)
def test_val_date_follows_the_market_clock(service, monkeypatch, market, now_utc, expected):
    """`_market_clock_val_date` delegates tz/session/holiday semantics to the clock.

    The service is given a symbol from `market`, so market inference is exercised too
    (the clock is frozen, but it is the *real* `expected_last_session` that answers).
    """
    import common.market_clock as market_clock_module

    svc, _storage, _temp_dir = service
    real_expected = market_clock_module.expected_last_session

    def _frozen(mkt="china", now=None, **kwargs):
        return real_expected(mkt, now=now_utc, **kwargs)

    monkeypatch.setattr(market_clock_module, "expected_last_session", _frozen)

    symbols = {"china": ["600519.SH"], "us": ["AAPL"], "hk": ["0700.HK"]}[market]
    assert market_clock_module.infer_market_from_symbol(symbols[0]) == market

    assert svc._market_clock_val_date(custom_symbols=symbols) == expected


def test_cash_proxy_does_not_shift_the_valuation_date(service, monkeypatch):
    """A China book with a ``BIL`` cash leg must use the China calendar, not the US one.

    ``BIL`` infers as a US ticker; letting it vote would resolve a *cross-market* date
    (the earliest session), i.e. an off-by-one-day run directory for a China book.
    """
    import common.market_clock as market_clock_module

    svc, _storage, _temp_dir = service
    real_expected = market_clock_module.expected_last_session
    # Beijing 22:00 Friday 2026-10-23 (14:00Z): China's Friday close+grace is already
    # published (as_of=Fri 10-23) while the US session is still mid-day (as_of=Thu
    # 10-22), so an excluded-vs-not difference is visible.
    frozen = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)

    def _frozen(mkt="china", now=None, **kwargs):
        return real_expected(mkt, now=frozen, **kwargs)

    monkeypatch.setattr(market_clock_module, "expected_last_session", _frozen)

    assert real_expected("china", now=frozen).isoformat() == "2026-10-23"
    assert real_expected("us", now=frozen).isoformat() == "2026-10-22"
    # China book + BIL cash leg => China's calendar (10-23), never the US one (10-22).
    assert svc._market_clock_val_date(custom_symbols=["600519.SH", "BIL", "CASH"]) == "2026-10-23"


# --------------------------------------------------------------------------- #
# 3. Actionable, non-swallowing failure text
# --------------------------------------------------------------------------- #
def test_intraday_gate_refusal_is_actionable(service, monkeypatch):
    """GUARD (C6): an intraday refusal must READ as a refusal, not as `code 1`."""
    svc, storage, temp_dir = service
    captured = _patch_subprocess(monkeypatch, returncode=1, stderr=_RUN_WINDOW_STDERR)

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    # `nav_override=True`: the subject here is the CHILD's refusal text, not the mark --
    # this workspace has no valuation cache, so the engine path would refuse first.
    res = svc.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
        nav_override=True,
    )

    assert res["status"] == "error"
    assert res["error_kind"] == "run_window_gate"
    msg = res["message"]
    # Actionable: says WHAT happened and WHAT to do -- not just a numeric code.
    assert "运行窗口门禁" in msg
    assert "盘中" in msg
    assert "--allow-intraday" in msg
    assert "returncode=1" in msg
    # Not swallowed: the verbatim exception line + full raw stderr survive.
    assert "RUN WINDOW GATE REFUSED" in res["error_detail"]
    assert res["stderr"] == _RUN_WINDOW_STDERR
    assert "RunWindowError" in res["error_excerpt"]

    # And it is archived for post-mortem, next to the raw stderr.
    run_dir = _arg_value(captured[0], "--output-dir")
    summary = storage._load_json(os.path.join(run_dir, "summary.json"), default={})
    assert summary["deploy_status"] == "FAILED"
    assert summary["error_kind"] == "run_window_gate"
    assert summary["deploy_stderr"] == _RUN_WINDOW_STDERR
    assert "运行窗口门禁" in summary["error_headline"]


def test_stale_price_refusal_names_the_staleness(service, monkeypatch):
    svc, storage, temp_dir = service
    _patch_subprocess(monkeypatch, returncode=1, stderr=_STALE_PRICE_STDERR)

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    # `nav_override=True`: pins the child's stale-price refusal plumbing, not the mark.
    res = svc.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        portfolio_value=100000.0,
        nav_override=True,
    )

    assert res["error_kind"] == "stale_prices"
    assert "陈旧" in res["message"]
    assert "price_stale" in res["message"]  # the documented emergency override
    assert "2026-10-21" in res["error_detail"]  # the bar it stopped at is quoted
    assert res["stderr"] == _STALE_PRICE_STDERR


def test_as_of_rejection_is_actionable(service, monkeypatch):
    svc, storage, temp_dir = service
    _patch_subprocess(monkeypatch, returncode=1, stderr=_AS_OF_STDERR)

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    res = svc.run_health_check(strategy_file=strat_file, custom_symbols=["600519.SH"], portfolio_value=100000.0)

    assert res["error_kind"] == "as_of_not_honourable"
    assert "估值日被拒" in res["message"]
    assert "前一交易日" in res["message"]
    assert "2026-10-22" in res["error_detail"]


def test_unknown_failure_still_surfaces_the_raw_error():
    """No rule may match => still readable, still raw: never silently swallowed."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/repo/scripts/run_live_strategy.py", line 1, in main\n'
        "KeyError: 'holdings.json is missing the BIL row'\n"
    )
    failure = summarize_execution_failure(stderr, returncode=7, label="Stage 2")

    assert failure["error_kind"] == "unknown"
    assert "returncode=7" in failure["error_message"]
    assert "'holdings.json is missing the BIL row'" in failure["error_message"]
    assert "STDERR" in failure["error_message"]  # tells the operator where to look
    assert failure["error_detail"].endswith("'holdings.json is missing the BIL row'")
    assert failure["error_excerpt"].startswith("Traceback")


def test_success_path_is_untouched(service, monkeypatch):
    """Requirement 4: a clean exit keeps the old message/status/argv contract."""
    svc, storage, temp_dir = service
    captured = _patch_subprocess(monkeypatch, returncode=0, stdout="[SUCCESS] done", stderr="")

    strat_file = os.path.join(temp_dir, "strat.json")
    storage._save_json(strat_file, {"template_name": "dummy", "params": {}})

    # `nav_override=True`: keeps `--portfolio-value` equal to the form field, which is what
    # this test asserts. The computed-mark path is pinned by `pipeline/tests/
    # test_live_nav_computed.py`; here the subject is the child-exit success contract.
    res = svc.run_live_deploy(
        strategy_file=strat_file,
        custom_symbols=["600519.SH"],
        as_of_date="2024-05-10",
        portfolio_value=100000.0,
        nav_override=True,
    )

    assert res["status"] == "success"
    assert res["message"] == "Stage 2 Live Deployment executed successfully"
    assert "error_kind" not in res  # no failure block on the success path
    assert "error_message" not in res
    assert _arg_value(captured[0], "--portfolio-value") == "100000.0"

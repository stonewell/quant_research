"""`L2.3` -- live-entry as-of decoupling, run-window gate and hard freshness assertion.

The 2026-10-22 incident had two near-causes (design §2):
  (a) no freshness assertion anywhere: six symbols' daily bars stopped on 2026-10-21
      while `as_of_date` was 2026-10-22, and they were still marked to / traded on;
  (b) no run-window gate: a run designed for the A-share closing window executed at
      Beijing 10:05 and nothing compared "now" against a market session.

`L2.1`/`L2.2`/`L2.7` built the clock, the cache's `expected_last_session` contract and
`common/freshness.py`, and an independent review confirmed those were correct -- but NO
live script passed `expected_last_session`, so in production the 7-day tolerant legacy
path was still the one running. This file pins the wiring itself:

* the as-of date comes from `common/market_clock` (design §5 decision table), never the
  host's `date.today()`;
* an intraday run outside the market's window is REFUSED, and `--allow-intraday` is the
  documented override (design §8/§14 row 2 -- see `common/cli_utils.py` module docstring
  for how those two sections are reconciled);
* a stale bar aborts the run with `StalePriceError` instead of quietly pricing the book at
  an old close;
* `summary.json` / the Stage-1 report carry the §9 observability block;
* a STATIC guardrail fails if anyone deletes the `expected_last_session=` pass-through
  from a live entrypoint (i.e. this cannot silently regress to the 2026-10-22 behaviour).

100% offline: synthetic provider, frozen clock instants (no `freezegun`), and every write
lands in pytest's `tmp_path`.
"""

import ast
import contextlib
import datetime as dt
import io
import json
import os
import sys
import time
import tokenize
from zoneinfo import ZoneInfo

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

import common.cli_utils as cli_utils
from common.cli_utils import RunWindowError, load_universe_with_banner
from common.freshness import StalePriceError
from common.market_clock import MarketClock

# --------------------------------------------------------------------------- #
# Frozen instants (design §12 cases 1-7, 9). Host timezone is irrelevant: every
# expectation below is stated in the MARKET's local clock.
# --------------------------------------------------------------------------- #
_UTC = dt.timezone.utc

#: Case 1: Beijing 09:59 on 2026-10-23 -> intraday (OPEN), as_of = 2026-10-22.
_BEIJING_0959 = dt.datetime(2026, 10, 23, 1, 59, tzinfo=_UTC)
#: Case 3: Beijing 15:30 on 2026-10-23 -> close + 30min grace published, as_of = 2026-10-23.
_BEIJING_1530 = dt.datetime(2026, 10, 23, 7, 30, tzinfo=_UTC)
#: Case 2: Beijing 15:05 on 2026-10-23 -> closed but NOT published, as_of = 2026-10-22.
_BEIJING_1505 = dt.datetime(2026, 10, 23, 7, 5, tzinfo=_UTC)
#: Case 4: Saturday 2026-10-24 (Beijing noon) -> NON_TRADING_DAY, as_of = Friday 10-23.
_SATURDAY = dt.datetime(2026, 10, 24, 4, 0, tzinfo=_UTC)
#: Case 6: US 06:00 PT = 09:00 ET on 2026-10-23 -> PRE_OPEN, as_of = 2026-10-22.
_US_PREOPEN = dt.datetime(2026, 10, 23, 13, 0, tzinfo=_UTC)

_AS_OF_INTRADAY = "2026-10-22"
_AS_OF_PUBLISHED = "2026-10-23"


def _freeze(monkeypatch, instant):
    """Freeze every live script's clock at `instant` (the single wall-clock read).

    Production calls `common.cli_utils.resolve_live_clock_context()` which passes
    `now=None` into `market_now()`; replacing that one symbol is enough to pin all three
    entrypoints, and it cannot leak into a backtest (which never calls it at all).
    """
    real_market_now = cli_utils.market_now

    def _frozen(market, now=None, holidays=None, publication_grace_minutes=None):
        return real_market_now(
            market,
            now=now if now is not None else instant,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(cli_utils, "market_now", _frozen)


def _write_universe(tmp_path, symbols):
    path = str(tmp_path / "universe.txt")
    with open(path, "w") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _stage2_args(tmp_path, as_of=None, extra=None):
    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    cmd = [
        "--strategy", "equal_weight",
        "--universe-file", universe_file,
        "--output-dir", str(tmp_path),
        "--start-date", "2026-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--no-cache",
    ]
    if as_of:
        cmd.extend(["--as-of-date", as_of])
    if extra:
        cmd.extend(extra)
    return cmd


# --------------------------------------------------------------------------- #
# Case 1 / 2 / 3 / 4 -- as-of resolution and the intraday refusal
# --------------------------------------------------------------------------- #
def test_intraday_run_is_refused_by_default(monkeypatch, tmp_path):
    """Case 1: Beijing 09:59 is OPEN -> refusal, and the message must be actionable.

    This is the 2026-10-22 shape (intraday, hours before the designed window). The gate
    must name the stage, market, market-local time, session state and expected window --
    "not in window" alone tells an operator nothing.
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_0959)
    with pytest.raises(RunWindowError) as excinfo:
        live_main(_stage2_args(tmp_path, _AS_OF_INTRADAY))

    msg = str(excinfo.value)
    assert "stage2" in msg
    assert "market=china" in msg
    assert "2026-10-23T09:59:00+08:00" in msg  # market-local, not host-local
    assert "session=open" in msg
    assert "14:10-15:00" in msg
    assert "--allow-intraday" in msg
    # Nothing was produced: the gate runs before any data load.
    assert not (tmp_path / "live_trading_ticket.csv").exists()


def test_allow_intraday_runs_but_values_at_previous_published_session(monkeypatch, tmp_path, capsys):
    """Case 1 continued: `--allow-intraday` passes the gate, as_of stays 2026-10-22.

    Today's bar is unfinished, so the valuation session MUST be the previous published
    one. `summary.json` records the whole decision.
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_0959)
    order_df = live_main(
        _stage2_args(tmp_path, _AS_OF_INTRADAY, extra=["--allow-intraday"])
    )
    assert order_df is not None and not order_df.empty

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    clock = summary["clock"]
    assert clock["market"] == "china"
    assert clock["session"] == "open"
    assert clock["as_of_trading_date"] == _AS_OF_INTRADAY
    assert clock["expected_last_session"] == _AS_OF_INTRADAY
    assert clock["run_window_gate"] == "intraday_allowed"
    assert clock["allow_intraday"] is True


def test_pre_open_us_book_resolves_previous_session(monkeypatch, tmp_path):
    """Case 6: US 09:00 ET PRE_OPEN -> as_of is 10-22 (the host's `date.today()` said 10-23).

    The window gate treats PRE_OPEN as outside the window, so the run is refused by
    default -- but the REFUSAL message must already carry the correct as_of, and
    `--allow-intraday` must then value at 10-22, not 10-23.
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _US_PREOPEN)
    universe_file = _write_universe(tmp_path, ["SPY", "QQQ", "BIL"])
    base = [
        "--strategy", "equal_weight",
        "--universe-file", universe_file,
        "--output-dir", str(tmp_path),
        "--start-date", "2026-01-01",
        "--data-provider", "synthetic",
        "--no-cache",
    ]
    with pytest.raises(RunWindowError) as excinfo:
        live_main(base + ["--as-of-date", _AS_OF_INTRADAY])
    assert "market=us" in str(excinfo.value)
    assert "session=pre_open" in str(excinfo.value)

    live_main(base + ["--as-of-date", _AS_OF_INTRADAY, "--allow-intraday"])
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]
    assert clock["market"] == "us"
    assert clock["as_of_trading_date"] == _AS_OF_INTRADAY
    assert clock["local_now"].endswith("-04:00")  # DST-aware, from tzdata


def test_default_as_of_comes_from_the_market_clock_not_the_host(monkeypatch, tmp_path):
    """Case 3: Beijing 15:30 -> as_of = 2026-10-23 with NO `--as-of-date` at all.

    That is the whole point of the decoupling: the caller supplies nothing and still gets
    a market-correct valuation session.
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)
    live_main(_stage2_args(tmp_path))
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]
    assert clock["as_of_source"] == "market_clock"
    assert clock["as_of_trading_date"] == _AS_OF_PUBLISHED
    assert clock["session"] == "closed"
    assert clock["run_window_gate"] == "not_applicable"


def test_weekend_as_of_resolves_to_previous_friday_and_is_allowed(monkeypatch, tmp_path):
    """Case 4: Saturday -> NON_TRADING_DAY, as_of = Friday 10-23, window not applicable."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _SATURDAY)
    live_main(_stage2_args(tmp_path))
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]
    assert clock["session"] == "non_trading_day"
    assert clock["as_of_trading_date"] == _AS_OF_PUBLISHED  # Friday, not Saturday
    assert clock["run_window_gate"] == "not_applicable"


def test_published_grace_boundary_decides_today_vs_yesterday(monkeypatch, tmp_path):
    """Case 2 vs 3: 15:05 (unpublished) keeps as_of on 10-22; 15:30 moves it to 10-23."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1505)
    live_main(_stage2_args(tmp_path))
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]
    assert clock["as_of_trading_date"] == _AS_OF_INTRADAY
    assert clock["publication_grace_minutes"] == 30


# --------------------------------------------------------------------------- #
# as-of override validation
# --------------------------------------------------------------------------- #
def test_explicit_as_of_later_than_published_session_is_rejected(monkeypatch, tmp_path):
    """`--as-of-date` is an override, not a licence to value an unpublished bar."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1505)  # as_of = 10-22, today's close not published
    with pytest.raises(ValueError) as excinfo:
        live_main(_stage2_args(tmp_path, _AS_OF_PUBLISHED))
    msg = str(excinfo.value)
    assert _AS_OF_PUBLISHED in msg and _AS_OF_INTRADAY in msg
    assert "尚未收盘/尚未发布" in msg


def test_explicit_as_of_on_a_non_trading_day_is_rejected(monkeypatch, tmp_path):
    """A weekend/holiday pin would anchor the whole lookback window off-calendar."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)
    with pytest.raises(ValueError) as excinfo:
        live_main(_stage2_args(tmp_path, "2026-10-24"))  # Saturday
    assert "不是 china 的交易日" in str(excinfo.value)


def test_explicit_earlier_as_of_is_a_labelled_replay(monkeypatch, tmp_path):
    """A pinned historical session is a replay: window gate not applicable, and said so."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_0959)  # intraday: a live-time run would be refused
    live_main(_stage2_args(tmp_path, "2026-10-20"))
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]
    assert clock["run_window_gate"] == "not_applicable_replay"
    assert clock["as_of_trading_date"] == "2026-10-20"
    assert clock["expected_last_session"] == "2026-10-20"


# --------------------------------------------------------------------------- #
# Case 9 -- the 2026-10-22 six-symbol staleness regression, end to end
# --------------------------------------------------------------------------- #
def _drop_last_bar(symbols):
    """Provider patch: these symbols' series stop one session short (the incident shape)."""
    from common.data import SyntheticDataProvider

    orig_fetch = SyntheticDataProvider.fetch_ohlcv
    targets = set(symbols)

    def mock_fetch(self, sym, start, end, interval="1d"):
        df = orig_fetch(self, sym, start, end, interval)
        if sym in targets and len(df) >= 2:
            df = df.iloc[:-1]
        return df

    return mock_fetch


def test_stale_bar_aborts_stage2_with_stale_price_error(monkeypatch, tmp_path):
    """A bar older than the as-of session => StalePriceError, never "valued at yesterday"."""
    from unittest.mock import patch

    from common.data import SyntheticDataProvider
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)  # as_of = 10-23
    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _drop_last_bar(["600000.SH"])):
        with pytest.raises(StalePriceError) as excinfo:
            live_main(_stage2_args(tmp_path))

    msg = str(excinfo.value)
    assert "600000.SH" in msg
    assert "2026-10-22" in msg  # the bar it would have used
    assert _AS_OF_PUBLISHED in msg  # the session it was supposed to be
    assert "FRESHNESS FAIL" in msg
    # The ticket was never produced from the stale close.
    assert not (tmp_path / "live_trading_ticket.csv").exists()


def test_no_strict_freshness_downgrades_and_records_the_failure(monkeypatch, tmp_path, capsys):
    """`--no-strict-freshness` continues but `freshness_summary` still says FAIL."""
    from unittest.mock import patch

    from common.data import SyntheticDataProvider
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)
    with patch.object(SyntheticDataProvider, "fetch_ohlcv", _drop_last_bar(["600000.SH"])):
        live_main(_stage2_args(tmp_path, extra=["--no-strict-freshness"]))

    out = capsys.readouterr().out
    assert "FRESHNESS" in out
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    fresh = summary["freshness_summary"]
    assert fresh["status"] == "FAIL"
    assert fresh["strict"] is False
    assert [row["symbol"] for row in fresh["stale"]] == ["600000.SH"]
    assert fresh["price_bar_dates"]["600000.SH"] == "2026-10-22"
    assert fresh["price_bar_dates"]["601857.SH"] == _AS_OF_PUBLISHED


# --------------------------------------------------------------------------- #
# Design §9 -- observability fields
# --------------------------------------------------------------------------- #
def test_summary_json_carries_every_required_clock_field(monkeypatch, tmp_path):
    """tz-aware UTC + market-local + session + as_of + freshness_summary, all present."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)
    live_main(_stage2_args(tmp_path))
    clock = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["clock"]

    for key in (
        "utc_now",
        "local_now",
        "session",
        "as_of_trading_date",
        "expected_last_session",
        "market",
        "market_inference",
        "run_window",
        "run_window_gate",
        "strict_freshness",
        "publication_grace_minutes",
        "max_probe_attempts",
        "calendar_status",
    ):
        assert key in clock, f"missing §9 field {key}"
    # Absolutes: UTC anchor and a market-local anchor carrying its own offset.
    assert clock["utc_now"].endswith("+00:00")
    assert clock["local_now"].endswith("+08:00")
    assert dt.datetime.fromisoformat(clock["utc_now"]).astimezone(
        ZoneInfo("Asia/Shanghai")
    ) == dt.datetime.fromisoformat(clock["local_now"])

    fresh = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["freshness_summary"]
    assert fresh["status"] == "PASS"
    assert fresh["expected_session"] == _AS_OF_PUBLISHED
    # The cash proxy is priced at a flat 1.0 and is deliberately NOT part of the
    # bar-freshness verdict (its frame may legitimately be absent), so the summary
    # covers exactly the equities whose closes actually move NAV.
    assert set(fresh["price_bar_dates"]) == {"600000.SH", "601857.SH"}


def test_stage1_report_carries_clock_and_freshness(monkeypatch, tmp_path):
    """Stage 1 persists the same clock/freshness block into its health report."""
    health = _load_health_module()

    _freeze(monkeypatch, _BEIJING_1530)
    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    out_dir = str(tmp_path / "reports")
    health["main"](
        [
            "--universe-file", universe_file,
            "--output-dir", out_dir,
            "--start-date", "2026-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", "100000",
            "--peak-nav", "100000",
            "--no-save-state",
        ]
    )

    report = json.loads((tmp_path / "reports" / "stage1_health_report.json").read_text(encoding="utf-8"))
    assert report["as_of_date"] == _AS_OF_PUBLISHED
    assert report["clock"]["market"] == "china"
    assert report["clock"]["session"] == "closed"
    assert report["freshness_summary"]["status"] == "PASS"
    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["clock"]["as_of_trading_date"] == _AS_OF_PUBLISHED
    assert summary["stage"] == "stage1"


def _load_health_module():
    import importlib.util

    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    spec = importlib.util.spec_from_file_location("check_live_portfolio_health_l23", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return vars(module)


# --------------------------------------------------------------------------- #
# Static anti-regression guardrail (design §11 / L2.3 item 7)
# --------------------------------------------------------------------------- #
#: Every LIVE price/data entrypoint whose load calls must carry an explicit session.
_LIVE_ENTRY_FILES = (
    "scripts/run_live_strategy.py",
    "scripts/check_live_portfolio_health.py",
    "scripts/run_live_multi_strategy.py",
    "web/livetrading/storage.py",
)

#: Call names that reach the cache/data layer. `load_live_universe` receives the clock
#: CONTEXT instead (`clock_ctx=`), and that argument is mandatory -- dropping it is a
#: TypeError at runtime, so it can never silently fall back to the legacy path.
_LOAD_CALL_NAMES = frozenset(
    {
        "load_universe",
        "load_universe_with_banner",
        "load_ohlcv",
        "load_live_universe",
        "fetch_ohlcv",
        "fetch_universe",
    }
)
_SESSION_KWARGS = frozenset({"expected_last_session", "clock_ctx"})


def _unwired_load_calls(source, filename="<src>"):
    """Every data-load call in `source` that does NOT pin a freshness expectation.

    Returned as `(lineno, func_name)` pairs so a failure names the exact site. This is
    the check that makes "someone deleted the pass-through" a test failure instead of a
    return to the 2026-10-22 behaviour.
    """
    tree = ast.parse(source, filename=filename)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _LOAD_CALL_NAMES:
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if not (_SESSION_KWARGS & set(kwargs)):
            offenders.append((node.lineno, name))
            continue
        value = kwargs.get("expected_last_session")
        if isinstance(value, ast.Constant) and value.value is None:
            offenders.append((node.lineno, name))
    return offenders


def test_static_guardrail_live_entries_always_pass_expected_last_session():
    """No live data load may rely on the default (`None`) freshness contract."""
    checked = 0
    for rel in _LIVE_ENTRY_FILES:
        path = os.path.join(_REPO_ROOT, rel)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        offenders = _unwired_load_calls(source, rel)
        assert not offenders, (
            f"{rel}: these data loads no longer pin an expected session "
            f"{offenders} -- a live run would silently fall back to the cache's legacy "
            "7-day tolerance, which is exactly the 2026-10-22 defect."
        )
        checked += 1
    assert checked == len(_LIVE_ENTRY_FILES)


def test_static_guardrail_detects_a_removed_pass_through():
    """Discrimination proof: deleting the kwarg from a real live file is detected.

    Guards against the check itself rotting into a no-op (e.g. by matching nothing).
    """
    rel = "scripts/run_live_strategy.py"
    path = os.path.join(_REPO_ROOT, rel)
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    assert _unwired_load_calls(source, rel) == []

    # Delete the `clock_ctx=` pass-through from the real `load_live_universe(...)` call.
    mutated = source.replace(
        "        clock_ctx=clock_ctx,\n",
        "",
    )
    assert mutated != source, "fixture drifted: the call shape changed, update this test"
    offenders = _unwired_load_calls(mutated, rel)
    # `load_live_universe(...)` loses `clock_ctx=`; the nested `load_universe_with_banner`
    # inside cli_utils is not scanned here -- what matters is that the removed pass-through
    # is reported rather than silently accepted.
    assert offenders, "static guardrail failed to notice a deleted expected_last_session"
    assert any("load_live_universe" == name for _line, name in offenders)


def test_backtest_side_never_passes_a_real_session():
    """The mirror-image guardrail: backtest/research code must only ever pass None."""
    for rel in (
        "backtester/run_backtest.py",
        "pipeline/research_strategy/run_research_strategy.py",
        "pipeline/strategy_generator/run_strategen.py",
        "pipeline/instrument_selection/run_screener.py",
        "pipeline/pattern_mining/run_pattern_mining.py",
    ):
        path = os.path.join(_REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        # `backtester/run_backtest.py` spells it through `backtest_expected_last_session()`,
        # which raises for any non-None value; everything else must simply not pass one.
        assert "expected_last_session=clock_ctx" not in source
        assert "market_now(" not in source or rel.startswith("pipeline/")


def test_backtest_guard_rejects_a_real_session():
    """`backtest_expected_last_session` is `None`-only by construction."""
    from backtester.run_backtest import backtest_expected_last_session

    assert backtest_expected_last_session() is None
    with pytest.raises(RuntimeError):
        backtest_expected_last_session(dt.date(2026, 10, 22))


# --------------------------------------------------------------------------- #
# Reconciliation sanity: the gate's two spellings agree, and cost nothing to compute
# --------------------------------------------------------------------------- #
def test_run_window_defaults_are_market_local():
    """`--cutoff`-style literals are gone: windows come from the market's own session."""
    assert cli_utils.market_run_window("china", "stage1") == (dt.time(13, 55), dt.time(14, 20))
    assert cli_utils.market_run_window("china", "stage2") == (dt.time(14, 10), dt.time(15, 0))
    assert cli_utils.market_run_window("hk", "stage2") == (dt.time(14, 10), dt.time(16, 0))
    assert cli_utils.market_run_window("us", "stage2") == (dt.time(14, 10), dt.time(16, 0))


def test_allow_stale_prices_and_no_strict_freshness_are_one_switch(monkeypatch):
    """Both spellings feed a single boolean, so no site can honour one and ignore the other."""
    _freeze(monkeypatch, _BEIJING_1530)
    clock = MarketClock(market="china", now=_BEIJING_1530)
    assert clock.as_of_trading_date() == dt.date(2026, 10, 23)

    class _Args:
        market = None
        as_of_date = None
        allow_intraday = False
        strict_market_clock = True
        publication_grace_minutes = None
        max_probe_attempts = 3
        strict_freshness = True
        allow_stale_prices = False

    # `BIL` is a cash proxy, so it must not make an A-share book look cross-market --
    # and the label records that inference excluded it.
    ctx = cli_utils.resolve_live_clock_context(_Args(), ["600000.SH", "BIL"], "stage2")
    assert ctx.freshness_strict is True
    assert ctx.market_inference == "inferred_excluding_cash_proxies"

    # An explicit `--market` is never re-labelled as an inference.
    explicit = _Args()
    explicit.market = "china"
    assert cli_utils.resolve_live_clock_context(
        explicit, ["600000.SH", "BIL"], "stage2"
    ).market_inference == "explicit"

    for attr in ("strict_freshness", "allow_stale_prices"):
        args = _Args()
        setattr(args, attr, False if attr == "strict_freshness" else True)
        ctx = cli_utils.resolve_live_clock_context(args, ["600000.SH"], "stage2")
        assert ctx.freshness_strict is False


def test_mixed_market_universe_requires_an_explicit_market(monkeypatch):
    """Two real markets and no `--market` is an error, not a guessed calendar (design §14.3)."""
    class _Args:
        market = None
        as_of_date = None
        allow_intraday = False
        strict_market_clock = True
        publication_grace_minutes = None
        max_probe_attempts = 3
        strict_freshness = True

    with pytest.raises(ValueError) as excinfo:
        cli_utils.resolve_live_clock_context(_Args(), ["600000.SH", "AAPL"], "stage2")
    assert "--market" in str(excinfo.value)


def test_load_universe_with_banner_forwards_expected_last_session(monkeypatch):
    """The shared banner wrapper must not swallow the argument (silent legacy fallback)."""
    seen = {}

    def _fake_load_universe(symbols, start, end, interval="1d", **kwargs):
        seen.update(kwargs)
        return {s: None for s in symbols}

    monkeypatch.setattr(cli_utils, "load_universe", _fake_load_universe)
    load_universe_with_banner(
        ["600000.SH"], "2026-01-01", "2026-10-22", expected_last_session=dt.date(2026, 10, 22)
    )
    assert seen.get("expected_last_session") == dt.date(2026, 10, 22)


# --------------------------------------------------------------------------- #
# L2.30 -- `--as-of-date` replay: loud banner + `run_mode` marker
# --------------------------------------------------------------------------- #
def _summary(tmp_path):
    return json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))


def test_replay_run_warns_and_marks_summary_run_mode(monkeypatch, tmp_path, capsys):
    """A pinned PAST session skips the run-window gate BY DESIGN -- and must say so.

    Same instant as `test_explicit_earlier_as_of_is_a_labelled_replay` (Beijing 09:59, i.e.
    a live-time run would be refused), so the only thing that lets this run produce a ticket
    is the replay semantics. The ticket must therefore be unmistakable: a WARN banner on the
    terminal plus `run_mode=replay` in the machine-readable archive.
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_0959)
    live_main(_stage2_args(tmp_path, "2026-10-20"))

    out = capsys.readouterr().out
    assert "REPLAY MODE" in out
    assert "run_mode=replay" in out
    assert "盘中手工回放仍能出工单" in out  # the exact operational caveat, not a vague hint

    summary = _summary(tmp_path)
    assert summary["run_mode"] == "replay"
    assert summary["date"] == "2026-10-20"
    assert summary["stage"] == "stage2"
    # The marker does not replace the existing clock block -- it is added next to it.
    assert summary["clock"]["run_window_gate"] == "not_applicable_replay"
    assert summary["clock"]["as_of_trading_date"] == "2026-10-20"
    # A replay still emits a real ticket: that is why the marker exists.
    assert (tmp_path / "live_trading_ticket.csv").exists()


def test_default_live_run_has_no_replay_marker(monkeypatch, tmp_path, capsys):
    """No `--as-of-date`: as-of is the clock's published session -> `run_mode=live`."""
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)  # 15:30 Beijing, as_of 2026-10-23
    live_main(_stage2_args(tmp_path))

    out = capsys.readouterr().out
    assert "REPLAY MODE" not in out
    summary = _summary(tmp_path)
    assert summary["run_mode"] == "live"
    assert summary["date"] == _AS_OF_PUBLISHED


def test_explicit_same_day_as_of_is_live_not_replay(monkeypatch, tmp_path, capsys):
    """An explicit `--as-of-date` EQUAL to the clock's session is still a live run.

    This is the shape the web layer passes; it must not be labelled a replay (bit-for-bit
    unchanged apart from the added `run_mode=live` key).
    """
    from scripts.run_live_strategy import main as live_main

    _freeze(monkeypatch, _BEIJING_1530)
    live_main(_stage2_args(tmp_path, _AS_OF_PUBLISHED))

    out = capsys.readouterr().out
    assert "REPLAY MODE" not in out
    summary = _summary(tmp_path)
    assert summary["run_mode"] == "live"
    assert summary["clock"]["run_window_gate"] == "not_applicable"
    assert summary["clock"]["as_of_source"] == "explicit"


def test_resolve_run_mode_agrees_with_the_gate_verdict():
    """One predicate, two spellings: the marker may never contradict `window_gate`."""

    def _ctx(gate, as_of, clock_as_of, source):
        return type(
            "Ctx",
            (),
            {
                "window_gate": gate,
                "as_of_date": as_of,
                "clock_as_of": clock_as_of,
                "as_of_source": source,
            },
        )()

    from scripts.run_live_strategy import resolve_run_mode

    # The gate's own replay verdict is authoritative.
    assert resolve_run_mode(_ctx("not_applicable_replay", "2026-10-20", "2026-10-23", "explicit")) == "replay"
    # The defensive second spelling (gate label renamed, same predicate).
    assert resolve_run_mode(_ctx("something_else", "2026-10-20", "2026-10-23", "explicit")) == "replay"
    # Same-day explicit, clock-derived, and in-window runs are all live.
    assert resolve_run_mode(_ctx("not_applicable", "2026-10-23", "2026-10-23", "explicit")) == "live"
    assert resolve_run_mode(_ctx("in_window", "2026-10-23", "2026-10-23", "market_clock")) == "live"
    # A future as-of never reaches the marker (the clock rejects it earlier), but even if it
    # did, it must not be reported as a replay of a past session.
    assert resolve_run_mode(_ctx("in_window", "2026-10-24", "2026-10-23", "explicit")) == "live"


# --------------------------------------------------------------------------- #
# L2.31 -- the 5 script-layer `date.today()` sites are MARKET-clock dates
# --------------------------------------------------------------------------- #
#: Host-vs-market discriminator: 2026-10-23T00:30Z is 17:30 THU 10-22 in Los Angeles
#: (UTC-7, PDT) but 08:30 FRI 10-23 in Shanghai. A host-calendar answer and the market
#: answer therefore differ by a full day -- exactly the off-by-one this task removes.
_L5_INSTANT = dt.datetime(2026, 10, 23, 0, 30, tzinfo=_UTC)
_L5_MARKET_DAY = dt.date(2026, 10, 23)          # market calendar day at the instant
_L5_MARKET_AS_OF = dt.date(2026, 10, 22)        # close+grace: today is PRE_OPEN -> prior day


# --------------------------------------------------------------------------- #
# TEST-ISOLATION CONTRACT (read before touching anything below)
# --------------------------------------------------------------------------- #
# `L2.31` needs "host TZ != market TZ", and the only honest way to express that is to
# mutate the process's `TZ` + `time.tzset()`. That is inherently GLOBAL state, so this
# module keeps every such mutation inside a restored context manager and every clock
# freeze inside `monkeypatch`:
#
#   * `_host_timezone(...)` -- a `@contextlib.contextmanager` whose `finally` restores the
#     previous `TZ` (or pops it) AND re-calls `time.tzset()`, so it is safe even on error;
#   * `_freeze(...)` / `_freeze_module_clock(...)` -- `monkeypatch.setattr`, which pytest
#     undoes per test. They are deliberately NOT `pytest.fixture(scope="module")` and
#     never write to a module-level variable.
#
# DO NOT replace any of these with a module/session-scoped fixture, a bare
# `os.environ["TZ"] = ...`, or `unittest.mock.patch` without `addCleanup`: a leaked TZ or
# a leaked frozen clock silently rewrites "what day is it" for every later test in the
# session (it is exactly the class of bug `L2.31` itself removes). If a future test needs
# a frozen clock, use `monkeypatch` so teardown is automatic.
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _host_timezone(tz_name):
    """Run a block with the process's local timezone set to `tz_name` (POSIX `TZ`).

    `date.today()` reads the host zone; setting `TZ` + `time.tzset()` is what makes the
    host-calendar answer observably wrong on a non-market host, so it is the only honest
    way to pin "host TZ != market TZ". Restored even on failure.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _freeze_module_clock(monkeypatch, module, instant):
    """Freeze `module.market_now` (the script modules import it by name)."""
    real = module.market_now

    def _frozen(market=None, now=None, holidays=None, publication_grace_minutes=None):
        return real(
            market,
            now=now if now is not None else instant,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(module, "market_now", _frozen)


def test_host_timezone_context_restores_even_on_exception():
    """The `TZ` mutation is process-global, so it MUST be undone -- including on error.

    A leaked `TZ` (or a leaked frozen clock) silently shifts "what day is it" for every
    later test in the session, which is the reported cross-file pollution class and the
    very bug `L2.31` removes one layer down. This pins the restore contract that makes
    `test_five_script_date_sites_follow_the_market_clock` safe to run before any other
    file. See this module's TEST-ISOLATION CONTRACT block.
    """
    before_env = os.environ.get("TZ")
    before_offset = dt.datetime.now().astimezone().utcoffset()

    with pytest.raises(RuntimeError):
        with _host_timezone("America/Los_Angeles"):
            assert dt.datetime.now().astimezone().utcoffset() == dt.timedelta(hours=-7)
            raise RuntimeError("boom")

    assert os.environ.get("TZ") == before_env
    assert dt.datetime.now().astimezone().utcoffset() == before_offset


@pytest.mark.parametrize(
    "host_tz,host_day",
    [
        ("America/Los_Angeles", dt.date(2026, 10, 22)),  # UTC-7: one day BEHIND the market
        ("Asia/Shanghai", dt.date(2026, 10, 23)),        # market zone: same day
    ],
)
def test_five_script_date_sites_follow_the_market_clock(host_tz, host_day, monkeypatch):
    """All 5 former `date.today()` decision points must answer with the MARKET date.

    Sites (development_plan.md `L2.31`):
      1. `run_live_strategy.resolve_exit_cooldown` -> holding/cooldown fallback as-of;
      2. `sync_china_5m_and_indices` `--end` default;
      3. `record_intraday_5m` EastMoney intraday fallback fetch window;
      4. `record_intraday_5m.cleanup_old_parquet_files` retention age;
      5. `record_intraday_5m.main` `{date}.parquet` key.
    """
    from common.market_clock import as_of_trading_date
    from scripts import record_intraday_5m as rec
    from scripts import run_live_strategy as rls
    from scripts import sync_china_5m_and_indices as sync
    import pandas as pd

    # Precondition of the test itself: the host calendar really does disagree on UTC-7.
    assert _L5_INSTANT.astimezone(ZoneInfo(host_tz)).date() == host_day

    with _host_timezone(host_tz):
        clock_as_of = as_of_trading_date(now=_L5_INSTANT, market="china")
        assert clock_as_of == _L5_MARKET_AS_OF

        # --- site 1: cooldown/tenure fallback -------------------------------------
        seen = []

        def _recorder(exit_d, as_of_d):
            seen.append((exit_d, as_of_d))
            return []

        monkeypatch.setattr(rls, "check_missing_trading_days", _recorder)
        rls.resolve_exit_cooldown(
            ["600000.SH"],
            account_state={"last_exit_date": {"600000.SH": "2026-10-20"}},
            as_of_date=None,  # exercise the fallback under test
            now=_L5_INSTANT,
        )
        assert seen == [(dt.date(2026, 10, 20), _L5_MARKET_AS_OF)]
        # ... and the as-of resolution alone (no calendar call needed):
        assert rls.resolve_market_as_of_date(["600000.SH"], now=_L5_INSTANT) == _L5_MARKET_AS_OF
        # An unrecognisable/mixed symbol list cannot pick a calendar -> documented A-share
        # default instead of an exception (which callers swallow as "no anchor").
        assert rls.resolve_market_as_of_date(["NEW_SYM"], now=_L5_INSTANT) == _L5_MARKET_AS_OF
        assert rls.resolve_market_as_of_date(["600000.SH", "AAPL"], now=_L5_INSTANT) == _L5_MARKET_AS_OF

        # --- site 2: `--end` default ----------------------------------------------
        assert sync.default_end_date(now=_L5_INSTANT) == "2026-10-23"

        # --- site 3: EastMoney intraday fallback fetch window ---------------------
        _freeze_module_clock(monkeypatch, rec, _L5_INSTANT)
        windows = []

        class _RecordingEastMoney:
            def __init__(self, *a, **k):
                pass

            def fetch_ohlcv(self, symbol, start, end, interval="5m"):
                windows.append((symbol, start, end))
                return pd.DataFrame()

        monkeypatch.setattr(rec, "EastMoneyDataProvider", _RecordingEastMoney)
        etfs = ["510300.SH", "510500.SH", "159919.SZ", "511880.SH", "512100.SH", "588000.SH"]
        rec.FuyaoThrottledClient(client=object()).get_snapshots(etfs)
        assert len(windows) == len(etfs)
        assert {(start, end) for _, start, end in windows} == {("2026-10-23", "2026-10-23")}

        # --- site 4: retention age, on the same calendar as the filename key ------
        assert rec.market_today() == _L5_MARKET_DAY
        kept = rec.market_today() - dt.timedelta(days=90)     # age == 90 -> keep
        expired = rec.market_today() - dt.timedelta(days=91)  # age == 91 -> delete
        assert kept == dt.date(2026, 7, 25) and expired == dt.date(2026, 7, 24)

        # --- site 5: the `{date}.parquet` key -------------------------------------
        assert rec.market_today_str() == "2026-10-23"

        # Host-TZ invariance: the answers above did not depend on `host_tz`.
        assert rec.market_today() == _L5_MARKET_DAY
        assert sync.default_end_date(now=_L5_INSTANT) == "2026-10-23"


@pytest.mark.parametrize("host_tz", ["America/Los_Angeles", "Asia/Shanghai"])
def test_retention_cleanup_uses_the_market_day(host_tz, monkeypatch, tmp_path):
    """Site 4 behaviourally: a UTC-7 host must not keep an extra day of parquet files."""
    from scripts import record_intraday_5m as rec

    _freeze_module_clock(monkeypatch, rec, _L5_INSTANT)
    with _host_timezone(host_tz):
        keep = tmp_path / "2026-07-25.parquet"   # age 90 vs market day 2026-10-23
        drop = tmp_path / "2026-07-24.parquet"   # age 91
        for path in (keep, drop):
            path.write_bytes(b"x")

        deleted = rec.cleanup_old_parquet_files(str(tmp_path), retention_days=90)

        assert keep.exists(), "a file exactly at the retention boundary must be kept"
        assert not drop.exists(), "a file past the retention boundary must be deleted"
        assert deleted == [str(drop)]


def test_main_keys_the_daily_parquet_file_on_the_market_day():
    """Site 5 structurally: a long-running recorder must never revert to `date.today()`."""
    import inspect

    from scripts import record_intraday_5m as rec

    src = inspect.getsource(rec.main)
    assert "market_today_str()" in src
    # `--once` runs write `{market day}.parquet` through `writer.write_bars(..., today_str)`.
    assert "today_str" in src


def _date_today_call_lines(path):
    """Line numbers of real `date.today()` CALLS (comments/strings never count).

    Token-level, so a comment or docstring that records the `L2.31` history keeps its
    literal without failing the guard -- while any live call, however it is spelled
    (`date.today()`, `datetime.date.today()`), is caught.
    """
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
    toks = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in skip]
    hits = []
    for i in range(len(toks) - 3):
        a, b, c, d = toks[i], toks[i + 1], toks[i + 2], toks[i + 3]
        if (
            a.type == tokenize.NAME
            and a.string == "date"
            and b.string == "."
            and c.type == tokenize.NAME
            and c.string == "today"
            and d.string == "("
        ):
            hits.append(a.start[0])
    return hits


def test_no_bare_date_today_call_left_in_scripts():
    """`grep -rn "date.today()" scripts/` == zero NON-COMMENT hits (L2.31 acceptance)."""
    scripts_dir = os.path.join(_REPO_ROOT, "scripts")
    offenders = {}
    for fname in sorted(os.listdir(scripts_dir)):
        if not fname.endswith(".py"):
            continue
        lines = _date_today_call_lines(os.path.join(scripts_dir, fname))
        if lines:
            offenders[fname] = lines
    assert offenders == {}, (
        "script-layer code still derives a date from the HOST calendar; route it through "
        f"common/market_clock instead: {offenders}"
    )


# --------------------------------------------------------------------------- #
# L2.30 (residual) -- the MULTI-STRATEGY netted entrypoint carries the same marker
# --------------------------------------------------------------------------- #
def _multi_strategy_args(tmp_path, as_of=None):
    """CLI for `scripts/run_live_multi_strategy.py` (the netted runner)."""
    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    cmd = [
        "--strategies", "equal_weight:1.0",
        "--universe-file", universe_file,
        "--output-dir", str(tmp_path),
        "--start-date", "2026-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--no-cache",
    ]
    if as_of:
        cmd.extend(["--as-of-date", as_of])
    return cmd


class _StopAfterPersist(Exception):
    """Sentinel: stop `main()` at the archive step -- nothing after it is under test."""


def _run_multi_strategy_to_archive(monkeypatch, tmp_path, capsys, as_of):
    """Drive the real `main()` up to (and including) its `persist_run_metadata` call site.

    Only the data/freshness boundaries are stubbed; the clock resolution, the banner, the
    argument parsing and the archive call itself are the real code paths -- that is the
    whole point, since the bug being guarded against was a MISSING keyword at that site.
    """
    from scripts import run_live_multi_strategy as rms

    captured = {}

    def _fake_persist(output_dir, clock_ctx, **kwargs):
        captured["extra"] = kwargs.get("extra")
        captured["as_of"] = clock_ctx.as_of_date
        captured["gate"] = clock_ctx.window_gate
        raise _StopAfterPersist

    monkeypatch.setattr(rms, "load_live_universe", lambda *a, **k: ({}, 1))
    monkeypatch.setattr(rms, "assert_fresh_universe", lambda *a, **k: {})
    monkeypatch.setattr(rms, "persist_run_metadata", _fake_persist)
    monkeypatch.setattr(
        sys, "argv", ["run_live_multi_strategy.py"] + _multi_strategy_args(tmp_path, as_of)
    )

    with pytest.raises(_StopAfterPersist):
        rms.main()
    return captured, capsys.readouterr().out


def test_multi_strategy_entry_marks_replay_run_mode(monkeypatch, tmp_path, capsys):
    """A replay routed through the NETTED runner must announce itself AND archive the marker.

    `run_live_multi_strategy.main()` resolves the clock via `resolve_live_clock_context`
    exactly as Stage 2 does and declares its own `--as-of-date`, so a pinned past session
    bypasses the run-window gate there too and still emits a full netted ticket. Until the
    marker was threaded, that archive was indistinguishable from a same-day live run --
    the precise failure mode `L2.30` exists to prevent.
    """
    _freeze(monkeypatch, _BEIJING_0959)  # a live-time run at this instant would be REFUSED
    captured, out = _run_multi_strategy_to_archive(monkeypatch, tmp_path, capsys, "2026-10-20")

    assert captured["gate"] == "not_applicable_replay"
    assert "REPLAY MODE" in out
    assert "盘中手工回放仍能出工单" in out  # the exact operational caveat, not a vague hint
    assert captured["extra"]["run_mode"] == "replay"
    assert captured["extra"]["stage"] == "multi_strategy_netted"
    assert captured["extra"]["date"] == "2026-10-20"


def test_multi_strategy_entry_default_run_is_live(monkeypatch, tmp_path, capsys):
    """No `--as-of-date`: as-of is the clock's published session -> `run_mode=live`."""
    _freeze(monkeypatch, _BEIJING_1530)  # 15:30 Beijing -> as_of 2026-10-23
    captured, out = _run_multi_strategy_to_archive(monkeypatch, tmp_path, capsys, None)

    assert "REPLAY MODE" not in out
    assert captured["extra"]["run_mode"] == "live"
    assert captured["extra"]["date"] == _AS_OF_PUBLISHED


def test_multi_strategy_reuses_the_stage2_replay_predicate():
    """Exactly ONE replay predicate: the netted runner imports it, never re-implements it.

    A second copy would be free to drift (e.g. after a gate-label rename), which would
    silently re-open the hole this task closed.
    """
    from scripts import run_live_multi_strategy as rms
    from scripts import run_live_strategy as rls

    assert rms.resolve_run_mode is rls.resolve_run_mode
    assert rms.announce_run_mode is rls.announce_run_mode


def test_stage1_defaults_to_allow_intraday_and_supports_flags():
    """Stage 1 defaults allow_intraday to True, Stage 2 defaults to False."""
    import argparse
    from common import cli_utils

    p1 = argparse.ArgumentParser()
    cli_utils.add_market_clock_cli_args(p1, stage="stage1")
    parsed_s1 = p1.parse_args([])
    assert parsed_s1.allow_intraday is True

    parsed_s1_no = p1.parse_args(["--no-allow-intraday"])
    assert parsed_s1_no.allow_intraday is False

    p2 = argparse.ArgumentParser()
    cli_utils.add_market_clock_cli_args(p2, stage="stage2")
    parsed_s2 = p2.parse_args([])
    assert parsed_s2.allow_intraday is False

    parsed_s2_yes = p2.parse_args(["--allow-intraday"])
    assert parsed_s2_yes.allow_intraday is True


def test_stage1_refresh_data_cli_and_offline_skip(tmp_path):
    """Stage 1 CLI supports --refresh-data and --no-refresh-data, and skips offline."""
    from scripts.check_live_portfolio_health import parse_args as parse_stage1_args, refresh_stage1_data

    res_default = parse_stage1_args(["--strategy-file", "dummy.json"])
    assert res_default.refresh_data is True

    res_no = parse_stage1_args(["--strategy-file", "dummy.json", "--no-refresh-data"])
    assert res_no.refresh_data is False

    # 100% offline rule check: synthetic or no_cache returns skipped immediately
    skipped_syn = refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="synthetic",
        cache_dir=str(tmp_path),
        no_cache=False,
    )
    assert skipped_syn == {"status": "skipped", "reason": "synthetic_or_no_cache"}

    skipped_nocache = refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(tmp_path),
        no_cache=True,
    )
    assert skipped_nocache == {"status": "skipped", "reason": "synthetic_or_no_cache"}

    skipped_csv = refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="csv",
        cache_dir=str(tmp_path),
        no_cache=False,
    )
    assert skipped_csv == {"status": "skipped", "reason": "synthetic_or_no_cache"}


def test_stage1_incident_timestamp_lunch_break_gate_and_refresh_order(monkeypatch, tmp_path):
    """Incident timestamp: 2026-10-27T11:37:32+08:00 (US Pacific 2026-10-26T20:37:32-07:00).

    Verifies:
    1. Session is lunch_break and market is china.
    2. Stage 1 runs without raising RunWindowError (since allow_intraday defaults to True in Stage 1).
    3. Resolves as_of_date == "2026-10-26" and sets run_window_gate == "intraday_allowed".
    4. Calls refresh_stage1_data BEFORE load_live_universe.
    """
    import scripts.check_live_portfolio_health as health_mod

    # Exact incident instant: 2026-10-27T11:37:32+08:00
    # In UTC: 2026-10-27T03:37:32Z
    # In US Pacific (PDT, UTC-7): 2026-10-26T20:37:32-07:00
    incident_instant = dt.datetime(2026, 10, 27, 3, 37, 32, tzinfo=dt.timezone.utc)
    _freeze(monkeypatch, incident_instant)

    # Track execution order of refresh_stage1_data vs load_live_universe
    call_order = []
    orig_refresh = health_mod.refresh_stage1_data
    orig_load = health_mod.load_live_universe

    def spy_refresh(*args, **kwargs):
        call_order.append("refresh_stage1_data")
        return orig_refresh(*args, **kwargs)

    def spy_load(*args, **kwargs):
        call_order.append("load_live_universe")
        return orig_load(*args, **kwargs)

    monkeypatch.setattr(health_mod, "refresh_stage1_data", spy_refresh)
    monkeypatch.setattr(health_mod, "load_live_universe", spy_load)

    universe_file = _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"])
    out_dir = str(tmp_path / "reports")

    # Run Stage 1 main - must NOT raise RunWindowError
    res = health_mod.main(
        [
            "--universe-file", universe_file,
            "--output-dir", out_dir,
            "--start-date", "2026-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", "100000",
            "--peak-nav", "100000",
            "--no-save-state",
        ]
    )
    assert res is not None

    # Verify refresh_stage1_data was called BEFORE load_live_universe
    assert call_order == ["refresh_stage1_data", "load_live_universe"]

    # Verify report and summary JSON contents
    report = json.loads((tmp_path / "reports" / "stage1_health_report.json").read_text(encoding="utf-8"))
    assert report["as_of_date"] == "2026-10-26"
    assert report["clock"]["market"] == "china"
    assert report["clock"]["session"] == "lunch_break"
    assert report["clock"]["run_window_gate"] == "intraday_allowed"
    assert report["clock"]["as_of_trading_date"] == "2026-10-26"
    assert report["clock"]["expected_last_session"] == "2026-10-26"
    assert report["clock"]["local_now"] == "2026-10-27T11:37:32+08:00"

    summary = json.loads((tmp_path / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "stage1"
    assert summary["clock"]["as_of_trading_date"] == "2026-10-26"
    assert summary["clock"]["session"] == "lunch_break"
    assert summary["clock"]["run_window_gate"] == "intraday_allowed"


def test_refresh_stage1_data_invokes_sync_routines_when_marketdb(monkeypatch, tmp_path):
    """refresh_stage1_data invokes sync routines with mocked sync functions when provider=marketdb and no_cache=False."""
    from unittest.mock import MagicMock
    import scripts.check_live_portfolio_health as health_mod
    import scripts.sync_china_5m_and_indices as sync_mod

    mock_cache = MagicMock()
    mock_duckdb_cls = MagicMock(return_value=mock_cache)
    monkeypatch.setattr("common.duckdb_cache.DuckDBCache", mock_duckdb_cls)

    mock_sync_indices = MagicMock()
    mock_patch_equity = MagicMock()
    mock_sync_5m = MagicMock()

    monkeypatch.setattr(sync_mod, "sync_indices_daily", mock_sync_indices)
    monkeypatch.setattr(sync_mod, "patch_equity_daily_bars", mock_patch_equity)
    monkeypatch.setattr(sync_mod, "sync_5m_intraday", mock_sync_5m)

    symbols = ["600000.SH", "601857.SH", "BIL"]

    # 1. With enable_intraday_5m=True: all 3 routines invoked
    #
    # (2026-10-27) `allow_create_cache=True` is now required here: this test exercises the
    # creation path (the DuckDB layer itself is mocked, so no file is ever produced) and the
    # new default REFUSES a missing cache -- Stage-1 refreshes, it does not rebuild. The
    # refusal path has its own regression tests in `test_cache_write_gates.py`.
    summary_5m = health_mod.refresh_stage1_data(
        symbols=symbols,
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(tmp_path),
        market="china",
        enable_intraday_5m=True,
        no_cache=False,
        allow_create_cache=True,
    )
    assert summary_5m["status"] == "completed"
    assert summary_5m["steps"]["sync_indices_daily"] == "ok"
    assert summary_5m["steps"]["patch_equity_daily_bars"] == "ok"
    assert summary_5m["steps"]["sync_5m_intraday"] == "ok"

    mock_sync_indices.assert_called_once()
    assert mock_sync_indices.call_args[0][0] == mock_cache
    assert mock_sync_indices.call_args[0][1] == sync_mod.MAJOR_INDICES
    assert mock_sync_indices.call_args[1]["end"] == "2026-10-26"

    mock_patch_equity.assert_called_once()
    assert mock_patch_equity.call_args[0][0] == mock_cache
    assert mock_patch_equity.call_args[0][1] == ["600000.SH", "601857.SH"]  # BIL excluded
    assert mock_patch_equity.call_args[1]["end"] == "2026-10-26"

    mock_sync_5m.assert_called_once()
    assert mock_sync_5m.call_args[0][0] == mock_cache
    assert mock_sync_5m.call_args[0][1] == ["600000.SH", "601857.SH"]
    assert mock_sync_5m.call_args[0][2] == sync_mod.MAJOR_INDICES
    assert mock_sync_5m.call_args[1]["end"] == "2026-10-26"

    # 2. With enable_intraday_5m=False: 5m routine not invoked
    mock_sync_indices.reset_mock()
    mock_patch_equity.reset_mock()
    mock_sync_5m.reset_mock()

    summary_no_5m = health_mod.refresh_stage1_data(
        symbols=symbols,
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(tmp_path),
        market="china",
        enable_intraday_5m=False,
        no_cache=False,
        # (2026-10-27) still a missing-cache call: the mocked DuckDB layer never creates the
        # file, so the default gate would refuse the second half of this test too.
        allow_create_cache=True,
    )
    assert summary_no_5m["status"] == "completed"
    assert summary_no_5m["steps"]["sync_indices_daily"] == "ok"
    assert summary_no_5m["steps"]["patch_equity_daily_bars"] == "ok"
    assert "sync_5m_intraday" not in summary_no_5m["steps"]
    mock_sync_indices.assert_called_once()
    mock_patch_equity.assert_called_once()
    mock_sync_5m.assert_not_called()



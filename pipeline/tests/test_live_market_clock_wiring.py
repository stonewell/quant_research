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
import datetime as dt
import json
import os
import sys
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

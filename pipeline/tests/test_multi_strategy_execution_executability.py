"""(`NV16`, 2026-11-17) The D3 executability gate on the NETTED multi-strategy runner.

THE HOLE
--------
`scripts/run_live_multi_strategy.py` resolved the market clock exactly as Stage 2 does
(`resolve_live_clock_context(..., stage="stage2")`) but never ran the *execution-executability*
check. So the only thing standing between a mid-session round and a written
`netted_trading_ticket.json` was the run-WINDOW gate -- and `--allow-intraday` downgrades that
refusal to a warning (`common/cli_utils.py::assert_run_window` ->
`window_gate="intraday_allowed"`). The web requests defaulted `allow_intraday=True`, hence the
reachability: an 11:00 / 14:00 matrix run from the station produced a ticket that DECLARED
`execution_session=as_of+1` / `price_reference=next_open` (an instant that had already passed)
while Stage 2's single-strategy path had been fail-closed since D3
(`scripts/run_live_strategy.py::enforce_execution_executability` ->
`MissedExecutionOpenError`).

OPERATOR RULING (2026-11-17): live ticketing is post-close -> next-open ONLY, i.e. intraday
ticketing is never legitimate. Hence the gate is UNCONDITIONAL on this runner: `--allow-intraday`
relaxes the window, never a missed open.

WHAT THIS SUITE PINS
--------------------
(a) a mid-session multi-strategy round RAISES `MissedExecutionOpenError` and leaves NO
    `netted_trading_ticket.json` / `.csv` behind (the refusal writes only the auditable
    `execution_timing.json` block, exactly like Stage 2);
(b) a post-close -> next-open round still emits the ticket as before (guard against
    over-blocking), with `--allow-intraday` absent -- i.e. the new default;
(c) a pinned PAST `--as-of-date` (the replay/backfill escape hatch) still tickets intraday:
    `annotate_executability` exempts replay inside the shared helper, which is why the replay
    path needs no flag and must not be broken by the default flip.

100% offline: synthetic provider only. The only doubles are the strategy definitions (the
subject is the runner's timing wiring, not a blend engine) and the 5m loader (poisoned, so no
code path can reach a DuckDB cache). Every clock read is the injected `now`.
"""

import datetime as dt
import json
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import common.cli_utils as cli_utils  # noqa: E402
from common.live_execution_timing import MissedExecutionOpenError  # noqa: E402
from scripts import run_live_multi_strategy as rms  # noqa: E402

_UTC = dt.timezone.utc
#: 2026-10-23 is a Friday.
#: 07:30 UTC == 15:30 Beijing: AFTER the daily close + publication grace, i.e. INSIDE the
#: designed post-close -> next-open window (`as_of=2026-10-23`, `execution_date=2026-10-26`).
_BEIJING_1530 = dt.datetime(2026, 10, 23, 7, 30, tzinfo=_UTC)
#: 03:00 UTC == 11:00 Beijing: mid-session, and AFTER that session's 09:30 open -- the exact
#: shape that used to be ticketed (`execution_date=2026-10-23`, `as_of=2026-10-22`).
_BEIJING_1100 = dt.datetime(2026, 10, 23, 3, 0, tzinfo=_UTC)

_AS_OF_POST_CLOSE = "2026-10-23"
_AS_OF_INTRADAY = "2026-10-22"
_EXECUTION_DATE_INTRADAY = "2026-10-23"
_UNIVERSE = ["600000.SH", "601857.SH", "BIL"]
_STRATEGY_KEYS = ["trend_a", "trend_b"]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _freeze(monkeypatch, instant):
    """Pin the single wall-clock read every live entrypoint goes through."""
    real_market_now = cli_utils.market_now

    def _frozen(market, now=None, holidays=None, publication_grace_minutes=None):
        return real_market_now(
            market,
            now=now if now is not None else instant,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(cli_utils, "market_now", _frozen)


def _weights_from_last_close(universe):
    """Deterministic, sparse, CLOSE-dependent target weights (a real signal, not a constant)."""
    closes = {
        sym: float(df["Close"].iloc[-1])
        for sym, df in universe.items()
        if df is not None
        and not getattr(df, "empty", True)
        and "Close" in getattr(df, "columns", ())
        and str(sym) not in ("BIL", "CASH")
    }
    if not closes:
        return pd.DataFrame()
    total = float(sum(closes.values())) or 1.0
    last_date = max(
        df.index.max()
        for sym, df in universe.items()
        if df is not None and not getattr(df, "empty", True)
    )
    row = {sym: value / total for sym, value in closes.items()}
    return pd.DataFrame([row], index=pd.DatetimeIndex([last_date]))


class _StaticTemplate:
    """Static-template double: NO `resolve_param`, hence it can never request 5m (§4.50.1)."""

    def __init__(self, label):
        self.label = label
        self.params = {}

    def generate_weights(self, universe, params):
        return _weights_from_last_close(universe)

    def explain_weights(self, params):  # pragma: no cover - contract completeness
        return self.label

    def warmup_bars(self, params):  # pragma: no cover - contract completeness
        return 0


def _install_fake_strategy_resolution(monkeypatch):
    """Replace ONLY the dump/config resolver; everything else in `main()` is real."""
    templates = {key: _StaticTemplate(key) for key in _STRATEGY_KEYS}

    def _resolve(key, _strategy_file=None):
        strat_def = {
            "template_name": "equal_weight",
            "strategy_name": f"Fake {key}",
            "params": {},
        }
        return strat_def, "equal_weight", "tests:fake_strategy", templates[key]

    monkeypatch.setattr(rms, "resolve_strategy_definition", _resolve)


def _poisoned_5m(*_args, **_kwargs):
    raise AssertionError(
        "the netted 5m loader must not run: no strategy in this suite requests 5m, and this "
        "suite must stay hermetic (no DuckDB cache, no provider 5m frames)"
    )


def _write_universe(directory, symbols):
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), "universe.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _run_netting(monkeypatch, tmp_path, *, instant, extra_args=()):
    """Run the REAL `main()` end-to-end; return the output directory (may be empty on refusal)."""
    _freeze(monkeypatch, instant)
    _install_fake_strategy_resolution(monkeypatch)
    monkeypatch.setattr(rms, "load_intraday_5m_panel", _poisoned_5m)
    root = str(tmp_path)
    universe_file = _write_universe(root, _UNIVERSE)
    out_dir = os.path.join(root, "out")
    argv = [
        "run_live_multi_strategy.py",
        "--strategies",
        *[f"{key}:1.0" for key in _STRATEGY_KEYS],
        "--universe-file",
        universe_file,
        "--output-dir",
        out_dir,
        "--start-date",
        "2026-01-01",
        "--data-provider",
        "synthetic",
        "--portfolio-value",
        "100000",
        # Hermetic cache: never touch the repo's production DuckDB.
        "--cache-dir",
        os.path.join(root, "cache"),
        "--no-cache",
        *extra_args,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rms.main()
    return out_dir


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# (a) Mid-session: REFUSED, and nothing persisted but the audit block
# --------------------------------------------------------------------------- #
def test_intraday_netted_run_is_refused_and_writes_no_ticket(monkeypatch, tmp_path, capsys):
    """11:00 Beijing, `--allow-intraday` => the WINDOW opens but the OPEN is already gone.

    `--allow-intraday` is passed on purpose: it is the exact configuration that used to reach
    the ticket writer (web default `allow_intraday=True` until NV16). The executable check must
    ignore it, because it only ever relaxed the inspection window.

    re-break: delete the `enforce_execution_executability` call (or move it below the ticket
    write) => `main()` returns normally here and the ticket assertions fail.
    """
    out_dir = os.path.join(str(tmp_path), "out")

    with pytest.raises(MissedExecutionOpenError) as excinfo:
        _run_netting(
            monkeypatch, tmp_path, instant=_BEIJING_1100, extra_args=["--allow-intraday"]
        )

    message = str(excinfo.value)
    # The verdict names the violation, the declared instant and the lateness -- and says the
    # window flag cannot launder it.
    assert "timing_violation=after_open" in message
    assert f"execution_date={_EXECUTION_DATE_INTRADAY}" in message
    assert "--allow-intraday" in message

    # NO ticket of any kind: this is the "nothing is persisted" half of the requirement.
    assert not os.path.exists(os.path.join(out_dir, "netted_trading_ticket.json"))
    assert not os.path.exists(os.path.join(out_dir, "netted_trading_ticket.csv"))
    assert not os.path.exists(os.path.join(out_dir, "friction_savings_report.json"))
    assert not os.path.exists(os.path.join(out_dir, "sub_ledgers.json"))
    assert not os.path.exists(os.path.join(out_dir, "summary.json"))

    # The refusal itself IS auditable (same contract as Stage 2): the block is written BEFORE
    # the raise, so the archive can tell "refused" apart from "never ran".
    block = _load_json(os.path.join(out_dir, "execution_timing.json"))
    assert block["executable"] is False
    assert block["timing_violation"] == "after_open"
    assert block["disposition"] == "refused"
    assert block["execution_date"] == _EXECUTION_DATE_INTRADAY
    assert block["as_of"] == _AS_OF_INTRADAY
    assert block["run_mode"] == "live"
    assert block["window_gate"] == "intraday_allowed"
    assert block["lateness_minutes"] > 0

    # The verdict is on stdout too, so a human reading the console sees the same sentence.
    assert "timing_violation=after_open" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (b) Post-close -> next-open: unchanged, still tickets (no over-blocking)
# --------------------------------------------------------------------------- #
def test_post_close_netted_run_still_emits_the_ticket(monkeypatch, tmp_path):
    """15:30 Beijing, NO `--allow-intraday` (the new default) => in window, ticket as before.

    `as_of=2026-10-23` -> `execution_date=2026-10-26` (Monday), whose open is still in the
    future, so the gate must stay quiet. This is the over-blocking guard.
    """
    out_dir = _run_netting(monkeypatch, tmp_path, instant=_BEIJING_1530)

    ticket_json = os.path.join(out_dir, "netted_trading_ticket.json")
    ticket_csv = os.path.join(out_dir, "netted_trading_ticket.csv")
    for name in ("netted_trading_ticket.json", "netted_trading_ticket.csv",
                 "friction_savings_report.json", "sub_ledgers.json"):
        assert os.path.exists(os.path.join(out_dir, name)), name

    ticket = _load_json(ticket_json)
    assert isinstance(ticket, list) and ticket
    actions = {str(row.get("action", "")) for row in ticket}
    symbols = {str(row.get("symbol", "")) for row in ticket}
    # A real 4-phase book, not an empty placeholder: phase 2 (buys) and phase 4 (cash sweep)
    # both reached the writer.
    assert any(a.startswith("BUY") for a in actions)
    assert "BIL" in symbols
    assert any("CASH" in a for a in actions)
    with open(ticket_csv, encoding="utf-8-sig") as f:
        assert f.readline().strip()  # header present => the CSV was flushed, not truncated

    # Clean run => no audit block at all (the helper only stamps a verdict on a violation),
    # which keeps the pre-NV16 artifact set unchanged for in-window rounds.
    assert not os.path.exists(os.path.join(out_dir, "execution_timing.json"))

    # as_of/execution window were resolved from the clock, not the host's date.
    summary = _load_json(os.path.join(out_dir, "summary.json"))
    assert summary.get("as_of_date", _AS_OF_POST_CLOSE) == _AS_OF_POST_CLOSE


# --------------------------------------------------------------------------- #
# (c) Replay / backfill escape hatch: a pinned PAST as-of is exempt INSIDE the helper
# --------------------------------------------------------------------------- #
def test_replay_with_pinned_past_as_of_still_tickets_intraday(monkeypatch, tmp_path):
    """A deliberate historical replay at 11:00 must keep working (opt-in, unchanged).

    `annotate_executability` returns early for `run_mode="replay"`: there is no live fill to be
    late for. This suite therefore does NOT gate the replay path behind a flag -- it only pins
    that the default flip did not break it.
    """
    out_dir = _run_netting(
        monkeypatch,
        tmp_path,
        instant=_BEIJING_1100,
        extra_args=["--as-of-date", "2026-10-20"],
    )
    assert os.path.exists(os.path.join(out_dir, "netted_trading_ticket.json"))
    assert not os.path.exists(os.path.join(out_dir, "execution_timing.json"))

"""Offline regression suite for `D3` -- a ticket must not be written after its declared fill.

THE DEFECT UNDER TEST. Every ticket declares `execution_session=as_of+1` /
`price_reference=next_open`, i.e. "these orders fill at the NEXT session's OPEN". Nothing
compared that declared instant with the instant the ticket was actually written. Three
consecutive archives resolve `execution_date = as_of+1` and were all generated around NOON
of that very `execution_date` (from `summary.clock.local_now`):

    11-09 run -> 2026-11-10T12:50:14+08:00   (execution_date=2026-11-10)
    11-10 run -> 2026-11-11T12:27:29+08:00   (execution_date=2026-11-11)
    11-11 run -> 2026-11-12T12:52:42+08:00   (execution_date=2026-11-12)

so the declared next-open reference price had already expired when the file appeared.

THE DISPOSITION PINNED HERE = (A) HARD REFUSE, fail-closed:

* `execution_timing.json` gains, ONLY when violated, `executable: false`,
  `timing_violation: after_open|after_close`, `lateness_minutes`, `planned_open`,
  `planned_close`, `disposition: refused` -- and no ticket is written.
* a clean (pre-open) run's key set is UNCHANGED: absent means "not late (or not judgeable)",
  never "false".
* `--allow-intraday` / `window_gate=intraday_allowed` relax the inspection WINDOW only; they
  must not launder "missed the open".
* `run_mode=replay` is exempt: a historical replay has no live fill to be late for.

100% offline: synthetic clock and `tmp_path` only.
"""

import datetime as _dt
import json
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common.live_execution_timing import (
    EXECUTION_TIMING_FILENAME,
    MissedExecutionOpenError,
    annotate_executability,
    executability_log_line,
    merge_timing_into_summary,
    resolve_execution_timing,
    write_execution_timing,
)
from common.market_clock import MarketClock
from scripts.run_live_strategy import enforce_execution_executability

_TZ = _dt.timezone(_dt.timedelta(hours=8))
#: The live window is a datetime pair now (post-close -> next open, crossing midnight).
_WINDOW = (
    _dt.datetime(2026, 11, 9, 15, 30),
    _dt.datetime(2026, 11, 10, 9, 30),
)


def _clock(local_now):
    """China clock at a fixed market-local instant (explicit empty holiday set = offline)."""
    moment = _dt.datetime.fromisoformat(str(local_now))
    return MarketClock(market="china", now=moment.astimezone(_TZ), holidays=frozenset())


def _ctx(clock, *, gate="intraday_allowed", allow_intraday=True, as_of="2026-11-09"):
    return types.SimpleNamespace(
        window_gate=gate,
        window=_WINDOW,
        as_of_date=as_of,
        clock=clock,
        local_now=lambda: clock.local_now(),
        allow_intraday=allow_intraday,
    )


def _timing(local_now, *, gate="intraday_allowed", run_mode="live", as_of="2026-11-09"):
    clock = _clock(local_now)
    ctx = _ctx(clock, gate=gate, as_of=as_of)
    return resolve_execution_timing(ctx, as_of=as_of, run_mode=run_mode), clock


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_after_open_on_the_execution_date_is_a_violation():
    """The archived 11-09 shape: generated 12:50 on the very `execution_date` (open 09:30)."""
    timing, clock = _timing("2026-11-10T12:50:14+08:00")

    assert timing["execution_date"] == "2026-11-10"
    annotated = annotate_executability(timing, clock)

    assert annotated["executable"] is False
    assert annotated["timing_violation"] == "after_open"
    assert annotated["lateness_minutes"] == pytest.approx(200.2, abs=0.2)
    assert annotated["disposition"] == "refused"
    assert annotated["planned_open"] == "2026-11-10T09:30:00+08:00"
    assert annotated["planned_close"] == "2026-11-10T15:00:00+08:00"


def test_after_close_is_the_more_severe_violation():
    """A post-close generation is NOT reported as merely "after open"."""
    timing, clock = _timing("2026-11-10T15:30:00+08:00")

    annotated = annotate_executability(timing, clock)

    assert annotated["timing_violation"] == "after_close"
    assert annotated["lateness_minutes"] == pytest.approx(360.0, abs=0.1)


def test_a_pre_open_generation_on_the_same_day_is_clean():
    """Generated 08:00 on `execution_date` -> the open is still ahead: NO audit keys at all."""
    timing, clock = _timing("2026-11-10T08:00:00+08:00")

    annotated = annotate_executability(timing, clock)

    assert set(annotated) == set(timing)
    for key in ("executable", "timing_violation", "lateness_minutes", "disposition"):
        assert key not in annotated


def test_the_archived_intraday_lunch_break_instants_are_all_violations():
    """All three archived vintages, verbatim from `summary.clock.local_now`."""
    for local_now, execution_date in (
        ("2026-11-10T12:50:14+08:00", "2026-11-10"),
        ("2026-11-11T12:27:29+08:00", "2026-11-11"),
        ("2026-11-12T12:52:42+08:00", "2026-11-12"),
    ):
        timing, clock = _timing(local_now, as_of=_previous_day(execution_date))
        assert timing["execution_date"] == execution_date
        annotated = annotate_executability(timing, clock)
        assert annotated["executable"] is False
        assert annotated["timing_violation"] == "after_open"


def _previous_day(iso):
    day = _dt.date.fromisoformat(iso) - _dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= _dt.timedelta(days=1)
    return day.isoformat()


def test_replay_runs_are_exempt_because_there_is_no_live_fill():
    timing, clock = _timing("2026-11-10T12:50:14+08:00", run_mode="replay")

    annotated = annotate_executability(timing, clock)

    assert "timing_violation" not in annotated


def test_unjudgeable_timing_is_left_untouched():
    """No `execution_date` / no clock -> absence, NOT a fabricated `executable=false`."""
    timing, clock = _timing("2026-11-10T12:50:14+08:00")
    timing["execution_date"] = None

    assert annotate_executability(timing, clock) == timing
    assert annotate_executability(timing, None) == timing


# ---------------------------------------------------------------------------
# Persisted shape (clean key set unchanged / audit keys only when violated)
# ---------------------------------------------------------------------------


def test_clean_execution_timing_json_key_set_is_unchanged(tmp_path):
    timing, clock = _timing("2026-11-10T08:00:00+08:00")
    clean = annotate_executability(timing, clock)

    path = write_execution_timing(str(tmp_path), clean)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    assert path == os.path.join(str(tmp_path), EXECUTION_TIMING_FILENAME)
    assert "executable" not in payload
    assert "timing_violation" not in payload
    assert "lateness_minutes" not in payload
    assert "disposition" not in payload
    # The pre-D3 block is still intact.
    for key in ("execution_session", "price_reference", "as_of", "execution_date", "local_now"):
        assert key in payload


def test_violating_execution_timing_json_carries_the_structured_fields(tmp_path):
    timing, clock = _timing("2026-11-10T12:50:14+08:00")
    annotated = annotate_executability(timing, clock)

    path = write_execution_timing(str(tmp_path), annotated)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    assert payload["executable"] is False
    assert payload["timing_violation"] == "after_open"
    assert payload["lateness_minutes"] == pytest.approx(200.2, abs=0.2)
    assert payload["disposition"] == "refused"
    assert payload["timing_log"].startswith("[EXECUTION TIMING] executable=false")


def test_summary_block_gains_the_audit_fields_only_when_violated():
    timing, clock = _timing("2026-11-10T08:00:00+08:00")
    clean_block = merge_timing_into_summary(annotate_executability(timing, clock))["execution_timing"]
    assert "executable" not in clean_block

    timing2, clock2 = _timing("2026-11-10T12:50:14+08:00")
    late_block = merge_timing_into_summary(annotate_executability(timing2, clock2))["execution_timing"]
    assert late_block["executable"] is False
    assert late_block["timing_violation"] == "after_open"


# ---------------------------------------------------------------------------
# Disposition (A): hard refusal, and `--allow-intraday` does NOT bypass it
# ---------------------------------------------------------------------------


def _file_set(root):
    return sorted(os.listdir(str(root)))


def test_enforce_refuses_the_ticket_and_writes_the_audit_block(tmp_path, capsys):
    timing, clock = _timing("2026-11-10T12:50:14+08:00")

    with pytest.raises(MissedExecutionOpenError) as excinfo:
        enforce_execution_executability(timing, _ctx(clock), str(tmp_path))

    message = str(excinfo.value)
    assert "timing_violation=after_open" in message
    assert "--allow-intraday" in message
    out = capsys.readouterr().out
    assert "[EXECUTION TIMING] executable=false" in out
    # Audit block landed; NO ticket was written.
    assert _file_set(tmp_path) == [EXECUTION_TIMING_FILENAME]
    with open(os.path.join(str(tmp_path), EXECUTION_TIMING_FILENAME), "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["executable"] is False
    assert payload["disposition"] == "refused"


def test_allow_intraday_does_not_bypass_the_missed_open(tmp_path):
    """`intraday_allowed` relaxes the WINDOW gate only -- it must not launder the late fill."""
    timing, clock = _timing("2026-11-10T12:50:14+08:00", gate="intraday_allowed")

    with pytest.raises(MissedExecutionOpenError):
        enforce_execution_executability(timing, _ctx(clock, allow_intraday=True), str(tmp_path))


def test_window_gate_label_does_not_change_the_verdict(tmp_path):
    """The window gate's label is provenance, not evidence: only the planned OPEN decides.

    An `in_window` run is refused exactly like an `intraday_allowed` one when its declared
    fill instant is already past -- the two facts are independent.
    """
    timing, clock = _timing("2026-11-10T13:00:00+08:00", gate="in_window")

    with pytest.raises(MissedExecutionOpenError):
        enforce_execution_executability(timing, _ctx(clock, gate="in_window"), str(tmp_path))


def test_enforce_returns_the_block_unchanged_when_the_open_is_ahead(tmp_path, capsys):
    timing, clock = _timing("2026-11-10T08:00:00+08:00")

    returned = enforce_execution_executability(timing, _ctx(clock), str(tmp_path))

    assert returned == timing
    out = capsys.readouterr().out
    assert "[EXECUTION TIMING] executable=true" in out
    assert "timing_violation=none" in out
    assert _file_set(tmp_path) == []


def test_log_line_reports_violation_and_disposition():
    timing, clock = _timing("2026-11-10T15:30:00+08:00")
    line = executability_log_line(annotate_executability(timing, clock))

    assert line.startswith("[EXECUTION TIMING] ")
    assert "executable=false" in line
    assert "timing_violation=after_close" in line
    assert "disposition=refused" in line
    assert "lateness_minutes=360.0" in line


# ---------------------------------------------------------------------------
# Wiring: `main()` refuses BEFORE any data load / ticket write
# ---------------------------------------------------------------------------


def test_main_refuses_before_loading_data_or_writing_a_ticket(tmp_path, monkeypatch):
    """The real `main()` path, with only the clock context faked.

    Proves the call site exists and sits ahead of the order math: no synthetic data is even
    needed, because the refusal happens right after timing resolution.
    """
    import datetime as _d

    import scripts.run_live_strategy as runner

    universe_file = tmp_path / "univ.txt"
    universe_file.write_text("600000.SH\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    clock = _clock("2026-11-10T12:50:14+08:00")
    ctx = _ctx(clock, as_of="2026-11-09")
    ctx.clock_as_of = "2026-11-09"
    ctx.expected_last_session = _d.date(2026, 11, 9)
    ctx.market = "china"
    ctx.market_inference = "inferred"
    ctx.as_of_source = "explicit"
    ctx.freshness_strict = True
    ctx.strict_market_clock = True
    ctx.publication_grace_minutes = 30
    ctx.max_probe_attempts = 1
    ctx.window_note = ""
    ctx.stage = "stage2"

    monkeypatch.setattr(runner, "resolve_live_clock_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_strategy.py",
            "--strategy", "equal_weight",
            "--universe-file", str(universe_file),
            "--output-dir", str(out_dir),
            "--portfolio-value", "100000",
            "--data-provider", "synthetic",
        ],
    )

    with pytest.raises(MissedExecutionOpenError):
        runner.main()

    assert not (out_dir / "live_trading_ticket.json").exists()
    assert not (out_dir / "live_trading_ticket.csv").exists()
    with open(out_dir / EXECUTION_TIMING_FILENAME, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["executable"] is False
    assert payload["timing_violation"] == "after_open"

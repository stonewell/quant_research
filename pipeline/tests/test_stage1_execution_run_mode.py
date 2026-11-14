"""`L2.30` Stage-1 half: the Stage-1 report must not claim `run_mode="live"` on a replay.

THE DEFECT UNDER TEST
---------------------
`scripts/check_live_portfolio_health.py` resolved its execution-timing block with a
HARDCODED `run_mode="live"`:

    timing = resolve_execution_timing(clock_ctx, as_of=as_of_date, run_mode="live")

That block is written into `stage1_health_report.json` (`execution_timing`) and into
`summary.json` (`execution_timing.run_mode` via `merge_timing_into_summary`), so it IS the
machine-readable answer to "was this a real run or a replay?". A `--as-of-date` pinned to an
EARLIER session is a deliberate replay/backfill -- `common/cli_utils.resolve_live_clock_context`
already labels it `window_gate="not_applicable_replay"` and Stage 2 has recorded
`run_mode=replay` for it since `L2.30` -- yet Stage 1's report said `live`, i.e. the report
lied about its own mode, in the very block a post-hoc audit reads.

THE FIX PINNED HERE
-------------------
`resolve_stage1_run_mode(clock_ctx)` DELEGATES to
`scripts.run_live_strategy.resolve_run_mode` (the single replay predicate Stage 2 and the
netted runner already share -- never a second copy), and `main()` passes its answer into
`resolve_execution_timing`. The three invocations that matter:

* no `--as-of-date` (as-of = the clock's published session) => `live`, report unchanged;
* `--as-of-date` EQUAL to that session (what the web layer sends) => `live`, unchanged;
* `--as-of-date` pinned EARLIER => `replay`, and the marker is in the written report JSON.

100% offline: synthetic provider, a frozen clock instant (no `freezegun`), every write under
pytest's `tmp_path`, no network.
"""

import datetime as dt
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

import common.cli_utils as cli_utils

_UTC = dt.timezone.utc

#: Beijing 15:30 on 2026-10-23 -> close + publication grace already published, so the
#: market clock's latest session is 2026-10-23 (the normal post-close live window).
_BEIJING_1530 = dt.datetime(2026, 10, 23, 7, 30, tzinfo=_UTC)
_AS_OF_PUBLISHED = "2026-10-23"
#: One session EARLIER: pinning this is a deliberate replay, not a live run.
_AS_OF_REPLAY = "2026-10-22"


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


def _write_universe(tmp_path, symbols):
    path = str(tmp_path / "universe.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _run_stage1(tmp_path, as_of=None, extra=None):
    """One REAL Stage-1 `main()`; returns `(report, summary)` read back from disk."""
    import scripts.check_live_portfolio_health as health_mod

    out_dir = os.path.join(str(tmp_path), "reports" + f"_{as_of or 'clock'}")
    argv = [
        "--universe-file", _write_universe(tmp_path, ["600000.SH", "601857.SH", "BIL"]),
        "--output-dir", out_dir,
        "--start-date", "2026-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--peak-nav", "100000",
        "--no-save-state",
    ]
    if as_of:
        argv.extend(["--as-of-date", as_of])
    argv.extend(extra or [])

    assert health_mod.main(argv) is not None

    with open(os.path.join(out_dir, "stage1_health_report.json"), encoding="utf-8") as f:
        report = json.load(f)
    with open(os.path.join(out_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    return report, summary


# --------------------------------------------------------------------------- #
# 1. The predicate is DELEGATED, never re-implemented
# --------------------------------------------------------------------------- #
def test_stage1_run_mode_is_the_same_predicate_as_stage2():
    """One predicate, two entrypoints: a second copy could drift from Stage 2's verdict."""
    from scripts.check_live_portfolio_health import resolve_stage1_run_mode
    from scripts.run_live_strategy import resolve_run_mode

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

    cases = [
        # The gate's own replay verdict is authoritative.
        _ctx("not_applicable_replay", _AS_OF_REPLAY, _AS_OF_PUBLISHED, "explicit"),
        # Defensive second spelling of the same predicate (gate label renamed).
        _ctx("something_else", _AS_OF_REPLAY, _AS_OF_PUBLISHED, "explicit"),
        # Clock-derived and explicit SAME-DAY runs are live.
        _ctx("in_window", _AS_OF_PUBLISHED, _AS_OF_PUBLISHED, "market_clock"),
        _ctx("not_applicable", _AS_OF_PUBLISHED, _AS_OF_PUBLISHED, "explicit"),
        _ctx("intraday_allowed", _AS_OF_PUBLISHED, _AS_OF_PUBLISHED, "explicit"),
    ]
    for ctx in cases:
        assert resolve_stage1_run_mode(ctx) == resolve_run_mode(ctx)


# --------------------------------------------------------------------------- #
# 2. Live runs: today's report, byte for byte
# --------------------------------------------------------------------------- #
def test_clock_derived_live_run_reports_run_mode_live(monkeypatch, tmp_path):
    _freeze(monkeypatch, _BEIJING_1530)

    report, summary = _run_stage1(tmp_path)

    assert report["execution_timing"]["run_mode"] == "live"
    assert report["execution_timing"]["window_gate"] == "in_window"
    assert summary["execution_timing"]["run_mode"] == "live"
    # The provenance of a live in-window run is untouched by the fix.
    assert report["execution_timing"]["provenance"] == "in_window"


def test_explicit_same_day_as_of_stays_live(monkeypatch, tmp_path):
    """The shape the web layer sends: an explicit `--as-of-date` EQUAL to the session."""
    _freeze(monkeypatch, _BEIJING_1530)

    report, summary = _run_stage1(tmp_path, as_of=_AS_OF_PUBLISHED)

    assert report["execution_timing"]["run_mode"] == "live"
    assert summary["execution_timing"]["run_mode"] == "live"


# --------------------------------------------------------------------------- #
# 3. A replay says so, in the report it writes
# --------------------------------------------------------------------------- #
def test_replay_run_reports_the_non_live_marker_in_the_written_report(monkeypatch, tmp_path):
    """`--as-of-date` earlier than the clock's session => `run_mode="replay"` on disk."""
    _freeze(monkeypatch, _BEIJING_1530)

    report, summary = _run_stage1(tmp_path, as_of=_AS_OF_REPLAY)

    timing = report["execution_timing"]
    assert timing["run_mode"] == "replay", (
        "a replay must not write run_mode='live' into its own report"
    )
    # Not just the label: the window claim is the replay one too, so a reader cannot be
    # told "in window" about a session that was replayed after the fact.
    assert timing["window_gate"] == "not_applicable_replay"
    assert timing["provenance"] == "replay"
    assert "REPLAY" in str(timing["header_text"]).upper()
    # And it survives into `summary.json` (the block `persist_run_metadata` merges).
    assert summary["execution_timing"]["run_mode"] == "replay"


def test_replay_report_never_claims_a_live_window(monkeypatch, tmp_path):
    """The defect's user-visible half: no "live" claim anywhere in the replay report."""
    _freeze(monkeypatch, _BEIJING_1530)

    report, _summary = _run_stage1(tmp_path, as_of=_AS_OF_REPLAY)

    assert report["execution_timing"]["run_mode"] != "live"
    assert report["execution_timing"]["provenance"] != "in_window"
    assert report["execution_timing"]["in_window"] is False

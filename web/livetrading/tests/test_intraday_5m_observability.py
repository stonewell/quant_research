"""L2.4 regression tests: a 0-row 5m frame must never read as a silent PASS.

FORENSIC CONTEXT (2026-10-22 defect)
------------------------------------
Stage 2 printed ``成功加载并聚合 5m 日内特征 (标的数: 20)`` where the 20 was the
DAILY symbol count while the 5m universe was in fact empty.  Downstream readers saw
a request for intraday features and no evidence of failure, so every intraday
VWAP / Chan-5m gate passed on data that did not exist.

The fix has three parts, pinned here:

1. ``common/intraday_features.py`` marks an unfilled frame with
   ``df.attrs["intraday_5m_available"] = False``; ``summarize_intraday_5m_availability``
   CONSUMES that marker (it never re-fetches to "check").
2. Zero 5m ROWS on the as-of session => ``enabled=False`` + a WARNING, and the block
   is persisted into ``summary.json`` (``observability.intraday_5m``).
3. A stage-1 report that claims ``enabled=True`` next to "nothing measured" is
   NORMALIZED in place, so a later reader of ``stage1_health_report.json`` cannot
   see the contradictory pair.  Design §12 case 11 requires the affected symbols to
   be named in the freshness audit rather than leaving a blank that reads like
   "nothing to report".

Everything is offline: no provider, no network, no DuckDB -- the helpers take
plain mappings/DataFrames and the one service method is called on a bare instance.
"""

import os
import sys

import pandas as pd
import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from execution_service import (  # noqa: E402
    INTRADAY_5M_WARN_MARKER,
    ExecutionService,
    reconcile_intraday_5m_observability,
    summarize_intraday_5m_availability,
)


def _unavailable_frame():
    df = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    df.attrs["intraday_5m_available"] = False
    return df


def _service():
    # The methods under test touch no storage; a bare instance keeps this offline.
    return ExecutionService(None)


# ---------------------------------------------------------------------------
# 1. The attrs marker is consumed, not re-derived
# ---------------------------------------------------------------------------


def test_zero_row_frames_are_disabled_and_warn(capsys):
    block = summarize_intraday_5m_availability(
        {"600519.SH": _unavailable_frame(), "000858.SZ": None},
        "2026-10-23",
    )

    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["symbols_with_rows"] == []
    assert block["symbols_unavailable"] == ["000858.SZ", "600519.SH"]

    out = capsys.readouterr().out
    assert "5m 日内特征不可用" in out
    assert "enabled=False" in out
    assert block.get("warning") and "WARNING" in block["warning"]


def test_marker_wins_over_accidentally_non_empty_rows():
    """An attrs-marked frame stays unavailable even if rows were handed over."""
    df = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2026-10-23 10:00"]))
    df.attrs["intraday_5m_available"] = False

    block = summarize_intraday_5m_availability({"AAA": df}, "2026-10-23")
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["symbols_unavailable"] == ["AAA"]


def test_real_rows_on_the_as_of_session_stay_enabled():
    idx = pd.DatetimeIndex(["2026-10-23 10:00", "2026-10-23 10:05", "2026-10-22 10:00"])
    block = summarize_intraday_5m_availability(
        {"AAA": pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)},
        "2026-10-23",
    )

    # Only rows ON the as-of session count -- not the previous session's bars.
    assert block["rows_5m"] == 2
    assert block["enabled"] is True
    assert block["symbols_with_rows"] == ["AAA"]
    assert "warning" not in block


# ---------------------------------------------------------------------------
# 2. reconcile_*: the persisted verdict, from either artifact
# ---------------------------------------------------------------------------


def test_reconcile_uses_the_stage2_stdout_marker():
    block = reconcile_intraday_5m_observability(
        None, stdout=f"[WARNING] {INTRADAY_5M_WARN_MARKER} (enabled=False): as_of=...", as_of_date="2026-10-23"
    )

    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["source"] == "stdout"
    assert block["requested"] is True
    assert "0" in block["reason"]


def test_reconcile_normalizes_a_stage1_report_that_claims_enabled():
    """§12 case 11: `enabled=True` next to zero measured symbols is rewritten."""
    metrics = {
        "enabled": True,
        "symbols_measured": 20,
        "symbols_5m_available": 0,
        "symbols_missing_features": ["600519.SH", "000858.SZ"],
        "unavailable_reason": "5m 缓存缺失",
    }
    report = {"intraday_5m_metrics": metrics}

    block = reconcile_intraday_5m_observability(report, as_of_date="2026-10-23")

    assert block["enabled"] is False
    assert block["source"] == "stage1_health_report.intraday_5m_metrics"
    assert block["symbols_unavailable"] == ["000858.SZ", "600519.SH"]
    assert block["reason"] == "5m 缓存缺失"

    # The RAW report is normalized too: a later reader cannot see the contradiction.
    assert metrics["enabled"] is False
    assert metrics["intraday_5m_available"] is False


def test_reconcile_keeps_a_genuinely_available_report_enabled():
    metrics = {"enabled": True, "symbols_measured": 5, "symbols_5m_available": 5}
    report = {"intraday_5m_metrics": metrics}

    block = reconcile_intraday_5m_observability(report, as_of_date="2026-10-23")

    assert block["enabled"] is True
    assert metrics["enabled"] is True, "an available report must not be rewritten"
    assert "intraday_5m_available" not in metrics


def test_reconcile_without_artifacts_is_not_checked_not_pass():
    block = reconcile_intraday_5m_observability(None, stdout="", as_of_date="2026-10-23")

    assert block["requested"] is False
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["source"] == "none"


# ---------------------------------------------------------------------------
# 2b. (D11 / §4.50.3) THE CHILD'S OWN BLOCK WINS -- or the hard overwrite turns it
#     back into a less specific source and a real 5m round is unobservable
# ---------------------------------------------------------------------------
def _child_block(**overrides):
    block = {
        "requested": True,
        "enabled": True,
        "rows_5m": 48,
        "as_of_date": "2026-10-23",
        "symbols_with_rows": ["600519.SH"],
        "symbols_unavailable": [],
        "reason": None,
        "source": "multi_strategy_netted",
    }
    block.update(overrides)
    return block


def test_reconcile_prefers_the_childs_own_block_over_stdout_and_health():
    """State 2 (§4.50.4): a REAL child block must survive reconciliation verbatim.

    Before D11 the netted call site passed `health_report=None`, so the only reachable
    sources were `stdout` (needs the WARN marker) and `none` -- and `_apply_intraday_observability`
    HARD-OVERWRITES `observability["intraday_5m"]`, so a block the child had already persisted
    was replaced by `"none"`. `re-break`: drop `child_report` from the priority chain -> this
    test reads `source == "stdout"` and fails (verified red).
    """
    stdout = f"[WARNING] {INTRADAY_5M_WARN_MARKER} (enabled=False): as_of=..."
    health = {"intraday_5m_metrics": {"enabled": True, "symbols_measured": 3, "symbols_5m_available": 1}}

    block = reconcile_intraday_5m_observability(
        health,
        stdout=stdout,
        as_of_date="2026-10-23",
        child_report=_child_block(),
    )

    assert block["source"] == "multi_strategy_netted"
    assert block["requested"] is True
    assert block["enabled"] is True
    assert block["rows_5m"] == 48
    assert block["symbols_with_rows"] == ["600519.SH"]


def test_reconcile_falls_through_without_a_child_block():
    """Absent child block => the OLD behaviour, unchanged (Stage 1/2 call sites)."""
    stdout = f"[WARNING] {INTRADAY_5M_WARN_MARKER} (enabled=False): as_of=..."
    from_stdout = reconcile_intraday_5m_observability(
        None, stdout=stdout, as_of_date="2026-10-23", child_report=None
    )
    assert from_stdout["source"] == "stdout"

    from_health = reconcile_intraday_5m_observability(
        {"intraday_5m_metrics": {"enabled": False, "symbols_measured": 2, "symbols_5m_available": 0}},
        stdout="",
        as_of_date="2026-10-23",
        child_report=None,
    )
    assert from_health["source"] == "stage1_health_report.intraday_5m_metrics"

    # An empty/malformed block is "nothing to go on", not "requested=False".
    for junk in ({}, ["not", "a", "mapping"], {"requested": True}):
        block = reconcile_intraday_5m_observability(
            None, stdout=stdout, as_of_date="2026-10-23", child_report=junk
        )
        assert block["source"] == "stdout", junk


def test_reconcile_respects_a_child_block_that_says_not_requested():
    """`requested=False` is a WELL-FORMED answer -- a stray stdout marker must not revive 5m.

    The zero-request state (§4.50.4 clause 1) is `requested=False`, so a run that honestly
    did not ask for 5m must not be relabelled "requested but unavailable" by an unrelated
    marker elsewhere in the child's stdout.
    """
    block = reconcile_intraday_5m_observability(
        None,
        stdout=f"[WARNING] {INTRADAY_5M_WARN_MARKER} (enabled=False): as_of=...",
        as_of_date="2026-10-23",
        child_report=_child_block(requested=False, enabled=False, rows_5m=0),
    )

    assert block["requested"] is False
    assert block["enabled"] is False
    assert block["source"] == "multi_strategy_netted"


def test_reconcile_ignores_a_stale_child_block():
    """A block inherited from ANOTHER session is not evidence for THIS round."""
    block = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date="2026-10-23",
        child_report=_child_block(as_of_date="2026-10-22"),
    )

    assert block["source"] == "none", "a stale block must fall through, not be trusted"
    assert block["requested"] is False


# ---------------------------------------------------------------------------
# 2c. (B1) SAME-DAY RERUN: the block must be from THIS round, not the last one
#     in the same reused `multi_netted_<matrix>` directory
# ---------------------------------------------------------------------------
def test_reconcile_rejects_a_same_day_block_generated_before_this_round():
    """`as_of_date` matches (same session) -- only `generated_at` can separate the rounds.

    The matrix run directory is fixed per matrix+date and never cleaned, so a rerun finds
    the previous round's block in `summary.json`. Monotonic freshness is the tie the old
    `as_of_date`-only check could never draw.
    """
    round_start = "2026-10-23T07:30:00+00:00"
    stale = _child_block(generated_at="2026-10-23T06:00:00+00:00")
    fresh = _child_block(generated_at="2026-10-23T07:31:00+00:00")

    rejected = reconcile_intraday_5m_observability(
        None, stdout="", as_of_date="2026-10-23", child_report=stale, round_started_at=round_start
    )
    assert rejected["source"] == "none"
    assert rejected["requested"] is False
    assert rejected["enabled"] is False

    accepted = reconcile_intraday_5m_observability(
        None, stdout="", as_of_date="2026-10-23", child_report=fresh, round_started_at=round_start
    )
    assert accepted["source"] == "multi_strategy_netted"
    assert accepted["enabled"] is True


def test_reconcile_fails_closed_without_a_usable_generated_at():
    """At the netting call site an UNPROVABLE timestamp is stale, never fresh.

    Missing, unparseable and timezone-naive `generated_at` all fail closed. A stray
    timezone-naive string could belong to any offset; assuming UTC is how a stale block
    would sneak through.
    """
    round_start = "2026-10-23T07:30:00+00:00"
    for bad in ({}, {"generated_at": ""}, {"generated_at": "not-a-timestamp"}, {"generated_at": "2026-10-23T09:00:00"}):
        block = reconcile_intraday_5m_observability(
            None,
            stdout="",
            as_of_date="2026-10-23",
            child_report=_child_block(**bad),
            round_started_at=round_start,
        )
        assert block["source"] == "none", bad
        assert block["requested"] is False, bad

    # Stage-1/Stage-2 call sites pass no `round_started_at`: behaviour is unchanged there
    # (a block with no `generated_at` is still accepted on the older call sites).
    unchanged = reconcile_intraday_5m_observability(
        None, stdout="", as_of_date="2026-10-23", child_report=_child_block()
    )
    assert unchanged["source"] == "multi_strategy_netted"


def test_reconcile_accepts_a_tz_aware_generated_at_datetime_object():
    """A `datetime` round start is accepted (that is what the netting call site passes)."""
    import datetime as dt

    block = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date="2026-10-23",
        child_report=_child_block(generated_at="2026-10-23T07:31:00+00:00"),
        round_started_at=dt.datetime(2026, 10, 23, 7, 30, tzinfo=dt.timezone.utc),
    )
    assert block["source"] == "multi_strategy_netted"


def test_reconcile_never_enables_a_child_block_with_zero_rows():
    """`enabled=True` next to zero measured rows is rewritten, child block or not."""
    block = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date="2026-10-23",
        child_report=_child_block(enabled=True, rows_5m=0, reason="provider 不支持 5m"),
    )

    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["reason"] == "provider 不支持 5m"
    assert block["source"] == "multi_strategy_netted"


def test_apply_intraday_observability_does_not_erase_a_child_block():
    """The persistence step writes the RECONCILED block, never a hard-coded `"none"`."""
    summary = {"freshness_summary": {"status": "PASS", "expected_session": "2026-10-23"}}
    reconciled = reconcile_intraday_5m_observability(
        None, stdout="", as_of_date="2026-10-23", child_report=_child_block()
    )

    _service()._apply_intraday_observability(summary, reconciled)

    assert summary["observability"]["intraday_5m"]["source"] == "multi_strategy_netted"
    assert summary["observability"]["intraday_5m"]["enabled"] is True
    assert "intraday_5m" not in summary["freshness_summary"]


# ---------------------------------------------------------------------------
# 3. What lands in summary.json (the reader-facing surface)
# ---------------------------------------------------------------------------


def test_case11_unavailable_symbols_are_visible_in_the_freshness_audit():
    summary = {"freshness_summary": {"status": "PASS", "expected_session": "2026-10-23"}}
    block = summarize_intraday_5m_availability(
        {"600519.SH": _unavailable_frame()}, "2026-10-23", reason="5m 缓存缺失"
    )

    _service()._apply_intraday_observability(summary, block)

    assert summary["observability"]["intraday_5m"]["enabled"] is False
    fresh_5m = summary["freshness_summary"]["intraday_5m"]
    assert fresh_5m["status"] == "UNAVAILABLE"
    assert fresh_5m["enabled"] is False
    assert fresh_5m["symbols_unavailable"] == ["600519.SH"]
    assert fresh_5m["reason"] == "5m 缓存缺失"


def test_an_available_verdict_does_not_fabricate_a_freshness_anomaly():
    summary = {"freshness_summary": {"status": "PASS"}}
    idx = pd.DatetimeIndex(["2026-10-23 10:00"])
    block = summarize_intraday_5m_availability(
        {"AAA": pd.DataFrame({"Close": [1.0]}, index=idx)}, "2026-10-23"
    )

    _service()._apply_intraday_observability(summary, block)

    assert summary["observability"]["intraday_5m"]["enabled"] is True
    assert "intraday_5m" not in summary["freshness_summary"]


# ---------------------------------------------------------------------------
# 4. Archive key vs summary.json (L2.4's first acceptance criterion)
# ---------------------------------------------------------------------------


def test_archive_dir_must_be_keyed_by_the_valuation_session():
    assert ExecutionService._assert_archive_keyed_by(
        os.path.join("/tmp", "runs", "2026-10-23", "multi_netted_m"), "2026-10-23"
    ) is None

    with pytest.raises(ValueError, match="not keyed by the valuation session"):
        ExecutionService._assert_archive_keyed_by(
            os.path.join("/tmp", "runs", "2026-10-22", "multi_netted_m"), "2026-10-23"
        )


def _stub_clock_block(monkeypatch, valuation_date):
    """Isolate MERGE semantics from the market clock (no storage / no wall clock)."""
    def _block(self, valuation_date_, universe_file, custom_symbols, as_of_source):
        return {
            "utc_now": "2026-10-23T10:00:00+00:00",
            "local_now": "2026-10-23T18:00:00+08:00",
            "session": "closed",
            "as_of_trading_date": valuation_date_,
            "as_of_source": as_of_source,
            "expected_last_session": valuation_date_,
            "markets": ["china"],
            "source": "web/livetrading",
        }

    monkeypatch.setattr(ExecutionService, "_clock_observability", _block)


def test_observability_merge_never_overwrites_the_childs_own_blocks(monkeypatch):
    """The child's `clock` / `freshness_summary` are authoritative; we only fill gaps."""
    child_clock = {
        "utc_now": "2026-10-23T09:00:00+00:00",
        "local_now": "2026-10-23T17:00:00+08:00",
        "session": "open",
        "as_of_trading_date": "2026-10-23",
        "source": "scripts/run_live_strategy.py",
    }
    child_freshness = {"status": "PASS", "expected_session": "2026-10-23", "source": "child"}
    summary = {"clock": dict(child_clock), "freshness_summary": dict(child_freshness)}
    _stub_clock_block(monkeypatch, "2026-10-23")

    _service()._merge_observability(
        summary,
        valuation_date="2026-10-23",
        universe_file=None,
        custom_symbols=["600519.SH"],
        as_of_source="market_clock",
        stage="stage1",
    )

    # Child values survive verbatim...
    for key, value in child_clock.items():
        assert summary["clock"][key] == value
    assert summary["freshness_summary"] == child_freshness
    # ...and only the fields it could not know about are added.
    assert summary["clock"]["as_of_source"] == "market_clock"
    assert summary["clock"]["expected_last_session"] == "2026-10-23"


def test_missing_freshness_is_not_checked_rather_than_pass(monkeypatch):
    summary = {}
    _stub_clock_block(monkeypatch, "2026-10-23")

    _service()._merge_observability(
        summary,
        valuation_date="2026-10-23",
        universe_file=None,
        custom_symbols=["600519.SH"],
        as_of_source="explicit",
        stage="stage2",
    )

    assert summary["freshness_summary"]["status"] == "NOT_CHECKED"
    assert summary["freshness_summary"]["status"] != "PASS"

"""Offline tests for `scripts/check_live_run_integrity.py`.

Every fixture is a miniature live run archive built in `tmp_path` -- two JSON files per
round, no market data, no network, and no real calendar (the session list is injected via
`trading_days=[...]`, so nothing here can break because a holiday table changes).

The suite pins the incident that motivated the auditor, the exit-code contract, and the
supersession rule (one broken signal = one HIGH finding, not two):

* (a) a clean week -- zero findings, exit 0 even with `--strict`;
* (b) the real 2026-11-13 / 2026-11-16 shape -- two after-open refusals on Friday, still
  only ONE `MISSED_SESSION`, and the finding names 2026-11-16 as the open that received
  no fill instruction at all;
* (b2) the real archive's *actual* Friday shape -- a `stage2` round with no
  `deploy_status` field produces no `DEPLOY_FAILED` but still the `MISSED_SESSION`;
* (c) `DEPLOY_FAILED` picks the traceback's exception line, not a `!!!!` banner;
* (d) ticket without a post-trade book, and a ticket whose `execution_date` is stale;
* (e) duplicate same-day shipments;
* (f) weekend / injected-holiday exclusion;
* (g) exit-code contract: 0 clean, 0 dirty-without-strict, 1 dirty-with-strict, 2 wrong
  tree / inverted range / unknown market;
* plus the JSON payload shape, `--json-out`, the legacy LIST ticket fallback, and the
  read-only guarantee (no archived file is touched).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_live_run_integrity.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_live_run_integrity", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `@dataclass` resolves `sys.modules[cls.__module__].__dict__` at decoration time, so
    # the module must be registered before `exec_module` -- otherwise loading a script by
    # path (no `scripts` package) raises AttributeError on the first dataclass.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


integrity = _load_script()

#: Abridged but verbatim refusal text from `2026-11-13/192803/summary.json`.
AFTER_OPEN_119 = (
    "Traceback (most recent call last):\n"
    '  File "/repo/scripts/run_live_strategy.py", line 1212, in enforce_execution_executability\n'
    "    raise MissedExecutionOpenError(\n"
    "common.live_execution_timing.MissedExecutionOpenError: [EXECUTION TIMING] "
    "executable=false timing_violation=after_open disposition=refused lateness_minutes=119.2 "
    "as_of=2026-11-13 execution_date=2026-11-16 planned_open=2026-11-16T09:30:00+08:00\n"
    "票据未写出, 需重跑 (declared next-open fill already opened)\n"
)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def ticket(as_of: str, execution_date: str, orders=None) -> dict:
    return {
        "ticket_schema": "v2",
        "execution_session": "as_of+1",
        "price_reference": "next_open",
        "as_of": as_of,
        "execution_date": execution_date,
        "orders": orders if orders is not None else [{"symbol": "600362.SH", "action": "SELL"}],
    }


def make_instance(
    root: Path,
    day: str,
    name: str,
    *,
    deploy_status="SUCCESS",
    as_of=None,
    ticket_payload=None,
    write_ticket=True,
    post_holdings=True,
    meta_complete=True,
    stderr="",
    stdout="",
    stage="stage2",
) -> Path:
    """One `<day>/<name>` round with the exact files the auditor reads."""
    path = root / day / name
    path.mkdir(parents=True, exist_ok=True)
    summary = {
        "date": day,
        "stage": stage,
        "as_of": as_of or day,
        "deploy_stderr": stderr,
        "deploy_stdout": stdout,
    }
    if deploy_status is not None:
        summary["deploy_status"] = deploy_status
    _write_json(path / "summary.json", summary)
    if write_ticket:
        payload = ticket_payload
        if payload is None:
            payload = ticket(as_of or day, as_of or day)
        _write_json(path / "live_trading_ticket.json", payload)
    if post_holdings:
        _write_json(path / "post_holdings.json", {"600362.SH": 100})
        if meta_complete is not None:
            _write_json(
                path / "post_holdings.meta.json",
                {"as_of": as_of or day, "complete": meta_complete},
            )
    return path


def clean_week_archive(root: Path) -> list[str]:
    """(a) Mon..Fri 2026-11-09..11-13: every session ships, every book is attested."""
    sessions = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    for index, day in enumerate(sessions):
        following = sessions[index + 1] if index + 1 < len(sessions) else "2026-11-16"
        make_instance(root, day, "205000", ticket_payload=ticket(day, following))
    return sessions


def codes_of(payload: dict, counted_only: bool = True) -> list[str]:
    return [
        finding["code"]
        for finding in payload["findings"]
        if finding.get("counted", True) or not counted_only
    ]


# ---------------------------------------------------------------------------
# (a) a clean week
# ---------------------------------------------------------------------------
def test_clean_week_has_no_findings(tmp_path, capsys):
    sessions = clean_week_archive(tmp_path)
    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date.fromisoformat(day) for day in sessions]
    )

    assert payload["findings"] == []
    assert payload["verdict"]["clean"] is True
    assert payload["scanned"]["sessions_expected"] == 5
    assert payload["scanned"]["sessions_covered"] == 5
    assert payload["scanned"]["unfilled_execution_sessions"] == []

    exit_code = integrity.main(["--runs-root", str(tmp_path), "--strict"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "VERDICT: CLEAN" in captured.out
    assert "CLEAN:" in captured.out


# ---------------------------------------------------------------------------
# (b) the real 2026-11-13 -> 2026-11-16 shape
# ---------------------------------------------------------------------------
def test_friday_signal_lost_and_monday_open_unfilled(tmp_path, capsys):
    """Two after-open refusals on Friday = ONE missed session, not two.

    Monday's evening round DOES ship (as_of 2026-11-16, execution_date 2026-11-17), so
    Monday as a *session* is covered -- but nothing in the archive carries
    `execution_date=2026-11-16`, i.e. Monday's OPEN had no fill instruction at all. That
    is reported on the Friday finding, and in `scanned.unfilled_execution_sessions`.
    """
    make_instance(
        tmp_path, "2026-11-12", "162857", ticket_payload=ticket("2026-11-12", "2026-11-13")
    )
    make_instance(
        tmp_path, "2026-11-13", "144835",
        deploy_status="FAILED", write_ticket=False, post_holdings=False,
        stderr=AFTER_OPEN_119.replace("119.2", "98.4").replace("2026-11-13", "2026-11-13"),
        stage="stage2",
    )
    make_instance(
        tmp_path, "2026-11-13", "192803",
        deploy_status="FAILED", write_ticket=False, post_holdings=False, stderr=AFTER_OPEN_119,
    )
    make_instance(
        tmp_path, "2026-11-16", "124508", ticket_payload=ticket("2026-11-16", "2026-11-17")
    )
    sessions = [date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)]

    payload = integrity.audit_run_archive(tmp_path, trading_days=sessions)

    missed = [f for f in payload["findings"] if f["code"] == "MISSED_SESSION"]
    refused = [f for f in payload["findings"] if f["code"] == "TIMING_REFUSED"]
    assert len(missed) == 1
    assert missed[0]["date"] == "2026-11-13"
    assert missed[0]["severity"] == "HIGH"
    assert missed[0]["instance"] == "144835, 192803"
    # The acceptance criterion, in one assertion.
    assert "2026-11-16" in missed[0]["detail"]
    assert "无任何可执行票据" in missed[0]["detail"]

    # Both refusals are reported raw; both are superseded by the missed session.
    assert len(refused) == 2
    assert all(f["counted"] is False for f in refused)
    assert all(f["superseded_by"] == "MISSED_SESSION@2026-11-13" for f in refused)
    assert payload["summary"]["counts_by_code"]["TIMING_REFUSED"] == 0
    assert payload["summary"]["superseded_findings"] == 2
    assert payload["summary"]["counts_by_code"]["MISSED_SESSION"] == 1
    # lateness_minutes is parsed out of the refusal text, per instance.
    details = " ".join(f["detail"] for f in refused)
    assert "lateness_minutes=119.2" in details
    assert "lateness_minutes=98.4" in details
    # A refused round writes no ticket, so execution_date exists only in the text.
    assert "execution_date=2026-11-16" in details

    # Monday's session is covered, its OPEN is not.
    assert payload["scanned"]["sessions_covered"] == 2
    assert payload["scanned"]["unfilled_execution_sessions"] == ["2026-11-16"]

    assert integrity.main(["--runs-root", str(tmp_path), "--strict"]) == 1
    captured = capsys.readouterr()
    assert "[sup]" in captured.out
    assert "VERDICT: DIRTY" in captured.out
    assert "缺票交易日 2026-11-13" in captured.out
    assert "无票可填的开盘日 2026-11-16" in captured.out


def test_stage2_round_without_deploy_status_is_not_deploy_failed(tmp_path):
    """The real 2026-11-13/144835: stage2, gate GO, no deploy_status, no ticket.

    Absent is not FAILED -- reporting it as DEPLOY_FAILED would invent an error kind the
    archive does not contain. It still leaves the day without a ticket, so exactly one
    MISSED_SESSION must appear, and its detail must name the missing field.
    """
    make_instance(
        tmp_path, "2026-11-13", "144835",
        deploy_status=None, write_ticket=False, post_holdings=False, stage="stage2",
    )

    payload = integrity.audit_run_archive(tmp_path, trading_days=[date(2026, 11, 13)])

    assert codes_of(payload) == ["MISSED_SESSION"]
    detail = payload["findings"][0]["detail"]
    assert "deploy_status缺失(stage2)" in detail
    assert "DEPLOY_FAILED" not in codes_of(payload, counted_only=False)


# ---------------------------------------------------------------------------
# (c) DEPLOY_FAILED carries the diagnostic line
# ---------------------------------------------------------------------------
def test_deploy_failed_uses_traceback_line_not_banner(tmp_path):
    banner = "!" * 110
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/repo/scripts/run_live_strategy.py", line 3559, in <module>\n'
        "    main()\n"
        "    raise LiveGateDegradedError(\n"
        "LiveGateDegradedError: \n"
        f"{banner}\n"
        "[REFUSED / 拒绝出票] Stage 1 门禁净值不可用：缺失 ≠ 未触发。\n"
        f"{banner}\n"
    )
    # Same day also ships a healthy round, so only the deploy failure is reported.
    make_instance(tmp_path, "2026-11-09", "204956", ticket_payload=ticket("2026-11-09", "2026-11-10"))
    make_instance(
        tmp_path, "2026-11-09", "204716",
        deploy_status="FAILED", write_ticket=False, post_holdings=False, stderr=stderr,
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 9), date(2026, 11, 10)]
    )

    assert codes_of(payload) == ["DEPLOY_FAILED"]
    finding = payload["findings"][0]
    assert finding["instance"] == "204716"
    assert finding["severity"] == "MEDIUM"
    assert "LiveGateDegradedError" in finding["detail"]
    assert "UNKNOWN" in finding["detail"] or "缺失" in finding["detail"]
    assert "!!!" not in finding["detail"]
    assert finding["detail"].count("\n") == 0


def test_timing_refusal_on_a_covered_day_stays_counted(tmp_path):
    """The real 2026-11-12: a late retry refused, but the day did ship earlier.

    No MISSED_SESSION exists for that date, so nothing supersedes the refusal -- it is
    counted. That is the difference between "the mechanism of a lost day" (superseded)
    and "a near miss on a day that still traded" (independent HIGH).
    """
    make_instance(
        tmp_path, "2026-11-12", "162857", ticket_payload=ticket("2026-11-12", "2026-11-13")
    )
    make_instance(
        tmp_path, "2026-11-12", "195821",
        deploy_status="FAILED", write_ticket=False, post_holdings=False,
        stderr=AFTER_OPEN_119.replace("119.2", "149.4"),
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 12), date(2026, 11, 13)]
    )

    refused = [f for f in payload["findings"] if f["code"] == "TIMING_REFUSED"]
    assert len(refused) == 1
    assert refused[0].get("counted", True) is True
    assert "lateness_minutes=149.4" in refused[0]["detail"]
    assert payload["summary"]["counts_by_code"]["TIMING_REFUSED"] == 1
    assert payload["summary"]["superseded_findings"] == 0
    assert codes_of(payload) == ["TIMING_REFUSED"]


def test_missed_execution_open_is_never_also_deploy_failed(tmp_path):
    make_instance(
        tmp_path, "2026-11-13", "192803",
        deploy_status="FAILED", write_ticket=False, post_holdings=False, stderr=AFTER_OPEN_119,
    )

    payload = integrity.audit_run_archive(tmp_path, trading_days=[date(2026, 11, 13)])

    all_codes = codes_of(payload, counted_only=False)
    assert "TIMING_REFUSED" in all_codes
    assert "DEPLOY_FAILED" not in all_codes


# ---------------------------------------------------------------------------
# (d) ticket without a post-trade book / stale execution_date
# ---------------------------------------------------------------------------
def test_ticket_without_post_holdings(tmp_path):
    make_instance(
        tmp_path, "2026-11-09", "204956",
        ticket_payload=ticket("2026-11-09", "2026-11-10"), post_holdings=False,
    )

    payload = integrity.audit_run_archive(
        tmp_path,
        from_date=date(2026, 11, 9),
        to_date=date(2026, 11, 12),
        trading_days=[date(2026, 11, 9)],
    )

    assert sorted(codes_of(payload)) == ["POST_HOLDINGS_MISSING", "TICKET_UNFILLED"]
    by_code = {f["code"]: f for f in payload["findings"]}
    assert by_code["POST_HOLDINGS_MISSING"]["severity"] == "MEDIUM"
    assert "post_holdings.json" in by_code["POST_HOLDINGS_MISSING"]["detail"]
    assert by_code["TICKET_UNFILLED"]["severity"] == "LOW"
    assert "2026-11-10" in by_code["TICKET_UNFILLED"]["detail"]
    # The day itself is covered, so no MISSED_SESSION is invented.
    assert "MISSED_SESSION" not in codes_of(payload, counted_only=False)


def test_post_holdings_meta_complete_false_is_a_finding(tmp_path):
    make_instance(
        tmp_path, "2026-11-09", "204956",
        ticket_payload=ticket("2026-11-09", "2026-11-10"), meta_complete=False,
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 9), date(2026, 11, 10)]
    )

    assert codes_of(payload) == ["POST_HOLDINGS_MISSING"]
    assert "complete=False" in payload["findings"][0]["detail"]


def test_post_holdings_meta_absent_is_a_finding(tmp_path):
    """Legacy rounds wrote no meta attestation -- unverifiable, therefore reported."""
    make_instance(
        tmp_path, "2026-10-29", "215823",
        ticket_payload=ticket("2026-10-29", "2026-10-30"), meta_complete=None,
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 10, 29), date(2026, 10, 30)]
    )

    assert codes_of(payload) == ["POST_HOLDINGS_MISSING"]
    assert "meta.json 缺失" in payload["findings"][0]["detail"]


def test_stale_execution_date_is_ticket_unfilled(tmp_path):
    make_instance(
        tmp_path, "2026-11-09", "204956",
        ticket_payload=ticket("2026-11-09", "2026-11-09"),
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 9), date(2026, 11, 10)]
    )

    assert codes_of(payload) == ["TICKET_UNFILLED"]
    finding = payload["findings"][0]
    assert finding["severity"] == "LOW"
    assert "早于" in finding["detail"]


# ---------------------------------------------------------------------------
# (e) duplicate same-day shipments
# ---------------------------------------------------------------------------
def test_duplicate_instances_for_one_as_of(tmp_path):
    make_instance(tmp_path, "2026-11-11", "205137", ticket_payload=ticket("2026-11-11", "2026-11-12"))
    make_instance(tmp_path, "2026-11-11", "234433", ticket_payload=ticket("2026-11-11", "2026-11-12"))

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 11), date(2026, 11, 12)]
    )

    assert codes_of(payload) == ["DUPLICATE_INSTANCES"]
    finding = payload["findings"][0]
    assert finding["severity"] == "LOW"
    assert finding["instance"] == "205137, 234433"
    assert payload["summary"]["counts_by_code"]["DUPLICATE_INSTANCES"] == 1


# ---------------------------------------------------------------------------
# (f) weekend / injected-holiday exclusion
# ---------------------------------------------------------------------------
def test_weekend_is_not_a_session(tmp_path):
    """Fri 11-13 -> Mon 11-16 with nothing on 11-14/15: no missed session is invented."""
    make_instance(tmp_path, "2026-11-13", "205000", ticket_payload=ticket("2026-11-13", "2026-11-16"))
    make_instance(tmp_path, "2026-11-16", "124508", ticket_payload=ticket("2026-11-16", "2026-11-17"))

    payload = integrity.audit_run_archive(tmp_path, use_calendar=False)

    assert payload["scanned"]["trading_days"] == ["2026-11-13", "2026-11-16"]
    assert payload["scanned"]["calendar_source"] == "weekdays"
    assert payload["findings"] == []


def test_injected_holiday_is_not_a_session(tmp_path):
    """An injected session list is authoritative -- no dependency on the real table."""
    make_instance(tmp_path, "2026-11-09", "205000", ticket_payload=ticket("2026-11-09", "2026-11-11"))
    make_instance(tmp_path, "2026-11-11", "205000", ticket_payload=ticket("2026-11-11", "2026-11-12"))

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 11, 9), date(2026, 11, 11), date(2026, 11, 12)]
    )

    assert "2026-11-10" not in payload["scanned"]["trading_days"]
    assert "2026-11-10" not in [f["date"] for f in payload["findings"]]
    assert payload["scanned"]["calendar_source"] == "injected"


def test_holiday_weekday_with_no_run_is_a_missed_session_when_expected(tmp_path):
    """Control for the test above: an EXPECTED session with no instance IS reported."""
    make_instance(tmp_path, "2026-11-09", "205000", ticket_payload=ticket("2026-11-09", "2026-11-10"))

    payload = integrity.audit_run_archive(
        tmp_path,
        to_date=date(2026, 11, 10),
        trading_days=[date(2026, 11, 9), date(2026, 11, 10)],
    )

    missed = [f for f in payload["findings"] if f["code"] == "MISSED_SESSION"]
    assert [f["date"] for f in missed] == ["2026-11-10"]
    assert missed[0]["instance"] == "-"
    assert "无任何运行实例" in missed[0]["detail"]


# ---------------------------------------------------------------------------
# (g) exit-code contract
# ---------------------------------------------------------------------------
def test_missing_runs_root_is_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        integrity.main([])
    assert excinfo.value.code == 2


def test_nonexistent_runs_root_is_exit_2(tmp_path, capsys):
    assert integrity.main(["--runs-root", str(tmp_path / "does-not-exist")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_archive_without_instance_dirs_is_exit_2(tmp_path, capsys):
    (tmp_path / "2026-11-16").mkdir()
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")

    assert integrity.main(["--runs-root", str(tmp_path)]) == 2
    assert "does not look like a live run archive" in capsys.readouterr().err


def test_inverted_range_is_exit_2(tmp_path, capsys):
    clean_week_archive(tmp_path)
    assert integrity.main([
        "--runs-root", str(tmp_path), "--from", "2026-11-13", "--to", "2026-11-09",
    ]) == 2
    assert "is after" in capsys.readouterr().err


def test_malformed_date_is_exit_2(tmp_path):
    clean_week_archive(tmp_path)
    assert integrity.main(["--runs-root", str(tmp_path), "--from", "2026-11-9"]) == 2


def test_unknown_market_is_exit_2(tmp_path, capsys):
    clean_week_archive(tmp_path)
    assert integrity.main(["--runs-root", str(tmp_path), "--market", "atlantis"]) == 2
    assert "unknown market" in capsys.readouterr().err


def test_dirty_archive_without_strict_exits_0_but_still_prints(tmp_path, capsys):
    make_instance(tmp_path, "2026-11-09", "204956", post_holdings=False)

    exit_code = integrity.main(["--runs-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "POST_HOLDINGS_MISSING" in captured.out
    assert "VERDICT: DIRTY" in captured.out


# ---------------------------------------------------------------------------
# payload shape, --json-out, legacy ticket fallback, read-only guarantee
# ---------------------------------------------------------------------------
def test_json_payload_shape_and_json_out(tmp_path):
    out = tmp_path / "audit.json"
    make_instance(
        tmp_path, "2026-11-09", "204716",
        deploy_status="FAILED", write_ticket=False, post_holdings=False,
        stderr="ValueError: boom\n",
    )
    make_instance(tmp_path, "2026-11-09", "204956", ticket_payload=ticket("2026-11-09", "2026-11-10"))

    exit_code = integrity.main([
        "--runs-root", str(tmp_path),
        "--from", "2026-11-09", "--to", "2026-11-10",
        "--no-calendar", "--json-out", str(out), "--strict",
    ])
    assert exit_code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert set(payload) == {"runs_root", "findings", "summary", "scanned", "verdict"}
    scanned = payload["scanned"]
    assert scanned["from"] == "2026-11-09" and scanned["to"] == "2026-11-10"
    assert scanned["instances"] == 2
    assert scanned["sessions_expected"] == 2
    assert scanned["sessions_covered"] == 1
    assert scanned["calendar_source"] == "weekdays"

    summary = payload["summary"]
    assert set(summary["counts_by_code"]) == set(integrity.FINDING_CODES)
    assert summary["counts_by_code"]["DEPLOY_FAILED"] == 1
    assert summary["counts_by_code"]["MISSED_SESSION"] == 1
    assert summary["counted_findings"] == 2
    assert summary["counted_findings"] == sum(summary["counts_by_severity"].values())

    for finding in payload["findings"]:
        assert set(finding) >= {"date", "instance", "code", "severity", "detail"}
        assert "\n" not in finding["detail"]
        assert finding["severity"] == integrity.SEVERITY_BY_CODE[finding["code"]]


def test_legacy_list_ticket_counts_as_usable(tmp_path):
    """Pre-2026-11-04 rounds wrote a bare list of orders -- no `as_of` envelope."""
    make_instance(
        tmp_path, "2026-10-29", "215823",
        ticket_payload=[{"symbol": "601919.SH", "action": "BUY", "shares": 2400}],
    )

    payload = integrity.audit_run_archive(
        tmp_path, trading_days=[date(2026, 10, 29), date(2026, 10, 30)]
    )

    assert payload["findings"] == []
    assert payload["scanned"]["sessions_covered"] == 1


def test_audit_is_read_only(tmp_path):
    clean_week_archive(tmp_path)
    make_instance(
        tmp_path, "2026-11-12", "162625",
        deploy_status="FAILED", write_ticket=False, post_holdings=False,
        stderr="ValueError: boom\n",
    )

    def snapshot():
        return {
            str(path.relative_to(tmp_path)): path.stat().st_mtime_ns
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    integrity.audit_run_archive(tmp_path, trading_days=[date(2026, 11, 12)])
    integrity.main(["--runs-root", str(tmp_path), "--strict"])
    assert snapshot() == before



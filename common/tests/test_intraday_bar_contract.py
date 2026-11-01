"""Offline contract tests for the M3.3 5m calibration gate.

The unit under test is the tracked module `common.intraday_bar_contract` -- every assertion, the
whitelist, the report shape and the exit-code decision. Everything here is synthetic and local:
each test builds the two plain frames the contract takes (5m bars + daily bars), runs the gate
into a temp directory, and reads back `probe_report.json` + `probe_violations.csv`. No network, no
provider, no production cache, and no DuckDB required -- the contract never touches a database.

The CLI shim that used to hold this logic lives in a research directory that `.gitignore` excludes
on purpose, and is exercised by exactly ONE skip-guarded smoke test at the bottom of this file.
That test is never the sole proof of any assertion; if the directory is absent (a clean clone) it
skips and every assertion is still covered.

Six assertions under test (docs/development_plan.md M3.3):
  A1 amount>0 share >= 99.5% per symbol x shard, whitelist disclosed
  A2 implied VWAP in [low, high]
  A3 48 bars/day, no lunch / pre-open / post-close bars, 停牌 vs 缺失 separated
  A4 END-labelled timestamps + provenance pin that refuses to be overwritten
  A5 sum(5m amount) vs daily amount < 1% (or explicit degraded 2% path)
  A6 the gate: exit 0 only if all of the above pass
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pandas as pd
import pytest

import common.intraday_bar_contract as contract

SYMBOL = "600519.SH"
GOOD_DAY = "2024-06-03"
BAD_DAY = "2024-06-04"
HALT_DAY = "2024-06-05"


# ---------------------------------------------------------------------------------------------
# synthetic builders -- the contract takes frames, so the fixtures are frames
# ---------------------------------------------------------------------------------------------
def _bar_rows(symbol: str, day: str, *, labels: list[str] | None = None,
              base: float = 100.0, volume: float = 1000.0,
              amount_mult: float = 1.0001) -> list[dict]:
    """48 END-labelled 5m bars for one day, with sane amount inside [low, high].

    `amount = volume * base * 1.0001` while `close = base`, i.e. amount is NOT volume*close
    (the item-7 guard must stay quiet) and it sits inside [base*0.999, base*1.001].
    """
    if labels is None:
        labels = contract.END_LABELS
    rows = []
    for lab in labels:
        ts = pd.Timestamp(f"{day} {lab}")
        rows.append({
            "symbol": symbol,
            "ts": ts,
            "open": base,
            "high": base * 1.001,
            "low": base * 0.999,
            "close": base,
            "volume": volume,
            "amount": volume * base * amount_mult,
            "adjustflag": "3",
        })
    return rows


def _daily_row(symbol: str, day: str, bars: list[dict], *, amount: float | None = None,
               volume: float | None = None) -> dict:
    """Daily bar consistent with the given 5m bars (defaults reconcile exactly)."""
    if not bars:
        return {"symbol": symbol, "date": pd.Timestamp(day).date(), "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.0,
                "volume": float("nan") if volume is None else volume,
                "amount": float("nan") if amount is None else amount}
    return {
        "symbol": symbol,
        "date": pd.Timestamp(day).date(),
        "open": bars[0]["open"],
        "high": max(b["high"] for b in bars),
        "low": min(b["low"] for b in bars),
        "close": bars[-1]["close"],
        "volume": sum(b["volume"] for b in bars) if volume is None else volume,
        "amount": sum(b["amount"] for b in bars) if amount is None else amount,
    }


def _bars_df(rows: list[dict]) -> pd.DataFrame:
    """Bar rows -> the frame shape the contract documents (datetime `ts`, numeric OHLCV/amount)."""
    df = pd.DataFrame(rows, columns=["symbol", "ts", "open", "high", "low", "close", "volume",
                                     "amount", "adjustflag"])
    df["ts"] = pd.to_datetime(df["ts"])
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _daily_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close",
                                       "volume", "amount"])


def _run(tmp_path, *, bars: list[dict], daily: list[dict]) -> tuple[int, dict, pd.DataFrame | None]:
    """Run the tracked gate into a temp dir and read its artifacts back from disk."""
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out, exist_ok=True)
    report, _violations = contract.run_gate(_bars_df(bars), _daily_df(daily), out_dir=out)
    with open(os.path.join(out, contract.REPORT_NAME), "r", encoding="utf-8") as fh:
        written = json.load(fh)
    vpath = os.path.join(out, contract.VIOLATIONS_NAME)
    violations = pd.read_csv(vpath) if os.path.exists(vpath) else None
    return written["exit_code"], written, violations


def _happy_fixture(*, days=(GOOD_DAY, "2024-06-06"), **bar_kwargs):
    bars: list[dict] = []
    daily: list[dict] = []
    for day in days:
        daybars = _bar_rows(SYMBOL, day, **bar_kwargs)
        bars.extend(daybars)
        daily.append(_daily_row(SYMBOL, day, daybars))
    return bars, daily


# ---------------------------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------------------------
def test_happy_path_all_assertions_pass(tmp_path):
    bars, daily = _happy_fixture()
    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    assert code == 0
    assert report["all_passed"] is True
    assert report["A6_gate"]["passed"] is True
    assert report["A6_gate"]["downstream_allowed"] is True
    for name, block in report["assertions"].items():
        assert block["passed"] is True, f"{name} unexpectedly failed: {block}"
    assert report["degraded"] is False
    assert report["assertions"]["A3_shape"]["days_ok"] == 2
    assert report["assertions"]["A3_shape"]["days_missing"] == 0
    assert report["assertions"]["A4_timestamp_convention"]["detected"] == "END"
    assert report["assertions"]["A3_shape"]["lunch_bars"] == 0
    assert len(violations) == 0


def test_happy_path_pins_timestamp_convention_in_provenance(tmp_path):
    bars, daily = _happy_fixture()
    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    assert code == 0
    provenance_path = os.path.join(str(tmp_path), "out", contract.PROVENANCE_NAME)
    with open(provenance_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc[contract.PROVENANCE_KEY] == "END"
    assert report["assertions"]["A4_timestamp_convention"]["provenance_action"] == "file_created"


# ---------------------------------------------------------------------------------------------
# A1 -- amount non-null rate
# ---------------------------------------------------------------------------------------------
def test_amount_null_rate_violation_fails_and_writes_violation_row(tmp_path):
    # 12 zero-amount bars out of 96 => 87.5% coverage on one shard, far below 99.5%
    bars, daily = _happy_fixture()
    zeroed = [b for b in bars if b["symbol"] == SYMBOL][:12]
    for b in zeroed:
        b["amount"] = 0.0
        b["volume"] = 0.0
    # daily rows stay as before (they only matter for A3/A5)

    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    assert code == 1
    a1 = report["assertions"]["A1_amount_coverage"]
    assert a1["passed"] is False
    assert a1["violations_raw"] == 12
    assert a1["violations_whitelisted"] == 0
    assert a1["violations_effective"] == 12
    assert any(not r["passed"] for r in a1["per_symbol_shard"])

    assert violations is not None and len(violations) > 0
    v1 = violations[violations["check"] == "A1_amount_coverage"]
    assert len(v1) == 12
    assert set(v1["reason"]) == {"amount_nonpositive"}
    assert bool(v1["whitelisted"].any()) is False


def test_known_zero_volume_window_is_whitelisted_not_ignored(tmp_path):
    """The 2024-09-27 10:30-11:20 zero-volume window must be matched by the explicit whitelist."""
    whitelist_day = "2024-09-27"
    bars, daily = _happy_fixture(days=(whitelist_day, "2024-09-30"))
    window = [b for b in bars
              if b["ts"].date() == pd.Timestamp(whitelist_day).date()
              and "10:30" <= b["ts"].strftime("%H:%M") <= "11:20"]
    assert len(window) == 11  # 10:35,10:40,...,11:20 at END labels inside the window
    for b in window:
        b["amount"] = 0.0
        b["volume"] = 0.0
    # re-derive the daily bar so A5 (reconciliation) is not the thing under test here
    daily[0] = _daily_row(SYMBOL, whitelist_day,
                          [b for b in bars if b["ts"].date() == pd.Timestamp(whitelist_day).date()])

    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    a1 = report["assertions"]["A1_amount_coverage"]
    assert a1["violations_raw"] == len(window)
    assert a1["violations_whitelisted"] == len(window)
    assert a1["violations_effective"] == 0
    assert a1["passed"] is True
    assert code == 0
    # disclosed, not silently dropped
    assert a1["whitelist_match_counts"]["wl_2024-09-27_1030_1120_zero_volume"] == len(window)
    assert "whitelist_expected_vs_matched" in a1  # 11 != 155 -> disclosed
    assert violations["whitelisted"].any()


# ---------------------------------------------------------------------------------------------
# A2 -- implied VWAP range
# ---------------------------------------------------------------------------------------------
def test_implied_vwap_outside_low_high_fails_loud(tmp_path):
    bars, daily = _happy_fixture()
    # amount far above high*volume => implied VWAP ~120 vs a [99.9, 100.1] bar
    bars[7]["amount"] = bars[7]["volume"] * 120.0
    daily[0] = _daily_row(SYMBOL, GOOD_DAY, [b for b in bars if b["ts"].date() == pd.Timestamp(GOOD_DAY).date()])

    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    assert code == 1
    a2 = report["assertions"]["A2_implied_vwap_range"]
    assert a2["passed"] is False
    assert a2["violations"] >= 1
    assert a2["worst_relative_excursion"] > 0.1
    v2 = violations[violations["check"] == "A2_implied_vwap_range"]
    assert len(v2) >= 1
    assert set(v2["reason"]) == {"vwap_implied_outside_low_high"}


# ---------------------------------------------------------------------------------------------
# A3 -- shape: 缺失 vs 停牌, lunch / pre-open / post-close
# ---------------------------------------------------------------------------------------------
def test_partial_day_and_suspension_are_classified_differently(tmp_path):
    good = _bar_rows(SYMBOL, GOOD_DAY)
    partial = _bar_rows(SYMBOL, BAD_DAY, labels=contract.END_LABELS[:47])  # real 缺失
    bars = good + partial
    daily = [_daily_row(SYMBOL, GOOD_DAY, good),
             _daily_row(SYMBOL, BAD_DAY, partial),          # daily says the stock traded
             _daily_row(SYMBOL, HALT_DAY, [])]              # volume NaN => 停牌

    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    a3 = report["assertions"]["A3_shape"]
    assert a3["days_missing"] == 1, a3
    assert a3["days_suspended"] == 1, a3
    assert a3["days_ok"] == 1
    assert a3["passed"] is False
    assert code == 1
    assert [d["classification"] for d in a3["example_bad_days"]] == ["missing"]
    assert a3["example_suspended_days"][0]["date"] == HALT_DAY


def test_suspension_day_alone_does_not_fail_the_gate(tmp_path):
    good = _bar_rows(SYMBOL, GOOD_DAY)
    bars = good
    daily = [_daily_row(SYMBOL, GOOD_DAY, good), _daily_row(SYMBOL, HALT_DAY, [])]

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    a3 = report["assertions"]["A3_shape"]
    assert a3["days_suspended"] == 1
    assert a3["days_missing"] == 0
    assert a3["passed"] is True
    assert code == 0


@pytest.mark.parametrize("stray", ["12:05", "09:00", "15:30"])
def test_stray_bar_outside_session_fails(tmp_path, stray):
    bars, daily = _happy_fixture()
    stray_bar = dict(bars[0])
    stray_bar["ts"] = pd.Timestamp(f"{GOOD_DAY} {stray}")
    bars.append(stray_bar)
    # keep the day's 48 real bars + 1 stray => n_bars = 49

    code, report, violations = _run(tmp_path, bars=bars, daily=daily)

    a3 = report["assertions"]["A3_shape"]
    assert a3["passed"] is False
    assert code == 1
    if stray == "12:05":
        assert a3["lunch_bars"] == 1
    elif stray == "09:00":
        assert a3["pre_session_bars"] == 1
    else:
        assert a3["post_session_bars"] == 1
    assert (violations["check"] == "A3_shape").any()


# ---------------------------------------------------------------------------------------------
# A5 -- amount reconciliation + the disclosed degraded path
# ---------------------------------------------------------------------------------------------
def test_amount_reconciliation_above_threshold_fails(tmp_path):
    bars, daily = _happy_fixture()
    # daily amount 8% too large => median rel err 8% > 1%. Volume is scaled with it so the A4
    # magnitude cross-check (which reads daily amount/volume) is not what fails here.
    for d in daily:
        d["amount"] = d["amount"] * 1.08
        d["volume"] = d["volume"] * 1.08

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    a5 = report["assertions"]["A5_amount_reconciliation"]
    assert a5["degraded"] is False
    assert a5["median_abs_rel_err"] == pytest.approx(abs(1 / 1.08 - 1), abs=1e-6)
    assert a5["passed"] is False
    assert code == 1


def test_degraded_path_without_daily_amount_passes_but_is_disclosed(tmp_path):
    bars, daily = _happy_fixture()
    for d in daily:
        d["amount"] = float("nan")   # the daily interface gives no amount at all

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    a5 = report["assertions"]["A5_amount_reconciliation"]
    assert a5["degraded"] is True
    assert "no usable `amount`" in a5["degraded_reason"]
    assert a5["passed"] is True
    assert a5["days_without_daily_amount"] == 2
    assert report["degraded"] is True
    assert report["degraded_reasons"]
    assert code == 0


# ---------------------------------------------------------------------------------------------
# A4 -- timestamp convention pin
# ---------------------------------------------------------------------------------------------
def test_start_labelled_bars_fail_the_convention_assertion(tmp_path):
    start_labels = contract.START_LABELS
    bars = _bar_rows(SYMBOL, GOOD_DAY, labels=start_labels)
    daily = [_daily_row(SYMBOL, GOOD_DAY, bars)]

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    a4 = report["assertions"]["A4_timestamp_convention"]
    assert a4["detected"] == "START"
    assert a4["passed"] is False
    assert code == 1


def test_magnitude_cross_check_catches_a_basis_or_units_mismatch(tmp_path):
    """A4's coarse check: 5m CNY/volume must be on the same scale as the day's average price."""
    bars, daily = _happy_fixture()
    # daily amount reconciles exactly, but its implied price is 10x the 5m level
    for d in daily:
        d["volume"] = d["volume"] / 10.0

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)

    a4 = report["assertions"]["A4_timestamp_convention"]
    assert a4["detected"] == "END"
    assert a4["magnitude_check"]["median_abs_rel_dev"] == pytest.approx(0.9, abs=1e-6)
    assert a4["passed"] is False
    assert "magnitude" in a4["reason"]
    assert report["assertions"]["A5_amount_reconciliation"]["passed"] is True  # A5 itself is fine
    assert code == 1


def test_existing_provenance_conflict_fails_instead_of_overwriting(tmp_path):
    bars, daily = _happy_fixture()
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out, exist_ok=True)

    prov_path = os.path.join(out, contract.PROVENANCE_NAME)
    original = {
        "git_sha": "deadbeef",
        "recorded_values": {"intraday_rows": 775488, "valid_symbol_day_rows": 16156},
        contract.PROVENANCE_KEY: "START",   # conflicts with the END-labelled frame
    }
    with open(prov_path, "w", encoding="utf-8") as fh:
        json.dump(original, fh, indent=2)

    code, report, _ = _run(tmp_path, bars=bars, daily=daily)
    with open(prov_path, "r", encoding="utf-8") as fh:
        after = json.load(fh)

    a4 = report["assertions"]["A4_timestamp_convention"]
    assert a4["passed"] is False
    assert a4["recorded"] == "START"
    assert a4["provenance_action"] == "conflict_not_rewritten"
    assert code == 1
    # nothing silently rewritten: the pin and every numeric field survive verbatim
    assert after == original


def test_absent_provenance_field_is_added_without_touching_existing_numbers(tmp_path):
    bars, daily = _happy_fixture()
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out, exist_ok=True)

    prov_path = os.path.join(out, contract.PROVENANCE_NAME)
    original = {"git_sha": "deadbeef", "recorded_values": {"intraday_rows": 775488}}
    with open(prov_path, "w", encoding="utf-8") as fh:
        json.dump(original, fh, indent=2)

    code, _, _ = _run(tmp_path, bars=bars, daily=daily)
    with open(prov_path, "r", encoding="utf-8") as fh:
        after = json.load(fh)

    assert code == 0
    assert after[contract.PROVENANCE_KEY] == "END"
    assert after["git_sha"] == original["git_sha"]
    assert after["recorded_values"] == original["recorded_values"]


# ---------------------------------------------------------------------------------------------
# fail-closed input handling
# ---------------------------------------------------------------------------------------------
def test_missing_db_fails_closed_with_nonzero_exit(tmp_path):
    """A hard input error still writes a fail-closed report: the gate never opens on an error.

    The path-existence check itself belongs to the loader in the gitignored CLI shim (covered by
    the guarded smoke test below); here the contract's own fail-closed assembly is under test.
    """
    out = os.path.join(str(tmp_path), "out")
    report, violations = contract.run_gate(
        None, None, out_dir=out,
        error=str(contract.ProbeGateError(f"calibration DB not found: {tmp_path}/nope.db")),
    )
    with open(os.path.join(out, contract.REPORT_NAME), "r", encoding="utf-8") as fh:
        written = json.load(fh)

    assert report["exit_code"] == 1
    assert written["all_passed"] is False
    assert written["A6_gate"]["downstream_allowed"] is False
    assert "not found" in written["error"]
    assert len(violations) == 0


def test_missing_amount_column_fails_closed_and_never_reconstructs(tmp_path):
    out = os.path.join(str(tmp_path), "out")
    bar_rows = _bar_rows(SYMBOL, GOOD_DAY)
    bars = _bars_df(bar_rows).drop(columns=["amount"])
    daily = _daily_df([_daily_row(SYMBOL, GOOD_DAY, bar_rows)])

    report, _violations = contract.run_gate(bars, daily, out_dir=out)
    with open(os.path.join(out, contract.REPORT_NAME), "r", encoding="utf-8") as fh:
        written = json.load(fh)

    assert report["exit_code"] == 1
    assert written["all_passed"] is False
    assert "volume*close" in written["error"]


def test_guard_trips_on_reconstructed_amount_column():
    """Requirement 7: amount == volume*close on every row is the fingerprint of a rebuild."""
    rows = _bar_rows(SYMBOL, GOOD_DAY)
    for r in rows:
        r["amount"] = r["volume"] * r["close"]
    bars = _bars_df(rows)
    with pytest.raises(contract.ProbeGateError, match=r"volume\*close"):
        contract._guard_amount_not_reconstructed(bars)


# ---------------------------------------------------------------------------------------------
# the (gitignored) research-dir CLI shim -- smoke test only, skipped in a clean clone
# ---------------------------------------------------------------------------------------------
def test_research_cli_shim_fails_closed_without_a_db(tmp_path):
    """Guarded smoke test: the shim parses flags, then delegates to the tracked contract.

    It proves the wiring (imports, parser, fail-closed artifacts, exit code) and nothing about the
    assertion semantics -- those are covered above without the shim. Skipped when the research
    directory is absent, which is the normal case for a clean clone.
    """
    probe_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "backtests_research", "2026-10-26_window_vwap", "probe_5m.py",
    )
    if not os.path.exists(probe_path):
        pytest.skip("research-dir CLI shim is not tracked by git")

    out = os.path.join(str(tmp_path), "out")
    proc = subprocess.run(
        [sys.executable, probe_path, "--db", os.path.join(str(tmp_path), "nope.db"),
         "--out-dir", out],
        capture_output=True, text=True, timeout=300,
    )

    assert proc.returncode == 1, proc.stderr
    with open(os.path.join(out, contract.REPORT_NAME), "r", encoding="utf-8") as fh:
        written = json.load(fh)
    assert written["all_passed"] is False
    assert written["A6_gate"]["downstream_allowed"] is False
    assert "not found" in written["error"]
    assert written["probe_version"] == contract.CONTRACT_VERSION

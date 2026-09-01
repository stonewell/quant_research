"""Offline unit tests for live_signal/lsig/signal.py (pure logic) and
run_live_signal.py's main() CLI orchestration. Guaranteed 100% offline: CLI
tests always pass --data-provider synthetic explicitly.
"""

import json
import os
import sys

_LIVE_SIGNAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(_LIVE_SIGNAL_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
for _extra in (_LIVE_SIGNAL_ROOT, _PROJECT_ROOT, _REPO_ROOT):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import numpy as np
import pandas as pd
import pytest

from lsig.signal import as_of_universe, compute_rebalance_instruction, latest_rebalance_rows, top_n_buys

import run_live_signal as rls


# --- compute_rebalance_instruction ------------------------------------------

def test_compute_rebalance_instruction_classifies_buy_sell_hold():
    target = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    reference = pd.Series({"A": 0.5, "B": 0.1, "C": 0.4})
    instruction = compute_rebalance_instruction(target, reference)
    assert instruction.loc["A", "action"] == "hold"
    assert instruction.loc["B", "action"] == "buy"
    assert instruction.loc["C", "action"] == "sell"
    assert instruction.loc["B", "delta"] == pytest.approx(0.2)
    assert instruction.loc["C", "delta"] == pytest.approx(-0.2)


def test_compute_rebalance_instruction_handles_symbol_only_in_target():
    target = pd.Series({"A": 1.0})
    reference = pd.Series({}, dtype=float)
    instruction = compute_rebalance_instruction(target, reference)
    assert instruction.loc["A", "action"] == "buy"
    assert instruction.loc["A", "reference_weight"] == 0.0
    assert instruction.loc["A", "is_new_position"]


def test_compute_rebalance_instruction_handles_symbol_only_in_reference():
    target = pd.Series({}, dtype=float)
    reference = pd.Series({"A": 1.0})
    instruction = compute_rebalance_instruction(target, reference)
    assert instruction.loc["A", "action"] == "sell"
    assert instruction.loc["A", "target_weight"] == 0.0
    assert instruction.loc["A", "delta"] == pytest.approx(-1.0)


def test_compute_rebalance_instruction_drops_symbols_at_zero_on_both_sides():
    target = pd.Series({"A": 1.0, "B": 0.0})
    reference = pd.Series({"A": 1.0, "B": 0.0})
    instruction = compute_rebalance_instruction(target, reference)
    assert "B" not in instruction.index
    assert instruction.loc["A", "action"] == "hold"


def test_compute_rebalance_instruction_respects_threshold():
    target = pd.Series({"A": 0.500001})
    reference = pd.Series({"A": 0.5})
    instruction = compute_rebalance_instruction(target, reference, threshold=1e-3)
    assert instruction.loc["A", "action"] == "hold"


# --- top_n_buys --------------------------------------------------------------

def test_top_n_buys_sorts_by_target_weight_and_caps_at_n():
    target = pd.Series({"A": 0.1, "B": 0.5, "C": 0.4})
    reference = pd.Series({}, dtype=float)
    instruction = compute_rebalance_instruction(target, reference)
    top2 = top_n_buys(instruction, 2)
    assert list(top2.index) == ["B", "C"]


# --- as_of_universe / latest_rebalance_rows ----------------------------------

def test_as_of_universe_truncates_each_symbol_independently():
    idx = pd.bdate_range("2020-01-01", periods=10)
    universe = {
        "A": pd.DataFrame({"Close": range(10)}, index=idx),
        "B": pd.DataFrame({"Close": range(10)}, index=idx),
    }
    truncated = as_of_universe(universe, idx[4])
    assert len(truncated["A"]) == 5
    assert len(truncated["B"]) == 5
    assert truncated["A"].index[-1] == idx[4]


def test_latest_rebalance_rows_drops_all_nan_rows():
    idx = pd.bdate_range("2020-01-01", periods=5)
    df = pd.DataFrame({"A": [np.nan, 0.5, np.nan, np.nan, 0.6]}, index=idx)
    rebalances = latest_rebalance_rows(df)
    assert len(rebalances) == 2
    assert list(rebalances["A"]) == [0.5, 0.6]


# --- main() CLI orchestration ------------------------------------------------

def _write_strategy_file(tmp_path, template_name="equal_weight", rebalance_freq_days=5):
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps({
        "template_name": template_name,
        "params": {"rebalance_freq_days": rebalance_freq_days},
        "explanation": "test fixture",
    }))
    return str(path)


def test_main_writes_a_well_formed_report_with_no_holdings_given(tmp_path, monkeypatch):
    strategy_path = _write_strategy_file(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_live_signal.py", "--strategy-file", strategy_path,
        "--universe", "SPY", "QQQ", "--data-provider", "synthetic",
        "--as-of-date", "2020-06-01", "--results-dir", str(tmp_path),
    ])
    rls.main()

    report_path = tmp_path / "live_signal_report.json"
    assert report_path.exists()
    with open(report_path) as f:
        report = json.load(f)
    assert report["status"] == "ok"
    assert report["run_context"]["reference_source"] == "strategy's own previous rebalance"
    assert (tmp_path / "live_signal_instruction.csv").exists()


def test_main_uses_current_holdings_when_given(tmp_path, monkeypatch):
    # Universe restricted to just QQQ -> equal_weight targets 100% QQQ.
    # Holdings claim 100% SPY (not even in this run's universe) -- SPY must
    # still show up as a full sell, QQQ as a new buy.
    strategy_path = _write_strategy_file(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_live_signal.py", "--strategy-file", strategy_path,
        "--universe", "QQQ", "--data-provider", "synthetic",
        "--as-of-date", "2020-06-01", "--results-dir", str(tmp_path),
        "--current-holdings", json.dumps({"SPY": 1.0}),
    ])
    rls.main()

    with open(tmp_path / "live_signal_report.json") as f:
        report = json.load(f)
    assert report["run_context"]["reference_source"] == "user-supplied current holdings"
    sell_symbols = {row["symbol"] for row in report["sell_signal"]}
    buy_symbols = {row["symbol"] for row in report["buy_signal"]}
    assert "SPY" in sell_symbols
    assert "QQQ" in buy_symbols
    assert "QQQ" in {row["symbol"] for row in report["top_n_buys"]}


def test_main_as_of_date_excludes_later_data(tmp_path, monkeypatch):
    strategy_path = _write_strategy_file(tmp_path)
    early_date = "2020-03-02"
    monkeypatch.setattr(sys, "argv", [
        "run_live_signal.py", "--strategy-file", strategy_path,
        "--universe", "SPY", "--data-provider", "synthetic",
        "--as-of-date", early_date, "--results-dir", str(tmp_path),
    ])
    rls.main()
    with open(tmp_path / "live_signal_report.json") as f:
        report = json.load(f)
    assert report["status"] == "ok"
    assert report["run_context"]["signal_date"] <= early_date


def test_main_reports_no_signal_cleanly_when_as_of_date_too_early(tmp_path):
    # inverse_volatility needs vol_lookback bars of return history before it
    # can compute a non-NaN weight (insufficient-data convention: leave the
    # row NaN, no rebalance at all) -- a tiny --lookback-days window never
    # accumulates that much history.
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps({
        "template_name": "inverse_volatility",
        "params": {"vol_lookback": 60, "rebalance_freq_days": 21},
        "explanation": "test fixture",
    }))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "argv", [
            "run_live_signal.py", "--strategy-file", str(path),
            "--universe", "SPY", "--data-provider", "synthetic",
            "--as-of-date", "2020-01-15", "--lookback-days", "10",
            "--results-dir", str(tmp_path),
        ])
        rls.main()  # must not raise
    with open(tmp_path / "live_signal_report.json") as f:
        report = json.load(f)
    assert report["status"] == "no_signal"

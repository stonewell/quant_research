"""Unit tests for shared output-writing conventions (common/reporting.py).
Guaranteed 100% offline/synthetic.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.reporting import (
    format_backtest_metrics_summary,
    format_walkforward_performance_table,
    format_weights_pct,
    to_dense_weights,
    utc_timestamp,
    write_dense_weights_csv,
    write_json_report,
)


def _sparse_weights():
    idx = pd.bdate_range("2020-01-01", periods=6)
    df = pd.DataFrame(index=idx, columns=["A", "B"], data=np.nan)
    df.loc[idx[0]] = [0.5, 0.5]
    df.loc[idx[3]] = [0.3, 0.7]
    return df


def test_to_dense_weights_forward_fills_and_zero_fills_leading_nan():
    dense = to_dense_weights(_sparse_weights())
    assert dense.iloc[1:3].eq(dense.iloc[0]).all().all()  # forward-filled
    assert dense.iloc[4:].eq(dense.iloc[3]).all().all()


def test_write_dense_weights_csv_round_trip(tmp_path):
    path = tmp_path / "weights.csv"
    dense = write_dense_weights_csv(_sparse_weights(), str(path))
    reloaded = pd.read_csv(path, index_col=0)
    assert list(reloaded.columns) == ["A", "B"]
    assert len(reloaded) == len(dense)


def test_format_weights_pct_default_suffix_and_tail_count():
    formatted = format_weights_pct(_sparse_weights(), n=1)
    assert len(formatted) == 1
    assert formatted.iloc[0]["A"] == "30.0%"


def test_format_weights_pct_custom_suffix_baked_into_cell():
    formatted = format_weights_pct(_sparse_weights(), n=1, suffix="%\n")
    assert formatted.iloc[0]["A"] == "30.0%\n"


def test_utc_timestamp_ends_with_z_and_is_isoformat():
    ts = utc_timestamp()
    assert ts.endswith("Z")
    # Should parse back as an ISO datetime once the trailing Z is stripped.
    from datetime import datetime
    datetime.fromisoformat(ts[:-1])


def test_write_json_report_sanitizes_top_level_non_finite_float(tmp_path):
    path = tmp_path / "report.json"
    write_json_report({"sharpe": float("nan"), "cagr": 0.1}, str(path))
    with open(path) as f:
        data = json.load(f)
    assert data == {"sharpe": None, "cagr": 0.1}


def test_write_json_report_sanitizes_nested_non_finite_floats(tmp_path):
    path = tmp_path / "report.json"
    write_json_report({"a": {"b": float("inf")}, "c": [1.0, float("-inf"), 2.0]}, str(path))
    with open(path) as f:
        data = json.load(f)
    assert data == {"a": {"b": None}, "c": [1.0, None, 2.0]}


def test_write_json_report_unchanged_for_all_finite_fields(tmp_path):
    path = tmp_path / "report.json"
    payload = {"name": "x", "sharpe": 1.23, "nested": {"a": [1, 2, 3]}}
    write_json_report(payload, str(path))
    with open(path) as f:
        data = json.load(f)
    assert data == payload


def test_write_json_report_default_str_for_non_native_types(tmp_path):
    path = tmp_path / "report.json"
    write_json_report({"date": pd.Timestamp("2020-01-01")}, str(path))
    with open(path) as f:
        data = json.load(f)
    assert data["date"] == str(pd.Timestamp("2020-01-01"))


def test_format_backtest_metrics_summary_exact_text():
    result = {
        "sharpe_ratio": 1.234, "cagr": 0.0856, "max_drawdown": 0.1865,
        "calmar_ratio": 0.459, "win_rate": 0.5303, "profit_factor": 1.154,
    }
    summary = format_backtest_metrics_summary(result)
    assert summary == (
        "Sharpe Ratio: 1.23 | CAGR: 8.56% | Max Drawdown: 18.6%\n"
        "Calmar Ratio: 0.46 | Win Rate: 53.0% | Profit Factor: 1.15"
    )


def test_format_walkforward_performance_table_empty_df():
    assert format_walkforward_performance_table(pd.DataFrame()) == ""


def test_format_walkforward_performance_table_formats_pct_and_ratios():
    df = pd.DataFrame([
        {
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
            "sharpe_ratio": 1.234,
            "cagr": 0.153,
            "max_drawdown": 0.082,
            "calmar_ratio": 1.865,
            "win_rate": 0.55,
            "profit_factor": 1.45,
            "baseline_cagr": 0.10,
            "outperformance": 0.053,
        }
    ])
    formatted_str = format_walkforward_performance_table(df)
    assert "2020-01-01" in formatted_str
    assert "15.30%" in formatted_str
    assert "8.20%" in formatted_str
    assert "1.23" in formatted_str
    assert "5.30%" in formatted_str

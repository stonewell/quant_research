import pandas as pd
import pytest

from selectorbot.config import SelectionConfig
from selectorbot.screening import screen_universe


def _metrics(**overrides):
    rows = {
        "GOOD": {"avg_dollar_volume": 50_000_000.0, "history_years": 8.0},
        "ILLIQUID": {"avg_dollar_volume": 100_000.0, "history_years": 8.0},
        "TOO_NEW": {"avg_dollar_volume": 50_000_000.0, "history_years": 0.3},
        "BOTH_FAIL": {"avg_dollar_volume": 100_000.0, "history_years": 0.3},
    }
    df = pd.DataFrame(rows).T
    for col, series in overrides.items():
        df[col] = series
    return df


def test_screen_universe_excludes_illiquid_instrument():
    config = SelectionConfig()
    passed, screened_out = screen_universe(_metrics(), config)
    assert "ILLIQUID" not in passed.index
    assert "ILLIQUID" in screened_out.index
    assert screened_out.loc["ILLIQUID", "screen_fail_reason"] == "liquidity"


def test_screen_universe_excludes_too_short_history():
    config = SelectionConfig()
    passed, screened_out = screen_universe(_metrics(), config)
    assert "TOO_NEW" not in passed.index
    assert screened_out.loc["TOO_NEW", "screen_fail_reason"] == "history"


def test_screen_universe_reports_both_reasons_when_both_gates_fail():
    config = SelectionConfig()
    _, screened_out = screen_universe(_metrics(), config)
    assert screened_out.loc["BOTH_FAIL", "screen_fail_reason"] == "liquidity;history"


def test_screen_universe_keeps_a_good_instrument():
    config = SelectionConfig()
    passed, screened_out = screen_universe(_metrics(), config)
    assert "GOOD" in passed.index
    assert "GOOD" not in screened_out.index


def test_screen_universe_never_excludes_the_benchmark():
    config = SelectionConfig()
    metrics = _metrics()
    metrics.loc["BENCH"] = {"avg_dollar_volume": 1.0, "history_years": 0.01}  # would fail both gates
    passed, screened_out = screen_universe(metrics, config, benchmark="BENCH")
    assert "BENCH" in passed.index
    assert "BENCH" not in screened_out.index


def test_screen_universe_without_benchmark_arg_can_exclude_anything():
    config = SelectionConfig()
    metrics = _metrics()
    metrics.loc["BENCH"] = {"avg_dollar_volume": 1.0, "history_years": 0.01}
    passed, screened_out = screen_universe(metrics, config)  # no benchmark exemption
    assert "BENCH" not in passed.index


def test_screen_universe_treats_missing_liquidity_or_history_as_failing():
    config = SelectionConfig()
    metrics = _metrics()
    metrics.loc["NO_DATA"] = {"avg_dollar_volume": float("nan"), "history_years": float("nan")}
    passed, screened_out = screen_universe(metrics, config)
    assert "NO_DATA" not in passed.index
    assert screened_out.loc["NO_DATA", "screen_fail_reason"] == "liquidity;history"


def test_screen_universe_passed_and_screened_out_partition_the_input():
    config = SelectionConfig()
    metrics = _metrics()
    passed, screened_out = screen_universe(metrics, config)
    assert set(passed.index) | set(screened_out.index) == set(metrics.index)
    assert set(passed.index) & set(screened_out.index) == set()

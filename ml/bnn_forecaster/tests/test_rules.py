"""Unit tests for bnnf/rules.py -- pure functions, no fitting, no I/O."""

import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_BNN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BNN_ROOT not in sys.path:
    sys.path.insert(0, _BNN_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bnnf.config import ForecasterConfig
from bnnf.rules import evaluate_buy_sell, expected_return, confident, rank_buy_sell


def _row(forecast_return=0.15, ci_width=0.10):
    return pd.Series({"forecast_return": forecast_return, "ci_width": ci_width})


def test_expected_return_is_a_passthrough_of_forecast_return():
    assert expected_return(_row(forecast_return=0.22)) == pytest.approx(0.22)


def test_confident_passes_below_ceiling_fails_above():
    cfg = ForecasterConfig(max_ci_width=0.30)
    assert bool(confident(_row(ci_width=0.20), cfg)) is True
    assert bool(confident(_row(ci_width=0.40), cfg)) is False


def test_confident_treats_nan_ci_width_as_failing():
    cfg = ForecasterConfig()
    assert bool(confident(_row(ci_width=float("nan")), cfg)) is False


def test_evaluate_buy_sell_overlap_is_always_resolved_sell_wins():
    # GOOD: confident and clears the buy hurdle.
    # WIDE_CI: a great forecast_return but an unconfident (too-wide) interval
    #          -- would land on both lists under a naive independent check.
    # BELOW_BENCHMARK: confident but the forecast doesn't beat the benchmark.
    df = pd.DataFrame({
        "forecast_return": [0.30, 0.30, 0.02],
        "ci_width": [0.10, 0.80, 0.10],
    }, index=["GOOD", "WIDE_CI", "BELOW_BENCHMARK"])

    cfg = ForecasterConfig(required_return=0.10, max_ci_width=0.30)
    evaluated = evaluate_buy_sell(df, benchmark_return=0.07, cfg=cfg)

    assert evaluated.loc["GOOD", "buy_flag"] and not evaluated.loc["GOOD", "sell_flag"]
    assert evaluated.loc["WIDE_CI", "sell_flag"] and not evaluated.loc["WIDE_CI", "buy_flag"]
    assert evaluated.loc["BELOW_BENCHMARK", "sell_flag"] and not evaluated.loc["BELOW_BENCHMARK", "buy_flag"]
    assert not (evaluated["buy_flag"] & evaluated["sell_flag"]).any()


def test_rank_buy_sell_orders_and_truncates():
    df = pd.DataFrame({
        "expected_return": [0.30, 0.20, 0.10, -0.05, -0.20],
        "buy_flag": [True, True, True, False, False],
        "sell_flag": [False, False, False, True, True],
    }, index=["A", "B", "C", "D", "E"])

    top_buy, top_sell = rank_buy_sell(df, top_n=2)

    assert list(top_buy.index) == ["A", "B"]
    assert list(top_sell.index) == ["E", "D"]

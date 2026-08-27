"""Unit tests for fscreen/rules.py -- pure functions, no I/O, no network."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fundamental_screener.fscreen.config import ScreenerConfig
from fundamental_screener.fscreen.rules import evaluate_buy_sell, expected_return, quality_ok, rank_buy_sell


def _row(roe=0.20, dividend_yield=0.02, earnings_growth=0.08, debt_to_equity=50.0):
    return pd.Series({
        "roe": roe, "dividend_yield": dividend_yield,
        "earnings_growth": earnings_growth, "debt_to_equity": debt_to_equity,
    })


def test_expected_return_is_growth_plus_dividend_yield():
    row = _row(earnings_growth=0.08, dividend_yield=0.02)
    assert expected_return(row) == pytest.approx(0.10)


def test_quality_ok_passes_when_every_gate_clears():
    cfg = ScreenerConfig()
    assert quality_ok(_row(roe=0.20, dividend_yield=0.02, earnings_growth=0.08, debt_to_equity=50.0), cfg)


@pytest.mark.parametrize("overrides,should_pass", [
    ({"roe": 0.05}, False),                    # below min_roe
    ({"dividend_yield": 0.0}, False),          # must be > min_dividend_yield, not >=
    ({"debt_to_equity": 500.0}, False),        # above max_debt_to_equity
    ({"earnings_growth": 0.01}, False),        # below min_earnings_growth
])
def test_quality_ok_fails_on_each_individual_gate(overrides, should_pass):
    cfg = ScreenerConfig()
    row = _row(**overrides)
    assert bool(quality_ok(row, cfg)) is should_pass


def test_quality_ok_treats_nan_field_as_failing_not_passing():
    cfg = ScreenerConfig()
    row = _row(roe=float("nan"))
    assert bool(quality_ok(row, cfg)) is False


def test_evaluate_buy_sell_overlap_is_always_resolved_sell_wins():
    # BADROE: fails quality (low ROE) but has a great expected_return --
    # would land on both lists under a naive independent evaluation.
    # GOOD: passes quality and clears the buy hurdle.
    # WEAK: passes quality but expected_return is below the buy hurdle AND
    # below the benchmark -- sell only.
    df = pd.DataFrame({
        "roe": [0.35, 0.05, 0.20],
        "dividend_yield": [0.03, 0.05, 0.01],
        "earnings_growth": [0.10, 0.15, 0.01],
        "debt_to_equity": [50.0, 50.0, 50.0],
    }, index=["GOOD", "BADROE", "WEAK"])

    cfg = ScreenerConfig(required_return=0.12, min_roe=0.15, min_earnings_growth=0.05)
    evaluated = evaluate_buy_sell(df, benchmark_return=0.07, cfg=cfg)

    assert evaluated.loc["GOOD", "buy_flag"] and not evaluated.loc["GOOD", "sell_flag"]
    # BADROE's expected_return (0.20) clears the buy hurdle, but its ROE
    # fails the quality gate -- sell must win, buy must never also fire.
    assert evaluated.loc["BADROE", "sell_flag"] and not evaluated.loc["BADROE", "buy_flag"]
    assert evaluated.loc["WEAK", "sell_flag"] and not evaluated.loc["WEAK", "buy_flag"]

    # No symbol is ever flagged both ways, for any row.
    assert not (evaluated["buy_flag"] & evaluated["sell_flag"]).any()


def test_rank_buy_sell_orders_and_truncates():
    df = pd.DataFrame({
        "expected_return": [0.30, 0.20, 0.10, -0.05, -0.20],
        "buy_flag": [True, True, True, False, False],
        "sell_flag": [False, False, False, True, True],
    }, index=["A", "B", "C", "D", "E"])

    top_buy, top_sell = rank_buy_sell(df, top_n=2)

    assert list(top_buy.index) == ["A", "B"]   # highest expected_return first
    assert list(top_sell.index) == ["E", "D"]  # lowest (worst) expected_return first
    assert len(top_buy) == 2
    assert len(top_sell) == 2


def test_rank_buy_sell_handles_fewer_than_top_n():
    df = pd.DataFrame({
        "expected_return": [0.30],
        "buy_flag": [True],
        "sell_flag": [False],
    }, index=["ONLY"])
    top_buy, top_sell = rank_buy_sell(df, top_n=5)
    assert list(top_buy.index) == ["ONLY"]
    assert top_sell.empty

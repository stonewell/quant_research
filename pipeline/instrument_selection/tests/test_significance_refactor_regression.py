"""Golden-master regression tests, written BEFORE `persistence.hurst_significance`,
`candlestick.candlestick_significance`, and `momentum.momentum_efficacy` were
refactored to build on the shared `common.significance.shuffle_null_test`
primitive. Values in golden_significance_values.json were captured by running
the exact fixtures/seeds below against the pre-refactor implementations;
comparing with `==` (not `approx`) proves the refactor is bit-identical, not
merely close. Guaranteed 100% offline/synthetic.
"""

import json
import os

import numpy as np
import pandas as pd
import pytest
from common.testing import make_ar1_series, make_random_walk_df

from selectorbot.candlestick import candlestick_significance
from selectorbot.momentum import momentum_efficacy
from selectorbot.persistence import hurst_significance

_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_significance_values.json")
with open(_GOLDEN_PATH) as _f:
    GOLDEN = json.load(_f)


def _assert_matches_golden(result: dict, key: str):
    expected = GOLDEN[key]
    assert set(result.keys()) == set(expected.keys())
    for field, expected_value in expected.items():
        actual_value = result[field]
        if expected_value == "NaN":
            assert isinstance(actual_value, float) and np.isnan(actual_value), f"{key}.{field}"
        elif isinstance(expected_value, float):
            assert actual_value == pytest.approx(expected_value, abs=0, rel=0) or actual_value == expected_value, (
                f"{key}.{field}: expected {expected_value!r}, got {actual_value!r}"
            )
        else:
            assert actual_value == expected_value, f"{key}.{field}: expected {expected_value!r}, got {actual_value!r}"


def _bars(o, h, l, c, start="2015-01-01"):
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}, index=idx)


def make_reversal_cycles(n_cycles=45, down=8, up=8):
    o, h, l, c = [], [], [], []
    price = 100.0
    for _ in range(n_cycles):
        for _ in range(down):
            op, cl = price, price * 0.985
            o.append(op); c.append(cl); h.append(op * 1.002); l.append(cl * 0.998)
            price = cl
        prev_o, prev_c = o[-1], c[-1]
        op, cl = prev_c * 0.997, prev_o * 1.02
        o.append(op); c.append(cl); h.append(cl * 1.002); l.append(op * 0.998)
        price = cl
        for _ in range(up):
            op, cl = price, price * 1.012
            o.append(op); c.append(cl); h.append(cl * 1.002); l.append(op * 0.998)
            price = cl
    return _bars(o, h, l, c)


def test_hurst_significance_matches_golden_trend():
    result = hurst_significance(make_ar1_series(0.75, 3000, seed=42), n_surrogates=200, seed=7)
    _assert_matches_golden(result, "hurst_trend")


def test_hurst_significance_matches_golden_meanrev():
    result = hurst_significance(make_ar1_series(-0.9, 3000, seed=42), n_surrogates=200, seed=7)
    _assert_matches_golden(result, "hurst_meanrev")


def test_hurst_significance_matches_golden_random_walk():
    rw = pd.Series(np.random.default_rng(42).normal(0, 1, 3000))
    result = hurst_significance(rw, n_surrogates=200, seed=7)
    _assert_matches_golden(result, "hurst_rw")


def test_candlestick_significance_matches_golden_engineered_edge():
    result = candlestick_significance(make_reversal_cycles(), horizon=5, seed=0)
    _assert_matches_golden(result, "candlestick_engineered")


def test_candlestick_significance_matches_golden_random_walk():
    result = candlestick_significance(make_random_walk_df(n=900, seed=11), horizon=5, seed=0)
    _assert_matches_golden(result, "candlestick_rw")


def test_momentum_efficacy_matches_golden_trend():
    from common.testing import make_ar1_ohlcv
    df = make_ar1_ohlcv(phi=0.7, n=3000, seed=1)
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    _assert_matches_golden(result, "momentum_trend")


def test_momentum_efficacy_matches_golden_meanrev():
    from common.testing import make_ar1_ohlcv
    df = make_ar1_ohlcv(phi=-0.7, n=3000, seed=2)
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    _assert_matches_golden(result, "momentum_meanrev")


def test_momentum_efficacy_matches_golden_random_walk():
    from common.testing import make_ar1_ohlcv
    df = make_ar1_ohlcv(phi=0.0, n=3000, seed=3)
    result = momentum_efficacy(df["Close"], lookback=5, horizon=5, seed=0)
    _assert_matches_golden(result, "momentum_rw")

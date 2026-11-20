"""Offline unit tests for Amihud illiquidity & shock ratio (Track MS, MS15).

All tests run 100% offline on deterministic synthetic daily panels.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.daily_factors import amihud_illiquidity, amihud_illiquidity_ratio
from common.lookahead_checks import assert_future_append_invariant


def test_amihud_exact_formula_amount_and_fallback_scaling():
    dates = pd.bdate_range("2025-01-02", periods=10)
    close = pd.DataFrame(
        {
            "A": [10.0, 11.0, 10.45, 11.0, 11.55, 12.1, 11.5, 12.0, 12.6, 13.0],
            "B": [20.0, 20.2, 20.4, 20.6, 20.8, 21.0, 21.2, 21.4, 21.6, 21.8],
        },
        index=dates,
    )
    volume = pd.DataFrame({"A": np.full(10, 1000.0), "B": np.full(10, 5000.0)}, index=dates)

    # 1. Exact hand calculation without hidden scaling: |ret| / (close * volume)
    illiq5 = amihud_illiquidity(close, volume, lookback=5)
    assert illiq5.iloc[:5].isna().all().all()  # rows 0..4 are NaN (row 0 ret is NaN)

    ret_a = close["A"].pct_change(fill_method=None)
    expected_1d_a = ret_a.abs() / (close["A"] * volume["A"])
    expected_5_a = expected_1d_a.rolling(5, min_periods=5).mean()
    np.testing.assert_allclose(
        illiq5["A"].iloc[5:].to_numpy(), expected_5_a.iloc[5:].to_numpy(), atol=1e-15
    )

    # 2. Ratio short=3 / long=5
    ratio = amihud_illiquidity_ratio(close, volume, short=3, long=5)
    illiq3 = amihud_illiquidity(close, volume, lookback=3)
    np.testing.assert_allclose(
        ratio.iloc[5:].to_numpy(), (illiq3 / illiq5).iloc[5:].to_numpy(), atol=1e-12
    )

    # 3. Explicit amount takes precedence over close * volume; missing/non-positive falls back
    amount = (close * volume) * 2.0
    amount.iloc[6, 0] = np.nan  # cell-wise fallback to close * volume
    illiq_amt = amihud_illiquidity(close, volume, lookback=5, amount=amount)
    assert illiq_amt.iloc[5, 0] == pytest.approx(illiq5.iloc[5, 0] * 0.5, rel=1e-12)
    assert illiq_amt.iloc[6, 0] > illiq5.iloc[6, 0] * 0.5  # row 6 fell back to 1x dvol


def test_amihud_zero_volume_produces_nan_and_validation():
    dates = pd.bdate_range("2025-01-02", periods=10)
    close = pd.DataFrame({"A": np.linspace(10.0, 12.0, 10)}, index=dates)
    volume = pd.DataFrame({"A": np.full(10, 1000.0)}, index=dates)
    volume.iloc[4, 0] = 0.0  # suspended bar

    illiq = amihud_illiquidity(close, volume, lookback=3)
    # Windows containing row 4 (rows 4, 5, 6) must be NaN, never inf or 0.0
    assert illiq.iloc[4:7, 0].isna().all()
    assert np.isfinite(illiq.iloc[7:, 0]).all()

    with pytest.raises(ValueError):
        amihud_illiquidity(close, volume, lookback=0)
    with pytest.raises(ValueError):
        amihud_illiquidity_ratio(close, volume, short=5, long=5)


def test_amihud_causality_via_ms12_harness():
    rng = np.random.default_rng(15)
    dates = pd.bdate_range("2025-01-02", periods=35)
    close = pd.DataFrame(
        10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, size=(35, 3)), axis=0)),
        index=dates,
        columns=["A", "B", "C"],
    )
    vol = pd.DataFrame(rng.uniform(1000.0, 5000.0, size=(35, 3)), index=dates, columns=close.columns)

    assert_future_append_invariant(
        lambda d: amihud_illiquidity(d, vol.loc[d.index], lookback=5), close, [10, 22]
    )
    assert_future_append_invariant(
        lambda d: amihud_illiquidity_ratio(d, vol.loc[d.index], short=5, long=15), close, [18, 28]
    )

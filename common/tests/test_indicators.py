"""Unit tests for common/indicators.py."""

import os
import sys

import numpy as np
import pandas as pd

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.indicators import rsi_cutler, rsi_wilder


def test_rsi_wilder_flat_series_is_neutral_fifty():
    # Regression test: `avg_loss == 0` used to map BOTH "all gains, zero
    # losses" (genuinely 100) AND "completely flat, zero gains AND zero
    # losses" to RSI=100 via `result.where(avg_loss != 0, 100.0)`. A
    # constant price series has zero gains and zero losses every bar -- RSI
    # should be the neutral midpoint (50.0), not the maximal reading (100.0).
    close = pd.Series([100.0] * 30)
    result = rsi_wilder(close, period=14)
    valid = result.dropna()
    assert not valid.empty
    assert (valid == 50.0).all()


def test_rsi_cutler_flat_series_is_neutral_fifty():
    close = pd.Series([100.0] * 30)
    result = rsi_cutler(close, period=14)
    valid = result.dropna()
    assert not valid.empty
    assert (valid == 50.0).all()


def test_rsi_wilder_monotonically_rising_series_is_still_one_hundred():
    # Pin the existing, correct behavior: all gains / zero losses over the
    # window must still read as maximal RSI (100.0), unaffected by the flat-
    # window fix above.
    close = pd.Series(np.arange(1.0, 31.0))  # strictly increasing, no down bars
    result = rsi_wilder(close, period=14)
    valid = result.dropna()
    assert not valid.empty
    assert (valid == 100.0).all()


def test_rsi_cutler_monotonically_rising_series_is_still_one_hundred():
    close = pd.Series(np.arange(1.0, 31.0))
    result = rsi_cutler(close, period=14)
    valid = result.dropna()
    assert not valid.empty
    assert (valid == 100.0).all()

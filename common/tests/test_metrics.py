"""Unit tests for common/metrics.py."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.metrics import profit_factor_from_returns, win_rate_from_returns


def test_win_rate_from_returns_mixed():
    returns = pd.Series([0.01, -0.02, 0.03, -0.01, 0.0])
    # 2 of 5 periods are strictly positive
    assert win_rate_from_returns(returns) == pytest.approx(2 / 5)


def test_win_rate_from_returns_empty():
    assert win_rate_from_returns(pd.Series([], dtype=float)) == 0.0


def test_win_rate_from_returns_all_positive():
    returns = pd.Series([0.01, 0.02, 0.03])
    assert win_rate_from_returns(returns) == pytest.approx(1.0)


def test_profit_factor_from_returns_mixed():
    returns = pd.Series([0.02, -0.01, 0.03, -0.01])
    # gains = 0.05, losses = 0.02
    assert profit_factor_from_returns(returns) == pytest.approx(0.05 / 0.02)


def test_profit_factor_from_returns_no_losses_is_nan():
    returns = pd.Series([0.01, 0.02, 0.0])
    assert np.isnan(profit_factor_from_returns(returns))


def test_profit_factor_from_returns_empty_is_nan():
    assert np.isnan(profit_factor_from_returns(pd.Series([], dtype=float)))

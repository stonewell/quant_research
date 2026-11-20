"""Offline unit tests for lottery-demand MAX return (top-k mean, Track MS, MS16).

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

from common.daily_factors import max_return
from common.lookahead_checks import assert_future_append_invariant


def test_max_return_top_k_mean_falsifies_rolling_max():
    dates = pd.bdate_range("2025-01-02", periods=6)
    # Daily returns for rows 1..5: [0.0, 0.10, 0.0, 0.02, 0.0]
    rets = [0.0, 0.10, 0.0, 0.02, 0.0]
    prices = [100.0]
    for r in rets:
        prices.append(prices[-1] * (1.0 + r))
    close = pd.DataFrame({"A": prices}, index=dates)

    out_k2 = max_return(close, lookback=5, k=2)
    assert out_k2.iloc[:5, 0].isna().all()
    assert out_k2.iloc[5, 0] == pytest.approx(0.06, abs=1e-12)
    assert out_k2.iloc[5, 0] != pytest.approx(0.10, abs=1e-6)  # strictly != rolling().max()

    # k=1 degenerates to rolling().max()
    out_k1 = max_return(close, lookback=5, k=1)
    assert out_k1.iloc[5, 0] == pytest.approx(0.10, abs=1e-12)


def test_max_return_ties_validation_and_causality():
    dates = pd.bdate_range("2025-01-02", periods=30)
    rng = np.random.default_rng(16)
    close = pd.DataFrame(
        10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, size=(30, 3)), axis=0)),
        index=dates,
        columns=["A", "B", "C"],
    )

    with pytest.raises(ValueError):
        max_return(close, lookback=5, k=6)
    with pytest.raises(ValueError):
        max_return(close, lookback=5, k=0)
    with pytest.raises(ValueError):
        max_return(close, lookback=0, k=1)

    assert_future_append_invariant(lambda d: max_return(d, lookback=10, k=2), close, [12, 22])

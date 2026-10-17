"""Unit tests for all 101 WorldQuant Formulaic Alphas in rs/alpha101_factors.py.

100% offline: Uses synthetic data only per workspace testing policy.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.data import SyntheticDataProvider
from research_strategy.rs.alpha101_factors import (
    Alpha101Data,
    ALPHA_REGISTRY,
    compute_all_alphas,
    compute_alpha,
)


@pytest.fixture(scope="module")
def synthetic_alpha_data():
    dp = SyntheticDataProvider(seed=42)
    universe = {
        "SPY": dp.fetch_ohlcv("SPY", "2021-01-01", "2022-01-01"),
        "QQQ": dp.fetch_ohlcv("QQQ", "2021-01-01", "2022-01-01"),
        "IWM": dp.fetch_ohlcv("IWM", "2021-01-01", "2022-01-01"),
        "TLT": dp.fetch_ohlcv("TLT", "2021-01-01", "2022-01-01"),
    }
    return Alpha101Data.from_universe(universe)


def test_alpha_registry_contains_101_alphas():
    assert len(ALPHA_REGISTRY) == 101
    assert set(ALPHA_REGISTRY.keys()) == set(range(1, 102))


@pytest.mark.parametrize("alpha_id", list(range(1, 102)))
def test_all_101_alphas_compute_cleanly(synthetic_alpha_data, alpha_id):
    """Verifies that every single alpha from 1 to 101 computes without error,
    produces the correct output DataFrame shape, and contains no NaNs or infs.
    """
    data = synthetic_alpha_data
    res = compute_alpha(alpha_id, data)
    assert isinstance(res, pd.DataFrame)
    assert res.shape == (len(data.close), len(data.close.columns))
    assert not np.isinf(res.values).any()
    assert not res.isna().any().any()


def test_compute_all_alphas_subset(synthetic_alpha_data):
    subset = [1, 6, 12, 41, 53, 101]
    res_dict = compute_all_alphas(synthetic_alpha_data, alpha_ids=subset)
    assert set(res_dict.keys()) == set(subset)
    for aid in subset:
        assert isinstance(res_dict[aid], pd.DataFrame)
        assert res_dict[aid].shape == (len(synthetic_alpha_data.close), 4)


def test_invalid_alpha_id_raises_value_error(synthetic_alpha_data):
    with pytest.raises(ValueError, match="Unknown alpha_id: 0"):
        compute_alpha(0, synthetic_alpha_data)
    with pytest.raises(ValueError, match="Unknown alpha_id: 102"):
        compute_alpha(102, synthetic_alpha_data)


def test_alpha101_data_from_universe_vwap_no_lookahead():
    """Verifies that Alpha101Data.from_universe falls back to typical price (H+L+C)/3
    when VWAP is NaN, without leaking future VWAP backwards via bfill() or carrying
    stale prior-day VWAP across gaps via ffill().
    """
    dates = pd.bdate_range("2023-01-01", periods=6)
    # Day 0, 1: VWAP is NaN. Day 2: VWAP = 999.0. Day 3: VWAP is NaN. Day 4, 5: VWAP = 500.0.
    df = pd.DataFrame({
        "Open": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        "High": [12.0, 22.0, 32.0, 42.0, 52.0, 62.0],
        "Low": [8.0, 18.0, 28.0, 38.0, 48.0, 58.0],
        "Close": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        "Volume": [1000.0] * 6,
        "VWAP": [np.nan, np.nan, 999.0, np.nan, 500.0, 500.0],
    }, index=dates)

    universe = {"TEST": df}
    data = Alpha101Data.from_universe(universe)

    # Typical price for each day is (High + Low + Close) / 3
    # Day 0: (12 + 8 + 10) / 3 = 10.0
    # Day 1: (22 + 18 + 20) / 3 = 20.0
    # Day 2: VWAP is 999.0
    # Day 3: VWAP is NaN -> typical price (42 + 38 + 40) / 3 = 40.0 (NOT 999.0 from ffill!)
    # Day 4: VWAP is 500.0
    # Day 5: VWAP is 500.0
    expected_vwap = [10.0, 20.0, 999.0, 40.0, 500.0, 500.0]

    actual_vwap = data.vwap["TEST"].tolist()
    assert np.allclose(actual_vwap, expected_vwap)


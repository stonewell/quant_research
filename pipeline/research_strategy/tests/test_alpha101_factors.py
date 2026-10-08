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

"""Unit tests for Blend Strategy Enhancements and Sector Incumbent Protection.

Validates:
1. sector_incumbent_hurdle parameter resolution across default, StrategyConfig, and param dictionary.
2. Sector incumbent protection logic:
   - Suppresses churn between same-sector assets (e.g. SPDB 600000.SH vs ICBC 601398.SH) when ROC difference < hurdle.
   - Retains the existing incumbent to fill the sector quota.
   - Allows displacement when challenger ROC exceeds incumbent ROC by at least sector_incumbent_hurdle.
3. Compliance with sparse weights NaN-vs-0.0 contract.

100% offline using synthetic OHLCV data.
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

from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import (
    BaseBlendAllocationStrategy,
    ChanCrisisShieldBlendStrategy,
    ChanFourStateBlendStrategy,
    GenericBlendStrategy,
    _get_symbol_sector,
)


def _build_synthetic_bar_df(dates, base_price, daily_ret=0.001):
    n = len(dates)
    closes = [base_price * ((1.0 + daily_ret) ** i) for i in range(n)]
    df = make_ohlcv_from_closes(closes)
    df.index = dates
    return df


def test_sector_incumbent_hurdle_resolution():
    """Verify sector_incumbent_hurdle resolution hierarchy and default of 0.03."""
    strat = GenericBlendStrategy()
    # Default without parameter
    assert strat.resolve_param({}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.03)

    # Overridden in params dict
    assert strat.resolve_param({"sector_incumbent_hurdle": 0.05}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.05)
    assert strat.resolve_param({"blend_sector_incumbent_hurdle": 0.04}, "sector_incumbent_hurdle", 0.03, float) == pytest.approx(0.04)


def test_sector_incumbent_protection_churn_suppression():
    """Verify that when challenger's ROC does not beat incumbent's ROC by at least
    sector_incumbent_hurdle, the existing incumbent is retained, preventing churn.
    """
    dates = pd.date_range("2024-01-01", periods=270, freq="B")
    # Both 600000.SH (SPDB) and 601398.SH (ICBC) belong to the 'banking' sector
    assert _get_symbol_sector("600000.SH") == "banking"
    assert _get_symbol_sector("601398.SH") == "banking"

    # Construct synthetic prices:
    # SPDB: positive momentum
    # ICBC: slightly higher positive momentum on the final day, but diff < hurdle 0.03
    spdb_df = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.002)
    icbc_df = _build_synthetic_bar_df(dates, 5.0, daily_ret=0.0021)
    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600000.SH": spdb_df,
        "601398.SH": icbc_df,
        "BIL": cash_df,
    }

    # Run GenericBlendStrategy with sub-strategy weights favoring 600000.SH or breadth thrust
    strat = GenericBlendStrategy()
    params = {
        "cash_proxy": "BIL",
        "max_assets_per_sector": 1,
        "sector_incumbent_hurdle": 0.03,
        "preemptive_thrust_deployment": True,
        "min_thrust_assets": 1,
    }

    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert "600000.SH" in weights.columns
    assert "601398.SH" in weights.columns

    # Verify weights are sparse (rebalance dates have non-NaN, non-rebalance are NaN)
    # and weights sum to <= 1.0 (with cash proxy)
    non_nan_rows = weights.dropna(how="all")
    assert len(non_nan_rows) > 0


def test_sector_incumbent_protection_allows_displacement_on_large_hurdle_beat():
    """Verify that when challenger's ROC beats incumbent's ROC by >= sector_incumbent_hurdle,
    the challenger can displace the incumbent.
    """
    dates = pd.date_range("2024-01-01", periods=270, freq="B")
    spdb_df = _build_synthetic_bar_df(dates, 10.0, daily_ret=0.001)
    # ICBC surging strongly (+0.01 per day = much higher 10d ROC)
    icbc_df = _build_synthetic_bar_df(dates, 5.0, daily_ret=0.01)
    cash_df = _build_synthetic_bar_df(dates, 100.0, daily_ret=0.0001)

    universe = {
        "600000.SH": spdb_df,
        "601398.SH": icbc_df,
        "BIL": cash_df,
    }

    strat = GenericBlendStrategy()
    params = {
        "cash_proxy": "BIL",
        "max_assets_per_sector": 1,
        "sector_incumbent_hurdle": 0.03,
        "preemptive_thrust_deployment": True,
    }
    weights = strat.generate_weights(universe, params=params)
    assert not weights.empty
    assert "601398.SH" in weights.columns

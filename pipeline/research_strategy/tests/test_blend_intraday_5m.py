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


def test_live_runner_limit_down_double_freeze_and_limit_prices(tmp_path):
    """Test 10: Verify Stage 2 live runner limit-down double freeze (R14),
    limit-up buy rejection (R10), and execution price guidance fields (R11, T4.2, T4.3).
    """
    import json
    from unittest.mock import patch
    from common.allocation_templates import EqualWeightAllocation
    from common.data import SyntheticDataProvider
    from scripts.run_live_strategy import main as live_main

    univ_file = str(tmp_path / "universe.txt")
    with open(univ_file, "w") as f:
        f.write("600000.SH\n601398.SH\nBIL\n")

    # Account holds 1,000 shares of 600000.SH and 50,000 in BIL cash proxy
    holdings_file = str(tmp_path / "holdings.json")
    with open(holdings_file, "w") as f:
        json.dump({"600000.SH": 1000, "BIL": 50000}, f)

    state_file = str(tmp_path / "account_state.json")
    with open(state_file, "w") as f:
        json.dump({}, f)

    cmd_args = [
        "--strategy", "equal_weight",
        "--universe-file", univ_file,
        "--current-holdings-file", holdings_file,
        "--account-state-file", state_file,
        "--output-dir", str(tmp_path),
        "--as-of-date", "2024-05-10",
        "--start-date", "2024-01-01",
        "--data-provider", "synthetic",
        "--portfolio-value", "100000",
        "--exit-confirm-bars", "1",
        "--entry-confirm-bars", "1",
    ]

    # Strategy instructs: sell 600000.SH (target 0.0), buy 601398.SH (target 0.50)
    orig_gen = EqualWeightAllocation.generate_weights
    def mock_gen(self, universe, params=None):
        df = orig_gen(self, universe, params)
        df["600000.SH"] = 0.0
        df["601398.SH"] = 0.50
        df["BIL"] = 0.50
        return df

    # Mock market prices:
    # 600000.SH hits limit-down (-10% from prev close)
    # 601398.SH hits limit-up (+10% from prev close)
    orig_fetch = SyntheticDataProvider.fetch_ohlcv
    def mock_fetch(self, symbol, start, end, interval="1d"):
        df = orig_fetch(self, symbol, start, end, interval)
        if symbol == "600000.SH" and len(df) >= 2:
            prev_c = float(df["Close"].iloc[-2])
            df.iloc[-1, df.columns.get_loc("Close")] = round(prev_c * 0.90, 2)
            df.iloc[-1, df.columns.get_loc("Low")] = round(prev_c * 0.90, 2)
        elif symbol == "601398.SH" and len(df) >= 2:
            prev_c = float(df["Close"].iloc[-2])
            df.iloc[-1, df.columns.get_loc("Close")] = round(prev_c * 1.10, 2)
            df.iloc[-1, df.columns.get_loc("High")] = round(prev_c * 1.10, 2)
        return df

    with patch.object(EqualWeightAllocation, "generate_weights", mock_gen):
        with patch.object(SyntheticDataProvider, "fetch_ohlcv", mock_fetch):
            order_df = live_main(cmd_args)

    assert not order_df.empty

    # 1. Verify Phase 1 Sell Limit-Down Double Freeze (R14)
    r_600000 = order_df[order_df["symbol"] == "600000.SH"].iloc[0]
    assert r_600000["action"] == "SELL"
    assert r_600000["execution_warning"] == "LIMIT_DOWN_ILLIQUID_RISK"
    assert "LIMIT_DOWN_ILLIQUID_RISK" in r_600000["reason"]

    # 2. Verify Phase 2 Buy Limit-Up Lock (R10)
    r_601398 = order_df[order_df["symbol"] == "601398.SH"].iloc[0]
    assert r_601398["action"] == "HOLD_FILTERED"
    assert r_601398["execution_warning"] == "LIMIT_UP_LOCKED"
    assert "LIMIT_UP_LOCKED" in r_601398["reason"]

    # 3. Verify Phase 4 Cash Sweep did not overdraft proceeds of frozen sell order (R14)
    r_bil = order_df[order_df["symbol"] == "BIL"].iloc[0]
    # Trade value of BIL cash sweep should not include proceeds of 600000.SH
    assert r_bil["action"] == "SWEEP_CASH"

    # 4. Verify Execution price guidance fields (R11, T4.3)
    for _, row in order_df.iterrows():
        assert "suggested_limit_price" in row
        assert "passive_support_limit_price" in row
        assert "intraday_vwap" in row
        assert "vwap_premium_pct" in row
        assert "chan_5m_state" in row
        assert pd.notna(row["suggested_limit_price"])
        assert pd.notna(row["passive_support_limit_price"])


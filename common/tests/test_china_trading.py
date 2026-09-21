"""Unit tests for China A-share trading rules in common/allocation_backtester.py."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import _get_china_price_limit, run_allocation_backtest
from common.testing import make_ohlcv_from_closes


def test_get_china_price_limit():
    assert _get_china_price_limit("600000.SH") == 0.10
    assert _get_china_price_limit("000001.SZ") == 0.10
    assert _get_china_price_limit("300750.SZ") == 0.20
    assert _get_china_price_limit("688981.SH") == 0.20
    assert _get_china_price_limit("830946.BJ") == 0.30
    assert _get_china_price_limit("920001.BJ") == 0.30
    assert _get_china_price_limit("SPY") == 1.0
    assert _get_china_price_limit("BIL") == 1.0


def test_china_trading_auto_upgrades_min_shares():
    """When china_trading=True, default min_shares (1) upgrades to 100 (一手)."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    # Price is 10.0, target weight 0.05 on 100k is 5000. At price 10, 500 shares.
    # If target weight is 0.0005 (50 dollars), 5 shares. At min_shares=100, 5 shares is suppressed to 0.
    df = pd.DataFrame({
        "Open": [10.0, 10.0, 10.0],
        "High": [10.0, 10.0, 10.0],
        "Low": [10.0, 10.0, 10.0],
        "Close": [10.0, 10.0, 10.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"600000.SH": df}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[0] = [0.0005]  # $50 -> 5 shares < 100

    res = run_allocation_backtest(universe, target_weights, min_shares=1, china_trading=True)
    trades = res.get("rebalance_report")
    # Suppressed to 0 shares due to 100-share minimum
    assert trades.empty


def test_china_trading_limit_up_blocks_buy():
    """A stock locked at limit-up (+10% on Main Board closing at High) cannot be bought."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    # Day 0: 10.0, Day 1: limit up to 11.0 (+10%), closing at High
    df = pd.DataFrame({
        "Open": [10.0, 10.5, 11.0],
        "High": [10.0, 11.0, 11.0],
        "Low": [10.0, 10.5, 11.0],
        "Close": [10.0, 11.0, 11.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"600000.SH": df}

    # Attempt to BUY on day 1
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[1] = [0.50]

    # Without china_trading: BUY succeeds
    res_no_china = run_allocation_backtest(universe, target_weights, china_trading=False)
    assert not res_no_china["rebalance_report"].empty
    assert res_no_china["rebalance_report"].iloc[0]["action"] == "BUY"

    # With china_trading: BUY is blocked
    res_china = run_allocation_backtest(universe, target_weights, china_trading=True)
    assert res_china["rebalance_report"].empty


def test_china_trading_limit_down_blocks_sell():
    """A stock locked at limit-down (-10% closing at Low) cannot be sold."""
    idx = pd.bdate_range("2023-01-01", periods=4)
    # Day 0: bought at 10.0
    # Day 1: stable at 10.0
    # Day 2: limit down to 9.0 (-10%), closing at Low
    df = pd.DataFrame({
        "Open": [10.0, 10.0, 9.5, 9.0],
        "High": [10.0, 10.0, 9.5, 9.0],
        "Low": [10.0, 10.0, 9.0, 9.0],
        "Close": [10.0, 10.0, 9.0, 9.0],
        "Volume": [100000.0] * 4,
    }, index=idx)
    universe = {"600000.SH": df}

    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[0] = [0.50]  # Buy on day 0
    target_weights.iloc[2] = [0.0]   # Attempt to Sell on day 2 (limit-down)

    res_china = run_allocation_backtest(universe, target_weights, min_shares=100, china_trading=True)
    trades = res_china["rebalance_report"]
    # Only Day 0 buy trade executed; Day 2 sell was blocked
    assert len(trades) == 1
    assert trades.iloc[0]["action"] == "BUY"


def test_china_trading_t_plus_one_settlement():
    """Shares bought today cannot be sold on the same day."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [10.0, 10.0, 10.0],
        "High": [10.0, 10.0, 10.0],
        "Low": [10.0, 10.0, 10.0],
        "Close": [10.0, 10.0, 10.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"600000.SH": df}

    # Target: Buy on day 1, then sell on day 2
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[1] = [0.50]
    target_weights.iloc[2] = [0.0]

    res = run_allocation_backtest(universe, target_weights, min_shares=100, china_trading=True)
    trades = res["rebalance_report"]
    assert len(trades) == 2
    assert trades.iloc[0]["action"] == "BUY"
    assert trades.iloc[1]["action"] == "SELL"


def test_china_trading_sell_stamp_duty():
    """Sells incur additional 0.05% stamp duty."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [10.0, 10.0, 10.0],
        "High": [10.0, 10.0, 10.0],
        "Low": [10.0, 10.0, 10.0],
        "Close": [10.0, 10.0, 10.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"600000.SH": df}

    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[0] = [0.50]  # Buy
    target_weights.iloc[1] = [0.0]   # Sell

    res = run_allocation_backtest(
        universe, target_weights, min_shares=100, china_trading=True,
        commission_pct=0.0005, slippage_pct=0.0005, stamp_duty_pct=0.0005
    )
    trades = res["rebalance_report"]
    buy_trade = trades.iloc[0]
    sell_trade = trades.iloc[1]

    # BUY cost factor = 0.0005 comm + 0.0005 slip = 0.001
    assert buy_trade["total_cost"] == pytest.approx(buy_trade["trade_value"] * 0.001)
    # SELL cost factor = 0.0005 comm + 0.0005 slip + 0.0005 stamp = 0.0015
    assert sell_trade["total_cost"] == pytest.approx(sell_trade["trade_value"] * 0.0015)

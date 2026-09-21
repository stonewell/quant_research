"""Unit tests for US and Hong Kong trading regimes in common/allocation_backtester.py."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import _get_hk_board_lot, run_allocation_backtest
from pipeline.strategy_generator.stratgen.generator import GeneratorConfig
from pipeline.research_strategy.rs.config import StrategyConfig
from pipeline.research_strategy.rs.strategy import ChanPivotShiftStrategy, ChanThreeTypeStrategy
from pipeline.research_strategy.rs.chan_advanced_strategies import ChanCompositeStrategy


def test_mutual_exclusivity_backtester():
    """Verify that enabling multiple trading regimes raises ValueError in backtester."""
    idx = pd.bdate_range("2023-01-01", periods=2)
    df = pd.DataFrame({"Open": [10, 10], "High": [10, 10], "Low": [10, 10], "Close": [10, 10], "Volume": [100, 100]}, index=idx)
    universe = {"SPY": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["SPY"])
    tw.iloc[0] = [1.0]

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        run_allocation_backtest(universe, tw, china_trading=True, us_trading=True)

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        run_allocation_backtest(universe, tw, china_trading=True, hk_trading=True)

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        run_allocation_backtest(universe, tw, us_trading=True, hk_trading=True)


def test_mutual_exclusivity_configs():
    """Verify that GeneratorConfig and StrategyConfig enforce mutual exclusivity."""
    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        GeneratorConfig(china_trading=True, us_trading=True)

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        GeneratorConfig(us_trading=True, hk_trading=True)

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        StrategyConfig(china_trading=True, hk_trading=True)

    with pytest.raises(ValueError, match="Only one of china_trading, us_trading, hk_trading"):
        StrategyConfig(us_trading=True, hk_trading=True)


def test_us_trading_one_share_sizing():
    """US trading enforces 1-share lot sizing even with small weights."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0],
        "High": [100.0, 100.0, 100.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"AAPL": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["AAPL"])
    # 0.001 on 100k is $100 -> exactly 1 share at price $100
    tw.iloc[0] = [0.001]

    res = run_allocation_backtest(universe, tw, us_trading=True)
    trades = res["rebalance_report"]
    assert len(trades) == 1
    assert trades.iloc[0]["shares"] == 1.0


def test_us_trading_sec_fee_on_sell_only():
    """US trading applies SEC Section 31 fee on sells only, not on buys."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0],
        "High": [100.0, 100.0, 100.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"SPY": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["SPY"])
    tw.iloc[0] = [0.50]  # Buy
    tw.iloc[1] = [0.0]   # Sell

    commission_pct = 0.0005
    slippage_pct = 0.0005
    sec_fee_pct = 0.0000278

    res = run_allocation_backtest(
        universe, tw, us_trading=True,
        commission_pct=commission_pct, slippage_pct=slippage_pct, sec_fee_pct=sec_fee_pct,
    )
    trades = res["rebalance_report"]
    assert len(trades) == 2

    buy_tr = trades.iloc[0]
    sell_tr = trades.iloc[1]

    # BUY: comm + slip
    assert buy_tr["action"] == "BUY"
    expected_buy_cost = buy_tr["trade_value"] * (commission_pct + slippage_pct)
    assert buy_tr["total_cost"] == pytest.approx(expected_buy_cost, rel=1e-5)

    # SELL: comm + slip + sec_fee
    assert sell_tr["action"] == "SELL"
    expected_sell_cost = sell_tr["trade_value"] * (commission_pct + slippage_pct + sec_fee_pct)
    assert sell_tr["total_cost"] == pytest.approx(expected_sell_cost, rel=1e-5)


def test_us_trading_t_plus_zero_intraday():
    """US trading permits T+0 selling (no T+1 hold)."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0],
        "High": [100.0, 100.0, 100.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"QQQ": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["QQQ"])
    tw.iloc[1] = [0.50]  # Buy on day 1
    tw.iloc[2] = [0.0]   # Sell on day 2

    res = run_allocation_backtest(universe, tw, us_trading=True)
    trades = res["rebalance_report"]
    assert len(trades) == 2
    assert trades.iloc[0]["action"] == "BUY"
    assert trades.iloc[1]["action"] == "SELL"


def test_hk_board_lot_lookup():
    """Verify HK board lot resolution."""
    assert _get_hk_board_lot("00005.HK") == 400
    assert _get_hk_board_lot("0005") == 400
    assert _get_hk_board_lot("00175.HK") == 1000
    assert _get_hk_board_lot("01211.HK") == 500
    assert _get_hk_board_lot("01810.HK") == 200
    assert _get_hk_board_lot("00941.HK") == 500
    assert _get_hk_board_lot("09618.HK") == 50
    assert _get_hk_board_lot("00700.HK") == 100
    assert _get_hk_board_lot("UNKNOWN.HK", default_lot=100) == 100


def test_hk_trading_board_lot_enforcement():
    """HK trading sizes orders to symbol-specific board lots."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    # Price is 50.0. HSBC board lot is 400 shares = $20,000 per lot.
    df = pd.DataFrame({
        "Open": [50.0, 50.0, 50.0],
        "High": [50.0, 50.0, 50.0],
        "Low": [50.0, 50.0, 50.0],
        "Close": [50.0, 50.0, 50.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"00005.HK": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["00005.HK"])
    # 0.35 on 100k is $35,000 -> raw shares = 700.
    # At 400 board lot, 700 // 400 = 1 lot = 400 shares.
    tw.iloc[0] = [0.35]

    res = run_allocation_backtest(universe, tw, hk_trading=True)
    trades = res["rebalance_report"]
    assert len(trades) == 1
    assert trades.iloc[0]["shares"] == 400.0


def test_hk_trading_stamp_duty_on_both_buy_and_sell():
    """HK trading applies 0.1085% stamp duty/levies on BOTH BUY and SELL."""
    idx = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0],
        "High": [100.0, 100.0, 100.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": [100000.0] * 3,
    }, index=idx)
    universe = {"00700.HK": df}
    tw = pd.DataFrame(np.nan, index=idx, columns=["00700.HK"])
    tw.iloc[0] = [0.50]  # Buy on day 0
    tw.iloc[1] = [0.0]   # Sell on day 1

    commission_pct = 0.0005
    slippage_pct = 0.0005
    hk_stamp_pct = 0.001085

    res = run_allocation_backtest(
        universe, tw, hk_trading=True,
        commission_pct=commission_pct, slippage_pct=slippage_pct, hk_stamp_duty_pct=hk_stamp_pct,
    )
    trades = res["rebalance_report"]
    assert len(trades) == 2

    buy_tr = trades.iloc[0]
    sell_tr = trades.iloc[1]

    # BUY: comm + slip + hk_stamp
    assert buy_tr["action"] == "BUY"
    expected_cost_rate = commission_pct + slippage_pct + hk_stamp_pct
    assert buy_tr["total_cost"] == pytest.approx(buy_tr["trade_value"] * expected_cost_rate, rel=1e-5)

    # SELL: comm + slip + hk_stamp
    assert sell_tr["action"] == "SELL"
    assert sell_tr["total_cost"] == pytest.approx(sell_tr["trade_value"] * expected_cost_rate, rel=1e-5)


def test_chan_causal_signals_default_true():
    """Ensure chan_causal_signals defaults to True in StrategyConfig."""
    cfg = StrategyConfig()
    assert cfg.chan_causal_signals is True

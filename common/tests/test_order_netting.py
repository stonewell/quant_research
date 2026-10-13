"""Unit tests for multi-strategy order netting engine and friction audit."""

import pytest
import pandas as pd
from common.order_netting import (
    OrderNettingEngine,
    SubStrategyIntent,
    NettingResult,
)


def test_exact_wash_trading_cancellation():
    """Verify that opposite orders of equal size are 100% crossed with zero master trade."""
    engine = OrderNettingEngine(lot_size_default=100)

    # Strategy 1 (50% weight): wants to buy 10% in 601872.SH (10,000 RMB / 10.0 = 1,000 shares)
    # Strategy 2 (50% weight): wants 0% in 601872.SH (currently holds 1,000 shares in its prior book)
    intents = [
        SubStrategyIntent(
            strategy_key="strat_bull",
            strategy_name="Bullish Strategy",
            capital_weight=0.50,
            allocated_capital=50000.0,
            target_weights={"601872.SH": 0.20},  # 10,000 RMB -> 1,000 shares
            current_weights={"601872.SH": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="strat_bear",
            strategy_name="Bearish Strategy",
            capital_weight=0.50,
            allocated_capital=50000.0,
            target_weights={"601872.SH": 0.0},   # wants 0 shares
            current_weights={"601872.SH": 0.20}, # prior held 1,000 shares
        ),
    ]

    # Master portfolio currently holds 1,000 shares
    current_holdings = {"601872.SH": 1000, "BIL": 90000.0}
    latest_prices = {"601872.SH": 10.0}
    portfolio_value = 100000.0

    prior_sub_ledgers = {
        "strat_bull": {"holdings": {"601872.SH": 0}},
        "strat_bear": {"holdings": {"601872.SH": 1000}},
    }

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=portfolio_value,
        latest_prices=latest_prices,
        prior_sub_ledgers=prior_sub_ledgers,
    )

    # Global target weight is 0.50 * 0.20 + 0.50 * 0.0 = 0.10 (10,000 RMB -> 1,000 shares)
    # Master account already holds 1,000 shares -> Net delta shares = 0!
    # Gross buy was 1,000 shares, gross sell was 1,000 shares -> 1,000 shares crossed!
    assert res.friction_savings.total_crossed_shares == 1000
    assert res.friction_savings.churn_reduction_ratio == 1.0  # 100% churn reduction!
    assert res.friction_savings.total_friction_saved > 0

    # Stock order row should be HOLD (net delta shares = 0)
    stock_row = [r for r in res.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row["delta_shares"] == 0
    assert stock_row["action"] == "HOLD"


def test_partial_netting_and_lot_rounding():
    """Verify partial contra-trade netting with round lot (100 shares) compliance."""
    engine = OrderNettingEngine(lot_size_default=100)

    # Strategy 1 (60% weight, 60k): wants 20% in 600028.SH (12,000 RMB / 5.0 = 2,400 shares)
    # Strategy 2 (40% weight, 40k): wants 0% in 600028.SH (prior held 800 shares)
    intents = [
        SubStrategyIntent(
            strategy_key="strat_a",
            strategy_name="Strat A",
            capital_weight=0.60,
            allocated_capital=60000.0,
            target_weights={"600028.SH": 0.20},
            current_weights={"600028.SH": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="strat_b",
            strategy_name="Strat B",
            capital_weight=0.40,
            allocated_capital=40000.0,
            target_weights={"600028.SH": 0.0},
            current_weights={"600028.SH": 0.10},
        ),
    ]

    current_holdings = {"600028.SH": 800, "BIL": 96000.0}
    latest_prices = {"600028.SH": 5.0}
    portfolio_value = 100000.0

    prior_sub_ledgers = {
        "strat_a": {"holdings": {"600028.SH": 0}},
        "strat_b": {"holdings": {"600028.SH": 800}},
    }

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=portfolio_value,
        latest_prices=latest_prices,
        prior_sub_ledgers=prior_sub_ledgers,
    )

    # Global target weight = 0.60 * 0.20 = 0.12 (12,000 RMB -> 2,400 shares)
    # Master currently holds 800 shares -> Net delta shares = +1,600 shares (BUY)
    # Gross buy: 2,400 shares; Gross sell: 800 shares -> Crossed: 800 shares!
    stock_row = [r for r in res.net_orders if r["symbol"] == "600028.SH"][0]
    assert stock_row["action"] == "BUY"
    assert stock_row["delta_shares"] == 1600
    assert stock_row["crossed_shares"] == 800
    assert res.friction_savings.total_crossed_shares == 800


def test_tri_core_blended_execution_hierarchy():
    """Verify Tri-Core (Crisis 40%, Risk-Managed 35%, Four-State 25%) execution sorting."""
    engine = OrderNettingEngine(lot_size_default=100)

    intents = [
        SubStrategyIntent(
            strategy_key="crisis_shield",
            strategy_name="Chan Single Crisis Shield Blend",
            capital_weight=0.40,
            allocated_capital=40000.0,
            target_weights={"300394.SZ": 0.20, "601872.SH": 0.20},
        ),
        SubStrategyIntent(
            strategy_key="risk_managed",
            strategy_name="Chan Risk-Managed Blend",
            capital_weight=0.35,
            allocated_capital=35000.0,
            target_weights={"601288.SH": 0.20, "601872.SH": 0.10},
        ),
        SubStrategyIntent(
            strategy_key="four_state_shield",
            strategy_name="Chan Four-State Shield Blend",
            capital_weight=0.25,
            allocated_capital=25000.0,
            target_weights={"601899.SH": 0.20, "300394.SZ": 0.10},
        ),
    ]

    current_holdings = {
        "601166.SH": 500,  # Legacy held stock targeted to 0 -> should be SELL
        "BIL": 90000.0,
    }
    latest_prices = {
        "300394.SZ": 50.0,
        "601872.SH": 8.0,
        "601288.SH": 4.0,
        "601899.SH": 15.0,
        "601166.SH": 18.0,
    }
    portfolio_value = 100000.0

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=portfolio_value,
        latest_prices=latest_prices,
    )

    # Phase 1: SELLS FIRST
    actions = [r["action"] for r in res.net_orders]
    assert "SELL" in actions
    assert "BUY" in actions
    assert "SWEEP_CASH" in actions or "RELEASE_CASH" in actions or "HOLD" in actions

    # Check order priority: all SELLs precede all BUYs
    sell_indices = [i for i, r in enumerate(res.net_orders) if r["action"] == "SELL"]
    buy_indices = [i for i, r in enumerate(res.net_orders) if r["action"] == "BUY"]
    assert max(sell_indices) < min(buy_indices)

    # Legacy 601166.SH must be in SELLS
    sell_symbols = [r["symbol"] for r in res.net_orders if r["action"] == "SELL"]
    assert "601166.SH" in sell_symbols


def test_tenure_lock_protection():
    """Verify that an asset held under min_holding_bars is prevented from premature liquidation."""
    engine = OrderNettingEngine(lot_size_default=100)

    intents = [
        SubStrategyIntent(
            strategy_key="strat_exit",
            strategy_name="Exit Strat",
            capital_weight=1.0,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.0},  # wants to liquidate
        )
    ]

    current_holdings = {"601872.SH": 1000, "BIL": 90000.0}
    latest_prices = {"601872.SH": 10.0}

    # Holding age is 1 day, while min_holding_bars is 2 days
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=100000.0,
        latest_prices=latest_prices,
        holding_bars={"601872.SH": 1},
        min_holding_bars=2,
    )

    stock_row = [r for r in res.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row["action"] == "HOLD_FILTERED"
    assert stock_row["delta_shares"] == 0
    assert "未满最低持仓周期" in stock_row["reason"]


def test_virtual_sub_ledger_conservation():
    """Verify that sum of virtual sub-ledgers equals total master account NAV."""
    engine = OrderNettingEngine(lot_size_default=100)

    intents = [
        SubStrategyIntent(
            strategy_key="strat_1",
            strategy_name="Strat 1",
            capital_weight=0.70,
            allocated_capital=70000.0,
            target_weights={"601872.SH": 0.50},  # 35,000 equity, 35,000 cash
        ),
        SubStrategyIntent(
            strategy_key="strat_2",
            strategy_name="Strat 2",
            capital_weight=0.30,
            allocated_capital=30000.0,
            target_weights={"601872.SH": 0.20},  # 6,000 equity, 24,000 cash
        ),
    ]

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"BIL": 100000.0},
        portfolio_value=100000.0,
        latest_prices={"601872.SH": 10.0},
    )

    total_sub_nav = sum(led["allocated_nav"] for led in res.sub_ledgers.values())
    total_sub_equity = sum(led["virtual_equity_value"] for led in res.sub_ledgers.values())
    total_sub_cash = sum(led["virtual_cash"] for led in res.sub_ledgers.values())

    assert abs(total_sub_nav - 100000.0) < 1e-4
    assert abs((total_sub_equity + total_sub_cash) - 100000.0) < 1e-4


def test_friction_savings_math():
    """Verify exact formula calculations for stamp duty, commission, and slippage savings."""
    engine = OrderNettingEngine(
        lot_size_default=100,
        stamp_duty=0.0005,
        commission=0.0003,
        slippage=0.0005,
    )

    intents = [
        SubStrategyIntent(
            strategy_key="s1",
            strategy_name="S1",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"600000.SH": 0.4},  # 20,000 RMB / 10 = 2,000 shares buy
        ),
        SubStrategyIntent(
            strategy_key="s2",
            strategy_name="S2",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"600000.SH": 0.0},  # sell 2,000 shares
            current_weights={"600000.SH": 0.4},
        ),
    ]

    prior_sub_ledgers = {
        "s1": {"holdings": {"600000.SH": 0}},
        "s2": {"holdings": {"600000.SH": 2000}},
    }

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"600000.SH": 2000, "BIL": 80000.0},
        portfolio_value=100000.0,
        latest_prices={"600000.SH": 10.0},
        prior_sub_ledgers=prior_sub_ledgers,
    )

    # 2,000 shares crossed at price 10.0:
    # Crossed sell value = 2,000 * 10 = 20,000 RMB
    # Crossed total volume (both sides) = 2,000 * 2 * 10 = 40,000 RMB
    # Expected saved stamp duty: 20,000 * 0.0005 = 10.0 RMB
    # Expected saved commission: 40,000 * 0.0003 = 12.0 RMB
    # Expected saved slippage: 40,000 * 0.0005 = 20.0 RMB
    # Total saved: 10 + 12 + 20 = 42.0 RMB
    fs = res.friction_savings
    assert fs.total_crossed_shares == 2000
    assert abs(fs.saved_stamp_duty - 10.0) < 1e-4
    assert abs(fs.saved_commission - 12.0) < 1e-4
    assert abs(fs.saved_slippage - 20.0) < 1e-4
    assert abs(fs.total_friction_saved - 42.0) < 1e-4


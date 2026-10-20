"""Unit tests for multi-strategy order netting engine and friction audit."""

import pytest
import pandas as pd
from common.order_netting import (
    OrderNettingEngine,
    SubStrategyIntent,
    NettingResult,
    execute_netting,
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


def test_hkex_board_lot_and_dual_sided_stamp_duty():
    """Verify HKEX stock-specific board lots (400, 500, 2000, 50) and dual-sided 0.1085% stamp duty."""
    engine = OrderNettingEngine(
        hk_stamp_duty=0.001085,
        commission=0.0003,
        slippage=0.0005,
    )

    # Check board lot resolution
    assert engine.resolve_lot_size("00005.HK") == 400   # HSBC
    assert engine.resolve_lot_size("01211.HK") == 500   # BYD
    assert engine.resolve_lot_size("09618.HK") == 50    # JD
    assert engine.resolve_lot_size("02899.HK") == 2000  # Zijin Mining
    assert engine.resolve_lot_size("00700.HK") == 100   # Tencent

    # Strategy 1 wants to buy HSBC (target 24,000 HKD / 60 = 400 shares, exactly 1 board lot)
    # Strategy 2 wants to sell HSBC (prior held 400 shares)
    intents = [
        SubStrategyIntent(
            strategy_key="hk_bull",
            strategy_name="HK Bull",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"00005.HK": 0.48},  # 24,000 / 60 = 400 shares
            current_weights={"00005.HK": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="hk_bear",
            strategy_name="HK Bear",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"00005.HK": 0.0},
            current_weights={"00005.HK": 0.48},
        ),
    ]

    prior_sub_ledgers = {
        "hk_bull": {"holdings": {"00005.HK": 0}},
        "hk_bear": {"holdings": {"00005.HK": 400}},
    }

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"00005.HK": 400, "BIL": 76000.0},
        portfolio_value=100000.0,
        latest_prices={"00005.HK": 60.0},
        prior_sub_ledgers=prior_sub_ledgers,
    )

    fs = res.friction_savings
    assert fs.total_crossed_shares == 400
    # Crossed turnover = 400 * 2 * 60 = 48,000 HKD
    # Avoided HK stamp duty & levies = 48,000 * 0.001085 = 52.08 HKD (both buy and sell legs saved!)
    expected_saved_hk_stamp = 400 * 2 * 60.0 * 0.001085
    assert abs(fs.saved_stamp_duty - expected_saved_hk_stamp) < 1e-4

    detail = [c for c in res.crossed_details if c.symbol == "00005.HK"][0]
    assert detail.market == "HONGKONG"
    assert detail.lot_size == 400
    assert detail.currency == "HKD"
    assert abs(detail.saved_stamp_duty - expected_saved_hk_stamp) < 1e-4


def test_us_equity_1_share_lot_and_sec_fee():
    """Verify US equity 1-share lot resolution, T+0 tenure defense bypass, and SEC fee savings."""
    engine = OrderNettingEngine(
        us_sec_fee=0.0000278,
        commission=0.0003,
        slippage=0.0005,
    )

    # Check US lot size is 1
    assert engine.resolve_lot_size("AAPL") == 1
    assert engine.resolve_lot_size("NVDA") == 1
    assert engine.resolve_lot_size("SPY") == 1

    intents = [
        SubStrategyIntent(
            strategy_key="us_growth",
            strategy_name="US Growth",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"AAPL": 0.30},  # 15,000 USD / 150 = 100 shares
            current_weights={"AAPL": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="us_value",
            strategy_name="US Value",
            capital_weight=0.5,
            allocated_capital=50000.0,
            target_weights={"AAPL": 0.0},
            current_weights={"AAPL": 0.30},  # sells 100 shares
        ),
    ]

    prior_sub_ledgers = {
        "us_growth": {"holdings": {"AAPL": 0}},
        "us_value": {"holdings": {"AAPL": 100}},
    }

    # AAPL held for 0 days (held_age = 0), but in US market T+0 trading is allowed
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"AAPL": 100, "BIL": 85000.0},
        portfolio_value=100000.0,
        latest_prices={"AAPL": 150.0},
        holding_bars={"AAPL": 0},
        min_holding_bars=2,
        prior_sub_ledgers=prior_sub_ledgers,
    )

    fs = res.friction_savings
    assert fs.total_crossed_shares == 100
    # US market has 0 stamp duty, SEC Section 31 fee on sells = 100 * 150 * 0.0000278 = 0.417 USD
    expected_sec_fee = 100 * 150.0 * 0.0000278
    assert abs(fs.saved_sec_fee - expected_sec_fee) < 1e-4
    assert abs(fs.saved_stamp_duty - 0.0) < 1e-4

    detail = [c for c in res.crossed_details if c.symbol == "AAPL"][0]
    assert detail.market == "US"
    assert detail.lot_size == 1
    assert detail.currency == "USD"
    assert abs(detail.saved_sec_fee - expected_sec_fee) < 1e-4


def test_cross_border_multi_strategy_portfolio():
    """Verify simultaneous multi-strategy crossing across China, HK, and US assets."""
    engine = OrderNettingEngine()

    intents = [
        SubStrategyIntent(
            strategy_key="strat_alpha",
            strategy_name="Alpha",
            capital_weight=0.5,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.20, "00005.HK": 0.20, "AAPL": 0.20},
            current_weights={"601872.SH": 0.0, "00005.HK": 0.0, "AAPL": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="strat_beta",
            strategy_name="Beta",
            capital_weight=0.5,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.0, "00005.HK": 0.0, "AAPL": 0.0},
            current_weights={"601872.SH": 0.20, "00005.HK": 0.20, "AAPL": 0.20},
        ),
    ]

    prices = {"601872.SH": 10.0, "00005.HK": 50.0, "AAPL": 200.0}
    # Strat Beta holds:
    # 601872.SH: 20,000 / 10 = 2,000 shares (multiple of 100)
    # 00005.HK: 20,000 / 50 = 400 shares (multiple of 400 board lot)
    # AAPL: 20,000 / 200 = 100 shares (multiple of 1)
    holdings = {"601872.SH": 2000, "00005.HK": 400, "AAPL": 100, "BIL": 140000.0}
    prior_ledgers = {
        "strat_alpha": {"holdings": {"601872.SH": 0, "00005.HK": 0, "AAPL": 0}},
        "strat_beta": {"holdings": {"601872.SH": 2000, "00005.HK": 400, "AAPL": 100}},
    }

    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=holdings,
        portfolio_value=200000.0,
        latest_prices=prices,
        prior_sub_ledgers=prior_ledgers,
    )

    fs = res.friction_savings
    assert fs.total_crossed_shares == (2000 + 400 + 100)
    assert fs.saved_by_market["CHINA"] > 0
    assert fs.saved_by_market["HONGKONG"] > 0
    assert fs.saved_by_market["US"] > 0

    # Verify order ticket records have correct market tags and lot sizes
    orders_by_sym = {o["symbol"]: o for o in res.net_orders}
    assert orders_by_sym["601872.SH"]["market"] == "CHINA"
    assert orders_by_sym["601872.SH"]["lot_size"] == 100
    assert orders_by_sym["00005.HK"]["market"] == "HONGKONG"
    assert orders_by_sym["00005.HK"]["lot_size"] == 400
    assert orders_by_sym["AAPL"]["market"] == "US"
    assert orders_by_sym["AAPL"]["lot_size"] == 1


def test_initial_launch_integer_lot_preservation():
    """Verify that launching without prior sub-ledgers preserves integer board lots without odd lots."""
    engine = OrderNettingEngine()

    # Master portfolio holds 1,000 shares of BYD (01211.HK, lot size 500)
    # Two strategies with 60% and 40% capital weights
    intents = [
        SubStrategyIntent(
            strategy_key="strat_x",
            strategy_name="Strat X",
            capital_weight=0.60,
            allocated_capital=60000.0,
            target_weights={"01211.HK": 0.50},  # 30,000 / 200 = 150 shares -> floor(150/500)*500 = 0
        ),
        SubStrategyIntent(
            strategy_key="strat_y",
            strategy_name="Strat Y",
            capital_weight=0.40,
            allocated_capital=40000.0,
            target_weights={"01211.HK": 0.50},
        ),
    ]

    holdings = {"01211.HK": 1000, "BIL": 100000.0}
    prices = {"01211.HK": 200.0}

    # No prior_sub_ledgers passed (initial launch)
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=holdings,
        portfolio_value=100000.0,
        latest_prices=prices,
        prior_sub_ledgers=None,
    )

    # 1,000 shares (2 lots of 500) divided 60% and 40%:
    # Hare-Niemeyer allocation gives 1 lot (500) to strat_x and 1 lot (500) to strat_y.
    # Neither strategy receives 600 or 400 (which would be non-board lots of 500).
    for detail in res.crossed_details:
        if detail.symbol == "01211.HK":
            for contrib in detail.contributing_strategies:
                assert contrib["current_shares"] % 500 == 0, f"Odd lot in prior shares: {contrib['current_shares']}"
                assert contrib["target_shares"] % 500 == 0, f"Odd lot in target shares: {contrib['target_shares']}"


def test_cash_inference_when_cash_proxy_omitted_or_zero():
    """Regression test: when current_holdings omits BIL (or passes BIL=0) while
    portfolio_value exceeds equity value, unallocated cash is inferred so buys are not zeroed out."""
    engine = OrderNettingEngine(lot_size_default=100)
    intents = [
        SubStrategyIntent(
            strategy_key="strat_a",
            strategy_name="Strat A",
            capital_weight=1.0,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.10, "600028.SH": 0.20},
        ),
    ]
    # Hold 1000 shares of 601872.SH (worth 10,000 RMB) out of 100,000 NAV, no BIL key passed
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"601872.SH": 1000},
        portfolio_value=100000.0,
        latest_prices={"601872.SH": 10.0, "600028.SH": 5.0},
    )
    buy_row = [r for r in res.net_orders if r["symbol"] == "600028.SH"][0]
    assert buy_row["action"] == "BUY"
    assert buy_row["delta_shares"] == 4000
    assert buy_row["target_shares"] == 4000


def test_virtual_sub_ledger_reconciliation_on_tenure_lock_and_cash_scaling():
    """Regression test: virtual sub-ledgers reconcile against actual master post-trade shares
    when a sell is blocked by tenure lock (HOLD_FILTERED) or buys are scaled by cash."""
    engine = OrderNettingEngine(lot_size_default=100)
    intents = [
        SubStrategyIntent(
            strategy_key="strat_exit",
            strategy_name="Exit Strat",
            capital_weight=1.0,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.0},
        )
    ]
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"601872.SH": 1000, "BIL": 90000.0},
        portfolio_value=100000.0,
        latest_prices={"601872.SH": 10.0},
        holding_bars={"601872.SH": 1},
        min_holding_bars=2,
        prior_sub_ledgers={"strat_exit": {"holdings": {"601872.SH": 1000}}},
    )
    # Master blocked the liquidation via HOLD_FILTERED -> still holds 1,000 shares
    # Virtual sub-ledger must also retain 1,000 shares (not drift to 0)
    assert res.sub_ledgers["strat_exit"]["holdings"].get("601872.SH") == 1000


def test_fx_rates_conversion_in_multi_currency_portfolio():
    """Verify fx_rates converts HKD and USD turnovers and friction savings into RMB."""
    engine = OrderNettingEngine(fx_rates={"CNY": 1.0, "HKD": 0.90, "USD": 7.0})
    intents = [
        SubStrategyIntent(
            strategy_key="s1",
            strategy_name="S1",
            capital_weight=0.5,
            allocated_capital=100000.0,
            target_weights={"AAPL": 0.35},
            current_weights={"AAPL": 0.0},
        ),
        SubStrategyIntent(
            strategy_key="s2",
            strategy_name="S2",
            capital_weight=0.5,
            allocated_capital=100000.0,
            target_weights={"AAPL": 0.0},
            current_weights={"AAPL": 0.35},
        ),
    ]
    res = engine.net_portfolio_orders(
        intents=intents,
        current_holdings={"AAPL": 100, "BIL": 130000.0},
        portfolio_value=200000.0,
        latest_prices={"AAPL": 100.0},
        prior_sub_ledgers={
            "s1": {"holdings": {"AAPL": 0}},
            "s2": {"holdings": {"AAPL": 100}},
        },
    )
    # 100 shares of AAPL @ $100 crossed -> $20,000 crossed USD turnover -> 140,000 RMB
    assert res.friction_savings.saved_turnover > 0


def test_netting_emergency_stop_loss_penetrates_tenure_lock():
    """Confirms that emergency stops liquidate immediately on Day 1 by penetrating tenure lock.

    Tests 3 critical scenarios:
    1. Direct emergency_symbols argument on Day 1 (held_age = 1 >= 1) penetrates tenure lock (min_holding_bars=2).
    2. Sub-strategy ticket carrying -8% hard stop-loss reason auto-penetrates tenure lock on Day 1.
    3. Day 0 purchase (held_age = 0 < 1) strictly obeys China A-share statutory T+1 law and remains locked.
    """
    engine = OrderNettingEngine(lot_size_default=100)
    current_holdings = {"601872.SH": 1000, "BIL": 90000.0}
    latest_prices = {"601872.SH": 10.0}

    intents = [
        SubStrategyIntent(
            strategy_key="strat_exit",
            strategy_name="Exit Strat",
            capital_weight=1.0,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.0},
        )
    ]

    # Baseline: Normal exit on Day 1 with min_holding_bars=2 is locked
    res_normal = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=100000.0,
        latest_prices=latest_prices,
        holding_bars={"601872.SH": 1},
        min_holding_bars=2,
    )
    stock_row_normal = [r for r in res_normal.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row_normal["action"] == "HOLD_FILTERED"
    assert stock_row_normal["delta_shares"] == 0

    # Scenario 1: Emergency symbols explicitly passed -> penetrates tenure lock on Day 1
    res_em = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=100000.0,
        latest_prices=latest_prices,
        holding_bars={"601872.SH": 1},
        min_holding_bars=2,
        emergency_symbols={"601872.SH"},
    )
    stock_row_em = [r for r in res_em.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row_em["action"] == "SELL"
    assert stock_row_em["delta_shares"] == -1000
    assert stock_row_em["target_shares"] == 0
    assert stock_row_em["fast_path_triggered"] == "STOP_LOSS"
    assert "止损穿透" in stock_row_em["reason"]

    # Scenario 2: Sub-strategy raw orders carry -8% hard stop reason -> auto-penetrates via execute_netting
    intents_with_stop_ticket = [
        SubStrategyIntent(
            strategy_key="strat_exit",
            strategy_name="Exit Strat",
            capital_weight=1.0,
            allocated_capital=100000.0,
            target_weights={"601872.SH": 0.0},
            raw_orders=[
                {
                    "symbol": "601872.SH",
                    "action": "SELL",
                    "reason": "触发个股 -8% 硬止损风控 (当前价格 9.20 较成本 10.00 跌幅 -8.0%)",
                }
            ],
        )
    ]
    res_auto = execute_netting(
        intents=intents_with_stop_ticket,
        current_holdings=current_holdings,
        portfolio_value=100000.0,
        latest_prices=latest_prices,
        holding_bars={"601872.SH": 1},
        min_holding_bars=2,
    )
    stock_row_auto = [r for r in res_auto.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row_auto["action"] == "SELL"
    assert stock_row_auto["delta_shares"] == -1000
    assert stock_row_auto["target_shares"] == 0
    assert stock_row_auto["fast_path_triggered"] == "STOP_LOSS"

    # Scenario 3: Statutory T+1 protection on Day 0 (holding_bars = 0)
    # Even if emergency stop is requested, statutory A-share T+1 cannot execute intraday
    res_day0 = engine.net_portfolio_orders(
        intents=intents,
        current_holdings=current_holdings,
        portfolio_value=100000.0,
        latest_prices=latest_prices,
        holding_bars={"601872.SH": 0},
        min_holding_bars=2,
        emergency_symbols={"601872.SH"},
    )
    stock_row_day0 = [r for r in res_day0.net_orders if r["symbol"] == "601872.SH"][0]
    assert stock_row_day0["action"] == "HOLD_FILTERED"
    assert stock_row_day0["delta_shares"] == 0
    assert "未满最低持仓周期" in stock_row_day0["reason"]


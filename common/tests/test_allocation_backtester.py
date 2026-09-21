"""Unit tests for common/allocation_backtester.py."""

import os
import sys

import numpy as np
import pandas as pd

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import run_allocation_backtest
from common.testing import make_ohlcv_from_closes as make_df


def test_allocation_backtester_disjoint_date_ranges_returns_empty_result():
    # Regression test: target_weights and the universe's OHLCV used to be
    # aligned via closes.index.intersection(target_weights.index) with no
    # check that the intersection is non-empty. When the two cover disjoint
    # date ranges (e.g. a template's rebalance schedule was computed against
    # a different calendar), `common_idx` came out zero-length, and
    # `equity[0] = initial_capital` raised an unhandled IndexError on the
    # zero-length `equity` array. This must instead short-circuit exactly
    # like the existing empty-target_weights/empty-universe cases.
    universe = {"A": make_df([100.0] * 10, start="2020-01-01")}

    # A completely disjoint date range, far in the future.
    disjoint_idx = pd.bdate_range("2030-01-01", periods=5)
    target_weights = pd.DataFrame(np.nan, index=disjoint_idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights)

    assert set(res.keys()) == {"equity_curve", "turnover"}
    assert res["equity_curve"].empty
    assert res["turnover"] == 0.0


def test_allocation_backtester_overlapping_date_ranges_still_runs():
    # Sanity check alongside the disjoint-range regression test above: a
    # partial overlap should NOT be treated as "no data" -- only a truly
    # empty intersection short-circuits.
    idx = pd.bdate_range("2020-01-01", periods=10)
    universe = {"A": make_df([100.0] * 10, start="2020-01-01")}

    # Overlaps the last 3 bars of the universe's calendar, then extends past it.
    overlap_idx = idx[-3:].append(pd.bdate_range(idx[-1] + pd.Timedelta(days=10), periods=2))
    target_weights = pd.DataFrame(np.nan, index=overlap_idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights)

    assert not res["equity_curve"].empty
    assert len(res["equity_curve"]) == 3


def test_rebalance_report_columns_and_reconciliation():
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {
        "A": make_df([100.0, 200.0, 200.0], start="2020-01-01"),
        "B": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
    }

    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.loc[idx[0]] = [0.5, 0.5]
    target_weights.loc[idx[2]] = [0.5, 0.5]

    comm_pct = 0.001
    slip_pct = 0.0005
    res = run_allocation_backtest(
        universe,
        target_weights,
        initial_capital=100_000.0,
        commission_pct=comm_pct,
        slippage_pct=slip_pct,
    )

    rebal_df = res["rebalance_report"]
    assert isinstance(rebal_df, pd.DataFrame)
    from common.allocation_backtester import REBALANCE_REPORT_COLUMNS
    assert list(rebal_df.columns) == REBALANCE_REPORT_COLUMNS

    # Day 0 trades: 2 BUY trades
    day0_trades = rebal_df[rebal_df["rebalance_id"] == 1]
    assert len(day0_trades) == 2
    assert set(day0_trades["action"]) == {"BUY"}
    assert (day0_trades["prior_weight"] == 0.0).all()
    assert (day0_trades["prior_shares"] == 0.0).all()
    assert (day0_trades["target_weight"] == 0.5).all()
    assert (day0_trades["trade_value"] == 50_000.0).all()
    assert (day0_trades["shares"] == 500.0).all()

    # Total Day 0 cost reconciliation
    expected_day0_cost = 100_000.0 * 1.0 * (comm_pct + slip_pct)
    np.testing.assert_allclose(day0_trades["total_cost"].sum(), expected_day0_cost)

    # Day 2 trades: 1 SELL (A) and 1 BUY (B)
    day2_trades = rebal_df[rebal_df["rebalance_id"] == 2]
    assert len(day2_trades) == 2

    a_trade = day2_trades[day2_trades["symbol"] == "A"].iloc[0]
    b_trade = day2_trades[day2_trades["symbol"] == "B"].iloc[0]

    assert a_trade["action"] == "SELL"
    assert b_trade["action"] == "BUY"

    # Pre-rebalance weights were 2/3 (A) and 1/3 (B) due to A doubling on day 1
    np.testing.assert_allclose(a_trade["prior_weight"], 2.0 / 3.0, atol=1e-4)
    np.testing.assert_allclose(b_trade["prior_weight"], 1.0 / 3.0, atol=1e-4)
    np.testing.assert_allclose(a_trade["weight_change"], 0.5 - 2.0 / 3.0, atol=1e-4)
    np.testing.assert_allclose(b_trade["weight_change"], 0.5 - 1.0 / 3.0, atol=1e-4)

    # Mathematical consistency: sum of trade values / equity equals turnover (within discrete share rounding)
    pre_equity = a_trade["portfolio_equity"]
    turnover = abs(a_trade["weight_change"]) + abs(b_trade["weight_change"])
    np.testing.assert_allclose(day2_trades["trade_value"].sum() / pre_equity, turnover, atol=2e-3)

    # Cost reconciliation
    expected_day2_cost = pre_equity * turnover * (comm_pct + slip_pct)
    np.testing.assert_allclose(day2_trades["total_cost"].sum(), expected_day2_cost, atol=0.5)


def test_rebalance_report_skips_untraded_assets():
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {
        "A": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
        "B": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
        "C": make_df([50.0, 50.0, 50.0], start="2020-01-01"),
    }

    # C is always 0, A is always 1.0, flat prices -> no drift on day 2
    target_weights = pd.DataFrame(0.0, index=idx, columns=["A", "B", "C"])
    target_weights["A"] = 1.0

    res = run_allocation_backtest(universe, target_weights)
    rebal_df = res["rebalance_report"]

    # C and B never have weight changes, so they should never appear
    assert "C" not in rebal_df["symbol"].values
    assert "B" not in rebal_df["symbol"].values

    # Only Day 0 trade for A (from 0.0 to 1.0)
    assert len(rebal_df) == 1
    assert rebal_df.iloc[0]["symbol"] == "A"
    assert rebal_df.iloc[0]["action"] == "BUY"


def test_format_rebalance_trades_preview():
    from common.reporting import format_rebalance_trades_preview

    # Empty report
    assert format_rebalance_trades_preview(pd.DataFrame()) == "No rebalance trades recorded."
    assert format_rebalance_trades_preview(None) == "No rebalance trades recorded."

    # Sample trade df
    df = pd.DataFrame([{
        "rebalance_id": 1,
        "date": "2024-01-02",
        "symbol": "SPY",
        "action": "BUY",
        "price": 475.50,
        "prior_weight": 0.0,
        "target_weight": 0.5,
        "weight_change": 0.5,
        "trade_value": 50000.0,
        "shares": 105.15,
        "total_cost": 50.0,
    }])

    table_str = format_rebalance_trades_preview(df)
    assert "SPY" in table_str
    assert "BUY" in table_str
    assert "$475.50" in table_str
    assert "50.0%" in table_str


def test_no_fractional_shares_trading_default():
    # Verify that default min_shares=1 enforces whole shares with no fractions
    idx = pd.bdate_range("2020-01-01", periods=5)
    universe = {
        "SPY": make_df([475.50, 480.25, 478.10, 482.00, 485.00], start="2020-01-01"),
        "QQQ": make_df([312.33, 315.50, 310.00, 320.00, 322.00], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["SPY", "QQQ"])
    target_weights.iloc[0] = [0.5, 0.5]
    target_weights.iloc[2] = [0.4, 0.6]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0)
    rebal_df = res["rebalance_report"]
    assert not rebal_df.empty

    for _, row in rebal_df.iterrows():
        # Shares must be exact integer values
        assert row["shares"] > 0
        assert row["shares"] == int(row["shares"])
        assert row["prior_shares"] == int(row["prior_shares"])
        assert row["target_shares"] == int(row["target_shares"])
        # trade_value must equal shares * price
        np.testing.assert_allclose(row["trade_value"], row["shares"] * row["price"])


def test_min_shares_suppresses_micro_trades():
    idx = pd.bdate_range("2020-01-01", periods=4)
    # Very high stock price ($90,000) so tiny weight changes correspond to < 1 share
    universe = {
        "BRK": make_df([90_000.0, 90_000.0, 90_000.0, 90_000.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["BRK"])
    # Day 0: allocate 90% ($90k of $100k -> 1 share)
    target_weights.iloc[0] = [0.90]
    # Day 2: rebalance to 0.9001 (a tiny delta that is << 1 share of a $90k stock)
    target_weights.iloc[2] = [0.9001]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=1)
    rebal_df = res["rebalance_report"]
    # Only Day 0 trade should exist; Day 2 micro-delta must be suppressed
    assert len(rebal_df) == 1
    assert rebal_df.iloc[0]["rebalance_id"] == 1


def test_min_shares_lot_sizing():
    idx = pd.bdate_range("2020-01-01", periods=4)
    universe = {
        "A": make_df([10.0, 10.0, 10.0, 10.0], start="2020-01-01"),
        "B": make_df([10.0, 10.0, 10.0, 10.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    # Day 0: 50% A ($5,000 -> 500 shares), 50% B ($5,000 -> 500 shares)
    target_weights.iloc[0] = [0.5, 0.5]
    # Day 2: 70% A ($7,000 -> desired 700 shares, delta = +200 shares)
    #        30% B ($3,000 -> desired 300 shares, delta = -200 shares)
    target_weights.iloc[2] = [0.7, 0.3]

    res = run_allocation_backtest(universe, target_weights, initial_capital=10_000.0, min_shares=100)
    rebal_df = res["rebalance_report"]
    assert len(rebal_df) == 4

    for _, row in rebal_df.iterrows():
        assert row["shares"] % 100 == 0
        assert row["prior_shares"] % 100 == 0
        assert row["target_shares"] % 100 == 0


def test_min_shares_full_exit_liquidation():
    # When min_shares=100 and an odd lot is held (e.g. 50 shares), full exit to 0.0 must sell all 50 shares
    idx = pd.bdate_range("2020-01-01", periods=4)
    universe = {
        "A": make_df([100.0, 100.0, 100.0, 100.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    # Day 0 with min_shares=50: buys 50 shares ($5,000 / 100)
    target_weights.iloc[0] = [0.5]
    # Day 2: strategy completely exits (target_weight = 0.0)
    target_weights.iloc[2] = [0.0]

    res = run_allocation_backtest(universe, target_weights, initial_capital=10_000.0, min_shares=50)
    rebal_df = res["rebalance_report"]
    assert len(rebal_df) == 2
    exit_trade = rebal_df.iloc[1]
    assert exit_trade["action"] == "SELL"
    assert exit_trade["shares"] == 50.0
    assert exit_trade["target_shares"] == 0.0
    assert exit_trade["prior_shares"] == 50.0


def test_invalid_min_shares_raises_value_error():
    import pytest
    universe = {"A": make_df([100.0] * 5)}
    target_weights = pd.DataFrame(0.5, index=pd.bdate_range("2020-01-01", periods=5), columns=["A"])

    with pytest.raises(ValueError, match="min_shares must be an integer >= 0"):
        run_allocation_backtest(universe, target_weights, min_shares=-1)

    with pytest.raises(ValueError, match="min_shares must be an integer >= 0"):
        run_allocation_backtest(universe, target_weights, min_shares=-10)

    with pytest.raises(ValueError, match="min_shares must be an integer >= 0"):
        run_allocation_backtest(universe, target_weights, min_shares="1")

    with pytest.raises(ValueError, match="min_shares must be an integer >= 0"):
        run_allocation_backtest(universe, target_weights, min_shares=True)


def test_min_shares_zero_enables_fractional_trading():
    import pytest
    # min_shares=0 allows fractional / unconstrained trading (e.g. for benchmark index backtests)
    universe = {"A": make_df([3000.0] * 5)}
    idx = pd.bdate_range("2020-01-01", periods=5)
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    # With initial_capital=100k and price=3000:
    # If min_shares=100 was required, 100*3000 = 300k > 100k -> 0 shares bought!
    # With min_shares=0, fractional shares 100,000 / 3,000 = 33.3333 shares are bought.
    result = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=0, commission_pct=0.0, slippage_pct=0.0)
    trades = result["rebalance_report"]
    assert len(trades) == 1
    assert np.isclose(trades.iloc[0]["shares"], 100_000.0 / 3000.0)
    assert result["equity_curve"].iloc[-1]["equity"] == pytest.approx(100_000.0)


def test_min_shares_prevents_ghost_leverage_on_suppressed_sell():
    # When asset A buys (+30%) but asset B's sell (-30%) is suppressed because
    # B has a huge share price ($100k) making delta < min_shares,
    # total exposure must not exceed 1.0 (ghost leverage prevention).
    idx = pd.bdate_range("2020-01-01", periods=4)
    universe = {
        "A": make_df([10.0, 10.0, 10.0, 10.0], start="2020-01-01"),
        "B": make_df([100_000.0, 100_000.0, 100_000.0, 100_000.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    # Day 0: 50% A ($50k -> 5000 shares), 50% B ($50k -> 0 shares because $100k stock > $50k)
    target_weights.iloc[0] = [0.5, 0.5]
    # Day 2: rebalance to 80% A, 20% B
    target_weights.iloc[2] = [0.8, 0.2]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=1)
    actual_w = res["actual_weights"]
    for d in idx:
        assert np.sum(np.abs(actual_w.loc[d])) <= 1.0 + 1e-7


def test_min_shares_zero_target_with_zero_prior_shares():
    # When target_w is 0.0 and prior_s is 0, it should safely clear weight without attempting an invalid trade
    idx = pd.bdate_range("2020-01-01", periods=4)
    universe = {
        "A": make_df([100.0, 100.0, 100.0, 100.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    target_weights.iloc[0] = [0.0]
    target_weights.iloc[2] = [0.0]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=1)
    rebal_df = res["rebalance_report"]
    assert rebal_df.empty
    assert (res["actual_weights"]["A"] == 0.0).all()



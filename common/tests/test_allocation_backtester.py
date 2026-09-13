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

    # Mathematical consistency: sum of trade values / equity equals turnover
    pre_equity = a_trade["portfolio_equity"]
    turnover = abs(a_trade["weight_change"]) + abs(b_trade["weight_change"])
    np.testing.assert_allclose(day2_trades["trade_value"].sum() / pre_equity, turnover)

    # Cost reconciliation
    expected_day2_cost = pre_equity * turnover * (comm_pct + slip_pct)
    np.testing.assert_allclose(day2_trades["total_cost"].sum(), expected_day2_cost)


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

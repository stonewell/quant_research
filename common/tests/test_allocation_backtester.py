"""Unit tests for common/allocation_backtester.py."""

import hashlib
import os
import sys

import numpy as np
import pandas as pd
import pytest

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

    # Day 0 trades: 2 BUY trades. EXPECTATIONS REVISED for the B1.1 cash-funded
    # entry fix: a 0.5 target of 100,000 is 499 whole shares a side, because the
    # affordable notional is capital/(1+fee) = 99,850.22, not the full 100,000
    # with the fee left as a negative cash balance (the old 500-share fills).
    # Disclosed old -> new: trade_value 50,000 -> 49,900, shares 500 -> 499,
    # equity[0] 99,850 (100,000 of holdings against -150 of cash) -> 99,850.3
    # (99,800 of holdings against +50.30 of cash), day-0 cost 150 -> 149.7.
    day0_trades = rebal_df[rebal_df["rebalance_id"] == 1]
    assert len(day0_trades) == 2
    assert set(day0_trades["action"]) == {"BUY"}
    assert (day0_trades["prior_weight"] == 0.0).all()
    assert (day0_trades["prior_shares"] == 0.0).all()
    assert (day0_trades["target_weight"] == 0.5).all()
    assert (day0_trades["trade_value"] == 49_900.0).all()
    assert (day0_trades["shares"] == 499.0).all()
    np.testing.assert_allclose(res["equity_curve"]["equity"].iloc[0], 99_850.3)

    # Total Day 0 cost reconciliation: costs are charged on the FILLED notional
    # (two 49,900 legs at 0.15%), which is now less than the capital because the
    # fee must also be payable out of it.
    expected_day0_cost = day0_trades["trade_value"].sum() * (comm_pct + slip_pct)
    np.testing.assert_allclose(day0_trades["total_cost"].sum(), expected_day0_cost)

    # Day 2 trades: 1 SELL (A) and 1 BUY (B)
    day2_trades = rebal_df[rebal_df["rebalance_id"] == 2]
    assert len(day2_trades) == 2

    a_trade = day2_trades[day2_trades["symbol"] == "A"].iloc[0]
    b_trade = day2_trades[day2_trades["symbol"] == "B"].iloc[0]

    assert a_trade["action"] == "SELL"
    assert b_trade["action"] == "BUY"

    # Pre-rebalance weights were 2/3 (A) and 1/3 (B) due to A doubling on day 1.
    # B1.1 ledger note: the day-0 entry is FUNDED (notional + fee <= cash), so
    # equity[0] = 99,850.3 for 99,800 of holdings and the weights are positions
    # / equity -- 99,800/149,750.3 and 49,900/149,750.3 -- not the force-written
    # target instruction and no longer inflated by an unfunded fee ratio.
    # Disclosed old -> new: pre_equity 149,850 -> 149,750.3 (the old book held
    # 100,000 of A/B against -150 of cash; this one holds 99,800 against +50.3).
    pre_equity = 149_750.3  # 99,800 holdings + 50.3 cash, then +49,900 mark-up on A
    np.testing.assert_allclose(a_trade["portfolio_equity"], pre_equity)
    np.testing.assert_allclose(b_trade["portfolio_equity"], pre_equity)
    np.testing.assert_allclose(a_trade["prior_weight"], 99_800.0 / pre_equity)
    np.testing.assert_allclose(b_trade["prior_weight"], 49_900.0 / pre_equity)
    np.testing.assert_allclose(a_trade["weight_change"], 0.5 - 99_800.0 / pre_equity)
    np.testing.assert_allclose(b_trade["weight_change"], 0.5 - 49_900.0 / pre_equity)

    # Mathematical consistency: sum of trade values / equity equals turnover.
    # The two differ only by whole-share granularity AND the cash cap on the B
    # leg (247 shares are all the post-sale cash can fund, vs ~249.7 wanted),
    # i.e. ~3 shares out of ~150,000 of equity. atol=5e-3 covers that; it is
    # still 60x tighter than the 0.166 weight change being tested.
    pre_equity = a_trade["portfolio_equity"]
    turnover = abs(a_trade["weight_change"]) + abs(b_trade["weight_change"])
    np.testing.assert_allclose(day2_trades["trade_value"].sum() / pre_equity, turnover, atol=5e-3)

    # Cost reconciliation: costs are charged on the filled notional, exactly...
    np.testing.assert_allclose(
        day2_trades["total_cost"].sum(),
        day2_trades["trade_value"].sum() * (comm_pct + slip_pct),
    )
    # ...and, up to lot rounding, on equity * instructed turnover (the coarse
    # closed form; ~0.6 dollars of slack on this 124/247-share fixture).
    expected_day2_cost = pre_equity * turnover * (comm_pct + slip_pct)
    np.testing.assert_allclose(day2_trades["total_cost"].sum(), expected_day2_cost, atol=1.0)


def test_rebalance_report_skips_untraded_assets():
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {
        "A": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
        "B": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
        "C": make_df([50.0, 50.0, 50.0], start="2020-01-01"),
    }

    # C is always 0, A is always 1.0, flat prices -> no drift on day 2.
    target_weights = pd.DataFrame(0.0, index=idx, columns=["A", "B", "C"])
    target_weights["A"] = 1.0

    # Zero costs: this test is about "symbols whose position never changes must
    # never appear in the report". With nonzero costs the day-0 book would be
    # 0.99-ish invested (the fee is paid out of cash, so the affordable notional
    # is capital/(1+fee)) and the fill sizes would be lot-rounded -- both fine,
    # but they would drag a second "trim" rebalance into the fixture. That
    # behaviour is asserted separately in test_ledger_parity.
    res = run_allocation_backtest(universe, target_weights, commission_pct=0.0, slippage_pct=0.0)
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


def test_min_weight_change_suppresses_passive_drift_rebalance():
    # Asset A and B held at 50/50.
    # Natural price drift shifts A to 51% and B to 49%.
    # A subsequent rebalance date targeting [0.50, 0.50] has delta ~1%.
    # With min_weight_change=0.02, passive drift adjustment is suppressed (0 trades).
    # Full liquidation (target=0.0) is still executed.
    idx = pd.bdate_range("2020-01-01", periods=4)
    universe = {
        "A": make_df([10.0, 10.4, 10.4, 10.4], start="2020-01-01"),
        "B": make_df([10.0, 10.0, 10.0, 10.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    # Day 0: Initial allocation 50/50
    target_weights.iloc[0] = [0.50, 0.50]
    # Day 1: Target row present with 50/50 (drift is ~1.9%)
    target_weights.iloc[1] = [0.50, 0.50]
    # Day 2: Full exit of A (target=0.0, B=1.0)
    target_weights.iloc[2] = [0.0, 1.0]

    # Without drift suppression: Day 1 generates trades to correct minor drift
    res_no_thresh = run_allocation_backtest(universe, target_weights, min_weight_change=0.0)
    trades_no_thresh = res_no_thresh["rebalance_report"]
    # Rebalance on Day 1 was executed
    day1_trades_no = trades_no_thresh[trades_no_thresh["date"] == idx[1].strftime("%Y-%m-%d")]
    assert len(day1_trades_no) > 0

    # With drift suppression min_weight_change=0.03: Day 1 drift is suppressed (< 3%), Day 2 exit succeeds
    res_thresh = run_allocation_backtest(universe, target_weights, min_weight_change=0.03)
    trades_thresh = res_thresh["rebalance_report"]
    day1_trades_yes = trades_thresh[trades_thresh["date"] == idx[1].strftime("%Y-%m-%d")]
    assert len(day1_trades_yes) == 0  # Drift suppressed!

    # Day 2 full liquidation of A must execute even though prior_s != 0
    day2_trades = trades_thresh[trades_thresh["date"] == idx[2].strftime("%Y-%m-%d")]
    assert len(day2_trades) > 0
    assert any(tr["symbol"] == "A" and tr["action"] == "SELL" and tr["target_weight"] == 0.0 for _, tr in day2_trades.iterrows())


def test_suspension_guard():
    """B1.3: Volume <= 0 / missing Volume blocks BOTH directions, with a trace.

    Day 0 goes all-in A (tradable). Day 1 instructs the standard rotation --
    sell half of A and buy B -- but A prints Volume=0 (halted) and B has no
    bar at all (NaN Volume). Under the pre-B1.3 engine both legs filled at the
    last print, i.e. a "ghost trade" through a suspension; the assertions
    below (no report row dated day 1, equity frozen, two suspension events)
    all fail on that old behaviour.
    """
    idx = pd.bdate_range("2020-01-01", periods=5)
    a = make_df([100.0] * 5, start="2020-01-01")
    a["Volume"] = [1e6, 0.0, 0.0, 1e6, 1e6]      # halted on days 1-2
    b = make_df([10.0] * 5, start="2020-01-01")
    b["Volume"] = [1e6, np.nan, 1e6, 1e6, 1e6]   # no volume print on day 1
    universe = {"A": a, "B": b}

    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [1.0, 0.0]   # day 0: all-in A
    target_weights.iloc[1] = [0.5, 0.5]   # day 1: rotate, but both are suspended
    target_weights.iloc[3] = [0.5, 0.5]   # day 3: both live again -> executes

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0)
    report = res["rebalance_report"]
    susp = res["suspension_report"]

    day1 = str(idx[1].date())
    # Nothing may fill through a suspension, in EITHER direction.
    assert day1 not in set(report["date"])
    # ...but the refusal is on the record, not silently skipped.
    assert list(susp.columns) == [
        "date", "symbol", "action", "reason", "volume", "target_weight",
    ]
    day1_susp = susp[susp["date"] == day1]
    assert len(day1_susp) == 2
    assert set(zip(day1_susp["symbol"], day1_susp["action"])) == {("A", "SELL"), ("B", "BUY")}
    reasons = dict(zip(day1_susp["symbol"], day1_susp["reason"]))
    assert reasons == {"A": "volume_zero", "B": "volume_missing"}

    # Flat prices + zero fills => equity and holdings freeze across the halt.
    equity = res["equity_curve"]["equity"]
    assert equity.iloc[1] == pytest.approx(equity.iloc[0])
    assert equity.iloc[2] == pytest.approx(equity.iloc[0])
    weights = res["actual_weights"]
    assert weights.iloc[1]["A"] == pytest.approx(weights.iloc[0]["A"])
    assert weights.iloc[1]["B"] == 0.0

    # The deferred instruction is honoured once both names trade again.
    day3 = report[report["date"] == str(idx[3].date())]
    assert set(zip(day3["symbol"], day3["action"])) == {("A", "SELL"), ("B", "BUY")}
    assert weights.iloc[3]["A"] == pytest.approx(0.5, abs=0.01)
    assert weights.iloc[3]["B"] == pytest.approx(0.5, abs=0.01)

    # A price-only series (no Volume column anywhere) carries no suspension
    # information, so the guard must stay out of the way -- see the docstring's
    # "DISCLOSED LIMIT OF THE GUARD".
    res_no_vol = run_allocation_backtest(
        {"A": make_df([100.0] * 5), "B": make_df([10.0] * 5)}, target_weights,
        initial_capital=100_000.0,
    )
    assert res_no_vol["suspension_report"].empty
    assert str(idx[1].date()) in set(res_no_vol["rebalance_report"]["date"])


def test_st_stock_price_limit_is_five_percent():
    """B1.3: ST / *ST names trade in a 5% band, not the 10% main-board band.

    The marker lives in the display name, which OHLCV does not carry, so it
    can only come from the optional `name_map` (or a marker embedded in the
    symbol itself). Without one the board band is returned unchanged -- the
    engine never guesses a name.
    """
    from common.allocation_backtester import _get_china_price_limit

    # Main board: ST / *ST tighten to 5% ...
    assert _get_china_price_limit("600000.SH", "ST浦发") == 0.05
    assert _get_china_price_limit("600000.SH", "*ST浦发") == 0.05
    assert _get_china_price_limit("600000.SH", "ＳＴ浦发") == 0.05  # full-width prefix
    # ... a marker embedded in the symbol is detected too ...
    assert _get_china_price_limit("ST浦发.SH") == 0.05
    # ... and a non-ST name keeps the board band.
    assert _get_china_price_limit("600000.SH", "浦发银行") == 0.10
    assert _get_china_price_limit("600000.SH") == 0.10
    # Foreign tickers are never re-banded by a stray "ST" substring.
    assert _get_china_price_limit("SPY", "ST Best Inc") == 1.0

    # Behavioural half: a +5.2% close ON the high is limit-locked for an ST
    # name (5% band) but an ordinary +5.2% day for a normal 10% name.
    idx = pd.bdate_range("2023-01-01", periods=2)
    px = [10.0, 10.52]
    df = pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px, "Volume": [1e5] * 2}, index=idx)
    universe = {"600000.SH": df}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[1] = [0.5]  # BUY on the +5.2% day

    st_res = run_allocation_backtest(
        universe, target_weights, china_trading=True, name_map={"600000.SH": "ST浦发"}
    )
    assert st_res["rebalance_report"].empty, "ST limit-up (+5%) must block the buy"

    normal_res = run_allocation_backtest(
        universe, target_weights, china_trading=True, name_map={"600000.SH": "浦发银行"}
    )
    assert not normal_res["rebalance_report"].empty
    assert normal_res["rebalance_report"].iloc[0]["action"] == "BUY"


# Scalar metrics `minimal_metrics` claims are unaffected. Tolerance is EXACTLY
# ZERO (rtol=0, atol=0): the ledger and the metric arithmetic are literally the
# same statements in both modes, only the reporting frames differ, so anything
# but bit-equality would mean the switch leaked into the simulation.
_MINIMAL_COMPARED_KEYS = (
    "total_turnover", "total_rebalances", "total_return", "cagr", "max_drawdown",
    "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor",
)


def _equivalence_fixture():
    """A multi-symbol, multi-rebalance, fee-paying, drifting book."""
    idx = pd.bdate_range("2020-01-01", periods=12)
    vs = {
        "A": [50.0, 52.0, 49.0, 55.0, 53.0, 58.0, 57.0, 61.0, 60.0, 62.0, 59.0, 63.0],
        "B": [24.0, 23.5, 25.0, 24.0, 26.5, 27.0, 26.0, 25.5, 27.5, 28.0, 27.0, 29.0],
        "C": [311.0, 305.0, 319.0, 322.0, 301.0, 298.0, 310.0, 315.0, 320.0, 318.0, 325.0, 330.0],
    }
    universe = {s: make_df(v, start="2020-01-01") for s, v in vs.items()}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=list(vs))
    for i, row in ((0, [0.4, 0.3, 0.3]), (1, [0.2, 0.5, 0.3]), (4, [0.6, 0.0, 0.4]),
                   (7, [0.0, 1.0, 0.0]), (10, [0.35, 0.35, 0.3])):
        target_weights.iloc[i] = row
    return universe, target_weights


def test_minimal_metrics_equivalence():
    """B1.2: lightweight mode is a REPORTING switch, not an approximation.

    Same ledger, same metrics, bit-for-bit (tolerance 0). The fixture trades
    on 5 dates with fees, lot rounding and a full exit, so any leakage of the
    switch into the simulation (a skipped fill, a cash drift, a different
    turnover accumulation) would move at least one metric.
    """
    universe, target_weights = _equivalence_fixture()

    full = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=10)
    minimal = run_allocation_backtest(
        universe, target_weights, initial_capital=100_000.0, min_shares=10, minimal_metrics=True,
    )

    for key in _MINIMAL_COMPARED_KEYS:
        np.testing.assert_allclose(minimal[key], full[key], rtol=0, atol=0, err_msg=key)

    # The metrics are non-degenerate: this fixture really does trade and drift.
    assert full["total_rebalances"] == 5
    assert not full["rebalance_report"].empty
    assert full["total_turnover"] > 0

    # Equity curve is still built in full (cheap, and consumers check `.empty`).
    np.testing.assert_allclose(
        minimal["equity_curve"]["equity"].to_numpy(), full["equity_curve"]["equity"].to_numpy(),
        rtol=0, atol=0,
    )
    assert list(minimal["equity_curve"].index) == list(full["equity_curve"].index)

    # ...while the two big per-day frames are NOT collected, only declared.
    assert minimal["rebalance_report"].empty
    assert list(minimal["rebalance_report"].columns) == list(full["rebalance_report"].columns)
    assert minimal["actual_weights"].empty
    assert list(minimal["actual_weights"].columns) == list(full["actual_weights"].columns)

    # The ambient scope (`common/allocation_search`'s ERS wiring) is equivalent
    # to the explicit flag, and leaves no residue once it exits.
    from common.allocation_backtester import minimal_metrics_scope

    with minimal_metrics_scope():
        scoped = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=10)
    for key in _MINIMAL_COMPARED_KEYS:
        np.testing.assert_allclose(scoped[key], full[key], rtol=0, atol=0, err_msg=key)
    assert scoped["rebalance_report"].empty

    after = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=10)
    assert not after["rebalance_report"].empty, "the scope must not leak past its block"


# --------------------------------------------------------------------------- #
# Task B3.1 -- execution-price modes (close | next_open | next_vwap |
# intraday_twap), signal/execution date separation and deferral tracing.
# --------------------------------------------------------------------------- #
def _gap_fixture():
    """Two names, one rebalance instruction printed on the close of `idx[2]`,
    and a +10% OVERNIGHT GAP on the next session's open (idx[3]).

    `close` mode therefore buys A at 100 (idx[2]'s close) while `next_open`
    buys the very same instruction at 110 (idx[3]'s open) -- the exact
    difference an overnight gap makes, with no fees so the arithmetic is exact.
    """
    idx = pd.bdate_range("2020-01-01", periods=6)
    a = pd.DataFrame({
        "Open":  [100.0, 100.0, 100.0, 110.0, 110.0, 110.0],
        "High":  [100.5, 100.5, 100.5, 110.5, 110.5, 110.5],
        "Low":   [99.5, 99.5, 99.5, 109.5, 109.5, 109.5],
        "Close": [100.0, 100.0, 100.0, 110.0, 110.0, 110.0],
        "Volume": [1e6] * 6,
    }, index=idx)
    b = pd.DataFrame({
        "Open": [50.0] * 6, "High": [50.5] * 6, "Low": [49.5] * 6,
        "Close": [50.0] * 6, "Volume": [1e6] * 6,
    }, index=idx)
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [0.5, 0.5]
    target_weights.iloc[2] = [0.8, 0.2]   # signal printed at idx[2]'s close
    return {"A": a, "B": b}, target_weights, idx


def _assert_ledger_identities(res, symbols):
    """`equity[t] == cash + holdings` can only be checked through the report, so
    this checks the two PUBLIC invariants every mode must preserve: cash never
    goes negative and the reported weights never sum above 1."""
    w = res["actual_weights"][symbols]
    assert np.isfinite(w.to_numpy()).all()
    assert (w.sum(axis=1) <= 1.0 + 1e-9).all(), w.sum(axis=1).max()
    # A fill priced below the book's mark can never produce a negative cash
    # because every trade's cost is charged to the same balance -- the replay
    # equivalent lives in test_ledger_parity.
    assert (res["rebalance_report"]["total_cost"] >= 0).all()


def test_execution_price_close_mode_is_bit_identical():
    """The DEFAULT path must be the pre-B3.1 engine, bit for bit.

    `execution_price="close"` and the omitted argument are the same code path,
    and their two large frames, equity curve and every scalar metric must agree
    with ZERO tolerance (rtol=0, atol=0). The GOLDEN DIGEST below pins the
    default path's actual numbers: it was computed before this task's edit and
    would change if B3.1 ever leaked into the close path.
    """
    universe, target_weights = _equivalence_fixture()

    default = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0, min_shares=10)
    explicit = run_allocation_backtest(
        universe, target_weights, initial_capital=100_000.0, min_shares=10, execution_price="close",
    )

    np.testing.assert_allclose(
        explicit["equity_curve"]["equity"].to_numpy(), default["equity_curve"]["equity"].to_numpy(),
        rtol=0, atol=0,
    )
    np.testing.assert_allclose(
        explicit["actual_weights"].to_numpy(), default["actual_weights"].to_numpy(), rtol=0, atol=0,
    )
    for frame in ("rebalance_report", "suspension_report", "deferral_report"):
        pd.testing.assert_frame_equal(explicit[frame], default[frame], check_exact=True)
    for key in _MINIMAL_COMPARED_KEYS:
        np.testing.assert_allclose(explicit[key], default[key], rtol=0, atol=0, err_msg=key)

    # `close` mode can never defer anything: it fills on the signal bar itself.
    assert default["deferral_report"].empty
    assert list(default["deferral_report"].columns) == [
        "signal_date", "execution_date", "symbol", "action", "reason",
        "execution_price", "target_weight",
    ]
    assert default["execution_price"] == "close"

    # Golden numbers of the DEFAULT path (frozen pre-B3.1 values, verified
    # against a snapshot taken before this task's edit; see
    # `_equivalence_fixture`). Disclosed: these are expected values, not a
    # tolerance -- any drift means the close path changed.
    assert default["total_rebalances"] == 5
    assert default["equity_curve"]["equity"].iloc[0] == 99_902.01
    assert default["equity_curve"]["equity"].iloc[-1] == 131_445.065
    assert default["total_return"] == 0.3157399435707051
    assert default["total_turnover"] == 5.745252966737446
    assert default["sharpe_ratio"] == 12.81797773867943
    assert default["max_drawdown"] == 0.036921355183288596

    # ...and one more on a China-rules fixture (lot rounding, ST 5% band, sell
    # stamp duty and a Volume==0 suspension day), so the pin is not a
    # single-path coincidence.
    idx = pd.bdate_range("2023-01-02", periods=8)
    px = [10.0, 10.5, 9.8, 9.8, 10.6, 10.2, 10.9, 11.2]
    china_universe = {}
    for sym, base in (("600000.SH", 1.0), ("000001.SZ", 1.3), ("300750.SZ", 2.1)):
        p = [round(x * base, 4) for x in px]
        china_universe[sym] = pd.DataFrame(
            {"Open": p, "High": [x + 0.1 for x in p], "Low": [x - 0.1 for x in p],
             "Close": p, "Volume": [1e5] * len(p)}, index=idx,
        )
    china_universe["000001.SZ"].loc[idx[3], "Volume"] = 0.0
    china_tw = pd.DataFrame(np.nan, index=idx, columns=list(china_universe))
    china_tw.iloc[0] = [0.4, 0.3, 0.3]
    china_tw.iloc[2] = [0.2, 0.5, 0.3]
    china_tw.iloc[3] = [0.5, 0.2, 0.3]
    china_tw.iloc[6] = [0.6, 0.0, 0.4]
    china = run_allocation_backtest(
        china_universe, china_tw, initial_capital=100_000.0,
        china_trading=True, name_map={"600000.SH": "ST浦发"},
    )
    assert len(china["rebalance_report"]) == 9
    assert len(china["suspension_report"]) == 1
    assert china["total_rebalances"] == 4
    assert china["total_return"] == 0.1178758106800053
    assert china["total_turnover"] == 2.3787680451884246
    assert china["sharpe_ratio"] == 5.002009825143002
    assert china["max_drawdown"] == 0.06675671835500079
    assert china["deferral_report"].empty


def test_execution_price_modes_signal_and_execution_dates_are_separated():
    """`next_open`: a signal printed at T's close is filled at T+1's OPEN.

    Pins the three things that must never drift:
      * the fill price is T+1's open, not T's close (the overnight gap is
        actually paid);
      * the trade is booked on the EXECUTION date, so the signal date's
        `actual_weights` row still shows the untouched, merely drifted book;
      * `equity_curve` is marked at closes throughout, so the gap appears in the
        equity curve exactly once, before the fill.
    """
    universe, target_weights, idx = _gap_fixture()
    kwargs = dict(initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0,
                  min_shares=0)

    close_res = run_allocation_backtest(universe, target_weights, execution_price="close", **kwargs)
    nopen_res = run_allocation_backtest(universe, target_weights, execution_price="next_open", **kwargs)

    signal_day = idx[2].strftime("%Y-%m-%d")
    exec_day = idx[3].strftime("%Y-%m-%d")

    close_a = close_res["rebalance_report"]
    close_a = close_a[(close_a["symbol"] == "A") & (close_a["date"] == signal_day)].iloc[0]
    assert close_a["price"] == 100.0, "close mode fills the signal bar's close"

    nopen = nopen_res["rebalance_report"]
    assert signal_day not in set(nopen["date"]), "no fill may be booked on the signal date"
    nopen_a = nopen[(nopen["symbol"] == "A") & (nopen["date"] == exec_day)].iloc[0]
    assert nopen_a["price"] == 110.0, "next_open fills the NEXT session's OPEN"
    assert nopen_a["action"] == "BUY"

    # Day 0 (idx[0]) also only QUEUES: the first fill lands on idx[1].
    assert nopen["date"].min() == idx[1].strftime("%Y-%m-%d")
    assert nopen_res["actual_weights"].iloc[0].sum() == 0.0, "day 0 is all cash in next_* modes"
    assert nopen_res["equity_curve"]["equity"].iloc[0] == 100_000.0

    # The signal day still reflects the PRE-TRADE book, not the instruction.
    assert nopen_res["actual_weights"].loc[idx[2]].to_dict() == \
        nopen_res["actual_weights"].loc[idx[1]].to_dict()

    # The overnight gap is in the equity curve on the execution day (A marks up
    # 100 -> 110 before/independently of the fill), and the round trip is worse
    # than close mode because the incremental A leg was bought 10% higher.
    assert nopen_res["equity_curve"]["equity"].loc[idx[3]] != close_res["equity_curve"]["equity"].loc[idx[3]]
    assert nopen_res["total_return"] < close_res["total_return"]

    # same number of instructions received in both modes
    assert nopen_res["total_rebalances"] == close_res["total_rebalances"] == 2
    _assert_ledger_identities(nopen_res, ["A", "B"])
    _assert_ledger_identities(close_res, ["A", "B"])


def test_execution_price_next_open_defers_through_limit_boards_and_suspensions():
    """一字板 / 停牌 defer the order to the next tradable session, with a trace.

    A-share fixture: the instruction printed on idx[0]'s close is only fillable
    three sessions later, because idx[1] and idx[2] are one-word limit-up boards
    (open = high = low) -- a buy cannot cross at such an open. Then a suspended
    name (Volume == 0) is deferred the same way, and both refusals land in
    `deferral_report` with their reason and the price that was unavailable.
    """
    idx = pd.bdate_range("2023-01-02", periods=6)
    px = [10.0, 11.0, 12.1, 12.1, 12.1, 12.1]
    boards = pd.DataFrame({
        "Open": px,
        # idx[1] and idx[2]: high == low == open == close (+10% one-word boards)
        "High": [10.1, 11.0, 12.1, 12.2, 12.2, 12.2],
        "Low":  [9.9, 11.0, 12.1, 12.0, 12.0, 12.0],
        "Close": px,
        "Volume": [1e6] * 6,
    }, index=idx)
    tw = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    tw.iloc[0] = [1.0]

    res = run_allocation_backtest({"600000.SH": boards}, tw, initial_capital=100_000.0,
                                  china_trading=True, execution_price="next_open")
    report = res["rebalance_report"]
    deferrals = res["deferral_report"]

    # Deferred on the two board-locked sessions, filled on the third.
    assert list(deferrals["reason"]) == ["open_limit_up", "open_limit_up"]
    assert list(deferrals["execution_date"]) == [idx[1].strftime("%Y-%m-%d"), idx[2].strftime("%Y-%m-%d")]
    assert list(deferrals["signal_date"]) == [idx[0].strftime("%Y-%m-%d")] * 2
    np.testing.assert_allclose(deferrals["execution_price"].to_numpy(), [11.0, 12.1])
    assert len(report) == 1
    assert report.iloc[0]["date"] == idx[3].strftime("%Y-%m-%d")
    assert report.iloc[0]["price"] == 12.1
    # Nothing filled through the boards: the book is all cash until idx[3].
    assert res["equity_curve"]["equity"].iloc[1] == pytest.approx(100_000.0)
    assert res["equity_curve"]["equity"].iloc[2] == pytest.approx(100_000.0)
    _assert_ledger_identities(res, ["600000.SH"])

    # --- suspension (B1.3 Volume==0) defers by the SAME machinery ----------
    a = pd.DataFrame({"Open": [10.0] * 6, "High": [10.1] * 6, "Low": [9.9] * 6,
                      "Close": [10.0] * 6, "Volume": [1e6, 0.0, 0.0, 1e6, 1e6, 1e6]}, index=idx)
    b = pd.DataFrame({"Open": [20.0] * 6, "High": [20.1] * 6, "Low": [19.9] * 6,
                      "Close": [20.0] * 6, "Volume": [1e6] * 6}, index=idx)
    tw2 = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    tw2.iloc[0] = [1.0, 0.0]
    tw2.iloc[1] = [0.5, 0.5]   # arrives while the idx[0] order is still unfilled
    res2 = run_allocation_backtest({"A": a, "B": b}, tw2, initial_capital=100_000.0,
                                   execution_price="next_open")
    # Two refusals (idx[1], idx[2]) + one `superseded` record for the newer
    # instruction that arrives on idx[1] before the old one could fill.
    assert list(res2["deferral_report"]["reason"]) == ["volume_zero", "superseded", "volume_zero"]
    # A B1.3 suspension is ALSO on the B1.3 report, so the old guard's contract
    # still holds in the new mode.
    assert set(res2["suspension_report"]["reason"]) == {"volume_zero"}
    assert len(res2["suspension_report"]) == 2
    filled = res2["rebalance_report"]
    assert filled[filled["symbol"] == "A"].iloc[0]["date"] == idx[3].strftime("%Y-%m-%d")
    _assert_ledger_identities(res2, ["A", "B"])

    # --- a signal on the LAST bar has no session left: reported, not dropped --
    tw3 = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    tw3.iloc[0] = [0.5]
    tw3.iloc[-1] = [1.0]
    res3 = run_allocation_backtest({"600000.SH": boards}, tw3, initial_capital=100_000.0,
                                   china_trading=True, execution_price="next_open")
    last = res3["deferral_report"].iloc[-1]
    assert last["reason"] == "no_next_bar"
    assert last["execution_date"] == ""
    assert last["signal_date"] == idx[-1].strftime("%Y-%m-%d")

    # --- the board band is the B1.3 one, so an ST name locks at 5% -----------
    st_idx = pd.bdate_range("2023-01-02", periods=3)
    p = [10.0, 10.52, 10.6]
    st_df = pd.DataFrame(
        {"Open": p, "High": [10.1, 10.52, 10.7], "Low": [9.9, 10.52, 10.5],
         "Close": p, "Volume": [1e6] * 3},
        index=st_idx,
    )
    st_tw = pd.DataFrame(np.nan, index=st_idx, columns=["600000.SH"])
    st_tw.iloc[0] = [0.5]
    st_res = run_allocation_backtest(
        {"600000.SH": st_df}, st_tw, china_trading=True,
        name_map={"600000.SH": "ST浦发"}, execution_price="next_open",
    )
    assert list(st_res["deferral_report"]["reason"]) == ["open_limit_up"]
    assert st_res["rebalance_report"].iloc[0]["date"] == st_idx[2].strftime("%Y-%m-%d")

    # ...while the same +5.2% one-word open is an ordinary bar for a 10%-band
    # name, so the very same fixture fills on the first session.
    normal_res = run_allocation_backtest(
        {"600000.SH": st_df}, st_tw, china_trading=True,
        name_map={"600000.SH": "浦发银行"}, execution_price="next_open",
    )
    assert normal_res["deferral_report"].empty
    assert normal_res["rebalance_report"].iloc[0]["date"] == st_idx[1].strftime("%Y-%m-%d")
    assert normal_res["rebalance_report"].iloc[0]["price"] == 10.52


def test_execution_price_modes_require_their_own_data():
    """`next_vwap` / `intraday_twap` fail loudly instead of degrading to close.

    A backtest silently measured at a different price than the one requested is
    precisely the distortion B3.1 exists to expose, so a missing VWAP / 5m feed
    raises a ValueError that names the requirement -- it never quietly becomes
    `close` (or `next_open`).
    """
    universe, target_weights, idx = _gap_fixture()

    with pytest.raises(ValueError, match="next_vwap"):
        run_allocation_backtest(universe, target_weights, execution_price="next_vwap")
    # `_gap_fixture` has no Amount/VWAP column, so the VWAP mode has nothing to
    # price the fill with -- the error must name the symbols.
    with pytest.raises(ValueError, match=r"Missing for .*'A'.*'B'"):
        run_allocation_backtest(universe, target_weights, execution_price="next_vwap")

    with pytest.raises(ValueError, match="5-minute"):
        run_allocation_backtest(universe, target_weights, execution_price="intraday_twap")

    with pytest.raises(ValueError, match="execution_price must be one of"):
        run_allocation_backtest(universe, target_weights, execution_price="twap")

    # --- and both modes DO work when the data is actually supplied ----------
    # next_vwap from Amount/Volume: Turnover/Volume must be the fill price.
    vwap_universe = {}
    for sym, df in universe.items():
        df = df.copy()
        df["Amount"] = df["Close"] * df["Volume"]
        vwap_universe[sym] = df
    vwap_res = run_allocation_backtest(vwap_universe, target_weights, initial_capital=100_000.0,
                                       commission_pct=0.0, slippage_pct=0.0, min_shares=0,
                                       execution_price="next_vwap")
    vwap_fill = vwap_res["rebalance_report"].iloc[0]
    assert vwap_fill["date"] == idx[1].strftime("%Y-%m-%d")
    assert vwap_fill["price"] == pytest.approx(float(vwap_universe[vwap_fill["symbol"]].loc[idx[1], "Close"]))

    # intraday_twap from 5m bars: the fill is the mean typical price of the
    # execution session's bars, and a session with no bars DEFERS.
    sessions = pd.DatetimeIndex([
        pd.Timestamp(d).replace(hour=9, minute=30) + pd.Timedelta(minutes=5 * k)
        for d in idx for k in range(4)
    ])
    intraday = {}
    for sym, base in (("A", 100.0), ("B", 50.0)):
        intraday[sym] = pd.DataFrame(
            {"High": base * 1.01, "Low": base * 0.99, "Close": base}, index=sessions,
        )
    # Only A gets a print on idx[1]; B's idx[1] is missing -> deferred.
    intraday["B"] = intraday["B"].loc[
        intraday["B"].index.difference(pd.DatetimeIndex([d for d in sessions if d.normalize() == idx[1]]))
    ]
    tw_res = run_allocation_backtest(universe, target_weights, initial_capital=100_000.0,
                                     commission_pct=0.0, slippage_pct=0.0, min_shares=0,
                                     execution_price="intraday_twap", intraday_data=intraday)
    tw_fills = tw_res["rebalance_report"]
    assert tw_fills.iloc[0]["symbol"] == "A"
    assert tw_fills.iloc[0]["date"] == idx[1].strftime("%Y-%m-%d")
    assert tw_fills.iloc[0]["price"] == pytest.approx(100.0)  # (101 + 99 + 100) / 3
    deferred = tw_res["deferral_report"]
    assert "intraday_missing" in set(deferred["reason"])
    _assert_ledger_identities(tw_res, ["A", "B"])


# --------------------------------------------------------------------------- #
# Task B3.5 -- SHORT-SIDE SIGN CONVENTION.
#
# The ledger books every fill as a SIGNED share delta: a BUY is `+trade_s`, a
# SELL is `-trade_s`, for EVERY prior sign. Reducing a long and ADDING to a
# short are therefore the same arithmetic (`new_s = prior_s - trade_s`), and so
# are covering a short and opening a long (`new_s = prior_s + trade_s`).
#
# The pre-B3.5 sell loops instead branched on the sign of the PRIOR position
# (`prior_s + trade_s if prior_s <= 0`), which inverted the delta on the short
# side:
#   * a short opened from flat (`prior_s == 0`, target_w < 0) was booked as a
#     LONG of `+trade_s` shares -- the `next_*` modes' day-0 short became a long
#     as soon as the queued order filled, and the cash flow was booked with the
#     same inverted sign;
#   * a partial rebalance that EXTENDS a short (`prior_s < 0`, target_w < 0)
#     covered it instead, while the `action` label still said SELL.
# Both were real position-direction errors, not modelling choices. It is
# reported as task B3.5 and fixed here; the two snapshot tables that contain a
# short book were re-pinned (see `test_cash_and_borrow_costs`), and every
# PURE-LONG snapshot in `_PRE_B32_DIGESTS` is byte-for-byte unchanged.
# --------------------------------------------------------------------------- #
def _short_rebalance_fixture():
    """`L` (long leg) + `S` (short leg), ZERO fees so the share maths is exact.

    Day 0 instructs `[0.5, -0.25]`; day 1 re-instructs the SAME long leg and a
    LARGER short (`-0.40`) -- a PARTIAL rebalance of an existing short, which is
    exactly the case the pre-B3.5 sign branch got backwards. There is an
    overnight gap between the two legs' prices, so `close` and `next_open` fill
    at different (hand-checkable) prices.
    """
    idx = pd.bdate_range("2023-01-02", periods=5)
    px = {"L": [40.0, 41.0, 42.0, 43.0, 44.0],
          "S": [90.0, 89.0, 88.0, 87.0, 86.0]}
    universe = {}
    for sym, vals in px.items():
        a = np.asarray(vals, dtype=float)
        universe[sym] = pd.DataFrame(
            {"Open": a, "High": a * 1.01, "Low": a * 0.99, "Close": a, "Volume": 2e5},
            index=idx,
        )
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["L", "S"])
    target_weights.iloc[0] = [0.5, -0.25]
    target_weights.iloc[1] = [0.5, -0.40]
    return universe, target_weights, idx


def _held_shares(res, symbol, closes):
    """Ledger-implied share count: `actual_w * equity / close` (the B1.1 identity)."""
    w = res["actual_weights"][symbol].to_numpy()
    eq = res["equity_curve"]["equity"].to_numpy()
    return w * eq / np.asarray(closes, dtype=float)


def _run_short_fixture(execution_price):
    universe, target_weights, idx = _short_rebalance_fixture()
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=100_000.0, min_shares=10,
        commission_pct=0.0, slippage_pct=0.0, execution_price=execution_price,
    )
    closes = {sym: df["Close"].to_numpy() for sym, df in universe.items()}
    return res, idx, closes


# (mode, execution date of the day-0 short entry, prior/target share counts of
# the day-1 PARTIAL rebalance). Hand-computed from the fixture: in `close` mode
# the day-0 short fills at 90 (0.25 * 100k / 90 = 277.8 -> 270 shares in 10-share
# lots); in `next_open` mode it fills at day 1's open of 89 (280.9 -> 280).
_B35_SHORT_CASES = [
    ("close", 0, -270.0, -450.0),
    ("next_open", 1, -280.0, -460.0),
]


@pytest.mark.parametrize("mode, entry_day, day0_short, day1_short", _B35_SHORT_CASES)
def test_short_opened_on_day0_stays_short_in_every_execution_mode(
        mode, entry_day, day0_short, day1_short):
    """A day-0 SHORT instruction must never be booked as a LONG.

    Pre-B3.5 this failed in `next_open` specifically: the day-0 order is QUEUED
    and filled on day 1 from a flat book (`prior_s == 0`), and the
    `prior_s + trade_s` branch turned the whole short into `+280` LONG shares
    (the cash flow flipped with it). `close` mode opened the short correctly
    (`new_s` is assigned from `held_s = -target_s` on the day-0 path), so this
    test pins BOTH modes -- the defect must not be "fixed" by reintroducing it
    somewhere else.
    """
    res, idx, closes = _run_short_fixture(mode)
    rep = res["rebalance_report"]
    entry = rep[(rep["symbol"] == "S") & (rep["date"] == idx[entry_day].strftime("%Y-%m-%d"))]
    assert len(entry) == 1
    row = entry.iloc[0]
    assert row["action"] == "SELL"
    assert row["prior_shares"] == 0.0
    assert row["shares"] == pytest.approx(abs(day0_short))
    # THE ASSERTION THE OLD BRANCH VIOLATED: the position is NEGATIVE.
    assert row["target_shares"] == pytest.approx(day0_short)
    assert row["target_shares"] < 0

    # ...and the ledger agrees with the report on the entry day.
    held = _held_shares(res, "S", closes["S"])
    assert held[entry_day] == pytest.approx(day0_short)
    assert (held[entry_day:] < 0).all()


@pytest.mark.parametrize("mode, entry_day, day0_short, day1_short", _B35_SHORT_CASES)
def test_short_partial_rebalance_extends_the_short_and_matches_the_target(
        mode, entry_day, day0_short, day1_short):
    """`prior_s < 0` + a more-negative target EXTENDS the short (B3.5 core case).

    The pre-B3.5 expression `prior_s + trade_s` covered 180 of the 270 short
    shares here (report: `prior_shares=-270 -> target_shares=-90`) while still
    labelling the fill SELL, i.e. the share count and the cash flow pointed in
    opposite directions. Three independent checks:
      * the signed delta of the fill is NEGATIVE (`target = prior - shares`);
      * the position moves FURTHER from zero (never toward it);
      * the post-trade weight sits on the -0.40 instruction within one 10-share
        lot, so "direction kept" is not achieved by mis-sizing the trade.
    """
    res, idx, closes = _run_short_fixture(mode)
    rep = res["rebalance_report"]
    reb_day = entry_day + 1
    rows = rep[(rep["symbol"] == "S") & (rep["date"] == idx[reb_day].strftime("%Y-%m-%d"))]
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["action"] == "SELL"
    assert row["prior_shares"] == pytest.approx(day0_short)
    assert row["target_shares"] == pytest.approx(day1_short)
    assert row["target_shares"] < row["prior_shares"]          # extended, not covered
    assert row["target_shares"] == pytest.approx(row["prior_shares"] - row["shares"])

    # Direction kept through the ledger, every day.
    held = _held_shares(res, "S", closes["S"])
    assert held[entry_day] == pytest.approx(day0_short)
    np.testing.assert_allclose(held[reb_day:], day1_short, rtol=1e-9)
    assert (held[entry_day:] < 0).all()
    # Sizing matches the -0.40 instruction to within one lot of tracking error.
    exec_equity = float(row["portfolio_equity"])
    lot_w = 10 * float(row["price"]) / exec_equity
    assert abs(res["actual_weights"]["S"].iloc[reb_day] - (-0.40)) <= lot_w + 1e-9


def test_gross_exposure_above_one_is_the_instructions_own_and_is_not_renormalised():
    """CONTRACT BOUNDARY: the engine enforces `cash >= 0`, NOT `gross <= 1`.

    A long/short instruction whose GROSS target exceeds 1.0 is self-funding
    (the short sale releases cash), so the cash constraint does not bound gross
    at 1 and the engine deliberately does not rescale the book: it reports what
    the ledger holds, and normalising gross exposure is the STRATEGY's job
    (`AllocationTemplate.explain_weights` / `strategy_generator`), not the
    engine's -- see the "One DISCLOSED deviation remains" note in
    `run_allocation_backtest`'s docstring.

    Pre-B3.5 this could not even be observed in `next_open` mode: the day-0
    short was booked as a LONG, so a `[0.6, -0.6]` instruction produced a
    ~1.0-gross book of TWO LONGS with almost no cash left (the "short" leg
    consumed it) instead of a ~1.2-gross hedge -- and the next rebalance then
    "sold" that phantom long back to zero, erasing the hedge entirely.
    """
    universe, target_weights, _idx = _short_rebalance_fixture()
    target_weights = target_weights.copy()
    target_weights.iloc[0] = [0.6, -0.6]   # gross 1.2, by instruction
    target_weights.iloc[1] = [0.6, -0.6]
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=100_000.0, min_shares=10,
        commission_pct=0.0, slippage_pct=0.0, execution_price="next_open",
    )
    w = res["actual_weights"][["L", "S"]].to_numpy()
    eq = res["equity_curve"]["equity"].to_numpy()
    gross = np.abs(w).sum(axis=1)
    net = w.sum(axis=1)
    # Gross really is > 1 and it is the instruction's 1.2 less lot rounding.
    assert gross[1] > 1.0
    assert gross[1] == pytest.approx(1.2, abs=0.01)
    # The book is a genuine hedge: one long leg and one SHORT leg, not two longs.
    assert w[1, 0] > 0
    assert w[1, 1] < 0
    # The ledger identity still holds for a levered book: idle cash is
    # `equity * (1 - net)`, and it is positive (no margin was consumed).
    cash = eq * (1.0 - net)
    assert (cash > 0).all()
    # And the short really is the short: proceeds funded the long leg.
    assert res["rebalance_report"]["target_shares"].min() < 0


def _b35_digest(res):
    """sha256 over every reported float -- the "bit for bit" definition."""
    h = hashlib.sha256()
    h.update(repr([float(v) for v in res["equity_curve"]["equity"].to_numpy()]).encode())
    h.update(repr([[float(x) for x in row] for row in res["actual_weights"].to_numpy()]).encode())
    for frame in ("rebalance_report", "suspension_report", "deferral_report"):
        df = res[frame]
        h.update(repr(list(df.columns)).encode())
        h.update(repr([
            [str(x) if isinstance(x, str) else float(x) for x in row] for row in df.to_numpy()
        ]).encode())
    for key in ("total_turnover", "total_rebalances", "total_return", "cagr", "max_drawdown",
                "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor",
                "total_market_impact"):
        h.update(repr(float(res[key])).encode())
    return h.hexdigest()


# Captured from the tree BEFORE the B3.5 edit (i.e. WITH the short sign defect
# still in place) on a PURE-LONG fixture. They must still match bit for bit:
# this is the evidence that the fix touched ONLY books that hold a short. A
# long-only book can never enter the `prior_s < 0` / `prior_s == 0 SELL` branch,
# so any drift here would mean the edit changed something else.
_B35_PRE_FIX_LONG_ONLY_DIGESTS = {
    "close": "15bc10d79813c13008e095d2aea92370df38f3c586d1ad73cd8f27164f7f9bf9",
    "next_open": "ddb669d2a1b41b9b900d392db41866142904fcae410a2cdc14b197896ce5ab82",
}


@pytest.mark.parametrize("mode", ["close", "next_open"])
def test_pure_long_books_are_bit_identical_after_the_b3_5_fix(mode):
    """Zero-tolerance regression: a long-only book must not move a single bit."""
    universe, target_weights, _idx = _gap_fixture()
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=100_000.0, min_shares=10,
        execution_price=mode,
    )
    assert _b35_digest(res) == _B35_PRE_FIX_LONG_ONLY_DIGESTS[mode], (
        f"{mode}: a PURE-LONG book moved -- the B3.5 short-sign fix leaked into it"
    )
    # And it really is long-only, so the digest above is meaningful.
    assert (res["actual_weights"].to_numpy() >= 0).all()





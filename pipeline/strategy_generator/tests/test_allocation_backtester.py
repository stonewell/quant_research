import numpy as np
import pandas as pd
import pytest
from common.testing import make_ohlcv_from_closes as make_df

from common.allocation_backtester import run_allocation_backtest
from common.metrics import max_drawdown as metrics_max_drawdown


def test_allocation_backtester_drift():
    idx = pd.bdate_range("2020-01-01", periods=5)

    # Asset A doubles on day 2. Asset B stays flat.
    closes_a = [100, 200, 200, 200, 200]
    closes_b = [100, 100, 100, 100, 100]

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }

    # Target weights: 50/50 set once on day 0, never rebalanced again.
    # Sparse (NaN after day 0) is the real contract: a dense frame repeating
    # 0.5 every day would tell the backtester to rebalance EVERY day instead.
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [0.5, 0.5]

    # Zero costs to isolate drift logic
    res = run_allocation_backtest(universe, target_weights, commission_pct=0.0, slippage_pct=0.0)

    eq = res["equity_curve"]["equity"]
    actual_w = res["actual_weights"]

    # Day 0: 100k, 50/50
    assert eq.iloc[0] == 100_000
    assert actual_w.iloc[0]["A"] == 0.5

    # Day 1: Asset A doubles (+100%). Asset B is flat (0%).
    # Portfolio return = 0.5 * 100% + 0.5 * 0% = 50%
    # Equity should be 150k
    assert eq.iloc[1] == 150_000

    # Asset A's new weight = (0.5 * 2.0) / 1.5 = 2/3
    # Asset B's new weight = (0.5 * 1.0) / 1.5 = 1/3
    np.testing.assert_allclose(actual_w.iloc[1]["A"], 2.0 / 3.0)
    np.testing.assert_allclose(actual_w.iloc[1]["B"], 1.0 / 3.0)


def test_allocation_backtester_rebalance_costs():
    """Flat prices, so equity can only move by the costs actually paid.

    EXPECTATIONS REVISED for the B1.1 cash-funded entry fix (disclosed, not
    silently relaxed). The old numbers assumed a 1,000-share entry at $100
    financed at exactly 100% of capital with the 1% fee left as a NEGATIVE cash
    balance -- i.e. an unrequested 1,000 margin loan. The entry is now sized off
    `cash / (1 + fee)`, which changes the prefix of this fixture:

        day-0 equity   99,000 ->  99,010   (990 shares @ 100 = 99,000 notional,
                                            1% fee = 990, cash left = 10)
        day-1 equity   99,000 ->  99,010   (flat prices, nothing to pay)
        day-2 equity   97,020 ->  97,050   (sell 990 A: -990 fee -> cash 98,020;
                                            buy 970 B: -970 fee -> cash 50;
                                            equity = 50 + 97,000)
        turnover          3.0 ->   2.9696 (same formula, smaller fills:
                                            99,000/100,000 + 196,000/99,010)

    The invariant being tested is unchanged and is now asserted structurally as
    well: equity only ever drops by the `total_cost` the report booked.
    """
    idx = pd.bdate_range("2020-01-01", periods=3)

    # Flat prices so equity only changes due to costs
    closes = [100, 100, 100]
    universe = {
        "A": make_df(closes, start="2020-01-01"),
        "B": make_df(closes, start="2020-01-01"),
    }

    # Day 0: instruct 100% A. Day 1: no instruction (sparse NaN) -> just holds.
    # Day 2: instruct 100% B (sells A, then buys B with the released cash).
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.loc[idx[0]] = [1.0, 0.0]
    target_weights.loc[idx[2]] = [0.0, 1.0]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000, commission_pct=0.01, slippage_pct=0.0)

    eq = res["equity_curve"]["equity"]
    report = res["rebalance_report"]

    day0 = report[report["rebalance_id"] == 1]
    assert day0["shares"].tolist() == [990.0]
    assert day0["total_cost"].tolist() == [990.0]
    # Day 0: only the entry cost leaves the book (capital - fee, not a loan).
    assert eq.iloc[0] == 100_000 - day0["total_cost"].sum() == 99_010

    # Day 1: no target change, no price change. Equity is unchanged.
    assert eq.iloc[1] == 99_010

    # Day 2: equity drops by exactly the costs that day's fills paid.
    day2 = report[report["rebalance_id"] == 2]
    assert sorted(day2["action"].tolist()) == ["BUY", "SELL"]
    assert day2["shares"].tolist() == [990.0, 970.0]
    assert eq.iloc[2] == pytest.approx(eq.iloc[1] - day2["total_cost"].sum())
    assert eq.iloc[2] == 97_050

    assert res["total_rebalances"] == 2  # Initial + Day 2
    # Turnover = traded notional / reference equity (definition unchanged; the
    # value moved only because the fills are now affordable, not 100% + fee).
    assert res["total_turnover"] == pytest.approx(99_000 / 100_000 + 196_000 / 99_010)


def test_backtester_rebalances_even_when_target_value_is_unchanged():
    """A template recomputing the SAME target must still trigger a rebalance.

    Intent (unchanged since the original regression test): equal-weight emits
    an identical 0.5/0.5 instruction on every rebalance date, and the engine
    must not confuse "recomputed to an identical value" with "no rebalance was
    due" (the old bug: inferring rebalances by diffing consecutive targets).

    REWRITTEN for the B1.1 ledger fix: the old assertions demanded weights of
    EXACTLY [0.5, 0.5] on the rebalance dates. That was only true in the
    abolished force-overwrite model, where the engine wrote the day's TARGET
    back into `actual_weights` and hid whole-share lot rounding. Under the
    honest share ledger a 0.5 target is only reachable to within one share
    (~9.4e-4 of equity on this fixture), so the test now pins three things
    instead of one exact vector:
      1. all three instructions were received (`total_rebalances == 3`);
      2. the rebalance-day weights are within `atol=1e-3` of 0.5, i.e. the
         drift from days 1-4 was genuinely reset (loose enough for a
         single-share remainder, far tighter than the ~0.05 drift);
      3. the FILLS exist: `rebalance_report` carries a SELL and a BUY at each
         of idx[5] and idx[10], so "the rebalance happened" is evidenced by
         trades, not by idealised weights.
    """
    idx = pd.bdate_range("2020-01-01", periods=11)
    closes_a = [100 * (1.05 ** i) for i in range(11)]  # A drifts up strongly
    closes_b = [100.0] * 11                              # B stays flat
    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
    }
    # Sparse target: 50/50 on day 0, day 5, and day 10 -- three identical-
    # valued rebalances, NaN (drift freely) in between -- exactly what
    # EqualWeightAllocation.generate_weights emits.
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.loc[idx[[0, 5, 10]]] = 0.5

    res = run_allocation_backtest(universe, target_weights, commission_pct=0.0, slippage_pct=0.0)
    actual_w = res["actual_weights"]

    # (1) Every instruction was received, including the ones whose target is
    # identical to the previous one.
    assert res["total_rebalances"] == 3

    # (2) Weights are reset to 0.5 +/- one whole share on each rebalance date
    # (atol, NOT the old exact equality: whole-share lots cannot hit 0.5).
    for d in (idx[0], idx[5], idx[10]):
        np.testing.assert_allclose(actual_w.loc[d], [0.5, 0.5], atol=1e-3)
    # ...having genuinely drifted away from 50/50 in between (otherwise this
    # test would trivially pass even under the old, broken value-diff logic).
    assert actual_w.loc[idx[4]]["A"] > 0.5 + 1e-3
    assert actual_w.loc[idx[9]]["A"] > 0.5 + 1e-3

    # (3) There are real fills on the repeated-target dates: A must be sold
    # down (it drifted up) and B bought up, on BOTH idx[5] and idx[10].
    report = res["rebalance_report"]
    for d in (idx[5], idx[10]):
        fills = report[report["date"] == d.strftime("%Y-%m-%d")]
        assert not fills.empty, f"no fill booked on repeated-target date {d.date()}"
        assert (fills["action"] == "SELL").any()
        assert (fills["action"] == "BUY").any()
        sell_a = fills[(fills["symbol"] == "A") & (fills["action"] == "SELL")]
        buy_b = fills[(fills["symbol"] == "B") & (fills["action"] == "BUY")]
        assert sell_a["shares"].iloc[0] > 0
        assert buy_b["shares"].iloc[0] > 0


def test_max_drawdown_matches_common_metrics_sign_convention():
    # Regression test: run_allocation_backtest's own max_drawdown used to be
    # NEGATIVE ((eq - cummax) / cummax, .min()) while common/metrics.py's
    # max_drawdown() -- the shared convention used across this whole
    # workspace, and what backtester/run_backtest.py displays alongside it --
    # is POSITIVE. Pin both the sign and the exact value against a synthetic
    # equity path with a known, hand-computed drawdown.
    idx = pd.bdate_range("2020-01-01", periods=5)
    # Single-asset equal weight: +25% then -20% (back to start) then flat --
    # an exact 20% drawdown from the day-2 peak.
    closes_a = [100.0, 125.0, 100.0, 100.0, 100.0]
    universe = {"A": make_df(closes_a, start="2020-01-01")}

    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    target_weights.iloc[0] = [1.0]

    res = run_allocation_backtest(universe, target_weights, commission_pct=0.0, slippage_pct=0.0)

    assert res["max_drawdown"] > 0
    np.testing.assert_allclose(res["max_drawdown"], 0.20, atol=1e-9)
    np.testing.assert_allclose(res["max_drawdown"], metrics_max_drawdown(res["equity_curve"]["equity"]))

    # Calmar Ratio = CAGR / |Max Drawdown|; with a positive max_drawdown this
    # is a straight division, no abs() needed -- pin that the two agree in sign.
    if res["max_drawdown"] > 0:
        np.testing.assert_allclose(res["calmar_ratio"], res["cagr"] / res["max_drawdown"])

import numpy as np
import pandas as pd
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
    idx = pd.bdate_range("2020-01-01", periods=3)

    # Flat prices so equity only changes due to costs
    closes = [100, 100, 100]
    universe = {
        "A": make_df(closes, start="2020-01-01"),
        "B": make_df(closes, start="2020-01-01"),
    }

    # Day 0: instruct 100% A. Day 1: no instruction (sparse NaN) -> just holds.
    # Day 2: instruct 100% B (Full turnover of 2.0: sell 1.0 A, buy 1.0 B).
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.loc[idx[0]] = [1.0, 0.0]
    target_weights.loc[idx[2]] = [0.0, 1.0]

    res = run_allocation_backtest(universe, target_weights, initial_capital=100_000, commission_pct=0.01, slippage_pct=0.0)

    eq = res["equity_curve"]["equity"]

    # Day 0: Turnover = 1.0 (from 0 to 100% A). Cost = 1% of 100k = 1,000. Equity = 99k.
    assert eq.iloc[0] == 99_000

    # Day 1: No target change, no price change. Equity stays 99k.
    assert eq.iloc[1] == 99_000

    # Day 2: Turnover = 2.0 (sell A, buy B). Cost = 2.0 * 0.01 = 2%.
    # 2% of 99k = 1,980. Equity = 99,000 - 1,980 = 97,020.
    assert eq.iloc[2] == 97_020

    assert res["total_rebalances"] == 2 # Initial + Day 2
    assert res["total_turnover"] == 3.0 # 1.0 + 2.0


def test_backtester_rebalances_even_when_target_value_is_unchanged():
    # Regression test: a template like equal-weight recomputes the SAME
    # target (1/N) on every rebalance date. The backtester must still reset
    # drifted weights back to that target on each such date -- it must not
    # confuse "recomputed to an identical value" with "no rebalance was due"
    # (the bug: inferring rebalances by diffing consecutive target values).
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

    # Weights must be reset to exactly 50/50 on each rebalance date...
    np.testing.assert_allclose(actual_w.loc[idx[0]], [0.5, 0.5])
    np.testing.assert_allclose(actual_w.loc[idx[5]], [0.5, 0.5])
    np.testing.assert_allclose(actual_w.loc[idx[10]], [0.5, 0.5])
    # ...having genuinely drifted away from 50/50 in between (otherwise this
    # test would trivially pass even under the old, broken value-diff logic).
    assert actual_w.loc[idx[4]]["A"] > 0.5
    assert actual_w.loc[idx[9]]["A"] > 0.5
    assert res["total_rebalances"] == 3


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

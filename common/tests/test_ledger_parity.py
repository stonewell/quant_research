"""Ledger-parity tests for `common/allocation_backtester.run_allocation_backtest`.

Task B1.1: the backtester used to force-overwrite its rebalance-day weights with
the day's TARGET weights (`new_w[i] = tgt_w_arr[t, i]`), so round-lot idle cash
and cash-constrained partial fills disappeared from the reported book and the
equity curve compounded returns on positions the share ledger never held
("ghost leverage" / inflated returns).

The engine is now a genuine share ledger: `cash` + `held_shares` are the state,
and both outputs are derived from them every day::

    equity[t]      = cash[t] + sum_i(held_shares_i * close[t, i])
    actual_w[t]    = held_shares_i * close[t, i] / equity[t]

Every test here therefore REPLAYS the returned `rebalance_report` (signed share
deltas + per-trade costs) into an independent cash/share book and demands that
the reported equity curve and weights match that replay. Nothing here reads real
market data or touches the network -- all prices are synthetic.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import run_allocation_backtest
from common.testing import make_ohlcv_from_closes as make_df

# Replay/engine agreement tolerance. The engine accumulates the same quantities
# in loop order and the replay re-sums them, so agreement is exact up to
# float64 rounding: `rtol=1e-12` covers ~4 orders of magnitude of headroom over
# that rounding noise while still failing on any real accounting break
# (a single 1-share position is ~1e-4 of equity on these fixtures).
_PARITY_RTOL = 1e-12
_PARITY_ATOL = 1e-8

# Tolerance for the `cash >= 0` invariant. The ledger must never run a negative
# balance (that is margin nobody asked for); this budget only absorbs float64
# summation noise, which on these ~1e5 fixtures is a handful of ~1e-11 ulps. A
# genuine accounting break (e.g. an unfunded entry fee) is O(1e2), i.e. ten
# orders of magnitude outside it.
_CASH_TOL = 1e-9


def _replay_ledger(initial_capital, report, dates, closes, symbols):
    """Independently replay the trade report into a cash/share book.

    Returns `(equity, weights, cash)` arrays indexed like `dates`. Trades are
    applied on their own `date` (the engine trades at the close), then the book
    is marked at that day's forward-filled close -- the same marking rule the
    engine uses.
    """
    by_date = {}
    if report is not None and not report.empty:
        for tr in report.itertuples(index=False):
            by_date.setdefault(str(tr.date), []).append(tr)

    cash = float(initial_capital)
    shares = {sym: 0.0 for sym in symbols}
    equity, weights, cash_hist = [], [], []

    for d in dates:
        for tr in by_date.get(d.strftime("%Y-%m-%d"), []):
            signed = float(tr.shares) if tr.action == "BUY" else -float(tr.shares)
            # cash flow of the fill: pay for a buy, receive for a sell, and the
            # fees/slippage always leave the account.
            cash -= signed * float(tr.price) + float(tr.total_cost)
            shares[tr.symbol] = shares.get(tr.symbol, 0.0) + signed

        mv = [shares[sym] * float(closes.loc[d, sym]) for sym in symbols]
        day_equity = cash + float(sum(mv))
        equity.append(day_equity)
        cash_hist.append(cash)
        if day_equity > 0:
            weights.append([v / day_equity for v in mv])
        else:
            weights.append([0.0 for _ in mv])

    return np.array(equity), np.array(weights), np.array(cash_hist)


def _assert_ledger_parity(res, initial_capital, universe, symbols):
    """The reported equity curve and weights must equal the replayed ledger."""
    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()}).ffill().fillna(0.0)
    dates = res["equity_curve"].index
    closes = closes.loc[dates]

    eq, w, _cash = _replay_ledger(
        initial_capital, res["rebalance_report"], dates, closes, symbols
    )
    np.testing.assert_allclose(
        res["equity_curve"]["equity"].to_numpy(), eq, rtol=_PARITY_RTOL, atol=_PARITY_ATOL
    )
    np.testing.assert_allclose(
        res["actual_weights"][symbols].to_numpy(), w, rtol=_PARITY_RTOL, atol=_PARITY_ATOL
    )
    return eq, w


def test_round_lot_truncation_leaves_idle_cash_and_never_levers():
    """Requirement 1: round-lot truncation must not conjure cash.

    A $333 stock in a 100-share lot and a $37 stock in a 100-share lot cannot
    spend a 50% target exactly, so a large slice of capital stays in cash. The
    reported weights must be  holdings/equity, they must sum to <= 1, and the
    replayed cash balance must never be inflated (it is plainly positive here).
    """
    idx = pd.bdate_range("2020-01-01", periods=5)
    universe = {
        "A": make_df([333.0, 333.0, 333.0, 333.0, 400.0], start="2020-01-01"),
        "B": make_df([37.0, 37.0, 37.0, 37.0, 40.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [0.5, 0.5]
    target_weights.iloc[4] = [0.9, 0.1]

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=initial_capital, min_shares=100
    )

    # Day 0: 0.5*100,000/333 = 150.15 -> 1 lot of 100 shares = 33,300,
    #        0.5*100,000/37  = 1351.4 -> 13 lots of 100 = 1,300 shares = 48,100.
    day0 = res["rebalance_report"][res["rebalance_report"]["rebalance_id"] == 1]
    shares = dict(zip(day0["symbol"], day0["shares"]))
    assert shares == {"A": 100.0, "B": 1300.0}

    weights = res["actual_weights"]
    # Target was 100% invested; lot truncation legitimately leaves ~18.5% idle.
    assert weights.iloc[0].sum() < 0.85
    assert weights.iloc[0].sum() > 0.81

    _eq, _w, cash = _replay_ledger(
        initial_capital,
        res["rebalance_report"],
        res["equity_curve"].index,
        pd.DataFrame({s: df["Close"] for s, df in universe.items()}),
        ["A", "B"],
    )
    assert cash.min() > 0.0, "idle cash must stay positive, never be conjured away"
    assert (weights.sum(axis=1) <= 1.0 + 1e-9).all()

    _assert_ledger_parity(res, initial_capital, universe, ["A", "B"])


def test_cash_constrained_partial_fill_matches_share_ledger():
    """Requirement 2: a buy capped by available cash must be reported honestly.

    Day 0 goes 100% into A. Day 2 asks for 125% of equity in B, i.e. more than
    the cash released by selling A, so the fill is partial. The share count in
    the report, the reported weight and the replayed ledger must all agree --
    the weight must NOT be written back to the 1.25 instruction.

    Costs are zeroed here on purpose: the whole point is the cash CAP (with
    fees, the cap is `cash/(1+rate)` rather than `cash` -- see
    `test_full_notional_entry_pays_its_fee_from_cash_and_never_trims`, which
    pins that the fee is never financed on margin).
    """
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {
        "A": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
        "B": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [1.0, 0.0]
    target_weights.iloc[2] = [0.0, 1.25]  # deliberately > 100%: unfundable

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe,
        target_weights,
        initial_capital=initial_capital,
        commission_pct=0.0,
        slippage_pct=0.0,
    )

    report = res["rebalance_report"]
    buy_b = report[(report["symbol"] == "B") & (report["action"] == "BUY")].iloc[0]
    requested_shares = buy_b["target_weight"] * buy_b["portfolio_equity"] / buy_b["price"]
    assert buy_b["shares"] == 1000.0
    assert buy_b["shares"] < requested_shares  # the instruction was NOT honoured in full
    assert buy_b["target_shares"] == buy_b["prior_shares"] + buy_b["shares"]

    # The reported weight is the ledger's 1,000 shares of B, not the 1.25 target.
    assert res["actual_weights"].iloc[2]["B"] == pytest.approx(1.0)
    assert res["actual_weights"].iloc[2]["A"] == pytest.approx(0.0)
    assert res["actual_weights"].iloc[2].sum() <= 1.0 + 1e-9

    _eq, _w = _assert_ledger_parity(res, initial_capital, universe, ["A", "B"])


def test_daily_equity_equals_cash_plus_holdings():
    """Requirement 3: equity[t] = cash + holdings, with costs charged exactly.

    A drifting 3-symbol book rebalanced 4 times (with fees) is replayed from the
    trade report; the equity curve must match to float64 rounding (see
    `_PARITY_RTOL`/`_PARITY_ATOL`). This is the assertion that fails loudly if
    costs are charged on unspent cash, if a partially-filled order is billed as
    a full one, or if equity is ever recursed from weights instead of the book.
    """
    idx = pd.bdate_range("2020-01-01", periods=9)
    universe = {
        "A": make_df([50.0, 52.0, 49.0, 55.0, 53.0, 58.0, 57.0, 61.0, 60.0], start="2020-01-01"),
        "B": make_df([24.0, 23.5, 25.0, 24.0, 26.5, 27.0, 26.0, 25.5, 27.5], start="2020-01-01"),
        "C": make_df([311.0, 305.0, 319.0, 322.0, 301.0, 298.0, 310.0, 315.0, 320.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B", "C"])
    for i, row in ((0, [0.4, 0.3, 0.3]), (2, [0.2, 0.5, 0.3]), (5, [0.6, 0.0, 0.4]), (7, [0.0, 1.0, 0.0])):
        target_weights.iloc[i] = row

    initial_capital = 100_000.0
    res = run_allocation_backtest(universe, target_weights, initial_capital=initial_capital, min_shares=10)

    eq, _w = _assert_ledger_parity(res, initial_capital, universe, ["A", "B", "C"])

    # Sanity: the replay is not trivially equal to the initial capital, i.e. the
    # fixture really does trade / drift.
    assert eq[-1] != pytest.approx(initial_capital, abs=1.0)
    # Fees were actually charged: post-trade equity is strictly below the
    # pre-trade mark-to-market value on the rebalance days.
    assert (res["rebalance_report"]["total_cost"] > 0).all()


def test_non_rebalance_day_weights_converge_to_real_holdings():
    """Requirement 4 (regression): off-rebalance weights are the holdings, not a stale target.

    Day 0 buys 50/50 at $100 with no costs; A then doubles and there is never
    another rebalance instruction. The day-1 weights must be the drifted 2/3-1/3
    implied by the shares actually held, and must differ from the day-0 target
    row that the old code effectively carried forward.
    """
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {
        "A": make_df([100.0, 200.0, 200.0], start="2020-01-01"),
        "B": make_df([100.0, 100.0, 100.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [0.5, 0.5]

    res = run_allocation_backtest(
        universe, target_weights, commission_pct=0.0, slippage_pct=0.0
    )
    actual = res["actual_weights"]

    # 500 shares of A ($100,000) + 500 shares of B ($50,000) -> 2/3 / 1/3.
    np.testing.assert_allclose(actual.iloc[1][["A", "B"]], [2.0 / 3.0, 1.0 / 3.0])
    np.testing.assert_allclose(actual.iloc[2][["A", "B"]], [2.0 / 3.0, 1.0 / 3.0])
    # ...and emphatically NOT the carried-forward instruction.
    assert not np.allclose(actual.iloc[1][["A", "B"]], [0.5, 0.5], atol=1e-3)

    _assert_ledger_parity(res, 100_000.0, universe, ["A", "B"])


def test_full_notional_entry_pays_its_fee_from_cash_and_never_trims():
    """A 100%-notional entry must PAY its fee out of cash, not finance it.

    REWRITTEN from `test_entry_fee_is_financed_from_cash_and_trimmed_next_rebalance`
    (B1.1 follow-up). The first B1.1 cut spent the whole `cash` balance on
    notional and then billed commission/slippage to that same balance, leaving
    `cash = -fee`. That was a genuine accounting deviation (an unrequested
    margin loan), not a convention: it pushed `sum(actual_w)` to `1 + fee` and
    manufactured a spurious 1-share SELL at the next rebalance instruction,
    because the fee-financed excess had to be sold back out.

    The entry is now sized off `cash / (1 + fee_rate)`, so notional + fee is
    always payable and `cash >= 0` on every path. Disclosed expectation
    changes for this fixture (100,000 at $100, fee = 10 bps):
      * entry shares      1,000     ->   999   (999.0009 whole shares is all
                                               that 100,000/(100*1.001) buys)
      * cash after entry    -100     ->   0.1
      * equity[0]         99,900     ->  99,900.1  (cash 0.1 + 999 * 100;
                                               the old 99,900 was the levered
                                               book, 1,000 shares against a
                                               cash DEBT of 100)
      * weight[0]           1.001    ->   0.999999  (no longer > 1)
      * next rebalance   SELL 1 sh   ->   no fill at all (nothing to trim)
    The ledger-replay check at the end still pins the invariant itself.
    """
    idx = pd.bdate_range("2020-01-01", periods=3)
    universe = {"A": make_df([100.0, 100.0, 100.0], start="2020-01-01")}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    target_weights.iloc[0] = [1.0]
    target_weights.iloc[2] = [1.0]

    initial_capital = 100_000.0
    fee_rate = 0.0005 + 0.0005  # commission_pct + slippage_pct (defaults)
    res = run_allocation_backtest(universe, target_weights, initial_capital=initial_capital)

    report = res["rebalance_report"]
    entry = report.iloc[0]
    assert entry["shares"] == 999.0
    assert entry["total_cost"] == pytest.approx(entry["trade_value"] * fee_rate)
    # The fee is fully funded: the notional plus the fee never exceeds capital.
    assert entry["trade_value"] + entry["total_cost"] <= initial_capital

    # 999 shares of $100 against 99,900.1 of equity -- NOT overweight.
    assert res["actual_weights"].iloc[0]["A"] == pytest.approx(99_900.0 / 99_900.1)
    assert res["actual_weights"].iloc[0]["A"] <= 1.0 + 1e-9
    assert res["equity_curve"]["equity"].iloc[0] == pytest.approx(99_900.1)

    # The next rebalance instruction (day 2) has nothing to trim: the book is
    # already inside the cash envelope, so the only correct fill count is the
    # day-0 entry. The old "SELL 1 share" was pure fee-financing friction.
    assert res["total_rebalances"] == 2  # both instructions were still received
    assert len(report) == 1, "no trim / no ghost fill may follow a funded entry"
    assert not (report["action"] == "SELL").any()
    assert res["actual_weights"].iloc[2]["A"] == pytest.approx(99_900.0 / 99_900.1)

    # cash >= 0 on EVERY day (the invariant this test exists for). `_CASH_TOL`
    # is float64 summation slack, nothing more; a real -fee balance is ~1e2.
    _eq, _w, cash = _replay_ledger(
        initial_capital,
        report,
        res["equity_curve"].index,
        pd.DataFrame({s: df["Close"] for s, df in universe.items()}),
        ["A"],
    )
    assert cash.min() >= -_CASH_TOL, f"cash went negative: {cash.min()}"

    _assert_ledger_parity(res, initial_capital, universe, ["A"])


def test_single_day_missing_price_marks_at_last_close_without_ghost_trades():
    """A one-day price hole must not move equity, drop the symbol, or fake a fill.

    `mark_arr = closes.ffill().fillna(0)` (see `run_allocation_backtest`) means
    a symbol that does not quote on a given day is marked at its LAST KNOWN
    close. Only the fully-NaN-symbol case was covered before, so this test
    pins the single-day hole explicitly:

      * equity does not jump on the missing day (same value as the prior day);
      * the position is still held (no silent liquidation / de-listing);
      * a rebalance instruction that lands ON the missing day produces NO
        trade record for that symbol -- the engine refuses to invent a price
        ("ghost fill") -- and the instruction is only honoured at the next
        date that has a real price.
    """
    idx = pd.bdate_range("2020-01-01", periods=6)
    # Day 2 has no close at all; the last real close is 102.
    universe = {"A": make_df([100.0, 102.0, np.nan, 104.0, 104.0, 104.0], start="2020-01-01")}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A"])
    target_weights.iloc[0] = [1.0]
    target_weights.iloc[2] = [0.5]  # instructed on the day the price is missing
    target_weights.iloc[4] = [0.5]  # ...and re-instructed once a price exists

    initial_capital = 100_000.0
    fee_rate = 0.0005 + 0.0005
    res = run_allocation_backtest(universe, target_weights, initial_capital=initial_capital)

    equity = res["equity_curve"]["equity"]
    weights = res["actual_weights"]
    report = res["rebalance_report"]

    # Day 0 entry: 100,000/(100*1.001) -> 999 whole shares, cash 0.1.
    assert report.iloc[0]["shares"] == 999.0
    day0_equity = 0.1 + 999.0 * 100.0
    assert equity.iloc[0] == pytest.approx(day0_equity)

    # Day 1 marks at 102 (real close), day 2 must reuse that same 102.
    assert equity.iloc[1] == pytest.approx(0.1 + 999.0 * 102.0)
    assert equity.iloc[2] == pytest.approx(equity.iloc[1]), "no price -> no equity jump"
    assert weights.iloc[2]["A"] == pytest.approx(weights.iloc[1]["A"])
    assert weights.iloc[2]["A"] > 0.99, "the symbol must stay held, not be de-listed"

    # No fill may be booked on the missing-price date, even though the target
    # asked for a 50% position there.
    assert str(idx[2].date()) not in set(report["date"])
    assert (report["date"] != str(idx[2].date())).all()

    # The deferred instruction is honoured at the next rebalance with a price.
    day4 = report[report["date"] == str(idx[4].date())]
    assert not day4.empty
    assert (day4["action"] == "SELL").all()
    assert weights.iloc[4]["A"] == pytest.approx(0.5, abs=0.01)

    assert (weights.sum(axis=1) <= 1.0 + 1e-9).all()
    _eq, _w, cash = _replay_ledger(
        initial_capital,
        report,
        res["equity_curve"].index,
        pd.DataFrame({"A": universe["A"]["Close"]}),
        ["A"],
    )
    assert cash.min() >= -_CASH_TOL
    _assert_ledger_parity(res, initial_capital, universe, ["A"])


def test_ledger_parity_under_china_trading_rules():
    """The ledger invariant must survive T+1 settlement, lot sizing and sell-side stamp duty.

    China rules change the cash side of every SELL (stamp duty) and can block a
    trade outright (T+1 / limit moves), so they are the easiest place for the
    cash book to drift away from the reported weights. The replay must still
    match, which pins the fee treatment on both sides of the book.
    """
    idx = pd.bdate_range("2023-01-01", periods=4)
    px = [10.0, 10.5, 9.8, 9.8]
    df = pd.DataFrame(
        {"Open": px, "High": px, "Low": px, "Close": px, "Volume": [1e5] * 4}, index=idx
    )
    universe = {"600000.SH": df}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    target_weights.iloc[0] = [0.6]
    target_weights.iloc[2] = [1.0]
    target_weights.iloc[3] = [0.0]

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe,
        target_weights,
        initial_capital=initial_capital,
        china_trading=True,
        commission_pct=0.0005,
        slippage_pct=0.0005,
        stamp_duty_pct=0.0005,
    )

    report = res["rebalance_report"]
    assert not report.empty
    # Every fill is a whole 100-share lot.
    assert (report["shares"] % 100 == 0).all()
    # Sells (only) carry the stamp duty on top of commission + slippage.
    sells = report[report["action"] == "SELL"]
    assert not sells.empty
    np.testing.assert_allclose(sells["total_cost"], sells["trade_value"] * 0.0015)
    buys = report[report["action"] == "BUY"]
    np.testing.assert_allclose(buys["total_cost"], buys["trade_value"] * 0.001)

    _assert_ledger_parity(res, initial_capital, universe, ["600000.SH"])


def test_rebalance_day_weights_report_fills_not_instructions():
    """B1.1 core regression: a rebalance date's weights are the fills, not the target.

    The old code wrote `new_w[i] = tgt_w_arr[t, i]` for every traded symbol, so
    `actual_weights` on a rebalance date was EXACTLY the target even when the
    lot size made that impossible. Here every rebalance is lot-truncated, so the
    reported weights must sit away from the targets and must equal the ledger.
    """
    idx = pd.bdate_range("2020-01-01", periods=5)
    universe = {
        "A": make_df([333.0, 340.0, 350.0, 345.0, 400.0], start="2020-01-01"),
        "B": make_df([37.0, 36.5, 38.0, 39.0, 40.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B"])
    target_weights.iloc[0] = [0.5, 0.5]
    target_weights.iloc[4] = [0.9, 0.1]

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=initial_capital, min_shares=100
    )

    rebalanced = res["actual_weights"].loc[[idx[0], idx[4]]]
    targets = target_weights.loc[[idx[0], idx[4]]]

    # The engine must not parrot the instruction back on a rebalance date...
    assert not np.allclose(rebalanced.to_numpy(), targets.to_numpy(), atol=1e-3)
    # ...while still moving toward it: A is bought up from its drifted weight
    # (as close to the 0.9 target as whole 100-share lots of a $400 stock allow).
    drifted_a = float(res["actual_weights"].iloc[3]["A"])
    assert rebalanced.iloc[1]["A"] > drifted_a
    assert rebalanced.iloc[1]["A"] < 0.9

    _assert_ledger_parity(res, initial_capital, universe, ["A", "B"])


# --------------------------------------------------------------------------- #
# Task B3.1 -- the SAME ledger identities must hold when the fill happens on the
# session AFTER the signal (`next_open`), including when orders are deferred
# through board-locked / suspended bars.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("execution_price", ["close", "next_open"])
def test_ledger_parity_holds_in_every_execution_price_mode(execution_price):
    """`equity[t] == cash + holdings@close` regardless of WHEN the fill happened.

    The replay only ever sees the returned report: signed share deltas, the fill
    price and the per-trade costs, applied on the report's own `date`. In
    `next_open` that date is the EXECUTION session, so a green run here proves
    the fill is booked on the execution day and that equity is still
    cash + holdings -- the signal date carries no trade at all.
    """
    idx = pd.bdate_range("2020-01-01", periods=9)
    universe = {
        "A": make_df([50.0, 52.0, 49.0, 55.0, 53.0, 58.0, 57.0, 61.0, 60.0], start="2020-01-01"),
        "B": make_df([24.0, 23.5, 25.0, 24.0, 26.5, 27.0, 26.0, 25.5, 27.5], start="2020-01-01"),
        "C": make_df([311.0, 305.0, 319.0, 322.0, 301.0, 298.0, 310.0, 315.0, 320.0], start="2020-01-01"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["A", "B", "C"])
    for i, row in ((0, [0.4, 0.3, 0.3]), (2, [0.2, 0.5, 0.3]), (5, [0.6, 0.0, 0.4]), (7, [0.0, 1.0, 0.0])):
        target_weights.iloc[i] = row

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=initial_capital, min_shares=10,
        execution_price=execution_price,
    )

    eq, _w = _assert_ledger_parity(res, initial_capital, universe, ["A", "B", "C"])
    assert eq[-1] != pytest.approx(initial_capital, abs=1.0)
    assert (res["actual_weights"].sum(axis=1) <= 1.0 + 1e-9).all()

    _eq, _w2, cash = _replay_ledger(
        initial_capital, res["rebalance_report"], res["equity_curve"].index,
        pd.DataFrame({s: df["Close"] for s, df in universe.items()}), ["A", "B", "C"],
    )
    assert cash.min() >= -_CASH_TOL, f"cash went negative: {cash.min()}"

    if execution_price == "next_open":
        # The fill date can never be the signal date, and day 0 is all cash.
        assert idx[0].strftime("%Y-%m-%d") not in set(res["rebalance_report"]["date"])
        assert res["equity_curve"]["equity"].iloc[0] == pytest.approx(initial_capital)


def test_ledger_parity_survives_deferred_orders_in_next_open_mode():
    """Deferrals (一字板 + suspension) must not break the ledger identities.

    Two names, three sessions of un-fillable orders (a board-locked open and a
    Volume==0 halt), then a normal fill. The replayed book has to match the
    engine's equity/weights exactly, with `cash >= 0` -- i.e. deferring an order
    moves it, it never charges the account for a trade that did not happen.
    """
    idx = pd.bdate_range("2023-01-02", periods=7)
    # +10% one-word boards on idx[1] and idx[2]; a normal bar afterwards.
    px = [10.0, 11.0, 12.1, 12.1, 12.1, 12.1, 12.1]
    boards = pd.DataFrame({
        "Open": px,
        "High": [10.1, 11.0, 12.1, 12.2, 12.3, 12.3, 12.3],
        "Low":  [9.9, 11.0, 12.1, 12.0, 11.9, 11.9, 11.9],
        "Close": px,
        "Volume": [1e6, 1e6, 1e6, 1e6, 1e6, 1e6, 1e6],
    }, index=idx)
    halted = pd.DataFrame(
        {"Open": [20.0] * 7, "High": [20.2] * 7, "Low": [19.8] * 7,
         "Close": [20.0] * 7, "Volume": [1e6, 0.0, 0.0, 1e6, 1e6, 1e6, 1e6]},
        index=idx,
    )
    universe = {"600000.SH": boards, "000001.SZ": halted}
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["600000.SH", "000001.SZ"])
    target_weights.iloc[0] = [0.5, 0.5]
    target_weights.iloc[4] = [0.3, 0.7]

    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=initial_capital,
        china_trading=True, execution_price="next_open",
    )

    assert not res["deferral_report"].empty
    assert {"open_limit_up", "volume_zero"} <= set(res["deferral_report"]["reason"])
    # The first fill of the board-locked name is at its first NON-locked open.
    first_board_fill = res["rebalance_report"][res["rebalance_report"]["symbol"] == "600000.SH"].iloc[0]
    assert first_board_fill["date"] == idx[3].strftime("%Y-%m-%d")

    eq, _w = _assert_ledger_parity(res, initial_capital, universe, ["600000.SH", "000001.SZ"])
    assert eq[-1] != pytest.approx(initial_capital, abs=1.0)
    assert (res["actual_weights"].sum(axis=1) <= 1.0 + 1e-9).all()

    _eq, _w2, cash = _replay_ledger(
        initial_capital, res["rebalance_report"], res["equity_curve"].index,
        pd.DataFrame({s: df["Close"] for s, df in universe.items()}),
        ["600000.SH", "000001.SZ"],
    )
    assert cash.min() >= -_CASH_TOL, f"cash went negative: {cash.min()}"


# --------------------------------------------------------------------------- #
# Task B3.5 -- the ledger identities must hold for a SHORT book too, including a
# PARTIAL rebalance that EXTENDS an existing short.
#
# `_replay_ledger` signs a fill by its `action` (`+shares` for BUY, `-shares`
# for SELL), i.e. it already assumed the correct convention. The pre-B3.5 engine
# branched on the sign of the PRIOR position instead, so a short-side SELL moved
# the share count one way (`prior_s + trade_s`, toward flat) and the cash flow
# the other way -- and the replay could not agree with it. Default fees are ON
# here on purpose: the cost leg must ride along with the corrected signed delta.
# --------------------------------------------------------------------------- #
def _short_book_fixture():
    """`L` + `S`: day 0 opens a short, day 1 makes it BIGGER (partial rebalance)."""
    idx = pd.bdate_range("2023-01-02", periods=5)
    universe = {
        "L": make_df([40.0, 41.0, 42.0, 43.0, 44.0], start="2023-01-02"),
        "S": make_df([90.0, 89.0, 88.0, 87.0, 86.0], start="2023-01-02"),
    }
    target_weights = pd.DataFrame(np.nan, index=idx, columns=["L", "S"])
    target_weights.iloc[0] = [0.5, -0.25]
    target_weights.iloc[1] = [0.5, -0.40]
    return universe, target_weights


@pytest.mark.parametrize("execution_price", ["close", "next_open"])
def test_ledger_parity_for_a_short_book_with_a_partial_rebalance(execution_price):
    """`equity == cash + holdings` and `cash >= 0` while a short is EXTENDED.

    Four things are checked, all through an independent replay of the report:
      * the replayed equity/weights match the engine exactly;
      * cash never goes negative (a short add RELEASES cash, it never consumes
        it -- the pre-B3.5 inverted sign consumed cash to "sell" phantom shares);
      * the short leg is negative on the entry day and stays negative after the
        partial rebalance (direction kept);
      * extending a short INCREASES cash by the trade's value net of costs.
    """
    universe, target_weights = _short_book_fixture()
    initial_capital = 100_000.0
    res = run_allocation_backtest(
        universe, target_weights, initial_capital=initial_capital, min_shares=10,
        execution_price=execution_price,
    )
    closes = pd.DataFrame({s: df["Close"] for s, df in universe.items()})
    dates = res["equity_curve"].index
    closes = closes.loc[dates]

    eq, w = _assert_ledger_parity(res, initial_capital, universe, ["L", "S"])
    replayed_eq, _replayed_w, cash = _replay_ledger(
        initial_capital, res["rebalance_report"], dates, closes, ["L", "S"])
    np.testing.assert_allclose(eq, replayed_eq, rtol=_PARITY_RTOL, atol=_PARITY_ATOL)
    assert cash.min() >= -_CASH_TOL, f"cash went negative: {cash.min()}"

    # The short leg is present, negative, and still negative after the rebalance.
    short_col = res["actual_weights"].columns.get_loc("S")
    assert (w[:, short_col] < 0).any()
    entry_day = 0 if execution_price == "close" else 1
    reb_day = entry_day + 1
    assert (w[entry_day:, short_col] < 0).all()
    assert w[reb_day, short_col] < w[entry_day, short_col]   # MORE short, not covered

    # Cash moves WITH the short: the add is a sale, so the balance grows by the
    # traded value net of costs. The pre-B3.5 book "sold" phantom shares (same
    # label, but `target_shares > prior_shares`), which DRAINED cash; the
    # direction assertions above plus this cash direction pin both sides.
    short_rows = res["rebalance_report"][res["rebalance_report"]["symbol"] == "S"]
    add = short_rows.iloc[-1]
    assert add["action"] == "SELL"
    assert add["target_shares"] < add["prior_shares"]
    assert float(add["trade_value"]) - float(add["total_cost"]) > 0
    day = dates.get_loc(pd.Timestamp(add["date"]))
    assert cash[day] > cash[day - 1]

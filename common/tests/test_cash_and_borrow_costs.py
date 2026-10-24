"""Tests for tasks B3.3 (idle-cash repo interest) and B3.4 (short borrow fee).

`common/allocation_backtester.run_allocation_backtest` gains two OPT-IN,
DAILY-SETTLEMENT carry items:

* `cash_yield_annual` (B3.3) -- an annualized repo/逆回购 rate credited on the
  ledger's UNINVESTED cash balance, accrued on ACTUAL CALENDAR DAYS / 365;
* `borrow_fee_annual` (B3.4) -- an annualized securities-borrow fee charged on
  the market value of a SHORT book, accrued on the same basis and deducted from
  cash.

Both are folded into the EXISTING per-day loop as a settlement step (step 0 of
each bar) that mutates `cash` and nothing else, so the ledger identity
`equity[t] == cash + holdings` keeps holding without a second state machine and
without a liability side-account.

Everything here is 100% offline: prices are hand-built arrays, and the one
random-walk fixture is seeded.

The first test is the important one: BOTH switches are OFF by default and the
default path must be the pre-B3.3 engine BIT FOR BIT. It does that two ways:

  * seven sha256 snapshots of the PRE-edit tree (``_CASH_CARRY_DIGESTS``), and
  * a re-run of B3.2's own eight PRE-B3.2 snapshots
    (``test_market_impact_model._PRE_B32_DIGESTS``), i.e. a hash taken two tasks
    ago still matches after this task's edit.

Task B3.5 (2026-10-23) subsequently fixed the SHORT-SIDE SIGN defect (a SELL was
booked as `prior_s + trade_s` when `prior_s < 0`, so a short add covered and a
queued day-0 short became a long). That is a POSITION-DIRECTION bug, so it was
fixed rather than preserved, and the digests of every book that holds a short
were re-pinned: c1..c4 and d1..d4 below were re-captured, while the PURE-LONG
entries (c5..c7, d5..d6) and all eight ``_PRE_B32_DIGESTS`` are untouched bit for
bit -- which is the proof the fix reached only books that hold a short.

A second snapshot set (``_CARRY_ON_DIGESTS``) pins the behaviour of the ENABLED
configurations, so a future task cannot silently move them.
"""

import hashlib
import logging
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_COMMON_TESTS = os.path.join(_PROJECT_ROOT, "common", "tests")
if _COMMON_TESTS not in sys.path:
    sys.path.insert(0, _COMMON_TESTS)

from common.allocation_backtester import (  # noqa: E402
    CASH_CARRY_REPORT_COLUMNS,
    CASH_PROXY_SYMBOLS,
    _daily_borrow_fee,
    _daily_cash_interest,
    run_allocation_backtest,
)
from test_ledger_parity import _replay_ledger  # noqa: E402
import test_market_impact_model as _b32  # noqa: E402

# The day-count basis is a DOCUMENTED choice, not a free parameter: money-market
# repo (GC001, DR007) quotes and settles on ACT/365, and the idle cash really is
# parked over the weekend. See the engine docstring's "CASH CARRY" section.
_DAYS_PER_YEAR = 365.0


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _bars(closes, index=None, volume=None, start="2023-01-02"):
    """OHLCV frame from a close series. `volume=None` => price-only series."""
    closes = np.asarray(closes, dtype=float)
    idx = pd.DatetimeIndex(index) if index is not None else pd.bdate_range(start, periods=len(closes))
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
        },
        index=idx,
    )
    if volume is not None:
        vol = np.full(len(closes), float(volume)) if np.isscalar(volume) else np.asarray(volume, dtype=float)
        frame["Volume"] = vol
    return frame


def _sparse(index, rows, symbols):
    tw = pd.DataFrame(np.nan, index=pd.DatetimeIndex(index), columns=list(symbols))
    for row, weights in rows.items():
        tw.iloc[row] = weights
    return tw


def _flat_universe(n, price=100.0, start="2023-01-02", index=None):
    """A single name with a CONSTANT close and NO Volume column.

    Constant price => zero market P&L, so every equity move in these fixtures is
    carry and the expected values are hand-checkable. No Volume => impact and the
    participation cap are inert (B1.3/B3.2 "absent volume is unobservable").
    """
    idx = pd.DatetimeIndex(index) if index is not None else pd.bdate_range(start, periods=n)
    return {"A": _bars(np.full(len(idx), float(price)), index=idx)}


def _long_short_fixture():
    """3 names, fee-paying, Volume present, and a genuine SHORT opened at day 0.

    A short can only be OPENED on the day-0 entry in `close` mode; in the
    `next_*` modes the day-0 order is queued and filled from a FLAT book on day
    1. Pre-B3.5 that queued fill booked the short as a LONG (the sell loop
    branched on the prior sign), so `c4_next_open` was silently a two-long book.
    Task B3.5 fixed the sign convention; the digests in `_CASH_CARRY_DIGESTS` for
    c1..c4 were re-pinned on 2026-10-23 for exactly that reason (see the note on
    the table). The fixture itself is unchanged.
    """
    idx = pd.bdate_range("2021-03-01", periods=10)
    px = {
        "L": [40.0, 41.0, 39.5, 42.0, 41.0, 43.0, 42.5, 44.0, 43.0, 45.0],
        "S": [90.0, 89.0, 91.0, 88.0, 87.0, 89.5, 90.5, 88.5, 87.5, 86.0],
        "C": [15.0, 15.2, 14.9, 15.4, 15.1, 15.6, 15.8, 15.5, 15.9, 16.1],
    }
    universe = {sym: _bars(vals, index=idx, volume=2e5) for sym, vals in px.items()}
    tw = _sparse(idx, {0: [0.6, -0.25, 0.4], 4: [0.0, -0.25, 0.0], 7: [0.0, 0.0, 0.0]},
                 ["L", "S", "C"])
    return universe, tw


def _china_fixture():
    idx = pd.bdate_range("2023-01-02", periods=8)
    px = [10.0, 10.5, 9.8, 9.8, 10.6, 10.2, 10.9, 11.2]
    universe = {}
    for sym, base in (("600000.SH", 1.0), ("000001.SZ", 1.3), ("300750.SZ", 2.1)):
        p = [round(x * base, 4) for x in px]
        universe[sym] = _bars(p, index=idx, volume=1e5)
    universe["000001.SZ"].iloc[3, universe["000001.SZ"].columns.get_loc("Volume")] = 0.0
    tw = _sparse(idx, {0: [0.4, 0.3, 0.3], 2: [0.2, 0.5, 0.3], 3: [0.5, 0.2, 0.3],
                       6: [0.6, 0.0, 0.4]}, list(universe))
    return universe, tw


def _proxy_fixture(total_return=0.02):
    """A book that parks 90% in BIL, a CASH SUBSTITUTE, and holds 10% cash.

    BIL's price series is a TOTAL-RETURN series (its own carry compounds in the
    price), so a run that ALSO credited interest on the BIL notional would be
    visibly double counting.
    """
    idx = pd.bdate_range("2023-01-02", periods=5)
    k = np.arange(len(idx), dtype=float)
    bil = 100.0 * (1.0 + total_return / _DAYS_PER_YEAR) ** k
    universe = {"BIL": _bars(bil, index=idx, volume=5e6)}
    tw = _sparse(idx, {0: [0.9]}, ["BIL"])
    return universe, tw


def _all_cash_fixture():
    idx = pd.bdate_range("2023-01-02", periods=4)
    universe = _flat_universe(0, index=idx)
    tw = _sparse(idx, {0: [0.0]}, ["A"])
    return universe, tw


def _clean_short_fixture():
    """A short opened at day 0 and FULLY covered later, with nothing in between.

    Deliberately free of a PARTIAL short adjustment. The PRE-EXISTING DEFECT this
    fixture used to work around -- the rebalance path booked
    `new_s = prior_s + trade_s` for `prior_s < 0`, so an instruction to make a
    short MORE negative actually COVERED it while the cash flow was booked as a
    sale -- was reported as task B3.5 and FIXED on 2026-10-23 (a SELL is a
    negative signed share delta for every prior sign; see
    `common/allocation_backtester.py`'s B3.5 notes and
    `test_ledger_parity.test_ledger_parity_for_a_short_book_with_a_partial_rebalance`,
    which covers the partial-adjustment case this fixture avoids). Full
    liquidation (`target == 0`, the branch used here) was correct both before
    and after, which is why the ledger replay below keeps this fixture.
    """
    idx = pd.bdate_range("2021-03-01", periods=6)
    px = {"L": [40.0, 41.0, 39.5, 42.0, 41.0, 43.0],
          "S": [90.0, 89.0, 91.0, 88.0, 87.0, 89.5]}
    universe = {sym: _bars(vals, index=idx, volume=2e5) for sym, vals in px.items()}
    tw = _sparse(idx, {0: [0.6, -0.25], 4: [0.0, 0.0]}, ["L", "S"])
    return universe, tw


# --------------------------------------------------------------------------- #
# 1. Default path: bit-identical, proven against PRE-EDIT snapshots
# --------------------------------------------------------------------------- #
_DIGEST_SCALARS = (
    "total_turnover", "total_rebalances", "total_return", "cagr", "max_drawdown",
    "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor", "total_market_impact",
)


def _digest(res):
    """sha256 over every reported float (bit-level, not a tolerance).

    The three B3.3/B3.4 accumulators are read through `.get(..., 0.0)` on
    purpose: that is what lets the SAME digest be computed against the PRE-edit
    engine (which does not return those keys at all), so a value of `0.0` in the
    off state is proven equal to "the key did not exist yet".
    """
    h = hashlib.sha256()
    h.update(repr([float(v) for v in res["equity_curve"]["equity"].to_numpy()]).encode())
    h.update(repr([[float(x) for x in row] for row in res["actual_weights"].to_numpy()]).encode())
    for frame in ("rebalance_report", "suspension_report", "deferral_report"):
        df = res[frame]
        h.update(repr(list(df.columns)).encode())
        h.update(repr([
            [str(x) if isinstance(x, str) else float(x) for x in row] for row in df.to_numpy()
        ]).encode())
    for key in _DIGEST_SCALARS:
        h.update(repr(float(res[key])).encode())
    for key in ("total_cash_interest", "total_borrow_fees", "total_borrow_fees_unpaid"):
        h.update(repr(float(res.get(key, 0.0))).encode())
    h.update(repr(res.get("execution_price")).encode())
    return h.hexdigest()


def _snapshot_runs():
    """The seven configurations whose PRE-edit digests are pinned below.

    Every one of them leaves both switches at their DEFAULTS. They deliberately
    include the paths this task touches: a SHORT book (the borrow-fee branch), an
    all-cash book (the cash-interest branch), a cash-proxy book, the China rules,
    the `next_open` queue, `minimal_metrics`, and market impact + the
    participation cap.
    """
    runs = {}
    uni, tw = _long_short_fixture()
    runs["c1_long_short_close"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10)
    runs["c2_long_short_minimal"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, minimal_metrics=True)
    runs["c3_costs_and_cap"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        market_impact=True, max_volume_participation=0.10)
    runs["c4_next_open"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, execution_price="next_open")

    cu, ctw = _china_fixture()
    runs["c5_china_close"] = run_allocation_backtest(
        cu, ctw, initial_capital=100_000.0, china_trading=True,
        name_map={"600000.SH": "ST浦发"})

    pu, ptw = _proxy_fixture()
    runs["c6_cash_proxy_book"] = run_allocation_backtest(
        pu, ptw, initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0)

    au, atw = _all_cash_fixture()
    runs["c7_all_cash"] = run_allocation_backtest(au, atw, initial_capital=100_000.0)
    return runs


# Captured from the tree BEFORE the B3.3/B3.4 edit. These are the *definition* of
# "the default path did not move": any drift in the ledger loop, the day-0 entry,
# the `next_*` queue, the China rules, minimal-metrics mode or the report shapes
# shows up as a different hash, with zero tolerance involved.
#
# RE-PINNED by task B3.5 (2026-10-23), the SHORT-SIDE SIGN FIX -- old -> new for
# the FOUR configurations whose fixture holds a short (`_long_short_fixture`):
#     c1_long_short_close   391a3007..ab47 -> 0c5130db..8292  (max |d equity| 140.00, 1.3e-03 rel)
#     c2_long_short_minimal d19c69e9..61ec -> f9293795..97fb  (same, minimal-metrics twin)
#     c3_costs_and_cap      9e7a3736..c2fb -> c395caaa..96e7  (same)
#     c4_next_open          3af5f4ed..e210 -> ff05047e..f1c4  (max |d equity| 1636.93, 1.6e-02 rel)
# `c4` moves most because the pre-fix engine booked the day-0 SHORT as a LONG in
# `next_open`; `c1..c3` move only where the day-4 instruction rebalances that
# short (the old code covered it instead of extending it).
# c5_china_close / c6_cash_proxy_book / c7_all_cash are PURE-LONG (or flat) and
# are bit-identical -- that is the evidence the fix did not touch them.
_CASH_CARRY_DIGESTS = {
    "c1_long_short_close": "0c5130db011e471082ebd68428667d398590096d8bb28ceb6c0a4cb17c938292",
    "c2_long_short_minimal": "f92937950255e6a5476c91ac9fc6ed1378586bcb9caa807d0d0d8f7e178097fb",
    "c3_costs_and_cap": "c395caaaa09cc39d1dd0f690bbf9115349c1e8a7405da3871fec1cb71de496e7",
    "c4_next_open": "ff05047ee53a1bc0350641307736376b221ecc1370d7e7f97c3040cb00f0f1c4",
    "c5_china_close": "f6b9373cfc095ceb172b341a1f7db83577ab164cf85f03e4015dfb7dda3bbae0",
    "c6_cash_proxy_book": "6be158f7bf187c219be4e92973a2c1f591092f2d49ac484879fb9e77abf5c674",
    "c7_all_cash": "5b2ad61c24a7354f2f87ea1685ebee178cc0c7ea00d82b7ef6d12163d80760f3",
}

# Captured immediately AFTER the edit, for the ENABLED configurations only. They
# are not a pre-edit baseline (the switches did not exist before); they are a
# forward lock, so a later task cannot silently change what B3.3/B3.4 charge.
# B3.5 (2026-10-23) re-pinned d1..d4 for the same short-sign fix (all four run
# on the short-bearing `_long_short_fixture`); d5_proxy_book / d6_china_cash_yield
# are long-only and did not move a bit.
_CARRY_ON_DIGESTS = {
    "d1_cash_yield": "7bb4fc09071c0118b4bf82bb9dc37e5e7f8b67c3771a5704cad597b4d44ea5d0",
    "d2_borrow_fee": "2c59b239285c41ccf46c01b866ae34126559d4fe767a1ff85a24a2c833dab8bf",
    "d3_both": "16b2110fbaac18fd5ebaf5b9b32debb2f4e6944ddbe38caa1c809a52dbbe493f",
    "d4_both_minimal": "bfcda1122bf270a2c753b27197b0cdccf4cf08bbd444a9a904612cee0fa7ecba",
    "d5_proxy_book": "17cfd2eb46175998c544447dfc39770812cf8b8e5bd625d8c824d339908e29f3",
    "d6_china_cash_yield": "4fe1f8dbc53aa093d9b764ccf798c278e4524f73c31c01afc9f59d91b56fb2a6",
}


def _assert_bit_identical(explicit, default, keys=_DIGEST_SCALARS + (
    "total_cash_interest", "total_borrow_fees", "total_borrow_fees_unpaid",
)):
    """Zero-tolerance agreement on every scalar, frame and equity point."""
    np.testing.assert_allclose(
        explicit["equity_curve"]["equity"].to_numpy(),
        default["equity_curve"]["equity"].to_numpy(), rtol=0, atol=0)
    np.testing.assert_allclose(
        explicit["actual_weights"].to_numpy(), default["actual_weights"].to_numpy(),
        rtol=0, atol=0)
    for frame in ("rebalance_report", "suspension_report", "deferral_report",
                  "cash_carry_report"):
        pd.testing.assert_frame_equal(explicit[frame], default[frame], check_exact=True)
    for key in keys:
        assert explicit[key] == default[key], f"{key} moved: {explicit[key]!r} != {default[key]!r}"


def test_cash_and_borrow_are_off_by_default_and_the_default_path_is_bit_identical():
    """Both switches are OPT-IN; the default path must not move a single bit.

    Three independent pieces of evidence:
      1. spelling the switches as `None` (or `0.0`) IS the omitted-argument path;
      2. the seven PRE-edit snapshots still hash identically;
      3. task B3.2's eight PRE-B3.2 snapshots -- hashes captured two tasks ago --
         still match after this edit.
    """
    uni, tw = _long_short_fixture()
    kwargs = dict(initial_capital=100_000.0, min_shares=10)
    default = run_allocation_backtest(uni, tw, **kwargs)
    explicit_none = run_allocation_backtest(
        uni, tw, cash_yield_annual=None, borrow_fee_annual=None, **kwargs)
    explicit_zero = run_allocation_backtest(
        uni, tw, cash_yield_annual=0.0, borrow_fee_annual=0.0, **kwargs)

    _assert_bit_identical(explicit_none, default)
    _assert_bit_identical(explicit_zero, default)
    # Nothing accrued, nothing borrowed, nothing recorded.
    assert default["total_cash_interest"] == 0.0
    assert default["total_borrow_fees"] == 0.0
    assert default["total_borrow_fees_unpaid"] == 0.0
    assert default["cash_yield_proxy_overlap"] is False
    assert default["cash_carry_report"].empty
    assert list(default["cash_carry_report"].columns) == CASH_CARRY_REPORT_COLUMNS

    # 2. The seven frozen PRE-edit snapshots.
    assert set(_CASH_CARRY_DIGESTS) == set(_snapshot_runs()), "snapshot set changed"
    for name, res in _snapshot_runs().items():
        assert _digest(res) == _CASH_CARRY_DIGESTS[name], (
            f"{name}: the default path moved -- B3.3/B3.4 leaked into the off state"
        )

    # 3. B3.2's own PRE-B3.2 snapshots (hashes taken two tasks ago) still hold.
    for name, res in _b32._snapshot_runs().items():
        assert _b32._digest(res) == _b32._PRE_B32_DIGESTS[name], (
            f"{name}: a PRE-B3.2 digest moved"
        )


def test_enabled_configurations_are_pinned():
    """Forward lock on what the ENABLED switches charge (see `_CARRY_ON_DIGESTS`)."""
    assert set(_CARRY_ON_DIGESTS) == set(_carry_on_runs()), "pinned config set changed"
    for name, res in _carry_on_runs().items():
        assert _digest(res) == _CARRY_ON_DIGESTS[name], f"{name}: enabled behaviour moved"


def _carry_on_runs():
    """The ENABLED configurations locked by `_CARRY_ON_DIGESTS`."""
    runs = {}
    uni, tw = _long_short_fixture()
    runs["d1_cash_yield"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, cash_yield_annual=0.02)
    runs["d2_borrow_fee"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, borrow_fee_annual=0.045)
    runs["d3_both"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        cash_yield_annual=0.02, borrow_fee_annual=0.045)
    runs["d4_both_minimal"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, minimal_metrics=True,
        cash_yield_annual=0.02, borrow_fee_annual=0.045)

    pu, ptw = _proxy_fixture()
    runs["d5_proxy_book"] = run_allocation_backtest(
        pu, ptw, initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0,
        cash_yield_annual=0.02)

    xu, xtw = _china_fixture()
    runs["d6_china_cash_yield"] = run_allocation_backtest(
        xu, xtw, initial_capital=100_000.0, china_trading=True, cash_yield_annual=0.02)
    return runs


# --------------------------------------------------------------------------- #
# 2. B3.3 -- the day-count rule itself
# --------------------------------------------------------------------------- #
def test_cash_interest_accrues_on_actual_calendar_days_over_365():
    """ACT/365 on CALENDAR days -- hand-computable, and NOT a trading-day basis.

    Sessions Thu 01-05, Fri 01-06, Mon 01-09, Tue 01-10 => the gaps are 1, 3, 1
    calendar days, so a fully-cashed 100,000 book at 2% earns

        day 1: 100,000 * 0.02 * 1/365
        day 2:      cash * 0.02 * 3/365   (the weekend is three days of carry)
        day 3:      cash * 0.02 * 1/365

    i.e. 5 calendar days of interest, compounded on the running balance (~27.40).
    A trading-day basis would have credited 3 sessions instead, which is a
    materially different number and is asserted against below.
    """
    idx = pd.DatetimeIndex(["2023-01-05", "2023-01-06", "2023-01-09", "2023-01-10"])
    universe = _flat_universe(0, index=idx)
    tw = _sparse(idx, {0: [0.0]}, ["A"])

    rate = 0.02
    res = run_allocation_backtest(
        universe, tw, initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0,
        cash_yield_annual=rate)

    # Independent recurrence over the SAME calendar gaps.
    expected, cash = [100_000.0], 100_000.0
    for gap in (1, 3, 1):
        cash += cash * rate * gap / _DAYS_PER_YEAR
        expected.append(cash)
    np.testing.assert_allclose(
        res["equity_curve"]["equity"].to_numpy(), expected, rtol=0, atol=1e-9)

    total = res["total_cash_interest"]
    assert total == pytest.approx(100_000.0 * rate * 5.0 / _DAYS_PER_YEAR, rel=1e-4)
    # NOT the trading-day variants (3 sessions / 252 or / 365).
    assert total != pytest.approx(100_000.0 * rate * 3.0 / 252.0, rel=1e-3)
    assert total != pytest.approx(100_000.0 * rate * 3.0 / _DAYS_PER_YEAR, rel=1e-3)

    # The per-day settlement step is on the record, with the calendar gap it used.
    carry = res["cash_carry_report"]
    assert list(carry["calendar_days"]) == [1.0, 3.0, 1.0]
    assert list(carry["borrow_fee"]) == [0.0, 0.0, 0.0]
    np.testing.assert_allclose(carry["cash_interest"].sum(), total, rtol=0, atol=1e-12)
    # Cash only ever grows with a positive yield, and it is the LEDGER's cash.
    assert (np.diff(carry["cash_balance"].to_numpy()) > 0).all()


def test_cash_interest_is_never_credited_on_a_negative_balance():
    """Defensive: a NEGATIVE (margin) balance must not earn interest.

    B1.1 keeps `cash >= 0` on every path, so this is a defence against a future
    regression rather than a reachable state. The helper is asserted directly,
    and every fixture in this module is checked for `cash >= 0` at the end.
    """
    assert _daily_cash_interest(-1_000.0, 0.02, 1) == 0.0
    assert _daily_cash_interest(0.0, 0.02, 30) == 0.0
    assert _daily_cash_interest(float("nan"), 0.02, 1) == 0.0
    assert _daily_cash_interest(1_000.0, None, 1) == 0.0
    assert _daily_cash_interest(1_000.0, 0.02, 0) == 0.0
    # ... and it is genuinely proportional when it DOES apply.
    assert _daily_cash_interest(100_000.0, 0.02, 1) == pytest.approx(
        100_000.0 * 0.02 / 365.0, rel=1e-15)

    # A book that is 100% long still cannot run negative cash, carry included.
    idx = pd.bdate_range("2023-01-02", periods=4)
    universe = {"A": _bars([100.0, 101.0, 99.0, 102.0], index=idx)}
    tw = _sparse(idx, {0: [1.0], 2: [1.0]}, ["A"])
    res = run_allocation_backtest(
        universe, tw, initial_capital=100_000.0, cash_yield_annual=0.02,
        borrow_fee_annual=0.045)
    _eq, _w, cash = _replay_ledger(
        100_000.0, res["rebalance_report"], res["equity_curve"].index,
        pd.DataFrame({"A": universe["A"]["Close"]}), ["A"])
    # The replay omits the carry, so compare against it explicitly.
    net_carry = res["total_cash_interest"] - res["total_borrow_fees"]
    assert cash.min() + net_carry >= -1e-6
    assert (res["actual_weights"].to_numpy().sum(axis=1) <= 1.0 + 1e-9).all()


# --------------------------------------------------------------------------- #
# 3. B3.4 -- the borrow fee itself
# --------------------------------------------------------------------------- #
def _short_fixture(rows=((0, [-0.5]),)):
    idx = pd.bdate_range("2023-01-02", periods=4)
    universe = _flat_universe(0, index=idx)
    tw = _sparse(idx, {r: w for r, w in rows}, ["A"])
    return universe, tw, idx


def test_borrow_fee_accrues_daily_on_the_prior_close_short_notional():
    """`short_notional * rate * days / 365`, marked at the PRIOR close, per day.

    100,000 at $100 with a -50% target: 500 shares short (proceeds 50,000 in
    cash, equity still 100,000). At 4.5%/yr over three one-day sessions:

        fee = 500 * 100 * 0.045 * 1/365 = 6.1643835616438...  per day
        total = 3 * fee               = 18.4931506849...

    The base is the PREVIOUS session's mark on purpose: that is the value that
    was actually borrowed across the interval, and it keeps the charge strictly
    causal (today's close cannot price today's overnight loan).
    """
    universe, tw, idx = _short_fixture()
    rate = 0.045
    res = run_allocation_backtest(
        universe, tw, initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0,
        min_shares=0, borrow_fee_annual=rate)

    day0 = res["rebalance_report"].iloc[0]
    assert day0["shares"] == 500.0 and day0["action"] == "SELL"
    assert day0["target_shares"] == -500.0
    assert res["equity_curve"]["equity"].iloc[0] == pytest.approx(100_000.0)

    fee = 500.0 * 100.0 * rate / _DAYS_PER_YEAR
    assert res["total_borrow_fees"] == pytest.approx(3.0 * fee, rel=1e-12)
    assert res["total_cash_interest"] == 0.0
    # ... and the helper agrees with the closed form, and never charges a book
    # that is flat / long-only / has no rate.
    assert _daily_borrow_fee(500.0 * 100.0, rate, 1) == pytest.approx(fee, rel=1e-15)
    assert _daily_borrow_fee(0.0, rate, 1) == 0.0
    assert _daily_borrow_fee(-5_000.0, rate, 1) == 0.0
    assert _daily_borrow_fee(50_000.0, None, 1) == 0.0
    assert _daily_borrow_fee(50_000.0, rate, 0) == 0.0

    carry = res["cash_carry_report"]
    assert len(carry) == 3
    np.testing.assert_allclose(carry["borrow_fee"].to_numpy(), [fee] * 3, rtol=1e-12)
    np.testing.assert_allclose(carry["cash_interest"].to_numpy(), 0.0, atol=0)
    # A fee is cash OUT, so the balance falls monotonically.
    assert (np.diff(carry["cash_balance"].to_numpy()) < 0).all()

    for k in range(1, 4):
        assert res["equity_curve"]["equity"].iloc[k] == pytest.approx(
            100_000.0 - k * fee, rel=1e-12)


def test_borrow_fee_stops_after_the_cover_and_the_cover_day_is_still_charged():
    """Settlement boundary: overnight accrual on the OPENING short book.

    The short is opened at day 0's close and covered at day 2's close. Day 1 and
    day 2 both charge (each charges the book that was short OVERNIGHT into that
    session), and day 3 charges nothing because the book is flat when the night
    starts. So exactly TWO days of fee, never three and never one.
    """
    universe, tw, idx = _short_fixture(rows=((0, [-0.5]), (2, [0.0])))
    rate = 0.045
    res = run_allocation_backtest(
        universe, tw, initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0,
        min_shares=0, borrow_fee_annual=rate)

    fee = 500.0 * 100.0 * rate / _DAYS_PER_YEAR
    assert res["total_borrow_fees"] == pytest.approx(2.0 * fee, rel=1e-12)

    carry = res["cash_carry_report"]
    assert list(carry["date"]) == [
        idx[1].strftime("%Y-%m-%d"), idx[2].strftime("%Y-%m-%d"),
    ]
    # The cover itself is a real fill on day 2, AFTER the day's fee.
    cover = res["rebalance_report"][res["rebalance_report"]["action"] == "BUY"]
    assert list(cover["date"]) == [idx[2].strftime("%Y-%m-%d")]
    assert cover.iloc[0]["shares"] == 500.0
    assert cover.iloc[0]["target_shares"] == 0.0
    # Day 3 pays nothing: carrying the fee one day too long would show here.
    assert res["equity_curve"]["equity"].iloc[3] == pytest.approx(
        res["equity_curve"]["equity"].iloc[2], rel=1e-15)


def test_no_short_position_means_no_borrow_fee():
    """A long-only book pays nothing, and the run is bit-identical to fee-off."""
    idx = pd.bdate_range("2023-01-02", periods=5)
    universe = {"A": _bars([100.0, 102.0, 99.0, 103.0, 101.0], index=idx, volume=1e5)}
    tw = _sparse(idx, {0: [0.6], 2: [0.4]}, ["A"])
    kwargs = dict(initial_capital=100_000.0, min_shares=10)

    off = run_allocation_backtest(universe, tw, **kwargs)
    on = run_allocation_backtest(universe, tw, borrow_fee_annual=0.045, **kwargs)
    assert on["total_borrow_fees"] == 0.0
    assert on["total_borrow_fees_unpaid"] == 0.0
    assert on["cash_carry_report"].empty
    _assert_bit_identical(on, off)


# --------------------------------------------------------------------------- #
# 4. No double counting -- with cash proxies, and with execution costs
# --------------------------------------------------------------------------- #
def test_cash_proxy_holding_is_not_credited_twice(caplog):
    """Interest is credited on the CASH LEG ONLY, never on the proxy's notional.

    THE RULE (coexist, not exclusive): `cash_yield_annual` accrues on the ledger's
    uninvested cash. A cash substitute (BIL / 511880 / GC001 / ...) is an
    ordinary POSITION whose carry arrives through its own price series, so the
    two credits are on DISJOINT assets and cannot overlap by construction -- the
    proxy's value is never part of the interest base, and its price path is never
    touched by the yield.

    The test makes the distinction visible: BIL's series is a 2% TOTAL-RETURN
    series, 90% of the book is parked in it, and the equity gain versus the
    yield-off run must equal interest on the 10% cash leg ALONE (~2.19), not on
    the whole book (~21.9, what double crediting would produce).
    """
    assert {"BIL", "511880", "GC001"} <= CASH_PROXY_SYMBOLS
    universe, tw = _proxy_fixture(total_return=0.02)
    kwargs = dict(initial_capital=100_000.0, commission_pct=0.0, slippage_pct=0.0)

    off = run_allocation_backtest(universe, tw, **kwargs)
    with caplog.at_level(logging.WARNING, logger="common.allocation_backtester"):
        on = run_allocation_backtest(universe, tw, cash_yield_annual=0.02, **kwargs)

    entry = off["rebalance_report"].iloc[0]
    cash0 = 100_000.0 - float(entry["trade_value"]) - float(entry["total_cost"])
    assert cash0 == pytest.approx(10_000.0)      # 90% went into the proxy

    expected, cash = 0.0, cash0
    for _ in range(len(universe["BIL"]) - 1):
        step = cash * 0.02 / _DAYS_PER_YEAR
        cash += step
        expected += step
    gap = on["equity_curve"]["equity"].iloc[-1] - off["equity_curve"]["equity"].iloc[-1]
    assert gap == pytest.approx(expected, abs=1e-9)
    assert on["total_cash_interest"] == pytest.approx(expected, abs=1e-9)
    # The whole-book figure a double count would produce is far away.
    assert gap < 0.5 * 100_000.0 * 0.02 * 4.0 / _DAYS_PER_YEAR

    # The overlap is DETECTED and disclosed, not silently ignored.
    assert on["cash_yield_proxy_overlap"] is True
    assert off["cash_yield_proxy_overlap"] is False
    msgs = [r.getMessage() for r in caplog.records if "cash substitute" in r.getMessage()]
    assert msgs, "holding a cash proxy alongside cash yield must be reported"

    # A caller's OWN ticker can be declared a cash substitute (and an unknown
    # ticker is NOT guessed to be one).
    custom_uni = {"MYCASH": universe["BIL"]}
    custom_tw = tw.rename(columns={"BIL": "MYCASH"})
    assert run_allocation_backtest(
        custom_uni, custom_tw, cash_yield_annual=0.02,
        cash_proxy_symbols={"MYCASH"}, **kwargs)["cash_yield_proxy_overlap"] is True
    assert run_allocation_backtest(
        custom_uni, custom_tw, cash_yield_annual=0.02,
        **kwargs)["cash_yield_proxy_overlap"] is False

    # The proxy's own carry is untouched: the difference between the two runs is
    # ONLY the cash-leg interest, i.e. the BIL leg earned exactly its price path
    # in both runs (no second credit, no cancelled return).
    np.testing.assert_allclose(
        on["equity_curve"]["equity"].to_numpy() - off["equity_curve"]["equity"].to_numpy(),
        np.cumsum([0.0] + [cash0 * (1 + 0.02 / _DAYS_PER_YEAR) ** k * 0.02 / _DAYS_PER_YEAR
                           for k in range(len(universe["BIL"]) - 1)]),
        rtol=1e-12, atol=1e-9)


def test_carry_costs_are_not_double_counted_with_commission_slippage_stamp_or_impact():
    """The carry is a FINANCING cost, not a second execution cost.

    With every other cost switched on (commission, slippage, market impact, the
    China sell-side stamp duty), enabling the carry must leave `rebalance_report`
    bit-identical -- same fills, same `commission`, same `slippage` (impact
    included), same `total_cost` -- and move equity by EXACTLY
    `total_cash_interest - total_borrow_fees`. That is the assertion that the two
    are charged on disjoint accounts: per-fill execution concession vs
    time-based carry on cash / borrowed stock.
    """
    idx = pd.bdate_range("2023-01-02", periods=6)
    px = {"L": [40.0, 41.0, 39.5, 42.0, 41.0, 43.0],
          "S": [90.0, 89.0, 91.0, 88.0, 87.0, 89.5]}
    universe = {s: _bars(v, index=idx, volume=2e5) for s, v in px.items()}
    # Static book: no rebalance after day 0, so the carry cannot change a fill.
    tw = _sparse(idx, {0: [0.5, -0.3]}, ["L", "S"])
    kwargs = dict(initial_capital=100_000.0, min_shares=10, commission_pct=0.0005,
                  slippage_pct=0.0005, market_impact=True, china_trading=False)

    off = run_allocation_backtest(universe, tw, **kwargs)
    on = run_allocation_backtest(
        universe, tw, cash_yield_annual=0.02, borrow_fee_annual=0.045, **kwargs)

    pd.testing.assert_frame_equal(on["rebalance_report"], off["rebalance_report"],
                                  check_exact=True)
    assert on["total_market_impact"] == off["total_market_impact"]
    assert on["total_turnover"] == off["total_turnover"]
    assert on["total_borrow_fees"] > 0.0
    assert on["total_cash_interest"] > 0.0

    gap = on["equity_curve"]["equity"].iloc[-1] - off["equity_curve"]["equity"].iloc[-1]
    assert gap == pytest.approx(
        on["total_cash_interest"] - on["total_borrow_fees"], abs=1e-9)

    # A short book really was open, i.e. the fee had a base to charge against.
    assert (on["rebalance_report"]["target_shares"] < 0).any()

    # And the China stamp duty path (a per-fill levy) is likewise untouched: the
    # carry is never folded into `total_cost`, so every fill still pays exactly
    # its own commission/slippage/stamp rate. NOTE: the FILLS themselves may
    # differ here (credited interest is extra cash, and these buys are
    # cash-limited), which is precisely why the static book above is the case
    # that pins frame equality -- "no double counting" is about WHERE a cost is
    # booked, not about freezing trade sizes.
    cu, ctw = _china_fixture()
    c_on = run_allocation_backtest(
        cu, ctw, initial_capital=100_000.0, china_trading=True, cash_yield_annual=0.02)
    rep = c_on["rebalance_report"]
    sells = rep[rep["action"] == "SELL"]
    buys = rep[rep["action"] == "BUY"]
    np.testing.assert_allclose(sells["total_cost"], sells["trade_value"] * 0.0015, atol=1e-9)
    np.testing.assert_allclose(buys["total_cost"], buys["trade_value"] * 0.001, atol=1e-9)
    assert c_on["total_cash_interest"] > 0.0
    assert c_on["total_borrow_fees"] == 0.0


# --------------------------------------------------------------------------- #
# 5. Ledger identities with the carry switched ON
# --------------------------------------------------------------------------- #
def test_equity_identity_holds_with_carry_in_every_mode():
    """`equity[t] == cash + holdings`, `cash >= 0`, `sum(actual_w) <= 1`.

    The carry is a DAILY CASH OUTFLOW (interest in, fee out) rather than a
    liability side-account, so an independent replay of the trade report matches
    the engine once the cumulative net carry is added back:

        equity[t] = replay_equity[t] + sum_{s<=t}(cash_interest_s - borrow_fee_s)

    That identity IS the proof that a liability-style accrual was avoided: a
    liability would have to be subtracted inside the equity formula, creating a
    second accounting path that the trade-level replay cannot see.
    """
    def _check(use_uni, use_tw, mode, expect_fee):
        res = run_allocation_backtest(
            use_uni, use_tw, initial_capital=100_000.0, min_shares=10,
            execution_price=mode, cash_yield_annual=0.02, borrow_fee_annual=0.045)
        dates = res["equity_curve"].index
        closes = pd.DataFrame({s: df["Close"] for s, df in use_uni.items()}).ffill().fillna(0.0)
        eq, _w, cash = _replay_ledger(
            100_000.0, res["rebalance_report"], dates, closes.loc[dates], list(use_uni))

        carry = res["cash_carry_report"]
        net = {r.date: r.cash_interest - r.borrow_fee for r in carry.itertuples(index=False)}
        running, adj = 0.0, []
        for d in dates:
            running += net.get(d.strftime("%Y-%m-%d"), 0.0)
            adj.append(running)
        np.testing.assert_allclose(
            res["equity_curve"]["equity"].to_numpy(), eq + np.array(adj),
            rtol=1e-12, atol=1e-8)

        assert carry["cash_interest"].sum() == pytest.approx(
            res["total_cash_interest"], abs=1e-9)
        assert carry["borrow_fee"].sum() == pytest.approx(
            res["total_borrow_fees"], abs=1e-9)
        assert (res["total_borrow_fees"] > 0.0) is expect_fee
        assert (res["actual_weights"].to_numpy().sum(axis=1) <= 1.0 + 1e-9).all()
        return res, cash

    # `close` mode: a REAL short book (opened at day 0, fully covered at day 4).
    res, cash = _check(*_clean_short_fixture(), "close", expect_fee=True)
    # Nothing was left unpaid, and the ledger never ran a negative balance.
    assert res["total_borrow_fees_unpaid"] == 0.0
    assert cash.min() + (res["total_cash_interest"] - res["total_borrow_fees"]) >= -1e-6

    # `next_open` mode: long-only (the day-0 short cannot survive the queue -- see
    # `_clean_short_fixture`'s note), so cash interest only.
    idx = pd.bdate_range("2023-01-02", periods=6)
    universe = {"A": _bars([100.0, 102.0, 99.0, 103.0, 101.0, 104.0], index=idx)}
    tw_long = _sparse(idx, {0: [0.6], 3: [0.5]}, ["A"])
    _res, cash = _check(universe, tw_long, "next_open", expect_fee=False)
    assert cash.min() >= -1e-9


def test_minimal_metrics_reports_the_same_carry_scalars():
    """`minimal_metrics` is a reporting switch: the accrue/charge maths is identical."""
    uni, tw = _long_short_fixture()
    kwargs = dict(initial_capital=100_000.0, min_shares=10,
                  cash_yield_annual=0.02, borrow_fee_annual=0.045)
    full = run_allocation_backtest(uni, tw, **kwargs)
    minimal = run_allocation_backtest(uni, tw, minimal_metrics=True, **kwargs)
    for key in _DIGEST_SCALARS + ("total_cash_interest", "total_borrow_fees",
                                  "total_borrow_fees_unpaid"):
        assert minimal[key] == full[key], f"{key} differs in minimal mode"
    assert minimal["cash_carry_report"].empty
    assert list(minimal["cash_carry_report"].columns) == CASH_CARRY_REPORT_COLUMNS


# --------------------------------------------------------------------------- #
# 6. Validation: bad rates fail loudly, never silently
# --------------------------------------------------------------------------- #
def test_invalid_rates_are_rejected():
    idx = pd.bdate_range("2023-01-02", periods=3)
    universe = _flat_universe(0, index=idx)
    tw = _sparse(idx, {0: [0.5]}, ["A"])

    with pytest.raises(ValueError, match="cash_yield_annual"):
        run_allocation_backtest(universe, tw, cash_yield_annual=True)
    with pytest.raises(ValueError, match="cash_yield_annual"):
        run_allocation_backtest(universe, tw, cash_yield_annual=float("nan"))
    with pytest.raises(ValueError, match="cash_yield_annual"):
        run_allocation_backtest(universe, tw, cash_yield_annual=-1.0)
    with pytest.raises(ValueError, match="borrow_fee_annual"):
        run_allocation_backtest(universe, tw, borrow_fee_annual=-0.01)
    with pytest.raises(ValueError, match="borrow_fee_annual"):
        run_allocation_backtest(universe, tw, borrow_fee_annual=float("inf"))
    # A bare string would silently iterate into characters instead of tickers.
    with pytest.raises(ValueError, match="cash_proxy_symbols"):
        run_allocation_backtest(universe, tw, cash_yield_annual=0.02,
                                cash_proxy_symbols="BIL")

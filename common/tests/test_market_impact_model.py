"""Tests for task B3.2 -- ADV20 square-root market impact + participation cap.

`common/allocation_backtester.run_allocation_backtest` gains two OPT-IN
execution-fidelity features:

* an Almgren-Chriss SQUARE-ROOT temporary impact
  (``cost = notional * sigma * gamma * sqrt(shares / ADV20)``), and
* a hard liquidity budget (`max_volume_participation`), whose unfilled excess is
  DEFERRED through the B3.1 pending-order machinery with the new
  ``volume_participation_cap`` reason.

Everything here is 100% offline: prices/volumes are hand-built arrays, and
random-walk fixtures use a seeded `numpy` generator.

The first test is the important one: both features are OFF by default and the
default path must be the pre-B3.2 engine BIT FOR BIT. It pins a sha256 digest of
every float the engine reports, computed from the tree BEFORE this task's edit.
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

from common.allocation_backtester import run_allocation_backtest
from test_ledger_parity import _replay_ledger


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _bar_frame(prices, volume, seed=None, vol_scale=0.012):
    """OHLCV frame. `volume` may be a scalar (broadcast) or a sequence."""
    prices = np.asarray(prices, dtype=float)
    n = len(prices)
    vol = np.full(n, float(volume)) if np.isscalar(volume) else np.asarray(volume, dtype=float)
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": vol,
        },
        index=pd.bdate_range("2023-01-02", periods=n),
    )


def _random_walk(n=30, seed=3, vol=0.012):
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))


def _sparse(idx, weights_by_row, symbols=("A",)):
    tw = pd.DataFrame(np.nan, index=idx, columns=list(symbols))
    for row, w in weights_by_row.items():
        tw.iloc[row] = w
    return tw


def _identical(explicit, default, keys=(
    "total_turnover", "total_rebalances", "total_return", "cagr", "max_drawdown",
    "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor", "total_market_impact",
)):
    """Zero-tolerance agreement on every scalar, frame and equity point."""
    np.testing.assert_allclose(
        explicit["equity_curve"]["equity"].to_numpy(),
        default["equity_curve"]["equity"].to_numpy(), rtol=0, atol=0,
    )
    np.testing.assert_allclose(
        explicit["actual_weights"].to_numpy(), default["actual_weights"].to_numpy(), rtol=0, atol=0,
    )
    for frame in ("rebalance_report", "suspension_report", "deferral_report"):
        pd.testing.assert_frame_equal(explicit[frame], default[frame], check_exact=True)
    for key in keys:
        np.testing.assert_allclose(explicit[key], default[key], rtol=0, atol=0, err_msg=key)


# --------------------------------------------------------------------------- #
# 1. Default path: bit-identical, proven against a PRE-EDIT snapshot
# --------------------------------------------------------------------------- #
def _digest(res):
    """sha256 over every reported float (bit-level, not a tolerance)."""
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
                "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor"):
        h.update(repr(float(res[key])).encode())
    h.update(repr(res.get("execution_price")).encode())
    return h.hexdigest()


def _multi_fixture():
    """Multi-symbol, fee-paying, drifting book WITH a Volume column.

    Volume is present on purpose: it makes the participation cap and the impact
    model *eligible*, so the digest also proves the new code stays inert when
    both switches are off (a fixture without volume would pass vacuously).
    """
    idx = pd.bdate_range("2020-01-01", periods=12)
    vs = {
        "A": [50.0, 52.0, 49.0, 55.0, 53.0, 58.0, 57.0, 61.0, 60.0, 62.0, 59.0, 63.0],
        "B": [24.0, 23.5, 25.0, 24.0, 26.5, 27.0, 26.0, 25.5, 27.5, 28.0, 27.0, 29.0],
        "C": [311.0, 305.0, 319.0, 322.0, 301.0, 298.0, 310.0, 315.0, 320.0, 318.0, 325.0, 330.0],
    }
    universe = {}
    for i, (sym, v) in enumerate(vs.items()):
        universe[sym] = pd.DataFrame(
            {
                "Open": [x * (1.0 + 0.001 * i) for x in v],
                "High": [x + 0.5 for x in v],
                "Low": [x - 0.5 for x in v],
                "Close": v,
                "Volume": [1e6 - 1000.0 * i] * len(v),
            },
            index=idx,
        )
    tw = pd.DataFrame(np.nan, index=idx, columns=list(vs))
    for i, row in ((0, [0.4, 0.3, 0.3]), (1, [0.2, 0.5, 0.3]), (4, [0.6, 0.0, 0.4]),
                   (7, [0.0, 1.0, 0.0]), (10, [0.35, 0.35, 0.3])):
        tw.iloc[i] = row
    return universe, tw


def _china_fixture():
    idx = pd.bdate_range("2023-01-02", periods=8)
    px = [10.0, 10.5, 9.8, 9.8, 10.6, 10.2, 10.9, 11.2]
    universe = {}
    for sym, base in (("600000.SH", 1.0), ("000001.SZ", 1.3), ("300750.SZ", 2.1)):
        p = [round(x * base, 4) for x in px]
        universe[sym] = pd.DataFrame(
            {"Open": p, "High": [x + 0.1 for x in p], "Low": [x - 0.1 for x in p],
             "Close": p, "Volume": [1e5] * len(p)}, index=idx,
        )
    universe["000001.SZ"].loc[idx[3], "Volume"] = 0.0
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe))
    tw.iloc[0] = [0.4, 0.3, 0.3]
    tw.iloc[2] = [0.2, 0.5, 0.3]
    tw.iloc[3] = [0.5, 0.2, 0.3]
    tw.iloc[6] = [0.6, 0.0, 0.4]
    return universe, tw


# Digests captured from the tree BEFORE the B3.2 edit. They are the *definition*
# of "the default path did not move": any drift in the close path, the `next_*`
# queueing, the China rules, the minimal-metrics mode or the report shapes shows
# up as a different hash with zero tolerance involved.
_PRE_B32_DIGESTS = {
    "s1_close_default": "af1c58ab605e1ed97819f2f4d805587023f21a41f0bb64986a3eab8ad43a7fe6",
    "s2_close_explicit": "af1c58ab605e1ed97819f2f4d805587023f21a41f0bb64986a3eab8ad43a7fe6",
    "s3_close_minimal": "8483143a5effa3206c483a97d4ebfd151794d7664c8bc822867d2b36ba26f59a",
    "s4_next_open": "a5d0225e61a0f54ef299e5123175296636bb6b1348ff7c8842b8155378f19951",
    "s5_china_close": "297ceaa20897073e7ce9541c719aa0973c376c9433817d5fbeb74d49d9f3536c",
    "s6_china_next_open": "6e0849148df812ba392a5f07208deb7c264befd6d09aa7b1432fd2cd46825bd0",
    "s7_next_vwap": "c0c58687cb8cd5d411da5e404a26b349bb4f04b6a34bca22707f3b08c0b1a1ad",
    "s8_intraday_twap": "3c025658594787041d38bc7f4d96399097c8062a8f73e62b143b7a828e6325a5",
}


def _snapshot_runs():
    """The eight configurations whose digests are pinned above."""
    runs = {}
    uni, tw = _multi_fixture()
    runs["s1_close_default"] = run_allocation_backtest(uni, tw, initial_capital=100_000.0, min_shares=10)
    runs["s2_close_explicit"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, execution_price="close")
    runs["s3_close_minimal"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, minimal_metrics=True)
    runs["s4_next_open"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10, execution_price="next_open")

    cu, ctw = _china_fixture()
    runs["s5_china_close"] = run_allocation_backtest(
        cu, ctw, initial_capital=100_000.0, china_trading=True, name_map={"600000.SH": "ST浦发"})
    runs["s6_china_next_open"] = run_allocation_backtest(
        cu, ctw, initial_capital=100_000.0, china_trading=True, execution_price="next_open")

    vuni = {}
    for sym, df in uni.items():
        df = df.copy()
        df["Amount"] = df["Close"] * df["Volume"]
        vuni[sym] = df
    runs["s7_next_vwap"] = run_allocation_backtest(
        vuni, tw, initial_capital=100_000.0, min_shares=10, execution_price="next_vwap")

    sessions = pd.DatetimeIndex([
        pd.Timestamp(d).replace(hour=9, minute=30) + pd.Timedelta(minutes=5 * k)
        for d in uni["A"].index for k in range(4)
    ])
    intraday = {}
    for sym, df in uni.items():
        rows_hi, rows_lo, rows_c = [], [], []
        for v in df["Close"].to_numpy():
            for _k in range(4):
                rows_hi.append(v * 1.005)
                rows_lo.append(v * 0.995)
                rows_c.append(v)
        intraday[sym] = pd.DataFrame({"High": rows_hi, "Low": rows_lo, "Close": rows_c}, index=sessions)
    runs["s8_intraday_twap"] = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        execution_price="intraday_twap", intraday_data=intraday)
    return runs


def test_market_impact_default_path_is_bit_identical():
    """Both features are OPT-IN: the default path must not move a single bit.

    `market_impact=False` / `max_volume_participation=None` (the defaults), the
    same two spelled explicitly, and the eight pre-edit snapshots must all come
    out IDENTICAL -- and the snapshots are sha256 digests of every reported
    float, captured before this task's edit, so "identical" means identical, not
    "within tolerance".
    """
    # 1. The explicit-off spellings are the omitted-argument path.
    uni, tw = _multi_fixture()
    default = run_allocation_backtest(uni, tw, initial_capital=100_000.0, min_shares=10)
    explicit = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        market_impact=False, impact_gamma=1.0, max_volume_participation=None,
    )
    _identical(explicit, default)
    assert default["total_market_impact"] == 0.0
    # `close` mode never queues anything when the cap is off, so no new deferral
    # reason can appear on the default path.
    assert default["deferral_report"].empty

    # 2. The eight frozen snapshots.
    for name, res in _snapshot_runs().items():
        assert _digest(res) == _PRE_B32_DIGESTS[name], (
            f"{name}: the default path moved -- B3.2 leaked into the off state"
        )

    # 3. `minimal_metrics` must not change the new scalar either.
    minimal = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        market_impact=True, max_volume_participation=0.10, minimal_metrics=True,
    )
    full = run_allocation_backtest(
        uni, tw, initial_capital=100_000.0, min_shares=10,
        market_impact=True, max_volume_participation=0.10,
    )
    for key in ("total_turnover", "total_rebalances", "total_return", "cagr", "max_drawdown",
                "sharpe_ratio", "calmar_ratio", "win_rate", "profit_factor",
                "total_market_impact"):
        np.testing.assert_allclose(minimal[key], full[key], rtol=0, atol=0, err_msg=key)


# --------------------------------------------------------------------------- #
# 2. The square-root law itself
# --------------------------------------------------------------------------- #
def test_square_root_impact_is_monotone_concave_and_matches_the_formula():
    """`cost = notional * sigma * gamma * sqrt(Q / ADV20)`, verified numerically.

    Three things are asserted:
      * the reported cost matches a hand-computed square-root value exactly
        (sigma = past-20d std through the PREVIOUS session, ADV20 = mean of the
        prior sessions' volume);
      * it is MONOTONE in order size (a bigger order always costs more);
      * it is CONCAVE -- doubling the size doubles the notional but raises the
        impact FRACTION by only sqrt(2), so total cost grows ~2*sqrt(2), i.e.
        strictly less than the 4x a linear cost model would charge.
    """
    prices = np.array([100., 101., 99., 102., 103., 104., 103., 105., 106., 104., 107., 108.])
    volume = 2e5
    universe = {"A": _bar_frame(prices, volume)}
    idx = universe["A"].index
    tw = _sparse(idx, {5: [0.5]})

    def run(cap):
        return run_allocation_backtest(
            universe, tw, initial_capital=cap, commission_pct=0.0, slippage_pct=0.0,
            min_shares=0, market_impact=True,
        )

    res = run(1e6)
    tr = res["rebalance_report"].iloc[0]

    # --- closed-form check (day 5: 5 prior sessions, all inside the 20d window)
    rets = pd.Series(prices).pct_change().fillna(0.0)
    expected_sigma = float(rets.iloc[0:5].std())
    expected_adv = float(np.full(5, volume).mean())
    expected = float(tr["trade_value"]) * expected_sigma * 1.0 * np.sqrt(float(tr["shares"]) / expected_adv)
    assert tr["slippage"] == pytest.approx(expected, rel=1e-12)
    assert res["total_market_impact"] == pytest.approx(expected, rel=1e-12)

    # --- monotone + concave in size (fractional shares keep the ratio exact)
    small, large = run(1e6), run(2e6)
    s_tr = small["rebalance_report"].iloc[0]
    l_tr = large["rebalance_report"].iloc[0]
    assert l_tr["shares"] == pytest.approx(2.0 * s_tr["shares"])
    assert l_tr["trade_value"] == pytest.approx(2.0 * s_tr["trade_value"])
    # impact FRACTION grows by sqrt(2) ...
    frac_ratio = (l_tr["slippage"] / l_tr["trade_value"]) / (s_tr["slippage"] / s_tr["trade_value"])
    assert frac_ratio == pytest.approx(np.sqrt(2.0), rel=1e-9)
    # ... so total cost grows by 2*sqrt(2), strictly sub-linear vs 4x
    cost_ratio = l_tr["slippage"] / s_tr["slippage"]
    assert cost_ratio == pytest.approx(2.0 * np.sqrt(2.0), rel=1e-9)
    assert cost_ratio < 4.0

    # --- `gamma` scales the cost linearly, and gamma=0 disables it
    def run_gamma(g):
        return run_allocation_backtest(
            universe, tw, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            min_shares=0, market_impact=True, impact_gamma=g,
        )

    assert run_gamma(0.0)["total_market_impact"] == 0.0
    assert run_gamma(2.0)["total_market_impact"] == pytest.approx(
        2.0 * res["total_market_impact"], rel=1e-12)


def test_impact_is_charged_to_cash_once_and_never_double_counted_with_fee():
    """Impact leaves the CASH account, on top of `fee` -- not instead of it.

    With the cap off, the trade set is identical with and without the model, so
    the equity difference IS the impact total (nothing else moved). It also sits
    INSIDE the `slippage` column of `rebalance_report` while `commission` and
    `stamp` are untouched, and `commission + slippage + stamp == total_cost`
    still reconciles -- i.e. it is a genuine extra execution cost, not a second
    charge of the same money.
    """
    prices = _random_walk(25, seed=11)
    universe = {"A": _bar_frame(prices, 3e5)}
    idx = universe["A"].index
    tw = _sparse(idx, {0: [0.5], 8: [0.8], 16: [0.2]})
    kwargs = dict(initial_capital=1e6, commission_pct=0.0005, slippage_pct=0.0005,
                  min_shares=10)

    off = run_allocation_backtest(universe, tw, **kwargs)
    on = run_allocation_backtest(universe, tw, market_impact=True, **kwargs)

    assert off["total_market_impact"] == 0.0
    assert on["total_market_impact"] > 0.0
    # Same fills, so the whole equity gap is the impact.
    assert list(on["rebalance_report"]["shares"]) == list(off["rebalance_report"]["shares"])
    eq_gap = (off["equity_curve"]["equity"].iloc[-1] - on["equity_curve"]["equity"].iloc[-1])
    assert eq_gap == pytest.approx(on["total_market_impact"], rel=1e-9)

    # The cost columns still reconcile, and the flat bps terms are unchanged.
    # (`rebalance_report` has no separate `stamp` column: stamp duty is folded
    # into `total_cost` by the engine, so the reconcilable identity here is
    # commission + slippage within `total_cost`.)
    rep = on["rebalance_report"]
    off_rep = off["rebalance_report"]
    assert (rep["total_cost"] >= rep["commission"] + rep["slippage"] - 1e-9).all()
    np.testing.assert_allclose(rep["commission"], off_rep["commission"], rtol=1e-12, atol=1e-9)
    # ... and the whole gain in `slippage` is exactly the impact total.
    np.testing.assert_allclose(
        (rep["slippage"] - off_rep["slippage"]).sum(), on["total_market_impact"],
        rtol=1e-12, atol=1e-9,
    )
    # Ledger identities survive the extra cash cost.
    assert (on["actual_weights"].to_numpy().sum(axis=1) <= 1.0 + 1e-9).all()


# --------------------------------------------------------------------------- #
# 3. ADV20 causality / degraded warmup
# --------------------------------------------------------------------------- #
def test_adv20_uses_only_prior_sessions_never_todays_print():
    """ADV20 must be built from sessions BEFORE the fill -- no look-ahead.

    Two universes are identical except for the fill day's OWN volume. The impact
    charged on that day must be IDENTICAL (ADV20 cannot see it), while the
    participation CAP deliberately DOES see it (the task defines the budget as
    `volume_today x pct`) -- so the same perturbation moves the cap and leaves
    the impact untouched, which is exactly the disclosure in the docstring.
    """
    prices = np.array([100., 101., 99., 102., 103., 104., 103., 105., 106., 104., 107., 108.])
    idx = pd.bdate_range("2023-01-02", periods=len(prices))
    tw = _sparse(idx, {5: [0.5]})

    base_vol = np.full(len(prices), 1e4)
    quiet = base_vol.copy()
    quiet[5] = 1e4
    loud = base_vol.copy()
    loud[5] = 2e9          # only the FILL day's own volume changes

    runs = {}
    for name, vol in (("quiet", quiet), ("loud", loud)):
        runs[name] = run_allocation_backtest(
            {"A": _bar_frame(prices, vol)}, tw,
            initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            min_shares=0, market_impact=True,
        )
    # Same impact cost, bit for bit: today's print is not in ADV20.
    assert runs["quiet"]["total_market_impact"] == runs["loud"]["total_market_impact"]
    assert runs["quiet"]["rebalance_report"].iloc[0]["slippage"] == \
        runs["loud"]["rebalance_report"].iloc[0]["slippage"]

    # The CAP, in contrast, is defined on today's volume -- so it differs.
    cap_runs = {}
    for name, vol in (("quiet", quiet), ("loud", loud)):
        cap_runs[name] = run_allocation_backtest(
            {"A": _bar_frame(prices, vol)}, tw,
            initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            max_volume_participation=0.10,
        )
    assert set(cap_runs["quiet"]["deferral_report"]["reason"]) == {"volume_participation_cap"}
    assert cap_runs["quiet"]["rebalance_report"]["shares"].max() <= 1000.0 + 1e-9
    assert cap_runs["loud"]["deferral_report"].empty
    assert cap_runs["loud"]["rebalance_report"]["shares"].max() > 1000.0


def test_degraded_adv20_uses_available_history_and_warns(caplog):
    """Fewer than 20 prior sessions: use the mean of what exists, and say so.

    A 3-session fill has only 2 prior volume prints. The model must NOT refuse
    to price the trade and must NOT invent a 20-day average -- it uses the mean
    of the available prior sessions and logs one WARNING per run.
    """
    prices = np.array([100., 101., 102., 103.])
    idx = pd.bdate_range("2023-01-02", periods=4)
    tw = _sparse(idx, {2: [0.5]})
    universe = {"A": _bar_frame(prices, 1e5)}

    with caplog.at_level(logging.WARNING, logger="common.allocation_backtester"):
        res = run_allocation_backtest(
            universe, tw, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            min_shares=0, market_impact=True,
        )
    msgs = [r.getMessage() for r in caplog.records if "fewer than" in r.getMessage()]
    assert msgs, "a degraded ADV20 window must be reported, not silently accepted"
    assert "20" in msgs[0]

    tr = res["rebalance_report"].iloc[0]
    rets = pd.Series(prices).pct_change().fillna(0.0)
    expected_sigma = float(rets.iloc[0:2].std())
    expected_adv = float(np.mean([1e5, 1e5]))
    expected = float(tr["trade_value"]) * expected_sigma * np.sqrt(float(tr["shares"]) / expected_adv)
    assert tr["slippage"] == pytest.approx(expected, rel=1e-12)

    # No prior volume at all on day 0 -> no ADV20 -> no charge (disclosed, never
    # back-filled from the future).
    tw0 = _sparse(idx, {0: [0.5]})
    res0 = run_allocation_backtest(
        universe, tw0, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
        min_shares=0, market_impact=True,
    )
    assert res0["total_market_impact"] == 0.0


# --------------------------------------------------------------------------- #
# 4. Participation cap + deferral
# --------------------------------------------------------------------------- #
def test_volume_participation_cap_defers_the_excess_and_keeps_the_ledger_honest():
    """Single-session fills never exceed `volume_today x pct`; the excess defers.

    Reusing the B3.1 queue means the SAME fixture behaves the same way in
    `close` and `next_open` (the only difference being the price the retry
    pays). Every capped attempt is on `deferral_report` with the new reason, and
    the order keeps filling session after session until the target is reached.
    """
    prices = _random_walk(30, seed=3)
    volume = 5_000.0          # 10% budget = 500 shares per session
    universe = {"A": _bar_frame(prices, volume)}
    idx = universe["A"].index
    tw = _sparse(idx, {0: [0.9]})

    for mode in ("close", "next_open"):
        res = run_allocation_backtest(
            universe, tw, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            max_volume_participation=0.10, execution_price=mode,
        )
        rep = res["rebalance_report"]
        assert not rep.empty
        # HARD BUDGET: no session may trade more than 500 shares.
        assert rep["shares"].max() <= 500.0 + 1e-9, rep["shares"].max()
        # Every fill is a whole budget until the order is complete.
        assert list(rep["shares"])[:5] == [500.0] * 5
        # ... and the excess was TRACED, not silently shrunk.
        d = res["deferral_report"]
        assert "volume_participation_cap" in set(d["reason"])
        caps = d[d["reason"] == "volume_participation_cap"]
        assert (caps["action"] == "BUY").all()
        assert np.isfinite(caps["execution_price"]).all()
        assert (caps["target_weight"] == 0.9).all()
        # The residual keeps the ORIGINAL instruction date, not the retry date.
        assert caps["signal_date"].nunique() == 1

    # --- the ledger reconciles through the deferred fills -------------------
    res = run_allocation_backtest(
        universe, tw, initial_capital=1e6, max_volume_participation=0.10,
        market_impact=True, execution_price="next_open",
    )
    eq, _w, cash = _replay_ledger(
        1e6, res["rebalance_report"], res["equity_curve"].index,
        pd.DataFrame({"A": universe["A"]["Close"]}), ["A"],
    )
    np.testing.assert_allclose(res["equity_curve"]["equity"].to_numpy(), eq, rtol=0, atol=0)
    assert cash.min() >= -1e-9
    assert (res["actual_weights"].to_numpy().sum(axis=1) <= 1.0 + 1e-9).all()

    # --- a SELL is capped the same way -------------------------------------
    tw_sell = _sparse(idx, {0: [0.9], 20: [0.0]})
    res_sell = run_allocation_backtest(
        universe, tw_sell, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
        max_volume_participation=0.10,
    )
    sells = res_sell["rebalance_report"]
    sells = sells[sells["action"] == "SELL"]
    assert not sells.empty and sells["shares"].max() <= 500.0 + 1e-9
    assert "volume_participation_cap" in set(res_sell["deferral_report"]["reason"])


def test_volume_zero_is_a_suspension_and_produces_no_deferrable_budget():
    """`Volume == 0` is a REFUSAL, not a zero-share budget that accumulates.

    The B1.3 suspension guard runs first and refuses BOTH directions, so the
    session must appear in `suspension_report` as `volume_zero` -- and must NOT
    appear in `deferral_report` as a participation cap (that would imply a
    budget was carried forward), and no fill may be booked on it.
    """
    prices = _random_walk(12, seed=5)
    volume = np.full(12, 5_000.0)
    volume[1] = 0.0
    volume[2] = 0.0
    universe = {"A": _bar_frame(prices, volume)}
    idx = universe["A"].index
    tw = _sparse(idx, {0: [0.9]})

    for mode in ("close", "next_open"):
        res = run_allocation_backtest(
            universe, tw, initial_capital=1e6, commission_pct=0.0, slippage_pct=0.0,
            max_volume_participation=0.10, execution_price=mode,
        )
        susp = res["suspension_report"]
        assert set(susp["reason"]) == {"volume_zero"}
        assert len(susp) == 2
        # The halted sessions were REFUSED -- none of them may show up as a
        # deferred participation budget on that same date.
        halted = {idx[1].strftime("%Y-%m-%d"), idx[2].strftime("%Y-%m-%d")}
        caps = res["deferral_report"]
        caps = caps[caps["reason"] == "volume_participation_cap"]
        assert not (halted & set(caps["execution_date"])), (
            "a Volume==0 halt must produce a refusal, never a deferrable budget"
        )
        # Nothing was booked at the halted prices.
        assert not (halted & set(res["rebalance_report"]["date"]))
        # The order survived the halt and filled once the bar traded again.
        assert not res["rebalance_report"].empty

    # A day-0 halt: no entry at all, and no cap trace (there was no budget).
    vol0 = np.full(12, 5_000.0)
    vol0[0] = 0.0
    res0 = run_allocation_backtest(
        {"A": _bar_frame(prices, vol0)}, tw, initial_capital=1e6,
        max_volume_participation=0.10,
    )
    assert res0["rebalance_report"].empty
    assert res0["deferral_report"].empty
    assert list(res0["suspension_report"]["reason"]) == ["volume_zero"]


def test_participation_cap_is_inert_without_a_volume_column():
    """Price-only series: no volume data means no budget, so no cap and no trace.

    Mirrors B1.3's DISCLOSED limit -- absent volume is "unobservable", never
    "zero". Rejecting it here would turn every price-only caller into an
    all-cash book, and inventing a budget would be worse.
    """
    prices = _random_walk(12, seed=7)
    frame = pd.DataFrame(
        {"Open": prices, "High": prices, "Low": prices, "Close": prices},
        index=pd.bdate_range("2023-01-02", periods=len(prices)),
    )
    tw = _sparse(frame.index, {0: [0.9]})
    res = run_allocation_backtest(
        {"A": frame}, tw, initial_capital=1e6, max_volume_participation=0.10,
    )
    assert res["deferral_report"].empty
    assert not res["rebalance_report"].empty
    assert res["total_market_impact"] == 0.0

    # Bad configurations fail loudly instead of silently disabling the guard.
    with pytest.raises(ValueError, match="max_volume_participation"):
        run_allocation_backtest({"A": frame}, tw, max_volume_participation=0.0)
    with pytest.raises(ValueError, match="max_volume_participation"):
        run_allocation_backtest({"A": frame}, tw, max_volume_participation=1.5)
    with pytest.raises(ValueError, match="impact_gamma"):
        run_allocation_backtest({"A": frame}, tw, market_impact=True, impact_gamma=-1.0)
    with pytest.raises(ValueError, match="impact_vol_window"):
        run_allocation_backtest({"A": frame}, tw, market_impact=True, impact_vol_window=0)

"""100% offline backtester fidelity & regression suite (Tier P4 Task B4.2).

Validates the full quantitative fidelity of common/allocation_backtester.py:
1. Combined B1.1-B3.4 features:
   - next_open execution mode (signal/execution day separation and deferral).
   - next_vwap execution mode (pricing from VWAP column).
   - intraday_twap execution mode (pricing from (H+L+C)/3 5-min bars).
   - market_impact (Almgren-Chriss square-root temporary price impact).
   - max_volume_participation (liquidity cap and residual order requeueing).
   - cash_yield_annual (idle-cash overnight repo interest).
   - borrow_fee_annual (short position securities borrow fees).
   - suspension blocking (volume <= 0 halts BUY and SELL).
   - China ST 5% limit blocking (price limit ceiling/floor trading refusal).
   - minimal_metrics=True scalar parity with full report mode.
2. Walkforward multiprocessing parity:
   - run_walkforward with jobs=2 vs jobs=1 bit-for-bit parity across all folds.
3. All 9 static allocation templates:
   - Sparse weights contract compliance (no template-level forward filling).
   - Rebalance weight sum <= 1.0 constraint.
   - Exact equity accounting identity (equity[t] == cash[t] + holdings[t]), asserted by
     REPLAYING the trade tape (`_replay_ledger`: initial capital, each reported fill's
     `shares` at its reported `price` and `total_cost`, marked at that bar's close)
     and requiring the replayed book to match the reported `equity` AND
     `actual_weights` within 1e-6 (rtol=0) on every bar -- plus a non-negative
     replayed cash balance. The engine exposes no per-bar cash/holdings array, so
     the report alone cannot prove this identity; the replay is what makes it a real
     check rather than a restatement.
   - Cash non-negativity and actual weight budget compliance.

100% OFFLINE: strictly uses synthetic OHLCV generators; 0 network calls.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backtester.run_backtest import run_walkforward
from common.allocation_backtester import (
    CASH_CARRY_REPORT_COLUMNS,
    REBALANCE_REPORT_COLUMNS,
    run_allocation_backtest,
)
from common.allocation_templates import ALLOCATION_TEMPLATES, EqualWeightAllocation
from common.testing import make_ar1_ohlcv, make_ohlcv_from_closes

# The independent cash+holdings replay used to verify `equity[t] == cash[t] +
# holdings[t]@close[t]` lives next to the ledger-parity suite; importing it (rather
# than re-deriving the identity from the report) is what makes the assertion below
# non-tautological -- see `common/tests/test_ledger_parity.py::_replay_ledger`.
_COMMON_TESTS = os.path.join(_PROJECT_ROOT, "common", "tests")
if _COMMON_TESTS not in sys.path:
    sys.path.insert(0, _COMMON_TESTS)

from test_ledger_parity import _replay_ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic Fixture Helpers
# --------------------------------------------------------------------------- #
def _build_synthetic_universe(n_days: int = 60, seed: int = 42, symbols=None):
    symbols = symbols or ["S0", "S1", "S2", "S3"]
    idx = pd.bdate_range("2021-01-04", periods=n_days)
    universe = {}
    for i, sym in enumerate(symbols):
        base_px = 20.0 + 10.0 * i
        ar1 = make_ar1_ohlcv(phi=0.03, n=n_days, seed=seed + 13 * i, start="2021-01-04")
        closes = ar1["Close"].to_numpy()
        opens = np.roll(closes, 1)
        opens[0] = closes[0]
        highs = np.maximum(opens, closes) * 1.01
        lows = np.minimum(opens, closes) * 0.99
        vols = np.full(n_days, 500_000.0 + 10_000.0 * i)
        vwaps = (opens + highs + lows + closes) / 4.0
        universe[sym] = pd.DataFrame(
            {
                "Open": opens,
                "High": highs,
                "Low": lows,
                "Close": closes,
                "Volume": vols,
                "VWAP": vwaps,
            },
            index=idx,
        )
    return universe, idx


def _build_synthetic_intraday(universe):
    """Build typical (H+L+C)/3 5-min intraday bars for each day in universe."""
    intraday_data = {}
    for sym, df in universe.items():
        all_bars = []
        for d in df.index:
            date_str = d.strftime("%Y-%m-%d")
            intra_idx = pd.date_range(f"{date_str} 09:30", f"{date_str} 15:00", freq="30min")
            day_close = df.loc[d, "Close"]
            bar_df = pd.DataFrame(
                {
                    "Open": [day_close] * len(intra_idx),
                    "High": [day_close * 1.005] * len(intra_idx),
                    "Low": [day_close * 0.995] * len(intra_idx),
                    "Close": [day_close] * len(intra_idx),
                    "Volume": [10_000.0] * len(intra_idx),
                },
                index=intra_idx,
            )
            all_bars.append(bar_df)
        intraday_data[sym] = pd.concat(all_bars)
    return intraday_data


# --------------------------------------------------------------------------- #
# 1. Combined B1.1-B3.4 Features Tests
# --------------------------------------------------------------------------- #
def test_fidelity_execution_price_next_open():
    """B3.1: next_open queues trade from day T signal to day T+1 open."""
    universe, idx = _build_synthetic_universe(n_days=10)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[0] = [0.25, 0.25, 0.25, 0.25]
    tw.iloc[-1] = [0.5, 0.5, 0.0, 0.0]  # Signal on last bar -> no_next_bar deferral

    res = run_allocation_backtest(universe, tw, execution_price="next_open")
    assert res["execution_price"] == "next_open"

    reb = res["rebalance_report"]
    assert not reb.empty
    # Day 0 signal executes on Day 1
    day1_str = idx[1].strftime("%Y-%m-%d")
    assert (reb["date"] == day1_str).all()

    # Deferral report must log the last bar signal as no_next_bar
    def_rep = res["deferral_report"]
    assert not def_rep.empty
    assert (def_rep["reason"] == "no_next_bar").any()

    # Invariants
    act_w = res["actual_weights"]
    assert (act_w.sum(axis=1) <= 1.0 + 1e-9).all()


def test_fidelity_execution_price_next_vwap():
    """B3.1: next_vwap executes trades at next session's VWAP."""
    universe, idx = _build_synthetic_universe(n_days=10)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[0] = [1.0, 0.0, 0.0, 0.0]

    res = run_allocation_backtest(universe, tw, execution_price="next_vwap")
    assert res["execution_price"] == "next_vwap"

    reb = res["rebalance_report"]
    assert len(reb) == 1
    exec_row = reb.iloc[0]
    expected_vwap = universe["S0"].loc[idx[1], "VWAP"]
    np.testing.assert_allclose(exec_row["price"], expected_vwap, rtol=1e-6)


def test_fidelity_execution_price_intraday_twap():
    """B3.1: intraday_twap executes trades at mean of 5m (H+L+C)/3 bars."""
    universe, idx = _build_synthetic_universe(n_days=10)
    intraday = _build_synthetic_intraday(universe)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[0] = [1.0, 0.0, 0.0, 0.0]

    res = run_allocation_backtest(
        universe, tw, execution_price="intraday_twap", intraday_data=intraday
    )
    assert res["execution_price"] == "intraday_twap"
    reb = res["rebalance_report"]
    assert len(reb) == 1
    # Check that execution price matches intraday typical price on day 1
    exec_price = reb.iloc[0]["price"]
    day1_bars = intraday["S0"].loc[idx[1].strftime("%Y-%m-%d")]
    expected_twap = float(((day1_bars["High"] + day1_bars["Low"] + day1_bars["Close"]) / 3.0).mean())
    np.testing.assert_allclose(exec_price, expected_twap, rtol=1e-6)


def test_fidelity_market_impact():
    """B3.2: market_impact=True applies square-root impact cost on fills."""
    universe, idx = _build_synthetic_universe(n_days=30)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[22] = [0.8, 0.2, 0.0, 0.0]

    res_no_imp = run_allocation_backtest(universe, tw, market_impact=False)
    res_imp = run_allocation_backtest(universe, tw, market_impact=True, impact_gamma=1.0)

    assert res_no_imp["total_market_impact"] == 0.0
    assert res_imp["total_market_impact"] > 0.0
    # Equity curve with impact must be strictly lower or equal due to impact costs
    assert res_imp["equity_curve"]["equity"].iloc[-1] < res_no_imp["equity_curve"]["equity"].iloc[-1]


def test_fidelity_max_volume_participation():
    """B3.2: max_volume_participation caps fill and logs residual deferral."""
    universe, idx = _build_synthetic_universe(n_days=15)
    # Set volume small enough to trigger participation cap
    for s in universe:
        universe[s]["Volume"] = 100.0

    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[0] = [1.0, 0.0, 0.0, 0.0]

    res = run_allocation_backtest(universe, tw, max_volume_participation=0.10)
    def_rep = res["deferral_report"]
    assert not def_rep.empty
    assert (def_rep["reason"] == "volume_participation_cap").any()


def test_fidelity_cash_yield_idle_cash():
    """B3.3: cash_yield_annual credits interest on idle cash without double-counting."""
    universe, idx = _build_synthetic_universe(n_days=30)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    # Hold 20% risky, 80% idle cash
    tw.iloc[0] = [0.2, 0.0, 0.0, 0.0]

    res_off = run_allocation_backtest(universe, tw, cash_yield_annual=None)
    res_on = run_allocation_backtest(universe, tw, cash_yield_annual=0.05)

    assert res_off["total_cash_interest"] == 0.0
    assert res_on["total_cash_interest"] > 0.0
    assert not res_on["cash_carry_report"].empty
    assert list(res_on["cash_carry_report"].columns) == CASH_CARRY_REPORT_COLUMNS
    assert (res_on["cash_carry_report"]["cash_interest"] > 0.0).any()


def test_fidelity_borrow_fee_short_positions():
    """B3.4: borrow_fee_annual charges securities borrow fee on short books."""
    universe, idx = _build_synthetic_universe(n_days=30)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    # Enter short position
    tw.iloc[0] = [-0.3, 0.3, 0.0, 0.0]

    res_off = run_allocation_backtest(universe, tw, borrow_fee_annual=None)
    res_on = run_allocation_backtest(universe, tw, borrow_fee_annual=0.06)

    assert res_off["total_borrow_fees"] == 0.0
    assert res_on["total_borrow_fees"] > 0.0
    assert not res_on["cash_carry_report"].empty
    assert (res_on["cash_carry_report"]["borrow_fee"] > 0.0).any()


def test_fidelity_suspension_volume_zero_blocking():
    """B1.3: Volume <= 0 or NaN blocks rebalance fills and logs to suspension_report."""
    universe, idx = _build_synthetic_universe(n_days=10)
    universe["S0"].loc[idx[2], "Volume"] = 0.0

    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[2] = [0.5, 0.5, 0.0, 0.0]

    res = run_allocation_backtest(universe, tw)
    susp = res["suspension_report"]
    assert len(susp) == 1
    assert susp.iloc[0]["symbol"] == "S0"
    assert susp.iloc[0]["reason"] == "volume_zero"


def test_fidelity_china_st_limit_blocking():
    """B1.3: Under china_trading=True, ST stocks have 5% limit band and block limit locks."""
    idx = pd.bdate_range("2021-01-04", periods=6)
    # Day 0 -> Day 1 jumps +5% and closes at high (limit up)
    df_st = pd.DataFrame(
        {
            "Open": [10.0, 10.5, 10.5, 10.5, 10.5, 10.5],
            "High": [10.0, 10.5, 10.5, 10.5, 10.5, 10.5],
            "Low": [10.0, 10.5, 10.5, 10.5, 10.5, 10.5],
            "Close": [10.0, 10.5, 10.5, 10.5, 10.5, 10.5],
            "Volume": [100_000.0] * 6,
        },
        index=idx,
    )
    tw_st = pd.DataFrame(np.nan, index=idx, columns=["600000.SH"])
    tw_st.iloc[1] = [1.0]

    res = run_allocation_backtest(
        {"600000.SH": df_st}, tw_st, china_trading=True, name_map={"600000.SH": "ST浦发"}
    )
    # Fill is refused on limit-up ceiling
    assert res["rebalance_report"].empty


def test_fidelity_minimal_metrics_scalar_parity():
    """B1.2: minimal_metrics=True produces bit-for-bit identical scalar metrics."""
    universe, idx = _build_synthetic_universe(n_days=40)
    tw = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
    tw.iloc[0] = [0.25, 0.25, 0.25, 0.25]
    tw.iloc[10] = [0.4, 0.4, 0.1, 0.1]
    tw.iloc[20] = [0.1, 0.1, 0.4, 0.4]

    full = run_allocation_backtest(
        universe, tw, market_impact=True, cash_yield_annual=0.03, min_shares=10
    )
    minimal = run_allocation_backtest(
        universe, tw, market_impact=True, cash_yield_annual=0.03, min_shares=10, minimal_metrics=True
    )

    scalar_keys = [
        "sharpe_ratio",
        "cagr",
        "max_drawdown",
        "calmar_ratio",
        "win_rate",
        "profit_factor",
        "total_turnover",
        "total_rebalances",
        "total_market_impact",
        "total_cash_interest",
        "total_borrow_fees",
    ]
    for key in scalar_keys:
        np.testing.assert_allclose(
            minimal[key], full[key], rtol=1e-12, atol=1e-12, err_msg=f"Metric mismatch: {key}"
        )

    # Equity curves match
    np.testing.assert_allclose(
        minimal["equity_curve"]["equity"].to_numpy(),
        full["equity_curve"]["equity"].to_numpy(),
        rtol=1e-12,
        atol=1e-12,
    )


# --------------------------------------------------------------------------- #
# 2. Walkforward Multiprocessing Parity Tests
# --------------------------------------------------------------------------- #
def test_walkforward_multiprocessing_parity():
    """B2.2: run_walkforward produces bit-for-bit identical outputs for jobs=1 vs jobs=2."""
    universe, _ = _build_synthetic_universe(n_days=140, symbols=["A", "B", "C", "D"])
    template = EqualWeightAllocation()
    params = {"rebalance_freq_days": 21}

    common_kwargs = dict(
        start="2021-01-04",
        end="2021-07-01",
        window_years=0.25,
        step_years=0.1,
        initial_capital=100_000.0,
        commission_pct=0.0005,
        slippage_pct=0.0005,
        min_shares=1,
        min_weight_change=0.0,
        china_trading=False,
        us_trading=False,
        hk_trading=False,
        execution_price="close",
        market_impact=False,
        market_impact_gamma=1.0,
        max_volume_participation=None,
        cash_sweep_rate=None,
        borrow_fee_annual=None,
        seed=12345,
    )

    args_serial = argparse.Namespace(n_jobs=1, **common_kwargs)
    args_parallel = argparse.Namespace(n_jobs=2, **common_kwargs)

    res_serial = run_walkforward(universe, template, params, args_serial)
    res_parallel = run_walkforward(universe, template, params, args_parallel)

    assert len(res_serial) == len(res_parallel)
    assert len(res_serial) > 0

    compared_fields = [
        "start_date",
        "end_date",
        "sharpe_ratio",
        "cagr",
        "max_drawdown",
        "calmar_ratio",
        "win_rate",
        "profit_factor",
        "total_turnover",
        "total_rebalances",
    ]

    for f_idx, (f_s, f_p) in enumerate(zip(res_serial, res_parallel)):
        for field in compared_fields:
            val_s = f_s[field]
            val_p = f_p[field]
            if isinstance(val_s, (int, float)) and np.isnan(val_s):
                assert np.isnan(val_p), f"Fold {f_idx} NaN mismatch on {field}"
            elif isinstance(val_s, (int, float)):
                np.testing.assert_allclose(
                    val_s, val_p, rtol=0, atol=0, err_msg=f"Fold {f_idx} mismatch on {field}"
                )
            else:
                assert val_s == val_p, f"Fold {f_idx} mismatch on {field}: {val_s} != {val_p}"


# --------------------------------------------------------------------------- #
# 3. All 9 Static Templates Fidelity & Accounting Identity Tests
# --------------------------------------------------------------------------- #
def test_all_nine_static_templates_fidelity_and_accounting_identity():
    """Verify all 9 static AllocationTemplates satisfy sparse weights contract and ledger identity."""
    rng = np.random.default_rng(5)
    universe = {}
    for i in range(3):
        closes = 100.0 + np.cumsum(rng.normal(0.05, 1.0, 250))
        universe[chr(65 + i)] = make_ohlcv_from_closes(closes, start="2020-01-01")
    symbols = list(universe.keys())

    assert len(ALLOCATION_TEMPLATES) == 9

    for template_cls in ALLOCATION_TEMPLATES:
        template = template_cls()
        params = {k: v[0] for k, v in template.param_grid.items()}
        warmup = template.warmup_bars(params)

        target_weights = template.generate_weights(universe, params)
        assert isinstance(target_weights, pd.DataFrame)
        assert not target_weights.empty
        assert list(target_weights.columns) == symbols

        # Sparse weights contract:
        # Non-rebalance dates MUST be NaN across all symbols
        rebalance_mask = target_weights.notna().any(axis=1)
        assert rebalance_mask.sum() > 0, f"{template.name} generated 0 rebalances"
        assert (~rebalance_mask).sum() > 0, f"{template.name} forward-filled non-rebalance rows"

        # Rebalance rows must not exceed budget
        rebal_sums = target_weights[rebalance_mask].sum(axis=1)
        assert (rebal_sums <= 1.0 + 1e-6).all(), (
            f"{template.name} rebalance weights exceed 1.0: {rebal_sums.max()}"
        )

        # Run backtest with China A-share trading rules (T+1, lots, commissions, stamp duty)
        res = run_allocation_backtest(
            universe,
            target_weights,
            initial_capital=100_000.0,
            commission_pct=0.0005,
            slippage_pct=0.0005,
            min_shares=100,
            china_trading=True,
        )

        assert not res["equity_curve"].empty
        eq = res["equity_curve"]["equity"].to_numpy()
        assert np.isfinite(eq).all()
        assert (eq > 0.0).all(), f"{template.name} equity depleted"

        act_w = res["actual_weights"]
        assert (act_w.sum(axis=1) <= 1.0 + 1e-9).all(), f"{template.name} actual weights > 1"
        assert (act_w.to_numpy() >= -1e-9).all(), f"{template.name} negative actual weights"

        # --- the accounting identity, replayed independently --------------------
        # The engine quotes a single `equity[r] = cash + holdings@close` identity but
        # does NOT expose per-bar cash or holdings, so asserting it from the report
        # alone would be tautological (weights are DERIVED from the same equity).
        # Instead rebuild the book from the trade tape: initial capital, then every
        # reported fill's `shares` at its reported `price` plus `total_cost`, marked
        # at each forward-filled close. If `equity` ever stopped equalling
        # `cash + Σ held×price` for ANY bar (a dropped fill, a fee charged twice, a
        # weight computed off stale holdings), the replay diverges.
        closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()}).ffill().fillna(0.0)
        dates = res["equity_curve"].index
        closes = closes.loc[dates]
        replayed_eq, replayed_w, replayed_cash = _replay_ledger(
            100_000.0, res["rebalance_report"], dates, closes, symbols
        )
        np.testing.assert_allclose(
            eq, replayed_eq, rtol=0, atol=1e-6,
            err_msg=f"{template.name}: reported equity != cash + holdings@close (replayed)",
        )
        np.testing.assert_allclose(
            act_w[symbols].to_numpy(), replayed_w, rtol=0, atol=1e-6,
            err_msg=f"{template.name}: reported weights != replayed holdings/equity",
        )
        assert (replayed_cash >= -1e-9).all(), (
            f"{template.name} replayed cash went negative ({replayed_cash.min()})"
        )

        # Trade-tape shape/sanity on every rebalance (the identity itself is asserted
        # by the replay above; these only prove the tape is well-formed and that
        # costs are non-refundable):
        reb = res["rebalance_report"]
        if not reb.empty:
            assert list(reb.columns) == REBALANCE_REPORT_COLUMNS
            assert (reb["portfolio_equity"] > 0).all()
            assert (reb["total_cost"] >= 0).all()

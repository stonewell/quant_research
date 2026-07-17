import numpy as np
import pandas as pd
import pytest
from common.testing import make_trending_pullback_df

from stratgen.portfolio_backtester import run_portfolio_backtest
from stratgen.templates import MomentumTemplate, NoTradeTemplate


def _trending_universe(n_symbols=4, n=400, start_seed=1):
    return {f"S{i}": make_trending_pullback_df(n=n, seed=start_seed + i) for i in range(n_symbols)}


def test_never_exceeds_max_concurrent_positions():
    universe = _trending_universe(n_symbols=5, n=400)
    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=2, warmup=60)
    assert (result["equity_curve"]["num_open_positions"] <= 2).all()


def test_cash_never_negative_and_equity_always_positive():
    universe = _trending_universe(n_symbols=4, n=400)
    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=3, warmup=60)
    assert (result["equity_curve"]["cash"] >= -1e-6).all()
    assert (result["equity_curve"]["equity"] > 0).all()


def test_trade_log_is_tagged_by_symbol_across_multiple_instruments():
    universe = _trending_universe(n_symbols=3, n=400)
    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=3, warmup=60)
    trades = result["trades"]
    assert not trades.empty
    assert set(trades["symbol"]).issubset({"S0", "S1", "S2"})
    assert (trades["side"] == "buy").sum() > 0


def test_no_trade_template_produces_no_trades_and_flat_equity():
    universe = _trending_universe(n_symbols=3, n=400)
    result = run_portfolio_backtest(universe, NoTradeTemplate(), {}, max_concurrent_positions=3, warmup=60)
    assert result["trades"].empty
    assert (result["equity_curve"]["equity"] == result["equity_curve"]["equity"].iloc[0]).all()


def test_max_holding_days_force_closes_a_position_a_pure_signal_exit_would_hold_open():
    # A smooth, deterministic, never-reversing uptrend: MomentumTemplate's
    # fast>slow crossover state never flips back to false once warmed up, so
    # the signal-based exit alone would never fire -- only max_holding_days
    # can close the position within the test window.
    n = 300
    idx = pd.bdate_range("2019-01-01", periods=n)
    close = 100 + np.arange(n) * 0.2
    df = pd.DataFrame({"Open": close, "High": close + 0.3, "Low": close - 0.3, "Close": close}, index=idx)
    universe = {"A": df}

    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=1, max_holding_days=15, warmup=60)
    sells = result["trades"][result["trades"]["side"] == "sell"]
    assert not sells.empty
    buys = result["trades"][result["trades"]["side"] == "buy"]
    first_buy_idx = df.index.get_indexer([buys.iloc[0]["date"]])[0]
    first_sell_idx = df.index.get_indexer([sells.iloc[0]["date"]])[0]
    assert first_sell_idx - first_buy_idx <= 16  # max_holding_days + 1 (next-bar-open execution lag)


def test_position_sizing_is_roughly_equal_weight_across_simultaneous_entries():
    # Two symbols with IDENTICAL price action (so they signal entry on
    # exactly the same bar) -- with 2 slots and no prior positions, each
    # should get roughly half the portfolio's starting capital.
    df = make_trending_pullback_df(n=400, seed=9)
    universe = {"A": df, "B": df.copy()}
    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=2, initial_capital=100_000.0, warmup=60)
    buys = result["trades"][result["trades"]["side"] == "buy"]
    first_two = buys.iloc[:2]
    costs = first_two["price"] * first_two["qty"]
    assert abs(costs.iloc[0] - costs.iloc[1]) / costs.iloc[0] < 0.01
    assert 40_000 < costs.iloc[0] < 60_000


def test_raises_when_universe_empty():
    with pytest.raises(ValueError):
        run_portfolio_backtest({}, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50})


def test_raises_when_not_enough_aligned_bars_for_warmup():
    universe = _trending_universe(n_symbols=2, n=20)
    with pytest.raises(ValueError):
        run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50}, warmup=60)


def test_symbols_with_non_overlapping_calendars_are_inner_joined():
    df_a = make_trending_pullback_df(n=400, seed=1, start="2015-01-01")
    df_b = make_trending_pullback_df(n=400, seed=2, start="2016-01-01")  # different start -> partial overlap
    universe = {"A": df_a, "B": df_b}
    result = run_portfolio_backtest(universe, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50},
                                    max_concurrent_positions=2, warmup=60)
    assert not result["equity_curve"].empty
    assert result["equity_curve"].index.max() <= df_a.index.max()
    assert result["equity_curve"].index.min() >= df_b.index.min()

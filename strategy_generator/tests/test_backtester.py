import numpy as np
import pandas as pd
import pytest
from common.testing import make_trending_pullback_df

from stratgen.backtester import run_backtest
from stratgen.templates import (
    MeanReversionTemplate,
    MomentumTemplate,
    NoTradeTemplate,
    TurnOfMonthTemplate,
    VolGatedMomentumTemplate,
)


def test_momentum_backtest_runs_and_never_lets_cash_go_negative():
    df = make_trending_pullback_df()
    result = run_backtest(df, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50}, warmup=60)
    assert not result["equity_curve"].empty
    assert (result["equity_curve"]["cash"] >= -1e-6).all()
    assert (result["equity_curve"]["equity"] > 0).all()


def test_mean_reversion_backtest_trades_on_oscillating_market():
    df = make_trending_pullback_df(seed=3)
    result = run_backtest(df, MeanReversionTemplate(rsi_period=2), {"entry_threshold": 20, "exit_threshold": 70}, warmup=30)
    assert (result["trades"]["side"] == "sell").sum() > 0


def test_no_trade_template_produces_no_trades_and_flat_equity():
    df = make_trending_pullback_df(seed=5)
    result = run_backtest(df, NoTradeTemplate(), {}, warmup=30)
    assert result["trades"].empty
    assert (result["equity_curve"]["equity"] == result["equity_curve"]["equity"].iloc[0]).all()


def test_stop_loss_triggers_and_caps_loss():
    # A smooth, deterministic uptrend puts the fast MA above the slow MA
    # almost immediately once both are warmed up, so entry happens early
    # (well before bar 80). A single bar with an extreme low wick well after
    # that intrabar-triggers the stop BEFORE the momentum exit signal (which
    # needs a full extra bar to execute) gets any chance to preempt it --
    # this makes the stop-loss path deterministic rather than data-dependent.
    n = 300
    idx = pd.bdate_range("2018-01-01", periods=n)
    close = 100 + np.arange(n) * 0.1
    high, low = close + 0.3, close - 0.3
    low = low.copy()
    low[80] = close[80] - 20  # far below any plausible ATR-based stop
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)

    result = run_backtest(df, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50}, warmup=60)
    sells = result["trades"][result["trades"]["side"] == "sell"]
    stop_losses = sells[sells["event"] == "stop_loss"]
    assert not stop_losses.empty
    assert (stop_losses["pnl"] < 0).all()


def test_raises_when_not_enough_bars_for_warmup():
    df = make_trending_pullback_df(n=20, seed=1)
    with pytest.raises(ValueError):
        run_backtest(df, MomentumTemplate(), {"fast_ma": 10, "slow_ma": 50}, warmup=60)


def test_turn_of_month_backtest_trades_repeatedly_and_never_lets_cash_go_negative():
    df = make_trending_pullback_df(n=500, seed=9)
    result = run_backtest(df, TurnOfMonthTemplate(),
                          {"entry_days_before_month_end": 1, "exit_trading_day_of_month": 3}, warmup=30)
    sells = result["trades"][result["trades"]["side"] == "sell"]
    assert len(sells) > 5  # ~500 trading days / ~21 per month => should fire most months
    assert (result["equity_curve"]["cash"] >= -1e-6).all()
    assert (result["equity_curve"]["equity"] > 0).all()


def test_vol_gated_momentum_backtest_runs_and_never_lets_cash_go_negative():
    df = make_trending_pullback_df(n=600, seed=10)
    result = run_backtest(df, VolGatedMomentumTemplate(), {"vol_lookback": 20, "vol_percentile": 90}, warmup=260)
    assert not result["equity_curve"].empty
    assert (result["equity_curve"]["cash"] >= -1e-6).all()
    assert (result["equity_curve"]["equity"] > 0).all()

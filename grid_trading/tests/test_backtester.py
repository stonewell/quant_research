import numpy as np
import pandas as pd
from common.testing import make_oscillating_df

from gridbot.backtester import run_backtest
from gridbot.config import GridConfig


def test_backtest_runs_and_trades_on_oscillating_market():
    df = make_oscillating_df()
    config = GridConfig(warmup_bars=120, trend_ma_period=100, atr_period=14, grid_levels_per_side=4)
    result = run_backtest(df, config)

    equity_curve, trades = result["equity_curve"], result["trades"]
    assert not equity_curve.empty
    assert (equity_curve["equity"] > 0).all()
    # A range-bound, oscillating market should generate at least a few round trips.
    assert (trades["side"] == "sell").sum() > 0


def test_backtest_never_lets_cash_go_negative():
    df = make_oscillating_df(seed=3)
    config = GridConfig(warmup_bars=120, initial_capital=20_000.0)
    result = run_backtest(df, config)
    assert (result["equity_curve"]["cash"] >= 0).all()


def test_drawdown_stop_caps_losses_in_a_crash():
    n = 300
    idx = pd.bdate_range("2020-01-01", periods=n)
    # Range-bound for warmup, then a relentless crash to force the circuit breaker.
    flat = 100.0 + np.zeros(150)
    crash = 100.0 - np.linspace(0, 60, 150)
    close = np.concatenate([flat, crash])
    high = close + 0.5
    low = close - 0.5
    open_ = close
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)

    config = GridConfig(warmup_bars=120, drawdown_stop_pct=0.10, trend_ma_period=100)
    result = run_backtest(df, config)
    equity_curve = result["equity_curve"]
    # Equity should never have drawn down far beyond the configured stop threshold.
    assert equity_curve["drawdown"].max() < config.drawdown_stop_pct + 0.05

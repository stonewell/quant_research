import numpy as np
import pandas as pd

from rsibot.backtester import run_backtest
from rsibot.config import RSIConfig


def make_oscillating_df(n=400, base=100.0, amplitude=8.0, noise=0.3, seed=7):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base + amplitude * np.sin(t / 10.0) + rng.normal(0, noise, n)
    high = close + np.abs(rng.normal(0.5, 0.2, n))
    low = close - np.abs(rng.normal(0.5, 0.2, n))
    open_ = close + rng.normal(0, 0.1, n)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def test_backtest_runs_and_trades_on_oscillating_market():
    df = make_oscillating_df()
    config = RSIConfig(warmup_bars=60, require_trend_filter=False, rsi_period=2, oversold_threshold=15)
    result = run_backtest(df, config)

    equity_curve, trades = result["equity_curve"], result["trades"]
    assert not equity_curve.empty
    assert (equity_curve["equity"] > 0).all()
    assert (trades["side"] == "sell").sum() > 0


def test_backtest_never_lets_cash_go_negative():
    df = make_oscillating_df(seed=3)
    config = RSIConfig(warmup_bars=60, require_trend_filter=False, initial_capital=20_000.0)
    result = run_backtest(df, config)
    assert (result["equity_curve"]["cash"] >= -1e-6).all()


def test_stop_loss_caps_single_trade_loss():
    n = 300
    idx = pd.bdate_range("2020-01-01", periods=n)
    flat = 100.0 + np.zeros(100)
    dip = 100.0 - np.concatenate([np.linspace(0, 2, 5), np.full(95, 2.0)])       # brief dip triggers entry
    crash = dip[-1] - np.linspace(0, 40, 100)                                     # then a hard crash
    close = np.concatenate([flat, dip, crash])
    high = close + 0.3
    low = close - 0.3
    open_ = close
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)

    config = RSIConfig(warmup_bars=60, require_trend_filter=False, rsi_period=2, oversold_threshold=20,
                        stop_loss_pct=0.05, max_holding_days=None, exit_rsi_threshold=99)
    result = run_backtest(df, config)
    trades = result["trades"]
    sells = trades[trades["side"] == "sell"]
    assert (sells["event"] == "stop_loss").any()
    stop_loss_trades = sells[sells["event"] == "stop_loss"]
    # A 5% stop should not let any single trade lose dramatically more than that (after costs/slippage slop).
    entry_prices = trades[trades["side"] == "buy"]["price"].values
    for pnl, entry_price, qty in zip(stop_loss_trades["pnl"], entry_prices[: len(stop_loss_trades)], stop_loss_trades["qty"]):
        loss_pct = -pnl / (entry_price * qty)
        assert loss_pct < 0.08


def test_max_holding_days_forces_exit():
    n = 250
    idx = pd.bdate_range("2020-01-01", periods=n)
    # Flat, then a brief oversold dip that never recovers (RSI never crosses back above exit threshold).
    closes = np.concatenate([np.full(100, 100.0), np.full(5, 95.0), np.full(145, 95.0)])
    high = closes + 0.3
    low = closes - 0.3
    df = pd.DataFrame({"Open": closes, "High": high, "Low": low, "Close": closes}, index=idx)

    config = RSIConfig(warmup_bars=60, require_trend_filter=False, rsi_period=2, oversold_threshold=50,
                        exit_rsi_threshold=99, max_holding_days=10, stop_loss_pct=None)
    result = run_backtest(df, config)
    trades = result["trades"]
    sells = trades[trades["side"] == "sell"]
    assert not sells.empty
    assert (sells["event"] == "max_holding_days").any()

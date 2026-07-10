import numpy as np
import pandas as pd
import pytest
from common.testing import make_trending_pullback_df

from swingbot.backtester import run_backtest
from swingbot.config import SwingConfig


def test_backtest_runs_and_trades_in_a_trending_pullback_market():
    df = make_trending_pullback_df()
    config = SwingConfig(warmup_bars=210)
    result = run_backtest(df, config)

    equity_curve, trades = result["equity_curve"], result["trades"]
    assert not equity_curve.empty
    assert (equity_curve["equity"] > 0).all()
    assert (trades["side"] == "sell").sum() > 0


def test_equity_pct_sizing_at_full_allocation_still_enters_trades():
    # Regression: position_size_pct=1.0 previously left no room for fees, so
    # cost > cash on every attempted entry and the strategy silently traded zero times.
    df = make_trending_pullback_df()
    config = SwingConfig(warmup_bars=210, sizing_mode="equity_pct", position_size_pct=1.0)
    result = run_backtest(df, config)
    assert (result["trades"]["side"] == "buy").sum() > 0
    assert (result["equity_curve"]["cash"] >= -1e-6).all()


def test_backtest_never_lets_cash_go_negative():
    df = make_trending_pullback_df(seed=3)
    config = SwingConfig(warmup_bars=210, initial_capital=20_000.0)
    result = run_backtest(df, config)
    assert (result["equity_curve"]["cash"] >= -1e-6).all()


def test_no_trade_exceeds_max_holding_days():
    df = make_trending_pullback_df(seed=11, n=700)
    config = SwingConfig(warmup_bars=210, max_holding_days=63)
    result = run_backtest(df, config)
    trades = result["trades"]
    buys = trades[trades["side"] == "buy"].reset_index(drop=True)
    sells = trades[trades["side"] == "sell"].reset_index(drop=True)
    n = min(len(buys), len(sells))
    assert n > 0
    holding_bars = (pd.to_datetime(sells["date"][:n]) - pd.to_datetime(buys["date"][:n])).dt.days
    # calendar days is a loose upper bound on trading-bar holding period; generous multiplier for weekends
    assert (holding_bars <= config.max_holding_days * 1.6).all()


def test_stop_loss_caps_single_trade_loss():
    # Dip/crash must land AFTER warmup_bars=210 or the entry opportunity is skipped entirely.
    n = 500
    idx = pd.bdate_range("2018-01-01", periods=n)
    uptrend = 100 + np.arange(300) * 0.1
    dip = uptrend[-1] - np.linspace(0, 3, 10)
    crash = dip[-1] - np.linspace(0, 30, 190)
    close = np.concatenate([uptrend, dip, crash])
    high = close + 0.3
    low = close - 0.3
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)

    config = SwingConfig(warmup_bars=210, require_rising_trend_ma=False, entry_rsi_threshold=60,
                          stop_loss_pct=0.05, use_trailing_stop=False, max_holding_days=None)
    result = run_backtest(df, config)
    sells = result["trades"][result["trades"]["side"] == "sell"]
    stop_losses = sells[sells["event"] == "stop_loss"]
    assert not stop_losses.empty
    assert (stop_losses["pnl_pct"] > -0.07).all()  # 5% stop plus slippage/commission slop


def test_profit_target_exits_at_target_price():
    # A rising close's 5-period RSI pins at exactly 100 within a few bars of
    # zero down-days, which would trip the RSI mean-reversion exit before a
    # gradual rally could reach a 15% profit target. To test the profit-target
    # path in isolation, give the bars right after entry a large intrabar High
    # wick well above the target -- since the target is checked intrabar on
    # the very bar it's reached, it fires before any close-based RSI exit
    # (which only executes at the following bar's open) gets a chance to.
    n = 500
    idx = pd.bdate_range("2018-01-01", periods=n)
    uptrend = 100 + np.arange(300) * 0.1
    dip = uptrend[-1] - np.linspace(0, 8, 15)  # deep enough to clearly break below the 20-day MA
    rally_close = dip[-1] + np.linspace(0, 5, 185)
    close = np.concatenate([uptrend, dip, rally_close])
    high = close + 0.3
    low = close - 0.3
    high[300 + 15: 300 + 15 + 5] = dip[-1] * 1.20  # big high wick right after the dip trough
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)

    config = SwingConfig(warmup_bars=210, require_rising_trend_ma=False, entry_rsi_threshold=60,
                          stop_loss_pct=0.05, reward_risk_ratio=3.0, use_trailing_stop=False, max_holding_days=None)
    result = run_backtest(df, config)
    sells = result["trades"][result["trades"]["side"] == "sell"]
    targets = sells[sells["event"] == "profit_target"]
    assert not targets.empty
    assert (targets["pnl_pct"] > 0.10).all()  # 3:1 on a 5% stop should be roughly +15%


def test_risk_based_sizing_matches_formula():
    df = make_trending_pullback_df(seed=21)
    config = SwingConfig(warmup_bars=210, sizing_mode="risk_based", risk_per_trade_pct=0.01, stop_loss_pct=0.05,
                          max_position_pct_of_equity=0.99)
    result = run_backtest(df, config)
    buys = result["trades"][result["trades"]["side"] == "buy"]
    assert not buys.empty
    first_buy = buys.iloc[0]
    expected_qty = (config.risk_per_trade_pct * config.initial_capital) / (first_buy["price"] * config.stop_loss_pct)
    assert first_buy["qty"] == pytest.approx(expected_qty, rel=1e-2)

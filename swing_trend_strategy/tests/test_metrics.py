import numpy as np
import pandas as pd
import pytest

from swingbot import metrics


def test_total_return_and_cagr_doubling_in_one_year():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    equity = pd.Series(np.linspace(100_000, 200_000, 252), index=idx)
    assert metrics.total_return(equity) == pytest.approx(1.0)
    assert metrics.cagr(equity, periods_per_year=252) == pytest.approx(1.0, rel=0.05)


def test_max_drawdown_simple_vshape():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0, 130.0])
    assert metrics.max_drawdown(equity) == pytest.approx(0.25)


def test_win_rate_and_profit_factor():
    trades = pd.DataFrame([
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": 10.0, "pnl_pct": 0.1},
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": -5.0, "pnl_pct": -0.05},
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": 20.0, "pnl_pct": 0.2},
    ])
    assert metrics.win_rate(trades) == pytest.approx(2 / 3)
    assert metrics.profit_factor(trades) == pytest.approx(30.0 / 5.0)


def test_expectancy_matches_hand_calculation():
    # 2 wins of +10%, 1 loss of -5% -> win rate 2/3, avg win 10%, avg loss -5%
    # expectancy = (2/3)*10 + (1/3)*(-5) = 5.0
    trades = pd.DataFrame([
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": 10.0, "pnl_pct": 0.10},
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": 10.0, "pnl_pct": 0.10},
        {"side": "buy", "pnl": np.nan, "pnl_pct": np.nan},
        {"side": "sell", "pnl": -5.0, "pnl_pct": -0.05},
    ])
    stats = metrics.expectancy_stats(trades)
    assert stats["avg_win_pct"] == pytest.approx(10.0)
    assert stats["avg_loss_pct"] == pytest.approx(-5.0)
    assert stats["expectancy_pct"] == pytest.approx(5.0)


def test_avg_holding_days():
    trades = pd.DataFrame([
        {"side": "buy", "date": pd.Timestamp("2020-01-01")},
        {"side": "sell", "date": pd.Timestamp("2020-01-11")},   # 10 days
        {"side": "buy", "date": pd.Timestamp("2020-02-01")},
        {"side": "sell", "date": pd.Timestamp("2020-02-21")},   # 20 days
    ])
    assert metrics.avg_holding_days(trades) == pytest.approx(15.0)


def test_pct_time_in_market():
    equity_curve = pd.DataFrame({"in_position": [True, True, False, False, True]})
    assert metrics.pct_time_in_market(equity_curve) == pytest.approx(0.6)

import numpy as np
import pandas as pd
import pytest

from rsibot import metrics


def test_total_return_and_cagr_doubling_in_one_year():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")
    equity = pd.Series(np.linspace(100_000, 200_000, 252), index=idx)
    assert metrics.total_return(equity) == pytest.approx(1.0)
    assert metrics.cagr(equity, periods_per_year=252) == pytest.approx(1.0, rel=0.05)


def test_max_drawdown_simple_vshape():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0, 130.0])
    assert metrics.max_drawdown(equity) == pytest.approx(0.25)


def test_sharpe_ratio_zero_when_flat_returns():
    returns = pd.Series([0.0] * 30)
    assert metrics.sharpe_ratio(returns) == 0.0


def test_win_rate_and_profit_factor():
    trades = pd.DataFrame([
        {"side": "buy", "pnl": np.nan},
        {"side": "sell", "pnl": 10.0},
        {"side": "buy", "pnl": np.nan},
        {"side": "sell", "pnl": -5.0},
        {"side": "buy", "pnl": np.nan},
        {"side": "sell", "pnl": 20.0},
    ])
    assert metrics.win_rate(trades) == pytest.approx(2 / 3)
    assert metrics.profit_factor(trades) == pytest.approx(30.0 / 5.0)


def test_profit_factor_no_losses_is_infinite():
    trades = pd.DataFrame([{"side": "sell", "pnl": 10.0}, {"side": "sell", "pnl": 5.0}])
    assert metrics.profit_factor(trades) == float("inf")


def test_pct_time_in_market():
    equity_curve = pd.DataFrame({"in_position": [True, True, False, False, True]})
    assert metrics.pct_time_in_market(equity_curve) == pytest.approx(0.6)

import numpy as np
import pandas as pd
import pytest

from stratgen.metrics import (deflated_sharpe_ratio, expected_max_sharpe, max_drawdown, profit_factor,
                                sharpe_ratio, total_return, win_rate)


def test_total_return_doubling():
    equity = pd.Series([100_000, 200_000])
    assert total_return(equity) == pytest.approx(1.0)


def test_max_drawdown_simple_vshape():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0, 130.0])
    assert max_drawdown(equity) == pytest.approx(0.25)


def test_sharpe_ratio_zero_when_flat_returns():
    assert sharpe_ratio(pd.Series([0.0] * 30)) == 0.0


def test_win_rate_and_profit_factor():
    trades = pd.DataFrame([
        {"side": "sell", "pnl": 10.0}, {"side": "sell", "pnl": -5.0}, {"side": "sell", "pnl": 20.0},
    ])
    assert win_rate(trades) == pytest.approx(2 / 3)
    assert profit_factor(trades) == pytest.approx(30.0 / 5.0)


def test_expected_max_sharpe_is_zero_for_single_trial():
    assert expected_max_sharpe(1, sharpe_std=0.5) == 0.0


def test_expected_max_sharpe_increases_with_more_trials():
    values = [expected_max_sharpe(n, sharpe_std=0.3) for n in [2, 10, 50, 200]]
    assert values == sorted(values)


def test_deflated_sharpe_ratio_is_half_when_observed_equals_expected_max():
    sr0 = expected_max_sharpe(50, sharpe_std=0.3)
    dsr = deflated_sharpe_ratio(sr0, n_trials=50, n_obs=500, skewness=0.0, kurtosis=3.0, sharpe_std=0.3)
    assert dsr == pytest.approx(0.5, abs=1e-6)


def test_deflated_sharpe_ratio_reduces_to_classic_psr_for_single_trial():
    from scipy import stats
    dsr = deflated_sharpe_ratio(1.0, n_trials=1, n_obs=252, skewness=0.0, kurtosis=3.0)
    manual_psr = stats.norm.cdf(1.0 * np.sqrt(251))
    assert dsr == pytest.approx(manual_psr)


def test_deflated_sharpe_ratio_decreases_as_trials_increase():
    values = [deflated_sharpe_ratio(0.8, n_trials=n, n_obs=120, sharpe_std=0.4) for n in [1, 20, 100, 1000]]
    assert values == sorted(values, reverse=True)

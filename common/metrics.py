"""Shared performance metrics used across every project in this workspace:
the standard backtesting toolkit (CAGR, Sharpe/Sortino, max drawdown, win
rate, profit factor, time-in-market) plus the Deflated Sharpe Ratio (Bailey
& Lopez de Prado, 2014) for correcting a Sharpe ratio for having picked the
best of many search trials.

Each project's own `metrics.py` re-exports the subset it needs and keeps its
own `summarize()` (the exact dict shape differs per project/report) and any
project-specific extras (e.g. expectancy stats, average holding period)
local.
"""

import numpy as np
import pandas as pd
from scipy import stats

EULER_MASCHERONI = 0.5772156649


def total_return(equity: pd.Series) -> float:
    return equity.iloc[-1] / equity.iloc[0] - 1.0


def cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    n_periods = len(equity)
    if n_periods < 2:
        return 0.0
    growth = equity.iloc[-1] / equity.iloc[0]
    years = n_periods / periods_per_year
    if growth <= 0 or years <= 0:
        return -1.0
    return growth ** (1 / years) - 1.0


def annualized_vol(returns: pd.Series, periods_per_year: int = 252) -> float:
    return returns.std(ddof=1) * np.sqrt(periods_per_year)


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    excess = returns - risk_free / periods_per_year
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return (excess.mean() / std) * np.sqrt(periods_per_year)


def sortino_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    excess = returns - risk_free / periods_per_year
    downside = excess[excess < 0]
    dd_std = downside.std(ddof=1)
    if dd_std == 0 or np.isnan(dd_std):
        return 0.0
    return (excess.mean() / dd_std) * np.sqrt(periods_per_year)


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return dd.max()


def win_rate(trades: pd.DataFrame) -> float:
    sells = trades[trades["side"] == "sell"]
    if sells.empty:
        return 0.0
    return (sells["pnl"] > 0).mean()


def profit_factor(trades: pd.DataFrame) -> float:
    sells = trades[trades["side"] == "sell"]
    gains = sells.loc[sells["pnl"] > 0, "pnl"].sum()
    losses = -sells.loc[sells["pnl"] < 0, "pnl"].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def pct_time_in_market(equity_curve: pd.DataFrame) -> float:
    return equity_curve["in_position"].mean()


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Expected maximum Sharpe ratio achievable by pure luck across
    `n_trials` independent trials with cross-trial Sharpe std `sharpe_std`
    (Bailey & Lopez de Prado's Euler-Mascheroni approximation)."""
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return sharpe_std * ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int, n_obs: int,
                           skewness: float = 0.0, kurtosis: float = 3.0, sharpe_std: float = None) -> float:
    """Probability the true Sharpe ratio exceeds zero, after correcting for
    (a) having picked the best of `n_trials` independent trials and (b)
    non-Normal returns (skewness/kurtosis -- pass regular, not excess,
    kurtosis; 3.0 is the Normal-distribution value).

    `sharpe_std` is the cross-trial standard deviation of Sharpe ratios --
    ideally computed from the actual candidate Sharpes your search tried; if
    unavailable, `1/sqrt(n_obs)` is used as a rough default (the asymptotic
    std of a Sharpe ratio estimate under IID Normal returns).
    """
    if sharpe_std is None:
        sharpe_std = 1.0 / np.sqrt(max(n_obs, 2))
    sr0 = expected_max_sharpe(n_trials, sharpe_std)
    denom = np.sqrt(max(1e-12, 1 - skewness * observed_sharpe + ((kurtosis - 1) / 4) * observed_sharpe ** 2))
    z = (observed_sharpe - sr0) * np.sqrt(max(n_obs - 1, 1)) / denom
    return stats.norm.cdf(z)

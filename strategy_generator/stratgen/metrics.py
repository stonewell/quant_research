"""Standard performance metrics, plus the Deflated Sharpe Ratio (Bailey &
Lopez de Prado, 2014, J. Portfolio Management 40(5)) -- the single
best-corroborated statistical safeguard for an automated strategy-generation
pipeline specifically, because it models a SEARCH PROCESS (N independent
trials) rather than evaluating one fixed, pre-chosen strategy. A generator
that tries several templates x parameter grids x instruments is exactly that
search process, so a raw Sharpe ratio from the winning combination
overstates confidence unless deflated by how many trials were tried.
"""

import numpy as np
import pandas as pd
from scipy import stats

EULER_MASCHERONI = 0.5772156649


def total_return(equity: pd.Series) -> float:
    return equity.iloc[-1] / equity.iloc[0] - 1.0


def cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    n = len(equity)
    if n < 2:
        return 0.0
    growth = equity.iloc[-1] / equity.iloc[0]
    years = n / periods_per_year
    if growth <= 0 or years <= 0:
        return -1.0
    return growth ** (1 / years) - 1.0


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = 252) -> float:
    excess = returns - risk_free / periods_per_year
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return (excess.mean() / std) * np.sqrt(periods_per_year)


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return ((peak - equity) / peak).max()


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


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, periods_per_year: int = 252) -> dict:
    equity = equity_curve["equity"]
    returns = equity.pct_change().dropna()
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades
    return {
        "total_return_pct": total_return(equity) * 100,
        "cagr_pct": cagr(equity, periods_per_year) * 100,
        "sharpe_ratio": sharpe_ratio(returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": max_drawdown(equity) * 100,
        "num_trades": len(sells),
        "win_rate_pct": win_rate(trades) * 100 if not trades.empty else 0.0,
        "profit_factor": profit_factor(trades) if not trades.empty else 0.0,
        "final_equity": equity.iloc[-1],
    }


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

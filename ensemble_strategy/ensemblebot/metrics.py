"""Performance metrics for evaluating a backtested equity curve and trade log."""

import numpy as np
import pandas as pd


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


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, periods_per_year: int = 252) -> dict:
    equity = equity_curve["equity"]
    returns = equity.pct_change().dropna()
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades

    return {
        "total_return_pct": total_return(equity) * 100,
        "cagr_pct": cagr(equity, periods_per_year) * 100,
        "annualized_vol_pct": annualized_vol(returns, periods_per_year) * 100,
        "sharpe_ratio": sharpe_ratio(returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": max_drawdown(equity) * 100,
        "pct_time_in_market": pct_time_in_market(equity_curve) * 100,
        "num_trades": len(sells),
        "win_rate_pct": win_rate(trades) * 100 if not trades.empty else 0.0,
        "profit_factor": profit_factor(trades) if not trades.empty else 0.0,
        "final_equity": equity.iloc[-1],
    }

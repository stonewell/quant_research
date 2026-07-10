"""Performance metrics for evaluating a backtested equity curve and trade log.

Beyond the standard toolkit (re-exported from the shared `common/metrics.py`
module), this includes expectancy (win_rate * avg_win - loss_rate * avg_loss)
and average holding period, since the research behind this strategy
specifically warns that win rate alone doesn't establish profitability --
average win/loss size is what determines expectancy.
"""

import pandas as pd
from common.metrics import (annualized_vol, cagr, max_drawdown, pct_time_in_market, profit_factor, sharpe_ratio,
                              sortino_ratio, total_return, win_rate)


def expectancy_stats(trades: pd.DataFrame) -> dict:
    """avg win/loss size and per-trade expectancy, as % of that trade's cost basis."""
    sells = trades[trades["side"] == "sell"].copy()
    if sells.empty or "pnl_pct" not in sells.columns:
        return {"avg_win_pct": 0.0, "avg_loss_pct": 0.0, "expectancy_pct": 0.0}
    wins = sells.loc[sells["pnl"] > 0, "pnl_pct"]
    losses = sells.loc[sells["pnl"] <= 0, "pnl_pct"]
    wr = win_rate(trades)
    avg_win = wins.mean() if not wins.empty else 0.0
    avg_loss = losses.mean() if not losses.empty else 0.0
    expectancy = wr * avg_win + (1 - wr) * avg_loss
    return {"avg_win_pct": avg_win * 100, "avg_loss_pct": avg_loss * 100, "expectancy_pct": expectancy * 100}


def avg_holding_days(trades: pd.DataFrame) -> float:
    buys = trades[trades["side"] == "buy"].reset_index(drop=True)
    sells = trades[trades["side"] == "sell"].reset_index(drop=True)
    n = min(len(buys), len(sells))
    if n == 0:
        return 0.0
    durations = (pd.to_datetime(sells["date"][:n]) - pd.to_datetime(buys["date"][:n])).dt.days
    return durations.mean()


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, periods_per_year: int = 252) -> dict:
    equity = equity_curve["equity"]
    returns = equity.pct_change().dropna()
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades
    exp_stats = expectancy_stats(trades) if not trades.empty else {"avg_win_pct": 0.0, "avg_loss_pct": 0.0, "expectancy_pct": 0.0}

    return {
        "total_return_pct": total_return(equity) * 100,
        "cagr_pct": cagr(equity, periods_per_year) * 100,
        "annualized_vol_pct": annualized_vol(returns, periods_per_year) * 100,
        "sharpe_ratio": sharpe_ratio(returns, periods_per_year=periods_per_year),
        "sortino_ratio": sortino_ratio(returns, periods_per_year=periods_per_year),
        "max_drawdown_pct": max_drawdown(equity) * 100,
        "pct_time_in_market": pct_time_in_market(equity_curve) * 100,
        "num_trades": len(sells),
        "win_rate_pct": win_rate(trades) * 100 if not trades.empty else 0.0,
        "profit_factor": profit_factor(trades) if not trades.empty else 0.0,
        "avg_win_pct": exp_stats["avg_win_pct"],
        "avg_loss_pct": exp_stats["avg_loss_pct"],
        "expectancy_pct": exp_stats["expectancy_pct"],
        "avg_holding_calendar_days": avg_holding_days(trades) if not trades.empty else 0.0,
        "total_realized_pnl": sells["pnl"].sum() if not sells.empty else 0.0,
        "final_equity": equity.iloc[-1],
    }

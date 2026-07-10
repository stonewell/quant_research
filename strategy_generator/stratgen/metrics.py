"""Standard performance metrics, plus the Deflated Sharpe Ratio (Bailey &
Lopez de Prado, 2014, J. Portfolio Management 40(5)) -- the single
best-corroborated statistical safeguard for an automated strategy-generation
pipeline specifically, because it models a SEARCH PROCESS (N independent
trials) rather than evaluating one fixed, pre-chosen strategy. A generator
that tries several templates x parameter grids x instruments is exactly that
search process, so a raw Sharpe ratio from the winning combination
overstates confidence unless deflated by how many trials were tried.

All of the math here is re-exported from the shared `common/metrics.py`
module; `summarize` (this project's specific report shape) stays local.
"""

import pandas as pd
from common.metrics import cagr, deflated_sharpe_ratio, expected_max_sharpe, max_drawdown, profit_factor, \
    sharpe_ratio, total_return, win_rate


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

"""Performance metrics for evaluating a backtested equity curve and trade log.

Base metrics are re-exported from the shared `common/metrics.py` module;
`summarize` (this project's specific report shape) stays local.
"""

import pandas as pd
from common.metrics import (annualized_vol, cagr, max_drawdown, pct_time_in_market, profit_factor, sharpe_ratio,
                              sortino_ratio, total_return, win_rate)


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, periods_per_year: int = 252) -> dict:
    equity = equity_curve["equity"]
    returns = equity.pct_change().dropna()
    sells = trades[trades["side"] == "sell"] if not trades.empty else trades

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
        "avg_trade_pnl": sells["pnl"].mean() if not sells.empty else 0.0,
        "total_realized_pnl": sells["pnl"].sum() if not sells.empty else 0.0,
        "final_equity": equity.iloc[-1],
    }

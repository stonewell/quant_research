"""Portfolio Allocation Backtester

Simulates a portfolio tracking a set of target weights over time.
Accounts for:
1. Daily mark-to-market drift (as asset prices change, their actual weight in the portfolio drifts).
2. Rebalancing costs (commissions/slippage applied only to the turnover required to reach the new target weights).
"""

from typing import Dict

import numpy as np
import pandas as pd


def run_allocation_backtest(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
) -> dict:
    """
    Simulates portfolio equity curve given daily target weights.

    `target_weights` is a SPARSE DataFrame indexed by date, with columns for
    each symbol: a row is NaN except on an actual rebalance date, where it
    holds the target portfolio fraction (0.0 to 1.0) for that symbol. Do not
    pass an already-forward-filled frame -- a template can legitimately
    recompute the SAME target on consecutive rebalance dates (e.g.
    equal-weight always targets 1/N), and this backtester tells "a rebalance
    was instructed" apart from "no rebalance today" by whether the row is
    present at all, not by whether its value differs from the previous row.

    The backtester assumes trading happens at the CLOSE of the day a
    rebalance is instructed.
    """
    symbols = list(universe.keys())
    if not symbols or target_weights.empty:
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}

    # Extract aligned close prices
    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})

    # Ensure target_weights and closes are aligned
    common_idx = closes.index.intersection(target_weights.index)
    closes = closes.loc[common_idx]
    sparse_weights = target_weights.loc[common_idx, symbols]

    # A row with ANY non-NaN value is an explicit rebalance instruction for
    # that date -- computed BEFORE forward-filling, since forward-filling (or
    # a template recomputing an identical target) would otherwise erase the
    # one signal that tells "rebalanced to the same weight" apart from
    # "nothing happened".
    is_rebalance = sparse_weights.notna().any(axis=1).to_numpy()
    target_weights = sparse_weights.ffill().fillna(0.0)

    # Calculate daily returns for all assets
    returns = closes.pct_change().fillna(0.0)

    n_days = len(common_idx)

    # Arrays for fast simulation
    ret_arr = returns.values
    tgt_w_arr = target_weights.values

    # State tracking
    equity = np.zeros(n_days)
    equity[0] = initial_capital

    # Actual weights held at the END of the day (after drift and any rebalancing)
    actual_w = np.zeros_like(tgt_w_arr)

    total_turnover = 0.0
    cost_factor = commission_pct + slippage_pct

    # Day 0: Initial allocation
    actual_w[0] = tgt_w_arr[0]
    turnover = np.sum(np.abs(actual_w[0])) # From 0 to target
    equity[0] -= equity[0] * turnover * cost_factor
    total_turnover += turnover

    for t in range(1, n_days):
        # 1. Morning: Portfolio grows by the return of the assets held overnight
        # The return on day t applies to the weights held at the end of day t-1
        portfolio_return = np.sum(actual_w[t-1] * ret_arr[t])
        equity[t] = equity[t-1] * (1.0 + portfolio_return)

        # 2. Mid-day: Weights drift due to relative price changes
        # If an asset goes up more than the portfolio, its weight increases
        drifted_w = actual_w[t-1] * (1.0 + ret_arr[t]) / (1.0 + portfolio_return)

        # 3. End of day: rebalance only on a date the template actually
        # instructed one (is_rebalance), never inferred from a value change.
        if is_rebalance[t]:
            # Rebalance required
            turnover = np.sum(np.abs(tgt_w_arr[t] - drifted_w))
            # Deduct costs from equity
            equity[t] -= equity[t] * turnover * cost_factor
            total_turnover += turnover
            # Set new actual weights to the target
            actual_w[t] = tgt_w_arr[t]
        else:
            # No rebalance, actual weights are just the drifted weights
            actual_w[t] = drifted_w

    equity_df = pd.DataFrame(index=common_idx)
    equity_df["equity"] = equity

    # Reconstruct actual weights DataFrame for transparency
    actual_weights_df = pd.DataFrame(actual_w, index=common_idx, columns=symbols)

    # Compute additional performance metrics
    eq = equity_df["equity"]
    daily_returns = eq.pct_change().dropna()

    total_return = (eq.iloc[-1] / eq.iloc[0]) - 1.0 if len(eq) > 0 else 0.0
    n_years = max((common_idx[-1] - common_idx[0]).days / 365.25, 1.0 / 252.0) if len(common_idx) > 1 else 1.0
    cagr = ((1.0 + total_return) ** (1.0 / n_years)) - 1.0 if total_return > -1.0 else -1.0

    # Max Drawdown -- positive magnitude (e.g. 0.18 for an 18% drawdown),
    # matching common/metrics.py's max_drawdown() convention used elsewhere
    # in this workspace (backtester/run_backtest.py in particular reports
    # both side by side, so the sign must agree).
    cummax = eq.cummax()
    drawdown = (cummax - eq) / cummax
    max_dd = float(drawdown.max()) if not drawdown.empty else 0.0

    # Calmar Ratio
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    # Sharpe Ratio
    mean_ret = float(daily_returns.mean()) if not daily_returns.empty else 0.0
    std_ret = float(daily_returns.std()) if not daily_returns.empty else 0.0
    sr = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    # Win Rate & Profit Factor
    pos_ret = daily_returns[daily_returns > 0]
    neg_ret = daily_returns[daily_returns < 0]
    win_rate = len(pos_ret) / len(daily_returns) if len(daily_returns) > 0 else 0.0
    profit_factor = pos_ret.sum() / abs(neg_ret.sum()) if not neg_ret.empty and neg_ret.sum() != 0 else np.nan

    return {
        "equity_curve": equity_df,
        "actual_weights": actual_weights_df,
        "total_turnover": total_turnover,
        "total_rebalances": int(is_rebalance.sum()),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe_ratio": sr,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
    }

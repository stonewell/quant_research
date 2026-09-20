"""Portfolio Allocation Backtester

Simulates a portfolio tracking a set of target weights over time.
Accounts for:
1. Daily mark-to-market drift (as asset prices change, their actual weight in the portfolio drifts).
2. Rebalancing costs (commissions/slippage applied only to the turnover required to reach the new target weights).
"""

from typing import Dict

import numpy as np
import pandas as pd

from common.metrics import profit_factor_from_returns, win_rate_from_returns

REBALANCE_REPORT_COLUMNS = [
    "rebalance_id",
    "date",
    "symbol",
    "action",
    "price",
    "prior_weight",
    "target_weight",
    "weight_change",
    "trade_value",
    "shares",
    "prior_shares",
    "target_shares",
    "commission",
    "slippage",
    "total_cost",
    "portfolio_equity",
]


def run_allocation_backtest(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
    min_shares: int = 1,
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

    `min_shares` defines the minimal amount of shares trading each time
    (default: 1). Fractional share trading is not allowed; all traded share
    quantities and holdings are integer share amounts.
    """
    if not isinstance(min_shares, (int, np.integer)) or min_shares < 1:
        raise ValueError(f"min_shares must be an integer >= 1, got {min_shares!r}")

    symbols = list(universe.keys())
    if not symbols or target_weights.empty:
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}

    # Extract aligned close prices
    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})

    # Ensure target_weights and closes are aligned
    common_idx = closes.index.intersection(target_weights.index)
    if len(common_idx) == 0:
        # target_weights and the universe's OHLCV cover disjoint date ranges
        # (e.g. a template's rebalance dates were computed against a
        # different calendar). Without this check, `equity[0] = initial_capital`
        # below indexes into a zero-length array and raises IndexError.
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}
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

    # Tracking integer shares held for each symbol (no fractional shares allowed)
    held_shares = {sym: 0 for sym in symbols}

    trades = []
    rebalance_id = 0

    # Day 0: Initial allocation
    day0_trades = []
    day0_actual_w = np.zeros(len(symbols))
    for i, sym in enumerate(symbols):
        target_w = float(tgt_w_arr[0, i])
        if abs(target_w) > 1e-7:
            price = float(closes.iloc[0, i])
            if price > 0:
                raw_shares = (abs(target_w) * initial_capital) / price
                target_s = (int(raw_shares) // min_shares) * min_shares if min_shares > 1 else int(np.floor(raw_shares))
            else:
                target_s = 0

            if target_s >= min_shares:
                trade_s = target_s
                held_s = trade_s if target_w >= 0 else -trade_s
                held_shares[sym] = held_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                day0_actual_w[i] = target_w
                day0_trades.append({
                    "rebalance_id": 1,
                    "date": common_idx[0].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": "BUY" if target_w > 0 else "SELL",
                    "price": price,
                    "prior_weight": 0.0,
                    "target_weight": target_w,
                    "weight_change": target_w,
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": 0.0,
                    "target_shares": float(held_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": comm + slip,
                    "portfolio_equity": float(initial_capital),
                })

    if day0_trades:
        rebalance_id = 1
        trades.extend(day0_trades)
        actual_w[0] = day0_actual_w
        turnover = np.sum(np.abs(actual_w[0]))
        equity[0] -= equity[0] * turnover * cost_factor
        total_turnover += turnover
    else:
        actual_w[0] = np.zeros(len(symbols))

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
            pre_rebal_equity = float(equity[t])
            rebal_trades = []
            traded_symbols = set()

            for i, sym in enumerate(symbols):
                prior_s = held_shares[sym]
                prior_w = float(drifted_w[i])
                target_w = float(tgt_w_arr[t, i])
                delta_w = target_w - prior_w
                if abs(delta_w) <= 1e-7:
                    continue

                price = float(closes.iloc[t, i])
                if price <= 0:
                    continue

                if abs(target_w) < 1e-7 and prior_s != 0:
                    # Full liquidation allowed (closing out odd-lots)
                    trade_s = abs(prior_s)
                    action = "SELL" if prior_s > 0 else "BUY"
                    new_s = 0
                else:
                    raw_val = abs(delta_w) * pre_rebal_equity
                    raw_s = raw_val / price
                    trade_s = (int(raw_s) // min_shares) * min_shares if min_shares > 1 else int(np.floor(raw_s))
                    if trade_s < min_shares:
                        continue
                    action = "BUY" if delta_w > 0 else "SELL"
                    new_s = prior_s + (trade_s if delta_w > 0 else -trade_s)

                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                rebal_trades.append({
                    "rebalance_id": rebalance_id + 1,
                    "date": common_idx[t].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": action,
                    "price": price,
                    "prior_weight": prior_w,
                    "target_weight": target_w,
                    "weight_change": delta_w,
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": float(prior_s),
                    "target_shares": float(new_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": comm + slip,
                    "portfolio_equity": pre_rebal_equity,
                })
                held_shares[sym] = new_s
                traded_symbols.add(sym)

            if rebal_trades:
                rebalance_id += 1
                trades.extend(rebal_trades)
                new_w = drifted_w.copy()
                for i, sym in enumerate(symbols):
                    if sym in traded_symbols:
                        new_w[i] = tgt_w_arr[t, i]
                turnover = np.sum(np.abs(new_w - drifted_w))
                equity[t] -= pre_rebal_equity * turnover * cost_factor
                total_turnover += turnover
                actual_w[t] = new_w
            else:
                # No trades executed because all deltas were below min_shares
                actual_w[t] = drifted_w
        else:
            # No rebalance, actual weights are just the drifted weights
            actual_w[t] = drifted_w

    equity_df = pd.DataFrame(index=common_idx)
    equity_df["equity"] = equity

    rebalance_report_df = pd.DataFrame(trades, columns=REBALANCE_REPORT_COLUMNS) if trades else pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS)

    # Reconstruct actual weights DataFrame for transparency
    actual_weights_df = pd.DataFrame(actual_w, index=common_idx, columns=symbols)

    # Compute additional performance metrics
    eq = equity_df["equity"]
    daily_returns = eq.pct_change().dropna()

    total_return = (eq.iloc[-1] / eq.iloc[0]) - 1.0 if len(eq) > 0 else 0.0
    # NOTE -- undocumented-until-now "years" convention mismatch:
    # unlike common/metrics.py's cagr() (trading-day-count basis,
    # n_periods/periods_per_year), this inline CAGR uses actual CALENDAR
    # days elapsed (.days/365.25). The two will disagree whenever the
    # underlying calendar has gaps vs. a fixed periods_per_year assumption.
    # This is a genuine inconsistency (not a deliberate disclosed dual
    # convention like win_rate/profit_factor vs. win_rate_from_returns/
    # profit_factor_from_returns) -- flagged here rather than changed, since
    # altering either formula risks breaking other correctness-dependent
    # code/tests.
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

    # Win Rate & Profit Factor (returns-based; see common/metrics.py's
    # win_rate_from_returns/profit_factor_from_returns docstrings for why
    # these are distinct from the trades-based win_rate/profit_factor also
    # defined there).
    win_rate = win_rate_from_returns(daily_returns)
    profit_factor = profit_factor_from_returns(daily_returns)

    return {
        "equity_curve": equity_df,
        "actual_weights": actual_weights_df,
        "rebalance_report": rebalance_report_df,
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

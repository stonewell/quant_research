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


def _get_china_price_limit(symbol: str) -> float:
    """Returns price limit for China A-shares:
    - 0.20 for ChiNext (300, 301) and STAR Market (688, 588)
    - 0.30 for BSE (8, 4, 920)
    - 0.10 for Main board (60, 00, 51, 15)
    Returns 1.0 (unlimited) for foreign assets or cash proxies.
    """
    sym = symbol.upper().split(".")[0]
    if not any(symbol.upper().endswith(sfx) for sfx in (".SH", ".SZ", ".BJ")) and not sym.isdigit():
        return 1.0
    if sym.startswith(("300", "301", "688", "588")):
        return 0.20
    if sym.startswith(("8", "4", "920")):
        return 0.30
    if sym.startswith(("60", "00", "51", "15")):
        return 0.10
    return 0.10


HK_COMMON_BOARD_LOTS = {
    "00005": 400, "0005": 400,   # HSBC Holdings
    "00175": 1000, "0175": 1000, # Geely Auto
    "01211": 500, "1211": 500,   # BYD Company
    "01810": 200, "1810": 200,   # Xiaomi
    "00941": 500, "0941": 500,   # China Mobile
    "09618": 50, "9618": 50,     # JD.com
    "02318": 500, "2318": 500,   # Ping An
    "00700": 100, "0700": 100,   # Tencent
    "09988": 100, "9988": 100,   # Alibaba
    "03690": 100, "3690": 100,   # Meituan
}


def _get_hk_board_lot(symbol: str, default_lot: int = 100) -> int:
    """Returns the board lot for an HKEX stock.
    Defaults to default_lot (normally 100) if not in the lookup table."""
    code = symbol.upper().split(".")[0].lstrip("0") or "0"
    raw_code = symbol.upper().split(".")[0]
    return HK_COMMON_BOARD_LOTS.get(raw_code, HK_COMMON_BOARD_LOTS.get(code, default_lot))


def run_allocation_backtest(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
    min_shares: int = 1,
    china_trading: bool = False,
    stamp_duty_pct: float = 0.0005,
    us_trading: bool = False,
    hk_trading: bool = False,
    hk_stamp_duty_pct: float = 0.001085,
    sec_fee_pct: float = 0.0000278,
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

    `china_trading`: applies China A-share trading rules:
    - Board price limits: blocks BUY orders on limit-up (10%/20%/30%),
      and blocks SELL orders on limit-down.
    - T+1 settlement: shares bought today cannot be sold until next day.
    - 100-share round lot: auto-upgrades `min_shares` to at least 100.
    - Sell-side stamp duty: applies `stamp_duty_pct` (default 5 bps) only on sells.

    `us_trading`: applies US market rules:
    - 1-share lot sizing (`min_shares = 1`).
    - Unconstrained price limits.
    - T+0 intraday day-trading.
    - SEC Section 31 fee on SELL orders only (`sec_fee_pct`, default 0.00278%).

    `hk_trading`: applies Hong Kong market rules:
    - Board lot sizing (typically 100 shares, stock-specific lookup).
    - Unconstrained price limits.
    - T+0 intraday day-trading.
    - Dual-sided stamp duty and levies on BOTH BUY and SELL orders (`hk_stamp_duty_pct`, default 0.1085%).
    """
    if sum([bool(china_trading), bool(us_trading), bool(hk_trading)]) > 1:
        raise ValueError("Only one of china_trading, us_trading, hk_trading may be enabled.")

    if china_trading and min_shares == 1:
        min_shares = 100
    elif hk_trading and min_shares == 1:
        min_shares = 100
    elif us_trading:
        min_shares = 1

    if isinstance(min_shares, bool) or not isinstance(min_shares, (int, np.integer)) or min_shares < 1:
        raise ValueError(f"min_shares must be an integer >= 1, got {min_shares!r}")

    symbols = list(universe.keys())
    if not symbols or target_weights.empty:
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}

    # Extract aligned close, high, low prices
    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})
    highs = pd.DataFrame({sym: df["High"] if "High" in df.columns else df["Close"] for sym, df in universe.items()})
    lows = pd.DataFrame({sym: df["Low"] if "Low" in df.columns else df["Close"] for sym, df in universe.items()})

    # Ensure target_weights and closes are aligned
    common_idx = closes.index.intersection(target_weights.index)
    if len(common_idx) == 0:
        # target_weights and the universe's OHLCV cover disjoint date ranges
        # (e.g. a template's rebalance dates were computed against a
        # different calendar). Without this check, `equity[0] = initial_capital`
        # below indexes into a zero-length array and raises IndexError.
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}
    closes = closes.loc[common_idx]
    highs = highs.loc[common_idx]
    lows = lows.loc[common_idx]
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
    closes_arr = closes.to_numpy(dtype=float)
    highs_arr = highs.to_numpy(dtype=float)
    lows_arr = lows.to_numpy(dtype=float)

    china_limit_pcts = np.array([_get_china_price_limit(s) for s in symbols], dtype=float) if china_trading else None
    sym_min_shares_arr = np.array([_get_hk_board_lot(s, min_shares) for s in symbols], dtype=int) if hk_trading else np.full(len(symbols), min_shares, dtype=int)

    # State tracking
    equity = np.zeros(n_days)
    equity[0] = initial_capital

    # Actual weights held at the END of the day (after drift and any rebalancing)
    actual_w = np.zeros_like(tgt_w_arr)

    total_turnover = 0.0
    cost_factor = commission_pct + slippage_pct

    # Tracking integer shares held for each symbol (no fractional shares allowed)
    held_shares = {sym: 0 for sym in symbols}
    settled_shares = {sym: 0 for sym in symbols}

    trades = []
    rebalance_id = 0

    # Day 0: Initial allocation
    day0_trades = []
    day0_actual_w = np.zeros(len(symbols))
    for i, sym in enumerate(symbols):
        target_w = float(tgt_w_arr[0, i])
        if abs(target_w) > 1e-7:
            price = float(closes_arr[0, i])
            sym_min_shares = int(sym_min_shares_arr[i])
            if price > 0:
                raw_shares = (abs(target_w) * initial_capital) / price
                # round() before int() prevents IEEE 754 float truncation
                # (e.g. 99.99999999997 → 99 → 0 lots instead of 100).
                rounded_s = int(round(raw_shares, 6))
                target_s = (rounded_s // sym_min_shares) * sym_min_shares if sym_min_shares > 1 else rounded_s
            else:
                target_s = 0

            if target_s >= sym_min_shares:
                trade_s = target_s
                held_s = trade_s if target_w >= 0 else -trade_s
                held_shares[sym] = held_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                if hk_trading:
                    stamp = float(trade_val * hk_stamp_duty_pct)
                elif china_trading:
                    stamp = float(trade_val * stamp_duty_pct) if target_w < 0 else 0.0
                elif us_trading:
                    stamp = float(trade_val * sec_fee_pct) if target_w < 0 else 0.0
                else:
                    stamp = 0.0
                total_cost = comm + slip + stamp
                day0_actual_w[i] = target_w  # tentative; overwritten below with share-based weight
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
                    "total_cost": total_cost,
                    "portfolio_equity": float(initial_capital),
                })

    if day0_trades:
        rebalance_id = 1
        trades.extend(day0_trades)
        total_exposure = np.sum(np.abs(day0_actual_w))
        if total_exposure > 1.0:
            day0_actual_w = day0_actual_w / total_exposure
        actual_w[0] = day0_actual_w
        turnover = np.sum(np.abs(actual_w[0]))
        if china_trading or us_trading or hk_trading:
            equity[0] -= sum(tr["total_cost"] for tr in day0_trades)
        else:
            equity[0] -= equity[0] * turnover * cost_factor
        total_turnover += turnover
    else:
        actual_w[0] = np.zeros(len(symbols))

    settled_shares = held_shares.copy()

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

            # 1. Determine candidate trades for each symbol based on delta_w
            candidates = []
            for i, sym in enumerate(symbols):
                prior_s = held_shares[sym]
                prior_w = float(drifted_w[i])
                target_w = float(tgt_w_arr[t, i])
                delta_w = target_w - prior_w
                if abs(delta_w) <= 1e-7:
                    continue

                price = float(closes_arr[t, i])
                if price <= 0:
                    continue

                if china_trading:
                    limit_pct = float(china_limit_pcts[i])
                    if limit_pct < 1.0:
                        ret_i = float(ret_arr[t, i])
                        p_high = float(highs_arr[t, i])
                        p_low = float(lows_arr[t, i])
                        is_limit_up = (ret_i >= limit_pct - 0.005) and (p_high - price <= 1e-4 * price)
                        is_limit_down = (ret_i <= -limit_pct + 0.005) and (price - p_low <= 1e-4 * price)
                        if delta_w > 0 and is_limit_up:
                            # Cannot buy when locked at limit-up ceiling
                            continue
                        if delta_w < 0 and is_limit_down:
                            # Cannot sell when locked at limit-down floor
                            continue

                sym_min_shares = int(sym_min_shares_arr[i])

                if abs(target_w) < 1e-7 and prior_s != 0:
                    # Full liquidation allowed (closing out odd-lots)
                    trade_s = abs(prior_s)
                    action = "SELL" if prior_s > 0 else "BUY"
                elif abs(target_w) < 1e-7 and prior_s == 0:
                    # Target is zero and we already hold zero shares
                    continue
                else:
                    raw_val = abs(delta_w) * pre_rebal_equity
                    raw_s = raw_val / price
                    rounded_s = int(round(raw_s, 6))
                    trade_s = (rounded_s // sym_min_shares) * sym_min_shares if sym_min_shares > 1 else rounded_s
                    if trade_s < sym_min_shares:
                        continue
                    action = "BUY" if delta_w > 0 else "SELL"

                if china_trading and action == "SELL":
                    avail_to_sell = max(0, settled_shares[sym])
                    trade_s = min(trade_s, avail_to_sell)
                    if trade_s <= 0:
                        continue

                candidates.append({
                    "i": i,
                    "symbol": sym,
                    "action": action,
                    "price": price,
                    "prior_w": prior_w,
                    "target_w": target_w,
                    "delta_w": delta_w,
                    "prior_s": prior_s,
                    "trade_s": trade_s,
                    "sym_min_shares": sym_min_shares,
                })

            # Available cash before new trades: unallocated portfolio cash plus a discrete
            # lot-rounding buffer so minor rounding across symbols isn't choked, while
            # still blocking large-scale ghost leverage from suppressed sells.
            prior_market_exposure = float(np.sum(np.clip(drifted_w, 0.0, None)))
            lot_buffer = float(np.dot(sym_min_shares_arr, closes_arr[t]))
            avail_cash = max(0.0, (1.0 - prior_market_exposure) * pre_rebal_equity) + lot_buffer

            sells = [c for c in candidates if c["action"] == "SELL"]
            buys = [c for c in candidates if c["action"] == "BUY"]

            # Execute sells first to release cash
            for c in sells:
                sym = c["symbol"]
                price = c["price"]
                trade_s = min(c["trade_s"], abs(c["prior_s"])) if c["prior_s"] != 0 else c["trade_s"]
                if trade_s <= 0:
                    continue
                new_s = c["prior_s"] - trade_s if c["prior_s"] > 0 else c["prior_s"] + trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                if hk_trading:
                    stamp = float(trade_val * hk_stamp_duty_pct)
                elif china_trading:
                    stamp = float(trade_val * stamp_duty_pct)
                elif us_trading:
                    stamp = float(trade_val * sec_fee_pct)
                else:
                    stamp = 0.0
                total_cost = comm + slip + stamp
                avail_cash += trade_val
                held_shares[sym] = new_s
                settled_shares[sym] = max(0, settled_shares[sym] - trade_s)
                rebal_trades.append({
                    "rebalance_id": rebalance_id + 1,
                    "date": common_idx[t].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": "SELL",
                    "price": price,
                    "prior_weight": c["prior_w"],
                    "target_weight": c["target_w"],
                    "weight_change": c["delta_w"],
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": float(c["prior_s"]),
                    "target_shares": float(new_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": total_cost,
                    "portfolio_equity": pre_rebal_equity,
                })

            # Execute buys up to available cash capacity to prevent ghost leverage
            for c in buys:
                sym = c["symbol"]
                price = c["price"]
                sym_min_shares = c.get("sym_min_shares", min_shares)
                max_affordable_s = int(round(avail_cash / price, 6))
                max_lots_s = (max_affordable_s // sym_min_shares) * sym_min_shares if sym_min_shares > 1 else max_affordable_s
                trade_s = min(c["trade_s"], max_lots_s)
                if trade_s < sym_min_shares:
                    continue
                new_s = c["prior_s"] + trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                stamp = float(trade_val * hk_stamp_duty_pct) if hk_trading else 0.0
                total_cost = comm + slip + stamp
                avail_cash = max(0.0, avail_cash - trade_val)
                held_shares[sym] = new_s
                rebal_trades.append({
                    "rebalance_id": rebalance_id + 1,
                    "date": common_idx[t].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": "BUY",
                    "price": price,
                    "prior_weight": c["prior_w"],
                    "target_weight": c["target_w"],
                    "weight_change": c["delta_w"],
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": float(c["prior_s"]),
                    "target_shares": float(new_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": total_cost,
                    "portfolio_equity": pre_rebal_equity,
                })

            if rebal_trades:
                rebalance_id += 1
                trades.extend(rebal_trades)
                new_w = drifted_w.copy()
                traded_symbols = {tr["symbol"] for tr in rebal_trades}
                for i, sym in enumerate(symbols):
                    if sym in traded_symbols or (abs(tgt_w_arr[t, i]) < 1e-7 and held_shares[sym] == 0):
                        new_w[i] = tgt_w_arr[t, i]
                total_exposure = np.sum(np.abs(new_w))
                if total_exposure > 1.0:
                    new_w = new_w / total_exposure
                turnover = np.sum(np.abs(new_w - drifted_w))
                if china_trading or us_trading or hk_trading:
                    equity[t] -= sum(tr["total_cost"] for tr in rebal_trades)
                else:
                    equity[t] -= pre_rebal_equity * turnover * cost_factor
                total_turnover += turnover
                actual_w[t] = new_w
            else:
                # No trades executed because all deltas were below min_shares
                actual_w[t] = drifted_w
        else:
            # No rebalance, actual weights are just the drifted weights
            actual_w[t] = drifted_w

        settled_shares = held_shares.copy()

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

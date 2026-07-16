"""Dollar-neutral long-short execution for the pairs-trading signals in
pairs.py. Mirrors backtester.py's no-lookahead convention (signals decided
from a bar's CLOSE, acted on at the NEXT bar's OPEN) and its commission/
slippage cost model, extended to two simultaneous legs funded from one
shared cash pool.

Disclosed simplification: no margin/borrow-cost modeling for the short leg
-- proceeds from the short sale are credited to cash immediately and the
liability is marked to market every bar (the standard simplified short-
accounting convention), but real short selling also incurs a stock-borrow
fee and margin requirements that this does not model. None of this
workspace's other backtesters model borrow costs either, since none of them
short -- this is the first one that needs to disclose the gap.
"""

import numpy as np
import pandas as pd

from .pairs import PairsConfig, pairs_signals


def run_pairs_backtest(df_a: pd.DataFrame, df_b: pd.DataFrame, config: PairsConfig = None,
                        initial_capital: float = 100_000.0, commission_per_trade: float = 1.0,
                        commission_pct: float = 0.0005, slippage_pct: float = 0.0005,
                        warmup: int = None) -> dict:
    config = config or PairsConfig()
    sig = pairs_signals(df_a, df_b, config)

    if warmup is None:
        warmup = config.lookback + 1
    if len(df_a) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    cash = initial_capital
    position = None
    pending_action = None
    equity_rows, trades = [], []

    def log_trade(date, side, instrument, price, qty, pnl, event):
        trades.append({"date": date, "side": side, "instrument": instrument, "price": price,
                       "qty": qty, "pnl": pnl, "event": event})

    def execute_entry(direction, date, open_a, open_b):
        nonlocal cash, position
        if direction == "short_a_long_b":
            long_price, short_price = open_b * (1 + slippage_pct), open_a * (1 - slippage_pct)
            instrument_long, instrument_short = "b", "a"
        else:
            long_price, short_price = open_a * (1 + slippage_pct), open_b * (1 - slippage_pct)
            instrument_long, instrument_short = "a", "b"

        leg_notional = (cash - 2 * commission_per_trade) / 2
        if leg_notional <= 0:
            return

        qty_long = leg_notional / (long_price * (1 + commission_pct))
        long_cost = qty_long * long_price * (1 + commission_pct) + commission_per_trade
        cash -= long_cost

        qty_short = leg_notional / short_price
        short_proceeds = qty_short * short_price * (1 - commission_pct) - commission_per_trade
        cash += short_proceeds

        position = {
            "direction": direction, "entry_idx": None,
            "qty_long": qty_long, "long_cost_basis": long_cost,
            "qty_short": qty_short, "short_proceeds": short_proceeds,
        }
        log_trade(date, "buy", instrument_long, long_price, qty_long, np.nan, "entry")
        log_trade(date, "short", instrument_short, short_price, qty_short, np.nan, "entry")

    def execute_exit(date, open_a, open_b, event):
        nonlocal cash, position
        if position["direction"] == "short_a_long_b":
            long_price, short_price = open_b * (1 - slippage_pct), open_a * (1 + slippage_pct)
            instrument_long, instrument_short = "b", "a"
        else:
            long_price, short_price = open_a * (1 - slippage_pct), open_b * (1 + slippage_pct)
            instrument_long, instrument_short = "a", "b"

        gross = position["qty_long"] * long_price
        long_proceeds = gross - (commission_per_trade + gross * commission_pct)
        long_pnl = long_proceeds - position["long_cost_basis"]
        cash += long_proceeds

        cover_cost = position["qty_short"] * short_price * (1 + commission_pct) + commission_per_trade
        short_pnl = position["short_proceeds"] - cover_cost
        cash -= cover_cost

        log_trade(date, "sell", instrument_long, long_price, position["qty_long"], long_pnl, event)
        log_trade(date, "cover", instrument_short, short_price, position["qty_short"], short_pnl, event)
        position = None

    for t in range(warmup, len(df_a)):
        date = df_a.index[t]
        bar_a, bar_b = df_a.iloc[t], df_b.iloc[t]

        if pending_action in ("enter_short_a_long_b", "enter_long_a_short_b") and position is None:
            execute_entry(pending_action.replace("enter_", ""), date, bar_a["Open"], bar_b["Open"])
            if position is not None:
                position["entry_idx"] = t
        elif pending_action == "exit" and position is not None:
            execute_exit(date, bar_a["Open"], bar_b["Open"], "signal_exit")
        pending_action = None

        if position is None:
            if bool(sig["enter_short_a_long_b"].iloc[t]):
                pending_action = "enter_short_a_long_b"
            elif bool(sig["enter_long_a_short_b"].iloc[t]):
                pending_action = "enter_long_a_short_b"
        else:
            held_days = t - position["entry_idx"]
            if bool(sig["exit_signal"].iloc[t]) or bool(sig["stop_signal"].iloc[t]) or held_days >= config.max_holding_days:
                pending_action = "exit"

        if position is not None:
            if position["direction"] == "short_a_long_b":
                long_value = position["qty_long"] * bar_b["Close"]
                short_liability = position["qty_short"] * bar_a["Close"]
            else:
                long_value = position["qty_long"] * bar_a["Close"]
                short_liability = position["qty_short"] * bar_b["Close"]
        else:
            long_value = short_liability = 0.0

        equity = cash + long_value - short_liability
        equity_rows.append({"date": date, "cash": cash, "long_value": long_value,
                            "short_liability": short_liability, "equity": equity,
                            "in_position": position is not None})

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    if not equity_curve.empty:
        equity_curve["drawdown"] = (equity_curve["equity"].cummax() - equity_curve["equity"]) / equity_curve["equity"].cummax()
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log}

"""Template-agnostic backtest loop: works for any Template from templates.py.

Single position, binary exposure (0%/100% of equity, comparable to a
buy-and-hold baseline). Entry/exit signals are computed from a bar's CLOSE
and acted on at the NEXT bar's OPEN (no lookahead); the ATR-based stop-loss
distance uses the ATR as of the bar the entry was DECIDED on, not the
execution bar, for the same reason. The stop itself is checked intrabar
(against the bar's Low) and executes the same bar, since a protective stop
should react immediately rather than wait a full bar.
"""

import numpy as np
import pandas as pd

from .indicators import atr_pct


def run_backtest(df: pd.DataFrame, template, params: dict, initial_capital: float = 100_000.0,
                  commission_per_trade: float = 1.0, commission_pct: float = 0.0005,
                  slippage_pct: float = 0.0005, atr_period: int = 14, warmup: int = None) -> dict:
    sig = template.signals(df, params)
    atr_pct_series = atr_pct(df, atr_period)
    atr_pct_decision = atr_pct_series.shift(1)  # ATR as of the bar the decision was made on

    if warmup is None:
        warmup = max(atr_period, 20) + 1
    if len(df) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    cash = initial_capital
    position = None
    pending_action = None
    equity_rows, trades = [], []

    def log_trade(date, side, price, qty, pnl, event):
        trades.append({"date": date, "side": side, "price": price, "qty": qty, "pnl": pnl, "event": event})

    def execute_exit(price, date, event):
        nonlocal cash, position
        gross = position["qty"] * price
        fees = commission_per_trade + gross * commission_pct
        proceeds = gross - fees
        pnl = proceeds - position["cost_basis"]
        cash += proceeds
        log_trade(date, "sell", price, position["qty"], pnl, event)
        position = None

    for t in range(warmup, len(df)):
        date = df.index[t]
        bar = df.iloc[t]

        if pending_action == "enter" and position is None:
            entry_price = bar["Open"] * (1 + slippage_pct)
            effective_alloc = cash - commission_per_trade
            if effective_alloc > 0:
                qty = effective_alloc / (entry_price * (1 + commission_pct))
                cost = qty * entry_price * (1 + commission_pct) + commission_per_trade
                cash -= cost
                decision_atr = atr_pct_decision.iloc[t]
                stop_distance = entry_price * template.stop_loss_atr_mult * decision_atr if not np.isnan(decision_atr) else None
                position = {
                    "qty": qty, "entry_price": entry_price, "cost_basis": cost,
                    "stop_price": (entry_price - stop_distance) if stop_distance else None,
                }
                log_trade(date, "buy", entry_price, qty, np.nan, "entry")
        elif pending_action == "exit" and position is not None:
            exit_price = bar["Open"] * (1 - slippage_pct)
            execute_exit(exit_price, date, "signal_exit")
        pending_action = None

        if position is not None and position["stop_price"] is not None and bar["Low"] <= position["stop_price"]:
            execute_exit(position["stop_price"], date, "stop_loss")

        if position is None:
            if bool(sig["entry_signal"].iloc[t]):
                pending_action = "enter"
        elif bool(sig["exit_signal"].iloc[t]):
            pending_action = "exit"

        inventory_value = position["qty"] * bar["Close"] if position is not None else 0.0
        equity = cash + inventory_value
        equity_rows.append({"date": date, "cash": cash, "inventory_value": inventory_value,
                             "equity": equity, "in_position": position is not None})

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    if not equity_curve.empty:
        equity_curve["drawdown"] = (equity_curve["equity"].cummax() - equity_curve["equity"]) / equity_curve["equity"].cummax()
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log}

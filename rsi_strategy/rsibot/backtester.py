"""Event-driven backtest loop for the RSI-2 long-only mean-reversion strategy.

Single position at a time (flat or long). Signals are computed from a bar's
CLOSE, then acted on at the NEXT bar's OPEN -- a deliberate no-lookahead
choice, since the closing RSI/trend-filter values used to make the decision
aren't actually known until the bar finishes. The one exception is a
stop-loss, which is checked intrabar against the bar's Low and executes at
the stop price the same bar, since a protective stop is meant to react
immediately rather than wait a full bar.

Equity is marked to market every bar (cash + open position valued at the
bar's close), matching the mark-to-market discipline used in the sibling
grid-trading backtester.
"""

import numpy as np
import pandas as pd

from .config import RSIConfig
from .strategy import generate_signals


def run_backtest(df: pd.DataFrame, config: RSIConfig) -> dict:
    signals = generate_signals(df, config)

    warmup = max(config.warmup_bars, config.trend_ma_period, config.rsi_period, config.exit_ma_period) + 1
    if len(df) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    cash = config.initial_capital
    position = None  # dict: qty, entry_price, entry_bar, cost_basis
    pending_action = None

    equity_rows = []
    trades = []

    def log_trade(date, side, price, qty, pnl, event):
        trades.append({"date": date, "side": side, "price": price, "qty": qty, "pnl": pnl, "event": event})

    def execute_exit(price, date, event):
        nonlocal cash, position
        gross = position["qty"] * price
        fees = config.commission_per_trade + gross * config.commission_pct
        proceeds = gross - fees
        pnl = proceeds - position["cost_basis"]
        cash += proceeds
        log_trade(date, "sell", price, position["qty"], pnl, event)
        position = None

    for t in range(warmup, len(df)):
        date = df.index[t]
        bar = df.iloc[t]
        sig = signals.iloc[t]

        # --- 1. execute an action queued from the previous bar's close, at this bar's open ---
        if pending_action == "enter" and position is None:
            entry_price = bar["Open"] * (1 + config.slippage_pct)
            effective_alloc = config.position_size_pct * cash - config.commission_per_trade
            if effective_alloc > 0:
                qty = effective_alloc / (entry_price * (1 + config.commission_pct))
                cost = qty * entry_price * (1 + config.commission_pct) + config.commission_per_trade
                cash -= cost
                position = {"qty": qty, "entry_price": entry_price, "entry_bar": t, "cost_basis": cost}
                log_trade(date, "buy", entry_price, qty, np.nan, "entry")
        elif isinstance(pending_action, tuple) and pending_action[0] == "exit" and position is not None:
            exit_price = bar["Open"] * (1 - config.slippage_pct)
            execute_exit(exit_price, date, pending_action[1])
        pending_action = None

        # --- 2. intrabar protective stop (checked every bar while in a position) ---
        if position is not None and config.stop_loss_pct is not None:
            stop_price = position["entry_price"] * (1 - config.stop_loss_pct)
            if bar["Low"] <= stop_price:
                execute_exit(stop_price, date, "stop_loss")

        # --- 3. decide the action for the NEXT bar, based on this bar's close ---
        if position is None:
            if bool(sig["entry_signal"]):
                pending_action = "enter"
        elif config.max_holding_days is not None and (t - position["entry_bar"]) >= config.max_holding_days:
            pending_action = ("exit", "max_holding_days")
        elif bool(sig["exit_signal"]):
            pending_action = ("exit", config.exit_mode)

        # --- 4. mark to market ---
        inventory_value = position["qty"] * bar["Close"] if position is not None else 0.0
        equity = cash + inventory_value
        equity_rows.append({
            "date": date, "cash": cash, "inventory_value": inventory_value, "equity": equity,
            "in_position": position is not None, "rsi": sig["rsi"], "trend_ok": bool(sig["trend_ok"]),
        })

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    equity_curve["drawdown"] = (equity_curve["equity"].cummax() - equity_curve["equity"]) / equity_curve["equity"].cummax()
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log, "config": config}

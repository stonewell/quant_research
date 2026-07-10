"""Event-driven backtest loop for the trend-pullback swing strategy.

Single position at a time (flat or long). Entries and the RSI mean-reversion
exit are decided from a bar's CLOSE and acted on at the NEXT bar's OPEN (no
lookahead). Stop-loss, trailing stop, and profit target are checked intrabar
against the bar's Low/High and execute the same bar, since protective/target
exits are meant to react immediately rather than wait a full bar. Whichever
protective level (fixed stop vs. an activated trailing stop) is tightest for
a given bar is the one that's checked -- see config.py for why the trailing
stop can, once active, sit above the original fixed stop.

Position sizing defaults to fixed-fractional risk sizing (risk_per_trade_pct
of equity, based on the stop distance) rather than a flat percent of equity,
per the risk-of-ruin research cited in the README.
"""

import numpy as np
import pandas as pd

from .config import SwingConfig
from .strategy import generate_signals


def run_backtest(df: pd.DataFrame, config: SwingConfig) -> dict:
    signals = generate_signals(df, config)

    warmup = max(config.warmup_bars, config.trend_ma_period + config.trend_slope_lookback,
                 config.pullback_ma_period, config.rsi_period) + 1
    if len(df) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    cash = config.initial_capital
    position = None
    pending_action = None

    equity_rows = []
    trades = []

    def log_trade(date, side, price, qty, pnl, pnl_pct, event):
        trades.append({"date": date, "side": side, "price": price, "qty": qty, "pnl": pnl, "pnl_pct": pnl_pct, "event": event})

    def execute_exit(price, date, event):
        nonlocal cash, position
        gross = position["qty"] * price
        fees = config.commission_per_trade + gross * config.commission_pct
        proceeds = gross - fees
        pnl = proceeds - position["cost_basis"]
        pnl_pct = pnl / position["cost_basis"]
        cash += proceeds
        log_trade(date, "sell", price, position["qty"], pnl, pnl_pct, event)
        position = None

    def size_position(entry_price: float) -> float:
        if config.sizing_mode == "risk_based":
            risk_dollars = config.risk_per_trade_pct * cash
            stop_distance = entry_price * config.stop_loss_pct
            qty = risk_dollars / stop_distance if stop_distance > 0 else 0.0
            cap_qty = (config.max_position_pct_of_equity * cash) / entry_price
            return min(qty, cap_qty)
        # equity_pct: leave room for fees so position_size_pct=1.0 doesn't
        # silently fail the cost<=cash check and skip every entry.
        effective_alloc = config.position_size_pct * cash - config.commission_per_trade
        if effective_alloc <= 0:
            return 0.0
        return effective_alloc / (entry_price * (1 + config.commission_pct))

    for t in range(warmup, len(df)):
        date = df.index[t]
        bar = df.iloc[t]
        sig = signals.iloc[t]

        # --- 1. execute an action queued from the previous bar's close, at this bar's open ---
        if pending_action == "enter" and position is None:
            entry_price = bar["Open"] * (1 + config.slippage_pct)
            qty = size_position(entry_price)
            cost = qty * entry_price * (1 + config.commission_pct) + config.commission_per_trade
            if qty > 0 and cost <= cash:
                cash -= cost
                position = {
                    "qty": qty, "entry_price": entry_price, "entry_bar": t, "cost_basis": cost,
                    "peak_price": entry_price,
                    "stop_price": entry_price * (1 - config.stop_loss_pct),
                    "target_price": entry_price * (1 + config.stop_loss_pct * config.reward_risk_ratio),
                }
                log_trade(date, "buy", entry_price, qty, np.nan, np.nan, "entry")
        elif isinstance(pending_action, tuple) and pending_action[0] == "exit" and position is not None:
            exit_price = bar["Open"] * (1 - config.slippage_pct)
            execute_exit(exit_price, date, pending_action[1])
        pending_action = None

        # --- 2. intrabar protective stop / trailing stop / profit target ---
        if position is not None:
            effective_stop = position["stop_price"]
            stop_event = "stop_loss"
            if config.use_trailing_stop:
                position["peak_price"] = max(position["peak_price"], bar["High"])
                gain = position["peak_price"] / position["entry_price"] - 1
                if gain >= config.trailing_activate_pct:
                    trailing_price = position["peak_price"] * (1 - config.trailing_stop_pct)
                    if trailing_price > effective_stop:
                        effective_stop = trailing_price
                        stop_event = "trailing_stop"

            if bar["Low"] <= effective_stop:
                execute_exit(effective_stop, date, stop_event)
            elif bar["High"] >= position["target_price"]:
                execute_exit(position["target_price"], date, "profit_target")

        # --- 3. decide the action for the NEXT bar, based on this bar's close ---
        if position is None:
            if bool(sig["entry_signal"]):
                pending_action = "enter"
        elif config.max_holding_days is not None and (t - position["entry_bar"]) >= config.max_holding_days:
            pending_action = ("exit", "max_holding_days")
        elif bool(sig["exit_signal"]):
            pending_action = ("exit", "rsi_cross")

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

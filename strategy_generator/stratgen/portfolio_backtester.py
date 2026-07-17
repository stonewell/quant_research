"""Multi-asset portfolio backtest: holds CONCURRENT positions across every
signaled symbol in a universe, on one shared trading calendar, funded from
one shared cash pool -- instead of backtesting each symbol independently
(each with its own 100%-of-capital single-position run) and pooling the
resulting Sharpe ratios after the fact. `generator.py`'s single-symbol
candidate search now scores each params combo with ONE run of this
backtester across the whole universe, rather than N independent single-
symbol runs pooled by median/mean -- see generator.py's module docstring for
why that's a materially different, more realistic evaluation of "trade the
universe as a strategy" than approximating it via N separate backtests ever
was. Mirrors backtester.py's no-lookahead convention (a bar's CLOSE decides
the signal, acted on at the NEXT bar's OPEN) and per-position ATR stop-loss,
extended to many simultaneously-open positions.

Position sizing: equal-weight across up to `max_concurrent_positions` open
slots, each sized as `current_portfolio_equity / max_concurrent_positions`
at the moment of entry (not continuously rebalanced) -- the same "size once
at entry, then let it ride" convention every other backtester in this
workspace uses, extended from 1 slot to N. If more symbols signal entry on
the same bar than there are free slots, they're filled in a fixed,
deterministic order (sorted symbol name) -- a disclosed simplification; no
signal-strength ranking is attempted since every template here only emits a
boolean signal, not a ranked score.

Holding-period cap: `max_holding_days` (default 63 trading days ~= 3
months) force-closes any position still open past that many bars, REGARDLESS
of the template's own exit signal. This is what actually enforces this
project's sub-3-month holding-period theme when a template is run across a
universe: several templates' own signal-based exits (e.g. `MomentumTemplate`'s
MA crossover) have no such guarantee in isolation -- a strong trend can hold
a position open for a long time on its own, same reasoning as the hard cap
already added to pairs_backtester.py for the same purpose.
"""

import numpy as np
import pandas as pd

from .indicators import atr_pct


def _align(universe: dict) -> dict:
    common_index = None
    for df in universe.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    return {symbol: df.loc[common_index] for symbol, df in universe.items()}


def run_portfolio_backtest(universe: dict, template, params: dict, max_concurrent_positions: int = 10,
                           max_holding_days: int = 63, initial_capital: float = 100_000.0,
                           commission_per_trade: float = 1.0, commission_pct: float = 0.0005,
                           slippage_pct: float = 0.0005, atr_period: int = 14, warmup: int = None) -> dict:
    """`universe`: {symbol: OHLCV DataFrame}. Symbols are inner-joined onto a
    shared trading calendar internally (a real portfolio can only trade bars
    every held symbol actually has) -- unlike the old per-symbol-independent
    approach, which didn't need a shared calendar since each symbol's run
    was fully separate."""
    aligned = _align(universe)
    symbols = sorted(aligned.keys())
    if not symbols:
        raise ValueError("universe must contain at least one symbol's OHLCV DataFrame")
    common_index = aligned[symbols[0]].index

    if warmup is None:
        warmup = max(atr_period, 20) + 1
    if len(common_index) <= warmup:
        raise ValueError("Not enough aligned bars across the universe for the configured warmup period")

    sig = {s: template.signals(aligned[s], params) for s in symbols}
    atr_pct_decision = {s: atr_pct(aligned[s], atr_period).shift(1) for s in symbols}

    cash = initial_capital
    positions = {}          # symbol -> {qty, cost_basis, stop_price, entry_idx}
    pending_action = {}     # symbol -> "enter" | "exit"
    equity_rows, trades = [], []

    def log_trade(date, symbol, side, price, qty, pnl, event):
        trades.append({"date": date, "symbol": symbol, "side": side, "price": price,
                       "qty": qty, "pnl": pnl, "event": event})

    def execute_exit(symbol, price, date, event):
        nonlocal cash
        pos = positions.pop(symbol)
        gross = pos["qty"] * price
        fees = commission_per_trade + gross * commission_pct
        proceeds = gross - fees
        pnl = proceeds - pos["cost_basis"]
        cash += proceeds
        log_trade(date, symbol, "sell", price, pos["qty"], pnl, event)

    for t in range(warmup, len(common_index)):
        date = common_index[t]
        bar = {s: aligned[s].iloc[t] for s in symbols}

        for symbol in symbols:
            if pending_action.get(symbol) == "exit" and symbol in positions:
                exit_price = bar[symbol]["Open"] * (1 - slippage_pct)
                execute_exit(symbol, exit_price, date, "signal_exit")

        open_slots = max_concurrent_positions - len(positions)
        entry_candidates = [s for s in symbols if pending_action.get(s) == "enter" and s not in positions]
        for symbol in entry_candidates[:max(open_slots, 0)]:
            portfolio_equity = cash + sum(positions[s]["qty"] * bar[s]["Close"] for s in positions)
            alloc = min(portfolio_equity / max_concurrent_positions, cash - commission_per_trade)
            if alloc <= 0:
                continue
            entry_price = bar[symbol]["Open"] * (1 + slippage_pct)
            qty = alloc / (entry_price * (1 + commission_pct))
            cost = qty * entry_price * (1 + commission_pct) + commission_per_trade
            cash -= cost
            decision_atr = atr_pct_decision[symbol].iloc[t]
            stop_distance = entry_price * template.stop_loss_atr_mult * decision_atr if not np.isnan(decision_atr) else None
            positions[symbol] = {
                "qty": qty, "cost_basis": cost, "entry_idx": t,
                "stop_price": (entry_price - stop_distance) if stop_distance else None,
            }
            log_trade(date, symbol, "buy", entry_price, qty, np.nan, "entry")

        pending_action = {}

        for symbol in list(positions.keys()):
            pos = positions[symbol]
            if pos["stop_price"] is not None and bar[symbol]["Low"] <= pos["stop_price"]:
                execute_exit(symbol, pos["stop_price"], date, "stop_loss")

        for symbol in symbols:
            if symbol in positions:
                held_days = t - positions[symbol]["entry_idx"]
                if bool(sig[symbol]["exit_signal"].iloc[t]) or held_days >= max_holding_days:
                    pending_action[symbol] = "exit"
            elif bool(sig[symbol]["entry_signal"].iloc[t]):
                pending_action[symbol] = "enter"

        inventory_value = sum(positions[s]["qty"] * bar[s]["Close"] for s in positions)
        equity = cash + inventory_value
        equity_rows.append({"date": date, "cash": cash, "inventory_value": inventory_value,
                            "equity": equity, "num_open_positions": len(positions)})

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    if not equity_curve.empty:
        equity_curve["drawdown"] = (equity_curve["equity"].cummax() - equity_curve["equity"]) / equity_curve["equity"].cummax()
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log}

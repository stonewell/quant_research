"""Event-driven backtest loop for the regime-switching ensemble.

Binary exposure model (0% or 100% of equity, matching buy-and-hold's full
exposure for a fair comparison) with a single position at a time. Regime and
RSI values are already shifted (see regime.py), so bar t's decision uses only
data through bar t-1's close; execution happens at bar t's OPEN.

config.mode selects which sleeve(s) are active, so the CLI can report the
combined ensemble against each standalone component:
  "ensemble"     - trend regime -> buy-and-hold; range regime -> tactical
                   RSI(2) mean-reversion; downtrend -> cash.
  "trend_only"   - invested iff price > rising 200-day SMA, cash otherwise.
  "meanrev_only" - RSI(2) entries/exits whenever price > 200-day SMA (no ADX
                   sub-regime routing), cash below it -- this is the original
                   standalone RSI-2 strategy's logic.
"""

import numpy as np
import pandas as pd

from .config import EnsembleConfig
from .regime import classify_regime


def _desired_exposure(mode: str, regime: str, rsi_value: float, in_position: bool, config: EnsembleConfig) -> int:
    if mode == "trend_only":
        return 1 if regime != "downtrend" else 0

    if mode == "meanrev_only":
        if regime == "downtrend":
            return 0
        return _rsi_signal(rsi_value, in_position, config)

    # "ensemble"
    if regime == "downtrend":
        return 0
    if regime == "trend":
        return 1
    return _rsi_signal(rsi_value, in_position, config)  # regime == "range"


def _rsi_signal(rsi_value: float, in_position: bool, config: EnsembleConfig) -> int:
    if pd.isna(rsi_value):
        return 1 if in_position else 0
    if in_position:
        return 0 if rsi_value > config.exit_rsi_threshold else 1
    return 1 if rsi_value < config.entry_rsi_threshold else 0


def run_backtest(df: pd.DataFrame, config: EnsembleConfig) -> dict:
    classified = classify_regime(df, config)

    warmup = max(config.warmup_bars, config.trend_ma_period, config.adx_period, config.rsi_period) + 1
    if len(df) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    cash = config.initial_capital
    position = None  # dict: qty, entry_price, cost_basis

    equity_rows = []
    trades = []

    def log_trade(date, side, price, qty, pnl, event):
        trades.append({"date": date, "side": side, "price": price, "qty": qty, "pnl": pnl, "event": event})

    for t in range(warmup, len(df)):
        date = df.index[t]
        bar = df.iloc[t]
        row = classified.iloc[t]
        regime = row["regime"]
        rsi_value = row["rsi"]

        if pd.isna(regime):
            continue

        desired = _desired_exposure(config.mode, regime, rsi_value, position is not None, config)

        if desired == 1 and position is None:
            entry_price = bar["Open"] * (1 + config.slippage_pct)
            effective_alloc = cash - config.commission_per_trade
            if effective_alloc > 0:
                qty = effective_alloc / (entry_price * (1 + config.commission_pct))
                cost = qty * entry_price * (1 + config.commission_pct) + config.commission_per_trade
                cash -= cost
                position = {"qty": qty, "entry_price": entry_price, "cost_basis": cost}
                log_trade(date, "buy", entry_price, qty, np.nan, f"enter_{regime}")
        elif desired == 0 and position is not None:
            exit_price = bar["Open"] * (1 - config.slippage_pct)
            gross = position["qty"] * exit_price
            fees = config.commission_per_trade + gross * config.commission_pct
            proceeds = gross - fees
            pnl = proceeds - position["cost_basis"]
            cash += proceeds
            log_trade(date, "sell", exit_price, position["qty"], pnl, f"exit_{regime}")
            position = None

        inventory_value = position["qty"] * bar["Close"] if position is not None else 0.0
        equity = cash + inventory_value
        equity_rows.append({
            "date": date, "cash": cash, "inventory_value": inventory_value, "equity": equity,
            "in_position": position is not None, "regime": regime, "rsi": rsi_value,
        })

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    equity_curve["drawdown"] = (equity_curve["equity"].cummax() - equity_curve["equity"]) / equity_curve["equity"].cummax()
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log, "config": config}

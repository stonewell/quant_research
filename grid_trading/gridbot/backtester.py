"""Event-driven backtest loop for the ATR-adaptive grid strategy.

Owns all money accounting (cash, position cost basis, commissions, slippage,
mark-to-market equity) and risk controls (trend-filtered entries, an equity
drawdown circuit breaker, and a capital reserve cap). The GridEngine only
tracks grid geometry and slot state; this module decides whether a triggered
fill is actually affordable/allowed and books the cash flow.

Equity is marked to market every bar (cash + open inventory valued at the
bar's close), not just realized P&L on closed trades -- this specifically
avoids the "looks profitable intraday because only winners have closed"
illusion flagged in grid-trading backtesting literature.
"""

import numpy as np
import pandas as pd

from .config import GridConfig
from .grid_engine import GridEngine, compute_spacing_pct
from .indicators import atr as atr_indicator
from .indicators import trend_regime


def run_backtest(df: pd.DataFrame, config: GridConfig) -> dict:
    atr_series = atr_indicator(df, config.atr_period).shift(1)   # avoid lookahead: use prior-bar ATR
    trend_series = trend_regime(df["Close"], config.trend_ma_period, config.trend_band_pct).shift(1)
    prev_close = df["Close"].shift(1)

    warmup = max(config.warmup_bars, config.atr_period, config.trend_ma_period) + 1
    if len(df) <= warmup:
        raise ValueError("Not enough bars for the configured warmup period")

    engine = GridEngine(levels_per_side=config.grid_levels_per_side)
    cash = config.initial_capital
    equity_prev = config.initial_capital
    peak_equity = config.initial_capital
    cooldown_until = -1
    slot_cost_basis: dict = {}

    equity_rows = []
    trades = []

    def deployed_capital() -> float:
        return sum(slot_cost_basis.get(i, 0.0) for i, s in enumerate(engine.slots) if s.state == "long")

    def log_trade(date, side, price, qty, slot_id, pnl, event):
        trades.append({
            "date": date, "side": side, "price": price, "qty": qty,
            "slot_id": slot_id, "pnl": pnl, "event": event,
        })

    def execute_sell(i, price, date, event="grid_fill"):
        nonlocal cash
        fill = engine.fill_sell(i, price)
        gross = fill.qty * fill.price
        fees = config.commission_per_trade + gross * config.commission_pct
        proceeds = gross - fees
        cost_basis = slot_cost_basis.pop(i, fill.qty * fill.entry_price)
        pnl = proceeds - cost_basis
        cash += proceeds
        log_trade(date, "sell", fill.price, fill.qty, i, pnl, event)

    for t in range(warmup, len(df)):
        date = df.index[t]
        bar = df.iloc[t]
        a = atr_series.iloc[t]
        trend = trend_series.iloc[t]
        pc = prev_close.iloc[t]

        if pd.isna(a) or pd.isna(pc):
            continue

        spacing_pct = compute_spacing_pct(a, pc, config.atr_multiplier, config.min_spacing_pct, config.max_spacing_pct)
        spacing_abs = spacing_pct * pc

        # Regrid while flat: let the grid drift to follow price when there is
        # no open risk to disturb (initial build always happens here too).
        if not engine.slots or (not engine.open_slots and config.regrid_on_profit_cycle):
            engine.build_grid(center_price=pc, spacing_abs=spacing_abs)

        # Breakout regrid: strong trend has pushed price outside the band even
        # with open inventory -- cut losses/lock gains and re-anchor the grid.
        elif engine.is_breakout(bar["Close"], config.regrid_breakout_mult):
            for i in [idx for idx, s in enumerate(engine.slots) if s.state == "long"]:
                execute_sell(i, bar["Close"], date, event="breakout_regrid")
            engine.build_grid(center_price=bar["Close"], spacing_abs=spacing_abs)

        in_cooldown = t <= cooldown_until
        allow_new_entries = (not in_cooldown) and (trend != "down")

        # --- sells first: always allowed, they only reduce risk ---
        for i in engine.sell_triggers(bar["High"]):
            slot = engine.slots[i]
            price = slot.upper * (1 - config.slippage_pct)
            execute_sell(i, price, date)

        # --- buys: gated by trend filter, cooldown, open-slot cap, and reserve ---
        for i in engine.buy_triggers(bar["Low"], allow_new_entries, config.max_open_slots):
            slot = engine.slots[i]
            price = slot.lower * (1 + config.slippage_pct)
            dollar_alloc = config.position_size_pct * equity_prev
            reserve_cap = (1 - config.capital_reserve_pct) * equity_prev
            if deployed_capital() + dollar_alloc > reserve_cap:
                break  # capital reserve exhausted; further buys this bar would only be worse
            qty = dollar_alloc / price
            fees = config.commission_per_trade + qty * price * config.commission_pct
            cost = qty * price + fees
            if cost > cash:
                break
            cash -= cost
            engine.fill_buy(i, price, qty)
            slot_cost_basis[i] = cost
            log_trade(date, "buy", price, qty, i, np.nan, "grid_fill")

        inventory_value = sum(s.qty * bar["Close"] for s in engine.slots if s.state == "long")
        equity = cash + inventory_value
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0

        # --- equity circuit breaker ---
        if drawdown >= config.drawdown_stop_pct and not in_cooldown:
            for i, s in enumerate(engine.slots):
                if s.state == "long":
                    execute_sell(i, bar["Close"], date, event="drawdown_stop")
            inventory_value = 0.0
            equity = cash
            cooldown_until = t + config.cooldown_bars_after_stop

        equity_prev = equity
        equity_rows.append({
            "date": date, "cash": cash, "inventory_value": inventory_value,
            "equity": equity, "drawdown": drawdown, "trend": trend,
            "spacing_pct": spacing_pct, "grid_lower": engine.lower_bound, "grid_upper": engine.upper_bound,
        })

    equity_curve = pd.DataFrame(equity_rows).set_index("date")
    trade_log = pd.DataFrame(trades)
    return {"equity_curve": equity_curve, "trades": trade_log, "config": config}

"""Configuration for the ATR-adaptive grid trading strategy.

Default values are grounded in the research summarized in README.md:
- ATR-scaled spacing with a floor/ceiling (dev.to ATR-grid writeups): spacing_pct =
  clip(ATR% * atr_multiplier, min_spacing_pct, max_spacing_pct)
- Per-order sizing of 1-2% of equity and a cap on simultaneously open grid slots
  per side, to prevent overexposure during a directional move.
- A long-term SMA trend filter that pauses new buys in a confirmed downtrend and
  new sells (of un-owned shares) in a confirmed uptrend, since grid bots lose
  money buying every dip in a downtrend or selling every rally in an uptrend.
- An equity-based circuit breaker (floating drawdown stop) that liquidates and
  halts trading if drawdown from the equity high-water mark breaches a threshold.
"""

from dataclasses import dataclass


@dataclass
class GridConfig:
    # --- instrument / data ---
    symbol: str = "SPY"
    start: str = "2018-01-01"
    end: str = "2024-12-31"
    interval: str = "1d"

    # --- capital ---
    initial_capital: float = 100_000.0
    capital_reserve_pct: float = 0.4  # fraction of capital kept out of active grids

    # --- ATR-based dynamic spacing ---
    atr_period: int = 14
    atr_multiplier: float = 1.0
    min_spacing_pct: float = 0.01   # 1% floor
    max_spacing_pct: float = 0.06   # 6% ceiling

    # --- grid shape ---
    grid_levels_per_side: int = 6   # buy levels below center + sell levels above
    # Recenter (force-liquidating any open longs) once price exits
    # center +- regrid_breakout_mult * band_half_width. Backtesting shows this
    # matters a lot: a tight multiplier (~0.5-1.0) re-triggers on every minor
    # swing and racks up whipsaw losses, since each trigger force-sells
    # whatever is open at that moment's price. A wider multiplier (~2-3) lets
    # the trend filter and the drawdown-stop circuit breaker do most of the
    # defensive work instead, and only regrids for a genuine breakout. Treat
    # this as the primary knob to sensitivity-test/walk-forward-validate
    # per instrument rather than trusting the default blindly.
    regrid_breakout_mult: float = 2.0
    regrid_on_profit_cycle: bool = True  # recenter around price while flat (no open risk)

    # --- position sizing / exposure caps ---
    position_size_pct: float = 0.015   # equity fraction risked per grid slot (1.5%)
    # Long-only equities grid has one exposure direction (bought inventory), so
    # this caps the total number of concurrently open (bought) slots rather than
    # "per side" as it would for a long+short crypto grid.
    max_open_slots: int = 5

    # --- trend filter (avoid grid trading into strong trends) ---
    trend_ma_period: int = 100
    trend_band_pct: float = 0.03  # +-3% band around the MA counts as "range-bound"

    # --- risk controls ---
    drawdown_stop_pct: float = 0.10  # equity circuit breaker (10% floating DD)
    cooldown_bars_after_stop: int = 10  # bars to stay flat after a circuit-breaker trip

    # --- costs ---
    commission_per_trade: float = 1.0
    commission_pct: float = 0.0005   # 5 bps
    slippage_pct: float = 0.0005     # 5 bps

    # --- backtest mechanics ---
    warmup_bars: int = 120  # bars reserved for indicator warmup before trading starts

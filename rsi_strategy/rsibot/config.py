"""Configuration for the short-period RSI (Connors-style RSI-2) long-only
mean-reversion strategy.

Research grounding (see README.md for full citations/caveats):
- Entry rule (well-verified across 4+ independent sources): only buy when
  price is above its 200-day SMA (trend filter), and RSI(2) drops below 10
  (conservative) or 5 (aggressive). A "cumulative RSI(2)" variant sums RSI(2)
  over 2 days and enters below a cumulative threshold of 10.
- Trend filter (verified): restricting entries to price > 200-day SMA
  measurably reduces max drawdown vs. an unfiltered version.
- Exit rule: NOT reliably verified. The commonly repeated conventions (RSI
  crossing back above 50/70/90, or price closing above a 5-day SMA) failed
  adversarial source-checking despite being near-universal in secondary
  literature. Treat exit_mode/exit_rsi_threshold/exit_ma_period as an open
  parameter to tune empirically, not a settled citation.
- Risk management (stop-loss, holding-period limits, position sizing): no
  verified claims were found specific to this strategy -- this is a real
  evidence gap. Stop-loss and max-holding-period are implemented here as
  optional, off-by-default-for-the-stop knobs so both can be backtested
  empirically rather than asserted as best practice.
- Known failure mode (verified, single-source SPY 1993-2018 backtest):
  performance is highly sensitive to the exit threshold specifically (not
  the entry threshold), and even with the 200-day filter, RSI-2 underperformed
  a naive 50/200 moving-average crossover on both raw and risk-adjusted
  return in that sample. Don't expect this strategy to beat buy-and-hold or
  simpler trend-following in a trending market -- it's a mean-reversion tool.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RSIConfig:
    # --- instrument / data ---
    symbol: str = "SPY"
    start: str = "2018-01-01"
    end: str = "2024-12-31"
    interval: str = "1d"

    # --- capital ---
    initial_capital: float = 100_000.0
    position_size_pct: float = 1.0  # fraction of equity committed per trade (single position at a time)

    # --- RSI calculation ---
    rsi_period: int = 2
    rsi_method: str = "wilder"  # "wilder" (recursive smoothing) or "cutler" (simple average, avoids data-length dependency)

    # --- entry ---
    entry_mode: str = "single"  # "single" (RSI(period) < oversold_threshold) or "cumulative" (sum of RSI over cumulative_lookback bars)
    oversold_threshold: float = 10.0
    cumulative_lookback: int = 2
    cumulative_threshold: float = 10.0

    # --- trend filter ---
    require_trend_filter: bool = True
    trend_ma_period: int = 200

    # --- exit (open design choice per research -- tune empirically) ---
    exit_mode: str = "rsi_cross"  # "rsi_cross", "ma_cross", or "either"
    exit_rsi_threshold: float = 70.0
    exit_ma_period: int = 5

    # --- optional risk controls (no verified consensus; test with/without) ---
    stop_loss_pct: Optional[float] = None       # e.g. 0.05 for a 5% hard stop; None disables
    max_holding_days: Optional[int] = 10        # time-based exit safety net; None disables

    # --- costs ---
    commission_per_trade: float = 1.0
    commission_pct: float = 0.0005
    slippage_pct: float = 0.0005

    # --- backtest mechanics ---
    warmup_bars: int = 210  # covers the 200-day trend filter plus indicator lookback

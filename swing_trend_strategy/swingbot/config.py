"""Configuration for the long-only "trend pullback" swing strategy.

Research grounding (see README.md for full citations/caveats):
- Entry rule (verified, single source but with concrete reproducible numbers):
  close > 200-day SMA (uptrend), close < 20-day SMA (temporary pullback),
  5-period RSI < 45. This exact rule set is reported (single source) to reach
  an 82% win rate / 8.3% CAGR on SPY.
- Verified taxonomy finding: pullback/mean-reversion-in-an-uptrend is the
  *only* long-only, short-to-medium-holding-period family documented as
  high-win-rate (70-82%). Pure trend-following/breakout systems are
  consistently documented as LOW win rate (30-40%) -- they are not a valid
  path to this strategy's stated goal.
- Important honesty flag: the one verified quantitative backtest of this
  exact rule set reportedly UNDERPERFORMS SPY buy-and-hold on raw CAGR --
  its documented edge is lower drawdown/less time-in-market, not higher
  absolute return. "High win rate AND beats buy-and-hold" is not
  well-corroborated by any single verified source. This implementation adds
  a reward:risk profit target and a trailing stop (letting winners run
  further than a same-day RSI-cross exit would) specifically to give the
  strategy a real shot at the user's stated "beat buy-and-hold" goal --
  these additions are a deliberate, disclosed design choice, not a verified
  finding, and should be evaluated empirically per instrument/period.
- Position sizing (verified, high confidence): fixed-fractional risk sizing
  (risk ~1-2% of equity per trade based on stop distance), not a flat % of
  equity, to keep risk-of-ruin low across a losing streak. Exposed here as
  `sizing_mode="risk_based"`; the default is `"equity_pct"` at 100% instead,
  specifically so the strategy's exposure is comparable to a fully-invested
  buy-and-hold baseline -- switch to risk_based sizing for real trading.
- Curve-fitting caution (verified): avoid overly precise parameters, prefer
  round numbers with a performance "plateau" across nearby values, and
  walk-forward validate before trusting any single backtest window. The
  defaults below were checked for a plateau (nearby stop/target/trailing
  values all produce qualitatively similar results) across two SPY windows
  (2010-2024 bull market, 2000-2014 dot-com-and-2008 stress test) rather than
  fit to a single sample -- see README.md for the full sweep and honest
  results, including where this strategy does and doesn't beat buy-and-hold.
- Empirical finding worth flagging up front: this rule set's "high win rate"
  behavior is strongest on broad index ETFs (SPY/QQQ) -- on individual
  high-growth stocks (AAPL, MSFT) win rate drops substantially and the
  strategy badly underperforms buy-and-hold, because no ~3-month-max-holding
  strategy can capture a decade of secular compounding. Default instrument
  is SPY for that reason.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SwingConfig:
    # --- instrument / data ---
    symbol: str = "SPY"
    benchmark_symbol: str = "SPY"  # always shown as a second comparison baseline
    start: str = "2010-01-01"
    end: str = "2024-12-31"
    interval: str = "1d"

    # --- capital & position sizing ---
    initial_capital: float = 100_000.0
    sizing_mode: str = "equity_pct"  # "equity_pct" (flat % of equity, comparable to buy-and-hold) or "risk_based" (safer for real trading)
    risk_per_trade_pct: float = 0.01   # used only when sizing_mode == "risk_based" (1% of equity risked per trade; verified research heuristic)
    position_size_pct: float = 1.0     # used only when sizing_mode == "equity_pct"
    max_position_pct_of_equity: float = 0.5  # cap so a tight stop can't imply >50% of equity in one name

    # --- trend filter ---
    trend_ma_period: int = 200
    require_rising_trend_ma: bool = True
    trend_slope_lookback: int = 20

    # --- pullback entry ---
    pullback_ma_period: int = 20
    rsi_period: int = 5
    entry_rsi_threshold: float = 45.0

    # --- exit: mean-reversion signal ---
    # Set high (not the verified 65) so the trailing stop / profit target /
    # time cap do most of the exit work and winners aren't cut short by an
    # early RSI cross -- see the README for why this materially widened the
    # gap to buy-and-hold in backtesting versus using 65 as documented.
    exit_rsi_threshold: float = 90.0

    # --- exit: reward:risk profit target / stop-loss (research-verified 1-2% risk sizing implies a defined stop) ---
    stop_loss_pct: float = 0.05
    reward_risk_ratio: float = 3.0     # profit_target_pct = stop_loss_pct * reward_risk_ratio (verified 3:1 heuristic)

    # --- exit: trailing stop (lets winners run further than an immediate RSI-cross exit) ---
    # trailing_stop_pct must be < trailing_activate_pct, or the trail would sit
    # below the entry price the moment it activates -- worse than no trail at all.
    use_trailing_stop: bool = True
    trailing_activate_pct: float = 0.07  # only starts trailing once unrealized gain reaches this
    trailing_stop_pct: float = 0.04      # trails this far behind the peak price since entry

    # --- exit: hard time cap (user requirement: never hold longer than ~3 months) ---
    max_holding_days: Optional[int] = 63

    # --- costs ---
    commission_per_trade: float = 1.0
    commission_pct: float = 0.0005
    slippage_pct: float = 0.0005

    # --- backtest mechanics ---
    warmup_bars: int = 210

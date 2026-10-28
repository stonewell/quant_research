"""Portfolio Allocation Backtester

Simulates a portfolio tracking a set of target weights over time.
Accounts for:
1. Daily mark-to-market drift (as asset prices change, their actual weight in the portfolio drifts).
2. Rebalancing costs (commissions/slippage applied only to the turnover required to reach the new target weights).
"""

import contextlib
import contextvars
import logging
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from common.metrics import profit_factor_from_returns, win_rate_from_returns

_logger = logging.getLogger(__name__)

# Lightweight-evaluation switch (task B1.2). `run_allocation_backtest`'s
# `minimal_metrics` argument is the explicit, per-call form. This ContextVar is
# the AMBIENT form: `common/allocation_search.run_ers_validation` wraps its
# 1,000-draw random-portfolio loop in `minimal_metrics_scope()` so a caller's
# opaque `score_fn` (which owns the actual `run_allocation_backtest` call --
# `strategy_generator`'s `_portfolio_score`, `backtester`'s `run_standard`) can
# be accelerated without changing any score_fn signature. It defaults to False,
# so nothing outside that scope is affected.
_MINIMAL_METRICS_CTX = contextvars.ContextVar("quant_minimal_metrics", default=False)


@contextlib.contextmanager
def minimal_metrics_scope(enabled: bool = True):
    """Run everything inside the block with minimal-metric backtests.

    Only meaningfully accelerates ERS-style sampling, where the returned
    frames are thrown away and only the scalar metrics are compared. A context
    variable (not a module global) keeps the setting thread/task-local.
    """
    token = _MINIMAL_METRICS_CTX.set(bool(enabled))
    try:
        yield
    finally:
        _MINIMAL_METRICS_CTX.reset(token)

# Absolute float64 slack allowed on the cash balance. A share ledger must never
# be NEGATIVE (that is margin the strategy did not ask for), but summing
# ~hundreds of applied trade cash flows leaves a few ulps of noise, so the
# feasibility checks compare against `_CASH_FLOAT_TOL * max(1, |cash|)`.
_CASH_FLOAT_TOL = 1e-9

REBALANCE_REPORT_COLUMNS = [
    "rebalance_id",
    "date",
    "symbol",
    "action",
    "price",
    "prior_weight",
    "target_weight",
    "weight_change",
    "trade_value",
    "shares",
    "prior_shares",
    "target_shares",
    "commission",
    "slippage",
    "total_cost",
    "portfolio_equity",
]

# Assertable trace of every order the suspension guard refused (task B1.3).
# A silently-skipped order is indistinguishable from "the strategy never asked
# for that trade", so every refusal is recorded here instead of only logged.
SUSPENSION_REPORT_COLUMNS = [
    "date",
    "symbol",
    "action",
    "reason",
    "volume",
    "target_weight",
]

# Assertable trace of every DEFERRED order attempt (task B3.1). In the
# next-session execution modes (`next_open` / `next_vwap` / `intraday_twap`) an
# order that cannot be filled on its execution day is requeued, not dropped and
# not filled at a price that never traded -- so the deferral itself has to be on
# the record, otherwise "we could not buy for 3 sessions" is indistinguishable
# from "the strategy never asked". `signal_date` is the close the instruction
# was emitted at; `execution_date` is the session on which the attempt failed
# ("" for `no_next_bar`, where no session was left at all).
# Task B3.2 reuses this SAME report (there is deliberately no second pending-order
# ledger) for the liquidity budget: a fill cut short by `max_volume_participation`
# reports `reason="volume_participation_cap"` and keeps its queue slot.
DEFERRAL_REPORT_COLUMNS = [
    "signal_date",
    "execution_date",
    "symbol",
    "action",
    "reason",
    "execution_price",
    "target_weight",
]

# Task B3.1. `close` is the pre-B3.1 engine: the signal row of date T fills at
# T's close. The other three queue the signal at T's close and fill at the next
# session's execution price (open / VWAP / intraday TWAP).
EXECUTION_PRICE_MODES = ("close", "next_open", "next_vwap", "intraday_twap")

# --------------------------------------------------------------------------- #
# (B3.3 / B3.4) DAILY CARRY -- idle-cash repo interest and securities borrow
# --------------------------------------------------------------------------- #
# Both are OPT-IN and both settle in the SAME daily step (step 0 of every bar):
# they mutate the ledger's `cash` balance and NOTHING else, so
# `equity[t] = cash + holdings` keeps holding with no liability side-account.
# With both switches off the step is never even reached, so the default path is
# the pre-B3.3 engine bit for bit.
#
# DAY COUNT: ACTUAL CALENDAR DAYS / 365. This is a disclosed choice, not a free
# parameter: money-market repo -- GC001, DR007, the 国债逆回购 task B3.3 models --
# quotes and accrues on ACT/365, and the cash really IS parked over the weekend
# (a Friday balance earns three days of carry by Monday). A trading-day basis
# (252 or 365 SESSIONS) would silently pay nothing for the calendar a long
# weekend or a Chinese 7-day holiday covers -- precisely the defensive,
# mostly-cash periods this feature exists to model -- and would make the accrual
# depend on the index convention of whatever calendar the caller happened to
# pass. It is also the basis this engine's own CAGR already uses (actual calendar
# days / 365.25, see the note in `run_allocation_backtest`); the 365 vs 365.25
# difference is DISCLOSED and the RATE side keeps the money-market 365.
_CASH_CARRY_DAYS_PER_YEAR = 365.0

# One row per session that actually settled a NON-ZERO carry item. This is a
# reporting mirror, exactly like `suspension_report`/`deferral_report` -- the
# authoritative numbers are the scalar accumulators (`total_cash_interest`,
# `total_borrow_fees`), and a session with nothing to settle writes no row.
CASH_CARRY_REPORT_COLUMNS = [
    "date",
    "calendar_days",
    "cash_interest",
    "borrow_fee",
    "cash_balance",
]

# Cash substitutes: a symbol whose OWN price series already carries the
# risk-free rate (a T-bill ETF's total-return path, a money-market fund unit, a
# 1-day repo contract held as a position). They are used ONLY to DETECT and
# DISCLOSE an overlap with `cash_yield_annual` -- they are never added to, or
# subtracted from, the interest base, which is the plain cash balance. See the
# docstring's "NO DOUBLE COUNTING WITH A CASH SUBSTITUTE" paragraph; a caller
# whose proxies use other tickers can pass `cash_proxy_symbols`.
CASH_PROXY_SYMBOLS = frozenset({
    "BIL", "SGOV", "SHV",                 # US T-bill / short-treasury ETFs
    "511880", "511990", "511660", "511810", "511620",  # China money-market ETFs
    "GC001", "204001", "R001",            # 1-day repo, modelled as a symbol
})

# `ST` / `*ST` / `SST` / `S*ST` name prefixes used by the exchanges to flag
# special-treatment A-shares. Matched at the START of the string only, so an
# English name that merely contains "st" ("Best Inc") is not mis-flagged.
_ST_NAME_RE = re.compile(r"^[Ss]?\*?ST")


def _is_st_marked(symbol: str, name: Optional[str] = None) -> bool:
    """True when `symbol`'s code or `name` carries the ST / *ST marker.

    The marker lives in the DISPLAY NAME, which an OHLCV frame does not carry,
    so a caller that has names must pass them via `run_allocation_backtest`'s
    optional `name_map` (nothing is invented when it is absent -- see that
    parameter's docstring). Some feeds instead prefix the SYMBOL itself with
    the marker (`"ST康得.SZ"`); that form is detected too. Both are only ever
    consulted for symbols that already resolved to an A-share board, so a
    foreign ticker is never re-banded by a stray "ST" substring.
    """
    code = symbol.upper().split(".")[0]
    candidates = [code]
    if name:
        candidates.append(str(name).upper())
    for cand in candidates:
        # NFKC folds full-width latin ("ＳＴ浦发") to ASCII; full-width spaces
        # appear in some Chinese name feeds ("ST 康得").
        normalized = unicodedata.normalize("NFKC", cand).replace(" ", "").replace("\u3000", "")
        if _ST_NAME_RE.match(normalized):
            return True
    return False


def _get_china_price_limit(symbol: str, name: Optional[str] = None) -> float:
    """Returns price limit for China A-shares:
    - 0.05 for ST / *ST special-treatment names (task B1.3)
    - 0.20 for ChiNext (300, 301) and STAR Market (688, 588)
    - 0.30 for BSE (8, 4, 920)
    - 0.10 for Main board (60, 00, 51, 15)
    Returns 1.0 (unlimited) for foreign assets or cash proxies.

    DISCLOSED SIMPLIFICATION: the deployed exchange rules actually keep the
    WIDER band for ST names on the growth boards (ChiNext/STAR ST = 20%, BSE
    ST = 30%) and only tighten to 5% on the main board. Task B1.3 specifies a
    "5% for ST" band, so that is what is implemented for every board; the
    result is CONSERVATIVE (a 20%-band ST growth-board name can only look
    limit-locked here if it moves >= 4.5% while closing on its high/low, i.e.
    some genuine 20%-board trades are refused). Pass a `name_map` and, if an
    exact growth-board band is ever required, thread the board through here.
    """
    sym = symbol.upper().split(".")[0]
    if not any(symbol.upper().endswith(sfx) for sfx in (".SH", ".SZ", ".BJ")) and not sym.isdigit():
        return 1.0
    if _is_st_marked(symbol, name):
        return 0.05
    if sym.startswith(("300", "301", "688", "588")):
        return 0.20
    if sym.startswith(("8", "4", "920")):
        return 0.30
    if sym.startswith(("60", "00", "51", "15")):
        return 0.10
    return 0.10


HK_COMMON_BOARD_LOTS = {
    "00005": 400, "0005": 400,   # HSBC Holdings
    "00175": 1000, "0175": 1000, # Geely Auto
    "01211": 500, "1211": 500,   # BYD Company
    "01810": 200, "1810": 200,   # Xiaomi
    "00941": 500, "0941": 500,   # China Mobile
    "09618": 50, "9618": 50,     # JD.com
    "02318": 500, "2318": 500,   # Ping An
    "00700": 100, "0700": 100,   # Tencent
    "09988": 100, "9988": 100,   # Alibaba
    "03690": 100, "3690": 100,   # Meituan
    "00883": 1000, "0883": 1000, "883": 1000,   # CNOOC
    "01088": 500, "1088": 500,                  # China Shenhua
    "00857": 2000, "0857": 2000, "857": 2000,   # PetroChina
    "00728": 2000, "0728": 2000, "728": 2000,   # China Telecom
    "00939": 1000, "0939": 1000, "939": 1000,   # China Construction Bank
    "01398": 1000, "1398": 1000,                # ICBC
    "03988": 1000, "3988": 1000,                # Bank of China
    "02628": 1000, "2628": 1000,                # China Life
    "01919": 500, "1919": 500,                  # COSCO Shipping Holdings
    "00388": 100, "0388": 100, "388": 100,      # HKEX
    "00002": 500, "0002": 500, "2": 500,        # CLP Holdings
    "00144": 2000, "0144": 2000, "144": 2000,   # China Merchants Port
    "03311": 2000, "3311": 2000,                # China State Construction Intl
    "02899": 2000, "2899": 2000,                # Zijin Mining
    "00981": 500, "0981": 500, "981": 500,      # SMIC
    "09999": 100, "9999": 100,                  # NetEase
    "09888": 50, "9888": 50,                    # Baidu
    "02015": 100, "2015": 100,                  # Li Auto
    "01299": 200, "1299": 200,                  # AIA
}


def _get_hk_board_lot(symbol: str, default_lot: int = 100) -> int:
    """Returns the board lot for an HKEX stock.
    Defaults to default_lot (normally 100) if not in the lookup table."""
    code = symbol.upper().split(".")[0].lstrip("0") or "0"
    raw_code = symbol.upper().split(".")[0]
    return HK_COMMON_BOARD_LOTS.get(raw_code, HK_COMMON_BOARD_LOTS.get(code, default_lot))


def _validate_annual_rate(name: str, value, *, minimum: float,
                          include_minimum: bool = False) -> Optional[float]:
    """Validate an ANNUALIZED FRACTION (`0.02` = 2%/yr) and normalise it to float.

    DISCLOSED UNIT: these are fractions of 1, not percent. The task table writes
    `--cash-sweep-rate 0.02` for "2%", and the kwarg is named `*_annual` (not
    `*_pct`) precisely so nobody passes `4.5` meaning 4.5% and gets 450%/yr. A
    `bool` is rejected too (`True` would otherwise silently read as 100%/yr).

    `include_minimum=True` is how `borrow_fee_annual=0.0` stays legal (a zero fee
    is bit-identical to switching the feature off), while the cash rate keeps a
    strict bound at whatever floor the caller sets.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(
            f"{name} must be an annualized FRACTION of 1 (0.02 = 2%/yr) or None "
            f"(None = feature off), got {value!r}"
        )
    rate = float(value)
    op = ">=" if include_minimum else ">"
    below = rate < float(minimum) if include_minimum else rate <= float(minimum)
    if not np.isfinite(rate) or below:
        raise ValueError(
            f"{name} must be a finite annualized fraction {op} {minimum} "
            f"(0.02 = 2%/yr), got {value!r}"
        )
    return rate


def _daily_cash_interest(
    cash: float,
    annual_rate: Optional[float],
    days: float,
    basis: float = _CASH_CARRY_DAYS_PER_YEAR,
) -> float:
    """Interest credited on the UNINVESTED cash balance for `days` calendar days.

    ``interest = cash * annual_rate * days / basis`` (ACT/365, see
    `_CASH_CARRY_DAYS_PER_YEAR`). The credit lands in the CASH ledger itself, i.e.
    it is part of `cash[t]` and therefore of `equity[t] = cash + holdings` on
    every later day -- not a separate accrual bucket (task B3.3 requirement 4).

    A NEGATIVE or non-finite balance NEVER accrues. `cash < 0` is margin the
    strategy never asked for (B1.1 forbids it), and paying interest on a debt
    balance would reward being levered; `_settle_daily_carry` asserts the
    invariant, and this clamp is the second line of defence. Same for a
    non-positive day count (two sessions on the same calendar day have no
    overnight interval to accrue).
    """
    if annual_rate is None:
        return 0.0
    base = float(cash)
    if not np.isfinite(base) or base <= 0.0:
        return 0.0
    dt = float(days)
    if not np.isfinite(dt) or dt <= 0.0:
        return 0.0
    return float(base * float(annual_rate) * dt / float(basis))


def _daily_borrow_fee(
    short_notional: float,
    annual_rate: Optional[float],
    days: float,
    basis: float = _CASH_CARRY_DAYS_PER_YEAR,
) -> float:
    """Securities-borrow fee charged on `short_notional` for `days` calendar days.

    ``fee = short_notional * annual_rate * days / basis`` (ACT/365, the same basis
    the cash leg uses, so a net-carry line is internally consistent).

    The charge is a CASH OUTFLOW on the portfolio, never a fee added to a trade's
    `total_cost`: no security changed hands to accrue it. `short_notional` is the
    market value of the borrowed shares marked at the PREVIOUS session's close
    (see `_settle_daily_carry`), so a flat/covered book simply charges nothing.
    """
    if annual_rate is None:
        return 0.0
    base = float(short_notional)
    if not np.isfinite(base) or base <= 0.0:
        return 0.0
    dt = float(days)
    if not np.isfinite(dt) or dt <= 0.0:
        return 0.0
    return float(base * float(annual_rate) * dt / float(basis))


def run_allocation_backtest(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
    min_shares: int = 1,
    min_weight_change: float = 0.0,
    china_trading: bool = False,
    stamp_duty_pct: float = 0.0005,
    us_trading: bool = False,
    hk_trading: bool = False,
    hk_stamp_duty_pct: float = 0.001085,
    sec_fee_pct: float = 0.0000278,
    name_map: Optional[Dict[str, str]] = None,
    minimal_metrics: bool = False,
    execution_price: str = "close",
    intraday_data: Optional[Dict[str, pd.DataFrame]] = None,
    market_impact: bool = False,
    impact_gamma: float = 1.0,
    impact_vol_window: int = 20,
    impact_adv_window: int = 20,
    max_volume_participation: Optional[float] = None,
    cash_yield_annual: Optional[float] = None,
    borrow_fee_annual: Optional[float] = None,
    cash_proxy_symbols: Optional[Iterable[str]] = None,
    enable_position_lifecycle: bool = False,
    lifecycle_config: Optional[Any] = None,
    lifecycle_records: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Simulates portfolio equity curve given daily target weights.

    `target_weights` is a SPARSE DataFrame indexed by date, with columns for
    each symbol: a row is NaN except on an actual rebalance date, where it
    holds the target portfolio fraction (0.0 to 1.0) for that symbol. Do not
    pass an already-forward-filled frame -- a template can legitimately
    recompute the SAME target on consecutive rebalance dates (e.g.
    equal-weight always targets 1/N), and this backtester tells "a rebalance
    was instructed" apart from "no rebalance today" by whether the row is
    present at all, not by whether its value differs from the previous row.

    The backtester assumes trading happens at the CLOSE of the day a
    rebalance is instructed.

    `min_shares` defines the minimal amount of shares trading each time
    (default: 1). Fractional share trading is not allowed; all traded share
    quantities and holdings are integer share amounts.

    `min_weight_change`: minimum weight difference |delta_w| required to trigger
    a rebalance on an already-held asset (default: 0.0). Suppresses sub-threshold
    passive price drift rebalances when an asset's position has not intentionally
    changed, while allowing new entries (prior_s == 0) and full exits (target_w == 0).

    `china_trading`: applies China A-share trading rules:
    - Board price limits: blocks BUY orders on limit-up (10%/20%/30%),
      and blocks SELL orders on limit-down.
    - T+1 settlement: shares bought today cannot be sold until next day.
    - 100-share round lot: auto-upgrades `min_shares` to at least 100.
    - Sell-side stamp duty: applies `stamp_duty_pct` (default 5 bps) only on sells.

    `us_trading`: applies US market rules:
    - 1-share lot sizing (`min_shares = 1`).
    - Unconstrained price limits.
    - T+0 intraday day-trading.
    - SEC Section 31 fee on SELL orders only (`sec_fee_pct`, default 0.00278%).

    `hk_trading`: applies Hong Kong market rules:
    - Board lot sizing (typically 100 shares, stock-specific lookup).
    - Unconstrained price limits.
    - T+0 intraday day-trading.
    - Dual-sided stamp duty and levies on BOTH BUY and SELL orders (`hk_stamp_duty_pct`, default 0.1085%).

    `name_map`: optional `{symbol: display_name}` used ONLY by the China ST
    detection (task B1.3). OHLCV frames carry no display name, and the ST
    marker lives there, so it cannot be inferred from `universe` -- without a
    `name_map` only a marker embedded in the symbol string itself (`"ST康得.SZ"`)
    is detected, and nothing is guessed. Nothing about the load itself changes
    when this is None.

    SUSPENSION GUARD (task B1.3): an A-share bar with `Volume <= 0`, or with a
    missing/NaN `Volume`, is treated as SUSPENDED (停牌), and BOTH directions
    are refused -- a suspended stock cannot be bought and cannot be sold. This
    is deliberately separate from the missing-PRICE path above: a NaN close
    still marks the position at its last known close (equity carries on, the
    symbol is not de-listed), but NO fill may be booked at that price in
    either case. Every refusal is appended to the returned `suspension_report`
    (`date, symbol, action, reason, volume, target_weight`), so a blocked
    order can never be mistaken for "the strategy never asked".

    DISCLOSED LIMIT OF THE GUARD: a symbol whose OHLCV frame has NO `Volume`
    column at all is treated as "volume unobservable" and tradable, because a
    price-only series (benchmark proxies, cash legs, the synthetic fixtures
    used across this repo's tests) carries no suspension information and
    rejecting it would silently turn every such caller into an all-cash book.
    The guard fires on positive evidence of a non-trading bar -- a `Volume`
    column that is present and is NaN/<= 0 -- never on absent volume data.

    `minimal_metrics`: lightweight evaluation mode (task B1.2). When True --
    or when the ambient `minimal_metrics_scope()` is active -- the engine
    skips building the two large per-day frames (`rebalance_report` and
    `actual_weights`, both returned EMPTY but with their proper columns) and
    skips materialising one trade dict per fill. The LEDGER IS UNCHANGED: cash,
    shares and `equity[t]` are still advanced every day, `equity_curve` is
    still built, and every scalar metric is computed by the very same code on
    the very same numbers, so `sharpe_ratio` / `cagr` / `max_drawdown` /
    `calmar_ratio` / `win_rate` / `profit_factor` / `total_turnover` /
    `total_rebalances` are BIT-IDENTICAL to the full run (see
    `test_minimal_metrics_equivalence`). It is a reporting-overhead switch,
    not an approximation. Default False = pre-B1.2 behaviour, unchanged.

    EXECUTION-PRICE MODE (task B3.1): `execution_price` chooses WHEN a rebalance
    signal is filled, and therefore separates SIGNAL date from FILL date.

    * `"close"` (default): the signal row dated T is filled at T's CLOSE. This
      is the pre-B3.1 engine, bit-for-bit; `rebalance_report["date"]`,
      `actual_weights` and `equity_curve` all refer to that same day T.
    * `"next_open"`: the signal row dated T is only QUEUED at T's close and is
      filled at the OPEN of the next session T+1. The fill is booked on T+1 --
      `rebalance_report["date"]` is the EXECUTION date (T+1, never the signal
      date), the rebalance-day `actual_weights` row is T+1 (marked at T+1's
      close) and T's own row shows the untouched, merely drifted book.
      `equity_curve` is marked at every day's close in every mode, so the
      overnight gap between T's close and T+1's open lands in the equity curve
      exactly once, BEFORE the fill. A day-0 signal fills at day 1's open (day
      0 is then all cash).
    * `"next_vwap"`: same queueing/timing as `next_open`, filled at the
      execution day's volume-weighted average price, taken from a `VWAP`
      column or from `Amount` + `Volume`. Raises ValueError, naming the
      symbols, when no frame supplies either.
    * `"intraday_twap"`: same timing, filled at the mean of the execution day's
      5-minute typical prices `(H+L+C)/3` supplied via `intraday_data`. Raises
      ValueError when that argument is not supplied.

    Neither `next_vwap` nor `intraday_twap` ever degrades to another price: a
    backtest silently measured at a different price than the one requested is
    exactly the distortion these modes exist to expose.

    `rebalance_report["portfolio_equity"]` is the pre-trade mark used for
    sizing -- the day's CLOSE in `close` mode, the execution-day OPEN (or VWAP
    / TWAP mark) in the `next_*` modes, i.e. the portfolio value at the moment
    the trade was sized, so `weight_change x portfolio_equity` reconciles with
    the notional actually filled.

    Deferral (顺延): an order that cannot be filled on its execution day is
    neither dropped nor filled at a price that never traded. It stays queued
    and is re-attempted on the next session until it fills or a newer signal
    row supersedes it. Every deferred attempt is appended to the returned
    `deferral_report` (`signal_date, execution_date, symbol, action, reason,
    execution_price, target_weight`) with `reason` one of
      - `volume_zero` / `volume_missing`: B1.3 suspension on the execution bar
        (ALSO recorded in `suspension_report`, whose valuation-still-uses-last-
        close rule is untouched -- the position keeps being marked, only the
        fill is refused);
      - `open_limit_up` / `open_limit_down`: 一字板 -- the bar OPENED on its
        board limit and never traded away from it, so nothing could cross at
        the open. The band comes from `_get_china_price_limit`, hence ST names
        use 5% and ChiNext/STAR 20% (only applied under `china_trading`);
      - `price_missing`: no fill price for that symbol on that day;
      - `intraday_missing`: no TWAP available for that symbol on that day;
      - `superseded`: a newer signal arrived before the order could fill;
      - `volume_participation_cap` (task B3.2): the order could not be filled
        IN FULL within the session's participation budget `volume_today x
        max_volume_participation`; the unfilled remainder stays queued (see
        MARKET IMPACT / LIQUIDITY CAP below);
      - `no_next_bar`: the signal sits on the LAST bar, so no session is left
        to fill it (the order is reported, never silently dropped).
    A queued order never touches the ledger, so `cash >= 0`,
    `equity[t] == cash + holdings@close` and `sum(actual_w) <= 1` hold on every
    day of every mode. DISCLOSED: a BUY capped by available cash on its
    execution day is FINAL for that instruction (same rule as `close` mode) --
    it is not requeued, so a cash-starved instruction cannot re-buy day after
    day as cash trickles in.

    MARKET IMPACT + LIQUIDITY CAP (task B3.2) -- OFF BY DEFAULT. Both features
    are gated on explicit arguments, so the default configuration is the
    pre-B3.2 engine BIT FOR BIT (pinned by
    `test_market_impact_default_path_is_bit_identical`).

    `market_impact` (default False) charges an Almgren-Chriss SQUARE-ROOT
    temporary impact on every fill::

        impact_fraction = sigma * gamma * sqrt(Shares / ADV20)
        impact_cost     = trade_value * impact_fraction

    i.e. the cost is a FRACTION OF NOTIONAL that scales with the square root of
    the order's size relative to the symbol's average daily volume.
    * `sigma` is the symbol's DAILY return volatility, the standard deviation of
      simple daily returns over `impact_vol_window` sessions (default 20),
      taken through the PREVIOUS session (`.shift(1)`) -- strictly causal.
    * `ADV20` is the mean of the symbol's `Volume` over the `impact_adv_window`
      sessions strictly BEFORE the fill day (default 20). Today's own volume
      never enters it, so the impact model cannot see the print it is pricing.
    * `gamma` (`impact_gamma`, default 1.0) is the dimensionless prefactor.

    LITERATURE. The square-root law -- impact proportional to
    `sigma * sqrt(Q / V)`, i.e. to the square root of participation -- is the
    empirical regularity reported by Almgren, Thum, Hauptmann & Li (2005),
    "Direct estimation of equity market impact" (Risk 18(7)), and by Toth,
    Lemperiere, Deremble, de Lataillade, Kockelkoren & Bouchaud (2011),
    "Anomalous price impact and the critical nature of liquidity in financial
    markets" (PRX 1, 021006). DISCLOSED APPROXIMATION: (1) the prefactor is not
    fitted to this universe -- Toth et al. put the dimensionless `Y` at order
    UNITY, while Almgren et al.'s parametric fit implies a materially SMALLER
    per-unit coefficient; the default `gamma = 1.0` therefore sits at the
    CONSERVATIVE (pessimistic) end; (2) only the TEMPORARY impact is charged --
    the PERMANENT impact of Almgren-Chriss is ignored (it would have to decay
    through the price path, which this engine marks from the market's own
    closes); (3) `sigma` is a daily-return vol, not the intraday vol the
    references use, and it is charged once per fill rather than integrated over
    an execution schedule; (4) the pre-trade spread cost is the SEPARATE
    `slippage_pct` term, so the two are complementary components of the same
    decomposition, not duplicates (see the accounting note below).

    `max_volume_participation` (default None = no cap) is a HARD liquidity
    constraint: no symbol may trade more than `volume_today x
    max_volume_participation` shares in one session. The unfilled EXCESS is
    neither dropped nor silently shrunk -- it is DEFERRED onto the next
    tradable session through the very same pending-order machinery B3.1
    introduced (no second state machine), and every capped attempt is traced in
    `deferral_report` with `reason == "volume_participation_cap"`. Works in
    every `execution_price` mode: in the `next_*` modes a capped order keeps its
    queue slot, and in `close` mode a capped order is retried at the next
    session's CLOSE. A capped order is re-sized from scratch on each attempt
    (the residual is `target_weight - current_weight` at that session's mark),
    so it converges without needing a stored share count.
    DISCLOSED: the budget uses TODAY's volume, which is only fully known at the
    session's close -- a retrospective capacity check, not a pre-trade forecast
    (the same convention the task specifies). A `Volume <= 0` / missing bar is a
    SUSPENSION (B1.3) and is refused by that guard FIRST: it yields a refusal,
    never a zero-share budget, so nothing "accumulates" across a halt. A
    cash-limited BUY stays FINAL (B3.1 rule): only the volume-capped part of an
    order is requeued.

    WHERE THE IMPACT COST IS CHARGED, AND ITS RELATION TO `fee`. The impact
    cost leaves the CASH balance exactly like commission and slippage do, i.e.
    it is a real reduction of equity on the fill (`cash -= notional +
    total_cost`, `total_cost` now containing it), and never a share-count
    adjustment: `price` stays the market print, so the position and the trade
    report still reconcile. It is NOT double counted against `commission_pct` /
    `slippage_pct`: those are a fixed commission and a fixed pre-trade spread
    proxy charged on the same notional, while the square-root term is the
    size-dependent execution concession. In `rebalance_report` the impact is
    reported inside the `slippage` column (the "execution cost from price
    concession" column), so `total_cost == commission + slippage + stamp` still
    holds and the ledger replay in `test_ledger_parity` remains valid; the
    result dict additionally reports the impact total separately as
    `total_market_impact` (`0.0` when the model is off).
    Ledger identities are preserved with either feature enabled:
    `equity[t] == cash + sum_i(held_shares_i * close[t,i])`, `cash >= 0`
    (impact-inclusive BUY affordability is solved by a fixed-point shrink, and
    the day-0 entry scales through it), and `sum(actual_w) <= 1`.
    A symbol with NO `Volume` column has neither a usable ADV20 nor a
    participation budget, so it is charged no impact and capped at nothing
    (mirroring B1.3's "absent volume is unobservable" rule); this is disclosed,
    not guessed. On the first session -- and for any session with no earlier
    volume/return yet -- ADV20/sigma are undefined and the impact is 0.0, again
    disclosed rather than back-filled from the future.
    DEGRADED ADV20: a fill that happens before `impact_adv_window` sessions of
    history exist uses the mean of the volume it DOES have (never a future
    value, never a fabricated 20-day average) and logs one WARNING per run
    naming the window and how many sessions fell short.

    CASH CARRY (tasks B3.3 + B3.4) -- BOTH OFF BY DEFAULT. `cash_yield_annual`
    (idle-cash repo interest) and `borrow_fee_annual` (securities-borrow fee on a
    short book) are OPT-IN and default to `None` = switched off, in which case the
    engine is the pre-B3.3 engine BIT FOR BIT (pinned by
    `test_cash_and_borrow_are_off_by_default_and_the_default_path_is_bit_identical`,
    whose snapshots include seven taken from the pre-edit tree and B3.2's eight
    pre-B3.2 ones). DISCLOSED DEVIATION from the task table, which lists defaults
    of `--cash-sweep-rate 0.0` and `borrow_fee_pct 4.5%`: the DEFAULT is off,
    because a default-on charge would move every existing backtest and every
    strategy ranking. The plan's numbers are the values to pass when the feature
    is wanted, and they are documented as such rather than applied silently.

    `cash_yield_annual` credits interest on the UNINVESTED CASH BALANCE::

        interest = cash[t-1] * cash_yield_annual * calendar_days / 365

    `borrow_fee_annual` charges the borrow on a SHORT book::

        fee = sum_i(max(0, -held_shares_i) * close[t-1, i]) * borrow_fee_annual
              * calendar_days / 365

    Both are ANNUALIZED FRACTIONS of 1 (`0.02` = 2%/yr, `0.045` = 4.5%/yr), never
    percent; a bad value raises ValueError instead of silently disabling the
    feature. `0.0` is legal and is bit-identical to `None` (the term is exactly
    zero).

    DAY COUNT -- ACTUAL CALENDAR DAYS / 365. Money-market repo (GC001, DR007)
    quotes and accrues on ACT/365, and the cash really is parked over the
    weekend: a Friday balance earns THREE days of carry by Monday, and an
    all-cash book over a Chinese 7-day holiday earns seven. A trading-day basis
    (252 or 365 sessions) would silently pay nothing for those calendar days --
    exactly the defensive, mostly-cash windows B3.3 exists to model -- and would
    tie the accrual to whatever calendar convention the caller's index happened
    to use. The gap is taken as `(date[t] - date[t-1]).days` in CALENDAR dates
    (tz/DST-safe; two sessions on the same calendar date accrue nothing rather
    than inventing a day) and the rate side uses 365, not this file's CAGR
    convention of 365.25 -- disclosed, and the 365 is the money-market one.

    SETTLEMENT STEP, NOT A SECOND STATE MACHINE: both items settle as step 0 of
    the EXISTING per-bar loop, on the OPENING book (the cash and the positions
    carried OVERNIGHT from the previous session's close), before the morning
    mark and before any fill. Consequences, all deliberate and all tested:
      * the balance is `cash` only -- interest is created and the fee destroyed
        inside the same `cash` variable the trade path already mutates, so
        `equity[t] = cash + holdings@close` needs no change and cannot drift;
      * a short opened at T's close pays its FIRST fee at T+1, and the session
        on which it is COVERED still pays for the night it was held (the charge
        precedes that session's fills); the session after the cover pays nothing;
      * the rebalance on a later bar is sized against the SETTLED balance, which
        is why enabling the carry can legitimately change a cash-capped fill --
        but never a fill that was not cash-limited;
      * the base mark is the PREVIOUS close, so today's print cannot price today's
        overnight loan (strictly causal).

    WHERE THE CARRY IS CHARGED, AND WHY IT IS NOT DOUBLE COUNTED. Neither item is
    a fill, so neither is written into `rebalance_report`: the borrow fee is not
    a commission and the interest is not a rebate. The fee leaves `cash` on the
    day it accrues (a real reduction of equity that day) and the interest enters
    `cash` on the day it accrues; both are reported as the scalar accumulators
    `total_cash_interest` / `total_borrow_fees` (plus
    `total_borrow_fees_unpaid`, the part a depleted balance could not pay -- the
    charge is clamped at the available cash so `cash >= 0` survives, and the
    shortfall is disclosed rather than financed) and per-day in the new
    `cash_carry_report` (`date, calendar_days, cash_interest, borrow_fee,
    cash_balance`). The relationship to the execution costs is one of DISJOINT
    ACCOUNTS, not overlap: `commission_pct`, `slippage_pct`, the stamp/SEC levies
    and the B3.2 square-root impact are all charged per FILL on the traded
    notional, while the borrow fee is a time-based FINANCING cost on the borrowed
    stock (no security changes hands) and the cash yield is the return on the
    un-lent cash. Enabling either therefore leaves `rebalance_report` -- fills,
    `commission`, `slippage`, `total_cost`, turnover -- BIT-IDENTICAL, and moves
    equity by exactly `total_cash_interest - total_borrow_fees`.

    THE LEDGER IDENTITY SURVIVES BY CONSTRUCTION: the carry is a daily CASH
    entry, not an accrued LIABILITY. `equity[t] = cash[t] + sum_i(held * close)`
    is still the one definition, so an independent replay of the trade report
    matches the engine once the cumulative net carry is added back:

        equity[t] == replay_equity[t] + sum_{s<=t}(interest_s - fee_s)

    (asserted in `test_equity_identity_holds_with_carry_in_every_mode`). A
    liability-style accrual would have had to be SUBTRACTED inside the equity
    formula -- a second accounting path the trade-level replay cannot see, and
    exactly the kind of parallel book this engine spent B1.1 removing.

    NO DOUBLE COUNTING WITH A CASH SUBSTITUTE (B3.3 requirement 2). RULE:
    COEXIST, and the two yields are on DISJOINT ASSETS. The interest base is the
    plain cash balance; a cash substitute (`BIL`, `511880`, `GC001`, ... -- see
    `CASH_PROXY_SYMBOLS`, overridable via `cash_proxy_symbols`) is an ordinary
    POSITION whose carry arrives through its own price series. Its market value
    is never added to the base, and the yield never touches its price path, so no
    configuration can credit the same dollar twice -- they are not made mutually
    exclusive because a book legitimately holds both a T-bill sleeve and
    un-swept cash. What the engine does add is DETECTION: when interest is
    credited while a proxy is held, `cash_yield_proxy_overlap` is set True and one
    WARNING names the situation, so a caller that ALSO baked the risk-free rate
    into the proxy's series cannot silently double count across the two runs.

    A NEGATIVE `cash` NEVER EARNS: the settlement step asserts `cash >= -tol`
    (B1.1 guarantees it) and clamps the base at 0.0, so a broken ledger fails
    loudly instead of being paid interest on a margin balance.

    ACCOUNTING MODEL (share ledger, task B1.1): the portfolio is simulated as a
    real ledger -- a cash balance plus an integer (or, with `min_shares=0`,
    fractional) share position per symbol. Both `equity_curve` and
    `actual_weights` are DERIVED from that ledger every day::

        equity[t]        = cash[t] + sum_i(held_shares_i * close[t, i])
        actual_w[t, i]   = held_shares_i * close[t, i] / equity[t]

    so `sum(actual_w) <= 1` (up to float noise) and the weight frame can never
    disagree with the share/trade report. Round-lot truncation and
    cash-constrained partial fills therefore show up honestly as idle cash
    instead of being rounded back up to the instruction.

    SHORT BOOKS AND THE SIGN CONVENTION (task B3.5): a NEGATIVE target weight is
    a genuine short -- `held_shares` goes negative and its market value is
    negative. Every fill is booked as a SIGNED share delta taken from its
    direction, never from the sign of the prior position: a BUY adds `+trade_s`,
    a SELL adds `-trade_s`, so reducing a long and extending a short are the
    same arithmetic (`new_s = prior_s - trade_s`) and covering a short is the
    same as buying (`new_s = prior_s + trade_s`). The cash flow follows the same
    signed delta, which is why a short sale CREDITS the account and a cover
    DEBITS it, and why `equity[t] = cash + sum(held * close)` stays true for a
    long/short book. Pre-B3.5 the sell path branched on `prior_s`, which inverted
    both the share count and the cash on the short side.

    `cash >= 0` on every path, fees included: a long entry of notional `N`
    costs `N * (1 + rate)`, so the entry is sized off `cash / (1 + rate)` and
    the rebalance-day buy cap likewise divides the free balance by
    `(1 + rate)` before lot-rounding. (The first B1.1 cut spent `cash` on
    notional and then billed the fee to the same account, leaving
    `cash = -fee`, `sum(actual_w) = 1 + fee`, and a spurious 1-share trim at
    the next rebalance; that fee-financed construct is gone.) One DISCLOSED
    deviation remains: a long/short book whose GROSS target exposure exceeds
    1.0 is not silently renormalised in the reported weights -- only the
    CASH-FEASIBLE long side is scaled down.
    """
    if sum([bool(china_trading), bool(us_trading), bool(hk_trading)]) > 1:
        raise ValueError("Only one of china_trading, us_trading, hk_trading may be enabled.")

    if china_trading and min_shares == 1:
        min_shares = 100
    elif hk_trading and min_shares == 1:
        min_shares = 100
    elif us_trading and min_shares != 0:
        min_shares = 1

    if isinstance(min_shares, bool) or not isinstance(min_shares, (int, np.integer)) or min_shares < 0:
        raise ValueError(f"min_shares must be an integer >= 0, got {min_shares!r}")

    # B1.2: either the explicit per-call flag or the ambient ERS scope.
    minimal_metrics = bool(minimal_metrics) or bool(_MINIMAL_METRICS_CTX.get())

    # ------------------------------------------------------------------ #
    # (B3.2) MARKET-IMPACT / LIQUIDITY-CAP ARGUMENT VALIDATION
    # ------------------------------------------------------------------ #
    # Both features are OFF by default and the whole block below is inert
    # unless a caller opts in, so the default path stays bit-identical to the
    # pre-B3.2 engine.
    market_impact = bool(market_impact)
    if max_volume_participation is not None:
        if isinstance(max_volume_participation, bool) or not isinstance(
            max_volume_participation, (int, float, np.integer, np.floating)
        ):
            raise ValueError(
                "max_volume_participation must be a fraction in (0, 1] or None "
                f"(None = no participation cap), got {max_volume_participation!r}"
            )
        max_volume_participation = float(max_volume_participation)
        if not np.isfinite(max_volume_participation) or not (0.0 < max_volume_participation <= 1.0):
            raise ValueError(
                "max_volume_participation must be in (0, 1] -- it is the fraction of a "
                f"session's volume a single order may consume, got {max_volume_participation!r}"
            )
    if market_impact:
        impact_gamma = float(impact_gamma)
        if not np.isfinite(impact_gamma) or impact_gamma < 0.0:
            raise ValueError(f"impact_gamma must be a finite float >= 0, got {impact_gamma!r}")
        for _name, _w in (("impact_vol_window", impact_vol_window),
                          ("impact_adv_window", impact_adv_window)):
            if isinstance(_w, bool) or not isinstance(_w, (int, np.integer)) or int(_w) < 1:
                raise ValueError(f"{_name} must be an integer >= 1, got {_w!r}")
        impact_vol_window = int(impact_vol_window)
        impact_adv_window = int(impact_adv_window)

    # ------------------------------------------------------------------ #
    # (B3.3 / B3.4) CASH-CARRY ARGUMENT VALIDATION
    # ------------------------------------------------------------------ #
    # Both default to None = OFF, and the whole block is inert on the default
    # path (nothing is even allocated when both are None -- see `carry_enabled`
    # below), so the default numbers stay the pre-B3.3 engine's.
    cash_yield_annual = _validate_annual_rate("cash_yield_annual", cash_yield_annual, minimum=-1.0)
    borrow_fee_annual = _validate_annual_rate(
        "borrow_fee_annual", borrow_fee_annual, minimum=0.0, include_minimum=True)
    if cash_proxy_symbols is not None:
        if isinstance(cash_proxy_symbols, str):
            # A bare string would iterate into characters ("BIL" -> {"B","I","L"})
            # and silently detect nothing. Refuse instead of guessing.
            raise ValueError(
                "cash_proxy_symbols must be an iterable of tickers (e.g. {'BIL', '511880'}) "
                f"or None, not a bare string ({cash_proxy_symbols!r})"
            )
        cash_proxy_symbols = frozenset(str(s).upper() for s in cash_proxy_symbols)

    symbols = list(universe.keys())
    if not symbols or target_weights.empty:
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}

    # Extract aligned close, high, low prices
    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})
    highs = pd.DataFrame({sym: df["High"] if "High" in df.columns else df["Close"] for sym, df in universe.items()})
    lows = pd.DataFrame({sym: df["Low"] if "Low" in df.columns else df["Close"] for sym, df in universe.items()})

    # Ensure target_weights and closes are aligned
    common_idx = closes.index.intersection(target_weights.index)
    if len(common_idx) == 0:
        # target_weights and the universe's OHLCV cover disjoint date ranges
        # (e.g. a template's rebalance dates were computed against a
        # different calendar). Without this check, `equity[0] = initial_capital`
        # below indexes into a zero-length array and raises IndexError.
        return {"equity_curve": pd.DataFrame(), "turnover": 0.0}
    closes = closes.loc[common_idx]
    highs = highs.loc[common_idx]
    lows = lows.loc[common_idx]
    sparse_weights = target_weights.reindex(columns=symbols).loc[common_idx]

    lifecycle_records_out: List[Dict[str, Any]] = []
    lifecycle_simulated = False
    if enable_position_lifecycle:
        # DISCLOSURE: strategy_generator candidate search runs with position lifecycle OFF
        # to preserve an unconstrained search landscape; final backtests run with it ON.
        #
        # (F4) A NON-EMPTY `lifecycle_records` is a caller-supplied, already
        # simulated record list (e.g. `run_backtest.py` warming the FSM over a
        # fold's pre-eval buffer) -- it may be passed straight through, because
        # re-simulating would double-filter. An EMPTY list is NOT that: it used
        # to take the same branch, so `lifecycle_records=[]` silently skipped
        # the state machine and returned the RAW, unfiltered weights while still
        # reporting `position_lifecycle_enabled=True` -- a run that claims to
        # have modelled tenure locks and cooldowns but did not. Empty/None now
        # falls through to the simulation.
        if lifecycle_records is not None and len(lifecycle_records) > 0:
            lifecycle_records_out = list(lifecycle_records)
            lifecycle_simulated = True
        else:
            from common.position_lifecycle import resolve_position_lifecycle_config, simulate_position_lifecycle
            cfg = lifecycle_config if lifecycle_config is not None else resolve_position_lifecycle_config()
            market_type = "china" if china_trading else ("us" if us_trading else ("hk" if hk_trading else "other"))
            _proxy_syms = CASH_PROXY_SYMBOLS if cash_proxy_symbols is None else cash_proxy_symbols
            resolved_cash_proxy = next((s for s in symbols if str(s).upper() in _proxy_syms), None)
            sparse_weights, lifecycle_records_out = simulate_position_lifecycle(
                universe=universe,
                target_weights=sparse_weights,
                cfg=cfg,
                market=market_type,
                cash_proxy=resolved_cash_proxy,
            )
            lifecycle_simulated = True

    # Suspension input (task B1.3). Only symbols that actually CARRY a Volume
    # column can be judged -- see the docstring's "DISCLOSED LIMIT OF THE
    # GUARD". A symbol's bars that fall outside `common_idx`, or that are
    # absent from its own frame, reindex to NaN and are therefore treated as
    # suspended (the conservative direction).
    volume_cols = {sym: df["Volume"] for sym, df in universe.items() if "Volume" in df.columns}
    volume_known = np.array([sym in volume_cols for sym in symbols], dtype=bool)
    if volume_cols:
        # ALWAYS reindexed to the full `symbols` column order (NaN for symbols
        # that supplied no Volume) so `volume_arr[day, i]` lines up with
        # `symbols[i]`; `volume_known` -- not the NaN -- decides whether a
        # symbol is judgeable at all.
        volume_arr = (
            pd.DataFrame({sym: pd.to_numeric(vol, errors="coerce") for sym, vol in volume_cols.items()})
            .reindex(common_idx)
            .reindex(columns=symbols)
            .to_numpy(dtype=float)
        )
    else:
        volume_arr = None

    # ------------------------------------------------------------------ #
    # (B3.1) EXECUTION-PRICE PLANNING -- signal date vs. fill date
    # ------------------------------------------------------------------ #
    # Default `close` keeps the historical engine untouched: every line below
    # is skipped, so the numbers are bit-identical to pre-B3.1 (pinned by
    # `test_execution_price_close_mode_is_bit_identical`).
    if execution_price not in EXECUTION_PRICE_MODES:
        raise ValueError(
            f"execution_price must be one of {list(EXECUTION_PRICE_MODES)}, got {execution_price!r}."
        )
    next_day_execution = execution_price != "close"

    # Raw (NOT forward-filled) Open matrix: a day with no open print must not be
    # filled, so NaN has to survive into the execution step. It is also the
    # board-limit test's reference point for every `next_*` mode (the board
    # state is a property of the bar, not of the chosen fill price). Built only
    # for the `next_*` modes so the default path allocates nothing extra.
    opens_arr = None
    if next_day_execution:
        opens = pd.DataFrame({
            sym: (df["Open"] if "Open" in df.columns else df["Close"]) for sym, df in universe.items()
        })
        opens_arr = opens.reindex(index=common_idx, columns=symbols).to_numpy(dtype=float)

    exec_price_arr = None
    if execution_price == "next_open":
        exec_price_arr = opens_arr
    elif execution_price == "next_vwap":
        # DEPENDENCY (M3.6 review P2): this branch needs a per-bar VWAP, i.e. either the `VWAP`
        # column or `Amount` + `Volume`. `DuckDBCache.query_bars` deliberately returns the
        # required OHLCV columns only by default (`columns=None`), so `Amount` is present here
        # ONLY because the loader asked for it: `load_universe` defaults to `columns="all"`
        # (`common/data.py`), which is what every backtest goes through
        # (`common/cli_utils.load_universe_with_banner` -> `load_universe`). A caller that builds
        # `universe` some other way must carry `VWAP` or `Amount` itself, or this mode fails loud
        # below -- it never degrades to the open/close. Do not "fix" a missing `Amount` here by
        # widening the cache's default columns: the narrower default is what keeps a hit-path read
        # from changing frame shape.
        vwap_cols, vwap_missing = {}, []
        for sym, df in universe.items():
            vol = pd.to_numeric(df["Volume"], errors="coerce") if "Volume" in df.columns else None
            if "VWAP" in df.columns:
                vwap_cols[sym] = pd.to_numeric(df["VWAP"], errors="coerce")
            elif "Amount" in df.columns and vol is not None:
                # VWAP = turnover / shares. A zero-volume bar has no VWAP at all
                # (0/0), so it becomes NaN -> no fill -> deferral, never a
                # division-by-zero price.
                vwap_cols[sym] = pd.to_numeric(df["Amount"], errors="coerce") / vol.where(vol > 0)
            else:
                vwap_missing.append(sym)
        if vwap_missing:
            raise ValueError(
                "execution_price='next_vwap' needs a per-bar VWAP for every symbol: either a "
                "`VWAP` column, or `Amount` + `Volume` (VWAP = Amount / Volume). Missing for "
                f"{sorted(vwap_missing)}. Refusing to fall back to another price -- a VWAP-mode "
                "backtest measured at the open/close would misreport execution quality. "
                "Modes usable without extra data: 'close', 'next_open'."
            )
        exec_price_arr = (
            pd.DataFrame(vwap_cols).reindex(index=common_idx, columns=symbols).to_numpy(dtype=float)
        )
    elif execution_price == "intraday_twap":
        if not intraday_data:
            raise ValueError(
                "execution_price='intraday_twap' needs 5-minute (or finer) bars, passed as "
                "`intraday_data={symbol: DataFrame}` -- this engine only receives DAILY OHLCV, so "
                "there is no intraday print to average. Refusing to fall back to the daily "
                "open/close (that would silently be a different mode). Load 5m data (see "
                "common/intraday_features.load_intraday_5m_universe) or use 'close'/'next_open'/"
                "'next_vwap'."
            )
        twap_cols = {}
        for sym in symbols:
            idf = intraday_data.get(sym)
            if idf is None or len(idf) == 0:
                continue
            if {"High", "Low", "Close"} <= set(idf.columns):
                typical = (idf["High"].astype(float) + idf["Low"].astype(float) + idf["Close"].astype(float)) / 3.0
            elif "Close" in idf.columns:
                typical = idf["Close"].astype(float)
            else:
                continue
            # One TWAP per calendar session: equal-weighted over the day's bars
            # (DISCLOSED APPROXIMATION of a true time-weighted average -- the
            # 5m grid is equally spaced, so equal-weighting the bars is the TWAP
            # of typical prices, not of traded volume).
            twap_cols[sym] = typical.groupby(typical.index.normalize()).mean()
        if not twap_cols:
            raise ValueError(
                "execution_price='intraday_twap' got `intraday_data` but none of the requested "
                f"symbols {sorted(symbols)} supplied usable intraday bars (need a DatetimeIndex "
                "plus High/Low/Close, or Close)."
            )
        exec_price_arr = (
            pd.DataFrame(twap_cols).reindex(index=common_idx, columns=symbols).to_numpy(dtype=float)
        )

    # A row with ANY non-NaN value is an explicit rebalance instruction for
    # that date -- computed BEFORE forward-filling, since forward-filling (or
    # a template recomputing an identical target) would otherwise erase the
    # one signal that tells "rebalanced to the same weight" apart from
    # "nothing happened".
    is_rebalance = sparse_weights.notna().any(axis=1).to_numpy()
    target_weights = sparse_weights.ffill().fillna(0.0)

    # Calculate daily returns for all assets
    returns = closes.pct_change().fillna(0.0)

    n_days = len(common_idx)

    # ------------------------------------------------------------------ #
    # (B3.3 / B3.4) CALENDAR GAPS + CASH-PROXY MASK -- built only when a carry
    # item is switched on, so the default path allocates nothing at all.
    # ------------------------------------------------------------------ #
    # `session_days[t-1]` is the number of CALENDAR days between session t-1 and
    # session t: 1 for consecutive business days, 3 into a Monday, 7 across a
    # Chinese Golden Week. That is the day count the interest and the borrow fee
    # accrue on (ACT/365, see `_CASH_CARRY_DAYS_PER_YEAR`). `date()` subtraction
    # (not the nanosecond diff) keeps it tz/DST-safe and gives 0 -- accruing
    # nothing -- for two sessions on the same calendar date rather than inventing
    # a day.
    carry_enabled = cash_yield_annual is not None or borrow_fee_annual is not None
    session_days = None
    proxy_mask = None
    if carry_enabled:
        _session_dates = [ts.date() for ts in common_idx]
        session_days = np.array(
            [max((_session_dates[k] - _session_dates[k - 1]).days, 0) for k in range(1, n_days)],
            dtype=float,
        )
        _proxy_syms = CASH_PROXY_SYMBOLS if cash_proxy_symbols is None else cash_proxy_symbols
        proxy_mask = np.array([str(s).upper() in _proxy_syms for s in symbols], dtype=bool)

    # Arrays for fast simulation
    ret_arr = returns.values
    tgt_w_arr = target_weights.values
    closes_arr = closes.to_numpy(dtype=float)
    highs_arr = highs.to_numpy(dtype=float)
    lows_arr = lows.to_numpy(dtype=float)

    china_limit_pcts = np.array([_get_china_price_limit(s, (name_map or {}).get(s)) for s in symbols], dtype=float) if china_trading else None
    sym_min_shares_arr = np.array([_get_hk_board_lot(s, min_shares) for s in symbols], dtype=int) if (hk_trading and min_shares > 0) else np.full(len(symbols), min_shares, dtype=int)

    # ------------------------------------------------------------------ #
    # (B3.2) ADV20 / SIGMA INPUTS -- built only when the model is enabled
    # ------------------------------------------------------------------ #
    # Both are STRICTLY CAUSAL: the fill on day `t` is priced from
    #   * ADV20  = mean(volume[t-adv_window .. t-1])   (`shift(1)`, so today's
    #     own print is never in it -- the model cannot see the volume it is
    #     charging impact against), and
    #   * sigma  = std(return[.. t-1])                 (`.shift(1)` too).
    # `min_periods=1` gives the DISCLOSED DEGRADED behaviour: a fill with fewer
    # than `impact_adv_window` prior sessions uses the mean of what it has
    # (never a future value) and the shortfall is reported once, per run, at
    # WARNING level rather than silently pretending a 20-day average existed.
    adv20_arr = None
    sigma_arr = None
    # (B3.2) `close` mode has no execution-price matrix, but a participation-
    # capped order retried on a later session is filled by `_execute_pending`,
    # which prices off `exec_price_arr`. Pointing it at the closes makes that
    # retry a close-mode fill -- the same price `close` mode always uses.
    if exec_price_arr is None and max_volume_participation is not None:
        exec_price_arr = closes_arr
    if market_impact:
        if volume_cols:
            prior_vol = (
                pd.DataFrame({sym: pd.to_numeric(v, errors="coerce") for sym, v in volume_cols.items()})
                .reindex(index=common_idx)
                .reindex(columns=symbols)
                .shift(1)
            )
            adv20_arr = (
                prior_vol.rolling(impact_adv_window, min_periods=1).mean()
                .to_numpy(dtype=float)
            )
            short = (prior_vol.notna() & (prior_vol.notna().cumsum() < impact_adv_window))
            n_short = int(short.to_numpy().sum())
            if n_short:
                _logger.warning(
                    "market impact: %d (symbol, session) fill(s) have fewer than %d prior "
                    "sessions of Volume, so ADV%d degrades to the mean of the available prior "
                    "sessions (never a future value, never a fabricated %d-day average).",
                    n_short, impact_adv_window, impact_adv_window, impact_adv_window,
                )
        else:
            _logger.warning(
                "market impact: no symbol supplied a Volume column, so ADV20 is undefined and "
                "the square-root impact is 0 on every fill. Supply Volume to price impact."
            )
        sigma_arr = (
            returns.rolling(impact_vol_window, min_periods=2).std().shift(1)
            .to_numpy(dtype=float)
        )

    # State tracking
    equity = np.zeros(n_days)

    # Ledger state -- single source of truth for the portfolio (task B1.1).
    # `cash` and `held_shares` are mutated only by trades; `equity[t]` and
    # `actual_w[t]` are recomputed from them every day (see the ACCOUNTING
    # MODEL paragraph in the docstring). The pre-B1.1 code instead recursed
    # equity from weights (equity[t] = equity[t-1] * (1 + sum(w * r))) and then
    # FORCE-OVERWROTE the rebalance-day weights with the day's TARGET weights
    # (`new_w[i] = tgt_w_arr[t, i]`). Because the weights were instructions
    # rather than holdings, round-lot idle cash and cash-constrained partial
    # fills vanished from the reported book and returns were compounded on
    # positions the ledger never owned ("ghost leverage" / inflated returns).
    cash = float(initial_capital)

    # Actual weights held at the END of the day (after drift and any rebalancing).
    # B1.2: in minimal-metrics mode the (n_days x n_symbols) matrix is never
    # allocated -- nothing reads it, and the ledger is what the metrics come
    # from, not this reporting mirror.
    actual_w = None if minimal_metrics else np.zeros_like(tgt_w_arr)

    total_turnover = 0.0

    # Mark-to-market prices. Forward-filled so a missing bar marks at its last
    # known close instead of poisoning equity with NaN; a symbol that never
    # quoted marks at 0.0, which is harmless because it can never be bought
    # (its share count stays 0, so it contributes nothing).
    mark_arr = closes.ffill().fillna(0.0).to_numpy(dtype=float)

    # Tracking integer shares held for each symbol (no fractional shares allowed)
    held_shares = {sym: 0 for sym in symbols}
    settled_shares = {sym: 0 for sym in symbols}
    # Vectorised mirror of `held_shares`, updated at the exact same three points
    # (day-0 entry, sell fill, buy fill). It exists only so the daily mark-to-
    # market is one `np.dot` instead of a Python loop over the universe -- the
    # dict stays the readable ledger used for the trade records.
    held_vec = np.zeros(len(symbols))

    def _lot_round(raw_shares: float, lot: int) -> float:
        """Shares for `raw_shares` under a per-symbol lot size.

        Mirrors the original sizing rules EXACTLY (unchanged by B1.1), because
        they are part of the published contract: fractional shares when
        `lot == 0`, a whole-share count when `lot == 1`, and a whole-lot
        multiple when `lot > 1`.
        """
        if lot == 0:
            return float(raw_shares)
        rounded_s = int(round(raw_shares, 6))
        if lot > 1:
            return float((rounded_s // lot) * lot)
        return float(rounded_s)

    def _tradable(shares: float, lot: int) -> bool:
        """Same tradability predicate the code has always used."""
        return shares > 1e-9 if lot == 0 else shares >= lot

    # ------------------------------------------------------------------ #
    # (B3.3 / B3.4) DAILY CARRY SETTLEMENT
    # ------------------------------------------------------------------ #
    # Plain scalar accumulators, exactly like `total_impact`: advanced in the same
    # loop, so `minimal_metrics` mode reports identical numbers, and a hard 0.0
    # whenever the corresponding switch is off.
    total_cash_interest = 0.0
    total_borrow_fees = 0.0
    # The part of a borrow fee the balance could not pay (clamped so `cash >= 0`
    # survives). Always 0.0 in practice; reported rather than financed.
    total_borrow_fees_unpaid = 0.0
    # True when interest was credited while a cash substitute was held (B3.3
    # requirement 2). Purely a disclosure flag -- the base is `cash` either way.
    cash_proxy_overlap = False
    carry_events = []
    _proxy_overlap_warned = False

    def _settle_daily_carry(day: int) -> None:
        """Credit idle-cash interest / charge the borrow fee for ONE session.

        Runs as step 0 of the bar, i.e. on the OPENING book: the cash and the
        positions that were actually carried OVERNIGHT from the previous
        session's close. Both amounts then live in `cash` itself, so the caller's
        `equity[day] = cash + holdings` includes them with no extra term and no
        liability side-account (`rebalance_report` is untouched: neither item is
        a fill).

        Boundaries, all deliberate (and tested):
          * a short opened at T's close first pays at T+1;
          * the session a short is COVERED on still pays, because the charge is
            taken before that session's fills -- the position was indeed borrowed
            over the intervening night;
          * the session after the cover pays nothing (the opening book is flat);
          * the borrow base is marked at `mark_arr[day-1]`, the PREVIOUS close, so
            today's print cannot price today's overnight loan (strictly causal);
          * `day-1` may be the first bar, so a `close`-mode day-0 entry is charged
            from day 1 onward and never for a period it did not exist.
        """
        nonlocal cash, total_cash_interest, total_borrow_fees
        nonlocal total_borrow_fees_unpaid, cash_proxy_overlap, _proxy_overlap_warned

        dt = float(session_days[day - 1])
        opening_cash = cash

        # (B3.3) A NEGATIVE balance must never earn: `cash >= 0` is a B1.1
        # invariant, so a violation is an assertion, not a state to be paid
        # interest on. `_daily_cash_interest` additionally clamps at 0.0.
        cash_tol = _CASH_FLOAT_TOL * max(1.0, abs(opening_cash))
        if opening_cash < -cash_tol:
            raise AssertionError(
                f"cash went negative ({opening_cash!r}) on {common_idx[day].date()} with a "
                "cash-carry item enabled: idle cash must never earn interest on, or be "
                "spent below, a negative balance (B1.1 guarantees cash >= 0)"
            )

        interest = _daily_cash_interest(opening_cash, cash_yield_annual, dt)
        if interest != 0.0:
            cash += interest
            total_cash_interest += interest
            if proxy_mask is not None and proxy_mask.any() and bool(np.any(held_vec[proxy_mask] != 0.0)):
                cash_proxy_overlap = True
                if not _proxy_overlap_warned:
                    _proxy_overlap_warned = True
                    _logger.warning(
                        "cash yield: interest is credited on the UNINVESTED CASH leg only, "
                        "while a cash substitute is also held (%s). The substitute's own "
                        "carry must come from its price series and is NOT added here -- do "
                        "not model the risk-free rate twice for the same money.",
                        [symbols[i] for i in np.flatnonzero(proxy_mask) if held_vec[i] != 0.0],
                    )

        fee = 0.0
        charged = 0.0
        if borrow_fee_annual is not None and bool(np.any(held_vec < 0.0)):
            short_notional = float(np.dot(np.maximum(-held_vec, 0.0), mark_arr[day - 1]))
            fee = _daily_borrow_fee(short_notional, borrow_fee_annual, dt)
            if fee > 0.0:
                charged = fee
                if charged > cash:
                    # Defensive: an all-but-empty balance. Pay what the account
                    # holds and DISCLOSE the shortfall instead of borrowing to
                    # fund a fee (which would break `cash >= 0`).
                    charged = max(0.0, cash)
                    unpaid = fee - charged
                    total_borrow_fees_unpaid += unpaid
                    _logger.warning(
                        "borrow fee: %s of the %s fee due on %s could not be paid from a "
                        "cash balance of %s; the remainder is reported as unpaid rather "
                        "than financed (cash >= 0 is a hard invariant).",
                        unpaid, fee, common_idx[day].date(), cash,
                    )
                cash -= charged
                total_borrow_fees += charged

        if (interest != 0.0 or fee > 0.0) and not minimal_metrics:
            # B1.2 discipline: the per-day record is REPORTING overhead, so it is
            # not even built in minimal-metrics mode (the scalars above already
            # carry the numbers).
            carry_events.append({
                "date": common_idx[day].strftime("%Y-%m-%d"),
                "calendar_days": dt,
                "cash_interest": float(interest),
                "borrow_fee": float(charged),
                "cash_balance": float(cash),
            })

    # ------------------------------------------------------------------ #
    # (B3.2) SQUARE-ROOT IMPACT + PARTICIPATION BUDGET
    # ------------------------------------------------------------------ #
    # `total_impact` is a scalar accumulator exactly like `total_turnover`: it
    # is advanced at every fill (so `minimal_metrics` mode still reports it) and
    # surfaced as `total_market_impact`. It stays 0.0 when the model is off.
    total_impact = 0.0

    def _floor_lot(raw: float, lot: int) -> float:
        """`raw` shares ROUNDED DOWN to a whole lot.

        Deliberately a floor, not `_lot_round`'s nearest: a capacity budget must
        never be exceeded by rounding up (a 0.4-share budget under a 1-share lot
        buys nothing, it does not buy 1). A +1e-9 fudge keeps float noise
        (999.9999999999 for a 1000-share budget) from costing a whole lot.
        """
        if lot == 0:
            return float(raw)
        whole = int(np.floor(float(raw) + 1e-9))
        if whole <= 0:
            return 0.0
        if lot > 1:
            return float((whole // lot) * lot)
        return float(whole)

    def _participation_cap(day: int, i: int, lot: int) -> Optional[float]:
        """This session's share budget under `max_volume_participation`, or None.

        None means "no cap applies here", in two distinct cases that are both
        deliberate:
        * the cap is switched off (default), or the symbol has NO `Volume`
          column -- absent volume is "unobservable", never "zero" (the same
          DISCLOSED rule B1.3 applies to its suspension guard), and
        * the bar is a SUSPENSION (`Volume <= 0` / NaN / NaN-implied). A halted
          session must NOT read as a zero-share budget that then "carries" into
          the next day: the B1.3 guard has already refused the order outright,
          so there is nothing to accumulate. Keeping that distinction here is
          what makes `Volume == 0` a refusal rather than a deferral.
        The budget uses TODAY's volume (the task's definition); see the
        docstring's DISCLOSED note about it being a retrospective check.
        """
        if max_volume_participation is None or volume_arr is None or not volume_known[i]:
            return None
        v = float(volume_arr[day, i])
        if not np.isfinite(v) or v <= 0.0:
            return None
        return _floor_lot(v * float(max_volume_participation), lot)

    def _impact_cost(day: int, i: int, shares: float, trade_val: float) -> float:
        """Almgren-Chriss square-root temporary impact, in CURRENCY (B3.2).

        ``cost = trade_value * sigma * gamma * sqrt(|shares| / ADV20)``

        Charged to CASH (like commission), never to the share count -- `price`
        stays the market print so the position and the trade report stay
        reconcilable. Returns 0.0 whenever the model is off or the inputs are
        undefined (no volume column, first session, flat series), which is the
        DISCLOSED "no fabricated history" rule rather than a silent guess.
        """
        if not market_impact or adv20_arr is None or sigma_arr is None:
            return 0.0
        adv = float(adv20_arr[day, i])
        sig = float(sigma_arr[day, i])
        if not (np.isfinite(adv) and adv > 0.0 and np.isfinite(sig) and sig > 0.0):
            return 0.0
        frac = sig * impact_gamma * float(np.sqrt(abs(float(shares)) / adv))
        return float(trade_val * frac)

    def _shrink_buy_for_impact(trade_s: float, price: float, avail_cash: float,
                               day: int, i: int, lot: int) -> float:
        """Shrink a BUY so `shares*price + costs(impact included) <= avail_cash`.

        The square-root impact is NON-LINEAR in size, so the fee-inclusive cash
        cap cannot be solved in closed form the way the flat `buy_cost_rate`
        one is. A few fixed-point steps converge because impact grows slower
        than notional (sqrt): each pass removes exactly the shortfall. Only ever
        called when `market_impact` is on, so the default path never runs it.
        """
        if trade_s <= 0.0:
            return trade_s
        for _ in range(6):
            imp = _impact_cost(day, i, trade_s, trade_s * price)
            need = trade_s * price * (1.0 + buy_cost_rate) + imp
            if need <= avail_cash + 1e-9:
                break
            affordable = _floor_lot((avail_cash - imp) / (price * (1.0 + buy_cost_rate)), lot)
            if affordable >= trade_s:
                affordable = _floor_lot(trade_s - max(1.0, lot), lot)
            if affordable <= 0.0:
                return 0.0
            trade_s = affordable
        return trade_s

    def _holdings_value(day: int) -> float:
        """Mark-to-market value of the ledger's share positions on `day`."""
        return float(np.dot(held_vec, mark_arr[day]))

    def _weights_from_ledger(day: int, day_equity: float) -> np.ndarray:
        """actual_w_i = held_shares_i * P_i / Equity (the B1.1 definition)."""
        if day_equity <= 0:
            return np.zeros(len(symbols))
        return held_vec * mark_arr[day] / day_equity

    def _suspension_reason(day: int, i: int) -> Optional[str]:
        """Why symbol `i` cannot trade on `day`, or None when it can (B1.3).

        `volume_missing` (no bar / NaN volume) and `volume_zero` are both
        suspensions: neither a buy nor a sell may be filled at a print that
        says no shares changed hands.
        """
        if volume_arr is None or not volume_known[i]:
            return None
        v = float(volume_arr[day, i])
        if not np.isfinite(v):
            return "volume_missing"
        if v <= 0.0:
            return "volume_zero"
        return None

    suspension_events = []

    def _record_suspension(day: int, i: int, action: str, reason: str, target_w: float) -> None:
        """Append an assertable trace AND a log line for a refused order."""
        vol = float(volume_arr[day, i]) if volume_arr is not None else float("nan")
        suspension_events.append({
            "date": common_idx[day].strftime("%Y-%m-%d"),
            "symbol": symbols[i],
            "action": action,
            "reason": reason,
            "volume": vol,
            "target_weight": float(target_w),
        })
        _logger.debug(
            "suspension guard: %s %s on %s refused (%s, volume=%s) -- no fill booked",
            action, symbols[i], common_idx[day].date(), reason, vol,
        )

    trades = []
    rebalance_id = 0

    # ------------------------------------------------------------------ #
    # (B3.1) DEFERRED-ORDER STATE -- `next_*` modes only
    # ------------------------------------------------------------------ #
    # A `next_*` mode fills a signal on the session AFTER the close that printed
    # it, so an order that cannot be filled then has to survive to the next
    # session. `pending_target` is the latest still-outstanding instruction row,
    # `pending_remaining[i]` marks the symbols of that row that have not been
    # resolved yet, and `pending_signal_day` is the close it was printed at.
    # All three stay None/empty in `close` mode, where every signal is filled on
    # the spot.
    pending_target = None
    pending_remaining = None
    pending_signal_day = None
    # (B3.2) Per-symbol OVERRIDE of the signal date. The row-level
    # `pending_signal_day` is enough when a whole instruction row is queued at
    # once, but a participation-capped residual can be queued on a LATER session
    # than the close that produced its instruction, and different residuals can
    # originate on different sessions. Keeping the true origin per symbol is
    # what makes `deferral_report["signal_date"]` an honest audit trail instead
    # of "whenever the last residual happened to be queued". Cleared whenever a
    # fresh instruction row replaces the queue (both modes).
    pending_sig_day_override = {}
    deferral_events = []

    def _record_deferral(exec_day, i, action, reason, price, target_w) -> None:
        """Trace a DEFERRED order attempt (task B3.1) -- never a silent skip."""
        sig = pending_sig_day_override.get(int(i), pending_signal_day)
        try:
            px = float("nan") if price is None else float(price)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            px = float("nan")
        deferral_events.append({
            "signal_date": common_idx[sig].strftime("%Y-%m-%d") if sig is not None else "",
            "execution_date": common_idx[exec_day].strftime("%Y-%m-%d") if exec_day is not None else "",
            "symbol": symbols[i],
            "action": action,
            "reason": reason,
            "execution_price": px,
            "target_weight": float(target_w),
        })
        _logger.debug(
            "execution deferral: %s %s (signal %s) not filled on %s (%s) -- requeued for the next session",
            action, symbols[i], sig, exec_day, reason,
        )

    def _queue_residual(day: int, order, cap_s: Optional[float] = None) -> None:
        """Put the UNFILLED remainder of a capped order back on the B3.1 queue.

        Task B3.2's whole deferral mechanism: the residual is not a second,
        parallel book of pending orders -- it reuses `pending_target` /
        `pending_remaining` / `pending_signal_day`, so `_execute_pending` fills
        it exactly as it fills any other deferred instruction, and the
        `superseded` / `no_next_bar` bookkeeping applies unchanged.

        `cap_s` is accepted for the trace/log line only: the queue stores a
        TARGET WEIGHT, not a share count, so each retry re-derives the residual
        against that session's mark (which is also why a partially-capped order
        converges without storing "how many shares are left").
        """
        nonlocal pending_target, pending_remaining, pending_signal_day
        i = int(order["i"])
        target_w = float(order["target_w"])
        action = "BUY" if target_w > 0 else "SELL"
        if pending_target is None or pending_remaining is None:
            pending_target = np.zeros(len(symbols))
            pending_remaining = np.zeros(len(symbols), dtype=bool)
        pending_target[i] = target_w
        pending_remaining[i] = True
        # The residual's own instruction date, NOT the day it got capped (see
        # `pending_sig_day_override`). Set BEFORE the record below, so the trace
        # names the session whose close produced the instruction.
        pending_sig_day_override.setdefault(i, day)
        if pending_signal_day is None:
            pending_signal_day = day
        _record_deferral(
            day, i, action, "volume_participation_cap", order.get("price"), target_w,
        )
        _logger.debug(
            "participation cap: %s %s capped to %s shares on %s -- remainder requeued",
            action, symbols[i], cap_s, common_idx[day].date(),
        )

    def _close_limit_lock(day: int, i: int, buying: bool) -> Optional[str]:
        """Board-limit test for a fill AT THE CLOSE (used by B3.2 in `close` mode).

        Identical maths to the inline limit test the `close` rebalance block has
        always used (return at the board band, plus the close sitting on the
        day's extreme); it is lifted into a helper so a participation-capped
        residual retried at a later close is refused on a locked bar exactly
        like any other close-mode order. `opens_arr` may be None here, which is
        why `_open_limit_lock` falls back to this rather than assuming an open
        print exists.
        """
        if not china_trading:
            return None
        limit_pct = float(china_limit_pcts[i])
        if limit_pct >= 1.0:
            return None
        ret_i = float(ret_arr[day, i])
        price = float(closes_arr[day, i])
        p_high = float(highs_arr[day, i])
        p_low = float(lows_arr[day, i])
        if buying and (ret_i >= limit_pct - 0.005) and (p_high - price <= 1e-4 * price):
            return "open_limit_up"
        if (not buying) and (ret_i <= -limit_pct + 0.005) and (price - p_low <= 1e-4 * price):
            return "open_limit_down"
        return None

    def _open_limit_lock(day: int, i: int, buying: bool) -> Optional[str]:
        """`open_limit_up` / `open_limit_down` for a 一字板 at the open, else None.

        Two conditions, BOTH required: (1) the open sits at the board limit
        measured against the previous close, and (2) the bar never traded away
        from that open (its low equals the open for a ceiling, its high equals
        the open for a floor) -- there was no print on the other side for the
        order to cross with. A limit-up day that OPENS at the ceiling and then
        falls back is deliberately NOT a 一字板 and remains fillable at its open,
        which is why the range condition is not optional.

        The band comes from `_get_china_price_limit`, so ST / *ST names use the
        5% band and ChiNext/STAR the 20% one (tasks B1.3 + B3.1), with the same
        50bp tolerance the close-mode limit test uses. Only active under
        `china_trading`: no other venue in this engine has a fixed band.
        """
        if not china_trading:
            return None
        if opens_arr is None:
            # `close` mode never builds an open matrix. A B3.2 participation
            # residual retried at a later close is refused on a locked bar by
            # the close-based test instead (identical band maths).
            return _close_limit_lock(day, i, buying)
        limit_pct = float(china_limit_pcts[i])
        if limit_pct >= 1.0:
            return None
        prev_close = float(mark_arr[day - 1, i])  # last KNOWN close, never a hole
        open_px = float(opens_arr[day, i])
        if not (np.isfinite(prev_close) and prev_close > 0 and np.isfinite(open_px) and open_px > 0):
            return None
        ret_open = open_px / prev_close - 1.0
        if buying and ret_open >= limit_pct - 0.005 and (open_px - float(lows_arr[day, i])) <= 1e-4 * open_px:
            return "open_limit_up"
        if (not buying) and ret_open <= -limit_pct + 0.005 and (float(highs_arr[day, i]) - open_px) <= 1e-4 * open_px:
            return "open_limit_down"
        return None

    def _execute_pending(day: int):
        """Fill the queued instruction at `day`'s execution price.

        Returns `(n_fills, traded_notional)`. A symbol that cannot be filled
        today stays queued (`pending_remaining`) and is traced in the deferral
        report; it is never dropped and never filled at a print that did not
        trade.

        SIZING BASIS: the portfolio marked at the EXECUTION price
        (`cash + sum(held * exec_price)`) -- the book's value at the moment the
        order is sized, which is what a target weight means. `close` mode uses
        the same idea with the close. Cash feasibility is the same rule as close
        mode: sells first, then buys capped at `cash / (1 + rate)` (the fee is
        payable out of the same balance, so it can never be financed), and a
        cash-capped BUY is FINAL for that instruction -- requeueing it would let
        a cash-starved order re-buy every session as cash trickles in.
        """
        nonlocal cash, rebalance_id, total_turnover, pending_target, pending_remaining, total_impact

        if pending_target is None or pending_remaining is None or not pending_remaining.any():
            return 0, 0.0

        # Basis mark: the execution price where it exists, else the last known
        # close -- one symbol with no open print must not NaN-poison the value
        # of the whole book (that symbol is still refused a fill below).
        day_exec = np.asarray(exec_price_arr[day], dtype=float).copy()
        bad_px = ~np.isfinite(day_exec) | (day_exec <= 0.0)
        if bad_px.any():
            day_exec[bad_px] = mark_arr[day][bad_px]
        exec_equity = cash + float(np.dot(held_vec, day_exec))
        if not (exec_equity > 0.0):
            return 0, 0.0
        cur_w = held_vec * day_exec / exec_equity

        candidates = []
        for i in np.flatnonzero(pending_remaining):
            i = int(i)
            sym = symbols[i]
            target_w = float(pending_target[i])
            prior_s = held_shares[sym]
            prior_w = float(cur_w[i]) if np.isfinite(cur_w[i]) else 0.0
            delta_w = target_w - prior_w
            if abs(delta_w) <= 1e-7:
                # The book already sits on the instruction: the order is done.
                pending_remaining[i] = False
                continue

            price = float(exec_price_arr[day, i])
            action = "BUY" if delta_w > 0 else "SELL"

            # 1. Suspension on the EXECUTION bar (B1.3 rule, unchanged: a
            #    Volume<=0 / missing bar refuses BOTH directions and the position
            #    keeps being valued at its last close).
            suspended = _suspension_reason(day, i)
            if suspended is not None:
                _record_suspension(day, i, action, suspended, target_w)
                _record_deferral(day, i, action, suspended, price, target_w)
                continue
            # 2. No fill price for this symbol today -> nothing to trade at.
            if not (np.isfinite(price) and price > 0):
                _record_deferral(
                    day, i, action,
                    "intraday_missing" if execution_price == "intraday_twap" else "price_missing",
                    price, target_w,
                )
                continue
            # 3. Board-locked open (一字板) -> defer to the next session.
            lock = _open_limit_lock(day, i, buying=(delta_w > 0))
            if lock is not None:
                _record_deferral(day, i, action, lock, price, target_w)
                continue

            sym_min_shares = int(sym_min_shares_arr[i])

            if abs(target_w) < 1e-7 and prior_s != 0:
                # Full liquidation is always allowed (closes out odd lots).
                trade_s = abs(prior_s)
                action = "SELL" if prior_s > 0 else "BUY"
            elif abs(target_w) < 1e-7 and prior_s == 0:
                pending_remaining[i] = False
                continue
            else:
                if min_weight_change > 0.0 and prior_s != 0 and abs(delta_w) < min_weight_change:
                    # Passive-drift suppression: there is nothing to execute, so
                    # the order is RESOLVED instead of re-tried tomorrow.
                    pending_remaining[i] = False
                    continue
                trade_s = _lot_round((abs(delta_w) * exec_equity) / price, sym_min_shares)
                if not _tradable(trade_s, sym_min_shares):
                    # Below one lot: unexecutable at this price, resolve it.
                    pending_remaining[i] = False
                    continue
                action = "BUY" if delta_w > 0 else "SELL"

            if china_trading and action == "SELL":
                trade_s = min(trade_s, max(0, settled_shares[sym]))
                if trade_s <= 0:
                    # T+1: nothing settled yet -> stays queued.
                    continue

            # 4. (B3.2) Liquidity budget. Applied AFTER every sizing/rounding
            #    rule above, so it can only ever CUT a fill, never inflate one.
            #    The unfilled remainder KEEPS its queue slot (it is not resolved
            #    here), which is what makes the excess a deferral rather than a
            #    silent shrink.
            cap_s = None
            capped = False
            if max_volume_participation is not None:
                cap_s = _participation_cap(day, i, sym_min_shares)
                if cap_s is not None and trade_s > cap_s + 1e-12:
                    trade_s = cap_s
                    capped = True
                    if not _tradable(trade_s, sym_min_shares):
                        # Today's budget is smaller than one lot: nothing may be
                        # filled, and the order must NOT be resolved (the budget
                        # is per-session, so tomorrow's is fresh).
                        _record_deferral(
                            day, i, action, "volume_participation_cap", price, target_w,
                        )
                        continue

            candidates.append({
                "i": i,
                "symbol": sym,
                "action": action,
                "price": price,
                "prior_w": prior_w,
                "target_w": target_w,
                "delta_w": delta_w,
                "prior_s": prior_s,
                "trade_s": trade_s,
                "sym_min_shares": sym_min_shares,
                "capped": capped,
                "cap_s": cap_s,
            })

        # Sells first (they release cash), then buys against the real balance --
        # the same execution order as close mode, so the two modes cannot
        # disagree about what a book can afford.
        sells = sorted([c for c in candidates if c["action"] == "SELL"], key=lambda c: (-abs(c["delta_w"]), c["symbol"]))
        buys = sorted([c for c in candidates if c["action"] == "BUY"], key=lambda c: (-abs(c["delta_w"]), c["symbol"]))

        n_fills = 0
        notional = 0.0
        rebal_trades = []

        for c in sells:
            sym = c["symbol"]
            price = c["price"]
            # (B3.5 / B5.2) SIGN CONVENTION: a SELL is a NEGATIVE signed share delta
            # for EVERY prior sign. Reducing a long and ADDING to a short are
            # the same arithmetic, `new_s = prior_s - trade_s`. The pre-B3.5
            # `prior_s + trade_s if prior_s <= 0` branch inverted it: a short
            # opened from flat became a LONG of `+trade_s` shares (and a short
            # top-up covered instead of extending), while the cash flow was
            # booked with the inverted sign too. The `|prior_s|` clamp is a
            # LONG-TARGET guard: when target_w >= -1e-7, a sell may never be
            # financed past flat into an unintended short; but when target_w < -1e-7
            # (a long-to-short flip or short expansion), trade_s spans across flat
            # and must NOT be clamped to prior_s.
            if c["target_w"] >= -1e-7 and c["prior_s"] > 0:
                trade_s = min(c["trade_s"], c["prior_s"])
            else:
                trade_s = c["trade_s"]
            if trade_s <= 0:
                continue
            new_s = c["prior_s"] - trade_s
            trade_val = float(trade_s * price)
            comm = float(trade_val * commission_pct)
            # (B3.2) `slippage` is the "execution cost from price concession"
            # column, so the square-root impact is charged INTO it: the
            # `total_cost = commission + slippage + stamp` identity the ledger
            # replay depends on keeps holding, and with the model off `imp` is a
            # hard 0.0 (bit-identical to pre-B3.2).
            imp = _impact_cost(day, c["i"], trade_s, trade_val)
            slip = float(trade_val * slippage_pct) + imp
            total_impact += imp
            if hk_trading:
                stamp = float(trade_val * hk_stamp_duty_pct)
            elif china_trading:
                stamp = float(trade_val * stamp_duty_pct)
            elif us_trading:
                stamp = float(trade_val * sec_fee_pct)
            else:
                stamp = 0.0
            total_cost = comm + slip + stamp
            cash -= (new_s - c["prior_s"]) * price + total_cost
            held_shares[sym] = new_s
            held_vec[c["i"]] = new_s
            settled_shares[sym] = max(0, settled_shares[sym] - trade_s)
            # (B3.2) A participation-capped SELL leaves its remainder queued.
            pending_remaining[c["i"]] = bool(c.get("capped", False))
            if c.get("capped", False):
                _record_deferral(
                    day, c["i"], "SELL",
                    "volume_participation_cap", price, c["target_w"],
                )
            n_fills += 1
            notional += trade_val
            if not minimal_metrics:
                rebal_trades.append({
                    "rebalance_id": rebalance_id + 1,
                    "date": common_idx[day].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": "SELL",
                    "price": price,
                    "prior_weight": c["prior_w"],
                    "target_weight": c["target_w"],
                    "weight_change": c["delta_w"],
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": float(c["prior_s"]),
                    "target_shares": float(new_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": total_cost,
                    "portfolio_equity": exec_equity,
                })

        for c in buys:
            sym = c["symbol"]
            price = c["price"]
            sym_min_shares = c.get("sym_min_shares", min_shares)
            avail_cash = max(0.0, cash)
            unit_cash = price * (1.0 + buy_cost_rate)
            if sym_min_shares == 0:
                max_lots_s = avail_cash / unit_cash
            else:
                max_affordable_s = int(np.floor(avail_cash / unit_cash))
                max_lots_s = (max_affordable_s // sym_min_shares) * sym_min_shares if sym_min_shares > 1 else max_affordable_s
            trade_s = min(c["trade_s"], max_lots_s)
            # (B3.2) `unit_cash` above is the FLAT-rate affordability; the
            # square-root impact is non-linear and would let the last shares
            # overrun the balance, so the size is shrunk to a truly affordable
            # one. Only runs when the model is on.
            if market_impact and trade_s > 0:
                trade_s = _shrink_buy_for_impact(
                    trade_s, price, avail_cash, day, c["i"], sym_min_shares,
                )
            if sym_min_shares > 0 and trade_s < sym_min_shares:
                continue
            if sym_min_shares == 0 and trade_s <= 1e-9:
                continue
            new_s = c["prior_s"] + trade_s
            trade_val = float(trade_s * price)
            comm = float(trade_val * commission_pct)
            imp = _impact_cost(day, c["i"], trade_s, trade_val)
            slip = float(trade_val * slippage_pct) + imp
            total_impact += imp
            stamp = float(trade_val * hk_stamp_duty_pct) if hk_trading else 0.0
            total_cost = comm + slip + stamp
            cash -= (new_s - c["prior_s"]) * price + total_cost
            held_shares[sym] = new_s
            held_vec[c["i"]] = new_s
            # (B3.2) The remainder stays queued ONLY when the CAP was the
            # binding constraint and the whole capped amount actually filled. A
            # cash-limited BUY is FINAL (the B3.1 rule): requeueing it would let
            # a cash-starved order re-buy every session as cash trickles in.
            cap_s = c.get("cap_s")
            pending_remaining[c["i"]] = bool(
                c.get("capped", False) and cap_s is not None and trade_s >= cap_s - 1e-9
            )
            if pending_remaining[c["i"]]:
                _record_deferral(
                    day, c["i"], "BUY" if c["delta_w"] > 0 else "SELL",
                    "volume_participation_cap", price, c["target_w"],
                )
            n_fills += 1
            notional += trade_val
            if not minimal_metrics:
                rebal_trades.append({
                    "rebalance_id": rebalance_id + 1,
                    "date": common_idx[day].strftime("%Y-%m-%d"),
                    "symbol": sym,
                    "action": "BUY",
                    "price": price,
                    "prior_weight": c["prior_w"],
                    "target_weight": c["target_w"],
                    "weight_change": c["delta_w"],
                    "trade_value": trade_val,
                    "shares": float(trade_s),
                    "prior_shares": float(c["prior_s"]),
                    "target_shares": float(new_s),
                    "commission": comm,
                    "slippage": slip,
                    "total_cost": total_cost,
                    "portfolio_equity": exec_equity,
                })

        if n_fills:
            rebalance_id += 1
            if not minimal_metrics:
                trades.extend(rebal_trades)
            total_turnover += notional / exec_equity

        if not pending_remaining.any():
            pending_target = None
            pending_remaining = None

        return n_fills, notional

    # Day 0: Initial allocation. In `close` mode this is the historical
    # day-0 close entry, untouched. In a `next_*` mode there is nothing to fill
    # yet (day 0's signal can only be a CLOSE signal, and no earlier close
    # exists), so no order is built here and the row is queued below instead --
    # `day0_orders` staying empty makes every later block on this path inert.
    day0_trades = []
    day0_orders = []
    if not next_day_execution:
        for i, sym in enumerate(symbols):
            target_w = float(tgt_w_arr[0, i])
            if abs(target_w) <= 1e-7:
                continue
            suspended = _suspension_reason(0, i)
            if suspended is not None:
                _record_suspension(0, i, "BUY" if target_w > 0 else "SELL", suspended, target_w)
                continue
            price = float(closes_arr[0, i])
            if np.isnan(price) or not (price > 0):
                continue
            sym_min_shares = int(sym_min_shares_arr[i])
            target_s = _lot_round((abs(target_w) * initial_capital) / price, sym_min_shares)
            if not _tradable(target_s, sym_min_shares):
                continue
            day0_orders.append({
                "i": i,
                "symbol": sym,
                "target_w": target_w,
                "price": price,
                "target_s": target_s,
                "sym_min_shares": sym_min_shares,
            })
    elif is_rebalance[0]:
        # A `next_*` mode can only QUEUE day 0: the first fill happens at day
        # 1's execution price. Day 0 therefore opens as an all-cash book.
        pending_target = np.asarray(tgt_w_arr[0], dtype=float).copy()
        pending_remaining = np.ones(len(symbols), dtype=bool)
        pending_signal_day = 0
        pending_sig_day_override.clear()

    # Cost rate on a day-0 order. A long entry pays commission + slippage (plus
    # HK's dual-sided stamp duty, which applies to BOTH sides); opening a short
    # is a SELL, so it also carries the China/US sell-side levies.
    def _cost_rate_for(target_w: float) -> float:
        rate = commission_pct + slippage_pct
        if hk_trading:
            rate += hk_stamp_duty_pct
        elif china_trading and target_w < 0:
            rate += stamp_duty_pct
        elif us_trading and target_w < 0:
            rate += sec_fee_pct
        return rate

    def _order_cash_flow(order) -> float:
        """Signed cash impact of filling `order` (positive = cash comes in).

        Includes the B3.2 square-root impact when the model is on, so the day-0
        cash-feasibility loop below prices it too instead of leaving the entry
        to overrun its balance. With the model off the expression is EXACTLY the
        pre-B3.2 one (`imp` is a hard 0.0).
        """
        value = float(order["target_s"]) * float(order["price"])
        rate = _cost_rate_for(order["target_w"])
        imp = _impact_cost(0, order["i"], order["target_s"], value)
        if order["target_w"] > 0:
            return -value * (1.0 + rate) - imp
        return value * (1.0 - rate) - imp

    # ------------------------------------------------------------------ #
    # (B3.2) DAY-0 PARTICIPATION CAP
    # ------------------------------------------------------------------ #
    # The day-0 entry is an ordinary fill and obeys the same liquidity budget;
    # without this, the single largest order of the whole backtest would be the
    # one place the cap never applied. A capped order keeps its sized-but-
    # unfilled remainder on the B3.1 queue (`pending_*`), so day 1+ tops it up
    # through the SAME machinery -- no second state machine.
    if max_volume_participation is not None and day0_orders:
        kept_orders = []
        for o in day0_orders:
            cap_s = _participation_cap(0, o["i"], o["sym_min_shares"])
            if cap_s is not None and o["target_s"] > cap_s + 1e-12:
                _queue_residual(0, o, cap_s)
                o["target_s"] = cap_s
                if not _tradable(o["target_s"], o["sym_min_shares"]):
                    continue
            kept_orders.append(o)
        day0_orders = kept_orders

    # Cash feasibility on entry. The old code allocated the full target share
    # count and then renormalised only the REPORTED weights when gross exposure
    # exceeded 1.0 -- the share ledger stayed over-levered. Scaling the orders
    # down proportionally keeps the old "normalise an over-100% target"
    # intent while keeping the ledger honest.
    #
    # B1.1 fix: the budget must cover the FEE too, not just the notional. The
    # first B1.1 cut spent `cash` on notional and then charged commission /
    # slippage to the same cash account, driving it to `cash = -fee` on any
    # full-notional entry. That is a real deviation, not a convention: it made
    # `sum(actual_w) = 1 + fee` (levered book) and manufactured a spurious
    # 1-share "trim" at the next rebalance instruction, because the
    # fee-financed excess had to be sold back out. A long entry of notional N
    # costs N*(1+rate), so the affordable notional is `cash/(1+rate)` (plus
    # what short-sale proceeds, net of their own costs, release).
    long_cost_rate = _cost_rate_for(1.0)
    # Same rate for every BUY fill below (buys never carry the China/US
    # sell-side levies; HK stamp duty is dual-sided).
    buy_cost_rate = long_cost_rate

    def _entry_need(orders) -> float:
        """Net cash the entry consumes (fees included)."""
        return -sum(_order_cash_flow(o) for o in orders)

    cash_tolerance = _CASH_FLOAT_TOL * max(1.0, abs(cash))
    for _attempt in range(8):
        excess = _entry_need(day0_orders) - cash
        if excess <= cash_tolerance:
            break
        credit_net = sum(_order_cash_flow(o) for o in day0_orders if o["target_w"] < 0)
        long_notional = sum(o["target_s"] * o["price"] for o in day0_orders if o["target_w"] > 0)
        if long_notional <= 0:
            break
        budget = max(0.0, (cash + credit_net) / (1.0 + long_cost_rate))
        scale = budget / long_notional
        scaled_orders = []
        for o in day0_orders:
            if o["target_w"] > 0:
                scaled_s = _lot_round(o["target_s"] * scale, o["sym_min_shares"])
                if not _tradable(scaled_s, o["sym_min_shares"]):
                    continue
                o["target_s"] = scaled_s
            scaled_orders.append(o)
        day0_orders = scaled_orders

    # Last-resort guard: lot granularity can still make the rounded book a few
    # shares too expensive (a single 100-share lot is a coarse step). Shave
    # whole lots off the largest long order until the entry is payable, so
    # `cash >= 0` holds even on coarse-lot fixtures.
    guard = 0
    while _entry_need(day0_orders) - cash > cash_tolerance and guard < 10_000:
        guard += 1
        longs = [o for o in day0_orders if o["target_w"] > 0]
        if not longs:
            break
        o = max(longs, key=lambda x: x["target_s"] * x["price"])
        step = max(int(o["sym_min_shares"]), 1)
        new_s = o["target_s"] - step
        if not _tradable(new_s, o["sym_min_shares"]):
            day0_orders.remove(o)
        else:
            o["target_s"] = new_s

    day0_fills = 0
    day0_notional = 0.0
    for order in day0_orders:
        sym = order["symbol"]
        target_w = order["target_w"]
        price = order["price"]
        trade_s = order["target_s"]
        held_s = trade_s if target_w >= 0 else -trade_s
        held_shares[sym] = held_s
        held_vec[order["i"]] = held_s
        trade_val = float(trade_s * price)
        comm = float(trade_val * commission_pct)
        # (B3.2) Impact charged into the `slippage` column, i.e. into
        # `total_cost` and therefore into the same cash account (see docstring).
        imp = _impact_cost(0, order["i"], trade_s, trade_val)
        slip = float(trade_val * slippage_pct) + imp
        total_impact += imp
        if hk_trading:
            stamp = float(trade_val * hk_stamp_duty_pct)
        elif china_trading:
            stamp = float(trade_val * stamp_duty_pct) if target_w < 0 else 0.0
        elif us_trading:
            stamp = float(trade_val * sec_fee_pct) if target_w < 0 else 0.0
        else:
            stamp = 0.0
        total_cost = comm + slip + stamp
        # Signed cash flow: a long entry pays out, a short entry receives the
        # proceeds. Costs always leave the account.
        cash -= held_s * price
        cash -= total_cost
        day0_fills += 1
        day0_notional += trade_val
        if not minimal_metrics:
            day0_trades.append({
                "rebalance_id": 1,
                "date": common_idx[0].strftime("%Y-%m-%d"),
                "symbol": sym,
                "action": "BUY" if target_w > 0 else "SELL",
                "price": price,
                "prior_weight": 0.0,
                "target_weight": target_w,
                "weight_change": target_w,
                "trade_value": trade_val,
                "shares": float(trade_s),
                "prior_shares": 0.0,
                "target_shares": float(held_s),
                "commission": comm,
                "slippage": slip,
                "total_cost": total_cost,
                "portfolio_equity": float(initial_capital),
            })

    if day0_fills:
        rebalance_id = 1
        if not minimal_metrics:
            trades.extend(day0_trades)
        # Turnover = traded notional / starting capital, i.e. the notional that
        # was ACTUALLY turned over. (The old sum-of-weight-deltas form counted
        # the intended deltas, which no longer equal the fills once lot
        # rounding / cash caps apply.)
        turnover = day0_notional / initial_capital
        total_turnover += turnover

    equity[0] = cash + _holdings_value(0)
    if not minimal_metrics:
        actual_w[0] = _weights_from_ledger(0, equity[0])

    settled_shares = held_shares.copy()

    for t in range(1, n_days):
        # 0. (B3.3 / B3.4) DAILY CARRY SETTLEMENT -- idle-cash repo interest in,
        #    securities-borrow fee out. It runs on the OPENING book (before the
        #    morning mark and before any fill), so `equity[t] = cash + holdings`
        #    below already includes it, and any rebalance on this bar is sized
        #    against the settled balance. Inert (never reached) unless one of the
        #    two switches is on.
        if carry_enabled:
            _settle_daily_carry(t)

        # 1. Morning: mark the ledger to today's close.
        pre_rebal_equity = cash + _holdings_value(t)
        equity[t] = pre_rebal_equity

        # 2. Pre-rebalance weights: what the ledger actually holds after
        # today's price drift. Idle cash dilutes them automatically, so no
        # separate drift divisor is needed.
        drifted_w = _weights_from_ledger(t, pre_rebal_equity)

        # 3. (B3.2) CLOSE-MODE CARRIED RESIDUALS, before today's own signal.
        #    A participation-capped order from an earlier session is retried at
        #    TODAY's close through the same `_execute_pending` the `next_*`
        #    modes use -- same queue, same trace, no second state machine. Inert
        #    unless the cap is enabled (`pending_target` stays None otherwise).
        if (not next_day_execution and max_volume_participation is not None
                and pending_target is not None):
            n_carry, _carry_notional = _execute_pending(t)
            if n_carry:
                # Re-mark AFTER the fills: they happened at today's close, so
                # `equity[t] = cash + holdings@close` is the post-trade mark and
                # is what today's own instruction must be sized against.
                equity[t] = cash + _holdings_value(t)
                pre_rebal_equity = equity[t]
                drifted_w = _weights_from_ledger(t, pre_rebal_equity)
            if is_rebalance[t] and pending_remaining is not None and pending_remaining.any():
                # A newer instruction supersedes the unfilled remainder, traced
                # exactly like the `next_*` modes' `superseded` records.
                for i in np.flatnonzero(pending_remaining):
                    i = int(i)
                    _record_deferral(
                        t, i, "BUY" if pending_target[i] > 0 else "SELL",
                        "superseded", float("nan"), float(pending_target[i]),
                    )
                pending_target = None
                pending_remaining = None
                pending_sig_day_override.clear()

        # 3. End of day: rebalance only on a date the template actually
        # instructed one (is_rebalance), never inferred from a value change.
        if next_day_execution:
            # (B3.1) A `next_*` mode has TWO halves per bar, in THIS order:
            #
            #   (A) FILL the instruction printed at an EARLIER close, at TODAY's
            #       execution price (open / VWAP / TWAP). This is the only point
            #       at which the ledger moves, and it is why the rebalance-date
            #       `actual_weights` row lands on the EXECUTION day rather than
            #       on the signal day.
            #   (B) QUEUE today's close signal for the NEXT bar. It must come
            #       AFTER (A): filling it in (A) would trade at an open that
            #       precedes the close that produced the signal (look-ahead).
            n_exec_fills, _exec_notional = _execute_pending(t)
            if n_exec_fills:
                # Re-mark the ledger at today's close AFTER the fills: the fill
                # happened at the open, so equity[t] = cash + holdings@close
                # captures the intraday open->close move of the new position.
                equity[t] = cash + _holdings_value(t)
            if not minimal_metrics:
                actual_w[t] = _weights_from_ledger(t, equity[t])

            if is_rebalance[t]:
                if pending_target is not None and pending_remaining is not None and pending_remaining.any():
                    # A newer instruction supersedes the unfilled remainder of
                    # the previous one -- recorded, so an order can never
                    # disappear merely because the strategy re-signalled.
                    for i in np.flatnonzero(pending_remaining):
                        i = int(i)
                        _record_deferral(
                            t, i, "BUY" if pending_target[i] > 0 else "SELL",
                            "superseded", float("nan"), float(pending_target[i]),
                        )
                pending_target = np.asarray(tgt_w_arr[t], dtype=float).copy()
                pending_remaining = np.ones(len(symbols), dtype=bool)
                pending_signal_day = t
                # A FRESH instruction owns the whole queue: stale per-symbol
                # origin dates (B3.2 residuals) must not leak into its trace.
                pending_sig_day_override.clear()

        elif is_rebalance[t] and pre_rebal_equity > 0:
            rebal_trades = []
            # B1.2: fills are COUNTED, not just recorded, so minimal-metrics
            # mode can still tell "trades happened on this date" apart from
            # "all deltas were below min_shares" without materialising dicts.
            n_fills = 0
            rebal_notional = 0.0

            # 1. Determine candidate trades for each symbol based on delta_w
            candidates = []
            for i, sym in enumerate(symbols):
                prior_s = held_shares[sym]
                prior_w = float(drifted_w[i])
                target_w = float(tgt_w_arr[t, i])
                delta_w = target_w - prior_w
                if abs(delta_w) <= 1e-7:
                    continue

                suspended = _suspension_reason(t, i)
                if suspended is not None:
                    # B1.3: a suspended bar blocks SELLS as well as BUYS --
                    # the old code only ever refused fills on limit-locked
                    # (china_trading) bars, so a halted name was silently
                    # "traded" at its last print on every rebalance.
                    _record_suspension(t, i, "BUY" if delta_w > 0 else "SELL", suspended, target_w)
                    continue

                price = float(closes_arr[t, i])
                if np.isnan(price) or not (price > 0):
                    continue

                if china_trading:
                    limit_pct = float(china_limit_pcts[i])
                    if limit_pct < 1.0:
                        ret_i = float(ret_arr[t, i])
                        p_high = float(highs_arr[t, i])
                        p_low = float(lows_arr[t, i])
                        is_limit_up = (ret_i >= limit_pct - 0.005) and (p_high - price <= 1e-4 * price)
                        is_limit_down = (ret_i <= -limit_pct + 0.005) and (price - p_low <= 1e-4 * price)
                        if delta_w > 0 and is_limit_up:
                            # Cannot buy when locked at limit-up ceiling
                            continue
                        if delta_w < 0 and is_limit_down:
                            # Cannot sell when locked at limit-down floor
                            continue

                sym_min_shares = int(sym_min_shares_arr[i])

                if abs(target_w) < 1e-7 and prior_s != 0:
                    # Full liquidation allowed (closing out odd-lots)
                    trade_s = abs(prior_s)
                    action = "SELL" if prior_s > 0 else "BUY"
                elif abs(target_w) < 1e-7 and prior_s == 0:
                    # Target is zero and we already hold zero shares
                    continue
                else:
                    # Recommendation 2: Passive drift suppression
                    # If we already hold shares of this asset and the delta is smaller than min_weight_change,
                    # treat this as passive price drift rather than an active portfolio adjustment.
                    if min_weight_change > 0.0 and prior_s != 0 and abs(delta_w) < min_weight_change:
                        continue

                    raw_val = abs(delta_w) * pre_rebal_equity
                    raw_s = raw_val / price
                    if sym_min_shares == 0:
                        trade_s = raw_s
                    elif sym_min_shares > 1:
                        rounded_s = int(round(raw_s, 6))
                        trade_s = (rounded_s // sym_min_shares) * sym_min_shares
                    else:
                        rounded_s = int(round(raw_s, 6))
                        trade_s = rounded_s

                    if sym_min_shares > 0 and trade_s < sym_min_shares:
                        continue
                    if sym_min_shares == 0 and trade_s <= 1e-9:
                        continue
                    action = "BUY" if delta_w > 0 else "SELL"

                if china_trading and action == "SELL":
                    avail_to_sell = max(0, settled_shares[sym])
                    trade_s = min(trade_s, avail_to_sell)
                    if trade_s <= 0:
                        continue

                candidates.append({
                    "i": i,
                    "symbol": sym,
                    "action": action,
                    "price": price,
                    "prior_w": prior_w,
                    "target_w": target_w,
                    "delta_w": delta_w,
                    "prior_s": prior_s,
                    "trade_s": trade_s,
                    "sym_min_shares": sym_min_shares,
                })

            # Available cash is EXACTLY the ledger's cash balance (plus whatever
            # the sells below release). Pre-B1.1 this was
            # `max(0, (1 - exposure) * equity) + lot_buffer`, where `lot_buffer`
            # (one lot per symbol) was never-existing cash -- it is precisely
            # what turned a suppressed sell into ghost buying power. Now the
            # balance is the truth, so no buffer is needed or allowed.
            sells = sorted([c for c in candidates if c["action"] == "SELL"], key=lambda c: (-abs(c["delta_w"]), c["symbol"]))
            buys = sorted([c for c in candidates if c["action"] == "BUY"], key=lambda c: (-abs(c["delta_w"]), c["symbol"]))

            # Execute sells first to release cash
            for c in sells:
                sym = c["symbol"]
                price = c["price"]
                # (B3.5 / B5.2) Same sign convention as `_execute_pending`: a SELL is a
                # negative signed share delta for EVERY prior sign. The clamp to prior_s
                # is a LONG-TARGET guard (target_w >= -1e-7); for target_w < -1e-7
                # (long-to-short flip), trade_s spans across flat and must NOT be clamped to prior_s.
                if c["target_w"] >= -1e-7 and c["prior_s"] > 0:
                    trade_s = min(c["trade_s"], c["prior_s"])
                else:
                    trade_s = c["trade_s"]
                # (B3.2) Liquidity budget, applied AFTER every sizing rule so it
                # can only cut. The unfilled remainder goes back on the B3.1
                # queue (`_queue_residual`) and is retried at a later close --
                # this branch is inert unless `max_volume_participation` is set.
                cap_s = _participation_cap(t, c["i"], c["sym_min_shares"]) \
                    if max_volume_participation is not None else None
                capped = cap_s is not None and trade_s > cap_s + 1e-12
                if capped:
                    trade_s = cap_s
                if trade_s <= 0:
                    if capped:
                        _queue_residual(t, c, cap_s)
                    continue
                # (B3.5) `prior_s - trade_s` unconditionally -- see the sell-loop
                # note in `_execute_pending`.
                new_s = c["prior_s"] - trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                imp = _impact_cost(t, c["i"], trade_s, trade_val)
                slip = float(trade_val * slippage_pct) + imp
                total_impact += imp
                if hk_trading:
                    stamp = float(trade_val * hk_stamp_duty_pct)
                elif china_trading:
                    stamp = float(trade_val * stamp_duty_pct)
                elif us_trading:
                    stamp = float(trade_val * sec_fee_pct)
                else:
                    stamp = 0.0
                total_cost = comm + slip + stamp
                # Signed cash flow: proceeds for a long sale, cash out for
                # covering a short; costs always leave the account. This is what
                # makes `cash` -- and therefore equity = cash + holdings --
                # reconcile against the trade report line by line.
                cash -= (new_s - c["prior_s"]) * price + total_cost
                held_shares[sym] = new_s
                held_vec[c["i"]] = new_s
                settled_shares[sym] = max(0, settled_shares[sym] - trade_s)
                n_fills += 1
                rebal_notional += trade_val
                if capped:
                    # Sell side has no cash constraint, so the capped remainder
                    # is ALWAYS still owed and always requeued.
                    _queue_residual(t, c, cap_s)
                # B1.2: the whole dict literal (including the `strftime` date
                # string) is skipped in minimal mode -- it must stay inside the
                # `if`, because a helper taking the dict as an ARGUMENT would
                # build it eagerly and throw it away.
                if not minimal_metrics:
                    rebal_trades.append({
                        "rebalance_id": rebalance_id + 1,
                        "date": common_idx[t].strftime("%Y-%m-%d"),
                        "symbol": sym,
                        "action": "SELL",
                        "price": price,
                        "prior_weight": c["prior_w"],
                        "target_weight": c["target_w"],
                        "weight_change": c["delta_w"],
                        "trade_value": trade_val,
                        "shares": float(trade_s),
                        "prior_shares": float(c["prior_s"]),
                        "target_shares": float(new_s),
                        "commission": comm,
                        "slippage": slip,
                        "total_cost": total_cost,
                        "portfolio_equity": pre_rebal_equity,
                    })

            # Execute buys up to the real cash balance to prevent ghost leverage
            for c in buys:
                sym = c["symbol"]
                price = c["price"]
                sym_min_shares = c.get("sym_min_shares", min_shares)
                avail_cash = max(0.0, cash)
                # The fee comes OUT of that same balance, so the affordable
                # notional is not `avail_cash` but `avail_cash/(1+rate)`.
                # Capping at `avail_cash` alone let the cost be financed on
                # margin (cash = -fee, sum(actual_w) > 1) -- the rebalance-day
                # twin of the day-0 entry bug fixed above. `floor` (not
                # `round`) is used so the last share can never overshoot.
                unit_cash = price * (1.0 + buy_cost_rate)
                if sym_min_shares == 0:
                    max_lots_s = avail_cash / unit_cash
                elif sym_min_shares > 1:
                    max_affordable_s = int(np.floor(avail_cash / unit_cash))
                    max_lots_s = (max_affordable_s // sym_min_shares) * sym_min_shares
                else:
                    max_affordable_s = int(np.floor(avail_cash / unit_cash))
                    max_lots_s = max_affordable_s
                trade_s = min(c["trade_s"], max_lots_s)
                # (B3.2) Liquidity budget (see the sell loop above). Inert
                # unless `max_volume_participation` is set.
                cap_s = _participation_cap(t, c["i"], sym_min_shares) \
                    if max_volume_participation is not None else None
                capped = cap_s is not None and trade_s > cap_s + 1e-12
                if capped:
                    trade_s = cap_s
                if market_impact and trade_s > 0:
                    # (B3.2) square-root impact is non-linear in size, so the
                    # flat `unit_cash` cap above cannot keep `cash >= 0` on its
                    # own; shrink to a truly affordable count.
                    trade_s = _shrink_buy_for_impact(
                        trade_s, price, avail_cash, t, c["i"], sym_min_shares,
                    )
                if sym_min_shares > 0 and trade_s < sym_min_shares:
                    if capped:
                        # The session's budget is smaller than one lot: nothing
                        # may trade, but the order is DEFERRED (tomorrow's budget
                        # is fresh), never resolved.
                        _queue_residual(t, c, cap_s)
                    continue
                if sym_min_shares == 0 and trade_s <= 1e-9:
                    if capped:
                        _queue_residual(t, c, cap_s)
                    continue
                new_s = c["prior_s"] + trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                imp = _impact_cost(t, c["i"], trade_s, trade_val)
                slip = float(trade_val * slippage_pct) + imp
                total_impact += imp
                stamp = float(trade_val * hk_stamp_duty_pct) if hk_trading else 0.0
                total_cost = comm + slip + stamp
                cash -= (new_s - c["prior_s"]) * price + total_cost
                held_shares[sym] = new_s
                held_vec[c["i"]] = new_s
                n_fills += 1
                rebal_notional += trade_val
                if capped and trade_s >= cap_s - 1e-9:
                    # Only the VOLUME-capped remainder is requeued. A fill that
                    # the cash balance cut short stays FINAL (the B3.1 rule), so
                    # a cash-starved order cannot re-buy every session.
                    _queue_residual(t, c, cap_s)
                if not minimal_metrics:
                    rebal_trades.append({
                        "rebalance_id": rebalance_id + 1,
                        "date": common_idx[t].strftime("%Y-%m-%d"),
                        "symbol": sym,
                        "action": "BUY",
                        "price": price,
                        "prior_weight": c["prior_w"],
                        "target_weight": c["target_w"],
                        "weight_change": c["delta_w"],
                        "trade_value": trade_val,
                        "shares": float(trade_s),
                        "prior_shares": float(c["prior_s"]),
                        "target_shares": float(new_s),
                        "commission": comm,
                        "slippage": slip,
                        "total_cost": total_cost,
                        "portfolio_equity": pre_rebal_equity,
                    })

            if n_fills:
                rebalance_id += 1
                if not minimal_metrics:
                    trades.extend(rebal_trades)
                # B1.1: report what the ledger now holds. The old code wrote
                # `new_w[i] = tgt_w_arr[t, i]` for every traded symbol (and
                # renormalised gross exposure to 1.0), i.e. it reported the
                # INSTRUCTION even when lot rounding idled cash or the buy was
                # capped by available cash, and then compounded returns on those
                # never-held positions. `mark_arr` + `held_shares` cannot lie.
                equity[t] = cash + _holdings_value(t)
                # Turnover = traded notional / pre-rebalance equity (what was
                # actually turned over, reconcilable with the trade report).
                # B1.2: accumulated in `rebal_notional` instead of summed off
                # the trade dicts, so minimal mode needs no dicts at all -- the
                # additions happen in the same order, so the value is identical.
                turnover = rebal_notional / pre_rebal_equity
                total_turnover += turnover
                if not minimal_metrics:
                    actual_w[t] = _weights_from_ledger(t, equity[t])
            else:
                # No trades executed because all deltas were below min_shares
                if not minimal_metrics:
                    actual_w[t] = drifted_w
        else:
            # No rebalance, actual weights are just the drifted weights
            if not minimal_metrics:
                actual_w[t] = drifted_w

        settled_shares = held_shares.copy()

    # (B3.1) An instruction printed on the LAST bar has no next session left to
    # fill it in. Report it rather than let it vanish: `execution_date` is empty,
    # `reason` is `no_next_bar`. (B3.2) applies the same rule to a close-mode
    # participation residual, which is also an order that never got its session.
    if (next_day_execution or max_volume_participation is not None) and pending_target is not None \
            and pending_remaining is not None and pending_remaining.any():
        for i in np.flatnonzero(pending_remaining):
            i = int(i)
            _record_deferral(
                None, i, "BUY" if pending_target[i] > 0 else "SELL",
                "no_next_bar", float("nan"), float(pending_target[i]),
            )

    equity_df = pd.DataFrame(index=common_idx)
    equity_df["equity"] = equity

    rebalance_report_df = pd.DataFrame(trades, columns=REBALANCE_REPORT_COLUMNS) if trades else pd.DataFrame(columns=REBALANCE_REPORT_COLUMNS)

    suspension_report_df = pd.DataFrame(suspension_events, columns=SUSPENSION_REPORT_COLUMNS)

    # Reconstruct actual weights DataFrame for transparency. B1.2: minimal
    # mode returns an EMPTY, properly-columned frame instead -- a declared
    # "not collected" signal rather than a silently different object shape,
    # so `.empty` / `.columns` / `pd.concat` consumers keep working.
    if minimal_metrics:
        actual_weights_df = pd.DataFrame(columns=symbols)
    else:
        actual_weights_df = pd.DataFrame(actual_w, index=common_idx, columns=symbols)

    # Compute additional performance metrics
    eq = equity_df["equity"]
    daily_returns = eq.pct_change().dropna()

    total_return = (eq.iloc[-1] / eq.iloc[0]) - 1.0 if len(eq) > 0 else 0.0
    # NOTE -- undocumented-until-now "years" convention mismatch:
    # unlike common/metrics.py's cagr() (trading-day-count basis,
    # n_periods/periods_per_year), this inline CAGR uses actual CALENDAR
    # days elapsed (.days/365.25). The two will disagree whenever the
    # underlying calendar has gaps vs. a fixed periods_per_year assumption.
    # This is a genuine inconsistency (not a deliberate disclosed dual
    # convention like win_rate/profit_factor vs. win_rate_from_returns/
    # profit_factor_from_returns) -- flagged here rather than changed, since
    # altering either formula risks breaking other correctness-dependent
    # code/tests.
    n_years = max((common_idx[-1] - common_idx[0]).days / 365.25, 1.0 / 252.0) if len(common_idx) > 1 else 1.0
    cagr = ((1.0 + total_return) ** (1.0 / n_years)) - 1.0 if total_return > -1.0 else -1.0

    # Max Drawdown -- positive magnitude (e.g. 0.18 for an 18% drawdown),
    # matching common/metrics.py's max_drawdown() convention used elsewhere
    # in this workspace (backtester/run_backtest.py in particular reports
    # both side by side, so the sign must agree).
    cummax = eq.cummax()
    drawdown = (cummax - eq) / cummax
    max_dd = float(drawdown.max()) if not drawdown.empty else 0.0

    # Calmar Ratio
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    # Sharpe Ratio
    mean_ret = float(daily_returns.mean()) if not daily_returns.empty else 0.0
    std_ret = float(daily_returns.std()) if not daily_returns.empty else 0.0
    sr = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    # Win Rate & Profit Factor (returns-based; see common/metrics.py's
    # win_rate_from_returns/profit_factor_from_returns docstrings for why
    # these are distinct from the trades-based win_rate/profit_factor also
    # defined there).
    win_rate = win_rate_from_returns(daily_returns)
    profit_factor = profit_factor_from_returns(daily_returns)

    # (B3.1) Every deferred order attempt, in chronological order. Always
    # present (empty in `close` mode, where nothing can ever be deferred) so a
    # caller can assert on it unconditionally.
    deferral_report_df = pd.DataFrame(deferral_events, columns=DEFERRAL_REPORT_COLUMNS)

    # (B3.3 / B3.4) One row per session that settled a NON-ZERO carry item, plus
    # the two scalar accumulators (reported in `minimal_metrics` mode too, and a
    # hard 0.0 when the switches are off). B1.2: minimal mode returns an EMPTY,
    # properly-columned frame -- a declared "not collected" signal, like the
    # other reporting mirrors.
    if minimal_metrics:
        cash_carry_report_df = pd.DataFrame(columns=CASH_CARRY_REPORT_COLUMNS)
    else:
        cash_carry_report_df = pd.DataFrame(carry_events, columns=CASH_CARRY_REPORT_COLUMNS)

    return {
        "equity_curve": equity_df,
        "actual_weights": actual_weights_df,
        "rebalance_report": rebalance_report_df,
        "suspension_report": suspension_report_df,
        "deferral_report": deferral_report_df,
        "cash_carry_report": cash_carry_report_df,
        "execution_price": execution_price,
        # (B3.2) Total square-root impact charged to cash over the run. A plain
        # accumulator (like `total_turnover`), so it is reported in
        # `minimal_metrics` mode too, and it is 0.0 whenever the model is off.
        "total_market_impact": float(total_impact),
        # (B3.3 / B3.4) Carry totals. `total_cash_interest` is money credited to
        # cash, `total_borrow_fees` money taken out of it, and
        # `total_borrow_fees_unpaid` the part a depleted balance could not pay.
        # All three are 0.0 when the switches are off.
        "total_cash_interest": float(total_cash_interest),
        "total_borrow_fees": float(total_borrow_fees),
        "total_borrow_fees_unpaid": float(total_borrow_fees_unpaid),
        # (B3.3) True if interest was credited while a cash substitute was held.
        "cash_yield_proxy_overlap": bool(cash_proxy_overlap),
        "cash_yield_annual": cash_yield_annual,
        "borrow_fee_annual": borrow_fee_annual,
        "total_turnover": total_turnover,
        "total_rebalances": int(is_rebalance.sum()),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe_ratio": sr,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "lifecycle_records": lifecycle_records_out,
        # (F4) Reports whether the state machine ACTUALLY produced the weights
        # above, not merely whether the flag was set: `enable_position_lifecycle`
        # is only ever honest together with a simulation (or a caller-supplied
        # non-empty record list from one), so this can never again claim
        # "lifecycle enabled" over raw, unfiltered targets.
        "position_lifecycle_enabled": bool(enable_position_lifecycle and lifecycle_simulated),
    }

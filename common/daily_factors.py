"""Daily cross-sectional factor primitives (Task M3.1).

Pure functions -- no IO, no network, no global state. Every function takes a
*daily panel*: a ``DataFrame`` indexed by date (one row per trading day) with
one column per symbol. Auxiliary series (e.g. benchmark returns) are passed
explicitly where a factor needs them.

Consumed by ``pipeline/instrument_selection/selectorbot/scoring.py`` only, and
only behind its opt-in ``use_daily_factors`` flag. These factors are
**deliberately not** wired into any ``AllocationTemplate``: there is exactly one
consumer, so nothing here has to obey the backtester's sparse-weights signal
contract.

NaN CONTRACT (read this before "fixing" a warning)
--------------------------------------------------
These are *factor values* meant for cross-sectional ranking. Wherever a value is
not computable -- the window is not full yet, the skip region of the momentum
factor, an all-NaN or zero-volume window -- the result is ``NaN`` and **never**
``0.0``. A fabricated ``0.0`` is a real number to a percentile rank: it would
land in the middle of the cross-section and silently pollute selection instead
of being excluded (``selectorbot.scoring._pct_rank`` drops NaN from both the
ranking *and* the denominator, so NaN is the only correct "no opinion" value).

This is a DIFFERENT contract from the sparse-weights ``NaN`` vs ``0.0`` rule in
``common/allocation_templates.py`` -- there ``0.0`` inside a rebalance row means
"explicitly exit this symbol" and ``NaN`` means "carry the previous weight".
Both rules are correct for their own consumer; do not mix them.

CAUSALITY
---------
Every function uses data up to and including ``t`` only (trailing windows,
positive lags). ``common/tests/test_daily_factors.py`` pins this with a
future-invariance test: appending later rows must not change earlier values.

ACADEMIC GROUNDING
------------------
* VWAP level / slope: Berkowitz, Logue & Noser (1988) and the intraday
  mean-reversion literature (Heston, Korajczyk & Sadka 2010) -- price trading
  away from the volume-weighted average is a documented short-horizon signal.
* Idiosyncratic volatility (IVOL): Ang, Hodrick, Xing & Zhang (2006),
  "The Cross-Section of Volatility and Expected Returns", JF -- residual vol
  after removing market/benchmark exposure is priced in the cross-section.
* 1M-1W momentum: Jegadeesh & Titman (1993) with the standard one-week skip
  (Asness, Moskowitz & Pedersen 2013) to avoid the short-term reversal that
  contaminates a raw 21-day return.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from common.intraday_features import detect_limit_pct_and_tick

__all__ = [
    "ANNUALIZATION_DAYS",
    "DEFAULT_MOM_LOOKBACK",
    "DEFAULT_MOM_SKIP",
    "DEFAULT_VWAP_LOOKBACK",
    "DEFAULT_IVOL_LOOKBACK",
    "amihud_illiquidity",
    "amihud_illiquidity_ratio",
    "intraday_return",
    "ivol_residual",
    "limit_up_magnet_and_break",
    "max_return",
    "momentum_1m_1w",
    "overnight_return",
    "price_to_vwap_ratio",
    "vwap",
    "vwap_slope",
]

# Trading days per year. Kept as a module constant so the annualization of
# `ivol_residual` is auditable from one place (and the tests can assert it).
ANNUALIZATION_DAYS = 252

# Defaults are also surfaced as `score_universe` keyword defaults (Task M3.1);
# keep the two in sync.
DEFAULT_VWAP_LOOKBACK = 20
DEFAULT_IVOL_LOOKBACK = 63
DEFAULT_MOM_SKIP = 5
DEFAULT_MOM_LOOKBACK = 20


def _as_float_panel(panel: pd.DataFrame, name: str) -> pd.DataFrame:
    """Validate a daily panel and return it as float (never mutating the input).

    A non-DataFrame (e.g. a Series for a single symbol) is rejected rather than
    silently promoted: the cross-sectional functions below all assume the
    column axis is the symbol axis, and guessing that from a Series' name is
    exactly the kind of implicit contract change that breaks callers later.
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame (date x symbol) panel, got {type(panel).__name__}")
    return panel.astype(float)


# ---------------------------------------------------------------------------
# 1-3: VWAP family
# ---------------------------------------------------------------------------
def vwap(close: pd.DataFrame, volume: pd.DataFrame, lookback: int = DEFAULT_VWAP_LOOKBACK) -> pd.DataFrame:
    """Rolling volume-weighted average price over the trailing ``lookback`` days.

    ``vwap_t = sum_{i=t-L+1..t}(close_i * volume_i) / sum_{i=t-L+1..t}(volume_i)``

    DISCLOSED APPROXIMATION: this is a rolling *daily-close* VWAP, not an
    intraday VWAP built from tick/bar data (no such data is available offline).
    It answers "where did the volume actually transact over the window", which is
    the quantity the slope/ratio factors below are defined on.

    Returns NaN until ``lookback`` observations exist, and NaN for a window whose
    total volume is zero (0/0 -- emitting ``inf`` would poison a later mean, and
    emitting ``0.0`` would rank as a real price level).
    """
    lb = int(lookback)
    if lb < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    close = _as_float_panel(close, "close")
    volume = _as_float_panel(volume, "volume")
    # Align volume onto close's exact axes so a caller's column-order mismatch
    # cannot silently multiply symbol A's price by symbol B's volume.
    volume = volume.reindex(index=close.index, columns=close.columns)

    traded_value = close * volume
    numerator = traded_value.rolling(lb, min_periods=lb).sum()
    denominator = volume.rolling(lb, min_periods=lb).sum()
    # `where` turns 0 -> NaN so the division yields NaN (not inf) for an empty
    # (zero-volume) window.
    return numerator / denominator.where(denominator != 0.0)


def price_to_vwap_ratio(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    lookback: int = DEFAULT_VWAP_LOOKBACK,
) -> pd.DataFrame:
    """``close / vwap(lookback)`` -- how far price sits from the volume-weighted
    average of its own recent history (>1 = trading above VWAP)."""
    return _as_float_panel(close, "close") / vwap(close, volume, lookback)


def vwap_slope(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    lookback: int = DEFAULT_VWAP_LOOKBACK,
) -> pd.DataFrame:
    """OLS slope of the rolling VWAP series, normalized by ``mean(vwap)``.

    ``slope_t = OLS_slope(vwap_{t-L+1..t} ~ time) / mean(vwap_{t-L+1..t})``

    WHY NORMALIZED BY THE WINDOW MEAN: a raw price slope is in currency units per
    day, so a $500 ETF and a $20 stock with identical *percentage* trends get
    incomparable slopes and the cross-sectional rank would just be a price-level
    rank. Dividing by the same-window mean VWAP makes the factor unit-free
    (fraction of the average price per day) and therefore comparable across
    symbols -- and exactly invariant to any common price scaling.

    DISCLOSED: the VWAP series is itself smoothed with the same ``lookback``, so
    the fit starts only once ``2 * lookback - 1`` rows exist. That double
    smoothing is intentional (the factor is "is the *volume-weighted* trend
    rising"), and it is disclosed here because it makes the warmup longer than
    the other factors'.

    Signed, not absolute: a falling VWAP trend is a negative value, which is the
    economically meaningful direction for a trending-selection factor.
    """
    lb = int(lookback)
    if lb < 2:
        raise ValueError(f"lookback must be >= 2 for a slope fit, got {lookback}")
    vw = vwap(close, volume, lb)

    # Row position (0..n-1) as float. Using the position rather than the date
    # keeps equal spacing even across gaps/holidays (a daily-panel convention:
    # one row == one trading day).
    pos = pd.Series(np.arange(len(vw), dtype=float), index=vw.index)
    window_sum = vw.rolling(lb, min_periods=lb).sum()
    window_sum_pos = vw.mul(pos, axis="index").rolling(lb, min_periods=lb).sum()

    # For the window ending at row position p, with j the 0-based position of a
    # value INSIDE the window (j = i - (p - L + 1)):
    #   sum_j ((j - (L-1)/2) * y_j) = sum_i(i*y_i) - (p - (L-1)/2) * sum_i(y_i)
    # because sum_j j = L*(L-1)/2. The OLS slope with a free intercept is exactly
    # that centered cross-product over sum_j (j - (L-1)/2)^2 = L*(L^2-1)/12.
    # `axis="index"` is mandatory: a DataFrame minus a bare Series aligns on
    # COLUMNS by default, which would broadcast the date-axis positions across
    # the symbol axis and silently produce a (dates x dates) union frame.
    centered_sum = window_sum_pos - window_sum.mul(pos - (lb - 1) / 2.0, axis="index")
    slope = centered_sum / (lb * (lb * lb - 1) / 12.0)

    window_mean = window_sum / lb
    return slope / window_mean.where(window_mean != 0.0)


# ---------------------------------------------------------------------------
# 4: idiosyncratic volatility
# ---------------------------------------------------------------------------
def ivol_residual(
    close: pd.DataFrame,
    lookback: int = DEFAULT_IVOL_LOOKBACK,
    benchmark_returns: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Annualized residual (idiosyncratic) volatility vs. a benchmark.

    ``ivol_t = std(residuals of ret_sym ~ a + b*ret_bench over t-L+1..t) * sqrt(252)``

    ``benchmark_returns=None`` uses the **cross-sectional equal-weight mean** of
    the panel's own returns on each date -- the offline-friendly default, since
    the scoring pass must not require a network-fetched index series. A
    single-symbol panel has no cross-section to average, so that path returns
    all-NaN instead of a trivially-zero "IVOL vs itself" (see the NaN contract).

    Residual std is SSE/(window-1), the same ddof=1 normalization as the rolling
    variances it is assembled from. At ``lookback=63`` the ddof choice is a
    uniform ~0.8% scale factor and changes no ranking; it is stated so nobody
    later "corrects" it into a behaviour change.

    DELIBERATELY NOT UNIFIED with `pipeline/research_strategy/rs/
    residual_momentum_strategy.py`'s internal 252d-residual / 21d-signal
    convention. That strategy's convention is a private implementation detail of
    a strategy that consumes this statistic *in time series*; this module's
    contract is a single cross-sectional consumer (instrument selection) that is
    not used by any template, so sharing the constants would couple two
    unrelated numerics for no benefit -- and would silently change one of them
    the day the other is tuned.
    """
    lb = int(lookback)
    if lb < 2:
        raise ValueError(f"lookback must be >= 2, got {lookback}")
    close = _as_float_panel(close, "close")
    # fill_method=None: never fabricate returns across a trading gap (that would
    # be both non-causal-looking and a fabricated factor input).
    returns = close.pct_change(fill_method=None)

    if benchmark_returns is None:
        if close.shape[1] < 2:
            return pd.DataFrame(np.nan, index=close.index, columns=close.columns)
        bench = returns.mean(axis=1, skipna=True)
    else:
        if not isinstance(benchmark_returns, pd.Series):
            raise TypeError(
                "benchmark_returns must be a pandas Series indexed by date, got "
                f"{type(benchmark_returns).__name__}"
            )
        bench = benchmark_returns.reindex(close.index).astype(float)

    var_x = bench.rolling(lb, min_periods=lb).var(ddof=1)
    out = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for symbol in close.columns:
        y = returns[symbol]
        var_y = y.rolling(lb, min_periods=lb).var(ddof=1)
        cov_xy = y.rolling(lb, min_periods=lb).cov(bench)
        # SSE/(L-1) = var_y - cov^2/var_x (the usual R^2 decomposition, valid
        # for the univariate OLS with intercept). Clipping at 0 absorbs float
        # noise on a perfect fit, where the algebra says exactly zero.
        residual_var = (var_y - cov_xy * cov_xy / var_x.where(var_x != 0.0)).clip(lower=0.0)
        out[symbol] = np.sqrt(residual_var) * np.sqrt(ANNUALIZATION_DAYS)
    return out


# ---------------------------------------------------------------------------
# 5: 1-month minus 1-week momentum
# ---------------------------------------------------------------------------
def momentum_1m_1w(
    close: pd.DataFrame,
    skip: int = DEFAULT_MOM_SKIP,
    lookback: int = DEFAULT_MOM_LOOKBACK,
) -> pd.DataFrame:
    """``close[t-skip] / close[t-lookback] - 1`` -- 1-month momentum with the
    last week skipped.

    The skip drops the short-horizon reversal that dominate the last few days
    (Jegadeesh 1990; Asness-Moskowitz-Pedersen 2013 use the same "1-month lag"
    convention). Both legs must be available, so the first ``lookback`` rows are
    NaN (the skip region alone would be a shorter warmup, but the *lookback* lag
    is the binding constraint).
    """
    sk = int(skip)
    lb = int(lookback)
    if sk < 0:
        raise ValueError(f"skip must be >= 0, got {skip}")
    if lb <= sk:
        raise ValueError(f"lookback ({lookback}) must be greater than skip ({skip})")
    close = _as_float_panel(close, "close")
    delayed = close.shift(sk)
    base = close.shift(lb).replace(0.0, np.nan)  # guard a corrupt 0.0 price
    return delayed / base - 1.0


# ---------------------------------------------------------------------------
# 6-8: MS4 overnight/intraday return decomposition & limit-up magnet / break
# ---------------------------------------------------------------------------
def overnight_return(open_panel: pd.DataFrame, close_panel: pd.DataFrame) -> pd.DataFrame:
    """Overnight return ``Open_t / Close_{t-1} - 1`` (Task MS4).

    Row 0 and any cell with non-positive or missing ``Open_t`` / ``Close_{t-1}``
    returns ``NaN`` (never ``0.0``). Satisfies the exact compound identity
    ``(1 + overnight_return) * (1 + intraday_return) == Close_t / Close_{t-1}``.
    """
    close = _as_float_panel(close_panel, "close_panel")
    open_ = _as_float_panel(open_panel, "open_panel").reindex(
        index=close.index, columns=close.columns
    )
    valid_open = open_.where(open_ > 0.0)
    prev_close = close.where(close > 0.0).shift(1)
    return (valid_open / prev_close) - 1.0


def intraday_return(open_panel: pd.DataFrame, close_panel: pd.DataFrame) -> pd.DataFrame:
    """Intraday open-to-close return ``Close_t / Open_t - 1`` (Task MS4).

    Any cell with non-positive or missing ``Open_t`` / ``Close_t`` returns ``NaN``.
    """
    close = _as_float_panel(close_panel, "close_panel")
    open_ = _as_float_panel(open_panel, "open_panel").reindex(
        index=close.index, columns=close.columns
    )
    valid_open = open_.where(open_ > 0.0)
    valid_close = close.where(close > 0.0)
    return (valid_close / valid_open) - 1.0


def limit_up_magnet_and_break(
    open_panel: pd.DataFrame,
    high_panel: pd.DataFrame,
    low_panel: pd.DataFrame,
    close_panel: pd.DataFrame,
    volume_panel: pd.DataFrame,
    *,
    magnet_zone_fraction: float = 0.80,
    vol_lookback: int = 20,
    break_penalty_weight: float = 1.5,
    tick_tolerance: float = 1e-4,
) -> pd.DataFrame:
    """A-share board-aware limit-up magnet boost minus touch-and-break penalty (MS4).

    Reuses ``common.intraday_features.detect_limit_pct_and_tick`` per column symbol
    so Main Board (10%), ChiNext/STAR (20%), BSE (30%), and ST (5%) limits and tick
    sizes are respected automatically.

    - **Magnet component** ``M_t in [0, 1]``: positive when ``Close_t`` finishes in
      the upper magnet zone ``[magnet_zone_fraction * limit_pct, limit_pct]``.
    - **Break penalty** ``B_t >= 0``: active when ``High_t`` touched the limit-up
      zone (``High_t >= P_limit - tol`` or ``u_high >= 0.98``) and failed to seal
      (``Close_t < P_limit - tol``), scaled by pullback depth and relative volume.
    - Net score ``M_t - B_t``: strongly positive for sealed limit-ups / magnet closes,
      strongly negative for high-volume broken boards ("炸板"), and ``NaN`` on row 0
      or invalid/zero-volume cells.
    """
    if not (0.0 < magnet_zone_fraction < 1.0):
        raise ValueError(
            f"magnet_zone_fraction must lie in (0, 1), got {magnet_zone_fraction!r}"
        )
    if vol_lookback < 1:
        raise ValueError(f"vol_lookback must be >= 1, got {vol_lookback!r}")

    close = _as_float_panel(close_panel, "close_panel")
    open_ = _as_float_panel(open_panel, "open_panel").reindex_like(close)
    high = _as_float_panel(high_panel, "high_panel").reindex_like(close)
    low = _as_float_panel(low_panel, "low_panel").reindex_like(close)
    vol = _as_float_panel(volume_panel, "volume_panel").reindex_like(close)

    out = pd.DataFrame(np.nan, index=close.index, columns=close.columns, dtype=np.float64)
    prev_close = close.shift(1)

    for sym in close.columns:
        limit_pct, tick = detect_limit_pct_and_tick(str(sym))
        c_s = close[sym].to_numpy(dtype=np.float64)
        pc_s = prev_close[sym].to_numpy(dtype=np.float64)
        o_s = open_[sym].to_numpy(dtype=np.float64)
        h_s = high[sym].to_numpy(dtype=np.float64)
        l_s = low[sym].to_numpy(dtype=np.float64)
        v_s = vol[sym].to_numpy(dtype=np.float64)

        pos_vol_series = vol[sym].where(vol[sym] > 0.0)
        vol_sma = pos_vol_series.rolling(window=vol_lookback, min_periods=1).mean().to_numpy(
            dtype=np.float64
        )

        valid = (
            np.isfinite(pc_s)
            & (pc_s > 0.0)
            & np.isfinite(o_s)
            & (o_s > 0.0)
            & np.isfinite(h_s)
            & (h_s > 0.0)
            & np.isfinite(l_s)
            & (l_s > 0.0)
            & np.isfinite(c_s)
            & (c_s > 0.0)
            & np.isfinite(v_s)
            & (v_s > 0.0)
            & np.isfinite(vol_sma)
            & (vol_sma > 0.0)
        )

        scores = np.full(len(close), np.nan, dtype=np.float64)
        if np.any(valid):
            pc_v = pc_s[valid]
            h_v = h_s[valid]
            c_v = c_s[valid]
            rel_vol = v_s[valid] / vol_sma[valid]

            p_limit = np.round(np.round((pc_v * (1.0 + limit_pct)) / tick) * tick, 4)
            u_close = (c_v / pc_v - 1.0) / limit_pct
            u_high = (h_v / pc_v - 1.0) / limit_pct

            magnet = np.clip(
                (u_close - magnet_zone_fraction) / (1.0 - magnet_zone_fraction),
                0.0,
                1.0,
            )

            touched_limit = (h_v >= p_limit - tick_tolerance) | (u_high >= 0.98)
            failed_seal = c_v < (p_limit - tick_tolerance)
            broken = touched_limit & failed_seal

            pullback = np.where(
                broken,
                np.maximum(0.0, (h_v - c_v) / (pc_v * limit_pct)),
                0.0,
            )
            penalty = break_penalty_weight * pullback * np.clip(rel_vol, 0.5, 5.0)
            scores[valid] = magnet - penalty

        out[sym] = scores

    return out


# ---------------------------------------------------------------------------
# 9-11: MS15 Amihud illiquidity & MS16 lottery-demand MAX return (top-k mean)
# ---------------------------------------------------------------------------
def amihud_illiquidity(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    lookback: int = 20,
    *,
    amount: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Rolling Amihud (2002) illiquidity ``mean(|ret_t| / dollar_vol_t)`` (Task MS15).

    Academic Grounding
    ------------------
    Amihud (2002), "Illiquidity and stock returns: cross-section and time-series
    effects", *Journal of Financial Markets* 5(1), 31-56.

    Unit & Scale Contract
    ---------------------
    When ``amount`` is supplied, dollar volume uses ``amount`` wherever ``amount > 0``
    and falls back cell-wise to ``close * volume`` where ``amount`` is missing or
    non-positive. When ``amount is None``, dollar volume is ``close * volume``
    directly in the caller's native currency/share units (no hidden ``1e6`` or
    ``* 100`` rescaling). Non-positive denominators and warmup rows (rows
    ``0 .. lookback - 1``) return ``NaN`` (never ``0.0`` or ``inf``).
    """
    lb = int(lookback)
    if lb < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")

    close_f = _as_float_panel(close, "close")
    vol_f = _as_float_panel(volume, "volume")
    if not close_f.index.equals(vol_f.index) or not close_f.columns.equals(vol_f.columns):
        raise ValueError("close and volume must share identical index and columns")

    ret = close_f.pct_change(fill_method=None)
    fallback_dvol = (close_f * vol_f).where((close_f > 0.0) & (vol_f > 0.0))

    if amount is not None:
        amt_f = _as_float_panel(amount, "amount").reindex_like(close_f)
        valid_amt = amt_f.where(amt_f > 0.0)
        dvol = valid_amt.combine_first(fallback_dvol)
    else:
        dvol = fallback_dvol

    illiq_1d = ret.abs() / dvol
    return illiq_1d.rolling(window=lb, min_periods=lb).mean()


def amihud_illiquidity_ratio(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    short: int = 5,
    long: int = 20,
    *,
    amount: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Short-to-long Amihud illiquidity shock ratio ``SMA(Illiq, short) / SMA(Illiq, long)`` (MS15).

    Unsigned by construction so callers control direction (liquidity repair vs
    illiquidity shock). Zero or ``NaN`` denominators emit ``NaN`` (never ``0.0``).
    """
    s_lb = int(short)
    l_lb = int(long)
    if s_lb < 1 or l_lb <= s_lb:
        raise ValueError(f"require 1 <= short < long, got short={short}, long={long}")

    num = amihud_illiquidity(close, volume, lookback=s_lb, amount=amount)
    den = amihud_illiquidity(close, volume, lookback=l_lb, amount=amount)
    safe_den = den.where(den > 0.0)
    return num / safe_den


def max_return(
    close: pd.DataFrame,
    lookback: int = 20,
    k: int = 2,
) -> pd.DataFrame:
    """Rolling mean of the top-``k`` daily returns over ``lookback`` bars (Task MS16).

    Academic Grounding
    ------------------
    Bali, Cakici & Whitelaw (2011), "Maxing out: Stocks as lotteries and the
    cross-section of expected returns", *Journal of Financial Economics* 99(2).

    When ``k == 1``, degenerates to the rolling single-day maximum return; when
    ``k >= 2``, strictly computes the mean of the ``k`` highest daily returns
    (including duplicate tie values) in each full ``lookback`` window — never a
    single-point ``rolling().max()``. Raw values stay physical (negative-factor
    ranking belongs in the score layer). First ``lookback`` rows are ``NaN``.
    """
    lb = int(lookback)
    k_int = int(k)
    if lb < 1 or k_int < 1 or k_int > lb:
        raise ValueError(f"require 1 <= k <= lookback, got k={k}, lookback={lookback}")

    close_f = _as_float_panel(close, "close")
    ret = close_f.pct_change(fill_method=None)
    arr = ret.to_numpy(dtype=np.float64, copy=False)
    n_rows, n_cols = arr.shape
    out = np.full((n_rows, n_cols), np.nan, dtype=np.float64)

    for t in range(lb, n_rows):
        win = arr[t - lb + 1 : t + 1, :]  # (lb, n_cols)
        valid_col = np.all(np.isfinite(win), axis=0)
        if not np.any(valid_col):
            continue
        sub = win[:, valid_col]
        # Partition along axis=0 so the k largest values sit in the last k rows
        part = np.partition(sub, lb - k_int, axis=0)
        out[t, valid_col] = np.mean(part[lb - k_int :, :], axis=0)

    return pd.DataFrame(out, index=close_f.index, columns=close_f.columns, dtype=np.float64)

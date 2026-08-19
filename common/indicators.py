"""Shared technical indicators used across every project in this workspace:
RSI (Wilder + Cutler variants), SMA, ATR/ATR%, ADX, realized volatility,
volatility-of-volatility, and the ATR volatility-regime-change ratio.

Each project's own `indicators.py`/`volatility.py` re-exports the subset it
needs (and keeps any project-specific logic, like a trend-regime classifier
or a config-driven summary dict, local to that project) so existing call
sites and signatures are unchanged.
"""

import numpy as np
import pandas as pd


def _gains_losses(close: pd.Series) -> tuple:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    return gain, loss


def _resolve_zero_loss_rsi(result: pd.Series, avg_gain: pd.Series, avg_loss: pd.Series) -> pd.Series:
    """`100 - 100/(1+rs)` is undefined (NaN, via a 0/0 `rs`) wherever
    `avg_loss == 0`, which covers two very different cases that a plain
    `.where(avg_loss != 0, 100.0)` used to conflate:

    - all gains, zero losses over the window -> genuinely maximal RSI, 100.0.
    - a completely FLAT window (zero gains AND zero losses, e.g. a constant
      price series) -> no directional information at all, so RSI should be
      the neutral midpoint, 50.0, not 100.0.
    """
    flat = (avg_loss == 0) & (avg_gain == 0)
    result = result.where(avg_loss != 0, 100.0)
    return result.where(~flat, 50.0)


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI: recursively smooths average gain/loss with weight 1/n
    on the newest bar (equivalent to a (2n-1)-period EMA)."""
    gain, loss = _gains_losses(close)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return _resolve_zero_loss_rsi(result, avg_gain, avg_loss)


def rsi_cutler(close: pd.Series, period: int) -> pd.Series:
    """Cutler's/"plain" RSI: simple moving average of gains/losses instead
    of Wilder's recency-weighted smoothing, trading recency weighting for a
    result that doesn't depend on how far back the calculation window starts."""
    gain, loss = _gains_losses(close)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return _resolve_zero_loss_rsi(result, avg_gain, avg_loss)


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Plain alias for Wilder's RSI -- the default most callers want when
    there's no need to choose between smoothing methods."""
    return rsi_wilder(close, period)


def cumulative_rsi(rsi_series: pd.Series, lookback: int) -> pd.Series:
    """Sum of RSI over the trailing `lookback` bars (the Connors 'cumulative RSI(2)' variant)."""
    return rsi_series.rolling(window=lookback, min_periods=lookback).sum()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range. df must have columns High, Low, Close."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return atr(df, period) / df["Close"]


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average Directional Index -- conventionally, ADX >= ~25
    signals a trending market, ADX <= ~20 a ranging one."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def realized_vol(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    returns = close.pct_change()
    return returns.rolling(window, min_periods=window).std() * np.sqrt(periods_per_year)


def downside_realized_vol(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Annualized downside semi-deviation (std of negative returns relative to 0).
    Grounding: Estrada (2000), Ang, Chen & Xing (2006, J. Finance "Downside Risk").
    Isolates loss volatility rather than penalizing upside gains."""
    returns = close.pct_change()
    downside_sq = returns.clip(upper=0.0) ** 2
    return np.sqrt(downside_sq.rolling(window, min_periods=window).mean()) * np.sqrt(periods_per_year)


def vol_of_vol(close: pd.Series, vol_window: int = 20, vov_window: int = 60,
               periods_per_year: int = 252) -> pd.Series:
    """Rolling std of the realized-vol series itself -- a volatility-
    clustering measure: high vol-of-vol means volatility regimes are
    themselves unstable, harder for any strategy to size risk against."""
    vol = realized_vol(close, vol_window, periods_per_year)
    return vol.rolling(vov_window, min_periods=vov_window).std()


def atr_regime_ratio(df: pd.DataFrame, period: int = 14, short_window: int = 20, long_window: int = 60) -> pd.Series:
    """Short-term ATR% vs its own longer-term average. A ratio >= 1.30 is a
    commonly-cited illustrative trigger for a volatility-regime change (cut
    size, widen stops); well below 1.0 signals volatility compression."""
    a_pct = atr_pct(df, period)
    short_avg = a_pct.rolling(short_window, min_periods=short_window).mean()
    long_avg = a_pct.rolling(long_window, min_periods=long_window).mean()
    return short_avg / long_avg


def roc(close: pd.Series, period: int = 252) -> pd.Series:
    """Rate of change / raw price momentum: the trailing `period`-bar return
    (`close / close[t-period] - 1`). With period ~= 252 (12 months) this is
    the exact signal behind the cross-sectional (Jegadeesh & Titman 1993) and
    time-series (Moskowitz, Ooi & Pedersen 2012) momentum anomalies."""
    return close / close.shift(period) - 1


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Moving Average Convergence-Divergence (Appel). Returns a frame with the
    `macd` line (fast EMA - slow EMA), its `signal` EMA, and the `hist`ogram
    (macd - signal). A standard practitioner momentum indicator; its
    stand-alone profitability is weak once data-snooping is corrected for
    (Park & Irwin 2007), so this project reports it descriptively rather than
    scoring the raw signal."""
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=slow).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=slow + signal).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line})


def ema(close: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average (span/adjust=False convention,
    matching this module's own `macd` internals) -- distinct from Wilder's
    alpha=1/period smoothing used by `rsi_wilder`/`atr`/`adx`, which is a
    different (slower-decaying) weighting, not just a naming variant."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands (Bollinger 1980s): a `period`-bar SMA envelope +/-
    `num_std` rolling standard deviations. Returns `mid`/`upper`/`lower` plus
    two derived, commonly-used descriptive columns: `pctb` (%B, where price
    sits within the band: 0.0 = lower band, 1.0 = upper band, can exceed
    [0,1] on a breakout) and `bandwidth` ((upper-lower)/mid, a squeeze/
    expansion measure)."""
    mid = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    pctb = (close - lower) / (upper - lower).replace(0, np.nan)
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "pctb": pctb, "bandwidth": bandwidth})


def stochastic_oscillator(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """Stochastic Oscillator (Lane): %K measures where the close sits within
    the trailing `k_period`-bar high-low range (0-100); %D is a `d_period`-bar
    SMA of %K. df must have columns High, Low, Close."""
    high, low, close = df["High"], df["Low"], df["Close"]
    lowest_low = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()
    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    d = k.rolling(window=d_period, min_periods=d_period).mean()
    return pd.DataFrame({"k": k, "d": d})


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index (Lambert 1980): typical price's deviation from
    its own SMA, scaled by mean absolute deviation (the constant 0.015 sets
    the conventional +/-100 band). df must have columns High, Low, Close."""
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    tp_sma = sma(typical_price, period)
    mean_abs_dev = typical_price.rolling(window=period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    return (typical_price - tp_sma) / (0.015 * mean_abs_dev.replace(0, np.nan))


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R (Williams): where the close sits within the trailing
    `period`-bar high-low range, on a 0 to -100 scale (0 = period high,
    -100 = period low) -- the same underlying construction as the Stochastic
    %K, rescaled and inverted. df must have columns High, Low, Close."""
    high, low, close = df["High"], df["Low"], df["Close"]
    highest_high = high.rolling(window=period, min_periods=period).max()
    lowest_low = low.rolling(window=period, min_periods=period).min()
    return -100 * (highest_high - close) / (highest_high - lowest_low).replace(0, np.nan)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume (Granville 1963): cumulative volume, added on an
    up-close bar and subtracted on a down-close bar -- a running measure of
    whether volume is flowing into or out of an instrument. df must have
    columns Close, Volume (the one indicator in this module that needs
    Volume; most synthetic OHLCV test fixtures in this workspace don't
    generate one -- see common/data.py's SyntheticDataProvider, which does)."""
    close, volume = df["Close"], df["Volume"]
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


# --------------------------------------------------------------------------
# Candlestick reversal patterns (OHLC single-/two-/three-bar patterns).
#
# These detect the classic Japanese-candlestick reversal patterns from the
# open/high/low/close relationships within and across bars. They are the raw
# building block for `instrument_selection`'s candlestick-predictability
# component; see that project's `candlestick.py` for the significance test and
# the (largely negative) academic evidence on whether these patterns actually
# predict anything in liquid markets. Pattern geometry follows the standard
# definitions (Nison 1991; Morris 2006) as summarized in the studies that
# tested them (Caginalp & Laurent 1998; Marshall, Young & Rose 2006).
#
# Every reversal pattern is only meaningful when it interrupts a prior trend
# (a "hammer" in a downtrend is a bullish reversal; the identical shape in an
# uptrend is a bearish "hanging man"), so each detector is gated on a simple
# preceding-trend context, computed exactly as Caginalp & Laurent did: a short
# moving average that has been rising (uptrend) or falling (downtrend) over its
# own window, measured through the bar BEFORE the pattern completes.
# --------------------------------------------------------------------------


def _candle_parts(df: pd.DataFrame) -> dict:
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    rng = (h - l).replace(0, np.nan)
    body = c - o
    abody = body.abs()
    upper_shadow = h - c.combine(o, max)
    lower_shadow = o.combine(c, min) - l
    return {
        "o": o, "h": h, "l": l, "c": c, "rng": rng, "body": body, "abody": abody,
        "upper": upper_shadow, "lower": lower_shadow,
        "white": c > o, "black": c < o,
    }


def _trend_context(close: pd.Series, window: int) -> tuple:
    """(prior_uptrend, prior_downtrend) booleans: whether an SMA(window) rose
    or fell over its own window, measured through the bar before the signal
    (shift(1)). Matches Caginalp & Laurent (1998)'s use of a short MA to
    require a pattern to actually interrupt a preceding trend."""
    ma = close.rolling(window, min_periods=window).mean()
    rose = (ma > ma.shift(window)).shift(1).fillna(False)
    fell = (ma < ma.shift(window)).shift(1).fillna(False)
    return rose, fell


def candlestick_patterns(df: pd.DataFrame, trend_window: int = 5, doji_body_frac: float = 0.1,
                         shadow_ratio: float = 2.0, small_body_frac: float = 0.5) -> pd.DataFrame:
    """Boolean DataFrame (indexed like `df`) with one column per detected
    candlestick pattern. `df` must have Open/High/Low/Close columns.

    - `trend_window`: SMA window used for the preceding-trend gate.
    - `doji_body_frac`: body <= this fraction of the high-low range -> doji.
    - `shadow_ratio`: the long shadow of a hammer/star must be >= this many
      times the body length.
    - `small_body_frac`: "small body" threshold (fraction of range, and of a
      neighbouring bar's body) used by star/harami-type patterns.
    """
    p = _candle_parts(df)
    o, h, l, c = p["o"], p["h"], p["l"], p["c"]
    rng, body, abody = p["rng"], p["body"], p["abody"]
    upper, lower, white, black = p["upper"], p["lower"], p["white"], p["black"]
    up, down = _trend_context(c, trend_window)

    small_body = abody <= (small_body_frac * rng)
    long_lower = lower >= (shadow_ratio * abody)
    long_upper = upper >= (shadow_ratio * abody)
    tiny_upper = upper <= abody
    tiny_lower = lower <= abody

    po, ph, pl, pc = o.shift(1), h.shift(1), l.shift(1), c.shift(1)
    p_white, p_black = white.shift(1, fill_value=False), black.shift(1, fill_value=False)
    p_abody = abody.shift(1)
    p_mid = (po + pc) / 2
    body_hi, body_lo = c.combine(o, max), c.combine(o, min)
    p_body_hi, p_body_lo = pc.combine(po, max), pc.combine(po, min)

    out = pd.DataFrame(index=df.index)

    # --- neutral (indecision), reported but not a directional signal ---
    out["doji"] = (abody <= (doji_body_frac * rng))

    # --- single-bar reversals ---
    out["hammer"] = down & small_body & long_lower & tiny_upper
    out["hanging_man"] = up & small_body & long_lower & tiny_upper
    out["inverted_hammer"] = down & small_body & long_upper & tiny_lower
    out["shooting_star"] = up & small_body & long_upper & tiny_lower

    # --- two-bar reversals ---
    out["bullish_engulfing"] = down & p_black & white & (c >= po) & (o <= pc)
    out["bearish_engulfing"] = up & p_white & black & (o >= pc) & (c <= po)
    out["piercing_line"] = down & p_black & white & (o < pl) & (c > p_mid) & (c < po)
    out["dark_cloud_cover"] = up & p_white & black & (o > ph) & (c < p_mid) & (c > po)
    out["bullish_harami"] = down & p_black & (body_hi <= po) & (body_lo >= pc)
    out["bearish_harami"] = up & p_white & (body_hi <= pc) & (body_lo >= po)

    # --- three-bar reversals ---
    o1, c1 = o.shift(2), c.shift(2)
    abody1 = abody.shift(2)
    black1, white1 = black.shift(2, fill_value=False), white.shift(2, fill_value=False)
    small_star = abody.shift(1) <= (small_body_frac * abody1)
    out["morning_star"] = down.shift(1).fillna(False) & black1 & small_star & white & (c > (o1 + c1) / 2)
    out["evening_star"] = up.shift(1).fillna(False) & white1 & small_star & black & (c < (o1 + c1) / 2)

    higher_closes = white & (c > pc) & (c.shift(1) > c.shift(2))
    three_white = white & white.shift(1, fill_value=False) & white.shift(2, fill_value=False)
    out["three_white_soldiers"] = down.shift(1).fillna(False) & three_white & higher_closes
    lower_closes = black & (c < pc) & (c.shift(1) < c.shift(2))
    three_black = black & black.shift(1, fill_value=False) & black.shift(2, fill_value=False)
    out["three_black_crows"] = up.shift(1).fillna(False) & three_black & lower_closes

    return out.fillna(False)


BULLISH_REVERSAL_PATTERNS = [
    "hammer", "inverted_hammer", "bullish_engulfing", "piercing_line",
    "bullish_harami", "morning_star", "three_white_soldiers",
]
BEARISH_REVERSAL_PATTERNS = [
    "hanging_man", "shooting_star", "bearish_engulfing", "dark_cloud_cover",
    "bearish_harami", "evening_star", "three_black_crows",
]


def bullish_reversal_signals(df: pd.DataFrame, **kwargs) -> pd.Series:
    """True on any bar completing a bullish candlestick reversal pattern."""
    patterns = candlestick_patterns(df, **kwargs)
    return patterns[BULLISH_REVERSAL_PATTERNS].any(axis=1)


def bearish_reversal_signals(df: pd.DataFrame, **kwargs) -> pd.Series:
    """True on any bar completing a bearish candlestick reversal pattern."""
    patterns = candlestick_patterns(df, **kwargs)
    return patterns[BEARISH_REVERSAL_PATTERNS].any(axis=1)

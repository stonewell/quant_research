import numpy as np
import pandas as pd
import pytest
from common.indicators import (
    bearish_reversal_signals,
    bullish_reversal_signals,
    candlestick_patterns,
)
from common.testing import make_random_walk_df

from selectorbot.candlestick import (
    _directional_edge,
    candlestick_significance,
    candlestick_summary,
)
from selectorbot.config import SelectionConfig
from selectorbot.scoring import score_universe


def _bars(o, h, l, c, start="2015-01-01"):
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}, index=idx)


def make_reversal_cycles(n_cycles=45, down=8, up=8):
    """Deterministic sawtooth: each cycle falls (black candles), prints a
    textbook bullish ENGULFING bar at the trough, then rises (white candles).
    A bullish pattern therefore reliably precedes an up-move, and the falling
    leg reliably precedes a down-move -- a genuine, detectable candlestick
    edge the significance test should flag."""
    o, h, l, c = [], [], [], []
    price = 100.0
    for _ in range(n_cycles):
        for _ in range(down):  # declining black candles
            op, cl = price, price * 0.985
            o.append(op); c.append(cl); h.append(op * 1.002); l.append(cl * 0.998)
            price = cl
        prev_o, prev_c = o[-1], c[-1]  # last black bar
        op, cl = prev_c * 0.997, prev_o * 1.02  # opens below prev close, closes above prev open -> engulfing
        o.append(op); c.append(cl); h.append(cl * 1.002); l.append(op * 0.998)
        price = cl
        for _ in range(up):  # rising white candles
            op, cl = price, price * 1.012
            o.append(op); c.append(cl); h.append(cl * 1.002); l.append(op * 0.998)
            price = cl
    return _bars(o, h, l, c)


# --- pattern detection ---------------------------------------------------

def test_bullish_engulfing_detected_in_downtrend():
    # eight declining black bars to establish a downtrend (the trend gate is
    # measured on the bar BEFORE the pattern, so it needs real warmup), then a
    # bullish engulfing: opens below the prior close, closes above the prior open.
    o = [108, 107, 106, 105, 104, 103, 102, 101, 99.5]
    c = [107, 106, 105, 104, 103, 102, 101, 100, 101.5]
    h = [x + 0.2 for x in o[:-1]] + [101.7]
    l = [x - 0.2 for x in c[:-1]] + [99.3]
    df = _bars(o, h, l, c)
    patterns = candlestick_patterns(df, trend_window=3)
    assert bool(patterns["bullish_engulfing"].iloc[-1])
    assert bool(bullish_reversal_signals(df, trend_window=3).iloc[-1])


def test_bearish_engulfing_detected_in_uptrend():
    o = [92, 93, 94, 95, 96, 97, 98, 99, 100.5]
    c = [93, 94, 95, 96, 97, 98, 99, 100, 98.5]  # last bar black, engulfs the prior white body
    h = [x + 0.2 for x in c[:-1]] + [100.7]
    l = [x - 0.2 for x in o[:-1]] + [98.3]
    df = _bars(o, h, l, c)
    patterns = candlestick_patterns(df, trend_window=3)
    assert bool(patterns["bearish_engulfing"].iloc[-1])
    assert bool(bearish_reversal_signals(df, trend_window=3).iloc[-1])


def test_same_shape_is_bullish_in_downtrend_but_not_in_uptrend():
    # A hammer shape (small body, long lower shadow) is a bullish reversal
    # only when it interrupts a downtrend; the identical bar in an uptrend is
    # the bearish "hanging man" instead.
    down_o = [108, 107, 106, 105, 104, 103, 102, 101, 100.0]
    down_c = [107, 106, 105, 104, 103, 102, 101, 100, 100.2]
    up_o = [92, 93, 94, 95, 96, 97, 98, 99, 100.0]
    up_c = [93, 94, 95, 96, 97, 98, 99, 100, 100.2]
    hammer_h, hammer_l = 100.4, 98.0  # small body up top, long lower shadow
    down = _bars(down_o, [x + 0.2 for x in down_o[:-1]] + [hammer_h],
                 [x - 0.2 for x in down_c[:-1]] + [hammer_l], down_c)
    up = _bars(up_o, [x + 0.2 for x in up_c[:-1]] + [hammer_h],
               [x - 0.2 for x in up_o[:-1]] + [hammer_l], up_c)
    down_p = candlestick_patterns(down, trend_window=3)
    up_p = candlestick_patterns(up, trend_window=3)
    assert bool(down_p["hammer"].iloc[-1])
    assert not bool(up_p["hammer"].iloc[-1])
    assert bool(up_p["hanging_man"].iloc[-1])


# --- directional-edge math ----------------------------------------------

def test_directional_edge_is_zero_net_of_baseline_when_returns_are_pure_drift():
    idx = pd.RangeIndex(10)
    fwd = pd.Series(0.01, index=idx)  # constant forward drift, no pattern information
    bull = pd.Series([False] * 10, index=idx); bull.iloc[2] = True; bull.iloc[5] = True
    bear = pd.Series([False] * 10, index=idx); bear.iloc[7] = True
    edge, n = _directional_edge(bull, bear, fwd)
    assert n == 3
    assert edge == pytest.approx(0.0, abs=1e-12)  # signals add nothing beyond the base-rate drift


def test_directional_edge_positive_when_bullish_signals_precede_bigger_up_moves():
    idx = pd.RangeIndex(10)
    fwd = pd.Series(0.0, index=idx)
    fwd.iloc[2] = 0.05  # the bullish-signal bar is followed by a strong up move
    bull = pd.Series([False] * 10, index=idx); bull.iloc[2] = True
    bear = pd.Series([False] * 10, index=idx)
    edge, _ = _directional_edge(bull, bear, fwd)
    assert edge > 0


# --- significance test (mirrors the Hurst-significance conventions) ------

def test_candlestick_significance_flags_a_real_engineered_edge():
    df = make_reversal_cycles()
    result = candlestick_significance(df, horizon=5, seed=0)
    assert result["candlestick_n_signals"] > 20
    assert result["candlestick_edge"] > 0
    assert result["candlestick_significant"]


def test_candlestick_significance_does_not_flag_a_random_walk():
    df = make_random_walk_df(n=900, seed=11)
    result = candlestick_significance(df, horizon=5, seed=0)
    assert not result["candlestick_significant"]


def test_candlestick_summary_reports_insufficient_data_for_short_series():
    df = make_random_walk_df(n=50, seed=3)
    config = SelectionConfig()
    result = candlestick_summary(df, config)
    assert result["candlestick_label"] == "insufficient_data"
    assert np.isnan(result["candlestick_edge"])
    assert result["candlestick_significant"] is False


# --- scoring integration -------------------------------------------------

def test_candlestick_score_gates_on_significance():
    metrics = pd.DataFrame({
        "avg_dollar_volume": [1e7, 1e7],
        "median_spread_pct": [0.01, 0.01],
        "realized_vol_annualized_pct": [20.0, 20.0],
        "hurst": [0.5, 0.5],
        "hurst_significant": [False, False],
        "candlestick_edge": [0.02, 0.02],            # identical magnitude...
        "candlestick_significant": [True, False],    # ...but only one is significant
        "history_years": [10.0, 10.0],
    }, index=["SIG", "NOISE"])
    scored = score_universe(metrics)
    assert scored.loc["SIG", "candlestick_score"] > scored.loc["NOISE", "candlestick_score"]


def test_missing_candlestick_edge_does_not_penalize_overall_score():
    metrics = pd.DataFrame({
        "avg_dollar_volume": [1e7, 1e7],
        "median_spread_pct": [0.01, 0.01],
        "realized_vol_annualized_pct": [20.0, 20.0],
        "hurst": [0.6, 0.6],
        "hurst_significant": [True, True],
        "candlestick_edge": [0.02, np.nan],          # one symbol has no candlestick data
        "candlestick_significant": [True, False],
        "history_years": [10.0, 10.0],
    }, index=["HAS_IT", "MISSING"])
    scored = score_universe(metrics)
    assert np.isnan(scored.loc["MISSING", "candlestick_score"])
    assert scored.loc["MISSING", "overall_selection_score"] > 0  # weight renormalized, not penalized
    assert not np.isnan(scored.loc["MISSING", "overall_selection_score"])

"""Unit tests for research_strategy/rs/chan_signals.py -- the additive
segments (线段) / real MACD divergence (背驰) / formal 一/二/三类买卖点
extension of chan_structure.py.

All fixtures are small, hand-built, hand-verified DataFrames (for
`build_segments`/`macd_divergence`/`classify_points`) or deterministic
synthetic closes arrays (for `compute_chan3_signals`) -- no network access.
Numeric expectations for `macd_divergence` and the end-to-end
`compute_chan3_signals` fixture were computed by actually running this
module's own functions against `common.indicators.macd` (not hand-derived),
since MACD's EMA warm-up interacts with `min_periods` in a way that isn't
obvious from the formula alone -- see each fixture's comment for how it was
derived.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from research_strategy.rs.chan_signals import (
    _SEGMENT_COLUMNS,
    build_segments,
    classify_points,
    compute_chan3_signals,
    compute_chan_pivot_macd_signals,
    macd_divergence,
)


def _stroke_row(start_pos, end_pos, start_price, end_price, direction):
    return {
        "start_pos": start_pos, "end_pos": end_pos, "start_price": start_price,
        "end_price": end_price, "direction": direction, "bars": end_pos - start_pos,
    }


def _seg_row(start_pos, end_pos, start_price, end_price, direction):
    return {
        "start_pos": start_pos, "end_pos": end_pos, "start_price": start_price,
        "end_price": end_price, "direction": direction, "bars": end_pos - start_pos,
        "start_stroke_idx": 0, "end_stroke_idx": 0,
    }


# --- build_segments ---------------------------------------------------------

def test_build_segments_seeds_extends_then_terminates_on_full_retracement():
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 110, "up"),
            _stroke_row(10, 20, 110, 104, "down"),
            _stroke_row(20, 30, 104, 115, "up"),    # seed window [0,1,2]: overlap + net progress -> seeds at i=0
            _stroke_row(30, 40, 115, 108, "down"),  # pullback: 108 > last_same_start(104) -> not full retracement, extend
            _stroke_row(40, 50, 108, 120, "up"),    # new last_same
            _stroke_row(50, 60, 120, 90, "down"),   # pullback: 90 <= last_same_start(108) -> full retracement, terminate at stroke idx 4
        ]
    )
    segments = build_segments(strokes, min_strokes=3)
    assert len(segments) == 1
    seg = segments.iloc[0]
    assert seg["start_pos"] == 0 and seg["end_pos"] == 50
    assert seg["start_price"] == 100 and seg["end_price"] == 120
    assert seg["direction"] == "up"
    assert seg["start_stroke_idx"] == 0 and seg["end_stroke_idx"] == 4


def test_build_segments_returns_empty_on_the_same_fixture_build_pivots_rejects():
    # Mirrors test_build_pivots_requires_a_genuine_price_overlap: a clean
    # uptrend of non-overlapping stroke ranges never seeds a segment either.
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 110, "up"),
            _stroke_row(10, 20, 110, 120, "up"),
            _stroke_row(20, 30, 120, 130, "up"),
        ]
    )
    assert build_segments(strokes, min_strokes=3).empty


def test_build_segments_below_min_strokes_returns_empty():
    strokes = pd.DataFrame([_stroke_row(0, 10, 100, 90, "down"), _stroke_row(10, 20, 90, 98, "up")])
    assert build_segments(strokes, min_strokes=3).empty


def test_build_segments_output_columns_match_schema():
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 110, "up"),
            _stroke_row(10, 20, 110, 104, "down"),
            _stroke_row(20, 30, 104, 115, "up"),
        ]
    )
    segments = build_segments(strokes, min_strokes=3)
    assert list(segments.columns) == _SEGMENT_COLUMNS


def test_build_segments_never_overlap():
    # Two segments back to back (up then down); the second must not start
    # before the first ends, in either stroke-index or bar-position space.
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 110, "up"),
            _stroke_row(10, 20, 110, 104, "down"),
            _stroke_row(20, 30, 104, 115, "up"),
            _stroke_row(30, 40, 115, 95, "down"),   # fully retraces stroke idx 2 (start=104) -> terminates segment 0 here
            _stroke_row(40, 50, 95, 80, "down"),    # NOTE: not alternating in reality -- build_segments only reads ranges
            _stroke_row(50, 60, 80, 70, "down"),
        ]
    )
    segments = build_segments(strokes, min_strokes=3)
    if len(segments) >= 2:
        for k in range(len(segments) - 1):
            assert segments.iloc[k]["end_pos"] <= segments.iloc[k + 1]["start_pos"]


# --- macd_divergence ---------------------------------------------------------
# The (warmup, leg lengths/slopes) below were chosen by directly running
# common.indicators.macd on candidate closes arrays until the histogram
# cleared its NaN warm-up with comfortable margin and produced the intended
# area relationship -- MACD's signal-line min_periods interacts with EMA
# warm-up in a way that isn't obvious from the formula alone, so these
# numbers are verified, not hand-derived.

def _macd_divergence_fixture(warmup=100, legA_n=20, legA_slope=1.0, pause_n=3, legB_n=20, legB_slope=0.6, sign=1):
    base = 100.0
    closes = [base] * warmup
    for i in range(1, legA_n + 1):
        closes.append(base + sign * legA_slope * i)
    pause_val = closes[-1]
    closes += [pause_val] * pause_n
    for i in range(1, legB_n + 1):
        closes.append(pause_val + sign * legB_slope * i)
    closes = np.array(closes)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Close": closes}, index=idx)
    from common.indicators import macd

    hist = macd(df["Close"])["hist"].reset_index(drop=True)
    merged = pd.DataFrame({"orig_pos": np.arange(len(closes))})
    legA_start, legA_end = warmup, warmup + legA_n - 1
    legB_start, legB_end = warmup + legA_n + pause_n, warmup + legA_n + pause_n + legB_n - 1
    direction = "up" if sign > 0 else "down"
    earlier = pd.Series(
        {"start_pos": legA_start, "end_pos": legA_end, "start_price": closes[legA_start], "end_price": closes[legA_end], "direction": direction}
    )
    later = pd.Series(
        {"start_pos": legB_start, "end_pos": legB_end, "start_price": closes[legB_start], "end_price": closes[legB_end], "direction": direction}
    )
    return earlier, later, hist, merged


def test_macd_divergence_detects_a_weaker_second_leg_up():
    earlier, later, hist, merged = _macd_divergence_fixture(legA_slope=1.0, legB_slope=0.6, sign=1)
    has_div, area_earlier, area_later = macd_divergence(earlier, later, hist, merged)
    assert area_later < area_earlier
    assert has_div is True
    assert area_earlier == pytest.approx(14.6748, abs=1e-3)
    assert area_later == pytest.approx(1.0221, abs=1e-3)


def test_macd_divergence_detects_a_weaker_second_leg_down():
    earlier, later, hist, merged = _macd_divergence_fixture(legA_slope=1.0, legB_slope=0.6, sign=-1)
    has_div, area_earlier, area_later = macd_divergence(earlier, later, hist, merged)
    assert has_div is True
    assert area_later < area_earlier


def test_macd_divergence_no_signal_when_second_leg_is_stronger():
    # Mirror image: shallow first leg, steep second leg -- reaches a new
    # extreme with MORE momentum, not less, so this must NOT be flagged.
    earlier, later, hist, merged = _macd_divergence_fixture(legA_slope=0.6, legB_slope=1.0, sign=1)
    has_div, area_earlier, area_later = macd_divergence(earlier, later, hist, merged)
    assert has_div is False
    assert area_later >= area_earlier


def test_macd_divergence_requires_matching_direction():
    earlier, later, hist, merged = _macd_divergence_fixture()
    later = later.copy()
    later["direction"] = "down"
    with pytest.raises(ValueError):
        macd_divergence(earlier, later, hist, merged)


def test_macd_divergence_returns_false_never_raises_during_warmup():
    closes = np.full(50, 100.0) + np.arange(50) * 0.1
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Close": closes}, index=idx)
    from common.indicators import macd

    hist = macd(df["Close"])["hist"].reset_index(drop=True)
    merged = pd.DataFrame({"orig_pos": np.arange(len(closes))})
    earlier = pd.Series({"start_pos": 0, "end_pos": 5, "start_price": 100.0, "end_price": 100.5, "direction": "up"})
    later = pd.Series({"start_pos": 6, "end_pos": 12, "start_price": 100.6, "end_price": 101.2, "direction": "up"})
    has_div, area_earlier, area_later = macd_divergence(earlier, later, hist, merged)
    assert has_div is False
    assert np.isnan(area_earlier) or np.isnan(area_later)


# --- classify_points ---------------------------------------------------------
# `segments`/`pivots`/`hist`/`merged` are hand-built directly (bypassing the
# raw-OHLC pipeline), matching this project's existing style of testing
# `build_pivots` on hand-built strokes rather than only end to end. `merged`
# is 1:1 with original bars (orig_pos = identity) so `hist` indices line up
# directly with segment `pos` values.

def _build_buy_fixture():
    rows = [
        _seg_row(0, 10, 83, 70, "down"),      # 0: E (kept out of pivot0's own seed window)
        _seg_row(10, 20, 70, 92, "up"),       # 1: pivot0 window start
        _seg_row(20, 30, 92, 84, "down"),     # 2
        _seg_row(30, 40, 84, 90, "up"),       # 3: pivot0 window end
        _seg_row(40, 50, 90, 60, "down"),     # 4: L (first_buy leaving move, breaks below zd, further than E)
        _seg_row(50, 60, 60, 83, "up"),       # 5: M (bounce)
        _seg_row(60, 70, 83, 65, "down"),     # 6: N (does not make a new low vs L=60) -> second_buy
        _seg_row(70, 80, 65, 95, "up"),       # 7: entering move for pivot1 (kept out of its own window)
        _seg_row(80, 90, 95, 88, "down"),     # 8: pivot1 window start
        _seg_row(90, 100, 88, 96, "up"),      # 9
        _seg_row(100, 110, 96, 89, "down"),   # 10: pivot1 window end
        _seg_row(110, 120, 89, 105, "up"),    # 11: B breakout above pivot1's zg
        _seg_row(120, 130, 105, 98, "down"),  # 12: R retest holding above zg -> third_buy
    ]
    segments = pd.DataFrame(rows, columns=_SEGMENT_COLUMNS)
    from research_strategy.rs.chan_structure import build_pivots

    pivots = build_pivots(segments, min_strokes=3)
    n = 131
    merged = pd.DataFrame({"orig_pos": np.arange(n)})
    hist = np.zeros(n)
    hist[0:11] = -1.0   # E: strong down momentum
    hist[40:51] = -0.3  # L: weaker down momentum -> divergence
    return segments, pivots, hist, merged


def _build_sell_fixture():
    # Exact mirror of the buy fixture: prices negated around 200, directions flipped.
    base = 200
    buy_rows = [
        (0, 10, 83, 70, "down"), (10, 20, 70, 92, "up"), (20, 30, 92, 84, "down"), (30, 40, 84, 90, "up"),
        (40, 50, 90, 60, "down"), (50, 60, 60, 83, "up"), (60, 70, 83, 65, "down"), (70, 80, 65, 95, "up"),
        (80, 90, 95, 88, "down"), (90, 100, 88, 96, "up"), (100, 110, 96, 89, "down"), (110, 120, 89, 105, "up"),
        (120, 130, 105, 98, "down"),
    ]
    flip = {"up": "down", "down": "up"}
    rows = [_seg_row(sp, ep, base - spr, base - epr, flip[d]) for sp, ep, spr, epr, d in buy_rows]
    segments = pd.DataFrame(rows, columns=_SEGMENT_COLUMNS)
    from research_strategy.rs.chan_structure import build_pivots

    pivots = build_pivots(segments, min_strokes=3)
    n = 131
    merged = pd.DataFrame({"orig_pos": np.arange(n)})
    hist = np.zeros(n)
    hist[0:11] = 1.0
    hist[40:51] = 0.3
    return segments, pivots, hist, merged


def test_classify_points_detects_first_second_and_third_buy():
    segments, pivots, hist, merged = _build_buy_fixture()
    points = classify_points(segments, pivots, hist, merged)
    kinds = dict(zip(points["kind"], points["pivot_idx"]))
    assert kinds["first_buy"] == 0
    assert kinds["second_buy"] == 0
    assert kinds["third_buy"] == 1
    assert points[points["kind"] == "first_buy"]["price"].iloc[0] == 65.0
    assert points[points["kind"] == "second_buy"]["price"].iloc[0] == 88.0
    assert points[points["kind"] == "third_buy"]["price"].iloc[0] == 89.0


def test_classify_points_detects_first_second_and_third_sell():
    segments, pivots, hist, merged = _build_sell_fixture()
    points = classify_points(segments, pivots, hist, merged)
    kinds = dict(zip(points["kind"], points["pivot_idx"]))
    assert kinds["first_sell"] == 0
    assert kinds["second_sell"] == 0
    assert kinds["third_sell"] == 1


def test_classify_points_same_polarity_first_and_third_never_share_a_pivot():
    for builder in (_build_buy_fixture, _build_sell_fixture):
        segments, pivots, hist, merged = builder()
        points = classify_points(segments, pivots, hist, merged)
        firsts = points[points["kind"].isin(["first_buy", "first_sell"])]
        thirds = points[points["kind"].isin(["third_buy", "third_sell"])]
        for _, f in firsts.iterrows():
            same_polarity = "buy" if f["kind"] == "first_buy" else "sell"
            later_thirds = thirds[(thirds["pos"] > f["pos"]) & (thirds["kind"].str.endswith(same_polarity))]
            assert (later_thirds["pivot_idx"] > f["pivot_idx"]).all()


def test_classify_points_no_first_type_points_on_flat_pivot_no_divergence():
    segments, pivots, _, merged = _build_buy_fixture()
    n = 131
    hist = np.zeros(n)  # zero everywhere -> no leg ever has smaller area than another (all equal, not strictly smaller)
    points = classify_points(segments, pivots, hist, merged)
    assert not points["kind"].isin(["first_buy", "first_sell"]).any()


def test_classify_points_returns_empty_on_no_pivots():
    empty_pivots = pd.DataFrame(columns=["start_pos", "end_pos", "zg", "zd", "gg", "dd", "start_stroke_idx", "end_stroke_idx"])
    segments, _, hist, merged = _build_buy_fixture()
    result = classify_points(segments, empty_pivots, hist, merged)
    assert result.empty
    assert list(result.columns) == ["pos", "price", "kind", "pivot_idx"]


# --- compute_chan3_signals ---------------------------------------------------

def test_compute_chan3_signals_returns_expected_schema():
    closes = np.full(100, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_chan3_signals(df)
    expected_cols = {"first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell", "buy_signal", "sell_signal"}
    assert set(sig.columns) == expected_cols
    assert sig.index.equals(df.index)
    assert sig.dtypes.apply(lambda d: d == bool).all()


def test_compute_chan3_signals_no_signals_on_a_flat_series():
    closes = np.full(150, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_chan3_signals(df)
    assert not sig["buy_signal"].any()
    assert not sig["sell_signal"].any()


def test_compute_chan3_signals_buy_and_sell_are_or_of_the_three_types():
    from research_strategy.tests.test_strategy import _chan3_breakout_closes

    closes = _chan3_breakout_closes()
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_chan3_signals(df)
    assert (sig["buy_signal"] == (sig["first_buy"] | sig["second_buy"] | sig["third_buy"])).all()
    assert (sig["sell_signal"] == (sig["first_sell"] | sig["second_sell"] | sig["third_sell"])).all()


# --- compute_chan_pivot_macd_signals -----------------------------------------
# A near-literal copy of compute_chan_signals's own pivot-band-shift buy/sell
# rules, with only the disclosed stroke-slope "momentum divergence proxy"
# replaced by real MACD-histogram-area divergence (symmetric: a top-
# divergence sell AND a bottom-divergence buy). Divergence fixtures below
# were derived by actually running the implemented function end to end
# (merge_inclusion -> find_fractals -> build_strokes -> macd_divergence),
# iterating on leg lengths until the intended structure/divergence
# relationship appeared -- not hand-derived up front.

def test_compute_chan_pivot_macd_signals_reproduces_pivot_shift_buy_rule_exactly():
    from research_strategy.rs.chan_structure import compute_chan_signals

    def leg(a, b, n):
        return np.linspace(a, b, n + 1)[1:]

    closes = np.concatenate(
        [
            [100.0],
            leg(100, 90, 10), leg(90, 100, 10), leg(100, 90, 10),
            leg(90, 112, 10), leg(112, 104, 10), leg(104, 114, 10), leg(114, 104, 10),
            leg(104, 108, 20),
        ]
    )
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    old = compute_chan_signals(df, min_gap_bars=4, min_strokes=3)
    new = compute_chan_pivot_macd_signals(df, min_gap_bars=4, min_strokes=3)
    # The pivot-shift buy rule (rule 1) is copied verbatim and untouched by
    # the divergence swap, so it must match compute_chan_signals's own
    # buy_signal exactly on this fixture (which has no downward pivot shift,
    # so rule 2/sell is not exercised by this comparison).
    assert (old["buy_signal"] == new["buy_signal"]).all()


def _chanm_top_divergence_closes():
    """96 flat warm-up bars, then a dip (seeds a bottom fractal), a steep
    up-leg, a partial pullback, a shallower second up-leg reaching a new
    high, then a tail (seeds the final top fractal). Produces exactly one
    pivot (too few strokes for a second one, so rules 1/2 never fire) and
    two up-strokes whose real MACD divergence -- verified by direct
    execution -- fires a sell at bar 144 of 148."""
    warmup = np.full(96, 100.0)
    dip = np.linspace(99, 97, 5)[1:]
    leg_a = np.linspace(97.6, 120, 20)
    pullback = np.linspace(119, 116, 5)[1:]
    leg_b = np.linspace(116.6, 126, 20)
    tail = np.linspace(125, 122, 5)[1:]
    return np.concatenate([warmup, dip, leg_a, pullback, leg_b, tail])


def test_compute_chan_pivot_macd_signals_detects_top_divergence_sell():
    closes = _chanm_top_divergence_closes()
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.2, "Low": closes - 0.2, "Close": closes}, index=idx)
    sig = compute_chan_pivot_macd_signals(df, min_gap_bars=4, min_strokes=3)
    assert np.flatnonzero(sig["sell_signal"].to_numpy()).tolist() == [144]
    assert not sig["buy_signal"].any()


def test_compute_chan_pivot_macd_signals_detects_bottom_divergence_buy():
    # Exact mirror of the top-divergence fixture: negate prices around a
    # baseline, flipping up-legs into down-legs.
    closes = 200.0 - _chanm_top_divergence_closes()
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.2, "Low": closes - 0.2, "Close": closes}, index=idx)
    sig = compute_chan_pivot_macd_signals(df, min_gap_bars=4, min_strokes=3)
    assert np.flatnonzero(sig["buy_signal"].to_numpy()).tolist() == [144]
    assert not sig["sell_signal"].any()


def test_compute_chan_pivot_macd_signals_returns_expected_schema():
    closes = np.full(100, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_chan_pivot_macd_signals(df)
    assert set(sig.columns) == {"buy_signal", "sell_signal"}
    assert sig.index.equals(df.index)
    assert not sig["buy_signal"].any()
    assert not sig["sell_signal"].any()


# --- memoization shared with chan_structure.py (review fix 7) ---------------

def test_compute_chan3_signals_cache_hit_returns_identical_object():
    closes = np.linspace(100, 90, 60)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    first = compute_chan3_signals(df, min_gap_bars=4, min_strokes=3)
    second = compute_chan3_signals(df, min_gap_bars=4, min_strokes=3)
    assert second is first


def test_compute_chan_pivot_macd_signals_cache_hit_returns_identical_object():
    closes = np.linspace(100, 90, 60)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)

    first = compute_chan_pivot_macd_signals(df, min_gap_bars=4, min_strokes=3)
    second = compute_chan_pivot_macd_signals(df, min_gap_bars=4, min_strokes=3)
    assert second is first


def test_compute_chan3_signals_cache_miss_on_different_data():
    closes_a = np.linspace(100, 90, 60)
    idx_a = pd.bdate_range("2020-01-01", periods=len(closes_a))
    df_a = pd.DataFrame({"Open": closes_a, "High": closes_a + 0.5, "Low": closes_a - 0.5, "Close": closes_a}, index=idx_a)

    closes_b = np.linspace(100, 90, 61)
    idx_b = pd.bdate_range("2020-01-01", periods=len(closes_b))
    df_b = pd.DataFrame({"Open": closes_b, "High": closes_b + 0.5, "Low": closes_b - 0.5, "Close": closes_b}, index=idx_b)

    result_a = compute_chan3_signals(df_a, min_gap_bars=4, min_strokes=3)
    result_b = compute_chan3_signals(df_b, min_gap_bars=4, min_strokes=3)
    assert result_a is not result_b
    assert len(result_a) == 60 and len(result_b) == 61

"""Unit tests for the independent Chan-theory structure detector
(research_strategy/rs/chan_structure.py).

All fixtures are small, hand-built, hand-verified DataFrames -- no network
access, no synthetic-data generators needed at this level. This module
shares no code with, and was not validated against, the third-party `czsc`
library; it is this project's own from-scratch reading of 缠中说禅.
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

from research_strategy.rs.chan_structure import (
    build_pivots,
    build_strokes,
    compute_chan_signals,
    find_fractals,
    merge_inclusion,
)


def _ohlc(highs, lows):
    idx = pd.bdate_range("2020-01-01", periods=len(highs))
    return pd.DataFrame({"High": highs, "Low": lows}, index=idx)


# --- merge_inclusion -------------------------------------------------------

def test_merge_inclusion_leaves_a_non_including_series_untouched():
    df = _ohlc(highs=[10, 12, 9, 13], lows=[8, 9, 6, 10])
    merged = merge_inclusion(df)
    assert len(merged) == 4
    np.testing.assert_allclose(merged["high"].to_numpy(), [10, 12, 9, 13])
    np.testing.assert_allclose(merged["low"].to_numpy(), [8, 9, 6, 10])
    assert list(merged["orig_pos"]) == [0, 1, 2, 3]


def test_merge_inclusion_collapses_an_included_bar_upward():
    # Bar 1 (9, 8.5) is fully contained within bar 0 (10, 8) -> merged
    # up-biased (no established direction yet) into (max(10,9), max(8,8.5)).
    # Bar 2 (12, 9) breaks out above the merged bar -> starts a new one.
    df = _ohlc(highs=[10, 9, 12], lows=[8, 8.5, 9])
    merged = merge_inclusion(df)
    assert len(merged) == 2
    assert merged.iloc[0]["high"] == 10
    assert merged.iloc[0]["low"] == 8.5
    assert merged.index[0] == df.index[1]  # last original bar folded into it
    assert merged.iloc[1]["high"] == 12
    assert merged.iloc[1]["low"] == 9
    assert merged.iloc[1]["orig_pos"] == 2


# --- find_fractals ----------------------------------------------------------

def test_find_fractals_detects_alternating_top_bottom_top():
    merged = pd.DataFrame(
        {"high": [10, 14, 9, 13, 7], "low": [8, 11, 6, 10, 4]},
        index=pd.bdate_range("2020-01-01", periods=5),
    )
    fractals = find_fractals(merged)
    assert list(fractals["pos"]) == [1, 2, 3]
    assert list(fractals["kind"]) == ["top", "bottom", "top"]
    np.testing.assert_allclose(fractals["price"].to_numpy(), [14, 6, 13])


def test_find_fractals_needs_at_least_three_bars():
    merged = pd.DataFrame({"high": [10, 14], "low": [8, 11]}, index=pd.bdate_range("2020-01-01", periods=2))
    assert find_fractals(merged).empty


# --- build_strokes -----------------------------------------------------------

def _fractal_row(pos, kind, price):
    return {"pos": pos, "kind": kind, "price": price}


def test_build_strokes_keeps_the_more_extreme_same_kind_fractal():
    fractals = pd.DataFrame(
        [
            _fractal_row(0, "bottom", 5.0),
            _fractal_row(2, "top", 20.0),
            _fractal_row(4, "top", 25.0),  # more extreme top -> replaces the one above
            _fractal_row(6, "bottom", 3.0),
        ]
    )
    strokes = build_strokes(fractals, min_gap_bars=1)
    assert len(strokes) == 2
    assert strokes.iloc[0].to_dict() == {
        "start_pos": 0, "end_pos": 4, "start_price": 5.0, "end_price": 25.0, "direction": "up", "bars": 4,
    }
    assert strokes.iloc[1].to_dict() == {
        "start_pos": 4, "end_pos": 6, "start_price": 25.0, "end_price": 3.0, "direction": "down", "bars": 2,
    }


def test_build_strokes_drops_a_fractal_too_close_to_be_independent():
    fractals = pd.DataFrame(
        [
            _fractal_row(0, "top", 14.0),
            _fractal_row(1, "bottom", 6.0),   # only 1 bar away -> not independent
            _fractal_row(3, "top", 13.0),     # weaker than pos 0's top -> doesn't replace it
        ]
    )
    strokes = build_strokes(fractals, min_gap_bars=5)
    assert strokes.empty


# --- build_pivots -------------------------------------------------------------

def _stroke_row(start_pos, end_pos, start_price, end_price, direction):
    return {
        "start_pos": start_pos, "end_pos": end_pos, "start_price": start_price,
        "end_price": end_price, "direction": direction, "bars": end_pos - start_pos,
    }


def test_build_pivots_forms_and_extends_then_closes_on_breakout():
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 90, "down"),
            _stroke_row(10, 20, 90, 98, "up"),
            _stroke_row(20, 30, 98, 92, "down"),
            _stroke_row(30, 40, 92, 99, "up"),     # still overlaps [92, 98] (low=92 < zg) -> extends the pivot
            _stroke_row(40, 50, 99, 110, "up"),    # low=99 >= zg=98: clears the band -> pivot closes before this
        ]
    )
    pivots = build_pivots(strokes, min_strokes=3)
    assert len(pivots) == 1
    p = pivots.iloc[0]
    assert p["zg"] == 98 and p["zd"] == 92
    assert p["gg"] == 100 and p["dd"] == 90
    assert p["start_stroke_idx"] == 0 and p["end_stroke_idx"] == 3
    assert p["start_pos"] == 0 and p["end_pos"] == 40


def test_build_pivots_requires_a_genuine_price_overlap():
    # Each stroke's range is entirely above the previous one's -- a clean
    # uptrend, never a 3-stroke overlap -- so no pivot should ever form.
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 110, "up"),
            _stroke_row(10, 20, 110, 120, "up"),  # not alternating in reality, but build_pivots
            _stroke_row(20, 30, 120, 130, "up"),  # only looks at ranges, so this is a valid probe
        ]
    )
    assert build_pivots(strokes, min_strokes=3).empty


def test_build_pivots_below_min_strokes_returns_empty():
    strokes = pd.DataFrame(
        [
            _stroke_row(0, 10, 100, 90, "down"),
            _stroke_row(10, 20, 90, 98, "up"),
        ]
    )
    assert build_pivots(strokes, min_strokes=3).empty


# --- compute_chan_signals -----------------------------------------------------

def test_compute_chan_signals_buys_only_after_the_pivot_confirms_a_shift_up():
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

    sig = compute_chan_signals(df, min_gap_bars=4, min_strokes=3)
    assert not sig["buy_signal"].iloc[:65].any(), "no buy before the 2nd pivot's window can even close"
    assert sig["buy_signal"].iloc[65:].any(), "buy once the pivot shifts to the higher range"


def test_compute_chan_signals_no_signals_on_a_flat_series():
    closes = np.full(100, 100.0)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    df = pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5, "Close": closes}, index=idx)
    sig = compute_chan_signals(df)
    assert not sig["buy_signal"].any()
    assert not sig["sell_signal"].any()

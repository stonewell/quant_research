import pandas as pd

from pmine.turning_points import find_turning_points


def _series(values):
    idx = pd.bdate_range("2020-01-01", periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def test_finds_known_peak_and_trough_on_deterministic_up_down_up_path():
    # Known peak at 130 (index 3), known trough at 100 (index 6). The final
    # rally to 140 is a still-forming, unconfirmed swing and must NOT appear.
    closes = _series([100, 110, 120, 130, 120, 110, 100, 110, 120, 130, 140])
    tp = find_turning_points(closes, min_swing_pct=0.05)

    assert len(tp) == 2
    assert tp.iloc[0]["type"] == "peak"
    assert tp.iloc[0]["price"] == 130
    assert tp.iloc[1]["type"] == "trough"
    assert tp.iloc[1]["price"] == 100


def test_small_wiggle_below_threshold_is_ignored():
    # A 2% dip in the middle of a rally should NOT register as a peak/trough
    # when min_swing_pct=0.05 -- only genuine, larger swings should.
    closes = _series([100, 110, 120, 130, 128, 132, 140])  # 130->128 is a ~1.5% dip
    tp = find_turning_points(closes, min_swing_pct=0.05)
    assert tp.empty


def test_end_of_series_unconfirmed_pivot_is_excluded():
    # A rally that's still ongoing at the end of the series must not be
    # reported as a confirmed peak -- it could still extend or reverse.
    closes = _series([100, 90, 80, 90, 100, 110, 120])  # trough at 80, then still-rising
    tp = find_turning_points(closes, min_swing_pct=0.05)
    assert len(tp) == 1
    assert tp.iloc[0]["type"] == "trough"
    assert tp.iloc[0]["price"] == 80


def test_flat_series_has_no_turning_points():
    closes = _series([100.0] * 20)
    tp = find_turning_points(closes, min_swing_pct=0.05)
    assert tp.empty
    assert list(tp.columns) == ["type", "price"]


def test_too_short_series_returns_empty():
    tp = find_turning_points(_series([100.0]), min_swing_pct=0.05)
    assert tp.empty
    tp_empty = find_turning_points(pd.Series([], dtype=float), min_swing_pct=0.05)
    assert tp_empty.empty

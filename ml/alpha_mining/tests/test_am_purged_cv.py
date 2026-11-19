"""Unit tests for amine/purged_cv.py Purged Group TimeSeries CV + Embargo.

100% offline: uses synthetic date indices only.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_AM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AM_ROOT not in sys.path:
    sys.path.insert(0, _AM_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from amine.purged_cv import (
    PurgedGroupTimeSeriesCV,
    assert_no_leakage,
    split_timeline_three_way,
)


def test_pre_test_purge_boundary_exactness():
    dates = pd.bdate_range("2020-01-01", periods=150)
    h, e = 6, 4
    cv = PurgedGroupTimeSeriesCV(
        n_splits=4, h=h, embargo=e, train_side="past", min_train_dates=20, min_test_dates=10
    )
    folds = list(cv.split_folds(dates))
    assert len(folds) == 4

    for fold in folds:
        a = fold.test_start_pos
        tr_set = set(fold.train_indices.tolist())
        # Pre-test purge [a - h, a - 1] strictly absent
        for p in range(a - h, a):
            assert p not in tr_set
        # Immediate boundary a - h - 1 strictly present
        assert (a - h - 1) in tr_set
        # No future training dates in "past" mode
        assert fold.train_indices.max() < a
        assert_no_leakage(fold.train_dates, fold.test_dates, h=h, embargo=e, all_dates=dates)


@pytest.mark.parametrize("embargo", [0, 5])
def test_train_side_both_post_test_boundary_exactness(embargo: int):
    dates = pd.bdate_range("2020-01-01", periods=160)
    h = 6
    cv = PurgedGroupTimeSeriesCV(
        n_splits=4, h=h, embargo=embargo, train_side="both", min_train_dates=15, min_test_dates=10
    )
    folds = list(cv.split_folds(dates))
    assert len(folds) == 4

    for fold in folds:
        a, b = fold.test_start_pos, fold.test_end_pos
        tr_set = set(fold.train_indices.tolist())

        # Pre-test purge [a - h, a - 1] absent; a - h - 1 present when >= 0
        for p in range(max(0, a - h), a):
            assert p not in tr_set
        if a - h - 1 >= 0:
            assert (a - h - 1) in tr_set

        # Post-test purge + embargo [b + 1, b + h + e] absent; b + h + e + 1 present when < T
        for p in range(b + 1, min(len(dates), b + h + embargo + 1)):
            assert p not in tr_set
        if b + h + embargo + 1 < len(dates):
            assert (b + h + embargo + 1) in tr_set

        assert_no_leakage(fold.train_dates, fold.test_dates, h=h, embargo=embargo, all_dates=dates)


def test_assert_no_leakage_catches_overlap_and_purge_violations():
    dates = pd.bdate_range("2021-01-01", periods=80)
    test_dates = dates[30:45]

    # Clean split passes
    clean_train = dates[:24].union(dates[56:])  # a=30, h=6 -> <=23; b=44, h=6, e=5 -> >=56
    assert_no_leakage(clean_train, test_dates, h=6, embargo=5, all_dates=dates)

    # Direct overlap fails
    with pytest.raises(AssertionError, match="Leakage detected"):
        assert_no_leakage(dates[:31], test_dates, h=6, embargo=5, all_dates=dates)

    # Pre-test purge violation (pos 24 == a - h) fails
    with pytest.raises(AssertionError, match="Pre-test purge violation"):
        assert_no_leakage(dates[:25], test_dates, h=6, embargo=5, all_dates=dates)

    # Post-test embargo violation (pos 55 == b + h + e) fails
    with pytest.raises(AssertionError, match="Post-test purge/embargo violation"):
        assert_no_leakage(dates[55:], test_dates, h=6, embargo=5, all_dates=dates)


def test_three_way_timeline_split_and_invalid_args():
    dates = pd.bdate_range("2020-01-01", periods=200)
    sp = split_timeline_three_way(dates, h=6, embargo=5)
    assert_no_leakage(sp.discovery_dates, sp.combiner_dates, h=6, embargo=5, all_dates=dates)
    assert_no_leakage(sp.combiner_dates, sp.holdout_dates, h=6, embargo=5, all_dates=dates)

    # Invalid arguments raise ValueError
    with pytest.raises(ValueError):
        PurgedGroupTimeSeriesCV(n_splits=1)
    with pytest.raises(ValueError):
        PurgedGroupTimeSeriesCV(h=0)
    with pytest.raises(ValueError):
        PurgedGroupTimeSeriesCV(embargo=-1)
    with pytest.raises(ValueError):
        PurgedGroupTimeSeriesCV(train_side="future")
    with pytest.raises(ValueError):
        PurgedGroupTimeSeriesCV(n_splits=5, h=6, embargo=5, min_train_dates=20, min_test_dates=10).split_folds(dates[:30]).__next__()
    with pytest.raises(ValueError):
        assert_no_leakage(dates[:10], dates[20:30], h=0, embargo=0, all_dates=dates)
    with pytest.raises(ValueError):
        assert_no_leakage(dates[:10], dates[20:30], h=5, embargo=-1, all_dates=dates)

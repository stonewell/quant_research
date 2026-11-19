"""Purged Group TimeSeries Cross-Validation + Embargo (pure numpy/pandas).

Academic Grounding:
Lopez de Prado, Marcos (2018). *Advances in Financial Machine Learning*,
Chapter 7: Cross-Validation in Finance (Purging and Embargoing).

Exact Closed-Interval Symmetry Contract:
- Every observation at integer trading-day position ``i`` has a forward label
  spanning the closed interval ``[i, i + h]``, where ``h = holding_period + exec_lag``.
- A contiguous test block ``[a, b]`` spans the closed interval ``[a, b + h]``.
- **Pre-test purge**: drop every training index ``i`` whose label window
  overlaps ``[a, b + h]``, i.e. ``i in [a - h, a - 1]``. The maximum allowed
  pre-test training index is ``a - h - 1``.
- **Post-test purge + embargo** (active only when ``train_side="both"``):
  drop every training index ``i in [b + 1, b + h + e]``. The minimum allowed
  post-test training index is ``b + h + e + 1``, which is strictly disjoint
  from ``b + h`` even when ``e = 0``.
- Default ``train_side="past"`` (expanding walk-forward) trains strictly before
  each test block, guaranteeing temporal causality by construction.

Dependency Policy:
- Strictly pure ``numpy`` and ``pandas`` (zero ML-library imports).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

_LOGGER = logging.getLogger(__name__)

VALID_TRAIN_SIDES = ("past", "both")


@dataclass(frozen=True)
class PurgedFold:
    """Single fold emitted by ``PurgedGroupTimeSeriesCV``."""

    fold_id: int
    train_indices: np.ndarray
    test_indices: np.ndarray
    train_dates: pd.Index
    test_dates: pd.Index
    test_start_pos: int
    test_end_pos: int


@dataclass(frozen=True)
class TimelineSplit:
    """Three-way temporally separated split for GP discovery, combiner, and holdout.

    Layout::

        [GP discovery] -> gap(h + e) -> [Combiner] -> gap(h + e) -> [Holdout]

    Each gap drops ``h + e`` trading dates so that the last sample of the
    preceding segment and the first sample of the following segment satisfy
    ``assert_no_leakage(..., h=h, embargo=e)``.
    """

    discovery_dates: pd.Index
    combiner_dates: pd.Index
    holdout_dates: pd.Index
    gap_size: int
    discovery_slice: Tuple[int, int]
    combiner_slice: Tuple[int, int]
    holdout_slice: Tuple[int, int]


def _normalize_unique_dates(dates: Union[pd.Index, pd.Series, Sequence]) -> pd.Index:
    """Return sorted unique date index."""
    idx = pd.Index(dates)
    if idx.isna().any():
        idx = idx.dropna()
    return pd.Index(np.sort(idx.unique()))


def _contiguous_blocks(sorted_pos: np.ndarray) -> List[Tuple[int, int]]:
    """Split a sorted 1D integer array into contiguous closed intervals ``[a, b]``."""
    if len(sorted_pos) == 0:
        return []
    breaks = np.where(np.diff(sorted_pos) > 1)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [len(sorted_pos) - 1]))
    return [(int(sorted_pos[s]), int(sorted_pos[e])) for s, e in zip(starts, ends)]


def assert_no_leakage(
    train_dates: Union[pd.Index, pd.Series, Sequence],
    test_dates: Union[pd.Index, pd.Series, Sequence],
    h: int,
    embargo: int = 5,
    *,
    all_dates: Optional[Union[pd.Index, pd.Series, Sequence]] = None,
) -> None:
    """Assert strict closed-interval disjointness and embargo between train and test.

    Checks that:
    1. ``train_dates`` and ``test_dates`` have empty intersection.
    2. For every contiguous test block ``[a, b]`` in ``all_dates``:
       - No training index lies in ``[a - h, a - 1]`` (pre-test purge).
       - No training index lies in ``[b + 1, b + h + embargo]`` (post-test
         purge + embargo).

    Raises
    ------
    ValueError
        If ``h < 1`` or ``embargo < 0``.
    AssertionError
        If any overlap, purge violation, or embargo violation is detected.
    """
    h_int = int(h)
    e_int = int(embargo)
    if h_int < 1:
        raise ValueError(f"h must be >= 1, got {h}")
    if e_int < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")

    tr = _normalize_unique_dates(train_dates)
    te = _normalize_unique_dates(test_dates)

    if len(tr) == 0 or len(te) == 0:
        return

    overlap = tr.intersection(te)
    if len(overlap) > 0:
        raise AssertionError(
            f"Leakage detected: {len(overlap)} dates appear in both train and test "
            f"(first: {overlap[0]})."
        )

    if all_dates is None:
        universe = _normalize_unique_dates(tr.union(te))
    else:
        universe = _normalize_unique_dates(all_dates)

    tr_pos = universe.get_indexer(tr)
    te_pos = universe.get_indexer(te)
    if (tr_pos < 0).any() or (te_pos < 0).any():
        raise ValueError("train_dates and test_dates must be subsets of all_dates.")

    tr_pos = np.sort(tr_pos)
    te_pos = np.sort(te_pos)

    for a, b in _contiguous_blocks(te_pos):
        pre_lo = a - h_int
        pre_hi = a - 1
        pre_bad = tr_pos[(tr_pos >= pre_lo) & (tr_pos <= pre_hi)]
        if len(pre_bad) > 0:
            raise AssertionError(
                f"Pre-test purge violation for test block [{a}, {b}] with h={h_int}: "
                f"train positions {pre_bad.tolist()} fall inside [{pre_lo}, {pre_hi}]."
            )

        post_lo = b + 1
        post_hi = b + h_int + e_int
        post_bad = tr_pos[(tr_pos >= post_lo) & (tr_pos <= post_hi)]
        if len(post_bad) > 0:
            raise AssertionError(
                f"Post-test purge/embargo violation for test block [{a}, {b}] with "
                f"h={h_int}, embargo={e_int}: train positions {post_bad.tolist()} "
                f"fall inside [{post_lo}, {post_hi}]."
            )


class PurgedGroupTimeSeriesCV:
    """Purged Group TimeSeries Cross-Validator with exact closed-interval symmetry.

    Parameters
    ----------
    n_splits : int, default 5
        Number of contiguous test blocks.
    h : int, default 6
        Forward label horizon ``k + exec_lag`` (must be >= 1).
    embargo : int, default 5
        Post-test embargo length in trading days (must be >= 0).
    train_side : str, default "past"
        ``"past"`` for strictly causal expanding walk-forward CV, or ``"both"``
        for research K-fold purged CV using both pre- and post-test data.
    min_train_dates : int, default 20
        Minimum number of training dates required to form a valid fold.
    min_test_dates : int, default 10
        Minimum number of dates per test block.
    """

    def __init__(
        self,
        n_splits: int = 5,
        h: int = 6,
        embargo: int = 5,
        train_side: str = "past",
        min_train_dates: int = 20,
        min_test_dates: int = 10,
    ) -> None:
        if int(n_splits) < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        if int(h) < 1:
            raise ValueError(f"h must be >= 1, got {h}")
        if int(embargo) < 0:
            raise ValueError(f"embargo must be >= 0, got {embargo}")
        if train_side not in VALID_TRAIN_SIDES:
            raise ValueError(
                f"train_side must be one of {VALID_TRAIN_SIDES}, got {train_side!r}"
            )
        if int(min_train_dates) < 1:
            raise ValueError(f"min_train_dates must be >= 1, got {min_train_dates}")
        if int(min_test_dates) < 1:
            raise ValueError(f"min_test_dates must be >= 1, got {min_test_dates}")

        self.n_splits = int(n_splits)
        self.h = int(h)
        self.embargo = int(embargo)
        self.train_side = train_side
        self.min_train_dates = int(min_train_dates)
        self.min_test_dates = int(min_test_dates)

    def _compute_test_blocks(self, n_dates: int) -> List[Tuple[int, int]]:
        """Partition the test region into ``n_splits`` contiguous ``[a, b]`` blocks."""
        min_required = (
            self.min_train_dates + self.n_splits * self.min_test_dates + self.h + self.embargo
        )
        if n_dates < min_required:
            raise ValueError(
                f"Insufficient unique dates T={n_dates} for PurgedGroupTimeSeriesCV "
                f"(need >= {min_required}: min_train={self.min_train_dates}, "
                f"n_splits={self.n_splits}, min_test={self.min_test_dates}, "
                f"h={self.h}, embargo={self.embargo})."
            )

        if self.train_side == "past":
            # Reserve [0, min_train_dates - 1] for fold 0's training plus h purge gap.
            test_region_start = self.min_train_dates + self.h
            test_positions = np.arange(test_region_start, n_dates, dtype=int)
        else:
            test_positions = np.arange(0, n_dates, dtype=int)

        raw_blocks = np.array_split(test_positions, self.n_splits)
        blocks: List[Tuple[int, int]] = []
        for blk in raw_blocks:
            if len(blk) > 0:
                blocks.append((int(blk[0]), int(blk[-1])))
        return blocks

    def split_folds(
        self,
        dates: Union[pd.Index, pd.Series, Sequence],
    ) -> Iterator[PurgedFold]:
        """Yield ``PurgedFold`` objects over sorted unique ``dates``."""
        unique_dates = _normalize_unique_dates(dates)
        n_dates = len(unique_dates)
        blocks = self._compute_test_blocks(n_dates)

        fold_id = 0
        for a, b in blocks:
            test_idx = np.arange(a, b + 1, dtype=int)

            # Pre-test: i <= a - h - 1
            pre_end = a - self.h
            pre_idx = np.arange(0, max(0, pre_end), dtype=int)

            if self.train_side == "past":
                train_idx = pre_idx
            else:
                # Post-test: i >= b + h + embargo + 1
                post_start = b + self.h + self.embargo + 1
                post_idx = (
                    np.arange(post_start, n_dates, dtype=int)
                    if post_start < n_dates
                    else np.empty(0, dtype=int)
                )
                train_idx = np.concatenate((pre_idx, post_idx))

            if len(train_idx) == 0:
                _LOGGER.warning(
                    "Skipping fold for test block [%d, %d]: empty train set after "
                    "purging (h=%d, embargo=%d, train_side=%s).",
                    a,
                    b,
                    self.h,
                    self.embargo,
                    self.train_side,
                )
                continue

            yield PurgedFold(
                fold_id=fold_id,
                train_indices=train_idx,
                test_indices=test_idx,
                train_dates=unique_dates[train_idx],
                test_dates=unique_dates[test_idx],
                test_start_pos=a,
                test_end_pos=b,
            )
            fold_id += 1

    def split(
        self,
        dates: Union[pd.Index, pd.Series, Sequence],
    ) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        """Yield ``(train_indices, test_indices)`` positions into sorted unique dates."""
        for fold in self.split_folds(dates):
            yield fold.train_indices, fold.test_indices

    def split_panel_rows(
        self,
        row_dates: Union[pd.Series, pd.Index, Sequence],
    ) -> Iterator[Tuple[np.ndarray, np.ndarray, PurgedFold]]:
        """Yield ``(train_row_idx, test_row_idx, fold)`` for long-format panels."""
        date_arr = pd.Index(row_dates)
        for fold in self.split_folds(date_arr):
            train_mask = date_arr.isin(fold.train_dates)
            test_mask = date_arr.isin(fold.test_dates)
            yield (
                np.flatnonzero(train_mask),
                np.flatnonzero(test_mask),
                fold,
            )


def split_timeline_three_way(
    dates: Union[pd.Index, pd.Series, Sequence],
    h: int,
    embargo: int = 5,
    *,
    discovery_ratio: float = 0.45,
    combiner_ratio: float = 0.35,
    holdout_ratio: float = 0.20,
    min_segment_dates: int = 10,
) -> TimelineSplit:
    """Split sorted unique dates into 3 non-overlapping segments separated by ``h + embargo`` gaps.

    Layout::

        [GP discovery] -> gap(h + embargo) -> [Combiner] -> gap(h + embargo) -> [Holdout]

    Dropping ``gap = h + embargo`` dates between adjacent segments ensures that
    if segment 1 ends at index ``d_end``, segment 2 starts at
    ``d_end + h + embargo + 1``, which satisfies ``assert_no_leakage`` with
    exact closed-interval symmetry.
    """
    h_int = int(h)
    e_int = int(embargo)
    if h_int < 1:
        raise ValueError(f"h must be >= 1, got {h}")
    if e_int < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")
    if min_segment_dates < 1:
        raise ValueError(f"min_segment_dates must be >= 1, got {min_segment_dates}")

    ratios = (discovery_ratio, combiner_ratio, holdout_ratio)
    if any(r <= 0.0 for r in ratios) or abs(sum(ratios) - 1.0) > 1e-6:
        raise ValueError(
            f"Segment ratios must be positive and sum to 1.0, got {ratios}."
        )

    unique_dates = _normalize_unique_dates(dates)
    total = len(unique_dates)
    gap = h_int + e_int
    usable = total - 2 * gap
    if usable < 3 * min_segment_dates:
        raise ValueError(
            f"Insufficient dates T={total} for 3-way split with 2 gaps of {gap} "
            f"(need at least {2 * gap + 3 * min_segment_dates} dates)."
        )

    n_disc = max(min_segment_dates, int(round(usable * discovery_ratio)))
    n_comb = max(min_segment_dates, int(round(usable * combiner_ratio)))
    n_hold = usable - n_disc - n_comb
    if n_hold < min_segment_dates:
        deficit = min_segment_dates - n_hold
        if n_disc - deficit >= min_segment_dates:
            n_disc -= deficit
        elif n_comb - deficit >= min_segment_dates:
            n_comb -= deficit
        else:
            raise ValueError(
                f"Cannot allocate >= {min_segment_dates} dates to holdout from T={total}."
            )
        n_hold = usable - n_disc - n_comb

    d_start, d_end = 0, n_disc
    c_start, c_end = d_end + gap, d_end + gap + n_comb
    h_start, h_end = c_end + gap, c_end + gap + n_hold

    return TimelineSplit(
        discovery_dates=unique_dates[d_start:d_end],
        combiner_dates=unique_dates[c_start:c_end],
        holdout_dates=unique_dates[h_start:h_end],
        gap_size=gap,
        discovery_slice=(d_start, d_end),
        combiner_slice=(c_start, c_end),
        holdout_slice=(h_start, h_end),
    )

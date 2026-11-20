"""Reusable offline look-ahead and intraday cutoff invariance harness (Track MS, MS12).

This module provides two lightweight, ``pytest``-free verification helpers that
detect future-data leakage in both daily/panel transforms and intraday 5-minute
feature pipelines:

1. ``assert_future_append_invariant``: verifies that appending future rows after
   a historical cut point does not alter any historical output value or index.
2. ``assert_cutoff_truncation_equivalent``: verifies that an intraday function
   honoring ``cutoff_time`` does not read post-cutoff bars, supporting both
   physical truncation (``method="truncate"``) and post-cutoff value perturbation
   (``method="perturb"``).

Why ``method="perturb"`` Is Required for Gated Intraday Functions
-----------------------------------------------------------------
In ``common/intraday_features.py``, ``compute_daily_intraday_features`` computes
``full_day_counts`` on the **uncut** 5-minute DataFrame (line 540) and skips any
session with fewer than 45 bars (lines 579-593, emitting
``IntradayIncompleteWarning``). If a test physically truncates a 48-bar session
at ``14:15`` (39 bars), the ``< 45`` completeness gate skips every day on the
truncated input while keeping every day on the full input, producing a guaranteed
false failure unrelated to look-ahead leakage. ``method="perturb"`` preserves
every timestamp (so the bar-count gate is untouched) while multiplying all
numeric columns of post-cutoff rows by seeded random factors in ``perturb_range``;
any function that reads post-cutoff prices or volumes will diverge immediately.
Use ``method="truncate"`` for pure window functions without a full-day bar-count
gate, and ``method="perturb"`` for gated functions like
``compute_daily_intraday_features``.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import Any, Literal, Union

import numpy as np
import pandas as pd

from common.intraday_features import _parse_time_cutoff

__all__ = [
    "LookaheadViolation",
    "assert_future_append_invariant",
    "assert_cutoff_truncation_equivalent",
]


class LookaheadViolation(AssertionError):
    """Raised when a transform violates future-append or cutoff causality."""


def _assert_same(
    left: Any,
    right: Any,
    *,
    context: str,
    rtol: float = 0.0,
    atol: float = 0.0,
    check_dtype: bool = True,
) -> None:
    """Compare two Series or DataFrames, raising LookaheadViolation on any mismatch."""
    if not isinstance(left, (pd.Series, pd.DataFrame)) or not isinstance(
        right, (pd.Series, pd.DataFrame)
    ):
        raise TypeError(
            f"{context}: expected pd.Series or pd.DataFrame outputs, "
            f"got {type(left).__name__} and {type(right).__name__}"
        )
    if type(left) is not type(right):
        raise LookaheadViolation(
            f"{context}: output type changed ({type(left).__name__} vs {type(right).__name__})"
        )

    check_exact = (rtol == 0.0) and (atol == 0.0)
    kwargs: dict[str, Any] = {
        "check_freq": False,
        "check_dtype": check_dtype,
        "check_exact": check_exact,
    }
    if not check_exact:
        kwargs["rtol"] = rtol
        kwargs["atol"] = atol

    try:
        if isinstance(left, pd.DataFrame):
            pd.testing.assert_frame_equal(left, right, **kwargs)
        else:
            pd.testing.assert_series_equal(left, right, **kwargs)
    except AssertionError as exc:
        raise LookaheadViolation(f"{context}: {exc}") from exc


def _resolve_cut_positions(
    index: pd.Index,
    cut_points: Union[int, np.integer, Any, Sequence[Any]],
) -> list[tuple[Any, int]]:
    """Resolve one or more cut points (integer positions or labels) into (raw, pos)."""
    if isinstance(cut_points, (str, bytes, pd.Timestamp, datetime.date, datetime.datetime)):
        raw_cuts: list[Any] = [cut_points]
    elif isinstance(cut_points, (int, np.integer)) and not isinstance(cut_points, bool):
        raw_cuts = [int(cut_points)]
    elif isinstance(cut_points, (list, tuple, pd.Index, np.ndarray)):
        raw_cuts = list(cut_points)
    else:
        raw_cuts = [cut_points]

    if len(raw_cuts) == 0:
        raise ValueError("cut_points must be non-empty")

    n = len(index)
    resolved: list[tuple[Any, int]] = []
    for cp in raw_cuts:
        if isinstance(cp, (int, np.integer)) and not isinstance(cp, bool):
            pos = int(cp)
            if pos < 0 or pos >= n - 1:
                raise ValueError(
                    f"Cut position {cp!r} must satisfy 0 <= pos < {n - 1} "
                    "(requires at least one history row and one future row)."
                )
            resolved.append((cp, pos))
            continue

        label = cp
        if label not in index and isinstance(index, pd.DatetimeIndex):
            try:
                ts = pd.Timestamp(label)
            except Exception as exc:
                raise ValueError(f"Cut label {cp!r} not found in index") from exc
            if ts in index:
                label = ts

        if label not in index:
            raise ValueError(f"Cut label {cp!r} not found in index")

        loc = index.get_loc(label)
        if not isinstance(loc, (int, np.integer)):
            raise ValueError(f"Cut label {cp!r} did not resolve to a unique position")
        pos = int(loc)
        if pos < 0 or pos >= n - 1:
            raise ValueError(
                f"Cut label {cp!r} (pos={pos}) must leave at least one history row "
                f"and one future row (index length={n})."
            )
        resolved.append((cp, pos))

    return resolved


def assert_future_append_invariant(
    fn: Callable[[Any], Any],
    data: Union[pd.Series, pd.DataFrame],
    cut_points: Union[int, np.integer, Any, Sequence[Any]],
    *,
    rtol: float = 0.0,
    atol: float = 0.0,
    check_dtype: bool = True,
) -> None:
    """Assert that ``fn`` produces identical historical outputs when future rows are appended.

    Parameters
    ----------
    fn : callable
        Pure transform ``fn(data) -> Series | DataFrame``.
    data : pd.Series or pd.DataFrame
        Input series or panel with a strictly monotonic increasing unique index.
    cut_points : int, label, or sequence of int/label
        Inclusive last history row(s). Each cut must leave at least one history
        row and at least one future row, otherwise ``ValueError`` is raised.
    rtol : float, default 0.0
        Relative tolerance; when both ``rtol`` and ``atol`` are ``0.0``, exact
        equality (``check_exact=True``) is enforced.
    atol : float, default 0.0
        Absolute tolerance.
    check_dtype : bool, default True
        Whether to require identical output dtypes.

    Raises
    ------
    TypeError
        If ``data`` or ``fn`` output is not a ``pd.Series`` or ``pd.DataFrame``.
    ValueError
        If ``data.index`` is not monotonic increasing and unique, or any cut point
        leaves no history or no future rows.
    LookaheadViolation
        If the historical output diverges from the full-sample projection.
    """
    if not isinstance(data, (pd.Series, pd.DataFrame)):
        raise TypeError(f"data must be pd.Series or pd.DataFrame, got {type(data).__name__}")
    if not data.index.is_monotonic_increasing or not data.index.is_unique:
        raise ValueError("data.index must be monotonic increasing and unique")

    cuts = _resolve_cut_positions(data.index, cut_points)
    out_full = fn(data.copy(deep=True))
    if not isinstance(out_full, (pd.Series, pd.DataFrame)):
        raise TypeError(
            f"fn(full) must return pd.Series or pd.DataFrame, got {type(out_full).__name__}"
        )

    for cp, pos in cuts:
        hist = data.iloc[: pos + 1].copy(deep=True)
        cut_label = hist.index[-1]
        out_hist = fn(hist)
        if not isinstance(out_hist, (pd.Series, pd.DataFrame)):
            raise TypeError(
                f"fn(hist) at cut={cp!r} must return pd.Series or pd.DataFrame, "
                f"got {type(out_hist).__name__}"
            )

        if isinstance(out_full.index, pd.DatetimeIndex) and isinstance(
            out_hist.index, pd.DatetimeIndex
        ):
            proj = out_full.loc[out_full.index <= cut_label]
        else:
            missing = out_hist.index.difference(out_full.index)
            if len(missing) > 0:
                raise LookaheadViolation(
                    f"Future-append invariance violated at cut={cp!r}: "
                    f"history output has index labels absent from full output: {list(missing[:5])}"
                )
            proj = out_full.loc[out_hist.index]

        _assert_same(
            out_hist,
            proj,
            context=f"Future-append invariance violated at cut={cp!r} (label={cut_label!r})",
            rtol=rtol,
            atol=atol,
            check_dtype=check_dtype,
        )


def assert_cutoff_truncation_equivalent(
    fn: Callable[[pd.DataFrame, Any], Any],
    bars: pd.DataFrame,
    cutoff_time: Union[str, datetime.time],
    *,
    method: Literal["truncate", "perturb"] = "truncate",
    project: Callable[[Any], Any] | None = None,
    perturb_range: tuple[float, float] = (1.5, 3.0),
    seed: int = 0,
    rtol: float = 0.0,
    atol: float = 0.0,
    check_dtype: bool = True,
) -> None:
    """Assert that an intraday function ``fn(bars, cutoff_time)`` ignores post-cutoff bars.

    Parameters
    ----------
    fn : callable
        Function with signature ``fn(bars, cutoff_time) -> Series | DataFrame``.
        The harness passes ``cutoff_time`` through unchanged.
    bars : pd.DataFrame
        Intraday OHLCV bars indexed by ``pd.DatetimeIndex``.
    cutoff_time : str or datetime.time
        Strictly intraday cutoff (e.g., ``'14:15'``). ``None``, ``'15:00'``, or
        ``'full'`` raises ``ValueError`` because the check would be vacuous.
    method : {'truncate', 'perturb'}, default 'truncate'
        - ``'truncate'``: compares ``fn(bars[time <= c], c)`` vs ``fn(bars, c)``.
          Use for pure window functions without a full-day bar-count gate.
        - ``'perturb'``: preserves all timestamps and scales numeric columns of
          post-cutoff rows by seeded per-row factors in ``perturb_range`` (writing
          integer columns back via ``np.rint(...).astype(orig_dtype)``). Use for
          gated functions such as ``compute_daily_intraday_features`` whose
          ``< 45`` full-day bar-count gate (``intraday_features.py`` L540/L579-593)
          would otherwise drop every truncated day.
    project : callable or None, default None
        Optional projection applied to both outputs before comparison. Defaults to
        keeping ``index.time <= cutoff`` if the output has an intraday
        ``DatetimeIndex``, and identity otherwise.
    perturb_range : tuple[float, float], default (1.5, 3.0)
        Uniform multiplier bounds for ``method='perturb'``.
    seed : int, default 0
        Seed for ``np.random.default_rng`` in ``method='perturb'``.
    rtol : float, default 0.0
        Relative tolerance passed to comparison.
    atol : float, default 0.0
        Absolute tolerance passed to comparison.
    check_dtype : bool, default True
        Whether to require identical output dtypes.

    Raises
    ------
    TypeError
        If ``bars`` is not a DataFrame with a ``pd.DatetimeIndex``, or if ``fn``
        returns an unsupported type.
    ValueError
        If ``cutoff_time`` resolves to full-day (``None``), ``bars`` has no rows
        after ``cutoff_time``, or ``method`` / ``perturb_range`` is invalid.
    LookaheadViolation
        If post-cutoff bars affect the projected output.
    """
    if not isinstance(bars, pd.DataFrame) or not isinstance(bars.index, pd.DatetimeIndex):
        raise TypeError("bars must be a pd.DataFrame with a pd.DatetimeIndex")
    if method not in ("truncate", "perturb"):
        raise ValueError(f"Unknown method {method!r}; expected 'truncate' or 'perturb'")

    cutoff_t = _parse_time_cutoff(cutoff_time)
    if cutoff_t is None:
        raise ValueError(
            f"cutoff_time={cutoff_time!r} resolves to full-day (None); "
            "a strictly intraday cutoff is required."
        )

    after_mask = bars.index.time > cutoff_t
    if not bool(after_mask.any()):
        raise ValueError(
            f"bars contains no rows after cutoff_time={cutoff_time!r}; "
            "equivalence check would pass vacuously."
        )

    if project is None:
        def _default_project(out: Any) -> Any:
            if isinstance(out, (pd.Series, pd.DataFrame)) and isinstance(
                out.index, pd.DatetimeIndex
            ):
                times = out.index.time
                has_intraday = any(
                    (t.hour != 0 or t.minute != 0 or t.second != 0 or t.microsecond != 0)
                    for t in times
                )
                if has_intraday:
                    return out.loc[times <= cutoff_t]
            return out

        proj_fn = _default_project
    else:
        proj_fn = project

    out_full = fn(bars.copy(deep=True), cutoff_time)

    if method == "truncate":
        bars_alt = bars.loc[~after_mask].copy(deep=True)
        out_alt = fn(bars_alt, cutoff_time)
    else:
        lo, hi = perturb_range
        if not (np.isfinite(lo) and np.isfinite(hi) and lo > 0.0 and lo < hi):
            raise ValueError(
                f"perturb_range must satisfy 0 < lo < hi, got {perturb_range!r}"
            )
        bars_p = bars.copy(deep=True)
        rng = np.random.default_rng(seed)
        n_after = int(after_mask.sum())
        factors = rng.uniform(lo, hi, size=n_after)
        after_idx = bars_p.index[after_mask]

        for col in bars_p.columns:
            dtype = bars_p[col].dtype
            if pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype):
                scaled = bars_p.loc[after_idx, col].to_numpy(dtype=np.float64) * factors
                if pd.api.types.is_integer_dtype(dtype):
                    bars_p.loc[after_idx, col] = np.rint(scaled).astype(dtype)
                else:
                    bars_p.loc[after_idx, col] = scaled.astype(dtype)

        out_alt = fn(bars_p, cutoff_time)

    lhs = proj_fn(out_alt)
    rhs = proj_fn(out_full)
    _assert_same(
        lhs,
        rhs,
        context=f"Cutoff equivalence ({method}) violated at cutoff_time={cutoff_time!r}",
        rtol=rtol,
        atol=atol,
        check_dtype=check_dtype,
    )

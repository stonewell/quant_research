"""Unit tests for common/structural_factors.py feature enrichment pipeline.

Validates Task B2.1:
1. enrich_universe_with_structural_features creates all 12 required columns.
2. Idempotent memoization skips computation when columns already exist.
3. Inplace vs copy behavior.
4. Robustness against empty and short DataFrames.

100% offline using synthetic OHLCV data generators.
"""

import itertools
import math
import os
import sys
import warnings
import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.structural_factors import (
    CURVATURE_KAPPA_BOUND_WINDOW5,
    DEFAULT_REACHABLE_MIN_CURVATURE,
    enrich_universe_with_structural_features,
    compute_rolling_poc_series,
    extremum_curvature,
    fractal_dimension,
    standardized_divergence,
    warn_unreachable_curvature_threshold,
)
from common.testing import make_ohlcv_from_closes


REQUIRED_COLUMNS = [
    "cyq_winner_ratio",
    "cyq_cost_concentration",
    "wyckoff_spring",
    "extremum_curvature",
    "standardized_divergence",
    "wyckoff_effort_result",
    "fractal_dimension",
    "consolidation_density",
    "POC",
    "VAL",
    "VAH",
    "NPOC",
]


def _build_test_bars(n: int = 80, base: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = base + np.cumsum(np.random.normal(0, 0.5, n))
    bars = make_ohlcv_from_closes(closes)
    bars.index = dates
    bars["Volume"] = 10000.0
    bars["Turn"] = 2.0
    return bars


def test_enrich_universe_with_structural_features_creates_all_columns():
    """Verify all 12 required structural columns are attached to universe DataFrames."""
    np.random.seed(42)
    universe = {
        "ASSET_A": _build_test_bars(80, 10.0),
        "ASSET_B": _build_test_bars(80, 50.0),
    }

    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)

    for sym, df in enriched.items():
        assert len(df) == 80
        for col in REQUIRED_COLUMNS:
            assert col in df.columns, f"Missing required column {col} in {sym}"
            assert len(df[col]) == 80

        # Check values
        assert not df["POC"].dropna().empty
        assert not df["VAL"].dropna().empty
        assert not df["VAH"].dropna().empty
        assert not df["NPOC"].dropna().empty
        assert np.all((df["cyq_winner_ratio"] >= 0.0) & (df["cyq_winner_ratio"] <= 1.0))
        assert np.all((df["cyq_cost_concentration"] >= 0.0) & (df["cyq_cost_concentration"] <= 1.0))


def test_enrich_universe_with_structural_features_memoization_idempotency():
    """Verify pre-existing columns are skipped and not overwritten."""
    np.random.seed(42)
    bars = _build_test_bars(80, 20.0)
    for col in REQUIRED_COLUMNS:
        bars[col] = 999.0

    universe = {"MEMO_ASSET": bars}
    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)

    # Should have skipped re-computation and preserved 999.0
    for col in REQUIRED_COLUMNS:
        assert np.all(enriched["MEMO_ASSET"][col] == 999.0)


def test_enrich_universe_with_structural_features_inplace_flag():
    """Verify inplace=True modifies original frame, while inplace=False preserves it."""
    np.random.seed(42)
    bars_inplace = _build_test_bars(80, 30.0)
    universe_inplace = {"ASSET_IP": bars_inplace}
    res_inplace = enrich_universe_with_structural_features(universe_inplace, lookback=60, inplace=True)
    assert res_inplace["ASSET_IP"] is bars_inplace
    assert "POC" in bars_inplace.columns

    bars_copy = _build_test_bars(80, 40.0)
    universe_copy = {"ASSET_CP": bars_copy}
    res_copy = enrich_universe_with_structural_features(universe_copy, lookback=60, inplace=False)
    assert res_copy["ASSET_CP"] is not bars_copy
    assert "POC" not in bars_copy.columns
    assert "POC" in res_copy["ASSET_CP"].columns


def test_enrich_universe_with_structural_features_empty_and_short_df():
    """Verify empty and short dataframes are handled safely without exceptions."""
    empty_df = pd.DataFrame()
    short_df = pd.DataFrame({
        "High": [10.0, 10.5],
        "Low": [9.5, 9.8],
        "Close": [9.9, 10.2],
        "Volume": [100.0, 150.0],
    })
    universe = {"EMPTY": empty_df, "SHORT": short_df}
    enriched = enrich_universe_with_structural_features(universe, lookback=60, inplace=False)
    assert enriched["EMPTY"].empty
    assert len(enriched["SHORT"]) == 2
    for col in REQUIRED_COLUMNS:
        assert col in enriched["SHORT"].columns


# ---------------------------------------------------------------------------
# Task C5.3: the three Series-level operators must share ONE input contract.
# ---------------------------------------------------------------------------

_SERIES_LEVEL_OPERATORS = ("extremum_curvature", "standardized_divergence", "fractal_dimension")


def _call_series_operator(op_name, price, indicator):
    if op_name == "extremum_curvature":
        return extremum_curvature(price)
    if op_name == "fractal_dimension":
        return fractal_dimension(price)
    return standardized_divergence(price, indicator)


@pytest.mark.parametrize("op_name", _SERIES_LEVEL_OPERATORS)
def test_series_level_operators_accept_series_and_single_column_frame(op_name):
    """Series / 1-column DataFrame must produce identical 1-D output.

    Regression for C5.3: fractal_dimension used to raise
    "Data must be 1-dimensional" on a 1-column frame (its R/S estimator got an
    (n,1) array), extremum_curvature silently worked, and
    standardized_divergence returned a DataFrame instead of a Series.
    """
    rs = np.random.RandomState(0)
    close = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, 0.01, 160))))
    indicator = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()

    as_series = _call_series_operator(op_name, close, indicator)
    as_frame = _call_series_operator(op_name, close.to_frame("Close"), indicator)

    assert isinstance(as_series, pd.Series), f"{op_name} must return a Series"
    assert isinstance(as_frame, pd.Series), f"{op_name} must return a Series for 1-D input"
    assert list(as_frame.index) == list(close.index)
    np.testing.assert_allclose(
        as_frame.to_numpy(), as_series.to_numpy(), rtol=0.0, atol=1e-12, equal_nan=True
    )


def test_standardized_divergence_accepts_single_column_indicator_frame():
    """The indicator argument follows the same 1-D contract (C5.3)."""
    rs = np.random.RandomState(1)
    close = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, 0.01, 120))))
    ind = pd.Series(rs.normal(0.0, 1.0, 120)).cumsum()
    ref = standardized_divergence(close, ind)
    got = standardized_divergence(close, ind.to_frame("ind"))
    assert isinstance(got, pd.Series)
    np.testing.assert_allclose(got.to_numpy(), ref.to_numpy(), atol=1e-12, equal_nan=True)


@pytest.mark.parametrize("op_name", _SERIES_LEVEL_OPERATORS)
def test_series_level_operators_reject_multi_column_frame(op_name):
    """A multi-column frame is ambiguous and must raise explicitly (C5.3)."""
    close = pd.Series(np.linspace(100.0, 110.0, 40))
    indicator = pd.Series(np.linspace(0.0, 1.0, 40))
    multi = pd.DataFrame({"A": close, "B": close * 1.01})
    with pytest.raises(
        ValueError,
        match=(
            rf"{op_name} expects a Series or a single-column DataFrame; "
            rf"got a DataFrame with 2 columns"
        ),
    ):
        _call_series_operator(op_name, multi, indicator)


# ---------------------------------------------------------------------------
# Task C5.1: curvature reachability (the 1.5 gate was structurally dead).
# ---------------------------------------------------------------------------


def test_curvature_kappa_bound_constant_matches_algebra():
    assert math.isclose(
        CURVATURE_KAPPA_BOUND_WINDOW5, 2.0 * math.sqrt(5.0 / 14.0), rel_tol=0.0, abs_tol=1e-15
    )
    assert math.isclose(CURVATURE_KAPPA_BOUND_WINDOW5, 1.1952286093343936, rel_tol=0.0, abs_tol=1e-12)


def test_extremum_curvature_window5_bound_respected_adversarially():
    """|kappa| <= 2*sqrt(5/14) for every FULL window=5 fit.

    Adversarial coverage: the complete {-1,0,1}^5 sign/level grid plus ~54k
    random windows at several volatility scales. min_periods=5 keeps partial
    warm-up fits out (their k=3/k=4 bounds are 4.2426 and 2.0, which is exactly
    why the historical 1.5 gate looked alive in the first bars of a series).
    """
    bound = CURVATURE_KAPPA_BOUND_WINDOW5

    grid = np.array(list(itertools.product([-1.0, 0.0, 1.0], repeat=5)))
    for window in grid:
        kappa = extremum_curvature(pd.Series(window), window=5, min_periods=5).iloc[-1]
        assert abs(kappa) <= bound + 1e-12, f"grid window {window} gave kappa={kappa}"
        # The old production gate could never fire on a full window.
        assert kappa < 1.5

    rs = np.random.RandomState(12345)
    for vol in (0.002, 0.01, 0.05):
        for _ in range(3):
            price = pd.Series(100.0 * np.exp(np.cumsum(rs.normal(0.0, vol, 6000))))
            curv = extremum_curvature(price, window=5, min_periods=5).dropna()
            assert len(curv) > 0
            assert curv.abs().max() <= bound + 1e-9, (
                f"full-window |kappa| {curv.abs().max()} exceeded analytic bound {bound}"
            )
            assert not (curv.abs() >= 1.1953).any()


def test_reachable_default_curvature_attainable_by_v_bottom():
    """DEFAULT_REACHABLE_MIN_CURVATURE=0.8 must lie strictly inside the bound.

    A symmetric 5-point V-bottom [2,1,0,1,2] scores kappa ~= 1.1454, so the new
    default is genuinely attainable by a realistic reversal fixture while the
    old 1.5 was not. (0.8 is an INTERIM reachability default, not a
    walkforward-optimised threshold.)
    """
    assert 0.0 < DEFAULT_REACHABLE_MIN_CURVATURE < CURVATURE_KAPPA_BOUND_WINDOW5

    v_bottom = pd.Series([2.0, 1.0, 0.0, 1.0, 2.0])
    kappa = extremum_curvature(v_bottom, window=5, min_periods=5).iloc[-1]
    # Deterministic and scale-invariant: [2,1,0,1,2] and [1,0.5,0,0.5,1]
    # both score ~= 1.1454 (differing only at the 8th decimal from polyfit).
    assert math.isclose(kappa, 1.1454, rel_tol=0.0, abs_tol=1e-3)
    assert DEFAULT_REACHABLE_MIN_CURVATURE < kappa < 1.5

    # Zero-mean noise is centred on kappa ~= 0 (though individual windows can
    # approach the bound), so 0.8 still filters most non-turning-point windows.
    rs = np.random.RandomState(2024)
    noise_kappas = np.array([
        extremum_curvature(pd.Series(rs.normal(size=5)), window=5, min_periods=5).iloc[-1]
        for _ in range(4000)
    ])
    assert abs(noise_kappas.mean()) < 0.05


def test_warn_unreachable_curvature_threshold():
    """The dead-gate guard warns at/above the bound and stays silent below."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert warn_unreachable_curvature_threshold(DEFAULT_REACHABLE_MIN_CURVATURE) is False
        assert warn_unreachable_curvature_threshold(1.19) is False
        # Exactly at the analytic bound -> unreachable (a full window can only
        # touch it, never exceed it, so `>=` cannot fire in practice).
        assert warn_unreachable_curvature_threshold(CURVATURE_KAPPA_BOUND_WINDOW5) is True
        assert warn_unreachable_curvature_threshold(1.1953) is True
        assert warn_unreachable_curvature_threshold(1.5, context="some.gate") is True
        assert warn_unreachable_curvature_threshold(-1.5) is True

    assert len(caught) == 4
    assert all(issubclass(w.category, UserWarning) for w in caught)
    assert "some.gate" in str(caught[2].message)
    assert "1.5" in str(caught[2].message)

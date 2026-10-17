"""Unit tests for common/structural_factors.py (Wave 0 / Tier P0 Task C1.1).

Guaranteed 100% offline using synthetic OHLCV data generators from common/testing.py
and small hand-crafted DataFrames. No network access or external APIs.
"""

import ast
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

from common.structural_factors import compute_rolling_poc_series
from common.testing import make_ohlcv_from_closes
from research_strategy.rs.chan_similar_strategies import (
    compute_rolling_poc_series as reexported_compute_rolling_poc_series,
)


def test_no_pipeline_imports_in_structural_factors():
    """Verify common/structural_factors.py adheres to Defect M5 constraint: ZERO imports from pipeline/."""
    file_path = os.path.join(_REPO_ROOT, "common", "structural_factors.py")
    assert os.path.isfile(file_path), f"File {file_path} not found"

    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("pipeline"), (
                    f"Forbidden pipeline import found in common/structural_factors.py: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert not node.module.startswith("pipeline"), (
                    f"Forbidden pipeline import found in common/structural_factors.py: {node.module}"
                )


def test_reexported_identity():
    """Verify that compute_rolling_poc_series in chan_similar_strategies is identical to common/structural_factors."""
    assert reexported_compute_rolling_poc_series is compute_rolling_poc_series


def test_poc_output_shapes_and_types():
    """Verify compute_rolling_poc_series returns 3 ndarrays of length n_bars with float dtype."""
    n = 100
    closes = 100.0 + np.sin(np.linspace(0, 10, n)) * 5.0
    bars = make_ohlcv_from_closes(closes)
    lookback = 30
    poc, val, vah = compute_rolling_poc_series(bars, lookback=lookback, n_bins=25, value_area_pct=0.70)

    assert isinstance(poc, np.ndarray)
    assert isinstance(val, np.ndarray)
    assert isinstance(vah, np.ndarray)
    assert len(poc) == n
    assert len(val) == n
    assert len(vah) == n
    assert poc.dtype == np.float64
    assert val.dtype == np.float64
    assert vah.dtype == np.float64


def test_poc_warmup_behavior():
    """Verify first lookback - 1 entries are NaN and all entries from lookback - 1 onward are finite."""
    n = 80
    lookback = 25
    closes = 100.0 + np.arange(n) * 0.1
    bars = make_ohlcv_from_closes(closes)
    bars["Volume"] = 10_000.0

    poc, val, vah = compute_rolling_poc_series(bars, lookback=lookback, n_bins=20)

    # First lookback - 1 entries must be NaN
    assert np.all(np.isnan(poc[: lookback - 1]))
    assert np.all(np.isnan(val[: lookback - 1]))
    assert np.all(np.isnan(vah[: lookback - 1]))

    # Valid entries from lookback - 1 onward must be finite
    assert np.all(np.isfinite(poc[lookback - 1 :]))
    assert np.all(np.isfinite(val[lookback - 1 :]))
    assert np.all(np.isfinite(vah[lookback - 1 :]))


def test_poc_ordering_invariant():
    """Verify val <= poc <= vah invariant holds across all valid bars."""
    n = 120
    closes = 100.0 + np.sin(np.linspace(0, 20, n)) * 10.0
    bars = make_ohlcv_from_closes(closes)
    bars["Volume"] = 1000.0 + np.random.RandomState(42).uniform(100, 5000, n)

    lookback = 40
    poc, val, vah = compute_rolling_poc_series(bars, lookback=lookback, n_bins=30, value_area_pct=0.70)

    for t in range(lookback - 1, n):
        assert val[t] <= poc[t] + 1e-9, f"Violation val[{t}] ({val[t]}) > poc[{t}] ({poc[t]})"
        assert poc[t] <= vah[t] + 1e-9, f"Violation poc[{t}] ({poc[t]}) > vah[{t}] ({vah[t]})"


def test_poc_flat_series_fallback():
    """Verify flat price / zero range fallback (poc == close, val == close * 0.95, vah == close * 1.05)."""
    n = 50
    flat_price = 88.0
    high = np.full(n, flat_price)
    low = np.full(n, flat_price)
    close = np.full(n, flat_price)
    bars = pd.DataFrame({"High": high, "Low": low, "Close": close})

    lookback = 15
    poc, val, vah = compute_rolling_poc_series(bars, lookback=lookback)

    assert len(poc) == n
    assert np.all(np.isnan(poc[: lookback - 1]))
    for t in range(lookback - 1, n):
        assert np.isclose(poc[t], flat_price)
        assert np.isclose(val[t], flat_price * 0.95)
        assert np.isclose(vah[t], flat_price * 1.05)


def test_poc_missing_or_all_nan_volume():
    """Verify graceful handling when Volume column is absent or completely NaN."""
    n = 60
    closes = 50.0 + np.arange(n) * 0.2
    bars = make_ohlcv_from_closes(closes)

    # 1. Missing Volume column
    bars_no_vol = bars[["High", "Low", "Close"]].copy()
    poc1, val1, vah1 = compute_rolling_poc_series(bars_no_vol, lookback=20)
    assert not np.isnan(poc1[-1])
    assert val1[-1] <= poc1[-1] <= vah1[-1]

    # 2. Volume column exists but is all NaN
    bars_nan_vol = bars.copy()
    bars_nan_vol["Volume"] = np.nan
    poc2, val2, vah2 = compute_rolling_poc_series(bars_nan_vol, lookback=20)
    assert not np.isnan(poc2[-1])
    assert val2[-1] <= poc2[-1] <= vah2[-1]

    # Should match results since fallback is unweighted / equal volume
    np.testing.assert_allclose(poc1, poc2, equal_nan=True)
    np.testing.assert_allclose(val1, val2, equal_nan=True)
    np.testing.assert_allclose(vah1, vah2, equal_nan=True)


def test_poc_identifies_volume_cluster():
    """Verify rolling POC locates high volume price clusters accurately."""
    n = 80
    high = np.full(n, 105.0)
    low = np.full(n, 95.0)
    close = np.full(n, 100.0)
    volume = np.full(n, 1000.0)

    # Heavy trading cluster at price 104 between bars 40 and 50
    high[40:50] = 105.0
    low[40:50] = 103.0
    close[40:50] = 104.0
    volume[40:50] = 50000.0

    bars = pd.DataFrame({"High": high, "Low": low, "Close": close, "Volume": volume})
    poc, val, vah = compute_rolling_poc_series(bars, lookback=30, n_bins=20, value_area_pct=0.70)

    # At bar 55, POC should be inside the concentrated range [103.0, 105.0]
    assert not np.isnan(poc[55])
    assert 103.0 <= poc[55] <= 105.0
    assert val[55] <= poc[55] <= vah[55]


def test_poc_short_series_smaller_than_lookback():
    """Verify that when len(bars) < lookback, all outputs are NaN of correct length."""
    n = 15
    closes = 100.0 + np.arange(n)
    bars = make_ohlcv_from_closes(closes)
    poc, val, vah = compute_rolling_poc_series(bars, lookback=30)

    assert len(poc) == n
    assert len(val) == n
    assert len(vah) == n
    assert np.all(np.isnan(poc))
    assert np.all(np.isnan(val))
    assert np.all(np.isnan(vah))

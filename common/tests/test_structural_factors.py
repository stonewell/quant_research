"""Unit tests for common/structural_factors.py feature enrichment pipeline.

Validates Task B2.1:
1. enrich_universe_with_structural_features creates all 12 required columns.
2. Idempotent memoization skips computation when columns already exist.
3. Inplace vs copy behavior.
4. Robustness against empty and short DataFrames.

100% offline using synthetic OHLCV data generators.
"""

import os
import sys
import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.structural_factors import (
    enrich_universe_with_structural_features,
    compute_rolling_poc_series,
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

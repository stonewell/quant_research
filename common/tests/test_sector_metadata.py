"""
common/tests/test_sector_metadata.py

Comprehensive test suite for offline sector metadata and sector-neutralization.
Task M2.1 Core Layer.
100% offline, synthetic data only.
"""

import json
import os
import sys
import tempfile

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.factor_analyzer import compute_rank_ic
from common.sector_metadata import (
    _prefix_sector,
    load_sector_map,
    resolve_sector,
    sector_neutral_demean,
    sector_neutral_zscore,
)
from common.testing import assign_synthetic_sectors


def test_sector_neutral_zscore_degenerate():
    """Case 1: Degenerate sector safety.
    - Single member sector -> assigned exactly 0.0.
    - All-equal members (zero variance) -> assigned exactly 0.0.
    - No division by zero, no exception, no NaN.
    - One degenerate sector must not NaN or corrupt valid non-degenerate sectors.
    """
    values = pd.Series({
        "A": 10.0,  # sector 1 (single-member)
        "B": 5.0,   # sector 2 (zero-variance equal values)
        "C": 5.0,   # sector 2 (zero-variance equal values)
        "D": 1.0,   # sector 3 (valid multi-member)
        "E": 2.0,   # sector 3 (valid multi-member)
        "F": 3.0,   # sector 3 (valid multi-member)
    })
    sectors = {
        "A": "sec_single",
        "B": "sec_const",
        "C": "sec_const",
        "D": "sec_valid",
        "E": "sec_valid",
        "F": "sec_valid",
    }

    result = sector_neutral_zscore(values, sectors)

    # Assert no NaNs or infs
    assert not result.isna().any(), "Result should not contain any NaN values"
    assert not np.isinf(result).any(), "Result should not contain any Inf values"

    # Single-member sector must be 0.0
    assert result["A"] == 0.0

    # Zero-variance sector must be 0.0
    assert result["B"] == 0.0
    assert result["C"] == 0.0

    # Valid sector must be normalized with mean 0.0 and std 1.0
    sec_valid_res = result[["D", "E", "F"]]
    assert np.isclose(sec_valid_res.mean(), 0.0, atol=1e-9)
    assert np.isclose(sec_valid_res.std(ddof=1), 1.0, atol=1e-7)
    assert sec_valid_res["D"] < sec_valid_res["E"] < sec_valid_res["F"]


def test_unknown_symbols_not_dropped():
    """Case 2: Input-output contract.
    - Unknown/unmapped symbols are never dropped.
    - Output index matches input index identically in length, order, and name.
    """
    idx = pd.Index(["SYM_UNKNOWN_1", "601288.SH", "SYM_UNKNOWN_2", "CAT"], name="asset_symbols")
    values = pd.Series([1.5, -0.5, 2.0, 0.1], index=idx, name="alpha_score")
    sector_map = {"601288.SH": "banking", "CAT": "machinery"}

    result = sector_neutral_zscore(values, sector_map, fallback="auto")

    # Exact index structure preservation
    assert len(result) == len(values)
    assert (result.index == values.index).all()
    assert result.index.name == values.index.name
    assert result.name == values.name
    assert "SYM_UNKNOWN_1" in result.index
    assert "SYM_UNKNOWN_2" in result.index


def test_load_sector_map_edge_cases():
    """Case 3: Robust loading edge cases.
    - path=None -> returns empty dict {}
    - missing file -> returns empty dict {}, never raises
    - skips keys starting with '_'
    - uppercases all symbol keys
    """
    # 1. path is None
    assert load_sector_map(None) == {}
    assert load_sector_map("") == {}

    # 2. missing file
    assert load_sector_map("/tmp/non_existent_sector_map_12345.json") == {}

    # 3. temporary valid file with _meta and mixed cases
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump({
            "_meta": {"note": "header"},
            "_comment": "should be skipped",
            "601288.sh": "banking",
            "aapl": "technology",
            "NVDA": "semiconductor",
        }, tmp)
        tmp_path = tmp.name

    try:
        loaded = load_sector_map(tmp_path)
        assert "_meta" not in loaded
        assert "_comment" not in loaded
        assert loaded["601288.SH"] == "banking"
        assert loaded["AAPL"] == "technology"
        assert loaded["NVDA"] == "semiconductor"
        assert len(loaded) == 3
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_assign_synthetic_sectors():
    """Case 4: assign_synthetic_sectors helper.
    - Deterministic across multiple calls with same seed.
    - Assigns every symbol, including delisted or synthetic ones.
    - No filtering or dropping.
    """
    symbols = ["000001.SZ", "600000.SH", "DELISTED_X", "SYNTH_A", "SYNTH_B"]
    sec1 = assign_synthetic_sectors(symbols, n_sectors=3, seed=42)
    sec2 = assign_synthetic_sectors(symbols, n_sectors=3, seed=42)

    # Determinism
    assert sec1 == sec2

    # Completeness
    assert len(sec1) == len(symbols)
    for sym in symbols:
        assert sym in sec1
        assert sec1[sym].startswith("sec_")

    # Different seed yields different random assignment
    sec_diff = assign_synthetic_sectors(symbols, n_sectors=3, seed=999)
    assert len(sec_diff) == len(symbols)


def test_neutralization_invariant_sector_mean():
    """Case 5: Mathematical invariant.
    - Non-degenerate sector output mean must be approximately 0.0 (tol 1e-9).
    """
    symbols = [f"SYM_{i:02d}" for i in range(20)]
    rng = np.random.default_rng(101)
    values = pd.Series(rng.standard_normal(len(symbols)), index=symbols)
    sectors = assign_synthetic_sectors(symbols, n_sectors=4, seed=101)

    z = sector_neutral_zscore(values, sectors)

    sec_series = pd.Series(sectors)
    for sec_name, group in z.groupby(sec_series):
        if len(group) > 1 and not np.isclose(values.loc[group.index].std(), 0.0):
            assert np.isclose(group.mean(), 0.0, atol=1e-9), f"Sector {sec_name} mean != 0.0"


def test_rank_ic_sector_neutralization_panel():
    """Case 6: Panel factor neutralization structural invariant and Rank IC computability.

    This test checks two things:
    1. Structural invariant: per-date and per-sector neutralized factor mean is 0.0
       (tolerance 1e-9), eliminating raw sector bias across the panel.
    2. Rank IC computability smoke check: compute_rank_ic produces finite, non-NaN
       Series for both raw and neutralized panels.

    NOTE: We do NOT claim that Rank IC is preserved or improved here because synthetic
    forward returns are noise (both raw and neutralized IC are near zero). The weight-layer
    allocation active risk budget (|dev| <= 0.03) belongs to M1.3, not here in M2.1.
    """
    dates = pd.bdate_range("2026-01-01", periods=10)
    symbols = [f"A_{i}" for i in range(5)] + [f"B_{i}" for i in range(5)]
    sectors = {sym: ("sec_A" if sym.startswith("A") else "sec_B") for sym in symbols}

    rng = np.random.default_rng(2026)
    factor_data = {}
    fwd_data = {}
    for sym in symbols:
        # Group A has structural +10 factor bias, Group B has -10 factor bias
        bias = 10.0 if sym.startswith("A") else -10.0
        factor_data[sym] = rng.normal(0, 1, len(dates)) + bias
        fwd_data[sym] = rng.normal(0, 0.02, len(dates))

    factor_df = pd.DataFrame(factor_data, index=dates)
    fwd_df = pd.DataFrame(fwd_data, index=dates)

    # Neutralize factor row-by-row
    neutral_factor_df = pd.DataFrame(index=dates, columns=symbols, dtype=float)
    for dt in dates:
        neutral_factor_df.loc[dt] = sector_neutral_zscore(factor_df.loc[dt], sectors)

    # Assert structural invariant: per-date and per-sector neutralized mean ≈ 0 (tol 1e-9)
    # AND raw factor per-sector means are NOT ≈ 0 for at least one sector
    raw_has_bias = False
    for dt in dates:
        for sec in ["sec_A", "sec_B"]:
            sec_syms = [s for s, sc in sectors.items() if sc == sec]
            neu_mean = float(neutral_factor_df.loc[dt, sec_syms].mean())
            assert np.isclose(neu_mean, 0.0, atol=1e-9), f"Date {dt} sector {sec} neutralized mean != 0.0: {neu_mean}"
            raw_mean = float(factor_df.loc[dt, sec_syms].mean())
            if not np.isclose(raw_mean, 0.0, atol=1e-3):
                raw_has_bias = True
    assert raw_has_bias, "Raw factor fixture should carry non-zero sector bias"

    raw_ic = compute_rank_ic(factor_df, fwd_df, min_assets=3)
    neu_ic = compute_rank_ic(neutral_factor_df, fwd_df, min_assets=3)

    assert isinstance(raw_ic, pd.Series)
    assert isinstance(neu_ic, pd.Series)
    assert len(neu_ic) == len(dates)
    assert not raw_ic.isna().any(), "raw_ic contains NaN"
    assert not neu_ic.isna().any(), "neu_ic contains NaN"
    assert np.isfinite(raw_ic).all(), "raw_ic contains non-finite values"
    assert np.isfinite(neu_ic).all(), "neu_ic contains non-finite values"


def test_fallback_auto_ashare_and_us():
    """Case 7: Fallback semantics pinning.
    - fallback='auto':
      - A-share 6-digit with .SH/.SZ -> prefix pseudo-class (e.g. 'sec_60', 'sec_00')
      - US tickers ('AAPL', 'BRK-B') -> 'other'
      - HK tickers without sector map -> 'other'
    - explicit overrides:
      - fallback='prefix' forces prefix classification for any ticker
      - fallback='other' forces 'other' for all unmapped tickers
    """
    empty_map = {}

    # 1. A-share 6-digit under 'auto'
    assert resolve_sector("600000.SH", empty_map, fallback="auto") == "sec_60"
    assert resolve_sector("000001.SZ", empty_map, fallback="auto") == "sec_00"
    assert resolve_sector("300394.SZ", empty_map, fallback="auto") == "sec_30"
    assert resolve_sector("688981.SH", empty_map, fallback="auto") == "sec_68"

    # 2. Non A-share under 'auto' defaults to 'other'
    assert resolve_sector("AAPL", empty_map, fallback="auto") == "other"
    assert resolve_sector("BRK-B", empty_map, fallback="auto") == "other"
    assert resolve_sector("NVDA", empty_map, fallback="auto") == "other"
    assert resolve_sector("0700.HK", empty_map, fallback="auto") == "other"

    # 3. Explicit 'prefix' override
    assert resolve_sector("AAPL", empty_map, fallback="prefix") == "sec_AA"
    assert resolve_sector("NVDA", empty_map, fallback="prefix") == "sec_NV"

    # 4. Explicit 'other' override
    assert resolve_sector("600000.SH", empty_map, fallback="other") == "other"

    # 5. _prefix_sector directly
    assert _prefix_sector("601288.SH") == "sec_60"
    assert _prefix_sector("000001.SZ") == "sec_00"
    assert _prefix_sector("X") == "sec_other"


def test_resolve_sector_fallback_value_error():
    """Case 8: Fallback validation fail-fast.
    - Unrecognised fallback raises ValueError.
    - All three legal values ('auto', 'prefix', 'other') work.
    """
    # 1. Invalid fallback raises ValueError
    with pytest.raises(ValueError, match="unknown fallback"):
        resolve_sector("600000.SH", fallback="invalid_mode")

    with pytest.raises(ValueError, match="unknown fallback"):
        sector_neutral_zscore(pd.Series([1.0, 2.0], index=["A", "B"]), fallback="invalid_mode")

    # 2. All 3 legal values work without error
    assert resolve_sector("600000.SH", fallback="auto") == "sec_60"
    assert resolve_sector("600000.SH", fallback="prefix") == "sec_60"
    assert resolve_sector("600000.SH", fallback="other") == "other"


def test_sector_neutral_zscore_dict_with_none_or_nan():
    """Case 9: Dict with None / NaN sector values resolves via fallback.
    - Dict with None or NaN does not silently drop symbols.
    - Coerces through fallback bucket and computes valid finite z-scores.
    """
    values = pd.Series([10.0, 20.0, 30.0, 40.0], index=["SYM_1", "SYM_2", "SYM_3", "SYM_4"])
    sectors = {
        "SYM_1": None,
        "SYM_2": np.nan,
        "SYM_3": "banking",
        "SYM_4": "banking",
    }
    result = sector_neutral_zscore(values, sectors, fallback="other")

    assert len(result) == 4
    assert not result.isna().any(), "No NaN values expected"
    assert np.isfinite(result).all(), "All z-scores should be finite"
    # SYM_1 and SYM_2 land in 'other' fallback bucket (values 10.0 and 20.0)
    assert np.isclose(result[["SYM_1", "SYM_2"]].mean(), 0.0, atol=1e-9)
    assert np.isclose(result[["SYM_1", "SYM_2"]].std(ddof=1), 1.0, atol=1e-7)
    # SYM_3 and SYM_4 are grouped in 'banking'
    assert np.isclose(result[["SYM_3", "SYM_4"]].mean(), 0.0, atol=1e-9)
    assert np.isclose(result[["SYM_3", "SYM_4"]].std(ddof=1), 1.0, atol=1e-7)


def test_sector_neutral_zscore_series_branch():
    """Case 10: pd.Series branch with shuffled index and NaN label.
    - Index order of sector Series does not match values.index (shuffled).
    - Contains one NaN sector label resolved via fallback.
    - Asserts output index identical to input values.index.
    - Asserts correct per-sector grouping after alignment.
    - Asserts no symbols dropped and all z-scores finite.
    """
    values = pd.Series(
        [1.0, 2.0, 10.0, 100.0, 200.0],
        index=["s1", "s2", "s3", "s4", "s5"],
        name="my_factor",
    )
    sector_series = pd.Series(
        ["sec_B", "sec_A", np.nan, "sec_A", "sec_B"],
        index=["s4", "s2", "s3", "s1", "s5"],
    )

    result = sector_neutral_zscore(values, sector_series, fallback="auto")

    # 1. Output index identical to input values.index
    assert (result.index == values.index).all()
    assert result.name == values.name
    assert len(result) == 5

    # 2. Correct per-sector grouping:
    # sec_A: s1 (1.0), s2 (2.0)
    assert np.isclose(result[["s1", "s2"]].mean(), 0.0, atol=1e-9)
    assert np.isclose(result[["s1", "s2"]].std(ddof=1), 1.0, atol=1e-7)

    # sec_B: s4 (100.0), s5 (200.0)
    assert np.isclose(result[["s4", "s5"]].mean(), 0.0, atol=1e-9)
    assert np.isclose(result[["s4", "s5"]].std(ddof=1), 1.0, atol=1e-7)

    # s3 had NaN sector -> resolves via fallback='auto' to 'other' (single member -> 0.0)
    assert result["s3"] == 0.0
    assert not result.isna().any()


def test_sector_neutral_demean_single_group_identity():
    """Case 11 (a): Single-group identity.
    All symbols in one sector => output exactly equals input (graceful no-op degradation).
    """
    values = pd.Series([12.5, 45.0, 78.2, 90.1], index=["s1", "s2", "s3", "s4"], name="score")
    sectors = {"s1": "tech", "s2": "tech", "s3": "tech", "s4": "tech"}
    result = sector_neutral_demean(values, sectors)
    pd.testing.assert_series_equal(result, values, check_exact=False, rtol=1e-12, atol=1e-12)


def test_sector_neutral_demean_sector_level_shift_removed():
    """Case 12 (b): Sector-level shift removed.
    Fixture with large between-sector level gap => every qualifying sector mean equals global_mean (atol 1e-9).
    Also asserts raw sector means differ significantly (fixture is biased).
    """
    values = pd.Series(
        [15.0, 25.0, 20.0, 75.0, 85.0, 80.0],
        index=["a1", "a2", "a3", "b1", "b2", "b3"],
        name="selection_score",
    )
    sectors = {
        "a1": "sec_A", "a2": "sec_A", "a3": "sec_A",
        "b1": "sec_B", "b2": "sec_B", "b3": "sec_B",
    }
    raw_mean_a = float(values[["a1", "a2", "a3"]].mean())
    raw_mean_b = float(values[["b1", "b2", "b3"]].mean())
    assert abs(raw_mean_a - raw_mean_b) > 40.0, "Raw fixture must carry non-zero sector bias"

    global_mean = float(values.mean())
    result = sector_neutral_demean(values, sectors)

    res_mean_a = float(result[["a1", "a2", "a3"]].mean())
    res_mean_b = float(result[["b1", "b2", "b3"]].mean())

    assert np.isclose(res_mean_a, global_mean, atol=1e-9)
    assert np.isclose(res_mean_b, global_mean, atol=1e-9)
    assert np.isclose(float(result.mean()), global_mean, atol=1e-9)


def test_sector_neutral_demean_small_groups_untouched():
    """Case 13 (c): Small groups untouched.
    1-member sector keeps its raw value bit-for-bit, while qualifying multi-member sector is shifted.
    """
    values = pd.Series([10.0, 30.0, 99.0], index=["a1", "a2", "single"], name="score")
    sectors = {"a1": "sec_A", "a2": "sec_A", "single": "sec_solo"}
    result = sector_neutral_demean(values, sectors, min_group_size=2)

    # 1-member sector keeps raw value bit-for-bit
    assert result["single"] == 99.0

    global_mean = float(values.mean())
    assert np.isclose(float(result[["a1", "a2"]].mean()), global_mean, atol=1e-9)
    assert result["a1"] != 10.0
    assert result["a2"] != 30.0


def test_sector_neutral_demean_zero_spread_group_untouched():
    """Case 14 (d): Zero-spread group untouched.
    4-member sector where all values are identical keeps those values (no NaN, no exception).
    """
    values = pd.Series(
        [50.0, 50.0, 50.0, 50.0, 10.0, 90.0],
        index=["z1", "z2", "z3", "z4", "b1", "b2"],
        name="score",
    )
    sectors = {
        "z1": "sec_zero", "z2": "sec_zero", "z3": "sec_zero", "z4": "sec_zero",
        "b1": "sec_B", "b2": "sec_B",
    }
    result = sector_neutral_demean(values, sectors, min_group_size=2)

    for sym in ["z1", "z2", "z3", "z4"]:
        assert result[sym] == 50.0
    assert not result.isna().any()

    global_mean = float(values.mean())
    assert np.isclose(float(result[["b1", "b2"]].mean()), global_mean, atol=1e-9)


def test_sector_neutral_demean_index_contract_and_nans():
    """Case 15 (e): Index contract and NaN handling.
    - Output index identical in length, order, and name.
    - Map-less unknown symbols not dropped.
    - Input NaN stays NaN; no new NaN introduced.
    """
    values = pd.Series(
        [10.0, np.nan, 30.0, 40.0],
        index=["SYM_A", "SYM_B", "UNKNOWN_X", "UNKNOWN_Y"],
        name="test_index_contract",
    )
    sectors = {"SYM_A": "banking", "SYM_B": "banking"}
    result = sector_neutral_demean(values, sectors, fallback="auto")

    assert (result.index == values.index).all()
    assert len(result) == len(values)
    assert result.name == values.name
    assert np.isnan(result["SYM_B"])
    assert not np.isnan(result["SYM_A"])
    assert not np.isnan(result["UNKNOWN_X"])
    assert not np.isnan(result["UNKNOWN_Y"])


def test_sector_neutral_demean_validation_errors():
    """Case 16 (f): min_group_size and fallback validation fail-fast.
    - fallback not in VALID_FALLBACKS raises ValueError.
    - min_group_size < 1 raises ValueError.
    """
    values = pd.Series([10.0, 20.0], index=["s1", "s2"])

    with pytest.raises(ValueError, match="unknown fallback"):
        sector_neutral_demean(values, fallback="invalid_mode")

    with pytest.raises(ValueError, match="min_group_size must be >= 1"):
        sector_neutral_demean(values, min_group_size=0)

    with pytest.raises(ValueError, match="min_group_size must be >= 1"):
        sector_neutral_demean(values, min_group_size=-1)


def test_sector_neutral_demean_series_branch_shuffled():
    """Case 17 (g): sector passed as a shuffled pd.Series aligns correctly.
    """
    values = pd.Series(
        [10.0, 20.0, 80.0, 90.0, 50.0],
        index=["s1", "s2", "s3", "s4", "s5"],
        name="score",
    )
    sector_series = pd.Series(
        ["sec_B", "sec_A", np.nan, "sec_B", "sec_A"],
        index=["s3", "s1", "s5", "s4", "s2"],
    )
    result = sector_neutral_demean(values, sector_series, fallback="auto")

    assert (result.index == values.index).all()
    assert result.name == values.name
    assert len(result) == 5

    global_mean = float(values.mean())
    assert np.isclose(float(result[["s1", "s2"]].mean()), global_mean, atol=1e-9)
    assert np.isclose(float(result[["s3", "s4"]].mean()), global_mean, atol=1e-9)
    assert result["s5"] == 50.0


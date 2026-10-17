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

from common.structural_factors import (
    compute_rolling_poc_series,
    consolidation_density,
    cyq_cost_concentration,
    cyq_chip_distribution,
    cyq_winner_ratio,
    extremum_curvature,
    fractal_dimension,
    standardized_divergence,
    wyckoff_effort_result,
    wyckoff_spring,
)
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


def test_extremum_curvature_scale_invariance_and_v_bottom():
    """Verify extremum_curvature scale invariance (M1) and sharp V-bottom curvature >= 1.5."""
    np.random.seed(42)
    n = 60
    price = pd.Series(100.0 + np.cumsum(np.random.normal(0, 1, n)))

    # 1. Scale invariance: multiplying price by 500x produces identical curvature
    c1 = extremum_curvature(price, window=5, min_periods=3)
    c2 = extremum_curvature(price * 500.0, window=5, min_periods=3)
    np.testing.assert_allclose(c1.dropna(), c2.dropna(), rtol=1e-5, atol=1e-5)

    # 2. Sharp V-bottom yields kappa >= 1.5
    v_bottom = pd.Series([100.0, 90.0, 100.0])
    c_v = extremum_curvature(v_bottom, window=3, min_periods=3)
    assert c_v.iloc[-1] >= 1.5, f"Expected V-bottom curvature >= 1.5, got {c_v.iloc[-1]}"

    # 3. Flat price yields kappa == 0.0
    flat_price = pd.Series([100.0] * 10)
    c_flat = extremum_curvature(flat_price, window=5, min_periods=3)
    assert np.all(c_flat.dropna() == 0.0)


def test_standardized_divergence_polarity_and_normalization():
    """Verify standardized_divergence polarity (M2: bottom > 0, top < 0) and zero scale dominance."""
    n = 60
    np.random.seed(42)
    base_p = 100.0 + np.cumsum(np.random.normal(0, 0.5, n))
    base_ind = 50.0 + np.cumsum(np.random.normal(0, 0.5, n))

    # 1. Bullish bottom divergence: price makes lower lows while momentum rises
    p_bull = base_p.copy()
    ind_bull = base_ind.copy()
    p_bull[40:] = np.linspace(95.0, 75.0, 20)
    ind_bull[40:] = np.linspace(45.0, 65.0, 20)
    div_bull = standardized_divergence(pd.Series(p_bull), pd.Series(ind_bull), lag=5, vol_lookback=20)
    assert div_bull.iloc[-1] > 1.2, f"Expected bullish divergence > 1.2, got {div_bull.iloc[-1]}"

    # 2. Bearish top divergence: price makes higher highs while momentum collapses
    p_bear = base_p.copy()
    ind_bear = base_ind.copy()
    p_bear[40:] = np.linspace(95.0, 125.0, 20)
    ind_bear[40:] = np.linspace(65.0, 35.0, 20)
    div_bear = standardized_divergence(pd.Series(p_bear), pd.Series(ind_bear), lag=5, vol_lookback=20)
    assert div_bear.iloc[-1] < -1.0, f"Expected bearish divergence < -1.0, got {div_bear.iloc[-1]}"

    # 3. Scale invariance: indicator multiplied by 10,000 does not dominate price return
    ind_scaled = pd.Series(ind_bear) * 10_000.0
    div_scaled = standardized_divergence(pd.Series(p_bear), ind_scaled, lag=5, vol_lookback=20)
    np.testing.assert_allclose(div_bear.dropna(), div_scaled.dropna(), rtol=1e-5, atol=1e-5)


def test_wyckoff_effort_result_positive_and_signed():
    """Verify wyckoff_effort_result zero division immunity (M3) and institutional distribution < 0.35."""
    n = 40
    # 1. Constant price and volume: zero division immunity
    bars_flat = pd.DataFrame({
        "High": np.full(n, 10.0),
        "Low": np.full(n, 10.0),
        "Close": np.full(n, 10.0),
        "Volume": np.full(n, 1000.0),
    })
    er_flat = wyckoff_effort_result(bars_flat, lag=5, volume_lookback=20)
    assert np.all(np.isfinite(er_flat.dropna()))
    assert np.all(er_flat.dropna() == 0.0)

    # 2. High volume narrow spread yields low EffortResult < 0.35 (institutional absorption/distribution)
    np.random.seed(42)
    closes = 100.0 + np.cumsum(np.random.normal(0, 0.5, n))
    bars = pd.DataFrame({
        "High": closes + 1.0,
        "Low": closes - 1.0,
        "Close": closes,
        "Volume": np.full(n, 1000.0),
    })
    # Bar 35: massive volume surge with tiny price displacement
    bars.loc[35, "Close"] = bars.loc[30, "Close"] + 0.1
    bars.loc[35, "High"] = bars.loc[35, "Close"] + 0.1
    bars.loc[35, "Low"] = bars.loc[35, "Close"] - 0.1
    bars.loc[35, "Volume"] = 10000.0

    er = wyckoff_effort_result(bars, lag=5, volume_lookback=20, signed=False)
    assert er.iloc[35] < 0.35, f"Expected ER < 0.35, got {er.iloc[35]}"

    # 3. Signed version retains magnitude and tracks direction
    er_signed = wyckoff_effort_result(bars, lag=5, volume_lookback=20, signed=True)
    np.testing.assert_allclose(er_signed.abs().dropna(), er.dropna())


def test_wyckoff_spring_causal_and_max_penetration():
    """Verify wyckoff_spring causal undercut-reclaim (M4) and black-swan crash filtering."""
    n = 25
    bars = pd.DataFrame({
        "High": np.full(n, 102.0),
        "Low": np.full(n, 100.5),
        "Close": np.full(n, 101.0),
        "Volume": np.full(n, 1000.0),
    })
    support = pd.Series(100.0, index=bars.index)

    # 1. Bar 10 undercut (2% penetration, within [0.5%..4%]) and Bar 11 reclaim on 2x volume
    bars.loc[10, "Low"] = 98.0
    bars.loc[10, "Close"] = 98.5
    bars.loc[11, "Close"] = 101.5
    bars.loc[11, "Volume"] = 2000.0

    res = wyckoff_spring(bars, support, max_penetration=0.04, min_penetration=0.005)
    assert res.iloc[11] >= 1.2, f"Expected spring trigger >= 1.2, got {res.iloc[11]}"
    assert res.iloc[:11].sum() == 0.0

    # 2. Catastrophic crash (-16% penetration) does NOT trigger spring
    bars_crash = bars.copy()
    bars_crash.loc[10, "Low"] = 84.0
    res_crash = wyckoff_spring(bars_crash, support, max_penetration=0.04, min_penetration=0.005)
    assert res_crash.iloc[11] == 0.0

    # 3. Shallow penetration (< 0.5% penetration) does NOT trigger when min_penetration=0.005
    bars_shallow = bars.copy()
    bars_shallow.loc[10, "Low"] = 99.8
    res_shallow = wyckoff_spring(bars_shallow, support, max_penetration=0.04, min_penetration=0.005)
    assert res_shallow.iloc[11] == 0.0

    # 4. Failure to reclaim (Close <= support) does NOT trigger spring
    bars_fail = bars.copy()
    bars_fail.loc[11, "Close"] = 99.5
    res_fail = wyckoff_spring(bars_fail, support, max_penetration=0.04, min_penetration=0.005)
    assert res_fail.iloc[11] == 0.0


def test_fractal_dimension_bounds_and_regimes():
    """Verify fractal_dimension bounds in [1.0, 2.0] and D_trend < D_mean_reversion."""
    np.random.seed(42)
    n = 160

    # Persistent trending series (drift >> noise)
    trending_ret = np.random.normal(0.005, 0.002, n)
    p_trend = pd.Series(100.0 * np.exp(np.cumsum(trending_ret)))

    # Anti-persistent mean-reverting series (alternating increments)
    mr_ret = np.zeros(n)
    for i in range(1, n):
        mr_ret[i] = -0.7 * mr_ret[i - 1] + np.random.normal(0, 0.01)
    p_mr = pd.Series(100.0 * np.exp(np.cumsum(mr_ret)))

    d_trend = fractal_dimension(p_trend, window=60)
    d_mr = fractal_dimension(p_mr, window=60)

    # Verify bounds in [1.0, 2.0]
    valid_d_trend = d_trend.dropna()
    valid_d_mr = d_mr.dropna()
    assert np.all((valid_d_trend >= 1.0) & (valid_d_trend <= 2.0))
    assert np.all((valid_d_mr >= 1.0) & (valid_d_mr <= 2.0))

    # Verify regime differentiation: D_trend < D_mean_reversion
    assert valid_d_trend.mean() < valid_d_mr.mean(), (
        f"Expected trend D ({valid_d_trend.mean()}) < MR D ({valid_d_mr.mean()})"
    )


def test_consolidation_density_interval_overlap():
    """Verify consolidation_density interval overlap (M5: inside=1.0, outside=0.0, partial=0.5)."""
    n = 20
    zd = pd.Series(90.0, index=range(n))
    zg = pd.Series(110.0, index=range(n))

    # 1. Full overlap (bars inside [ZD, ZG])
    bars_in = pd.DataFrame({
        "High": np.full(n, 105.0),
        "Low": np.full(n, 95.0),
        "Close": np.full(n, 100.0),
        "Volume": np.full(n, 1000.0),
    })
    d_in = consolidation_density(bars_in, zd, zg, window=10)
    np.testing.assert_allclose(d_in, 1.0)

    # 2. Zero overlap (bars outside [ZD, ZG])
    bars_out = pd.DataFrame({
        "High": np.full(n, 125.0),
        "Low": np.full(n, 115.0),
        "Close": np.full(n, 120.0),
        "Volume": np.full(n, 1000.0),
    })
    d_out = consolidation_density(bars_out, zd, zg, window=10)
    np.testing.assert_allclose(d_out, 0.0)

    # 3. Partial overlap (50% overlap ratio)
    bars_part = pd.DataFrame({
        "High": np.full(n, 115.0),
        "Low": np.full(n, 105.0),
        "Close": np.full(n, 110.0),
        "Volume": np.full(n, 1000.0),
    })
    d_part = consolidation_density(bars_part, zd, zg, window=10)
    np.testing.assert_allclose(d_part, 0.5)

    # 4. Invalid pivot ZD >= ZG yields 0.0
    d_invalid = consolidation_density(bars_in, zg, zd, window=10)
    np.testing.assert_allclose(d_invalid, 0.0)


def test_cyq_chip_distribution_conservation_and_causality():
    """Verify cyq_chip_distribution bounds, kernels, turnover fallbacks, and strict causality (C1.3)."""
    n_days = 50
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")
    np.random.seed(42)
    closes = 100.0 + np.cumsum(np.random.normal(0, 1, n_days))
    highs = closes + np.random.uniform(0.5, 2.0, n_days)
    lows = closes - np.random.uniform(0.5, 2.0, n_days)
    volumes = np.random.uniform(1000, 5000, n_days)
    daily_bars = pd.DataFrame(
        {"High": highs, "Low": lows, "Close": closes, "Volume": volumes, "Turn": 2.5},
        index=dates,
    )

    # 1. Triangular kernel with turnover column
    wr_tri, cc_tri = cyq_chip_distribution(daily_bars, kernel="triangular", decay_mode="turnover")
    assert np.all((wr_tri >= 0.0) & (wr_tri <= 1.0))
    assert np.all((cc_tri >= 0.0) & (cc_tri <= 1.0))

    # 2. Uniform kernel with relative volume fallback proxy (no Turn column)
    bars_no_turn = daily_bars.drop(columns=["Turn"])
    wr_uni, cc_uni = cyq_chip_distribution(bars_no_turn, kernel="uniform", decay_mode="turnover")
    assert np.all((wr_uni >= 0.0) & (wr_uni <= 1.0))
    assert np.all((cc_uni >= 0.0) & (cc_uni <= 1.0))

    # 3. Intraday 5m kernel
    m5_records = []
    for d in dates[:10]:
        for minute in range(9 * 60 + 30, 15 * 60, 5):
            m5_records.append({
                "datetime": pd.Timestamp(d.year, d.month, d.day, minute // 60, minute % 60),
                "Open": 100.0,
                "High": 101.5,
                "Low": 99.5,
                "Close": 100.5,
                "Volume": 200.0,
            })
    m5_df = pd.DataFrame(m5_records).set_index("datetime")
    wr_5m, cc_5m = cyq_chip_distribution(daily_bars.iloc[:10], intraday_5m_bars=m5_df)
    assert np.all((wr_5m >= 0.0) & (wr_5m <= 1.0))
    assert np.all((cc_5m >= 0.0) & (cc_5m <= 1.0))

    # 4. Strict causality check: cyq_winner_ratio(bars.iloc[:40]) == cyq_winner_ratio(bars).iloc[:40]
    wr_sub = cyq_winner_ratio(daily_bars.iloc[:40], kernel="triangular")
    cc_sub = cyq_cost_concentration(daily_bars.iloc[:40], kernel="triangular")
    np.testing.assert_allclose(wr_tri.iloc[:40], wr_sub, atol=1e-12)
    np.testing.assert_allclose(cc_tri.iloc[:40], cc_sub, atol=1e-12)


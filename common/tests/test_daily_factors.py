"""Unit tests for common/daily_factors.py (Task M3.1).

100% offline / synthetic. The `score_universe` opt-in half of the M3.1 contract
(`use_daily_factors=False` bit-identical, `=True` additive-only) is covered in
`pipeline/instrument_selection/tests/test_scoring.py`, because it needs the
`selectorbot` import path that only that project's conftest sets up.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.daily_factors import (
    ANNUALIZATION_DAYS,
    ivol_residual,
    momentum_1m_1w,
    price_to_vwap_ratio,
    vwap,
    vwap_slope,
)


def _dates(n, start="2020-01-01"):
    return pd.date_range(start, periods=n, freq="B")


def _panel(values, index):
    """Build a single-symbol daily panel from a 1-D sequence."""
    return pd.DataFrame({"A": np.asarray(values, dtype=float)}, index=index)


def _synthetic_panel(n=170, n_symbols=3, seed=7):
    """Deterministic synthetic close/volume panels (random walk + U-shaped volume)."""
    rng = np.random.default_rng(seed)
    idx = _dates(n)
    close = {}
    volume = {}
    for k in range(n_symbols):
        rets = rng.normal(0.0004, 0.011, n)
        close[f"S{k}"] = 100.0 * np.cumprod(1.0 + rets)
        volume[f"S{k}"] = 1e6 * (1.0 + 0.3 * np.sin(np.arange(n) / 7.0 + k))
    return pd.DataFrame(close, index=idx), pd.DataFrame(volume, index=idx)


# ---------------------------------------------------------------------------
# 1. vwap -- hand-computed 3-row example
# ---------------------------------------------------------------------------
def test_vwap_matches_hand_computed_three_row_example():
    idx = _dates(3)
    close = _panel([10.0, 20.0, 30.0], idx)
    volume = _panel([100.0, 300.0, 600.0], idx)

    result = vwap(close, volume, lookback=2)

    # Row 0: window not full -> NaN (never 0.0).
    assert np.isnan(result.iloc[0, 0])
    # Row 1: (10*100 + 20*300) / (100 + 300) = 7000 / 400
    assert result.iloc[1, 0] == pytest.approx(7000.0 / 400.0)
    # Row 2: (20*300 + 30*600) / (300 + 600) = 24000 / 900
    assert result.iloc[2, 0] == pytest.approx(24000.0 / 900.0)


# ---------------------------------------------------------------------------
# 2. price_to_vwap_ratio == close / vwap
# ---------------------------------------------------------------------------
def test_price_to_vwap_ratio_is_close_over_vwap_to_1e12():
    close, volume = _synthetic_panel(n=60, n_symbols=2)
    expected = close / vwap(close, volume, lookback=20)
    got = price_to_vwap_ratio(close, volume, lookback=20)
    pd.testing.assert_frame_equal(got, expected, atol=1e-12, rtol=0.0)


# ---------------------------------------------------------------------------
# 3. vwap_slope -- sign, flat ~0, and /mean normalization
# ---------------------------------------------------------------------------
def test_vwap_slope_positive_on_rising_vwap():
    idx = _dates(40)
    # Strictly rising prices with constant volume -> strictly rising VWAP.
    close = _panel(np.linspace(50.0, 90.0, 40), idx)
    volume = _panel(np.full(40, 1e5), idx)

    slope = vwap_slope(close, volume, lookback=10)

    valid = slope["A"].dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_vwap_slope_is_zero_on_flat_series():
    idx = _dates(40)
    close = _panel(np.full(40, 73.25), idx)
    volume = _panel(np.full(40, 5e4), idx)

    slope = vwap_slope(close, volume, lookback=10)

    valid = slope["A"].dropna()
    assert len(valid) > 0
    assert valid.abs().max() == pytest.approx(0.0, abs=1e-12)


def test_vwap_slope_is_invariant_to_a_common_price_scale():
    # Proves the /mean(vwap) normalization: a raw currency-unit slope would
    # scale with the price level (x3.7), the normalized factor must not.
    close, volume = _synthetic_panel(n=80, n_symbols=2)
    base = vwap_slope(close, volume, lookback=10)
    scaled = vwap_slope(close * 3.7, volume, lookback=10)
    pd.testing.assert_frame_equal(scaled.dropna(), base.dropna(), atol=1e-12, rtol=0.0)


# ---------------------------------------------------------------------------
# 4. ivol_residual
# ---------------------------------------------------------------------------
def test_ivol_is_zero_when_benchmark_is_the_asset():
    idx = _dates(120)
    rng = np.random.default_rng(3)
    close = _panel(100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 120)), idx)
    benchmark = close["A"].pct_change(fill_method=None)

    ivol = ivol_residual(close, lookback=63, benchmark_returns=benchmark)

    valid = ivol["A"].dropna()
    assert len(valid) > 0
    # beta == 1 and residuals == 0 up to float noise.
    assert valid.abs().max() == pytest.approx(0.0, abs=1e-12)


def test_ivol_positive_for_a_noisy_asset():
    idx = _dates(140)
    rng = np.random.default_rng(11)
    bench_ret = pd.Series(rng.normal(0.0, 0.01, 140), index=idx)
    asset_ret = 0.5 * bench_ret.to_numpy() + rng.normal(0.0, 0.02, 140)
    close = _panel(100.0 * np.cumprod(1.0 + asset_ret), idx)

    ivol = ivol_residual(close, lookback=63, benchmark_returns=bench_ret)

    valid = ivol["A"].dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_ivol_annualization_is_sqrt_252_of_the_residual_std():
    idx = _dates(140)
    rng = np.random.default_rng(5)
    bench_ret = pd.Series(rng.normal(0.0, 0.01, 140), index=idx)
    asset_ret = 0.8 * bench_ret.to_numpy() + rng.normal(0.0, 0.015, 140)
    close = _panel(100.0 * np.cumprod(1.0 + asset_ret), idx)

    ivol = ivol_residual(close, lookback=63, benchmark_returns=bench_ret)

    # Hand-recompute the final window with an independent OLS (np.polyfit).
    y = close["A"].pct_change(fill_method=None).iloc[-63:].to_numpy()
    x = bench_ret.iloc[-63:].to_numpy()
    beta, alpha = np.polyfit(x, y, 1)
    residuals = y - (alpha + beta * x)
    expected = np.std(residuals, ddof=1) * np.sqrt(ANNUALIZATION_DAYS)

    assert ANNUALIZATION_DAYS == 252
    assert ivol["A"].iloc[-1] == pytest.approx(expected, rel=1e-10)
    # And the sqrt(252) factor is actually applied (not a raw daily std).
    assert ivol["A"].iloc[-1] / np.std(residuals, ddof=1) == pytest.approx(np.sqrt(252.0), rel=1e-10)


def test_ivol_default_benchmark_is_the_cross_sectional_mean():
    # With benchmark_returns=None the benchmark is the equal-weight mean of the
    # panel's own returns; passing that same series explicitly must be identical.
    close, _ = _synthetic_panel(n=140, n_symbols=4)
    expected_bench = close.pct_change(fill_method=None).mean(axis=1, skipna=True)
    implicit = ivol_residual(close, lookback=63)
    explicit = ivol_residual(close, lookback=63, benchmark_returns=expected_bench)
    pd.testing.assert_frame_equal(implicit, explicit, atol=1e-12, rtol=0.0)


# ---------------------------------------------------------------------------
# 5. momentum_1m_1w
# ---------------------------------------------------------------------------
def test_momentum_1m_1w_matches_hand_computed_series():
    n = 25
    idx = _dates(n)
    close = _panel(100.0 + np.arange(n, dtype=float), idx)  # 100, 101, ..., 124

    mom = momentum_1m_1w(close, skip=5, lookback=20)

    # t=24: close[19] / close[4] - 1 = 119/104 - 1
    assert mom["A"].iloc[24] == pytest.approx(119.0 / 104.0 - 1.0)
    # t=20: close[15] / close[0] - 1 = 115/100 - 1
    assert mom["A"].iloc[20] == pytest.approx(0.15)
    # Rows 0..19 have no t-20 observation.
    assert mom["A"].iloc[:20].isna().all()


# ---------------------------------------------------------------------------
# 6. warmup is NaN, never 0.0
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, build, lookback",
    [
        ("vwap", lambda c, v, lb: vwap(c, v, lb), 10),
        ("price_to_vwap_ratio", lambda c, v, lb: price_to_vwap_ratio(c, v, lb), 10),
        ("vwap_slope", lambda c, v, lb: vwap_slope(c, v, lb), 10),
        ("ivol_residual", lambda c, v, lb: ivol_residual(c, lookback=lb), 10),
        ("momentum_1m_1w", lambda c, v, lb: momentum_1m_1w(c, skip=2, lookback=lb), 10),
    ],
)
def test_warmup_rows_are_nan_and_never_zero(name, build, lookback):
    close, volume = _synthetic_panel(n=60, n_symbols=3)
    result = build(close, volume, lookback)

    head = result.iloc[: lookback - 1]
    assert result.shape[1] == 3
    assert head.isna().all().all(), f"{name}: warmup rows must be NaN"
    # The whole point of the rule: no fabricated 0.0 anywhere in the warmup.
    assert not (head.fillna(np.nan) == 0.0).any().any(), f"{name}: warmup must never emit 0.0"


def test_vwap_slope_disclosed_double_warmup():
    # vwap_slope fits the OLS on a series that is itself VWAP-smoothed with the
    # same lookback: the VWAP series starts at row lb-1 and the fit needs lb
    # points of it, so the first valid slope sits at row 2*lb-2 -- disclosed in
    # the module (longer warmup than the other factors).
    close, volume = _synthetic_panel(n=60, n_symbols=2)
    lb = 10
    slope = vwap_slope(close, volume, lb)
    assert slope.iloc[: 2 * lb - 2].isna().all().all()
    assert slope.iloc[2 * lb - 2].notna().all()


# ---------------------------------------------------------------------------
# 7. causality -- appending future rows must not change history
# ---------------------------------------------------------------------------
def test_appending_future_rows_does_not_change_historical_values():
    close, volume = _synthetic_panel(n=160, n_symbols=3, seed=21)
    cut = 120
    prefix_close, prefix_volume = close.iloc[:cut], volume.iloc[:cut]

    builders = {
        "vwap": lambda c, v: vwap(c, v, 20),
        "price_to_vwap_ratio": lambda c, v: price_to_vwap_ratio(c, v, 20),
        "vwap_slope": lambda c, v: vwap_slope(c, v, 20),
        "ivol_residual": lambda c, v: ivol_residual(c, 63),
        "momentum_1m_1w": lambda c, v: momentum_1m_1w(c, 5, 20),
    }
    for name, build in builders.items():
        full = build(close, volume).iloc[:cut]
        truncated = build(prefix_close, prefix_volume)
        pd.testing.assert_frame_equal(
            full, truncated, atol=1e-12, rtol=0.0, obj=f"{name} leaked future data"
        )


# ---------------------------------------------------------------------------
# 8. degenerate inputs -- NaN, no exception
# ---------------------------------------------------------------------------
def test_single_symbol_panel_degrades_to_nan_where_undefined():
    idx = _dates(100)
    rng = np.random.default_rng(2)
    close = _panel(100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, 100)), idx)
    volume = _panel(np.full(100, 1e6), idx)

    # IVOL vs a cross-section of one is undefined by construction: "residual vs
    # itself" is a fake 0.0 that would rank mid-pack. NaN, explicitly.
    assert ivol_residual(close, lookback=63).isna().all().all()
    # Non-cross-sectional factors still work on a single symbol.
    assert vwap(close, volume, 20).iloc[-1].notna().all()
    assert vwap_slope(close, volume, 20).iloc[-1].notna().all()
    assert momentum_1m_1w(close, 5, 20).iloc[-1].notna().all()


def test_all_nan_window_yields_nan_without_exception():
    idx = _dates(40)
    close = _panel(np.full(40, np.nan), idx)
    volume = _panel(np.full(40, 1e6), idx)

    assert vwap(close, volume, 10).isna().all().all()
    assert price_to_vwap_ratio(close, volume, 10).isna().all().all()
    assert vwap_slope(close, volume, 10).isna().all().all()
    assert ivol_residual(close, lookback=10).isna().all().all()
    assert momentum_1m_1w(close, 2, 10).isna().all().all()

    # A partially-NaN window (one missing print) is also not computable.
    close2 = _panel(np.linspace(10.0, 20.0, 40), idx)
    close2.iloc[25, 0] = np.nan
    v = vwap(close2, volume, 10)
    assert np.isnan(v.iloc[25, 0]) and np.isnan(v.iloc[30, 0]) and np.isfinite(v.iloc[35, 0])


def test_zero_and_missing_volume_windows_yield_nan_not_inf():
    idx = _dates(30)
    close = _panel(np.linspace(10.0, 20.0, 30), idx)
    volume = _panel(np.zeros(30), idx)

    v = vwap(close, volume, 10)
    assert v.isna().all().all()  # 0/0 must be NaN, not inf and not 0.0
    assert not np.isinf(v.to_numpy()).any()
    assert price_to_vwap_ratio(close, volume, 10).isna().all().all()
    assert vwap_slope(close, volume, 10).isna().all().all()
    # Momentum needs no volume at all -- it must still be computable.
    assert momentum_1m_1w(close, 5, 20).iloc[-1].notna().all()


def test_momentum_guards_a_zero_price_base():
    idx = _dates(30)
    values = np.linspace(10.0, 20.0, 30)
    values[5] = 0.0  # corrupt print in the base leg
    mom = momentum_1m_1w(_panel(values, idx), skip=5, lookback=20)
    assert np.isnan(mom["A"].iloc[25])


# ---------------------------------------------------------------------------
# input guards (documented contract, not just an implementation detail)
# ---------------------------------------------------------------------------
def test_non_dataframe_input_is_rejected():
    with pytest.raises(TypeError):
        vwap(pd.Series([1.0, 2.0], index=_dates(2)), pd.Series([1.0, 1.0], index=_dates(2)), 2)


def test_invalid_lookbacks_are_rejected():
    close, volume = _synthetic_panel(n=30, n_symbols=1)
    with pytest.raises(ValueError):
        vwap(close, volume, 0)
    with pytest.raises(ValueError):
        vwap_slope(close, volume, 1)
    with pytest.raises(ValueError):
        momentum_1m_1w(close, skip=20, lookback=20)

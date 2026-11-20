"""Unit tests for amine/labels.py forward-return label construction.

100% offline: uses synthetic price panels only.
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

from amine.labels import build_forward_labels


def _make_close_panel(n_dates: int = 120, n_syms: int = 8, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_dates)
    cols = [f"S{i}" for i in range(n_syms)]
    rets = rng.normal(0.0005, 0.015, size=(n_dates, n_syms))
    prices = 100.0 * np.exp(np.cumsum(rets, axis=0))
    return pd.DataFrame(prices, index=dates, columns=cols)


def test_raw_return_perturbation_horizon_boundaries():
    close = _make_close_panel(n_dates=60, n_syms=6)
    k, lag = 5, 1
    h = k + lag
    base = build_forward_labels(close, holding_period=k, exec_lag=lag, mode="raw_return")

    # Trailing h rows must be strictly NaN
    assert base.iloc[-h:].isna().all().all()

    t = 20
    # Perturbations strictly OUTSIDE [t + lag, t + k + lag] must leave y_t unchanged
    close_outside = close.copy()
    close_outside.iloc[t] *= 1.5
    close_outside.iloc[t - 1] *= 0.8
    close_outside.iloc[t + h + 1] *= 1.4
    out_outside = build_forward_labels(close_outside, holding_period=k, exec_lag=lag, mode="raw_return")
    pd.testing.assert_series_equal(base.iloc[t], out_outside.iloc[t])

    # Perturbation at entry (t + lag) changes y_t
    close_entry = close.copy()
    close_entry.iloc[t + lag] *= 1.15
    out_entry = build_forward_labels(close_entry, holding_period=k, exec_lag=lag, mode="raw_return")
    assert not np.allclose(base.iloc[t].to_numpy(), out_entry.iloc[t].to_numpy())

    # Perturbation at exit (t + k + lag) changes y_t
    close_exit = close.copy()
    close_exit.iloc[t + h] *= 1.15
    out_exit = build_forward_labels(close_exit, holding_period=k, exec_lag=lag, mode="raw_return")
    assert not np.allclose(base.iloc[t].to_numpy(), out_exit.iloc[t].to_numpy())


def test_raw_vs_residual_label_modes():
    close = _make_close_panel(n_dates=140, n_syms=8, seed=11)
    k, lag = 5, 1
    h = k + lag

    raw = build_forward_labels(close, holding_period=k, exec_lag=lag, mode="raw_return")
    mkt = build_forward_labels(close, holding_period=k, exec_lag=lag, mode="market_residual", min_obs=5)
    spec = build_forward_labels(close, holding_period=k, exec_lag=lag, mode="specific_residual", min_obs=5)

    assert raw.shape == mkt.shape == spec.shape == close.shape
    assert mkt.iloc[-h:].isna().all().all()
    assert spec.iloc[-h:].isna().all().all()

    # Non-trivial finite residuals produced on interior dates
    assert mkt.notna().sum().sum() > 0
    assert spec.notna().sum().sum() > 0

    # Residual modes differ from raw returns (common market/style component stripped)
    valid = raw.notna() & mkt.notna() & spec.notna()
    assert valid.sum().sum() > 50
    assert not np.allclose(raw.to_numpy()[valid], mkt.to_numpy()[valid])
    assert not np.allclose(raw.to_numpy()[valid], spec.to_numpy()[valid])


def test_sector_neutral_demeaning():
    close = _make_close_panel(n_dates=40, n_syms=6, seed=19)
    sector_map = {
        "S0": "tech",
        "S1": "tech",
        "S2": "tech",
        "S3": "fin",
        "S4": "fin",
        "S5": "fin",
    }
    neutral = build_forward_labels(
        close,
        holding_period=5,
        exec_lag=1,
        mode="raw_return",
        sector_neutral=True,
        sector_map=sector_map,
        sector_fallback=None,
    )
    valid_rows = neutral.dropna(how="any")
    assert len(valid_rows) > 10
    np.testing.assert_allclose(valid_rows[["S0", "S1", "S2"]].mean(axis=1).to_numpy(), 0.0, atol=1e-12)
    np.testing.assert_allclose(valid_rows[["S3", "S4", "S5"]].mean(axis=1).to_numpy(), 0.0, atol=1e-12)


def test_invalid_args_raise():
    close = _make_close_panel(n_dates=30, n_syms=4)
    with pytest.raises(ValueError):
        build_forward_labels(close, holding_period=0)
    with pytest.raises(ValueError):
        build_forward_labels(close, exec_lag=-1)
    with pytest.raises(ValueError):
        build_forward_labels(close, mode="unknown_mode")


def test_ms8_cross_sectional_rank_label_mode():
    """MS8: cross_sectional_rank label mode maps forward returns to [-1, +1] causally."""
    from common.cross_sectional import cs_rank_gaussian_or_uniform

    close = _make_close_panel(n_dates=60, n_syms=8, seed=23)
    k, lag = 5, 1
    h = k + lag

    raw = build_forward_labels(close, holding_period=k, exec_lag=lag, mode="raw_return")
    ranked = build_forward_labels(
        close, holding_period=k, exec_lag=lag, mode="cross_sectional_rank"
    )

    assert ranked.shape == close.shape
    assert ranked.iloc[-h:].isna().all().all()

    valid_rows = ranked.iloc[:-h]
    vals = valid_rows.to_numpy(dtype=float)
    assert np.isfinite(vals).all()
    assert float(np.min(vals)) >= -0.5 - 1e-12
    assert float(np.max(vals)) <= 0.5 + 1e-12
    np.testing.assert_allclose(valid_rows.mean(axis=1).to_numpy(), 0.0, atol=1e-12)

    # Interior NaN must stay NaN (never fabricated as 0.0 or 0.5)
    close_nan = close.copy()
    close_nan.iloc[15, 0] = np.nan
    ranked_nan = build_forward_labels(
        close_nan, holding_period=k, exec_lag=lag, mode="cross_sectional_rank"
    )
    assert np.isnan(ranked_nan.iloc[15 - lag, 0])

    expected = cs_rank_gaussian_or_uniform(raw, method="centered_uniform")
    pd.testing.assert_frame_equal(ranked, expected)

    # Perturbation strictly outside [t + lag, t + k + lag] leaves row t unchanged
    t = 20
    close_out = close.copy()
    close_out.iloc[t + h + 1] *= 1.8
    ranked_out = build_forward_labels(
        close_out, holding_period=k, exec_lag=lag, mode="cross_sectional_rank"
    )
    pd.testing.assert_series_equal(ranked.iloc[t], ranked_out.iloc[t])

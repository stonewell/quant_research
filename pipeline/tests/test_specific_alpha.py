"""Offline tests for `common/specific_alpha.py` (Task `AT2`).

100% synthetic / offline: every panel comes from
`common.testing.make_ar1_ohlcv`, no network, no real market data.

What is pinned here:
1. the module docstring really declares **SIMPLIFIED** and the per-item gap vs.
   a Barra decomposition, plus the "univariate market residual != multi-factor"
   rule from plan §4.44:1995 -- asserted, not trusted to a code review;
2. the output is *not* `ivol_residual` (signed daily residual returns vs. a
   non-negative annualized volatility level);
3. the stage-S fit has a (near-)zero cross-sectional mean by construction;
4. health thresholds are report labels only -- extreme values change the label
   and nothing else, never raise, never touch the residuals;
5. degenerate inputs (single symbol, short sample, constant returns, all-NaN
   exposure) degrade to NaN / explicit status, never to fabricated zeros.
"""

import inspect
import os
import sys

import numpy as np
import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import common.specific_alpha as specific_alpha  # noqa: E402
from common.daily_factors import ivol_residual  # noqa: E402
from common.specific_alpha import (  # noqa: E402
    market_model_residual_returns,
    specific_alpha_health,
    specific_alpha_residual,
    style_exposures_from_close,
)
from common.testing import make_ar1_ohlcv  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic fixtures (deterministic, offline)
# ---------------------------------------------------------------------------
def _close_panel(n_symbols: int = 8, n: int = 240, seed: int = 0) -> pd.DataFrame:
    columns = {
        f"S{i}": make_ar1_ohlcv(phi=0.15, n=n, seed=seed + i + 1)["Close"]
        for i in range(n_symbols)
    }
    return pd.DataFrame(columns)


def _panels(n_symbols: int = 8, n: int = 240, seed: int = 0):
    close = _close_panel(n_symbols=n_symbols, n=n, seed=seed)
    returns = close.pct_change(fill_method=None)
    exposures = style_exposures_from_close(close)
    return close, returns, exposures


def _multifactor_residuals(n_symbols: int = 8, n: int = 240, seed: int = 0):
    _close, returns, exposures = _panels(n_symbols=n_symbols, n=n, seed=seed)
    return specific_alpha_residual(returns, exposures, min_obs=6)


def _overlap(a: pd.DataFrame, b: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    both = a.notna() & b.notna()
    left = a.where(both).to_numpy(dtype=float)
    right = b.where(both).to_numpy(dtype=float)
    mask = np.isfinite(left) & np.isfinite(right)
    return left[mask], right[mask]


# ---------------------------------------------------------------------------
# 1. disclosure contract (docstrings, not vibes)
# ---------------------------------------------------------------------------
def test_module_docstring_declares_simplified_and_per_item_barra_gaps():
    doc = inspect.getdoc(specific_alpha)
    assert doc is not None

    # The plan row demands the literal SIMPLIFIED marker.
    assert "SIMPLIFIED" in doc
    # ... and that the gap vs. a complete Barra decomposition is enumerated,
    # not waved through. Industry + style + estimation machinery are the three
    # irreducible gaps and each must be named.
    assert "Barra" in doc
    lowered = doc.lower()
    assert "industry" in lowered
    assert "style" in lowered
    assert "covariance" in lowered or "risk-model" in lowered
    # The numbered list must really be a list of differences.
    for marker in ("\n1. ", "\n2. ", "\n3. ", "\n4. ", "\n5. ", "\n6. ", "\n7. "):
        assert marker in doc

    # Plan §4.44:1995, verbatim: 单变量市场残差 != 多因子特质收益分解.
    assert "≠ 多因子" in doc
    assert "不等于 Barra" in doc
    # The ivol relationship must be disclosed in the module docstring itself.
    assert "ivol_residual" in doc
    assert "not interchangeable" in lowered


def test_residual_function_docstring_repeats_the_equivalence_warning():
    doc = inspect.getdoc(specific_alpha_residual)
    assert doc is not None
    assert "SIMPLIFIED" in doc
    assert "NOT a multi-factor" in doc
    assert "advisory" in doc.lower()


# ---------------------------------------------------------------------------
# 2. not `ivol_residual` -- same inputs, different object
# ---------------------------------------------------------------------------
def test_residual_is_not_ivol_residual_although_inputs_are_shared():
    close, returns, exposures = _panels()
    resid = specific_alpha_residual(returns, exposures, min_obs=6)
    ivol = ivol_residual(close, lookback=63)

    left, right = _overlap(resid, ivol)
    assert left.size > 200, "synthetic panels must produce a usable overlap"

    # ivol_residual is a non-negative annualized volatility level ...
    assert (right >= 0.0).all()
    # ... while the specific-alpha residual is a signed *return*.
    assert (left < 0.0).any() and (left > 0.0).any()

    # Not the same numbers, not merely rescaled versions of each other.
    assert not np.allclose(left, right)
    assert float(np.abs(left - right).max()) > 1e-3
    # Scale sanity: daily residuals are O(1%) once annualized vol is O(10%).
    assert float(np.median(np.abs(left))) < 0.1
    assert float(np.median(right)) > 0.01

    # The health report does not pretend the two are the same statistic.
    health = specific_alpha_health(resid)
    assert health["simplified"] is True
    assert "SIMPLIFIED_FACTOR_MODEL" in health["labels"]


def test_univariate_market_residual_differs_from_the_multifactor_leftover():
    _close, returns, exposures = _panels()
    market_only = specific_alpha_residual(returns, None, min_obs=6)
    multifactor = specific_alpha_residual(returns, exposures, min_obs=6)

    left, right = _overlap(market_only, multifactor)
    assert left.size > 200
    # A single market exposure cannot reproduce the style-adjusted leftover
    # (plan §4.44:1995), so the two call shapes must not coincide.
    assert not np.allclose(left, right)
    assert float(np.abs(left - right).max()) > 1e-6


# ---------------------------------------------------------------------------
# 3. stage-S identity: cross-sectional mean is (near) zero
# ---------------------------------------------------------------------------
def test_cross_sectional_mean_of_residuals_is_near_zero():
    resid = _multifactor_residuals()
    row_means = resid.mean(axis=1).dropna()
    assert len(row_means) > 50
    assert float(row_means.abs().max()) < 1e-9

    health = specific_alpha_health(resid)
    assert abs(health["mean_residual"]) < 1e-9
    # ... and the pooled mean is small enough not to trip the diagnostic label.
    assert "NONZERO_POOLED_MEAN" not in health["labels"]


# ---------------------------------------------------------------------------
# 4. thresholds are report labels, never gates
# ---------------------------------------------------------------------------
_ALLOWED_STATUSES = {
    "OK",
    "LOW_COVERAGE",
    "INSUFFICIENT_HISTORY",
    "NO_RESIDUALS",
    "INVALID_INPUT",
}


def test_thresholds_change_labels_only_and_never_touch_the_residuals():
    resid = _multifactor_residuals()
    frozen = resid.copy(deep=True)

    base = specific_alpha_health(resid)
    ridiculously_permissive = specific_alpha_health(
        resid,
        coverage_label_threshold=-5.0,
        dispersion_label_threshold=-1.0,
        min_label_dates=-100,
    )
    ridiculously_strict = specific_alpha_health(
        resid,
        coverage_label_threshold=1e9,
        dispersion_label_threshold=1e9,
        min_label_dates=10**9,
    )

    for report in (base, ridiculously_permissive, ridiculously_strict):
        assert set(report) == set(base)
        assert report["simplified"] is True
        assert report["advisory"] is True
        assert report["status"] in _ALLOWED_STATUSES
        assert isinstance(report["labels"], list)
        assert np.isclose(report["coverage"], base["coverage"])
        assert np.isclose(report["mean_residual"], base["mean_residual"])
        assert np.isclose(report["residual_vol_annualized"], base["residual_vol_annualized"])

    # Permissive thresholds can only upgrade the label (never raise, never
    # change the numbers); strict ones downgrade it.
    assert ridiculously_permissive["status"] == "OK"
    assert "HIGH_DISPERSION" in ridiculously_permissive["labels"]
    assert ridiculously_strict["status"] in ("INSUFFICIENT_HISTORY", "LOW_COVERAGE")
    # Nothing was mutated by any of the three calls.
    pd.testing.assert_frame_equal(resid, frozen)


def test_health_never_raises_on_garbage_input():
    # Wrong type -> explicit status, not an exception escaping to a report path.
    report = specific_alpha_health([1.0, 2.0, 3.0])
    assert report["status"] == "INVALID_INPUT"
    assert report["advisory"] is True
    assert set(report) >= {"simplified", "labels", "coverage"}

    empty = specific_alpha_health(pd.DataFrame())
    assert empty["status"] == "NO_RESIDUALS"
    assert empty["coverage"] == 0.0


# ---------------------------------------------------------------------------
# 5. deterministic degradation
# ---------------------------------------------------------------------------
def test_single_symbol_degrades_to_nan_without_raising():
    close = pd.DataFrame({"ONLY": make_ar1_ohlcv(phi=0.2, n=160, seed=11)["Close"]})
    returns = close.pct_change(fill_method=None)
    exposures = style_exposures_from_close(close)

    out = specific_alpha_residual(returns, exposures)
    assert out.shape == returns.shape
    assert out.isna().all().all()

    market_only = market_model_residual_returns(returns)
    assert market_only.isna().all().all()

    report = specific_alpha_health(out)
    assert report["status"] == "NO_RESIDUALS"
    assert report["simplified"] is True


def test_short_sample_degrades_to_nan_without_raising():
    # 12 rows cannot fill a 63-day beta window, so no row may carry a value.
    close, returns, exposures = _panels(n=12)
    out = specific_alpha_residual(returns, exposures, min_obs=3)
    assert out.isna().all().all()

    report = specific_alpha_health(out)
    assert report["status"] == "NO_RESIDUALS"


def test_constant_returns_degrade_to_nan_not_fabricated_zeros():
    index = pd.bdate_range("2022-01-03", periods=120)
    returns = pd.DataFrame(0.001, index=index, columns=["A", "B", "C", "D", "E", "F"])

    out = specific_alpha_residual(returns, min_obs=4)
    assert out.isna().all().all(), "a degenerate regression must not emit 0.0 residuals"

    report = specific_alpha_health(out)
    assert report["status"] == "NO_RESIDUALS"


def test_all_nan_exposure_column_yields_nan_not_partial_fit():
    _close, returns, exposures = _panels()
    poisoned = dict(exposures)
    poisoned["poisoned"] = pd.DataFrame(
        np.nan, index=returns.index, columns=returns.columns
    )
    out = specific_alpha_residual(returns, poisoned, min_obs=6)
    # "Every exposure must be finite" is a per-date requirement: a single
    # all-NaN exposure removes the whole basis, it is never silently dropped
    # down to the market-only fit.
    assert out.isna().all().all()

    # A partially-NaN exposure still yields values only on the dates where the
    # full basis exists -- never on back-filled/zero-filled rows.
    partial = dict(exposures)
    mask = pd.DataFrame(False, index=returns.index, columns=returns.columns)
    mask.iloc[:120, :] = True
    partial["late_start"] = exposures["momentum_1m_1w"].where(~mask)
    partial_out = specific_alpha_residual(returns, partial, min_obs=6)
    finite_rows = partial_out.notna().any(axis=1)
    assert not finite_rows.iloc[:120].any()
    assert finite_rows.iloc[120:].any()

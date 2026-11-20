"""Unit tests for common/allocation_templates.py."""

import os
import sys

import numpy as np
import pandas as pd

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_templates import (
    MaxDiversificationAllocation,
    _inverse_vol_weights,
    _sparse_from_daily,
    apply_asset_inertia,
)
from common.testing import make_ohlcv_from_closes as make_df


def test_inverse_vol_weights_normal_case_sums_to_scale():
    vols = pd.Series({"A": 1.0, "B": 2.0, "C": 4.0})
    weights = _inverse_vol_weights(vols)
    np.testing.assert_allclose(weights.sum(), 1.0)
    # Lower vol -> higher weight.
    assert weights["A"] > weights["B"] > weights["C"]


def test_inverse_vol_weights_partial_fill_scale():
    vols = pd.Series({"A": 1.0, "B": 1.0})
    weights = _inverse_vol_weights(vols, scale=0.5)
    np.testing.assert_allclose(weights.sum(), 0.5)


def test_inverse_vol_weights_zero_vol_treated_as_missing():
    vols = pd.Series({"A": 0.0, "B": 2.0})
    weights = _inverse_vol_weights(vols)
    assert np.isnan(weights["A"])
    np.testing.assert_allclose(weights["B"], 1.0)


def test_inverse_vol_weights_all_invalid_on_invalid_nan():
    vols = pd.Series({"A": 0.0, "B": np.nan})
    weights = _inverse_vol_weights(vols, on_invalid="nan")
    assert weights.isna().all()


def test_inverse_vol_weights_all_invalid_on_invalid_zero():
    vols = pd.Series({"A": 0.0, "B": np.nan})
    weights = _inverse_vol_weights(vols, on_invalid="zero")
    assert (weights == 0.0).all()


def test_max_diversification_excludes_a_symbol_with_no_data_in_window():
    # Regression test: unlike its sibling covariance templates
    # (HierarchicalRiskParityAllocation, MinimumVarianceAllocation),
    # MaxDiversificationAllocation used to NOT filter to symbols with a full
    # lookback return history before computing correlations/volatilities. A
    # symbol that hasn't started trading yet (all-NaN Close for the whole
    # lookback window) would have its correlation `fillna(0)`'d to look
    # maximally diversifying, and its own weight would come out NaN --
    # breaking the row's weight-sum invariant. It must instead be excluded
    # entirely (left NaN in this sparse frame, later filled to 0.0 by the
    # backtester). All synthetic data, no network/market data involved.
    idx = pd.bdate_range("2020-01-01", periods=100)
    rng = np.random.default_rng(7)
    closes_a = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    closes_b = 100 + np.cumsum(rng.normal(0, 1.0, 100))

    # C has no price history at all for the first 90 bars (NaN Close) --
    # every 60-day lookback window before bar 90 is entirely NaN for C.
    closes_c = np.full(100, np.nan)
    closes_c[90:] = 100 + np.cumsum(rng.normal(0, 1.0, 10))

    universe = {
        "A": make_df(closes_a, start="2020-01-01"),
        "B": make_df(closes_b, start="2020-01-01"),
        "C": pd.DataFrame({"Close": closes_c}, index=idx),
    }

    template = MaxDiversificationAllocation()
    weights = template.generate_weights(universe, {"vol_lookback": 60, "rebalance_freq_days": 10})

    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty

    # Every rebalance date before C has any data must NOT allocate to C --
    # it should be excluded (NaN in this sparse frame), not leak a NaN
    # weight into A/B's row via a maximally-diversifying fillna(0) correlation.
    early_rows = rebalance_rows[rebalance_rows.index < idx[90]]
    assert not early_rows.empty
    assert early_rows["C"].isna().all()

    # No NaN leaks into A or B's weights, and they sum to 1.0 -- properly
    # renormalized among just the symbols that actually had data, not
    # diluted (or NaN-poisoned) by a phantom C weight.
    assert not early_rows["A"].isna().any()
    assert not early_rows["B"].isna().any()
    np.testing.assert_allclose(early_rows["A"] + early_rows["B"], 1.0, atol=1e-9)


def test_apply_asset_inertia_freezes_small_changes():
    dates = pd.bdate_range("2023-01-01", periods=6)
    df = pd.DataFrame(
        {
            "A": [0.10, 0.105, 0.108, 0.20, 0.20, 0.05],
            "B": [0.20, 0.201, 0.199, 0.198, 0.10, 0.10],
            "CASH": [0.70, 0.694, 0.693, 0.602, 0.70, 0.85],
        },
        index=dates,
    )
    filtered = apply_asset_inertia(df, min_weight_change=0.02, cash_proxy="CASH")

    # Day 1: A and B changed < 0.02, must freeze at day 0 weights
    np.testing.assert_allclose(filtered.loc[dates[1], "A"], 0.10)
    np.testing.assert_allclose(filtered.loc[dates[1], "B"], 0.20)
    np.testing.assert_allclose(filtered.loc[dates[1], "CASH"], 0.70)

    # Day 3: A jumped to 0.20 (>= 0.02), B changed by -0.002 (< 0.02)
    # A is updated, B remains frozen at 0.20, CASH absorbs remainder
    np.testing.assert_allclose(filtered.loc[dates[3], "A"], 0.20)
    np.testing.assert_allclose(filtered.loc[dates[3], "B"], 0.20)
    np.testing.assert_allclose(filtered.loc[dates[3], "CASH"], 0.60)

    # Sparse conversion should drop day 1 and 2
    sparse = _sparse_from_daily(df, min_weight_change=0.02, cash_proxy="CASH")
    rebal_dates = sparse.dropna(how="all").index
    assert dates[1] not in rebal_dates
    assert dates[2] not in rebal_dates
    assert dates[3] in rebal_dates


def test_apply_asset_inertia_emergency_override():
    dates = pd.bdate_range("2023-01-01", periods=3)
    df = pd.DataFrame(
        {
            "A": [0.20, 0.195, 0.0],
            "CASH": [0.80, 0.805, 1.0],
        },
        index=dates,
    )
    emergency_mask = pd.Series([False, True, True], index=dates)
    filtered = apply_asset_inertia(df, min_weight_change=0.02, cash_proxy="CASH", emergency_mask=emergency_mask)

    # On day 1, diff is -0.005 (< 0.02), but emergency_mask is True -> must update!
    np.testing.assert_allclose(filtered.loc[dates[1], "A"], 0.195)
    np.testing.assert_allclose(filtered.loc[dates[1], "CASH"], 0.805)


def test_apply_asset_inertia_asymmetric_sells_ratio_anchor():
    """Verify asymmetric_sell_ratio parameterization and default 0.60 backward compatibility."""
    dates = pd.bdate_range("2023-01-01", periods=4)
    # Asset A starts at 0.50.
    # Day 1: drops to 0.43 (diff = -0.07). With thresh=0.10:
    #   ratio=0.60 -> sell_thresh = 0.06 -> diff <= -0.06 -> sells! (weight = 0.43)
    #   ratio=0.80 -> sell_thresh = 0.08 -> diff > -0.08 -> suppressed! (weight = 0.50)
    df1 = pd.DataFrame(
        {
            "A": [0.50, 0.43, 0.43, 0.43],
            "CASH": [0.50, 0.57, 0.57, 0.57],
        },
        index=dates,
    )
    # Default ratio (0.60) vs explicit ratio=0.60 parity
    res_default = apply_asset_inertia(df1, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=True)
    res_explicit_06 = apply_asset_inertia(df1, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=True, asymmetric_sell_ratio=0.60)
    pd.testing.assert_frame_equal(res_default, res_explicit_06)
    # Day 1 sells because diff (-0.07) <= -0.06
    np.testing.assert_allclose(res_default.loc[dates[1], "A"], 0.43)

    # With ratio=0.80, sell_thresh is 0.08, so diff=-0.07 does not trigger sell
    res_08 = apply_asset_inertia(df1, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=True, asymmetric_sell_ratio=0.80)
    np.testing.assert_allclose(res_08.loc[dates[1], "A"], 0.50)

    # df2 for ratio=0.40 test:
    df2 = pd.DataFrame(
        {
            "A": [0.50, 0.45, 0.45, 0.45],
            "CASH": [0.50, 0.55, 0.55, 0.55],
        },
        index=dates,
    )
    # With ratio=0.60, diff=-0.05 > -0.06 -> suppressed (0.50)
    res_06_df2 = apply_asset_inertia(df2, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=True, asymmetric_sell_ratio=0.60)
    np.testing.assert_allclose(res_06_df2.loc[dates[1], "A"], 0.50)

    # With ratio=0.40, sell_thresh is 0.04, diff=-0.05 <= -0.04 -> sells (0.45)
    res_04_df2 = apply_asset_inertia(df2, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=True, asymmetric_sell_ratio=0.40)
    np.testing.assert_allclose(res_04_df2.loc[dates[1], "A"], 0.45)

    # When asymmetric_sells=False, asymmetric_sell_ratio has no effect (thresh stays 0.10)
    res_sym = apply_asset_inertia(df1, min_weight_change=0.10, cash_proxy="CASH", asymmetric_sells=False, asymmetric_sell_ratio=0.40)
    np.testing.assert_allclose(res_sym.loc[dates[1], "A"], 0.50)


# =============================================================================
# MS6: L1.5 Impact-Penalized SLSQP Solver & Robust Covariance Integration
# =============================================================================


def _ms6_synthetic_universe(n_days: int = 120, n_assets: int = 6, seed: int = 42):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    universe = {}
    for i in range(n_assets):
        rets = rng.normal(0.0005, 0.015, size=n_days)
        close = 100.0 * np.exp(np.cumsum(rets))
        universe[f"A{i}"] = pd.DataFrame(
            {"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": 1000.0},
            index=dates,
        )
    return universe


def test_ms6_min_var_and_hrp_default_off_bit_identical_and_versions_bumped():
    from common.allocation_templates import (
        HierarchicalRiskParityAllocation,
        MinimumVarianceAllocation,
    )

    mv = MinimumVarianceAllocation()
    hrp = HierarchicalRiskParityAllocation()
    assert mv.version == "1.2.0"
    assert hrp.version == "1.1.0"
    assert "min_var_robust_cov" not in mv.param_grid
    assert "hrp_robust_cov" not in hrp.param_grid

    univ = _ms6_synthetic_universe()
    p_base = {"cov_lookback": 60, "rebalance_freq_days": 21}

    w_mv_default = mv.generate_weights(univ, p_base)
    w_mv_off = mv.generate_weights(univ, {**p_base, "min_var_robust_cov": False})
    assert w_mv_default.isna().equals(w_mv_off.isna())
    np.testing.assert_array_equal(
        w_mv_default.dropna().to_numpy(), w_mv_off.dropna().to_numpy()
    )

    w_hrp_default = hrp.generate_weights(univ, p_base)
    w_hrp_off = hrp.generate_weights(univ, {**p_base, "hrp_robust_cov": False})
    assert w_hrp_default.isna().equals(w_hrp_off.isna())
    np.testing.assert_array_equal(
        w_hrp_default.dropna().to_numpy(), w_hrp_off.dropna().to_numpy()
    )


def test_ms6_robust_cov_eliminates_singular_and_hrp_linkage_degeneracy():
    from unittest.mock import patch

    import common.allocation_templates as at_mod
    from common.allocation_templates import (
        HierarchicalRiskParityAllocation,
        MinimumVarianceAllocation,
    )

    # Pathological universe: duplicate collinear columns + short lookback (N > T)
    rng = np.random.default_rng(99)
    dates = pd.bdate_range("2024-01-02", periods=60)
    base_close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.015, size=60)))
    univ = {}
    for i in range(15):
        c = base_close.copy() if i < 5 else 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.015, size=60)))
        univ[f"S{i:02d}"] = pd.DataFrame(
            {"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1000.0},
            index=dates,
        )

    mv = MinimumVarianceAllocation()
    hrp = HierarchicalRiskParityAllocation()
    params_mv = {"cov_lookback": 10, "rebalance_freq_days": 15, "min_var_robust_cov": True}
    params_hrp = {"cov_lookback": 10, "rebalance_freq_days": 15, "hrp_robust_cov": True}

    with patch.object(at_mod, "denoise_covariance", wraps=at_mod.denoise_covariance) as spy_denoise:
        w_mv = mv.generate_weights(univ, params_mv)
        w_hrp = hrp.generate_weights(univ, params_hrp)
        # Mutually exclusive: denoise_covariance must NOT be called when *_robust_cov=True
        assert spy_denoise.call_count == 0

    for w_df in (w_mv, w_hrp):
        rebal_rows = w_df.dropna(how="all")
        assert len(rebal_rows) > 0
        assert np.isfinite(rebal_rows.to_numpy()).all()
        assert (rebal_rows.to_numpy() >= -1e-12).all()
        np.testing.assert_allclose(rebal_rows.sum(axis=1).to_numpy(), 1.0, atol=1e-6)


def test_ms6_alpha_impact_penalized_weights_degenerates_to_turnover_penalized():
    from common.allocation_templates import (
        _alpha_impact_penalized_weights,
        _turnover_penalized_weights,
    )

    rng = np.random.default_rng(7)
    raw = rng.normal(0.0, 0.02, size=(60, 6))
    cov = np.cov(raw, rowvar=False)
    w0 = np.array([0.25, 0.25, 0.20, 0.15, 0.10, 0.05])
    groups = [np.array([0, 1, 2]), np.array([3, 4, 5])]

    for tp in (0.0, 0.005, 0.02):
        for cash in (0.0, 0.15):
            w_l1 = _turnover_penalized_weights(
                cov, w0, turnover_penalty=tp, single_cap=0.30,
                sector_groups=groups, sector_dev=0.05, cash_allowance=cash,
            )
            w_ms6 = _alpha_impact_penalized_weights(
                cov, w0, alpha=None, risk_aversion=2.0, turnover_penalty=tp,
                impact_penalty=0.0, single_cap=0.30,
                sector_groups=groups, sector_dev=0.05, cash_allowance=cash,
            )
            np.testing.assert_allclose(w_ms6, w_l1, rtol=1e-7, atol=1e-7)


def test_ms6_alpha_impact_penalized_weights_suppresses_low_adv_high_vol_turnover():
    from common.allocation_templates import (
        _alpha_impact_penalized_weights,
        compute_almgren_chriss_impact_coefficients,
    )

    cov = np.eye(4) * 0.0004
    w0 = np.zeros(4)
    alpha = np.full(4, 0.02)
    # Asset 0: illiquid high vol; Asset 1: liquid low vol; Assets 2 & 3: medium
    vols = np.array([0.04, 0.01, 0.02, 0.02])
    advs = np.array([1e5, 1e8, 1e6, 1e6])
    kappas = compute_almgren_chriss_impact_coefficients(vols, advs)
    assert kappas[0] > kappas[1] * 50.0

    w_no_imp = _alpha_impact_penalized_weights(
        cov, w0, alpha=alpha, turnover_penalty=0.0, impact_penalty=0.0,
        impact_kappas=kappas, single_cap=0.50,
    )
    w_imp = _alpha_impact_penalized_weights(
        cov, w0, alpha=alpha, turnover_penalty=0.0, impact_penalty=0.005,
        impact_kappas=kappas, single_cap=0.50,
    )
    assert w_imp[0] < w_imp[1]
    assert w_imp[0] < w_no_imp[0]


def test_ms6_alpha_impact_cash_box_and_fallback_ladder():
    from unittest.mock import patch

    import pytest
    import scipy.optimize

    from common.allocation_templates import _alpha_impact_penalized_weights

    cov = np.eye(4) * 0.0004
    w0 = np.array([0.25, 0.25, 0.25, 0.25])
    alpha = np.array([0.03, 0.01, -0.01, -0.02])

    # Cap-constrained cash residual: 4 * 0.20 = 0.80 < 1.0 -> sum(w) == 0.80
    w_cap = _alpha_impact_penalized_weights(
        cov, w0, alpha=alpha, impact_penalty=0.002, single_cap=0.20, cash_allowance=0.10
    )
    assert np.all(w_cap >= -1e-9) and np.all(w_cap <= 0.20 + 1e-9)
    assert w_cap.sum() == pytest.approx(0.80, abs=1e-6)

    # Cash-box bounds when cap can fund full book
    w_box = _alpha_impact_penalized_weights(
        cov, w0, alpha=alpha, impact_penalty=0.002, single_cap=0.40, cash_allowance=0.25
    )
    assert np.all(w_box >= -1e-9) and np.all(w_box <= 0.40 + 1e-9)
    assert 0.75 - 1e-6 <= w_box.sum() <= 1.0 + 1e-6

    # Monkeypatch scipy.optimize.minimize to fail -> degrades cleanly down fallback ladder
    with patch.object(scipy.optimize, "minimize", side_effect=RuntimeError("forced slsqp fail")):
        w_fb = _alpha_impact_penalized_weights(
            cov, w0, alpha=alpha, impact_penalty=0.005, single_cap=0.40, cash_allowance=0.0
        )
    assert np.isfinite(w_fb).all()
    assert w_fb.sum() == pytest.approx(1.0, abs=1e-6)



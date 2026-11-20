"""Unit tests for canonical cross-sectional factor primitives (Track MS, MS1).

All tests run 100% offline on seeded synthetic NumPy/Pandas panels.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest
from scipy.special import ndtri

from common.alpha101_operators import cs_rank
from common.cross_sectional import (
    MAD_TO_SIGMA,
    MEANAD_TO_SIGMA,
    MODE_CONTINUOUS_ONLY,
    MODE_FULL,
    MODE_INSUFFICIENT,
    MODE_ZSCORE,
    cs_lowdin_orthogonalize,
    cs_mad_winsorize,
    cs_neutralize_ols,
    cs_rank_gaussian_or_uniform,
)


def _make_dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2025-01-02", periods=n)


# =============================================================================
# 1. cs_mad_winsorize
# =============================================================================


class TestCsMadWinsorize:
    def test_outlier_clipped_exact_and_inbounds_bit_identical(self):
        vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 100.0], dtype=np.float64)
        df = pd.DataFrame([vals], index=_make_dates(1), columns=[f"S{i}" for i in range(6)])
        out = cs_mad_winsorize(df, n_mad=3.0)

        med = np.median(vals)  # 3.5
        mad = np.median(np.abs(vals - med))  # 1.5
        expected_hi = med + 3.0 * MAD_TO_SIGMA * mad

        assert out.iloc[0, 5] == pytest.approx(expected_hi, rel=0.0, abs=1e-12)
        # In-bound values must be bit-identical
        np.testing.assert_array_equal(out.iloc[0, :5].to_numpy(), vals[:5])

    def test_manual_bounds_multiple_rows(self):
        rng = np.random.default_rng(42)
        data = rng.normal(0.0, 1.0, size=(5, 20))
        data[:, 0] = 50.0
        data[:, 1] = -50.0
        df = pd.DataFrame(data, index=_make_dates(5), columns=[f"A{i}" for i in range(20)])

        out = cs_mad_winsorize(df, n_mad=2.5)
        for t in range(5):
            row = data[t]
            med = np.median(row)
            mad = np.median(np.abs(row - med))
            scale = MAD_TO_SIGMA * mad
            lo, hi = med - 2.5 * scale, med + 2.5 * scale
            assert out.iloc[t, 0] == pytest.approx(hi, rel=0.0, abs=1e-12)
            assert out.iloc[t, 1] == pytest.approx(lo, rel=0.0, abs=1e-12)
            assert (out.iloc[t] <= hi + 1e-12).all()
            assert (out.iloc[t] >= lo - 1e-12).all()

    def test_mad_zero_uses_meanad_fallback_no_nan(self):
        # >50% tied at 1.0 => MAD == 0, MeanAD > 0
        vals = np.array([1.0, 1.0, 1.0, 1.0, 20.0], dtype=np.float64)
        df = pd.DataFrame([vals], index=_make_dates(1), columns=[f"S{i}" for i in range(5)])
        out = cs_mad_winsorize(df, n_mad=3.0)

        med = 1.0
        mean_ad = np.mean(np.abs(vals - med))
        expected_hi = med + 3.0 * MEANAD_TO_SIGMA * mean_ad

        assert out.notna().all().all()
        assert out.iloc[0, 4] == pytest.approx(expected_hi, rel=0.0, abs=1e-12)
        np.testing.assert_array_equal(out.iloc[0, :4].to_numpy(), vals[:4])

    def test_all_equal_row_unchanged(self):
        df = pd.DataFrame(
            [[7.5, 7.5, 7.5, 7.5]],
            index=_make_dates(1),
            columns=["A", "B", "C", "D"],
        )
        out = cs_mad_winsorize(df, n_mad=3.0)
        pd.testing.assert_frame_equal(out, df)

    def test_nan_all_nan_preserved_and_inf_clipped(self):
        df = pd.DataFrame(
            [
                [1.0, 2.0, 3.0, 4.0, np.nan, np.inf, -np.inf],
                [np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan],
            ],
            index=_make_dates(2),
            columns=[f"S{i}" for i in range(7)],
        )
        out = cs_mad_winsorize(df, n_mad=3.0)

        finite_row0 = np.array([1.0, 2.0, 3.0, 4.0])
        med = np.median(finite_row0)
        mad = np.median(np.abs(finite_row0 - med))
        hi = med + 3.0 * MAD_TO_SIGMA * mad
        lo = med - 3.0 * MAD_TO_SIGMA * mad

        assert np.isnan(out.iloc[0, 4])
        assert out.iloc[0, 5] == pytest.approx(hi, rel=0.0, abs=1e-12)
        assert out.iloc[0, 6] == pytest.approx(lo, rel=0.0, abs=1e-12)
        assert out.iloc[1].isna().all()

    def test_series_roundtrip_and_invalid_n_mad(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 50.0], index=["A", "B", "C", "D", "E"], name="mom")
        out = cs_mad_winsorize(s, n_mad=3.0)
        assert isinstance(out, pd.Series)
        assert out.name == "mom"
        assert out.index.equals(s.index)
        assert out.dtype == np.float64
        assert out.loc["E"] < 50.0

        with pytest.raises(ValueError):
            cs_mad_winsorize(s, n_mad=0.0)
        with pytest.raises(ValueError):
            cs_mad_winsorize(s, n_mad=-1.5)


# =============================================================================
# 2. cs_rank_gaussian_or_uniform
# =============================================================================


class TestCsRankGaussianOrUniform:
    def test_exact_uniform_n5(self):
        s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0], index=["A", "B", "C", "D", "E"], name="f")
        u = cs_rank_gaussian_or_uniform(s, method="unit_uniform")
        c = cs_rank_gaussian_or_uniform(s, method="centered_uniform")

        np.testing.assert_allclose(u.to_numpy(), [0.0, 0.25, 0.5, 0.75, 1.0], atol=1e-12)
        np.testing.assert_allclose(c.to_numpy(), [-0.5, -0.25, 0.0, 0.25, 0.5], atol=1e-12)
        assert c.sum() == pytest.approx(0.0, abs=1e-12)
        assert isinstance(u, pd.Series) and u.name == "f"

    def test_ties_average_rank(self):
        s = pd.Series([1.0, 1.0, 2.0], index=["A", "B", "C"])
        u = cs_rank_gaussian_or_uniform(s, method="unit_uniform")
        np.testing.assert_allclose(u.to_numpy(), [0.25, 0.25, 1.0], atol=1e-12)

    def test_gaussian_exact_finite_and_symmetric(self):
        n = 10_000
        rng = np.random.default_rng(7)
        s = pd.Series(rng.normal(size=n), index=[f"S{i}" for i in range(n)])
        g = cs_rank_gaussian_or_uniform(s, method="gaussian")
        g_alias = cs_rank_gaussian_or_uniform(s, method="normal")

        pd.testing.assert_series_equal(g, g_alias)
        assert np.isfinite(g.to_numpy()).all()

        # Verify exact match to ndtri(r / (n + 1)) and symmetry
        ranks = s.rank(method="average").to_numpy()
        expected = ndtri(ranks / (n + 1.0))
        np.testing.assert_allclose(g.to_numpy(), expected, atol=1e-12)
        assert g.mean() == pytest.approx(0.0, abs=1e-12)
        assert (g.max() + g.min()) == pytest.approx(0.0, abs=1e-12)

    def test_nan_masked_and_single_valid_row_nan(self):
        df = pd.DataFrame(
            [
                [1.0, np.nan, 3.0, 2.0],
                [np.nan, 5.0, np.nan, np.nan],
            ],
            index=_make_dates(2),
            columns=["A", "B", "C", "D"],
        )
        out = cs_rank_gaussian_or_uniform(df, method="centered_uniform")
        assert np.isnan(out.iloc[0, 1])  # NOT 0.5
        np.testing.assert_allclose(
            out.iloc[0, [0, 2, 3]].to_numpy(), [-0.5, 0.5, 0.0], atol=1e-12
        )
        assert out.iloc[1].isna().all()

    def test_unit_matches_cs_rank_formula_on_full_panel(self):
        rng = np.random.default_rng(11)
        df = pd.DataFrame(rng.normal(size=(10, 15)), index=_make_dates(10), columns=[f"S{i}" for i in range(15)])
        u = cs_rank_gaussian_or_uniform(df, method="unit_uniform")
        n = df.shape[1]
        expected = (cs_rank(df) * n - 1.0) / (n - 1.0)
        np.testing.assert_allclose(u.to_numpy(), expected.to_numpy(), atol=1e-12)

    def test_invalid_method_and_min_valid_raise(self):
        s = pd.Series([1.0, 2.0, 3.0])
        with pytest.raises(ValueError):
            cs_rank_gaussian_or_uniform(s, method="invalid")
        with pytest.raises(ValueError):
            cs_rank_gaussian_or_uniform(s, min_valid=1)


# =============================================================================
# 3. cs_neutralize_ols
# =============================================================================


class TestCsNeutralizeOls:
    def test_full_mode_n60_orthogonal_and_sector_means_zero(self):
        rng = np.random.default_rng(101)
        t_len, n_assets = 30, 60
        dates = _make_dates(t_len)
        cols = [f"S{i:02d}" for i in range(n_assets)]
        sectors = {c: f"SEC_{i % 5}" for i, c in enumerate(cols)}

        e1 = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
        e2 = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
        e3 = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
        sec_shift = np.array([float(i % 5) * 1.5 for i in range(n_assets)])
        factor = 2.0 * e1 - 1.5 * e2 + 0.7 * e3 + sec_shift + rng.normal(0.0, 0.5, size=(t_len, n_assets))

        resid, diag = cs_neutralize_ols(
            factor,
            {"size": e1, "beta": e2, "vol": e3},
            sectors=sectors,
            return_diagnostics=True,
        )

        assert (diag["mode"] == MODE_FULL).all()
        assert (diag["k_regressors"] == 3 + 4).all()

        for t in range(t_len):
            e_vec = resid.iloc[t].to_numpy()
            assert np.std(e_vec, ddof=1) == pytest.approx(1.0, abs=1e-10)
            for exp_df in (e1, e2, e3):
                assert abs(float(np.dot(exp_df.iloc[t].to_numpy(), e_vec))) < 1e-10
            for sec_id in range(5):
                sec_cols = [c for c in cols if sectors[c] == f"SEC_{sec_id}"]
                assert abs(float(resid.iloc[t][sec_cols].mean())) < 1e-10

    def test_continuous_only_n20_strips_dummies(self):
        rng = np.random.default_rng(202)
        t_len, n_assets = 10, 20
        dates = _make_dates(t_len)
        cols = [f"S{i:02d}" for i in range(n_assets)]
        sectors = {c: f"SEC_{i % 6}" for i, c in enumerate(cols)}

        e1 = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
        e2 = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
        # Strong sector signal that will survive if dummies are stripped
        sec_shift = np.array([float(i % 6) * 5.0 for i in range(n_assets)])
        factor = 1.5 * e1 - 0.8 * e2 + sec_shift + rng.normal(0.0, 0.2, size=(t_len, n_assets))

        resid, diag = cs_neutralize_ols(
            factor,
            {"e1": e1, "e2": e2},
            sectors=sectors,
            return_diagnostics=True,
        )

        assert (diag["mode"] == MODE_CONTINUOUS_ONLY).all()
        assert (diag["k_regressors"] == 2).all()

        # Orthogonal to continuous exposures
        for t in range(t_len):
            e_vec = resid.iloc[t].to_numpy()
            assert abs(float(np.dot(e1.iloc[t].to_numpy(), e_vec))) < 1e-10
            assert abs(float(np.dot(e2.iloc[t].to_numpy(), e_vec))) < 1e-10

        # At least one sector residual mean must be clearly non-zero (proves dummies were stripped)
        sec_means = [
            abs(float(resid.iloc[0][[c for c in cols if sectors[c] == f"SEC_{s}"]].mean()))
            for s in range(6)
        ]
        assert max(sec_means) > 0.2

    def test_zscore_fallback_n8_matches_manual_ddof1_and_keeps_nan(self):
        rng = np.random.default_rng(303)
        cols = [f"S{i}" for i in range(10)]
        dates = _make_dates(2)
        raw = rng.normal(5.0, 2.0, size=(2, 10))
        raw[:, 8:] = np.nan  # 8 valid entries (< min_obs_ols=10)
        factor = pd.DataFrame(raw, index=dates, columns=cols)
        exp1 = pd.DataFrame(rng.normal(size=(2, 10)), index=dates, columns=cols)

        resid, diag = cs_neutralize_ols(factor, {"e1": exp1}, return_diagnostics=True)
        assert (diag["mode"] == MODE_ZSCORE).all()
        assert resid.iloc[:, 8:].isna().all().all()

        for t in range(2):
            valid_vals = raw[t, :8]
            expected = (valid_vals - valid_vals.mean()) / valid_vals.std(ddof=1)
            np.testing.assert_allclose(resid.iloc[t, :8].to_numpy(), expected, atol=1e-12)

    def test_boundary_n30_vs_n29_flips_full_to_continuous_only(self):
        rng = np.random.default_rng(404)
        cols = [f"S{i:02d}" for i in range(30)]
        dates = _make_dates(2)
        sectors = {c: f"SEC_{i % 3}" for i, c in enumerate(cols)}
        factor = pd.DataFrame(rng.normal(size=(2, 30)), index=dates, columns=cols)
        factor.iloc[1, 0] = np.nan  # row 0 has N=30, row 1 has N=29
        exp1 = pd.DataFrame(rng.normal(size=(2, 30)), index=dates, columns=cols)

        _, diag = cs_neutralize_ols(
            factor, {"e1": exp1}, sectors=sectors, return_diagnostics=True
        )
        assert diag.iloc[0]["mode"] == MODE_FULL
        assert diag.iloc[1]["mode"] == MODE_CONTINUOUS_ONLY

    def test_wls_orthogonality(self):
        rng = np.random.default_rng(505)
        cols = [f"S{i:02d}" for i in range(40)]
        dates = _make_dates(5)
        e1 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)
        e2 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)
        w = pd.Series(rng.uniform(0.5, 5.0, size=40), index=cols)
        factor = 1.2 * e1 - 0.5 * e2 + rng.normal(size=(5, 40))

        resid = cs_neutralize_ols(factor, {"e1": e1, "e2": e2}, weights=w)
        w_vec = w.to_numpy()
        for t in range(5):
            e_vec = resid.iloc[t].to_numpy()
            assert abs(float(np.dot(w_vec, e_vec))) < 1e-10
            assert abs(float(np.dot(e1.iloc[t].to_numpy() * w_vec, e_vec))) < 1e-10
            assert abs(float(np.dot(e2.iloc[t].to_numpy() * w_vec, e_vec))) < 1e-10

    def test_collinear_exposures_exact_fit_insufficient_and_empty_exposures(self):
        rng = np.random.default_rng(606)
        cols = [f"S{i:02d}" for i in range(20)]
        dates = _make_dates(3)
        e1 = pd.DataFrame(rng.normal(size=(3, 20)), index=dates, columns=cols)
        e2 = e1 * 2.0  # perfectly collinear

        # Row 0: noisy, Row 1: exact linear fit, Row 2: N=1 (<2 -> insufficient)
        factor = 3.0 * e1 + rng.normal(0.0, 0.5, size=(3, 20))
        factor.iloc[1] = 2.0 * e1.iloc[1] + 1.0
        factor.iloc[2, 1:] = np.nan

        resid, diag = cs_neutralize_ols(factor, {"e1": e1, "e2": e2}, return_diagnostics=True)
        assert diag.iloc[0]["mode"] == MODE_FULL
        assert np.std(resid.iloc[0].to_numpy(), ddof=1) == pytest.approx(1.0, abs=1e-10)
        np.testing.assert_allclose(resid.iloc[1].to_numpy(), 0.0, atol=1e-12)
        assert diag.iloc[2]["mode"] == MODE_INSUFFICIENT
        assert resid.iloc[2].isna().all()

        with pytest.raises(ValueError):
            cs_neutralize_ols(factor, None)
        with pytest.raises(ValueError):
            cs_neutralize_ols(factor, {})


# =============================================================================
# 4. cs_lowdin_orthogonalize
# =============================================================================


class TestCsLowdinOrthogonalize:
    def test_orthonormality_shape_and_minimal_distortion_vs_gram_schmidt(self):
        rng = np.random.default_rng(707)
        t_len, n_assets = 20, 60
        dates = _make_dates(t_len)
        cols = [f"S{i:02d}" for i in range(n_assets)]

        base = rng.normal(size=(t_len, n_assets))
        f1 = pd.DataFrame(base + rng.normal(0.0, 0.5, size=(t_len, n_assets)), index=dates, columns=cols)
        f2 = pd.DataFrame(0.7 * base + rng.normal(0.0, 0.6, size=(t_len, n_assets)), index=dates, columns=cols)
        f3 = pd.DataFrame(0.4 * base + rng.normal(0.0, 0.8, size=(t_len, n_assets)), index=dates, columns=cols)

        out, diag = cs_lowdin_orthogonalize(
            {"f1": f1, "f2": f2, "f3": f3},
            scale="gram_identity",
            return_diagnostics=True,
        )

        assert (diag["mode"] == "full_rank").all()
        assert (diag["effective_rank"] == 3).all()
        for k in ("f1", "f2", "f3"):
            assert out[k].shape == (t_len, n_assets)

        for t in range(t_len):
            f_raw = np.column_stack([f1.iloc[t].to_numpy(), f2.iloc[t].to_numpy(), f3.iloc[t].to_numpy()])
            f_raw = f_raw - f_raw.mean(axis=0, keepdims=True)
            f_hat = f_raw / np.linalg.norm(f_raw, axis=0, keepdims=True)

            f_star = np.column_stack([out["f1"].iloc[t].to_numpy(), out["f2"].iloc[t].to_numpy(), out["f3"].iloc[t].to_numpy()])
            gram = f_star.T @ f_star
            assert gram.shape == (3, 3)
            assert gram.shape != (n_assets, n_assets)
            np.testing.assert_allclose(gram, np.eye(3), atol=1e-10)

            # Diagonal correlation with original factors must be positive
            for k in range(3):
                corr = np.corrcoef(f_star[:, k], f_raw[:, k])[0, 1]
                assert corr > 0.0

            # Löwdin minimizes Frobenius distance to unit-norm original factors vs Gram-Schmidt
            q_gs, _ = np.linalg.qr(f_hat)
            # Align Gram-Schmidt signs positively with f_hat
            signs = np.sign(np.sum(q_gs * f_hat, axis=0))
            signs[signs == 0] = 1.0
            q_gs = q_gs * signs[None, :]
            dist_lowdin = np.linalg.norm(f_star - f_hat, ord="fro")
            dist_gs = np.linalg.norm(q_gs - f_hat, ord="fro")
            assert dist_lowdin <= dist_gs + 1e-12

    def test_orthonormal_input_passes_through_unchanged(self):
        rng = np.random.default_rng(808)
        dates = _make_dates(3)
        cols = [f"S{i:02d}" for i in range(30)]
        f_mats = []
        for _ in range(3):
            raw = rng.normal(size=(30, 3))
            raw -= raw.mean(axis=0, keepdims=True)
            q, _ = np.linalg.qr(raw)
            f_mats.append(q)

        factors = {
            f"f{k}": pd.DataFrame(
                np.vstack([m[:, k] for m in f_mats]), index=dates, columns=cols
            )
            for k in range(3)
        }
        out = cs_lowdin_orthogonalize(factors, demean=True, scale="gram_identity")
        for k in range(3):
            np.testing.assert_allclose(
                out[f"f{k}"].to_numpy(), factors[f"f{k}"].to_numpy(), atol=1e-12
            )

    def test_listwise_nan_duplicate_constant_and_insufficient(self):
        rng = np.random.default_rng(909)
        dates = _make_dates(4)
        cols = [f"S{i:02d}" for i in range(25)]

        f1 = pd.DataFrame(rng.normal(size=(4, 25)), index=dates, columns=cols)
        f2 = pd.DataFrame(rng.normal(size=(4, 25)), index=dates, columns=cols)
        f3 = pd.DataFrame(rng.normal(size=(4, 25)), index=dates, columns=cols)

        # Date 0: listwise NaN at col 0 in f1 and col 1 in f2
        f1.iloc[0, 0] = np.nan
        f2.iloc[0, 1] = np.nan

        # Date 1: duplicate factor (f2 == f1)
        f2.iloc[1] = f1.iloc[1].copy()

        # Date 2: constant factor (f3 == 5.0)
        f3.iloc[2] = 5.0

        # Date 3: N_v = 3 < K + 1 = 4 -> insufficient
        f1.iloc[3, 3:] = np.nan

        out, diag = cs_lowdin_orthogonalize(
            {"f1": f1, "f2": f2, "f3": f3}, return_diagnostics=True
        )

        # Date 0: listwise NaN
        for k in ("f1", "f2", "f3"):
            assert np.isnan(out[k].iloc[0, 0])
            assert np.isnan(out[k].iloc[0, 1])
            assert out[k].iloc[0, 2:].notna().all()

        # Date 1: duplicate factor -> rank_deficient, projector P^2 ~= P, identical columns
        assert diag.iloc[1]["mode"] == "rank_deficient"
        assert diag.iloc[1]["effective_rank"] == 2
        f_star1 = np.column_stack([out["f1"].iloc[1], out["f2"].iloc[1], out["f3"].iloc[1]])
        p_mat = f_star1.T @ f_star1
        np.testing.assert_allclose(p_mat @ p_mat, p_mat, atol=1e-10)
        np.testing.assert_allclose(f_star1[:, 0], f_star1[:, 1], atol=1e-12)

        # Date 2: constant f3 becomes NaN while f1 & f2 stay orthonormal
        assert diag.iloc[2]["mode"] == "rank_deficient"
        assert out["f3"].iloc[2].isna().all()
        f_star2 = np.column_stack([out["f1"].iloc[2], out["f2"].iloc[2]])
        np.testing.assert_allclose(f_star2.T @ f_star2, np.eye(2), atol=1e-10)

        # Date 3: insufficient -> whole date NaN
        assert diag.iloc[3]["mode"] == MODE_INSUFFICIENT
        for k in ("f1", "f2", "f3"):
            assert out[k].iloc[3].isna().all()

    def test_unit_variance_and_mismatched_panels_raise(self):
        rng = np.random.default_rng(1010)
        dates = _make_dates(5)
        cols = [f"S{i:02d}" for i in range(40)]
        f1 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)
        f2 = pd.DataFrame(0.5 * f1.to_numpy() + rng.normal(size=(5, 40)), index=dates, columns=cols)

        out = cs_lowdin_orthogonalize({"f1": f1, "f2": f2}, scale="unit_variance")
        for t in range(5):
            mat = np.column_stack([out["f1"].iloc[t], out["f2"].iloc[t]])
            np.testing.assert_allclose(np.std(mat, axis=0, ddof=1), [1.0, 1.0], atol=1e-10)
            np.testing.assert_allclose(np.corrcoef(mat, rowvar=False), np.eye(2), atol=1e-10)

        # Mismatched columns
        f_bad = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=[f"X{i}" for i in range(40)])
        with pytest.raises(ValueError):
            cs_lowdin_orthogonalize({"f1": f1, "bad": f_bad})
        with pytest.raises(ValueError):
            cs_lowdin_orthogonalize({})

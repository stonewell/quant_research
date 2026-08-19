"""Unit tests for the shared shuffle-null significance primitive
(common/significance.py). Guaranteed 100% offline/synthetic.
"""

import os
import sys

import numpy as np
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.significance import StopSurrogates, empirical_pvalue, shuffle_null_test, significance_flag


def test_empirical_pvalue_hand_computed():
    # observed deviation from 0 is 1.0; surrogates' deviations from 0 are
    # [0, 0.5, 1, 2, 3] -> 3 of 5 are >= 1.0 -> p = 0.6
    p = empirical_pvalue(1.0, np.array([0, 0.5, 1, 2, 3]), reference=0.0)
    assert p == pytest.approx(0.6)


def test_empirical_pvalue_nonzero_reference():
    # Mirrors Hurst's reference=0.5: observed deviation is |0.6-0.5|=0.1;
    # surrogate deviations from 0.5 are [0.0, 0.1, 0.4] -> 2 of 3 >= 0.1
    p = empirical_pvalue(0.6, np.array([0.5, 0.6, 0.9]), reference=0.5)
    assert p == pytest.approx(2 / 3)


def test_empirical_pvalue_empty_surrogates_returns_nan_without_raising():
    assert np.isnan(empirical_pvalue(1.0, np.array([])))


def test_significance_flag_nan_is_not_significant():
    assert significance_flag(np.nan) is False


def test_significance_flag_boundary_is_strict_less_than():
    assert significance_flag(0.05, alpha=0.05) is False
    assert significance_flag(0.049, alpha=0.05) is True


def test_shuffle_null_test_shared_rng_state_advances_across_calls():
    # A deterministic surrogate function that returns successive draws from
    # the SAME rng -- proves a caller can share one rng across many
    # shuffle_null_test calls (as pattern_mining.py does) and get the same
    # sequence as calling rng.random() directly that many times.
    rng_a = np.random.default_rng(7)
    rng_b = np.random.default_rng(7)

    result1 = shuffle_null_test(0.5, lambda r: r.random(), n_surrogates=3, rng=rng_a)
    result2 = shuffle_null_test(0.5, lambda r: r.random(), n_surrogates=3, rng=rng_a)

    expected_first = [rng_b.random() for _ in range(3)]
    expected_second = [rng_b.random() for _ in range(3)]

    np.testing.assert_allclose(result1["surrogate_stats"], expected_first)
    np.testing.assert_allclose(result2["surrogate_stats"], expected_second)


def test_shuffle_null_test_stop_surrogates_yields_zero_surrogates():
    def _always_stop(rng):
        raise StopSurrogates

    result = shuffle_null_test(1.0, _always_stop, n_surrogates=10, rng=np.random.default_rng(0))
    assert result["n_surrogates_used"] == 0
    assert np.isnan(result["p_value"])
    assert result["significant"] is False


def test_shuffle_null_test_skip_nan_true_drops_nan_surrogates():
    values = iter([1.0, np.nan, 2.0, np.nan, 3.0])
    result = shuffle_null_test(0.0, lambda r: next(values), n_surrogates=5,
                                rng=np.random.default_rng(0), skip_nan=True)
    assert result["n_surrogates_used"] == 3
    np.testing.assert_allclose(sorted(result["surrogate_stats"]), [1.0, 2.0, 3.0])


def test_shuffle_null_test_skip_nan_false_propagates_nan():
    values = iter([1.0, np.nan, 2.0])
    result = shuffle_null_test(0.0, lambda r: next(values), n_surrogates=3,
                                rng=np.random.default_rng(0), skip_nan=False)
    assert result["n_surrogates_used"] == 3
    assert np.isnan(result["surrogate_stats"]).sum() == 1


def test_shuffle_null_test_callable_reference_uses_surrogate_mean():
    # reference=lambda s: s.mean() reproduces pattern_mining.py's null_mean.
    values = iter([1.0, 2.0, 3.0])
    result = shuffle_null_test(10.0, lambda r: next(values), n_surrogates=3,
                                rng=np.random.default_rng(0), reference=lambda s: s.mean(),
                                skip_nan=False)
    assert result["reference"] == pytest.approx(2.0)

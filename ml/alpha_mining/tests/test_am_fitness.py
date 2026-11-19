"""Unit tests for amine/fitness.py vectorized RankIC and multi-objective fitness.

100% offline: uses synthetic factor & return panels only.
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

from common.factor_analyzer import compute_rank_ic
from amine.fitness import (
    compute_factor_turnover,
    evaluate_factor_fitness,
    fast_rank_ic,
)


def test_fast_rank_ic_parity_with_compute_rank_ic():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2022-01-03", periods=40)
    cols = [f"A{i}" for i in range(10)]
    fac = pd.DataFrame(rng.normal(size=(40, 10)), index=dates, columns=cols)
    ret = pd.DataFrame(rng.normal(size=(40, 10)), index=dates, columns=cols)

    # Inject ties, NaNs, a constant row, and a < min_assets row
    fac.iloc[3, :4] = 1.5
    ret.iloc[3, 2:6] = -0.2
    fac.iloc[5, :3] = np.nan
    ret.iloc[5, 3:5] = np.nan
    fac.iloc[7, :] = 2.0  # constant row -> 0.0
    fac.iloc[9, 2:] = np.nan  # only 2 valid pairs < min_assets=3 -> NaN

    fast = fast_rank_ic(fac, ret, min_assets=3)
    ref = compute_rank_ic(fac, ret, min_assets=3)

    assert fast.index.equals(ref.index)
    # NaN locations must match identically
    assert (fast.isna() == ref.isna()).all()
    valid = fast.notna()
    np.testing.assert_allclose(fast[valid].to_numpy(), ref[valid].to_numpy(), atol=1e-10)
    assert fast.iloc[7] == 0.0
    assert np.isnan(fast.iloc[9])


def test_library_correlation_penalty_exactness_at_c_equals_1():
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2022-01-03", periods=50)
    cols = [f"S{i}" for i in range(8)]
    fac = pd.DataFrame(rng.normal(size=(50, 8)), index=dates, columns=cols)
    ret = 0.3 * fac + pd.DataFrame(rng.normal(size=(50, 8)), index=dates, columns=cols)

    lambda_corr = 1.25
    res_no_lib = evaluate_factor_fitness(
        fac,
        ret,
        node_count=4,
        library={},
        lambda_turnover=0.5,
        lambda_corr=lambda_corr,
        lambda_length=0.05,
        max_abs_corr=0.6,
    )
    res_identical_lib = evaluate_factor_fitness(
        fac,
        ret,
        node_count=4,
        library={"self_dup": fac.copy()},
        lambda_turnover=0.5,
        lambda_corr=lambda_corr,
        lambda_length=0.05,
        max_abs_corr=0.6,
    )

    assert np.isclose(res_identical_lib.max_corr, 1.0, atol=1e-10)
    # At C = 1.0, penalty = lambda_corr * (1 - 0.6) / (1 - 0.6) == lambda_corr exactly
    assert np.isclose(res_no_lib.fitness - res_identical_lib.fitness, lambda_corr, atol=1e-10)


def test_turnover_bounds_and_negative_ic_orientation():
    rng = np.random.default_rng(19)
    dates = pd.bdate_range("2022-01-03", periods=60)
    cols = [f"S{i}" for i in range(10)]
    fac = pd.DataFrame(rng.normal(size=(60, 10)), index=dates, columns=cols)
    to = compute_factor_turnover(fac)
    assert 0.0 <= to <= 1.0

    # Constant cross-sectional ranks across time have 0 turnover
    static_row = np.arange(10, dtype=float)
    static_fac = pd.DataFrame(
        np.tile(static_row, (30, 1)), index=dates[:30], columns=cols
    )
    assert np.isclose(compute_factor_turnover(static_fac), 0.0, atol=1e-12)

    # Negative-IC factor gets sign = -1.0 and identical fitness to its negated version
    ret_pos = 0.5 * fac + pd.DataFrame(rng.normal(scale=0.3, size=fac.shape), index=dates, columns=cols)
    ret_neg = -ret_pos

    res_pos = evaluate_factor_fitness(fac, ret_pos, node_count=5)
    res_neg = evaluate_factor_fitness(fac, ret_neg, node_count=5)

    assert res_pos.mean_ic > 0.0
    assert res_pos.sign == 1.0
    assert res_neg.mean_ic < 0.0
    assert res_neg.sign == -1.0
    assert np.isclose(res_pos.fitness, res_neg.fitness, atol=1e-10)

    # Orienting by s * expr flips negative IC to positive IC
    oriented = fac * res_neg.sign
    assert float(fast_rank_ic(oriented, ret_neg).mean()) > 0.0

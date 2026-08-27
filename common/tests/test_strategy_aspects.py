"""Unit tests for common/strategy_aspects.py (Track A: basket-template
selection x weighting composition). All synthetic price data, no network."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_templates import (
    BreadthGatedMomentumAllocation,
    CrossSectionalMomentumAllocation,
    EqualWeightAllocation,
    HierarchicalRiskParityAllocation,
    InverseVolatilityAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
)
from common.scheduling import get_rebalance_dates as _get_rebalance_dates
from common.strategy_aspects import (
    ATOMIC_TEMPLATE_ASPECTS,
    SELECTION_ASPECTS,
    WEIGHTING_ASPECTS,
    CompositeAllocationTemplate,
    build_composite_candidates,
)
from common.testing import make_ohlcv_from_closes as make_df


def _make_universe(n=3, periods=300, seed=5):
    rng = np.random.default_rng(seed)
    universe = {}
    for i in range(n):
        closes = 100 + np.cumsum(rng.normal(0.05, 1.0, periods))
        universe[chr(65 + i)] = make_df(closes, start="2020-01-01")
    return universe


ATOMIC_PARITY_CASES = [
    (EqualWeightAllocation, {"rebalance_freq_days": 21}),
    (InverseVolatilityAllocation, {"vol_lookback": 60, "rebalance_freq_days": 21}),
    (HierarchicalRiskParityAllocation, {"cov_lookback": 126, "rebalance_freq_days": 21}),
    (MaxDiversificationAllocation, {"vol_lookback": 60, "rebalance_freq_days": 21}),
    (MinimumVarianceAllocation, {"cov_lookback": 126, "rebalance_freq_days": 21}),
    (CrossSectionalMomentumAllocation, {"mom_lookback": 63, "top_n_fraction": 0.5, "rebalance_freq_days": 21}),
    (MeanReversionAllocation, {"rsi_period": 5, "top_n_fraction": 0.5, "rebalance_freq_days": 5}),
    (
        BreadthGatedMomentumAllocation,
        {"mom_lookback": 63, "top_n_fraction": 0.5, "protection_factor": 1, "rebalance_freq_days": 21},
    ),
]


@pytest.mark.parametrize("atomic_cls,params", ATOMIC_PARITY_CASES)
def test_composite_reproduces_atomic_template_exactly(atomic_cls, params):
    """For the 8 atomic templates where Track A's aspects are an exact
    reimplementation (everything except DualMomentumAllocation, which is a
    disclosed simplification -- see module docstring), pairing that same
    template's own (selection, weighting) key pair back together via
    CompositeAllocationTemplate must reproduce byte-for-byte identical
    weights to the original class."""
    universe = _make_universe()
    atomic = atomic_cls()
    sel_key, wt_key = ATOMIC_TEMPLATE_ASPECTS[atomic.name]
    composite = CompositeAllocationTemplate(SELECTION_ASPECTS[sel_key], WEIGHTING_ASPECTS[wt_key])

    expected = atomic.generate_weights(universe, params)
    actual = composite.generate_weights(universe, params)

    pd.testing.assert_frame_equal(actual, expected, check_dtype=False)


def test_dual_momentum_topn_excludes_non_positive_momentum_even_if_top_ranked():
    idx_len = 300
    rng = np.random.default_rng(3)
    up = 100 * np.exp(np.cumsum(rng.normal(0.003, 0.01, idx_len)))
    down = 100 * np.exp(np.cumsum(rng.normal(-0.003, 0.01, idx_len)))
    universe = {"UP": make_df(up, start="2020-01-01"), "DOWN": make_df(down, start="2020-01-01")}

    aspect = SELECTION_ASPECTS["dual_momentum_topn"]
    params = {"mom_lookback": 63, "top_n_fraction": 1.0}  # top_n covers both symbols
    master_index = universe["UP"].index
    rebalance_dates = _get_rebalance_dates(master_index, 21)

    result = aspect.select(universe, params, master_index, rebalance_dates)
    last_date = rebalance_dates[-1]

    assert bool(result.eligible.loc[last_date, "UP"]) is True
    assert bool(result.eligible.loc[last_date, "DOWN"]) is False


def test_weighting_aspect_scales_to_invested_fraction_not_always_1():
    """breadth_gated_topn's invested_fraction can be < 1.0; every weighting
    aspect must scale its chosen symbols' weights to sum to exactly that
    fraction, not silently renormalize back to 1.0."""
    universe = _make_universe(n=2, seed=11)
    selection_aspect = SELECTION_ASPECTS["momentum_topn"]
    weighting_aspect = WEIGHTING_ASPECTS["inverse_vol"]

    master_index = universe["A"].index
    rebalance_dates = _get_rebalance_dates(master_index, 21)
    sel_params = {"mom_lookback": 63, "top_n_fraction": 1.0}
    selection = selection_aspect.select(universe, sel_params, master_index, rebalance_dates)
    # Manually shrink the invested fraction to simulate a de-risked date.
    selection.invested_fraction.iloc[:] = 0.4

    weights = weighting_aspect.weight(universe, selection, {"vol_lookback": 20}, master_index, rebalance_dates)
    rebalance_rows = weights.dropna(how="all")
    assert not rebalance_rows.empty
    for _, row in rebalance_rows.iterrows():
        row_sum = row.dropna().sum()
        if row_sum > 0:
            np.testing.assert_allclose(row_sum, 0.4, atol=1e-9)


def test_weighting_aspect_zeroes_chosen_symbols_on_full_derisk_not_nan():
    """Regression: a fully de-risked date (invested_fraction == 0.0, only
    breadth_gated_topn ever produces this) is a definite decision, not a
    data-availability gap -- a chosen symbol must be explicitly zeroed, not
    left NaN, or the backtester's column-wise ffill would read that as
    "carry forward the prior weight", silently ignoring the de-risk signal."""
    universe = _make_universe(n=2, seed=11)
    selection_aspect = SELECTION_ASPECTS["momentum_topn"]
    master_index = universe["A"].index
    rebalance_dates = _get_rebalance_dates(master_index, 21)
    selection = selection_aspect.select(
        universe, {"mom_lookback": 63, "top_n_fraction": 1.0}, master_index, rebalance_dates,
    )
    selection.invested_fraction.iloc[:] = 0.0  # simulate a full de-risk at every rebalance

    for weighting_key in ("inverse_vol", "hrp", "min_variance", "max_diversification"):
        weighting_aspect = WEIGHTING_ASPECTS[weighting_key]
        params = {"vol_lookback": 20, "cov_lookback": 20}
        weights = weighting_aspect.weight(universe, selection, params, master_index, rebalance_dates)
        rebalance_rows = weights.dropna(how="all")
        assert not rebalance_rows.empty, weighting_key
        for date, row in rebalance_rows.iterrows():
            assert not row.isna().any(), f"{weighting_key} left NaN at {date}: {row.to_dict()}"
            assert (row == 0.0).all(), f"{weighting_key} failed to zero out at {date}: {row.to_dict()}"


def test_build_composite_candidates_cross_breeds_and_skips_existing_pairs():
    fake_best = {
        "cross_sectional_momentum": {
            "template": CrossSectionalMomentumAllocation(),
            "params": {"mom_lookback": 126, "top_n_fraction": 0.5, "rebalance_freq_days": 21},
            "score": 1.0,
            "res": {},
        },
        "minimum_variance": {
            "template": MinimumVarianceAllocation(),
            "params": {"cov_lookback": 126, "rebalance_freq_days": 63},
            "score": 0.9,
            "res": {},
        },
    }

    candidates = build_composite_candidates(fake_best, top_k=4)
    by_name = {t.name: (t, p) for t, p in candidates}

    # Cross-bred pairing plus the "reconstructs EqualWeightAllocation, but
    # EqualWeightAllocation itself wasn't in the top-k" edge case -- both
    # expected, since only pairs already present AMONG THE TOP-K SOURCE
    # TEMPLATES are excluded.
    assert set(by_name.keys()) == {"momentum_topn__min_variance", "all_symbols__equal_weight"}

    # The selection side's own best rebalance_freq_days wins on merge.
    _, merged_params = by_name["momentum_topn__min_variance"]
    assert merged_params["rebalance_freq_days"] == 21
    assert merged_params["mom_lookback"] == 126
    assert merged_params["cov_lookback"] == 126


def test_build_composite_candidates_needs_at_least_two_decomposable_templates():
    fake_best = {
        "cross_sectional_momentum": {
            "template": CrossSectionalMomentumAllocation(), "params": {}, "score": 1.0, "res": {},
        },
    }
    assert build_composite_candidates(fake_best, top_k=4) == []

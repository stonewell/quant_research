"""Unit tests for amine/combiner.py native LightGBM combiner & Purged WF CV.

100% offline: uses synthetic factor & label panels only.
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

from amine.combiner import fit_predict_holdout, run_purged_cv_oof
from amine.config import CombinerConfig, PurgedCVConfig
from amine.purged_cv import assert_no_leakage


def _make_factor_and_label_panels(n_dates: int = 120, n_syms: int = 10, seed: int = 42):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-04", periods=n_dates)
    cols = [f"S{i}" for i in range(n_syms)]
    f1 = pd.DataFrame(rng.normal(size=(n_dates, n_syms)), index=dates, columns=cols)
    f2 = pd.DataFrame(rng.normal(size=(n_dates, n_syms)), index=dates, columns=cols)
    f3 = pd.DataFrame(rng.normal(size=(n_dates, n_syms)), index=dates, columns=cols)
    labels = 0.03 * f1 - 0.02 * f2 + pd.DataFrame(
        rng.normal(scale=0.02, size=(n_dates, n_syms)), index=dates, columns=cols
    )
    labels.iloc[-6:] = np.nan
    return {"f1": f1, "f2": f2, "f3": f3}, labels


@pytest.mark.parametrize("objective", ["lambdarank", "regression"])
def test_combiner_purged_cv_oof_objectives_and_non_leakage(objective: str):
    factors, labels = _make_factor_and_label_panels(n_dates=130, n_syms=10, seed=17)
    cv_cfg = PurgedCVConfig(
        n_splits=3, embargo_days=3, train_side="past", min_train_dates=15, min_test_dates=10
    )
    comb_cfg = CombinerConfig.small_data(seed=42, objective=objective)

    res = run_purged_cv_oof(
        factors,
        labels,
        cv_config=cv_cfg,
        combiner_config=comb_cfg,
        h=6,
    )

    assert res.n_folds_trained == 3
    assert res.feature_names == ["f1", "f2", "f3"]
    assert len(res.fold_importances) == 3
    assert not res.oof_scores.empty
    assert np.isfinite(res.oof_scores.to_numpy(dtype=float)).all()

    # Every (date, symbol) in OOF is predicted strictly once, and each OOF date belongs to one fold
    assert not res.oof_long.duplicated(subset=["date", "symbol"]).any()
    folds_per_date = res.oof_long.groupby("date")["fold_id"].nunique()
    assert (folds_per_date == 1).all()

    # Check small_data profile parameters
    params = comb_cfg.to_lgb_params()
    assert params["objective"] == objective
    assert params["min_child_samples"] == 5
    assert comb_cfg.num_boost_round == 50


def test_fit_predict_holdout_enforces_purge_and_predicts_all_holdout_dates():
    factors, labels = _make_factor_and_label_panels(n_dates=120, n_syms=8, seed=29)
    all_dates = labels.index
    train_dates = all_dates[:70]
    holdout_dates = all_dates[81:]  # gap = 11 >= h(6) + embargo(5)

    assert_no_leakage(train_dates, holdout_dates, h=6, embargo=5, all_dates=all_dates)

    res = fit_predict_holdout(
        factors,
        labels,
        train_dates=train_dates,
        holdout_dates=holdout_dates,
        combiner_config=CombinerConfig.small_data(seed=7, objective="lambdarank"),
        h=6,
        embargo=5,
        all_dates=all_dates,
    )
    # Predicts every holdout date including trailing rows where labels are NaN
    assert res.holdout_scores.index.equals(holdout_dates)
    assert np.isfinite(res.holdout_scores.to_numpy(dtype=float)).all()
    assert set(res.feature_importance.keys()) == {"f1", "f2", "f3"}

    # Violating purge gap must raise AssertionError
    leaky_holdout = all_dates[72:]
    with pytest.raises(AssertionError):
        fit_predict_holdout(
            factors,
            labels,
            train_dates=train_dates,
            holdout_dates=leaky_holdout,
            combiner_config=CombinerConfig.small_data(seed=7),
            h=6,
            embargo=5,
            all_dates=all_dates,
        )

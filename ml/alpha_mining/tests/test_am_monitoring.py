"""Unit tests for amine/monitoring.py report-only diagnostics.

100% offline: uses synthetic distributions, importances, and score panels only.
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

from amine.monitoring import (
    adversarial_validation,
    check_importance_decay,
    check_turnover_breaker,
    compute_roc_auc,
)


def test_mann_whitney_roc_auc_with_ties_hand_computed():
    # y_true:  [0,   0,   1,   1,   0,   1]
    # y_score: [0.1, 0.4, 0.4, 0.8, 0.2, 0.9]
    # Sorted scores: 0.1(0,r=1), 0.2(0,r=2), 0.4(0,r=3.5), 0.4(1,r=3.5), 0.8(1,r=5), 0.9(1,r=6)
    # sum(rank_pos) = 3.5 + 5 + 6 = 14.5; n_pos=3, n_neg=3
    # AUC = (14.5 - 3*4/2) / (3*3) = 8.5 / 9
    y_true = [0, 0, 1, 1, 0, 1]
    y_score = [0.1, 0.4, 0.4, 0.8, 0.2, 0.9]
    auc = compute_roc_auc(y_true, y_score)
    assert np.isclose(auc, 8.5 / 9.0, atol=1e-12)

    # All-tied scores -> exactly 0.5
    assert np.isclose(compute_roc_auc([0, 1, 0, 1], [1.0, 1.0, 1.0, 1.0]), 0.5, atol=1e-12)


def test_adversarial_validation_shifted_vs_iid():
    rng = np.random.default_rng(42)
    cols = ["f1", "f2", "f3"]

    # Clearly shifted distribution -> AUC > 0.9 and alarm=True
    ref_shift = pd.DataFrame(rng.normal(0.0, 1.0, size=(180, 3)), columns=cols)
    rec_shift = pd.DataFrame(rng.normal(2.5, 1.0, size=(180, 3)), columns=cols)
    res_shift = adversarial_validation(ref_shift, rec_shift, cols, n_splits=3, auc_threshold=0.70, seed=42)
    assert res_shift.auc > 0.90
    assert res_shift.alarm is True

    # IID draws from identical distribution -> AUC near 0.5 and alarm=False
    ref_iid = pd.DataFrame(rng.normal(0.0, 1.0, size=(240, 3)), columns=cols)
    rec_iid = pd.DataFrame(rng.normal(0.0, 1.0, size=(240, 3)), columns=cols)
    res_iid = adversarial_validation(ref_iid, rec_iid, cols, n_splits=3, auc_threshold=0.70, seed=42)
    assert 0.35 <= res_iid.auc <= 0.65
    assert res_iid.alarm is False


def test_importance_decay_alarm_and_stable_case():
    # Stable fold importances -> no alarm
    stable = [
        {"f1": 50.0, "f2": 30.0, "f3": 15.0, "f4": 5.0},
        {"f1": 48.0, "f2": 32.0, "f3": 14.0, "f4": 6.0},
    ]
    res_ok = check_importance_decay(stable, spearman_threshold=0.30, top3_drop_ratio=0.50)
    assert res_ok.alarm is False
    assert res_ok.min_spearman > 0.9

    # Severe rank reversal and >50% top-3 drop -> alarm=True
    decayed = [
        {"f1": 60.0, "f2": 25.0, "f3": 10.0, "f4": 5.0},
        {"f1": 5.0, "f2": 10.0, "f3": 25.0, "f4": 60.0},
    ]
    res_bad = check_importance_decay(decayed, spearman_threshold=0.30, top3_drop_ratio=0.50)
    assert res_bad.alarm is True
    assert res_bad.min_spearman < 0.30
    assert len(res_bad.dropped_top3_features) >= 1


def test_turnover_breaker_alarm_and_calm_case():
    dates = pd.bdate_range("2023-01-02", periods=30)
    cols = ["A", "B", "C", "D"]

    # Alternating top-2 every single day -> 1.0 daily one-way turnover -> alarm=True
    alt_rows = []
    for i in range(len(dates)):
        if i % 2 == 0:
            alt_rows.append([10.0, 9.0, 1.0, 0.0])
        else:
            alt_rows.append([0.0, 1.0, 9.0, 10.0])
    scores_churn = pd.DataFrame(alt_rows, index=dates, columns=cols)
    res_churn = check_turnover_breaker(scores_churn, top_n=2, window=20, threshold=0.30)
    assert res_churn.alarm is True
    assert res_churn.max_rolling_turnover > 0.9
    assert res_churn.recommended_action == "freeze holdings"

    # Constant scores -> 0.0 turnover -> alarm=False
    scores_calm = pd.DataFrame(
        np.tile([10.0, 9.0, 1.0, 0.0], (len(dates), 1)), index=dates, columns=cols
    )
    res_calm = check_turnover_breaker(scores_calm, top_n=2, window=20, threshold=0.30)
    assert res_calm.alarm is False
    assert np.isclose(res_calm.max_rolling_turnover, 0.0, atol=1e-12)
    assert res_calm.recommended_action == "none"

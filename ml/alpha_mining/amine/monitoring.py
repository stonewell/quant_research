"""Report-only production monitoring diagnostics for ML1 Alpha Mining.

All three diagnostics are strictly **report-only** in v1 (they record alarms
and recommendations in the JSON report and never gate or mutate live orders):

1. **Adversarial Validation**: binary native LightGBM separating reference
   (train) rows from recent rows using time-blocked K-fold OOF. Pure
   Mann-Whitney ROC-AUC via ``scipy.stats.rankdata`` (zero ``sklearn``).
   Alarm if ``AUC > 0.70``.
2. **Feature Importance Decay**: normalized gain share per fold. Alarm if
   consecutive-fold ``Spearman(imp_f, imp_{f-1}) < 0.30`` or any former top-3
   feature loses ``> 50%`` of its importance share.
3. **Turnover-Overload Circuit Breaker**: rolling 20-day mean one-way turnover
   of the top-N equal-weight book from OOF/holdout scores. Alarm if
   ``> 0.30 / day`` with report-only recommendation ``"freeze holdings"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import rankdata, spearmanr


@dataclass
class AdversarialValidationResult:
    """Output of time-blocked OOF adversarial validation."""

    auc: float
    alarm: bool
    threshold: float
    n_reference: int
    n_recent: int
    top_features_by_gain: List[Tuple[str, float]] = field(default_factory=list)


@dataclass
class ImportanceDecayResult:
    """Output of cross-fold feature importance stability check."""

    alarm: bool
    min_spearman: float
    pair_spearmans: List[float]
    dropped_top3_features: List[Dict[str, object]]
    normalized_fold_importances: List[Dict[str, float]]


@dataclass
class TurnoverBreakerResult:
    """Output of rolling one-way turnover overload check (report-only)."""

    alarm: bool
    max_rolling_turnover: float
    mean_daily_turnover: float
    threshold: float
    window: int
    recommended_action: str


def compute_roc_auc(
    y_true: Sequence[int] | np.ndarray,
    y_score: Sequence[float] | np.ndarray,
) -> float:
    """Compute ROC-AUC via the Mann-Whitney U statistic with average tie ranks.

    Formula::

        AUC = (sum(rank_pos) - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    Uses ``scipy.stats.rankdata(..., method="average")`` so tied scores receive
    their exact midrank without requiring ``scikit-learn``.
    """
    yt = np.asarray(y_true, dtype=int)
    ys = np.asarray(y_score, dtype=float)
    if yt.shape != ys.shape:
        raise ValueError(f"y_true shape {yt.shape} != y_score shape {ys.shape}")
    if yt.ndim != 1:
        raise ValueError(f"Expected 1D arrays, got ndim={yt.ndim}")

    finite = np.isfinite(ys)
    yt = yt[finite]
    ys = ys[finite]

    pos_mask = yt == 1
    neg_mask = yt == 0
    n_pos = int(pos_mask.sum())
    n_neg = int(neg_mask.sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")

    # Keep only binary {0, 1} entries
    valid = pos_mask | neg_mask
    yt = yt[valid]
    ys = ys[valid]
    pos_mask = yt == 1

    ranks = rankdata(ys, method="average")
    rank_sum_pos = float(ranks[pos_mask].sum())
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / float(n_pos * n_neg)
    return float(np.clip(auc, 0.0, 1.0))


def adversarial_validation(
    reference_df: pd.DataFrame,
    recent_df: pd.DataFrame,
    feature_cols: Sequence[str],
    *,
    n_splits: int = 3,
    auc_threshold: float = 0.70,
    seed: int = 42,
) -> AdversarialValidationResult:
    """Detect covariate/time drift between ``reference_df`` and ``recent_df``.

    Uses time-blocked ``n_splits``-fold OOF predictions from a native binary
    LightGBM classifier (reference=0, recent=1). Alarm triggers if
    ``AUC > auc_threshold``.
    """
    cols = list(feature_cols)
    if not cols:
        raise ValueError("feature_cols must be non-empty.")

    ref_x = reference_df[cols].to_numpy(dtype=float)
    rec_x = recent_df[cols].to_numpy(dtype=float)
    n_ref = len(ref_x)
    n_rec = len(rec_x)

    if n_ref < n_splits or n_rec < n_splits:
        return AdversarialValidationResult(
            auc=float("nan"),
            alarm=False,
            threshold=float(auc_threshold),
            n_reference=n_ref,
            n_recent=n_rec,
            top_features_by_gain=[],
        )

    # Contiguous time-blocked folds within each segment
    ref_blocks = np.array_split(np.arange(n_ref), n_splits)
    rec_blocks = np.array_split(np.arange(n_rec), n_splits)

    oof_y: List[np.ndarray] = []
    oof_p: List[np.ndarray] = []
    total_gain = np.zeros(len(cols), dtype=float)

    for fold_idx in range(n_splits):
        val_ref = ref_blocks[fold_idx]
        val_rec = rec_blocks[fold_idx]
        tr_ref = np.concatenate([ref_blocks[i] for i in range(n_splits) if i != fold_idx])
        tr_rec = np.concatenate([rec_blocks[i] for i in range(n_splits) if i != fold_idx])

        x_tr = np.vstack([ref_x[tr_ref], rec_x[tr_rec]])
        y_tr = np.concatenate([np.zeros(len(tr_ref), dtype=int), np.ones(len(tr_rec), dtype=int)])

        x_val = np.vstack([ref_x[val_ref], rec_x[val_rec]])
        y_val = np.concatenate([np.zeros(len(val_ref), dtype=int), np.ones(len(val_rec), dtype=int)])

        min_child = max(2, min(20, len(y_tr) // 10))
        params = {
            "objective": "binary",
            "max_depth": 3,
            "num_leaves": 7,
            "learning_rate": 0.05,
            "min_child_samples": min_child,
            "feature_fraction": 0.8,
            "deterministic": True,
            "num_threads": 1,
            "force_col_wise": True,
            "seed": int(seed) + fold_idx,
            "verbose": -1,
        }
        ds = lgb.Dataset(x_tr, label=y_tr, feature_name=cols, free_raw_data=False)
        booster = lgb.train(params, ds, num_boost_round=50)

        preds = booster.predict(x_val)
        oof_y.append(y_val)
        oof_p.append(preds)
        total_gain += booster.feature_importance(importance_type="gain")

    y_all = np.concatenate(oof_y)
    p_all = np.concatenate(oof_p)
    auc = compute_roc_auc(y_all, p_all)
    alarm = bool(np.isfinite(auc) and auc > float(auc_threshold))

    gain_sum = float(total_gain.sum())
    if gain_sum > 0.0:
        norm_gain = total_gain / gain_sum
    else:
        norm_gain = total_gain
    top_feats = sorted(
        [(name, float(g)) for name, g in zip(cols, norm_gain)],
        key=lambda kv: (-kv[1], kv[0]),
    )

    return AdversarialValidationResult(
        auc=auc,
        alarm=alarm,
        threshold=float(auc_threshold),
        n_reference=n_ref,
        n_recent=n_rec,
        top_features_by_gain=top_feats,
    )


def check_importance_decay(
    fold_importances: Sequence[Mapping[str, float]],
    *,
    spearman_threshold: float = 0.30,
    top3_drop_ratio: float = 0.50,
) -> ImportanceDecayResult:
    """Monitor cross-fold feature importance stability.

    Normalizes gain per fold to sum to 1.0. Triggers an alarm if any adjacent
    fold pair has ``Spearman(imp_f, imp_{f-1}) < spearman_threshold`` OR if any
    former top-3 feature loses more than ``top3_drop_ratio`` (default 50%) of
    its normalized share.
    """
    if not fold_importances:
        return ImportanceDecayResult(
            alarm=False,
            min_spearman=1.0,
            pair_spearmans=[],
            dropped_top3_features=[],
            normalized_fold_importances=[],
        )

    all_feats = sorted({k for imp in fold_importances for k in imp.keys()})
    norm_folds: List[Dict[str, float]] = []
    for imp in fold_importances:
        total = float(sum(max(0.0, float(imp.get(f, 0.0))) for f in all_feats))
        if total > 0.0:
            norm_folds.append({f: max(0.0, float(imp.get(f, 0.0))) / total for f in all_feats})
        else:
            norm_folds.append({f: 0.0 for f in all_feats})

    if len(norm_folds) < 2:
        return ImportanceDecayResult(
            alarm=False,
            min_spearman=1.0,
            pair_spearmans=[],
            dropped_top3_features=[],
            normalized_fold_importances=norm_folds,
        )

    pair_spearmans: List[float] = []
    dropped_top3: List[Dict[str, object]] = []
    alarm = False

    for f_idx in range(1, len(norm_folds)):
        prev_imp = norm_folds[f_idx - 1]
        curr_imp = norm_folds[f_idx]

        v_prev = np.array([prev_imp[f] for f in all_feats], dtype=float)
        v_curr = np.array([curr_imp[f] for f in all_feats], dtype=float)

        if len(all_feats) < 2:
            rho = 1.0
        elif np.std(v_prev) <= 1e-12 or np.std(v_curr) <= 1e-12:
            rho = 0.0
        else:
            r_val, _ = spearmanr(v_prev, v_curr)
            rho = float(r_val) if np.isfinite(r_val) else 0.0

        pair_spearmans.append(rho)
        if rho < float(spearman_threshold):
            alarm = True

        # Check former top-3 features with positive share
        prev_ranked = sorted(
            [(f, prev_imp[f]) for f in all_feats if prev_imp[f] > 0.0],
            key=lambda kv: (-kv[1], kv[0]),
        )[:3]
        for feat, prev_share in prev_ranked:
            curr_share = curr_imp[feat]
            rel_drop = (prev_share - curr_share) / prev_share
            if rel_drop > float(top3_drop_ratio):
                alarm = True
                dropped_top3.append(
                    {
                        "fold_pair": (f_idx - 1, f_idx),
                        "feature": feat,
                        "prev_share": prev_share,
                        "curr_share": curr_share,
                        "relative_drop": rel_drop,
                    }
                )

    min_sp = float(min(pair_spearmans)) if pair_spearmans else 1.0
    return ImportanceDecayResult(
        alarm=alarm,
        min_spearman=min_sp,
        pair_spearmans=pair_spearmans,
        dropped_top3_features=dropped_top3,
        normalized_fold_importances=norm_folds,
    )


def check_turnover_breaker(
    scores_df: pd.DataFrame,
    *,
    top_n: int = 5,
    window: int = 20,
    threshold: float = 0.30,
) -> TurnoverBreakerResult:
    """Evaluate rolling one-way turnover of the top-N book (report-only).

    Triggers an alarm if the rolling ``window``-day mean one-way turnover
    exceeds ``threshold`` (default 0.30/day), recording the report-only
    recommendation ``"freeze holdings"``.
    """
    if scores_df.empty or len(scores_df) < 2:
        return TurnoverBreakerResult(
            alarm=False,
            max_rolling_turnover=0.0,
            mean_daily_turnover=0.0,
            threshold=float(threshold),
            window=int(window),
            recommended_action="none",
        )

    weights = pd.DataFrame(0.0, index=scores_df.index, columns=scores_df.columns, dtype=float)
    for dt in scores_df.index:
        row = scores_df.loc[dt]
        valid = row[np.isfinite(row.to_numpy(dtype=float))]
        if valid.empty:
            continue
        k = min(int(top_n), len(valid))
        # Deterministic tie-break by symbol name
        ranked_syms = sorted(valid.index, key=lambda s: (-float(valid[s]), str(s)))[:k]
        w = 1.0 / float(k)
        weights.loc[dt, ranked_syms] = w

    daily_to = 0.5 * weights.diff().abs().sum(axis=1).iloc[1:]
    if daily_to.empty:
        return TurnoverBreakerResult(
            alarm=False,
            max_rolling_turnover=0.0,
            mean_daily_turnover=0.0,
            threshold=float(threshold),
            window=int(window),
            recommended_action="none",
        )

    mean_to = float(daily_to.mean())
    eff_win = min(int(window), len(daily_to))
    rolling_to = daily_to.rolling(eff_win, min_periods=eff_win).mean().dropna()
    max_roll = float(rolling_to.max()) if not rolling_to.empty else mean_to

    alarm = bool(np.isfinite(max_roll) and max_roll > float(threshold))
    return TurnoverBreakerResult(
        alarm=alarm,
        max_rolling_turnover=max_roll,
        mean_daily_turnover=mean_to,
        threshold=float(threshold),
        window=int(window),
        recommended_action="freeze holdings" if alarm else "none",
    )

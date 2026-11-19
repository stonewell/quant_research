"""Stage-2 native LightGBM combiner with Purged Walk-Forward CV (ML1).

Academic & Engineering Grounding:
- Cross-sectional non-linear factor combination via GBDT: Gu, Kelly & Xiu (2020),
  *Empirical Asset Pricing via Machine Learning*, RFS 33(5).
- Purged walk-forward out-of-fold evaluation: Lopez de Prado (2018), ch. 7.

Design Notes:
- **Native ``lightgbm.train`` only**: avoids ``LGBMRanker``/``LGBMRegressor``
  wrappers so ``scikit-learn`` is never required.
- **Fixed ``num_boost_round``**: no early stopping on the test fold, preventing
  nested validation leakage.
- **Objectives**:
  - ``"lambdarank"`` (default): per-date integer relevance bins
    ``clip(floor(rank_pct * n_bins), 0, n_bins - 1)`` with exact sorted
    per-date ``group`` counts.
  - ``"regression"``: per-date cross-sectional Gaussian-rank target via
    ``scipy.stats.norm.ppf((rank - 0.5) / n)``.
  - Custom negative-RankIC loss is deliberately deferred (non-differentiable
    rank objective requires a soft-sort surrogate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import norm

from amine.config import CombinerConfig, PurgedCVConfig
from amine.orthogonalize import lowdin_orthogonalize_panel
from amine.purged_cv import PurgedGroupTimeSeriesCV, assert_no_leakage


@dataclass
class CombinerCVResult:
    """Out-of-fold predictions and per-fold diagnostics from Purged WF CV."""

    oof_scores: pd.DataFrame
    oof_long: pd.DataFrame
    fold_importances: List[Dict[str, float]]
    feature_names: List[str]
    n_folds_trained: int


@dataclass
class HoldoutResult:
    """Holdout predictions and trained model metadata."""

    holdout_scores: pd.DataFrame
    feature_importance: Dict[str, float]
    feature_names: List[str]
    booster: Optional[lgb.Booster] = field(default=None, repr=False)


def _cs_zscore_preserve_nan(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per date preserving ``NaN`` cells."""
    arr = df.to_numpy(dtype=float)
    finite = np.isfinite(arr)
    out = np.full_like(arr, np.nan)
    for t in range(arr.shape[0]):
        mask = finite[t]
        n = int(mask.sum())
        if n == 0:
            continue
        vals = arr[t, mask]
        if n == 1:
            out[t, mask] = 0.0
            continue
        sd = float(np.std(vals, ddof=1))
        if not np.isfinite(sd) or sd <= 1e-12:
            out[t, mask] = 0.0
        else:
            out[t, mask] = (vals - float(np.mean(vals))) / sd
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def _prepare_feature_panels(
    factors: Mapping[str, pd.DataFrame],
    *,
    apply_lowdin: bool = False,
) -> Tuple[Dict[str, pd.DataFrame], pd.Index, pd.Index]:
    """Align, cross-sectionally z-score, and optionally Löwdin-orthogonalize factors."""
    if not factors:
        raise ValueError("factors mapping must be non-empty.")

    names = list(factors.keys())
    common_idx = factors[names[0]].index
    common_cols = factors[names[0]].columns
    for name in names[1:]:
        common_idx = common_idx.intersection(factors[name].index)
        common_cols = common_cols.intersection(factors[name].columns)

    common_idx = pd.Index(np.sort(common_idx))
    common_cols = pd.Index(sorted(common_cols, key=str))

    z_panels = {
        name: _cs_zscore_preserve_nan(factors[name].loc[common_idx, common_cols])
        for name in names
    }
    if apply_lowdin and len(names) >= 2:
        z_panels, _ = lowdin_orthogonalize_panel(z_panels)

    return z_panels, common_idx, common_cols


def _transform_labels_by_date(
    raw_labels: pd.Series,
    dates: pd.Series,
    *,
    objective: str,
    n_relevance_bins: int = 5,
) -> np.ndarray:
    """Transform raw forward returns into per-date ranking or Gaussian-rank targets."""
    out = np.empty(len(raw_labels), dtype=float)
    df_tmp = pd.DataFrame({"date": dates.to_numpy(), "y": raw_labels.to_numpy(dtype=float)})
    for _dt, grp in df_tmp.groupby("date", sort=False):
        idx = grp.index.to_numpy()
        y = grp["y"]
        n = len(y)
        if objective == "lambdarank":
            rank_pct = y.rank(method="average", pct=True).to_numpy(dtype=float)
            rel = np.clip(np.floor(rank_pct * n_relevance_bins), 0, n_relevance_bins - 1)
            out[idx] = rel.astype(int)
        else:
            # Cross-sectional Gaussian-rank label: norm.ppf((rank - 0.5) / n)
            ranks = y.rank(method="average").to_numpy(dtype=float)
            u = np.clip((ranks - 0.5) / float(max(n, 1)), 1e-6, 1.0 - 1e-6)
            out[idx] = norm.ppf(u)
    return out


def prepare_long_dataset(
    factors: Mapping[str, pd.DataFrame],
    labels: Optional[pd.DataFrame] = None,
    *,
    dates_subset: Optional[pd.Index] = None,
    apply_lowdin: bool = False,
    objective: str = "lambdarank",
    n_relevance_bins: int = 5,
    require_label: bool = True,
) -> Tuple[pd.DataFrame, List[str]]:
    """Build a ``(date, symbol)``-sorted long-format table for LightGBM.

    Returns ``(long_df, feature_names)`` where ``long_df`` has columns
    ``["date", "symbol", *feature_names]`` plus ``["raw_label", "target"]``
    when ``labels`` is provided.
    """
    z_panels, common_idx, common_cols = _prepare_feature_panels(
        factors, apply_lowdin=apply_lowdin
    )
    if labels is not None:
        common_idx = pd.Index(np.sort(common_idx.intersection(labels.index)))
        common_cols = pd.Index(sorted(common_cols.intersection(labels.columns), key=str))

    if dates_subset is not None:
        common_idx = pd.Index(np.sort(common_idx.intersection(dates_subset)))

    feature_names = list(z_panels.keys())
    n_dates = len(common_idx)
    n_syms = len(common_cols)

    if n_dates == 0 or n_syms == 0:
        cols = ["date", "symbol"] + feature_names
        if labels is not None:
            cols += ["raw_label", "target"]
        return pd.DataFrame(columns=cols), feature_names

    # Row-major repeat preserves exact (date, symbol) lexicographical sort.
    date_col = np.repeat(common_idx.to_numpy(), n_syms)
    sym_col = np.tile(common_cols.to_numpy(), n_dates)
    data: Dict[str, object] = {"date": date_col, "symbol": sym_col}

    for name in feature_names:
        arr = z_panels[name].loc[common_idx, common_cols].to_numpy(dtype=float)
        data[name] = arr.reshape(-1)

    if labels is not None:
        y_arr = labels.loc[common_idx, common_cols].to_numpy(dtype=float).reshape(-1)
        data["raw_label"] = y_arr

    long_df = pd.DataFrame(data)

    if labels is not None and require_label:
        valid_y = np.isfinite(long_df["raw_label"].to_numpy(dtype=float))
        long_df = long_df.loc[valid_y].reset_index(drop=True)
        if not long_df.empty:
            long_df["target"] = _transform_labels_by_date(
                long_df["raw_label"],
                long_df["date"],
                objective=objective,
                n_relevance_bins=n_relevance_bins,
            )
        else:
            long_df["target"] = np.empty(0, dtype=float)
    elif labels is not None:
        long_df["target"] = np.nan

    return long_df, feature_names


def _make_lgb_dataset(
    df: pd.DataFrame,
    feature_names: List[str],
    *,
    objective: str,
) -> lgb.Dataset:
    """Create a native ``lightgbm.Dataset`` with sorted per-date group counts for lambdarank."""
    x_mat = df[feature_names].to_numpy(dtype=float)
    y_vec = df["target"].to_numpy(dtype=float)
    if objective == "lambdarank":
        # df is already sorted by (date, symbol); groupby(sort=True) preserves order.
        groups = df.groupby("date", sort=True).size().to_numpy(dtype=int)
        return lgb.Dataset(
            x_mat,
            label=y_vec.astype(int),
            group=groups,
            feature_name=feature_names,
            free_raw_data=False,
        )
    return lgb.Dataset(
        x_mat,
        label=y_vec,
        feature_name=feature_names,
        free_raw_data=False,
    )


def _extract_gain_importance(
    booster: lgb.Booster, feature_names: List[str]
) -> Dict[str, float]:
    gains = booster.feature_importance(importance_type="gain")
    return {name: float(val) for name, val in zip(feature_names, gains)}


def run_purged_cv_oof(
    factors: Mapping[str, pd.DataFrame],
    labels: pd.DataFrame,
    *,
    cv_config: Optional[PurgedCVConfig] = None,
    combiner_config: Optional[CombinerConfig] = None,
    h: int = 6,
    dates_subset: Optional[pd.Index] = None,
    apply_lowdin: bool = False,
) -> CombinerCVResult:
    """Run Purged Walk-Forward CV and collect strictly out-of-fold predictions.

    Each test block is predicted exclusively by a model trained on its purged
    training set. Asserts ``assert_no_leakage`` on every fold and verifies
    that every OOF date/row is predicted at most once.
    """
    cv_cfg = cv_config if cv_config is not None else PurgedCVConfig()
    comb_cfg = combiner_config if combiner_config is not None else CombinerConfig()

    long_df, feature_names = prepare_long_dataset(
        factors,
        labels,
        dates_subset=dates_subset,
        apply_lowdin=apply_lowdin,
        objective=comb_cfg.objective,
        n_relevance_bins=comb_cfg.n_relevance_bins,
        require_label=True,
    )
    if long_df.empty:
        raise ValueError("No valid (feature, label) rows available for Purged CV.")

    unique_dates = pd.Index(np.sort(long_df["date"].unique()))
    splitter = PurgedGroupTimeSeriesCV(
        n_splits=cv_cfg.n_splits,
        h=h,
        embargo=cv_cfg.embargo_days,
        train_side=cv_cfg.train_side,
        min_train_dates=cv_cfg.min_train_dates,
        min_test_dates=cv_cfg.min_test_dates,
    )

    lgb_params = comb_cfg.to_lgb_params()
    oof_parts: List[pd.DataFrame] = []
    fold_importances: List[Dict[str, float]] = []
    seen_oof_dates: set = set()

    for train_rows, test_rows, fold in splitter.split_panel_rows(long_df["date"]):
        assert_no_leakage(
            fold.train_dates,
            fold.test_dates,
            h=h,
            embargo=cv_cfg.embargo_days,
            all_dates=unique_dates,
        )

        # Verify no duplicate date predictions across folds
        fold_test_set = set(fold.test_dates)
        dup_dates = seen_oof_dates.intersection(fold_test_set)
        if dup_dates:
            raise AssertionError(
                f"Duplicate OOF predictions detected across folds ({len(dup_dates)} dates)."
            )
        seen_oof_dates.update(fold_test_set)

        train_df = long_df.iloc[train_rows]
        test_df = long_df.iloc[test_rows]

        train_ds = _make_lgb_dataset(
            train_df, feature_names, objective=comb_cfg.objective
        )
        booster = lgb.train(
            lgb_params,
            train_ds,
            num_boost_round=comb_cfg.num_boost_round,
        )

        preds = booster.predict(test_df[feature_names].to_numpy(dtype=float))
        part = pd.DataFrame(
            {
                "date": test_df["date"].to_numpy(),
                "symbol": test_df["symbol"].to_numpy(),
                "score": preds.astype(float),
                "raw_label": test_df["raw_label"].to_numpy(dtype=float),
                "fold_id": int(fold.fold_id),
            }
        )
        oof_parts.append(part)
        fold_importances.append(_extract_gain_importance(booster, feature_names))

    if not oof_parts:
        raise ValueError("Purged CV produced zero valid folds.")

    oof_long = pd.concat(oof_parts, ignore_index=True)
    oof_long = oof_long.sort_values(["date", "symbol"]).reset_index(drop=True)
    if oof_long.duplicated(subset=["date", "symbol"]).any():
        raise AssertionError("Duplicate (date, symbol) entries in OOF predictions.")

    oof_scores = oof_long.pivot(index="date", columns="symbol", values="score")
    oof_scores = oof_scores.sort_index()

    return CombinerCVResult(
        oof_scores=oof_scores,
        oof_long=oof_long,
        fold_importances=fold_importances,
        feature_names=feature_names,
        n_folds_trained=len(oof_parts),
    )


def fit_predict_holdout(
    factors: Mapping[str, pd.DataFrame],
    labels: pd.DataFrame,
    train_dates: pd.Index,
    holdout_dates: pd.Index,
    *,
    combiner_config: Optional[CombinerConfig] = None,
    h: int = 6,
    embargo: int = 5,
    all_dates: Optional[pd.Index] = None,
    apply_lowdin: bool = False,
) -> HoldoutResult:
    """Fit native LightGBM on ``train_dates`` and predict on ``holdout_dates``.

    Enforces ``assert_no_leakage`` between ``train_dates`` and ``holdout_dates``
    before fitting. Holdout predictions are produced for every ``holdout_dates``
    row (even trailing rows whose forward labels are ``NaN``) so downstream
    backtesting covers the full holdout window.
    """
    comb_cfg = combiner_config if combiner_config is not None else CombinerConfig()
    assert_no_leakage(
        train_dates,
        holdout_dates,
        h=h,
        embargo=embargo,
        all_dates=all_dates,
    )

    train_df, feature_names = prepare_long_dataset(
        factors,
        labels,
        dates_subset=train_dates,
        apply_lowdin=apply_lowdin,
        objective=comb_cfg.objective,
        n_relevance_bins=comb_cfg.n_relevance_bins,
        require_label=True,
    )
    if train_df.empty:
        raise ValueError("Empty training set in fit_predict_holdout.")

    holdout_df, _ = prepare_long_dataset(
        factors,
        labels=None,
        dates_subset=holdout_dates,
        apply_lowdin=apply_lowdin,
        objective=comb_cfg.objective,
        n_relevance_bins=comb_cfg.n_relevance_bins,
        require_label=False,
    )
    if holdout_df.empty:
        raise ValueError("Empty holdout set in fit_predict_holdout.")

    train_ds = _make_lgb_dataset(
        train_df, feature_names, objective=comb_cfg.objective
    )
    booster = lgb.train(
        comb_cfg.to_lgb_params(),
        train_ds,
        num_boost_round=comb_cfg.num_boost_round,
    )

    preds = booster.predict(holdout_df[feature_names].to_numpy(dtype=float))
    pred_long = pd.DataFrame(
        {
            "date": holdout_df["date"].to_numpy(),
            "symbol": holdout_df["symbol"].to_numpy(),
            "score": preds.astype(float),
        }
    )
    holdout_scores = pred_long.pivot(index="date", columns="symbol", values="score").sort_index()

    return HoldoutResult(
        holdout_scores=holdout_scores,
        feature_importance=_extract_gain_importance(booster, feature_names),
        feature_names=feature_names,
        booster=booster,
    )

"""End-to-end 3-segment benchmark and Rule-9 empirical gate for ML1 Alpha Mining.

Timeline Isolation (Zero Selection Leakage):
::

    [GP discovery] -> gap(h + e) -> [Combiner Purged WF CV -> OOF] -> gap(h + e) -> [Holdout]

Holdout Comparison Arms (all simulated via ``run_allocation_backtest`` with friction):
- ``(a) lgbm_top_n``: LightGBM combiner top-N equal-weight (default ``lowdin_for_combiner``)
- ``(b) equal_weight_universe``: Equal-weight across all tradable assets in universe
  (``ic_summary: null`` -- a constant score panel has no IC by construction, so no
  degenerate ``ic=0.0`` is fabricated)
- ``(c) best_single_seed``: Best seed factor chosen strictly on discovery dates
- ``(d) equal_weight_gp_combo``: Equal-weight z-score average of discovered GP factors
  (with signed Löwdin enabled when ``lowdin_for_gp_library=True``)
- ``(e) lgbm_lowdin_on``: LightGBM combiner ablation with signed Löwdin enabled

Rule-9 Empirical Positive-Value Gate:
Passes iff on the holdout window:
1. ``holdout RankIC t-stat >= 2.0``
2. ``(a)`` RankIC >= ``(c)`` and ``(d)``
3. ``(a)`` net Sharpe >= ``(c)`` and ``(d)``
4. ``total_rebalances > 0`` and ``total_trades > 0`` (C4.1/C5.2 anti-starvation)
5. Zero monitoring alarms (adversarial validation, importance decay, turnover breaker)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from common.allocation_backtester import run_allocation_backtest
from common.factor_analyzer import asymmetric_long_only_factor_summary, factor_ic_summary

from amine.combiner import (
    _cs_zscore_preserve_nan,
    fit_predict_holdout,
    prepare_long_dataset,
    run_purged_cv_oof,
)
from amine.config import AlphaMiningConfig
from amine.fitness import build_seed_library, fast_rank_ic
from amine.gp import GPFactorMiner
from amine.labels import build_forward_labels
from amine.monitoring import (
    adversarial_validation,
    check_importance_decay,
    check_turnover_breaker,
)
from amine.orthogonalize import lowdin_orthogonalize_panel
from amine.purged_cv import split_timeline_three_way
from amine.strategy import AlphaMiningStrategy


def ohlcv_dict_from_universe(
    universe: Mapping[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """Convert ``{symbol: OHLCV_df}`` into ``{"open","high","low","close","volume"}`` panels."""
    symbols = list(universe.keys())
    if not symbols:
        raise ValueError("universe must be non-empty.")

    all_idx = pd.DatetimeIndex(
        sorted(set().union(*(df.index for df in universe.values() if not df.empty)))
    )
    if len(all_idx) == 0:
        raise ValueError("universe contains no dates.")

    close = pd.DataFrame(
        {s: universe[s]["Close"].reindex(all_idx) for s in symbols}, index=all_idx
    ).astype(float)
    open_ = pd.DataFrame(
        {
            s: (universe[s]["Open"] if "Open" in universe[s].columns else universe[s]["Close"]).reindex(all_idx)
            for s in symbols
        },
        index=all_idx,
    ).astype(float)
    high = pd.DataFrame(
        {
            s: (universe[s]["High"] if "High" in universe[s].columns else universe[s]["Close"]).reindex(all_idx)
            for s in symbols
        },
        index=all_idx,
    ).astype(float)
    low = pd.DataFrame(
        {
            s: (universe[s]["Low"] if "Low" in universe[s].columns else universe[s]["Close"]).reindex(all_idx)
            for s in symbols
        },
        index=all_idx,
    ).astype(float)
    volume = pd.DataFrame(
        {
            s: (
                universe[s]["Volume"].reindex(all_idx)
                if "Volume" in universe[s].columns
                else pd.Series(1.0, index=all_idx)
            )
            for s in symbols
        },
        index=all_idx,
    ).astype(float)

    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _slice_universe(
    universe: Mapping[str, pd.DataFrame], dates: pd.Index
) -> Dict[str, pd.DataFrame]:
    return {sym: df.loc[df.index.intersection(dates)] for sym, df in universe.items()}


def _clean_ic_summary(summary: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip non-JSON-serializable ``ic_series`` from ``factor_ic_summary``."""
    return {
        "mean_ic": float(summary.get("mean_ic", 0.0)),
        "std_ic": float(summary.get("std_ic", 0.0)),
        "ic_ir": float(summary.get("ic_ir", 0.0)),
        "t_stat": float(summary.get("t_stat", 0.0)),
        "p_value": float(summary.get("p_value", 1.0)),
        "n_obs": int(summary.get("n_obs", 0)),
        "is_significant": bool(summary.get("is_significant", False)),
        "is_statistically_significant": bool(summary.get("is_statistically_significant", False)),
        "half_life": float(summary.get("half_life", 0.0)),
    }


def _evaluate_arm(
    arm_name: str,
    score_panel: pd.DataFrame,
    holdout_universe: Dict[str, pd.DataFrame],
    holdout_labels: pd.DataFrame,
    *,
    top_n: int,
    rebalance_every: int,
    min_assets: int,
    commission_pct: float,
    slippage_pct: float,
    min_shares: int,
    compute_ic: bool = True,
) -> Dict[str, Any]:
    """Run ``AlphaMiningStrategy`` + ``run_allocation_backtest`` + ``factor_ic_summary``.

    ``compute_ic=False`` is used for arms whose score panel is constant by
    construction (e.g. ``equal_weight_universe``): ``factor_ic_summary`` would
    hard-code constant cross-sections to ``ic=0.0`` and emit a fake
    ``mean_ic=0.0, t_stat=0.0`` with full ``n_obs``. Such arms report
    ``ic_summary: null`` instead of a degenerate IC.

    ``warmup`` is intentionally NOT plumbed here: ``AlphaMiningStrategy`` never
    skips leading bars from it (the holdout score panel is already pre-warmed),
    so passing it through would be a no-op.
    """
    strategy = AlphaMiningStrategy(
        score_panel=score_panel,
        top_n=top_n,
        rebalance_every=rebalance_every,
        name=arm_name,
    )
    weights = strategy.generate_weights(holdout_universe)
    bt = run_allocation_backtest(
        holdout_universe,
        weights,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
        min_shares=min_shares,
    )
    rebal_report = bt.get("rebalance_report", pd.DataFrame())
    n_trades = int(len(rebal_report)) if isinstance(rebal_report, pd.DataFrame) else 0
    ic_sum = (
        _clean_ic_summary(
            factor_ic_summary(score_panel, holdout_labels, min_assets=min_assets)
        )
        if compute_ic
        else None
    )
    return {
        "arm": arm_name,
        "ic_summary": ic_sum,
        "sharpe_ratio": float(bt.get("sharpe_ratio", 0.0)),
        "cagr": float(bt.get("cagr", 0.0)),
        "total_return": float(bt.get("total_return", 0.0)),
        "max_drawdown": float(bt.get("max_drawdown", 0.0)),
        "total_turnover": float(bt.get("total_turnover", 0.0)),
        "total_rebalances": int(bt.get("total_rebalances", 0)),
        "total_trades": n_trades,
    }


def evaluate_rule9_gate(
    arms: Mapping[str, Mapping[str, Any]],
    monitoring_report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Evaluate the Rule-9 empirical positive-value gate on holdout results."""
    arm_a = arms["lgbm_top_n"]
    arm_c = arms["best_single_seed"]
    arm_d = arms["equal_weight_gp_combo"]

    t_stat = float(arm_a["ic_summary"]["t_stat"])
    ic_a = float(arm_a["ic_summary"]["mean_ic"])
    ic_c = float(arm_c["ic_summary"]["mean_ic"])
    ic_d = float(arm_d["ic_summary"]["mean_ic"])
    sr_a = float(arm_a["sharpe_ratio"])
    sr_c = float(arm_c["sharpe_ratio"])
    sr_d = float(arm_d["sharpe_ratio"])

    checks = {
        "holdout_t_stat_ge_2": bool(t_stat >= 2.0),
        "beats_seed_ic": bool(ic_a >= ic_c),
        "beats_seed_sharpe": bool(sr_a >= sr_c),
        "beats_eq_gp_ic": bool(ic_a >= ic_d),
        "beats_eq_gp_sharpe": bool(sr_a >= sr_d),
        "non_starvation": bool(
            int(arm_a["total_rebalances"]) > 0 and int(arm_a["total_trades"]) > 0
        ),
        "no_monitoring_alarm": bool(
            not monitoring_report["adversarial_validation"]["alarm"]
            and not monitoring_report["importance_decay"]["alarm"]
            and not monitoring_report["turnover_breaker"]["alarm"]
        ),
    }
    return {
        "passed": bool(all(checks.values())),
        "checks": checks,
    }


def run_alpha_mining_benchmark(
    universe: Mapping[str, pd.DataFrame],
    config: Optional[AlphaMiningConfig] = None,
    *,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
    min_shares: int = 1,
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """Execute the full 3-segment ML1 pipeline and return ``(report_dict, oof_scores_df)``."""
    cfg = config if config is not None else AlphaMiningConfig()
    ohlcv = ohlcv_dict_from_universe(universe)
    close = ohlcv["close"]

    labels = build_forward_labels(
        close,
        holding_period=cfg.holding_period,
        exec_lag=cfg.exec_lag,
        mode=cfg.label_mode,
        min_obs=cfg.residual_min_obs,
        sector_neutral=cfg.sector_neutral,
        sector_map_path=cfg.sector_map_path,
        sector_fallback=cfg.sector_fallback,
        drop_trailing_nan=False,
    )

    h = cfg.label_horizon
    embargo = cfg.cv.embargo_days
    all_dates = close.index

    split = split_timeline_three_way(
        all_dates,
        h=h,
        embargo=embargo,
        discovery_ratio=cfg.discovery_ratio,
        combiner_ratio=cfg.combiner_ratio,
        holdout_ratio=cfg.holdout_ratio,
        min_segment_dates=max(
            10, cfg.cv.min_train_dates + cfg.cv.n_splits * cfg.cv.min_test_dates + h + embargo
        )
        if len(all_dates) >= 3 * (cfg.cv.min_train_dates + cfg.cv.n_splits * cfg.cv.min_test_dates + h + embargo) + 2 * (h + embargo)
        else 10,
    )

    # Seed library
    seed_lib = build_seed_library(ohlcv)

    # Stage 1: GP discovery strictly on discovery_dates
    miner = GPFactorMiner(cfg.gp)
    selected_factors = miner.mine(
        ohlcv,
        labels,
        discovery_dates=split.discovery_dates,
        seed_library=seed_lib,
    )

    gp_fallback_used = False
    gp_panels: Dict[str, pd.DataFrame] = {}
    gp_records: List[Dict[str, Any]] = []

    if selected_factors:
        for f in selected_factors:
            if f.node is not None:
                gp_panels[f.name] = miner.evaluate_expression_on_panel(
                    f.node, ohlcv, sign=f.sign
                )
            else:
                gp_panels[f.name] = f.panel.reindex(index=all_dates, columns=close.columns)
            gp_records.append(
                {
                    "name": f.name,
                    "expression": f.expression,
                    "sign": float(f.sign),
                    "fitness": float(f.fitness),
                    "node_count": int(f.node_count),
                    "max_window": int(f.max_window),
                    "discovery_ic_summary": _clean_ic_summary(f.ic_summary),
                }
            )
    else:
        gp_fallback_used = True
        gp_panels = dict(seed_lib)

    # Stage 2: Purged Walk-Forward CV -> OOF strictly on combiner_dates
    cv_res = run_purged_cv_oof(
        gp_panels,
        labels,
        cv_config=cfg.cv,
        combiner_config=cfg.combiner,
        h=h,
        dates_subset=split.combiner_dates,
        apply_lowdin=cfg.lowdin_for_combiner,
    )
    oof_ic = _clean_ic_summary(
        factor_ic_summary(
            cv_res.oof_scores,
            labels.loc[split.combiner_dates],
            min_assets=cfg.gp.min_assets,
        )
    )
    oof_asym_raw = asymmetric_long_only_factor_summary(
        cv_res.oof_scores,
        labels.loc[split.combiner_dates],
        top_k=cfg.top_n,
        min_assets=cfg.gp.min_assets,
        annualization_days=max(1, int(round(252 / max(1, cfg.holding_period)))),
    )
    oof_asym = {
        k: (None if isinstance(v, float) and not np.isfinite(v) else v)
        for k, v in oof_asym_raw.items()
    }

    # Holdout predictions: arm (a) LightGBM top-N and arm (e) LightGBM + Löwdin on
    holdout_a = fit_predict_holdout(
        gp_panels,
        labels,
        train_dates=split.combiner_dates,
        holdout_dates=split.holdout_dates,
        combiner_config=cfg.combiner,
        h=h,
        embargo=embargo,
        all_dates=all_dates,
        apply_lowdin=cfg.lowdin_for_combiner,
    )
    if len(gp_panels) >= 2:
        holdout_e = fit_predict_holdout(
            gp_panels,
            labels,
            train_dates=split.combiner_dates,
            holdout_dates=split.holdout_dates,
            combiner_config=cfg.combiner,
            h=h,
            embargo=embargo,
            all_dates=all_dates,
            apply_lowdin=True,
        )
        scores_e = holdout_e.holdout_scores
    else:
        scores_e = holdout_a.holdout_scores

    # Arm (b): equal-weight universe (constant score 1.0, top_n = all symbols)
    scores_b = pd.DataFrame(
        1.0, index=split.holdout_dates, columns=close.columns, dtype=float
    )

    # Arm (c): best single seed factor chosen strictly on discovery_dates
    best_seed_name = ""
    best_seed_abs_ic = -1.0
    best_seed_sign = 1.0
    disc_labels = labels.loc[split.discovery_dates]
    for s_name, s_df in seed_lib.items():
        ic_s = fast_rank_ic(
            s_df.loc[split.discovery_dates], disc_labels, min_assets=cfg.gp.min_assets
        ).dropna()
        m_ic = float(ic_s.mean()) if not ic_s.empty else 0.0
        if abs(m_ic) > best_seed_abs_ic:
            best_seed_abs_ic = abs(m_ic)
            best_seed_name = s_name
            best_seed_sign = 1.0 if m_ic >= 0.0 else -1.0
    scores_c = seed_lib[best_seed_name].loc[split.holdout_dates] * best_seed_sign

    # Arm (d): equal-weight z-combo of GP factors (with Löwdin when enabled)
    z_gp_holdout = {
        name: _cs_zscore_preserve_nan(df.loc[split.holdout_dates])
        for name, df in gp_panels.items()
    }
    if cfg.lowdin_for_gp_library and len(z_gp_holdout) >= 2:
        z_gp_holdout, _ = lowdin_orthogonalize_panel(z_gp_holdout)
    scores_d = sum(z_gp_holdout.values()) / float(len(z_gp_holdout))

    # Backtest all 5 arms on holdout
    holdout_universe = _slice_universe(universe, split.holdout_dates)
    holdout_labels = labels.loc[split.holdout_dates]

    # FIX 3a diagnostics: the holdout backtest runs to the last data row, while the
    # forward labels leave the trailing ``h = holding_period + exec_lag`` rows NaN,
    # so the label-valid IC window stops ``h`` bars earlier than the backtest window.
    label_row_valid = holdout_labels.notna().any(axis=1).to_numpy()
    n_trailing_unlabeled = 0
    for flag in label_row_valid[::-1]:
        if flag:
            break
        n_trailing_unlabeled += 1
    valid_positions = np.flatnonzero(label_row_valid)
    if len(valid_positions) > 0:
        ic_valid_start = str(split.holdout_dates[int(valid_positions[0])].date())
        ic_valid_end = str(split.holdout_dates[int(valid_positions[-1])].date())
    else:
        ic_valid_start = None
        ic_valid_end = None
    window_alignment = {
        "label_horizon_h": int(h),
        "holdout_backtest_start": str(split.holdout_dates[0].date()),
        "holdout_backtest_end": str(split.holdout_dates[-1].date()),
        "holdout_backtest_bars": int(len(split.holdout_dates)),
        "holdout_ic_valid_start": ic_valid_start,
        "holdout_ic_valid_end": ic_valid_end,
        "holdout_ic_valid_bars": int(label_row_valid.sum()),
        "holdout_trailing_unlabeled_bars": int(n_trailing_unlabeled),
        "note": (
            "Holdout Sharpe/CAGR are simulated over the full holdout bar range, "
            "which extends label_horizon_h bars beyond the label-valid window used "
            "for holdout RankIC/t_stat validation."
        ),
    }

    common_bt_kwargs = dict(
        holdout_universe=holdout_universe,
        holdout_labels=holdout_labels,
        rebalance_every=cfg.holding_period,
        min_assets=cfg.gp.min_assets,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
        min_shares=min_shares,
    )
    arms = {
        "lgbm_top_n": _evaluate_arm(
            "lgbm_top_n", holdout_a.holdout_scores, top_n=cfg.top_n, **common_bt_kwargs
        ),
        "equal_weight_universe": _evaluate_arm(
            "equal_weight_universe",
            scores_b,
            top_n=len(holdout_universe),
            compute_ic=False,
            **common_bt_kwargs,
        ),
        "best_single_seed": _evaluate_arm(
            f"best_single_seed:{best_seed_name}",
            scores_c,
            top_n=cfg.top_n,
            **common_bt_kwargs,
        ),
        "equal_weight_gp_combo": _evaluate_arm(
            "equal_weight_gp_combo", scores_d, top_n=cfg.top_n, **common_bt_kwargs
        ),
        "lgbm_lowdin_on": _evaluate_arm(
            "lgbm_lowdin_on", scores_e, top_n=cfg.top_n, **common_bt_kwargs
        ),
    }

    # Monitoring diagnostics (report-only in v1)
    ref_long, feat_cols = prepare_long_dataset(
        gp_panels,
        labels=None,
        dates_subset=split.combiner_dates,
        apply_lowdin=cfg.lowdin_for_combiner,
        require_label=False,
    )
    rec_long, _ = prepare_long_dataset(
        gp_panels,
        labels=None,
        dates_subset=split.holdout_dates,
        apply_lowdin=cfg.lowdin_for_combiner,
        require_label=False,
    )
    adv_res = adversarial_validation(
        ref_long,
        rec_long,
        feat_cols,
        n_splits=cfg.monitoring.adv_n_splits,
        auc_threshold=cfg.monitoring.adv_auc_threshold,
        seed=cfg.seed,
    )
    imp_res = check_importance_decay(
        cv_res.fold_importances,
        spearman_threshold=cfg.monitoring.imp_spearman_threshold,
        top3_drop_ratio=cfg.monitoring.imp_top3_drop_ratio,
    )
    to_res = check_turnover_breaker(
        cv_res.oof_scores,
        top_n=cfg.top_n,
        window=cfg.monitoring.turnover_window,
        threshold=cfg.monitoring.turnover_threshold,
    )

    monitoring_report = {
        "report_only": True,
        "adversarial_validation": {
            "auc": None if not np.isfinite(adv_res.auc) else float(adv_res.auc),
            "alarm": bool(adv_res.alarm),
            "threshold": float(adv_res.threshold),
            "n_reference": int(adv_res.n_reference),
            "n_recent": int(adv_res.n_recent),
            "top_features_by_gain": [
                {"feature": f, "gain_share": float(g)}
                for f, g in adv_res.top_features_by_gain[:10]
            ],
        },
        "importance_decay": {
            "alarm": bool(imp_res.alarm),
            "min_spearman": float(imp_res.min_spearman),
            "pair_spearmans": [float(x) for x in imp_res.pair_spearmans],
            "dropped_top3_features": imp_res.dropped_top3_features,
        },
        "turnover_breaker": {
            "alarm": bool(to_res.alarm),
            "max_rolling_turnover": float(to_res.max_rolling_turnover),
            "mean_daily_turnover": float(to_res.mean_daily_turnover),
            "threshold": float(to_res.threshold),
            "window": int(to_res.window),
            "recommended_action": to_res.recommended_action,
        },
    }

    gate = evaluate_rule9_gate(arms, monitoring_report)

    report = {
        "strategy_version": AlphaMiningStrategy.version,
        "config": asdict(cfg),
        "timeline": {
            "n_total_dates": int(len(all_dates)),
            "gap_size": int(split.gap_size),
            "discovery_start": str(split.discovery_dates[0].date()),
            "discovery_end": str(split.discovery_dates[-1].date()),
            "n_discovery_dates": int(len(split.discovery_dates)),
            "combiner_start": str(split.combiner_dates[0].date()),
            "combiner_end": str(split.combiner_dates[-1].date()),
            "n_combiner_dates": int(len(split.combiner_dates)),
            "holdout_start": str(split.holdout_dates[0].date()),
            "holdout_end": str(split.holdout_dates[-1].date()),
            "n_holdout_dates": int(len(split.holdout_dates)),
        },
        "stage1_gp": {
            "n_selected": len(gp_records),
            "fallback_to_seeds": gp_fallback_used,
            "best_seed_factor": best_seed_name,
            "factors": gp_records,
        },
        "stage2_combiner_oof": {
            "n_folds_trained": int(cv_res.n_folds_trained),
            "n_oof_dates": int(len(cv_res.oof_scores)),
            "feature_names": cv_res.feature_names,
            "ic_summary": oof_ic,
            "asymmetric_long_only_summary": oof_asym,
        },
        "holdout_arms": arms,
        "window_alignment": window_alignment,
        "monitoring": monitoring_report,
        "rule9_gate": gate,
    }
    return report, cv_res.oof_scores

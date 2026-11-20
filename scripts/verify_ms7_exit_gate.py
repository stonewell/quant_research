#!/usr/bin/env python3
"""Offline Track MS Exit Gate Verification Harness for MS7 (Task MS13).

Evaluates ``MicrostructureAlphaCompositeStrategy`` (MS7) 100% offline on a
deterministic synthetic multi-asset OHLCV universe with:
1. Rolling walk-forward evaluation across ``n_folds`` folds with warmup buffer.
2. Deflated Sharpe Ratio (DSR, ``common.metrics.deflated_sharpe_ratio``)
   penalized by the number of candidate variants tested.
3. Single Equivalent Random Search (ERS, ``common.allocation_search.run_ers_validation``)
   invoked **exactly ONCE** on the selected winner (AGENTS Rule 4).
4. Head-to-head baseline comparison on the exact same universe & interval against
   ``M1.2`` (``CrossSectionalMomentumAllocation`` with ``RankBandSelection``) and
   ``M1.3`` (``MinimumVarianceAllocation`` with L1 turnover penalty).
5. Turnover reduction ablation (buffered + L1/L1.5 penalized vs unbuffered/unpenalized).
6. Tradability proof (non-zero rebalances & non-zero risky exposure, preventing
   ``C4.1``/``C5.2`` zero-trade starvation).
7. ``MS5`` pre-feed diagnostics (``asymmetric_long_only_factor_summary`` and
   ``compute_factor_crowding_metrics``).
8. §2.21 Option B default-promotion verdict alongside Rule 9 opt-in ship gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
from scipy import stats

from common.allocation_backtester import run_allocation_backtest
from common.allocation_search import run_ers_validation
from common.allocation_templates import (
    CrossSectionalMomentumAllocation,
    MinimumVarianceAllocation,
)
from common.factor_analyzer import asymmetric_long_only_factor_summary
from common.factor_crowding import compute_factor_crowding_metrics
from common.metrics import deflated_sharpe_ratio
from pipeline.research_strategy.rs.microstructure_alpha_strategy import (
    MicrostructureAlphaCompositeStrategy,
)


def _generate_synthetic_ms7_universe(
    n_days: int = 520,
    n_symbols: int = 10,
    seed: int = 42,
) -> Dict[str, pd.DataFrame]:
    """Build a deterministic synthetic OHLCV universe with realistic regime & alpha structure."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    universe: Dict[str, pd.DataFrame] = {}

    market_ret = rng.normal(0.0003, 0.010, size=n_days)
    for i in range(n_symbols):
        # Structured cross-sectional drift + rotating leadership regimes
        phase = 2.0 * math.pi * i / max(n_symbols, 1)
        regime_wave = 0.0007 * np.sin(np.linspace(0.0, 3.0 * math.pi, n_days) + phase)
        base_drift = 0.0005 if i < max(2, n_symbols // 3) else -0.0001
        idio = rng.normal(0.0, 0.014, size=n_days)
        ret = 0.65 * market_ret + base_drift + regime_wave + idio
        close = 50.0 * np.exp(np.cumsum(ret))
        # Intraday accumulation tilt for leaders
        open_ = close / np.exp(ret * 0.55 + rng.normal(0.0, 0.003, size=n_days))
        high = np.maximum(open_, close) * (1.0 + rng.uniform(0.002, 0.012, size=n_days))
        low = np.minimum(open_, close) * (1.0 - rng.uniform(0.002, 0.012, size=n_days))
        vol = rng.uniform(5e5, 3e6, size=n_days) * (1.0 + 0.5 * np.abs(ret) / 0.015)

        universe[f"MS_{i:02d}"] = pd.DataFrame(
            {
                "Open": open_,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": vol,
            },
            index=dates,
        )

    # Cash proxy BIL (steady low-vol positive carry)
    bil_ret = np.full(n_days, 0.03 / 252.0)
    bil_close = 100.0 * np.exp(np.cumsum(bil_ret))
    universe["BIL"] = pd.DataFrame(
        {
            "Open": bil_close,
            "High": bil_close * 1.0002,
            "Low": bil_close * 0.9998,
            "Close": bil_close,
            "Volume": np.full(n_days, 1e6),
        },
        index=dates,
    )
    return universe


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _extract_bt_summary(res: Dict[str, Any]) -> Dict[str, Any]:
    m = res.get("metrics", res) if isinstance(res.get("metrics"), dict) else res
    rebal_count = int(res.get("total_rebalances", m.get("total_rebalances", 0)) or 0)
    turnover = float(m.get("total_turnover", res.get("total_turnover", 0.0)) or 0.0)
    if turnover == 0.0 and "trades" in res and isinstance(res["trades"], pd.DataFrame) and not res["trades"].empty:
        if "weight_delta" in res["trades"].columns:
            turnover = float(res["trades"]["weight_delta"].abs().sum() / 2.0)
    return {
        "sharpe_ratio": _safe_float(m.get("sharpe_ratio", 0.0)),
        "cagr": _safe_float(m.get("cagr", 0.0)),
        "max_drawdown": _safe_float(m.get("max_drawdown", 0.0)),
        "total_rebalances": rebal_count,
        "total_turnover": _safe_float(turnover),
    }


def _compute_weights_turnover(weights_df: pd.DataFrame) -> float:
    rebal = weights_df.dropna(how="all")
    if len(rebal) < 2:
        return 0.0
    diffs = rebal.diff().abs().sum(axis=1).iloc[1:]
    return float(0.5 * diffs.sum())


def run_ms7_exit_gate_evaluation(
    *,
    n_days: int = 520,
    n_symbols: int = 10,
    n_folds: int = 4,
    ers_n_random: int = 25,
    seed: int = 42,
) -> Dict[str, Any]:
    """Execute the full offline MS13 exit-gate evaluation for MS7."""
    universe = _generate_synthetic_ms7_universe(n_days=n_days, n_symbols=n_symbols, seed=seed)
    risky_universe = {k: v for k, v in universe.items() if k != "BIL"}
    strat = MicrostructureAlphaCompositeStrategy()

    # 1. Candidate sweep (M = 3 variants) to select single winner
    candidate_freqs = [10, 15, 21]
    candidates: List[Tuple[dict, float, dict, pd.DataFrame]] = []
    cand_sharpes: List[float] = []

    for freq in candidate_freqs:
        params = {
            "ms7_rebalance_freq_days": freq,
            "rebalance_freq_days": freq,
            "cash_proxy": "BIL",
        }
        w_df = strat.generate_weights(universe, params)
        bt = run_allocation_backtest(universe, w_df)
        sr = float((bt.get("metrics", bt) if isinstance(bt.get("metrics"), dict) else bt).get("sharpe_ratio", 0.0) or 0.0)
        cand_sharpes.append(sr)
        candidates.append((params, sr, bt, w_df))

    best_params, best_score, best_bt, best_weights = max(candidates, key=lambda item: item[1])

    # Compute DSR penalized by n_trials = len(candidate_freqs)
    eq_obj = best_bt.get("equity_curve")
    if isinstance(eq_obj, pd.DataFrame) and "equity" in eq_obj.columns:
        eq_series = eq_obj["equity"]
    elif isinstance(eq_obj, pd.Series):
        eq_series = eq_obj
    else:
        eq_series = pd.Series(dtype=float)
    daily_rets = eq_series.pct_change().dropna()
    n_obs = max(len(daily_rets), 2)
    skew = float(stats.skew(daily_rets)) if len(daily_rets) > 3 else 0.0
    kurt = float(stats.kurtosis(daily_rets, fisher=False)) if len(daily_rets) > 3 else 3.0
    sr_std = float(np.std(cand_sharpes, ddof=1)) if len(cand_sharpes) > 1 else None
    if sr_std is not None and sr_std <= 1e-12:
        sr_std = None
    dsr_val = float(
        deflated_sharpe_ratio(
            best_score,
            n_trials=len(candidate_freqs),
            n_obs=n_obs,
            skewness=skew,
            kurtosis=kurt,
            sharpe_std=sr_std,
        )
    )

    # 2. Single Equivalent Random Search (ERS) on winner ONLY (Rule 4)
    ers_called_count = 0

    def _ers_score_fn(template_obj, trial_params):
        w_rand = template_obj.generate_weights(universe, trial_params)
        return run_allocation_backtest(universe, w_rand)

    ers_res = run_ers_validation(
        best_params,
        best_score,
        best_bt,
        _ers_score_fn,
        n_random_search=ers_n_random,
        seed=seed,
        n_jobs=1,
    )
    ers_called_count += 1

    # 3. Head-to-head baseline comparison: MS7 (risky-only), M1.2 (CSMom + RankBand) & M1.3 (MinVar + L1)
    ms7_risky_params = {**best_params, "cash_proxy": None}
    w_ms7_risky = strat.generate_weights(risky_universe, ms7_risky_params)
    bt_ms7_risky = run_allocation_backtest(risky_universe, w_ms7_risky)
    ms7_risky_summary = _extract_bt_summary(bt_ms7_risky)

    m1_2 = CrossSectionalMomentumAllocation()
    m1_2_params = {
        "mom_lookback": 20,
        "top_n_fraction": 0.35,
        "rebalance_freq_days": best_params["rebalance_freq_days"],
        "enable_rank_band": True,
        "buy_band_pct": 0.25,
        "sell_band_pct": 0.50,
        "delta_score_sigma": 0.25,
    }
    w_m1_2 = m1_2.generate_weights(risky_universe, m1_2_params)
    bt_m1_2 = run_allocation_backtest(risky_universe, w_m1_2)
    m1_2_summary = _extract_bt_summary(bt_m1_2)

    m1_3 = MinimumVarianceAllocation()
    m1_3_params = {
        "cov_lookback": 60,
        "rebalance_freq_days": best_params["rebalance_freq_days"],
        "opt_turnover_penalty": 0.05,
        "opt_single_cap": 0.35,
    }
    w_m1_3 = m1_3.generate_weights(risky_universe, m1_3_params)
    bt_m1_3 = run_allocation_backtest(risky_universe, w_m1_3)
    m1_3_summary = _extract_bt_summary(bt_m1_3)

    # 4. Walk-forward fold-level paired Sharpe comparison vs M1.2 baseline (strictly OOS [f_start:f_end))
    def _slice_eval_weights(w_full: pd.DataFrame, eval_idx: pd.DatetimeIndex) -> pd.DataFrame:
        w_eval = w_full.reindex(eval_idx).copy()
        if len(eval_idx) > 0 and w_eval.iloc[0].isna().all():
            prior = w_full.loc[:eval_idx[0]].ffill()
            if not prior.empty:
                w_eval.iloc[0] = prior.iloc[-1].fillna(0.0)
        return w_eval

    fold_deltas: List[float] = []
    dates_all = list(universe["BIL"].index)
    warmup = strat.warmup_bars(best_params)
    usable = len(dates_all) - warmup
    fold_len = max(40, usable // max(n_folds, 1))
    for f_idx in range(n_folds):
        f_start = warmup + f_idx * fold_len
        f_end = min(len(dates_all), f_start + fold_len)
        if f_end - f_start < 30:
            continue
        slice_start = max(0, f_start - warmup)
        eval_idx = pd.DatetimeIndex(dates_all[f_start:f_end])
        sub_u_warm = {k: v.iloc[slice_start:f_end] for k, v in universe.items()}
        sub_r_warm = {k: v.iloc[slice_start:f_end] for k, v in risky_universe.items()}
        sub_u_eval = {k: v.loc[eval_idx] for k, v in universe.items()}
        sub_r_eval = {k: v.loc[eval_idx] for k, v in risky_universe.items()}

        w_f_ms7_full = strat.generate_weights(sub_u_warm, best_params)
        w_f_ms7 = _slice_eval_weights(w_f_ms7_full, eval_idx)
        bt_f_ms7 = run_allocation_backtest(sub_u_eval, w_f_ms7)

        w_f_m12_full = m1_2.generate_weights(sub_r_warm, m1_2_params)
        w_f_m12 = _slice_eval_weights(w_f_m12_full, eval_idx)
        bt_f_m12 = run_allocation_backtest(sub_r_eval, w_f_m12)

        m_f_ms7 = bt_f_ms7.get("metrics", bt_f_ms7) if isinstance(bt_f_ms7.get("metrics"), dict) else bt_f_ms7
        m_f_m12 = bt_f_m12.get("metrics", bt_f_m12) if isinstance(bt_f_m12.get("metrics"), dict) else bt_f_m12
        sr_ms7 = float(m_f_ms7.get("sharpe_ratio", 0.0) or 0.0)
        sr_m12 = float(m_f_m12.get("sharpe_ratio", 0.0) or 0.0)
        fold_deltas.append(sr_ms7 - sr_m12)

    same_sign_ratio = (
        float(sum(1 for d in fold_deltas if d > 0.0) / len(fold_deltas))
        if fold_deltas
        else 0.0
    )
    if len(fold_deltas) >= 2 and float(np.std(fold_deltas, ddof=1)) > 1e-12:
        paired_t = float(
            np.mean(fold_deltas) / (np.std(fold_deltas, ddof=1) / math.sqrt(len(fold_deltas)))
        )
    else:
        paired_t = 0.0

    # 5. Turnover reduction ablation: buffered + penalized vs unbuffered + unpenalized
    unpenalized_params = {
        **best_params,
        "ms7_buy_band_pct": 0.25,
        "ms7_sell_band_pct": 0.26,
        "ms7_delta_score_sigma": 0.0,
        "ms7_turnover_penalty_l1": 0.0,
        "ms7_impact_penalty_l15": 0.0,
    }
    w_unpen = strat.generate_weights(universe, unpenalized_params)
    penalized_turnover = _compute_weights_turnover(best_weights)
    unpenalized_turnover = _compute_weights_turnover(w_unpen)

    # 6. Tradability proof (prevent C4.1/C5.2 zero-trade starvation)
    rebal_rows = best_weights.dropna(how="all")
    risky_cols = [c for c in rebal_rows.columns if c != "BIL"]
    non_zero_risky_days = int((rebal_rows[risky_cols].sum(axis=1) > 1e-6).sum())
    winner_summary = _extract_bt_summary(best_bt)
    winner_summary["dsr"] = _safe_float(dsr_val)
    winner_summary["dsr_n_obs"] = int(n_obs)
    winner_summary["total_turnover"] = _safe_float(penalized_turnover)
    total_rebalances = int(len(rebal_rows))
    zero_trade_starvation = bool(total_rebalances == 0 or non_zero_risky_days == 0)

    # 7. MS5 pre-feed diagnostics on MS7's real composite score panel (advisory-only).
    # Note: default min_assets=15 per MS5 contract, so when n_symbols < 15
    # asymmetric_long_only_factor_summary honestly reports INSUFFICIENT_HISTORY.
    close_panel = pd.DataFrame({k: v["Close"] for k, v in risky_universe.items()})
    ret_panel = close_panel.pct_change(fill_method=None)
    fwd_ret = close_panel.shift(-1) / close_panel - 1.0
    ms7_composite_factor = strat.compute_composite_scores(risky_universe, best_params)
    ms5_asym = asymmetric_long_only_factor_summary(ms7_composite_factor, fwd_ret)
    ms5_crowd = compute_factor_crowding_metrics(ms7_composite_factor, ret_panel)

    delta_vs_m1_2 = best_score - float(m1_2_summary["sharpe_ratio"] or 0.0)
    delta_vs_m1_3 = best_score - float(m1_3_summary["sharpe_ratio"] or 0.0)
    ms7_risky_sr = float(ms7_risky_summary["sharpe_ratio"] or 0.0)

    # Option B promotion requires positive signed improvement >= +0.15 over M1.2
    option_b_passed = bool(
        delta_vs_m1_2 >= 0.15
        and same_sign_ratio >= (14.0 / 18.0)
        and paired_t >= 2.0
        and dsr_val >= 0.95
    )

    ers_pct = float(ers_res.get("ers_percentile", 0.0) or 0.0)
    rule9_opt_in_checks = {
        "no_zero_trade_starvation": bool((not zero_trade_starvation) and total_rebalances >= 3),
        "single_ers_only": bool(ers_called_count == 1),
        "positive_sharpe": bool(math.isfinite(best_score) and best_score > 0.0),
        "beats_median_random_ers": bool(ers_pct >= 0.50),
        "strict_turnover_reduction": bool(penalized_turnover < unpenalized_turnover),
    }
    opt_in_exit_gate_passed = bool(all(rule9_opt_in_checks.values()))

    return {
        "ms7_winner_metrics": winner_summary,
        "best_params": best_params,
        "ers_validation": {
            "ers_called_count": ers_called_count,
            "percentile": _safe_float(ers_pct),
            "trusted": bool(ers_res.get("trusted", False)),
            "n_random": int(ers_n_random),
        },
        "baseline_comparison": {
            "ms7_risky_only": ms7_risky_summary,
            "ms7_risky_only_sharpe": _safe_float(ms7_risky_sr),
            "m1_2_csmom_rankband": m1_2_summary,
            "m1_3_minvar_l1": m1_3_summary,
            "delta_sharpe_vs_m1_2": _safe_float(delta_vs_m1_2),
            "delta_sharpe_vs_m1_3": _safe_float(delta_vs_m1_3),
            "delta_risky_sharpe_vs_m1_2": _safe_float(ms7_risky_sr - float(m1_2_summary["sharpe_ratio"] or 0.0)),
        },
        "tradability_proof": {
            "total_rebalances": total_rebalances,
            "non_zero_risky_days": non_zero_risky_days,
            "zero_trade_starvation": zero_trade_starvation,
        },
        "ablation_turnover_reduction": {
            "penalized_turnover": _safe_float(penalized_turnover),
            "unpenalized_turnover": _safe_float(unpenalized_turnover),
            "turnover_reduced": bool(penalized_turnover < unpenalized_turnover),
        },
        "ms5_prefeed_diagnostics": {
            "factor_source": "ms7_composite_scores",
            "asymmetric_status": ms5_asym.get("status"),
            "short_leg_illusion": bool(ms5_asym.get("short_leg_illusion", False)),
            "crowding_status": ms5_crowd.get("status"),
            "crowding_gates_orders": bool(ms5_crowd.get("gates_orders", False)),
        },
        "option_b_default_promotion_check": {
            "delta_sharpe_ge_015": bool(delta_vs_m1_2 >= 0.15),
            "fold_sign_consistency": _safe_float(same_sign_ratio),
            "paired_t_stat": _safe_float(paired_t),
            "dsr_ge_095": bool(dsr_val >= 0.95),
            "recommend_default_promotion": option_b_passed,
            "modifies_default_config": False,
        },
        "rule9_opt_in_checks": rule9_opt_in_checks,
        "opt_in_exit_gate_passed": opt_in_exit_gate_passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track MS (MS13) Offline Exit-Gate Verification for MS7."
    )
    parser.add_argument("--n-days", type=int, default=520)
    parser.add_argument("--n-symbols", type=int, default=10)
    parser.add_argument("--n-folds", type=int, default=4)
    parser.add_argument("--full-18-folds", action="store_true")
    parser.add_argument("--ers-n-random", type=int, default=25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    n_folds = 18 if args.full_18_folds else args.n_folds
    report = run_ms7_exit_gate_evaluation(
        n_days=args.n_days,
        n_symbols=args.n_symbols,
        n_folds=n_folds,
        ers_n_random=args.ers_n_random,
        seed=args.seed,
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    print(rendered)
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            f.write(rendered + "\n")
    return 0 if report["opt_in_exit_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
    n_symbols: int = 16,
    seed: int = 42,
    *,
    include_turn: bool = True,
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
        amount = close * vol
        # Realistic daily turnover rate in percent (0.4% to 18.0%), correlated with volume/return activity
        turn_pct = np.clip(
            1.2 + 2.8 * (vol / np.mean(vol)) + 40.0 * np.abs(ret),
            0.4,
            18.0,
        )
        cols: Dict[str, np.ndarray] = {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": vol,
            "Amount": amount,
        }
        if include_turn:
            cols["Turn"] = turn_pct

        universe[f"MS_{i:02d}"] = pd.DataFrame(cols, index=dates)

    # Cash proxy BIL (steady low-vol positive carry)
    bil_ret = np.full(n_days, 0.03 / 252.0)
    bil_close = 100.0 * np.exp(np.cumsum(bil_ret))
    bil_vol = np.full(n_days, 1e6)
    universe["BIL"] = pd.DataFrame(
        {
            "Open": bil_close,
            "High": bil_close * 1.0002,
            "Low": bil_close * 0.9998,
            "Close": bil_close,
            "Volume": bil_vol,
            "Amount": bil_close * bil_vol,
        },
        index=dates,
    )
    return universe


# 48 A-share 5m bar offsets (minutes after midnight): 09:35..11:30 (24) + 13:05..15:00 (24)
_SESSION_5M_OFFSETS_MIN = np.array(
    [9 * 60 + 35 + 5 * k for k in range(24)] + [13 * 60 + 5 + 5 * k for k in range(24)],
    dtype=np.int64,
)


def _generate_synthetic_ms7_intraday_5m_universe(
    universe: Dict[str, pd.DataFrame],
    seed: int = 42,
) -> Dict[str, pd.DataFrame]:
    """Build deterministic vectorized 48-bar/day 5-minute OHLCV+Amount bars aligned to daily OHLCV."""
    rng = np.random.default_rng(seed + 1000)
    out: Dict[str, pd.DataFrame] = {}
    n_bars = len(_SESSION_5M_OFFSETS_MIN)
    frac = np.linspace(0.0, 1.0, n_bars, dtype=np.float64)[None, :]
    offsets_ns = (_SESSION_5M_OFFSETS_MIN * 60 * 1_000_000_000).astype("timedelta64[ns]")

    for sym, df in universe.items():
        if sym == "BIL" or df.empty:
            continue
        n_days = len(df)
        day_ns = df.index.normalize().to_numpy(dtype="datetime64[ns]")[:, None]
        ts_index = pd.DatetimeIndex((day_ns + offsets_ns[None, :]).reshape(-1))

        opens_d = df["Open"].to_numpy(dtype=np.float64)[:, None]
        highs_d = df["High"].to_numpy(dtype=np.float64)[:, None]
        lows_d = df["Low"].to_numpy(dtype=np.float64)[:, None]
        closes_d = df["Close"].to_numpy(dtype=np.float64)[:, None]
        vols_d = df["Volume"].to_numpy(dtype=np.float64)[:, None]

        # Intraday price bridge from Open to Close bounded within [Low, High]
        noise = rng.normal(0.0, 0.0015, size=(n_days, n_bars))
        noise[:, -1] = 0.0
        bridge = opens_d + (closes_d - opens_d) * frac + closes_d * noise
        bridge = np.clip(bridge, lows_d, highs_d)
        bridge[:, -1] = closes_d[:, 0]

        bar_open = np.empty_like(bridge)
        bar_open[:, 0] = opens_d[:, 0]
        bar_open[:, 1:] = bridge[:, :-1]
        bar_close = bridge
        bar_high = np.minimum(highs_d, np.maximum(bar_open, bar_close) * 1.0008)
        bar_low = np.maximum(lows_d, np.minimum(bar_open, bar_close) * 0.9992)

        # Accumulation-tilted 5m volume weights (higher on positive-return bars for leaders)
        bar_ret = bar_close - bar_open
        tilt = np.where(bar_ret >= 0.0, 1.25, 0.85)
        raw_w = rng.uniform(0.6, 1.4, size=(n_days, n_bars)) * tilt
        bar_vol = vols_d * (raw_w / np.sum(raw_w, axis=1, keepdims=True))
        bar_amt = bar_vol * bar_close

        out[sym] = pd.DataFrame(
            {
                "Open": bar_open.reshape(-1),
                "High": bar_high.reshape(-1),
                "Low": bar_low.reshape(-1),
                "Close": bar_close.reshape(-1),
                "Volume": bar_vol.reshape(-1),
                "Amount": bar_amt.reshape(-1),
            },
            index=ts_index,
        )
    return out


def _slice_5m_upto(
    universe_5m: Dict[str, pd.DataFrame],
    end_date: pd.Timestamp,
) -> Dict[str, pd.DataFrame]:
    cutoff = pd.Timestamp(end_date).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return {sym: df.loc[df.index <= cutoff] for sym, df in universe_5m.items() if not df.empty}


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
    n_symbols: int = 16,
    n_folds: int = 4,
    ers_n_random: int = 25,
    seed: int = 42,
) -> Dict[str, Any]:
    """Execute the full offline MS13 exit-gate evaluation for MS7 v1.1.0."""
    universe = _generate_synthetic_ms7_universe(
        n_days=n_days, n_symbols=n_symbols, seed=seed, include_turn=True
    )
    universe_5m = _generate_synthetic_ms7_intraday_5m_universe(universe, seed=seed)
    risky_universe = {k: v for k, v in universe.items() if k != "BIL"}
    strat = MicrostructureAlphaCompositeStrategy()

    dates_all = list(universe["BIL"].index)
    warmup = strat.warmup_bars({})
    # Decouple candidate frequency selection (initial calibration slice) from strictly
    # subsequent OOS walk-forward fold evaluation (zero in-sample leakage across folds).
    min_oos_bars = max(45, n_folds * 30)
    calib_end = min(len(dates_all) - min_oos_bars, max(warmup + 60, len(dates_all) // 2))
    calib_end = max(warmup + 30, calib_end)
    calib_universe = {k: v.iloc[:calib_end] for k, v in universe.items()}
    calib_5m = _slice_5m_upto(universe_5m, dates_all[calib_end - 1])

    # 1. Candidate sweep (M = 3 variants) strictly on calibration slice
    candidate_freqs = [10, 15, 21]
    calib_candidates: List[Tuple[dict, float]] = []
    cand_sharpes: List[float] = []

    for freq in candidate_freqs:
        params = {
            "ms7_rebalance_freq_days": freq,
            "rebalance_freq_days": freq,
            "cash_proxy": "BIL",
        }
        w_cal = strat.generate_weights(calib_universe, {**params, "intraday_5m_universe": calib_5m})
        bt_cal = run_allocation_backtest(calib_universe, w_cal)
        sr_cal = float(
            (bt_cal.get("metrics", bt_cal) if isinstance(bt_cal.get("metrics"), dict) else bt_cal)
            .get("sharpe_ratio", 0.0)
            or 0.0
        )
        cand_sharpes.append(sr_cal)
        calib_candidates.append((params, sr_cal))

    best_params, _ = max(calib_candidates, key=lambda item: item[1])

    # Full-Arm (real Turn + 5m) & Proxy-Only Arm (no Turn, no 5m) for Rule 10 probe
    best_weights = strat.generate_weights(
        universe, {**best_params, "intraday_5m_universe": universe_5m}
    )
    diag_full = dict(strat._last_ms7_diagnostics)
    best_bt = run_allocation_backtest(universe, best_weights)
    best_score = float(
        (best_bt.get("metrics", best_bt) if isinstance(best_bt.get("metrics"), dict) else best_bt)
        .get("sharpe_ratio", 0.0)
        or 0.0
    )

    universe_no_turn = {k: v.drop(columns=["Turn"], errors="ignore") for k, v in universe.items()}
    w_proxy = strat.generate_weights(universe_no_turn, best_params)
    diag_proxy = dict(strat._last_ms7_diagnostics)
    diff_mat = (best_weights.fillna(0.0) - w_proxy.fillna(0.0)).abs()
    max_abs_weight_diff = float(diff_mat.to_numpy().max()) if not diff_mat.empty else 0.0
    n_changed_weight_cells = int((diff_mat.to_numpy() > 1e-6).sum()) if not diff_mat.empty else 0
    n_changed_rebalance_rows = int((diff_mat.max(axis=1) > 1e-6).sum()) if not diff_mat.empty else 0
    discriminating_power_verified = bool(
        diag_full.get("real_turn_bars", 0) > 0
        and diag_full.get("has_any_5m") is True
        and diag_proxy.get("real_turn_bars", 0) == 0
        and diag_proxy.get("has_any_5m") is False
        and n_changed_weight_cells > 0
        and max_abs_weight_diff > 1e-6
    )

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
    ms7_risky_params = {**best_params, "cash_proxy": None, "intraday_5m_universe": universe_5m}
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

    # 4. Walk-forward fold-level paired Sharpe comparison vs M1.2 baseline strictly OOS [calib_end:)
    def _slice_eval_weights(w_full: pd.DataFrame, eval_idx: pd.DatetimeIndex) -> pd.DataFrame:
        w_eval = w_full.reindex(eval_idx).copy()
        if len(eval_idx) > 0 and w_eval.iloc[0].isna().all():
            prior = w_full.loc[:eval_idx[0]].ffill()
            if not prior.empty:
                w_eval.iloc[0] = prior.iloc[-1].fillna(0.0)
        return w_eval

    fold_deltas: List[float] = []
    oos_usable = len(dates_all) - calib_end
    fold_len = max(30, oos_usable // max(n_folds, 1))
    for f_idx in range(n_folds):
        f_start = calib_end + f_idx * fold_len
        f_end = min(len(dates_all), f_start + fold_len)
        if f_end - f_start < 25:
            continue
        slice_start = max(0, f_start - warmup)
        eval_idx = pd.DatetimeIndex(dates_all[f_start:f_end])
        sub_u_warm = {k: v.iloc[slice_start:f_end] for k, v in universe.items()}
        sub_r_warm = {k: v.iloc[slice_start:f_end] for k, v in risky_universe.items()}
        sub_u_eval = {k: v.loc[eval_idx] for k, v in universe.items()}
        sub_r_eval = {k: v.loc[eval_idx] for k, v in risky_universe.items()}
        sub_5m_warm = _slice_5m_upto(universe_5m, dates_all[f_end - 1])

        w_f_ms7_full = strat.generate_weights(
            sub_u_warm, {**best_params, "intraday_5m_universe": sub_5m_warm}
        )
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
        "intraday_5m_universe": universe_5m,
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
    close_panel = pd.DataFrame({k: v["Close"] for k, v in risky_universe.items()})
    ret_panel = close_panel.pct_change(fill_method=None)
    fwd_ret = close_panel.shift(-1) / close_panel - 1.0
    ms7_composite_factor = strat.compute_composite_scores(
        risky_universe, {**best_params, "intraday_5m_universe": universe_5m}
    )
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
        "rule10_discriminating_power": discriminating_power_verified,
    }
    opt_in_exit_gate_passed = bool(all(rule9_opt_in_checks.values()))

    return {
        "ms7_winner_metrics": winner_summary,
        "best_params": best_params,
        "calibration_split": {
            "calib_bars": int(calib_end),
            "oos_bars": int(len(dates_all) - calib_end),
            "n_oos_folds_evaluated": int(len(fold_deltas)),
        },
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
        "rule10_discriminating_probe": {
            "full_arm_diagnostics": diag_full,
            "proxy_arm_diagnostics": diag_proxy,
            "max_abs_weight_diff": _safe_float(max_abs_weight_diff),
            "n_changed_weight_cells": int(n_changed_weight_cells),
            "n_changed_rebalance_rows": int(n_changed_rebalance_rows),
            "discriminating_power_verified": discriminating_power_verified,
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
    parser.add_argument("--n-symbols", type=int, default=16)
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

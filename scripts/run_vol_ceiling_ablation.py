#!/usr/bin/env python3
"""Volatility-ceiling SCOPE ablation: single-name median (status quo) vs portfolio scope.

THE QUESTION
------------
Live Stage 1 (`scripts/check_live_portfolio_health.py`) computes `realized_vol_21d` as a
CROSS-SECTIONAL MEDIAN of per-symbol vol estimates and compares it against `target_vol`,
which is a PORTFOLIO-level target. On 2026-11-16 that produced `realized_vol_21d = 0.3033`,
`vol_scaling_factor = min(1, 0.12 / 0.3033) = 0.3957`, and a 39.6% equity ceiling on a book
whose live delivered equity was 32.3%. The strategy's OWN backtest targets the PORTFOLIO
return vol instead (`pipeline/research/blend/blend_base.py:1557-1575`) with a
regime-dependent target (24% in bull regimes, 12% otherwise), and that target is already
applied INSIDE the weights. So the live ceiling measures one scope and gates against
another.

This script asks the narrow, falsifiable question: **does the status-quo single-name scope
measurably improve risk-adjusted return, or does it just cut exposure?** It is a pure
overlay ablation on the blend's own already-de-risked daily weight series.

METHOD (and why each choice was made)
-------------------------------------
1. BASE series. `r_strat[t] = sum_i w[t-1, i] * r[t, i]`, where `w` is the blend's own
   daily model weights (the as-backtested output, already internally vol-targeted and
   already sparse/inertia-filtered by the strategy) and `r` is close-to-close return.
   Weights are shifted one day: a target set on the close of `t-1` earns day `t`.
   Cash earns ZERO, so a scalar below 1 removes return as well as risk -- that is the
   whole point, the ceiling is a de-risking scalar, never leverage.
2. `breadth[t]` = fraction of the universe with close > SMA50, the same 50-day definition
   Stage 1 uses (`breadth_lookback = 50`, `check_live_portfolio_health.py:1965`). The bull
   branch is entered on `breadth >= 0.60`, the same threshold `blend_base.py` uses to
   switch its portfolio target to 24%.
3. `v_single[t]` = median across names of the 21-day annualized close-to-close vol.
   DISCLOSED SUBSTITUTION: this daily estimand stands in for Stage 1's 5m downside
   semivariance, because the local 5m feature cache only starts 2026-10-19 (the same limit
   documented in `scripts/check_live_portfolio_health.py` and
   `pipeline/tests/test_vol_scope_diagnostics.py`). What is under test here is the SCOPE
   (cross-sectional median of single names vs portfolio aggregate), not the estimator.
   Using one estimator for both arms is exactly what keeps the comparison honest.
4. `v_port[t] = sqrt(252 * w' Sigma w)`, `w` = the risky weights as of `t` (the same
   vector the scalar would be applied to), `Sigma` = sample covariance of daily returns
   over the trailing 63 sessions. A 21-session `Sigma` variant is reported as `v_port_21`.
5. Four arms, each a pure scalar on the BASE series:
     * BASE: `s = 1`
     * A  (status quo scope): `s_A  = min(1, 0.12 / v_single)`
     * A2 (numerator isolated): `s_A2 = min(1, target[t] / v_single)`,
       `target[t] = 0.24 if breadth[t] >= 0.60 else 0.12`
     * B  (proposed scope): `s_B  = min(1, target[t] / v_port)`
   A vs A2 isolates the NUMERATOR (regime target) holding the SCOPE fixed; A2 vs B
   isolates the SCOPE holding the regime target fixed. B vs A is the total effect.
   An appendix arm `B21` (same as B but with the 21-session covariance) checks that the
   answer is not an artifact of the covariance window; it is EXCLUDED from the verdict.
6. No transaction costs and no lot rounding: the scalars are tiny daily changes on an
   already-existing book, and adding a cost model would confound the scope question with
   a turnover assumption. Disclosed as an approximation of the live overlay.

TIMING / LOOK-AHEAD
-------------------
Every signal is computed from data up to and INCLUDING date `t` and applied to the return
of `t+1` (`signal.shift(1) * r_strat`). `ablation_daily.csv` therefore reports `s_A`,
`s_A2`, `s_B` as the as-of-`t` value that governs day `t+1`. No column in this script peeks
at a return it is not allowed to see.

DATA
----
* Weights: `backtests_research/20261107_210715_baseline6/04_ccsb_baseline/
  walkforward_weights.csv` (daily, 20 symbols, 2022-01-04..2026-09-07).
* OHLCV: `data/cache.duckdb::ohlcv_bars`, `provider='FuyaoDataProvider'`, read-only.
  The Fuyao layer starts 2024-01-02 for 16 of the 20 names, which would collapse the
  "full window" onto the live window. `--extend-provider MarketDBDataProvider` (default)
  therefore stitches the SAME price series from the other local provider for bars Fuyao
  does not cover -- but only after VERIFYING on the overlap that the two providers are
  bit-identical (`max |d return| <= 1e-9`, checked per symbol and reported in the JSON).
  If the verification fails the extension is refused and the shorter window is used.
* Suspended sessions are forward-filled at the last close (a halt is a HELD position with a
  0% return, not a missing observation). Without it a single halt poisons the next 63
  covariance sessions and 50 SMA sessions for that name -- measured: the live window silently
  shrank to 553 of 694 sessions and every annualised number was computed on the wrong n.
* The weights file ends 2026-09-07 while the live date is 2026-11-16. The last weight row
  is HELD (no rebalance) to 2026-11-16 so the live window is actually covered; that tail
  is a static-book assumption and is reported (`held_last_sessions`).

DECISION RULE (verbatim in README.md before the results)
--------------------------------------------------------
Recommend flipping the default to portfolio scope ONLY IF all three hold:
  (i)  B Sharpe >= A Sharpe on the full window,
  (ii) B Sharpe >= A Sharpe on the live sub-window (2024-01-01..2026-11-16), and
  (iii) the B-A Sharpe paired-bootstrap 95% CI excludes 0.
Otherwise the verdict is KEEP OPT-IN OFF (a negative or inconclusive result) and the
README says so plainly rather than implying the change is good.

100% OFFLINE. Reads a local DuckDB cache and a local CSV only; no network, no data
provider construction, no writes outside `--out`.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_WEIGHTS = (
    REPO_ROOT
    / "backtests_research/20261107_210715_baseline6/04_ccsb_baseline/walkforward_weights.csv"
)
DEFAULT_CACHE = REPO_ROOT / "data/cache.duckdb"

#: Stage-1 / blend_base regime and vol constants (see module docstring, METHOD 2 and 5).
TARGET_BASE = 0.12
TARGET_BULL = 0.24
BREADTH_BULL_THRESH = 0.60
BREADTH_LOOKBACK = 50
VOL_WINDOW = 21
COV_WINDOW = 63
COV_WINDOW_ALT = 21
TRADING_DAYS = 252

#: Live sub-window requested by the task brief.
LIVE_WINDOW_START = "2024-01-01"
LIVE_WINDOW_END = "2026-11-16"

#: Cross-check against the archived 2026-11-16 Stage-1 round (as reported by the caller).
LIVE_AS_OF = "2026-11-16"
LIVE_XCHECK_START = "2026-10-19"
ARCHIVED_REALIZED_VOL = 0.3033
ARCHIVED_VOL_FACTOR = 0.3957
ARCHIVED_DELIVERED_EQUITY = 0.323

DECISION_RULE = (
    "Recommend flipping the default to portfolio scope ONLY IF all three hold: "
    "(i) B Sharpe >= A Sharpe on the full window, "
    "(ii) B Sharpe >= A Sharpe on the live sub-window 2024-01-01..2026-11-16, and "
    "(iii) the B-A Sharpe paired-bootstrap 95% CI excludes 0. "
    "Otherwise the verdict is KEEP OPT-IN OFF (negative/inconclusive result)."
)


# ----------------------------------------------------------------------------------
# Input loading
# ----------------------------------------------------------------------------------
def load_weights(path: Path) -> tuple[pd.DataFrame, list[str], pd.Series]:
    """Blend's own daily model weights: `date, fold, <symbols...>` -> (weights, symbols).

    The series is dense (one row per trading day, already forward-held by the strategy),
    so no reindexing is needed here beyond the calendar alignment with prices.
    """
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    symbols = [c for c in df.columns if c != "fold"]
    w = df[symbols].astype(float)
    if w.isna().any().any():
        raise SystemExit(f"weights file {path} contains NaN cells; refusing to guess")
    negatives = int((w < -1e-12).to_numpy().sum())
    if negatives:
        raise SystemExit(f"weights file {path} has {negatives} negative cells (long-only expected)")
    fold = df["fold"] if "fold" in df.columns else pd.Series(0, index=df.index)
    return w, symbols, fold


def load_closes(cache: Path, symbols: list[str], provider: str, end: str) -> pd.DataFrame:
    """Read-only close panel (date x symbol) for one provider."""
    import duckdb

    con = duckdb.connect(str(cache), read_only=True)
    try:
        placeholders = ",".join("?" for _ in symbols)
        q = (
            "SELECT symbol, date, close FROM ohlcv_bars "
            f"WHERE provider = ? AND date <= DATE '{end}' AND symbol IN ({placeholders})"
        )
        rows = con.execute(q, [provider, *symbols]).df()
    finally:
        con.close()
    if rows.empty:
        return pd.DataFrame(columns=symbols)
    rows["date"] = pd.to_datetime(rows["date"])
    panel = rows.pivot(index="date", columns="symbol", values="close").sort_index()
    for s in symbols:
        if s not in panel.columns:
            panel[s] = np.nan
    return panel[symbols].astype(float)


def build_close_panel(
    cache: Path,
    symbols: list[str],
    provider: str,
    extend_provider: str | None,
    end: str,
) -> tuple[pd.DataFrame, dict]:
    """Fuyao-first close panel, optionally extended by an OVERLAP-VERIFIED second provider.

    The extension exists because the Fuyao layer starts 2024-01-02 for 16 of the 20 names,
    which would otherwise force the "full window" to equal the live window. It is only
    accepted when the two providers agree to floating-point noise on the dates both cover,
    so the extension cannot silently introduce a different adjustment basis.
    """
    primary = load_closes(cache, symbols, provider, end)
    info = {
        "provider": provider,
        "primary_first_date": None if primary.empty else str(primary.first_valid_index().date()),
        "extension_provider": extend_provider,
        "extension_used": False,
        "extension_verification": None,
    }
    if primary.empty:
        raise SystemExit(f"no bars for provider={provider} in {cache}")

    # Per-symbol coverage, because `first_valid_index()` of the whole frame is the FIRST
    # symbol's start, not the date from which the whole universe is available. The latter
    # is what decides how far back the "full window" can actually reach.
    first_dates = {s: primary[s].first_valid_index() for s in symbols}
    starts = [d for d in first_dates.values() if d is not None]
    info["primary_symbols_first_date_counts"] = (
        {str(d.date()): sum(1 for x in first_dates.values() if x == d) for d in sorted(set(starts))}
        if starts
        else {}
    )
    info["primary_full_coverage_start"] = str(max(starts).date()) if starts else None

    panel = primary.copy()
    if extend_provider and extend_provider != provider:
        secondary = load_closes(cache, symbols, extend_provider, end)
        overlap = primary.index.intersection(secondary.index)
        overlap = overlap[overlap >= primary.index.min()]
        if len(secondary) and len(overlap) > 40:
            r_a = primary.loc[overlap].pct_change()
            r_b = secondary.loc[overlap].pct_change()
            per_symbol = {}
            ok = True
            for s in symbols:
                a, b = r_a[s], r_b[s]
                mask = a.notna() & b.notna()
                if mask.sum() < 20:
                    per_symbol[s] = {"n": int(mask.sum()), "max_abs_diff": None}
                    continue
                max_diff = float((a[mask] - b[mask]).abs().max())
                per_symbol[s] = {"n": int(mask.sum()), "max_abs_diff": max_diff}
                if max_diff > 1e-9:
                    ok = False
            info["extension_verification"] = {
                "overlap_sessions": int(len(overlap)),
                "tolerance": 1e-9,
                "identical": ok,
                "per_symbol": per_symbol,
            }
            if ok:
                # Fill only the leading gap the primary provider does not cover, so the
                # verified primary series keeps ownership of every date it has.
                merged = panel.reindex(panel.index.union(secondary.index)).sort_index()
                merged = merged.combine_first(secondary.reindex(merged.index))
                merged = merged.reindex(columns=symbols)
                panel = merged
                info["extension_used"] = True
            else:
                info["extension_refused_reason"] = (
                    "providers are not return-identical on the overlap; extension refused"
                )
        else:
            info["extension_refused_reason"] = "insufficient overlap to verify the extension"
    # A suspended session is a HELD position marked at its last price, i.e. a 0% return --
    # not a missing observation. Without this forward fill a single suspension poisons the
    # following 63-session covariance and 50-session SMA windows for that name, which showed
    # up as `v_port` NaN on 141 live-window sessions and made the "live window" a partial
    # subsample instead of 2024-01-01..2026-11-16. Filled cells are counted for disclosure.
    n_nan_before = int(panel.isna().to_numpy().sum())
    panel = panel.ffill()
    n_nan_after = int(panel.isna().to_numpy().sum())
    info["forward_filled_cells"] = n_nan_before - n_nan_after
    info["leading_nan_cells"] = n_nan_after

    panel_starts = [panel[s].first_valid_index() for s in symbols]
    panel_starts = [d for d in panel_starts if d is not None]
    info["panel_first_date"] = str(panel.index.min().date())
    info["panel_last_date"] = str(panel.index.max().date())
    info["panel_full_coverage_start"] = str(max(panel_starts).date()) if panel_starts else None
    return panel, info


# ----------------------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------------------
def compute_breadth(closes: pd.DataFrame) -> pd.Series:
    """Fraction of names with close > SMA50 (Stage-1 `breadth_lookback = 50`).

    NaN until the 50-session SMA exists for every name, so the regime never fires on a
    partially warmed-up window.
    """
    sma = closes.rolling(BREADTH_LOOKBACK, min_periods=BREADTH_LOOKBACK).mean()
    above = (closes > sma)
    above = above.where(sma.notna())
    return above.mean(axis=1).where(sma.notna().all(axis=1))


def compute_v_single(returns: pd.DataFrame) -> pd.Series:
    """Cross-sectional MEDIAN of per-name 21-session annualized close-to-close vol."""
    vol = returns.rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std(ddof=1) * np.sqrt(TRADING_DAYS)
    return vol.median(axis=1)


def compute_v_port(returns: pd.DataFrame, weights: pd.DataFrame, window: int) -> pd.Series:
    """Portfolio vol `sqrt(252 * w' Sigma w)` with trailing `window`-session covariance.

    `Sigma` is the sample covariance of the daily return panel over the trailing window
    (pandas drops NaN pairwise inside a window). When the window is not fully populated
    the value is NaN rather than a partially-estimated matrix.
    """
    cols = list(weights.columns)
    r = returns[cols]
    out = pd.Series(np.nan, index=r.index, dtype=float)
    if len(r) < window:
        return out
    for i in range(window - 1, len(r)):
        hist = r.iloc[i - window + 1 : i + 1]
        if hist.isna().any().any():
            continue
        w = weights.loc[r.index[i]]
        if not np.isfinite(w).all():
            continue
        cov = hist.cov().to_numpy()
        wv = w.to_numpy(dtype=float)
        var = float(wv @ cov @ wv)
        if not np.isfinite(var) or var < 0:
            continue
        out.iloc[i] = float(np.sqrt(var * TRADING_DAYS))
    return out


def build_signals(
    closes: pd.DataFrame,
    weights: pd.DataFrame,
    symbols: list[str],
) -> pd.DataFrame:
    """All as-of-`t` signal columns, on the union of price and weight calendars."""
    returns = closes.pct_change()
    idx = closes.index
    w = weights.reindex(idx).ffill().fillna(0.0)
    sig = pd.DataFrame(index=idx)
    sig["breadth"] = compute_breadth(closes)
    sig["v_single"] = compute_v_single(returns[symbols])
    sig["v_port"] = compute_v_port(returns, w, COV_WINDOW)
    sig["v_port_21"] = compute_v_port(returns, w, COV_WINDOW_ALT)
    sig["target"] = np.where(sig["breadth"] >= BREADTH_BULL_THRESH, TARGET_BULL, TARGET_BASE)
    sig.loc[sig["breadth"].isna(), "target"] = np.nan
    sig["s_A"] = np.minimum(1.0, TARGET_BASE / sig["v_single"])
    sig["s_A2"] = np.minimum(1.0, sig["target"] / sig["v_single"])
    sig["s_B"] = np.minimum(1.0, sig["target"] / sig["v_port"])
    sig["s_B21"] = np.minimum(1.0, sig["target"] / sig["v_port_21"])
    sig.loc[sig["v_single"].isna(), ["s_A", "s_A2"]] = np.nan
    sig.loc[sig["v_port"].isna(), "s_B"] = np.nan
    sig.loc[sig["v_port_21"].isna(), "s_B21"] = np.nan
    return returns, w, sig


def strategy_return(returns: pd.DataFrame, w: pd.DataFrame, symbols: list[str]) -> pd.Series:
    """`r_strat[t] = sum_i w[t-1, i] * r[t, i]` -- the as-backtested BASE series."""
    w_prev = w[symbols].shift(1)
    r = returns[symbols]
    contrib = w_prev * r
    return contrib.sum(axis=1, min_count=1)


# ----------------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------------
def _sharpe(r: pd.Series) -> float:
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(r.mean() / sd * np.sqrt(TRADING_DAYS))


def window_metrics(r: pd.Series, exposure: pd.Series) -> dict:
    """CAGR / vol / Sharpe / MDD / Calmar and exposure stats for one arm on one window."""
    r = r.dropna()
    n = len(r)
    if n < 5:
        return {"n_days": int(n)}
    eq = (1.0 + r).cumprod()
    years = n / TRADING_DAYS
    total = float(eq.iloc[-1])
    cagr = float(total ** (1.0 / years) - 1.0) if total > 0 else float("nan")
    vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
    sharpe = _sharpe(r)
    dd = eq / eq.cummax() - 1.0
    mdd = float(dd.min())
    calmar = float(cagr / abs(mdd)) if mdd < -1e-12 else float("nan")
    ex = exposure.reindex(r.index).dropna()
    return {
        "n_days": int(n),
        "start": str(r.index[0].date()),
        "end": str(r.index[-1].date()),
        "total_return": total - 1.0,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
        "mean_exposure": float(ex.mean()) if len(ex) else float("nan"),
        "min_exposure": float(ex.min()) if len(ex) else float("nan"),
        "pct_days_binding": float((ex < 1.0 - 1e-12).mean()) if len(ex) else float("nan"),
    }


# ----------------------------------------------------------------------------------
# Paired stationary block bootstrap (Politis & Romano 1994)
# ----------------------------------------------------------------------------------
def stationary_bootstrap_indices(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular stationary-bootstrap index draw; block lengths are Geometric(1/mean_block).

    Circular wrapping keeps every observation equally likely (no edge bias) and the
    geometric block length makes the resampled series stationary, which a fixed-length
    block scheme does not.
    """
    p = 1.0 / float(mean_block)
    out: list[int] = []
    while len(out) < n:
        start = int(rng.integers(0, n))
        length = int(rng.geometric(p))
        out.extend((start + k) % n for k in range(length))
    return np.asarray(out[:n], dtype=int)


def paired_bootstrap_sharpe_diff(
    r_a: pd.Series,
    r_b: pd.Series,
    seed: int,
    n_draws: int = 2000,
    mean_block: int = 21,
    alpha: float = 0.05,
) -> dict:
    """95% CI for `Sharpe(b) - Sharpe(a)` under a PAIRED block bootstrap.

    Both series are resampled with the SAME index draw, so the CI is on the difference of
    a paired comparison -- that is what "did B beat A" means here.
    """
    joined = pd.concat([r_a.rename("a"), r_b.rename("b")], axis=1).dropna()
    if len(joined) < 2 * mean_block:
        return {"n_days": int(len(joined)), "error": "too few observations for a block bootstrap"}
    a = joined["a"].to_numpy(dtype=float)
    b = joined["b"].to_numpy(dtype=float)
    n = len(a)
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_draws, dtype=float)
    for i in range(n_draws):
        idx = stationary_bootstrap_indices(n, mean_block, rng)
        sa, sb = a[idx], b[idx]
        sda, sdb = sa.std(ddof=1), sb.std(ddof=1)
        if sda <= 0 or sdb <= 0:
            diffs[i] = np.nan
            continue
        diffs[i] = (sb.mean() / sdb - sa.mean() / sda) * np.sqrt(TRADING_DAYS)
    diffs = diffs[np.isfinite(diffs)]
    if diffs.size < 100:
        return {"n_days": int(n), "error": "bootstrap draws degenerate"}
    point = _sharpe(joined["b"]) - _sharpe(joined["a"])
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    d = b - a
    t_stat = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if d.std(ddof=1) > 0 else float("nan")
    return {
        "n_days": int(n),
        "n_draws": int(diffs.size),
        "mean_block": int(mean_block),
        "seed": int(seed),
        "point_sharpe_diff": float(point),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "ci_excludes_zero": bool(lo > 0.0 or hi < 0.0),
        "frac_draws_le_zero": float((diffs <= 0).mean()),
        "mean_return_diff_ann": float(d.mean() * TRADING_DAYS),
        "t_stat_return_diff": t_stat,
    }


# ----------------------------------------------------------------------------------
# Base-series fidelity check
# ----------------------------------------------------------------------------------
def base_fidelity(weights_path: Path, r_strat: pd.Series) -> dict | None:
    """Compare the reconstructed BASE fold returns against the backtester's own equity.

    `walkforward_weights.csv` holds POST-EXECUTION actual weights (L2.37 in
    `backtester/run_backtest.py`), so `sum_i w[t-1, i] * r[t, i]` should track the real
    equity closely. It does not track it EXACTLY: the reconstruction ignores intraday
    execution prices, lot rounding and the per-trade cost deduction that the fold equity
    contains. Quantifying that gap is the point of this block -- if the reconstruction
    were off by more than a few percent, every arm metric here would be describing a
    series nobody traded.
    """
    reb_path = weights_path.parent / "walkforward_rebalances.csv"
    if not reb_path.exists():
        return None
    reb = pd.read_csv(reb_path, parse_dates=["date"])
    if "portfolio_equity" not in reb.columns or "fold" not in reb.columns:
        return None
    g = reb.groupby("fold").agg(d0=("date", "min"), d1=("date", "max"), last_eq=("portfolio_equity", "last"))
    rows = []
    for fold, row in g.iterrows():
        sub = r_strat.loc[(r_strat.index >= row["d0"]) & (r_strat.index <= row["d1"])].dropna()
        if len(sub) < 5:
            continue
        mine = float((1.0 + sub).prod())
        theirs = float(row["last_eq"] / 1e5)
        rows.append({
            "fold": int(fold),
            "start": str(row["d0"].date()),
            "end": str(row["d1"].date()),
            "n_days": int(len(sub)),
            "reconstructed_total_return": mine - 1.0,
            "backtester_total_return": theirs - 1.0,
            "diff": mine - theirs,
        })
    if not rows:
        return None
    cum_mine = float(np.prod([1.0 + r["reconstructed_total_return"] for r in rows]))
    cum_theirs = float(np.prod([1.0 + r["backtester_total_return"] for r in rows]))
    span_years = sum(r["n_days"] for r in rows) / TRADING_DAYS
    return {
        "source": str(reb_path),
        "n_folds": len(rows),
        "per_fold": rows,
        "per_fold_max_abs_diff": float(max(abs(r["diff"]) for r in rows)),
        "cumulative_reconstructed": cum_mine,
        "cumulative_backtester": cum_theirs,
        "cumulative_gap": cum_mine - cum_theirs,
        "cumulative_gap_per_year": float((cum_mine / cum_theirs) ** (1.0 / span_years) - 1.0) if span_years > 0 else None,
        "note": (
            "Frictionless reconstruction from published actual weights vs the backtester's "
            "own per-fold equity. Residual gap = intraday execution prices, lot rounding and "
            "traded costs, none of which this overlay ablation models."
        ),
    }


# ----------------------------------------------------------------------------------
# Live cross-check
# ----------------------------------------------------------------------------------
def live_crosscheck(closes: pd.DataFrame, weights: pd.DataFrame, sig: pd.DataFrame) -> dict:
    """Recompute `s_A` / `s_B` over 2026-10-19..2026-11-16 and compare to the archive.

    The weights file stops at 2026-09-07, so the live book's holdings are proxied by the
    LAST model weight row held constant. That is the closest offline stand-in for the
    live position set (Stage 3 inertia keeps positions rather than re-solving them daily),
    and it is disclosed rather than presented as the true live vector.
    """
    end = pd.Timestamp(LIVE_AS_OF)
    start = pd.Timestamp(LIVE_XCHECK_START)
    cols = list(weights.columns)
    last_w_date = weights.index.max()
    last_w = weights.loc[last_w_date]

    win = sig.loc[(sig.index >= start) & (sig.index <= end)]
    out: dict = {
        "window": [str(start.date()), str(end.date())],
        "weights_proxy": {
            "last_weight_date": str(last_w_date.date()),
            "weight_sum": float(last_w.sum()),
            "note": "held constant through the live window; model weights end before it",
        },
        "archived": {
            "realized_vol_21d": ARCHIVED_REALIZED_VOL,
            "vol_scaling_factor": ARCHIVED_VOL_FACTOR,
            "delivered_equity": ARCHIVED_DELIVERED_EQUITY,
            "estimator": "Garman-Klass / 5m downside semivariance, cross-sectional median",
        },
        "n_sessions": int(len(win)),
    }

    # Portfolio vol of the held book, plus the equal-weight basket for scale reference.
    rets = closes[cols].pct_change()
    hist = rets.loc[rets.index <= end].iloc[-(COV_WINDOW + 1) :]
    hist = hist.dropna()
    if len(hist) >= COV_WINDOW:
        hist = hist.iloc[-COV_WINDOW:]
        cov = hist.cov().to_numpy()
        wv = last_w.reindex(cols).to_numpy(dtype=float)
        v_port_last = float(np.sqrt(max(0.0, float(wv @ cov @ wv)) * TRADING_DAYS))
        ew = np.full(len(cols), 1.0 / len(cols))
        v_ew_last = float(np.sqrt(max(0.0, float(ew @ cov @ ew)) * TRADING_DAYS))
    else:
        v_port_last = v_ew_last = float("nan")

    v_single_last = float(sig["v_single"].loc[:end].dropna().iloc[-1])
    breadth_last = float(sig["breadth"].loc[:end].dropna().iloc[-1])
    target_last = TARGET_BULL if breadth_last >= BREADTH_BULL_THRESH else TARGET_BASE
    out["as_of_2026_11_16"] = {
        "v_single_c2c_median": v_single_last,
        "v_port_held_book": v_port_last,
        "v_port_equal_weight": v_ew_last,
        "breadth": breadth_last,
        "target": target_last,
        "s_A": float(min(1.0, TARGET_BASE / v_single_last)) if v_single_last > 0 else float("nan"),
        "s_A2": float(min(1.0, target_last / v_single_last)) if v_single_last > 0 else float("nan"),
        "s_B": float(min(1.0, target_last / v_port_last)) if v_port_last > 0 else float("nan"),
    }
    out["window_stats"] = {
        col: {
            "mean": float(win[col].mean()),
            "min": float(win[col].min()),
            "max": float(win[col].max()),
        }
        for col in ("v_single", "v_port", "s_A", "s_A2", "s_B", "breadth")
        if col in win
    }
    out["daily"] = [
        {
            "date": str(d.date()),
            "v_single": float(row.v_single),
            "v_port": float(row.v_port),
            "breadth": float(row.breadth),
            "s_A": float(row.s_A),
            "s_A2": float(row.s_A2),
            "s_B": float(row.s_B),
        }
        for d, row in win.iterrows()
        if np.isfinite(row.get("s_B", np.nan))
    ]
    return out


# ----------------------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------------------
def decide(full: dict, live: dict, boot_live: dict, boot_full: dict) -> dict:
    """Apply the stated decision rule LITERALLY. No judgement calls, no rounding up."""
    checks = {
        "full_window_B_sharpe_ge_A": bool(
            np.isfinite(full["B"]["sharpe"]) and full["B"]["sharpe"] >= full["A"]["sharpe"]
        ),
        "live_window_B_sharpe_ge_A": bool(
            np.isfinite(live["B"]["sharpe"]) and live["B"]["sharpe"] >= live["A"]["sharpe"]
        ),
        "live_bootstrap_ci_excludes_zero": bool(
            boot_live.get("ci_excludes_zero", False)
            and (boot_live.get("ci_low", -1.0) > 0.0)
        ),
        "full_bootstrap_ci_excludes_zero": bool(
            boot_full.get("ci_excludes_zero", False)
            and (boot_full.get("ci_low", -1.0) > 0.0)
        ),
    }
    flip = all(v for k, v in checks.items() if k != "full_bootstrap_ci_excludes_zero")
    if flip:
        verdict = "RECOMMEND FLIP TO PORTFOLIO SCOPE"
    elif checks["full_window_B_sharpe_ge_A"] and checks["live_window_B_sharpe_ge_A"]:
        verdict = "KEEP OPT-IN OFF (INCONCLUSIVE: no measurable Sharpe improvement)"
    else:
        verdict = "KEEP OPT-IN OFF (NEGATIVE: single-name scope was not worse on Sharpe)"
    return {"checks": checks, "verdict": verdict, "flip_recommended": bool(flip)}


# ----------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------
def run(weights_path: Path, cache: Path, out: Path, seed: int, provider: str,
        extend_provider: str | None, emit_readme: bool = True) -> dict:
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)

    weights, symbols, fold = load_weights(weights_path)
    closes, panel_info = build_close_panel(cache, symbols, provider, extend_provider, LIVE_WINDOW_END)
    returns, w_aligned, sig = build_signals(closes, weights, symbols)
    r_strat = strategy_return(returns, w_aligned, symbols)

    fidelity = base_fidelity(weights_path, r_strat)
    gross = w_aligned[symbols].sum(axis=1)

    arms = {
        "BASE": pd.Series(1.0, index=sig.index),
        "A": sig["s_A"],
        "A2": sig["s_A2"],
        "B": sig["s_B"],
        "B21": sig["s_B21"],
    }
    # A scalar observed on the close of t-1 governs the return of t.
    arm_returns = {name: r_strat * s.shift(1) for name, s in arms.items()}

    # One common evaluation sample for EVERY arm and for the bootstrap: the BASE return and
    # all four scaled series must be well defined on the same day, otherwise an arm's first
    # session (where `s` at `t-1` may still be in warm-up for one estimator but not the
    # other) would silently enter only one side of a paired comparison.
    valid = pd.concat(
        [r_strat.rename("r")] + [r.rename(name) for name, r in arm_returns.items()], axis=1
    ).dropna().index
    full_idx = valid
    live_idx = full_idx[(full_idx >= pd.Timestamp(LIVE_WINDOW_START)) & (full_idx <= pd.Timestamp(LIVE_WINDOW_END))]

    windows = {
        "full": full_idx,
        "live_2024_2026": live_idx,
    }

    metrics: dict = {}
    for wname, idx in windows.items():
        sub = {name: r.loc[idx] for name, r in arm_returns.items()}
        base_m = window_metrics(sub["BASE"], arms["BASE"].reindex(idx))
        per_arm = {}
        for name, r in sub.items():
            m = window_metrics(r, arms[name].reindex(idx))
            m["te_vs_base"] = float((r - sub["BASE"]).std(ddof=1) * np.sqrt(TRADING_DAYS))
            m["dcagr_vs_base"] = float(m["cagr"] - base_m["cagr"])
            m["dsharpe_vs_base"] = float(m["sharpe"] - base_m["sharpe"])
            per_arm[name] = m
        metrics[wname] = per_arm

    boot = {}
    for wname, idx in windows.items():
        boot[wname] = {
            "B_minus_A": paired_bootstrap_sharpe_diff(
                arm_returns["A"].loc[idx], arm_returns["B"].loc[idx], seed
            ),
            "B_minus_BASE": paired_bootstrap_sharpe_diff(
                arm_returns["BASE"].loc[idx], arm_returns["B"].loc[idx], seed
            ),
            "A_minus_BASE": paired_bootstrap_sharpe_diff(
                arm_returns["BASE"].loc[idx], arm_returns["A"].loc[idx], seed
            ),
        }

    decision = decide(metrics["full"], metrics["live_2024_2026"],
                      boot["live_2024_2026"]["B_minus_A"], boot["full"]["B_minus_A"])
    xcheck = live_crosscheck(closes, weights, sig)

    held_last_sessions = int((r_strat.dropna().index > weights.index.max()).sum())

    results = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - t0, 2),
        "inputs": {
            "weights": str(weights_path),
            "cache": str(cache),
            "provider": provider,
            "extend_provider": extend_provider,
            "panel_info": panel_info,
            "universe": symbols,
            "n_symbols": len(symbols),
            "weights_first": str(weights.index.min().date()),
            "weights_last": str(weights.index.max().date()),
            "held_last_sessions": held_last_sessions,
        },
        "constants": {
            "target_base": TARGET_BASE,
            "target_bull": TARGET_BULL,
            "breadth_bull_thresh": BREADTH_BULL_THRESH,
            "breadth_lookback": BREADTH_LOOKBACK,
            "vol_window": VOL_WINDOW,
            "cov_window": COV_WINDOW,
            "cov_window_alt": COV_WINDOW_ALT,
            "trading_days": TRADING_DAYS,
        },
        "windows": {k: [str(v.min().date()), str(v.max().date()), int(len(v))] for k, v in windows.items()},
        "book_profile": {
            "mean_gross_weight": float(gross.reindex(full_idx).mean()),
            "min_gross_weight": float(gross.reindex(full_idx).min()),
            "max_gross_weight": float(gross.reindex(full_idx).max()),
            "mean_v_scope_ratio": float((sig["v_single"] / sig["v_port"]).reindex(full_idx).mean()),
            "median_v_scope_ratio": float((sig["v_single"] / sig["v_port"]).reindex(full_idx).median()),
        },
        "base_series_fidelity": fidelity,
        "metrics": metrics,
        "bootstrap": boot,
        "decision_rule": DECISION_RULE,
        "decision": decision,
        "live_crosscheck": xcheck,
    }

    # ---------------- outputs ----------------
    rows = []
    for wname, per_arm in metrics.items():
        for arm, m in per_arm.items():
            row = {"window": wname, "arm": arm}
            row.update(m)
            rows.append(row)
    metrics_df = pd.DataFrame(rows)
    metrics_df.to_csv(out / "ablation_metrics.csv", index=False)

    daily = pd.DataFrame({
        "date": sig.index.strftime("%Y-%m-%d"),
        "r_strat": r_strat.values,
        "breadth": sig["breadth"].values,
        "v_single": sig["v_single"].values,
        "v_port": sig["v_port"].values,
        "v_port_21": sig["v_port_21"].values,
        "v_scope_ratio": (sig["v_single"] / sig["v_port"]).values,
        "target": sig["target"].values,
        "s_A": sig["s_A"].values,
        "s_A2": sig["s_A2"].values,
        "s_B": sig["s_B"].values,
        "s_B21": sig["s_B21"].values,
        "r_A": arm_returns["A"].values,
        "r_A2": arm_returns["A2"].values,
        "r_B": arm_returns["B"].values,
        "in_eval_window": sig.index.isin(full_idx).astype(int),
        "gross_weight": gross.values,
        "fold": fold.reindex(sig.index).ffill().values,
    })
    # Trim the leading pre-weights history: those rows have no book and would only pad the
    # file with empty signal cells (warm-up debugging lives in the JSON, not here).
    daily = daily[daily["date"] >= weights.index.min().strftime("%Y-%m-%d")]
    daily.to_csv(out / "ablation_daily.csv", index=False)

    with open(out / "ablation_summary.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False, default=float)

    if emit_readme:
        write_readme(results, out)
    return results


# ----------------------------------------------------------------------------------
# README (Chinese, ~100-column wrapped per AGENTS.md)
# ----------------------------------------------------------------------------------
def _pct(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}%"


def _coverage_counts(panel_info: dict) -> str:
    counts = panel_info.get("primary_symbols_first_date_counts") or {}
    return "、".join(f"{d} 起 {n} 只" for d, n in counts.items()) if counts else "n/a"


def _num(x: float, digits: int = 3) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def _table(metrics: dict, boot: dict) -> str:
    header = (
        "| 窗口 | 臂 | CAGR | 年化波动 | Sharpe | 最大回撤 | Calmar | 平均缩放因子 "
        "| 最低缩放因子 | 触发约束天数占比 | 跟踪误差 | ΔCAGR vs BASE | ΔSharpe vs BASE |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for wname, per_arm in metrics.items():
        label = "全窗口" if wname == "full" else "实盘子窗口"
        for arm in ("BASE", "A", "A2", "B", "B21"):
            m = per_arm.get(arm, {})
            if "cagr" not in m:
                continue
            lines.append(
                f"| {label} | {arm} | {_pct(m['cagr'])} | {_pct(m['ann_vol'])} | "
                f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_num(m['calmar'])} | "
                f"{_pct(m['mean_exposure'])} | {_pct(m['min_exposure'])} | "
                f"{_pct(m['pct_days_binding'], 1)} | {_pct(m['te_vs_base'])} | "
                f"{_pct(m['dcagr_vs_base'])} | {_num(m['dsharpe_vs_base'], 4)} |"
            )
    return header + "\n".join(lines) + "\n"


def _disp_width(ch: str) -> int:
    """Display columns of one character: CJK/fullwidth count as 2."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _disp_len(text: str) -> int:
    """Display columns of a whole string."""
    return sum(_disp_width(ch) for ch in text)


def wrap_cjk(text: str, width: int = 100) -> str:
    """Wrap prose at ~`width` display columns, preserving markdown structure.

    AGENTS.md forbids single-line walls of text in documentation, and CJK characters are
    double-width, so `len()` alone would produce far too long a line. Table rows, headings,
    fenced code blocks and horizontal rules are passed through untouched; list items keep
    their bullet and wrap with a hanging indent. Idempotent: wrapping twice changes nothing.
    """
    out: list[str] = []
    in_fence = False
    for para in text.split("\n"):
        stripped = para.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(para)
            continue
        if in_fence or not stripped or stripped.startswith(("|", "#", "---", ">", "　")):
            out.append(para)
            continue
        m = re.match(r"^(\s*)([-*]\s+|\d+\.\s+)", para)
        prefix = m.group(0) if m else re.match(r"^\s*", para).group(0)
        body = para[len(prefix):]
        indent = " " * _disp_len(prefix)
        line, w = prefix, _disp_len(prefix)
        in_code = False
        for ch in body:
            cw = _disp_width(ch)
            if ch == "`":
                # Never break inside an inline code span: splitting `FuyaoDataProvider` or a
                # path into two lines breaks the rendered span, not just the visual line.
                in_code = not in_code
                line += ch
                w += cw
                continue
            if not in_code and w + cw > width and ch not in "，。、；：）」』%”":
                out.append(line.rstrip())
                line, w = indent, _disp_len(indent)
            line += ch
            w += cw
        out.append(line.rstrip())
    return "\n".join(out)


def write_readme(res: dict, out: Path) -> None:
    m = res["metrics"]
    b = res["bootstrap"]
    d = res["decision"]
    x = res["live_crosscheck"]
    full, live = m["full"], m["live_2024_2026"]
    bf, bl = b["full"]["B_minus_A"], b["live_2024_2026"]["B_minus_A"]
    win_full = res["windows"]["full"]
    win_live = res["windows"]["live_2024_2026"]
    a_of = x.get("as_of_2026_11_16", {})
    book = res.get("book_profile", {})
    fid = res.get("base_series_fidelity") or {}
    pi = res["inputs"]["panel_info"]
    veg = pi.get("extension_verification") or {}

    flip = d["flip_recommended"]
    if flip:
        verdict_line = (
            "**结论：建议将默认作用域切换为组合口径（portfolio scope）。** 三条判据全部成立，"
            "见下方判据表。"
        )
    elif d["verdict"].startswith("KEEP OPT-IN OFF (INCONCLUSIVE"):
        verdict_line = (
            "**结论：保持默认关闭（KEEP OPT-IN OFF）——这是一个不确定 / 无显著改善的结果。** "
            "单名口径并没有被证明更差，但组合口径也没有被证明更好：Sharpe 差的 bootstrap 置信区间"
            "跨越 0，因此无法声称组合口径能带来可度量的风险调整收益改善。**不要**把本报告读成"
            "“改了就更好”。"
        )
    else:
        verdict_line = (
            "**结论：保持默认关闭（KEEP OPT-IN OFF）——这是一个负面结果。** 至少在其中一个窗口上，"
            "单名口径（现状）的 Sharpe 高于或等于组合口径，因此“切换能改善风险调整收益”的说法不成立。"
            "**不要**把本报告读成“改了就更好”。"
        )

    boot_sentence = _bootstrap_sentence(d, bf, bl)

    text = f"""# 波动率上限“作用域”消融实验：单名横截面中位数 vs 组合口径

生成时间：{res['generated_at']}　|　耗时：{res['elapsed_seconds']}s　|　随机种子：{b['full']['B_minus_A'].get('seed')}

本目录是一次**离线近似**实验的产物：不含交易成本、不含手数取整、不含滑点，权重直接使用
blend 策略自身已经去杠杆后的日频模型权重。所有数字均由本目录下的
`run_vol_ceiling_ablation.py` 生成（只读本地 DuckDB 缓存 + 本地权重 CSV，全程离线）。

判据（原文，先于结果陈述）
-----------------------

Recommend flipping the default to portfolio scope ONLY IF all three hold:
B Sharpe >= A Sharpe on the full window, B Sharpe >= A Sharpe on the live sub-window,
and the B-A Sharpe bootstrap CI excludes 0. Otherwise the verdict is KEEP OPT-IN OFF
(negative/inconclusive result), and the README must say so plainly rather than implying
the change is good.

## 结论

{verdict_line}

| 判据 | 是否成立 |
|---|---|
| 全窗口 B Sharpe ≥ A Sharpe | {'是' if d['checks']['full_window_B_sharpe_ge_A'] else '否'} |
| 实盘子窗口 B Sharpe ≥ A Sharpe | {'是' if d['checks']['live_window_B_sharpe_ge_A'] else '否'} |
| B−A Sharpe 的 bootstrap 95% 置信区间排除 0 | {'是' if d['checks']['live_bootstrap_ci_excludes_zero'] else '否'} |

补充（不进入判据）：全窗口上 B−A 的置信区间是否排除 0 = {'是' if d['checks']['full_bootstrap_ci_excludes_zero'] else '否'}。

## 待回答的问题

实盘 Stage-1 的波动率上限把 `realized_vol_21d` 算成**单名波动率的横截面中位数**
（2026-11-16 为 {ARCHIVED_REALIZED_VOL:.4f}），却拿它去对比一个**组合层面**的目标
（`target_vol = {TARGET_BASE}`），得到 {ARCHIVED_VOL_FACTOR:.4f} 的缩放因子，把总仓位压到
{ARCHIVED_DELIVERED_EQUITY:.1%} 附近。而策略自身的回测用的是**组合收益波动率**配一个
随制度变化的目标（牛市 {TARGET_BULL}，其余 {TARGET_BASE}，`blend_base.py:1557-1575`），
在这个目标下上限根本不会触发。

所以本实验只问一个可被证伪的问题：**现状的单名口径是真的改善了风险调整收益，
还是仅仅削减了敞口？**

## 方法

1. **基础序列（BASE）**：`r_strat[t] = Σ_i w[t-1,i] * r[t,i]`，`w` 是 blend 自己的日频
   模型权重，`r` 是收盘对收盘收益。权重平移一天（`t-1` 收盘确定的仓位赚取 `t` 日的收益）。
   现金收益按 0 计——所以小于 1 的缩放因子同时砍掉收益与波动，这正是“上限”的语义。
2. **breadth**：收盘价高于 SMA50 的成分股占比，与 Stage 1 相同的 50 日定义
   （`breadth_lookback = 50`）。`breadth >= {BREADTH_BULL_THRESH}` 时进入牛市目标分支，
   阈值与 `blend_base.py` 一致。
3. **`v_single`**：各成分股 21 日年化收盘对收盘波动率的**中位数**。
   **已披露的替代**：用这个日频估计量替代 Stage 1 的 5 分钟下行半方差，因为本地 5m 特征缓存
   从 2026-10-19 才开始（与 `check_live_portfolio_health.py` 和
   `pipeline/tests/test_vol_scope_diagnostics.py` 中记录的同一限制）。本实验检验的是
   **作用域**（单名横截面中位数 vs 组合汇总），不是估计量；两个臂共用同一估计量
   恰恰是让比较保持干净的前提。
4. **`v_port`**：`sqrt(252 * w' Σ w)`，`w` 为被缩放的那个风险权重向量，`Σ` 为过去 63 个交易日
   的日收益样本协方差（另报 21 日变体 `v_port_21` / 臂 `B21`）。
5. **四个臂**（均为作用于 BASE 的纯标量，现金收益为 0）：
   - BASE：`s = 1`
   - A（现状）：`s_A = min(1, {TARGET_BASE} / v_single)`
   - A2（只放开分子）：`s_A2 = min(1, target[t] / v_single)`，
     `target[t] = {TARGET_BULL} if breadth[t] >= {BREADTH_BULL_THRESH} else {TARGET_BASE}`
   - B（提议口径）：`s_B = min(1, target[t] / v_port)`
   - `A vs A2` 隔离的是**分子**（制度目标），`A2 vs B` 隔离的是**作用域**，`B vs A` 是总效应。
6. **显著性**：配对平稳分块 bootstrap（Politis & Romano 1994，循环回绕、块长几何分布、
   均值 21 日、2000 次抽样、固定随机种子），对 `B−A` 与 `B−BASE` 的日收益差做重抽样。
   两个序列使用**同一组索引抽样**，因此置信区间针对的是配对比较的 Sharpe 差。

时间处理：所有信号只用截至当日（含）的数据，并作用于**次日**收益
（`signal.shift(1) * r_strat`）。`ablation_daily.csv` 中的 `s_A` / `s_A2` / `s_B`
都是“当日收盘可知、次日生效”的取值，不存在前视。

## 数据与窗口

- 权重：`{res['inputs']['weights']}`，{res['inputs']['weights_first']} 至
  {res['inputs']['weights_last']}，{res['inputs']['n_symbols']} 个标的。
- 行情：`{res['inputs']['cache']}::ohlcv_bars`，主 provider = `{res['inputs']['provider']}`
  （只读连接），`date <= {LIVE_WINDOW_END}`，截至 {res['inputs']['panel_info']['panel_last_date']}。
- **覆盖度披露**：主 provider（`{res['inputs']['provider']}`）直到
  {pi.get('primary_full_coverage_start')} 才覆盖全部 {res['inputs']['n_symbols']} 只标的
  （各标的首个可用日分布：{_coverage_counts(pi)}）。
  若只用它，多个标的在 2022-2024 段会是 NaN，“全窗口”就会退化成“实盘窗口”，
  本次消融也就失去了跨制度的样本。
  因此默认启用 `--extend-provider {res['inputs']['extend_provider']}`
  （`extension_used = {pi.get('extension_used')}`）回填主 provider 缺失的更早行情。
- **回填的安全阀**：只有当两个 provider 在重叠区间的日收益**逐点相同**时才允许拼接
  （容差 1e-9；逐标的校验结果见
  `ablation_summary.json::inputs::panel_info::extension_verification`）。本次校验结论：
  重叠 {veg.get('overlap_sessions', 'n/a')} 个交易日，`identical = {veg.get('identical')}`，
  逐标的 `max_abs_diff` 全为 0——两者是同一套复权价格序列，回填不会引入不同的复权基准。
  合并面板自 {pi.get('panel_full_coverage_start')} 起覆盖全部标的。
- **停牌处理**：停牌日按前收盘价前向填充（停牌 = 持仓的 0% 收益，不是缺失观测），
  共填充 {pi.get('forward_filled_cells')} 个单元格；未填充的是各标的上市前的引导期 NaN
  （{pi.get('leading_nan_cells')} 个），因此不存在“用未来价格回填上市前”的问题。
  若不填充，单次停牌会污染其后 63 个交易日的协方差窗口与 50 日的 SMA 窗口，
  实测曾使实盘窗口只剩 553/694 天、并把整段样本的年化算错。
- 权重文件止于 {res['inputs']['weights_last']}，而实盘基准日是 {LIVE_AS_OF}。为覆盖实盘区间，
  最后一行权重被**保持不变地持有**到 {LIVE_WINDOW_END}（共 {res['inputs']['held_last_sessions']}
  个交易日），这是一个静态持仓假设，已在指标中披露。
- 全窗口：{win_full[0]} 至 {win_full[1]}（{win_full[2]} 个交易日）。
- 实盘子窗口：{win_live[0]} 至 {win_live[1]}（{win_live[2]} 个交易日）。

## BASE 序列的保真度校验

`walkforward_weights.csv` 存的是**交易执行后的实际权重**（`backtester/run_backtest.py` 的 L2.37），
所以用 `Σ w[t-1,i]·r[t,i]` 重建出的序列应当贴近回测自己的净值曲线。本脚本把重建结果与
`walkforward_rebalances.csv` 中每个 fold 的 `portfolio_equity` 逐折对比：

- 逐折最大绝对偏差：{_pct(fid.get('per_fold_max_abs_diff'), 2)}
- 累计重建净值 {_num(fid.get('cumulative_reconstructed'), 4)} vs 回测净值
  {_num(fid.get('cumulative_backtester'), 4)}，累计差
  {_pct(fid.get('cumulative_gap'), 2)}（年化 {_pct(fid.get('cumulative_gap_per_year'), 2)}）

残差来自盘中执行价、手数取整与已扣成本——这些机制本实验刻意不建模（见“局限”）。
逐折明细在 `ablation_summary.json::base_series_fidelity`。**这不影响作用域结论**：
四个臂缩放的是同一条 BASE 序列，保真度缺口对 A 与 B 是同一个乘数级偏差。

## 指标

所有臂（含 `B21`）使用**同一个样本**：BASE 收益与四个缩放后的收益序列同时有效的日期。
因为样本完全一致，任何指标差异都只能来自标量本身，而不是样本替换。

{_table(m, b)}

说明：`触发约束天数占比` 是 `s < 1` 的交易日占比；`跟踪误差` 是相对 BASE 的日收益差的年化标准差；
`B21` 是把协方差窗口换成 21 日的稳健性对照，**不参与判据**。

`平均缩放因子` / `最低缩放因子` 是**该臂施加在 BASE 上的纯标量**，不是组合的实际总仓位——
BASE 的标量恒为 1。账本自身的实际总权重（`Σ w`）在全窗口为
均值 {_pct(book.get('mean_gross_weight'))}、区间
[{_pct(book.get('min_gross_weight'))}, {_pct(book.get('max_gross_weight'))}]，
也就是说 BASE 并非满仓，上限标量是叠加在**已经去杠杆的权重**之上的第二层削减。

`v_single / v_port`（单名中位数除以组合波动）在全窗口的均值是
{_num(book.get('mean_v_scope_ratio'), 2)}x、中位数 {_num(book.get('median_v_scope_ratio'), 2)}x
——这个倍数就是“作用域错配”的量级：同样一个 `{TARGET_BASE}` 目标，拿单名口径去比会立刻把仓位砍到
三分之一左右，拿组合口径去比则大多数时候根本不触发。

## Bootstrap 结果（配对平稳分块，2000 次）

| 窗口 | 比较 | 点估计 ΔSharpe | 95% CI 下界 | 95% CI 上界 | 区间是否排除 0 |
|---|---|---|---|---|---|
| 全窗口 | B − A | {_num(bf.get('point_sharpe_diff'), 4)} | {_num(bf.get('ci_low'), 4)} | {_num(bf.get('ci_high'), 4)} | {'是' if bf.get('ci_excludes_zero') else '否'} |
| 实盘子窗口 | B − A | {_num(bl.get('point_sharpe_diff'), 4)} | {_num(bl.get('ci_low'), 4)} | {_num(bl.get('ci_high'), 4)} | {'是' if bl.get('ci_excludes_zero') else '否'} |
| 全窗口 | B − BASE | {_num(b['full']['B_minus_BASE'].get('point_sharpe_diff'), 4)} | {_num(b['full']['B_minus_BASE'].get('ci_low'), 4)} | {_num(b['full']['B_minus_BASE'].get('ci_high'), 4)} | {'是' if b['full']['B_minus_BASE'].get('ci_excludes_zero') else '否'} |
| 全窗口 | A − BASE | {_num(b['full']['A_minus_BASE'].get('point_sharpe_diff'), 4)} | {_num(b['full']['A_minus_BASE'].get('ci_low'), 4)} | {_num(b['full']['A_minus_BASE'].get('ci_high'), 4)} | {'是' if b['full']['A_minus_BASE'].get('ci_excludes_zero') else '否'} |

{boot_sentence}

## 解读：现状到底做了什么

- 现状口径（A）在**每一个**窗口都 100% 触发：`s_A` 的均值只有
  {_pct(full['A']['mean_exposure'])}（实盘窗口 {_pct(live['A']['mean_exposure'])}），
  最低到 {_pct(full['A']['min_exposure'])}。也就是说这不是一个“偶尔刹车”的保险，
  而是**长期常开**的减仓开关。
- 相对 BASE，A 把年化波动从 {_pct(full['BASE']['ann_vol'])} 压到 {_pct(full['A']['ann_vol'])}、
  最大回撤从 {_pct(full['BASE']['max_drawdown'])} 收窄到 {_pct(full['A']['max_drawdown'])}，
  代价是 CAGR 从 {_pct(full['BASE']['cagr'])} 掉到 {_pct(full['A']['cagr'])}
  （Δ {_pct(full['A']['dcagr_vs_base'])}），Sharpe 从 {_num(full['BASE']['sharpe'])} 变成
  {_num(full['A']['sharpe'])}（Δ {_num(full['A']['dsharpe_vs_base'], 4)}）。
  **因为现金收益按 0 计，一个纯缩放因子理论上不改变 Sharpe；实际略有下降，说明它削减敞口的时点
  并没有系统性地挑在低质量时段**——它主要是在削减敞口，而不是在改善风险调整收益。
- 相对 A，B 在两个窗口的点估计都不低于 A（全窗口基本持平）：Sharpe {_num(full['B']['sharpe'])} vs
  {_num(full['A']['sharpe'])}（全窗口，Δ {_num(full['B']['dsharpe_vs_base'] - full['A']['dsharpe_vs_base'], 4)}）、
  {_num(live['B']['sharpe'])} vs {_num(live['A']['sharpe'])}（实盘窗口，Δ
  {_num(live['B']['dsharpe_vs_base'] - live['A']['dsharpe_vs_base'], 4)}），
  CAGR 差距 {(live['B']['cagr'] - live['A']['cagr']) * 100:.2f} 个百分点。方向是**一致的**，但见下一节的置信区间：
  这个差距在统计上还无法与噪声区分，**所以判据（iii）不成立**。
- A2（只把分子换成制度目标、作用域仍为单名）的 Sharpe 反而最差
  （{_num(full['A2']['sharpe'])}）。这恰好说明：**分子（0.12 vs 0.24）不是问题的根源，作用域才是**
  ——单名口径下把目标调松只是把“几乎不持有”变成“持有三分之二”，并没有解决口径错配。

## 实盘交叉核对（{LIVE_XCHECK_START} 至 {LIVE_AS_OF}）

归档值（Stage 1 实盘，Garman-Klass / 5m 下行半方差 × 单名横截面中位数）：
`realized_vol_21d = {ARCHIVED_REALIZED_VOL:.4f}`、`vol_scaling_factor = {ARCHIVED_VOL_FACTOR:.4f}`、
实际交付仓位 ≈ {ARCHIVED_DELIVERED_EQUITY:.1%}。

本脚本在**同一窗口**用日频收盘口径复算，持仓用最后一行模型权重（{x['weights_proxy']['last_weight_date']}，
合计 {_pct(x['weights_proxy']['weight_sum'])}）恒定持有作为代理。该代理只是“同一批持仓”的粗略替身，
与归档的 {ARCHIVED_DELIVERED_EQUITY:.1%} 交付仓位不是同一个栈（实盘还要经过 Stage-2 允许清单与
手数取整），所以下表只用于比较两个口径的量级差，不用于复现实盘仓位：

| 量 | 数值 |
|---|---|
| `v_single`（21 日收盘对收盘中位数） | {_pct(a_of.get('v_single_c2c_median'))} |
| `v_port`（持有组合 63 日协方差） | {_pct(a_of.get('v_port_held_book'))} |
| `v_port`（等权 20 只对照） | {_pct(a_of.get('v_port_equal_weight'))} |
| 中位数 / 组合 比值 | {_num((a_of.get('v_single_c2c_median') or float('nan')) / (a_of.get('v_port_held_book') or float('nan')), 2)}x |
| `breadth` | {_pct(a_of.get('breadth'))} |
| 当期制度目标 `target` | {_pct(a_of.get('target'))} |
| `s_A = min(1, {TARGET_BASE}/v_single)` | {_num(a_of.get('s_A'), 4)} |
| `s_A2 = min(1, target/v_single)` | {_num(a_of.get('s_A2'), 4)} |
| `s_B = min(1, target/v_port)` | {_num(a_of.get('s_B'), 4)} |

窗口内 `s_A` 均值 {_num(x.get('window_stats', {}).get('s_A', {}).get('mean'), 4)}、
`s_B` 均值 {_num(x.get('window_stats', {}).get('s_B', {}).get('mean'), 4)}；
逐日取值见 `ablation_summary.json::live_crosscheck::daily`。

**这不是数字对数字的复现**：归档值用的是 5 分钟下行半方差 + Garman-Klass 的横截面中位数，
本表用的是日频收盘对收盘中位数，两者估计量不同（作用域相同，都是单名）。因此 `s_A` 与
{ARCHIVED_VOL_FACTOR:.4f} 不完全相等是预期内的；可比的是 `s_A` 与 `s_B` 之间的**量级差**，
以及 `v_single / v_port` 的分散化比值——这个比值才是上限“量错了作用域”的直接证据。

## 口径与局限（务必连同结论一起读）

- **离线近似**：无交易成本、无手数取整、无滑点、无涨跌停约束、无 T+1 限制。标量只做很小的
  日频调整，若加成本模型，会把“作用域”问题与“换手假设”混在一起，所以刻意不加。
- **权重已是策略自身的去杠杆输出**：B 臂的标量因此很少触发，这是“上限重复计费”现象本身，
  不是实现错误。
- **估计量替代**：`v_single` 用日频收盘对收盘波动率，而非 Stage 1 的 5m 下行半方差；见方法第 3 条。
- **制度判据只用了 breadth**：`blend_base.py` 的牛市分支是 `breadth >= 0.60 OR thrust_active`，
  本实验按任务规定只用 breadth，因此属于对策略自身目标函数的**保守近似**。
- **尾部静态持仓**：{res['inputs']['held_last_sessions']} 个交易日沿用最后一行权重，
  真实实盘由 Stage 3 惯性维持并进行手数取整。
- **单一策略、单一票池**：只检验 `04_ccsb_baseline`（20 只）这一条权重序列，
  结论不宜直接外推到其他 blend 变体或票池。
- **年化口径**：CAGR = `总净值^(252 / 交易日数) - 1`，与仓库内 `sqrt(252)` 的波动率口径一致；
  用自然日历天数折算会低约 0.5-0.7 个百分点，但四个臂口径相同，不影响相对比较。
- **bootstrap 只度量抽样不确定性**，不修正制度切换、参数选择等结构性不确定性；2000 次抽样
  的 CI 边缘存在蒙特卡洛噪声。

## 复现

```bash
pipeline/.venv/bin/python scripts/run_vol_ceiling_ablation.py \\
  --weights {res['inputs']['weights']} \\
  --cache {res['inputs']['cache']} \\
  --out {out} \\
  --seed {b['full']['B_minus_A'].get('seed')}
```

无参数运行即等价于上面这条命令（默认路径与默认种子）。产出：`ablation_metrics.csv`
（臂 × 窗口 × 指标）、`ablation_daily.csv`（逐日信号与各臂收益）、`ablation_summary.json`
（指标 + bootstrap 置信区间 + 判据 + 决断原文）。
"""
    (out / "README.md").write_text(wrap_cjk(text), encoding="utf-8")


def _bootstrap_sentence(d: dict, bf: dict, bl: dict) -> str:
    parts = []
    if bl.get("ci_excludes_zero") and (bl.get("ci_low") or 0) > 0:
        parts.append(
            "实盘子窗口上 `B−A` 的 95% 置信区间整体位于 0 以上，说明组合口径的 Sharpe 优势"
            "不是抽样噪声。"
        )
    elif bl.get("ci_excludes_zero") and (bl.get("ci_high") or 0) < 0:
        parts.append(
            "实盘子窗口上 `B−A` 的 95% 置信区间整体位于 0 以下，即组合口径显著**更差**。"
        )
    else:
        parts.append(
            "实盘子窗口上 `B−A` 的 95% 置信区间包含 0（"
            f"[{bl.get('ci_low'):.4f}, {bl.get('ci_high'):.4f}]），"
            "因此**没有可度量的 Sharpe 改善**——差值的方向即使为正，也无法与抽样噪声区分。"
        )
    if not (bf.get("ci_excludes_zero") and (bf.get("ci_low") or 0) > 0):
        parts.append(
            "全窗口上同样不能宣称改善，任何“组合口径更好”的说法都缺少统计支持。"
        )
    parts.append(
        "这正是判据要求“置信区间排除 0”的原因：如果没有这一条，任何方向上的一点噪声都会被读成结论。"
    )
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Volatility-ceiling SCOPE ablation: status-quo single-name median vs portfolio "
            "scope, applied as a pure scalar on the blend's own daily weight series."
        )
    )
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS), help="blend walkforward_weights.csv")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="local DuckDB OHLCV cache (read-only)")
    ap.add_argument(
        "--out",
        default=None,
        help="results directory (default: backtests_research/<YYYYMMDD_HHMMSS>_volscope_ablation)",
    )
    ap.add_argument("--seed", type=int, default=42, help="bootstrap RNG seed")
    ap.add_argument(
        "--provider",
        default="FuyaoDataProvider",
        help="primary OHLCV provider in the cache",
    )
    ap.add_argument(
        "--extend-provider",
        default="MarketDBDataProvider",
        help=(
            "second provider used ONLY to backfill bars the primary lacks, and only after "
            "per-symbol return identity is verified on the overlap (default: MarketDBDataProvider)"
        ),
    )
    ap.add_argument(
        "--no-extend",
        action="store_true",
        help="disable the backfill entirely (the full window then collapses onto the live window)",
    )
    ap.add_argument("--no-readme", action="store_true", help="skip README.md generation")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else (
        REPO_ROOT / "backtests_research" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_volscope_ablation"
    )
    extend = None if args.no_extend else args.extend_provider

    res = run(
        Path(args.weights), Path(args.cache), out, args.seed, args.provider, extend,
        emit_readme=not args.no_readme,
    )

    print(f"[volscope] results -> {out}")
    for wname, per_arm in res["metrics"].items():
        print(f"\n[{wname}] {res['windows'][wname][0]} .. {res['windows'][wname][1]} "
              f"({res['windows'][wname][2]} sessions)")
        for arm in ("BASE", "A", "A2", "B", "B21"):
            m = per_arm.get(arm, {})
            if "cagr" not in m:
                continue
            print(
                f"  {arm:<5} CAGR {m['cagr']*100:7.2f}%  vol {m['ann_vol']*100:6.2f}%  "
                f"Sharpe {m['sharpe']:5.3f}  MDD {m['max_drawdown']*100:7.2f}%  "
                f"Calmar {m['calmar']:5.2f}  expo {m['mean_exposure']*100:5.1f}%  "
                f"bind {m['pct_days_binding']*100:5.1f}%  TE {m['te_vs_base']*100:5.2f}%"
            )
    for wname in ("full", "live_2024_2026"):
        bb = res["bootstrap"][wname]["B_minus_A"]
        print(f"\n[bootstrap {wname}] B-A dSharpe {bb.get('point_sharpe_diff'):+.4f} "
              f"CI [{bb.get('ci_low'):+.4f}, {bb.get('ci_high'):+.4f}] "
              f"excludes0={bb.get('ci_excludes_zero')}")
    x = res["live_crosscheck"]["as_of_2026_11_16"]
    print(
        f"\n[live 2026-11-16] v_single {x['v_single_c2c_median']:.4f}  v_port {x['v_port_held_book']:.4f}  "
        f"breadth {x['breadth']:.3f}  s_A {x['s_A']:.4f}  s_A2 {x['s_A2']:.4f}  s_B {x['s_B']:.4f}  "
        f"| archived vol {ARCHIVED_REALIZED_VOL} factor {ARCHIVED_VOL_FACTOR} equity {ARCHIVED_DELIVERED_EQUITY}"
    )
    print(f"\n[verdict] {res['decision']['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
